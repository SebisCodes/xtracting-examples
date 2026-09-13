"""The Log API against a real archive.

    DATABASE_URL=postgresql://... python -m pytest tests/flow/test_logs_api.py -q

The rows are written here rather than crawled: what is being checked is the
reading - newest first, bounded by the time range, filtered by source, kind,
severity and free text, and paginated - and a crawl would only add ways for
the test to fail for reasons that are not the API's.

Everything this module writes carries `text_run_id = '_logstest'` and is
removed afterwards, so it can run against the same throw-away archive as the
rest of the suite without leaving a log behind.
"""

from __future__ import annotations

import pytest

RUN_ID = "_logstest"

#: (minutes ago, kind, severity, source id, source name, host, message)
ROWS = [
    (5, "ROBOTS_FORBIDDEN", "error", 901, "_logstest Flats", "flats.example",
     "robots.txt of flats.example forbids the list page of _logstest Flats, so "
     "nothing was fetched. Ask the site owner for permission."),
    (20, "FILE_TOO_LARGE", "warning", 901, "_logstest Flats", "flats.example",
     "plan-7.pdf is larger than the limit set for pdf files, so it was not sent. "
     "Raise the limit for this type on the source."),
    (90, "HTTP_5XX", "error", 902, "_logstest Cars", "cars.example",
     "cars.example has a problem of its own (HTTP 503) and could not answer."),
    (60 * 40, "SUBMIT_REJECTED", "error", 902, "_logstest Cars", "cars.example",
     "The platform refused 3 documents sent with key 'alpha'."),
    (10, "NO_LINKS", "info", 903, "_logstest Law", "law.example",
     "The list page of _logstest Law had no links to collect."),
]


@pytest.fixture(scope="module")
def logged(archive):
    """The rows this module reads, and nothing else in the log."""
    with archive.connect() as conn:
        conn.execute("DELETE FROM monitoring.scraper_errors WHERE text_run_id = %s",
                     (RUN_ID,))
        for minutes, kind, severity, source_id, name, host, message in ROWS:
            conn.execute("""
                INSERT INTO monitoring.scraper_errors
                      (date_added, text_name, bigint_fk_source, text_source_name,
                       text_host, text_kind, text_severity, text_uri,
                       integer_http_status, text_message, text_detail, text_run_id)
                VALUES (now() - make_interval(mins => %(mins)s), %(kind)s, %(source)s,
                        %(name)s, %(host)s, %(kind)s, %(severity)s, %(uri)s,
                        %(status)s, %(message)s, %(detail)s, %(run)s)
            """, {"mins": minutes, "kind": kind, "source": source_id, "name": name,
                  "host": host, "uri": f"https://{host}/list", "severity": severity,
                  "status": 503 if kind == "HTTP_5XX" else None,
                  "message": message, "detail": f"technical detail for {kind}",
                  "run": RUN_ID})
        conn.execute("""
            INSERT INTO monitoring.scraper_runs
                  (date_added, text_name, text_status, bigint_fk_source,
                   integer_pages, integer_links, integer_accepted, integer_new,
                   integer_submitted, integer_ms, text_message)
            VALUES (now() - INTERVAL '5 minutes', '_logstest Flats', 'SKIPPED', 901,
                    0, 0, 0, 0, 0, 12, 'robots.txt forbids the list page'),
                   (now() - INTERVAL '30 minutes', '_logstest Cars', 'OK', 902,
                    2, 48, 20, 3, 3, 1840, NULL),
                   -- Source 904 crawls happily and has never written an error
                   -- row. It is the case a Source filter built from the
                   -- error table alone would hide.
                   (now() - INTERVAL '35 minutes', '_logstest Register', 'OK', 904,
                    1, 12, 12, 2, 2, 900, NULL)
        """)
        conn.commit()
    yield
    with archive.connect() as conn:
        conn.execute("DELETE FROM monitoring.scraper_errors WHERE text_run_id = %s",
                     (RUN_ID,))
        conn.execute("DELETE FROM monitoring.scraper_runs WHERE text_name LIKE %s",
                     ("_logstest%",))
        conn.commit()


def ours(payload: dict) -> list[dict]:
    """Only the rows this module wrote - the archive may hold others."""
    return [item for item in payload["items"]
            if item["source"]["name"].startswith("_logstest")]


# ── The log ─────────────────────────────────────────────────────────────


def test_the_log_reads_newest_first_with_everything_a_row_shows(client, logged):
    payload = client.get("/api/logs/errors", params={"range": "day"}).json()
    assert payload["available"] is True

    rows = ours(payload)
    kinds = [row["kind"] for row in rows]
    # Newest first: the five-minute-old robots row before the twenty-minute
    # file row, and the forty-hour-old submission is outside the day.
    assert kinds[:3] == ["ROBOTS_FORBIDDEN", "NO_LINKS", "FILE_TOO_LARGE"]
    assert "SUBMIT_REJECTED" not in kinds

    row = rows[0]
    assert row["severity"] == "error"
    assert row["severity_label"] == "Error"
    assert row["kind_label"] == "robots.txt forbids this address"
    assert row["source"]["id"] == 901
    assert row["source"]["name"] == "_logstest Flats"
    assert row["host"] == "flats.example"
    assert "Ask the site owner" in row["message"]
    assert row["detail"] == "technical detail for ROBOTS_FORBIDDEN"
    assert row["date"]


def test_the_time_range_is_what_bounds_the_log(client, logged):
    day = [row["kind"] for row in ours(client.get(
        "/api/logs/errors", params={"range": "day"}).json())]
    week = [row["kind"] for row in ours(client.get(
        "/api/logs/errors", params={"range": "week"}).json())]
    hour = [row["kind"] for row in ours(client.get(
        "/api/logs/errors", params={"range": "hour"}).json())]

    assert "SUBMIT_REJECTED" in week and "SUBMIT_REJECTED" not in day
    # The ninety-minute-old row is in the day and not in the hour.
    assert "HTTP_5XX" in day and "HTTP_5XX" not in hour


def test_a_range_that_does_not_exist_is_refused_by_name(client, logged):
    answer = client.get("/api/logs/errors", params={"range": "year"})
    assert answer.status_code == 400
    body = answer.json()
    assert "year" in body["error"]
    assert "week" in body["hint"]


def test_filtering_by_source_kind_and_severity(client, logged):
    by_source = ours(client.get("/api/logs/errors",
                                params={"range": "week", "source_id": 902}).json())
    assert {row["source"]["id"] for row in by_source} == {902}

    by_kind = ours(client.get("/api/logs/errors",
                              params={"range": "week", "kind": "FILE_TOO_LARGE"}).json())
    assert [row["kind"] for row in by_kind] == ["FILE_TOO_LARGE"]

    warnings = ours(client.get("/api/logs/errors",
                               params={"range": "week", "severity": "warning"}).json())
    assert {row["severity"] for row in warnings} == {"warning"}

    unknown = client.get("/api/logs/errors", params={"kind": "EXPLODED"})
    assert unknown.status_code == 400
    assert "FILE_TOO_LARGE" in unknown.json()["hint"]


def test_severity_problem_is_everything_that_is_not_a_test(client, logged):
    """The seam between the Sources overview and this view.

    /api/logs/summary counts `text_severity <> 'info'` for the line "N
    problems in the last 24 hours"; the log itself listed every row,
    including the ones a test from the dashboard writes, so the link
    promised one number and the page it opened showed another. `problem` is
    that reading by name - one filter value, not a fourth severity in the
    table."""
    problems = ours(client.get("/api/logs/errors",
                               params={"range": "week", "severity": "problem"}).json())
    assert problems, "the fixture writes errors and warnings"
    assert "info" not in {row["severity"] for row in problems}
    assert {row["severity"] for row in problems} <= {"error", "warning"}

    # And the total is the number the overview's link shows: both readings
    # over the whole archive, both over the same 24 hours.
    summary = client.get("/api/logs/summary", params={"hours": 24}).json()
    day = client.get("/api/logs/errors",
                     params={"range": "day", "severity": "problem",
                             "page_size": 1}).json()
    assert day["total"] == summary["problems"]
    # Without the filter the log shows more, and the link must not
    # contradict that: the test rows are in there too.
    unfiltered = client.get("/api/logs/errors",
                            params={"range": "day", "page_size": 1}).json()
    assert unfiltered["total"] >= day["total"]

    # The filter list offers it, so the reader can see and undo it.
    severities = {s["value"]: s for s in client.get(
        "/api/logs/kinds", params={"range": "week"}).json()["severities"]}
    assert severities["problem"]["count"] == (severities["error"]["count"]
                                              + severities["warning"]["count"])
    assert "test" in severities["problem"]["label"].lower()


def test_the_free_text_searches_message_host_and_source(client, logged):
    by_host = ours(client.get("/api/logs/errors",
                              params={"range": "week", "q": "cars.example"}).json())
    assert {row["source"]["id"] for row in by_host} == {902}

    by_word = ours(client.get("/api/logs/errors",
                              params={"range": "week", "q": "plan-7.pdf"}).json())
    assert [row["kind"] for row in by_word] == ["FILE_TOO_LARGE"]

    # A wildcard a person typed is a character they are looking for, not a
    # pattern: "%" must not match everything.
    nothing = ours(client.get("/api/logs/errors",
                              params={"range": "week", "q": "%"}).json())
    assert nothing == []


def test_paging_keeps_the_total_on_a_page_past_the_first(client, logged):
    first = client.get("/api/logs/errors",
                       params={"range": "week", "source_id": 901, "page_size": 1}).json()
    assert first["total"] == 2 and first["pages"] == 2 and len(first["items"]) == 1

    second = client.get("/api/logs/errors",
                        params={"range": "week", "source_id": 901,
                                "page_size": 1, "page": 1}).json()
    assert second["total"] == 2
    assert second["items"][0]["kind"] != first["items"][0]["kind"]

    # A page past the end still knows how many there are, so the view says
    # "page 4 of 2" rather than "nothing went wrong".
    beyond = client.get("/api/logs/errors",
                        params={"range": "week", "source_id": 901,
                                "page_size": 1, "page": 9}).json()
    assert beyond["items"] == [] and beyond["total"] == 2


# ── The filter lists and the one number ─────────────────────────────────


def test_the_filter_lists_carry_counts_and_every_kind(client, logged):
    payload = client.get("/api/logs/kinds", params={"range": "week"}).json()
    assert payload["available"] is True

    kinds = {item["value"]: item["count"] for item in payload["kinds"]}
    # Every kind is offered even at count zero - "was there ever a 403?" has
    # to be answerable with a truthful empty list.
    assert len(kinds) == 17
    assert kinds["ROBOTS_FORBIDDEN"] >= 1 and kinds["FILE_TOO_LARGE"] >= 1
    # The ones that occur come first.
    assert payload["kinds"][0]["count"] >= payload["kinds"][-1]["count"]

    sources = {item["id"]: item for item in payload["sources"]}
    assert sources[901]["name"] == "_logstest Flats"
    assert sources[901]["count"] == 2
    # `count` is the problems, and both numbers are carried so the view can
    # show the one belonging to the tab that is open.
    assert sources[901]["problems"] == 2 and sources[901]["runs"] == 1

    severities = {item["value"]: item["label"] for item in payload["severities"]}
    # Three severities the table stores, and one combined reading in front of
    # them: "problem" is what the Sources overview links with, so the filter
    # that reproduces its number is the first one offered.
    assert severities == {"problem": "Error or warning (no tests)",
                          "error": "Error", "warning": "Warning", "info": "Test"}
    assert payload["severities"][0]["value"] == "problem"
    assert {item["value"] for item in payload["run_statuses"]} == {
        "OK", "SKIPPED", "BLOCKED", "BACKPRESSURE", "ERROR", "TEST"}


def test_the_source_filter_is_built_from_both_tables(client, logged):
    """A source that ran and never went wrong has to be pickable.

    Counted over monitoring.scraper_errors alone, source 904 - which crawls
    happily - would be in neither tab's Source select. Two readings would be
    impossible: "nothing went wrong all week, but did MY source run?" could
    not be asked at all, because the select would be empty exactly when
    nothing had gone wrong.
    """
    payload = client.get("/api/logs/kinds", params={"range": "week"}).json()
    sources = {item["id"]: item for item in payload["sources"]}

    assert 904 in sources, "a source with runs and no errors is not offered"
    assert sources[904]["name"] == "_logstest Register"
    assert sources[904]["problems"] == 0
    assert sources[904]["runs"] == 1
    # And it can then be used: the filter the select offers has to answer.
    runs = client.get("/api/logs/runs",
                      params={"range": "week", "source_id": 904}).json()
    assert [row["name"] for row in runs["items"]] == ["_logstest Register"]

    # A source known only from the error table is still there, and the name
    # is taken from whichever table carries one.
    assert sources[903]["name"] == "_logstest Law"
    assert sources[903]["runs"] == 0


def test_the_summary_counts_problems_and_not_tests(client, logged):
    """The number the Sources overview shows: "N problems in the last 24
    hours". A test crawl is somebody trying a configuration out."""
    payload = client.get("/api/logs/summary", params={"hours": 24}).json()
    assert payload["available"] is True
    assert payload["problems"] >= 3
    assert payload["errors"] >= 2 and payload["warnings"] >= 1
    assert payload["newest"]

    one_source = client.get("/api/logs/summary",
                            params={"hours": 24, "source_id": 903}).json()
    # Source 903 only ever left an `info` row, so it has no problems.
    assert one_source["problems"] == 0


# ── The run history ─────────────────────────────────────────────────────


def test_the_runs_tab_reads_the_run_table_newest_first(client, logged):
    payload = client.get("/api/logs/runs", params={"range": "day"}).json()
    rows = [item for item in payload["items"] if item["name"].startswith("_logstest")]
    assert [row["name"] for row in rows] == ["_logstest Flats", "_logstest Cars",
                                             "_logstest Register"]

    skipped = rows[0]
    assert skipped["status"] == "SKIPPED"
    assert skipped["status_label"] == "Skipped (robots.txt)"
    assert skipped["message"] == "robots.txt forbids the list page"

    crawled = rows[1]
    assert crawled["counts"] == {"pages": 2, "links": 48, "accepted": 20,
                                 "new": 3, "files": 0, "submitted": 3}
    assert crawled["ms"] == 1840


def test_the_runs_can_be_filtered_by_result_and_source(client, logged):
    payload = client.get("/api/logs/runs",
                         params={"range": "day", "status": "SKIPPED"}).json()
    rows = [item for item in payload["items"] if item["name"].startswith("_logstest")]
    assert [row["name"] for row in rows] == ["_logstest Flats"]

    refused = client.get("/api/logs/runs", params={"status": "MAYBE"})
    assert refused.status_code == 400


# ── What a test crawl leaves ────────────────────────────────────────────


def test_a_test_crawl_writes_the_same_sentence_with_severity_info(client, logged, archive):
    """The dashboard's test runner writes into the same table, with
    `info` - so a customer finds the banner's wording again in the log."""
    from app.testrunner import record_test_problem

    job = {"id": 4242, "bigint_fk_source": 901,
           "json_config": {"text_name": "_logstest Flats",
                           "text_list_url": "https://flats.example/rent/list"}}
    result = {"status": "SKIPPED", "robots": {"status": 200,
              "list_reason": "robots.txt of https://flats.example forbids this address"},
              "fetch": {}, "warnings": [], "links": []}

    assert record_test_problem(job, result) is True
    try:
        payload = client.get("/api/logs/errors",
                             params={"range": "hour", "severity": "info"}).json()
        rows = [row for row in payload["items"] if row["run_id"] == "test-4242"]
        assert len(rows) == 1
        assert rows[0]["kind"] == "ROBOTS_FORBIDDEN"
        assert rows[0]["severity_label"] == "Test"
        assert "forbids this address" in rows[0]["message"]
        assert rows[0]["source"]["name"] == "_logstest Flats"
        assert rows[0]["host"] == "flats.example"
    finally:
        with archive.connect() as conn:
            conn.execute("DELETE FROM monitoring.scraper_errors "
                         "WHERE text_run_id = 'test-4242'")
            conn.commit()


def test_a_test_crawl_that_went_well_writes_nothing(client):
    from app.testrunner import record_test_problem

    assert record_test_problem({"id": 1}, {"status": "OK", "links": [{"url": "x"}]}) is False


# ── The log outlives what it is about ───────────────────────────────────


def test_every_row_says_whether_its_watched_page_still_exists(client, logged, archive):
    """A log row is kept for ninety days; a watched page is deleted in a
    second. The view linked every name it printed, so a row about a page
    that is gone opened the three-step editor under a 404 - a dead end with
    a Save button on it that could not succeed.

    The reading the view needs is one boolean per row: is this id still in
    scraper_config.sources? The ids the rest of this module writes about
    (901-904) are deliberately not, so they carry `exists: False`; the one
    written here is, and carries True."""
    with archive.connect() as conn:
        live = conn.execute("""
            INSERT INTO scraper_config.sources (text_name, text_list_url, text_host)
            VALUES ('_logstest Live', 'https://live.example/list', 'live.example')
            RETURNING bigint_id
        """).fetchone()["bigint_id"]
        conn.execute("""
            INSERT INTO monitoring.scraper_errors
                  (date_added, text_name, bigint_fk_source, text_source_name,
                   text_host, text_kind, text_severity, text_uri, text_message,
                   text_run_id)
            VALUES (now(), 'NO_LINKS', %s, '_logstest Live', 'live.example',
                    'NO_LINKS', 'warning', 'https://live.example/list',
                    'The list page of _logstest Live had no links to collect.', %s)
        """, (live, RUN_ID))
        conn.execute("""
            INSERT INTO monitoring.scraper_runs
                  (date_added, text_name, text_status, bigint_fk_source,
                   integer_pages, integer_links, integer_accepted, integer_new,
                   integer_submitted, integer_ms)
            VALUES (now(), '_logstest Live', 'OK', %s, 1, 4, 4, 0, 0, 300)
        """, (live,))
        conn.commit()
    try:
        rows = ours(client.get("/api/logs/errors", params={"range": "day"}).json())
        by_id = {row["source"]["id"]: row["source"]["exists"] for row in rows}
        assert by_id[live] is True
        # 901 wrote four rows in this module and has never been configured.
        assert by_id[901] is False

        runs = [item for item in client.get(
            "/api/logs/runs", params={"range": "day"}).json()["items"]
            if item["name"].startswith("_logstest")]
        run_exists = {item["source"]["id"]: item["source"]["exists"] for item in runs}
        assert run_exists[live] is True
        assert run_exists[901] is False
    finally:
        with archive.connect() as conn:
            conn.execute("DELETE FROM monitoring.scraper_errors "
                         "WHERE bigint_fk_source = %s", (live,))
            conn.execute("DELETE FROM monitoring.scraper_runs "
                         "WHERE bigint_fk_source = %s", (live,))
            conn.execute("DELETE FROM scraper_config.sources WHERE bigint_id = %s",
                         (live,))
            conn.commit()
