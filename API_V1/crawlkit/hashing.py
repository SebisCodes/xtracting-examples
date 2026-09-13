"""URL canonicalisation and hashes.

THIS FILE IS THE ONLY PLACE WHERE A URL IS NORMALISED.

The reason is not a matter of style. The crawler derives the key in
`scraper.seen_urls` from the canonical URL, and the sender passes exactly the
same string as `source` to the API. The reconciliation later mirrors the
answers back over this value. If the two differ by even one character - a
trailing slash, a parameter in a different order - the mirroring finds
nothing, every page counts as new on the next run, and every document is
submitted and paid for a second time.

Therefore: no second normalisation anywhere in the code, not even a "small"
one (no .rstrip('/'), no .lower() on a URL). Whoever wants to change something
changes it here - and the doctests say immediately what that means for
existing rows.
"""

from __future__ import annotations

import hashlib
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Parameters that only serve attribution and deliver the same content. Left in
# place, the same page is known under five different keys and submitted five
# times.
TRACKING_PARAMS = frozenset({
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "utm_id", "utm_source_platform", "utm_creative_format",
    "gclid", "gbraid", "wbraid", "dclid", "fbclid", "msclkid", "twclid",
    "mc_cid", "mc_eid", "igshid", "ref", "ref_src", "referrer",
    "_ga", "_gl", "yclid", "s_kwcid",
})

DEFAULT_PORTS = {"http": "80", "https": "443"}


def canonical_uri(url: str, *, keep_query: bool = True) -> str:
    """Bring a URL into a stable form.

    Deliberately NOT done:

    * Lower-casing the path. On many servers ``/Auflagen`` and ``/auflagen``
      are different pages; according to RFC 3986 only scheme and host are
      case-insensitive.
    * Resolving redirects. That would need network access, and this function
      must return the same thing offline and in tests.
    * Removing ``index.html``. Some servers serve something else under it than
      under the directory.

    >>> canonical_uri("HTTPS://Www.Example.CH:443/Weg/?b=2&a=1#teil")
    'https://www.example.ch/Weg/?a=1&b=2'
    >>> canonical_uri("http://example.ch")
    'http://example.ch/'
    >>> canonical_uri("https://example.ch/a?utm_source=x&id=7")
    'https://example.ch/a?id=7'

    A parameter without a value is kept - for some applications its mere
    presence is the meaning:

    >>> canonical_uri("https://example.ch/a?druck&id=7")
    'https://example.ch/a?druck=&id=7'

    Empty input returns empty instead of raising: a broken URL on a list page
    must not abort the whole run.

    >>> canonical_uri("")
    ''
    """
    if not url or not url.strip():
        return ""

    parts = urlsplit(url.strip())
    if not parts.scheme or not parts.netloc:
        # Relative or incomplete URL. The caller should have resolved it
        # beforehand; here it is passed through unchanged so that the error
        # shows up in the log as what it is, instead of disguising itself as
        # a silent duplicate key.
        return url.strip()

    scheme = parts.scheme.lower()

    host = parts.hostname or ""
    try:
        # IDN as punycode: müller.ch and xn--mller-kva.ch are the same page
        # and must produce the same key.
        host = host.encode("idna").decode("ascii")
    except (UnicodeError, UnicodeDecodeError):
        host = host.lower()

    netloc = host
    if parts.port is not None and str(parts.port) != DEFAULT_PORTS.get(scheme):
        netloc = f"{host}:{parts.port}"
    # Credentials in the URL are discarded. They do not belong in a database
    # key, and certainly not as `source` sent to a third-party API.

    path = unicodedata.normalize("NFC", parts.path) or "/"

    query = ""
    if keep_query and parts.query:
        pairs = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                 if k.lower() not in TRACKING_PARAMS]
        # Sorted, so that ?a=1&b=2 and ?b=2&a=1 are the same key. Secondary
        # sort by value, so that repeated parameters (?id=2&id=1) also have a
        # fixed order.
        pairs.sort(key=lambda kv: (kv[0], kv[1]))
        query = urlencode(pairs)

    # The fragment is always dropped: it is never sent to the server and can
    # therefore never denote a different page.
    return urlunsplit((scheme, netloc, path, query, ""))


def sha256_text(text: str) -> str:
    """SHA-256 of a text as hex.

    Always UTF-8. The hash is compared with the one the Xtracting API computes
    over the same content - a different encoding would give a different hash
    and thus a false alarm for every single document.

    >>> sha256_text("abc")[:16]
    'ba7816bf8f01cfea'
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def uri_hash(url: str) -> str:
    """The key in `scraper.seen_urls`: sha256 of the canonical URL.

    >>> uri_hash("https://example.ch/a?b=1") == uri_hash("https://example.ch/a?b=1#x")
    True
    >>> uri_hash("") == ''
    True
    """
    canon = canonical_uri(url)
    return sha256_text(canon) if canon else ""


def same_document(url_a: str, url_b: str) -> bool:
    """Do two URLs point at the same page after canonicalisation?

    >>> same_document("http://Example.ch/a?x=1&utm_source=q", "http://example.ch/a?x=1")
    True
    """
    return bool(url_a) and canonical_uri(url_a) == canonical_uri(url_b)
