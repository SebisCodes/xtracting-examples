"""Selected mode: only what a pattern matches.

The homegate-shaped case, and the one most sources will be. Somebody ticked
twenty listings in the picker, `learn()` made `/rent/#` of them, and from then
on the crawl fetches those twenty and nothing else - not the sibling city, not
the imprint, not the login.

The strongest assertions here are about what was NOT fetched. A crawler is
judged by the access log of the site it visits, not by its own counters.
"""

from __future__ import annotations


def crawl_estate(crawler, **fields):
    source_id = crawler.add_source(
        "estate selected", crawler.site.estate_list, text_mode="selected",
        accept=[crawler.site.estate + "/rent/40012300"], **fields)
    return source_id, crawler.crawl(source_id)


def test_the_twenty_listings_become_twenty_documents(crawler):
    source_id, result = crawl_estate(crawler)

    assert result.status == "OK"
    assert result.counts["accepted"] == 20
    documents = crawler.documents(source_id)
    assert len(documents) == 20
    assert all(document["text_kind"] == "page" for document in documents)
    assert all("/rent/4001" in document["text_uri_canonical"] for document in documents)


def test_every_document_is_queued_with_its_own_tag(crawler):
    source_id, _ = crawl_estate(crawler)
    queue = crawler.queue(source_id)
    assert len(queue) == 20
    assert {item["text_status"] for item in queue} == {"PENDING"}
    assert all(item["text_tag"].startswith(f"xs_{source_id}_") for item in queue)
    assert len({item["text_tag"] for item in queue}) == 20
    # The queue carries the address and the hash beside the pointer, so submit
    # and reconcile never have to touch the hypertable to do their work.
    assert all(item["text_source_uri"] and item["text_content_hash"] for item in queue)


def test_the_text_is_the_reading_of_the_page_not_its_furniture(crawler):
    source_id, _ = crawl_estate(crawler)
    document = crawler.documents(source_id)[0]
    assert "Drei Zimmer" in document["text_content"]
    assert "Impressum" not in document["text_content"]
    assert document["integer_char_count"] == len(document["text_content"])
    assert document["text_format"] == "plain"
    assert document["text_title"]


def test_nothing_outside_the_pattern_is_fetched(crawler):
    """The access log of the site, not our own counters. `/impressum` is on
    every page of the list; fetching it once would be a wasted request, and
    submitting it would be a paid one."""
    crawl_estate(crawler)
    fetched = crawler.site.paths("estate")

    assert not any(path.startswith("/impressum") for path in fetched)
    assert not any(path.startswith("/agb") for path in fetched)
    assert not any(path.startswith("/datenschutz") for path in fetched)
    assert not any("city-basel" in path for path in fetched)
    assert not any("/de/d/" in path for path in fetched)
    assert not any(path.startswith("/challenge") for path in fetched)
    # Exactly the list page and the twenty listings, plus robots.txt.
    assert len([path for path in fetched if path.startswith("/rent/4")]) == 20


def test_page_two_is_not_fetched_because_robots_forbids_it(crawler):
    crawl_estate(crawler)
    assert not crawler.site.fetched("estate", "ep=")


def test_a_second_run_fetches_nothing_and_queues_nothing(crawler):
    """The address is the anchor. A list that has not changed produces no
    second document and no second bill."""
    source_id, _ = crawl_estate(crawler)
    crawler.site.reset_log()

    second = crawler.crawl(source_id)

    assert second.status == "OK"
    assert second.counts["queued"] == 0
    assert len(crawler.documents(source_id)) == 20
    # The list page is read again - that is the point of a run - but not one
    # listing.
    assert not any(path.startswith("/rent/4") for path in crawler.site.paths("estate"))


def test_the_run_row_says_what_happened(crawler):
    source_id, _ = crawl_estate(crawler)
    run = crawler.runs(source_id)[-1]
    assert run["text_status"] == "OK"
    assert run["integer_pages"] == 1
    assert run["integer_accepted"] == 20
    assert run["integer_submitted"] == 20
    assert run["integer_ms"] > 0
    assert run["text_key_label"] == "_crawlertest"


def test_the_cap_on_new_addresses_leaves_the_rest_for_the_next_run(crawler):
    """A first run against a list of ten thousand should not fetch ten thousand
    pages before anyone can see whether the configuration is right."""
    source_id, result = crawl_estate(crawler, integer_max_new_per_run=5)
    assert len(crawler.documents(source_id)) == 5
    assert any("takes 5 of them" in warning for warning in result.warnings)

    crawler.crawl(source_id)
    assert len(crawler.documents(source_id)) == 10
