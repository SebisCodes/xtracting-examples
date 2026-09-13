"""The picture under the charts: where the Locations are, and where the
Connections run.

NOT A CHART, and that is why it is not in the registry. Every other module in
this package returns a `Plan` the chart machinery turns into bars; this one
returns points and lines for a Leaflet map. It lives here because it asks the
archive the SAME question the tab above it asks - the same scope CTEs, the
same window - and a second module that resolved "one entity in this period"
its own way would eventually disagree with the bars it sits under.

TWO PICTURES, ONE RULE EACH:

  locations    one pin per place, counted. Two entities at one address are
               one pin with a 2 on it, not two pins on top of each other -
               the Map view's rule (routers/api_map.py: _LOCATIONS), kept so
               a reader who knows one map can read the other.
  connections  one line per pair of entities, each end at the address that
               entity carries most often, and a pin at each end.

WHAT THE PERIOD MEANS ON EACH. On the Locations map the period is the whole
question: a location row is dated, and "the places written about in the last
7 days" is what the bars above count. On the Connections map the period is
the question about the LINES - the connections written in it - while the
ADDRESSES are the standing ones, unwindowed. An address is not news: an
entity's office does not stop existing because nobody filed a document about
it this week, and windowing the addresses too would empty the map of almost
every line it had just found. The caption on the page says exactly this, so
nobody has to guess which half of the picture the period moved.

BOTH ARE CAPPED, AND THE CAP IS SAID OUT LOUD. A whole-project locations map
is tens of thousands of points, which is a slow page on a machine that has
to draw them and a useless picture on any machine at all. The biggest N come
back and the answer carries how many there were, so the sentence under the
map can say "400 of 12,309 places" rather than quietly showing a fraction.

AND WHAT THE READER'S PERSPECTIVE NARROWS, WHICH IS THE SAME HALF THE PERIOD
NARROWS. The tab above each of these pictures is filtered by it
(charts/drilldown.py: perspective_where), and the page carries one line
saying every count on it is a subset - so a map underneath that ignored the
filter would make that line false. The pins of the Locations map and the
LINES of the Connections map are therefore filtered.

The addresses the Connections map PLACES those lines at are not, exactly as
they are not windowed: they are standing facts, used to put a line the filter
has already chosen somewhere on the world. Filtering them would leave a
chosen line with nothing to end on and take it off the map - which is not
"showing a subset", it is showing a different picture.
"""

from __future__ import annotations

from typing import Any

from psycopg import sql

from .. import sqlbuild
from ..colours import ColourResolver
from .connections import scope_predicate as connection_scope
from .locations import scope_predicate as location_scope

# How much of either picture is drawn. Chosen from what a 22 rem map can
# show rather than from what the database can answer: past a few hundred
# marks the picture is a solid block of pins, and the archive has to send
# every one of them to a browser that then has to place them.
MAX_POINTS = 400
MAX_LINES = 400

# How many names one pin lists in its popup. The pin says how many rows it
# stands for; the names are so the reader can tell WHICH place it is.
NAMES_PER_POINT = 4


def _with(scope, extra: list[sql.Composable], body: sql.Composable) -> sql.Composable:
    """`WITH ent AS (…), src AS (…), <extra> <body>`.

    The scope's own CTEs come first because the extras read them, and on the
    unscoped summary there are none at all - the predicates are TRUE and the
    sets are never consulted (app/scope.py: ScopeSet.scoped).
    """
    ctes = (list(scope.ctes()) if scope.scoped else []) + list(extra)
    if not ctes:
        return body
    return sql.SQL("WITH {c} {b}").format(c=sql.SQL(", ").join(ctes), b=body)


# ── Locations ────────────────────────────────────────────────
#
# GROUPED BY THE POINT ON THE WORLD, NOT BY THE ADDRESS STRING. One place is
# one pin, and the archive spells it several ways: "Cupertino, California,
# United States", "Cupertino, California, USA" and no address at all are
# three rows at 37.3193/-122.0293, and grouping by the text put three pins on
# the same pixel - the reader clicks one and never learns the other two are
# under it. Five decimal places is about a metre.
#
# The address the pin carries is then the one the archive writes most often
# for that point (`mode`), which ignores the rows that have none - a pin
# labelled with the empty spelling would be the commonest and say nothing.
#
# `count(*) OVER ()` is the number of GROUPS, because a window function runs
# after grouping and before the LIMIT: it is how many places there are, which
# is the number the caption needs and the LIMIT would otherwise hide.
_PLACES = """
    SELECT count(*) OVER ()                         AS places,
           avg(t.float_latitude)                    AS lat,
           avg(t.float_longitude)                   AS lng,
           mode() WITHIN GROUP (ORDER BY nullif(t.text_address, '')) AS address,
           count(*)                                 AS n,
           count(DISTINCT t.text_name)              AS named,
           (array_remove(array_agg(DISTINCT t.text_name), ''))[1:{names}] AS names,
           (array_remove(array_agg(DISTINCT t.text_type), ''))[1:{names}] AS types
      FROM processed_data.locations AS t
     WHERE {idn} AND {win} AND ({scope}) AND ({persp})
       AND t.float_latitude IS NOT NULL AND t.float_longitude IS NOT NULL
     GROUP BY round(t.float_latitude::numeric, 5),
              round(t.float_longitude::numeric, 5)
     ORDER BY n DESC, address
     LIMIT {cap}
"""


# HOW MANY ROWS THERE WERE AT ALL, asked only when none of them could be
# placed.
#
# Both pictures above are built from rows that HAVE a coordinate, so when the
# archive has none the statement comes back empty and every number in the
# answer is zero - including the one the caption uses to say how many it could
# not draw. "No address has a point on the world" then reads as "there is
# nothing here", which is the one thing it does not mean: an address is a fact
# a document has to state and most do not, so this is what a real archive
# mostly looks like on these two tabs.
#
# It costs one count, in the case where there is nothing else to do.
_UNPLACED = {
    "locations": """
        SELECT count(*) AS n FROM processed_data.locations AS t
         WHERE {idn} AND {win} AND ({scope}) AND ({persp})
    """,
    "connections": """
        SELECT count(*) AS n FROM (
          SELECT DISTINCT least(t.text_fk_parent_entity_id, t.text_fk_child_entity_id),
                 greatest(t.text_fk_parent_entity_id, t.text_fk_child_entity_id)
            FROM processed_data.connections AS t
           WHERE {idn} AND {win} AND ({scope}) AND ({persp})
             AND t.text_fk_parent_entity_id IS NOT NULL AND t.text_fk_parent_entity_id <> ''
             AND t.text_fk_child_entity_id  IS NOT NULL AND t.text_fk_child_entity_id  <> ''
             AND t.text_fk_parent_entity_id <> t.text_fk_child_entity_id) AS pairs
    """,
}


def _unplaced(conn, table: str, scope, ctx, window, where: sql.Composable) -> int:
    body = sql.SQL(_UNPLACED[table]).format(
        idn=sqlbuild.identity("t"),
        win=sqlbuild.window_predicate(table, window, "t"),
        scope=where,
        persp=sqlbuild.perspective_scope(
            getattr(ctx, "perspective", ""), "t",
            sqlbuild.source_id_column(table)))
    row = conn.execute(
        _with(scope, [], body),
        {**sqlbuild.identity_params(ctx.project, ctx.language),
         **sqlbuild.perspective_params(getattr(ctx, "perspective", ""),
                                       getattr(ctx, "min_importance", "")),
         **sqlbuild.window_params(window), **scope.params}).fetchone()
    return int(row["n"]) if row else 0


def locations_map(conn, scope, ctx, window) -> dict[str, Any]:
    """Every place the scope's locations sit at, in this period."""
    body = sql.SQL(_PLACES).format(
        idn=sqlbuild.identity("t"),
        win=sqlbuild.window_predicate("locations", window, "t"),
        scope=location_scope(scope),
        # The same judgement the bars above this map are counted under, so a
        # page that says "every count here is a subset" is telling the truth
        # about its picture as well as about its charts.
        persp=sqlbuild.perspective_scope(
            getattr(ctx, "perspective", ""), "t",
            sqlbuild.source_id_column("locations")),
        names=sql.Literal(NAMES_PER_POINT),
        cap=sql.Literal(MAX_POINTS))
    rows = conn.execute(
        _with(scope, [], body),
        {**sqlbuild.identity_params(ctx.project, ctx.language),
         **sqlbuild.perspective_params(getattr(ctx, "perspective", ""),
                                       getattr(ctx, "min_importance", "")),
         **sqlbuild.window_params(window), **scope.params}).fetchall()

    places = [{
        "lat": float(r["lat"]), "lng": float(r["lng"]),
        "address": r["address"] or "",
        "count": int(r["n"]),
        # How many names are AT the point, against the four the popup lists:
        # a pin that says "Cupertino +3" when nine things stand there is a
        # pin that has quietly dropped five of them.
        "named": int(r["named"]),
        "names": list(r["names"] or []),
        "types": list(r["types"] or []),
    } for r in rows]
    total = int(rows[0]["places"]) if rows else 0
    return {
        "mode": "locations",
        "places": places,
        "lines": [],
        "total": total,
        # The rows the period holds that the archive cannot place, so the card
        # can say which of the two kinds of empty this is.
        "unplaced": 0 if places else _unplaced(
            conn, "locations", scope, ctx, window, location_scope(scope)),
        "shown": len(places),
        "capped": total > len(places),
        "bounds": _bounds([(p["lat"], p["lng"]) for p in places]),
    }


# ── Connections ──────────────────────────────────────────────
#
# ONE LINE PER PAIR, WHICHEVER WAY ROUND THE EXTRACTION WROTE IT. The archive
# holds Foxconn→Apple and Apple→Foxconn for one fact; `least`/`greatest` fold
# the two into one pair, so the map draws one line where the reader sees one
# relationship. (routers/api_map.py: _pair_key does the same, in Python.)
#
# The type on the line is the pair's most frequent one, and it decides the
# colour - the same colour table the "Connections by colour group" chart
# directly above the map is drawn from (app/colours.py), so a green line here
# is a green bar there.
#
# WHERE AN ENTITY SITS IS A POINT THE ARCHIVE ACTUALLY WROTE, NEVER AN
# AVERAGE OF SEVERAL. The obvious way to place an entity that carries four
# coordinates is to average them, and it puts "iPhone 17" - geocoded to
# Cupertino, to Shenzhen and to nowhere in particular - in the middle of the
# Sahara, on a map that gives no sign anything is wrong. So the candidates
# are counted per point and the entity is put at the one written most often,
# with the address it carries there. A point on a map is a claim; the mean of
# two claims is not a third one.
#
# THE ADDRESSES ARE NOT WINDOWED; see the module header for why.
#
# The connections table is aliased `t` and not `c`, because that is the alias
# the charts above use and the predicate that says what "in scope" means is
# theirs (charts/connections.py: scope_predicate writes `t.` into its SQL).
# One alias, one predicate, one meaning.
_LINE_CTES = """
    edge AS (
        SELECT least(t.text_fk_parent_entity_id, t.text_fk_child_entity_id)    AS a,
               greatest(t.text_fk_parent_entity_id, t.text_fk_child_entity_id) AS b,
               t.text_type_parent_to_child AS type,
               /* THE ROLE EACH END PLAYS TOWARDS THE OTHER, kept through the
                * sort that puts the pair in a fixed order.
                *
                * The archive writes one row per tie and names both roles in
                * it - (Foxconn, Apple, "Supplier", "Customer"). `a` and `b`
                * are the two ids sorted, so which of the two words belongs
                * to `a` depends on which way round the extraction wrote the
                * pair, and the popup needs to know: "Foxconn is a Supplier
                * of Apple" and "Apple is a Customer of Foxconn" are the two
                * sentences one row makes, and swapping them makes both of
                * them false. */
               CASE WHEN t.text_fk_parent_entity_id <= t.text_fk_child_entity_id
                    THEN t.text_type_parent_to_child
                    ELSE t.text_type_child_to_parent END AS a_role,
               CASE WHEN t.text_fk_parent_entity_id <= t.text_fk_child_entity_id
                    THEN t.text_type_child_to_parent
                    ELSE t.text_type_parent_to_child END AS b_role,
               count(*) AS n
          FROM processed_data.connections AS t
         WHERE {idn_t} AND {win} AND ({scope}) AND ({persp})
           AND t.text_fk_parent_entity_id IS NOT NULL AND t.text_fk_parent_entity_id <> ''
           AND t.text_fk_child_entity_id  IS NOT NULL AND t.text_fk_child_entity_id  <> ''
           AND t.text_fk_parent_entity_id <> t.text_fk_child_entity_id
         GROUP BY 1, 2, 3, 4, 5
    ),
    paired AS (
        SELECT DISTINCT ON (a, b)
               a, b, type, a_role, b_role,
               sum(n) OVER (PARTITION BY a, b) AS total,
               count(*) OVER () AS pairs
          FROM edge
         ORDER BY a, b, n DESC, type
    ),
    ids AS (SELECT a AS id FROM paired UNION SELECT b FROM paired),
    spot AS (
        SELECT DISTINCT ON (id) id, address, lat, lng
          FROM (SELECT l.text_fk_entity_id          AS id,
                       coalesce(l.text_address, '') AS address,
                       round(l.float_latitude::numeric, 5)::float8  AS lat,
                       round(l.float_longitude::numeric, 5)::float8 AS lng,
                       count(*)                     AS rows_here
                  FROM processed_data.locations AS l
                 WHERE {idn_l} AND l.text_fk_entity_id IN (SELECT id FROM ids)
                   AND l.float_latitude IS NOT NULL AND l.float_longitude IS NOT NULL
                 GROUP BY 1, 2, 3, 4) AS sp
         ORDER BY id, rows_here DESC, address
    ),
    /* THE CAP TAKES FROM WHAT CAN BE DRAWN, NOT FROM WHAT EXISTS.
     *
     * Cutting the biggest N pairs first and looking for addresses
     * afterwards would, on an archive with more pairs than the cap, drop a
     * pair the map knows the place of in favour of one it does not - and
     * the picture would be a sample of a sample. The addresses
     * are looked up first now and the cap is spent on pairs that will
     * actually be lines. `pairs` still counts every pair in the period, so
     * the sentence under the map can say how many the archive has never
     * placed. */
    pair AS (
        SELECT b.a, b.b, b.type, b.a_role, b.b_role, b.total, b.pairs
          FROM paired AS b
          JOIN spot AS sa ON sa.id = b.a
          JOIN spot AS sb ON sb.id = b.b
         ORDER BY b.total DESC, b.a, b.b
         LIMIT {cap}
    ),
    named AS (
        SELECT DISTINCT ON (id) id, name, type
          FROM (SELECT e.text_entity_id AS id, e.text_name AS name,
                       e.text_type AS type, count(*) AS rows_here
                  FROM processed_data.entities AS e
                 WHERE {idn_e} AND e.text_entity_id IN (SELECT id FROM ids)
                   AND e.text_name <> ''
                 GROUP BY 1, 2, 3) AS nm
         ORDER BY id, rows_here DESC, name
    )
"""

_LINE_BODY = """
    SELECT p.a, p.b, p.type, p.a_role, p.b_role, p.total, p.pairs,
           na.name AS a_name, na.type AS a_type,
           sa.lat  AS a_lat,  sa.lng  AS a_lng,  sa.address AS a_address,
           nb.name AS b_name, nb.type AS b_type,
           sb.lat  AS b_lat,  sb.lng  AS b_lng,  sb.address AS b_address
      FROM pair AS p
      LEFT JOIN named AS na ON na.id = p.a
      LEFT JOIN named AS nb ON nb.id = p.b
      LEFT JOIN spot  AS sa ON sa.id = p.a
      LEFT JOIN spot  AS sb ON sb.id = p.b
     ORDER BY p.total DESC, p.a, p.b
"""


def connections_map(conn, scope, ctx, window, resolver: ColourResolver) -> dict[str, Any]:
    """The pairs the scope's connections join, in this period, placed."""
    cte = sql.SQL(_LINE_CTES).format(
        idn_t=sqlbuild.identity("t"),
        idn_l=sqlbuild.identity("l"),
        idn_e=sqlbuild.identity("e"),
        win=sqlbuild.window_predicate("connections", window, "t"),
        scope=connection_scope(scope),
        # ON THE LINES ONLY - the module header says why the addresses the
        # ends stand at are left alone, and it is the same reason the period
        # is not applied to them.
        persp=sqlbuild.perspective_scope(
            getattr(ctx, "perspective", ""), "t",
            sqlbuild.source_id_column("connections")),
        cap=sql.Literal(MAX_LINES))
    rows = conn.execute(
        _with(scope, [cte], sql.SQL(_LINE_BODY)),
        {**sqlbuild.identity_params(ctx.project, ctx.language),
         **sqlbuild.perspective_params(getattr(ctx, "perspective", ""),
                                       getattr(ctx, "min_importance", "")),
         **sqlbuild.window_params(window), **scope.params}).fetchall()

    lines: list[dict[str, Any]] = []
    # ONE PIN PER POINT ON THE WORLD, not one per entity and not one per line
    # end. An entity in nine pairs is one place; and five entity ids all
    # called "Apple Inc." - which is what an archive of four thousand
    # documents really holds - are one address in Cupertino, not five markers
    # on the same pixel with four of them unreachable. The lines still end at
    # each entity's own coordinate, which is that same point.
    points: dict[tuple[int, int], dict[str, Any]] = {}
    for r in rows:
        name = (r["type"] or "").strip()
        colour = resolver.resolve(name or None)
        ends = []
        for side in ("a", "b"):
            eid = r[side]
            lat, lng = r[f"{side}_lat"], r[f"{side}_lng"]
            end = {
                "id": eid,
                "name": r[f"{side}_name"] or eid,
                "type": r[f"{side}_type"] or "",
                "address": r[f"{side}_address"] or "",
                # What this end IS to the other one, so the popup can write
                # the tie out as a sentence in each direction.
                "role": (r[f"{side}_role"] or "").strip(),
                "lat": None if lat is None else float(lat),
                "lng": None if lng is None else float(lng),
            }
            ends.append(end)
            if end["lat"] is None:
                continue
            # Five decimal places is about a metre - the same rounding the
            # locations map groups by, so the two pictures agree about what
            # "the same place" is.
            at = (round(end["lat"], 5), round(end["lng"], 5))
            spot = points.get(at)
            if spot is None:
                spot = points[at] = {
                    "lat": end["lat"], "lng": end["lng"],
                    "address": end["address"], "count": 0,
                    "names": [], "types": [], "seen": set(),
                }
            if eid not in spot["seen"]:
                spot["seen"].add(eid)
                if end["name"] not in spot["names"]:
                    spot["names"].append(end["name"])
                if end["type"] and end["type"] not in spot["types"]:
                    spot["types"].append(end["type"])
            spot["count"] += int(r["total"])
        lines.append({
            "from": ends[0], "to": ends[1],
            "type": name, "count": int(r["total"]),
            "group": colour.group_key, "group_name": colour.group.name,
            "colour": colour.colour,
            # Both ends need a coordinate before a line can be drawn. The
            # pair is still counted, so the sentence under the map can say
            # how many the archive has never placed.
            "drawable": ends[0]["lat"] is not None and ends[1]["lat"] is not None,
        })

    # Every pair in the period, drawable or not - the number the sentence
    # under the map uses to say how many the archive has never placed.
    total = int(rows[0]["pairs"]) if rows else 0
    drawn = [c for c in lines if c["drawable"]]
    # THE SAME TWO KINDS OF EMPTY as the locations map has, and here the
    # statement can come back with pairs it cannot draw as well as with none
    # at all - so the number is asked for only in the second case.
    unplaced = 0 if lines else _unplaced(
        conn, "connections", scope, ctx, window, connection_scope(scope))
    places = []
    for spot in sorted(points.values(),
                       key=lambda p: (-p["count"], (p["names"] or [""])[0].lower())):
        spot.pop("seen", None)
        spot["named"] = len(spot["names"])
        spot["names"] = spot["names"][:NAMES_PER_POINT]
        spot["types"] = spot["types"][:NAMES_PER_POINT]
        places.append(spot)
    return {
        "mode": "connections",
        "places": places,
        "lines": lines,
        "total": total,
        "unplaced": unplaced,
        "shown": len(lines),
        "drawn": len(drawn),
        # CAPPED MEANS "THE LIMIT WAS REACHED", not "some pairs have no
        # address". Because the cap is spent on drawable pairs only, the
        # difference between `total` and what came back is mostly pairs the
        # archive cannot place - which is a different sentence and has one
        # of its own under the map.
        "capped": len(lines) >= MAX_LINES,
        "bounds": _bounds([(p["lat"], p["lng"]) for p in places]),
    }


def _bounds(points: list[tuple[float, float]]) -> list[list[float]] | None:
    if not points:
        return None
    lats = [p[0] for p in points]
    lngs = [p[1] for p in points]
    return [[min(lats), min(lngs)], [max(lats), max(lngs)]]


# Which tab has a map under it, and what it draws. A tab that is not here
# has no map at all - the route answers 404 for it rather than an empty one,
# because "no map for this tab" and "a map with nothing on it" are different
# answers and only one of them is worth drawing a card for.
BUILDERS = {"locations": locations_map, "connections": connections_map}
