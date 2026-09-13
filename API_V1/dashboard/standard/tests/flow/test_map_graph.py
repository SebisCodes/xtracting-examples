"""GET /api/map/entity, /api/map/heat and /api/graph/neighbours against the preseed.

    DATABASE_URL=postgresql://... python -m pytest tests/flow/test_map_graph.py -q

The Alpha project's connections are the whole cast of this file. Read as
"parent, child, the parent's role, the child's role", which is how the
archive stores them:

    Foxconn   → Apple Inc.  Supplier / Customer      (and the reverse row)
    Tim Cook  → Apple Inc.  CEO / Employer
    Commission→ Apple       Regulator / Regulated entity
    Apple     ↔ Microsoft   Competitor / Competitor  (and the reverse row)
    Apple Inc.↔ Apple       Subsidiary / Parent company (and the reverse row)
    Commission→ Microsoft   Regulator / Regulated entity   (two years old)
    Nordwyk   → Springfield Works   Supplier / Customer
    Zurich    ↔ Nordwyk     Partner / Partner  and  Mysterious Bond (unmapped)
    Springfield Mills → Nordwyk     Customer / Supplier

and the bucket "Apple" holds (Apple Inc., Company) and (Apple, Company) -
never Apple the fruit.

Three properties are checked over and over because everything else rests on
them: an edge is labelled with the FAR end's role (a ring around Apple shows
"Foxconn - Supplier", not "Customer"), the map and the graph give the same
pair the same colour, and both bind project AND language.
"""

from __future__ import annotations

import time
from datetime import date, timedelta

import pytest

from app.routers import api_map
from preseed import ALPHA, BETA

pytestmark = pytest.mark.flow

EN = {"project": ALPHA, "language": "English"}
DE = {"project": ALPHA, "language": "German"}
BETA_EN = {"project": BETA, "language": "English"}


def map_of(client, params: dict, expect: int = 200) -> dict:
    r = client.get("/api/map/entity", params={**EN, **params})
    assert r.status_code == expect, r.text
    return r.json()


def heat(client, params: dict | None = None, expect: int = 200) -> dict:
    r = client.get("/api/map/heat", params={**EN, **(params or {})})
    assert r.status_code == expect, r.text
    return r.json()


def graph(client, params: dict, expect: int = 200) -> dict:
    r = client.get("/api/graph/neighbours", params={**EN, **params})
    assert r.status_code == expect, r.text
    return r.json()


def names(items) -> set[str]:
    return {i["name"] for i in items}


def pair(data: dict, one: str, two: str) -> dict | None:
    for line in data["connections"]:
        if {line["from"]["name"], line["to"]["name"]} == {one, two}:
            return line
    return None


# ── The map ──────────────────────────────────────────────────

def test_a_bucket_puts_every_member_and_its_neighbours_on_the_map(client):
    data = map_of(client, {"q": "Apple"})
    assert data["resolved"]["match"] == "bucket"
    # Level 0 is the bucket itself, level 1 what it is connected to.
    levels = {e["name"]: e["level"] for e in data["entities"]}
    assert levels["Apple"] == 0 and levels["Apple Inc."] == 0
    assert levels["Foxconn"] == 1 and levels["Microsoft"] == 1
    assert levels["European Commission"] == 1 and levels["Tim Cook"] == 1
    # Apple the fruit is in the archive and not in the bucket.
    assert "Springfield Mills" not in levels


def test_one_marker_per_address_with_its_entity_and_level(client):
    data = map_of(client, {"q": "Apple"})
    addresses = {m["entity"]: m["address"] for m in data["markers"]}
    assert addresses["Apple Inc."] == "Cupertino, California, USA"
    assert addresses["Foxconn"] == "Taipei, Taiwan"
    # Tim Cook and Apple (the company without a seat) have no coordinates and
    # therefore no marker - but they are still entities on the answer.
    assert "Tim Cook" not in addresses
    assert names(data["entities"]) > set(addresses)
    for marker in data["markers"]:
        assert -90 <= marker["lat"] <= 90 and -180 <= marker["lng"] <= 180
        assert marker["level"] in (0, 1)
    south, west = data["bounds"][0]
    north, east = data["bounds"][1]
    assert south <= north and west <= east


def test_a_line_is_labelled_with_what_the_far_end_is(client):
    data = map_of(client, {"q": "Apple"})
    supplier = pair(data, "Apple Inc.", "Foxconn")
    assert [t["name"] for t in supplier["types"]] == ["Supplier"]
    # The archive holds the same fact twice (Foxconn→Apple and Apple→Foxconn);
    # one line, one type, two connections.
    assert supplier["count"] == 2
    assert supplier["group"] == "supplier" and supplier["colour"] == "#30A0A6"
    assert supplier["drawable"] is True

    boss = pair(data, "Apple Inc.", "Tim Cook")
    assert [t["name"] for t in boss["types"]] == ["CEO"]
    assert boss["group"] == "person"
    # Tim Cook has no address, so the line cannot be drawn - and says so
    # instead of being dropped without a word.
    assert boss["drawable"] is False


def test_more_levels_reach_further(client):
    one = map_of(client, {"q": "Apple", "levels": 1})
    two = map_of(client, {"q": "Apple", "levels": 2})
    assert pair(one, "European Commission", "Microsoft") is None
    assert pair(two, "European Commission", "Microsoft") is not None
    assert two["levels"] == 2
    # The regulator's line is drawn in the regulator colour whichever way
    # round the two ends are read.
    assert pair(two, "European Commission", "Microsoft")["group"] == "regulator"


def test_the_type_checklist_is_what_the_lines_are_labelled_with(client):
    data = map_of(client, {"q": "Apple", "levels": 2})
    checklist = {t["name"]: t["count"] for t in data["types"]}
    drawn = {t["name"] for line in data["connections"] for t in line["types"]}
    assert set(checklist) == drawn
    assert checklist["Supplier"] == 2 and checklist["CEO"] == 1


def test_a_type_filter_keeps_only_those_lines(client):
    data = map_of(client, {"q": "Apple", "levels": 2, "types": "Supplier,Competitor"})
    assert {(l["from"]["name"], l["to"]["name"]) for l in data["connections"]} == {
        ("Apple Inc.", "Foxconn"), ("Apple", "Microsoft")}
    # The checklist itself does not shrink: a filter you cannot undo is a trap.
    assert {t["name"] for t in data["types"]} >= {"Supplier", "Competitor", "CEO", "Regulator"}


# ── The two pictures the map draws, and why they are exclusive ──────────
#
# Not two checkboxes, both on: a search for one person would then put a pin
# on the map for every company they are connected to, with nothing saying
# whose address each pin is. They are one switch:
#
#   connections  the network - every entity at the ONE address it carries
#                most often, because a line has to end somewhere
#   locations    every address of the entity that was searched, no lines
#
# The preseed gives Apple Inc. a head office in both its tasks and a second
# office in one of them (EXTRA_LOCATIONS), so the two modes differ here as
# well as on a real archive.

def test_locations_mode_draws_every_address_and_no_lines(client):
    data = map_of(client, {"q": "Apple", "mode": "locations"})
    assert names(data["entities"]) == {"Apple", "Apple Inc."}
    assert data["connections"] == []
    assert data["mode"] == "locations"
    addresses = {m["address"] for m in data["markers"] if m["entity"] == "Apple Inc."}
    assert addresses == {"Cupertino, California, USA", "Austin, Texas, USA"}


def test_connections_mode_places_each_entity_at_its_main_address(client):
    """One pin per entity, and it is the address the entity carries most
    often - not the first one the archive happens to return."""
    data = map_of(client, {"q": "Apple", "levels": 2, "mode": "connections"})
    assert data["mode"] == "connections"
    pins = [m for m in data["markers"] if m["entity"] == "Apple Inc."]
    assert [m["address"] for m in pins] == ["Cupertino, California, USA"], (
        "the head office is in both tasks and the second office in one, so "
        "the head office is the main address")
    # Every entity on the map, not just this one.
    per_entity = {}
    for m in data["markers"]:
        per_entity[m["entity_id"]] = per_entity.get(m["entity_id"], 0) + 1
    assert set(per_entity.values()) == {1}, f"more than one pin for an entity: {per_entity}"
    # The list beside the map still says how many addresses it HAS.
    apple = next(e for e in data["entities"] if e["name"] == "Apple Inc.")
    assert apple["locations"] == 2


def test_an_unknown_mode_draws_the_map_rather_than_failing(client):
    data = map_of(client, {"q": "Apple", "mode": "whatever"})
    assert data["mode"] == "connections"


def test_line_weight_grows_with_the_number_of_connections(client):
    data = map_of(client, {"q": "Apple", "levels": 2})
    busy = pair(data, "Apple Inc.", "Foxconn")["weight"]
    quiet = pair(data, "Apple Inc.", "Tim Cook")["weight"]
    # THE RANGE WAS SCALED DOWN, NOT FLATTENED. The lines were drawn too
    # thick (3-9 px of colour inside 6 px of casing and keyline); halving
    # both ends keeps the one thing the width means - the busiest pair is
    # three times a single connection - while the picture stops being a
    # bundle of ribbons over the coastline.
    assert busy > quiet >= api_map.MIN_WEIGHT
    assert all(api_map.MIN_WEIGHT <= l["weight"] <= api_map.MAX_WEIGHT
               for l in data["connections"])
    # The ratio is the meaning, and it is the same as it always was.
    assert api_map.MAX_WEIGHT / api_map.MIN_WEIGHT == pytest.approx(3.0)
    assert busy / quiet == pytest.approx(api_map.MAX_WEIGHT / api_map.MIN_WEIGHT)


def test_the_legend_holds_the_groups_that_are_drawn(client):
    data = map_of(client, {"q": "Apple", "levels": 2})
    legend = {g["key"]: g for g in data["legend"]}
    assert set(legend) == {l["group"] for l in data["connections"]}
    assert legend["supplier"]["colour"] == "#30A0A6"
    assert legend["supplier"]["count"] == 1
    # Every legend row carries the text colour a label needs on that swatch.
    assert all(g["text_colour"] in ("#212121", "#ffffff") for g in data["legend"])


def test_german_is_the_same_map_with_translated_names(client):
    english = map_of(client, {"q": "Apple", "levels": 2})
    r = client.get("/api/map/entity", params={**DE, "q": "Apple", "levels": 2})
    german = r.json()
    assert {e["id"] for e in german["entities"]} == {e["id"] for e in english["entities"]}
    assert "Europäische Kommission" in names(german["entities"])
    assert {m["address"] for m in german["markers"]} >= {"Brüssel, Belgien", "Taipeh, Taiwan"}
    # Same relationship, same colour, translated name.
    supplier = pair(german, "Apple Inc.", "Foxconn")
    assert [t["name"] for t in supplier["types"]] == ["Lieferant"]
    assert supplier["group"] == "supplier"


def test_projects_stay_apart(client):
    r = client.get("/api/map/entity", params={**BETA_EN, "q": "Apple"})
    data = r.json()
    assert names(data["entities"]) == {"Apple", "Contoso"}
    assert "Foxconn" not in names(data["entities"])


def test_an_empty_term_is_an_empty_map_and_not_an_error(client):
    data = map_of(client, {})
    assert data["entities"] == [] and data["connections"] == []
    assert data["resolved"]["match"] == "none"
    # The colour scale is still there, so the legend is on the page before
    # anybody has searched.
    assert len(data["legend"]) == 11


def test_a_term_nobody_knows_says_so(client):
    data = map_of(client, {"q": "Zzz Qqq"})
    assert data["resolved"]["match"] == "none"
    assert data["entities"] == []


# ── The heat grid ────────────────────────────────────────────

def test_the_heat_grid_counts_every_archived_coordinate(client):
    data = heat(client)
    assert data["precision"] == 3
    assert data["cells"] > 0 and data["max"] >= 1
    assert data["locations"] == sum(p[2] for p in data["points"])
    assert not data["capped"]
    # Three decimals, and the busiest square first.
    for lat, lng, n in data["points"]:
        assert round(lat, 3) == lat and round(lng, 3) == lng
    assert data["points"] == sorted(data["points"], key=lambda p: (-p[2], p[0], p[1]))


def test_every_square_is_named_after_the_place_in_it(client):
    """The list beside the heat map is its text alternative, and "37.332,
    -122.031" names nothing a customer recognises. The archive holds the
    address of every one of these squares, so the busiest ones carry it.

    SQUARE here, SPOT on the page: the unit of counting is a cell of a grid
    of about 110 metres, which is what this endpoint returns, and the map
    draws a soft field with no straight edge in it. static/js/heatmap.js
    says why the reader is told the second word."""
    data = heat(client)
    top = data["top"]
    assert top, "no square was named"
    assert len(top) <= len(data["points"])
    # Same order and same numbers as the layer's own points.
    for square, point in zip(top, data["points"]):
        assert (square["lat"], square["lng"], square["count"]) == tuple(point)
    named = {s["address"] for s in top}
    assert {"Cupertino, California, USA", "Redmond, Washington, USA",
            "Brussels, Belgium", "Taipei, Taiwan"} <= named
    # The German half of Alpha is the same map with translated addresses.
    german = client.get("/api/map/heat", params=DE).json()
    assert {s["address"] for s in german["top"]} != named
    assert [s["count"] for s in german["top"]] == [s["count"] for s in top]


def test_a_bbox_counts_only_what_is_inside_it(client):
    everything = heat(client)
    europe = heat(client, {"bbox": "45,0,55,10"})
    assert europe["cells"] < everything["cells"]
    for lat, lng, _ in europe["points"]:
        assert 45 <= lat <= 55 and 0 <= lng <= 10


def test_the_heat_checklist_lists_the_location_types(client):
    data = heat(client)
    types = {t["name"]: t["count"] for t in data["types"]}
    assert {"Factory", "Headquarters", "Seat"} <= set(types)
    only_factories = heat(client, {"types": "Factory"})
    assert only_factories["locations"] == types["Factory"]
    # The checklist stays complete even while a filter is on.
    assert {t["name"] for t in only_factories["types"]} == set(types)


def test_a_bbox_that_is_not_four_numbers_is_a_400(client):
    body = client.get("/api/map/heat", params={**EN, "bbox": "45,0,55"}).json()
    assert "bbox" in body["error"] and body["hint"]
    heat(client, {"bbox": "45,0,55"}, expect=400)
    heat(client, {"bbox": "north,south,east,west"}, expect=400)


def test_heat_is_bound_to_one_project_and_language(client):
    alpha = heat(client)
    beta = client.get("/api/map/heat", params=BETA_EN).json()
    assert beta["cells"] == 1        # Contoso in Berlin
    assert beta["locations"] < alpha["locations"]


# ── The place the heat map's own search box asks for ─────────
#
# Every view's search box searches the thing that view is about, and this one
# counts ADDRESSES: `q` here is a place, never an entity. The name is read
# with the Query view's rule (a city beats a country beats a fragment of an
# address), but where the Query view has to ASK which Springfield is meant,
# a count answers for both and names them - "2 locations in 2 squares" is a
# true answer about squares, and the names are what say which squares.

def test_a_city_name_counts_every_town_of_that_name_and_names_them(client):
    everything = heat(client)
    springfield = heat(client, {"q": "Springfield"})
    assert springfield["cells"] == 2 and springfield["locations"] == 2
    assert springfield["cells"] < everything["cells"]
    assert springfield["place"]["match"] == "city"
    assert springfield["place"]["places"] == 2
    # Named as ADDRESSES, the form the archive writes and the list beside the
    # map shows - the caption puts these into a sentence.
    assert springfield["place"]["names"] == ["Springfield, Illinois, USA",
                                             "Springfield, Ontario, Canada"]
    assert {s["address"] for s in springfield["top"]} == set(springfield["place"]["names"])


def test_a_country_counts_every_address_in_it(client):
    usa = heat(client, {"q": "USA"})
    assert usa["place"]["match"] == "country" and usa["place"]["names"] == ["USA"]
    assert {s["address"] for s in usa["top"]} == {
        "Cupertino, California, USA", "Redmond, Washington, USA",
        "Springfield, Illinois, USA",
        # Apple Inc.'s second office, in one of its two tasks: what lets the
        # Map tell "every address of this entity" from "one address each"
        # (preseed.py: EXTRA_LOCATIONS).
        "Austin, Texas, USA"}
    assert usa["locations"] == 6 and usa["cells"] == 4


def test_a_whole_address_counts_only_that_address(client):
    """What choosing a suggestion sends: the suggestion list offers whole
    addresses, so the commonest search of all must be the narrowest one."""
    one = heat(client, {"q": "Cupertino, California, USA"})
    assert one["cells"] == 1 and one["locations"] == 2
    assert one["place"]["match"] == "address"
    assert one["place"]["names"] == ["Cupertino, California, USA"]


def test_a_fragment_of_an_address_counts_the_addresses_it_was_offered_for(client):
    """"field" is no city and no country; it is part of two addresses, and the
    suggestion list offers both. Counting it as a city would answer nothing
    for a term the field itself proposes."""
    data = heat(client, {"q": "field"})
    assert data["place"]["match"] == "address"
    assert sorted(data["place"]["names"]) == ["Springfield, Illinois, USA",
                                              "Springfield, Ontario, Canada"]
    assert data["locations"] == 2


def test_a_place_the_archive_does_not_know_counts_nothing_and_says_so(client):
    """Not `TRUE`, which would answer a search for Atlantis with the whole
    archive - the picture a reader would take for an answer."""
    data = heat(client, {"q": "Atlantis"})
    assert data["place"]["match"] == "none" and data["place"]["names"] == []
    assert data["cells"] == 0 and data["locations"] == 0
    assert data["points"] == [] and data["top"] == []


def test_the_place_is_bound_to_the_language_like_everything_else(client):
    """The archive translates addresses (Zurich/Zürich, Canada/Kanada), and a
    view bound to a language must not answer in another one."""
    english = heat(client, {"q": "Zurich"})
    assert english["locations"] == 1
    german = client.get("/api/map/heat", params={**DE, "q": "Zürich"}).json()
    assert german["locations"] == 1 and german["place"]["names"] == ["Zürich, Schweiz"]
    # The English spelling is not an address in the German half.
    missing = client.get("/api/map/heat", params={**DE, "q": "Zurich"}).json()
    assert missing["place"]["match"] == "none" and missing["cells"] == 0


def test_the_checklist_beside_a_place_holds_that_places_types(client):
    """The checklist is what the map COULD show. With a place in the box that
    is the types of the place: ticking a type nothing there has would empty a
    map for a reason nobody could see."""
    everything = heat(client)
    springfield = heat(client, {"q": "Springfield"})
    assert {t["name"] for t in springfield["types"]} == {"Factory"}
    assert {t["name"] for t in everything["types"]} > {"Factory"}
    # And a type filter still applies on top of the place.
    assert heat(client, {"q": "USA", "types": "Factory"})["locations"] == 1


def test_a_place_and_a_bbox_are_both_true_at_once(client):
    """"Only the visible area" is a second question about the same map, not a
    replacement for the first one."""
    assert heat(client, {"q": "USA", "bbox": "45,0,55,10"})["cells"] == 0
    assert heat(client, {"q": "USA", "bbox": "20,-130,50,-80"})["cells"] == 4


def test_the_place_travels_into_the_exported_file(client):
    """A file exported from a map of Springfield that held every square in the
    archive would be read as the archive."""
    r = client.get("/api/export/heatmap.csv", params={**EN, "q": "Springfield"})
    assert r.status_code == 200, r.text
    lines = [line for line in r.text.splitlines() if line.strip()]
    assert len(lines) == 3          # a header and the two towns
    assert "Springfield, Illinois, USA" in r.text
    assert "Cupertino" not in r.text
    body = client.get("/api/export/heatmap.json", params={**EN, "q": "Springfield"}).json()
    assert body["export"]["filters"]["q"] == "Springfield"
    assert body["context"]["place"]["places"] == 2

    # A hidden type is applied by asking the server a second time (a heat
    # square has no type to filter on afterwards), and that second question
    # has to carry the place as well.
    both = client.get("/api/export/heatmap.csv",
                      params={**EN, "q": "USA", "hide": "Headquarters"})
    rows = [line for line in both.text.splitlines() if line.strip()][1:]
    # Springfield (a City) and Austin (an Office); the two head offices go.
    assert len(rows) == 2
    assert {"Springfield, Illinois, USA", "Austin, Texas, USA"} == {
        line.split(",", 3)[3].strip('"') for line in rows}


# ── The graph ────────────────────────────────────────────────

def test_the_first_ring_of_a_bucket(client):
    data = graph(client, {"q": "Apple"})
    assert data["entity"]["kind"] == "bucket"
    assert data["entity"]["name"] == "Apple"
    assert set(data["entity"]["ids"]) == {"ent:apple", "ent:apple-inc"}
    ring = {n["name"]: n for n in data["neighbours"]}
    assert set(ring) == {"Foxconn", "Microsoft", "European Commission", "Tim Cook"}
    # Labelled with what the NEIGHBOUR is, not with what the centre is.
    assert [t["name"] for t in ring["Foxconn"]["types"]] == ["Supplier"]
    assert ring["Foxconn"]["group"] == "supplier"
    assert ring["Tim Cook"]["group"] == "person"
    assert ring["European Commission"]["group"] == "regulator"
    # Two members of the bucket connected to each other are not a neighbour:
    # it would be a loop on the root.
    assert "Apple Inc." not in ring


def test_a_neighbour_carries_its_counts_and_dates(client):
    data = graph(client, {"q": "Apple"})
    foxconn = next(n for n in data["neighbours"] if n["name"] == "Foxconn")
    assert foxconn["count"] == 2          # the fact and its reversed duplicate
    assert foxconn["first"] and foxconn["last"] and foxconn["first"] <= foxconn["last"]
    assert foxconn["type"] == "Company"
    assert foxconn["types"][0]["count"] == 2


def test_expanding_by_id_gives_the_next_ring(client):
    data = graph(client, {"id": "ent:nordwyk"})
    assert data["entity"]["kind"] == "entity"
    assert data["entity"]["name"] == "Nordwyk Pumps"
    assert names(data["neighbours"]) == {"Zurich Insurance", "Springfield Mills",
                                         "Springfield Works"}
    works = next(n for n in data["neighbours"] if n["name"] == "Springfield Works")
    assert [t["name"] for t in works["types"]] == ["Customer"]


def test_expanding_a_bucket_member_by_id_expands_the_whole_bucket(client):
    """Clicking the node "Apple" must give the same ring as searching for it,
    or the same name would mean two different things on one page."""
    by_id = graph(client, {"id": "ent:apple"})
    by_name = graph(client, {"q": "Apple"})
    assert by_id["entity"]["kind"] == "bucket"
    assert set(by_id["entity"]["ids"]) == set(by_name["entity"]["ids"])
    assert names(by_id["neighbours"]) == names(by_name["neighbours"])


def test_an_unmapped_type_falls_back_instead_of_disappearing(client):
    data = graph(client, {"id": "ent:zurich"})
    nordwyk = next(n for n in data["neighbours"] if n["name"] == "Nordwyk Pumps")
    types = {t["name"]: t for t in nordwyk["types"]}
    assert set(types) == {"Partner", "Mysterious Bond"}
    assert types["Mysterious Bond"]["group"] == "other"
    assert types["Mysterious Bond"]["colour"] == "#7D4B12"
    # A tie between a grouped type and the fallback goes to the grouped one.
    assert nordwyk["group"] == "partner"


def test_the_graph_speaks_the_requested_language(client):
    english = graph(client, {"q": "Apple"})
    german = client.get("/api/graph/neighbours", params={**DE, "q": "Apple"}).json()
    assert {n["id"] for n in german["neighbours"]} == {n["id"] for n in english["neighbours"]}
    types = {n["name"]: [t["name"] for t in n["types"]] for n in german["neighbours"]}
    assert types["Foxconn"] == ["Lieferant"]
    assert types["Europäische Kommission"] == ["Regulierungsbehörde"]
    assert next(n for n in german["neighbours"] if n["name"] == "Foxconn")["group"] == "supplier"


def test_asking_for_nothing_and_asking_for_a_ghost(client):
    graph(client, {}, expect=400)
    body = client.get("/api/graph/neighbours", params={**EN, "id": "ent:nobody"})
    assert body.status_code == 404
    assert body.json()["hint"]
    empty = graph(client, {"q": "Zzz Qqq"})
    assert empty["entity"] is None and empty["neighbours"] == []
    assert empty["resolved"]["match"] == "none"


def test_the_legend_is_the_whole_colour_scale(client):
    groups = client.get("/api/graph/legend", params=EN).json()["groups"]
    assert len(groups) == 11
    assert [g["key"] for g in groups][:2] == ["competitor", "regulator"]
    assert sum(1 for g in groups if g["fallback"]) == 1
    assert all(g["colour"].startswith("#") and len(g["colour"]) == 7 for g in groups)


# ── One ring, one click, no waiting ──────────────────────────────────────
#
# The neighbours query must not look each neighbour's name up in a LEFT JOIN
# LATERAL, once per edge row. On this 36-entity preseed that costs 2.5-3.6
# SECONDS - every other query over the same table answers in single-digit
# milliseconds - because `entities` is a hypertable and a lateral is
# re-planned and re-scanned per outer row, so a handful of edges pays for a
# few hundred chunk scans. The whole interaction model of the graph is
# "click a node to draw its own connections", and that made every click a
# click-and-wait. One DISTINCT ON over the ids the edges name does the same
# job in one scan.
#
# The bound is deliberately loose (a busy CI box is not a benchmark rig) and
# still an order of magnitude below what a lateral costs. What it really pins
# is the SHAPE: the ring of a term and the ring of an id must not cost
# fundamentally more than the map, which walks two levels of the same table.

GRAPH_BUDGET_SECONDS = 1.0


def _timed(client, params: dict) -> float:
    start = time.perf_counter()
    r = client.get("/api/graph/neighbours", params={**EN, **params})
    assert r.status_code == 200, r.text
    return time.perf_counter() - start


def test_a_ring_answers_without_a_wait(client):
    graph(client, {"q": "Apple"})            # warm the pools and the caches
    for params in ({"q": "Apple"}, {"id": "ent:microsoft"}, {"q": "Zurich Insurance"}):
        # Twice, because the first of a pair can pay for a plan.
        spent = min(_timed(client, params), _timed(client, params))
        assert spent < GRAPH_BUDGET_SECONDS, (
            f"/api/graph/neighbours?{params} took {spent * 1000:.0f} ms")


def test_a_ring_costs_about_what_the_map_costs(client):
    """The map walks two levels of the same connections table and answers in
    tens of milliseconds. One hop must not be an order of magnitude worse."""
    map_of(client, {"q": "Apple", "levels": 2})
    start = time.perf_counter()
    map_of(client, {"q": "Apple", "levels": 2})
    on_map = time.perf_counter() - start
    in_graph = min(_timed(client, {"q": "Apple"}), _timed(client, {"q": "Apple"}))
    assert in_graph < max(0.25, on_map * 25), (
        f"one ring took {in_graph * 1000:.0f} ms against {on_map * 1000:.0f} ms for "
        "two levels of the map")


def test_the_map_and_the_graph_agree_about_one_pair(client):
    """The same relationship must not be blue on one page and orange on the
    other - that is what the colour table is for."""
    on_map = pair(map_of(client, {"q": "Apple"}), "Apple Inc.", "Foxconn")
    ring = graph(client, {"q": "Apple"})
    in_graph = next(n for n in ring["neighbours"] if n["name"] == "Foxconn")
    assert on_map["group"] == in_graph["group"]
    assert on_map["colour"] == in_graph["colour"]
    assert [t["name"] for t in on_map["types"]] == [t["name"] for t in in_graph["types"]]


# ── The date range, on both endpoints ────────────────────────
#
# The preseed dates its rows RELATIVE to now (a task is "60 days old"), so
# every bound here is computed the same way. The one row that is two years
# old is the Commission's action against Microsoft, which is what makes
# "everything except the old one" a question with a known answer.

def days_ago(n: int) -> str:
    return (date.today() - timedelta(days=n)).isoformat()


def test_the_graph_takes_a_date_range_and_narrows_the_edges(client):
    """From/To go with every graph request and the service honours them:
    "who was this connected to last year" has to be askable."""
    whole = graph(client, {"q": "Microsoft"})
    assert names(whole["neighbours"]) == {"Apple", "European Commission"}

    # The Commission's action is two years old; ten days ago leaves it out.
    recent = graph(client, {"q": "Microsoft", "date_from": days_ago(10)})
    assert names(recent["neighbours"]) == {"Apple"}, recent["neighbours"]
    assert recent["date_from"].startswith(days_ago(10))

    # And the other way round: only the old one.
    old = graph(client, {"q": "Microsoft", "date_to": days_ago(365)})
    assert names(old["neighbours"]) == {"European Commission"}, old["neighbours"]


def test_a_graph_emptied_by_its_dates_says_how_many_it_left_out(client):
    """"Nothing is connected to it" is about the archive; "nothing in these
    three months" is about the toolbar, and only the second one is undone by
    clearing a field. The answer counts what the range left out so the page
    can tell them apart."""
    empty = graph(client, {"q": "Microsoft",
                           "date_from": days_ago(3650), "date_to": days_ago(3600)})
    assert empty["neighbours"] == []
    assert empty["without_dates"] == 2, empty["without_dates"]

    # Without a range there is nothing to explain, and the second query is
    # not made at all.
    nothing = graph(client, {"q": "Linjiang Chemical"})
    assert nothing["neighbours"] == []
    assert nothing["without_dates"] == 0


def test_the_graph_refuses_a_date_that_is_not_one(client):
    bad = graph(client, {"q": "Apple", "date_from": "yesterday"}, expect=400)
    assert "date_from" in bad["error"] and bad["hint"]
    backwards = graph(client, {"q": "Apple", "date_from": days_ago(1),
                               "date_to": days_ago(30)}, expect=400)
    assert "ends before it starts" in backwards["error"]


def test_the_heat_grid_takes_a_date_range(client):
    whole = heat(client)
    recent = heat(client, {"date_from": days_ago(10)})
    assert recent["cells"] < whole["cells"], (recent["cells"], whole["cells"])
    assert recent["locations"] < whole["locations"]
    assert recent["date_from"].startswith(days_ago(10))

    none = heat(client, {"date_from": days_ago(3650), "date_to": days_ago(3600)})
    assert none["cells"] == 0 and none["locations"] == 0

    bad = heat(client, {"date_to": "soon"}, expect=400)
    assert "date_to" in bad["error"] and bad["hint"]


# ── The entity axis on the Heatmap ───────────────────────────
#
# A heat view that searches only ENTITIES cannot be asked about a place, and
# one that asks only for a place cannot be asked about an entity. Both are
# here and they AND together.

def test_the_heat_grid_can_be_asked_about_one_entity(client):
    whole = heat(client)
    apple = heat(client, {"entity": "Apple"})
    assert 0 < apple["locations"] < whole["locations"], (apple, whole["locations"])
    # A BUCKET COUNTS EVERY MEMBER, exactly as it does on the Map and the
    # Graph - one rule for one word across the product.
    assert apple["resolved"]["match"] == "bucket"
    assert apple["resolved"]["label"] == "Apple"
    # The checklist is narrowed with it: the types a reader can tick are the
    # types of what they are looking at.
    assert {t["name"] for t in apple["types"]} <= {t["name"] for t in whole["types"]}


def test_the_entity_and_the_place_are_both_true_at_once(client):
    """The place box keeps its meaning; the entity box is an addition. "Apple
    in Cupertino" is one question with one answer, and neither half may
    quietly replace the other."""
    apple = heat(client, {"entity": "Apple"})
    cupertino = heat(client, {"q": "Cupertino"})
    both = heat(client, {"entity": "Apple", "q": "Cupertino"})
    assert both["locations"] <= min(apple["locations"], cupertino["locations"])
    assert both["locations"] > 0
    assert both["place"]["names"] == ["Cupertino, California, USA"]
    assert both["resolved"]["match"] == "bucket"

    # A place that has nothing to do with the entity is an empty answer, not
    # a wider one.
    elsewhere = heat(client, {"entity": "Apple", "q": "Taipei"})
    assert elsewhere["cells"] == 0


def test_an_entity_the_archive_does_not_know_counts_nothing_and_says_so(client):
    unknown = heat(client, {"entity": "Zzz Nobody Ltd"})
    assert unknown["cells"] == 0 and unknown["locations"] == 0
    assert unknown["resolved"]["match"] == "none"
    assert unknown["entity"] == "Zzz Nobody Ltd"


def test_the_entity_axis_is_bound_to_one_project_and_language(client):
    """Every endpoint answers about ONE project in ONE language; a filter is
    no reason to leave that."""
    english = heat(client, {"entity": "Apple"})
    r = client.get("/api/map/heat", params={**BETA_EN, "entity": "Apple"})
    assert r.status_code == 200, r.text
    other = r.json()
    assert other["locations"] != english["locations"] or other["cells"] != english["cells"]


# ── The second axis: an event type, on both views ────────────
#
# The Map's box and the Heatmap's entity box
# read their term either as ONE THING or as an EVENT TYPE, and an event type
# resolves to the entities its events are about (app/scope.py, the events
# scope's type axis). Nothing below the resolution knows which was asked -
# the same expansion, the same markers, the same grid - so what is checked
# here is the resolution, the echo and the refusal.

# The preseed's Delivery: "Pump delivered", about Springfield Works and
# Nordwyk Pumps, both of which the archive has coordinates for.
DELIVERY = "Delivery"


def entities_of_event_type(archive, type_name: str, project: str = ALPHA) -> int:
    """The same count, written independently in SQL, so the endpoint is
    measured against the archive rather than against itself."""
    return archive.scalar(
        "SELECT count(DISTINCT (ee.text_task_id, ee.text_fk_entity_id)) "
        "FROM processed_data.events ev "
        "JOIN processed_data.event_entities ee "
        "  ON ee.text_task_id = ev.text_task_id AND ee.bigint_fk_event_id = ev.bigint_id "
        " AND ee.text_project = ev.text_project AND ee.text_language = ev.text_language "
        "WHERE ev.text_project = %s AND lower(ev.text_type) = %s "
        "  AND ee.text_fk_entity_id IS NOT NULL AND ee.text_fk_entity_id <> ''",
        (project, type_name.lower()))


def test_an_event_type_maps_the_entities_its_events_are_about(client, archive):
    data = map_of(client, {"q": DELIVERY, "axis": "type", "mode": "locations"})
    assert data["axis"] == "type"
    assert data["resolved"]["kind"] == "events" and data["resolved"]["axis"] == "type"
    assert data["resolved"]["label"] == DELIVERY
    assert names(data["entities"]) == {"Springfield Works", "Nordwyk Pumps"}
    # And the size the caption prints is the archive's own count.
    assert data["resolved"]["entities"] == entities_of_event_type(archive, DELIVERY)
    # The pins are those entities' addresses, drawn like any other.
    assert {m["entity"] for m in data["markers"]} == {"Springfield Works", "Nordwyk Pumps"}


def test_the_type_axis_follows_the_connections_like_any_other_search(client):
    """The axis changes the resolution and nothing else: the levels still
    expand, so a delivery's two ends bring their own neighbours."""
    alone = map_of(client, {"q": DELIVERY, "axis": "type", "mode": "locations"})
    around = map_of(client, {"q": DELIVERY, "axis": "type", "levels": 1})
    assert len(around["entities"]) > len(alone["entities"])
    assert names(alone["entities"]) <= names(around["entities"])
    assert around["connections"], "a level-1 map of an event type draws no line"


def test_an_event_type_nobody_has_is_an_empty_map_and_not_an_error(client):
    data = map_of(client, {"q": "Zzz Qqq", "axis": "type"})
    assert data["resolved"]["match"] == "none"
    assert data["resolved"]["kind"] == "events"
    assert data["entities"] == [] and data["markers"] == []


def test_an_axis_the_endpoint_does_not_have_is_refused(client):
    """A map drawn on the other axis is a different map, so an unknown axis
    is a 400 rather than a silent fallback to the default."""
    body = client.get("/api/map/entity", params={**EN, "q": "Apple", "axis": "sideways"}).json()
    assert "axis" in body["error"] and body["hint"]
    body = client.get("/api/map/heat", params={**EN, "entity": "Apple",
                                               "entity_axis": "sideways"}).json()
    assert "axis" in body["error"] and body["hint"]


def test_the_heat_grid_takes_an_event_type_on_its_entity_axis(client):
    whole = heat(client)
    delivery = heat(client, {"entity": DELIVERY, "entity_axis": "type"})
    assert delivery["entity_axis"] == "type"
    assert delivery["resolved"]["kind"] == "events"
    assert delivery["resolved"]["axis"] == "type"
    assert 0 < delivery["locations"] < whole["locations"]
    # The same term on the OBJECT axis is nobody: "Delivery" is not an
    # entity, which is exactly why the axis was needed.
    as_entity = heat(client, {"entity": DELIVERY})
    assert as_entity["resolved"]["match"] == "none"
    assert as_entity["locations"] == 0


def test_an_event_type_and_a_place_are_both_true_at_once(client):
    """The two axes of this view are not the two axes of the field: the
    place still ANDs with whatever the entity field resolved to."""
    delivery = heat(client, {"entity": DELIVERY, "entity_axis": "type"})
    both = heat(client, {"entity": DELIVERY, "entity_axis": "type", "q": "Rotterdam"})
    assert 0 < both["locations"] < delivery["locations"]
    assert both["place"]["match"] != "none"
