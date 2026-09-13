"""Connections to the archive.

Two pools, on purpose. The READ pool is what every view uses: its connections
open every transaction read-only, so a mistake in a query builder cannot
change the archive - `processed_data` belongs to the collector, and the
dashboard only ever looks at it. The WRITE pool exists for the dashboard's
own schemas, `dashboard.*` (buckets, colour groups) and `scraper_config.*`
(sources the crawler reads), and nothing else should be reached through it.

Both pools carry a statement timeout, so a summary over a large archive that
would take minutes shows an error instead of holding a connection until
somebody notices. Rows come back as dicts, so the code names columns rather
than counting them.
"""

from __future__ import annotations

import logging
import re
import time
from contextlib import contextmanager
from typing import Iterator

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .config import Config

log = logging.getLogger(__name__)


class Database:
    def __init__(self, cfg: Config) -> None:
        timeout_ms = cfg.statement_timeout_seconds * 1000
        # libpq's `options` is how a session setting is fixed at connection
        # time, before any query runs. Doing it here rather than with SET on
        # every checkout means a connection can never be handed out without it.
        common = dict(row_factory=dict_row, connect_timeout=10)
        # JIT OFF, AND IT IS NOT A MICRO-OPTIMISATION.
        #
        # Every query here aggregates across a hypertable, and a hypertable is
        # hundreds or thousands of chunks - one per day. PostgreSQL decides to
        # JIT-compile on estimated COST, and a query spanning 600 chunks looks
        # expensive, so it compiles an expression for each of them: measured on
        # a real archive, one suggestion query produced 5203 compiled functions
        # and took 40.7 SECONDS, of which 39.8 was compilation. The same query
        # with jit off runs in 0.89 s. Nothing about the plan changed.
        #
        # It never shows up on a small archive - a demo database has a handful
        # of chunks and JIT stays out of it - which is exactly why it has to be
        # set here rather than discovered by each person who fills the archive.
        jit = "-c jit=off"
        self._read = ConnectionPool(
            cfg.database_url,
            name="read",
            min_size=1,
            max_size=8,
            open=False,
            kwargs={
                **common,
                "application_name": "xtracting-dashboard read",
                "options": (f"-c default_transaction_read_only=on "
                            f"-c statement_timeout={timeout_ms} {jit}"),
            },
        )
        self._write = ConnectionPool(
            cfg.database_url,
            name="write",
            min_size=1,
            max_size=3,
            open=False,
            kwargs={
                **common,
                "application_name": "xtracting-dashboard write",
                "options": f"-c statement_timeout={timeout_ms} {jit}",
            },
        )

    def open(self) -> None:
        # wait=True makes the min_size connections real before the first
        # request, so a wrong password fails here with a readable message
        # rather than on somebody's first click.
        self._read.open(wait=True, timeout=30)
        self._write.open(wait=True, timeout=30)

    def close(self) -> None:
        self._read.close()
        self._write.close()

    @contextmanager
    def read(self) -> Iterator[psycopg.Connection]:
        """A read-only connection to the archive. Commits on exit, which for a
        read-only transaction only releases the snapshot."""
        with self._read.connection() as conn:
            yield conn

    @contextmanager
    def write(self) -> Iterator[psycopg.Connection]:
        """A connection for the dashboard's own schemas. The transaction is
        committed when the block ends without an exception, rolled back
        otherwise - callers do not call commit() themselves."""
        with self._write.connection() as conn:
            yield conn


# The one Database the application uses, created in main.py's lifespan. A
# module-level singleton rather than app.state, so routers and the background
# threads reach it the same way and tests can swap it for another.
_db: Database | None = None


def set_db(db: Database | None) -> None:
    global _db
    _db = db


def get_db() -> Database:
    if _db is None:
        raise RuntimeError("the database is not initialised - main.py's lifespan sets it")
    return _db


def connection_hint(dsn: str, exc: Exception) -> str | None:
    """What to change, for the two failures that are actually configuration.

    Repeating a resolver error thirty times tells someone their database is
    unreachable, which they already know. It does not tell them that the host
    they need is in a variable they have never seen.
    """
    # An IPv6 host is bracketed in a DSN - postgresql://u:p@[::1]:5432/db - so
    # the bracketed form has to be tried first, or the host reads as "[".
    host = ""
    match = re.search(r"@\[([^\]]+)\]", dsn) or re.search(r"@([^/:?]+)", dsn)
    if match:
        host = match.group(1)
    text = str(exc).lower()

    if host in {"localhost", "127.0.0.1", "::1"}:
        return (f"POSTGRES_HOST is {host!r}, which inside a container means this "
                "container - not the machine it runs on. Use 'xtracting_db' for the "
                "database from API_V1/database, or host.docker.internal (Podman: "
                "host.containers.internal) for one running directly on your machine.")

    if "name resolution" in text or "name or service not known" in text:
        if host == "xtracting_db":
            return ("The name 'xtracting_db' only resolves on the archive network, "
                    "which the database container creates. Start API_V1/database first, or "
                    "set POSTGRES_HOST in .env to where your database actually is.")
        return (f"The host {host!r} does not resolve from inside the container. Check "
                "POSTGRES_HOST in .env - a hostname or IP the container can reach.")

    return None


def wait_for_database(cfg: Config, attempts: int = 30) -> None:
    """Block until the archive answers, or give up with a message.

    The database container takes a while on first start; the dashboard is
    usually started right after it, and a restart loop with a clear log line
    beats a crash nobody reads.
    """
    hinted = False
    for i in range(attempts):
        try:
            with psycopg.connect(cfg.database_url, connect_timeout=5) as conn:
                conn.execute("SELECT 1")
            return
        except psycopg.Error as exc:
            if not hinted:
                hint = connection_hint(cfg.database_url, exc)
                if hint:
                    log.error("%s", hint)
                hinted = True
            log.info("waiting for the database (%d/%d): %s", i + 1, attempts, exc)
            time.sleep(2)
    raise SystemExit("database did not become available")


def require_archive_schema(conn: psycopg.Connection) -> None:
    """Stop early if this is a PostgreSQL without the archive schema.

    .env.example invites pointing DATABASE_URL at any database carrying the
    schema, so pointing it at one that does not is a predictable mistake. Left
    unchecked it surfaces as a traceback from whichever table is touched first,
    which reads like a bug in the dashboard.
    """
    row = conn.execute(
        "SELECT to_regclass('processed_data.tasks') IS NOT NULL AS present"
    ).fetchone()
    # Works with and without dict_row on the connection.
    found = bool(row and (row["present"] if isinstance(row, dict) else row[0]))
    if not found:
        raise SystemExit(
            "connected, but this database has no archive schema - "
            "processed_data.tasks does not exist. Point the dashboard at the "
            "database from API_V1/database, or load database/init/01-schema.sql "
            "into this one first."
        )
