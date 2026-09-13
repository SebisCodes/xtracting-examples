"""Rendered mode: the list that only exists after the scripts have run.

The fedlex-shaped case. The delivered HTML has an empty `<main>`; the
sixty-two links appear fifty milliseconds later. Over plain HTTP there is
nothing to collect, and the run says so in the sentence the editor shows -
"try Rendered mode" - rather than reporting an empty list as a success.

Skipped without a browser, never failed: the slim image has no Chromium on
purpose, and a suite that goes red there would train people to ignore it.
"""

from __future__ import annotations

import pytest

from crawlkit.fetch import playwright_available

pytestmark = pytest.mark.skipif(not playwright_available(),
                                reason="no browser in this environment")


def law_source(crawler, engine: str, **fields):
    return crawler.add_source(
        f"law {engine}", crawler.site.law_search, text_mode="all_except_rejected",
        text_engine=engine, integer_max_new_per_run=2, integer_max_pages=1,
        **fields)


def test_over_http_the_list_is_empty_and_the_run_says_what_to_do(crawler):
    source_id = law_source(crawler, "http")
    result = crawler.crawl(source_id)

    assert result.status == "OK"
    assert result.counts["links"] == 0
    assert any("try Rendered mode" in warning.replace("Rendered", "Rendered")
               for warning in result.warnings)
    assert crawler.documents(source_id) == []


def test_with_a_browser_the_same_page_has_sixty_two_links(crawler):
    source_id = law_source(crawler, "playwright",
                           integer_wait_after_load_ms=200)
    result = crawler.crawl(source_id)

    assert result.status == "OK"
    assert result.engine_used == "playwright"
    assert result.pages[0].links == 62
    # Sixty of them are legislation, one is the imprint, one is page two.
    assert result.counts["accepted"] == 60
    assert len(crawler.documents(source_id)) == 2      # the cap for this run


def test_the_engine_used_is_recorded_so_an_empty_result_can_be_explained(crawler):
    """"Nothing found" means something different with a browser than without
    one, and the run row is where that difference is kept."""
    http_id = law_source(crawler, "http")
    crawler.crawl(http_id)
    assert crawler.runs(http_id)[-1]["integer_links"] == 0
