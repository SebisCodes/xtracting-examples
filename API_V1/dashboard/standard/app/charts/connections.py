"""The Connections tab: who is tied to whom, and how.

A connection row carries BOTH directions ("Supplier" one way, "Customer"
the other). The charts count it once, from the parent to the child, which is
the direction the extraction wrote it in; the drilldown shows both, so a row
that reads oddly one way round can still be understood.

Colours come from the colour table (dashboard.colour_group_types), never
from a regular expression at draw time - the same type must be the same
colour on the map, in the graph and here (app/colours.py explains why the
regex list only ever seeds that table). The lookup is a LATERAL with
LIMIT 1 that prefers the exactly-spelled row, so "Supplier" and "supplier"
cannot both match and double the count.
"""

from __future__ import annotations

from psycopg import sql

from .. import sqlbuild
from . import register_builder
from .drilldown import SIDE_ROLE, Plan, nz

PARENT_TO_CHILD = sql.SQL("t.text_type_parent_to_child")

GROUP_JOIN = sql.SQL(
    " LEFT JOIN LATERAL (SELECT g.text_key, g.text_name FROM dashboard.colour_group_types ct "
    "JOIN dashboard.colour_groups g ON g.bigint_id = ct.bigint_fk_group "
    "WHERE lower(ct.text_type_name) = lower({t}) "
    "ORDER BY (ct.text_type_name = {t}) DESC LIMIT 1) AS grp ON true"
).format(t=PARENT_TO_CHILD)

# A type nobody has assigned belongs to the group marked as the fallback -
# the same rule the runtime resolver follows, so the bar and the map agree.
GROUP_KEY = sql.SQL(
    "COALESCE(grp.text_key, (SELECT g2.text_key FROM dashboard.colour_groups g2 "
    "WHERE g2.bool_fallback LIMIT 1), 'other')")
GROUP_NAME = sql.SQL(
    "COALESCE(grp.text_name, (SELECT g2.text_name FROM dashboard.colour_groups g2 "
    "WHERE g2.bool_fallback LIMIT 1), 'Other')")


def scope_predicate(scope) -> sql.Composable:
    """A connection is in scope when EITHER end is.

    Asked about Apple, "Apple is a customer of Foxconn" and "Foxconn is a
    supplier to Apple" are both about Apple; keeping only the rows where
    Apple happens to be the parent would show half the network and give no
    sign that the other half exists.

    Public because the map under these charts (app/charts/maps.py) asks the
    archive the same question and must not answer it its own way: bars and
    lines that disagree about what "in scope" means are two pictures of two
    different searches on one screen.
    """
    if not scope.scoped:
        return sql.SQL("TRUE")
    if scope.kind == "source":
        return scope.src_predicate("t", "text_fk_source_id")
    return sql.SQL("({p} OR {c})").format(
        p=scope.ent_predicate("t", "text_fk_parent_entity_id"),
        c=scope.ent_predicate("t", "text_fk_child_entity_id"))


def _entity_name(expr: sql.Composable, out: str) -> sql.Composable:
    return sql.SQL(
        " LEFT JOIN LATERAL (SELECT e.text_name FROM processed_data.entities e "
        "WHERE {idn} AND e.text_task_id = t.text_task_id AND e.text_entity_id = {x} "
        "LIMIT 1) AS {o} ON true"
    ).format(idn=sqlbuild.identity("e"), x=expr, o=sql.Identifier(out))


@register_builder("connections", "per_period")
def per_period(scope, ctx, window) -> Plan:
    return Plan(table="connections", where=[scope_predicate(scope)])


@register_builder("connections", "by_group")
def by_group(scope, ctx, window) -> Plan:
    return Plan(table="connections", joins=GROUP_JOIN, where=[scope_predicate(scope)],
                category=GROUP_KEY, category_label=GROUP_NAME,
                colour_source="colour_group")


@register_builder("connections", "by_type")
def by_type(scope, ctx, window) -> Plan:
    """Every tie counted from BOTH of its ends.

    A client/supplier pair is one row in the archive and two facts on this
    chart: one for "Client" and one for "Supplier". Counted from the parent
    alone - which is the end the extraction happened to write first - half
    the vocabulary never appears, and the half that does is an accident of
    how the sentence in the document was phrased.

    The doubling is Plan.both_directions (app/charts/drilldown.py), so the
    bar, the listing behind it and the CSV are the same rows: click the
    "Client" bar and every line of the dialog reads "... is a Client of
    ...".
    """
    return Plan(table="connections", where=[scope_predicate(scope)],
                both_directions=True, category=nz(SIDE_ROLE))


@register_builder("connections", "matrix")
def matrix(scope, ctx, window) -> Plan:
    """Type against counterpart when something is in scope, parent against
    child for the summary.

    With a scope there is one entity (or one bucket) everybody already knows
    about, so naming it on both axes wastes the picture: the useful pair is
    "what kind of tie" against "with whom". Without one, the two ends are
    the only thing there is to compare.

    Which means the sentence under the title cannot be one sentence. The
    registry's covers the summary; the scoped chart is a different picture
    and says so itself, because "type by counterpart for a scoped entity;
    parent by child for the summary" asks the reader to work out which half
    of it is in front of them.

    THE SUMMARY KEEPS PARENT BY CHILD EVEN WHEN IT IS SEARCHED. Its
    magnifier searches everything (app/scope.py: _resolve_everything), so a
    term there is not one entity everybody knows about - it can be a bucket
    AND two documents AND a place at once, and there is no "the" entity to
    leave off the axes.
    """
    if scope.kind == "summary":
        joins = (_entity_name(sql.SQL("t.text_fk_parent_entity_id"), "par")
                 + _entity_name(sql.SQL("t.text_fk_child_entity_id"), "chi"))
        return Plan(table="connections", joins=joins, where=[scope_predicate(scope)],
                    description="Which entity is the parent, which is the child, and how often.",
                    category=nz(sql.SQL("t.text_fk_parent_entity_id")),
                    category_label=nz(sql.SQL("par.text_name")),
                    axis_y=nz(sql.SQL("t.text_fk_child_entity_id")),
                    axis_y_label=nz(sql.SQL("chi.text_name")),
                    axis_titles=("Parent entity", "Child entity"))

    counterpart = sql.SQL(
        "CASE WHEN {inscope} THEN t.text_fk_child_entity_id ELSE t.text_fk_parent_entity_id END"
    ).format(inscope=scope.ent_predicate("t", "text_fk_parent_entity_id"))
    return Plan(table="connections", joins=_entity_name(counterpart, "cpt"),
                where=[scope_predicate(scope)],
                description="Which connection type links this entity to which counterpart.",
                category=nz(PARENT_TO_CHILD),
                axis_y=nz(counterpart), axis_y_label=nz(sql.SQL("cpt.text_name")),
                axis_titles=("Connection type", "Counterpart"))


