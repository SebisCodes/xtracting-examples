"""Every view as a file: GET /api/export/<view>.csv and .json.

    DATABASE_URL=postgresql://... python -m pytest tests/flow/test_export.py -q

The Export menu in the top bar is on every page, and until app/routers/
api_export.py existed both of its links answered 404 - a control that fails
when a customer presses it. What is checked here is that they now answer, and
that the answer is the same rows the view itself would show.

Three properties run through the whole file:

  * a CSV starts with the byte order mark, or Excel reads it as Latin-1 and
    turns Zürich into ZÃ¼rich;
  * the file is served as an attachment with a name that says which project,
    which language and when - a folder of "export.csv" files is a folder of
    files nobody can tell apart;
  * the rows are the VIEW's rows. Each assertion below compares a count or a
    value against the API the view itself calls, so the two cannot drift.
"""

from __future__ import annotations

import csv
import io
import json
import re

import pytest

from preseed import ALPHA, BETA

pytestmark = pytest.mark.flow

EN = {"project": ALPHA, "language": "English"}
DE = {"project": ALPHA, "language": "German"}
BETA_EN = {"project": BETA, "language": "English"}

BOM = "﻿"


# ── helpers ─────────────────────────────────────────────────────────────

def get(client, view: str, fmt: str, params: dict | None = None, expect: int = 200):
    r = client.get(f"/api/export/{view}.{fmt}", params={**EN, **(params or {})})
    assert r.status_code == expect, f"{view}.{fmt}: {r.status_code} {r.text[:300]}"
    return r


def rows_of_csv(response) -> list[dict[str, str]]:
    text = response.content.decode("utf-8")
    assert text.startswith(BOM), "Excel needs the byte order mark to read UTF-8"
    return list(csv.DictReader(io.StringIO(text[len(BOM):])))


def csv_rows(client, view: str, params: dict | None = None) -> list[dict[str, str]]:
    return rows_of_csv(get(client, view, "csv", params))


def json_body(client, view: str, params: dict | None = None) -> dict:
    return json.loads(get(client, view, "json", params).content.decode("utf-8"))


# Every name the menu on a page can ask for, with enough parameters to make
# it produce rows, and the first column its table has to carry.
CASES: list[tuple[str, dict, str]] = [
    ("dashboard-stats", {}, "id"),
    ("dashboard", {}, "id"),
    ("activities", {}, "table"),
    ("latest-events", {}, "id"),
    ("query", {"terms": "battery", "address": "Cupertino, California, USA"}, "kind"),
    ("events", {"by": "entity", "q": "Apple"}, "id"),
    ("diagrams", {"scope": "entity", "tab": "connections", "q": "Apple", "timeframe": "1y"}, "tab"),
    # "row", not "date": the drilldown's file carries the dialog's own
    # columns now, per tab, and the first of them is the running number the
    # rows are counted by (app/charts/drilldown.py: csv_columns).
    ("drilldown", {"scope": "summary", "tab": "sources", "chart_id": "by_domain",
                   "timeframe": "1y", "key_x": "news.example.com"}, "row"),
    ("map", {"q": "Apple", "levels": "2"}, "entity_id"),
    ("heatmap", {}, "lat"),
    ("graph", {"q": "Apple"}, "from_id"),
    ("buckets", {}, "bucket_id"),
    ("colour-groups", {}, "id"),
    ("colours", {}, "id"),
    ("colour-types", {}, "type"),
    ("logs", {"range": "all"}, "date"),
    ("logs-errors", {"range": "all"}, "date"),
    ("logs-runs", {"range": "all"}, "date"),
]


# ── the contract every view keeps ───────────────────────────────────────

def test_every_view_the_menu_can_ask_for_is_answered(client):
    """The Export menu is rendered from `view` in _layout.html, so the names
    it can ask for are the ids in routers/pages.py VIEWS. A name that is not
    in the registry is a dead control on that page - the exact defect this
    router closes - so the list is checked against the pages themselves."""
    from app.routers import api_export, pages

    known = set(api_export.VIEWS)
    # sources is older than this router and lives in api_sources; see there.
    known.add("sources")
    missing = [view for view, _, _ in pages.VIEWS if view not in known]
    assert not missing, f"no export for {missing}"


@pytest.mark.parametrize("view,params,first_column", CASES, ids=[c[0] for c in CASES])
def test_a_view_exports_as_csv_and_as_json(client, view, params, first_column):
    answer = get(client, view, "csv", params)
    assert answer.headers["content-type"].startswith("text/csv")
    assert "charset=utf-8" in answer.headers["content-type"]
    disposition = answer.headers["content-disposition"]
    assert disposition.startswith("attachment;")
    assert re.search(rf'filename="xtracting-{view}-_preseed_Alpha-English-\d{{8}}-\d{{4}}\.csv"',
                     disposition), disposition

    text = answer.content.decode("utf-8")
    assert text.startswith(BOM)
    header = text[len(BOM):].splitlines()[0]
    assert header.split(",")[0] == first_column, header
    # Every one of these has something to show in the preseed. An export that
    # is empty for a reason nobody can see is the failure mode this file is
    # here to catch.
    table = rows_of_csv(answer)
    assert table, f"{view} exported no rows"

    body = json_body(client, view, params)
    assert body["export"]["view"] == view
    assert body["export"]["project"] == ALPHA and body["export"]["language"] == "English"
    assert body["export"]["columns"][0] == first_column
    assert body["export"]["rows"] == len(table)
    assert len(body["rows"]) == len(table)
    # The two formats carry the same table, not two tables.
    assert list(body["rows"][0]) == body["export"]["columns"]
    assert [str(k) for k in table[0]] == body["export"]["columns"]


@pytest.mark.parametrize("view,params,_first", CASES, ids=[c[0] for c in CASES])
def test_the_json_says_what_the_file_was_cut_with(client, view, params, _first):
    """The envelope is what turns a spreadsheet into a record: which view,
    which project and language, which filters, and when. An export a reader
    cannot place is an export they cannot act on."""
    body = json_body(client, view, params)
    envelope = body["export"]
    assert envelope["format"] == "json"
    assert envelope["generated"].startswith("20")
    assert envelope["filters"]["project"] == ALPHA
    assert envelope["filters"]["language"] == "English"
    for name, value in params.items():
        assert envelope["filters"].get(name) == value, envelope["filters"]
    assert envelope["truncated"] is False
    assert isinstance(body["context"], dict)


def test_a_name_nobody_exports_is_a_404_and_the_names_are_listed(client):
    """The paths are registered one by one - not behind a `{view}` catch-all,
    which would also swallow /api/export/sources.csv - so a name nobody
    exports is simply not a route. `/api/export/views` is the index that says
    which names there are, and the menu can only ask for those."""
    assert client.get("/api/export/nonsense.csv", params=EN).status_code == 404

    listing = client.get("/api/export/views").json()
    assert listing["formats"] == ["csv", "json"]
    named = {v["view"] for v in listing["views"]}
    assert {"diagrams", "map", "graph", "logs-runs", "buckets"} <= named
    assert listing["elsewhere"][0]["view"] == "sources"


# ── the rows are the view's rows ────────────────────────────────────────

def test_the_counters_are_the_ones_the_home_page_draws(client):
    api = client.get("/api/dashboard/stats", params=EN).json()
    table = {r["id"]: r for r in csv_rows(client, "dashboard-stats")}
    assert len(table) == len(api["tables"])
    for wanted in api["tables"]:
        row = table[wanted["id"]]
        for period in ("hour", "day", "week", "month", "year"):
            assert row[period] == str(wanted[period]), (wanted["id"], period)


def test_the_events_file_holds_every_page_not_the_first_twenty(client):
    """Row paging does not travel into the file. Somebody who presses Export
    wants what the filters match, not the twenty rows that happen to be on
    the screen - and page=1 in the URL must not silently cut page 0 out."""
    api = client.get("/api/events", params={**EN, "by": "entity", "q": ""}).json()
    table = csv_rows(client, "events", {"by": "entity"})
    assert len(table) == api["total"] >= len(api["events"])
    assert csv_rows(client, "events", {"by": "entity", "page": "1"}) == table

    # And the filters DO travel: a type nothing has is an empty table, not
    # the whole archive.
    launches = csv_rows(client, "events", {"by": "type", "q": "Product launch"})
    assert launches and all(r["type"] == "Product launch" for r in launches)
    assert len(launches) < len(table)


def test_a_search_exports_the_matches_and_says_what_it_searched_for(client):
    body = json_body(client, "query", {"terms": "battery",
                                       "address": "Cupertino, California, USA"})
    assert body["context"]["where"]["kind"] == "place"
    assert body["context"]["where"]["place"]["city"] == "Cupertino"
    assert body["context"]["terms"] == ["battery"]
    assert body["export"]["rows"] == body["context"]["total"]
    assert all("battery" in r["matched_terms"] for r in body["rows"])


def test_a_search_with_no_place_is_the_same_refusal_the_page_gets(client):
    """The export runs the view's own function, so it fails the view's own
    way: a sentence naming the problem and the fix, not an empty file that
    reads like an empty archive."""
    r = client.get("/api/export/query.csv", params={**EN, "terms": "battery"})
    assert r.status_code == 400
    detail = r.json().get("detail", r.json())
    assert "place" in detail["error"]
    assert detail["hint"]


def test_the_diagrams_file_is_the_charts_that_are_on_the_screen(client):
    params = {"scope": "entity", "tab": "connections", "q": "Apple", "timeframe": "1y"}
    api = client.get("/api/diagrams/entity/connections",
                     params={**EN, "q": "Apple", "timeframe": "1y"}).json()
    body = json_body(client, "diagrams", params)
    drawn = sum(len(d["data"]) for c in api["charts"] for d in c["datasets"])
    assert body["export"]["rows"] == drawn
    assert body["context"]["window"]["label"] == api["window"]["label"]
    assert body["context"]["resolved"]["match"] == api["resolved"]["match"]
    assert {r["chart_id"] for r in body["rows"]} == {c["id"] for c in api["charts"]}

    # `page` here steps the PERIOD, not a row page, so it has to travel.
    earlier = json_body(client, "diagrams", {**params, "page": "1"})
    assert earlier["context"]["window"]["start"] < body["context"]["window"]["start"]


def test_the_tab_defaults_to_the_one_the_page_opens_on(client):
    from app.charts import TAB_IDS

    body = json_body(client, "diagrams", {"scope": "summary", "timeframe": "1y"})
    assert body["context"]["tab"] == TAB_IDS[0]


def test_a_drilldown_exports_the_rows_behind_one_point(client):
    params = {"scope": "summary", "tab": "sources", "chart_id": "by_domain",
              "timeframe": "1y", "key_x": "news.example.com"}
    table = csv_rows(client, "drilldown", params)
    assert table and all(r["source_uri"].startswith("https://news.example.com")
                         or "news.example.com" in r["source_uri"] for r in table)
    body = json_body(client, "drilldown", params)
    assert body["context"]["chart_id"] == "by_domain"
    assert body["context"]["key"]["x"] == "news.example.com"

    # The same file the dialog's own "Download CSV" produces.
    dialog = client.get("/api/diagrams/drilldown.csv", params={**EN, **params})
    assert dialog.status_code == 200
    assert (dialog.content.decode("utf-8").splitlines()[1:]
            == get(client, "drilldown", "csv", params).content.decode("utf-8").splitlines()[1:])


def test_a_drilldown_without_a_chart_says_which_two_parameters_are_missing(client):
    r = client.get("/api/export/drilldown.csv", params=EN)
    assert r.status_code == 400
    detail = r.json().get("detail", r.json())
    assert "chart_id" in detail["hint"] and "tab" in detail["hint"]


def test_the_map_exports_the_markers_and_keeps_the_lines_in_the_json(client):
    params = {"q": "Apple", "levels": "2"}
    api = client.get("/api/map/entity", params={**EN, **params}).json()
    table = csv_rows(client, "map", params)
    assert len(table) == len(api["markers"])
    assert {r["address"] for r in table} == {m["address"] for m in api["markers"]}
    body = json_body(client, "map", params)
    assert len(body["context"]["connections"]) == len(api["connections"])
    # Untick a type on the page and the lines it drew go out of the file too.
    hidden = json_body(client, "map", {**params, "hide": "Competitor,Subsidiary,Parent company"})
    assert len(hidden["context"]["connections"]) < len(api["connections"])
    assert hidden["context"]["hidden_types"] == ["Competitor", "Parent company", "Subsidiary"]


def test_the_graph_exports_one_row_per_edge_with_the_colour_it_is_drawn_in(client):
    api = client.get("/api/graph/neighbours", params={**EN, "q": "Apple"}).json()
    table = csv_rows(client, "graph", {"q": "Apple"})
    assert len(table) == sum(len(n["types"]) for n in api["neighbours"])
    foxconn = [r for r in table if r["to"] == "Foxconn"]
    assert foxconn, table
    # The far end's role, and the colour the edge carries on the drawing.
    assert {r["type"] for r in foxconn} >= {"Supplier"}
    assert all(re.fullmatch(r"#[0-9A-Fa-f]{6}", r["colour"]) for r in table)
    assert all(r["from"] == "Apple" for r in table)


def test_a_graph_without_a_subject_says_so(client):
    r = client.get("/api/export/graph.csv", params=EN)
    assert r.status_code == 400
    assert "q=" in r.json().get("detail", r.json())["hint"]


def test_a_bucket_exports_one_row_per_member(client):
    api = client.get("/api/buckets", params=EN).json()
    table = csv_rows(client, "buckets")
    apple = [r for r in table if r["bucket"] == "Apple"]
    assert {r["member"] for r in apple} == {"Apple Inc.", "Apple"}
    assert all(r["member_type"] == "Company" for r in apple)
    assert "Fruit" not in {r["member_type"] for r in table}
    assert len(table) == sum(max(1, len(b["members"])) for b in api["items"])


def test_the_colour_groups_export_carries_the_hex_a_reader_can_check(client):
    api = client.get("/api/colour-groups", params=EN).json()
    table = csv_rows(client, "colour-groups")
    assert len(table) == len(api["groups"])
    for row, group in zip(table, api["groups"]):
        assert row["key"] == group["key"]
        assert row["colour"] == group["colour"]
    assert csv_rows(client, "colours") == table          # the page's own name
    types = csv_rows(client, "colour-types")
    assert types and {"Competitor", "Supplier"} <= {r["type"] for r in types}
    assert csv_rows(client, "colour-types", {"unassigned": "true"}) != types


def test_the_log_exports_the_open_tab(client):
    problems = csv_rows(client, "logs", {"range": "all"})
    runs = csv_rows(client, "logs", {"range": "all", "tab": "runs"})
    assert problems and runs
    assert list(problems[0])[:3] == ["date", "kind", "kind_label"]
    assert list(runs[0])[:3] == ["date", "source_id", "name"]
    assert problems == csv_rows(client, "logs-errors", {"range": "all"})
    assert runs == csv_rows(client, "logs-runs", {"range": "all"})

    api = client.get("/api/logs/runs", params={"range": "all", "page_size": 500}).json()
    assert len(runs) == api["total"]
    # A filter the page can set narrows the file the same way.
    tests_only = csv_rows(client, "logs-runs", {"range": "all", "status": "TEST"})
    assert tests_only and all(r["status"] == "TEST" for r in tests_only)
    assert len(tests_only) < len(runs)


# ── the pair the whole dashboard binds ──────────────────────────────────

def test_a_file_holds_one_project_in_one_language(client):
    """Every query in the dashboard binds project AND language; an export
    that did not would hand a reader a spreadsheet mixing two archives."""
    english = csv_rows(client, "events", {"by": "entity"})
    german = rows_of_csv(client.get("/api/export/events.csv",
                                    params={**DE, "by": "entity"}))
    assert len(english) == len(german)
    assert {r["type"] for r in english} != {r["type"] for r in german}
    assert "Produkteinführung" in {r["type"] for r in german}

    beta = rows_of_csv(client.get("/api/export/events.csv", params={**BETA_EN, "by": "entity"}))
    assert {r["id"] for r in beta}.isdisjoint({r["id"] for r in english})

    name = client.get("/api/export/events.csv",
                      params={**DE, "by": "entity"}).headers["content-disposition"]
    assert "-_preseed_Alpha-German-" in name, name


def test_a_pair_the_archive_does_not_have_is_the_same_400_as_everywhere(client):
    r = client.get("/api/export/events.csv",
                   params={"project": ALPHA, "language": "Klingon"})
    assert r.status_code == 400
    detail = r.json().get("detail", r.json())
    assert "Klingon" in detail["error"] and "English" in detail["hint"]


def test_the_row_limit_is_named_where_a_reader_can_find_it(client):
    """A file cut off at the limit says so in a header, because a CSV cannot
    carry a note without ceasing to be a CSV."""
    from app.routers import api_export

    assert api_export.MAX_ROWS >= 1000
    answer = get(client, "events", "csv", {"by": "entity"})
    assert "X-Export-Truncated" not in answer.headers
    assert client.get("/api/export/views").json()["row_limit"] == api_export.MAX_ROWS


def test_the_sources_export_is_still_the_one_api_sources_owns(client):
    """This router registers explicit paths rather than a catch-all, so the
    older /api/export/sources.csv is not shadowed by it."""
    r = client.get("/api/export/sources.csv")
    if r.status_code == 404:
        pytest.skip("api_sources is not loaded (crawlkit not importable)")
    assert r.headers["content-disposition"].startswith('attachment; filename="xtracting-sources-')
    assert r.content.decode("utf-8").startswith(BOM)
