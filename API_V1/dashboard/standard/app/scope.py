"""What a search term means: the set of entities and sources a view is about.

A person types "Apple" into the Diagrams page. Which rows are "about Apple"?
The entities named Apple - in every language of the project, because the
German rows are a second complete set with the SAME entity ids and a
translated name - plus every member of a bucket called Apple, or of a bucket
that Apple is a member of. Then the sources those entities came from. Every
tab builder needs that same set, so it is resolved ONCE per request, here,
and handed to the builders as two CTEs they append to their own statement:

    ent (task_id, id)   the entities in scope
    src (task_id, id)   the sources in scope

Ids are per task: the extraction assigns "ent:apple-inc" inside one document,
and the next document may reuse the string for something else, so an entity
is identified by (text_task_id, text_entity_id) and never by the id alone.
Translations share the task id, which is what makes the German view line up.

The CTEs bind the project but deliberately NOT the language: names, addresses,
topics and event types are translated, and "Brüssel" typed into the English
view should still find the Commission. The data-table side of every statement
binds both (see sqlbuild.identity), so the rows that come back are always one
language's rows.

The resolve ladder - exact, then prefix, then substring - runs once, not once
per tab, so every tab of one page agrees on what was found and the response
can say so (`resolved`).

The summary is the sixth kind and the odd one out: with nothing typed it is
the whole project and both CTEs are dropped, and with a term its magnifier
SEARCHES EVERYTHING - the term is put to all five other kinds at once and the
scope is the union of what they answer (_resolve_everything). "Apple" there is
the bucket AND the two documents whose address says apple; on the Entity page
it would only ever be the bucket.

TWO AXES, AND HOW A CALLER ASKS FOR ONE
---------------------------------------

Every scope answers two questions, and `resolve_scope(conn, ctx, kind, q,
axis=...)` is where a caller says which:

    axis="object"  one entity, one document or its host, one address, one
                   market topic, one event by its own name. It is the
                   default for every scope but events, whose box has always
                   asked for a type and therefore opens on that one.
    axis="type"    the CLASS instead of the thing: "Company" is every
                   company, "Press release" every press release,
                   "Headquarters" every location of that type; on the market
                   scope it is the other closed vocabulary the table
                   carries, an outlook or a sentiment.

Both come back as the same ScopeSet with the same two CTEs, so nothing
downstream - no tab, no drilldown, no export - can tell them apart. Only the
RESOLUTION differs, and `resolved["axis"]` says which one produced the answer
so a view can print "Company - 42 entities" rather than "Apple Inc. - 1".

BUCKETS, FOR EVERY KIND
-----------------------

A bucket is several spellings of one thing - and not only of one ENTITY.
It is the same problem everywhere, and twice over in a bilingual archive: "Company"
and "Unternehmen" are two rows in entity_types, "t" and "tonne" two units.
So `dashboard.buckets` carries a kind (BUCKET_KINDS), and the rule holds
unchanged for all of them:

    A BUCKET WINS OVER A LITERAL MATCH. A term matching the bucket's NAME or
    any of its members resolves to the WHOLE bucket, and the answer is
    presented as ONE thing: `resolved["match"] == "bucket"`,
    `resolved["label"]` is the bucket's name and `resolved["bucket"]` is the
    grouping with its members, so every view can say so.

`resolve_terms(conn, project, kind, term)` is that rule on its own, for the
callers that are not a scope at all - the vocabulary dropdowns, the "by type"
charts, the Events type list, the Query place box. Connection types are the
one kind whose grouping is not a bucket: the Connection Colours page already
groups them, and resolve_terms reads a COLOUR GROUP there instead, so the
grouping is usable in a search without existing twice.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from psycopg import sql

from . import places
from .sqlbuild import (host_expression, like_prefix, like_substring, _escape_like,
                       readable_source_label)

KINDS = ("summary", "entity", "source", "location", "market", "events")

# THE SECOND AXIS.
#
# "object" is the search that has always been here: one entity, one document,
# one address, one topic, one event type. "type" is the other question a
# person asks of the same page - "show me every Company", "every Press
# release", "every Headquarters" - and it resolves to the same two CTEs, so
# every tab, every drilldown and every export is built from it unchanged.
#
# A caller asks for it as the fourth argument of resolve_scope:
#
#     resolve_scope(conn, ctx, "entity", "Company", axis="type")
#
# and reads scope.resolved["axis"], ["label"] and ["entities"] to say which
# axis produced the answer and how big it is.
AXES = ("object", "type")

# WHICH AXIS A SCOPE STARTS ON when the caller does not name one.
#
# "object" everywhere the search box means one thing - an entity, a
# document, an address, a topic. EVENTS is the exception, and it is not an
# inconsistency: that box searches the event TYPE ("Regulatory action")
# first, which is the question people ask of events. So the Events page
# opens on Type, and the object axis - one event, by its own name - is the
# second one.
DEFAULT_AXIS: dict[str, str] = {"events": "type"}


def default_axis(kind: str) -> str:
    """The axis `resolve_scope(...)` uses when the caller passes none."""
    return DEFAULT_AXIS.get(kind, "object")

# How many distinct matched names the response lists; the CTE is not capped.
MAX_NAMES = 10

# How many vocabulary values one term may expand to. A type search is meant
# to name a class, not to become a saved search over half the archive: a term
# that matches two hundred type names is a term nobody meant to type, and the
# list it expands to travels in every statement of the request.
MAX_VALUES = 200


class ScopeError(ValueError):
    pass


@dataclass
class ScopeSet:
    kind: str
    q: str
    # `ent AS (...)` and `src AS (...)`, ready for a WITH list. For the summary
    # scope both are empty sets, so a builder that references them still
    # runs, but the predicates below are TRUE and the sets are not consulted.
    ent_cte: sql.Composed
    src_cte: sql.Composed
    params: dict[str, Any] = field(default_factory=dict)
    # kind, label, match, entities (distinct ids), sources, names, bucket,
    # and on the summary `kinds`: what each kind made of the term.
    resolved: dict[str, Any] = field(default_factory=dict)
    # CTEs that have to be declared BEFORE ent, because ent reads them. Only
    # the summary's "everything" search has one (src_named, the sources whose
    # name or host is the term); every other scope reads ent and src alone.
    pre_ctes: list[sql.Composed] = field(default_factory=list)

    @property
    def scoped(self) -> bool:
        """Whether ent and src are a restriction at all.

        The summary is the view of everything - until somebody types into
        its search box, and then it is the view of everything that term
        names (_resolve_everything). An empty term there is the whole
        project again, which is what the predicates below being TRUE means.
        """
        return self.kind != "summary" or bool(self.q)

    def ctes(self) -> list[sql.Composable]:
        """The CTEs in dependency order: for the source scope the entities
        are derived from the sources, everywhere else the other way round.
        Anything in pre_ctes is read by both and comes first."""
        if self.kind == "source":
            return [*self.pre_ctes, self.src_cte, self.ent_cte]
        return [*self.pre_ctes, self.ent_cte, self.src_cte]

    def with_clause(self) -> sql.Composable:
        """`WITH ent AS (...), src AS (...) ` - nothing at all for summary."""
        if not self.scoped:
            return sql.SQL("")
        return sql.SQL("WITH ") + sql.SQL(", ").join(self.ctes()) + sql.SQL(" ")

    def ent_predicate(self, alias: str, column: str = "text_fk_entity_id") -> sql.Composable:
        """`(alias.text_task_id, alias.column) IN (SELECT task_id, id FROM ent)`.
        The entities table itself passes column="text_entity_id"."""
        if not self.scoped:
            return sql.SQL("TRUE")
        return sql.SQL("({a}.text_task_id, {a}.{c}) IN (SELECT task_id, id FROM ent)").format(
            a=sql.Identifier(alias), c=sql.Identifier(column))

    def src_predicate(self, alias: str, column: str = "text_fk_source_id") -> sql.Composable:
        """Same for sources; the sources table itself passes column="text_source_id"."""
        if not self.scoped:
            return sql.SQL("TRUE")
        return sql.SQL("({a}.text_task_id, {a}.{c}) IN (SELECT task_id, id FROM src)").format(
            a=sql.Identifier(alias), c=sql.Identifier(column))


# ── Pure pieces (unit-tested without a database) ─────────────

def normalise_terms(members: list[tuple[str, str | None]]) -> list[tuple[str, str | None]]:
    """Lower-cased, trimmed, de-duplicated (name, type) pairs; an empty type
    becomes None, meaning "this name, whatever its type"."""
    seen: set[tuple[str, str | None]] = set()
    out = []
    for name, typ in members:
        n = (name or "").strip().lower()
        t = (typ or "").strip().lower() or None
        if not n or (n, t) in seen:
            continue
        seen.add((n, t))
        out.append((n, t))
    return out


def bucket_terms(members: list[tuple[str, str | None]], prefix: str = "bt") -> tuple[sql.Composable, dict[str, Any]]:
    """A `(VALUES ...) AS bt(name, type)` list of lower-cased members, and its
    params. Types are cast so a NULL in the first row does not leave the
    column untyped.

    >>> frag, params = bucket_terms([("Apple Inc.", "Company"), ("Apple", None)])
    >>> sorted(params.items())
    [('bt_name_0', 'apple inc.'), ('bt_name_1', 'apple'), ('bt_type_0', 'company'), ('bt_type_1', None)]
    """
    terms = normalise_terms(members)
    if not terms:
        raise ScopeError("a bucket needs at least one member")
    params: dict[str, Any] = {}
    rows = []
    for i, (name, typ) in enumerate(terms):
        params[f"{prefix}_name_{i}"] = name
        params[f"{prefix}_type_{i}"] = typ
        rows.append(sql.SQL("(%({n})s::text, %({t})s::text)").format(
            n=sql.SQL(f"{prefix}_name_{i}"), t=sql.SQL(f"{prefix}_type_{i}")))
    frag = sql.SQL("(VALUES {rows}) AS {p}(name, type)").format(
        rows=sql.SQL(", ").join(rows), p=sql.Identifier(prefix))
    return frag, params


def member_match(alias: str = "e", terms: str = "bt") -> sql.Composable:
    """The join condition between an entities row and the VALUES list: same
    name, and the same type unless the member has none."""
    return sql.SQL("lower({a}.text_name) = {t}.name AND ({t}.type IS NULL OR lower({a}.text_type) = {t}.type)").format(
        a=sql.Identifier(alias), t=sql.Identifier(terms))


# ── Grouping: buckets of every kind, and the colour groups ───
#
# A bucket is several spellings of one thing, and not only of one ENTITY:
# an archive built from free text calls one
# type, one place, one unit several names, and in a bilingual archive it does
# so twice over ("Company" and "Unternehmen" are two rows in entity_types).
# So `dashboard.buckets` carries a kind, and these are the kinds.
#
# THE RULE, unchanged and now general: A BUCKET WINS OVER A LITERAL MATCH.
# When a term matches a bucket name OR any of its members, the search
# resolves to the whole bucket and is presented as ONE thing. `resolved`
# carries the bucket, so every view can say so.
BUCKET_KINDS = ("entity", "entity_type", "source_type", "location", "location_type",
                "event_type", "attribute_type", "unit", "market_topic")

# connection_type is deliberately NOT a bucket kind. dashboard.colour_groups
# already groups connection types - one colour per group, assigned in a table
# that the Connection Colours page edits - and a second grouping for the same
# thing in a different place is how a product starts contradicting itself. So
# a colour GROUP is read here wherever a bucket would be read, which makes it
# usable in a search without existing twice.
COLOUR_GROUP_KIND = "connection_type"

# Every kind a term can be grouped by, whichever table does the grouping.
GROUPING_KINDS = BUCKET_KINDS + (COLOUR_GROUP_KIND,)

# Which grouping kind a scope consults, per axis. None means "this axis has
# no grouping": a document name is not bucketed, an outlook is a closed
# vocabulary the extraction chooses from, and a single event has no group.
SCOPE_AXIS_KIND: dict[tuple[str, str], str | None] = {
    ("entity", "object"): "entity",
    ("entity", "type"): "entity_type",
    ("source", "object"): None,
    ("source", "type"): "source_type",
    ("location", "object"): "location",
    ("location", "type"): "location_type",
    # A MARKET INSIGHT IS ABOUT AN ENTITY AND CARRIES A TOPIC, and it has
    # no third thing that is a "type". An outlook or a sentiment is a VALUE
    # the reading has, not a class the reading belongs to, so a "type" axis
    # over those would offer a type that does not exist - and the page has
    # to be able to ask about the entity every other page asks about.
    ("market", "object"): "entity",
    ("market", "type"): "market_topic",
    # An event's own name is not bucketed: a bucket merges spellings of a
    # CLASS, and no two documents call one event two things.
    ("events", "object"): None,
    ("events", "type"): "event_type",
    ("summary", "object"): None,
    ("summary", "type"): None,
}

# WHERE THE VALUES OF A KIND LIVE - ONE DEFINITION, THREE READERS.
#
# The ladder below matches a typed term against these values; the vocabulary
# dropdowns are built from them with the members of a bucket taken out and
# the bucket put in their place; and the Buckets page suggests members from
# them (routers/api_settings.py reads this map rather than keeping a second
# copy). Three lists of "the entity types of this project" would sooner or
# later be three different lists.
#
# Each statement answers (value, n) for one project - the count orders the
# suggestions, and the ladder ignores it.
#
# The project is bound; the LANGUAGE deliberately is not, for the same reason
# the CTEs do not bind it - "Unternehmen" typed in the German view and
# "Company" typed in the English view are the same entities, and a type
# search that bound the language would answer the two questions differently.
KIND_VALUES_SQL: dict[str, str] = {
    "entity_type": ("SELECT e.text_type AS value, count(*) AS n FROM processed_data.entities e "
                    "WHERE e.text_project = %(project)s AND coalesce(e.text_type, '') <> '' "
                    "GROUP BY 1"),
    "source_type": ("SELECT s.text_type AS value, count(*) AS n FROM processed_data.sources s "
                    "WHERE s.text_project = %(project)s AND coalesce(s.text_type, '') <> '' "
                    "GROUP BY 1"),
    "location_type": ("SELECT l.text_type AS value, count(*) AS n FROM processed_data.locations l "
                      "WHERE l.text_project = %(project)s AND coalesce(l.text_type, '') <> '' "
                      "GROUP BY 1"),
    "event_type": ("SELECT ev.text_type AS value, count(*) AS n FROM processed_data.events ev "
                   "WHERE ev.text_project = %(project)s AND coalesce(ev.text_type, '') <> '' "
                   "GROUP BY 1"),
    "attribute_type": ("SELECT a.text_type AS value, count(*) AS n FROM processed_data.attributes a "
                       "WHERE a.text_project = %(project)s AND coalesce(a.text_type, '') <> '' "
                       "GROUP BY 1"),
    "unit": ("SELECT a.text_unit AS value, count(*) AS n FROM processed_data.attributes a "
             "WHERE a.text_project = %(project)s AND coalesce(a.text_unit, '') <> '' GROUP BY 1"),
    "market_topic": ("SELECT m.text_topic AS value, count(*) AS n "
                     "FROM processed_data.market_insights m "
                     "WHERE m.text_project = %(project)s AND coalesce(m.text_topic, '') <> '' "
                     "GROUP BY 1"),
    "location": ("SELECT l.text_address AS value, count(*) AS n FROM processed_data.locations l "
                 "WHERE l.text_project = %(project)s AND coalesce(l.text_address, '') <> '' "
                 "GROUP BY 1"),
    "entity": ("SELECT e.text_name AS value, count(*) AS n FROM processed_data.entities e "
               "WHERE e.text_project = %(project)s AND coalesce(e.text_name, '') <> '' "
               "GROUP BY 1"),
    # Not a bucket kind: outlook and sentiment are the closed vocabularies the
    # extraction chooses from, so there is nothing to merge - but the market
    # scope's type axis searches them, and the ladder needs their values.
    "market_value": (
        "SELECT value, count(*) AS n FROM ("
        "  SELECT m.text_short_term_outlook AS value FROM processed_data.market_insights m"
        "   WHERE m.text_project = %(project)s"
        "  UNION ALL SELECT m.text_long_term_outlook FROM processed_data.market_insights m"
        "   WHERE m.text_project = %(project)s"
        "  UNION ALL SELECT m.text_short_term_sentiment FROM processed_data.market_insights m"
        "   WHERE m.text_project = %(project)s"
        "  UNION ALL SELECT m.text_long_term_sentiment FROM processed_data.market_insights m"
        "   WHERE m.text_project = %(project)s"
        ") AS every_reading WHERE coalesce(value, '') <> '' GROUP BY 1"),
    COLOUR_GROUP_KIND: (
        "SELECT value, count(*) AS n FROM ("
        "  SELECT c.text_type_parent_to_child AS value FROM processed_data.connections c"
        "   WHERE c.text_project = %(project)s AND c.text_type_parent_to_child <> ''"
        "  UNION ALL"
        "  SELECT c.text_type_child_to_parent FROM processed_data.connections c"
        "   WHERE c.text_project = %(project)s AND c.text_type_child_to_parent <> ''"
        "  UNION ALL"
        "  SELECT ct.text_name FROM processed_data.connection_types ct"
        "   WHERE ct.text_project = %(project)s AND ct.text_name <> ''"
        ") AS every_connection_type GROUP BY 1"),
}

# The values of one kind that a term matches, on one rung of the ladder.
_KIND_LADDER_SQL = ("SELECT value FROM ({body}) AS vals WHERE lower(value) LIKE %(kv_pat)s "
                    "ORDER BY lower(value) LIMIT %(kv_lim)s")

# Every bucket of one kind that applies to a project, with its members. Read
# whole rather than one lookup per term: there are a handful of buckets per
# kind, and the vocabulary lists need all of them anyway.
BUCKETS_OF_KIND_SQL = """
    SELECT b.bigint_id AS id, b.text_name AS name, b.text_project AS project,
           m.text_name AS member, m.text_type AS member_type
      FROM dashboard.buckets b
      LEFT JOIN dashboard.bucket_members m ON m.bigint_fk_bucket = b.bigint_id
     WHERE b.text_kind = %(kind)s
       AND (b.text_project IS NULL OR b.text_project = %(project)s)
     ORDER BY (b.text_project IS NOT NULL) DESC, b.bigint_id,
              lower(m.text_name), lower(coalesce(m.text_type, ''))
"""

# The colour groups, read in the shape a bucket is read in, so one code path
# serves both. A group with no types is not a grouping of anything and is
# left out, exactly as a bucket with no members is.
COLOUR_GROUPS_AS_BUCKETS_SQL = """
    SELECT g.bigint_id AS id, g.text_name AS name, NULL::text AS project,
           t.text_type_name AS member, NULL::text AS member_type
      FROM dashboard.colour_groups g
      JOIN dashboard.colour_group_types t ON t.bigint_fk_group = g.bigint_id
     ORDER BY g.integer_sort, g.bigint_id, lower(t.text_type_name)
"""


@dataclass
class Grouping:
    """One bucket (or colour group) as a search reads it."""
    id: int
    name: str
    kind: str
    # "bucket" | "colour group" - what the resolved line calls it.
    source: str
    members: list[dict[str, str | None]] = field(default_factory=list)
    project: str | None = None

    @property
    def values(self) -> list[str]:
        return [str(m["name"]) for m in self.members if m.get("name")]

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "kind": self.kind, "source": self.source,
                "project": self.project,
                "members": [{"name": m.get("name"), "type": m.get("type") or ""}
                            for m in self.members]}


@dataclass
class TermSet:
    """What a term MEANS for one kind: the values a query should match.

    `values` is always what to search for - the members of the bucket that
    won, or the vocabulary values the ladder found, or (when the archive
    holds nothing of that name) the term itself. `grouping` is set exactly
    when a bucket or a colour group applied, and `label` is then its name,
    which is what the resolved line must show: a person may never be left
    wondering whether they are seeing one name or five merged.
    """
    kind: str
    q: str
    label: str
    values: list[str]
    match: str = "none"
    grouping: Grouping | None = None

    @property
    def lowered(self) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for value in self.values:
            low = (value or "").strip().lower()
            if low and low not in seen:
                seen.add(low)
                out.append(low)
        return out


def groupings(conn, project: str, kind: str) -> list[Grouping]:
    """Every bucket of this kind that applies to the project - or, for
    connection types, every colour group, which is the same idea kept in the
    table the Colours page already edits."""
    if kind == COLOUR_GROUP_KIND:
        rows = conn.execute(COLOUR_GROUPS_AS_BUCKETS_SQL).fetchall()
        source = "colour group"
    elif kind in BUCKET_KINDS:
        rows = conn.execute(BUCKETS_OF_KIND_SQL, {"kind": kind, "project": project}).fetchall()
        source = "bucket"
    else:
        return []
    by_id: dict[int, Grouping] = {}
    for row in rows:
        gid = int(row["id"])
        found = by_id.get(gid)
        if found is None:
            found = Grouping(gid, row["name"], kind, source, [], row["project"])
            by_id[gid] = found
        if row["member"]:
            found.members.append({"name": row["member"], "type": row["member_type"]})
    # A grouping with no members groups nothing and must not swallow a term.
    return [g for g in by_id.values() if g.members]


def grouping_for(conn, project: str, kind: str, term: str) -> Grouping | None:
    """The bucket (or colour group) a term belongs to, by NAME or by MEMBER.

    A grouping whose name matches beats one where only a member does, and a
    project's own bucket beats one that applies to every project - the order
    of BUCKETS_OF_KIND_SQL already puts those first, so ties keep it.
    """
    needle = (term or "").strip().lower()
    if not needle:
        return None
    by_member: Grouping | None = None
    for group in groupings(conn, project, kind):
        if (group.name or "").strip().lower() == needle:
            return group
        if by_member is None and any((m.get("name") or "").strip().lower() == needle
                                     for m in group.members):
            by_member = group
    return by_member


def member_index(conn, project: str, kind: str) -> dict[str, str]:
    """Every value that is inside a grouping of this kind, lower-cased, to
    the name of the grouping that holds it.

    A VOCABULARY LIST USES THIS TO TAKE ITS MEMBERS OUT. A dropdown that
    offers both "Company" and the bucket that holds it lets a person pick the
    member and silently defeat the merge - they get half the answer and no
    sign that a half is missing.
    """
    index: dict[str, str] = {}
    for group in groupings(conn, project, kind):
        for member in group.members:
            low = (member.get("name") or "").strip().lower()
            if low:
                index.setdefault(low, group.name)
    return index


def member_holders(conn, project: str, kind: str) -> dict[str, list[tuple[str | None, str]]]:
    """The same index as `member_index`, but keeping the member's TYPE.

    An entity bucket's members are a name AND a type, and that is the whole
    difference between the two things called Apple: the preseed's bucket
    holds Apple (Company) and Apple Inc. (Company), and Apple (Fruit) is
    deliberately outside it. A suggestion list that folded on the bare name
    would take the fruit out of the vocabulary along with the company -
    hiding the one entity in the archive the bucket does not stand for.

    So: lower-cased name -> [(lower-cased type or None, bucket name)], and
    `holder_for` below asks the question the way BUCKET_LOOKUP_SQL asks it -
    a member recorded WITHOUT a type means "this name, whatever its type".
    """
    index: dict[str, list[tuple[str | None, str]]] = {}
    for group in groupings(conn, project, kind):
        for member in group.members:
            low = (member.get("name") or "").strip().lower()
            if not low:
                continue
            typ = (member.get("type") or "").strip().lower() or None
            index.setdefault(low, []).append((typ, group.name))
    return index


def holders_for(holders: dict[str, list[tuple[str | None, str]]],
                name: str, typ: str | None = None) -> list[str]:
    """EVERY bucket that holds this name (of this type), in reading order.

    `holder_for` below answers with one, which is what a vocabulary list wants:
    a value appears under one heading or the list has it twice. This answers
    with all of them, which is what a MAP OF CONNECTIONS wants - an entity can
    honestly belong to several buckets ("Germany" and "EU"), and a connection to
    it is a connection to each of them. Folding that to the first bucket would
    silently drop the others, and nothing on the map would say a bucket had been
    left out.

    Duplicates are removed but the order is kept: a bucket that holds the same
    name twice, once with a type and once without, is one bucket.

    >>> holders = {"apple": [("company", "Apple"), (None, "Tech")]}
    >>> holders_for(holders, "Apple", "Company")
    ['Apple', 'Tech']
    >>> holders_for(holders, "Apple", "Fruit")
    ['Tech']
    >>> holders_for(holders, "Pear")
    []
    """
    entries = holders.get((name or "").strip().lower())
    if not entries:
        return []
    wanted = (typ or "").strip().lower() or None
    found: list[str] = []
    for member_type, bucket in entries:
        # A member with no type of its own matches any type, exactly as
        # BUCKET_LOOKUP_SQL reads it.
        if member_type is None or member_type == wanted:
            if bucket not in found:
                found.append(bucket)
    return found


def holder_for(holders: dict[str, list[tuple[str | None, str]]],
               name: str, typ: str | None = None) -> str | None:
    """The bucket that holds this name (of this type), or None.

    >>> holders = {"apple": [("company", "Apple")]}
    >>> holder_for(holders, "Apple", "Company")
    'Apple'
    >>> holder_for(holders, "Apple", "Fruit") is None
    True
    >>> holder_for(holders, "Apple") is None
    True
    """
    entries = holders.get((name or "").strip().lower())
    if not entries:
        return None
    wanted = (typ or "").strip().lower() or None
    for member_type, bucket in entries:
        # A member with no type of its own matches any type, exactly as
        # BUCKET_LOOKUP_SQL reads it.
        if member_type is None or member_type == wanted:
            return bucket
    return None


def _kind_ladder(conn, project: str, kind: str, q: str) -> tuple[str, list[str]]:
    """The first rung on which this kind's vocabulary answers, and the values
    it found there - ("", []) when the archive holds nothing like the term."""
    body = KIND_VALUES_SQL.get(kind)
    if body is None:
        return "", []
    stmt = _KIND_LADDER_SQL.format(body=body)
    for match, pattern in ladder_patterns(q):
        rows = conn.execute(stmt, {"project": project, "kv_pat": pattern,
                                   "kv_lim": MAX_VALUES}).fetchall()
        if rows:
            return match, [r["value"] for r in rows]
    return "", []


def resolve_terms(conn, project: str, kind: str, q: str) -> TermSet:
    """What a term means for one kind of value - THE one place the rule lives.

    Every view asks it the same question and gets the same answer, which is
    the point: Query, Events, Map, Heatmap, Graph, the vocabulary dropdowns
    and every "by type" chart have to agree about what "Company" is, or the
    same word means two things in one product.

        terms = resolve_terms(conn, ctx.project, "entity_type", "Unternehmen")
        terms.values     ['Company', 'Unternehmen']   what to match
        terms.label      'Company'                    what to SAY
        terms.match      'bucket'
        terms.grouping   the bucket, for the resolved line

    A bucket wins over a literal match; without one the values are what the
    archive's own vocabulary answers on the strongest rung of the ladder;
    with neither, the term itself, so a search still runs and comes back
    empty rather than silently searching everything.
    """
    term = (q or "").strip()
    if not term:
        return TermSet(kind, "", "", [], "none", None)
    group = grouping_for(conn, project, kind, term)
    if group is not None:
        return TermSet(kind, term, group.name, group.values, "bucket", group)
    match, values = _kind_ladder(conn, project, kind, term)
    if not match:
        return TermSet(kind, term, term, [term], "none", None)
    label = term if match == "exact" or len(values) > 1 else values[0]
    return TermSet(kind, term, label, values, match, None)


# ── A term that names ONE entity ─────────────────────────────
#
# The suggestion list offers four distinguishable rows for "Apple" - the
# bucket, Apple the Company, Apple Inc., Apple the Fruit - and until the
# type travelled with the choice all four sent the same q=Apple, all four
# came back as the bucket, and there was nowhere in the product to look at
# the fruit alone.
#
# The type therefore rides in the TERM, in the "Name (Type)" spelling the
# members of a bucket are already listed in. Not in a second parameter: q
# is the one thing every view, every export link, every drilldown and every
# shared URL carries, and a filter that only half of them know about is a
# CSV that does not hold what the screen holds.
#
# THE RULE: a bare name may be a bucket, a name with a type is one entity.
# That is also what makes "Show only Apple Inc." possible at all - a bare
# "Apple Inc." resolves straight back into the bucket, which is what a
# bucket is for.
#
# A real name that ends in brackets ("Zurich (2019)") is not lost: the type
# reading is put to the archive first, and when no entity of that name and
# type exists the whole string is searched exactly as it was typed.

_TYPED_TERM = re.compile(r"^(?P<name>\S.*?)\s*\((?P<type>[^()]+)\)$")

# The entity a "Name (Type)" term names, spelled as the archive spells it.
# The language is deliberately NOT bound, for the same reason the CTEs do
# not bind it: "Apfel (Frucht)" and "Apple (Fruit)" are the same ids, and
# the data tables put the rows back in the language that was asked for.
# Two entities may share a name AND a type; then the term means both of
# them, because the archive offers no other handle on the screen.
_ONE_ENTITY_SQL = """
    SELECT e.text_name AS name, e.text_type AS type,
           count(DISTINCT (e.text_task_id, e.text_entity_id)) AS n
      FROM processed_data.entities e
     WHERE e.text_project = %(project)s
       AND lower(e.text_name) = %(name)s AND lower(e.text_type) = %(type)s
       AND e.text_entity_id IS NOT NULL AND e.text_entity_id <> ''
     GROUP BY e.text_name, e.text_type
     ORDER BY n DESC, name
     LIMIT 1
"""

# The types one name carries in ONE language. Two rows mean the name alone
# does not identify an entity ("Apple" is a Company and a Fruit). The
# language has to be bound here, or a translated type would count as a
# second one and every name in a bilingual project would look ambiguous.
_NAME_TYPES_SQL = """
    SELECT DISTINCT e.text_type AS type
      FROM processed_data.entities e
     WHERE e.text_project = %(project)s AND e.text_language = %(language)s
       AND lower(e.text_name) = %(name)s
       AND e.text_type IS NOT NULL AND e.text_type <> ''
     LIMIT 3
"""


def split_entity_term(q: str) -> tuple[str, str | None]:
    """A term into (name, type); the type is None when none was given.

    >>> split_entity_term("Apple (Fruit)")
    ('Apple', 'Fruit')
    >>> split_entity_term("Apple")
    ('Apple', None)
    >>> split_entity_term("Zurich (2019) (Company)")
    ('Zurich (2019)', 'Company')
    >>> split_entity_term("(Fruit)")
    ('(Fruit)', None)
    """
    term = (q or "").strip()
    m = _TYPED_TERM.match(term)
    if not m:
        return term, None
    return m.group("name").strip(), m.group("type").strip() or None


def entity_term(name: str, typ: str | None) -> str | None:
    """The term that means this one entity, or None when there is none.

    Without a type there is no such term - a bare name may be a bucket -
    and a caller has to be told that rather than offer a control that
    cannot keep its word.

    >>> entity_term("Apple", "Fruit")
    'Apple (Fruit)'
    >>> entity_term("Apple", None) is None
    True
    """
    name = (name or "").strip()
    typ = (typ or "").strip()
    return f"{name} ({typ})" if name and typ else None


# The bucket a term belongs to: named like the term, or holding a member
# named like the term. Buckets with a NULL project apply to every project.
# A project's own bucket beats a global one of the same name; a bucket whose
# NAME matches beats one where only a member does. With a type given the
# question is "which bucket is this entity in", and only members count -
# Apple the fruit is not in a bucket because the bucket is called Apple.
BUCKET_LOOKUP_SQL = """
    SELECT b.bigint_id, b.text_name, b.text_project
      FROM dashboard.buckets b
     WHERE (b.text_project IS NULL OR b.text_project = %(project)s)
       AND b.text_kind = %(kind)s
       AND ((%(type)s::text IS NULL AND lower(b.text_name) = %(term)s)
            OR EXISTS (SELECT 1 FROM dashboard.bucket_members m
                        WHERE m.bigint_fk_bucket = b.bigint_id
                          AND lower(m.text_name) = %(term)s
                          AND (%(type)s::text IS NULL OR m.text_type IS NULL
                               OR lower(m.text_type) = %(type)s)))
     ORDER BY (lower(b.text_name) = %(term)s) DESC,
              (b.text_project IS NOT NULL) DESC,
              b.bigint_id
     LIMIT 1
"""

BUCKET_MEMBERS_SQL = """
    SELECT text_name, text_type FROM dashboard.bucket_members
     WHERE bigint_fk_bucket = %(bucket)s ORDER BY lower(text_name), lower(coalesce(text_type, ''))
"""


def ladder_patterns(q: str) -> list[tuple[str, str]]:
    """The three rungs, in order: (match kind, LIKE pattern) for the
    lower-cased term. An exact rung is a LIKE without wildcards, so the same
    predicate serves all three.

    >>> ladder_patterns("Ap_ple")
    [('exact', 'ap\\\\_ple'), ('prefix', 'ap\\\\_ple%'), ('substring', '%ap\\\\_ple%')]
    """
    term = q.strip().lower()
    return [("exact", _escape_like(term)), ("prefix", like_prefix(term)), ("substring", like_substring(term))]


# ── Building the CTEs ────────────────────────────────────────

def _cte(name: str, body: sql.Composable) -> sql.Composed:
    return sql.SQL("{n} AS ({b})").format(n=sql.Identifier(name), b=body)


_EMPTY = sql.SQL("SELECT NULL::text AS task_id, NULL::text AS id WHERE false")


def _summary(ctx) -> ScopeSet:
    """The summary with nothing typed: everything in the project."""
    return ScopeSet("summary", "", _cte("ent", _EMPTY), _cte("src", _EMPTY),
                    {"project": ctx.project, "language": ctx.language},
                    {"kind": "summary", "label": "Everything", "match": "all",
                     "entities": None, "sources": None, "names": [], "bucket": None,
                     "kinds": []})


# THE NAME OF A BODY'S LIKE PARAMETER.
#
# One scope has one ladder and therefore one pattern, and that pattern is
# %(sc_pat)s. The summary runs FIVE ladders in one request
# (_resolve_everything) and two of them can stop on different rungs -
# "Apple" is an exact bucket and a substring of two document URLs - so each
# kind is handed a parameter of its own and the bodies take its name.
def _pat(name: str = "sc_pat") -> sql.Composable:
    return sql.SQL("%({n})s").format(n=sql.SQL(name))


def _entity_body_from_terms(terms_frag: sql.Composable, alias: str = "bt") -> sql.Composable:
    return sql.SQL(
        "SELECT DISTINCT e.text_task_id AS task_id, e.text_entity_id AS id "
        "FROM processed_data.entities e JOIN {bt} ON {m} "
        "WHERE e.text_project = %(project)s AND e.text_entity_id IS NOT NULL AND e.text_entity_id <> ''"
    ).format(bt=terms_frag, m=member_match("e", alias))


def _entity_body_from_pattern(pat: str = "sc_pat") -> sql.Composable:
    return sql.SQL(
        "SELECT DISTINCT e.text_task_id AS task_id, e.text_entity_id AS id "
        "FROM processed_data.entities e "
        "WHERE e.text_project = %(project)s AND lower(e.text_name) LIKE {p} "
        "AND e.text_entity_id IS NOT NULL AND e.text_entity_id <> ''").format(p=_pat(pat))


def _source_body(pat: str = "sc_pat") -> sql.Composable:
    return sql.SQL(
        "SELECT DISTINCT s.text_task_id AS task_id, s.text_source_id AS id "
        "FROM processed_data.sources s "
        "WHERE s.text_project = %(project)s AND s.text_source_id IS NOT NULL AND s.text_source_id <> '' "
        "AND (lower(s.text_name) LIKE {p} OR lower({host}) LIKE {p} OR lower(s.text_uri) LIKE {p})"
    ).format(host=host_expression("s"), p=_pat(pat))


def _location_body(where: sql.Composable) -> sql.Composable:
    return sql.SQL(
        "SELECT DISTINCT l.text_task_id AS task_id, l.text_fk_entity_id AS id "
        "FROM processed_data.locations l "
        "WHERE l.text_project = %(project)s AND l.text_fk_entity_id IS NOT NULL AND l.text_fk_entity_id <> '' AND {w}"
    ).format(w=where)


def _market_ent_body(pat: str = "sc_pat") -> sql.Composable:
    return sql.SQL(
        "SELECT DISTINCT m.text_task_id AS task_id, m.text_fk_entity_id AS id "
        "FROM processed_data.market_insights m "
        "WHERE m.text_project = %(project)s AND lower(m.text_topic) LIKE {p} "
        "AND m.text_fk_entity_id IS NOT NULL AND m.text_fk_entity_id <> ''").format(p=_pat(pat))


def _market_src_body(pat: str = "sc_pat") -> sql.Composable:
    return sql.SQL(
        "SELECT DISTINCT m.text_task_id AS task_id, m.text_fk_source_id AS id "
        "FROM processed_data.market_insights m "
        "WHERE m.text_project = %(project)s AND lower(m.text_topic) LIKE {p} "
        "AND m.text_fk_source_id IS NOT NULL AND m.text_fk_source_id <> ''").format(p=_pat(pat))


# event_entities joins its event on (task, bigint id): the event has no id of
# its own in the extraction.
def _events_ent_body(pat: str = "sc_pat") -> sql.Composable:
    return sql.SQL(
        "SELECT DISTINCT ee.text_task_id AS task_id, ee.text_fk_entity_id AS id "
        "FROM processed_data.events ev "
        "JOIN processed_data.event_entities ee "
        "  ON ee.text_task_id = ev.text_task_id AND ee.bigint_fk_event_id = ev.bigint_id "
        " AND ee.text_project = ev.text_project AND ee.text_language = ev.text_language "
        "WHERE ev.text_project = %(project)s AND lower(ev.text_type) LIKE {p} "
        "AND ee.text_fk_entity_id IS NOT NULL AND ee.text_fk_entity_id <> ''").format(p=_pat(pat))


def _events_src_body(pat: str = "sc_pat") -> sql.Composable:
    return sql.SQL(
        "SELECT DISTINCT ev.text_task_id AS task_id, ev.text_fk_source_id AS id "
        "FROM processed_data.events ev "
        "WHERE ev.text_project = %(project)s AND lower(ev.text_type) LIKE {p} "
        "AND ev.text_fk_source_id IS NOT NULL AND ev.text_fk_source_id <> ''").format(p=_pat(pat))


# ONE EVENT, BY ITS OWN NAME - the events scope's object axis.
#
# The two bodies above match the event TYPE, which is what this scope has
# always searched and what its type axis still does. The object axis is the
# other question, and it is the one the Events page never had: "the Brussels
# hearing", not "every hearing". `events.text_name` is the extraction's own
# name for the event, so it is translated like every other name and the CTEs
# deliberately do not bind the language (see the module docstring).
def _event_name_ent_body(pat: str = "sc_pat") -> sql.Composable:
    return sql.SQL(
        "SELECT DISTINCT ee.text_task_id AS task_id, ee.text_fk_entity_id AS id "
        "FROM processed_data.events ev "
        "JOIN processed_data.event_entities ee "
        "  ON ee.text_task_id = ev.text_task_id AND ee.bigint_fk_event_id = ev.bigint_id "
        " AND ee.text_project = ev.text_project AND ee.text_language = ev.text_language "
        "WHERE ev.text_project = %(project)s AND lower(ev.text_name) LIKE {p} "
        "AND ee.text_fk_entity_id IS NOT NULL AND ee.text_fk_entity_id <> ''").format(p=_pat(pat))


def _event_name_src_body(pat: str = "sc_pat") -> sql.Composable:
    return sql.SQL(
        "SELECT DISTINCT ev.text_task_id AS task_id, ev.text_fk_source_id AS id "
        "FROM processed_data.events ev "
        "WHERE ev.text_project = %(project)s AND lower(ev.text_name) LIKE {p} "
        "AND ev.text_fk_source_id IS NOT NULL AND ev.text_fk_source_id <> ''").format(p=_pat(pat))


# ── The type axis ────────────────────────────────────────────
#
# A type search resolves to the same two CTEs as an object search - the
# entity set of everything of that type, and the documents it came from - so
# every tab, every drilldown and every export is built from it unchanged.
# Only the resolution differs, and it is a list of values rather than a LIKE
# pattern, because a bucket of five spellings is five exact values.
#
# `= ANY(%(sc_vals)s)` and not an IN list of parameters: the list is built
# from a bucket and can hold a couple of hundred entries, and one array
# parameter keeps the statement (and therefore the plan cache) one shape.

def _values(param: str = "sc_vals") -> sql.Composable:
    return sql.SQL("%({p})s").format(p=sql.SQL(param))


def _in_values(alias: str, column: str, param: str = "sc_vals") -> sql.Composable:
    return sql.SQL("lower({a}.{c}) = ANY({v})").format(
        a=sql.Identifier(alias), c=sql.Identifier(column), v=_values(param))


def _entity_type_body(param: str = "sc_vals") -> sql.Composable:
    return sql.SQL(
        "SELECT DISTINCT e.text_task_id AS task_id, e.text_entity_id AS id "
        "FROM processed_data.entities e "
        "WHERE e.text_project = %(project)s AND {t} "
        "AND e.text_entity_id IS NOT NULL AND e.text_entity_id <> ''"
    ).format(t=_in_values("e", "text_type", param))


def _source_type_body(param: str = "sc_vals") -> sql.Composable:
    return sql.SQL(
        "SELECT DISTINCT s.text_task_id AS task_id, s.text_source_id AS id "
        "FROM processed_data.sources s "
        "WHERE s.text_project = %(project)s AND {t} "
        "AND s.text_source_id IS NOT NULL AND s.text_source_id <> ''"
    ).format(t=_in_values("s", "text_type", param))


def _location_type_body(param: str = "sc_vals") -> sql.Composable:
    return sql.SQL(
        "SELECT DISTINCT l.text_task_id AS task_id, l.text_fk_entity_id AS id "
        "FROM processed_data.locations l "
        "WHERE l.text_project = %(project)s AND {t} "
        "AND l.text_fk_entity_id IS NOT NULL AND l.text_fk_entity_id <> ''"
    ).format(t=_in_values("l", "text_type", param))


def _event_type_ent_body(param: str = "sc_vals") -> sql.Composable:
    return sql.SQL(
        "SELECT DISTINCT ee.text_task_id AS task_id, ee.text_fk_entity_id AS id "
        "FROM processed_data.events ev "
        "JOIN processed_data.event_entities ee "
        "  ON ee.text_task_id = ev.text_task_id AND ee.bigint_fk_event_id = ev.bigint_id "
        " AND ee.text_project = ev.text_project AND ee.text_language = ev.text_language "
        "WHERE ev.text_project = %(project)s AND {t} "
        "AND ee.text_fk_entity_id IS NOT NULL AND ee.text_fk_entity_id <> ''"
    ).format(t=_in_values("ev", "text_type", param))


def _event_type_src_body(param: str = "sc_vals") -> sql.Composable:
    return sql.SQL(
        "SELECT DISTINCT ev.text_task_id AS task_id, ev.text_fk_source_id AS id "
        "FROM processed_data.events ev "
        "WHERE ev.text_project = %(project)s AND {t} "
        "AND ev.text_fk_source_id IS NOT NULL AND ev.text_fk_source_id <> ''"
    ).format(t=_in_values("ev", "text_type", param))


# The market's second axis is the TOPIC: "everything the archive read about
# semiconductors". It is the closest thing a market insight has to a class,
# and it is what the page's second switch is called - Topic, not Type.
#
# The pattern-matching pair above (_market_ent_body) is still the summary
# magnifier's way in, which searches a typed word against every kind at
# once; these two take the resolved VALUES, so a topic bucket that merges
# "Semiconductors" and "Halbleiter" arrives as both names.


def _market_topic_ent_body(param: str = "sc_vals") -> sql.Composable:
    return sql.SQL(
        "SELECT DISTINCT m.text_task_id AS task_id, m.text_fk_entity_id AS id "
        "FROM processed_data.market_insights m "
        "WHERE m.text_project = %(project)s AND {t} "
        "AND m.text_fk_entity_id IS NOT NULL AND m.text_fk_entity_id <> ''"
    ).format(t=_in_values("m", "text_topic", param))


def _market_topic_src_body(param: str = "sc_vals") -> sql.Composable:
    return sql.SQL(
        "SELECT DISTINCT m.text_task_id AS task_id, m.text_fk_source_id AS id "
        "FROM processed_data.market_insights m "
        "WHERE m.text_project = %(project)s AND {t} "
        "AND m.text_fk_source_id IS NOT NULL AND m.text_fk_source_id <> ''"
    ).format(t=_in_values("m", "text_topic", param))


def _sources_of_entities(entities: str = "ent") -> sql.Composable:
    return sql.SQL(
        "SELECT DISTINCT e.text_task_id AS task_id, e.text_fk_source_id AS id "
        "FROM processed_data.entities e "
        "WHERE e.text_project = %(project)s AND (e.text_task_id, e.text_entity_id) IN (SELECT task_id, id FROM {e}) "
        "AND e.text_fk_source_id IS NOT NULL AND e.text_fk_source_id <> ''").format(e=sql.Identifier(entities))


def _entities_of_sources(sources: str = "src") -> sql.Composable:
    return sql.SQL(
        "SELECT DISTINCT e.text_task_id AS task_id, e.text_entity_id AS id "
        "FROM processed_data.entities e "
        "WHERE e.text_project = %(project)s AND (e.text_task_id, e.text_fk_source_id) IN (SELECT task_id, id FROM {s}) "
        "AND e.text_entity_id IS NOT NULL AND e.text_entity_id <> ''").format(s=sql.Identifier(sources))


# The pair of CTEs one scoped kind needs, out of the bodies above.

def _entity_ctes_from_terms(terms_frag: sql.Composable) -> tuple[sql.Composed, sql.Composed]:
    return _cte("ent", _entity_body_from_terms(terms_frag)), _cte("src", _sources_of_entities())


def _entity_ctes_from_pattern() -> tuple[sql.Composed, sql.Composed]:
    return _cte("ent", _entity_body_from_pattern()), _cte("src", _sources_of_entities())


def _source_ctes() -> tuple[sql.Composed, sql.Composed]:
    return _cte("ent", _entities_of_sources()), _cte("src", _source_body())


def _location_ctes(where: sql.Composable) -> tuple[sql.Composed, sql.Composed]:
    return _cte("ent", _location_body(where)), _cte("src", _sources_of_entities())


def _market_ctes() -> tuple[sql.Composed, sql.Composed]:
    return _cte("ent", _market_ent_body()), _cte("src", _market_src_body())


def _events_ctes() -> tuple[sql.Composed, sql.Composed]:
    return _cte("ent", _events_ent_body()), _cte("src", _events_src_body())


def _event_name_ctes() -> tuple[sql.Composed, sql.Composed]:
    return _cte("ent", _event_name_ent_body()), _cte("src", _event_name_src_body())


# ── Resolution against the archive ───────────────────────────

# One probe per rung: the names that match and how many distinct ids they
# cover. The first rung with a row wins. `{p}` is filled in by _probe() with
# whichever parameter carries this kind's pattern.
_PROBES = {
    "entity": ("SELECT e.text_name AS name, count(DISTINCT (e.text_task_id, e.text_entity_id)) AS n "
               "FROM processed_data.entities e WHERE e.text_project = %(project)s "
               "AND lower(e.text_name) LIKE {p} GROUP BY e.text_name ORDER BY n DESC, name LIMIT %(lim)s"),
    # Grouped by what a person can read, not by text_name: this probe already
    # MATCHED on the host, and grouping by the name then answered with the
    # identifier - "matched 3cf1dadd3725f8d20c2158445c3bb4ca" for a search for
    # vogue, one group per document, every count 1. The names it returns are
    # the LABEL only (see _resolve_one: `names`/`values`); the rows themselves
    # come from _source_body and its pattern, so this changes what the page
    # says and not what it finds.
    "source": ("SELECT {label} AS name, count(DISTINCT (s.text_task_id, s.text_source_id)) AS n "
               "FROM processed_data.sources s WHERE s.text_project = %(project)s "
               "AND (lower(s.text_name) LIKE {p} OR lower(substring(s.text_uri from '^[a-zA-Z][a-zA-Z0-9+.-]*://([^/:?#]+)')) LIKE {p} "
               "OR lower(s.text_uri) LIKE {p}) GROUP BY 1 ORDER BY n DESC, name LIMIT %(lim)s"),
    "market": ("SELECT m.text_topic AS name, count(DISTINCT (m.text_task_id, m.text_fk_entity_id)) AS n "
               "FROM processed_data.market_insights m WHERE m.text_project = %(project)s "
               "AND lower(m.text_topic) LIKE {p} GROUP BY m.text_topic ORDER BY n DESC, name LIMIT %(lim)s"),
    "events": ("SELECT ev.text_type AS name, count(*) AS n "
               "FROM processed_data.events ev WHERE ev.text_project = %(project)s "
               "AND lower(ev.text_type) LIKE {p} GROUP BY ev.text_type ORDER BY n DESC, name LIMIT %(lim)s"),
    # The events scope's object axis: the event itself, by its own name.
    "event_name": ("SELECT ev.text_name AS name, count(*) AS n "
                   "FROM processed_data.events ev WHERE ev.text_project = %(project)s "
                   "AND lower(ev.text_name) LIKE {p} GROUP BY ev.text_name "
                   "ORDER BY n DESC, name LIMIT %(lim)s"),
}


def _probe(kind: str, pat: str = "sc_pat") -> sql.Composable:
    text = _PROBES[kind]
    if "{label}" in text:
        return sql.SQL(text).format(p=_pat(pat), label=readable_source_label("s"))
    return sql.SQL(text).format(p=_pat(pat))


def _ladder(conn, kind: str, base: dict[str, Any], q: str,
            pat: str = "sc_pat") -> tuple[str, list[str], str]:
    """The first rung on which this kind answers, the names it found there,
    and the pattern that found them - ("", [], "") when no rung answers."""
    probe = _probe(kind, pat)
    for match, pattern in ladder_patterns(q):
        rows = conn.execute(probe, {**base, pat: pattern, "lim": MAX_NAMES}).fetchall()
        if rows:
            return match, [r["name"] for r in rows], pattern
    return "", [], ""


def _location_rungs(q: str, prefix: str = "sc"):
    """A place token - "Springfield, Ontario, Canada" as the Query page
    disambiguated it, or a bare city/country - first as parts, then as a
    substring of the address. Returns the rungs and the parsed place."""
    place = places.split_address(q)
    rungs: list[tuple[str, sql.Composable, dict[str, Any]]] = []
    if place.parts:
        frag, pparams = places.place_predicate(place, alias="l", prefix=f"{prefix}_pl")
        rungs.append(("exact", frag, pparams))
    rungs.append(("substring",
                  sql.SQL("lower(l.text_address) LIKE {p}").format(p=_pat(f"{prefix}_pat")),
                  {f"{prefix}_pat": like_substring(q.lower())}))
    return rungs, place


def _count(conn, scope: "ScopeSet", which: str) -> int:
    stmt = sql.SQL("{w}SELECT count(*) AS n FROM {t}").format(w=scope.with_clause(), t=sql.Identifier(which))
    row = conn.execute(stmt, scope.params).fetchone()
    return int(row["n"] if isinstance(row, dict) else row[0])


def _counted(scope: "ScopeSet", conn) -> "ScopeSet":
    scope.resolved["entities"] = _count(conn, scope, "ent")
    scope.resolved["sources"] = _count(conn, scope, "src")
    return scope


def find_bucket(conn, project: str, name: str, typ: str | None = None,
                kind: str = "entity") -> dict[str, Any] | None:
    """The bucket a name (and optional type) belongs to, with its members,
    or None. Used here and by the graph, which expands through buckets.

    `kind` defaults to "entity", which is what every caller written before
    buckets had kinds means; the other kinds go through resolve_terms, which
    also reads a colour group where connection types are concerned."""
    term = (name or "").strip().lower()
    if not term:
        return None
    row = conn.execute(BUCKET_LOOKUP_SQL, {"project": project, "term": term, "kind": kind,
                                           "type": (typ or "").strip().lower() or None}).fetchone()
    if not row:
        return None
    members = [(m["text_name"], m["text_type"]) for m in
               conn.execute(BUCKET_MEMBERS_SQL, {"bucket": row["bigint_id"]}).fetchall()]
    if not members:
        return None
    return {"id": int(row["bigint_id"]), "name": row["text_name"], "project": row["text_project"],
            "members": [{"name": n, "type": t} for n, t in members]}


def find_one_entity(conn, project: str, name: str, typ: str) -> dict[str, Any] | None:
    """The entity a "Name (Type)" term names, or None when the archive has
    no such entity - and then the brackets belong to a name."""
    row = conn.execute(_ONE_ENTITY_SQL, {"project": project,
                                         "name": (name or "").strip().lower(),
                                         "type": (typ or "").strip().lower()}).fetchone()
    return {"name": row["name"], "type": row["type"]} if row else None


def _sole_type(conn, ctx, name: str) -> str | None:
    """The one type this name has in this project and language, or None
    when it has none or several."""
    rows = conn.execute(_NAME_TYPES_SQL, {"project": ctx.project, "language": ctx.language,
                                          "name": (name or "").strip().lower()}).fetchall()
    return rows[0]["type"] if len(rows) == 1 else None


def _member_of(conn, ctx, bucket: dict[str, Any], q: str) -> dict[str, Any] | None:
    """Which member of the bucket the typed term is, and the term that
    shows that member alone.

    Both halves are what makes the notice truthful. A page that answers
    "Apple Inc." with the whole bucket must not say `"Apple Inc." is a
    bucket: Apple, Apple Inc.` - no bucket is called Apple Inc. It says it
    from the bucket's own name and offers the single entity.

    A member recorded without a type means "this name, whatever its type",
    and there is no term for one of two types; such a member gets the
    sentence but no offer (q is None) unless the archive gives the name a
    single type.
    """
    term = (q or "").strip().lower()
    for m in bucket["members"]:
        if (m["name"] or "").strip().lower() != term:
            continue
        typ = (m["type"] or "").strip() or _sole_type(conn, ctx, m["name"])
        return {"name": m["name"], "type": typ or "", "q": entity_term(m["name"], typ)}
    return None


def resolve_scope(conn, ctx, kind: str, q: str | None, axis: str | None = None) -> ScopeSet:
    """The ScopeSet for a request - what a view is about.

        resolve_scope(conn, ctx, "entity", "Apple")                 one entity or a bucket
        resolve_scope(conn, ctx, "entity", "Company", axis="type")  every entity of a type
        resolve_scope(conn, ctx, "events", "Delivery", axis="type") every entity of an event type

    `conn` is a read connection, `ctx` carries project and language, `kind`
    is one of KINDS and `axis` one of AXES - or None, which takes that
    scope's own default (default_axis: "object" everywhere except events,
    whose box has always searched the type):

        "object"  one thing: an entity, a document or its host, an
                  address, a market topic, ONE EVENT by its own name.
                  Buckets apply (SCOPE_AXIS_KIND says which kind of bucket
                  each scope consults).
        "type"    the class rather than the thing: "Company" is every
                  company, "Press release" every press release,
                  "Headquarters" every location of that type, and on the
                  market scope the closed vocabularies the table carries -
                  an outlook or a sentiment. Type BUCKETS apply, so a
                  bilingual bucket of "Company" and "Unternehmen" answers
                  with one entity set in both languages.

    Unknown kinds and axes raise ScopeError (a 400 upstream); an empty term
    on a scoped kind resolves to nothing rather than to everything, so a page
    cannot accidentally chart the whole archive.

    What comes back is a ScopeSet whose `resolved` dict always carries
    `kind`, `axis`, `q`, `label`, `match`, `entities`, `sources`, `names`
    and `bucket` - `match` is "bucket" exactly when a bucket or a colour
    group applied, `bucket` is then that grouping with its members, and
    `label` is its name. Views must show that: a person may never be left
    wondering whether they are seeing one name or five merged.
    """
    if kind not in KINDS:
        raise ScopeError(f"unknown scope {kind!r}; one of {', '.join(KINDS)}")
    if axis is None:
        axis = default_axis(kind)
    if axis not in AXES:
        raise ScopeError(f"unknown axis {axis!r}; one of {', '.join(AXES)}")
    q = (q or "").strip()
    if kind == "summary":
        # The summary is everything, so it has no second axis: a type search
        # over "everything" is the summary again.
        if axis == "type":
            raise ScopeError("the summary scope has no type axis; it is already everything")
        # Nothing typed is the whole project; a term is "everything called
        # this", resolved across every kind at once (_resolve_everything).
        return _summary(ctx) if not q else _resolve_everything(conn, ctx, q)

    base = {"project": ctx.project, "language": ctx.language}
    # `type`, `member` and `in_bucket` are the entity scope's three, and they
    # are in every scoped answer so the shape does not depend on the kind:
    # the type the term named, which member of a bucket was typed, and which
    # bucket a single entity is in.
    resolved: dict[str, Any] = {"kind": kind, "axis": axis, "q": q, "label": q, "match": "none",
                                "entities": 0, "sources": 0, "names": [], "bucket": None,
                                "type": None, "member": None, "in_bucket": None,
                                "values": [], "grouping_kind": SCOPE_AXIS_KIND.get((kind, axis))}
    if not q:
        return ScopeSet(kind, q, _cte("ent", _EMPTY), _cte("src", _EMPTY), base, resolved)

    if axis == "type":
        return _resolve_type(conn, ctx, kind, q, base, resolved)

    # THE MARKET SCOPE READS ITS OBJECT AXIS THE WAY THE ENTITY SCOPE DOES,
    # because a market insight is about an entity: "what has the archive read
    # about Apple" is the question, and the topic is the other axis.
    if kind in ("entity", "market"):
        name, typ = split_entity_term(q)
        one = find_one_entity(conn, ctx.project, name, typ) if typ else None
        if one:
            # The bucket is NOT expanded - that is what the type in the term
            # means - but the answer says which bucket this entity is in, so
            # the notice can offer the way back to the whole of it.
            frag, oparams = bucket_terms([(one["name"], one["type"])])
            ent, src = _entity_ctes_from_terms(frag)
            resolved.update(label=entity_term(one["name"], one["type"]), match="exact",
                            type=one["type"], names=[one["name"]],
                            in_bucket=find_bucket(conn, ctx.project, one["name"], one["type"]))
            return _counted(ScopeSet(kind, q, ent, src, {**base, **oparams}, resolved), conn)

        bucket = find_bucket(conn, ctx.project, q)
        if bucket:
            frag, bparams = bucket_terms([(m["name"], m["type"]) for m in bucket["members"]])
            ent, src = _entity_ctes_from_terms(frag)
            params = {**base, **bparams}
            resolved.update(label=bucket["name"], match="bucket", bucket=bucket,
                            names=[m["name"] for m in bucket["members"]],
                            values=[m["name"] for m in bucket["members"]],
                            member=_member_of(conn, ctx, bucket, q))
            return _counted(ScopeSet(kind, q, ent, src, params, resolved), conn)

    if kind == "location":
        return _resolve_location(conn, ctx, q, base, resolved)

    builders = {"entity": _entity_ctes_from_pattern, "source": _source_ctes,
                "market": _entity_ctes_from_pattern, "events": _event_name_ctes}
    # The events scope's object axis is the EVENT, not its type: same ladder,
    # a different column (see _event_name_ent_body). Its type axis, which is
    # what that page opens on, goes through _resolve_type above.
    probe = _probe({"events": "event_name", "market": "entity"}.get(kind, kind))
    for match, pattern in ladder_patterns(q):
        params = {**base, "sc_pat": pattern}
        rows = conn.execute(probe, {**params, "lim": MAX_NAMES}).fetchall()
        if not rows:
            continue
        ent, src = builders[kind]()
        names = [r["name"] for r in rows]
        resolved.update(match=match, names=names, values=names,
                        label=q if match == "exact" or len(names) > 1 else names[0])
        return _counted(ScopeSet(kind, q, ent, src, params, resolved), conn)

    params = {**base, "sc_pat": ladder_patterns(q)[0][1]}
    ent, src = builders[kind]()
    return ScopeSet(kind, q, ent, src, params, resolved)


# ── The type axis, resolved ──────────────────────────────────
#
# One shape for every scope: put the term to that scope's TYPE vocabulary
# (a bucket first, the archive's own values after), then build the entity
# set from the values that came back. The counts follow the object axis's
# path exactly, so a caller cannot tell the two apart downstream - which is
# the point: every tab, every drilldown, every export is built once.

_TYPE_BODIES = {
    "entity": (_entity_type_body, None),
    "source": (None, _source_type_body),
    "location": (_location_type_body, None),
    "events": (_event_type_ent_body, _event_type_src_body),
    "market": (_market_topic_ent_body, _market_topic_src_body),
}


def _resolve_type(conn, ctx, kind: str, q: str, base: dict[str, Any],
                  resolved: dict[str, Any]) -> ScopeSet:
    vocabulary = SCOPE_AXIS_KIND.get((kind, "type")) or ""
    if not vocabulary:
        raise ScopeError(f"the {kind} scope has no type axis")
    terms = resolve_terms(conn, ctx.project, vocabulary, q)
    resolved.update(label=terms.label, match=terms.match, names=terms.values,
                    values=terms.values,
                    bucket=terms.grouping.as_dict() if terms.grouping else None)
    ent_body, src_body = _TYPE_BODIES[kind]
    params = {**base, "sc_vals": terms.lowered}
    if kind == "source":
        # As on the object axis: the documents decide, the entities follow.
        scope = ScopeSet(kind, q, _cte("ent", _entities_of_sources()),
                         _cte("src", src_body()), params, resolved)
    elif src_body is None:
        scope = ScopeSet(kind, q, _cte("ent", ent_body()),
                         _cte("src", _sources_of_entities()), params, resolved)
    else:
        scope = ScopeSet(kind, q, _cte("ent", ent_body()), _cte("src", src_body()),
                         params, resolved)
    if terms.match == "none":
        # Nothing of that name is a type here. The scope stays and answers
        # nothing, so the page can say "no Widget has anything in this
        # period" rather than charting the whole archive.
        return scope
    return _counted(scope, conn)


# ── The summary, searched ────────────────────────────────────
#
# "Everything" is a scope like the others, and its magnifier searches
# EVERYTHING: the term is asked of all five kinds at once and the page is
# the union of what they answer. Typing "Apple" on the summary means what it
# means on the Entity page (the bucket), plus what it means on the Source
# page (two documents whose address says apple), plus a place, a topic or an
# event type of that name if the archive has one.
#
# Each kind climbs ITS OWN ladder - exact, prefix, substring - and stops on
# the rung where it answers, which is exactly what it would do on its own
# scope page; a shared ladder would silence a kind that only matches as a
# substring while another matched exactly, and the reader would have no way
# to see that half the answer was dropped. That is also why every kind is
# given its own LIKE parameter (_pat): two kinds can be holding two
# different patterns at the same time.
#
# An empty term is NOT this path - it is the plain summary, everything in
# the project (resolve_scope). A term nobody answers charts nothing, the
# same as on a scoped page, because eight charts of the whole archive under
# a search box holding a misspelling is the wrong answer told confidently.

_EVERYTHING_KINDS = ("entity", "source", "location", "market", "events")

# One parameter namespace per kind, so five ladders can be in flight at once.
_EVERYTHING_PREFIX = {"entity": "sc_ent", "source": "sc_src", "location": "sc_loc",
                      "market": "sc_mkt", "events": "sc_evt"}

# Strongest rung first: what the page reports as THE match of the term.
_RUNGS = ("bucket", "exact", "prefix", "substring")


def _union(bodies: list[sql.Composable]) -> sql.Composable:
    """UNION, not UNION ALL: an entity found as a name and again through its
    address is one entity, and the predicates downstream are IN (...) tests
    that would not care - but the counts in `resolved` would."""
    return sql.SQL(" UNION ").join(bodies)


def _resolve_everything(conn, ctx, q: str) -> ScopeSet:
    """The summary with a term: the union of what all five kinds answer.

    `resolved.kinds` lists one entry per kind that answered - kind, rung and
    the names it found - because a page that only said "matched Apple Inc.,
    Apple" would leave the reader unable to explain why the numbers are
    bigger than the bucket.
    """
    base = {"project": ctx.project, "language": ctx.language}
    params: dict[str, Any] = dict(base)
    hits: list[dict[str, Any]] = []
    ent_bodies: list[sql.Composable] = []
    src_bodies: list[sql.Composable] = []
    pre: list[sql.Composed] = []

    def hit(kind: str, match: str, names: list[str],
            grouping: dict[str, Any] | None = None) -> None:
        # `grouping` is the bucket (or colour group) that won, as a dict -
        # NOT just its name. The Summary's sentence has to be built from the
        # bucket's own name and never from what was typed: no bucket is
        # called "Apple Inc.", and a line saying one is sends the reader
        # looking for a bucket that does not exist (static/js/resolved.js
        # records the same bug, fixed once already on the other three views).
        hits.append({"kind": kind, "match": match, "names": names, "bucket": grouping})

    # Entities - one named entity first, then a bucket, exactly as the
    # Entity page resolves it. Without the first of the two a link copied
    # from that page ("Apple (Fruit)") would come back here as "nothing in
    # this project is called that".
    prefix = _EVERYTHING_PREFIX["entity"]
    name, typ = split_entity_term(q)
    one = find_one_entity(conn, ctx.project, name, typ) if typ else None
    bucket = None if one else find_bucket(conn, ctx.project, q)
    if one:
        frag, oparams = bucket_terms([(one["name"], one["type"])], prefix=f"{prefix}_bt")
        params.update(oparams)
        ent_bodies.append(_entity_body_from_terms(frag, alias=f"{prefix}_bt"))
        hit("entity", "exact", [entity_term(one["name"], one["type"])])
    elif bucket:
        frag, bparams = bucket_terms([(m["name"], m["type"]) for m in bucket["members"]],
                                     prefix=f"{prefix}_bt")
        params.update(bparams)
        ent_bodies.append(_entity_body_from_terms(frag, alias=f"{prefix}_bt"))
        hit("entity", "bucket", [m["name"] for m in bucket["members"]], bucket)
    else:
        match, names, pattern = _ladder(conn, "entity", base, q, f"{prefix}_pat")
        if match:
            params[f"{prefix}_pat"] = pattern
            ent_bodies.append(_entity_body_from_pattern(f"{prefix}_pat"))
            hit("entity", match, names)

    # Sources. The named documents are a CTE OF THEIR OWN because ent reads
    # them (the entities of those documents are in scope) and src reads them
    # too; deriving ent from src the way the source scope does is impossible
    # here, since src also holds the sources of everything else in ent.
    prefix = _EVERYTHING_PREFIX["source"]
    match, names, pattern = _ladder(conn, "source", base, q, f"{prefix}_pat")
    if match:
        params[f"{prefix}_pat"] = pattern
        pre.append(_cte("src_named", _source_body(f"{prefix}_pat")))
        ent_bodies.append(_entities_of_sources("src_named"))
        src_bodies.append(sql.SQL("SELECT task_id, id FROM src_named"))
        hit("source", match, names)

    # Places. A location bucket wins here as it does on the Query page: an
    # area a person has defined once is an area everywhere.
    prefix = _EVERYTHING_PREFIX["location"]
    area = grouping_for(conn, ctx.project, "location", q)
    place_hits: list[str] = []
    place_rung = ""
    for i, member in enumerate(area.values if area else [q]):
        rungs, place = _location_rungs(member, f"{prefix}_{i}")
        for match, where, extra in rungs:
            body = _location_body(where)
            found = conn.execute(sql.SQL("SELECT count(*) AS n FROM ({b}) AS hits").format(b=body),
                                 {**base, **extra}).fetchone()
            if int(found["n"] if isinstance(found, dict) else found[0]):
                params.update(extra)
                ent_bodies.append(body)
                place_hits.append(place.label or member)
                place_rung = place_rung or match
                break
    if place_hits:
        # ONE entry, whether the area is one address or five: a bucket is
        # presented as one thing, and a list of five places under "location"
        # would read as five separate answers to one term.
        hit("location", "bucket" if area else place_rung,
            [area.name] if area else place_hits,
            area.as_dict() if area else None)

    # Market topics and event types. A market_topic bucket merges
    # "Semiconductors" and "Halbleiter" here too - the summary is the union
    # of the other scopes, so it cannot answer one of them differently.
    prefix = _EVERYTHING_PREFIX["market"]
    topics = resolve_terms(conn, ctx.project, "market_topic", q)
    if topics.match == "bucket":
        params[f"{prefix}_vals"] = topics.lowered
        ent_bodies.append(sql.SQL(
            "SELECT DISTINCT m.text_task_id AS task_id, m.text_fk_entity_id AS id "
            "FROM processed_data.market_insights m WHERE m.text_project = %(project)s AND {t} "
            "AND m.text_fk_entity_id IS NOT NULL AND m.text_fk_entity_id <> ''"
        ).format(t=_in_values("m", "text_topic", f"{prefix}_vals")))
        src_bodies.append(sql.SQL(
            "SELECT DISTINCT m.text_task_id AS task_id, m.text_fk_source_id AS id "
            "FROM processed_data.market_insights m WHERE m.text_project = %(project)s AND {t} "
            "AND m.text_fk_source_id IS NOT NULL AND m.text_fk_source_id <> ''"
        ).format(t=_in_values("m", "text_topic", f"{prefix}_vals")))
        hit("market", "bucket", topics.values,
            topics.grouping.as_dict() if topics.grouping else None)
    else:
        match, names, pattern = _ladder(conn, "market", base, q, f"{prefix}_pat")
        if match:
            params[f"{prefix}_pat"] = pattern
            ent_bodies.append(_market_ent_body(f"{prefix}_pat"))
            src_bodies.append(_market_src_body(f"{prefix}_pat"))
            hit("market", match, names)

    prefix = _EVERYTHING_PREFIX["events"]
    match, names, pattern = _ladder(conn, "events", base, q, f"{prefix}_pat")
    if match:
        params[f"{prefix}_pat"] = pattern
        ent_bodies.append(_events_ent_body(f"{prefix}_pat"))
        src_bodies.append(_events_src_body(f"{prefix}_pat"))
        hit("events", match, names)

    # The flat list every other scope also carries, kind order kept.
    found_names: list[str] = []
    for entry in hits:
        for name in entry["names"]:
            if name and name not in found_names and len(found_names) < MAX_NAMES:
                found_names.append(name)
    resolved: dict[str, Any] = {
        "kind": "summary", "q": q, "label": q, "match": "none",
        "entities": 0, "sources": 0, "names": found_names,
        "bucket": bucket if any(h["match"] == "bucket" for h in hits) else None,
        # Which member of that bucket was typed, and the term that shows it
        # alone: the Summary offers the same way out of a bucket the Map,
        # the Graph and the Events page offer (static/js/resolved.js).
        "member": _member_of(conn, ctx, bucket, q) if bucket else None,
        "kinds": hits,
    }
    if not hits:
        return ScopeSet("summary", q, _cte("ent", _EMPTY), _cte("src", _EMPTY), params, resolved)

    # Every kind that answers puts entities into ent, and whatever is in ent
    # brings its own documents with it - the ratings of an entity are in
    # scope wherever they were written, and the tab that counts documents
    # has to see those documents.
    src_bodies.append(_sources_of_entities())
    resolved["match"] = min((h["match"] for h in hits), key=_RUNGS.index)
    scope = ScopeSet("summary", q, _cte("ent", _union(ent_bodies)),
                     _cte("src", _union(src_bodies)), params, resolved, pre_ctes=pre)
    return _counted(scope, conn)


def _location_bucket_scope(conn, ctx, q: str, base: dict[str, Any], resolved: dict[str, Any],
                           bucket: Grouping) -> ScopeSet:
    """A location bucket: "Rotterdam", "Rotterdam Port" and "Schiedam" as ONE
    area.

    Each member is resolved exactly as if it had been typed alone - split
    into city/region/country where it is a place token, matched as a
    substring of the address otherwise - and the union is the area. Anything
    else would make a member inside a bucket behave differently from the same
    member typed into the box, which is the one thing a merge may not do.
    """
    params = dict(base)
    bodies: list[sql.Composable] = []
    for i, member in enumerate(bucket.members):
        name = (member.get("name") or "").strip()
        if not name:
            continue
        rungs, _place = _location_rungs(name, f"sc_m{i}")
        for _match, where, extra in rungs:
            body = _location_body(where)
            found = conn.execute(sql.SQL("SELECT count(*) AS n FROM ({b}) AS hits").format(b=body),
                                 {**base, **extra}).fetchone()
            if int(found["n"] if isinstance(found, dict) else found[0]):
                params.update(extra)
                bodies.append(body)
                break
    if not bodies:
        # The bucket still WON - the reader has to be told that this area is
        # what was searched - but nothing in the archive is inside it.
        resolved.update(match="bucket", label=bucket.name, names=bucket.values,
                        values=bucket.values, bucket=bucket.as_dict())
        return ScopeSet("location", q, _cte("ent", _EMPTY), _cte("src", _EMPTY), params, resolved)
    resolved.update(match="bucket", label=bucket.name, names=bucket.values,
                    values=bucket.values, bucket=bucket.as_dict())
    return ScopeSet("location", q, _cte("ent", _union(bodies)),
                    _cte("src", _sources_of_entities()), params, resolved)


def _resolve_location(conn, ctx, q: str, base: dict[str, Any], resolved: dict[str, Any]) -> ScopeSet:
    """The location scope: a location bucket first, then the rungs of
    _location_rungs, counted."""
    bucket = grouping_for(conn, ctx.project, "location", q)
    if bucket is not None:
        return _counted(_location_bucket_scope(conn, ctx, q, base, resolved, bucket), conn)
    rungs, place = _location_rungs(q, "sc")
    for match, where, extra in rungs:
        ent, src = _location_ctes(where)
        sc = ScopeSet("location", q, ent, src, {**base, **extra}, resolved)
        n = _count(conn, sc, "ent")
        if n:
            resolved.update(match=match, label=place.label or q, names=[place.label or q], entities=n,
                            sources=_count(conn, sc, "src"))
            return sc
    match, where, extra = rungs[-1]
    ent, src = _location_ctes(where)
    resolved.update(label=place.label or q)
    return ScopeSet("location", q, ent, src, {**base, **extra}, resolved)
