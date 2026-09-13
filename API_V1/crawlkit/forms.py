"""The form of an address: a pattern with the variable parts taken out.

THE OBSERVATION EVERYTHING RESTS ON: the hits on a list page occur many
times and look alike; the imprint occurs
once. Nobody has to know what a hit looks like - it is enough to see that
something appears REPEATEDLY in the same form. `split_form()` computes that
form; `Pattern` is a form that may additionally be *pinned* at some positions
(only `de`, or only `oc`/`cc`), which is how `learn()` separates the ticked
links from their un-ticked twins.

WHAT IS STORED. `scraper_config.source_patterns.json_form` holds the pattern
as `to_json()` writes it - that is the truth. `text_regex` is only a
rendering of it for the eye and for `psql`; `matches()` evaluates the tuple
and never the regex, so a pattern can never "drift" between the two. The
equivalence of the two views is a test, not a hope.

The rules for a path segment, in this order:

* all digits                       -> ``#``      (an id, a year, a page)
* contains a digit or > 24 chars   -> ``*``      (a slug: ``bmw-320d-12345678``,
                                                 ``neubau-einfamilienhaus-musterweg``)
* a two-letter language code       -> ``{lang}`` (``de``, ``fr``, ``en`` ...
                                                 from `LANG_CODES`; ``oc`` and
                                                 ``cc`` in fedlex paths are
                                                 literals, which is what lets
                                                 them be pinned and widened)
* anything else                    -> the literal, lower-cased

Query parameter NAMES belong to the form, their values do not - ``?id=1`` and
``?id=2`` are the same kind, ``?id=1`` and ``?lang=de`` are not. Paging
parameters (``page``, ``ep``, ``currentPage`` ...) are removed from the form
and reported separately, so that page 2 of a list has the same form as page 1.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import NamedTuple
from urllib.parse import parse_qsl, quote_plus, urlsplit

#: Query keys that only turn the page. Compared case-insensitively:
#: fedlex writes ``currentPage``, most others ``page``.
PAGING_KEYS = frozenset({
    "page", "currentpage", "p", "pn", "s", "seite", "offset", "start", "ep",
    "pg", "skip",
})

#: A path segment longer than this is a title slug, not a fixed path piece.
#: Measured against real municipal sites.
LONG_SEGMENT = 24

#: The two-letter codes a site switches its interface between. A plain
#: "any two letters" rule made ``oc`` (fedlex's official compilation) a
#: language and ``/eli/oc/#/#/{lang}`` impossible to tell from
#: ``/eli/cc/#/#/{lang}``. The list is what European sites actually use;
#: a code that is missing simply stays a literal, which pinning handles.
LANG_CODES = frozenset({
    "de", "fr", "it", "rm", "en", "es", "pt", "nl", "pl", "cs", "sk", "sl",
    "hu", "ro", "bg", "hr", "sr", "bs", "sq", "mk", "el", "tr", "ru", "uk",
    "be", "lt", "lv", "et", "fi", "sv", "da", "no", "nb", "nn", "is", "ga",
    "cy", "ca", "eu", "gl", "ar", "he", "fa", "hi", "ur", "bn", "ta", "th",
    "vi", "id", "ms", "ko", "ja", "zh",
})

_DIGITS_RE = re.compile(r"[0-9]+")


@dataclass(frozen=True)
class Segment:
    """One position of a pattern.

    ``kind`` is one of ``lit`` (the literal text, in ``value``), ``num``
    (``#``), ``any`` (``*``) or ``lang`` (``{lang}``). Only ``lit`` carries a
    value; the others are described entirely by their kind.

    >>> Segment.of("2024")
    Segment(kind='num', value='')
    >>> Segment.of("bmw-320d-12345678")
    Segment(kind='any', value='')
    >>> Segment.of("de")
    Segment(kind='lang', value='')
    >>> Segment.of("oc")
    Segment(kind='lit', value='oc')
    >>> Segment.of("News")
    Segment(kind='lit', value='news')
    """

    kind: str
    value: str = ""

    @staticmethod
    def of(text: str) -> "Segment":
        if _DIGITS_RE.fullmatch(text):
            return Segment("num")
        if len(text) > LONG_SEGMENT or any(c.isdigit() for c in text):
            return Segment("any")
        if text.lower() in LANG_CODES:
            return Segment("lang")
        return Segment("lit", text.lower())

    def accepts(self, text: str) -> bool:
        """Does this segment describe the raw path piece ``text``?

        This is the tuple-side twin of `regex_piece()`; the two must agree on
        every input, which `tests/test_rules.py` checks on generated URLs.

        >>> Segment("num").accepts("12"), Segment("num").accepts("1a")
        (True, False)
        >>> Segment("lit", "news").accepts("NEWS")
        True
        """
        if self.kind == "num":
            return bool(_DIGITS_RE.fullmatch(text))
        if self.kind == "any":
            # Any piece at all. `Segment.of()` only *produces* ``*`` for a
            # piece with a digit or over-length, but once a position is a
            # wildcard it must accept tomorrow's slug too - that is the
            # point of `learn()` generalising a cluster of slugs into it.
            return bool(text)
        if self.kind == "lang":
            return text.lower() in LANG_CODES
        return text.lower() == self.value

    @property
    def label(self) -> str:
        return {"num": "#", "any": "*", "lang": "{lang}"}.get(self.kind, self.value)

    def regex_piece(self) -> str:
        if self.kind == "num":
            return r"[0-9]+"
        if self.kind == "any":
            return r"[^/?]+"
        if self.kind == "lang":
            # The code list spelled out, so that the regex and the tuple
            # agree on "oc" - the regex is a rendering, not a second rule.
            return "(?:" + "|".join(sorted(LANG_CODES)) + ")"
        return re.escape(self.value)

    def to_json(self) -> dict:
        return {"kind": self.kind, "value": self.value} if self.value else {"kind": self.kind}

    @staticmethod
    def from_json(data: dict) -> "Segment":
        return Segment(str(data["kind"]), str(data.get("value", "")))


class Form(NamedTuple):
    """What `split_form()` returns: the pattern, the raw segment values (lower
    case, for pinning) and the paging parameter as it was written in the URL.
    """

    pattern: "Pattern"
    values: tuple[str, ...]
    paging_param: str | None


@dataclass(frozen=True)
class Pattern:
    """A form, optionally pinned at some positions.

    ``pinned`` maps a segment position to the values allowed there - a tuple
    of (position, values) pairs so that the pattern stays hashable and can be
    a dict key in `learn()`. ``#`` and ``*`` positions are never pinned; that
    is a rule of `learn()`, not of this class, because the class must still
    load whatever an older row holds.

    >>> p = split_form("https://www.fedlex.admin.ch/eli/oc/2024/123/de").pattern
    >>> p.label()
    '/eli/oc/#/#/{lang}'
    >>> p.pin(4, ["de"]).label()
    '/eli/oc/#/#/de'
    >>> p.pin(1, ["oc", "cc"]).label()
    '/eli/(cc|oc)/#/#/{lang}'
    >>> p.pin(4, ["de"]).matches("https://www.fedlex.admin.ch/eli/oc/2024/7/fr")
    False
    >>> p.pin(4, ["de"]).matches("https://www.fedlex.admin.ch/eli/oc/2024/7/de")
    True

    Round trip through JSON, which is how the pattern lives in the database:

    >>> Pattern.from_json(p.pin(4, ["de"]).to_json()) == p.pin(4, ["de"])
    True
    """

    host: str
    segments: tuple[Segment, ...]
    query_keys: tuple[str, ...] = ()
    pinned: tuple[tuple[int, tuple[str, ...]], ...] = ()

    # -- construction -----------------------------------------------------

    def pin(self, position: int, values) -> "Pattern":
        """A copy pinned at ``position`` to ``values`` (lower-cased, sorted)."""
        if not 0 <= position < len(self.segments):
            raise ValueError(f"position {position} outside the pattern")
        pins = dict(self.pinned)
        pins[position] = tuple(sorted({str(v).lower() for v in values}))
        return Pattern(self.host, self.segments, self.query_keys,
                       tuple(sorted(pins.items())))

    def unpinned(self) -> "Pattern":
        return Pattern(self.host, self.segments, self.query_keys, ())

    @property
    def pins(self) -> dict[int, tuple[str, ...]]:
        return dict(self.pinned)

    # -- views ------------------------------------------------------------

    def label(self) -> str:
        """The human rendering: ``/rent/#``, ``/{lang}/d/*/#``, ``/d?id&lang``.

        >>> split_form("https://a.example/d?id=7&lang=de").pattern.label()
        '/d?id&lang'
        >>> split_form("https://a.example/").pattern.label()
        '/'
        """
        pins = self.pins
        pieces = []
        for i, seg in enumerate(self.segments):
            if i in pins:
                values = pins[i]
                pieces.append(values[0] if len(values) == 1 else "(" + "|".join(values) + ")")
            else:
                pieces.append(seg.label)
        text = "/" + "/".join(pieces)
        if self.query_keys:
            text += "?" + "&".join(self.query_keys)
        return text

    def to_regex(self) -> str:
        """The regex rendering, anchored on the canonical URL.

        Compile it with ``re.IGNORECASE`` (or use it as ``~*`` in psql):
        literals are lower-cased in the tuple, and the canonical URL keeps
        the path's case. Paging parameters may appear anywhere in the sorted
        query, so they are allowed between every two keys - which is why the
        rendering is long; it is complete, not a sketch.

        >>> rx = split_form("https://a.example/rent/4001231").pattern.to_regex()
        >>> rx.startswith('^https?://a\\\\.example/rent/[0-9]+/?(?:\\\\?(?:currentpage|ep|')
        True
        >>> rx.endswith(')=[^&]*)*)?$')
        True
        """
        paging = "(?:" + "|".join(sorted(PAGING_KEYS)) + r")=[^&]*"
        pins = self.pins
        path = ""
        for i, seg in enumerate(self.segments):
            if i in pins:
                path += "/(?:" + "|".join(re.escape(v) for v in pins[i]) + ")"
            else:
                path += "/" + seg.regex_piece()
        path += "/?"
        if self.query_keys:
            keys = [re.escape(quote_plus(k)) for k in self.query_keys]
            query = rf"\?(?:{paging}&)*"
            query += rf"(?:&{paging})*&".join(f"{k}=[^&]*(?:&{k}=[^&]*)*" for k in keys)
            query += rf"(?:&{paging})*"
        else:
            query = rf"(?:\?{paging}(?:&{paging})*)?"
        return "^https?://" + re.escape(self.host) + path + query + "$"

    def compile(self) -> re.Pattern:
        """`to_regex()` compiled the way it is meant to be used."""
        return re.compile(self.to_regex(), re.IGNORECASE)

    def matches(self, url: str) -> bool:
        """Evaluate the tuple against a URL (canonical or not).

        >>> p = split_form("https://a.example/rent/4001231").pattern
        >>> p.matches("https://a.example/rent/4001299?ep=3")
        True
        >>> p.matches("https://a.example/rent/apartment/city-basel/matching-list")
        False
        """
        return self.matches_form(split_form(url))

    def matches_form(self, form: Form) -> bool:
        other = form.pattern
        if other.host != self.host or other.query_keys != self.query_keys:
            return False
        if len(form.values) != len(self.segments):
            return False
        pins = self.pins
        for i, (seg, raw) in enumerate(zip(self.segments, form.values)):
            if i in pins:
                if raw not in pins[i]:
                    return False
            elif not seg.accepts(raw):
                return False
        return True

    # -- JSON -------------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "host": self.host,
            "segments": [s.to_json() for s in self.segments],
            "query_keys": list(self.query_keys),
            "pinned": {str(pos): list(values) for pos, values in self.pinned},
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)

    @staticmethod
    def from_json(data) -> "Pattern":
        """Accepts the string `to_json()` wrote, or the dict `to_dict()` wrote
        (psycopg hands a jsonb column back as a dict)."""
        if isinstance(data, (str, bytes)):
            data = json.loads(data)
        if not isinstance(data, dict):
            raise ValueError("a pattern is a JSON object")
        segments = tuple(Segment.from_json(s) for s in data.get("segments", ()))
        pins = tuple(sorted(
            (int(pos), tuple(sorted(str(v).lower() for v in values)))
            for pos, values in (data.get("pinned") or {}).items()
        ))
        return Pattern(str(data["host"]), segments,
                       tuple(str(k) for k in data.get("query_keys", ())), pins)


def split_form(url: str) -> Form:
    """The form of an address, its raw segment values, and its paging key.

    >>> f = split_form("https://a.example/News/2024/12345?ep=2")
    >>> f.pattern.label(), f.values, f.paging_param
    ('/news/#/#', ('news', '2024', '12345'), 'ep')
    >>> split_form("https://a.example/d?id=7&lang=de").pattern.query_keys
    ('id', 'lang')

    A blank parameter counts, like it does in `canonical_uri()`:

    >>> split_form("https://a.example/a?druck=&id=7").pattern.query_keys
    ('druck', 'id')
    """
    parts = urlsplit(url)
    values = tuple(piece.lower() for piece in parts.path.split("/") if piece)
    segments = tuple(Segment.of(piece) for piece in parts.path.split("/") if piece)
    keys: set[str] = set()
    paging: str | None = None
    for key, _value in parse_qsl(parts.query, keep_blank_values=True):
        if key.lower() in PAGING_KEYS:
            paging = paging or key
        else:
            keys.add(key)
    return Form(Pattern(parts.netloc.lower(), segments, tuple(sorted(keys))),
                values, paging)


def form_of(url: str) -> Pattern:
    """The pattern alone - what two addresses of the same kind share.

    >>> form_of("https://a.example/news/2024/12345") == form_of("https://a.example/news/2023/99")
    True
    """
    return split_form(url).pattern
