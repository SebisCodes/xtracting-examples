"""One archive, one test session at a time - counted per process.

The flow suite fills the archive with the preseed and REMOVES it again when
its session ends. Two runs against the same database therefore take each
other's data away halfway through, and the symptom is never "the preseed is
gone": it is a dozen unrelated failures in whatever happened to be running -
empty suggestion lists, "0 cards", a home page whose counters are all zero -
which pass again when the suite is run on its own. A Postgres advisory lock
makes the second run wait instead.

WHY THE COUNTER. An advisory lock belongs to a CONNECTION, not to a process,
so two session fixtures in one pytest run would block each other as surely as
two terminals would: the first takes the lock, holds it until the whole run
ends, and the second waits for it forever. So the lock is taken once per
PROCESS here and handed out by reference count; the connection closes, and the
lock goes, when the last session releases it.

The lock is released by closing the connection, which also happens when a run
is killed - a crashed suite never leaves the next one hanging.
"""

from __future__ import annotations

import threading

import psycopg

# Arbitrary, but every session must pick the same number.
LOCK_ID = 8_147_251_063

_guard = threading.Lock()
_connection: psycopg.Connection | None = None
_depth = 0


def acquire(dsn: str, timeout_seconds: float = 900.0) -> None:
    """Take the archive for this process, waiting for another run to finish.

    Waiting is the point, so the timeout is generous - it is there to fail
    with something readable instead of hanging forever when a run on another
    machine, or a stuck psql, is holding the lock.
    """
    global _connection, _depth
    with _guard:
        if _depth == 0:
            conn = psycopg.connect(dsn, autocommit=True, connect_timeout=5)
            try:
                # set_config(), not SET: a SET statement takes no bind
                # parameters, and psycopg sends everything as one. The
                # difference is a syntax error at run time, on every session.
                conn.execute("SELECT set_config('lock_timeout', %s, false)",
                             (f"{int(timeout_seconds * 1000)}ms",))
                conn.execute("SELECT pg_advisory_lock(%s)", (LOCK_ID,))
            except psycopg.errors.LockNotAvailable as exc:
                conn.close()
                raise RuntimeError(
                    f"another test session has held the archive for over "
                    f"{timeout_seconds:.0f}s. Is a pytest run still going, or a psql "
                    f"session holding pg_advisory_lock({LOCK_ID})?") from exc
            except Exception:
                conn.close()
                raise
            _connection = conn
        _depth += 1


def release() -> None:
    """Give it back. The last release closes the connection and drops the lock."""
    global _connection, _depth
    with _guard:
        if _depth == 0:
            return
        _depth -= 1
        if _depth == 0 and _connection is not None:
            try:
                _connection.close()
            finally:
                _connection = None


def held() -> bool:
    """For tests of this module: is this process holding the archive?"""
    return _depth > 0
