"""The Map and the Heatmap: where the entities are, and how they connect.

    GET /api/map/entity?q&axis&levels&types&connections&locations
    GET /api/map/heat?q&bbox&types&entity&entity_axis&date_from&date_to
    GET /api/map/spot?lat&lng&<the same filters>&ddpage

Both answer about ONE project in ONE language, like every other endpoint, and
both draw only what the archive itself knows: `processed_data.locations` is
the geocoder, there is no external one, and an address without coordinates is
a row in a list rather than a pin on a map.

WHAT A LEVEL IS. Level 0 is the search term - the entities `app/scope.py`
resolves it to, so a BUCKET named Apple starts with all its members. Level 1
adds everything connected to those, level 2 everything connected to THOSE,
up to three, which is where a picture of a network stops being a picture.
The expansion follows `text_entity_id` and not (task, id): the extraction
reuses an id whenever it recognises the same thing again, so "Foxconn found
in the battery document" and "Foxconn found in the supplier filing" are one
company with one factory - the alternative is six disconnected drawings of
the same six documents. Inside a document the id is still what a connection
points at, which is why the join is on the id and the project, never on the
name.

WHAT A COLOUR MEANS. A connection type is free text ("Supplier", "Lieferant",
"manufactured-by"), so the map colours by GROUP - `app/colours.py` resolves
type to group to colour, from the table a customer edits, never by a regex at
runtime. Every line the map draws therefore has the same colour as the same
relationship on the graph, and the legend is the same list.

TWO AXES ON THE ENTITY BOX OF BOTH VIEWS. A term is read either as one thing
- an entity or a bucket - or as an EVENT TYPE, and an event type resolves to
the entities its events are about (app/scope.py). "Where does a Product
launch happen" is then a map and a heat field, and nothing below the
resolution knows which question was asked. MAP_AXES holds the pair.

WHAT THE SEARCH BOX ON EACH VIEW ASKS FOR. The Map's is an entity - a map of
Apple is a map of Apple's places. The Heatmap's is a PLACE, because that view
counts addresses and nothing else: `q=Springfield` counts the squares of every
place the archive calls Springfield and names them, `q=USA` every address in
the country. The reading of the name is the Query view's own (places.disambiguate),
so one archive answers one name one way wherever it is typed.

THE HEATMAP'S `entity` IS A SECOND AXIS, NEVER A REPLACEMENT FOR `q`. The old
dashboard's heat view searched entities and had no place box at all, so "where
does this archive cluster in the Netherlands" could not be asked. Both are here
now and they AND together: `q=Rotterdam&entity=Apple` is "Apple's addresses in
Rotterdam". `entity` is resolved through app/scope.py like every other entity
box, so a bucket name counts every member.

`date_from` / `date_to` narrow the heat grid on the location's own timeline
column, with the same names, the same parsing and the same inclusive end as
every other date range in the dashboard.

THE CAPS ARE REAL, AND ONE OF THEM IS OFF BY DEFAULT.
DASHBOARD_MAP_MAX_ENTITIES stopped the expansion at 500 entities, which
ordinary searches were reaching: the answer said `capped: true`, the page said
so, and a reader was told the truth about half a network rather than shown the
whole one. It defaults to 0 - no limit - and a number can be put back in the
environment for an archive where a hub makes the browser crawl.

What still bounds a runaway is MAX_EDGE_ROWS: 5000 edge rows per hop, three
hops, so the growth per search has a ceiling whatever the entity cap says. The
heat grid rounds to three decimals - about 110 m - and stops at 20000 cells for
the same reason.

WHAT IS IN ONE SPOT OF THAT GRID (`/spot`). A field of colour has no clickable
parts and no per-spot object anywhere - the only key a spot has is the pair of
rounded numbers `/heat` answered with. So a click sends those two numbers back,
this file re-derives the cell with the same rounding, RE-APPLIES THE SAME FOUR
FILTERS the field was drawn with (_heat_filters, which both endpoints call so
neither can forget one), and app/charts/places.py lists the entities inside it,
most results first, a page at a time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from psycopg import sql

from .. import config as app_config
from .. import places
from ..charts import places as spots
from ..colours import FALLBACK_KEY, ColourResolver, get_resolver
from ..context import Context, ContextDep
from ..db import Database, get_db
from ..scope import ScopeError, groupings, holders_for, member_holders, resolve_scope
from ..sqlbuild import (identity, identity_params, like_substring,
                        perspective_params, perspective_scope, timeline)

router = APIRouter(prefix="/api/map", tags=["map"])

MAX_LEVELS = 3
# Per hop. A hop over a busy hub can return tens of thousands of rows, and
# nothing on the map can show them; the answer says `capped` instead.
MAX_EDGE_ROWS = 5000
# The heat grid: three decimals is ~110 m at the equator, which is finer than
# the addresses the extraction writes are accurate to.
HEAT_PRECISION = 3
MAX_HEAT_CELLS = 20000
# How many squares the page lists by name. The list is the heat layer's text
# alternative, and a text alternative nobody scrolls to the end of is a wall;
# the page shows the busiest twenty of these.
TOP_SQUARES = 50
# How many of the places a name covers the answer names. "USA" is one
# candidate, "Springfield" two, and a typed fragment can be dozens - a
# caption that lists dozens is not a caption.
MAX_PLACE_NAMES = 12

# Line width for a pair, by how many connections it carries.
#
# THE WHOLE RANGE WAS HALVED, NOT FLATTENED. It was 3-9 px of colour inside a
# casing and a keyline, which drew a single connection as a 9 px band and a
# busy one as 15 px: over a real archive the map was a bundle of ribbons with
# the coastline underneath them. 1.5-4.5 px keeps the RATIO exactly as it was
# (the busiest pair is three times the width of a single one, and that
# difference is the only thing the width means), and static/js/map.js scales
# the two strokes around the colour by the same half - so the whole drawing
# is thinner and nothing about it says anything different.
MIN_WEIGHT = 1.5
MAX_WEIGHT = 4.5


def _bad(error: str, hint: str) -> HTTPException:
    return HTTPException(400, {"error": error, "hint": hint})


# WHAT THE SEARCH BOX ASKS, AND THE TWO AXES IT ASKS ON.
#
# Both views take one term and read it one of two ways, the way every
# Diagrams page does (app/scope.py: AXES):
#
#   object   an entity or a bucket. The map of Apple.
#   type     an EVENT TYPE. The map of everything a "Product launch"
#            happened to: app/scope.py's events scope, type axis, resolves
#            it to the entities those events are ABOUT (through
#            event_entities), and from there this file cannot tell the two
#            apart - one ScopeSet, one expansion, one drawing, one export.
#
# The pair is (scope kind, scope axis), so the events scope's own default
# (which is "type") is never relied on: the axis a caller asked for is the
# axis that is passed.
MAP_AXES: dict[str, tuple[str, str]] = {
    "object": ("entity", "object"),
    "type": ("events", "type"),
}

# What to type, per axis, for the error a bad term produces.
AXIS_HINTS = {
    "object": "search for an entity or a bucket name",
    "type": "search for an event type, or switch back to Object",
}


def _axis(value: str) -> str:
    """The axis a request asked for. Unknown is a 400 and not a silent
    fallback: a map drawn on the other axis is a different map."""
    axis = (value or "object").strip().lower()
    if axis not in MAP_AXES:
        raise _bad(f"unknown axis {axis!r}",
                   "axis=object for an entity or a bucket, axis=type for an event type")
    return axis


def _config(request: Request):
    cfg = getattr(request.app.state, "cfg", None)
    return cfg if cfg is not None else app_config.load()


def _split(value: str) -> list[str]:
    """A comma-separated filter list from the query string. Empty entries are
    dropped, so `types=,,Supplier,` is one type and not four."""
    return [part.strip() for part in (value or "").split(",") if part.strip()]


def _weight(count: int, most: int) -> float:
    """MIN_WEIGHT for a single connection, MAX_WEIGHT for the busiest pair on
    the map.

    Linear in the SHARE, not in the count: one pair with 200 connections must
    not squash every other line to the minimum."""
    if most <= 1:
        return MIN_WEIGHT
    share = (max(1, count) - 1) / (most - 1)
    return round(MIN_WEIGHT + share * (MAX_WEIGHT - MIN_WEIGHT), 2)


# ── The archive side ─────────────────────────────────────────

_SCOPE_IDS = "SELECT DISTINCT id FROM ent WHERE id IS NOT NULL AND id <> ''"

# THE SAME SEED, NARROWED BY THE READER'S PERSPECTIVE - AND ONLY THE SEED.
#
# This is a real decision and not an oversight, so it is written down twice:
# here, and on the Map page itself under its heading (templates/map.html).
#
# The perspective filter asks "was this written in a document important
# enough for this reader" (app/sqlbuild.py: perspective_scope). An entity is
# named in a document, so the question has an answer for the search term.
# The EXPANSION is a graph walk: level 1 is what the term is connected to,
# level 2 what those are connected to. Filtering each hop by the importance
# of the document that recorded it would cut paths IN THE MIDDLE - a
# supplier two steps away would vanish because the one document that names
# the company between them is Low Importance, and the map would draw two
# disconnected halves of a network with nothing on screen saying a link had
# been removed. That is a picture nobody asked for and cannot tell from the
# truth.
#
# So: the filter decides WHERE THE MAP STARTS, and the network around that
# start is drawn whole. Only used when a perspective is actually chosen -
# unfiltered, the cheap read straight off the CTE above is the same answer.
#
# The language is deliberately not bound here, exactly as the `ent` CTE does
# not bind it (app/scope.py): one entity id is one thing in every language,
# and the importance lookup inside the predicate binds the language itself.
_SCOPE_IDS_FILTERED = """
    SELECT DISTINCT e.text_entity_id AS id
      FROM processed_data.entities e
     WHERE e.text_project = %(project)s
       AND (e.text_task_id, e.text_entity_id) IN (SELECT task_id, id FROM ent)
       AND e.text_entity_id IS NOT NULL AND e.text_entity_id <> ''
       AND {persp}
"""

# Every connection with at least one end in the frontier, aggregated per
# (parent, child, both type names). Both id columns are indexed
# (idx_connections_parent / idx_connections_child), which is what makes a
# three-level expansion three index scans rather than three table scans.
_EDGES = """
    SELECT c.text_fk_parent_entity_id  AS parent,
           c.text_fk_child_entity_id   AS child,
           c.text_type_parent_to_child AS type_ptc,
           c.text_type_child_to_parent AS type_ctp,
           count(*)                    AS n,
           min(c.date_commissioned)    AS first_seen,
           max(c.date_commissioned)    AS last_seen
      FROM processed_data.connections c
     WHERE {idn}
       AND c.text_fk_parent_entity_id IS NOT NULL AND c.text_fk_parent_entity_id <> ''
       AND c.text_fk_child_entity_id  IS NOT NULL AND c.text_fk_child_entity_id  <> ''
       AND c.text_fk_parent_entity_id <> c.text_fk_child_entity_id
       AND (c.text_fk_parent_entity_id = ANY(%(frontier)s)
            OR c.text_fk_child_entity_id = ANY(%(frontier)s))
     GROUP BY 1, 2, 3, 4
     ORDER BY n DESC, parent, child
     LIMIT %(edge_cap)s
"""

# The name and type an id is shown under. An id may carry two spellings
# across documents; the one that occurs most often wins, and ties go to the
# alphabet so the map does not change its labels between two loads.
_NAMES = """
    SELECT e.text_entity_id AS id, e.text_name AS name, e.text_type AS type, count(*) AS n
      FROM processed_data.entities e
     WHERE {idn} AND e.text_entity_id = ANY(%(ids)s) AND e.text_name <> ''
     GROUP BY 1, 2, 3
     ORDER BY id, n DESC, name
"""

# One marker per (entity, address, type): the same factory reported by four
# documents is one pin with a count, not four pins on top of each other.
_LOCATIONS = """
    SELECT l.text_fk_entity_id AS id, l.text_address AS address, l.text_type AS type,
           avg(l.float_latitude)  AS lat,
           avg(l.float_longitude) AS lng,
           count(*) AS n
      FROM processed_data.locations l
     WHERE {idn} AND l.text_fk_entity_id = ANY(%(ids)s)
       AND l.float_latitude IS NOT NULL AND l.float_longitude IS NOT NULL
     GROUP BY 1, 2, 3
     ORDER BY id, n DESC, address
"""


def _fetch_edges(conn, ctx, frontier: list[str]) -> tuple[list[dict[str, Any]], bool]:
    rows = conn.execute(
        sql.SQL(_EDGES).format(idn=identity("c")),
        {**identity_params(ctx.project, ctx.language),
         "frontier": frontier, "edge_cap": MAX_EDGE_ROWS + 1}).fetchall()
    capped = len(rows) > MAX_EDGE_ROWS
    return [dict(r) for r in rows[:MAX_EDGE_ROWS]], capped


def _fetch_names(conn, ctx, ids: list[str]) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    if not ids:
        return out
    rows = conn.execute(sql.SQL(_NAMES).format(idn=identity("e")),
                        {**identity_params(ctx.project, ctx.language), "ids": ids}).fetchall()
    for r in rows:
        out.setdefault(r["id"], {"name": r["name"], "type": r["type"] or ""})
    return out


def _fetch_locations(conn, ctx, ids: list[str]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    if not ids:
        return out
    rows = conn.execute(sql.SQL(_LOCATIONS).format(idn=identity("l")),
                        {**identity_params(ctx.project, ctx.language), "ids": ids}).fetchall()
    for r in rows:
        out.setdefault(r["id"], []).append({
            "address": r["address"] or "",
            "type": r["type"] or "",
            "lat": float(r["lat"]), "lng": float(r["lng"]),
            "count": int(r["n"]),
        })
    return out


# ── Connections by bucket ────────────────────────────────────
#
# WHY THIS IS A REMAP AND NOT A SECOND QUERY. The network is fetched exactly
# once, by entity, because that is what the archive stores: connections are
# between entities and buckets are the dashboard's own idea of which entities a
# reader means as one thing. Asking the archive for "connections of a bucket"
# would mean expanding the bucket into its members and asking for those - which
# is what has already happened by the time this runs.
#
# AN ENTITY MAY BE IN SEVERAL BUCKETS, AND THEN IT IS IN ALL OF THEM. "Angela
# Merkel" can sit in "Germany" and in "EU leaders", and a connection to her is a
# connection to both: the map draws one line to each. Folding to the first
# bucket would drop the rest with nothing on screen saying so. That is the whole
# of what "even if entities inside the buckets are duplicated" asks for.
#
# An entity in no bucket stays itself. A bucket-only map would hide most of an
# archive behind a handful of groupings somebody happened to have made.


def _bucket_groups(conn, ctx, ids: list[str],
                   names: dict[str, dict[str, str]]) -> dict[str, list[str]]:
    """Entity id -> the ids it is drawn as. Its buckets, or itself.

    A bucket's id is `bucket:<name>` and cannot collide with an entity id,
    which is the archive's own opaque key.
    """
    holders = member_holders(conn, ctx.project, "entity")
    out: dict[str, list[str]] = {}
    for eid in ids:
        info = names.get(eid, {})
        buckets = holders_for(holders, info.get("name", ""), info.get("type", ""))
        out[eid] = [f"bucket:{b}" for b in buckets] or [eid]
    return out


def _regroup(groups_of: dict[str, list[str]], level_of: dict[str, int],
             edges: list[dict[str, Any]], names: dict[str, dict[str, str]],
             places: dict[str, list[dict[str, Any]]]):
    """The same network, keyed by bucket instead of by entity.

    A bucket's level is the smallest of its members' - it is as close to the
    search as its nearest member, and drawing "Germany" at level 2 because one
    obscure member is far away would put it in the wrong ring.

    Its addresses are its members' addresses, merged and ordered by how often
    they are carried, which is the rule ONE entity's main address already
    follows (_LOCATIONS orders that way). So "the address a bucket carries most
    often" means the same thing as it does for an entity.

    A connection between two entities of the SAME bucket is dropped: a line from
    a thing to itself is not a connection, it is a loop the map cannot draw.
    """
    new_levels: dict[str, int] = {}
    for eid, level in level_of.items():
        for gid in groups_of.get(eid, [eid]):
            if gid not in new_levels or level < new_levels[gid]:
                new_levels[gid] = level

    new_names: dict[str, dict[str, str]] = {}
    merged_places: dict[str, dict[tuple[str, str], dict[str, Any]]] = {}
    for eid, gids in groups_of.items():
        for gid in gids:
            if gid.startswith("bucket:"):
                new_names.setdefault(gid, {"name": gid[len("bucket:"):], "type": "bucket"})
            else:
                new_names.setdefault(gid, names.get(eid, {"name": eid, "type": ""}))
            spots = merged_places.setdefault(gid, {})
            for spot in places.get(eid, []):
                key = (spot["address"], spot["type"])
                found = spots.get(key)
                if found is None:
                    spots[key] = dict(spot)
                else:
                    # The same address under two members is one place the
                    # bucket carries twice, not two places.
                    found["count"] += spot["count"]

    new_places = {
        gid: sorted(spots.values(), key=lambda s: (-s["count"], s["address"]))
        for gid, spots in merged_places.items()
    }

    new_edges: list[dict[str, Any]] = []
    for e in edges:
        for a in groups_of.get(e["parent"], [e["parent"]]):
            for b in groups_of.get(e["child"], [e["child"]]):
                if a == b:
                    continue
                new_edges.append({**e, "parent": a, "child": b})

    return new_levels, new_edges, new_names, new_places


# ── Building the answer ──────────────────────────────────────

def _pair_key(a: str, b: str) -> tuple[str, str]:
    """A connection is drawn once, whichever way round the extraction wrote
    it: the preseed holds Foxconn→Apple AND Apple→Foxconn for one fact, and
    two lines between two pins is a picture of the archive's shape, not of
    the world's."""
    return (a, b) if a <= b else (b, a)


def _type_entries(edges: Iterable[dict[str, Any]], resolver: ColourResolver,
                  near: str) -> list[dict[str, Any]]:
    """The types on one pair, most frequent first, each with its colour.

    WHICH OF THE TWO NAMES A LINE IS LABELLED WITH. The archive names both
    ends of a connection: (Foxconn, Apple, "Supplier", "Customer") means
    Foxconn supplies Apple - `text_type_parent_to_child` is the PARENT's
    role, `text_type_child_to_parent` the child's. A line drawn outward from
    the search term is labelled with the FAR end's role, the same way the
    graph labels the ring around its centre ("Foxconn - Supplier"), so one
    relationship has one name and one colour on both views.

    That also folds the reversed duplicates the extraction writes: (Foxconn,
    Apple, Supplier, Customer) and (Apple, Foxconn, Customer, Supplier) are
    one fact written twice, and seen from Apple they are both "Supplier".
    """
    merged: dict[str, dict[str, Any]] = {}
    for e in edges:
        if e["parent"] == near:
            far, name, reverse = e["child"], e["type_ctp"], e["type_ptc"]
        else:
            far, name, reverse = e["parent"], e["type_ptc"], e["type_ctp"]
        name = (name or reverse or "").strip()
        if not name:
            continue
        entry = merged.get(name)
        if entry is None:
            colour = resolver.resolve(name)
            entry = merged[name] = {
                "name": name, "reverse": (reverse or "").strip(), "entity": far, "count": 0,
                "group": colour.group_key, "group_name": colour.group.name,
                "colour": colour.colour, "text_colour": colour.group.text_colour,
            }
        entry["count"] += int(e["n"])
    # A tie between a grouped type and an unassigned one goes to the grouped
    # one: the pair is better drawn in the colour of a group somebody chose
    # than in the fallback of a type nobody has classified yet.
    return sorted(merged.values(), key=lambda t: (-t["count"], t["group"] == FALLBACK_KEY, t["name"]))


def _bounds(points: list[tuple[float, float]]) -> list[list[float]] | None:
    if not points:
        return None
    lats = [p[0] for p in points]
    lngs = [p[1] for p in points]
    return [[min(lats), min(lngs)], [max(lats), max(lngs)]]


@router.get("/entity")
def map_entity(ctx: ContextDep, request: Request, db: Database = Depends(get_db),
               q: str = Query("", description="an entity or a bucket, or an event type on axis=type"),
               axis: str = Query("object", description="object: an entity or a bucket; type: an event type"),
               levels: int = Query(1, ge=1, le=MAX_LEVELS),
               types: str = Query("", description="connection type names, comma separated"),
               mode: str = Query("connections", description=(
                   "connections: the network, every entity at its main address; "
                   "locations: every address of the search, without its connections")),
               group: str = Query("entities", description=(
                   "entities: one node per entity; "
                   "buckets: one node per bucket, and an entity in several "
                   "buckets is drawn in each of them"))):
    """The entities a term names, their addresses, and the lines between them.

    `axis` says how the term is read - one entity or bucket, or an event
    type whose events name the entities (MAP_AXES above). Everything after
    the resolution is the same on both: the same expansion, the same
    markers, the same lines, the same caps.

    `mode` is the OTHER question the page asks, and the two are exclusive
    because each needs the other one's pins to be gone:

      connections  the network around the term - the levels are followed,
                   the lines are drawn, and every entity is placed at ONE
                   address, the one it carries most often. A line has to end
                   somewhere: a company with nine offices drawn nine times
                   makes nine ends of the same relationship, and the reader
                   cannot tell which is the company.
      locations    every address of the entity that was searched, and no
                   expansion and no lines at all.

    Not two independent checkboxes, both on by default: a search for one
    politician would then cover the map with the addresses of every company
    they are connected to - with nothing on screen saying whose addresses
    those are. Which is the honest answer depends on the
    question, so the page asks one at a time.
    """
    cfg = _config(request)
    term = (q or "").strip()
    # An unknown value is the default rather than a 400: the mode is a view
    # switch, and a stale bookmark should draw the map, not an error.
    mode = "locations" if (mode or "").strip().lower() == "locations" else "connections"
    expand = mode == "connections"
    # Unknown values fall back for the same reason `mode` does: this is a view
    # switch, and a stale bookmark should draw the map rather than answer 400.
    grouping = "buckets" if (group or "").strip().lower() == "buckets" else "entities"
    wanted = set(_split(types))
    resolver = get_resolver()
    which = _axis(axis)
    kind, scope_axis = MAP_AXES[which]

    empty: dict[str, Any] = {
        "q": term, "axis": which, "levels": levels,
        "mode": mode,
        "resolved": {"kind": kind, "axis": scope_axis, "q": term, "label": term,
                     "match": "none", "names": [], "bucket": None,
                     "entities": 0, "sources": 0},
        "grouping": grouping,
        "entities": [], "markers": [], "connections": [], "types": [],
        "legend": resolver.legend(), "bounds": None, "capped": False,
        "max_entities": cfg.map_max_entities,
        "project": ctx.project, "language": ctx.language,
    }
    if not term:
        # Not an error: the page opens without a term and says what to type.
        return empty

    with db.read() as conn:
        try:
            scope = resolve_scope(conn, ctx, kind, term, axis=scope_axis)
        except ScopeError as exc:
            raise _bad(str(exc), AXIS_HINTS[which])
        empty["resolved"] = dict(scope.resolved)

        # THE SEED IS FILTERED; THE EXPANSION BELOW IS NOT. _SCOPE_IDS_FILTERED
        # says why in full, and templates/map.html says it to the reader.
        seed = sql.SQL(_SCOPE_IDS)
        seed_params = dict(scope.params)
        if ctx.filtered:
            seed = sql.SQL(_SCOPE_IDS_FILTERED).format(
                persp=perspective_scope(ctx.perspective, "e"))
            seed_params.update({
                **identity_params(ctx.project, ctx.language),
                **perspective_params(ctx.perspective, ctx.min_importance)})
        rows = conn.execute(
            sql.SQL("{w}{s}").format(w=scope.with_clause(), s=seed),
            seed_params).fetchall()
        level_of: dict[str, int] = {r["id"]: 0 for r in rows}
        if not level_of:
            return {**empty, "resolved": dict(scope.resolved)}

        edges: list[dict[str, Any]] = []
        seen_edges: set[tuple[str, str, str, str]] = set()
        capped = False
        frontier = sorted(level_of)

        if expand:
            for hop in range(1, levels + 1):
                if not frontier:
                    break
                found, hop_capped = _fetch_edges(conn, ctx, frontier)
                capped = capped or hop_capped
                nxt: list[str] = []
                for e in found:
                    key = (e["parent"], e["child"], e["type_ptc"] or "", e["type_ctp"] or "")
                    if key not in seen_edges:
                        seen_edges.add(key)
                        edges.append(e)
                    for side in (e["parent"], e["child"]):
                        if side in level_of:
                            continue
                        # 0 is no limit, and it is the default: a network is
                        # drawn whole or the page says it was cut, and "cut at
                        # 500" was being hit by ordinary searches. What still
                        # bounds a runaway is MAX_EDGE_ROWS per hop above.
                        if cfg.map_max_entities and len(level_of) >= cfg.map_max_entities:
                            capped = True
                            continue
                        level_of[side] = hop
                        nxt.append(side)
                frontier = nxt

        ids = sorted(level_of)
        names = _fetch_names(conn, ctx, ids)
        places = _fetch_locations(conn, ctx, ids)

        # Drawn by bucket, on the network that was already fetched by entity.
        # Only on the connections view: Locations draws the addresses of the
        # SEARCH and nothing else, and a bucket there would answer a question
        # nobody asked.
        if grouping == "buckets" and expand:
            groups_of = _bucket_groups(conn, ctx, ids, names)
            level_of, edges, names, places = _regroup(
                groups_of, level_of, edges, names, places)
            ids = sorted(level_of)

    # ── The drawable shape ───────────────────────────────────
    entities = []
    for eid in ids:
        info = names.get(eid, {"name": eid, "type": ""})
        # _LOCATIONS orders each entity's addresses by how often it carries
        # them, so the first row IS the main address - which is the only one
        # the connections view draws.
        spots = places.get(eid, [])[:1] if expand else places.get(eid, [])
        entities.append({
            "id": eid, "name": info["name"], "type": info["type"],
            "level": level_of[eid],
            "lat": spots[0]["lat"] if spots else None,
            "lng": spots[0]["lng"] if spots else None,
            "address": spots[0]["address"] if spots else "",
            # How many addresses the entity HAS, not how many are drawn -
            # the connections view draws one and the list beside the map
            # says "1 of 9" from this number.
            "locations": len(places.get(eid, [])),
        })
    entities.sort(key=lambda e: (e["level"], e["name"].lower()))
    by_id = {e["id"]: e for e in entities}

    markers = []
    for eid in ids:
        for spot in (places.get(eid, [])[:1] if expand else places.get(eid, [])):
            markers.append({
                "entity_id": eid, "entity": by_id[eid]["name"], "entity_type": by_id[eid]["type"],
                "level": level_of[eid], **spot,
            })

    # One line per pair of entities. The list of type names a person can tick
    # off is built from the SAME entries the lines are labelled with - a
    # checklist that offered "Customer" while the line said "Supplier" would
    # be a filter nobody could predict.
    pairs: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for e in edges:
        pairs.setdefault(_pair_key(e["parent"], e["child"]), []).append(e)

    checklist: dict[str, dict[str, Any]] = {}
    drawn = []
    for (a, b), group in sorted(pairs.items()):
        left, right = by_id.get(a), by_id.get(b)
        if not left or not right:
            continue
        # The end closer to the search term is the "near" one, and the line is
        # labelled with what the FAR one is. Two ends on the same level fall
        # back to the pair's own order, so the label does not change between
        # two loads of the same map.
        near = a if left["level"] <= right["level"] else b
        entries = _type_entries(group, resolver, near)
        if left["level"] == right["level"] and entries and entries[0]["group"] == FALLBACK_KEY:
            # Neither end is closer to the search, and the far end's role is a
            # type nobody has grouped. The other reading of the same pair may
            # well be one that IS grouped ("Regulator" against "Regulated
            # entity"), and a line with a meaning beats a grey one.
            other = _type_entries(group, resolver, b if near == a else a)
            if other and other[0]["group"] != FALLBACK_KEY:
                entries = other
        if not entries:
            continue
        for entry in entries:
            row = checklist.setdefault(entry["name"], {**entry, "count": 0})
            row["count"] += entry["count"]

        kept = [t for t in entries if not wanted or t["name"] in wanted]
        if not kept:
            continue
        total = sum(t["count"] for t in kept)
        first = min((e["first_seen"] for e in group if e["first_seen"]), default=None)
        last = max((e["last_seen"] for e in group if e["last_seen"]), default=None)
        drawn.append({
            "from": {"id": a, "name": left["name"], "lat": left["lat"], "lng": left["lng"]},
            "to": {"id": b, "name": right["name"], "lat": right["lat"], "lng": right["lng"]},
            "level": max(left["level"], right["level"]),
            "count": total, "types": kept,
            "group": kept[0]["group"], "group_name": kept[0]["group_name"],
            "colour": kept[0]["colour"],
            "first": first.isoformat() if first else None,
            "last": last.isoformat() if last else None,
            # Both ends need coordinates before a line can be drawn; the pair
            # is still listed, so the page can say how many are not drawable.
            "drawable": left["lat"] is not None and right["lat"] is not None,
        })

    most = max((c["count"] for c in drawn), default=1)
    for line in drawn:
        line["weight"] = _weight(line["count"], most)
    drawn.sort(key=lambda c: (-c["count"], c["from"]["name"]))

    used = {c["group"] for c in drawn}
    legend = [g for g in resolver.legend() if g["key"] in used] or resolver.legend()
    for entry in legend:
        entry["count"] = sum(1 for c in drawn if c["group"] == entry["key"])

    return {
        "q": term, "axis": which, "levels": levels,
        "mode": mode,
        "grouping": grouping,
        "resolved": dict(scope.resolved),
        "entities": entities,
        "markers": markers,
        "connections": drawn,
        "types": sorted(checklist.values(), key=lambda t: (-t["count"], t["name"])),
        "legend": legend,
        "bounds": _bounds([(m["lat"], m["lng"]) for m in markers]),
        "capped": capped,
        "max_entities": cfg.map_max_entities,
        "project": ctx.project, "language": ctx.language,
    }


# ── The place the search box names ───────────────────────────
#
# WHY THIS VIEW HAS A SEARCH BOX AT ALL, AND WHY ITS BOX ASKS FOR A PLACE.
# Every box in the dashboard searches the thing its view is about - entities
# on the Map and the Graph, event types on Events, anything on Summary. This
# view counts ADDRESSES, so a place is the only search that belongs to it,
# and without one the page can be asked a different question only by dragging
# a map: the one gesture NN/g's older-users guidance says must never be the
# only way in.
#
# The name is read with the Query view's rule (places.disambiguate): a city
# name beats a country name beats a fragment of an address. Where the Query
# view then has to ASK which Springfield is meant - a list of texts that
# silently drops one of two towns looks complete and is not - this one counts
# both and says which two it counted. "13 locations in 9 squares" is a
# question about squares, not about a single town, and a count of everything
# the archive calls Springfield is a true answer to it.
def _resolve_place(conn, ctx: Context, term: str) -> places.PlaceMatch:
    """What a typed place name means here, from dashboard.places."""
    rows = [dict(r) for r in conn.execute(
        places.places_query(),
        {"project": ctx.project, "language": ctx.language,
         "q": term.lower(), "q_sub": like_substring(term)}).fetchall()]
    return places.disambiguate(rows, term)


def _place_names(match: places.PlaceMatch) -> list[str]:
    """What to call the places a name covers - as ADDRESSES, the form the
    archive writes and every other list on the page shows: "Springfield,
    Illinois, USA", not the "Springfield - Illinois, USA" of a chooser, whose
    dash is there to separate a name from the parts that tell it apart in a
    list of radio buttons. This goes into a sentence.

    An address candidate already carries the whole address in `city`
    (places.disambiguate writes it there); a country candidate is its country
    and nothing else; a city candidate is its parts, joined. The rule is the
    one query.js sends back to the server (addressOf), so a name resolved
    here and a name resolved there are the same string."""
    if match.match == "address":
        return [c.city or "" for c in match.candidates if c.city]
    if match.match == "country":
        return [c.country or "" for c in match.candidates if c.country]
    return [", ".join(p for p in (c.city, c.region, c.country) if p)
            for c in match.candidates if c.city or c.country]


# ── The heat grid ────────────────────────────────────────────

# WHAT A SQUARE IS CALLED. Two decimal numbers are not a place: a list of
# "37.332, -122.031" names nothing a customer recognises, and that list is
# the heat map's text alternative. The archive already holds the answer -
# every location carries `text_address` - so the most frequent address in a
# square is its name, with the coordinates kept underneath for the reader
# who wants them. `mode()` reads the same column dashboard.places is built
# from; going through the view would cost a join on rounded coordinates to
# get the same string back.
_HEAT = """
    SELECT round(l.float_latitude::numeric,  {p}) AS lat,
           round(l.float_longitude::numeric, {p}) AS lng,
           count(*) AS n,
           count(DISTINCT l.text_fk_entity_id) AS entities,
           mode() WITHIN GROUP (ORDER BY btrim(l.text_address))
             FILTER (WHERE btrim(coalesce(l.text_address, '')) <> '') AS address
      FROM processed_data.locations l
     WHERE {idn}
       AND l.float_latitude IS NOT NULL AND l.float_longitude IS NOT NULL
       AND {where}
     GROUP BY 1, 2
     ORDER BY n DESC, lat, lng
     LIMIT %(cells)s
"""

_HEAT_TYPES = """
    SELECT l.text_type AS name, count(*) AS n
      FROM processed_data.locations l
     WHERE {idn}
       AND l.float_latitude IS NOT NULL AND l.float_longitude IS NOT NULL
       AND {where}
     GROUP BY 1
     ORDER BY n DESC, name
     LIMIT 60
"""


# ── The date range ───────────────────────────────────────────
#
# The same two names, the same parsing and the same inclusive end as Events
# and the Graph: a date range means one thing across the dashboard.
def _parse_date(value: str | None, field: str) -> datetime | None:
    if not value or not value.strip():
        return None
    try:
        stamp = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        raise _bad(f"{field} is not a date", "use YYYY-MM-DD, for example 2026-01-31")
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def _date_range(date_from: str, date_to: str) -> tuple[datetime | None, datetime | None]:
    start = _parse_date(date_from, "date_from")
    end = _parse_date(date_to, "date_to")
    if end is not None and end.hour == 0 and end.minute == 0 and end.second == 0:
        end = end + timedelta(days=1)
    if start and end and end <= start:
        raise _bad("the date range ends before it starts",
                   "swap the two dates, or clear one of them")
    return start, end


def _date_predicate(start: datetime | None, end: datetime | None) -> sql.Composable:
    tl = timeline("locations", "l")
    parts = [sql.SQL("TRUE")]
    if start:
        parts.append(sql.SQL("{tl} >= %(date_from)s").format(tl=tl))
    if end:
        parts.append(sql.SQL("{tl} < %(date_to)s").format(tl=tl))
    return sql.SQL(" AND ").join(parts)


def _bbox_predicate(bbox: str) -> tuple[sql.Composable, dict[str, Any]]:
    """`bbox=minLat,minLng,maxLat,maxLng`, the order Leaflet's toBBoxString
    is NOT in - the page sends the four numbers itself, and this is the one
    place that has to agree with it."""
    parts = _split(bbox)
    if not parts:
        return sql.SQL("TRUE"), {}
    if len(parts) != 4:
        raise _bad("bbox needs four numbers",
                   "bbox=minLat,minLng,maxLat,maxLng - for example 45,5,49,11")
    try:
        min_lat, min_lng, max_lat, max_lng = (float(p) for p in parts)
    except ValueError:
        raise _bad("bbox must be four numbers",
                   "bbox=minLat,minLng,maxLat,maxLng - for example 45,5,49,11")
    if min_lat > max_lat:
        min_lat, max_lat = max_lat, min_lat
    params = {"min_lat": min_lat, "max_lat": max_lat}
    frag = sql.SQL("l.float_latitude BETWEEN %(min_lat)s AND %(max_lat)s")
    if min_lng <= max_lng:
        params.update({"min_lng": min_lng, "max_lng": max_lng})
        frag = frag + sql.SQL(" AND l.float_longitude BETWEEN %(min_lng)s AND %(max_lng)s")
    else:
        # The box crosses the date line: two strips, not one impossible one.
        params.update({"min_lng": min_lng, "max_lng": max_lng})
        frag = frag + sql.SQL(" AND (l.float_longitude >= %(min_lng)s"
                              " OR l.float_longitude <= %(max_lng)s)")
    return frag, params


# ── The four filters, written down once ──────────────────────
#
# THE HEAT FIELD AND THE LIST BEHIND ONE OF ITS SPOTS HAVE TO BE NARROWED BY
# THE SAME THINGS, and this is the only reason that is guaranteed rather than
# remembered. A spot has no server-side identity - `/heat` answers with
# aggregated cells and the only key a spot has is the rounded pair of numbers
# the browser holds - so `/spot` re-derives the cell and re-applies the
# filters. Re-applying THREE of the four would list rows the spot was never
# counted from, and nothing on the screen would explain why the dialog says
# fourteen and the row beside the map says two.
#
# So both endpoints call this, and a fifth filter added to one of them
# arrives in the other by construction.


@dataclass
class HeatFilters:
    """What a heat count is narrowed by, and what the page has to be told
    about it: the SQL, its parameters, and the two resolutions that produce a
    sentence ("no place of that name", "that bucket, 4 members merged")."""
    where: sql.Composable
    params: dict[str, Any]
    scope: Any = None
    place: Any = None
    wanted: list[str] = field(default_factory=list)

    def head(self) -> sql.Composable:
        """`WITH ent AS (…), src AS (…)` when an entity narrows the count."""
        return self.scope.with_clause() if self.scope is not None else sql.SQL("")

    def typed(self) -> sql.Composable:
        """The same WHERE with the location-type checklist on top.

        Separate, because the checklist itself is answered from the count
        WITHOUT it: a list that shrinks as you untick things cannot be
        ticked again (see map_heat).
        """
        if not self.wanted:
            return self.where
        return sql.SQL("({w}) AND l.text_type = ANY(%(types)s)").format(w=self.where)


def _heat_filters(conn, ctx: Context, base: sql.Composable, params: dict[str, Any],
                  q: str, types: str, entity: str, entity_axis: str,
                  date_from: str, date_to: str) -> HeatFilters:
    """`base` narrowed by the place, the entity, the dates and the types.

    `base` is what the caller is already asking about - the viewport for the
    grid, one cell of it for the listing - and everything after it is the
    same four filters in the same order for both.
    """
    term = (q or "").strip()
    who = (entity or "").strip()
    which = _axis(entity_axis)
    kind, scope_axis = MAP_AXES[which]
    wanted = _split(types)
    start, end = _date_range(date_from, date_to)
    where = base
    params = dict(params)
    if start or end:
        where = sql.SQL("({w}) AND {d}").format(w=where, d=_date_predicate(start, end))
        params.update({"date_from": start, "date_to": end})

    # The place is resolved on the same connection as the count, so the names
    # in the answer and the squares in it come from one view of the archive
    # rather than two.
    place = _resolve_place(conn, ctx, term) if term else None
    if place is not None:
        frag, place_params = places.name_predicate(place.match, term, alias="l")
        # A place the archive does not know is `false`: an empty map with a
        # sentence, never the whole world under a search box that looks as if
        # it had done something.
        where = sql.SQL("({w}) AND {p}").format(w=where, p=frag)
        params.update(place_params)

    # THE ENTITY IS A SECOND AXIS, AND-ED WITH THE PLACE. The scope CTE is the
    # one every other view resolves an entity with, so a bucket name counts
    # every member here exactly as it does on the Map - and on
    # `entity_axis=type` the very same CTE holds the entities an event type
    # names, so the counting below does not know which was asked.
    scope = None
    if who:
        try:
            scope = resolve_scope(conn, ctx, kind, who, axis=scope_axis)
        except ScopeError as exc:
            raise _bad(str(exc), AXIS_HINTS[which])
        where = sql.SQL("({w}) AND {e}").format(w=where, e=scope.ent_predicate("l"))
        params.update(scope.params)

    # THE READER'S PERSPECTIVE, AND IT IS NEARLY FREE HERE. A location row
    # carries the id of the document that reported it, so "is that document
    # important enough for this reader" is the same EXISTS every other view
    # applies (app/sqlbuild.py: perspective_scope) - and it is TRUE when no
    # perspective is chosen, so this line costs an unfiltered count nothing.
    #
    # It goes in the shared WHERE deliberately: the grid, the type checklist
    # and the listing behind one spot all read it from here, so the field a
    # reader is looking at and the dialog they open out of it are narrowed by
    # the same judgement.
    where = sql.SQL("({w}) AND {p}").format(
        w=where, p=perspective_scope(ctx.perspective, "l"))
    params.update(perspective_params(ctx.perspective, ctx.min_importance))

    if wanted:
        params["types"] = wanted
    return HeatFilters(where=where, params=params, scope=scope, place=place, wanted=wanted)


@router.get("/heat")
def map_heat(ctx: ContextDep, db: Database = Depends(get_db),
             q: str = Query("", description="a city, a country or an address"),
             bbox: str = Query("", description="minLat,minLng,maxLat,maxLng"),
             types: str = Query("", description="location type names, comma separated"),
             # PLAIN DEFAULTS, NOT Query(...): api_export.py calls this
             # function directly, in Python, and a parameter whose default is
             # a Query object arrives there as that object rather than as a
             # string. FastAPI reads a scalar with a plain default as a query
             # parameter all the same; what is lost is the description, which
             # is in the docstring below instead.
             entity: str = "",
             entity_axis: str = "",
             date_from: str = "",
             date_to: str = ""):
    """The heat grid, narrowed by any of four things at once.

    `q`        a place - a city, a country or an address (see the header).
    `entity`   an entity or a bucket, AND-ed with the place.
    `entity_axis`  how `entity` is read: "object" (an entity or a bucket) or
               "type" (an EVENT TYPE - the addresses of every entity the
               events of that kind are about). MAP_AXES at the top of this
               file; the same two axes the Map's `axis` names, spelled
               `entity_axis` here because this endpoint has TWO search terms
               and only one of them has axes - `q` is a place either way.
    `date_from` / `date_to`  the location's own timeline column; the end date
               is inclusive, as it is everywhere else in the dashboard.
    `bbox`, `types`  the viewport and the location-type checklist.
    """
    base, base_params = _bbox_predicate(bbox)
    term = (q or "").strip()
    who = (entity or "").strip()
    which = _axis(entity_axis)
    start, end = _date_range(date_from, date_to)

    with db.read() as conn:
        narrow = _heat_filters(conn, ctx, base, base_params, q, types,
                               entity, entity_axis, date_from, date_to)
        scope, place, params = narrow.scope, narrow.place, narrow.params
        head = narrow.head()
        args = {**identity_params(ctx.project, ctx.language), **params, "cells": MAX_HEAT_CELLS + 1}
        rows = conn.execute(
            sql.SQL("{h}{s}").format(h=head, s=sql.SQL(_HEAT).format(
                p=sql.Literal(HEAT_PRECISION), idn=identity("l"), where=narrow.typed())),
            args).fetchall()
        # The checklist is what the map COULD show, so it ignores the type
        # filter: a list that shrinks as you tick things off cannot be
        # untick_ed again. It does NOT ignore the place, the entity or the
        # dates - the types of what a reader is looking at are the ones they
        # can tick.
        type_rows = conn.execute(
            sql.SQL("{h}{s}").format(h=head, s=sql.SQL(_HEAT_TYPES).format(
                idn=identity("l"), where=narrow.where)),
            {**identity_params(ctx.project, ctx.language), **params}).fetchall()

    capped = len(rows) > MAX_HEAT_CELLS
    rows = rows[:MAX_HEAT_CELLS]
    points = [[float(r["lat"]), float(r["lng"]), int(r["n"])] for r in rows]
    # `points` stays three numbers per row - it is what the heat layer is fed
    # with, and 20000 of them. `top` is the same squares for PEOPLE: the
    # busiest few, each with the name of the place, for the list beside the
    # map. Two shapes because they answer two questions, not one shape that
    # answers neither well.
    top = [{"lat": float(r["lat"]), "lng": float(r["lng"]), "count": int(r["n"]),
            "address": r["address"] or ""} for r in rows[:TOP_SQUARES]]
    return {
        "points": points,
        "top": top,
        "max": max((p[2] for p in points), default=0),
        "cells": len(points),
        "locations": sum(p[2] for p in points),
        "entities": max((int(r["entities"]) for r in rows), default=0),
        "bounds": _bounds([(p[0], p[1]) for p in points]),
        "types": [{"name": r["name"] or "", "count": int(r["n"])} for r in type_rows],
        "precision": HEAT_PRECISION,
        "capped": capped, "limit": MAX_HEAT_CELLS,
        "bbox": bbox, "q": term,
        "entity": who, "entity_axis": which,
        "date_from": start.isoformat() if start else None,
        "date_to": end.isoformat() if end else None,
        # WHAT THE ENTITY BOX BECAME, in the same shape every other view
        # reports it (app/scope.py): a bucket name says so, a name nothing is
        # called says "none", and the page can then say which of the two
        # emptied the map.
        "resolved": None if scope is None else dict(scope.resolved),
        # `place` is how the page tells "nothing here" from "no such place":
        # match "none" is a name the archive has never seen, and the two need
        # different sentences and different next steps.
        "place": None if place is None else {
            "match": place.match, "places": len(place.candidates),
            "names": _place_names(place)[:MAX_PLACE_NAMES],
        },
        "project": ctx.project, "language": ctx.language,
    }


# ── What is inside one spot ──────────────────────────────────
#
# A FIELD OF COLOUR CANNOT BE CLICKED THROUGH, AND THIS IS WHAT MAKES IT
# CLICKABLE. The heat layer is one canvas: it has no per-spot object, no id
# and nothing on the server that names a spot - `/heat` answers with
# aggregated cells and the page keys them by the rounded pair of numbers it
# was sent. So the cell is re-derived here from those two numbers, with the
# same `round(…, HEAT_PRECISION)` the grid groups by, and the same four
# filters the field was drawn with are applied again (_heat_filters).
#
# WHAT A ROW IS. One ENTITY - and a bucket as one row under its own name,
# the way the rest of the dashboard answers a bucket - ranked by how many
# locations of it are in the spot, most first. The SQL is in
# app/charts/places.py, which says why neither of the drilldown module's two
# statements is this shape.
#
# WHY IT PAGES RATHER THAN ANSWERING WHOLE. One spot of a small archive is
# two rows; one spot of a large archive over a city centre is thousands, and
# the dialog is the same dialog every drilldown uses - twenty rows, then
# more as the reader scrolls (static/js/drilldown.js).
MAX_DD_PAGE = 1000


@router.get("/spot")
def map_spot(ctx: ContextDep, db: Database = Depends(get_db),
             lat: str = Query(..., description="the spot's latitude, as the grid rounded it"),
             lng: str = Query(..., description="the spot's longitude, as the grid rounded it"),
             q: str = Query("", description="a city, a country or an address"),
             types: str = Query("", description="location type names, comma separated"),
             # PLAIN DEFAULTS for the four the heat grid also takes, so that
             # the two signatures read the same way round (map_heat says why
             # it has no Query(...) at all). The descriptions are below.
             entity: str = "",
             entity_axis: str = "",
             date_from: str = "",
             date_to: str = "",
             ddpage: int = 1):
    """The entities inside one spot of the heat grid, most results first.

    `lat` / `lng`  the spot, as the grid rounded it - the two numbers
               `/heat` sent for that cell. Anything else is a 400: a
               listing that quietly answered about the nearest cell would
               look right and be about somewhere else.
    `q`, `entity`, `entity_axis`, `types`, `date_from`, `date_to`
               the same four filters `/heat` takes, and they mean the same
               things (see map_heat). They are re-applied here because the
               spot was counted with them: without them the list would show
               rows the spot was never counted from.
    `ddpage`   1 is the first page; static/js/drilldown.js counts from
               there, and every drilldown in this dashboard pages the same
               way.

    `bbox` is deliberately NOT here. The viewport is what the grid was
    counted over and one of its cells is by construction inside it; a second
    box around one 110 m cell can only be a way of getting a different
    answer for the same spot.
    """
    page = max(1, min(int(ddpage or 1), MAX_DD_PAGE))
    offset = (page - 1) * spots.PAGE_SIZE
    try:
        at_lat = spots.cell(lat, HEAT_PRECISION, "lat")
        at_lng = spots.cell(lng, HEAT_PRECISION, "lng")
    except ValueError as exc:
        raise _bad(str(exc), "lat and lng are the two numbers the heat grid answered with")
    if not (-90 <= at_lat <= 90) or not (-180 <= at_lng <= 180):
        raise _bad("that is not a point on the world",
                   "lat is between -90 and 90, lng between -180 and 180")

    base = spots.cell_predicate("l", HEAT_PRECISION)
    with db.read() as conn:
        narrow = _heat_filters(conn, ctx, base, spots.cell_params(at_lat, at_lng, HEAT_PRECISION),
                               q, types, entity, entity_axis, date_from, date_to)
        # THE BUCKETS ARE READ WHOLE, ONCE. There are a handful of them per
        # project and the fold has to happen inside the statement - a paged
        # list folded afterwards would merge the members that landed on the
        # same page and leave the rest as separate rows (charts/places.py).
        members = spots.bucket_members(groupings(conn, ctx.project, "entity"))
        statement, bucket_params = spots.spot_statement(
            narrow.scope, narrow.typed(), members)
        # NO JIT FOR THIS ONE STATEMENT, AND THE NUMBER IS WHY.
        #
        # `processed_data.locations` is a hypertable of 560 chunks on the law
        # archive, so this statement's plan is an Append over all of them and
        # its estimated cost is far above `jit_above_cost` - and above the two
        # thresholds that turn on inlining and optimisation as well. Measured
        # there (1.17 M addresses in one project): 3,418 JIT
        # functions, 70 SECONDS of compilation for 4.8 seconds of work. The
        # same spot with JIT off: 7.3 s cold, and the busiest spot in the
        # archive - 72,712 addresses under a country-level geocode - 9.9 s
        # against 83.7 s.
        #
        # SET LOCAL, so it lasts exactly this transaction and the pooled
        # connection goes back the way it came (app/db.py: read() commits on
        # exit). It is not the whole of the problem - `/heat` pays 8.6 s
        # against 3.4 s for the same reason and nothing here changes that;
        # the systemic answer is this archive's own jit_above_cost, which is
        # a setting and not a query.
        conn.execute("SET LOCAL jit = off")
        # ONE ROW MORE THAN A PAGE, so "is there more" is known without
        # asking a second time - the envelope every drilldown here uses.
        rows = conn.execute(statement, {
            **identity_params(ctx.project, ctx.language), **narrow.params, **bucket_params,
            "dd_limit": spots.PAGE_SIZE + 1, "dd_offset": offset}).fetchall()

    more = len(rows) > spots.PAGE_SIZE
    shown = rows[:spots.PAGE_SIZE]
    # Both totals ride on the statement that fetched the rows: `groups` is
    # how many entities the spot holds and `total` in the dialog's counter,
    # `locations` how many addresses they stand for - which is the number the
    # field was painted from, and the one the sentence under the title says.
    # An empty page past the end carries neither, and 0 is then the truth.
    total = int(rows[0]["groups"]) if rows else 0
    located = int(rows[0]["locations"] or 0) if rows else 0
    return {
        "lat": float(at_lat), "lng": float(at_lng), "precision": HEAT_PRECISION,
        "ddpage": page, "page_size": spots.PAGE_SIZE, "more": more,
        "offset": offset, "total": total, "locations": located,
        "columns": spots.columns_json(),
        "row_links": spots.row_links(),
        "rows": [spots.shape_row(row) for row in shown],
        # The same two resolutions the grid reports, so the dialog can say
        # what narrowed it in the same words the caption does.
        "resolved": None if narrow.scope is None else dict(narrow.scope.resolved),
        "place": None if narrow.place is None else {
            "match": narrow.place.match, "places": len(narrow.place.candidates),
            "names": _place_names(narrow.place)[:MAX_PLACE_NAMES],
        },
        "types": narrow.wanted,
        "project": ctx.project, "language": ctx.language,
    }
