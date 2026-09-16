"""The Tables view's API against the preseed.

    DATABASE_URL=postgresql://... python -m pytest tests/flow/test_tables_api.py -q

Every number here is read off tests/preseed/preseed.py: six documents in
Alpha's English, one in Beta's; Apple Inc. named three times in each of
the two tasks that carry it; 42 ratings under the battery document and 50
in all; a connection stored once and listed from both of its ends.

What is pinned:

  * a row of the Tables view IS a drilldown row - the same link, the same
    `source` key, the same cells - so one renderer draws both;
  * the four sorts: newest and oldest list rows, most and fewest list
    groups, and a group expands to exactly the rows it counted;
  * the total is counted once, on the first page, and the later pages say
    null rather than counting again;
  * a document opens with every row read out of it, one first page per
    kind; a bucket opens with the documents that hold a member; a pair
    reads from `a`'s side; a place is found by its address or its point;
  * the reader's perspective filter narrows every table.
"""

from __future__ import annotations

import pytest

from app.charts.drilldown import PAGE_SIZE
from preseed import ALPHA, BETA
# The bucket factory test_bucket_kinds.py cleans up after: a bucket made
# here is gone before the next file asserts about the preseeded one.
from test_bucket_kinds import kinded  # noqa: F401

pytestmark = pytest.mark.flow

EN = {"project": ALPHA, "language": "English"}
BETA_EN = {"project": BETA, "language": "English"}

BATTERY = {"task": "_preseed:alpha:1", "id": "src:battery"}


def get(client, path: str, params: dict | None = None, expect: int = 200):
    r = client.get(f"/api/tables/{path}", params={**EN, **(params or {})})
    assert r.status_code == expect, f"{path}: {r.status_code} {r.text[:400]}"
    return r.json()


def cell(row: dict, key: str) -> str:
    return " ".join(p["text"] for p in row["cells"][key])


def groups(body: dict) -> list[tuple[str, str, int]]:
    return [(cell(r, "name"), cell(r, "type"), int(cell(r, "count").replace(",", "")))
            for r in body["rows"]]


# ── The rows of one table ────────────────────────────────────

def test_sources_newest_first_are_drilldown_rows(client):
    body = get(client, "sources")
    assert body["tab"] == "sources" and body["sort"] == "newest"
    assert body["total"] == 6 and len(body["rows"]) == 6
    assert body["more"] is False and body["offset"] == 0
    assert body["page_size"] == PAGE_SIZE
    assert body["row_links"] == {"source": True, "entity": False}
    assert [c["key"] for c in body["columns"]] == ["importance", "trust", "type", "date", "text"]
    first = body["rows"][0]
    assert first["link"]["domain"] == "news.example.com"
    assert first["link"]["title"] == "Inside the new battery"
    # THE DOCUMENT'S KEY TRAVELS WITH THE ROW, which is what opens it.
    assert first["source"] == BATTERY


def test_oldest_first_is_the_other_end(client):
    body = get(client, "sources", {"sort": "oldest"})
    assert body["rows"][0]["link"]["domain"] == "blog.example.net"
    assert body["total"] == 6


def test_name_and_type_narrow_the_table(client):
    # Two documents are named by an identifier and stand under their host.
    assert get(client, "sources", {"q": "filings"})["total"] == 2
    assert get(client, "sources", {"type": "blog"})["total"] == 2
    assert get(client, "sources", {"q": "filings", "type": "blog"})["total"] == 0


def test_entities_by_name(client):
    body = get(client, "entities", {"q": "apple"})
    assert body["total"] == 9 and len(body["rows"]) == 9
    assert all(r["entity"] for r in body["rows"])


def test_most_lists_groups_with_a_key_to_expand(client):
    body = get(client, "entities", {"q": "apple", "sort": "most"})
    assert groups(body) == [("Apple Inc.", "Company", 6), ("Apple", "Company", 2), ("Apple", "Fruit", 1)]
    assert body["total"] == 3
    assert [c["key"] for c in body["columns"]] == ["name", "type", "count", "newest"]
    assert [c["label"] for c in body["columns"]] == ["Name", "Type", "Rows", "Newest"]
    assert body["row_links"] == {"source": True, "entity": False}
    for row in body["rows"]:
        n = int(cell(row, "count"))
        assert row["link"] == {"uri": "", "domain": "", "title": f"{n} row" + ("" if n == 1 else "s")}
        assert row["source"] == {"task": "", "id": ""}
        assert row["key"] == {"name": cell(row, "name"), "type": cell(row, "type")}
        assert row["cells"]["newest"][0]["iso"]


def test_fewest_is_most_reversed(client):
    body = get(client, "entities", {"q": "apple", "sort": "fewest"})
    assert groups(body) == [("Apple", "Fruit", 1), ("Apple", "Company", 2), ("Apple Inc.", "Company", 6)]


def test_a_group_expands_to_the_rows_it_counted(client):
    fruit = get(client, "entities", {"exact": "1", "name": "Apple", "type": "Fruit"})
    assert fruit["total"] == 1 and len(fruit["rows"]) == 1
    assert fruit["exact"] is True
    company = get(client, "entities", {"exact": "1", "name": "Apple", "type": "Company"})
    assert company["total"] == 2 and len(company["rows"]) == 2


# ── Buckets ──────────────────────────────────────────────────

def test_the_buckets_the_search_touches_ride_on_the_first_page(client):
    body = get(client, "entities", {"q": "apple", "buckets": "1"})
    assert len(body["buckets"]) == 1
    bucket = body["buckets"][0]
    assert bucket["kind"] == "entity" and bucket["kind_label"] == "Entity"
    assert bucket["name"] == "Apple" and bucket["member_count"] == 2
    assert bucket["members"] == [{"name": "Apple", "type": "Company"},
                                 {"name": "Apple Inc.", "type": "Company"}]
    # Only the first page carries them; nothing is looked up twice.
    assert "buckets" not in get(client, "entities", {"q": "apple", "buckets": "1", "ddpage": 2})
    # Beta has no bucket of its own.
    r = client.get("/api/tables/entities", params={**BETA_EN, "q": "apple", "buckets": "1"})
    assert r.status_code == 200 and r.json()["buckets"] == []


# ── Paging ───────────────────────────────────────────────────

def test_paging_counts_once_and_says_where_it_is(client):
    first = get(client, "ratings")
    assert first["total"] == 50
    assert len(first["rows"]) == PAGE_SIZE and first["more"] is True and first["offset"] == 0
    second = get(client, "ratings", {"ddpage": 2})
    assert second["total"] is None, "the count is paid once, on the first page"
    assert second["offset"] == PAGE_SIZE and second["more"] is True
    third = get(client, "ratings", {"ddpage": 3})
    assert len(third["rows"]) == 10 and third["more"] is False and third["offset"] == 40
    assert first["rows"] != second["rows"]


# ── Entity-named tabs ────────────────────────────────────────

def test_connections_are_read_from_both_ends(client):
    body = get(client, "connections", {"q": "foxconn"})
    assert body["total"] == 2 and len(body["rows"]) == 2
    for row in body["rows"]:
        assert row["entity"] == "Foxconn"
        assert cell(row, "reference") == "Foxconn"
        assert cell(row, "connection") == "Supplier"
        assert cell(row, "target") == "Apple Inc."


def test_connection_groups_are_keyed_by_entity_id(client):
    body = get(client, "connections", {"q": "foxconn", "sort": "most"})
    assert groups(body) == [("Foxconn", "Supplier", 2)]
    assert body["rows"][0]["key"] == {"entity_id": "ent:foxconn", "type": "Supplier"}
    expanded = get(client, "connections", {"exact": "1", "entity_id": "ent:foxconn", "type": "Supplier"})
    assert expanded["total"] == 2


def test_market_groups_resolve_the_entity_name(client):
    body = get(client, "market", {"q": "apple", "sort": "most"})
    assert groups(body) == [("Apple Inc.", "Batteries", 8),
                            ("Apple", "Antitrust", 1),
                            ("Apple", "Consumer electronics", 1)]
    assert body["rows"][0]["key"] == {"entity_id": "ent:apple-inc", "type": "Batteries"}


# ── One document ─────────────────────────────────────────────

def test_a_document_opens_with_every_row_read_out_of_it(client):
    body = get(client, "source", BATTERY)
    assert body["source"]["link"]["title"] == "Inside the new battery"
    assert body["source"]["source"] == BATTERY
    kinds = {k["tab"]: k for k in body["kinds"]}
    assert list(kinds) == ["entities", "connections", "locations", "events", "ratings",
                           "attributes", "market"]
    assert {tab: k["total"] for tab, k in kinds.items()} == {
        "entities": 5, "connections": 6, "locations": 3, "events": 2, "ratings": 42,
        "attributes": 4, "market": 9}
    assert len(kinds["ratings"]["rows"]) == PAGE_SIZE and kinds["ratings"]["more"] is True
    assert kinds["market"]["title"] == "Market Insights"
    assert all(k["columns"] and "row_links" in k for k in kinds.values())
    assert all(r["source"] == BATTERY for k in kinds.values() for r in k["rows"])


def test_an_unknown_document_is_404(client):
    get(client, "source", {"task": "_preseed:alpha:1", "id": "src:nope"}, expect=404)


# ── The documents of a bucket ────────────────────────────────

def test_an_entity_bucket_lists_the_documents_that_hold_a_member(client):
    body = get(client, "bucket/sources", {"kind": "entity", "name": "Apple"})
    assert body["bucket"]["name"] == "Apple" and body["bucket"]["kind"] == "entity"
    assert [r["source"]["id"] for r in body["rows"]] == ["src:battery", "src:antitrust", "src:subsidiary"]
    assert body["total"] == 3


def test_other_kinds_of_bucket_list_their_documents(client, kinded):
    kinded("_c Filings", "source_type", ["Regulatory filing"])
    assert get(client, "bucket/sources", {"kind": "source_type", "name": "_c Filings"})["total"] == 2
    # A place is matched as a substring, and Nordwyk's factory travels
    # with the two documents that name Nordwyk.
    kinded("_c Port", "location", ["Rotterdam"])
    assert get(client, "bucket/sources", {"kind": "location", "name": "_c Port"})["total"] == 2
    # A colour group stands where a bucket would: the Supplier group holds
    # Supplier and Lieferant, at either end of a connection.
    body = get(client, "bucket/sources", {"kind": "connection_type", "name": "Supplier"})
    assert body["total"] == 3
    assert body["bucket"]["source"] == "colour group"


def test_a_bucket_nobody_made_is_404_and_a_kind_that_is_not_one_is_400(client):
    get(client, "bucket/sources", {"kind": "source_type", "name": "Nope"}, expect=404)
    get(client, "bucket/sources", {"kind": "colour", "name": "Supplier"}, expect=400)


# ── A pair, and a place ──────────────────────────────────────

def test_a_pair_reads_from_the_first_end(client):
    body = get(client, "pair", {"a": "ent:foxconn", "b": "ent:apple-inc"})
    assert body["total"] == 2 and len(body["rows"]) == 2
    for row in body["rows"]:
        assert cell(row, "reference") == "Foxconn"
        assert cell(row, "connection") == "Supplier"
        assert cell(row, "target") == "Apple Inc."
    assert get(client, "pair", {"a": "ent:foxconn", "b": "ent:apple-inc", "role": "Customer"})["total"] == 0
    other = get(client, "pair", {"a": "ent:apple-inc", "b": "ent:foxconn"})
    assert other["total"] == 2
    assert {cell(r, "connection") for r in other["rows"]} == {"Customer"}


def test_a_bucket_end_stands_for_its_members(client):
    body = get(client, "pair", {"a": "bucket:Apple", "b": "ent:foxconn"})
    assert body["total"] == 2
    assert {cell(r, "reference") for r in body["rows"]} == {"Apple Inc."}
    get(client, "pair", {"a": "bucket:Nope", "b": "ent:foxconn"}, expect=404)


def test_a_place_by_address_or_by_point(client):
    assert get(client, "place", {"entity": "ent:apple-inc",
                                 "address": "Cupertino, California, USA"})["total"] == 2
    assert get(client, "place", {"entity": "ent:apple-inc", "address": "Austin, Texas, USA"})["total"] == 1
    body = get(client, "place", {"lat": 30.2672, "lng": -97.7431})
    assert body["total"] == 1
    assert cell(body["rows"][0], "address") == "Austin, Texas, USA"
    get(client, "place", {"entity": "ent:apple-inc"}, expect=400)


# ── Scope ────────────────────────────────────────────────────

def test_the_perspective_filter_narrows_the_table(client):
    body = get(client, "sources", {"perspective": "Compliance", "min_importance": "High Importance"})
    assert body["total"] == 1
    assert body["rows"][0]["source"]["id"] == "src:antitrust"


def test_the_other_project_sees_only_its_own_rows(client):
    r = client.get("/api/tables/sources", params=BETA_EN)
    assert r.status_code == 200 and r.json()["total"] == 1


def test_a_wrong_ask_is_refused_with_a_hint(client):
    for path, params in (("nope", {}), ("sources", {"sort": "sideways"}),
                         ("sources", {"task": "_preseed:alpha:1"}),
                         ("sources", {"exact": "1", "type": "Blog post"})):
        r = client.get(f"/api/tables/{path}", params={**EN, **params})
        assert r.status_code == 400, (path, params, r.text)
        assert set(r.json()) == {"error", "hint"} and r.json()["hint"], r.text


def test_the_export_holds_the_same_rows(client):
    r = client.get("/api/export/tables.json", params={**EN, "tab": "entities", "q": "apple", "sort": "most"})
    assert r.status_code == 200, r.text
    body = r.json()
    rows = body["rows"]
    assert [(x["name"], x["type"], x["count"]) for x in rows] == [
        ("Apple Inc.", "Company", "6"), ("Apple", "Company", "2"), ("Apple", "Fruit", "1")]
    assert rows[0]["row"] == "1"
