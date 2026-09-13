"""Address splitting, disambiguation and distance - no database.

The one thing that matters most here: the split rule in app/places.py and
the one in sql/03-places-view.sql are the same text, so the view and a
search that re-derives the parts can never disagree.

    python -m pytest tests/unit/test_places.py -q
"""

from __future__ import annotations

import doctest
from pathlib import Path

import pytest

from app import places
from app.places import (MAX_CANDIDATES, Place, address_parts, bbox, disambiguate, haversine_km,
                        holds, inside, name_predicate, place_predicate, radius_predicate,
                        resolve_exact, split_address)
from app.sqlbuild import check_params, placeholders, render

VIEW_SQL = (Path(__file__).resolve().parents[2] / "sql" / "03-places-view.sql").read_text(encoding="utf-8")


def test_doctests():
    failed, _ = doctest.testmod(places)
    assert failed == 0


def test_split_rule_is_mirrored_in_the_view_sql():
    assert places.ADDRESS_PARTS_SQL.format(a="l") in VIEW_SQL
    assert places.CITY_SQL + " AS text_city" in VIEW_SQL
    assert places.REGION_SQL + " AS text_region" in VIEW_SQL
    assert places.COUNTRY_SQL in VIEW_SQL


@pytest.mark.parametrize("address, expected", [
    ("Springfield, Illinois, USA", Place("Springfield", "Illinois", "USA")),
    ("1 Infinite Loop, Cupertino, California, USA", Place("1 Infinite Loop", "California", "USA")),
    ("Rotterdam, Netherlands", Place("Rotterdam", None, "Netherlands")),
    ("Linjiang", Place("Linjiang", None, None)),
    ("  Zürich , , Schweiz ", Place("Zürich", None, "Schweiz")),
    (",,,", Place()),
    (None, Place()),
])
def test_split_cases(address, expected):
    assert split_address(address) == expected


def test_place_label_and_parts():
    assert Place("Brussels", None, "Belgium").label == "Brussels, Belgium"
    assert Place().parts == ()


# ── Disambiguation ───────────────────────────────────────────

def _row(city, region, country, address, lat, lng, locations=1, entities=1):
    return {"text_city": city, "text_region": region, "text_country": country, "text_address": address,
            "float_latitude": lat, "float_longitude": lng, "integer_locations": locations,
            "integer_entities": entities}


ROWS = [
    _row("Springfield", "Illinois", "USA", "Springfield, Illinois, USA", 39.8, -89.6, 3, 2),
    _row("Springfield", "Ontario", "Canada", "Springfield, Ontario, Canada", 42.8, -80.9),
    _row("Cupertino", "California", "USA", "1 Infinite Loop, Cupertino, California, USA", 37.3, -122.0),
    _row("Redmond", "Washington", "USA", "Redmond, Washington, USA", 47.7, -122.1, 2, 2),
    _row("Linjiang", None, None, "Linjiang", 41.8, 126.9),
]


def test_two_cities_of_one_name_are_two_candidates():
    m = disambiguate(ROWS, "Springfield")
    assert m.match == "city" and m.ambiguous
    assert [c.label for c in m.candidates] == ["Springfield - Illinois, USA", "Springfield - Ontario, Canada"]
    assert m.candidates[0].locations == 3


def test_a_country_collapses_every_address_in_it():
    m = disambiguate(ROWS, "usa")
    assert m.match == "country" and not m.ambiguous
    c = m.candidates[0]
    assert c.country == "USA" and c.city is None
    assert c.addresses == 3 and c.locations == 6 and c.entities == 5
    assert c.label == "USA"
    assert c.lat == pytest.approx((39.8 + 37.3 + 47.7) / 3)


def test_a_substring_falls_back_to_addresses():
    m = disambiguate(ROWS, "infinite")
    assert m.match == "address" and not m.ambiguous
    assert m.candidates[0].label.startswith("1 Infinite Loop")


def test_single_part_address_is_a_city():
    m = disambiguate(ROWS, "linjiang")
    assert m.match == "city" and m.candidates[0].label == "Linjiang"


def test_nothing_found():
    assert disambiguate(ROWS, "atlantis").match == "none"
    assert disambiguate([], "usa").match == "none"
    assert disambiguate(ROWS, "  ").match == "none"
    assert disambiguate(ROWS, "usa").as_dict()["ambiguous"] is False


# ── The chooser asks only what it cannot answer ──────────────
#
# Everything below is the rule the user's two reports produced: "lass diese
# Auswahl 'Which United States do you mean?' weg", and - after PICKING
# "Houston, Texas, USA" out of the suggestion list - being asked which
# Houston was meant and offered six places inside Houston.

HOUSTON = [
    _row("Houston", "Texas", "USA", "Houston, Texas, USA", 29.76, -95.37, 414, 202),
    _row("NRG Stadium", "Texas", "USA", "NRG Stadium, Houston, Texas, USA", 29.68, -95.41, 4, 2),
    _row("Rice University", "Texas", "USA", "Rice University, Houston, Texas, USA", 29.72, -95.40),
    _row("NASA Johnson Space Center", "Texas", "USA",
         "NASA Johnson Space Center, Houston, Texas, USA", 29.56, -95.09),
]


def test_a_whole_and_its_parts_are_not_alternatives():
    """None of those is a different Houston. They are places INSIDE
    Houston, so the area is the answer and they go."""
    m = disambiguate(HOUSTON, "Houston, Texas, USA")
    assert m.match == "address" and not m.ambiguous
    (only,) = m.candidates
    assert only.label == "Houston, Texas, USA"
    assert only.locations == 414
    # One place, so it is searched as itself - no name reading needed.
    assert only.search_match is None and only.covers == 0


def test_the_area_answers_even_when_it_has_no_row_of_its_own():
    """With the plain "Houston, Texas, USA" row absent, the three inside it
    are still not alternatives: the term is the area they sit in, so it is
    one row that stands for all three and says so."""
    m = disambiguate(HOUSTON[1:], "Houston, Texas, USA")
    assert m.match == "address" and not m.ambiguous
    (only,) = m.candidates
    assert only.label == "Houston, Texas, USA"
    assert only.whole is True and only.covers == 3
    assert only.locations == 6            # 4 + 1 + 1
    # Searched as the name the rows were found with, so what the line says
    # and what the search finds are the same places.
    assert only.search_match == "address"
    assert only.as_dict()["address"] == "Houston, Texas, USA"


def test_two_places_of_one_name_still_ask_which_is_the_point():
    """The containment rule must not eat the case the chooser exists for:
    neither Springfield is inside the other."""
    m = disambiguate(ROWS, "Springfield")
    assert m.ambiguous and len(m.candidates) == 2


def test_a_fragment_of_an_address_is_not_an_area():
    """"infinite" is no part of any address on its own, so the addresses
    that carry it stay separate candidates - which is what the Heatmap
    counts them as."""
    m = disambiguate(ROWS, "infinite")
    assert m.match == "address" and len(m.candidates) == 1
    assert m.candidates[0].whole is False and m.candidates[0].search_match is None


def _many(name: str, n: int) -> list[dict]:
    return [_row(name, f"Region {i}", f"Country {i}", f"{name}, Region {i}, Country {i}",
                 10.0 + i, 20.0 + i, locations=i + 1, entities=1) for i in range(n)]


def test_more_places_of_one_name_than_a_person_can_read_are_searched_together():
    """A chooser that scrolls is a failure of the chooser: past ten, the
    page stops asking and searches every one of them."""
    rows = _many("Ambridge", MAX_CANDIDATES + 2)
    m = disambiguate(rows, "Ambridge")
    assert not m.ambiguous
    (only,) = m.candidates
    assert only.covers == MAX_CANDIDATES + 2 and only.whole is False
    assert only.label == "Ambridge"
    assert only.locations == sum(range(1, MAX_CANDIDATES + 3))
    # Every city of that name, which is exactly the rows that were counted.
    assert only.search_match == "city"


def test_exactly_ten_still_asks():
    """The boundary, pinned: ten is a list somebody can read."""
    m = disambiguate(_many("Ambridge", MAX_CANDIDATES), "Ambridge")
    assert m.ambiguous and len(m.candidates) == MAX_CANDIDATES


def test_a_country_is_not_ambiguous_with_the_towns_in_it():
    """"United States" is the country, whichever end of the address the
    archive writes it at. Read as the CITY of every big-endian address
    ("United States, California, Los Angeles") it would produce a 221-row
    "Which United States do you mean?"."""
    rows = [
        _row("United States", None, None, "United States", 39.8, -98.6, 39435, 9000),
        _row("United States", "California", "Los Angeles",
             "United States, California, Los Angeles", 34.0, -118.2, 42, 20),
        _row("Cupertino", "California", "United States",
             "Cupertino, California, United States", 37.3, -122.0, 7, 3),
    ]
    m = disambiguate(rows, "United States")
    assert m.match == "country" and not m.ambiguous
    (only,) = m.candidates
    assert only.label == "United States" and only.whole is True
    # Counted at both ends of the address, because that is what the search
    # behind this one row covers.
    assert only.addresses == 3 and only.covers == 3
    assert only.locations == 39435 + 42 + 7


# ── A value out of the suggestion list is final ──────────────

def test_a_suggestion_resolves_to_its_own_address_and_never_asks():
    for term in ("NRG Stadium, Houston, Texas, USA", "Houston, Texas, USA"):
        m = resolve_exact(HOUSTON, term)
        assert m.match == "address" and not m.ambiguous, term
        assert m.candidates[0].address == term


def test_a_suggestion_the_archive_does_not_hold_as_an_address_still_never_asks():
    """A stale link, or a value from some other list. The reading is the
    ordinary one, and where THAT is undecided the places are searched
    together - somebody who picked from a list has answered the only
    question there was."""
    m = resolve_exact(ROWS, "Springfield")
    assert not m.ambiguous
    (only,) = m.candidates
    assert only.covers == 2 and only.search_match == "city"
    assert only.locations == 4            # 3 in Illinois, 1 in Ontario
    assert resolve_exact(ROWS, "Atlantis").match == "none"
    assert resolve_exact([], "Springfield").match == "none"


# ── The two containment questions, at their boundaries ───────

@pytest.mark.parametrize("inner, outer, expected", [
    ("NRG Stadium, Houston, Texas, USA", "Houston, Texas, USA", True),
    # Whole parts, not letters: a different town whose name happens to end
    # in another's is not inside it.
    ("New Houston, Texas, USA", "Houston, Texas, USA", False),
    ("Houston, Texas, USA", "Houston, Texas, USA", False),      # itself
    ("Houston, Texas, USA", "NRG Stadium, Houston, Texas, USA", False),
    ("Springfield, Illinois, USA", "Springfield, Ontario, Canada", False),
    ("Anything", "", False),
])
def test_inside_is_whole_parts_at_the_end(inner, outer, expected):
    assert inside(address_parts(inner), address_parts(outer)) is expected


@pytest.mark.parametrize("address, term, expected", [
    # Both ends, because this archive writes addresses both ways round.
    ("NRG Stadium, Houston, Texas, USA", "Houston, Texas, USA", True),
    ("United States, California, Los Angeles", "United States, California", True),
    ("1 Infinite Loop, Cupertino, California, USA", "Infinite", False),
    ("Houston, Texas, USA", "Houston, Texas, USA", True),
    ("Houston, Texas", "Houston, Texas, USA", False),
])
def test_holds_is_the_term_as_whole_parts(address, term, expected):
    assert holds(address_parts(address), address_parts(term)) is expected


# ── Predicates ───────────────────────────────────────────────

def test_place_predicate_city_and_country_binds_both():
    frag, params = place_predicate(Place("Springfield", "Ontario", "Canada"), alias="l")
    text = render(frag)
    assert params == {"pl_city": "springfield", "pl_country": "canada", "pl_region": "ontario"}
    assert placeholders(text) == set(params)
    assert places.ADDRESS_PARTS_SQL.format(a='"l"') in text
    check_params(frag, params)


def test_place_predicate_single_part_matches_city_or_country():
    frag, params = place_predicate(Place("USA"), alias="l")
    text = render(frag)
    assert params == {"pl_any": "usa"}
    assert "lower(parts[1]) = %(pl_any)s OR" in text


def test_place_predicate_empty_is_false():
    frag, params = place_predicate(Place(), alias="l")
    assert render(frag) == "false" and params == {}


# name_predicate answers the OTHER question about a place name - not "the
# candidate somebody chose" but "everything the archive calls this", which is
# what a view that COUNTS (the Heatmap) asks. The three readings have to be
# the three the places query itself offers, or a name could be offered as a
# suggestion and then counted as nothing.

def test_name_predicate_city_matches_the_first_part_of_the_address():
    frag, params = name_predicate("city", "Springfield", alias="l")
    text = render(frag)
    assert params == {"pn_name": "springfield"}
    assert placeholders(text) == set(params)
    assert places.ADDRESS_PARTS_SQL.format(a='"l"') in text
    assert "lower(parts[1]) = %(pn_name)s" in text
    check_params(frag, params)


def test_name_predicate_country_matches_the_last_part():
    frag, params = name_predicate("country", "USA", alias="l")
    text = render(frag)
    assert params == {"pn_name": "usa"}
    assert places.COUNTRY_SQL in text
    check_params(frag, params)


def test_name_predicate_address_is_the_substring_the_places_query_matched():
    frag, params = name_predicate("address", "Infinite Loop", alias="l")
    text = render(frag)
    # Character for character the pattern places_query() matched with: a
    # fragment that is not a city must count the addresses it was offered
    # for, not be re-split into a city nothing is called.
    assert params == {"pn_like": "%Infinite Loop%"}
    assert '"l".text_address ILIKE %(pn_like)s' in text
    check_params(frag, params)


def test_name_predicate_of_an_unknown_place_is_false_not_everything():
    """A name the archive does not know counts nothing. `TRUE` would answer a
    search for Atlantis with the whole archive, which reads as an answer."""
    for match, term in [("none", "Atlantis"), ("city", ""), ("nonsense", "Springfield")]:
        frag, params = name_predicate(match, term, alias="l")
        assert render(frag) == "false" and params == {}


def test_name_predicate_escapes_like_metacharacters():
    _, params = name_predicate("address", "100% Ltd_", alias="l")
    assert params == {"pn_like": "%100\\% Ltd\\_%"}


# ── Distance ─────────────────────────────────────────────────

def test_bbox_at_high_latitude_is_wider_than_tall():
    lo, hi, w, e = bbox(70.0, 20.0, 50.0)
    assert hi - lo == pytest.approx(2 * 50.0 / places.EARTH_RADIUS_KM * 180 / 3.141592653589793, rel=1e-3)
    assert (e - w) > 2.5 * (hi - lo)


def test_bbox_near_the_pole_or_date_line_spans_every_longitude():
    assert bbox(89.9, 0, 50)[2:] == (-180.0, 180.0)
    assert bbox(0, 179.9, 50)[2:] == (-180.0, 180.0)
    assert bbox(-89.5, 0, 100)[0] == -90.0


def test_bbox_refuses_negative_radius():
    with pytest.raises(ValueError):
        bbox(0, 0, -1)


def test_haversine_known_distances():
    assert haversine_km(51.92, 4.47, 47.3769, 8.5417) == pytest.approx(583, abs=2)
    assert haversine_km(0, 0, 0, 180) == pytest.approx(3.141592653589793 * places.EARTH_RADIUS_KM, rel=1e-6)


def test_radius_predicate_binds_box_and_distance():
    frag, params = radius_predicate(47.38, 8.54, 25, alias="l")
    text = render(frag)
    assert placeholders(text) == set(params)
    assert params["geo_km"] == 25 and params["geo_min_lat"] < 47.38 < params["geo_max_lat"]
    assert '"l".float_latitude BETWEEN %(geo_min_lat)s AND %(geo_max_lat)s' in text
    assert "asin(least(1.0, sqrt(" in text
    check_params(frag, params)
