"""The Query view's two endpoints: what a place name means, and what is there.

    DATABASE_URL=postgresql://... python -m pytest tests/flow/test_query_places.py -q

The preseed puts two Springfields in the archive on purpose - Springfield
Works in Illinois and Springfield Mills in Ontario - because the interesting
part of this view is the question it asks BACK. Every other place in it is
unique, and Zurich/Zürich is there to show that the answer is per language.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from preseed import ALPHA, BETA

pytestmark = pytest.mark.flow

EN = {"project": ALPHA, "language": "English"}
DE = {"project": ALPHA, "language": "German"}

CUPERTINO = (37.3318, -122.0312)


def places(client, q: str, params=None) -> dict:
    r = client.get("/api/query/places", params={**(params or EN), "q": q})
    assert r.status_code == 200, r.text
    return r.json()


def search(client, body: dict, params=None, expect: int = 200) -> dict:
    r = client.post("/api/query/search", params=params or EN, json=body)
    assert r.status_code == expect, r.text
    return r.json()


def names_in(data: dict) -> set[str]:
    return {i["about"] for group in data["groups"] for i in group["items"] if i["about"]}


def titles_in(data: dict, kind: str = "source") -> set[str]:
    return {i["title"] for group in data["groups"] for i in group["items"] if i["kind"] == kind}


# ── Two shapes of place the preseed does not hold ────────────
#
# The preseed is a small, readable archive and it has no address with other
# addresses inside it, and no name carried by more than two towns. Both of
# those are what the user's reports are about - "Houston, Texas, USA" with
# six places in it, "United States" with 221 - so the two tests that need
# them put them in for the length of the test and take them out again.

TASK = "_q places"


def _put_places(archive, rows: list[tuple[str, float, float]]) -> None:
    """An entity and a location per address, under one task id."""
    now = datetime.now(timezone.utc)
    with archive.connect() as conn:
        for i, (address, lat, lng) in enumerate(rows):
            eid = f"_qp{i}"
            conn.execute(
                """INSERT INTO processed_data.entities
                     (date_added, text_name, text_project, text_language, text_task_id,
                      text_job_id, text_entity_id, text_fk_source_id, text_type,
                      date_commissioned, date_evaluated)
                   VALUES (%s,%s,%s,'English',%s,%s,%s,'',%s,%s,%s)""",
                (now, f"_qp entity {i}", ALPHA, TASK, TASK, eid, "Company", now, now))
            conn.execute(
                """INSERT INTO processed_data.locations
                     (date_added, text_name, text_project, text_language, text_task_id,
                      text_job_id, text_fk_entity_id, text_fk_source_id, text_type,
                      text_address, float_latitude, float_longitude,
                      date_commissioned, date_evaluated)
                   VALUES (%s,%s,%s,'English',%s,%s,%s,'','Factory',%s,%s,%s,%s,%s)""",
                (now, address, ALPHA, TASK, TASK, eid, address, lat, lng, now, now))
        conn.commit()
    archive.refresh_places()


def _drop_places(archive) -> None:
    with archive.connect() as conn:
        for table in ("locations", "entities"):
            conn.execute(f"DELETE FROM processed_data.{table} WHERE text_task_id = %s", (TASK,))
        conn.commit()
    archive.refresh_places()


@pytest.fixture
def houston(archive):
    """A town and four places inside it, as a real archive holds them."""
    rows = [("Houston, Texas, USA", 29.76, -95.37)] * 9 + [
        ("NRG Stadium, Houston, Texas, USA", 29.68, -95.41),
        ("Rice University, Houston, Texas, USA", 29.72, -95.40),
        ("NASA Johnson Space Center, Houston, Texas, USA", 29.56, -95.09),
        ("University of Houston, Houston, Texas, USA", 29.72, -95.34),
    ]
    _put_places(archive, rows)
    yield
    _drop_places(archive)


@pytest.fixture
def united_states(archive):
    """A country written at BOTH ends of an address, as a real archive
    writes it: "United States, California, Los Angeles" as readily as
    "Cupertino, California, United States"."""
    _put_places(archive, [
        ("United States", 39.8, -98.6),
        ("United States, California, Los Angeles", 34.05, -118.24),
        ("United States, Washington, D.C.", 38.9, -77.0),
        ("Cupertino, California, United States", 37.33, -122.03),
    ])
    yield
    _drop_places(archive)


@pytest.fixture
def ambridge(archive):
    """Twelve towns of one name - more than a chooser may ever show."""
    _put_places(archive, [(f"Ambridge, Region {i}, Country {i}", 10.0 + i, 20.0 + i)
                          for i in range(12)])
    yield
    _drop_places(archive)


# ── What a place name means ──────────────────────────────────

def test_springfield_is_ambiguous_and_says_so(client):
    answer = places(client, "Springfield")
    assert answer["match"] == "city"
    assert answer["ambiguous"] is True
    labels = [c["label"] for c in answer["candidates"]]
    assert labels == ["Springfield - Illinois, USA", "Springfield - Ontario, Canada"]
    for candidate in answer["candidates"]:
        assert candidate["lat"] and candidate["lng"]
        assert candidate["locations"] >= 1 and candidate["entities"] >= 1


def test_a_country_is_one_candidate_covering_every_address_in_it(client):
    answer = places(client, "USA")
    assert answer["match"] == "country"
    assert answer["ambiguous"] is False
    (candidate,) = answer["candidates"]
    assert candidate["country"] == "USA"
    assert candidate["city"] is None
    # Cupertino (2), Redmond (2), Springfield, Illinois (1) and Austin (1) -
    # Apple Inc.'s second office, in one of its two tasks (preseed.py:
    # EXTRA_LOCATIONS).
    assert candidate["locations"] == 6
    # ADDED UP ADDRESS BY ADDRESS, so an entity with two addresses in one
    # country is counted at each of them: Apple Inc. is in Cupertino and in
    # Austin, and this says 4 where the distinct entities are 3. The exact
    # number would be a distinct count over every location row in the
    # country, which is a full scan of a hypertable on a real archive for a
    # figure that only helps somebody choose between two towns - the
    # locations count is what does that. Named here so it is not read as
    # something it is not.
    assert candidate["entities"] == 4


def test_picking_a_suggestion_never_opens_the_chooser(client):
    """The user's report: after PICKING "Houston, Texas, USA" out of the
    suggestion list, the page asked which Houston was meant. A value that
    came from the list is a choice already made, and `exact` is how the page
    says so - asserted on what the list actually offers (a whole address,
    api_suggest._ADDRESS) rather than on typed text.

    The second half is the case where the picked value is NOT one of the
    archive's addresses - a stale link, another list. It still never asks:
    the places are searched together and counted."""
    picked = places(client, "Springfield, Illinois, USA", {**EN, "exact": "1"})
    assert picked["ambiguous"] is False
    (only,) = picked["candidates"]
    assert only["address"] == "Springfield, Illinois, USA"
    assert only["covers"] == 0 and only["search_match"] is None

    both = places(client, "Springfield", {**EN, "exact": "1"})
    assert both["ambiguous"] is False
    (all_of_them,) = both["candidates"]
    assert all_of_them["covers"] == 2 and all_of_them["search_match"] == "city"
    # And typing the same letters is a different act, which still asks.
    assert places(client, "Springfield")["ambiguous"] is True


def test_a_whole_and_its_parts_are_not_offered_as_a_choice(client, houston):
    """Six places inside Houston are not six Houstons. The area is the
    answer; searching it is how a person reaches what is in it."""
    answer = places(client, "Houston, Texas, USA")
    assert answer["match"] == "address" and answer["ambiguous"] is False
    (only,) = answer["candidates"]
    assert only["address"] == "Houston, Texas, USA"
    assert only["locations"] == 9        # the town's own rows, not the four inside it


def test_more_places_of_one_name_than_a_person_can_read_are_searched_together(client, ambridge):
    """"Which United States do you mean?" over 221 rows is no question. Past
    ten there is no question left to ask: they are all searched, and the
    answer says how many."""
    answer = places(client, "Ambridge")
    assert answer["ambiguous"] is False
    (only,) = answer["candidates"]
    assert only["covers"] == 12 and only["search_match"] == "city"
    assert only["locations"] == 12

    # And the search behind that one row reaches every one of them.
    found = search(client, {"place": {"address": "Ambridge", "match": "city"}})
    assert found["where"]["label"] == "Ambridge"
    assert len(found["places"]) == 12


def test_a_country_is_the_country_and_not_a_list_of_the_towns_in_it(client, united_states):
    """A "Which United States do you mean?" list of 221 rows is no answer.

    It would be one because the archive writes addresses at both ends, and
    the split rule reads "United States, California, Los Angeles" as a CITY
    called United States. The country is asked first, so the towns in it
    are the result and not the question."""
    answer = places(client, "United States")
    assert answer["match"] == "country" and answer["ambiguous"] is False
    (only,) = answer["candidates"]
    assert only["label"] == "United States"
    assert only["whole"] is True
    # Every address that carries the name at either end - which is what the
    # search behind this candidate covers.
    assert only["addresses"] == 4


def test_a_unique_city_needs_no_question_and_an_unknown_one_says_nothing(client):
    rotterdam = places(client, "Rotterdam")
    assert rotterdam["ambiguous"] is False and len(rotterdam["candidates"]) == 1
    nowhere = places(client, "Atlantis")
    assert nowhere["match"] == "none" and nowhere["candidates"] == []


def test_place_names_are_answered_in_the_language_asked_for(client):
    assert places(client, "Zurich")["candidates"][0]["country"] == "Switzerland"
    assert places(client, "Zurich", DE)["candidates"] == []
    assert places(client, "Zürich", DE)["candidates"][0]["country"] == "Schweiz"


# ── Searching a place ────────────────────────────────────────

def test_the_disambiguated_place_finds_only_its_own_entity(client):
    illinois = search(client, {"place": {"address": "Springfield, Illinois, USA"}})
    assert names_in(illinois) == {"Springfield Works"}
    # THE TITLE IS NEVER THE EXTRACTION'S OWN ID. "src:pump" is what the
    # extraction called the document - a slug that is not unique across
    # projects and is not what anything is CALLED - so the list shows the
    # domain and the title derived from the address instead
    # (app/source_names.py: is_identifier).
    assert "blog.example.net - Pump delivery" in titles_in(illinois)
    assert not [t for t in titles_in(illinois) if t.startswith("src:")]
    assert not [t for t in titles_in(illinois) if "harvest" in t.lower()]

    ontario = search(client, {"place": {"address": "Springfield, Ontario, Canada"}})
    assert names_in(ontario) == {"Springfield Mills"}


def test_an_undisambiguated_city_finds_both_which_is_why_the_page_asks(client):
    both = search(client, {"place": {"address": "Springfield"}})
    assert names_in(both) == {"Springfield Works", "Springfield Mills"}


def test_a_country_finds_every_address_in_it(client):
    usa = search(client, {"place": {"address": "USA"}})
    assert names_in(usa) >= {"Apple Inc.", "Microsoft", "Springfield Works"}


def test_the_other_project_is_somewhere_else(client):
    assert search(client, {"place": {"address": "Berlin, Germany"}})["total"] == 0
    beta = search(client, {"place": {"address": "Berlin, Germany"}},
                  {"project": BETA, "language": "English"})
    assert names_in(beta) == {"Contoso"}


# ── Searching a point ────────────────────────────────────────

def test_a_point_and_a_radius_find_the_one_entity_inside_it(client):
    lat, lng = CUPERTINO
    data = search(client, {"lat": lat, "lng": lng, "radius_km": 25})
    assert data["where"]["kind"] == "point"
    assert names_in(data) == {"Apple Inc."}
    # ONE KIND PER SEARCH. The same walk finds the same rows; what a request
    # says is which of them come back, and the count is of that kind alone.
    # Asked for both at once, it would be a second pass over every location
    # of the place for an answer only half of it is waiting for.
    assert data["counts"] == {"source": 2}
    assert search(client, {"lat": lat, "lng": lng, "radius_km": 25,
                           "kind": "attribute"})["counts"] == {"attribute": 3}
    # The map gets the address it found, with coordinates.
    assert data["places"] == [{"entity": "Apple Inc.", "entity_type": "Company",
                               "address": "Cupertino, California, USA", "type": "Headquarters",
                               "lat": lat, "lng": lng}]


def test_a_radius_that_reaches_nothing_finds_nothing(client):
    data = search(client, {"lat": 0.0, "lng": 0.0, "radius_km": 100})
    assert data["total"] == 0 and data["groups"] == []


def test_a_place_and_a_point_together_or_neither_are_refused(client):
    lat, lng = CUPERTINO
    both = search(client, {"lat": lat, "lng": lng, "place": {"address": "USA"}}, expect=400)
    assert "not both" in both["error"]
    neither = search(client, {}, expect=400)
    assert neither["hint"]
    too_far = search(client, {"lat": lat, "lng": lng, "radius_km": 30000}, expect=400)
    assert "distance" in too_far["error"]
    off_world = search(client, {"lat": 99.0, "lng": 0.0}, expect=400)
    assert "outside the world" in off_world["error"]


def test_the_archived_rows_are_called_what_every_other_view_calls_them(client):
    """This endpoint labelled its first group "Documents", and the
    Dashboard card, the Diagrams tab, the Diagrams scope and the Events rows
    all call the very same rows "Sources". A customer who read "Sources 5"
    on the Dashboard and then searched here was told the archive held
    "Documents (1 of 1)", with nothing on either screen saying they are the
    same thing."""
    lat, lng = CUPERTINO
    for kind, label in (("source", "Sources"), ("attribute", "Attributes"),
                        ("entity", "Entities"), ("location", "Locations"),
                        ("connection", "Connections"),
                        ("market_insight", "Market Insights"), ("rating", "Ratings")):
        data = search(client, {"lat": lat, "lng": lng, "radius_km": 25, "kind": kind})
        found = {group["kind"]: group["label"] for group in data["groups"]}
        # A kind with no rows here has no group, and that is the empty answer
        # rather than a missing label - so only what came back is checked.
        assert found.get(kind, label) == label, (kind, found)


# ── Words and dates ──────────────────────────────────────────

def test_terms_are_matched_in_documents_and_in_attributes(client):
    """One kind per search, so the same words are asked of each in turn."""
    lat, lng = CUPERTINO
    docs = search(client, {"lat": lat, "lng": lng, "radius_km": 25, "terms": ["battery"]})
    assert docs["counts"] == {"source": 1}
    attrs = search(client, {"lat": lat, "lng": lng, "radius_km": 25,
                            "terms": ["battery"], "kind": "attribute"})
    assert attrs["counts"] == {"attribute": 2}
    for data in (docs, attrs):
        for group in data["groups"]:
            for item in group["items"]:
                assert item["matched_terms"] == ["battery"], item

    # Two words are OR by default and AND on request.
    either = search(client, {"lat": lat, "lng": lng, "radius_km": 25,
                             "terms": ["battery", "subsidiary"]})
    assert either["counts"]["source"] == 2
    both = search(client, {"lat": lat, "lng": lng, "radius_km": 25,
                           "terms": ["battery", "subsidiary"], "match_all": True})
    assert both["counts"]["source"] == 0


def test_a_word_nobody_wrote_finds_nothing(client):
    lat, lng = CUPERTINO
    data = search(client, {"lat": lat, "lng": lng, "radius_km": 25, "terms": ["zeppelin"]})
    assert data["total"] == 0


def test_the_date_range_leaves_out_the_two_year_old_document(client):
    # Microsoft sits in Redmond and is written about twice: three days ago
    # and two years ago.
    redmond = {"place": {"address": "Redmond, Washington, USA"}}
    assert search(client, redmond)["counts"]["source"] == 2

    year_ago = (datetime.now(timezone.utc) - timedelta(days=365)).date().isoformat()
    recent = search(client, {**redmond, "date_from": year_ago})
    assert recent["counts"]["source"] == 1
    assert titles_in(recent) == {"filings.example.org - Antitrust 2026"}

    # And the other way round: everything older than a year.
    #
    # This document is the one the preseed names the way a real archive names
    # them - a 32-character checksum - so the title is built from the address
    # instead (app/charts/drilldown.py: readable_title). A reader who is
    # shown a hash where a headline belongs has been told nothing.
    old = search(client, {**redmond, "date_to": year_ago})
    assert titles_in(old) == {"blog.example.net - Old antitrust"}

    backwards = search(client, {**redmond, "date_from": "2026-06-01", "date_to": "2026-01-01"},
                       expect=400)
    assert "ends before it starts" in backwards["error"]
    bad = search(client, {**redmond, "date_from": "yesterday"}, expect=400)
    assert "not a date" in bad["error"]


def test_paging_keeps_the_total(client):
    """The same block contract as the Events view, and for the same reason:
    a walk over every location of a place the size of "USA" is expensive
    enough that it is done once for two hundred rows, and the pager then
    moves inside what it holds (api_query.py:58-74). So page 1 of block 0 is
    served from the first fetch, and the total is asked for separately - a
    page past the last block reports the count rather than zero."""
    usa = {"place": {"address": "USA"}}
    first = search(client, usa)
    assert first["page"] == 0 and first["page_size"] == 25

    inside = search(client, {**usa, "page": 1})
    assert inside["total"] == first["total"]
    assert inside["block"] == first["block"] == 0
    assert inside["groups"] == first["groups"], "one block, one fetch"

    beyond = search(client, {**usa, "page": first["pages_per_block"]})
    assert beyond["block"] == 1
    assert beyond["groups"] == []
    assert beyond["total"] == first["total"], "the count does not ride on the rows"


# ── Which objects a search looks at ──────────────────────────
#
# The walk is always the same: the locations that answer the place, the
# entities standing at them, and the objects hanging off those entities. What
# a request chooses is the last step - and only the last step, so the same
# place and the same words find the same rows whichever kind is asked for.

def test_every_kind_answers_in_the_same_shape(client):
    """Seven branches, one contract. The page draws whatever comes back
    without knowing which branch ran, so a branch that answered a different
    shape would be a view that renders one kind and breaks on another."""
    lat, lng = CUPERTINO
    for kind in ("source", "entity", "location", "connection",
                 "market_insight", "rating", "attribute"):
        data = search(client, {"lat": lat, "lng": lng, "radius_km": 25, "kind": kind})
        assert data["object_kind"] == kind
        assert set(data["counts"]) == {kind}, data["counts"]
        for group in data["groups"]:
            assert group["kind"] == kind
            for item in group["items"]:
                # Nine fields, always. `about` is the entity the row hangs off,
                # and a fact without its subject is a fact nobody can place.
                assert set(item) >= {"kind", "id", "title", "body", "uri",
                                     "type", "about", "date", "matched_terms"}
                assert item["kind"] == kind


def test_an_unknown_kind_searches_rather_than_refusing(client):
    """A view switch, not a parameter: a stale bookmark should show the
    archive rather than an error page."""
    lat, lng = CUPERTINO
    data = search(client, {"lat": lat, "lng": lng, "radius_km": 25, "kind": "nonsense"})
    assert data["object_kind"] == "source"


def test_a_kind_is_the_only_thing_that_changes(client):
    """The same place finds the same entities whatever is asked about them.
    If choosing Connections also changed which entities were walked, the
    counts would be answers to different questions."""
    lat, lng = CUPERTINO
    entities = search(client, {"lat": lat, "lng": lng, "radius_km": 25, "kind": "entity"})
    sources = search(client, {"lat": lat, "lng": lng, "radius_km": 25, "kind": "source"})
    assert entities["where"] == sources["where"]
    assert entities["places"] == sources["places"]


def test_a_connection_is_named_by_its_two_ends_and_never_by_their_ids(client):
    """The branch returns entity IDS - resolving both ends in SQL ran a
    correlated lookup for every matching row rather than for the page, and on a
    real archive it did not finish. The route names the page's rows instead,
    and an id that no entity answers to keeps the id: a blank end would read as
    a connection to nothing rather than to something unnamed."""
    lat, lng = CUPERTINO
    data = search(client, {"lat": lat, "lng": lng, "radius_km": 25, "kind": "connection"})
    for group in data["groups"]:
        for item in group["items"]:
            assert " → " in item["title"], item
            for end in item["title"].split(" → "):
                assert end.strip(), item
                # An entity id in this archive is a cuid or an opaque key; a
                # name has a space or a capital in it. What is pinned is that
                # the two ends were LOOKED UP, which `derived` records.
            assert item["derived"] is True, item
