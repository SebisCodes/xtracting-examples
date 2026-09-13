"""The published vocabulary, and the words that must not appear.

The archive holds market insights in a vocabulary that DESCRIBES and never
instructs: an outlook points a direction, a sentiment says how strongly a
source read. This dashboard shows that vocabulary and no other - never
Buy/Sell, Put/Call, "high impact opportunity", which are instructions to a
reader rather than a description of what a document said.

The COLOUR is a separate decision and it goes the other way: ratings
and market insights are drawn on the diverging red-to-green scale, because
that is the encoding people read fastest (app/charts/__init__.py). What
follows from that is the second list here - a red bar and a green bar are
already as much as the picture is allowed to say, so no arrow, triangle or
trend glyph may be drawn on top of them.

FORBIDDEN_TERMS and FORBIDDEN_GLYPHS are both enforced by
tests/unit/test_vocabulary_guard.py, which scans the chart registry, the
templates and the JavaScript. Keep the lists here so the guard and the code
it protects cannot disagree about them.
"""

from __future__ import annotations

import re

MARKET_INSIGHTS_TITLE = "Market Insights"

# Order is the order the charts stack them in.
OUTLOOKS = ("Rising", "Neutral", "Declining", "Unset")
SENTIMENTS = ("Very Negative", "Negative", "Slightly Negative", "Neutral",
              "Slightly Positive", "Positive", "Very Positive", "Unset")
# The rating scale by name only; the ORDER of ratings in a chart comes from
# processed_data.rating_values.float_value (project or ''), not from here.
RATING_VALUES = ("Egregious", "Very Bad", "Bad", "Neutral", "Good", "Very Good", "Excellent")
IMPORTANCE_LEVELS = ("Not Important", "Low Importance", "Medium Importance",
                     "High Importance", "Critical Importance")
RELATIONS = ("Direct", "Indirect", "Unrelated")
TRUST = ("Trusted", "Unknown", "Untrusted")

# The three labels of the diverging scheme, reused for ratings and sentiment.
# They describe the REPORTING, which is what the archive holds.
NEGATIVE_LABEL = "Negative reporting"
NEUTRAL_LABEL = "Neutral reporting"
POSITIVE_LABEL = "Positive reporting"
HIGH_RELEVANCE_LABEL = "High relevance"


def sign_of(float_value: float | None) -> str:
    """negative / neutral / positive from a rating_values.float_value; a value
    the vocabulary does not know (NULL) is shown as neutral rather than dropped."""
    if float_value is None or float_value == 0:
        return "neutral"
    return "negative" if float_value < 0 else "positive"


def sentiment_sign(name: str) -> str:
    n = (name or "").lower()
    if "negative" in n:
        return "negative"
    if "positive" in n:
        return "positive"
    return "neutral"


# Word-boundary, case-insensitive, singular or plural.
FORBIDDEN_TERMS = ("buy", "sell", "put", "call", "hold", "warrant",
                   "high impact opportunity", "indicator", "option", "opportunity")


def _term_pattern(term: str) -> re.Pattern[str]:
    # Singular or plural, including y -> ies ("opportunities"), as a whole
    # word or whole phrase.
    if term.endswith("y"):
        body = re.escape(term[:-1]) + "(?:y|ies)"
    else:
        body = re.escape(term) + "(?:s|es)?"
    return re.compile(r"\b" + body + r"\b", re.IGNORECASE)


_FORBIDDEN_PATTERNS = tuple((t, _term_pattern(t)) for t in FORBIDDEN_TERMS)


def find_forbidden(text: str) -> list[str]:
    """Every forbidden term in a piece of user-visible text, as the canonical
    lower-case term, in order of appearance.

    >>> find_forbidden("Rising outlook, positive reporting")
    []
    >>> find_forbidden("Strong Buy - call options")
    ['buy', 'call', 'option']
    >>> find_forbidden("High Impact Opportunities")
    ['high impact opportunity', 'opportunity']
    >>> find_forbidden("placeholder callback optional")   # inside words is fine
    []
    """
    hits: list[tuple[int, str]] = []
    for term, pattern in _FORBIDDEN_PATTERNS:
        for m in pattern.finditer(text or ""):
            hits.append((m.start(), term))
    return [term for _, term in sorted(hits)]


def assert_clean(text: str, where: str = "text") -> None:
    hits = find_forbidden(text)
    if hits:
        raise ValueError(f"{where} uses forbidden vocabulary: {', '.join(sorted(set(hits)))}")


# ── The other half of the rule: no trend glyphs ──────────────
#
# Ratings and Market Insights are drawn on the diverging red-to-green scale,
# because that is the encoding people read fastest. Red and green on market
# data already read as buy and sell to anyone
# trained in finance, so the rest of the picture must not add to it: the
# wording stays as the archive publishes it (FORBIDDEN_TERMS above), and
# NOTHING on those views may draw a direction on top of the colour.
#
# An up arrow beside a green bar is a second, louder statement than the bar:
# a bar says "the reporting read positive over this period", an arrow says
# "it is going up" - which is a claim about the future the archive never
# made. A triangle is the same thing with the tail cut off.
#
# WHAT IS AND IS NOT ON THE LIST. Up, down and the two diagonals, filled or
# hollow, plus the chart emoji. LEFT AND RIGHT ARE NOT: "◀ Last 7 days ▶"
# steps the period and "Parent → Child" names the direction of a connection,
# and neither of them says anything about a value. Nor are the small
# carets ▾ ▴, which open and close a disclosure everywhere in this product.
#
# tests/unit/test_vocabulary_guard.py runs this over the chart registry and
# over the views that draw those charts.
FORBIDDEN_GLYPHS: tuple[str, ...] = (
    "\u2191",  # ↑
    "\u2193",  # ↓
    "\u2197",  # ↗
    "\u2198",  # ↘
    "\u21D1",  # ⇑
    "\u21D3",  # ⇓
    "\u2B06",  # ⬆
    "\u2B07",  # ⬇
    "\u25B2",  # ▲
    "\u25B3",  # △
    "\u25BC",  # ▼
    "\u25BD",  # ▽
    "\U0001F4C8",  # 📈
    "\U0001F4C9",  # 📉
)

# The names, for a failure message somebody can act on: a test that says
# "\u25bc" sends the reader to a code-point table.
GLYPH_NAMES: dict[str, str] = {
    "\u2191": "an up arrow", "\u2193": "a down arrow",
    "\u2197": "a rising arrow", "\u2198": "a falling arrow",
    "\u21D1": "a double up arrow", "\u21D3": "a double down arrow",
    "\u2B06": "an up arrow", "\u2B07": "a down arrow",
    "\u25B2": "an up triangle", "\u25B3": "a hollow up triangle",
    "\u25BC": "a down triangle", "\u25BD": "a hollow down triangle",
    "\U0001F4C8": "a rising chart", "\U0001F4C9": "a falling chart",
}


def find_glyphs(text: str) -> list[str]:
    """Every trend glyph in a piece of user-visible text, in order.

    >>> find_glyphs("Rising outlook")
    []
    >>> find_glyphs("Rising \u25b2 12%")
    ['\u25b2']
    >>> find_glyphs("\u25c0 Last 7 days \u25b6")   # stepping the period is not a trend
    []
    """
    body = text or ""
    hits = [(body.index(g), g) for g in FORBIDDEN_GLYPHS if g in body]
    return [g for _, g in sorted(hits)]


def assert_no_glyphs(text: str, where: str = "text") -> None:
    hits = find_glyphs(text)
    if hits:
        named = ", ".join(f"{g} ({GLYPH_NAMES.get(g, 'a trend glyph')})" for g in hits)
        raise ValueError(f"{where} draws a direction on top of the colour: {named}")
