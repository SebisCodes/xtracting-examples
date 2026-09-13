"""The connection graph: what is next to one entity, one ring at a time.

    GET /api/graph/neighbours?q=Apple            start from a search term
    GET /api/graph/neighbours?id=ent:apple-inc   expand one node
    GET /api/graph/legend                        the colour groups

A DATE RANGE NARROWS THE EDGES, NOT THE CENTRE. `date_from` / `date_to` are
compared against the connection's own timeline column (date_commissioned,
app/sqlbuild.py), so "who was Apple connected to last year" is answerable and
the centre of the graph stays the entity that was searched for even when the
range leaves it with nothing. When a range empties a ring the answer says how
many neighbours there would have been without it (`without_dates`), because
"nothing is connected to it" and "nothing is connected to it in these three
months" are two different facts and a person acts on them differently.

ONE REQUEST PER RING. The graph is drawn by progressive disclosure - the term
gives a centre and its first ring, clicking a node asks again for that node's
own neighbours - so every answer here is one hop. Nothing here refuses a
neighbour that is already drawn: whether an edge is new, a cross-link between
two nodes that are already on the canvas, or a repetition of an edge that
exists (Apple → Microsoft → Apple) is a question about the PICTURE, and the
picture lives in the browser (static/js/graph.js).

`id` AND BUCKETS. Expanding by id first asks what that id is called, then
resolves the NAME through app/scope.py - so a node that is a bucket member
expands to the whole bucket, exactly as a search for the same name would.
Without that, "Apple" the search and "Apple" the clicked node would produce
two different graphs and nobody could say why. The centre's own ids ride
along in the answer (`entity.ids`), which is what lets the browser recognise
a neighbour that IS the centre and draw no edge back to it.

Ids, not names, are the identity of a node: the extraction hands the same
`text_entity_id` to the same thing every time it recognises it, and a
project's translations share those ids - which is what makes the German view
of a graph the same graph. The name is what an id is CALLED in the requested
language.

Colours come from app/colours.py, never from a regex at runtime, so an edge
has the same colour here as the same relationship on the map.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from psycopg import sql

from .. import config as app_config
from ..colours import FALLBACK_COLOUR, FALLBACK_KEY, ColourResolver, get_resolver
from ..context import ContextDep
from ..db import Database, get_db
from ..scope import ScopeError, find_bucket, resolve_scope
from ..sqlbuild import (identity, identity_params, perspective_params, perspective_scope,
                        timeline)

router = APIRouter(prefix="/api/graph", tags=["graph"])

# How many ids one centre may stand for. A bucket with more members than this
# is not a graph centre, it is a list.
MAX_CENTRE_IDS = 500


def _bad(error: str, hint: str) -> HTTPException:
    return HTTPException(400, {"error": error, "hint": hint})


def _config(request: Request):
    cfg = getattr(request.app.state, "cfg", None)
    return cfg if cfg is not None else app_config.load()


_SCOPE_IDS = "SELECT DISTINCT id FROM ent WHERE id IS NOT NULL AND id <> '' ORDER BY id LIMIT %(id_cap)s"

# What one id is called. Most frequent spelling first, ties to the alphabet,
# so a node does not rename itself between two clicks.
_NAME_OF = """
    SELECT e.text_name AS name, e.text_type AS type, count(*) AS n
      FROM processed_data.entities e
     WHERE {idn} AND e.text_entity_id = %(id)s AND e.text_name <> ''
     GROUP BY 1, 2
     ORDER BY n DESC, name
     LIMIT 1
"""

# Every connection with one end in the centre, seen FROM the centre.
#
# WHICH OF THE TWO TYPE NAMES IS THE EDGE'S. `text_type_parent_to_child` names
# the PARENT's role and `text_type_child_to_parent` the CHILD's: the archive
# holds (Foxconn, Apple, "Supplier", "Customer") for "Foxconn supplies Apple".
# The label on an edge has to describe the NEIGHBOUR - a ring around Apple
# reads "Foxconn - Supplier", not "Foxconn - Customer" - so `type_out` is the
# far end's role and `type_back` the centre's. Getting this the wrong way
# round is invisible on Competitor/Competitor and wrong on every other pair.
#
# A row with both ends in the centre (two members of one bucket connected to
# each other) is dropped - it would be a loop on the root node, and a loop
# says nothing a person can act on.
#
# `{dates}` is the date range from the toolbar, on the connection's own
# timeline column. It is inside the edge CTE and not outside it, so a range
# that matches nothing costs one index scan rather than a scan of every
# connection the centre has.
#
# `{persp}` - the reader's perspective and minimum importance - sits in
# exactly the same place, in BOTH halves of the UNION, and for exactly the
# same reason. It is a predicate on the connection's own document, so it
# belongs where the connection rows are still being chosen; applied after the
# UNION it would have made the planner materialise every edge the centre has
# before throwing most of them away, which is what the paragraph above this
# one is about. Both halves, because an edge may be found from either end and
# a filter on one of them would make the ring depend on which way round the
# extraction happened to write the pair.
#
# Both id columns are indexed (idx_connections_parent / idx_connections_child),
# so each half of the UNION is an index scan.
_NEIGHBOURS = """
    WITH edge AS (
        SELECT c.text_fk_child_entity_id   AS other,
               c.text_type_child_to_parent AS type_out,
               c.text_type_parent_to_child AS type_back,
               c.date_commissioned         AS seen
          FROM processed_data.connections c
         WHERE {idn} AND {dates} AND {persp}
           AND c.text_fk_parent_entity_id = ANY(%(centre_ids)s)
           AND c.text_fk_child_entity_id IS NOT NULL AND c.text_fk_child_entity_id <> ''
           AND NOT (c.text_fk_child_entity_id = ANY(%(centre_ids)s))
        UNION ALL
        SELECT c.text_fk_parent_entity_id,
               c.text_type_parent_to_child,
               c.text_type_child_to_parent,
               c.date_commissioned
          FROM processed_data.connections c
         WHERE {idn} AND {dates} AND {persp}
           AND c.text_fk_child_entity_id = ANY(%(centre_ids)s)
           AND c.text_fk_parent_entity_id IS NOT NULL AND c.text_fk_parent_entity_id <> ''
           AND NOT (c.text_fk_parent_entity_id = ANY(%(centre_ids)s))
    ),
    -- THE NAMES OF THE NEIGHBOURS, IN ONE PASS.
    --
    -- This was a LEFT JOIN LATERAL that looked one id up per edge row, and
    -- it cost 2.5-3.6 SECONDS on a 36-row preseed while every other query
    -- on the same table answered in single-digit milliseconds. The reason
    -- is the hypertable: `entities` is 36 chunks, and a lateral is
    -- re-planned and re-scanned per outer row, so a handful of edges paid
    -- for a few hundred chunk scans. Every click on the graph went through
    -- it, which made "click a node to draw its own connections" a
    -- click-and-wait.
    --
    -- One DISTINCT ON over the ids the edges actually name does the same
    -- job in a single scan (measured: 2503 ms -> 6 ms on the same data).
    -- The rule: the most frequent spelling of an id
    -- wins, ties go to the alphabet, so a node does not rename itself
    -- between two clicks.
    named AS (
        SELECT DISTINCT ON (x.text_entity_id)
               x.text_entity_id AS id, x.text_name AS name, x.text_type AS type
          FROM processed_data.entities x
         WHERE {idn_x} AND x.text_name <> ''
           AND x.text_entity_id IN (SELECT other FROM edge)
         GROUP BY x.text_entity_id, x.text_name, x.text_type
         ORDER BY x.text_entity_id, count(*) DESC, x.text_name
    )
    SELECT e.other, e.type_out, e.type_back,
           count(*) AS n, min(e.seen) AS first_seen, max(e.seen) AS last_seen,
           max(named.name) AS name, max(named.type) AS type
      FROM edge e
      LEFT JOIN named ON named.id = e.other
     GROUP BY e.other, e.type_out, e.type_back
     ORDER BY n DESC, e.other
"""


# ── The date range ───────────────────────────────────────────
#
# The same two parameter names, the same parsing and the same inclusive end
# as the Events view (app/routers/api_events.py): a date range means one
# thing across the dashboard, and "to 31 January" is the whole 31st
# everywhere or it is a trap somewhere.
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
        # An end date is inclusive: "to 31 January" means the whole 31st.
        end = end + timedelta(days=1)
    if start and end and end <= start:
        raise _bad("the date range ends before it starts",
                   "swap the two dates, or clear one of them")
    return start, end


def _date_predicate(start: datetime | None, end: datetime | None) -> sql.Composable:
    tl = timeline("connections", "c")
    parts = [sql.SQL("TRUE")]
    if start:
        parts.append(sql.SQL("{tl} >= %(date_from)s").format(tl=tl))
    if end:
        parts.append(sql.SQL("{tl} < %(date_to)s").format(tl=tl))
    return sql.SQL(" AND ").join(parts)


def _name_of(conn, ctx, entity_id: str) -> dict[str, str] | None:
    row = conn.execute(sql.SQL(_NAME_OF).format(idn=identity("e")),
                       {**identity_params(ctx.project, ctx.language), "id": entity_id}).fetchone()
    return None if not row else {"name": row["name"], "type": row["type"] or ""}


def _scope_ids(conn, scope) -> list[str]:
    """The entity ids a resolved scope covers - the centre of the graph."""
    rows = conn.execute(
        sql.SQL("{w}{s}").format(w=scope.with_clause(), s=sql.SQL(_SCOPE_IDS)),
        {**scope.params, "id_cap": MAX_CENTRE_IDS}).fetchall()
    return [r["id"] for r in rows]


def _group_neighbours(rows: list[dict[str, Any]], resolver: ColourResolver,
                      limit: int) -> tuple[list[dict[str, Any]], bool]:
    """The per-(neighbour, type) rows folded into one entry per neighbour.

    Sorted by how much the archive says about the pair, so a cap keeps the
    connections a person came for and drops the single mentions."""
    nodes: dict[str, dict[str, Any]] = {}
    for r in rows:
        other = r["other"]
        node = nodes.get(other)
        if node is None:
            node = nodes[other] = {"id": other, "name": r["name"] or other,
                                   "type": r["type"] or "", "count": 0, "types": [],
                                   "first": None, "last": None}
        name = (r["type_out"] or r["type_back"] or "").strip()
        colour = resolver.resolve(name)
        node["types"].append({
            "name": name, "reverse": (r["type_back"] or "").strip(), "count": int(r["n"]),
            "group": colour.group_key, "group_name": colour.group.name,
            "colour": colour.colour, "text_colour": colour.group.text_colour,
        })
        node["count"] += int(r["n"])
        if r["first_seen"] and (node["first"] is None or r["first_seen"] < node["first"]):
            node["first"] = r["first_seen"]
        if r["last_seen"] and (node["last"] is None or r["last_seen"] > node["last"]):
            node["last"] = r["last_seen"]

    ordered = sorted(nodes.values(), key=lambda n: (-n["count"], n["name"].lower(), n["id"]))
    capped = len(ordered) > limit
    for node in ordered:
        # A tie between a grouped type and an unassigned one goes to the
        # grouped one: "Partner" says more about a pair than the fallback
        # colour of a type nobody has classified yet.
        node["types"].sort(key=lambda t: (-t["count"], t["group"] == FALLBACK_KEY, t["name"]))
        # The dominant type gives the node its edge colour; the ring layout
        # groups by it, so neighbours of one kind end up side by side.
        head = node["types"][0] if node["types"] else None
        node["group"] = head["group"] if head else "other"
        node["group_name"] = head["group_name"] if head else "Other"
        node["colour"] = head["colour"] if head else FALLBACK_COLOUR
        node["first"] = node["first"].isoformat() if node["first"] else None
        node["last"] = node["last"].isoformat() if node["last"] else None
    return ordered[:limit], capped


def _legend_for(resolver: ColourResolver, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The groups this answer actually uses, with counts. An answer with no
    connections still gets the whole scale, so the legend is never empty and
    the colours mean the same thing before and after a search."""
    used = {n["group"] for n in items}
    legend = [g for g in resolver.legend() if g["key"] in used] or resolver.legend()
    for entry in legend:
        entry["count"] = sum(1 for n in items if n["group"] == entry["key"])
    return legend


def _empty(term: str, node_id: str, resolved: dict[str, Any], resolver: ColourResolver,
           ctx, limit: int, dates: dict[str, Any]) -> dict[str, Any]:
    return {"q": term, "id": node_id, "resolved": resolved, "entity": None,
            "neighbours": [], "legend": resolver.legend(), "limit": limit, "capped": False,
            **dates, "without_dates": 0,
            "project": ctx.project, "language": ctx.language}


@router.get("/neighbours")
def neighbours(ctx: ContextDep, request: Request, db: Database = Depends(get_db),
               q: str = Query("", description="an entity or a bucket name"),
               id: str = Query("", description="the entity id of a node being expanded"),
               # PLAIN DEFAULTS, NOT Query(...): api_export.py calls this
               # function directly, in Python, and a parameter whose default
               # is a Query object arrives there as that object rather than
               # as a string. FastAPI reads a scalar with a plain default as
               # a query parameter all the same; what is lost is the
               # description, which is in the docstring below instead.
               date_from: str = "",
               date_to: str = ""):
    """One ring around one centre, narrowed by a date range.

    `q` / `id`  the centre: a term to resolve, or the id of a node being
                expanded (see the header).
    `date_from` / `date_to`  the connection's own timeline column
                (date_commissioned); the end date is inclusive, as it is
                everywhere else in the dashboard. When a range leaves the
                ring empty, `without_dates` counts what it left out.
    """
    cfg = _config(request)
    term = (q or "").strip()
    node_id = (id or "").strip()
    if not term and not node_id:
        raise _bad("nothing to look up", "pass q=<entity or bucket> or id=<entity id>")
    resolver = get_resolver()
    start, end = _date_range(date_from, date_to)
    dates = {"date_from": start.isoformat() if start else None,
             "date_to": end.isoformat() if end else None}
    when = _date_predicate(start, end)
    always = _date_predicate(None, None)

    with db.read() as conn:
        if node_id:
            known = _name_of(conn, ctx, node_id)
            if known is None:
                raise HTTPException(404, {
                    "error": f"no entity with id {node_id!r} in this project and language",
                    "hint": "expand a node that is on the graph, or search by name"})
            bucket = find_bucket(conn, ctx.project, known["name"], known["type"])
            if bucket:
                scope = resolve_scope(conn, ctx, "entity", known["name"])
                centre_ids = _scope_ids(conn, scope) or [node_id]
                resolved = dict(scope.resolved)
                centre = {"id": node_id, "name": bucket["name"], "type": known["type"],
                          "kind": "bucket", "members": bucket["members"], "ids": centre_ids}
            else:
                centre_ids = [node_id]
                resolved = {"kind": "entity", "q": known["name"], "label": known["name"],
                            "match": "id", "names": [known["name"]], "bucket": None,
                            "entities": 1, "sources": None}
                centre = {"id": node_id, "name": known["name"], "type": known["type"],
                          "kind": "entity", "members": [], "ids": centre_ids}
        else:
            try:
                scope = resolve_scope(conn, ctx, "entity", term)
            except ScopeError as exc:
                raise _bad(str(exc), "search for an entity or a bucket name")
            resolved = dict(scope.resolved)
            centre_ids = _scope_ids(conn, scope)
            if not centre_ids:
                return _empty(term, node_id, resolved, resolver, ctx,
                              cfg.graph_max_neighbours, dates)
            bucket = resolved.get("bucket")
            # The node the ring is drawn around: a bucket is its own name, a
            # plain search is the entity it found (the first id, named).
            head = _name_of(conn, ctx, centre_ids[0]) or {"name": term, "type": ""}
            centre = {
                "id": (f"bucket:{bucket['id']}" if bucket else centre_ids[0]),
                "name": bucket["name"] if bucket else (head["name"] if len(centre_ids) == 1
                                                       else resolved.get("label") or term),
                "type": "" if bucket else head["type"],
                "kind": "bucket" if bucket else "entity",
                "members": bucket["members"] if bucket else [],
                "ids": centre_ids,
            }

        args = {**identity_params(ctx.project, ctx.language),
                **perspective_params(ctx.perspective, ctx.min_importance),
                "centre_ids": centre_ids, "date_from": start, "date_to": end}
        # THE CENTRE IS NOT FILTERED, THE EDGES ARE. A perspective narrows
        # what the archive says about a thing, never which thing was asked
        # about: a search for an entity whose own document falls below the
        # threshold must still find the entity and say the ring is empty,
        # rather than 404 as if the name had never been in the archive.
        persp = perspective_scope(ctx.perspective, "c")
        rows = [dict(r) for r in conn.execute(
            sql.SQL(_NEIGHBOURS).format(idn=identity("c"), idn_x=identity("x"),
                                        dates=when, persp=persp),
            args).fetchall()]
        # WHY THE RING IS EMPTY IS TWO DIFFERENT FACTS. "Nothing is connected
        # to it" is about the archive; "nothing in these three months" is
        # about the toolbar, and the second one is undone by clearing a
        # field. The second query runs only when a range is set AND it left
        # nothing, so the common path still costs one statement.
        without = 0
        if not rows and (start or end):
            without = len({r["other"] for r in conn.execute(
                sql.SQL(_NEIGHBOURS).format(idn=identity("c"), idn_x=identity("x"),
                                            dates=always, persp=persp),
                args).fetchall()})

    items, capped = _group_neighbours(rows, resolver, cfg.graph_max_neighbours)
    return {
        "q": term, "id": node_id, "resolved": resolved, "entity": centre,
        "neighbours": items, "legend": _legend_for(resolver, items),
        "limit": cfg.graph_max_neighbours, "capped": capped,
        **dates, "without_dates": without,
        "project": ctx.project, "language": ctx.language,
    }


@router.get("/legend")
def legend():
    """The colour groups, for a graph nobody has searched in yet.

    The settings API owns colour groups (/api/colour-groups); this is the same
    list from the same resolver, so the legend is on the page before the first
    search and cannot disagree with the edges afterwards."""
    return {"groups": get_resolver().legend()}
