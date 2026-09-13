"""Configuration, read once at start-up.

Everything comes from the environment, so the only file a user edits is `.env`.
Anything missing that the dashboard cannot invent a sensible answer for stops
the process immediately with a message naming the variable - a dashboard that
starts up and then shows an empty page with no explanation is the worst
outcome here.

The database part is the collector's, line for line: the two services read the
same .env variables, and a password that works in one folder must work in the
other.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
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
class Config:
    database_url: str
    # Where uvicorn listens inside the container. 8088 rather than the usual
    # 8080, which on many developer machines is already taken by something.
    port: int = 8088
    # Create and seed the `dashboard` schema on start. Off for databases where
    # the application user may not create anything - see schema.py.
    seed: bool = True
    tile_url: str = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
    #: Where Xtracting itself is. This archive is one half of a pair - the
    #: extraction runs there and the results land here - and several answers
    #: on these pages are only actionable over there: a project's settings,
    #: the keys that may extract. A customer on their own installation is not
    #: on xtracting.io, so it is a setting; the default is the public one, so
    #: a dashboard nobody has configured still links somewhere true.
    xtracting_url: str = "https://xtracting.io"

    #: The password gate in front of every page (app/gate.py). Empty means no
    #: gate at all, which is what an intranet installation wants and has to
    #: be the default: a gate that switched itself on at upgrade time would
    #: lock a running installation out of its own archive.
    #:
    #: `label:password,label:password`, so a team can hand out one each and
    #: withdraw one without changing everybody's.
    gate_password: str = ""
    #: Cloudflare Turnstile on the gate form. Both or neither: a site key
    #: with no secret draws a widget nobody checks, and a secret with no site
    #: key demands a token nothing produces.
    turnstile_site_key: str = ""
    turnstile_secret: str = ""
    #: How long one passing of the gate lasts.
    gate_max_age_hours: int = 24
    places_refresh_minutes: int = 15
    statement_timeout_seconds: int = 300
    #: How many entities one connection search may draw. 0 means no limit,
    #: which is the default: a map that quietly shows part of a network is
    #: worse than a slow one, and what actually bounds a runaway is the per-hop
    #: edge cap in routers/api_map.py (MAX_EDGE_ROWS, 5000). Set a number here
    #: if an archive turns out to have a hub that makes the browser crawl.
    map_max_entities: int = 0
    graph_max_neighbours: int = 120
    log_level: str = "INFO"
    # Test crawls from the Watched pages editor may reach private networks. Off by
    # default: the dashboard has no user accounts, and an open port that fetches any
    # address it is given would otherwise be a way into the rest of the network.
    allow_private_hosts: bool = False
    test_timeout_seconds: int = 120


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
    tile_url = os.environ.get("DASHBOARD_TILE_URL", "").strip() or Config.tile_url
    if "{z}" not in tile_url or "{x}" not in tile_url or "{y}" not in tile_url:
        # A tile URL without the placeholders would load the same image for
        # every tile, which looks like a broken map rather than a wrong setting.
        raise ConfigError(
            "DASHBOARD_TILE_URL must contain {z}, {x} and {y}, got " + repr(tile_url))

    return Config(
        database_url=_database_url(),
        port=_int("DASHBOARD_PORT", 8088),
        seed=_bool("DASHBOARD_SEED", True),
        tile_url=tile_url,
        places_refresh_minutes=_int("DASHBOARD_PLACES_REFRESH_MINUTES", 15),
        statement_timeout_seconds=_int("DASHBOARD_STATEMENT_TIMEOUT_SECONDS", 300),
        xtracting_url=(os.environ.get("DASHBOARD_XTRACTING_URL", "").strip().rstrip("/")
                       or "https://xtracting.io"),
        # NOT .strip()ped as a whole: a password may legitimately begin or
        # end with a space, and gate.parse_tokens trims each label and
        # password itself. Only the surrounding newline goes.
        gate_password=os.environ.get("DASHBOARD_GATE_PASSWORD", "").strip("\r\n"),
        turnstile_site_key=os.environ.get("DASHBOARD_TURNSTILE_SITE_KEY", "").strip(),
        turnstile_secret=os.environ.get("DASHBOARD_TURNSTILE_SECRET", "").strip(),
        gate_max_age_hours=_int("DASHBOARD_GATE_MAX_AGE_HOURS", 24, minimum=1),
        map_max_entities=_int("DASHBOARD_MAP_MAX_ENTITIES", 0, minimum=0),
        graph_max_neighbours=_int("DASHBOARD_GRAPH_MAX_NEIGHBOURS", 120),
        log_level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        allow_private_hosts=_bool("DASHBOARD_ALLOW_PRIVATE_HOSTS", False),
        test_timeout_seconds=_int("DASHBOARD_TEST_TIMEOUT_SECONDS", 120),
    )
