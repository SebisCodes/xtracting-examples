"""Following a list over several pages - and knowing when to stop.

`test_crawl_rules.py` checks the four stop rules against a server built for
them. Here they are checked where they will actually be met: a list whose pages
hang off `?ep=`, whose last page links back to the first, and whose paginated
addresses robots.txt forbids.
"""

from __future__ import annotations

REASON = "written permission from the site's operator, ticket 4711"


def paging_source(crawler, **fields):
    """The estate list with robots switched off - its `?ep=` pages are
    forbidden, and the point here is the pagination, not the verdict."""
    return crawler.add_source(
        "estate paging", crawler.site.estate_list,
        text_mode="selected", accept=[crawler.site.estate + "/rent/40012300"],
        bool_respect_robots=False, text_override_reason=REASON,
        **{"integer_max_new_per_run": 1, **fields})


def test_the_pages_are_read_and_the_loop_back_ends_it(crawler):
    """Page three links back to page one. Without a stop rule the crawl walks
    in a circle until the page cap ends it - ten requests for three pages."""
    source_id = paging_source(crawler, integer_max_pages=10)
    result = crawler.crawl(source_id)

    fetched = [path for path in crawler.site.paths("estate")
               if "matching-list" in path]
    assert len(result.pages) == 3
    assert any("ep=2" in path for path in fetched)
    assert any("ep=3" in path for path in fetched)
    # Three requests, not four: the link back to page one is recognised as an
    # address already read, and is not fetched again.
    assert len(fetched) == 3
    assert any("already read" in warning for warning in result.warnings)


def test_the_page_cap_holds(crawler):
    source_id = paging_source(crawler, integer_max_pages=2)
    result = crawler.crawl(source_id)

    assert len(result.pages) == 2
    assert any("stopped after 2 pages" in warning for warning in result.warnings)
    assert not any("ep=3" in path for path in crawler.site.paths("estate"))


def test_the_learned_parameter_is_used_when_there_is_one(crawler):
    """`text_paging_param` comes out of `learn()` - the key the ticked links
    carried. It is tried before any "next" label, because it is the one that
    matched the pages somebody actually wanted."""
    source_id = paging_source(crawler, text_paging_param="ep", integer_max_pages=3)
    result = crawler.crawl(source_id)

    assert len(result.pages) == 3
    assert any("ep=2" in path for path in crawler.site.paths("estate"))


def test_pagination_can_be_switched_off(crawler):
    source_id = paging_source(crawler, bool_follow_pagination=False,
                              integer_max_pages=10)
    result = crawler.crawl(source_id)

    assert len(result.pages) == 1
    assert not any("ep=" in path for path in crawler.site.paths("estate"))


def test_the_next_link_is_never_fetched_as_a_document(crawler):
    """It is an ordinary link on the page. Collected as a listing it would be
    submitted - a list page as a document - and counted as new every run."""
    source_id = paging_source(crawler, integer_max_pages=3,
                              integer_max_new_per_run=100)
    crawler.crawl(source_id)

    documents = crawler.documents(source_id)
    assert documents
    assert not any("ep=" in document["text_uri_canonical"] for document in documents)
    assert not any("matching-list" in document["text_uri_canonical"]
                   for document in documents)


def test_robots_stops_the_pagination_without_stopping_the_run(crawler):
    """With robots respected, page one is read and page two is not - and the
    run is a success, not a failure. The banner in the editor says the same
    thing: "only page 1"."""
    source_id = crawler.add_source(
        "estate paging robots", crawler.site.estate_list, text_mode="selected",
        accept=[crawler.site.estate + "/rent/40012300"], integer_max_pages=5,
        integer_max_new_per_run=1)
    result = crawler.crawl(source_id)

    assert result.status == "OK"
    assert len(result.pages) == 1
    assert any("forbids page 2" in warning for warning in result.warnings)
