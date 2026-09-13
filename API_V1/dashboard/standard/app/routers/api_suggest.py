"""What the typeahead fields offer: /api/suggest/{kind}?q&limit.

Every suggestion comes out of the archive of the current project - there is
no dictionary and no external service. That is the whole point of the box:
what it offers is what a search will actually find.

Three rules hold for every kind, and they are why this is one file rather
than nine endpoints:

1. PREFIX FIRST, SUBSTRING ONLY IF THE PREFIX IS THIN. Typing "app" should
   put "Apple Inc." at the top, not "Snapple". The substring pass runs only
   when the prefix pass found fewer than MIN_PREFIX_HITS rows, and its
   results are appended after them - so the list a person reads is still
   "what starts with what I typed", with the rest as a fallback.

2. THERE IS ALWAYS A LIMIT. An entity list over a real archive is hundreds
   of thousands of rows long, and NN/g's suggestion research says ten short
   suggestions beat fifty. The default is twelve, the ceiling fifty.

3. THE PAIR IS BOUND. Suggestions are per project, and per language wherever
   the rows are translated (names, addresses, event types, topics). The
   exception is a bucket, which is the dashboard's own object and has no
   language at all.

4. A BUCKET APPEARS ONCE AND ITS MEMBERS DO NOT APPEAR BESIDE IT. Every kind
   `dashboard.buckets` can hold is folded (BUCKETED_KINDS below), including
   the two composed lists, where the folding is per ROW because the rows are
   of several kinds at once. A list that offered both would let a person
   pick a member and silently defeat the merge - half the answer, and
   nothing on screen saying a half is missing. `fold=0` asks for the members
   instead, and exactly one page does: the one where a bucket is made.

Three kinds are composed rather than read from one place. `entity_or_bucket`:
the Events field asks for either, so its list has to offer both, buckets
first. `anything`: the Summary page of Diagrams is the view of everything and
its magnifier searches everything, so its list holds all seven - buckets,
entities, documents, hosts, places, market topics, event types - with the
hint saying which. `place`: the Heatmap counts addresses and takes a city, a
country or a whole address, so its list offers all three sizes, the coarse
ones first. A field that names a kind of value it cannot suggest is a feature
nobody can find.

An empty `q` is not an error: the field opens its list on focus, before
anything is typed, and then the answer is "the most common values", which is
a useful thing to see.
"""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from psycopg import sql

from ..context import ContextDep
from ..db import Database, get_db
from ..scope import BUCKET_KINDS, entity_term, groupings, holder_for, member_holders
from ..sqlbuild import host_expression, like_prefix, like_substring

router = APIRouter(prefix="/api/suggest", tags=["suggest"])

DEFAULT_LIMIT = 12
MAX_LIMIT = 50
# Below this many prefix hits the substring pass is worth the second query.
MIN_PREFIX_HITS = 3


# ── The kinds ────────────────────────────────────────────────
#
# Each kind is one statement with a single %(pat)s placeholder for the
# lower-cased LIKE pattern. The columns are always the same four, so the
# response shape does not depend on the kind:
#
#   value  what the hidden field gets and what a search is run with
#   label  what is shown (usually the same as value)
#   hint   the lighter text after the label - a type, a region, a host;
#          it is what tells "Apple (Company)" from "Apple (Fruit)"
#   count  how many rows carry it, so the order is "most seen first"

# `mtype` repeats the type as its own column. The hint happens to be the
# type here and does not in every list, and the fold has to ask "is this
# name OF THIS TYPE in a bucket" - Apple the company is, Apple the fruit is
# not - so it reads a column that means the type and only the type.
_ENTITY = """
    SELECT e.text_name AS value, e.text_name AS label, e.text_type AS hint,
           e.text_type AS mtype,
           -- HOW OFTEN IT APPEARS, not how many tasks it appeared in.
           -- count(DISTINCT (task, entity)) is worthless on a real
           -- archive: an import whose source recorded 21
           -- task ids for 2.3 million mentions gives every single entity the
           -- answer 1, so the ranking collapses to alphabetical order and
           -- "Apple Inc." with 4059 mentions never reaches the first twelve
           -- while "Apple (Brand)" with one does. The number a person reads
           -- as "how much is there" is the number of mentions.
           count(*) AS count
      FROM processed_data.entities e
     WHERE e.text_project = %(project)s AND e.text_language = %(language)s
       AND e.text_name <> '' AND lower(e.text_name) LIKE %(pat)s
     GROUP BY e.text_name, e.text_type
     ORDER BY count DESC, value
     LIMIT %(limit)s
"""

# WHAT THE "Source or host" FIELD OFFERS.
#
# The host first, and the document's own name only where the extraction
# produced one. Reading `text_name` alone is not enough: on a real archive
# that field can hold an identifier in every single row (app/sqlbuild.py:
# MACHINE_NAME_REGEX) - so the list would offer hundreds of thousands of
# checksums, each with the count 1, and typing "vogue" would match none of
# the documents that came from vogue.com.
#
# Both halves are read from materialized views for the same reason the domain
# list is (sql/03-places-view.sql): splitting the host out of every URI, or
# scanning every name, takes a third of a second per keystroke on this
# archive and about a millisecond against the views.
#
# `count` means DOCUMENTS on both halves, so the two can be ordered against
# each other. The rank only breaks a tie: at equal counts the host goes
# first, because it is the broader thing and still leads to the title.
_SOURCE = """
    SELECT value, label, hint, count FROM (
        SELECT h.host AS value, h.host AS label, 'host' AS hint,
               h.integer_sources AS count, 0 AS rank
          FROM dashboard.source_hosts h
         WHERE h.text_project = %(project)s AND h.text_language = %(language)s
           AND lower(h.host) LIKE %(pat)s
        UNION ALL
        SELECT n.text_name, n.text_name, n.host, n.integer_sources, 1
          FROM dashboard.source_names n
         WHERE n.text_project = %(project)s AND n.text_language = %(language)s
           AND lower(n.text_name) LIKE %(pat)s
    ) AS readable
     ORDER BY count DESC, rank, value
     LIMIT %(limit)s
"""

# The host of a source URI, from dashboard.source_hosts.
#
# The host is not stored - the archive keeps `text_uri` and the dashboard
# cuts the host out of it (app/sqlbuild.py: host_expression) - and this
# statement must not do that cutting itself, over every document in the
# project, on every keystroke. On a real archive (750,000 documents in one
# project) that takes 10.1 s for the prefix pass and 11.7 s for the substring
# one: a field that answers while somebody types cannot do that work. The
# split is done once per refresh into a materialized view of one row per
# host - 13,594 rows there, so the same two passes take 1.2 ms and 9.0 ms.
# sql/03-places-view.sql builds it beside dashboard.places and the same
# background thread refreshes both (app/schema.py: refresh_places).
#
# The lag that buys: a host first seen since the last refresh is not
# SUGGESTED yet, though it is still FOUND - the search itself matches
# `text_uri` in the archive (app/scope.py), not this list.
_DOMAIN = """
    SELECT h.host AS value, h.host AS label, '' AS hint,
           h.integer_sources AS count
      FROM dashboard.source_hosts h
     WHERE h.text_project = %(project)s AND h.text_language = %(language)s
       AND lower(h.host) LIKE %(pat)s
     ORDER BY count DESC, value
     LIMIT %(limit)s
"""

# Addresses and city/country names come from dashboard.places, the
# materialized view that already carries the split parts - the Query page
# asks on every keystroke and must not re-split every location row each time.
_ADDRESS = """
    SELECT p.text_address AS value, p.text_address AS label,
           concat_ws(', ', p.text_city, p.text_region, p.text_country) AS hint,
           p.integer_locations AS count
      FROM dashboard.places p
     WHERE p.text_project = %(project)s AND p.text_language = %(language)s
       AND (lower(p.text_address) LIKE %(pat)s OR lower(p.text_city) LIKE %(pat)s
            OR lower(p.text_country) LIKE %(pat)s)
     ORDER BY p.integer_locations DESC, p.text_address
     LIMIT %(limit)s
"""

# City and country in one list: on the Query page both mean "search here",
# and a person types "USA" as readily as "Springfield". The hint says which
# of the two a row is, because "Luxembourg" is both.
_CITY_OR_COUNTRY = """
    SELECT value, label, hint, sum(count) AS count
      FROM (
        SELECT p.text_city AS value, p.text_city AS label, 'City' AS hint,
               p.integer_locations AS count
          FROM dashboard.places p
         WHERE p.text_project = %(project)s AND p.text_language = %(language)s
           AND p.text_city IS NOT NULL AND lower(p.text_city) LIKE %(pat)s
        UNION ALL
        SELECT p.text_country, p.text_country, 'Country', p.integer_locations
          FROM dashboard.places p
         WHERE p.text_project = %(project)s AND p.text_language = %(language)s
           AND p.text_country IS NOT NULL AND lower(p.text_country) LIKE %(pat)s
      ) AS both_kinds
     GROUP BY value, label, hint
     ORDER BY count DESC, value
     LIMIT %(limit)s
"""

# A place at any size, for a view that COUNTS places.
#
# The Heatmap's box takes a city, a country or a whole address (see
# app/routers/api_map.py), and a field that offers only one of the three
# would be promising less than the search behind it delivers - the same
# reason `anything` exists for the Summary page. The two coarse rows come
# first because they are the ones that cannot be typed out of an address
# list: "USA" as one row that counts every address in the country, and
# "Springfield" as one row that counts both towns of that name, with the
# addresses themselves underneath for whoever means exactly one of them.
#
# A one-part address ("Linjiang") is its own city, so it is offered once, as
# the city - two rows carrying the same value would look like two answers.
_PLACE = """
    SELECT value, label, hint, sum(count) AS count, min(grp) AS grp
      FROM (
        SELECT p.text_city AS value, p.text_city AS label, 'City' AS hint,
               p.integer_locations AS count, 0 AS grp
          FROM dashboard.places p
         WHERE p.text_project = %(project)s AND p.text_language = %(language)s
           AND p.text_city IS NOT NULL AND lower(p.text_city) LIKE %(pat)s
        UNION ALL
        SELECT p.text_country, p.text_country, 'Country', p.integer_locations, 0
          FROM dashboard.places p
         WHERE p.text_project = %(project)s AND p.text_language = %(language)s
           AND p.text_country IS NOT NULL AND lower(p.text_country) LIKE %(pat)s
        UNION ALL
        SELECT p.text_address, p.text_address,
               concat_ws(', ', p.text_city, p.text_region, p.text_country),
               p.integer_locations, 1
          FROM dashboard.places p
         WHERE p.text_project = %(project)s AND p.text_language = %(language)s
           AND lower(p.text_address) <> lower(p.text_city)
           AND (lower(p.text_address) LIKE %(pat)s OR lower(p.text_city) LIKE %(pat)s
                OR lower(p.text_country) LIKE %(pat)s)
      ) AS every_size_of_place
     GROUP BY value, label, hint
     ORDER BY grp, count DESC, value
     LIMIT %(limit)s
"""

# ── The type vocabularies ────────────────────────────────────
#
# The second axis of every Diagrams scope is a TYPE, and the field that
# offers it reads the vocabulary that axis searches - short, exact and
# closed, so the list can be shown in full on focus.
#
# THE LANGUAGE IS NOT BOUND. app/scope.py resolves a type search without it
# ("Unternehmen" typed in the German view and "Company" typed in the English
# view are the same entities), and a suggestion list that offered less than
# the search behind it accepts is a field that lies about what it can do.
# The hint says which language a spelling was seen in, so the two rows are
# still told apart - and a bucket over them makes them one row (see the
# folding in suggest() below).
_ENTITY_TYPE = """
    SELECT e.text_type AS value, e.text_type AS label,
           string_agg(DISTINCT e.text_language, ' - ' ORDER BY e.text_language) AS hint,
           count(*) AS count
      FROM processed_data.entities e
     WHERE e.text_project = %(project)s AND coalesce(e.text_type, '') <> ''
       AND lower(e.text_type) LIKE %(pat)s
     GROUP BY e.text_type
     ORDER BY count DESC, value
     LIMIT %(limit)s
"""

_SOURCE_TYPE = """
    SELECT s.text_type AS value, s.text_type AS label,
           string_agg(DISTINCT s.text_language, ' - ' ORDER BY s.text_language) AS hint,
           count(*) AS count
      FROM processed_data.sources s
     WHERE s.text_project = %(project)s AND coalesce(s.text_type, '') <> ''
       AND lower(s.text_type) LIKE %(pat)s
     GROUP BY s.text_type
     ORDER BY count DESC, value
     LIMIT %(limit)s
"""

_LOCATION_TYPE = """
    SELECT l.text_type AS value, l.text_type AS label,
           string_agg(DISTINCT l.text_language, ' - ' ORDER BY l.text_language) AS hint,
           count(*) AS count
      FROM processed_data.locations l
     WHERE l.text_project = %(project)s AND coalesce(l.text_type, '') <> ''
       AND lower(l.text_type) LIKE %(pat)s
     GROUP BY l.text_type
     ORDER BY count DESC, value
     LIMIT %(limit)s
"""

_ATTRIBUTE_TYPE = """
    SELECT a.text_type AS value, a.text_type AS label,
           string_agg(DISTINCT a.text_language, ' - ' ORDER BY a.text_language) AS hint,
           count(*) AS count
      FROM processed_data.attributes a
     WHERE a.text_project = %(project)s AND coalesce(a.text_type, '') <> ''
       AND lower(a.text_type) LIKE %(pat)s
     GROUP BY a.text_type
     ORDER BY count DESC, value
     LIMIT %(limit)s
"""

# A unit is not translated, so it carries what it measures as its hint
# instead - "t" alone says nothing about whether it is a bucket worth making.
_UNIT = """
    SELECT a.text_unit AS value, a.text_unit AS label,
           string_agg(DISTINCT a.text_type, ' - ' ORDER BY a.text_type) AS hint,
           count(*) AS count
      FROM processed_data.attributes a
     WHERE a.text_project = %(project)s AND coalesce(a.text_unit, '') <> ''
       AND lower(a.text_unit) LIKE %(pat)s
     GROUP BY a.text_unit
     ORDER BY count DESC, value
     LIMIT %(limit)s
"""

_EVENT_TYPE = """
    SELECT ev.text_type AS value, ev.text_type AS label, '' AS hint, count(*) AS count
      FROM processed_data.events ev
     WHERE ev.text_project = %(project)s AND ev.text_language = %(language)s
       AND ev.text_type <> '' AND lower(ev.text_type) LIKE %(pat)s
     GROUP BY ev.text_type
     ORDER BY count DESC, value
     LIMIT %(limit)s
"""

# ONE EVENT, BY ITS OWN NAME - the Diagrams events page's object axis.
#
# The type list above answers "every hearing"; this one answers "the
# Brussels hearing". The hint carries the type, because two events of one
# name in a large archive are told apart by nothing else on the screen.
_EVENT = """
    SELECT ev.text_name AS value, ev.text_name AS label,
           coalesce(max(ev.text_type), '') AS hint, count(*) AS count
      FROM processed_data.events ev
     WHERE ev.text_project = %(project)s AND ev.text_language = %(language)s
       AND ev.text_name <> '' AND lower(ev.text_name) LIKE %(pat)s
     GROUP BY ev.text_name
     ORDER BY count DESC, value
     LIMIT %(limit)s
"""

_MARKET_TOPIC = """
    SELECT m.text_topic AS value, m.text_topic AS label, '' AS hint, count(*) AS count
      FROM processed_data.market_insights m
     WHERE m.text_project = %(project)s AND m.text_language = %(language)s
       AND m.text_topic <> '' AND lower(m.text_topic) LIKE %(pat)s
     GROUP BY m.text_topic
     ORDER BY count DESC, value
     LIMIT %(limit)s
"""

# THE MARKET SCOPE'S SECOND AXIS: the closed vocabularies its table carries.
#
# A market topic is already a class, so "topic" is both the object and the
# type of that scope (app/scope.py). The useful second axis is the other
# vocabulary the row holds - an outlook or a sentiment - and both horizons
# count, because a reading is about the same entity whether the source was
# talking about the short or the long term.
#
# The language is NOT bound: these are the vocabulary the extraction chooses
# from, the same words in every language's rows, and binding it would only
# halve the counts. The hint says which of the two readings a value is, so
# "Neutral" is not offered twice with nothing to tell the rows apart.
_MARKET_VALUE = """
    SELECT value, value AS label, hint, sum(count) AS count
      FROM (
        SELECT m.text_short_term_outlook AS value, 'Outlook' AS hint, count(*) AS count
          FROM processed_data.market_insights m
         WHERE m.text_project = %(project)s AND coalesce(m.text_short_term_outlook, '') <> ''
         GROUP BY 1
        UNION ALL
        SELECT m.text_long_term_outlook, 'Outlook', count(*)
          FROM processed_data.market_insights m
         WHERE m.text_project = %(project)s AND coalesce(m.text_long_term_outlook, '') <> ''
         GROUP BY 1
        UNION ALL
        SELECT m.text_short_term_sentiment, 'Sentiment', count(*)
          FROM processed_data.market_insights m
         WHERE m.text_project = %(project)s AND coalesce(m.text_short_term_sentiment, '') <> ''
         GROUP BY 1
        UNION ALL
        SELECT m.text_long_term_sentiment, 'Sentiment', count(*)
          FROM processed_data.market_insights m
         WHERE m.text_project = %(project)s AND coalesce(m.text_long_term_sentiment, '') <> ''
         GROUP BY 1
      ) AS every_reading
     WHERE lower(value) LIKE %(pat)s
     GROUP BY value, hint
     ORDER BY count DESC, value
     LIMIT %(limit)s
"""

# The colour groups are keyed by the type NAME as the archive spells it, in
# every language, so this one deliberately does not bind the language: the
# Colours page assigns "Supplier" and "Lieferant" in one pass.
_CONNECTION_TYPE = """
    SELECT value, label, '' AS hint, sum(count) AS count
      FROM (
        SELECT c.text_type_parent_to_child AS value, c.text_type_parent_to_child AS label, count(*) AS count
          FROM processed_data.connections c
         WHERE c.text_project = %(project)s AND c.text_type_parent_to_child <> ''
           AND lower(c.text_type_parent_to_child) LIKE %(pat)s
         GROUP BY c.text_type_parent_to_child
        UNION ALL
        SELECT c.text_type_child_to_parent, c.text_type_child_to_parent, count(*)
          FROM processed_data.connections c
         WHERE c.text_project = %(project)s AND c.text_type_child_to_parent <> ''
           AND lower(c.text_type_child_to_parent) LIKE %(pat)s
         GROUP BY c.text_type_child_to_parent
      ) AS both_directions
     GROUP BY value, label
     ORDER BY count DESC, value
     LIMIT %(limit)s
"""

# A bucket belongs to the dashboard, not to a language. A bucket with no
# project applies to every project, which is why the filter is an OR.
_BUCKET = """
    SELECT b.text_name AS value, b.text_name AS label,
           CASE WHEN b.text_project IS NULL THEN 'every project' ELSE b.text_project END AS hint,
           count(m.bigint_id) AS count
      FROM dashboard.buckets b
      LEFT JOIN dashboard.bucket_members m ON m.bigint_fk_bucket = b.bigint_id
     WHERE (b.text_project IS NULL OR b.text_project = %(project)s)
       AND lower(b.text_name) LIKE %(pat)s
     GROUP BY b.bigint_id, b.text_name, b.text_project
     ORDER BY count DESC, value
     LIMIT %(limit)s
"""

# Entities and buckets in one list, buckets first.
#
# The Events page has ONE field labelled "Entity or bucket", and a field that
# promises a kind of value has to be able to offer it: with the entity list
# alone, the only buckets anybody could find were the ones that happen to
# share a name with an entity. A bucket that groups five suppliers under
# "Suppliers" was invisible unless the name was already known.
#
# `grp` puts the buckets at the top - there are a handful of them and they
# change what the search MEANS, so they are worth the first two rows - and
# the hint starts with the word "bucket", which is what tells them apart
# from an entity of the same name. (Plan A3 lists nine kinds; this tenth one
# is the critic's finding, and it is a composition of two of them rather
# than a new source of data.)
#
# AND THE ENTITIES A BUCKET HOLDS ARE IN IT, NOT BESIDE IT. The rows are
# folded through the entity member index (suggest() below), so "Apple"
# offers the bucket and Apple the FRUIT - which the bucket does not hold -
# and not the two spellings inside it. Three offers of which two run the
# same query, with nothing on screen saying so, is the thing the vocabulary
# rule exists to prevent.
#
# AN ENTITY ROW CARRIES ITS TYPE IN THE VALUE, THE BUCKET ROW DOES NOT.
# Every spelling of "Apple" sent a bare name and every one came back as the
# bucket: several offers, one query, and no way in the product to look at
# the fruit alone. The type therefore goes into the value, in the
# "Name (Type)" spelling app/scope.py resolves to that one entity
# (split_entity_term). The LABEL stays the bare name, so the list still
# reads "Apple - Fruit" and the type is not said twice.
#
# `src` AND `mtype` ARE WHAT LET THE FOLDING WORK HERE. A composed list holds
# rows of several kinds at once, so "which grouping does this row belong to"
# is a fact about the ROW, not about the endpoint: `src` names it ('bucket'
# for a row that IS one), and `mtype` carries the member type, because an
# entity is a name AND a type and Apple the fruit is not in the Apple bucket.
# Both are stripped before the answer goes out.
#
# AND THE BUCKETS OFFERED ARE THE ONES THIS FIELD CAN RESOLVE. Without
# reading `text_kind`, every bucket of every kind would be offered here -
# and offered FIRST, because buckets sort ahead of entities. An archive
# whose only bucket is "Fire", of kind `event_type`, would have it at the
# top of the entity field on the Map, the Heatmap, the Graph and Events "by
# entity", hinted "bucket - 4 members". Picking it would not merely be
# empty, it would be WRONG: app/scope.py looks a bucket up with `text_kind =
# 'entity'` (BUCKET_LOOKUP_SQL), finds none, falls through to the literal
# term and maps a Team called Fire in Portland, Oregon - "1 entity around
# Fire" - while the four event types the person meant are dropped without a
# word. One named object meaning two different things on two views is
# exactly what the folding rule below exists to prevent, so the offer is
# made only where the search behind it can honour it: %(bucket_kinds)s is
# COMPOSED_KINDS[kind], the kinds this list's rows can resolve to.
_ENTITY_OR_BUCKET = """
    SELECT value, label, hint, count, grp, src, mtype
      FROM (
        SELECT b.text_name AS value, b.text_name AS label,
               'bucket - ' || count(m.bigint_id)
                 || CASE WHEN count(m.bigint_id) = 1 THEN ' member' ELSE ' members' END AS hint,
               count(m.bigint_id) AS count, 0 AS grp, 'bucket' AS src, NULL::text AS mtype
          FROM dashboard.buckets b
          LEFT JOIN dashboard.bucket_members m ON m.bigint_fk_bucket = b.bigint_id
         WHERE (b.text_project IS NULL OR b.text_project = %(project)s)
           AND b.text_kind = ANY(%(bucket_kinds)s)
           AND lower(b.text_name) LIKE %(pat)s
         GROUP BY b.bigint_id, b.text_name
        UNION ALL
        SELECT CASE WHEN coalesce(e.text_type, '') = '' THEN e.text_name
                    ELSE e.text_name || ' (' || e.text_type || ')' END,
               e.text_name, e.text_type,
               count(*), 1, 'entity', e.text_type   -- mentions; see _ENTITY
          FROM processed_data.entities e
         WHERE e.text_project = %(project)s AND e.text_language = %(language)s
           AND e.text_name <> '' AND lower(e.text_name) LIKE %(pat)s
         GROUP BY e.text_name, e.text_type
      ) AS entities_and_buckets
     ORDER BY grp, count DESC, value
     LIMIT %(limit)s
"""

# Everything the archive is named by, in one list.
#
# The Summary page of Diagrams is the view of EVERYTHING, and its magnifier
# has to be able to offer everything: an entity, a bucket, a document, a
# host, a place, a market topic, an event type. app/scope.py resolves a term
# typed there against all five kinds at once (_resolve_everything), so a
# field that could only suggest entities would be promising less than the
# search behind it delivers - and the hint is what says which of the seven a
# row is, because "Zurich" is an entity AND a place.
#
# Buckets come first for the same reason as in `entity_or_bucket`: there are
# a handful of them, they change what the search MEANS, and they are worth
# the first row. Everything else is ordered by how often the archive says
# it, which is the only ordering that does not privilege one kind of thing
# over another.
_ANYTHING = """
    SELECT value, label, hint, sum(count) AS count, min(grp) AS grp, src, mtype
      FROM (
        SELECT b.text_name AS value, b.text_name AS label,
               'bucket - ' || count(m.bigint_id)
                 || CASE WHEN count(m.bigint_id) = 1 THEN ' member' ELSE ' members' END AS hint,
               count(m.bigint_id) AS count, 0 AS grp, 'bucket' AS src, NULL::text AS mtype
          FROM dashboard.buckets b
          LEFT JOIN dashboard.bucket_members m ON m.bigint_fk_bucket = b.bigint_id
         WHERE (b.text_project IS NULL OR b.text_project = %(project)s)
           AND b.text_kind = ANY(%(bucket_kinds)s)
           AND lower(b.text_name) LIKE %(pat)s
         GROUP BY b.bigint_id, b.text_name
        UNION ALL
        SELECT e.text_name, e.text_name, coalesce(nullif(e.text_type, ''), 'Entity'),
               count(*), 1, 'entity', e.text_type   -- mentions; see _ENTITY
          FROM processed_data.entities e
         WHERE e.text_project = %(project)s AND e.text_language = %(language)s
           AND e.text_name <> '' AND lower(e.text_name) LIKE %(pat)s
         GROUP BY e.text_name, e.text_type
        UNION ALL
        SELECT s.text_name, s.text_name, 'Document', count(*), 1, NULL, NULL
          FROM processed_data.sources s
         WHERE s.text_project = %(project)s AND s.text_language = %(language)s
           AND s.text_name <> '' AND lower(s.text_name) LIKE %(pat)s
         GROUP BY s.text_name
        UNION ALL
        -- The hosts, from the same materialized view /api/suggest/domain
        -- reads: this magnifier asks on every keystroke too, and splitting
        -- three quarters of a million URIs inside it was ten seconds of the
        -- eleven this list took.
        SELECT h.host, h.host, 'Host', h.integer_sources, 1, NULL, NULL
          FROM dashboard.source_hosts h
         WHERE h.text_project = %(project)s AND h.text_language = %(language)s
           AND lower(h.host) LIKE %(pat)s
        UNION ALL
        SELECT p.text_address, p.text_address, 'Place', p.integer_locations, 1, 'location', NULL
          FROM dashboard.places p
         WHERE p.text_project = %(project)s AND p.text_language = %(language)s
           AND (lower(p.text_address) LIKE %(pat)s OR lower(p.text_city) LIKE %(pat)s
                OR lower(p.text_country) LIKE %(pat)s)
        UNION ALL
        SELECT m.text_topic, m.text_topic, 'Market topic', count(*), 1, 'market_topic', NULL
          FROM processed_data.market_insights m
         WHERE m.text_project = %(project)s AND m.text_language = %(language)s
           AND m.text_topic <> '' AND lower(m.text_topic) LIKE %(pat)s
         GROUP BY m.text_topic
        UNION ALL
        SELECT ev.text_type, ev.text_type, 'Event type', count(*), 1, 'event_type', NULL
          FROM processed_data.events ev
         WHERE ev.text_project = %(project)s AND ev.text_language = %(language)s
           AND ev.text_type <> '' AND lower(ev.text_type) LIKE %(pat)s
         GROUP BY ev.text_type
      ) AS everything
     GROUP BY value, label, hint, src, mtype
     ORDER BY grp, count DESC, value
     LIMIT %(limit)s
"""

KINDS: dict[str, str] = {
    "anything": _ANYTHING,
    "entity": _ENTITY,
    "entity_or_bucket": _ENTITY_OR_BUCKET,
    "source": _SOURCE,
    "domain": _DOMAIN,
    "address": _ADDRESS,
    "city_or_country": _CITY_OR_COUNTRY,
    "place": _PLACE,
    "entity_type": _ENTITY_TYPE,
    "source_type": _SOURCE_TYPE,
    "location_type": _LOCATION_TYPE,
    "attribute_type": _ATTRIBUTE_TYPE,
    "unit": _UNIT,
    "event_type": _EVENT_TYPE,
    "event": _EVENT,
    "market_topic": _MARKET_TOPIC,
    "market_value": _MARKET_VALUE,
    "connection_type": _CONNECTION_TYPE,
    "bucket": _BUCKET,
}

# WHICH SUGGESTION LISTS A BUCKET FOLDS.
#
# A bucket appears once and its members do not appear separately: a person
# who picks the member instead of the bucket gets half the answer, and
# nothing on screen says a half is missing (app/scope.member_index says the
# same thing in its own docstring, and docs/DESIGN.md states it as a rule).
# The value the folded row sends is the BUCKET NAME, which app/scope.py
# resolves back to every member - so the field promises exactly what the
# search behind it does.
#
# EVERY KIND `dashboard.buckets` CAN HOLD IS IN HERE. It is derived from
# app/scope.BUCKET_KINDS rather than written out, because a list written
# out by hand loses a kind - entity and location, the two a person is most
# likely to bucket, are the easiest to leave out - and then
# /api/suggest/entity, the Map's and the Graph's `entity_or_bucket` field
# and the Diagrams magnifier all offer the bare member and never the
# bucket, which is exactly the failure the rule exists to prevent. A tenth
# kind cannot be forgotten the same way -
# tests/flow/test_suggest.py walks BUCKET_KINDS and fails on any kind whose
# list does not fold.
#
# CONNECTION TYPES ARE THE ONE EXCEPTION, deliberately. That list is what the
# Connection colours page searches to ASSIGN a type to a group. Folding it
# there would hide "Lieferant" behind the very group somebody is trying to
# put it in - the page where a grouping is made cannot be a page that shows
# only groupings. A search that USES the grouping goes through
# app/scope.resolve_terms, which reads a colour group exactly as it reads a
# bucket, and the vocabulary dropdown (/api/types/connection) folds like the
# rest. THE BUCKETS PAGE HAS THE SAME NEED and answers it the same way, but
# per request rather than per kind: it asks with `fold=0`, because the page
# where a bucket is made must be able to see the members it is made of.
#
# Which grouping kind each list reads. A list whose rows are all of one kind
# says so once here; a COMPOSED list (entities and buckets in one, or the
# Summary's "anything") carries the kind per row in its `src` column,
# because its rows are not all the same kind of thing.
_SUGGEST_KIND_GROUPING = {
    "entity": "entity",
    # The three sizes of place all count locations, and a location bucket
    # ("Rotterdam, Rotterdam Port, Schiedam - one area") merges them
    # wherever they are offered.
    "address": "location",
    "city_or_country": "location",
    "place": "location",
}
BUCKETED_KINDS = {
    **{kind: kind for kind in BUCKET_KINDS if kind not in ("entity", "location")},
    **_SUGGEST_KIND_GROUPING,
}

# The lists that hold rows of several kinds at once. Their rows carry `src`;
# these are the grouping kinds those rows can name, and the ones whose
# indexes are therefore worth reading.
COMPOSED_KINDS: dict[str, tuple[str, ...]] = {
    "entity_or_bucket": ("entity",),
    "anything": ("entity", "location", "market_topic", "event_type"),
}


def _statement(kind: str) -> sql.Composable:
    """The kind's statement, with the host expression filled in where it is
    needed. Composed here rather than stored composed so the constants above
    stay readable as SQL."""
    text = KINDS[kind]
    if "{host}" in text:
        return sql.SQL(text).format(host=host_expression("s"))
    return sql.SQL(text)


def _rows(conn, kind: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(_statement(kind), params).fetchall()]


# WHAT IS ALREADY IN THE BUCKET IS NOT OFFERED TO IT AGAIN.
#
# Only the Buckets page passes `?bucket=`, and only there does the question
# arise: every other field offers a bucket's members folded into the bucket
# and never asks what is in one. The member list sits directly above the
# field, so a suggestion that repeats it spends one of twelve rows saying
# what the reader can already see.
#
# A member with no type means "this name, whatever its type" (the schema
# says so), so it takes the name out on every type - which is exactly what
# the bucket does when it searches.


def members_of_bucket(conn, bucket_id: int) -> list[tuple[str, str | None]]:
    rows = conn.execute(
        "SELECT text_name, text_type FROM dashboard.bucket_members "
        " WHERE bigint_fk_bucket = %s", (bucket_id,)).fetchall()
    return [((r["text_name"] or "").strip().lower(),
             (r["text_type"] or "").strip().lower() or None) for r in rows]


def _without_members_of(rows: list[dict[str, Any]],
                        held: list[tuple[str, str | None]]) -> list[dict[str, Any]]:
    if not held:
        return rows
    any_type = {name for name, typ in held if typ is None}
    exact = {(name, typ) for name, typ in held if typ is not None}

    def is_member(row: dict[str, Any]) -> bool:
        # The label is the bare name on every list that can be a member; the
        # value carries the type on a folded one, which is why the label is
        # what is compared here.
        name = (row.get("label") or row.get("value") or "").strip().lower()
        typ = (row.get("mtype") or row.get("hint") or "").strip().lower()
        return name in any_type or (name, typ) in exact

    return [r for r in rows if not is_member(r)]


def _merge(first: list[dict[str, Any]], second: list[dict[str, Any]],
           limit: int) -> list[dict[str, Any]]:
    """Prefix hits, then the substring hits that are not already in the list.
    (value, hint) is the identity: "Apple (Company)" and "Apple (Fruit)" are
    two suggestions, and dropping one of them would hide exactly the row a
    person is looking for."""
    seen = {(r["value"], r.get("hint")) for r in first}
    out = list(first)
    for row in second:
        key = (row["value"], row.get("hint"))
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
        if len(out) >= limit:
            break
    return out[:limit]


@router.get("/{kind}")
def suggest(kind: str, ctx: ContextDep, db: Database = Depends(get_db),
            q: str = Query("", description="what has been typed so far"),
            limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
            bucket: int | None = Query(None, description=(
                "the bucket being edited: its own members are not offered. "
                "The Buckets page passes it; nothing else does.")),
            fold: bool = Query(True, description=(
                "false: offer the members themselves rather than the buckets "
                "that hold them. For the page where a bucket is MADE - see "
                "BUCKETED_KINDS above - and nowhere else."))):
    if kind not in KINDS:
        raise HTTPException(404, {"error": f"no suggestions of kind {kind!r}",
                                  "hint": "one of " + ", ".join(sorted(KINDS))})
    term = (q or "").strip().lower()
    # A term that is only wildcards would match everything and read as a bug;
    # LIKE's own metacharacters are escaped by like_prefix/like_substring, so
    # this only trims whitespace and control characters.
    term = re.sub(r"\s+", " ", term)

    # The grouping kinds this list can fold through: one for a plain list,
    # several for a composed one, none when the caller asked for the members.
    grouped = BUCKETED_KINDS.get(kind)
    kinds = COMPOSED_KINDS.get(kind, (grouped,) if grouped else ())
    params = {"project": ctx.project, "language": ctx.language,
              "limit": limit, "pat": like_prefix(term),
              # WHICH BUCKETS A COMPOSED LIST MAY OFFER (see _ENTITY_OR_BUCKET).
              # Its bucket half is a UNION over the whole table, so without
              # this it offers kinds the search behind the field cannot
              # resolve - and offers them first. Not narrowed by `fold`: a
              # bucket of the wrong kind is wrong on that list whether or not
              # its members are folded into it.
              "bucket_kinds": list(COMPOSED_KINDS.get(kind, ()))}
    if not fold:
        kinds = ()
    with db.read() as conn:
        rows = _rows(conn, kind, params)
        if term and len(rows) < MIN_PREFIX_HITS:
            more = _rows(conn, kind, {**params, "pat": like_substring(term)})
            rows = _merge(rows, more, limit)
        held = {gk: member_holders(conn, ctx.project, gk) for gk in kinds}
        # THE BUCKET IS ALSO OFFERED WHEN NONE OF ITS MEMBERS MATCHED.
        # Somebody typing the bucket's own name has to find it, and the
        # members it holds may be spelled nothing like it. A composed list
        # already offers every bucket from its own UNION, so it does not
        # need this half and would end up with the row twice.
        named = ([g for g in groupings(conn, ctx.project, grouped)
                  if not term or term in g.name.lower()]
                 if grouped and kind not in COMPOSED_KINDS and fold else [])
        held_by_bucket = members_of_bucket(conn, bucket) if bucket is not None else []

    if kinds:
        # A COMPOSED LIST KEEPS THE WORD "bucket" IN THE HINT. Its rows are
        # of several kinds and the hint is the only thing that says which a
        # row is: "Apple - 2 merged - …" beside "Zurich, Switzerland -
        # Place" would leave a reader guessing what the first one is. A
        # single-kind list needs no such word - every row in it is that
        # kind - and keeps the shorter hint it has always had.
        rows = _fold(rows, held, named, default_kind=grouped, limit=limit,
                     word="bucket - " if kind in COMPOSED_KINDS else "")
        # A FOLDED LIST HAS TO SEND AN UNAMBIGUOUS VALUE.
        #
        # Once "Apple (Company)" has been folded into the bucket, the entity
        # row left standing is Apple the FRUIT - and a bare "Apple" is
        # resolved by app/scope.py to the bucket, so the list would be
        # offering a search it cannot run. The type therefore travels in the
        # value, in the spelling split_entity_term() parses back; the label
        # stays the bare name, so the row still reads "Apple - Fruit" and
        # the type is not said twice. `entity_or_bucket` already did this
        # and wrote down why; the other two entity lists now agree with it.
        for row in rows:
            if (row.get("src") or grouped) != "entity":
                continue
            typed = entity_term(row.get("label") or "", row.get("mtype"))
            if typed:
                row["value"] = typed

    rows = _without_members_of(rows, held_by_bucket)

    return {"kind": kind, "q": q, "items": [
        {"value": r["value"], "label": r["label"], "hint": r.get("hint") or "",
         "count": int(r["count"]) if r.get("count") is not None else None}
        for r in rows if r["value"]
    ]}


def _member_of(row: dict[str, Any], kind: str) -> tuple[str, str | None]:
    """The (name, type) a row would be a bucket member under.

    An entity is a name AND a type - "Apple (Company)" is in the bucket and
    "Apple (Fruit)" is not - so the fold has to ask with both. The value of
    an entity row carries the type in some lists and not in others, which is
    why `label` and `mtype` are read instead of the value.
    """
    if kind == "entity":
        return str(row.get("label") or row.get("value") or ""), row.get("mtype")
    return str(row.get("value") or ""), None


def _fold(rows: list[dict[str, Any]], held: dict[str, dict[str, list[tuple[str | None, str]]]],
          named: list[Any], default_kind: str | None, limit: int,
          word: str = "") -> list[dict[str, Any]]:
    """Members replaced by the bucket that holds them, in the place the first
    of them stood, plus any bucket whose own name matched and whose members
    did not. The hint says what a folded row stands for, because a row that
    quietly means five things is the thing this is meant to prevent.

    A COMPOSED LIST ALREADY HOLDS THE BUCKETS (its `src` says 'bucket'), so
    those rows are the anchors the members fold INTO - otherwise "Apple"
    would be offered twice, once from the buckets table and once as the
    fold of the entities under it. Such a row's count means "members" until
    something folds in, and records afterwards, which is why it is reset.
    """
    out: list[dict[str, Any]] = []
    at: dict[str, dict[str, Any]] = {}

    def anchor(name: str) -> dict[str, Any]:
        found = at.get(name.strip().lower())
        if found is None:
            found = {"value": name, "label": name, "hint": "", "count": 0, "_members": []}
            at[name.strip().lower()] = found
            out.append(found)
        return found

    def fold_in(name: str, row: dict[str, Any] | None, members: list[str]) -> None:
        found = anchor(name)
        if found.pop("_from_bucket_row", False):
            # It counted its members; from here it counts records.
            found["count"] = 0
        found["_members"].extend(members)
        if row is not None:
            found["count"] = int(found["count"] or 0) + int(row.get("count") or 0)

    # The buckets a composed list already offers, registered first so a
    # member folding in finds the row instead of making a second one. Two
    # buckets of one name (a project's own and a global one) keep their two
    # rows; only the first is an anchor, because only one of them is what a
    # term resolves to (app/scope.grouping_for orders them).
    for row in rows:
        if row.get("src") != "bucket" or not row.get("value"):
            continue
        key = str(row["value"]).strip().lower()
        out.append(row)
        if key in at:
            continue
        at[key] = row
        row["_members"] = []
        row["_from_bucket_row"] = True

    for row in rows:
        if row.get("src") == "bucket":
            continue
        kind = row.get("src") or default_kind
        holders = held.get(kind) if kind else None
        name, member_type = _member_of(row, kind or "")
        holder = holder_for(holders, name, member_type) if holders else None
        if holder is None:
            out.append(row)
        else:
            fold_in(holder, row, [row["value"]])
    for group in named:
        if group.name.strip().lower() not in at:
            fold_in(group.name, None, group.values)
    for row in out:
        row.pop("_from_bucket_row", None)
        members = row.pop("_members", None)
        if members:
            row["hint"] = f"{word}{len(members)} merged - " + ", ".join(members[:3]) + (
                ", …" if len(members) > 3 else "")
    return out[:limit]
