"""One host at a time, and a pause between two visits to it.

THE BRAKE BELONGS TO THE HOST, NOT TO THE SOURCE. Two sources on the same
site are two rows in the configuration and one server on the other side; if
both ran at once, the operator of that server would see a client that ignores
its own politeness delay, whatever the delay is set to.

Two effects in one object:

  * while a source is being crawled, no second source on the same host can
    start (the lock), and
  * after it finishes, the delay has to pass before the next one starts (the
    next-free time). Without the second part two sources of the same site run
    back to back and look like a rush in the access log.

`sleep` is injectable because the tests would otherwise have to wait in real
time for what they are checking - and what they are checking is the number,
not the waiting.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict
from typing import Callable
from urllib.parse import urlsplit


def host_of(url: str) -> str:
    """The host a politeness delay applies to - port included.

    The port is part of it because it is part of the server: the stand-in site
    runs four hosts on 127.0.0.1 and they must not brake each other.

    >>> host_of("https://Www.Example.CH/a/b?c=1")
    'www.example.ch'
    >>> host_of("http://127.0.0.1:8123/x")
    '127.0.0.1:8123'
    """
    return urlsplit(url).netloc.lower()


class HostThrottle:
    """Hands out one host at a time and keeps the delay after each release.

    A fake clock that the fake sleep moves shows what the second caller pays:

    >>> now = [0.0]
    >>> t = HostThrottle(sleep=lambda s: now.__setitem__(0, now[0] + s),
    ...                  clock=lambda: now[0])
    >>> t.acquire("https://a.example/one", 6.0)
    'a.example'
    >>> t.release("a.example", 6.0)
    >>> t.acquire("https://a.example/two", 6.0)
    'a.example'
    >>> now[0]
    6.0
    """

    def __init__(self, *, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._sleep = sleep
        self._clock = clock
        self._locks: dict[str, threading.Lock] = defaultdict(threading.Lock)
        self._next_free: dict[str, float] = {}
        self._guard = threading.Lock()

    def _lock_for(self, host: str) -> threading.Lock:
        with self._guard:
            return self._locks[host]

    def acquire(self, url: str, delay_seconds: float,
                stopping: Callable[[], bool] = lambda: False) -> str:
        """Take the host of `url`, waiting out whatever is left of its delay."""
        host = host_of(url)
        self._lock_for(host).acquire()
        # The wait happens AFTER the lock is taken, not before: waiting first
        # would let every waiter count down in parallel and then start
        # together, which is the burst the delay exists to prevent.
        while not stopping():
            remaining = self._next_free.get(host, 0.0) - self._clock()
            if remaining <= 0:
                break
            self._sleep(min(remaining, 1.0))
        return host

    def release(self, host: str, delay_seconds: float) -> None:
        self._next_free[host] = self._clock() + max(0.0, delay_seconds)
        lock = self._lock_for(host)
        if lock.locked():
            lock.release()


class Pacer:
    """The delay INSIDE one crawl: page two, subpage three, the next file.

    `HostThrottle` guards the boundary between two sources; between the
    requests of a single run there is nothing to lock, only something to wait
    for. The first request does not wait - the pause belongs between two
    requests, and paying it before the first one only makes every run longer.

    >>> waits = []
    >>> p = Pacer(2.0, sleep=waits.append)
    >>> p.wait(); p.wait(); p.wait()
    >>> waits
    [2.0, 2.0]

    A crawl-delay from robots.txt raises it; the source's own setting is a
    floor, never a ceiling:

    >>> Pacer(6.0, robots_delay=1.0).delay
    6.0
    >>> Pacer(6.0, robots_delay=20.0).delay
    20.0
    """

    def __init__(self, delay_seconds: float, *, robots_delay: float | None = None,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.delay = max(float(delay_seconds), float(robots_delay or 0.0))
        self._sleep = sleep
        self._first = True
        #: What was actually waited, for the run report and for the tests.
        self.waited = 0.0

    def wait(self) -> None:
        if self._first:
            self._first = False
            return
        if self.delay > 0:
            self.waited += self.delay
            self._sleep(self.delay)

    def slow_down(self, factor: float = 2.0) -> float:
        """Double the delay after a 429 or 503 - the site asked for room."""
        self.delay = max(1.0, self.delay * factor)
        return self.delay


__all__ = ["HostThrottle", "Pacer", "host_of"]
