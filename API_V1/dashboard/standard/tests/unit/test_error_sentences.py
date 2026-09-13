"""One place finishes the server's error fragments into sentences.

The API answers every failure as `{error, hint}`, and it writes both as
lowercase pieces without a full stop - a toast prints them on two lines and
a heading does not want punctuation. Anything that puts them into running
text has to close them first, or the two run together into "the date range
ends before it starts swap the two dates".

That four-line finishing grew a private copy in one view module after
another, and the copies drifted: some views closed the fragments and let
api.js raise its own toast from the raw ones, so one failure stood on one
screen twice in two registers - "The date range ends before it starts." in
the panel, "the date range ends before it starts" in the corner.

So `sentence()` is exported from api.js, api.js finishes what it toasts, and
a view that also words the failure itself imports that same function. This
guard reads the source rather than the browser: it is the convention, not
the rendering, that regressed.

The Query, Events and Home views are converted. Six other view modules
(diagrams, map, heatmap, graph, logs, sources) still carry a private copy;
they belong to other workers this round and are listed here as fact, not as
a failing expectation - the general rule below is what applies to them.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

JS = Path(__file__).resolve().parents[2] / "app" / "static" / "js"

# The views whose failure wording now comes from api.js.
SHARING = ("query.js", "events.js", "dashboard.js")

_DEFINES = re.compile(r"^\s*(?:export\s+)?function\s+sentence\s*\(", re.MULTILINE)
_IMPORTS_API = re.compile(r"^import\s*\{([^}]*)\}\s*from\s*[\"']\./api\.js[\"'];", re.MULTILINE)


def _imported_from_api(source: str) -> set[str]:
    names: set[str] = set()
    for match in _IMPORTS_API.finditer(source):
        for part in match.group(1).split(","):
            names.add(part.strip().split(" as ")[0].strip())
    return names


def test_api_js_exports_the_one_sentence():
    source = (JS / "api.js").read_text(encoding="utf-8")
    assert "export function sentence(" in source
    # And it uses it on what it shows: a toast raised from the raw fragments
    # is the half of the fault the views cannot prevent themselves.
    toasts = re.findall(r"toast\(([^;]*?)\);", source, re.DOTALL)
    assert toasts, "api.js no longer raises a toast - has the error path moved?"
    for call in toasts:
        assert "sentence(" in call, call


@pytest.mark.parametrize("name", SHARING)
def test_a_sharing_view_imports_the_sentence_and_does_not_keep_one(name: str):
    source = (JS / name).read_text(encoding="utf-8")
    assert not _DEFINES.search(source), f"{name} has grown its own sentence() again"
    assert "sentence" in _imported_from_api(source), f"{name} does not import sentence from api.js"


def _scripts() -> list[Path]:
    return sorted(p for p in JS.rglob("*.js") if "vendor" not in p.parts)


@pytest.mark.parametrize("path", _scripts(), ids=lambda p: p.name)
def test_no_module_both_imports_and_shadows_it(path: Path):
    """The one way the copies can start disagreeing again without anybody
    noticing: an import that a local definition below it shadows."""
    source = path.read_text(encoding="utf-8")
    if "sentence" not in _imported_from_api(source):
        return
    assert not _DEFINES.search(source), f"{path.name} imports sentence() and defines one too"
