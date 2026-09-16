"""The Tables view's strip is the chart registry's tab list, word for word.

    python -m pytest tests/unit/test_tables_view.py -q

templates/tables.html writes its eight tabs out so the page has its
navigation before any JavaScript runs - the same decision diagrams.html
takes, pinned the same way (test_chart_registry.py). The Tables view and
the Diagrams view are two readings of one archive, and a tab that exists on
one and not the other, or is spelled differently, is a page a reader cannot
find their way between.
"""

from __future__ import annotations

import re
from pathlib import Path

from app import charts

TEMPLATES = Path(__file__).resolve().parents[2] / "app" / "templates"
TEMPLATE = TEMPLATES / "tables.html"
JS = Path(__file__).resolve().parents[2] / "app" / "static" / "js"


def _tabs_of(template: Path) -> list[tuple[str, str]]:
    source = template.read_text(encoding="utf-8")
    block = re.search(r"set tabs = \[(.*?)\] -%\}", source, re.S)
    assert block, f"{template.name}: the tab list is not where the template says it is"
    return re.findall(r'\("([a-z_]+)",\s*"([^"]+)"\)', block.group(1))


def test_the_strip_is_exactly_the_registry_tabs_in_order():
    assert _tabs_of(TEMPLATE) == [(t, charts.TAB_TITLES[t]) for t in charts.TAB_IDS]


def test_the_two_views_carry_the_same_strip():
    """Diagrams and Tables are the same eight words in the same order, so a
    reader moving between the two never has to learn a second list."""
    assert _tabs_of(TEMPLATE) == _tabs_of(TEMPLATES / "diagrams.html")


def test_the_strip_is_a_tablist_of_tabs():
    source = TEMPLATE.read_text(encoding="utf-8")
    assert 'role="tablist"' in source
    assert 'role="tab"' in source
    assert 'aria-controls="tables-panel"' in source
    assert 'role="tabpanel"' in source


def test_the_view_has_the_four_sorts_the_api_knows():
    """The select's values are the API's `sort` values; the page module keeps
    the same list, so a sort the page can ask for is one the server answers."""
    source = TEMPLATE.read_text(encoding="utf-8")
    block = re.search(r"set sorts = \[(.*?)\] -%\}", source, re.S)
    assert block
    values = [v for v, _ in re.findall(r'\("([a-z]+)",\s*"([^"]+)"\)', block.group(1))]
    assert values == ["newest", "oldest", "most", "fewest"]
    script = (JS / "tables.js").read_text(encoding="utf-8")
    assert 'const SORTS = ["newest", "oldest", "most", "fewest"];' in script


def test_the_page_loads_the_row_table_sheet_and_the_view_has_its_glyph():
    """The rows are the drilldown's table on a page, so its sheet has to be
    on the page; and the top bar draws every view's own glyph."""
    source = TEMPLATE.read_text(encoding="utf-8")
    assert "css/drilldown.css" in source
    macros = (TEMPLATES / "_macros.html").read_text(encoding="utf-8")
    assert re.search(r'"tables":\s*"M', macros), "the icon macro has no tables glyph"


def test_the_state_carries_the_tables_keys():
    """tab, q, type, sort, buckets and source live in the URL, so a reload,
    a shared link and the Export menu all show the same rows."""
    script = (JS / "state.js").read_text(encoding="utf-8")
    keys = re.search(r"export const KEYS = \[(.*?)\];", script, re.S)
    assert keys
    names = re.findall(r'"([a-z_]+)"', keys.group(1))
    for key in ("tab", "q", "type", "sort", "buckets", "source"):
        assert key in names, key
