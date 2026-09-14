"""The sentences a manual run ends with, checked against the crawl's verdicts.

A manual run that sent nothing has to say why, and the reason must be the
crawl's own: robots.txt is the SKIPPED verdict of `crawlkit.crawl.run()`, the
site's refusal is BLOCKED. A sentence attached to the wrong verdict reads
"no link on this page counts as a document" about a page the crawler never
fetched, which is the one wrong answer a person cannot see through.
"""

from __future__ import annotations

import pytest

from app import errorlog, manualrun
from crawlkit.crawl import CrawlResult


def reason(status: str, *, error: str = "", accepted: int = 0, **sink_counts) -> str:
    return manualrun._nothing_reason(
        CrawlResult(status=status, error=error, counts={"accepted": accepted}),
        sink_counts)


def test_robots_refusal_is_the_skipped_verdict():
    assert reason("SKIPPED").startswith("robots.txt forbids this page")


@pytest.mark.parametrize("error", ["HTTP 403 for the list page.", ""])
def test_a_refused_page_reports_the_sites_answer_not_robots(error):
    text = reason("BLOCKED", error=error)
    assert "robots" not in text
    assert "counts as a document" not in text
    assert text.startswith(error or "the site refused")


def test_an_error_is_reported_as_itself():
    assert reason("ERROR", error="the site did not answer in time.") == \
        "the site did not answer in time."
    assert reason("ERROR") == "the site did not answer."


def test_a_page_where_nothing_counts_says_which_button():
    assert "Choose the links" in reason("OK", accepted=0)


def test_a_page_already_collected_says_nothing_was_charged():
    assert "already in the archive" in reason("OK", accepted=5, duplicates=5)
    assert "already been collected" in reason("OK", accepted=5, unchanged=5)


def test_a_finished_run_is_a_run_row_and_a_manual_run_row():
    """The run table records the counts; the log gets the same sentence
    under a kind of its own, not under one of the failure kinds.

    The row is severity info, so the Log page lists it without counting it
    among the problems - a run that went well is not something to fix.
    """
    docs = [{"project_id": "p1", "tag": "xs_7_ab", "uri": "https://flats.example/rent/1"}]
    message = manualrun.manual_run_message(docs)
    assert message.startswith("manual run: 1 document submitted to 1 project")
    assert "xs_7_ab" in message

    row = errorlog.manual_run_row(message=message, uri="https://flats.example/rent")
    assert row["kind"] == "MANUAL_RUN" and "MANUAL_RUN" in errorlog.KINDS
    assert row["severity"] == "info"
    assert row["message"] == message
    assert row["uri"] == "https://flats.example/rent"
    assert errorlog.LABELS["MANUAL_RUN"] == "Manual run"
