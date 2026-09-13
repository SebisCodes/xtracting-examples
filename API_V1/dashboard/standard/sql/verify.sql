-- ============================================================
-- Verification: does the dashboard schema do what it claims?
--
--     docker compose exec -T xtracting_db \
--       psql -U xtracting -d xtracting_archive < ../dashboard/standard/sql/verify.sql
--
-- Same format as database/verify.sql: every check prints PASS or FAIL, the
-- last line says ALL n CHECKS PASSED, and everything runs inside one
-- transaction that is rolled back - the `_verify` rows written below never
-- reach the archive. Safe on an archive with real data: nothing here carries
-- anybody else's project name.
--
-- Needs the dashboard schema (01, 03, 02 have run - the dashboard does that
-- on start, or run them by hand) and the archive schema from database/init.
-- ============================================================

\set ON_ERROR_STOP on
\set QUIET on
\pset footer off

BEGIN;

CREATE TEMP TABLE _results (seq SERIAL, check_name TEXT, verdict TEXT, detail TEXT);

-- ── 1. Structure ────────────────────────────────────────────

INSERT INTO _results (check_name, verdict, detail)
SELECT 'dashboard schema exists',
       CASE WHEN count(*) = 1 THEN 'PASS' ELSE 'FAIL' END,
       count(*) || ' schema'
FROM pg_namespace WHERE nspname = 'dashboard';

INSERT INTO _results (check_name, verdict, detail)
SELECT 'the seven tables exist',
       CASE WHEN count(*) = 7 THEN 'PASS' ELSE 'FAIL' END,
       count(*) || '/7: ' || coalesce(string_agg(table_name, ', ' ORDER BY table_name), '-')
FROM information_schema.tables
WHERE table_schema = 'dashboard' AND table_type = 'BASE TABLE'
  AND table_name IN ('colour_groups', 'colour_group_types', 'buckets',
                     'bucket_members', 'settings', 'schema_version',
                     'access_tokens');

INSERT INTO _results (check_name, verdict, detail)
SELECT 'schema version recorded',
       CASE WHEN count(*) >= 1 THEN 'PASS' ELSE 'FAIL' END,
       'version ' || coalesce(max(integer_version)::text, '-')
FROM dashboard.schema_version;

-- ── 2. Colour groups ────────────────────────────────────────

INSERT INTO _results (check_name, verdict, detail)
SELECT 'exactly one fallback group',
       CASE WHEN count(*) FILTER (WHERE bool_fallback) = 1 THEN 'PASS' ELSE 'FAIL' END,
       count(*) FILTER (WHERE bool_fallback) || ' fallback of ' || count(*) || ' groups'
FROM dashboard.colour_groups;

INSERT INTO _results (check_name, verdict, detail)
SELECT 'the seeded groups are there',
       CASE WHEN count(*) = 11 THEN 'PASS' ELSE 'FAIL' END,
       count(*) || '/11 seeded keys'
FROM dashboard.colour_groups
WHERE text_key IN ('competitor', 'regulator', 'investor', 'person', 'ownership', 'customer',
                   'supplier', 'partner', 'product', 'location', 'other');

-- WCAG relative luminance, in SQL, so the seed cannot drift from what
-- app/colours.py checks: lines and swatches sit on white and must reach 3:1.
-- The channel transfer is a temporary function, gone with the transaction.
CREATE FUNCTION pg_temp.lin(v int) RETURNS double precision LANGUAGE sql IMMUTABLE AS $$
    SELECT CASE WHEN v / 255.0 <= 0.03928 THEN v / 255.0 / 12.92
                ELSE power((v / 255.0 + 0.055) / 1.055, 2.4) END
$$;

INSERT INTO _results (check_name, verdict, detail)
SELECT 'every group colour has 3:1 contrast on white',
       CASE WHEN count(*) FILTER (WHERE ratio < 3.0) = 0 THEN 'PASS' ELSE 'FAIL' END,
       'lowest ' || round(min(ratio)::numeric, 2) || ':1'
FROM (
    SELECT text_key,
           1.05 / (0.2126 * pg_temp.lin(('x' || substr(text_colour, 2, 2))::bit(8)::int)
                 + 0.7152 * pg_temp.lin(('x' || substr(text_colour, 4, 2))::bit(8)::int)
                 + 0.0722 * pg_temp.lin(('x' || substr(text_colour, 6, 2))::bit(8)::int) + 0.05) AS ratio
    FROM dashboard.colour_groups
) x;

INSERT INTO _results (check_name, verdict, detail)
SELECT 'every type assignment references a group',
       CASE WHEN count(*) FILTER (WHERE g.bigint_id IS NULL) = 0 THEN 'PASS' ELSE 'FAIL' END,
       count(*) || ' assignments, ' || count(*) FILTER (WHERE g.bigint_id IS NULL) || ' dangling'
FROM dashboard.colour_group_types t
LEFT JOIN dashboard.colour_groups g ON g.bigint_id = t.bigint_fk_group;

-- A second fallback must be refused by the partial unique index.
DO $$
DECLARE verdict TEXT := 'FAIL'; detail TEXT := 'a second fallback was accepted';
BEGIN
    BEGIN
        INSERT INTO dashboard.colour_groups (text_key, text_name, text_colour, bool_fallback)
        VALUES ('zz-verify-fallback', '_verify', '#000000', true);
    EXCEPTION WHEN unique_violation THEN
        verdict := 'PASS'; detail := 'unique_violation as expected';
    END;
    INSERT INTO _results (check_name, verdict, detail) VALUES ('a second fallback is refused', verdict, detail);
END $$;

DO $$
DECLARE verdict TEXT := 'FAIL'; detail TEXT := 'a named colour was accepted';
BEGIN
    BEGIN
        INSERT INTO dashboard.colour_groups (text_key, text_name, text_colour)
        VALUES ('zz-verify-colour', '_verify', 'red');
    EXCEPTION WHEN check_violation THEN
        verdict := 'PASS'; detail := 'check_violation as expected';
    END;
    INSERT INTO _results (check_name, verdict, detail) VALUES ('a colour must be six-digit hex', verdict, detail);
END $$;

-- ── 3. Buckets ──────────────────────────────────────────────
-- Three entities in three tasks: two Apples that are companies, one that is
-- a fruit. A bucket naming the two companies must resolve to exactly their
-- ids, in the same way app/scope.py does it (name, and type unless NULL).

INSERT INTO processed_data.entities
    (text_name, text_project, text_language, text_task_id, text_entity_id, text_type)
VALUES ('Apple Inc.', '_verify', 'English', '_verify:1', 'ent:apple-inc', 'Company'),
       ('Apple Inc.', '_verify', 'German',  '_verify:1', 'ent:apple-inc', 'Unternehmen'),
       ('Apple',      '_verify', 'English', '_verify:2', 'ent:apple',     'Company'),
       ('Apple',      '_verify', 'English', '_verify:3', 'ent:fruit',     'Fruit');

INSERT INTO dashboard.buckets (text_name, text_project) VALUES ('_verify Apple', '_verify');
INSERT INTO dashboard.bucket_members (bigint_fk_bucket, text_name, text_type)
SELECT bigint_id, m.name, m.type
FROM dashboard.buckets, (VALUES ('APPLE INC.', 'company'), ('Apple', 'Company')) AS m(name, type)
WHERE text_name = '_verify Apple';

INSERT INTO _results (check_name, verdict, detail)
SELECT 'a bucket resolves to the union of its members'' ids',
       CASE WHEN count(*) = 2 AND bool_and(id <> 'ent:fruit') THEN 'PASS' ELSE 'FAIL' END,
       count(*) || ' ids: ' || coalesce(string_agg(id, ', ' ORDER BY id), '-')
FROM (
    SELECT DISTINCT e.text_task_id AS task_id, e.text_entity_id AS id
    FROM processed_data.entities e
    JOIN dashboard.bucket_members m
      ON lower(e.text_name) = lower(m.text_name)
     AND (m.text_type IS NULL OR lower(e.text_type) = lower(m.text_type))
    JOIN dashboard.buckets b ON b.bigint_id = m.bigint_fk_bucket
    WHERE e.text_project = '_verify' AND b.text_name = '_verify Apple'
) ids;

DO $$
DECLARE verdict TEXT := 'FAIL'; detail TEXT := 'a duplicate member in another case was accepted';
BEGIN
    BEGIN
        INSERT INTO dashboard.bucket_members (bigint_fk_bucket, text_name, text_type)
        SELECT bigint_id, 'apple inc.', 'COMPANY' FROM dashboard.buckets WHERE text_name = '_verify Apple';
    EXCEPTION WHEN unique_violation THEN
        verdict := 'PASS'; detail := 'unique_violation as expected';
    END;
    INSERT INTO _results (check_name, verdict, detail) VALUES ('members are unique regardless of case', verdict, detail);
END $$;

DO $$
DECLARE b BIGINT; orphans INT;
BEGIN
    SELECT bigint_id INTO b FROM dashboard.buckets WHERE text_name = '_verify Apple';
    DELETE FROM dashboard.buckets WHERE bigint_id = b;
    SELECT count(*) INTO orphans FROM dashboard.bucket_members WHERE bigint_fk_bucket = b;
    INSERT INTO _results (check_name, verdict, detail)
    VALUES ('deleting a bucket removes its members',
            CASE WHEN orphans = 0 THEN 'PASS' ELSE 'FAIL' END,
            orphans || ' orphaned members');
END $$;

-- ── 4. Places ───────────────────────────────────────────────

INSERT INTO _results (check_name, verdict, detail)
SELECT 'places view and its unique index exist',
       CASE WHEN v = 1 AND i = 1 THEN 'PASS' ELSE 'FAIL' END,
       v || ' view, ' || i || ' unique index'
FROM (SELECT (SELECT count(*) FROM pg_matviews WHERE schemaname = 'dashboard' AND matviewname = 'places') AS v,
             (SELECT count(*) FROM pg_indexes WHERE schemaname = 'dashboard' AND tablename = 'places'
                 AND indexname = 'idx_places_key' AND indexdef LIKE 'CREATE UNIQUE%') AS i) x;

-- The split rule, on the view itself. A plain REFRESH inside this
-- transaction: CONCURRENTLY cannot run in a transaction block, and the
-- rollback at the end restores the previous contents anyway.
INSERT INTO processed_data.locations
    (text_name, text_project, text_language, text_task_id, text_fk_entity_id, text_address,
     float_latitude, float_longitude)
VALUES ('Works', '_verify', 'English', '_verify:1', 'ent:apple-inc', ' Springfield , Illinois, USA ', 39.8, -89.6),
       ('Works', '_verify', 'English', '_verify:2', 'ent:apple',     'Springfield, Illinois, USA',   39.8, -89.6),
       ('Port',  '_verify', 'English', '_verify:1', 'ent:apple-inc', 'Rotterdam, Netherlands',       51.9,   4.5),
       ('Prov',  '_verify', 'English', '_verify:3', 'ent:fruit',     'Linjiang',                     41.8, 126.9);

REFRESH MATERIALIZED VIEW dashboard.places;

-- One row per distinct (trimmed) address text: the oddly spaced Springfield
-- stays its own row, but its PARTS are trimmed like the tidy one's.
INSERT INTO _results (check_name, verdict, detail)
SELECT 'places split city / region / country as documented',
       CASE WHEN count(*) = 4
             AND bool_and(CASE text_address
                              WHEN 'Springfield, Illinois, USA'  THEN (text_city, text_region, text_country) = ('Springfield', 'Illinois', 'USA')
                                                                      AND integer_locations = 1 AND integer_entities = 1
                              WHEN 'Springfield , Illinois, USA' THEN (text_city, text_region, text_country) = ('Springfield', 'Illinois', 'USA')
                              WHEN 'Rotterdam, Netherlands'      THEN (text_city, text_country) = ('Rotterdam', 'Netherlands') AND text_region IS NULL
                              WHEN 'Linjiang'                    THEN text_city = 'Linjiang' AND text_region IS NULL AND text_country IS NULL
                              ELSE false END)
            THEN 'PASS' ELSE 'FAIL' END,
       count(*) || ' places: ' || coalesce(string_agg(text_address, '; ' ORDER BY text_address), '-')
FROM dashboard.places WHERE text_project = '_verify';

-- ── 5. Access tokens ────────────────────────────────────────
-- The gate calls a row live when it is enabled and either has no expiry or
-- has one in the future (dashboard/app/gate.py). Written out here against
-- four rows that cover the corners, because the whole value of an expiry is
-- that the day it passes nobody has to remember anything.

INSERT INTO dashboard.access_tokens (text_token, text_label, bool_enabled, date_expires)
VALUES ('_verify:live-forever', '_verify', true,  NULL),
       ('_verify:live-until',   '_verify', true,  NOW() + INTERVAL '30 days'),
       ('_verify:expired',      '_verify', true,  NOW() - INTERVAL '1 day'),
       ('_verify:disabled',     '_verify', false, NULL);

INSERT INTO _results (check_name, verdict, detail)
SELECT 'only an enabled, unexpired token is live',
       CASE WHEN count(*) = 2 THEN 'PASS' ELSE 'FAIL' END,
       count(*) || '/2 live: ' || coalesce(string_agg(text_token, ', ' ORDER BY text_token), '-')
FROM dashboard.access_tokens
WHERE text_label = '_verify'
  AND bool_enabled AND (date_expires IS NULL OR date_expires > NOW());

-- Two people cannot be handed the same string, so revoking it for one of
-- them cannot leave it working for the other.
DO $$
DECLARE verdict TEXT := 'FAIL'; detail TEXT := 'the duplicate was accepted';
BEGIN
    BEGIN
        INSERT INTO dashboard.access_tokens (text_token, text_label)
        VALUES ('_verify:live-forever', '_verify again');
    EXCEPTION WHEN unique_violation THEN
        verdict := 'PASS'; detail := 'unique_violation as expected';
    END;
    INSERT INTO _results (check_name, verdict, detail)
    VALUES ('the same token cannot be issued twice', verdict, detail);
END $$;

-- ── 6. Read-only ────────────────────────────────────────────
-- Last, because it turns the rest of this transaction read-only: the
-- dashboard's read pool opens every transaction this way, and an INSERT into
-- the archive must fail there. Temp tables stay writable, so the verdict can
-- still be recorded.

SET LOCAL transaction_read_only = on;

DO $$
DECLARE verdict TEXT := 'FAIL'; detail TEXT := 'the INSERT went through';
BEGIN
    BEGIN
        INSERT INTO processed_data.tasks (text_name, text_project, text_task_id)
        VALUES ('_verify', '_verify', '_verify:ro');
    EXCEPTION WHEN read_only_sql_transaction THEN
        verdict := 'PASS'; detail := 'read_only_sql_transaction as expected';
    END;
    INSERT INTO _results (check_name, verdict, detail)
    VALUES ('a read-only transaction cannot write processed_data', verdict, detail);
END $$;

-- ── Report ──────────────────────────────────────────────────
\pset tuples_only off
\echo ''
SELECT verdict, check_name, detail FROM _results ORDER BY seq;

\echo ''
SELECT CASE WHEN count(*) FILTER (WHERE verdict = 'FAIL') = 0
            THEN 'ALL ' || count(*) || ' CHECKS PASSED'
            ELSE count(*) FILTER (WHERE verdict = 'FAIL') || ' OF ' || count(*) || ' CHECKS FAILED'
       END AS result
FROM _results;

ROLLBACK;
