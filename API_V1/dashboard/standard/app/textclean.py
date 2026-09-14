"""What the archive stored, as a person would read it.

THE EXTRACTION KEEPS WHAT THE PAGE SAID, AND A PAGE SAYS `&auml;`.

A crawled document is HTML, and the text taken out of it carries whatever
escapes the page used: `W&auml;rtsil&auml; Corporation`, `AT&amp;T`,
`Bureau&nbsp;Veritas`. The archive is right to store what it was given - a
store that rewrites its input cannot be checked against the source - so the
decoding belongs here, at the edge where a value becomes something to read.

It is not a parser and it is not a sanitizer. `html.unescape` turns a
character reference into the character it names and leaves everything else
exactly as it is, including a bare `&` that names nothing; the browser is
never handed markup, because every one of these values reaches the page as
`textContent` or as a JSON string.

`&nbsp;` is the one that needs a word of its own: it decodes to U+00A0, which
looks like a space, sorts like a space, and is not one - a label with it in
the middle cannot be searched for by typing the name. It becomes an ordinary
space here, which is what the document meant by it.
"""

from __future__ import annotations

import html
import re

#: Only work on values that could carry a reference. `&` is the cheapest test
#: there is, and the values that reach this are names, types and summaries -
#: almost none of them have one.
_HAS_REF = re.compile(r"&[#a-zA-Z0-9]")


def plain(value):
    """One value, decoded. Anything that is not a string is returned as it is,
    so callers can hand rows through without asking what a column holds.

    >>> plain("M&uuml;ller &amp; S&ouml;hne")
    'Müller & Söhne'
    >>> plain("AT&amp;T")
    'AT&T'
    >>> plain("Smith & Sons")
    'Smith & Sons'
    >>> plain("Bureau&nbsp;Veritas")
    'Bureau Veritas'
    >>> plain(None), plain(7)
    (None, 7)
    """
    if not isinstance(value, str) or not _HAS_REF.search(value):
        return value
    return html.unescape(value).replace(" ", " ")


# ── What a spreadsheet would run ────────────────────────────

#: A cell that begins with one of these is a formula to Excel and
#: LibreOffice, whatever the file called it. Tab and the line breaks are in
#: the set because a leading one hides the character after it from the
#: check while the sheet still reads past it.
_FORMULA_STARTS = ("=", "+", "-", "@", "\t", "\r", "\n")
_NUMBER = re.compile(r"^[+-]?(\d+([.,]\d*)?|[.,]\d+)(e[+-]?\d+)?%?$", re.IGNORECASE)


def spreadsheet_safe(text: str) -> str:
    """A CSV cell that a spreadsheet will show and never evaluate.

    A crawled page decides what the archive holds, so a name in it can be
    `=HYPERLINK(...)` or `=cmd|' /C calc'!A0` - and a file that reaches a
    spreadsheet with that at the start of a cell runs it when opened. The
    quote in front is the spreadsheet convention for "this is text", and it
    is added only where it is needed: a number that happens to be negative
    is a number, and `-5` must stay one.

    >>> spreadsheet_safe("=1+1")
    "'=1+1"
    >>> spreadsheet_safe("-5"), spreadsheet_safe("+3.5%"), spreadsheet_safe("-1e3")
    ('-5', '+3.5%', '-1e3')
    >>> spreadsheet_safe("-cmd|' /C calc'!A0")
    "'-cmd|' /C calc'!A0"
    >>> spreadsheet_safe("@SUM(A1)"), spreadsheet_safe("\\t=1")
    ("'@SUM(A1)", "'\\t=1")
    >>> spreadsheet_safe("Apple Inc."), spreadsheet_safe("")
    ('Apple Inc.', '')
    """
    if not text or not text.startswith(_FORMULA_STARTS):
        return text
    if text[0] in "+-" and _NUMBER.match(text):
        return text
    return "'" + text
