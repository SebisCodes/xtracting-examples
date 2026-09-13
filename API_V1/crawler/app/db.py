"""Talking to the archive.

Small on purpose. The crawler does a handful of statements per run, none of
them hot, and a connection pool would add a lifecycle to reason about for no
gain. What is here instead is the thing that IS needed: dictionary rows, so
that every query in this service names its columns rather than counting them,
and a wait-for-database that says something useful when the host is wrong.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

import psycopg
from psycopg.rows import dict_row

log = logging.getLogger(__name__)


def connect(dsn: str, *, autocommit: bool = False) -> psycopg.Connection:
    return psycopg.connect(dsn, row_factory=dict_row, autocommit=autocommit)


def fetch_all(conn: psycopg.Connection, sql: str, params: Any = None) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return list(cur.fetchall())


def fetch_one(conn: psycopg.Connection, sql: str, params: Any = None) -> dict | None:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()


def execute(conn: psycopg.Connection, sql: str, params: Any = None) -> int:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.rowcount


def connection_hint(dsn: str, exc: Exception) -> str | None:
    """What to change, for the two failures that are actually configuration.

    Repeating a resolver error thirty times tells someone their database is
    unreachable, which they already know. It does not tell them that the host
    they need is in a variable they have never seen. Same wording as the
    collector's, because it is the same mistake.
    """
    host = ""
    match = re.search(r"@\[([^\]]+)\]", dsn) or re.search(r"@([^/:?]+)", dsn)
    if match:
        host = match.group(1)
    text = str(exc).lower()

    if host in {"localhost", "127.0.0.1", "::1"}:
        return (f"POSTGRES_HOST is {host!r}, which inside a container means this "
                "container - not the machine it runs on. Use 'xtracting_db' for "
                "the database from API_V1/database, or host.docker.internal (Podman: "
                "host.containers.internal) for one running directly on your machine.")
    if "name resolution" in text or "name or service not known" in text:
        if host == "xtracting_db":
            return ("The name 'xtracting_db' only resolves on the archive network, "
                    "which the database container creates. Start API_V1/database first, "
                    "or set POSTGRES_HOST in .env to where your database actually is.")
        return (f"The host {host!r} does not resolve from inside the container. "
                "Check POSTGRES_HOST in .env - a hostname or IP the container can reach.")
    return None


def wait_for_database(dsn: str, attempts: int = 30) -> None:
    hinted = False
    for attempt in range(attempts):
        try:
            with connect(dsn) as conn:
                conn.execute("SELECT 1")
            return
        except psycopg.Error as exc:
            if not hinted:
                hint = connection_hint(dsn, exc)
                if hint:
                    log.error("%s", hint)
                hinted = True
            log.info("waiting for the database (%d/%d): %s", attempt + 1, attempts, exc)
            time.sleep(2)
    raise SystemExit("database did not become available")


def require_crawler_schema(dsn: str) -> None:
    """Stop early if this database has no crawler tables.

    `.env.example` invites pointing DATABASE_URL at any archive, so pointing it
    at one where `03-scraper.sql` was never applied is a predictable mistake.
    Unchecked it surfaces as a traceback from whichever table is touched first,
    which reads like a bug in the crawler.
    """
    with connect(dsn) as conn:
        row = fetch_one(conn, """
            SELECT to_regclass('scraper_config.sources') IS NOT NULL AS config,
                   to_regclass('scraper.targets')        IS NOT NULL AS runtime,
                   to_regclass('processed_data.tasks')   IS NOT NULL AS archive
        """)
    missing = [name for name in ("config", "runtime", "archive")
               if not (row or {}).get(name)]
    if missing:
        raise SystemExit(
            "connected, but this database is not a complete archive - missing: "
            + ", ".join({"config": "scraper_config.sources",
                         "runtime": "scraper.targets",
                         "archive": "processed_data.tasks"}[name] for name in missing)
            + ". Load database/init/03-scraper.sql into it, or point the crawler "
              "at the database from API_V1/database.")


def paused(conn: psycopg.Connection) -> bool:
    """The dashboard's global pause switch.

    A `dashboard.settings` row rather than an environment variable, because the
    person who needs to stop a crawl in a hurry has a browser open, not a
    shell. Missing table or missing row means "not paused": the crawler must
    run without the dashboard's schema.
    """
    try:
        row = fetch_one(conn, """
            SELECT text_value FROM dashboard.settings
             WHERE text_key = 'scraper.paused'
        """)
    except psycopg.Error:
        conn.rollback()
        return False
    return bool(row) and str(row["text_value"]).strip().lower() in ("1", "true", "yes", "on")


def heartbeat(conn: psycopg.Connection, service: str, version: str = "1.0") -> None:
    """One row per SERVICE, overwritten. Feeds the status pill and nothing
    else - editing and testing sources work without a crawler.

    IT IS `monitoring.heartbeat`, keyed by the service rather than by a
    worker. A heartbeat table belonging to this crawler alone would leave the
    collector able to prove it is alive only by leaving a run row - and a
    collector that has crashed leaves no run row at all, which would make the
    one state worth seeing the one state invisible. The dashboard asks the
    same question of both services, so both answer in the same table.

    Called ONCE A MINUTE, not once a tick (main.Runner). The reader only asks
    whether the row is younger than three minutes; at one write every five
    seconds twelve of every thirteen of them told nobody anything."""
    execute(conn, """
        INSERT INTO monitoring.heartbeat (text_service, date_seen, text_version)
        VALUES (%s, NOW(), %s)
        ON CONFLICT (text_service) DO UPDATE
           SET date_seen = NOW(), text_version = EXCLUDED.text_version
    """, (service, version))
