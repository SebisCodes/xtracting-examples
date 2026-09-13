"""Submitting: what goes out, and what happens when it does not.

Every branch here costs money if it is wrong. A 402 counted as a failure
retires healthy documents after three rounds; a "202 without a jobId" that is
retried pays twice for certain; a batch that is silently dropped is a document
nobody ever misses.
"""

from __future__ import annotations

import pytest


@pytest.fixture()
def queued(crawler):
    """Three documents in the queue, PENDING, ready to go out."""
    source_id = crawler.add_source(
        "estate submit", crawler.site.estate_list, text_mode="selected",
        accept=[crawler.site.estate + "/rent/40012300"],
        integer_max_new_per_run=3)
    crawler.crawl(source_id)
    assert len(crawler.queue(source_id)) == 3
    return source_id


def statuses(crawler, source_id):
    return sorted(item["text_status"] for item in crawler.queue(source_id))


def test_a_batch_goes_out_and_comes_back_with_a_job(crawler, queued):
    assert crawler.submit() == 3

    queue = crawler.queue(queued)
    assert statuses(crawler, queued) == ["SENT"] * 3
    assert all(item["text_job_id"] for item in queue)
    assert all(item["date_sent"] for item in queue)
    # The API saw one batch with three tasks, each carrying its own tag.
    assert len(crawler.api.state.submissions) == 1
    assert len({task["tag"] for task in crawler.api.state.submissions[0]}) == 3


def test_the_task_carries_the_address_as_source(crawler, queued):
    """`source` is what the archive stores as `text_source_uri` - and that is
    the second dedupe gate for every later run."""
    crawler.submit()
    task = crawler.api.state.submissions[0][0]
    assert task["source"].startswith(crawler.site.estate)
    assert set(task) == {"content", "source", "tag"}


def test_backpressure_puts_everything_back_without_counting_an_attempt(crawler, queued):
    """402 is a state, not a fault. Counting it would retire three healthy
    documents because an account was empty for an hour."""
    crawler.api.state.next_submit_status = 402

    assert crawler.submit() == 0

    assert statuses(crawler, queued) == ["PENDING"] * 3
    assert all(item["integer_attempts"] == 0 for item in crawler.queue(queued))
    assert all("balance" in (item["text_error"] or "")
               for item in crawler.queue(queued))


def test_backpressure_pauses_the_key_and_says_so_in_a_run_row(crawler, queued):
    crawler.api.state.next_submit_status = 429
    crawler.submit()

    assert crawler.submitter.paused("_crawlertest")
    # The next round does not even try - and that is visible, not silent.
    assert crawler.submit() == 0
    assert len(crawler.api.state.submissions) == 1

    rows = crawler.rows("""
        SELECT text_status, text_message FROM monitoring.scraper_runs
         WHERE text_name = 'submit-_crawlertest' ORDER BY date_added
    """)
    assert rows and rows[-1]["text_status"] == "BACKPRESSURE"
    assert "429" in rows[-1]["text_message"]


def test_a_pause_ends_and_the_documents_go_out_untouched(crawler, queued):
    crawler.api.state.next_submit_status = 402
    crawler.submit()
    crawler.submitter.pause("_crawlertest", 0)   # as if the fifteen minutes were up

    assert crawler.submit() == 3
    assert statuses(crawler, queued) == ["SENT"] * 3


def test_a_permanent_refusal_fails_the_batch_with_its_reason(crawler, queued):
    crawler.api.state.next_submit_status = 400
    crawler.submit()

    assert statuses(crawler, queued) == ["FAILED"] * 3
    assert all("400" in (item["text_error"] or "")
               for item in crawler.queue(queued))


def test_a_rejected_key_is_recorded_on_the_target(crawler, queued):
    """401 is not about this batch, it is about the key - and the Sources page
    has to be able to say which sources are affected."""
    crawler.api.state.next_submit_status = 401
    crawler.submit()

    assert statuses(crawler, queued) == ["FAILED"] * 3
    assert "401" in crawler.target(queued)["text_key_status"]


def test_temporary_failures_are_retried_and_then_it_works(crawler, queued):
    crawler.api.state.next_submit_status = [500, 502]

    assert crawler.submit() == 3
    assert statuses(crawler, queued) == ["SENT"] * 3
    assert len(crawler.api.state.submissions) == 3


def test_a_202_without_a_job_id_is_failed_not_retried(crawler, queued):
    """Submitted, billed, and not attributable. Retrying would pay a second
    time; the items are marked FAILED so a person sees it."""
    crawler.api.state.next_submit_status = 202
    crawler.submit()

    assert statuses(crawler, queued) == ["FAILED"] * 3
    assert all("cannot be attributed" in (item["text_error"] or "")
               for item in crawler.queue(queued))
    assert len(crawler.api.state.submissions) == 1


def test_the_split_is_reconciled_back_to_archived(crawler, queued):
    """The whole loop in one test: submitted, split by the platform, archived
    by the collector, and recognised again here by the tag prefix."""
    crawler.api.state.split_at = 40           # every document becomes several tasks
    crawler.submit()

    job = list(crawler.api.jobs.values())[0]
    assert job["totalTasks"] > 3
    assert all("-" in task["tag"] for task in job["tasks"])

    written = crawler.archive_with_collector(job)
    assert written > 0

    assert crawler.reconcile() == 3
    assert statuses(crawler, queued) == ["ARCHIVED"] * 3
    assert all(item["date_archived"] for item in crawler.queue(queued))

    # And the documents now carry the three identity columns the archive uses.
    documents = crawler.documents(queued)
    assert all(document["text_project"] == "_crawlertest" for document in documents)
    assert all(document["text_task_id"] for document in documents)
    assert all(document["text_language"] == "English" for document in documents)


def test_an_item_left_in_sending_goes_out_again(crawler, queued):
    """A sender that dies between claiming an item and recording its outcome
    would otherwise leave it SENDING forever - not failed, not sent, and not
    visible as either."""
    from app import db
    with db.connect(crawler.dsn) as conn:
        db.execute(conn, """
            UPDATE scraper.submit_queue
               SET text_status = 'SENDING', integer_attempts = 1,
                   date_updated = NOW() - INTERVAL '2 hours'
             WHERE bigint_fk_source = %s
        """, (queued,))
        conn.commit()

    assert crawler.submit() == 3
    assert statuses(crawler, queued) == ["SENT"] * 3
    # The attempt it already cost is kept: a document that crashes the sender
    # twice must still run out of attempts rather than loop.
    assert all(item["integer_attempts"] >= 2 for item in crawler.queue(queued))


def test_reconcile_leaves_what_has_not_arrived_alone(crawler, queued):
    crawler.submit()
    assert crawler.reconcile() == 0
    assert statuses(crawler, queued) == ["SENT"] * 3
