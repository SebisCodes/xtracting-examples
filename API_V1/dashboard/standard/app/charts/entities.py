"""The Entities tab: who and what the documents talk about.

Two numbers that are easy to confuse and are both worth having: how many
entity ROWS were written in a period, and how many DISTINCT entities those
rows are. Six documents each naming Apple are six rows and one entity, and
which of the two a person means depends entirely on the question.

An entity is identified by (task, entity id), never by its name: the German
rows carry the same ids under translated names, and two tasks may reuse one
id string for different things (see app/scope.py). So "by name" groups by
the id and SHOWS the name.
"""

from __future__ import annotations

from psycopg import sql

from . import register_builder
from .drilldown import Measure, Plan, nz, scope_where


def _scope(scope) -> sql.Composable:
    return scope_where(scope, "t", entity_column="text_entity_id",
                       source_column="text_fk_source_id")


@register_builder("entities", "most_named")
def most_named(scope, ctx, window) -> Plan:
    """The summary band: the most mentioned entities, each with its type.

    THE TYPE TRAVELS IN THE LABEL, not as a second series. "Together with
    their type" could be drawn either way, and colouring by type would need
    one legend entry per kind the archive holds - twenty of them on a real
    project, cycled through eight palette colours, which is a key that
    cannot be read. In the label it costs nothing: the axis already carries
    the name, and the type under it answers "what kind of thing is this"
    without a legend at all.

    ON ITS OWN LINE, separated by a newline. Joined into one string it
    would be a line that grows with the type and is then cut to the axis, so
    the name - the thing being read - would lose its last characters to a
    word that is not it. static/js/charts.js draws the first line on the axis and the second
    under it, smaller and in italics.

    Keyed on the entity id like every other "by entity" chart, so a
    translated name does not split one entity into two bars.
    """
    return Plan(table="entities", where=[_scope(scope)],
                category=nz(sql.SQL("t.text_entity_id")),
                category_label=nz(sql.SQL(
                    "t.text_name || CASE WHEN COALESCE(t.text_type, '') <> '' "
                    "THEN E'\\n' || t.text_type ELSE '' END")))


@register_builder("entities", "per_period")
def per_period(scope, ctx, window) -> Plan:
    return Plan(table="entities", where=[_scope(scope)], measures=(
        Measure("rows", "Entity mentions", sql.SQL("count(*)")),
        # Distinct by the ID ALONE, deliberately, although an entity is
        # identified by (task, id) everywhere else: the question this line
        # answers is "how many different things were named", and Apple Inc.
        # in six documents is one thing. Counting the pair would make this
        # line identical to the one above it. The price is that two tasks
        # reusing one id string for different things are conflated - the
        # same price "by name" pays, and for the same reason.
        Measure("distinct", "Distinct entities",
                sql.SQL("count(DISTINCT t.text_entity_id)")),
    ))


@register_builder("entities", "by_type")
def by_type(scope, ctx, window) -> Plan:
    return Plan(table="entities", where=[_scope(scope)],
                category=nz(sql.SQL("t.text_type")))


