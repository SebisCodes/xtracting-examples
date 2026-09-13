"""The password gate in front of every page.

One shared password in front of the whole application, checked before
anything else runs, with an optional Cloudflare Turnstile widget on the form
itself.

WHAT IT IS FOR. This dashboard reads a customer's archive. It is meant to be
run on an intranet, and it usually is - but "usually" is not a property you
can rely on, and an archive reachable from the internet with no gate in front
of it shows its project names, its hosts and its documents to anybody who
finds the address. The gate answers every request the same way whether or not
the address exists behind it.

IT IS NOT AN ACCOUNT SYSTEM, and it does not pretend to be. There are no
users here, no sessions per person, nothing to reset. It is one shared secret
that decides whether this installation is reachable at all - the same
distinction the other projects draw:

    the gate    shared, against the public
    an account  personal, against confusion between people

SEVERAL PASSWORDS, EACH WITH A LABEL. `DASHBOARD_GATE_PASSWORD` takes a
comma-separated list of `label:password` pairs, so a team can hand out one
each and withdraw one without changing everybody's:

    DASHBOARD_GATE_PASSWORD=anna:x7Kp...,ben:9Qm2...

The label is for the person writing the file and for the log line that says
which one was used. It is never shown to somebody who has not passed the
gate, because that would be a list of who has access.

A bare password with no label is accepted too, and gets the label "default" -
an installation with one password should not have to invent a name for it.

A SECOND SOURCE: TOKENS IN THE ARCHIVE. `dashboard.access_tokens` holds rows
that open the gate exactly as a password from the environment does, each with
a label, a switch and an expiry date. `database/manage_tokens.sh` writes them,
and a token is live while `bool_enabled` is true and `date_expires` is either
NULL or still ahead. Handing somebody access until March, and taking it away
again this afternoon, then costs no file on the server, no restart, and
nobody else's session.

EMPTY MEANS OFF - AND EMPTY MEANS BOTH SOURCES EMPTY. No password in the
environment and no live token in the archive: no gate at all. That is the
setting an intranet installation wants, and it has to be the default: a
gate that switched itself on by itself would lock a running installation
out of its own archive. Because a token can close a gate that no environment
variable mentions, main.py mounts the middleware whatever
DASHBOARD_GATE_PASSWORD says and asks `in_force()` per request - the older
shape, which skipped the middleware entirely when that variable was empty,
would have left a token-only installation wide open.

THE COOKIE IS SIGNED WITH THE ENVIRONMENT SET AND AN INSTALLATION SECRET,
and deliberately NOT with the tokens. Changing the password list still ends
every session, which is what makes withdrawing access one edit rather than a
hunt for a session store. A token must not do the same: an installation that
hands one out on a Monday would log every other person out on that Monday,
and the last thing anybody would connect that to is somebody else being let
in. The secret is generated once into `dashboard.settings` under
`gate.signing_secret`, so the two sources stay independent of each other.
Upgrading to this version invalidates the cookies the version before it
issued, which costs everybody one re-entry of the password, once.

THE ARCHIVE IS ASKED ONCE EVERY 30 SECONDS, not once per request, and if it
cannot be asked the last answer stands. Replacing the set with an empty one
on a failed read would be a gate that opens itself because the database
hiccuped; keeping the last known set means the worst a broken read can do is
leave a revoked token working for another half minute.

TURNSTILE IS OFF ONLY WHEN BOTH KEYS ARE EMPTY. Set either one and the check
must pass - not "both keys before it counts", which is the rule that turns a
typo into an open gate: delete the site key by accident and a dashboard
protected by a bot check quietly becomes a password box anybody may hammer,
with nothing on the page to say so.

IT FAILS CLOSED IN EVERY DIRECTION. A token Cloudflare will not vouch for is
refused. A token it cannot be asked about - network down, timeout,
unreadable reply - is refused, because a check that passes when the network
breaks can be defeated by breaking the network. And half a configuration -
one key set, the other empty - refuses everybody, with the missing half named
in the log. A gate nobody can pass is loud and recoverable; a gate everybody
can pass is silent.

With BOTH keys empty the gate runs on the password alone and says so in the
log at start. That is deliberate: the keys are issued per domain by
Cloudflare, so an installation on an intranet address cannot have them, and a
dashboard that refused to start without them would be unreachable for ever.

THE COOKIE SAYS NOTHING. Its name is readable in any browser console before
the gate has been passed; called `xtracting_gate` it would answer the first
question a stranger has - what is this - before they had seen the form.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import logging
import secrets
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

#: Deliberately uninformative; see the header.
COOKIE_NAME = "access"
VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"

#: How long a reading of dashboard.access_tokens is trusted for. Long enough
#: that the middleware in front of every request costs one query a minute
#: rather than one per request; short enough that "revoke it now" means this
#: minute and not the next restart.
TOKEN_MEMO_SECONDS = 30.0
#: And how long to wait before trying again after a read that failed, so a
#: database that is down is asked twice a minute rather than by every request
#: that arrives while it is.
RETRY_SECONDS = 30.0

#: The row in dashboard.settings that keeps this installation's half of the
#: cookie signing key. Generated once, never shown, and the reason a token
#: can be added without ending everybody else's session; see the header.
SECRET_SETTING = "gate.signing_secret"

#: Live means: switched on, and either no expiry or one that has not arrived.
LIVE_TOKENS_SQL = """
    SELECT text_token, text_label
    FROM dashboard.access_tokens
    WHERE bool_enabled
      AND (date_expires IS NULL OR date_expires > NOW())
"""

#: What stays reachable without passing. `/gate` is the form itself, `/static`
#: is the stylesheet that form is drawn with, and `/healthz` is what Docker
#: asks - without it the container is permanently unhealthy behind its own
#: gate. Nothing else: the default is closed, and the exceptions are in one
#: place rather than one decorator per route somebody can forget.
#:
#: TWO EXACT NAMES AND ONE PREFIX, kept apart on purpose. Matched by prefix,
#: "/gate" also opens "/gateway" and "/healthz" opens "/healthzz" - which is
#: how a three-item allowlist becomes a hole somebody can walk through by
#: adding a letter.
OPEN_EXACT = ("/gate", "/healthz")
OPEN_PREFIX = ("/static/",)


def parse_tokens(raw: str) -> dict[str, str]:
    """`label:password,label:password` into {label: password}.

    A bare password with no colon is kept under the label "default". A label
    with an empty password is dropped: an entry that lets anybody in with an
    empty box is the one mistake this must not silently accept.

    THE PASSWORD MAY CONTAIN A COLON - only the FIRST one separates, so
    `anna:a:b:c` is anna with the password `a:b:c`. It may not contain a
    comma, which is what separates the entries; see the note in .env.example
    about the characters an env file cannot carry.

    >>> parse_tokens("anna:one,ben:two")
    {'anna': 'one', 'ben': 'two'}
    >>> parse_tokens("just-one-password")
    {'default': 'just-one-password'}
    >>> parse_tokens("anna:a:b:c")
    {'anna': 'a:b:c'}
    >>> parse_tokens("  anna : one , ben:two ")
    {'anna': 'one', 'ben': 'two'}
    >>> parse_tokens("anna:,ben:two")
    {'ben': 'two'}
    >>> parse_tokens("")
    {}
    """
    out: dict[str, str] = {}
    for part in (raw or "").split(","):
        entry = part.strip()
        if not entry:
            continue
        label, sep, password = entry.partition(":")
        if not sep:
            label, password = "default", entry
        label, password = label.strip(), password.strip()
        if not password:
            log.warning("gate: the entry %r has no password and is ignored", label or entry)
            continue
        if label in out:
            log.warning("gate: the label %r appears twice; the later one wins", label)
        out[label or "default"] = password
    return out


# ── The second source: tokens kept in the archive ─────────────
#
# THE DATABASE IS REACHED LAZILY, INSIDE THE FUNCTIONS, AND NEVER AT IMPORT.
# main.py's `_mount_gate` runs while the application is being built, which is
# before the lifespan opens the connection pool, so a `from .db import get_db`
# at the top of this file would either fail there or hand out a pool that does
# not exist yet. app/colours.py's `get_resolver` solves the same problem the
# same way: import where it is used, and remember the answer.
#
# Nothing here raises. The gate runs in front of every request, and a token
# table that cannot be read is a reason to fall back on what was last known -
# not a reason to answer 500 on the home page.

_tokens: dict[str, str] = {}
_tokens_read_at = 0.0
_tokens_lock = threading.Lock()
#: The last thing that went wrong, so a database that is down says so once
#: rather than twice a minute for as long as it is down.
_last_complaint = ""

_secret = ""
_secret_read_at = 0.0
_secret_lock = threading.Lock()


def database_tokens() -> dict[str, str]:
    """{token: label} for every live row, read at most every 30 seconds.

    A failed read keeps the previous answer rather than emptying it: an empty
    one is an OPEN GATE on an installation whose only tokens are in the
    database, and "the archive was briefly unreachable" must not be a way in.
    """
    global _tokens, _tokens_read_at, _last_complaint
    with _tokens_lock:
        now = time.monotonic()
        if _tokens_read_at and now - _tokens_read_at < TOKEN_MEMO_SECONDS:
            return _tokens
        _tokens_read_at = now
        try:
            _tokens = _load_database_tokens()
            _last_complaint = ""
        except Exception as exc:  # noqa: BLE001 - see the note above
            _complain(exc)
        return _tokens


def _load_database_tokens() -> dict[str, str]:
    from .db import get_db
    with get_db().read() as conn:
        rows = conn.execute(LIVE_TOKENS_SQL).fetchall()
    return {str(row["text_token"]): str(row["text_label"]) for row in rows}


def _complain(exc: Exception) -> None:
    """One line per DISTINCT failure, because this runs on a timer.

    42P01 is `undefined_table`, and it means something specific and fixable:
    the archive has no dashboard.access_tokens table. Said in those words
    rather than as a psycopg traceback, because the answer is one file and
    the message is the only place anybody will see it named.
    """
    global _last_complaint
    if getattr(exc, "sqlstate", "") == "42P01":
        message = ("gate: dashboard.access_tokens does not exist, so no token can "
                   "open the gate. Run dashboard/sql/01-dashboard-schema.sql "
                   "against the archive, or let the dashboard seed it "
                   "(DASHBOARD_SEED=true).")
    else:
        message = f"gate: the tokens in the archive could not be read ({exc})"
    if message != _last_complaint:
        _last_complaint = message
        log.warning("%s The last set read stands until it can be read again.", message)


def reset_cache() -> None:
    """Forget both memos - for tests, and for a caller that has just written a
    token and wants the next request to see it."""
    global _tokens, _tokens_read_at, _last_complaint, _secret, _secret_read_at
    with _tokens_lock:
        _tokens, _tokens_read_at, _last_complaint = {}, 0.0, ""
    with _secret_lock:
        _secret, _secret_read_at = "", 0.0


def installation_secret() -> str:
    """This installation's half of the cookie signing key, made once.

    Empty when the archive cannot be asked: the key is then the passwords
    alone, cookies keep working, and
    the only cost is that they are invalidated once more when the row does
    arrive. Once a secret has been read it is kept for the life of the
    process - a database that goes away mid-afternoon must not silently
    change the key and log everybody out.
    """
    global _secret, _secret_read_at
    with _secret_lock:
        if _secret:
            return _secret
        now = time.monotonic()
        if _secret_read_at and now - _secret_read_at < RETRY_SECONDS:
            return ""
        _secret_read_at = now
        try:
            _secret = _load_installation_secret()
        except Exception as exc:  # noqa: BLE001 - see the note above
            log.warning("gate: %s could not be read or written (%s); the cookie is "
                        "signed with the passwords alone until it can be",
                        SECRET_SETTING, exc)
        return _secret


def _load_installation_secret() -> str:
    """Read it, or write one if this is the first time anybody asked.

    ONE STATEMENT, because two workers start at the same moment and would
    otherwise each generate a secret and each believe its own. `DO UPDATE SET`
    on the key itself looks pointless and is not: `DO NOTHING` returns no row
    when it conflicts, so the second worker would learn nothing, while this
    form returns the value that is actually stored - the first worker's.
    """
    from .db import get_db
    with get_db().write() as conn:
        row = conn.execute(
            "INSERT INTO dashboard.settings (text_key, text_value) VALUES (%s, %s) "
            "ON CONFLICT (text_key) DO UPDATE SET text_key = EXCLUDED.text_key "
            "RETURNING text_value",
            (SECRET_SETTING, secrets.token_hex(32))).fetchone()
    return str(row["text_value"]) if row else ""


def _stamp_used(token: str) -> None:
    """Record that this token opened the gate, and never fail because of it.

    An unused token can then be told from a working one before somebody
    decides whether to withdraw it. A write that does not go through is worth
    a log line and nothing more: refusing a right token because a column
    could not be updated would be the wrong trade by a wide margin.
    """
    try:
        from .db import get_db
        with get_db().write() as conn:
            conn.execute("UPDATE dashboard.access_tokens SET date_last_used = NOW() "
                         "WHERE text_token = %s", (token,))
    except Exception as exc:  # noqa: BLE001 - see the note above
        log.warning("gate: the token was accepted, but date_last_used could not "
                    "be stamped (%s)", exc)


@dataclass(frozen=True)
class GateConfig:
    #: {label: password}, from DASHBOARD_GATE_PASSWORD. This is the
    #: ENVIRONMENT half only - the archive holds the other half, and
    #: `in_force()` is the question the middleware actually asks.
    tokens: dict[str, str] = field(default_factory=dict)
    turnstile_site_key: str = ""
    turnstile_secret: str = ""
    #: How long one passing lasts. A day by default: long enough not to be a
    #: nuisance on a screen somebody keeps open, short enough that a borrowed
    #: laptop is not open for a month.
    max_age_seconds: int = 24 * 60 * 60

    @property
    def enabled(self) -> bool:
        """Is a password configured in the environment?

        NOT "is the gate on": a live row in dashboard.access_tokens closes a
        gate that no environment variable mentions. `in_force()` is the whole
        answer; this property is the half that can be known without asking
        the archive, and it is what the start-up log reports.
        """
        return bool(self.tokens)

    @property
    def turnstile_required(self) -> bool:
        """EITHER key means the check must pass. Only both empty switches it off.

        The other way round - requiring both before the check counts - is a
        misconfiguration that opens the gate. Delete the site key by accident,
        or mistype the variable name, and a dashboard that was protected by a
        bot check silently goes back to a password box anybody may hammer,
        with nothing on the page or in the log to say so.

        So the presence of ANY Turnstile setting is read as "this
        installation wants the check", and from then on the check has to pass.
        The failure mode of a half-configured gate is then a gate nobody can
        pass, which is loud, recoverable and safe - rather than a gate
        everybody can pass, which is silent.
        """
        return bool(self.turnstile_site_key or self.turnstile_secret)

    @property
    def turnstile_complete(self) -> bool:
        """Both halves present, so the check CAN be made: a site key with no
        secret draws a widget nobody can verify, and a secret with no site
        key demands a token nothing produces."""
        return bool(self.turnstile_site_key and self.turnstile_secret)

    @property
    def turnstile_broken(self) -> bool:
        """Wanted but not usable - one key set and the other missing. Nobody
        gets in until it is fixed, and the log says which half is missing."""
        return self.turnstile_required and not self.turnstile_complete

    @property
    def signing_key(self) -> str:
        """What the cookie is signed with: the passwords, and a secret.

        THE PASSWORDS, so that changing or removing one stops every cookie
        issued under the old set from verifying. Withdrawing access that way
        is a matter of editing one line, not of hunting for a session store.

        AND AN INSTALLATION SECRET, so that the key is not derived from the
        passwords ALONE - which is what lets the tokens in the archive stay
        out of it. If they were in it, adding a token would log every other
        person out, and revoking one would too; with the secret carrying the
        rest of the key, a token comes and goes without touching anybody
        else's session. There is still nothing to configure: the secret is
        generated the first time it is wanted and kept in dashboard.settings.

        An archive that cannot be asked yields an empty secret, and the key
        falls back to the passwords - the behaviour of the version before
        this one, so a cookie is never signed with a key that changes twice.
        """
        joined = "\x00".join(f"{k}\x01{v}" for k, v in sorted(self.tokens.items()))
        return hashlib.sha256(
            f"{joined}\x02{installation_secret()}".encode("utf-8")).hexdigest()


def match_password(given: str, cfg: GateConfig) -> tuple[str, str]:
    """The label this opens the gate under, and the database token it was.

    BOTH SOURCES, and EVERY entry in each of them is compared - no early
    break - with `compare_digest`, so neither the answer time nor the
    position in the list says anything about which one matched or how much of
    it was right. The two sources are searched the same way for the same
    reason: an environment password and a token are equally good ways in, and
    the gate must not answer one of them faster than the other.

    The second half of the answer is the token STRING when it came from the
    archive and "" when it came from the environment, which is how `attempt`
    knows whether there is a `date_last_used` to stamp.

    >>> match_password("two", GateConfig(tokens={"anna": "one", "ben": "two"}))
    ('ben', '')
    """
    label, token = "", ""
    candidate = (given or "").strip()
    if not candidate:
        return "", ""
    for entry_label, password in cfg.tokens.items():
        if _same(candidate, password):
            label = label or entry_label
    for entry_token, entry_label in database_tokens().items():
        if _same(candidate, entry_token):
            label = label or entry_label
            token = token or entry_token
    return label, token


def check_password(given: str, cfg: GateConfig) -> str:
    """The label whose password this is, or "" for none.

    >>> cfg = GateConfig(tokens={"anna": "one", "ben": "two"})
    >>> check_password("two", cfg)
    'ben'
    >>> check_password("three", cfg)
    ''
    >>> check_password("", cfg)
    ''
    """
    return match_password(given, cfg)[0]


def _same(a: str, b: str) -> bool:
    """Constant-time comparison that survives a non-ASCII password.

    `hmac.compare_digest` REFUSES a str with a character above U+007F -
    "comparing strings with non-ASCII characters is not supported" - so a
    password with an umlaut or a ç in it raised TypeError and answered the
    gate with a 500 instead of "that is not the password". Comparing the
    sha256 digests fixes that and hides the length as well, which comparing
    the encoded bytes would still have leaked.
    """
    return hmac.compare_digest(
        hashlib.sha256(a.encode("utf-8")).digest(),
        hashlib.sha256(b.encode("utf-8")).digest())


def _sign(payload: bytes, key: str) -> str:
    return hmac.new(key.encode("utf-8"), payload, hashlib.sha256).hexdigest()


def seal(cfg: GateConfig) -> str:
    """Proof that the gate was passed: a timestamp and a signature.

    Nothing else is in it. The gate is shared rather than personal, so there
    is nobody to name - and a cookie that carried the label would put the
    list of who has access into the browser of anybody who got in.
    """
    stamp = str(int(time.time()))
    return f"{stamp}.{_sign(stamp.encode('utf-8'), cfg.signing_key)}"


def unseal(cookie: str, cfg: GateConfig) -> bool:
    """>>> cfg = GateConfig(tokens={"a": "b"})
    >>> unseal(seal(cfg), cfg)
    True
    >>> unseal("nonsense", cfg)
    False
    >>> unseal(seal(cfg), GateConfig(tokens={"a": "changed"}))
    False
    """
    if not cookie or "." not in cookie:
        return False
    stamp, _, signature = cookie.rpartition(".")
    if not hmac.compare_digest(_sign(stamp.encode("utf-8"), cfg.signing_key), signature):
        return False
    try:
        age = time.time() - float(stamp)
    except ValueError:
        return False
    return 0 <= age < cfg.max_age_seconds


def in_force(cfg: GateConfig) -> bool:
    """Is there anything at all that could open this gate?

    Either source is enough, and the environment is asked first on purpose:
    an installation with a password configured never touches the archive on
    this path, so the check in front of every request costs one dictionary
    lookup. Only an installation with no password configured asks whether a
    token exists - once every 30 seconds, and see `database_tokens`.
    """
    return bool(cfg.tokens) or bool(database_tokens())


def passed(cookie_value: str, cfg: GateConfig) -> bool:
    """May this request go through at all?"""
    if not in_force(cfg):
        return True
    return unseal(cookie_value, cfg)


def is_open_path(path: str) -> bool:
    """Exactly the three, and nothing that merely begins like one.

    >>> [is_open_path(p) for p in ("/gate", "/healthz", "/static/css/app.css")]
    [True, True, True]
    >>> [is_open_path(p) for p in ("/", "/gateway", "/healthzz", "/staticfiles/x")]
    [False, False, False, False]
    """
    return path in OPEN_EXACT or path.startswith(OPEN_PREFIX)


def public_address(remote_ip: str) -> str:
    """The address to tell Cloudflare the visitor came from - or "" when the
    one this process saw cannot be the one the widget was solved from.

    `remoteip` is optional on siteverify, and it is CHECKED when it is sent:
    a token solved from one address and verified with another comes back as
    invalid-input-response, the same answer as a forged token. That is right
    on a server that sees real addresses and wrong behind any NAT that
    rewrites them - a router that loops a LAN client back in through the
    public name hands this process its own private address, and every
    reader on that LAN was refused with the correct password and a solved
    widget. The private ranges, loopback, link-local and anything that is
    not an address at all are therefore not sent; Cloudflare then judges the
    token on its own, which is what it does for everybody who leaves the
    field out.

    >>> public_address("93.184.216.34")
    '93.184.216.34'
    >>> public_address("192.168.50.1")
    ''
    >>> public_address("127.0.0.1")
    ''
    >>> public_address("not an address")
    ''
    >>> public_address("")
    ''
    """
    try:
        address = ipaddress.ip_address((remote_ip or "").strip())
    except ValueError:
        return ""
    if not address.is_global:
        return ""
    return str(address)


def verify_turnstile(secret: str, token: str, remote_ip: str = "") -> tuple[bool, str]:
    """Ask Cloudflare whether a token is genuine.

    FAILS CLOSED. A network error, an unreadable reply or a timeout is a NO,
    not a yes: a gate that opens when Cloudflare is unreachable can be opened
    by making Cloudflare unreachable, and somebody working through passwords
    is in a good position to arrange that.
    """
    if not token:
        return False, "no token was sent"

    data = {"secret": secret, "response": token}
    remote_ip = public_address(remote_ip)
    if remote_ip:
        data["remoteip"] = remote_ip
    try:
        request = urllib.request.Request(
            VERIFY_URL, data=urllib.parse.urlencode(data).encode("utf-8"), method="POST")
        request.add_header("Content-Type", "application/x-www-form-urlencoded")
        with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
            body = json.loads(response.read())
    except Exception as exc:  # noqa: BLE001
        log.warning("gate: the Turnstile check could not be made: %s", exc)
        return False, "the check could not be made"

    if body.get("success"):
        return True, ""
    codes = ", ".join(body.get("error-codes") or []) or "unknown"
    log.info("gate: Turnstile refused the token (%s)", codes)
    return False, f"the check was refused ({codes})"


def attempt(password: str, bot_token: str, cfg: GateConfig, remote_ip: str = "") -> tuple[bool, str]:
    """Both checks, ALWAYS BOTH, and one message for either failing.

    Running the second only when the first passed would make the answer time
    say whether the password was right, and two different messages would say
    it outright. The one exception is the bot check being unconfigured, which
    is not a failure at all.

    TWO THINGS CALLED "TOKEN" MEET HERE, AND THEY ARE NOT THE SAME THING.
    `bot_token` is what the Turnstile widget put into the form - the proof
    that a person sat in front of it. `archive_token` is the row from
    dashboard.access_tokens that the password matched, if it was one of
    those - the thing that gets a date_last_used stamp. As one variable the
    second assignment would overwrite the first: Cloudflare asked about ""
    whenever the password comes from the environment ("no token was sent")
    and about the ARCHIVE TOKEN whenever it comes from the database
    ("invalid-input-response") - a gate no key pair could ever open.
    """
    label, archive_token = match_password(password, cfg)
    bot_ok, reason = True, ""
    if cfg.turnstile_broken:
        # Half a bot check is not half a gate, it is no check at all - so
        # this refuses everybody rather than falling back to the password.
        # The log names the missing half; the page does not, because a
        # stranger learning that this installation is misconfigured is a
        # stranger learning something.
        missing = ("DASHBOARD_TURNSTILE_SECRET" if cfg.turnstile_site_key
                   else "DASHBOARD_TURNSTILE_SITE_KEY")
        log.error("gate: the Cloudflare check is configured but %s is empty, so "
                  "nobody can pass. Set it, or clear both Turnstile variables "
                  "to run on the password alone.", missing)
        bot_ok, reason = False, "the check is not configured"
    elif cfg.turnstile_required:
        bot_ok, reason = verify_turnstile(cfg.turnstile_secret, bot_token, remote_ip)

    if label and bot_ok:
        # Stamped only when the gate actually opened. A right token behind a
        # failed bot check was not used to get in, and a date_last_used that
        # said otherwise would be read as one.
        if archive_token:
            _stamp_used(archive_token)
        log.info("gate: passed (%s%s)", label,
                 ", a token from the archive" if archive_token else "")
        return True, ""
    if not bot_ok and reason:
        log.info("gate: the bot check said no (%s)", reason)
    # A pause, not as protection - it is far too little for that - but so an
    # automated run cannot manage hundreds of tries a second and bury the log.
    time.sleep(1.0 + secrets.randbelow(500) / 1000)
    log.info("gate: refused")
    return False, "That is not the password."
