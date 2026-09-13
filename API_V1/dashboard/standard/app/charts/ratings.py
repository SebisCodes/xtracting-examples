"""The Ratings tab: how the sources rated the entities in scope.

A rating is a value on a named scale ("Reputation: Bad") given from a
perspective ("Compliance"). The ORDER of the values is not alphabetical and
not a guess - it is `processed_data.rating_values.float_value`, seeded by
database/init/02-vocabularies.sql (Egregious -3 … Excellent +3), which is
also what makes "negative, neutral, positive reporting" a comparison rather
than a list of names to memorise.

The join to that table is a LATERAL with LIMIT 1, not a plain join: the
vocabulary is seeded under the empty project and a customer may add a row of
their own under theirs (sqlbuild.vocabulary_scope), and two matching rows
would double every rating in the chart.
"""

from __future__ import annotations

from psycopg import sql

from .. import sqlbuild
from . import register_builder
from .drilldown import Plan, nz, scope_where, source_join

# float_value of the rating's value, for the sign and for the scale order.
VALUE_JOIN = sql.SQL(
    " LEFT JOIN LATERAL (SELECT rv.float_value FROM processed_data.rating_values rv "
    "WHERE rv.text_name = t.text_rating_value AND {vs} "
    "ORDER BY (rv.text_project <> '') DESC LIMIT 1) AS rv ON true"
).format(vs=sqlbuild.vocabulary_scope("rv"))

# vocabulary.sign_of, in SQL: a value the vocabulary does not know is shown
# as neutral rather than dropped, so the three datasets add up to the total.
SIGN = sql.SQL("CASE WHEN rv.float_value < 0 THEN 'negative' "
               "WHEN rv.float_value > 0 THEN 'positive' ELSE 'neutral' END")

# THE SAME SCALE WITH ALL OF ITS STEPS, for the chart that shows the shape of
# the reporting rather than its sign. Read off `float_value` and not off the
# words: the number is what the archive orders by and what a customer's own
# vocabulary carries too, so a renamed value still lands on its own step. A
# value the vocabulary has never heard of has no number and becomes "unset" -
# counted, grey, and never silently dropped.
RATING_STEP = sql.SQL(
    "CASE WHEN rv.float_value IS NULL THEN 'unset' "
    "WHEN rv.float_value <= -3 THEN 'very_negative' "
    "WHEN rv.float_value <= -2 THEN 'negative' "
    "WHEN rv.float_value < 0 THEN 'slightly_negative' "
    "WHEN rv.float_value = 0 THEN 'neutral' "
    "WHEN rv.float_value < 2 THEN 'slightly_positive' "
    "WHEN rv.float_value < 3 THEN 'positive' ELSE 'very_positive' END")


def _scope(scope) -> sql.Composable:
    return scope_where(scope, "t", entity_column="text_fk_entity_id",
                       source_column="text_fk_source_id")


# ── The summary band: the ends of the scale ──────────────────
#
# HOW FAR FROM NEUTRAL A RATING HAS TO BE TO COUNT AS EXTREME. Two steps, on
# the seeded scale of -3…+3 (02-vocabularies.sql): Egregious and Very Bad at
# one end, Very Good and Excellent at the other. It is a NUMBER and not a
# list of words because the vocabulary is the customer's to extend - a
# project that adds a value of its own gets it counted here without anybody
# editing this file, which is the whole reason float_value exists.
EXTREME_STEP = 2

# ONE CHART OF THE WHOLE SCALE, and not two of its ends.
#
# Two charts of the ends - the most negative rating names and the most
# positive - would between them answer "where is the shouting" and nothing
# else: a name rated Neutral four hundred times appears in neither, and a
# reader cannot tell a scale that is used evenly from one that is never used
# at all.
#
# The summary is one bar per rating name split into the steps of the scale
# (by_name_and_value above), which is the distribution the two ends would be
# a sample of. Stacked rather than side by side, because eight series beside
# each other at the band's row height are eight 5 px bars.


@register_builder("ratings", "by_name_and_value")
def by_name_and_value(scope, ctx, window) -> Plan:
    """The tab's summary: which scales were used, and what was said on them.

    One bar per rating name, split into the steps of the value scale. The two
    charts it replaced showed the ENDS of the scale - the most negative names
    and the most positive ones - which answered "where is the shouting" and
    nothing about the shape of the reporting; a name rated Neutral four
    hundred times appeared in neither.
    """
    return Plan(table="ratings", joins=VALUE_JOIN, where=[_scope(scope)],
                series=RATING_STEP, category=nz(sql.SQL("t.text_rating_name")))


@register_builder("ratings", "per_period")
def per_period(scope, ctx, window) -> Plan:
    return Plan(table="ratings", where=[_scope(scope)])


@register_builder("ratings", "values_per_period")
def values_per_period(scope, ctx, window) -> Plan:
    return Plan(table="ratings", joins=VALUE_JOIN, where=[_scope(scope)], series=SIGN)


@register_builder("ratings", "by_value")
def by_value(scope, ctx, window) -> Plan:
    # Ordered by the scale, not by size: a rating chart read from the worst
    # value to the best is a distribution; sorted by count it is a list.
    #
    # AND COLOURED BY IT. Seven bars in one blue said only "how many of
    # each"; the scale is the point of this chart, so each bar takes the
    # colour of its own step - neutral in the middle, green rising to
    # Excellent, red falling to Egregious. The step is the number the
    # archive already carries (`rating_values.float_value`), which the
    # statement selects for the ordering anyway, so the colour costs
    # nothing and cannot disagree with the order.
    #
    # GOOD AT THE TOP, BAD AT THE BOTTOM. A horizontal bar chart draws its
    # first category at the TOP, so ordering by the scale as it reads
    # (Egregious first) puts the worst value above the best - a distribution
    # standing on its head. `order_desc` turns it the right way up.
    #
    # NOT BY NEGATING THE VALUE: the same number is the bar's COLOUR, so
    # `-rv.float_value` would draw Excellent in the red of -3 and Bad in the
    # green of +1 (drilldown.py: Plan.order_desc).
    return Plan(table="ratings", joins=VALUE_JOIN, where=[_scope(scope)],
                category=nz(sql.SQL("t.text_rating_value")),
                order="scale", order_expr=sql.SQL("rv.float_value"), order_desc=True,
                colour_source="valence")


@register_builder("ratings", "by_perspective")
def by_perspective(scope, ctx, window) -> Plan:
    return Plan(table="ratings", joins=VALUE_JOIN, where=[_scope(scope)], series=SIGN,
                category=nz(sql.SQL("t.text_perspective")))


@register_builder("ratings", "by_domain")
def by_domain(scope, ctx, window) -> Plan:
    # Which hosts rate this entity, and how often - the rating's own row
    # has no URI, so its document is looked up first.
    return Plan(table="ratings", joins=source_join("t", "rsrc"), where=[_scope(scope)],
                category=nz(sqlbuild.host_expression("rsrc")))
