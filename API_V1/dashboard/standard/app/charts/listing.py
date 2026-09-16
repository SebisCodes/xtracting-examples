"""The rows of one table, as the Tables view lists them.

A drilldown lists the rows behind one point of one chart (drilldown.py). The
Tables view asks the same table a plainer question - every row, newest
first, narrowed by a name and a type - and it has no chart to take its FROM
and WHERE from. So this module writes that FROM and WHERE itself, once per
tab, and hands the page to the SAME cut-then-look-up shape every listing
uses (drilldown.page_rows) and the SAME row shaper (drilldown.shape_row),
so a row of the Tables view is a row of a drilldown to the reader and to
the CSV.

Statements only. Nothing here opens a connection except `page()`, and that
runs exactly the statements the builders above it render, so a test can
read every statement without a database.

WHAT A ROW IS CALLED, PER TAB. Every tab has a NAME and a TYPE expression
(SHAPES), and they are what `q` and `type` search, what "most"/"fewest"
group by, and what an exact match compares. Two tabs are ENTITY-NAMED: a
market insight and a connection are about an entity and carry its id, not
its name, so their name is the entity's, looked up once per page
(ENTITY_NAMES_SQL) rather than joined per row.

A CONNECTION IS LISTED FROM BOTH ENDS. One stored row is one tie and two
facts - "Foxconn is a Supplier of Apple Inc." and "Apple Inc. is a Customer
of Foxconn" - and a table that showed only the end the extraction wrote
first would say the archive knows of no customers. drilldown.sides() is the
doubling, and `side.role` is the type of the row as read from its end.
"""

from __future__ import annotations

from datetime import datetime

from dataclasses import dataclass, field
from typing import Any

from psycopg import sql

from .. import sqlbuild
from ..textclean import plain
from ..scope import Grouping, bucket_terms, member_match, value_match
from . import TAB_IDS
from .drilldown import (Column, MAX_KEY_LENGTH, PAGE_SIZE, csv_columns, csv_row, page_rows,
                        readable_title, side_columns, sides, shape_row, _date, _part)

__all__ = [
    "PAGE_SIZE", "MAX_KEY_LENGTH", "SORTS", "GROUPED_SORTS", "ListingError",
    "Filters", "Shape", "SHAPES", "Listing", "table_of", "shape_of",
    "where", "rows_statement", "count_statement", "groups_statement",
    "ENTITY_NAMES_SQL", "GROUP_COLUMNS", "group_columns_json", "group_row_links",
    "group_csv_columns", "shape_group", "csv_group", "page",
    "bucket_holders", "bucket_entity_ids", "pair_where", "place_where", "end_predicate",
    "entity_names",
]

# The four orders a table can be read in. The first two list rows, the
# last two list GROUPS of them - one row per name and type, biggest first
# or smallest first - because "which names are there, and how often" is
# the question a table of ten thousand rows is opened to answer.
SORTS = ("newest", "oldest", "most", "fewest")
GROUPED_SORTS = ("most", "fewest")

# The half of the lat/lng match that is tolerance. A coordinate comes back
# from the page rounded to four decimals (drilldown.shape_row writes it so)
# and the archive holds more; half of one unit in the fifth decimal is what
# lets the rounded value find the stored one and nothing beside it.
COORDINATE_TOLERANCE = 0.000005


class ListingError(ValueError):
    """A tab, a sort or a filter the Tables view does not have. Carries a
    hint, so the API can answer {error, hint} like everything else."""

    def __init__(self, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.hint = hint


@dataclass(frozen=True)
class Shape:
    """How one tab names and types its rows.

    `name` and `type` are expressions over the alias `t` (and `side`, for
    connections). `entity_fk` is set on the entity-named tabs: the
    expression that carries the entity id, which the name is looked up by.
    """
    table: str
    name: sql.Composable
    type: sql.Composable
    entity_fk: sql.Composable | None = None

    @property
    def entity_named(self) -> bool:
        return self.entity_fk is not None

    @property
    def key(self) -> sql.Composable:
        """What a group is keyed on: the entity id where there is one, the
        name everywhere else."""
        return self.entity_fk if self.entity_fk is not None else self.name


def _t(column: str) -> sql.Composable:
    return sql.SQL("t.") + sql.Identifier(column)


# ONE SHAPE PER TAB. The tab ids are the Diagrams' own (charts.TAB_IDS), so
# a link from a chart lands on the same word; `market` lists
# processed_data.market_insights.
SHAPES: dict[str, Shape] = {
    # A document's own title where it is one, otherwise its host: an
    # archive that writes no titles holds `src_1416664` in the name column,
    # and grouping on that is one group per document (sqlbuild says why).
    "sources": Shape("sources", sqlbuild.readable_source_label("t"), _t("text_type")),
    "entities": Shape("entities", _t("text_name"), _t("text_type")),
    "events": Shape("events", _t("text_name"), _t("text_type")),
    "attributes": Shape("attributes", _t("text_name"), _t("text_type")),
    # A location's address is what a reader knows it by; its name is the
    # type word the extraction wrote ("Headquarters"), and stands in only
    # where the address is empty.
    "locations": Shape("locations",
                       sql.SQL("COALESCE(NULLIF(t.text_address, ''), t.text_name)"),
                       _t("text_type")),
    "ratings": Shape("ratings", _t("text_rating_name"), _t("text_perspective")),
    "market": Shape("market_insights", _t("text_fk_entity_id"), _t("text_topic"),
                    entity_fk=_t("text_fk_entity_id")),
    # The reference entity of the end the row is read from, and the role it
    # plays towards the other end (drilldown._SIDES).
    "connections": Shape("connections", sql.SQL("side.ref_id"), sql.SQL("side.role"),
                         entity_fk=sql.SQL("side.ref_id")),
}

# A tab the Diagrams have and this view does not is a link that leads
# nowhere; the reverse is a table nobody can reach.
assert set(SHAPES) == set(TAB_IDS), "SHAPES and charts.TAB_IDS disagree"


def table_of(tab: str) -> str:
    return shape_of(tab).table


def shape_of(tab: str) -> Shape:
    found = SHAPES.get(tab)
    if found is None:
        raise ListingError(f"there is no table {tab!r}", "one of " + ", ".join(TAB_IDS))
    return found


# ── The filters, and the WHERE they make ─────────────────────

@dataclass(frozen=True)
class Filters:
    """What the request narrows the table to. Every field is optional;
    `task` and `source` come together or not at all, and `exact` needs a
    name (or an entity id) and a type."""
    q: str = ""
    type: str = ""
    sort: str = "newest"
    task: str = ""
    source: str = ""
    # When the document is named: the moment it arrived, so that its rows
    # are read from that day's chunk and not from every chunk of the table
    # (drilldown.near says why the day is enough). None reads them all.
    added: datetime | None = None
    exact: bool = False
    name: str = ""
    entity_id: str = ""

    @property
    def grouped(self) -> bool:
        return self.sort in GROUPED_SORTS

    @property
    def newest_first(self) -> bool:
        return self.sort != "oldest"

    def check(self) -> None:
        """Refused rather than ignored: a filter that is quietly dropped
        answers with the whole table and looks like it worked."""
        if self.sort not in SORTS:
            raise ListingError(f"there is no sort {self.sort!r}", "one of " + ", ".join(SORTS))
        for label, value in (("q", self.q), ("type", self.type), ("name", self.name)):
            if len(value) > MAX_KEY_LENGTH:
                raise ListingError(f"{label} is too long", f"at most {MAX_KEY_LENGTH} characters")
        if bool(self.task) != bool(self.source):
            raise ListingError("a document is named by task and source together",
                               "send both task= and source=, or neither")
        if self.exact and not (self.name or self.entity_id):
            raise ListingError("exact=1 needs the name (or the entity id) of the group",
                               "send name=&type= as the group row carries them, "
                               "or entity_id=&type=")


# Name search on an entity-named tab: the rows whose entity is called so.
# The language IS bound here, unlike in app/scope.py's CTEs: this is a
# lookup for the rows of one language's table, and the names it matches
# are that language's.
_ENTITY_NAME_SEARCH = (
    "{fk} IN (SELECT e.text_entity_id FROM processed_data.entities e "
    "WHERE {idn} AND e.text_name ILIKE %(q_pat)s)")


def where(tab: str, ctx, filters: Filters, *, perspective: bool = True,
          extra: tuple[sql.Composable, ...] = ()) -> tuple[sql.Composable, dict[str, Any]]:
    """The WHERE of every statement of one page, and its params.

    Identity first, then the reader's perspective filter (TRUE when none
    is chosen), then the request's own filters, then whatever the caller
    adds - the document behind a Tables link, the members of a bucket, the
    two ends of a pair. `perspective=False` is for the document row itself:
    a document is opened by its key, and hiding it because the reader's
    filter would not have listed it answers 404 to a link the reader holds.
    """
    filters.check()
    shape = shape_of(tab)
    parts: list[sql.Composable] = [sqlbuild.identity("t")]
    params: dict[str, Any] = sqlbuild.identity_params(ctx.project, ctx.language)
    if perspective:
        parts.append(sqlbuild.perspective_scope(
            ctx.perspective, "t", sqlbuild.source_id_column(shape.table)))
        params.update(sqlbuild.perspective_params(ctx.perspective, ctx.min_importance))
    if filters.q:
        params["q_pat"] = sqlbuild.like_substring(filters.q)
        if shape.entity_named:
            parts.append(sql.SQL(_ENTITY_NAME_SEARCH).format(
                fk=shape.entity_fk, idn=sqlbuild.identity("e")))
            if tab == "connections":
                # THE SAME LIST AGAINST THE BASE COLUMNS AS WELL. `side.ref_id`
                # is one end of a lateral and no index answers it; the two
                # foreign keys underneath are indexed, and "one of the two
                # ends is in the list" is what lets the archive read the
                # matching rows instead of every connection of the project.
                parts.append(sql.SQL("({p} OR {c})").format(
                    p=sql.SQL(_ENTITY_NAME_SEARCH).format(
                        fk=_t("text_fk_parent_entity_id"), idn=sqlbuild.identity("e")),
                    c=sql.SQL(_ENTITY_NAME_SEARCH).format(
                        fk=_t("text_fk_child_entity_id"), idn=sqlbuild.identity("e"))))
        else:
            parts.append(sql.SQL("{n} ILIKE %(q_pat)s").format(n=shape.name))
    if filters.type:
        parts.append(sql.SQL("{t} ILIKE %(type_pat)s").format(t=shape.type))
        params["type_pat"] = sqlbuild.like_substring(filters.type)
    if filters.task:
        parts.append(sql.SQL("t.text_task_id = %(task)s AND t.{c} = %(source)s").format(
            c=sql.Identifier(sqlbuild.source_id_column(shape.table))))
        params["task"] = filters.task
        params["source"] = filters.source
        if filters.added is not None:
            parts.append(sql.SQL("t.date_added BETWEEN %(added)s::timestamptz - interval '1 day' "
                                 "AND %(added)s::timestamptz + interval '1 day'"))
            params["added"] = filters.added
    if filters.exact:
        # The group's own key and type, compared as the group statement
        # made them (COALESCE to ''), so the rows under a group are the
        # rows its count counted - a group of rows with no type included.
        if shape.entity_named and filters.entity_id:
            parts.append(sql.SQL("COALESCE({k}, '') = %(entity_x)s").format(k=shape.entity_fk))
            params["entity_x"] = filters.entity_id
        else:
            parts.append(sql.SQL("COALESCE({n}, '') = %(name_x)s").format(n=shape.name))
            params["name_x"] = filters.name
        parts.append(sql.SQL("COALESCE({t}, '') = %(type_x)s").format(t=shape.type))
        params["type_x"] = filters.type
    parts.extend(extra)
    return sql.SQL(" AND ").join(sql.SQL("({p})").format(p=p) for p in parts), params


def _from(shape: Shape) -> sql.Composable:
    """`FROM the table AS t`, doubled from both ends for connections."""
    return sql.SQL("FROM {t} AS t{s}").format(
        t=sqlbuild.table(shape.table), s=sides(shape.table, "t", True))


def rows_statement(tab: str, where_sql: sql.Composable, newest_first: bool = True) -> sql.Composable:
    """A page of rows, in the shape every listing has: the page cut first,
    the per-row lookups after (drilldown.page_rows says why).

    NEWEST MEANS NEWEST IN THE ARCHIVE. The page is ordered by `date_added`,
    the moment the collector wrote the row, and not by the row's own date
    (sqlbuild.TIMELINE_COLUMN), which the charts order by. The archive is
    partitioned on date_added and nothing else: a page ordered by it is the
    last chunk read backwards, and a page ordered by the row's own date is
    every chunk decompressed and sorted - on a table of a few million rows,
    half a second against twenty. The chart has a window that keeps it to
    a few chunks; this listing has none, because "every row" is its
    question. The date the row shows stays its own (row_date), as in every
    drilldown.

    NO `count(*) OVER ()` HERE. A Tables page has no chart to narrow it and
    "every source of the project" is the usual ask; the window function
    would run the lookups for every row before cutting the page. The total
    is count_statement(), run once for the first page.

    Params: dd_limit, dd_offset, plus those of `where_sql`.
    """
    shape = shape_of(tab)
    tl = sqlbuild.timeline(shape.table, "t")
    direction = sql.SQL("DESC" if newest_first else "ASC")
    inner = sql.SQL(
        "SELECT t.*{side}, {tl} AS row_date {f} WHERE {w} "
        "ORDER BY t.date_added {d}, t.bigint_id {d} LIMIT %(dd_limit)s OFFSET %(dd_offset)s"
    ).format(side=side_columns(shape.table), tl=tl, f=_from(shape), w=where_sql, d=direction)
    return page_rows(shape.table, "t", inner, with_total=False, newest_first=newest_first,
                     order_column="date_added")


def count_statement(tab: str, where_sql: sql.Composable) -> sql.Composable:
    """How many rows the filters match, and nothing else - no lookups."""
    return sql.SQL("SELECT count(*) AS n {f} WHERE {w}").format(f=_from(shape_of(tab)), w=where_sql)


def groups_statement(tab: str, where_sql: sql.Composable, most_first: bool = True) -> sql.Composable:
    """One row per name and type: how many, and the newest of them.

    The total number of groups rides on `count(*) OVER ()`: a group row is
    an aggregate already and carries no per-row lookup, so the window costs
    nothing the GROUP BY has not paid. Ties are broken by the newest row
    and then the name, so two groups of the same size keep one order
    between pages. "Newest" is the arrival, as in rows_statement, so the
    two sorts agree about which row is the newest.

    Params: dd_limit, dd_offset, plus those of `where_sql`.
    """
    shape = shape_of(tab)
    return sql.SQL(
        "SELECT COALESCE({k}, '') AS name, COALESCE({t}, '') AS type, count(*) AS n, "
        "max(t.date_added) AS newest, count(*) OVER () AS dd_total {f} WHERE {w} "
        "GROUP BY 1, 2 ORDER BY n {d}, newest DESC, name ASC "
        "LIMIT %(dd_limit)s OFFSET %(dd_offset)s"
    ).format(k=shape.key, t=shape.type,
             f=_from(shape), w=where_sql, d=sql.SQL("DESC" if most_first else "ASC"))


def document_added(conn, ctx, task: str, source: str) -> datetime | None:
    """When a document arrived, or None when the archive has no such
    document - read once, so that every table of its rows can be asked for
    that day's chunk (Filters.added)."""
    row = conn.execute(sql.SQL(
        "SELECT min(s.date_added) AS added FROM processed_data.sources s "
        "WHERE {idn} AND s.text_task_id = %(task)s AND s.text_source_id = %(source)s"
    ).format(idn=sqlbuild.identity("s")),
        {**sqlbuild.identity_params(ctx.project, ctx.language), "task": task, "source": source}
    ).fetchone()
    return row["added"] if row else None


# The names of one page of entity ids, in one statement. The ids of a page
# are in `IN (SELECT unnest(...))` and never `= ANY(array)`, for the reason
# routers/api_map.py measures: the subselect is hashed once and each row is
# one probe. Grouped by id alone, not by (task, id): a group of the Tables
# view spans every task an entity appears in, and one name is enough.
ENTITY_NAMES_SQL = """
    SELECT e.text_entity_id AS id, min(e.text_name) AS name
      FROM processed_data.entities e
     WHERE e.text_project = %(project)s AND e.text_language = %(language)s
       AND e.text_entity_id IN (SELECT unnest(%(ids)s::text[]))
     GROUP BY e.text_entity_id
"""


# ── A group row, in the shape the row table draws ────────────

# THE COLUMNS OF A GROUP ROW. The same Column shape as a drilldown's, so
# the page draws both tables with one renderer.
GROUP_COLUMNS: tuple[Column, ...] = (
    Column("name", "Name"),
    Column("type", "Type"),
    Column("count", "Rows"),
    Column("newest", "Newest", kind="date"),
)


def group_columns_json() -> list[dict[str, Any]]:
    return [{"key": c.key, "label": c.label, "wide": c.wide, "kind": c.kind,
             "sep": c.sep, "hint": c.hint}
            for c in GROUP_COLUMNS]


def group_row_links() -> dict[str, bool]:
    """The count cell OPENS: it lists the group's rows under it, which is
    the same control a drilldown's source link is. No entity link: a group
    is a name, not one entity."""
    return {"source": True, "entity": False}


def _rows(n: int) -> str:
    """"1 row", "42 rows" - the count cell says a number and a noun, and
    "1 rows" reads as a fault in the page."""
    return f"{n:,} row" + ("" if n == 1 else "s")


# What a group whose entity has no name in this language is called. Not
# the id: `ent:1822949` names a row to a machine and says nothing to a
# reader (drilldown.py).
UNNAMED = "(no name)"


def shape_group(tab: str, row: dict[str, Any], names: dict[str, str] | None = None) -> dict[str, Any]:
    """One group row in the shape drilldown.shape_row returns, so the page
    draws it with the same code.

    `key` is what expanding the group sends back - `name` and `type` as
    the statement made them, or `entity_id` and `type` on an entity-named
    tab - and it is the same comparison `where()` makes for `exact`, so the
    rows listed under a group are the rows its count counted.
    """
    shape = shape_of(tab)
    n = int(row.get("n") or 0)
    key_value = str(row.get("name") or "")
    typ = str(row.get("type") or "")
    if shape.entity_named:
        label = (names or {}).get(key_value) or UNNAMED
        key: dict[str, str] = {"entity_id": key_value, "type": typ}
    else:
        label = key_value
        key = {"name": key_value, "type": typ}
    return {
        "link": {"uri": "", "domain": "", "title": _rows(n)},
        "source": {"task": "", "id": ""},
        "key": key,
        "entity": "",
        "cells": {
            "name": [_part(label)],
            "type": [_part(typ)],
            "count": [_part(f"{n:,}")],
            "newest": _date(row.get("newest")),
        },
    }


def example(listing: "Listing") -> dict[str, str] | None:
    """The first row's name and type, in the words the tab searches by - the
    placeholder of the Name and Type fields, so the example under a reader's
    cursor is a row of THIS archive. None when the page holds no row.

    Read off the raw row with the same choices SHAPES makes in SQL: the
    label for a document, the address for a place, the entity's name where
    the row carries an id (the group statement has looked those names up;
    a plain row carries the name beside its id)."""
    if not listing.rows:
        return None
    row = listing.rows[0]
    if listing.grouped:
        name = str(row.get("name") or "")
        if shape_of(listing.tab).entity_named:
            name = listing.names.get(name) or ""
        return {"name": name, "type": str(row.get("type") or "")}
    tab = listing.tab
    if tab == "sources":
        name = readable_title(str(row.get("source_name") or ""), str(row.get("source_uri") or ""))
    elif tab == "locations":
        name = str(row.get("address") or row.get("name") or "")
    elif tab == "ratings":
        name = str(row.get("rating_name") or "")
    elif tab == "market":
        name = str(row.get("entity_name") or "")
    elif tab == "connections":
        name = str(row.get("reference_name") or "")
    else:
        name = str(row.get("name") or "")
    if tab == "ratings":
        typ = row.get("perspective")
    elif tab == "market":
        typ = row.get("topic")
    else:
        typ = row.get("type")
    return {"name": plain(name), "type": plain(str(typ or ""))}


def group_csv_columns() -> list[str]:
    return ["row"] + [c.key for c in GROUP_COLUMNS]


def csv_group(tab: str, row: dict[str, Any], number: int,
              names: dict[str, str] | None = None) -> dict[str, str]:
    """One group row, flattened for the file - the words only."""
    item = shape_group(tab, row, names)
    out = {"row": str(number)}
    for column in GROUP_COLUMNS:
        parts = item["cells"].get(column.key) or []
        out[column.key] = column.sep.join(p["text"] for p in parts if p["text"])
    return out


# ── Running a page ───────────────────────────────────────────

@dataclass
class Listing:
    """One page as it came back: raw rows, for the caller to shape or to
    write to a file, and what is known about the rest."""
    tab: str
    table: str
    grouped: bool
    rows: list[dict[str, Any]] = field(default_factory=list)
    # How many the filters match; None when the page did not ask.
    total: int | None = None
    # Entity id -> name, for the group rows of an entity-named tab.
    names: dict[str, str] = field(default_factory=dict)

    def shaped(self) -> list[dict[str, Any]]:
        if self.grouped:
            return [shape_group(self.tab, row, self.names) for row in self.rows]
        return [shape_row(self.table, row) for row in self.rows]

    def csv_columns(self) -> list[str]:
        return group_csv_columns() if self.grouped else csv_columns(self.table)

    def csv_rows(self) -> list[dict[str, str]]:
        if self.grouped:
            return [csv_group(self.tab, row, i, self.names) for i, row in enumerate(self.rows, start=1)]
        return [csv_row(self.table, row, i) for i, row in enumerate(self.rows, start=1)]


def page(conn, ctx, tab: str, filters: Filters, *, limit: int, offset: int,
         count: bool = True, perspective: bool = True,
         extra: tuple[sql.Composable, ...] = (),
         extra_params: dict[str, Any] | None = None) -> Listing:
    """One page of one table: the rows, or the groups, and the total.

    `limit` is the caller's - a page asks for one more than it shows, so
    "is there more" is known without a second statement; the export asks
    for its ceiling plus one for the same reason. `count` runs the count
    for a row page (the group page carries its own); a caller that already
    holds the total passes False.
    """
    shape = shape_of(tab)
    where_sql, params = where(tab, ctx, filters, perspective=perspective, extra=extra)
    params.update(extra_params or {})
    paged = {**params, "dd_limit": limit, "dd_offset": offset}
    listing = Listing(tab, shape.table, filters.grouped)
    if filters.grouped:
        statement = groups_statement(tab, where_sql, most_first=filters.sort == "most")
        listing.rows = [dict(r) for r in conn.execute(statement, paged).fetchall()]
        # A page past the last one carries no rows and therefore no total.
        listing.total = int(listing.rows[0]["dd_total"]) if listing.rows else 0
        if shape.entity_named:
            listing.names = entity_names(conn, ctx, [str(r["name"]) for r in listing.rows if r["name"]])
        return listing
    statement = rows_statement(tab, where_sql, newest_first=filters.newest_first)
    listing.rows = [dict(r) for r in conn.execute(statement, paged).fetchall()]
    if count:
        listing.total = int(conn.execute(count_statement(tab, where_sql), params).fetchone()["n"])
    return listing


def entity_names(conn, ctx, ids: list[str]) -> dict[str, str]:
    """The names of these entity ids in the reader's language, once."""
    wanted = sorted(set(ids))
    if not wanted:
        return {}
    rows = conn.execute(ENTITY_NAMES_SQL, {**sqlbuild.identity_params(ctx.project, ctx.language),
                                           "ids": wanted}).fetchall()
    return {str(r["id"]): str(r["name"] or "") for r in rows}


# The entity ids an entity bucket stands for: every entity whose name and
# type match a member. The project is bound and the language is not, as in
# app/scope.py's CTEs - "Apfel (Frucht)" and "Apple (Fruit)" are one id.
_BUCKET_ENTITY_IDS = (
    "SELECT DISTINCT e.text_entity_id AS id FROM processed_data.entities e JOIN {bt} ON {m} "
    "WHERE e.text_project = %(project)s AND e.text_entity_id IS NOT NULL AND e.text_entity_id <> ''")


def bucket_entity_ids(conn, ctx, grouping: Grouping) -> list[str]:
    """The ids of the entities in an entity bucket, resolved once so a
    statement can take them as one list (end_predicate)."""
    terms, params = bucket_terms([(m.get("name") or "", m.get("type")) for m in grouping.members])
    statement = sql.SQL(_BUCKET_ENTITY_IDS).format(bt=terms, m=member_match("e", "bt"))
    rows = conn.execute(statement, {**params, "project": ctx.project}).fetchall()
    return sorted(str(r["id"]) for r in rows)


# ── The documents that hold a bucket's members ───────────────

# A row of `table` that belongs to the document `t` (a sources row) and
# says one of the bucket's values. EXISTS and not a join, for the reason
# drilldown.source_join gives: a document that holds three members is one
# document, and a join would list it three times.
_HOLDS = ("EXISTS (SELECT 1 FROM {table} x{bt} WHERE {idn} "
          "AND x.text_task_id = t.text_task_id AND x.text_fk_source_id = t.text_source_id "
          "AND {m})")


def bucket_holders(grouping: Grouping, table: str, column: str) -> tuple[sql.Composable, dict[str, Any]]:
    """The predicate on a sources row `t`: this document holds at least one
    member of the bucket. `table` and `column` are where the bucket's kind
    lives (routers/api_settings.KIND_INFO), which the router knows.

    Four shapes, one per way a kind is matched:

      source_type      the document's own type is one of the values;
      entity           an entity row of the document matches a member by
                       name AND type (scope.member_match - a member with
                       no type means "this name, whatever its type");
      connection_type  a connection of the document carries one of the
                       values at either end;
      everything else  a row of the kind's table says one of the values
                       (scope.value_match - a substring for a place).
    """
    kind = grouping.kind
    if kind == "entity":
        terms, params = bucket_terms([(m.get("name") or "", m.get("type")) for m in grouping.members])
        frag = sql.SQL(_HOLDS).format(
            table=sql.SQL("processed_data.entities"),
            bt=sql.SQL(" JOIN ") + terms + sql.SQL(" ON ") + member_match("x", "bt"),
            idn=sqlbuild.identity("x"), m=sql.SQL("TRUE"))
        return frag, params
    values = sorted({(v or "").strip().lower() for v in grouping.values if (v or "").strip()})
    params = {"vals": values}
    if kind == "source_type":
        return value_match(kind, "t", "text_type"), params
    if kind == "connection_type":
        match = sql.SQL("({p} OR {c})").format(
            p=value_match(kind, "x", "text_type_parent_to_child"),
            c=value_match(kind, "x", "text_type_child_to_parent"))
        table = "processed_data.connections"
    else:
        match = value_match(kind, "x", column)
    frag = sql.SQL(_HOLDS).format(table=sql.SQL(table), bt=sql.SQL(""),
                                  idn=sqlbuild.identity("x"), m=match)
    return frag, params


# ── Two entities, and one place ──────────────────────────────

def end_predicate(expr: sql.Composable, param: str, many: bool) -> sql.Composable:
    """`expr = %(param)s`, or the id list of a bucket - as `IN (SELECT
    unnest(...))`, never `= ANY(array)` (routers/api_map.py measures why)."""
    if many:
        return sql.SQL("{e} IN (SELECT unnest(%({p})s::text[]))").format(e=expr, p=sql.SQL(param))
    return sql.SQL("{e} = %({p})s").format(e=expr, p=sql.SQL(param))


def pair_where(a_many: bool, b_many: bool, role: bool = False) -> tuple[sql.Composable, ...]:
    """The connections between two ends, read from `a`'s side.

    BOTH THE INDEXED PREDICATE AND THE SIDE'S. `side.ref_id = a AND
    side.tgt_id = b` is the fact the row states, and it is an expression
    over a lateral that no index answers. The same pair on the base
    columns, either way round, is what idx_connections_parent and
    idx_connections_child can read - so it is written as well, and the
    side predicate then picks the one end of each stored row that reads
    "a is a <role> of b".

    Params: a, b (a string or a list each, per `a_many`/`b_many`), role.
    """
    parent, child = _t("text_fk_parent_entity_id"), _t("text_fk_child_entity_id")
    base = sql.SQL("({pa} AND {cb}) OR ({pb} AND {ca})").format(
        pa=end_predicate(parent, "a", a_many), cb=end_predicate(child, "b", b_many),
        pb=end_predicate(parent, "b", b_many), ca=end_predicate(child, "a", a_many))
    side = sql.SQL("{ra} AND {tb}").format(
        ra=end_predicate(sql.SQL("side.ref_id"), "a", a_many),
        tb=end_predicate(sql.SQL("side.tgt_id"), "b", b_many))
    parts = [base, side]
    if role:
        parts.append(sql.SQL("side.role = %(role)s"))
    return tuple(parts)


def place_where(*, address: bool, entity: bool) -> tuple[sql.Composable, ...]:
    """The locations at one place: by address, or by coordinates.

    Two branches and not a CASE, because the two are answered by two
    different indexes and one predicate that names both columns is answered
    by neither. Params: address, or lat and lng; entity when `entity`.
    """
    if address:
        parts = [sql.SQL("lower(t.text_address) = lower(%(address)s)")]
    else:
        parts = [sql.SQL("t.float_latitude BETWEEN %(lat)s - {tol} AND %(lat)s + {tol} "
                         "AND t.float_longitude BETWEEN %(lng)s - {tol} AND %(lng)s + {tol}").format(
            tol=sql.Literal(COORDINATE_TOLERANCE))]
    if entity:
        # A list, never one id: the entity the map drew is every id of its
        # name (routers/api_tables.same_ids), and its rows are under all of
        # them.
        parts.append(sql.SQL("t.text_fk_entity_id IN (SELECT unnest(%(entity)s::text[]))"))
    return tuple(parts)
