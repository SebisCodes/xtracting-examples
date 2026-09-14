"""One concept, one word: the archive's "source" is not the crawler's page.

"Sources" named two unrelated things in the same top bar. The Dashboard card
("Sources 5 in the last year"), the Diagrams tab ("How many sources were
written in each period"), the Diagrams scope and the Events rows
("Source: src:battery") all mean an ARCHIVED DOCUMENT by it. The button
beside them opened the crawler's configuration - the list pages the crawler
watches - which is a different thing entirely; and Query then called the
very same archived rows "Documents (1 of 1)". A customer who read "Sources 5"
and pressed Sources landed on a page about crawling, and had three names for
two things.

The archive keeps the word: Dashboard, Diagrams, Events and Query all say
*Sources*. The crawler's list pages are *Watched pages* everywhere - the nav
button, both headings, the Log's filter, and the sentences the crawler writes
into that log.

This scanner is what keeps the next edit from putting one of them back. It
reads what a person is shown: the visible text of the templates, the string
literals of the scripts, and the message literals of the crawler's log rows.
Comments and identifiers are not scanned - `source_id`, `bigint_fk_source`
and `scraper_config.sources` are column names, and a database column is not
vocabulary.

    python -m pytest tests/unit/test_one_word_per_concept.py -q
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from app.routers.pages import VIEWS
from test_vocabulary_guard import string_literals_of_js, visible_text_of_template

ROOT = Path(__file__).resolve().parents[4]
APP = Path(__file__).resolve().parents[2] / "app"

#: The word this dashboard reserves for an archived document.
ARCHIVE_WORD = re.compile(r"\bsources?\b", re.I)

#: `${…}` inside a template literal is code, not the sentence around it:
#: `Watched page ${item.source.id}` says "Watched page" to a reader. It is
#: taken out rather than blanked, so that `/api/sources/${id}/enable` closes
#: up into one word and is thrown away with the other addresses below.
_INTERPOLATION = re.compile(r"\$\{[^}]*\}")
#: A literal with no space left in it is an identifier, a class or a path.
_ONE_WORD = re.compile(r"^\S*$")
_SQL = re.compile(r"\b(INSERT INTO|SELECT|VALUES|UPDATE |DELETE FROM)\b")

#: The views that are about the crawler, in the order a person meets them.
CRAWLER_TEMPLATES = ("sources.html", "source_edit.html", "logs.html")
CRAWLER_SCRIPTS = ("sources.js", "logs.js", "linkpicker.js", "filepicker.js",
                   "patterns.js")
#: The crawler writes these sentences into `monitoring.scraper_errors`, and
#: the Log view shows them word for word - so they are dashboard text that
#: happens to live in another component.
CRAWLER_SENTENCES = (ROOT / "crawler" / "app" / "errorlog.py",)


def _phrases(texts) -> list[str]:
    """The chunks of `texts` that are sentences rather than identifiers."""
    out = []
    for text in texts:
        for line in str(text).splitlines():
            line = _INTERPOLATION.sub("", line).strip()
            if line and not _ONE_WORD.match(line):
                out.append(line)
    return out


def _hits(phrases) -> list[str]:
    return [p for p in phrases if ARCHIVE_WORD.search(p)]


def test_the_top_bar_never_uses_one_word_for_two_things():
    labels = [label for _, label, _ in VIEWS]
    assert len(labels) == len(set(labels)), labels
    crawler = {view: label for view, label, _ in VIEWS}["sources"]
    assert not ARCHIVE_WORD.search(crawler), (
        f"the nav calls the crawler's page {crawler!r}, which is also what the "
        "Dashboard, Diagrams and Events call an archived document")
    assert crawler == "Watchlist", crawler
    # …and the heading it opens uses it too, with the rows it holds named
    # underneath: a Watchlist of watched pages.
    overview = (APP / "templates" / "sources.html").read_text(encoding="utf-8")
    assert f"<h1>{crawler}</h1>" in overview, crawler


@pytest.mark.parametrize("name", CRAWLER_TEMPLATES)
def test_a_watched_page_is_never_called_a_source_on_screen(name):
    source = (APP / "templates" / name).read_text(encoding="utf-8")
    hits = _hits(_phrases(visible_text_of_template(source)))
    assert not hits, f"{name} still calls a watched page a source: {hits}"


@pytest.mark.parametrize("name", CRAWLER_SCRIPTS)
def test_the_scripts_of_those_views_say_it_too(name):
    source = (APP / "static" / "js" / name).read_text(encoding="utf-8")
    hits = _hits(_phrases(string_literals_of_js(source)))
    assert not hits, f"{name} still calls a watched page a source: {hits}"


def _message_literals(path: Path) -> list[str]:
    """Every string constant of a module except its docstrings.

    Docstrings and comments explain the code to the next programmer and may
    say whatever the database says; these are the strings that reach a
    customer.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if (isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                and body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            docstrings.add(id(body[0].value))
    return [node.value for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
            and id(node) not in docstrings and not _SQL.search(node.value)]


@pytest.mark.parametrize("path", CRAWLER_SENTENCES, ids=lambda p: p.name)
def test_the_crawler_writes_the_same_word_into_the_log(path):
    hits = _hits(_phrases(_message_literals(path)))
    assert not hits, f"{path.name} writes 'source' into the log view: {hits}"


def test_the_scanner_still_bites(tmp_path):
    """A positive control: the filters above throw a great deal away, and a
    scanner that has stopped finding anything is not a passing test."""
    assert _hits(_phrases(["Add a source"])) == ["Add a source"]
    # …and the three shapes it is right to walk past.
    assert _hits(_phrases(["source_id"])) == []
    assert _hits(_phrases(["Watched page ${item.source.id}"])) == []
    assert _hits(_phrases(["/api/sources/${sourceId}/enable"])) == []

    module = tmp_path / "sample.py"
    module.write_text('"""A docstring about a source."""\n'
                      'ROW = {"message": "This source was refused"}\n'
                      'SQL = "SELECT text_name FROM scraper_config.sources"\n',
                      encoding="utf-8")
    assert _hits(_phrases(_message_literals(module))) == ["This source was refused"]
