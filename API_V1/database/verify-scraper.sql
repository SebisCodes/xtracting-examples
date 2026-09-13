-- ============================================================
-- Verification of the crawler and dashboard-configuration schemas
--
--     docker compose exec -T xtracting_db \
--       psql -U xtracting -d xtracting_archive -f /verify-scraper.sql
--
-- The companion of verify.sql for what init/03-scraper.sql adds. A separate
-- file, so the archive suite keeps its documented twenty-six checks and can
-- still be run against an archive that never loaded 03-scraper.sql.
--
-- Same rules as verify.sql: writes into a throwaway source, proves every
-- claim, rolls everything back. Every check prints PASS or FAIL, and a check
-- that cannot be answered prints FAIL rather than nothing. The role checks
-- are the one deliberate exception: 04-roles.sh is opt-in, so an archive
-- without application roles passes them with a note saying so.
-- ============================================================

\set ON_ERROR_STOP on
\set QUIET on
\pset footer off

BEGIN;

CREATE TEMP TABLE _results (seq SERIAL, check_name TEXT, verdict TEXT, detail TEXT);

-- A CHECK constraint proves itself by refusing. Each refusal is caught in
-- its own block, so the transaction survives to run the next check.
CREATE FUNCTION pg_temp.expect_rejected(check_name TEXT, stmt TEXT) RETURNS void AS $$
BEGIN
    EXECUTE stmt;
    INSERT INTO _results (check_name, verdict, detail)
    VALUES (check_name, 'FAIL', 'accepted');
EXCEPTION
    WHEN check_violation OR unique_violation THEN
        INSERT INTO _results (check_name, verdict, detail)
        VALUES (check_name, 'PASS', 'rejected: ' || SQLERRM);
END
$$ LANGUAGE plpgsql;

-- ── 1. Structure ────────────────────────────────────────────
INSERT INTO _results (check_name, verdict, detail)
SELECT 'scraper_config and crawler schemas exist',
       CASE WHEN count(*) = 2 THEN 'PASS' ELSE 'FAIL' END,
       count(*) || '/2 schemas'
FROM information_schema.schemata
WHERE schema_name IN ('scraper_config', 'scraper');

-- Everything a person edits or the crawler updates is a plain table. A
-- hypertable here would turn every status change into a decompression and
-- could not carry the unique indexes a queue needs.
INSERT INTO _results (check_name, verdict, detail)
SELECT 'configuration and state tables are PLAIN',
       CASE WHEN count(*) FILTER (WHERE is_hyper) = 0 AND count(*) = 13 THEN 'PASS' ELSE 'FAIL' END,
       count(*) FILTER (WHERE NOT is_hyper) || '/' || count(*) || ' plain'
FROM (
    SELECT t.table_schema, t.table_name,
           EXISTS (SELECT 1 FROM timescaledb_information.hypertables h
                   WHERE h.hypertable_schema = t.table_schema
                     AND h.hypertable_name = t.table_name) AS is_hyper
    FROM information_schema.tables t
    WHERE (t.table_schema, t.table_name) IN (
        ('scraper_config', 'sources'), ('scraper_config', 'source_patterns'),
        ('scraper_config', 'source_exact_urls'), ('scraper_config', 'source_file_rules'),
        ('scraper_config', 'test_snapshots'), ('scraper_config', 'source_projects'),
        ('scraper_config', 'manual_runs'),
        ('scraper', 'projects'), ('scraper', 'api_keys'),
        ('scraper', 'targets'), ('scraper', 'seen_urls'), ('scraper', 'submit_queue'),
        ('scraper', 'files'))
) x;

-- The registry, and the four columns that point at it. Written out by name
-- rather than counted: the whole point of the additive ALTER section in
-- 03-scraper.sql is that these exist on a grown archive as well as a fresh one,
-- and only naming them catches the day one is added to the CREATE and forgotten
-- in the ALTER.
INSERT INTO _results (check_name, verdict, detail)
SELECT 'the key registry carries what the chooser reads',
       CASE WHEN count(*) = 8 THEN 'PASS' ELSE 'FAIL' END,
       count(*) || '/8 columns'
FROM information_schema.columns
WHERE (table_schema, table_name, column_name) IN (
    ('scraper', 'projects', 'text_project_id'),
    ('scraper', 'projects', 'date_last_seen'),
    ('scraper', 'projects', 'bool_keys_detailed'),
    ('scraper', 'api_keys', 'text_prefix'),
    ('scraper', 'api_keys', 'text_project_id'),
    ('scraper', 'api_keys', 'bool_can_extract'),
    ('scraper', 'api_keys', 'integer_order'),
    ('scraper', 'api_keys', 'integer_effort'));

INSERT INTO _results (check_name, verdict, detail)
SELECT 'a project, a link group and an address can each name a key',
       CASE WHEN count(*) = 6 THEN 'PASS' ELSE 'FAIL' END,
       count(*) || '/6 columns'
FROM information_schema.columns
WHERE (table_schema, table_name, column_name) IN (
    ('scraper_config', 'source_projects', 'text_project_id'),
    ('scraper_config', 'source_projects', 'text_key_prefix'),
    ('scraper_config', 'source_patterns', 'text_key_prefix'),
    ('scraper_config', 'source_exact_urls', 'text_key_prefix'),
    ('scraper', 'submit_queue', 'text_project_id'),
    ('scraper', 'submit_queue', 'text_key_prefix'));

-- Sources carry no single project column: a page belongs to as many
-- projects as it is wanted by, and a column holding one of them is the
-- kind of thing something else starts reading.
INSERT INTO _results (check_name, verdict, detail)
SELECT 'a watchlist carries no single project of its own',
       CASE WHEN count(*) = 0 THEN 'PASS' ELSE 'FAIL' END,
       count(*) || ' leftover column(s)'
FROM information_schema.columns
WHERE table_schema = 'scraper_config' AND table_name = 'sources'
  AND column_name IN ('text_project_id', 'text_key_prefix');

-- The address gate is per project, which is what makes one page serve two.
INSERT INTO _results (check_name, verdict, detail)
SELECT 'the address gate is keyed by project and address together',
       CASE WHEN string_agg(a.attname, ',' ORDER BY k.ord) = 'text_project_id,text_uri_hash'
            THEN 'PASS' ELSE 'FAIL' END,
       coalesce(string_agg(a.attname, ', ' ORDER BY k.ord), 'no primary key')
FROM pg_index i
JOIN LATERAL unnest(i.indkey) WITH ORDINALITY AS k(attnum, ord) ON TRUE
JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = k.attnum
WHERE i.indrelid = 'scraper.seen_urls'::regclass AND i.indisprimary;

-- A SUBMISSION IS PROVISIONAL UNTIL THE COLLECTOR HAS CONFIRMED IT. Without
-- these two columns the crawler writes an address off as collected the moment
-- it submits, and an afternoon of collector downtime is an afternoon of pages
-- that are never offered again (crawler/app/store.py: SUBMISSION_GRACE).
INSERT INTO _results (check_name, verdict, detail)
SELECT 'a submitted address carries a clock and a confirmation',
       CASE WHEN count(*) = 2 THEN 'PASS' ELSE 'FAIL' END,
       coalesce(string_agg(column_name, ', ' ORDER BY column_name), 'neither column is there')
FROM information_schema.columns
WHERE table_schema = 'scraper' AND table_name = 'seen_urls'
  AND column_name IN ('date_submitted', 'date_confirmed');

-- The partial index the "what has not come back?" question walks.
INSERT INTO _results (check_name, verdict, detail)
SELECT 'the addresses still waiting for a result have an index of their own',
       CASE WHEN count(*) = 1 THEN 'PASS' ELSE 'FAIL' END,
       coalesce(string_agg(indexname, ', '), 'no partial index on the unconfirmed rows')
FROM pg_indexes
WHERE schemaname = 'scraper' AND tablename = 'seen_urls'
  AND indexname = 'idx_seen_urls_unconfirmed';

-- '' and not NULL, because '' means "inherit" everywhere in this schema and a
-- nullable column would give the same idea two spellings.
INSERT INTO _results (check_name, verdict, detail)
SELECT 'a key that was never chosen reads as empty, not null',
       CASE WHEN count(*) FILTER (WHERE is_nullable = 'YES') = 0
             AND count(*) FILTER (WHERE column_default LIKE '''''%') = 4
            THEN 'PASS' ELSE 'FAIL' END,
       count(*) FILTER (WHERE is_nullable = 'NO') || '/4 not null, '
       || count(*) FILTER (WHERE column_default LIKE '''''%') || ' default to empty'
FROM information_schema.columns
WHERE column_name = 'text_key_prefix'
  AND (table_schema, table_name) IN (
      ('scraper_config', 'source_projects'), ('scraper_config', 'source_patterns'),
      ('scraper_config', 'source_exact_urls'), ('scraper', 'submit_queue'));

-- The effort ladder has four steps and a "not known". Anything else is a step
-- the dashboard has no name for.
SELECT pg_temp.expect_rejected(
    'an effort outside the four steps is refused',
    $$INSERT INTO scraper.api_keys (text_prefix, integer_effort)
      VALUES ('_crawlertest_effort', 9)$$);

INSERT INTO _results (check_name, verdict, detail)
SELECT 'documents and scraper_runs are hypertables, 1-day chunks',
       CASE WHEN count(*) = 2 AND count(*) FILTER (WHERE time_interval = INTERVAL '1 day') = 2
            THEN 'PASS' ELSE 'FAIL' END,
       count(*) || '/2 hypertables, ' ||
       count(*) FILTER (WHERE time_interval = INTERVAL '1 day') || ' with 1-day chunks'
FROM timescaledb_information.dimensions
WHERE (hypertable_schema, hypertable_name) IN (('scraper', 'documents'), ('monitoring', 'scraper_runs'))
  AND column_name = 'date_added';

-- Two days for documents: reconcile updates fresh rows, and the dashboard
-- reads them. One day for runs, like collector_runs.
INSERT INTO _results (check_name, verdict, detail)
SELECT 'documents and runs compress after 30 days',
       CASE WHEN count(*) = 2 THEN 'PASS' ELSE 'FAIL' END,
       coalesce(string_agg(hypertable_name || ': ' || (config->>'compress_after'), ', '), 'no policy')
FROM timescaledb_information.jobs
WHERE proc_name = 'policy_compression'
  AND ((hypertable_schema, hypertable_name, config->>'compress_after') = ('scraper', 'documents', '30 days')
    OR (hypertable_schema, hypertable_name, config->>'compress_after') = ('monitoring', 'scraper_runs', '30 days'));

INSERT INTO _results (check_name, verdict, detail)
SELECT 'documents segment by source and project',
       CASE WHEN bool_or(attname = 'bigint_fk_source' AND segmentby_column_index = 1)
             AND bool_or(attname = 'text_project' AND segmentby_column_index = 2)
            THEN 'PASS' ELSE 'FAIL' END,
       coalesce(string_agg(attname, ', ' ORDER BY segmentby_column_index)
                FILTER (WHERE segmentby_column_index IS NOT NULL), 'none')
FROM timescaledb_information.compression_settings
WHERE hypertable_schema = 'scraper' AND hypertable_name = 'documents';

-- The run table has the archive's shape: the three identity columns and
-- their indexes, so a project split treats it like every other table.
INSERT INTO _results (check_name, verdict, detail)
SELECT 'scraper_runs carries the identity columns, indexed',
       CASE WHEN cols = 3 AND idx = 3 THEN 'PASS' ELSE 'FAIL' END,
       cols || '/3 columns, ' || idx || '/3 indexes'
FROM (
    SELECT (SELECT count(*) FROM information_schema.columns
            WHERE table_schema = 'monitoring' AND table_name = 'scraper_runs'
              AND column_name IN ('text_project', 'text_language', 'text_task_id')) AS cols,
           (SELECT count(*) FROM pg_indexes
            WHERE schemaname = 'monitoring' AND tablename = 'scraper_runs'
              AND indexname ~ '_(project|language|task)$') AS idx
) x;

INSERT INTO _results (check_name, verdict, detail)
SELECT 'scraper_runs has the shape of collector_runs plus its counters',
       CASE WHEN count(*) FILTER (WHERE in_runs) = count(*) THEN 'PASS' ELSE 'FAIL' END,
       count(*) FILTER (WHERE in_runs) || '/' || count(*) || ' columns'
FROM (
    SELECT c.column_name,
           EXISTS (SELECT 1 FROM information_schema.columns s
                   WHERE s.table_schema = 'monitoring' AND s.table_name = 'scraper_runs'
                     AND s.column_name = c.column_name) AS in_runs
    FROM (SELECT column_name FROM information_schema.columns
          WHERE table_schema = 'monitoring' AND table_name = 'collector_runs'
          UNION ALL
          SELECT unnest(ARRAY['bigint_fk_source', 'integer_pages', 'integer_links',
                              'integer_accepted', 'integer_new', 'integer_files',
                              'integer_submitted', 'integer_ms'])) c
) x;

-- The two lookups the crawler makes against the archive, and the one the
-- scheduler makes every tick.
INSERT INTO _results (check_name, verdict, detail)
SELECT 'archive indexes for source uri and tag present',
       CASE WHEN count(*) = 2 THEN 'PASS' ELSE 'FAIL' END,
       count(*) || '/2 on processed_data.tasks'
FROM pg_indexes
WHERE schemaname = 'processed_data' AND tablename = 'tasks'
  AND indexname IN ('idx_tasks_source_uri', 'idx_tasks_tag');

INSERT INTO _results (check_name, verdict, detail)
SELECT 'due-target index is partial on the lease',
       CASE WHEN count(*) = 1 THEN 'PASS' ELSE 'FAIL' END,
       coalesce(min(indexdef), 'idx_targets_due missing')
FROM pg_indexes
WHERE schemaname = 'scraper' AND indexname = 'idx_targets_due'
  AND indexdef ILIKE '%WHERE (NOT bool_leased)%';

INSERT INTO _results (check_name, verdict, detail)
SELECT 'newest-snapshot-per-source index present',
       CASE WHEN count(*) = 1 THEN 'PASS' ELSE 'FAIL' END,
       coalesce(min(indexdef), 'idx_test_snapshots_source missing')
FROM pg_indexes
WHERE schemaname = 'scraper_config' AND indexname = 'idx_test_snapshots_source'
  AND indexdef LIKE '%(bigint_fk_source, date_added DESC)%';

-- The error log. A run row says a run failed; these rows say what failed,
-- and the log view reads them newest first, filtered by source, kind or
-- severity - which is exactly the four indexes below.
INSERT INTO _results (check_name, verdict, detail)
SELECT 'scraper_errors is a hypertable with 1-day chunks',
       CASE WHEN count(*) = 1 THEN 'PASS' ELSE 'FAIL' END,
       coalesce(min(time_interval)::text, 'no dimension on date_added')
FROM timescaledb_information.dimensions
WHERE hypertable_schema = 'monitoring' AND hypertable_name = 'scraper_errors'
  AND column_name = 'date_added' AND time_interval = INTERVAL '1 day';

-- The log is the one table here that is thrown away again: it is read
-- while it is fresh. Both numbers are the ones 03-scraper.sql names in its
-- init/05-policies.sql, so a customer who changed a number there sees this
-- check say which numbers are in force.
INSERT INTO _results (check_name, verdict, detail)
SELECT 'scraper_errors compresses after 30 days and is kept 90',
       CASE WHEN count(*) FILTER (WHERE proc_name = 'policy_compression'
                                    AND config->>'compress_after' = '30 days') = 1
             AND count(*) FILTER (WHERE proc_name = 'policy_retention'
                                    AND config->>'drop_after' = '90 days') = 1
            THEN 'PASS' ELSE 'FAIL' END,
       coalesce(string_agg(proc_name || ': ' ||
                           coalesce(config->>'compress_after', config->>'drop_after'),
                           ', ' ORDER BY proc_name), 'no policy')
FROM timescaledb_information.jobs
WHERE hypertable_schema = 'monitoring' AND hypertable_name = 'scraper_errors';

INSERT INTO _results (check_name, verdict, detail)
SELECT 'scraper_errors carries the identity columns and its own',
       CASE WHEN identity = 3 AND own = 6 THEN 'PASS' ELSE 'FAIL' END,
       identity || '/3 identity columns, ' || own || '/6 of its own'
FROM (
    SELECT count(*) FILTER (WHERE column_name IN
               ('text_project', 'text_language', 'text_task_id')) AS identity,
           count(*) FILTER (WHERE column_name IN
               ('bigint_fk_source', 'text_source_name', 'text_host', 'text_kind',
                'text_severity', 'text_message')) AS own
    FROM information_schema.columns
    WHERE table_schema = 'monitoring' AND table_name = 'scraper_errors'
) x;

INSERT INTO _results (check_name, verdict, detail)
SELECT 'the four log indexes are present, newest first',
       CASE WHEN count(*) = 4 THEN 'PASS' ELSE 'FAIL' END,
       count(*) || '/4: ' || coalesce(string_agg(indexname, ', ' ORDER BY indexname), 'none')
FROM pg_indexes
WHERE schemaname = 'monitoring' AND tablename = 'scraper_errors'
  AND indexname IN ('idx_scraper_errors_time', 'idx_scraper_errors_source',
                    'idx_scraper_errors_kind', 'idx_scraper_errors_severity')
  AND indexdef LIKE '%date_added DESC%';

-- The step log. The table above answers "what went wrong"; this one answers
-- "what is it doing right now" - one row per step the crawler or the
-- collector took while somebody had its switch on. Checked here rather than
-- taken on trust because the Log view's two newest tabs read nothing else,
-- and a missing table there shows as an empty panel that looks exactly like
-- a switch nobody turned on.
INSERT INTO _results (check_name, verdict, detail)
SELECT 'the step log exists and carries what the Log view reads',
       CASE WHEN count(*) = 7 THEN 'PASS' ELSE 'FAIL' END,
       count(*) || '/7 columns'
FROM information_schema.columns
WHERE (table_schema, table_name, column_name) IN (
    ('monitoring', 'service_log', 'text_service'),
    ('monitoring', 'service_log', 'text_action'),
    ('monitoring', 'service_log', 'text_detail'),
    ('monitoring', 'service_log', 'text_run_id'),
    ('monitoring', 'service_log', 'bigint_fk_source'),
    ('monitoring', 'service_log', 'text_project'),
    ('monitoring', 'service_log', 'integer_ms'));

-- ONE-HOUR CHUNKS, and that is not the same number as everywhere else in
-- this file on purpose: with rows that live for a day, a one-day chunk
-- cannot be dropped until it is two days old, so the table would keep twice
-- what it promises and lose it in one lump. Hourly chunks let it walk
-- forward smoothly, which is why the interval is checked and not only the
-- fact that it is a hypertable.
INSERT INTO _results (check_name, verdict, detail)
SELECT 'the step log is a hypertable with 1-hour chunks',
       CASE WHEN count(*) = 1 THEN 'PASS' ELSE 'FAIL' END,
       coalesce(min(time_interval)::text, 'no 1-hour dimension on date_added')
FROM timescaledb_information.dimensions
WHERE hypertable_schema = 'monitoring' AND hypertable_name = 'service_log'
  AND column_name = 'date_added' AND time_interval = INTERVAL '1 hour';

-- TWENTY-FOUR HOURS, against the ninety days of the problem log two checks
-- up. Two questions, two lifetimes: switched on, this table is written a row
-- per step, and none of those rows is interesting tomorrow. A missing policy
-- is the failure that matters - it is silent, it looks like nothing at all,
-- and it fills a disk.
--
-- Compared as an INTERVAL and not as text: TimescaleDB stores this one as
-- '24:00:00' and the ninety days above as '90 days', so a check written the
-- same way as its neighbour would fail on a policy that is perfectly right.
INSERT INTO _results (check_name, verdict, detail)
SELECT 'the step log is thrown away after 24 hours',
       CASE WHEN count(*) FILTER (
                WHERE proc_name = 'policy_retention'
                  AND (config->>'drop_after')::interval = INTERVAL '24 hours') = 1
            THEN 'PASS' ELSE 'FAIL' END,
       coalesce(string_agg(proc_name || ': ' ||
                           coalesce(config->>'drop_after', config->>'compress_after'),
                           ', ' ORDER BY proc_name), 'no policy')
FROM timescaledb_information.jobs
WHERE hypertable_schema = 'monitoring' AND hypertable_name = 'service_log';

-- ── 2. The constraints refuse what the editor refuses ───────
-- The probe function returns nothing worth printing; its verdicts land in
-- _results. Output goes to /dev/null for this section so the report at the
-- end is the only thing on screen.
\o /dev/null
INSERT INTO scraper_config.sources (text_name, text_list_url, text_host)
VALUES ('_verify source', 'https://example.invalid/rent/list', 'example.invalid');

INSERT INTO _results (check_name, verdict, detail)
SELECT 'a saved source is off until enabled',
       CASE WHEN bool_enabled = false AND bool_respect_robots THEN 'PASS' ELSE 'FAIL' END,
       'enabled=' || bool_enabled || ', respect_robots=' || bool_respect_robots
FROM scraper_config.sources WHERE text_name = '_verify source';

SELECT pg_temp.expect_rejected('unknown mode is rejected',
    $q$UPDATE scraper_config.sources SET text_mode = 'everything' WHERE text_name = '_verify source'$q$);
SELECT pg_temp.expect_rejected('unknown format is rejected',
    $q$UPDATE scraper_config.sources SET text_format = 'markdown' WHERE text_name = '_verify source'$q$);
SELECT pg_temp.expect_rejected('unknown engine is rejected',
    $q$UPDATE scraper_config.sources SET text_engine = 'curl' WHERE text_name = '_verify source'$q$);
SELECT pg_temp.expect_rejected('ignoring robots.txt without a reason is rejected',
    $q$UPDATE scraper_config.sources SET bool_respect_robots = false WHERE text_name = '_verify source'$q$);

-- And with a reason it goes through - the constraint asks for a reason, it
-- does not forbid the decision.
UPDATE scraper_config.sources
SET bool_respect_robots = false, text_override_reason = 'written permission from the operator, ticket 1234'
WHERE text_name = '_verify source';
INSERT INTO _results (check_name, verdict, detail)
SELECT 'ignoring robots.txt with a reason is accepted',
       CASE WHEN count(*) = 1 THEN 'PASS' ELSE 'FAIL' END, count(*) || ' row(s)'
FROM scraper_config.sources
WHERE text_name = '_verify source' AND NOT bool_respect_robots;

-- The same shape twice is one pattern.
INSERT INTO scraper_config.source_patterns
    (bigint_fk_source, text_kind, text_label, text_regex, json_form, integer_examples)
SELECT bigint_id, 'accept', '/rent/#', '^https://example\.invalid/rent/\d+$',
       '{"host": "example.invalid", "segments": ["rent", "#"], "query_keys": []}', 20
FROM scraper_config.sources WHERE text_name = '_verify source';
SELECT pg_temp.expect_rejected('the same pattern twice is rejected',
    $q$INSERT INTO scraper_config.source_patterns
           (bigint_fk_source, text_kind, text_label, text_regex, json_form)
       SELECT bigint_id, 'accept', '/rent/#', '^https://example\.invalid/rent/\d+$', '{}'
       FROM scraper_config.sources WHERE text_name = '_verify source'$q$);

SELECT pg_temp.expect_rejected('unknown queue status is rejected',
    $q$INSERT INTO scraper.submit_queue
           (text_tag, bigint_fk_source, date_document_added, bigint_fk_document, text_status)
       VALUES ('xs_1_verify', 1, NOW(), 1, 'DONE')$q$);
SELECT pg_temp.expect_rejected('a tag with a hyphen is rejected',
    $q$INSERT INTO scraper.submit_queue
           (text_tag, bigint_fk_source, date_document_added, bigint_fk_document)
       VALUES ('xs-1-verify', 1, NOW(), 1)$q$);
SELECT pg_temp.expect_rejected('unknown snapshot status is rejected',
    $q$INSERT INTO scraper_config.test_snapshots (json_config, text_status)
       VALUES ('{}', 'PENDING')$q$);
SELECT pg_temp.expect_rejected('unknown run status is rejected',
    $q$INSERT INTO monitoring.scraper_runs (text_name, text_status)
       VALUES ('_verify run', 'MAYBE')$q$);

-- A log row nobody can filter by is a log row nobody finds: both columns
-- the log view groups on are closed lists, and the database says so.
SELECT pg_temp.expect_rejected('unknown error kind is rejected',
    $q$INSERT INTO monitoring.scraper_errors (text_name, text_kind, text_message)
       VALUES ('_verify error', 'SOMETHING_ELSE', 'nonsense')$q$);
SELECT pg_temp.expect_rejected('unknown error severity is rejected',
    $q$INSERT INTO monitoring.scraper_errors
           (text_name, text_kind, text_severity, text_message)
       VALUES ('_verify error', 'NETWORK', 'critical', 'nonsense')$q$);

\o

-- ── 3. Behaviour ────────────────────────────────────────────
-- Three snapshots: a draft, an older DONE one, a newer DONE one, and a
-- FAILED one newer still. The preview must pick the newest DONE.
INSERT INTO scraper_config.test_snapshots (date_added, bigint_fk_source, json_config, text_status, json_result)
SELECT NOW() - INTERVAL '3 hours', NULL::bigint, '{"draft": true}'::jsonb, 'DONE', '{"which": "draft"}'::jsonb
UNION ALL
SELECT NOW() - INTERVAL '2 hours', bigint_id, '{}',              'DONE',   '{"which": "older"}'
FROM scraper_config.sources WHERE text_name = '_verify source'
UNION ALL
SELECT NOW() - INTERVAL '1 hour',  bigint_id, '{}',              'DONE',   '{"which": "newest"}'
FROM scraper_config.sources WHERE text_name = '_verify source'
UNION ALL
SELECT NOW(),                      bigint_id, '{}',              'FAILED', NULL
FROM scraper_config.sources WHERE text_name = '_verify source';

INSERT INTO _results (check_name, verdict, detail)
SELECT 'newest DONE snapshot per source is found',
       CASE WHEN which = 'newest' THEN 'PASS' ELSE 'FAIL' END,
       coalesce(which, 'no DONE snapshot found')
FROM (SELECT (SELECT t.json_result->>'which'
              FROM scraper_config.test_snapshots t
              JOIN scraper_config.sources s ON s.bigint_id = t.bigint_fk_source
              WHERE s.text_name = '_verify source' AND t.text_status = 'DONE'
              ORDER BY t.date_added DESC
              LIMIT 1) AS which) x;

-- A document, its queue item, its run - the join the Sources page makes,
-- through the hypertable by (date, id) so it touches one chunk.
INSERT INTO scraper.documents
    (text_name, bigint_fk_source, text_uri_canonical, text_uri_hash,
     text_content, text_content_hash, integer_char_count, text_tag)
SELECT 'Flat 4001234', bigint_id, 'https://example.invalid/rent/4001234',
       encode(sha256('https://example.invalid/rent/4001234'::bytea), 'hex'),
       'Two rooms, Basel.', encode(sha256('Two rooms, Basel.'::bytea), 'hex'), 17,
       'xs_' || bigint_id || '_0123456789ab'
FROM scraper_config.sources WHERE text_name = '_verify source';

INSERT INTO scraper.submit_queue
    (text_tag, bigint_fk_source, date_document_added, bigint_fk_document,
     text_source_uri, text_content_hash, integer_char_count)
SELECT text_tag, bigint_fk_source, date_added, bigint_id,
       text_uri_canonical, text_content_hash, integer_char_count
FROM scraper.documents WHERE text_name = 'Flat 4001234';

INSERT INTO monitoring.scraper_runs
    (text_name, text_status, bigint_fk_source, integer_pages, integer_links,
     integer_accepted, integer_new, integer_submitted, integer_ms)
SELECT 'source:' || bigint_id, 'OK', bigint_id, 1, 24, 20, 1, 1, 812
FROM scraper_config.sources WHERE text_name = '_verify source';

INSERT INTO _results (check_name, verdict, detail)
SELECT 'queue item joins its document and last run',
       CASE WHEN count(*) = 1 THEN 'PASS' ELSE 'FAIL' END, count(*) || ' row(s)'
FROM scraper.submit_queue q
JOIN scraper.documents d
  ON d.date_added = q.date_document_added AND d.bigint_id = q.bigint_fk_document
JOIN monitoring.scraper_runs r ON r.bigint_fk_source = q.bigint_fk_source
WHERE q.text_status = 'PENDING' AND d.text_name = 'Flat 4001234' AND r.text_status = 'OK';

-- The reconcile: the platform echoes the tag back, whole or with the split
-- suffix, and processed_data.tasks answers through idx_tasks_tag.
INSERT INTO processed_data.tasks
    (text_name, text_project, text_language, text_task_id, text_job_id, text_status, text_source_uri, text_tag)
SELECT '_verify:task:0', '_verify', 'English', 'task_v', 'job_v', 'COMPLETED',
       text_source_uri, text_tag || '-01'
FROM scraper.submit_queue WHERE text_tag LIKE 'xs\_%\_0123456789ab';

INSERT INTO _results (check_name, verdict, detail)
SELECT 'a split tag still reconciles against the archive',
       CASE WHEN count(*) = 1 THEN 'PASS' ELSE 'FAIL' END, count(*) || ' archived'
FROM scraper.submit_queue q
JOIN processed_data.tasks t
  ON t.text_tag = q.text_tag OR t.text_tag LIKE q.text_tag || '-%'
WHERE t.text_project = '_verify';

-- Three log rows for one source, written a minute apart, read back the way
-- the log view reads them: newest first, and the sentence a person acts on
-- comes back whole. The middle one is a test crawl - same wording, severity
-- info - because that is what the dashboard's test runner writes.
INSERT INTO monitoring.scraper_errors
    (date_added, text_name, bigint_fk_source, text_source_name, text_host,
     text_kind, text_severity, text_uri, integer_http_status, text_message,
     text_detail, text_run_id)
SELECT NOW() - INTERVAL '2 minutes', 'robots forbids the list page', bigint_id,
       text_name, 'example.invalid', 'ROBOTS_FORBIDDEN', 'error',
       text_list_url, NULL,
       'robots.txt of example.invalid forbids the list page of this source '
       '(rule: Disallow: /rent/) - nothing was fetched. Ask the site owner, '
       'or watch a page the rules allow.',
       'User-agent: *' || chr(10) || 'Disallow: /rent/', 'run-verify'
FROM scraper_config.sources WHERE text_name = '_verify source'
UNION ALL
SELECT NOW() - INTERVAL '1 minute', 'the site answered 503', bigint_id,
       text_name, 'example.invalid', 'HTTP_5XX', 'info', text_list_url, 503,
       'The site has a problem of its own (HTTP 503). Nothing to change '
       'here; try again later.', '<html>maintenance</html>', 'run-verify'
FROM scraper_config.sources WHERE text_name = '_verify source'
UNION ALL
SELECT NOW(), 'a file was too large', bigint_id, text_name, 'example.invalid',
       'FILE_TOO_LARGE', 'warning', text_list_url || '/plan.pdf', NULL,
       'plan.pdf is larger than the 25 MB this source allows, so it was not '
       'sent. Raise the limit for pdf files, or leave it out.',
       'stopped after 26214400 bytes', 'run-verify'
FROM scraper_config.sources WHERE text_name = '_verify source';

INSERT INTO _results (check_name, verdict, detail)
SELECT 'the log reads newest first, with its sentence intact',
       CASE WHEN newest = 'FILE_TOO_LARGE' AND rows = 3
             AND message LIKE '%not%sent%' THEN 'PASS' ELSE 'FAIL' END,
       rows || ' row(s), newest is ' || coalesce(newest, 'none')
FROM (
    SELECT (SELECT text_kind FROM monitoring.scraper_errors
             WHERE text_run_id = 'run-verify' ORDER BY date_added DESC LIMIT 1) AS newest,
           (SELECT text_message FROM monitoring.scraper_errors
             WHERE text_run_id = 'run-verify' ORDER BY date_added DESC LIMIT 1) AS message,
           (SELECT count(*) FROM monitoring.scraper_errors
             WHERE text_run_id = 'run-verify') AS rows
) x;

-- One source, one host, one rule: the question "why is nothing coming from
-- this domain?" is answered from this table alone, without a join.
INSERT INTO _results (check_name, verdict, detail)
SELECT 'a rejection names its source, host and rule without a join',
       CASE WHEN count(*) = 1 THEN 'PASS' ELSE 'FAIL' END,
       coalesce(min(text_source_name || ' / ' || text_host), 'no row')
FROM monitoring.scraper_errors
WHERE text_kind = 'ROBOTS_FORBIDDEN' AND text_run_id = 'run-verify'
  AND text_source_name <> '' AND text_host <> '' AND text_detail LIKE '%Disallow%';

-- Deleting a source takes its patterns, addresses, rules and snapshots with
-- it, and leaves the crawler's own state alone - that is the crawler's to
-- clean up on its next sync.
INSERT INTO scraper.targets (bigint_fk_source)
SELECT bigint_id FROM scraper_config.sources WHERE text_name = '_verify source';

DELETE FROM scraper_config.sources WHERE text_name = '_verify source';

INSERT INTO _results (check_name, verdict, detail)
SELECT 'deleting a source cascades to its configuration only',
       CASE WHEN cfg = 0 AND state = 1 THEN 'PASS' ELSE 'FAIL' END,
       cfg || ' configuration rows left, ' || state || ' target row kept'
FROM (
    SELECT (SELECT count(*) FROM scraper_config.source_patterns p
            WHERE NOT EXISTS (SELECT 1 FROM scraper_config.sources s WHERE s.bigint_id = p.bigint_fk_source))
         + (SELECT count(*) FROM scraper_config.test_snapshots t
            WHERE t.json_result->>'which' IN ('older', 'newest')) AS cfg,
           (SELECT count(*) FROM scraper.targets t
            WHERE NOT EXISTS (SELECT 1 FROM scraper_config.sources s WHERE s.bigint_id = t.bigint_fk_source)) AS state
) x;

-- ── 4. Roles, when 04-roles.sh was applied ──────────────────
-- Application roles are any login roles that are neither superuser nor the
-- owner of the database. Without any, the boundary is agreed rather than
-- enforced, and that is a PASS with a note - the script is opt-in.
CREATE TEMP TABLE _app_roles AS
SELECT r.rolname
FROM pg_roles r
WHERE r.rolcanlogin AND NOT r.rolsuper AND r.rolname NOT LIKE 'pg\_%'
  AND r.oid <> (SELECT datdba FROM pg_database WHERE datname = current_database());

INSERT INTO _results (check_name, verdict, detail)
SELECT 'no application role can write processed_data',
       CASE WHEN count(*) FILTER (WHERE can_write) = 0 THEN 'PASS' ELSE 'FAIL' END,
       CASE WHEN count(*) = 0 THEN 'no application roles (04-roles.sh not applied)'
            ELSE count(*) FILTER (WHERE NOT can_write) || '/' || count(*) || ' roles read-only: '
                 || string_agg(rolname, ', ') END
FROM (SELECT rolname,
             has_table_privilege(rolname, 'processed_data.tasks', 'INSERT, UPDATE, DELETE')
             OR has_schema_privilege(rolname, 'processed_data', 'CREATE') AS can_write
      FROM _app_roles) x;

INSERT INTO _results (check_name, verdict, detail)
SELECT 'writers of scraper_config cannot write crawler, and vice versa',
       CASE WHEN count(*) FILTER (WHERE both_sides) = 0 THEN 'PASS' ELSE 'FAIL' END,
       CASE WHEN count(*) = 0 THEN 'no application roles (04-roles.sh not applied)'
            ELSE count(*) FILTER (WHERE NOT both_sides) || '/' || count(*) || ' roles on one side' END
FROM (SELECT rolname,
             has_table_privilege(rolname, 'scraper_config.sources', 'INSERT')
             AND has_table_privilege(rolname, 'scraper.targets', 'INSERT') AS both_sides
      FROM _app_roles) x;

-- The check above asks about INSERT, and DELETE is also a write. There is
-- exactly one deliberate exception to the boundary and this is where it is
-- written down: the dashboard may DELETE scraper.projects, because removing a
-- project nobody uses is the customer's act and there is nowhere else
-- to do it. Everything else in the crawler's schema stays the crawler's -
-- including scraper.api_keys, which is a record of what the keys actually
-- answered and stops being that the moment something else can edit it.
INSERT INTO _results (check_name, verdict, detail)
SELECT 'the dashboard reaches into crawler for one deletion and no more',
       CASE WHEN count(*) FILTER (WHERE too_much) = 0 THEN 'PASS' ELSE 'FAIL' END,
       CASE WHEN count(*) = 0 THEN 'no application roles (04-roles.sh not applied)'
            ELSE count(*) FILTER (WHERE deletes_projects)
                 || ' role(s) may delete a project, '
                 || count(*) FILTER (WHERE too_much) || ' reach further' END
FROM (SELECT rolname,
             has_table_privilege(rolname, 'scraper.projects', 'DELETE') AS deletes_projects,
             -- Anything beyond that one DELETE: writing a project, touching the
             -- key registry, or writing any of the crawler's runtime state.
             has_table_privilege(rolname, 'scraper.projects', 'INSERT, UPDATE')
             OR has_table_privilege(rolname, 'scraper.api_keys', 'INSERT, UPDATE, DELETE')
             OR has_table_privilege(rolname, 'scraper.targets', 'INSERT, UPDATE, DELETE')
             OR has_table_privilege(rolname, 'scraper.submit_queue', 'INSERT, UPDATE, DELETE')
                AS too_much
      FROM _app_roles
     WHERE has_table_privilege(rolname, 'scraper_config.sources', 'INSERT')) x;

-- The crawler writes its runs and nothing else in monitoring: the
-- collector's run table is the collector's.
INSERT INTO _results (check_name, verdict, detail)
SELECT 'monitoring is written through scraper_runs only',
       CASE WHEN count(*) FILTER (WHERE writes_collector) = 0 THEN 'PASS' ELSE 'FAIL' END,
       CASE WHEN count(*) = 0 THEN 'no application roles (04-roles.sh not applied)'
            ELSE count(*) FILTER (WHERE writes_runs) || '/' || count(*) || ' roles insert scraper_runs, '
                 || count(*) FILTER (WHERE writes_collector) || ' touch collector_runs' END
FROM (SELECT rolname,
             has_table_privilege(rolname, 'monitoring.scraper_runs', 'INSERT') AS writes_runs,
             has_table_privilege(rolname, 'monitoring.collector_runs', 'INSERT, UPDATE, DELETE') AS writes_collector
      FROM _app_roles) x;

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
