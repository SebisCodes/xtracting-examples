"""The collector loop.

Every POLL_INTERVAL_MINUTES it walks each configured API key in turn, asks what
jobs that key has, and archives the ones it has not seen. Keys are staggered by
KEY_STAGGER_SECONDS so ten keys do not all hit the API in the same second.

It runs whether or not there is anything to fetch. That is the point: start it
once, submit work whenever you like, and the results appear here without
anybody remembering to do anything.

TEST MODE: WHILE SOMEBODY IS WATCHING, IT LOOKS EVERY HALF MINUTE.

Fifteen minutes is the right interval for work nobody is waiting for. It is the
wrong one for the dashboard's "crawl this page and send one document" button,
where a person pressed it, is looking at the screen, and is asking a question -
did it arrive? - that a quarter of an hour does not answer.

So: while a manual run has documents that were submitted and are not archived
yet, the loop shortens its sleep to TEST_POLL_SECONDS. It stays short for at
most TEST_MODE_MINUTES from the submission - long enough for a slow extraction,
short enough that a document the platform never finishes cannot hold the
collector at a half-minute cadence for ever. The moment the last one is
archived, the interval goes back to the configured one.

That check is one cheap statement against a partial index, and it is asked
twice: once when a round ends, and again in each five-second slice of the sleep
that follows. The second is not belt and braces - without it a run submitted a
second after a round began waited the whole interval, which is the fifteen
minutes this feature exists to remove. It needs no doorbell, no port and no
second mechanism: the loop already wakes in five-second slices to be able to
stop.
"""

from __future__ import annotations

import argparse
import logging
import re
import signal
import sys
import time
from types import FrameType

import psycopg

from . import api, config, servicelog
from .servicelog import plural
from .store import Store

log = logging.getLogger("collector")

_stop = False

#: Step logging, as the CURRENT round read it. A module-level value beside
#: `_stop` because `collect_for_key()` is called from three places and
#: threading the switch through all of them would say nothing extra: it is a
#: property of the round, and a round is one pass of this module.
_steps = False

#: What this service is called in `monitoring.heartbeat`, what it writes as
#: its version, and how often. The reader asks whether the row is younger than
#: three minutes, so a minute leaves room for a round that overruns without
#: turning a working collector red.
SERVICE = "collector"
VERSION = "1.0"
HEARTBEAT_SECONDS = 60

#: When the heartbeat was last written, monotonic. Module level beside
#: `_steps` and for the same reason: it is a property of this process, and
#: threading it through the three places that beat would say nothing extra.
_last_beat = float("-inf")

#: How often to look while a manual run is waiting for its result.
TEST_POLL_SECONDS = 30
#: And for how long. Measured from the submission, so a run that is never
#: finished by the platform releases the collector instead of pinning it.
TEST_MODE_MINUTES = 15


def beat(store: Store) -> None:
    """"Still here", at most once a minute.

    CALLED FROM INSIDE THE SLEEP AS WELL AS FROM THE WORK. This service spends
    fourteen of every fifteen minutes asleep, and a heartbeat written only
    when something happens would say "not running" for the whole of that - so
    the loop's five-second slices call this, and so does each key of a round,
    which keeps a round that overruns the reader's three-minute window from
    turning the dashboard red while the collector is at its busiest.

    Best effort, like the step log: the heartbeat is what the DASHBOARD reads,
    not something this service needs to do its work, and a database that is
    briefly away must not stop a round. The clock moves before the write, so a
    failing archive is asked once a minute rather than on every slice.
    """
    global _last_beat
    now = time.monotonic()
    if now - _last_beat < HEARTBEAT_SECONDS:
        return
    _last_beat = now
    try:
        with store.connect() as conn:
            store.heartbeat(conn, SERVICE, VERSION)
            conn.commit()
    except psycopg.Error as exc:
        log.debug("the heartbeat could not be written: %s", exc)


def wait_between_rounds(store: Store, sleep_for: float, testing: bool) -> bool:
    """Sleep until the next round, and cut the sleep short if somebody starts
    waiting for a document while it runs. Returns whether that happened.

    THE HEARTBEAT IS WHY THIS IS A LOOP AND NOT A SLEEP. Between rounds the
    process does nothing at all for up to fifteen minutes, and "nothing at all"
    is exactly what a crashed one does too - so it wakes every five seconds to
    say that it is alive, which is also what lets it stop promptly.

    AND A MANUAL RUN THAT ARRIVES DURING THE SLEEP MUST NOT WAIT FOR IT. A
    cadence decided once, straight after a round, would leave a document
    submitted a second later sitting until the whole interval is up - a run
    submitted at 20:40 not looked for until 20:50, on the one feature whose
    promise is "as good as at once". The question is asked here too, in the
    slice that is already awake, and the
    deadline is pulled in to the test cadence the moment the answer is yes.
    """
    deadline = time.monotonic() + sleep_for
    entered = False
    while not _stop and time.monotonic() < deadline:
        beat(store)
        if not testing and not entered and somebody_is_waiting(store):
            entered = True
            log.info("test mode on: a manual run came in while waiting, "
                     "looking every %ds", TEST_POLL_SECONDS)
            deadline = min(deadline, time.monotonic() + TEST_POLL_SECONDS)
        left = deadline - time.monotonic()
        if left <= 0:
            break
        time.sleep(min(5.0, left))
    return entered


def somebody_is_waiting(store: Store) -> bool:
    """Is somebody waiting for a document right now?

    True while a manual run has something submitted within the last
    TEST_MODE_MINUTES that has not been archived. Both halves matter: without
    the age limit a document the platform loses would hold the cadence for
    ever; without the archived check the collector would keep looking after the
    answer had already arrived.
    """
    try:
        with store.connect() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT count(*) AS waiting
                      FROM scraper.submit_queue q
                     WHERE q.text_status IN ('SENT', 'SENDING')
                       AND q.date_sent > NOW() - make_interval(mins => %(mins)s)
                       AND EXISTS (
                             SELECT 1 FROM scraper_config.manual_runs m
                              WHERE m.bigint_fk_source = q.bigint_fk_source
                                AND m.date_started
                                    > q.date_added - INTERVAL '5 minutes')
                """, {"mins": TEST_MODE_MINUTES})
                row = cur.fetchone()
    except psycopg.Error:
        # The cadence is a convenience. A collector that cannot ask must still
        # collect on its ordinary interval.
        return False
    return bool(row and row["waiting"])


def step(store: Store, action: str, **fields) -> None:
    """One row in the step log, on a connection of its own.

    The collector holds no connection between steps - every write it makes
    opens one, commits and closes it - so a step that has nothing to ride on
    gets its own. That costs a connection per step and is affordable exactly
    because it only happens while somebody has switched the stream on.

    Silent when the switch is off, and best effort when it is on:
    `servicelog.record` never raises, and neither may the round this is
    describing.
    """
    if not _steps:
        return
    try:
        with store.connect() as conn:
            servicelog.record(conn, action=action, **fields)
            conn.commit()
    except psycopg.Error:
        # Not even a warning: `record` has already logged what went wrong,
        # and a failure to OPEN a connection is about to be reported by the
        # real work of the next step in a sentence that means more.
        log.debug("the step %r could not be logged", action)


def read_step_switch(store: Store) -> bool:
    """The debug switch, once per round, said out loud when it moves.

    Read here rather than per key so that a round is in the log whole or not
    at all: a switch flipped halfway through would write the second half of a
    round and leave the reader wondering about the first. A change therefore
    takes effect on the NEXT round, which is what the dashboard promises.
    """
    global _steps
    try:
        with store.connect() as conn:
            on = servicelog.enabled(conn)
    except psycopg.Error:
        # The same rule as `somebody_is_waiting`: a collector that cannot ask
        # must still collect. Off is the answer that writes nothing.
        on = False
    if on != _steps:
        _steps = on
        if on:
            log.info("step logging on: every step goes into "
                     "monitoring.service_log, which the Log view shows under "
                     "Collector and which keeps its rows for 24 hours")
        else:
            log.info("step logging off: nothing more is written to "
                     "monitoring.service_log")
    return _steps


def _handle_signal(signum: int, _frame: FrameType | None) -> None:
    """Finish the job in flight, then exit.

    Killing mid-task would be safe - writes are transactional and idempotent -
    but a clean stop keeps the logs readable.
    """
    global _stop
    log.info("signal %s received, finishing the current key then stopping", signum)
    _stop = True


def collect_for_key(cfg: config.Config, store: Store, entry: config.ApiKeyEntry) -> None:
    # Every key, so a round with ten of them keeps saying "still here" while
    # it walks them. `--once` says it too, which is correct: a collector run
    # from a shell or from cron IS the collector running, for as long as the
    # reader's window lasts.
    beat(store)
    client = api.Client(cfg.api_url, entry.secret, timeout=cfg.request_timeout_seconds)

    # FIRST WHAT THE KEY IS, THEN WHAT IT HAS. The label in .env is the
    # reader's own mark; the key's name and the project it opens come from
    # the platform, so every line below can name both. A key with "Read
    # project" is asked for the project's name as well, which is the name
    # the dashboard and the logs then carry.
    facts = client.describe_key()
    project_name = facts.project_name
    if facts.known and facts.can_read_project:
        project_name = client.project_name() or project_name
    if facts.known:
        who = f"key {facts.name!r}" if facts.name else "key"
        opens = project_name or facts.project_id or "an unnamed project"
        log.info("[%s] %s opens project %s%s", entry.label, who, opens,
                 "" if facts.usable else f" - not usable: {facts.reason or 'no reason given'}")
        step(store, "asked what the key is", project=project_name or facts.project_id,
             detail=(f"key {entry.label!r} is {who}, project {opens}; "
                     f"{'may' if facts.can_read_project else 'may not'} read the project"))

    step(store, "asked a key", detail=f"key {entry.label!r} at {cfg.api_url}")
    try:
        project, jobs = client.list_jobs()
    except api.ApiError as exc:
        log.error("[%s] listing jobs failed: %s", entry.label, exc)
        with store.connect() as conn:
            store.record_run(conn, project=project_name or "", key_label=entry.label,
                             status="ERROR", jobs_seen=0, jobs_new=0, rows=0, message=str(exc))
            conn.commit()
        return

    project_id = project.name or project_name or project.project_id
    if not project_id:
        # Without a project there is nowhere to file rows, and filing them under
        # an empty string would mix keys together.
        log.error("[%s] the API returned no project for this key - skipping", entry.label)
        return

    log.info("[%s] project %s: %d job(s) known to the API", entry.label, project_id, len(jobs))
    step(store, "saw the jobs", project=project_id,
         detail=(f"key {entry.label!r}, project {project_id}: "
                 f"{plural(len(jobs), 'job')} known to the platform"))

    with store.connect() as conn:
        known = store.recently_collected(conn, project_id, cfg.collected_lookback_hours)
    fetched = new_tasks = rows = 0

    for job in jobs:
        if _stop:
            break
        job_id = job.get("jobId")
        if not job_id:
            continue
        # A job whose tasks are all archived needs no request at all. `known`
        # covers the last COLLECTED_LOOKBACK_HOURS only; an older job that is
        # still listed gets one request and a 410, which is the platform saying
        # its results expired. That is correct and costs one call.
        if job.get("status") in ("COMPLETED", "PARTIAL_FAILURE"):
            expected = {f"{job_id}:{i}" for i in range(job.get("totalTasks") or 0)}
            if expected and expected <= known:
                continue
        elif job.get("status") in ("QUEUED", "PROCESSING"):
            # Still running. It will be picked up on a later pass.
            continue

        try:
            full = client.get_job(job_id, retain=cfg.retain_on_read)
        except api.ApiError as exc:
            if exc.status == api.GONE:
                # Results were dropped by the platform's retention sweep before
                # this collector reached them. Normal, and not recoverable.
                log.info("[%s] job %s expired before it could be archived", entry.label, job_id)
                continue
            log.error("[%s] job %s: %s", entry.label, job_id, exc)
            continue

        fetched += 1
        step(store, "fetched a job", project=project_id,
             detail=(f"key {entry.label!r}, job {job_id}: "
                     f"{plural(len(full.get('tasks', []) or []), 'task')} in it"))
        before = (new_tasks, rows)
        try:
            with store.connect() as conn:
                for task in full.get("tasks", []):
                    if task.get("status") != "COMPLETED":
                        continue
                    task_id = f"{job_id}:{task.get('taskIndex', 0)}"
                    if task_id in known:
                        continue
                    rows += store.store_task(conn, project=project_id, job=full, task=task)
                    known.add(task_id)
                    new_tasks += 1
                conn.commit()
        except psycopg.Error as exc:
            # One bad job must not stop the round; the next pass will retry it.
            log.error("[%s] job %s could not be stored: %s", entry.label, job_id, exc)
            continue
        # WHAT THIS JOB ADDED, not what the key has added so far: a running
        # total repeated on every job reads like the same work being done
        # again, and the number somebody is waiting for is "did MY document
        # land?" - which is this job's.
        step(store, "wrote the tasks", project=project_id,
             detail=(f"key {entry.label!r}, job {job_id}: "
                     f"{plural(new_tasks - before[0], 'new task')}, "
                     f"{plural(rows - before[1], 'row')} written"))

    log.info("[%s] %d job(s) fetched, %d new task(s), %d row(s) written",
             entry.label, fetched, new_tasks, rows)
    with store.connect() as conn:
        store.record_run(conn, project=project_id, key_label=entry.label, status="OK",
                         jobs_seen=len(jobs), jobs_new=new_tasks, rows=rows)
        conn.commit()


def connection_hint(dsn: str, exc: Exception) -> str | None:
    """What to change, for the two failures that are actually configuration.

    Repeating a resolver error thirty times tells someone their database is
    unreachable, which they already know. It does not tell them that the host
    they need is in a variable they have never seen.
    """
    # An IPv6 host is bracketed in a DSN - postgresql://u:p@[::1]:5432/db - so
    # the bracketed form has to be tried first, or the host reads as "[".
    host = ""
    match = re.search(r"@\[([^\]]+)\]", dsn) or re.search(r"@([^/:?]+)", dsn)
    if match:
        host = match.group(1)
    text = str(exc).lower()

    if host in {"localhost", "127.0.0.1", "::1"}:
        return (f"POSTGRES_HOST is {host!r}, which inside a container means this "
                "container - not the machine it runs on. Use 'xtracting_db' for the "
                "database from API_V1/database, or host.docker.internal (Podman: "
                "host.containers.internal) for one running directly on your machine.")

    if "name resolution" in text or "name or service not known" in text:
        if host == "xtracting_db":
            return ("The name 'xtracting_db' only resolves on the archive network, "
                    "which the database container creates. Start API_V1/database first, or "
                    "set POSTGRES_HOST in .env to where your database actually is.")
        return (f"The host {host!r} does not resolve from inside the container. Check "
                "POSTGRES_HOST in .env - a hostname or IP the container can reach.")

    return None


def wait_for_database(store: Store, dsn: str, attempts: int = 30) -> None:
    hinted = False
    for i in range(attempts):
        try:
            with store.connect() as conn:
                conn.execute("SELECT 1")
            return
        except psycopg.Error as exc:
            if not hinted:
                hint = connection_hint(dsn, exc)
                if hint:
                    log.error("%s", hint)
                hinted = True
            log.info("waiting for the database (%d/%d): %s", i + 1, attempts, exc)
            time.sleep(2)
    raise SystemExit("database did not become available")


def require_archive_schema(store: Store) -> None:
    """Stop early if this is a PostgreSQL without the archive schema.

    .env.example invites pointing DATABASE_URL at any database carrying the
    schema, so pointing it at one that does not is a predictable mistake. Left
    unchecked it surfaces as a traceback from whichever table is touched first,
    which reads like a bug in the collector.
    """
    with store.connect() as conn:
        # dict_row on the connection, so name the column rather than index it.
        row = conn.execute(
            "SELECT to_regclass('processed_data.tasks') IS NOT NULL AS present"
        ).fetchone()
        found = bool(row and row["present"])
    if not found:
        raise SystemExit(
            "connected, but this database has no archive schema - "
            "processed_data.tasks does not exist. Point the collector at the "
            "database from API_V1/database, or load database/init/01-schema.sql "
            "into this one first."
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="collector",
        description="Fetches finished extractions from Xtracting into the "
                    "archive. Without arguments it runs the loop; --once does "
                    "one round and stops, which is how you fetch a result "
                    "straight away instead of waiting for the next interval.")
    parser.add_argument("--once", action="store_true",
                        help="do one round over every key and exit")
    parser.add_argument("--key", default="", metavar="LABEL",
                        help="with --once: only this key, by its label in "
                             "XTRACTING_API_KEYS")
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

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    store = Store(cfg.database_url)
    wait_for_database(store, cfg.database_url)
    require_archive_schema(store)

    if args.once:
        keys = [e for e in cfg.api_keys if not args.key or e.label == args.key]
        if args.key and not keys:
            print(f"there is no key labelled {args.key!r}. The labels are: "
                  + ", ".join(e.label for e in cfg.api_keys), file=sys.stderr)
            return 2
        log.info("one round over %d key(s)", len(keys))
        # `--once` is a round too, and somebody who ran it by hand is the
        # very person the stream is for. Read once, here, for the same
        # reason the loop reads it at the top of a round.
        read_step_switch(store)
        step(store, "started a round",
             detail=f"{plural(len(keys), 'key')} to walk (one round, by hand)")
        for i, entry in enumerate(keys):
            if i and cfg.key_stagger_seconds:
                time.sleep(cfg.key_stagger_seconds)
            try:
                collect_for_key(cfg, store, entry)
            except Exception:
                log.exception("[%s] unhandled error", entry.label)
                return 1
        return 0

    log.info("collector started: %d key(s), every %d minute(s), %ds apart, %s",
             len(cfg.api_keys), cfg.poll_interval_minutes, cfg.key_stagger_seconds, cfg.api_url)
    log.info("dedup window %dh, content on the platform is %s",
             cfg.collected_lookback_hours,
             "CLEARED ON READ" if cfg.clear_content_on_fetch else "left for the platform to expire")

    testing = False
    while not _stop:
        started = time.monotonic()
        # THE SWITCH, ONCE, BEFORE THE ROUND. Same place in the loop as the
        # cadence decision below and for the same reason: both are settings
        # this round is entitled to keep, so a change made now shows up on
        # the next round rather than halfway through this one.
        read_step_switch(store)
        step(store, "started a round",
             detail=f"{plural(len(cfg.api_keys), 'key')} to walk, "
                    f"{cfg.key_stagger_seconds}s apart")
        one_round(cfg, store)
        elapsed = time.monotonic() - started
        step(store, "finished a round",
             detail=f"{plural(len(cfg.api_keys), 'key')} walked",
             ms=int(elapsed * 1000))

        if _stop:
            break

        # Somebody watching a manual run gets a half-minute cadence; everyone
        # else gets the configured interval. Said out loud once on each change,
        # because a collector that has quietly become chatty is a thing to be
        # able to see in the log.
        waiting = somebody_is_waiting(store)
        if waiting != testing:
            testing = waiting
            if waiting:
                log.info("test mode on: a manual run is waiting for its result, "
                         "looking every %ds", TEST_POLL_SECONDS)
            else:
                log.info("test mode off: back to every %d minute(s)",
                         cfg.poll_interval_minutes)
        # Measured from the start of the round, so the interval is the interval
        # and not the interval plus however long the round took.
        wanted = (TEST_POLL_SECONDS if waiting
                  else cfg.poll_interval_minutes * 60)
        sleep_for = max(5.0, wanted - elapsed)
        log.info("round finished in %.0fs, next in %.0fs", elapsed, sleep_for)
        if wait_between_rounds(store, sleep_for, testing):
            testing = True

    log.info("stopped")
    return 0


def one_round(cfg: config.Config, store: Store) -> None:
    """Every key once, staggered. Lifted out of the loop so that `--once` and
    the loop run exactly the same round rather than two that look alike."""
    for i, entry in enumerate(cfg.api_keys):
        if _stop:
            break
        if i and cfg.key_stagger_seconds:
            time.sleep(cfg.key_stagger_seconds)
        try:
            collect_for_key(cfg, store, entry)
        except Exception:
            # The loop has to survive anything one key can do to it.
            log.exception("[%s] unhandled error", entry.label)


if __name__ == "__main__":
    raise SystemExit(main())
