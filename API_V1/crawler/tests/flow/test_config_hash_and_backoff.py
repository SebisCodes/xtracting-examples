"""The schedule: when a source runs again, and why.

Three things decide it, and each of them is a decision somebody would otherwise
have to make by hand: a changed configuration is due at once, a failing site is
asked less and less often, and a crashed worker's lease expires instead of
holding a source forever.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app import db, scheduler

from conftest import PREFIX


def estate_source(crawler, **fields):
    return crawler.add_source(
        "estate schedule", crawler.site.estate_list, text_mode="selected",
        accept=[crawler.site.estate + "/rent/40012300"],
        integer_max_new_per_run=1, integer_interval_minutes=360, **fields)


def test_a_new_source_is_due_at_once(crawler):
    """Somebody just switched it on and is looking at the page. Six hours later
    is not an answer."""
    source_id = estate_source(crawler)
    target = crawler.target(source_id)
    assert target["date_next_run"] <= datetime.now(timezone.utc)
    assert target["text_config_hash"]


def test_a_successful_run_sets_the_next_date_around_the_interval(crawler):
    source_id = estate_source(crawler)
    crawler.crawl(source_id)

    target = crawler.target(source_id)
    ahead = target["date_next_run"] - datetime.now(timezone.utc)
    # Six hours plus or minus a tenth - the jitter that keeps ten sources saved
    # in the same minute from staying in lockstep forever.
    assert timedelta(hours=5) < ahead < timedelta(hours=7)
    assert target["integer_consecutive_failures"] == 0
    assert target["bool_leased"] is False


def test_changing_the_rules_makes_it_due_again(crawler):
    """The hash covers what changes a crawl. Somebody who has just changed a
    pattern wants to see the effect, and waiting six hours to show it is how a
    person concludes the switch did nothing."""
    source_id = estate_source(crawler)
    crawler.crawl(source_id)
    before = crawler.target(source_id)
    assert before["date_next_run"] > datetime.now(timezone.utc)

    crawler.update_source(source_id, text_mode="all_except_rejected")

    after = crawler.target(source_id)
    assert after["text_config_hash"] != before["text_config_hash"]
    assert after["date_next_run"] <= datetime.now(timezone.utc)


def test_renaming_a_source_does_not_send_a_request(crawler):
    """A rename is not a crawl. Re-running because somebody fixed a typo would
    be a request the site did not need to answer."""
    source_id = estate_source(crawler)
    crawler.crawl(source_id)
    before = crawler.target(source_id)

    crawler.update_source(source_id, text_name="_crawlertest renamed")

    after = crawler.target(source_id)
    assert after["text_config_hash"] == before["text_config_hash"]
    assert after["date_next_run"] == before["date_next_run"]


def test_a_changed_configuration_clears_the_backoff(crawler):
    """The three failures the previous rules collected say nothing about the
    new ones - and leaving them would keep the new configuration in a
    sixteen-fold backoff nobody asked for."""
    source_id = estate_source(crawler)
    with db.connect(crawler.dsn) as conn:
        db.execute(conn, """
            UPDATE scraper.targets SET integer_consecutive_failures = 3
             WHERE bigint_fk_source = %s
        """, (source_id,))
        conn.commit()

    crawler.update_source(source_id, integer_max_pages=3)

    assert crawler.target(source_id)["integer_consecutive_failures"] == 0


def test_a_failing_source_is_asked_less_and_less_often(crawler):
    """The site answers 404. Nothing here can fix that, and asking every ten
    minutes does not make it come back sooner."""
    source_id = crawler.add_source(
        "estate gone", crawler.site.estate + "/rent/does-not-exist",
        text_mode="all_except_rejected", integer_interval_minutes=60)

    result = crawler.crawl(source_id)
    assert result.status == "BLOCKED"

    target = crawler.target(source_id)
    assert target["integer_consecutive_failures"] == 1
    ahead = target["date_next_run"] - datetime.now(timezone.utc)
    assert timedelta(minutes=110) < ahead < timedelta(minutes=130)   # 60 * 2^1
    assert "404" in target["text_last_error"]

    # The distance doubles with every failure, four times at most: after the
    # fourth failed run it is sixteen hours and stays there. A site that has
    # been dead all day will not be alive in the next minute.
    for _ in range(3):
        crawler.crawl(source_id)
    ahead = crawler.target(source_id)["date_next_run"] - datetime.now(timezone.utc)
    assert timedelta(hours=15) < ahead < timedelta(hours=17)

    crawler.crawl(source_id)
    ahead = crawler.target(source_id)["date_next_run"] - datetime.now(timezone.utc)
    assert timedelta(hours=15) < ahead < timedelta(hours=17)


def test_a_successful_run_after_failures_starts_over(crawler):
    source_id = estate_source(crawler)
    with db.connect(crawler.dsn) as conn:
        db.execute(conn, """
            UPDATE scraper.targets SET integer_consecutive_failures = 3
             WHERE bigint_fk_source = %s
        """, (source_id,))
        conn.commit()

    crawler.crawl(source_id)
    assert crawler.target(source_id)["integer_consecutive_failures"] == 0


def test_a_lease_is_taken_once_and_returned(crawler):
    source_id = estate_source(crawler)
    with db.connect(crawler.dsn) as conn:
        first = scheduler.claim_due(conn, limit=50, lease_seconds=300)
        assert any(target.source_id == source_id for target in first)
        # While it is leased, a second scheduler sees nothing of it.
        second = scheduler.claim_due(conn, limit=50, lease_seconds=300)
        assert not any(target.source_id == source_id for target in second)
    assert crawler.target(source_id)["bool_leased"] is True
    assert crawler.target(source_id)["text_lease_owner"]


def test_an_expired_lease_is_freed_so_a_crashed_worker_does_not_block_it(crawler):
    """Without this the dashboard shows a crashed worker's source as "running"
    forever - the difference between a system that recovers and one that looks
    stuck."""
    source_id = estate_source(crawler)
    with db.connect(crawler.dsn) as conn:
        scheduler.claim_due(conn, limit=50, lease_seconds=300)
        db.execute(conn, """
            UPDATE scraper.targets SET date_lease_until = NOW() - INTERVAL '1 minute'
             WHERE bigint_fk_source = %s
        """, (source_id,))
        conn.commit()

        assert scheduler.reap_expired_leases(conn) >= 1
        assert crawler.target(source_id)["bool_leased"] is False

        again = scheduler.claim_due(conn, limit=50, lease_seconds=300)
        assert any(target.source_id == source_id for target in again)


def test_the_heartbeat_is_one_row_per_service(crawler):
    """It feeds the online/offline pills and nothing else - editing and testing
    sources work without a crawler, and no test depends on this table.

    `monitoring.heartbeat`, keyed by the SERVICE and not by the worker. A
    table in the crawler's own schema would leave the collector able to prove
    it is alive only by leaving a run row - and a collector that has crashed
    leaves no run row at all, which would make the one state worth seeing the
    one state invisible. Both services answer in one table, and the dashboard
    asks both the same question.

    Written under a name of this suite's own rather than under 'crawler': the
    archive may be shared with a dashboard somebody is looking at, and a test
    that stamped the real service's row would turn its pill green for whoever
    looked next. What is being checked is the shape - one row per key,
    overwritten - and that is the same shape whatever the key says.
    """
    service = f"{PREFIX}-service"
    with db.connect(crawler.dsn) as conn:
        db.heartbeat(conn, service, "1.0")
        db.heartbeat(conn, service, "1.1")
        conn.commit()
        rows = db.fetch_all(conn, "SELECT * FROM monitoring.heartbeat "
                                  "WHERE text_service = %s", (service,))
        assert len(rows) == 1 and rows[0]["text_version"] == "1.1"
        db.execute(conn, "DELETE FROM monitoring.heartbeat WHERE text_service = %s",
                   (service,))
        conn.commit()


def test_the_pause_switch_is_read_from_the_dashboard(crawler):
    """The emergency brake belongs to the person who has a browser open, not a
    shell - so it is a row the dashboard writes, not an environment variable.

    Written and rolled back: nothing outside this transaction ever sees it, and
    a test must not be able to pause a crawler somebody else is running.

    IT ASSERTS THE TRANSITIONS, NEVER THE STARTING VALUE. Opening with
    `assert db.paused(conn) is False` would only be true of an archive with
    no such row at all - and the dashboard's seed writes `scraper.paused =
    'true'` on purpose, because a crawler that starts fetching the moment it
    is deployed spends money before anybody has looked at one page
    (dashboard/standard/sql/02-dashboard-seed.sql says so). The claim worth
    pinning is not "a new archive is running"; it is that the crawler
    reads what the dashboard wrote, both ways, and that a rollback really undoes
    it. Written against whatever the archive happens to hold, this test is true
    of an installation that has been seeded and of one that has not.
    """
    with db.connect(crawler.dsn) as conn:
        before = db.paused(conn)

        for wanted in (True, False):
            db.execute(conn, """
                INSERT INTO dashboard.settings (text_key, text_value)
                VALUES ('scraper.paused', %s)
                ON CONFLICT (text_key) DO UPDATE SET text_value = EXCLUDED.text_value
            """, ("true" if wanted else "false",))
            assert db.paused(conn) is wanted

        conn.rollback()
        assert db.paused(conn) is before


def test_disabling_a_source_removes_its_target_but_keeps_what_it_collected(crawler):
    source_id = estate_source(crawler)
    crawler.crawl(source_id)
    assert crawler.documents(source_id)

    crawler.update_source(source_id, bool_enabled=False)

    assert crawler.target(source_id) is None
    # What happened stays: the documents and the queue are history, not plans.
    assert crawler.documents(source_id)
    assert crawler.queue(source_id)
