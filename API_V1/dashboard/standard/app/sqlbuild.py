"""Building blocks for the SQL every chart and list is made of.

Two rules that every statement in the dashboard follows are enforced here so
they cannot be forgotten in one of forty chart builders:

1. Every predicate binds project AND language. A row is never "in the archive",
   it is in one project's one language version, and a count that forgets the
   language doubles for every translation a job requested.

2. Charts are placed on the timeline column, not on date_added. date_added is
   when the collector happened to fetch a row; date_commissioned is when the
   task was submitted, date_written when the document says it was written,
   date_eventdate when the event happened. Those are the dates a person means.

   The one place date_added still appears is the CHUNK PREFILTER. The
   hypertables are partitioned by date_added, so "date_added >= window start"
   lets TimescaleDB skip whole chunks, and it is valid because a row is always
   archived AFTER it was commissioned: a timeline value inside the window
   implies a date_added inside or after it. Events are the exception - an event
   can be dated in the future relative to its document, so the prefilter would
   drop next month's announcements, and the events branch runs without it.

Fragments are psycopg.sql objects with NAMED placeholders (%(name)s) and a
params dict, so a scope CTE, a window predicate and a builder's own filters can
be concatenated and their params merged without counting positions.
"""

from __future__ import annotations

from typing import Any

from psycopg import sql

from . import source_names
from .timeframes import DATE_TRUNC_UNITS, Window

# The archive's data tables, and the alias each builder uses for them.
TABLES = (
    "sources", "entities", "locations", "events", "ratings", "connections",
    "attributes", "market_insights", "source_importances_by_perspective",
    "event_entities", "tasks",
)

# The column a table's rows are placed on for any chart with a time axis.
# {a} is the table alias. Never date_added - see the module docstring.
TIMELINE_COLUMN: dict[str, str] = {
    "sources": "COALESCE({a}.date_written, {a}.date_commissioned)",
    "events": "COALESCE({a}.date_eventdate, {a}.date_commissioned)",
}
_DEFAULT_TIMELINE = "{a}.date_commissioned"

# THE ARRIVAL CLOCK, which is the OTHER question the same rows answer.
#
# `date_commissioned` is when the task that produced a row was submitted, and
# it is the one column every archive table has that means "when this reached
# us". The two entries above deliberately override it with a CONTENT date,
# because a chart of sources over time must plot when the documents were
# WRITTEN - that is what the picture is for.
#
# Counting is not plotting, and mixing the two produces a card nobody can
# read. Running the Home page's "Archived, per period" tiles through
# TIMELINE_COLUMN would have six of them count arrivals and two count
# content dates under one heading that promises the first. Measured on a
# real archive: date_added spanning a single second while date_written
# spans five years, and the card reading Sources 3/4/6/9 beside Entities
# 158/158/158/158 - eight tiles of one import, two of which claim almost
# nothing arrived.
#
# So `timeline(..., arrival=True)` asks for this clock instead, and
# stats_statement() is the caller that does. window_predicate() and bucket()
# stay on the content clock: the Diagrams plot when things HAPPENED.

# Tables whose timeline can legitimately lie after date_added, so the chunk
# prefilter must not be applied to them.
NO_PREFILTER = frozenset({"events"})

# WHAT EACH TABLE CALLS THE DOCUMENT A ROW CAME FROM. `sources` IS the
# document, so its own id is the key; everywhere else it is a foreign key.
# perspective_scope() joins on it, and a table missing from this map would
# silently be filtered on a column it does not have.
SOURCE_ID_COLUMN: dict[str, str] = {"sources": "text_source_id"}
_DEFAULT_SOURCE_ID = "text_fk_source_id"

# Horizontal "top N" charts never show more than this many categories.
KEY_CHART_LIMIT = 25


def table(name: str) -> sql.Composable:
    if name not in TABLES:
        raise ValueError(f"unknown archive table {name!r}")
    return sql.SQL("processed_data.") + sql.Identifier(name)


def timeline(name: str, alias: str = "t", *, arrival: bool = False) -> sql.Composable:
    """The timeline expression for a table, as SQL.

    `arrival=True` returns the arrival clock - date_commissioned for every
    table, with no content-date override - for a caller that is counting
    what came in rather than plotting when it happened. A parameter rather
    than a second dict, and never a mutation of TIMELINE_COLUMN: the module
    dict is read by every chart in the dashboard, and a counter that edited
    it would move forty pictures to answer one card."""
    if name not in TABLES:
        raise ValueError(f"unknown archive table {name!r}")
    template = _DEFAULT_TIMELINE if arrival else TIMELINE_COLUMN.get(name, _DEFAULT_TIMELINE)
    return sql.SQL(template.format(a="{a}")).format(a=sql.Identifier(alias))


def source_id_column(name: str) -> str:
    """The column that names the document a row of `name` came from."""
    if name not in TABLES:
        raise ValueError(f"unknown archive table {name!r}")
    return SOURCE_ID_COLUMN.get(name, _DEFAULT_SOURCE_ID)


def identity(alias: str = "t") -> sql.Composable:
    """`project AND language` for one alias. Every statement has this."""
    return sql.SQL("{a}.text_project = %(project)s AND {a}.text_language = %(language)s").format(
        a=sql.Identifier(alias))


def identity_params(project: str, language: str) -> dict[str, Any]:
    return {"project": project, "language": language}


def window_predicate(name: str, window: Window, alias: str = "t") -> sql.Composable:
    """Timeline inside the window, plus the chunk prefilter where it is valid.

    Params: w_start, w_end (see window_params)."""
    tl = timeline(name, alias)
    pred = sql.SQL("{tl} >= %(w_start)s AND {tl} < %(w_end)s").format(tl=tl)
    if name not in NO_PREFILTER:
        pred = pred + sql.SQL(" AND {a}.date_added >= %(w_start)s").format(a=sql.Identifier(alias))
    return pred


def window_params(window: Window) -> dict[str, Any]:
    return {"w_start": window.start, "w_end": window.end}


def bucket(name: str, window: Window, alias: str = "t") -> sql.Composable:
    """`date_trunc(unit, timeline)` - the x value of a time chart.

    The unit is interpolated as SQL text, never bound from the request: it
    comes from the timeframe table and is one of DATE_TRUNC_UNITS."""
    if window.unit not in DATE_TRUNC_UNITS:
        raise ValueError(f"unit {window.unit!r} is not a date_trunc unit")
    return sql.SQL("date_trunc({u}, {tl})").format(
        u=sql.Literal(window.unit), tl=timeline(name, alias))


def limit(n: int = KEY_CHART_LIMIT) -> sql.Composable:
    """LIMIT for a key chart. Always present: a "by name" chart over a real
    archive would otherwise return every entity ever seen."""
    if n < 1:
        raise ValueError("limit must be positive")
    return sql.SQL("LIMIT {n}").format(n=sql.Literal(int(n)))


def vocabulary_scope(alias: str = "v") -> sql.Composable:
    """The vocabulary rows that apply to a project: its own, plus the ones
    seeded under the empty project by database/init/02-vocabularies.sql. Rating
    values and importance levels get their scale order from there."""
    return sql.SQL("{a}.text_project IN (%(project)s, '')").format(a=sql.Identifier(alias))


# ── The reader's perspective, and how much a document has to matter ──────
#
# WHY THIS IS NOT PART OF identity().
#
# identity() is applied to thirty-two places, and about half of them are a
# LOOKUP: `sources` inside a LATERAL that fetches one title, `entities` in a
# name join, `event_entities`, `rating_values`. Asking there whether the
# document is important enough for the reader's perspective is meaningless at
# best - the lateral has already been narrowed by the row it hangs off - and
# actively wrong at worst, because it would drop the NAME of a row the chart
# has already counted and leave a bar with a blank label under it. So this is
# its own helper, and the idiom is the one ScopeSet.scoped already uses: no
# perspective chosen returns TRUE, and every caller can concatenate it
# unconditionally.
#
# WHY EXISTS AND NOT A JOIN. charts/drilldown.py:source_join records the
# reason in the same words: two rows of one task can carry the same source id
# after a re-run, and a join would double every count that passes through it.
# The key is the PAIR (text_task_id, text_fk_source_id) - a source id is only
# unique inside its task.
#
# WHY THE COMPARISON IS INVERTED - "not one of the levels BELOW the
# threshold" rather than "one of the levels at or above it". The two are the
# same sentence about a level the vocabulary knows, and they differ on the
# two levels it does not:
#
#   * A TRANSLATED LABEL. vocabulary_scope() binds the project but not the
#     language, so `importance_types` holds the English scale and a German
#     view's rows say "Hohe Wichtigkeit". Under ">= threshold" every one of
#     them resolves to NULL, falls out of the comparison, and the German
#     view empties itself in silence. Under "not below" they are kept: the
#     filter cannot order that language's labels, so it does not pretend to,
#     and falls back to unfiltered rather than to nothing. This is a known
#     limit, written here rather than papered over - the fix is a language
#     column in the vocabulary join, and that is a change to the archive.
#   * A THRESHOLD THE VOCABULARY DOES NOT KNOW. The scalar subquery is then
#     NULL, `float_value < NULL` is NULL, the array is empty, and `<> ALL`
#     of an empty array is TRUE. Same fallback, for the same reason.
#
# WHY <> ALL (ARRAY(...)) AND NOT A CORRELATED LATERAL. The plan this was
# written from spelled the level lookup as a LEFT JOIN LATERAL per row of
# `source_importances_by_perspective`, which is what charts/drilldown.py's
# _importance_join does for ONE chart. Measured on a large archive
# (2.28 M entities, one project, perspective "Investor",
# "High Importance and above"): the lateral ran 232,087 times and the query
# took 11.0 SECONDS. As an array built once from the same table and the same
# float_value it is 1.7 seconds - the identical answer, 6.4 times faster,
# and still "worse than" decided by a number in the database rather than by
# a rank map in Python (database/init/02-vocabularies.sql says why that
# number exists).
_PERSPECTIVE_EXISTS = (
    "EXISTS (SELECT 1 FROM processed_data.source_importances_by_perspective si "
    "WHERE si.text_project = %(project)s AND si.text_language = %(language)s "
    "AND si.text_task_id = {a}.text_task_id "
    "AND si.text_fk_source_id = {a}.{c} "
    "AND si.text_perspective = %(perspective)s "
    "AND si.text_importance <> ALL (ARRAY("
    "SELECT lv.text_name FROM ("
    "SELECT DISTINCT ON (it.text_name) it.text_name, it.float_value "
    "FROM processed_data.importance_types it WHERE {vs} "
    "ORDER BY it.text_name, (it.text_project <> '') DESC) AS lv "
    "WHERE lv.float_value < ("
    "SELECT m.float_value FROM processed_data.importance_types m "
    "WHERE m.text_name = %(min_importance)s AND {vm} "
    "ORDER BY (m.text_project <> '') DESC LIMIT 1))))"
)


def perspective_scope(perspective: str, alias: str = "t",
                      column: str = _DEFAULT_SOURCE_ID) -> sql.Composable:
    """"this row's document is at least this important, for this reader".

    Returns TRUE when no perspective is chosen, so a statement can carry it
    unconditionally. A document with NO judgement for the chosen perspective
    is HIDDEN - decided with the customer, and it is what EXISTS says.

    Params: perspective, min_importance (see perspective_params), plus the
    project and language identity() already binds."""
    if not (perspective or "").strip():
        return sql.SQL("TRUE")
    return sql.SQL(_PERSPECTIVE_EXISTS).format(
        a=sql.Identifier(alias), c=sql.Identifier(column),
        vs=vocabulary_scope("it"), vm=vocabulary_scope("m"))


def perspective_params(perspective: str, min_importance: str) -> dict[str, Any]:
    """The two parameters perspective_scope() binds - nothing at all when no
    perspective is chosen, because then the predicate names neither."""
    if not (perspective or "").strip():
        return {}
    return {"perspective": perspective, "min_importance": min_importance}


def ilike_any(column: sql.Composable, param: str) -> sql.Composable:
    """`column ILIKE ANY(%(param)s)` for a list of patterns."""
    return sql.SQL("{c} ILIKE ANY(%({p})s)").format(c=column, p=sql.SQL(param))


def like_prefix(term: str) -> str:
    """The pattern for a prefix match, with LIKE's own metacharacters escaped
    so a term containing % or _ matches those characters literally."""
    return _escape_like(term) + "%"


def like_substring(term: str) -> str:
    return "%" + _escape_like(term) + "%"


def _escape_like(term: str) -> str:
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def host_expression(alias: str = "s") -> sql.Composable:
    """The host part of text_uri, for "by domain" charts and source scope.
    Pure SQL because no extension is installed in the archive image."""
    return sql.SQL("substring({a}.text_uri from '^[a-zA-Z][a-zA-Z0-9+.-]*://([^/:?#]+)')").format(
        a=sql.Identifier(alias))


# WHAT A DOCUMENT IS CALLED - AND WHEN THAT IS NOT A NAME AT ALL.
#
# `processed_data.sources.text_name` is meant to be the document's title. On
# a real archive it need not be: hundreds of thousands of rows carrying
# `src_<id>` or a 32-character checksum, every one of them DISTINCT. Neither
# shape is a name, and a unique value per row breaks three things at once
# for anybody who looks:
#
#   * the suggestion list offered checksums - one row per document, unreadable;
#   * every one of them showed the count 1, because a unique name groups alone
#     and no query bug is needed for that;
#   * typing "vogue" found nothing, though 44 documents come from vogue.com -
#     the list matched the name, and the name is a hash.
#
# So wherever a source has to be NAMED, the identifier is stepped over and the
# host stands in its place: "www.vogue.com, 44 documents" is both readable and
# true. The search behind it is unaffected and always was - scope._source_body
# matches name OR host OR uri, which is why the archive could find the vogue
# document while the field that suggests it could not.
#
# The same pattern lives in three places that must not drift: here, in
# app/charts/drilldown.py (readable_title, which prefers the URL slug because
# a drilldown row has room for a title) and literally in sql/03-places-view.sql
# (dashboard.source_names). tests/unit/test_sqlbuild.py pins all three.
# The shapes a "name" has that is not one. Defined in app/source_names.py,
# which is the plain module that decides the same question row by row - one
# spelling, or a document is an identifier to one half of this dashboard and
# a title to the other.
MACHINE_NAME_REGEX = source_names.IDENTIFIER_REGEX


def readable_source_label(alias: str = "s") -> sql.Composable:
    """The document's name where it is one, otherwise the host of its URI.

    Used where a source has to be GROUPED and shown under one heading. It is
    deliberately not readable_title(): that one reads the URL slug as well,
    which is right for a single row and wrong for a group key, where the slug
    would split one host into as many groups as it has articles."""
    return sql.SQL(
        "CASE WHEN {a}.text_name IS NULL OR {a}.text_name = '' "
        "OR {a}.text_name ~* {rx} THEN {host} ELSE {a}.text_name END"
    ).format(a=sql.Identifier(alias), rx=sql.Literal(MACHINE_NAME_REGEX),
             host=host_expression(alias))


def stats_statement(name: str, alias: str = "t") -> sql.Composable:
    """The dashboard's counters for one table, in ONE statement.

    hour/day/week/month/year as FILTER clauses over a single pass that is
    prefiltered on date_added for chunk exclusion. Deliberately no unbounded
    total: on a multi-year archive that is a full scan for a number nobody
    acts on.

    EVERY TILE IS ON THE ARRIVAL CLOCK. Counters run through timeline()
    would give `sources` the date the document says it was written and
    `events` the date the event happened, while the other six tables have
    only date_commissioned. One card headed "Archived, per period" would
    then count six tables by when they arrived and two by what they are
    about, and nothing on the page would say so.

    It is not a subtle difference on a real archive. Measured on one:
    date_added spanning a single second - one import - while date_written
    spans five years, and the card reading Sources 3/4/6/9 beside Entities
    158/158/158/158. The same import, and the tile for the documents saying
    almost nothing had come in.

    The preseed alone cannot catch it, because it sets commissioned =
    written + 30 minutes, so on that data the two clocks agree to within
    half an hour and every window is a day or more. A test for this needs a
    row whose content date is a year old and whose arrival is now.

    The charts are NOT moved: window_predicate() and bucket() stay on the
    content clock, because a diagram plots when things HAPPENED and that is
    what those charts are for.

    EVERY WINDOW IS CLOSED AT BOTH ENDS, and the upper end is the half that
    is easy to forget. "The last hour" written as `>= now() - '1 hour'` with
    nothing above is satisfied by a row dated in the FUTURE - and so are the
    day, the week, the month and the year.

    A real archive has such rows and they are not rare: thousands of events
    carrying an eventdate later than now (years in the 2100s and beyond),
    and sources carrying a date a year in their own future. A Home page would
    then report thousands of events "in the last hour" on an archive that is
    receiving nothing at all, which is the kind of number that makes a reader
    distrust every other number on the page.

    A date after now is a fact about the extraction, not about the last
    hour, so it is counted in no window at all. It is still in the archive
    and still in every chart that plots a real axis - this counter simply
    stops claiming it just arrived."""
    tl = timeline(name, alias, arrival=True)
    return sql.SQL(
        "SELECT "
        "count(*) FILTER (WHERE {tl} >= now() - interval '1 hour'  AND {tl} <= now()) AS hour, "
        "count(*) FILTER (WHERE {tl} >= now() - interval '1 day'   AND {tl} <= now()) AS day, "
        "count(*) FILTER (WHERE {tl} >= now() - interval '7 days'  AND {tl} <= now()) AS week, "
        "count(*) FILTER (WHERE {tl} >= now() - interval '30 days' AND {tl} <= now()) AS month, "
        "count(*) FILTER (WHERE {tl} >= now() - interval '1 year'  AND {tl} <= now()) AS year "
        "FROM {t} AS {a} "
        "WHERE {idn} AND {a}.date_added >= now() - interval '1 year'"
    ).format(tl=tl, t=table(name), a=sql.Identifier(alias), idn=identity(alias))


def render(fragment: sql.Composable) -> str:
    """The SQL text of a fragment, for tests and logs. psycopg 3.2+ renders
    without a connection; identifiers are quoted as PostgreSQL would."""
    return fragment.as_string(None)


def placeholders(text: str) -> set[str]:
    """The named placeholders a rendered statement expects."""
    import re
    return set(re.findall(r"%\(([A-Za-z_][A-Za-z0-9_]*)\)s", text))


def check_params(fragment: sql.Composable, params: dict[str, Any]) -> None:
    """Fail loudly when a statement names a parameter nobody supplied. psycopg
    would raise too, but this names the statement and the key."""
    missing = placeholders(render(fragment)) - set(params)
    if missing:
        raise KeyError(f"statement expects parameters {sorted(missing)} that were not supplied")
