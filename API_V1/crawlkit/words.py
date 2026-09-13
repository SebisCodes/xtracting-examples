"""Numbers written the way a person writes them.

One link, twelve links - never "12 link(s)". The bracketed plural is what a
program writes when nobody has decided what the sentence says; it reads as
machine output, and these sentences are read by the customer twice: once as
a verdict banner in the dashboard's source editor and once as a row in the
Log view, where the same text arrives from `monitoring.scraper_errors`.

It lives in crawlkit rather than in either service because both write those
sentences. A dashboard that tidies the brackets away on the way to the
screen leaves the Log view - the same sentence, from the same code - still
saying "12 document(s)".
"""

from __future__ import annotations


def plural(count: int, one: str, many: str = "") -> str:
    """`count` with the word that belongs to it.

    `many` defaults to `one` with an "s", which covers every word these
    modules use; it is an argument because "one entry / many entries" is one
    rename away and a helper that cannot say it would be worked around.

    >>> plural(1, "page")
    '1 page'
    >>> plural(3, "page")
    '3 pages'
    >>> plural(0, "link")
    '0 links'
    >>> plural(1, "entry", "entries")
    '1 entry'
    >>> plural(2, "entry", "entries")
    '2 entries'
    """
    number = int(count)
    return f"{number} {one if number == 1 else (many or one + 's')}"
