-- ============================================================
-- 06-indexes.sql - INDEXES ADDED AFTER THE ARCHIVE WAS FIRST DESIGNED
--
-- WHY THIS IS ITS OWN FILE, AND WHY IT COMES AFTER THE POLICIES.
--
-- 01-schema.sql creates every index the archive was born with, and it is an
-- init-only file: it uses plain CREATE TABLE for the vocabularies, so
-- applying it to a grown archive stops at the first one that already exists
-- and everything after it - including its indexes - never runs. An index
-- that a later feature needs therefore has to live somewhere that may be
-- re-applied, and 05-policies.sql is the file that proved the shape:
-- idempotent, creating nothing that could collide, safe to run as often as
-- you like.
--
-- It is not, however, the file this belongs in. 05-policies.sql is about ONE
-- thing - how long everything is kept - and it says so four times over; an
-- index in it would be the second kind of number in a file whose whole point
-- is that there is only one kind. So: same property, different subject, its
-- own file, and it runs last because an index needs the tables that 01 and
-- 03 create.
--
-- Every statement here is CREATE INDEX IF NOT EXISTS. Run it against any
-- archive, at any time, as often as you like:
--
--   docker compose exec -T xtracting_db \
--     psql -U xtracting -d xtracting_archive -f /docker-entrypoint-initdb.d/06-indexes.sql
--
-- An index added here should ALSO be added to 01-schema.sql's index section,
-- so a fresh archive is born with it and does not depend on somebody
-- remembering to run this file afterwards. The two are meant to say the same
-- thing; this one exists only because the other cannot be re-applied.
-- ============================================================


-- ── The reader's perspective, on every view that has one ────────────────
--
-- The dashboard's top bar can narrow Query, Events, Diagrams and the Graph
-- to "only what came from documents that are at least this important for
-- this reader" (app/sqlbuild.py: perspective_scope). The predicate is an
-- EXISTS into source_importances_by_perspective on
-- (text_project, text_language, text_task_id, text_fk_source_id,
--  text_perspective) - five equality tests, in that order - and the table
-- had an index on none of the last three.
--
-- WHAT THIS INDEX IS MEASURED TO BE WORTH, WHICH IS LESS THAN IT LOOKS.
--
-- On a large archive (700,000 documents, 368 chunks, 625 MB) the heavy
-- query took 1,742 and 1,724 ms without it and 1,728 and 1,714 ms with it.
-- That is noise. It is not a mystery and it is worth writing down, because
-- the next person to look at a slow query on this table will otherwise add
-- the same index again:
--
--   364 of those 368 chunks are compressed, and a B-tree index on a
--   hypertable covers the UNCOMPRESSED rows only. The compressed ones are
--   read by a columnar scan that can skip whole segments - but only on the
--   compress_segmentby columns, and this table's segmentby leads with
--   text_name, which is UNIQUE PER ROW. Measured on one chunk: 2,985 rows
--   in 2,982 segments. One row per segment means nothing to skip and
--   nothing to compress, so every query walks the lot.
--
-- The real fix for a grown archive is therefore a segmentby that groups
-- something - text_perspective would put five hundred rows in a segment
-- instead of one - and that means decompressing and recompressing every
-- chunk, which is a migration and not an index. It is written here so it is
-- not rediscovered from scratch.
--
-- The index is still created, and not out of superstition: everything inside
-- the 30-day compression window is uncompressed, which is the whole archive
-- while it is being collected and the head of it for ever after. That is
-- where new rows land and where this index does its work. It costs about
-- 3 MB per 700,000 judgements.
CREATE INDEX IF NOT EXISTS idx_source_importances_lookup
    ON processed_data.source_importances_by_perspective
       (text_project, text_language, text_task_id, text_fk_source_id, text_perspective);


\echo '06-indexes.sql: every index added after the first design is present'
