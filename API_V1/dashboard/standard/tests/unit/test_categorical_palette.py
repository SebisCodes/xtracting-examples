"""A category may not wear a scale's colour.

    python -m pytest tests/unit/test_categorical_palette.py -q

THE RULE: one colour coding across the whole dashboard, so a colour means
the same thing wherever it is seen. app.css turns it into four reservations
and tests/unit/test_palette.py measures those four:

    red to green   valence - a rating, a sentiment, an outlook
    violet         relevance - how much something matters
    blue           a plain count
    grey           --no-data - no reading at all

This file measures the OTHER half, which nothing measured before: the two
categorical palettes that are drawn beside them. The eleven connection
colour groups (dashboard/sql/02-dashboard-seed.sql) and the eight free-text
chart colours (app/charts.PALETTE) are not scales - they are categories
somebody chose - and a palette picked by eye lands squarely on the reserved
hues. Some examples of what this test refuses:

    PALETTE[0] #1D4F91  --count-4, the same hex, 1.00:1
    Competitor #E53935  1.28:1 from --valence-neg-1
    Partner    #388E3C  1.14:1 from --valence-pos-1
    Investor   #7E57C2  1.09:1 from --relevance-1
    Supplier   #2196F3  1.12:1 from --count-1
    Ownership  #757575  1.25:1 from --no-data
    Other      #78909C  a second grey, which would meet --no-data on ONE
                        screen: Diagrams -> Connections draws "Connections
                        by colour group" one card above another chart, so
                        #78909C for the category "Other" would stand
                        beside #616670 for absence.

WHY THE RULE IS NOT A CONTRAST NUMBER ON ITS OWN. "At least 1.8:1 from
every step of every scale" cannot be satisfied by any colour at all. WCAG
contrast is a ratio of LUMINANCES, and the three ramps together run from
--relevance-0 (3.21:1 on white) down to --valence-neg-3 (12.05:1) with no
gap in between; a colour 1.8:1 lighter than the lightest step is under
1.8:1 on white itself and fails WCAG 1.4.11 for a mark. Two colours are
confusable when they are close in BOTH hue and lightness, so that is what
is measured: a categorical colour must be out of the reserved hue bands
altogether - which, given the arithmetic above, is the only way to be far
enough from a whole ramp - and it must not be grey.

The free bands that leaves are amber and brown, olive, teal, magenta and
rose. Eleven groups and eight palette entries fit in them by hue and by
lightness, which is what the tables below check.
"""

from __future__ import annotations

import colorsys
import re
from pathlib import Path

import pytest

from app import charts, colours

SEED = Path(__file__).resolve().parents[2] / "sql" / "02-dashboard-seed.sql"

# ── The reserved hue bands ───────────────────────────────────────────────
#
# Measured off the tokens themselves (the hues below are what app.css
# holds) and widened by 22 degrees on each side, which is about where two
# saturated colours of one lightness stop being read as the same colour.
#
#   valence red      2 - 8      valence green  138 - 148
#   relevance      274 - 279    count          209 - 215
#
# --valence-0 is the one scale colour outside a family: an amber at 44
# degrees, the palest step of the valence ramp. It is not a whole family,
# so it is handled by the pair rule further down rather than by a band.
MARGIN = 22.0
RESERVED_BANDS: tuple[tuple[str, float, float], ...] = (
    ("valence, the red half", 2.0 - MARGIN, 8.0 + MARGIN),
    ("valence, the green half", 138.0 - MARGIN, 148.0 + MARGIN),
    ("relevance, the violet", 274.5 - MARGIN, 278.5 + MARGIN),
    ("count, the blue", 209.6 - MARGIN, 214.2 + MARGIN),
)

# Below this a colour reads as a grey, and grey is --no-data's alone.
MIN_SATURATION = 0.25
# Two colours of one hue are told apart by lightness; this is the same
# floor tests/unit/test_chart_registry.py uses for two bars side by side.
PAIR_MIN = 1.8


def _hsl(hex_colour: str) -> tuple[float, float, float]:
    h = hex_colour.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    hue, light, sat = colorsys.rgb_to_hls(r, g, b)
    return hue * 360, sat, light


def _hue_distance(a: float, b: float) -> float:
    d = abs(a - b) % 360
    return min(d, 360 - d)


def _in_band(hue: float, low: float, high: float) -> bool:
    return any(low <= hue + turn <= high for turn in (-360.0, 0.0, 360.0))


def _seed_groups() -> list[tuple[str, str]]:
    """(key, colour) for the eleven groups, read out of the seed file so the
    test cannot drift from what an archive is actually given."""
    body = SEED.read_text(encoding="utf-8").split("VALUES", 1)[1].split("ON CONFLICT", 1)[0]
    rows = re.findall(r"\('([a-z]+)',\s*'[^']*',\s*'(#[0-9A-Fa-f]{6})'", body)
    assert len(rows) == 11, f"expected eleven groups in the seed, found {len(rows)}"
    return rows


GROUPS = _seed_groups()
FREE_TEXT = [(f"PALETTE[{i}]", c) for i, c in enumerate(charts.PALETTE)]
CATEGORICAL = [(f"group {k}", c) for k, c in GROUPS] + FREE_TEXT

# Every colour that carries a READING, which is what a category must not be
# mistaken for.
SCALE_COLOURS: dict[str, str] = {
    **{f"--valence-{n}": c for n, c in zip(
        ("neg-3", "neg-2", "neg-1", "0", "pos-1", "pos-2", "pos-3"), charts.VALENCE)},
    **{f"--relevance-{i}": c for i, c in enumerate(charts.RELEVANCE)},
    **{f"--count-{i}": c for i, c in enumerate(charts.COUNT, start=1)},
    "--no-data": charts.NO_DATA,
}


@pytest.mark.parametrize("name,colour", CATEGORICAL, ids=[n for n, _ in CATEGORICAL])
def test_a_category_is_not_on_a_reserved_hue(name, colour):
    """The whole of a ramp is off limits, not just its nearest step: no
    colour can be 1.8:1 from every step of a ramp that spans 3.21:1 to
    12.05:1 on white AND still reach 3:1 on white itself, so the only
    honest rule is to stay out of the hue."""
    hue, _sat, _light = _hsl(colour)
    for what, low, high in RESERVED_BANDS:
        assert not _in_band(hue, low, high), (
            f"{name} {colour} is at {hue:.0f} degrees, inside {low:.0f}-{high:.0f} - "
            f"the hue app.css reserves for {what}")


@pytest.mark.parametrize("name,colour", CATEGORICAL, ids=[n for n, _ in CATEGORICAL])
def test_a_category_is_never_grey(name, colour):
    """Grey is --no-data and nothing else. An Ownership in #757575 and an
    Other in #78909C would be greys, and "Other" would meet absence on one
    screen.

    SATURATION is what says so, not a contrast ratio: a grey and a colour
    of the same lightness are 1.00:1 apart whatever their hues, so the
    ratio to --no-data cannot tell a grey from anything."""
    _hue, sat, _light = _hsl(colour)
    assert sat >= MIN_SATURATION, \
        f"{name} {colour} has saturation {sat:.2f} - it reads as a grey, and grey means no data"


@pytest.mark.parametrize("name,colour", CATEGORICAL, ids=[n for n, _ in CATEGORICAL])
def test_a_category_is_never_literally_a_scale_colour(name, colour):
    """PALETTE[0] was --count-4, hex for hex."""
    same = [token for token, value in SCALE_COLOURS.items() if value.lower() == colour.lower()]
    assert not same, f"{name} is painted {colour}, which IS {', '.join(same)}"


@pytest.mark.parametrize("name,colour", CATEGORICAL, ids=[n for n, _ in CATEGORICAL])
def test_a_category_beside_a_scale_colour_of_its_own_lightness_differs_in_hue(name, colour):
    """The pair rule, which catches what the band rule cannot: --valence-0
    is an amber at 44 degrees, in nobody's family and in the middle of the
    free amber-and-brown band, so a category near it has to be told apart
    by lightness instead."""
    hue, _sat, _light = _hsl(colour)
    for token, value in SCALE_COLOURS.items():
        other_hue, _s, _l = _hsl(value)
        if _hue_distance(hue, other_hue) >= MARGIN:
            continue
        ratio = colours.contrast_ratio(colour, value)
        assert ratio >= PAIR_MIN, (
            f"{name} {colour} is {_hue_distance(hue, other_hue):.0f} degrees from {token} "
            f"{value} and only {ratio:.2f}:1 - one colour, two meanings")


@pytest.mark.parametrize("name,colour", CATEGORICAL, ids=[n for n, _ in CATEGORICAL])
def test_a_category_is_visible_on_the_white_it_is_drawn_on(name, colour):
    # WCAG 1.4.11 again, so a hue moved off a reserved band cannot land
    # somewhere invisible instead.
    assert colours.is_valid_hex(colour)
    assert colours.contrast_ratio(colour, "#ffffff") >= colours.MIN_CONTRAST_ON_WHITE, colour


def test_two_groups_of_one_hue_are_told_apart_by_lightness():
    """Five free hues for eleven groups means some share one, and a legend
    of eleven swatches is only readable if those differ in lightness."""
    for i, (key_a, a) in enumerate(GROUPS):
        for key_b, b in GROUPS[i + 1:]:
            hue_a, _sa, _la = _hsl(a)
            hue_b, _sb, _lb = _hsl(b)
            if _hue_distance(hue_a, hue_b) >= MARGIN:
                continue
            ratio = colours.contrast_ratio(a, b)
            assert ratio >= PAIR_MIN, (
                f"{key_a} {a} and {key_b} {b} share a hue and are only {ratio:.2f}:1 apart")


def test_the_fallback_colour_is_the_same_one_everywhere():
    """Four places draw an unassigned type before or without an answer from
    the table - this module, the graph endpoint and the two scripts - and
    four literals drift. The seed's "other" row is the one truth."""
    seeded = dict(GROUPS)["other"]
    assert colours.FALLBACK_COLOUR.lower() == seeded.lower()
    assert colours._BUILTIN_FALLBACK.colour.lower() == seeded.lower()
    js = Path(__file__).resolve().parents[2] / "app" / "static" / "js"
    for name in ("graph.js", "colours.js"):
        source = (js / name).read_text(encoding="utf-8")
        assert seeded in source, f"{name} draws an unassigned type in some other colour"


def test_every_group_takes_a_readable_text_colour():
    for key, colour in GROUPS:
        text = colours.text_colour_for(colour)
        assert colours.contrast_ratio(colour, text) >= 4.5, f"{key} {colour} on {text}"
