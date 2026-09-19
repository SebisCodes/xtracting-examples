"""The phone layout: one sheet, one button, nothing hidden.

    python -m pytest tests/unit/test_phone_layout.py -q

Under 48rem the top bar folds into the brand and a Menu button, and
css/phone.css narrows every view (docs/DESIGN.md, "A phone gets the same
pages, folded"). Three things hold it together and none of them is
exercised by a laptop-sized browser, which is what the UI suite runs:

  * the sheet is loaded LAST and only under 48rem - loaded earlier, a
    view's own stylesheet wins every tie and the phone rules do nothing;
    loaded without the media condition, they reach a laptop;
  * the button and the panel are tied by `aria-controls`, and the panel's
    visibility follows `aria-expanded` alone - the stylesheet reads the
    attribute layout.js writes, so what is announced and what is shown
    cannot disagree;
  * the sheet hides nothing but that panel: a table that does not fit is
    scrolled, not cut, and every `display: none` in it is accounted for.

These are read from the source text, like tests/unit/test_templates_compile.py:
the page cannot be rendered here (it needs an archive), and the facts under
test are in the text.
"""

from __future__ import annotations

import re
from pathlib import Path

APP = Path(__file__).resolve().parents[2] / "app"
LAYOUT = (APP / "templates" / "_layout.html").read_text(encoding="utf-8")
APP_CSS = (APP / "static" / "css" / "app.css").read_text(encoding="utf-8")
PHONE_CSS = (APP / "static" / "css" / "phone.css").read_text(encoding="utf-8")
LAYOUT_JS = (APP / "static" / "js" / "layout.js").read_text(encoding="utf-8")


def _without_comments(css: str) -> str:
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


# ── The sheet ─────────────────────────────────────────────────────────


def test_the_phone_sheet_is_the_last_stylesheet_and_only_under_48rem():
    link = re.search(r'<link rel="stylesheet" href="\{\{ static\(\'css/phone.css\'\) \}\}"'
                     r'\s+media="([^"]+)">', LAYOUT)
    assert link, "the layout does not link css/phone.css"
    assert link.group(1) == "screen and (max-width: 48rem)", link.group(1)
    head_block = LAYOUT.index("{% block head %}")
    assert link.start() > head_block, \
        "phone.css is linked before a view's own stylesheets, so the view wins every tie"
    assert LAYOUT.index("</head>") > link.start()


def test_the_phone_sheet_hides_only_the_closed_panel():
    rules = _without_comments(PHONE_CSS)
    hidden = [m.group(0) for m in re.finditer(r"[^{}]+\{[^{}]*display:\s*none[^{}]*\}", rules)]
    assert hidden == [m for m in hidden if 'aria-expanded="false"] + .topbar-menu' in m], \
        f"phone.css hides something that is not the closed menu panel: {hidden}"
    assert len(hidden) == 1, hidden


def test_the_phone_sheet_scrolls_a_table_rather_than_cutting_it():
    rules = _without_comments(PHONE_CSS)
    assert "overflow: hidden" not in rules and "overflow-x: hidden" not in rules
    table_box = re.search(r"\.table-scroll,\s*\.tables-scroll\s*\{([^}]*)\}", rules)
    assert table_box, "no rule for the table's scroll box"
    assert "local" in table_box.group(1) and "scroll" in table_box.group(1), \
        "the scroll box draws no edge shadow, so nothing says the table continues"


# ── The button and the panel ──────────────────────────────────────────


def test_the_menu_button_names_the_panel_it_opens():
    button = re.search(r'<button[^>]*class="topbar-menu-button"[^>]*>', LAYOUT)
    assert button, "no Menu button in the layout"
    assert 'aria-controls="topbar-menu"' in button.group(0)
    assert 'aria-expanded="false"' in button.group(0), "the panel must start closed"
    assert 'title="Menu"' in button.group(0), "the button carries no tooltip"
    assert '<div class="topbar-menu" id="topbar-menu">' in LAYOUT


def test_the_panel_holds_the_selector_and_the_navigation():
    panel = LAYOUT.index('<div class="topbar-menu" id="topbar-menu">')
    tools = LAYOUT.index('class="topbar-tools toolbar"')
    nav = LAYOUT.index('<nav aria-label="Views">')
    header_end = LAYOUT.index("</header>")
    assert panel < tools < nav < header_end, \
        "the selector and the nav are not both inside the panel"
    button = LAYOUT.index('class="topbar-menu-button"')
    assert button < panel, "the panel must follow the button: the stylesheet reads `button + panel`"


def test_the_panel_is_not_a_box_on_a_laptop():
    """`display: contents` is what keeps the bar's flex layout the one it
    always had; a real box would put the selector and the nav in one flex
    item and undo the row the bar is measured for."""
    rules = _without_comments(APP_CSS)
    assert re.search(r"\.topbar-menu\s*\{\s*display:\s*contents;\s*\}", rules)
    assert re.search(r"\.topbar-menu-button\s*\{\s*display:\s*none;\s*\}", rules)
    assert ".topbar-menu > nav { flex-basis: 100%; }" in rules, \
        "the nav is a child of the panel now; a rule on `.topbar-inner > nav` reaches nothing"


def test_the_script_writes_only_the_attribute_the_sheet_reads():
    fn = re.search(r"function initTopbarMenu\(\)\s*\{(.*?)\n\}", LAYOUT_JS, re.S)
    assert fn, "layout.js has no initTopbarMenu"
    body = fn.group(1)
    assert 'setAttribute("aria-expanded"' in body
    assert "classList" not in body and ".hidden" not in body, \
        "the panel's state must live in aria-expanded alone"
    assert '"Escape"' in body and "button.focus()" in body, \
        "Escape closes the panel and hands the focus back, like the Export menu"
    assert "initTopbarMenu();" in LAYOUT_JS.split("function init()")[1]
