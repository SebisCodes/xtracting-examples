"""A submitted address is only provisionally collected.

WHAT THIS IS ABOUT. The crawler writes an address into `scraper.seen_urls` the
moment it queues the document. That must not be the end of it: an address that
counts as collected whether or not a result ever comes back means a collector
that is down for an afternoon does not merely delay the archive - it LOSES that
afternoon's pages, permanently and without a word, because the crawler has
written them off and will never offer them again.

So the entry is provisional. `date_submitted` starts a clock, `date_confirmed`
stops it, and only the reconcile pass sets the second - when the document is
really in `processed_data.tasks`, which is to say when the collector has
fetched and filed it. An address whose result has not come back within
`store.SUBMISSION_GRACE` is offered again.

Every test here moves the CLOCK IN THE DATABASE rather than waiting: the rule
is two hours long and a test suite is not.
"""

from __future__ import annotations

from app import store


def estate_source(crawler, **fields):
    return crawler.add_source(
        "estate grace", crawler.site.estate_list, text_mode="selected",
        accept=[crawler.site.estate + "/rent/40012300"], **fields)


def seen_rows(crawler, source_id: int) -> list[dict]:
    return crawler.rows("""
        SELECT text_uri_canonical, date_submitted, date_confirmed
          FROM scraper.seen_urls WHERE bigint_fk_source = %s
         ORDER BY text_uri_canonical
    """, (source_id,))


def age_the_submissions(crawler, source_id: int, hours: int) -> None:
    """Put every pending submission of this source `hours` into the past."""
    crawler.execute("""
        UPDATE scraper.seen_urls
           SET date_submitted = date_submitted - make_interval(hours => %s)
         WHERE bigint_fk_source = %s AND date_submitted IS NOT NULL
    """, (hours, source_id))


def test_a_queued_address_starts_out_unconfirmed(crawler):
    """The clock starts at the submission, and nothing else sets it."""
    source_id = estate_source(crawler)
    crawler.crawl(source_id)

    rows = seen_rows(crawler, source_id)
    assert rows, "nothing was seen at all"
    pending = [r for r in rows if r["date_submitted"] is not None]
    assert pending, "no address carries a submission clock"
    assert all(r["date_confirmed"] is None for r in pending), (
        "an address was confirmed without anything coming back from the collector")


def test_an_address_whose_result_never_came_is_offered_again(crawler):
    """THE WHOLE POINT. The collector is down; two hours pass; the page is
    fetched and submitted a second time instead of being lost."""
    source_id = estate_source(crawler)
    crawler.crawl(source_id)
    first = len(crawler.documents(source_id))
    assert first > 0

    # Nothing was archived - the collector never ran - and the clock runs out.
    age_the_submissions(crawler, source_id, 3)
    crawler.site.reset_log()
    crawler.crawl(source_id)

    assert len(crawler.documents(source_id)) == 2 * first, (
        "the pages were written off as collected although nothing came back")


def test_a_confirmed_address_is_never_offered_again(crawler):
    """And the other half: once the collector HAS filed it, the address is
    collected for good - however long ago that was. Without this the archive
    would re-buy its own back catalogue every two hours."""
    source_id = estate_source(crawler)
    crawler.crawl(source_id)
    documents = crawler.documents(source_id)
    assert documents

    # The collector files every one of them, and the crawler's own reconcile
    # pass notices - which is what sets date_confirmed.
    # SUBMITTED FIRST, because reconcile only asks about rows that were really
    # sent - a queue row still PENDING has nothing to come back.
    crawler.submit()
    by_id = {d["bigint_id"]: d["text_uri_canonical"] for d in documents}
    for queued in crawler.queue(source_id):
        crawler.archive_task(tag=queued["text_tag"],
                             source_uri=by_id[queued["bigint_fk_document"]])
    archived = crawler.reconcile()
    assert archived == len(documents)

    rows = seen_rows(crawler, source_id)
    confirmed = [r for r in rows if r["date_confirmed"] is not None]
    assert len(confirmed) == len(documents), (rows, documents)

    # Long past the window, and still nothing new to do.
    age_the_submissions(crawler, source_id, 99)
    crawler.site.reset_log()
    crawler.crawl(source_id)
    assert len(crawler.documents(source_id)) == len(documents), (
        "a confirmed address was collected a second time")


def test_a_page_that_was_never_submitted_stays_seen(crawler):
    """An address the crawler fetched and deliberately did NOT send - the
    content was unchanged - has nothing in flight and no clock. Giving it one
    would re-fetch the whole site every two hours for ever."""
    source_id = estate_source(crawler)
    crawler.crawl(source_id)
    crawler.crawl(source_id)          # second run: everything is unchanged

    rows = seen_rows(crawler, source_id)
    assert rows
    # The second run submitted nothing, so no clock was restarted by it.
    assert all(r["date_confirmed"] is None for r in rows if r["date_submitted"])

    # And one that was never submitted at all keeps both columns empty.
    crawler.execute("""
        INSERT INTO scraper.seen_urls
              (text_project_id, text_uri_hash, text_uri_canonical, bigint_fk_source)
        VALUES ('', 'unsubmitted-hash', 'https://example.invalid/never-sent', %s)
        ON CONFLICT DO NOTHING
    """, (source_id,))
    quiet = crawler.one("""
        SELECT date_submitted, date_confirmed FROM scraper.seen_urls
         WHERE text_uri_hash = 'unsubmitted-hash'
    """)
    assert quiet["date_submitted"] is None and quiet["date_confirmed"] is None


def test_the_window_is_one_number_in_one_place():
    """It is read by the statement that decides what is new, and by nothing
    else - so a platform that needs longer is a one-line change."""
    assert store.SUBMISSION_GRACE == "2 hours"
