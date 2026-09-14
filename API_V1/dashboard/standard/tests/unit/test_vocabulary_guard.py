"""No Buy/Sell/Put/Call anywhere a person can read it.

Scans what the UI shows: the chart registry (titles, descriptions, dataset
labels), the text and label attributes of every template, and the string
literals of every first-party script. Code and comments are not scanned -
`options` and `callback` are not vocabulary - and vendored libraries are
not ours. A positive control makes sure the scanner itself still bites.

    python -m pytest tests/unit/test_vocabulary_guard.py -q
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app import charts, vocabulary
from app.vocabulary import find_forbidden, find_glyphs

APP = Path(__file__).resolve().parents[2] / "app"
TEMPLATES = APP / "templates"
JS = APP / "static" / "js"

# Attributes whose value a person sees or hears.
_LABEL_ATTRS = ("title", "aria-label", "placeholder", "alt", "value", "aria-description", "data-empty")
_TAG = re.compile(r"<[^>]+>")
_JINJA = re.compile(r"\{%.*?%\}|\{\{.*?\}\}|\{#.*?#\}", re.S)
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)
_ATTR = re.compile(r"\b(" + "|".join(_LABEL_ATTRS) + r')\s*=\s*"([^"]*)"')
_JS_COMMENT = re.compile(r"/\*.*?\*/|(?<![:\\])//[^\n]*", re.S)
_JS_STRING = re.compile(r'"((?:[^"\\\n]|\\.)*)"|\'((?:[^\'\\\n]|\\.)*)\'|`((?:[^`\\]|\\.)*)`', re.S)


def visible_text_of_template(source: str) -> list[str]:
    source = _HTML_COMMENT.sub(" ", source)
    source = _JINJA.sub(" ", source)
    out = [m.group(2) for m in _ATTR.finditer(source)]
    out.append(_TAG.sub(" ", source))
    return out


# String literals that are DOM vocabulary, not ours: the <option> element
# and the ARIA role "option", alone or inside a CSS selector. Nothing else.
_DOM_TOKEN = re.compile(r'^(option|[a-z]*\[role=\\?"?option\\?"?\]([^\s]*)?)$')
# An attribute name inside a message ("data-options is not JSON") is code too.
_DATA_ATTR = re.compile(r"\bdata-[a-z][a-z0-9-]*")


def string_literals_of_js(source: str) -> list[str]:
    source = _JS_COMMENT.sub(" ", source)
    literals = [next(g for g in m.groups() if g is not None) for m in _JS_STRING.finditer(source)]
    return [_DATA_ATTR.sub(" ", s) for s in literals if not _DOM_TOKEN.match(s.strip())]


def _hits(texts: list[str]) -> set[str]:
    hits: set[str] = set()
    for t in texts:
        hits.update(find_forbidden(t))
    return hits


def _glyphs(texts: list[str]) -> set[str]:
    found: set[str] = set()
    for t in texts:
        found.update(find_glyphs(t))
    return found


def test_positive_control():
    assert find_forbidden("Strong Buy - a call option on the warrant") == ["buy", "call", "option", "warrant"]
    assert find_forbidden("High Impact Opportunities") == ["high impact opportunity", "opportunity"]
    assert _hits(visible_text_of_template('<p title="Sell now">Hold</p>')) == {"sell", "hold"}
    assert _hits(string_literals_of_js('const x = "Put"; // sell\n')) == {"put"}
    assert string_literals_of_js('a("option"); b(\'li[role="option"]\'); c("Sell option")') == ["Sell option"]
    assert _hits(string_literals_of_js('w("data-options is not JSON")')) == set()
    assert find_forbidden("callback optional holdings") == []
    with pytest.raises(ValueError):
        vocabulary.assert_clean("Buy", "control")


def test_vocabulary_constants_are_clean():
    for name in ("OUTLOOKS", "SENTIMENTS", "RATING_VALUES", "IMPORTANCE_LEVELS", "RELATIONS", "TRUST",
                 "NEGATIVE_LABEL", "NEUTRAL_LABEL", "POSITIVE_LABEL", "HIGH_RELEVANCE_LABEL",
                 "MARKET_INSIGHTS_TITLE"):
        value = getattr(vocabulary, name)
        texts = list(value) if isinstance(value, tuple) else [value]
        assert not _hits(texts), name


def test_chart_registry_is_clean():
    for spec in charts.all_charts():
        # The HINT is scanned with the rest: it is the sentence the ⓘ beside
        # a chart's title opens, so it is as visible as the title itself -
        # and it is the longest piece of prose in the registry, which is
        # exactly where a stray "hold" or "opportunity" gets written.
        texts = [spec.title, spec.description, spec.hint,
                 *(label for _, label in spec.datasets)]
        hits = _hits(texts)
        assert not hits, f"{spec.tab}/{spec.id}: {sorted(hits)}"
    assert not _hits([title for _, title in charts.TABS])
    # And the sentence beside each legend entry, which the catalogue serves
    # the same way (app/charts/__init__.py: DATASET_HINTS).
    for dataset_id, said in charts.DATASET_HINTS.items():
        assert not _hits([said]), f"dataset {dataset_id}: {sorted(_hits([said]))}"


# ── The other half of the rule: no trend glyph on a valence chart ────
#
# Ratings and Market Insights are drawn on the diverging red-to-green scale
# now, because it is the encoding people read fastest. Red and green on
# market data already read as an instruction to anyone trained in finance,
# so the rest of the picture may not add to it - an up arrow beside a green
# bar is a claim about the future that the archive never made. The list of
# glyphs and the reason live in app/vocabulary.py (FORBIDDEN_GLYPHS); this
# runs it over the registry and over the views that draw those charts.
#
# LEFT AND RIGHT ARE NOT ON THE LIST: "◀ Last 7 days ▶" steps the period,
# "Parent → Child" names the direction of a connection, and neither says
# anything about a value.

# The views a valence chart is drawn on or reached from. Not every template
# in the product: an up arrow in the crawler's link picker is a caret, not a
# statement about a market.
VALENCE_VIEWS = ("diagrams.html", "dashboard.html", "events.html")
VALENCE_SCRIPTS = ("charts.js", "diagrams.js", "drilldown.js", "legend.js", "dashboard.js")


def test_the_glyph_scanner_bites():
    assert find_glyphs("Rising \u25b2 12%") == ["\u25b2"]
    assert find_glyphs("up \u2191 down \u2193") == ["\u2191", "\u2193"]
    # Stepping a period and naming a direction of a connection are not trends.
    assert find_glyphs("\u25c0 Last 7 days \u25b6") == []
    assert find_glyphs("Parent \u2192 Child") == []
    assert find_glyphs("Rising outlook, positive reporting") == []
    with pytest.raises(ValueError):
        vocabulary.assert_no_glyphs("Rising \u2191", "control")
    # Every glyph on the list has a name, so a failure says what was found.
    assert set(vocabulary.FORBIDDEN_GLYPHS) == set(vocabulary.GLYPH_NAMES)


def test_no_chart_draws_a_direction_on_top_of_its_colour():
    for spec in charts.all_charts():
        texts = [spec.title, spec.description, spec.hint, spec.category,
                 *(label for _, label in spec.datasets)]
        found = _glyphs(texts)
        assert not found, f"{spec.tab}/{spec.id}: {sorted(found)}"
    assert not _glyphs([title for _, title in charts.TABS])
    assert not _glyphs(list(charts.DATASET_HINTS.values()))
    for name in ("OUTLOOKS", "SENTIMENTS", "RATING_VALUES", "IMPORTANCE_LEVELS"):
        assert not _glyphs(list(getattr(vocabulary, name))), name


@pytest.mark.parametrize("name", VALENCE_VIEWS)
def test_a_view_that_draws_a_valence_chart_carries_no_trend_glyph(name):
    path = TEMPLATES / name
    if not path.exists():
        pytest.skip(f"{name} is not in this build")
    found = _glyphs(visible_text_of_template(path.read_text(encoding="utf-8")))
    assert not found, f"{name}: {sorted(found)}"


@pytest.mark.parametrize("name", VALENCE_SCRIPTS)
def test_a_script_that_draws_a_valence_chart_carries_no_trend_glyph(name):
    path = JS / name
    if not path.exists():
        pytest.skip(f"{name} is not in this build")
    found = _glyphs(string_literals_of_js(path.read_text(encoding="utf-8")))
    assert not found, f"{name}: {sorted(found)}"


def _templates() -> list[Path]:
    return sorted(TEMPLATES.rglob("*.html")) if TEMPLATES.is_dir() else []


def _scripts() -> list[Path]:
    return sorted(p for p in JS.rglob("*.js") if "vendor" not in p.parts) if JS.is_dir() else []


@pytest.mark.parametrize("path", _templates(), ids=lambda p: p.name)
def test_template_is_clean(path: Path):
    hits = _hits(visible_text_of_template(path.read_text(encoding="utf-8")))
    assert not hits, f"{path.name}: {sorted(hits)}"


@pytest.mark.parametrize("path", _scripts(), ids=lambda p: p.name)
def test_script_strings_are_clean(path: Path):
    hits = _hits(string_literals_of_js(path.read_text(encoding="utf-8")))
    assert not hits, f"{path.name}: {sorted(hits)}"


# The seed writes rows a person then reads: a colour group's description is
# printed beside its swatch and on the printed page. A word that would be
# caught in a template is just as wrong when the database hands it over, so
# the quoted strings of our own SQL are scanned too. Comments are stripped -
# they may name the words they warn about - and '' inside a literal is the
# SQL escape for a quote, not a terminator.
SQL = Path(__file__).resolve().parents[2] / "sql"
_SQL_COMMENT = re.compile(r"--[^\n]*")
_SQL_STRING = re.compile(r"'((?:[^']|'')*)'")


def string_literals_of_sql(source: str) -> list[str]:
    return [m.group(1).replace("''", "'") for m in _SQL_STRING.finditer(_SQL_COMMENT.sub(" ", source))]


def _sql_files() -> list[Path]:
    return sorted(SQL.rglob("*.sql")) if SQL.is_dir() else []


@pytest.mark.parametrize("path", _sql_files(), ids=lambda p: p.name)
def test_sql_literals_are_clean(path: Path):
    hits = _hits(string_literals_of_sql(path.read_text(encoding="utf-8")))
    assert not hits, f"{path.name}: {sorted(hits)}"


def test_sql_scanner_bites():
    assert string_literals_of_sql("-- a comment naming Buy\nSELECT 'Strong Buy', 'it''s fine';") == [
        "Strong Buy", "it's fine"]
    assert _hits(string_literals_of_sql("SELECT 'Strong Buy';")) == {"buy"}
