"""The Events view: what happened, by entity or by type.

    GET /api/events?by=entity|type&q&date_from&date_to&page

Two questions, one endpoint, because they differ only in the predicate:

  by=entity  "what happened around Apple" - the term is resolved through
             app/scope.py, so a BUCKET named Apple pulls in every member
             (Apple Inc. and Apple the company, but not Apple the fruit),
             and it matches in every language of the project because
             translations share the entity ids.
  by=type    "every regulatory action" - the term is matched against
             events.text_type on the exact → prefix → substring ladder, so
             typing "Regul" finds "Regulatory action" and typing
             "Regulierungsmassnahme" finds the German rows.

An event is placed on COALESCE(date_eventdate, date_commissioned): the date
it happened, or failing that the date the document was commissioned. The
newest is first, and an event dated in the future - an announced launch -
therefore sits at the top, which is where a person looks for it.

There is deliberately no chunk prefilter on date_added here. An event may be
dated after the document that reports it, so "date_added >= from" would drop
exactly the announcements the list is most useful for (see sqlbuild's
NO_PREFILTER).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from psycopg import sql

from ..context import ContextDep
from ..db import Database, get_db
from ..scope import ladder_patterns, resolve_scope, resolve_terms
from ..source_names import source_payload
from ..sqlbuild import (identity, identity_params, perspective_params, perspective_scope,
                        timeline)

router = APIRouter(prefix="/api", tags=["events"])

PAGE_SIZE = 20
#: HOW MANY ROWS ONE REQUEST BRINGS BACK, against 20 shown at a time. The
#: reasoning is the Query view's, in the same words: the archive does the
#: same work for twenty rows as for two hundred, so the rows come in blocks
#: and the pager moves inside the block it has. See routers/api_query.py.
BLOCK_SIZE = 200
PAGES_PER_BLOCK = BLOCK_SIZE // PAGE_SIZE
BY_KINDS = ("entity", "type")

# ONE PAGE OF EVENTS, THEN WHAT BELONGS TO THAT PAGE. Four small statements
# rather than one clever one, and the reason is measured:
#
# A single SELECT that carries the source and the entities as two LEFT JOIN
# LATERALs and the total as count(*) OVER () answers the Events view's
# first, commonest request - no term, twenty rows - in 1.5 SECONDS on a
# preseed of seventeen events. Almost none of that is work:
# EXPLAIN ANALYZE put the query itself at 1.5 ms and JIT compilation at 1507
# ms (219 functions, "Optimization 916 ms, Emission 738 ms"). The planner
# cannot estimate a lateral over a hypertable, priced the plan at 154 MILLION,
# and PostgreSQL duly LLVM-optimised a query over eight rows. Dropping the
# window function alone left it at 542 000 - still over jit_optimize_above_cost
# - and 1.5 s. Without the laterals the same plan costs 403 and runs in 0.1 ms.
#
# That is slow in the way that breaks the page. The view loads its first
# list before anybody has typed anything, and a suggestion chosen while that
# request is still in flight is easily dropped on the floor (events.js).
#
# And the shape does not scale either way round: count(*) OVER () means every
# matching event is materialised - with both laterals evaluated - before LIMIT
# takes twenty of them. So: the page, the total, and the two hydrations, each
# a statement a planner can price.
_EVENT_PAGE = """
    SELECT ev.bigint_id, ev.text_task_id, ev.text_name, ev.text_type, ev.text_description,
           ev.text_reason, ev.text_relation_to_source, ev.text_fk_source_id,
           ev.date_eventdate, ev.date_commissioned,
           {tl} AS row_date
      FROM processed_data.events ev
     WHERE {idn_ev} AND {where}
     ORDER BY {tl} DESC, ev.bigint_id DESC
     LIMIT %(limit)s OFFSET %(offset)s
"""

# The same predicate, counted. A page past the last one has no rows, and a
# total that rode on the rows would then be 0 - the list would say "no events
# in this project yet" about a project with eight of them.
_EVENT_TOTAL = """
    SELECT count(*) AS total
      FROM processed_data.events ev
     WHERE {idn_ev} AND {where}
"""

# The sources of the events on the page. unnest of two arrays zips them, so
# the pairs stay pairs: an event belongs to (task, source), and matching the
# two columns independently would hand row A the source of row B.
_PAGE_SOURCES = """
    SELECT k.task_id, k.source_id, s.text_name, s.text_uri
      FROM unnest(%(src_tasks)s::text[], %(src_ids)s::text[]) AS k(task_id, source_id)
      JOIN processed_data.sources s
        ON s.text_task_id = k.task_id AND s.text_source_id = k.source_id
     WHERE {idn_s}
"""

# The entities of the events on the page. event_entities joins its event on
# (text_task_id, bigint_fk_event_id): the extraction gives an event no id of
# its own, so the row number inside the task is the key.
_PAGE_ENTITIES = """
    SELECT ee.text_task_id AS task_id, ee.bigint_fk_event_id AS event_id,
           e.text_entity_id AS id, e.text_name AS name, e.text_type AS type
      FROM unnest(%(ev_tasks)s::text[], %(ev_ids)s::bigint[]) AS k(task_id, event_id)
      JOIN processed_data.event_entities ee
        ON ee.text_task_id = k.task_id AND ee.bigint_fk_event_id = k.event_id
      JOIN processed_data.entities e
        ON e.text_project = ee.text_project AND e.text_language = ee.text_language
       AND e.text_task_id = ee.text_task_id AND e.text_entity_id = ee.text_fk_entity_id
     WHERE {idn_ee}
     ORDER BY e.text_name
"""

# The events of a set of entities: EXISTS rather than a join, so an event
# about three entities in scope is still one row.
_ABOUT_SCOPE = """
    EXISTS (SELECT 1
              FROM processed_data.event_entities ee2
             WHERE ee2.text_project = ev.text_project AND ee2.text_language = ev.text_language
               AND ee2.text_task_id = ev.text_task_id
               AND ee2.bigint_fk_event_id = ev.bigint_id
               AND (ee2.text_task_id, ee2.text_fk_entity_id) IN (SELECT task_id, id FROM ent))
"""


def _bad(error: str, hint: str) -> HTTPException:
    return HTTPException(400, {"error": error, "hint": hint})


def _parse_date(value: str | None, field: str) -> datetime | None:
    if not value or not value.strip():
        return None
    try:
        stamp = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        raise _bad(f"{field} is not a date", "use YYYY-MM-DD, for example 2026-01-31")
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def _date_predicate(date_from: datetime | None, date_to: datetime | None) -> sql.Composable:
    tl = timeline("events", "ev")
    parts = [sql.SQL("TRUE")]
    if date_from:
        parts.append(sql.SQL("{tl} >= %(date_from)s").format(tl=tl))
    if date_to:
        parts.append(sql.SQL("{tl} < %(date_to)s").format(tl=tl))
    return sql.SQL(" AND ").join(parts)


def _type_rungs(conn, project: str, language: str, q: str) -> tuple[str, str]:
    """The first rung of the ladder that finds a type, and its LIKE pattern.

    Run once, here, rather than inside the listing query: the page has to be
    able to say "no event type contains that" instead of showing an empty
    list that looks like "nothing happened"."""
    probe = ("SELECT 1 FROM processed_data.events ev "
             "WHERE ev.text_project = %(project)s AND ev.text_language = %(language)s "
             "AND lower(ev.text_type) LIKE %(pat)s LIMIT 1")
    for match, pattern in ladder_patterns(q):
        if conn.execute(probe, {"project": project, "language": language,
                                "pat": pattern}).fetchone():
            return match, pattern
    return "none", ladder_patterns(q)[0][1]


def _matched_types(conn, project: str, language: str, pattern: str) -> list[str]:
    rows = conn.execute(
        "SELECT ev.text_type AS name, count(*) AS n FROM processed_data.events ev "
        "WHERE ev.text_project = %(project)s AND ev.text_language = %(language)s "
        "AND lower(ev.text_type) LIKE %(pat)s GROUP BY ev.text_type ORDER BY n DESC, name LIMIT 10",
        {"project": project, "language": language, "pat": pattern}).fetchall()
    return [r["name"] for r in rows]


def _hydrate(conn, ctx, rows: list[dict[str, Any]]) -> tuple[dict, dict]:
    """The source and the entities of the events on this page.

    Two statements over at most twenty (task, id) pairs, instead of two
    LATERALs the planner cannot price (see _EVENT_PAGE). Nothing is fetched
    for events nobody is looking at."""
    if not rows:
        return {}, {}
    ident = identity_params(ctx.project, ctx.language)

    src_keys = sorted({(r["text_task_id"], r["text_fk_source_id"])
                       for r in rows if r["text_fk_source_id"]})
    sources: dict[tuple[str, str], dict[str, str]] = {}
    if src_keys:
        found = conn.execute(
            sql.SQL(_PAGE_SOURCES).format(idn_s=identity("s")),
            {**ident, "src_tasks": [k[0] for k in src_keys], "src_ids": [k[1] for k in src_keys]},
        ).fetchall()
        for r in found:
            sources[(r["task_id"], r["source_id"])] = {"name": r["text_name"] or "",
                                                       "uri": r["text_uri"] or ""}

    ev_keys = [(r["text_task_id"], r["bigint_id"]) for r in rows]
    entities: dict[tuple[str, int], list[dict[str, str]]] = {}
    found = conn.execute(
        sql.SQL(_PAGE_ENTITIES).format(idn_ee=identity("ee")),
        {**ident, "ev_tasks": [k[0] for k in ev_keys], "ev_ids": [k[1] for k in ev_keys]},
    ).fetchall()
    for r in found:
        if not r["name"]:
            continue
        item = {"id": r["id"] or "", "name": r["name"], "type": r["type"] or ""}
        bucket = entities.setdefault((r["task_id"], r["event_id"]), [])
        # An entity named twice in one event is one chip, not two: the
        # extraction writes a row per mention.
        if item not in bucket:
            bucket.append(item)
    return sources, entities


@router.get("/events")
def events(ctx: ContextDep, db: Database = Depends(get_db),
           by: str = Query("entity", description="entity | type"),
           q: str = Query("", description="an entity, a bucket, or an event type"),
           date_from: str = Query(""), date_to: str = Query(""),
           page: int = Query(0, ge=0, le=10000)):
    if by not in BY_KINDS:
        raise _bad(f"cannot list events by {by!r}", "by=entity or by=type")
    term = (q or "").strip()
    start = _parse_date(date_from, "date_from")
    end = _parse_date(date_to, "date_to")
    if end is not None and end.hour == 0 and end.minute == 0 and end.second == 0:
        # An end date is inclusive: "to 31 January" means the whole 31st.
        end = end + timedelta(days=1)
    if start and end and end <= start:
        raise _bad("the date range ends before it starts",
                   "swap the two dates, or clear one of them")

    params: dict[str, Any] = {**identity_params(ctx.project, ctx.language),
                              "date_from": start, "date_to": end,
                              "limit": BLOCK_SIZE, "offset": (page // PAGES_PER_BLOCK) * BLOCK_SIZE}
    with_clause: sql.Composable = sql.SQL("")
    resolved: dict[str, Any] = {"kind": by, "q": term, "label": term or "Everything",
                                "match": "all" if not term else "none", "names": [], "bucket": None}

    with db.read() as conn:
        if not term:
            where = sql.SQL("TRUE")
        elif by == "entity":
            scope = resolve_scope(conn, ctx, "entity", term)
            with_clause = scope.with_clause()
            params.update(scope.params)
            # `type`, `member` and `in_bucket` travel with the rest: they are
            # what lets the notice above the list name the BUCKET rather than
            # the typed term, and offer the single entity beside it
            # (app/scope.py, "A term that names ONE entity").
            resolved.update({k: scope.resolved.get(k) for k in
                             ("label", "match", "names", "bucket", "entities", "sources",
                              "type", "member", "in_bucket")})
            resolved["kind"] = "entity"
            where = sql.SQL(_ABOUT_SCOPE)
        else:
            # A BUCKET WINS OVER A LITERAL MATCH HERE TOO. "Product launch"
            # and "Produkteinführung" are two rows in event_types and one
            # thing to the person reading this page; where somebody has said
            # so, the list is the whole bucket and the notice says the
            # bucket's name rather than what was typed.
            bucket = resolve_terms(conn, ctx.project, "event_type", term)
            if bucket.match == "bucket":
                params["type_vals"] = bucket.lowered
                resolved.update({"match": "bucket", "names": bucket.values,
                                 "label": bucket.label,
                                 "bucket": bucket.grouping.as_dict()})
                where = sql.SQL("lower(ev.text_type) = ANY(%(type_vals)s)")
            else:
                match, pattern = _type_rungs(conn, ctx.project, ctx.language, term)
                params["type_pat"] = pattern
                names = _matched_types(conn, ctx.project, ctx.language, pattern) if match != "none" else []
                resolved.update({"match": match, "names": names,
                                 "label": names[0] if len(names) == 1 else term})
                where = sql.SQL("lower(ev.text_type) LIKE %(type_pat)s")

        # THE READER'S PERSPECTIVE NARROWS THE LIST AND THE TOTAL TOGETHER.
        # Both statements below are built from this one fragment, which is
        # why it is assembled here and not twice: a list of ten under a
        # heading that says 4,000 is a lie, and a filter applied to one of
        # the two would produce it.
        # An event carries the id of the document that reported it, so the
        # judgement is looked up on (task, source) exactly as everywhere else.
        full_where = sql.SQL("({w}) AND ({d}) AND ({p})").format(
            w=where, d=_date_predicate(start, end),
            p=perspective_scope(ctx.perspective, "ev"))
        params.update(perspective_params(ctx.perspective, ctx.min_importance))
        listing = sql.SQL("{c}{s}").format(c=with_clause, s=sql.SQL(_EVENT_PAGE).format(
            tl=timeline("events", "ev"), idn_ev=identity("ev"), where=full_where))
        rows = conn.execute(listing, params).fetchall()

        counting = sql.SQL("{c}{s}").format(c=with_clause, s=sql.SQL(_EVENT_TOTAL).format(
            idn_ev=identity("ev"), where=full_where))
        total = int(conn.execute(counting, params).fetchone()["total"])

        sources, entities = _hydrate(conn, ctx, rows)

    items = []
    for r in rows:
        key = (r["text_task_id"], r["bigint_id"])
        source = sources.get((r["text_task_id"], r["text_fk_source_id"] or ""), {})
        items.append({
            "id": f"{r['text_task_id']}:{r['bigint_id']}",
            "name": r["text_name"] or "",
            "type": r["text_type"] or "",
            "description": r["text_description"] or "",
            "reason": r["text_reason"] or "",
            "relation": r["text_relation_to_source"] or "",
            "date": r["row_date"].isoformat() if r["row_date"] else None,
            "dated": bool(r["date_eventdate"]),
            # Where it came from, named from what exists: the domain and
            # the slug of the address. `sources.text_name` is an identifier
            # in every row of this archive (app/source_names.py), so it was
            # printing "Source: src_1127664" under every event.
            "source": source_payload(source.get("name", ""), source.get("uri", "")),
            "entities": entities.get(key, []),
        })

    return {
        "by": by, "q": term, "resolved": resolved,
        "date_from": start.isoformat() if start else None,
        "date_to": date_to or None,
        "page": page, "page_size": PAGE_SIZE, "total": total,
        "pages": (total + PAGE_SIZE - 1) // PAGE_SIZE,
        # Which rows these are, so the page can tell whether the one it wants
        # is one it already holds.
        "block": page // PAGES_PER_BLOCK, "block_size": BLOCK_SIZE,
        "block_first": (page // PAGES_PER_BLOCK) * BLOCK_SIZE,
        "pages_per_block": PAGES_PER_BLOCK,
        "events": items,
        "project": ctx.project, "language": ctx.language,
    }
