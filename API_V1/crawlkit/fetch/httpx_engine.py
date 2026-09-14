"""The fast way: one HTTP request, done.

Enough for every server-rendered page, which is most of them. Where it is not
enough the answer says so: a page that returns 200 and carries no links is
very probably a JavaScript application and belongs on `engine: playwright` -
the editor's "0 links found - try Rendered" banner is that observation.

REDIRECTS ARE FOLLOWED BY HAND, one hop at a time. httpx would do it in one
call, but then the guard - the check that an address does not point into a
private network - would see the first URL only, and a redirect to
`http://127.0.0.1:22/` would go straight through it. Every hop is checked.

THE BODY IS READ IN PIECES, AGAINST A CAP. A page is a few hundred kilobytes;
a site that answers with sixty megabytes - or with a small gzip body that
inflates to sixty - is not sending a page, and reading it whole would take
the memory of the container, which is what the next watched page needed.
The cap is on the inflated size, because that is what is kept.

THE PEER IS CHECKED AGAIN ONCE CONNECTED. The guard resolves the name before
the request; the connection resolves it a second time, and a name with a
short TTL can answer differently the second time (DNS rebinding). So after
the headers arrive, and before a byte of the body is read, the address the
socket actually reached is put to the same guard.
"""

from __future__ import annotations

import logging
import time
from typing import Callable

import httpx

from crawlkit.fetch.base import FetchError, FetchResult, looks_like_challenge, refuse

log = logging.getLogger(__name__)

#: A full set of browser headers. Not to disguise anything - the User-Agent
#: names the crawler and where to complain about it - but because some
#: protection layers refuse anything without Accept-Language or Sec-Fetch-*.
#: A request that identifies itself and gets through is better for both sides
#: than one that does not try.
BASE_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en;q=0.9,de;q=0.8,fr;q=0.7",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}

MAX_REDIRECTS = 10

#: Above this a response is not a page. Measured after decompression: a
#: gzip bomb is small on the wire and huge in memory.
MAX_BODY_BYTES = 10 * 1024 * 1024

#: Read in this size while checking the cap.
CHUNK_BYTES = 64 * 1024

#: One fetch, redirects and body included, may take this many times the
#: timeout. The timeout itself is per operation - a connect, one read - so a
#: server that hands over a byte every twenty seconds would otherwise hold a
#: worker for as long as it liked.
BUDGET_FACTOR = 4


class HttpxEngine:
    """One client for a whole run: connections and cookies are reused.

    The cookie jar matters more than it looks. Some applications redirect to
    an error page without a session cookie; the first request sets it and the
    second one gets through.
    """

    name = "http"

    def __init__(self, user_agent: str, *, timeout: float = 30.0,
                 guard: Callable[[str], None] | None = None,
                 max_bytes: int = MAX_BODY_BYTES) -> None:
        self.user_agent = user_agent
        self.timeout = timeout
        #: Called with every address before it is requested, redirects
        #: included, and with the connected peer's address before the body
        #: is read. Raises to refuse; `crawlkit.netguard.make_guard()` is
        #: what both the dashboard and the crawler pass in.
        self.guard = guard
        self.max_bytes = max_bytes
        self._client: httpx.Client | None = None
        self._deadline = 0.0

    def __enter__(self) -> "HttpxEngine":
        self._client = httpx.Client(
            timeout=self.timeout,
            follow_redirects=False,
            headers={**BASE_HEADERS, "User-Agent": self.user_agent},
            cookies=httpx.Cookies(),
            # Two connections per host at most. The politeness delay sets the
            # distance between requests; this caps how many can be open when
            # something goes wrong with that.
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
        )
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def fetch(self, url: str, **_ignored: object) -> FetchResult:
        """Fetch one page. `**_ignored` swallows the rendering options: the
        caller uses one call for both engines and only one of them waits for
        selectors."""
        if self._client is None:
            raise RuntimeError("HttpxEngine used outside its with-block")

        started = time.monotonic()
        self._deadline = started + self.timeout * BUDGET_FACTOR
        request = self._client.build_request("GET", url)
        response = None
        for _hop in range(MAX_REDIRECTS):
            self._within_budget(url)
            refuse(self.guard, str(request.url))
            try:
                response = self._client.send(request, follow_redirects=False,
                                             stream=True)
            except httpx.HTTPError as exc:
                raise FetchError(f"{type(exc).__name__}: {exc}") from exc
            try:
                self._check_peer(response)
            except FetchError:
                response.close()
                raise
            if response.is_redirect and response.next_request is not None:
                request = response.next_request
                response.close()
                continue
            break
        else:
            raise FetchError(f"more than {MAX_REDIRECTS} redirects starting at {url}")

        assert response is not None
        try:
            body = self._body_of(response, url)
        finally:
            response.close()
        self._within_budget(url)
        elapsed_ms = int((time.monotonic() - started) * 1000)
        ctype = response.headers.get("content-type", "")
        html = body.decode(response.charset_encoding or "utf-8", errors="replace")

        if response.status_code >= 400:
            challenge = (response.status_code in (401, 403, 429)
                         and looks_like_challenge(html))
            raise FetchError(
                f"HTTP {response.status_code} for {url}"
                + (" - the site answered with a browser check, not a page"
                   if challenge else ""),
                status=response.status_code, challenge=challenge)

        if looks_like_challenge(html):
            raise FetchError(
                f"the site answered {response.status_code} with a browser "
                f"check instead of the page",
                status=response.status_code, challenge=True)

        return FetchResult(url=url, final_url=str(response.url),
                           status=response.status_code, html=html,
                           content_type=ctype, engine=self.name,
                           elapsed_ms=elapsed_ms, bytes=len(body))

    def _body_of(self, response: httpx.Response, url: str) -> bytes:
        """Everything decided from the headers, then the body - in that order,
        so that nothing is read from a status or a type that is refused
        anyway."""
        if response.status_code in (429, 503):
            # A request for distance, not a fault. Retry-After may be a
            # number or a date; only the number is read - a date would be
            # scaffolding for a case that does not occur.
            raw = response.headers.get("retry-after", "").strip()
            wait = float(raw) if raw.isdigit() else None
            raise FetchError(
                f"HTTP {response.status_code} for {url} - the site is asking "
                f"for room" + (f", Retry-After {wait:.0f}s" if wait else ""),
                status=response.status_code, retry_after=wait)

        ctype = response.headers.get("content-type", "")
        if (response.status_code < 400
                and not any(kind in ctype for kind in ("html", "xml", "text"))):
            # Not a page. Files are a different path with different rules
            # (files.py); here it would only be text nobody can read. An
            # error page is read whatever it calls itself: the body is what
            # tells a refusal from a browser check.
            raise FetchError(f"not a page but {ctype!r} - skipped",
                             status=response.status_code)

        return self._read_capped(response, url)

    def _within_budget(self, url: str) -> None:
        if time.monotonic() > self._deadline:
            raise FetchError(f"{url} took longer than {self.timeout * BUDGET_FACTOR:.0f} s "
                             f"to arrive - the site is dripping, not answering")

    def _check_peer(self, response: httpx.Response) -> None:
        """The address the socket reached, put to the guard.

        Nothing of the body has been read at this point: a peer the guard
        refuses gets its headers looked at and its connection closed.
        """
        if self.guard is None:
            return
        stream = response.extensions.get("network_stream")
        address = stream.get_extra_info("server_addr") if stream is not None else None
        if address:
            from crawlkit.netguard import peer_url
            refuse(self.guard, peer_url(response.url.scheme, address))

    def _read_capped(self, response: httpx.Response, url: str) -> bytes:
        """The body, or `FetchError` once it is larger than the cap.

        Reading stops at the cap rather than after it: what matters is that
        the rest never comes down the wire, not that it is discarded once it
        has.
        """
        announced = response.headers.get("content-length", "").strip()
        if announced.isdigit() and int(announced) > self.max_bytes:
            raise FetchError(f"{int(announced) // 1024 // 1024} MB is not a page "
                             f"- the limit is {self.max_bytes // 1024 // 1024} MB",
                             status=response.status_code)
        chunks: list[bytes] = []
        read = 0
        try:
            for chunk in response.iter_bytes(CHUNK_BYTES):
                self._within_budget(url)
                read += len(chunk)
                if read > self.max_bytes:
                    raise FetchError(f"more than {self.max_bytes // 1024 // 1024} MB "
                                     f"from {url} - that is not a page",
                                     status=response.status_code)
                chunks.append(chunk)
        except httpx.HTTPError as exc:
            raise FetchError(f"{type(exc).__name__}: {exc}") from exc
        return b"".join(chunks)


__all__ = ["BASE_HEADERS", "BUDGET_FACTOR", "CHUNK_BYTES", "MAX_BODY_BYTES", "MAX_REDIRECTS",
           "HttpxEngine"]
