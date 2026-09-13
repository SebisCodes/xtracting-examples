"""The Events tab: what happened, when it happened.

Events are the one table placed on a date that can lie in the FUTURE: an
announced launch is dated next month while the document reporting it was
written yesterday. Two consequences, both of them deliberate:

  * the timeline is COALESCE(date_eventdate, date_commissioned), so an event
    without its own date still appears - on its document's date, which is
    the only honest guess;
  * there is no chunk prefilter on date_added for this table
    (sqlbuild.NO_PREFILTER), because "archived after it happened" is exactly
    what an announcement breaks.

An event may also name the entities it is about (processed_data.event_entities,
one row per pair). An event with no such rows is not incomplete: it is about
the document as a whole, and the last chart of this tab is the count of the
two kinds side by side.
"""

from __future__ import annotations

from psycopg import sql

from .. import sqlbuild
from . import register_builder
from .drilldown import Plan, nz

# The pairs an event has with the entities it names. An inner join, so the
# charts built on it count (event, entity) pairs - which is what "events by
# entity" means - while the drilldown still lists the events themselves.
ENTITY_JOIN = sql.SQL(
    " JOIN processed_data.event_entities ee ON {idn} AND ee.text_task_id = t.text_task_id "
    "AND ee.bigint_fk_event_id = t.bigint_id"
    " LEFT JOIN LATERAL (SELECT e.text_name FROM processed_data.entities e "
    "WHERE {idn_e} AND e.text_task_id = ee.text_task_id "
    "AND e.text_entity_id = ee.text_fk_entity_id LIMIT 1) AS een ON true"
).format(idn=sqlbuild.identity("ee"), idn_e=sqlbuild.identity("e"))

NAMES_ENTITIES = sql.SQL(
    "EXISTS (SELECT 1 FROM processed_data.event_entities ee2 "
    "WHERE {idn} AND ee2.text_task_id = t.text_task_id "
    "AND ee2.bigint_fk_event_id = t.bigint_id)"
).format(idn=sqlbuild.identity("ee2"))


def _scope(scope) -> sql.Composable:
    """An event is in scope when one of the entities it names is.

    EXISTS rather than a join: an event about three entities in scope is
    still one event, and joining would count it three times.
    """
    if not scope.scoped:
        return sql.SQL("TRUE")
    if scope.kind == "source":
        return scope.src_predicate("t", "text_fk_source_id")
    return sql.SQL(
        "EXISTS (SELECT 1 FROM processed_data.event_entities ee3 "
        "WHERE {idn} AND ee3.text_task_id = t.text_task_id "
        "AND ee3.bigint_fk_event_id = t.bigint_id AND {p})"
    ).format(idn=sqlbuild.identity("ee3"),
             p=scope.ent_predicate("ee3", "text_fk_entity_id"))


@register_builder("events", "per_period")
def per_period(scope, ctx, window) -> Plan:
    return Plan(table="events", where=[_scope(scope)])


@register_builder("events", "by_type")
def by_type(scope, ctx, window) -> Plan:
    return Plan(table="events", where=[_scope(scope)], category=nz(sql.SQL("t.text_type")))


@register_builder("events", "by_entity")
def by_entity(scope, ctx, window) -> Plan:
    # Keyed on the entity id and labelled with the name, like every other
    # "by entity" chart: names are translated, ids are not.
    return Plan(table="events", joins=ENTITY_JOIN, where=[_scope(scope)],
                category=nz(sql.SQL("ee.text_fk_entity_id")),
                category_label=nz(sql.SQL("een.text_name")))


@register_builder("events", "type_matrix")
def type_matrix(scope, ctx, window) -> Plan:
    return Plan(table="events", joins=ENTITY_JOIN, where=[_scope(scope)],
                category=nz(sql.SQL("t.text_type")),
                axis_y=nz(sql.SQL("ee.text_fk_entity_id")),
                axis_y_label=nz(sql.SQL("een.text_name")),
                axis_titles=("Event type", "Entity"))


@register_builder("events", "about_document_vs_entities")
def about_document_vs_entities(scope, ctx, window) -> Plan:
    return Plan(table="events", where=[_scope(scope)], series=sql.SQL(
        "CASE WHEN {e} THEN 'about_entities' ELSE 'about_document' END").format(e=NAMES_ENTITIES))
