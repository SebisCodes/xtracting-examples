-- ============================================================
-- Verification: does this database actually do what the schema claims?
--
--     docker compose exec -T db \
--       psql -U xtracting -d xtracting_archive -f /verify.sql
--
-- Writes into a throwaway project, checks every claim, then removes what it
-- wrote. Safe to run against an archive that already holds real data: nothing
-- it touches carries the project name of anything else.
--
-- Every check prints PASS or FAIL. A check that cannot be answered prints FAIL,
-- not nothing - a verification that goes quiet when it breaks is worse than no
-- verification at all.
-- ============================================================

\set ON_ERROR_STOP on
\set QUIET on
\pset footer off

BEGIN;

CREATE TEMP TABLE _results (seq SERIAL, check_name TEXT, verdict TEXT, detail TEXT);

-- ── 1. Structure ────────────────────────────────────────────
-- Vocabularies are plain tables on purpose, so this counts the data tables.
-- Only the ones 01-schema.sql creates: init/03-scraper.sql adds
-- monitoring.scraper_runs and scraper.documents on top, and those have their
-- own suite, verify-scraper.sql.
INSERT INTO _results (check_name, verdict, detail)
SELECT 'data tables are hypertables',
       CASE WHEN count(*) = 12 THEN 'PASS' ELSE 'FAIL' END,
       count(*) || ' hypertables'
FROM timescaledb_information.hypertables
WHERE hypertable_schema = 'processed_data'
   OR (hypertable_schema, hypertable_name) = ('monitoring', 'collector_runs');

-- OPERATIONAL TABLES ARE NOT ARCHIVE TABLES. `monitoring` holds two kinds of
-- thing. The run tables are archive rows in every sense - one per project, per
-- language, keyed like the rest - and every claim below is about them.
-- `monitoring.heartbeat` and `monitoring.service_log` are not: a heartbeat has
-- no project and a step log has no language, they are read while they are
-- minutes old, and the step log keeps its rows for a day rather than for ever.
-- They are excluded by name from the four claims that would otherwise be
-- asserting something untrue about them.
INSERT INTO _results (check_name, verdict, detail)
SELECT 'every table carries project, language and task id',
       CASE WHEN count(*) FILTER (WHERE cols < 3) = 0 THEN 'PASS' ELSE 'FAIL' END,
       count(*) FILTER (WHERE cols = 3) || '/' || count(*) || ' tables'
FROM (SELECT t.table_name,
             count(*) FILTER (WHERE c.column_name IN
                   ('text_project', 'text_language', 'text_task_id')) AS cols
      FROM information_schema.tables t
      JOIN information_schema.columns c
        ON c.table_name = t.table_name AND c.table_schema = t.table_schema
      WHERE t.table_schema IN ('processed_data', 'monitoring')
        AND t.table_type = 'BASE TABLE'
        AND (t.table_schema, t.table_name) NOT IN (
              ('monitoring', 'heartbeat'), ('monitoring', 'service_log'))
      GROUP BY t.table_name) x;

INSERT INTO _results (check_name, verdict, detail)
SELECT 'all three columns indexed on every table',
       CASE WHEN count(*) FILTER (WHERE idx < 3) = 0 THEN 'PASS' ELSE 'FAIL' END,
       count(*) FILTER (WHERE idx >= 3) || '/' || count(*) || ' tables'
FROM (SELECT t.table_name,
             count(*) FILTER (WHERE i.indexname ~ '_(project|language|task)$') AS idx
      FROM information_schema.tables t
      LEFT JOIN pg_indexes i
        ON i.tablename = t.table_name AND i.schemaname = t.table_schema
      WHERE t.table_schema IN ('processed_data', 'monitoring')
        AND t.table_type = 'BASE TABLE'
        AND (t.table_schema, t.table_name) NOT IN (
              ('monitoring', 'heartbeat'), ('monitoring', 'service_log'))
      GROUP BY t.table_name) y;

-- Name + project is unique, so the same vocabulary in two projects is two rows.
INSERT INTO _results (check_name, verdict, detail)
SELECT 'vocabularies enforce name + project uniqueness',
       CASE WHEN count(*) = 16 THEN 'PASS' ELSE 'FAIL' END,
       count(*) || '/16 vocabulary tables'
FROM pg_indexes
WHERE schemaname = 'processed_data'
  AND indexdef LIKE 'CREATE UNIQUE%'
  AND indexdef LIKE '%(text_name, text_project, text_language)%'
  AND indexdef NOT LIKE '%date_added%';

INSERT INTO _results (check_name, verdict, detail)
SELECT 'chunk interval is 1 day',
       CASE WHEN count(*) = 0 THEN 'PASS' ELSE 'FAIL' END,
       CASE WHEN count(*) = 0 THEN 'all tables' ELSE count(*) || ' tables differ' END
FROM timescaledb_information.dimensions
WHERE hypertable_schema IN ('processed_data', 'monitoring')
  AND (hypertable_schema, hypertable_name) <> ('monitoring', 'service_log')
  AND time_interval IS DISTINCT FROM INTERVAL '1 day';

INSERT INTO _results (check_name, verdict, detail)
SELECT 'compression enabled everywhere',
       CASE WHEN count(*) FILTER (WHERE NOT compression_enabled) = 0 THEN 'PASS' ELSE 'FAIL' END,
       count(*) FILTER (WHERE compression_enabled) || '/' || count(*) || ' hypertables'
FROM timescaledb_information.hypertables
WHERE hypertable_schema IN ('processed_data', 'monitoring')
  AND (hypertable_schema, hypertable_name) <> ('monitoring', 'service_log');

-- THIRTY DAYS, NOT ONE. The dashboard reads the whole archive rather than
-- its newest day, so a one-day policy meant every filtered scan paid to
-- decompress what it walked over; 01-schema.sql says what that measured. The
-- number here follows that file, and this check exists so the two cannot
-- drift apart.
--
-- The archive's own tables only. Two tables of the crawler wait longer by
-- design and are asserted in verify-scraper.sql instead: scraper.documents
-- (two days - its rows are updated after they are written) and
-- monitoring.scraper_errors (seven days - a log is read while it is fresh,
-- and it is the one table here that is also dropped again after ninety days).
INSERT INTO _results (check_name, verdict, detail)
SELECT 'compression policy after 30 days',
       CASE WHEN count(*) > 0 AND count(*) FILTER (WHERE config->>'compress_after' <> '30 days') = 0
            THEN 'PASS' ELSE 'FAIL' END,
       count(*) || ' policies'
FROM timescaledb_information.jobs
WHERE proc_name = 'policy_compression'
  AND hypertable_schema IN ('processed_data', 'monitoring')
  AND (hypertable_schema, hypertable_name) NOT IN (
        ('monitoring', 'crawler_errors'), ('monitoring', 'service_log'));

-- project and language must be ordinary indexes, never partitioning dimensions.
INSERT INTO _results (check_name, verdict, detail)
SELECT 'project/language are NOT hypertable dimensions',
       CASE WHEN count(*) = 0 THEN 'PASS' ELSE 'FAIL' END,
       CASE WHEN count(*) = 0 THEN 'time only' ELSE count(*) || ' found' END
FROM timescaledb_information.dimensions
WHERE column_name IN ('text_project', 'text_language');

INSERT INTO _results (check_name, verdict, detail)
SELECT 'project/language B-tree indexes present',
       CASE WHEN count(*) >= 50 THEN 'PASS' ELSE 'FAIL' END,
       count(*) || ' indexes'
FROM pg_indexes
WHERE schemaname IN ('processed_data', 'monitoring')
  AND (indexname LIKE 'idx_%_project' OR indexname LIKE 'idx_%_language');

INSERT INTO _results (check_name, verdict, detail)
SELECT 'no table for raw task text',
       CASE WHEN count(*) = 0 THEN 'PASS' ELSE 'FAIL' END,
       CASE WHEN count(*) = 0 THEN 'none' ELSE string_agg(table_name, ', ') END
FROM information_schema.columns
WHERE table_schema = 'processed_data'
  AND column_name IN ('text_content', 'text_input', 'text_raw');

-- ── 2. Vocabularies ─────────────────────────────────────────
INSERT INTO _results (check_name, verdict, detail)
SELECT 'outlook vocabulary seeded',
       CASE WHEN count(*) = 4 THEN 'PASS' ELSE 'FAIL' END, count(*) || '/4'
FROM processed_data.outlook_types
WHERE text_name IN ('Rising', 'Declining', 'Neutral', 'Unset');

INSERT INTO _results (check_name, verdict, detail)
SELECT 'sentiment vocabulary seeded',
       CASE WHEN count(*) = 8 THEN 'PASS' ELSE 'FAIL' END, count(*) || '/8'
FROM processed_data.sentiment_types;

INSERT INTO _results (check_name, verdict, detail)
SELECT 'rating scale ordered by value',
       CASE WHEN min(float_value) = -3 AND max(float_value) = 3 THEN 'PASS' ELSE 'FAIL' END,
       min(float_value) || ' .. ' || max(float_value)
FROM processed_data.rating_values;

-- ── 3. Writing, reading, and the language split ─────────────
INSERT INTO processed_data.tasks
    (text_name, text_project, text_language, text_job_id, text_task_id,
     integer_task_index, text_status, text_source_uri, date_commissioned, date_evaluated)
VALUES
    ('_verify:task:0', '_verify', 'English', 'job_v', 'task_v', 0, 'COMPLETED',
     'https://example.invalid/doc', NOW() - INTERVAL '2 hours', NOW() - INTERVAL '1 hour'),
    ('_verify:task:0', '_verify', 'German',  'job_v', 'task_v', 0, 'COMPLETED',
     'https://example.invalid/doc', NOW() - INTERVAL '2 hours', NOW() - INTERVAL '1 hour');

INSERT INTO processed_data.entities
    (text_name, text_project, text_language, text_entity_id, text_task_id, text_type)
VALUES
    ('Nordwyk NW-180', '_verify', 'English', 'ent:nordwyk', 'task_v', 'Machine'),
    ('Nordwyk NW-180', '_verify', 'German',  'ent:nordwyk', 'task_v', 'Maschine');

INSERT INTO processed_data.attributes
    (text_name, text_project, text_language, text_fk_entity_id, text_task_id,
     text_type, text_unit, text_value, float_value)
VALUES
    ('flow_rate', '_verify', 'English', 'ent:nordwyk', 'task_v', 'Throughput', 'm³/h', '180', 180),
    ('Fördermenge', '_verify', 'German', 'ent:nordwyk', 'task_v', 'Durchsatz', 'm³/h', '180', 180);

INSERT INTO processed_data.market_insights
    (text_name, text_project, text_language, text_fk_entity_id, text_task_id,
     text_topic, text_long_term_outlook, text_short_term_outlook,
     text_long_term_sentiment, text_short_term_sentiment, bool_high_relevance)
VALUES
    ('_verify:insight', '_verify', 'English', 'ent:nordwyk', 'task_v',
     'Industrial equipment', 'Rising', 'Neutral', 'Positive', 'Slightly Positive', true);

INSERT INTO _results (check_name, verdict, detail)
SELECT 'same task stored once per language',
       CASE WHEN count(DISTINCT text_language) = 2 AND count(*) = 2 THEN 'PASS' ELSE 'FAIL' END,
       count(*) || ' rows, ' || count(DISTINCT text_language) || ' languages'
FROM processed_data.tasks WHERE text_project = '_verify';

INSERT INTO _results (check_name, verdict, detail)
SELECT 'language filter isolates a version',
       CASE WHEN count(*) = 1 AND min(text_name) = 'Fördermenge' THEN 'PASS' ELSE 'FAIL' END,
       coalesce(min(text_name), '(nothing)')
FROM processed_data.attributes
WHERE text_project = '_verify' AND text_language = 'German';

INSERT INTO _results (check_name, verdict, detail)
SELECT 'project filter isolates an archive',
       CASE WHEN count(*) = 0 THEN 'PASS' ELSE 'FAIL' END,
       count(*) || ' foreign rows'
FROM processed_data.attributes
WHERE text_project <> '_verify' AND text_task_id = 'task_v';

-- An event, one linked to the entity and one linked to nothing. The second is
-- the ordinary case and has to stay a first-class row: it happened, it has a
-- date, and it is about the document rather than about anything in it.
INSERT INTO processed_data.events
    (text_name, text_project, text_language, text_task_id, text_type, date_eventdate)
VALUES
    ('Commissioning', '_verify', 'English', 'task_v', 'Milestone', '2019-03-14'),
    ('Publication',   '_verify', 'English', 'task_v', 'Publication', '2026-09-20');

INSERT INTO processed_data.event_entities
    (text_name, text_project, text_language, text_task_id,
     bigint_fk_event_id, text_fk_entity_id)
SELECT 'Commissioning->ent:nordwyk', '_verify', 'English', 'task_v',
       bigint_id, 'ent:nordwyk'
FROM processed_data.events
WHERE text_project = '_verify' AND text_name = 'Commissioning';

INSERT INTO _results (check_name, verdict, detail)
SELECT 'event links to its entity in both directions',
       CASE WHEN count(*) = 1 THEN 'PASS' ELSE 'FAIL' END, count(*) || ' row(s)'
FROM processed_data.events ev
JOIN processed_data.event_entities le
  ON le.bigint_fk_event_id = ev.bigint_id
 AND le.text_project = ev.text_project AND le.text_language = ev.text_language
JOIN processed_data.entities e
  ON e.text_entity_id = le.text_fk_entity_id
 AND e.text_project = le.text_project AND e.text_language = le.text_language
WHERE ev.text_project = '_verify' AND ev.text_language = 'English';

INSERT INTO _results (check_name, verdict, detail)
SELECT 'an event that names nothing is still stored',
       CASE WHEN count(*) = 1 THEN 'PASS' ELSE 'FAIL' END, count(*) || ' row(s)'
FROM processed_data.events ev
LEFT JOIN processed_data.event_entities le ON le.bigint_fk_event_id = ev.bigint_id
WHERE ev.text_project = '_verify' AND ev.text_name = 'Publication'
  AND le.bigint_id IS NULL;

-- The join a dashboard actually makes: entity -> its values -> its insight.
INSERT INTO _results (check_name, verdict, detail)
SELECT 'entity joins to attributes and insights',
       CASE WHEN count(*) = 1 THEN 'PASS' ELSE 'FAIL' END, count(*) || ' row(s)'
FROM processed_data.entities e
JOIN processed_data.attributes a
  ON a.text_fk_entity_id = e.text_entity_id
 AND a.text_project = e.text_project AND a.text_language = e.text_language
JOIN processed_data.market_insights m
  ON m.text_fk_entity_id = e.text_entity_id
 AND m.text_project = e.text_project AND m.text_language = e.text_language
WHERE e.text_project = '_verify' AND e.text_language = 'English';

INSERT INTO _results (check_name, verdict, detail)
SELECT 'ordering by rating scale works',
       CASE WHEN count(*) = 7 AND (array_agg(text_name ORDER BY float_value))[1] = 'Egregious'
            THEN 'PASS' ELSE 'FAIL' END,
       (array_agg(text_name ORDER BY float_value))[1] || ' .. ' ||
       (array_agg(text_name ORDER BY float_value DESC))[1]
FROM processed_data.rating_values;

INSERT INTO _results (check_name, verdict, detail)
SELECT 'commissioned/evaluated timestamps stored',
       CASE WHEN count(*) = 2 THEN 'PASS' ELSE 'FAIL' END,
       count(*) || ' rows with both'
FROM processed_data.tasks
WHERE text_project = '_verify'
  AND date_commissioned IS NOT NULL AND date_evaluated IS NOT NULL;

INSERT INTO processed_data.entity_types (text_name, text_project, text_language)
VALUES ('_verify:type', '_verify', 'English'), ('_verify:type', '_verify-b', 'English')
ON CONFLICT (text_name, text_project, text_language) DO NOTHING;
INSERT INTO processed_data.entity_types (text_name, text_project, text_language)
VALUES ('_verify:type', '_verify', 'English')
ON CONFLICT (text_name, text_project, text_language) DO NOTHING;

INSERT INTO _results (check_name, verdict, detail)
SELECT 'same vocabulary in two projects stays two rows',
       CASE WHEN count(*) = 2 THEN 'PASS' ELSE 'FAIL' END,
       count(*) || ' rows after three inserts'
FROM processed_data.entity_types WHERE text_name = '_verify:type';

-- ── 4. Compression actually runs ────────────────────────────
-- Backdated so the row is older than the 1 day policy, then compressed by hand
-- rather than waiting for the scheduler.
INSERT INTO processed_data.tasks
    (date_added, text_name, text_project, text_language, text_job_id, text_task_id, text_status)
VALUES (NOW() - INTERVAL '3 days', '_verify:old', '_verify', 'English', 'job_old', 'task_old', 'COMPLETED');

DO $$
DECLARE ch REGCLASS;
BEGIN
    FOR ch IN
        SELECT format('%I.%I', chunk_schema, chunk_name)::regclass
        FROM timescaledb_information.chunks
        WHERE hypertable_name = 'tasks' AND range_end < NOW() - INTERVAL '1 day'
          AND NOT is_compressed
    LOOP
        PERFORM compress_chunk(ch);
    END LOOP;
END
$$;

INSERT INTO _results (check_name, verdict, detail)
SELECT 'old chunks compress',
       CASE WHEN count(*) FILTER (WHERE is_compressed) > 0 THEN 'PASS' ELSE 'FAIL' END,
       count(*) FILTER (WHERE is_compressed) || '/' || count(*) || ' chunks compressed'
FROM timescaledb_information.chunks
WHERE hypertable_name = 'tasks';

INSERT INTO _results (check_name, verdict, detail)
SELECT 'compressed rows still readable',
       CASE WHEN count(*) = 1 THEN 'PASS' ELSE 'FAIL' END, count(*) || ' row(s)'
FROM processed_data.tasks
WHERE text_project = '_verify' AND text_name = '_verify:old';

-- Compression segments by the columns analyses filter on. text_name must lead
-- on every hypertable; text_type joins it wherever the table has one. A silent
-- revert to segmenting by project alone would cost a name lookup an order of
-- magnitude, and nothing else in here would notice.
INSERT INTO _results (check_name, verdict, detail)
SELECT 'compression segments by name (and type)',
       CASE WHEN count(*) FILTER (WHERE NOT ok) = 0 THEN 'PASS' ELSE 'FAIL' END,
       count(*) FILTER (WHERE ok) || '/' || count(*) || ' hypertables'
FROM (
    SELECT h.hypertable_name,
           bool_or(cs.attname = 'text_name' AND cs.segmentby_column_index = 1)
           AND (
               NOT EXISTS (SELECT 1 FROM information_schema.columns c
                           WHERE c.table_schema = 'processed_data'
                             AND c.table_name = h.hypertable_name
                             AND c.column_name = 'text_type')
               OR bool_or(cs.attname = 'text_type' AND cs.segmentby_column_index IS NOT NULL)
           ) AS ok
    FROM timescaledb_information.hypertables h
    JOIN timescaledb_information.compression_settings cs
      ON cs.hypertable_schema = h.hypertable_schema
     AND cs.hypertable_name = h.hypertable_name
    WHERE h.hypertable_schema = 'processed_data'
    GROUP BY h.hypertable_name
) x;

-- Every hypertable carries its own compression policy, so the number of
-- background jobs grows with the schema. PostgreSQL's default of eight worker
-- processes is below what TimescaleDB is configured to start, and policies then
-- fail with "failed to start job" - they are retried and the data does get
-- compressed, but the failures recur and multiply with load.
--
-- The configuration is asserted rather than the failure count: how many jobs
-- fail depends on what else the machine is doing at that second, so a count
-- makes this suite report FAIL for something harmless.
INSERT INTO _results (check_name, verdict, detail)
SELECT 'enough background workers for the policies',
       CASE WHEN have >= needed THEN 'PASS' ELSE 'FAIL' END,
       have || ' worker processes, ' || needed || ' needed'
FROM (
    SELECT current_setting('max_worker_processes')::int AS have,
           1 + current_setting('timescaledb.max_background_workers')::int
             + current_setting('max_parallel_workers')::int AS needed
) x;

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
