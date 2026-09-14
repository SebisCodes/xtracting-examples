"""Addresses that are never a hit: imprint, terms, privacy, cookie policy.

Kept short on purpose: every word here is matched as a whole path piece
against practically every site, and a long
list eventually catches something real - and then hits go missing without
anyone noticing. The words are matched against the PATH only; a host called
``contact.example`` is not a legal page.

What this list does NOT do: a footer with three links under one path piece
(``/service/agb``, ``/service/kontakt``, ``/service/team``) cannot be told from
a three-element hit list by shape alone. The list catches the first two; for
the third, the un-ticked link in the picker becomes a reject in `learn()`.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

#: German and English, because both are what sites actually use.
LEGAL_WORDS = (
    "impressum", "imprint", "legal-notice", "legal", "agb", "gtc", "terms",
    "terms-of-use", "terms-and-conditions", "datenschutz", "privacy",
    "privacy-policy", "disclaimer", "rechtliches", "rechtliche", "rechtlichen",
    "cookie", "cookies", "cookie-policy", "sitemap", "kontakt", "contact",
    "login", "anmelden", "newsletter", "suche", "search", "hilfe", "faq",
    "barrierefreiheit", "accessibility",
)

# A word counts when it starts a path piece and is followed by the end, a
# slash, a dot, or a hyphen - "/privacy-policy" and "/impressum.html" are
# legal, "/searchlight" is not. Multi-word entries are listed longest first
# so the alternation reports the whole word.
LEGAL_RE = re.compile(
    r"(?:^|/)(" + "|".join(re.escape(w) for w in sorted(LEGAL_WORDS, key=len, reverse=True))
    + r")(?:[./?#-]|$)", re.IGNORECASE)


def legal_word(url: str) -> str | None:
    """The legal word the path contains, or None.

    >>> legal_word("https://a.example/impressum")
    'impressum'
    >>> legal_word("https://a.example/en/privacy-policy.html")
    'privacy-policy'
    >>> legal_word("https://a.example/rent/4001231") is None
    True
    >>> legal_word("https://contact.example/rent/1") is None
    True
    """
    match = LEGAL_RE.search(urlsplit(url).path)
    return match.group(1).lower() if match else None


def is_legal(url: str) -> bool:
    """Does the path contain a legal word as a whole piece?

    >>> is_legal("https://a.example/agb"), is_legal("https://a.example/agbau/1")
    (True, False)
    """
    return legal_word(url) is not None
