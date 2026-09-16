"""The Tables view's statements, read without a database.

    python -m pytest tests/unit/test_listing.py -q

charts/listing.py writes a FROM and a WHERE per tab and hands the page to
the same shapes every listing uses (drilldown.page_rows, shape_row). What
is pinned here is the part a database would not tell you:

  * every tab of the Diagrams has a shape, and a tab without one is refused
    with a hint rather than answered with somebody else's rows;
  * every statement of every tab renders, names only the parameters the
    builder supplies, and puts the page cut INSIDE the per-row lookups;
  * a filter that cannot mean anything is refused, not dropped;
  * a group row and a drilldown row have the same shape, so the page draws
    both with one renderer, and the group's key is what expanding it sends.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app import charts, sqlbuild
from app.charts import drilldown as dd
from app.charts import listing
from app.sqlbuild import check_params, placeholders, render


class Ctx:
    project = "_preseed Alpha"
    language = "English"
    perspective = ""
    min_importance = ""


class Filtered(Ctx):
    perspective = "Compliance"
    min_importance = "High Importance"


PAGED = {"dd_limit": 21, "dd_offset": 0}


def statements(tab: str, filters: listing.Filters, ctx=Ctx()) -> list[tuple[str, dict]]:
    where, params = listing.where(tab, ctx, filters)
    return [(render(listing.rows_statement(tab, where, filters.newest_first)), {**params, **PAGED}),
            (render(listing.count_statement(tab, where)), params),
            (render(listing.groups_statement(tab, where, filters.sort != "fewest")), {**params, **PAGED})]


# ── Every tab ────────────────────────────────────────────────

def test_every_tab_of_the_diagrams_has_a_shape():
    assert set(listing.SHAPES) == set(charts.TAB_IDS)
    assert listing.table_of("market") == "market_insights"
    for tab in charts.TAB_IDS:
        assert listing.table_of(tab) in sqlbuild.TABLES
        # A group row and a drilldown row are drawn by one renderer, so
        # every tab's columns come from the same Column shape.
        assert dd.columns_json(listing.table_of(tab))


def test_the_entity_named_tabs_are_the_two_that_carry_only_an_id():
    # A rating carries its own name; a market insight and a connection are
    # about an entity and carry its id, so their name is looked up.
    assert {tab for tab, shape in listing.SHAPES.items() if shape.entity_named} == {"market", "connections"}


def test_an_unknown_tab_is_refused_with_the_list():
    with pytest.raises(listing.ListingError) as caught:
        listing.shape_of("nope")
    assert "sources" in caught.value.hint


@pytest.mark.parametrize("tab", charts.TAB_IDS)
def test_every_statement_renders_and_binds_only_what_it_is_given(tab):
    for filters in (listing.Filters(), listing.Filters(q="apple", type="company"),
                    listing.Filters(sort="oldest"), listing.Filters(sort="fewest"),
                    listing.Filters(task="_preseed:alpha:1", source="src:battery"),
                    listing.Filters(exact=True, name="Apple", type="Fruit"),
                    listing.Filters(exact=True, entity_id="ent:apple-inc", type="Batteries")):
        for text, params in statements(tab, filters):
            check_params(sqlbuild.sql.SQL(text), params)
            assert "%(project)s" in text and "%(language)s" in text


@pytest.mark.parametrize("tab", charts.TAB_IDS)
def test_the_page_is_cut_before_the_lookups(tab):
    text, _ = statements(tab, listing.Filters())[0]
    # The LIMIT is inside the subselect; the per-row joins hang off it.
    cut = text.index("LIMIT %(dd_limit)s OFFSET %(dd_offset)s")
    assert text.index(") AS \"t\"") > cut
    assert "count(*) OVER ()" not in text, "a Tables page counts with count_statement, once"
    # Newest is newest in the archive: the partition column, inside and out.
    assert "ORDER BY t.date_added DESC, t.bigint_id DESC LIMIT" in text
    assert text.rstrip().endswith('ORDER BY "t"."date_added" DESC, "t".bigint_id DESC')


def test_oldest_turns_both_orders_round():
    text, _ = statements("sources", listing.Filters(sort="oldest"))[0]
    assert text.rstrip().endswith('ORDER BY "t"."date_added" ASC, "t".bigint_id ASC')
    assert "ORDER BY t.date_added ASC, t.bigint_id ASC LIMIT" in text


def test_a_group_statement_carries_its_own_total_and_a_stable_order():
    text, params = statements("entities", listing.Filters(q="apple", sort="most"))[2]
    assert "count(*) OVER () AS dd_total" in text
    assert "GROUP BY 1, 2 ORDER BY n DESC, newest DESC, name ASC" in text
    fewest, _ = statements("entities", listing.Filters(q="apple", sort="fewest"))[2]
    assert "ORDER BY n ASC, newest DESC, name ASC" in fewest
    assert params["q_pat"] == "%apple%"


def test_connections_are_read_from_both_ends_and_keyed_on_the_side():
    text, _ = statements("connections", listing.Filters(q="fox"))[0]
    assert "WHERE TRUE) AS side" in text, "both directions, always"
    assert "side.ref_id IN (SELECT e.text_entity_id" in text
    # And the same list against the indexed columns, so the lateral is not
    # what the archive has to read every row through.
    assert 't."text_fk_parent_entity_id" IN (SELECT e.text_entity_id' in text
    group, _ = statements("connections", listing.Filters(sort="most"))[2]
    assert "COALESCE(side.ref_id, '') AS name, COALESCE(side.role, '') AS type" in group


def test_a_document_is_named_by_task_and_its_own_id_column():
    text, params = statements("sources", listing.Filters(task="t1", source="s1"))[1]
    assert 't.text_task_id = %(task)s AND t."text_source_id" = %(source)s' in text
    text, _ = statements("ratings", listing.Filters(task="t1", source="s1"))[1]
    assert 't."text_fk_source_id" = %(source)s' in text
    assert params == {"project": Ctx.project, "language": Ctx.language, "task": "t1", "source": "s1"}


def test_the_perspective_filter_rides_on_every_statement():
    for text, params in statements("attributes", listing.Filters(), Filtered()):
        assert "source_importances_by_perspective" in text
        assert params["perspective"] == "Compliance" and params["min_importance"] == "High Importance"
    where, params = listing.where("sources", Filtered(), listing.Filters(), perspective=False)
    assert "source_importances_by_perspective" not in render(where)
    assert "perspective" not in params


def test_exact_compares_what_the_group_statement_grouped():
    where, params = listing.where("locations", Ctx(), listing.Filters(exact=True, name="Austin", type=""))
    text = render(where)
    assert "COALESCE(COALESCE(NULLIF(t.text_address, ''), t.text_name), '') = %(name_x)s" in text
    assert "COALESCE(t.\"text_type\", '') = %(type_x)s" in text
    assert params["name_x"] == "Austin" and params["type_x"] == ""
    where, params = listing.where("market", Ctx(), listing.Filters(exact=True, entity_id="ent:x", type="T"))
    assert "COALESCE(t.\"text_fk_entity_id\", '') = %(entity_x)s" in render(where)


# ── Refusals ─────────────────────────────────────────────────

@pytest.mark.parametrize("filters,word", [
    (listing.Filters(sort="sideways"), "sort"),
    (listing.Filters(task="t1"), "together"),
    (listing.Filters(source="s1"), "together"),
    (listing.Filters(exact=True, type="Company"), "exact"),
    (listing.Filters(q="x" * (listing.MAX_KEY_LENGTH + 1)), "long"),
])
def test_a_filter_that_cannot_mean_anything_is_refused(filters, word):
    with pytest.raises(listing.ListingError) as caught:
        filters.check()
    assert word in str(caught.value) and caught.value.hint


# ── Rows ─────────────────────────────────────────────────────

def test_a_drilldown_row_carries_the_document_key():
    row = dd.shape_row("sources", {
        "name": "Inside the new battery", "uri": "https://news.example.com/apple-battery",
        "type": "News article", "importance": "High Importance", "trustful": True, "summary": "",
        "source_name": "Inside the new battery", "source_uri": "https://news.example.com/apple-battery",
        "dd_task": "_preseed:alpha:1", "dd_source_id": "src:battery",
        "row_date": datetime(2026, 5, 29, tzinfo=timezone.utc)})
    assert row["source"] == {"task": "_preseed:alpha:1", "id": "src:battery"}
    assert row["link"]["domain"] == "news.example.com"


def test_a_group_row_has_the_shape_of_a_drilldown_row():
    newest = datetime(2026, 5, 29, 9, 30, tzinfo=timezone.utc)
    row = listing.shape_group("entities", {"name": "Apple", "type": "Fruit", "n": 1, "newest": newest})
    assert row["link"] == {"uri": "", "domain": "", "title": "1 row"}
    assert row["source"] == {"task": "", "id": ""} and row["entity"] == ""
    assert row["key"] == {"name": "Apple", "type": "Fruit"}
    assert set(row["cells"]) == {c["key"] for c in listing.group_columns_json()}
    assert row["cells"]["count"] == [{"text": "1"}]
    assert row["cells"]["newest"][0]["iso"] == newest.isoformat()
    many = listing.shape_group("entities", {"name": "Apple Inc.", "type": "Company", "n": 1234, "newest": None})
    assert many["link"]["title"] == "1,234 rows" and many["cells"]["count"] == [{"text": "1,234"}]


def test_an_entity_named_group_shows_the_name_and_keys_on_the_id():
    row = listing.shape_group("market", {"name": "ent:apple-inc", "type": "Batteries", "n": 8, "newest": None},
                              {"ent:apple-inc": "Apple Inc."})
    assert row["cells"]["name"] == [{"text": "Apple Inc."}]
    assert row["key"] == {"entity_id": "ent:apple-inc", "type": "Batteries"}
    # An id nobody could name is not shown as an id.
    unnamed = listing.shape_group("market", {"name": "ent:x", "type": "", "n": 1, "newest": None}, {})
    assert unnamed["cells"]["name"] == [{"text": listing.UNNAMED}]


def test_the_group_columns_read_like_the_drilldown_columns():
    columns = listing.group_columns_json()
    assert [c["key"] for c in columns] == ["name", "type", "count", "newest"]
    assert set(columns[0]) == set(dd.columns_json("sources")[0])
    assert columns[3]["kind"] == "date"
    assert listing.group_csv_columns() == ["row", "name", "type", "count", "newest"]
    assert listing.csv_group("entities", {"name": "Apple", "type": "Fruit", "n": 1, "newest": None}, 3) == {
        "row": "3", "name": "Apple", "type": "Fruit", "count": "1", "newest": ""}


# ── The other four routes' predicates ────────────────────────

def test_a_pair_is_written_for_the_index_and_for_the_side():
    parts = listing.pair_where(False, False, role=True)
    text = " AND ".join(render(p) for p in parts)
    assert '(t."text_fk_parent_entity_id" = %(a)s AND t."text_fk_child_entity_id" = %(b)s) OR' in text
    assert "side.ref_id = %(a)s AND side.tgt_id = %(b)s" in text
    assert "side.role = %(role)s" in text
    assert placeholders(text) == {"a", "b", "role"}
    bucket = " ".join(render(p) for p in listing.pair_where(True, False))
    assert "side.ref_id IN (SELECT unnest(%(a)s::text[]))" in bucket
    assert "= ANY" not in bucket


def test_a_place_is_two_branches_not_a_case():
    by_address = " AND ".join(render(p) for p in listing.place_where(address=True, entity=True))
    assert by_address == ("lower(t.text_address) = lower(%(address)s) AND "
                          "t.text_fk_entity_id IN (SELECT unnest(%(entity)s::text[]))")
    by_point = render(listing.place_where(address=False, entity=False)[0])
    assert "CASE" not in by_point
    assert "t.float_latitude BETWEEN %(lat)s - 5e-06 AND %(lat)s + 5e-06" in by_point
    assert "t.float_longitude BETWEEN %(lng)s - 5e-06 AND %(lng)s + 5e-06" in by_point


def test_the_entity_names_are_asked_for_with_unnest():
    assert "IN (SELECT unnest(%(ids)s::text[]))" in listing.ENTITY_NAMES_SQL
    assert "= ANY" not in listing.ENTITY_NAMES_SQL
