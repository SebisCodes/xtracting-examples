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
