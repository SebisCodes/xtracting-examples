"""Scope resolution against the preseed: the bucket, the ladder, both
languages, project isolation, and the other scope kinds.

    DATABASE_URL=postgresql://... python -m pytest tests/flow/test_scope_resolution.py -q
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from psycopg import sql

from app.scope import find_bucket, resolve_scope
# From the preseed itself, not `from conftest import ...`: with tests/unit
# collected in the same run, `conftest` names the unit suite's file. The
# flow conftest puts tests/preseed on the path before this module loads.
from preseed import ALPHA, BETA

pytestmark = pytest.mark.flow


@dataclass(frozen=True)
class Ctx:
    project: str
    language: str


EN = Ctx(ALPHA, "English")
DE = Ctx(ALPHA, "German")


def ids_of(conn, scope) -> set[str]:
    rows = conn.execute(sql.SQL("{w}SELECT DISTINCT id FROM ent").format(w=scope.with_clause()), scope.params).fetchall()
    return {r["id"] for r in rows}


def source_ids_of(conn, scope) -> set[str]:
    rows = conn.execute(sql.SQL("{w}SELECT DISTINCT id FROM src").format(w=scope.with_clause()), scope.params).fetchall()
    return {r["id"] for r in rows}


def entity_names_in_language(conn, scope, ctx) -> set[str]:
    """What a tab builder does: the CTE plus the data table bound to the
    request's language."""
    stmt = sql.SQL("{w}SELECT DISTINCT e.text_name FROM processed_data.entities e "
                   "WHERE e.text_project = %(project)s AND e.text_language = %(language)s AND {p}").format(
        w=scope.with_clause(), p=scope.ent_predicate("e", "text_entity_id"))
    return {r["text_name"] for r in conn.execute(stmt, scope.params).fetchall()}


# ── The bucket ───────────────────────────────────────────────

@pytest.mark.parametrize("ctx", [EN, DE], ids=["English", "German"])
def test_bucket_apple_resolves_to_both_members_and_not_the_fruit(conn, ctx):
    sc = resolve_scope(conn, ctx, "entity", "apple")
    assert sc.resolved["match"] == "bucket"
    assert sc.resolved["bucket"]["name"] == "Apple"
    assert {m["name"] for m in sc.resolved["bucket"]["members"]} == {"Apple Inc.", "Apple"}
    assert ids_of(conn, sc) == {"ent:apple-inc", "ent:apple"}
    assert "ent:apple-fruit" not in ids_of(conn, sc)
    # Distinct (task, id) pairs: Apple Inc. in tasks 1 and 5, Apple in 2 and 5.
    assert sc.resolved["entities"] == 4
    assert source_ids_of(conn, sc) == {"src:battery", "src:antitrust", "src:subsidiary"}


def test_the_german_view_returns_german_rows_for_the_same_ids(conn):
    en = entity_names_in_language(conn, resolve_scope(conn, EN, "entity", "Apple"), EN)
    de = entity_names_in_language(conn, resolve_scope(conn, DE, "entity", "Apple"), DE)
    assert en == {"Apple Inc.", "Apple"}
    assert de == {"Apple Inc.", "Apple"}
    # A German-only name still finds the id, and the English view shows it.
    sc = resolve_scope(conn, EN, "entity", "Europäische Kommission")
    assert sc.resolved["match"] == "exact"
    assert ids_of(conn, sc) == {"ent:ec"}
    assert entity_names_in_language(conn, sc, EN) == {"European Commission"}


def test_a_member_name_expands_to_the_whole_bucket(conn):
    sc = resolve_scope(conn, EN, "entity", "Apple Inc.")
    assert sc.resolved["match"] == "bucket"
    assert ids_of(conn, sc) == {"ent:apple-inc", "ent:apple"}
    assert find_bucket(conn, ALPHA, "Apple", "Fruit") is None       # the type keeps the fruit out
    assert find_bucket(conn, ALPHA, "Apple", "Company")["name"] == "Apple"


def test_a_type_in_the_term_means_one_entity_and_not_its_bucket(conn):
    """A bare name may be a bucket, a name with a type is one entity
    (app/scope.py, "A term that names ONE entity"). Without that rule all
    four rows the suggestion list offers for "Apple" - the bucket, the
    company, Apple Inc., the fruit - sent the same query and came back as
    the bucket, and Apple Inc. could not be looked at alone anywhere."""
    inc = resolve_scope(conn, EN, "entity", "Apple Inc. (Company)")
    assert ids_of(conn, inc) == {"ent:apple-inc"}
    assert inc.resolved["match"] == "exact"
    assert inc.resolved["type"] == "Company"
    assert inc.resolved["label"] == "Apple Inc. (Company)"
    assert inc.resolved["bucket"] is None, "the bucket is not expanded"
    # …but the answer knows the bucket, so the notice can offer the way back.
    assert inc.resolved["in_bucket"]["name"] == "Apple"

    company = resolve_scope(conn, EN, "entity", "Apple (Company)")
    assert ids_of(conn, company) == {"ent:apple"}
    fruit = resolve_scope(conn, EN, "entity", "Apple (Fruit)")
    assert ids_of(conn, fruit) == {"ent:apple-fruit"}
    assert fruit.resolved["in_bucket"] is None, "the type keeps the fruit out"


def test_the_typed_term_crosses_languages_like_every_other_term(conn):
    """The CTEs bind the project and not the language, and a type is no
    different: the German type name finds the same ids as the English one,
    which is what makes a link work in either view."""
    assert ids_of(conn, resolve_scope(conn, DE, "entity", "Apfel (Frucht)")) == {"ent:apple-fruit"}
    assert ids_of(conn, resolve_scope(conn, EN, "entity", "Apfel (Frucht)")) == {"ent:apple-fruit"}
    assert ids_of(conn, resolve_scope(conn, DE, "entity", "Apple (Fruit)")) == {"ent:apple-fruit"}


def test_the_bucket_answer_names_the_member_that_was_typed(conn):
    """What the notice is built from. «"Apple Inc." is a bucket» was an
    untruth - the bucket is called Apple - so the answer carries the bucket
    AND the member, with the term that shows that member alone."""
    sc = resolve_scope(conn, EN, "entity", "Apple Inc.")
    assert sc.resolved["bucket"]["name"] == "Apple"
    assert sc.resolved["member"] == {"name": "Apple Inc.", "type": "Company",
                                     "q": "Apple Inc. (Company)"}
    # Nothing to offer when the term is the bucket's name and no member
    # carries it: "Everyone" holds Contoso and Apple, neither is called that.
    conn.execute("INSERT INTO dashboard.buckets (text_name, text_project) VALUES ('Everyone', %s)", (ALPHA,))
    conn.execute("INSERT INTO dashboard.bucket_members (bigint_fk_bucket, text_name, text_type) "
                 "SELECT bigint_id, 'Microsoft', 'Company' FROM dashboard.buckets WHERE text_name = 'Everyone'")
    every = resolve_scope(conn, EN, "entity", "Everyone")
    assert every.resolved["bucket"]["name"] == "Everyone"
    assert every.resolved["member"] is None


def test_a_member_without_a_type_is_offered_only_when_the_name_has_one(conn):
    """A member recorded as "this name, whatever its type" has no term of
    its own unless the archive gives the name a single type - "Apple" is a
    Company and a Fruit, and there is no term that means one of the two."""
    conn.execute("INSERT INTO dashboard.buckets (text_name, text_project) VALUES ('Loose', %s)", (ALPHA,))
    conn.execute("INSERT INTO dashboard.bucket_members (bigint_fk_bucket, text_name, text_type) "
                 "SELECT bigint_id, 'Microsoft', NULL FROM dashboard.buckets WHERE text_name = 'Loose'")
    conn.execute("INSERT INTO dashboard.bucket_members (bigint_fk_bucket, text_name, text_type) "
                 "SELECT bigint_id, 'Apple', NULL FROM dashboard.buckets WHERE text_name = 'Loose'")
    one = resolve_scope(conn, EN, "entity", "Microsoft").resolved
    assert one["bucket"]["name"] == "Loose"
    assert one["member"] == {"name": "Microsoft", "type": "Company", "q": "Microsoft (Company)"}
    two = resolve_scope(conn, EN, "entity", "Apple").resolved
    assert two["bucket"]["name"] == "Apple", "the bucket named Apple still wins"
    assert two["member"]["q"] == "Apple (Company)", "and its member carries a type"


def test_a_name_that_really_ends_in_brackets_is_searched_whole(conn):
    """The type reading is put to the archive first; when no entity of that
    name and type exists the whole string is searched as it was typed, so a
    bucket - or an entity - called "Team (EU)" is not lost to the rule."""
    conn.execute("INSERT INTO dashboard.buckets (text_name, text_project) VALUES ('Team (EU)', %s)", (ALPHA,))
    conn.execute("INSERT INTO dashboard.bucket_members (bigint_fk_bucket, text_name, text_type) "
                 "SELECT bigint_id, 'European Commission', 'Regulator' FROM dashboard.buckets "
                 "WHERE text_name = 'Team (EU)'")
    sc = resolve_scope(conn, EN, "entity", "Team (EU)")
    assert sc.resolved["match"] == "bucket" and sc.resolved["bucket"]["name"] == "Team (EU)"
    assert ids_of(conn, sc) == {"ent:ec"}
    # And a type nothing carries falls through to the ladder, which finds
    # nothing rather than raising.
    assert resolve_scope(conn, EN, "entity", "Apple (Vegetable)").resolved["match"] == "none"


def test_the_fruit_alone_is_found_by_its_german_name(conn):
    sc = resolve_scope(conn, DE, "entity", "Apfel")
    assert sc.resolved["match"] == "exact" and sc.resolved["bucket"] is None
    assert ids_of(conn, sc) == {"ent:apple-fruit"}


def test_beta_has_no_bucket_and_its_own_apple(conn):
    sc = resolve_scope(conn, Ctx(BETA, "English"), "entity", "Apple")
    assert sc.resolved["match"] == "exact" and sc.resolved["bucket"] is None
    assert ids_of(conn, sc) == {"ent:apple"}
    assert sc.resolved["entities"] == 1
    assert source_ids_of(conn, sc) == {"src:contoso"}


def test_a_project_null_bucket_applies_to_every_project(conn):
    conn.execute("INSERT INTO dashboard.buckets (text_name, text_project) VALUES ('Everyone', NULL)")
    conn.execute("INSERT INTO dashboard.bucket_members (bigint_fk_bucket, text_name, text_type) "
                 "SELECT bigint_id, 'Contoso', NULL FROM dashboard.buckets WHERE text_name = 'Everyone'")
    conn.execute("INSERT INTO dashboard.bucket_members (bigint_fk_bucket, text_name, text_type) "
                 "SELECT bigint_id, 'Apple', 'Company' FROM dashboard.buckets WHERE text_name = 'Everyone'")
    sc = resolve_scope(conn, Ctx(BETA, "English"), "entity", "everyone")
    assert sc.resolved["match"] == "bucket"
    assert ids_of(conn, sc) == {"ent:contoso", "ent:apple"}
    # Alpha's own "Apple" bucket still wins for "Apple" in Alpha.
    assert resolve_scope(conn, EN, "entity", "Apple").resolved["bucket"]["project"] == ALPHA
    # (the connection is rolled back by the fixture)


# ── The ladder ───────────────────────────────────────────────

def test_prefix_then_substring(conn):
    sc = resolve_scope(conn, EN, "entity", "Springfield")
    assert sc.resolved["match"] == "prefix"
    assert ids_of(conn, sc) == {"ent:sf-works", "ent:sf-mills"}
    assert set(sc.resolved["names"]) == {"Springfield Works", "Springfield Mills"}

    sc = resolve_scope(conn, EN, "entity", "wyk")
    assert sc.resolved["match"] == "substring"
    assert ids_of(conn, sc) == {"ent:nordwyk"}
    assert sc.resolved["label"] == "Nordwyk Pumps"

    sc = resolve_scope(conn, EN, "entity", "Atlantis")
    assert sc.resolved["match"] == "none" and ids_of(conn, sc) == set()


# ── Other kinds ──────────────────────────────────────────────

def test_source_scope_by_host_and_by_name(conn):
    sc = resolve_scope(conn, EN, "source", "filings.example.org")
    assert source_ids_of(conn, sc) == {"src:antitrust", "src:subsidiary"}
    assert ids_of(conn, sc) == {"ent:ec", "ent:apple", "ent:microsoft", "ent:apple-inc", "ent:linjiang"}
    sc = resolve_scope(conn, EN, "source", "src:pump")
    assert sc.resolved["match"] == "exact"
    assert ids_of(conn, sc) == {"ent:nordwyk", "ent:sf-works", "ent:zurich"}


@pytest.mark.parametrize("ctx, place, expected", [
    (EN, "Springfield, Ontario, Canada", {"ent:sf-mills"}),
    (DE, "Springfield, Ontario, Kanada", {"ent:sf-mills"}),
    (EN, "Springfield, Illinois, USA", {"ent:sf-works"}),
    (EN, "Springfield", {"ent:sf-works", "ent:sf-mills"}),
    (EN, "USA", {"ent:apple-inc", "ent:microsoft", "ent:sf-works"}),
    (DE, "Brüssel, Belgien", {"ent:ec"}),
    (EN, "Linjiang", {"ent:linjiang"}),
], ids=["ontario", "ontario-de", "illinois", "either", "country", "brussels-de", "one-part"])
def test_location_scope_isolates_the_right_springfield(conn, ctx, place, expected):
    sc = resolve_scope(conn, ctx, "location", place)
    assert sc.resolved["match"] == "exact"
    assert ids_of(conn, sc) == expected


def test_location_scope_falls_back_to_a_substring_of_the_address(conn):
    sc = resolve_scope(conn, EN, "location", "Ontario")
    assert sc.resolved["match"] == "substring"
    assert ids_of(conn, sc) == {"ent:sf-mills"}


def test_market_and_events_scopes(conn):
    # THE TOPIC IS THE MARKET SCOPE'S TYPE AXIS. Its object axis is the
    # entity, like every other page: a market insight is about an entity and
    # carries a topic, and it has no third thing that is a type.
    sc = resolve_scope(conn, EN, "market", "antitrust", axis="type")
    assert sc.resolved["match"] == "exact"
    assert ids_of(conn, sc) == {"ent:apple", "ent:microsoft"}
    assert source_ids_of(conn, sc) == {"src:antitrust", "src:old-antitrust"}

    sc = resolve_scope(conn, DE, "market", "Kartellrecht", axis="type")
    assert ids_of(conn, sc) == {"ent:apple", "ent:microsoft"}

    # And the object axis reads an entity.
    sc = resolve_scope(conn, EN, "market", "Microsoft")
    assert sc.resolved["match"] == "exact"
    assert ids_of(conn, sc) == {"ent:microsoft"}

    sc = resolve_scope(conn, EN, "events", "Regulatory action")
    assert ids_of(conn, sc) == {"ent:ec", "ent:apple", "ent:microsoft"}
    assert source_ids_of(conn, sc) == {"src:antitrust", "src:old-antitrust"}

    # An event about the document only: a source, no entities.
    sc = resolve_scope(conn, EN, "events", "Hearing")
    assert ids_of(conn, sc) == set()
    assert source_ids_of(conn, sc) == {"src:antitrust"}


# ── The summary: nothing typed, and the magnifier that searches everything ──

def test_an_empty_summary_is_the_whole_project(conn):
    sc = resolve_scope(conn, EN, "summary", "")
    assert not sc.scoped
    n = conn.execute(sql.SQL("SELECT count(*) AS n FROM processed_data.entities e WHERE e.text_project = %(project)s "
                             "AND e.text_language = %(language)s AND {p}").format(p=sc.ent_predicate("e", "text_entity_id")),
                     sc.params).fetchone()["n"]
    # 21, not 17: Apple Inc. is named three times in each of the two tasks
    # that carry it (preseed.py: MENTIONS), which is what lets a suggestion
    # count mean mentions rather than task/entity pairs.
    assert n == 21


@pytest.mark.parametrize("term", [
    "Apple",              # a bucket, and two documents whose address says apple
    "Springfield",        # entities by prefix, documents by substring, two places
    "Antitrust",          # a market topic, and the documents named after it
    "Zurich",             # an entity and a place
    "Regulatory action",  # an event type alone
    "example.org",        # a host alone
    "Apple (Fruit)",      # one named entity, and nothing else called that
    "Atlantis",           # nobody
])
def test_the_summary_term_is_the_union_of_what_every_kind_answers(conn, term):
    """THE SPECIFICATION OF THE SUMMARY'S MAGNIFIER, as one property.

    A term typed on the summary means what it means on the Entity page,
    PLUS what it means on the Source page, PLUS the Location, Market and
    Events pages - each on its own ladder. So the union of the five scoped
    answers has to be exactly the summary's answer, and this is the test
    that says so for a bucket, a host, a place, a topic and a type.
    """
    summary = resolve_scope(conn, EN, "summary", term)
    entities: set[str] = set()
    sources: set[str] = set()
    for kind in ("entity", "source", "location", "market", "events"):
        one = resolve_scope(conn, EN, kind, term)
        if one.resolved["match"] != "none":
            entities |= ids_of(conn, one)
            sources |= source_ids_of(conn, one)
    assert ids_of(conn, summary) == entities
    assert source_ids_of(conn, summary) == sources


def test_the_summary_says_which_kinds_answered(conn):
    sc = resolve_scope(conn, EN, "summary", "Apple")
    assert [(h["kind"], h["match"]) for h in sc.resolved["kinds"]] == [
        ("entity", "bucket"), ("source", "substring")]
    assert sc.resolved["match"] == "bucket"           # the strongest rung reached
    assert sc.resolved["bucket"]["name"] == "Apple"
    # The bucket alone would be two entities; the documents bring three more.
    assert ids_of(conn, sc) == {"ent:apple", "ent:apple-inc",
                                "ent:foxconn", "ent:linjiang", "ent:tim-cook"}
    assert sc.resolved["entities"] > resolve_scope(conn, EN, "entity", "Apple").resolved["entities"]


def test_the_summary_understands_a_term_that_names_one_entity(conn):
    """A link copied from the Entity page can be opened on the summary. If
    the rule stopped at the Entity page, "Apple (Fruit)" would come back
    here as "nothing in this project is called that" - the term would look
    broken on the one page that is meant to find everything."""
    sc = resolve_scope(conn, EN, "summary", "Apple (Fruit)")
    assert [(h["kind"], h["match"], tuple(h["names"])) for h in sc.resolved["kinds"]] == [
        ("entity", "exact", ("Apple (Fruit)",))]
    assert ids_of(conn, sc) == {"ent:apple-fruit"}
    # A bare "Apple" is still the bucket AND the documents - and it does not
    # even hold the fruit. The two terms are two different searches, which
    # is the whole point of carrying the type.
    bare = ids_of(conn, resolve_scope(conn, EN, "summary", "Apple"))
    assert len(bare) > 1 and "ent:apple-fruit" not in bare


def test_a_summary_term_nobody_answers_charts_nothing(conn):
    sc = resolve_scope(conn, EN, "summary", "Atlantis")
    assert sc.scoped and sc.resolved["match"] == "none" and sc.resolved["kinds"] == []
    assert ids_of(conn, sc) == set() and source_ids_of(conn, sc) == set()


def test_the_summary_search_is_per_project_and_finds_the_german_name(conn):
    # Alpha's Apple bucket does not reach into Beta: Beta's own Apple is
    # found by name, and Apple Inc. - which only Alpha has - is not.
    beta = resolve_scope(conn, Ctx(BETA, "English"), "summary", "Apple")
    assert beta.resolved["bucket"] is None
    assert "ent:apple" in ids_of(conn, beta) and "ent:apple-inc" not in ids_of(conn, beta)
    # A German name resolves the ids, and the English view still shows them.
    sc = resolve_scope(conn, EN, "summary", "Europäische Kommission")
    assert "ent:ec" in ids_of(conn, sc)
    assert entity_names_in_language(conn, sc, EN) >= {"European Commission"}
