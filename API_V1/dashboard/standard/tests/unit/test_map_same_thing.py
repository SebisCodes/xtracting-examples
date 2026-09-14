"""One thing under several archive ids is one pin and one line.

The extraction hands out a new entity id whenever it does not recognise a
thing it has met before, so a real archive holds "Epic Games - Company"
under a dozen ids - and "Apple" under ten TYPES. The Map folds every id that
carries the same name into one entity before it draws (routers/api_map.py:
_same_thing, then _regroup) - one pin at the main location, one line per
pair, with the connections of every id added up. This is arithmetic on
dictionaries and is tested here without a database.

    python -m pytest tests/unit/test_map_same_thing.py -q
"""

from __future__ import annotations

from app.routers.api_map import _regroup, _same_thing


def info(name: str, typ: str, rows: int = 1) -> dict:
    return {"name": name, "type": typ, "rows": rows}


def edge(parent: str, child: str, n: int = 1, ptc: str = "Client", ctp: str = "Supplier"):
    return {"parent": parent, "child": child, "n": n,
            "type_ptc": ptc, "type_ctp": ctp, "first_seen": None, "last_seen": None}


def place(address: str, count: int, typ: str = "Office"):
    return {"address": address, "type": typ, "lat": 1.0, "lng": 2.0, "count": count}


# ── The key ───────────────────────────────────────────────────

def test_same_name_and_type_is_one_thing_drawn_as_the_busiest_id():
    names = {"e1": info("Epic Games", "Company", 12),
             "e2": info("Epic Games", "Company", 218),
             "e3": info("Epic Games", "Company", 2)}
    same = _same_thing(["e1", "e2", "e3"], names)
    assert same == {"e2": ["e2"], "e1": ["e2"], "e3": ["e2"]}
    # The representative comes first, so _regroup names the group after it.
    assert list(same)[0] == "e2"


def test_case_is_folded_and_nothing_else_is():
    names = {"a": info("apple", "Company", 5), "b": info("Apple", "Company", 9),
             "c": info("Apple Inc.", "Company", 100)}
    same = _same_thing(["a", "b", "c"], names)
    assert same["a"] == ["b"] and same["b"] == ["b"]
    # "Apple Inc." is a different name. Reading the suffix off it would be a
    # guess, and a guess drawn as one pin is a lie the reader cannot see.
    assert same["c"] == ["c"]


def test_the_type_is_not_part_of_the_key():
    """The type is the extraction's guess, and it guesses "Apple" ten ways.
    A key on it keeps ten Apples on the map; the search box itself resolves
    a name whatever its type (app/scope.py), and the map follows it. The
    entity is shown under the type of its busiest id."""
    names = {"co": info("Apple", "Company", 300), "org": info("Apple", "Organization", 3),
             "fruit": info("Apple", "Fruit", 30)}
    same = _same_thing(["co", "org", "fruit"], names)
    assert same == {"co": ["co"], "org": ["co"], "fruit": ["co"]}


def test_a_tie_goes_to_the_smaller_id():
    names = {"z": info("Acme", "Company", 4), "a": info("Acme", "Company", 4)}
    assert _same_thing(["z", "a"], names)["z"] == ["a"]


def test_an_id_without_a_name_stays_itself():
    same = _same_thing(["known", "ghost"], {"known": info("Acme", "Company")})
    assert same == {"known": ["known"], "ghost": ["ghost"]}


# ── What the fold does to the picture ─────────────────────────

def test_the_fold_gives_one_pin_one_line_and_the_sum_of_the_connections():
    names = {"apple": info("Apple", "Company", 50),
             "epic1": info("Epic Games", "Company", 20),
             "epic2": info("Epic Games", "Company", 5)}
    same = _same_thing(["apple", "epic1", "epic2"], names)
    levels, edges, new_names, places = _regroup(
        same, {"apple": 0, "epic1": 1, "epic2": 2},
        [edge("apple", "epic1", 3), edge("apple", "epic2", 4), edge("epic1", "epic2", 9)],
        names,
        {"epic1": [place("Cary, North Carolina, USA", 2)],
         "epic2": [place("Cary, North Carolina, USA", 5), place("Berlin, Germany", 1)]})
    assert set(levels) == {"apple", "epic1"}
    # As close to the search as its nearest id.
    assert levels["epic1"] == 1
    assert new_names["epic1"]["name"] == "Epic Games"
    # Both ids' connections to Apple are one pair now; the one between the
    # two Epic ids was Epic Games connected to itself and is gone.
    assert [(e["parent"], e["child"], e["n"]) for e in edges] == [
        ("apple", "epic1", 3), ("apple", "epic1", 4)]
    # The main location is the address carried most often ACROSS the ids.
    assert [(p["address"], p["count"]) for p in places["epic1"]] == [
        ("Cary, North Carolina, USA", 7), ("Berlin, Germany", 1)]
