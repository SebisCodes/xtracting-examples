"""What a click on a hot spot lists, against a real archive.

    DATABASE_URL=postgresql://... python -m pytest tests/flow/test_heat_spot.py -q

The Heatmap's field is one canvas and a spot has no id: `/heat` answers with
aggregated cells and the only key a spot has is the pair of rounded numbers
the browser was sent. `/spot` takes those two back, re-derives the cell and
re-applies the filters the field was drawn with. Two things can go wrong and
both of them look right on the screen:

  * THE WRONG CELL. A rounding that does not match the grid's lists the
    neighbouring 110 metres under this spot's name.
  * THE WRONG SET. Applying three of the four filters lists rows the spot
    was never counted from, and the dialog then says a bigger number than
    the row beside the map does with nothing on screen to explain it.

So every test here compares `/spot` against `/heat` FOR THE SAME CELL AND
THE SAME FILTERS, which is the only comparison that can catch either.

The rest is the drilldown envelope - most results first, twenty to a page,
the running offset, `more` without a second query - and the one rule the
customer decided: a bucket is ONE row under its own name.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.charts.places import PAGE_SIZE
from preseed import ALPHA

pytestmark = pytest.mark.flow

PARAMS = {"project": ALPHA, "language": "English"}

# Apple Inc.'s head office: 37.3318, -122.0312 in the preseed, which the
# grid rounds to three decimals. Written here as the browser writes it back
# (toFixed(3)), because that is the string this endpoint has to accept.
CUPERTINO = {"lat": "37.332", "lng": "-122.031"}
# Its second office, in one task only (preseed: EXTRA_LOCATIONS).
AUSTIN = {"lat": "30.267", "lng": "-97.743"}


def heat(client, **filters) -> dict:
    r = client.get("/api/map/heat", params={**PARAMS, **filters})
    assert r.status_code == 200, r.text
    return r.json()


def spot(client, at=CUPERTINO, **filters) -> dict:
    r = client.get("/api/map/spot", params={**PARAMS, **at, **filters})
    assert r.status_code == 200, r.text
    return r.json()


def cell_of(field: dict, at=CUPERTINO) -> dict | None:
    """The one cell of a heat answer that a spot request is about."""
    for point in field["points"]:
        if f"{point[0]:.3f}" == at["lat"] and f"{point[1]:.3f}" == at["lng"]:
            return {"lat": point[0], "lng": point[1], "count": point[2]}
    return None


def names(answer: dict) -> list[str]:
    return [row["entity"] for row in answer["rows"]]


def counts(answer: dict) -> list[int]:
    return [int(row["cells"]["count"][0]["text"].replace(",", "")) for row in answer["rows"]]


# ── The spot the reader clicked ──────────────────────────────

def test_the_spot_lists_what_is_standing_in_it(client):
    """Two locations, one thing standing there, and the two numbers the
    dialog says are both on the answer."""
    answer = spot(client)
    assert answer["locations"] == 2
    assert answer["total"] == 1
    assert answer["more"] is False
    assert answer["offset"] == 0
    assert answer["lat"] == 37.332 and answer["lng"] == -122.031


def test_a_bucket_is_one_row_under_its_own_name(client):
    """The preseed's Apple bucket holds Apple Inc. and Apple, and Apple Inc.
    is what stands in Cupertino. The rest of the dashboard answers a bucket
    as ONE thing, and so does this list - four spellings at one address are
    one row, not four a reader has to add up."""
    answer = spot(client)
    assert names(answer) == ["Apple"]
    assert counts(answer) == [2]


def test_a_row_carries_the_facts_the_column_headers_promise(client):
    """The Source cell counts the documents rather than naming one of them:
    an entity in a spot was usually written about by several, and a link
    labelled with one of their domains would be a claim that is not true of
    the row."""
    row = spot(client)["rows"][0]
    assert row["link"] == {"uri": "", "domain": "", "title": "2 documents"}
    assert [p["text"] for p in row["cells"]["kinds"]] == ["Headquarters"]
    assert row["cells"]["address"][0]["text"] == "Cupertino, California, USA"
    assert row["cells"]["newest"][0]["iso"]
    assert [c["key"] for c in spot(client)["columns"]] == [
        "count", "kinds", "address", "newest"]
    assert spot(client)["row_links"] == {"source": False, "entity": True}


def test_the_neighbouring_110_metres_is_a_different_spot(client):
    """The whole reason the rounding has to match the grid's. Apple Inc. is
    at 37.332 and nothing is at 37.333."""
    assert spot(client, at={"lat": "37.333", "lng": "-122.031"})["total"] == 0
    assert spot(client, at={"lat": "37.333", "lng": "-122.031"})["rows"] == []
    # The trailing zeros a browser may or may not send are a spelling.
    assert spot(client, at={"lat": "37.3320", "lng": "-122.0310"})["total"] == 1


@pytest.mark.parametrize("at", [
    {"lat": "north", "lng": "-122.031"},
    {"lat": "", "lng": "-122.031"},
    {"lat": "37.332", "lng": "nowhere"},
    {"lat": "91", "lng": "0"},
    {"lat": "0", "lng": "181"},
])
def test_a_spot_that_is_not_a_point_on_the_world_is_refused(client, at):
    """Refused rather than defaulted: a listing that quietly answered about
    0, 0 is a dialog about the Gulf of Guinea, and it would look right."""
    r = client.get("/api/map/spot", params={**PARAMS, **at})
    assert r.status_code == 400, r.text
    body = r.json()
    assert body["error"] and body["hint"]


# ── The same four filters the field was drawn with ───────────

def test_the_count_in_the_dialog_is_the_count_beside_the_map(client):
    """Unfiltered, and then with each of the four filters in turn: the
    spot's own count has to be the heat cell's count every time, or the
    dialog is listing rows the field was never painted from."""
    for filters in (
            {},
            {"types": "Headquarters"},
            {"q": "Cupertino, California, USA"},
            {"entity": "Apple"},
            {"date_from": "2000-01-01"},
    ):
        cell = cell_of(heat(client, **filters))
        assert cell is not None, filters
        assert spot(client, **filters)["locations"] == cell["count"], filters


def test_a_type_the_spot_does_not_carry_empties_it(client):
    """Cupertino is a Headquarters and Austin is an Office. Ticking only
    Office takes the Cupertino cell off the map, and the list behind it has
    to go with it - a dialog that still listed two would be answering a
    question the reader has just unticked."""
    field = heat(client, types="Office")
    assert cell_of(field) is None
    assert cell_of(field, AUSTIN) is not None
    listing = spot(client, types="Office")
    assert listing["total"] == 0 and listing["locations"] == 0
    assert spot(client, at=AUSTIN, types="Office")["locations"] == 1


def test_another_place_empties_it(client):
    """The place box narrows the grid, so it narrows the spot: Cupertino is
    not in Springfield."""
    assert spot(client, q="Springfield")["total"] == 0


def test_another_entity_empties_it(client):
    """The entity box is the second axis and it is AND-ed with the rest.
    Microsoft does not stand in Cupertino."""
    assert spot(client, entity="Microsoft")["total"] == 0
    # …and the answer says what the term became, in the same shape the grid
    # reports it, so the dialog can say which of the two emptied it.
    assert spot(client, entity="Microsoft")["resolved"]["label"] == "Microsoft"


def test_a_date_range_that_ends_before_the_archive_starts_empties_it(client):
    assert spot(client, date_to="2000-01-01")["total"] == 0


def test_an_event_type_on_the_second_axis_reads_the_same_as_the_grid(client):
    """`entity_axis=type` counts the addresses of every entity the events of
    that kind are about. Whatever it counts, the grid and the spot have to
    count the same thing."""
    filters = {"entity": "Recall", "entity_axis": "type"}
    cell = cell_of(heat(client, **filters))
    listing = spot(client, **filters)
    assert listing["locations"] == (cell["count"] if cell else 0)


# ── Most results first, twenty at a time ─────────────────────
#
# The preseed is a small readable archive: every one of its spots holds one
# entity, so it can say nothing about ranking or about a page boundary.
# These two put a crowd in one spot for the length of the test and take it
# out again, the way tests/flow/test_query_places.py does for the two shapes
# of place it needs.

TASK = "_spot crowd"
# Chosen well away from a half-way case, so the fixture and the grid cannot
# disagree about which cell these rows fall in: 48.8584 / 2.2941 round to
# 48.858 / 2.294 whichever way a tie would have gone.
CROWD = {"lat": "48.858", "lng": "2.294"}
# 25 entities, so there is a second page - and each carries a different
# number of locations, so "most results first" has something to order.
SIZES = list(range(1, 26))


@pytest.fixture
def crowd(archive):
    now = datetime.now(timezone.utc)
    with archive.connect() as conn:
        for i, rows in enumerate(SIZES):
            eid = f"_spot{i:02d}"
            conn.execute(
                """INSERT INTO processed_data.entities
                     (date_added, text_name, text_project, text_language, text_task_id,
                      text_job_id, text_entity_id, text_fk_source_id, text_type,
                      date_commissioned, date_evaluated)
                   VALUES (%s,%s,%s,'English',%s,%s,%s,'',%s,%s,%s)""",
                (now, f"_spot tenant {i:02d}", ALPHA, TASK, TASK, eid, "Company", now, now))
            for n in range(rows):
                conn.execute(
                    """INSERT INTO processed_data.locations
                         (date_added, text_name, text_project, text_language, text_task_id,
                          text_job_id, text_fk_entity_id, text_fk_source_id, text_type,
                          text_address, float_latitude, float_longitude,
                          date_commissioned, date_evaluated)
                       VALUES (%s,%s,%s,'English',%s,%s,%s,%s,'Office',%s,%s,%s,%s,%s)""",
                    (now, "Tower", ALPHA, TASK, TASK, eid, f"{TASK}:{n}",
                     "Champ de Mars, Paris, France", 48.8584, 2.2941, now, now))
        conn.commit()
    yield
    with archive.connect() as conn:
        for table in ("locations", "entities"):
            conn.execute(f"DELETE FROM processed_data.{table} WHERE text_task_id = %s", (TASK,))
        conn.commit()


def test_the_busiest_entity_is_first(client, crowd):
    """"Most results first" is the whole request. A list ordered any other
    way makes the reader scroll to find what the hot spot is hot BECAUSE
    of."""
    first = spot(client, at=CROWD)
    assert counts(first) == sorted(counts(first), reverse=True)
    assert counts(first)[0] == max(SIZES)
    # Named "Name (Type)" - the spelling app/scope.py resolves back to ONE
    # entity, and therefore the term the row's Diagrams link can carry. A
    # bare name would resolve to a BUCKET wherever one shares it.
    assert names(first)[0] == "_spot tenant 24 (Company)"


def test_the_page_is_twenty_and_the_second_one_carries_on_from_it(client, crowd):
    """The envelope every drilldown in this dashboard uses: twenty rows, an
    offset the running number is drawn from, and `more` decided by fetching
    one row more than a page rather than by asking a second time."""
    one = spot(client, at=CROWD)
    assert len(one["rows"]) == PAGE_SIZE
    assert one["offset"] == 0 and one["more"] is True
    assert one["total"] == len(SIZES)
    assert one["locations"] == sum(SIZES)

    two = spot(client, at=CROWD, ddpage=2)
    assert two["offset"] == PAGE_SIZE and two["more"] is False
    assert len(two["rows"]) == len(SIZES) - PAGE_SIZE
    # THE TWO PAGES ARE ONE LIST. A row on both of them, or a row on
    # neither, is what an order that is not total does to a paged list.
    assert not set(names(one)) & set(names(two))
    assert len(set(names(one)) | set(names(two))) == len(SIZES)
    assert counts(one)[-1] >= counts(two)[0]


def test_a_page_past_the_end_is_empty_and_says_so(client, crowd):
    """Not an error: a reader who scrolls hard, or a stale dialog, asks for
    a page that has gone. It answers with nothing rather than with the last
    page again."""
    past = spot(client, at=CROWD, ddpage=9)
    assert past["rows"] == [] and past["more"] is False
    assert past["total"] == 0 and past["locations"] == 0


def test_the_crowd_is_still_the_same_count_the_field_was_painted_with(client, crowd):
    cell = cell_of(heat(client), at=CROWD)
    assert cell is not None
    assert spot(client, at=CROWD)["locations"] == cell["count"] == sum(SIZES)


# ── The reader's perspective ─────────────────────────────────
#
# The global filter (app/sqlbuild.py: perspective_scope) narrows both map
# endpoints, and on one of them it narrows only HALF the drawing. Both facts
# are pinned here because both are easy to lose: the first silently, by a
# filter that never reaches a WHERE, and the second loudly, by somebody
# "finishing the job" and filtering the expansion too.

INVESTOR = {"perspective": "Maintenance", "min_importance": "High Importance"}


def test_the_grid_and_the_spot_are_narrowed_by_the_reader_s_perspective(client):
    """A location row carries the id of the document that reported it, so
    the question has an answer for it: the field is counted from the
    documents this reader watches, and the list behind a spot from the same
    ones. Both, or the dialog contradicts the picture it was opened from."""
    wide = heat(client)
    narrow = heat(client, **INVESTOR)
    assert 0 < narrow["locations"] < wide["locations"]
    assert narrow["cells"] < wide["cells"]

    # Cupertino's two rows come from documents that are not High Importance
    # for Maintenance, so the cell is off the map - and the list with it.
    assert cell_of(narrow) is None
    assert spot(client, **INVESTOR)["total"] == 0
    assert spot(client, **INVESTOR)["locations"] == 0


def test_a_spot_that_survives_the_filter_still_agrees_with_the_field(client):
    """The comparison that catches a filter applied to one of the two: for
    every cell the narrowed field still holds, the listing behind it has to
    count the same number of locations."""
    field = heat(client, perspective="Maintenance", min_importance="Low Importance")
    assert field["points"], "the preseed no longer has a cell that survives this filter"
    for point in field["points"][:5]:
        at = {"lat": f"{point[0]:.3f}", "lng": f"{point[1]:.3f}"}
        listing = spot(client, at=at, perspective="Maintenance",
                       min_importance="Low Importance")
        assert listing["locations"] == point[2], at


def test_the_map_filters_where_it_starts_and_not_the_network_around_it(client):
    """THE DECISION, MEASURED. Filtering the seed is right: the map starts
    from entities this reader's documents actually name. Filtering each HOP
    would cut paths in the middle - a supplier two steps away vanishing
    because the company between them was named only in a Low Importance
    document - and would draw two disconnected halves of one network with
    nothing on screen saying so.

    So: a perspective whose documents do not name Apple empties the map,
    and a perspective whose documents do gives back the SAME network as no
    filter at all.
    """
    def entities(**extra):
        r = client.get("/api/map/entity",
                       params={**PARAMS, "q": "Apple", "levels": 2, **extra})
        assert r.status_code == 200, r.text
        return {e["id"] for e in r.json()["entities"]}

    whole = entities()
    assert len(whole) > 1, "the preseed no longer expands past the search term"
    # The seed is gone: nothing to draw, rather than a network with holes.
    assert entities(**INVESTOR) == set()
    # The seed survives, so every hop the unfiltered map found is still here
    # - the expansion is deliberately not filtered.
    assert entities(perspective="Compliance", min_importance="High Importance") == whole


def test_the_map_under_a_diagrams_tab_is_narrowed_with_the_bars_above_it(client):
    """A PAGE THAT SAYS "EVERY COUNT HERE IS A SUBSET" HAS TO MEAN THE
    PICTURE TOO. The Locations and Connections tabs draw a map under their
    charts (app/charts/maps.py), built from its own SQL rather than through
    the drilldown module's `_where` - so a filter applied to all 48 charts
    does not reach it by itself, and the map could go on showing places the
    bars above it no longer count.

    The addresses the CONNECTIONS map places its lines at are still
    unfiltered, for the same reason they are unwindowed: they are standing
    facts used to put a line the filter has already chosen on the world, and
    filtering them would take a chosen line off the map altogether.
    """
    def picture(tab, **extra):
        r = client.get("/api/diagrams/summary/map",
                       params={**PARAMS, "tab": tab, "timeframe": "5y", **extra})
        assert r.status_code == 200, r.text
        return r.json()

    for tab, drawn in (("locations", "places"), ("connections", "lines")):
        wide = picture(tab)
        some = picture(tab, perspective="Maintenance", min_importance="Low Importance")
        strict = picture(tab, **INVESTOR)
        counts = [len(wide[drawn]), len(some[drawn]), len(strict[drawn])]
        assert counts[0] > counts[1] > counts[2] > 0, f"{tab}: {counts}"
