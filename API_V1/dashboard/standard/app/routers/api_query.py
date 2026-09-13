"""The Query view: relevant texts within a place, with a radius, in a date range.

Two endpoints, and the order they are called in is the point of the design:

    GET  /api/query/places?q     what does this place name mean?
    POST /api/query/search       everything found there

A place name is not an answer. "Springfield" is two towns in this archive and
twelve in the world, and a search that silently picks one of them is worse
than no search at all - the results look complete and are not. So the first
call groups the archive's addresses into candidates and says whether they are
ambiguous, and the page refuses to search until one of them has been chosen
(places.disambiguate does the grouping; the same rule is unit-tested there).

BUT IT ASKS ONLY WHAT IT CANNOT ANSWER, and that is most of the rule. A value
taken from the suggestion list already names a place - `?exact=1` says so,
and the answer is that place, never a question. A country is the country and
not a choice between the towns in it. Candidates are alternatives, never a
whole and its parts. And more than ten of them is not a question at all: they
are searched together, with `place.match` carrying the reading, and the line
says how many. places.py holds those rules and the reasoning behind them.

The search itself is one CTE chain:

    loc   the locations inside the place (or inside the circle)
    ents  the entities those locations belong to
    srcs  the sources those entities came from
      → the source documents, and the attributes of those entities

Two ways to say where, never both: a PLACE (name parts, re-derived from
text_address with the very expressions the places view uses, so the answer
does not depend on the view being fresh) or a POINT with a radius (a bounding
box on the archive's own coordinate index, then an exact haversine). Asking
for both, or for neither, is a 400: the two would have to be intersected or
unioned, and nobody would know which.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from psycopg import sql
from pydantic import BaseModel, Field

from .. import places
from ..context import ContextDep
from ..db import Database, get_db
from ..scope import grouping_for
from ..source_names import source_heading
from ..sqlbuild import (identity, identity_params, like_substring, perspective_params,
                        perspective_scope, timeline)

router = APIRouter(prefix="/api/query", tags=["query"])

PAGE_SIZE = 25
#: HOW MANY ROWS ONE REQUEST BRINGS BACK, against 25 shown at a time.
#:
#: Not one query per page: that is eight presses, eight walks over every
#: location of the place, and on a place the size of Houston eight times
#: half a minute to read two hundred rows. The archive does the
#: same work for twenty-five rows as for two hundred - the count(*) OVER ()
#: that rides on them already touches every match - so the rows are fetched
#: in blocks and the pager moves inside the block it has.
#:
#: TWO HUNDRED, NOT EVERYTHING. This dashboard is internal and the concern is
#: not somebody copying the archive; it is a browser on a modest machine
#: holding what it was sent. Two hundred rows is one JSON of a few hundred
#: kilobytes and eight pages of reading, and the page still draws only
#: twenty-five of them at once - which is the half that matters on a weak
#: device, where rendering is the cost and not the network.
BLOCK_SIZE = 200
PAGES_PER_BLOCK = BLOCK_SIZE // PAGE_SIZE
# The mini-map draws one marker per address, not per row: two hundred is more
# than a person can read and already more than a map can show without
# turning into a single blob.
MAX_MARKERS = 200
DEFAULT_RADIUS_KM = 25.0
MAX_RADIUS_KM = 20000.0
MAX_TERMS = 12


# ── What a place name means ──────────────────────────────────

# ── A place bucket is ONE area ───────────────────────────────
#
# "Rotterdam", "Rotterdam Port" and "Schiedam" are three addresses in the
# archive and one area to the person asking about it. Where somebody has said
# so on the Buckets page, this view has to agree: the term resolves to the
# bucket, the chooser offers ONE candidate rather than three, and the search
# behind it is the union of the members - each resolved exactly as if it had
# been typed alone, because a member inside a bucket may not behave
# differently from the same member in the box.


def _area_predicate(area, alias: str = "l") -> tuple[sql.Composable, dict[str, Any]]:
    frags: list[sql.Composable] = []
    params: dict[str, Any] = {}
    for i, value in enumerate(area.values):
        frag, extra = places.place_predicate(places.split_address(value), alias=alias,
                                             prefix=f"plb{i}")
        frags.append(sql.SQL("({f})").format(f=frag))
        params.update(extra)
    if not frags:
        return sql.SQL("false"), {}
    return sql.SQL(" OR ").join(frags), params


def _area_candidate(rows: list[dict[str, Any]], area) -> dict[str, Any]:
    """The bucket as one row of the chooser: its own name, the middle of its
    members and their counts added up."""
    lats = [r["float_latitude"] for r in rows if r["float_latitude"] is not None]
    lngs = [r["float_longitude"] for r in rows if r["float_longitude"] is not None]
    return {"city": area.name, "region": None, "country": None,
            "lat": sum(lats) / len(lats) if lats else None,
            "lng": sum(lngs) / len(lngs) if lngs else None,
            "locations": sum(int(r["integer_locations"] or 0) for r in rows),
            "entities": sum(int(r["integer_entities"] or 0) for r in rows),
            "addresses": len(rows), "label": area.name,
            # The same shape every other candidate has (places.Candidate),
            # so the page reads one kind of row. A bucket is searched by its
            # own name - the members are looked up again on the way in - so
            # it carries no name reading of its own.
            "address": area.name, "covers": 0, "whole": False, "search_match": None}


def _area_rows(conn, ctx, area) -> list[dict[str, Any]]:
    seen: dict[str, dict[str, Any]] = {}
    for value in area.values:
        for row in conn.execute(places.places_query(),
                                {"project": ctx.project, "language": ctx.language,
                                 "q": value.strip().lower(),
                                 "q_sub": like_substring(value)}).fetchall():
            seen.setdefault(row["text_address"], dict(row))
    return list(seen.values())


@router.get("/places")
def query_places(ctx: ContextDep, db: Database = Depends(get_db),
                 q: str = Query("", description="a city, a country or part of an address"),
                 exact: bool = Query(False, description=(
                     "the term was TAKEN FROM THE SUGGESTION LIST, so it names one "
                     "place already: resolve it and never ask back"))):
    term = (q or "").strip()
    if not term:
        return {"q": "", "match": "none", "ambiguous": False, "candidates": []}
    params = {"project": ctx.project, "language": ctx.language,
              "q": term.lower(), "q_sub": like_substring(term)}
    with db.read() as conn:
        area = grouping_for(conn, ctx.project, "location", term)
        if area is not None:
            rows = _area_rows(conn, ctx, area)
            if rows:
                # ONE candidate, and the answer says which bucket it is, so
                # nobody wonders whether they are looking at one place or
                # five merged.
                return {"q": term, "match": "bucket", "ambiguous": False,
                        "bucket": area.as_dict(),
                        "candidates": [_area_candidate(rows, area)]}
        rows = [dict(r) for r in conn.execute(places.places_query(), params).fetchall()]
    # A VALUE PICKED FROM THE SUGGESTIONS IS FINAL. The list carries the
    # archive's own addresses, so the text already IS a place and asking
    # afterwards is asking a question that has been answered - which is how
    # picking "Houston, Texas, USA" ended in "Which Houston, Texas, USA do
    # you mean?" over six places inside Houston.
    match = places.resolve_exact(rows, term) if exact else places.disambiguate(rows, term)
    return {"q": term, **match.as_dict()}


# ── The search ───────────────────────────────────────────────

class PlaceIn(BaseModel):
    """A place, either already split or as one line.

    `address` is what the page sends: the label of the candidate a person
    chose ("Springfield, Illinois, USA"). Splitting it is the server's job -
    the address rule lives in app/places.py and in sql/03-places-view.sql,
    and a third copy of it in JavaScript is exactly how the three would come
    to disagree. The split fields are there for a caller that already has
    them."""

    address: str | None = None
    city: str | None = None
    region: str | None = None
    country: str | None = None
    #: SEVERAL PLACES OF ONE NAME, SEARCHED TOGETHER. The chooser hands back
    #: one candidate for "all 221 places called United States" and for an
    #: address every match sits inside; both stand for more than one place,
    #: so the predicate is the NAME's ("city", "country", "address" - the
    #: reading places.disambiguate gave it) rather than one place's own
    #: parts. Absent for the ordinary case: one candidate, one place.
    match: str | None = None


# ── WHICH OBJECTS A SEARCH LOOKS AT ──────────────────────────
#
# The pipeline is the same whatever is asked for: locations that answer the
# place, the entities standing at them, and the objects hanging off those
# entities. What changes is the last step - which kind of object comes back.
#
# ONE KIND AT A TIME, and that is a radio button rather than a set of tick
# boxes on purpose. A search for sources AND attributes in one UNION over
# every location of a place is, on a project with two million entities,
# over a minute of database for a question nobody asked in full: somebody
# looking for connections would wait for the sources too. One
# branch is one question, and the answer arrives while the reader is still
# looking at the screen.
#
# `kind_rank` survives because the answer still carries it, but with one kind
# selected it no longer orders anything.
KINDS = ("source", "entity", "location", "connection", "market_insight",
         "rating", "attribute")
DEFAULT_KIND = "source"

#: What each kind shows as its heading, in the words the rest of the dashboard
#: uses for the same thing.
KIND_LABELS = {
    "source": "Sources",
    "entity": "Entities",
    "location": "Locations",
    "connection": "Connections",
    "market_insight": "Market Insights",
    "rating": "Ratings",
    "attribute": "Attributes",
}

#: The columns a term is matched against, per kind. Prose columns only: an id
#: column would make a search for a word find rows whose id happens to contain
#: it, which is a match a reader cannot see and cannot explain.
KIND_COLUMNS = {
    "source": ["s.text_name", "s.text_summary", "s.text_reason"],
    "entity": ["e2.text_name", "e2.text_type", "e2.text_description", "e2.text_keywords"],
    "location": ["l2.text_address", "l2.text_name", "l2.text_type", "l2.text_description"],
    "connection": ["c.text_type_parent_to_child", "c.text_type_child_to_parent", "c.text_reason"],
    "market_insight": ["m.text_topic", "m.text_reason", "m.text_long_term_outlook",
                       "m.text_short_term_outlook"],
    "rating": ["r.text_rating_name", "r.text_perspective", "r.text_rating_value", "r.text_reason"],
    "attribute": ["a.text_name", "a.text_value", "a.text_type"],
}


def _kind_of(value: str | None) -> str:
    """An unknown kind is the default rather than a 400: this is a view
    switch, and a stale bookmark should search rather than answer an error."""
    wanted = (value or "").strip().lower().replace(" ", "_")
    return wanted if wanted in KINDS else DEFAULT_KIND


class SearchRequest(BaseModel):
    terms: list[str] = Field(default_factory=list)
    # OR by default: a person who types "battery" and "pump" is looking for
    # either, and an AND that quietly returns nothing looks like an empty
    # archive. The page has a tick box for "all terms".
    match_all: bool = False
    place: PlaceIn | None = None
    lat: float | None = None
    lng: float | None = None
    radius_km: float = DEFAULT_RADIUS_KM
    date_from: str | None = None
    date_to: str | None = None
    # Capped: an offset of a billion is not a page anybody asked for, and it
    # is the one number in the request that costs the database work.
    page: int = Field(0, ge=0, le=10000)
    # Which kind of object comes back. One at a time - see KINDS above for why
    # that is the whole of the performance story as well as the feature.
    kind: str = DEFAULT_KIND


def _bad(error: str, hint: str) -> HTTPException:
    return HTTPException(400, {"error": error, "hint": hint})


def _parse_date(value: str | None, field: str) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    if not text:
        return None
    try:
        # A bare date means midnight UTC; a full timestamp is taken as given.
        stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise _bad(f"{field} is not a date", "use YYYY-MM-DD, for example 2026-01-31")
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def _where(req: SearchRequest, area=None) -> tuple[str, places.Place | None, sql.Composable, dict[str, Any], dict[str, Any]]:
    """The place predicate, and what the answer should say about it.

    `area` is the location bucket the typed place named, when it named one -
    looked up by the caller, which is where the connection is.

    Returns (kind, place, fragment, params, description)."""
    has_place = bool(req.place and (req.place.address or req.place.city
                                    or req.place.region or req.place.country))
    has_point = req.lat is not None and req.lng is not None

    if has_place and has_point:
        raise _bad("a search is either about a place or about a point, not both",
                   "clear the address, or clear the latitude and longitude")
    if not has_place and not has_point:
        raise _bad("a search needs a place or a point",
                   "type an address and choose one of the suggestions, or "
                   "give a latitude and a longitude with a distance")

    if has_place:
        # Re-split the parts through the address rule, so a candidate that
        # carries a whole address ("Cupertino, California, USA") and one that
        # carries only a country both end up as the parts the predicate wants.
        raw = req.place.address or ", ".join(
            p for p in (req.place.city, req.place.region, req.place.country) if p)
        if area is not None:
            # The chooser offered the bucket as one candidate; the search
            # behind it is the union of its members.
            frag, params = _area_predicate(area)
            return "place", places.split_address(area.values[0]), frag, params, {
                "label": area.name, "bucket": area.as_dict()}
        reading = (req.place.match or "").strip().lower()
        if reading in ("city", "country", "address"):
            # EVERY PLACE THIS NAME COVERS, in the reading the chooser gave
            # it - the rows it counted, so what the line says and what the
            # search finds are the same places. A name the archive does not
            # know is `false` inside name_predicate, never "everything".
            frag, params = places.name_predicate(reading, raw, alias="l", prefix="pn")
            return "place", places.split_address(raw), frag, params, {
                "label": raw, "reading": reading}
        place = places.split_address(raw)
        frag, params = places.place_predicate(place, alias="l", prefix="pl")
        return "place", place, frag, params, {"label": place.label}

    if not -90 <= req.lat <= 90 or not -180 <= req.lng <= 180:
        raise _bad("the coordinates are outside the world",
                   "latitude is between -90 and 90, longitude between -180 and 180")
    radius = req.radius_km if req.radius_km and req.radius_km > 0 else DEFAULT_RADIUS_KM
    if radius > MAX_RADIUS_KM:
        raise _bad("the distance is larger than the earth",
                   f"use at most {MAX_RADIUS_KM:.0f} km")
    frag, params = places.radius_predicate(req.lat, req.lng, radius, alias="l", prefix="geo")
    return "point", None, frag, params, {"label": f"{req.lat:.4f}, {req.lng:.4f} within {radius:g} km",
                                         "lat": req.lat, "lng": req.lng, "radius_km": radius}


def _scope_ctes(place_predicate: sql.Composable, perspective: str = "") -> sql.Composable:
    """loc → ents → srcs. Locations carry the coordinates the map draws, so
    loc keeps them rather than being reduced to ids straight away.

    THE PERSPECTIVE FILTER GOES ON `ents`, WHICH IS WHERE IT REACHES ALL
    SEVEN BRANCHES. Six of the seven kinds this view can list narrow
    themselves on `ents` (an entity, a location, a rating, an attribute, a
    market insight and either end of a connection are all facts about an
    entity), and `srcs` is DERIVED from ents, so the source branch is
    narrowed by the same predicate one step later. Putting it on `srcs`
    instead would have filtered exactly one of the seven.

    `ents` already carries the entity's own text_fk_source_id, so the
    judgement is looked up on the pair the row already holds - no extra
    join, and every entity of a document the reader's perspective does not
    rate highly enough drops out here rather than in seven places.
    """
    return sql.SQL("""
        WITH loc AS (
            SELECT l.text_task_id AS task_id, l.text_fk_entity_id AS id,
                   l.text_address AS address, l.text_type AS type,
                   l.float_latitude AS lat, l.float_longitude AS lng
              FROM processed_data.locations l
             WHERE {idn} AND l.text_fk_entity_id IS NOT NULL AND l.text_fk_entity_id <> ''
               AND {place}
        ),
        ents AS (
            SELECT DISTINCT e.text_task_id AS task_id, e.text_entity_id AS id,
                   e.text_name AS name, e.text_type AS type, e.text_fk_source_id AS source_id
              FROM processed_data.entities e
             WHERE {idn_e} AND (e.text_task_id, e.text_entity_id) IN (SELECT task_id, id FROM loc)
               AND {persp}
        ),
        srcs AS (
            SELECT DISTINCT task_id, source_id AS id FROM ents WHERE source_id <> ''
        )
    """).format(idn=identity("l"), idn_e=identity("e"), place=place_predicate,
                persp=perspective_scope(perspective, "e"))


def _terms_of(req: SearchRequest) -> list[str]:
    terms = [t.strip() for t in (req.terms or []) if t and t.strip()]
    if len(terms) > MAX_TERMS:
        raise _bad(f"a search takes at most {MAX_TERMS} terms",
                   "remove a few terms - fewer, wider terms find more than many narrow ones")
    return terms


def _term_predicate(columns: list[sql.Composable], terms: list[str],
                    match_all: bool, prefix: str) -> tuple[sql.Composable, dict[str, Any]]:
    """"any of these words appears in any of these columns", or "all of them".

    No terms at all is not an empty result but no restriction: "everything
    written about this place" is a question people ask, and the place is
    already a filter."""
    if not terms:
        return sql.SQL("TRUE"), {}
    params: dict[str, Any] = {}
    per_term: list[sql.Composable] = []
    for i, term in enumerate(terms):
        key = f"{prefix}_{i}"
        params[key] = like_substring(term)
        per_term.append(sql.SQL("({cols})").format(cols=sql.SQL(" OR ").join(
            sql.SQL("{c} ILIKE %({p})s").format(c=col, p=sql.SQL(key)) for col in columns)))
    joiner = sql.SQL(" AND ") if match_all else sql.SQL(" OR ")
    return sql.SQL("({p})").format(p=joiner.join(per_term)), params


def _date_predicate(table: str, alias: str, date_from: datetime | None,
                    date_to: datetime | None) -> sql.Composable:
    """The row's own date inside the range, plus the chunk prefilter on
    date_added when there is a lower bound - a document is archived after it
    is commissioned, so nothing inside the range can have arrived before it."""
    tl = timeline(table, alias)
    parts = [sql.SQL("TRUE")]
    if date_from:
        parts.append(sql.SQL("{tl} >= %(date_from)s").format(tl=tl))
        parts.append(sql.SQL("{a}.date_added >= %(date_from)s").format(a=sql.Identifier(alias)))
    if date_to:
        parts.append(sql.SQL("{tl} < %(date_to)s").format(tl=tl))
    return sql.SQL(" AND ").join(parts)


#: "A source's name, description and summary" - in the archive's columns.
#: There is no `text_description` on `processed_data.sources`: what the
#: extraction writes about a document is `text_summary` (what it says) and
#: `text_reason` (why it was judged the way it was, in prose). Both are the
#: document's own words about itself, so both are searched; a word a reader
#: remembers from a source is as likely to be in the one as in the other.
#:
#: `text_keywords` is deliberately NOT here. It is the extraction's list of
#: evaluational keywords, not prose, and a keyword list matches whole
#: subject areas: on the test archive `text_keywords ILIKE '%battery%'`
#: matches 13 of 13 sources while `text_summary` matches 3. Searching it
#: would turn a search into "everything", which is the one answer a search
#: box may never give. Keywords have their own place in the Diagrams view.
_SOURCE_COLUMNS = ["s.text_name", "s.text_summary", "s.text_reason"]
_ATTRIBUTE_COLUMNS = ["a.text_name", "a.text_value", "a.text_description"]


#: One SELECT per kind, all answering the same nine columns so the wrapper and
#: the page do not have to know which one ran. `about` is the entity the row
#: hangs off, because every one of these is a fact about an entity and a row
#: without its subject is a fact nobody can place.
#:
#: A DOCUMENT IS NEVER TITLED BY ITS ID: `sources.text_name` is `src_1127664`
#: or an md5 in every row of this archive, so the source branch selects the raw
#: column and the route replaces it with the domain and the slug
#: (app/source_names.py). The COLUMN is still searched, so somebody holding an
#: id can still paste it; only the heading changes.
_KIND_BODIES = {
    "source": """
        SELECT 'source' AS kind, 1 AS kind_rank,
               s.text_task_id AS task_id, s.bigint_id AS row_id,
               s.text_name AS title,
               nullif(concat_ws(', ', nullif(s.text_summary, ''),
                                nullif(s.text_reason, '')), '') AS body,
               s.text_uri AS uri,
               s.text_type AS type,
               (SELECT jsonb_agg(DISTINCT jsonb_build_array(en.name, coalesce(en.type, '')))
                  FROM ents en
                 WHERE en.task_id = s.text_task_id AND en.source_id = s.text_source_id) AS about,
               {tl} AS row_date
          FROM processed_data.sources s
         WHERE {idn}
           AND (s.text_task_id, s.text_source_id) IN (SELECT task_id, id FROM srcs)
           AND {terms} AND {dates}
    """,
    "entity": """
        SELECT 'entity', 1,
               e2.text_task_id, e2.bigint_id,
               e2.text_name,
               nullif(concat_ws(', ', nullif(e2.text_description, ''),
                                nullif(e2.text_keywords, '')), ''),
               '',
               e2.text_type,
               jsonb_build_array(jsonb_build_array(e2.text_name, coalesce(e2.text_type, ''))),
               {tl}
          FROM processed_data.entities e2
         WHERE {idn}
           AND (e2.text_task_id, e2.text_entity_id) IN (SELECT task_id, id FROM ents)
           AND {terms} AND {dates}
    """,
    "location": """
        SELECT 'location', 1,
               l2.text_task_id, l2.bigint_id,
               coalesce(nullif(l2.text_address, ''), nullif(l2.text_name, ''), 'Address unknown'),
               nullif(l2.text_description, ''),
               '',
               l2.text_type,
               (SELECT jsonb_agg(DISTINCT jsonb_build_array(en.name, coalesce(en.type, '')))
                  FROM ents en
                 WHERE en.task_id = l2.text_task_id AND en.id = l2.text_fk_entity_id),
               {tl}
          FROM processed_data.locations l2
         WHERE {idn}
           AND (l2.text_task_id, l2.text_fk_entity_id) IN (SELECT task_id, id FROM ents)
           AND {terms} AND {dates}
    """,
    # BOTH ENDS, and either end may be the one standing at the place. A
    # connection whose child is here and whose parent is elsewhere is still a
    # connection of this place, and dropping it would make the count depend on
    # which way round the extraction happened to write the pair.
    # IDS HERE, NAMES IN PYTHON. Looking both ends up with a correlated
    # subquery over processed_data.entities would run those two lookups for
    # all twelve thousand matches rather than for the twenty-five on the
    # page, because `count(*) OVER ()` makes every matching row part of the
    # window - and not finish in five minutes. The ids come back instead and the
    # route resolves the fifty on the page in one query, the same way a source
    # gets its heading.
    #
    # BOTH ENDS MATCH: a connection whose child stands at the place and whose
    # parent is elsewhere is still a connection of this place, and taking only
    # one side would make the count depend on which way round the extraction
    # happened to write the pair. EXISTS rather than IN, because the planner
    # can stop at the first hit.
    "connection": """
        SELECT 'connection', 1,
               c.text_task_id, c.bigint_id,
               concat_ws(' → ', c.text_fk_parent_entity_id, c.text_fk_child_entity_id),
               nullif(concat_ws(', ',
                   nullif(concat_ws(' / ', nullif(c.text_type_parent_to_child, ''),
                                    nullif(c.text_type_child_to_parent, '')), ''),
                   nullif(c.text_reason, '')), ''),
               '',
               c.text_type_parent_to_child,
               (SELECT jsonb_agg(DISTINCT jsonb_build_array(en.name, coalesce(en.type, '')))
                  FROM ents en
                 WHERE en.task_id = c.text_task_id
                   AND en.id IN (c.text_fk_parent_entity_id, c.text_fk_child_entity_id)),
               {tl}
          FROM processed_data.connections c
         WHERE {idn}
           AND (EXISTS (SELECT 1 FROM ents en
                         WHERE en.task_id = c.text_task_id
                           AND en.id = c.text_fk_parent_entity_id)
             OR EXISTS (SELECT 1 FROM ents en
                         WHERE en.task_id = c.text_task_id
                           AND en.id = c.text_fk_child_entity_id))
           AND {terms} AND {dates}
    """,
    "market_insight": """
        SELECT 'market_insight', 1,
               m.text_task_id, m.bigint_id,
               coalesce(nullif(m.text_topic, ''), 'Topic unknown'),
               nullif(concat_ws(', ',
                   nullif(concat_ws(', ',
                       nullif(m.text_short_term_outlook, ''),
                       nullif(m.text_long_term_outlook, '')), ''),
                   nullif(m.text_reason, '')), ''),
               '',
               m.text_topic,
               (SELECT jsonb_agg(DISTINCT jsonb_build_array(en.name, coalesce(en.type, '')))
                  FROM ents en
                 WHERE en.task_id = m.text_task_id AND en.id = m.text_fk_entity_id),
               {tl}
          FROM processed_data.market_insights m
         WHERE {idn}
           AND (m.text_task_id, m.text_fk_entity_id) IN (SELECT task_id, id FROM ents)
           AND {terms} AND {dates}
    """,
    "rating": """
        SELECT 'rating', 1,
               r.text_task_id, r.bigint_id,
               concat_ws(': ', nullif(r.text_rating_name, ''), nullif(r.text_rating_value, '')),
               nullif(concat_ws(', ', nullif(r.text_perspective, ''),
                                nullif(r.text_reason, '')), ''),
               '',
               r.text_rating_name,
               (SELECT jsonb_agg(DISTINCT jsonb_build_array(en.name, coalesce(en.type, '')))
                  FROM ents en
                 WHERE en.task_id = r.text_task_id AND en.id = r.text_fk_entity_id),
               {tl}
          FROM processed_data.ratings r
         WHERE {idn}
           AND (r.text_task_id, r.text_fk_entity_id) IN (SELECT task_id, id FROM ents)
           AND {terms} AND {dates}
    """,
    "attribute": """
        SELECT 'attribute', 2,
               a.text_task_id, a.bigint_id,
               a.text_name,
               nullif(concat_ws(' ', nullif(a.text_value, ''), nullif(a.text_unit, '')), ''),
               '',
               a.text_type,
               (SELECT jsonb_agg(DISTINCT jsonb_build_array(en.name, coalesce(en.type, '')))
                  FROM ents en
                 WHERE en.task_id = a.text_task_id AND en.id = a.text_fk_entity_id),
               {tl}
          FROM processed_data.attributes a
         WHERE {idn}
           AND (a.text_task_id, a.text_fk_entity_id) IN (SELECT task_id, id FROM ents)
           AND {terms} AND {dates}
    """,
}

#: The alias and the table each branch reads, for identity(), timeline() and
#: the date predicate. One place, so a branch cannot be filtered on one table
#: and dated by another.
_KIND_SOURCE = {
    "source": ("sources", "s"),
    "entity": ("entities", "e2"),
    "location": ("locations", "l2"),
    "connection": ("connections", "c"),
    "market_insight": ("market_insights", "m"),
    "rating": ("ratings", "r"),
    "attribute": ("attributes", "a"),
}


def _results_statement(req: SearchRequest, terms: list[str], place_predicate: sql.Composable,
                       date_from: datetime | None, date_to: datetime | None,
                       perspective: str = "") -> tuple[sql.Composable, dict[str, Any]]:
    kind = _kind_of(req.kind)
    table, alias = _KIND_SOURCE[kind]
    predicate, params = _term_predicate(
        [sql.SQL(c) for c in KIND_COLUMNS[kind]], terms, req.match_all, "tk")

    body = sql.SQL(_KIND_BODIES[kind]).format(
        tl=timeline(table, alias),
        idn=identity(alias),
        terms=predicate,
        dates=_date_predicate(table, alias, date_from, date_to))

    # THE COLUMN NAMES ARE ON THE ALIAS, not repeated in seven SELECTs. Under
    # the old UNION the first branch named the columns for all of them; each
    # branch now stands on its own, and a SELECT of bare expressions has
    # columns nothing can ORDER BY. An alias list names them once, by position,
    # and a branch that returns them in the wrong order is a mistake this makes
    # loud rather than silent - the dates would end up under `title`.
    statement = sql.SQL("""
        {ctes}
        SELECT f.*, count(*) OVER () AS total
          FROM ({body}) AS f(kind, kind_rank, task_id, row_id, title, body,
                             uri, type, about, row_date)
         ORDER BY f.row_date DESC NULLS LAST, f.title
         LIMIT %(limit)s OFFSET %(offset)s
    """).format(ctes=_scope_ctes(place_predicate, perspective), body=body)
    return statement, params


_MARKERS_SQL = """
    SELECT DISTINCT en.name AS entity, en.type AS entity_type,
           loc.address, loc.type, loc.lat, loc.lng
      FROM loc JOIN ents en ON en.task_id = loc.task_id AND en.id = loc.id
     WHERE loc.lat IS NOT NULL AND loc.lng IS NOT NULL
     ORDER BY en.name
     LIMIT %(markers)s
"""


def _pairs(value) -> list[tuple[str, str]]:
    """The `about` column as (name, type), whatever shape it came back in.

    jsonb_agg answers NULL when a row is about nobody - an orphan location,
    a document whose entities have no coordinates - and a list of two-element
    arrays otherwise. One place to make that a list, so seven branches do not
    each have to be careful."""
    if not value:
        return []
    out: list[tuple[str, str]] = []
    for one in value:
        if isinstance(one, (list, tuple)) and one:
            out.append((str(one[0] or ""), str(one[1] if len(one) > 1 and one[1] else "")))
        elif one:
            out.append((str(one), ""))
    # DISTINCT in SQL is per pair; two spellings of one name with the same
    # type cannot happen, but the order jsonb_agg returns is not defined and
    # a card that lists its subjects differently on every reload reads as a
    # card that changed.
    return sorted(set(out))


def matched_terms(item: dict[str, Any], terms: list[str], extra: str = "") -> list[str]:
    """Which of the terms this row actually contains, computed here rather
    than in SQL: the page marks them in the text, and the database would have
    to return one boolean column per term to say the same thing.

    `extra` is text the row was FOUND by but is not shown with - a
    document's stored `text_name`, which is an identifier in this archive
    and is therefore replaced in the heading (app/source_names.py). Without
    it, somebody who pastes an id in gets a card with nothing marked, which
    reads as a search that matched by accident.
    """
    hay = " ".join(str(item.get(k) or "") for k in ("title", "body", "about", "uri")).lower()
    if extra:
        hay = f"{hay} {extra.lower()}"
    return [t for t in terms if t.lower() in hay]


def _place_and_dates(req: SearchRequest, ctx, db):
    """The half of a search request every route works out the same way."""
    area = None
    named = (req.place.address or "").strip() if req.place else ""
    if named:
        with db.read() as conn:
            area = grouping_for(conn, ctx.project, "location", named)
    kind, place, place_predicate, place_params, described = _where(req, area)
    date_from = _parse_date(req.date_from, "date_from")
    date_to = _parse_date(req.date_to, "date_to")
    if date_to is not None and date_to.hour == 0 and date_to.minute == 0 and date_to.second == 0:
        date_to = date_to + timedelta(days=1)
    if date_from and date_to and date_to <= date_from:
        raise _bad("the date range ends before it starts",
                   "swap the two dates, or clear one of them")
    return kind, place, place_predicate, place_params, described, date_from, date_to


#: What a row hangs off, per kind: the column that names its entity or
#: entities, and the one that names the document. Every table but `sources`
#: carries `text_fk_source_id`; `sources` IS the document.
_ROW_LINKS = {
    "source": (None, None),
    "entity": (["text_entity_id"], "text_fk_source_id"),
    "location": (["text_fk_entity_id"], "text_fk_source_id"),
    "connection": (["text_fk_parent_entity_id", "text_fk_child_entity_id"], "text_fk_source_id"),
    "market_insight": (["text_fk_entity_id"], "text_fk_source_id"),
    "rating": (["text_fk_entity_id"], "text_fk_source_id"),
    "attribute": (["text_fk_entity_id"], "text_fk_source_id"),
}

#: Columns worth reading in a detail popup, per table. Named rather than
#: `SELECT *`: an archive row carries job ids, evidence blobs and internal
#: timestamps, and a popup that showed all of it would be a table dump.
_DETAIL_COLUMNS = {
    "sources": ["text_name", "text_uri", "text_type", "text_summary", "text_reason",
                "text_language", "date_commissioned", "date_added"],
    "entities": ["text_name", "text_type", "text_description", "text_keywords",
                 "text_relation_to_source", "date_added"],
    "locations": ["text_address", "text_name", "text_type", "text_description",
                  "float_latitude", "float_longitude", "date_added"],
    "connections": ["text_type_parent_to_child", "text_type_child_to_parent",
                    "text_reason", "text_relation_to_source", "date_added"],
    "market_insights": ["text_topic", "text_short_term_outlook", "text_long_term_outlook",
                        "text_reason", "date_added"],
    "ratings": ["text_rating_name", "text_rating_value", "text_perspective",
                "text_reason", "date_added"],
    "attributes": ["text_name", "text_value", "text_unit", "text_type", "date_added"],
}


class DetailRequest(BaseModel):
    kind: str = DEFAULT_KIND
    task_id: str = ""
    row_id: int = 0


def _one_row(conn, ctx, table: str, columns: list[str], where: sql.Composable,
             params: dict[str, Any]) -> dict[str, Any] | None:
    statement = sql.SQL("SELECT {cols} FROM processed_data.{table} t WHERE {idn} AND {where}"
                        ).format(cols=sql.SQL(", ").join(sql.Identifier("t", c) for c in columns),
                                 table=sql.Identifier(table), idn=identity("t"), where=where)
    row = conn.execute(statement, {**identity_params(ctx.project, ctx.language), **params}).fetchone()
    return dict(row) if row else None


def _readable(row: dict[str, Any] | None) -> dict[str, Any]:
    """The archive's column names are not words. `text_short_term_outlook`
    becomes "short term outlook", dates become strings, and anything empty
    is dropped - a popup of blank labels says less than a shorter one."""
    if not row:
        return {}
    out: dict[str, Any] = {}
    for key, value in row.items():
        if value in (None, "", []):
            continue
        # THE PLUMBING STAYS OUT. `text_fk_source_id` is how the row is joined
        # up, not something anybody reads: the popup shows the document it
        # points AT, which is worth more than the id it points at it with.
        if key.startswith("text_fk_") or key.endswith("_id"):
            continue
        label = key.split("_", 1)[1].replace("_", " ") if "_" in key else key
        out[label] = value.isoformat() if hasattr(value, "isoformat") else value
    return out


@router.post("/detail")
def detail(req: DetailRequest, ctx: ContextDep, db: Database = Depends(get_db)):
    """ONE ROW, IN FULL, WITH WHO AND WHERE IT CAME FROM.

    Its own request on purpose: this is what a reader asks for about ONE of
    two hundred rows, and putting the entity's description and the document's
    summary into every row of every search would be two hundred times the
    payload for the one somebody opens.

    Three small reads, all keyed by id: the row itself, the entities it is
    about, and the document it was taken from. No scan, no scope - the ids
    are already in hand, which is why this is quick where the search is not.
    """
    kind = _kind_of(req.kind)
    table, _ = _KIND_SOURCE[kind]
    entity_cols, source_col = _ROW_LINKS[kind]
    wanted = list(_DETAIL_COLUMNS[table])
    for extra in ([*(entity_cols or []), source_col] if kind != "source" else ["text_source_id"]):
        if extra and extra not in wanted:
            wanted.append(extra)

    with db.read() as conn:
        row = _one_row(conn, ctx, table, wanted,
                       sql.SQL("t.text_task_id = %(task)s AND t.bigint_id = %(row)s"),
                       {"task": req.task_id, "row": req.row_id})
        if not row:
            raise HTTPException(404, {"error": "that row is not in the archive any more",
                                      "hint": "search again - the archive may have been rebuilt"})

        entities: list[dict[str, Any]] = []
        ids = ([row.get("text_source_id")] if kind == "source"
               else [row.get(c) for c in (entity_cols or [])])
        if kind == "source":
            found = conn.execute(sql.SQL("""
                SELECT DISTINCT ON (e.text_entity_id) {cols}, e.text_entity_id
                  FROM processed_data.entities e
                 WHERE {idn} AND e.text_task_id = %(task)s AND e.text_fk_source_id = %(src)s
                 ORDER BY e.text_entity_id, e.bigint_id
                 LIMIT 25
            """).format(cols=sql.SQL(", ").join(
                            sql.Identifier("e", c) for c in _DETAIL_COLUMNS["entities"]),
                        idn=identity("e")),
                {**identity_params(ctx.project, ctx.language),
                 "task": req.task_id, "src": row.get("text_source_id") or ""}).fetchall()
            entities = [dict(r) for r in found]
        else:
            for one in [i for i in ids if i]:
                got = _one_row(conn, ctx, "entities", _DETAIL_COLUMNS["entities"],
                               sql.SQL("t.text_task_id = %(task)s AND t.text_entity_id = %(id)s"),
                               {"task": req.task_id, "id": one})
                if got:
                    entities.append(got)

        source = None
        source_id = (row.get("text_source_id") if kind == "source"
                     else row.get(source_col) if source_col else None)
        if not source_id and entities:
            # A row that names no document takes its entity's: an attribute
            # is written down in the same document its entity was.
            source_id = None
        if source_id:
            source = _one_row(conn, ctx, "sources", _DETAIL_COLUMNS["sources"] + ["text_source_id"],
                              sql.SQL("t.text_task_id = %(task)s AND t.text_source_id = %(id)s"),
                              {"task": req.task_id, "id": source_id})

    heading = None
    if source:
        head = source_heading(source.get("text_name") or "", source.get("text_uri") or "")
        heading = {"text": head["text"], "derived": head["derived"]}

    return {
        "kind": kind,
        "label": KIND_LABELS[kind],
        "row": _readable(row),
        "entities": [{"name": e.get("text_name") or "", "type": e.get("text_type") or "",
                      "detail": _readable(e)} for e in entities],
        "source": None if not source else {
            "heading": heading["text"] if heading else "",
            "derived": bool(heading and heading["derived"]),
            "uri": source.get("text_uri") or "",
            "detail": _readable(source),
        },
    }


@router.post("/search")
def search(req: SearchRequest, ctx: ContextDep, db: Database = Depends(get_db)):
    terms = _terms_of(req)
    # A PLACE BUCKET IS ONE AREA, here as in the chooser. Looked up first,
    # because the predicate the whole statement is built from depends on it.
    area = None
    named = (req.place.address or "").strip() if req.place else ""
    if named:
        with db.read() as conn:
            area = grouping_for(conn, ctx.project, "location", named)
    kind, place, place_predicate, place_params, described = _where(req, area)
    date_from = _parse_date(req.date_from, "date_from")
    date_to = _parse_date(req.date_to, "date_to")
    if date_to is not None and date_to.hour == 0 and date_to.minute == 0 and date_to.second == 0:
        # A day given as an end is meant to be included: "to 31 January"
        # means up to the end of the 31st, not up to its first second.
        date_to = date_to + timedelta(days=1)
    if date_from and date_to and date_to <= date_from:
        raise _bad("the date range ends before it starts",
                   "swap the two dates, or clear one of them")
    page = max(0, int(req.page or 0))
    # `kind` above is the PLACE's kind - place or point - and is not this
    # one. Two things in one function called kind is how a rename goes wrong
    # quietly, so the object's kind carries the longer name.
    object_kind = _kind_of(req.kind)

    # THE BLOCK THE ASKED-FOR PAGE FALLS IN, not the page. The caller still
    # names a page - that is what a link carries and what a reader counts in
    # - and gets back the two hundred rows around it, so the seven presses
    # after this one cost nothing.
    block = page // PAGES_PER_BLOCK
    statement, term_params = _results_statement(req, terms, place_predicate, date_from,
                                                date_to, ctx.perspective)
    params = {**identity_params(ctx.project, ctx.language),
              **perspective_params(ctx.perspective, ctx.min_importance),
              **place_params, **term_params,
              "date_from": date_from, "date_to": date_to,
              "limit": BLOCK_SIZE, "offset": block * BLOCK_SIZE, "markers": MAX_MARKERS}

    with db.read() as conn:
        rows = [dict(r) for r in conn.execute(statement, params).fetchall()]
        if not rows and page:
            # A page past the end has no rows, and the count(*) OVER() that
            # rides on them is then missing too - the pager would say "0
            # results" the moment somebody clicked one page too far. Ask for
            # the first row again, only for its counts.
            first = conn.execute(statement, {**params, "offset": 0, "limit": 1}).fetchall()
            counts_only = [dict(r) for r in first]
        else:
            counts_only = rows
        markers = [dict(r) for r in conn.execute(
            sql.SQL("{ctes} {body}").format(
                ctes=_scope_ctes(place_predicate, ctx.perspective),
                body=sql.SQL(_MARKERS_SQL)), params).fetchall()]

    total = int(counts_only[0]["total"]) if counts_only else 0

    # THE NAMES OF THE TWO ENDS, for the twenty-five rows on this page and not
    # for the twelve thousand behind them. The branch returns entity ids joined
    # by an arrow (see _KIND_BODIES); this turns them into names in one query,
    # the same shape source_heading() gives a document its heading.
    ends: dict[tuple[str, str], str] = {}
    if object_kind == "connection" and rows:
        wanted: set[tuple[str, str]] = set()
        for r in rows:
            for part in (r["title"] or "").split(" → "):
                if part:
                    wanted.add((r["task_id"], part))
        if wanted:
            with db.read() as conn:
                named = conn.execute(sql.SQL("""
                    SELECT e.text_task_id AS task_id, e.text_entity_id AS id,
                           min(e.text_name) AS name
                      FROM processed_data.entities e
                     WHERE {idn} AND (e.text_task_id, e.text_entity_id)
                           IN (SELECT * FROM unnest(%(tasks)s::text[], %(ids)s::text[]))
                     GROUP BY 1, 2
                """).format(idn=identity("e")),
                    {**identity_params(ctx.project, ctx.language),
                     "tasks": [t for t, _ in wanted], "ids": [i for _, i in wanted]}).fetchall()
            ends = {(r["task_id"], r["id"]): r["name"] for r in named if r["name"]}

    items = []
    for r in rows:
        title = r["title"] or ""
        derived = False
        if r["kind"] == "source":
            # A DOCUMENT IS NEVER CALLED BY ITS ID. `sources.text_name` is
            # `src_1127664` or an md5 in every row of this archive, so the
            # card is headed with the domain and the address's own slug
            # instead (app/source_names.py). The COLUMN is still searched -
            # somebody who has an id in hand can still paste it - only the
            # heading changes, because that is the half a reader reads.
            head = source_heading(title, r["uri"])
            title, derived = head["text"], head["derived"]
        elif r["kind"] == "connection":
            # An id that no entity row answers to keeps the id: it is still a
            # connection, and a blank end would read as a connection to
            # nothing rather than to something the archive did not name.
            parts = [ends.get((r["task_id"], part), part)
                     for part in (title.split(" → ") if title else [])]
            title = " → ".join(parts)
            derived = True
            # THE BRANCH ALREADY NAMED BOTH ENDS, with their types; the ids
            # above are only what the TITLE is built from. The fallback is
            # for a connection whose ends are not in `ents` - one side stands
            # at the place, the other is elsewhere - where the subquery has
            # nothing to say and the resolved names are all there is.
            if parts and not r["about"]:
                r = {**r, "about": [[part, ""] for part in parts]}
        item = {
            "kind": r["kind"],
            "id": f"{r['task_id']}:{r['row_id']}",
            "title": title,
            "derived": derived,
            "body": r["body"] or "",
            "uri": r["uri"] or "",
            "type": r["type"] or "",
            # WHO THE ROW IS ABOUT, with the type each of them carries.
            #
            # A list of pairs, not a joined string. The page builds its own
            # "what this is about" list out of the rows it was sent
            # (static/js/query.js) - one search, no second question to the
            # archive - and a list of names with the types glued off would be
            # a list nobody could tell two Apples apart in. `about` stays as
            # the sentence the card prints, built from the same pairs so the
            # two can never disagree.
            "subjects": [{"name": n, "type": t} for n, t in _pairs(r["about"])],
            "about": ", ".join(n for n, _ in _pairs(r["about"])),
            "date": r["row_date"].isoformat() if r["row_date"] else None,
        }
        item["matched_terms"] = matched_terms(item, terms, extra=r["title"] or "")
        items.append(item)

    # ONE GROUP, because one kind was asked for. The shape is a list of groups
    # all the same, so the page that drew two of them draws one without
    # knowing anything new - and the heading is the word the rest of the
    # dashboard uses for those rows. "Sources" is what the Dashboard card, the
    # Diagrams tab and the Events rows call an archived document; calling the
    # same rows "Documents" here left the archive with two names.
    groups = [{"kind": object_kind, "label": KIND_LABELS[object_kind], "items": items}]
    return {
        "where": {"kind": kind, "place": dataclasses.asdict(place) if place else None, **described},
        "terms": terms, "match_all": req.match_all,
        "date_from": date_from.isoformat() if date_from else None,
        "date_to": req.date_to or None,
        "page": page, "page_size": PAGE_SIZE, "total": total,
        "pages": (total + PAGE_SIZE - 1) // PAGE_SIZE,
        # WHICH ROWS THESE ARE. `block_first` is the position of the first
        # item in the whole answer, so the page can say "51 to 75 of 4,901"
        # and can tell whether the page it wants is one it already holds.
        "block": block, "block_size": BLOCK_SIZE, "block_first": block * BLOCK_SIZE,
        "pages_per_block": PAGES_PER_BLOCK,
        "counts": {object_kind: total},
        # What was searched, so a reloaded page and a shared link show the
        # same answer rather than the default one.
        "object_kind": object_kind,
        "groups": [g for g in groups if g["items"]],
        "places": [
            {"entity": m["entity"], "entity_type": m["entity_type"], "address": m["address"],
             "type": m["type"], "lat": m["lat"], "lng": m["lng"]}
            for m in markers
        ],
        "project": ctx.project, "language": ctx.language,
    }
