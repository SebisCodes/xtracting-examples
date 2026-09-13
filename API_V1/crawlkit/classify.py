"""What a link on a list page is, before any rule is applied.

Every link is one of `CLASSES`, decided in this order - the first that fits:

* ``list_self``   the list page itself (canonical URLs equal)
* ``pagination``  page 2, 3 ... of the same list: the same form with a paging
                  key, or an address the page's own "next" link points at
* ``legal``       imprint, terms, privacy ... (`legal.py`)
* ``asset``       stylesheets, scripts, images, fonts, media
* ``external``    another host than the list page (``www.`` does not count)
* ``file``        a document file - pdf, docx, xlsx, csv, txt, md, zip
* ``candidate``   everything else: what the patterns are learned from

`learn()` uses the class to keep pagination, the list itself and assets out of
the positives and negatives; `decide()` uses it to reject legal, asset and
external links before the mode is even consulted; the link picker shows it as
a tag. Dashboard and crawler share this one function so that what the picker
called "pagination" is what the crawler follows as pagination.

The "next" link itself is found by the I/O half (it needs the HTML). This
module only knows the label words, so that both halves agree on them.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from crawlkit.forms import Form, split_form
from crawlkit.hashing import canonical_uri
from crawlkit.legal import is_legal

CLASSES = ("list_self", "pagination", "legal", "asset", "external", "file",
           "candidate")

ASSET_EXTENSIONS = frozenset({
    "css", "js", "mjs", "map", "png", "jpg", "jpeg", "gif", "webp", "avif",
    "svg", "ico", "bmp", "tif", "tiff", "woff", "woff2", "ttf", "otf", "eot",
    "mp4", "webm", "mp3", "ogg", "wav", "m4a", "mov", "avi",
})

#: Files the crawler can turn into text (`files.py`) or at least name.
FILE_EXTENSIONS = frozenset({
    "pdf", "docx", "doc", "xlsx", "xls", "csv", "txt", "md", "zip", "pptx",
    "odt", "ods", "rtf",
})

# ---- the label words of a "next page" link ------------------------------

#: Labels that mean "next page". Kept short: they are checked against the
#: WHOLE link text, and "Weitere Informationen" is not a next page.
NEXT_WORDS = ("weiter", "nächste", "naechste", "nächste seite", "next",
              "next page", "vor", "mehr anzeigen", "mehr laden", "suivant",
              "page suivante", "avanti", "successiva")
NEXT_SYMBOLS = ("›", "»", "→", ">", ">>", "▸", "▶")

#: What is explicitly NOT the next page. Without this brake "vor" matches
#: inside "vorherige", and the crawl walks back and forth between page one
#: and two until the page cap stops it.
BACK_WORDS = ("zurück", "zurueck", "vorherige", "vorher", "previous", "prev",
              "erste", "first", "précédent", "precedente", "‹", "«", "←", "<", "<<")

#: Longer than this is a sentence, not a page turner.
MAX_NEXT_LABEL = 24


def looks_like_next_label(text: str) -> bool:
    """Is this link text a "next page" label?

    >>> looks_like_next_label("Weiter"), looks_like_next_label("›")
    (True, True)
    >>> looks_like_next_label("nächste Seite »")
    True
    >>> looks_like_next_label("vorherige"), looks_like_next_label("Weitere Informationen")
    (False, False)
    """
    text = re.sub(r"\s+", " ", text or "").strip().lower()
    if not text or len(text) > MAX_NEXT_LABEL:
        return False
    if any(back in text for back in BACK_WORDS):
        return False
    if text in NEXT_SYMBOLS or text in NEXT_WORDS:
        return True
    # As a whole word, so that "weiter" does not match "weitere
    # informationen" - there it opens a sentence and does not stand alone.
    return any(re.fullmatch(rf"{re.escape(w)}\s*[›»→>]?", text) for w in NEXT_WORDS)


def _extension(url: str) -> str:
    last = urlsplit(url).path.rsplit("/", 1)[-1]
    return last.rsplit(".", 1)[-1].lower() if "." in last else ""


def _bare_host(netloc: str) -> str:
    host = netloc.lower()
    return host[4:] if host.startswith("www.") else host


class Classifier:
    """`classify()` for many links against one list page.

    The list page's canonical URL and form are computed once; on a page with
    two thousand links that is the difference between "instant" and "why is
    the picker spinning".
    """

    def __init__(self, list_url: str, next_urls=()) -> None:
        self.list_url = canonical_uri(list_url)
        self.list_form: Form = split_form(self.list_url)
        self.list_host = _bare_host(self.list_form.pattern.host)
        self.next_urls = frozenset(canonical_uri(u) for u in next_urls if u)
        #: The paging key the list page itself carries, if it is already page
        #: 2 or was saved with ``?page=1``.
        self.paging_param = self.list_form.paging_param

    def classify(self, url: str, form: Form | None = None) -> str:
        canon = canonical_uri(url)
        if canon == self.list_url:
            return "list_self"
        form = form or split_form(canon)
        if canon in self.next_urls or (
                form.pattern == self.list_form.pattern
                and (form.paging_param or self.paging_param)):
            # The same list with a paging key - or, when the list page was
            # saved as page 2, the link back to page 1 that has none.
            return "pagination"
        if is_legal(canon):
            return "legal"
        ext = _extension(canon)
        if ext in ASSET_EXTENSIONS:
            return "asset"
        if _bare_host(form.pattern.host) != self.list_host:
            return "external"
        if ext in FILE_EXTENSIONS:
            return "file"
        return "candidate"


def classify(url: str, list_url: str, next_urls=()) -> str:
    """The class of one link on the list page ``list_url``.

    >>> L = "https://www.homegate.ch/rent/apartment/city-zurich/matching-list"
    >>> classify(L, L)
    'list_self'
    >>> classify(L + "?ep=2", L)
    'pagination'
    >>> classify("https://www.homegate.ch/impressum", L)
    'legal'
    >>> classify("https://www.homegate.ch/static/app.css", L)
    'asset'
    >>> classify("https://cdn.other.example/x", L)
    'external'
    >>> classify("https://www.homegate.ch/files/expose-4001231.pdf", L)
    'file'
    >>> classify("https://www.homegate.ch/rent/4001231", L)
    'candidate'

    A "next" link the page itself declared counts as pagination even when
    its address has a different shape:

    >>> classify("https://www.homegate.ch/liste/seite-2", L, next_urls=["https://www.homegate.ch/liste/seite-2"])
    'pagination'
    """
    return Classifier(list_url, next_urls).classify(url)


__all__ = ["CLASSES", "Classifier", "classify", "looks_like_next_label",
           "ASSET_EXTENSIONS", "FILE_EXTENSIONS", "NEXT_WORDS", "BACK_WORDS",
           ]
