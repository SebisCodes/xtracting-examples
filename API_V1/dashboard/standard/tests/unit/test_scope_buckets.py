"""Scope resolution and bucket expansion, against a fake connection.

The SQL is rendered and the rows are canned, so the ladder, the bucket
lookup and the shape of the answer are tested without an archive; the flow
test tests/flow/test_scope_resolution.py runs the same code against a real one.

    python -m pytest tests/unit/test_scope_buckets.py -q
"""

from __future__ import annotations

import doctest
from typing import Any, Callable

import pytest
from psycopg import sql

from app import scope, sqlbuild
from app.scope import (AXES, BUCKET_KINDS, COLOUR_GROUP_KIND, GROUPING_KINDS, KINDS,
                       SCOPE_AXIS_KIND, ScopeError, bucket_terms, find_bucket, groupings,
                       ladder_patterns, member_index, normalise_terms, resolve_scope,
                       resolve_terms)
from app.sqlbuild import placeholders, render


class Ctx:
    project = "_preseed Alpha"
    language = "German"


class Cursor:
    def __init__(self, rows: list[dict[str, Any]]):
        self.rows = rows

    def fetchall(self):
        return list(self.rows)

    def fetchone(self):
        return self.rows[0] if self.rows else None


class FakeConn:
    """Answers execute() from a handler(text, params) -> rows and records
    every call, so a test can see which rungs were tried."""

    def __init__(self, handler: Callable[[str, dict[str, Any] | None], list[dict[str, Any]]]):
        self.handler = handler
        self.calls: list[tuple[str, dict[str, Any] | None]] = []

    def execute(self, query, params=None):
        text = query if isinstance(query, str) else render(query)
        # A statement must never name a parameter nobody supplied.
        if params is not None:
            missing = placeholders(text) - set(params)
            assert not missing, f"unbound parameters {missing} in: {text}"
        self.calls.append((text, params))
        return Cursor(self.handler(text, params or {}))


def _no_bucket(text, params):
    return []


# ── Pure pieces ──────────────────────────────────────────────

def test_doctests():
    failed, _ = doctest.testmod(scope)
    assert failed == 0


def test_normalise_terms_lowercases_and_drops_duplicates():
    assert normalise_terms([("Apple", "Company"), (" apple ", "COMPANY"), ("Apple", ""), ("", "x")]) == [
        ("apple", "company"), ("apple", None)]


def test_bucket_terms_values_list_and_null_type():
    frag, params = bucket_terms([("Apple Inc.", "Company"), ("Apple", None)])
    text = render(frag)
    assert text == ('(VALUES (%(bt_name_0)s::text, %(bt_type_0)s::text), '
                    '(%(bt_name_1)s::text, %(bt_type_1)s::text)) AS "bt"(name, type)')
    assert params["bt_type_1"] is None and params["bt_name_0"] == "apple inc."
    with pytest.raises(ScopeError):
        bucket_terms([("", None)])


def test_bucket_lookup_applies_project_null_buckets_and_ignores_case():
    assert "b.text_project IS NULL OR b.text_project = %(project)s" in scope.BUCKET_LOOKUP_SQL
    assert "lower(b.text_name) = %(term)s" in scope.BUCKET_LOOKUP_SQL
    assert "lower(m.text_name) = %(term)s" in scope.BUCKET_LOOKUP_SQL
    # A project's own bucket wins over a global one of the same name.
    assert "(b.text_project IS NOT NULL) DESC" in scope.BUCKET_LOOKUP_SQL


def test_ladder_order():
    assert [m for m, _ in ladder_patterns("Apple")] == ["exact", "prefix", "substring"]
    assert ladder_patterns(" Apple ")[0][1] == "apple"


def test_unknown_kind_is_refused():
    with pytest.raises(ScopeError, match="unknown scope"):
        resolve_scope(FakeConn(_no_bucket), Ctx(), "galaxy", "x")


# ── Buckets ──────────────────────────────────────────────────

def _bucket_handler(text, params):
    if "FROM dashboard.buckets b" in text:
        assert params["term"] == "apple" and params["project"] == "_preseed Alpha"
        return [{"bigint_id": 7, "text_name": "Apple", "text_project": "_preseed Alpha"}]
    if "FROM dashboard.bucket_members" in text:
        return [{"text_name": "Apple", "text_type": "Company"}, {"text_name": "Apple Inc.", "text_type": "Company"}]
    if 'SELECT count(*) AS n FROM "ent"' in text:
        return [{"n": 2}]
    if 'SELECT count(*) AS n FROM "src"' in text:
        return [{"n": 3}]
    raise AssertionError("unexpected statement: " + text)


def test_a_term_that_is_a_bucket_expands_to_its_members():
    conn = FakeConn(_bucket_handler)
    sc = resolve_scope(conn, Ctx(), "entity", "  APPLE ")
    assert sc.scoped and sc.kind == "entity"
    assert sc.resolved["match"] == "bucket"
    assert sc.resolved["bucket"]["id"] == 7
    assert sc.resolved["names"] == ["Apple", "Apple Inc."]
    assert sc.resolved["entities"] == 2 and sc.resolved["sources"] == 3
    assert sc.params["bt_name_0"] == "apple" and sc.params["bt_type_0"] == "company"
    assert sc.params["bt_name_1"] == "apple inc."
    assert sc.params["project"] == "_preseed Alpha" and sc.params["language"] == "German"
    assert 'AS "bt"(name, type)' in render(sc.ent_cte)
    # No ladder probe ran: the bucket decided.
    assert not any("GROUP BY e.text_name" in t for t, _ in conn.calls)


def test_find_bucket_with_a_type_and_without_members():
    def handler(text, params):
        if "FROM dashboard.buckets b" in text:
            assert params["type"] == "company"
            return [{"bigint_id": 1, "text_name": "Apple", "text_project": None}]
        return []
    assert find_bucket(FakeConn(handler), "p", "Apple Inc.", "Company") is None
    assert find_bucket(FakeConn(handler), "p", "   ") is None


# ── The ladder ───────────────────────────────────────────────

def _ladder_handler(hits_at: str):
    def handler(text, params):
        if "FROM dashboard.buckets b" in text:
            return []
        if "GROUP BY e.text_name" in text:
            pat = params["sc_pat"]
            rung = "exact" if "%" not in pat else ("prefix" if pat.endswith("%") and not pat.startswith("%") else "substring")
            if rung == hits_at:
                return [{"name": "Microsoft Corporation", "n": 4}, {"name": "Microsoft Ireland", "n": 1}]
            return []
        if 'SELECT count(*) AS n FROM "ent"' in text:
            return [{"n": 5}]
        if 'SELECT count(*) AS n FROM "src"' in text:
            return [{"n": 4}]
        raise AssertionError("unexpected statement: " + text)
    return handler


def test_prefix_wins_when_exact_finds_nothing():
    conn = FakeConn(_ladder_handler("prefix"))
    sc = resolve_scope(conn, Ctx(), "entity", "Micro")
    assert sc.resolved["match"] == "prefix"
    assert sc.resolved["names"] == ["Microsoft Corporation", "Microsoft Ireland"]
    assert sc.resolved["label"] == "Micro"          # several names: the term stays the label
    assert sc.params["sc_pat"] == "micro%"
    probes = [p["sc_pat"] for t, p in conn.calls if "GROUP BY e.text_name" in t]
    assert probes == ["micro", "micro%"]


def test_substring_is_the_last_rung_and_nothing_yields_an_empty_scope():
    conn = FakeConn(_ladder_handler("substring"))
    assert resolve_scope(conn, Ctx(), "entity", "soft").resolved["match"] == "substring"

    conn = FakeConn(_ladder_handler("never"))
    sc = resolve_scope(conn, Ctx(), "entity", "atlantis")
    assert sc.resolved["match"] == "none" and sc.resolved["entities"] == 0
    assert sc.scoped
    assert sc.params["sc_pat"] == "atlantis"
    probes = [p["sc_pat"] for t, p in conn.calls if "GROUP BY e.text_name" in t]
    assert probes == ["atlantis", "atlantis%", "%atlantis%"]


def test_single_name_becomes_the_label():
    def handler(text, params):
        if "FROM processed_data.sources s" in text and "AS n" in text:
            return [{"name": "news.example.com", "n": 2}] if params["sc_pat"].endswith("%") else []
        if "count(*) AS n FROM" in text:
            return [{"n": 1}]
        return []
    sc = resolve_scope(FakeConn(handler), Ctx(), "source", "news.example")
    assert sc.resolved["label"] == "news.example.com"
    assert sc.resolved["match"] == "prefix"


def test_the_source_probe_groups_by_something_a_person_can_read():
    """It matched on the host and answered with `text_name` - which on the
    real archive is an identifier in every row, so a search for "vogue" was
    answered with "matched 3cf1dadd3725f8d20c2158445c3bb4ca", one group per
    document, every count 1 (app/sqlbuild.py: MACHINE_NAME_REGEX)."""
    text = render(scope._probe("source"))
    # As psycopg writes the literal: the pattern carries a backslash, so it
    # comes out as an E-string with that backslash escaped.
    assert sql.Literal(sqlbuild.MACHINE_NAME_REGEX).as_string(None) in text
    assert "GROUP BY s.text_name" not in text
    # Still MATCHES on all three, which is why the vogue document was found
    # even while the answer was unreadable.
    assert text.count("text_uri") >= 2 and "text_name) LIKE" in text


def test_empty_term_on_a_scoped_kind_is_nothing_not_everything():
    conn = FakeConn(_no_bucket)
    sc = resolve_scope(conn, Ctx(), "market", "")
    assert sc.scoped and sc.resolved["match"] == "none"
    assert "WHERE false" in render(sc.ent_cte)
    assert conn.calls == []


def test_an_empty_summary_needs_no_database():
    conn = FakeConn(_no_bucket)
    sc = resolve_scope(conn, Ctx(), "summary", "")
    assert not sc.scoped and sc.resolved["label"] == "Everything"
    assert sc.resolved["kinds"] == []
    assert conn.calls == []


def test_a_searched_summary_asks_every_kind_on_its_own_ladder():
    """The magnifier on the summary searches EVERYTHING: one term, five
    kinds, each on its own ladder and each with its own LIKE parameter -
    two kinds can be holding two different rungs at the same time."""
    def handler(text, params):
        if "dashboard.buckets" in text:
            return []
        if "processed_data.entities e WHERE" in text:      # the entity probe
            return [{"name": "Springfield Works", "n": 2}] if params.get("sc_ent_pat") == "springfield%" else []
        if "processed_data.sources s WHERE" in text:       # the source probe
            return [{"name": "src:harvest", "n": 1}] if params.get("sc_src_pat") == "%springfield%" else []
        if "AS hits" in text:                              # the place count
            return [{"n": 0}]
        if 'FROM "ent"' in text or 'FROM "src"' in text:
            return [{"n": 3}]
        return []

    conn = FakeConn(handler)
    sc = resolve_scope(conn, Ctx(), "summary", "Springfield")
    assert sc.scoped
    assert [(h["kind"], h["match"]) for h in sc.resolved["kinds"]] == [
        ("entity", "prefix"), ("source", "substring")]
    # The strongest rung any kind reached is what the page reports.
    assert sc.resolved["match"] == "prefix"
    # Two patterns, two parameters, both bound.
    assert sc.params["sc_ent_pat"] == "springfield%" and sc.params["sc_src_pat"] == "%springfield%"
    # The sources the term named are their own CTE, because ent reads them.
    text = render(sc.with_clause())
    assert text.startswith('WITH "src_named" AS (')
    assert ' UNION ' in text


def test_a_summary_term_nobody_answers_charts_nothing():
    conn = FakeConn(lambda text, params: [{"n": 0}] if "AS hits" in text else [])
    sc = resolve_scope(conn, Ctx(), "summary", "Atlantis")
    assert sc.scoped and sc.resolved["match"] == "none" and sc.resolved["kinds"] == []
    assert "WHERE false" in render(sc.ent_cte)


def test_location_scope_tries_parts_then_substring():
    seen = []

    def handler(text, params):
        if "dashboard.buckets" in text:          # no location bucket in this archive
            return []
        if 'SELECT count(*) AS n FROM "ent"' in text:
            seen.append(sorted(k for k in params if k.startswith("sc_")))
            return [{"n": 0}] if "sc_pl_city" in params else [{"n": 2}]
        if 'SELECT count(*) AS n FROM "src"' in text:
            return [{"n": 1}]
        raise AssertionError(text)

    sc = resolve_scope(FakeConn(handler), Ctx(), "location", "Springfield, Ontario, Canada")
    assert seen == [["sc_pl_city", "sc_pl_country", "sc_pl_region"], ["sc_pat"]]
    assert sc.resolved["match"] == "substring"
    assert sc.resolved["label"] == "Springfield, Ontario, Canada"
    assert sc.params["sc_pat"] == "%springfield, ontario, canada%"


# ── Buckets of every kind, and the type axis ─────────────────
#
# The rule - A BUCKET WINS OVER A LITERAL MATCH - holds for every kind,
# not for entities only. These pin it for every kind, and pin the second
# axis every Diagrams scope resolves through: the CLASS rather than the
# thing, built from the same two CTEs so nothing downstream can tell them
# apart. The flow suite (tests/flow/test_bucket_kinds.py) runs the same code
# against a real archive.

def _rows(*members):
    return [{"id": 7, "name": "Organisations", "project": "_preseed Alpha",
             "member": m, "member_type": None} for m in members]


def _kind_handler(members=("Company", "Unternehmen"), vocabulary=None, counts=(2, 3)):
    """A connection that answers with one bucket of the asked kind, or with
    the archive's own vocabulary when there is none."""
    def handler(text, params):
        if "FROM dashboard.buckets b" in text:
            return list(_rows(*members)) if members else []
        if "FROM dashboard.colour_groups g" in text:
            return []
        if "AS vals WHERE lower(value) LIKE" in text:
            if vocabulary is None:
                return []
            return [{"value": v} for v in vocabulary] if params["kv_pat"] == params["kv_pat"] else []
        if 'SELECT count(*) AS n FROM "ent"' in text:
            return [{"n": counts[0]}]
        if 'SELECT count(*) AS n FROM "src"' in text:
            return [{"n": counts[1]}]
        raise AssertionError("unexpected statement: " + text)
    return handler


def test_every_kind_and_axis_has_a_decision():
    """A pair that is not in the table is a scope whose second axis nobody
    decided about, and it would fail at run time instead of at import."""
    assert set(SCOPE_AXIS_KIND) == {(k, a) for k in KINDS for a in AXES}
    # Every grouping kind named there is one that actually exists.
    named = {v for v in SCOPE_AXIS_KIND.values() if v}
    assert named <= set(GROUPING_KINDS)
    # connection_type is deliberately not a bucket kind - the colour groups
    # do that job - but it IS a grouping.
    assert COLOUR_GROUP_KIND not in BUCKET_KINDS
    assert COLOUR_GROUP_KIND in GROUPING_KINDS


def test_a_bucket_of_any_kind_wins_over_the_literal_term():
    for kind in BUCKET_KINDS:
        terms = resolve_terms(FakeConn(_kind_handler()), "_preseed Alpha", kind, "Unternehmen")
        assert terms.match == "bucket", kind
        assert terms.label == "Organisations", kind
        assert terms.values == ["Company", "Unternehmen"], kind
        assert terms.grouping.source == "bucket"


def test_without_a_bucket_the_terms_are_the_archives_own_values():
    conn = FakeConn(_kind_handler(members=(), vocabulary=["Company"]))
    terms = resolve_terms(conn, "_preseed Alpha", "entity_type", "Comp")
    assert terms.match == "exact" and terms.values == ["Company"]
    assert terms.grouping is None
    # One value found on a non-exact rung becomes the label; the term stays
    # the label when it matched exactly or when several values answered.
    assert terms.label == "Comp"


def test_a_term_the_archive_does_not_know_still_searches_for_itself():
    conn = FakeConn(_kind_handler(members=(), vocabulary=None))
    terms = resolve_terms(conn, "_preseed Alpha", "unit", "furlong")
    assert terms.match == "none" and terms.values == ["furlong"]


def test_the_values_are_lower_cased_and_de_duplicated_once():
    conn = FakeConn(_kind_handler(members=("Company", "company", "Unternehmen")))
    assert resolve_terms(conn, "p", "entity_type", "Company").lowered == ["company", "unternehmen"]


def test_a_grouping_with_no_members_groups_nothing():
    """It must not swallow a term: a bucket somebody made and has not filled
    yet would otherwise answer with an empty search."""
    def handler(text, params):
        if "FROM dashboard.buckets b" in text:
            return [{"id": 7, "name": "Empty", "project": None, "member": None, "member_type": None}]
        return []
    conn = FakeConn(handler)
    assert groupings(conn, "p", "entity_type") == []
    assert resolve_terms(conn, "p", "entity_type", "Empty").match == "none"


def test_the_member_index_says_which_bucket_holds_a_value():
    conn = FakeConn(_kind_handler())
    assert member_index(conn, "p", "entity_type") == {
        "company": "Organisations", "unternehmen": "Organisations"}


def test_a_colour_group_is_read_where_a_bucket_would_be():
    def handler(text, params):
        if "FROM dashboard.colour_groups g" in text:
            return [{"id": 3, "name": "Supplier", "project": None,
                     "member": "Lieferant", "member_type": None},
                    {"id": 3, "name": "Supplier", "project": None,
                     "member": "Supplier", "member_type": None}]
        return []
    terms = resolve_terms(FakeConn(handler), "p", COLOUR_GROUP_KIND, "Lieferant")
    assert terms.match == "bucket" and terms.label == "Supplier"
    assert terms.grouping.source == "colour group"
    assert sorted(terms.values) == ["Lieferant", "Supplier"]


def test_the_type_axis_matches_a_list_of_values_not_a_pattern():
    conn = FakeConn(_kind_handler())
    sc = resolve_scope(conn, Ctx(), "entity", "Unternehmen", axis="type")
    assert sc.resolved["axis"] == "type" and sc.resolved["match"] == "bucket"
    assert sc.resolved["label"] == "Organisations"
    assert sc.resolved["values"] == ["Company", "Unternehmen"]
    assert sc.params["sc_vals"] == ["company", "unternehmen"]
    body = render(sc.ent_cte)
    assert 'lower("e"."text_type") = ANY(%(sc_vals)s)' in body
    assert "LIKE" not in body


def test_each_scope_reads_its_own_column_on_the_type_axis():
    wanted = {
        "entity": 'lower("e"."text_type") = ANY',
        "source": 'lower("s"."text_type") = ANY',
        "location": 'lower("l"."text_type") = ANY',
        "events": 'lower("ev"."text_type") = ANY',
        # The market's second axis is the TOPIC. An outlook is a value a
        # reading HAS, not a class it belongs to, so the page that offered
        # "Outlook or sentiment" as a type named something the data does
        # not have (app/scope.py: SCOPE_AXIS_KIND).
        "market": 'lower("m"."text_topic") = ANY',
    }
    for kind, fragment in wanted.items():
        sc = resolve_scope(FakeConn(_kind_handler()), Ctx(), kind, "Rising", axis="type")
        text = render(sc.ent_cte) + render(sc.src_cte)
        assert fragment in text, kind


def test_the_summary_has_no_type_axis_and_an_unknown_axis_is_refused():
    with pytest.raises(ScopeError, match="already everything"):
        resolve_scope(FakeConn(_no_bucket), Ctx(), "summary", "Company", axis="type")
    with pytest.raises(ScopeError, match="unknown axis"):
        resolve_scope(FakeConn(_no_bucket), Ctx(), "entity", "Company", axis="sideways")


def test_the_object_axis_is_what_a_caller_gets_without_asking():
    conn = FakeConn(_bucket_handler)
    assert resolve_scope(conn, Ctx(), "entity", "Apple").resolved["axis"] == "object"
