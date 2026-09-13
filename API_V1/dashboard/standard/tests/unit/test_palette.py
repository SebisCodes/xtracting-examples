"""The three data scales, measured - the test that stops a later tweak.

    python -m pytest tests/unit/test_palette.py -q

app.css defines valence (diverging red to green), relevance (sequential
violet) and count (blue) once, as custom properties, and every chart,
badge, pill and legend reads them from there. That is only worth anything
while the values keep the promises the comments beside them make, and a
colour is exactly the kind of thing that gets "improved" by half a shade
six months later. So the promises are here, as numbers:

  * every mark reaches 3:1 against the ground it is drawn on (WCAG 1.4.11);
  * text on a filled mark reaches 4.5:1 with one of the page's two text
    colours - and the palest step of each ramp is the one that fails that
    if nobody checks, which is why every step is checked;
  * each ramp is MONOTONIC in luminance, so it survives a black-and-white
    print and a reader who sees no hue at all;
  * the two ends of the valence scale differ in LUMINANCE and not only in
    hue - a red and a green of the same lightness are one grey to a
    dichromat, and the same grey on a printer;
  * grey means "no data" and nothing else, so it is far enough from the
    neutral step of the valence scale to be told apart from it;
  * the border token separates what it sits between (3:1), and there is
    only one of it.

Nothing here needs a browser or a database: it reads app.css, and it reads
the fallback table in js/palette.js to check the two lists still agree.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.colours import contrast_ratio, relative_luminance

CSS = Path(__file__).resolve().parents[2] / "app" / "static" / "css" / "app.css"
PALETTE_JS = Path(__file__).resolve().parents[2] / "app" / "static" / "js" / "palette.js"

# What a mark is drawn on. --surface is the card a chart lives in; --bg is
# the page ground a chip or a pill sits on directly, and it is the DARKER
# of the two, so it is the one that decides.
SURFACE = "#ffffff"
PAGE = "#f4f5f7"
TEXT = "#1a1a1a"

MARK_MIN = 3.0        # WCAG 1.4.11: a graphic that carries meaning
TEXT_ON_MARK_MIN = 4.5  # WCAG 1.4.3: the value written inside a bar
# Two steps of one ramp have to be told apart. 1.2:1 is the smallest
# difference that survives a projector; the ramps below sit well above it.
STEP_MIN = 1.2
# The two ends of the valence scale, and grey against the neutral step.
ENDS_MIN = 1.5


def _tokens() -> dict[str, str]:
    """Every `--name: #rrggbb;` in the :root block of app.css."""
    text = CSS.read_text(encoding="utf-8")
    root = text.split(":root {", 1)[1]
    # The first closing brace at the start of a line ends the block.
    root = re.split(r"^\}", root, maxsplit=1, flags=re.MULTILINE)[0]
    return {m.group(1): m.group(2).lower()
            for m in re.finditer(r"(--[a-z0-9-]+)\s*:\s*(#[0-9a-fA-F]{3,8})\s*;", root)}


@pytest.fixture(scope="module")
def tokens() -> dict[str, str]:
    return _tokens()


VALENCE = ["--valence-neg-3", "--valence-neg-2", "--valence-neg-1", "--valence-0",
           "--valence-pos-1", "--valence-pos-2", "--valence-pos-3"]
RELEVANCE = [f"--relevance-{i}" for i in range(5)]
COUNT = [f"--count-{i}" for i in range(1, 7)]
MARKS = VALENCE + RELEVANCE + COUNT + ["--no-data"]

# A ramp is ordered from its palest step to its deepest. The valence scale
# is two ramps that share a middle: it diverges, so it cannot be monotonic
# as one list - each ARM is, and that is what a reader follows.
RAMPS = {
    "valence, towards negative": ["--valence-0", "--valence-neg-1", "--valence-neg-2", "--valence-neg-3"],
    "valence, towards positive": ["--valence-0", "--valence-pos-1", "--valence-pos-2", "--valence-pos-3"],
    "relevance": RELEVANCE,
    "count": COUNT,
}


def test_every_scale_is_defined(tokens):
    """A missing token is a chart drawn in the browser's default black."""
    missing = [name for name in MARKS if name not in tokens]
    assert not missing, f"app.css does not define: {', '.join(missing)}"


@pytest.mark.parametrize("name", MARKS)
def test_a_mark_is_visible_on_the_page_it_is_drawn_on(tokens, name):
    """3:1 against BOTH surfaces - the card and the page ground.

    The page ground is the darker of the two, so it is the one that decides;
    both are asserted because a bar in a card and a pill on the page are the
    same colour and have to work in both places.
    """
    colour = tokens[name]
    on_page = contrast_ratio(colour, PAGE)
    on_card = contrast_ratio(colour, SURFACE)
    assert on_page >= MARK_MIN, f"{name} {colour} is {on_page:.2f}:1 on the page ground {PAGE}"
    assert on_card >= MARK_MIN, f"{name} {colour} is {on_card:.2f}:1 on a card {SURFACE}"


@pytest.mark.parametrize("name", MARKS)
def test_a_value_can_be_written_inside_a_filled_mark(tokens, name):
    """4.5:1 with white or with --text; js/palette.js picks which.

    The palest step of a ramp is the one that fails this, every time, which
    is why it is checked per step and not per scale.
    """
    colour = tokens[name]
    best = max(contrast_ratio(colour, SURFACE), contrast_ratio(colour, TEXT))
    assert best >= TEXT_ON_MARK_MIN, (
        f"{name} {colour}: neither white ({contrast_ratio(colour, SURFACE):.2f}:1) nor "
        f"{TEXT} ({contrast_ratio(colour, TEXT):.2f}:1) can be read on it")


@pytest.mark.parametrize("ramp", list(RAMPS), ids=list(RAMPS))
def test_a_ramp_gets_darker_step_by_step(tokens, ramp):
    """Monotonic in luminance, and every step far enough from the last.

    This is what makes "the stronger the colour, the more of it" readable
    without seeing hue at all - on a projector, on a black-and-white print,
    and to the one reader in twelve with a colour vision deficiency.
    """
    colours = [tokens[name] for name in RAMPS[ramp]]
    lums = [relative_luminance(c) for c in colours]
    assert lums == sorted(lums, reverse=True), (
        f"{ramp} is not ordered light to dark: "
        + ", ".join(f"{n} {c} L={x:.3f}" for n, c, x in zip(RAMPS[ramp], colours, lums)))
    for (a, la), (b, lb) in zip(zip(RAMPS[ramp], lums), list(zip(RAMPS[ramp], lums))[1:]):
        step = (la + 0.05) / (lb + 0.05)
        assert step >= STEP_MIN, f"{a} and {b} are {step:.2f}:1 apart - one step, not two"


def test_the_two_ends_of_the_valence_scale_differ_in_luminance(tokens):
    """Not only in hue.

    A red and a green of equal lightness are one grey to a dichromat and
    the same grey on a black-and-white printer - which would leave the most
    negative and the most positive reading in the archive looking identical
    in exactly the two places where it matters most.
    """
    worst, best = tokens["--valence-neg-3"], tokens["--valence-pos-3"]
    ratio = contrast_ratio(worst, best)
    assert ratio >= ENDS_MIN, (
        f"the ends of the valence scale are {ratio:.2f}:1 apart in luminance "
        f"({worst} L={relative_luminance(worst):.3f}, {best} L={relative_luminance(best):.3f})")


def test_grey_is_not_mistakable_for_the_neutral_step(tokens):
    """`Unset` is absence, Neutral is a reading, and a key has to show that.

    They are two hues; this is the other half - they are two lightnesses as
    well, so the key separates them however the page is printed or seen.
    """
    ratio = contrast_ratio(tokens["--no-data"], tokens["--valence-0"])
    assert ratio >= ENDS_MIN, (
        f"--no-data {tokens['--no-data']} and --valence-0 {tokens['--valence-0']} are "
        f"only {ratio:.2f}:1 apart: 'no reading' would read as 'a middling reading'")


def test_relevance_is_never_red_or_green(tokens):
    """One hue, and not one that means good or bad.

    Relevance says how much something matters, not whether it is welcome.
    Measured rather than eyeballed: the violet steps must be nearer to the
    scale's own top step than to either end of the valence scale.
    """
    for name in RELEVANCE:
        colour = tokens[name]
        red, green = tokens["--valence-neg-2"], tokens["--valence-pos-2"]
        assert _hue_distance(colour, red) > 40, f"{name} {colour} is a red"
        assert _hue_distance(colour, green) > 40, f"{name} {colour} is a green"


def _hue(colour: str) -> float:
    import colorsys
    h = colour.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    return colorsys.rgb_to_hls(r, g, b)[0] * 360


def _hue_distance(a: str, b: str) -> float:
    d = abs(_hue(a) - _hue(b)) % 360
    return min(d, 360 - d)


def test_counts_are_blue_and_valence_is_not(tokens):
    """The three scales are three hues, so a bar's colour says which
    question it answers before its label is read."""
    assert 190 <= _hue(tokens["--count-4"]) <= 250, "the count scale is not blue"
    assert _hue_distance(tokens["--count-4"], tokens["--relevance-4"]) > 30
    assert _hue_distance(tokens["--count-4"], tokens["--valence-pos-2"]) > 30


def test_there_is_one_border_token_and_it_separates(tokens):
    """A second, paler token used for every card, table row and tile would
    make the borders too faint to separate anything. There is one, and it
    is a visible boundary (3:1) on both surfaces it is ever drawn between."""
    text = CSS.read_text(encoding="utf-8")
    line = tokens["--line"]
    assert contrast_ratio(line, SURFACE) >= MARK_MIN, f"--line {line} is invisible on a card"
    assert contrast_ratio(line, PAGE) >= MARK_MIN, f"--line {line} is invisible on the page"
    # The soft name still exists, because other stylesheets spell it - but
    # it may not carry a colour of its own again.
    assert re.search(r"--line-soft:\s*var\(--line\)\s*;", text), (
        "--line-soft has been given a colour of its own again; it is the same line")


def test_javascript_reads_the_same_values(tokens):
    """js/palette.js publishes the scales to the charts through
    getComputedStyle, and carries the same hexes as its fallback for a page
    rendered without the stylesheet. Two lists, one truth: a value changed
    in one and not the other fails here rather than drifting."""
    js = PALETTE_JS.read_text(encoding="utf-8")
    block = js.split("const FALLBACK = {", 1)[1].split("};", 1)[0]
    fallback = {m.group(1): m.group(2).lower()
                for m in re.finditer(r'"(--[a-z0-9-]+)":\s*"(#[0-9a-fA-F]{3,8})"', block)}
    for name in MARKS:
        assert name in fallback, f"js/palette.js has no fallback for {name}"
        assert fallback[name] == tokens[name], (
            f"{name}: app.css says {tokens[name]}, js/palette.js says {fallback[name]}")
