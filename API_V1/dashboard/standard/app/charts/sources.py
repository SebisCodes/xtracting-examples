"""The Sources tab: how many documents arrived, of what kind, from where.

A source is a document the extraction was run on. It sits on the date the
document says it was written, falling back to the date its task was
commissioned (sqlbuild.TIMELINE_COLUMN) - never on the date the collector
happened to fetch it, which is a fact about the collector and not about the
world.

Importance appears twice on purpose. `text_importance` is the one importance
the extraction gave the document; `source_importances_by_perspective` is the
same question asked once per perspective a customer watches ("for
Maintenance this filing is critical, for Compliance it is not"), and that is
a different table with one row per perspective.
"""

from __future__ import annotations

from psycopg import sql

from .. import sqlbuild
from . import IMPORTANCE_VALUES, UNSTATED, register_builder
from .drilldown import Plan, case_map, nz, scope_where


def _scope(scope, alias: str = "t", column: str = "text_source_id") -> sql.Composable:
    return scope_where(scope, alias, source_column=column)

#: The same predicate under the name every other module reads it by:
#: charts/maps.py and charts/summaries.py both build statements of their own
#: and need this tab's idea of "in scope" without reaching into a private name.
scope_predicate = _scope



def _importance(alias: str = "t", column: str = "text_importance"):
    return case_map(sql.SQL("{a}.{c}").format(a=sql.Identifier(alias), c=sql.Identifier(column)),
                    IMPORTANCE_VALUES, UNSTATED, "imp")


@register_builder("sources", "per_period")
def per_period(scope, ctx, window) -> Plan:
    return Plan(table="sources", where=[_scope(scope)])


@register_builder("sources", "importance_per_period")
def importance_per_period(scope, ctx, window) -> Plan:
    """The importance judgements of every perspective, per period.

    NOT `sources.text_importance`. That column is a document's one overall
    importance, and there is no such thing to a reader of this product: an
    importance is always FROM a perspective, and a document that matters to
    Compliance and not to Procurement has two judgements rather than one
    average. The chart counts the judgements themselves - one document judged
    from three perspectives contributes three - which is the same table the
    chart beside it splits by perspective, read the other way round.
    """
    series, params = _importance()
    return Plan(table="source_importances_by_perspective",
                where=[_scope(scope, column="text_fk_source_id")],
                params=params, series=series)


@register_builder("sources", "importance_by_perspective")
def importance_by_perspective(scope, ctx, window) -> Plan:
    series, params = _importance()
    return Plan(table="source_importances_by_perspective",
                where=[_scope(scope, column="text_fk_source_id")],
                params=params, series=series,
                category=nz(sql.SQL("t.text_perspective")))


@register_builder("sources", "by_domain")
def by_domain(scope, ctx, window) -> Plan:
    # The host of text_uri, in SQL: the archive image has no URL functions
    # and pg_trgm is not installed, so this is a substring on a pattern
    # (sqlbuild.host_expression) rather than anything cleverer.
    return Plan(table="sources", where=[_scope(scope)],
                category=nz(sqlbuild.host_expression("t")))


@register_builder("sources", "by_type")
def by_type(scope, ctx, window) -> Plan:
    return Plan(table="sources", where=[_scope(scope)],
                category=nz(sql.SQL("t.text_type")))


@register_builder("sources", "trust_per_period")
def trust_per_period(scope, ctx, window) -> Plan:
    # bool_trustful is NOT NULL, so the two datasets cover every row and
    # their sum is the same number the first chart shows.
    return Plan(table="sources", where=[_scope(scope)],
                series=sql.SQL("CASE WHEN t.bool_trustful THEN 'trustful' ELSE 'not_trustful' END"))
