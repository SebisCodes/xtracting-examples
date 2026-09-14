"""The gate as a browser meets it, over HTTP and without a database.

    python -m pytest tests/unit/test_gate_http.py -q

test_gate.py checks the functions; this file checks what main.py wraps
around them: the cookie and its flags, the form's answers, the throttle, the
refusal of a form posted from another site, and the headers every answer
carries. The application is built with a password in the environment and no
archive behind it - the gate must not need one to say no.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import gate


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://nobody:nothing@127.0.0.1:1/none")
    monkeypatch.setenv("DASHBOARD_GATE_PASSWORD", "johndoe:one,janedoe:two")
    # The pause after a refusal is the handler's, on the event loop; a test
    # that waits a second per wrong password is a slow test for nothing.
    monkeypatch.setattr(gate, "refusal_pause", lambda: 0.0)
    gate.reset_cache()
    from app.main import create_app
    # No lifespan (no `with`): the archive is never opened, and the gate has
    # to work anyway.
    yield TestClient(create_app(), base_url="http://dashboard.test")
    gate.reset_cache()


def cookie_header(response) -> str:
    return response.headers.get("set-cookie", "")


# ── Passing and not passing ──────────────────────────────────

def test_a_page_behind_the_gate_redirects_to_the_form(client):
    response = client.get("/query?x=1", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/gate?next=%2Fquery%3Fx%3D1"


def test_an_api_call_gets_a_readable_401(client):
    response = client.get("/api/projects")
    assert response.status_code == 401
    assert response.json()["error"] == "this dashboard is closed"


def test_the_wrong_password_is_refused_without_a_cookie(client):
    response = client.post("/gate", data={"password": "nope"})
    assert response.status_code == 401
    assert gate.REFUSED in response.text
    assert not cookie_header(response)


def test_the_right_password_sets_a_cookie_with_the_right_flags(client):
    response = client.post("/gate", data={"password": "two", "next": "/map"},
                           follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/map"
    header = cookie_header(response).lower()
    assert header.startswith(f"{gate.COOKIE_NAME}=")
    assert "httponly" in header
    assert "samesite=lax" in header
    assert "path=/" in header
    # Plain http: a Secure cookie would be dropped and the form would loop.
    assert "secure" not in header


def test_behind_a_tls_proxy_the_cookie_is_secure(client):
    response = client.post("/gate", data={"password": "two"},
                           headers={"X-Forwarded-Proto": "https"},
                           follow_redirects=False)
    assert "secure" in cookie_header(response).lower()


def test_a_cookie_with_a_character_outside_ascii_is_a_refusal_not_a_crash(client):
    """`compare_digest` on two str raises on anything above U+007F, and this
    value is whatever the browser sent - left unguarded, that is a 500 on
    every page."""
    # As a raw header: the test client refuses to build such a cookie itself,
    # which is exactly why it has to be tried.
    raw = f"{gate.COOKIE_NAME}=123.\u00fc".encode("utf-8")
    response = client.get("/", headers={"cookie": raw}, follow_redirects=False)
    assert response.status_code == 303


def test_next_only_ever_points_at_this_dashboard(client):
    for evil in ("//evil.example", "https://evil.example", "/\\evil.example"):
        response = client.post("/gate", data={"password": "one", "next": evil},
                               follow_redirects=False)
        assert response.headers["location"] == "/", evil


@pytest.mark.parametrize("host", ["evil.example/gate", "evil.example/healthz",
                                  "dashboard.test/static/", "/gate"])
def test_the_host_header_has_no_say_in_which_path_is_open(client, host):
    """The open paths are read from the request line alone. A Host header
    is the sender's to write, and a URL assembled from it must not be what
    decides whether a page is behind the gate."""
    response = client.get("/api/projects", headers={"Host": host})
    assert response.status_code == 401
    response = client.get("/query", headers={"Host": host}, follow_redirects=False)
    assert response.status_code == 303


# ── The throttle ─────────────────────────────────────────────

def test_an_address_that_keeps_guessing_is_throttled_even_with_the_right_password(client):
    for _ in range(gate.MAX_FAILURES):
        assert client.post("/gate", data={"password": "nope"}).status_code == 401
    throttled = client.post("/gate", data={"password": "nope"})
    assert throttled.status_code == 429
    assert gate.THROTTLED in throttled.text
    # The password is not even looked at.
    assert client.post("/gate", data={"password": "one"},
                       follow_redirects=False).status_code == 429


def test_a_right_answer_forgets_the_wrong_ones_before_it(client):
    for _ in range(gate.MAX_FAILURES - 1):
        client.post("/gate", data={"password": "nope"})
    assert client.post("/gate", data={"password": "one"},
                       follow_redirects=False).status_code == 303
    for _ in range(gate.MAX_FAILURES - 1):
        assert client.post("/gate", data={"password": "nope"}).status_code == 401


def test_the_throttle_forgets_after_the_window(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(gate.time, "monotonic", lambda: clock[0])
    throttle = gate.Throttle(max_failures=2, window_seconds=60)
    throttle.record_failure("a")
    throttle.record_failure("a")
    assert throttle.blocked("a")
    assert not throttle.blocked("b")
    clock[0] += 61
    assert not throttle.blocked("a")


def test_the_throttle_remembers_a_bounded_number_of_addresses(monkeypatch):
    monkeypatch.setattr(gate, "MAX_ADDRESSES", 3)
    throttle = gate.Throttle()
    for address in ("a", "b", "c", "d"):
        throttle.record_failure(address)
    assert len(throttle._failures) == 3
    assert "a" not in throttle._failures


def test_a_refusal_no_longer_sleeps_inside_the_attempt(monkeypatch):
    """The pause is awaited by the handler on the event loop. Slept inside
    `attempt` it held every request of every visitor for its length."""
    def no_sleeping(_seconds):
        raise AssertionError("attempt() slept")
    monkeypatch.setattr(gate.time, "sleep", no_sleeping)
    ok, message = gate.attempt("nope", "", gate.GateConfig(tokens={"johndoe": "one"}))
    assert (ok, message) == (False, gate.REFUSED)
    assert 1.0 <= gate.refusal_pause() < 1.5


# ── Writes from another site ─────────────────────────────────

@pytest.mark.parametrize("headers", [
    {"Origin": "https://evil.example"},
    {"Origin": "null"},
    {"Sec-Fetch-Site": "cross-site"},
    {"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"},
    {"Origin": "https://evil.example", "X-Forwarded-Host": "dash.example"},
])
def test_a_form_posted_from_another_site_is_refused(client, headers):
    response = client.post("/gate", data={"password": "one"}, headers=headers,
                           follow_redirects=False)
    assert response.status_code == 403
    assert not cookie_header(response)


def test_an_api_write_from_another_site_is_refused_before_the_route(client):
    response = client.post("/api/crawler/state", json={"running": True},
                           headers={"Origin": "https://evil.example"})
    assert response.status_code == 403
    assert response.json()["error"].startswith("this request came from another site")


@pytest.mark.parametrize("headers", [
    {},
    {"Origin": "http://dashboard.test"},
    {"Origin": "http://DASHBOARD.test", "Sec-Fetch-Site": "same-origin"},
    {"Sec-Fetch-Site": "none"},
    # A proxy that rewrites Host on the way in, and an older browser that
    # sends no Sec-Fetch-Site: the name the browser used is in the
    # forwarded header.
    {"Origin": "https://dash.example", "X-Forwarded-Host": "dash.example"},
])
def test_the_dashboard_s_own_pages_and_plain_clients_may_write(client, headers):
    response = client.post("/gate", data={"password": "one"}, headers=headers,
                           follow_redirects=False)
    assert response.status_code == 303


def test_reads_from_another_site_are_not_the_guard_s_business(client):
    """A cross-site GET is what a link is. The gate decides those."""
    response = client.get("/healthz", headers={"Sec-Fetch-Site": "cross-site"})
    assert response.status_code == 200


# ── Headers on every answer ──────────────────────────────────

@pytest.mark.parametrize("path", ["/healthz", "/gate", "/api/projects", "/query"])
def test_every_answer_carries_the_security_headers(client, path):
    response = client.get(path, follow_redirects=False)
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "strict-origin-when-cross-origin"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["content-security-policy"] == "frame-ancestors 'none'"
