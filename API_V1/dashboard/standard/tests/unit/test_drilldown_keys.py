"""What a click carries back, and what it is allowed to carry.

    python -m pytest tests/unit/test_drilldown_keys.py -q

The drilldown is the one place where a value from the browser becomes a
predicate on the archive, so its shape is checked before anything else
happens: exactly the fields the chart's kind has an axis for, no extras, no
empty ones, and a period start that is really a date.

Nothing here touches a database. The statements are rendered and read - the
point being that the drilldown compares THE SAME expressions the chart
grouped by, which is what makes "the rows behind this bar" a fact rather
than a hope.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from psycopg import sql

from app import charts, scope as scope_module, sqlbuild
from app.charts import drilldown as dd
from app.sqlbuild import check_params, placeholders, render
from app.timeframes import window_for

NOW = datetime(2026, 3, 11, 14, 37, tzinfo=timezone.utc)
WINDOW = window_for("7d", 0, NOW)
BUCKET = WINDOW.buckets()[0].isoformat()


class Ctx:
    project = "plant-docs"
    language = "German"


CTX = Ctx()


def summary_scope():
    return scope_module._summary(CTX)


def entity_scope():
    """A scoped ScopeSet built by hand: resolve_scope needs a database, the
    CTEs it would build do not."""
    ent, src = scope_module._entity_ctes_from_pattern()
    return scope_module.ScopeSet(
        "entity", "Apple", ent, src,
        {"project": CTX.project, "language": CTX.language, "sc_pat": "apple%"},
        {"kind": "entity", "label": "Apple", "match": "prefix"})


SCOPES = {"summary": summary_scope, "entity": entity_scope}


# ── The key itself ───────────────────────────────────────────

def test_each_kind_takes_its_own_key():
    assert dd.normalise_key("bucket", {"bucket": BUCKET})["bucket"].tzinfo is not None
    assert dd.normalise_key("x", {"x": "News article"}) == {"x": "News article"}
    assert dd.normalise_key("xy", {"x": "Rising", "y": "Neutral"}) == {"x": "Rising", "y": "Neutral"}


def test_a_key_field_the_chart_has_no_axis_for_is_refused():
    # Ignoring it would answer with the rows of the whole chart and look
    # like it worked.
    with pytest.raises(dd.DrilldownError):
        dd.normalise_key("x", {"x": "News article", "y": "Neutral"})
    with pytest.raises(dd.DrilldownError):
        dd.normalise_key("bucket", {"bucket": BUCKET, "x": "News article"})


@pytest.mark.parametrize("drill,key", [
    ("bucket", {}), ("bucket", {"bucket": ""}), ("bucket", {"bucket": "   "}),
    ("x", {}), ("x", {"x": None}),
    ("xy", {"x": "Rising"}), ("xy", {"x": "Rising", "y": ""}),
])
def test_a_missing_or_empty_key_is_refused(drill, key):
    with pytest.raises(dd.DrilldownError):
        dd.normalise_key(drill, key)


def test_a_key_longer_than_any_column_is_refused():
    with pytest.raises(dd.DrilldownError):
        dd.normalise_key("x", {"x": "a" * (dd.MAX_KEY_LENGTH + 1)})


def test_an_unknown_key_shape_is_refused():
    with pytest.raises(dd.DrilldownError):
        dd.normalise_key("colour", {"x": "a"})


def test_the_bucket_is_a_date_and_naive_means_utc():
    assert dd.parse_bucket("2026-03-09T00:00:00Z") == datetime(2026, 3, 9, tzinfo=timezone.utc)
    assert dd.parse_bucket("2026-03-09T00:00:00") == datetime(2026, 3, 9, tzinfo=timezone.utc)
    with pytest.raises(dd.DrilldownError):
        dd.parse_bucket("last tuesday")


def test_the_error_carries_something_to_do_about_it():
    with pytest.raises(dd.DrilldownError) as caught:
        dd.normalise_key("xy", {"x": "Rising"})
    assert caught.value.hint


# ── The key as a predicate ───────────────────────────────────

def test_a_time_key_compares_the_same_bucket_expression_the_chart_grouped_by():
    plan = charts.get_chart("sources", "per_period").builder(summary_scope(), CTX, WINDOW)
    parts, params = dd.key_predicates(plan, "time", WINDOW, dd.normalise_key("bucket", {"bucket": BUCKET}))
    text = render(sql.SQL(" AND ").join(parts))
    assert render(sqlbuild.bucket("sources", WINDOW, "t")) in text
    assert "%(dd_bucket)s" in text
    assert params["dd_bucket"] == WINDOW.buckets()[0]


def test_a_key_chart_compares_the_category_and_a_matrix_compares_both_axes():
    plan = charts.get_chart("sources", "by_type").builder(summary_scope(), CTX, WINDOW)
    parts, params = dd.key_predicates(plan, "key", WINDOW, {"x": "News article"})
    assert render(plan.category) in render(parts[0])
    assert params == {"dd_x": "News article"}

    plan = charts.get_chart("market", "sentiment_matrix").builder(summary_scope(), CTX, WINDOW)
    parts, params = dd.key_predicates(plan, "matrix", WINDOW, {"x": "Positive", "y": "Neutral"})
    assert len(parts) == 2
    assert params == {"dd_x": "Positive", "dd_y": "Neutral"}


def test_a_dataset_that_is_a_split_compares_the_series_expression():
    spec = charts.get_chart("market", "short_outlook_per_period")
    plan = spec.builder(summary_scope(), CTX, WINDOW)
    frag, params = dd.dataset_predicate(plan, "rising", tuple(i for i, _ in spec.datasets))
    assert "%(dd_series)s" in render(frag)
    assert params == {"dd_series": "rising"}


def test_a_dataset_that_is_a_measure_uses_its_own_filter():
    spec = charts.get_chart("market", "per_period")
    plan = spec.builder(summary_scope(), CTX, WINDOW)
    frag, params = dd.dataset_predicate(plan, "high_relevance", ("insights", "high_relevance"))
    assert "bool_high_relevance" in render(frag)
    assert params == {}
    # The other measure counts every row of the point, so it adds nothing.
    frag, params = dd.dataset_predicate(plan, "insights", ("insights", "high_relevance"))
    assert frag is None and params == {}


def test_a_dataset_the_chart_does_not_have_is_refused():
    spec = charts.get_chart("market", "short_outlook_per_period")
    plan = spec.builder(summary_scope(), CTX, WINDOW)
    with pytest.raises(dd.DrilldownError):
        dd.dataset_predicate(plan, "soaring", tuple(i for i, _ in spec.datasets))


# ── Every chart, every scope ─────────────────────────────────

def key_for(spec):
    if spec.drill == "bucket":
        return {"bucket": BUCKET}
    if spec.drill == "x":
        return {"x": "anything"}
    return {"x": "anything", "y": "anything else"}


@pytest.mark.parametrize("spec", charts.all_charts(), ids=lambda s: f"{s.tab}.{s.id}")
def test_the_key_shape_follows_the_kind(spec):
    assert spec.drill == {"time": "bucket", "key": "x", "matrix": "xy"}[spec.kind]
    assert dd.normalise_key(spec.drill, key_for(spec))


@pytest.mark.parametrize("kind", sorted(SCOPES))
@pytest.mark.parametrize("spec", charts.all_charts(), ids=lambda s: f"{s.tab}.{s.id}")
def test_every_drilldown_is_bound_and_scoped(spec, kind):
    scope = SCOPES[kind]()
    plan = spec.builder(scope, CTX, WINDOW)
    dataset = spec.datasets[0][0] if spec.datasets else ""
    statement, key_params = dd.rows_statement(
        plan, spec.kind, scope, WINDOW, dd.normalise_key(spec.drill, key_for(spec)),
        dataset, tuple(i for i, _ in spec.datasets) + (charts.DEFAULT_DATASET_ID,))
    params = {**dd.params_for(plan, scope, CTX, WINDOW), **key_params,
              "dd_limit": dd.PAGE_SIZE, "dd_offset": 0}
    # Nothing is left unbound - psycopg would say so at run time, this says
    # so with the chart's name on it.
    check_params(statement, params)
    text = render(statement)
    assert "%(project)s" in text and "%(language)s" in text
    assert "%(w_start)s" in text and "%(w_end)s" in text
    assert "LIMIT %(dd_limit)s OFFSET %(dd_offset)s" in text
    # A scoped page never lists rows that are outside the scope.
    if kind == "entity":
        assert "FROM ent" in text or "FROM src" in text


@pytest.mark.parametrize("spec", charts.all_charts(), ids=lambda s: f"{s.tab}.{s.id}")
def test_every_chart_statement_is_bound(spec):
    scope = entity_scope()
    plan = spec.builder(scope, CTX, WINDOW)
    statement = dd.chart_statement(spec.kind, plan, scope, WINDOW)
    check_params(statement, dd.params_for(plan, scope, CTX, WINDOW))
    text = render(statement)
    assert "%(project)s" in text and "%(language)s" in text
    if spec.kind in ("key", "matrix"):
        assert f"LIMIT {plan.limit}" in text, "a category chart is never unbounded"
    if spec.tab == "events":
        # An event may be dated after its document, so the chunk prefilter
        # would drop exactly the announcements this tab is for.
        assert "date_added" not in text


# ── What a matrix says about its own axes ────────────────────

MATRICES = [s for s in charts.all_charts() if s.kind == "matrix"]


@pytest.mark.parametrize("kind", sorted(SCOPES))
@pytest.mark.parametrize("spec", MATRICES, ids=lambda s: f"{s.tab}.{s.id}")
def test_every_matrix_names_its_two_axes(spec, kind):
    """A bubble chart's numbers table is the accessible form of the picture,
    and a table headed "First axis" and "Second axis" makes the reader guess
    which column is the short-term value. The names come from the builder,
    because the same chart can be about different pairs."""
    plan = spec.builder(SCOPES[kind](), CTX, WINDOW)
    assert len(plan.axis_titles) == 2, f"{spec.tab}/{spec.id} does not name its axes"
    for title in plan.axis_titles:
        assert title and title[0].isupper()
    assert plan.axis_titles[0] != plan.axis_titles[1]


def test_a_matrix_over_an_ordered_vocabulary_pins_the_scale_to_its_axes():
    # Short-term against long-term sentiment only answers its question when
    # both axes run in the order of the scale: agreement is then the
    # diagonal. In count order the bubbles sit in arbitrary places.
    plan = charts.get_chart("market", "sentiment_matrix").builder(summary_scope(), CTX, WINDOW)
    assert plan.axis_order == (charts.SENTIMENT_AXIS, charts.SENTIMENT_AXIS)
    # THE LIST LEADS WITH ABSENCE, THE AXIS STILL RUNS THE SCALE.
    #
    # One order for every scale in the product now: "Unset" first - it is not
    # a step and a reader should meet it before the scale starts rather than
    # find it appended past the best value - then the bad end, the middle and
    # the good end. That is the order of the legend and of a stacked bar.
    #
    # The AXIS is the other question and is unchanged: Unset, an empty slot,
    # then Very Negative to Very Positive, so agreement is still the diagonal.
    assert charts.SENTIMENT_SCALE[0] == "Unset", "the list leads with absence"
    assert charts.SENTIMENT_SCALE[1] == "Very Negative"
    assert charts.SENTIMENT_SCALE[-1] == "Very Positive"
    assert charts.SENTIMENT_AXIS[0] == "Unset"
    assert charts.SENTIMENT_AXIS[2] == "Very Negative"
    assert charts.SENTIMENT_AXIS[-1] == "Very Positive"


def test_the_missing_value_is_off_the_scale_and_not_past_its_good_end():
    """Unset drawn at the end of the ramp lands above Very Positive on the y
    axis and to the right of it on the x axis - the positive extreme of both
    ordered scales - so an insight nobody gave a sentiment reads as one step
    better than the best there is. It is drawn first instead, with an empty
    slot between it and Very Negative, where it can only be read as off the
    scale; the ramp itself is untouched."""
    axis = charts.SENTIMENT_AXIS
    assert axis[0] == "Unset"
    assert axis[1] == charts.AXIS_GAP and not axis[1].strip(), "the divider carries no word"
    assert axis[2] == "Very Negative" and axis[-1] == "Very Positive"
    # The ramp keeps its own order, and nothing but the gap was added.
    assert tuple(a for a in axis[2:]) == tuple(
        s for s in charts.SENTIMENT_SCALE if s != "Unset")
    assert len(axis) == len(charts.SENTIMENT_SCALE) + 1


@pytest.mark.parametrize("tab,chart_id", [("connections", "matrix"), ("events", "type_matrix")])
def test_a_matrix_of_names_keeps_the_order_the_statement_gave_it(tab, chart_id):
    # Entities and connection types have no scale; ordering them by count -
    # the biggest cells first - is the honest answer, so no scale is pinned.
    plan = charts.get_chart(tab, chart_id).builder(entity_scope(), CTX, WINDOW)
    assert plan.axis_order == ((), ())


def test_the_page_size_is_the_one_the_dialog_expects():
    assert dd.PAGE_SIZE == 20
