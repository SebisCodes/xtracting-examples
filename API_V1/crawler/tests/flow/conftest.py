"""Flow tests: the crawler's own code, a real archive, and a stand-in web site.

    DATABASE_URL=postgresql://... python -m pytest tests -q

Without DATABASE_URL every test here is skipped, not failed - the unit suite is
what runs without a database. Point DATABASE_URL at a throw-away archive
(`database/.env.test`) and never at the one you keep: these tests write into
`scraper_config`, `crawler` and `monitoring`.

WHAT IS REAL HERE AND WHAT IS NOT. The database is real, the HTTP is real (four
hosts in this process), the Xtracting API is a fake with the two habits that
decide the reconciliation - auto-split and the tag suffix. What is faked beyond
that is time: the politeness delay is recorded instead of waited out. Waiting
one second per page to prove that one second is waited would make the suite
take minutes and prove nothing extra.

EVERY ROW THIS SUITE WRITES IS PREFIXED `_crawlertest`, and the fixtures remove
what they made. A test that leaves a source behind would leave a crawler behind.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest

CRAWLER_DIR = Path(__file__).resolve().parents[2]
REPO_ROOT = CRAWLER_DIR.parent
for folder in (str(REPO_ROOT), str(CRAWLER_DIR)):
    if folder not in sys.path:
        sys.path.insert(0, folder)

from app import config, configsync, db, main, scheduler  # noqa: E402
from crawlkit.forms import split_form  # noqa: E402
from crawlkit.hashing import canonical_uri  # noqa: E402
from crawlkit.tests.standin.fake_xtracting import FakeXtracting  # noqa: E402
from crawlkit.tests.standin.site import StandinSite  # noqa: E402

#: Everything this suite writes carries it, and the cleanup deletes by it.
PREFIX = "_crawlertest"

#: The label the test sources point at, and the key the fake API accepts.
KEY_LABEL = "_crawlertest"
KEY_SECRET = "test1234.secret"
#: The project the fake platform says this key belongs to. Its own constant
#: because the key registry is keyed by it and the cleanup has to find it again.
TEST_PROJECT = "_crawlertest_project"


def pytest_collection_modifyitems(items):
    for item in items:
        item.add_marker(pytest.mark.flow)


@pytest.fixture(scope="session")
def dsn() -> str:
    value = os.environ.get("DATABASE_URL", "").strip()
    if not value:
        pytest.skip("DATABASE_URL is not set - the flow tests need an archive")
    with db.connect(value) as conn:
        row = db.fetch_one(conn, "SELECT to_regclass('scraper.targets') AS present")
        if not (row or {}).get("present"):
            pytest.skip("this database has no crawler schema - load "
                        "database/init/03-scraper.sql into it")
    return value


@pytest.fixture(scope="session")
def site():
    """The four stand-in hosts, once for the session."""
    server = StandinSite().start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture()
def api():
    """A fresh fake Xtracting per test - the switches are per scenario."""
    with FakeXtracting() as fake:
        yield fake


@dataclass
class Harness:
    """What a flow test drives: sources in, crawls run, rows out."""

    dsn: str
    site: StandinSite
    api: FakeXtracting
    sched: scheduler.Scheduler
    source_ids: list[int] = field(default_factory=list)
    #: Every politeness delay that was asked for, instead of waited out.
    waits: list[float] = field(default_factory=list)
    _submitter: object = None

    # -- configuration -------------------------------------------------

    @property
    def cfg(self) -> config.Config:
        return config.Config(
            database_url=self.dsn, api_url=self.api.url,
            api_keys=[config.ApiKeyEntry(KEY_LABEL, KEY_SECRET)],
            user_agent="xtracting-crawler-test/1.0 (+https://example.com/crawler)",
            concurrency=1, lease_minutes=5, submit_interval_seconds=0,
            reconcile_minutes=0, max_attempts=3)

    def add_source(self, name: str, list_url: str, *, accept: list[str] = (),
                   reject: list[str] = (), monitor: list[str] = (),
                   exact_reject: list[str] = (), file_types: dict = None,
                   file_exceptions: list[tuple] = (),
                   projects: list[str] | None = None, **fields) -> int:
        """Insert a source the way the dashboard would, and make it due.

        `accept` and `reject` are example URLs: their form becomes the pattern,
        which is exactly what `learn()` stores after someone ticks links in the
        picker. Writing regexes here would test a different thing from what the
        product does.

        `projects` is which projects the page collects into, and it defaults to
        the one project these tests use. A page assigned to none collects
        nothing at all - that is the correct behaviour and it has a test of its
        own, but it is not the default a hundred other tests want.
        """
        columns = {
            "text_name": f"{PREFIX} {name}",
            "text_list_url": list_url,
            "text_list_url_canonical": canonical_uri(list_url),
            "text_host": canonical_uri(list_url).split("/")[2],
            "text_key_label": KEY_LABEL,
            "bool_enabled": True,
            "integer_politeness_seconds": 0,
            "integer_max_new_per_run": 100,
            "integer_timeout_seconds": 10,
            **fields,
        }
        names = ", ".join(columns)
        placeholders = ", ".join(f"%({key})s" for key in columns)
        with db.connect(self.dsn) as conn:
            row = db.fetch_one(conn, f"""
                INSERT INTO scraper_config.sources ({names})
                VALUES ({placeholders}) RETURNING bigint_id
            """, columns)
            source_id = row["bigint_id"]

            for order, project_id in enumerate(
                    projects if projects is not None else [TEST_PROJECT]):
                db.execute(conn, """
                    INSERT INTO scraper_config.source_projects
                          (bigint_fk_source, text_project_id, integer_order)
                    VALUES (%s, %s, %s)
                """, (source_id, project_id, order))

            for kind, examples in (("accept", accept), ("reject", reject)):
                for example in examples:
                    pattern = split_form(canonical_uri(example)).pattern
                    db.execute(conn, """
                        INSERT INTO scraper_config.source_patterns
                              (bigint_fk_source, text_kind, text_label, text_regex,
                               json_form, text_origin, text_example_url,
                               integer_examples)
                        VALUES (%s, %s, %s, %s, %s, 'learned', %s, 3)
                    """, (source_id, kind, pattern.label(), pattern.to_regex(),
                          json.dumps(pattern.to_dict()), example))

            for kind, urls in (("monitor", monitor), ("reject", exact_reject)):
                for url in urls:
                    db.execute(conn, """
                        INSERT INTO scraper_config.source_exact_urls
                              (bigint_fk_source, text_kind, text_url_canonical)
                        VALUES (%s, %s, %s)
                    """, (source_id, kind, canonical_uri(url)))

            for file_type, enabled in (file_types or {}).items():
                db.execute(conn, """
                    INSERT INTO scraper_config.source_file_rules
                          (bigint_fk_source, text_kind, text_value, bool_enabled,
                           integer_max_mb)
                    VALUES (%s, 'type_default', %s, %s, %s)
                """, (source_id, file_type,
                      bool(enabled if not isinstance(enabled, tuple) else enabled[0]),
                      int(enabled[1]) if isinstance(enabled, tuple) else 25))
            for kind, url in file_exceptions:
                db.execute(conn, """
                    INSERT INTO scraper_config.source_file_rules
                          (bigint_fk_source, text_kind, text_value)
                    VALUES (%s, %s, %s)
                """, (source_id, kind, canonical_uri(url)))
            conn.commit()

        self.source_ids.append(source_id)
        self.sync()
        return source_id

    def update_source(self, source_id: int, **fields) -> None:
        """Change a source the way the editor would - `date_updated` moves, so
        the next sync sees it."""
        assignments = ", ".join(f"{key} = %({key})s" for key in fields)
        with db.connect(self.dsn) as conn:
            db.execute(conn, f"""
                UPDATE scraper_config.sources SET {assignments}, date_updated = NOW()
                 WHERE bigint_id = %(id)s
            """, {**fields, "id": source_id})
            conn.commit()
        self.sync()

    def sync(self) -> dict:
        with db.connect(self.dsn) as conn:
            sources = configsync.load_sources(conn)
            return configsync.sync_targets(conn, sources, [KEY_LABEL])

    # -- running -------------------------------------------------------

    def crawl(self, source_id: int):
        """Claim this source's target and run it - the crawler's own frame."""
        with db.connect(self.dsn) as conn:
            db.execute(conn, """
                UPDATE scraper.targets SET date_next_run = NOW() - INTERVAL '1 minute',
                       bool_leased = false, date_lease_until = NULL
                 WHERE bigint_fk_source = %s
            """, (source_id,))
            conn.commit()
            due = [target for target in scheduler.claim_due(conn, limit=50,
                                                            lease_seconds=300)
                   if target.source_id == source_id]
        assert due, f"source {source_id} was not due"
        return main.run_source(self.cfg, self.sched, due[0],
                               sleep=self.waits.append)

    @property
    def submitter(self):
        """One submitter across a scenario: the pause after a 402 lives in it,
        and a fresh object per call would forget it - which is exactly what the
        backpressure test needs to see."""
        from functools import partial

        from app.submit import Submitter
        if self._submitter is None:
            cfg = self.cfg
            self._submitter = Submitter(
                cfg, on_run=partial(main.submit_run_row, cfg))
        return self._submitter

    def submit(self) -> int:
        return self.submitter.tick()

    def reconcile(self) -> int:
        from app import reconcile as reconcile_mod
        with db.connect(self.dsn) as conn:
            return reconcile_mod.tick(conn)

    # -- looking -------------------------------------------------------

    def rows(self, sql: str, params=None) -> list[dict]:
        with db.connect(self.dsn) as conn:
            return db.fetch_all(conn, sql, params)

    def execute(self, sql: str, params=None) -> None:
        """One statement against the archive, committed.

        For the handful of tests that have to MOVE THE CLOCK: a rule measured
        in hours cannot be tested by waiting, so the row is aged in place and
        the crawler is asked again.
        """
        with db.connect(self.dsn) as conn:
            db.execute(conn, sql, params)
            conn.commit()

    def one(self, sql: str, params=None) -> dict | None:
        with db.connect(self.dsn) as conn:
            return db.fetch_one(conn, sql, params)

    def documents(self, source_id: int) -> list[dict]:
        return self.rows("""
            SELECT * FROM scraper.documents WHERE bigint_fk_source = %s
             ORDER BY date_added, bigint_id
        """, (source_id,))

    def queue(self, source_id: int) -> list[dict]:
        return self.rows("""
            SELECT * FROM scraper.submit_queue WHERE bigint_fk_source = %s
             ORDER BY date_added
        """, (source_id,))

    def files(self, source_id: int) -> list[dict]:
        return self.rows("""
            SELECT * FROM scraper.files WHERE bigint_fk_source = %s
             ORDER BY text_file_uri_canonical
        """, (source_id,))

    def runs(self, source_id: int) -> list[dict]:
        return self.rows("""
            SELECT * FROM monitoring.scraper_runs WHERE bigint_fk_source = %s
             ORDER BY date_added
        """, (source_id,))

    def target(self, source_id: int) -> dict:
        return self.one("SELECT * FROM scraper.targets WHERE bigint_fk_source = %s",
                        (source_id,))

    def archive_task(self, *, tag: str, source_uri: str,
                     project: str = PREFIX) -> None:
        """A task in `processed_data.tasks`, as the collector writes it.

        Only the columns that matter here: the archive is the second dedupe
        gate (`text_source_uri`) and the reconciliation key (`text_tag`).
        """
        with db.connect(self.dsn) as conn:
            db.execute(conn, """
                INSERT INTO processed_data.tasks
                      (text_name, text_project, text_language, text_job_id,
                       text_task_id, text_status, text_source_uri, text_tag,
                       date_commissioned)
                VALUES (%(name)s, %(project)s, 'English', 'job-test', %(task)s,
                        'COMPLETED', %(uri)s, %(tag)s, NOW())
            """, {"name": tag, "project": project, "task": f"{PREFIX}:{tag}",
                  "uri": source_uri, "tag": tag})
            conn.commit()

    # -- cleaning ------------------------------------------------------

    def archive_with_collector(self, job: dict, project: str = PREFIX) -> int:
        """Archive a job the way the collector does - with its own code.

        Loaded by path rather than imported: `collector/app` and `crawler/app`
        are both called `app`, and only one of them can own that name in a
        process. Using the real module matters here because what is being
        checked is that the tag the crawler wrote comes back through the
        collector's INSERT and is found again by `reconcile`.
        """
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "collector_store", REPO_ROOT / "collector" / "app" / "store.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        store = module.Store(self.dsn)
        written = 0
        with store.connect() as conn:
            for task in job["tasks"]:
                written += store.store_task(conn, project=project, job=job, task=task)
            conn.commit()
        return written

    def cleanup(self) -> None:
        if not self.source_ids:
            self._delete_archive_rows()
            return
        with db.connect(self.dsn) as conn:
            for table in ("scraper.submit_queue", "scraper.documents",
                          "scraper.seen_urls", "scraper.files", "scraper.targets"):
                db.execute(conn, f"DELETE FROM {table} WHERE bigint_fk_source = ANY(%s)",
                           (self.source_ids,))
            db.execute(conn, "DELETE FROM monitoring.scraper_runs "
                             "WHERE bigint_fk_source = ANY(%s) OR text_name LIKE %s",
                       (self.source_ids, f"{PREFIX}%"))
            db.execute(conn, "DELETE FROM monitoring.scraper_runs "
                             "WHERE text_name LIKE 'submit-%'")
            # The error log the run wrote goes with the run: by source for a
            # crawl, and by the run id the submitter uses for a batch, which
            # belongs to a key label rather than to one source.
            db.execute(conn, "DELETE FROM monitoring.scraper_errors "
                             "WHERE bigint_fk_source = ANY(%s) OR text_run_id = %s",
                       (self.source_ids, f"submit-{KEY_LABEL}"))
            # The step log goes the same way and for the same reason: by
            # source for a crawl, by the submitter's run id for a batch, which
            # belongs to a key label rather than to one source. It is kept for
            # twenty-four hours, so a suite that left its rows behind would
            # leave them exactly where the dashboard's Crawler tab reads them,
            # and whoever looks at an EMPTY stream next would find this suite's
            # crawls in it. A pass with the debug switch off
            # writes nothing here at all, which is why this line is usually a
            # no-op and is still not optional.
            db.execute(conn, "DELETE FROM monitoring.service_log "
                             "WHERE bigint_fk_source = ANY(%s) OR text_run_id = %s",
                       (self.source_ids, f"submit-{KEY_LABEL}"))
            # manual_runs is ON DELETE CASCADE from sources, so this line is
            # only for a run whose source somebody deleted by hand first.
            db.execute(conn, "DELETE FROM scraper_config.manual_runs "
                             "WHERE bigint_fk_source = ANY(%s)", (self.source_ids,))
            db.execute(conn, "DELETE FROM scraper_config.sources "
                             "WHERE bigint_id = ANY(%s)", (self.source_ids,))
            # The registry a probe wrote. Keyed by the test project rather than
            # by source, because a key and a project belong to no source.
            db.execute(conn, "DELETE FROM scraper.api_keys WHERE text_project_id = %s",
                       (TEST_PROJECT,))
            db.execute(conn, "DELETE FROM scraper.projects WHERE text_project_id = %s",
                       (TEST_PROJECT,))
            conn.commit()
        self._delete_archive_rows()

    def _delete_archive_rows(self) -> None:
        """Everything this suite wrote into the archive, by project.

        Found rather than listed: the collector writes a dozen tables and the
        vocabulary tables besides, and a hand-kept list here would go stale the
        first time the archive gains a section.
        """
        with db.connect(self.dsn) as conn:
            tables = db.fetch_all(conn, """
                SELECT c.table_name FROM information_schema.columns c
                  JOIN information_schema.tables t
                    ON t.table_schema = c.table_schema AND t.table_name = c.table_name
                 WHERE c.table_schema = 'processed_data'
                   AND c.column_name = 'text_project'
                   AND t.table_type = 'BASE TABLE'
            """)
            for row in tables:
                db.execute(conn, f'DELETE FROM processed_data."{row["table_name"]}" '
                                 f"WHERE text_project = %s", (PREFIX,))
            db.execute(conn, "DELETE FROM monitoring.collector_runs "
                             "WHERE text_project = %s", (PREFIX,))
            conn.commit()


@pytest.fixture()
def crawler(dsn, site, api):
    """The harness. Everything it created is removed afterwards."""
    sched = scheduler.Scheduler(dsn, concurrency=1, lease_seconds=300,
                                stopping=lambda: False)
    site.reset_log()

    # THE PROJECT HAS A NAME HERE, AND IT IS THE NAME archive_task() FILES
    # UNDER. The second dedupe gate reads processed_data.tasks, which records a
    # project by NAME because that is what the API answers with, while the
    # configuration records it by id; scraper.projects is where the two meet.
    # Written on every test rather than once, because the key-registry suite
    # renames the same project from the fake platform, and a gate whose verdict
    # depends on which file ran first is not a gate anybody can read.
    with db.connect(dsn) as conn:
        db.execute(conn, """
            INSERT INTO scraper.projects (text_project_id, text_name)
            VALUES (%s, %s)
            ON CONFLICT (text_project_id) DO UPDATE SET text_name = EXCLUDED.text_name
        """, (TEST_PROJECT, PREFIX))
        conn.commit()

    harness = Harness(dsn=dsn, site=site, api=api, sched=sched)
    try:
        yield harness
    finally:
        harness.cleanup()
        with db.connect(dsn) as conn:
            db.execute(conn, "DELETE FROM scraper.projects WHERE text_project_id = %s",
                       (TEST_PROJECT,))
            conn.commit()
        sched.shutdown()
