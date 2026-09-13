"""What a chart counted, and the rows behind one of its points.

A chart and its drilldown are the same question asked twice: "how many
sources per day, split by importance" and "which sources are the eleven in
Tuesday's High Importance bar". If the two are written apart they drift, and
a drilldown that shows different rows than the bar it came from is worse than
no drilldown at all - it is a wrong answer with a confident number over it.

So a builder does not write SQL. It fills in a `Plan`: the table its rows
come from, the joins it needs, the predicates that narrow them, and the
expressions that make a bucket, a category and a dataset out of a row. This
module turns one Plan into

  * the aggregate statement that draws the chart (`chart_statement`), and
  * the listing statement for one clicked point (`rows_statement`),

with the SAME table, joins and predicates in both, plus the key's own
predicate on the listing side. The only way the two can disagree is if
somebody writes SQL by hand again.

Three key shapes, one per chart kind (charts/__init__.py DRILL_KEYS):

  bucket  a time chart: the bar's period start, ISO 8601
  x       a key chart: the category, as the chart's own x value (an entity
          id for "by name", a group key for "by colour group", the text
          itself for a domain or a type)
  x + y   a matrix: both axes

A dataset narrows it further: a series dataset compares the series
expression to the dataset id, a measure dataset applies the measure's own
filter ("high relevance"), and a chart with a single unsplit dataset adds
nothing. Every predicate compares the SAME expression the chart grouped by,
which is what makes "the rows behind this bar" true rather than plausible.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

from psycopg import sql

from .. import sqlbuild
from ..timeframes import Window, add_units
from ..textclean import plain

# How many rows one drilldown page carries. Twenty is what fits in the
# dialog without scrolling on a laptop; the rest arrives as the reader
# scrolls (static/js/drilldown.js).
PAGE_SIZE = 20

# A matrix is a picture, not a table: past this many cells nothing can be
# told apart, and the ones that are left are the biggest cells.
MATRIX_CELL_LIMIT = 60

# A key from the request is compared against a text column. Longer than this
# it cannot match anything the archive holds, and it is not worth binding.
MAX_KEY_LENGTH = 400

# What a category expression says when the archive says nothing: an address
# with no country, an attribute with no unit. Shown as a category rather
# than dropped, because a silently missing bar is a lie about the total.
NOT_STATED = "(not stated)"


class DrilldownError(ValueError):
    """A chart id, dataset or key that does not belong to the chart. Carries
    a hint, so the API can answer {error, hint} like everything else."""

    def __init__(self, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.hint = hint


# ── The pieces a builder fills in ────────────────────────────

@dataclass(frozen=True)
class Measure:
    """One dataset that is a different AGGREGATE of the same rows.

    Datasets are usually a split - one per importance level - and then the
    series expression names them. Sometimes they are not: "market insights"
    and "of those, high relevance" count the same rows twice, and minimum,
    average and maximum are three numbers over one group. Those are
    measures, and `filter` is what the drilldown narrows the rows to when
    the reader clicks that dataset (None = all rows of the point).
    """
    id: str
    label: str
    expr: sql.Composable
    filter: sql.Composable | None = None


@dataclass
class Plan:
    """Everything one chart needs, in the form both statements read."""

    table: str
    alias: str = "t"
    # Joins written out (LEFT JOIN LATERAL ... ON true). They are part of
    # both statements, so a category that comes from a join can be drilled.
    joins: sql.Composable = field(default_factory=lambda: sql.SQL(""))
    # Scope and chart-specific predicates; identity and the window are added
    # here, never by a builder, so they cannot be forgotten.
    where: list[sql.Composable] = field(default_factory=list)
    params: dict[str, Any] = field(default_factory=dict)

    # The dataset split: an expression that yields the DATASET ID itself
    # ('rising', 'high', 'negative'), so the drilldown's predicate is a
    # plain comparison. None = one dataset for the whole chart.
    series: sql.Composable | None = None
    # Datasets that are aggregates rather than a split (see Measure).
    measures: tuple[Measure, ...] = ()

    # The sentence under the title, when the registry's cannot be right for
    # every scope. connections/matrix is the case: with an entity in hand it
    # is type against counterpart, without one it is parent against child,
    # and one sentence describing both makes the reader work out which half
    # applies to the picture in front of them. "" = keep the registry's.
    description: str = ""

    # Key charts: the x value (what the drilldown compares) and the label
    # shown for it, when they differ - "by name" keys on the entity id and
    # shows the name.
    category: sql.Composable | None = None
    category_label: sql.Composable | None = None
    # Matrix charts: the second axis.
    axis_y: sql.Composable | None = None
    axis_y_label: sql.Composable | None = None
    # What the two axes of a matrix are CALLED. The numbers table under the
    # chart is the accessible form of the bubbles, and a table whose columns
    # are headed "First axis" and "Second axis" does not say which of them
    # is the short-term value - the reader has to guess the mapping the
    # picture showed. The builder names them because the same chart can be
    # about different pairs: connections/matrix is type against counterpart
    # with a scope, parent against child without one.
    axis_titles: tuple[str, str] = ()
    # For an axis over an ordered vocabulary: the scale, in its own order.
    # () = the categories in the order the statement returned them, which is
    # the right answer for a nominal axis (entities, connection types).
    axis_order: tuple[tuple[str, ...], tuple[str, ...]] = ((), ())

    # How the categories of a key chart are ordered and cut to the top N:
    #   count  the biggest first (the default)
    #   scale  by `order_expr` - the rating scale
    #   name   alphabetically
    order: str = "count"
    order_expr: sql.Composable | None = None
    # WHICH WAY UP THE SCALE IS DRAWN, and why it is a flag and not a minus
    # sign in front of `order_expr`.
    #
    # A horizontal bar chart draws its first category at the TOP, so "good at
    # the top, bad at the bottom" means the best value must come first - and
    # the way that was said was `order_expr=-rv.float_value`. But `ord` is
    # not only the order: it is also the STEP OF THE SCALE the bar is
    # coloured by (routers/api_diagrams.py: tone_of), which the same comment
    # there promises can never disagree with the order. Negating it made
    # Excellent the colour of -3 and Bad the colour of +1 - the rating chart
    # came out with the good end red and the bad end green.
    #
    # So the value stays the value, and which end leads is said here.
    order_desc: bool = False
    # What "biggest" counts, when it is not the number of rows.
    rank_expr: sql.Composable | None = None
    # A HAVING for the group ("at least three numeric readings").
    having: sql.Composable | None = None
    limit: int = sqlbuild.KEY_CHART_LIMIT

    # "" = the palette; "colour_group" = the x value is a colour-group key
    # and the colour comes from the colour table (app/colours.py).
    colour_source: str = ""

    # CONNECTIONS ONLY: count a connection from BOTH ends.
    #
    # The archive writes one row for a pair and names both roles in it -
    # (Foxconn, Apple, "Supplier", "Customer"). Counted from the parent
    # alone, "Supplier" has a bar and "Customer" has none, which says the
    # archive knows of no customers. Both ends is the true picture and it is
    # what a reader of "connections by type" is asking for: every row is
    # one tie seen from one side, and one tie has two sides.
    #
    # It is a field on the Plan and not a join a builder writes, because the
    # rows behind the bar have to be the rows the bar counted: `side` is
    # part of the FROM (_from), so the listing, the count and the CSV all
    # see the same doubling, and each row of the listing reads as the
    # sentence its own side makes.
    both_directions: bool = False

    # THE READER'S PERSPECTIVE FILTER, stamped on by plan_for() and never by
    # a builder. It is not a chart's business: it narrows all forty-eight of
    # them the same way, and a field on the Plan is what lets ONE line in
    # _where() apply it to every chart, every drilldown and every drilldown
    # CSV without touching a single builder. "" is All, which is no filter.
    perspective: str = ""
    min_importance: str = ""

    def __post_init__(self) -> None:
        if self.order not in ("count", "scale", "name"):
            raise ValueError(f"unknown category order {self.order!r}")
        if self.order == "scale" and self.order_expr is None:
            raise ValueError("order='scale' needs an order_expr")

    def measure(self, dataset_id: str) -> Measure | None:
        for m in self.measures:
            if m.id == dataset_id:
                return m
        return None


def nz(expr: sql.Composable, text: str = NOT_STATED) -> sql.Composable:
    """`COALESCE(NULLIF(expr, ''), '(not stated)')` - a category that is
    always a string, so the chart, the axis and the drilldown key all say
    the same thing about a row the extraction left empty."""
    return sql.SQL("COALESCE(NULLIF({e}, ''), {t})").format(e=expr, t=sql.Literal(text))


def case_map(expr: sql.Composable, mapping: dict[str, str], default: str,
             prefix: str) -> tuple[sql.Composable, dict[str, Any]]:
    """`CASE WHEN expr = 'High Importance' THEN 'high' ... ELSE 'unset' END`.

    The archive stores the published wording; the chart's dataset ids are
    slugs. Mapping in SQL rather than in Python is what lets the drilldown
    compare the same expression to the dataset id it was given.

    The values are BOUND, never interpolated: they come from a vocabulary
    table in the end, and one day one of them will contain a quote.
    """
    params: dict[str, Any] = {}
    branches: list[sql.Composable] = []
    for i, (value, dataset_id) in enumerate(mapping.items()):
        name = f"{prefix}_{i}"
        params[name] = value
        branches.append(sql.SQL("WHEN {e} = %({p})s THEN {d}").format(
            e=expr, p=sql.SQL(name), d=sql.Literal(dataset_id)))
    frag = sql.SQL("CASE {b} ELSE {d} END").format(
        b=sql.SQL(" ").join(branches), d=sql.Literal(default))
    return frag, params


def source_join(alias: str = "t", out: str = "src") -> sql.Composable:
    """The document a row came from, by (task, source id).

    LATERAL with LIMIT 1 rather than a join: two rows of one task can carry
    the same source id in a re-run, and a plain join would double every
    count that passes through it.
    """
    return sql.SQL(
        " LEFT JOIN LATERAL (SELECT s.text_name, s.text_uri, s.text_summary "
        "FROM processed_data.sources s "
        "WHERE {idn} AND s.text_task_id = {a}.text_task_id "
        "AND s.text_source_id = {a}.text_fk_source_id LIMIT 1) AS {o} ON true"
    ).format(idn=sqlbuild.identity("s"), a=sql.Identifier(alias), o=sql.Identifier(out))


def entity_join(alias: str, column: str, out: str) -> sql.Composable:
    """The entity a row points at, by (task, entity id) - the only key an
    entity has (see app/scope.py)."""
    return sql.SQL(
        " LEFT JOIN LATERAL (SELECT e.text_name, e.text_type FROM processed_data.entities e "
        "WHERE {idn} AND e.text_task_id = {a}.text_task_id "
        "AND e.text_entity_id = {a}.{c} LIMIT 1) AS {o} ON true"
    ).format(idn=sqlbuild.identity("e"), a=sql.Identifier(alias),
             c=sql.Identifier(column), o=sql.Identifier(out))


def scope_where(scope, alias: str, *, entity_column: str | None = None,
                source_column: str | None = None) -> sql.Composable:
    """The scope predicate for one table.

    A row usually points at both an entity and a document, and which of the
    two the scope means depends on what was searched for. Asked about a
    SOURCE ("everything in this filing"), the document is the boundary and
    an entity that also appears elsewhere must not drag its other rows in.
    Asked about anything else, the entity is the boundary - "ratings about
    Apple" are Apple's ratings, in whichever document they were written.
    """
    if not scope.scoped:
        return sql.SQL("TRUE")
    if scope.kind == "source" and source_column:
        return scope.src_predicate(alias, source_column)
    if entity_column:
        return scope.ent_predicate(alias, entity_column)
    if source_column:
        return scope.src_predicate(alias, source_column)
    return sql.SQL("TRUE")


# ── Statements ───────────────────────────────────────────────

def _with(scope, extra: list[sql.Composable], body: sql.Composable) -> sql.Composable:
    ctes = list(scope.ctes()) if scope.scoped else []
    ctes += extra
    if not ctes:
        return body
    return sql.SQL("WITH {c} {b}").format(c=sql.SQL(", ").join(ctes), b=body)


def plan_for(spec, scope, ctx, window) -> Plan:
    """A chart's plan, with the request's perspective filter stamped on it.

    Every caller that draws a chart, lists the rows behind one of its points
    or asks what the newest row outside the window is goes through here, so
    the filter cannot be forgotten in one of the three. A builder still
    knows nothing about it - it is asked for in the top bar and applies to
    every chart identically, so it belongs on the way OUT of the builder
    rather than inside forty-eight of them.
    """
    plan = spec.builder(scope, ctx, window)
    plan.perspective = getattr(ctx, "perspective", "")
    plan.min_importance = getattr(ctx, "min_importance", "")
    return plan


def perspective_where(plan: Plan) -> sql.Composable:
    """The perspective filter for this plan's table, or TRUE.

    THE ONE CHART THAT MUST NOT FILTER ITSELF. sources/importance_by_
    perspective is drawn FROM processed_data.source_importances_by_
    perspective: applying "this document has an Investor judgement of at
    least High" to the table of judgements would leave a chart that shows
    only the bar it was already narrowed to, and its own Perspective axis
    would collapse to one category. The picture is the reader's answer to
    "what does the filter cover?", so it keeps showing all of it.

    Every other table joins the judgements by (task, source id) - the pair,
    never the source id alone, because a source id is only unique inside its
    task (app/sqlbuild.py: perspective_scope).
    """
    if plan.table == "source_importances_by_perspective":
        return sql.SQL("TRUE")
    return sqlbuild.perspective_scope(
        plan.perspective, plan.alias, sqlbuild.source_id_column(plan.table))


def _where(plan: Plan, window: Window) -> sql.Composable:
    parts = [sqlbuild.identity(plan.alias),
             sqlbuild.window_predicate(plan.table, window, plan.alias),
             perspective_where(plan)]
    parts += [p for p in plan.where if p is not None]
    return sql.SQL(" AND ").join(sql.SQL("({p})").format(p=p) for p in parts)


# ONE CONNECTION ROW, SEEN FROM ONE END - always, on every connections
# chart, so that one shape describes them all.
#
# The second branch is `WHERE FALSE` unless the chart asked for both ends
# (Plan.both_directions), so the default is exactly the row the archive
# holds and no count moves. Where it is asked for, the row comes back twice
# with its ends and its two role words swapped, which is the same tie read
# the other way round: "Foxconn is a Supplier of Apple" and "Apple is a
# Customer of Foxconn".
_SIDES = (
    " CROSS JOIN LATERAL ("
    "SELECT {a}.text_type_parent_to_child AS role, {a}.text_type_child_to_parent AS back, "
    "{a}.text_fk_parent_entity_id AS ref_id, {a}.text_fk_child_entity_id AS tgt_id "
    "UNION ALL "
    "SELECT {a}.text_type_child_to_parent, {a}.text_type_parent_to_child, "
    "{a}.text_fk_child_entity_id, {a}.text_fk_parent_entity_id WHERE {both}"
    ") AS side")

# What a connections chart groups on when it counts both ends.
SIDE_ROLE = sql.SQL("side.role")


def _sides(plan: Plan) -> sql.Composable:
    if plan.table != "connections":
        return sql.SQL("")
    return sql.SQL(_SIDES).format(
        a=sql.Identifier(plan.alias),
        both=sql.SQL("TRUE" if plan.both_directions else "FALSE"))


def _from(plan: Plan) -> sql.Composable:
    return sql.SQL("FROM {t} AS {a}{s}{j}").format(
        t=sqlbuild.table(plan.table), a=sql.Identifier(plan.alias),
        s=_sides(plan), j=plan.joins)


def _value_columns(plan: Plan) -> list[sql.Composable]:
    """`n` and then one column per measure.

    `n` is on EVERY statement: it is how many rows the group holds, which is
    what the card says ("5 rows"), what "nothing in this period" is decided
    on, and - for a chart of minima and maxima - the only number in the
    answer that is a count at all. Without it a minimum of zero would read
    as a chart with no data in it.
    """
    cols = [sql.SQL("{r} AS n").format(r=_rank(plan))]
    cols += [sql.SQL("{e} AS {n}").format(e=m.expr, n=sql.Identifier(f"m{i}"))
             for i, m in enumerate(plan.measures)]
    return cols


def _series_column(plan: Plan) -> sql.Composable:
    if plan.series is None:
        return sql.SQL("NULL::text AS series")
    return sql.SQL("({s})::text AS series").format(s=plan.series)


def params_for(plan: Plan, scope, ctx, window: Window) -> dict[str, Any]:
    """Every parameter a statement of this plan binds, in one dict."""
    return {**sqlbuild.identity_params(ctx.project, ctx.language),
            **sqlbuild.perspective_params(plan.perspective, plan.min_importance),
            **sqlbuild.window_params(window), **scope.params, **plan.params}


def time_statement(plan: Plan, scope, window: Window) -> sql.Composable:
    body = sql.SQL("SELECT {b} AS bucket, {s}, {m} {f} WHERE {w} GROUP BY 1, 2").format(
        b=sqlbuild.bucket(plan.table, window, plan.alias),
        s=_series_column(plan),
        m=sql.SQL(", ").join(_value_columns(plan)),
        f=_from(plan), w=_where(plan, window))
    return _with(scope, [], body)


def _rank(plan: Plan) -> sql.Composable:
    return plan.rank_expr if plan.rank_expr is not None else sql.SQL("count(*)")


def _order_by(plan: Plan, alias: str) -> sql.Composable:
    a = sql.Identifier(alias)
    if plan.order == "scale":
        return sql.SQL("{a}.ord {d} NULLS LAST, {a}.x ASC").format(
            a=a, d=sql.SQL("DESC" if plan.order_desc else "ASC"))
    if plan.order == "name":
        return sql.SQL("{a}.x ASC").format(a=a)
    return sql.SQL("{a}.tot DESC, {a}.x ASC").format(a=a)


def key_statement(plan: Plan, scope, window: Window) -> sql.Composable:
    """Group by category (and series), then keep the top N categories.

    The cut is made on the category's TOTAL, not on one dataset's share, so
    a split chart keeps whole bars instead of the biggest slices of
    different bars.
    """
    if plan.category is None:
        raise ValueError("a key chart needs a category")
    label = plan.category_label if plan.category_label is not None else plan.category
    order_expr = plan.order_expr if plan.order_expr is not None else sql.SQL("NULL::double precision")
    inner = sql.SQL(
        "SELECT ({c})::text AS x, min(({l})::text) AS label, {s}, "
        "min({o}) AS ord, {m} {f} WHERE {w} GROUP BY 1, 3{h}"
    ).format(c=plan.category, l=label, s=_series_column(plan),
             o=order_expr, m=sql.SQL(", ").join(_value_columns(plan)),
             f=_from(plan), w=_where(plan, window),
             h=sql.SQL(" HAVING {h}").format(h=plan.having) if plan.having is not None else sql.SQL(""))
    g = sql.SQL("g AS ({i})").format(i=inner)
    top = sql.SQL(
        "top AS (SELECT x, sum(n) AS tot, min(ord) AS ord FROM g GROUP BY x ORDER BY {o} {lim})"
    ).format(o=_order_by_inner(plan), lim=sqlbuild.limit(plan.limit))
    measures = sql.SQL("").join(
        sql.SQL(", g.{n}").format(n=sql.Identifier(f"m{i}")) for i in range(len(plan.measures)))
    body = sql.SQL(
        "SELECT g.x, g.label, g.series, g.n{mc}, top.tot, top.ord "
        "FROM g JOIN top ON top.x IS NOT DISTINCT FROM g.x ORDER BY {o}, g.series"
    ).format(mc=measures, o=_order_by(plan, "top"))
    return _with(scope, [g, top], body)


def _order_by_inner(plan: Plan) -> sql.Composable:
    """The same ordering, written for the aggregate inside `top`."""
    if plan.order == "scale":
        return sql.SQL("min(ord) {d} NULLS LAST, x ASC").format(
            d=sql.SQL("DESC" if plan.order_desc else "ASC"))
    if plan.order == "name":
        return sql.SQL("x ASC")
    return sql.SQL("sum(n) DESC, x ASC")


def matrix_statement(plan: Plan, scope, window: Window) -> sql.Composable:
    if plan.category is None or plan.axis_y is None:
        raise ValueError("a matrix needs both axes")
    xl = plan.category_label if plan.category_label is not None else plan.category
    yl = plan.axis_y_label if plan.axis_y_label is not None else plan.axis_y
    body = sql.SQL(
        "SELECT ({x})::text AS x, min(({xl})::text) AS x_label, ({y})::text AS y, "
        "min(({yl})::text) AS y_label, count(*) AS n {f} WHERE {w} "
        "GROUP BY 1, 3 ORDER BY n DESC, x, y {lim}"
    ).format(x=plan.category, xl=xl, y=plan.axis_y, yl=yl,
             f=_from(plan), w=_where(plan, window), lim=sqlbuild.limit(plan.limit))
    return _with(scope, [], body)


def chart_statement(kind: str, plan: Plan, scope, window: Window) -> sql.Composable:
    if kind == "time":
        return time_statement(plan, scope, window)
    if kind == "key":
        return key_statement(plan, scope, window)
    if kind == "matrix":
        return matrix_statement(plan, scope, window)
    raise ValueError(f"unknown chart kind {kind!r}")


# ── One clicked point ────────────────────────────────────────

def normalise_key(drill: str, key: dict[str, Any] | None) -> dict[str, Any]:
    """The key a chart of this kind accepts, checked and cleaned.

    Refused rather than ignored: a drilldown that quietly drops the half of
    the key it did not understand answers with the rows of the whole chart
    and looks like it worked.
    """
    key = dict(key or {})
    required = {"bucket": ("bucket",), "x": ("x",), "xy": ("x", "y")}.get(drill)
    if required is None:
        raise DrilldownError(f"unknown drilldown key shape {drill!r}",
                             "one of bucket, x, xy")
    extra = sorted(set(key) - set(required))
    if extra:
        raise DrilldownError(f"the key carries {', '.join(extra)}, which this chart has no axis for",
                             f"this chart's key is {' and '.join(required)}")
    out: dict[str, Any] = {}
    for name in required:
        value = key.get(name)
        if value is None or str(value).strip() == "":
            raise DrilldownError(f"the key is missing {name}",
                                 f"this chart's key is {' and '.join(required)}")
        text = str(value).strip()
        if len(text) > MAX_KEY_LENGTH:
            raise DrilldownError(f"the key's {name} is too long",
                                 f"at most {MAX_KEY_LENGTH} characters")
        if name == "bucket":
            out[name] = parse_bucket(text)
        else:
            out[name] = text
    return out


def parse_bucket(value: str) -> datetime:
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise DrilldownError(f"{value!r} is not a period start",
                             "send the bucket exactly as the chart returned it "
                             "(ISO 8601, for example 2026-03-09T00:00:00+00:00)") from None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def key_predicates(plan: Plan, kind: str, window: Window,
                   key: dict[str, Any]) -> tuple[list[sql.Composable], dict[str, Any]]:
    """The clicked point, as predicates on the same expressions the chart
    grouped by."""
    parts: list[sql.Composable] = []
    params: dict[str, Any] = {}
    if kind == "time":
        parts.append(sql.SQL("{b} = %(dd_bucket)s").format(
            b=sqlbuild.bucket(plan.table, window, plan.alias)))
        params["dd_bucket"] = key["bucket"]
        # AND THE SAME BUCKET AS A RANGE, which is the only half of it the
        # database can act on. `date_trunc(unit, timeline) = x` is a function
        # of the column, so it excludes no chunk: a click on one day of a
        # 30-day chart made TimescaleDB decompress and sort all thirty before
        # it could drop twenty-nine of them - 90 seconds for a page of
        # twenty rows on a whole-project chart. The half-open range says
        # exactly the same thing about an aligned bucket and IS sargable, so
        # the click reads one chunk.
        #
        # Both are kept rather than one swapped for the other: a bucket that
        # is not aligned to the unit would match nothing under the equality
        # and a shifted range under the range alone, and the answer has to
        # be the one the chart drew.
        parts.append(sql.SQL("{tl} >= %(dd_bucket)s AND {tl} < %(dd_bucket_end)s").format(
            tl=sqlbuild.timeline(plan.table, plan.alias)))
        params["dd_bucket_end"] = add_units(key["bucket"], window.unit, 1)
        # The chunk prefilter, where the table admits one: a row is added no
        # earlier than the moment it is about (sqlbuild.NO_PREFILTER names
        # the tables where that is not true, and they get nothing here).
        if plan.table not in sqlbuild.NO_PREFILTER:
            parts.append(sql.SQL("{a}.date_added >= %(dd_bucket)s").format(
                a=sql.Identifier(plan.alias)))
    else:
        parts.append(sql.SQL("({c})::text = %(dd_x)s").format(c=plan.category))
        params["dd_x"] = key["x"]
        if kind == "matrix":
            parts.append(sql.SQL("({y})::text = %(dd_y)s").format(y=plan.axis_y))
            params["dd_y"] = key["y"]
    return parts, params


def dataset_predicate(plan: Plan, dataset_id: str,
                      allowed: tuple[str, ...]) -> tuple[sql.Composable | None, dict[str, Any]]:
    """What the clicked DATASET narrows the point to."""
    if dataset_id and allowed and dataset_id not in allowed:
        raise DrilldownError(f"this chart has no dataset {dataset_id!r}",
                             "one of " + ", ".join(allowed))
    measure = plan.measure(dataset_id)
    if measure is not None:
        return measure.filter, {}
    if plan.series is not None and dataset_id:
        return sql.SQL("({s})::text = %(dd_series)s").format(s=plan.series), {"dd_series": dataset_id}
    return None, {}


# ── The rows themselves ──────────────────────────────────────
#
# ONE SHAPE PER TABLE, AND IT IS A TABLE OF COLUMNS.
#
# A drilldown is a real table and every row of it has the same shape:
#
#   running number | open the source | the source's domain | the entity |
#   the subject columns | date | the free text, wide
#
# That shape is better than a paragraph per row. The first link is text,
# not a bare icon: three little glyphs say nothing about where a row came
# from, and a person scanning twenty of them needs to see the domain and
# the title without hovering.
#
# THERE IS NO ID COLUMN. `src:battery`, `ent:1822949` and a content hash
# identify a row to a machine and say nothing to a reader.

_ROW_COLUMNS: dict[str, str] = {
    "sources": "{a}.text_name AS name, {a}.text_uri AS uri, {a}.text_type AS type, "
               "{a}.text_importance AS importance, {a}.bool_trustful AS trustful, "
               "{a}.text_summary AS summary",
    "entities": "{a}.text_name AS name, {a}.text_entity_id AS entity_id, {a}.text_type AS type, "
                "{a}.text_description AS description, {a}.text_relation_to_source AS relation",
    "locations": "{a}.text_name AS name, {a}.text_address AS address, {a}.text_type AS type, "
                 "{a}.text_fk_entity_id AS entity_id, {a}.float_latitude AS lat, "
                 "{a}.float_longitude AS lng, {a}.text_relation_to_source AS relation",
    "events": "{a}.text_name AS name, {a}.text_type AS type, {a}.text_description AS description, "
              "{a}.date_eventdate AS eventdate, {a}.text_relation_to_source AS relation",
    "ratings": "{a}.text_rating_name AS rating_name, {a}.text_rating_value AS value, "
               "{a}.text_perspective AS perspective, {a}.text_fk_entity_id AS entity_id, "
               "{a}.text_reason AS reason, {a}.text_relation_to_source AS relation",
    "connections": "{a}.side_role AS type, {a}.side_back AS reverse_type, "
                   "{a}.text_reason AS reason, {a}.text_relation_to_source AS relation",
    "attributes": "{a}.text_name AS name, {a}.text_type AS type, {a}.text_unit AS unit, "
                  "{a}.text_value AS value, {a}.float_value AS number, {a}.text_reason AS reason, "
                  "{a}.text_fk_entity_id AS entity_id, {a}.text_relation_to_source AS relation",
    "market_insights": "{a}.text_topic AS topic, {a}.text_short_term_outlook AS short_outlook, "
                       "{a}.text_long_term_outlook AS long_outlook, "
                       "{a}.text_short_term_sentiment AS short_sentiment, "
                       "{a}.text_long_term_sentiment AS long_sentiment, "
                       "{a}.bool_high_relevance AS high_relevance, "
                       "{a}.text_reason AS reason, "
                       "{a}.text_fk_entity_id AS entity_id, {a}.text_relation_to_source AS relation",
    "source_importances_by_perspective":
        "{a}.text_perspective AS perspective, {a}.text_importance AS importance, "
        "{a}.text_reason AS reason, {a}.text_fk_source_id AS source_id",
}


@dataclass(frozen=True)
class Column:
    """One column of the drilldown table."""
    key: str
    label: str
    # The free text - a summary, a reason, a description. Last and widest,
    # shown two lines deep with a control that opens the rest in place
    # (static/js/drilldown.js). Exactly one per table.
    wide: bool = False
    # "date" is rendered as a <time>; everything else is words, with the
    # colour beside them where the value is on a scale.
    kind: str = "text"
    # What goes between two words in ONE cell. The short-term and the
    # long-term reading of a market insight are written as "Rising /
    # Neutral" in a single column, which is what makes the two readable as
    # one horizon pair rather than four columns to compare across; the
    # entities an event names are a list, and read as one.
    sep: str = ", "
    # WHAT AN ABBREVIATION IN THE HEADING MEANS. "Outlook ST / LT" is two
    # readings of one insight and four letters nobody is born knowing; the
    # words are two characters longer than the column is wide, so they go in
    # an ⓘ beside the heading instead of into it.
    hint: str = ""


# THE COLUMNS PER TAB, with the date second to last and the free text last
# and widest. The trust flag on a document and the relation on a location
# are columns too, because the row's facts show them.
COLUMNS: dict[str, tuple[Column, ...]] = {
    "sources": (
        Column("importance", "General importance"),
        Column("trust", "Trust"),
        Column("type", "Type"),
        Column("date", "Date", kind="date"),
        Column("text", "Summary", wide=True),
    ),
    "entities": (
        Column("entity", "Entity"),
        Column("type", "Type"),
        Column("relation", "Relation"),
        Column("date", "Date", kind="date"),
        Column("text", "Description", wide=True),
    ),
    "locations": (
        Column("name", "Name"),
        Column("type", "Type"),
        Column("relation", "Relation"),
        Column("address", "Address"),
        # Kept from the row's own facts: a place with coordinates is one
        # the map can draw, and a reader comparing twenty rows can see
        # which of them the archive placed.
        Column("coordinates", "Coordinates"),
        Column("date", "Date", kind="date"),
        Column("text", "Summary", wide=True),
    ),
    "events": (
        Column("type", "Type"),
        Column("eventdate", "Event date", kind="date"),
        Column("entities", "Entities"),
        Column("relation", "Relation"),
        Column("date", "Date", kind="date"),
        Column("text", "Description", wide=True),
    ),
    "ratings": (
        Column("value", "Value"),
        Column("rating", "Rating"),
        Column("perspective", "Perspective"),
        Column("relation", "Relation"),
        Column("date", "Date", kind="date"),
        Column("text", "Reason", wide=True),
    ),
    # THE THREE COLUMNS ARE A SENTENCE WITH A HOLE IN IT, and the value
    # fills the hole: "Nordwyk | is a Ownership of | Nordwyk Pumps Asia".
    # A header saying "Connection" over a cell saying "Ownership" leaves a
    # reader to work out which end owns which - so the two names sit on
    # either side of the word that ties them, in the order the word reads.
    "connections": (
        Column("reference", "Reference"),
        Column("connection", "is a ... of",
               hint="The role the Reference plays towards the Target. Read the "
                    "row as a sentence: Reference is a <value> of Target."),
        Column("target", "Target"),
        # The archive records both directions and they are not always each
        # other's mirror ("Supplier" / "Customer"); this dashboard has
        # always shown the second one.
        Column("reverse", "The other way round"),
        Column("relation", "Relation"),
        Column("date", "Date", kind="date"),
        Column("text", "Reason", wide=True),
    ),
    "attributes": (
        Column("attribute", "Name: value unit"),
        Column("type", "Type"),
        # The number the extraction managed to read out of the text, where
        # it did: "about four hundred" and 400 are two different facts and
        # only one of them can be added up.
        Column("number", "Number"),
        Column("relation", "Relation"),
        Column("date", "Date", kind="date"),
        Column("text", "Reason", wide=True),
    ),
    "market_insights": (
        Column("topic", "Topic"),
        Column("relevance", "Relevance"),
        Column("outlook", "Outlook ST / LT", sep=" / ",
               hint="Which way the reporting pointed. ST is the short term and LT the "
                    "long term, in that order."),
        Column("sentiment", "Sentiment ST / LT", sep=" / ",
               hint="How strongly the reporting read. ST is the short term and LT the "
                    "long term, in that order."),
        Column("relation", "Relation"),
        Column("date", "Date", kind="date"),
        Column("text", "Reason", wide=True),
    ),
    "source_importances_by_perspective": (
        Column("perspective", "Perspective"),
        Column("importance", "Importance"),
        Column("date", "Date", kind="date"),
        Column("text", "Reason", wide=True),
    ),
}

# A table nobody has written columns for still lists its rows rather than
# answering with an empty dialog.
_FALLBACK_COLUMNS: tuple[Column, ...] = (
    Column("name", "Name"), Column("date", "Date", kind="date"),
    Column("text", "Detail", wide=True),
)


def columns_for(table: str) -> tuple[Column, ...]:
    return COLUMNS.get(table, _FALLBACK_COLUMNS)


def columns_json(table: str) -> list[dict[str, Any]]:
    return [{"key": c.key, "label": c.label, "wide": c.wide, "kind": c.kind,
             "sep": c.sep, "hint": c.hint}
            for c in columns_for(table)]


# WHICH TABLES GET THE THIRD LINK - the entity's own diagrams - AS A COLUMN
# OF ITS OWN. The others reach the same page through a cell that already
# carries a name: the Entity column of an entity row, the Reference and
# the Target of a connection, the entities an event names. A document has
# no single entity and has no such link.
ENTITY_LINK_TABLES = ("locations", "ratings", "attributes", "market_insights")


def row_links(table: str) -> dict[str, bool]:
    return {"source": True, "entity": table in ENTITY_LINK_TABLES}


# ── Colour: which scale a value is on, and where on it ───────
#
# The three scales are defined once, in app/static/css/app.css, and read
# from there by static/js/palette.js. NOTHING HERE IS A COLOUR: a cell
# carries the SCALE and the STEP, and the page turns the pair into the hex
# out of the stylesheet. A hex written here would be the second copy of the
# design system that the whole colour system exists to end.
#
# THE WORD ALWAYS STAYS IN THE CELL. The colour is a swatch beside it, so
# the table still reads with the colour removed - on a printer, to a
# dichromat, and to anyone reading the numbers rather than the picture.
VALENCE = "valence"
RELEVANCE = "relevance"
# Absence: "Unset", an unstated value, a document nobody marked. Not a step
# of any scale, and grey means nothing else.
ABSENT = "none"

# The two closed vocabularies that carry no number of their own. The
# published wording is the same wording app/charts/__init__.py maps for the
# charts (OUTLOOK_VALUES, SENTIMENT_VALUES), and it stays English: these
# vocabularies are seeded under the empty project by
# database/init/02-vocabularies.sql and are not translated.
SENTIMENT_STEPS: dict[str, int] = {
    "Very Negative": -3, "Negative": -2, "Slightly Negative": -1, "Neutral": 0,
    "Slightly Positive": 1, "Positive": 2, "Very Positive": 3,
}
# A direction the reporting pointed, not a verdict, so it takes ±2 rather
# than the ends of the scale - the same two steps the outlook charts use.
OUTLOOK_STEPS: dict[str, int] = {"Declining": -2, "Neutral": 0, "Rising": 2}

# Trust: trusted green, not trusted red - at the GENTLEST step of the ramp.
#
# It was ±2, which is a rating's own weight. A trust flag is a yes/no the
# extraction set, not a seven-step reading, and beside a rating on the same
# screen it was shouting as loudly. ±1 is plainly the same scale and plainly
# the lighter of the two; the "trustful / not trustful" chart on the Sources
# tab uses the same step, so a document that is green here is green there
# (app/charts/__init__.py: the colour map).
TRUST_STEP = 1



# The same text app/sqlbuild.py hands to PostgreSQL, so the two agree on
# what an identifier looks like.
_MACHINE_NAME = re.compile(sqlbuild.MACHINE_NAME_REGEX, re.I)


def readable_title(name: str, uri: str) -> str:
    """The document's own title where there is one, otherwise something a
    person can read - never an identifier.

    MEASURED ON A REAL ARCHIVE, WHICH IS WHY THIS EXISTS: `sources.text_name`
    can hold `src_1416664` in some rows and a 32-character checksum in
    others, for hundreds of thousands of them. Printing that field is
    faithful and useless - the reader sees a hash where they expect a
    headline.

    So the name is used only when it looks like a name. Otherwise the URL's
    own slug is turned back into words - `/2026/article/mondiaux-de-piste-x9f`
    becomes "Mondiaux de piste" - and if even that yields nothing, the domain
    stands alone rather than a fabricated title.

    >>> readable_title("Apple beats estimates", "https://x.test/a")
    'Apple beats estimates'
    >>> readable_title("src_1416664", "https://www.rts.ch/sport/2026/article/mondiaux-de-piste-a1b2c3")
    'Mondiaux de piste'
    >>> readable_title("8846ebcc63ff6a49245ef85e08e776de", "https://n.test/news/12345")
    ''
    >>> readable_title("", "")
    ''
    """
    name = (name or "").strip()
    if name and not _MACHINE_NAME.match(name):
        return name
    for part in reversed([p for p in urlsplit(uri or "").path.split("/") if p]):
        stem = re.sub(r"\.(html?|php|aspx?|jsp)$", "", part, flags=re.I)
        words = [w for w in re.split(r"[-_+]", stem) if w]
        # Drop the id a slug carries AT THE END, and only there: "s12" in
        # "galaxy-tab-s12-ultra" is part of the name, "a1b2" in
        # "mondiaux-de-piste-a1b2" is not, and position is what tells them
        # apart - not the shape of the token.
        while len(words) > 1 and re.fullmatch(r"\d+|[0-9a-f]{4,}|\w*\d\w*", words[-1], re.I):
            words.pop()
        if len(words) >= 2:
            text = " ".join(words)
            return text[:1].upper() + text[1:]
    return ""


def _part(text: Any, scale: str = "", step: Any = None,
          entity: str = "", iso: str = "") -> dict[str, Any]:
    """One thing inside a cell: a word, and what colours it.

    `entity` marks the word as the name of an entity, so the page can make
    it the link to that entity's own diagrams. The URL is built there, not
    here: the page is what knows the project and the language.
    """
    part: dict[str, Any] = {"text": "" if text is None else str(text)}
    if scale:
        part["scale"] = scale
    if step is not None:
        part["step"] = step
    if entity:
        part["entity"] = entity
    if iso:
        part["iso"] = iso
    return part


def _text(value: Any) -> list[dict[str, Any]]:
    return [_part(value)]


def _date(value: Any) -> list[dict[str, Any]]:
    if not value:
        return [_part("")]
    return [_part(value.date().isoformat() if hasattr(value, "date") else value,
                  iso=value.isoformat() if hasattr(value, "isoformat") else str(value))]


def _scaled(value: Any, steps: dict[str, int], scale: str = VALENCE) -> list[dict[str, Any]]:
    """A word out of a closed vocabulary, with its step - or grey where the
    vocabulary says the source said nothing."""
    word = "" if value is None else str(value)
    if not word:
        return [_part("")]
    if word in steps:
        return [_part(word, scale, steps[word])]
    return [_part(word, ABSENT)]


def _number_scaled(value: Any, number: Any, scale: str) -> list[dict[str, Any]]:
    """A word whose step is a NUMBER THE ARCHIVE CARRIES -
    `rating_values.float_value` (-3 … +3) and `importance_types.float_value`
    (0 … 4), joined in by _row_extra_joins. A value the vocabulary does not
    know keeps its word and loses its colour, rather than being drawn at
    some default step that means something else."""
    word = "" if value is None else str(value)
    if not word:
        return [_part("")]
    if number is None:
        return [_part(word, ABSENT)]
    return [_part(word, scale, int(round(float(number))))]


def _entity_part(name: Any, fallback: Any = "") -> dict[str, Any]:
    """An entity's name, as the link to its own diagrams.

    The name and NOT the id: /diagrams/entity resolves a term against
    `entities.text_name` (app/scope.py), so a link carrying `ent:1822949`
    would land on a page that finds nothing. The id is shown to nobody.
    """
    text = str(name or "")
    return _part(text or str(fallback or ""), entity=text)


def host_of(uri: Any) -> str:
    """The domain of a document's URI, for the text of its link and for the
    link to that domain's own diagrams. `www.` is dropped: it is noise in a
    column that has to be scanned twenty rows at a time, and
    /diagrams/source matches the host as a substring either way."""
    text = str(uri or "").strip()
    if not text:
        return ""
    try:
        host = urlsplit(text).hostname or ""
    except ValueError:
        return ""
    host = host.lower()
    return host[4:] if host.startswith("www.") else host


def shape_row(table: str, row: dict[str, Any]) -> dict[str, Any]:
    """One archive row as the table shows it: the document it came from, the
    entity it is about, and one cell per column of this table.

    EVERY VALUE IS DECODED ON THE WAY IN. A crawled page says
    `W&auml;rtsil&auml; Corporation` and the archive stores what the page
    said; this is the one funnel every drilldown row passes through, so it is
    the one place that has to remember (app/textclean.py).
    """
    row = {k: plain(v) for k, v in row.items()}
    cells: dict[str, list[dict[str, Any]]] = {}
    entity = str(row.get("entity_name") or "")

    if table == "sources":
        cells = {
            "importance": _number_scaled(row.get("importance"), row.get("importance_value"), RELEVANCE),
            # Two steps of the valence scale, and the word beside each of
            # them: a document the archive marked trustworthy, and one it
            # did not.
            "trust": [_part("Trusted", VALENCE, TRUST_STEP)] if row.get("trustful")
                     else [_part("Not trusted", VALENCE, -TRUST_STEP)],
            "type": _text(row.get("type")),
            "date": _date(row.get("row_date")),
            "text": _text(row.get("summary")),
        }
    elif table == "entities":
        entity = str(row.get("name") or "")
        cells = {
            "entity": [_entity_part(row.get("name"))],
            "type": _text(row.get("type")),
            "relation": _text(row.get("relation")),
            "date": _date(row.get("row_date")),
            "text": _text(row.get("description")),
        }
    elif table == "locations":
        cells = {
            "name": _text(row.get("name")),
            "type": _text(row.get("type")),
            "relation": _text(row.get("relation")),
            "address": _text(row.get("address")),
            "coordinates": _text(
                f"{row['lat']:.4f}, {row['lng']:.4f}"
                if row.get("lat") is not None and row.get("lng") is not None else ""),
            "date": _date(row.get("row_date")),
            # The document's summary: a location row's own description is
            # usually empty, and the sentence that explains why the place is
            # in the archive is the document's.
            "text": _text(row.get("source_summary")),
        }
    elif table == "events":
        cells = {
            "type": _text(row.get("type")),
            # An event with no date of its own sits on its DOCUMENT's date
            # (app/charts/events.py), which is a fact about the row rather
            # than a gap - and the Date column beside this one is that date.
            "eventdate": _date(row.get("eventdate")) if row.get("eventdate")
                         else [_part("From the document", ABSENT)],
            # An event with no entity rows is about the document as a whole
            # (app/charts/events.py), which is a fact about it rather than a
            # gap - so the cell says so instead of standing empty.
            "entities": ([_entity_part(name) for name in (row.get("entity_names") or [])]
                         or [_part("About the document")]),
            "relation": _text(row.get("relation")),
            "date": _date(row.get("row_date")),
            "text": _text(row.get("description")),
        }
    elif table == "ratings":
        cells = {
            "value": _number_scaled(row.get("value"), row.get("rating_value"), VALENCE),
            "rating": _text(row.get("rating_name")),
            "perspective": _text(row.get("perspective")),
            "relation": _text(row.get("relation")),
            "date": _date(row.get("row_date")),
            "text": _text(row.get("reason")),
        }
    elif table == "connections":
        cells = {
            "reference": [_entity_part(row.get("reference_name"))],
            "connection": _text(row.get("type")),
            "target": [_entity_part(row.get("target_name"))],
            "reverse": _text(row.get("reverse_type")),
            "relation": _text(row.get("relation")),
            "date": _date(row.get("row_date")),
            "text": _text(row.get("reason")),
        }
        entity = str(row.get("reference_name") or "")
    elif table == "attributes":
        value = str(row.get("value") or "")
        unit = str(row.get("unit") or "")
        name = str(row.get("name") or "")
        cells = {
            "attribute": _text(f"{name}: {value} {unit}".strip()),
            "type": _text(row.get("type")),
            "number": _text("" if row.get("number") is None else row["number"]),
            "relation": _text(row.get("relation")),
            "date": _date(row.get("row_date")),
            "text": _text(row.get("reason")),
        }
    elif table == "market_insights":
        cells = {
            "topic": _text(row.get("topic")),
            # `bool_high_relevance` is the yes half of a yes/no, so it takes
            # the top step of the violet ramp and its no the palest - the
            # same reading "Critical Importance" gets on the Sources tab.
            "relevance": [_part("High relevance", RELEVANCE, 4)] if row.get("high_relevance")
                         else [_part("Not marked", RELEVANCE, 0)],
            "outlook": (_scaled(row.get("short_outlook"), OUTLOOK_STEPS)
                        + _scaled(row.get("long_outlook"), OUTLOOK_STEPS)),
            "sentiment": (_scaled(row.get("short_sentiment"), SENTIMENT_STEPS)
                          + _scaled(row.get("long_sentiment"), SENTIMENT_STEPS)),
            "relation": _text(row.get("relation")),
            "date": _date(row.get("row_date")),
            "text": _text(row.get("reason")),
        }
    elif table == "source_importances_by_perspective":
        cells = {
            "perspective": _text(row.get("perspective")),
            "importance": _number_scaled(row.get("importance"), row.get("importance_value"), RELEVANCE),
            "date": _date(row.get("row_date")),
            "text": _text(row.get("reason")),
        }
    else:
        cells = {"name": _text(row.get("name")), "date": _date(row.get("row_date")),
                 "text": _text(row.get("description") or row.get("reason"))}

    uri = str(row.get("source_uri") or "")
    return {
        # WHERE THE ROW CAME FROM, AS TEXT: "news.example.com" and the
        # document's own title, not an icon and not an id. The domain is
        # also the second link - that host's own diagrams - and the entity
        # is the third.
        "link": {"uri": uri, "domain": host_of(uri),
                 "title": readable_title(str(row.get("source_name") or ""), uri)},
        "entity": entity,
        "cells": {c.key: cells.get(c.key, [_part("")]) for c in columns_for(table)},
    }


def csv_columns(table: str) -> list[str]:
    """The header of the file behind the dialog: the same columns, plus the
    document the row came from. `source_uri` keeps its name, because a link
    copied out of one export has to work in the other."""
    return (["row", "domain", "source", "source_uri", "entity"]
            + [c.key for c in columns_for(table)])


def csv_row(table: str, row: dict[str, Any], number: int) -> dict[str, str]:
    """One shaped row, flattened - the words only, never the colours: a file
    carries the value, and the value is the word."""
    item = shape_row(table, row)
    out = {"row": str(number), "domain": item["link"]["domain"],
           "source": item["link"]["title"], "source_uri": item["link"]["uri"],
           "entity": item["entity"]}
    for column in columns_for(table):
        parts = item["cells"].get(column.key) or []
        # The cell's own separator, so "Rising / Neutral" is two horizons in
        # the file exactly as it is two horizons on the screen.
        out[column.key] = column.sep.join(p["text"] for p in parts if p["text"])
    return out


def _importance_join(alias: str, column: str, out: str = "ddimp") -> sql.Composable:
    """`importance_types.float_value` for one importance level - 0 … 4, the
    step on the relevance ramp. LATERAL with LIMIT 1 for the reason
    charts/ratings.py gives: the vocabulary is seeded under the empty
    project and a customer may add a row of their own, and two matching
    rows would list every document twice."""
    return sql.SQL(
        " LEFT JOIN LATERAL (SELECT it.float_value FROM processed_data.importance_types it "
        "WHERE it.text_name = {a}.{c} AND {vs} "
        "ORDER BY (it.text_project <> '') DESC LIMIT 1) AS {o} ON true"
    ).format(a=sql.Identifier(alias), c=sql.Identifier(column),
             vs=sqlbuild.vocabulary_scope("it"), o=sql.Identifier(out))


def _rating_value_join(alias: str, out: str = "ddrv") -> sql.Composable:
    """`rating_values.float_value` - -3 … +3, the step on the valence scale.
    The number the archive carries, never the label: a value the vocabulary
    does not know keeps its word and loses its colour."""
    return sql.SQL(
        " LEFT JOIN LATERAL (SELECT rv.float_value FROM processed_data.rating_values rv "
        "WHERE rv.text_name = {a}.text_rating_value AND {vs} "
        "ORDER BY (rv.text_project <> '') DESC LIMIT 1) AS {o} ON true"
    ).format(a=sql.Identifier(alias), vs=sqlbuild.vocabulary_scope("rv"), o=sql.Identifier(out))


def _event_entities_join(alias: str, out: str = "ddev") -> sql.Composable:
    """The entities one event names, in one string.

    Aggregated inside the LATERAL rather than joined: an event about three
    entities is still ONE event, and a join would list it three times.
    """
    return sql.SQL(
        " LEFT JOIN LATERAL (SELECT array_agg(DISTINCT dde.text_name) AS names "
        "FROM processed_data.event_entities ddee "
        "JOIN processed_data.entities dde ON {idn_e} AND dde.text_task_id = ddee.text_task_id "
        "AND dde.text_entity_id = ddee.text_fk_entity_id "
        "WHERE {idn} AND ddee.text_task_id = {a}.text_task_id "
        "AND ddee.bigint_fk_event_id = {a}.bigint_id) AS {o} ON true"
    ).format(idn=sqlbuild.identity("ddee"), idn_e=sqlbuild.identity("dde"),
             a=sql.Identifier(alias), o=sql.Identifier(out))


def _row_extra_joins(table: str, alias: str) -> sql.Composable:
    """The names and the numbers a row needs to read as a sentence: its
    document, the entities it points at, and the two vocabulary tables that
    carry the step of a scale."""
    # A source row IS the document: it carries the name and the URI itself,
    # and its id column is text_source_id, not a foreign key to one.
    parts = [] if table == "sources" else [source_join(alias, "ddsrc")]
    if table in ("locations", "ratings", "attributes", "market_insights"):
        parts.append(entity_join(alias, "text_fk_entity_id", "ddent"))
    if table == "connections":
        parts.append(entity_join(alias, "side_ref", "ddref"))
        parts.append(entity_join(alias, "side_tgt", "ddtgt"))
    if table == "sources":
        parts.append(_importance_join(alias, "text_importance"))
    if table == "source_importances_by_perspective":
        parts.append(_importance_join(alias, "text_importance"))
    if table == "ratings":
        parts.append(_rating_value_join(alias))
    if table == "events":
        parts.append(_event_entities_join(alias))
    return sql.SQL("").join(parts)


def _row_extra_columns(table: str, alias: str) -> sql.Composable:
    parts = [sql.SQL("{a}.text_name AS source_name, {a}.text_uri AS source_uri, "
                     "{a}.text_summary AS source_summary").format(a=sql.Identifier(alias))
             if table == "sources" else
             sql.SQL("ddsrc.text_name AS source_name, ddsrc.text_uri AS source_uri, "
                     "ddsrc.text_summary AS source_summary")]
    if table in ("locations", "ratings", "attributes", "market_insights"):
        parts.append(sql.SQL("ddent.text_name AS entity_name"))
    if table == "connections":
        parts.append(sql.SQL("ddref.text_name AS reference_name, ddtgt.text_name AS target_name"))
    if table in ("sources", "source_importances_by_perspective"):
        parts.append(sql.SQL("ddimp.float_value AS importance_value"))
    if table == "ratings":
        parts.append(sql.SQL("ddrv.float_value AS rating_value"))
    if table == "events":
        parts.append(sql.SQL("ddev.names AS entity_names"))
    return sql.SQL(", ").join(parts)


def _point_where(plan: Plan, kind: str, window: Window, key: dict[str, Any],
                 dataset_id: str,
                 allowed_datasets: tuple[str, ...]) -> tuple[sql.Composable, dict[str, Any]]:
    """The chart's own WHERE, plus the clicked point and its dataset. Shared
    by the listing and by the count, so the two cannot answer about
    different rows."""
    parts, params = key_predicates(plan, kind, window, key)
    dataset_where, dataset_params = dataset_predicate(plan, dataset_id, allowed_datasets)
    if dataset_where is not None:
        parts.append(dataset_where)
    params.update(dataset_params)
    return sql.SQL(" AND ").join(
        [sql.SQL("({w})").format(w=_where(plan, window))] +
        [sql.SQL("({p})").format(p=p) for p in parts]), params


def _side_columns(plan: Plan) -> sql.Composable:
    """The end of the connection this row is read from, carried out of the
    page-sized inner query.

    `SELECT t.*` is the whole of the inner select, and `side` is not part of
    `t` - so without this the listing would show the parent's role under
    every bar, including the bars a child's role was counted into.
    """
    if plan.table != "connections":
        return sql.SQL("")
    return sql.SQL(", side.role AS side_role, side.back AS side_back, "
                   "side.ref_id AS side_ref, side.tgt_id AS side_tgt")


def rows_statement(plan: Plan, kind: str, scope, window: Window,
                   key: dict[str, Any], dataset_id: str,
                   allowed_datasets: tuple[str, ...],
                   with_total: bool = True) -> tuple[sql.Composable, dict[str, Any]]:
    """The listing behind one point: the chart's own FROM and WHERE, plus
    the point's key and the dataset's filter.

    HOW MANY THERE ARE IN ALL rides on the same statement by default.
    `count(*) OVER ()` is evaluated before LIMIT, so every row of the page
    carries the total the filters match - which is what the dialog's "Row
    87-106 of 312" is read from, at no extra cost when the point is small.

    `with_total=False` IS FOR A POINT THAT IS NOT SMALL, and the difference
    is not marginal. Every row of this listing carries its document's name
    and address, looked up per row (_row_extra_joins), and each of those
    lookups walks a hypertable of hundreds of chunks. With the LIMIT alone
    the loop stops after a page of them; `count(*) OVER ()` has to see every
    matching row first, so it runs the lookup for all of them. Measured on
    one bar of a whole-project chart - 881 rows behind it - that is 90
    seconds against six, for a page of twenty. A caller that expects a broad
    point asks for the number with count_statement() instead: the same
    predicates without the per-row joins, which took 0.03 s for the same bar.
    """
    table = plan.table
    where, params = _point_where(plan, kind, window, key, dataset_id, allowed_datasets)
    timeline = sqlbuild.timeline(plan.table, plan.alias)
    total = sql.SQL(", count(*) OVER () AS dd_total") if with_total else sql.SQL("")
    alias = sql.Identifier(plan.alias)
    # THE PAGE IS CUT BEFORE THE NAMES ARE LOOKED UP, and that is the whole
    # shape of this statement.
    #
    # Every row of a listing carries its document's name and address, and
    # each of those is a lookup into a hypertable of hundreds of chunks
    # (_row_extra_joins). Joined in the same SELECT as the LIMIT they ran
    # for every row the filters matched, not for the twenty on screen: one
    # bar of a whole-project chart is 881 rows, and the page took minutes.
    #
    # So the inner query is the chart's own FROM and WHERE, ordered and cut
    # to the page; the per-row lookups hang off THAT. The chart's own joins
    # (plan.joins) stay inside, because some charts group - and therefore
    # filter - on them. It is aliased back to the plan's alias, so the
    # columns and joins below read the same as they always did.
    inner = sql.SQL(
        "SELECT {a}.*{side}, {tl} AS row_date{total} {f} WHERE {w} "
        "ORDER BY {tl} DESC, {a}.bigint_id DESC LIMIT %(dd_limit)s OFFSET %(dd_offset)s"
    ).format(a=alias, side=_side_columns(plan), tl=timeline, total=total,
             f=_from(plan), w=where)
    body = sql.SQL(
        "SELECT {cols}, {extra}, {a}.row_date{total_out} FROM ({inner}) AS {a}{j} "
        "ORDER BY {a}.row_date DESC, {a}.bigint_id DESC"
    ).format(cols=sql.SQL(_ROW_COLUMNS.get(table, "{a}.text_name AS name")).format(a=alias),
             extra=_row_extra_columns(table, plan.alias), a=alias,
             total_out=sql.SQL(", {a}.dd_total").format(a=alias) if with_total else sql.SQL(""),
             inner=inner, j=_row_extra_joins(table, plan.alias))
    return _with(scope, [], body), params


def count_statement(plan: Plan, kind: str, scope, window: Window,
                    key: dict[str, Any], dataset_id: str,
                    allowed_datasets: tuple[str, ...]) -> tuple[sql.Composable, dict[str, Any]]:
    """How many rows are behind one point, and nothing else.

    The same predicates the listing runs, and the chart's own joins - which
    some charts group by and therefore filter on - but NOT the per-row
    lookups that give a row its document's name. Those are what a listing
    needs and a count never does; leaving them out is the whole reason this
    statement exists (see rows_statement).
    """
    where, params = _point_where(plan, kind, window, key, dataset_id, allowed_datasets)
    return _with(scope, [], sql.SQL("SELECT count(*) AS n {f} WHERE {w}").format(
        f=_from(plan), w=where)), params


def newest_statement(plan: Plan, scope) -> sql.Composable:
    """The newest row this scope holds IN NO WINDOW AT ALL.

    THE RULE: a search that finds nothing in the last seven days, on a
    scope whose newest data is four months old, says so and offers the period
    that holds it - an empty table reads as a broken search, and the user
    read it that way. This is the same FROM and the same scope predicate as
    the chart, with the window taken out and nothing else changed, so the
    date it returns is a date the charts would actually draw.

    Asked ONLY when a whole tab came back empty, so the archive is never
    made to answer it for a page that already has an answer.
    """
    # The perspective filter travels with it, or the offer would be a lie:
    # "there is data in November" about a row the filtered view will not
    # show is worse than the empty table it was meant to explain.
    where = sql.SQL(" AND ").join(
        [sql.SQL("({p})").format(p=p) for p in
         [sqlbuild.identity(plan.alias), perspective_where(plan)]
         + [p for p in plan.where if p is not None]])
    return _with(scope, [], sql.SQL("SELECT max({tl}) AS newest {f} WHERE {w}").format(
        tl=sqlbuild.timeline(plan.table, plan.alias), f=_from(plan), w=where))
