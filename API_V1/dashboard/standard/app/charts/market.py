"""The Market Insights tab.

Everything here is DESCRIPTION, never instruction: what a source reported
about a market, not what anybody should do about it. The archive's
vocabulary is the descriptive one - an outlook is Rising, Neutral or
Declining, a sentiment runs from Very Negative to Very Positive - and this
dashboard shows that vocabulary and no other. See app/vocabulary.py, which
the tests enforce, and the colour note in charts/__init__.py: no traffic
light, because a green-good/red-bad scale reads as an instruction whatever
the labels say.

An outlook points a direction over a horizon; a sentiment says how strongly
a source read over that horizon. Both come in short-term and long-term
versions, which is why there are four period charts and a matrix of the two
sentiments against each other.
"""

from __future__ import annotations

from psycopg import sql

from . import (OUTLOOK_AXIS, OUTLOOK_VALUES, SENTIMENT_AXIS, SENTIMENT_VALUES,
               register_builder)
from .drilldown import Measure, Plan, case_map, entity_join, nz, scope_where


def _scope(scope) -> sql.Composable:
    return scope_where(scope, "t", entity_column="text_fk_entity_id",
                       source_column="text_fk_source_id")

#: The same predicate under the name every other module reads it by:
#: charts/maps.py and charts/summaries.py both build statements of their own
#: and need this tab's idea of "in scope" without reaching into a private name.
scope_predicate = _scope



def _outlook(column: str, prefix: str):
    return case_map(sql.SQL("t.{c}").format(c=sql.Identifier(column)),
                    OUTLOOK_VALUES, "unset", prefix)


def _sentiment(column: str, prefix: str):
    return case_map(sql.SQL("t.{c}").format(c=sql.Identifier(column)),
                    SENTIMENT_VALUES, "unset", prefix)


# ── The ends of the scale are not a chart here ───────────────
#
# No chart of the topics whose reporting reads Very Negative over either
# horizon, and none for Very Positive: the list at the foot of this tab
# already IS the extreme readings - the same
# filter, with the sentence each one rests on beside it (charts/summaries.py:
# strong_signals) - and a bar counting how many of them a topic carries adds
# a number to something that is better read as rows.
#
# THE WORD IS "READING", NEVER A DIRECTION. This tab describes what a source
# reported, and charts/__init__.py sets out at length why nothing on it may
# add an arrow, a triangle or a verb to the colour.


@register_builder("market", "per_period")
def per_period(scope, ctx, window) -> Plan:
    # Two measures, not two categories: a high-relevance insight is counted
    # in both lines, because it IS one of the insights.
    return Plan(table="market_insights", where=[_scope(scope)], measures=(
        Measure("insights", "Market insights", sql.SQL("count(*)")),
        Measure("high_relevance", "High relevance",
                sql.SQL("count(*) FILTER (WHERE t.bool_high_relevance)"),
                filter=sql.SQL("t.bool_high_relevance")),
    ))


@register_builder("market", "short_outlook_per_period")
def short_outlook_per_period(scope, ctx, window) -> Plan:
    series, params = _outlook("text_short_term_outlook", "so")
    return Plan(table="market_insights", where=[_scope(scope)], params=params, series=series)


@register_builder("market", "long_outlook_per_period")
def long_outlook_per_period(scope, ctx, window) -> Plan:
    series, params = _outlook("text_long_term_outlook", "lo")
    return Plan(table="market_insights", where=[_scope(scope)], params=params, series=series)


@register_builder("market", "short_sentiment_per_period")
def short_sentiment_per_period(scope, ctx, window) -> Plan:
    series, params = _sentiment("text_short_term_sentiment", "ss")
    return Plan(table="market_insights", where=[_scope(scope)], params=params, series=series)


@register_builder("market", "long_sentiment_per_period")
def long_sentiment_per_period(scope, ctx, window) -> Plan:
    series, params = _sentiment("text_long_term_sentiment", "ls")
    return Plan(table="market_insights", where=[_scope(scope)], params=params, series=series)


@register_builder("market", "by_entity")
def by_entity(scope, ctx, window) -> Plan:
    """Which entities the reporting is about.

    The topic says what KIND of thing is being reported on ("Shipbuilding");
    this says WHOSE. Both are on the tab because a reader arrives with one of
    the two questions and neither answers the other.

    Keyed on the entity id with the name as the label, like every other "by
    entity" chart, so a translated name does not split one entity into two
    bars; where the archive has no name for it the label says so rather than
    printing the extraction's own id (routers/api_diagrams.py: readable).
    """
    return Plan(table="market_insights",
                joins=entity_join("t", "text_fk_entity_id", "ment"),
                where=[_scope(scope)],
                category=nz(sql.SQL("t.text_fk_entity_id")),
                category_label=nz(sql.SQL("ment.text_name")))


@register_builder("market", "by_topic")
def by_topic(scope, ctx, window) -> Plan:
    return Plan(table="market_insights", where=[_scope(scope)],
                category=nz(sql.SQL("t.text_topic")))


@register_builder("market", "outlook_matrix")
def outlook_matrix(scope, ctx, window) -> Plan:
    """Which way the reporting pointed over the short term against the long.

    The counterpart of the sentiment matrix, and the same shape for the same
    reason: both axes pinned to the scale so agreement is the diagonal, and
    "Unset" first, off the ramp, because an insight nobody gave a direction
    is not one step past Rising.

    Outlook and sentiment are two different readings of one insight -
    WHICH WAY it points and HOW STRONGLY it reads - so this is a second
    matrix rather than a second series of the first one.
    """
    return Plan(table="market_insights", where=[_scope(scope)],
                category=nz(sql.SQL("t.text_short_term_outlook"), "Unset"),
                axis_y=nz(sql.SQL("t.text_long_term_outlook"), "Unset"),
                axis_titles=("Short-term outlook", "Long-term outlook"),
                axis_order=(OUTLOOK_AXIS, OUTLOOK_AXIS))


@register_builder("market", "sentiment_matrix")
def sentiment_matrix(scope, ctx, window) -> Plan:
    # The axes carry the published words themselves, so the drilldown key is
    # readable in a URL and the axis needs no legend of its own.
    #
    # Both axes are PINNED to the scale (SENTIMENT_AXIS), not left in the
    # order the archive returned them in. This chart exists to show whether
    # the short-term reading and the long-term reading agree, and agreement
    # is only visible as a diagonal when both axes run in the same order;
    # scrambled, a bubble's position says nothing at all.
    #
    # SENTIMENT_AXIS, not SENTIMENT_SCALE: an insight with no sentiment is
    # off the scale, not one step past Very Positive, so Unset is drawn
    # first and cut off from the ramp by an empty slot.
    return Plan(table="market_insights", where=[_scope(scope)],
                category=nz(sql.SQL("t.text_short_term_sentiment"), "Unset"),
                axis_y=nz(sql.SQL("t.text_long_term_sentiment"), "Unset"),
                axis_titles=("Short-term sentiment", "Long-term sentiment"),
                axis_order=(SENTIMENT_AXIS, SENTIMENT_AXIS))
