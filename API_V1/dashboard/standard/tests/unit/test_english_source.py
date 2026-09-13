"""The code is English - no German identifier anywhere.

A German option key (`wert`, `beschriftung`, `zusatz`, `anzahl`) in a script
that no endpoint in this repository ever sends is the kind of thing that
arrives with a port from another codebase and is never noticed, because the
key is simply never read. This scanner is what keeps it out.

WHAT IS AND IS NOT GERMAN HERE

German *data* is correct and stays: `crawlkit/classify.py` matches the words a
German site writes on its "next page" link, `crawlkit/legal.py` matches
`/rechtliches`, `crawlkit/forms.py` and `patterns.js` list `seite` among the
paging query keys, `typeahead.js` folds umlauts so "zuerich" finds "Zurich",
and comments quote all of it. So the scan works on what is left when comments
and string literals are removed: the identifiers. A German word that a person
chose as a name is a finding; a German word the crawler has to recognise is
not.

The one exception is the visible half of the dashboard - template text and the
strings the browser shows - which is scanned as text, because that is where a
German sentence would actually reach a reader.

It lives in the dashboard's unit suite although it reads `crawlkit/` and
`crawler/app` as well: that suite is the one CI runs on every push without a
database, so one place keeps all three components honest. A German name in the
crawler therefore fails the `dashboard` job - the message says which file.

    python -m pytest tests/unit/test_english_source.py -q
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

DASHBOARD = Path(__file__).resolve().parents[2]
ROOT = DASHBOARD.parents[1]

#: Every first-party tree of the three components this repository builds in
#: Python and JavaScript. `collector/` is older than this plan and not ours to
#: touch; vendored libraries are nobody's to rewrite.
ROOTS = (DASHBOARD / "app", ROOT / "crawlkit", ROOT / "crawler" / "app")
SKIP_PARTS = frozenset({"vendor", "__pycache__", ".pytest_cache", "node_modules"})

#: German words that are unambiguous: none of them is also an English word, an
#: abbreviation this code uses, or a token of HTML, SQL or CSS. `tag`, `art`,
#: `stand`, `name`, `datum` and `alt` are German too and are deliberately NOT
#: here - `text_tag`, `alt=""` and "the datum beside the bar" are English.
GERMAN = (
    # the option-key vocabulary of a German suggestion widget
    "wert", "werte", "beschriftung", "zusatz", "anzahl", "vorschlag",
    "vorschlaege", "auswahl", "eintrag", "eintraege",
    # the words a German UI or a German port would reach for next
    "quelle", "quellen", "einstellung", "einstellungen", "farbe", "farben",
    "gruppe", "gruppen", "zeile", "zeilen", "spalte", "spalten", "tabelle",
    "ueberschrift", "abschnitt", "bereich", "meldung", "fehler", "hinweis",
    "ergebnis", "ergebnisse", "uhrzeit", "menge", "groesse", "hoehe",
    "breite", "dauer", "anfang", "ende", "knopf", "schalter", "feld",
    "felder", "inhalt", "ueberpruefung", "pruefer", "abfrage", "suche",
    # verbs and adverbs, in both spellings
    "abbrechen", "speichern", "loeschen", "löschen", "aendern", "ändern",
    "schliessen", "schließen", "oeffnen", "öffnen", "pruefen", "prüfen",
    "gespeichert", "gefunden", "geladen", "naechste", "nächste",
    "vorherige", "zurueck", "zurück", "seite", "seiten", "groeße", "größe",
)
_WORDS = frozenset(GERMAN)

#: Split an identifier the way a reader does: `text_wert`, `dateAnzahl` and
#: `WERT_MAX` all contain the word `wert`.
_IDENT = re.compile(r"[A-Za-z_$À-ɏ][A-Za-z0-9_$À-ɏ]*")
_CAMEL = re.compile(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|[0-9]+")

# Comments and literals, per language. Python triple quotes first, so that a
# docstring is not eaten one quote at a time.
_PY_NOISE = re.compile(
    r'"""(?:[^"\\]|\\.|"(?!""))*"""'
    r"|'''(?:[^'\\]|\\.|'(?!''))*'''"
    r'|"(?:[^"\\\n]|\\.)*"'
    r"|'(?:[^'\\\n]|\\.)*'"
    r"|#[^\n]*", re.S)
# In JavaScript a `/` starts a regular expression only where a value may
# start; after an identifier, a `)` or a `]` it is division. Looking back at
# the previous non-space character is enough for this code and keeps
# `a / b` from swallowing the rest of the line.
_JS_NOISE = re.compile(
    r"/\*.*?\*/"
    r"|//[^\n]*"
    r'|"(?:[^"\\\n]|\\.)*"'
    r"|'(?:[^'\\\n]|\\.)*'"
    r"|`(?:[^`\\]|\\.)*`"
    r"|(?<=[=(,:;&|!?{}\[+\-*%<>~^])\s*/(?![/*])(?:[^/\\\n\[]|\\.|\[(?:[^\]\\]|\\.)*\])+/[a-z]*", re.S)


def code_of(path: Path) -> str:
    """The file with every comment and every literal blanked out."""
    source = path.read_text(encoding="utf-8", errors="replace")
    noise = _PY_NOISE if path.suffix == ".py" else _JS_NOISE
    # Newlines are kept so a reported line number still means something.
    return noise.sub(lambda m: "\n" * m.group(0).count("\n"), source)


def german_in(text: str) -> set[str]:
    hits: set[str] = set()
    for ident in _IDENT.findall(text):
        parts = {ident.lower()} | {p.lower() for p in _CAMEL.findall(ident)}
        hits |= parts & _WORDS
    return hits


def _sources() -> list[Path]:
    out: list[Path] = []
    for root in ROOTS:
        if not root.is_dir():
            continue  # a component can be checked out on its own
        for path in sorted(root.rglob("*")):
            if path.suffix in (".py", ".js") and not SKIP_PARTS & set(path.parts):
                out.append(path)
    return out


SOURCES = _sources()


def test_there_is_something_to_scan():
    # A path that stopped matching would make every test below vacuous.
    assert len(SOURCES) > 50, [str(p) for p in SOURCES]


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: str(p.relative_to(ROOT)))
def test_identifiers_are_english(path: Path):
    found = german_in(code_of(path))
    assert not found, f"{path.relative_to(ROOT)} names things in German: {sorted(found)}"


def test_scanner_bites():
    # The exact line that was in typeahead.js until this test existed.
    assert german_in('const value = o.value ?? o.wert ?? "";') == {"wert"}
    assert german_in("count = row.anzahl") == {"anzahl"}
    assert german_in("text_naechste_seite = 1") == {"naechste", "seite"}


def test_scanner_leaves_data_and_prose_alone():
    # crawlkit has to know these words; they live in literals and comments.
    assert german_in(code_of(ROOT / "crawlkit" / "classify.py")) == set()
    assert german_in(code_of(ROOT / "crawlkit" / "legal.py")) == set()
    # A literal is data, a comment is prose - neither is a name.
    assert german_in(code_of_text('NEXT = ("weiter", "nächste Seite")\n', ".py")) == set()
    assert german_in(code_of_text('# the Vorschlag list\n', ".py")) == set()
    assert german_in(code_of_text('const k = ["seite"]; // paging keys\n', ".js")) == set()
    # ... but division must not eat the line that follows it.
    assert german_in(code_of_text("const anzahl = a / b;\nconst x = 1;\n", ".js")) == {"anzahl"}


def code_of_text(source: str, suffix: str) -> str:
    noise = _PY_NOISE if suffix == ".py" else _JS_NOISE
    return noise.sub(lambda m: "\n" * m.group(0).count("\n"), source)


# ── The visible half of the dashboard ───────────────────────────────────

TEMPLATES = DASHBOARD / "app" / "templates"
JS = DASHBOARD / "app" / "static" / "js"

_TAG = re.compile(r"<[^>]+>")
_JINJA = re.compile(r"\{%.*?%\}|\{\{.*?\}\}|\{#.*?#\}", re.S)
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)
_LABEL_ATTRS = ("title", "aria-label", "placeholder", "alt", "value",
                "aria-description", "data-empty-label", "data-no-suggestion")
_ATTR = re.compile(r"\b(" + "|".join(_LABEL_ATTRS) + r')\s*=\s*"([^"]*)"')
_JS_COMMENT = re.compile(r"/\*.*?\*/|(?<![:\\])//[^\n]*", re.S)
_JS_STRING = re.compile(r'"((?:[^"\\\n]|\\.)*)"|\'((?:[^\'\\\n]|\\.)*)\'|`((?:[^`\\]|\\.)*)`', re.S)

#: One-word strings are keys, not sentences: `patterns.js` lists `seite`
#: among the paging query parameters a German site uses, and the map sends
#: `"anzahl"`-free option names around. A German *sentence* is what a reader
#: would ever see, and a sentence has a space in it.
def visible_strings(path: Path) -> list[str]:
    source = path.read_text(encoding="utf-8", errors="replace")
    if path.suffix == ".html":
        source = _HTML_COMMENT.sub(" ", source)
        source = _JINJA.sub(" ", source)
        texts = [m.group(2) for m in _ATTR.finditer(source)] + [_TAG.sub(" ", source)]
    else:
        source = _JS_COMMENT.sub(" ", source)
        texts = [next(g for g in m.groups() if g is not None)
                 for m in _JS_STRING.finditer(source)]
    return [t for t in texts if " " in t.strip()]


def _visible_files() -> list[Path]:
    out = sorted(TEMPLATES.rglob("*.html")) if TEMPLATES.is_dir() else []
    out += sorted(p for p in JS.rglob("*.js")) if JS.is_dir() else []
    return out


def german_words_in(texts: list[str]) -> set[str]:
    hits: set[str] = set()
    for text in texts:
        hits |= {w.lower() for w in re.findall(r"[\wÀ-ɏ]+", text)} & _WORDS
    return hits


@pytest.mark.parametrize("path", _visible_files(), ids=lambda p: str(p.relative_to(DASHBOARD)))
def test_visible_text_is_english(path: Path):
    found = german_words_in(visible_strings(path))
    assert not found, f"{path.relative_to(DASHBOARD)} shows German: {sorted(found)}"


def test_visible_scanner_bites():
    assert german_words_in(["Bitte speichern Sie das Ergebnis"]) == {"speichern", "ergebnis"}
    # A single word is a key, not a sentence: the paging parameter list in
    # patterns.js has to name `seite`, and that must not read as a finding.
    assert german_words_in(["seite"]) == {"seite"}          # the word is known
    assert visible_strings(JS / "patterns.js").count("seite") == 0  # never a sentence
