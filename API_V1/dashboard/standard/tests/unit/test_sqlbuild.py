"""The SQL building blocks and the scope CTEs - rendered, never executed.

Every rule the module docstrings promise is asserted on the rendered text:
project AND language bound, the right timeline column, the chunk prefilter
everywhere except events, a LIMIT on every key chart, and no placeholder
without a parameter.

    python -m pytest tests/unit/test_sqlbuild.py -q
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

import pytest
from psycopg import sql

from app import scope, sqlbuild
from app.sqlbuild import TABLES, check_params, placeholders, render
from app.timeframes import window_for

NOW = datetime(2026, 3, 11, 14, 37, tzinfo=timezone.utc)
WINDOW = window_for("7d", 0, NOW)


class Ctx:
    project = "plant-docs"
    language = "German"


# ── Timeline and window ─────────────────────────────────────

@pytest.mark.parametrize("table", TABLES)
def test_timeline_column_per_table(table):
    text = render(sqlbuild.timeline(table, "t"))
    if table == "sources":
        assert text == 'COALESCE("t".date_written, "t".date_commissioned)'
    elif table == "events":
        assert text == 'COALESCE("t".date_eventdate, "t".date_commissioned)'
    else:
        assert text == '"t".date_commissioned'
    assert "date_added" not in text


@pytest.mark.parametrize("table", TABLES)
def test_window_predicate_binds_bounds_and_prefilters_chunks(table):
    frag = sqlbuild.window_predicate(table, WINDOW, "t")
    text = render(frag)
    assert placeholders(text) == {"w_start", "w_end"}
    assert ">= %(w_start)s" in text and "< %(w_end)s" in text
    if table == "events":
        # An event can be dated after its document; the prefilter would drop it.
        assert "date_added" not in text
    else:
        assert '"t".date_added >= %(w_start)s' in text
    check_params(frag, sqlbuild.window_params(WINDOW))


def test_identity_binds_project_and_language():
    text = render(sqlbuild.identity("r"))
    assert '"r".text_project = %(project)s' in text
    assert '"r".text_language = %(language)s' in text
    assert placeholders(text) == set(sqlbuild.identity_params("p", "l"))


def test_bucket_uses_the_timeframe_unit_as_a_literal():
    text = render(sqlbuild.bucket("ratings", WINDOW, "r"))
    assert text.startswith("date_trunc('day', ")
    assert "%(" not in text.split(",")[0]


def test_bucket_refuses_a_unit_that_is_not_date_trunc():
    class W:
        unit = "fortnight; DROP TABLE x"
    with pytest.raises(ValueError):
        sqlbuild.bucket("ratings", W(), "r")


def test_unknown_table_is_refused_everywhere():
    for fn in (sqlbuild.table, sqlbuild.timeline):
        with pytest.raises(ValueError):
            fn("users")


# ── Key charts and helpers ──────────────────────────────────

def test_limit_is_always_present_and_positive():
    assert render(sqlbuild.limit()) == f"LIMIT {sqlbuild.KEY_CHART_LIMIT}"
    assert render(sqlbuild.limit(5)) == "LIMIT 5"
    with pytest.raises(ValueError):
        sqlbuild.limit(0)


def test_like_patterns_escape_metacharacters():
    assert sqlbuild.like_prefix("50%_a\\b") == "50\\%\\_a\\\\b%"
    assert sqlbuild.like_substring("x") == "%x%"


def test_vocabulary_scope_includes_the_seeded_project():
    assert render(sqlbuild.vocabulary_scope("v")) == '"v".text_project IN (%(project)s, \'\')'


def test_host_expression_is_pure_sql():
    text = render(sqlbuild.host_expression("s"))
    assert text.startswith('substring("s".text_uri from')
    assert "%(" not in text


# The host list the suggestion box reads is a materialized view built with
# this very expression (sql/03-places-view.sql). If the two ever differ, a
# search finds a host the field cannot suggest, or the other way round - the
# same trap the address rule is pinned against in tests/unit/test_places.py.
VIEW_SQL = (Path(__file__).resolve().parents[2] / "sql" / "03-places-view.sql").read_text(
    encoding="utf-8")


def test_the_host_rule_is_mirrored_in_the_view_sql():
    pattern = re.search(r"from '([^']+)'", render(sqlbuild.host_expression("s")))
    assert pattern, "the host expression no longer has a pattern to mirror"
    assert f"from '{pattern.group(1)}'" in VIEW_SQL, (
        "dashboard.source_hosts cuts the host out of text_uri with a different "
        "pattern from app/sqlbuild.host_expression")
    assert "CREATE MATERIALIZED VIEW IF NOT EXISTS dashboard.source_hosts" in VIEW_SQL


def test_stats_statement_has_five_counters_and_no_total():
    text = render(sqlbuild.stats_statement("sources"))
    assert text.count("count(*) FILTER") == 5
    assert "AS total" not in text and "count(*) AS" not in text
    assert 'date_added >= now() - interval \'1 year\'' in text
    assert placeholders(text) == {"project", "language"}


@pytest.mark.parametrize("table", TABLES)
def test_the_arrival_clock_is_the_same_column_for_every_table(table):
    """`timeline(arrival=True)` is date_commissioned everywhere, with no
    content-date override - and asking for it does not move the content
    clock the charts are drawn on."""
    assert render(sqlbuild.timeline(table, "t", arrival=True)) == '"t".date_commissioned'
    # The module dict is read by forty chart builders. A counter that edited
    # it to answer one card would move all of them.
    assert sqlbuild.TIMELINE_COLUMN["sources"] == \
        "COALESCE({a}.date_written, {a}.date_commissioned)"
    assert sqlbuild.TIMELINE_COLUMN["events"] == \
        "COALESCE({a}.date_eventdate, {a}.date_commissioned)"


@pytest.mark.parametrize("table", TABLES)
def test_every_tile_of_the_home_page_counts_on_the_arrival_clock(table):
    """ALL EIGHT TILES, NOT SIX.

    The counters ran through timeline(), which gives sources the date the
    document says it was WRITTEN and events the date the event HAPPENED. So
    one card headed "Archived, per period" counted six tables by when they
    arrived and two by what they are about. Measured on a live archive:
    date_added spanned a single second while date_written spanned 2021 to
    2026, and the card read Sources 3/4/6/9 beside Entities 158/158/158/158
    for one and the same import.
    """
    text = render(sqlbuild.stats_statement(table))
    assert "date_written" not in text and "date_eventdate" not in text
    assert text.count('"t".date_commissioned >=') == 5


def test_the_charts_still_plot_when_things_happened():
    """The other half of the same decision: window_predicate and bucket are
    NOT moved. A diagram plots when things happened, which is what those
    charts are for."""
    assert "date_written" in render(sqlbuild.window_predicate("sources", WINDOW, "t"))
    assert "date_eventdate" in render(sqlbuild.bucket("events", WINDOW, "t"))


def test_check_params_names_the_missing_key():
    frag = sql.SQL("SELECT %(a)s, %(b)s")
    with pytest.raises(KeyError, match="'b'"):
        check_params(frag, {"a": 1})
    check_params(frag, {"a": 1, "b": 2})


# ── Scope CTEs ──────────────────────────────────────────────

_KIND_BUILDERS = {
    "entity": lambda: scope._entity_ctes_from_pattern(),
    "source": lambda: scope._source_ctes(),
    "location": lambda: scope._location_ctes(sql.SQL("lower(l.text_address) LIKE %(sc_pat)s")),
    "market": lambda: scope._market_ctes(),
    "events": lambda: scope._events_ctes(),
}


@pytest.mark.parametrize("kind", sorted(_KIND_BUILDERS))
def test_scope_ctes_bind_the_project_and_yield_task_id_pairs(kind):
    ent, src = _KIND_BUILDERS[kind]()
    for cte, name in ((ent, "ent"), (src, "src")):
        text = render(cte)
        assert text.startswith(f'"{name}" AS (')
        assert "text_project = %(project)s" in text
        # Deliberately no language: names are translated, ids are shared.
        assert "text_language = %(language)s" not in text
        assert "AS task_id" in text and "AS id" in text
        assert placeholders(text) <= {"project", "sc_pat"}


def test_bucket_scope_ctes_join_the_values_list():
    frag, params = scope.bucket_terms([("Apple Inc.", "Company"), ("Apple", None)])
    ent, src = scope._entity_ctes_from_terms(frag)
    text = render(ent)
    assert '(VALUES (%(bt_name_0)s::text, %(bt_type_0)s::text), (%(bt_name_1)s::text, %(bt_type_1)s::text)) AS "bt"(name, type)' in text
    assert 'lower("e".text_name) = "bt".name' in text
    assert '"bt".type IS NULL OR lower("e".text_type) = "bt".type' in text
    check_params(ent, {"project": "p", **params})


def test_scope_predicates_are_true_for_summary_and_pairs_otherwise():
    summary = scope._summary(Ctx())
    assert not summary.scoped
    assert render(summary.ent_predicate("r")) == "TRUE"
    assert render(summary.with_clause()) == ""
    assert summary.params == {"project": "plant-docs", "language": "German"}

    ent, src = scope._market_ctes()
    scoped = scope.ScopeSet("market", "chips", ent, src, {"project": "p", "language": "l", "sc_pat": "chips"})
    assert render(scoped.ent_predicate("r")) == '("r".text_task_id, "r"."text_fk_entity_id") IN (SELECT task_id, id FROM ent)'
    assert render(scoped.src_predicate("s", "text_source_id")) == '("s".text_task_id, "s"."text_source_id") IN (SELECT task_id, id FROM src)'
    with_text = render(scoped.with_clause())
    assert with_text.startswith('WITH "ent" AS (') and '"src" AS (' in with_text


def test_a_whole_statement_composes_with_every_parameter_supplied():
    """The shape every tab builder produces: scope WITH + identity + window."""
    ent, src = scope._source_ctes()
    sc = scope.ScopeSet("source", "example.com", ent, src,
                        {"project": "p", "language": "l", "sc_pat": "%example.com%"})
    stmt = sql.SQL("{w}SELECT {b} AS x, count(*) FROM {t} AS r WHERE {i} AND {wp} AND {ep} GROUP BY 1").format(
        w=sc.with_clause(), b=sqlbuild.bucket("ratings", WINDOW, "r"), t=sqlbuild.table("ratings"),
        i=sqlbuild.identity("r"), wp=sqlbuild.window_predicate("ratings", WINDOW, "r"),
        ep=sc.ent_predicate("r"))
    params = {**sc.params, **sqlbuild.window_params(WINDOW)}
    check_params(stmt, params)
    text = render(stmt)
    assert re.search(r'"r"\.text_project = %\(project\)s AND "r"\.text_language = %\(language\)s', text)


def test_ctes_come_in_dependency_order():
    """The source scope derives entities from sources; every other scope
    the other way round. A WITH list in the wrong order is an error."""
    ent, src = scope._source_ctes()
    by_source = scope.ScopeSet("source", "x", ent, src, {"project": "p", "language": "l", "sc_pat": "x"})
    assert [render(c)[:5] for c in by_source.ctes()] == ['"src"', '"ent"']
    ent, src = scope._entity_ctes_from_pattern()
    by_entity = scope.ScopeSet("entity", "x", ent, src, {"project": "p", "language": "l", "sc_pat": "x"})
    assert [render(c)[:5] for c in by_entity.ctes()] == ['"ent"', '"src"']


# ── One definition of "that is not a name" ──────────────────────────────
#
# The pattern that tells a document's title from the archive's identifier
# lives in four places: app/sqlbuild.py (this constant), app/scope.py and
# app/charts/drilldown.py (which both read it from here) and, spelled out
# because SQL cannot import, sql/03-places-view.sql. If the file and the
# constant drift, the suggestion list and the view disagree about what is
# offerable and the disagreement is invisible until somebody reads a hash.

def test_the_machine_name_pattern_is_the_same_text_in_the_sql_file():
    from pathlib import Path
    view = (Path(__file__).resolve().parents[2] / "sql" / "03-places-view.sql").read_text(encoding="utf-8")
    assert f"'{sqlbuild.MACHINE_NAME_REGEX}'" in view, (
        "sql/03-places-view.sql no longer spells MACHINE_NAME_REGEX the same way")


def test_the_pattern_matches_identifiers_and_leaves_titles_alone():
    import re
    machine = re.compile(sqlbuild.MACHINE_NAME_REGEX, re.I)
    # `src:` AND `ent:` ARE IDENTIFIERS TOO, and they were on this side of the
    # line in app/source_names.py and on the other one here: the extraction
    # gives every document an id of "src:" plus a slug, so "src:battery" was
    # hidden from the suggestion list by one spelling of this pattern and
    # printed as the title of a document by the other (app/source_names.py:
    # IDENTIFIER_REGEX is now the only spelling).
    for identifier in ("src_1416664", "src:battery", "ent:apple",
                       "8846ebcc63ff6a49245ef85e08e776de",
                       "8846EBCC63FF6A49245EF85E08E776DE", "a" * 40, "0" * 64):
        assert machine.match(identifier), identifier
    for title in ("Apple beats estimates", "src_", "Q3 2026", "src: battery",
                  "deadbeef", "8846ebcc63ff6a49245ef85e08e776de and more"):
        assert not machine.match(title), title


def test_a_source_is_labelled_by_its_host_when_its_name_is_an_identifier():
    """The expression the suggestion list and the scope probe group by."""
    text = sqlbuild.readable_source_label("s").as_string(None)
    # AS PSYCOPG WRITES IT, not as Python spells it: the pattern carries a
    # backslash now, so the literal comes out as an E-string with the
    # backslash escaped. Comparing the raw text would fail on a statement
    # that is perfectly correct.
    assert sql.Literal(sqlbuild.MACHINE_NAME_REGEX).as_string(None) in text
    assert "text_uri" in text and "text_name" in text
