"""What a fetch refuses: private addresses at every step, and bodies that
are not pages.

Against a real HTTP server in this process, like `test_crawl_rules.py`. The
guard here is a dictionary rather than DNS: every address the server has is
on loopback, so the fake resolver decides which of its paths count as
"public" and which as "the inside of the operator's network". What is
checked is that the right addresses are put to the guard at the right
moment - before the request, and for every redirect hop - and that a
refusal leaves nothing behind.

    python -m pytest crawlkit -q
"""

from __future__ import annotations

import gzip
import io
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from crawlkit import files
from crawlkit.crawl import Source, run
from crawlkit.fetch import FetchError, HttpxEngine, PlaywrightEngine, playwright_available
from crawlkit.robots import RobotsCache, fetch_robots

PRIVATE = "This address points at a private network"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    log: list

    def log_message(self, *args: object) -> None:
        pass

    def _send(self, status: int, body: bytes, content_type="text/html; charset=utf-8",
              **headers: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        if "content_length" not in headers:
            self.send_header("Content-Length", str(len(body)))
        for name, value in headers.items():
            self.send_header(name.replace("_", "-"), value)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        self.log.append(self.path)
        path = self.path
        if path == "/robots.txt":
            self._send(200, b"User-agent: *\nAllow: /\n", "text/plain")
        elif path == "/to-private":
            self._send(302, b"", location="/private/secret")
        elif path == "/to-private-twice":
            self._send(302, b"", location="/to-private")
        elif path == "/to-page":
            self._send(302, b"", location="/page?from=redirect")
        elif path.startswith("/page"):
            self._send(200, b'<html><body><h1>Page</h1><a href="/doc/1">One</a></body></html>')
        elif path == "/bomb":
            body = gzip.compress(b"<html><body>" + b"A" * (12 * 1024 * 1024) + b"</body></html>")
            self._send(200, body, content_encoding="gzip")
        elif path == "/announced":
            self._send(200, b"<html></html>", content_length="99999999999")
        elif path == "/private/secret":
            self._send(200, b"<html><body>the inside</body></html>")
        elif path == "/list":
            self._send(200, b'<html><body><a href="/private/secret">x</a>'
                            b'<a href="/doc/1">one</a><a href="/private/file.txt">f</a>'
                            b'<a href="/to-private-file.txt">g</a></body></html>')
        elif path == "/doc/1":
            self._send(200, b"<html><body><h1>Doc</h1>text</body></html>")
        elif path == "/private/file.txt":
            self._send(200, b"the inside as a file", "text/plain")
        elif path == "/to-private-file.txt":
            self._send(302, b"", location="/private/file.txt")
        else:
            self._send(404, b"<html><body>gone</body></html>")


@pytest.fixture()
def site():
    log: list = []
    handler = type("H", (Handler,), {"log": log})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield base, log
    finally:
        server.shutdown()
        server.server_close()


def guard_by_path(url: str) -> None:
    """The guard of these tests: `/private/...` is the inside of the network."""
    if "/private/" in url:
        raise ValueError(f"{PRIVATE} ({url}) and cannot be tested.")


# -- the HTTP engine -----------------------------------------------------

def test_a_redirect_into_the_private_network_is_refused_before_it_is_requested(site):
    base, log = site
    with HttpxEngine("test", guard=guard_by_path) as engine:
        with pytest.raises(FetchError) as info:
            engine.fetch(base + "/to-private")
        assert PRIVATE in str(info.value)
        with pytest.raises(FetchError):
            engine.fetch(base + "/to-private-twice")
    assert "/private/secret" not in log, "the private address was requested"


def test_a_refusal_is_a_fetch_error_and_not_the_end_of_the_run(site):
    """One subpage redirecting inwards is one warning; the others are read."""
    base, log = site
    source = Source.from_config({"text_list_url": base + "/list",
                                 "text_mode": "all_except_rejected",
                                 "integer_politeness_seconds": 0})
    result = run(source, guard=guard_by_path, sleep=lambda s: None)
    assert result.status == "OK"
    assert result.counts["queued"] == 1
    assert "/private/secret" not in log
    assert any(PRIVATE in warning for warning in result.warnings)


def test_a_redirect_to_a_public_page_is_followed_and_the_guard_sees_every_hop(site):
    base, log = site
    seen: list[str] = []
    with HttpxEngine("test", guard=seen.append) as engine:
        page = engine.fetch(base + "/to-page")
    assert page.ok and page.final_url == base + "/page?from=redirect"
    # Both hops, and the address the socket reached once connected - the
    # check that a name resolving differently the second time cannot get past.
    assert seen == [base + "/to-page", base + "/", base + "/page?from=redirect", base + "/"]


def test_the_connected_peer_is_judged_before_the_body_is_read(site):
    """A name that resolves to a public address for the guard and to a
    private one for the connection - DNS rebinding - is caught at the
    socket: the body is never read."""
    base, log = site

    def guard(url: str) -> None:
        # The name passes; the peer the socket reached is loopback and does not.
        if url.startswith("http://127.0.0.1:") and url.endswith("/"):
            raise ValueError(f"{PRIVATE} (127.0.0.1) and cannot be tested.")

    with HttpxEngine("test", guard=guard) as engine:
        with pytest.raises(FetchError) as info:
            engine.fetch(base + "/page")
    assert "127.0.0.1" in str(info.value)


def test_a_body_larger_than_the_cap_is_refused_after_decompression(site):
    """A gzip bomb is small on the wire and huge in memory. The cap is on
    what is kept, and the reading stops there."""
    base, log = site
    with HttpxEngine("test", max_bytes=1024 * 1024) as engine:
        with pytest.raises(FetchError) as info:
            engine.fetch(base + "/bomb")
        assert "not a page" in str(info.value)
        with pytest.raises(FetchError) as info:
            engine.fetch(base + "/announced")
        assert "not a page" in str(info.value)
        assert engine.fetch(base + "/page").ok


# -- robots.txt and files ------------------------------------------------

def test_robots_txt_puts_its_redirects_to_the_guard(site):
    base, log = site
    seen: list[str] = []
    fetch_robots(base + "/to-page", user_agent="test", timeout=5, guard=seen.append)
    assert seen == [base + "/to-page", base + "/page?from=redirect"]

    cache = RobotsCache("test", guard=guard_by_path)
    cache.check(base + "/private/x")  # the origin is loopback, allowed by the guard
    with pytest.raises(ValueError):
        fetch_robots(base + "/to-private", user_agent="test", timeout=5,
                     guard=guard_by_path)
    assert "/private/secret" not in log


def test_a_file_behind_a_redirect_into_the_private_network_is_an_error_row(site):
    base, log = site
    loaded = files.download(base + "/to-private-file.txt", "txt", user_agent="test",
                            max_bytes=1000, guard=guard_by_path)
    assert loaded.status == "ERROR"
    assert PRIVATE in loaded.detail
    assert "/private/file.txt" not in log

    loaded = files.download(base + "/private/file.txt", "txt", user_agent="test",
                            max_bytes=1000, guard=guard_by_path)
    assert loaded.status == "ERROR"
    assert "/private/file.txt" not in log


def test_the_crawl_hands_the_guard_to_the_file_download(site):
    base, log = site
    source = Source.from_config({
        "text_list_url": base + "/list", "text_mode": "all_except_rejected",
        "bool_sub_sub": True, "integer_politeness_seconds": 0,
        "file_rules": [{"text_kind": "type_default", "text_value": "txt",
                        "bool_enabled": True}],
    })
    result = run(source, guard=guard_by_path, sleep=lambda s: None)
    assert result.status == "OK"
    statuses = {record.url: record.status for record in result.files}
    assert all(status == "ERROR" for status in statuses.values()), statuses
    assert "/private/file.txt" not in log


# -- what a file may hold ------------------------------------------------

def _zip_with(name: str, size: int) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr(name, b"<w:p>" + b"a" * size + b"</w:p>")
    return buffer.getvalue()


def test_a_zip_that_inflates_beyond_the_cap_is_not_read(monkeypatch):
    """A .docx is a zip; a zip can hold a kilobyte that inflates to a
    gigabyte. The sizes are in the directory, and that is all that is read."""
    monkeypatch.setattr(files, "MAX_MEMBER_BYTES", 1024)
    bomb = _zip_with("word/document.xml", 10_000)
    text, reason = files.to_text(bomb, "docx")
    assert text == ""
    assert "once unpacked" in reason
    small = _zip_with("word/document.xml", 100)
    assert files.to_text(small, "docx")[0] == "a" * 100


def test_the_text_of_a_file_stops_at_the_cap(monkeypatch):
    monkeypatch.setattr(files, "MAX_TEXT_CHARS", 50)
    from crawlkit.tests.standin.site import pdf_bytes, xlsx_bytes
    text, _ = files.to_text(xlsx_bytes([["x" * 30] for _ in range(20)]), "xlsx")
    assert len(text) <= 50
    text, _ = files.to_text(pdf_bytes(["y" * 30] * 20), "pdf")
    assert len(text) <= 50


# -- the browser ---------------------------------------------------------

needs_browser = pytest.mark.skipif(not playwright_available(),
                                   reason="no Chromium in this environment")


@needs_browser
def test_the_browser_refuses_what_the_page_asks_for_inside_the_network(site):
    """A rendered page runs its scripts, and a script can fetch anything.
    The route sees the request before the browser sends it."""
    base, log = site

    class Scripted(Handler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/scripted":
                self.log.append(self.path)
                self._send(200, b'<html><body><script>fetch("/private/secret");'
                                b'</script><iframe src="/private/frame"></iframe>'
                                b"rendered</body></html>")
            else:
                super().do_GET()

    server = ThreadingHTTPServer(("127.0.0.1", 0), type("S", (Scripted,), {"log": log}))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}"
        with PlaywrightEngine("test", guard=guard_by_path) as engine:
            page = engine.fetch(url + "/scripted")
            assert "rendered" in page.html
            assert not any("/private/" in path for path in log), log

            with pytest.raises(FetchError) as info:
                engine.fetch(url + "/to-private-twice")
            assert PRIVATE in str(info.value)
            assert not any("/private/" in path for path in log), log

            page = engine.fetch(url + "/to-page")
            assert page.final_url == url + "/page?from=redirect"
            with pytest.raises(FetchError):
                engine.fetch(url + "/private/secret")
    finally:
        server.shutdown()
        server.server_close()


def test_a_site_that_drips_is_cut_off_by_the_budget():
    """The timeout is per read; a server that sends a byte at a time never
    trips it. The budget over the whole fetch does."""
    import time

    class Drip(Handler):
        def do_GET(self) -> None:  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                for _ in range(200):
                    self.wfile.write(b"<p>" + b"x" * 70000 + b"</p>")
                    self.wfile.flush()
                    time.sleep(0.05)
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), type("D", (Drip,), {"log": []}))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/drip"
        started = time.monotonic()
        with HttpxEngine("test", timeout=0.1) as engine:
            with pytest.raises(FetchError) as info:
                engine.fetch(url)
        assert "dripping" in str(info.value)
        assert time.monotonic() - started < 5
    finally:
        server.shutdown()
        server.server_close()
