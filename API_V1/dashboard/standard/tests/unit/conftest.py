"""Shared fixtures for the unit suite.

Unit tests need no database: they exercise the pure parts - configuration,
context resolution, SQL building, address splitting. `app` is importable
because pytest.ini puts the dashboard folder on the path - and, for a run
started from the API_V1 folder with another folder's ini, because
../../conftest.py does the same.

`clean_env` strips every variable the dashboard reads, so a developer's own
.env or shell (a real DATABASE_URL, a LOG_LEVEL=DEBUG) cannot leak into a
test and make it pass or fail for reasons that are not in the test.
"""

from __future__ import annotations

import os

import pytest

DASHBOARD_VARS = (
    "DATABASE_URL", "POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB", "POSTGRES_HOST",
    "POSTGRES_PORT", "POSTGRES_PORT_INTERNAL", "DASHBOARD_PORT", "DASHBOARD_SEED",
    "DASHBOARD_TILE_URL", "DASHBOARD_PLACES_REFRESH_MINUTES",
    "DASHBOARD_STATEMENT_TIMEOUT_SECONDS", "DASHBOARD_MAP_MAX_ENTITIES",
    "DASHBOARD_GRAPH_MAX_NEIGHBOURS", "DASHBOARD_ALLOW_PRIVATE_HOSTS",
    "DASHBOARD_TEST_TIMEOUT_SECONDS", "LOG_LEVEL",
)


@pytest.fixture
def clean_env(monkeypatch):
    """An environment with none of the dashboard's variables set. Returns a
    setter so a test reads `env("DATABASE_URL", "...")` rather than reaching
    for monkeypatch itself."""
    for name in DASHBOARD_VARS:
        monkeypatch.delenv(name, raising=False)

    def setenv(name: str, value: str) -> None:
        monkeypatch.setenv(name, value)

    return setenv


@pytest.fixture
def pairs() -> list[tuple[str, str]]:
    """The (project, language) pairs a small archive with translations has,
    in the order /api/projects presents them: by project, English first."""
    return [
        ("_preseed Alpha", "English"),
        ("_preseed Alpha", "German"),
        ("_preseed Beta", "English"),
    ]


@pytest.fixture(autouse=True)
def _no_database_url_by_default(monkeypatch):
    """Nothing in the unit suite may talk to a database, so the URL is not
    even visible - a test that needs one to be set says so via clean_env."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    # Silence the unused-import warning tools would raise for os.
    assert os is not None
