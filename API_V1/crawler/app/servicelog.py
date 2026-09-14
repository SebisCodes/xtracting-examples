"""One row per step the crawler took, while somebody is watching.

`monitoring.scraper_errors` answers "what went wrong" and keeps its rows for
ninety days, because the six that matter out of a quiet week have to still be
there next month. This module writes into `monitoring.service_log`, which
answers a different question - "what is it doing right now" - and keeps its
rows for twenty-four hours, because none of them is interesting tomorrow.
Two questions, two lifetimes, two tables.

OFF BY DEFAULT, AND READ ONCE PER PASS. `enabled()` reads the switch out of
`dashboard.settings`; the loop asks it on the tick where it already asks
`db.paused()` and keeps the answer for that tick. So switching it on takes
effect on the NEXT pass, which is the promise the dashboard makes - and a
tick either writes its steps whole or writes none of them, instead of
starting halfway through and leaving the reader to wonder about the first
half.

`record()` NEVER RAISES, for the same reason `errorlog.record()` does not: a
crawl that ran for four minutes must not be lost because a debug row could
not be written. It returns a bool so the tests - and a caller that cares -
can tell.

WHY A SAVEPOINT. The helper is handed the caller's connection in the middle
of the caller's transaction: the queue row, the run row and the lease are on
it. `conn.transaction()` opens a SAVEPOINT when a transaction is already
running, so a failing INSERT here rolls back to the savepoint and leaves the
caller's real work intact. A bare rollback would throw away the very thing
the step was describing - a debug log that eats the work it is watching.
"""

from __future__ import annotations

import logging
from typing import Any

import psycopg

log = logging.getLogger(__name__)

#: Which half of `monitoring.service_log` this module writes. Mirrors the
#: CHECK in database/init/03-scraper.sql; the collector has its own module
#: with the other value, because the two run as separate containers and
#: neither can import the other's package.
SERVICE = "crawler"

#: The `dashboard.settings` key that turns the stream on.
SETTING = "crawler.debug"

#: Three or four words, so the column can be scanned rather than read.
#: Anything longer than this is a detail wearing an action's clothes.
ACTION_LIMIT = 200
#: One line of particulars - which page, which key, how many. Not a dump:
#: what needs a dump is an error, and errors have their own table.
DETAIL_LIMIT = 2000
#: The same limit `errorlog` gives `text_run_id`, because it is the same id.
RUN_LIMIT = 100

_INSERT = """
    INSERT INTO monitoring.service_log
          (text_service, text_action, text_detail, text_run_id,
           bigint_fk_source, text_project, integer_ms)
    VALUES (%(service)s, %(action)s, %(detail)s, %(run)s, %(source)s,
            %(project)s, %(ms)s)
"""

_SWITCH = """
    SELECT text_value FROM dashboard.settings WHERE text_key = %s
"""

_TRUE = ("1", "true", "yes", "on")


def enabled(conn: psycopg.Connection) -> bool:
    """Is step logging switched on for this service?

    The same defensive shape as `db.paused()`, and for the same reason: the
    crawler must run against an archive that has no `dashboard` schema at
    all. A missing table, a missing row or a database that just went away
    all mean "off" - the careful answer, because off is the state that
    writes nothing.
    """
    try:
        with conn.cursor() as cur:
            cur.execute(_SWITCH, (SETTING,))
            row = cur.fetchone()
    except psycopg.Error:
        conn.rollback()
        return False
    if not row:
        return False
    value = row["text_value"] if isinstance(row, dict) else row[0]
    return str(value).strip().lower() in _TRUE


def record(conn: psycopg.Connection, *, action: str, detail: str = "",
           run_id: str = "", source_id: int | None = None, project: str = "",
           ms: int | None = None) -> bool:
    """Write one step. Returns whether it was written; never raises.

    Every field but `action` is empty by default and that is correct: a step
    the loop takes before it has claimed anything belongs to no watched page,
    no project and no run, and inventing ids for it would make the log
    harder to read rather than fuller.
    """
    if conn is None:
        return False

    text = str(action or "").strip()[:ACTION_LIMIT]
    if not text:
        # A row with no action is a row nobody can scan, and the column is
        # NOT NULL besides. Dropping it costs a debug line; writing it would
        # cost the reader a blank row in the middle of a story.
        return False

    params: dict[str, Any] = {
        "service": SERVICE,
        "action": text,
        "detail": str(detail or "").strip()[:DETAIL_LIMIT],
        "run": (run_id or "")[:RUN_LIMIT],
        "source": source_id,
        "project": project or "",
        "ms": None if ms is None else int(ms),
    }

    try:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(_INSERT, params)
        return True
    except Exception:  # noqa: BLE001 - a log that can fail a crawl is worse than no log
        log.warning("the step log could not be written (%s)", text[:120],
                    exc_info=True)
        return False


__all__ = ["ACTION_LIMIT", "DETAIL_LIMIT", "RUN_LIMIT", "SERVICE", "SETTING",
           "enabled", "record"]
