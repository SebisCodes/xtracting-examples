"""What the archive contains, for the top bar and the form controls.

/api/projects feeds the project + language selector; /api/types/{kind} feeds
any control that offers a closed vocabulary - rating values, importance
levels, outlooks - in the order the archive defines rather than the alphabet.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from psycopg import sql

from fastapi import Request

from ..context import ContextDep, ContextError, list_projects, resolve_context
from ..db import Database, get_db
from ..scope import member_index

router = APIRouter(prefix="/api", tags=["meta"])

# A BUCKET APPEARS ONCE; ITS MEMBERS DO NOT APPEAR SEPARATELY.
#
# These vocabularies can be bucketed (app/scope.py: BUCKET_KINDS, plus the
# connection types, which the Connection Colours page groups instead). A
# dropdown that offered both "Unternehmen" and the bucket that holds it would
# let a person pick the member and silently defeat the merge: they would get
# the German half of the answer with nothing on screen saying a half was
# missing. So the members are folded into one row named after the bucket.
#
# Entity NAMES are deliberately not in this list. "Apple (Fruit)" is a thing
# a person is entitled to look at on its own - app/scope.py has a whole
# section on the term that names one entity, and the Events page offers the
# way back into the bucket - so the entity list keeps every name.
BUCKETED_VOCABULARY = {
    "entity": "entity_type",
    "source": "source_type",
    "event": "event_type",
    "location": "location_type",
    "attribute": "attribute_type",
    "unit": "unit",
    "market_topic": "market_topic",
    "connection": "connection_type",
}

# The vocabulary tables of the archive, by the short name a URL carries.
# A dict rather than f-string interpolation of the path: the kind ends up in a
# table name, and only these sixteen may.
TYPE_TABLES = {
    "source": "source_types",
    "entity": "entity_types",
    "event": "event_types",
    "location": "location_types",
    "perspective": "perspective_types",
    "rating": "rating_types",
    "rating_value": "rating_values",
    "connection": "connection_types",
    "relation": "relation_types",
    "attribute": "attribute_types",
    "unit": "unit_types",
    "importance": "importance_types",
    "market_topic": "market_topic_types",
    "outlook": "outlook_types",
    "sentiment": "sentiment_types",
    "trustlist": "trustlist_types",
}


@router.get("/projects")
def projects(request: Request, db: Database = Depends(get_db)):
    """Every project and language in the archive, and which one is current.

    THIS ENDPOINT MUST NOT REQUIRE A VALID CONTEXT, and that is not a detail.
    The selection lives in a cookie; a project can disappear from under it -
    an archive re-imported under different names, a project deleted. If the
    list that lets a person CHANGE the selection is itself refused because the
    selection is wrong, they are locked out of the whole dashboard with no way
    back except clearing cookies by hand. That happened.

    So the list is always answered. `current` says what would be used, and
    `stale` names the stored selection that no longer exists, so the page can
    say why it moved rather than silently showing somebody else's project.
    """
    rows = list_projects(db)
    stale = None
    try:
        ctx = resolve_context(request, db)
    except (HTTPException, ContextError) as exc:
        # resolve_context raises HTTPException(400); ContextError is what the
        # pure picker raises. Catch both, because which one arrives is an
        # implementation detail of a function this endpoint must survive.
        detail = getattr(exc, "detail", None)
        reason = (detail.get("error") if isinstance(detail, dict) else None) or str(exc)
        stale = {"project": _clean_cookie(request, "xd.project"),
                 "language": _clean_cookie(request, "xd.language"),
                 "reason": reason}
        first = rows[0] if rows else None
        ctx = None
        current = ({"project": first["project"], "language": first["language"]}
                   if first else {"project": "", "language": ""})
    else:
        current = {"project": ctx.project, "language": ctx.language}
    return {
        "projects": [
            {
                "project": r["project"],
                "language": r["language"],
                "tasks": r["tasks"],
                "last_evaluated": r["last_evaluated"].isoformat() if r["last_evaluated"] else None,
            }
            for r in rows
        ],
        "current": current,
        "stale": stale,
    }


def _clean_cookie(request: Request, name: str) -> str:
    from urllib.parse import unquote
    return unquote(request.cookies.get(name, "") or "").strip()


@router.get("/types/{kind}")
def types(kind: str, ctx: ContextDep, db: Database = Depends(get_db)):
    table = TYPE_TABLES.get(kind)
    if table is None:
        raise HTTPException(404, {"error": f"no vocabulary {kind!r}",
                                  "hint": "one of " + ", ".join(TYPE_TABLES)})
    # The fixed vocabularies are seeded under project '' (database/init/02-
    # vocabularies.sql); the open ones arrive with the collector under the
    # project and language they were seen in. Both belong in the list.
    query = sql.SQL("""
        SELECT text_name AS name, text_description AS description, float_value AS value,
               bool_enabled AS enabled
          FROM processed_data.{table}
         WHERE (text_project = %(project)s AND text_language = %(language)s)
            OR text_project = ''
         ORDER BY float_value NULLS LAST, text_name
    """).format(table=sql.Identifier(table))
    with db.read() as conn:
        rows = [dict(r) for r in
                conn.execute(query, {"project": ctx.project, "language": ctx.language}).fetchall()]
        grouped = BUCKETED_VOCABULARY.get(kind)
        held = member_index(conn, ctx.project, grouped) if grouped else {}
    items, merged = _fold_buckets(rows, held)
    return {"kind": kind, "table": table, "items": items,
            # Which rows are a group rather than a single value, and how many
            # values each of them stands for. A page that draws the list can
            # then say so instead of the reader wondering why a type they know
            # is missing.
            "buckets": merged, "bucket_kind": grouped or ""}


def _fold_buckets(rows: list[dict], held: dict[str, str]) -> tuple[list[dict], list[dict]]:
    """Members replaced by the bucket that holds them, once, in the place the
    first member stood - so the order of the list is not reshuffled by an
    edit on a settings page."""
    if not held:
        return rows, []
    out: list[dict] = []
    seen: dict[str, dict] = {}
    for row in rows:
        holder = held.get(str(row.get("name") or "").strip().lower())
        if holder is None:
            out.append(row)
            continue
        found = seen.get(holder)
        if found is None:
            found = {**row, "name": holder, "bucket": True, "members": [row["name"]]}
            seen[holder] = found
            out.append(found)
        else:
            found["members"].append(row["name"])
    return out, [{"name": name, "members": g["members"]} for name, g in seen.items()]
