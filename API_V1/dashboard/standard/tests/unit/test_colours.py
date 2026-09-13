"""Colour groups: the runtime lookup, and the suggestion rules against the
corpus of 1'800 real connection type names copied from the previous
dashboard (conn-type-corpus.json: name -> how often it appeared in ~9'900
edges).

    python -m pytest tests/unit/test_colours.py -q
"""

from __future__ import annotations

import doctest
import json
import re
from pathlib import Path

import pytest

from app import colours
from app.colours import (FALLBACK_KEY, SUGGESTION_KEYS, ColourResolver, contrast_ratio, suggest_for_names,
                         suggest_group, text_colour_for)

HERE = Path(__file__).resolve().parent
CORPUS = json.loads((HERE / "conn-type-corpus.json").read_text(encoding="utf-8"))
SEED_SQL = (HERE.parents[1] / "sql" / "02-dashboard-seed.sql").read_text(encoding="utf-8")

SEED_ROWS = re.findall(r"\('([a-z]+)',\s*'([^']+)',\s*'(#[0-9A-Fa-f]{6})'.*?,\s*(\d+),\s*(true|false)\)", SEED_SQL)


def test_doctests():
    failed, _ = doctest.testmod(colours)
    assert failed == 0


# ── The seed ─────────────────────────────────────────────────

def test_seed_has_the_eleven_groups_and_one_fallback():
    keys = [r[0] for r in SEED_ROWS]
    assert keys == ["competitor", "regulator", "investor", "person", "ownership", "customer",
                    "supplier", "partner", "product", "location", "other"]
    assert [r[0] for r in SEED_ROWS if r[4] == "true"] == [FALLBACK_KEY]
    assert set(keys) == set(SUGGESTION_KEYS)


def test_every_seeded_colour_has_three_to_one_on_white():
    for key, _name, colour, _sort, _fb in SEED_ROWS:
        assert contrast_ratio(colour, "#ffffff") >= colours.MIN_CONTRAST_ON_WHITE, f"{key} {colour}"
    assert len({r[2].lower() for r in SEED_ROWS}) == len(SEED_ROWS), "colours must be distinct"


def test_text_colour_is_readable():
    assert text_colour_for("#B26A00") == "#ffffff"
    assert text_colour_for("#FFB300") == "#212121"
    assert text_colour_for("#455A64") == "#ffffff"


# ── Suggestions ──────────────────────────────────────────────

@pytest.mark.parametrize("name, key", [
    ("Customer", "customer"), ("Client", "customer"),
    ("Supplier", "supplier"), ("Manufacturer", "supplier"),
    ("Partner", "partner"), ("Ally", "partner"),
    ("Competitor", "competitor"), ("Rival", "competitor"),
    ("Regulator", "regulator"), ("Journalist", "regulator"),
    ("Product", "product"),
    ("Subsidiary", "ownership"), ("Parent Company", "ownership"), ("Owner", "ownership"),
    ("Investor", "investor"),
    ("Country", "location"),
    # Specific beats generic, otherwise a hostile relation reads as friendly.
    ("Competitive Partnership", "competitor"),
    ("Investment Partner", "investor"),
    ("Former Supplier", "supplier"),
    ("Zzz Qqq", FALLBACK_KEY), ("", FALLBACK_KEY), (None, FALLBACK_KEY),
])
def test_suggest_group(name, key):
    assert suggest_group(name) == key


def test_corpus_coverage_and_every_rule_used():
    total = covered = 0
    per_key: dict[str, int] = {}
    for name, count in CORPUS.items():
        key = suggest_group(name)
        total += count
        per_key[key] = per_key.get(key, 0) + count
        if key != FALLBACK_KEY:
            covered += count
    assert covered / total >= 0.85, f"only {covered / total:.1%} of real edges covered"
    unused = [k for k, _ in colours.SUGGESTION_RULES if not per_key.get(k)]
    assert not unused, f"rules never hit on the corpus: {unused}"


def test_suggest_for_names_skips_assigned_and_fallback():
    out = suggest_for_names(["Supplier", "Zzz", "Owner", "Supplier"], existing={"Owner"})
    assert out == [("Supplier", "supplier"), ("Supplier", "supplier")]


# ── The resolver ─────────────────────────────────────────────

GROUPS = [
    {"bigint_id": 1, "text_key": "supplier", "text_name": "Supplier", "text_colour": "#30A0A6",
     "text_description": "", "integer_sort": 70, "bool_fallback": False},
    {"bigint_id": 2, "text_key": "other", "text_name": "Other", "text_colour": "#7D4B12",
     "text_description": "", "integer_sort": 110, "bool_fallback": True},
    {"bigint_id": 3, "text_key": "person", "text_name": "Person / Role", "text_colour": "#74116A",
     "text_description": "", "integer_sort": 40, "bool_fallback": False},
]


class Fake:
    def __init__(self):
        self.types = [{"text_type_name": "Supplier", "bigint_fk_group": 1, "text_source": "manual"},
                      {"text_type_name": "CEO", "bigint_fk_group": 3, "text_source": "suggested"}]
        self.loads = 0
        self.now = 1000.0

    def load(self):
        self.loads += 1
        return GROUPS, list(self.types)

    def clock(self):
        return self.now


def test_lookup_fallback_and_case():
    fake = Fake()
    r = ColourResolver(fake.load, ttl=60, clock=fake.clock)
    assert r.resolve("Supplier").colour == "#30A0A6"
    assert r.resolve("Supplier").source == "manual"
    assert r.resolve("supplier").group_key == "supplier"     # case-insensitive courtesy
    assert r.resolve("CEO").source == "suggested"
    c = r.resolve("Mysterious Bond")
    assert c.group_key == "other" and c.source == "fallback" and c.colour == "#7D4B12"
    assert r.resolve(None).group_key == "other"
    assert r.colour_of("  Supplier ") == "#30A0A6"
    assert [g["key"] for g in r.legend()] == ["person", "supplier", "other"]   # sort order
    assert r.assigned_types() == {"Supplier": "supplier", "CEO": "person"}
    assert fake.loads == 1


def test_cache_expires_after_ttl_and_on_invalidate():
    fake = Fake()
    r = ColourResolver(fake.load, ttl=60, clock=fake.clock)
    r.resolve("Supplier")
    fake.types.append({"text_type_name": "Vendor", "bigint_fk_group": 1, "text_source": "manual"})
    assert r.resolve("Vendor").source == "fallback"      # still cached
    fake.now += 61
    assert r.resolve("Vendor").source == "manual"        # reloaded
    assert fake.loads == 2
    fake.types.clear()
    r.invalidate()
    assert r.resolve("Vendor").source == "fallback"
    assert fake.loads == 3


def test_loader_failure_keeps_the_previous_table():
    fake = Fake()
    r = ColourResolver(fake.load, ttl=60, clock=fake.clock)
    r.resolve("Supplier")

    def boom():
        raise RuntimeError("database away")
    r._loader = boom
    r.invalidate()
    assert r.resolve("Supplier").colour == "#30A0A6"


def test_without_a_fallback_row_the_builtin_one_is_used():
    r = ColourResolver(lambda: ([GROUPS[0]], []), ttl=60)
    assert r.resolve("x").group.key == FALLBACK_KEY
    assert r.fallback.colour == "#7D4B12"
