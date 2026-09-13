"""The password gate, in the parts that need no browser.

    python -m pytest tests/unit/test_gate.py -q

app/gate.py carries doctests for the shapes; this file is for the claims a
doctest reads badly: that the gate is OFF unless a password is set, that a
cookie stops verifying the moment the password list changes, and that the
paths left open are exactly the three that have to be.

Since the gate reads a second source it also pins the two halves of that
bargain, which pull in opposite directions and are easy to get backwards: a
token in dashboard.access_tokens can close a gate no environment variable
mentions, and it must NOT end anybody's session when it arrives - while
changing DASHBOARD_GATE_PASSWORD must still end all of them.

THE ARCHIVE HERE IS A FAKE, and it answers exactly the two statements
gate.py sends, refusing anything else so that a third one cannot pass
unnoticed. Its liveness rule mirrors the WHERE clause of
gate.LIVE_TOKENS_SQL; the clause itself is checked as SQL, against a real
PostgreSQL, by section 5 of dashboard/sql/verify.sql.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest
import urllib.parse

from app import db as db_module
from app import gate


def cfg(raw: str = "anna:one,ben:two", **kw) -> gate.GateConfig:
    return gate.GateConfig(tokens=gate.parse_tokens(raw), **kw)


# ── A fake archive, for the second source ────────────────────

NOW = datetime.now(timezone.utc)


def row(token: str, label: str = "anna", enabled: bool = True, expires=None) -> dict:
    return {"text_token": token, "text_label": label,
            "bool_enabled": enabled, "date_expires": expires}


LIVE = row("tok_live")
EXPIRED = row("tok_expired", expires=NOW - timedelta(days=1))
DISABLED = row("tok_disabled", enabled=False)
DATED = row("tok_dated", expires=NOW + timedelta(days=30))


class FakeArchive:
    """Enough of app.db.Database for gate.py, and not one statement more."""

    def __init__(self, *rows: dict, fails: bool = False) -> None:
        self.rows = list(rows)
        self.fails = fails
        #: Which tokens were stamped as used, in order.
        self.stamped: list[str] = []
        self.reads = 0

    @contextmanager
    def read(self):
        yield _FakeConnection(self)

    @contextmanager
    def write(self):
        yield _FakeConnection(self)


class _FakeConnection:
    def __init__(self, archive: FakeArchive) -> None:
        self.archive = archive

    def execute(self, sql: str, params=None):
        if self.archive.fails:
            raise RuntimeError("the archive is not answering")
        if sql == gate.LIVE_TOKENS_SQL:
            self.archive.reads += 1
            # The mirror of the WHERE clause; see the module docstring.
            live = [r for r in self.archive.rows
                    if r["bool_enabled"]
                    and (r["date_expires"] is None or r["date_expires"] > NOW)]
            return _Answer(live)
        if "date_last_used" in sql:
            self.archive.stamped.append(params[0])
            return _Answer([])
        if "dashboard.settings" in sql:
            return _Answer([{"text_value": "the-installation-secret"}])
        raise AssertionError(f"the gate sent a statement this fake does not know: {sql}")


class _Answer:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows

    def fetchall(self) -> list[dict]:
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


@pytest.fixture(autouse=True)
def a_clean_memo():
    """Both memos live for the process, so every test starts without one -
    and no test leaves a fake archive behind for the next."""
    gate.reset_cache()
    yield
    db_module.set_db(None)
    gate.reset_cache()


def archive(*rows: dict, fails: bool = False) -> FakeArchive:
    fake = FakeArchive(*rows, fails=fails)
    db_module.set_db(fake)
    return fake


# ── Off unless it is switched on ─────────────────────────────

def test_no_password_means_no_gate():
    """The default, and it has to be: a gate that switched itself on at
    upgrade time would lock a running installation out of its own archive."""
    empty = gate.GateConfig()
    assert not empty.enabled
    assert gate.passed("", empty) is True
    assert gate.passed("nonsense", empty) is True


def test_a_password_closes_it():
    assert cfg().enabled
    assert gate.passed("", cfg()) is False


# ── Several passwords, each with a label ─────────────────────

@pytest.mark.parametrize("raw, expected", [
    ("anna:one,ben:two", {"anna": "one", "ben": "two"}),
    ("one", {"default": "one"}),
    ("anna:a:b:c", {"anna": "a:b:c"}),
    (" anna : one , ben : two ", {"anna": "one", "ben": "two"}),
    ("anna:one,,ben:two", {"anna": "one", "ben": "two"}),
    ("", {}),
])
def test_the_list_is_read_as_written(raw, expected):
    assert gate.parse_tokens(raw) == expected


def test_an_entry_with_no_password_is_dropped():
    """The one mistake this must not accept quietly: an entry that would let
    anybody in with an empty box."""
    assert gate.parse_tokens("anna:,ben:two") == {"ben": "two"}
    assert gate.check_password("", cfg("anna:,ben:two")) == ""


def test_each_password_opens_it_and_says_which_one():
    c = cfg()
    assert gate.check_password("one", c) == "anna"
    assert gate.check_password("two", c) == "ben"
    assert gate.check_password("three", c) == ""
    # An empty box never matches, whatever is configured.
    assert gate.check_password("", c) == ""


def test_a_password_may_carry_the_characters_an_env_file_allows():
    """Everything but the comma, which separates the entries. The colon is
    fine after the first one - that is what makes `a:b:c` a password."""
    raw = "user1:salkfjoi43r340gmeods!.asdf*ç),user2:9042romwfeks"
    parsed = gate.parse_tokens(raw)
    assert parsed == {"user1": "salkfjoi43r340gmeods!.asdf*ç)", "user2": "9042romwfeks"}
    assert gate.check_password("salkfjoi43r340gmeods!.asdf*ç)", gate.GateConfig(tokens=parsed)) == "user1"


# ── The cookie ───────────────────────────────────────────────

def test_a_sealed_cookie_opens_the_gate_and_a_forged_one_does_not():
    c = cfg()
    assert gate.unseal(gate.seal(c), c) is True
    for forged in ("", "nonsense", "123.deadbeef", f"{int(time.time())}."):
        assert gate.unseal(forged, c) is False, forged


def test_changing_the_password_list_ends_every_session():
    """Withdrawing somebody's access is one edit, not a hunt for a session
    store: the cookie is signed with the whole set, so any change to it
    invalidates every cookie issued under the old one - including the ones
    belonging to passwords that stayed."""
    before = cfg("anna:one,ben:two")
    cookie = gate.seal(before)
    assert gate.unseal(cookie, before) is True

    without_ben = cfg("anna:one")
    assert gate.unseal(cookie, without_ben) is False
    changed = cfg("anna:one,ben:CHANGED")
    assert gate.unseal(cookie, changed) is False


def test_a_cookie_expires():
    c = cfg(max_age_seconds=1)
    cookie = gate.seal(c)
    assert gate.unseal(cookie, c) is True
    old = f"{int(time.time()) - 10}"
    assert gate.unseal(f"{old}.{gate._sign(old.encode(), c.signing_key)}", c) is False


# ── What stays reachable ─────────────────────────────────────

@pytest.mark.parametrize("path", ["/healthz", "/gate", "/static/css/app.css"])
def test_the_three_open_paths_are_open(path):
    """/healthz so the container is not permanently unhealthy behind its own
    gate, /gate because it is the form, /static because that is the
    stylesheet the form is drawn with."""
    assert gate.is_open_path(path) is True


@pytest.mark.parametrize("path", [
    "/", "/query", "/diagrams", "/sources", "/api/dashboard/stats",
    # A prefix that merely STARTS with an open one is not open. This is the
    # mistake that turns a three-item allowlist into a hole.
    "/gateway", "/healthzz", "/staticfiles/secret.txt",
])
def test_everything_else_is_closed(path):
    assert gate.is_open_path(path) is False


# ── Turnstile ────────────────────────────────────────────────

def test_either_key_makes_the_check_required():
    """THE RULE THAT KEEPS A TYPO FROM OPENING THE GATE.

    "Both keys before the check counts" is the tempting reading and it is
    the wrong one: delete the site key by accident, or mistype the variable
    name, and a dashboard protected by a bot check quietly becomes a
    password box anybody may hammer. So ANY Turnstile setting means the
    check must pass, and only both empty switches it off.
    """
    assert cfg(turnstile_site_key="a", turnstile_secret="b").turnstile_required
    assert cfg(turnstile_site_key="a").turnstile_required
    assert cfg(turnstile_secret="b").turnstile_required
    assert not cfg().turnstile_required


def test_half_a_configuration_lets_nobody_in():
    """A gate nobody can pass is loud, recoverable and safe. A gate everybody
    can pass is silent."""
    for half in (cfg(turnstile_site_key="a"), cfg(turnstile_secret="b")):
        assert half.turnstile_broken
        assert not half.turnstile_complete
    whole = cfg(turnstile_site_key="a", turnstile_secret="b")
    assert whole.turnstile_complete and not whole.turnstile_broken
    assert not cfg().turnstile_broken


def test_a_half_configured_check_refuses_even_the_right_password(monkeypatch):
    monkeypatch.setattr(gate.time, "sleep", lambda _s: None)
    # A secret with no site key: no widget can be drawn, so no token exists.
    ok, message = gate.attempt("one", "", cfg(turnstile_secret="b"))
    assert ok is False
    # A site key with no secret: a token arrives and there is nothing to
    # verify it with. Refused just the same, and with the same words.
    ok2, message2 = gate.attempt("one", "a-token", cfg(turnstile_site_key="a"))
    assert ok2 is False
    assert message == message2


def test_the_bot_check_fails_closed_without_a_token():
    """A gate that opens when the check cannot be made can be opened by
    making sure it cannot be made."""
    ok, why = gate.verify_turnstile("secret", "")
    assert ok is False and why


def test_cloudflare_is_asked_about_the_widget_token_not_the_password(monkeypatch):
    """The bot check gets what the WIDGET put in the form. It got the archive
    token instead - or nothing, for an environment password - because the two
    shared a name, and no key pair on earth could open that gate."""
    asked: list[tuple[str, str]] = []

    def fake_verify(secret, token, remote_ip=""):
        asked.append((secret, token))
        return True, ""

    monkeypatch.setattr(gate, "verify_turnstile", fake_verify)
    monkeypatch.setattr(gate, "_stamp_used", lambda _t: None)
    c = cfg(turnstile_site_key="site", turnstile_secret="secret")

    # An environment password: the widget token goes to Cloudflare, whole.
    assert gate.attempt("one", "0.widget-token-abc", c) == (True, "")
    assert asked[-1] == ("secret", "0.widget-token-abc")

    # A token from the archive: STILL the widget token, never the archive one.
    monkeypatch.setattr(gate, "database_tokens", lambda: {"tok-from-db": "anna"})
    assert gate.attempt("tok-from-db", "0.widget-token-xyz", c) == (True, "")
    assert asked[-1] == ("secret", "0.widget-token-xyz")


def test_a_private_address_is_not_told_to_cloudflare(monkeypatch):
    """Behind a router that loops LAN clients back in through the public
    name, this process sees the router's private address - and a token
    solved from the public one, verified with that, is refused as forged.
    So private, loopback and link-local addresses stay out of the request;
    a public one goes in."""
    sent: list[dict] = []

    class Reply:
        def __init__(self, body): self._body = body
        def read(self): return self._body
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_urlopen(request, timeout=0):
        sent.append(dict(urllib.parse.parse_qsl(request.data.decode("utf-8"))))
        return Reply(b'{"success": true}')

    monkeypatch.setattr(gate.urllib.request, "urlopen", fake_urlopen)
    for address in ("192.168.50.1", "10.1.1.186", "172.21.0.1", "127.0.0.1", "fe80::1", "", "nonsense"):
        assert gate.verify_turnstile("secret", "tok", address) == (True, "")
        assert "remoteip" not in sent[-1], (address, sent[-1])
    assert gate.verify_turnstile("secret", "tok", "93.184.216.34") == (True, "")
    assert sent[-1]["remoteip"] == "93.184.216.34"



def test_with_the_check_configured_a_right_password_alone_is_not_enough(monkeypatch):
    c = cfg(turnstile_site_key="site", turnstile_secret="secret")
    monkeypatch.setattr(gate.time, "sleep", lambda _s: None)
    monkeypatch.setattr(gate, "verify_turnstile", lambda *a, **k: (False, "refused"))
    ok, message = gate.attempt("one", "a-token", c)
    assert ok is False
    # And the message is the SAME one a wrong password gets: two different
    # messages would say which of the two hurdles was cleared.
    monkeypatch.setattr(gate, "verify_turnstile", lambda *a, **k: (True, ""))
    wrong_ok, wrong_message = gate.attempt("nope", "a-token", c)
    assert wrong_ok is False
    assert message == wrong_message


def test_both_right_opens_it(monkeypatch):
    c = cfg(turnstile_site_key="site", turnstile_secret="secret")
    monkeypatch.setattr(gate, "verify_turnstile", lambda *a, **k: (True, ""))
    assert gate.attempt("two", "a-token", c) == (True, "")


# ── The second source: tokens in the archive ─────────────────

def test_a_token_in_the_archive_opens_a_gate_no_variable_mentions():
    """The point of the second source. Nothing is configured in the
    environment here, which under the older shape meant no gate at all - and
    would have left an installation that hands out tokens open to everybody."""
    archive(LIVE)
    open_env = cfg("")
    assert open_env.enabled is False          # nothing in the environment
    assert gate.in_force(open_env) is True    # and the gate is shut anyway
    assert gate.check_password("tok_live", open_env) == "anna"
    assert gate.passed("", open_env) is False
    assert gate.unseal(gate.seal(open_env), open_env) is True


def test_a_token_works_beside_the_environment_passwords():
    """Neither source shadows the other: both open the same gate, and each
    keeps its own label for the log line that says which one was used."""
    archive(row("tok_ben", label="ben-on-the-road"))
    c = cfg("anna:one")
    assert gate.check_password("one", c) == "anna"
    assert gate.check_password("tok_ben", c) == "ben-on-the-road"
    assert gate.check_password("tok_nothing", c) == ""


def test_an_expired_token_does_not_open_it():
    """The whole value of an expiry is that the day it passes, nobody has to
    remember anything."""
    archive(EXPIRED, DATED)
    c = cfg("")
    assert gate.check_password("tok_expired", c) == ""
    # And the one whose date is still ahead is untouched by its neighbour.
    assert gate.check_password("tok_dated", c) == "anna"


def test_a_disabled_token_does_not_open_it():
    """The soft revoke: the row stays, so the label and the date it was last
    used survive the decision - but it is no longer a way in."""
    archive(DISABLED)
    c = cfg("")
    assert gate.check_password("tok_disabled", c) == ""
    assert gate.in_force(c) is False


def test_the_query_asks_only_for_rows_that_are_live():
    """Read as SQL, because that clause is where "live" is actually decided -
    the fake at the top of this file only mirrors it. Section 5 of
    dashboard/sql/verify.sql runs the same rule against real rows."""
    clause = " ".join(gate.LIVE_TOKENS_SQL.split())
    assert "WHERE bool_enabled" in clause
    assert "(date_expires IS NULL OR date_expires > NOW())" in clause


def test_using_a_token_stamps_it_and_a_password_does_not(monkeypatch):
    """So an unused token can be told from a working one before somebody
    decides whether to withdraw it."""
    monkeypatch.setattr(gate.time, "sleep", lambda _s: None)
    fake = archive(LIVE)
    c = cfg("anna:one")

    assert gate.attempt("tok_live", "", c) == (True, "")
    assert fake.stamped == ["tok_live"]

    # A password from the environment has no row to stamp, and a refusal
    # stamps nothing at all.
    assert gate.attempt("one", "", c) == (True, "")
    gate.attempt("not-a-token", "", c)
    assert fake.stamped == ["tok_live"]


def test_a_right_token_behind_a_failed_bot_check_is_not_stamped(monkeypatch):
    """It did not open the gate, and a date_last_used saying otherwise would
    be read as though it had."""
    monkeypatch.setattr(gate.time, "sleep", lambda _s: None)
    monkeypatch.setattr(gate, "verify_turnstile", lambda *a, **k: (False, "refused"))
    fake = archive(LIVE)
    ok, _ = gate.attempt("tok_live", "a-token",
                         cfg("", turnstile_site_key="site", turnstile_secret="secret"))
    assert ok is False
    assert fake.stamped == []


# ── The two halves of the signing bargain ────────────────────

def test_adding_or_revoking_a_token_ends_nobody_else_s_session():
    """THE REASON THE TOKENS ARE NOT IN THE SIGNING KEY.

    An installation that hands one out on a Monday would otherwise log every
    other person out on that Monday, and the last thing anybody would connect
    that to is somebody else being let in."""
    fake = archive(LIVE)
    c = cfg("anna:one")
    cookie = gate.seal(c)

    fake.rows.append(row("tok_new", label="ben"))
    gate.reset_cache()
    assert gate.check_password("tok_new", c) == "ben"     # the new one works
    assert gate.unseal(cookie, c) is True                  # and nobody was logged out

    fake.rows = [r for r in fake.rows if r["text_token"] != "tok_new"]
    gate.reset_cache()
    assert gate.check_password("tok_new", c) == ""
    assert gate.unseal(cookie, c) is True


def test_changing_the_environment_passwords_still_ends_every_session():
    """The other half, and it survives the installation secret joining the
    key: withdrawing access is still one edit to one variable."""
    archive(LIVE)
    before = cfg("anna:one,ben:two")
    cookie = gate.seal(before)
    assert gate.unseal(cookie, before) is True
    assert gate.unseal(cookie, cfg("anna:one")) is False
    assert gate.unseal(cookie, cfg("anna:one,ben:CHANGED")) is False


def test_the_secret_is_read_once_and_kept():
    """It is part of the key, so a second reading that answered differently
    would log everybody out. Written down because the memo is what stops the
    signing key from touching the database on every request."""
    archive(LIVE)
    assert gate.installation_secret() == "the-installation-secret"
    db_module.set_db(None)                    # the archive goes away
    assert gate.installation_secret() == "the-installation-secret"


def test_without_an_archive_the_key_is_the_passwords_alone():
    """A dashboard whose schema has not been seeded yet still issues cookies
    that verify."""
    c = cfg("anna:one")
    assert gate.installation_secret() == ""
    assert gate.unseal(gate.seal(c), c) is True


# ── When the archive cannot be asked ─────────────────────────

def test_the_set_is_read_at_most_once_every_30_seconds(monkeypatch):
    """The gate runs in front of every request. One query a minute is the
    price of the second source; one query per request would not be."""
    clock = [1000.0]
    monkeypatch.setattr(gate.time, "monotonic", lambda: clock[0])
    fake = archive(LIVE)
    c = cfg("")
    for _ in range(5):
        assert gate.in_force(c) is True
    assert fake.reads == 1

    clock[0] += gate.TOKEN_MEMO_SECONDS + 1
    assert gate.in_force(c) is True
    assert fake.reads == 2


def test_a_failed_read_keeps_the_last_answer(monkeypatch):
    """An empty set would be an OPEN GATE on an installation whose only
    tokens are in the database, so "the archive hiccuped" must not be a way
    in. The worst this can do is leave a revoked token working a little
    longer, which is the safer half of the trade."""
    clock = [1000.0]
    monkeypatch.setattr(gate.time, "monotonic", lambda: clock[0])
    fake = archive(LIVE)
    c = cfg("")
    assert gate.check_password("tok_live", c) == "anna"

    fake.fails = True
    clock[0] += gate.TOKEN_MEMO_SECONDS + 1
    assert gate.in_force(c) is True
    assert gate.check_password("tok_live", c) == "anna"


def test_an_archive_that_was_never_reachable_leaves_the_gate_as_it_found_it():
    """With a password configured the gate stays shut; with none it stays
    open, which is what an installation without the schema had before."""
    archive(LIVE, fails=True)
    assert gate.in_force(cfg("anna:one")) is True
    assert gate.in_force(cfg("")) is False


def test_an_installation_with_a_password_never_asks_the_archive():
    """`in_force` is answered from the environment first, so the check in
    front of every request costs a dictionary lookup where a password is
    configured. Only the form itself, which has a string to compare against
    both sources, reads the tokens."""
    fake = archive(LIVE)
    assert gate.in_force(cfg("anna:one")) is True
    assert fake.reads == 0
    assert gate.check_password("one", cfg("anna:one")) == "anna"
    assert fake.reads == 1
