"""The crawl itself: pagination, the next link, and what a challenge is.

Against a real HTTP server in this process, not a mock. Pagination is a
conversation with a site - fetch, look, decide - and the four stop rules are
about what a site does when it is asked for a page that does not exist. A
mocked fetch would let the test decide that, which is exactly the wrong way
round.
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest

from crawlkit.crawl import Source, run

ROBOTS = "User-agent: *\nAllow: /\n"


def _page(body: str) -> bytes:
    return (f"<!doctype html><html><head><title>List</title></head>"
            f"<body>{body}</body></html>").encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    log: list

    def log_message(self, *args: object) -> None:
        pass

    def _send(self, status: int, body: bytes, content_type="text/html; charset=utf-8"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlsplit(self.path)
        page = int((parse_qs(parsed.query).get("p") or ["1"])[0])
        self.log.append(self.path)

        if parsed.path == "/robots.txt":
            self._send(200, ROBOTS.encode(), "text/plain")
            return

        if parsed.path == "/loop":
            # Page two links back to page one - the shape of a list whose last
            # page carries a "next" that wraps around.
            following = 2 if page == 1 else 1
            self._send(200, _page(
                f'<a href="/loop/{page}a">A{page}</a><a href="/loop/{page}b">B{page}</a>'
                f'<a href="/loop?p={following}">Weiter</a>'))
            return

        if parsed.path == "/same":
            # Every page answers with the same two entries: a site that
            # ignores the paging parameter looks like an endless list.
            self._send(200, _page(
                '<a href="/same/a">A</a><a href="/same/b">B</a>'
                f'<a href="/same?p={page + 1}">Weiter</a>'))
            return

        if parsed.path == "/deep":
            self._send(200, _page(
                f'<a href="/deep/{page}a">A{page}</a><a href="/deep/{page}b">B{page}</a>'
                f'<a href="/deep?p=1">1</a>'
                f'<a href="/deep?p={page + 1}">Weiter</a>'))
            return

        if parsed.path == "/decoy":
            # A rel=next that points at the newsletter, and the real paging
            # links beside it.
            self._send(200, _page(
                '<a rel="next" href="/newsletter">Newsletter</a>'
                f'<a href="/decoy/{page}a">A{page}</a>'
                f'<a href="/decoy?p={page + 1}">{page + 1}</a>'))
            return

        if parsed.path == "/busy/list":
            # A list page whose detail pages ask for room after the first one.
            self._send(200, _page('<a href="/busy/1">One</a>'
                                  '<a href="/busy/2">Two</a>'
                                  '<a href="/busy/3">Three</a>'))
            return

        if parsed.path.startswith("/busy/"):
            if parsed.path == "/busy/1":
                self._send(200, _page("<h1>One</h1><p>Content.</p>"))
                return
            body = _page("<h1>Slow down</h1>")
            self.send_response(429)
            self.send_header("Retry-After", "5")
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if parsed.path == "/missing":
            self._send(404, _page("<h1>404</h1>"))
            return

        if parsed.path == "/challenge":
            self._send(403, _page("<h1>Just a moment...</h1>"
                                  "<p>Checking your browser before access.</p>"))
            return

        self._send(200, _page(f"<h1>{parsed.path}</h1><p>Content.</p>"))


@pytest.fixture()
def site():
    """One server per test, with its access log."""
    handler = type("BoundHandler", (Handler,), {"log": []})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    server.base_url = f"http://127.0.0.1:{server.server_address[1]}"  # type: ignore[attr-defined]
    server.log = handler.log  # type: ignore[attr-defined]
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


def crawl(site, path: str, **fields):
    """A run against `path`, with the delay recorded instead of waited out."""
    waits: list[float] = []
    source = Source.from_config({
        "bigint_id": 1, "text_name": "list", "text_list_url": site.base_url + path,
        "text_mode": "all_except_rejected", "integer_politeness_seconds": 3,
        **fields})
    result = run(source, None, True, sleep=waits.append, sample_subpages=0)
    result.waits = waits  # type: ignore[attr-defined]
    return result


# -- the four stop rules -----------------------------------------------

def test_pagination_stops_when_it_loops_back(site):
    result = crawl(site, "/loop?p=1", integer_max_pages=10)
    assert len(result.pages) == 2
    assert any("already read" in warning for warning in result.warnings)


def test_pagination_stops_when_the_next_page_brings_nothing_new(site):
    """A site that ignores the paging parameter answers 200 to every page
    number. Without this rule the page cap would be what stops the crawl -
    after ten pointless requests to a site we are being polite towards."""
    result = crawl(site, "/same", integer_max_pages=10)
    assert len(result.pages) == 1
    assert any("no new links" in warning for warning in result.warnings)
    assert len([path for path in site.log if path.startswith("/same?")]) == 1


def test_the_page_cap_holds_and_says_the_list_has_more(site):
    result = crawl(site, "/deep", integer_max_pages=3)
    assert len(result.pages) == 3
    assert any("stopped after 3 pages" in warning for warning in result.warnings)


def test_a_test_crawl_never_reads_more_than_three_pages(site):
    """`dry_run` caps it below whatever the source says: a test is meant to
    show what the site answers, not to walk it."""
    result = crawl(site, "/deep", integer_max_pages=50)
    assert len(result.pages) == 3


def test_pagination_can_be_switched_off(site):
    result = crawl(site, "/deep", bool_follow_pagination=False)
    assert len(result.pages) == 1


# -- the next link ------------------------------------------------------

def test_the_next_link_is_not_a_document(site):
    """It is an ordinary <a href> on the page. Left in the harvest it would be
    fetched as a detail page AND counted as a new link on every round, which
    blunts the "nothing new" rule."""
    result = crawl(site, "/deep", integer_max_pages=2)
    accepted = result.accepted
    assert not any("?p=" in url for url in accepted)
    assert "/deep/1a" in " ".join(accepted)
    # It is in the result, classified, so the picker can show it as pagination.
    pagination = [link for link in result.links if link.cls == "pagination"]
    assert pagination and not any(link.accepted_by_config for link in pagination)


def test_the_counts_account_for_every_link_the_page_had(site):
    """The dashboard prints these five numbers next to each other, and they
    have to add up on the screen: `rejected` counts DOCUMENTS no rule keeps,
    so the two classes that are not documents at all - the paging links and
    the list page's own address - are the rest of `links` and are counted on
    their own. Without `list_self` the editor's row was short by however
    many times the list linked to itself, and nothing on the page said so.
    """
    counts = crawl(site, "/deep", integer_max_pages=2).counts
    assert counts["links"] == (counts["accepted"] + counts["rejected"]
                               + counts["pagination"] + counts["list_self"])
    assert counts["pagination"] > 0


def test_the_learned_parameter_beats_a_rel_next_that_points_elsewhere(site):
    result = crawl(site, "/decoy", text_paging_param="p", integer_max_pages=2)
    assert len(result.pages) == 2
    assert not any(path.startswith("/newsletter") for path in site.log)


# -- politeness ---------------------------------------------------------

def test_the_delay_is_paid_between_pages_and_not_before_the_first(site):
    result = crawl(site, "/deep", integer_max_pages=3)
    assert result.waits == [3.0, 3.0]


# -- what the site answers ---------------------------------------------

def test_a_browser_check_is_told_apart_from_a_refusal(site):
    """403 alone cannot say which it is, and the two need different answers:
    a challenge means "this site wants a browser", a refusal means "this
    address is not for you"."""
    result = crawl(site, "/challenge")
    assert result.status == "BLOCKED"
    assert result.fetch["challenge"] is True
    assert any("browser check" in warning for warning in result.warnings)


def test_a_missing_page_says_what_to_change(site):
    """A banner that says "HTTP 404" tells a person nothing they can act on.
    "Check the list address" does."""
    result = crawl(site, "/missing")
    assert result.status == "BLOCKED"
    assert result.fetch["status"] == 404
    assert any("Check the list address" in warning for warning in result.warnings)


def test_a_site_that_asks_for_room_ends_the_run_and_says_the_new_distance(site):
    """429 is not a fault of the address, it is a request for distance. The run
    stops there rather than working through the rest of the list, and the
    delay it would use next is named - "it slowed down" is not something a
    person should have to infer from a log."""
    waits = []
    source = Source.from_config({
        "bigint_id": 1, "text_list_url": site.base_url + "/busy/list",
        "text_mode": "all_except_rejected", "integer_politeness_seconds": 3})
    result = run(source, None, False, sleep=waits.append)

    assert result.status == "BLOCKED"
    assert result.retry_after == 5.0
    assert any("asked for room" in warning for warning in result.warnings)
    assert any("doubled to 6 seconds" in warning for warning in result.warnings)
    # One good page, then the refusal - the third is not even tried.
    assert len([path for path in site.log if path.startswith("/busy/")]) == 3


def test_the_run_records_every_page_it_read(site):
    result = crawl(site, "/deep", integer_max_pages=2)
    assert [page.status for page in result.pages] == [200, 200]
    assert all(page.links for page in result.pages)
    assert result.counts["pages"] == 2
