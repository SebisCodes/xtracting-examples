"""One real crawl, on request, by hand - and the submission that follows it.

WHAT THIS FEATURE IS FOR. "Test this configuration" fetches the list page and
shows what it found; it writes nothing and sends nothing, which is what makes it
safe to press twenty times while somebody works out a pattern. It cannot answer
the question that comes next: would a document from this page actually be
accepted, extracted and archived?

So this one really crawls, really submits and really costs. The three things
worth pinning are therefore about restraint, not about capability:

  * it sends AT MOST what was asked for, whatever the watchlist's own cap says
  * it sends ONLY its own documents, never the backlog that happens to be
    waiting under the same key
  * it runs while the scheduled crawl is switched off, because a run somebody
    just asked for and is watching is not the crawler running
"""

from __future__ import annotations

from app import db, manualrun

from conftest import TEST_PROJECT


def ask(crawler, source_id: int, *, wanted: int = 1, project: str = "") -> int:
    with db.connect(crawler.dsn) as conn:
        row = db.fetch_one(conn, """
            INSERT INTO scraper_config.manual_runs
                  (bigint_fk_source, text_project_id, integer_wanted)
            VALUES (%s, %s, %s) RETURNING bigint_id
        """, (source_id, project, wanted))
        conn.commit()
    return int(row["bigint_id"])


def state(crawler, run_id: int) -> dict:
    with db.connect(crawler.dsn) as conn:
        return db.fetch_one(conn, "SELECT * FROM scraper_config.manual_runs "
                                  "WHERE bigint_id = %s", (run_id,))


def estate(crawler, **fields):
    return crawler.add_source(
        "manual", crawler.site.estate_list, text_mode="selected",
        accept=[crawler.site.estate + "/rent/40012300"], **fields)


def test_one_document_is_crawled_and_submitted_at_once(crawler):
    source_id = estate(crawler)
    run_id = ask(crawler, source_id)

    assert manualrun.tick(crawler.cfg, crawler.submitter) is True

    run = state(crawler, run_id)
    assert run["text_status"] == "DONE"
    documents = run["json_result"]["documents"]
    assert len(documents) == 1
    # SENT, not PENDING: the person is watching, and "it is in the queue, the
    # next round will take it" is not an answer to "did it go?".
    assert documents[0]["status"] == "SENT"
    assert documents[0]["project_id"] == TEST_PROJECT


def test_the_watchlists_own_cap_does_not_apply(crawler):
    """A watchlist set to 25 documents a run must not turn one press of a
    button into 25 paid extractions."""
    source_id = estate(crawler, integer_max_new_per_run=100)
    run_id = ask(crawler, source_id, wanted=1)

    manualrun.tick(crawler.cfg, crawler.submitter)

    assert len(state(crawler, run_id)["json_result"]["documents"]) == 1
    assert len(crawler.queue(source_id)) == 1


def test_the_backlog_is_not_swept_up_with_it(crawler):
    """The button promised one page. Everything else that happens to be waiting
    under the same key stays waiting - the ordinary round will take it."""
    other = crawler.add_source(
        "manual backlog", crawler.site.estate_list, text_mode="selected",
        accept=[crawler.site.estate + "/rent/40012301"])
    crawler.crawl(other)
    waiting = crawler.queue(other)
    assert waiting and {item["text_status"] for item in waiting} == {"PENDING"}

    source_id = estate(crawler)
    ask(crawler, source_id)
    manualrun.tick(crawler.cfg, crawler.submitter)

    still_waiting = crawler.queue(other)
    assert {item["text_status"] for item in still_waiting} == {"PENDING"}


def test_a_page_that_has_nothing_new_says_why_rather_than_nothing(crawler):
    """"0 documents" with no sentence is the worst outcome this can produce: it
    looks like a fault and is usually the system working."""
    source_id = estate(crawler)
    crawler.crawl(source_id)          # everything collected already

    run_id = ask(crawler, source_id)
    manualrun.tick(crawler.cfg, crawler.submitter)

    run = state(crawler, run_id)
    assert run["text_status"] == "DONE"
    assert run["json_result"]["documents"] == []
    assert "already been collected" in run["json_result"]["reason"]


def test_a_page_with_no_project_is_refused_with_something_to_do(crawler):
    source_id = crawler.add_source(
        "manual no project", crawler.site.estate_list, text_mode="selected",
        accept=[crawler.site.estate + "/rent/40012300"], projects=[])
    run_id = ask(crawler, source_id)

    manualrun.tick(crawler.cfg, crawler.submitter)

    run = state(crawler, run_id)
    assert run["text_status"] == "FAILED"
    assert "not assigned to a project" in run["text_error"]


def test_one_project_of_several_can_be_tried_on_its_own(crawler):
    """Trying a configuration must not cost one extraction per project when the
    question is about one of them."""
    second = "_crawlertest_project_manual"
    source_id = crawler.add_source(
        "manual two projects", crawler.site.estate_list, text_mode="selected",
        accept=[crawler.site.estate + "/rent/40012300"],
        projects=[TEST_PROJECT, second])

    run_id = ask(crawler, source_id, project=second)
    manualrun.tick(crawler.cfg, crawler.submitter)

    documents = state(crawler, run_id)["json_result"]["documents"]
    assert len(documents) == 1
    assert documents[0]["project_id"] == second
    # And the other project has not been charged for anything.
    assert not [item for item in crawler.queue(source_id)
                if item["text_project_id"] == TEST_PROJECT]


def test_nothing_waiting_means_nothing_taken(crawler):
    assert manualrun.tick(crawler.cfg, crawler.submitter) is False


def test_a_run_whose_crawler_died_is_failed_not_left_running(crawler):
    """A row stuck at RUNNING is a progress line that never moves. It becomes a
    failure with a sentence, so the panel says something rather than spinning."""
    source_id = estate(crawler)
    run_id = ask(crawler, source_id)
    with db.connect(crawler.dsn) as conn:
        db.execute(conn, """
            UPDATE scraper_config.manual_runs
               SET text_status = 'RUNNING', date_started = NOW() - INTERVAL '2 hours'
             WHERE bigint_id = %s
        """, (run_id,))
        conn.commit()
        assert manualrun.reap_stuck(conn) == 1

    run = state(crawler, run_id)
    assert run["text_status"] == "FAILED"
    assert "stopped while this run was going" in run["text_error"]
