"""Flow tests: the dashboard's code against a real archive with the preseed.

    DATABASE_URL=postgresql://... python -m pytest tests/flow -q

Without DATABASE_URL every test here is skipped, not failed: the unit suite
is what runs without a database. With it, the session loads the preseed
(tests/preseed/preseed.py), applies the dashboard schema by running the
three SQL files directly - not through app.main, so a broken lifespan cannot
hide a broken schema file - refreshes the place list, and removes the preseed
again at the end.

Point DATABASE_URL at a throw-away archive (database/.env.test) and never at
the one you keep.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import psycopg
import pytest
from psycopg.rows import dict_row

DASHBOARD_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(DASHBOARD_DIR))
sys.path.insert(0, str(DASHBOARD_DIR / "tests"))
sys.path.insert(0, str(DASHBOARD_DIR / "tests" / "preseed"))

import archive_gate  # noqa: E402
import preseed  # noqa: E402

SQL_DIR = DASHBOARD_DIR / "sql"
SCHEMA_FILES = ("01-dashboard-schema.sql", "03-places-view.sql", "02-dashboard-seed.sql")


@dataclass
class Archive:
    dsn: str

    def connect(self, autocommit: bool = False) -> psycopg.Connection:
        """A fresh connection with dict rows. Close it, or use it as a
        context manager - the archive is shared by every test."""
        return psycopg.connect(self.dsn, row_factory=dict_row, autocommit=autocommit)

    def apply_schema(self) -> None:
        with self.connect() as conn:
            for name in SCHEMA_FILES:
                conn.execute((SQL_DIR / name).read_text(encoding="utf-8"))
            conn.commit()

    def refresh_places(self) -> None:
        # EVERY list a suggestion box is answered from, taken from the one
        # place that names them (app/schema.py: SUGGESTION_VIEWS). Hard-coded
        # names here are how a third view can be added to
        # sql/03-places-view.sql and silently never refreshed in tests - the
        # suggestions would then be empty and the test would blame the query.
        from app.schema import SUGGESTION_VIEWS
        with self.connect(autocommit=True) as conn:
            for name in SUGGESTION_VIEWS:
                try:
                    conn.execute(f"REFRESH MATERIALIZED VIEW CONCURRENTLY {name}")
                except psycopg.Error:
                    conn.execute(f"REFRESH MATERIALIZED VIEW {name}")

    def scalar(self, query: str, params=None):
        with self.connect() as conn:
            row = conn.execute(query, params).fetchone()
            return next(iter(row.values())) if row else None


def _dsn() -> str:
    return os.environ.get("DATABASE_URL", "").strip()


# One flow session at a time per archive - see tests/archive_gate.py for why
# the lock is counted per process rather than taken here directly.
@pytest.fixture(scope="session")
def archive() -> Iterator[Archive]:
    dsn = _dsn()
    if not dsn:
        pytest.skip("DATABASE_URL is not set; flow tests need a running archive")
    arch = Archive(dsn)
    try:
        with arch.connect() as conn:
            conn.execute("SELECT 1")
    except psycopg.Error as exc:
        pytest.skip(f"archive at DATABASE_URL not reachable: {exc}")

    archive_gate.acquire(dsn)
    try:
        preseed.load(dsn)
        arch.apply_schema()
        arch.refresh_places()
        yield arch
        preseed.remove(dsn)
    finally:
        archive_gate.release()


@pytest.fixture
def conn(archive: Archive) -> Iterator[psycopg.Connection]:
    """One connection per test, rolled back afterwards: a test may write to
    dashboard.* and leave nothing behind."""
    with archive.connect() as c:
        yield c
        c.rollback()


@pytest.fixture(scope="session")
def client(archive: Archive):
    """A TestClient over the real app, for the API tests. Imported lazily:
    the schema fixture above must not depend on app.main importing."""
    os.environ["DATABASE_URL"] = archive.dsn
    try:
        from fastapi.testclient import TestClient
        from app.main import app
    except Exception as exc:  # noqa: BLE001 - a missing router is a skip, not a crash here
        pytest.skip(f"app.main not importable: {exc}")
    with TestClient(app) as tc:
        yield tc


# Names the tests refer to, so a rename in the preseed is a rename here.
ALPHA = preseed.ALPHA
BETA = preseed.BETA
