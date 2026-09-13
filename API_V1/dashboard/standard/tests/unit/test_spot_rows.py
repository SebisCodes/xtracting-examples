"""What is inside one spot of the Heatmap, as SQL and as a row.

    python -m pytest tests/unit/test_spot_rows.py -q

The Heatmap's field is one canvas: a spot has no id, no object and nothing
on the server that names it - the only key it has is the pair of rounded
numbers `/heat` answered with. So the listing behind a click re-derives the
cell and re-applies the filters the field was drawn with, and getting either
of those wrong produces a dialog that looks right and is about somewhere
else, or about more than the reader is looking at.

Everything here is pure: the statement is rendered without a database and
read as text, and `shape_row` takes a database row as a dict and returns
what the dialog draws. What is pinned:

  * THE CELL IS RE-DERIVED WITH THE SAME ROUNDING the grid grouped by, in
    exact decimal arithmetic on both sides;
  * MOST RESULTS FIRST, with a tie broken by name so two pages of one list
    cannot reshuffle between them;
  * A BUCKET IS ONE ROW under its own name, folded inside the statement
    because a paged list cannot be folded after the LIMIT;
  * ONE ENTITY IS IN ONE ROW, not in every bucket that holds it - a list
    whose rows add up to more than the spot holds contradicts the field;
  * THE ROW'S LINK CARRIES A TERM THIS DASHBOARD CAN RESOLVE BACK.
"""

from __future__ import annotations

import doctest
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from psycopg import sql

from app.charts import places as spots
from app.sqlbuild import placeholders, render

NOW = datetime(2026, 5, 29, 9, 30, tzinfo=timezone.utc)


class Grouping:
    """As much of app/scope.py's Grouping as the builder reads."""

    def __init__(self, name: str, members: list[tuple[str, str | None]]) -> None:
        self.name = name
        self.members = [{"name": n, "type": t} for n, t in members]


APPLE = Grouping("Apple", [("Apple Inc.", "Company"), ("Apple", "Company")])


def statement(scope=None, where=None, groups=(APPLE,)):
    members = spots.bucket_members(groups)
    frag = where if where is not None else spots.cell_predicate("l", 3)
    text, params = spots.spot_statement(scope, frag, members)
    return render(text), params


def test_doctests():
    failed, _ = doctest.testmod(spots)
    assert not failed


# ── The cell ─────────────────────────────────────────────────

def test_a_coordinate_lands_back_on_the_grid_it_was_counted_on():
    """The browser sends back what the grid rounded to; anything else is a
    neighbouring cell, and the dialog would then list somebody else's
    addresses under this spot's name."""
    assert spots.cell("37.3318", 3, "lat") == Decimal("37.332")
    assert spots.cell("37.332", 3, "lat") == Decimal("37.332")
    # Half goes AWAY from zero, which is what PostgreSQL's round(numeric)
    # does - the two have to agree or the edge cells go to different sides.
    assert spots.cell("0.0005", 3, "lat") == Decimal("0.001")
    assert spots.cell("-0.0005", 3, "lat") == Decimal("-0.001")
    # Trailing digits and a sign on zero are spellings, not places.
    assert spots.cell("37.33200", 3, "lat") == Decimal("37.332")
    assert spots.cell("-0.000", 3, "lng") == Decimal("-0.000")


@pytest.mark.parametrize("value", ["", "  ", "north", "NaN", "Infinity", "1,5", None])
def test_a_coordinate_that_is_not_a_number_is_refused(value):
    """Refused rather than defaulted: a spot listing that quietly answered
    about 0, 0 is a dialog about the Gulf of Guinea."""
    with pytest.raises(ValueError):
        spots.cell(value, 3, "lat")


def test_the_cell_is_compared_as_the_grid_grouped_it():
    """`round(x::numeric, 3)` on both sides, and the value bound as a
    numeric. Through float8 it would be two roundings of one number, and
    37.333 would not have to equal 37.333."""
    text = render(spots.cell_predicate("l", 3))
    assert 'round("l".float_latitude::numeric,  3) = %(spot_lat)s' in text
    assert 'round("l".float_longitude::numeric, 3) = %(spot_lng)s' in text
    params = spots.cell_params(Decimal("37.332"), Decimal("-122.031"), 3)
    assert params["spot_lat"] == Decimal("37.332")
    assert params["spot_lng"] == Decimal("-122.031")


def test_an_index_is_given_a_range_it_can_read_and_the_exact_test_still_decides():
    """No index can answer `round(lat, 3) = c`, so on its own the listing
    reads every address in the project - 2.0 s over a large archive's 1.17 M
    of them, for a dialog somebody has just clicked. The plain columns ARE
    indexed, so the statement says where to look as well as what to keep.

    The range has to be a SUPERSET, closed at both ends: a value rounds to c
    exactly when it is within half a step, and a bound one row too tight
    would drop an address the field had counted."""
    text = render(spots.cell_predicate("l", 3))
    assert '"l".float_latitude  BETWEEN %(spot_lat_lo)s AND %(spot_lat_hi)s' in text
    assert '"l".float_longitude BETWEEN %(spot_lng_lo)s AND %(spot_lng_hi)s' in text
    p = spots.cell_params(Decimal("37.332"), Decimal("-122.031"), 3)
    assert (p["spot_lat_lo"], p["spot_lat_hi"]) == (37.3315, 37.3325)
    assert (p["spot_lng_lo"], p["spot_lng_hi"]) == (-122.0315, -122.0305)
    # Every value that rounds into the cell is inside the range.
    for value in ("37.33150", "37.3319", "37.332", "37.3324", "37.33249"):
        assert p["spot_lat_lo"] <= float(value) <= p["spot_lat_hi"], value


# ── The statement ────────────────────────────────────────────

def test_the_rows_are_the_busiest_first_and_the_order_is_total():
    """Most results first is the whole request. The alphabet after it is
    what makes the order TOTAL: two spots with equal counts have to come
    back in the same order twice running, or page 2 repeats a row page 1
    already showed and drops another off the end."""
    text, _ = statement()
    assert "ORDER BY g.n DESC, lower(g.x), g.bucketed DESC" in text


def test_one_page_is_the_page_every_other_drilldown_uses():
    """The dialog is the same dialog, so the page size is the same twenty
    and the two names it binds are the two names every listing binds."""
    from app.charts.drilldown import PAGE_SIZE

    assert spots.PAGE_SIZE == PAGE_SIZE
    text, _ = statement()
    assert "LIMIT %(dd_limit)s OFFSET %(dd_offset)s" in text


def test_both_totals_ride_on_the_statement_that_fetched_the_page():
    """"Row 1-20 of 214" and "417 locations in this spot" are two numbers
    the dialog says before it has loaded 214 rows. Window functions run
    before the LIMIT, so both cost nothing - the same trick every drilldown
    total is read from."""
    text, _ = statement()
    assert "count(*) OVER () AS groups" in text
    assert "sum(g.n) OVER () AS locations" in text


def test_a_bucket_is_folded_inside_the_statement():
    """IN the statement, because this list is PAGED: folded afterwards, the
    members that happened to land on one page would merge and the rest would
    stay as separate rows further down."""
    text, params = statement()
    assert "(VALUES" in text and "(ord, name, type, bucket)" in text
    # Every member is BOUND. A bucket name is text a customer typed.
    assert sorted(params) == ["pb_b0", "pb_b1", "pb_n0", "pb_n1", "pb_t0", "pb_t1"]
    assert params["pb_b0"] == "Apple"
    assert (params["pb_n0"], params["pb_t0"]) == ("apple inc.", "company")
    assert "Apple Inc." not in text, "a member was spliced into the SQL"


def test_one_entity_id_is_one_row_under_its_commonest_spelling():
    """This file's own doctrine about what an entity is: the extraction
    reuses an id whenever it recognises the same thing again, so an id is
    one company however many documents named it. Where two of them spell it
    differently the commonest wins and ties go to the alphabet - the rule
    _NAMES and charts/maps.py already follow, and what keeps a row's name
    the same between page 1 and page 2."""
    text, _ = statement()
    assert "SELECT DISTINCT ON (id) id, name, type" in text
    assert "ORDER BY id, seen DESC, name" in text


def test_the_names_are_one_join_and_not_one_lookup_per_address():
    """MEASURED, and it is the difference between a dialog and a hang. As a
    LATERAL per (task, entity) - the shape every drilldown ROW gets its
    entity through - the busiest spot of a large archive (72,712 addresses,
    15,098 entities) did not finish in 150 seconds. As one grouped semi-join
    it is 1.3 s. rows_statement cures the same disease by cutting the page
    before the lookup; that is not available here, because the grouping key
    IS what the lookup returns."""
    text, _ = statement()
    assert "e.text_entity_id IN (SELECT eid FROM pairs)" in text
    assert "LEFT JOIN LATERAL (SELECT e.text_name" not in text
    # The bucket's VALUES list is read once per ENTITY, not once per address.
    assert text.index("named AS") < text.index("folded AS") < text.index("grouped AS")


def test_an_entity_lands_in_exactly_one_bucket():
    """The MAP draws an entity in every bucket that holds it, because a
    connection to Angela Merkel really is a connection to Germany and to EU
    leaders. A LIST may not: two rows counting the same locations would add
    up to more than the spot holds and the number under the title would stop
    matching the field. So it is the first bucket in reading order -
    app/scope.py's `holder_for`, written as SQL."""
    text, _ = statement(groups=(APPLE, Grouping("Tech", [("Apple Inc.", None)])))
    assert 'ORDER BY "pb".ord LIMIT 1' in text
    assert "GROUP BY 1, 2" in text


def test_a_member_without_a_type_holds_the_name_whatever_its_type():
    """The preseed's Apple bucket holds Apple (Company) and deliberately not
    Apple (Fruit); a member recorded without a type means the other thing -
    this name, whichever type it carries. The same rule as BUCKET_LOOKUP_SQL
    and holders_for, so one archive answers one name one way."""
    assert spots.bucket_members([Grouping("Tech", [("Apple", None)])]) == [
        ("apple", None, "Tech")]
    text, _ = statement(groups=(Grouping("Tech", [("Apple", None)]),))
    assert '"pb".type IS NULL OR "pb".type = lower(n.type)' in text


def test_a_project_with_no_bucket_joins_no_empty_table():
    """Nothing to fold, and no reason to walk an empty VALUES list once per
    location row."""
    text, params = statement(groups=())
    assert params == {}
    assert "VALUES" not in text
    assert "folded" not in text
    assert "coalesce(NULL::text, w.name) AS x" in text


def test_the_statement_binds_everything_it_names():
    """Identity, the cell, the page - and nothing that is only in a comment.
    psycopg would raise too; this says which key is missing."""
    text, params = statement()
    supplied = {"project", "language", "dd_limit", "dd_offset",
                *spots.cell_params(Decimal("1"), Decimal("2"), 3), *params}
    assert placeholders(text) - supplied == set()


def test_the_caller_s_filters_are_the_only_ones():
    """The builder never composes a WHERE of its own. The heat field and
    this list have to be narrowed by the same four things, and the one place
    that decides them is routers/api_map.py's _heat_filters - a builder with
    a filter of its own would be a fifth that only one of the two applies."""
    where = sql.SQL("({c}) AND l.text_type = ANY(%(types)s)").format(
        c=spots.cell_predicate("l", 3))
    text, _ = statement(where=where)
    assert "l.text_type = ANY(%(types)s)" in text


# ── One row ──────────────────────────────────────────────────

def row(**over):
    base = {"x": "Apple Inc.", "bucketed": False, "n": 4, "docs": 2,
            "type": "Company", "address": "Cupertino, California, USA",
            "kinds": ["Headquarters", "Office"], "kinds_total": 2, "newest": NOW}
    base.update(over)
    return spots.shape_row(base)


def test_the_columns_are_the_ones_the_shell_does_not_draw_itself():
    """The running number, the Source column and the Entity column come from
    the dialog itself (static/js/drilldown.js: drawHead); these four are what
    this listing adds."""
    assert [c["key"] for c in spots.columns_json()] == [
        "count", "kinds", "address", "newest"]
    assert [c["label"] for c in spots.columns_json()] == [
        "Locations here", "Location types", "Address", "Newest"]
    # No wide free-text column: a row here is an aggregate of several archive
    # rows, and their reasons concatenated are not a summary of anything.
    assert not any(c["wide"] for c in spots.columns_json())


def test_the_entity_column_is_asked_for_and_the_document_link_is_not():
    """A row is an entity, so "everything else about it" is the next
    question and the Diagrams link answers it. It is not one document: an
    entity in a spot was usually written about by several, and a link
    labelled with one of their domains would be a claim that is not true of
    the row."""
    assert spots.row_links() == {"source": False, "entity": True}
    assert row()["link"] == {"uri": "", "domain": "", "title": "2 documents"}
    assert row(docs=1)["link"]["title"] == "1 document"


def test_an_entity_is_named_so_that_its_link_finds_it_again():
    """`entity` is BOTH the words in the cell and the term the link carries.
    A bare "Apple" resolves to the BUCKET where one exists, so a row about
    the fruit would open a page about four companies."""
    assert row()["entity"] == "Apple Inc. (Company)"
    assert row(x="Apple", type="Fruit")["entity"] == "Apple (Fruit)"
    # A bucket answers under its own name, which is what resolves to it.
    assert row(x="Apple", bucketed=True, type="Company")["entity"] == "Apple"
    # No type recorded: the bare name is all there is, and it is honest.
    assert row(type="")["entity"] == "Apple Inc."


def test_an_address_nothing_is_attached_to_is_still_a_row():
    """Dropping it would make the rows stop adding up to the number the
    field was painted from. The dialog draws its own "not stated" dash for
    an empty name; a word here would become a link to a page about an entity
    called "(not stated)", which nothing in the archive is."""
    assert row(x=None, type=None)["entity"] == ""


def test_a_cell_that_had_to_drop_a_type_says_so():
    """A list of four that stands for seven looks complete and is not."""
    assert [p["text"] for p in row()["cells"]["kinds"]] == ["Headquarters", "Office"]
    assert [p["text"] for p in row(kinds_total=7)["cells"]["kinds"]] == [
        "Headquarters", "Office", "and 5 more"]
    assert [p["text"] for p in row(kinds=[], kinds_total=0)["cells"]["kinds"]] == [""]


def test_the_date_is_a_date_the_page_can_render_as_one():
    """`iso` is what the <time> element carries; the text is what is read."""
    cell = row()["cells"]["newest"][0]
    assert cell["text"] == "2026-05-29"
    assert cell["iso"] == NOW.isoformat()
    assert row(newest=None)["cells"]["newest"] == [{"text": ""}]


def test_the_count_is_the_number_the_rows_are_ranked_by():
    assert row(n=1234)["cells"]["count"] == [{"text": "1,234"}]
    assert row()["cells"]["address"] == [{"text": "Cupertino, California, USA"}]
