"""The crawler loop.

Every CRAWLER_TICK_SECONDS it does five things, in this order and for a reason:

  1. HEARTBEAT. One row, so the dashboard's pill can say "online". It is
     written first, because a tick that dies later should still show that the
     process was alive when it started.
  2. CONFIG SYNC, but only when `max(date_updated)` in `scraper_config.sources`
     has moved. A source someone just enabled becomes a target due now; one
     whose rules changed becomes due now again; a deleted one loses its target.
  3. LEASES. Expired ones are freed, then what is due is claimed - at most as
     many as there are free slots, one host at a time.
  4. SUBMIT, every CRAWLER_SUBMIT_INTERVAL_SECONDS.
  5. RECONCILE, every CRAWLER_RECONCILE_MINUTES: which submitted documents have
     arrived in the archive.

It runs whether or not there is anything to do. That is the point: configure a
source in the dashboard, and the pages appear in the archive without anyone
remembering to do anything.

SIGTERM finishes the watched page in flight and then stops. Cutting it off would be
safe - every write is transactional - but it would leave a lease standing, and
the next container would have to wait for it to expire.
"""

from __future__ import annotations

import argparse
import json
import logging
import secrets
import signal
import sys
import time
from types import FrameType

import psycopg

from crawlkit import crawl as crawlkit_crawl
from crawlkit import testrun
from crawlkit.robots import AllowAll, RobotsCache
from crawlkit.words import plural

from . import (config, configsync, db, errorlog, keys, manualrun, reconcile,
               scheduler, servicelog, store, submit)

log = logging.getLogger("crawler")

VERSION = "1.0"

#: The name this service writes into `monitoring.heartbeat`. The service, not
#: the worker: the question the dashboard asks is "is the crawler alive", and
#: two containers sharing an archive are two answers to one question, not two
#: questions. A configurable worker name would mean a renamed container
#: writes a row the pill never looks at.
SERVICE = "crawler"

#: How often the heartbeat is written. The reader asks whether the row is
#: younger than three minutes, and this loop ticks every five seconds - so at
#: one write per tick, twelve of every thirteen would tell nobody anything.
HEARTBEAT_SECONDS = 60

_stop = False


def _handle_signal(signum: int, _frame: FrameType | None) -> None:
    global _stop
    log.info("signal %s received, finishing the watched page in flight then stopping",
             signum)
    _stop = True


def stopping() -> bool:
    return _stop


# ----------------------------------------------------------------------
# One source
# ----------------------------------------------------------------------

def run_source(cfg: config.Config, sched: scheduler.Scheduler,
               target: scheduler.DueTarget,
               sleep=time.sleep, *, steps: bool = False) -> crawlkit_crawl.CrawlResult | None:
    """Crawl one source and release its lease, whatever happens.

    The whole crawl is `crawlkit.crawl.run()` - the same call the dashboard
    makes for a test, with a sink that writes instead of one that does not.
    What is left here is the frame: the politeness brake between sources, the
    run row, the robots verdict, and the next date.

    `sleep` is injected for the flow tests. They check the politeness delay by
    its value, and waiting six seconds per page to confirm a number would make
    the suite take minutes and prove nothing more.

    `steps` is the debug switch as the tick read it, passed down rather than
    re-read here: one crawl belongs to one pass, and a value that changed
    between the claim and the fetch would leave half a story in the log.
    """
    started = time.monotonic()
    with db.connect(cfg.database_url) as conn:
        entry = configsync.source_of(conn, target.source_id)
    if entry is None or not entry.enabled:
        # Deleted or switched off between claiming and starting. The target row
        # goes on the next config sync; nothing to do here.
        with db.connect(cfg.database_url) as conn:
            scheduler.release(conn, target.source_id, failed=False,
                              delay_seconds=3600,
                              error="the watched page is gone or switched off")
        return None

    source = entry.source
    host = sched.throttle.acquire(source.list_url, source.politeness_seconds,
                                 stopping)
    status, message, counts, result = "ERROR", "", {}, None
    # One id for everything this run writes into the log: the blocked page and
    # the three files it could not send are one story, and the log view groups
    # them by it instead of showing five unrelated accidents.
    run_id = f"s{source.id}-{secrets.token_hex(4)}"
    sink = store.Store(cfg.database_url, source, key_label=source.key_label,
                       run_id=run_id, steps=steps)
    if steps:
        # The first row of this run's story, and the one that gives the id
        # every later row is grouped by. Written on the sink's own connection
        # because it is open anyway; `record` takes a savepoint, so a debug
        # row that cannot be written cannot cost the crawl its lease.
        servicelog.record(sink.conn, action="claimed a watched page",
                          detail=f"{source.name} - {source.list_url}",
                          run_id=run_id, source_id=source.id)
        sink.conn.commit()
    try:
        robots = (RobotsCache(cfg.user_agent, timeout=source.timeout_seconds)
                  if source.respect_robots else AllowAll(cfg.user_agent))
        result = crawlkit_crawl.run(source, sink, False, user_agent=cfg.user_agent,
                                    robots=robots, sleep=sleep)
        status = result.status
        counts = dict(result.counts)
        message = result.error or "; ".join(result.warnings[:3])

        if steps:
            # WHAT THE SITE ACTUALLY HANDED OVER, which is the step a person
            # watching wants next: a page count of 1 where they expected 4
            # says the pagination rule stopped, and 200 links with 0 accepted
            # says the link rules reject everything.
            servicelog.record(
                sink.conn, action="fetched the list page",
                detail=(f"{plural(counts.get('pages', 0), 'page')}, "
                        f"{plural(counts.get('links', 0), 'link')}, "
                        f"{counts.get('accepted', 0)} accepted - {status}"),
                run_id=run_id, source_id=source.id,
                ms=int((time.monotonic() - started) * 1000))
            sink.conn.commit()

        with db.connect(cfg.database_url) as conn:
            verdict = result.robots
            scheduler.record_robots(
                conn, target.source_id,
                allowed=bool(verdict.get("list_allowed", True)),
                reason=str(verdict.get("list_reason") or ""),
                crawl_delay=verdict.get("crawl_delay"))
    except Exception as exc:  # noqa: BLE001 - one source must not stop the rest
        log.exception("watched page %s (%s) failed", source.id, source.name)
        message = f"{type(exc).__name__}: {exc}"
    finally:
        sink.close()
        sched.throttle.release(host, source.politeness_seconds)

    failed = status in ("BLOCKED", "ERROR")
    elapsed_ms = int((time.monotonic() - started) * 1000)
    with db.connect(cfg.database_url) as conn:
        store.record_run(conn, source_id=source.id, name=source.name,
                         key_label=source.key_label, status=status,
                         counts=counts, ms=elapsed_ms, message=message)
        # The run row says a run was SKIPPED, BLOCKED or ERROR; this says what
        # happened and what to do about it, in one sentence, on the same
        # connection. `record` never raises: a crawl that took four minutes
        # must not be lost because the log could not be written.
        row = errorlog.crawl_row(result, source_name=source.name,
                                 host=source.host, error=message)
        if row is not None:
            errorlog.record(conn, source_id=source.id, source_name=source.name,
                            host=source.host, run_id=run_id, **row)
        scheduler.release(
            conn, target.source_id, failed=failed,
            delay_seconds=scheduler.next_run_seconds(
                entry.interval_minutes, failures=target.consecutive_failures,
                failed=failed),
            error=message if failed else "")

    log.info("watched page %s (%s): %s - %d page(s), %d link(s), %d accepted, "
             "%d queued, %d file(s) in %d ms", source.id, source.name, status,
             counts.get("pages", 0), counts.get("links", 0),
             counts.get("accepted", 0), counts.get("queued", 0),
             counts.get("files_sendable", 0), elapsed_ms)
    return result


# ----------------------------------------------------------------------
# The tick
# ----------------------------------------------------------------------

def submit_run_row(cfg: config.Config, status: str, label: str,
                   message: str) -> None:
    """Backpressure and refusals belong in the run table too.

    They are the answer to "why has nothing arrived since lunchtime?", and that
    question is asked of the Watched pages view, not of the container's log.
    """
    try:
        with db.connect(cfg.database_url) as conn:
            store.record_run(conn, source_id=0, name=f"submit-{label}",
                             key_label=label, status=status, counts={}, ms=0,
                             message=message)
    except psycopg.Error:
        log.warning("could not record the submit run row", exc_info=True)


class Runner:
    """The loop's state between two ticks - deliberately small."""

    def __init__(self, cfg: config.Config) -> None:
        self.cfg = cfg
        self.scheduler = scheduler.Scheduler(
            cfg.database_url, concurrency=cfg.concurrency,
            lease_seconds=cfg.lease_minutes * 60, stopping=stopping)
        self.submitter = submit.Submitter(cfg, on_run=self._submit_run_row)
        self._config_seen = None
        self._last_submit = 0.0
        self._last_reconcile = 0.0
        # Deliberately in the past, so the first tick asks about the keys before
        # anything is crawled. A crawler that collected for ten minutes and only
        # then discovered it has no key for the project would have spent the
        # site's bandwidth to produce a queue nothing can submit.
        self._last_keys = float("-inf")
        # Also in the past, so the first tick says "here" straight away rather
        # than leaving the dashboard's pill red for the first minute of a
        # crawler that is perfectly alive.
        self._last_heartbeat = float("-inf")
        self._said_paused = False
        #: The step-logging switch as the CURRENT tick read it. A property of
        #: the pass, not of the moment: see `_set_steps`.
        self.steps = False

    def _submit_run_row(self, status: str, label: str, message: str) -> None:
        submit_run_row(self.cfg, status, label, message)

    def _set_steps(self, on: bool) -> None:
        """Keep the switch for this tick, and say so once when it moves.

        A crawler that has quietly become chatty - or that quietly stopped
        writing the rows somebody is waiting to read - is a thing to be able
        to see in the container's log, and one line per CHANGE is what that
        costs. One line per tick would be exactly the noise the switch exists
        to control. Same rule as the collector's test-mode line.
        """
        if on == self.steps:
            return
        self.steps = on
        self.submitter.steps = on
        if on:
            log.info("step logging on: every step goes into "
                     "monitoring.service_log, which the Log view shows under "
                     "Crawler and which keeps its rows for 24 hours")
        else:
            log.info("step logging off: nothing more is written to "
                     "monitoring.service_log")

    def tick(self) -> None:
        cfg = self.cfg
        with db.connect(cfg.database_url) as conn:
            # ONCE A MINUTE, ON A CONNECTION THAT IS OPENED ANYWAY. The pause
            # switch is read on every tick because a person who presses it is
            # waiting; the heartbeat is not read that finely by anybody, so
            # it rides along once a minute and stays a statement nobody
            # notices.
            beat_at = time.monotonic()
            if beat_at - self._last_heartbeat >= HEARTBEAT_SECONDS:
                self._last_heartbeat = beat_at
                db.heartbeat(conn, SERVICE, VERSION)
                conn.commit()

            paused = db.paused(conn)
            # ON THE SAME ROUND TRIP AS THE PAUSE, and once for the whole
            # tick. Both are `dashboard.settings` rows the crawler must be
            # able to run without, and reading the switch again halfway
            # through would write the second half of a pass and leave the
            # reader wondering what happened to the first. A change made now
            # therefore shows up on the NEXT tick, which is what the
            # dashboard promises.
            self._set_steps(servicelog.enabled(conn))

        # BEFORE THE PAUSE, AND THAT IS THE POINT. The switch means "do not
        # crawl on a schedule"; a run somebody just asked for and is watching is
        # not the crawler running. One request per tick, and the tick is a round
        # trip that happens anyway - the check needs no doorbell of its own.
        if manualrun.tick(cfg, self.submitter, steps=self.steps):
            return

        with db.connect(cfg.database_url) as conn:
            if paused:
                if not self._said_paused:
                    log.warning("paused: dashboard.settings has scraper.paused "
                                "switched on. Nothing is crawled or submitted "
                                "until it is switched off. A manual run from "
                                "the dashboard still goes out.")
                    self._said_paused = True
                return
            if self._said_paused:
                log.info("the pause has been lifted, carrying on")
                self._said_paused = False

            # Which projects the keys open, and which keys still work. On the
            # same connection as the config sync and just before it, because the
            # sync's verdict about a source's key is only as good as the registry
            # it is read against.
            now = time.monotonic()
            if now - self._last_keys >= cfg.key_refresh_seconds:
                self._last_keys = now
                try:
                    facts = keys.refresh(conn, cfg)
                    keys.backfill_prefixes(conn)
                    conn.commit()
                    if self.steps:
                        servicelog.record(
                            conn, action="probed the keys",
                            detail=(f"{plural(len(cfg.api_keys), 'key')} asked, "
                                    f"{plural(len(facts), 'project')} answered"))
                        conn.commit()
                except Exception as exc:  # noqa: BLE001
                    # Never fatal. The registry is what the dashboard reads to
                    # offer a choice; crawling and submitting run off the queue
                    # and the environment, and both keep working while Xtracting
                    # is unreachable.
                    log.warning("could not refresh the key registry: %s", exc)
                    conn.rollback()

            latest = configsync.latest_change(conn)
            if latest != self._config_seen:
                sources = configsync.load_sources(conn)
                changed = configsync.sync_targets(conn, sources, cfg.labels)
                self._config_seen = latest
                if self.steps:
                    # Only when the sync actually ran. A row saying "nothing
                    # had changed" on every tick would be the whole stream on
                    # a quiet archive, and the question this answers is "did
                    # my edit reach the crawler?".
                    servicelog.record(
                        conn, action="synced the configuration",
                        detail=(f"{plural(len(sources), 'watched page')} read - "
                                f"{changed['new']} new, {changed['changed']} changed, "
                                f"{changed['gone']} gone"))
                    conn.commit()

            scheduler.reap_expired_leases(conn)

        if cfg.only_tests:
            # CRAWLER_ONLY_TESTS: a container that exists to run `--test` and
            # nothing else. It keeps the heartbeat and the config sync, so the
            # dashboard still shows it, and crawls nothing.
            return

        self.scheduler.tick(
            lambda target: run_source(cfg, self.scheduler, target, steps=self.steps))

        if now - self._last_submit >= cfg.submit_interval_seconds:
            self._last_submit = now
            self.submitter.tick()
        if now - self._last_reconcile >= cfg.reconcile_minutes * 60:
            self._last_reconcile = now
            started = time.monotonic()
            with db.connect(cfg.database_url) as conn:
                archived = reconcile.tick(conn)
                if self.steps:
                    # "It says SENT and nothing has arrived" is the question
                    # this step answers, so the row is written even when it
                    # matched nothing - a zero here is the answer, not silence.
                    servicelog.record(
                        conn, action="reconciled the queue",
                        detail=f"{plural(archived, 'document')} found in the archive",
                        ms=int((time.monotonic() - started) * 1000))
                    conn.commit()

    def shutdown(self) -> None:
        self.scheduler.wait_idle()
        self.scheduler.shutdown()


# ----------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------

def test_one(cfg: config.Config, source_id: int, *, kind: str = "crawl",
             sample_subpages: int = 5, allow_private: bool = False) -> int:
    """`--test <source id>`: the same dry run the dashboard does, on stdout.

    For debugging a source from a shell - the dashboard stores the same result
    as a snapshot and draws it. Nothing is written here.
    """
    with db.connect(cfg.database_url) as conn:
        entry = configsync.source_of(conn, source_id)
    if entry is None:
        print(f"there is no watched page with id {source_id}", file=sys.stderr)
        return 2
    result = testrun.execute(entry.config, allow_private, kind=kind,
                             sample_subpages=sample_subpages,
                             user_agent=cfg.user_agent,
                             timeout_seconds=cfg.test_timeout_seconds,
                             progress=lambda message: print(f"  {message}",
                                                            file=sys.stderr))
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    return 0 if result["status"] in ("OK", "SKIPPED") else 1


def run_now(cfg: config.Config, source_id: int, *, project_id: str = "",
            wanted: int = 1) -> int:
    """`--run-now <source id>`: really crawl it, really submit, print where it got to.

    THE SAME PATH THE DASHBOARD'S BUTTON TAKES, and deliberately so: it writes
    the same request row and lets the same code carry it out, rather than being
    a second implementation that can drift. Without a crawler running, this
    process carries out its own request and then reports.
    """
    with db.connect(cfg.database_url) as conn:
        entry = configsync.source_of(conn, source_id)
        if entry is None:
            print(f"there is no watched page with id {source_id}", file=sys.stderr)
            return 2
        row = db.fetch_one(conn, """
            INSERT INTO scraper_config.manual_runs
                  (bigint_fk_source, text_project_id, integer_wanted)
            VALUES (%(source)s, %(project)s, %(wanted)s)
            RETURNING bigint_id
        """, {"source": source_id, "project": project_id,
              "wanted": max(1, min(25, wanted))})
        conn.commit()
    assert row is not None
    log.info("manual run %s asked for: %s", row["bigint_id"], entry.source.name)

    submitter = submit.Submitter(cfg)
    if not manualrun.tick(cfg, submitter):
        # Somebody else's crawler took it first, which is fine - it is the same
        # work. Wait for it rather than doing it twice.
        log.info("another crawler took the request; waiting for it")
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        with db.connect(cfg.database_url) as conn:
            state = db.fetch_one(conn, "SELECT * FROM scraper_config.manual_runs "
                                       "WHERE bigint_id = %s", (row["bigint_id"],))
        if state and state["text_status"] in ("DONE", "FAILED"):
            print(json.dumps({k: v for k, v in state.items()}, indent=2,
                             ensure_ascii=False, default=str))
            return 0 if state["text_status"] == "DONE" else 1
        time.sleep(2)
    print("the run did not finish within five minutes", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="crawler",
        description="Crawls the watched pages configured in the dashboard and "
                    "submits them to Xtracting. Without arguments it runs the loop; "
                    "--test runs one watched page as a dry run and prints the "
                    "result; --run-now really crawls one and really submits.")
    parser.add_argument("--test", type=int, metavar="SOURCE_ID",
                        help="run one watched page as a dry run and print the result")
    parser.add_argument("--run-now", type=int, metavar="SOURCE_ID",
                        help="really crawl this watched page and submit at most "
                             "--wanted new document(s). THIS COSTS: every document "
                             "is an extraction. The same thing the dashboard's "
                             "button does, for a shell.")
    parser.add_argument("--project", default="", metavar="PROJECT_ID",
                        help="with --run-now: only this project of the page, "
                             "rather than every project it is assigned to")
    parser.add_argument("--wanted", type=int, default=1, metavar="N",
                        help="with --run-now: how many documents to send (default 1)")
    parser.add_argument("--kind", choices=("crawl", "files"), default="crawl",
                        help="with --test: also sample subpages for files")
    parser.add_argument("--sample-subpages", type=int, default=5)
    parser.add_argument("--allow-private", action="store_true",
                        help="with --test: allow an address in a private network")
    args = parser.parse_args(argv)

    try:
        cfg = config.load()
    except config.ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    logging.basicConfig(
        level=getattr(logging, cfg.log_level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s  %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S")

    db.wait_for_database(cfg.database_url)
    db.require_crawler_schema(cfg.database_url)

    if args.test is not None:
        return test_one(cfg, args.test, kind=args.kind,
                        sample_subpages=args.sample_subpages,
                        allow_private=args.allow_private)

    if args.run_now is not None:
        return run_now(cfg, args.run_now, project_id=args.project,
                       wanted=args.wanted)

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    log.info("crawler started: %d key(s), %d at a time, tick every %ds, as %r",
             len(cfg.api_keys), cfg.concurrency, cfg.tick_seconds, cfg.user_agent)

    runner = Runner(cfg)
    while not _stop:
        started = time.monotonic()
        try:
            runner.tick()
        except psycopg.Error as exc:
            # The database going away for a moment must not end the process:
            # the schedule is in the database, so a restart would fire nothing
            # early, but a container in a crash loop is nobody's friend.
            log.error("database error in this tick: %s", exc)
        except Exception:  # noqa: BLE001
            log.exception("unhandled error in this tick")

        elapsed = time.monotonic() - started
        deadline = time.monotonic() + max(1.0, cfg.tick_seconds - elapsed)
        while not _stop and time.monotonic() < deadline:
            time.sleep(min(1.0, deadline - time.monotonic()))

    runner.shutdown()
    log.info("stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
