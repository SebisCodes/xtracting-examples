"""GET /api/events: by entity (buckets apply) and by type, in both languages.

    DATABASE_URL=postgresql://... python -m pytest tests/flow/test_events.py -q

The preseed's eight Alpha events are the whole cast of this file:

    +30 d  Battery product launch    Product launch      Apple Inc.
    +10 d  Hearing scheduled         Hearing             - nothing -
     -1 d  Battery announcement      Product announcement Apple Inc., Foxconn
     -3 d  Antitrust probe opened    Regulatory action   Commission, Apple, Microsoft
    -21 d  Pump delivered            Delivery            Springfield Works, Nordwyk
    -62 d  Harvest processed         Harvest             Springfield Mills
   -205 d  Subsidiary registered     Registration        Apple, Apple Inc.
   -740 d  Old antitrust fine        Regulatory action   Microsoft

and the bucket "Apple" holds (Apple Inc., Company) and (Apple, Company) -
never Apple the fruit, which is why the harvest must not turn up.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from preseed import ALPHA, BETA

pytestmark = pytest.mark.flow

EN = {"project": ALPHA, "language": "English"}
DE = {"project": ALPHA, "language": "German"}


def events(client, params: dict, expect: int = 200) -> dict:
    r = client.get("/api/events", params={**EN, **params})
    assert r.status_code == expect, r.text
    return r.json()


def names(data: dict) -> list[str]:
    return [e["name"] for e in data["events"]]


def test_everything_is_listed_newest_first(client):
    data = events(client, {})
    assert data["total"] == 8
    assert data["pages"] == 1
    assert names(data) == [
        "Battery product launch", "Hearing scheduled", "Battery announcement",
        "Antitrust probe opened", "Pump delivered", "Harvest processed",
        "Subsidiary registered", "Old antitrust fine",
    ]
    dates = [datetime.fromisoformat(e["date"]) for e in data["events"]]
    assert dates == sorted(dates, reverse=True)


def test_by_entity_a_bucket_finds_every_member_and_only_members(client):
    data = events(client, {"by": "entity", "q": "Apple"})
    assert sorted(names(data)) == ["Antitrust probe opened", "Battery announcement",
                                   "Battery product launch", "Subsidiary registered"]
    # Apple the fruit is in the archive and in the harvest document, but not
    # in the bucket - so the harvest is not "about Apple".
    assert "Harvest processed" not in names(data)

    resolved = data["resolved"]
    assert resolved["match"] == "bucket"
    assert resolved["label"] == "Apple"
    members = {(m["name"], m["type"]) for m in resolved["bucket"]["members"]}
    assert members == {("Apple Inc.", "Company"), ("Apple", "Company")}


def test_a_member_name_says_which_bucket_it_belongs_to(client):
    """The notice above the list is built from this, and it must not lie:
    «"Apple Inc." is a bucket: Apple, Apple Inc.» - no bucket is called
    Apple Inc. The answer therefore says which MEMBER was typed and what
    term shows that member alone."""
    data = events(client, {"by": "entity", "q": "Apple Inc."})
    resolved = data["resolved"]
    assert resolved["match"] == "bucket"
    assert resolved["bucket"]["name"] == "Apple", "the bucket, not the term"
    assert resolved["member"] == {"name": "Apple Inc.", "type": "Company",
                                  "q": "Apple Inc. (Company)"}
    # The bucket's own name is a member's name too, so it gets the same offer.
    assert events(client, {"by": "entity", "q": "Apple"})["resolved"]["member"] == {
        "name": "Apple", "type": "Company", "q": "Apple (Company)"}


def test_a_type_in_the_term_shows_that_entity_alone(client):
    """"Show only Apple Inc." searches this term. If every spelling of the
    name resolved back into the bucket, the antitrust probe - which is about
    Apple the Company, not about Apple Inc. - would be on the list of a
    reader who asked for Apple Inc."""
    data = events(client, {"by": "entity", "q": "Apple Inc. (Company)"})
    assert sorted(names(data)) == ["Battery announcement", "Battery product launch",
                                   "Subsidiary registered"]
    assert "Antitrust probe opened" not in names(data)
    resolved = data["resolved"]
    assert resolved["match"] == "exact" and resolved["type"] == "Company"
    assert resolved["bucket"] is None, "the bucket is not expanded"
    assert resolved["in_bucket"]["name"] == "Apple", "but the way back to it is offered"

    # The other member, and the fruit that is in no bucket at all.
    company = events(client, {"by": "entity", "q": "Apple (Company)"})
    assert sorted(names(company)) == ["Antitrust probe opened", "Subsidiary registered"]
    # The fruit is in the harvest document, but the harvest event is about
    # the mill - so the fruit is found, has no events, and is in no bucket.
    fruit = events(client, {"by": "entity", "q": "Apple (Fruit)"})
    assert names(fruit) == []
    assert fruit["resolved"]["match"] == "exact" and fruit["resolved"]["entities"] >= 1
    assert fruit["resolved"]["in_bucket"] is None


def test_by_entity_without_a_bucket_uses_the_ladder(client):
    exact = events(client, {"by": "entity", "q": "Microsoft"})
    assert sorted(names(exact)) == ["Antitrust probe opened", "Old antitrust fine"]
    assert exact["resolved"]["match"] == "exact"

    prefix = events(client, {"by": "entity", "q": "Foxc"})
    assert names(prefix) == ["Battery announcement"]
    assert prefix["resolved"]["match"] == "prefix"

    nothing = events(client, {"by": "entity", "q": "Atlantis Ltd"})
    assert nothing["total"] == 0 and nothing["resolved"]["match"] == "none"


def test_by_type_matches_exactly_then_by_prefix(client):
    exact = events(client, {"by": "type", "q": "Regulatory action"})
    assert sorted(names(exact)) == ["Antitrust probe opened", "Old antitrust fine"]
    assert exact["resolved"]["match"] == "exact"

    prefix = events(client, {"by": "type", "q": "Product"})
    assert sorted(names(prefix)) == ["Battery announcement", "Battery product launch"]
    assert prefix["resolved"]["match"] == "prefix"
    assert sorted(prefix["resolved"]["names"]) == ["Product announcement", "Product launch"]

    nothing = events(client, {"by": "type", "q": "Coronation"})
    assert nothing["total"] == 0 and nothing["resolved"]["names"] == []


def test_an_event_carries_its_entities_its_source_and_its_date(client):
    (probe,) = [e for e in events(client, {"by": "type", "q": "Regulatory action"})["events"]
                if e["name"] == "Antitrust probe opened"]
    assert probe["type"] == "Regulatory action"
    assert sorted(e["name"] for e in probe["entities"]) == ["Apple", "European Commission", "Microsoft"]
    assert probe["source"]["uri"] == "https://filings.example.org/antitrust-2026"
    assert probe["dated"] is True
    assert probe["relation"] == "Direct"

    # The one event that is about the document and nothing in it.
    (hearing,) = [e for e in events(client, {"by": "type", "q": "Hearing"})["events"]]
    assert hearing["entities"] == []


def test_german_asks_the_same_questions_in_german(client):
    # Entity ids are shared by the translations, so the bucket - whose
    # members are the English names - still finds the German rows.
    german = client.get("/api/events", params={**DE, "by": "entity", "q": "Apple"}).json()
    assert len(german["events"]) == 4
    assert "Produktankündigung" in {e["type"] for e in german["events"]}

    by_type = client.get("/api/events", params={**DE, "by": "type", "q": "Produktankündigung"}).json()
    assert [e["name"] for e in by_type["events"]] == ["Battery announcement"]
    assert by_type["resolved"]["match"] == "exact"

    # The English type name finds nothing in the German rows, which is the
    # point of binding the language.
    assert client.get("/api/events",
                      params={**DE, "by": "type", "q": "Product announcement"}).json()["total"] == 0


def test_the_date_range_is_inclusive_at_both_ends(client):
    tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).date().isoformat()
    future = events(client, {"date_from": tomorrow})
    assert sorted(names(future)) == ["Battery product launch", "Hearing scheduled"]

    week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).date().isoformat()
    recent = events(client, {"date_from": week_ago, "date_to": tomorrow})
    assert sorted(names(recent)) == ["Antitrust probe opened", "Battery announcement"]

    backwards = events(client, {"date_from": tomorrow, "date_to": week_ago}, expect=400)
    assert "ends before it starts" in backwards["error"]


def test_the_other_project_has_its_own_events(client):
    beta = client.get("/api/events", params={"project": BETA, "language": "English",
                                             "by": "entity", "q": "Apple"}).json()
    assert names(beta) == ["Partnership announced"]
    # The Alpha bucket belongs to Alpha; in Beta the name resolves as a
    # plain entity.
    assert beta["resolved"]["match"] == "exact"


def test_an_unknown_way_of_asking_is_refused(client):
    bad = events(client, {"by": "everything"}, expect=400)
    assert "by=entity or by=type" in bad["hint"]
    assert events(client, {"date_from": "last tuesday"}, expect=400)["error"]


def test_paging_reports_the_same_total(client):
    """THE ROWS COME IN BLOCKS, and the total does not ride on them.

    One request brings BLOCK_SIZE rows and the pager moves inside the block it
    already holds (api_events.py:45-52), so asking for page 1 of block 0 is
    the same fetch and answers with the same rows - the page number decides
    which twenty of them the view draws, not which twenty it is sent. Only a
    page in a LATER block is a second request, and past the last block there
    is nothing to send.

    What must hold either way is the count: it comes from its own statement
    rather than from a count(*) OVER() riding on the rows, so a page past the
    end still knows how many events there are. Without that, the pager would
    say "no events in this project yet" about a project with eight of them
    the moment somebody clicked one page too far.
    """
    first = events(client, {})
    assert first["page"] == 0 and first["page_size"] == 20
    assert first["total"] == 8 and first["pages"] == 1

    inside = events(client, {"page": 1})
    assert inside["page"] == 1
    assert inside["block"] == first["block"] == 0
    assert inside["events"] == first["events"], "one block, one fetch"
    assert inside["total"] == 8 and inside["pages"] == 1

    beyond = events(client, {"page": first["pages_per_block"]})
    assert beyond["block"] == 1
    assert beyond["events"] == []
    assert beyond["total"] == 8 and beyond["pages"] == 1
