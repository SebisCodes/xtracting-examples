"""What the log looks like after something has gone wrong.

Four things go wrong here, one per way a crawl can fail, and each one has to
leave EXACTLY ONE row that a person can act on:

    a list page robots.txt forbids      ROBOTS_FORBIDDEN
    a host that does not answer         ROBOTS_UNREADABLE / NETWORK
    a file above its size limit         FILE_TOO_LARGE
    a batch the platform refuses        SUBMIT_REJECTED

Exactly one matters as much as the kind. A run that writes four rows about
the same rejection turns the log into something nobody reads, and the one row
that mattered is then somewhere on page three.
"""

from __future__ import annotations

import socket

from app import db, errorlog

FLAT = 40012300


def errors(crawler, source_id: int) -> list[dict]:
    return crawler.rows("""
        SELECT * FROM monitoring.scraper_errors
         WHERE bigint_fk_source = %s ORDER BY date_added, bigint_id
    """, (source_id,))


def closed_port_url() -> str:
    """An address on this machine that nothing is listening on.

    A host that does not resolve would take the resolver's timeout and make
    the suite slow and flaky; a closed port on loopback refuses at once, which
    is the same thing for the crawler: no answer, no crawl.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    return f"http://127.0.0.1:{port}/rent/list"


def test_a_forbidden_list_page_leaves_one_row_that_says_what_to_do(crawler):
    """`Disallow: /*sort=` on the cars host: the run is SKIPPED, and the log
    says which host, which source, and what a person can do about it."""
    forbidden = crawler.site.cars + "/de/autos/alle-marken?sort=price"
    source_id = crawler.add_source("cars forbidden", forbidden,
                                   text_mode="all_except_rejected")
    result = crawler.crawl(source_id)
    assert result.status == "SKIPPED"

    rows = errors(crawler, source_id)
    assert len(rows) == 1
    row = rows[0]
    assert row["text_kind"] == "ROBOTS_FORBIDDEN"
    assert row["text_severity"] == "error"
    # The three things the question "why is nothing coming from this domain?"
    # needs, all in one row: which source, which host, which rule.
    assert "cars forbidden" in row["text_source_name"]
    assert row["text_host"] in forbidden
    assert "forbids" in row["text_message"]
    assert "ask the site owner" in row["text_message"].lower()
    assert "robots.txt" in row["text_detail"].lower()
    # The identity columns are carried and empty: this row belongs to no task.
    assert (row["text_project"], row["text_task_id"]) == ("", "")


def test_a_host_that_does_not_answer_is_told_apart_from_one_that_refuses(crawler):
    """Two different answers for the customer.

    robots.txt that cannot be read means "wait, the rules are unknown"; a host
    that cannot be reached at all with robots switched off means "check the
    address". Both are one row, and they are not the same row.
    """
    unreadable = crawler.add_source("broken robots", crawler.site.broken + "/list",
                                    text_mode="all_except_rejected")
    crawler.crawl(unreadable)

    unreachable = crawler.add_source(
        "closed port", closed_port_url(), text_mode="all_except_rejected",
        bool_respect_robots=False,
        text_override_reason="test: there is nothing at this address to ask")
    crawler.crawl(unreachable)

    first, = errors(crawler, unreadable)
    assert first["text_kind"] == "ROBOTS_UNREADABLE"
    assert "503" in first["text_message"] or "503" in first["text_detail"]

    row, = errors(crawler, unreachable)
    assert row["text_kind"] in ("NETWORK", "TIMEOUT")
    assert "could not be reached" in row["text_message"] or \
           "did not answer" in row["text_message"]
    assert "check the address" in row["text_message"].lower() or \
           "try again" in row["text_message"].lower()


def test_a_file_over_its_limit_leaves_one_row_naming_the_file(crawler):
    """The thirty-megabyte plan set with a one-megabyte cap for pdf. The file
    table says TOO_LARGE; the log says which file, and how to allow it."""
    address = f"{crawler.site.estate}/rent/{FLAT}"
    source_id = crawler.add_source(
        "estate big file", crawler.site.estate_list, text_mode="exact",
        monitor=[address], bool_sub_sub=True,
        file_types={"pdf": (True, 1), "csv": False, "xlsx": False})
    crawler.crawl(source_id)

    too_large = [row for row in errors(crawler, source_id)
                 if row["text_kind"] == "FILE_TOO_LARGE"]
    assert len(too_large) == 1
    row = too_large[0]
    assert f"huge-{FLAT}.pdf" in row["text_message"]
    assert "raise the limit" in row["text_message"].lower()
    assert row["text_severity"] == "warning"
    assert row["text_uri"].endswith(f"huge-{FLAT}.pdf")
    # Everything this run logged carries the same run id, so the log view can
    # show the rejected file next to the run it happened in.
    assert row["text_run_id"]
    assert len({r["text_run_id"] for r in errors(crawler, source_id)}) == 1

    # A type that is switched off is a decision, not a fault - no row.
    assert not [r for r in errors(crawler, source_id)
                if "rooms-" in (r["text_uri"] or "")]


def test_a_refused_batch_leaves_one_row_that_does_not_promise_a_retry(crawler):
    source_id = crawler.add_source(
        "estate submit log", crawler.site.estate_list, text_mode="selected",
        accept=[crawler.site.estate + f"/rent/{FLAT}"], integer_max_new_per_run=3)
    crawler.crawl(source_id)
    crawler.api.state.next_submit_status = 400
    crawler.submit()

    rows = crawler.rows("""
        SELECT * FROM monitoring.scraper_errors WHERE text_run_id = %s
         ORDER BY date_added
    """, ("submit-_crawlertest",))
    assert len(rows) == 1
    row = rows[0]
    assert row["text_kind"] == "SUBMIT_REJECTED"
    assert row["integer_http_status"] == 400
    assert "not sent again" in row["text_message"]
    assert "_crawlertest" in row["text_message"]      # which key


def test_backpressure_is_a_warning_and_says_nothing_is_lost(crawler):
    """402 is a state, not a fault, and the row has to read like one: the
    customer's next step is to top up, not to look for a broken source."""
    source_id = crawler.add_source(
        "estate backpressure", crawler.site.estate_list, text_mode="selected",
        accept=[crawler.site.estate + f"/rent/{FLAT}"], integer_max_new_per_run=2)
    crawler.crawl(source_id)
    crawler.api.state.next_submit_status = 402
    crawler.submit()

    row, = crawler.rows("""
        SELECT * FROM monitoring.scraper_errors
         WHERE text_run_id = %s AND text_kind = 'SUBMIT_BACKPRESSURE'
    """, ("submit-_crawlertest",))
    assert row["text_severity"] == "warning"
    assert "nothing is lost" in row["text_message"]


def test_a_key_label_nothing_answers_to_is_logged_once(crawler):
    """The source crawls and then cannot submit. That is a configuration
    fault, it is logged when it appears, and not again on the next sync."""
    source_id = crawler.add_source("wrong key", crawler.site.estate_list,
                                   text_key_label="not-a-key")
    crawler.sync()
    crawler.sync()

    rows = [row for row in errors(crawler, source_id)
            if row["text_kind"] == "KEY_INVALID"]
    assert len(rows) == 1
    assert "not-a-key" in rows[0]["text_message"]
    assert "XTRACTING_API_KEYS" in rows[0]["text_message"]


def test_a_successful_run_writes_nothing_to_the_log(crawler):
    """The log is for what went wrong. A source that works has to leave it
    empty, or nobody will believe the rows that are there."""
    source_id = crawler.add_source(
        "estate quiet", crawler.site.estate_list, text_mode="selected",
        accept=[crawler.site.estate + f"/rent/{FLAT}"], integer_max_new_per_run=2)
    result = crawler.crawl(source_id)

    assert result.status == "OK"
    assert errors(crawler, source_id) == []


def test_the_log_survives_a_connection_that_has_gone_away(crawler):
    """`record()` never raises. The crawl is worth more than its log row."""
    conn = db.connect(crawler.dsn)
    conn.close()

    assert errorlog.record(conn, kind="NETWORK", severity="error",
                           message="the site could not be reached") is False

    # And the same helper on a healthy connection still writes. This test
    # makes no source, so the harness's cleanup has nothing to clean by: the
    # row is removed here.
    with db.connect(crawler.dsn) as healthy:
        assert errorlog.record(healthy, kind="NETWORK", severity="error",
                               message="the site could not be reached",
                               run_id="_crawlertest direct") is True
        healthy.commit()
        assert db.execute(healthy, "DELETE FROM monitoring.scraper_errors "
                                   "WHERE text_run_id = %s",
                          ("_crawlertest direct",)) == 1
        healthy.commit()


def test_a_failed_log_row_does_not_take_the_run_row_with_it(crawler):
    """The savepoint. `record()` is handed the connection the run row was
    written on; a failing INSERT here must roll back to its own savepoint and
    leave the caller's work standing."""
    with db.connect(crawler.dsn) as conn:
        db.execute(conn, """
            INSERT INTO monitoring.scraper_runs (text_name, text_status)
            VALUES ('_crawlertest savepoint', 'OK')
        """)
        # An HTTP status that is not a number: the column is INTEGER, so this
        # is a refusal from the database rather than one this module makes.
        written = errorlog.record(conn, kind="NETWORK", severity="error",
                                  message="the site could not be reached",
                                  http_status="not a number")
        assert written is False
        conn.commit()

    row = crawler.one("""
        SELECT text_status FROM monitoring.scraper_runs
         WHERE text_name = '_crawlertest savepoint'
    """)
    assert row and row["text_status"] == "OK"

    with db.connect(crawler.dsn) as conn:
        db.execute(conn, "DELETE FROM monitoring.scraper_runs "
                         "WHERE text_name = '_crawlertest savepoint'")
        conn.commit()
