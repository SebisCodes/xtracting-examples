"""The chart catalogue is a contract, and this is the contract test.

    python -m pytest tests/unit/test_chart_registry.py -q

app/charts/__init__.py is what the API answers with, what the drilldown
validates against, what the vocabulary guard reads and what the page's tab
strip is written from. Four places that can only stay in step if the
registry is checked as a whole:

  * every chart has a builder - a tab with a hole in it is a card that can
    only say it has nothing to draw;
  * every kind and key shape is one the drawing code knows;
  * every dataset id has a colour, and every colour is visible on white;
  * the tab strip in templates/diagrams.html is exactly this list.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app import charts, colours, vocabulary

TEMPLATE = Path(__file__).resolve().parents[2] / "app" / "templates" / "diagrams.html"
APP_JS = Path(__file__).resolve().parents[2] / "app" / "static" / "js"

ALL = charts.all_charts()
IDS = [f"{s.tab}.{s.id}" for s in ALL]


def test_the_registry_holds_every_tab_of_the_plan():
    assert charts.TAB_IDS == ("sources", "ratings", "entities", "connections",
                              "locations", "market", "attributes", "events")
    assert charts.TAB_TITLES["market"] == vocabulary.MARKET_INSIGHTS_TITLE


@pytest.mark.parametrize("spec", ALL, ids=IDS)
def test_every_chart_has_a_builder(spec):
    assert spec.ready, f"{spec.tab}/{spec.id} has no builder"
    assert callable(spec.builder)


@pytest.mark.parametrize("spec", ALL, ids=IDS)
def test_kinds_and_key_shapes_are_ones_the_drawing_code_knows(spec):
    assert spec.kind in charts.KINDS
    assert spec.drill in charts.DRILL_KEYS
    assert spec.title and spec.title[0].isupper()


@pytest.mark.parametrize("spec", ALL, ids=IDS)
def test_every_chart_says_in_a_sentence_what_it_is_for(spec):
    """A title is a name, not an explanation.

    "Relation to source per period" appears on five tabs with a
    Direct/Indirect/Unrelated legend and nothing saying what the relation is
    between; "Short-term outlook per period" and "Event types by entity" are
    just as opaque to somebody meeting them for the first time. Twenty-six
    of these cards jumped from the title straight to the plot, so the card
    also had a different height from its neighbour in the same row.
    """
    assert spec.description, f"{spec.tab}/{spec.id} has no purpose sentence"
    assert spec.description[0].isupper(), spec.description
    assert spec.description.endswith("."), spec.description
    # A sentence, not a second title: it says something the title does not.
    assert spec.description.lower() != spec.title.lower()


# The three charts whose datasets are NOT a partition of the rows: two
# where one series is a subset of another ("of those, high relevance",
# "of those, distinct") and one where the series are three aggregates of
# the same rows. Stacking any of them would draw a total that does not
# exist, so they are named here rather than left to a rule of thumb.
NOT_A_PARTITION = {("market", "per_period"), ("entities", "per_period"),
                   ("attributes", "value_stats")}


@pytest.mark.parametrize("spec", ALL, ids=IDS)
def test_only_the_charts_whose_series_add_up_are_stacked(spec):
    """More than two series side by side is a 3 px bar nobody can hit.

    Stacking is the answer, and it is only allowed where the series
    partition the rows - so the flag is checked against the two things that
    make a partition: it declares its datasets, and it is not one of the
    charts whose series overlap.
    """
    if spec.stacked:
        assert (spec.tab, spec.id) not in NOT_A_PARTITION, "these series overlap"
        assert len(spec.datasets) >= 2, "a partition of one is not a partition"
        assert spec.kind in ("time", "key"), "only bars stack"
    elif len(spec.datasets) > 2:
        # Three or more declared datasets and not stacked: only the charts
        # named above may do that, and they must say why in this list.
        assert (spec.tab, spec.id) in NOT_A_PARTITION, \
            f"{spec.tab}/{spec.id} draws {len(spec.datasets)} series side by side"


def test_the_catalogue_tells_the_page_which_charts_stack():
    payload = charts.get_chart("market", "short_sentiment_per_period").as_dict()
    assert payload["stacked"] is True
    assert charts.get_chart("market", "per_period").as_dict()["stacked"] is False
    # And the whole catalogue carries it, because static/js/charts.js reads
    # it from there.
    every = [c for tab in charts.catalogue()["tabs"] for c in tab["charts"]]
    assert all("stacked" in c for c in every)
    assert sum(1 for c in every if c["stacked"]) == sum(1 for s in ALL if s.stacked)


def test_chart_ids_are_unique_within_a_tab():
    for tab in charts.TAB_IDS:
        ids = [c.id for c in charts.charts_for(tab)]
        assert len(ids) == len(set(ids)), tab


def test_every_tab_has_a_period_chart_and_a_name_for_its_rows():
    for tab in charts.TAB_IDS:
        kinds = {c.kind for c in charts.charts_for(tab)}
        assert "time" in kinds, f"{tab} has nothing over time"
        assert tab in charts.TAB_ROW_LABELS


# ── Vocabulary ───────────────────────────────────────────────

@pytest.mark.parametrize("spec", ALL, ids=IDS)
def test_titles_and_labels_use_the_published_vocabulary(spec):
    for text in (spec.title, spec.description, *(label for _, label in spec.datasets)):
        vocabulary.assert_clean(text, f"{spec.tab}/{spec.id}")


def test_the_vocabulary_maps_only_produce_declared_dataset_ids():
    # Every value the archive can hold maps to an id a chart declares, apart
    # from UNSTATED, which is deliberately undeclared: it appears only when
    # the archive actually holds an empty column, and it is labelled then.
    declared = {i for spec in ALL for i, _ in spec.datasets}
    for mapping in (charts.IMPORTANCE_VALUES,
                    charts.OUTLOOK_VALUES, charts.SENTIMENT_VALUES):
        assert set(mapping.values()) <= declared
    assert charts.UNSTATED not in declared
    assert charts.UNSTATED in charts.EXTRA_LABELS


def test_the_archive_vocabulary_is_the_published_one():
    assert set(charts.OUTLOOK_VALUES) == set(vocabulary.OUTLOOKS)
    assert set(charts.SENTIMENT_VALUES) == set(vocabulary.SENTIMENTS)
    assert set(charts.IMPORTANCE_VALUES) == set(vocabulary.IMPORTANCE_LEVELS)
    # RELATIONS IS NOT HERE, AND THAT IS THE POINT OF THIS LINE.
    #
    # "Relation to source per period" is not a chart on any tab - it would
    # tell the reader, six times over, how many rows a document was about,
    # which nobody asks. The vocabulary is still the archive's
    # (app/vocabulary.py) and the drilldown prints the column; this module
    # has nothing to say about it, so it neither maps it nor paints it.
    assert not hasattr(charts, "RELATION_VALUES")


# ── Colours ──────────────────────────────────────────────────

def test_every_declared_dataset_has_a_colour_of_its_own():
    for spec in ALL:
        for dataset_id, _ in spec.datasets:
            assert dataset_id in charts.DATASET_COLOURS, f"{spec.tab}/{spec.id}: {dataset_id}"


def _drawn_side_by_side(spec) -> bool:
    """Whether this chart's series end up as separate bars in one slot.

    The same rule as static/js/charts.js stacksSeries(): a chart may only
    stack when its series partition the rows, and it is worth doing from
    three series up. Everything else is drawn side by side.
    """
    return len(spec.datasets) >= 2 and not (spec.stacked and len(spec.datasets) > 2)


SIDE_BY_SIDE = [s for s in ALL if _drawn_side_by_side(s)]

# THE ONE CHART THAT CANNOT REACH 1.8:1, AND THE ARITHMETIC THAT SAYS SO.
#
# "Numeric values by type" draws minimum, average and maximum side by side.
# They are three steps of ONE ordered reading, so they are one hue by right -
# and the blue count ramp spans 3.77:1 from its lightest step to its darkest
# (both ends are fixed: the light end by the 3:1 every mark owes the page,
# the dark end by black). Three steps of a 3.77:1 ramp cannot each be 1.8:1
# apart from the next - that would need 3.24:1 of range for two gaps and
# leaves none over. The widest split the ramp allows is --count-1 /
# --count-3 / --count-6, which is 1.80:1 and 2.09:1, and the floor below is
# what that measures to.
#
# It is a floor, not an exemption: the pair is still measured, and the three
# bars carry the same value text, the same order in every group and the same
# legend as any other chart. Nothing else may join this list without the
# same arithmetic written beside it.
ONE_RAMP_FLOOR = {("attributes", "value_stats"): 1.75}


@pytest.mark.parametrize("spec", SIDE_BY_SIDE, ids=[f"{s.tab}.{s.id}" for s in SIDE_BY_SIDE])
def test_two_bars_next_to_each_other_differ_in_lightness_not_only_in_hue(spec):
    """Market insights (#1D4F91) and High relevance (#6A3D9A) had a contrast
    ratio of 1.06 - the same lightness. On a black-and-white printer (and
    print.css forces the colours onto the paper) the two bars are one block,
    and blue against purple is the classic confusion pair for a red-green
    deficiency. The bars are 13 px wide at 1024 px, so hue was the only cue
    left, and it is not a cue everybody has.

    Segments stacked in ONE bar are exempt: they are steps of an ordered
    scale, a white hairline separates them, and eight steps cannot be
    1.8:1 apart from each other and all still visible on white.
    """
    floor = ONE_RAMP_FLOOR.get((spec.tab, spec.id), 1.8)
    ids = [i for i, _ in spec.datasets]
    for a in range(len(ids)):
        for b in range(a + 1, len(ids)):
            first, second = charts.DATASET_COLOURS[ids[a]], charts.DATASET_COLOURS[ids[b]]
            ratio = colours.contrast_ratio(first, second)
            assert ratio >= floor, \
                f"{spec.tab}/{spec.id}: {ids[a]} {first} and {ids[b]} {second} are {ratio:.2f}:1"


@pytest.mark.parametrize("colour", sorted(set(charts.DATASET_COLOURS.values()) | set(charts.PALETTE)))
def test_every_chart_colour_is_visible_on_white(colour):
    # WCAG 1.4.11: a graphic that carries meaning needs 3:1 against what it
    # sits on, and every chart here sits on a white card.
    assert colours.is_valid_hex(colour)
    assert colours.contrast_ratio(colour, "#ffffff") >= 3.0, colour


def test_unset_is_the_grey_that_means_absence_and_nothing_else_is():
    """"Unset" is the absence of a reading, not a step of the scale - which
    is why it goes off the end of a sentiment axis (SENTIMENT_AXIS) instead
    of beyond Very Positive.

    The colour now says the same thing, and says it more strongly than a
    contrast number could: Unset is --no-data, --no-data is on none of the
    three scales, and no other dataset in the whole registry is painted with
    it. So a grey mark on any chart of this dashboard means one thing.
    """
    unset = charts.DATASET_COLOURS["unset"]
    assert unset == charts.NO_DATA
    assert charts.DATASET_COLOURS[charts.UNSTATED] == charts.NO_DATA
    assert charts.NO_DATA not in set(charts.VALENCE) | set(charts.RELEVANCE) | set(charts.COUNT)
    grey = {name for name, colour in charts.DATASET_COLOURS.items() if colour == charts.NO_DATA}
    assert grey == {"unset", charts.UNSTATED}, grey
    # And it is told from the neutral step of the valence scale by hue AND
    # by lightness: 1.64:1 is what app.css measures the pair at, and the two
    # sit in one key on an outlook chart (Rising, Neutral, Declining, Unset).
    neutral = charts.DATASET_COLOURS["neutral"]
    assert colours.contrast_ratio(unset, neutral) >= 1.6
    assert unset != neutral


def test_every_chart_colour_comes_out_of_the_design_system():
    """A hex written in this file is a second copy of the stylesheet, and a
    second copy is how one meaning becomes two colours. Every dataset colour
    has to BE one of the tokens app.css defines - the module reads them from
    there at import, so this test fails the moment somebody types one in."""
    system = set(charts.VALENCE) | set(charts.RELEVANCE) | set(charts.COUNT) | {charts.NO_DATA}
    for dataset_id, colour in charts.DATASET_COLOURS.items():
        assert colour in system, f"{dataset_id} is painted {colour}, which is in no scale"
    # The tokens really were read from the stylesheet, not defaulted away.
    assert charts.TOKENS, "app.css defined no tokens; the chart colours would be a guess"
    assert charts.COUNT_SINGLE == charts.TOKENS["--count-4"]


def test_each_reading_is_on_the_scale_that_matches_what_it_measures():
    """The user's rule, as a table: a rating or a market reading is a
    valence (red to green), an importance is a relevance (violet), a plain
    magnitude is a count (blue). A dataset on the wrong scale is the whole
    problem this replaced - relevance drawn in the same blue as a row count,
    and a rating drawn in a scheme nobody could read as good or bad."""
    valence = set(charts.VALENCE)
    relevance = set(charts.RELEVANCE)
    count = set(charts.COUNT)
    for name in ("very_negative", "negative", "slightly_negative", "neutral",
                 "slightly_positive", "positive", "very_positive", "rising", "declining"):
        assert charts.DATASET_COLOURS[name] in valence, name
    # "insights" IS THE PALE STEP OF THE VIOLET, NOT A COUNT.
    #
    # It is drawn beside "high_relevance" on Market insights per period, and
    # must not be the count blue there: a bar that means "the archive did not
    # mark this one relevant" standing in the colour of a plain total, next
    # to a violet bar that means the opposite. The pair is one reading of one
    # column, so it is one ramp - the lightest step and the darkest of it -
    # and the legend says "Low relevance" rather than "Market insights",
    # which named the tab and not the bar.
    for name in ("not_important", "low", "medium", "high", "critical",
                 "insights", "high_relevance"):
        assert charts.DATASET_COLOURS[name] in relevance, name
    for name in ("count", "sources", "ratings", "connections", "locations", "attributes",
                 "events", "rows", "distinct", "min", "avg", "max",
                 "about_entities", "about_document"):
        assert charts.DATASET_COLOURS[name] in count, name
    # TRUST IS A VERDICT AND SITS ON THE VALENCE SCALE. Not two shades of
    # the count blue, on the reasoning that a trust flag is a split of a
    # count rather than a reading of good and bad. It is a reading: a reader
    # who has just seen a green bar on the Ratings tab looks for the same
    # meaning here.
    for name in ("trustful", "not_trustful"):
        assert charts.DATASET_COLOURS[name] in valence, name


def test_the_scales_run_the_way_they_are_meant_to_be_read():
    """Negative is red and positive is green, the step grows with the
    distance from neutral, and the violet gets stronger with relevance. All
    three are checked by LUMINANCE, which is what survives a dichromat and a
    black-and-white printer - a scale whose strength is carried by
    saturation alone is a scale half the readers do not have."""
    # Valence: the ends are far apart in lightness, not only in hue.
    ends = colours.contrast_ratio(charts.valence(-3), charts.valence(3))
    assert ends >= 1.7, f"the two ends of the valence scale are {ends:.2f}:1 apart"
    # Negative is the red side, positive the green side.
    for step in (-3, -2, -1):
        red, green, _ = (int(charts.valence(step)[i:i + 2], 16) for i in (1, 3, 5))
        assert red > green, f"valence {step} is not on the red side"
    for step in (1, 2, 3):
        red, green, _ = (int(charts.valence(step)[i:i + 2], 16) for i in (1, 3, 5))
        assert green > red, f"valence {step} is not on the green side"
    # Neutral is the palest step of the scale, not grey and not an end.
    pale = max(charts.VALENCE, key=lambda c: colours.relative_luminance(c))
    assert charts.valence(0) == pale
    # Relevance and count both darken monotonically.
    for ramp in (charts.RELEVANCE, charts.COUNT):
        lums = [colours.relative_luminance(c) for c in ramp]
        assert lums == sorted(lums, reverse=True), ramp


def test_the_valence_scale_is_the_encoding_and_nothing_else_adds_to_it():
    """RED FOR NEGATIVE AND GREEN FOR POSITIVE, which is what people read
    fastest - and then the one caveat, implemented rather than argued about.

    Red and green on market data read as buy and sell to anyone trained in
    finance. The colour may carry the valence; the WORDS may not, and no
    arrow, triangle or trend glyph may be added to it. So the wording stays
    as the archive publishes it ("Negative reporting", never an
    instruction), and the glyph half of the rule is
    tests/unit/test_vocabulary_guard.py.
    """
    # One meaning, one colour: a rating reduced to "negative" and a
    # sentiment of "Negative" are the same reading and the same step.
    assert charts.DATASET_COLOURS["negative"] == charts.valence(-2)
    assert charts.DATASET_COLOURS["positive"] == charts.valence(2)
    assert charts.DATASET_COLOURS["neutral"] == charts.valence(0)
    # It IS a traffic light, deliberately.
    red, green, _ = (int(charts.DATASET_COLOURS["negative"][i:i + 2], 16) for i in (1, 3, 5))
    assert red > green + 40, "negative reporting is not red"
    red, green, _ = (int(charts.DATASET_COLOURS["positive"][i:i + 2], 16) for i in (1, 3, 5))
    assert green > red + 40, "positive reporting is not green"
    # And nothing in the wording turns it into advice.
    for spec in charts.charts_for("market") + charts.charts_for("ratings"):
        vocabulary.assert_clean(spec.title, f"market/{spec.id}")
        vocabulary.assert_clean(spec.description, f"market/{spec.id}")
        for _, label in spec.datasets:
            vocabulary.assert_clean(label, f"market/{spec.id}")


def test_a_dataset_nobody_declared_still_gets_a_colour():
    assert charts.colour_for("something-new", 0) in charts.PALETTE
    assert charts.colour_for("something-new", 1) != charts.colour_for("something-new", 0)
    assert charts.colour_for("rising") == charts.DATASET_COLOURS["rising"]


# ── Labels ───────────────────────────────────────────────────

def test_a_chart_without_a_split_has_exactly_one_named_dataset():
    single = charts.get_chart("sources", "per_period")
    assert charts.default_dataset(single) == ("sources", "Sources")
    plain = charts.get_chart("sources", "by_type")
    assert charts.default_dataset(plain) == (charts.DEFAULT_DATASET_ID, "Sources")


@pytest.mark.parametrize("spec", [s for s in ALL if s.kind in ("key", "matrix")],
                         ids=[f"{s.tab}.{s.id}" for s in ALL if s.kind in ("key", "matrix")])
def test_a_chart_with_categories_says_what_one_of_them_is(spec):
    """A chart with more bars than its plot has room for draws NO labels -
    the alternative is the smear of overlapping text this was raised about -
    and puts one line under the title instead: "43 entities - open the chart
    to see the names". That line needs the noun, in the plural, and
    "43 categories" is not a sentence anybody wants to read."""
    assert spec.category, f"{spec.tab}/{spec.id} has no word for one of its categories"
    assert spec.category == spec.category.lower(), spec.category
    assert not spec.category.endswith("s ")
    # A plural, so the sentence reads "43 entities" and not "43 entity".
    assert spec.category.endswith("s"), spec.category


def test_a_time_chart_leaves_the_category_word_empty():
    """Its categories are periods, and a time axis thins its ticks rather
    than hiding them - so there is no sentence to write."""
    for spec in ALL:
        if spec.kind == "time":
            assert not spec.category, f"{spec.tab}/{spec.id}"


def test_the_catalogue_carries_the_category_word_to_the_page():
    every = [c for tab in charts.catalogue()["tabs"] for c in tab["charts"]]
    assert all("category" in c for c in every)
    by_id = {(t["id"], c["id"]): c for t in charts.catalogue()["tabs"] for c in t["charts"]}
    assert by_id[("entities", "most_named")]["category"] == "entities"


def test_dataset_labels_come_from_the_registry_first():
    spec = charts.get_chart("market", "short_outlook_per_period")
    assert charts.dataset_label("market", spec, "rising") == "Rising"
    assert charts.dataset_label("market", spec, charts.UNSTATED) == "Not stated"
    assert charts.dataset_label("market", spec, "made_up") == "Made up"


# ── The page's tab strip ─────────────────────────────────────

def test_the_template_lists_exactly_the_registry_tabs_in_order():
    """templates/diagrams.html writes the tabs out so the page has them
    before any JavaScript runs. That is only safe while the two agree."""
    source = TEMPLATE.read_text(encoding="utf-8")
    block = re.search(r"set tabs = \[(.*?)\] -%\}", source, re.S)
    assert block, "the tab list is not where the template says it is"
    pairs = re.findall(r'\("([a-z_]+)",\s*"([^"]+)"\)', block.group(1))
    assert pairs == [(t, charts.TAB_TITLES[t]) for t in charts.TAB_IDS]


def test_the_template_asks_for_a_suggestion_list_per_scope_and_per_axis():
    """Every scope has a search box - the summary included, because the view
    of everything gets the magnifier that searches everything (app/scope.py:
    _resolve_everything) - and every scope but the summary has TWO, one per
    axis: the same term read as one thing or as a whole class.

    The summary is the one with no type axis. It is already everything, and
    a type search over everything is the summary again, which is why
    resolve_scope raises on that pair rather than answering it.
    """
    source = TEMPLATE.read_text(encoding="utf-8")
    # WITHIN `search_fields`, not the whole file: the template holds a second
    # table keyed by the same scope names (axis_words, the word each half of
    # the switch is called), and a search over the whole file finds whichever
    # comes first.
    fields = source[source.index("{%- set search_fields = {"):]
    for scope in ("summary", "entity", "source", "location", "market", "events"):
        block = re.search(rf'"{scope}":\s*\{{(.*?)\}},\n', fields, re.S)
        assert block, scope
        axes = set(re.findall(r'"(object|type)":\s*\(', block.group(1)))
        assert "/api/suggest/" in block.group(1), scope
        assert axes == ({"object"} if scope == "summary" else {"object", "type"}), (scope, axes)
    assert '"/api/suggest/anything"' in source
    # The type axis of every scope, by the list it reads.
    for url in ("/api/suggest/entity_type", "/api/suggest/source_type",
                "/api/suggest/location_type", "/api/suggest/market_topic",
                "/api/suggest/event_type", "/api/suggest/event"):
        assert f'"{url}"' in source, url
    # ... and the box is rendered for all six, not five of them.
    assert "{% if scope != 'summary' %}" not in source


def test_the_page_and_its_script_agree_about_what_each_axis_asks_for():
    """The template renders the axis the page opens on; the script has to
    know the OTHER one, so pressing the switch can re-label the field and
    re-point its suggestion list without a round trip. Two tables, and this
    is what stops them drifting - a field labelled "Entity type" still
    offering entity names is the exact failure the switch exists to
    prevent."""
    template = TEMPLATE.read_text(encoding="utf-8")
    script = (APP_JS / "diagrams.js").read_text(encoding="utf-8")

    def from_template() -> dict:
        out: dict[tuple[str, str], tuple[str, str, str]] = {}
        for scope, block in re.findall(r'"(\w+)":\s*\{(.*?)\},\n', template, re.S):
            # Split on the axis keys rather than matching the closing paren:
            # the last row of a scope has had its `}` eaten by the block
            # above, so there is nothing after it to anchor on.
            pieces = re.split(r'"(object|type)":\s*\(', block)
            for axis, body in zip(pieces[1::2], pieces[2::2]):
                parts = re.findall(r'"((?:[^"\\]|\\.)*)"', body)
                out[(scope, axis)] = tuple(parts[:3])
        return out

    def from_script() -> dict:
        block = re.search(r"const AXIS_FIELDS = \{(.*?)\n\};", script, re.S)
        assert block, "diagrams.js no longer declares AXIS_FIELDS"
        out: dict[tuple[str, str], tuple[str, str, str]] = {}
        for scope, body in re.findall(r"\n  (\w+): \{(.*?)\n  \},", block.group(1), re.S):
            for axis, row in re.findall(r'(object|type): \[(.*?)\],', body, re.S):
                out[(scope, axis)] = tuple(re.findall(r'"([^"]*)"', row)[:3])
        return out

    page = from_template()
    js = from_script()
    # The summary is rendered but has no switch, so the script never needs it.
    page.pop(("summary", "object"), None)
    assert js, "diagrams.js declares no axis fields at all"
    assert set(js) == set(page), (sorted(set(js) ^ set(page)))
    for key in sorted(page):
        assert js[key] == page[key], (key, page[key], js[key])


def test_the_events_page_opens_on_the_type_axis():
    """Its box has always searched the event type, so every link ever shared
    of it keeps meaning what it meant; what the page GAINS is the object
    axis - one event, by its own name. app/scope.py holds the same fact
    (DEFAULT_AXIS), and this is the template agreeing with it."""
    from app import scope
    source = TEMPLATE.read_text(encoding="utf-8")
    assert 'set default_axis = "type" if scope == "events" else "object"' in source
    assert scope.default_axis("events") == "type"
    for kind in ("entity", "source", "location", "market", "summary"):
        assert scope.default_axis(kind) == "object"



def test_the_switch_says_what_the_scope_searches_rather_than_object():
    """"Object" names nothing: on the Source page it is a document, on
    Location a place, on Market an entity - and the reader has to open the
    field to find out which. Every scope names its own subject."""
    source = TEMPLATE.read_text(encoding="utf-8")
    words = source[source.index("{%- set axis_words = {"):]
    words = words[:words.index("-%}")]
    wanted = {"entity": ("Entity", "Type"), "source": ("Source", "Type"),
              "location": ("Location", "Type"), "market": ("Entity", "Topic"),
              "events": ("Event", "Type")}
    for scope, (obj, typ) in wanted.items():
        row = re.search(rf'"{scope}":\s*\{{(.*?)\}}', words, re.S)
        assert row, scope
        assert f'"object": "{obj}"' in row.group(1), (scope, row.group(1))
        assert f'"type": "{typ}"' in row.group(1), (scope, row.group(1))
    assert "Object" not in words, "a scope still calls its subject Object"
    # And the switch is rendered from the table, not from a literal.
    assert "axis_words[scope]['object']" in source
    assert "axis_words[scope]['type']" in source


# ── The ⓘ: one sentence per chart, one per legend entry ──────

@pytest.mark.parametrize("spec", ALL, ids=IDS)
def test_every_chart_says_what_it_counts_and_what_one_bar_is(spec):
    """The card's purpose line is two reserved lines and may not grow past
    them (css/diagrams.css: everything above the plot is a constant height).
    The hint is the sentence there was never room for, and it is asked for
    rather than shown - so it has room to say the thing the description
    cannot: the UNIT.

    "Ratings per period" counts RATING ROWS and not documents; "Events by
    entity" counts (event, entity) PAIRS, so one event naming three entities
    adds one to each of three bars; "Numeric values by type" counts nothing
    at all - its bars are a minimum, a mean and a maximum. Every one of those
    is a number a reader will otherwise take for something else.
    """
    assert spec.hint, f"{spec.tab}/{spec.id} has no ⓘ sentence"
    assert spec.hint[0].isupper(), spec.hint
    assert spec.hint.endswith("."), spec.hint
    # Not a second title and not the description again.
    assert spec.hint.lower() != spec.title.lower(), spec.hint
    assert spec.hint.lower() != spec.description.lower(), spec.hint
    # Long enough to be the three answers it promises, short enough to read
    # in a bubble over a card.
    assert 60 <= len(spec.hint) <= 400, (len(spec.hint), spec.hint)


def test_the_catalogue_carries_the_hint_to_the_page():
    """There are fifty-six of these and one page: the sentence is served with
    the catalogue rather than written into fifty-six templates."""
    every = [c for tab in charts.catalogue()["tabs"] for c in tab["charts"]]
    assert all(c["hint"] for c in every)
    by_id = {(t["id"], c["id"]): c for t in charts.catalogue()["tabs"] for c in t["charts"]}
    assert "rating rows" in by_id[("ratings", "per_period")]["hint"].lower()


def test_every_declared_dataset_says_what_its_colour_means():
    """A key of six violet bands names its bands and explains none of them.
    "Low Importance" is a phrase from a vocabulary the reader has never been
    shown, and "Minimum" is not a count although every other bar on the page
    is - so every dataset a chart DECLARES carries a sentence of its own, and
    the legend turns it into the ⓘ beside the row (static/js/legend.js)."""
    for spec in ALL:
        # A chart with ONE dataset draws no key at all (static/js/charts.js:
        # renderChartKey) - there is nothing to tell apart, and the title
        # already names what the bars are.
        if len(spec.datasets) < 2:
            continue
        for dataset_id, label in spec.datasets:
            said = charts.dataset_hint(spec.tab, spec, dataset_id)
            assert said, f"{spec.tab}/{spec.id}: {dataset_id} has no sentence"
            assert said[0].isupper() and said.endswith("."), said
            assert said.lower() != label.lower(), said
    # And the bucket a row with an empty column falls into, which is not
    # declared anywhere and is the one a reader is most likely to query.
    assert charts.dataset_hint("sources", ALL[0], charts.UNSTATED)


def test_the_hint_sentences_use_the_published_vocabulary():
    for spec in ALL:
        vocabulary.assert_clean(spec.hint, f"{spec.tab}/{spec.id}")
        vocabulary.assert_no_glyphs(spec.hint, f"{spec.tab}/{spec.id}")
    for dataset_id, said in charts.DATASET_HINTS.items():
        vocabulary.assert_clean(said, f"dataset {dataset_id}")
        vocabulary.assert_no_glyphs(said, f"dataset {dataset_id}")


def test_the_info_glyph_in_script_is_the_one_the_macros_draw():
    """ONE PLACE FOR EVERY GLYPH IN THE PRODUCT (templates/_macros.html), and
    where a second copy cannot be avoided it is pinned rather than trusted.

    static/js/legend.js builds a hint button for a chart card, and a chart
    card's ⓘ is the FIRST hint on the Diagrams page - there is nothing yet in
    the DOM to clone from, which is what map.js does everywhere else. So the
    path is written out once in that file, and this reads both files and
    fails when they differ.
    """
    macros = (Path(__file__).resolve().parents[2] / "app" / "templates" / "_macros.html"
              ).read_text(encoding="utf-8")
    drawn = re.search(r'"info":\s*"([^"]+)"', macros)
    assert drawn, "the macros no longer define an info icon"
    script = (APP_JS / "legend.js").read_text(encoding="utf-8")
    copied = re.search(r'const INFO_PATH = "([^"]+)"', script)
    assert copied, "legend.js no longer carries the info path"
    assert copied.group(1) == drawn.group(1), (copied.group(1), drawn.group(1))


# ── The summary band ─────────────────────────────────────────

# THE THREE TABS THAT ASKED FOR ONE, AND THE FIVE THAT SAID NO.
#
# A summary was once required of every tab, and every tab got one whether or
# not it had anything to summarise. Read through, five of them were a second
# answer to a question the charts above had already answered: the extreme
# ratings under a chart of ratings by value, the most-named entities under a
# chart of the most-named entities. Those five are gone by name, one at a
# time, and what is left is the three where the summary says something the
# bars cannot - the rows themselves, newest first.
SUMMARY_TABS = {"sources", "attributes", "events"}


def test_the_summary_is_a_list_on_the_three_tabs_that_keep_one():
    """The summary is a LIST of rows, below everything, or it is nothing.

    Two other kinds do not share that place:

      a band chart    - the first chart of a tab, drawn full width above the
                        grid. Tabs order their own charts, and a chart that
                        wants the full width says so with width="full" -
                        there is nothing a band could say that a first
                        full-width chart does not.
      the map         - not a summary of the rows, it is one of the drawings
                        OF them, so it sits in the grid beside the charts
                        (MAP_AFTER names the chart it follows). The card is
                        parked in the summary section while a tab has no map
                        for it, which is a place to keep a card and not a
                        statement about what a summary is.

    So the whole claim is: the list registry covers exactly the three tabs
    that keep a summary, no tab carries a band chart, and a tab without a
    list ends at its last chart.
    """
    from app.charts import summaries as summarydata  # noqa: PLC0415 - one test

    assert set(summarydata.BUILDERS) == SUMMARY_TABS
    assert SUMMARY_TABS <= set(charts.TAB_IDS)
    assert not [c for tab in charts.TAB_IDS for c in charts.charts_for(tab) if c.band]


def test_a_map_belongs_to_a_chart_and_not_to_the_summary():
    """MAP_AFTER answers "after which chart", and a map with no answer would
    be drawn nowhere - the card stays parked and the tab silently loses its
    map. So every tab that can build one has to name the chart it follows,
    and that chart has to exist on that tab."""
    from app.charts import maps as mapdata  # noqa: PLC0415 - one test

    for tab in mapdata.BUILDERS:
        after = charts.MAP_AFTER.get(tab)
        assert after, f"{tab} builds a map and says after which chart it goes"
        assert after in {c.id for c in charts.charts_for(tab)}, f"{tab}/{after}"
    # And nothing names a chart on a tab that has no map to place.
    assert set(charts.MAP_AFTER) <= set(mapdata.BUILDERS)


# THE TWELVE-ROW CUT WENT WITH THE BAND.
#
# A band chart stood above the grid and had to be read at a glance, so six of
# them were cut to twelve categories in SQL. Those six are ordinary full-width
# charts now, sized from their own data so that every name is drawn - and a
# chart whose whole point is "all of it, readable" may not quietly show twelve
# of twenty-five. They take KEY_CHART_LIMIT like every other key chart, which
# is what this asserts in their place.


def test_no_key_chart_is_cut_below_the_limit_every_key_chart_has():
    from app import scope as scope_module, sqlbuild  # noqa: PLC0415 - one test needs them
    from app.context import Context  # noqa: PLC0415
    from app.timeframes import window_for  # noqa: PLC0415
    ctx = Context(project="p", language="English")
    for spec in ALL:
        if spec.kind != "key" or spec.builder is None:
            continue
        plan = spec.builder(scope_module._summary(ctx), ctx, window_for("7d", 0))
        assert plan.limit == sqlbuild.KEY_CHART_LIMIT, (spec.tab, spec.id, plan.limit)


def test_the_catalogue_tells_the_page_which_charts_are_the_band():
    every = [c for tab in charts.catalogue()["tabs"] for c in tab["charts"]]
    assert all("band" in c for c in every)
    assert sum(1 for c in every if c["band"]) == sum(1 for s in ALL if s.band)
