"""The perspective filter: the predicate, the guard, and how a choice is read.

    python -m pytest tests/unit/test_perspective_filter.py -q

Two selectors in the top bar mean one sentence: show only what came from
documents that carry, FOR THIS PERSPECTIVE, at least this importance. It
narrows Query, Events, Diagrams and the Graph at once, so the thing that has
to be pinned is not any one view - it is the fragment all four are built from
and the two rules that make it safe:

  * nothing chosen is TRUE, never an empty result, so every statement can
    carry it unconditionally;
  * a level the vocabulary cannot order does not silently disappear.

Nothing here touches a database. The predicate is rendered and read.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app import sqlbuild
from app.charts import drilldown as dd
from app.context import DEFAULT_MIN_IMPORTANCE, Context, pick_filter
from app.sqlbuild import check_params, placeholders, render
from app.timeframes import window_for

NOW = datetime(2026, 3, 11, 14, 37, tzinfo=timezone.utc)
WINDOW = window_for("7d", 0, NOW)

PERSPECTIVES = ["Compliance", "Investigative Journalist", "Maintenance"]


# ── The predicate ────────────────────────────────────────────

def test_no_perspective_is_TRUE_and_binds_nothing():
    """The idiom ScopeSet.scoped already uses: a caller concatenates it
    without asking whether it applies."""
    frag = sqlbuild.perspective_scope("", "t")
    assert render(frag) == "TRUE"
    assert placeholders(render(frag)) == set()
    assert sqlbuild.perspective_params("", "") == {}
    # A perspective of nothing but spaces is not a perspective.
    assert render(sqlbuild.perspective_scope("   ", "t")) == "TRUE"


def test_the_predicate_binds_every_placeholder_it_names():
    frag = sqlbuild.perspective_scope("Investor", "t")
    params = {**sqlbuild.identity_params("plant-docs", "German"),
              **sqlbuild.perspective_params("Investor", "High Importance")}
    check_params(frag, params)
    assert placeholders(render(frag)) == {"project", "language", "perspective", "min_importance"}


def test_the_join_key_is_the_pair_never_the_source_id_alone():
    """A source id is only unique inside its task. Two rows of one task can
    carry the same id after a re-run, which is why this is an EXISTS on both
    columns and not a join on one."""
    text = render(sqlbuild.perspective_scope("Investor", "ev"))
    assert "EXISTS (SELECT 1" in text
    assert 'si.text_task_id = "ev".text_task_id' in text
    assert 'si.text_fk_source_id = "ev"."text_fk_source_id"' in text
    assert " JOIN processed_data.source_importances_by_perspective" not in text


def test_the_sources_table_is_joined_on_its_own_id():
    """`sources` IS the document, so the column is text_source_id there and
    text_fk_source_id everywhere else."""
    assert sqlbuild.source_id_column("sources") == "text_source_id"
    for name in ("entities", "events", "connections", "ratings", "locations",
                 "attributes", "market_insights"):
        assert sqlbuild.source_id_column(name) == "text_fk_source_id"
    with pytest.raises(ValueError):
        sqlbuild.source_id_column("not_a_table")


def test_the_ordering_is_a_number_in_the_database_not_a_rank_map():
    """database/init/02-vocabularies.sql:9-12 says float_value exists so that
    "worse than" is a comparison rather than a lookup table in application
    code. If this ever becomes a Python list of level names in order, the
    scale and the archive can disagree and nobody will notice."""
    text = render(sqlbuild.perspective_scope("Investor", "t"))
    assert "processed_data.importance_types" in text
    assert "float_value <" in text
    for level in ("Low Importance", "High Importance", "Critical Importance"):
        assert level not in text, "a level name is spliced into the SQL"


def test_an_unorderable_level_falls_back_to_unfiltered_not_to_empty():
    """THE INVERTED COMPARISON, and the reason for it.

    The predicate asks "not one of the levels BELOW the threshold" rather
    than "one of the levels at or above it". On a level the vocabulary knows
    the two are the same sentence; on one it does not they differ, and the
    difference is a whole language's worth of rows.

    vocabulary_scope() binds the project but not the language, so
    importance_types holds the English scale and a German view's rows say
    "Hohe Wichtigkeit". Under >= every one of them resolves to NULL and falls
    out; under "not below" they are kept. `<> ALL` of an empty array is TRUE,
    which is the same fallback when the THRESHOLD is the unknown one.
    """
    text = render(sqlbuild.perspective_scope("Investor", "t"))
    assert "<> ALL (ARRAY(" in text
    assert ">= %(min_importance)s" not in text


# ── The one chart that must not filter itself ────────────────

def test_the_importances_chart_is_not_narrowed_by_the_importances():
    """sources/importance_by_perspective is drawn FROM the judgement table.
    Filtering it would leave a chart showing only the bar it was narrowed to,
    and its Perspective axis would collapse to one category - the picture is
    the reader's answer to "what does the filter cover?"."""
    plan = dd.Plan(table="source_importances_by_perspective",
                   perspective="Investor", min_importance="High Importance")
    assert render(dd.perspective_where(plan)) == "TRUE"


@pytest.mark.parametrize("table", ["sources", "entities", "events", "ratings",
                                   "connections", "locations", "attributes",
                                   "market_insights"])
def test_every_other_table_is_narrowed(table):
    plan = dd.Plan(table=table, perspective="Investor", min_importance="High Importance")
    text = render(dd.perspective_where(plan))
    assert text.startswith("EXISTS (")
    assert sqlbuild.source_id_column(table) in text


def test_a_plan_with_no_perspective_is_not_narrowed():
    plan = dd.Plan(table="entities")
    assert render(dd.perspective_where(plan)) == "TRUE"


def test_the_filter_reaches_every_chart_through_one_where():
    """One edit in _where() is what covers all forty-eight charts, all eight
    Diagrams tabs and every drilldown. If a builder ever has to opt in, this
    is where it stops being true."""
    plan = dd.Plan(table="entities", perspective="Investor",
                   min_importance="High Importance")
    text = render(dd._where(plan, WINDOW))
    assert "source_importances_by_perspective" in text
    assert "%(perspective)s" in text and "%(min_importance)s" in text

    plain = render(dd._where(dd.Plan(table="entities"), WINDOW))
    assert "source_importances_by_perspective" not in plain


def test_the_offer_of_another_period_obeys_the_filter_too():
    """"there is data in November" about a row the filtered view will not
    show is worse than the empty table it was meant to explain."""
    class Scope:
        scoped = False

        def ctes(self):
            return []

    plan = dd.Plan(table="entities", perspective="Investor",
                   min_importance="High Importance")
    text = render(dd.newest_statement(plan, Scope()))
    assert "source_importances_by_perspective" in text


# ── Reading the choice off a request ─────────────────────────

def test_nothing_chosen_is_All():
    assert pick_filter("", "", "", "", PERSPECTIVES) == ("", "")
    assert pick_filter(None, None, None, None, PERSPECTIVES) == ("", "")


def test_the_query_string_wins_over_the_cookie():
    assert pick_filter("Maintenance", "High Importance", "Compliance", "Low Importance",
                       PERSPECTIVES) == ("Maintenance", "High Importance")


def test_the_cookie_is_used_when_the_query_is_silent():
    assert pick_filter("", "", "Compliance", "Medium Importance",
                       PERSPECTIVES) == ("Compliance", "Medium Importance")


def test_the_default_level_is_the_second_rung():
    """Not the first: "Not Important and above" is every judged document, a
    filter that looks active and does nothing."""
    assert pick_filter("Compliance", "", "", "", PERSPECTIVES) == \
        ("Compliance", DEFAULT_MIN_IMPORTANCE)
    assert DEFAULT_MIN_IMPORTANCE == "Low Importance"


def test_a_perspective_this_project_does_not_have_is_dropped():
    """A stale cookie must not empty every view of a project it was never
    chosen for - the same trap the language half of pick_context guards."""
    assert pick_filter("", "", "Investor", "High Importance", PERSPECTIVES) == ("", "")
    assert pick_filter("Investor", "High Importance", "", "", PERSPECTIVES) == ("", "")


def test_a_perspective_comes_back_spelled_as_the_archive_spells_it():
    """The predicate compares the stored value, so a link typed in lower case
    must resolve to the stored spelling rather than to nothing."""
    assert pick_filter("compliance", "", "", "", PERSPECTIVES)[0] == "Compliance"


# ── What a Context carries ───────────────────────────────────

def test_a_context_without_a_filter_is_the_pair_it_always_was():
    ctx = Context("plant-docs", "German")
    assert ctx.params == {"project": "plant-docs", "language": "German"}
    assert ctx.filtered is False


def test_a_filtered_context_carries_both_halves_in_every_link():
    ctx = Context("plant-docs", "German", "Compliance", "High Importance")
    assert ctx.params == {"project": "plant-docs", "language": "German",
                          "perspective": "Compliance", "min_importance": "High Importance"}
    assert ctx.filtered is True
