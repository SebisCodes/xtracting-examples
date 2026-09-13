"""The chart catalogue: every chart of every Diagrams tab, by id.

The registry is the contract between the API, the drilldown and the tests:
`/api/diagrams/{scope}/{tab}` returns exactly the charts listed here for the
tab, in this order, with these ids and titles, and the drilldown refuses any
chart id or key shape that is not in it. The vocabulary guard
(tests/unit/test_vocabulary_guard.py) reads the titles from here too.

Three kinds of chart:

  time    grouped bars, one per bucket of the timeframe (sqlbuild.bucket)
  key     horizontal top-25 (sqlbuild.KEY_CHART_LIMIT), never unbounded
  matrix  a cell per pair on two category axes

A ChartSpec carries a `builder`: a function (scope, ctx, window) -> the
chart's statement and its datasets. The builders live in one module per tab
and register themselves with `register_builder`; a chart whose builder has
not been written yet still shows up in the catalogue, and the API answers
it with an empty chart and a note rather than a 500.

Two things every chart here also carries, and both of them are TEXT the page
would otherwise have to hold fifty-six copies of:

  hint    the sentence the ⓘ beside the title opens - what this chart
          counts, over what, and what one bar of it is. DATASET_HINTS below
          is the same idea for one entry of a legend.
  band    this chart is the tab's SUMMARY: drawn above the grid, full width,
          at the size its own data asks for rather than the grid's fixed
          one. One band per tab, and they are written first in each list
          because that is the order they are read in.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from psycopg import sql

from .. import sqlbuild

KINDS = ("time", "key", "matrix")

# What a click on a point carries back to the drilldown: `bucket` for a time
# chart, `x` for a key chart, `x` and `y` for a matrix. Some datasets add a
# second part (an importance level, a sign, an outlook value) that the
# builder names in the dataset id.
DRILL_KEYS = ("bucket", "x", "xy")

Builder = Callable[..., Any]


@dataclass
class ChartSpec:
    tab: str
    id: str
    kind: str
    title: str
    # One sentence for the aria-label and the hidden data table's caption.
    description: str = ""
    # WHAT THE ⓘ BESIDE THE TITLE SAYS: what this chart counts, over what,
    # and what ONE BAR of it is.
    #
    # It is a second sentence and not a longer `description` because the two
    # are read at different moments. The description is always on the card,
    # in two reserved lines, and has to stay short enough not to push the
    # plot down (css/diagrams.css); the hint is asked for, once, by somebody
    # who is not sure what they are looking at - and that person needs the
    # UNIT ("rating rows, not documents"), which is exactly the sentence
    # there was never room for.
    #
    # It lives here rather than in the templates because there are fifty-six
    # of these and one page: the catalogue carries it (as_dict below), and
    # static/js/charts.js builds the same hint button and bubble the forms
    # use (templates/_macros.html: help_icon / help_text). One place, not
    # fifty-six.
    hint: str = ""
    # WHAT ONE CATEGORY OF THIS CHART IS, in the plural: "entities",
    # "domains", "rating values". A key chart with more bars than its plot
    # has room for draws NO labels at all rather than a smear of overlapping
    # ones, and says so in one line under the title - "43 entities - open
    # the chart to see the names" (static/js/charts.js). That line cannot be
    # written without this word, and "43 categories" is not a sentence
    # anybody wants to read. Time charts leave it empty: their categories
    # are periods, and a time axis thins its ticks instead of hiding them.
    category: str = ""
    # Which key shape the drilldown expects for this chart.
    drill: str = "bucket"
    # Dataset ids and labels the builder produces, when they are fixed in
    # advance (levels, signs, outlooks). Free-text datasets (by domain, by
    # type) leave this empty and name theirs at run time.
    datasets: tuple[tuple[str, str], ...] = ()
    # True when the datasets PARTITION the rows: every row is counted in
    # exactly one of them, so the series may be added up. A chart marked
    # this way is drawn as one bar per period with the series stacked in it
    # (static/js/charts.js), which is what keeps a bar wide enough to read
    # and to hit: eight sentiments side by side over twelve months is a
    # 3 px hairline, well under the 24 px WCAG 2.5.8 asks of a target.
    #
    # It is FALSE wherever a dataset is a subset of another ("of those,
    # high relevance") or a different aggregate of the same rows (minimum,
    # average, maximum) - stacking those would draw a total that does not
    # exist.
    stacked: bool = False
    # THE SUMMARY BAND: this chart is drawn above the grid, at full width,
    # rather than as one card among the others.
    #
    # One band per tab, holding what a reader wants FIRST - the newest
    # sources, the most extreme ratings, the connection matrix. It is a
    # property of the CHART and not of the page, so the eight bands are
    # described in one place and the page only has to read the flag
    # (static/js/diagrams.js sorts the answer into the band and the grid).
    #
    # A band chart is an ordinary chart in every other respect: same
    # builder, same drilldown, same key shape, same tests. What is different
    # is its SIZE - the band computes the height from the number of
    # categories instead of taking the card's fixed one (static/js/charts.js),
    # because a summary nobody can read is not a summary.
    band: bool = False
    # HOW MUCH OF THE GRID THIS CHART TAKES, when the default is wrong.
    #
    #   ""      one cell, which is what almost everything wants
    #   "full"  the whole width of the grid, on a row of its own
    #   "half"  half of it, paired with the next "half" beside it
    #
    # The grid is `auto-fit` and gives each card at least 30rem, so on a wide
    # screen it can be three or four across - fine for a bar chart and wrong
    # for the two things this exists for. A matrix at a third of the width
    # draws its axis names over each other; a chart that IS the tab's subject
    # ("Market insights by topic") reads as one card among six when it should
    # open the tab. Neither can be expressed by ordering alone.
    #
    # "half" is a PAIR and not a fraction: static/js/diagrams.js puts two
    # consecutive halves in one row of their own, so each gets just under
    # half the grid whatever the window does, and on a narrow screen they
    # stack like everything else.
    width: str = ""
    # THE HEIGHT COMES FROM THE DATA, not from the card.
    #
    # A grid card keeps a fixed 17 rem for its picture so that two cards in a
    # row are the same size. That is right for a chart of eight bars and
    # wrong for one of forty: the rule that draws no label it cannot draw
    # properly then takes every name off, and the card says "40 entities -
    # open the chart to see the names" on the chart that exists to show them.
    #
    # `grow` says this chart is sized the way a summary band is: one row per
    # category, as tall as it needs to be, so every label is drawn. It is for
    # the full-width charts whose categories are free text out of the
    # archive - names, addresses, types - where "all of it, readable" is the
    # whole point of the picture.
    grow: bool = False
    # A TIME AXIS THAT WRITES ITS DATES UPRIGHT.
    #
    # A date axis THINS its labels when they do not fit - every second or
    # every fifth - which is right for a chart read as a shape and wrong for
    # one read as a series of periods: "which month was that bar" has no
    # answer when the month is one of the four that were dropped. Turned a
    # quarter of the way round, every date is drawn.
    upright_dates: bool = False
    # The SQL builder, attached by the module that owns the tab
    # (@register_builder below). Every chart in the registry has one, and
    # tests/unit/test_chart_registry.py is what keeps that true.
    builder: Builder | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(f"chart {self.tab}/{self.id}: unknown kind {self.kind!r}")
        if self.drill not in DRILL_KEYS:
            raise ValueError(f"chart {self.tab}/{self.id}: unknown drill key {self.drill!r}")
        if self.width not in ("", "full", "half"):
            raise ValueError(f"chart {self.tab}/{self.id}: unknown width {self.width!r}")

    @property
    def ready(self) -> bool:
        return self.builder is not None

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "kind": self.kind, "title": self.title,
                "description": self.description, "hint": self.hint,
                "category": self.category, "drill": self.drill,
                "stacked": self.stacked, "band": self.band, "width": self.width,
                "grow": self.grow, "upright_dates": self.upright_dates,
                "datasets": [{"id": i, "label": l, "hint": dataset_hint(self.tab, self, i)}
                             for i, l in self.datasets]}


# ── Vocabulary the datasets share ───────────────────────────
# Only the published wording (app/vocabulary.py); the guard scans this file.

# THE WHOLE RATING SCALE, AS DATASETS. The ids are the valence steps, which
# is what gives each step its colour (DATASET_COLOURS below) and what makes
# "Bad" on this tab the same red as "Slightly Negative" on Market Insights.
# The labels are the seeded words (02-vocabularies.sql), because those are
# what a reader sees in the archive; a value the vocabulary does not know has
# no step and is counted as "unset", grey, rather than dropped - the bars are
# a distribution and a distribution that quietly loses rows is not one.
_RATING_STEPS = (
    ("unset", "Not on the scale"),
    ("very_negative", "Egregious"), ("negative", "Very Bad"),
    ("slightly_negative", "Bad"), ("neutral", "Neutral"),
    ("slightly_positive", "Good"), ("positive", "Very Good"),
    ("very_positive", "Excellent"),
)

_SIGNS = (("negative", "Negative reporting"), ("neutral", "Neutral reporting"),
          ("positive", "Positive reporting"))
# ONE ORDER FOR EVERY SCALE ON EVERY CHART: absence first, then the bad end,
# then the middle, then the good end. A legend read top to bottom and a stack
# read left to right then say the same thing in the same direction, on the
# Ratings tab and on Market Insights alike - and "Unset" leads rather than
# trails, because it is not a step of the scale and a reader should meet it
# before the scale starts rather than find it appended past the best value.
_OUTLOOKS = (("unset", "Unset"), ("declining", "Declining"),
             ("neutral", "Neutral"), ("rising", "Rising"))
_SENTIMENTS = (("unset", "Unset"),
               ("very_negative", "Very Negative"), ("negative", "Negative"),
               ("slightly_negative", "Slightly Negative"), ("neutral", "Neutral"),
               ("slightly_positive", "Slightly Positive"), ("positive", "Positive"),
               ("very_positive", "Very Positive"))
_IMPORTANCE = (("not_important", "Not Important"), ("low", "Low Importance"),
               ("medium", "Medium Importance"), ("high", "High Importance"),
               ("critical", "Critical Importance"))
_TRUST = (("trustful", "Trustful"), ("not_trustful", "Not trustful"))

# The published wording of an ORDERED vocabulary, in the order of the scale
# itself. A matrix over one of these has to carry the scale onto its axes:
# "short-term sentiment against long-term sentiment" only answers its own
# question when both axes run Very Negative to Very Positive, because
# agreement is then the diagonal. Sorted by count, or in whatever order the
# archive returned, the bubbles sit in arbitrary places and mean nothing.
# Unset is not a step of the scale, so it goes last in the LIST - and
# nowhere near the good end of an AXIS (SENTIMENT_AXIS, below).
SENTIMENT_SCALE: tuple[str, ...] = tuple(label for _, label in _SENTIMENTS)
OUTLOOK_SCALE: tuple[str, ...] = tuple(label for _, label in _OUTLOOKS)

# One slot of an axis with no name and no bubbles: a divider, and the only
# thing a category axis can be given one with.
AXIS_GAP = " "

# The sentiment scale as it goes ON AN AXIS.
#
# "Unset" is not a step of the scale. Drawn at the end of the ramp it lands
# beyond Very Positive - at the top of the y axis and the far right of the
# x axis - so a market insight nobody gave a sentiment reads as one step
# better than the best there is. It goes FIRST instead, separated from the
# scale by an empty slot, where it can only be read as "off the scale":
# nothing is dropped from the picture, and the ramp itself still runs Very
# Negative to Very Positive with agreement on the diagonal.
SENTIMENT_AXIS: tuple[str, ...] = (
    ("Unset", AXIS_GAP) + tuple(l for l in SENTIMENT_SCALE if l != "Unset"))

# The outlook scale on an axis, by the same rule and for the same reason.
# ASCENDING, unlike the list: a legend reads best-first (Rising, Neutral,
# Declining) and an axis reads upwards, so the axis runs Declining to Rising
# and agreement between the two horizons is the diagonal, exactly as it is on
# the sentiment matrix. "Unset" is not a fourth step of it.
OUTLOOK_AXIS: tuple[str, ...] = (
    ("Unset", AXIS_GAP) + tuple(l for l in OUTLOOK_SCALE if l != "Unset"))

#: WHERE THE MAP GOES IN THE ORDER OF A TAB'S CHARTS.
#
# The map is not a summary - it is one of the pictures, and it
# belongs where the reader meets it: on Connections after the types (what the
# ties ARE, then where they run), on Locations after the addresses (which
# places, then where they cluster). Everything below it is read after it.
#
# The value is the id of the chart the map follows; a tab that is not here
# has no map. static/js/diagrams.js moves the card into the grid there.
MAP_AFTER: dict[str, str] = {
    "connections": "by_type",
    "locations": "by_address",
}

# Order of the tabs in the UI, and their display names.
TABS: tuple[tuple[str, str], ...] = (
    ("sources", "Sources"),
    ("ratings", "Ratings"),
    ("entities", "Entities"),
    ("connections", "Connections"),
    ("locations", "Locations"),
    ("market", "Market Insights"),
    ("attributes", "Attributes"),
    ("events", "Events"),
)
TAB_IDS = tuple(t for t, _ in TABS)
TAB_TITLES = dict(TABS)


def _c(tab: str, id: str, kind: str, title: str, description: str = "", hint: str = "",
       drill: str | None = None, datasets: tuple[tuple[str, str], ...] = (),
       stacked: bool = False, category: str = "", band: bool = False,
       width: str = "", grow: bool = False, upright_dates: bool = False) -> ChartSpec:
    if drill is None:
        drill = {"time": "bucket", "key": "x", "matrix": "xy"}[kind]
    return ChartSpec(tab, id, kind, title, description, hint, category, drill,
                     datasets, stacked, band, width, grow, upright_dates)


REGISTRY: dict[str, list[ChartSpec]] = {
    "sources": [
        _c("sources", "per_period", "time", "Sources per period",
           "How many sources were written in each period.",
           hint="Counts documents, one bar per period of the timeframe. A bar is how many "
                "documents this search reached that were written in that period - never how "
                "many were fetched.",
           datasets=(("sources", "Sources"),), width="half", upright_dates=True),
        _c("sources", "trust_per_period", "time", "Trust per period",
           "Sources per period, trustful or not.",
           hint="The same documents again, split by whether the extraction found the source "
                "trustful. A bar is a period and its two parts add up to the documents of "
                "that period.",
           datasets=_TRUST, stacked=True, width="half"),
        _c("sources", "importance_by_perspective", "key", "Importance by perspective",
           "For each perspective, how many sources at each importance level.",
           hint="Counts per-perspective judgements, not documents: one document judged for "
                "three perspectives is three rows here. A bar is one perspective and each "
                "band in it is how many documents it judged at that level.",
           datasets=_IMPORTANCE, stacked=True, category="perspectives", width="half"),
        _c("sources", "importance_per_period", "time", "Importance per period",
           "Sources per period, split by importance level.",
           hint="The same documents as the chart above, each counted once under the one "
                "importance the extraction gave it. A bar is a period and each band in it is "
                "one level, so the bands add up to the documents of that period.",
           datasets=_IMPORTANCE, stacked=True, width="half", upright_dates=True),
        _c("sources", "by_type", "key", "Sources by type", "Article types.",
           hint="Counts documents by the kind the extraction called them. One bar is one "
                "article type; a document the extraction left blank is counted under "
                "“(not stated)” rather than dropped.",
           category="source types", width="full"),
        _c("sources", "by_domain", "key", "Sources by domain", "The hosts the sources come from.",
           hint="Counts documents by the host of their address. One bar is one host, and its "
                "length is how many documents came from it in this period.",
           category="domains", width="full", grow=True),
    ],
    "ratings": [
        _c("ratings", "per_period", "time", "Ratings per period",
           "How many ratings were recorded in each period.",
           hint="Counts rating rows, one bar per period. A rating is one value on one named "
                "scale, given from one perspective, so a document rated on three scales "
                "contributes three.",
           datasets=(("ratings", "Ratings"),), width="half"),
        _c("ratings", "values_per_period", "time", "Rating values per period",
           "Negative, neutral and positive reporting per period, from the rating scale.",
           hint="The same ratings reduced to a sign by the number the archive carries for "
                "each value. A bar is a period and its three bands add up to the ratings of "
                "that period.",
           datasets=_SIGNS, stacked=True, width="half"),
        _c("ratings", "by_perspective", "key", "Ratings by perspective",
           "Negative, neutral and positive reporting per perspective.",
           hint="Counts ratings by the perspective they were given from. One bar is a "
                "perspective and its bands are negative, neutral and positive reporting.",
           datasets=_SIGNS, stacked=True, category="perspectives", width="half"),
        _c("ratings", "by_value", "key", "Ratings by value",
           "Ratings per value, in the order of the scale (Egregious to Excellent).",
           hint="Counts ratings per value of the scale. One bar is one value, and the bars "
                "are in the order of the scale rather than by size - read down, it is a "
                "distribution.",
           category="rating values", width="half"),
        _c("ratings", "by_domain", "key", "Ratings by domain",
           "The hosts the rated documents come from.",
           hint="Counts ratings by the host of the document they were written in - the "
                "rating's own row has no address, so its document is looked up first. One "
                "bar is one host.",
           category="domains", width="full", grow=True),
        _c("ratings", "by_name_and_value", "key", "Ratings by name and value",
           "Every rating name, split into the steps of the scale.",
           hint="Counts ratings per name, and splits each bar into the values that were "
                "given on it - Egregious through Excellent, in the order of the scale. "
                "One bar is one rating name and the bands in it add up to every rating "
                "recorded on it in this period.",
           datasets=_RATING_STEPS, stacked=True, category="rating names",
           band=False, width="full", grow=True),
    ],
    "entities": [
        _c("entities", "per_period", "time", "Entities per period",
           "Entity rows and distinct entities per period.",
           hint="Two numbers that are easy to confuse: how many entity ROWS were written in "
                "a period, and how many different entities those rows are. Six documents "
                "naming Apple are six rows and one entity, so the two lines are never added "
                "together.",
           datasets=(("rows", "Entity mentions"), ("distinct", "Distinct entities")), width="full"),
        _c("entities", "by_type", "key", "Entities by type",
           "The kinds of entity that were mentioned.",
           hint="Counts mentions per kind of entity. One bar is one entity type, so an "
                "entity named ten times adds ten to its type.",
           category="entity types", width="full", grow=True),
        _c("entities", "most_named", "key", "The entities most often named",
           "The entities with the most mentions, each with the kind of thing it is.",
           hint="Counts mentions per entity. One bar is one entity, labelled with its name "
                "and the type the extraction gave it, and its length is how often it was "
                "named in this period.",
           category="entities", band=False, width="full", grow=True),
    ],
    "connections": [
        _c("connections", "per_period", "time", "Connections per period",
           "How many connections were recorded in each period.",
           hint="Counts connection rows, one bar per period. A connection is counted once, "
                "from the parent to the child, in the direction the extraction wrote it.",
           datasets=(("connections", "Connections"),), width="half"),
        _c("connections", "by_group", "key", "Connections by colour group",
           "Connections per colour group, in the group's colour.",
           hint="Counts connections by the colour group their type belongs to, in that "
                "group's own colour. One bar is one group; a type nobody has grouped falls "
                "into the fallback group.",
           category="colour groups", width="half"),
        _c("connections", "by_type", "key", "Connections by type",
           "What kind of tie was recorded between two entities.",
           hint="Counts connections by the word the extraction used for the tie, read from "
                "parent to child. One bar is one connection type.",
           category="connection types", width="full", grow=True),
        # The sentence depends on what the picture actually is, so the
        # builder overrides it per scope (charts/connections.py); this one
        # is the summary's, which is what the catalogue lists.
        _c("connections", "matrix", "matrix", "Connection matrix",
           "Which entity is the parent, which is the child, and how often.",
           hint="Counts connections per pair. One cell is a pair of entities, and the "
                "darker it is the more connections were recorded between them in this "
                "period; the number is on hover.",
           # IN THE SUMMARY, DIRECTLY ABOVE THE MAP, AND ACROSS IT. The matrix
           # and the map are the same answer at two scales - which entities are
           # tied together, and where those ties are - so they are read one
           # after the other rather than a screen apart. Full width because a
           # matrix at a third of the grid draws its axis names over each
           # other: both of its axes are entity names.
           category="entities", band=False,
           width="full", grow=True),
    ],
    "locations": [
        _c("locations", "per_period", "time", "Locations per period",
           "How many locations were recorded in each period.",
           hint="Counts location rows, one bar per period. A row is one place named in one "
                "document, so the same office named in four documents is four.",
           datasets=(("locations", "Locations"),), width="full"),
        _c("locations", "by_country", "key", "Locations by country",
           "The countries the locations are in.",
           hint="Counts location rows by the last part of the address. One bar is one "
                "country; a one-part address has none and is counted under "
                "“(not stated)” so the bars still add up.",
           category="countries", width="half"),
        _c("locations", "by_city", "key", "Locations by city",
           "The cities the locations are in.",
           hint="Counts location rows by the FIRST part of the address, which is where this "
                "archive writes the city - and where an address that carries a street "
                "writes the street. One bar is one such part.",
           category="cities", width="half"),
        _c("locations", "by_address", "key", "Locations by address",
           "The addresses, as the archive spells them.",
           hint="Counts location rows by the whole address, exactly as the archive spells "
                "it. One bar is one spelling, so two spellings of one office are two bars.",
           category="addresses", width="full", grow=True),
        _c("locations", "by_type", "key", "Locations by type",
           "The kinds of place that were named.",
           hint="Counts location rows by the kind of place the extraction called it - a head "
                "office, a plant, a court. One bar is one place type.",
           category="location types", width="full", grow=True),
        _c("locations", "most_named", "key", "The places most often named",
           "The places with the most rows, grouped as coarsely as still tells them apart.",
           hint="Counts location rows per place. The addresses are grouped at the coarsest "
                "part that still tells them apart - the city rather than each street where "
                "several streets share one city - so one bar is one place at whichever of "
                "those levels the answer is drawn at.",
           # NOT A BAND CHART: this tab's summary is the heat field at
           # the foot of the page, which is what the Heatmap view draws and
           # what "where is there most of this" actually asks for.
           category="places",
           band=False, width="full", grow=True),
    ],
    "market": [
        _c("market", "per_period", "time", "Market insights per period",
           "All market insights, and those marked as high relevance.",
           hint="Counts market insights, one bar per period, with the high-relevance ones "
                "drawn beside them rather than on top: a high-relevance insight IS one of "
                "the insights, so the two lines are not added together.",
           # "LOW RELEVANCE", NOT "MARKET INSIGHTS". The pale bar is every
           # insight and the dark one is the subset the archive marked as
           # high relevance - so beside each other the pale one reads as the
           # rest, which is what it is.
           datasets=(("insights", "Low relevance"), ("high_relevance", "High relevance")),
           width="full"),
        _c("market", "short_outlook_per_period", "time", "Short-term outlook per period",
           "Which direction the reporting pointed over the short term, per period.",
           hint="Counts market insights by the direction a source reported over the short "
                "term. A bar is a period and its bands add up to the insights of that "
                "period. It describes what was reported, never what anybody should do.",
           datasets=_OUTLOOKS, stacked=True, width="half"),
        _c("market", "short_sentiment_per_period", "time", "Short-term sentiment per period",
           "How the reporting read, over the short term.",
           hint="Counts market insights by how strongly a source read over the short term, "
                "from Very Negative to Very Positive. A bar is a period; an insight with no "
                "sentiment is counted as Unset rather than dropped.",
           datasets=_SENTIMENTS, stacked=True, width="half"),
        _c("market", "long_outlook_per_period", "time", "Long-term outlook per period",
           "Which direction the reporting pointed over the long term, per period.",
           hint="The same insights read over the long term instead. A bar is a period and "
                "its bands add up to the insights of that period.",
           datasets=_OUTLOOKS, stacked=True, width="half"),
        _c("market", "long_sentiment_per_period", "time", "Long-term sentiment per period",
           "How the reporting read, over the long term.",
           hint="The same insights read over the long term instead. A bar is a period, and "
                "the bands are the same seven steps plus Unset.",
           datasets=_SENTIMENTS, stacked=True, width="half"),
        _c("market", "outlook_matrix", "matrix", "Outlook matrix",
           "Short-term outlook against long-term outlook.",
           hint="Counts market insights per pair of directions. One cell is a short-term "
                "outlook against a long-term one, and the darker it is the more insights "
                "read that way; both axes run in the order of the scale, so agreement is "
                "the diagonal.",
           category="outlooks", width="half", grow=True),
        _c("market", "sentiment_matrix", "matrix", "Sentiment matrix",
           "Short-term sentiment against long-term sentiment.",
           hint="Counts market insights per pair of readings. One bubble is a short-term "
                "reading against a long-term one, and the darker it is the more insights "
                "read that way; both axes run in the order of the scale, so agreement is "
                "the diagonal.",
           category="sentiments", width="half", grow=True),
        _c("market", "by_topic", "key", "Market insights by topic",
           "The topics the market insights are about.",
           hint="Counts market insights by the topic they are about. One bar is one topic; "
                "topics are translated, so this is the wording of the language above.",
           category="topics",
           # THE SUBJECT OF THIS TAB, on the first row and across it. A topic
           # is what a market insight is ABOUT, so the list of them answers
           # "what is being reported on" - and at a third of the grid it read
           # as one card among six.
           band=False, width="full", grow=True),
        _c("market", "by_entity", "key", "Market insights by entity",
           "Which entities the reporting is about.",
           hint="Counts market insights per entity. The topic says what kind of thing is "
                "being reported on; this says whose. One bar is one entity, and its "
                "length is how many insights name it in this period.",
           category="entities", width="full", grow=True),
    ],
    "attributes": [
        _c("attributes", "per_period", "time", "Attributes per period",
           "How many attributes were recorded in each period.",
           hint="Counts attribute rows, one bar per period. An attribute is a name, a value "
                "and a unit read off one document, so one entity described twice is two.",
           datasets=(("attributes", "Attributes"),), width="full"),
        _c("attributes", "by_type", "key", "Attributes by type",
           "The kinds of attribute that were recorded.",
           hint="Counts attribute rows by the kind the extraction called them. One bar is "
                "one attribute type.",
           category="attribute types", width="full", grow=True),
        _c("attributes", "by_name", "key", "Attributes by name",
           "The names the attributes were given.",
           hint="Counts attribute rows by the name they were given. One bar is one name, "
                "whatever value or unit it carried.",
           category="attribute names", width="full", grow=True),
        _c("attributes", "by_unit", "key", "Attributes by unit",
           "The units the values are measured in.",
           hint="Counts attribute rows by the unit their value is measured in. One bar is "
                "one unit; an attribute that is ordinary and has none is counted under "
                "“(no unit)”.",
           category="units",
           band=False, width="full", grow=True),
    ],
    "events": [
        _c("events", "per_period", "time", "Events per period",
           "Events per period, placed on the event date.",
           hint="Counts events, one bar per period, placed on the date the event HAPPENS - "
                "which for an announced hearing is in the future, later than the document "
                "that reported it.",
           datasets=(("events", "Events"),), width="half"),
        _c("events", "about_document_vs_entities", "time", "About source or entity per period",
           "Events that name entities against events that are about the document itself.",
           hint="Counts events, one bar per period, split by whether the archive carries an "
                "entity for the event. An event with none is not incomplete - it is about "
                "the document as a whole - so the two parts add up to the events of that "
                "period.",
           datasets=(("about_entities", "About entities"), ("about_document", "About the document")),
           stacked=True, width="half"),
        _c("events", "type_matrix", "matrix", "Event types by entity",
           "Which kind of event names which entity.",
           hint="Counts (event, entity) pairs per pair of axes. One bubble is a kind of "
                "event against an entity it named, and its area is how many events that "
                "was.",
           category="entities",
           band=False, width="full", grow=True),
        _c("events", "by_type", "key", "Events by type",
           "The kinds of event that were recorded.",
           hint="Counts events by the kind the extraction called them. One bar is one event "
                "type; type names are translated, so this is the wording of the language "
                "above.",
           category="event types", width="full", grow=True),
        _c("events", "by_entity", "key", "Events by entity", "The entities events are about.",
           hint="Counts (event, entity) pairs, not events: one event naming three entities "
                "adds one to each of three bars. One bar is one entity, and an event about "
                "the document itself is in none of them.",
           category="entities", width="full", grow=True),
    ],
}


class UnknownChart(KeyError):
    pass


def charts_for(tab: str) -> list[ChartSpec]:
    try:
        return REGISTRY[tab]
    except KeyError:
        raise UnknownChart(f"unknown tab {tab!r}; one of {', '.join(TAB_IDS)}") from None


def get_chart(tab: str, chart_id: str) -> ChartSpec:
    for spec in charts_for(tab):
        if spec.id == chart_id:
            return spec
    raise UnknownChart(f"tab {tab!r} has no chart {chart_id!r}")


def all_charts() -> list[ChartSpec]:
    return [spec for tab in TAB_IDS for spec in REGISTRY[tab]]


def register_builder(tab: str, chart_id: str) -> Callable[[Builder], Builder]:
    """Decorator for a builder module:

        @register_builder("sources", "per_period")
        def per_period(scope, ctx, window): ...
    """
    spec = get_chart(tab, chart_id)

    def wrap(fn: Builder) -> Builder:
        spec.builder = fn
        return fn
    return wrap


def catalogue() -> dict[str, Any]:
    """The whole registry as JSON: what /api/diagrams/catalogue answers."""
    return {"tabs": [{"id": t, "title": TAB_TITLES[t], "charts": [c.as_dict() for c in REGISTRY[t]]}
                     for t in TAB_IDS]}


# ── From the archive's wording to a dataset id ───────────────
#
# The archive stores the published vocabulary ("Critical Importance"); a
# dataset id is a slug ("critical"). The mapping is here rather than in the
# tab modules because the chart and the drilldown must agree on it, and
# charts/drilldown.py turns each of these into one CASE expression that
# yields the dataset id itself - so drilling into a dataset is a comparison
# against that same expression.
#
# A value the map does not know becomes UNSTATED. It is not declared as a
# dataset: it appears in the answer only when the archive actually holds
# such a row, and then it is labelled rather than silently counted as
# something else.

UNSTATED = "unstated"

IMPORTANCE_VALUES: dict[str, str] = {
    "Not Important": "not_important", "Low Importance": "low", "Medium Importance": "medium",
    "High Importance": "high", "Critical Importance": "critical"}
OUTLOOK_VALUES: dict[str, str] = {"Rising": "rising", "Neutral": "neutral",
                                  "Declining": "declining", "Unset": "unset"}
SENTIMENT_VALUES: dict[str, str] = {
    "Very Negative": "very_negative", "Negative": "negative",
    "Slightly Negative": "slightly_negative", "Neutral": "neutral",
    "Slightly Positive": "slightly_positive", "Positive": "positive",
    "Very Positive": "very_positive", "Unset": "unset"}

# Labels for dataset ids that are not declared on a chart: the ones a row
# with an empty column falls into.
EXTRA_LABELS: dict[str, str] = {UNSTATED: "Not stated"}

# What one row of a tab is called, for the chart that has a single
# undivided dataset ("Sources per period" has one line, and it is sources).
TAB_ROW_LABELS: dict[str, str] = {
    "sources": "Sources", "ratings": "Ratings", "entities": "Entity mentions",
    "connections": "Connections", "locations": "Locations",
    "market": "Market insights", "attributes": "Attributes", "events": "Events",
}


# ── Colours ──────────────────────────────────────────────────
#
# COLOUR MEANS ONE THING, AND THE MEANING IS IN THE STYLESHEET.
#
# app/static/css/app.css defines three data scales as custom properties -
# valence (diverging red to green), relevance (sequential violet) and count
# (blue) - plus --no-data, a grey that means absence and nothing else. Those
# tokens are the design system, they are measured there (3:1 against both
# surfaces, 4.5:1 for text on a filled mark, monotonic luminance per ramp),
# and js/palette.js publishes the same values to the browser.
#
# This module READS THEM OUT OF THAT FILE rather than keeping a second copy.
# A second copy is exactly how one meaning becomes two colours: the ramp is
# adjusted in the stylesheet, the API keeps sending last month's hexes, and
# a bar and its legend swatch disagree on the same card. The parse is of our
# own file, at import, and _token() fails loudly on a name the stylesheet
# does not define - a missing token is a broken build, not a black bar.
#
# WHICH SCALE A DATASET IS ON is a fact about the DATA, and it is decided
# here because the archive's vocabulary is what decides it:
#
#   valence     a reading that can be good or bad. Rating values and the
#               three signs built from them, sentiments, outlooks. Negative
#               is red, positive is green, the step grows with the distance
#               from neutral, and NEUTRAL IS THE PALEST STEP - not grey.
#   relevance   how much something matters: the five importance levels and
#               `bool_high_relevance`. One hue, violet, stronger with more
#               relevance. Never red or green - relevance is not good or bad.
#   count       a plain magnitude. One blue for a single series; the ramp,
#               lightest first, where one count is split into ORDERED PARTS
#               (three relations to the document, two kinds of trust,
#               minimum/average/maximum).
#   --no-data   absence: Unset, and the UNSTATED bucket a row with an empty
#               column falls into. It is not a step of any scale, which is
#               also why Unset goes off the END of a sentiment axis
#               (SENTIMENT_AXIS above) instead of one step past Very
#               Positive.
#
# THE ONE CAVEAT, SAID ONCE. Red and green on market data read as buy and
# sell to anyone trained in finance, and this product refuses to give that
# impression. The colour is allowed to carry the valence - it is the
# clearest encoding there is, and a dashboard whose ratings were orange and
# blue made people read the legend instead of the chart - but then NOTHING
# ELSE may add to it: the wording stays as the archive publishes it
# ("Negative reporting", never "Sell"), the Market Insights views keep the
# sentence saying this describes what a source reported rather than advice,
# and there are no arrows, triangles or trend glyphs anywhere on those
# charts. app/vocabulary.py holds both halves of that rule and
# tests/unit/test_vocabulary_guard.py enforces them.
#
# ACCESSIBILITY. Hue is never the only carrier: every valence chart keeps
# its axis labels, its value text, its ordered position and a tooltip that
# names the value in words. The two ends of the valence ramp differ in
# LUMINANCE as well as hue (a red and a green of one lightness are the same
# grey to a dichromat and on a black-and-white printer), and every step
# clears 3:1 on white. tests/unit/test_palette.py measures the tokens;
# tests/unit/test_chart_registry.py measures the mapping below.

_APP_CSS = Path(__file__).resolve().parents[1] / "static" / "css" / "app.css"
# `--name: #rrggbb;` at the start of a line - the shape every token in the
# :root block is written in. A token defined as var(--other) is deliberately
# not followed: this map wants the literal a chart is painted with.
_CSS_TOKEN = re.compile(r"^\s*(--[a-z0-9-]+)\s*:\s*(#[0-9a-fA-F]{6})\s*;", re.M)


def _read_tokens() -> dict[str, str]:
    try:
        source = _APP_CSS.read_text(encoding="utf-8")
    except OSError:  # pragma: no cover - a stripped deployment
        return {}
    return {name: value.lower() for name, value in _CSS_TOKEN.findall(source)}


TOKENS: dict[str, str] = _read_tokens()


def _token(name: str) -> str:
    """One colour out of the stylesheet, or a loud failure.

    Not a fallback hex: a fallback is the second copy this whole section
    exists to avoid, and it would paint a chart in a colour that is in no
    stylesheet and no test.
    """
    try:
        return TOKENS[name]
    except KeyError:
        raise KeyError(f"{name} is not defined in {_APP_CSS.name}; "
                       "the chart colours are read from the design system") from None


# The three scales, in the order a legend reads them.
VALENCE: tuple[str, ...] = tuple(_token(f"--valence-{step}") for step in
                                 ("neg-3", "neg-2", "neg-1", "0", "pos-1", "pos-2", "pos-3"))
RELEVANCE: tuple[str, ...] = tuple(_token(f"--relevance-{i}") for i in range(5))
COUNT: tuple[str, ...] = tuple(_token(f"--count-{i}") for i in range(1, 7))
NO_DATA: str = _token("--no-data")
# The single-series blue, which is also the page's own accent, so a bar and
# a link are one colour.
COUNT_SINGLE: str = _token("--count-4")


def valence(step: int) -> str:
    """-3 … +3 on the diverging scale. The argument is the NUMBER the archive
    carries (`rating_values.float_value`), not the label."""
    return VALENCE[max(0, min(6, int(step) + 3))]


def relevance(level: int) -> str:
    """0 … 4 on the violet ramp."""
    return RELEVANCE[max(0, min(4, int(level)))]


# HOW ORDERED PARTS OF ONE COUNT ARE SPREAD OVER THE BLUE RAMP.
#
# Lightest first, using the whole ramp however many parts there are, so two
# parts are its two ends and six are every step. The indices are written out
# rather than computed because the middle of a three-part split is chosen
# for the widest luminance gaps the ramp can give (the ramp spans 3.77:1 end
# to end, so three parts cannot each be 1.8:1 apart - see
# tests/unit/test_chart_registry.py, which measures it and says so).
_RAMP_STEPS: dict[int, tuple[int, ...]] = {
    1: (3,), 2: (0, 5), 3: (0, 2, 5), 4: (0, 1, 3, 5), 5: (0, 1, 2, 4, 5),
}


def count(index: int = 0, of: int = 1) -> str:
    """The blue. `count()` is the single series; `count(i, n)` is part i of n
    ordered parts, lightest first."""
    parts = max(1, int(of))
    i = max(0, int(index))
    if parts == 1:
        return COUNT_SINGLE
    steps = _RAMP_STEPS.get(parts)
    return COUNT[steps[min(i, parts - 1)]] if steps else COUNT[i % len(COUNT)]


def _parts(ids: tuple[str, ...]) -> dict[str, str]:
    """Ordered parts of one count, in declared order, lightest first."""
    return {name: count(i, len(ids)) for i, name in enumerate(ids)}


# Free-text categories - domains, types, names, topics. Not a scale: these
# are things the archive happened to hold, in no order, so they are cycled
# by position and stay as they were. Every one is at least 3:1 on white.
#
# AND NOT ONE OF THEM IS ON A RESERVED HUE. Red-to-green is valence, violet
# is relevance, blue is a count and grey is --no-data; a free-text category
# is none of those, so it may not wear their colours. This list did: its
# first entry WAS --count-4, the same hex, 1.00:1 - a domain and a row
# count painted identically - and the rest sat on the red, the green, the
# violet and a grey. They are teal, brown, magenta, olive and rose now, in
# that cycling order so two neighbours differ in hue as well as lightness.
# tests/unit/test_categorical_palette.py measures it.
PALETTE: tuple[str, ...] = (
    "#12787D", "#3B470A", "#98168B", "#98164E", "#0B494C", "#5C7010", "#620E5A", "#A63063",
)

DATASET_COLOURS: dict[str, str] = {
    # ── Valence: the rating scale, and everything built on it ──────────
    # The seven sentiments ARE the seven steps. The three signs a rating is
    # reduced to (negative / neutral / positive reporting) share the ids
    # with the sentiments of the same name, so one meaning keeps one colour
    # across the Ratings and the Market Insights tabs.
    "very_negative": valence(-3), "negative": valence(-2), "slightly_negative": valence(-1),
    "neutral": valence(0),
    "slightly_positive": valence(1), "positive": valence(2), "very_positive": valence(3),
    # Outlook is the same scale with three steps used of it: a direction the
    # reporting pointed, not a verdict, so it takes ±2 rather than the ends.
    "declining": valence(-2), "rising": valence(2),
    # ── Absence, which is not a reading ────────────────────────────────
    "unset": NO_DATA, UNSTATED: NO_DATA,
    # ── Relevance: one hue, five steps ─────────────────────────────────
    "not_important": relevance(0), "low": relevance(1), "medium": relevance(2),
    "high": relevance(3), "critical": relevance(4),
    # `bool_high_relevance` is the yes half of a yes/no, so it takes the top
    # step of the same ramp: "high relevance" means the same thing on the
    # Market tab as "Critical Importance" does on the Sources tab.
    "high_relevance": relevance(4),
    # ── Counts split into ordered parts ────────────────────────────────
    # TRUSTFUL IS GREEN AND NOT TRUSTFUL IS RED, at the gentlest step of the
    # valence ramp.
    #
    # This was two shades of the count blue, on the reasoning that red and
    # green were spent on ratings and market insights and that a trust flag
    # is a split of a count rather than a reading on a scale of good and
    # bad. That reasoning was overruled, and rightly: "trustful" IS a
    # verdict, and the reader who has just seen a green bar on the Ratings
    # tab looks for the same meaning here.
    #
    # NOT THE SAME STEP ON BOTH SIDES, and that is measured rather than
    # chosen. Two datasets are drawn SIDE BY SIDE, not stacked (charts.js:
    # stacksSeries - stacking is worth it from three series up), so these
    # are two 13 px bars beside each other at 1024 px. At ±1 they would be
    # #258442 and #ba412f: 1.15:1 apart in lightness, which is hue and
    # nothing else - the red-green pair, in the one place where hue is the
    # only cue left. tests/unit/test_chart_registry.py measures it.
    #
    # So: the lightest green of the ramp, and the red one step deeper.
    # 1.82:1, over the 1.8 floor, and still plainly green against red.
    "trustful": valence(1), "not_trustful": valence(-2),
    # All entity mentions, and the distinct entities inside them.
    **_parts(("rows", "distinct")),
    # Three aggregates of one numeric column.
    **_parts(("min", "avg", "max")),
    # Events that name entities, against events about the document itself.
    **_parts(("about_entities", "about_document")),
    # All market insights, and - drawn beside them - those the archive marked
    # as high relevance. ONE HUE, TWO STEPS: the whole takes the palest step
    # of the same violet ramp its subset is drawn in, so the two read as
    # "more of this" and "less of this" rather than as two different things.
    # Not the count blue against a violet subset, which is two hues for
    # one quantity - and the legend says "Low relevance", which is what
    # the pale bar actually means beside the dark one.
    "insights": relevance(0),
    # ── Plain counts: one blue ─────────────────────────────────────────
    "count": COUNT_SINGLE, "sources": COUNT_SINGLE, "ratings": COUNT_SINGLE,
    "connections": COUNT_SINGLE, "locations": COUNT_SINGLE, "attributes": COUNT_SINGLE,
    "events": COUNT_SINGLE,
}

DEFAULT_DATASET_ID = "count"


def colour_for(dataset_id: str, index: int = 0) -> str:
    """The colour of a dataset: its own if the vocabulary knows it, else the
    next one in the palette, so two free-text datasets never coincide."""
    return DATASET_COLOURS.get(dataset_id) or PALETTE[index % len(PALETTE)]


def dataset_label(tab: str, spec: "ChartSpec", dataset_id: str) -> str:
    """What a dataset is called: the registry first, then the wording for the
    ids that are not declared, then the id itself made readable."""
    for i, label in spec.datasets:
        if i == dataset_id:
            return label
    if dataset_id in EXTRA_LABELS:
        return EXTRA_LABELS[dataset_id]
    if dataset_id == DEFAULT_DATASET_ID:
        return TAB_ROW_LABELS.get(tab, "Rows")
    return dataset_id.replace("_", " ").capitalize()


# ── What one entry of a legend means ─────────────────────────
#
# A key of six violet bands says WHICH band is which and nothing about what
# any of them IS. "Low Importance" is a phrase from a vocabulary the reader
# has never been shown, "Direct" is a relation to something the card does
# not name, and "Minimum" is not a count at all although every other bar on
# the page is. So every legend row carries its own ⓘ, with one sentence
# saying what that colour is (static/js/legend.js draws it; the bubble is a
# real element, never a `title=`, for the reason templates/_macros.html
# gives).
#
# KEYED BY THE DATASET ID, because that is the one thing a series keeps
# across every chart it appears on: "negative" is the same red and the same
# meaning on the Ratings tab and on Market Insights, and it may not be
# explained twice in two different ways. The ids are a closed set - the
# vocabulary maps above produce them and nothing else does - so a sentence
# here reaches every chart that uses it.
DATASET_HINTS: dict[str, str] = {
    # The rating scale reduced to a sign, and the seven steps themselves.
    "negative": "Reporting that reads worse than neutral.",
    "neutral": "Reporting that reads neither better nor worse than neutral.",
    "positive": "Reporting that reads better than neutral.",
    "very_negative": "The worst step of the scale the archive publishes.",
    "slightly_negative": "One step below neutral.",
    "slightly_positive": "One step above neutral.",
    "very_positive": "The best step of the scale the archive publishes.",
    # Outlook: a direction a source reported, not a verdict and not advice.
    "rising": "A source reported the subject going up over this horizon.",
    "declining": "A source reported the subject going down over this horizon.",
    # Absence, which is not a reading.
    "unset": "The archive carries no reading here, so this is not a step of "
             "the scale at all - it is drawn grey for that reason.",
    UNSTATED: "The extraction left this column empty. Counted under its own "
              "name rather than folded into another value, because a bar "
              "that is silently missing is a lie about the total.",
    # Relevance: how much something matters, never whether it is good.
    "not_important": "The extraction judged the document of no importance here.",
    "low": "The lowest of the five importance levels the archive publishes.",
    "medium": "The middle of the five importance levels.",
    "high": "The second-highest of the five importance levels.",
    "critical": "The highest importance the archive publishes.",
    "high_relevance": "The archive marked this insight as highly relevant. It "
                      "is one of the insights beside it, not an extra one.",
    # Trust.
    "trustful": "The extraction found the document trustful.",
    "not_trustful": "The extraction did not find the document trustful.",
    # Two aggregates of one column, and three of another.
    "rows": "Every entity mention, including the same entity named twice.",
    "distinct": "How many DIFFERENT entities those mentions are. It is a "
                "subset of the line beside it, so the two are never added up.",
    "min": "The smallest number read for this type - not a count of rows.",
    "avg": "The mean of the numbers read for this type - not a count of rows.",
    "max": "The largest number read for this type - not a count of rows.",
    # Events with and without entities of their own.
    "about_entities": "Events the archive names at least one entity for.",
    "about_document": "Events with no entity of their own: they are about the "
                      "document as a whole, which is not the same as being "
                      "incomplete.",
    "insights": "Every market insight in this period.",
}


def dataset_hint(tab: str, spec: "ChartSpec", dataset_id: str) -> str:
    """The sentence beside one legend entry, or "" where there is nothing to
    add - a single undivided series is already named by the chart itself."""
    return DATASET_HINTS.get(dataset_id, "")


def default_dataset(spec: "ChartSpec") -> tuple[str, str]:
    """The one dataset of a chart that is not split: the declared one when
    there is exactly one, otherwise a plain count."""
    if len(spec.datasets) == 1:
        return spec.datasets[0]
    return DEFAULT_DATASET_ID, TAB_ROW_LABELS.get(spec.tab, "Rows")


def declared_ids(spec: "ChartSpec") -> tuple[str, ...]:
    return tuple(i for i, _ in spec.datasets)


# The builders register themselves by importing their module. Last, so that
# everything above exists by the time a tab module imports it - and inside a
# function-free block, so a broken tab module fails loudly at start-up
# rather than silently leaving its charts unbuilt.
from . import (  # noqa: E402,F401
    attributes, connections, entities, events, locations, market, ratings, sources,
)
