"""Every view as a file.

    GET /api/export/<view>.csv
    GET /api/export/<view>.json
    GET /api/export/views          (what the two above accept)

The top bar carries a Print button and an Export menu on every page
(`print_export_buttons` in templates/_macros.html), and the menu's two links
point here with the view's own query string attached - the same URL the page
is looking at. So the file holds what the screen holds, and a link to it can
be copied, bookmarked and opened in a second tab.

WHY THIS ROUTER RUNS THE VIEWS' OWN FUNCTIONS
---------------------------------------------
Each entry below calls the endpoint function of the router that owns the
view - `api_events.events`, `api_diagrams.diagrams`, `api_logs.runs` - and
flattens its answer. Not one line of SQL is repeated here. A second copy of
a predicate is a second thing to keep in step with the first, and an export
that quietly disagrees with the page it was taken from is worse than no
export at all: it is a spreadsheet somebody will act on.

Calling them directly means passing every argument by name, including the
ones FastAPI would normally fill from `Query(...)` defaults - a default that
is a `Query` object, not a value, is what a direct call gets otherwise.

PAGING
------
Row paging is NOT carried into the file. A person who presses Export wants
the rows the filters match, not the twenty of them that happen to be on
screen, so this router walks the pages itself up to `MAX_ROWS` rows or
`MAX_REQUESTS` round trips, whichever comes first, and says so in the
envelope (and in the `X-Export-Truncated` header) when it stops early.

Where `page` means something else it IS honoured: on the Diagrams and
drilldown exports `page` selects the time WINDOW ("one period earlier"), not
a row page, and a file from the wrong period would be silently wrong.

CSV, AND WHY IT STARTS WITH A BOM
---------------------------------
One table per file, header row first, CRLF line ends, and a byte order mark
in front: without it Excel guesses Latin-1 and turns Zürich into ZÃ¼rich.
The JSON carries the same rows plus the descriptive part of the view's own
answer (what the search resolved to, which window, how many in total) under
`context`, because that is the part a person reads to know what the rows are.

SOURCES IS NOT HERE
-------------------
`/api/export/sources.csv|.json` is older than this router and lives in
`api_sources.py` next to the store it reads; the list of sources is not a
view of one project and its file name says so. Registering it here as well
would shadow it, so the registry below skips the name and `/api/export/views`
points at the other router instead.
"""

from __future__ import annotations

import csv
import io
import json
import re
from datetime import date, datetime, timezone
from typing import Any, Callable, Iterable

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response

from ..context import Context, ContextDep
from ..db import Database, get_db
from . import api_dashboard, api_diagrams, api_events, api_graph, api_logs, api_map, api_query, api_settings

router = APIRouter(tags=["export"])

# The ceiling on one file. Big enough that a customer's year of events fits,
# small enough that a link nobody meant to press cannot hold a connection
# open while the archive streams a million rows into memory.
MAX_ROWS = 5000

# And a ceiling on the WORK, not just on the rows. The paged endpoints are
# walked a page at a time, and their page size is theirs, not ours: the Log
# answers 500 rows at a time and the Events view 20. Twenty at a time up to
# MAX_ROWS would be 250 round trips with a growing OFFSET behind each of
# them, which is slow in exactly the archive that is big enough to want the
# export. So whichever ceiling is reached first ends the file, and the
# envelope says it was cut short either way.
MAX_REQUESTS = 100

# The one export this router does not own; see the module docstring.
SOURCES_ELSEWHERE = {
    "view": "sources", "about": "The link lists, one row per source.",
    "csv": "/api/export/sources.csv", "json": "/api/export/sources.json",
    "router": "api_sources",
}


def _bad(error: str, hint: str) -> HTTPException:
    return HTTPException(400, {"error": error, "hint": hint})


# ── Reading the query string ────────────────────────────────────────────
#
# The parameters are the VIEW's, and every view has its own set, so they are
# read here rather than declared on every endpoint signature. Each helper
# answers with the same thing a missing parameter would give the view itself.

def _str(request: Request, name: str, default: str = "") -> str:
    value = request.query_params.get(name)
    return default if value is None else str(value).strip()


def _int(request: Request, name: str, default: int, low: int, high: int) -> int:
    raw = _str(request, name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise _bad(f"{name} is not a whole number", f"{name}={default} is the default")
    return max(low, min(high, value))


def _opt_int(request: Request, name: str) -> int | None:
    raw = _str(request, name)
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        raise _bad(f"{name} is not a whole number", f"leave {name} out to export everything")


def _bool(request: Request, name: str, default: bool) -> bool:
    raw = _str(request, name).lower()
    if not raw:
        return default
    if raw in ("0", "false", "no", "off"):
        return False
    if raw in ("1", "true", "yes", "on"):
        return True
    return default


def _list(request: Request, name: str) -> list[str]:
    return [part.strip() for part in _str(request, name).split(",") if part.strip()]


# ── Cells ───────────────────────────────────────────────────────────────

def _cell(value: Any) -> str:
    """One value as CSV text. `yes`/`no` rather than True/False: a column a
    person reads, not a Python repr."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return "; ".join(_cell(v) for v in value)
    if isinstance(value, dict):
        return "; ".join(f"{k}: {_cell(v)}" for k, v in value.items())
    return str(value)


def _names(items: Iterable[dict[str, Any]] | None, key: str = "name") -> str:
    return "; ".join(str(i.get(key) or "") for i in (items or []) if i.get(key))


def _paged(fetch: Callable[[int], dict[str, Any]], page_size: int,
           cap: int = MAX_ROWS) -> tuple[list[Any], dict[str, Any], bool]:
    """Walk a paged endpoint to the end, or to `cap`, or to MAX_REQUESTS.

    Returns (items, the first page's answer, truncated). The first answer is
    kept because it carries the descriptive part - what the term resolved to,
    the total, the range - and that does not change from page to page.
    """
    items: list[Any] = []
    first: dict[str, Any] = {}
    page = 0
    truncated = False
    while True:
        answer = fetch(page)
        if not first:
            first = answer
        batch = answer.get("items") or answer.get("events") or []
        items.extend(batch)
        if len(items) >= cap:
            truncated = len(items) > cap or (bool(batch) and len(batch) == page_size)
            del items[cap:]
            break
        if len(batch) < page_size:
            break
        page += 1
        if page >= MAX_REQUESTS:
            truncated = True
            break
    return items, first, truncated


# ── The views ───────────────────────────────────────────────────────────
#
# Each function returns (columns, rows, context). `columns` is the CSV header
# and the JSON key order; `rows` are dicts over exactly those columns;
# `context` is the descriptive part of the view's own answer.

Built = tuple[list[str], list[dict[str, Any]], dict[str, Any]]


def _dashboard_stats(request: Request, ctx: Context, db: Database) -> Built:
    # fresh=False on purpose: the page reads the same one-minute cache, and a
    # file that disagrees with the tiles it was taken from is a file somebody
    # will spend an afternoon reconciling.
    data = api_dashboard.stats(ctx=ctx, db=db, fresh=False)
    columns = ["id", "label", "tab", "hour", "day", "week", "month", "year"]
    rows = [{c: t.get(c) for c in columns} for t in data.get("tables", [])]
    return columns, rows, {"cached_at": data.get("cached_at")}


def _activities(request: Request, ctx: Context, db: Database) -> Built:
    # ACTIVITIES_PER_TABLE caps each branch of the UNION inside the SQL, so a
    # larger limit here would only ask for rows the query cannot produce.
    limit = _int(request, "limit", 50, 1, 50)
    data = api_dashboard.activities(ctx=ctx, db=db, limit=limit)
    columns = ["table", "label", "name", "detail", "about", "date_commissioned", "link"]
    rows = [{**{c: item.get(c) for c in columns}, "about": _cell(item.get("about"))}
            for item in data.get("items", [])]
    return columns, rows, {"limit": limit, "per_table": api_dashboard.ACTIVITIES_PER_TABLE}


def _latest_events(request: Request, ctx: Context, db: Database) -> Built:
    limit = _int(request, "limit", 25, 1, 25)
    data = api_dashboard.latest_events(ctx=ctx, db=db, limit=limit)
    columns = ["id", "name", "type", "description", "date", "dated",
               "source", "source_uri", "entities"]
    rows = [{
        "id": e["id"], "name": e["name"], "type": e["type"],
        "description": e["description"], "date": e["date"], "dated": e["dated"],
        "source": (e.get("source") or {}).get("name", ""),
        "source_uri": (e.get("source") or {}).get("uri", ""),
        "entities": _names(e.get("entities")),
    } for e in data.get("events", [])]
    return columns, rows, {"limit": limit}


def _query(request: Request, ctx: Context, db: Database) -> Built:
    """The Query view writes its search into the URL (`terms`, `address` or
    `lat`/`lng`, `km`, `from`, `to`, `all`); the search itself is a POST with
    a body. The body is rebuilt from those parameters here, so the export URL
    stays a link - and a search this endpoint cannot run is the same 400 the
    page would get, not an empty file."""
    terms = _list(request, "terms")
    place = _str(request, "address") or _str(request, "city")
    lat, lng = _str(request, "lat"), _str(request, "lng")
    body: dict[str, Any] = {
        "terms": terms,
        "match_all": _str(request, "all") in ("1", "true", "yes"),
        "date_from": _str(request, "from") or _str(request, "date_from") or None,
        "date_to": _str(request, "to") or _str(request, "date_to") or None,
    }
    if place:
        body["place"] = {"address": _str(request, "address") or None,
                         "city": _str(request, "city") or None,
                         "region": _str(request, "region") or None,
                         "country": _str(request, "country") or None}
    if lat and lng:
        try:
            body["lat"], body["lng"] = float(lat), float(lng)
        except ValueError:
            raise _bad("lat and lng are not numbers", "use decimal degrees, e.g. lat=47.37&lng=8.54")
    km = _str(request, "km") or _str(request, "radius_km")
    if km:
        try:
            body["radius_km"] = float(km)
        except ValueError:
            raise _bad("km is not a number", "a radius in kilometres, e.g. km=25")

    def fetch(page: int) -> dict[str, Any]:
        req = api_query.SearchRequest(**{**body, "page": page})
        answer = api_query.search(req=req, ctx=ctx, db=db)
        # The results come grouped by kind; the file is one flat table.
        return {**answer, "items": [item for group in answer.get("groups", [])
                                    for item in group.get("items", [])]}

    items, first, truncated = _paged(fetch, api_query.PAGE_SIZE)
    columns = ["kind", "id", "title", "body", "type", "about", "date", "uri", "matched_terms"]
    rows = [{**{c: item.get(c) for c in columns},
             "matched_terms": _cell(item.get("matched_terms"))} for item in items]
    return columns, rows, {
        "where": first.get("where"), "terms": first.get("terms"),
        "match_all": first.get("match_all"), "total": first.get("total"),
        "counts": first.get("counts"), "truncated": truncated,
    }


def _events(request: Request, ctx: Context, db: Database) -> Built:
    by = _str(request, "by", "entity") or "entity"
    q = _str(request, "q")
    date_from = _str(request, "from") or _str(request, "date_from")
    date_to = _str(request, "to") or _str(request, "date_to")

    def fetch(page: int) -> dict[str, Any]:
        return api_events.events(ctx=ctx, db=db, by=by, q=q,
                                 date_from=date_from, date_to=date_to, page=page)

    items, first, truncated = _paged(fetch, api_events.PAGE_SIZE)
    columns = ["id", "name", "type", "description", "date", "dated", "reason",
               "relation", "source", "source_uri", "entities"]
    rows = [{
        "id": e["id"], "name": e["name"], "type": e["type"],
        "description": e["description"], "date": e["date"], "dated": e["dated"],
        "reason": e.get("reason", ""), "relation": e.get("relation", ""),
        "source": (e.get("source") or {}).get("name", ""),
        "source_uri": (e.get("source") or {}).get("uri", ""),
        "entities": _names(e.get("entities")),
    } for e in items]
    return columns, rows, {
        "by": by, "q": q, "resolved": first.get("resolved"),
        "date_from": first.get("date_from"), "date_to": first.get("date_to"),
        "total": first.get("total"), "truncated": truncated,
    }


def _diagrams(request: Request, ctx: Context, db: Database) -> Built:
    """Every point of every dataset of every chart of one tab, as rows.

    `page` here is the period the toolbar's ◀ / ▶ moved to, so it goes
    straight through: a file from the wrong twelve months is silently wrong.
    The tab defaults to the first one, which is what the page shows when the
    URL does not name one.
    """
    scope = _str(request, "scope", "summary") or "summary"
    tab = _str(request, "tab") or api_diagrams.catalogue_module.TAB_IDS[0]
    q = _str(request, "q")
    timeframe = _str(request, "timeframe")
    page = _int(request, "page", 0, 0, api_diagrams.MAX_PAGE)
    # The axis travels with the term: a file exported from a TYPE search
    # ("every company") must not come back as the object search for the same
    # word, which is a different set of rows under the same heading.
    data = api_diagrams.diagrams(scope=scope, tab=tab, ctx=ctx, db=db,
                                 q=q, axis=_str(request, "axis"),
                                 timeframe=timeframe, page=page)
    columns = ["tab", "chart_id", "chart_title", "kind", "dataset_id",
               "dataset_label", "label", "x", "y"]
    rows: list[dict[str, Any]] = []
    for chart in data.get("charts", []):
        for dataset in chart.get("datasets", []):
            for point in dataset.get("data", []):
                rows.append({
                    "tab": data["tab"], "chart_id": chart["id"],
                    "chart_title": chart["title"], "kind": chart["kind"],
                    "dataset_id": dataset.get("id", ""),
                    "dataset_label": dataset.get("label", ""),
                    "label": point.get("label", ""),
                    "x": point.get("x"), "y": point.get("y"),
                })
    truncated = len(rows) > MAX_ROWS
    del rows[MAX_ROWS:]
    return columns, rows, {
        "scope": data.get("scope"), "tab": data.get("tab"),
        "tab_title": data.get("tab_title"), "q": data.get("q"), "axis": data.get("axis"),
        "resolved": data.get("resolved"), "window": data.get("window"),
        "charts": len(data.get("charts", [])), "truncated": truncated,
    }


def _drilldown(request: Request, ctx: Context, db: Database) -> Built:
    """The rows behind one clicked point. Same parameters as the dialog's own
    download (`/api/diagrams/drilldown.csv`), so a link copied out of one
    works in the other."""
    tab = _str(request, "tab")
    chart_id = _str(request, "chart_id")
    if not tab or not chart_id:
        raise _bad("a drilldown export needs a chart",
                   "add tab=<tab>&chart_id=<chart>, as the dialog's own link does")
    key = {k: v for k, v in (("bucket", _str(request, "key_bucket")),
                             ("x", _str(request, "key_x")),
                             ("y", _str(request, "key_y"))) if v}
    req = api_diagrams.DrilldownRequest(
        scope=_str(request, "scope", "summary") or "summary", tab=tab,
        q=_str(request, "q"), axis=_str(request, "axis"),
        timeframe=_str(request, "timeframe"),
        page=_int(request, "page", 0, 0, api_diagrams.MAX_PAGE),
        chart_id=chart_id, dataset_id=_str(request, "dataset_id"), key=key, ddpage=1)
    with db.read() as conn:
        spec, plan, scope_set, window, parsed, raw = api_diagrams._rows_for(
            conn, ctx, req, MAX_ROWS + 1, 0)
    truncated = len(raw) > MAX_ROWS
    # The dialog's own columns, per tab (app/charts/drilldown.py): the file
    # and the table on screen say the same things in the same order, and
    # `source_uri` keeps its name so a link copied out of one export still
    # works in the other.
    columns = api_diagrams.dd.csv_columns(plan.table)
    rows = [api_diagrams.dd.csv_row(plan.table, row, number)
            for number, row in enumerate(raw[:MAX_ROWS], start=1)]
    return columns, rows, {
        "tab": req.tab, "chart_id": spec.id, "chart_title": spec.title,
        "dataset_id": req.dataset_id, "key": {k: str(v) for k, v in parsed.items()},
        "scope": req.scope, "q": scope_set.q, "resolved": scope_set.resolved,
        "window": window.as_dict(), "truncated": truncated,
    }


def _hidden(request: Request) -> set[str]:
    """The map views keep the types a reader has UNTICKED in the URL (`hide`),
    while the API takes the ones that are wanted. The rows are filtered here
    rather than the parameter inverted, because inverting it needs the full
    list of types - which is in the answer, not in the request."""
    return set(_list(request, "hide"))


def _map(request: Request, ctx: Context, db: Database) -> Built:
    """One row per marker: which entity is where, and what kind of place it
    is. The lines between them are the Graph export's table; both are in the
    JSON under `context` so neither file loses the other half."""
    # THE AXIS TRAVELS WITH THE TERM. The Map reads its box as an entity or
    # as an EVENT TYPE (api_map.MAP_AXES), and a file exported without the
    # axis would answer the other question under the same word.
    data = api_map.map_entity(
        ctx=ctx, request=request, db=db, q=_str(request, "q"),
        axis=_str(request, "axis") or "object",
        levels=_int(request, "levels", 1, 1, api_map.MAX_LEVELS),
        types=_str(request, "types"),
        # The page's own switch, so the file holds the picture that was on
        # screen: the network at one address each, or every address of the
        # search (routers/api_map.py: map_entity).
        mode=request.query_params.get("mode", "connections"),
        # And the page's other switch: whether a node is an entity or a bucket.
        # Called as a plain function, every parameter has to be given - a
        # default left out arrives as FastAPI's Query object, not as a string.
        group=request.query_params.get("group", "entities"))
    hide = _hidden(request)
    # The LOCATION-type checklist, which filters the pins the way `hide`
    # filters the lines (`hidepins` in the URL, static/js/map.js). A file that
    # held every pin under a map showing four of them would be read as the
    # archive.
    hide_pins = set(_list(request, "hidepins"))
    columns = ["entity_id", "entity", "entity_type", "level", "type",
               "address", "lat", "lng", "count"]
    rows = [{
        "entity_id": m.get("entity_id", ""), "entity": m.get("entity", ""),
        "entity_type": m.get("entity_type", ""), "level": m.get("level"),
        "type": m.get("type", ""), "address": m.get("address", ""),
        "lat": m.get("lat"), "lng": m.get("lng"), "count": m.get("count"),
    } for m in data.get("markers", []) if m.get("type", "") not in hide_pins]
    connections = [c for c in data.get("connections", [])
                   if not (hide and all(t.get("name") in hide for t in c.get("types", [])))]
    truncated = len(rows) > MAX_ROWS
    del rows[MAX_ROWS:]
    return columns, rows, {
        "q": data.get("q"), "axis": data.get("axis"),
        "levels": data.get("levels"), "show": data.get("show"),
        "resolved": data.get("resolved"), "bounds": data.get("bounds"),
        "capped": data.get("capped"), "hidden_types": sorted(hide),
        "hidden_location_types": sorted(hide_pins),
        "connections": connections, "truncated": truncated,
    }


def _heatmap(request: Request, ctx: Context, db: Database) -> Built:
    """One row per heat square. `points` is what the layer is drawn from and
    `top` is the same squares with the name of the place, so the two are
    joined here and the file carries both the numbers and the addresses.

    `q` is a PLACE on this view, not an entity (app/routers/api_map.py), and
    it has to travel: a file exported from a map of Springfield that held
    every square in the archive would be read as the archive."""
    bbox = _str(request, "bbox") or _str(request, "area")
    types = _str(request, "types")
    q = _str(request, "q")
    # THE OTHER TWO NARROWINGS TRAVEL TOO. The view can now be asked about one
    # entity and about a date range as well as about a place; a file exported
    # from "Apple's addresses since March" that held every square in the
    # archive would be read as the archive.
    entity = _str(request, "entity")
    # How the entity box was read - one entity, or an event type
    # (api_map.MAP_AXES). Without it the file would count something else.
    entity_axis = _str(request, "entity_axis") or "object"
    date_from = _str(request, "from") or _str(request, "date_from")
    date_to = _str(request, "to") or _str(request, "date_to")
    data = api_map.map_heat(ctx=ctx, db=db, q=q, bbox=bbox, types=types,
                            entity=entity, entity_axis=entity_axis,
                            date_from=date_from, date_to=date_to)
    hide = _hidden(request)
    if hide and not types:
        # A heat square has no type - the filter is in the SQL - so `hide`
        # cannot be applied to the rows afterwards the way it can on the map.
        # The first answer carries the whole checklist, which is what turns
        # "these are off" into "these are on"; asking twice is the price of a
        # file that matches the screen.
        wanted = [t["name"] for t in data.get("types", []) if t["name"] not in hide]
        if len(wanted) != len(data.get("types", [])):
            data = api_map.map_heat(ctx=ctx, db=db, q=q, bbox=bbox, types=",".join(wanted),
                                    entity=entity, entity_axis=entity_axis,
                                    date_from=date_from, date_to=date_to)
    named = {(t["lat"], t["lng"]): t.get("address", "") for t in data.get("top", [])}
    columns = ["lat", "lng", "count", "address"]
    rows = [{"lat": p[0], "lng": p[1], "count": p[2], "address": named.get((p[0], p[1]), "")}
            for p in data.get("points", [])]
    truncated = len(rows) > MAX_ROWS
    del rows[MAX_ROWS:]
    return columns, rows, {
        "max": data.get("max"), "cells": data.get("cells"),
        "locations": data.get("locations"), "entities": data.get("entities"),
        "precision": data.get("precision"), "bounds": data.get("bounds"),
        "types": data.get("types"), "capped": data.get("capped"),
        "bbox": data.get("bbox"), "hidden_types": sorted(hide),
        "q": data.get("q"), "place": data.get("place"),
        "entity": data.get("entity"), "entity_axis": data.get("entity_axis"),
        "resolved": data.get("resolved"),
        "date_from": data.get("date_from"), "date_to": data.get("date_to"),
        "truncated": truncated,
    }


def _graph(request: Request, ctx: Context, db: Database) -> Built:
    """One row per connection TYPE between the centre and a neighbour: a pair
    joined by both "Supplier" and "Competitor" is two lines on the graph and
    two rows here, each with the colour the edge is drawn in."""
    q, node_id = _str(request, "q"), _str(request, "id")
    if not q and not node_id:
        raise _bad("a graph export needs a subject",
                   "add q=<entity or bucket>, as the graph's own search does")
    # THE DATE RANGE TRAVELS TOO. The graph can be asked about a period now
    # (app/routers/api_graph.py), and a file exported from "Apple last year"
    # that held every connection in the archive would be read as the archive.
    data = api_graph.neighbours(
        ctx=ctx, request=request, db=db, q=q, id=node_id,
        date_from=_str(request, "from") or _str(request, "date_from"),
        date_to=_str(request, "to") or _str(request, "date_to"))
    centre = data.get("entity") or {}
    columns = ["from_id", "from", "to_id", "to", "to_type", "type", "reverse",
               "count", "group", "group_name", "colour", "first", "last"]
    rows = []
    for n in data.get("neighbours", []):
        for t in n.get("types", []):
            rows.append({
                "from_id": centre.get("id", ""), "from": centre.get("name", ""),
                "to_id": n.get("id", ""), "to": n.get("name", ""),
                "to_type": n.get("type", ""), "type": t.get("name", ""),
                "reverse": t.get("reverse", ""), "count": t.get("count"),
                "group": t.get("group", ""), "group_name": t.get("group_name", ""),
                "colour": t.get("colour", ""),
                "first": n.get("first"), "last": n.get("last"),
            })
    truncated = len(rows) > MAX_ROWS
    del rows[MAX_ROWS:]
    return columns, rows, {
        "q": data.get("q"), "id": data.get("id"), "entity": centre,
        "date_from": data.get("date_from"), "date_to": data.get("date_to"),
        "resolved": data.get("resolved"), "legend": data.get("legend"),
        "limit": data.get("limit"), "capped": data.get("capped"),
        "truncated": truncated,
    }


def _buckets(request: Request, ctx: Context, db: Database) -> Built:
    """One row per MEMBER, not per bucket: a bucket is what its members are,
    and a column holding six names separated by semicolons is not a table
    anybody can sort. A bucket with no members still gets its row, with the
    member columns empty, so it does not vanish from its own export."""
    data = api_settings.list_buckets(ctx=ctx, db=db)
    columns = ["bucket_id", "bucket", "project", "every_project",
               "member_count", "member", "member_type", "added"]
    rows = []
    for b in data.get("items", []):
        head = {"bucket_id": b["id"], "bucket": b["name"], "project": b.get("project") or "",
                "every_project": b.get("every_project"), "member_count": b.get("member_count"),
                "added": b.get("added")}
        members = b.get("members") or []
        if not members:
            rows.append({**head, "member": "", "member_type": ""})
        for m in members:
            rows.append({**head, "member": m.get("name", ""), "member_type": m.get("type", "")})
    truncated = len(rows) > MAX_ROWS
    del rows[MAX_ROWS:]
    return columns, rows, {"buckets": len(data.get("items", [])), "truncated": truncated}


def _colour_groups(request: Request, ctx: Context, db: Database) -> Built:
    data = api_settings.list_groups(db=db, project=_str(request, "project") or ctx.project)
    columns = ["id", "key", "name", "colour", "text_colour", "description",
               "sort", "fallback", "types", "types_all", "contrast"]
    rows = [{c: g.get(c) for c in columns} for g in data.get("groups", [])]
    return columns, rows, {
        "fallback": data.get("fallback"), "assigned": data.get("assigned"),
        "assigned_all": data.get("assigned_all"), "unassigned": data.get("unassigned"),
    }


def _colour_types(request: Request, ctx: Context, db: Database) -> Built:
    """The inverted view on the same page: one row per connection type, with
    the group it was put in and where that came from (manual or suggested)."""
    q = _str(request, "q")
    unassigned = _bool(request, "unassigned", False)
    size = api_settings.MAX_TYPES_PAGE_SIZE

    def fetch(page: int) -> dict[str, Any]:
        return api_settings.list_types(ctx=ctx, db=db, q=q, unassigned=unassigned,
                                       page=page, limit=size)

    items, first, truncated = _paged(fetch, size)
    columns = ["type", "connections", "group", "group_name", "colour", "text_colour", "source"]
    rows = [{c: t.get(c) for c in columns} for t in items]
    return columns, rows, {
        "q": q, "unassigned": unassigned, "total": first.get("total"),
        "fallback": first.get("fallback"), "truncated": truncated,
    }


def _logs_errors(request: Request, ctx: Context, db: Database) -> Built:
    rng = _str(request, "range", api_logs.DEFAULT_RANGE) or api_logs.DEFAULT_RANGE

    def fetch(page: int) -> dict[str, Any]:
        return api_logs.errors(db=db, range=rng, source_id=_opt_int(request, "source_id"),
                               kind=_str(request, "kind"), severity=_str(request, "severity"),
                               q=_str(request, "q"), page=page,
                               page_size=api_logs.MAX_PAGE_SIZE)

    items, first, truncated = _paged(fetch, api_logs.MAX_PAGE_SIZE)
    columns = ["date", "kind", "kind_label", "severity", "severity_label",
               "source_id", "source", "host", "uri", "http_status",
               "message", "detail", "run_id", "project", "task_id"]
    rows = [{
        "date": e.get("date"), "kind": e.get("kind", ""), "kind_label": e.get("kind_label", ""),
        "severity": e.get("severity", ""), "severity_label": e.get("severity_label", ""),
        "source_id": (e.get("source") or {}).get("id"),
        "source": (e.get("source") or {}).get("name", ""),
        "host": e.get("host", ""), "uri": e.get("uri", ""),
        "http_status": e.get("http_status"), "message": e.get("message", ""),
        "detail": e.get("detail", ""), "run_id": e.get("run_id", ""),
        "project": e.get("project", ""), "task_id": e.get("task_id", ""),
    } for e in items]
    return columns, rows, {
        "available": first.get("available"), "range": first.get("range"),
        "total": first.get("total"), "q": first.get("q"), "kind": first.get("kind"),
        "severity": first.get("severity"), "source_id": first.get("source_id"),
        "truncated": truncated,
    }


def _logs_runs(request: Request, ctx: Context, db: Database) -> Built:
    rng = _str(request, "range", api_logs.DEFAULT_RANGE) or api_logs.DEFAULT_RANGE

    def fetch(page: int) -> dict[str, Any]:
        return api_logs.runs(db=db, range=rng, source_id=_opt_int(request, "source_id"),
                             status=_str(request, "status"), q=_str(request, "q"),
                             page=page, page_size=api_logs.MAX_PAGE_SIZE)

    items, first, truncated = _paged(fetch, api_logs.MAX_PAGE_SIZE)
    columns = ["date", "source_id", "name", "status", "status_label", "key_label",
               "message", "pages", "links", "accepted", "new", "files", "submitted", "ms"]
    rows = []
    for r in items:
        counts = r.get("counts") or {}
        rows.append({
            "date": r.get("date"), "source_id": (r.get("source") or {}).get("id"),
            "name": r.get("name", ""), "status": r.get("status", ""),
            "status_label": r.get("status_label", ""), "key_label": r.get("key_label", ""),
            "message": r.get("message", ""),
            **{c: counts.get(c) for c in ("pages", "links", "accepted", "new", "files", "submitted")},
            "ms": r.get("ms"),
        })
    return columns, rows, {
        "available": first.get("available"), "range": first.get("range"),
        "total": first.get("total"), "q": first.get("q"),
        "status": first.get("status"), "source_id": first.get("source_id"),
        "truncated": truncated,
    }


def _logs(request: Request, ctx: Context, db: Database) -> Built:
    """The Log page's own name. It has two tabs and the open one is in the
    URL, so the file follows the tab rather than always exporting problems."""
    tab = _str(request, "tab", "errors") or "errors"
    return _logs_runs(request, ctx, db) if tab == "runs" else _logs_errors(request, ctx, db)


# ── The registry ────────────────────────────────────────────────────────
#
# name → (builder, the parameters it reads, one line of what is in the file).
# The names the top bar's menu asks for (`dashboard`, `colours`, `logs` -
# `view` in _layout.html) are here too, pointing at the table that page is
# about, so no view's Export menu can lead to a 404.

VIEWS: dict[str, tuple[Callable[[Request, Context, Database], Built], tuple[str, ...], str]] = {
    "dashboard-stats": (_dashboard_stats, (), "How many rows of each kind arrived in each period."),
    "dashboard": (_dashboard_stats, (), "The Home page's counters."),
    "activities": (_activities, ("limit",), "The activity feed: what arrived last."),
    "latest-events": (_latest_events, ("limit",), "The latest events with their sources."),
    "query": (_query, ("terms", "all", "address", "city", "region", "country",
                       "lat", "lng", "km", "from", "to"),
              "The search results, one row per match."),
    "events": (_events, ("by", "q", "from", "to"), "Events with their types, dates and entities."),
    "diagrams": (_diagrams, ("scope", "tab", "q", "axis", "timeframe", "page"),
                 "Every point of every chart on one tab."),
    "drilldown": (_drilldown, ("scope", "tab", "chart_id", "dataset_id", "q", "axis",
                               "timeframe", "page", "key_bucket", "key_x", "key_y"),
                  "The rows behind one point of one chart."),
    "map": (_map, ("q", "levels", "types", "hide", "connections", "locations"),
            "The places on the map; the lines are in the JSON."),
    "heatmap": (_heatmap, ("q", "bbox", "area", "types", "hide"),
                "The heat squares with their counts."),
    "graph": (_graph, ("q", "id"), "The connections around one entity or bucket."),
    "buckets": (_buckets, (), "Buckets and their members, one row per member."),
    "colour-groups": (_colour_groups, ("project",), "The colour groups and how many types are in each."),
    "colours": (_colour_groups, ("project",), "The colour groups and how many types are in each."),
    "colour-types": (_colour_types, ("q", "unassigned"), "Connection types and the group each is in."),
    "logs": (_logs, ("tab", "range", "source_id", "kind", "severity", "status", "q"),
             "The open tab of the Log: problems, or runs."),
    "logs-errors": (_logs_errors, ("range", "source_id", "kind", "severity", "q"),
                    "Everything the crawler had trouble with."),
    "logs-runs": (_logs_runs, ("range", "source_id", "status", "q"),
                  "Every crawl and every submission, with its counts."),
}

# On every view, whatever the view is: which project, which language, and -
# when one is chosen - whose perspective and how important a document has to
# be for it. The FILTERING is not done here: every builder below calls the
# view's own endpoint function with `ctx`, and a Context carries all four
# (app/context.py), so the file is cut by the same predicate the screen was.
# What this tuple does is put them in the envelope, so a person holding the
# file can see WHICH slice they are holding - a CSV of 300 rows and a screen
# of 300 rows that disagree about the perspective is an afternoon lost.
COMMON_PARAMS = ("project", "language", "perspective", "min_importance")


# ── Responses ───────────────────────────────────────────────────────────

def _slug(text: str) -> str:
    """The file-name rule. Character for character the same one export.js
    uses (`replace(/[^A-Za-z0-9_-]+/g, "_").slice(0, 40)`): the menu writes it
    into the link's `download` attribute and the server writes it into
    Content-Disposition, and two names for one file is a bug report."""
    return re.sub(r"[^A-Za-z0-9_-]+", "_", text or "")[:40] or "all"


def filename(view: str, ext: str, ctx: Context, now: datetime | None = None) -> str:
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%d-%H%M")
    return f"xtracting-{_slug(view)}-{_slug(ctx.project)}-{_slug(ctx.language)}-{stamp}.{ext}"


def _disposition(name: str) -> str:
    return f'attachment; filename="{name}"'


def _json_default(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def _filters(request: Request, params: tuple[str, ...]) -> dict[str, str]:
    """What the file was cut with - only the parameters this view reads, and
    only the ones that were actually given. An envelope listing eleven empty
    strings says nothing; four filled ones say what the reader is holding."""
    given = {}
    for name in COMMON_PARAMS + params:
        value = request.query_params.get(name)
        if value not in (None, ""):
            given[name] = value
    return given


def _respond(view: str, fmt: str, request: Request, ctx: Context, db: Database) -> Response:
    # Defensive: every registered path names a key of VIEWS, so this can only
    # fire on a typo in the registry. A name nobody registered is a plain 404
    # from the router - /api/export/views is where the names are.
    entry = VIEWS.get(view)
    if entry is None:
        raise HTTPException(404, {
            "error": f"there is nothing to export called {view!r}",
            "hint": "GET /api/export/views lists the names"})
    build, params, _ = entry
    columns, built, context = build(request, ctx, db)
    # The builders assemble their rows from several dicts, so the key order
    # they come out in is the order they were merged in. Both formats have to
    # follow the HEADER instead: a JSON object whose keys run in a different
    # order than `columns` is a table with two orders.
    rows = [{c: row.get(c) for c in columns} for row in built]
    truncated = bool(context.get("truncated"))
    name = filename(view, fmt, ctx)
    headers = {"Content-Disposition": _disposition(name)}
    if truncated:
        # A CSV cannot carry a note without becoming a broken CSV, so the one
        # place both formats can say it is the header.
        headers["X-Export-Truncated"] = "true"
        headers["X-Export-Row-Limit"] = str(MAX_ROWS)

    if fmt == "csv":
        buffer = io.StringIO()
        writer = csv.writer(buffer, lineterminator="\r\n")
        writer.writerow(columns)
        for row in rows:
            writer.writerow([_cell(row.get(c)) for c in columns])
        # The byte order mark is what makes Excel read the file as UTF-8.
        body = "﻿" + buffer.getvalue()
        return Response(body, media_type="text/csv; charset=utf-8", headers=headers)

    payload = {
        "export": {
            "view": view, "format": "json",
            "project": ctx.project, "language": ctx.language,
            "generated": datetime.now(timezone.utc),
            "filters": _filters(request, params),
            "columns": columns, "rows": len(rows),
            "truncated": truncated, "row_limit": MAX_ROWS,
            "request_limit": MAX_REQUESTS,
        },
        "context": {k: v for k, v in context.items() if k != "truncated"},
        "rows": rows,
    }
    return Response(json.dumps(payload, default=_json_default, ensure_ascii=False, indent=1),
                    media_type="application/json; charset=utf-8", headers=headers)


def _handler(view: str, fmt: str):
    """One endpoint per (view, format). Registered by hand rather than through
    a `/api/export/{view}.{fmt}` catch-all, because a catch-all would also
    swallow `/api/export/sources.csv`, which belongs to api_sources."""

    def endpoint(request: Request, ctx: ContextDep, db: Database = Depends(get_db)) -> Response:
        return _respond(view, fmt, request, ctx, db)

    endpoint.__name__ = f"export_{view.replace('-', '_')}_{fmt}"
    endpoint.__doc__ = f"{VIEWS[view][2]} Parameters: " + ", ".join(
        COMMON_PARAMS + VIEWS[view][1]) + "."
    return endpoint


def _register() -> None:
    """Two routes per name, at import time. In a function so the loop
    variables do not survive as module-level names."""
    for view in VIEWS:
        for fmt in ("csv", "json"):
            router.add_api_route(
                f"/api/export/{view}.{fmt}", _handler(view, fmt), methods=["GET"],
                name=f"export_{view}_{fmt}", summary=f"{view} as {fmt.upper()}",
                response_class=Response)


_register()


@router.get("/api/export/views")
def export_views() -> dict[str, Any]:
    """What can be exported, and with which parameters.

    Here so the list is checkable from outside - a view whose Export menu
    points at a name that is not in this answer is a dead control."""
    return {
        "formats": ["csv", "json"],
        "row_limit": MAX_ROWS,
        "request_limit": MAX_REQUESTS,
        "views": [{"view": name, "about": about,
                   "parameters": list(COMMON_PARAMS + params),
                   "csv": f"/api/export/{name}.csv", "json": f"/api/export/{name}.json"}
                  for name, (_, params, about) in VIEWS.items()],
        "elsewhere": [SOURCES_ELSEWHERE],
    }
