"""The landing view: how much arrived, what arrived last, and what happened.

Three endpoints, one per block of the page (app/templates/dashboard.html):

    GET /api/dashboard/stats          the counters per archive table
    GET /api/dashboard/activities     the newest rows across the tables
    GET /api/dashboard/latest-events  the newest events with their entities
    GET /api/dashboard/importances    the per-perspective importance judgements
    POST /api/dashboard/importances/drilldown      the rows behind one bar

The expensive one is the first, and the shape of its SQL is a decision worth
knowing about. Each table is counted in ONE statement with five FILTER
clauses (sqlbuild.stats_statement) rather than five statements, and the
statement carries `date_added >= now() - interval '1 year'`: the hypertables
are partitioned by date_added, so that predicate lets TimescaleDB skip whole
chunks instead of reading the archive from the beginning of time. There is
deliberately NO unbounded total - on a five-year archive "how many rows are
there in all" is a full scan for a number nobody acts on, and the old
dashboard's home page was slow for exactly that reason.

The counters are cached for a minute per (project, language). A dashboard
left open on a wall does not need to re-count every fifteen seconds, and a
person who reloads because a number looks wrong gets the same number twice,
which is what makes it believable.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query
from psycopg import sql
from pydantic import BaseModel, Field

from ..charts import drilldown as dd
from ..context import Context, ContextDep
from ..db import Database, get_db
from ..scope import resolve_scope
from .. import vocabulary
from ..source_names import source_heading, source_payload
from ..sqlbuild import identity, identity_params, stats_statement, window_params, window_predicate
from ..timeframes import window_for

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])

# The tables the tiles show, the label a person reads, and the Diagrams tab
# the tile links to. Order is the order on the page: what was read, then
# what was found in it, then what was said about it.
STAT_TABLES: tuple[tuple[str, str, str], ...] = (
    ("sources", "Sources", "sources"),
    ("entities", "Entities", "entities"),
    ("connections", "Connections", "connections"),
    ("locations", "Locations", "locations"),
    ("events", "Events", "events"),
    ("ratings", "Ratings", "ratings"),
    ("attributes", "Attributes", "attributes"),
    ("market_insights", "Market Insights", "market"),
)

PERIODS = ("hour", "day", "week", "month", "year")

STATS_CACHE_SECONDS = 60
ACTIVITIES_PER_TABLE = 10
ACTIVITIES_DEFAULT = 12
LATEST_EVENTS_DEFAULT = 6


# ── The counters ─────────────────────────────────────────────

_lock = threading.Lock()
_stats_cache: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}


def _count_tables(db: Database, ctx: Context) -> dict[str, Any]:
    params = identity_params(ctx.project, ctx.language)
    tables = []
    with db.read() as conn:
        for name, label, tab in STAT_TABLES:
            row = conn.execute(stats_statement(name), params).fetchone()
            tables.append({"id": name, "label": label, "tab": tab,
                           **{p: int(row[p] or 0) for p in PERIODS}})
    return {"tables": tables, "project": ctx.project, "language": ctx.language,
            "cached_at": datetime.now(timezone.utc).isoformat()}


@router.get("/stats")
def stats(ctx: ContextDep, db: Database = Depends(get_db),
          fresh: bool = Query(False, description="skip the one-minute cache")):
    key = (ctx.project, ctx.language)
    with _lock:
        hit = _stats_cache.get(key)
        if hit and not fresh and time.monotonic() - hit[0] < STATS_CACHE_SECONDS:
            return hit[1]
    data = _count_tables(db, ctx)
    with _lock:
        _stats_cache[key] = (time.monotonic(), data)
    return data


def invalidate_stats() -> None:
    """For tests and for anything that knows the archive has just changed."""
    with _lock:
        _stats_cache.clear()


# ── The activity feed ────────────────────────────────────────
#
# One branch per table, each with its own ORDER BY date_added DESC LIMIT n
# BEFORE the union: that is the whole trick. date_added is the hypertable's
# partitioning column, so every branch is a short reverse scan of the newest
# chunk, and the union then sorts a few dozen rows instead of millions.
#
# ORDERING BY date_added AND SHOWING date_commissioned is the documented
# exception to "never chart on date_added": the feed answers "what arrived
# last", which IS an arrival question, while the date a person cares about
# is still the one the document carries.
#
# Every branch produces the same five text-ish columns:
#
#   kind          the table, for the tag and the link
#   name          the row's own headline
#   detail        a second line, already composed in SQL
#   uri           the document's address, for the source rows only
#   entity_id     the entity the row is about, when it has one
#   entity_id_2   the second entity of a connection (parent → child)
#
# `uri` is NULL in seven of the eight branches and carries the whole weight
# in the eighth: `sources.text_name` is an identifier in every row this
# archive has (app/source_names.py), so a source row is named from its
# address instead, and the address has to come out of SQL with it.
#
# The entity ids are turned into names afterwards (one small query for all
# of them): a rating row carries "ent:apple-inc", and nobody reads that.

_ACTIVITY_BRANCHES: dict[str, str] = {
    "sources": """
        SELECT 'sources' AS kind, s.text_name AS name,
               nullif(s.text_summary, '') AS detail, s.text_uri AS uri,
               NULL::text AS entity_id, NULL::text AS entity_id_2,
               s.text_task_id AS task_id, s.date_commissioned, s.date_added
          FROM processed_data.sources s
         WHERE s.text_project = %(project)s AND s.text_language = %(language)s
         ORDER BY s.date_added DESC LIMIT %(per_table)s
    """,
    "entities": """
        SELECT 'entities', e.text_name, nullif(e.text_type, ''), NULL::text,
               NULL::text, NULL::text,
               e.text_task_id, e.date_commissioned, e.date_added
          FROM processed_data.entities e
         WHERE e.text_project = %(project)s AND e.text_language = %(language)s
         ORDER BY e.date_added DESC LIMIT %(per_table)s
    """,
    "locations": """
        SELECT 'locations', coalesce(nullif(l.text_address, ''), l.text_name),
               nullif(l.text_type, ''), NULL::text, nullif(l.text_fk_entity_id, ''), NULL::text,
               l.text_task_id, l.date_commissioned, l.date_added
          FROM processed_data.locations l
         WHERE l.text_project = %(project)s AND l.text_language = %(language)s
         ORDER BY l.date_added DESC LIMIT %(per_table)s
    """,
    "events": """
        SELECT 'events', ev.text_name, nullif(ev.text_type, ''), NULL::text,
               NULL::text, NULL::text,
               ev.text_task_id, ev.date_commissioned, ev.date_added
          FROM processed_data.events ev
         WHERE ev.text_project = %(project)s AND ev.text_language = %(language)s
         ORDER BY ev.date_added DESC LIMIT %(per_table)s
    """,
    "ratings": """
        SELECT 'ratings', r.text_rating_name,
               concat_ws(', ', nullif(r.text_rating_value, ''), nullif(r.text_perspective, '')),
               NULL::text, nullif(r.text_fk_entity_id, ''), NULL::text,
               r.text_task_id, r.date_commissioned, r.date_added
          FROM processed_data.ratings r
         WHERE r.text_project = %(project)s AND r.text_language = %(language)s
         ORDER BY r.date_added DESC LIMIT %(per_table)s
    """,
    "connections": """
        SELECT 'connections', c.text_type_parent_to_child,
               nullif(c.text_type_child_to_parent, ''), NULL::text,
               nullif(c.text_fk_parent_entity_id, ''), nullif(c.text_fk_child_entity_id, ''),
               c.text_task_id, c.date_commissioned, c.date_added
          FROM processed_data.connections c
         WHERE c.text_project = %(project)s AND c.text_language = %(language)s
         ORDER BY c.date_added DESC LIMIT %(per_table)s
    """,
    "attributes": """
        SELECT 'attributes', a.text_name,
               concat_ws(' ', nullif(a.text_value, ''), nullif(a.text_unit, '')),
               NULL::text, nullif(a.text_fk_entity_id, ''), NULL::text,
               a.text_task_id, a.date_commissioned, a.date_added
          FROM processed_data.attributes a
         WHERE a.text_project = %(project)s AND a.text_language = %(language)s
         ORDER BY a.date_added DESC LIMIT %(per_table)s
    """,
    "market_insights": """
        SELECT 'market_insights', m.text_topic,
               concat_ws(', ', 'Short-term ' || lower(m.text_short_term_outlook),
                                'long-term ' || lower(m.text_long_term_outlook)),
               NULL::text, nullif(m.text_fk_entity_id, ''), NULL::text,
               m.text_task_id, m.date_commissioned, m.date_added
          FROM processed_data.market_insights m
         WHERE m.text_project = %(project)s AND m.text_language = %(language)s
         ORDER BY m.date_added DESC LIMIT %(per_table)s
    """,
}

# The tag on each row and where clicking it leads. The Diagrams pages take a
# search term in `q`; the Events page takes a type.
_ACTIVITY_LABELS: dict[str, str] = {
    "sources": "Source", "entities": "Entity", "locations": "Location",
    "events": "Event", "ratings": "Rating", "connections": "Connection",
    "attributes": "Attribute", "market_insights": "Market Insights",
}

_ENTITY_NAMES_SQL = """
    SELECT e.text_task_id AS task_id, e.text_entity_id AS id, min(e.text_name) AS name
      FROM processed_data.entities e
     WHERE e.text_project = %(project)s AND e.text_language = %(language)s
       AND (e.text_task_id, e.text_entity_id) IN (SELECT * FROM unnest(%(tasks)s::text[], %(ids)s::text[]))
     GROUP BY e.text_task_id, e.text_entity_id
"""


def _activity_link(kind: str, name: str, detail: str | None, about: list[str],
                   ctx: Context, term: str | None = None) -> str | None:
    """Where a row leads. A row about an entity leads to that entity's
    diagrams, because that is the page that explains it; a row that is only
    about itself leads to the page listing its own kind.

    `term` is what the link SEARCHES, when that is not what the row is
    called. A document is called "news.example.com - Council approves …",
    and that whole string matches nothing: /diagrams/source searches the name,
    the host and the address (app/scope.py, `_source_body`), so the link
    carries the HOST - one of the three, and the one a reader would have
    typed themselves."""
    params = dict(ctx.params)
    if kind == "sources":
        params["q"] = term or name
        return "/diagrams/source?" + urlencode(params)
    if kind == "locations":
        params["q"] = name
        return "/diagrams/location?" + urlencode(params)
    if kind == "events":
        if not detail:
            return None
        params.update({"by": "type", "q": detail})
        return "/events?" + urlencode(params)
    if kind == "entities":
        params["q"] = name
        return "/diagrams/entity?" + urlencode(params)
    if about:
        params["q"] = about[0]
        return "/diagrams/entity?" + urlencode(params)
    return None


@router.get("/activities")
def activities(ctx: ContextDep, db: Database = Depends(get_db),
               limit: int = Query(ACTIVITIES_DEFAULT, ge=1, le=50)):
    # Each branch is parenthesised: a branch of a UNION carries its own
    # ORDER BY and LIMIT only inside brackets, and those two are the whole
    # reason the feed is fast.
    branches = sql.SQL(" UNION ALL ").join(
        sql.SQL("({b})").format(b=sql.SQL(b)) for b in _ACTIVITY_BRANCHES.values())
    # Rows of one document all arrive in the same microsecond, so the tie is
    # broken by the order the branches are written in - the document first,
    # then what was found in it, then what was said about it - and by name
    # after that, so two runs never show the same feed in a different order.
    statement = sql.SQL(
        "SELECT * FROM ({b}) AS feed "
        "ORDER BY date_added DESC, array_position(%(feed_order)s::text[], kind), name "
        "LIMIT %(limit)s"
    ).format(b=branches)
    params = {**identity_params(ctx.project, ctx.language),
              "feed_order": list(_ACTIVITY_BRANCHES),
              "per_table": ACTIVITIES_PER_TABLE, "limit": limit}

    with db.read() as conn:
        rows = [dict(r) for r in conn.execute(statement, params).fetchall()]
        pairs = sorted({(r["task_id"], eid) for r in rows
                        for eid in (r["entity_id"], r["entity_id_2"]) if eid})
        names: dict[tuple[str, str], str] = {}
        if pairs:
            lookup = conn.execute(_ENTITY_NAMES_SQL, {
                **identity_params(ctx.project, ctx.language),
                "tasks": [p[0] for p in pairs], "ids": [p[1] for p in pairs]}).fetchall()
            names = {(r["task_id"], r["id"]): r["name"] for r in lookup}

    items = []
    for r in rows:
        about = [names.get((r["task_id"], eid), eid)
                 for eid in (r["entity_id"], r["entity_id_2"]) if eid]
        name = r["name"] or ""
        derived = False
        term = None
        if r["kind"] == "sources":
            # NEVER THE IDENTIFIER. `sources.text_name` is `src_1127664` or
            # an md5 in every row of this archive, so the feed said "Source
            # src_1127664" twenty times and named nothing. The domain and
            # the address's own slug are what a person can read.
            head = source_heading(name, r["uri"])
            name, derived = head["text"], head["derived"]
            # What the link searches: the host if there is one, else the
            # address. Both are terms /diagrams/source resolves; the two
            # halves joined by a middle dot are not.
            term = head["domain"] or (r["uri"] or "") or name
        items.append({
            "table": r["kind"],
            "label": _ACTIVITY_LABELS.get(r["kind"], r["kind"]),
            "name": name,
            # True when the words above came out of the address rather than
            # out of the archive, so the view can say so instead of letting
            # a derivation read as a quotation.
            "derived": derived,
            "detail": r["detail"] or "",
            "about": about,
            "date_commissioned": r["date_commissioned"].isoformat() if r["date_commissioned"] else None,
            "link": _activity_link(r["kind"], name, r["detail"], about, ctx, term),
        })
    return {"items": items, "project": ctx.project, "language": ctx.language}


# ── The newest events ────────────────────────────────────────
#
# Ordered by date_added, like the feed: this block answers "what has just
# been archived", and an event dated next month would otherwise sit at the
# top for a month. The date shown is still the event's own.
#
# The entities come from event_entities, which joins its event on
# (text_task_id, bigint_fk_event_id) - the extraction gives an event no id of
# its own, so the row number inside the task is the key.

_LATEST_EVENTS_SQL = """
    WITH latest AS (
        SELECT ev.bigint_id, ev.text_task_id, ev.text_name, ev.text_type, ev.text_description,
               ev.text_fk_source_id, ev.date_eventdate, ev.date_commissioned, ev.date_added
          FROM processed_data.events ev
         WHERE ev.text_project = %(project)s AND ev.text_language = %(language)s
         -- The id breaks the tie: two events of one document arrive in the
         -- same microsecond, and a list whose order changes between two
         -- reloads looks like data that changed.
         ORDER BY ev.date_added DESC, ev.bigint_id DESC
         LIMIT %(limit)s
    )
    SELECT l.*, src.text_name AS source_name, src.text_uri AS source_uri,
           coalesce(ents.items, '[]'::jsonb) AS entities
      FROM latest l
      LEFT JOIN LATERAL (
            SELECT s.text_name, s.text_uri
              FROM processed_data.sources s
             WHERE s.text_project = %(project)s AND s.text_language = %(language)s
               AND s.text_task_id = l.text_task_id AND s.text_source_id = l.text_fk_source_id
             LIMIT 1
      ) AS src ON true
      LEFT JOIN LATERAL (
            SELECT jsonb_agg(DISTINCT jsonb_build_object(
                       'id', e.text_entity_id, 'name', e.text_name, 'type', e.text_type)) AS items
              FROM processed_data.event_entities ee
              JOIN processed_data.entities e
                ON e.text_project = ee.text_project AND e.text_language = ee.text_language
               AND e.text_task_id = ee.text_task_id AND e.text_entity_id = ee.text_fk_entity_id
             WHERE ee.text_project = %(project)s AND ee.text_language = %(language)s
               AND ee.text_task_id = l.text_task_id AND ee.bigint_fk_event_id = l.bigint_id
      ) AS ents ON true
     ORDER BY l.date_added DESC, l.bigint_id DESC
"""


@router.get("/latest-events")
def latest_events(ctx: ContextDep, db: Database = Depends(get_db),
                  limit: int = Query(LATEST_EVENTS_DEFAULT, ge=1, le=25)):
    params = {**identity_params(ctx.project, ctx.language), "limit": limit}
    with db.read() as conn:
        rows = conn.execute(_LATEST_EVENTS_SQL, params).fetchall()

    events = []
    for r in rows:
        when = r["date_eventdate"] or r["date_commissioned"]
        events.append({
            "id": f"{r['text_task_id']}:{r['bigint_id']}",
            "name": r["text_name"] or "",
            "type": r["text_type"] or "",
            "description": r["text_description"] or "",
            "date": when.isoformat() if when else None,
            "dated": bool(r["date_eventdate"]),
            # The document this event came out of, named from what exists:
            # its domain and the slug of its address. `text_name` is an
            # identifier here as everywhere else (app/source_names.py).
            "source": source_payload(r["source_name"], r["source_uri"]),
            "entities": [
                {"id": e.get("id") or "", "name": e.get("name") or "", "type": e.get("type") or ""}
                for e in (r["entities"] or []) if e.get("name")
            ],
        })
    return {"events": events, "project": ctx.project, "language": ctx.language}


# ── Importances by perspective ────────────────────────────────
#
# `processed_data.source_importances_by_perspective` is the same question -
# "how much does this document matter?" - asked once per perspective a
# customer watches: for Maintenance a filing can be critical and for
# Compliance beside the point. It is the one table in the archive that is
# ABOUT the reader rather than about the world, which is why it belongs on
# the landing page and not only on a Diagrams tab.
#
# WHAT THE PICTURE ANSWERS: for each perspective, how many judgements of
# each KIND - Critical, High, Medium, Low, Not Important. One bar per
# perspective, stacked by level, so the height says how much was judged for
# that reader and the stack says how much of it mattered. A chart of
# perspectives over time answered "who got judgements", which is the
# question nobody has: the perspectives are configured, so they are all
# there every day.
#
# TWO PERIODS, TWO PICTURES: the last 24 hours is what the collector has
# just done, the last 7 days is whether that is a run or a week. They are
# the archive's own timeframes, so the window, its caption and its empty
# periods are the ones every other chart in this dashboard uses.

#: The two charts, in the order they are drawn: the id in the URL and in
#: the template's data attribute, the archive's timeframe, and the words.
IMPORTANCE_GRAINS = {
    "24h": ("24h", "Last 24 hours", "How much the last day's documents were judged to matter, per perspective."),
    "7d": ("7d", "Last 7 days", "The same over seven days, so one quiet run does not read as a trend."),
}

#: Which perspective a judgement was for, and how much it said the document
#: matters, as two expressions.
#
# ONE SPELLING, TWO USES. The bars group by them and the drilldown filters
# by them (_importances_plan below), so a bar that says 14 and a dialog that
# lists 14 rows cannot come apart - which is what happens when a chart and
# its listing each write their own version of "not stated".
PERSPECTIVE = sql.SQL("coalesce(nullif(t.text_perspective, ''), 'Not stated')")
IMPORTANCE = sql.SQL("coalesce(nullif(t.text_importance, ''), 'Unset')")

_IMPORTANCES_SQL = """
    SELECT {category} AS category,
           {series} AS series,
           count(*) AS value
      FROM processed_data.source_importances_by_perspective t
     WHERE {idn} AND {window}
     GROUP BY 1, 2
     ORDER BY 1, 2
"""


@router.get("/importances")
def importances(ctx: ContextDep, db: Database = Depends(get_db),
                grain: str = Query("24h")):
    """One bar per perspective, one colour per importance level.

    The levels come back in the archive's own order (app/vocabulary.py),
    strongest last, so the stacks read the same way up on both charts and
    the same way up as every importance chart on the Diagrams pages. A level
    the vocabulary does not know is kept and put after them rather than
    dropped: a bar that does not add up to its own total is worse than an
    unexpected word in a legend.
    """
    key = grain if grain in IMPORTANCE_GRAINS else "24h"
    timeframe, label, description = IMPORTANCE_GRAINS[key]
    window = window_for(timeframe)
    statement = sql.SQL(_IMPORTANCES_SQL).format(
        category=PERSPECTIVE,
        series=IMPORTANCE,
        idn=identity("t"),
        window=window_predicate("source_importances_by_perspective", window))
    params = {**identity_params(ctx.project, ctx.language), **window_params(window)}
    with db.read() as conn:
        rows = [dict(r) for r in conn.execute(statement, params).fetchall()]

    perspectives = sorted({r["category"] for r in rows})
    at = {name: i for i, name in enumerate(perspectives)}
    order = list(vocabulary.IMPORTANCE_LEVELS)
    found = {r["series"] for r in rows}
    levels = [name for name in order if name in found]
    levels += sorted(found - set(order))
    series = {name: [0] * len(perspectives) for name in levels}
    for row in rows:
        index = at.get(row["category"])
        if index is not None:
            series[row["series"]][index] += int(row["value"] or 0)

    return {
        "grain": key,
        "label": label,
        "description": description,
        "caption": window.caption,
        "labels": perspectives,
        # The step each level sits on in the archive's scale, so the page can
        # draw it on the violet relevance ramp rather than in an arbitrary
        # colour: importance is how much something matters, never good or bad.
        "datasets": [{"id": name, "label": name, "data": series[name],
                      "step": order.index(name) if name in order else None}
                     for name in levels],
        "steps": len(order),
        "total": sum(int(r["value"] or 0) for r in rows),
    }


# ── The rows behind one bar ──────────────────────────────────
#
# THE SAME DRILLDOWN AS DIAGRAMS, and deliberately the same MODULE: the
# dialog, the columns, the links and the CSV all come from
# app/charts/drilldown.py, so a row listed from the landing page is the same
# row, in the same shape, as one listed from a chart. A second listing
# written here would be a second thing to keep in step, and the two would
# disagree the first time a column changed.
#
# What is NOT shared is the registry. These two charts belong to this page
# and not to a Diagrams tab, so they carry their own `Plan` instead of a
# ChartSpec - which is all the machinery below the registry ever needed.
#
# The scope is the summary's, unsearched: the landing page is about the whole
# project, and there is no box on it to narrow that with.
# NO CSV BEHIND THESE BARS, WHERE THE DIAGRAMS DRILLDOWN HAS ONE.
#
# A listed row carries the name and address of the document it came from,
# and that lookup costs a fifth of a second: `processed_data.sources` is
# chunked by the day, so finding one row plans 560 chunks and reads eighty
# thousand blocks (docs/DESIGN.md has the measurement). Twenty of them is a
# dialog that opens; a whole segment of a whole-project bar is hundreds, and
# the file would take minutes with a button that looks like it has hung.
#
# So the dialog pages through them and there is no file. When the archive's
# chunk interval is widened this is one argument back.


def _importances_plan() -> dd.Plan:
    """The chart above, as the plan its listing is built from: the bar is a
    perspective, the stack inside it is an importance level."""
    return dd.Plan(table="source_importances_by_perspective",
                   category=PERSPECTIVE, series=IMPORTANCE)


def _importance_rows(conn, ctx: Context, grain: str, perspective: str,
                     level: str, limit: int, offset: int, count: bool = True):
    """The judgements behind one segment: one perspective, one level.

    TWO STATEMENTS, NOT ONE, and this is the page where that is the right
    way round. The landing view is the whole project, so one segment can be
    hundreds of rows - and the running total that normally rides on the
    listing would make it look up the document behind every one of them
    before the LIMIT could drop all but twenty (charts/drilldown.py:
    rows_statement says what that costs). The count runs without those
    lookups instead and is over in a moment.
    """
    key = grain if grain in IMPORTANCE_GRAINS else "24h"
    window = window_for(IMPORTANCE_GRAINS[key][0])
    scope = resolve_scope(conn, ctx, "summary", "")
    plan = _importances_plan()
    try:
        parsed = dd.normalise_key("x", {"x": perspective})
        # `allowed=()` on purpose: the datasets of this chart are the
        # importance levels the archive actually holds, and a project may
        # spell one this file has never heard of. The value is bound as a
        # parameter, never spliced (charts/drilldown.py: dataset_predicate).
        statement, key_params = dd.rows_statement(
            plan, "key", scope, window, parsed, level, (), with_total=False)
        counter, _ = dd.count_statement(
            plan, "key", scope, window, parsed, level, ())
    except dd.DrilldownError as exc:
        raise HTTPException(400, {"error": str(exc), "hint": exc.hint})
    params = {**dd.params_for(plan, scope, ctx, window), **key_params,
              "dd_limit": limit, "dd_offset": offset}
    rows = conn.execute(statement, params).fetchall()
    total = int(conn.execute(counter, params).fetchone()["n"]) if count else 0
    return plan, window, parsed, rows, total


class ImportanceDrilldown(BaseModel):
    grain: str = "24h"
    #: The bar that was clicked - one perspective.
    perspective: str
    #: The stack the click landed in - one importance level. "" is the whole bar.
    level: str = ""
    #: 1 is the first page; static/js/drilldown.js counts from there.
    ddpage: int = Field(1, ge=1, le=1000)


@router.post("/importances/drilldown")
def importances_drilldown(request: ImportanceDrilldown, ctx: ContextDep,
                          db: Database = Depends(get_db)):
    """The judgements behind one segment of one bar."""
    offset = (request.ddpage - 1) * dd.PAGE_SIZE
    with db.read() as conn:
        # One row more than a page, so "is there more" is known without
        # asking a second time.
        plan, window, key, rows, total = _importance_rows(
            conn, ctx, request.grain, request.perspective, request.level,
            dd.PAGE_SIZE + 1, offset)
    more = len(rows) > dd.PAGE_SIZE
    shown = rows[:dd.PAGE_SIZE]
    return {
        "grain": request.grain, "perspective": request.perspective,
        "level": request.level,
        "window": window.as_dict(),
        "ddpage": request.ddpage, "page_size": dd.PAGE_SIZE, "more": more,
        "offset": offset, "total": total,
        "columns": dd.columns_json(plan.table),
        "row_links": dd.row_links(plan.table),
        "rows": [dd.shape_row(plan.table, row) for row in shown],
    }
