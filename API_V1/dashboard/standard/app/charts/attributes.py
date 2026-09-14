"""The Attributes tab: the measured facts on the entities in scope.

An attribute is a name, a value, a unit and - when the value could be read
as a number - `float_value` beside the original text. "approx. 12000" keeps
both: the text is what the document said, the number is what can be
averaged. The numeric chart therefore counts only the rows that HAVE a
number, and shows a type only once it has at least three of them; two
readings are not a range, they are two readings.
"""

from __future__ import annotations

from psycopg import sql

from . import register_builder
from .drilldown import Plan, nz, scope_where

# How many numeric readings a type needs before its minimum, average and
# maximum are worth drawing.
MIN_NUMERIC_READINGS = 3


def _scope(scope) -> sql.Composable:
    return scope_where(scope, "t", entity_column="text_fk_entity_id",
                       source_column="text_fk_source_id")

#: The same predicate under the name every other module reads it by:
#: charts/maps.py and charts/summaries.py both build statements of their own
#: and need this tab's idea of "in scope" without reaching into a private name.
scope_predicate = _scope



@register_builder("attributes", "per_period")
def per_period(scope, ctx, window) -> Plan:
    return Plan(table="attributes", where=[_scope(scope)])


@register_builder("attributes", "by_type")
def by_type(scope, ctx, window) -> Plan:
    return Plan(table="attributes", where=[_scope(scope)], category=nz(sql.SQL("t.text_type")))


@register_builder("attributes", "by_unit")
def by_unit(scope, ctx, window) -> Plan:
    # An attribute without a unit is ordinary ("solid state"), so the empty
    # unit is a category with a name rather than a gap in the chart.
    return Plan(table="attributes", where=[_scope(scope)],
                category=nz(sql.SQL("t.text_unit"), "(no unit)"))


@register_builder("attributes", "by_name")
def by_name(scope, ctx, window) -> Plan:
    return Plan(table="attributes", where=[_scope(scope)], category=nz(sql.SQL("t.text_name")))


