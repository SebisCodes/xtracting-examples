"""Exact mode: watch these addresses, and tell me when they change.

The one mode where the content decides. Everywhere else the address is the
anchor - a page with a date or a visitor counter in it would otherwise be
submitted, and billed, on every run. Here a change IS the news, which is why
the editor turns `bool_resubmit_on_change` on with this mode and leaves it off
everywhere else.
"""

from __future__ import annotations

import pytest


@pytest.fixture()
def watched(crawler):
    """One address, watched. The list page is only where it was configured."""
    address = crawler.site.estate + "/rent/40012307"
    source_id = crawler.add_source(
        "estate exact", crawler.site.estate_list, text_mode="exact",
        monitor=[address], bool_resubmit_on_change=True)
    return source_id, address


def test_the_monitored_address_is_the_document(crawler, watched):
    source_id, address = watched
    crawler.crawl(source_id)

    documents = crawler.documents(source_id)
    assert len(documents) == 1
    assert documents[0]["text_uri_canonical"] == address
    # Nothing found on the list page is collected in this mode.
    assert len([path for path in crawler.site.paths("estate")
                if path.startswith("/rent/4")]) == 1


def test_an_unchanged_page_is_fetched_again_and_submitted_once(crawler, watched):
    """It has to be fetched - that is the only way to see whether it changed -
    and it must not be submitted a second time."""
    source_id, _ = watched
    crawler.crawl(source_id)
    crawler.site.reset_log()

    result = crawler.crawl(source_id)

    assert any(path.startswith("/rent/4") for path in crawler.site.paths("estate"))
    assert result.counts["unchanged"] == 1
    assert result.counts["queued"] == 0
    assert len(crawler.documents(source_id)) == 1
    assert len(crawler.queue(source_id)) == 1


def test_a_changed_page_becomes_a_second_document(crawler, watched):
    source_id, _ = watched
    crawler.crawl(source_id)
    crawler.site.change()

    result = crawler.crawl(source_id)

    assert result.counts["queued"] == 1
    documents = crawler.documents(source_id)
    assert len(documents) == 2
    # Two versions of one address, each with its own hash and its own tag:
    # every version is kept, which is what makes a diff view possible later.
    assert documents[0]["text_content_hash"] != documents[1]["text_content_hash"]
    assert documents[0]["text_uri_canonical"] == documents[1]["text_uri_canonical"]
    assert len({item["text_tag"] for item in crawler.queue(source_id)}) == 2


def test_the_change_date_moves_only_when_the_content_moves(crawler, watched):
    source_id, address = watched
    crawler.crawl(source_id)
    first = crawler.one("""
        SELECT date_last_change, integer_times_seen FROM scraper.seen_urls
         WHERE text_uri_canonical = %s
    """, (address,))

    crawler.crawl(source_id)
    unchanged = crawler.one("""
        SELECT date_last_change, integer_times_seen FROM scraper.seen_urls
         WHERE text_uri_canonical = %s
    """, (address,))
    assert unchanged["date_last_change"] == first["date_last_change"]
    assert unchanged["integer_times_seen"] > first["integer_times_seen"]

    crawler.site.change()
    crawler.crawl(source_id)
    changed = crawler.one("""
        SELECT date_last_change FROM scraper.seen_urls
         WHERE text_uri_canonical = %s
    """, (address,))
    assert changed["date_last_change"] > first["date_last_change"]


def test_without_the_switch_a_change_is_noticed_and_not_resubmitted(crawler):
    """The default, and the safe one: the page is known, so nothing is fetched
    at all - which is also why a source that is meant to watch for changes has
    to say so."""
    address = crawler.site.estate + "/rent/40012308"
    source_id = crawler.add_source(
        "estate exact quiet", crawler.site.estate_list, text_mode="exact",
        monitor=[address], bool_resubmit_on_change=False)
    crawler.crawl(source_id)
    crawler.site.change()
    crawler.site.reset_log()

    result = crawler.crawl(source_id)

    assert result.counts["queued"] == 0
    assert not any(path.startswith("/rent/4") for path in crawler.site.paths("estate"))
    assert len(crawler.documents(source_id)) == 1
