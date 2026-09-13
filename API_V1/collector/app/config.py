"""Configuration, read once at start-up.

Everything comes from the environment, so the only file a user edits is `.env`.
Anything missing that the collector cannot invent a sensible answer for stops
the process immediately with a message naming the variable - a collector that
starts up and then quietly does nothing is the worst outcome here.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from urllib.parse import quote


class ConfigError(RuntimeError):
    pass


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"{name} must be true or false, got {raw!r}")


def _int(name: str, default: int, minimum: int = 1) -> int:
    raw = os.environ.get(name, "").strip()
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

    Keys may be written as `label:key` so the logs and the monitoring table say
    something a human recognises. Without a label the key's own prefix is used -
    never the secret, which must not end up in a log line.
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
    poll_interval_minutes: int = 15
    key_stagger_seconds: int = 20
    request_timeout_seconds: int = 120
    log_level: str = "INFO"
    # How far back to look when deciding what has already been archived.
    # Results live about an hour on the platform, so a task older than the
    # window cannot be re-fetched whether or not we remember it.
    collected_lookback_hours: int = 2
    # Default false: jobs are fetched with retain=true, so reading them here
    # does NOT consume the one look the platform otherwise gives you, and the
    # content is cleaned up on the platform's own schedule. Turning this on
    # trades that safety net for immediacy - see load() for the warning.
    clear_content_on_fetch: bool = False
    languages: list[str] = field(default_factory=list)

    @property
    def retain_on_read(self) -> bool:
        return not self.clear_content_on_fetch


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

    user = os.environ.get("POSTGRES_USER", "xtracting").strip()
    password = os.environ.get("POSTGRES_PASSWORD", "").strip()
    host = os.environ.get("POSTGRES_HOST", "xtracting_db").strip()
    # POSTGRES_PORT is the port to connect to. The older name is still read so
    # an existing .env keeps working.
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


def load() -> Config:
    database_url = _database_url()

    raw_keys = os.environ.get("XTRACTING_API_KEYS", "").strip()
    if not raw_keys:
        raise ConfigError(
            "XTRACTING_API_KEYS is not set - put at least one key in .env, "
            "comma separated, optionally as label:key"
        )

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
            # A duplicated key would double every row it fetches.
            continue
        seen.add(secret)
        keys.append(ApiKeyEntry(label=label.strip() or secret.split(".", 1)[0], secret=secret))

    if not keys:
        raise ConfigError("XTRACTING_API_KEYS contained no usable keys")

    poll = _int("POLL_INTERVAL_MINUTES", 15)
    lookback = _int("COLLECTED_LOOKBACK_HOURS", 2)
    if lookback * 60 < poll * 2:
        # Not fatal - re-storing a task is idempotent - but it means every round
        # re-fetches work it already has, which is pure waste.
        logging.getLogger(__name__).warning(
            "COLLECTED_LOOKBACK_HOURS=%d is short for a %d minute poll interval; "
            "tasks will be fetched more than once. Two hours suits any interval "
            "up to an hour.", lookback, poll)

    clear = _bool("CLEAR_CONTENT_ON_FETCH", False)
    if clear:
        logging.getLogger(__name__).warning(
            "CLEAR_CONTENT_ON_FETCH is on. Results are deleted from the platform "
            "the moment they are read, BEFORE this collector has committed them. "
            "If the database is unreachable or the write fails at that instant, "
            "those extractions are gone and cannot be fetched again. Leave this "
            "off unless you have a specific reason.")

    return Config(
        database_url=database_url,
        api_url=os.environ.get("XTRACTING_API_URL", "https://api.xtracting.io").rstrip("/"),
        api_keys=keys,
        poll_interval_minutes=poll,
        key_stagger_seconds=_int("KEY_STAGGER_SECONDS", 20, minimum=0),
        request_timeout_seconds=_int("REQUEST_TIMEOUT_SECONDS", 120),
        collected_lookback_hours=lookback,
        clear_content_on_fetch=clear,
        log_level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    )
