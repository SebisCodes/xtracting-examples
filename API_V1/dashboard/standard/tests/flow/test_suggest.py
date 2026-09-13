"""GET /api/suggest/{kind}: the twelve lists behind the typeahead fields.

    DATABASE_URL=postgresql://... python -m pytest tests/flow/test_suggest.py -q

What is pinned here is the behaviour a person notices: what starts with what
they typed comes first, the type next to a name tells "Apple (Company)" from
"Apple (Fruit)", nothing from another project or another language leaks in,
and the list is always short.
"""

from __future__ import annotations

import re

import pytest

from app import sqlbuild
from preseed import ALPHA, BETA

pytestmark = pytest.mark.flow

EN = {"project": ALPHA, "language": "English"}
DE = {"project": ALPHA, "language": "German"}


def suggest(client, kind: str, q: str = "", params=None, expect: int = 200, **extra) -> dict:
    r = client.get(f"/api/suggest/{kind}", params={**(params or EN), "q": q, **extra})
    assert r.status_code == expect, r.text
    return r.json()


def values(data: dict) -> list[str]:
    return [i["value"] for i in data["items"]]


def test_an_entity_suggestion_carries_its_type_and_its_count(client):
    """The list is FOLDED: the preseed's bucket holds Apple and Apple Inc.,
    both Company, so those two rows are ONE row - the bucket - and Apple the
    FRUIT, which the bucket deliberately does not hold, stands on its own.

    That is the rule of the whole vocabulary (docs/DESIGN.md, and
    app/scope.member_index says why): a list that offers the bucket AND its
    members lets a person pick a member and silently defeat the merge.

    And the row left standing sends "Apple (Fruit)", not "Apple" - a bare
    name resolves to the bucket, so an untyped value would be an offer the
    search cannot keep. The LABEL stays the bare name, so the list still
    reads "Apple - Fruit"."""
    items = suggest(client, "entity", "app")["items"]
    assert [(i["value"], i["label"], i["hint"]) for i in items] == [
        ("Apple", "Apple", "2 merged - Apple Inc., Apple"),
        ("Apple (Fruit)", "Apple", "Fruit"),
    ]
    for item in items:
        assert item["count"] >= 1

    # The page where a bucket is MADE has to see the members it is made of,
    # so it asks with fold=0 - the same carve-out the Colours page gets for
    # connection types (static/js/buckets.js).
    members = suggest(client, "entity", "app", fold=0)["items"]
    assert {(i["value"], i["hint"]) for i in members} == {
        ("Apple", "Company"), ("Apple Inc.", "Company"), ("Apple", "Fruit")}
    for item in members:
        assert item["label"] == item["value"]


def test_the_prefix_comes_first_and_the_substring_only_fills_up(client):
    data = suggest(client, "entity", "s")
    # Two names START with "s"; the rest merely contain one. The LABEL is
    # what a person reads; the value beside it carries the type, so that
    # what is sent names one entity and not a bucket of that name.
    labels = [i["label"] for i in data["items"]]
    assert labels[:2] == ["Springfield Mills", "Springfield Works"]
    assert "Microsoft" in labels
    assert "Springfield Mills (Company)" in values(data)

    # Nothing starts with "ple", so the substring pass is all there is - and
    # both spellings of the company come back as the one bucket.
    only_substring = suggest(client, "entity", "ple")
    assert set(values(only_substring)) == {"Apple", "Apple (Fruit)"}


def test_an_empty_term_offers_the_most_common_values(client):
    data = suggest(client, "entity", "")
    assert len(data["items"]) > 0
    counts = [i["count"] for i in data["items"]]
    assert counts == sorted(counts, reverse=True)


def test_the_list_is_always_short(client):
    assert len(suggest(client, "connection_type", "")["items"]) == 12
    assert len(suggest(client, "connection_type", "", limit=3)["items"]) == 3
    suggest(client, "entity", "", expect=400, limit=500)


def test_places_come_from_the_place_list(client):
    addresses = suggest(client, "address", "spring")
    assert values(addresses) == ["Springfield, Illinois, USA", "Springfield, Ontario, Canada"]
    assert addresses["items"][0]["hint"] == "Springfield, Illinois, USA"

    # A city name finds the addresses in it, too - people type either.
    assert values(suggest(client, "address", "rotterdam")) == ["Rotterdam, Netherlands"]

    both = suggest(client, "city_or_country", "usa")
    assert values(both) == ["USA"]
    assert both["items"][0]["hint"] == "Country"
    assert suggest(client, "city_or_country", "zurich")["items"][0]["hint"] == "City"


def test_a_place_is_offered_at_every_size_the_heatmap_takes(client):
    """The Heatmap counts addresses and its box takes a city, a country or a
    whole address, so all three have to be in the list - a field that names a
    kind of value it cannot suggest is a feature nobody can find.

    The coarse rows come first because they are the ones that cannot be
    picked out of a list of addresses: "Springfield" as ONE row that counts
    both towns, then each town."""
    items = suggest(client, "place", "springfield")["items"]
    assert [(i["value"], i["hint"]) for i in items] == [
        ("Springfield", "City"),
        ("Springfield, Illinois, USA", "Springfield, Illinois, USA"),
        ("Springfield, Ontario, Canada", "Springfield, Ontario, Canada"),
    ]
    # The city row counts both towns; each address row counts its own.
    assert [i["count"] for i in items] == [2, 1, 1]

    usa = suggest(client, "place", "usa")["items"]
    assert usa[0]["value"] == "USA" and usa[0]["hint"] == "Country"
    assert usa[0]["count"] == 6, "five addresses plus Apple Inc.'s second office"
    assert {i["value"] for i in usa[1:]} == {
        "Cupertino, California, USA", "Redmond, Washington, USA",
        "Springfield, Illinois, USA", "Austin, Texas, USA"}

    # A one-part address IS its city, and is offered once: two rows carrying
    # the same value would look like two answers to one question.
    assert values(suggest(client, "place", "linjiang")) == ["Linjiang"]

    # Bound to the language like every other list: the German half spells the
    # country Kanada, and the country row is followed by the address in it.
    assert values(suggest(client, "place", "kanada", DE)) == [
        "Kanada", "Springfield, Ontario, Kanada"]
    assert values(suggest(client, "place", "canada", DE)) == []


def test_sources_domains_topics_and_types(client):
    # BY THE TITLE, AND NEVER BY THE ID. Asking for "src:b" must not be
    # answered with "src:battery" - the extraction's own id for a document,
    # which names nothing and is shown to nobody.
    assert values(suggest(client, "source", "inside")) == ["Inside the new battery"]
    assert suggest(client, "source", "inside")["items"][0]["hint"] == "news.example.com"
    assert values(suggest(client, "source", "src:b")) == []

    domains = values(suggest(client, "domain", ""))
    assert sorted(domains) == ["blog.example.net", "filings.example.org", "news.example.com"]

    assert "Product announcement" in values(suggest(client, "event_type", ""))
    assert values(suggest(client, "event_type", "prod")) == ["Product announcement", "Product launch"]
    assert "Batteries" in values(suggest(client, "market_topic", "batt"))


def test_connection_types_are_offered_in_every_language_of_the_project(client):
    # The colour groups are keyed by the type NAME as the archive spells it,
    # so this list deliberately ignores the language: "Supplier" and
    # "Lieferant" are two rows to assign, and both must be findable.
    names = values(suggest(client, "connection_type", "", limit=50))
    assert {"Supplier", "Lieferant", "Mysterious Bond"} <= set(names)
    assert values(suggest(client, "connection_type", "liefer")) == ["Lieferant"]


def test_buckets_are_the_dashboards_own_and_have_no_language(client):
    items = suggest(client, "bucket", "")["items"]
    assert [i["value"] for i in items] == ["Apple"]
    assert items[0]["count"] == 2, "two members"
    assert values(suggest(client, "bucket", "", DE)) == ["Apple"]


def test_entities_and_buckets_are_offered_in_one_list(client):
    """The Events field is labelled "Entity or bucket", so both must be in it.

    With the entity list alone a bucket could only be found by already
    knowing its name - "Apple" worked only because an entity happens to be
    called that too."""
    items = suggest(client, "entity_or_bucket", "app")["items"]
    # The bucket first: there are few of them and one changes what the
    # search MEANS, so it is worth the top row - and the two entities it
    # holds are IN it rather than beside it. Three offers of which two run
    # the same query, with nothing on screen saying so, is the thing the
    # folding rule exists to prevent.
    assert items[0]["value"] == "Apple"
    assert items[0]["hint"] == "bucket - 2 merged - Apple Inc. (Company), Apple (Company)"
    # …and the entities after it, each one a DIFFERENT SEARCH. The list shows
    # four distinguishable rows for "Apple", and while every one of them sent
    # a bare name every one of them came back as the bucket - four offers,
    # one query, and no way anywhere in the product to look at the fruit
    # alone. The type therefore travels in the value, in the spelling
    # app/scope.py resolves to that one entity; the label stays the bare name
    # so the list does not say the type twice.
    rest = {(i["value"], i["label"], i["hint"]) for i in items[1:]}
    assert rest == {("Apple (Fruit)", "Apple", "Fruit")}
    assert len({i["value"] for i in items}) == len(items), "two rows, two searches"

    # A bucket has no language of its own; the entities beside it do.
    german = suggest(client, "entity_or_bucket", "app", DE)["items"]
    # No member matched in German, so the bucket says how many it holds.
    assert german[0]["hint"] == "bucket - 2 members"
    assert not [i for i in german[1:] if i["hint"] in ("Company", "Fruit")]

    # And nothing of another project leaks in through the bucket half.
    beta = suggest(client, "entity_or_bucket", "apple",
                   {"project": BETA, "language": "English"})["items"]
    assert [i["hint"] for i in beta] == ["Company"], "the Alpha bucket is not Beta's"


def test_everything_is_offered_in_one_list(client):
    """The Summary page of Diagrams is the view of everything and its
    magnifier searches everything (app/scope.py: _resolve_everything), so
    the field behind it has to be able to offer all seven kinds - and the
    hint is what says which of them a row is, because "Zurich" is an entity
    AND a place."""
    items = suggest(client, "anything", "zurich")["items"]
    assert {(i["value"], i["hint"]) for i in items} == {
        ("Zurich Insurance (Company)", "Company"), ("Zurich, Switzerland", "Place")}

    # A bucket still takes the top row, and an event type, a market topic
    # and a host are all reachable from the one field.
    assert suggest(client, "anything", "app")["items"][0]["hint"] == \
        "bucket - 2 merged - Apple Inc., Apple", "a bucket wins here as it does everywhere else"
    assert ("Regulatory action", "Event type") in \
        {(i["value"], i["hint"]) for i in suggest(client, "anything", "regulatory")["items"]}
    assert ("Batteries", "Market topic") in \
        {(i["value"], i["hint"]) for i in suggest(client, "anything", "batter")["items"]}
    assert ("news.example.com", "Host") in \
        {(i["value"], i["hint"]) for i in suggest(client, "anything", "news.")["items"]}
    assert ("src:harvest", "Document") in \
        {(i["value"], i["hint"]) for i in suggest(client, "anything", "src:har")["items"]}

    # Everything the field offers, the summary search can actually resolve.
    for item in suggest(client, "anything", "")["items"]:
        r = client.get("/api/diagrams/summary/sources",
                       params={**EN, "q": item["value"], "timeframe": "5y"})
        assert r.status_code == 200, r.text
        assert r.json()["resolved"]["match"] != "none", item


def test_nothing_leaks_across_language_or_project(client):
    assert values(suggest(client, "entity", "Apfel")) == []
    assert values(suggest(client, "entity", "Apfel", DE)) == ["Apfel (Frucht)"]

    assert values(suggest(client, "entity", "Contoso")) == []
    assert values(suggest(client, "entity", "Contoso",
                          {"project": BETA, "language": "English"})) == ["Contoso (Company)"]
    assert values(suggest(client, "address", "berlin")) == []


def test_a_kind_that_does_not_exist_says_which_ones_do(client):
    body = suggest(client, "planet", "x", expect=404)
    assert "entity" in body["hint"] and "bucket" in body["hint"]


# ── The host list, and why it is a materialized view ─────────
#
# /api/suggest/domain must not cut the host out of every URI in the project
# on every keystroke. Measured on a real archive - 750,000 documents in one
# project - the prefix pass takes 10.1 s and the substring pass 11.7 s: a
# field that answers while somebody types cannot do that. dashboard.
# source_hosts holds one row per host (13,594 there) and is refreshed beside
# dashboard.places; the same two passes then take 1.2 ms and 9.0 ms.
#
# What is checked here is that the list is the SAME list - the speed is a
# property of the view, the correctness is a property of these rows.

def hosts_in_archive(archive, project: str, language: str) -> set[str]:
    """The hosts, derived straight from the archive, so the view is measured
    against the table it is built from rather than against itself."""
    with archive.connect() as conn:
        rows = conn.execute(
            "SELECT DISTINCT substring(s.text_uri from "
            "'^[a-zA-Z][a-zA-Z0-9+.-]*://([^/:?#]+)') AS host "
            "FROM processed_data.sources s "
            "WHERE s.text_project = %s AND s.text_language = %s",
            (project, language)).fetchall()
    return {r["host"] for r in rows if r["host"]}


def test_the_host_list_is_the_archives_own_hosts(client, archive):
    offered = set(values(suggest(client, "domain", "", limit=50)))
    assert offered == hosts_in_archive(archive, ALPHA, "English")
    # And it is bound to the pair like every other list.
    assert set(values(suggest(client, "domain", "", {"project": BETA, "language": "English"},
                              limit=50))) == hosts_in_archive(archive, BETA, "English")


def test_a_host_is_offered_by_prefix_and_by_substring(client):
    assert values(suggest(client, "domain", "news")) == ["news.example.com"]
    # The substring pass runs when the prefix pass is thin, so the middle of
    # a host is findable too.
    assert "blog.example.net" in values(suggest(client, "domain", "example.net"))


def test_a_host_carries_how_many_documents_came_from_it(client):
    counted = {i["value"]: i["count"] for i in suggest(client, "domain", "", limit=50)["items"]}
    assert counted["news.example.com"] >= 1
    # Most-seen first, which is what makes the first suggestion useful.
    counts = [i["count"] for i in suggest(client, "domain", "", limit=50)["items"]]
    assert counts == sorted(counts, reverse=True)


def test_the_summary_magnifier_offers_hosts_from_the_same_list(client):
    """`anything` split the same URIs a second time and cost the same ten
    seconds; it reads the view now, and its rows are unchanged."""
    hosts = {i["value"] for i in suggest(client, "anything", "news", limit=50)["items"]
             if i["hint"] == "Host"}
    assert "news.example.com" in hosts


# ── The count means MENTIONS ────────────────────────────────────────────
#
# `assert item["count"] >= 1` was the only thing guarding this number, and it
# is true whether the query counts mentions or task/entity pairs - so a
# suggestion list that counted the wrong one passed. It reached a real
# archive, where every entity showed 1 and the list fell back to alphabetical
# order: "Apple Inc." with 4,059 mentions did not make the first twelve while
# "Apple (Brand)" with one did.
#
# The preseed now mentions Apple Inc. three times inside one task, so the two
# readings give different numbers and this test can tell them apart.

def test_the_count_is_how_often_a_name_appears_not_how_many_tasks(client):
    """Six mentions across two tasks must read as 6, not as 2.

    Asked with fold=0, the way the Buckets page asks: the folded list answers
    with the BUCKET Apple, whose count is its members' together, and the
    number under test is the member's own.

    The two numbers are the whole point. `count(*)` says 6 and
    `count(DISTINCT (task, entity))` says 2, and unless the preseed names one
    entity more than once inside a task the two are the same number here -
    so a query that used the second one would pass and then show 1 beside
    all 4,059 mentions of a company on a real archive."""
    items = suggest(client, "entity", "apple inc", fold=0)["items"]
    hit = next(i for i in items if i["value"] == "Apple Inc.")
    assert hit["count"] == 6, (
        "counted task/entity pairs instead of mentions: Apple Inc. is named "
        "three times in each of two tasks, so the pair count is 2 and the "
        "ranking dies with it")


def test_every_list_that_offers_entities_counts_them_the_same_way(client):
    """entity, entity_or_bucket and anything must agree.

    The views call `entity_or_bucket` in nine places - a rule applied to
    `entity` only would still be wrong on screen. Whatever the rule is, all
    three obey it."""
    counts = {}
    for kind in ("entity", "entity_or_bucket", "anything"):
        # By LABEL: entity_or_bucket sends "Apple Inc. (Company)" as the
        # value, because a bare name there would resolve to the bucket.
        items = suggest(client, kind, "apple inc", fold=0)["items"]
        hit = next((i for i in items if i["label"] == "Apple Inc."), None)
        assert hit is not None, f"{kind} does not offer the entity at all"
        counts[kind] = hit["count"]
    assert set(counts.values()) == {6}, f"the three lists disagree: {counts}"


# ── A source is named by its host, never by its identifier ──────────────
#
# `processed_data.sources.text_name` is meant to be the document's title and
# on a real archive it can be an identifier in every row: `src_1416664` or a
# 32-character checksum, all distinct. A list that reads that field alone
# offers one checksum per document, each with the count 1 - and typing
# "vogue" matches nothing, though 44 documents come from vogue.com.
#
# Nothing here could catch that while every preseeded document had a readable
# name: the two spellings agreed on this data and only disagreed on real
# data. Two documents in the preseed now carry an identifier instead
# (preseed.py, tasks alpha:5 and alpha:6), so the two spellings differ here
# too and these assertions can fail.

IDENTIFIER = re.compile(sqlbuild.MACHINE_NAME_REGEX, re.I)


def test_a_source_suggestion_offers_the_host_and_counts_documents(client):
    """Typing part of a host offers the host, with how many documents it has."""
    items = suggest(client, "source", "news.example")["items"]
    assert [(i["value"], i["hint"]) for i in items] == [("news.example.com", "host")]
    # DOCUMENTS, not 1: two of the preseed's Alpha documents come from this
    # host. A per-document row would have said 1 here and stayed at 1 for
    # every host in the archive.
    assert items[0]["count"] == 2


def test_a_source_identifier_is_never_offered(client):
    """The two documents named like the archive names them stay out of it."""
    assert values(suggest(client, "source", "src_14")) == []
    assert values(suggest(client, "source", "8846")) == []
    # Their host is offered instead, so the documents are still reachable.
    assert "blog.example.net" in values(suggest(client, "source", "blog"))

    # And nothing in the whole list looks like an identifier.
    for term in ("", "s", "e", "example", "src"):
        for value in values(suggest(client, "source", term, limit=50)):
            assert not IDENTIFIER.match(value), f"{term!r} offered {value!r}"


def test_a_document_that_has_a_real_name_still_carries_it(client):
    """The host does not replace a title where the extraction produced one -
    it stands in only where the name is an identifier."""
    items = suggest(client, "source", "inside")["items"]
    assert [(i["value"], i["hint"]) for i in items] == [
        ("Inside the new battery", "news.example.com")]


# ── The bucket that is being edited does not get its own members back ────
#
# The Buckets page asks with `?bucket=`, and the member list is on the same
# screen, directly above the field. Offering what is already in it spends one
# of a handful of rows to say what the reader can already see. `/api/buckets/vocabulary` does the same for the
# other kinds (tests/flow/test_bucket_kinds.py).

def test_the_bucket_being_edited_is_not_offered_its_own_members(client, conn):
    row = conn.execute(
        "SELECT bigint_id FROM dashboard.buckets"
        " WHERE text_name = 'Apple' AND text_project = %s", (ALPHA,)).fetchone()
    assert row, "the preseed's Apple bucket"

    # Unfolded, the way the Buckets page asks: the members themselves.
    everything = values(suggest(client, "entity", "app", fold=0))
    assert {"Apple", "Apple Inc."} <= set(everything)

    # …and with the bucket named, its two members are gone and Apple the
    # FRUIT - which the bucket deliberately does not hold - is still there.
    # …and with the bucket named, exactly its two members are gone. Apple the
    # FRUIT stays: the bucket deliberately does not hold it, and a member is
    # name AND type (dashboard.bucket_members), so the two Apples are two
    # different values.
    left = [(i["value"], i["hint"]) for i in
            suggest(client, "entity", "app", fold=0, bucket=row["bigint_id"])["items"]]
    assert left == [("Apple", "Fruit")]
