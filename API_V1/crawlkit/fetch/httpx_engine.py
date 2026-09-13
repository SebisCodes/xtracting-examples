"""The fast way: one HTTP request, done.

Enough for every server-rendered page, which is most of them. Where it is not
enough the answer says so: a page that returns 200 and carries no links is
very probably a JavaScript application and belongs on `engine: playwright` -
the editor's "0 links found - try Rendered" banner is that observation.

REDIRECTS ARE FOLLOWED BY HAND, one hop at a time. httpx would do it in one
call, but then the guard - the check that an address does not point into a
private network - would see the first URL only, and a redirect to
`http://127.0.0.1:22/` would go straight through it. Every hop is checked.
"""

from __future__ import annotations

import logging
import time
from typing import Callable

import httpx

from crawlkit.fetch.base import FetchError, FetchResult, looks_like_challenge

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


class HttpxEngine:
    """One client for a whole run: connections and cookies are reused.

    The cookie jar matters more than it looks. Some applications redirect to
    an error page without a session cookie; the first request sets it and the
    second one gets through.
    """

    name = "http"

    def __init__(self, user_agent: str, *, timeout: float = 30.0,
                 guard: Callable[[str], None] | None = None) -> None:
        self.user_agent = user_agent
        self.timeout = timeout
        #: Called with every address before it is requested, redirects
        #: included. Raises to refuse; `crawlkit.netguard.check_public` is
        #: what the dashboard passes in.
        self.guard = guard
        self._client: httpx.Client | None = None

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
        request = self._client.build_request("GET", url)
        response = None
        for _hop in range(MAX_REDIRECTS):
            if self.guard is not None:
                self.guard(str(request.url))
            try:
                response = self._client.send(request, follow_redirects=False)
            except httpx.HTTPError as exc:
                raise FetchError(f"{type(exc).__name__}: {exc}") from exc
            if response.is_redirect and response.next_request is not None:
                request = response.next_request
                response.close()
                continue
            break
        else:
            raise FetchError(f"more than {MAX_REDIRECTS} redirects starting at {url}")

        assert response is not None
        elapsed_ms = int((time.monotonic() - started) * 1000)
        ctype = response.headers.get("content-type", "")

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

        if response.status_code >= 400:
            challenge = (response.status_code in (401, 403, 429)
                         and looks_like_challenge(response.text))
            raise FetchError(
                f"HTTP {response.status_code} for {url}"
                + (" - the site answered with a browser check, not a page"
                   if challenge else ""),
                status=response.status_code, challenge=challenge)

        if not any(kind in ctype for kind in ("html", "xml", "text")):
            # Not a page. Files are a different path with different rules
            # (files.py); here it would only be text nobody can read.
            raise FetchError(f"not a page but {ctype!r} - skipped",
                             status=response.status_code)

        html = response.text
        if looks_like_challenge(html):
            raise FetchError(
                f"the site answered {response.status_code} with a browser "
                f"check instead of the page",
                status=response.status_code, challenge=True)

        return FetchResult(url=url, final_url=str(response.url),
                           status=response.status_code, html=html,
                           content_type=ctype, engine=self.name,
                           elapsed_ms=elapsed_ms, bytes=len(response.content))


__all__ = ["BASE_HEADERS", "MAX_REDIRECTS", "HttpxEngine"]
