"""The Watchlist configuration: everything that reads or writes `scraper_config`.

One door, for two reasons.

WHAT MAY BE WRITTEN IS A LIST, NOT A HABIT. The watched-page editor posts a form,
and a form is a dictionary somebody else composed. `FIELDS` below is the
positive list of columns that dictionary may touch, each with the type and the
range the database's own CHECK uses - so the form refuses what the database
would refuse, with a sentence instead of a constraint name. Anything not on
the list is either ignored (a field the editor sends back unchanged, like the
id) or refused by name where silence would mislead: `bool_enabled` has its own
endpoint, because saving a configuration and starting a crawl are two
different acts.

CHILDREN ARE REPLACED, NOT MERGED. Patterns, exact addresses and file rules
are what one round of learning produced; a merge of two rounds is a set nobody
chose. So a save deletes and re-inserts them inside the same transaction, and
the source's `date_updated` moves with them - the crawler's config sync only
watches that one column, and a pattern that changed without it would be read
hours later, or never.

The archive itself stays read-only: the joins in `list_sources()` read
`crawler.*` (the crawler's runtime state) and `processed_data.tasks` (what
actually arrived), and write to neither.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterable
from urllib.parse import urlsplit

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from crawlkit.hashing import canonical_uri
from crawlkit.learn import LearnedPattern

from . import heartbeats
from .db import Database, get_db
from .sqlbuild import like_substring

log = logging.getLogger(__name__)

MODES = ("selected", "all_except_rejected", "exact")
FORMATS = ("plain", "html")
ENGINES = ("http", "playwright")
PATTERN_KINDS = ("accept", "reject")
PATTERN_ORIGINS = ("learned", "manual", "legal_default")
EXACT_KINDS = ("monitor", "reject")
FILE_RULE_KINDS = ("type_default", "file_include", "file_exclude")
SNAPSHOT_KINDS = ("crawl", "files")

# The address column the editor asks for, mirrored from the CHECK in
# 03-scraper.sql so the message can name the mistake.
_HTTP_URL = re.compile(r"^https?://", re.IGNORECASE)

#: How many rows a list endpoint returns at most. A source that has run for a
#: year has tens of thousands of documents; the page shows the newest.
PAGE_SIZE = 50
MAX_PAGE_SIZE = 500


class SourceError(ValueError):
    """A refusal the editor can show as it is. `hint` says what to change.

    `status` is the HTTP code the router answers with. 400 is the usual one -
    the request said something that cannot be saved. A name that is already
    taken is 409: nothing about the request is malformed, the archive simply
    holds that name already, and the editor tells the two apart to decide
    whether to point at a field or at another source.
    """

    def __init__(self, message: str, hint: str = "", status: int = 400) -> None:
        super().__init__(message)
        self.hint = hint
        self.status = status


# ── The writable-field whitelist ────────────────────────────────────────


@dataclass(frozen=True)
class Field:
    """One writable column. `minimum`/`maximum` and `choices` repeat the
    database's CHECK on purpose - the constraint stays the last word, this is
    the first one, and the unit test holds the two together."""

    name: str
    kind: str                       # text | bool | int
    choices: tuple[str, ...] = ()
    minimum: int | None = None
    maximum: int | None = None
    pattern: re.Pattern[str] | None = None
    blank_ok: bool = True
    hint: str = ""


FIELDS: tuple[Field, ...] = (
    Field("text_name", "text", blank_ok=False,
          hint="a name for this source, e.g. the site and what is listed"),
    Field("text_list_url", "text", blank_ok=False, pattern=_HTTP_URL,
          hint="the address of the list page, starting with http:// or https://"),
    Field("text_key_label", "text",
          hint="the nickname of a key, as it appears in the log; the key "
               "itself is chosen by text_key_prefix"),
    Field("text_mode", "text", choices=MODES),
    Field("bool_sub_sub", "bool"),
    Field("text_format", "text", choices=FORMATS),
    Field("text_engine", "text", choices=ENGINES),
    Field("text_wait_for_selector", "text"),
    Field("integer_wait_after_load_ms", "int", minimum=0, maximum=60000),
    Field("bool_respect_robots", "bool"),
    Field("text_override_reason", "text"),
    Field("bool_same_host_only", "bool"),
    Field("bool_drop_legal_links", "bool"),
    Field("bool_follow_pagination", "bool"),
    Field("text_paging_param", "text"),
    Field("text_next_selector", "text"),
    Field("integer_max_pages", "int", minimum=1, maximum=1000),
    Field("integer_interval_minutes", "int", minimum=1, maximum=43200),
    Field("integer_politeness_seconds", "int", minimum=0, maximum=3600),
    Field("integer_max_new_per_run", "int", minimum=1, maximum=1000),
    Field("integer_timeout_seconds", "int", minimum=1, maximum=600),
    Field("bool_resubmit_on_change", "bool"),
    Field("text_content_selector", "text"),
    Field("text_drop_selectors", "text"),
    Field("text_notes", "text"),
)

WRITABLE: dict[str, Field] = {f.name: f for f in FIELDS}

# Columns a caller may well send back (they come from a GET) but that this
# module owns. Named rather than silently dropped where dropping would look
# like the change was saved.
REFUSED: dict[str, str] = {
    "bool_enabled": "enabling is a separate step: POST /api/sources/{id}/enable",
    "text_list_url_canonical": "derived from text_list_url when it is saved",
    "text_host": "derived from text_list_url when it is saved",
}
# Columns that are simply not the caller's business; sent back by an editor
# that posts the whole row, so they are dropped without a word.
IGNORED = ("bigint_id", "id", "date_added", "date_updated", "patterns",
           "exact_urls", "file_rules")


def _as_bool(name: str, value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off", ""):
        return False
    raise SourceError(f"{name} must be true or false, not {value!r}")


def _as_int(name: str, value: Any, low: int | None, high: int | None) -> int:
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise SourceError(f"{name} must be a whole number, not {value!r}") from exc
    if low is not None and number < low:
        raise SourceError(f"{name} must be at least {low}, not {number}")
    if high is not None and number > high:
        raise SourceError(f"{name} must be at most {high}, not {number}")
    return number


def clean_fields(payload: dict[str, Any]) -> dict[str, Any]:
    """The writable part of `payload`, typed and range-checked.

    >>> clean_fields({"text_name": " Estate ", "integer_max_pages": "3",
    ...               "bool_sub_sub": "yes", "bigint_id": 7})
    {'text_name': 'Estate', 'integer_max_pages': 3, 'bool_sub_sub': True}
    >>> clean_fields({"text_mode": "everything"})
    Traceback (most recent call last):
    ...
    app.sources_store.SourceError: text_mode must be one of selected, all_except_rejected, exact
    >>> clean_fields({"bool_enabled": True})
    Traceback (most recent call last):
    ...
    app.sources_store.SourceError: bool_enabled cannot be saved with the form
    """
    clean: dict[str, Any] = {}
    for name, value in (payload or {}).items():
        if name in IGNORED:
            continue
        if name in REFUSED:
            raise SourceError(f"{name} cannot be saved with the form", REFUSED[name])
        spec = WRITABLE.get(name)
        if spec is None:
            # An unknown key is a typo or a field of a newer editor; neither is
            # worth refusing a save that is otherwise correct.
            log.debug("ignoring unknown source field %r", name)
            continue
        if spec.kind == "bool":
            clean[name] = _as_bool(name, value)
            continue
        if spec.kind == "int":
            clean[name] = _as_int(name, value, spec.minimum, spec.maximum)
            continue
        text = "" if value is None else str(value).strip()
        if not text and not spec.blank_ok:
            raise SourceError(f"{name} cannot be empty", spec.hint)
        if text and spec.choices and text not in spec.choices:
            raise SourceError(f"{name} must be one of " + ", ".join(spec.choices))
        if text and spec.pattern and not spec.pattern.match(text):
            raise SourceError(f"{name} is not an address the crawl can use: {text!r}",
                              spec.hint)
        clean[name] = text
    return clean


def check_override(merged: dict[str, Any]) -> None:
    """The cross-field CHECK: ignoring robots.txt needs a written reason.

    Checked on the MERGED row - saving only `bool_respect_robots=false` must
    fail when the stored reason is empty, and saving only a reason must not.

    >>> check_override({"bool_respect_robots": False, "text_override_reason": ""})
    Traceback (most recent call last):
    ...
    app.sources_store.SourceError: crawling a site that forbids it needs a written reason
    >>> check_override({"bool_respect_robots": False, "text_override_reason": "written permission of the operator"})
    """
    if merged.get("bool_respect_robots", True):
        return
    if not str(merged.get("text_override_reason") or "").strip():
        raise SourceError(
            "crawling a site that forbids it needs a written reason",
            "fill in text_override_reason - who allowed it, and when. The "
            "database refuses the row without it.")


def derived_url_fields(list_url: str) -> dict[str, str]:
    """The two columns computed from the list address.

    Stored rather than recomputed on every read: the overview groups by host,
    and a later change to the canonical form should show up as a difference
    instead of silently re-keying every source.

    >>> derived_url_fields("https://WWW.Example.COM:443/rent/list?b=2&a=1#top")
    {'text_list_url_canonical': 'https://www.example.com/rent/list?a=1&b=2', 'text_host': 'www.example.com'}
    """
    canon = canonical_uri(list_url or "")
    return {"text_list_url_canonical": canon,
            "text_host": (urlsplit(canon).hostname or "").lower()}


# ── Children: patterns, exact addresses, file rules ─────────────────────


def clean_projects(rows: Iterable[dict] | None) -> list[dict]:
    """The projects this page collects into, as they arrive from the editor.

    A page may serve several: a project decides what is extracted from a
    document, so two projects asking different questions of the same page are
    two extractions. Each carries the key that evaluates it FOR THAT PROJECT,
    or '' for the project's default.

    Deduplicated by project id and kept in the order they were chosen: the
    first is the one the overview names when it has room for one name only.

    >>> clean_projects([{"text_project_id": " p1 ", "text_key_prefix": "aaaa1111"},
    ...                 {"text_project_id": "p1"}, {"text_project_id": ""}])
    [{'text_project_id': 'p1', 'text_key_prefix': 'aaaa1111', 'integer_order': 0}]
    """
    if rows is None:
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise SourceError("each project has to be an object with a "
                              "text_project_id", "e.g. {\"text_project_id\": \"clx…\"}")
        project_id = str(row.get("text_project_id") or "").strip()
        if not project_id or project_id in seen:
            continue
        seen.add(project_id)
        out.append({"text_project_id": project_id,
                    "text_key_prefix": str(row.get("text_key_prefix") or "").strip(),
                    "integer_order": len(out)})
    return out


def clean_patterns(rows: Iterable[dict] | None) -> list[dict]:
    """Pattern rows, with label and regex re-derived from `json_form`.

    The form is the source of truth (03-scraper.sql says so); a caller that
    sends a hand-edited regex beside it gets the regex the form actually
    means, not the one it typed.
    """
    clean: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for row in rows or ():
        if not isinstance(row, dict):
            raise SourceError("a pattern must be an object with json_form and text_kind")
        kind = str(row.get("text_kind") or "accept")
        if kind not in PATTERN_KINDS:
            raise SourceError(f"a pattern's text_kind must be one of {', '.join(PATTERN_KINDS)}")
        origin = str(row.get("text_origin") or "learned")
        if origin not in PATTERN_ORIGINS:
            raise SourceError(
                f"a pattern's text_origin must be one of {', '.join(PATTERN_ORIGINS)}")
        try:
            learned = LearnedPattern.from_row({**row, "text_kind": kind, "text_origin": origin})
        except Exception as exc:  # noqa: BLE001 - a malformed form is the caller's mistake
            raise SourceError(f"this pattern cannot be read: {exc}",
                              "json_form must be what /api/sources/learn returned") from exc
        out = learned.to_row()
        # The key is not something learn() knows about: it is attached after the
        # rules come back, by a person choosing which key evaluates this shape of
        # link. So it travels beside the row rather than through LearnedPattern,
        # which stays a statement about the links alone.
        out["text_key_prefix"] = str(row.get("text_key_prefix") or "")
        # The UNIQUE is (source, kind, regex): two rows that render the same
        # regex are one pattern, and the second would abort the transaction.
        key = (out["text_kind"], out["text_regex"])
        if key in seen:
            continue
        seen.add(key)
        clean.append(out)
    return clean


def clean_exact_urls(rows: Iterable[dict] | None) -> list[dict]:
    """Exact addresses, canonical, one verdict each."""
    clean: list[dict] = []
    verdicts: dict[str, str] = {}
    keys: dict[str, str] = {}
    for row in rows or ():
        if isinstance(row, str):
            row = {"text_kind": "reject", "text_url_canonical": row}
        kind = str(row.get("text_kind") or "reject")
        if kind not in EXACT_KINDS:
            raise SourceError(f"an exact address's text_kind must be one of {', '.join(EXACT_KINDS)}")
        raw = str(row.get("text_url_canonical") or row.get("url") or "")
        canon = canonical_uri(raw)
        if not canon or not _HTTP_URL.match(canon):
            raise SourceError(f"{raw!r} is not an address the crawl can use",
                              "exact addresses start with http:// or https://")
        if canon in verdicts and verdicts[canon] != kind:
            # The UNIQUE spans the address alone: it cannot be monitored and
            # rejected at once, and guessing which was meant would be worse
            # than asking.
            raise SourceError(f"{canon} is listed both as monitor and as reject",
                              "one address, one verdict")
        verdicts[canon] = kind
        keys[canon] = str(row.get("text_key_prefix") or "")
    return [{"text_kind": kind, "text_url_canonical": url,
             "text_key_prefix": keys.get(url, "")}
            for url, kind in verdicts.items()]


def clean_file_rules(rows: Iterable[dict] | None) -> list[dict]:
    clean: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for row in rows or ():
        kind = str(row.get("text_kind") or "type_default")
        if kind not in FILE_RULE_KINDS:
            raise SourceError(f"a file rule's text_kind must be one of {', '.join(FILE_RULE_KINDS)}")
        value = str(row.get("text_value") or "").strip()
        if not value:
            raise SourceError("a file rule needs a text_value",
                              "a file type (pdf, docx, xlsx, csv, txt) or one file address")
        if kind in ("file_include", "file_exclude"):
            value = canonical_uri(value) or value
        else:
            value = value.lower().lstrip(".")
        key = (kind, value)
        if key in seen:
            continue
        seen.add(key)
        clean.append({
            "text_kind": kind,
            "text_value": value,
            "bool_enabled": _as_bool("bool_enabled", row.get("bool_enabled", True)),
            "integer_max_mb": _as_int("integer_max_mb", row.get("integer_max_mb", 25), 1, 500),
        })
    return clean


# ── Reading ─────────────────────────────────────────────────────────────

SOURCE_COLUMNS = """
    s.bigint_id AS id, s.date_added, s.date_updated, s.text_name, s.text_list_url,
    s.text_list_url_canonical, s.text_host, s.text_key_label, s.text_mode,
    s.bool_sub_sub, s.text_format, s.text_engine, s.text_wait_for_selector,
    s.integer_wait_after_load_ms, s.bool_enabled, s.bool_respect_robots,
    s.text_override_reason, s.bool_same_host_only, s.bool_drop_legal_links,
    s.bool_follow_pagination, s.text_paging_param, s.text_next_selector,
    s.integer_max_pages, s.integer_interval_minutes, s.integer_politeness_seconds,
    s.integer_max_new_per_run, s.integer_timeout_seconds, s.bool_resubmit_on_change,
    s.text_content_selector, s.text_drop_selectors, s.text_notes
"""

# The tail of the overview statement, named so that the search box can put a
# WHERE in front of it without a second copy of forty lines of joins.
LIST_ORDER = "     ORDER BY s.text_name\n"

# The overview in one statement. Every join is LEFT: a source that has never
# run has no target, no run and no queue row, and the row still has to appear -
# "saved but never crawled" is exactly the state the page must show.
LIST_SQL = f"""
    SELECT {SOURCE_COLUMNS},
           t.bool_robots_allowed, t.text_robots_reason, t.date_robots_checked,
           t.float_robots_crawl_delay, t.date_last_run, t.date_next_run,
           t.integer_consecutive_failures, t.text_last_error, t.text_key_status,
           r.text_status AS run_status, r.date_added AS run_date,
           r.integer_pages AS run_pages, r.integer_links AS run_links,
           r.integer_accepted AS run_accepted, r.integer_new AS run_new,
           r.integer_files AS run_files, r.integer_submitted AS run_submitted,
           r.text_message AS run_message,
           COALESCE(q.pending, 0) AS queue_pending, COALESCE(q.sent, 0) AS queue_sent,
           COALESCE(q.archived, 0) AS queue_archived, COALESCE(q.failed, 0) AS queue_failed,
           COALESCE(p.accept_patterns, 0) AS accept_patterns,
           COALESCE(p.reject_patterns, 0) AS reject_patterns,
           COALESCE(x.exact_urls, 0) AS exact_urls,
           n.bigint_id AS snapshot_id, n.date_added AS snapshot_date,
           n.text_status AS snapshot_status,
           -- Only the verdict, never the whole result: a snapshot carries up
           -- to two thousand links, and the overview would then read
           -- megabytes per row to render a pill.
           n.json_result -> 'robots' AS snapshot_robots,
           -- AND WHAT THE SITE ITSELF ANSWERED. Two scalars out of the same
           -- JSON, for the same reason the robots verdict is taken and the
           -- rest is left: the overview showed "robots.txt not checked yet"
           -- on every row whose list page had answered 403, because the only
           -- thing it read out of a finished test was the robots verdict -
           -- and a site that refuses us never gets as far as robots.txt.
           n.json_result -> 'fetch' ->> 'status' AS snapshot_http,
           n.json_result -> 'fetch' ->> 'error' AS snapshot_fetch_error,
           n.json_result -> 'fetch' ->> 'challenge' AS snapshot_challenge,
           COALESCE(pr.projects, '[]'::jsonb) AS projects
      FROM scraper_config.sources s
      LEFT JOIN scraper.targets t ON t.bigint_fk_source = s.bigint_id
      LEFT JOIN LATERAL (
           SELECT sr.text_status, sr.date_added, sr.integer_pages, sr.integer_links,
                  sr.integer_accepted, sr.integer_new, sr.integer_files,
                  sr.integer_submitted, sr.text_message
             FROM monitoring.scraper_runs sr
            WHERE sr.bigint_fk_source = s.bigint_id
            ORDER BY sr.date_added DESC LIMIT 1) r ON TRUE
      LEFT JOIN LATERAL (
           SELECT count(*) FILTER (WHERE sq.text_status = 'PENDING')  AS pending,
                  count(*) FILTER (WHERE sq.text_status = 'SENT')     AS sent,
                  count(*) FILTER (WHERE sq.text_status = 'ARCHIVED') AS archived,
                  count(*) FILTER (WHERE sq.text_status = 'FAILED')   AS failed
             FROM scraper.submit_queue sq
            WHERE sq.bigint_fk_source = s.bigint_id) q ON TRUE
      LEFT JOIN LATERAL (
           -- The projects this page collects into, by name where the registry
           -- knows one. A card says "collects into A and B", and a page with
           -- none says so too: that is the state that collects nothing.
           SELECT jsonb_agg(jsonb_build_object(
                      'text_project_id', spj.text_project_id,
                      'text_key_prefix', spj.text_key_prefix,
                      'text_project_name',
                          COALESCE(pj.text_name, spj.text_project_id))
                  ORDER BY spj.integer_order, spj.text_project_id) AS projects
             FROM scraper_config.source_projects spj
             LEFT JOIN scraper.projects pj
                    ON pj.text_project_id = spj.text_project_id
            WHERE spj.bigint_fk_source = s.bigint_id) pr ON TRUE
      LEFT JOIN LATERAL (
           SELECT count(*) FILTER (WHERE sp.text_kind = 'accept') AS accept_patterns,
                  count(*) FILTER (WHERE sp.text_kind = 'reject') AS reject_patterns
             FROM scraper_config.source_patterns sp
            WHERE sp.bigint_fk_source = s.bigint_id) p ON TRUE
      LEFT JOIN LATERAL (
           SELECT count(*) AS exact_urls
             FROM scraper_config.source_exact_urls su
            WHERE su.bigint_fk_source = s.bigint_id) x ON TRUE
      LEFT JOIN LATERAL (
           SELECT ts.bigint_id, ts.date_added, ts.text_status, ts.json_result
             FROM scraper_config.test_snapshots ts
            WHERE ts.bigint_fk_source = s.bigint_id
            ORDER BY ts.date_added DESC LIMIT 1) n ON TRUE
{LIST_ORDER}"""

# "How many documents of this source reached the archive?" - the tag the
# crawler writes is xs_<source id>_<hex>, and the platform appends -01 when it
# splits the content, so the id is what stands between the first two
# underscores. One grouped statement for every source rather than one per row:
# the column is indexed but the prefix match is not, so this reads the tag
# index once and nothing else.
ARCHIVED_SQL = """
    SELECT (regexp_match(text_tag, '^xs_([0-9]+)_'))[1]::BIGINT AS source_id,
           count(*) AS archived
      FROM processed_data.tasks
     WHERE text_tag ~ '^xs_[0-9]+_'
     GROUP BY 1
"""

_ARCHIVED_CACHE_SECONDS = 60
_archived_lock = threading.Lock()
_archived_cached: tuple[float, dict[int, int]] = (0.0, {})


def archived_counts(db: Database, *, fresh: bool = False) -> dict[int, int]:
    """Archived task count per source id, cached for a minute.

    Cached because the overview asks for it on every load and the answer is a
    pass over the task tags of the whole archive - a number that is a minute
    old is worth more than a page that takes a minute.
    """
    global _archived_cached
    with _archived_lock:
        stamp, counts = _archived_cached
        if not fresh and counts and time.monotonic() - stamp < _ARCHIVED_CACHE_SECONDS:
            return counts
    with db.read() as conn:
        rows = conn.execute(ARCHIVED_SQL).fetchall()
    counts = {int(r["source_id"]): int(r["archived"]) for r in rows if r["source_id"]}
    with _archived_lock:
        _archived_cached = (time.monotonic(), counts)
    return counts


def invalidate_archived_counts() -> None:
    global _archived_cached
    with _archived_lock:
        _archived_cached = (0.0, {})


def robots_of(robots: Any) -> tuple[bool | None, str]:
    """(allowed, reason) out of a test result's `robots` object, or (None, "")
    when it says nothing about the list page.

    >>> robots_of({"list_allowed": False, "list_reason": "Disallow: /"})
    (False, 'Disallow: /')
    >>> robots_of(None), robots_of({"status": 200})
    ((None, ''), (None, ''))
    """
    if not isinstance(robots, dict):
        return None, ""
    allowed = robots.get("list_allowed")
    reason = str(robots.get("list_reason") or robots.get("note") or "")
    return (None if allowed is None else bool(allowed)), reason


def fetch_facts(row: Any) -> dict[str, Any]:
    """What the site answered, out of the three columns `SNAPSHOT_FETCH` selects.

    ONE SHAPE FOR TWO READERS. The list builds a row per watched page and
    `GET /sources/{id}` rebuilds one after a test, and the overview draws its
    pill from whichever spoke last. They disagreed: the list carried the status
    and the single read did not, so a page that had just answered 403 showed
    "robots.txt allows the list page" until the reader reloaded and the list
    corrected it. A pill that changes on reload is worse than either answer.
    """
    challenge = row.get("snapshot_challenge")
    return {
        # What the site answered, in the two forms it can answer in.
        "http_status": _int_or_none(row.get("snapshot_http")),
        "error": (row.get("snapshot_fetch_error") or "")[:400],
        "challenge": bool(challenge and str(challenge).lower()
                          not in ("false", "none", "null", "0", "")),
    }


def _int_or_none(value: Any) -> int | None:
    """An HTTP status out of JSON, which carries it as text.

    >>> _int_or_none("403"), _int_or_none(None), _int_or_none("")
    (403, None, None)
    >>> _int_or_none("not a number")
    """
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _overview_row(row: dict) -> dict[str, Any]:
    """One list row, shaped for the page: the source, then what happened to it."""
    source = {k: v for k, v in row.items() if k in _SOURCE_KEYS}
    tested_allowed, tested_reason = robots_of(row.get("snapshot_robots"))
    return {
        **source,
        "robots": {
            "allowed": tested_allowed if tested_allowed is not None else row.get("bool_robots_allowed"),
            "reason": tested_reason or (row.get("text_robots_reason") or ""),
            "checked": row.get("date_robots_checked"),
            "crawl_delay": row.get("float_robots_crawl_delay"),
            "from": "test" if tested_allowed is not None else ("crawler" if row.get("date_robots_checked") else "none"),
        },
        "schedule": {
            "last_run": row.get("date_last_run"),
            "next_run": row.get("date_next_run"),
            "consecutive_failures": row.get("integer_consecutive_failures") or 0,
            "last_error": row.get("text_last_error") or "",
            "key_status": row.get("text_key_status") or "",
        },
        "last_run": None if not row.get("run_date") else {
            "status": row.get("run_status"),
            "date": row.get("run_date"),
            "pages": row.get("run_pages"), "links": row.get("run_links"),
            "accepted": row.get("run_accepted"), "new": row.get("run_new"),
            "files": row.get("run_files"), "submitted": row.get("run_submitted"),
            "message": row.get("run_message") or "",
        },
        "projects": list(row.get("projects") or []),
        "counts": {
            "accept_patterns": row.get("accept_patterns", 0),
            "reject_patterns": row.get("reject_patterns", 0),
            "exact_urls": row.get("exact_urls", 0),
            "pending": row.get("queue_pending", 0),
            "sent": row.get("queue_sent", 0),
            "failed": row.get("queue_failed", 0),
            "archived": 0,          # filled in by list_sources from one query
        },
        "snapshot": None if not row.get("snapshot_id") else {
            "id": row["snapshot_id"], "date": row.get("snapshot_date"),
            "status": row.get("snapshot_status"),
            **fetch_facts(row),
        },
    }


_SOURCE_KEYS = {"id", "date_added", "date_updated", "text_name", "text_list_url",
                "text_list_url_canonical", "text_host", "text_key_label", "text_mode",
                "bool_sub_sub", "text_format", "text_engine", "text_wait_for_selector",
                "integer_wait_after_load_ms", "bool_enabled", "bool_respect_robots",
                "text_override_reason", "bool_same_host_only", "bool_drop_legal_links",
                "bool_follow_pagination", "text_paging_param", "text_next_selector",
                "integer_max_pages", "integer_interval_minutes",
                "integer_politeness_seconds", "integer_max_new_per_run",
                "integer_timeout_seconds", "bool_resubmit_on_change",
                "text_content_selector", "text_drop_selectors", "text_notes"}


#: What the overview's search box looks at: the four things a card says in
#: words. Deliberately NOT the notes - a note is written for the person who
#: maintains the source, and a card that appears because of a substring
#: nobody can see on screen reads as a search that does not work.
SEARCH_COLUMNS = ("s.text_name", "s.text_host", "s.text_list_url",
                  "s.text_list_url_canonical", "s.text_key_label")


def list_sources(db: Database, q: str = "") -> list[dict[str, Any]]:
    """The overview, by name, narrowed by the view's search box.

    The search is done here rather than in the browser so that what the page
    shows and what `/api/export/sources.csv` writes are the same rows: an
    export that quietly ignores the search would hand somebody a file they
    did not ask for. It is one ILIKE - the list is tens of rows, not
    millions, and a customer looks for `homegate` or `.ch`, not for a
    phrase.
    """
    term = (q or "").strip()
    params: dict[str, Any] = {}
    statement = LIST_SQL
    if term:
        params["q"] = like_substring(term)
        statement = LIST_SQL.replace(
            LIST_ORDER,
            "     WHERE (" + " OR ".join(f"{c} ILIKE %(q)s" for c in SEARCH_COLUMNS) + ")\n"
            + LIST_ORDER)
    with db.read() as conn:
        rows = conn.execute(statement, params or None).fetchall()
    items = [_overview_row(dict(r)) for r in rows]
    if items:
        archived = archived_counts(db)
        for item in items:
            item["counts"]["archived"] = archived.get(int(item["id"]), 0)
    return items


def count_sources(db: Database) -> int:
    """How many sources there are in total, whatever the search box says -
    the second half of "3 of 13 sources". Without it the page cannot tell
    "you have three sources" from "eleven are hidden by your search"."""
    with db.read() as conn:
        row = conn.execute("SELECT count(*) AS n FROM scraper_config.sources").fetchone()
    return int(row["n"]) if row else 0


def get_source(db: Database, source_id: int) -> dict[str, Any] | None:
    """One source with its children, in the shape the editor loads and
    `POST /api/sources/test` may hand straight back as a draft."""
    with db.read() as conn:
        row = conn.execute(
            sql.SQL("SELECT {cols} FROM scraper_config.sources s WHERE s.bigint_id = %s")
               .format(cols=sql.SQL(SOURCE_COLUMNS)), (source_id,)).fetchone()
        if row is None:
            return None
        source = dict(row)
        source["patterns"] = [dict(r) for r in conn.execute("""
            SELECT bigint_id AS id, text_kind, text_label, text_regex, json_form,
                   text_origin, bool_needs_review, text_example_url, integer_examples,
                   text_key_prefix
              FROM scraper_config.source_patterns
             WHERE bigint_fk_source = %s ORDER BY text_kind, text_label""", (source_id,))]
        source["exact_urls"] = [dict(r) for r in conn.execute("""
            SELECT bigint_id AS id, text_kind, text_url_canonical, text_key_prefix
              FROM scraper_config.source_exact_urls
             WHERE bigint_fk_source = %s ORDER BY text_kind, text_url_canonical""", (source_id,))]
        source["file_rules"] = [dict(r) for r in conn.execute("""
            SELECT bigint_id AS id, text_kind, text_value, bool_enabled, integer_max_mb
              FROM scraper_config.source_file_rules
             WHERE bigint_fk_source = %s ORDER BY text_kind, text_value""", (source_id,))]
        # In the order they were chosen, with the project's own name where the
        # registry knows it: the editor shows names, the crawler needs the ids.
        source["projects"] = [dict(r) for r in conn.execute("""
            SELECT sp.text_project_id, sp.text_key_prefix, sp.integer_order,
                   COALESCE(p.text_name, sp.text_project_id) AS text_project_name
              FROM scraper_config.source_projects sp
              LEFT JOIN scraper.projects p ON p.text_project_id = sp.text_project_id
             WHERE sp.bigint_fk_source = %s
             ORDER BY sp.integer_order, sp.text_project_id""", (source_id,))]
    return source


#: What a draft is not: the row's identity and its history. Left out of
#: `config_of()` so a stored draft is JSON on its own (a timestamp is not) and
#: so two reads of an unchanged source hash the same.
_NOT_CONFIG = ("id", "date_added", "date_updated")


def config_of(source: dict[str, Any]) -> dict[str, Any]:
    """The draft dictionary `crawlkit.Rules.from_config()` and the test runner
    read: the source's columns plus its children, nothing derived."""
    config = {k: v for k, v in source.items()
              if k in _SOURCE_KEYS and k not in _NOT_CONFIG}
    config["patterns"] = [{k: v for k, v in p.items() if k != "id"}
                          for p in source.get("patterns") or ()]
    config["exact_urls"] = [{k: v for k, v in u.items() if k != "id"}
                            for u in source.get("exact_urls") or ()]
    config["file_rules"] = [{k: v for k, v in f.items() if k != "id"}
                            for f in source.get("file_rules") or ()]
    # The name travels with the draft so a stored snapshot can still be read
    # back into a form that says which projects were chosen, even after the
    # registry has forgotten a project.
    config["projects"] = [dict(pr) for pr in source.get("projects") or ()]
    return config


#: The fields that decide WHAT a test fetched. When one of them has changed
#: since the newest snapshot, that snapshot is a picture of a different
#: question and the preview says so.
#:
#: The fields that decide what is KEPT - the mode, the patterns, the exact
#: addresses, the legal switch - are deliberately NOT here. Re-applying those
#: to the links a test already found is the whole purpose of the preview, and
#: flagging them as "changed" would put a warning on the normal way of
#: working: test, tick, learn, save, look.
FETCH_FIELDS = ("text_list_url", "text_engine", "text_wait_for_selector",
                "integer_wait_after_load_ms", "bool_follow_pagination",
                "text_paging_param", "text_next_selector", "integer_max_pages",
                "bool_respect_robots")


def changed_fetch_fields(tested: dict[str, Any], saved: dict[str, Any]) -> list[str]:
    """Which of `FETCH_FIELDS` the tested draft and the saved source disagree
    about. A field the draft never carried is not compared - a partial draft
    says nothing about it, and inventing a default would invent a change.

    >>> changed_fetch_fields({"text_list_url": "https://a.example/x"},
    ...                      {"text_list_url": "https://a.example/y"})
    ['text_list_url']
    >>> changed_fetch_fields({"text_mode": "exact"}, {"text_mode": "selected"})
    []
    """
    tested = tested or {}
    saved = saved or {}
    return [name for name in FETCH_FIELDS
            if name in tested and tested[name] != saved.get(name)]


# ── Writing ─────────────────────────────────────────────────────────────


def _replace_children(conn, source_id: int, patterns=None, exact_urls=None,
                      file_rules=None, projects=None) -> None:
    """Delete and re-insert, inside the caller's transaction. `None` means
    "not part of this save" and leaves the table alone; an empty list means
    "there are none", which is a change and is applied."""
    if projects is not None:
        # Deleting a project assignment does NOT delete what it collected: the
        # archive keeps every document it ever filed, and scraper.seen_urls
        # keeps knowing the addresses, so putting the project back does not
        # re-collect and re-pay for the back catalogue.
        conn.execute("DELETE FROM scraper_config.source_projects WHERE bigint_fk_source = %s",
                     (source_id,))
        for row in projects:
            conn.execute("""
                INSERT INTO scraper_config.source_projects
                    (bigint_fk_source, text_project_id, text_key_prefix, integer_order)
                VALUES (%(source)s, %(text_project_id)s, %(text_key_prefix)s,
                        %(integer_order)s)""",
                {**row, "source": source_id})
    if patterns is not None:
        conn.execute("DELETE FROM scraper_config.source_patterns WHERE bigint_fk_source = %s",
                     (source_id,))
        for row in patterns:
            conn.execute("""
                INSERT INTO scraper_config.source_patterns
                    (bigint_fk_source, text_kind, text_label, text_regex, json_form,
                     text_origin, bool_needs_review, text_example_url, integer_examples,
                     text_key_prefix)
                VALUES (%(source)s, %(text_kind)s, %(text_label)s, %(text_regex)s, %(json_form)s,
                        %(text_origin)s, %(bool_needs_review)s, %(text_example_url)s,
                        %(integer_examples)s, %(text_key_prefix)s)""",
                {**row, "source": source_id, "json_form": Jsonb(row["json_form"])})
    if exact_urls is not None:
        conn.execute("DELETE FROM scraper_config.source_exact_urls WHERE bigint_fk_source = %s",
                     (source_id,))
        for row in exact_urls:
            conn.execute("""
                INSERT INTO scraper_config.source_exact_urls
                    (bigint_fk_source, text_kind, text_url_canonical, text_key_prefix)
                VALUES (%(source)s, %(text_kind)s, %(text_url_canonical)s,
                        %(text_key_prefix)s)""",
                {**row, "source": source_id})
    if file_rules is not None:
        conn.execute("DELETE FROM scraper_config.source_file_rules WHERE bigint_fk_source = %s",
                     (source_id,))
        for row in file_rules:
            conn.execute("""
                INSERT INTO scraper_config.source_file_rules
                    (bigint_fk_source, text_kind, text_value, bool_enabled, integer_max_mb)
                VALUES (%(source)s, %(text_kind)s, %(text_value)s, %(bool_enabled)s,
                        %(integer_max_mb)s)""",
                {**row, "source": source_id})


def create_source(db: Database, payload: dict[str, Any]) -> int:
    fields = clean_fields(payload)
    for required in ("text_name", "text_list_url"):
        if not fields.get(required):
            raise SourceError(f"{required} is needed to save a source",
                              WRITABLE[required].hint)
    check_override(fields)
    fields.update(derived_url_fields(fields["text_list_url"]))
    patterns = clean_patterns(payload.get("patterns"))
    exact_urls = clean_exact_urls(payload.get("exact_urls"))
    file_rules = clean_file_rules(payload.get("file_rules"))
    projects = clean_projects(payload.get("projects"))

    columns = sorted(fields)
    statement = sql.SQL("INSERT INTO scraper_config.sources ({cols}) VALUES ({vals}) "
                        "RETURNING bigint_id AS id").format(
        cols=sql.SQL(", ").join(sql.Identifier(c) for c in columns),
        vals=sql.SQL(", ").join(sql.Placeholder(c) for c in columns))
    with _name_is_free(fields.get("text_name", "")):
        with db.write() as conn:
            row = conn.execute(statement, fields).fetchone()
            source_id = int(row["id"])
            _replace_children(conn, source_id, patterns, exact_urls, file_rules,
                              projects)
    return source_id


@contextmanager
def _name_is_free(name: str):
    """Turn `text_name` UNIQUE into a sentence.

    The editor asks the list it loaded before it saves, so the ordinary case
    is answered next to the field. It cannot be the only guard: a second tab,
    a rename between load and save, or a source added straight in the
    database all reach the constraint - and an unhandled UniqueViolation left
    the browser with a bare 500 and no JSON body, which is the one answer the
    page script cannot say anything about.
    """
    try:
        yield
    except psycopg.errors.UniqueViolation as exc:
        if "text_name" not in str(exc):
            raise
        raise SourceError(
            f"a source called {name!r} already exists" if name
            else "a source with this name already exists",
            "Names are how the log, the runs and the archived documents are "
            "read back, so every source needs its own. Give this one a "
            "different name, or open the one that has it.",
            status=409) from exc


def update_source(db: Database, source_id: int, payload: dict[str, Any]) -> bool:
    """Save the writable fields, and replace whichever child lists were sent.

    Returns False when there is no such source. The whole save is one
    transaction: a set of patterns that only half arrived would be a
    configuration nobody chose.
    """
    fields = clean_fields(payload)
    patterns = clean_patterns(payload["patterns"]) if "patterns" in payload else None
    exact_urls = clean_exact_urls(payload["exact_urls"]) if "exact_urls" in payload else None
    file_rules = clean_file_rules(payload["file_rules"]) if "file_rules" in payload else None
    projects = clean_projects(payload["projects"]) if "projects" in payload else None
    if "text_list_url" in fields:
        fields.update(derived_url_fields(fields["text_list_url"]))

    with _name_is_free(fields.get("text_name", "")), db.write() as conn:
        current = conn.execute(
            "SELECT bool_respect_robots, text_override_reason FROM scraper_config.sources "
            "WHERE bigint_id = %s", (source_id,)).fetchone()
        if current is None:
            return False
        check_override({**dict(current), **fields})
        if fields:
            statement = sql.SQL("UPDATE scraper_config.sources SET {sets}, date_updated = NOW() "
                                "WHERE bigint_id = %(id)s").format(
                sets=sql.SQL(", ").join(
                    sql.SQL("{c} = {p}").format(c=sql.Identifier(c), p=sql.Placeholder(c))
                    for c in sorted(fields)))
            conn.execute(statement, {**fields, "id": source_id})
        if (patterns is not None or exact_urls is not None
                or file_rules is not None or projects is not None):
            _replace_children(conn, source_id, patterns, exact_urls, file_rules,
                              projects)
            # The crawler only re-reads a source whose date_updated moved, and
            # a changed pattern that it never re-reads is the fault this line
            # prevents.
            conn.execute("UPDATE scraper_config.sources SET date_updated = NOW() "
                         "WHERE bigint_id = %s", (source_id,))
    return True


def delete_source(db: Database, source_id: int) -> bool:
    """Remove the configuration. The crawler's own rows (targets, seen
    addresses, documents) stay: they belong to the crawler, it clears them on
    its next sync, and reaching into them from here would cross the boundary
    that 03-scraper.sql draws."""
    with db.write() as conn:
        result = conn.execute("DELETE FROM scraper_config.sources WHERE bigint_id = %s",
                              (source_id,))
        return result.rowcount > 0


def set_enabled(db: Database, source_id: int, enabled: bool) -> bool:
    with db.write() as conn:
        result = conn.execute(
            "UPDATE scraper_config.sources SET bool_enabled = %s, date_updated = NOW() "
            "WHERE bigint_id = %s", (enabled, source_id))
        return result.rowcount > 0


def robots_verdict(db: Database, source_id: int) -> dict[str, Any]:
    """What is currently known about robots.txt for this source's list page.

    Two places know: the newest finished test (the dashboard asked) and
    `scraper.targets` (the crawler asked on its last run). The more recent
    answer wins - a source forbidden yesterday and allowed after the site
    changed its robots.txt this morning must be enablable.
    """
    with db.read() as conn:
        target = conn.execute(
            "SELECT bool_robots_allowed, text_robots_reason, date_robots_checked "
            "FROM scraper.targets WHERE bigint_fk_source = %s", (source_id,)).fetchone()
        snapshot = conn.execute("""
            SELECT bigint_id, date_finished, date_added, json_result -> 'robots' AS robots
              FROM scraper_config.test_snapshots
             WHERE bigint_fk_source = %s AND text_status = 'DONE'
             ORDER BY date_added DESC LIMIT 1""", (source_id,)).fetchone()

    answers: list[tuple[Any, bool, str, str]] = []
    if target and target.get("date_robots_checked") is not None:
        answers.append((target["date_robots_checked"], bool(target["bool_robots_allowed"]),
                        target.get("text_robots_reason") or "", "crawler"))
    if snapshot:
        allowed, reason = robots_of(snapshot.get("robots"))
        if allowed is not None:
            answers.append((snapshot.get("date_finished") or snapshot["date_added"],
                            allowed, reason, "test"))
    if not answers:
        return {"allowed": None, "reason": "", "checked": None, "from": "none"}
    when, allowed, reason, origin = max(answers, key=lambda a: a[0])
    return {"allowed": allowed, "reason": reason, "checked": when, "from": origin}


# ── Test snapshots ──────────────────────────────────────────────────────


def create_snapshot(db: Database, *, source_id: int | None, config: dict[str, Any],
                    kind: str = "crawl", sample_subpages: int = 5) -> int:
    if kind not in SNAPSHOT_KINDS:
        raise SourceError(f"kind must be one of {', '.join(SNAPSHOT_KINDS)}")
    sample = _as_int("sample_subpages", sample_subpages, 0, 50)
    with db.write() as conn:
        row = conn.execute("""
            INSERT INTO scraper_config.test_snapshots
                (bigint_fk_source, json_config, text_kind, integer_sample_subpages, text_status)
            VALUES (%s, %s, %s, %s, 'RUNNING')
            RETURNING bigint_id AS id""",
            (source_id, Jsonb(config), kind, sample)).fetchone()
    return int(row["id"])


def snapshot(db: Database, snapshot_id: int) -> dict[str, Any] | None:
    with db.read() as conn:
        row = conn.execute("""
            SELECT bigint_id AS id, bigint_fk_source AS source_id, date_added, date_started,
                   date_finished, text_kind, integer_sample_subpages, text_status,
                   json_config, json_result, text_error
              FROM scraper_config.test_snapshots WHERE bigint_id = %s""",
            (snapshot_id,)).fetchone()
    return dict(row) if row else None


#: The columns of a snapshot that are cheap to read. `json_config` and
#: `json_result` are not among them: the result of a test of a large list page
#: is a megabyte, and most callers want the date and the status.
SNAPSHOT_META = ("bigint_id AS id", "bigint_fk_source AS source_id", "date_added",
                 "date_finished", "text_kind", "text_status")

#: What the site answered, as three scalars out of the result rather than the
#: result itself - the same expressions the list query uses, so the two readers
#: of this table cannot drift apart on the one fact the pill is drawn from. A
#: snapshot carries up to two thousand links; none of them are read here.
SNAPSHOT_FETCH = ("json_result -> 'fetch' ->> 'status' AS snapshot_http",
                  "json_result -> 'fetch' ->> 'error' AS snapshot_fetch_error",
                  "json_result -> 'fetch' ->> 'challenge' AS snapshot_challenge")


def newest_snapshot(db: Database, source_id: int, *, status: str = "DONE",
                    light: bool = False) -> dict[str, Any] | None:
    """The newest snapshot of a source, DONE ones only unless `status` is ''.

    `light=True` leaves the draft and the result behind - what a page needs to
    say "tested two minutes ago" without reading what the test found.
    """
    columns = (list(SNAPSHOT_META) + list(SNAPSHOT_FETCH)
               + ([] if light else ["json_config", "json_result"]))
    query = sql.SQL("""
        SELECT {cols}
          FROM scraper_config.test_snapshots
         WHERE bigint_fk_source = %s AND (%s = '' OR text_status = %s)
         ORDER BY date_added DESC LIMIT 1""").format(
        cols=sql.SQL(", ").join(sql.SQL(c) for c in columns))
    with db.read() as conn:
        row = conn.execute(query, (source_id, status, status)).fetchone()
    return dict(row) if row else None


def attach_snapshot(db: Database, snapshot_id: int, source_id: int) -> None:
    """Point a draft's snapshot at the source it became. Only a snapshot that
    still belongs to nobody is moved: a test of source A must not end up on
    source B because the editor sent a stale id."""
    with db.write() as conn:
        conn.execute("UPDATE scraper_config.test_snapshots SET bigint_fk_source = %s "
                     "WHERE bigint_id = %s AND bigint_fk_source IS NULL",
                     (source_id, snapshot_id))


def delete_snapshot(db: Database, snapshot_id: int) -> None:
    with db.write() as conn:
        conn.execute("DELETE FROM scraper_config.test_snapshots WHERE bigint_id = %s",
                     (snapshot_id,))


#: The result column is JSONB in one row; a list page with ten thousand links
#: would make it megabytes. 03-scraper.sql says links are capped at 2000, and
#: this is where the cap happens - with a warning, so the page can say that
#: what it shows is not everything.
MAX_STORED_LINKS = 2000


def trim_result(result: dict[str, Any]) -> dict[str, Any]:
    """>>> r = trim_result({"links": [{"url": f"u{i}"} for i in range(2500)], "warnings": []})
    >>> len(r["links"]), r["warnings"]
    (2000, ['2500 links found; the 2000 shown here are the first of them'])
    """
    if not isinstance(result, dict):
        return {"warnings": [f"the test returned {type(result).__name__}, not an object"]}
    links = result.get("links")
    if isinstance(links, list) and len(links) > MAX_STORED_LINKS:
        warnings = list(result.get("warnings") or ())
        warnings.append(f"{len(links)} links found; the {MAX_STORED_LINKS} shown here "
                        "are the first of them")
        return {**result, "links": links[:MAX_STORED_LINKS], "warnings": warnings}
    return result


@dataclass
class SnapshotStore:
    """The four writes the test runner makes, and the job it reads.

    A class rather than four functions so the runner can be handed something
    else in a unit test - the thread, the queue and the timeout are worth
    testing without a database.

    The pool is looked up per call rather than kept: the runner outlives a
    single request and, in a test that starts the application twice, the pool
    it was built with is closed while the runner is not.
    """

    db: Database | None = None

    @property
    def pool(self) -> Database:
        return self.db or get_db()

    def job(self, snapshot_id: int) -> dict[str, Any] | None:
        with self.pool.read() as conn:
            row = conn.execute("""
                SELECT bigint_id AS id, bigint_fk_source AS source_id, json_config,
                       text_kind, integer_sample_subpages, text_status
                  FROM scraper_config.test_snapshots WHERE bigint_id = %s""",
                (snapshot_id,)).fetchone()
        return dict(row) if row else None

    def start(self, snapshot_id: int) -> None:
        with self.pool.write() as conn:
            conn.execute("UPDATE scraper_config.test_snapshots SET date_started = NOW() "
                         "WHERE bigint_id = %s", (snapshot_id,))

    def progress(self, snapshot_id: int, text: str) -> None:
        """The line the poll endpoint shows while the crawl runs.

        Kept in json_result rather than in a column of its own: the row has
        one place for what the test produced, and a progress line is the first
        thing it produces. The finished result carries the steps over.

        The list cannot run away: the same line is never written twice (the
        runner drops repeats), and a test reads at most three list pages plus
        `integer_sample_subpages` subpages, which the CHECK caps at fifty.
        """
        with self.pool.write() as conn:
            conn.execute("""
                UPDATE scraper_config.test_snapshots
                   SET json_result = jsonb_build_object(
                           'progress', %s::text,
                           'steps', COALESCE(json_result -> 'steps', '[]'::jsonb)
                                    || to_jsonb(%s::text))
                 WHERE bigint_id = %s AND text_status = 'RUNNING'""",
                (text, text, snapshot_id))

    def finish(self, snapshot_id: int, result: dict[str, Any]) -> None:
        """Store the result, keeping the steps the test reported on its way.

        The step list is merged back on top of the engine's answer rather than
        thrown away with the progress line: "what did this test actually do"
        is the first question about a result that surprises somebody, and the
        answer is three hundred bytes.
        """
        with self.pool.write() as conn:
            conn.execute("""
                UPDATE scraper_config.test_snapshots
                   SET text_status = 'DONE', date_finished = NOW(),
                       json_result = %s || jsonb_build_object(
                           'steps', COALESCE(json_result -> 'steps', '[]'::jsonb)),
                       text_error = NULL
                 WHERE bigint_id = %s""", (Jsonb(trim_result(result)), snapshot_id))

    def fail(self, snapshot_id: int, error: str) -> None:
        with self.pool.write() as conn:
            conn.execute("""
                UPDATE scraper_config.test_snapshots
                   SET text_status = 'FAILED', date_finished = NOW(), text_error = %s
                 WHERE bigint_id = %s""", (str(error)[:2000], snapshot_id))


# ── What the crawler did with a source ──────────────────────────────────


def runs(db: Database, source_id: int, limit: int = PAGE_SIZE,
         offset: int = 0) -> list[dict[str, Any]]:
    with db.read() as conn:
        rows = conn.execute("""
            SELECT date_added AS date, text_status AS status, integer_pages AS pages,
                   integer_links AS links, integer_accepted AS accepted, integer_new AS new,
                   integer_files AS files, integer_submitted AS submitted,
                   integer_ms AS ms, text_message AS message
              FROM monitoring.scraper_runs
             WHERE bigint_fk_source = %s
             ORDER BY date_added DESC LIMIT %s OFFSET %s""",
            (source_id, limit, offset)).fetchall()
    return [dict(r) for r in rows]


def documents(db: Database, source_id: int, limit: int = PAGE_SIZE,
              offset: int = 0) -> list[dict[str, Any]]:
    """The newest documents of a source - without their content.

    text_content is what was sent to the API and can be two million
    characters; the list shows how many there were, and the archive shows the
    text itself.
    """
    with db.read() as conn:
        rows = conn.execute("""
            SELECT d.date_added AS date, d.bigint_id AS id, d.text_name AS name,
                   d.text_uri_canonical AS uri, d.text_kind AS kind,
                   d.text_file_type AS file_type, d.text_format AS format,
                   d.integer_char_count AS characters, d.integer_http_status AS http_status,
                   d.text_content_hash AS content_hash, d.text_tag AS tag,
                   d.text_project AS project, d.text_language AS language,
                   d.text_task_id AS task_id, q.text_status AS queue_status
              FROM scraper.documents d
              LEFT JOIN scraper.submit_queue q ON q.text_tag = d.text_tag
             WHERE d.bigint_fk_source = %s
             ORDER BY d.date_added DESC LIMIT %s OFFSET %s""",
            (source_id, limit, offset)).fetchall()
    return [dict(r) for r in rows]


def files(db: Database, source_id: int, limit: int = PAGE_SIZE,
          offset: int = 0) -> list[dict[str, Any]]:
    with db.read() as conn:
        rows = conn.execute("""
            SELECT bigint_id AS id, date_updated AS date, text_page_uri AS page_uri,
                   text_file_uri_canonical AS uri, text_file_type AS file_type,
                   text_status AS status, integer_bytes AS bytes, text_detail AS detail
              FROM scraper.files
             WHERE bigint_fk_source = %s
             ORDER BY date_updated DESC LIMIT %s OFFSET %s""",
            (source_id, limit, offset)).fetchall()
    return [dict(r) for r in rows]


def crawler_online(db: Database,
                   within_seconds: int = heartbeats.WINDOW_SECONDS) -> dict[str, Any]:
    """Is a crawler alive? The pill on the overview, and nothing else - every
    other thing on the page works without one.

    The reading itself is in app/heartbeats.py, shared with the collector's
    pill: one table, one three-minute window, and one place to change it.
    This is the Watchlist's name for the same answer.

    IT NAMES A SERVICE, NOT A WORKER. Keyed on a configurable worker name, a
    renamed container would write a row the pill never looks at;
    `monitoring.heartbeat` is keyed on the SERVICE, which is what the
    question "is the crawler running" is actually about.
    """
    return heartbeats.service(db, "crawler", within_seconds)
