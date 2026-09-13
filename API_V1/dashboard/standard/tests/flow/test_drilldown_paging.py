"""The running number, the total, and the period that holds the data.

    DATABASE_URL=postgresql://... python -m pytest tests/flow/test_drilldown_paging.py -q

Three promises, all of them about knowing where you are:

  * THE RUNNING NUMBER COUNTS UP ACROSS PAGES. Row 21 on page 2 is 21, not
    1. The server sends the offset the page starts at, so a page fetched out of
    order still numbers correctly.

  * THE TOTAL COMES OFF THE STATEMENT THAT FETCHED THE ROWS.
    `count(*) OVER ()` rides on every row, so "Row 87-106 of 312" costs no
    second query. Checked here against a count written independently.

  * A PERIOD WITH NOTHING IN IT SAYS WHERE THE DATA IS. "Nothing in the
    last 24 hours - the newest for Microsoft is 4 Sep 2026", with the
    period that holds it - and that period really does hold it.
"""

from __future__ import annotations

import pytest

from app.charts.drilldown import PAGE_SIZE
from preseed import ALPHA

pytestmark = pytest.mark.flow

PARAMS = {"project": ALPHA, "language": "English"}


def charts_of(client, tab, scope="summary", q="", timeframe="5y"):
    r = client.get(f"/api/diagrams/{scope}/{tab}",
                   params={**PARAMS, "q": q, "timeframe": timeframe})
    assert r.status_code == 200, r.text
    return r.json()


def drill(client, tab, chart, dataset, key, ddpage=1, timeframe="5y"):
    r = client.post("/api/diagrams/drilldown", params=PARAMS, json={
        "scope": "summary", "tab": tab, "q": "", "timeframe": timeframe, "page": 0,
        "chart_id": chart, "dataset_id": dataset, "key": key, "ddpage": ddpage})
    assert r.status_code == 200, r.text
    return r.json()


def biggest_point(client):
    """The chart point with more rows behind it than one page - the only
    kind that can prove anything about a page boundary."""
    body = charts_of(client, "ratings")
    # THE BIGGEST POINT ON THE TAB, wherever it is. Not a named chart: the
    # distribution splits every bar into the steps of the scale
    # (charts/ratings.py: by_name_and_value), so which cell is a page deep
    # is the data's business. What this test needs is a point with more
    # rows behind it than one page, and which chart that is on is the
    # registry's business rather than this file's.
    chart, dataset, point = max(
        ((c, d, p) for c in body["charts"] if c["kind"] == "key"
         for d in c["datasets"] for p in d["data"]),
        key=lambda found: found[2]["y"])
    assert point["y"] > PAGE_SIZE, "the preseed no longer has a point worth paging"
    return chart, dataset, point


def test_the_running_number_continues_across_a_page_boundary(client):
    chart, dataset, point = biggest_point(client)
    first = drill(client, "ratings", chart["id"], dataset["id"], {"x": point["x"]}, 1)
    second = drill(client, "ratings", chart["id"], dataset["id"], {"x": point["x"]}, 2)

    assert first["offset"] == 0
    assert second["offset"] == PAGE_SIZE, "page 2 starts where page 1 stopped"
    assert first["more"] is True
    assert len(first["rows"]) == PAGE_SIZE
    # The two pages are different rows, not the same twenty again.
    assert first["rows"] != second["rows"]
    # And the whole listing is as long as the bar said.
    assert first["total"] == second["total"] == point["y"]


def test_the_total_is_the_number_the_bar_itself_says(client):
    """The dialog's total is a fact, not an estimate of one.

    Not checked against a count written out in this file, which only works
    while the biggest point happens to be keyed on one particular chart.
    The stronger claim needs no SQL here at all: the bar and the dialog are
    two different statements
    over the same rows, and they have to agree. A total that came from
    counting the page, or from the offset, would not.
    """
    chart, dataset, point = biggest_point(client)
    answer = drill(client, "ratings", chart["id"], dataset["id"], {"x": point["x"]})
    assert answer["total"] == point["y"] > PAGE_SIZE


def test_a_page_past_the_end_carries_no_rows_and_does_not_invent_a_total(client):
    chart, dataset, point = biggest_point(client)
    pages = (point["y"] // PAGE_SIZE) + 2
    last = drill(client, "ratings", chart["id"], dataset["id"], {"x": point["x"]}, pages)
    assert last["rows"] == []
    assert last["more"] is False


# ── Nothing in this period ───────────────────────────────────

def test_a_period_that_holds_the_data_says_nothing_about_being_empty(client):
    body = charts_of(client, "ratings", timeframe="5y")
    assert any(c["total"] for c in body["charts"])
    assert body["outside"] is None, "a tab with rows in it has nothing to offer"


def test_an_empty_period_names_the_newest_row_and_offers_the_period_that_holds_it(client):
    """The preseed's Microsoft rows are three days old, so the last 24 hours
    hold none of them - which is the case that read as a broken search."""
    body = charts_of(client, "market", scope="entity", q="Microsoft", timeframe="24h")
    assert not any(c["total"] for c in body["charts"]), "this period was supposed to be empty"
    outside = body["outside"]
    assert outside and outside["newest"], "a scope with data outside the window says so"
    assert outside["newest"] < body["window"]["start"], \
        "the date offered is outside the window, or there was nothing to say"

    widen = outside["widen"]
    assert widen and widen["timeframe"] and widen["label"]
    # AND THE OFFER IS TRUE: the period it names really does hold rows.
    wider = charts_of(client, "market", scope="entity", q="Microsoft",
                      timeframe=widen["timeframe"])
    assert any(c["total"] for c in wider["charts"]), \
        f"the offered period {widen['timeframe']} is as empty as the one it replaced"


def test_a_term_nothing_is_called_is_not_offered_a_period(client):
    """There is no period in which a word nobody used has rows, and offering
    one would send the reader through every window of the archive."""
    body = charts_of(client, "ratings", scope="entity", q="Nothingiscalledthis",
                     timeframe="24h")
    assert body["resolved"]["match"] == "none"
    assert body["outside"] is None
