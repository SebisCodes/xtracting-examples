-- ============================================================
-- Dashboard - the two lists a suggestion box is answered from
--
-- Both are materialized views over tables that grow for ever, and both
-- exist for the same reason: a field that answers while somebody types
-- cannot re-derive its answer from millions of rows on every keystroke.
-- They are refreshed together, CONCURRENTLY, every
-- DASHBOARD_PLACES_REFRESH_MINUTES (app/schema.py: refresh_places) and
-- after the preseed in tests.
--
-- 1. dashboard.places       one row per address  (the Query page, the Heatmap)
-- 2. dashboard.source_hosts one row per host     (the "by domain" suggestions)
-- 3. dashboard.source_names one row per TITLE    (the "Source or host" field)
--
-- ── 1. places ───────────────────────────────────────────────
--
-- One row per distinct address per project and language, with the address
-- split into city / region / country and the coordinates averaged. This is
-- what the Query page's place suggestions and its disambiguation ("Springfield
-- - Illinois, USA or Ontario, Canada?") are answered from, and it is the only
-- geocoder the dashboard has: coordinates come from the archive or not at all.
--
-- A materialized view rather than a query, because the suggestion box asks on
-- every keystroke and processed_data.locations is a hypertable that grows for
-- ever. The refresh is CONCURRENT, which needs the unique index below.
--
-- THE ADDRESS RULE, mirrored in app/places.py (split_address) and unit-tested
-- against this file so the two cannot drift:
--
--   split on ',', trim each part, drop empty parts;
--   country = last part      if there are at least 2 parts
--   region  = second to last if there are at least 3 parts
--   city    = first part     always (a single part is a city and nothing else)
--
-- The expressions are spelled out rather than wrapped in a function so that
-- a search can re-derive the same parts from processed_data.locations directly
-- (see places.place_predicate) without depending on this view existing.
-- ============================================================

CREATE MATERIALIZED VIEW IF NOT EXISTS dashboard.places AS
WITH loc AS (
    SELECT l.text_project,
           l.text_language,
           btrim(l.text_address) AS text_address,
           l.float_latitude,
           l.float_longitude,
           l.text_fk_entity_id,
           array_remove(ARRAY(SELECT btrim(x) FROM unnest(string_to_array(l.text_address, ',')) AS x), '') AS parts
    FROM processed_data.locations l
    WHERE l.text_address IS NOT NULL AND btrim(l.text_address) <> ''
)
SELECT text_project,
       text_language,
       text_address,
       parts[1] AS text_city,
       CASE WHEN cardinality(parts) >= 3 THEN parts[cardinality(parts) - 1] END AS text_region,
       CASE WHEN cardinality(parts) >= 2 THEN parts[cardinality(parts)] END     AS text_country,
       avg(float_latitude)  AS float_latitude,
       avg(float_longitude) AS float_longitude,
       count(*)                          AS integer_locations,
       count(DISTINCT text_fk_entity_id) AS integer_entities
FROM loc
WHERE cardinality(parts) > 0
GROUP BY text_project, text_language, text_address, parts
WITH DATA;

-- Required by REFRESH MATERIALIZED VIEW CONCURRENTLY, and the natural key.
-- md5(text_address), not the address itself: a btree entry may not exceed
-- about 2704 bytes, and a real archive holds addresses far past that - one
-- of 4913 characters in the first import, six over a thousand. The index
-- only has to make the row unique for REFRESH ... CONCURRENTLY, and a hash
-- does that in 32 bytes whatever the extraction produced.
CREATE UNIQUE INDEX IF NOT EXISTS idx_places_key
    ON dashboard.places (text_project, text_language, md5(text_address));

-- The two lookups the suggestion box makes.
CREATE INDEX IF NOT EXISTS idx_places_city    ON dashboard.places (lower(text_city));
CREATE INDEX IF NOT EXISTS idx_places_country ON dashboard.places (lower(text_country));

-- ============================================================
-- ── 2. source_hosts ─────────────────────────────────────────
--
-- One row per host per project and language, with how many documents came
-- from it. This is what /api/suggest/domain and the Summary magnifier's
-- "Host" rows are answered from.
--
-- WHY IT IS A VIEW AND NOT A SUBQUERY. The host is not stored - the archive
-- keeps `text_uri` and the dashboard cuts the host out of it with the same
-- expression everywhere (app/sqlbuild.py: host_expression). Doing that
-- inside the suggestion query means splitting every URI in the project on
-- every keystroke: measured on a real archive - 750,000 documents in one
-- project - the prefix pass takes 10.1 s and the substring pass 11.7 s, so
-- the field cannot answer anybody who types. Against this view the same
-- two passes take 1.2 ms and 9.0 ms, because the whole list is 13,594 rows.
-- Building it costs one pass of 10.9 s, once per refresh, in the background
-- thread that refreshes dashboard.places (app/main.py: PlacesRefresher).
--
-- An expression index on processed_data.sources would be the other route and
-- is not open to us: that schema is the collector's and the dashboard only
-- reads it.
--
-- WHAT A REFRESH LAG MEANS. A host first seen since the last refresh is not
-- SUGGESTED yet; it is still FOUND, because the search itself matches
-- `text_uri` in the archive directly (app/scope.py) rather than this list.
-- The same lag the place suggestions have had since they were made a view.
CREATE MATERIALIZED VIEW IF NOT EXISTS dashboard.source_hosts AS
SELECT text_project,
       text_language,
       host,
       count(*) AS integer_sources
FROM (
    SELECT s.text_project,
           s.text_language,
           substring(s.text_uri from '^[a-zA-Z][a-zA-Z0-9+.-]*://([^/:?#]+)') AS host
    FROM processed_data.sources s
) AS split
-- A name longer than the DNS maximum is not a host, it is a malformed URI
-- the pattern happened to match; leaving it out keeps both indexes below
-- inside the btree's own limit as well.
WHERE host IS NOT NULL AND host <> '' AND length(host) <= 253
GROUP BY text_project, text_language, host
WITH DATA;

-- Required by REFRESH MATERIALIZED VIEW CONCURRENTLY, and the natural key.
CREATE UNIQUE INDEX IF NOT EXISTS idx_source_hosts_key
    ON dashboard.source_hosts (text_project, text_language, host);

-- The lookup the suggestion box makes: "every host of this project whose
-- name starts with what has been typed". text_pattern_ops is what lets
-- LIKE 'app%' use an index under a non-C collation.
CREATE INDEX IF NOT EXISTS idx_source_hosts_prefix
    ON dashboard.source_hosts (text_project, text_language, lower(host) text_pattern_ops);

-- ============================================================
-- ── 3. source_names ─────────────────────────────────────────
--
-- One row per document NAME per project and language - but only where that
-- name is a name. `processed_data.sources.text_name` is meant to hold the
-- document's title and on this archive it holds an identifier in every row:
-- 692,120 of the shape `src_1416664` and, in the newer rows, 61,530 32-digit
-- checksums, all 753,650 of them distinct.
--
-- Suggesting those is the fault this view exists to make impossible. A list
-- of one checksum per document, each with the count 1 - which is not a
-- counting error but the arithmetic of a column that is unique per row -
-- where a search for "vogue" matches nothing, while 44 documents come from
-- vogue.com. So the "Source or host" field now offers dashboard.source_hosts
-- first and this view second, and on an archive like today's this one is
-- simply empty: nothing unreadable is offered because nothing readable
-- exists. On an archive whose extraction does produce titles, they appear.
--
-- THE PATTERN BELOW IS THE SAME TEXT as app/sqlbuild.py: MACHINE_NAME_REGEX,
-- which app/scope.py and app/charts/drilldown.py both use.
-- tests/unit/test_sqlbuild.py compares them so they cannot drift apart.
--
-- A view rather than a subquery for the same measured reason as the hosts
-- above: matching the names directly costs 0.4 s per keystroke on 753,616
-- rows, and this list is short enough to answer in about a millisecond.
-- ONE REBUILD WHEN THE DEFINITION CHANGES, AND NOT ONE OTHERWISE.
--
-- `CREATE ... IF NOT EXISTS` leaves an existing view exactly as it was, which
-- is what makes this file cheap to re-apply and is also how an archive can go
-- on answering with the OLD definition for ever. The names it filters out are
-- the extraction's own ids, and "src:pump" was being hidden here and printed
-- in the drilldown beside it, so the two had to be made one - which means
-- every archive built before that has a view that still lets them through.
--
-- The drop is guarded on the definition itself: an archive whose view already
-- knows about `src:` keeps it, and the rebuild costs one GROUP BY over the
-- sources ONCE.
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_matviews
              WHERE schemaname = 'dashboard' AND matviewname = 'source_names'
                AND definition !~ 'src:') THEN
    DROP MATERIALIZED VIEW dashboard.source_names;
  END IF;
END $$;

CREATE MATERIALIZED VIEW IF NOT EXISTS dashboard.source_names AS
SELECT s.text_project,
       s.text_language,
       s.text_name,
       min(substring(s.text_uri from '^[a-zA-Z][a-zA-Z0-9+.-]*://([^/:?#]+)')) AS host,
       count(*) AS integer_sources
FROM processed_data.sources s
WHERE s.text_name IS NOT NULL
  AND s.text_name <> ''
  AND s.text_name !~* '^(src_[0-9]+|src:\S+|ent:\S+|[0-9a-f]{32}|[0-9a-f]{40}|[0-9a-f]{64})$'
  -- Same btree limit as the places index: a "name" that long is not a title.
  AND length(s.text_name) <= 500
GROUP BY s.text_project, s.text_language, s.text_name
WITH DATA;

-- Required by REFRESH MATERIALIZED VIEW CONCURRENTLY, and the natural key.
CREATE UNIQUE INDEX IF NOT EXISTS idx_source_names_key
    ON dashboard.source_names (text_project, text_language, text_name);

CREATE INDEX IF NOT EXISTS idx_source_names_prefix
    ON dashboard.source_names (text_project, text_language, lower(text_name) text_pattern_ops);
