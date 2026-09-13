"""What is inside ONE spot of the Heatmap: the entities, most results first.

NOT A CHART, and that is why it is not in the registry - `charts/maps.py`
sits here for the same reason. It lives in this package because it answers
about the same table the Locations charts do, with the same identity, the
same scope CTEs and the same drilldown envelope, and a listing that resolved
"one entity in this project" its own way would eventually disagree with the
bars beside it.

(There is also `app/places.py`, one directory up. That one reads a typed
PLACE NAME - "Springfield", "USA" - out of the archive's addresses. This one
takes a point on the grid and says who is standing there. Two questions,
two modules, and neither imports the other.)

── WHY THIS IS A NEW SQL SHAPE AND NOT A THIRD CALLER OF drilldown.py ──

The drilldown module already has two statements and this is neither:

  rows_statement   the archive rows behind one point, newest first. Orders
                   by recency only, and a list of "most results first"
                   cannot be built by sorting a page of twenty by date.
  key_statement    groups by a category and ranks the groups by count -
                   which IS the shape here - but it is capped at
                   KEY_CHART_LIMIT (25) and has no LIMIT/OFFSET at all,
                   because a chart draws its top N once and never pages.

"Grouped by entity, ranked by count, and paged" is the two of them crossed,
so it is written out here. What IS reused is everything a reader would notice
if it drifted: the page size, the running number and the columns' shape.

It also learns the same lesson rows_statement learnt, in a place where the
cure has to be different: a per-row lookup into a hypertable is what makes a
listing take minutes, and the cure there is to cut the page before the lookup.
That is not available here, because the grouping key IS what the lookup
returns - so the lookup is done once per entity instead, in one join
(_HERE … _NAMED below, with the measurement that decided it).

── ONE ROW PER ENTITY, AND A BUCKET IS ONE ROW UNDER ITS OWN NAME ──

The rest of the dashboard answers a bucket as ONE thing (app/scope.py: a
bucket wins over a literal match), and this list does too: four spellings of
Apple standing at one address are one row called Apple, not four rows a
reader has to add up.

THE FOLD HAPPENS IN SQL, WHICH IS THE ONLY PLACE IT CAN. Buckets are the
dashboard's own table and the obvious way to apply them is in Python, after
the rows come back - which is what the Map does (routers/api_map.py:
_bucket_groups). It cannot work here: this list is PAGED, so folding after
the LIMIT would merge the members that happened to land on the same page and
leave the rest as separate rows further down. The membership therefore goes
into the statement as a VALUES list and the grouping is the database's.

AND EACH ENTITY LANDS IN EXACTLY ONE BUCKET, unlike the Map. A map draws an
entity in every bucket that holds it, because a connection to "Angela Merkel"
really is a connection to "Germany" and to "EU leaders". A LIST may not: two
rows counting the same locations would add up to more than the spot holds,
and the number under the title would stop matching the field. So the first
bucket in reading order wins - `ORDER BY ord LIMIT 1` below, which is
app/scope.py's `holder_for` written as SQL - and every location is counted
once.

── THE FOUR FILTERS ARE THE CALLER'S ──

This builder is handed the WHERE the heat grid was counted with and never
composes one of its own. A list narrowed by fewer filters than the field it
was opened from would show rows the spot was never counted from, and there
would be nothing on screen to explain the difference (routers/api_map.py:
_heat_filters is the one place both of them come from).
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Iterable

from psycopg import sql

from .. import sqlbuild
from . import drilldown

# One page of this list is one page of every other drilldown: the dialog is
# the same dialog and the running number counts through the same twenty.
PAGE_SIZE = drilldown.PAGE_SIZE

# How many location types one row names. A spot is about 110 metres across
# and an entity rarely carries more than one or two kinds of address in it;
# past four the cell is a list nobody reads. The row also carries how many
# there were, so a cell that had to drop three says so rather than looking
# complete.
MAX_KINDS = 4

# AN ADDRESS THE ARCHIVE NEVER ATTACHED TO ANYTHING is still a row here,
# with a dash where the entity would be. Dropping it would be quicker and
# would make the counts under the title stop adding up to the number the
# field was painted from - and a silently missing row is a lie about a
# total, which is the reason every "(not stated)" in this package exists.


# ── The columns ──────────────────────────────────────────────
#
# THE SHELL DRAWS THREE OF THEM ITSELF and they are not repeated here: the
# running number, the Source column and - because `row_links()` asks for it
# - the Entity column (static/js/drilldown.js: drawHead). So this list is
# read left to right as: which row, from how many documents, which entity,
# how much is here, of what kind, at which address, how recently.
#
# NO WIDE FREE-TEXT COLUMN. Every other drilldown ends in one because every
# other drilldown lists ARCHIVE ROWS, and a row has a reason or a summary
# somebody wrote. A row here is an AGGREGATE of several of them, and the
# honest free text for it would be several paragraphs concatenated - which
# is not a summary of anything.
COLUMNS: tuple[drilldown.Column, ...] = (
    drilldown.Column("count", "Locations here"),
    drilldown.Column("kinds", "Location types"),
    drilldown.Column("address", "Address"),
    # "Newest" and not "Date": the row stands for several locations, and the
    # one date that can be given for a group of them is the last time the
    # archive wrote one.
    drilldown.Column("newest", "Newest", kind="date"),
)


def columns_json() -> list[dict[str, Any]]:
    """The header the dialog draws, in the shape it already reads
    (charts/drilldown.py: columns_json)."""
    return [{"key": c.key, "label": c.label, "wide": c.wide, "kind": c.kind,
             "sep": c.sep, "hint": c.hint}
            for c in COLUMNS]


def row_links() -> dict[str, bool]:
    """Which of the shell's two link columns this listing wants.

    THE ENTITY COLUMN, YES; THE DOMAIN'S DIAGRAMS, NO. A row here is one
    entity, so "everything else this dashboard knows about it" is exactly
    the next question - and the Diagrams link answers it. It is NOT one
    document: an entity standing in a spot was usually written about by
    several, and a link labelled with one of their domains would be a claim
    about the row that is not true of it. The Source cell says how many
    documents there were instead (see `shape_row`).
    """
    return {"source": False, "entity": True}


# ── The cell on the grid ─────────────────────────────────────

def cell(value: Any, precision: int, field: str) -> Decimal:
    """One coordinate, put back on the same grid the count was made on.

    THIS IS THE HALF OF THE FEATURE THAT IS EASIEST TO GET WRONG. The heat
    grid has no server-side identity: `GET /heat` answers with aggregated
    cells, and the only key a spot has is the pair of rounded numbers the
    browser holds. So the listing has to land on the SAME cell - the same
    rounding, half away from zero, at the same precision the grid used
    (routers/api_map.py: HEAT_PRECISION) - or it lists a neighbour.

    Bound as a `numeric` and compared against `round(…::numeric, p)`, which
    is exact decimal arithmetic on both sides. Through `float8` it would be
    two roundings of one number and 37.333 would not have to equal 37.333.

    >>> cell("37.3325", 3, "lat")
    Decimal('37.333')
    >>> cell("-122.031", 3, "lng")
    Decimal('-122.031')
    """
    text = str("" if value is None else value).strip()
    try:
        number = Decimal(text)
    except (InvalidOperation, ValueError):
        raise ValueError(f"{field} is not a number")
    if not number.is_finite():
        raise ValueError(f"{field} is not a number")
    return number.quantize(Decimal(1).scaleb(-int(precision)), rounding=ROUND_HALF_UP)


def cell_predicate(alias: str, precision: int) -> sql.Composable:
    """The cell, twice: once so an index can find it and once exactly.

    `round(lat, p) = %(spot_lat)s` is the very expression the grid groups by
    (routers/api_map.py: _HEAT) and it DECIDES which rows are in the spot.
    But no index can answer it - there is none on the rounded value, and
    `idx_locations_coords` is on the plain columns - so on its own it reads
    the whole project: 2.0 s over a real archive's 1.17 M addresses, for a
    dialog somebody has just clicked.

    So it is preceded by the range the rounding can only have come from,
    which the index CAN answer, and which is a superset by construction: a
    value rounds to c exactly when it lies within half a step of c. The
    exact predicate still decides; the range only says where to look. 1.1 s
    on the same spot, and it falls with the archive's own indexes rather
    than with its size.
    """
    return sql.SQL(
        "{a}.float_latitude  BETWEEN %(spot_lat_lo)s AND %(spot_lat_hi)s"
        " AND {a}.float_longitude BETWEEN %(spot_lng_lo)s AND %(spot_lng_hi)s"
        " AND round({a}.float_latitude::numeric,  {p}) = %(spot_lat)s"
        " AND round({a}.float_longitude::numeric, {p}) = %(spot_lng)s"
    ).format(a=sql.Identifier(alias), p=sql.Literal(int(precision)))


def cell_params(lat: Decimal, lng: Decimal, precision: int) -> dict[str, Any]:
    """The exact cell, and the range around it the index reads.

    Half a step either side, INCLUSIVE at both ends: a closed interval is a
    superset of the half-open one the rounding really produces, and a
    superset is all a prefilter may ever be - the exact comparison beside it
    throws the extra row away, while a range that was one row too tight
    would drop an address the field had counted.
    """
    step = Decimal(1).scaleb(-int(precision)) / 2
    return {"spot_lat": lat, "spot_lng": lng,
            "spot_lat_lo": float(lat - step), "spot_lat_hi": float(lat + step),
            "spot_lng_lo": float(lng - step), "spot_lng_hi": float(lng + step)}


# ── The buckets, as a table the statement can join ───────────

def bucket_members(groupings: Iterable[Any]) -> list[tuple[str, str | None, str]]:
    """(member name, member type, bucket name), lower-cased, in the order
    app/scope.py reads them - a project's own buckets before the ones that
    apply to every project, and a bucket's members in its own order.

    An empty type means "this name, whatever its type", exactly as
    `BUCKET_LOOKUP_SQL` and `holders_for` read it: the preseed's Apple
    bucket holds Apple (Company) and deliberately not Apple (Fruit).

    >>> class G:
    ...     def __init__(self, name, members): self.name, self.members = name, members
    >>> bucket_members([G("Apple", [{"name": "Apple Inc.", "type": "Company"}])])
    [('apple inc.', 'company', 'Apple')]
    """
    out: list[tuple[str, str | None, str]] = []
    seen: set[tuple[str, str | None]] = set()
    for group in groupings:
        for member in getattr(group, "members", []) or []:
            name = (member.get("name") or "").strip().lower()
            if not name:
                continue
            typ = (member.get("type") or "").strip().lower() or None
            if (name, typ) in seen:
                continue
            seen.add((name, typ))
            out.append((name, typ, group.name))
    return out


def _bucket_table(members: list[tuple[str, str | None, str]],
                  prefix: str = "pb") -> tuple[sql.Composable | None, dict[str, Any]]:
    """`(VALUES (0, 'apple inc.', 'company', 'Apple'), …) AS pb(ord, name, type, bucket)`.

    Every value is bound, never spliced: a bucket name is text a customer
    typed into a form (app/scope.py: bucket_terms says the same about the
    same table). `ord` is the reading order, so the LATERAL below can take
    the FIRST bucket that holds a name and be `holder_for`.
    """
    if not members:
        return None, {}
    params: dict[str, Any] = {}
    rows: list[sql.Composable] = []
    for i, (name, typ, bucket) in enumerate(members):
        params[f"{prefix}_n{i}"] = name
        params[f"{prefix}_t{i}"] = typ
        params[f"{prefix}_b{i}"] = bucket
        rows.append(sql.SQL("({o}, %({n})s::text, %({t})s::text, %({b})s::text)").format(
            o=sql.Literal(i), n=sql.SQL(f"{prefix}_n{i}"),
            t=sql.SQL(f"{prefix}_t{i}"), b=sql.SQL(f"{prefix}_b{i}")))
    frag = sql.SQL("(VALUES {rows}) AS {p}(ord, name, type, bucket)").format(
        rows=sql.SQL(", ").join(rows), p=sql.Identifier(prefix))
    return frag, params


# ── The statement ────────────────────────────────────────────

# ONE LOOKUP PER ENTITY, NEVER ONE PER LOCATION ROW - AND THAT IS THE WHOLE
# SHAPE OF THIS STATEMENT.
#
# The obvious way to write it is the way every drilldown row gets its entity:
# a LEFT JOIN LATERAL over `processed_data.entities` hanging off the location
# row (charts/drilldown.py: entity_join). It is right for twenty rows and
# ruinous here, because the grouping key IS the entity's name - so the lookup
# cannot be cut to a page the way rows_statement cuts its document lookups,
# and it runs for every address in the spot before anything is grouped.
#
# MEASURED, on a large archive (2.28 M entities in one project): its
# busiest spot is 72,712 addresses - "United States", a country-level
# geocode - carrying 15,098 entities. As a LATERAL the statement did not
# finish in 150 SECONDS, at roughly 10 ms per lookup into a hypertable of
# hundreds of chunks. As ONE grouped semi-join over the distinct entity ids
# the same answer takes 1.3 s.
#
# So the pieces run in this order, each over fewer rows than the one before:
#   here    the addresses in the cell. One index range scan, no joins.
#   pairs   the distinct entity ids in it.
#   named   their names, in one join, not fifteen thousand lookups.
#   folded  the bucket each of those names falls in - the tiny VALUES list,
#           read once per ENTITY rather than once per address.
#   grouped one row per bucket-or-name, ranked, and then the page.


# RAW COLUMNS ONLY, AND THAT IS ALSO MEASURED. Every expression in this
# SELECT is compiled once PER CHUNK, and a large archive's locations
# hypertable has hundreds of them: with `nullif(btrim(…))` here the plan carried
# 3,418 JIT functions and PostgreSQL spent 70 SECONDS compiling them for 4.8
# seconds of work. The tidying happens once, in `grouped` below, where it is
# one node instead of five hundred and sixty. (map_spot turns JIT off for
# this statement as well - see the note there.)
_HERE = (
    "here AS (SELECT {a}.text_task_id AS tid, {a}.text_fk_entity_id AS eid,"
    " {a}.text_fk_source_id AS sid,"
    " {a}.text_type AS kind,"
    " {a}.text_address AS address,"
    " {tl} AS at"
    " FROM {t} AS {a} WHERE {idn} AND {w})"
)

_PAIRS = "pairs AS (SELECT DISTINCT eid FROM here)"

# The name and type an id is shown under - ONE row per entity id, whichever
# documents named it.
#
# BY THE ID ALONE AND NOT BY (task, id), which is this file's own doctrine
# about what an entity IS: the extraction reuses an id whenever it recognises
# the same thing again, so "Foxconn in the battery document" and "Foxconn in
# the supplier filing" are one company (routers/api_map.py's header says the
# same at length). Keyed by the pair, one company standing in one spot under
# two task ids would still be one row - they merge on the NAME below - but
# the two documents could disagree about the spelling and the row would then
# be named by whichever came back first.
#
# So the commonest spelling wins and ties go to the alphabet: the identical
# rule _NAMES and charts/maps.py's `named` already follow, which is what
# makes a row keep its name between page 1 and page 2.
_NAMED = (
    "named AS (SELECT DISTINCT ON (id) id, name, type FROM ("
    "SELECT e.text_entity_id AS id, nullif(btrim(e.text_name), '') AS name,"
    " nullif(btrim(e.text_type), '') AS type, count(*) AS seen"
    " FROM processed_data.entities AS e"
    " WHERE {idn} AND e.text_entity_id IN (SELECT eid FROM pairs)"
    " AND btrim(e.text_name) <> ''"
    " GROUP BY 1, 2, 3) AS nm"
    " ORDER BY id, seen DESC, name)"
)

_FOLDED = (
    "folded AS (SELECT n.id, n.name, n.type, {b}.bucket"
    " FROM named AS n LEFT JOIN LATERAL ("
    "SELECT {p}.bucket FROM {v}"
    " WHERE {p}.name = lower(n.name)"
    " AND ({p}.type IS NULL OR {p}.type = lower(n.type))"
    " ORDER BY {p}.ord LIMIT 1) AS {b} ON true)"
)

_GROUPED = (
    "grouped AS (SELECT coalesce({bucket}, w.name) AS x,"
    " ({bucket} IS NOT NULL) AS bucketed,"
    " count(*) AS n,"
    # HOW MANY DOCUMENTS, NOT WHICH ONE. See row_links() above: the row is an
    # entity and the documents behind it are usually several. It counts
    # (task, source) pairs, because a source id is only unique inside its
    # task (charts/drilldown.py: source_join).
    " count(DISTINCT (h.tid, h.sid)) AS docs,"
    # The type this entity is written under most often. A bucket's members
    # can disagree; the commonest is what the row is called.
    " mode() WITHIN GROUP (ORDER BY w.type) AS type,"
    # The address the archive writes most often for it HERE - the same rule
    # the spot's own name follows (routers/api_map.py: _HEAT) and the one the
    # Locations map places a pin by (charts/maps.py).
    " mode() WITHIN GROUP (ORDER BY nullif(btrim(h.address), '')) AS address,"
    " (array_remove(array_agg(DISTINCT nullif(btrim(h.kind), '')), NULL))[1:{kinds}] AS kinds,"
    " count(DISTINCT nullif(btrim(h.kind), '')) AS kinds_total,"
    " max(h.at) AS newest"
    " FROM here AS h LEFT JOIN {who} AS w ON w.id = h.eid"
    " GROUP BY 1, 2)"
)

_PAGE = (
    "SELECT g.*, count(*) OVER () AS groups, sum(g.n) OVER () AS locations"
    " FROM grouped AS g"
    # MOST RESULTS FIRST, and then the alphabet so that two rows with the same
    # count come back in the same order twice running - a list that reshuffles
    # between two pages loses rows off the end of one and repeats them at the
    # top of the next.
    " ORDER BY g.n DESC, lower(g.x), g.bucketed DESC"
    " LIMIT %(dd_limit)s OFFSET %(dd_offset)s"
)

_BKT = "spot_bkt"      # the bucket an entity belongs to, if any


def _with(scope, extra: list[sql.Composable], body: sql.Composable) -> sql.Composable:
    """`WITH ent AS (…), src AS (…), <extra> <body>` - and nothing at all on
    an unscoped search, where the scope's predicates are TRUE and its sets
    are never consulted (app/scope.py: ScopeSet.scoped)."""
    ctes = (list(scope.ctes()) if (scope is not None and scope.scoped) else []) + list(extra)
    if not ctes:
        return body
    return sql.SQL("WITH {c} {b}").format(c=sql.SQL(", ").join(ctes), b=body)


def spot_statement(scope, where: sql.Composable,
                   members: list[tuple[str, str | None, str]],
                   alias: str = "l") -> tuple[sql.Composable, dict[str, Any]]:
    """The entities in one spot, most results first, one page of them.

    `where` is the caller's - the cell, and the four filters the field was
    drawn with. Binds `dd_limit` and `dd_offset`, the same two names every
    other drilldown page binds.

    HOW MANY THERE ARE RIDES ON THE SAME STATEMENT. `count(*) OVER ()` over
    the grouped rows is the number of ENTITIES and `sum(n) OVER ()` the
    number of locations, both computed before the LIMIT - so the dialog's
    "Row 1-20 of 214" and the sentence under its title cost no second query,
    exactly as charts/drilldown.py's listing gets its total.
    """
    a = sql.Identifier(alias)
    table, bucket_params = _bucket_table(members)
    ctes = [
        sql.SQL(_HERE).format(a=a, t=sqlbuild.table("locations"),
                              tl=sqlbuild.timeline("locations", alias),
                              idn=sqlbuild.identity(alias), w=where),
        sql.SQL(_PAIRS),
        sql.SQL(_NAMED).format(idn=sqlbuild.identity("e")),
    ]
    if table is None:
        # No bucket of entities in this project: nothing to fold, and no
        # reason to walk an empty VALUES list once per entity.
        who, bucket = sql.SQL("named"), sql.SQL("NULL::text")
    else:
        ctes.append(sql.SQL(_FOLDED).format(
            v=table, p=sql.Identifier("pb"), b=sql.Identifier(_BKT)))
        who, bucket = sql.SQL("folded"), sql.SQL("w.bucket")
    ctes.append(sql.SQL(_GROUPED).format(
        bucket=bucket, who=who, kinds=sql.Literal(MAX_KINDS)))
    return _with(scope, ctes, sql.SQL(_PAGE)), bucket_params


# ── One row, as the dialog reads it ──────────────────────────

def _part(text: Any) -> dict[str, Any]:
    return {"text": "" if text is None else str(text)}


def _kinds(row: dict[str, Any]) -> list[dict[str, Any]]:
    """The location types this entity carries here, and - when there were
    more than the row lists - how many were left out. A cell that quietly
    dropped three kinds looks complete and is not."""
    listed = [str(k) for k in (row.get("kinds") or []) if str(k).strip()]
    total = int(row.get("kinds_total") or len(listed))
    parts = [_part(k) for k in listed]
    if total > len(listed):
        parts.append(_part(f"and {total - len(listed)} more"))
    return parts or [_part("")]


def _date(value: Any) -> list[dict[str, Any]]:
    if not value:
        return [_part("")]
    part = _part(value.date().isoformat() if hasattr(value, "date") else value)
    if hasattr(value, "isoformat"):
        part["iso"] = value.isoformat()
    return [part]


def _documents(count: int) -> str:
    """"1 document", "4 documents" - the Source cell of a row that stands
    for several of them. Never "1 documents": the dialog says a number and a
    noun in three places, and two that disagree read as a fault in the page.
    """
    return f"{count:,} document" + ("" if count == 1 else "s")


def shape_row(row: dict[str, Any]) -> dict[str, Any]:
    """One grouped row in the shape static/js/drilldown.js draws.

    `entity` is BOTH the words in the cell and the term the link carries
    (drilldown.js: entityCell), so it has to be a term this dashboard can
    resolve back to the same thing:

      a bucket   its own name. A bare name resolves to the bucket, which is
                 the dashboard's rule everywhere (app/scope.py).
      an entity  "Apple (Fruit)" - the spelling app/scope.py's
                 split_entity_term reads as ONE entity. The bare name would
                 resolve to a BUCKET of that name where one exists, and the
                 link would then open a page about four companies while the
                 row it came from is about the fruit.
      nothing    the empty string, and the dialog draws its own "not stated"
                 dash for it (drilldown.js: entityCell). A word here would
                 become a link to a page about an entity called
                 "(not stated)", which nothing in the archive is.
    """
    name = str(row.get("x") or "").strip()
    typ = str(row.get("type") or "").strip()
    bucketed = bool(row.get("bucketed"))
    if not name:
        entity = ""
    elif bucketed or not typ:
        entity = name
    else:
        entity = f"{name} ({typ})"
    docs = int(row.get("docs") or 0)
    return {
        # No document link: this row is an entity, not a row of one document
        # (row_links above). The cell carries how many there were.
        "link": {"uri": "", "domain": "", "title": _documents(docs)},
        "entity": entity,
        "cells": {
            "count": [_part(f"{int(row.get('n') or 0):,}")],
            "kinds": _kinds(row),
            "address": [_part(row.get("address") or "")],
            "newest": _date(row.get("newest")),
        },
    }
