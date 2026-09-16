"""The Tables view: every row of one table, a page at a time.

    GET /api/tables/{tab}              the rows (or the groups) of one table
    GET /api/tables/source             one document and every row read out of it
    GET /api/tables/bucket/sources     the documents that hold a bucket's members
    GET /api/tables/pair               the connections between two entities
    GET /api/tables/place              the locations at one address or point

The tabs are the Diagrams' (charts.TAB_IDS) and the rows are a drilldown's
rows: the same columns, the same cells, the same three links
(charts/drilldown.py), so the page draws them with the same code and the
CSV holds the same words. What differs is the question - not "the rows
behind this bar" but "every row, newest first, called so" - and
charts/listing.py writes that question once per tab.

Every answer is JSON. A wrong tab, sort or filter is a 400 with {error,
hint}; a document, a bucket or a pair the archive does not have is a 404.
Every route reads the project, the language and the reader's perspective
filter off the request (ContextDep), and the filter narrows every table
except the one document row that is opened by its own key.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from psycopg import sql

from .. import charts as catalogue_module
from .. import sqlbuild
from ..charts import drilldown as dd
from ..charts import listing
from ..context import ContextDep
from ..db import Database, get_db
from ..scope import COLOUR_GROUP_KIND, GROUPING_KINDS, grouping_for, groupings
from .api_diagrams import MAX_PAGE, _utc
from .api_settings import KIND_INFO

router = APIRouter(prefix="/api/tables", tags=["tables"])

PAGE_SIZE = listing.PAGE_SIZE

# The order the kinds of one document are listed in: the things the
# document is about first, then what was said about them.
DOCUMENT_KINDS = ("entities", "connections", "locations", "events", "ratings", "attributes", "market")

# Which groupings a tab's bucket strip offers. Ratings have none: a rating's
# name and perspective are closed vocabularies nobody spells twice.
TAB_BUCKET_KINDS: dict[str, tuple[str, ...]] = {
    "sources": ("source_type",),
    "entities": ("entity", "entity_type"),
    "connections": (COLOUR_GROUP_KIND,),
    "locations": ("location", "location_type"),
    "events": ("event_type",),
    "attributes": ("attribute_type", "unit"),
    "market": ("market_topic",),
    "ratings": (),
}

# What a colour group is called beside the bucket kinds: the Colours page
# groups connection types, and KIND_INFO has no entry for it (api_settings
# says why).
CONNECTION_TYPE_LABEL = "Connection type"

# The Map names a bucket this way where an entity id would stand.
BUCKET_PREFIX = "bucket:"


def _bad(error: str, hint: str) -> HTTPException:
    return HTTPException(400, {"error": error, "hint": hint})


def _missing(error: str, hint: str) -> HTTPException:
    return HTTPException(404, {"error": error, "hint": hint})


def _flag(value: str) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


def _kind_label(kind: str) -> str:
    if kind == COLOUR_GROUP_KIND:
        return CONNECTION_TYPE_LABEL
    return KIND_INFO.get(kind, {}).get("label", kind)


def _begin(conn) -> None:
    """What every statement of this router runs under: no JIT (app/db.py
    says why, and SET LOCAL keeps it to this transaction), and UTC, so the
    dates the rows carry are the dates the charts drew them on."""
    conn.execute("SET LOCAL jit = off")
    _utc(conn)


def _filters(q: str, type_: str, sort: str, task: str, source: str,
             exact: str, name: str, entity_id: str) -> listing.Filters:
    filters = listing.Filters(q=(q or "").strip(), type=(type_ or "").strip(),
                              sort=(sort or "newest").strip().lower() or "newest",
                              task=(task or "").strip(), source=(source or "").strip(),
                              exact=_flag(exact), name=(name or "").strip(),
                              entity_id=(entity_id or "").strip())
    try:
        filters.check()
    except listing.ListingError as exc:
        raise _bad(str(exc), exc.hint)
    return filters


def _tab_or_400(tab: str) -> None:
    try:
        listing.shape_of(tab)
    except listing.ListingError as exc:
        raise _bad(str(exc), exc.hint)


def _page(conn, ctx, tab: str, filters: listing.Filters, ddpage: int, *,
          count: bool = True, perspective: bool = True, extra=(), extra_params=None) -> dict[str, Any]:
    """One page, in the envelope every row page of this router answers with.

    One row more than a page is fetched, so "is there more" is known
    without a second statement. `total` is the count of the first page and
    null on the later ones - the page keeps the first - because the count
    is a second statement over the same predicates, and it is paid once.
    """
    offset = (ddpage - 1) * PAGE_SIZE
    found = listing.page(conn, ctx, tab, filters, limit=PAGE_SIZE + 1, offset=offset,
                         count=count and ddpage == 1, perspective=perspective,
                         extra=extra, extra_params=extra_params)
    more = len(found.rows) > PAGE_SIZE
    del found.rows[PAGE_SIZE:]
    if found.grouped:
        columns, links = listing.group_columns_json(), listing.group_row_links()
    else:
        columns, links = dd.columns_json(found.table), dd.row_links(found.table)
    shaped = found.shaped()
    return {
        "tab": tab, "q": filters.q, "type": filters.type, "sort": filters.sort,
        "exact": filters.exact,
        "ddpage": ddpage, "page_size": PAGE_SIZE, "more": more, "offset": offset,
        "total": found.total,
        "columns": columns, "row_links": links,
        # What the first column is called: a group row counts rows and
        # names no document.
        "source_label": "Rows" if found.grouped else "Source",
        # THE FIRST ROW'S NAME AND TYPE, for the search fields' placeholders:
        # an example from this archive, in this tab, rather than one from a
        # world the reader has never seen.
        "example": listing.example(found) if ddpage == 1 else None,
        "rows": shaped,
    }


# ── The rows of one table ────────────────────────────────────

def _buckets(conn, ctx, tab: str, q: str, type_: str) -> list[dict[str, Any]]:
    """The groupings of this tab's kinds that the search touches: the term
    against the bucket's name or any member's, and - for entity buckets,
    whose members carry one - the type against the members' types."""
    needle = (q or "").strip().lower()
    wanted_type = (type_ or "").strip().lower()
    out: list[dict[str, Any]] = []
    for kind in TAB_BUCKET_KINDS.get(tab, ()):
        for group in groupings(conn, ctx.project, kind):
            members = [{"name": m.get("name") or "", "type": m.get("type") or ""} for m in group.members]
            if needle and needle not in (group.name or "").lower() and not any(
                    needle in m["name"].lower() for m in members):
                continue
            if wanted_type and kind == "entity" and not any(
                    wanted_type in m["type"].lower() for m in members):
                continue
            out.append({"kind": kind, "kind_label": _kind_label(kind), "name": group.name,
                        "members": members, "member_count": len(members)})
    return out


@router.get("/source")
def source(ctx: ContextDep, db: Database = Depends(get_db),
           task: str = Query(..., description="the task the document was read in"),
           id: str = Query(..., description="the document's source id inside that task")):
    """One document, and every row the archive read out of it, one first
    page per kind. The document row itself is opened by its key and not
    narrowed by the reader's filter; the kinds under it are, like every
    other table."""
    task, id = (task or "").strip(), (id or "").strip()
    if not task or not id:
        raise _bad("a document is named by task and id together", "send task= and id=")
    key = listing.Filters(task=task, source=id)
    with db.read() as conn:
        _begin(conn)
        found = listing.page(conn, ctx, "sources", key, limit=1, offset=0, count=False,
                             perspective=False)
        if not found.rows:
            raise _missing("the archive has no such document",
                           "task and id are the pair a row's source link carries")
        document = found.shaped()[0]
        # The rows of a document arrive with it, so each kind is asked for
        # the document's day (listing.Filters.added) and not for every chunk.
        key = replace(key, added=found.rows[0].get("dd_added"))
        kinds = []
        for tab in DOCUMENT_KINDS:
            page = _page(conn, ctx, tab, key, 1)
            kinds.append({"tab": tab, "title": catalogue_module.TAB_TITLES[tab],
                          "total": page["total"], "more": page["more"],
                          "columns": page["columns"], "row_links": page["row_links"],
                          "rows": page["rows"]})
    return {"source": document, "kinds": kinds}


@router.get("/bucket/sources")
def bucket_sources(ctx: ContextDep, db: Database = Depends(get_db),
                   kind: str = Query(..., description="which kind of grouping"),
                   name: str = Query(..., description="the bucket's (or colour group's) name"),
                   ddpage: int = Query(1, ge=1, le=MAX_PAGE)):
    """The documents that hold at least one member of a bucket, newest
    first - the answer to "which documents is this bucket about"."""
    kind = (kind or "").strip()
    if kind not in GROUPING_KINDS:
        raise _bad(f"there is no grouping kind {kind!r}", "one of " + ", ".join(GROUPING_KINDS))
    with db.read() as conn:
        _begin(conn)
        # By name or by member, which is the rule everywhere (app/scope.py):
        # a bucket wins over a literal match.
        group = grouping_for(conn, ctx.project, kind, name)
        if group is None:
            raise _missing(f"there is no {_kind_label(kind).lower()} bucket called {name!r}",
                           "the name as the Buckets page (or the Colours page) spells it")
        info = KIND_INFO.get(kind, {})
        holds, params = listing.bucket_holders(group, info.get("table", ""), info.get("column", ""))
        page = _page(conn, ctx, "sources", listing.Filters(), ddpage,
                     extra=(holds,), extra_params=params)
    page["bucket"] = group.as_dict()
    return page


# ONE THING UNDER SEVERAL IDS. The extraction hands the same company several
# ids over time, and the Map folds every id that carries one name into one
# pin and one line (routers/api_map.py, _same_thing: the name, lower-cased,
# and nothing else). A pin's rows are therefore stored under all of those
# ids, and a drilldown that asked for the one id the pin was drawn as would
# answer "no rows" to a line the reader is looking at. So an id from the map
# becomes every id of its name here, with the same rule.
_IDS_OF_NAME = """
    SELECT DISTINCT e.text_entity_id AS id
      FROM processed_data.entities e
     WHERE {idn} AND lower(e.text_name) IN (
               SELECT DISTINCT lower(n.text_name) FROM processed_data.entities n
                WHERE {idn_n} AND n.text_entity_id = %(id)s AND n.text_name <> '')
       AND e.text_entity_id IS NOT NULL AND e.text_entity_id <> ''
"""


def same_ids(conn, ctx, entity_id: str) -> list[str]:
    """Every id that carries the name(s) this id carries - the ids the Map
    folds into one thing. The id itself is always in the list, so an id
    with no name of its own stays itself."""
    rows = conn.execute(
        sql.SQL(_IDS_OF_NAME).format(idn=sqlbuild.identity("e"), idn_n=sqlbuild.identity("n")),
        {**sqlbuild.identity_params(ctx.project, ctx.language), "id": entity_id}).fetchall()
    ids = [r["id"] for r in rows]
    return ids if entity_id in ids else [entity_id, *ids]


def _end(conn, ctx, value: str, label: str) -> tuple[list[str], bool]:
    """One end of a pair: every id of the entity's name, or the member ids
    of the entity bucket `bucket:<name>` names - the Map's own spelling."""
    value = (value or "").strip()
    if not value:
        raise _bad(f"{label} is missing", "a and b are two entity ids, or bucket:<name>")
    if not value.startswith(BUCKET_PREFIX):
        return same_ids(conn, ctx, value), True
    group = grouping_for(conn, ctx.project, "entity", value[len(BUCKET_PREFIX):])
    if group is None:
        raise _missing(f"there is no entity bucket called {value[len(BUCKET_PREFIX):]!r}",
                       "the name as the Buckets page spells it")
    return listing.bucket_entity_ids(conn, ctx, group), True


@router.get("/pair")
def pair(ctx: ContextDep, db: Database = Depends(get_db),
         a: str = Query(..., description="the reference entity id, or bucket:<name>"),
         b: str = Query(..., description="the target entity id, or bucket:<name>"),
         role: str = Query("", description="one role word, to narrow the rows to"),
         ddpage: int = Query(1, ge=1, le=MAX_PAGE)):
    """The connections between two entities, each stored row once, read
    from `a`'s side: every row says "a is a <role> of b"."""
    role = (role or "").strip()
    with db.read() as conn:
        _begin(conn)
        a_value, a_many = _end(conn, ctx, a, "a")
        b_value, b_many = _end(conn, ctx, b, "b")
        params: dict[str, Any] = {"a": a_value, "b": b_value}
        if role:
            params["role"] = role
        page = _page(conn, ctx, "connections", listing.Filters(), ddpage,
                     extra=listing.pair_where(a_many, b_many, bool(role)), extra_params=params)
    page.update({"a": (a or "").strip(), "b": (b or "").strip(), "role": role})
    return page


@router.get("/place")
def place(ctx: ContextDep, db: Database = Depends(get_db),
          entity: str = Query("", description="narrow to one entity id"),
          address: str = Query("", description="the address, as the row carries it"),
          lat: float | None = Query(None), lng: float | None = Query(None),
          ddpage: int = Query(1, ge=1, le=MAX_PAGE)):
    """The location rows at one place: by address, or by the point the
    map drew."""
    entity, address = (entity or "").strip(), (address or "").strip()
    params: dict[str, Any] = {}
    if address:
        params["address"] = address
    elif lat is not None and lng is not None:
        if not (-90 <= lat <= 90) or not (-180 <= lng <= 180):
            raise _bad("that is not a point on the world",
                       "lat is between -90 and 90, lng between -180 and 180")
        params.update({"lat": lat, "lng": lng})
    else:
        raise _bad("a place is an address or a point", "send address=, or lat= and lng=")
    with db.read() as conn:
        _begin(conn)
        if entity:
            params["entity"] = same_ids(conn, ctx, entity)
        page = _page(conn, ctx, "locations", listing.Filters(), ddpage,
                     extra=listing.place_where(address=bool(address), entity=bool(entity)),
                     extra_params=params)
    page.update({"entity": entity, "address": address, "lat": lat, "lng": lng})
    return page


@router.get("/{tab}")
def rows(tab: str, ctx: ContextDep, db: Database = Depends(get_db),
         q: str = Query("", description="a substring of the name"),
         type: str = Query("", description="a substring of the type"),
         sort: str = Query("newest", description="newest, oldest, most or fewest"),
         buckets: str = Query("", description="1 to add the buckets the search touches"),
         ddpage: int = Query(1, ge=1, le=MAX_PAGE),
         task: str = Query(""), source: str = Query(""),
         exact: str = Query("", description="1 to match name and type exactly"),
         name: str = Query(""), entity_id: str = Query("")):
    """The rows of one table, or - sorted by most/fewest - its groups."""
    _tab_or_400(tab)
    filters = _filters(q, type, sort, task, source, exact, name, entity_id)
    with db.read() as conn:
        _begin(conn)
        if filters.task and tab != "sources":
            # The document's day, so its rows are read from that chunk
            # (listing.Filters.added). A document the archive does not
            # have answers with no rows, as it would without the bound.
            filters = replace(filters, added=listing.document_added(
                conn, ctx, filters.task, filters.source))
        page = _page(conn, ctx, tab, filters, ddpage)
        if _flag(buckets) and ddpage == 1:
            page["buckets"] = _buckets(conn, ctx, tab, filters.q, filters.type)
    return page
