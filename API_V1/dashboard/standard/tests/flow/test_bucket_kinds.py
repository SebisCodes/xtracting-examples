"""Buckets of every kind, against the preseed.

    DATABASE_URL=postgresql://... python -m pytest tests/flow/test_bucket_kinds.py -q

A bucket began as "several spellings of one entity". It is the same problem
everywhere else, and in a bilingual archive it is the problem twice over: the
preseed's `_preseed Alpha` holds every type in English AND in German, so
`entity_types` has "Company" and "Unternehmen", `event_types` has "Product
launch" and "Produkteinführung", and until somebody says they are the same,
every type search and every "by type" chart splits one thing in half.

What is pinned here:

  * the rule - A BUCKET WINS OVER A LITERAL MATCH - for
    every kind, whether the term is the bucket's NAME or one of its members;
  * a bilingual type bucket answers with the SAME ENTITY SET in both
    languages, and presents it as ONE thing (the label is the bucket, the
    match is "bucket");
  * an overlapping member is counted ONCE - the members of the Orgs bucket
    add up to 28 records and the bucket matches 14, because the German and
    English spellings of one type are the same entities;
  * a value inside a bucket does not appear on its own in the vocabulary list
    for its kind, in the dropdowns AND in the suggestions - otherwise a
    person picks the member and silently defeats the merge;
  * a colour group is usable wherever a bucket would be, which is why
    connection types are not a bucket kind.

Everything written here is named `_c …` and removed in the fixture teardown,
including on failure: this session shares one archive with the other flow
tests and test_buckets.py asserts about the preseeded bucket as it stands.
"""

from __future__ import annotations

import pytest
from psycopg import sql

from app import scope
from app.routers import api_suggest
from preseed import ALPHA, BETA

pytestmark = pytest.mark.flow

EN = {"project": ALPHA, "language": "English"}
DE = {"project": ALPHA, "language": "German"}


class Ctx:
    """What app/scope.py reads off a request."""

    def __init__(self, language: str, project: str = ALPHA):
        self.project = project
        self.language = language


# ── Helpers ──────────────────────────────────────────────────────────────

@pytest.fixture
def kinded(client):
    """A factory for buckets of any kind, cleaned up afterwards.

        b = kinded("_c Orgs", "entity_type", ["Company", "Unternehmen"])
    """
    made: list[int] = []

    def make(name: str, kind: str, members=(), every_project: bool = False,
             project: str = ALPHA) -> dict:
        r = client.post("/api/buckets", params={"project": project, "language": "English"},
                        json={"name": name, "kind": kind, "every_project": every_project,
                              "members": [{"name": m} for m in members]})
        assert r.status_code == 201, r.text
        made.append(r.json()["id"])
        return r.json()

    yield make
    for bucket_id in made:
        client.delete(f"/api/buckets/{bucket_id}")


def entity_ids(conn, scoped) -> set[tuple[str, str]]:
    """The entity set a ScopeSet actually resolves to - the thing every tab,
    every drilldown and every export is built from."""
    stmt = sql.SQL("{w}SELECT task_id, id FROM ent").format(w=scoped.with_clause())
    return {(r["task_id"], r["id"]) for r in conn.execute(stmt, scoped.params).fetchall()}


def names(items) -> list[str]:
    return [i["name"] if "name" in i else i["value"] for i in items]


# ── The rule, for every kind ─────────────────────────────────────────────

# One bilingual pair per kind, out of the preseed's own vocabulary, and the
# scope whose TYPE axis searches it. `unit` and `attribute_type` have no
# scope of their own - they are read by the charts and the dropdowns - so
# they are checked through resolve_terms alone.
BILINGUAL = [
    ("entity_type", "Company", "Unternehmen", "entity"),
    ("source_type", "News article", "Nachrichtenartikel", "source"),
    ("location_type", "Headquarters", "Hauptsitz", "location"),
    ("event_type", "Product launch", "Produkteinführung", "events"),
    ("market_topic", "Batteries", "Batterien", None),
    ("attribute_type", "Throughput", "Durchsatz", None),
]


@pytest.mark.parametrize("kind,english,german,_scope", BILINGUAL,
                         ids=[b[0] for b in BILINGUAL])
def test_a_bucket_wins_over_a_literal_match_for_every_kind(conn, kinded, kind, english,
                                                           german, _scope):
    """The term may be the bucket's NAME or any of its members; either way
    the search is the whole bucket, and the answer says so."""
    kinded(f"_c {kind}", kind, [english, german])
    for term in (f"_c {kind}", english, german):
        terms = scope.resolve_terms(conn, ALPHA, kind, term)
        assert terms.match == "bucket", f"{term!r} did not resolve to the bucket"
        assert terms.label == f"_c {kind}", "the label has to be the bucket, not the term"
        assert sorted(terms.values) == sorted([english, german])


def test_a_bilingual_type_bucket_answers_the_same_in_both_languages(conn, kinded):
    """THE WIN THIS FEATURE EXISTS FOR.

    "Company" typed in the English view and "Unternehmen" typed in the
    German view are one question, and the bucket makes them one answer -
    the same entity ids, the same count, and the bucket's own name as the
    label, so nobody is left wondering whether they are seeing one type or
    two merged.
    """
    kinded("_c Orgs", "entity_type",
           ["Company", "Unternehmen", "Regulator", "Regulierungsbehörde"])

    english = scope.resolve_scope(conn, Ctx("English"), "entity", "Company", axis="type")
    german = scope.resolve_scope(conn, Ctx("German"), "entity", "Regulierungsbehörde", axis="type")

    assert entity_ids(conn, english) == entity_ids(conn, german)
    assert english.resolved["entities"] == german.resolved["entities"] > 0
    for answer in (english, german):
        assert answer.resolved["match"] == "bucket"
        assert answer.resolved["label"] == "_c Orgs"
        assert answer.resolved["axis"] == "type"
        assert answer.resolved["bucket"]["kind"] == "entity_type"

    # And it is a real merge, not a coincidence: the bucket reaches more than
    # either half of it does on its own.
    without = scope.resolve_scope(conn, Ctx("English"), "entity", "Regulator", axis="type")
    assert without.resolved["match"] == "bucket"   # Regulator IS in the bucket now
    assert english.resolved["entities"] > 2


def test_an_overlapping_member_is_counted_once(client, kinded):
    """"Company" and "Unternehmen" are the same entities in two languages.
    The bucket matches them once; the members added up do not, and the page
    is told both numbers so it can say why they differ."""
    made = kinded("_c Orgs", "entity_type",
                  ["Company", "Unternehmen", "Regulator", "Regulierungsbehörde"])
    r = client.get("/api/buckets/resolve", params={**EN, "id": made["id"]})
    assert r.status_code == 200, r.text
    data = r.json()

    assert data["kind"] == "entity_type"
    per_member = {m["name"]: m["entities"] for m in data["members"]}
    assert per_member["Company"] == per_member["Unternehmen"] > 0
    assert per_member["Regulator"] == per_member["Regulierungsbehörde"] > 0
    # Four members, two things: the total is the union, not the sum.
    assert data["entities"] == per_member["Company"] + per_member["Regulator"]
    assert data["sum_of_members"] == sum(per_member.values())
    assert data["sum_of_members"] == 2 * data["entities"]

    # Both languages are reached, which is the whole reason the bucket exists.
    assert {row["language"] for row in data["languages"]} == {"English", "German"}


def test_a_type_bucket_reaches_further_than_one_of_its_spellings(conn, kinded):
    """Without the bucket, a search is one spelling; with it, it is the
    class. The count has to grow, or the merge did nothing."""
    alone = scope.resolve_scope(conn, Ctx("English"), "events", "Product launch", axis="type")
    kinded("_c Launches", "event_type",
           ["Product launch", "Produkteinführung", "Product announcement", "Produktankündigung"])
    merged = scope.resolve_scope(conn, Ctx("English"), "events", "Product launch", axis="type")
    assert merged.resolved["match"] == "bucket"
    assert merged.resolved["entities"] >= alone.resolved["entities"]
    assert entity_ids(conn, alone) <= entity_ids(conn, merged)


# ── The vocabulary lists ─────────────────────────────────────────────────

def test_a_member_does_not_appear_on_its_own_in_the_vocabulary(client, kinded):
    """A dropdown that offers both the bucket and its members lets a person
    pick the member and silently defeat the merge."""
    before = client.get("/api/types/entity", params=EN).json()
    assert "Company" in names(before["items"])

    kinded("_c Orgs", "entity_type", ["Company", "Unternehmen"])

    after = client.get("/api/types/entity", params=EN).json()
    listed = names(after["items"])
    assert "_c Orgs" in listed, "the bucket has to be offered"
    assert "Company" not in listed, "the member must not be offered beside it"
    assert listed.count("_c Orgs") == 1, "and it is offered once, not once per member"
    assert after["buckets"] == [{"name": "_c Orgs", "members": ["Company"]}]

    # The same rule in the suggestion list the search fields read.
    items = client.get("/api/suggest/entity_type", params={**EN, "q": ""}).json()["items"]
    values = [i["value"] for i in items]
    assert "_c Orgs" in values and "Company" not in values and "Unternehmen" not in values
    folded = next(i for i in items if i["value"] == "_c Orgs")
    assert "merged" in folded["hint"], "a row that stands for several values has to say so"


# WHICH SUGGESTION LISTS FOLD A BUCKET OF THIS KIND - read out of the
# router's own tables, never kept by hand here. A hand-kept list is exactly
# how `entity` and `location` went missing from BUCKETED_KINDS: seven of the
# nine kinds folded, the two a person is most likely to bucket did not, and
# the Map's, the Graph's and the Diagrams magnifier's fields all offered the
# bare member while never offering the bucket.
def _lists_that_fold(kind: str) -> list[str]:
    plain = [k for k, grouping in api_suggest.BUCKETED_KINDS.items() if grouping == kind]
    composed = [k for k, kinds in api_suggest.COMPOSED_KINDS.items() if kind in kinds]
    return sorted(plain + composed)


def _offers(items, member: str) -> list[dict]:
    """The rows that ARE this member - by value or by label, because a list
    that folds sends "Apple (Fruit)" as the value and shows "Apple"."""
    low = member.strip().lower()
    return [i for i in items
            if str(i["value"]).strip().lower() == low or str(i["label"]).strip().lower() == low]


@pytest.mark.parametrize("kind", scope.BUCKET_KINDS, ids=list(scope.BUCKET_KINDS))
def test_every_kind_a_bucket_can_be_about_folds_in_every_list_that_offers_it(client, kinded, kind):
    """THE WALK, over app/scope.BUCKET_KINDS rather than a list written out
    here - so a tenth kind cannot be forgotten the way the eighth and ninth
    were.

    For each kind: take a value out of that kind's own vocabulary, ask every
    suggestion list that folds the kind what it offers for it, then make a
    bucket holding it and ask again. The value has to disappear from the
    list and the bucket has to appear in its place. Lists that never offered
    the value are skipped - a country list cannot offer a street address -
    but at least one has to have offered it, or the test would pass by
    finding nothing anywhere.
    """
    lists = _lists_that_fold(kind)
    assert lists, (f"no suggestion list folds a {kind} bucket - a field that offers the "
                   "member and not the bucket lets a person defeat the merge silently")

    vocab = client.get("/api/buckets/vocabulary",
                       params={**EN, "kind": kind, "limit": 30}).json()
    free = [i["value"] for i in vocab["items"] if not i.get("in_bucket")]
    assert free, f"the preseed holds no {kind} to bucket"

    def offered(suggest_kind: str, term: str) -> list[dict]:
        r = client.get(f"/api/suggest/{suggest_kind}", params={**EN, "q": term, "limit": 50})
        assert r.status_code == 200, r.text
        return r.json()["items"]

    # The first value some list actually offers. The vocabulary a bucket is
    # built from is not bound to a language (that is the point of a
    # bilingual bucket) while most suggestion lists are, so the German
    # spelling of a topic can be the first row of the one and in none of the
    # other - and a member nothing offers would prove nothing here.
    member = ""
    before: dict[str, list[dict]] = {}
    for candidate in free:
        found = {name: _offers(offered(name, candidate), candidate) for name in lists}
        if any(found.values()):
            member, before = candidate, found
            break
    assert member, \
        f"no list offered any {kind} of the preseed, so this proves nothing: {free[:5]}"

    bucket = kinded(f"_c walk {kind}", kind, [member])

    for name in lists:
        if not before[name]:
            continue
        items = offered(name, member)
        values = [i["value"] for i in items]
        assert not _offers(items, member), (
            f"/api/suggest/{name} still offers the {kind} {member!r} on its own, "
            f"although {bucket['name']} holds it")
        assert bucket["name"] in values, (
            f"/api/suggest/{name} dropped the {kind} {member!r} without offering "
            f"{bucket['name']} in its place")


def test_the_bucket_is_suggested_by_its_own_name_too(client, kinded):
    """Its members may be spelled nothing like it, so a name search has to
    find it even when no member matches."""
    kinded("_c Werke", "location_type", ["Factory", "Fabrik"])
    values = [i["value"] for i in
              client.get("/api/suggest/location_type", params={**EN, "q": "werk"}).json()["items"]]
    assert "_c Werke" in values


def test_a_value_in_another_bucket_is_shown_as_taken_rather_than_offered(client, kinded):
    """A value ANOTHER bucket holds is listed - hiding it would answer "where
    is Unternehmen" with silence - and it names the bucket that holds it, so
    the reader knows where to go instead of being told "no".

    What THIS bucket already holds is a different case and is left out: it is
    on the page already, in the member list right above the field, so a row
    that offers it again spends one of a handful and says nothing."""
    kinded("_c Orgs", "entity_type", ["Company", "Unternehmen"])
    other = kinded("_c Other", "entity_type", ["Person"])

    data = client.get("/api/buckets/vocabulary",
                      params={**EN, "kind": "entity_type", "bucket": other["id"]}).json()
    rows = {i["value"]: i for i in data["items"]}
    assert rows["Company"]["in_bucket"] == "_c Orgs"
    assert "Person" not in rows, "this bucket's own member was offered to it again"
    assert rows["Region"]["in_bucket"] is None
    assert data["taken"] >= 2 and data["offered"] >= 1


def test_the_vocabulary_of_a_kind_is_that_kind_and_nothing_else(client):
    units = [i["value"] for i in
             client.get("/api/buckets/vocabulary", params={**EN, "kind": "unit"}).json()["items"]]
    assert "mAh" in units and "Company" not in units
    topics = [i["value"] for i in
              client.get("/api/buckets/vocabulary",
                         params={**EN, "kind": "market_topic"}).json()["items"]]
    assert "Batteries" in topics and "mAh" not in topics


# ── The kinds themselves ─────────────────────────────────────────────────

def test_two_kinds_may_share_a_name(client, kinded):
    """An entity called Company and the entity TYPE Company are two things
    that happen to share a word, and both may be bucketed."""
    kinded("_c Company", "entity_type", ["Company"])
    kinded("_c Company", "entity", [])          # the same name, another kind
    listed = client.get("/api/buckets", params=EN).json()["items"]
    same = [b for b in listed if b["name"] == "_c Company"]
    assert {b["kind"] for b in same} == {"entity", "entity_type"}


def test_a_second_bucket_of_one_kind_and_name_is_refused(client, kinded):
    kinded("_c Twice", "unit", ["t"])
    r = client.post("/api/buckets", params=EN, json={"name": "_c Twice", "kind": "unit"})
    assert r.status_code == 409, r.text
    assert "unit" in r.json()["error"].lower()


def test_the_list_can_be_asked_for_one_kind(client, kinded):
    kinded("_c Units", "unit", ["t"])
    only = client.get("/api/buckets", params={**EN, "kind": "unit"}).json()
    assert only["kind"] == "unit"
    assert {b["kind"] for b in only["items"]} == {"unit"}
    assert "Apple" not in {b["name"] for b in only["items"]}


def test_an_unknown_kind_is_refused_and_says_where_connections_are_grouped(client):
    r = client.post("/api/buckets", params=EN, json={"name": "_c No", "kind": "connection_type"})
    assert r.status_code == 400, r.text
    hint = r.json()["hint"]
    assert "Connection colours" in hint


def test_a_member_of_a_plain_kind_carries_no_type(client, kinded):
    """Only an entity member is a name AND a type; everywhere else a type
    would be a field with nothing to put in it, so it is dropped."""
    made = kinded("_c Units", "unit", [])
    r = client.post(f"/api/buckets/{made['id']}/members", json={"name": "t", "type": "Weight"})
    assert r.status_code == 201, r.text
    assert r.json()["member"]["type"] == ""


def test_the_kind_of_a_bucket_cannot_be_changed_by_a_rename(client, kinded):
    made = kinded("_c Units", "unit", ["t"])
    r = client.put(f"/api/buckets/{made['id']}", params=EN,
                   json={"name": "_c Units renamed", "kind": "entity"})
    assert r.status_code == 200, r.text
    assert r.json()["kind"] == "unit"


# ── Colour groups are the grouping for connection types ──────────────────

def test_a_colour_group_is_read_wherever_a_bucket_would_be(conn):
    """connection_type is deliberately not a bucket kind: the Connection
    colours page already groups those, and a second grouping for the same
    thing is how a product starts contradicting itself. So the group is what
    a search expands through."""
    group = conn.execute(
        "SELECT g.text_name AS name, t.text_type_name AS type "
        "FROM dashboard.colour_groups g "
        "JOIN dashboard.colour_group_types t ON t.bigint_fk_group = g.bigint_id LIMIT 1"
    ).fetchone()
    if group is None:
        pytest.skip("this archive has no assigned connection type yet")

    by_member = scope.resolve_terms(conn, ALPHA, "connection_type", group["type"])
    assert by_member.match == "bucket"
    assert by_member.label == group["name"]
    assert by_member.grouping.source == "colour group"
    assert group["type"] in by_member.values

    by_name = scope.resolve_terms(conn, ALPHA, "connection_type", group["name"])
    assert by_name.match == "bucket" and by_name.label == group["name"]


# ── Where a bucket applies ───────────────────────────────────────────────

def test_a_bucket_of_one_project_does_not_reach_another(conn, kinded):
    kinded("_c Orgs", "entity_type", ["Company", "Unternehmen"])
    assert scope.resolve_terms(conn, ALPHA, "entity_type", "Company").match == "bucket"
    assert scope.resolve_terms(conn, BETA, "entity_type", "Company").match != "bucket"


def test_a_bucket_for_every_project_reaches_both(conn, kinded):
    kinded("_c Everywhere", "entity_type", ["Company", "Unternehmen"], every_project=True)
    for project in (ALPHA, BETA):
        assert scope.resolve_terms(conn, project, "entity_type", "Company").match == "bucket"


# ── The other views, which must not answer differently ───────────────────

def test_the_query_place_search_makes_a_place_bucket_one_area(client, kinded):
    """"Rotterdam", "Springfield" and "Zurich" are three places in the
    archive and one area once somebody says so - one candidate, named after
    the bucket, and a search that reaches every member."""
    without = client.get("/api/query/places", params={**EN, "q": "Springfield"}).json()
    assert without["match"] == "city" and without["ambiguous"] is True

    kinded("_c Works", "location", ["Springfield", "Rotterdam"])

    data = client.get("/api/query/places", params={**EN, "q": "_c Works"}).json()
    assert data["match"] == "bucket" and data["ambiguous"] is False
    assert len(data["candidates"]) == 1
    only = data["candidates"][0]
    assert only["label"] == "_c Works"
    assert only["addresses"] >= 3          # two Springfields and a Rotterdam
    assert data["bucket"]["kind"] == "location"

    # And a member reaches the bucket too, so the term a person remembers
    # works whichever half of it they type.
    assert client.get("/api/query/places",
                      params={**EN, "q": "Rotterdam"}).json()["match"] == "bucket"

    # The search behind the candidate is the union of the members.
    answer = client.post("/api/query/search", params=EN,
                         json={"place": {"address": "_c Works"}, "terms": []})
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert body["where"]["label"] == "_c Works"
    assert body["where"]["bucket"]["name"] == "_c Works"

    one = client.post("/api/query/search", params=EN,
                      json={"place": {"address": "Rotterdam, Netherlands"}, "terms": []}).json()
    assert body["total"] >= one["total"] > 0


def test_the_events_page_answers_an_event_type_bucket_as_one_thing(client, kinded):
    alone = client.get("/api/events", params={**EN, "by": "type", "q": "Product launch"}).json()
    assert alone["resolved"]["match"] != "bucket"

    kinded("_c Launches", "event_type",
           ["Product launch", "Produkteinführung", "Product announcement", "Produktankündigung"])

    merged = client.get("/api/events", params={**EN, "by": "type", "q": "Produkteinführung"}).json()
    assert merged["resolved"]["match"] == "bucket"
    assert merged["resolved"]["label"] == "_c Launches"
    assert merged["resolved"]["bucket"]["name"] == "_c Launches"
    assert merged["total"] >= alone["total"] > 0


# ── A field only offers a bucket it can answer with ──────────────────────

def test_an_entity_field_is_not_offered_a_bucket_of_another_kind(client, kinded):
    """THE HALF-ANSWER THE FOLDING RULE EXISTS TO PREVENT, found on a real
    archive.

    If `/api/suggest/entity_or_bucket` read `dashboard.buckets` with no
    filter on `text_kind` and sorted buckets FIRST, an archive whose only
    bucket is "Fire", of kind `event_type`, would have it as the top row of
    the entity field on the Map, the Heatmap, the Graph and Events "by
    entity", hinted "bucket - 4 members". Picking it there would not merely
    be empty, it would be wrong: app/scope.py looks a bucket up by kind,
    finds no ENTITY bucket of that name, falls through to the literal term
    and maps a Team called Fire in Portland, Oregon - "1 entity around
    Fire" - while the four event types the person asked for are dropped
    without a word. The same bucket on Events "by type" answers correctly,
    so one named object would mean two different things on two views.

    A field offers what the search behind it can honour, and nothing else."""
    made = kinded("_c Fire", "event_type", ["Product launch", "Produkteinführung"])

    def values(kind: str, term: str) -> list[str]:
        r = client.get(f"/api/suggest/{kind}", params={**EN, "q": term, "limit": 50})
        assert r.status_code == 200, r.text
        return [i["value"] for i in r.json()["items"]] + [i["label"] for i in r.json()["items"]]

    # It is a real bucket and the list that CAN resolve it still offers it.
    assert made["name"] in values("event_type", "_c Fire")
    # The Summary magnifier resolves an event-type bucket too (scope.py's
    # _resolve_everything), so it keeps the row.
    assert made["name"] in values("anything", "_c Fire")
    # The four entity fields cannot, so they are not offered it.
    assert made["name"] not in values("entity_or_bucket", "_c Fire")
    assert made["name"] not in values("entity_or_bucket", "")

    # And the filter did not empty the entity field of the buckets it CAN
    # answer with - which is the failure the other way round.
    entity_bucket = kinded("_c Apples", "entity", ["Apple Inc."])
    assert entity_bucket["name"] in values("entity_or_bucket", "_c Apples")


def test_a_bucket_offered_on_a_field_is_a_bucket_that_field_resolves(client, kinded):
    """The general rule, walked rather than written out: every bucket a
    composed list offers has to be of a kind that list's own scope resolves
    (api_suggest.COMPOSED_KINDS). One row that is not is a search that
    silently means something else."""
    for kind in scope.BUCKET_KINDS:
        kinded(f"_c every {kind}", kind, [])

    for name, kinds in api_suggest.COMPOSED_KINDS.items():
        items = client.get(f"/api/suggest/{name}",
                           params={**EN, "q": "_c every", "limit": 50}).json()["items"]
        offered = {i["value"] for i in items}
        for kind in scope.BUCKET_KINDS:
            bucket = f"_c every {kind}"
            if kind in kinds:
                assert bucket in offered, \
                    f"/api/suggest/{name} resolves a {kind} bucket but does not offer one"
            else:
                assert bucket not in offered, \
                    f"/api/suggest/{name} offers the {kind} bucket {bucket!r} and cannot resolve it"
