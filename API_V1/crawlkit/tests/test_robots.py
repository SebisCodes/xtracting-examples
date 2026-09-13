"""robots.txt: the four answers, and the two that surprise people.

Everything here runs against a fake transport rather than a server. What is
being checked is the MAPPING - which status leads to which verdict - and that
mapping is the part that decides whether a customer's crawler is a good
citizen or a nuisance. A test that needed a network to answer that would be
skipped in CI, which is where it matters most.
"""

from __future__ import annotations

import pytest

from crawlkit.robots import MAX_CRAWL_DELAY, AllowAll, RobotsCache

ESTATE = ("User-agent: *\n"
          "Disallow: /*-srp-new/*\n"
          "Disallow: /*?*ep=\n"
          "Disallow: /files/private/\n"
          "Crawl-delay: 1\n")


def transport(answers: dict):
    """A fetch function that answers from a dictionary, and counts the calls."""
    calls: list[str] = []

    def fetch(url: str, **_kwargs):
        calls.append(url)
        answer = answers[url]
        if isinstance(answer, Exception):
            raise answer
        return answer

    fetch.calls = calls  # type: ignore[attr-defined]
    return fetch


def test_wildcard_rules_are_honoured():
    """The reason for protego. `urllib.robotparser` matches prefixes only and
    would fetch every one of these three."""
    cache = RobotsCache("test-agent", fetch=transport(
        {"https://estate.example/robots.txt": (200, ESTATE)}))

    assert cache.check("https://estate.example/rent/40012300").allowed
    assert not cache.check("https://estate.example/search-srp-new/zurich").allowed
    assert not cache.check(
        "https://estate.example/rent/apartment/city-zurich/matching-list?ep=2").allowed
    assert not cache.check("https://estate.example/files/private/x.pdf").allowed


def test_the_forbidding_rule_is_quoted_in_the_reason():
    """The banner in the editor shows this sentence. It has to name the host
    and the agent, or a person cannot tell whose rule stopped them."""
    cache = RobotsCache("test-agent", fetch=transport(
        {"https://estate.example/robots.txt": (200, ESTATE)}))
    verdict = cache.check("https://estate.example/files/private/x.pdf")
    assert "estate.example" in verdict.reason
    assert "test-agent" in verdict.reason


def test_404_means_no_restriction_and_says_it_was_inferred():
    """RFC 9309: "not there" is not "forbidden"."""
    cache = RobotsCache("test-agent", fetch=transport(
        {"https://open.example/robots.txt": (404, "not found")}))
    verdict = cache.check("https://open.example/anything")
    assert verdict.allowed
    assert verdict.inferred
    assert "404" in verdict.reason


def test_403_is_a_4xx_too():
    """A protection layer that answers 403 to robots.txt has stated no rules.
    The permission is derived, and the note has to say so - otherwise nobody
    can tell a read permission from an assumed one."""
    cache = RobotsCache("test-agent", fetch=transport(
        {"https://guarded.example/robots.txt": (403, "<html>Forbidden</html>")}))
    verdict = cache.check("https://guarded.example/list")
    assert verdict.allowed and verdict.inferred
    assert "403" in verdict.reason


def test_503_forbids_everything():
    """Not "there are no rules" but "I cannot say which rules apply"."""
    cache = RobotsCache("test-agent", fetch=transport(
        {"https://broken.example/robots.txt": (503, "unavailable")}))
    verdict = cache.check("https://broken.example/list")
    assert not verdict.allowed
    assert "503" in verdict.reason


def test_a_network_error_forbids_everything_too():
    cache = RobotsCache("test-agent", fetch=transport(
        {"https://gone.example/robots.txt": TimeoutError("timed out")}))
    verdict = cache.check("https://gone.example/list")
    assert not verdict.allowed
    assert "timed out" in verdict.reason


def test_crawl_delay_is_reported():
    cache = RobotsCache("test-agent", fetch=transport(
        {"https://estate.example/robots.txt": (200, ESTATE)}))
    assert cache.crawl_delay("https://estate.example/rent/1") == 1.0


def test_a_delay_above_the_ceiling_is_a_refusal():
    """Above a minute this is not a slow site, it is an unusable one - and the
    person configuring it should be told that while they are configuring, not
    after a run that took four hours."""
    body = f"User-agent: *\nCrawl-delay: {int(MAX_CRAWL_DELAY) + 60}\n"
    cache = RobotsCache("test-agent", fetch=transport(
        {"https://slow.example/robots.txt": (200, body)}))
    verdict = cache.check("https://slow.example/list")
    assert not verdict.allowed and verdict.too_slow
    assert "too slow to crawl" in verdict.reason


def test_one_fetch_per_host_within_the_ttl():
    """Fifty detail pages must not mean fifty robots.txt requests to a site we
    are being polite to."""
    fetch = transport({"https://estate.example/robots.txt": (200, ESTATE)})
    cache = RobotsCache("test-agent", fetch=fetch)
    for number in range(20):
        cache.check(f"https://estate.example/rent/{number}")
    assert len(fetch.calls) == 1


def test_the_cache_expires():
    clock = [0.0]
    fetch = transport({"https://estate.example/robots.txt": (200, ESTATE)})
    cache = RobotsCache("test-agent", ttl_seconds=60, fetch=fetch,
                        clock=lambda: clock[0])
    cache.check("https://estate.example/rent/1")
    clock[0] = 30.0
    cache.check("https://estate.example/rent/2")
    assert len(fetch.calls) == 1
    clock[0] = 120.0
    cache.check("https://estate.example/rent/3")
    assert len(fetch.calls) == 2


def test_known_only_answers_for_hosts_already_read():
    """What keeps a popup from fetching twenty robots.txt files to fill in a
    column for links nobody will ever crawl."""
    cache = RobotsCache("test-agent", fetch=transport(
        {"https://estate.example/robots.txt": (200, ESTATE)}))
    assert not cache.known("https://estate.example/rent/1")
    cache.check("https://estate.example/rent/1")
    assert cache.known("https://estate.example/rent/2")
    assert not cache.known("https://elsewhere.example/x")


def test_switching_robots_off_is_one_object_not_a_second_gate():
    """`respect_robots = false` chooses `AllowAll`; nothing in the crawl asks
    "am I allowed to ignore robots today?". Two places that may block are two
    places to look for a bug, and the second one is the one nobody checks."""
    cache = AllowAll("test-agent")
    verdict = cache.check("https://estate.example/files/private/x.pdf")
    assert verdict.allowed
    assert "switched off" in verdict.reason


@pytest.mark.parametrize("url", ["", "not a url", "ftp://a.example/x"])
def test_nonsense_addresses_are_refused_not_crashed(url):
    cache = RobotsCache("test-agent", fetch=transport({}))
    assert not cache.check(url).allowed
