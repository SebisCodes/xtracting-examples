"""The Diagrams view: eight tabs of charts about one thing.

    GET  /api/diagrams/catalogue          what charts exist, per tab
    GET  /api/diagrams/{scope}/{tab}      the charts of one tab, drawn
    GET  /api/diagrams/{scope}/map        where those rows are, for the two
                                          tabs that have a place on the world
    POST /api/diagrams/drilldown          the rows behind one clicked point
    GET  /api/diagrams/drilldown.csv      the same rows, as a file

Six scopes (summary, entity, source, location, market, events) times eight
tabs, and ONLY THE TAB THAT IS OPEN IS ASKED FOR. Fifty-six charts over a
real archive is a minute of database work, seven eighths of it for tabs
nobody is looking at.

The term is resolved ONCE per request (app/scope.py), not once per chart, so
every chart of one page agrees about what was found - and the answer says so
in `resolved`, because "Apple" as a bucket of two companies is a different
question from "Apple" as one entity and the reader has to be able to tell
which they got. On the summary the term is put to ALL FIVE other kinds at
once and the page is the union of what they answer, so `resolved.kinds` says
which of them contributed; an empty term there is the whole project.

One chart that fails does not take the tab with it: each statement runs
inside its own savepoint, and a chart whose query breaks comes back empty
with a note. A timeout is the exception - it is about the whole request, and
main.py answers it with 504 and something to do about it.
"""

from __future__ import annotations

import csv
import io
import logging
from datetime import datetime, timezone
from typing import Any

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field

from .. import charts as catalogue_module
from .. import colours, timeframes
from ..charts import drilldown as dd
from ..charts import maps as mapdata
from ..charts import summaries as summarydata
from ..context import ContextDep
from ..db import Database, get_db
from ..scope import AXES, KINDS as SCOPE_KINDS, ScopeError, default_axis, resolve_scope
from ..source_names import is_identifier
from ..textclean import plain

#: What a category is called when the archive has no name for it. An axis
#: needs a word in every slot, and an identifier is not one
#: (app/source_names.py: is_identifier).
UNNAMED = "(no name)"


def readable(label: Any) -> str:
    """A label a person can read, or the honest admission that there is none.

    >>> readable("Apple Inc."), readable(""), readable("ent:apple-inc")
    ('Apple Inc.', '(no name)', '(no name)')
    """
    text = str(label or "").strip()
    return UNNAMED if not text or is_identifier(text) else text
from ..timeframes import TimeframeError

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/diagrams", tags=["diagrams"])

MAX_PAGE = 500
# The drilldown file is a page of a chart, not an export of the archive.
CSV_MAX_ROWS = 2000


def _bad(error: str, hint: str) -> HTTPException:
    return HTTPException(400, {"error": error, "hint": hint})


def _window(timeframe: str, page: int):
    try:
        return timeframes.window_for(timeframe or None, page)
    except TimeframeError as exc:
        raise _bad(str(exc), "timeframes: " + ", ".join(timeframes.TIMEFRAMES))


def _scope_or_400(conn, ctx, scope: str, q: str, axis: str = ""):
    """The ScopeSet, or a 400 that says what was wrong with the ask.

    `axis` is "object" or "type" - the same term, resolved as one thing or
    as a whole class ("Apple Inc." against "Company"). An empty string means
    the scope's own default (app/scope.default_axis), which is what a link
    written before the second axis existed carries.
    """
    try:
        return resolve_scope(conn, ctx, scope, q, axis or None)
    except ScopeError as exc:
        raise _bad(str(exc), "scopes: " + ", ".join(SCOPE_KINDS)
                   + "; axes: " + ", ".join(AXES))


def _tab_or_400(tab: str) -> list:
    try:
        return catalogue_module.charts_for(tab)
    except catalogue_module.UnknownChart as exc:
        raise HTTPException(404, {"error": str(exc),
                                  "hint": "tabs: " + ", ".join(catalogue_module.TAB_IDS)})


def _spec_or_400(tab: str, chart_id: str):
    try:
        return catalogue_module.get_chart(tab, chart_id)
    except catalogue_module.UnknownChart as exc:
        raise _bad(str(exc), "the charts of this tab are " +
                   ", ".join(c.id for c in catalogue_module.charts_for(tab)))


def _utc(conn) -> None:
    """Bucket boundaries are UTC, whatever the server's time zone is.

    date_trunc works in the session's time zone, and the window bounds are
    computed in Python in UTC (app/timeframes.py). A server in Europe/Zurich
    would cut its days at 23:00 UTC and every bar would land in the wrong
    bucket - by an hour, which is exactly the kind of wrong nobody notices.
    """
    conn.execute("SET LOCAL TimeZone TO 'UTC'")


# ── Turning rows into datasets ───────────────────────────────

def _dataset_ids(spec, plan, found: list[str]) -> list[tuple[str, str]]:
    """The datasets of a chart, in order: the ones the registry declares
    first (so the legend does not reshuffle between periods), then any the
    archive actually holds that nobody declared."""
    if plan.measures:
        return [(m.id, m.label) for m in plan.measures]
    declared = list(spec.datasets)
    if not declared and plan.series is None:
        return [catalogue_module.default_dataset(spec)]
    known = {i for i, _ in declared}
    extra = [(i, catalogue_module.dataset_label(spec.tab, spec, i))
             for i in found if i not in known]
    if not declared and not extra:
        return [catalogue_module.default_dataset(spec)]
    return declared + extra


def _value(plan, row: dict[str, Any], index: int) -> float:
    """One dataset's number out of one grouped row: the measure's column, or
    the plain count for a chart that is not split into measures."""
    if not plan.measures:
        return int(row["n"] or 0)
    value = row.get(f"m{index}")
    if value is None:
        return 0
    return float(value) if isinstance(value, float) else int(value)


def _time_chart(spec, plan, rows, window) -> tuple[list[dict], list[str]]:
    buckets = window.buckets()
    labels = [window.label(b) for b in buckets]
    at = {b: i for i, b in enumerate(buckets)}
    found = sorted({r["series"] for r in rows if r["series"] is not None})
    ids = _dataset_ids(spec, plan, found)
    single = ids[0][0] if len(ids) == 1 and plan.series is None and not plan.measures else None

    series: dict[str, list[float]] = {i: [0] * len(buckets) for i, _ in ids}
    for row in rows:
        index = at.get(row["bucket"])
        if index is None:
            continue
        if plan.measures:
            for m, (dataset_id, _) in enumerate(ids):
                series[dataset_id][index] += _value(plan, row, m)
        else:
            dataset_id = row["series"] if row["series"] is not None else single
            if dataset_id in series:
                series[dataset_id][index] += _value(plan, row, 0)

    datasets = []
    for n, (dataset_id, label) in enumerate(ids):
        values = series[dataset_id]
        datasets.append({
            "id": dataset_id, "label": label,
            "colour": catalogue_module.colour_for(dataset_id, n),
            # WHAT THIS COLOUR MEANS, for the ⓘ beside its legend entry. It
            # rides on the DATASET rather than on the chart because a series
            # is named at run time as often as it is declared - "Not stated"
            # only exists when the archive holds such a row - and the page
            # has nowhere else to look it up.
            "hint": catalogue_module.dataset_hint(spec.tab, spec, dataset_id),
            "data": [{"x": b.isoformat(), "label": labels[i], "y": values[i]}
                     for i, b in enumerate(buckets)],
        })
    return datasets, labels


def _key_chart(spec, plan, rows, resolver) -> tuple[list[dict], list[str]]:
    # The statement returns the categories already ordered and cut to the
    # top N; keep that order rather than re-sorting here.
    order: list[str] = []
    label_of: dict[str, str] = {}
    # THE STEP OF THE SCALE, when the chart is coloured by one. `ord` is the
    # ordering value the statement already selects (charts/drilldown.py:
    # key_statement) - for the rating scale it is
    # `rating_values.float_value`, which IS the step. Carrying it means the
    # colour of a bar and its place in the order can never disagree.
    tone_of: dict[str, float] = {}
    for row in rows:
        if row["x"] not in label_of:
            order.append(row["x"])
            if row.get("ord") is not None:
                tone_of[row["x"]] = float(row["ord"])
            # AS A PERSON WOULD READ IT. A crawled page says `W&auml;rtsil&auml;`
            # and the archive stores what the page said; the decoding belongs
            # at the edge where the value becomes a label (app/textclean.py).
            #
            # AND NEVER AN ID. Not the category KEY, which for "by entity"
            # charts is `ent:apple-inc` and for a document is
            # `src:antitrust` - the extraction's own identifiers, not unique
            # across projects and not what anything is CALLED. A bar labelled
            # with one tells the reader nothing except that we had nothing to
            # show, so it says that instead.
            label_of[row["x"]] = readable(plain(row["label"]))
    found = sorted({r["series"] for r in rows if r["series"] is not None})
    ids = _dataset_ids(spec, plan, found)
    single = ids[0][0] if len(ids) == 1 and plan.series is None and not plan.measures else None

    values: dict[str, dict[str, float]] = {i: {} for i, _ in ids}
    for row in rows:
        if plan.measures:
            for m, (dataset_id, _) in enumerate(ids):
                values[dataset_id][row["x"]] = _value(plan, row, m)
        else:
            dataset_id = row["series"] if row["series"] is not None else single
            if dataset_id in values:
                values[dataset_id][row["x"]] = values[dataset_id].get(row["x"], 0) + _value(plan, row, 0)

    datasets = []
    for n, (dataset_id, label) in enumerate(ids):
        points = []
        for key in order:
            point = {"x": key, "label": label_of[key], "y": values[dataset_id].get(key, 0)}
            if plan.colour_source == "valence" and key in tone_of:
                point["tone"] = tone_of[key]
            if plan.colour_source == "colour_group":
                group = resolver.group_by_key(key)
                point["colour"] = group.colour if group else resolver.fallback.colour
            points.append(point)
        datasets.append({"id": dataset_id, "label": label,
                         "colour": catalogue_module.colour_for(dataset_id, n),
                         "hint": catalogue_module.dataset_hint(spec.tab, spec, dataset_id),
                         "data": points})
    return datasets, [label_of[k] for k in order]


def _matrix_chart(spec, plan, rows) -> tuple[list[dict], list[str]]:
    points = [{"x": r["x"], "y": r["y"], "v": int(r["n"]),
               "x_label": readable(plain(r["x_label"])),
               "y_label": readable(plain(r["y_label"]))}
              for r in rows]
    dataset_id, label = catalogue_module.default_dataset(spec)
    return ([{"id": dataset_id, "label": label,
              "colour": catalogue_module.colour_for(dataset_id, 0),
              "hint": catalogue_module.dataset_hint(spec.tab, spec, dataset_id),
              "data": points}],
            sorted({p["x_label"] for p in points}))


def _matrix_axis(declared: tuple[str, ...], found: list[str]) -> list[str]:
    """The categories of one matrix axis, in the order they belong in.

    A declared scale is the axis, whether or not every step of it was
    counted: an empty column on an ordinal axis is information ("nothing
    read Very Negative"), and dropping it would move the diagonal. Anything
    the archive holds that the scale does not know is kept, at the end,
    rather than silently left out of the picture.

    Nothing declared - a nominal axis of entities or types - returns [], and
    the drawing keeps the order the statement produced (biggest first).
    """
    if not declared:
        return []
    seen = list(declared)
    seen += [f for f in found if f not in set(declared)]
    return seen


def _rows_counted(rows) -> int:
    """How many archive rows the chart is about.

    Read off `n`, which every statement carries, rather than added up from
    the datasets: measures overlap (a high-relevance insight is also an
    insight) and a chart of minima and maxima has no counts in it at all -
    a minimum of zero would otherwise read as a chart with no data.
    """
    return int(sum(int(r["n"] or 0) for r in rows))


def _build_chart(conn, spec, scope, ctx, window, resolver) -> dict[str, Any]:
    chart = spec.as_dict()
    chart.update({"datasets": [], "labels": [], "total": 0, "note": ""})
    if not spec.ready:
        chart["note"] = "This chart is not available."
        return chart
    plan = dd.plan_for(spec, scope, ctx, window)
    # A builder that draws a different picture per scope also says what that
    # picture is (Plan.description); the registry's sentence is the default.
    if plan.description:
        chart["description"] = plan.description
    statement = dd.chart_statement(spec.kind, plan, scope, window)
    params = dd.params_for(plan, scope, ctx, window)
    try:
        with conn.transaction():
            rows = conn.execute(statement, params).fetchall()
    except psycopg.errors.QueryCanceled:
        raise
    except psycopg.Error as exc:
        # One broken chart, seven that work: the tab still draws, and the
        # card says which one could not be answered and why.
        log.warning("chart %s/%s failed: %s", spec.tab, spec.id, exc)
        chart["note"] = f"This chart could not be built: {str(exc).strip().splitlines()[0]}"
        return chart

    if spec.kind == "time":
        chart["datasets"], chart["labels"] = _time_chart(spec, plan, rows, window)
    elif spec.kind == "key":
        chart["datasets"], chart["labels"] = _key_chart(spec, plan, rows, resolver)
    else:
        chart["datasets"], chart["labels"] = _matrix_chart(spec, plan, rows)
        # What the two axes are called and what order they run in. Both are
        # the builder's, because the same chart can be about different pairs
        # (connections: type by counterpart with a scope, parent by child
        # without one), and both are needed on the page: the axis titles head
        # the columns of the numbers table, the order pins the axes.
        points = chart["datasets"][0]["data"]
        chart["axes"] = list(plan.axis_titles) if plan.axis_titles else ["", ""]
        chart["x_order"] = _matrix_axis(plan.axis_order[0], [p["x_label"] for p in points])
        chart["y_order"] = _matrix_axis(plan.axis_order[1], [p["y_label"] for p in points])
    chart["total"] = _rows_counted(rows)
    chart["colour_source"] = plan.colour_source
    return chart


# ── Nothing in this period, and something outside it ─────────
#
# From a real archive: Apple has 7093 rows and the search finds
# nothing, because the newest entity-linked data is months old and the
# default period is the last seven days. An empty table reads as a broken
# search, so where the scope HAS data outside the window, the page says
# when, and offers the period that holds it.
#
# The cost is one statement, asked only when every chart of the tab came
# back empty; a page with an answer never asks it.

# The periods the offer may choose from, smallest first: the smallest one
# that reaches back far enough is the one that shows the data without
# burying it in years of empty buckets. Taken from the timeframe table
# itself, which is already in that order - a list typed here again would
# one day offer a period the page's own select does not have.
_WIDENING = tuple(timeframes.TIMEFRAMES)
# How far back the offer will step a window whose newest row is older than
# five years. Twenty pages of five years is a century; past that the offer
# is honest about there being nothing to offer.
_MAX_STEPS = 20


def widen_to(newest: datetime, now: datetime | None = None) -> dict[str, Any] | None:
    """The window that holds `newest`: the smallest timeframe whose newest
    page reaches it, or the largest one stepped back far enough."""
    for key in _WIDENING:
        window = timeframes.window_for(key, 0, now)
        if window.start <= newest < window.end:
            return {"timeframe": key, "label": window.timeframe.label, "page": 0}
    widest = _WIDENING[-1]
    for page in range(1, _MAX_STEPS + 1):
        window = timeframes.window_for(widest, page, now)
        if window.start <= newest < window.end:
            return {"timeframe": widest, "label": window.timeframe.label,
                    "page": page, "caption": window.caption}
        if newest >= window.end:
            return None
    return None


def _newest_outside(conn, specs, scope_set, ctx, window) -> dict[str, Any] | None:
    """The newest row this tab's tables hold for this scope, in no window at
    all - and the period that would show it.

    One statement per TABLE, not per chart: a tab is one or two tables over
    eight charts, and the first chart of each table carries the same scope
    predicate as the rest.
    """
    seen: set[str] = set()
    newest: datetime | None = None
    for spec in specs:
        if not spec.ready:
            continue
        plan = dd.plan_for(spec, scope_set, ctx, window)
        if plan.table in seen:
            continue
        seen.add(plan.table)
        statement = dd.newest_statement(plan, scope_set)
        params = dd.params_for(plan, scope_set, ctx, window)
        try:
            with conn.transaction():
                row = conn.execute(statement, params).fetchone()
        except psycopg.errors.QueryCanceled:
            raise
        except psycopg.Error as exc:   # pragma: no cover - a broken builder
            log.warning("newest outside %s failed: %s", plan.table, exc)
            continue
        found = row and row.get("newest")
        if found and (newest is None or found > newest):
            newest = found
    if newest is None or newest >= window.start:
        return None
    return {"newest": newest.isoformat(), "widen": widen_to(newest)}


# ── The endpoints ────────────────────────────────────────────

@router.get("/catalogue")
def catalogue():
    """Every tab and chart, without touching the archive - what the page
    renders its tab strip and its empty cards from."""
    return catalogue_module.catalogue()


# THE MAP UNDER THE CHARTS, and it is registered BEFORE /{scope}/{tab}
# because "map" would otherwise be read as a tab name and answered with 400.
@router.get("/{scope}/map")
def diagrams_map(scope: str, ctx: ContextDep, db: Database = Depends(get_db),
                 tab: str = Query("locations", description="connections or locations"),
                 q: str = "", axis: str = "",
                 timeframe: str = Query(""), page: int = Query(0, ge=0, le=MAX_PAGE)):
    """Where the rows of one tab are, drawn.

    Two tabs have a place on the world behind them - Locations is addresses
    and Connections is pairs of entities that have them - and for those two
    the bars are a picture of HOW MANY and the map is a picture of WHERE.
    Every other tab answers 404: "no map for this tab" is a different answer
    from "a map with nothing on it", and only one of them is worth a card.

    The scope and the period are read exactly as the tab beside it reads
    them, from the same parameters, so the map under a chart is the same
    search as the chart. What the period means differs between the two
    pictures - see app/charts/maps.py - and the page says which.
    """
    builder = mapdata.BUILDERS.get(tab)
    if builder is None:
        raise HTTPException(404, {
            "error": f"the {tab} tab has no map",
            "hint": "Only Connections and Locations are drawn on a map."})
    window = _window(timeframe, page)
    resolver = colours.get_resolver()
    resolver.groups()
    with db.read() as conn:
        _utc(conn)
        scope_set = _scope_or_400(conn, ctx, scope, q, axis if isinstance(axis, str) else "")
        # NOTHING TO DRAW IS NOT A QUERY. A term the archive has never heard
        # of, and a scoped page with an empty box, both resolve to an empty
        # set - and asking for the places of an empty set is a walk over the
        # archive that can only come back empty. The page draws no card for
        # this, so the answer only has to be honest, not pretty.
        blank = (scope_set.resolved.get("match") == "none"
                 or (not scope_set.q and scope != "summary"))
        answer = ({"mode": tab, "places": [], "lines": [], "total": 0, "shown": 0,
                   "capped": False, "bounds": None} if blank else
                  (builder(conn, scope_set, ctx, window, resolver)
                   if tab == "connections" else builder(conn, scope_set, ctx, window)))

    return {
        "scope": scope, "tab": tab,
        "q": scope_set.q, "axis": scope_set.resolved.get("axis", default_axis(scope)),
        "resolved": scope_set.resolved,
        "window": window.as_dict(),
        **answer,
        "project": ctx.project, "language": ctx.language,
    }


@router.get("/{scope}/{tab}")
def diagrams(scope: str, tab: str, ctx: ContextDep, db: Database = Depends(get_db),
             q: str = Query("", description="what the page is about; empty on the summary means everything"),
             # EMPTY, not "object": an empty axis means "this scope's own
             # default" (app/scope.default_axis), so a link written before
             # the second axis existed keeps meaning exactly what it meant -
             # which on the events page is a search for the event TYPE.
             #
             # A plain string default and NOT Query(...): api_export.py calls
             # this function directly, and a FastAPI default object arriving
             # where a string is expected fails deep inside app/scope.py.
             axis: str = "",
             timeframe: str = Query(""), page: int = Query(0, ge=0, le=MAX_PAGE)):
    """One tab of charts about one subject.

    `axis` says how the term is read: "object" is one thing (an entity, a
    document, an address, a topic, one event), "type" is the whole class
    ("Company" is every company). Both resolve to the same two CTEs, so all
    eight tabs are built from either without knowing which - only
    `resolved.axis` says which produced the answer, and the page prints it,
    because "Company - 42 entities" and "Apple Inc. - 1 entity" are
    different questions with the same shape of answer.
    """
    specs = _tab_or_400(tab)
    window = _window(timeframe, page)
    resolver = colours.get_resolver()
    # Warmed here, outside the read connection: the resolver loads its table
    # through the same pool, and asking it for a colour in the middle of a
    # request would take a second connection while this one is held.
    resolver.groups()
    with db.read() as conn:
        _utc(conn)
        scope_set = _scope_or_400(conn, ctx, scope, q, axis if isinstance(axis, str) else "")
        built = [_build_chart(conn, spec, scope_set, ctx, window, resolver) for spec in specs]
        # NOTHING HERE IS NOT NOTHING AT ALL. A tab with no rows in this
        # window asks one more question - "does this scope hold anything
        # outside it?" - because "no rows" and "no rows YET IN THIS PERIOD"
        # are two different answers and only one of them is true.
        outside = (None if any(c["total"] for c in built)
                   else _newest_outside(conn, specs, scope_set, ctx, window))
        # THE LIST AT THE FOOT OF THE TAB, in the same connection and under the
        # same predicates as the charts above it. Four tabs have one; the rest
        # answer `null` and their summary is whatever charts are marked `band`.
        #
        # A search that resolved to nothing is not asked: the scope is empty,
        # every row of the list would be too, and walking the archive to prove
        # it is a walk nobody reads. `maps` takes the same decision, in the
        # same words, a hundred lines up.
        builder = summarydata.BUILDERS.get(tab)
        blank = (scope_set.resolved.get("match") == "none"
                 or (not scope_set.q and scope != "summary"))
        summary = None if (builder is None or blank) else builder(conn, scope_set, ctx, window)

    return {
        "scope": scope, "tab": tab, "tab_title": catalogue_module.TAB_TITLES[tab],
        # The chart the map follows, on the two tabs that have one; "" on the
        # rest (app/charts/__init__.py: MAP_AFTER).
        "map_after": catalogue_module.MAP_AFTER.get(tab, ""),
        "q": scope_set.q, "axis": scope_set.resolved.get("axis", default_axis(scope)),
        "resolved": scope_set.resolved,
        "window": window.as_dict(), "charts": built,
        # {kind: "list", layout, title, note, rows, total} or null.
        "summary": summary,
        # {newest, widen: {timeframe, label, page}} when the period is the
        # only thing between the reader and the data; null otherwise.
        "outside": outside,
        "project": ctx.project, "language": ctx.language,
    }


class DrilldownRequest(BaseModel):
    scope: str = "summary"
    tab: str
    q: str = ""
    # WHICH AXIS THE TERM WAS READ ON, carried with the click. Without it a
    # drilldown out of a type search would resolve the same word as an
    # object and list the rows of something else - the bar would say 42 and
    # the dialog would show one. "" means the scope's own default, which is
    # what a link written before the second axis existed carries.
    axis: str = ""
    timeframe: str = ""
    page: int = Field(0, ge=0, le=MAX_PAGE)
    chart_id: str
    dataset_id: str = ""
    # {"bucket": iso} | {"x": ...} | {"x": ..., "y": ...} - which one the
    # chart takes is in the registry (ChartSpec.drill).
    key: dict[str, Any] = Field(default_factory=dict)
    # 1 is the first page; static/js/drilldown.js counts from there.
    ddpage: int = Field(1, ge=1, le=MAX_PAGE)


def _rows_for(conn, ctx, request: DrilldownRequest, limit: int, offset: int):
    spec = _spec_or_400(request.tab, request.chart_id)
    if not spec.ready:
        raise _bad(f"the chart {request.chart_id!r} is not available",
                   "pick a chart that shows data")
    window = _window(request.timeframe, request.page)
    _utc(conn)
    scope_set = _scope_or_400(conn, ctx, request.scope, request.q, request.axis)
    plan = dd.plan_for(spec, scope_set, ctx, window)
    try:
        key = dd.normalise_key(spec.drill, request.key)
        statement, key_params = dd.rows_statement(
            plan, spec.kind, scope_set, window, key, request.dataset_id,
            _allowed_datasets(spec, plan))
    except dd.DrilldownError as exc:
        raise _bad(str(exc), exc.hint)
    params = {**dd.params_for(plan, scope_set, ctx, window), **key_params,
              "dd_limit": limit, "dd_offset": offset}
    rows = conn.execute(statement, params).fetchall()
    return spec, plan, scope_set, window, key, rows


def _allowed_datasets(spec, plan) -> tuple[str, ...]:
    ids = tuple(i for i, _ in spec.datasets) + tuple(m.id for m in plan.measures)
    return ids + (catalogue_module.DEFAULT_DATASET_ID,)


@router.post("/drilldown")
def drilldown(request: DrilldownRequest, ctx: ContextDep, db: Database = Depends(get_db)):
    offset = (request.ddpage - 1) * dd.PAGE_SIZE
    with db.read() as conn:
        # One row more than a page, so "is there more" is known without a
        # second count over the same predicates.
        spec, plan, scope_set, window, key, rows = _rows_for(
            conn, ctx, request, dd.PAGE_SIZE + 1, offset)
    more = len(rows) > dd.PAGE_SIZE
    shown = rows[:dd.PAGE_SIZE]
    # HOW MANY THE FILTERS MATCH, off the statement that has just run.
    # `count(*) OVER ()` rides on every row (charts/drilldown.rows_statement),
    # so the dialog's "Row 87-106 of 312" costs nothing; a page past the last
    # one carries no rows and therefore no total, and the offset is what it
    # already knows.
    total = int(shown[0]["dd_total"]) if shown else offset
    return {
        "tab": request.tab, "chart_id": spec.id, "chart_title": spec.title,
        "dataset_id": request.dataset_id, "key": {k: str(v) for k, v in key.items()},
        "scope": request.scope, "q": scope_set.q,
        "axis": scope_set.resolved.get("axis", default_axis(request.scope)),
        "resolved": scope_set.resolved, "window": window.as_dict(),
        "ddpage": request.ddpage, "page_size": dd.PAGE_SIZE, "more": more,
        # THE RUNNING NUMBER COUNTS UP ACROSS PAGES: row 21 on page 2 is 21.
        # The page says where it starts and the rows follow from there.
        "offset": offset, "total": total,
        # The columns of THIS tab, once, so the header and the cells cannot
        # disagree about what is in which column - and which of the three
        # links a row of this table carries.
        "columns": dd.columns_json(plan.table),
        "row_links": dd.row_links(plan.table),
        "rows": [dd.shape_row(plan.table, row) for row in shown],
    }


def _slug(text: str) -> str:
    keep = [c.lower() if c.isalnum() else "-" for c in (text or "")]
    return "".join(keep).strip("-").replace("--", "-") or "all"


@router.get("/drilldown.csv")
def drilldown_csv(ctx: ContextDep, db: Database = Depends(get_db),
                  scope: str = Query("summary"), tab: str = Query(...),
                  chart_id: str = Query(...), dataset_id: str = Query(""),
                  q: str = Query(""), axis: str = Query(""), timeframe: str = Query(""),
                  page: int = Query(0, ge=0, le=MAX_PAGE),
                  key_bucket: str = Query(""), key_x: str = Query(""), key_y: str = Query("")):
    """The clicked point as a file. Same predicates, no paging, capped.

    A GET with the key spelled out in the query string rather than the POST
    the dialog uses: a download is a link, and a link has to survive being
    copied, bookmarked and opened in a second tab.
    """
    key = {k: v for k, v in (("bucket", key_bucket), ("x", key_x), ("y", key_y)) if v}
    request = DrilldownRequest(scope=scope, tab=tab, q=q, axis=axis, timeframe=timeframe,
                               page=page, chart_id=chart_id, dataset_id=dataset_id,
                               key=key, ddpage=1)
    with db.read() as conn:
        spec, plan, scope_set, window, parsed, rows = _rows_for(conn, ctx, request, CSV_MAX_ROWS, 0)

    # THE SAME COLUMNS AS THE DIALOG, per tab. A file whose columns are not
    # the table's columns is a second answer to the question the table has
    # already answered, and the two drift.
    columns = dd.csv_columns(plan.table)
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(columns)
    for number, row in enumerate(rows, start=1):
        item = dd.csv_row(plan.table, row, number)
        writer.writerow([item.get(name, "") for name in columns])
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    name = f"xtracting-diagrams-{_slug(ctx.project)}-{_slug(ctx.language)}-{stamp}.csv"
    # The BOM is what makes Excel read UTF-8 instead of guessing Latin-1 and
    # turning Zürich into ZÃ¼rich.
    body = "﻿" + buffer.getvalue()
    return Response(body, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})
