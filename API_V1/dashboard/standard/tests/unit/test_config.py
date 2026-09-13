"""Configuration: the environment becomes a Config, or a message naming the
variable.

The password case is the one that has bitten before: `p@ss/word` is a valid
PostgreSQL password and an invalid URL, and a dashboard that assembles the
DSN by string concatenation logs in as nobody at all.
"""

from __future__ import annotations

import pytest

from app import config


def test_defaults_are_the_documented_ones(clean_env):
    clean_env("POSTGRES_PASSWORD", "secret")
    cfg = config.load()
    assert cfg.database_url == "postgresql://xtracting:secret@xtracting_db:5432/xtracting_archive"
    assert cfg.port == 8088
    assert cfg.seed is True
    assert cfg.tile_url == "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
    assert cfg.places_refresh_minutes == 15
    assert cfg.statement_timeout_seconds == 300
    # 0 is no limit, and it is the default on purpose: the map draws a network
    # whole. What still bounds a runaway is the per-hop edge cap in
    # routers/api_map.py, which no environment variable can switch off.
    assert cfg.map_max_entities == 0
    assert cfg.graph_max_neighbours == 120
    assert cfg.log_level == "INFO"
    assert cfg.allow_private_hosts is False
    assert cfg.test_timeout_seconds == 120


def test_password_with_url_characters_is_percent_encoded(clean_env):
    clean_env("POSTGRES_PASSWORD", "p@ss/word")
    clean_env("POSTGRES_USER", "us:er")
    clean_env("POSTGRES_DB", "arch ive")
    cfg = config.load()
    assert cfg.database_url == "postgresql://us%3Aer:p%40ss%2Fword@xtracting_db:5432/arch%20ive"


def test_database_url_wins_over_the_parts(clean_env):
    clean_env("DATABASE_URL", "postgresql://a:b@somewhere:5433/db")
    clean_env("POSTGRES_PASSWORD", "ignored")
    assert config.load().database_url == "postgresql://a:b@somewhere:5433/db"


def test_missing_password_names_the_variable(clean_env):
    with pytest.raises(config.ConfigError) as exc:
        config.load()
    assert "POSTGRES_PASSWORD" in str(exc.value)
    assert "DATABASE_URL" in str(exc.value)


def test_old_port_name_is_still_read(clean_env):
    clean_env("POSTGRES_PASSWORD", "x")
    clean_env("POSTGRES_PORT_INTERNAL", "6543")
    assert config.load().database_url.endswith("@xtracting_db:6543/xtracting_archive")
    # The newer name wins when both are set.
    clean_env("POSTGRES_PORT", "7777")
    assert config.load().database_url.endswith("@xtracting_db:7777/xtracting_archive")


def test_every_dashboard_variable_is_read(clean_env):
    clean_env("DATABASE_URL", "postgresql://a:b@c/d")
    clean_env("DASHBOARD_PORT", "9000")
    clean_env("DASHBOARD_SEED", "false")
    clean_env("DASHBOARD_TILE_URL", "https://tiles.example/{z}/{x}/{y}.png")
    clean_env("DASHBOARD_PLACES_REFRESH_MINUTES", "5")
    clean_env("DASHBOARD_STATEMENT_TIMEOUT_SECONDS", "60")
    clean_env("DASHBOARD_MAP_MAX_ENTITIES", "50")
    clean_env("DASHBOARD_GRAPH_MAX_NEIGHBOURS", "12")
    clean_env("DASHBOARD_ALLOW_PRIVATE_HOSTS", "yes")
    clean_env("DASHBOARD_TEST_TIMEOUT_SECONDS", "10")
    clean_env("LOG_LEVEL", "debug")
    cfg = config.load()
    assert cfg.port == 9000
    assert cfg.seed is False
    assert cfg.tile_url == "https://tiles.example/{z}/{x}/{y}.png"
    assert cfg.places_refresh_minutes == 5
    assert cfg.statement_timeout_seconds == 60
    assert cfg.map_max_entities == 50
    assert cfg.graph_max_neighbours == 12
    assert cfg.allow_private_hosts is True
    assert cfg.test_timeout_seconds == 10
    assert cfg.log_level == "DEBUG"


@pytest.mark.parametrize("name,value,fragment", [
    ("DASHBOARD_PORT", "eighty", "whole number"),
    ("DASHBOARD_PORT", "0", "at least 1"),
    ("DASHBOARD_SEED", "maybe", "true or false"),
    ("DASHBOARD_STATEMENT_TIMEOUT_SECONDS", "-5", "at least 1"),
])
def test_bad_values_name_the_variable(clean_env, name, value, fragment):
    clean_env("DATABASE_URL", "postgresql://a:b@c/d")
    clean_env(name, value)
    with pytest.raises(config.ConfigError) as exc:
        config.load()
    assert name in str(exc.value)
    assert fragment in str(exc.value)


def test_tile_url_without_placeholders_is_refused(clean_env):
    clean_env("DATABASE_URL", "postgresql://a:b@c/d")
    clean_env("DASHBOARD_TILE_URL", "https://tiles.example/static.png")
    with pytest.raises(config.ConfigError) as exc:
        config.load()
    assert "DASHBOARD_TILE_URL" in str(exc.value)


def test_config_is_frozen(clean_env):
    clean_env("DATABASE_URL", "postgresql://a:b@c/d")
    cfg = config.load()
    with pytest.raises(Exception):
        cfg.port = 1  # type: ignore[misc]
