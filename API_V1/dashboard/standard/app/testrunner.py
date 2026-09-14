"""The dashboard's own test crawl: one thread, one test at a time.

"Test this configuration" runs here, in the dashboard process, so that a
customer who has not started the crawler container can still find out what a
list page answers. The crawl itself is `crawlkit.testrun.execute()` - the same
code the crawler runs, with dry_run set - so the preview cannot promise what
the scheduled crawl would not do.

ONE WORKER, NOT A POOL. The addresses of a test all lie on one host, and the
whole point of the crawler's politeness delay is that a site's operator never
sees a burst from us. A second thread would be exactly that burst. So a
request that arrives while a test runs is refused with 409 and the editor
retries - visible as "another test is running", which is the truth, rather
than a queue that quietly grows.

WHY A SECOND THREAD PER JOB. Python cannot stop a thread from outside, and a
crawl that hangs in a socket read would otherwise hold the single worker for
ever. The worker therefore runs the engine in a thread of its own and waits
`timeout_seconds` for it; when the time is up the snapshot is marked FAILED
and the thread is abandoned. It is a daemon, it holds no lock, and its late
progress writes are ignored because they only update rows that are still
RUNNING. The engine has its own per-request timeouts; this is the backstop
for the case where it does not.

The SSRF decision is NOT made here. The endpoint that accepts the test reads
the policy (`DASHBOARD_ALLOW_PRIVATE_HOSTS`) and checks the address with
`crawlkit.netguard.check_public` before a snapshot row exists at all; the flag
then travels with the job so the engine re-checks every redirect hop with the
same answer.
"""

from __future__ import annotations

import inspect
import logging
import queue
import threading
from dataclasses import dataclass
from typing import Any, Callable

log = logging.getLogger(__name__)

#: What the poll endpoint shows before the engine has said anything.
FIRST_PROGRESS = "starting the test"

#: How long the worker waits beyond the engine's own deadline before it gives
#: up on the thread. Short, because the wait is pure loss when it is needed,
#: and needed only when the engine's deadline did not work.
WATCHDOG_GRACE_SECONDS = 2.0


class TestBusy(RuntimeError):
    """Another test is running. Carries the snapshot id that holds the worker,
    so the editor can poll that one instead of guessing."""

    # These names are about the customer's test crawl, not about pytest; the
    # flag stops the test collector from trying to collect them as suites.
    __test__ = False

    def __init__(self, snapshot_id: int | None) -> None:
        super().__init__("another test is running")
        self.snapshot_id = snapshot_id


class TestTimeout(RuntimeError):
    __test__ = False


@dataclass(frozen=True)
class Job:
    snapshot_id: int
    allow_private: bool
    timeout_seconds: int


def crawlkit_execute() -> Callable[..., dict]:
    """The engine, imported when it is first needed.

    Imported late on purpose: `crawlkit`'s pure half has no dependencies and
    the dashboard imports it everywhere, but the crawl engine pulls in httpx,
    lxml and possibly playwright. A build without them must still start, show
    every other view, and say what is missing only when somebody presses Test.
    """
    from crawlkit.testrun import execute  # noqa: PLC0415 - deliberately late
    return execute


def call_execute(execute: Callable[..., dict], config: dict, *, allow_private: bool,
                 progress: Callable[[str], None], kind: str = "crawl",
                 sample_subpages: int = 5, timeout_seconds: float = 120.0) -> dict:
    """Call the engine with the arguments its signature actually names.

    The three that must arrive are ``config``, ``allow_private`` and
    ``progress``; ``kind``, ``sample_subpages`` and ``timeout_seconds`` are
    offered as well and left out when the engine does not name them - it then
    reads them from the snapshot's configuration, where they also stand. All
    of them are passed BY NAME, so a signature that grew an argument in the
    middle cannot silently receive the wrong value, and one that is missing a
    required argument fails here, naming it, instead of deep inside a crawl.
    """
    offered: dict[str, Any] = {
        "config": config, "allow_private": allow_private, "progress": progress,
        "kind": kind, "sample_subpages": sample_subpages,
        "timeout_seconds": float(timeout_seconds),
    }
    try:
        parameters = inspect.signature(execute).parameters
    except (TypeError, ValueError):        # a builtin or a C callable
        return execute(config, allow_private, progress)
    if any(p.kind is p.VAR_KEYWORD for p in parameters.values()):
        return execute(**offered)
    kwargs = {name: value for name, value in offered.items() if name in parameters}
    missing = [name for name, p in parameters.items()
               if name not in kwargs and p.default is p.empty
               and p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)]
    if missing:
        raise TypeError(
            "the crawl engine asks for " + ", ".join(missing) + ", which the test "
            "runner does not have; expected execute(config, allow_private, progress)")
    return execute(**kwargs)


class TestRunner:
    """A daemon thread that works through a queue of snapshot ids.

    `store` is anything with `job`, `start`, `progress`, `finish` and `fail`
    (`sources_store.SnapshotStore` in the application, a recorder in the unit
    test).
    """

    __test__ = False

    def __init__(self, store, *, timeout_seconds: int = 120,
                 execute: Callable[..., dict] | None = None,
                 poll_seconds: float = 0.25) -> None:
        self.store = store
        self.timeout_seconds = max(1, int(timeout_seconds))
        self._execute = execute
        self._poll = poll_seconds
        self._queue: queue.Queue[Job] = queue.Queue()
        self._open: set[int] = set()
        self._current: int | None = None
        self._guard = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    # ── the outside ────────────────────────────────────────────────

    def submit(self, snapshot_id: int, *, allow_private: bool = False,
               timeout_seconds: int | None = None) -> None:
        """Take one test, or refuse because another is running.

        The policy travels with the job rather than living on the runner: it
        is read from the configuration by the endpoint that accepted the
        request, which is the one place that also answers 400 for a private
        address.
        """
        with self._guard:
            if self._open:
                raise TestBusy(self._current or next(iter(self._open)))
            self._open.add(snapshot_id)
            self._queue.put(Job(snapshot_id, bool(allow_private),
                                int(timeout_seconds or self.timeout_seconds)))
        self.start()

    @property
    def busy(self) -> bool:
        with self._guard:
            return bool(self._open)

    @property
    def current(self) -> int | None:
        """The snapshot the worker is on, or the one queued a moment ago."""
        with self._guard:
            return self._current or (next(iter(self._open)) if self._open else None)

    def start(self) -> None:
        with self._guard:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name="test-runner",
                                            daemon=True)
            self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout)
        self._thread = None

    def wait_idle(self, timeout: float = 30.0) -> bool:
        """For tests and for the shutdown path: True when nothing is open."""
        deadline = threading.Event()
        waited = 0.0
        while self.busy and waited < timeout:
            deadline.wait(self._poll)
            waited += self._poll
        return not self.busy

    # ── the thread ─────────────────────────────────────────────────

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                job = self._queue.get(timeout=self._poll)
            except queue.Empty:
                continue
            with self._guard:
                self._current = job.snapshot_id
            try:
                self._run(job)
            except Exception:  # noqa: BLE001
                # Nothing may take the worker down: a dead worker leaves every
                # later test showing "running" for ever.
                log.exception("test snapshot %s could not be run", job.snapshot_id)
                self._safe_fail(job.snapshot_id, "the test could not be run")
            finally:
                with self._guard:
                    self._current = None
                    self._open.discard(job.snapshot_id)
                self._queue.task_done()

    def _safe_fail(self, snapshot_id: int, message: str) -> None:
        try:
            self.store.fail(snapshot_id, message)
        except Exception:  # noqa: BLE001
            log.exception("marking snapshot %s as failed did not work", snapshot_id)

    def _run(self, job: Job) -> None:
        snapshot_id = job.snapshot_id
        row = self.store.job(snapshot_id)
        if row is None:
            log.warning("test snapshot %s is gone; nothing to run", snapshot_id)
            return
        config = row.get("json_config") or {}
        kind = str(row.get("text_kind") or "crawl")
        sample = int(row.get("integer_sample_subpages") or 0)

        self.store.start(snapshot_id)
        report = self._progress_writer(snapshot_id)
        report(FIRST_PROGRESS)

        try:
            execute = self._execute or crawlkit_execute()
        except ImportError as exc:
            # A dashboard built on the slim base, or an image without the I/O
            # half of crawlkit. Say which, because the answer is a rebuild.
            self._safe_fail(snapshot_id,
                            "this dashboard build cannot crawl: " + str(exc))
            return

        try:
            result = self._with_timeout(job, execute, config, report, kind, sample)
        except TestTimeout:
            self._safe_fail(snapshot_id,
                            f"the test was stopped after {job.timeout_seconds} seconds - "
                            "the site did not finish answering. Try a smaller number of "
                            "sample subpages, or raise DASHBOARD_TEST_TIMEOUT_SECONDS.")
            return
        except Exception as exc:  # noqa: BLE001 - every failure belongs on the row
            log.info("test snapshot %s failed: %s", snapshot_id, exc)
            self._safe_fail(snapshot_id, f"{type(exc).__name__}: {exc}")
            return

        if not isinstance(result, dict):
            self._safe_fail(snapshot_id,
                            f"the crawl engine returned {type(result).__name__}, not a result")
            return
        self.store.finish(snapshot_id, result)
        # The same sentence the editor's banner shows, in the log as well, so
        # that "I tested it and it said something about robots" can be looked
        # up afterwards instead of remembered. Severity `info`: a test is
        # somebody trying a configuration out, not something that went wrong.
        record_test_problem(row, result)

    def _progress_writer(self, snapshot_id: int) -> Callable[[str], None]:
        """A `progress(text)` the engine can call as often as it likes.

        The same line twice is not written twice - the engine reports steps,
        not a spinner - and a failing write never reaches the engine: losing
        the progress line is a blemish, losing the crawl is not.
        """
        last: dict[str, str] = {}

        def report(text: Any) -> None:
            line = str(text or "").strip()[:500]
            if not line or last.get("line") == line:
                return
            last["line"] = line
            try:
                self.store.progress(snapshot_id, line)
            except Exception:  # noqa: BLE001
                log.debug("progress for snapshot %s was not stored", snapshot_id,
                          exc_info=True)

        return report

    def _with_timeout(self, job: Job, execute, config: dict,
                      report: Callable[[str], None], kind: str, sample: int) -> Any:
        box: dict[str, Any] = {}

        def target() -> None:
            try:
                box["result"] = call_execute(execute, config,
                                             allow_private=job.allow_private,
                                             progress=report, kind=kind,
                                             sample_subpages=sample,
                                             timeout_seconds=job.timeout_seconds)
            except BaseException as exc:  # noqa: BLE001 - handed to the worker
                box["error"] = exc

        thread = threading.Thread(target=target, daemon=True,
                                  name=f"test-crawl-{job.snapshot_id}")
        thread.start()
        # The engine has the same deadline and stops itself at it, with a
        # partial result that is worth showing. The few seconds on top are for
        # the case where it cannot - a socket that never returns - and the
        # snapshot then says the test was stopped.
        thread.join(job.timeout_seconds + WATCHDOG_GRACE_SECONDS)
        if thread.is_alive():
            raise TestTimeout(f"no answer within {job.timeout_seconds} seconds")
        if "error" in box:
            raise box["error"]
        return box.get("result")


# ── What a test leaves in the log ───────────────────────────────────────
#
# THE TWIN OF THIS LIVES IN crawler/app/errorlog.py (`crawl_row`), and the
# two have to say the same thing about the same crawl - that is the whole
# point of the log: a customer reads the sentence here in the editor's
# banner and finds it again under /logs, worded identically, whether the
# test ran in this process or the crawler ran the real thing.
#
# It is duplicated rather than imported because there is no import path
# between the two services: both packages are called `app`, and only one of
# them can own that name in a process. The honest home for it is `crawlkit`,
# which both already import; moving it there is a change to shared code that
# belongs in its own pass, and is written down as such.

def log_row_for_result(result: dict) -> dict | None:
    """One log row for a finished test crawl, or None when it went well."""
    status = str(result.get("status") or "OK")
    robots = dict(result.get("robots") or {})
    fetch = dict(result.get("fetch") or {})
    warnings = [str(w) for w in (result.get("warnings") or [])]
    http = fetch.get("status") or 0
    detail = "\n".join(part for part in (
        str(robots.get("list_reason") or robots.get("note") or ""),
        str(fetch.get("error") or ""),
        "; ".join(warnings[:5]),
    ) if part)

    if status == "SKIPPED":
        unreadable = not robots.get("status") or int(robots.get("status") or 0) >= 500
        kind = "ROBOTS_UNREADABLE" if unreadable else "ROBOTS_FORBIDDEN"
        message = str(robots.get("list_reason") or robots.get("note")
                      or "robots.txt does not allow this address")
    elif status in ("BLOCKED", "ERROR"):
        if fetch.get("challenge"):
            kind = "CHALLENGE"
        elif http and 400 <= int(http) < 500:
            kind = "HTTP_4XX"
        elif http and int(http) >= 500:
            kind = "HTTP_5XX"
        elif "timeout" in str(fetch.get("error") or "").lower():
            kind = "TIMEOUT"
        elif status == "ERROR" and not fetch.get("error"):
            kind = "INTERNAL"
        else:
            kind = "NETWORK"
        message = (warnings[0] if warnings
                   else str(fetch.get("error") or "the list page could not be read"))
    elif not (result.get("links") or []):
        kind = "NO_LINKS"
        message = (warnings[0] if warnings
                   else "0 links were found on this page.")
    else:
        return None

    return {"kind": kind, "severity": "info", "message": message,
            "detail": detail[:4000], "uri": str(fetch.get("final_url") or ""),
            "http_status": int(http) or None}


def record_test_problem(job_row: dict, result: dict) -> bool:
    """Write the row, and never let the log spoil the test.

    A test that finished is a result on screen; a log row that could not be
    written is a line in the container's log and nothing more.
    """
    row = log_row_for_result(result or {})
    if row is None:
        return False

    config = (job_row or {}).get("json_config") or {}
    list_url = str(config.get("text_list_url") or "")
    host = ""
    if list_url:
        from urllib.parse import urlsplit  # noqa: PLC0415 - one call, on one path
        host = urlsplit(list_url).netloc

    try:
        from .db import get_db  # noqa: PLC0415 - deliberately late, see below
        db = get_db()
        with db.write() as conn:
            conn.execute("""
                INSERT INTO monitoring.scraper_errors
                      (text_name, bigint_fk_source, text_source_name, text_host,
                       text_kind, text_severity, text_uri, integer_http_status,
                       text_message, text_detail, text_run_id)
                VALUES (%(name)s, %(source)s, %(source_name)s, %(host)s, %(kind)s,
                        'info', %(uri)s, %(status)s, %(message)s, %(detail)s, %(run)s)
            """, {
                "name": row["kind"].lower().replace("_", " "),
                "source": (job_row or {}).get("bigint_fk_source"),
                "source_name": str(config.get("text_name") or "")[:500],
                "host": host[:255], "kind": row["kind"],
                "uri": row["uri"][:2000], "status": row["http_status"],
                "message": row["message"][:2000], "detail": row["detail"],
                "run": f"test-{(job_row or {}).get('id') or 0}",
            })
        return True
    except Exception:  # noqa: BLE001 - the test is worth more than its log row
        # Imported late and caught widely on purpose: a dashboard whose
        # database has no crawler tables (or a unit test with no database at
        # all) must still be able to run a test crawl.
        log.debug("the test's log row was not written", exc_info=True)
        return False


# ── The one runner the application uses ─────────────────────────────────
#
# A module-level singleton created on first use rather than in main.py's
# lifespan: main.py is shared by every view and knows nothing about this one.
# The thread is a daemon, so process shutdown ends it.

_runner: TestRunner | None = None
_runner_guard = threading.Lock()


def get_runner(store=None, *, timeout_seconds: int = 120) -> TestRunner:
    global _runner
    with _runner_guard:
        if _runner is None:
            if store is None:
                raise RuntimeError("the test runner needs a store on first use")
            _runner = TestRunner(store, timeout_seconds=timeout_seconds)
        return _runner


def set_runner(runner: TestRunner | None) -> None:
    """Swap the runner - the flow tests put one with a stand-in engine here."""
    global _runner
    with _runner_guard:
        if _runner is not None and _runner is not runner:
            _runner.stop()
        _runner = runner
