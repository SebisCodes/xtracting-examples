"""The crawler's log: what went wrong, newest first.

    GET /api/logs/errors?range&source_id&kind&severity&q&page  -> the log
    GET /api/logs/runs?range&source_id&status&q&page           -> the history
    GET /api/logs/service?service&range&source_id&q&page       -> the steps
    GET /api/logs/kinds?range                                  -> the filters
    GET /api/logs/summary?hours=24                             -> one number

`monitoring.scraper_runs` says THAT a run was SKIPPED, BLOCKED or ERROR;
`monitoring.scraper_errors` says WHAT went wrong, in one sentence a person
who configured a source in this browser can act on. The Log view shows the
second and offers the first as a second tab, because "was there a run at
all?" and "what did it say?" are two different questions and the answer to
the first is often "no run since Tuesday".

AND `monitoring.service_log` ANSWERS A THIRD: what is it doing right now.
Those rows are written only while a switch in `dashboard.settings` is on,
they are kept for twenty-four hours rather than ninety days, and there are
two orders of magnitude more of them - which is why they are a table and a
tab of their own rather than a severity in the first one.

NO PROJECT AND LANGUAGE HERE, for the same reason as in api_sources.py: a
crawl belongs to a source and an API key label, not to a project - and most
rows are written before any task exists, so the three identity columns are
empty by design. Filtering the log by the project a reader happens to be
viewing would show an empty log to somebody whose crawler is on fire. The
pair is still accepted (every page sends it) and ignored.

EVERY LISTING IS BOUNDED BY THE TIME RANGE, and that is not cosmetic: both
tables are hypertables partitioned by `date_added`, so `>= now() - interval`
is what lets the database skip the chunks it does not need. "All" exists
because a customer does look for the one rejection from last month, and it
is the one reading that has to walk the whole table - the LIMIT keeps it
cheap, the index keeps it ordered.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from ..db import Database, get_db

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/logs", tags=["logs"])

PAGE_SIZE = 50
MAX_PAGE_SIZE = 500

#: The time ranges the view offers, as PostgreSQL intervals. "all" drops the
#: bound - see the module docstring for what that costs.
RANGES: dict[str, str | None] = {
    "hour": "1 hour",
    "day": "1 day",
    "week": "7 days",
    "month": "30 days",
    "all": None,
}
DEFAULT_RANGE = "day"

#: The kinds, in the order the filter shows them: the site's rules, the
#: site's answer, a file, the platform, us. Mirrors the CHECK in
#: database/init/03-scraper.sql and crawler/app/errorlog.py - the words here
#: are the ones a person reads, the constant there is the one written.
KIND_LABELS: dict[str, str] = {
    "ROBOTS_FORBIDDEN": "robots.txt forbids this address",
    "ROBOTS_UNREADABLE": "robots.txt could not be read",
    "HTTP_4XX": "the site refused the request",
    "HTTP_5XX": "the site has a problem of its own",
    "CHALLENGE": "the site asked for a browser check",
    "TIMEOUT": "the site did not answer in time",
    "NETWORK": "the site could not be reached",
    "PARSE": "the page could not be read",
    "NO_LINKS": "no links found on the list page",
    "FILE_TOO_LARGE": "a file is larger than its limit",
    "FILE_WRONG_TYPE": "a file is not what its address claims",
    "FILE_NO_TEXT": "a file has no text to send",
    "SUBMIT_REJECTED": "a document was refused",
    "SUBMIT_BACKPRESSURE": "the platform asked for a pause",
    "KEY_INVALID": "the API key was refused",
    "CONFIG": "the configuration cannot be used",
    "INTERNAL": "an unexpected error in the crawler",
}

SEVERITY_LABELS = {"error": "Error", "warning": "Warning", "info": "Test"}

#: One filter value that is not a severity in the table: everything that is
#: NOT a test. The Watched pages overview links here with "N problems in the last
#: 24 hours", and that N comes from /summary, which counts `text_severity <>
#: 'info'` - a link whose page shows a different number than the link did is
#: a link nobody trusts again. So the same reading has a name here.
PROBLEM_SEVERITY = "problem"
PROBLEM_LABEL = "Error or warning (no tests)"

#: `text_status` of monitoring.scraper_runs, in words.
RUN_STATUS_LABELS = {
    "OK": "Crawled",
    "SKIPPED": "Skipped (robots.txt)",
    "BLOCKED": "Blocked by the site",
    "BACKPRESSURE": "Paused by the platform",
    "ERROR": "Error",
    "TEST": "Test",
}

_ERROR_COLUMNS = """
    e.date_added, e.bigint_id, e.text_name, e.text_kind, e.text_severity,
    e.bigint_fk_source, e.text_source_name, e.text_host, e.text_uri,
    e.integer_http_status, e.text_message, e.text_detail, e.text_run_id,
    e.text_project, e.text_task_id
"""

_RUN_COLUMNS = """
    r.date_added, r.text_name, r.text_status, r.text_key_label, r.text_message,
    r.bigint_fk_source, r.integer_pages, r.integer_links, r.integer_accepted,
    r.integer_new, r.integer_files, r.integer_submitted, r.integer_ms
"""

_SERVICE_COLUMNS = """
    s.date_added, s.bigint_id, s.text_service, s.text_action, s.text_detail,
    s.text_run_id, s.bigint_fk_source, s.text_project, s.integer_ms
"""

#: The two halves of `monitoring.service_log`, and the `dashboard.settings`
#: row that fills each. Mirrors the CHECK in database/init/03-scraper.sql and
#: the SERVICE constant of crawler/app/servicelog.py and its collector twin.
#: The setting travels in the answer so the view can name it over an empty
#: panel without keeping a third copy of the string.
SWITCHES: dict[str, str] = {
    "crawler": "crawler.debug",
    "collector": "collector.debug",
}
SERVICES = tuple(SWITCHES)


def _bad(error: str, hint: str) -> HTTPException:
    return HTTPException(400, {"error": error, "hint": hint})


def _range(value: str) -> tuple[str, str | None]:
    """The named range, or a 400 that lists the names.

    A wrong name is not answered with a default: a bookmarked "?range=year"
    would then quietly show a day and the reader would believe it.
    """
    name = (value or DEFAULT_RANGE).strip().lower()
    if name not in RANGES:
        raise _bad(f"there is no time range {value!r}",
                   "one of " + ", ".join(RANGES))
    return name, RANGES[name]


def _present(conn, name: str) -> bool:
    row = conn.execute("SELECT to_regclass(%s) IS NOT NULL AS present",
                       (name,)).fetchone()
    return bool(row and row["present"])


def _missing_answer(table: str) -> dict[str, Any]:
    """A dashboard on an archive without the crawler tables.

    Not an error: nothing is broken, the archive simply predates
    03-scraper.sql. The view says so and stays usable.
    """
    return {
        "available": False,
        "items": [], "page": 0, "page_size": PAGE_SIZE, "total": 0, "pages": 0,
        "hint": (f"This archive has no {table} table yet. Load "
                 f"database/init/03-scraper.sql into it - it only adds the "
                 f"crawler's tables and changes nothing that is already there."),
    }


def _page_size(value: int) -> int:
    return max(1, min(int(value or PAGE_SIZE), MAX_PAGE_SIZE))


def _like(q: str) -> str:
    """A substring pattern with the wildcards a person typed escaped.

    Somebody searching for a URL with a `_` in it is searching for that
    address, not for "any character".
    """
    cleaned = q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{cleaned}%"


def _iso(value: Any) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else None


def _live_sources(conn, ids: Any) -> set[int]:
    """Which of these watched pages are still configured.

    THE LOG OUTLIVES WHAT IT IS ABOUT. A row stays for the ninety days the
    retention policy keeps it; a watched page is deleted in a second. Until
    the view knew the difference it linked every name it printed, and the
    link to a page that is gone opened a full editor under a 404 - a dead
    end with a Save button on it. One lookup per page of rows, with at most
    `page_size` ids in it, buys the view the word "(deleted)" instead.

    An archive without scraper_config.sources cannot be asked, so every id
    is taken to exist: the old behaviour, which is the right one when the
    question cannot be answered.
    """
    wanted = {int(value) for value in ids if value}
    if not wanted:
        return set()
    if not _present(conn, "scraper_config.sources"):
        return wanted
    rows = conn.execute(
        "SELECT bigint_id FROM scraper_config.sources WHERE bigint_id = ANY(%s)",
        (sorted(wanted),)).fetchall()
    return {int(row["bigint_id"]) for row in rows}


def _named_sources(conn, ids) -> tuple[set[int], dict[int, str]]:
    """Which of these watched pages still exist, and what they are called.

    THE STEP LOG DENORMALISES NOTHING. A crawl's error row carries the name
    of the page it is about, because it is written once per accident; a step
    row is written on a hot path several times per page, and a second lookup
    per step would cost more than the row is worth. So the name is fetched
    once per page of rows instead - at most `page_size` ids in one statement,
    the way `_live_sources()` fetches existence for the other two tabs.

    An archive without scraper_config.sources cannot be asked, so every id is
    taken to exist and none has a name: the view then prints the id, which is
    the truth it has.
    """
    wanted = {int(value) for value in ids if value}
    if not wanted:
        return set(), {}
    if not _present(conn, "scraper_config.sources"):
        return wanted, {}
    rows = conn.execute(
        "SELECT bigint_id, text_name FROM scraper_config.sources "
        "WHERE bigint_id = ANY(%s)", (sorted(wanted),)).fetchall()
    names = {int(row["bigint_id"]): row["text_name"] or "" for row in rows}
    return set(names), names


# ── The log ─────────────────────────────────────────────────────────────


@router.get("/errors")
def errors(db: Database = Depends(get_db),
           range: str = Query(DEFAULT_RANGE, description="hour|day|week|month|all"),
           source_id: int | None = Query(None, ge=1),
           kind: str = Query("", description="one of the kinds, or empty for all"),
           severity: str = Query("", description="error|warning|info|problem (= not a test)"),
           q: str = Query("", description="free text over message, host, address, watched page"),
           page: int = Query(0, ge=0, le=10000),
           page_size: int = Query(PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE)):
    name, interval = _range(range)
    if kind and kind not in KIND_LABELS:
        raise _bad(f"there is no error kind {kind!r}",
                   "leave it out for all of them, or use one of: "
                   + ", ".join(KIND_LABELS))
    if severity and severity not in SEVERITY_LABELS and severity != PROBLEM_SEVERITY:
        raise _bad(f"there is no severity {severity!r}",
                   "one of " + ", ".join([*SEVERITY_LABELS, PROBLEM_SEVERITY]))

    size = _page_size(page_size)
    params: dict[str, Any] = {"limit": size, "offset": page * size}
    # Every fragment below is a literal of this module; only values are
    # parameters. The list is assembled rather than nested so that each
    # filter can be read on its own line.
    where = ["TRUE"]
    if interval:
        where.append("e.date_added >= now() - %(since)s::interval")
        params["since"] = interval
    if source_id:
        where.append("e.bigint_fk_source = %(source)s")
        params["source"] = source_id
    if kind:
        where.append("e.text_kind = %(kind)s")
        params["kind"] = kind
    if severity == PROBLEM_SEVERITY:
        # Not a column value: the reading the Watched pages overview counts.
        where.append("e.text_severity <> 'info'")
    elif severity:
        where.append("e.text_severity = %(severity)s")
        params["severity"] = severity
    if q.strip():
        where.append("(e.text_message ILIKE %(q)s OR e.text_host ILIKE %(q)s "
                     "OR e.text_uri ILIKE %(q)s OR e.text_source_name ILIKE %(q)s "
                     "OR e.text_detail ILIKE %(q)s)")
        params["q"] = _like(q)

    statement = f"""
        SELECT {_ERROR_COLUMNS}, count(*) OVER () AS total
          FROM monitoring.scraper_errors e
         WHERE {' AND '.join(where)}
         ORDER BY e.date_added DESC, e.bigint_id DESC
         LIMIT %(limit)s OFFSET %(offset)s
    """

    with db.read() as conn:
        if not _present(conn, "monitoring.scraper_errors"):
            return {**_missing_answer("monitoring.scraper_errors"),
                    "range": name, "q": q, "kind": kind, "severity": severity,
                    "source_id": source_id}
        rows = conn.execute(statement, params).fetchall()
        if not rows and page:
            # A page past the end carries no count(*) OVER(), and a total of
            # zero would make the view say "nothing went wrong" about a log
            # with four hundred rows in it.
            counted = conn.execute(statement, {**params, "offset": 0, "limit": 1}).fetchall()
        else:
            counted = rows
        live = _live_sources(conn, (row["bigint_fk_source"] for row in rows))

    total = int(counted[0]["total"]) if counted else 0
    return {
        "available": True,
        "items": [_error_item(row, live) for row in rows],
        "page": page, "page_size": size, "total": total,
        "pages": (total + size - 1) // size,
        "range": name, "q": q, "kind": kind, "severity": severity,
        "source_id": source_id,
    }


def _error_item(row: dict, live: set[int]) -> dict[str, Any]:
    kind = row["text_kind"]
    source_id = row["bigint_fk_source"]
    return {
        "id": f"{_iso(row['date_added'])}:{row['bigint_id']}",
        "date": _iso(row["date_added"]),
        "kind": kind,
        "kind_label": KIND_LABELS.get(kind, row["text_name"] or kind),
        "severity": row["text_severity"],
        "severity_label": SEVERITY_LABELS.get(row["text_severity"], row["text_severity"]),
        # `exists` is what decides whether the view offers a link to the
        # editor or prints the name and says the page is gone.
        "source": {"id": source_id, "name": row["text_source_name"] or "",
                   "exists": source_id is None or int(source_id) in live},
        "host": row["text_host"] or "",
        "uri": row["text_uri"] or "",
        "http_status": row["integer_http_status"],
        "message": row["text_message"] or "",
        "detail": row["text_detail"] or "",
        "run_id": row["text_run_id"] or "",
        # Empty for almost every row, and that is correct - see the module
        # docstring. Carried so a row written about an archived task can be
        # followed into the archive.
        "project": row["text_project"] or "",
        "task_id": row["text_task_id"] or "",
    }


# ── The run history ─────────────────────────────────────────────────────


@router.get("/runs")
def runs(db: Database = Depends(get_db),
         range: str = Query(DEFAULT_RANGE, description="hour|day|week|month|all"),
         source_id: int | None = Query(None, ge=1),
         status: str = Query("", description="OK|SKIPPED|BLOCKED|BACKPRESSURE|ERROR|TEST"),
         q: str = Query("", description="free text over the name and the message"),
         page: int = Query(0, ge=0, le=10000),
         page_size: int = Query(PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE)):
    name, interval = _range(range)
    if status and status not in RUN_STATUS_LABELS:
        raise _bad(f"there is no run status {status!r}",
                   "one of " + ", ".join(RUN_STATUS_LABELS))

    size = _page_size(page_size)
    params: dict[str, Any] = {"limit": size, "offset": page * size}
    where = ["TRUE"]
    if interval:
        where.append("r.date_added >= now() - %(since)s::interval")
        params["since"] = interval
    if source_id:
        where.append("r.bigint_fk_source = %(source)s")
        params["source"] = source_id
    if status:
        where.append("r.text_status = %(status)s")
        params["status"] = status
    if q.strip():
        where.append("(r.text_name ILIKE %(q)s OR r.text_message ILIKE %(q)s "
                     "OR r.text_key_label ILIKE %(q)s)")
        params["q"] = _like(q)

    statement = f"""
        SELECT {_RUN_COLUMNS}, count(*) OVER () AS total
          FROM monitoring.scraper_runs r
         WHERE {' AND '.join(where)}
         ORDER BY r.date_added DESC, r.text_name DESC
         LIMIT %(limit)s OFFSET %(offset)s
    """

    with db.read() as conn:
        if not _present(conn, "monitoring.scraper_runs"):
            return {**_missing_answer("monitoring.scraper_runs"),
                    "range": name, "q": q, "status": status, "source_id": source_id}
        rows = conn.execute(statement, params).fetchall()
        counted = rows if (rows or not page) else conn.execute(
            statement, {**params, "offset": 0, "limit": 1}).fetchall()
        live = _live_sources(conn, (row["bigint_fk_source"] for row in rows))

    total = int(counted[0]["total"]) if counted else 0
    return {
        "available": True,
        "items": [{
            "date": _iso(row["date_added"]),
            "name": row["text_name"] or "",
            "status": row["text_status"],
            "status_label": RUN_STATUS_LABELS.get(row["text_status"], row["text_status"]),
            "key_label": row["text_key_label"] or "",
            "message": row["text_message"] or "",
            "source": {"id": row["bigint_fk_source"], "name": "",
                       "exists": row["bigint_fk_source"] is None
                                 or int(row["bigint_fk_source"]) in live},
            "counts": {
                "pages": row["integer_pages"], "links": row["integer_links"],
                "accepted": row["integer_accepted"], "new": row["integer_new"],
                "files": row["integer_files"], "submitted": row["integer_submitted"],
            },
            "ms": row["integer_ms"],
        } for row in rows],
        "page": page, "page_size": size, "total": total,
        "pages": (total + size - 1) // size,
        "range": name, "q": q, "status": status, "source_id": source_id,
    }


# ── What each service is doing, step by step ────────────────────────────


@router.get("/service")
def service_steps(db: Database = Depends(get_db),
                  service: str = Query(..., description="crawler|collector"),
                  range: str = Query(DEFAULT_RANGE, description="hour|day|week|month|all"),
                  source_id: int | None = Query(None, ge=1),
                  q: str = Query("", description="free text over the step, its detail and the run"),
                  page: int = Query(0, ge=0, le=10000),
                  page_size: int = Query(PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE)):
    """One row per step, newest first - and usually nothing at all.

    ONE ENDPOINT FOR BOTH SERVICES, chosen by `service`, because they are one
    table with one shape: the crawler and the collector write the same seven
    columns and the reader asks the same four questions of them. Two routes
    would be two places to fix a paging bug.

    THE STREAM IS NORMALLY EMPTY, and that is not a fault. `SWITCHES` names
    the `dashboard.settings` row that fills it; the view prints that sentence
    over an empty panel, because an empty table with no explanation is the
    worst answer this tab can give. The switch is not read here - the tab is
    about what WAS written, and a switch turned on two minutes ago has
    nothing behind it yet either way.
    """
    if service not in SERVICES:
        raise _bad(f"there is no service {service!r}",
                   "one of " + ", ".join(SERVICES))
    name, interval = _range(range)

    size = _page_size(page_size)
    params: dict[str, Any] = {"limit": size, "offset": page * size,
                              "service": service}
    where = ["s.text_service = %(service)s"]
    if interval:
        where.append("s.date_added >= now() - %(since)s::interval")
        params["since"] = interval
    if source_id:
        where.append("s.bigint_fk_source = %(source)s")
        params["source"] = source_id
    if q.strip():
        where.append("(s.text_action ILIKE %(q)s OR s.text_detail ILIKE %(q)s "
                     "OR s.text_run_id ILIKE %(q)s OR s.text_project ILIKE %(q)s)")
        params["q"] = _like(q)

    statement = f"""
        SELECT {_SERVICE_COLUMNS}, count(*) OVER () AS total
          FROM monitoring.service_log s
         WHERE {' AND '.join(where)}
         ORDER BY s.date_added DESC, s.bigint_id DESC
         LIMIT %(limit)s OFFSET %(offset)s
    """

    with db.read() as conn:
        if not _present(conn, "monitoring.service_log"):
            return {**_missing_answer("monitoring.service_log"),
                    "service": service, "setting": SWITCHES[service],
                    "range": name, "q": q, "source_id": source_id}
        rows = conn.execute(statement, params).fetchall()
        # A page past the end carries no count(*) OVER (), and a total of zero
        # would make the view say "nothing has been written" about a stream
        # with four hundred rows in it.
        counted = rows if (rows or not page) else conn.execute(
            statement, {**params, "offset": 0, "limit": 1}).fetchall()
        live, names = _named_sources(conn, (row["bigint_fk_source"] for row in rows))

    total = int(counted[0]["total"]) if counted else 0
    return {
        "available": True,
        "items": [_service_item(row, live, names) for row in rows],
        "page": page, "page_size": size, "total": total,
        "pages": (total + size - 1) // size,
        # Carried so the empty panel can name the switch without the view
        # keeping a second copy of these two strings.
        "service": service, "setting": SWITCHES[service],
        "range": name, "q": q, "source_id": source_id,
    }


def _service_item(row: dict, live: set[int], names: dict[int, str]) -> dict[str, Any]:
    source_id = row["bigint_fk_source"]
    return {
        "id": f"{_iso(row['date_added'])}:{row['bigint_id']}",
        "date": _iso(row["date_added"]),
        "service": row["text_service"],
        "action": row["text_action"] or "",
        "detail": row["text_detail"] or "",
        "run_id": row["text_run_id"] or "",
        # Same shape as the other two tabs, so the view has one way of
        # printing a watched page and one rule for the one that is gone.
        "source": {"id": source_id,
                   "name": names.get(int(source_id)) if source_id else "",
                   "exists": source_id is None or int(source_id) in live},
        "project": row["text_project"] or "",
        "ms": row["integer_ms"],
    }


# ── What the filters offer ──────────────────────────────────────────────


@router.get("/kinds")
def kinds(db: Database = Depends(get_db),
          range: str = Query(DEFAULT_RANGE, description="hour|day|week|month|all")):
    """The filter lists, with the counts of the range that is shown.

    Counted rather than listed from the constants above: a filter offering
    sixteen kinds of which two ever occur is a filter nobody uses. The
    kinds that ARE in the range come first, with their number; the rest are
    still offered, so a customer can ask "was there ever a 403?" and get a
    truthful empty answer.
    """
    name, interval = _range(range)
    params: dict[str, Any] = {}
    bound = "TRUE"
    if interval:
        bound = "date_added >= now() - %(since)s::interval"
        params["since"] = interval

    with db.read() as conn:
        if not _present(conn, "monitoring.scraper_errors"):
            return {"available": False, "range": name, "kinds": [], "severities": [],
                    "sources": [], "run_statuses": [],
                    "hint": _missing_answer("monitoring.scraper_errors")["hint"]}

        by_kind = {r["text_kind"]: r["n"] for r in conn.execute(
            f"SELECT text_kind, count(*) AS n FROM monitoring.scraper_errors "
            f"WHERE {bound} GROUP BY text_kind", params).fetchall()}
        by_severity = {r["text_severity"]: r["n"] for r in conn.execute(
            f"SELECT text_severity, count(*) AS n FROM monitoring.scraper_errors "
            f"WHERE {bound} GROUP BY text_severity", params).fetchall()}
        # THE SOURCE LIST COMES FROM BOTH TABLES, and that is the whole
        # point of it: counted over monitoring.scraper_errors alone, a
        # source that ran fine would not be in the filter at all, and two
        # readings would be impossible. "Nothing went wrong all week -
        # but did MY source run?" could not be asked of the Runs tab,
        # because the Source select was empty exactly when nothing had gone
        # wrong; and a source with runs and no errors could not be picked on
        # either tab, in the case a customer opens this view for most often.
        # The union is per source, the name is taken from whichever table
        # carries one (both denormalise it), and the two counts are kept
        # apart so the select can show the number of the tab that is open
        # instead of one mixed total nobody can act on.
        has_runs = _present(conn, "monitoring.scraper_runs")
        run_side = f"""
            UNION ALL
            SELECT r.bigint_fk_source AS id, r.text_name AS name, 0 AS problems, 1 AS runs
              FROM monitoring.scraper_runs r
             WHERE {bound} AND r.bigint_fk_source IS NOT NULL
        """ if has_runs else ""
        sources = conn.execute(f"""
            WITH mentioned AS (
                SELECT e.bigint_fk_source AS id, e.text_source_name AS name,
                       1 AS problems, 0 AS runs
                  FROM monitoring.scraper_errors e
                 WHERE {bound} AND e.bigint_fk_source IS NOT NULL
                {run_side}
            )
            SELECT id,
                   max(nullif(name, '')) AS name,
                   sum(problems) AS problems,
                   sum(runs)     AS runs
              FROM mentioned
             GROUP BY id
             ORDER BY sum(problems) + sum(runs) DESC, 2
             LIMIT 200
        """, params).fetchall()
        run_statuses = {}
        if has_runs:
            run_statuses = {r["text_status"]: r["n"] for r in conn.execute(
                f"SELECT text_status, count(*) AS n FROM monitoring.scraper_runs "
                f"WHERE {bound} GROUP BY text_status", params).fetchall()}

    return {
        "available": True,
        "range": name,
        "kinds": sorted(
            ({"value": value, "label": label, "count": int(by_kind.get(value, 0))}
             for value, label in KIND_LABELS.items()),
            key=lambda item: (-item["count"], item["label"])),
        "severities": [
            # The combined reading first: it is the one the Watched pages overview
            # links with, so a reader who followed that link finds the filter
            # that reproduces its number at the top of the list.
            {"value": PROBLEM_SEVERITY, "label": PROBLEM_LABEL,
             "count": int(sum(n for value, n in by_severity.items() if value != "info"))},
            *({"value": value, "label": label,
               "count": int(by_severity.get(value, 0))}
              for value, label in SEVERITY_LABELS.items()),
        ],
        # `count` stays the number of PROBLEMS, because that is what it has
        # always been and what the Problems tab shows; `runs` is the second
        # number, and the view picks the one that belongs to the open tab.
        "sources": [{"id": row["id"], "name": row["name"] or f"watched page {row['id']}",
                     "count": int(row["problems"]),
                     "problems": int(row["problems"]),
                     "runs": int(row["runs"])} for row in sources],
        "run_statuses": [{"value": value, "label": label,
                          "count": int(run_statuses.get(value, 0))}
                         for value, label in RUN_STATUS_LABELS.items()],
    }


@router.get("/summary")
def summary(db: Database = Depends(get_db),
            hours: int = Query(24, ge=1, le=8760),
            source_id: int | None = Query(None, ge=1)):
    """How much went wrong lately - one number, for a link somewhere else.

    The Watched pages overview shows "N problems in the last 24 hours" next to a
    link to this view, and a page that already makes six queries should not
    have to fetch a hundred rows to count them. Tests count as neither: they
    are somebody trying a configuration out, not something that went wrong.
    """
    params = {"hours": hours}
    where = ["date_added >= now() - make_interval(hours => %(hours)s)",
             "text_severity <> 'info'"]
    if source_id:
        where.append("bigint_fk_source = %(source)s")
        params["source"] = source_id

    with db.read() as conn:
        if not _present(conn, "monitoring.scraper_errors"):
            return {"available": False, "hours": hours, "problems": 0,
                    "errors": 0, "warnings": 0, "newest": None, "source_id": source_id}
        row = conn.execute(f"""
            SELECT count(*) AS problems,
                   count(*) FILTER (WHERE text_severity = 'error')   AS errors,
                   count(*) FILTER (WHERE text_severity = 'warning') AS warnings,
                   max(date_added) AS newest
              FROM monitoring.scraper_errors
             WHERE {' AND '.join(where)}
        """, params).fetchone()

    return {
        "available": True, "hours": hours, "source_id": source_id,
        "problems": int(row["problems"]), "errors": int(row["errors"]),
        "warnings": int(row["warnings"]), "newest": _iso(row["newest"]),
    }
