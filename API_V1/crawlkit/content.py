"""HTML in, links and text out.

Two jobs in one file because they share a parser, and kept apart in every
other respect because they fail differently:

  `extract_links`  from the list page to the detail pages. A mistake here
                   means too few or the wrong links, and it shows up in the
                   yield of a run.
  `prepare`        from the detail page to the text that is submitted. A
                   mistake here means navigation and footers in the result -
                   and that costs money on every single extraction, because
                   what is submitted is what is billed.

So `drop_selectors` is not polish, it is accounting: the navigation of a
portal is quickly longer than the entry it surrounds.

WHAT IS HASHED IS WHAT IS SENT. `prepare()` returns the content and its
sha256 together, and nothing between here and the API touches the text again.
That is what lets the dashboard line up `scraper.documents` with
`processed_data.sources.text_content_hash` - two sides of the same number.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup, Comment

from crawlkit.classify import looks_like_next_label
from crawlkit.hashing import canonical_uri, sha256_text

log = logging.getLogger(__name__)

#: Never content, in either format.
ALWAYS_DROP = ("script", "style", "noscript", "template", "svg", "iframe")

#: What is furniture on almost every page. Deliberately conservative - better
#: a little navigation too much than one entry too few. Whoever wants a
#: sharper cut sets a content selector on the source.
COMMON_CHROME = ("nav", "header", "footer",
                 "[role=navigation]", "[role=banner]", "[role=contentinfo]",
                 ".cookie", ".cookies", "#cookie", ".skip-link",
                 ".breadcrumb", ".breadcrumbs")

#: The attributes kept in `html` format. Everything else - classes, data-*,
#: inline styles, event handlers - is layout or tracking, and both are paid
#: for by the character in the extraction.
KEEP_ATTRIBUTES = ("href", "src", "alt", "title")

_SKIP_SCHEMES = ("#", "javascript:", "mailto:", "tel:", "data:", "blob:")


@dataclass(frozen=True)
class Link:
    """One link as it stood on the page: the address, and what it was called.

    The text is what the link picker shows and what the pagination detection
    reads; without it a list of two thousand addresses is unusable to a person.
    """

    url: str
    text: str = ""


@dataclass(frozen=True)
class Prepared:
    """The document as it will be submitted."""

    title: str
    content: str
    content_hash: str
    char_count: int
    text_format: str


def make_soup(html: str) -> BeautifulSoup:
    # lxml rather than html.parser: real pages are frequently not well formed,
    # and html.parser then silently returns a truncated tree - no error, just
    # less text.
    return BeautifulSoup(html or "", "lxml")


def _clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def extract_links(html: str, base_url: str, *,
                  link_selector: str = "a[href]") -> list[Link]:
    """Every link of the page, resolved, canonical, deduplicated, in page order.

    Nothing is filtered here - not other hosts, not assets, not the imprint.
    Classification and the rules do that, and the link picker needs to show
    what it is dropping and why. Filtering twice, in two places, is how a
    picker and a crawl end up disagreeing about what is on a page.

    >>> html = '''<a href="/rent/1">First</a> <a href="/rent/1?utm_source=x">again</a>
    ...           <a href="#top">top</a> <a href="mailto:a@b.ch">mail</a>
    ...           <a href="https://other.example/x">elsewhere</a>'''
    >>> [(l.url, l.text) for l in extract_links(html, "https://a.example/rent/list")]
    [('https://a.example/rent/1', 'First'), ('https://other.example/x', 'elsewhere')]
    """
    soup = make_soup(html)
    seen: set[str] = set()
    links: list[Link] = []

    for node in soup.select(link_selector):
        href = (node.get("href") or "").strip()
        if not href or href.lower().startswith(_SKIP_SCHEMES):
            continue
        absolute = urljoin(base_url, href)
        if not absolute.startswith(("http://", "https://")):
            continue
        canon = canonical_uri(absolute)
        if not canon or canon in seen:
            continue
        seen.add(canon)
        text = _clean_text(node.get_text(" ", strip=True))
        if not text:
            # A link whose content is an icon: the labels are the next best
            # thing, and the pagination detection reads exactly these.
            text = _clean_text(str(node.get("aria-label") or node.get("title") or ""))
        links.append(Link(canon, text[:300]))
    return links


def _paging_value(url: str, key: str) -> int | None:
    """The numeric value of `key` in `url`'s query, if it has one."""
    from urllib.parse import parse_qsl
    for name, value in parse_qsl(urlsplit(url).query, keep_blank_values=True):
        if name.lower() == key.lower() and value.isdigit():
            return int(value)
    return None


def _same_list(a: str, b: str, key: str) -> bool:
    """Are these the same address apart from the paging key?"""
    from crawlkit.forms import split_form
    fa, fb = split_form(a), split_form(b)
    return fa.pattern == fb.pattern and fa.values == fb.values


def next_page(html: str, base_url: str, *, paging_param: str = "",
              selector: str = "", links: list[Link] | None = None) -> str | None:
    """The address of the next list page, or None.

    Three ways, in this order, and the order is the point:

    1. The **paging parameter** that was learned from the links a person
       ticked. It is the reliable one: it names the key, and the next page is
       whichever link carries a higher number under it. A page that has no
       such link is the last page - which is the first of the four stop rules,
       and it is free.
    2. `rel="next"`, the standard.
    3. The label, as a whole word, with the back-words excluded so that "vor"
       does not match inside "vorherige" and walk the crawl backwards.

    >>> html = '<a href="?ep=2">2</a><a href="?ep=3">3</a><a href="/rent/9">flat</a>'
    >>> next_page(html, "https://a.example/list", paging_param="ep")
    'https://a.example/list?ep=2'
    >>> next_page(html, "https://a.example/list?ep=2", paging_param="ep")
    'https://a.example/list?ep=3'
    >>> next_page(html, "https://a.example/list?ep=3", paging_param="ep") is None
    True

    Without a parameter the standard, then the label:

    >>> next_page('<link rel="next" href="/list/2">', "https://a.example/list")
    'https://a.example/list/2'
    >>> next_page('<a href="/list/2">Next</a>', "https://a.example/list")
    'https://a.example/list/2'
    >>> next_page('<a href="/list/0">previous</a>', "https://a.example/list") is None
    True

    A selector on the source beats all of it - for the sites that paginate
    with something no rule recognises:

    >>> next_page('<a class="w" href="/p7">…</a>', "https://a.example/l", selector="a.w")
    'https://a.example/p7'
    """
    soup = make_soup(html)

    if selector:
        node = soup.select_one(selector)
        href = (node.get("href") or "").strip() if node else ""
        return canonical_uri(urljoin(base_url, href)) if href else None

    found = links if links is not None else extract_links(html, base_url)

    if paging_param:
        current = _paging_value(base_url, paging_param) or 1
        candidates = []
        for link in found:
            value = _paging_value(link.url, paging_param)
            if value is not None and value > current and _same_list(link.url, base_url, paging_param):
                candidates.append((value, link.url))
        if candidates:
            return min(candidates)[1]
        return None

    node = soup.select_one('link[rel~="next"][href], a[rel~="next"][href]')
    if node:
        href = (node.get("href") or "").strip()
        if href:
            return canonical_uri(urljoin(base_url, href))

    for link in found:
        if looks_like_next_label(link.text):
            return link.url
    return None


def prepare(html: str, *, text_format: str = "plain", content_selector: str = "",
            drop_selectors: str = "", title_selector: str = "h1",
            strip_chrome: bool = True) -> Prepared:
    """The page as it will be submitted, and its hash.

    `plain` is what a reader would take from the page: paragraphs separated by
    blank lines, no tables rebuilt, no markup. `html` keeps the structure -
    lists, headings, tables - and costs three to ten times the characters,
    which is why the test result shows the sampled count before anyone saves.

    In BOTH formats the same things are dropped, and `<script>` is the first
    of them. Cleaned HTML with a script in it would send code to be read as
    prose, and be billed for it.

    >>> page = ('<html><head><title>T</title></head><body><nav>Menu</nav>'
    ...         '<h1>Flat in Zurich</h1><p>Three rooms.</p>'
    ...         '<script>tracker()</script></body></html>')
    >>> plain = prepare(page)
    >>> plain.title, plain.content
    ('Flat in Zurich', 'Flat in Zurich\\nThree rooms.')
    >>> as_html = prepare(page, text_format="html")
    >>> "script" in as_html.content or "Menu" in as_html.content
    False
    >>> as_html.content
    '<h1>Flat in Zurich</h1><p>Three rooms.</p>'

    The same page gives the same hash, and the two formats do not:

    >>> plain.content_hash == prepare(page).content_hash
    True
    >>> plain.content_hash == as_html.content_hash
    False
    """
    soup = make_soup(html)

    for comment in soup.find_all(string=lambda s: isinstance(s, Comment)):
        comment.extract()
    for tag in ALWAYS_DROP:
        for node in soup.select(tag):
            node.decompose()

    if strip_chrome and not content_selector:
        # Only without a content selector: a selector is a statement about
        # where the content is, and dropping furniture inside it would be
        # second-guessing it.
        for selector in COMMON_CHROME:
            for node in soup.select(selector):
                node.decompose()

    for selector in [s.strip() for s in (drop_selectors or "").split(",") if s.strip()]:
        for node in soup.select(selector):
            node.decompose()

    title = ""
    if title_selector:
        node = soup.select_one(title_selector)
        if node:
            title = _clean_text(node.get_text(" ", strip=True))
    if not title and soup.title and soup.title.string:
        title = _clean_text(str(soup.title.string))

    roots = []
    if content_selector:
        roots = soup.select(content_selector)
        if not roots:
            # Not fatal: a selector can go stale, and the whole body is still
            # better than nothing. The run report shows the miss.
            log.warning("content selector %r matched nothing - taking the whole body",
                        content_selector)
    if not roots:
        roots = [soup.body or soup]

    if text_format == "html":
        content = "".join(_clean_html(node) for node in roots).strip()
    else:
        # separator="\n": without it two paragraphs run together
        # ("...deadlineMonday...") and the sentence splitting on the platform
        # then cuts in the wrong place.
        content = _tidy("\n".join(node.get_text("\n", strip=True) for node in roots))

    return Prepared(title=title[:500], content=content,
                    content_hash=sha256_text(content), char_count=len(content),
                    text_format="html" if text_format == "html" else "plain")


def _clean_html(node) -> str:  # type: ignore[no-untyped-def]
    """The node's HTML with every attribute but `KEEP_ATTRIBUTES` removed.

    Classes and data attributes are a site's layout, not its content. They can
    be half the characters of a page, and every one of them is submitted,
    stored and paid for.
    """
    for element in node.find_all(True):
        element.attrs = {name: value for name, value in element.attrs.items()
                         if name in KEEP_ATTRIBUTES}
    inner = node.decode_contents() if hasattr(node, "decode_contents") else str(node)
    # A body wrapper carries no meaning; anything else the selector picked is
    # kept as it stands.
    if getattr(node, "name", "") not in ("body", "html", "[document]"):
        inner = str(node)
    return re.sub(r"\n\s*\n+", "\n", inner).strip()


def _tidy(text: str) -> str:
    """Tidy whitespace WITHOUT flattening the line structure.

    The difference between "next line" and "next paragraph" has to survive.
    Pages put a field label on its own line and the value directly below it:

        Applicant:
        A. Muster, Musterweg 3

    Single line breaks therefore stay:

    >>> _tidy("Applicant:\\nA. Muster")
    'Applicant:\\nA. Muster'

    Repeated blank lines collapse into one:

    >>> _tidy("a\\n\\n\\n\\nb   c\\n \\n d")
    'a\\n\\nb c\\n\\nd'
    >>> _tidy("   ")
    ''
    """
    text = (text or "").replace("\xa0", " ").replace("​", "")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n")]
    out: list[str] = []
    for line in lines:
        if line:
            out.append(line)
        elif out and out[-1] != "":
            out.append("")
    while out and out[-1] == "":
        out.pop()
    return "\n".join(out).strip()


__all__ = ["ALWAYS_DROP", "COMMON_CHROME", "KEEP_ATTRIBUTES", "Link", "Prepared",
           "extract_links", "make_soup", "next_page", "prepare"]
