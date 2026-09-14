"""The colour-group API: groups, the inverted type list, and what it colours.

    DATABASE_URL=postgresql://... python -m pytest tests/flow/test_colour_groups.py -q

The archive seeds eleven groups (dashboard/sql/02-dashboard-seed.sql) and the
preseed assigns the seven connection types it uses, in both languages:

    Supplier / Lieferant                supplier
    Competitor / Wettbewerber           competitor
    CEO                                 person
    Regulator / Regulierungsbehörde     regulator
    Partner                             partner
    Customer / Kunde                    customer
    Subsidiary / Tochtergesellschaft    ownership

and deliberately leaves "Mysterious Bond" (with its German "Geheimnisvolle
Verbindung") unassigned, so there is always one type that falls back.

The tests here write to `dashboard.colour_groups` and
`dashboard.colour_group_types`, which the preseed does not own in full - so
every group is made through the fixture that removes it again, and every
assignment is taken back in the teardown. A leftover row would change the
colour of an edge for every other test in the session.
"""

from __future__ import annotations

import pytest

from preseed import ALPHA

pytestmark = pytest.mark.flow

EN = {"project": ALPHA, "language": "English"}
DE = {"project": ALPHA, "language": "German"}

# The two spellings the preseed leaves without a group.
UNASSIGNED = "Mysterious Bond"
UNASSIGNED_DE = "Geheimnisvolle Verbindung"


# ── Helpers ──────────────────────────────────────────────────────────────

@pytest.fixture
def groups(client):
    """Makes colour groups and deletes them again, whatever the test did."""
    made: list[int] = []

    def make(name: str, colour: str = "#123456") -> dict:
        r = client.post("/api/colour-groups", json={"name": name, "colour": colour})
        assert r.status_code == 201, r.text
        made.append(r.json()["id"])
        return r.json()

    yield make
    for group_id in made:
        client.delete(f"/api/colour-groups/{group_id}")


@pytest.fixture
def assignments(client):
    """Assigns types and puts every one of them back to no group at the end."""
    touched: set[str] = set()

    def assign(type_name: str, key: str | None) -> dict:
        touched.add(type_name)
        r = client.put(f"/api/colour-groups/types/{type_name}", json={"group": key})
        assert r.status_code == 200, r.text
        return r.json()

    yield assign
    for name in touched:
        client.put(f"/api/colour-groups/types/{name}", json={"group": None})


def all_groups(client) -> dict:
    r = client.get("/api/colour-groups")
    assert r.status_code == 200, r.text
    return {g["key"]: g for g in r.json()["groups"]}


def types(client, **params) -> dict:
    r = client.get("/api/colour-groups/types", params={**EN, **params})
    assert r.status_code == 200, r.text
    return r.json()


def by_name(data: dict) -> dict:
    return {i["type"]: i for i in data["items"]}


# ── The groups ───────────────────────────────────────────────────────────

def test_the_seeded_groups_are_listed_with_their_type_counts(client):
    body = client.get("/api/colour-groups").json()
    keys = [g["key"] for g in body["groups"]]
    assert keys[:3] == ["competitor", "regulator", "investor"]
    assert body["fallback"] == "other"
    assert keys[-1] == "other", "the fallback sorts last, where a legend ends"

    groups = {g["key"]: g for g in body["groups"]}
    # Both spellings of the preseed's supplier are in the same group.
    assert groups["supplier"]["types"] == 2
    assert groups["other"]["fallback"] is True
    # Every seeded colour carries a readable text colour and 3:1 on white.
    for g in body["groups"]:
        assert g["text_colour"] in ("#212121", "#ffffff")
        assert g["contrast"] >= 3.0, f"{g['key']} is too pale for a line on white"


def test_the_fallback_group_can_report_what_lands_in_it(client, assignments):
    """`count(colour_group_types)` is zero for the fallback and always will
    be: nothing is filed under it, types merely land there when nothing else
    claims them. So the page cannot label that zero "0 types" beside eight
    rows that read "Falls back to Other" - `?project=` adds the other number.
    """
    plain = client.get("/api/colour-groups").json()
    # Without a project there is nothing to count against, and the field says
    # so with null rather than a zero somebody would believe.
    assert plain["unassigned"] is None

    body = client.get("/api/colour-groups", params={"project": ALPHA}).json()
    every = types(client, limit=200)
    without = [i for i in every["items"] if not i["group"]]
    assert body["unassigned"] == len(without) > 0
    assert body["assigned"] + body["unassigned"] == every["total"]

    # And it follows a decision: assigning one of them moves it across.
    before = body["unassigned"]
    assignments(UNASSIGNED, "investor")
    after = client.get("/api/colour-groups", params={"project": ALPHA}).json()
    assert after["unassigned"] == before - 1
    assert after["assigned"] == body["assigned"] + 1


def test_the_counts_are_this_project_s_and_not_the_archive_s(client):
    """The Colours page prints one fraction out of these two numbers, and the
    type list beside it is the denominator - so they have to be counted over
    the same rows.

    It is easy to get wrong: `colour_group_types` is keyed by the type NAME
    across every project, so a numerator counting all of them beside an
    `unassigned` counting only this project's remainder would, on Beta,
    which holds a single connection type, read "12 of 12 types assigned"
    next to a table with one
    row, and three groups promised two types each that were nowhere in it.
    """
    from preseed import BETA
    body = client.get("/api/colour-groups", params={"project": BETA}).json()
    listed = client.get("/api/colour-groups/types",
                        params={"project": BETA, "language": "English", "limit": 200}).json()

    assert listed["total"] == 1, "the preseed's Beta project has one connection type"
    assert body["assigned"] + body["unassigned"] == listed["total"]

    per_group = {g["key"]: g for g in body["groups"]}
    # Partner is Beta's one type and it is assigned, so its group holds one
    # here; Supplier holds two - in Alpha - and none of Beta's.
    assert per_group["partner"]["types"] == 1
    assert per_group["supplier"]["types"] == 0
    # The archive-wide numbers are still answered, for the sentences that need
    # them: the page says how many are assigned elsewhere, and the delete
    # question says how many types actually move.
    assert per_group["supplier"]["types_all"] == 2
    assert body["assigned_all"] > body["assigned"]

    # Alpha holds every assignment the preseed made, so there the two agree.
    alpha = client.get("/api/colour-groups", params={"project": ALPHA}).json()
    assert alpha["assigned"] == alpha["assigned_all"]


def test_without_a_project_the_counts_are_the_whole_archive(client):
    body = client.get("/api/colour-groups").json()
    assert body["unassigned"] is None, "nothing to count the remainder against"
    for g in body["groups"]:
        assert g["types"] == g["types_all"]
    assert body["assigned"] == body["assigned_all"] > 0


def test_a_second_group_of_the_same_name_is_refused(client, groups):
    """Two groups called "Competitor" are two options that read the same in
    every select on the page, two identical rows in the list, and an edge
    whose colour cannot be traced to either. The key would have been unique
    ("competitor-2") and nothing else would have been."""
    made = groups("_test Twin", "#334455")

    r = client.post("/api/colour-groups", json={"name": "  _TEST twin  ", "colour": "#112233"})
    assert r.status_code == 409, r.text
    assert "_test Twin" in r.json()["error"]
    assert r.json()["hint"]

    # And a rename cannot walk around it either.
    other = groups("_test Other name", "#445566")
    r = client.put(f"/api/colour-groups/{other['id']}", json={"name": "_test Twin"})
    assert r.status_code == 409, r.text
    # The one that was there is untouched, and there is still only one of it.
    named = [g for g in client.get("/api/colour-groups").json()["groups"]
             if g["name"].lower() == "_test twin"]
    assert len(named) == 1 and named[0]["id"] == made["id"]

    # Renaming a group to what it already is called is not a clash.
    assert client.put(f"/api/colour-groups/{made['id']}",
                      json={"name": "_test Twin", "colour": "#556677"}).status_code == 200


def test_a_group_can_be_made_renamed_and_recoloured(client, groups):
    made = groups("_test Logistics", "#2f4f7f")
    assert made["key"] == "-test-logistics" or made["key"].startswith("test-logistics") \
        or made["key"] == "test-logistics"
    assert all_groups(client)[made["key"]]["name"] == "_test Logistics"

    r = client.put(f"/api/colour-groups/{made['id']}",
                   json={"name": "_test Freight", "colour": "#0F5132",
                         "description": "Hauliers and ports"})
    assert r.status_code == 200, r.text
    assert r.json()["warning"] == ""
    after = all_groups(client)[made["key"]]
    assert (after["name"], after["colour"], after["description"]) == (
        "_test Freight", "#0f5132", "Hauliers and ports")
    # The key does not change with the name: it is what JSON and the legend
    # refer to the group by.
    assert after["key"] == made["key"]


def test_a_pale_colour_is_saved_with_a_warning_rather_than_refused(client, groups):
    made = groups("_test Pale", "#f2f2f2")
    assert "3:1" in made["warning"]
    assert made["contrast"] < 3.0
    assert all_groups(client)[made["key"]]["colour"] == "#f2f2f2"


def test_something_that_is_not_a_colour_is_a_400(client, groups):
    made = groups("_test Colours")
    r = client.put(f"/api/colour-groups/{made['id']}", json={"colour": "rebeccapurple"})
    assert r.status_code == 400, r.text
    assert "hexadecimal" in r.json()["hint"]
    # A hex without the hash is accepted - that is what people paste.
    assert client.put(f"/api/colour-groups/{made['id']}", json={"colour": "AA3377"}).status_code == 200
    assert all_groups(client)[made["key"]]["colour"] == "#aa3377"


# ── The inverted view ────────────────────────────────────────────────────

def test_every_type_of_the_project_is_listed_in_every_language(client):
    data = types(client, limit=200)
    items = by_name(data)
    assert data["total"] == len(items) >= 20

    assert items["Supplier"]["group"] == "supplier"
    assert items["Lieferant"]["group"] == "supplier", "the German spelling is its own row"
    assert items["Supplier"]["colour"] == items["Lieferant"]["colour"]
    assert items["Supplier"]["source"] == "manual"

    # The unassigned one shows the fallback's colour and says nothing is stored.
    assert items[UNASSIGNED]["group"] is None
    assert items[UNASSIGNED]["source"] == ""
    assert items[UNASSIGNED]["colour"] == data["fallback"]["colour"]
    assert UNASSIGNED_DE in items

    # Most-seen first, so the types that matter are on the first page.
    counts = [i["connections"] for i in data["items"]]
    assert counts == sorted(counts, reverse=True)
    assert items["Supplier"]["connections"] > 0


def test_the_list_can_be_searched_and_narrowed_to_the_unassigned(client):
    found = by_name(types(client, q="lief"))
    assert set(found) == {"Lieferant"}

    unassigned = by_name(types(client, unassigned="true", limit=200))
    assert UNASSIGNED in unassigned and "Supplier" not in unassigned
    assert all(i["group"] is None for i in unassigned.values())

    both = types(client, q="bond", unassigned="true")
    assert list(by_name(both)) == [UNASSIGNED]


def test_the_list_pages(client):
    first = types(client, limit=5, page=0)
    second = types(client, limit=5, page=1)
    assert first["pages"] == second["pages"] > 1
    assert len(first["items"]) == 5
    assert set(by_name(first)).isdisjoint(set(by_name(second)))


def test_a_project_only_sees_its_own_types(client):
    from preseed import BETA
    r = client.get("/api/colour-groups/types",
                   params={"project": BETA, "language": "English", "limit": 200})
    assert r.status_code == 200, r.text
    assert UNASSIGNED not in by_name(r.json())


# ── Assigning ────────────────────────────────────────────────────────────

def test_assigning_a_type_changes_the_colour_the_graph_draws(client, groups, assignments):
    """The point of the whole page: a colour chosen here is the colour of the
    edge over there, at once - the resolver's cache is dropped on every write."""
    before = client.get("/api/graph/neighbours", params={**EN, "q": "Zurich Insurance"}).json()
    bond = next(t for n in before["neighbours"] for t in n["types"] if t["name"] == UNASSIGNED)
    assert bond["group"] == "other"

    made = groups("_test Bonds", "#7B1FA2")
    answer = assignments(UNASSIGNED, made["key"])
    assert answer["previous"] is None
    assert answer["group"]["colour"] == "#7b1fa2"

    after = client.get("/api/graph/neighbours", params={**EN, "q": "Zurich Insurance"}).json()
    bond = next(t for n in after["neighbours"] for t in n["types"] if t["name"] == UNASSIGNED)
    assert bond["colour"] == "#7b1fa2"
    assert bond["group"] == made["key"]
    # And the legend of that graph now names the group.
    assert made["key"] in {g["key"] for g in after["legend"]}


def test_the_answer_carries_the_previous_group_so_a_change_can_be_undone(client, assignments):
    first = assignments(UNASSIGNED, "investor")
    assert first["previous"] is None
    second = assignments(UNASSIGNED, "partner")
    assert second["previous"] == "investor"

    # The undo of the page is that value posted back.
    back = assignments(UNASSIGNED, second["previous"])
    assert by_name(types(client, q="bond"))[UNASSIGNED]["group"] == "investor"
    assert back["previous"] == "partner"


def test_taking_the_assignment_away_returns_the_type_to_the_fallback(client, assignments):
    assignments(UNASSIGNED, "investor")
    answer = assignments(UNASSIGNED, None)
    assert answer["group"] is None and answer["previous"] == "investor"

    item = by_name(types(client, q="bond"))[UNASSIGNED]
    assert item["group"] is None and item["source"] == ""
    assert item["colour"] == types(client, q="bond")["fallback"]["colour"]


def test_assigning_to_a_group_that_is_not_there_is_a_404(client):
    r = client.put(f"/api/colour-groups/types/{UNASSIGNED}", json={"group": "_test nothing"})
    assert r.status_code == 404, r.text
    assert r.json()["hint"]


def test_a_type_name_with_a_slash_can_be_assigned(client, assignments):
    """Type names are free text; the route takes the rest of the path so a
    slash in a name does not cut the address in two."""
    answer = assignments("Supplier/Vendor", "supplier")
    assert answer["type"] == "Supplier/Vendor"
    assert answer["group"]["key"] == "supplier"


# ── Deleting ─────────────────────────────────────────────────────────────

def test_deleting_a_group_moves_its_types_to_the_fallback(client, groups, assignments):
    made = groups("_test Doomed", "#334455")
    assignments(UNASSIGNED, made["key"])
    assert by_name(types(client, q="bond"))[UNASSIGNED]["group"] == made["key"]

    r = client.delete(f"/api/colour-groups/{made['id']}")
    assert r.status_code == 200, r.text
    assert r.json()["moved"] == 1 and r.json()["moved_to"] == "Other"

    # The decision survives the group: the type is now in the fallback group
    # by an actual row, not by having lost its assignment.
    item = by_name(types(client, q="bond"))[UNASSIGNED]
    assert item["group"] == "other" and item["source"] == "manual"
    assert made["key"] not in all_groups(client)


def test_the_fallback_group_cannot_be_deleted(client):
    fallback = all_groups(client)["other"]
    r = client.delete(f"/api/colour-groups/{fallback['id']}")
    assert r.status_code == 409, r.text
    assert "fall" in r.json()["error"].lower()
    assert r.json()["hint"]
    assert "other" in all_groups(client)


def test_an_unknown_group_is_a_404(client):
    assert client.delete("/api/colour-groups/999999").status_code == 404
    assert client.put("/api/colour-groups/999999", json={"name": "x"}).status_code == 404


# ── Suggestions ──────────────────────────────────────────────────────────

def test_suggest_proposes_and_saves_nothing(client):
    r = client.post("/api/colour-groups/suggest",
                    json={"types": [UNASSIGNED, "Supplier", "Zzz Qqq"]})
    assert r.status_code == 200, r.text
    body = r.json()
    items = {i["type"]: i for i in body["items"]}

    # "Supplier" is a supplier and "Zzz Qqq" is nothing - the one that matches
    # no rule is offered as the fallback, marked as not matched.
    #
    # What this test deliberately does NOT assert is which group the rules
    # propose for "Mysterious Bond". The keyword list is a helper with no
    # authority (app/colours.py), the page saves nothing without a press, and
    # pinning today's guess into a test would turn a rule somebody may want to
    # improve into a promise. What matters here is the CONTRACT: an answer per
    # type, a colour to tint the row with, and an archive that is unchanged.
    assert items["Supplier"]["group"] == "supplier" and items["Supplier"]["matched"] is True
    assert items["Zzz Qqq"]["matched"] is False and items["Zzz Qqq"]["group"] == "other"
    assert set(items) == {UNASSIGNED, "Supplier", "Zzz Qqq"}
    assert body["asked"] == 3 and 1 <= body["proposed"] <= 3
    assert items[UNASSIGNED]["colour"] == all_groups(client)[items[UNASSIGNED]["group"]]["colour"]

    # NOTHING was written: the page fills its fields, a person saves them.
    assert by_name(types(client, q="bond"))[UNASSIGNED]["group"] is None
