-- ============================================================
-- Xtracting archive - crawler and dashboard-configuration schemas
--
-- THE SCHEMAS ARE CALLED `scraper` AND `scraper_config`, AND THIS FILE IS
-- 03-scraper.sql, while the service is called the crawler - which is what it
-- is: it follows links and fetches pages. A schema name is not a name, it is
-- a contract: four services read these schemas, and every archive in use
-- holds its watchlists, its queue and its documents under them. Renaming
-- them would be a migration applied to every installation in lockstep with
-- every service, and it would buy nothing anybody can see.
--
-- Runs AFTER 01-schema.sql and 02-vocabularies.sql, and changes nothing in
-- them: the archive schema stays a copy you can compare against the original.
--
-- APPLIES TO AN EXISTING ARCHIVE TOO. Everything in this file is written with
-- IF NOT EXISTS, if_not_exists => TRUE, or a guard block, so it can be run any
-- number of times, against an empty volume by the container entrypoint or
-- against an archive that already holds years of data by hand:
--
--     docker compose exec -T xtracting_db \
--       psql -U xtracting -d xtracting_archive -v ON_ERROR_STOP=1 < init/03-scraper.sql
--
-- A second run is a no-op. It never drops, never alters a column that exists,
-- and never touches a row.
--
-- WHO WRITES WHERE. Two schemas, one boundary:
--
--   scraper_config   what a person configures in the dashboard: which list
--                    pages to watch, which links count, which files to send.
--                    The dashboard writes it; the crawler only reads it. The
--                    test snapshots the dashboard's own test runner produces
--                    live here as well, because they are configuration in the
--                    making - a draft and what the site answered to it.
--   crawler          what the crawler does with that: schedule, leases, what
--                    it has seen, what it fetched, what it submitted. The
--                    crawler writes it; the dashboard only reads it.
--
-- processed_data stays read-only for both, and monitoring gains two tables:
-- scraper_runs, in the shape of collector_runs, and crawler_errors, one row
-- per thing that went wrong. A run row says that a run failed; an error row
-- says what failed, on which host, and what to do about it - that is the
-- difference between a log a customer can act on and a status word. With
-- that split, a single privilege query answers "can the dashboard reach the
-- raw data?" - and the answer stays no. 04-roles.sh turns the boundary into
-- database roles when you want it enforced rather than agreed.
--
-- HOW LONG THE LOGS ARE KEPT. Two tables here throw rows away:
-- monitoring.scraper_errors after 90 days (a quarter is longer than any
-- support question about "why did this stop in March") and
-- monitoring.service_log after 24 hours (it is written a row per step and is
-- only of use while somebody is watching). Both numbers, and every
-- compression number in the archive, are set in init/05-policies.sql - one
-- file, applicable to a grown archive, which this one is not.
--
-- PLAIN OR HYPERTABLE. Same rule as 01-schema.sql, applied the other way
-- round: anything that gets UPDATEd after it is written - a queue status, a
-- lease, a counter, a heartbeat - is a plain table. A hypertable with a
-- compression policy makes every such UPDATE on a closed chunk a
-- decompression, and a hypertable cannot carry a unique index without its
-- partitioning column, which is exactly the index a queue needs. Only the
-- three things that are append-only and grow with time are hypertables:
-- scraper.documents, monitoring.scraper_runs and monitoring.scraper_errors.
--
-- FOREIGN KEYS. Inside scraper_config they are real: a pattern belongs to a
-- source and goes with it. Across the boundary they are not - scraper.targets
-- carries bigint_fk_source without a constraint, because the crawler must be
-- able to keep runtime state for a source the dashboard has just deleted
-- until its own sync notices, and because a hypertable can never be an FK
-- target anyway. Same naming as 01-schema.sql: *_fk_* means "resolved by the
-- application".
-- ============================================================

CREATE SCHEMA IF NOT EXISTS scraper_config;
CREATE SCHEMA IF NOT EXISTS scraper;



-- ------------------------------------------------------------
-- Additive indexes on processed_data.tasks.
--
-- The crawler's second dedupe gate asks "has this address been submitted
-- before, by anyone?" and its reconcile asks "has my tag come back?". Both
-- are lookups on columns 01-schema.sql stores but does not index, and
-- without these two they are a scan over every chunk of the window. Purely
-- additive: no column, no row and no constraint of the archive changes.
-- ------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_tasks_source_uri
    ON processed_data.tasks (text_source_uri, date_added DESC);
CREATE INDEX IF NOT EXISTS idx_tasks_tag
    ON processed_data.tasks (text_tag);


-- ============================================================
-- scraper_config.sources - one row per list page a person watches
--
-- This is the table the dashboard's Sources editor is a form for. Every
-- column is a decision the editor asks about in plain words; the CHECKs are
-- there so the database refuses what the form would refuse, in case
-- something other than the form writes here.
--
-- A SAVED SOURCE IS OFF. bool_enabled defaults to false on purpose: saving
-- a configuration and starting a crawl are two different acts, and the
-- second one is refused while the last test says robots.txt forbids the
-- list page. The editor says so next to the switch.
-- ============================================================
CREATE TABLE IF NOT EXISTS scraper_config.sources (
    bigint_id                    BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    date_added                   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- The crawler's config sync watches max(date_updated) and re-reads
    -- everything when it moves. Keep it moving: every UPDATE sets it.
    date_updated                 TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    text_name                    TEXT NOT NULL UNIQUE
        CONSTRAINT source_name_not_blank CHECK (btrim(text_name) <> ''),
    text_list_url                TEXT NOT NULL
        CONSTRAINT source_list_url_is_http CHECK (text_list_url ~* '^https?://'),
    -- The same address after crawlkit.hashing.canonical_uri(), and its host.
    -- Stored, not recomputed, so the overview can group by host without
    -- parsing and so a later change to the canonical form shows up as a
    -- difference rather than silently re-keying everything.
    text_list_url_canonical      TEXT NOT NULL DEFAULT '',
    text_host                    TEXT NOT NULL DEFAULT '',

    -- WHICH PROJECTS THIS WATCHLIST COLLECTS INTO IS ITS OWN TABLE now:
    -- scraper_config.source_projects, one row per project, each with the key
    -- that evaluates this page for that project. See the comment there.
    --
    -- A label from XTRACTING_API_KEYS (label:key). One key belongs to one
    -- project, so the label binds the source to its archive. An unknown
    -- label is not an error here - the crawler records it on the target
    -- and the overview shows it.
    --
    -- The key that decides is source_projects.text_key_prefix; this label
    -- is the nickname a person recognises in a log line, and the crawler
    -- backfills the prefix from it when a row names no prefix.
    text_key_label               TEXT NOT NULL DEFAULT '',

    -- What counts as a document on the list page:
    --   selected             only links matching an accept pattern
    --   all_except_rejected  every link except reject patterns, legal
    --                        pages, assets and pagination
    --   exact                the monitored addresses themselves, nothing
    --                        found on them
    text_mode                    TEXT NOT NULL DEFAULT 'selected'
        CONSTRAINT source_mode_known CHECK (
            text_mode IN ('selected', 'all_except_rejected', 'exact')),
    -- Also collect files (pdf, docx, xlsx, csv, txt) found on each page.
    bool_sub_sub                 BOOLEAN NOT NULL DEFAULT false,
    -- What is sent and hashed: boilerplate-stripped text, or cleaned HTML.
    -- HTML costs three to ten times the characters; the test shows the
    -- sampled count before this is saved.
    text_format                  TEXT NOT NULL DEFAULT 'plain'
        CONSTRAINT source_format_known CHECK (text_format IN ('plain', 'html')),
    -- http is the default and enough for most sites. playwright renders
    -- the page in a browser first - for lists that JavaScript builds.
    text_engine                  TEXT NOT NULL DEFAULT 'http'
        CONSTRAINT source_engine_known CHECK (text_engine IN ('http', 'playwright')),
    text_wait_for_selector       TEXT NOT NULL DEFAULT '',
    integer_wait_after_load_ms   INTEGER NOT NULL DEFAULT 0
        CONSTRAINT source_wait_sane CHECK (integer_wait_after_load_ms BETWEEN 0 AND 60000),

    bool_enabled                 BOOLEAN NOT NULL DEFAULT false,

    -- Ignoring robots.txt is a legal decision, not a technical one, and it
    -- has to stand there with its reason. A CHECK rather than a form rule,
    -- so nothing that writes this table can skip it.
    bool_respect_robots          BOOLEAN NOT NULL DEFAULT true,
    text_override_reason         TEXT NOT NULL DEFAULT '',
    CONSTRAINT source_override_needs_reason CHECK (
        bool_respect_robots OR btrim(text_override_reason) <> ''),

    bool_same_host_only          BOOLEAN NOT NULL DEFAULT true,
    -- Implicit reject list: Impressum, AGB, privacy, terms, cookie policy
    -- and their translations (crawlkit.legal). On by default because a
    -- legal footer is on every page and interesting on none.
    bool_drop_legal_links        BOOLEAN NOT NULL DEFAULT true,

    -- Pagination. text_paging_param is learned from the links the person
    -- ticked (?ep=2, ?page=2, ?currentPage=2); text_next_selector is the
    -- fallback when the site paginates with a "next" link instead.
    bool_follow_pagination       BOOLEAN NOT NULL DEFAULT true,
    text_paging_param            TEXT NOT NULL DEFAULT '',
    text_next_selector           TEXT NOT NULL DEFAULT '',
    integer_max_pages            INTEGER NOT NULL DEFAULT 10
        CONSTRAINT source_max_pages_sane CHECK (integer_max_pages BETWEEN 1 AND 1000),

    -- Cadence and politeness. Six hours and six seconds are deliberately
    -- slow defaults: a list page that changes twice a day does not need to
    -- be read every ten minutes, and the site's operator notices the second
    -- number, not the first.
    integer_interval_minutes     INTEGER NOT NULL DEFAULT 360
        CONSTRAINT source_interval_sane CHECK (integer_interval_minutes BETWEEN 1 AND 43200),
    integer_politeness_seconds   INTEGER NOT NULL DEFAULT 6
        CONSTRAINT source_politeness_sane CHECK (integer_politeness_seconds BETWEEN 0 AND 3600),
    integer_max_new_per_run      INTEGER NOT NULL DEFAULT 25
        CONSTRAINT source_max_new_sane CHECK (integer_max_new_per_run BETWEEN 1 AND 1000),
    integer_timeout_seconds      INTEGER NOT NULL DEFAULT 30
        CONSTRAINT source_timeout_sane CHECK (integer_timeout_seconds BETWEEN 1 AND 600),

    -- The address is the dedupe anchor, not the content: a page with a
    -- date or a visitor counter in it would otherwise be re-submitted and
    -- re-billed on every run. Set this only where a change IS the point -
    -- the editor turns it on for exact mode.
    bool_resubmit_on_change      BOOLEAN NOT NULL DEFAULT false,

    -- Detail page. Empty means "the whole page, boilerplate stripped".
    text_content_selector        TEXT NOT NULL DEFAULT '',
    text_drop_selectors          TEXT NOT NULL DEFAULT '',
    text_notes                   TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_sources_enabled ON scraper_config.sources (bool_enabled);
CREATE INDEX IF NOT EXISTS idx_sources_updated ON scraper_config.sources (date_updated DESC);


-- ============================================================
-- scraper_config.source_projects - which projects read this page
--
-- ONE PAGE, SEVERAL PROJECTS. A project decides what is extracted from a
-- document: its objects of interest and its perspectives. Two projects ask
-- different questions of the same page - "what has this company built" and
-- "what is the technology and who holds the patent" - and there is no way
-- to get both answers out of one extraction. So a page may be assigned to
-- as many projects as it is wanted by, and each of them evaluates it
-- separately, with its own settings and at its own cost.
--
-- THAT COSTS ONCE PER PROJECT, and the editor says so before it is saved.
-- The page is FETCHED once - the site sees one visitor either way - and
-- SUBMITTED once per project.
--
-- WHICH IS WHY scraper.seen_urls IS KEYED BY PROJECT. "Have I had this
-- address before" is a question about a project, not about the crawler: a
-- page already collected for the patents project has never been seen by the
-- companies project, and asking it globally is what would silently give the
-- second project nothing.
--
-- text_key_prefix is the key that evaluates this page FOR THIS PROJECT, as
-- scraper.api_keys.text_prefix. '' means "the project's default", the first
-- live extraction key of that project in the order they were written in the
-- environment - so collection carries on when one is switched off.
--
-- A prefix here is a promise, not a preference. If that key is deleted or
-- deactivated these pages stop being evaluated for this project and the
-- crawler says so by name, rather than quietly moving to a key with
-- different settings and a different price.
--
-- Either way the fallback never leaves the project. A project whose keys
-- are all invalid stops; using another project's key would file the
-- documents in the wrong archive, which is worse than collecting nothing.
-- ============================================================
CREATE TABLE IF NOT EXISTS scraper_config.source_projects (
    bigint_fk_source   BIGINT NOT NULL
        REFERENCES scraper_config.sources (bigint_id) ON DELETE CASCADE,
    text_project_id    TEXT NOT NULL
        CONSTRAINT source_project_not_blank CHECK (btrim(text_project_id) <> ''),
    text_key_prefix    TEXT NOT NULL DEFAULT '',
    -- The order they were chosen in. The first one is the project the
    -- overview names when it has room for one name only.
    integer_order      INTEGER NOT NULL DEFAULT 0,
    date_added         TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    PRIMARY KEY (bigint_fk_source, text_project_id)
);

CREATE INDEX IF NOT EXISTS idx_source_projects_project
    ON scraper_config.source_projects (text_project_id);



-- ============================================================
-- scraper_config.source_patterns - which links count, as a shape
--
-- A pattern is the shape of a link: host, path segments with numbers and
-- long tokens replaced by placeholders, the query keys that matter. It is
-- learned from what a person ticked in the "Links found" popup and stored
-- twice on purpose:
--
--   json_form    the tuple crawlkit.forms produces - THE source of truth,
--                what learn() merges against and what matches() evaluates
--   text_regex   a rendering of it, for the disclosure in the editor and
--                for anyone reading this table with psql
--
-- The regex is derived from the form, never the other way round. Two
-- patterns with the same regex are the same pattern, which is what the
-- UNIQUE constraint says.
-- ============================================================
CREATE TABLE IF NOT EXISTS scraper_config.source_patterns (
    bigint_id          BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    date_added         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    bigint_fk_source   BIGINT NOT NULL
        REFERENCES scraper_config.sources (bigint_id) ON DELETE CASCADE,

    text_kind          TEXT NOT NULL
        CONSTRAINT pattern_kind_known CHECK (text_kind IN ('accept', 'reject')),
    -- What the person sees: /rent/# rather than ^https://…/rent/\d+$.
    text_label         TEXT NOT NULL DEFAULT '',
    text_regex         TEXT NOT NULL,
    json_form          JSONB NOT NULL,
    -- learned        from ticked links
    -- manual         typed in, validated against the last test snapshot
    -- legal_default  the implicit reject list, materialised so it can be
    --                shown next to the others
    text_origin        TEXT NOT NULL DEFAULT 'learned'
        CONSTRAINT pattern_origin_known CHECK (
            text_origin IN ('learned', 'manual', 'legal_default')),
    -- true when learning could not separate ticked from un-ticked links of
    -- the same shape ("/rent/1 vs /rent/2 - only the number differs"). The
    -- un-ticked ones became exact rejects; a person should look.
    bool_needs_review  BOOLEAN NOT NULL DEFAULT false,
    text_example_url   TEXT NOT NULL DEFAULT '',
    integer_examples   INTEGER NOT NULL DEFAULT 0,

    -- Which key evaluates the links this pattern matches, overriding the
    -- source's own choice. '' inherits. One shape of link on a page is often a
    -- different kind of document from the rest, and worth more or less effort.
    text_key_prefix    TEXT NOT NULL DEFAULT '',

    UNIQUE (bigint_fk_source, text_kind, text_regex)
);

CREATE INDEX IF NOT EXISTS idx_source_patterns_source
    ON scraper_config.source_patterns (bigint_fk_source, text_kind);


-- ============================================================
-- scraper_config.source_exact_urls - single addresses, by name
--
--   monitor  the address itself is the document (exact mode), fetched on
--            every run and re-submitted when its content changes
--   reject   never this one, whatever the patterns say - the "exclude just
--            that address?" answer from the popup
--
-- One address, one verdict: the UNIQUE spans the canonical URL alone, so an
-- address cannot be monitored and rejected at once.
-- ============================================================
CREATE TABLE IF NOT EXISTS scraper_config.source_exact_urls (
    bigint_id          BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    date_added         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    bigint_fk_source   BIGINT NOT NULL
        REFERENCES scraper_config.sources (bigint_id) ON DELETE CASCADE,

    text_kind          TEXT NOT NULL
        CONSTRAINT exact_url_kind_known CHECK (text_kind IN ('monitor', 'reject')),
    text_url_canonical TEXT NOT NULL
        CONSTRAINT exact_url_is_http CHECK (text_url_canonical ~* '^https?://'),

    -- As on source_patterns: which key evaluates this one address. '' inherits
    -- the pattern's choice, then the source's, then the project's default.
    text_key_prefix    TEXT NOT NULL DEFAULT '',

    UNIQUE (bigint_fk_source, text_url_canonical)
);

CREATE INDEX IF NOT EXISTS idx_source_exact_urls_source
    ON scraper_config.source_exact_urls (bigint_fk_source, text_kind);


-- ============================================================
-- scraper_config.source_file_rules - which files to send as text
--
-- Two concepts the "Files found" popup keeps visibly apart:
--
--   type_default   text_value is a type (pdf, docx, xlsx, csv, txt, md);
--                  bool_enabled says whether that type is sent at all
--   file_include   text_value is one canonical file URL - an exception
--   file_exclude   that overrides its type's default
--
-- integer_max_mb caps the download per rule; a file above it is recorded
-- as TOO_LARGE in scraper.files after reading at most that much.
-- ============================================================
CREATE TABLE IF NOT EXISTS scraper_config.source_file_rules (
    bigint_id          BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    date_added         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    bigint_fk_source   BIGINT NOT NULL
        REFERENCES scraper_config.sources (bigint_id) ON DELETE CASCADE,

    text_kind          TEXT NOT NULL
        CONSTRAINT file_rule_kind_known CHECK (
            text_kind IN ('type_default', 'file_include', 'file_exclude')),
    text_value         TEXT NOT NULL
        CONSTRAINT file_rule_value_not_blank CHECK (btrim(text_value) <> ''),
    bool_enabled       BOOLEAN NOT NULL DEFAULT true,
    integer_max_mb     INTEGER NOT NULL DEFAULT 25
        CONSTRAINT file_rule_max_mb_sane CHECK (integer_max_mb BETWEEN 1 AND 500),

    UNIQUE (bigint_fk_source, text_kind, text_value)
);

CREATE INDEX IF NOT EXISTS idx_source_file_rules_source
    ON scraper_config.source_file_rules (bigint_fk_source);


-- ============================================================
-- scraper_config.test_snapshots - what the site answered to a draft
--
-- Written by the DASHBOARD, not the crawler: "Test this configuration"
-- runs in the dashboard process so it works without a crawler container.
-- It uses the same crawlkit code the crawler runs, with dry_run=True, and
-- stores what came back here - the robots verdict, the fetch, every link
-- with its classification and whether the draft accepts it, the files.
--
-- bigint_fk_source is NULL for a draft that has not been saved yet. When
-- the source is saved, the dashboard points the snapshot at it, and from
-- then on the newest DONE snapshot per source is what /preview applies a
-- changed configuration to - without fetching anything again.
--
-- json_config is the draft as tested (source fields, patterns, exact URLs,
-- file rules), kept beside the result so a later reader can see what was
-- asked, not only what was answered.
-- ============================================================
CREATE TABLE IF NOT EXISTS scraper_config.test_snapshots (
    bigint_id               BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    date_added              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    bigint_fk_source        BIGINT
        REFERENCES scraper_config.sources (bigint_id) ON DELETE CASCADE,

    json_config             JSONB NOT NULL,
    -- crawl  the list page(s) and their links
    -- files  additionally N sampled subpages and the files found on them
    text_kind               TEXT NOT NULL DEFAULT 'crawl'
        CONSTRAINT snapshot_kind_known CHECK (text_kind IN ('crawl', 'files')),
    integer_sample_subpages INTEGER NOT NULL DEFAULT 5
        CONSTRAINT snapshot_sample_sane CHECK (integer_sample_subpages BETWEEN 0 AND 50),

    text_status             TEXT NOT NULL DEFAULT 'RUNNING'
        CONSTRAINT snapshot_status_known CHECK (
            text_status IN ('RUNNING', 'DONE', 'FAILED')),
    date_started            TIMESTAMPTZ,
    date_finished           TIMESTAMPTZ,
    -- The shape crawlkit.testrun.execute() returns; links capped at 2000.
    json_result             JSONB,
    text_error              TEXT
);

-- "The newest snapshot of this source" is the one question this table is
-- asked, so the index is ordered for it.
CREATE INDEX IF NOT EXISTS idx_test_snapshots_source
    ON scraper_config.test_snapshots (bigint_fk_source, date_added DESC);
CREATE INDEX IF NOT EXISTS idx_test_snapshots_status
    ON scraper_config.test_snapshots (text_status)
    WHERE text_status = 'RUNNING';


-- ============================================================
-- scraper_config.manual_runs - one real crawl, on request, by hand
--
-- WHAT IT IS FOR. "Test this configuration" fetches the list page and shows
-- what it found; it writes nothing and sends nothing, which is what makes it
-- safe to press. It cannot answer the question that comes next: would a
-- document from this page actually be accepted, extracted and archived?
--
-- This table is that question. The dashboard writes a row; the crawler picks
-- it up ON ITS NEXT TICK EVEN WHILE IT IS PAUSED, crawls the page, takes the
-- first `integer_wanted` addresses this project has not had yet, stores them,
-- queues them and submits them at once. Then the dashboard follows each tag
-- through scraper.submit_queue and into processed_data.tasks and says where it
-- got to.
--
-- PAUSED IS NOT IGNORED, IT IS ANSWERED. The pause switch means "do not crawl
-- on a schedule"; it was never meant to mean "and refuse what a person asks
-- for by hand while watching". A manual run is one page, at one moment, on one
-- person's press - it is not the crawler running.
--
-- IT COSTS. It submits, so it is charged like any other document, and the
-- editor says so on the button rather than in a manual.
-- ============================================================
CREATE TABLE IF NOT EXISTS scraper_config.manual_runs (
    bigint_id          BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    date_added         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    date_updated       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    bigint_fk_source   BIGINT NOT NULL
        REFERENCES scraper_config.sources (bigint_id) ON DELETE CASCADE,
    -- '' means every project this source is assigned to. Naming one is how a
    -- person tries a single project without paying for the others.
    text_project_id    TEXT NOT NULL DEFAULT '',
    -- How many documents to take. One is the point of the thing: content by
    -- content, by hand, looked at before the next one.
    integer_wanted     INTEGER NOT NULL DEFAULT 1
        CONSTRAINT manual_run_wanted_sane CHECK (integer_wanted BETWEEN 1 AND 25),

    text_status        TEXT NOT NULL DEFAULT 'REQUESTED'
        CONSTRAINT manual_run_status_known CHECK (
            text_status IN ('REQUESTED', 'RUNNING', 'DONE', 'FAILED')),
    -- The crawler's own sentence about where it is, shown while it runs.
    text_progress      TEXT NOT NULL DEFAULT '',
    text_error         TEXT NOT NULL DEFAULT '',
    -- {"documents": [{"tag", "project_id", "uri", "chars"}], "skipped": [...]}
    -- The tags are the thread the dashboard follows afterwards.
    json_result        JSONB,

    date_started       TIMESTAMPTZ,
    date_finished      TIMESTAMPTZ
);

-- The crawler asks one question of this table on every tick - "is anything
-- waiting" - so the index is the partial one that answers it.
CREATE INDEX IF NOT EXISTS idx_manual_runs_waiting
    ON scraper_config.manual_runs (date_added)
    WHERE text_status IN ('REQUESTED', 'RUNNING');
CREATE INDEX IF NOT EXISTS idx_manual_runs_source
    ON scraper_config.manual_runs (bigint_fk_source, date_added DESC);


-- ============================================================
-- scraper.projects - which Xtracting projects the keys open, PLAIN
--
-- The crawler is the only thing that can know this: it holds the keys, and a
-- key is the only way to find out which project it belongs to. So it asks, on
-- start and on a timer, and writes what it learned here. The dashboard reads it
-- to let a person pick a project before building a watchlist, and writes
-- nothing.
--
-- A PROJECT OUTLIVES ITS KEYS. When every key of a project is taken out of the
-- environment the rows in scraper.api_keys go, and this row stays with its old
-- date_last_seen. That is deliberate and it is the whole mechanism behind
-- "old": a project nothing has used for seven days is one a person can be
-- offered the chance to delete, and a project whose row vanished the moment its
-- key did could never be offered at all.
--
-- Deleting one here removes its watchlists and their rules. It does NOT touch
-- processed_data: the documents stay, and the project stays selectable in every
-- view that reads processed_data.tasks.text_project, because that list is built
-- from the archive rather than from this table. Configuration is disposable;
-- what was collected is not.
-- ============================================================
CREATE TABLE IF NOT EXISTS scraper.projects (
    -- Xtracting's own project id. Not a local identity: it is what
    -- processed_data.tasks.text_project is matched against, so inventing one
    -- here would break the join that makes archived data selectable by project.
    text_project_id       TEXT PRIMARY KEY,

    -- The collector's rule, word for word: project.name or, when that is empty,
    -- the project id. Anything else and the registry stops lining up with what
    -- the archive already recorded under text_project.
    text_name             TEXT NOT NULL DEFAULT '',

    date_first_seen       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- Moved every time a key of this project authenticates. This is what "not
    -- used by the crawler for seven days" is measured against.
    date_last_seen        TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- True when at least one key of this project could read the project itself,
    -- so the rows in scraper.api_keys carry names, settings and an effort step
    -- rather than only what a refusal revealed. The dashboard shows the effort
    -- chooser only when this is true, because without it there is nothing to
    -- choose between.
    bool_keys_detailed    BOOLEAN NOT NULL DEFAULT false,

    -- The project's own defaults, as GET /api/v1/project returned them. Kept
    -- whole rather than split into columns: nothing here is queried, it is
    -- shown beside a key so a person can see what "inherit" means for it.
    json_defaults         JSONB
);

CREATE INDEX IF NOT EXISTS idx_scraper_projects_last_seen
    ON scraper.projects (date_last_seen);


-- ============================================================
-- scraper.api_keys - the keys that answered, and what they may do, PLAIN
--
-- Written by the crawler from what GET /api/v1/key answered for every key in
-- XTRACTING_API_KEYS and XTRACTING_PROJECT_KEYS. Which variable a key was
-- written in does not decide anything - every key is asked - and the split is
-- for a person's own tidiness.
--
-- ONLY KEYS THAT AUTHENTICATE ARE HERE. A key that stops working is deleted on
-- the next probe, and that deletion is what makes the dashboard's warning
-- possible: a watchlist pinned to a prefix that is no longer in this table is a
-- watchlist that has stopped being evaluated, and it can say so by name.
--
-- The secret is never stored. The prefix is the half a person already sees in
-- their own logs and in Xtracting's dashboard.
-- ============================================================
CREATE TABLE IF NOT EXISTS scraper.api_keys (
    text_prefix           TEXT PRIMARY KEY,

    -- The label from `label:key` in the environment, or the prefix when there
    -- was none. A nickname for logs; it decides nothing.
    text_label            TEXT NOT NULL DEFAULT '',
    -- Position in the environment variable, kept because the default key of a
    -- project is "the first live extraction key in the order the person wrote
    -- them". Order is the only way they can express a preference without a
    -- project-read key.
    integer_order         INTEGER NOT NULL DEFAULT 0,

    text_project_id       TEXT NOT NULL DEFAULT '',

    bool_can_extract      BOOLEAN NOT NULL DEFAULT false,
    bool_can_read_project BOOLEAN NOT NULL DEFAULT false,

    date_seen             TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Everything below is only known when some key of the project could read
    -- the project, which is why every one of them is nullable or defaulted:
    -- an ordinary extraction key can say what it may do and nothing more.
    text_name             TEXT NOT NULL DEFAULT '',
    bool_double_check     BOOLEAN,
    bool_high_thinking    BOOLEAN,
    -- 1 neither - 2 thinking - 3 double check - 4 both. Translations do not
    -- count: they are a separate charge and not a step on this ladder.
    -- 0 means "not known", which is what an ordinary key leaves behind.
    integer_effort        INTEGER NOT NULL DEFAULT 0
        CONSTRAINT api_key_effort_known CHECK (integer_effort BETWEEN 0 AND 4),
    text_translations     TEXT NOT NULL DEFAULT '',
    date_created          TIMESTAMPTZ
);

-- "Which keys does this project have, in the order they were written" is the
-- one question the dashboard's chooser asks.
CREATE INDEX IF NOT EXISTS idx_scraper_api_keys_project
    ON scraper.api_keys (text_project_id, integer_order);


-- ============================================================
-- scraper.targets - runtime state per source, PLAIN
--
-- One row per source, keyed by the source's id and updated on every run.
-- No FK to scraper_config.sources: the crawler's config sync creates and
-- removes these rows itself, and the dashboard must be able to delete a
-- source without waiting for it.
--
-- THE SCHEDULE LIVES HERE, NOT IN THE PROCESS. Every target carries its own
-- date_next_run, and the scheduler works through a due list. A restart
-- therefore does not fire every source at once, and a lease - even with a
-- single container - stops an old and a new process from crawling the same
-- site at the same moment during a restart, which the site's operator
-- would see as one client ignoring its own politeness delay.
-- ============================================================
CREATE TABLE IF NOT EXISTS scraper.targets (
    bigint_fk_source             BIGINT PRIMARY KEY,
    date_added                   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    date_updated                 TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Hash of the configuration this target was last synced from. A
    -- changed hash resets the schedule to "due now" and is what makes a
    -- document attributable to a configuration version afterwards.
    text_config_hash             TEXT NOT NULL DEFAULT '',

    date_last_run                TIMESTAMPTZ,
    -- NOW() as default: a source just enabled runs on the next tick.
    date_next_run                TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    bool_leased                  BOOLEAN NOT NULL DEFAULT false,
    text_lease_owner             TEXT NOT NULL DEFAULT '',
    date_lease_until             TIMESTAMPTZ,

    -- Exponential backoff: interval * 2^min(failures + 1, 4), so a dead
    -- site is retried at most sixteen times slower, never never.
    integer_consecutive_failures INTEGER NOT NULL DEFAULT 0,
    text_last_error              TEXT,

    -- The robots.txt verdict for the list page, stored and not just
    -- logged, because the overview shows it as a pill and the editor
    -- refuses "enable" on a forbidden list page.
    bool_robots_allowed          BOOLEAN NOT NULL DEFAULT true,
    text_robots_reason           TEXT NOT NULL DEFAULT '',
    float_robots_crawl_delay     DOUBLE PRECISION,
    date_robots_checked          TIMESTAMPTZ,

    -- '' while the key label resolves; 'unknown label' or the API's own
    -- verdict (401) otherwise. Shown on the overview next to the label.
    text_key_status              TEXT NOT NULL DEFAULT ''
);

-- The one index the scheduler uses every tick. Partial, so it stays as
-- small as the set of free targets.
CREATE INDEX IF NOT EXISTS idx_targets_due
    ON scraper.targets (date_next_run) WHERE NOT bool_leased;


-- ============================================================
-- scraper.seen_urls - "do I know this address?", PLAIN
--
-- Answers that for a hundred links per list page in one round trip whose
-- cost grows with the number of links and not with the size of the
-- archive. Keyed by sha256 of the canonical URI rather than the URI: a URI
-- can be longer than an index page, and a fixed-width key compares faster.
--
-- This is the first dedupe gate; processed_data.tasks.text_source_uri is
-- the second, and it catches what was submitted before this crawler
-- existed or by hand. Both are about the ADDRESS. Content hashes are kept
-- only to notice a change, and acting on a change is per source
-- (bool_resubmit_on_change).
--
-- KEYED BY PROJECT, AND THAT IS THE WHOLE POINT OF THE COLUMN. "Have I had
-- this address before" is a question about a project, not about the
-- crawler. A page assigned to two projects is fetched once and evaluated
-- twice - once per project, each with that project's objects of interest
-- and perspectives - so the address is new to the second project even
-- though the first one collected it an hour ago. Keyed by the address
-- alone, the second project would silently get nothing and the editor's
-- promise that a page can serve two projects would be false.
-- ============================================================
CREATE TABLE IF NOT EXISTS scraper.seen_urls (
    text_project_id        TEXT NOT NULL DEFAULT '',
    text_uri_hash          TEXT NOT NULL,
    text_uri_canonical     TEXT NOT NULL,
    bigint_fk_source       BIGINT NOT NULL,
    text_kind              TEXT NOT NULL DEFAULT 'page'
        CONSTRAINT seen_url_kind_known CHECK (text_kind IN ('page', 'file')),
    text_content_hash_last TEXT NOT NULL DEFAULT '',
    date_first_seen        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    date_last_seen         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    date_last_change       TIMESTAMPTZ,
    integer_times_seen     INTEGER NOT NULL DEFAULT 1,

    -- WHEN THIS ADDRESS WAS LAST HANDED TO THE PLATFORM, and when its result
    -- came back into the archive. NULL and NULL is a page that was fetched
    -- and deliberately NOT sent - unchanged, or a duplicate - and there is
    -- nothing to wait for.
    date_submitted         TIMESTAMPTZ,
    date_confirmed         TIMESTAMPTZ,

    PRIMARY KEY (text_project_id, text_uri_hash)
);

CREATE INDEX IF NOT EXISTS idx_seen_urls_source
    ON scraper.seen_urls (bigint_fk_source, date_last_seen DESC);

-- ============================================================
-- A SUBMITTED ADDRESS IS ONLY PROVISIONALLY "SEEN"
--
-- The crawler writes the address into this table the moment it queues the
-- document, and that must not be the end of it: an address that counts as
-- collected whether or not anything ever comes back means a collector that
-- is down for an afternoon does not merely delay the archive - it LOSES that
-- afternoon's pages, permanently and silently, because the crawler has
-- already written them off as seen and will never offer them again.
--
-- So a submission is provisional. `date_submitted` starts a clock;
-- `date_confirmed` is set by the crawler's own reconcile pass when the
-- document turns up in processed_data.tasks - that is, when the collector has
-- really fetched and filed it. Until then the address counts as seen for two
-- hours and afterwards it is fetchable again.
--
-- WHAT THAT COSTS, SAID OUT LOUD: an extraction that takes longer than two
-- hours will be submitted a second time, and paid for twice. The second dedupe
-- gate (processed_data.tasks.text_source_uri) catches it only once the first
-- result has actually landed. That is the deliberate trade: a page collected
-- twice is money, a page collected never is the thing the archive is for.
-- The window is one number, in one place - crawler/app/store.py:
-- SUBMISSION_GRACE - and raising it is the answer if a platform is slower.
-- ============================================================

-- THE ADDRESSES STILL WAITING FOR A RESULT. A partial index, because that is
-- what is asked for: "which submissions have not come back?" walks the few
-- rows that are unconfirmed rather than the whole table, and on an archive
-- where everything has arrived the index is empty and costs nothing.
CREATE INDEX IF NOT EXISTS idx_seen_urls_unconfirmed
    ON scraper.seen_urls (date_submitted)
 WHERE date_confirmed IS NULL AND date_submitted IS NOT NULL;


-- ============================================================
-- scraper.documents - one row per fetched page or file, HYPERTABLE
--
-- Append-only and growing with time, so a hypertable like the archive's
-- own tables. text_content is EXACTLY what goes to the API and
-- text_content_hash its sha256 - the same hash the platform computes and
-- the collector stores in processed_data.sources.text_content_hash, which
-- is how the dashboard reconciles the two sides.
--
-- The three identity columns are empty when the row is written: the
-- crawler does not know which task the platform will assign. reconcile
-- fills them in once the collector has archived the task, matched by tag.
-- That UPDATE lands inside the two-day pre-compression window below; a
-- document the collector takes longer to archive is updated on a
-- compressed chunk, which works and is merely slower.
-- ============================================================
CREATE TABLE IF NOT EXISTS scraper.documents (
    date_added          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- The page title, else the URI.
    text_name           TEXT NOT NULL,
    text_project        TEXT NOT NULL DEFAULT '',
    text_language       TEXT NOT NULL DEFAULT 'English',
    text_task_id        TEXT NOT NULL DEFAULT '',
    bigint_id           BIGINT GENERATED BY DEFAULT AS IDENTITY,

    bigint_fk_source    BIGINT NOT NULL,
    text_key_label      TEXT NOT NULL DEFAULT '',

    text_uri            TEXT NOT NULL DEFAULT '',   -- as found on the list page
    text_uri_canonical  TEXT NOT NULL DEFAULT '',   -- what goes to the API as `source`
    text_uri_hash       TEXT NOT NULL DEFAULT '',   -- sha256(text_uri_canonical), hex

    text_kind           TEXT NOT NULL DEFAULT 'page'
        CONSTRAINT document_kind_known CHECK (text_kind IN ('page', 'file')),
    text_file_type      TEXT NOT NULL DEFAULT '',   -- pdf | docx | xlsx | csv | txt | md, files only
    -- plain      boilerplate-stripped text of a page
    -- html       cleaned HTML of a page
    -- file_text  the text layer of a file
    text_format         TEXT NOT NULL DEFAULT 'plain'
        CONSTRAINT document_format_known CHECK (
            text_format IN ('plain', 'html', 'file_text')),

    text_content        TEXT NOT NULL DEFAULT '',
    text_content_hash   TEXT NOT NULL DEFAULT '',   -- sha256(text_content), hex
    integer_char_count  INTEGER NOT NULL DEFAULT 0,

    integer_http_status INTEGER,
    text_content_type   TEXT NOT NULL DEFAULT '',
    integer_bytes       INTEGER NOT NULL DEFAULT 0,
    text_title          TEXT,
    -- xs_<source_id>_<hex12>. What the platform echoes back into
    -- processed_data.tasks.text_tag, with -01, -02 appended when it split
    -- the content - hence no hyphen in the tag itself.
    text_tag            TEXT NOT NULL DEFAULT '',

    PRIMARY KEY (date_added, bigint_id)
);

SELECT create_hypertable('scraper.documents', 'date_added',
                         chunk_time_interval => INTERVAL '1 day',
                         if_not_exists => TRUE);

-- Guarded rather than repeated: ALTER TABLE ... SET (timescaledb.compress)
-- is not a no-op on a table that already has compressed chunks. Segmented
-- by source and project - every reading of this table filters on the
-- source, and the project is named for the same reason 01-schema.sql
-- names it: stored once per segment, it makes the chunk smaller.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM timescaledb_information.hypertables
                   WHERE hypertable_schema = 'crawler'
                     AND hypertable_name = 'documents'
                     AND compression_enabled) THEN
        ALTER TABLE scraper.documents SET (
            timescaledb.compress,
            timescaledb.compress_segmentby = 'bigint_fk_source, text_project',
            timescaledb.compress_orderby   = 'date_added DESC, bigint_id DESC'
        );
    END IF;
END
$$;

-- Two days instead of the archive's one: reconcile UPDATEs fresh rows and
-- the dashboard reads them, and a read of a compressed chunk costs more
-- than the space of one more day.
-- The policy lives in init/05-policies.sql, which owns every retention and
-- compression number in one place and, unlike this file, may be applied to an
-- archive that already exists.

CREATE INDEX IF NOT EXISTS idx_documents_project  ON scraper.documents (text_project);
CREATE INDEX IF NOT EXISTS idx_documents_language ON scraper.documents (text_language);
CREATE INDEX IF NOT EXISTS idx_documents_task     ON scraper.documents (text_task_id);
CREATE INDEX IF NOT EXISTS idx_documents_uri_hash ON scraper.documents (text_uri_hash, date_added DESC);
CREATE INDEX IF NOT EXISTS idx_documents_tag      ON scraper.documents (text_tag);
CREATE INDEX IF NOT EXISTS idx_documents_source   ON scraper.documents (bigint_fk_source, date_added DESC);


-- ============================================================
-- scraper.submit_queue - what is on its way to the API, PLAIN
--
-- Keyed by the tag, because the tag is what comes back. Written at least
-- three times per document (queued, sending, sent, archived), which is why
-- it is not a hypertable, and it saturates: it is as large as the open
-- items plus the recently finished ones.
--
-- Status flow:
--   PENDING -> SENDING -> SENT -> ARCHIVED     (reconcile saw the tag in
--                                               processed_data.tasks)
--                     \-> FAILED               (rejected by the API, or
--                                               three transport failures)
--   SKIPPED   never sent: over the limits, or the key was paused
--
-- A 402 or 429 puts the item back to PENDING without counting an attempt
-- and pauses the key for fifteen minutes - backpressure is not a failure
-- of the document.
-- ============================================================
CREATE TABLE IF NOT EXISTS scraper.submit_queue (
    text_tag              TEXT PRIMARY KEY
        CONSTRAINT queue_tag_has_no_hyphen CHECK (text_tag ~ '^[A-Za-z0-9_]+$'),
    date_added            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    date_updated          TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    bigint_fk_source      BIGINT NOT NULL,
    -- Which project this submission is FOR. A page assigned to two projects
    -- produces two rows here - two extractions, two tags, two archived
    -- documents - and this column is what tells them apart and what decides
    -- the key when the row names none.
    text_project_id       TEXT NOT NULL DEFAULT '',
    -- The key this row is to be submitted with, decided when it was queued so
    -- that a key switched off afterwards cannot silently re-price work that was
    -- already costed. '' is the project default, resolved at submit time.
    text_key_prefix       TEXT NOT NULL DEFAULT '',
    text_key_label        TEXT NOT NULL DEFAULT '',
    -- Pointer into scraper.documents. The partitioning column travels with
    -- the id, so the join hits one chunk instead of all of them.
    date_document_added   TIMESTAMPTZ NOT NULL,
    bigint_fk_document    BIGINT NOT NULL,
    -- Redundant with the document, on purpose: submit and reconcile run
    -- every minute and should not have to touch a hypertable to do it.
    text_source_uri       TEXT NOT NULL DEFAULT '',
    text_content_hash     TEXT NOT NULL DEFAULT '',
    integer_char_count    INTEGER NOT NULL DEFAULT 0,

    text_status           TEXT NOT NULL DEFAULT 'PENDING'
        CONSTRAINT queue_status_known CHECK (
            text_status IN ('PENDING', 'SENDING', 'SENT', 'ARCHIVED', 'FAILED', 'SKIPPED')),
    integer_attempts      INTEGER NOT NULL DEFAULT 0,
    text_job_id           TEXT NOT NULL DEFAULT '',
    -- What the API reported as totalTasks. More than the batch size means
    -- it split something, and the reconcile then also accepts tag-01, -02.
    integer_total_tasks   INTEGER,
    text_error            TEXT,
    date_sent             TIMESTAMPTZ,
    date_archived         TIMESTAMPTZ
);

-- Partial: the sender only ever asks for open items, so the index is only
-- ever as large as the open list.
CREATE INDEX IF NOT EXISTS idx_submit_queue_pending
    ON scraper.submit_queue (text_key_label, date_added)
    WHERE text_status = 'PENDING';
CREATE INDEX IF NOT EXISTS idx_submit_queue_inflight
    ON scraper.submit_queue (date_sent)
    WHERE text_status IN ('SENDING', 'SENT');
CREATE INDEX IF NOT EXISTS idx_submit_queue_source
    ON scraper.submit_queue (bigint_fk_source, date_updated DESC);
CREATE INDEX IF NOT EXISTS idx_submit_queue_job
    ON scraper.submit_queue (text_job_id);

-- An archive from before a page could serve more than one project.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                    WHERE table_schema = 'scraper' AND table_name = 'submit_queue'
                      AND column_name = 'text_project_id') THEN
        ALTER TABLE scraper.submit_queue ADD COLUMN text_project_id TEXT NOT NULL DEFAULT '';
        UPDATE scraper.submit_queue q
           SET text_project_id = p.text_project_id
          FROM (SELECT DISTINCT ON (bigint_fk_source) bigint_fk_source, text_project_id
                  FROM scraper_config.source_projects
                 ORDER BY bigint_fk_source, integer_order, text_project_id) p
         WHERE p.bigint_fk_source = q.bigint_fk_source;
    END IF;
END
$$;


-- ============================================================
-- scraper.files - every file seen on a subpage, and what became of it
--
-- Also the ones NOT sent, each with its reason. "Why is that PDF not in
-- the archive?" is the question this table exists for, and the answer has
-- to be readable on the source's page:
--   QUEUED        text extracted, in the submit queue
--   SENT          submitted
--   TOO_LARGE     above the rule's integer_max_mb; read at most that much
--   WRONG_TYPE    the bytes are not what the extension claims
--   NO_TEXT       no text layer (a scanned PDF, an empty sheet)
--   ROBOTS        robots.txt forbids the file's address
--   ERROR         download or parsing failed
--   NOT_SELECTED  type off, or excluded by a file rule
--
-- PLAIN: the status moves, and a file is one row however often it is seen.
-- ============================================================
CREATE TABLE IF NOT EXISTS scraper.files (
    bigint_id               BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    date_added              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    date_updated            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    bigint_fk_source        BIGINT NOT NULL,
    text_page_uri           TEXT NOT NULL DEFAULT '',
    text_file_uri_canonical TEXT NOT NULL,
    text_file_type          TEXT NOT NULL DEFAULT '',
    text_status             TEXT NOT NULL DEFAULT 'QUEUED'
        CONSTRAINT file_status_known CHECK (text_status IN (
            'QUEUED', 'SENT', 'TOO_LARGE', 'WRONG_TYPE', 'NO_TEXT',
            'ROBOTS', 'ERROR', 'NOT_SELECTED')),
    integer_bytes           INTEGER NOT NULL DEFAULT 0,
    text_detail             TEXT,

    UNIQUE (bigint_fk_source, text_file_uri_canonical)
);

CREATE INDEX IF NOT EXISTS idx_files_source
    ON scraper.files (bigint_fk_source, date_updated DESC);
CREATE INDEX IF NOT EXISTS idx_files_status
    ON scraper.files (text_status);


-- ============================================================
-- monitoring.heartbeat - which services are alive
--
-- ONE TABLE FOR EVERY SERVICE, and in `monitoring` rather than in `scraper`
-- because no service owns it. A heartbeat written by the crawler alone would
-- leave the collector able to prove it is alive only by leaving a run row -
-- and a collector that has crashed leaves no run row at all, so the one state
-- worth seeing would be the one state invisible.
--
-- ONE ROW PER SERVICE, UPDATED IN PLACE, so this is a plain table: the rule at
-- the top of this file is that anything UPDATEd after it is written stays plain,
-- because a compression policy turns every update on a closed chunk into a
-- decompression. Nothing here grows: two or three rows, for ever.
--
-- WRITTEN EVERY MINUTE, not every tick. The crawler ticks every five seconds;
-- the dashboard asks whether the row is younger than three minutes, so a
-- write on every tick would tell nobody anything twelve times out of thirteen.
-- ============================================================
CREATE TABLE IF NOT EXISTS monitoring.heartbeat (
    text_service TEXT PRIMARY KEY,
    date_seen    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    text_version TEXT NOT NULL DEFAULT ''
);


-- ============================================================
-- monitoring.service_log - what each service DID, step by step
--
-- WHY THIS IS NOT monitoring.scraper_errors. That table answers "what went
-- wrong"; a person reading it wants the six rows that matter out of a quiet
-- week. This one answers "what is it doing right now", and when it is switched
-- on it writes a row per step - two orders of magnitude more, of which none is
-- interesting tomorrow.
--
-- Two questions, two lifetimes, and a lifetime is a property of a table: this
-- one keeps its rows for TWENTY-FOUR HOURS, where the problem log keeps them for
-- ninety days. They could not share a table even if the columns matched.
--
-- OFF BY DEFAULT. `dashboard.settings` carries `crawler.debug` and
-- `collector.debug`, both 'false' when an archive is made. A switch that is on
-- by default is a disk that fills up while nobody is watching, and the reason
-- to switch it on is always "I am watching right now".
--
-- IT TAKES EFFECT ON THE NEXT PASS, not at once: each service reads the switch
-- where it already reads its configuration - the crawler on its tick, the
-- collector at the start of a round - because a service that re-read a setting
-- mid-round would write half a round's steps and leave the reader wondering
-- what happened to the first half.
-- ============================================================
CREATE TABLE IF NOT EXISTS monitoring.service_log (
    date_added       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    bigint_id        BIGINT GENERATED BY DEFAULT AS IDENTITY,

    text_service     TEXT NOT NULL
        CONSTRAINT service_log_service_known CHECK (
            text_service IN ('crawler', 'collector')),
    -- What it did, in three or four words: "claimed a watched page",
    -- "fetched a list page", "submitted a batch". The column a person scans.
    text_action      TEXT NOT NULL,
    -- The particulars: which page, which key, how many. One line, not a dump.
    text_detail      TEXT NOT NULL DEFAULT '',

    -- What ties the steps of one crawl, or one collector round, together.
    text_run_id      TEXT NOT NULL DEFAULT '',
    bigint_fk_source BIGINT,
    text_project     TEXT NOT NULL DEFAULT '',
    -- How long that step took, when the step is one worth timing.
    integer_ms       INTEGER,

    PRIMARY KEY (date_added, bigint_id)
);

SELECT create_hypertable('monitoring.service_log', 'date_added',
                         chunk_time_interval => INTERVAL '1 hour',
                         if_not_exists => TRUE);

-- One-hour chunks, not one-day: with a 24-hour retention a daily chunk means
-- the oldest rows are dropped a day late, in one lump. Hourly chunks make the
-- table walk forward smoothly and keep each one small enough to scan.
--
-- NO COMPRESSION. A chunk lives for a day and is read while it is fresh;
-- compressing it would spend more than it saves.
-- The policy lives in init/05-policies.sql, which owns every retention and
-- compression number in one place and, unlike this file, may be applied to an
-- archive that already exists.

CREATE INDEX IF NOT EXISTS idx_service_log_service
    ON monitoring.service_log (text_service, date_added DESC);
CREATE INDEX IF NOT EXISTS idx_service_log_run
    ON monitoring.service_log (text_run_id, date_added DESC);
CREATE INDEX IF NOT EXISTS idx_service_log_source
    ON monitoring.service_log (bigint_fk_source, date_added DESC);


-- ============================================================
-- Column comments for the project columns
--
-- The same words in the catalogue as in this file, so that `\d+` in psql
-- answers "which project" the way the comments above do.
-- ============================================================
COMMENT ON COLUMN scraper_config.source_projects.text_project_id IS
    'scraper.projects.text_project_id. A page may be assigned to several.';
COMMENT ON COLUMN scraper_config.source_projects.text_key_prefix IS
    'scraper.api_keys.text_prefix, or empty for the project default. Never a key of another project.';
COMMENT ON COLUMN scraper.submit_queue.text_project_id IS
    'Which project this submission is for. One row per project per document.';
COMMENT ON COLUMN scraper.seen_urls.text_project_id IS
    'Which project has had this address. The gate is per project, so a page assigned to a second project is new to it.';


-- ============================================================
-- monitoring.scraper_runs - one row per crawl of a source
--
-- The exact shape of monitoring.collector_runs, plus the crawler's own
-- counters. The shape is kept - including integer_jobs_seen and friends,
-- which a crawl leaves at zero - so that a query written for one run table
-- works on the other, and so that a script splitting an archive by project
-- treats every table in monitoring identically.
--
-- text_status:
--   OK            crawled
--   SKIPPED       robots.txt forbids the list page; nothing was fetched
--   BLOCKED       the site answered 4xx, 5xx or a browser challenge
--   BACKPRESSURE  the API asked for a pause (402/429) while submitting
--   ERROR         anything else, with the message
--   TEST          a dry run from the dashboard's test runner
-- ============================================================
CREATE TABLE IF NOT EXISTS monitoring.scraper_runs (
    date_added        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    text_name         TEXT NOT NULL,
    text_project      TEXT NOT NULL DEFAULT '',
    text_language     TEXT NOT NULL DEFAULT 'English',
    text_task_id      TEXT NOT NULL DEFAULT '',
    text_key_label    TEXT NOT NULL DEFAULT '',
    text_status       TEXT NOT NULL DEFAULT ''
        CONSTRAINT crawler_run_status_known CHECK (text_status IN (
            'OK', 'SKIPPED', 'BLOCKED', 'BACKPRESSURE', 'ERROR', 'TEST')),
    integer_jobs_seen INTEGER NOT NULL DEFAULT 0,
    integer_jobs_new  INTEGER NOT NULL DEFAULT 0,
    integer_rows      INTEGER NOT NULL DEFAULT 0,
    text_message      TEXT,

    bigint_fk_source  BIGINT,
    integer_pages     INTEGER NOT NULL DEFAULT 0,   -- list pages fetched
    integer_links     INTEGER NOT NULL DEFAULT 0,   -- links found on them
    integer_accepted  INTEGER NOT NULL DEFAULT 0,   -- accepted by the configuration
    integer_new       INTEGER NOT NULL DEFAULT 0,   -- not seen before
    integer_files     INTEGER NOT NULL DEFAULT 0,   -- files queued
    integer_submitted INTEGER NOT NULL DEFAULT 0,   -- documents put in the queue
    integer_ms        INTEGER NOT NULL DEFAULT 0,

    PRIMARY KEY (date_added, text_name)
);

SELECT create_hypertable('monitoring.scraper_runs', 'date_added',
                         chunk_time_interval => INTERVAL '1 day',
                         if_not_exists => TRUE);

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM timescaledb_information.hypertables
                   WHERE hypertable_schema = 'monitoring'
                     AND hypertable_name = 'scraper_runs'
                     AND compression_enabled) THEN
        ALTER TABLE monitoring.scraper_runs SET (
            timescaledb.compress,
            timescaledb.compress_segmentby = 'bigint_fk_source, text_project',
            timescaledb.compress_orderby   = 'date_added DESC, text_name'
        );
    END IF;
END
$$;

-- The policy lives in init/05-policies.sql, which owns every retention and
-- compression number in one place and, unlike this file, may be applied to an
-- archive that already exists.

CREATE INDEX IF NOT EXISTS idx_scraper_runs_project  ON monitoring.scraper_runs (text_project);
CREATE INDEX IF NOT EXISTS idx_scraper_runs_language ON monitoring.scraper_runs (text_language);
CREATE INDEX IF NOT EXISTS idx_scraper_runs_task     ON monitoring.scraper_runs (text_task_id);
CREATE INDEX IF NOT EXISTS idx_scraper_runs_status   ON monitoring.scraper_runs (text_status);
-- "The last run of this source", which the overview asks once per row.
CREATE INDEX IF NOT EXISTS idx_scraper_runs_source
    ON monitoring.scraper_runs (bigint_fk_source, date_added DESC);


-- ============================================================
-- monitoring.scraper_errors - one row per thing that went wrong
--
-- The run table says THAT a run was SKIPPED, BLOCKED or ERROR. This one
-- says WHAT: which source, which host, which address, which rule, and -
-- in one sentence a person who does not write software can act on - what
-- to do about it. That sentence is the whole point of the table:
--
--     robots.txt of www.example.ch forbids the list page
--     (rule: Disallow: /*?*ep=) - only page 1 can be crawled, or ask the
--     site owner
--
-- is an answer; "BLOCKED" is a status word. text_detail underneath it
-- holds the technical half (the rule text, the response snippet, the tail
-- of a traceback) for whoever wants it - the log view keeps it folded away.
--
-- WHO WRITES IT. The crawler, from crawler/app/errorlog.py, wherever a run
-- already records a SKIPPED/BLOCKED/ERROR status, a file it could not send
-- or a submission the API refused. The dashboard's test runner writes the
-- same rows for a test crawl with text_severity = 'info', so a customer
-- reads the same wording in the log that the editor showed them in its
-- banner. Nothing else writes here, and nothing ever updates a row.
--
-- THE THREE IDENTITY COLUMNS are carried like everywhere else in this
-- archive, and are empty for most rows: a crawl that never got a page has
-- no task and no project. They are there so that a script splitting an
-- archive by project can treat this table like every other one, and so
-- that a row written after reconcile (a submission refused for a task that
-- IS in the archive) can name it.
--
-- HYPERTABLE, and the only one here with a retention policy: this is a log.
-- It is read while it is fresh, and a year of rejected-file rows helps
-- nobody.
-- ============================================================
CREATE TABLE IF NOT EXISTS monitoring.scraper_errors (
    date_added          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- A short label for the row - the same words as the kind, in the
    -- language of the log ("robots forbids the list page"). text_name is
    -- the column every table in this archive carries as its headline.
    text_name           TEXT NOT NULL,
    text_project        TEXT NOT NULL DEFAULT '',
    text_language       TEXT NOT NULL DEFAULT '',
    text_task_id        TEXT NOT NULL DEFAULT '',
    bigint_id           BIGINT GENERATED BY DEFAULT AS IDENTITY,

    bigint_fk_source    BIGINT,
    -- Denormalised on purpose: a source that has been deleted is exactly
    -- the one somebody asks about, and a log row that then reads
    -- "source 41" instead of "Flats in Zurich" cannot be answered.
    text_source_name    TEXT NOT NULL DEFAULT '',
    text_host           TEXT NOT NULL DEFAULT '',

    -- What went wrong, in the sixteen kinds the two services can tell
    -- apart. They are grouped the way the log's filter shows them:
    --   the site's rules      ROBOTS_FORBIDDEN, ROBOTS_UNREADABLE
    --   the site's answer     HTTP_4XX, HTTP_5XX, CHALLENGE, TIMEOUT,
    --                         NETWORK, PARSE, NO_LINKS
    --   a file                FILE_TOO_LARGE, FILE_WRONG_TYPE, FILE_NO_TEXT
    --   the platform          SUBMIT_REJECTED, SUBMIT_BACKPRESSURE,
    --                         KEY_INVALID
    --   us                    CONFIG, INTERNAL
    text_kind           TEXT NOT NULL
        CONSTRAINT crawler_error_kind_known CHECK (text_kind IN (
            'ROBOTS_FORBIDDEN', 'ROBOTS_UNREADABLE', 'HTTP_4XX', 'HTTP_5XX',
            'CHALLENGE', 'TIMEOUT', 'NETWORK', 'PARSE', 'NO_LINKS',
            'FILE_TOO_LARGE', 'FILE_WRONG_TYPE', 'FILE_NO_TEXT',
            'SUBMIT_REJECTED', 'SUBMIT_BACKPRESSURE', 'KEY_INVALID',
            'CONFIG', 'INTERNAL')),
    -- error    something a person has to decide about
    -- warning  the run went on, but less was collected than intended
    -- info     a test crawl - the same wording, without the alarm
    text_severity       TEXT NOT NULL DEFAULT 'error'
        CONSTRAINT crawler_error_severity_known CHECK (
            text_severity IN ('error', 'warning', 'info')),

    text_uri            TEXT NOT NULL DEFAULT '',
    integer_http_status INTEGER,
    -- One sentence: what happened, and what to do. Written for the person
    -- who configured the source, not for the person who wrote the crawler.
    text_message        TEXT NOT NULL DEFAULT '',
    -- The technical half, capped by the writer at 4000 characters - a
    -- traceback or an HTML error page is cut, not stored whole.
    text_detail         TEXT NOT NULL DEFAULT '',
    -- Ties the rows of one run together: three rejected files and the
    -- blocked page they were on read as one story, not five accidents.
    text_run_id         TEXT NOT NULL DEFAULT '',

    PRIMARY KEY (date_added, bigint_id)
);

SELECT create_hypertable('monitoring.scraper_errors', 'date_added',
                         chunk_time_interval => INTERVAL '1 day',
                         if_not_exists => TRUE);

-- Guarded like the others: SET (timescaledb.compress) is not a no-op on a
-- table that already has compressed chunks. Segmented by the two columns
-- the log view filters on, so a filtered read of an old chunk opens the
-- segments it needs instead of all of them.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM timescaledb_information.hypertables
                   WHERE hypertable_schema = 'monitoring'
                     AND hypertable_name = 'crawler_errors'
                     AND compression_enabled) THEN
        ALTER TABLE monitoring.scraper_errors SET (
            timescaledb.compress,
            timescaledb.compress_segmentby = 'bigint_fk_source, text_kind',
            timescaledb.compress_orderby   = 'date_added DESC, bigint_id DESC'
        );
    END IF;
END
$$;

-- ------------------------------------------------------------
-- RETENTION KNOBS are in init/05-policies.sql.
--
-- Every compression and retention number in this archive is set in that one
-- file, including the two for this table. It creates nothing, so unlike this
-- one it may be applied to a grown archive whenever you want to change a
-- number - which is the whole reason it exists.
-- ------------------------------------------------------------
-- The policy lives in init/05-policies.sql, which owns every retention and
-- compression number in one place and, unlike this file, may be applied to an
-- archive that already exists.
-- The policy lives in init/05-policies.sql, which owns every retention and
-- compression number in one place and, unlike this file, may be applied to an
-- archive that already exists.

-- The three identity columns are indexed like everywhere else in this
-- archive, even though almost every row leaves them empty: the rule is what
-- lets a script split an archive by project treat every table the same, and
-- verify.sql checks it for the whole of processed_data and monitoring.
CREATE INDEX IF NOT EXISTS idx_scraper_errors_project  ON monitoring.scraper_errors (text_project);
CREATE INDEX IF NOT EXISTS idx_scraper_errors_language ON monitoring.scraper_errors (text_language);
CREATE INDEX IF NOT EXISTS idx_scraper_errors_task     ON monitoring.scraper_errors (text_task_id);

-- The four readings of this table: newest first (the log's own order), the
-- log of one source, one kind, and one severity. Each carries date_added
-- DESC, because every one of them is asked newest-first and bounded by the
-- time range the view offers.
CREATE INDEX IF NOT EXISTS idx_scraper_errors_time
    ON monitoring.scraper_errors (date_added DESC);
CREATE INDEX IF NOT EXISTS idx_scraper_errors_source
    ON monitoring.scraper_errors (bigint_fk_source, date_added DESC);
CREATE INDEX IF NOT EXISTS idx_scraper_errors_kind
    ON monitoring.scraper_errors (text_kind, date_added DESC);
CREATE INDEX IF NOT EXISTS idx_scraper_errors_severity
    ON monitoring.scraper_errors (text_severity, date_added DESC);
