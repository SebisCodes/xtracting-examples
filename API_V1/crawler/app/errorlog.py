"""One row per thing that went wrong, written so a person can act on it.

`monitoring.scraper_runs` says THAT a run was SKIPPED, BLOCKED or ERROR.
This module writes WHAT went wrong, into `monitoring.scraper_errors`, and the
dashboard's Log view reads it newest first. The difference is not bookkeeping:

    SKIPPED
    robots.txt of https://www.example.ch forbids the list page of Flats in
    Zurich - nothing was fetched. Ask the site owner for permission, or
    watch a list page the rules allow.

The first is a status word, the second is an answer. Everything in here
exists to produce the second one, for somebody who configured a watched page in a
browser and does not read Python.

TWO HALVES, and they are separate on purpose:

  the sentences   `crawl_row()`, `file_row()`, `submit_row()` and
                  `key_row()` are pure functions from "what happened" to
                  {kind, severity, message, detail, uri, http_status}. No
                  database, no I/O, doctested - the wording is the product
                  here, so it is testable without an archive.
  the writing     `record()` puts one such row in the table. It is the only
                  thing in this repository that writes it.

`record()` NEVER RAISES. A crawl that ran for four minutes must not be lost
because the log table is missing, full, or on a database that just went away
- the failure is logged to the process log and the crawl carries on. That is
why it returns a bool instead of nothing: the tests assert on it, and a
caller that cares can too.

WHY A SAVEPOINT. The helper is handed the caller's connection, in the middle
of the caller's transaction (the run row and the lease are written on the
same one). `conn.transaction()` opens a SAVEPOINT when a transaction is
already running, so a failing INSERT here rolls back to the savepoint and
leaves the caller's work intact. A bare rollback would throw away the run row
as well - a log that eats its own evidence.
"""

from __future__ import annotations

import logging
from typing import Any

import psycopg

from crawlkit.words import plural

log = logging.getLogger(__name__)

#: Mirrors the CHECK in database/init/03-scraper.sql. A kind that is not on
#: this list is a mistake in the caller, not in the database.
KINDS = (
    "ROBOTS_FORBIDDEN", "ROBOTS_UNREADABLE", "HTTP_4XX", "HTTP_5XX", "CHALLENGE",
    "TIMEOUT", "NETWORK", "PARSE", "NO_LINKS", "FILE_TOO_LARGE",
    "FILE_WRONG_TYPE", "FILE_NO_TEXT", "SUBMIT_REJECTED", "SUBMIT_BACKPRESSURE",
    "KEY_INVALID", "CONFIG", "INTERNAL",
)

SEVERITIES = ("error", "warning", "info")

#: The headline of a row - `text_name`, the column every table in this
#: archive carries. Lower case and in the words of the log, not of the code:
#: it is printed next to the message, not instead of it.
LABELS = {
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

#: `text_detail` holds a traceback tail, an HTML error page or a robots.txt.
#: All three can be long, and none of them is worth more than this.
DETAIL_LIMIT = 4000
#: One sentence. Above this it stopped being one.
MESSAGE_LIMIT = 2000

_INSERT = """
    INSERT INTO monitoring.scraper_errors
          (text_name, text_project, text_language, text_task_id,
           bigint_fk_source, text_source_name, text_host, text_kind,
           text_severity, text_uri, integer_http_status, text_message,
           text_detail, text_run_id)
    VALUES (%(name)s, %(project)s, %(language)s, %(task)s, %(source)s,
            %(source_name)s, %(host)s, %(kind)s, %(severity)s, %(uri)s,
            %(status)s, %(message)s, %(detail)s, %(run)s)
"""


# ----------------------------------------------------------------------
# Writing
# ----------------------------------------------------------------------

def record(conn: psycopg.Connection, *, kind: str, severity: str, message: str,
           source_id: int | None = None, source_name: str = "", host: str = "",
           uri: str = "", http_status: int | None = None, detail: str = "",
           run_id: str = "", project: str = "", language: str = "",
           task_id: str = "") -> bool:
    """Write one log row. Returns whether it was written; never raises.

    The three identity columns are empty by default and that is correct: a
    crawl that never got a page belongs to no task and no project. They are
    filled where they are known, so a row about a document the archive
    already holds can name it.
    """
    if conn is None:
        return False

    clean_kind = kind if kind in KINDS else "INTERNAL"
    clean_severity = severity if severity in SEVERITIES else "error"
    text = str(message or "").strip()[:MESSAGE_LIMIT]
    extra = str(detail or "")[:DETAIL_LIMIT]
    if clean_kind != kind or clean_severity != severity:
        # Not dropped: a row with the wrong label is still evidence, and
        # losing it would hide the very thing that went wrong. The mistake
        # travels in the detail, where someone reading the log will see it.
        extra = (f"[written with kind={kind!r} severity={severity!r}, which the "
                 f"log does not know]\n{extra}")[:DETAIL_LIMIT]

    params: dict[str, Any] = {
        "name": LABELS.get(clean_kind, clean_kind)[:500],
        "project": project or "", "language": language or "",
        "task": task_id or "", "source": source_id,
        "source_name": (source_name or "")[:500], "host": (host or "")[:255],
        "kind": clean_kind, "severity": clean_severity, "uri": (uri or "")[:2000],
        "status": http_status, "message": text, "detail": extra,
        "run": (run_id or "")[:100],
    }

    try:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(_INSERT, params)
        return True
    except Exception:  # noqa: BLE001 - a log that can fail a crawl is worse than no log
        log.warning("the error log could not be written (%s: %s)", clean_kind,
                    text[:120], exc_info=True)
        return False


# ----------------------------------------------------------------------
# The sentences
# ----------------------------------------------------------------------

def crawl_row(result: Any, *, source_name: str = "", host: str = "",
              error: str = "", severity: str = "error") -> dict | None:
    """The one row a finished crawl leaves behind, or None when it went well.

    `result` is a `crawlkit.crawl.CrawlResult` or the dictionary a test
    snapshot stores - both carry `status`, `robots`, `fetch` and `warnings`,
    and this reads them the same way. `None` means the crawl never got that
    far, which is itself a row: something threw.

    A run that was blocked by the site:

    >>> row = crawl_row({"status": "BLOCKED", "robots": {"status": 200},
    ...                  "fetch": {"status": 403, "error": "HTTP 403"}},
    ...                 source_name="Flats", host="www.example.ch")
    >>> row["kind"], row["http_status"]
    ('HTTP_4XX', 403)
    >>> print(row["message"])
    www.example.ch refused the request for the list page of Flats (HTTP 403). It may want a browser - switch this watched page to Rendered mode and test it again.

    A list page nobody is allowed to read:

    >>> row = crawl_row({"status": "SKIPPED", "robots": {"status": 200,
    ...     "list_reason": "robots.txt of https://www.example.ch forbids this "
    ...                    "address for 'xtracting-crawler/1.0'"}},
    ...                 source_name="Flats", host="www.example.ch")
    >>> row["kind"], row["severity"]
    ('ROBOTS_FORBIDDEN', 'error')

    And one that worked:

    >>> crawl_row({"status": "OK", "links": [1, 2, 3]}) is None
    True
    """
    if result is None:
        return {"kind": "INTERNAL", "severity": severity,
                "message": _internal_message(source_name),
                "detail": error or "", "uri": "", "http_status": None}

    status = str(_get(result, "status", "OK") or "OK")
    robots = dict(_get(result, "robots", {}) or {})
    fetch = dict(_get(result, "fetch", {}) or {})
    where = host or _host_of(robots, fetch)
    name = source_name or "this watched page"

    if status == "SKIPPED":
        return _robots_row(robots, name, where, severity)
    if status in ("BLOCKED", "ERROR"):
        return _fetch_row(fetch, name, where, status, error, severity)
    if not _get(result, "links", None) and not _get(result, "counts", {}).get("links", 0):
        # The run itself was fine; the page had nothing on it. That is the
        # shape of a site that changed its layout, or one that builds its
        # list with JavaScript - both worth a line in the log, neither worth
        # an alarm.
        return {
            "kind": "NO_LINKS", "severity": "info" if severity == "info" else "warning",
            "message": (f"The list page of {name} had no links to collect. Sites "
                        f"that build their list with JavaScript deliver an empty "
                        f"page to a plain request - switch this watched page to "
                        f"Rendered mode and test it again, or check the address."),
            "detail": "; ".join(str(w) for w in (_get(result, "warnings", []) or []))[:DETAIL_LIMIT],
            "uri": str(fetch.get("final_url") or ""), "http_status": _int(fetch.get("status")),
        }
    return None


def file_row(*, status: str, url: str, file_type: str = "", reason: str = "",
             page_uri: str = "", severity: str = "warning") -> dict | None:
    """The row for a file that was wanted and could not be sent.

    A file the configuration switched off (`NOT_SELECTED`) leaves NO row: it
    is a decision somebody took, not something that went wrong, and a log
    full of decisions is a log nobody reads.

    >>> row = file_row(status="TOO_LARGE", url="https://a.example/plan.pdf",
    ...                file_type="pdf", reason="more than 1 MB")
    >>> row["kind"]
    'FILE_TOO_LARGE'
    >>> print(row["message"])
    plan.pdf is larger than the limit set for pdf files, so it was not sent (more than 1 MB). Raise the limit for this type on the watched page, or leave the file out.
    >>> file_row(status="NOT_SELECTED", url="https://a.example/x.pdf") is None
    True
    >>> file_row(status="QUEUED", url="https://a.example/x.pdf") is None
    True
    """
    name = _file_name(url)
    kind_word = (file_type or "").lower() or "these"
    detail = "\n".join(part for part in (reason, f"found on {page_uri}" if page_uri else "") if part)

    if status == "TOO_LARGE":
        message = (f"{name} is larger than the limit set for {kind_word} files, so "
                   f"it was not sent ({reason or 'over the limit'}). Raise the limit "
                   f"for this type on the watched page, or leave the file out.")
        kind = "FILE_TOO_LARGE"
    elif status == "WRONG_TYPE":
        why = reason or "the bytes do not match the extension"
        message = (f"{name} is not really a {kind_word} file ({why}), so it was not "
                   f"sent. Often this is a login page served instead of the document "
                   f"- open the address in a browser to see what the site answers.")
        kind = "FILE_WRONG_TYPE"
    elif status == "NO_TEXT":
        message = (f"{name} has no text that could be sent ({reason or 'no text layer'}), "
                   f"so nothing was submitted for it. A scanned document has to go "
                   f"through text recognition first; switch this file type off if "
                   f"the site's {kind_word} files are all scans.")
        kind = "FILE_NO_TEXT"
    elif status == "ROBOTS":
        message = (f"robots.txt forbids {name}, so the file was not downloaded. Ask "
                   f"the site owner for permission, or switch this file type off to "
                   f"stop it being tried again.")
        kind = "ROBOTS_FORBIDDEN"
    elif status == "ERROR":
        message = (f"{name} could not be downloaded ({reason or 'the download failed'}). "
                   f"The next run tries again; if it stays, open the address in a "
                   f"browser to see what the site answers.")
        kind = "NETWORK"
    else:
        return None

    return {"kind": kind, "severity": severity, "message": message,
            "detail": detail, "uri": url, "http_status": None}


def submit_row(*, status: int, message: str, tags: int = 1,
               key_label: str = "", backpressure: bool = False,
               temporary: bool = False, severity: str = "") -> dict:
    """The row for a submission the platform did not take.

    Four groups with four different answers, and the sentence has to say
    which one this is - the customer's next step is "top up", "fix the key",
    "wait" or "nothing", and they are not interchangeable.

    >>> row = submit_row(status=402, message="HTTP 402 (the balance is used up)",
    ...                  tags=12, key_label="alpha", backpressure=True)
    >>> row["kind"], row["severity"]
    ('SUBMIT_BACKPRESSURE', 'warning')
    >>> print(row["message"])
    The platform asked for a pause for key 'alpha' (HTTP 402 (the balance is used up)). 12 documents stay in the queue and go out later - nothing is lost. Top up the balance, or let the open tasks finish.

    >>> submit_row(status=401, message="HTTP 401", key_label="alpha")["kind"]
    'KEY_INVALID'
    >>> row = submit_row(status=500, message="HTTP 500", tags=4, temporary=True)
    >>> row["kind"], row["severity"]
    ('HTTP_5XX', 'warning')
    """
    where = f"key {key_label!r}" if key_label else "this key"
    count = plural(tags, "document")

    if temporary and not backpressure and status not in (402, 429, 401):
        # Already retried by the client and still not through. Not a refusal:
        # the documents keep their place in the queue, and the row exists so
        # that "nothing has arrived since lunchtime" has an answer other than
        # the container's log.
        return {
            "kind": "HTTP_5XX" if status >= 500 else "NETWORK",
            "severity": severity or "warning",
            "message": (f"The platform could not take {count} for {where} yet "
                        f"({message}). They stay in the queue and go out on one "
                        f"of the next rounds - nothing needs to be done unless "
                        f"this keeps happening."),
            "detail": message, "uri": "", "http_status": _int(status) or None,
        }

    if backpressure or status in (402, 429):
        return {
            "kind": "SUBMIT_BACKPRESSURE", "severity": severity or "warning",
            "message": (f"The platform asked for a pause for {where} ({message}). "
                        f"{count} stay in the queue and go out later - nothing is "
                        f"lost. Top up the balance, or let the open tasks finish."),
            "detail": message, "uri": "", "http_status": _int(status),
        }
    if status == 401:
        return {
            "kind": "KEY_INVALID", "severity": severity or "error",
            "message": (f"The platform refused {where} ({message}). Every watched page "
                        f"using this label stops here: check the label in "
                        f"XTRACTING_API_KEYS, then the key itself."),
            "detail": message, "uri": "", "http_status": _int(status),
        }
    return {
        "kind": "SUBMIT_REJECTED", "severity": severity or "error",
        "message": (f"The platform refused {count} sent with {where} ({message}). "
                    f"They are marked failed and are not sent again - a retry "
                    f"would be refused the same way, or paid for twice."),
        "detail": message, "uri": "", "http_status": _int(status) or None,
    }


def key_row(*, key_label: str, source_name: str, known: list[str] | tuple = ()) -> dict:
    """The row for a watched page whose key label nothing answers to.

    >>> row = key_row(key_label="beta", source_name="Flats", known=["alpha"])
    >>> row["kind"], row["severity"]
    ('KEY_INVALID', 'warning')
    >>> print(row["message"])
    The key label 'beta' of Flats is not in XTRACTING_API_KEYS, so its documents cannot be submitted. Add the label to XTRACTING_API_KEYS, or choose one of: alpha.
    """
    labels = ", ".join(sorted(known)) or "none configured"
    return {
        "kind": "KEY_INVALID", "severity": "warning",
        "message": (f"The key label {key_label!r} of {source_name} is not in "
                    f"XTRACTING_API_KEYS, so its documents cannot be submitted. "
                    f"Add the label to XTRACTING_API_KEYS, or choose one of: "
                    f"{labels}."),
        "detail": f"labels this crawler knows: {labels}",
        "uri": "", "http_status": None,
    }


def key_gone_row(*, prefix: str, source_name: str, where: str = "watchlist") -> dict:
    """The row for a pinned key that no longer authenticates.

    A pinned key is a promise: somebody chose it because of what it costs and
    what it is set to run with. When it disappears the honest thing is to stop
    and say so, not to carry on with the next key and a different bill.

    >>> row = key_gone_row(prefix="ab12cd34", source_name="Flats", where="link group")
    >>> row["kind"], row["severity"]
    ('KEY_INVALID', 'error')
    >>> print(row["message"])
    The key ab12cd34 pinned to the link group of Flats no longer works, so those pages are not being evaluated. Put the key back in XTRACTING_API_KEYS, or choose another key of the same project in the dashboard.
    """
    return {
        "kind": "KEY_INVALID", "severity": "error",
        "message": (f"The key {prefix} pinned to the {where} of {source_name} no "
                    f"longer works, so those pages are not being evaluated. Put "
                    f"the key back in XTRACTING_API_KEYS, or choose another key "
                    f"of the same project in the dashboard."),
        "detail": (f"the key was deleted, deactivated or expired; the crawler "
                   f"does not switch to another key on its own because a pinned "
                   f"key fixes the settings and the price"),
        "uri": "", "http_status": None,
    }


def no_key_row(*, project_name: str, source_name: str) -> dict:
    """The row for a watchlist whose whole project has no working key left.

    The fallback deliberately stops at the project boundary. Borrowing a key
    from another project would file these documents in the wrong archive, which
    is worse than collecting nothing and saying so.

    >>> row = no_key_row(project_name="Flats", source_name="Rentals")
    >>> row["kind"], row["severity"]
    ('KEY_INVALID', 'error')
    >>> print(row["message"])
    No working key is left for the project Flats, so Rentals has stopped being evaluated. Add a key of that project to XTRACTING_API_KEYS. A key of another project is not used, because its documents would land in the wrong archive.
    """
    return {
        "kind": "KEY_INVALID", "severity": "error",
        "message": (f"No working key is left for the project {project_name}, so "
                    f"{source_name} has stopped being evaluated. Add a key of "
                    f"that project to XTRACTING_API_KEYS. A key of another "
                    f"project is not used, because its documents would land in "
                    f"the wrong archive."),
        "detail": "the fallback never crosses a project boundary",
        "uri": "", "http_status": None,
    }


# ----------------------------------------------------------------------
# The pieces
# ----------------------------------------------------------------------

def _robots_row(robots: dict, name: str, host: str, severity: str) -> dict:
    """A run nobody was allowed to make. Two cases, and they are different
    answers: rules that say no, and rules that could not be read at all."""
    reason = str(robots.get("list_reason") or robots.get("note") or "").strip()
    status = _int(robots.get("status"))
    detail = "\n".join(part for part in (
        reason,
        f"robots.txt answered HTTP {status}" if status else "robots.txt could not be fetched",
        f"crawl delay: {robots.get('crawl_delay')} s" if robots.get("crawl_delay") else "",
    ) if part)

    unreadable = not status or status >= 500
    if unreadable:
        return {
            "kind": "ROBOTS_UNREADABLE", "severity": severity,
            "message": (f"The rules of {host or 'this host'} could not be read "
                        f"({reason or 'robots.txt did not answer'}), so nothing was "
                        f"fetched for {name}. That is deliberate: unknown rules "
                        f"count as 'not allowed'. Try again when the site is back."),
            "detail": detail, "uri": "", "http_status": status or None,
        }
    return {
        "kind": "ROBOTS_FORBIDDEN", "severity": severity,
        "message": (f"robots.txt of {host or 'this host'} forbids the list page of "
                    f"{name}, so nothing was fetched. Ask the site owner for "
                    f"permission - and record it in the page's override reason - "
                    f"or watch a list page the rules allow."),
        "detail": detail, "uri": "", "http_status": status or None,
    }


def _fetch_row(fetch: dict, name: str, host: str, status_word: str, error: str,
               severity: str) -> dict:
    """What the site answered instead of the list page."""
    status = _int(fetch.get("status"))
    said = str(fetch.get("error") or error or "").strip()
    where = host or "the site"
    detail = "\n".join(part for part in (
        said, f"final address: {fetch.get('final_url')}" if fetch.get("final_url") else "",
    ) if part)
    uri = str(fetch.get("final_url") or "")

    if fetch.get("challenge"):
        return {"kind": "CHALLENGE", "severity": severity, "detail": detail,
                "uri": uri, "http_status": status or None,
                "message": (f"{where} answered with a browser check instead of the "
                            f"list page of {name}. Rendered mode gets through some "
                            f"of those; where it does not, the site does not want "
                            f"to be read by a program.")}
    if status == 404:
        return {"kind": "HTTP_4XX", "severity": severity, "detail": detail,
                "uri": uri, "http_status": 404,
                "message": (f"The list page of {name} does not exist any more - "
                            f"{where} answered 404. Open the address in a browser "
                            f"and enter the new one in the editor.")}
    if status in (429, 503):
        return {"kind": "HTTP_5XX" if status == 503 else "HTTP_4XX",
                "severity": severity, "detail": detail, "uri": uri,
                "http_status": status,
                "message": (f"{where} asked for room (HTTP {status}) while reading "
                            f"{name}. The run stopped and the delay between "
                            f"requests was raised; nothing needs to be done unless "
                            f"it keeps happening.")}
    if status and 400 <= status < 500:
        return {"kind": "HTTP_4XX", "severity": severity, "detail": detail,
                "uri": uri, "http_status": status,
                "message": (f"{where} refused the request for the list page of "
                            f"{name} (HTTP {status}). It may want a browser - "
                            f"switch this watched page to Rendered mode and test it again.")}
    if status and status >= 500:
        return {"kind": "HTTP_5XX", "severity": severity, "detail": detail,
                "uri": uri, "http_status": status,
                "message": (f"{where} has a problem of its own (HTTP {status}) and "
                            f"could not answer for {name}. Nothing to change here; "
                            f"the next run tries again.")}
    if _looks_like_timeout(said):
        return {"kind": "TIMEOUT", "severity": severity, "detail": detail,
                "uri": uri, "http_status": None,
                "message": (f"{where} did not answer in time for {name}. Try again "
                            f"later, or raise the timeout on this watched page if the "
                            f"site is simply slow.")}
    if status_word == "ERROR" and not said:
        return {"kind": "INTERNAL", "severity": severity, "detail": detail,
                "uri": uri, "http_status": None,
                "message": _internal_message(name)}
    return {"kind": "NETWORK", "severity": severity, "detail": detail, "uri": uri,
            "http_status": None,
            "message": (f"{where} could not be reached for {name} "
                        f"({said or 'the connection failed'}). Check the address, "
                        f"and whether this machine can reach the site at all.")}


def _internal_message(name: str) -> str:
    return (f"The crawl of {name or 'this watched page'} stopped with an unexpected error. "
            f"That is a fault in the crawler, not in the site - the technical "
            f"message is in the detail below, and the next run tries again.")


def _looks_like_timeout(text: str) -> bool:
    """
    >>> _looks_like_timeout("ReadTimeout: timed out"), _looks_like_timeout("refused")
    (True, False)
    """
    lowered = text.lower()
    return "timeout" in lowered or "timed out" in lowered


def _file_name(url: str) -> str:
    """The last part of an address, for a sentence about a file.

    >>> _file_name("https://a.example/files/expose-7.pdf?v=2")
    'expose-7.pdf'
    >>> _file_name("https://a.example/")
    'a.example'
    """
    tail = str(url or "").split("?")[0].rstrip("/").rsplit("/", 1)[-1]
    return tail or str(url or "")


def _host_of(robots: dict, fetch: dict) -> str:
    from urllib.parse import urlsplit
    for candidate in (fetch.get("final_url"), robots.get("origin")):
        if candidate:
            netloc = urlsplit(str(candidate)).netloc
            if netloc:
                return netloc
    return ""


def _get(result: Any, name: str, default: Any = None) -> Any:
    """Read a field off a CrawlResult or off the dictionary a snapshot keeps.

    >>> _get({"status": "OK"}, "status"), _get(object(), "status", "?")
    ('OK', '?')
    """
    if isinstance(result, dict):
        return result.get(name, default)
    return getattr(result, name, default)


def _int(value: Any) -> int:
    """
    >>> _int("403"), _int(None), _int("x")
    (403, 0, 0)
    """
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


__all__ = ["DETAIL_LIMIT", "KINDS", "LABELS", "SEVERITIES", "crawl_row",
           "file_row", "key_row", "record", "submit_row"]
