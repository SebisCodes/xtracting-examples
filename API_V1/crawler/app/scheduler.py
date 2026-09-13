"""The schedule: every source carries its own next date.

THIS IS THE CORE OF THE CRAWLER, and it works differently from what one would
first build.

The obvious design is a process that crawls a source and then sleeps for its
interval. With a hundred sources that needs a hundred processes - or they run
one after another, and then the sum of all the waiting decides the pace.

Instead every target carries its own `date_next_run` in `scraper.targets`. The
scheduler wakes every few seconds, takes what is due, and hands it to a pool
of fixed size. With a hundred sources it is busy nearly all the time, and the
individual web site still notices it only every six hours.

Three things that are easy to miss:

  * THE DATE IS PERSISTENT. A restart therefore does not fire every source at
    once - exactly the wave the schedule exists to prevent, and after a crash
    loop it would be a barrage on the sites.

  * THE LEASE IS NEEDED EVEN WITH ONE CONTAINER. During a restart the old and
    the new process overlap for a moment. Without a lease both take the same
    source, and the site's operator sees a client ignoring its own politeness
    delay.

  * THE POLITENESS BRAKE IS PER HOST, NOT PER SOURCE. Two sources on the same
    domain must never run at the same time, even when both have a free slot.
"""

from __future__ import annotations

import logging
import os
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable

import psycopg

from crawlkit.throttle import HostThrottle

from . import db

log = logging.getLogger(__name__)

#: The backoff doubles at most four times: interval * 2^4 = sixteen times
#: slower. Never "never" - a site that was down for a day comes back.
MAX_BACKOFF_DOUBLINGS = 4


@dataclass
class DueTarget:
    source_id: int
    consecutive_failures: int
    config_hash: str = ""


def owner_name() -> str:
    """Who holds a lease. Host and pid, so a stuck lease can be traced to a
    container rather than to "somebody"."""
    return f"{os.uname().nodename}:{os.getpid()}"


def claim_due(conn: psycopg.Connection, *, limit: int,
              lease_seconds: int, owner: str = "") -> list[DueTarget]:
    """Claim what is due - atomically.

    `FOR UPDATE SKIP LOCKED` in a subquery, then an UPDATE on the rows it
    picked. Two schedulers running at once get disjoint sets instead of the
    same row twice, which would mean crawling a site twice as fast as promised.
    """
    if limit <= 0:
        return []
    rows = db.fetch_all(conn, """
        UPDATE scraper.targets t
           SET bool_leased      = true,
               text_lease_owner = %(owner)s,
               date_lease_until = NOW() + make_interval(secs => %(lease)s),
               date_updated     = NOW()
          FROM (SELECT bigint_fk_source
                  FROM scraper.targets
                 WHERE date_next_run <= NOW()
                   -- The lease reaper: a target whose worker crashed would
                   -- otherwise be locked forever.
                   AND (NOT bool_leased OR date_lease_until < NOW())
                 ORDER BY date_next_run
                 LIMIT %(limit)s
                   FOR UPDATE SKIP LOCKED) picked
         WHERE t.bigint_fk_source = picked.bigint_fk_source
        RETURNING t.bigint_fk_source, t.integer_consecutive_failures,
                  t.text_config_hash
    """, {"owner": owner or owner_name(), "lease": lease_seconds, "limit": limit})
    conn.commit()
    return [DueTarget(source_id=row["bigint_fk_source"],
                      consecutive_failures=row["integer_consecutive_failures"],
                      config_hash=row["text_config_hash"]) for row in rows]


def next_run_seconds(interval_minutes: int, *, failures: int, failed: bool) -> float:
    """When this target is due again.

    Normally the interval plus a little jitter, so that ten sources saved in
    the same minute do not stay in lockstep forever. After failures with a
    growing distance: a site that has been dead for hours will not be alive in
    the next minute, and asking more often does not make it so.

    >>> next_run_seconds(60, failures=0, failed=True) / 60      # once: doubled
    120.0
    >>> next_run_seconds(60, failures=3, failed=True) / 60      # 60 * 2^4
    960.0
    >>> next_run_seconds(60, failures=9, failed=True) / 60      # the cap holds
    960.0
    >>> 3000 <= next_run_seconds(60, failures=0, failed=False) <= 4200
    True

    Never below a minute - a date in the past would fire again on the next
    tick and turn a fast source into a busy loop.

    >>> next_run_seconds(0, failures=0, failed=False)
    60.0
    """
    if failed:
        minutes = interval_minutes * (2 ** min(failures + 1, MAX_BACKOFF_DOUBLINGS))
    else:
        # Up to a tenth either way. Enough to break lockstep, not enough to
        # make "every six hours" mean anything else.
        minutes = interval_minutes * random.uniform(0.9, 1.1)
    return max(60.0, minutes * 60.0)


def release(conn: psycopg.Connection, source_id: int, *, failed: bool,
            delay_seconds: float, error: str = "") -> None:
    """Give the lease back and set the next date."""
    db.execute(conn, """
        UPDATE scraper.targets
           SET bool_leased      = false,
               text_lease_owner = '',
               date_lease_until = NULL,
               date_last_run    = NOW(),
               date_next_run    = NOW() + make_interval(secs => %(delay)s),
               integer_consecutive_failures = CASE WHEN %(failed)s
                    THEN integer_consecutive_failures + 1 ELSE 0 END,
               text_last_error  = %(error)s,
               date_updated     = NOW()
         WHERE bigint_fk_source = %(id)s
    """, {"id": source_id, "delay": delay_seconds, "failed": failed,
          "error": (error[:2000] or None)})
    conn.commit()


def record_robots(conn: psycopg.Connection, source_id: int, *, allowed: bool,
                  reason: str, crawl_delay: float | None) -> None:
    """Keep the robots verdict on the target row.

    It is recorded, not enforced, here: blocking happens in `crawlkit.crawl`,
    at the moment of fetching. Two places that may block are two places to look
    for a bug, and the second one is the one nobody checks. What this row is
    for is the pill on the overview and the refusal to enable a source whose
    list page is forbidden - both of which have to work without crawling first.
    """
    db.execute(conn, """
        UPDATE scraper.targets
           SET bool_robots_allowed      = %(ok)s,
               text_robots_reason       = %(reason)s,
               float_robots_crawl_delay = %(delay)s,
               date_robots_checked      = NOW(),
               date_updated             = NOW()
         WHERE bigint_fk_source = %(id)s
    """, {"id": source_id, "ok": allowed, "reason": (reason or "")[:500],
          "delay": crawl_delay})
    conn.commit()


def reap_expired_leases(conn: psycopg.Connection) -> int:
    """Free orphaned leases.

    `claim_due` already ignores expired leases, but without this the dashboard
    would show a crashed worker's source as "running" forever. That is the
    difference between a system that recovers and one that looks stuck.
    """
    freed = db.execute(conn, """
        UPDATE scraper.targets
           SET bool_leased = false, text_lease_owner = '',
               date_lease_until = NULL, date_updated = NOW()
         WHERE bool_leased AND date_lease_until < NOW()
    """)
    conn.commit()
    return freed


class Scheduler:
    """The pace. Claims what is due and lets it run, `concurrency` at a time."""

    def __init__(self, dsn: str, *, concurrency: int, lease_seconds: int,
                 stopping: Callable[[], bool]) -> None:
        self.dsn = dsn
        self.concurrency = concurrency
        self.lease_seconds = lease_seconds
        self.stopping = stopping
        self.throttle = HostThrottle()
        self._pool = ThreadPoolExecutor(max_workers=concurrency,
                                        thread_name_prefix="crawl")
        self._inflight: set[int] = set()
        self._guard = threading.Lock()

    @property
    def free_slots(self) -> int:
        with self._guard:
            return max(0, self.concurrency - len(self._inflight))

    def tick(self, run_target: Callable[[DueTarget], None]) -> int:
        """One pass: claim as much as there is room for.

        Only as many as there are free slots. Claiming more would lease sources
        without working on them - and a leased source is invisible to a second
        scheduler.
        """
        slots = self.free_slots
        if slots <= 0:
            return 0
        with db.connect(self.dsn) as conn:
            due = claim_due(conn, limit=slots, lease_seconds=self.lease_seconds)
        for target in due:
            with self._guard:
                self._inflight.add(target.source_id)
            self._pool.submit(self._wrap, target, run_target)
        return len(due)

    def _wrap(self, target: DueTarget, run_target: Callable[[DueTarget], None]) -> None:
        try:
            run_target(target)
        except Exception:  # noqa: BLE001
            # The caller handles its own errors and releases the lease. What
            # arrives here is a fault in the frame itself - and it must not
            # take the pool thread with it, or the concurrency shrinks by one
            # with every incident.
            log.exception("unhandled error in watched page %s", target.source_id)
            try:
                with db.connect(self.dsn) as conn:
                    release(conn, target.source_id, failed=True,
                            delay_seconds=next_run_seconds(
                                60, failures=target.consecutive_failures, failed=True),
                            error="unhandled error in the scheduler frame")
            except Exception:  # noqa: BLE001
                log.exception("could not release the lease of watched page %s",
                              target.source_id)
        finally:
            with self._guard:
                self._inflight.discard(target.source_id)

    def wait_idle(self, timeout: float = 60.0) -> None:
        deadline = time.monotonic() + timeout
        while self._inflight and time.monotonic() < deadline:
            time.sleep(0.5)

    def shutdown(self) -> None:
        # wait=True: a source in flight is finished. Cutting it off would leave
        # its lease standing, and the next instance would have to wait for it
        # to expire.
        self._pool.shutdown(wait=True, cancel_futures=True)
