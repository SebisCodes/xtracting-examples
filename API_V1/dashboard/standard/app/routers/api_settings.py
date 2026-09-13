"""Buckets and connection colours: the two things the customer edits.

Everything here writes to the `dashboard` schema and nothing at all to
`processed_data` - the archive is what the extraction said, and a settings
page that could change it would turn a result into an opinion. What the two
pages do is decide how the archive is READ:

    buckets         "Apple Inc." and "Apple (Company)" are one company to the
                    person looking at the dashboard - and "Company" and
                    "Unternehmen" are one entity TYPE, "t" and "tonne" one
                    unit. A bucket has a KIND (KIND_INFO below) and names the
                    values of that kind which are searched as one;
                    app/scope.py expands a term through it on every view.

    colour groups   Connection type names are free text and there are tens of
                    thousands of them. They are grouped into a dozen
                    categories with one colour each, and the assignment is a
                    table (app/colours.py reads it, never a regex). That
                    grouping is ALSO what a search expands a connection type
                    through, which is why connection_type is not a bucket
                    kind: one thing grouped in two places would sooner or
                    later be grouped two different ways.

The contract the two pages are written against:

    GET    /api/buckets/kinds                    -> {kinds:[…], connection_types}
    GET    /api/buckets?kind                     -> {items:[{…bucket…, kind, members}]}
    POST   /api/buckets                          {name, kind?, every_project?, members?} -> 201 {id}
    PUT    /api/buckets/{id}                     {name?, every_project?} -> {id, kind, saved}
                                                 the KIND is not editable - see update_bucket
    DELETE /api/buckets/{id}                     -> {id, deleted, bucket}
    POST   /api/buckets/{id}/members             {name, type?} -> 201 {id, member}
                                                 `type` only means anything for kind=entity
    DELETE /api/buckets/{id}/members/{member_id} -> {deleted, member}
    GET    /api/buckets/resolve?id | ?q&kind     -> {bucket, kind, languages, members,
                                                    entities, sum_of_members}
    GET    /api/buckets/vocabulary?kind&q&bucket -> {items:[{value, label, hint, count,
                                                    in_bucket, already_here}]}
                                                 the suggest shape, so a typeahead reads it

    GET    /api/colour-groups?project            -> {groups:[…], fallback, assigned, unassigned}
                                                 counts are that project's when it is given
    POST   /api/colour-groups                    {name, colour, description?} -> 201 {id, key}
                                                 409 when that name is taken
    PUT    /api/colour-groups/{id}               {name?, colour?, description?, sort?}
    DELETE /api/colour-groups/{id}               -> {deleted, moved} - 409 for the fallback
    GET    /api/colour-groups/types?q&unassigned&page  the INVERTED view
    PUT    /api/colour-groups/types/{name}       {group: key|null} -> {type, group, previous}
    POST   /api/colour-groups/suggest            {types:[…]} -> what the rules WOULD say

THREE DECISIONS WORTH THE WORDS.

A bucket resolves BY NAME AND TYPE, in every language of the project at once,
and the answer is counted PER LANGUAGE. Entity ids are shared by the
translations of one task, so members spelled in English still reach the German
rows - the Buckets page shows both numbers because a bucket that matches four
records in English and none in German is broken in a way nobody would guess
from the member list alone.

A suggestion is never saved. `POST /colour-groups/suggest` answers what the
keyword rules would propose and writes nothing; the page fills the selects
with it and the person decides. The one exception is the first seed
(app/schema.py), where there is nobody to ask yet and the rows are marked
'suggested' so they can be told from a decision.

Deleting a colour group moves its types to the fallback rather than letting
the foreign key cascade them away. "This type is not a supplier" is a
decision, and it survives the group it happened to be filed under; the
fallback itself cannot be deleted at all (409), because then a type without an
assignment would have no colour.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import psycopg
from fastapi import APIRouter, Body, Depends, HTTPException, Query
from psycopg import sql
from pydantic import BaseModel, Field

from .. import colours as colour_module
from ..colours import contrast_ratio, get_resolver, is_valid_hex, suggest_group, text_colour_for
from ..context import ContextDep
from ..db import Database, get_db
from ..scope import (BUCKET_KINDS, COLOUR_GROUP_KIND, KIND_VALUES_SQL, bucket_terms,
                     find_bucket, member_index, member_match, normalise_terms)
from ..sqlbuild import like_substring

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["settings"])

MAX_NAME = 200
# A bucket with more members than this is a saved search, not a bucket, and
# the VALUES list it becomes is in every scoped query the dashboard runs.
MAX_MEMBERS = 200
TYPES_PAGE_SIZE = 50
MAX_TYPES_PAGE_SIZE = 200
# How many type names one Suggest press may ask about - the page never shows
# more than a page of them.
MAX_SUGGEST = 500


# ── Small shared pieces ──────────────────────────────────────────────────

def _text(value: Any, field: str, *, required: bool = True, limit: int = MAX_NAME) -> str:
    text = str(value or "").strip()
    if not text and required:
        raise HTTPException(400, {"error": f"{field} is empty",
                                  "hint": f"give the {field} a name of up to {limit} characters"})
    if len(text) > limit:
        raise HTTPException(400, {"error": f"{field} is longer than {limit} characters",
                                  "hint": "shorten it; this is a label, not a description"})
    return text


def _duplicate(exc: psycopg.errors.UniqueViolation, what: str, hint: str) -> HTTPException:
    log.debug("unique violation on %s: %s", what, exc)
    return HTTPException(409, {"error": what, "hint": hint})


def _row_or_404(row: Any, what: str) -> dict[str, Any]:
    if row is None:
        raise HTTPException(404, {"error": what,
                                  "hint": "it may have been deleted in another tab; reload the page"})
    return dict(row)


# ── The kinds a bucket can be ────────────────────────────────────────────
#
# One page with a kind selector rather than nine pages: the work is the
# same - collect the spellings of one thing - and nine pages would be nine
# places to look for it.
#
# `member` says what one member IS, because that is the thing people get
# wrong: for an entity it is a name AND a type, for every other kind it is a
# plain value out of that kind's vocabulary.
#
# `table`/`column` is where the vocabulary comes from and what a search
# matches, and `identity` is what "one thing" means when the matches are
# counted, so an entity that is named twice in a task is not counted twice.
KIND_INFO: dict[str, dict[str, Any]] = {
    "entity": {
        "label": "Entity", "plural": "Entities", "member": "an entity name and its type",
        "example": "Apple Inc. (Company) and Apple (Company) are one company",
        "suggest": "/api/suggest/entity", "typed": True,
        "table": "processed_data.entities", "column": "text_name",
        "identity": "(t.text_task_id, t.text_entity_id)", "counts": "entity records",
    },
    "entity_type": {
        "label": "Entity type", "plural": "Entity types", "member": "a type name",
        "example": "Company, Unternehmen and AG are one type",
        "suggest": "/api/suggest/entity_type", "typed": False,
        "table": "processed_data.entities", "column": "text_type",
        "identity": "(t.text_task_id, t.text_entity_id)", "counts": "entity records",
    },
    "source_type": {
        "label": "Document type", "plural": "Document types", "member": "a type name",
        "example": "Press release and Pressemitteilung are one kind of document",
        "suggest": "/api/suggest/source_type", "typed": False,
        "table": "processed_data.sources", "column": "text_type",
        "identity": "(t.text_task_id, t.text_source_id)", "counts": "documents",
    },
    "location": {
        "label": "Place", "plural": "Places", "member": "an address or a place",
        "example": "Rotterdam, Rotterdam Port and Schiedam are one area",
        "suggest": "/api/suggest/place", "typed": False,
        "table": "processed_data.locations", "column": "text_address",
        "identity": "(t.text_task_id, t.text_fk_entity_id)", "counts": "locations",
        # A place is the one kind whose members are matched as a substring:
        # "Rotterdam" has to reach "Rotterdam, Netherlands", exactly as it
        # does when it is typed into the Query page's place box.
        "substring": True,
    },
    "location_type": {
        "label": "Location type", "plural": "Location types", "member": "a type name",
        "example": "Headquarters, Hauptsitz and Head office are one type",
        "suggest": "/api/suggest/location_type", "typed": False,
        "table": "processed_data.locations", "column": "text_type",
        "identity": "(t.text_task_id, t.text_fk_entity_id)", "counts": "locations",
    },
    "event_type": {
        "label": "Event type", "plural": "Event types", "member": "a type name",
        "example": "Product launch and Produkteinführung are one type",
        "suggest": "/api/suggest/event_type", "typed": False,
        "table": "processed_data.events", "column": "text_type",
        "identity": "(t.text_task_id, t.bigint_id)", "counts": "events",
    },
    "attribute_type": {
        "label": "Attribute type", "plural": "Attribute types", "member": "a type name",
        "example": "Revenue, Umsatz and Turnover are one measurement",
        "suggest": "/api/suggest/attribute_type", "typed": False,
        "table": "processed_data.attributes", "column": "text_type",
        "identity": "(t.text_task_id, t.bigint_id)", "counts": "attributes",
    },
    "unit": {
        "label": "Unit", "plural": "Units", "member": "a unit",
        "example": "t, tonne and metric ton are one unit",
        "suggest": "/api/suggest/unit", "typed": False,
        "table": "processed_data.attributes", "column": "text_unit",
        "identity": "(t.text_task_id, t.bigint_id)", "counts": "attributes",
    },
    "market_topic": {
        "label": "Market topic", "plural": "Market topics", "member": "a topic",
        "example": "Semiconductors and Halbleiter are one topic",
        "suggest": "/api/suggest/market_topic", "typed": False,
        "table": "processed_data.market_insights", "column": "text_topic",
        "identity": "(t.text_task_id, t.text_fk_entity_id)", "counts": "readings",
    },
}

# Said on both settings pages, and answered by /api/buckets/kinds so the two
# cannot drift apart: connection types are grouped ONCE, by the Connection
# Colours page, and app/scope.py reads a colour group wherever it reads a
# bucket. A second grouping for the same thing in a different place is how a
# product starts contradicting itself.
CONNECTION_TYPE_NOTE = (
    "Connection types are not bucketed here. They are grouped on the Connection "
    "colours page - one colour per group - and a search treats a colour group "
    "exactly as it treats a bucket, so grouping them twice is not needed.")


# app/scope.py decides what a search does with each kind and this file decides
# what the page shows about it; a kind in one and not the other is a bucket
# that can be made and never applied, or applied and never made.
assert tuple(KIND_INFO) == BUCKET_KINDS, "KIND_INFO and scope.BUCKET_KINDS disagree"


def _kind_param(value: Any) -> str | None:
    """`?kind=` as a plain string, or None for "every kind".

    api_export.py calls list_buckets() as a FUNCTION rather than over HTTP,
    so a parameter it does not pass arrives as FastAPI's own Query default
    object instead of as "". Coerced here, once, so a direct caller does not
    have to know what the route's signature happens to look like.
    """
    text = value if isinstance(value, str) else ""
    return _kind(text) if text.strip() else None


def _kind(value: Any, *, default: str = "entity") -> str:
    kind = str(value or "").strip() or default
    if kind not in KIND_INFO:
        raise HTTPException(400, {
            "error": f"there is no bucket kind {kind!r}",
            "hint": "one of " + ", ".join(KIND_INFO)
                    + (f". {CONNECTION_TYPE_NOTE}" if kind == COLOUR_GROUP_KIND else "")})
    return kind


@router.get("/buckets/kinds")
def bucket_kinds():
    """What a bucket can be about, for the kind selector on the Buckets page."""
    return {
        "kinds": [{"kind": k, "label": v["label"], "plural": v["plural"],
                   "member": v["member"], "example": v["example"],
                   "suggest": v["suggest"], "typed": bool(v["typed"]),
                   "counts": v["counts"]}
                  for k, v in KIND_INFO.items()],
        "default": "entity",
        "connection_types": {"kind": COLOUR_GROUP_KIND, "note": CONNECTION_TYPE_NOTE,
                             "where": "/colours"},
    }


# ── Buckets ──────────────────────────────────────────────────────────────

BUCKETS_SQL = """
    SELECT b.bigint_id AS id, b.text_name AS name, b.text_project AS project,
           b.text_kind AS kind, b.date_added AS added,
           count(m.bigint_id) AS members
      FROM dashboard.buckets b
      LEFT JOIN dashboard.bucket_members m ON m.bigint_fk_bucket = b.bigint_id
     WHERE (b.text_project IS NULL OR b.text_project = %(project)s)
       AND (%(kind)s::text IS NULL OR b.text_kind = %(kind)s)
     GROUP BY b.bigint_id
     ORDER BY b.text_kind, lower(b.text_name), b.bigint_id
"""

MEMBERS_SQL = """
    SELECT m.bigint_id AS id, m.bigint_fk_bucket AS bucket,
           m.text_name AS name, m.text_type AS type
      FROM dashboard.bucket_members m
      JOIN dashboard.buckets b ON b.bigint_id = m.bigint_fk_bucket
     WHERE (b.text_project IS NULL OR b.text_project = %(project)s)
       AND (%(kind)s::text IS NULL OR b.text_kind = %(kind)s)
     ORDER BY lower(m.text_name), lower(coalesce(m.text_type, '')), m.bigint_id
"""


def _bucket_dict(row: dict[str, Any], members: list[dict[str, Any]]) -> dict[str, Any]:
    kind = row.get("kind") or "entity"
    return {
        "id": int(row["id"]),
        "name": row["name"],
        "project": row["project"],
        "kind": kind,
        "kind_label": KIND_INFO.get(kind, {}).get("label", kind),
        # NULL project = every project. Spelled out as a flag, because
        # `project: null` reads like "unknown" in a JSON body.
        "every_project": row["project"] is None,
        "members": members,
        "member_count": len(members),
        "added": row["added"].isoformat() if row.get("added") else None,
    }


class MemberIn(BaseModel):
    name: str
    # No type means "this name, whatever its type" - which is what puts
    # Apple the fruit in a bucket meant for the company, so the page asks
    # for one and the API allows leaving it out on purpose.
    type: str | None = None


class BucketIn(BaseModel):
    name: str
    kind: str = "entity"
    every_project: bool = False
    members: list[MemberIn] = Field(default_factory=list)


class BucketPatch(BaseModel):
    name: str | None = None
    every_project: bool | None = None


@router.get("/buckets")
def list_buckets(ctx: ContextDep, db: Database = Depends(get_db),
                 kind: str = Query("", description="one bucket kind, or empty for every kind")):
    """Every bucket that applies to this project, with its members.

    `?kind=` is what the kind selector on the page sends; without it every
    kind comes back, which is what an export or a check of "is this name
    taken" needs.

    Two statements rather than one per bucket: there are a handful of buckets
    and a few dozen members, and a page that fires one request per row
    takes a second to draw a list.
    """
    wanted = _kind_param(kind)
    params = {"project": ctx.project, "kind": wanted}
    with db.read() as conn:
        rows = conn.execute(BUCKETS_SQL, params).fetchall()
        members: dict[int, list[dict[str, Any]]] = {}
        for m in conn.execute(MEMBERS_SQL, params).fetchall():
            members.setdefault(int(m["bucket"]), []).append(
                {"id": int(m["id"]), "name": m["name"], "type": m["type"] or ""})
    return {"items": [_bucket_dict(dict(r), members.get(int(r["id"]), [])) for r in rows],
            "kind": wanted or "", "project": ctx.project, "language": ctx.language}


def _member_type(kind: str, value: Any) -> str | None:
    """The type half of a member, which only the entity kind has.

    Every other kind groups plain values - a type NAME, a unit, a topic - and
    a second field there would be a field with nothing to put in it. Anything
    sent is dropped rather than refused: a client that carries the entity
    shape everywhere is not wrong about the value it means."""
    if kind != "entity":
        return None
    return _text(value, "member type", required=False) or None


def _insert_members(conn: psycopg.Connection, bucket_id: int, members: list[MemberIn],
                    kind: str = "entity") -> int:
    written = 0
    for member in members:
        name = _text(member.name, "member name")
        typ = _member_type(kind, member.type)
        row = conn.execute(
            "INSERT INTO dashboard.bucket_members (bigint_fk_bucket, text_name, text_type) "
            "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING RETURNING bigint_id",
            (bucket_id, name, typ)).fetchone()
        if row:
            written += 1
    return written


@router.post("/buckets", status_code=201)
def create_bucket(ctx: ContextDep, body: BucketIn, db: Database = Depends(get_db)):
    """A new bucket, with its members when there are any.

    Members in the same request is what makes "undo" of a deleted bucket one
    call instead of one plus one per member - and an undo that half fails is
    worse than none.
    """
    name = _text(body.name, "bucket name")
    kind = _kind(body.kind)
    if len(body.members) > MAX_MEMBERS:
        raise HTTPException(400, {"error": f"a bucket takes at most {MAX_MEMBERS} members",
                                  "hint": "split it into two buckets, or search for the "
                                          "term itself instead"})
    project = None if body.every_project else ctx.project
    try:
        with db.write() as conn:
            row = conn.execute(
                "INSERT INTO dashboard.buckets (text_name, text_project, text_kind) "
                "VALUES (%s, %s, %s) RETURNING bigint_id", (name, project, kind)).fetchone()
            bucket_id = int(row["bigint_id"])
            written = _insert_members(conn, bucket_id, body.members, kind)
    except psycopg.errors.UniqueViolation as exc:
        # The uniqueness is per KIND, so the sentence has to name the kind:
        # an entity bucket called "Company" and an entity-type bucket called
        # "Company" are two different things and both may exist.
        label = KIND_INFO[kind]["label"].lower()
        raise _duplicate(exc, f"there is already a {label} bucket called {name!r}",
                         "rename the existing one, or add the members to it instead")
    return {"id": bucket_id, "name": name, "kind": kind,
            "every_project": project is None, "members": written}


@router.put("/buckets/{bucket_id}")
def update_bucket(bucket_id: int, ctx: ContextDep, body: BucketPatch,
                  db: Database = Depends(get_db)):
    with db.write() as conn:
        row = _row_or_404(conn.execute(
            "SELECT bigint_id, text_name, text_project, text_kind FROM dashboard.buckets "
            "WHERE bigint_id = %s", (bucket_id,)).fetchone(), f"there is no bucket {bucket_id}")
        # THE KIND IS NOT EDITABLE. Its members are keyed to it - an entity
        # member is a name and a type, a unit member is a unit - so changing
        # it would leave a bucket full of values that mean nothing in the new
        # kind, and silently. Make the other bucket and move the members.
        kind = row["text_kind"] or "entity"
        name = _text(body.name, "bucket name") if body.name is not None else row["text_name"]
        if body.every_project is None:
            project = row["text_project"]
        else:
            project = None if body.every_project else ctx.project
        try:
            conn.execute("UPDATE dashboard.buckets SET text_name = %s, text_project = %s "
                         "WHERE bigint_id = %s", (name, project, bucket_id))
        except psycopg.errors.UniqueViolation as exc:
            label = KIND_INFO.get(kind, {}).get("label", kind).lower()
            raise _duplicate(exc, f"there is already a {label} bucket called {name!r}",
                             "pick another name for this one")
    return {"id": bucket_id, "name": name, "kind": kind,
            "every_project": project is None, "saved": True}


@router.delete("/buckets/{bucket_id}")
def delete_bucket(bucket_id: int, db: Database = Depends(get_db)):
    """Delete a bucket and answer with what it held.

    The answer is the undo: the page keeps it for five seconds and posts it
    back unchanged if the person says so. Nothing is kept server-side - a
    "deleted" flag on a row is a second kind of bucket nobody can see.
    """
    with db.write() as conn:
        row = _row_or_404(conn.execute(
            "SELECT bigint_id, text_name, text_project, text_kind FROM dashboard.buckets "
            "WHERE bigint_id = %s", (bucket_id,)).fetchone(), f"there is no bucket {bucket_id}")
        members = [{"name": m["text_name"], "type": m["text_type"] or ""} for m in conn.execute(
            "SELECT text_name, text_type FROM dashboard.bucket_members "
            "WHERE bigint_fk_bucket = %s ORDER BY bigint_id", (bucket_id,)).fetchall()]
        conn.execute("DELETE FROM dashboard.buckets WHERE bigint_id = %s", (bucket_id,))
    return {"id": bucket_id, "deleted": True,
            "bucket": {"name": row["text_name"], "kind": row["text_kind"] or "entity",
                       "every_project": row["text_project"] is None, "members": members}}


@router.post("/buckets/{bucket_id}/members", status_code=201)
def add_member(bucket_id: int, body: MemberIn, db: Database = Depends(get_db)):
    name = _text(body.name, "member name")
    with db.write() as conn:
        owner = _row_or_404(conn.execute(
            "SELECT bigint_id, text_kind FROM dashboard.buckets WHERE bigint_id = %s",
            (bucket_id,)).fetchone(), f"there is no bucket {bucket_id}")
        typ = _member_type(owner["text_kind"] or "entity", body.type)
        count = conn.execute("SELECT count(*) AS n FROM dashboard.bucket_members "
                             "WHERE bigint_fk_bucket = %s", (bucket_id,)).fetchone()["n"]
        if count >= MAX_MEMBERS:
            raise HTTPException(400, {"error": f"this bucket already has {MAX_MEMBERS} members",
                                      "hint": "remove one before adding another, or split the bucket"})
        row = conn.execute(
            "INSERT INTO dashboard.bucket_members (bigint_fk_bucket, text_name, text_type) "
            "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING RETURNING bigint_id",
            (bucket_id, name, typ)).fetchone()
    if row is None:
        # The unique index is case-insensitive, so this is "already there in
        # some spelling" - which is the answer, not an error to fix.
        raise HTTPException(409, {"error": f"{name} is already in this bucket",
                                  "hint": "the same name and type can only be in a bucket once"})
    return {"id": int(row["bigint_id"]), "bucket": bucket_id,
            "member": {"id": int(row["bigint_id"]), "name": name, "type": typ or ""}}


@router.delete("/buckets/{bucket_id}/members/{member_id}")
def delete_member(bucket_id: int, member_id: int, db: Database = Depends(get_db)):
    with db.write() as conn:
        row = _row_or_404(conn.execute(
            "SELECT text_name, text_type FROM dashboard.bucket_members "
            "WHERE bigint_id = %s AND bigint_fk_bucket = %s", (member_id, bucket_id)).fetchone(),
            f"there is no member {member_id} in bucket {bucket_id}")
        conn.execute("DELETE FROM dashboard.bucket_members WHERE bigint_id = %s", (member_id,))
    return {"id": member_id, "bucket": bucket_id, "deleted": True,
            "member": {"name": row["text_name"], "type": row["text_type"] or ""}}


# ── What a bucket actually matches ───────────────────────────────────────
#
# The same expansion app/scope.py builds for every scoped view, run for one
# bucket and counted. Two numbers come out of it:
#
#   per language   how many entity records a search for this bucket reaches
#                  in that language. Ids are shared by the translations of a
#                  task, so English members reach German rows - and where
#                  they do not, this is the only place it shows.
#   per member     what that one member contributes. A member that matches
#                  nothing is a typo, and a member without a type usually
#                  matches more than its author expected.

# GROUPING SETS gives the per-language rows AND the total in one pass: the
# total is not the sum of the languages (an entity record is one id pair in
# every language it exists in) and it is not their maximum either, so it has
# to be counted over the whole set rather than derived from the rows.
_PER_LANGUAGE = """
    SELECT e.text_language AS language,
           count(DISTINCT (e.text_task_id, e.text_entity_id)) AS entities,
           count(*) AS records
      FROM processed_data.entities e
     WHERE e.text_project = %(project)s
       AND (e.text_task_id, e.text_entity_id) IN (SELECT task_id, id FROM ent)
     GROUP BY GROUPING SETS ((e.text_language), ())
"""


def _ent_cte(terms: sql.Composable) -> sql.Composable:
    return sql.SQL(
        "WITH ent AS (SELECT DISTINCT e.text_task_id AS task_id, e.text_entity_id AS id "
        "FROM processed_data.entities e JOIN {bt} ON {m} "
        "WHERE e.text_project = %(project)s AND e.text_entity_id IS NOT NULL "
        "AND e.text_entity_id <> '') ").format(bt=terms, m=member_match("e", "bt"))


def _matches(conn: psycopg.Connection, project: str, language: str,
             members: list[tuple[str, str | None]]) -> dict[str, Any]:
    if not normalise_terms(members):
        return {"entities": 0, "languages": []}
    terms, params = bucket_terms(members)
    stmt = _ent_cte(terms) + sql.SQL(_PER_LANGUAGE)
    rows = [dict(r) for r in conn.execute(stmt, {**params, "project": project}).fetchall()]
    return _counts(rows, language)


def _counts(rows: list[dict[str, Any]], language: str) -> dict[str, Any]:
    total = next((r for r in rows if r["language"] is None), None)
    languages = [{"language": r["language"], "entities": int(r["entities"]),
                  "records": int(r["records"])} for r in rows if r["language"] is not None]
    # The language on screen first: it is the one the numbers below the
    # bucket are about, and the others are the cross-check.
    languages.sort(key=lambda row: (row["language"] != language, row["language"]))
    return {"entities": int(total["entities"]) if total else 0, "languages": languages}


# THE SAME COUNT FOR EVERY OTHER KIND, AND WHY IT IS PER LANGUAGE.
#
# A type bucket earns its keep in a bilingual archive: "Company" reaches the
# English rows and nothing else, "Unternehmen" the German rows and nothing
# else, and only the two together cover the project. The per-language
# breakdown is what shows that on the page - a bucket that matches in one
# language and not the other is either half-finished or exactly right, and
# the reader is the one who can tell which.
#
# `entities` counts the kind's own identity (an entity, a document, a
# location, an event) so a thing named twice in one task is not counted
# twice; `records` counts the rows, which is where the languages differ.
_PER_LANGUAGE_VALUES = """
    SELECT t.text_language AS language,
           count(DISTINCT {identity}) AS entities,
           count(*) AS records
      FROM {table} t
     WHERE t.text_project = %(project)s AND {match}
     GROUP BY GROUPING SETS ((t.text_language), ())
"""


def _values_match(kind: str, column: str) -> sql.Composable:
    """Exact on a value, substring on a place. "Rotterdam" has to reach
    "Rotterdam, Netherlands" here for the same reason it does in a search."""
    if KIND_INFO[kind].get("substring"):
        return sql.SQL("EXISTS (SELECT 1 FROM unnest(%(vals)s::text[]) AS v "
                       "WHERE position(v IN lower(t.{c})) > 0)").format(c=sql.Identifier(column))
    return sql.SQL("lower(t.{c}) = ANY(%(vals)s)").format(c=sql.Identifier(column))


def _matches_values(conn: psycopg.Connection, project: str, language: str, kind: str,
                    values: list[str]) -> dict[str, Any]:
    wanted = sorted({(v or "").strip().lower() for v in values if (v or "").strip()})
    if not wanted:
        return {"entities": 0, "languages": []}
    info = KIND_INFO[kind]
    stmt = sql.SQL(_PER_LANGUAGE_VALUES).format(
        table=sql.SQL(info["table"]), identity=sql.SQL(info["identity"]),
        match=_values_match(kind, info["column"]))
    rows = [dict(r) for r in conn.execute(stmt, {"project": project, "vals": wanted}).fetchall()]
    return _counts(rows, language)


def _matches_kind(conn: psycopg.Connection, project: str, language: str, kind: str,
                  members: list[tuple[str, str | None]]) -> dict[str, Any]:
    """What a bucket of any kind matches. The entity kind keeps its own
    statement, because a member there is a name AND a type."""
    if kind == "entity":
        return _matches(conn, project, language, members)
    return _matches_values(conn, project, language, kind, [name for name, _ in members])


@router.get("/buckets/resolve")
def resolve_bucket(ctx: ContextDep, db: Database = Depends(get_db),
                   id: int | None = Query(None, description="the bucket to resolve"),
                   q: str = Query("", description="a term - the bucket it names or belongs to"),
                   kind: str = Query("entity", description="which kind ?q= is a term of")):
    """What a bucket matches, per language and per member.

    `?id=` is what the Buckets page asks for each of its rows; `?q=` is the
    same question a search box asks - it goes through app/scope.find_bucket,
    so it answers with the bucket a term would actually be expanded through,
    and `?kind=` says which kind of term it is (an entity by default, which
    is what every caller written before buckets had kinds means).
    """
    with db.read() as conn:
        if id is not None:
            row = _row_or_404(conn.execute(
                "SELECT bigint_id, text_name, text_project, text_kind FROM dashboard.buckets "
                "WHERE bigint_id = %s AND (text_project IS NULL OR text_project = %s)",
                (id, ctx.project)).fetchone(),
                f"there is no bucket {id} in this project")
            members = [{"name": m["text_name"], "type": m["text_type"] or ""} for m in conn.execute(
                "SELECT text_name, text_type FROM dashboard.bucket_members "
                "WHERE bigint_fk_bucket = %s ORDER BY lower(text_name)", (id,)).fetchall()]
            bucket = {"id": int(row["bigint_id"]), "name": row["text_name"],
                      "project": row["text_project"], "kind": row["text_kind"] or "entity",
                      "members": members}
        else:
            term = (q or "").strip()
            wanted = _kind(kind)
            if not term:
                raise HTTPException(400, {"error": "nothing to resolve",
                                          "hint": "ask with ?id=<bucket> or ?q=<term>"})
            found = find_bucket(conn, ctx.project, term, kind=wanted)
            if not found:
                return {"bucket": None, "q": term, "kind": wanted, "entities": 0,
                        "languages": [], "members": []}
            bucket = {**found, "kind": wanted}

        of_kind = bucket.get("kind") or "entity"
        pairs = [(m["name"], m["type"] or None) for m in bucket["members"]]
        totals = _matches_kind(conn, ctx.project, ctx.language, of_kind, pairs)
        # One member at a time, so a member that matches nothing is visible.
        # A bucket holds a handful of members; this is a handful of small
        # queries against an indexed name, not a scan per row.
        #
        # The per-member numbers deliberately do NOT add up to the total: two
        # members can name the same thing ("Company" and "Unternehmen" are one
        # entity in two languages), and the total counts it once. That is the
        # whole promise of a bucket, so the page says it rather than hiding a
        # subtraction.
        per_member = []
        for name, typ in pairs:
            one = _matches_kind(conn, ctx.project, ctx.language, of_kind, [(name, typ)])
            per_member.append({"name": name, "type": typ or "", "entities": one["entities"],
                               "languages": one["languages"]})

    info = KIND_INFO.get(of_kind, KIND_INFO["entity"])
    return {"bucket": {k: v for k, v in bucket.items() if k != "members"},
            "q": q, "kind": of_kind, "counts": info["counts"],
            "sum_of_members": sum(m["entities"] for m in per_member),
            "members": per_member, **totals}


# ── The values one kind offers ───────────────────────────────────────────
#
# The member field suggests from THAT KIND'S vocabulary, so the list is short
# and exact - and a value that is already in another bucket of the same kind
# is not offered, because putting it in a second bucket would make one term
# resolve to two different groups and only one of them could win.
#
# The statements come from app/scope.KIND_VALUES_SQL, which is also what a
# type search matches against: a field that offered values the search cannot
# find would be a field that lies about what it can do.

VOCABULARY_LIMIT = 200


@router.get("/buckets/vocabulary")
def bucket_vocabulary(ctx: ContextDep, db: Database = Depends(get_db),
                      kind: str = Query("entity_type", description="which vocabulary"),
                      q: str = Query("", description="part of a value"),
                      bucket: int | None = Query(None, description="the bucket being edited"),
                      limit: int = Query(30, ge=1, le=VOCABULARY_LIMIT)):
    """The values of one kind, with the ones that are already spoken for
    marked - and the bucket that holds them named, so a person who wanted
    that value knows where it went instead of being told "no".

    `?bucket=` is the bucket being edited: its own members are left OUT of
    the list. They are already on the page, in the member list right above
    the field, so offering them again spends a row and adds nothing.
    """
    wanted = _kind(kind)
    term = (q or "").strip().lower()
    stmt = KIND_VALUES_SQL[wanted]
    params: dict[str, Any] = {"project": ctx.project}
    with db.read() as conn:
        rows = [dict(r) for r in conn.execute(
            sql.SQL("SELECT value, sum(n) AS n FROM ({body}) AS vocab "
                    "WHERE (%(pat)s::text IS NULL OR lower(value) LIKE %(pat)s) "
                    "GROUP BY value ORDER BY sum(n) DESC, lower(value) LIMIT %(limit)s"
                    ).format(body=sql.SQL(stmt)),
            {**params, "pat": like_substring(term) if term else None,
             "limit": limit}).fetchall()]
        taken = member_index(conn, ctx.project, wanted)
        mine = set()
        if bucket is not None:
            mine = {(m["text_name"] or "").strip().lower() for m in conn.execute(
                "SELECT text_name FROM dashboard.bucket_members WHERE bigint_fk_bucket = %s",
                (bucket,)).fetchall()}
    # The shape /api/suggest answers with - value, label, hint, count - so a
    # typeahead can read this endpoint directly and the page has one list,
    # not one to suggest from and another to check against.
    items = []
    for row in rows:
        value = row["value"]
        low = (value or "").strip().lower()
        here = low in mine
        holder = None if here else taken.get(low)
        if here:
            hint = "already in this bucket"
        elif holder:
            hint = f"in the bucket “{holder}”"
        else:
            hint = ""
        items.append({"value": value, "label": value, "hint": hint,
                      "count": int(row["n"] or 0),
                      "in_bucket": holder, "already_here": here})
    # WHAT IS ALREADY IN THIS BUCKET IS NOT OFFERED TO IT AGAIN.
    #
    # Not listed as "already in this bucket", on the reasoning that saying
    # so beats silence. It does not: the member is on the screen already,
    # three centimetres above the field, so the list would be repeating
    # what the page has just said and spending a row of twelve on it.
    #
    # A value held by ANOTHER bucket is a different case and stays: somebody
    # looking for "Unternehmen" would otherwise find nothing and conclude
    # the archive has none, when the truth is that another bucket has it.
    # That row names the bucket and the page refuses the press.
    items = [i for i in items if not i["already_here"]]
    return {"kind": wanted, "q": q, "limit": limit, "items": items,
            "offered": sum(1 for i in items if not i["in_bucket"]),
            "taken": sum(1 for i in items if i["in_bucket"])}


# ── Colour groups ────────────────────────────────────────────────────────

_GROUP_COLUMNS = """
    SELECT g.bigint_id AS id, g.text_key AS key, g.text_name AS name, g.text_colour AS colour,
           g.text_description AS description, g.integer_sort AS sort, g.bool_fallback AS fallback,
"""

# Every assignment there is, whatever project the type occurs in. This is the
# honest number when nobody said which project is meant.
GROUPS_SQL = _GROUP_COLUMNS + """
           count(t.text_type_name) AS types, count(t.text_type_name) AS types_all
      FROM dashboard.colour_groups g
      LEFT JOIN dashboard.colour_group_types t ON t.bigint_fk_group = g.bigint_id
     GROUP BY g.bigint_id
     ORDER BY g.integer_sort, g.text_name
"""

# THE SAME UNIVERSE AS THE TYPE LIST, WHEN A PROJECT IS NAMED.
#
# `colour_group_types` is keyed by the type NAME alone, across every project
# and language of the archive - which is right, because "Lieferant" is the
# same word wherever it turns up. But the Colours page shows the two counts
# side by side: the groups on the left and, on the right, the types THIS
# project has. Counting every assignment on the left and this project's
# remainder on the right produced arithmetic nobody could follow - on a
# project with one type the page read "12 of 12 types assigned" beside a
# table with a single row, and a group promising two types that were not in
# the list. So when `project` is given, a group counts the types of that
# project, exactly as `_UNASSIGNED_SQL` counts the rest of them; `types_all`
# keeps the archive-wide number for the sentences that need it (deleting a
# group moves every type it holds, not only this project's).
#
# The statement itself is `_GROUPS_IN_PROJECT_SQL`, built further down next to
# `_NAMES_CTE` - the definition of "a type of this project" is written once
# there and the two counts share it, so they cannot drift apart again.


def _group_dict(row: dict[str, Any]) -> dict[str, Any]:
    colour = row["colour"]
    return {"id": int(row["id"]), "key": row["key"], "name": row["name"], "colour": colour,
            "text_colour": text_colour_for(colour), "description": row["description"] or "",
            "sort": int(row["sort"] or 0), "fallback": bool(row["fallback"]),
            # `types` is what the page counts and compares; `types_all` is
            # the archive-wide number, equal to it unless a project narrowed
            # the question. The delete question uses the second one, because
            # deleting a group moves every type it holds.
            "types": int(row.get("types") or 0),
            "types_all": int(row.get("types_all") if row.get("types_all") is not None
                             else (row.get("types") or 0)),
            # Rounded to one decimal: it is shown as "2.4:1", and the
            # threshold it is compared against is 3.
            "contrast": round(contrast_ratio(colour, "#ffffff"), 1)}


def _colour(value: Any) -> str:
    colour = str(value or "").strip()
    if colour and not colour.startswith("#"):
        colour = "#" + colour
    if not is_valid_hex(colour):
        raise HTTPException(400, {"error": f"{value!r} is not a colour",
                                  "hint": "six hexadecimal digits, as the colour field "
                                          "sends them: #1d4f91"})
    return colour.lower()


def _slug(name: str, taken: set[str]) -> str:
    """A key for a new group: the name, lower-cased, letters and digits only.

    The key is what JSON, the legend and the seed file refer to a group by,
    so it has to survive a rename - which is why it is derived once here and
    never again."""
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    if not base or not base[0].isalpha():
        base = "group-" + base if base else "group"
    key = base[:60]
    n = 2
    while key in taken:
        key = f"{base[:56]}-{n}"
        n += 1
    return key


class GroupIn(BaseModel):
    name: str
    colour: str = "#607d8b"
    description: str = ""
    sort: int | None = None


class GroupPatch(BaseModel):
    name: str | None = None
    colour: str | None = None
    description: str | None = None
    sort: int | None = None


def _warning(colour: str) -> str:
    ratio = contrast_ratio(colour, "#ffffff")
    if ratio >= 3.0:
        return ""
    return (f"This colour has a contrast of {ratio:.1f}:1 against the white page, "
            "below the 3:1 that WCAG asks of a line or a swatch. It is saved, but "
            "a thin edge in it will be hard to see.")


@router.get("/colour-groups")
def list_groups(db: Database = Depends(get_db),
                project: str = Query("", description="count this project's unassigned types too")):
    """The groups, with how many types each of them holds.

    `project` narrows the COUNTING, never the groups: every group is always
    listed, but with it each `types` count and `assigned` cover the types that
    occur in that project - the same universe `/colour-groups/types` lists -
    and `unassigned` is the rest of them. Without it the two columns of the
    Colours page counted different things and the fraction between them was
    nonsense: a project with one connection type read "12 of 12 types
    assigned" next to a table with one row. `types_all` and `assigned_all`
    keep the archive-wide numbers for the sentences that need them.

    `unassigned` also answers a question the group rows cannot: the fallback
    group's own count is - correctly - zero, because nothing is filed under it
    and everything merely lands there. Without the second number the fallback
    card reads "0 types" beside eight rows that say "Falls back to Other".
    """
    name = project.strip()
    with db.read() as conn:
        if name:
            rows = [dict(r) for r in conn.execute(_GROUPS_IN_PROJECT_SQL,
                                                  {"project": name}).fetchall()]
            unassigned = int(conn.execute(_UNASSIGNED_SQL, {"project": name}).fetchone()["n"] or 0)
        else:
            rows = [dict(r) for r in conn.execute(GROUPS_SQL).fetchall()]
            unassigned = None
    groups = [_group_dict(r) for r in rows]
    fallback = next((g["key"] for g in groups if g["fallback"]), "")
    return {"groups": groups, "fallback": fallback,
            # `assigned` + `unassigned` is exactly the number of types the
            # type list answers with for the same project. The page prints
            # the pair as a fraction, so the two halves have to be counted
            # over the same rows or the fraction is a lie.
            "assigned": sum(g["types"] for g in groups),
            "assigned_all": sum(g["types_all"] for g in groups),
            "unassigned": unassigned, "project": name}


@router.post("/colour-groups", status_code=201)
def create_group(body: GroupIn, db: Database = Depends(get_db)):
    name = _text(body.name, "group name")
    colour = _colour(body.colour)
    with db.write() as conn:
        # TWO GROUPS WITH ONE NAME ARE TWO THINGS NOBODY CAN TELL APART.
        #
        # The key is unique and `_slug` would happily mint "competitor-2", so
        # the insert would succeed - and every Group select on the page would
        # then offer two options reading "Competitor" with different values,
        # the legend two identical rows, and an edge a colour nobody could
        # trace back. The key stays the identity; the NAME is what people work with,
        # so it is checked here, case-insensitively and trimmed, and the page
        # is told which existing group to open instead.
        same = conn.execute("SELECT text_name FROM dashboard.colour_groups "
                            "WHERE lower(text_name) = lower(%s)", (name,)).fetchone()
        if same is not None:
            raise HTTPException(409, {
                "error": f"there is already a group called {same['text_name']}",
                "hint": "open it in the list below to change its colour or what belongs "
                        "in it, or give the new group a name of its own"})
        taken = {r["text_key"] for r in
                 conn.execute("SELECT text_key FROM dashboard.colour_groups").fetchall()}
        key = _slug(name, taken)
        if body.sort is None:
            # After the last group but before the fallback, which sits at the
            # bottom of every list and stays there.
            top = conn.execute("SELECT coalesce(max(integer_sort), 0) AS s "
                               "FROM dashboard.colour_groups WHERE NOT bool_fallback").fetchone()["s"]
            sort = int(top) + 10
        else:
            sort = int(body.sort)
        row = conn.execute(
            "INSERT INTO dashboard.colour_groups "
            "(text_key, text_name, text_colour, text_description, integer_sort) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING bigint_id",
            (key, name, colour, _text(body.description, "description", required=False, limit=500),
             sort)).fetchone()
    get_resolver().invalidate()
    return {"id": int(row["bigint_id"]), "key": key, "name": name, "colour": colour,
            "contrast": round(contrast_ratio(colour, "#ffffff"), 1),
            "warning": _warning(colour)}


@router.put("/colour-groups/{group_id}")
def update_group(group_id: int, body: GroupPatch, db: Database = Depends(get_db)):
    with db.write() as conn:
        row = _row_or_404(conn.execute(
            "SELECT bigint_id, text_key, text_name, text_colour, text_description, integer_sort "
            "FROM dashboard.colour_groups WHERE bigint_id = %s", (group_id,)).fetchone(),
            f"there is no colour group {group_id}")
        name = _text(body.name, "group name") if body.name is not None else row["text_name"]
        if name.lower() != str(row["text_name"]).lower():
            clash = conn.execute("SELECT text_name FROM dashboard.colour_groups "
                                 "WHERE lower(text_name) = lower(%s) AND bigint_id <> %s",
                                 (name, group_id)).fetchone()
            if clash is not None:
                raise HTTPException(409, {
                    "error": f"there is already a group called {clash['text_name']}",
                    "hint": "two groups with one name cannot be told apart in the list or "
                            "in the legend; pick another name"})
        colour = _colour(body.colour) if body.colour is not None else row["text_colour"]
        description = (_text(body.description, "description", required=False, limit=500)
                       if body.description is not None else row["text_description"])
        sort = int(body.sort) if body.sort is not None else int(row["integer_sort"])
        conn.execute("UPDATE dashboard.colour_groups SET text_name = %s, text_colour = %s, "
                     "text_description = %s, integer_sort = %s WHERE bigint_id = %s",
                     (name, colour, description, sort, group_id))
    # The resolver caches for a minute; without this the map and the graph
    # would keep drawing the old colour for up to that long after a change.
    get_resolver().invalidate()
    return {"id": group_id, "key": row["text_key"], "name": name, "colour": colour,
            "contrast": round(contrast_ratio(colour, "#ffffff"), 1),
            "saved": True, "warning": _warning(colour)}


@router.delete("/colour-groups/{group_id}")
def delete_group(group_id: int, db: Database = Depends(get_db)):
    with db.write() as conn:
        row = _row_or_404(conn.execute(
            "SELECT bigint_id, text_key, text_name, bool_fallback FROM dashboard.colour_groups "
            "WHERE bigint_id = %s", (group_id,)).fetchone(), f"there is no colour group {group_id}")
        if row["bool_fallback"]:
            raise HTTPException(409, {
                "error": f"{row['text_name']} is the group every unassigned type falls back to, "
                         "so it cannot be deleted",
                "hint": "rename it or give it another colour instead - without it a type "
                        "nobody has assigned would have no colour at all"})
        fallback = conn.execute(
            "SELECT bigint_id, text_name FROM dashboard.colour_groups WHERE bool_fallback").fetchone()
        if fallback is None:
            raise HTTPException(409, {
                "error": "this archive has no fallback colour group, so the types of this "
                         "group have nowhere to go",
                "hint": "run dashboard/sql/02-dashboard-seed.sql, which creates the "
                        "'Other' group, and try again"})
        moved = conn.execute(
            "UPDATE dashboard.colour_group_types SET bigint_fk_group = %s, date_updated = now() "
            "WHERE bigint_fk_group = %s", (fallback["bigint_id"], group_id)).rowcount
        conn.execute("DELETE FROM dashboard.colour_groups WHERE bigint_id = %s", (group_id,))
    get_resolver().invalidate()
    return {"id": group_id, "deleted": True, "moved": int(moved),
            "moved_to": fallback["text_name"]}


# ── The inverted view: every type, with the group it is in ───────────────
#
# The Colours page reads from the TYPE side, not the group side: the question
# a person has on that page is "what happens to 'Lieferant'", and answering it
# from a group list means opening groups until one of them holds the type.
#
# Types come from the connections themselves AND from the vocabulary table
# (which the collector fills as names first appear), in every language of the
# project - the colour table has one row per name, whatever language it is
# in, so "Supplier" and "Lieferant" are two rows that can be assigned in one
# pass. The language of the top bar is deliberately not bound here; the
# project is, or a customer with five projects would be scrolling through
# every type they have ever seen.

# The names half of the query, shared with the count below so the two can
# never disagree about what "a type of this project" is.
_NAMES_CTE = """
    WITH seen AS (
        SELECT c.text_type_parent_to_child AS name, count(*) AS n
          FROM processed_data.connections c
         WHERE c.text_project = %(project)s AND c.text_type_parent_to_child <> ''
         GROUP BY 1
        UNION ALL
        SELECT c.text_type_child_to_parent, count(*)
          FROM processed_data.connections c
         WHERE c.text_project = %(project)s AND c.text_type_child_to_parent <> ''
         GROUP BY 1
        UNION ALL
        SELECT ct.text_name, 0
          FROM processed_data.connection_types ct
         WHERE ct.text_project = %(project)s AND ct.text_name <> ''
    ),
    names AS (SELECT name, sum(n) AS connections FROM seen GROUP BY name)
"""

_TYPES_SQL = _NAMES_CTE + """,
    joined AS (
        SELECT n.name, n.connections, g.bigint_id AS group_id, g.text_key AS group_key,
               g.text_name AS group_name, g.text_colour AS colour, t.text_source AS source
          FROM names n
          LEFT JOIN dashboard.colour_group_types t ON t.text_type_name = n.name
          LEFT JOIN dashboard.colour_groups g ON g.bigint_id = t.bigint_fk_group
    )
    SELECT *, count(*) OVER () AS total
      FROM joined
     WHERE (%(pat)s::text IS NULL OR lower(name) LIKE %(pat)s)
       AND (NOT %(unassigned)s OR group_id IS NULL)
     ORDER BY connections DESC, lower(name)
     LIMIT %(limit)s OFFSET %(offset)s
"""

# How many of this project's types have no group of their own. The fallback
# group's card shows it: `count(colour_group_types)` is zero for the fallback
# in a fresh archive, and a card reading "0 types" next to eight rows that
# say "Falls back to Other" is a card nobody believes.
_UNASSIGNED_SQL = _NAMES_CTE + """
    SELECT count(*) AS n
      FROM names nm
     WHERE NOT EXISTS (SELECT 1 FROM dashboard.colour_group_types t
                        WHERE t.text_type_name = nm.name)
"""


# The left column of the Colours page, counted over the same names as the
# right column. `LEFT JOIN names` rather than a WHERE: a group with none of
# this project's types still has to be listed, at zero.
_GROUPS_IN_PROJECT_SQL = _NAMES_CTE + """
    SELECT g.bigint_id AS id, g.text_key AS key, g.text_name AS name, g.text_colour AS colour,
           g.text_description AS description, g.integer_sort AS sort, g.bool_fallback AS fallback,
           count(n.name) AS types, count(t.text_type_name) AS types_all
      FROM dashboard.colour_groups g
      LEFT JOIN dashboard.colour_group_types t ON t.bigint_fk_group = g.bigint_id
      LEFT JOIN names n ON n.name = t.text_type_name
     GROUP BY g.bigint_id
     ORDER BY g.integer_sort, g.text_name
"""


@router.get("/colour-groups/types")
def list_types(ctx: ContextDep, db: Database = Depends(get_db),
               q: str = Query("", description="part of a type name"),
               unassigned: bool = Query(False, description="only types without a group"),
               page: int = Query(0, ge=0),
               limit: int = Query(TYPES_PAGE_SIZE, ge=1, le=MAX_TYPES_PAGE_SIZE)):
    term = (q or "").strip().lower()
    params = {"project": ctx.project, "unassigned": unassigned, "limit": limit,
              "offset": page * limit, "pat": like_substring(term) if term else None}
    with db.read() as conn:
        rows = [dict(r) for r in conn.execute(_TYPES_SQL, params).fetchall()]
    fallback = get_resolver().fallback
    total = int(rows[0]["total"]) if rows else 0
    items = [{
        "type": r["name"],
        "connections": int(r["connections"] or 0),
        "group": r["group_key"],
        "group_name": r["group_name"] or fallback.name,
        "colour": r["colour"] or fallback.colour,
        "text_colour": text_colour_for(r["colour"] or fallback.colour),
        # 'manual' | 'suggested' from the table, '' when there is no row and
        # the type simply falls back.
        "source": r["source"] or "",
    } for r in rows]
    return {"items": items, "total": total, "page": page, "limit": limit,
            "pages": max(1, -(-total // limit)), "q": q, "unassigned": unassigned,
            "fallback": {"key": fallback.key, "name": fallback.name, "colour": fallback.colour}}


class TypeAssignment(BaseModel):
    # The group key, or null / "" to take the assignment away again - which
    # is not the same as assigning the fallback group: without a row the type
    # follows whatever the fallback is later changed to.
    group: str | None = None


@router.put("/colour-groups/types/{name:path}")
def assign_type(name: str, body: TypeAssignment = Body(default=TypeAssignment()),
                db: Database = Depends(get_db)):
    """Put one connection type in a group, or take it out again.

    `{name:path}` rather than a plain segment: type names are free text from
    the extraction and a slash in one ("Supplier/Vendor") would otherwise cut
    the path in two.

    The answer carries `previous`, which is what the page's undo posts back.
    """
    type_name = _text(name, "type name", limit=500)
    key = (body.group or "").strip()
    with db.write() as conn:
        before = conn.execute(
            "SELECT g.text_key AS key FROM dashboard.colour_group_types t "
            "JOIN dashboard.colour_groups g ON g.bigint_id = t.bigint_fk_group "
            "WHERE t.text_type_name = %s", (type_name,)).fetchone()
        previous = before["key"] if before else None
        if not key:
            conn.execute("DELETE FROM dashboard.colour_group_types WHERE text_type_name = %s",
                         (type_name,))
            group = None
        else:
            row = conn.execute("SELECT bigint_id, text_key, text_name, text_colour "
                               "FROM dashboard.colour_groups WHERE text_key = %s", (key,)).fetchone()
            if row is None:
                raise HTTPException(404, {"error": f"there is no colour group {key!r}",
                                          "hint": "reload the page - the group may have been "
                                                  "deleted in another tab"})
            conn.execute(
                "INSERT INTO dashboard.colour_group_types (text_type_name, bigint_fk_group, text_source) "
                "VALUES (%s, %s, 'manual') "
                "ON CONFLICT (text_type_name) DO UPDATE SET bigint_fk_group = EXCLUDED.bigint_fk_group, "
                "text_source = 'manual', date_updated = now()", (type_name, int(row["bigint_id"])))
            group = {"key": row["text_key"], "name": row["text_name"], "colour": row["text_colour"],
                     "text_colour": text_colour_for(row["text_colour"])}
    get_resolver().invalidate()
    return {"type": type_name, "group": group, "previous": previous, "saved": True}


class SuggestRequest(BaseModel):
    types: list[str] = Field(default_factory=list)


@router.post("/colour-groups/suggest")
def suggest_types(body: SuggestRequest):
    """What the keyword rules would propose for these type names.

    NOTHING IS WRITTEN. The page fills its selects with the answer and the
    person saves the ones they agree with - a guess that saved itself would be
    indistinguishable from a decision the next time somebody looked.
    """
    names = [n for n in (str(t or "").strip() for t in body.types) if n][:MAX_SUGGEST]
    resolver = get_resolver()
    groups = {g.key: g for g in resolver.groups()}
    fallback_key = resolver.fallback.key
    items = []
    for name in names:
        key = suggest_group(name)
        matched = key != colour_module.FALLBACK_KEY and key in groups
        group = groups.get(key)
        if group is None:
            continue
        items.append({"type": name, "group": group.key, "group_name": group.name,
                      "colour": group.colour, "text_colour": group.text_colour,
                      "matched": matched})
    proposed = [i for i in items if i["matched"]]
    return {"items": items, "proposed": len(proposed), "asked": len(names),
            "fallback": fallback_key}
