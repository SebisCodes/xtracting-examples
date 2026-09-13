"""One page, two projects: fetched once, extracted twice.

THE FACT THIS FILE EXISTS FOR. A project decides what is extracted from a
document - its objects of interest and its perspectives - so two projects
asking different questions of the same page cannot share one extraction. The
crawler therefore fetches the page ONCE, and submits it once per project.

Which makes "have I had this address before" a question about a project. Keyed
by the address alone, the second project would be told the
address is old and would silently get nothing, and the editor's promise that a
page can serve two projects would be false. The strongest assertions here are
about the SITE'S access log on one side and the number of paid submissions on
the other: one visit, two tasks.
"""

from __future__ import annotations

from app import db

from conftest import TEST_PROJECT

SECOND_PROJECT = "_crawlertest_project_two"


def two_project_source(crawler, **fields):
    return crawler.add_source(
        "two projects", crawler.site.estate_list, text_mode="selected",
        accept=[crawler.site.estate + "/rent/40012300"],
        projects=[TEST_PROJECT, SECOND_PROJECT], **fields)


def test_one_fetch_and_one_document_per_project(crawler):
    source_id = two_project_source(crawler)
    result = crawler.crawl(source_id)

    assert result.status == "OK"
    documents = crawler.documents(source_id)
    # Twenty listings, two projects: forty documents and forty tags.
    assert len(documents) == 40
    addresses = {document["text_uri_canonical"] for document in documents}
    assert len(addresses) == 20

    # AND THE SITE SAW EACH PAGE ONCE. This is the assertion the whole design
    # rests on: the second project costs an extraction, never a second visit.
    fetched = [path for path in crawler.site.paths("estate")
               if path.startswith("/rent/4")]
    assert len(fetched) == len(set(fetched)) == 20


def test_the_queue_says_which_project_each_submission_is_for(crawler):
    source_id = two_project_source(crawler)
    crawler.crawl(source_id)

    queue = crawler.queue(source_id)
    assert len(queue) == 40
    by_project: dict[str, int] = {}
    for item in queue:
        by_project[item["text_project_id"]] = by_project.get(item["text_project_id"], 0) + 1
    assert by_project == {TEST_PROJECT: 20, SECOND_PROJECT: 20}
    # Every tag is its own: two submissions of one page are two jobs, two
    # results and two archived documents, and a shared tag could not be
    # reconciled back to either.
    assert len({item["text_tag"] for item in queue}) == 40


def test_a_project_added_later_collects_the_back_catalogue(crawler):
    """The reason the gate is per project, stated as a behaviour.

    One project has been collecting this page for a while. A second is added.
    It has never seen any of those addresses, so it gets them - all of them -
    without the first one paying for anything again.
    """
    source_id = crawler.add_source(
        "one then two", crawler.site.estate_list, text_mode="selected",
        accept=[crawler.site.estate + "/rent/40012300"],
        projects=[TEST_PROJECT])
    crawler.crawl(source_id)
    assert len(crawler.queue(source_id)) == 20

    with db.connect(crawler.dsn) as conn:
        db.execute(conn, """
            INSERT INTO scraper_config.source_projects
                  (bigint_fk_source, text_project_id, integer_order)
            VALUES (%s, %s, 1)
        """, (source_id, SECOND_PROJECT))
        conn.commit()

    crawler.crawl(source_id)

    queue = crawler.queue(source_id)
    assert len(queue) == 40
    fresh = [item for item in queue if item["text_project_id"] == SECOND_PROJECT]
    assert len(fresh) == 20
    # The first project is not charged twice for what it already has.
    old = [item for item in queue if item["text_project_id"] == TEST_PROJECT]
    assert len(old) == 20


def test_a_second_run_costs_neither_project_anything(crawler):
    source_id = two_project_source(crawler)
    crawler.crawl(source_id)
    crawler.site.reset_log()

    result = crawler.crawl(source_id)

    assert result.counts["queued"] == 0
    assert len(crawler.queue(source_id)) == 40
    assert not any(path.startswith("/rent/4")
                   for path in crawler.site.paths("estate"))


def test_a_page_with_no_project_collects_nothing_at_all(crawler):
    """Not an error, and not a crash: a page assigned to no project has
    nowhere to file what it collects, so it files nothing. The editor refuses
    to save one; this is what happens if one reaches the crawler anyway."""
    source_id = crawler.add_source(
        "no project", crawler.site.estate_list, text_mode="selected",
        accept=[crawler.site.estate + "/rent/40012300"], projects=[])

    result = crawler.crawl(source_id)

    assert result.status == "OK"
    assert crawler.documents(source_id) == []
    assert crawler.queue(source_id) == []


def test_each_project_may_be_evaluated_by_its_own_key(crawler):
    """A key belongs to one project, so the key a page uses is per project -
    and an override that names another project's key is not that project's
    business and is passed over rather than applied across the boundary."""
    source_id = two_project_source(crawler)
    with db.connect(crawler.dsn) as conn:
        db.execute(conn, """
            UPDATE scraper_config.source_projects SET text_key_prefix = %s
             WHERE bigint_fk_source = %s AND text_project_id = %s
        """, ("kkkk1111", source_id, SECOND_PROJECT))
        conn.commit()

    crawler.crawl(source_id)

    queue = crawler.queue(source_id)
    pinned = {item["text_key_prefix"] for item in queue
              if item["text_project_id"] == SECOND_PROJECT}
    default = {item["text_key_prefix"] for item in queue
               if item["text_project_id"] == TEST_PROJECT}
    assert pinned == {"kkkk1111"}
    # '' is not "no key": it is "this project's default", resolved when the row
    # is submitted so that collection carries on when one key is switched off.
    assert default == {""}
