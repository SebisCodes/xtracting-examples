-- ============================================================
-- 05-policies.sql - HOW LONG EVERYTHING IS KEPT, in one file
--
-- WHY THIS IS ITS OWN FILE, AND WHY IT IS THE LAST ONE.
--
-- Not in 01-schema.sql for the archive's own tables and in 03-scraper.sql
-- for the crawler's: two files with the same kind of number in them is how
-- two files come to disagree.
--
-- It is also the only way an EXISTING archive can be changed. 01-schema.sql
-- is an init-only file: it uses plain CREATE TABLE for the vocabularies, so
-- applying it to a grown archive stops at the first one that already exists
-- and everything after it - including its policies - never runs. This file
-- creates nothing. It may be applied to any archive, at any time, as often as
-- you like:
--
--   docker compose exec -T xtracting_db \
--     psql -U xtracting -d xtracting_archive -f /docker-entrypoint-initdb.d/05-policies.sql
--
-- ============================================================
-- THE KNOBS. These are the numbers. Change one, run this file, done.
-- ============================================================
--
--   processed_data.*            compress after 30 days, kept for ever
--   scraper.documents           compress after 30 days, kept for ever
--   monitoring.scraper_runs     compress after 30 days, kept for ever
--   monitoring.collector_runs   compress after 30 days, kept for ever
--   monitoring.scraper_errors   compress after 30 days, DROPPED after 90 days
--   monitoring.service_log      never compressed,       DROPPED after 24 hours
--
-- Thirty days rather than one for everything the dashboard reads: the
-- dashboard reads the whole archive, not its newest day, so a one-day policy
-- made every filtered scan pay to decompress what it walked over. Measured on
-- a real archive, one place search planned over 550 chunks and spent eight
-- seconds in them.
--
-- Ninety days for the problem log: "why did this source stop collecting in
-- March" is a question people really ask. Twenty-four hours for the step log:
-- it is written a row per step and is only of use while somebody is watching,
-- and it is the one number here that governs how much disk the debug switches
-- can cost.
--
-- Only those two tables throw anything away. Nothing else in this archive
-- forgets.

-- ============================================================
-- SETTING A POLICY, NOT MERELY ADDING ONE
--
-- `add_compression_policy(..., if_not_exists => TRUE)` finds an existing
-- policy and returns. It does NOT look at the interval. This file promised
-- the opposite for a long time - "run it again with new numbers and the
-- policy is replaced" - and the promise was false in the one direction that
-- matters: an archive kept the numbers it was BORN with, however often this
-- file was applied, while everybody reading the file believed otherwise.
--
-- It is easy to measure: a file saying thirty days for
-- monitoring.scraper_errors while the archive says seven after two
-- applications, and `verify.sql` failing on exactly that.
--
-- So: look at what is there, drop it if the interval differs, then add. The
-- two functions below are the only way this file sets a policy.
-- ============================================================
CREATE OR REPLACE FUNCTION pg_temp.set_compression(tbl REGCLASS, after INTERVAL)
RETURNS VOID LANGUAGE plpgsql AS $fn$
DECLARE existing INTERVAL;
BEGIN
    SELECT (j.config ->> 'compress_after')::INTERVAL INTO existing
      FROM timescaledb_information.jobs j
     WHERE j.proc_name = 'policy_compression'
       AND format('%I.%I', j.hypertable_schema, j.hypertable_name)::REGCLASS = tbl;

    IF existing IS NOT NULL AND existing <> after THEN
        PERFORM remove_compression_policy(tbl);
        RAISE NOTICE '% : compression % -> %', tbl, existing, after;
        existing := NULL;
    END IF;
    IF existing IS NULL THEN
        PERFORM add_compression_policy(tbl, after, if_not_exists => TRUE);
    END IF;
END
$fn$;

CREATE OR REPLACE FUNCTION pg_temp.set_retention(tbl REGCLASS, after INTERVAL)
RETURNS VOID LANGUAGE plpgsql AS $fn$
DECLARE existing INTERVAL;
BEGIN
    SELECT (j.config ->> 'drop_after')::INTERVAL INTO existing
      FROM timescaledb_information.jobs j
     WHERE j.proc_name = 'policy_retention'
       AND format('%I.%I', j.hypertable_schema, j.hypertable_name)::REGCLASS = tbl;

    IF existing IS NOT NULL AND existing <> after THEN
        PERFORM remove_retention_policy(tbl);
        RAISE NOTICE '% : retention % -> %', tbl, existing, after;
        existing := NULL;
    END IF;
    IF existing IS NULL THEN
        PERFORM add_retention_policy(tbl, after, if_not_exists => TRUE);
    END IF;
END
$fn$;

-- ── The archive's own tables ─────────────────────────────────────────────
DO $$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'sources', 'entities', 'locations', 'events', 'ratings', 'connections',
        'attributes', 'market_insights', 'source_importances_by_perspective',
        'event_entities', 'tasks'
    ] LOOP
        IF to_regclass(format('processed_data.%I', t)) IS NOT NULL THEN
            EXECUTE format(
                $f$SELECT pg_temp.set_compression('processed_data.%I', INTERVAL '30 days')$f$, t);
        END IF;
    END LOOP;
END
$$;

-- ── What the crawler and the collector leave behind ──────────────────────
SELECT pg_temp.set_compression('monitoring.collector_runs', INTERVAL '30 days');

DO $$
BEGIN
    IF to_regclass('scraper.documents') IS NOT NULL THEN
        PERFORM pg_temp.set_compression('scraper.documents', INTERVAL '30 days');
        PERFORM pg_temp.set_compression('monitoring.scraper_runs', INTERVAL '30 days');
        PERFORM pg_temp.set_compression('monitoring.scraper_errors', INTERVAL '30 days');
        PERFORM pg_temp.set_retention('monitoring.scraper_errors', INTERVAL '90 days');
        PERFORM pg_temp.set_retention('monitoring.service_log', INTERVAL '24 hours');
    END IF;
END
$$;

\echo '05-policies.sql: every retention and compression policy is set'
