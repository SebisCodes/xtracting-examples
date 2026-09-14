"""What robots.txt decides, before anything is fetched.

The verdict is a feature, not a technicality: it is the pill on the Sources
overview, it is why "Enable" is refused on a forbidden list page, and it is the
difference between a customer's crawler being tolerated and being blocked.
"""

from __future__ import annotations


def test_an_allowed_list_records_the_verdict_and_the_delay(crawler):
    source_id = crawler.add_source("estate allowed", crawler.site.estate_list,
                                   text_mode="selected")
    result = crawler.crawl(source_id)

    assert result.status == "OK"
    target = crawler.target(source_id)
    assert target["bool_robots_allowed"] is True
    # The site asks for a second between requests, and that is recorded so the
    # overview can show it without crawling again.
    assert target["float_robots_crawl_delay"] == 1.0
    assert target["date_robots_checked"] is not None


def test_the_crawl_delay_raises_the_politeness_delay(crawler):
    """The source says zero seconds; robots.txt says one. The larger wins - the
    source's setting is a floor, never a ceiling."""
    source_id = crawler.add_source("estate delay", crawler.site.estate_list,
                                   text_mode="all_except_rejected",
                                   integer_max_new_per_run=3)
    crawler.crawl(source_id)
    assert crawler.waits and all(wait >= 1.0 for wait in crawler.waits)


def test_a_forbidden_list_page_is_skipped_and_never_fetched(crawler):
    """`Disallow: /*sort=` on the cars host. The run is SKIPPED - which is a
    result, not a failure - and the page is not requested at all."""
    forbidden = crawler.site.cars + "/de/autos/alle-marken?sort=price"
    source_id = crawler.add_source("cars sorted", forbidden,
                                   text_mode="all_except_rejected")
    result = crawler.crawl(source_id)

    assert result.status == "SKIPPED"
    assert "forbids" in result.error
    assert not crawler.site.fetched("cars", "sort=price")
    assert crawler.target(source_id)["bool_robots_allowed"] is False

    run = crawler.runs(source_id)[-1]
    assert run["text_status"] == "SKIPPED"
    assert run["integer_pages"] == 0


def test_an_unreadable_robots_txt_forbids_the_whole_host(crawler):
    """5xx is not "there are no rules", it is "I cannot say which rules apply".
    Nothing is fetched from that host at all."""
    source_id = crawler.add_source("broken host", crawler.site.broken + "/list",
                                   text_mode="all_except_rejected")
    result = crawler.crawl(source_id)

    assert result.status == "SKIPPED"
    assert "503" in result.error
    assert crawler.site.paths("broken") == ["/robots.txt"]
    assert crawler.documents(source_id) == []


def test_pagination_forbidden_reads_the_first_page_and_says_so(crawler):
    """`Disallow: /*?*ep=` on the estate host: page one is allowed, page two is
    not. The list is read once, and the run says why there is no more."""
    source_id = crawler.add_source("estate paging", crawler.site.estate_list,
                                   text_mode="all_except_rejected",
                                   integer_max_new_per_run=1)
    result = crawler.crawl(source_id)

    assert len(result.pages) == 1
    assert any("robots.txt forbids page 2" in warning for warning in result.warnings)
    assert not crawler.site.fetched("estate", "ep=2")


def test_switching_robots_off_needs_a_reason_and_then_fetches(crawler):
    """The CHECK in the schema refuses an override without a written reason -
    ignoring robots.txt is a legal decision, not a technical one. With the
    reason, the forbidden list page is read."""
    forbidden = crawler.site.cars + "/de/autos/alle-marken?sort=price"
    source_id = crawler.add_source(
        "cars override", forbidden, text_mode="all_except_rejected",
        bool_respect_robots=False,
        text_override_reason="written permission from the site's operator, "
                             "ticket 4711", integer_max_new_per_run=2)
    result = crawler.crawl(source_id)

    assert result.status == "OK"
    assert crawler.site.fetched("cars", "sort=price")
