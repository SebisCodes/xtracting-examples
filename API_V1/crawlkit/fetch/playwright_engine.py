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
"""

from __future__ import annotations

import logging
import time
from contextlib import suppress
from typing import Callable

from crawlkit.fetch.base import (EngineUnavailable, FetchError, FetchResult,
                                 looks_like_challenge)

log = logging.getLogger(__name__)

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
                 guard: Callable[[str], None] | None = None) -> None:
        self.user_agent = user_agent
        self.timeout_ms = int(timeout * 1000)
        self.locale = locale
        self.guard = guard
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

        if self.guard is not None:
            self.guard(url)

        started = time.monotonic()
        context = None
        try:
            context = self._browser.new_context(
                user_agent=self.user_agent, locale=self.locale,
                viewport={"width": 1400, "height": 1000},
                extra_http_headers={"Accept-Language": "en;q=0.9"},
            )
            page = context.new_page()

            if self.guard is not None:
                # Every navigation, not only the first: a redirect into a
                # private network must be refused in the browser as well.
                def _check(frame) -> None:  # type: ignore[no-untyped-def]
                    if frame == page.main_frame:
                        self.guard(frame.url)  # type: ignore[misc]
                page.on("framenavigated", _check)

            # Images, fonts and video cost the site bandwidth and add nothing
            # to the text. Stylesheets stay: some pages only reveal content
            # after layout.
            page.route("**/*", lambda route, request: (
                route.abort() if request.resource_type in ("image", "media", "font")
                else route.continue_()))

            response = page.goto(url, timeout=self.timeout_ms,
                                 wait_until="domcontentloaded")
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

            if status >= 400:
                raise FetchError(
                    f"HTTP {status} for {url}", status=status,
                    challenge=looks_like_challenge(html))

            return FetchResult(url=url, final_url=final_url, status=status or 200,
                               html=html, content_type="text/html",
                               engine=self.name,
                               elapsed_ms=int((time.monotonic() - started) * 1000),
                               bytes=len(html.encode("utf-8", "replace")))

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


__all__ = ["MISSING_MESSAGE", "PlaywrightEngine", "available"]
