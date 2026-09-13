"""Every scope, every tab, against the preseeded archive.

    DATABASE_URL=postgresql://... python -m pytest tests/flow/test_diagrams_all_tabs.py -q

Six ways of asking (summary, entity, source, location, market, events) times
eight tabs is forty-eight answers, and every one of them has to be a chart
rather than a stack trace. The numbers below come from tests/preseed/preseed.py
and from nothing else: six Alpha documents written 2 h, 3 d, 20 d, 60 d,
200 d and 2 years ago, one Beta document, and events dated from two years
back to a month ahead.

The three facts this file pins beyond "it answers":

  * the term is resolved once and the answer says what it resolved TO -
    "Apple" is a bucket of two companies here, and a reader has to be able
    to see that;
  * the two languages of a project are the same rows twice, so they count
    the same - only the labels differ;
  * the location scope really isolates: Springfield in Ontario is not
    Springfield in Illinois, which is the disambiguation the preseed exists
    for.
"""

from __future__ import annotations

import pytest

from app import charts
from preseed import ALPHA, BETA

pytestmark = pytest.mark.flow

# What each scope is asked about on the preseed.
TERMS = {
    "summary": "",
    "entity": "Apple",                              # a bucket: Apple Inc. + Apple
    "source": "news.example.com",                   # two documents from one host
    "location": "Springfield, Ontario, Canada",     # Springfield Mills, not Works
    "market": "Apple Inc.",                          # the object axis is the ENTITY

    "events": "Regulatory action",
}
SCOPES = tuple(TERMS)
TABS = charts.TAB_IDS


def get(client, scope, tab, *, project=ALPHA, language="English", timeframe="1y", page=0,
        q=None, axis=None):
    params = {"project": project, "language": language, "timeframe": timeframe, "page": page,
              "q": TERMS[scope] if q is None else q}
    if axis:
        params["axis"] = axis
    r = client.get(f"/api/diagrams/{scope}/{tab}", params=params)
    assert r.status_code == 200, r.text
    return r.json()


def totals(body):
    return {c["id"]: c["total"] for c in body["charts"]}


# ── Shape ────────────────────────────────────────────────────

@pytest.mark.parametrize("tab", TABS)
@pytest.mark.parametrize("scope", SCOPES)
def test_every_scope_and_tab_answers_with_charts(client, scope, tab):
    body = get(client, scope, tab)
    assert body["scope"] == scope and body["tab"] == tab
    assert body["resolved"]["kind"] == scope
    assert body["window"]["timeframe"] == "1y"
    assert len(body["charts"]) == len(charts.charts_for(tab))
    for chart, spec in zip(body["charts"], charts.charts_for(tab)):
        assert chart["id"] == spec.id and chart["kind"] == spec.kind
        assert chart["title"] == spec.title
        # Every card explains itself, whatever the scope: the sentence under
        # the title is what a first-time reader has instead of a guess.
        assert chart["description"], f"{tab}/{spec.id} has no purpose sentence"
        assert not chart["note"], f"{tab}/{spec.id}: {chart['note']}"
        assert chart["datasets"], f"{tab}/{spec.id} has no datasets"
        for dataset in chart["datasets"]:
            assert dataset["colour"].startswith("#")
            assert isinstance(dataset["data"], list)


@pytest.mark.parametrize("timeframe", ["24h", "7d", "1y"])
@pytest.mark.parametrize("tab", TABS)
def test_the_timeframes_the_toolbar_offers_all_work(client, tab, timeframe):
    body = get(client, "summary", tab, timeframe=timeframe)
    window = body["window"]
    assert window["timeframe"] == timeframe
    assert window["buckets"], "a chart with no buckets has no x axis"
    for chart in body["charts"]:
        if chart["kind"] == "time":
            for dataset in chart["datasets"]:
                assert len(dataset["data"]) == len(window["buckets"]), \
                    "a period with no rows is a zero, not a gap"


@pytest.mark.parametrize("tab", TABS)
def test_every_tab_of_the_summary_has_something_to_show_over_a_year(client, tab):
    body = get(client, "summary", tab)
    assert any(c["total"] for c in body["charts"]), f"{tab} is empty over a year"


def test_a_later_page_is_a_window_further_back(client):
    now = get(client, "summary", "sources", timeframe="30d", page=0)
    before = get(client, "summary", "sources", timeframe="30d", page=1)
    assert before["window"]["start"] < now["window"]["start"]
    # Page 0 says "Last 30 days"; an older window says which days it is,
    # because "last 30 days" would be a lie about it.
    assert now["window"]["caption"] == "Last 30 days"
    assert "to" in before["window"]["caption"]

    # Three documents were written in the last 30 days (2 h, 3 d, 20 d ago)
    # and one 60 days ago. Windows are whole days, so the 60-day-old one
    # sits just before the start of page 1 and lands on page 2.
    assert totals(now)["per_period"] == 3
    assert totals(before)["per_period"] == 0
    assert totals(get(client, "summary", "sources", timeframe="30d", page=2))["per_period"] == 1


# ── What the term resolved to ────────────────────────────────

def test_the_entity_scope_resolves_the_bucket_and_says_so(client):
    body = get(client, "entity", "sources")
    resolved = body["resolved"]
    assert resolved["match"] == "bucket"
    assert resolved["bucket"]["name"] == "Apple"
    names = {m["name"] for m in resolved["bucket"]["members"]}
    assert names == {"Apple", "Apple Inc."}
    # Three of the six documents mention a member of the bucket.
    assert totals(body)["per_period"] == 3


def entity_names(body, chart_id="most_named"):
    """The names a "by entity" chart drew.

    The label carries the name AND the kind of thing it is, one under the
    other - the axis draws the first line and the plugin the second, in
    italics (static/js/charts.js) - so a test about names reads the first
    line of it.
    """
    chart = next(c for c in body["charts"] if c["id"] == chart_id)
    return {p["label"].split("\n")[0]
            for d in chart["datasets"] for p in d["data"] if p["y"]}


def test_the_bucket_leaves_the_fruit_out(client):
    body = get(client, "entity", "entities")
    assert entity_names(body) == {"Apple", "Apple Inc."}
    named = next(c for c in body["charts"] if c["id"] == "most_named")
    keys = {p["x"] for d in named["datasets"] for p in d["data"] if p["y"]}
    assert "ent:apple-fruit" not in keys, "Apple the fruit is not in the bucket"


def test_a_term_nothing_is_called_answers_empty_rather_than_everything(client):
    body = get(client, "entity", "sources", q="Nothing Is Called This")
    assert body["resolved"]["match"] == "none"
    assert all(c["total"] == 0 for c in body["charts"])


def test_the_location_scope_isolates_one_springfield(client):
    ontario = get(client, "location", "entities", q="Springfield, Ontario, Canada")
    illinois = get(client, "location", "entities", q="Springfield, Illinois, USA")

    names = entity_names

    assert "Springfield Mills" in names(ontario)
    assert "Springfield Works" not in names(ontario)
    assert "Springfield Works" in names(illinois)
    assert "Springfield Mills" not in names(illinois)


def test_the_source_scope_is_the_documents_of_one_host(client):
    body = get(client, "source", "sources")
    assert totals(body)["per_period"] == 2, "two documents come from news.example.com"
    by_domain = next(c for c in body["charts"] if c["id"] == "by_domain")
    hosts = {p["x"] for d in by_domain["datasets"] for p in d["data"] if p["y"]}
    assert hosts == {"news.example.com"}


def test_the_market_scope_is_one_entity_and_its_topics(client):
    """The object axis is the entity a reading is ABOUT, and the topic is the
    other axis - a market insight has no third thing that is a type."""
    body = get(client, "market", "market")
    by_topic = next(c for c in body["charts"] if c["id"] == "by_topic")
    topics = {p["x"] for d in by_topic["datasets"] for p in d["data"] if p["y"]}
    assert "Batteries" in topics

    # And the topic axis narrows to that topic alone.
    on_topic = get(client, "market", "market", q="Batteries", axis="type")
    chart = next(c for c in on_topic["charts"] if c["id"] == "by_topic")
    assert {p["x"] for d in chart["datasets"] for p in d["data"] if p["y"]} == {"Batteries"}


def test_the_events_scope_is_one_kind_of_event(client):
    body = get(client, "events", "events")
    by_type = next(c for c in body["charts"] if c["id"] == "by_type")
    types = {p["x"] for d in by_type["datasets"] for p in d["data"] if p["y"]}
    assert types == {"Regulatory action"}


# ── The tabs themselves ──────────────────────────────────────

def test_the_sources_tab_counts_the_documents_of_the_year(client):
    body = get(client, "summary", "sources")
    # Five of the six were written inside the last year; the sixth is two
    # years old.
    assert totals(body)["per_period"] == 5
    by_type = next(c for c in body["charts"] if c["id"] == "by_type")
    types = {p["x"]: p["y"] for d in by_type["datasets"] for p in d["data"]}
    assert types == {"News article": 2, "Regulatory filing": 2, "Blog post": 1}


def test_the_ratings_tab_orders_by_the_scale_not_by_size(client):
    body = get(client, "summary", "ratings")
    by_value = next(c for c in body["charts"] if c["id"] == "by_value")
    # GOOD AT THE TOP, BAD AT THE BOTTOM. A horizontal bar chart draws its
    # first category at the top, so the scale is read downwards here: the
    # best value first in the answer is the best value at the top of the
    # picture. Every other chart of this scale reads the other way round,
    # which is right for a legend and for a stack.
    assert by_value["labels"][:4] == ["Excellent", "Very Good", "Good", "Neutral"]
    values = next(c for c in body["charts"] if c["id"] == "values_per_period")
    assert [d["id"] for d in values["datasets"]] == ["negative", "neutral", "positive"]
    # The full 7 x 2 x 3 set plus the single ratings of the other documents.
    assert totals(body)["per_period"] == 49


def test_the_entities_tab_tells_rows_from_distinct_entities(client):
    # Per quarter, so that a document naming an entity a second time falls
    # in the same bucket: Nordwyk Pumps is in the pump document and in the
    # harvest one, both inside this quarter.
    body = get(client, "summary", "entities", timeframe="3y")
    per_period = next(c for c in body["charts"] if c["id"] == "per_period")
    rows = sum(p["y"] for p in per_period["datasets"][0]["data"])
    distinct = sum(p["y"] for p in per_period["datasets"][1]["data"])
    assert per_period["datasets"][0]["id"] == "rows"
    assert per_period["datasets"][1]["id"] == "distinct"
    assert rows > distinct, "the same entity is named in several documents"


def test_the_connections_tab_colours_by_group_and_falls_back(client):
    body = get(client, "summary", "connections")
    by_group = next(c for c in body["charts"] if c["id"] == "by_group")
    groups = {p["label"]: p["y"] for d in by_group["datasets"] for p in d["data"] if p["y"]}
    assert "Other" in groups, "the unmapped Mysterious Bond falls back"
    colours = {p["colour"] for d in by_group["datasets"] for p in d["data"] if p["y"]}
    assert all(c.startswith("#") for c in colours)
    assert len(colours) > 1, "each group is drawn in its own colour"


def test_the_market_tab_uses_the_published_vocabulary_only(client):
    body = get(client, "summary", "market")
    outlook = next(c for c in body["charts"] if c["id"] == "short_outlook_per_period")
    # ONE ORDER FOR EVERY SCALE: absence first, then the bad end, the middle
    # and the good end. A legend read downwards and a stack read sideways
    # then say the same thing in the same direction, here and on Ratings.
    assert [d["label"] for d in outlook["datasets"]] == ["Unset", "Declining", "Neutral", "Rising"]
    sentiment = next(c for c in body["charts"] if c["id"] == "short_sentiment_per_period")
    labels = [d["label"] for d in sentiment["datasets"]]
    assert labels[0] == "Unset" and labels[1] == "Very Negative" and labels[-1] == "Very Positive"
    per_period = next(c for c in body["charts"] if c["id"] == "per_period")
    assert [d["id"] for d in per_period["datasets"]] == ["insights", "high_relevance"]
    high = sum(p["y"] for p in per_period["datasets"][1]["data"])
    total = sum(p["y"] for p in per_period["datasets"][0]["data"])
    assert 0 < high < total, "high relevance is a subset, not a second set"


def test_the_attributes_tab_summarises_by_unit(client):
    """The tab opens on the units, across the page.

    Not "Numeric values by type" - a minimum, an average and a maximum per
    attribute type: three numbers about whatever happens to be numeric is
    not what a reader comes to this tab for, and on an archive whose
    attributes are mostly prose it draws one bar. The units are the
    summary: which measures this archive actually holds.
    """
    body = get(client, "summary", "attributes")
    assert not [c for c in body["charts"] if c["id"] == "value_stats"], \
        "the value statistics are gone"
    units = next(c for c in body["charts"] if c["id"] == "by_unit")
    # IN THE GRID, ACROSS THE PAGE, AND AS TALL AS IT NEEDS TO BE. The
    # summary band is gone: a chart is a chart wherever it stands, and what
    # is left at the foot of a tab is the list of rows. `grow` is what gives
    # a chart of thirty units thirty rows of label instead of a fixed card
    # height and the rule taking every name off.
    assert units["width"] == "full" and units["grow"] is True, units
    assert units["total"] > 0, units


def test_the_events_tab_is_placed_on_the_event_date(client):
    body = get(client, "summary", "events")
    # Inside the last-year window: -1 d, -3 d, -21 d, -62 d, -205 d and the
    # hearing announced for +10 d. The +30 d launch falls in the next month
    # and the -740 d fine is two years back, so neither is counted.
    assert totals(body)["per_period"] == 6
    about = next(c for c in body["charts"] if c["id"] == "about_document_vs_entities")
    split = {d["id"]: sum(p["y"] for p in d["data"]) for d in about["datasets"]}
    assert split["about_document"] == 1, "the scheduled hearing names no entity"
    assert split["about_entities"] == 5


# ── What the picture needs beyond the numbers ────────────────

def test_the_answer_says_which_charts_may_be_stacked(client):
    """More than two series side by side is a hairline nobody can hit, so
    the page stacks them - but only where the series partition the rows.
    The flag travels with the chart, from the registry to the drawing."""
    body = get(client, "summary", "market")
    stacked = {c["id"]: c["stacked"] for c in body["charts"]}
    assert stacked["short_sentiment_per_period"] is True
    assert stacked["long_outlook_per_period"] is True
    # "of those, high relevance" is a subset of "market insights": stacking
    # them would draw a total that is not the number of insights.
    assert stacked["per_period"] is False
    assert stacked["by_topic"] is False
    for tab in TABS:
        answered = {c["id"]: c["stacked"] for c in get(client, "summary", tab)["charts"]}
        assert answered == {spec.id: spec.stacked for spec in charts.charts_for(tab)}


def test_a_matrix_answers_with_the_names_of_its_two_axes(client):
    """The numbers table under a bubble chart is the accessible form of the
    picture; its first two columns have to say WHICH value they are."""
    market = get(client, "entity", "market")
    matrix = next(c for c in market["charts"] if c["id"] == "sentiment_matrix")
    assert matrix["axes"] == ["Short-term sentiment", "Long-term sentiment"]
    # The third column is the measure, by its own name.
    assert matrix["datasets"][0]["label"] == "Market insights"

    events = get(client, "summary", "events")
    types = next(c for c in events["charts"] if c["id"] == "type_matrix")
    assert types["axes"] == ["Event type", "Entity"]
    assert types["datasets"][0]["label"] == "Events"

    # Connections say what the pair is, and it depends on the scope: with
    # one entity in hand the useful pair is the kind of tie and the other
    # end; without one it is the two ends.
    scoped = get(client, "entity", "connections")
    scoped_matrix = next(c for c in scoped["charts"] if c["id"] == "matrix")
    assert scoped_matrix["axes"] == ["Connection type", "Counterpart"]
    summary = get(client, "summary", "connections")
    summary_matrix = next(c for c in summary["charts"] if c["id"] == "matrix")
    assert summary_matrix["axes"] == ["Parent entity", "Child entity"]

    # And so does the sentence under the title: one sentence describing both
    # pictures ("type by counterpart for a scoped entity; parent by child
    # for the summary") makes the reader work out which half they got.
    assert summary_matrix["description"] == \
        "Which entity is the parent, which is the child, and how often."
    assert scoped_matrix["description"] == \
        "Which connection type links this entity to which counterpart."
    assert scoped_matrix["description"] != summary_matrix["description"]


def test_the_sentiment_matrix_carries_the_scale_and_not_the_data_order(client):
    """Both axes of "short-term against long-term" are the same ordinal
    scale, so agreement is the diagonal - and only if the axes run in the
    scale's order. The archive returns the pairs biggest first."""
    body = get(client, "entity", "market")
    matrix = next(c for c in body["charts"] if c["id"] == "sentiment_matrix")
    assert matrix["x_order"] == list(charts.SENTIMENT_AXIS)
    assert matrix["y_order"] == list(charts.SENTIMENT_AXIS)
    # Unset is off the ramp, not one step past its good end: first on both
    # axes, with an empty slot between it and Very Negative.
    assert matrix["x_order"][:3] == ["Unset", charts.AXIS_GAP, "Very Negative"]
    assert matrix["x_order"][-1] == "Very Positive"
    # Every value the preseed holds is on the axis, wherever it appeared.
    drawn = {p["x_label"] for p in matrix["datasets"][0]["data"]}
    assert drawn and drawn <= set(matrix["x_order"])
    # Ordered, not sorted by count: the data order is a different one.
    first_seen = []
    for p in matrix["datasets"][0]["data"]:
        if p["x_label"] not in first_seen:
            first_seen.append(p["x_label"])
    assert first_seen != matrix["x_order"][:len(first_seen)] or len(first_seen) < 2

    # A matrix of names has no scale to pin, and says so with an empty one.
    events = next(c for c in get(client, "summary", "events")["charts"]
                  if c["id"] == "type_matrix")
    assert events["x_order"] == [] and events["y_order"] == []


# ── Projects and languages ───────────────────────────────────

@pytest.mark.parametrize("tab", TABS)
def test_the_two_languages_are_the_same_rows_and_count_the_same(client, tab):
    english = get(client, "summary", tab, language="English")
    german = get(client, "summary", tab, language="German")
    assert totals(english) == totals(german)


def test_the_german_view_shows_translated_labels(client):
    body = get(client, "summary", "sources", language="German")
    by_type = next(c for c in body["charts"] if c["id"] == "by_type")
    types = {p["x"] for d in by_type["datasets"] for p in d["data"] if p["y"]}
    assert "Nachrichtenartikel" in types


def test_the_other_project_does_not_leak_in(client):
    beta = get(client, "summary", "sources", project=BETA)
    assert totals(beta)["per_period"] == 1
    alpha = get(client, "summary", "sources")
    assert totals(alpha)["per_period"] == 5


# ── The summary, searched ────────────────────────────────────
#
# Its magnifier searches EVERYTHING: one term, all five other kinds at once,
# and the page is the union of what they answer (app/scope.py). Empty is
# still the whole project - the only scope where that is a picture.

@pytest.mark.parametrize("tab", TABS)
def test_every_tab_of_the_searched_summary_answers(client, tab):
    body = get(client, "summary", tab, q="Apple", timeframe="3y")
    assert body["q"] == "Apple"
    assert body["resolved"]["kind"] == "summary"
    assert [h["kind"] for h in body["resolved"]["kinds"]] == ["entity", "source"]
    assert len(body["charts"]) == len(charts.charts_for(tab))


def test_a_term_narrows_the_summary_without_emptying_it(client):
    everything = totals(get(client, "summary", "sources", q="", timeframe="3y"))
    narrowed = totals(get(client, "summary", "sources", q="Apple", timeframe="3y"))
    # Three of the five Alpha documents are about Apple in one sense or
    # another (the bucket's entities, or an address that says apple).
    assert 0 < narrowed["per_period"] < everything["per_period"]


def test_the_summary_reports_every_kind_that_answered(client):
    body = get(client, "summary", "entities", q="Apple", timeframe="3y")
    kinds = {h["kind"]: h for h in body["resolved"]["kinds"]}
    assert kinds["entity"]["match"] == "bucket"
    assert set(kinds["entity"]["names"]) == {"Apple Inc.", "Apple"}
    assert kinds["source"]["match"] == "substring"
    assert body["resolved"]["bucket"]["name"] == "Apple"


def test_a_summary_term_nobody_answers_draws_empty_charts(client):
    body = get(client, "summary", "sources", q="Atlantis", timeframe="3y")
    assert body["resolved"]["match"] == "none" and body["resolved"]["kinds"] == []
    assert set(totals(body).values()) == {0}


def test_the_searched_summary_keeps_parent_by_child(client):
    """There is no "the" entity on a searched summary - the term can be a
    bucket AND two documents AND a place at once - so the connections matrix
    stays the two ends, the way it is with nothing typed."""
    body = get(client, "summary", "connections", q="Apple", timeframe="3y")
    matrix = next(c for c in body["charts"] if c["id"] == "matrix")
    assert matrix["axes"] == ["Parent entity", "Child entity"]
    assert matrix["description"] == \
        "Which entity is the parent, which is the child, and how often."


def test_a_drilldown_from_the_searched_summary_carries_the_term(client):
    body = get(client, "summary", "sources", q="Apple", timeframe="3y")
    per_period = next(c for c in body["charts"] if c["id"] == "per_period")
    bucket = next(p["x"] for d in per_period["datasets"] for p in d["data"] if p["y"])
    r = client.post("/api/diagrams/drilldown",
                    params={"project": ALPHA, "language": "English"},
                    json={"scope": "summary", "tab": "sources", "q": "Apple",
                          "timeframe": "3y", "page": 0, "chart_id": "per_period",
                          "key": {"bucket": bucket}})
    assert r.status_code == 200, r.text
    rows = r.json()
    assert rows["q"] == "Apple" and rows["rows"]
    # The same point without a term holds at least as many rows.
    wide = client.post("/api/diagrams/drilldown",
                       params={"project": ALPHA, "language": "English"},
                       json={"scope": "summary", "tab": "sources", "q": "",
                             "timeframe": "3y", "page": 0, "chart_id": "per_period",
                             "key": {"bucket": bucket}}).json()
    assert len(wide["rows"]) >= len(rows["rows"])


def test_the_export_of_the_searched_summary_carries_the_term(client):
    r = client.get("/api/export/diagrams.json",
                   params={"project": ALPHA, "language": "English", "scope": "summary",
                           "tab": "sources", "q": "Apple", "timeframe": "3y"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["context"]["q"] == "Apple"
    assert [h["kind"] for h in body["context"]["resolved"]["kinds"]] == ["entity", "source"]
    assert body["export"]["filters"]["q"] == "Apple"


# ── Refusals ─────────────────────────────────────────────────

def test_an_unknown_tab_is_a_404_that_lists_the_tabs(client):
    r = client.get("/api/diagrams/summary/finances",
                   params={"project": ALPHA, "language": "English"})
    assert r.status_code == 404
    assert "sources" in r.json()["hint"]


def test_an_unknown_scope_and_an_unknown_timeframe_are_400(client):
    common = {"project": ALPHA, "language": "English"}
    r = client.get("/api/diagrams/weather/sources", params=common)
    assert r.status_code == 400 and "summary" in r.json()["hint"]
    r = client.get("/api/diagrams/summary/sources", params={**common, "timeframe": "10y"})
    assert r.status_code == 400 and "24h" in r.json()["hint"]


def test_a_page_before_the_beginning_of_time_is_refused(client):
    r = client.get("/api/diagrams/summary/sources",
                   params={"project": ALPHA, "language": "English", "page": -1})
    assert r.status_code == 400


def test_the_catalogue_needs_no_archive(client):
    body = client.get("/api/diagrams/catalogue").json()
    assert [t["id"] for t in body["tabs"]] == list(charts.TAB_IDS)
    assert all(c["id"] for tab in body["tabs"] for c in tab["charts"])
