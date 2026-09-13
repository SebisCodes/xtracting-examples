"""Not paying twice for the same address.

Two gates, and both are needed. `scraper.seen_urls` is what this crawler has
fetched - fast, and keyed by a hash so that a hundred links cost one round
trip. `processed_data.tasks.text_source_uri` is what has ever been extracted:
by hand, by an earlier installation, or by a second source that happens to list
the same page. Without the second gate the same document is submitted and
billed twice, and nothing about the result looks wrong.
"""

from __future__ import annotations


def estate_source(crawler, **fields):
    return crawler.add_source(
        "estate dedupe", crawler.site.estate_list, text_mode="selected",
        accept=[crawler.site.estate + "/rent/40012300"], **fields)


def test_an_address_already_in_the_archive_is_not_fetched(crawler):
    already = crawler.site.estate + "/rent/40012305"
    crawler.archive_task(tag="xs_0_deadbeefcafe", source_uri=already)

    source_id = estate_source(crawler)
    crawler.crawl(source_id)

    documents = {document["text_uri_canonical"]
                 for document in crawler.documents(source_id)}
    assert already not in documents
    assert len(documents) == 19
    # Not merely un-queued: not requested at all. The point of the gate is the
    # request that does not happen.
    assert not crawler.site.fetched("estate", "/rent/40012305")


def test_a_second_source_on_the_same_page_does_not_collect_it_again(crawler):
    """Two lists of the same site can carry the same listing. The first one to
    fetch it wins; the second finds it in seen_urls."""
    first = estate_source(crawler)
    crawler.crawl(first)
    fetched_first = len(crawler.documents(first))
    assert fetched_first == 20

    second = crawler.add_source(
        "estate dedupe two", crawler.site.estate_list, text_mode="selected",
        accept=[crawler.site.estate + "/rent/40012300"])
    crawler.site.reset_log()
    result = crawler.crawl(second)

    assert crawler.documents(second) == []
    assert result.counts["queued"] == 0
    assert not any(path.startswith("/rent/4")
                   for path in crawler.site.paths("estate"))


def test_the_archive_gate_holds_even_when_seen_urls_is_empty(crawler):
    """`seen_urls` is a mirror and can be younger than the archive - after a
    restore, or between two runs. The archive is the authority."""
    source_id = estate_source(crawler)
    crawler.crawl(source_id)

    with_addresses = [document["text_uri_canonical"]
                      for document in crawler.documents(source_id)]
    crawler.rows("DELETE FROM scraper.seen_urls WHERE bigint_fk_source = %s "
                 "RETURNING 1", (source_id,))
    for address in with_addresses[:3]:
        crawler.archive_task(tag=f"xs_0_{abs(hash(address)):012x}"[:29],
                             source_uri=address)
    crawler.site.reset_log()

    crawler.crawl(source_id)

    fetched = [path for path in crawler.site.paths("estate")
               if path.startswith("/rent/4")]
    assert len(fetched) == 17


def test_the_dedupe_is_about_the_address_not_the_content(crawler):
    """A page with a date or a counter in it has a different hash every time.
    Deduplicating on the content would submit it again on every run - a loop
    that does not stop and that looks like diligence from outside."""
    source_id = estate_source(crawler)
    crawler.crawl(source_id)
    crawler.site.change()          # every listing now has different content

    result = crawler.crawl(source_id)

    assert result.counts["queued"] == 0
    assert len(crawler.documents(source_id)) == 20
