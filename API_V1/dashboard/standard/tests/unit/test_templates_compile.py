"""Every Jinja template compiles.

    python -m pytest tests/unit/test_templates_compile.py -q

A template with a syntax error is not caught by anything else that runs in
seconds: the app starts, the routes answer, and the page it belongs to
returns 500. A `{#- … -#}` comment written INSIDE the dict literal a
`{% set %}` is building is a syntax error, because a comment is a tag and
not an expression, and the page answers

    jinja2.exceptions.TemplateSyntaxError: unexpected char '#' at 4611

The UI suite would find it, at the cost of a browser and several
minutes; this finds it in a few milliseconds without a database. It compiles
rather than renders: rendering needs a request, a context and an archive,
and the failure this guards against is in the source text.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader

TEMPLATES = Path(__file__).resolve().parents[2] / "app" / "templates"


def _names() -> list[str]:
    return sorted(str(p.relative_to(TEMPLATES)) for p in TEMPLATES.rglob("*.html"))


def test_there_are_templates_to_compile():
    """A loader pointed at the wrong folder would make every test below pass
    by having nothing to do."""
    assert len(_names()) > 5, TEMPLATES


@pytest.mark.parametrize("name", _names())
def test_template_compiles(name: str):
    Environment(loader=FileSystemLoader(str(TEMPLATES))).get_template(name)


def test_the_compiler_bites(tmp_path: Path):
    (tmp_path / "broken.html").write_text('{%- set x = {"a": 1, {#- no -#} "b": 2} -%}',
                                          encoding="utf-8")
    with pytest.raises(Exception, match="unexpected char"):
        Environment(loader=FileSystemLoader(str(tmp_path))).get_template("broken.html")
