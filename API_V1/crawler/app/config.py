"""Configuration, read once at start-up.

Everything comes from the environment, so the only file a user edits is `.env`.
Anything missing that the crawler cannot invent a sensible answer for stops the
process immediately with a message naming the variable - a crawler that starts
up and then quietly crawls nothing is the worst outcome here.

The key parsing is `collector/app/config.py`'s, word for word, and that is
deliberate: a label written once in `.env` has to mean the same thing in both
services, because it is what binds a source to the project its documents end
up in. Two parsers would eventually disagree about a space or a `=` in a
secret, and the symptom would be documents in the wrong project.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from urllib.parse import quote


class ConfigError(RuntimeError):
    pass


log = logging.getLogger(__name__)

def _env(name: str) -> str:
    """The raw value of one variable, or "" when it is not set."""
    return os.environ.get(name, "")


def _bool(name: str, default: bool) -> bool:
    raw = _env(name).strip().lower()
    if not raw:
        return default
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"{name} must be true or false, got {raw!r}")


def _int(name: str, default: int, minimum: int = 1) -> int:
    raw = _env(name).strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a whole number, got {raw!r}") from exc
    if value < minimum:
        raise ConfigError(f"{name} must be at least {minimum}, got {value}")
    return value


@dataclass(frozen=True)
class ApiKeyEntry:
    """One key, and the label it is reported under.

    Keys may be written as `label:key` so that the logs and the source rows say
    something a human recognises. Without a label the key's own prefix is used -
    never the secret, which must not end up in a log line.

    The label is more than cosmetics here: `scraper_config.sources.text_key_label`
    points at it, one key belongs to one Xtracting project, and that is how a
    source knows which archive its documents land in.
    """

    label: str
    secret: str

    @property
    def prefix(self) -> str:
        return self.secret.split(".", 1)[0]


@dataclass(frozen=True)
class Config:
    database_url: str
    api_url: str
    api_keys: list[ApiKeyEntry]
    user_agent: str
    concurrency: int = 2
    tick_seconds: int = 5
    lease_minutes: int = 30
    file_max_mb: int = 25
    submit_interval_seconds: int = 60
    max_batch_tasks: int = 1000
    max_request_bytes: int = 4_500_000
    max_content_chars: int = 2_000_000
    max_attempts: int = 3
    reconcile_minutes: int = 15
    #: How often the keys are asked what they are. Five minutes because the
    #: answer changes when a person edits a key in Xtracting's dashboard, and
    #: they are usually watching this one to see it take effect.
    key_refresh_seconds: int = 300
    test_timeout_seconds: int = 120
    request_timeout_seconds: int = 120
    only_tests: bool = False
    log_level: str = "INFO"

    def key(self, label: str) -> ApiKeyEntry | None:
        """The key a source's label points at, or None.

        None is not an error the crawler can fix: it is a source configured
        against a key this container does not have. It is recorded on the
        target (`text_key_status`) and shown in the dashboard, and the source's
        documents wait in the queue until the key appears.
        """
        for entry in self.api_keys:
            if entry.label == label:
                return entry
        return None

    @property
    def labels(self) -> list[str]:
        return [entry.label for entry in self.api_keys]


def _database_url() -> str:
    """DATABASE_URL if given, otherwise assembled from the parts.

    Assembled here rather than interpolated in the compose file for two
    reasons. Nested ${...} substitution is not portable - podman-compose 1.0.6
    leaves it half-expanded and the container then tries to log in as a literal
    "${POSTGRES_USER". And a password containing @ : / or # breaks a
    hand-built URL silently; quote() handles it.
    """
    explicit = os.environ.get("DATABASE_URL", "").strip()
    if explicit:
        return explicit

    user = (os.environ.get("SCRAPER_DB_USER", "").strip()
            or os.environ.get("POSTGRES_USER", "xtracting").strip())
    password = (os.environ.get("SCRAPER_DB_PASSWORD", "").strip()
                or os.environ.get("POSTGRES_PASSWORD", "").strip())
    host = os.environ.get("POSTGRES_HOST", "xtracting_db").strip()
    port = (os.environ.get("POSTGRES_PORT", "").strip()
            or os.environ.get("POSTGRES_PORT_INTERNAL", "").strip()
            or "5432")
    name = os.environ.get("POSTGRES_DB", "xtracting_archive").strip()
    if not password:
        raise ConfigError(
            "no database configured - set POSTGRES_PASSWORD (and the other "
            "POSTGRES_* variables if they differ from the defaults), or set "
            "DATABASE_URL to a full connection string"
        )
    return (f"postgresql://{quote(user, safe='')}:{quote(password, safe='')}"
            f"@{host}:{port}/{quote(name, safe='')}")


def parse_keys(raw_keys: str) -> list[ApiKeyEntry]:
    """`label:key, label:key` into entries. Duplicates are dropped.

    >>> [(entry.label, entry.secret) for entry in parse_keys("flats:ab12.xxx, ef34.yyy")]
    [('flats', 'ab12.xxx'), ('ef34', 'ef34.yyy')]

    A key repeated under two labels would double every document submitted with
    it, so the second one is ignored:

    >>> len(parse_keys("a:k1.x, b:k1.x"))
    1
    """
    keys: list[ApiKeyEntry] = []
    seen: set[str] = set()
    for chunk in raw_keys.replace("\n", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        label, _, secret = chunk.partition(":")
        if not secret:
            label, secret = "", chunk
        secret = secret.strip()
        if secret in seen:
            continue
        seen.add(secret)
        keys.append(ApiKeyEntry(label=label.strip() or secret.split(".", 1)[0],
                                secret=secret))
    return keys


def default_user_agent() -> str:
    from crawlkit.crawl import DEFAULT_USER_AGENT
    return DEFAULT_USER_AGENT


def load() -> Config:
    database_url = _database_url()

    # Two variables, one list. XTRACTING_PROJECT_KEYS exists so a person can
    # keep "the keys that submit" and "the key that lets the dashboard show what
    # the others cost" visibly apart in their .env. It buys nothing else:
    # every key from both is probed the same way and judged by what it answers,
    # so a key in the wrong variable still works. Placement must not be a thing
    # anybody has to get right, or the first symptom of getting it wrong is a
    # watchlist that silently collects nothing.
    raw_keys = os.environ.get("XTRACTING_API_KEYS", "").strip()
    raw_project_keys = os.environ.get("XTRACTING_PROJECT_KEYS", "").strip()
    if not raw_keys and not raw_project_keys:
        raise ConfigError(
            "XTRACTING_API_KEYS is not set - put at least one key in .env, "
            "comma separated, optionally as label:key. A key names the project "
            "its documents land in, and the dashboard asks for that project "
            "before a watchlist can be made."
        )
    # Order matters and this is where it is set: the default key of a project is
    # the first live extraction key of that project in this list. Submitting
    # keys first, because a project-read key usually may not extract and would
    # otherwise sit at the head of the list doing nothing.
    keys = parse_keys(", ".join(x for x in (raw_keys, raw_project_keys) if x))
    if not keys:
        raise ConfigError(
            "XTRACTING_API_KEYS and XTRACTING_PROJECT_KEYS contained no usable keys")

    user_agent = _env("CRAWLER_USER_AGENT").strip() or default_user_agent()
    if "http" not in user_agent and "@" not in user_agent:
        # Not fatal, but a crawler nobody can contact gets blocked instead of
        # asked to slow down.
        logging.getLogger(__name__).warning(
            "CRAWLER_USER_AGENT names no address anyone could reach you at. A "
            "site's operator who wants you to stop will block you instead of "
            "writing to you.")

    max_request_bytes = _int("CRAWLER_MAX_REQUEST_BYTES", 4_500_000, minimum=1000)
    if max_request_bytes >= 5_000_000:
        raise ConfigError(
            "CRAWLER_MAX_REQUEST_BYTES must stay below 5 MB - that is the "
            "limit of the gateway in front of the API, and a batch above it is "
            "refused with an HTML error page rather than a JSON one")

    return Config(
        database_url=database_url,
        api_url=os.environ.get("XTRACTING_API_URL",
                               "https://api.xtracting.io").rstrip("/"),
        api_keys=keys,
        user_agent=user_agent,
        concurrency=_int("CRAWLER_CONCURRENCY", 2),
        tick_seconds=_int("CRAWLER_TICK_SECONDS", 5),
        lease_minutes=_int("CRAWLER_LEASE_MINUTES", 30),
        file_max_mb=_int("CRAWLER_FILE_MAX_MB", 25),
        submit_interval_seconds=_int("CRAWLER_SUBMIT_INTERVAL_SECONDS", 60),
        max_batch_tasks=min(_int("CRAWLER_MAX_BATCH_TASKS", 1000), 1000),
        max_request_bytes=max_request_bytes,
        max_content_chars=min(_int("CRAWLER_MAX_CONTENT_CHARS", 2_000_000),
                              2_000_000),
        max_attempts=_int("CRAWLER_MAX_ATTEMPTS", 3),
        reconcile_minutes=_int("CRAWLER_RECONCILE_MINUTES", 15),
        key_refresh_seconds=_int("CRAWLER_KEY_REFRESH_SECONDS", 300, minimum=30),
        test_timeout_seconds=_int("CRAWLER_TEST_TIMEOUT_SECONDS", 120),
        request_timeout_seconds=_int("REQUEST_TIMEOUT_SECONDS", 120),
        only_tests=_bool("CRAWLER_ONLY_TESTS", False),
        log_level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    )
