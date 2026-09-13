"""Drawing a network by bucket instead of by entity.

The archive stores connections between ENTITIES. A bucket is the dashboard's
own idea of which entities a reader means as one thing, so the Map fetches the
network once, by entity, and regroups it - `routers/api_map.py: _regroup`. That
regrouping is arithmetic on dictionaries and is tested here without a database;
tests/flow/test_map_api.py drives the same switch against a real archive.

The claim worth the file is the one about DUPLICATES. An entity can honestly sit
in two buckets - Angela Merkel in "Germany" and in "EU leaders" - and a
connection to her is a connection to each of them. Folding her to the first
bucket would drop the second with nothing on screen saying which was left out.

    python -m pytest tests/unit/test_map_bucket_grouping.py -q
"""

from __future__ import annotations

import pytest

from app.routers.api_map import _regroup
from app.scope import holders_for


def edge(parent: str, child: str, n: int = 1, ptc: str = "Supplier", ctp: str = "Customer"):
    return {"parent": parent, "child": child, "n": n,
            "type_ptc": ptc, "type_ctp": ctp,
            "first_seen": None, "last_seen": None}


def place(address: str, lat: float, lng: float, count: int, typ: str = "Office"):
    return {"address": address, "type": typ, "lat": lat, "lng": lng, "count": count}


# ── An entity in one bucket ───────────────────────────────────

def test_a_bucket_takes_the_place_of_its_members():
    groups = {"e1": ["bucket:Germany"], "e2": ["bucket:Germany"], "e3": ["e3"]}
    levels, edges, names, places = _regroup(
        groups,
        {"e1": 0, "e2": 1, "e3": 1},
        [edge("e1", "e3"), edge("e2", "e3")],
        {"e1": {"name": "Merz", "type": "Person"},
         "e2": {"name": "Bundestag", "type": "Organization"},
         "e3": {"name": "Acme", "type": "Company"}},
        {},
    )

    assert set(levels) == {"bucket:Germany", "e3"}
    assert names["bucket:Germany"] == {"name": "Germany", "type": "bucket"}
    # An entity in no bucket keeps its own name: a map that showed only the
    # buckets somebody happened to have made would hide most of an archive.
    assert names["e3"]["name"] == "Acme"
    assert [(e["parent"], e["child"]) for e in edges] == [
        ("bucket:Germany", "e3"), ("bucket:Germany", "e3")]


def test_a_bucket_is_as_close_to_the_search_as_its_nearest_member():
    """Level decides which ring a node is drawn in. A bucket whose nearest
    member is the search itself belongs at the centre, however far its most
    obscure member is."""
    levels, _, _, _ = _regroup(
        {"near": ["bucket:Germany"], "far": ["bucket:Germany"]},
        {"near": 0, "far": 3},
        [], {"near": {"name": "a"}, "far": {"name": "b"}}, {},
    )
    assert levels["bucket:Germany"] == 0


def test_a_connection_inside_one_bucket_is_not_a_line():
    """Two members of "Germany" connected to each other is Germany connected to
    itself, which is not a connection - it is a loop the map cannot draw."""
    _, edges, _, _ = _regroup(
        {"e1": ["bucket:Germany"], "e2": ["bucket:Germany"]},
        {"e1": 0, "e2": 0},
        [edge("e1", "e2")],
        {"e1": {"name": "a"}, "e2": {"name": "b"}}, {},
    )
    assert edges == []


# ── An entity in several buckets ──────────────────────────────

def test_an_entity_in_two_buckets_is_drawn_in_both():
    """The claim this whole mode turns on. One connection to Merkel becomes a
    line to Germany AND a line to EU leaders, because it truly is both."""
    _, edges, _, _ = _regroup(
        {"merkel": ["bucket:Germany", "bucket:EU leaders"], "x": ["x"]},
        {"merkel": 1, "x": 0},
        [edge("x", "merkel", n=4)],
        {"merkel": {"name": "Merkel"}, "x": {"name": "X"}}, {},
    )
    assert sorted((e["parent"], e["child"]) for e in edges) == [
        ("x", "bucket:EU leaders"), ("x", "bucket:Germany")]
    # And each line carries the whole weight of the connection it stands for:
    # the fact is not divided between the buckets, it is true of both.
    assert [e["n"] for e in edges] == [4, 4]


def test_both_ends_in_several_buckets_is_every_pairing():
    _, edges, _, _ = _regroup(
        {"a": ["bucket:P", "bucket:Q"], "b": ["bucket:R", "bucket:S"]},
        {"a": 0, "b": 1},
        [edge("a", "b")],
        {"a": {"name": "A"}, "b": {"name": "B"}}, {},
    )
    assert sorted((e["parent"], e["child"]) for e in edges) == [
        ("bucket:P", "bucket:R"), ("bucket:P", "bucket:S"),
        ("bucket:Q", "bucket:R"), ("bucket:Q", "bucket:S")]


def test_two_buckets_that_share_a_member_still_connect_to_each_other():
    """Overlapping buckets are not merged. "Germany" and "EU leaders" share
    Merkel and are still two nodes, and a connection from one of Germany's other
    members to her is a line between them."""
    _, edges, _, _ = _regroup(
        {"bundestag": ["bucket:Germany"],
         "merkel": ["bucket:Germany", "bucket:EU leaders"]},
        {"bundestag": 0, "merkel": 1},
        [edge("bundestag", "merkel")],
        {"bundestag": {"name": "Bundestag"}, "merkel": {"name": "Merkel"}}, {},
    )
    # Germany→Germany is dropped as a loop; Germany→EU leaders is a real line.
    assert [(e["parent"], e["child"]) for e in edges] == [
        ("bucket:Germany", "bucket:EU leaders")]


# ── Where a bucket sits on the map ────────────────────────────

def test_a_bucket_carries_its_members_addresses_by_how_often_they_are_used():
    """One entity is drawn at the address it carries most often; a bucket is
    drawn at the address ITS MEMBERS carry most often, which is the same rule
    applied to more rows."""
    _, _, _, places = _regroup(
        {"e1": ["bucket:B"], "e2": ["bucket:B"]},
        {"e1": 0, "e2": 0}, [],
        {"e1": {"name": "a"}, "e2": {"name": "b"}},
        {"e1": [place("Bern", 46.9, 7.4, 2), place("Zug", 47.2, 8.5, 9)],
         "e2": [place("Bern", 46.9, 7.4, 8)]},
    )
    addresses = [p["address"] for p in places["bucket:B"]]
    # Bern is 2 + 8 across the two members and beats Zug's 9, which is the point
    # of merging rather than taking the first member's list.
    assert addresses == ["Bern", "Zug"]
    assert places["bucket:B"][0]["count"] == 10


def test_the_same_address_under_two_members_is_one_place():
    _, _, _, places = _regroup(
        {"e1": ["bucket:B"], "e2": ["bucket:B"]},
        {"e1": 0, "e2": 0}, [], {"e1": {"name": "a"}, "e2": {"name": "b"}},
        {"e1": [place("Bern", 46.9, 7.4, 1)], "e2": [place("Bern", 46.9, 7.4, 1)]},
    )
    assert len(places["bucket:B"]) == 1


# ── The lookup that decides which buckets a name is in ────────

def test_every_bucket_a_name_is_in_is_returned():
    holders = {"merkel": [("person", "Germany"), (None, "EU leaders")]}
    assert holders_for(holders, "Merkel", "Person") == ["Germany", "EU leaders"]


def test_a_member_with_a_type_only_matches_that_type():
    """The whole difference between the two things called Apple. A bucket that
    holds Apple (Company) must not swallow Apple (Fruit)."""
    holders = {"apple": [("company", "Tech")]}
    assert holders_for(holders, "Apple", "Company") == ["Tech"]
    assert holders_for(holders, "Apple", "Fruit") == []


def test_one_bucket_is_named_once_however_many_ways_it_holds_a_name():
    # Both entries match - one by type, one because a member without a type
    # means "this name, whatever its type" - and the bucket is still named once.
    holders = {"trump": [("person", "USA"), (None, "USA")]}
    assert holders_for(holders, "Trump", "Person") == ["USA"]
