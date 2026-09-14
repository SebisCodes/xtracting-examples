"""The buckets API against the preseed: /api/buckets and /api/buckets/resolve.

    DATABASE_URL=postgresql://... python -m pytest tests/flow/test_buckets.py -q

The cast of this file is the Apples of `_preseed Alpha`:

    ent:apple-inc    Apple Inc.  (Company)   in tasks alpha:1 and alpha:5
    ent:apple        Apple       (Company)   in tasks alpha:2 and alpha:5
    ent:apple-fruit  Apple       (Fruit)     in task  alpha:4   ("Apfel" in German)

and in `_preseed Beta` there is a second Apple (Company), ent:apple in beta:1 -
which is what a bucket without a project has to reach and a bucket of Alpha
must not.

The preseeded bucket "Apple" holds (Apple Inc., Company) and (Apple, Company),
so it covers four entity records - Apple the fruit is NOT one of them, and
the type is the only thing that keeps it out.

Every test that writes cleans up after itself: this session shares one archive
with the other flow tests, and test_events.py asserts about the preseeded
bucket as it stands.
"""

from __future__ import annotations

import pytest

from preseed import ALPHA, BETA

pytestmark = pytest.mark.flow

EN = {"project": ALPHA, "language": "English"}
DE = {"project": ALPHA, "language": "German"}
BETA_EN = {"project": BETA, "language": "English"}


# ── Helpers ──────────────────────────────────────────────────────────────

@pytest.fixture
def buckets(client):
    """A factory that makes buckets and takes them away again.

        b = buckets("_test Apples", members=[("Apple", "Company")])

    Named `_test …` so a leftover row is recognisable, and deleted in the
    teardown even when the test failed half way through.
    """
    made: list[int] = []

    def make(name: str, members=(), every_project: bool = False, project=ALPHA) -> dict:
        r = client.post("/api/buckets", params={"project": project, "language": "English"},
                        json={"name": name, "every_project": every_project,
                              "members": [{"name": n, "type": t} for n, t in members]})
        assert r.status_code == 201, r.text
        made.append(r.json()["id"])
        return r.json()

    yield make
    for bucket_id in made:
        client.delete(f"/api/buckets/{bucket_id}")


def resolve(client, params: dict, expect: int = 200) -> dict:
    r = client.get("/api/buckets/resolve", params=params)
    assert r.status_code == expect, r.text
    return r.json()


def per_language(data: dict) -> dict:
    return {row["language"]: row["entities"] for row in data["languages"]}


def listed(client, params: dict) -> dict:
    r = client.get("/api/buckets", params=params)
    assert r.status_code == 200, r.text
    return {b["name"]: b for b in r.json()["items"]}


# ── The list ─────────────────────────────────────────────────────────────

def test_the_preseeded_bucket_is_listed_with_its_members(client):
    items = listed(client, EN)
    apple = items["Apple"]
    assert apple["member_count"] == 2
    assert {(m["name"], m["type"]) for m in apple["members"]} == {
        ("Apple Inc.", "Company"), ("Apple", "Company")}
    assert apple["project"] == ALPHA and apple["every_project"] is False


def test_a_bucket_of_one_project_is_not_offered_in_another(client):
    assert "Apple" in listed(client, EN)
    assert "Apple" not in listed(client, BETA_EN)


# ── What a bucket matches ────────────────────────────────────────────────

def test_the_members_are_counted_once_each_and_in_every_language(client):
    apple = listed(client, EN)["Apple"]
    data = resolve(client, {**EN, "id": apple["id"]})

    # Two members, two records each, four in total - and the same four in
    # German, because the ids are shared by the translations of a task even
    # though the German type is spelled "Unternehmen".
    assert data["entities"] == 4
    assert per_language(data) == {"English": 4, "German": 4}
    assert {(m["name"], m["entities"]) for m in data["members"]} == {
        ("Apple Inc.", 2), ("Apple", 2)}
    # The language on screen comes first, so the page reads its own number.
    assert data["languages"][0]["language"] == "English"
    assert resolve(client, {**DE, "id": apple["id"]})["languages"][0]["language"] == "German"


def test_two_members_matching_the_same_entity_are_still_one(client, buckets):
    """(Apple Inc., Company) and (Apple Inc., any type) are two members and
    one entity: the count is over the entities, not over the matches."""
    made = buckets("_test One Apple", members=[("Apple Inc.", "Company")])
    assert resolve(client, {**EN, "id": made["id"]})["entities"] == 2

    r = client.post(f"/api/buckets/{made['id']}/members", json={"name": "Apple Inc.", "type": None})
    assert r.status_code == 201, r.text
    data = resolve(client, {**EN, "id": made["id"]})
    assert data["entities"] == 2
    assert sorted(m["entities"] for m in data["members"]) == [2, 2]


def test_the_type_is_what_keeps_the_fruit_out(client, buckets, conn):
    """A member with a type matches that type only; without one it takes
    every Apple in the archive, fruit included."""
    made = buckets("_test Apples", members=[("Apple", "Company")])
    with_type = resolve(client, {**EN, "id": made["id"]})
    assert with_type["entities"] == 2

    # The same question asked of the archive directly: the two records are
    # the company, never ent:apple-fruit.
    ids = {r["text_entity_id"] for r in conn.execute(
        "SELECT DISTINCT text_entity_id FROM processed_data.entities "
        "WHERE text_project = %s AND lower(text_name) = 'apple' AND lower(text_type) = 'company'",
        (ALPHA,)).fetchall()}
    assert ids == {"ent:apple"}

    r = client.post(f"/api/buckets/{made['id']}/members", json={"name": "Apple", "type": ""})
    assert r.status_code == 201, r.text
    without_type = resolve(client, {**EN, "id": made["id"]})
    # One record more: Apple the fruit, in task alpha:4.
    assert without_type["entities"] == 3
    assert per_language(without_type) == {"English": 3, "German": 3}


def test_deleting_a_member_drops_the_count_again(client, buckets):
    made = buckets("_test Shrinking", members=[("Apple", "Company"), ("Apple Inc.", "Company")])
    assert resolve(client, {**EN, "id": made["id"]})["entities"] == 4

    members = listed(client, EN)["_test Shrinking"]["members"]
    victim = next(m for m in members if m["name"] == "Apple Inc.")
    r = client.delete(f"/api/buckets/{made['id']}/members/{victim['id']}")
    assert r.status_code == 200, r.text
    assert r.json()["member"] == {"name": "Apple Inc.", "type": "Company"}

    assert resolve(client, {**EN, "id": made["id"]})["entities"] == 2
    assert listed(client, EN)["_test Shrinking"]["member_count"] == 1


def test_a_bucket_without_a_project_applies_to_every_project(client, buckets):
    made = buckets("_test Everywhere", members=[("Apple", "Company")], every_project=True)
    assert listed(client, EN)["_test Everywhere"]["every_project"] is True
    assert "_test Everywhere" in listed(client, BETA_EN)

    # Alpha holds two records of Apple (Company), Beta exactly one.
    assert resolve(client, {**EN, "id": made["id"]})["entities"] == 2
    beta = resolve(client, {**BETA_EN, "id": made["id"]})
    assert beta["entities"] == 1
    assert per_language(beta) == {"English": 1}


def test_a_bucket_that_matches_nothing_says_so(client, buckets):
    made = buckets("_test Atlantis", members=[("Atlantis Ltd", "Company")])
    data = resolve(client, {**EN, "id": made["id"]})
    assert data["entities"] == 0
    assert data["languages"] == []
    assert data["members"][0]["entities"] == 0


def test_resolving_by_term_finds_the_bucket_a_search_would_use(client):
    """?q= is the search box's question: app/scope.py expands a term through
    the same bucket, so the page can show what a search will actually do."""
    data = resolve(client, {**EN, "q": "Apple"})
    assert data["bucket"]["name"] == "Apple"
    assert data["entities"] == 4

    # A member's name finds the bucket it is in, not only the bucket's name.
    by_member = resolve(client, {**EN, "q": "Apple Inc."})
    assert by_member["bucket"]["name"] == "Apple"

    nothing = resolve(client, {**EN, "q": "Atlantis"})
    assert nothing["bucket"] is None and nothing["entities"] == 0


# ── Writing ──────────────────────────────────────────────────────────────

def test_a_second_bucket_of_the_same_name_is_refused(client, buckets):
    buckets("_test Twice")
    r = client.post("/api/buckets", params=EN, json={"name": "_test Twice"})
    assert r.status_code == 409, r.text
    assert "already" in r.json()["error"]


def test_a_member_can_only_be_in_a_bucket_once_however_it_is_spelled(client, buckets):
    made = buckets("_test Once", members=[("Apple Inc.", "Company")])
    r = client.post(f"/api/buckets/{made['id']}/members",
                    json={"name": "apple inc.", "type": "COMPANY"})
    assert r.status_code == 409, r.text
    assert listed(client, EN)["_test Once"]["member_count"] == 1


def test_a_bucket_can_be_renamed_and_moved_to_every_project(client, buckets):
    made = buckets("_test Renamed", members=[("Apple", "Company")])
    r = client.put(f"/api/buckets/{made['id']}", params=EN,
                   json={"name": "_test Renamed Twice", "every_project": True})
    assert r.status_code == 200, r.text
    items = listed(client, BETA_EN)
    assert "_test Renamed Twice" in items
    assert items["_test Renamed Twice"]["every_project"] is True


def test_deleting_a_bucket_answers_with_what_it_held_so_it_can_come_back(client, buckets):
    """The undo on the page is this answer posted straight back."""
    made = buckets("_test Undo", members=[("Apple", "Company"), ("Apple Inc.", "Company")])
    r = client.delete(f"/api/buckets/{made['id']}")
    assert r.status_code == 200, r.text
    kept = r.json()["bucket"]
    assert kept["name"] == "_test Undo"
    assert {(m["name"], m["type"]) for m in kept["members"]} == {
        ("Apple", "Company"), ("Apple Inc.", "Company")}
    assert "_test Undo" not in listed(client, EN)

    back = client.post("/api/buckets", params=EN, json=kept)
    assert back.status_code == 201, back.text
    restored = listed(client, EN)["_test Undo"]
    assert restored["member_count"] == 2
    assert resolve(client, {**EN, "id": restored["id"]})["entities"] == 4
    client.delete(f"/api/buckets/{restored['id']}")


def test_a_new_bucket_is_offered_by_the_suggestions(client, buckets):
    """The bucket field of the Events page reads /api/suggest; a bucket that
    cannot be found there might as well not exist."""
    buckets("_test Suggested", members=[("Apple", "Company")])
    r = client.get("/api/suggest/bucket", params={**EN, "q": "_test Sugg"})
    assert r.status_code == 200, r.text
    assert "_test Suggested" in [i["value"] for i in r.json()["items"]]


# ── Refusals ─────────────────────────────────────────────────────────────

def test_a_bucket_needs_a_name(client):
    r = client.post("/api/buckets", params=EN, json={"name": "   "})
    assert r.status_code == 400, r.text
    assert r.json()["hint"]


def test_an_unknown_bucket_is_a_404(client):
    assert client.delete("/api/buckets/999999").status_code == 404
    assert client.post("/api/buckets/999999/members", json={"name": "Apple"}).status_code == 404
    assert resolve(client, {**EN, "id": 999999}, expect=404)["hint"]


def test_resolving_without_a_term_or_an_id_is_a_400(client):
    body = resolve(client, EN, expect=400)
    assert "id" in body["hint"] and "q" in body["hint"]
