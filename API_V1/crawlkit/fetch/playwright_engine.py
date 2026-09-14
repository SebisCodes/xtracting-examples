"""The slow way: a real browser.

Needed for one case, and it is a common one: a list that JavaScript builds.
The delivered HTML then holds a navigation and an empty `<main>`, and the
twenty results appear only after the application has started. Without a
browser there is nothing to collect - which is why the editor's banner for a
page with zero links offers Rendered mode with one click.

THE COST HAS TO BE KNOWN: a browser window is some 300 MB and several
seconds, and the image that carries Chromium is about 4 GB. That is why HTTP
is the default and Rendered is switched on per source.

THE MOST IMPORTANT LINES IN THIS FILE are the two `finally` blocks. A context
that is not closed leaves a renderer process behind on every call; after a day
of hourly runs the container is full, and the error then looks like a memory
problem rather than a forgotten close().

THE GUARD SITS IN THE ROUTE, NOT ON AN EVENT. A page that has run its scripts
has made every request those scripts asked for - an image, a fetch() to the
metadata address of the cloud the crawler runs in, a frame of the database's
admin page. The route handler sees each request BEFORE the browser sends it
and aborts what the guard refuses; an event fires after the answer is in.

REDIRECTS OF THE PAGE ITSELF ARE FOLLOWED BY HAND, for the same reason as in
the HTTP engine: the browser follows a redirect inside its network stack, and
the route sees the first address only. So a navigation is fetched hop by hop
from the route handler, each hop put to the guard, and the browser is then
sent straight to the last one.
"""

from __future__ import annotations

import logging
import time
from contextlib import suppress
from typing import Callable

from crawlkit.fetch.base import (EngineUnavailable, FetchError, FetchResult,
                                 looks_like_challenge, refuse)
from crawlkit.fetch.httpx_engine import MAX_BODY_BYTES, MAX_REDIRECTS

log = logging.getLogger(__name__)

#: The browser's own name for a request its client refused.
_BLOCKED = "blockedbyclient"

MISSING_MESSAGE = (
    "Rendered mode is not available in this build. The image was built on the "
    "slim Python base, which has no browser (the Playwright base is about "
    "4 GB). Either set this watched page to HTTP, or rebuild without "
    "--build-arg *_BASE=docker.io/python:3.12-slim. To develop locally: "
    "pip install playwright && playwright install chromium"
)


def available() -> bool:
    """Is a browser in this image?

    Asked before a source is offered Rendered mode, and by the tests, which
    skip rather than fail where there is no Chromium.
    """
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
    except ImportError:
        return False
    return True


class PlaywrightEngine:
    name = "playwright"

    def __init__(self, user_agent: str, *, timeout: float = 45.0,
                 locale: str = "en-GB",
                 guard: Callable[[str], None] | None = None,
                 max_bytes: int = MAX_BODY_BYTES) -> None:
        self.user_agent = user_agent
        self.timeout_ms = int(timeout * 1000)
        self.locale = locale
        #: Called with every address the browser is about to request - the
        #: page, every hop of its redirects, every script, frame and fetch()
        #: the page makes. Raises to refuse.
        self.guard = guard
        self.max_bytes = max_bytes
        self._pw = None
        self._browser = None

    def __enter__(self) -> "PlaywrightEngine":
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover - depends on the image
            raise EngineUnavailable(MISSING_MESSAGE) from exc

        try:
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch(args=[
                # A container has no user namespace for the sandbox to run
                # in, and /dev/shm is 64 MB there. Without these two flags
                # Chromium does not start at all.
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
            ])
        except Exception as exc:  # noqa: BLE001 - the browser binary may be absent
            self.__exit__()
            raise EngineUnavailable(f"{MISSING_MESSAGE} ({exc})") from exc
        return self

    def __exit__(self, *exc: object) -> None:
        # Both suppressed: if the browser hangs, Playwright itself should
        # still be stopped, and an error raised here would hide the error the
        # with-block was really about.
        with suppress(Exception):
            if self._browser is not None:
                self._browser.close()
        with suppress(Exception):
            if self._pw is not None:
                self._pw.stop()
        self._browser = None
        self._pw = None

    def close(self) -> None:
        self.__exit__()

    def fetch(self, url: str, *, wait_for: str = "",
              wait_after_load_ms: int = 0, **_ignored: object) -> FetchResult:
        """Load the page and return it as it stands after the scripts ran.

        `wait_for` is a CSS selector to wait for. Without one the wait ends at
        `networkidle`, which is enough for most applications but not for one
        that starts a query after loading. A missing selector is NOT an error:
        the result list may genuinely be empty today, and the difference shows
        up in the yield, not here.
        """
        if self._browser is None:
            raise RuntimeError("PlaywrightEngine used outside its with-block")

        from playwright.sync_api import Error as PWError
        from playwright.sync_api import TimeoutError as PWTimeout

        refuse(self.guard, url)

        started = time.monotonic()
        context = None
        #: What the guard refused for the page itself, if anything. Filled
        #: by the route handler, which cannot raise into this frame: an
        #: exception in a handler is reported by the browser at some later
        #: call, not here.
        refused: list[str] = []
        try:
            context = self._browser.new_context(
                user_agent=self.user_agent, locale=self.locale,
                viewport={"width": 1400, "height": 1000},
                extra_http_headers={"Accept-Language": "en;q=0.9"},
                # A page that answers with a file is not a page. Refused
                # here, so that nothing a site hands out lands on the disk
                # of the container.
                accept_downloads=False,
            )
            page = context.new_page()
            page.route("**/*", lambda route, request:
                       self._route(route, request, refused))

            try:
                response = page.goto(url, timeout=self.timeout_ms,
                                     wait_until="domcontentloaded")
            except PWError:
                # The browser reports an aborted navigation as its own
                # error; the guard's sentence is the one worth reading.
                if refused:
                    raise FetchError(refused[0]) from None
                raise
            if refused:
                raise FetchError(refused[0])
            self._check_hops(response)
            status = response.status if response else 0

            if wait_for:
                try:
                    page.wait_for_selector(wait_for, timeout=self.timeout_ms,
                                           state="attached")
                except PWTimeout:
                    log.warning("selector %r did not appear on %s within %d ms",
                                wait_for, url, self.timeout_ms)
            else:
                with suppress(PWTimeout):
                    page.wait_for_load_state("networkidle", timeout=self.timeout_ms)

            if wait_after_load_ms:
                page.wait_for_timeout(wait_after_load_ms)

            html = page.content()
            final_url = page.url
            size = len(html.encode("utf-8", "replace"))
            if size > self.max_bytes:
                raise FetchError(f"more than {self.max_bytes // 1024 // 1024} MB "
                                 f"from {url} - that is not a page", status=status)

            if status >= 400:
                raise FetchError(
                    f"HTTP {status} for {url}", status=status,
                    challenge=looks_like_challenge(html))

            return FetchResult(url=url, final_url=final_url, status=status or 200,
                               html=html, content_type="text/html",
                               engine=self.name,
                               elapsed_ms=int((time.monotonic() - started) * 1000),
                               bytes=size)

        except PWTimeout as exc:
            raise FetchError(f"timeout loading {url}: {exc}") from exc
        except PWError as exc:
            raise FetchError(f"browser error at {url}: {exc}") from exc
        finally:
            # Without this close a renderer process stays behind on every
            # call. After a day of hourly runs the container is full, and the
            # error looks like a memory problem instead of a missing close().
            with suppress(Exception):
                if context is not None:
                    context.close()

    # -- the route -----------------------------------------------------

    def _route(self, route, request, refused: list[str]) -> None:  # type: ignore[no-untyped-def]
        """Every request the browser is about to make passes through here."""
        # Images, fonts and video cost the site bandwidth and add nothing
        # to the text. Stylesheets stay: some pages only reveal content
        # after layout.
        if request.resource_type in ("image", "media", "font"):
            route.abort()
            return
        if self.guard is None:
            route.continue_()
            return
        try:
            self.guard(request.url)
        except Exception as exc:  # noqa: BLE001 - the guard's own sentence is the answer
            if _is_the_page(request):
                refused.append(str(exc))
            else:
                log.info("refused a request of the page: %s", exc)
            route.abort(_BLOCKED)
            return
        if request.is_navigation_request() and request.method == "GET":
            self._navigate(route, request, refused)
        else:
            route.continue_()

    def _navigate(self, route, request, refused: list[str]) -> None:  # type: ignore[no-untyped-def]
        """A navigation, fetched hop by hop with the guard on every hop.

        The answer of the last hop is handed to the browser as it is when
        there was no redirect. After one, the browser is sent to the last
        address with a redirect of our own: it then requests that address
        itself, so that the page's own scripts see the address they were
        written for.
        """
        from urllib.parse import urljoin

        url = request.url
        for _hop in range(MAX_REDIRECTS):
            response = route.fetch(url=url, max_redirects=0)
            location = response.headers.get("location", "")
            if not (300 <= response.status < 400 and location):
                if url == request.url:
                    route.fulfill(response=response)
                else:
                    route.fulfill(status=302, headers={"location": url})
                return
            url = urljoin(url, location)
            try:
                self.guard(url)  # type: ignore[misc]
            except Exception as exc:  # noqa: BLE001
                if _is_the_page(request):
                    refused.append(str(exc))
                route.abort(_BLOCKED)
                return
        refused.append(f"more than {MAX_REDIRECTS} redirects starting at {request.url}")
        route.abort(_BLOCKED)

    def _check_hops(self, response) -> None:  # type: ignore[no-untyped-def]
        """The second lock: every address the page went through, judged once
        more after the fact. `_navigate` sends the browser to an address it
        has already checked; this is for the redirect that address might
        itself answer with in the moment between."""
        if self.guard is None or response is None:
            return
        request = response.request
        while request is not None:
            refuse(self.guard, request.url)
            request = request.redirected_from


def _is_the_page(request) -> bool:  # type: ignore[no-untyped-def]
    """Is this the navigation of the page itself, not a frame in it?

    A request from a service worker has no frame at all, and asking for it
    raises; that is not the page either.
    """
    if not request.is_navigation_request():
        return False
    try:
        frame = request.frame
        return frame == frame.page.main_frame
    except Exception:  # noqa: BLE001
        return False


__all__ = ["MISSING_MESSAGE", "PlaywrightEngine", "available"]
