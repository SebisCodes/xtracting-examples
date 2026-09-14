"""Everything except what I reject.

The mode for a list whose entries have no shape in common - and the one where
the implicit legal list earns its keep: an imprint, terms and a privacy page
are on every page of every site and are interesting on none of them.
"""

from __future__ import annotations


def add(crawler, **fields):
    return crawler.add_source("estate all except", crawler.site.estate_list,
                              text_mode="all_except_rejected",
                              integer_max_new_per_run=100, **fields)


def test_legal_links_are_dropped_by_default(crawler):
    source_id = add(crawler)
    result = crawler.crawl(source_id)

    rejected = {link.url: link.by for link in result.links if not link.accepted_by_config}
    assert any(url.endswith("/impressum") and by == "legal"
               for url, by in rejected.items())
    assert not any(path.startswith("/impressum")
                   for path in crawler.site.paths("estate"))


def test_switching_the_legal_list_off_collects_them(crawler):
    """Some customers want the imprint - it names the operator. The switch is
    theirs, and the consequence is visible: three more documents."""
    with_legal = add(crawler, bool_drop_legal_links=False)
    result = crawler.crawl(with_legal)

    accepted = set(result.accepted)
    assert any(url.endswith("/impressum") for url in accepted)
    assert any(path.startswith("/impressum")
               for path in crawler.site.paths("estate"))


def test_a_reject_pattern_keeps_the_sibling_list_out(crawler):
    """The link to the same search in another city is a list, not a listing.
    In this mode it would be collected without a reject pattern."""
    source_id = add(crawler, reject=[crawler.site.estate +
                                     "/rent/apartment/city-basel/matching-list"])
    result = crawler.crawl(source_id)

    assert not any("city-basel" in url for url in result.accepted)
    assert not any("city-basel" in path for path in crawler.site.paths("estate"))


def test_assets_and_pagination_are_never_documents(crawler):
    source_id = add(crawler)
    result = crawler.crawl(source_id)
    classes = {link.url: link.cls for link in result.links}
    assert "pagination" in classes.values()
    assert not any(link.accepted_by_config for link in result.links
                   if link.cls in ("pagination", "asset", "list_self"))


def test_a_link_robots_forbids_is_not_fetched_even_when_accepted(crawler):
    """`/search-srp-new/...` is accepted by this mode and forbidden by
    robots.txt. The rules of the source and the rules of the site are two
    different questions, and the site's answer wins."""
    source_id = add(crawler, bool_drop_legal_links=False)
    crawler.crawl(source_id)

    assert not any("-srp-new" in path for path in crawler.site.paths("estate"))
    seen = crawler.rows("""
        SELECT text_uri_canonical FROM scraper.seen_urls
         WHERE bigint_fk_source = %s AND text_uri_canonical LIKE %s
    """, (source_id, "%-srp-new%"))
    # Recorded as seen, or it would be counted as new on every single run.
    assert seen


def test_an_exact_reject_answers_the_question_about_one_address(crawler):
    """The picker's "exclude just that address?" - one address, no pattern."""
    one = crawler.site.estate + "/rent/40012305"
    source_id = add(crawler, exact_reject=[one])
    result = crawler.crawl(source_id)

    assert one not in result.accepted
    assert [link.by for link in result.links if link.url == one] == ["exact"]
