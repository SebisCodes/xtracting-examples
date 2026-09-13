"""The step log.

`monitoring.service_log` is written a row per step while somebody is watching,
and nothing at all the rest of the time. Two claims are worth a flow test,
because both are about the LOOP rather than about `servicelog.record()` - which
the unit suite can reach - and both are the kind of promise a refactor breaks
without any test noticing:

  1. WITH THE SWITCH OFF, A WHOLE PASS WRITES NOTHING. Not "almost nothing":
     the pass below claims a watched page, fetches a list page, decides its
     links, queues documents, submits them and reconciles the queue, and every
     one of those places has a `servicelog.record()` call behind an `if`. One
     of them left on by mistake is a stream that fills a disk on an archive
     where nobody ever turned anything on.

  2. THE SWITCH TAKES EFFECT ON THE NEXT PASS, NOT ON THIS ONE. `Runner.tick()`
     reads it once, where it already reads the pause, and hands the answer down
     to everything the tick does. That is what keeps a pass whole: a value
     re-read halfway through would write the second half of a crawl and leave
     the reader wondering what happened to the first - and "why does my log
     start in the middle" is a worse question than "why is my log empty".

WHAT MAKES THE SECOND CLAIM REAL HERE. The switch is turned on while the crawl
of pass one is still in the pool: `Runner.tick()` hands its sources to a thread
pool and returns, so between that return and `wait_idle()` there is a real
crawl running with a switch that has just changed under it. It writes nothing,
because the value it was started with belongs to the pass it belongs to.

The `wait_idle()` before pass two is part of the subject and not tidiness: pass
two flips `Runner.steps`, and a crawl of pass one still running at that moment
would read the new value out of the same attribute and start writing halfway
through its own story - the exact fault the design avoids.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from app import db, main

#: What the dashboard writes to turn the crawler's stream on. Spelled here
#: rather than imported from `servicelog.SETTING` on purpose: this file is the
#: test of the contract between the dashboard and the crawler, and a constant
#: both sides read from one module would let both sides be renamed together
#: while the archive still carried the old key.
SWITCH = "crawler.debug"

#: The emergency brake, which this file has to be sure is OFF. `tick()` reads
#: the step switch and THEN returns on the pause, so a paused archive would run
#: every assertion below against a pass that did no work at all - and the test
#: would pass while proving nothing. A new archive is seeded paused
#: (dashboard/standard/sql/02-dashboard-seed.sql), so this is the normal case and not a
#: hypothetical one.
PAUSE = "scraper.paused"


@dataclass
class Watching:
    """The two settings rows, and what the crawler has written since."""

    crawler: object
    since: object

    def on(self) -> None:
        _set(self.crawler, SWITCH, "true")

    def steps(self) -> list[dict]:
        """Every step this test could possibly have caused, newest last.

        By SERVICE and by time rather than by source: the steps a pass takes
        before it has claimed anything - the key probe, the config sync, the
        reconciliation - belong to no watched page, and a query that asked for
        one would miss exactly the rows that prove a pass wrote nothing.
        """
        return self.crawler.rows("""
            SELECT * FROM monitoring.service_log
             WHERE text_service = 'crawler' AND date_added >= %s
             ORDER BY date_added, bigint_id
        """, (self.since,))

    def actions(self) -> list[str]:
        return [row["text_action"] for row in self.steps()]


#: The table the switch lives in, as dashboard/standard/sql/01-dashboard-schema.sql
#: creates it. The crawler's suite runs against an archive the dashboard may
#: never have touched, and the crawler itself treats a missing table as
#: "switched off" - so the test creates the table the dashboard would, with
#: the same columns, and nothing else of the dashboard's schema.
_SETTINGS_TABLE = """
    CREATE SCHEMA IF NOT EXISTS dashboard;
    CREATE TABLE IF NOT EXISTS dashboard.settings (
        text_key         TEXT PRIMARY KEY,
        text_value       TEXT NOT NULL DEFAULT '',
        date_updated     TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
"""


def _set(crawler, key: str, value: str) -> None:
    with db.connect(crawler.dsn) as conn:
        db.execute(conn, _SETTINGS_TABLE)
        db.execute(conn, """
            INSERT INTO dashboard.settings (text_key, text_value) VALUES (%s, %s)
            ON CONFLICT (text_key) DO UPDATE SET text_value = EXCLUDED.text_value,
                                                 date_updated = NOW()
        """, (key, value))
        conn.commit()


def _read(crawler, key: str) -> str | None:
    with db.connect(crawler.dsn) as conn:
        db.execute(conn, _SETTINGS_TABLE)
        conn.commit()
    row = crawler.one("SELECT text_value FROM dashboard.settings WHERE text_key = %s",
                      (key,))
    return None if row is None else row["text_value"]


@pytest.fixture()
def watching(crawler):
    """The switch off, the pause off, and everything put back as it was.

    These two settings rows belong to the whole installation - a browser
    session, the dashboard's own suite and any crawler pointed at this archive
    read the same two - so a file that leaves the crawler paused, or leaves it
    chattering into `monitoring.service_log`, has broken something it was not
    testing. Restored to their PREVIOUS value rather than to a default, because
    a missing row and a row saying 'false' are not the same thing to a person
    reading the table.

    AND THE HEARTBEAT, which no other test in this suite has to think about:
    the tests here drive `Runner.tick()`, and a tick writes one row into
    `monitoring.heartbeat` under the service's real name. Left standing, that
    row makes every pill in the dashboard say the crawler is running for the
    next three minutes, on an installation where no crawler exists.
    """
    before = {key: _read(crawler, key) for key in (SWITCH, PAUSE)}
    for key in (SWITCH, PAUSE):
        _set(crawler, key, "false")
    beat = crawler.one("SELECT * FROM monitoring.heartbeat WHERE text_service = 'crawler'")
    # The archive's own clock, not this process's: the rows are stamped by the
    # database, and a container whose clock is a second fast would draw the
    # line in the wrong place and read its own first row as somebody else's.
    since = crawler.one("SELECT NOW() AS now")["now"]

    yield Watching(crawler=crawler, since=since)

    with db.connect(crawler.dsn) as conn:
        db.execute(conn, "DELETE FROM monitoring.service_log "
                         "WHERE text_service = 'crawler' AND date_added >= %s",
                   (since,))
        db.execute(conn, "DELETE FROM monitoring.heartbeat WHERE text_service = 'crawler'")
        if beat is not None:
            # Somebody else's crawler really is running against this archive.
            # Its row goes back with the stamp it had, not with NOW().
            db.execute(conn, """
                INSERT INTO monitoring.heartbeat (text_service, date_seen, text_version)
                VALUES ('crawler', %s, %s)
            """, (beat["date_seen"], beat["text_version"]))
        conn.commit()
    for key, value in before.items():
        if value is None:
            with db.connect(crawler.dsn) as conn:
                db.execute(conn, "DELETE FROM dashboard.settings WHERE text_key = %s",
                           (key,))
                conn.commit()
        else:
            _set(crawler, key, value)


def make_due(crawler, source_id: int) -> None:
    """The target is due now.

    By hand rather than by changing the configuration and letting the sync do
    it: what is being timed here is the switch, and a test whose second pass
    only crawls because a hash happened to move is a test that fails one day
    for a reason that has nothing to do with its subject.
    """
    with db.connect(crawler.dsn) as conn:
        db.execute(conn, """
            UPDATE scraper.targets
               SET date_next_run = NOW() - INTERVAL '1 minute',
                   bool_leased = false, date_lease_until = NULL
             WHERE bigint_fk_source = %s
        """, (source_id,))
        conn.commit()


def test_a_whole_pass_with_the_switch_off_writes_no_steps(crawler, watching):
    """Claim, fetch, decide, queue, submit, reconcile - and not one row."""
    source_id = crawler.add_source("steps off", crawler.site.estate_list,
                                   text_mode="all_except_rejected",
                                   integer_max_new_per_run=2)
    runner = main.Runner(crawler.cfg)
    try:
        runner.tick()
        runner.scheduler.wait_idle()
    finally:
        runner.shutdown()

    # The pass really did the work whose steps are missing. Without this the
    # assertion below would also pass on a pass that crawled nothing at all,
    # which is the way this test would quietly stop testing anything.
    assert crawler.runs(source_id), "the pass crawled nothing, so it proves nothing"
    assert runner.steps is False
    assert watching.actions() == []


def test_the_switch_takes_effect_on_the_pass_after_it(crawler, watching):
    source_id = crawler.add_source("steps on", crawler.site.estate_list,
                                   text_mode="all_except_rejected",
                                   integer_max_new_per_run=2)
    runner = main.Runner(crawler.cfg)
    try:
        # PASS ONE, with the switch off. `tick()` returns as soon as the crawl
        # is in the pool, so it is still running on the line below.
        runner.tick()

        # THE SWITCH GOES ON UNDER A PASS THAT IS STILL RUNNING. Everything
        # that pass does after this moment - the rest of the crawl, its
        # documents, its queue rows - was started under "off" and stays under
        # it, because the value belongs to the pass and not to the clock.
        watching.on()
        runner.scheduler.wait_idle()
        assert runner.steps is False
        assert watching.actions() == [], "a pass wrote steps it had not been told to"

        # PASS TWO reads the switch where it reads the pause - once, at the
        # top - and everything from there on is on the record.
        make_due(crawler, source_id)
        runner.tick()
        runner.scheduler.wait_idle()
        assert runner.steps is True
    finally:
        runner.shutdown()

    assert watching.actions(), "the pass after the switch wrote nothing"

    # THE STORY OF ONE CRAWL, IN THE ORDER IT HAPPENED: which page was
    # claimed, what the list page gave up, and what the two dedupe gates left
    # of it. A stream that starts halfway through is the fault this design
    # exists to avoid, so the FIRST row of the crawl is part of the claim.
    #
    # The crawl's own rows and not the pass's, because they are not the same
    # sequence: a tick hands its sources to a thread pool and then submits and
    # reconciles on its own thread, so which of those lands first is a race and
    # nothing about the promise being tested.
    rows = [row for row in watching.steps()
            if row["text_run_id"].startswith(f"s{source_id}-")]
    actions = [row["text_action"] for row in rows]
    assert actions, "the crawl of the second pass wrote nothing"
    assert actions[0] == "claimed a watched page"
    for step in ("fetched the list page", "decided the links"):
        assert step in actions, f"{step!r} is missing from {actions}"

    # And the row carries what the Log view prints beside the action: which
    # watched page it is about, and one line of particulars.
    assert rows[0]["bigint_fk_source"] == source_id
    assert rows[0]["text_detail"], "a step with no particulars says nothing"


def test_each_crawl_gets_its_own_run_id_and_keeps_it(crawler, watching):
    """"Show me the rest of this story" is one search, not a reading of stamps.

    The Log view's Crawler tab searches over the run id, and the id is what
    ties the claim, the fetch and the documents of ONE crawl together. Every
    row of a crawl has to carry the same one, or the story is a handful of
    unrelated lines; two crawls must not share one, or the story of the quiet
    page and the story of the broken one are one paragraph.

    Two passes and not one because the harness runs at a concurrency of one:
    a pass claims as many sources as it has free slots, so one page per pass
    is what this crawler does, and asking for two in a tick would be asserting
    about a configuration the test did not set.
    """
    watching.on()
    first = crawler.add_source("steps run one", crawler.site.estate_list,
                               text_mode="all_except_rejected",
                               integer_max_new_per_run=1)
    second = crawler.add_source("steps run two", crawler.site.cars + "/de/autos",
                                text_mode="all_except_rejected",
                                integer_max_new_per_run=1)
    runner = main.Runner(crawler.cfg)
    try:
        for _ in (first, second):
            runner.tick()
            runner.scheduler.wait_idle()
    finally:
        runner.shutdown()

    ids = {}
    for row in watching.steps():
        if row["bigint_fk_source"]:
            ids.setdefault(row["bigint_fk_source"], set()).add(row["text_run_id"])
    assert set(ids) == {first, second}, f"one pass each was expected, got {ids}"
    # One id per crawl, not one per row - that is what makes the search find
    # the whole story - and a different one per crawl.
    for source_id, run_ids in ids.items():
        assert len(run_ids) == 1, f"source {source_id} wrote {run_ids}"
    assert ids[first] != ids[second]
