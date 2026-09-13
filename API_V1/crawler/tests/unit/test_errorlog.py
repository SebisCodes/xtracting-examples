"""The wording of the log, and the promise that writing it can never fail.

Two things are being checked here, and the first one is unusual for a test
suite: THE SENTENCES. A log row is read by the person who configured a source
in a browser, and "BLOCKED" is not an answer to anything. So the message has
to name what happened AND what to do, and that is asserted, not left to
review.

The second is `record()`'s promise: it never raises. A crawl that ran for
four minutes must not be lost because the log table is missing or the
connection has gone away. Both cases are exercised with connections that
misbehave on purpose.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app import errorlog

SCHEMA = Path(__file__).resolve().parents[3] / "database" / "init" / "03-scraper.sql"


# ── The sentences ───────────────────────────────────────────────────────


def actionable(message: str) -> bool:
    """A message a person can act on says what to do, not only what broke."""
    return any(word in message.lower() for word in
               ("ask", "check", "switch", "open ", "try", "raise", "add ",
                "choose", "top up", "put ", "nothing needs to be done",
                "nothing to change"))


def test_a_forbidden_list_page_names_the_host_and_what_to_do():
    row = errorlog.crawl_row(
        {"status": "SKIPPED",
         "robots": {"status": 200,
                    "list_reason": "robots.txt of https://www.example.ch forbids "
                                   "this address for 'xtracting-crawler/1.0'"}},
        source_name="Flats in Zurich", host="www.example.ch")

    assert row["kind"] == "ROBOTS_FORBIDDEN"
    assert row["severity"] == "error"
    assert "www.example.ch" in row["message"]
    assert "Flats in Zurich" in row["message"]
    assert actionable(row["message"])
    # The rule text and the robots status stay in the detail, where the log
    # view keeps them folded away.
    assert "forbids this address" in row["detail"]
    assert "200" in row["detail"]


def test_a_robots_txt_that_could_not_be_read_is_a_different_row():
    """5xx or no answer at all is not "no rules", it is "unknown rules" - and
    the answer for the customer is "wait", not "ask for permission"."""
    row = errorlog.crawl_row(
        {"status": "SKIPPED",
         "robots": {"status": 503, "list_reason": "robots.txt answers 503 - the "
                                                  "rules are unknown, so nothing is fetched"}},
        source_name="Flats", host="www.example.ch")

    assert row["kind"] == "ROBOTS_UNREADABLE"
    assert row["http_status"] == 503
    assert "not allowed" in row["message"]
    assert actionable(row["message"])


def test_the_fetch_failures_are_told_apart():
    cases = {
        403: "HTTP_4XX",
        404: "HTTP_4XX",
        429: "HTTP_4XX",
        500: "HTTP_5XX",
        503: "HTTP_5XX",
    }
    for status, kind in cases.items():
        row = errorlog.crawl_row(
            {"status": "BLOCKED", "robots": {"status": 200},
             "fetch": {"status": status, "error": f"HTTP {status}"}},
            source_name="Flats", host="www.example.ch")
        assert row["kind"] == kind, status
        assert row["http_status"] == status
        assert actionable(row["message"]), status


def test_a_browser_check_says_what_rendered_mode_can_do():
    row = errorlog.crawl_row(
        {"status": "BLOCKED", "robots": {"status": 200},
         "fetch": {"status": 403, "challenge": True, "error": "browser check"}},
        source_name="Flats", host="www.example.ch")
    assert row["kind"] == "CHALLENGE"
    assert "Rendered mode" in row["message"]


def test_a_timeout_is_not_a_network_error():
    timed_out = errorlog.crawl_row(
        {"status": "ERROR", "robots": {"status": 200},
         "fetch": {"status": 0, "error": "ReadTimeout: timed out"}},
        source_name="Flats", host="www.example.ch")
    refused = errorlog.crawl_row(
        {"status": "ERROR", "robots": {"status": 200},
         "fetch": {"status": 0, "error": "ConnectError: connection refused"}},
        source_name="Flats", host="www.example.ch")

    assert timed_out["kind"] == "TIMEOUT"
    assert refused["kind"] == "NETWORK"
    assert "could not be reached" in refused["message"]


def test_a_run_that_found_nothing_is_a_warning_with_a_suggestion():
    row = errorlog.crawl_row({"status": "OK", "links": [], "counts": {"links": 0},
                              "fetch": {"status": 200, "final_url": "https://a.example/l"},
                              "warnings": ["0 links found on this page."]},
                             source_name="Flats", host="a.example")
    assert row["kind"] == "NO_LINKS"
    assert row["severity"] == "warning"
    assert "Rendered mode" in row["message"]


def test_a_run_that_worked_leaves_no_row():
    assert errorlog.crawl_row({"status": "OK", "links": [{"url": "x"}]}) is None


def test_a_crawl_that_threw_is_an_internal_row_with_the_message_in_the_detail():
    row = errorlog.crawl_row(None, source_name="Flats",
                             error="AttributeError: 'NoneType' has no attribute 'x'")
    assert row["kind"] == "INTERNAL"
    assert "fault in the crawler" in row["message"]
    assert "AttributeError" in row["detail"]


def test_a_test_crawl_says_the_same_thing_without_the_alarm():
    """The dashboard's test runner writes the same wording with severity
    info - so the log and the editor's banner cannot disagree."""
    same = dict(status="SKIPPED", robots={"status": 200, "list_reason": "forbidden"})
    real = errorlog.crawl_row(same, source_name="Flats", host="a.example")
    test = errorlog.crawl_row(same, source_name="Flats", host="a.example",
                              severity="info")
    assert test["kind"] == real["kind"]
    assert test["message"] == real["message"]
    assert (test["severity"], real["severity"]) == ("info", "error")


@pytest.mark.parametrize("status,kind", [
    ("TOO_LARGE", "FILE_TOO_LARGE"),
    ("WRONG_TYPE", "FILE_WRONG_TYPE"),
    ("NO_TEXT", "FILE_NO_TEXT"),
    ("ROBOTS", "ROBOTS_FORBIDDEN"),
    ("ERROR", "NETWORK"),
])
def test_every_rejected_file_gets_its_own_kind_and_a_way_out(status, kind):
    row = errorlog.file_row(status=status, url="https://a.example/f/plan-7.pdf",
                            file_type="pdf", reason="because",
                            page_uri="https://a.example/rent/7")
    assert row["kind"] == kind
    assert "plan-7.pdf" in row["message"]
    assert actionable(row["message"])
    assert row["uri"] == "https://a.example/f/plan-7.pdf"
    assert "https://a.example/rent/7" in row["detail"]


def test_a_file_the_configuration_switched_off_is_not_a_log_row():
    """A decision somebody took is not something that went wrong. The file
    table keeps the row; the log stays readable."""
    assert errorlog.file_row(status="NOT_SELECTED", url="https://a.example/x.xlsx") is None
    assert errorlog.file_row(status="QUEUED", url="https://a.example/x.pdf") is None
    assert errorlog.file_row(status="SENT", url="https://a.example/x.pdf") is None


def test_the_three_submit_answers_are_three_different_sentences():
    pause = errorlog.submit_row(status=429, message="HTTP 429", tags=8,
                                key_label="alpha", backpressure=True)
    key = errorlog.submit_row(status=401, message="HTTP 401", key_label="alpha")
    refused = errorlog.submit_row(status=400, message="HTTP 400", tags=3,
                                  key_label="alpha")

    assert (pause["kind"], key["kind"], refused["kind"]) == (
        "SUBMIT_BACKPRESSURE", "KEY_INVALID", "SUBMIT_REJECTED")
    assert "nothing is lost" in pause["message"]
    assert pause["severity"] == "warning"          # a state, not a fault
    assert "XTRACTING_API_KEYS" in key["message"]
    assert "not sent again" in refused["message"]
    for row in (pause, key, refused):
        assert actionable(row["message"])


def test_an_unknown_key_label_says_which_labels_exist():
    row = errorlog.key_row(key_label="beta", source_name="Flats",
                           known=["alpha", "gamma"])
    assert row["kind"] == "KEY_INVALID"
    assert "alpha, gamma" in row["message"]


# ── The table this writes into ──────────────────────────────────────────


def kinds_in_schema() -> set[str]:
    """The kinds 03-scraper.sql accepts, read out of its CHECK.

    The constraint is the last word and this module's KINDS the first one;
    without this test the two drift apart the first time somebody adds a kind
    on one side only, and the symptom would be log rows silently refused in
    production.
    """
    sql = SCHEMA.read_text(encoding="utf-8")
    block = re.search(r"crawler_error_kind_known CHECK \(text_kind IN \((.*?)\)\)",
                      sql, re.S)
    assert block, "the CHECK on text_kind is not where this test expects it"
    return set(re.findall(r"'([A-Z_0-9]+)'", block.group(1)))


def test_the_kinds_are_exactly_the_ones_the_database_accepts():
    assert set(errorlog.KINDS) == kinds_in_schema()


def test_the_severities_are_exactly_the_ones_the_database_accepts():
    sql = SCHEMA.read_text(encoding="utf-8")
    block = re.search(r"crawler_error_severity_known CHECK \(\s*text_severity IN \((.*?)\)\)",
                      sql, re.S)
    assert block
    assert set(errorlog.SEVERITIES) == set(re.findall(r"'(\w+)'", block.group(1)))


def test_every_kind_has_a_label():
    """`text_name` is the column every table in this archive carries, and an
    empty one would read as a blank line in the log."""
    assert set(errorlog.LABELS) == set(errorlog.KINDS)
    assert all(errorlog.LABELS[kind].strip() for kind in errorlog.KINDS)


# ── record(): it writes, and it never raises ────────────────────────────


class FakeCursor:
    def __init__(self, calls: list) -> None:
        self.calls = calls

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.calls.append((sql, params))


class FakeTransaction:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeConnection:
    """Enough of psycopg's shape for `record()` - and nothing else."""

    def __init__(self) -> None:
        self.calls: list = []

    def transaction(self):
        return FakeTransaction()

    def cursor(self):
        return FakeCursor(self.calls)


class BrokenConnection(FakeConnection):
    """A connection that has gone away between two statements."""

    def transaction(self):
        raise RuntimeError("the connection is closed")


def test_a_row_is_written_with_everything_the_log_view_shows():
    conn = FakeConnection()
    assert errorlog.record(conn, kind="ROBOTS_FORBIDDEN", severity="error",
                           message="robots.txt of a.example forbids this",
                           source_id=7, source_name="Flats", host="a.example",
                           uri="https://a.example/l", http_status=200,
                           detail="Disallow: /", run_id="s7-abcd") is True

    (sql, params), = conn.calls
    assert "monitoring.scraper_errors" in sql
    assert params["kind"] == "ROBOTS_FORBIDDEN"
    assert params["name"] == errorlog.LABELS["ROBOTS_FORBIDDEN"]
    assert params["source"] == 7 and params["source_name"] == "Flats"
    assert params["run"] == "s7-abcd"
    # The three identity columns are carried and empty - a crawl that never
    # got a page belongs to no task.
    assert (params["project"], params["language"], params["task"]) == ("", "", "")


def test_a_broken_connection_costs_a_log_row_and_nothing_else():
    """THE PROMISE OF THIS MODULE. A crawl of twenty pages must not be lost
    because the log could not be written."""
    assert errorlog.record(BrokenConnection(), kind="NETWORK", severity="error",
                           message="unreachable") is False
    assert errorlog.record(None, kind="NETWORK", severity="error",
                           message="unreachable") is False


def test_a_kind_the_log_does_not_know_still_leaves_a_row():
    """Losing the row would hide the very thing that went wrong; the mistake
    travels in the detail instead."""
    conn = FakeConnection()
    assert errorlog.record(conn, kind="WHAT_IS_THIS", severity="critical",
                           message="something") is True
    params = conn.calls[0][1]
    assert params["kind"] == "INTERNAL"
    assert params["severity"] == "error"
    assert "WHAT_IS_THIS" in params["detail"]


def test_a_long_detail_is_cut_rather_than_refused():
    conn = FakeConnection()
    errorlog.record(conn, kind="INTERNAL", severity="error", message="x",
                    detail="y" * 10000)
    assert len(conn.calls[0][1]["detail"]) == errorlog.DETAIL_LIMIT
