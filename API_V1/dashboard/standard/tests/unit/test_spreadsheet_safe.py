"""CSV cells a spreadsheet must not run.

    python -m pytest tests/unit/test_spreadsheet_safe.py -q

A crawled page decides what the archive holds, and a watched page's name is
typed by whoever reaches the form; both end up in the exported files. A
cell that begins with `=`, `+`, `-`, `@` or a tab is a formula to Excel and
LibreOffice, so every CSV writer of the dashboard goes through one function
- and this file pins that all three do.
"""

from __future__ import annotations

import doctest

import pytest

from app import textclean
from app.routers import api_export, api_sources
from app.textclean import spreadsheet_safe


def test_doctests():
    failed, _ = doctest.testmod(textclean)
    assert failed == 0


@pytest.mark.parametrize("cell", [
    "=HYPERLINK(\"http://evil.example\",\"open\")",
    "=cmd|' /C calc'!A0",
    "+cmd|' /C calc'!A0",
    "-cmd|' /C calc'!A0",
    "@SUM(A1:A9)",
    "\t=1+1",
    "\r=1+1",
])
def test_a_formula_is_quoted(cell):
    assert spreadsheet_safe(cell) == "'" + cell


@pytest.mark.parametrize("cell", ["-5", "+3.5", "-1,5", "-1e3", "-12%", "+.5",
                                  "Apple Inc.", "0", "", "a=b", "AT&T"])
def test_a_number_and_ordinary_text_are_left_alone(cell):
    assert spreadsheet_safe(cell) == cell


def test_every_writer_uses_it():
    """The two `_cell` helpers, and drilldown.csv, which is checked in the
    flow suite against real rows."""
    assert api_export._cell("=1+1") == "'=1+1"
    assert api_export._cell(["=1", "-2"]) == "'=1; -2"
    assert api_sources._cell("=1+1") == "'=1+1"
    assert api_export._cell(-5) == "-5"


def test_the_drilldown_file_name_fits_a_header():
    """A project name is whatever the archive holds, and the header the file
    name goes into holds latin-1: a name with letters outside it, passed
    through unchanged, is a 500 on the download."""
    from app.routers.api_diagrams import _slug
    assert _slug("北京 Alpha") == "alpha"
    assert _slug("Zürich (Test)") == "z-rich-test"
    assert _slug("") == "all"
    assert len(_slug("x" * 100)) == 40
    _slug("北京").encode("latin-1")
