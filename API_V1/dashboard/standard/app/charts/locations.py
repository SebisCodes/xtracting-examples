"""The Locations tab: where the entities in scope are.

An address is one string ("Springfield, Illinois, USA"). City, region and
country are derived from it with THE SAME expressions the place list and the
Query page use (app/places.py: ADDRESS_PARTS_SQL and friends), so "all
addresses in the USA" means the same thing on every page - a second, tidier
split written here would eventually disagree with that one, and the two
answers would both look right.
"""

from __future__ import annotations

from psycopg import sql

from .. import places, sqlbuild
from . import register_builder
from .drilldown import Plan, nz, scope_where

_PART_SQL = {"city": places.CITY_SQL, "region": places.REGION_SQL, "country": places.COUNTRY_SQL}

# The ladder the summary band climbs, coarsest first. The names are the parts
# of an address as THIS archive writes them, which is not quite what the
# words say: parts[1] is the first component, so it is the city on
# "Zurich, Switzerland" and the STREET on "Bahnhofstrasse 1, Zurich, ZH,
# Switzerland" - and then the city has slipped into the region slot. That is
# exactly why the band climbs the ladder by measurement instead of picking a
# level by name (see _grain_join).
_GRAINS = ("country", "region", "city")


def part(which: str, alias: str = "t") -> sql.Composable:
    """One part of text_address as a scalar expression."""
    parts = sql.SQL(places.ADDRESS_PARTS_SQL.format(a="{a}")).format(a=sql.Identifier(alias))
    return sql.SQL("(SELECT {e} FROM (SELECT {p} AS parts) AS _p)").format(
        e=sql.SQL(_PART_SQL[which]), p=parts)


def scope_predicate(scope) -> sql.Composable:
    """Which location rows this page is about.

    Public for the map under these charts (app/charts/maps.py): the pins and
    the bars are two pictures of one search, and a second reading of "in
    scope" written there would eventually make them disagree.
    """
    return scope_where(scope, "t", entity_column="text_fk_entity_id",
                       source_column="text_fk_source_id")


# ── The summary band: the smallest common denominator ────────
#
# THE RULE: group up to the coarsest place that
# still distinguishes them - the city rather than the street where several
# streets share a city.
#
# So the level is a fact about the ANSWER and not about a row: forty
# addresses in one city are forty bars of one each, which says nothing, and
# the same forty grouped at the level above are one bar, which says nothing
# either. The coarsest level that yields MORE THAN ONE group is the one that
# tells them apart, and it is the one the band draws at.
#
# It costs one extra pass over the same rows, in an UNCORRELATED lateral -
# so the planner runs it once for the whole statement, not once per row -
# and the answer is a single word that the category expression then switches
# on. The alternative was to ask the archive twice from Python, which a
# builder cannot do: it is handed a scope, a context and a window, and no
# connection.
#
# The same join rides on the drilldown (Plan.joins is in both statements), so
# the rows behind a bar are grouped at exactly the level the bar was drawn
# at. Without that, clicking "Zurich" would list one street.


def _grain_join(scope, window) -> sql.Composable:
    """`LEFT JOIN LATERAL (…) AS lvl ON true`, yielding one word: the level."""
    sub = "lvl_src"
    where = sql.SQL(" AND ").join(sql.SQL("({p})").format(p=p) for p in (
        sqlbuild.identity(sub),
        sqlbuild.window_predicate("locations", window, sub),
        scope_where(scope, sub, entity_column="text_fk_entity_id",
                    source_column="text_fk_source_id"),
    ))
    branches = sql.SQL(" ").join(
        sql.SQL("WHEN count(DISTINCT {p}) > 1 THEN {g}").format(
            p=part(grain, sub), g=sql.Literal(grain)) for grain in _GRAINS)
    return sql.SQL(
        " LEFT JOIN LATERAL (SELECT CASE {b} ELSE 'address' END AS grain "
        "FROM {t} AS {a} WHERE {w}) AS lvl ON true"
    ).format(b=branches, t=sqlbuild.table("locations"), a=sql.Identifier(sub), w=where)


@register_builder("locations", "most_named")
def most_named(scope, ctx, window) -> Plan:
    category = sql.SQL("CASE {b} ELSE {a} END").format(
        b=sql.SQL(" ").join(
            sql.SQL("WHEN lvl.grain = {g} THEN {p}").format(g=sql.Literal(grain), p=part(grain))
            for grain in _GRAINS),
        a=sql.SQL("t.text_address"))
    return Plan(table="locations", joins=_grain_join(scope, window),
                where=[scope_predicate(scope)], category=nz(category))


@register_builder("locations", "per_period")
def per_period(scope, ctx, window) -> Plan:
    return Plan(table="locations", where=[scope_predicate(scope)])


@register_builder("locations", "by_country")
def by_country(scope, ctx, window) -> Plan:
    # A one-part address ("Linjiang") has no country. It is shown as
    # "(not stated)" rather than left out: the bars have to add up.
    return Plan(table="locations", where=[scope_predicate(scope)], category=nz(part("country")))


@register_builder("locations", "by_city")
def by_city(scope, ctx, window) -> Plan:
    return Plan(table="locations", where=[scope_predicate(scope)], category=nz(part("city")))


@register_builder("locations", "by_address")
def by_address(scope, ctx, window) -> Plan:
    return Plan(table="locations", where=[scope_predicate(scope)],
                category=nz(sql.SQL("t.text_address")))


@register_builder("locations", "by_type")
def by_type(scope, ctx, window) -> Plan:
    return Plan(table="locations", where=[scope_predicate(scope)], category=nz(sql.SQL("t.text_type")))


