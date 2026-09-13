"""A small, known archive for the dashboard's flow tests.

    DATABASE_URL=postgresql://... python tests/preseed/preseed.py
    DATABASE_URL=postgresql://... python tests/preseed/preseed.py --remove

Writes two projects the way the collector would - one row set per language,
translations under the same task ids with the same entity ids - and the
dashboard rows the tests need (a bucket, colour assignments). Idempotent:
loading removes its own rows first, so it can be run again after a change;
--remove takes everything out and leaves nothing behind, including the
colour assignments it added (recorded in dashboard.settings so a customer's
own assignment of "Supplier" is never touched).

The names all begin with `_preseed`, so nothing here can be mistaken for a
customer's project, and every fact the tests assert is spelled out below
rather than generated: when a test fails, the answer is in this file.

What is in it:

  _preseed Alpha  English AND German. Apple Inc. / Apple (Company) - one
                  bucket - and Apple (Fruit), which must stay out of it.
                  Microsoft, Foxconn (Taipei), Tim Cook, the European
                  Commission (Brussels), Springfield Works (Illinois) and
                  Springfield Mills (Ontario) for the disambiguation, Nordwyk
                  Pumps (Rotterdam), Zurich Insurance, Linjiang Province
                  (a one-part address). Six sources from three hosts, written
                  2 h, 3 d, 20 d, 60 d, 200 d and 2 y ago, summaries with
                  "battery", "antitrust", "pump". Seven mapped connection
                  types plus "Mysterious Bond" (fallback colour), with
                  reversed duplicates. Events, one of them about the
                  document only, one dated in the future. Every rating value
                  x two perspectives x three relations. Attributes with and
                  without a number. Every outlook and sentiment value.
  _preseed Beta   English only. Contoso (Berlin) and a second Apple
                  (Company), to prove projects stay apart.

  the log        twelve monitoring.scraper_errors rows (four kinds, all
                 three severities, two sources, spread over the last two
                 hours, yesterday and last week) and seven
                 monitoring.scraper_runs rows covering all six statuses,
                 one of them for a THIRD source that has runs and no
                 problems. Without them /logs is an empty page on a fresh
                 archive and neither its filters nor its two tabs can be
                 looked at.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

ALPHA = "_preseed Alpha"
BETA = "_preseed Beta"
PROJECTS = (ALPHA, BETA)
LANGUAGES = {ALPHA: ("English", "German"), BETA: ("English",)}
TAG = "preseed"
BUCKET_NAME = "Apple"
SETTING_COLOUR_TYPES = "preseed.colour_types"

#: The crawler's log rows below all carry a run id starting with this, and
#: the run rows a name starting with it, so --remove takes out exactly them.
#: monitoring.* has no text_project to key on: those tables are written
#: before any task exists, so the three identity columns stay empty there by
#: design (see app/routers/api_logs.py) and the marker has to be the run.
LOG_PREFIX = "_preseed"

#: Two sources with problems and one with none, because "did MY source run?"
#: is the question the Log view is opened with and it needs both answers on
#: the screen. The ids are far above anything scraper_config.sources will
#: mint in a test, so they can never collide with a source somebody makes.
LOG_SOURCES = {
    900001: ("_preseed Flats in Zurich", "flats.preseed.example"),
    900002: ("_preseed Federal law", "law.preseed.example"),
    900003: ("_preseed Company register", "register.preseed.example"),
}

#: (hours ago, run, kind, severity, source, label, message, detail, uri, status)
#:
#: Four kinds over three severities and two sources, spread over the last two
#: hours, yesterday and last week - so every time range of the filter has
#: something in it and "Everything kept" is not the only reading that works.
#: An `info` row is a TEST of a configuration, not something that went wrong:
#: the Sources overview counts problems and leaves those out, and a preseed
#: with none of them would never exercise that.
LOG_ERRORS = [
    (0.25, "r1", "NO_LINKS", "info", 900002, "no links on the list page",
     "The list page of _preseed Federal law had no links to collect. The site may "
     "build its list with JavaScript - try the Rendered engine on the source.",
     "0 links in <main>, 41 in the page as a whole (navigation and footer)",
     "https://law.preseed.example/search", None),
    (0.6, "r2", "ROBOTS_FORBIDDEN", "error", 900001, "robots forbids the list page",
     "robots.txt of flats.preseed.example forbids the list page of _preseed Flats in "
     "Zurich, so nothing was fetched. Ask the site owner for permission, or watch a "
     "list page the rules allow.",
     "Disallow: /rent/  (User-agent: *)\nrobots.txt answered HTTP 200",
     "https://flats.preseed.example/rent/zurich", None),
    (0.9, "r3", "FILE_TOO_LARGE", "warning", 900001, "a file is over its limit",
     "plan-7.pdf is larger than the limit set for pdf files (more than 1 MB), so it "
     "was not sent. Raise the limit for this type on the source, or leave the file out.",
     "Content-Length: 4 812 993 bytes, limit 1 048 576",
     "https://flats.preseed.example/files/plan-7.pdf", None),
    (1.7, "r4", "HTTP_5XX", "error", 900002, "the site has a problem of its own",
     "law.preseed.example has a problem of its own (HTTP 503) and could not answer "
     "for _preseed Federal law. Nothing to change here; the next run tries again.",
     "HTTP 503 Service Unavailable\nRetry-After: 600", "https://law.preseed.example/search", 503),
    (26, "r5", "ROBOTS_FORBIDDEN", "error", 900001, "robots forbids the list page",
     "robots.txt of flats.preseed.example forbids the list page of _preseed Flats in "
     "Zurich, so nothing was fetched. Ask the site owner for permission, or watch a "
     "list page the rules allow.",
     "Disallow: /rent/  (User-agent: *)", "https://flats.preseed.example/rent/zurich", None),
    (27, "r6", "FILE_TOO_LARGE", "warning", 900001, "a file is over its limit",
     "grundriss-2.pdf is larger than the limit set for pdf files (more than 1 MB), so "
     "it was not sent. Raise the limit for this type on the source.",
     "Content-Length: 2 210 044 bytes, limit 1 048 576",
     "https://flats.preseed.example/files/grundriss-2.pdf", None),
    (28, "r7", "NO_LINKS", "info", 900002, "no links on the list page",
     "The list page of _preseed Federal law had no links to collect. The site may "
     "build its list with JavaScript - try the Rendered engine on the source.",
     "0 links in <main>", "https://law.preseed.example/search", None),
    (30, "r8", "HTTP_5XX", "error", 900002, "the site has a problem of its own",
     "law.preseed.example has a problem of its own (HTTP 502) and could not answer "
     "for _preseed Federal law. Nothing to change here; the next run tries again.",
     "HTTP 502 Bad Gateway", "https://law.preseed.example/search", 502),
    (24 * 3, "r9", "NO_LINKS", "info", 900002, "no links on the list page",
     "The list page of _preseed Federal law had no links to collect. The site may "
     "build its list with JavaScript - try the Rendered engine on the source.",
     "0 links in <main>", "https://law.preseed.example/search", None),
    (24 * 4, "r10", "HTTP_5XX", "error", 900002, "the site has a problem of its own",
     "law.preseed.example has a problem of its own (HTTP 503) and could not answer "
     "for _preseed Federal law. Nothing to change here; the next run tries again.",
     "HTTP 503 Service Unavailable", "https://law.preseed.example/search", 503),
    (24 * 5, "r11", "ROBOTS_FORBIDDEN", "error", 900001, "robots forbids the list page",
     "robots.txt of flats.preseed.example forbids the list page of _preseed Flats in "
     "Zurich, so nothing was fetched. Ask the site owner for permission.",
     "Disallow: /rent/  (User-agent: *)", "https://flats.preseed.example/rent/zurich", None),
    (24 * 6, "r12", "FILE_TOO_LARGE", "warning", 900001, "a file is over its limit",
     "situationsplan.pdf is larger than the limit set for pdf files (more than 1 MB), "
     "so it was not sent. Raise the limit for this type on the source.",
     "Content-Length: 1 998 210 bytes, limit 1 048 576",
     "https://flats.preseed.example/files/situationsplan.pdf", None),
]

#: (hours ago, status, source, key label, message, pages, links, accepted,
#:  new, files, submitted, ms)
#:
#: All six statuses, so every entry of the Result filter has a row behind it,
#: and 900003 has RUNS AND NO PROBLEMS - the source a Source filter counted
#: over the error table alone would miss altogether.
LOG_RUNS = [
    (0.33, "TEST", 900003, "register", "A test of this configuration found 62 links.",
     1, 62, 62, 62, 0, 0, 2180),
    (0.6, "SKIPPED", 900001, "flats", "robots.txt forbids the list page.",
     0, 0, 0, 0, 0, 0, 41),
    (0.75, "OK", 900003, "register", None, 3, 218, 41, 12, 4, 12, 4210),
    (1.7, "BLOCKED", 900002, "law", "The site asked for a browser check and the run stopped.",
     1, 0, 0, 0, 0, 0, 9120),
    (3.0, "OK", 900001, "flats", None, 2, 96, 33, 8, 2, 8, 3040),
    (26, "BACKPRESSURE", 900002, "law",
     "The platform asked for a pause; the remaining documents wait for the next run.",
     4, 180, 96, 21, 0, 9, 15400),
    (24 * 5, "ERROR", 900002, "law",
     "The run stopped on an unexpected error in the crawler.", 1, 0, 0, 0, 0, 0, 880),
]

SQL_DIR = Path(__file__).resolve().parents[2] / "sql"
SCHEMA_FILES = ("01-dashboard-schema.sql", "03-places-view.sql", "02-dashboard-seed.sql")

DATA_TABLES = ("sources", "entities", "locations", "events", "ratings", "connections",
               "attributes", "market_insights", "source_importances_by_perspective",
               "event_entities", "tasks")
VOCAB_TABLES = ("source_types", "entity_types", "event_types", "location_types", "perspective_types",
                "rating_types", "connection_types", "relation_types", "attribute_types", "unit_types",
                "importance_types", "market_topic_types", "outlook_types", "sentiment_types",
                "rating_values", "trustlist_types")

# The seven connection types the tests rely on for colours, in both
# languages, and the group each goes to.
COLOUR_TYPES = {
    "Supplier": "supplier", "Lieferant": "supplier",
    "Competitor": "competitor", "Wettbewerber": "competitor",
    "CEO": "person",
    "Regulator": "regulator", "Regulierungsbehörde": "regulator",
    "Partner": "partner",
    "Customer": "customer", "Kunde": "customer",
    "Subsidiary": "ownership", "Tochtergesellschaft": "ownership",
}

# ── Translation ──────────────────────────────────────────────
# Free text is translated: names, types, addresses, topics, summaries.
# The closed vocabularies (rating values, outlooks, sentiments, importance,
# relation to source) stay as the API publishes them.

GERMAN = {
    "Company": "Unternehmen", "Fruit": "Frucht", "Person": "Person", "Regulator": "Regulierungsbehörde",
    "Region": "Region", "European Commission": "Europäische Kommission",
    "Linjiang Province": "Provinz Linjiang",
    "Cupertino, California, USA": "Cupertino, Kalifornien, USA",
    "Redmond, Washington, USA": "Redmond, Washington, USA",
    "Taipei, Taiwan": "Taipeh, Taiwan",
    "Brussels, Belgium": "Brüssel, Belgien",
    "Springfield, Illinois, USA": "Springfield, Illinois, USA",
    "Springfield, Ontario, Canada": "Springfield, Ontario, Kanada",
    "Rotterdam, Netherlands": "Rotterdam, Niederlande",
    "Zurich, Switzerland": "Zürich, Schweiz",
    "Linjiang": "Linjiang",
    "Headquarters": "Hauptsitz", "Factory": "Fabrik", "Office": "Büro", "Seat": "Sitz", "Province": "Provinz",
    "Supplier": "Lieferant", "Customer": "Kunde", "Competitor": "Wettbewerber", "CEO": "CEO",
    "Partner": "Partner", "Subsidiary": "Tochtergesellschaft", "Parent company": "Muttergesellschaft",
    "Regulated entity": "Beaufsichtigtes Unternehmen", "Mysterious Bond": "Geheimnisvolle Verbindung",
    "Employer": "Arbeitgeber",
    "Product announcement": "Produktankündigung", "Regulatory action": "Regulierungsmassnahme",
    "Hearing": "Anhörung", "Delivery": "Lieferung", "Harvest": "Ernte", "Registration": "Registrierung",
    "Product launch": "Produkteinführung",
    "News article": "Nachrichtenartikel", "Regulatory filing": "Behördliche Einreichung",
    "Blog post": "Blogbeitrag",
    "Maintenance": "Wartung", "Compliance": "Compliance",
    "Reputation": "Reputation", "Reliability": "Zuverlässigkeit",
    "Capacity": "Kapazität", "Chemistry": "Chemie", "Throughput": "Durchsatz", "Pressure": "Druck",
    "Headcount": "Personalbestand",
    "Consumer electronics": "Unterhaltungselektronik", "Batteries": "Batterien", "Antitrust": "Kartellrecht",
    "Industrial equipment": "Industrieausrüstung", "Insurance": "Versicherung", "Agriculture": "Landwirtschaft",
    "Cloud services": "Cloud-Dienste",
}


def tr(text: str | None, language: str) -> str | None:
    if text is None or language == "English":
        return text
    return GERMAN.get(text, text)


# ── The data ─────────────────────────────────────────────────
# Entities by id. The same id string is used wherever the entity appears, in
# any task - what the extraction does when it recognises the same thing.

ENTITIES: dict[str, dict[str, Any]] = {
    "ent:apple-inc":   {"name": "Apple Inc.", "type": "Company",
                        "loc": ("Headquarters", "Cupertino, California, USA", 37.3318, -122.0312)},
    "ent:apple":       {"name": "Apple", "type": "Company", "loc": None},
    # Plainly "Apple", type Fruit - the whole point; "Apfel" in German.
    "ent:apple-fruit": {"name": "Apple", "type": "Fruit", "loc": None, "de": "Apfel"},
    "ent:microsoft":   {"name": "Microsoft", "type": "Company",
                        "loc": ("Headquarters", "Redmond, Washington, USA", 47.6740, -122.1215)},
    "ent:foxconn":     {"name": "Foxconn", "type": "Company",
                        "loc": ("Factory", "Taipei, Taiwan", 25.0330, 121.5654)},
    "ent:tim-cook":    {"name": "Tim Cook", "type": "Person", "loc": None},
    "ent:ec":          {"name": "European Commission", "type": "Regulator",
                        "loc": ("Seat", "Brussels, Belgium", 50.8503, 4.3517)},
    "ent:sf-works":    {"name": "Springfield Works", "type": "Company",
                        "loc": ("Factory", "Springfield, Illinois, USA", 39.7817, -89.6501)},
    "ent:sf-mills":    {"name": "Springfield Mills", "type": "Company",
                        "loc": ("Factory", "Springfield, Ontario, Canada", 42.8390, -80.9000)},
    "ent:nordwyk":     {"name": "Nordwyk Pumps", "type": "Company",
                        "loc": ("Factory", "Rotterdam, Netherlands", 51.9225, 4.4792)},
    "ent:zurich":      {"name": "Zurich Insurance", "type": "Company",
                        "loc": ("Headquarters", "Zurich, Switzerland", 47.3769, 8.5417)},
    "ent:linjiang":    {"name": "Linjiang Province", "type": "Region",
                        "loc": ("Province", "Linjiang", 41.8100, 126.9100)},
    "ent:contoso":     {"name": "Contoso", "type": "Company",
                        "loc": ("Office", "Berlin, Germany", 52.5200, 13.4050)},
}

RATING_VALUES = ("Egregious", "Very Bad", "Bad", "Neutral", "Good", "Very Good", "Excellent")
PERSPECTIVES = ("Maintenance", "Compliance")
RELATIONS = ("Direct", "Indirect", "Unrelated")
OUTLOOKS = ("Rising", "Neutral", "Declining", "Unset")
SENTIMENTS = ("Very Negative", "Negative", "Slightly Negative", "Neutral",
              "Slightly Positive", "Positive", "Very Positive", "Unset")
IMPORTANCES = ("Not Important", "Low Importance", "Medium Importance", "High Importance", "Critical Importance")


def _ratings_full(entity: str, source: str) -> list[dict[str, Any]]:
    out = []
    for value in RATING_VALUES:
        for perspective in PERSPECTIVES:
            for relation in RELATIONS:
                out.append({"entity": entity, "source": source, "name": "Reputation",
                            "perspective": perspective, "value": value, "relation": relation})
    return out


def _market_full(entity: str, source: str, topic: str) -> list[dict[str, Any]]:
    out = []
    for i, sentiment in enumerate(SENTIMENTS):
        out.append({"entity": entity, "source": source, "topic": topic,
                    "short_outlook": OUTLOOKS[i % 4], "long_outlook": OUTLOOKS[(i + 1) % 4],
                    "short_sentiment": sentiment, "long_sentiment": SENTIMENTS[-1 - i],
                    "high": i % 2 == 0, "relation": RELATIONS[i % 3]})
    return out


# One task = one document = one source. `age` is how long ago the document
# was written; commissioned 30 min later, archived 10 min after that, so the
# chunk prefilter (date_added >= window start) holds for every row.
TASKS: list[dict[str, Any]] = [
    {
        "project": ALPHA, "task": "_preseed:alpha:1", "age": timedelta(hours=2),
        # THE ONE DOCUMENT WITH A TITLE OF ITS OWN, which is what a document
        # that has one looks like beside the four below that do not. `src:<slug>`
        # is the extraction's id and is shown to nobody (app/source_names.py:
        # IDENTIFIER_REGEX), so a preseed in which every name was one of those
        # could not tell a title from an id at all.
        #
        # IT DOES NOT BEGIN WITH "Apple". The source probe answers "prefix"
        # when a name starts with the term and "substring" when it merely
        # contains it, and both answers are measured on this archive
        # (tests/flow/test_scope_resolution.py) - a title starting with the
        # word every test searches for would quietly turn one into the other.
        "source": {"id": "src:battery", "name": "Inside the new battery",
                   "uri": "https://news.example.com/apple-battery",
                   "type": "News article", "importance": "High Importance", "trustful": True,
                   "summary": "Apple Inc. unveils a solid-state battery built with Foxconn.",
                   "importances": {"Maintenance": "Medium Importance", "Compliance": "Low Importance"}},
        "entities": ["ent:apple-inc", "ent:foxconn", "ent:tim-cook"],
        "connections": [
            ("ent:foxconn", "ent:apple-inc", "Supplier", "Customer"),
            ("ent:apple-inc", "ent:foxconn", "Customer", "Supplier"),       # reversed duplicate
            ("ent:tim-cook", "ent:apple-inc", "CEO", "Employer"),
        ],
        "events": [
            {"name": "Battery announcement", "type": "Product announcement", "date": timedelta(days=1),
             "about": ["ent:apple-inc", "ent:foxconn"]},
            # Dated in the future relative to its document: the events branch
            # runs without the chunk prefilter for exactly this row.
            {"name": "Battery product launch", "type": "Product launch", "date": timedelta(days=-30),
             "about": ["ent:apple-inc"]},
        ],
        "ratings": _ratings_full("ent:apple-inc", "src:battery"),
        "attributes": [
            {"entity": "ent:apple-inc", "name": "Battery capacity", "type": "Capacity", "unit": "mAh",
             "value": "5000", "float": 5000.0},
            {"entity": "ent:apple-inc", "name": "Battery chemistry", "type": "Chemistry", "unit": "",
             "value": "solid state", "float": None},
            {"entity": "ent:foxconn", "name": "Line capacity", "type": "Capacity", "unit": "units/day",
             "value": "approx. 12000", "float": 12000.0},
            {"entity": "ent:apple-inc", "name": "Employees", "type": "Headcount", "unit": "people",
             "value": "164000", "float": 164000.0},
        ],
        "market": _market_full("ent:apple-inc", "src:battery", "Batteries")
                  + [{"entity": "ent:foxconn", "source": "src:battery", "topic": "Consumer electronics",
                      "short_outlook": "Rising", "long_outlook": "Rising", "short_sentiment": "Positive",
                      "long_sentiment": "Very Positive", "high": True, "relation": "Direct"}],
    },
    {
        "project": ALPHA, "task": "_preseed:alpha:2", "age": timedelta(days=3),
        "source": {"id": "src:antitrust", "uri": "https://filings.example.org/antitrust-2026",
                   "type": "Regulatory filing", "importance": "Critical Importance", "trustful": True,
                   "summary": "The European Commission opens an antitrust probe into Apple and Microsoft.",
                   "importances": {"Compliance": "Critical Importance", "Maintenance": "Not Important"}},
        "entities": ["ent:ec", "ent:apple", "ent:microsoft"],
        "connections": [
            ("ent:ec", "ent:apple", "Regulator", "Regulated entity"),
            ("ent:apple", "ent:microsoft", "Competitor", "Competitor"),
            ("ent:microsoft", "ent:apple", "Competitor", "Competitor"),    # reversed duplicate
        ],
        "events": [
            {"name": "Antitrust probe opened", "type": "Regulatory action", "date": timedelta(days=3),
             "about": ["ent:ec", "ent:apple", "ent:microsoft"]},
            # About the document, not about anything in it: no event_entities.
            {"name": "Hearing scheduled", "type": "Hearing", "date": timedelta(days=-10), "about": []},
        ],
        "ratings": [
            {"entity": "ent:apple", "source": "src:antitrust", "name": "Reputation", "perspective": "Compliance",
             "value": "Bad", "relation": "Direct"},
            {"entity": "ent:microsoft", "source": "src:antitrust", "name": "Reputation", "perspective": "Compliance",
             "value": "Very Bad", "relation": "Direct"},
            {"entity": "ent:ec", "source": "src:antitrust", "name": "Reliability", "perspective": "Compliance",
             "value": "Good", "relation": "Indirect"},
        ],
        "attributes": [
            {"entity": "ent:microsoft", "name": "Employees", "type": "Headcount", "unit": "people",
             "value": "228000", "float": 228000.0},
        ],
        "market": [
            {"entity": "ent:apple", "source": "src:antitrust", "topic": "Antitrust",
             "short_outlook": "Declining", "long_outlook": "Neutral", "short_sentiment": "Negative",
             "long_sentiment": "Slightly Negative", "high": True, "relation": "Direct"},
            {"entity": "ent:microsoft", "source": "src:antitrust", "topic": "Cloud services",
             "short_outlook": "Neutral", "long_outlook": "Rising", "short_sentiment": "Neutral",
             "long_sentiment": "Positive", "high": False, "relation": "Indirect"},
        ],
    },
    {
        "project": ALPHA, "task": "_preseed:alpha:3", "age": timedelta(days=20),
        "source": {"id": "src:pump", "uri": "https://blog.example.net/pump-delivery",
                   "type": "Blog post", "importance": "Medium Importance", "trustful": False,
                   "summary": "Nordwyk Pumps ships a centrifugal pump to Springfield Works, insured by Zurich.",
                   "importances": {"Maintenance": "High Importance"}},
        "entities": ["ent:nordwyk", "ent:sf-works", "ent:zurich"],
        "connections": [
            ("ent:nordwyk", "ent:sf-works", "Supplier", "Customer"),
            ("ent:zurich", "ent:nordwyk", "Partner", "Partner"),
            ("ent:zurich", "ent:nordwyk", "Mysterious Bond", "Mysterious Bond"),   # unmapped type
        ],
        "events": [
            {"name": "Pump delivered", "type": "Delivery", "date": timedelta(days=21),
             "about": ["ent:sf-works", "ent:nordwyk"]},
        ],
        "ratings": [
            {"entity": "ent:nordwyk", "source": "src:pump", "name": "Reliability", "perspective": "Maintenance",
             "value": "Excellent", "relation": "Direct"},
            {"entity": "ent:sf-works", "source": "src:pump", "name": "Reliability", "perspective": "Maintenance",
             "value": "Neutral", "relation": "Unrelated"},
        ],
        "attributes": [
            {"entity": "ent:nordwyk", "name": "Flow rate", "type": "Throughput", "unit": "m³/h",
             "value": "180", "float": 180.0},
            {"entity": "ent:nordwyk", "name": "Max pressure", "type": "Pressure", "unit": "bar",
             "value": "approx. 10", "float": 10.0},
            {"entity": "ent:nordwyk", "name": "Second stage pressure", "type": "Pressure", "unit": "bar",
             "value": "16", "float": 16.0},
            {"entity": "ent:sf-works", "name": "Inlet pressure", "type": "Pressure", "unit": "bar",
             "value": "2.5", "float": 2.5},
            {"entity": "ent:sf-works", "name": "Paint colour", "type": "Chemistry", "unit": "",
             "value": "signal red", "float": None},
        ],
        "market": [
            {"entity": "ent:nordwyk", "source": "src:pump", "topic": "Industrial equipment",
             "short_outlook": "Rising", "long_outlook": "Rising", "short_sentiment": "Slightly Positive",
             "long_sentiment": "Positive", "high": False, "relation": "Direct"},
            {"entity": "ent:zurich", "source": "src:pump", "topic": "Insurance",
             "short_outlook": "Unset", "long_outlook": "Unset", "short_sentiment": "Unset",
             "long_sentiment": "Unset", "high": False, "relation": "Unrelated"},
        ],
    },
    {
        "project": ALPHA, "task": "_preseed:alpha:4", "age": timedelta(days=60),
        "source": {"id": "src:harvest", "uri": "https://news.example.com/springfield-harvest",
                   "type": "News article", "importance": "Low Importance", "trustful": True,
                   "summary": "Springfield Mills in Ontario processes this year's apple harvest with a new pump.",
                   "importances": {"Maintenance": "Low Importance"}},
        "entities": ["ent:sf-mills", "ent:apple-fruit", "ent:nordwyk"],
        "connections": [
            ("ent:sf-mills", "ent:nordwyk", "Customer", "Supplier"),
        ],
        "events": [
            {"name": "Harvest processed", "type": "Harvest", "date": timedelta(days=62),
             "about": ["ent:sf-mills"]},
        ],
        "ratings": [
            {"entity": "ent:sf-mills", "source": "src:harvest", "name": "Reputation", "perspective": "Maintenance",
             "value": "Very Good", "relation": "Direct"},
        ],
        "attributes": [
            {"entity": "ent:sf-mills", "name": "Harvest volume", "type": "Throughput", "unit": "t",
             "value": "1'200", "float": 1200.0},
        ],
        "market": [
            {"entity": "ent:sf-mills", "source": "src:harvest", "topic": "Agriculture",
             "short_outlook": "Neutral", "long_outlook": "Declining", "short_sentiment": "Neutral",
             "long_sentiment": "Slightly Negative", "high": False, "relation": "Indirect"},
        ],
    },
    {
        "project": ALPHA, "task": "_preseed:alpha:5", "age": timedelta(days=200),
        # NAMED THE WAY A REAL ARCHIVE CAN NAME A DOCUMENT, which is not a
        # name at all: `src_<id>` or a checksum (app/sqlbuild.py:
        # MACHINE_NAME_REGEX). Every other document here carries a readable
        # name, and if ALL of them did, a suggestion list that offers
        # text_name unconditionally would look right - on a real archive it
        # then offers hundreds of thousands of checksums, one per document,
        # each with the count 1. These two rows are what tells the two apart.
        "source": {"id": "src:subsidiary", "name": "src_1416664",
                   "uri": "https://filings.example.org/apple-subsidiary",
                   "type": "Regulatory filing", "importance": "Medium Importance", "trustful": True,
                   "summary": "Apple Inc. registers Apple as a subsidiary for Linjiang Province.",
                   "importances": {"Compliance": "Medium Importance"}},
        "entities": ["ent:apple-inc", "ent:apple", "ent:linjiang"],
        "connections": [
            ("ent:apple-inc", "ent:apple", "Subsidiary", "Parent company"),
            ("ent:apple", "ent:apple-inc", "Parent company", "Subsidiary"),   # reversed duplicate
        ],
        "events": [
            {"name": "Subsidiary registered", "type": "Registration", "date": timedelta(days=205),
             "about": ["ent:apple", "ent:apple-inc"]},
        ],
        "ratings": [
            {"entity": "ent:apple", "source": "src:subsidiary", "name": "Reputation", "perspective": "Compliance",
             "value": "Good", "relation": "Direct"},
        ],
        "attributes": [
            {"entity": "ent:apple", "name": "Employees", "type": "Headcount", "unit": "people",
             "value": "unknown", "float": None},
        ],
        "market": [
            {"entity": "ent:apple", "source": "src:subsidiary", "topic": "Consumer electronics",
             "short_outlook": "Rising", "long_outlook": "Neutral", "short_sentiment": "Slightly Positive",
             "long_sentiment": "Neutral", "high": False, "relation": "Direct"},
        ],
    },
    {
        "project": ALPHA, "task": "_preseed:alpha:6", "age": timedelta(days=730),
        "source": {"id": "src:old-antitrust", "name": "8846ebcc63ff6a49245ef85e08e776de",
                   "uri": "https://blog.example.net/old-antitrust",
                   "type": "Blog post", "importance": "Low Importance", "trustful": False,
                   "summary": "Two years ago: an older antitrust dispute between Microsoft and the Commission.",
                   "importances": {"Compliance": "Low Importance"}},
        "entities": ["ent:microsoft", "ent:ec"],
        "connections": [
            ("ent:ec", "ent:microsoft", "Regulator", "Regulated entity"),
        ],
        "events": [
            {"name": "Old antitrust fine", "type": "Regulatory action", "date": timedelta(days=740),
             "about": ["ent:microsoft"]},
        ],
        "ratings": [
            {"entity": "ent:microsoft", "source": "src:old-antitrust", "name": "Reputation",
             "perspective": "Compliance", "value": "Egregious", "relation": "Direct"},
        ],
        "attributes": [],
        "market": [
            {"entity": "ent:microsoft", "source": "src:old-antitrust", "topic": "Antitrust",
             "short_outlook": "Declining", "long_outlook": "Declining", "short_sentiment": "Very Negative",
             "long_sentiment": "Negative", "high": True, "relation": "Direct"},
        ],
    },
    {
        "project": BETA, "task": "_preseed:beta:1", "age": timedelta(days=5),
        "source": {"id": "src:contoso", "uri": "https://news.example.com/contoso-apple",
                   "type": "News article", "importance": "Medium Importance", "trustful": True,
                   "summary": "Contoso of Berlin partners with Apple on a battery for pump controllers.",
                   "importances": {"Maintenance": "Medium Importance"}},
        "entities": ["ent:contoso", "ent:apple"],
        "connections": [
            ("ent:contoso", "ent:apple", "Partner", "Partner"),
        ],
        "events": [
            {"name": "Partnership announced", "type": "Product announcement", "date": timedelta(days=5),
             "about": ["ent:contoso", "ent:apple"]},
        ],
        "ratings": [
            {"entity": "ent:contoso", "source": "src:contoso", "name": "Reputation", "perspective": "Maintenance",
             "value": "Good", "relation": "Direct"},
        ],
        "attributes": [
            {"entity": "ent:contoso", "name": "Employees", "type": "Headcount", "unit": "people",
             "value": "340", "float": 340.0},
        ],
        "market": [
            {"entity": "ent:contoso", "source": "src:contoso", "topic": "Batteries",
             "short_outlook": "Rising", "long_outlook": "Rising", "short_sentiment": "Positive",
             "long_sentiment": "Positive", "high": True, "relation": "Direct"},
        ],
    },
]


# ── Writing ──────────────────────────────────────────────────

def _connect(dsn: str) -> psycopg.Connection:
    return psycopg.connect(dsn, row_factory=dict_row, autocommit=False)


def apply_dashboard_schema(conn: psycopg.Connection) -> None:
    """The three schema files, in the order app/schema.py runs them. All
    idempotent, so this is safe on an archive the dashboard already seeded."""
    for name in SCHEMA_FILES:
        conn.execute((SQL_DIR / name).read_text(encoding="utf-8"))


def _dates(age: timedelta, now: datetime) -> dict[str, datetime]:
    written = now - age
    commissioned = written + timedelta(minutes=30)
    return {"written": written, "commissioned": commissioned,
            "evaluated": commissioned + timedelta(minutes=5), "added": commissioned + timedelta(minutes=10)}


# HOW OFTEN AN ENTITY IS NAMED INSIDE ONE TASK.
#
# Every entity here got exactly one row per task, and while that was true of
# ALL of them, two different questions had the same answer: "how many
# mentions" and "how many task/entity pairs" could not be told apart. A
# suggestion list that counted the wrong one therefore passed every test
# here and was wrong on the first real archive it met - one company had
# 4,059 mentions under a single task id, the list showed 1 beside every
# name, fell back to alphabetical order, and the entity somebody was looking
# for was nowhere near the top.
#
# So one entity is named three times in each task that carries it, and the
# two readings give different numbers on this data as well.
MENTIONS = {"ent:apple-inc": 3}


# A SECOND ADDRESS FOR ONE ENTITY, IN ONE TASK ONLY.
#
# The map draws two different pictures and the preseed has to tell them
# apart: Connections places every entity at the address it carries MOST
# OFTEN, Locations draws every address of the entity that was searched.
# With one address each those two are the same picture, so a map that draws
# all of them - covering the map with the addresses of every company a
# searched person is connected to - would pass.
#
# Apple Inc. is in two tasks. The head office is in both (twice), the second
# office in one (once), so the main address is decided by the data rather
# than by the order rows come back in.
EXTRA_LOCATIONS = {
    ("_preseed:alpha:1", "ent:apple-inc"):
        ("Office", "Austin, Texas, USA", 30.2672, -97.7431),
}


def _write_task(cur, task: dict[str, Any], language: str, now: datetime,
                vocab: dict[str, set[tuple[str, str, str]]]) -> None:
    project, task_id = task["project"], task["task"]
    d = _dates(task["age"], now)
    src = task["source"]
    base = (project, language, task_id)

    def voc(table: str, name: str | None) -> None:
        if name:
            vocab[table].add((name, project, language))

    content_hash = hashlib.sha256(src["uri"].encode()).hexdigest()
    cur.execute(
        """INSERT INTO processed_data.tasks
             (date_added, text_name, text_project, text_language, text_task_id, text_job_id,
              integer_task_index, text_status, text_source_uri, text_content_hash, text_tag,
              float_cost_chf, bigint_processing_ms, date_commissioned, date_evaluated)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (d["added"], task_id, project, language, task_id, task_id.rsplit(":", 1)[0], 0, "COMPLETED",
         src["uri"], content_hash, TAG, 0.01, 1200, d["commissioned"], d["evaluated"]))

    cur.execute(
        """INSERT INTO processed_data.sources
             (date_added, text_name, text_project, text_language, text_task_id, text_job_id,
              text_source_id, text_uri, text_content_hash, text_type, text_importance, text_summary,
              text_reason, text_keywords, bool_trustful, date_written, date_commissioned, date_evaluated)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        # text_name is the document's own name where it has one; `src["name"]`
        # is the archive's identifier where it does not (see the two tasks
        # above). text_source_id stays src["id"] either way - that is the
        # identity, and it is never shown to anybody.
        (d["added"], src.get("name", src["id"]), *base, task_id, src["id"], src["uri"], content_hash,
         tr(src["type"], language), src["importance"], tr(src["summary"], language),
         "preseed", "battery, antitrust, pump", src["trustful"], d["written"], d["commissioned"], d["evaluated"]))
    voc("source_types", tr(src["type"], language))
    for perspective, importance in src["importances"].items():
        cur.execute(
            """INSERT INTO processed_data.source_importances_by_perspective
                 (date_added, text_name, text_project, text_language, text_task_id, text_job_id,
                  text_fk_source_id, text_perspective, text_importance, text_reason,
                  date_commissioned, date_evaluated)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (d["added"], f"{src['id']}|{tr(perspective, language)}", *base, task_id, src["id"],
             tr(perspective, language), importance, "preseed", d["commissioned"], d["evaluated"]))
        voc("perspective_types", tr(perspective, language))

    # Only the MENTION repeats. The entity is one thing wherever it is named,
    # so its place is written once - repeating the location row too would say
    # Apple Inc. has three head offices, and the Heatmap would count them.
    for eid in task["entities"]:
        ent = ENTITIES[eid]
        name = ent["de"] if language == "German" and "de" in ent else tr(ent["name"], language)
        for _ in range(MENTIONS.get(eid, 1)):
            cur.execute(
                """INSERT INTO processed_data.entities
                     (date_added, text_name, text_project, text_language, text_task_id, text_job_id,
                      text_entity_id, text_fk_source_id, text_type, text_description, text_keywords,
                      text_relation_to_source, date_commissioned, date_evaluated)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (d["added"], name, *base, task_id, eid, src["id"], tr(ent["type"], language), "preseed", "",
                 "Direct", d["commissioned"], d["evaluated"]))
        voc("entity_types", tr(ent["type"], language))
        spots = [ent["loc"]] if ent["loc"] else []
        extra = EXTRA_LOCATIONS.get((task_id, eid))
        if extra:
            spots.append(extra)
        for ltype, address, lat, lng in spots:
            cur.execute(
                """INSERT INTO processed_data.locations
                     (date_added, text_name, text_project, text_language, text_task_id, text_job_id,
                      text_fk_entity_id, text_fk_source_id, text_type, text_description, text_address,
                      text_relation_to_source, float_latitude, float_longitude,
                      date_commissioned, date_evaluated)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (d["added"], tr(ltype, language), *base, task_id, eid, src["id"], tr(ltype, language), "",
                 tr(address, language), "Direct", lat, lng, d["commissioned"], d["evaluated"]))
            voc("location_types", tr(ltype, language))

    for parent, child, p2c, c2p in task["connections"]:
        cur.execute(
            """INSERT INTO processed_data.connections
                 (date_added, text_name, text_project, text_language, text_task_id, text_job_id,
                  text_fk_source_id, text_fk_parent_entity_id, text_fk_child_entity_id,
                  text_type_parent_to_child, text_type_child_to_parent, text_reason,
                  text_relation_to_source, date_commissioned, date_evaluated)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (d["added"], f"{parent}|{child}", *base, task_id, src["id"], parent, child,
             tr(p2c, language), tr(c2p, language), "preseed", "Direct", d["commissioned"], d["evaluated"]))
        voc("connection_types", tr(p2c, language))
        voc("connection_types", tr(c2p, language))

    for ev in task["events"]:
        cur.execute(
            """INSERT INTO processed_data.events
                 (date_added, text_name, text_project, text_language, text_task_id, text_job_id,
                  text_fk_source_id, text_type, text_description, text_reason, text_relation_to_source,
                  date_eventdate, date_commissioned, date_evaluated)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING bigint_id""",
            (d["added"], ev["name"], *base, task_id, src["id"], tr(ev["type"], language),
             ev["name"], "preseed", "Direct", now - ev["date"], d["commissioned"], d["evaluated"]))
        event_key = cur.fetchone()["bigint_id"]
        voc("event_types", tr(ev["type"], language))
        for eid in ev["about"]:
            cur.execute(
                """INSERT INTO processed_data.event_entities
                     (date_added, text_name, text_project, text_language, text_task_id, text_job_id,
                      text_fk_source_id, bigint_fk_event_id, text_fk_entity_id,
                      date_commissioned, date_evaluated)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (d["added"], f"{ev['name']}|{eid}", *base, task_id, src["id"], event_key, eid,
                 d["commissioned"], d["evaluated"]))

    for r in task["ratings"]:
        cur.execute(
            """INSERT INTO processed_data.ratings
                 (date_added, text_name, text_project, text_language, text_task_id, text_job_id,
                  text_fk_entity_id, text_fk_source_id, text_rating_name, text_perspective,
                  text_rating_value, text_reason, text_relation_to_source, date_commissioned, date_evaluated)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (d["added"], f"{r['entity']}|{r['name']}|{r['perspective']}|{r['value']}|{r['relation']}",
             *base, task_id, r["entity"], r["source"], tr(r["name"], language), tr(r["perspective"], language),
             r["value"], "preseed", r["relation"], d["commissioned"], d["evaluated"]))
        voc("rating_types", tr(r["name"], language))
        voc("perspective_types", tr(r["perspective"], language))

    for a in task["attributes"]:
        cur.execute(
            """INSERT INTO processed_data.attributes
                 (date_added, text_name, text_project, text_language, text_task_id, text_job_id,
                  text_fk_entity_id, text_fk_source_id, text_type, text_unit, text_value, float_value,
                  text_description, text_reason, text_relation_to_source, date_commissioned, date_evaluated)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (d["added"], a["name"], *base, task_id, a["entity"], src["id"], tr(a["type"], language),
             a["unit"], a["value"], a["float"], "", "preseed", "Direct", d["commissioned"], d["evaluated"]))
        voc("attribute_types", tr(a["type"], language))
        voc("unit_types", a["unit"])

    for m in task["market"]:
        cur.execute(
            """INSERT INTO processed_data.market_insights
                 (date_added, text_name, text_project, text_language, text_task_id, text_job_id,
                  text_fk_entity_id, text_fk_source_id, text_topic, text_long_term_outlook,
                  text_short_term_outlook, text_long_term_sentiment, text_short_term_sentiment,
                  bool_high_relevance, text_reason, text_relation_to_source, date_commissioned, date_evaluated)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (d["added"], f"{m['entity']}|{m['topic']}", *base, task_id, m["entity"], m["source"],
             tr(m["topic"], language), m["long_outlook"], m["short_outlook"], m["long_sentiment"],
             m["short_sentiment"], m["high"], "preseed", m["relation"], d["commissioned"], d["evaluated"]))
        voc("market_topic_types", tr(m["topic"], language))


def _write_vocabulary(cur, vocab: dict[str, set[tuple[str, str, str]]]) -> None:
    # The closed vocabularies are seeded under project '' by the archive and
    # stay English; the open ones get a row per project and language, the way
    # the collector inserts them as names first appear.
    for table, rows in vocab.items():
        for name, project, language in sorted(rows):
            cur.execute(
                f"INSERT INTO processed_data.{table} (text_name, text_project, text_language, text_task_id) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING", (name, project, language, TAG))


def _write_dashboard_rows(cur) -> None:
    cur.execute("INSERT INTO dashboard.buckets (text_name, text_project) VALUES (%s, %s) RETURNING bigint_id",
                (BUCKET_NAME, ALPHA))
    bucket = cur.fetchone()["bigint_id"]
    for name, typ in (("Apple Inc.", "Company"), ("Apple", "Company")):
        cur.execute("INSERT INTO dashboard.bucket_members (bigint_fk_bucket, text_name, text_type) VALUES (%s, %s, %s)",
                    (bucket, name, typ))

    groups = {r["text_key"]: r["bigint_id"] for r in
              cur.execute("SELECT text_key, bigint_id FROM dashboard.colour_groups").fetchall()}
    added: list[str] = []
    for type_name, key in COLOUR_TYPES.items():
        cur.execute(
            "INSERT INTO dashboard.colour_group_types (text_type_name, bigint_fk_group, text_source) "
            "VALUES (%s, %s, 'manual') ON CONFLICT (text_type_name) DO NOTHING RETURNING text_type_name",
            (type_name, groups[key]))
        if cur.fetchone():
            added.append(type_name)
    # Remember what was ours, so --remove takes out exactly that.
    cur.execute(
        "INSERT INTO dashboard.settings (text_key, text_value) VALUES (%s, %s) "
        "ON CONFLICT (text_key) DO UPDATE SET text_value = EXCLUDED.text_value, date_updated = now()",
        (SETTING_COLOUR_TYPES, json.dumps(added)))


def _has(cur, name: str) -> bool:
    return cur.execute("SELECT to_regclass(%s) AS r", (name,)).fetchone()["r"] is not None


def _write_crawler_log(cur) -> int:
    """The rows /logs is made of.

    Without them the Log view is an empty page on a fresh archive: it says
    the crawler has not reported anything yet, which is true and tells a
    reviewer nothing about the view. These rows are what the crawler writes
    when it is refused, when a file is over its limit and when a list page
    turns out to be built with JavaScript - the three things a customer
    actually meets - plus the run history underneath them.

    Skipped on an archive that predates database/init/03-scraper.sql: the
    dashboard is expected to work on one (the view says so itself), and the
    preseed must not be the thing that breaks there.
    """
    if not _has(cur, "monitoring.scraper_errors"):
        return 0
    written = 0
    for hours, run, kind, severity, sid, label, message, detail, uri, status in LOG_ERRORS:
        name, host = LOG_SOURCES[sid]
        cur.execute("""
            INSERT INTO monitoring.scraper_errors
                  (date_added, text_name, bigint_fk_source, text_source_name, text_host,
                   text_kind, text_severity, text_uri, integer_http_status,
                   text_message, text_detail, text_run_id)
            VALUES (now() - make_interval(secs => %(secs)s), %(label)s, %(source)s,
                    %(sname)s, %(host)s, %(kind)s, %(severity)s, %(uri)s, %(status)s,
                    %(message)s, %(detail)s, %(run)s)
        """, {"secs": hours * 3600, "label": label, "source": sid, "sname": name,
              "host": host, "kind": kind, "severity": severity, "uri": uri,
              "status": status, "message": message, "detail": detail,
              "run": f"{LOG_PREFIX}-{run}"})
        written += 1
    if not _has(cur, "monitoring.scraper_runs"):
        return written
    for (hours, status, sid, key, message, pages, links, accepted, new_,
         files, submitted, ms) in LOG_RUNS:
        name, _host = LOG_SOURCES[sid]
        cur.execute("""
            INSERT INTO monitoring.scraper_runs
                  (date_added, text_name, text_status, text_key_label, text_message,
                   bigint_fk_source, integer_pages, integer_links, integer_accepted,
                   integer_new, integer_files, integer_submitted, integer_ms)
            VALUES (now() - make_interval(secs => %(secs)s), %(name)s, %(status)s,
                    %(key)s, %(message)s, %(source)s, %(pages)s, %(links)s,
                    %(accepted)s, %(new)s, %(files)s, %(submitted)s, %(ms)s)
        """, {"secs": hours * 3600, "name": name, "status": status, "key": key,
              "message": message, "source": sid, "pages": pages, "links": links,
              "accepted": accepted, "new": new_, "files": files,
              "submitted": submitted, "ms": ms})
        written += 1
    return written


def _remove_rows(cur) -> None:
    for table in DATA_TABLES + VOCAB_TABLES:
        cur.execute(f"DELETE FROM processed_data.{table} WHERE text_project = ANY(%s)", (list(PROJECTS),))
    # The crawler's log, by the marker it was written with. A customer's own
    # rows carry a run id the crawler minted and a source name of their own,
    # so nothing of theirs matches either pattern.
    if _has(cur, "monitoring.scraper_errors"):
        cur.execute("DELETE FROM monitoring.scraper_errors WHERE text_run_id LIKE %s",
                    (LOG_PREFIX + "-%",))
    if _has(cur, "monitoring.scraper_runs"):
        cur.execute("DELETE FROM monitoring.scraper_runs WHERE text_name LIKE %s",
                    (LOG_PREFIX + " %",))
    if not _has(cur, "dashboard.buckets"):
        return
    cur.execute("DELETE FROM dashboard.buckets WHERE text_project = ANY(%s)", (list(PROJECTS),))
    row = cur.execute("SELECT text_value FROM dashboard.settings WHERE text_key = %s",
                      (SETTING_COLOUR_TYPES,)).fetchone()
    if row:
        names = json.loads(row["text_value"] or "[]")
        if names:
            cur.execute("DELETE FROM dashboard.colour_group_types WHERE text_type_name = ANY(%s)", (names,))
        cur.execute("DELETE FROM dashboard.settings WHERE text_key = %s", (SETTING_COLOUR_TYPES,))


def refresh_places(conn: psycopg.Connection) -> None:
    """The two lists the suggestion boxes are answered from - the addresses
    and the source hosts (dashboard/sql/03-places-view.sql). A plain refresh:
    it runs inside the transaction and a view may be empty, both of which
    CONCURRENTLY refuses."""
    for name in ("dashboard.places", "dashboard.source_hosts"):
        if conn.execute("SELECT to_regclass(%s) AS r", (name,)).fetchone()["r"] is not None:
            conn.execute(f"REFRESH MATERIALIZED VIEW {name}")


def load(dsn: str, now: datetime | None = None) -> dict[str, int]:
    """Write the preseed (removing an earlier one first). Returns row counts."""
    now = now or datetime.now(timezone.utc)
    with _connect(dsn) as conn:
        apply_dashboard_schema(conn)
        with conn.cursor() as cur:
            _remove_rows(cur)
            vocab: dict[str, set[tuple[str, str, str]]] = {t: set() for t in VOCAB_TABLES}
            for task in TASKS:
                for language in LANGUAGES[task["project"]]:
                    _write_task(cur, task, language, now, vocab)
            _write_vocabulary(cur, vocab)
            _write_dashboard_rows(cur)
            log_rows = _write_crawler_log(cur)
        refresh_places(conn)
        conn.commit()
        counts = {}
        for table in DATA_TABLES:
            counts[table] = conn.execute(
                f"SELECT count(*) AS n FROM processed_data.{table} WHERE text_project = ANY(%s)",
                (list(PROJECTS),)).fetchone()["n"]
        counts["crawler log rows"] = log_rows
    return counts


def remove(dsn: str) -> None:
    with _connect(dsn) as conn:
        with conn.cursor() as cur:
            _remove_rows(cur)
        refresh_places(conn)
        conn.commit()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--remove", action="store_true", help="take the preseed out again")
    args = parser.parse_args(argv)
    dsn = os.environ.get("DATABASE_URL", "").strip()
    if not dsn:
        print("DATABASE_URL is not set", file=sys.stderr)
        return 2
    if args.remove:
        remove(dsn)
        print("preseed removed")
        return 0
    counts = load(dsn)
    print("preseed loaded: " + ", ".join(f"{k} {v}" for k, v in counts.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
