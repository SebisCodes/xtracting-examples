"""robots.txt: fetch it, read it, remember it.

The reading follows RFC 9309, and the two cases that surprise people are
exactly the two that occur in practice:

    2xx  the rules apply.
    4xx  NO RESTRICTION. "Not there" does not mean "forbidden": a 404 on
         robots.txt means the operator wrote no rules. A 403 - a protection
         layer that turns away non-browsers - is a 4xx too, and is treated the
         same way, but with a note, so the dashboard can show that the
         permission was inferred rather than read.
    5xx / network error / timeout
         EVERYTHING IS FORBIDDEN. Here the answer is not "there are no rules"
         but "I cannot say which rules apply". In doubt, do not crawl.

Parsed with **protego**, not `urllib.robotparser`. The difference is not
academic: `Disallow: /*?*ep=` and `Disallow: /*-srp-new/*` are wildcard rules
of the kind the real estate portals use, and urllib matches prefixes only - it
would happily fetch every paginated listing page those sites forbid.

`Crawl-delay` is honoured up to `MAX_CRAWL_DELAY`. Above that the answer is
not "wait longer", it is "this site is too slow to crawl": a list of a hundred
pages at two minutes each is four hours of one source politely blocking a
worker, and nobody wanted that when they saved the source.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urlsplit

from protego import Protego

log = logging.getLogger(__name__)

#: A delay above this makes the source unusable rather than slow. Sixty
#: seconds is already twenty times the default politeness.
MAX_CRAWL_DELAY = 60.0

#: How long one verdict is kept. A crawl reads dozens of pages of the same
#: host; without the cache each one would fetch robots.txt again - dozens of
#: extra requests to a site we are being polite towards.
DEFAULT_TTL_SECONDS = 3600


@dataclass(frozen=True)
class RobotsVerdict:
    """One answer about one address.

    `reason` is written to be shown to a person - it is what the banner in the
    editor and the pill on the overview say.
    """

    allowed: bool
    reason: str
    #: The status robots.txt itself answered with; 0 for a network error.
    status: int = 0
    crawl_delay: float | None = None
    #: True when the rules were derived (4xx) rather than read.
    inferred: bool = False
    #: True when a Crawl-delay above MAX_CRAWL_DELAY is what forbids this.
    too_slow: bool = False


@dataclass
class _Origin:
    rules: Protego | None
    fetched_at: float
    status: int
    note: str
    deny_all: bool


def fetch_robots(url: str, *, user_agent: str, timeout: float) -> tuple[int, str]:
    """Fetch one robots.txt. Returns (status, text); raises on a network error.

    Redirects are followed: many sites answer robots.txt on the apex and
    redirect www to it. Without following, that 301 would read as "not 2xx"
    and forbid the whole host.
    """
    import httpx  # local: robots.py is imported by the pure-rule tests too

    response = httpx.get(url, timeout=timeout, follow_redirects=True,
                         headers={"User-Agent": user_agent,
                                  "Accept": "text/plain,*/*;q=0.8"})
    return response.status_code, response.text


class RobotsCache:
    """One verdict per host, kept for `ttl_seconds`.

    `fetch` is injectable so the tests can answer with a fixed body, a 503 or
    a timeout without a network - the whole point of the verdict mapping is
    what happens in those three cases.

    >>> pages = {"https://a.example/robots.txt": (200, "User-agent: *\\nDisallow: /private/\\n")}
    >>> cache = RobotsCache("test-agent", fetch=lambda url, **kw: pages[url])
    >>> cache.check("https://a.example/public/1").allowed
    True
    >>> verdict = cache.check("https://a.example/private/1")
    >>> verdict.allowed, verdict.reason
    (False, "robots.txt of https://a.example forbids this address for 'test-agent'")
    """

    def __init__(self, user_agent: str, *, ttl_seconds: int = DEFAULT_TTL_SECONDS,
                 timeout: float = 15.0,
                 fetch: Callable[..., tuple[int, str]] = fetch_robots,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.user_agent = user_agent
        self.ttl = ttl_seconds
        self.timeout = timeout
        self._fetch = fetch
        self._clock = clock
        self._origins: dict[str, _Origin] = {}
        #: Every host asked about, in order, for the test result's warnings.
        self.hosts_seen: list[str] = []

    # -- reading -----------------------------------------------------------

    def origin_of(self, url: str) -> str:
        parts = urlsplit(url)
        return f"{parts.scheme}://{parts.netloc}" if parts.scheme and parts.netloc else ""

    def _load(self, origin: str) -> _Origin:
        cached = self._origins.get(origin)
        if cached is not None and (self._clock() - cached.fetched_at) < self.ttl:
            return cached

        try:
            status, text = self._fetch(f"{origin}/robots.txt",
                                       user_agent=self.user_agent,
                                       timeout=self.timeout)
        except Exception as exc:  # noqa: BLE001 - every cause ends the same way
            entry = _Origin(None, self._clock(), 0,
                            f"robots.txt of {origin} could not be read "
                            f"({type(exc).__name__}: {exc}) - nothing will be "
                            f"fetched from this host", deny_all=True)
            self._remember(origin, entry)
            return entry

        if 200 <= status < 300:
            entry = _Origin(Protego.parse(text or ""), self._clock(), status,
                            "robots.txt read", deny_all=False)
        elif 400 <= status < 500:
            # RFC 9309: 4xx means "no restriction". The note records that this
            # was derived and not read - with a 403 from a protection layer
            # the difference matters enough to show it.
            entry = _Origin(None, self._clock(), status,
                            f"robots.txt answers {status}; under RFC 9309 that "
                            f"counts as 'no restriction'", deny_all=False)
        else:
            entry = _Origin(None, self._clock(), status,
                            f"robots.txt answers {status} - the rules are "
                            f"unknown, so nothing is fetched", deny_all=True)

        log.info("robots.txt %s: %s", origin, entry.note)
        self._remember(origin, entry)
        return entry

    def _remember(self, origin: str, entry: _Origin) -> None:
        if origin not in self._origins:
            self.hosts_seen.append(origin)
        self._origins[origin] = entry

    # -- asking ------------------------------------------------------------

    def check(self, url: str) -> RobotsVerdict:
        """May this address be fetched?"""
        origin = self.origin_of(url)
        if not origin.startswith("http"):
            return RobotsVerdict(False, f"not a usable address: {url!r}")

        entry = self._load(origin)
        if entry.deny_all:
            return RobotsVerdict(False, entry.note, status=entry.status)
        if entry.rules is None:
            return RobotsVerdict(True, entry.note, status=entry.status, inferred=True)

        delay = entry.rules.crawl_delay(self.user_agent)
        delay = float(delay) if delay else None
        if delay is not None and delay > MAX_CRAWL_DELAY:
            return RobotsVerdict(
                False,
                f"robots.txt of {origin} asks for {delay:.0f} seconds between "
                f"requests. That is too slow to crawl - more than "
                f"{MAX_CRAWL_DELAY:.0f} seconds per page.",
                status=entry.status, crawl_delay=delay, too_slow=True)

        if entry.rules.can_fetch(url, self.user_agent):
            return RobotsVerdict(True, "allowed by robots.txt", status=entry.status,
                                 crawl_delay=delay)
        return RobotsVerdict(
            False,
            f"robots.txt of {origin} forbids this address for {self.user_agent!r}",
            status=entry.status, crawl_delay=delay)

    def crawl_delay(self, url: str) -> float | None:
        """The delay this host asks for, or None."""
        return self.check(url).crawl_delay

    def known(self, url: str) -> bool:
        """Has this host's robots.txt already been read?

        Asked by the crawl before it fills in the verdict for a link it is not
        going to fetch: a list page can link to twenty hosts, and fetching
        twenty robots.txt files to fill a column in a popup is twenty requests
        nobody asked for.
        """
        return self.origin_of(url) in self._origins

    def note(self, url: str) -> str:
        """How the rules for this host were obtained - for the test result."""
        origin = self.origin_of(url)
        entry = self._origins.get(origin)
        return entry.note if entry else ""

    def status(self, url: str) -> int:
        origin = self.origin_of(url)
        entry = self._origins.get(origin)
        return entry.status if entry else 0


class AllowAll(RobotsCache):
    """The cache a source with `respect_robots = false` gets.

    A second place that may block would be a second place to look when
    something is not fetched, and the second one is always the one nobody
    thinks of. So the switch does not sit in the crawl: it chooses this object
    instead, which answers yes and says why.

    >>> AllowAll("agent").check("https://a.example/anything").reason
    'robots.txt is switched off for this watched page'
    """

    def __init__(self, user_agent: str = "") -> None:
        super().__init__(user_agent, fetch=lambda *a, **kw: (200, ""))

    def check(self, url: str) -> RobotsVerdict:
        return RobotsVerdict(True, "robots.txt is switched off for this watched page")


__all__ = ["MAX_CRAWL_DELAY", "AllowAll", "RobotsCache", "RobotsVerdict", "fetch_robots"]
