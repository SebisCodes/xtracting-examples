"""The list at the foot of a tab: the rows themselves, not a picture of them.

WHY A LIST AND NOT A CHART. A bar says how many; on three of the eight tabs the
question a reader actually arrives with is WHICH - which documents came in
last, what an attribute's value IS, what happened. A bar chart of "the newest
sources" answers none of those: it draws one bar per document, all of them 1,
sorted by a date the bars cannot show.

So those tabs end in a list, under the rule at the foot of the page, and it is
allowed to be tall - which is the other half of why it lives down there rather
than above the grid.

EVERY LIST IS THE SAME SEARCH AS THE CHARTS ABOVE IT. Same scope, same period,
same perspective predicate, built from the same helpers - so a page that says
"every count here is a subset" is telling the truth about the list as well.
"""

from __future__ import annotations

from typing import Any

from psycopg import sql

from .. import sqlbuild
from ..source_names import source_payload
from ..textclean import plain

#: How many rows a summary list holds. Enough that scrolling it is worth more
#: than opening the drilldown, few enough that the page stays a page: the
#: drilldown itself is one click away and pages properly.
LIST_LIMIT = 25

def _with(scope, body: sql.Composable) -> sql.Composable:
    ctes = list(scope.ctes()) if scope.scoped else []
    if not ctes:
        return body
    return sql.SQL("WITH {c} {b}").format(c=sql.SQL(", ").join(ctes), b=body)


def _base_params(scope, ctx, window) -> dict[str, Any]:
    return {**sqlbuild.identity_params(ctx.project, ctx.language),
            **sqlbuild.perspective_params(getattr(ctx, "perspective", ""),
                                          getattr(ctx, "min_importance", "")),
            **sqlbuild.window_params(window), **scope.params}


def _persp(ctx, table: str, alias: str = "t") -> sql.Composable:
    return sqlbuild.perspective_scope(getattr(ctx, "perspective", ""), alias,
                                      sqlbuild.source_id_column(table))


# ── Sources: the newest documents ────────────────────────────
#
# The same row the drilldown lists, in the same order, because the request was
# for "a list like the drilldown with the newest sources" - and because a
# reader who clicks a bar and gets one shape of table should not meet another
# shape at the foot of the page.

_NEWEST_SOURCES = """
    SELECT count(*) OVER ()                     AS n_all,
           t.text_name                          AS name,
           t.text_uri                           AS uri,
           t.text_type                          AS type,
           t.text_importance                    AS importance,
           t.bool_trustful                      AS trustful,
           t.text_summary                       AS summary,
           t.date_written                       AS written,
           t.date_added                         AS added
      FROM processed_data.sources AS t
     WHERE {idn} AND {win} AND ({scope}) AND ({persp})
     ORDER BY coalesce(t.date_written, t.date_added) DESC, t.bigint_id DESC
     LIMIT {cap}
"""


def newest_sources(conn, scope, ctx, window) -> dict[str, Any]:
    from .sources import scope_predicate as source_scope
    body = sql.SQL(_NEWEST_SOURCES).format(
        idn=sqlbuild.identity("t"),
        win=sqlbuild.window_predicate("sources", window, "t"),
        scope=source_scope(scope),
        persp=_persp(ctx, "sources"),
        cap=sql.Literal(LIST_LIMIT))
    rows = conn.execute(_with(scope, body), _base_params(scope, ctx, window)).fetchall()
    return {
        "kind": "list", "layout": "sources",
        "title": "The newest documents",
        "note": "Newest first, by the date the document itself carries.",
        "empty": "No document was written in this period.",
        "total": int(rows[0]["n_all"]) if rows else 0,
        # THE NAME IS DERIVED, NEVER THE ARCHIVE'S IDENTIFIER. `sources.text_name`
        # is "src_1127664" in every row of a real archive (app/source_names.py),
        # and a list of twenty-five of those is a list nobody can read. The same
        # helper the Events view and the Dashboard use turns the address into
        # the domain and the slug, and says that it did.
        "rows": [{
            **{k: v for k, v in source_payload(r["name"], r["uri"]).items()
               if k in ("name", "uri", "domain", "title", "derived")},
            "type": r["type"] or "",
            "importance": r["importance"] or "",
            "trustful": bool(r["trustful"]),
            "summary": (r["summary"] or "")[:600],
            "date": (r["written"] or r["added"]).isoformat() if (r["written"] or r["added"]) else "",
        } for r in rows],
    }


# ── Attributes: the name, the value and the unit ─────────────
#
# "Name: Value Unit" on the left and what the document said on the right, with
# the way to the whole of it. An attribute is the one kind of row in this
# archive that IS its value - "Battery capacity: 5000 mAh" is the fact, and a
# bar chart counting how many attributes were written says nothing about it.

_NEWEST_ATTRIBUTES = """
    SELECT count(*) OVER ()             AS n_all,
           t.text_name                  AS name,
           t.text_value                 AS value,
           t.text_unit                  AS unit,
           t.text_type                  AS type,
           t.text_description           AS description,
           t.text_reason                AS reason,
           t.date_added                 AS added,
           e.text_name                  AS entity,
           s.text_name                  AS source_name,
           s.text_uri                   AS uri
      FROM processed_data.attributes AS t
      LEFT JOIN processed_data.entities AS e
             ON e.text_project = t.text_project AND e.text_language = t.text_language
            AND e.text_task_id = t.text_task_id AND e.text_entity_id = t.text_fk_entity_id
      LEFT JOIN processed_data.sources AS s
             ON s.text_project = t.text_project AND s.text_language = t.text_language
            AND s.text_task_id = t.text_task_id AND s.text_source_id = t.text_fk_source_id
     WHERE {idn} AND {win} AND ({scope}) AND ({persp})
     ORDER BY t.date_added DESC, t.bigint_id DESC
     LIMIT {cap}
"""


def newest_attributes(conn, scope, ctx, window) -> dict[str, Any]:
    from .attributes import scope_predicate as attribute_scope
    body = sql.SQL(_NEWEST_ATTRIBUTES).format(
        idn=sqlbuild.identity("t"),
        win=sqlbuild.window_predicate("attributes", window, "t"),
        scope=attribute_scope(scope),
        persp=_persp(ctx, "attributes"),
        cap=sql.Literal(LIST_LIMIT))
    rows = conn.execute(_with(scope, body), _base_params(scope, ctx, window)).fetchall()
    return {
        "kind": "list", "layout": "attributes",
        "title": "The newest attributes",
        "note": "Newest first. The value is what the document said; the line beside it "
                "is why it said so.",
        "empty": "No attribute was recorded in this period.",
        "total": int(rows[0]["n_all"]) if rows else 0,
        "rows": [{
            "name": r["name"] or "",
            "value": r["value"] or "",
            "unit": r["unit"] or "",
            "type": r["type"] or "",
            "entity": r["entity"] or "",
            # WHAT THE DOCUMENT SAID ABOUT IT, which is the half of an
            # attribute a number cannot carry. The reason is the extraction's
            # own sentence; the description is the attribute's. Either is
            # better than nothing, and neither is ever the whole of it - which
            # is what "More" is for.
            "summary": (r["description"] or r["reason"] or "")[:400],
            "source": source_payload(r["source_name"], r["uri"])["name"],
            "uri": r["uri"] or "",
            "date": r["added"].isoformat() if r["added"] else "",
        } for r in rows],
    }


_NEWEST_EVENTS = """
    SELECT count(*) OVER ()             AS n_all,
           t.text_name                  AS name,
           t.text_type                  AS type,
           t.text_description           AS description,
           t.text_reason                AS reason,
           t.date_eventdate             AS happened,
           t.date_added                 AS added,
           s.text_name                  AS source_name,
           s.text_uri                   AS uri
      FROM processed_data.events AS t
      LEFT JOIN processed_data.sources AS s
             ON s.text_project = t.text_project AND s.text_language = t.text_language
            AND s.text_task_id = t.text_task_id AND s.text_source_id = t.text_fk_source_id
     WHERE {idn} AND {win} AND ({scope}) AND ({persp})
     ORDER BY coalesce(t.date_eventdate, t.date_added) DESC, t.bigint_id DESC
     LIMIT {cap}
"""


def newest_events(conn, scope, ctx, window) -> dict[str, Any]:
    from .events import _scope as event_scope
    body = sql.SQL(_NEWEST_EVENTS).format(
        idn=sqlbuild.identity("t"),
        win=sqlbuild.window_predicate("events", window, "t"),
        scope=event_scope(scope),
        persp=_persp(ctx, "events"),
        cap=sql.Literal(LIST_LIMIT))
    rows = conn.execute(_with(scope, body), _base_params(scope, ctx, window)).fetchall()
    return {
        "kind": "list", "layout": "events",
        "title": "The newest events",
        "note": "Newest first, by the date the event itself carries.",
        "empty": "No event was recorded in this period.",
        "total": int(rows[0]["n_all"]) if rows else 0,
        "rows": [{
            "name": r["name"] or "",
            "type": r["type"] or "",
            "summary": (r["description"] or r["reason"] or "")[:400],
            "source": source_payload(r["source_name"], r["uri"])["name"],
            "uri": r["uri"] or "",
            "date": (r["happened"] or r["added"]).isoformat() if (r["happened"] or r["added"]) else "",
        } for r in rows],
    }


#: WHICH TAB ENDS IN A LIST, and only three do.
#
# The list is the one thing on a tab that is not a picture: the rows
# themselves, under everything that was drawn. Connections lost its list when
# the map started saying each tie in words, and Market Insights lost its one
# when the topics and the entities moved up into the charts - a tab does not
# need a second answer at the bottom to a question the pictures have already
# answered.
_RAW_BUILDERS = {
    "sources": newest_sources,
    "attributes": newest_attributes,
    "events": newest_events,
}


def _readable(summary: dict[str, Any]) -> dict[str, Any]:
    """Every string in every row, as a person would read it.

    A crawled page says `W&auml;rtsil&auml; Corporation` and the archive
    stores what the page said, so the decoding happens where the value
    becomes something to read (app/textclean.py). Here rather than in each
    builder: three lists with thirty fields between them, and the next one
    would have to remember.
    """
    rows = summary.get("rows")
    if isinstance(rows, list):
        summary["rows"] = [{k: plain(v) for k, v in row.items()} for row in rows]
    return summary


BUILDERS = {tab: (lambda build: lambda *a, **k: _readable(build(*a, **k)))(fn)
            for tab, fn in _RAW_BUILDERS.items()}
