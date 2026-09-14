"""Two engines behind one call.

`engine_for(name)` is what the crawl uses: the source says `http` or
`playwright`, and everything after that is the same code. A source that asks
for a browser in an image without one gets `EngineUnavailable` - a refusal
with a sentence a person can act on, not an ImportError from three frames
down.
"""

from __future__ import annotations

from typing import Callable

from crawlkit.fetch.base import (CHALLENGE_MARKERS, EngineUnavailable, FetchError,
                                 FetchResult, looks_like_challenge)
from crawlkit.fetch.httpx_engine import HttpxEngine
from crawlkit.fetch.playwright_engine import (MISSING_MESSAGE, PlaywrightEngine,
                                              available as playwright_available)

ENGINES = ("http", "playwright")


def engine_for(name: str, user_agent: str, *, timeout: float = 30.0,
               guard: Callable[[str], None] | None = None):
    """The engine a source asks for, unopened - use it as a context manager.

    >>> with engine_for("http", "test-agent") as engine:
    ...     engine.name
    'http'
    >>> engine_for("smoke-signal", "test-agent")
    Traceback (most recent call last):
    ...
    crawlkit.fetch.base.FetchError: unknown engine 'smoke-signal' - it is 'http' or 'playwright'
    """
    if name == "playwright":
        return PlaywrightEngine(user_agent, timeout=max(timeout, 45.0), guard=guard)
    if name in ("http", "httpx", ""):
        return HttpxEngine(user_agent, timeout=timeout, guard=guard)
    raise FetchError(f"unknown engine {name!r} - it is 'http' or 'playwright'")


__all__ = ["CHALLENGE_MARKERS", "ENGINES", "EngineUnavailable", "FetchError",
           "FetchResult", "HttpxEngine", "MISSING_MESSAGE", "PlaywrightEngine",
           "engine_for", "looks_like_challenge", "playwright_available"]
