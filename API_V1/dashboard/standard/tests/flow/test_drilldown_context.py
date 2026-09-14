"""The rows behind a point are the rows that point counted.

    DATABASE_URL=postgresql://... python -m pytest tests/flow/test_drilldown_context.py -q

A drilldown is a promise: this bar says eleven, so here are the eleven. The
promise is easy to break - a second query written by hand, a scope predicate
forgotten, a bucket boundary off by an hour - and the break is invisible,
because eleven plausible rows look exactly like the right eleven.

So this file checks the promise three ways:

  * the FIRST point of EVERY dataset of EVERY chart of every tab opens with
    rows in it, and for the charts that count rows there are exactly as many
    rows as the point says (up to the page size);
  * two cases are verified against SQL written independently here, so a
    mistake shared by the chart and the drilldown would still be caught;
  * a scoped page only ever lists rows that belong to the scope - checked
    against the entity ids the bucket resolves to.
"""

from __future__ import annotations

import pytest

import re

from app import charts, sqlbuild
from app.charts.drilldown import PAGE_SIZE

IDENTIFIER = re.compile(sqlbuild.MACHINE_NAME_REGEX, re.I)
from preseed import ALPHA

pytestmark = pytest.mark.flow

PARAMS = {"project": ALPHA, "language": "English"}
TIMEFRAME = "1y"

# Charts whose datasets are not a count of rows: two measures over the same
# rows, or statistics of a column. Their row count has no reason to equal
# the number in the chart.
NOT_ROW_COUNTS = {("entities", "per_period"), ("market", "per_period"),
                  ("attributes", "value_stats")}
# Charts that count PAIRS - one event named three entities - and list the
# events behind them.
COUNTS_PAIRS = {("events", "by_entity"), ("events", "type_matrix")}


def tab_charts(client, scope, tab, q=""):
    r = client.get(f"/api/diagrams/{scope}/{tab}",
                   params={**PARAMS, "q": q, "timeframe": TIMEFRAME})
    assert r.status_code == 200, r.text
    return r.json()["charts"]


def key_of(chart, point):
    if chart["drill"] == "bucket":
        return {"bucket": point["x"]}
    if chart["drill"] == "xy":
        return {"x": point["x"], "y": point["y"]}
    return {"x": point["x"]}


def drill(client, tab, chart, dataset, point, *, scope="summary", q="", ddpage=1):
    body = {"scope": scope, "tab": tab, "q": q, "timeframe": TIMEFRAME, "page": 0,
            "chart_id": chart["id"], "dataset_id": dataset["id"],
            "key": key_of(chart, point), "ddpage": ddpage}
    r = client.post("/api/diagrams/drilldown", params=PARAMS, json=body)
    assert r.status_code == 200, r.text
    return r.json()


def cell(row, key):
    """One cell of a shaped row, as the words it shows. The colour travels
    beside the word (`scale`, `step`); the word is the value, and it is what
    a test compares."""
    return " / ".join(p["text"] for p in row["cells"][key] if p["text"])


def titles(answer):
    """The document each row came from, by the name the archive gave it."""
    return {r["link"]["title"] for r in answer["rows"]}


def uris(answer):
    """The document each row came from, by its ADDRESS.

    A title is not a key. `sources.text_name` holds an identifier on a real
    archive - `src_1416664`, then a checksum - and the drilldown shows the
    URL's own words instead of printing one (app/charts/drilldown.py:
    readable_title), so a test that compares titles with the raw column is
    comparing two different things. The URI is what both agree on."""
    return {r["link"]["uri"] for r in answer["rows"]}


def first_points(chart):
    """The first point of every dataset that has one - what a reader clicks."""
    out = []
    for dataset in chart["datasets"]:
        for point in dataset["data"]:
            value = point["v"] if chart["kind"] == "matrix" else point["y"]
            if value:
                out.append((dataset, point, value))
                break
    return out


# ── Every chart, every dataset ───────────────────────────────

@pytest.mark.parametrize("tab", charts.TAB_IDS)
def test_the_first_point_of_every_dataset_opens_with_its_own_rows(client, tab):
    checked = 0
    for chart in tab_charts(client, "summary", tab):
        for dataset, point, value in first_points(chart):
            answer = drill(client, tab, chart, dataset, point)
            assert answer["rows"], f"{tab}/{chart['id']}/{dataset['id']} says {value} and lists none"
            assert answer["ddpage"] == 1
            assert answer["page_size"] == PAGE_SIZE
            for row in answer["rows"]:
                # A ROW A PERSON CAN READ: the document it came from, named
                # by its domain, and something in every column the tab
                # declares. An empty cell is allowed (the archive did not
                # say); a row with nothing in any of them is not a row.
                assert row["link"]["domain"] or row["link"]["title"], \
                    "a row with no source is a row nobody can check"
                assert any(cell(row, c["key"]) for c in answer["columns"]), \
                    f"{tab}/{chart['id']}: a row with every cell empty"
                assert set(row["cells"]) == {c["key"] for c in answer["columns"]}, \
                    "the cells and the columns of one answer must be the same set"
            if (tab, chart["id"]) not in NOT_ROW_COUNTS | COUNTS_PAIRS:
                assert len(answer["rows"]) == min(int(value), PAGE_SIZE), \
                    f"{tab}/{chart['id']}/{dataset['id']}: {value} counted, {len(answer['rows'])} listed"
            checked += 1
    assert checked, f"{tab} had nothing to drill into"


@pytest.mark.parametrize("tab", charts.TAB_IDS)
def test_the_same_holds_for_a_scoped_page(client, tab):
    for chart in tab_charts(client, "entity", tab, q="Apple"):
        for dataset, point, value in first_points(chart):
            answer = drill(client, tab, chart, dataset, point, scope="entity", q="Apple")
            assert answer["rows"], f"{tab}/{chart['id']}/{dataset['id']}"
            assert answer["resolved"]["match"] == "bucket"


def test_a_page_past_the_last_one_is_empty_and_says_there_is_no_more(client):
    chart = next(c for c in tab_charts(client, "summary", "sources") if c["id"] == "by_type")
    dataset = chart["datasets"][0]
    point = next(p for p in dataset["data"] if p["y"])
    first = drill(client, "sources", chart, dataset, point)
    assert first["more"] is False, "two documents fit on one page of twenty"
    second = drill(client, "sources", chart, dataset, point, ddpage=2)
    assert second["rows"] == []
    assert second["more"] is False


def test_a_dataset_narrows_the_point_it_was_clicked_on(client):
    """The negative bar of a period lists negative ratings only."""
    chart = next(c for c in tab_charts(client, "summary", "ratings")
                 if c["id"] == "values_per_period")
    negative = next(d for d in chart["datasets"] if d["id"] == "negative")
    positive = next(d for d in chart["datasets"] if d["id"] == "positive")
    point = next(p for p in negative["data"] if p["y"])
    same_period = next(p for p in positive["data"] if p["x"] == point["x"])

    bad = drill(client, "ratings", chart, negative, point)
    good = drill(client, "ratings", chart, positive, same_period)
    assert {cell(r, "value") for r in bad["rows"]} <= {"Egregious", "Very Bad", "Bad"}
    assert {cell(r, "value") for r in good["rows"]} <= {"Good", "Very Good", "Excellent"}
    # AND THE COLOUR IS THE SCALE'S OWN, from the number the archive
    # carries: `rating_values.float_value` is the step, not the label.
    for row in bad["rows"]:
        part = row["cells"]["value"][0]
        assert part["scale"] == "valence" and part["step"] < 0, part
    for row in good["rows"]:
        part = row["cells"]["value"][0]
        assert part["scale"] == "valence" and part["step"] > 0, part


# ── Verified against SQL written here ────────────────────────

def test_a_category_drilldown_matches_a_count_written_independently(client, conn):
    chart = next(c for c in tab_charts(client, "summary", "sources") if c["id"] == "by_type")
    dataset = chart["datasets"][0]
    point = next(p for p in dataset["data"] if p["x"] == "News article")
    answer = drill(client, "sources", chart, dataset, point)

    row = conn.execute(
        """SELECT count(*) AS n FROM processed_data.sources s
            WHERE s.text_project = %(p)s AND s.text_language = %(l)s
              AND s.text_type = 'News article'
              AND COALESCE(s.date_written, s.date_commissioned) >= %(start)s
              AND COALESCE(s.date_written, s.date_commissioned) <  %(end)s""",
        {"p": ALPHA, "l": "English",
         "start": _window(client, "sources")["start"], "end": _window(client, "sources")["end"]},
    ).fetchone()
    assert row["n"] == point["y"] == len(answer["rows"])
    # The one with a title carries it; the one without is called by what its
    # own address says, and neither is called by its id.
    assert titles(answer) == {"Inside the new battery", "Springfield harvest"}
    # The number the whole listing runs to, off the same statement.
    assert answer["total"] == row["n"] and answer["offset"] == 0


def test_a_bucket_drilldown_stays_inside_the_bucket(client, conn):
    chart = next(c for c in tab_charts(client, "summary", "sources") if c["id"] == "per_period")
    dataset = chart["datasets"][0]
    point = next(p for p in dataset["data"] if p["y"])
    answer = drill(client, "sources", chart, dataset, point)

    rows = conn.execute(
        """SELECT s.text_uri, s.text_name FROM processed_data.sources s
            WHERE s.text_project = %(p)s AND s.text_language = %(l)s
              AND date_trunc('month', COALESCE(s.date_written, s.date_commissioned)) = %(bucket)s""",
        {"p": ALPHA, "l": "English", "bucket": point["x"]}).fetchall()
    assert {r["text_uri"] for r in rows} == uris(answer)
    # And no row is titled with the identifier the archive stored.
    assert not (titles(answer) & {r["text_name"] for r in rows
                                  if IDENTIFIER.match(r["text_name"] or "")})


def test_a_scoped_drilldown_only_lists_rows_of_the_scope(client, conn):
    chart = next(c for c in tab_charts(client, "entity", "sources", q="Apple")
                 if c["id"] == "per_period")
    dataset = chart["datasets"][0]
    listed = set()
    for point in dataset["data"]:
        if not point["y"]:
            continue
        answer = drill(client, "sources", chart, dataset, point, scope="entity", q="Apple")
        listed |= uris(answer)

    allowed = conn.execute(
        """SELECT DISTINCT s.text_uri FROM processed_data.sources s
            WHERE s.text_project = %(p)s AND s.text_language = %(l)s
              AND EXISTS (SELECT 1 FROM processed_data.entities e
                           WHERE e.text_project = s.text_project
                             AND e.text_language = s.text_language
                             AND e.text_task_id = s.text_task_id
                             AND e.text_fk_source_id = s.text_source_id
                             AND e.text_entity_id IN ('ent:apple', 'ent:apple-inc'))""",
        {"p": ALPHA, "l": "English"}).fetchall()
    assert listed == {r["text_uri"] for r in allowed}
    assert "src:harvest" not in listed, "the harvest document is about Apple the fruit"


def test_a_matrix_drilldown_takes_both_axes(client):
    chart = next(c for c in tab_charts(client, "summary", "market")
                 if c["id"] == "sentiment_matrix")
    dataset = chart["datasets"][0]
    point = next(p for p in dataset["data"] if p["v"])
    answer = drill(client, "market", chart, dataset, point)
    assert len(answer["rows"]) == min(int(point["v"]), PAGE_SIZE)
    for row in answer["rows"]:
        # One cell, both horizons, short term first - the shape the old
        # build used ("Rising / Neutral") and the reason the column is
        # headed "Sentiment ST / LT".
        short, long_ = row["cells"]["sentiment"]
        assert short["text"] == point["x"]
        assert long_["text"] == point["y"]


# ── Refusals ─────────────────────────────────────────────────

def _window(client, tab, scope="summary"):
    r = client.get(f"/api/diagrams/{scope}/{tab}", params={**PARAMS, "timeframe": TIMEFRAME})
    return r.json()["window"]


def _post(client, **overrides):
    body = {"scope": "summary", "tab": "sources", "q": "", "timeframe": TIMEFRAME, "page": 0,
            "chart_id": "by_type", "dataset_id": "count", "key": {"x": "News article"}, "ddpage": 1}
    body.update(overrides)
    return client.post("/api/diagrams/drilldown", params=PARAMS, json=body)


def test_an_unknown_chart_a_wrong_key_and_a_wrong_dataset_are_all_400(client):
    assert _post(client, chart_id="by_colour").status_code == 400
    assert _post(client, key={"bucket": "2026-01-01T00:00:00Z"}).status_code == 400
    assert _post(client, key={"x": "News article", "y": "extra"}).status_code == 400
    assert _post(client, dataset_id="something-else").status_code == 400
    bad = _post(client, chart_id="by_colour").json()
    assert bad["error"] and bad["hint"]


def test_the_csv_is_the_same_rows_with_a_byte_order_mark(client):
    chart = next(c for c in tab_charts(client, "summary", "sources") if c["id"] == "by_type")
    dataset = chart["datasets"][0]
    point = next(p for p in dataset["data"] if p["x"] == "News article")
    answer = drill(client, "sources", chart, dataset, point)

    r = client.get("/api/diagrams/drilldown.csv", params={
        **PARAMS, "scope": "summary", "tab": "sources", "chart_id": "by_type",
        "dataset_id": "count", "timeframe": TIMEFRAME, "key_x": "News article"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "attachment; filename=" in r.headers["content-disposition"]
    text = r.content.decode("utf-8")
    assert text.startswith("﻿"), "Excel needs the BOM to read UTF-8"
    lines = [line for line in text.splitlines() if line.strip()]
    assert len(lines) == 1 + len(answer["rows"])
    # The file's header is the dialog's own columns, per tab, with the
    # document in front of them.
    header = lines[0].lstrip("\ufeff").split(",")
    assert header[:5] == ["row", "domain", "source", "source_uri", "entity"]
    assert header[5:] == [c["key"] for c in answer["columns"]]
    for row in answer["rows"]:
        assert row["link"]["title"] in text
        assert row["link"]["domain"] in text
