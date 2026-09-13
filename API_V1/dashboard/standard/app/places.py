"""Places: addresses split into parts, disambiguation, and distance in SQL.

The archive is the only geocoder. An address is whatever the extraction wrote
into processed_data.locations.text_address - "Springfield, Illinois, USA",
"Rotterdam, Netherlands", "Linjiang" - and the coordinates next to it are the
ones the dashboard can draw. Nothing here calls out to a map service.

The split rule lives in exactly two places, this file and sql/03-places-view.sql,
and tests/unit/test_places.py checks that the SQL text here is the SQL text
there. A search re-derives the parts from processed_data.locations with these
expressions (place_predicate), so it does not depend on the view being fresh.

Distance is a pure-SQL haversine on top of a bounding box: the box uses the
plain (latitude, longitude) index the archive already has, the haversine
trims the corners. cube/earthdistance exist in the image but are not
installed, and the dashboard must not need CREATE EXTENSION.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from psycopg import sql

from .sqlbuild import like_substring

# ── The address rule ─────────────────────────────────────────

# {a} is the locations alias. Byte-for-byte the expressions in 03-places-view.sql.
ADDRESS_PARTS_SQL = (
    "array_remove(ARRAY(SELECT btrim(x) FROM unnest(string_to_array({a}.text_address, ',')) AS x), '')"
)
CITY_SQL = "parts[1]"
REGION_SQL = "CASE WHEN cardinality(parts) >= 3 THEN parts[cardinality(parts) - 1] END"
COUNTRY_SQL = "CASE WHEN cardinality(parts) >= 2 THEN parts[cardinality(parts)] END"


@dataclass(frozen=True)
class Place:
    city: str | None = None
    region: str | None = None
    country: str | None = None

    @property
    def parts(self) -> tuple[str, ...]:
        return tuple(p for p in (self.city, self.region, self.country) if p)

    @property
    def label(self) -> str:
        return ", ".join(self.parts)


def split_address(address: str | None) -> Place:
    """City / region / country from a comma-separated address.

    >>> split_address("Springfield, Illinois, USA")
    Place(city='Springfield', region='Illinois', country='USA')
    >>> split_address("Rotterdam, Netherlands")
    Place(city='Rotterdam', region=None, country='Netherlands')
    >>> split_address("Linjiang")
    Place(city='Linjiang', region=None, country=None)
    >>> split_address(" 1 Infinite Loop , Cupertino, California, USA ")
    Place(city='1 Infinite Loop', region='California', country='USA')
    >>> split_address("Rotterdam, , Netherlands")
    Place(city='Rotterdam', region=None, country='Netherlands')
    >>> split_address("")
    Place(city=None, region=None, country=None)
    """
    if not address:
        return Place()
    parts = [p.strip() for p in address.split(",")]
    parts = [p for p in parts if p]
    if not parts:
        return Place()
    city = parts[0]
    country = parts[-1] if len(parts) >= 2 else None
    region = parts[-2] if len(parts) >= 3 else None
    return Place(city, region, country)


# ── Disambiguation ───────────────────────────────────────────
#
# THE CHOOSER ASKS ONLY WHAT IT CANNOT ANSWER.
#
# It exists for one case - two towns called Springfield, in two countries,
# and nothing in the text to say which one - and it must not fire for every
# case, including the ones where there is nothing to choose. The worst of
# those is a question asked AFTER the answer has been given: somebody picks
# "Houston, Texas, USA" out of the suggestion list and is asked which
# Houston they meant, then offered NRG Stadium, Rice University and NASA's
# Johnson Space Center. None of those is a different Houston. They are
# places INSIDE Houston, and the question has no answer because it is the
# wrong question.
#
# So, in this order:
#
#   * A value picked from the SUGGESTION LIST is final - resolve_exact()
#     below, and the page tells this module which path it is on.
#   * A term that names a COUNTRY is the country. The towns in it are the
#     RESULT, not a set of alternatives, and asking "which United States do
#     you mean?" over 221 addresses is a wall of options for a question the
#     person already answered. Asked BEFORE the cities on purpose: this
#     archive writes addresses at both ends ("United States, California,
#     Los Angeles" as readily as "Houston, Texas, USA"), so the split rule
#     reads a country as the city of a few hundred rows.
#   * Candidates must be ALTERNATIVES, never a whole and its parts. A
#     candidate whose address ends with another candidate's address is
#     inside it, and searching the area already reaches it, so it goes.
#   * More than MAX_CANDIDATES survive that: do not ask at all. Search
#     every one of them and say how many. A chooser that scrolls is a
#     failure of the chooser.

#: More candidates than this is not a choice any more, it is a list.
MAX_CANDIDATES = 10

#: The containment pass compares every candidate with every other one, and
#: the term is whatever somebody typed: "a" matches every address in the
#: archive - 68,774 of them in the one this was measured on, which is 4.7
#: billion comparisons for a question that has already been answered. Past
#: this many there is no chooser to build either way (see MAX_CANDIDATES),
#: so the work is not done at all.
CONTAINMENT_LIMIT = 200


def address_parts(address: str | None) -> tuple[str, ...]:
    """An address as its parts, blanks removed.

    >>> address_parts(" NRG Stadium, Houston , Texas, USA ")
    ('NRG Stadium', 'Houston', 'Texas', 'USA')
    >>> address_parts(None)
    ()
    """
    return tuple(p.strip() for p in (address or "").split(",") if p.strip())


def _fold(parts: tuple[str, ...]) -> list[str]:
    return [p.lower() for p in parts]


def inside(inner: tuple[str, ...], outer: tuple[str, ...]) -> bool:
    """`inner` is a place INSIDE `outer`: its address ends with the other's.

    Whole parts, not characters - "New Houston, Texas, USA" ends with the
    LETTERS of "Houston, Texas, USA" and is a different town.

    >>> inside(address_parts("NRG Stadium, Houston, Texas, USA"),
    ...        address_parts("Houston, Texas, USA"))
    True
    >>> inside(address_parts("New Houston, Texas, USA"),
    ...        address_parts("Houston, Texas, USA"))
    False
    >>> inside(address_parts("Houston, Texas, USA"), address_parts("Houston, Texas, USA"))
    False
    """
    if not outer or len(inner) <= len(outer):
        return False
    return _fold(inner[len(inner) - len(outer):]) == _fold(outer)


def holds(parts: tuple[str, ...], needle: tuple[str, ...]) -> bool:
    """This address contains the term as WHOLE PARTS - so the term names a
    place the address is in, at either end of it.

    >>> holds(address_parts("United States, California, Los Angeles"),
    ...       address_parts("United States, California"))
    True
    >>> holds(address_parts("NRG Stadium, Houston, Texas, USA"),
    ...       address_parts("Houston, Texas, USA"))
    True
    >>> holds(address_parts("1 Infinite Loop, Cupertino, California, USA"),
    ...       address_parts("Infinite"))
    False
    """
    n, m = len(parts), len(needle)
    if not m or m > n:
        return False
    low, want = _fold(parts), _fold(needle)
    return any(low[i:i + m] == want for i in range(n - m + 1))


@dataclass
class Candidate:
    city: str | None
    region: str | None
    country: str | None
    lat: float | None
    lng: float | None
    locations: int = 0
    entities: int = 0
    addresses: int = 0
    #: The line a search is asked with - the address rule splits it again.
    address: str = ""
    #: That address, split. What containment is decided on; not published.
    parts: tuple[str, ...] = ()
    #: How many distinct places this ONE row stands for. 0: it is one place.
    covers: int = 0
    #: True when the term is the AREA the matches sit inside (a country, or
    #: an address every match is within) rather than one of several places
    #: that happen to share a name.
    whole: bool = False
    #: Set when the row stands for SEVERAL places: the reading the search
    #: has to use (name_predicate), sent back as `place.match`.
    search_match: str | None = None

    @property
    def label(self) -> str:
        head = self.city or self.country or ""
        tail = ", ".join(p for p in (self.region, self.country) if p and p != head)
        return f"{head} - {tail}" if tail else head

    def as_dict(self) -> dict[str, Any]:
        return {
            "city": self.city, "region": self.region, "country": self.country,
            "lat": self.lat, "lng": self.lng, "locations": self.locations,
            "entities": self.entities, "addresses": self.addresses, "label": self.label,
            "address": self.address or self.label, "covers": self.covers,
            "whole": self.whole, "search_match": self.search_match,
        }


@dataclass
class PlaceMatch:
    match: str                      # city | country | address | none
    candidates: list[Candidate] = field(default_factory=list)

    @property
    def ambiguous(self) -> bool:
        return len(self.candidates) > 1

    def as_dict(self) -> dict[str, Any]:
        return {"match": self.match, "ambiguous": self.ambiguous,
                "candidates": [c.as_dict() for c in self.candidates]}


def places_query(alias: str = "p") -> sql.Composable:
    """The rows disambiguate() groups: every place of the project/language
    whose city, country or full address matches the term (params: project,
    language, q, q_sub)."""
    return sql.SQL(
        "SELECT {a}.text_city, {a}.text_region, {a}.text_country, {a}.text_address, "
        "{a}.float_latitude, {a}.float_longitude, {a}.integer_locations, {a}.integer_entities "
        "FROM dashboard.places AS {a} "
        "WHERE {a}.text_project = %(project)s AND {a}.text_language = %(language)s "
        "AND (lower({a}.text_city) = %(q)s OR lower({a}.text_country) = %(q)s "
        "OR {a}.text_address ILIKE %(q_sub)s)"
    ).format(a=sql.Identifier(alias))


def _mean(values: list[float | None]) -> float | None:
    xs = [v for v in values if v is not None]
    return sum(xs) / len(xs) if xs else None


def _collect(rows: list[dict[str, Any]], key, make) -> list[Candidate]:
    """Rows grouped into candidates: counts added up, coordinates averaged."""
    found: dict[tuple, Candidate] = {}
    coords: dict[tuple, tuple[list, list]] = {}
    for r in rows:
        k = key(r)
        c = found.get(k)
        if c is None:
            c = found[k] = make(r)
            coords[k] = ([], [])
        c.locations += int(r["integer_locations"] or 0)
        c.entities += int(r["integer_entities"] or 0)
        c.addresses += 1
        coords[k][0].append(r["float_latitude"])
        coords[k][1].append(r["float_longitude"])
    for k, c in found.items():
        c.lat, c.lng = _mean(coords[k][0]), _mean(coords[k][1])
    return sorted(found.values(), key=lambda c: (-c.locations, c.label))


def _city_candidate(r: dict[str, Any]) -> Candidate:
    address = ", ".join(p for p in (r["text_city"], r["text_region"], r["text_country"]) if p)
    return Candidate(city=r["text_city"], region=r["text_region"], country=r["text_country"],
                     lat=None, lng=None, address=address, parts=address_parts(address))


def _address_candidate(r: dict[str, Any]) -> Candidate:
    """The whole address is the name. Region and country are already inside
    it, and repeating them after a dash - "…, Texas, USA - Texas, USA" -
    reads as two places."""
    address = r["text_address"] or ""
    return Candidate(city=address, region=None, country=None, lat=None, lng=None,
                     address=address, parts=address_parts(address))


def _blend(candidates: list[Candidate], term: str, reading: str, whole: bool) -> Candidate:
    """Several candidates as ONE row - the term itself, their counts added up
    and the middle of their coordinates.

    Used where the chooser must not ask: the term is the area they all sit
    in, or there are too many of them to choose between. `search_match`
    carries the reading, because the search behind such a row is
    name_predicate over the term, not one place's own address."""
    shared = (candidates[0].city or "") if candidates else ""
    name = shared if shared.lower() == term.strip().lower() else term.strip()
    lats = [c.lat for c in candidates if c.lat is not None]
    lngs = [c.lng for c in candidates if c.lng is not None]
    return Candidate(
        city=name, region=None, country=None,
        lat=sum(lats) / len(lats) if lats else None,
        lng=sum(lngs) / len(lngs) if lngs else None,
        locations=sum(c.locations for c in candidates),
        entities=sum(c.entities for c in candidates),
        addresses=sum(c.addresses for c in candidates),
        address=name, parts=address_parts(name),
        covers=len(candidates), whole=whole, search_match=reading)


def _narrow(match: PlaceMatch, term: str, area: bool) -> PlaceMatch:
    """The candidates a person can choose BETWEEN, and never more than ten.

    Three rules, in this order: a whole and its parts are not alternatives,
    so the parts go; a term that every survivor sits INSIDE is the area
    itself, not a choice; and more survivors than a person can read is not
    a question, it is a list - so they are searched together instead."""
    cands = match.candidates
    if len(cands) <= CONTAINMENT_LIMIT:
        survivors = [c for c in cands
                     if not any(inside(c.parts, other.parts) for other in cands if other is not c)]
    else:
        survivors = cands                 # see CONTAINMENT_LIMIT
    if not survivors:                     # a containment cycle cannot happen,
        survivors = cands                 # but never answer with nothing
    wanted = address_parts(term)
    if area and len(survivors) > 1 and all(holds(c.parts, wanted) for c in survivors):
        return PlaceMatch(match.match, [_blend(survivors, term, match.match, whole=True)])
    if len(survivors) > MAX_CANDIDATES:
        return PlaceMatch(match.match, [_blend(survivors, term, match.match, whole=False)])
    return PlaceMatch(match.match, survivors)


def disambiguate(rows: list[dict[str, Any]], q: str) -> PlaceMatch:
    """Group place rows for a typed term into the candidates a person chooses from.

    A country name wins over a city name wins over a substring of an
    address. Two cities of the same name in different regions/countries are
    two candidates and the UI must ask; a country collapses every address in
    it into a single candidate ("all 12 addresses in the country"), because
    the towns inside a country are its contents and not alternatives to it.

    >>> rows = [
    ...  {"text_city": "Springfield", "text_region": "Illinois", "text_country": "USA",
    ...   "text_address": "Springfield, Illinois, USA", "float_latitude": 39.8, "float_longitude": -89.6,
    ...   "integer_locations": 3, "integer_entities": 2},
    ...  {"text_city": "Springfield", "text_region": "Ontario", "text_country": "Canada",
    ...   "text_address": "Springfield, Ontario, Canada", "float_latitude": 42.8, "float_longitude": -80.9,
    ...   "integer_locations": 1, "integer_entities": 1}]
    >>> m = disambiguate(rows, "springfield")
    >>> m.match, m.ambiguous, [c.label for c in m.candidates]
    ('city', True, ['Springfield - Illinois, USA', 'Springfield - Ontario, Canada'])

    A whole and its parts are not a choice - the parts are inside the whole:

    >>> houston = [
    ...  {"text_city": "Houston", "text_region": "Texas", "text_country": "USA",
    ...   "text_address": "Houston, Texas, USA", "float_latitude": 29.8, "float_longitude": -95.4,
    ...   "integer_locations": 414, "integer_entities": 202},
    ...  {"text_city": "NRG Stadium", "text_region": "Texas", "text_country": "USA",
    ...   "text_address": "NRG Stadium, Houston, Texas, USA", "float_latitude": 29.7,
    ...   "float_longitude": -95.4, "integer_locations": 4, "integer_entities": 2}]
    >>> m = disambiguate(houston, "Houston, Texas, USA")
    >>> m.ambiguous, [c.label for c in m.candidates]
    (False, ['Houston, Texas, USA'])
    """
    term = (q or "").strip().lower()
    shown = (q or "").strip()
    if not term or not rows:
        return PlaceMatch("none")

    # A COUNTRY IS NOT AMBIGUOUS WITH THE TOWNS IN IT.
    country = next((r["text_country"] for r in rows
                    if (r["text_country"] or "").lower() == term), None)
    if country:
        # Counted at BOTH ends, because that is what the search behind this
        # candidate covers: place_predicate reads a one-part place as "the
        # city OR the country", so a row this line did not count would turn
        # up in the results. What it says and what it finds are one set.
        covered = [r for r in rows if (r["text_country"] or "").lower() == term
                   or (r["text_city"] or "").lower() == term]
        candidates = _collect(covered, lambda r: (term,), lambda r: Candidate(
            city=None, region=None, country=country, lat=None, lng=None,
            address=country, parts=address_parts(country), whole=True))
        for c in candidates:
            c.covers = c.addresses
        return PlaceMatch("country", candidates)

    cities = [r for r in rows if (r["text_city"] or "").lower() == term]
    if cities:
        candidates = _collect(cities, lambda r: ((r["text_city"] or "").lower(),
                                                 (r["text_region"] or "").lower(),
                                                 (r["text_country"] or "").lower()),
                              _city_candidate)
        return _narrow(PlaceMatch("city", candidates), shown, area=False)

    addresses = [r for r in rows if term in (r["text_address"] or "").lower()]
    if addresses:
        candidates = _collect(addresses, lambda r: ((r["text_address"] or "").lower(),),
                              _address_candidate)
        return _narrow(PlaceMatch("address", candidates), shown, area=True)
    return PlaceMatch("none")


def resolve_exact(rows: list[dict[str, Any]], q: str) -> PlaceMatch:
    """What a value TAKEN FROM THE SUGGESTION LIST means. It is never a question.

    The list offers the archive's own addresses, so the text already is a
    place: it is found by its whole address and that is the answer. Where
    the text is not one of them - a stale link, a value from some other list
    - the ordinary reading applies, and if THAT is undecided the places are
    searched together and counted rather than asked about. Somebody who
    picked from a list has answered the only question there was.

    >>> rows = [
    ...  {"text_city": "Houston", "text_region": "Texas", "text_country": "USA",
    ...   "text_address": "Houston, Texas, USA", "float_latitude": 29.8, "float_longitude": -95.4,
    ...   "integer_locations": 414, "integer_entities": 202},
    ...  {"text_city": "NRG Stadium", "text_region": "Texas", "text_country": "USA",
    ...   "text_address": "NRG Stadium, Houston, Texas, USA", "float_latitude": 29.7,
    ...   "float_longitude": -95.4, "integer_locations": 4, "integer_entities": 2}]
    >>> m = resolve_exact(rows, "NRG Stadium, Houston, Texas, USA")
    >>> m.match, m.ambiguous, m.candidates[0].locations
    ('address', False, 4)
    """
    term = (q or "").strip().lower()
    if not term or not rows:
        return PlaceMatch("none")
    exact = [r for r in rows if (r["text_address"] or "").strip().lower() == term]
    if exact:
        return PlaceMatch("address", _collect(
            exact, lambda r: ((r["text_address"] or "").lower(),), _address_candidate))
    match = disambiguate(rows, q)
    if len(match.candidates) > 1:
        return PlaceMatch(match.match, [_blend(match.candidates, q, match.match, whole=False)])
    return match


# ── Place predicate on the raw locations table ───────────────

def place_predicate(place: Place, alias: str = "l", prefix: str = "pl") -> tuple[sql.Composable, dict[str, Any]]:
    """`(city, region, country)` re-derived from text_address with the view's
    own expressions. A single-part place matches as a city OR a country, so
    "USA" finds every address in the USA and "Linjiang" finds the province.

    Returns (fragment, params); the fragment expects the alias to be
    processed_data.locations and wraps the parts in a LATERAL-free subselect
    so it can be dropped into any WHERE."""
    parts_sql = sql.SQL(ADDRESS_PARTS_SQL.format(a="{a}")).format(a=sql.Identifier(alias))
    params: dict[str, Any] = {}
    conds: list[sql.Composable] = []

    def name(k: str) -> str:
        return f"{prefix}_{k}"

    if place.country and place.city:
        params[name("city")] = place.city.lower()
        params[name("country")] = place.country.lower()
        conds.append(sql.SQL("lower(parts[1]) = %({c})s").format(c=sql.SQL(name("city"))))
        conds.append(sql.SQL("lower(" + COUNTRY_SQL + ") = %({c})s").format(c=sql.SQL(name("country"))))
        if place.region:
            params[name("region")] = place.region.lower()
            conds.append(sql.SQL("lower(" + REGION_SQL + ") = %({c})s").format(c=sql.SQL(name("region"))))
    elif place.city:
        params[name("any")] = place.city.lower()
        conds.append(sql.SQL(
            "(lower(parts[1]) = %({c})s OR lower(" + COUNTRY_SQL + ") = %({c})s)"
        ).format(c=sql.SQL(name("any"))))
    else:
        return sql.SQL("false"), params

    body = sql.SQL(" AND ").join(conds)
    frag = sql.SQL("EXISTS (SELECT 1 FROM (SELECT {parts} AS parts) AS _p WHERE {body})").format(
        parts=parts_sql, body=body)
    return frag, params


def name_predicate(match: str, term: str, alias: str = "l",
                   prefix: str = "pn") -> tuple[sql.Composable, dict[str, Any]]:
    """Every location a place NAME covers, in the reading disambiguate() gave it.

    place_predicate() answers "this city, in this region, in this country" -
    one candidate, the one somebody chose, which is what the Query view has
    to have before it may search at all. The Heatmap asks the other question:
    it COUNTS, so "Springfield" is not a choice between two towns but two
    towns to count, and the answer names them.

    The three readings are the three places_query() itself offers - the city
    is the name, the country is the name, the name is somewhere in the
    address - so the rows counted are exactly the rows the resolution was
    computed from: no place can be counted that was not offered, and none
    that was offered can be left out. `none` is `false`, not "everything":
    a name the archive does not know is an empty map with a sentence, never
    the whole world with a search box that looks as if it did something.

    Returns (fragment, params) for a WHERE on processed_data.locations.

    >>> frag, params = name_predicate("city", "Springfield")
    >>> params
    {'pn_name': 'springfield'}
    >>> frag, params = name_predicate("address", "Infinite Loop")
    >>> params
    {'pn_like': '%Infinite Loop%'}
    >>> frag, params = name_predicate("none", "Atlantis")
    >>> params
    {}
    """
    name = (term or "").strip()
    key_name, key_like = f"{prefix}_name", f"{prefix}_like"
    if not name or match not in ("city", "country", "address"):
        return sql.SQL("false"), {}
    if match == "address":
        # The substring pass, character for character the one places_query()
        # matched with - so the map counts the addresses the resolution read,
        # not a re-split of a fragment that is not a city at all.
        return (sql.SQL("{a}.text_address ILIKE %({k})s").format(
            a=sql.Identifier(alias), k=sql.SQL(key_like)), {key_like: like_substring(name)})
    column = CITY_SQL if match == "city" else COUNTRY_SQL
    parts = sql.SQL(ADDRESS_PARTS_SQL.format(a="{a}")).format(a=sql.Identifier(alias))
    frag = sql.SQL(
        "EXISTS (SELECT 1 FROM (SELECT {parts} AS parts) AS _p"
        " WHERE lower(" + column + ") = %({k})s)"
    ).format(parts=parts, k=sql.SQL(key_name))
    return frag, {key_name: name.lower()}


# ── Distance ─────────────────────────────────────────────────

EARTH_RADIUS_KM = 6371.0088


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance in km.

    >>> round(haversine_km(51.92, 4.47, 47.3769, 8.5417))   # Rotterdam - Zurich
    584
    >>> haversine_km(10, 20, 10, 20)
    0.0
    """
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def bbox(lat: float, lng: float, radius_km: float) -> tuple[float, float, float, float]:
    """(min_lat, max_lat, min_lng, max_lng) enclosing the circle.

    Longitude degrees shrink towards the poles, so the box widens by
    1/cos(lat); close to a pole, or when the circle crosses the date line, the
    box spans every longitude rather than a wrong slice of them.

    >>> [round(v, 2) for v in bbox(0, 0, 111.195)]    # one degree at the equator
    [-1.0, 1.0, -1.0, 1.0]
    >>> lo, hi, w, e = bbox(60, 10, 100)
    >>> round(e - w, 2) > round(hi - lo, 2)     # wider than tall at 60 N
    True
    """
    if radius_km < 0:
        raise ValueError("radius must not be negative")
    dlat = math.degrees(radius_km / EARTH_RADIUS_KM)
    min_lat, max_lat = max(-90.0, lat - dlat), min(90.0, lat + dlat)
    cos_lat = math.cos(math.radians(lat))
    if cos_lat < 1e-6 or max_lat >= 90.0 or min_lat <= -90.0:
        return (round(min_lat, 6), round(max_lat, 6), -180.0, 180.0)
    dlng = math.degrees(radius_km / (EARTH_RADIUS_KM * cos_lat))
    if dlng >= 180.0 or lng - dlng < -180.0 or lng + dlng > 180.0:
        return (round(min_lat, 6), round(max_lat, 6), -180.0, 180.0)
    return (round(min_lat, 6), round(max_lat, 6), round(lng - dlng, 6), round(lng + dlng, 6))


def haversine_sql(alias: str = "l", lat_param: str = "lat", lng_param: str = "lng") -> sql.Composable:
    """The same formula as haversine_km, in SQL, against a row's coordinates.
    least/greatest clamp rounding noise so asin never sees 1.0000001."""
    return sql.SQL(
        "2 * {r} * asin(least(1.0, sqrt("
        "power(sin(radians({a}.float_latitude - %({lat})s) / 2), 2) + "
        "cos(radians(%({lat})s)) * cos(radians({a}.float_latitude)) * "
        "power(sin(radians({a}.float_longitude - %({lng})s) / 2), 2))))"
    ).format(r=sql.Literal(EARTH_RADIUS_KM), a=sql.Identifier(alias),
             lat=sql.SQL(lat_param), lng=sql.SQL(lng_param))


def radius_predicate(lat: float, lng: float, radius_km: float, alias: str = "l",
                     prefix: str = "geo") -> tuple[sql.Composable, dict[str, Any]]:
    """Within radius_km of (lat, lng): a bounding box first, so the
    idx_locations_coords index does the bulk of the work, then the exact
    distance. Returns (fragment, params)."""
    min_lat, max_lat, min_lng, max_lng = bbox(lat, lng, radius_km)
    p = {f"{prefix}_lat": lat, f"{prefix}_lng": lng, f"{prefix}_km": radius_km,
         f"{prefix}_min_lat": min_lat, f"{prefix}_max_lat": max_lat,
         f"{prefix}_min_lng": min_lng, f"{prefix}_max_lng": max_lng}
    frag = sql.SQL(
        "{a}.float_latitude BETWEEN %({pmin_lat})s AND %({pmax_lat})s "
        "AND {a}.float_longitude BETWEEN %({pmin_lng})s AND %({pmax_lng})s "
        "AND {hav} <= %({pkm})s"
    ).format(a=sql.Identifier(alias),
             pmin_lat=sql.SQL(f"{prefix}_min_lat"), pmax_lat=sql.SQL(f"{prefix}_max_lat"),
             pmin_lng=sql.SQL(f"{prefix}_min_lng"), pmax_lng=sql.SQL(f"{prefix}_max_lng"),
             pkm=sql.SQL(f"{prefix}_km"),
             hav=haversine_sql(alias, f"{prefix}_lat", f"{prefix}_lng"))
    return frag, p


# ── The materialized views ───────────────────────────────────

def refresh_places(conn) -> None:
    """Refresh the lists the suggestion boxes are answered from - the
    addresses and the source hosts (sql/03-places-view.sql). CONCURRENTLY so
    readers are not blocked; falls back to a plain refresh on a view that has
    never been populated, which is the only case where CONCURRENTLY is
    refused. A view that does not exist yet is skipped."""
    for name in ("dashboard.places", "dashboard.source_hosts"):
        row = conn.execute("SELECT to_regclass(%s) AS r", (name,)).fetchone()
        # Either row factory: this module is imported by callers that use
        # tuples as readily as dicts.
        found = row is not None and (row["r"] if isinstance(row, dict) else row[0])
        if not found:
            continue
        try:
            conn.execute(f"REFRESH MATERIALIZED VIEW CONCURRENTLY {name}")
        except Exception as exc:  # psycopg.errors.ObjectNotInPrerequisiteState
            if "CONCURRENTLY" not in str(exc) and "populated" not in str(exc):
                raise
            conn.rollback()
            conn.execute(f"REFRESH MATERIALIZED VIEW {name}")
