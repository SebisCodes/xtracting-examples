"""Configuration: the things that are wrong in `.env` before anything runs.

A crawler that starts and then quietly does nothing is the worst outcome here,
so most of these tests are about stopping early with a sentence that names the
variable.
"""

from __future__ import annotations

import pytest

from app import config


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    """Start from nothing: a developer's own .env must not decide a test."""
    for name in list(os_environ_names()):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("XTRACTING_API_KEYS", "flats:ab12cd34.secret")
    monkeypatch.setenv("POSTGRES_PASSWORD", "test")


def os_environ_names():
    import os
    return [name for name in os.environ
            if name.startswith(("CRAWLER_", "POSTGRES_", "XTRACTING_", "DATABASE_",
                                "LOG_LEVEL", "REQUEST_TIMEOUT"))]


# -- the keys ----------------------------------------------------------

def test_a_labelled_key_keeps_its_label():
    """The label is what a source points at - it is not decoration."""
    keys = config.parse_keys("flats:ab12.secret, law:ef56.other")
    assert [entry.label for entry in keys] == ["flats", "law"]
    assert keys[0].secret == "ab12.secret"


def test_an_unlabelled_key_is_named_after_its_prefix_never_its_secret():
    keys = config.parse_keys("ab12cd34.thesecret")
    assert keys[0].label == "ab12cd34"
    assert "thesecret" not in keys[0].label


def test_a_repeated_key_is_taken_once():
    """Two labels for one key would submit - and pay for - every document
    twice."""
    assert len(config.parse_keys("a:k1.x, b:k1.x, c:k2.y")) == 2


def test_no_keys_stops_with_a_message_naming_the_variable(monkeypatch):
    monkeypatch.setenv("XTRACTING_API_KEYS", "  ")
    with pytest.raises(config.ConfigError) as raised:
        config.load()
    assert "XTRACTING_API_KEYS" in str(raised.value)


def test_a_source_finds_its_key_by_label():
    cfg = config.load()
    assert cfg.key("flats").secret == "ab12cd34.secret"
    # Not an error the crawler can fix: a source configured against a key this
    # container does not have. It is recorded and shown, and the documents wait.
    assert cfg.key("law") is None


# -- the database ------------------------------------------------------

def test_the_dsn_is_assembled_from_the_parts(monkeypatch):
    monkeypatch.setenv("POSTGRES_HOST", "db.example.com")
    monkeypatch.setenv("POSTGRES_PORT", "6543")
    assert config.load().database_url == (
        "postgresql://xtracting:test@db.example.com:6543/xtracting_archive")


def test_a_password_with_punctuation_survives(monkeypatch):
    """A password containing @ or / breaks a hand-built URL silently - the
    connection then goes to a host nobody configured."""
    monkeypatch.setenv("POSTGRES_PASSWORD", "p@ss/wo:rd#1")
    assert "p%40ss%2Fwo%3Ard%231" in config.load().database_url


def test_an_explicit_url_wins(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@elsewhere/db")
    assert config.load().database_url == "postgresql://u:p@elsewhere/db"


def test_the_crawler_role_is_used_when_it_is_set(monkeypatch):
    """After 04-roles.sh the crawler has a login of its own, and then the
    boundary between the services is enforced rather than agreed."""
    monkeypatch.setenv("SCRAPER_DB_USER", "xtracting_crawler")
    monkeypatch.setenv("SCRAPER_DB_PASSWORD", "srp")
    assert config.load().database_url.startswith(
        "postgresql://xtracting_crawler:srp@")


def test_no_password_stops_with_advice(monkeypatch):
    monkeypatch.delenv("POSTGRES_PASSWORD", raising=False)
    with pytest.raises(config.ConfigError) as raised:
        config.load()
    assert "POSTGRES_PASSWORD" in str(raised.value)


# -- the numbers -------------------------------------------------------

def test_the_defaults_are_the_documented_ones():
    cfg = config.load()
    assert (cfg.concurrency, cfg.tick_seconds, cfg.lease_minutes) == (2, 5, 30)
    assert cfg.submit_interval_seconds == 60
    assert cfg.reconcile_minutes == 15
    assert cfg.max_batch_tasks == 1000
    assert cfg.max_content_chars == 2_000_000


def test_a_request_limit_at_or_above_five_megabytes_is_refused(monkeypatch):
    """Above it the gateway in front of the API answers with an HTML error
    page, and the log then shows markup instead of a reason."""
    monkeypatch.setenv("CRAWLER_MAX_REQUEST_BYTES", "5000000")
    with pytest.raises(config.ConfigError) as raised:
        config.load()
    assert "5 MB" in str(raised.value)


def test_the_api_limits_cannot_be_raised_beyond_the_api(monkeypatch):
    monkeypatch.setenv("CRAWLER_MAX_BATCH_TASKS", "5000")
    monkeypatch.setenv("CRAWLER_MAX_CONTENT_CHARS", "9000000")
    cfg = config.load()
    assert cfg.max_batch_tasks == 1000
    assert cfg.max_content_chars == 2_000_000


def test_a_number_that_is_not_one_says_which_variable(monkeypatch):
    monkeypatch.setenv("CRAWLER_CONCURRENCY", "two")
    with pytest.raises(config.ConfigError) as raised:
        config.load()
    assert "CRAWLER_CONCURRENCY" in str(raised.value)


def test_a_flag_that_is_not_a_flag_says_which_variable(monkeypatch):
    monkeypatch.setenv("CRAWLER_ONLY_TESTS", "maybe")
    with pytest.raises(config.ConfigError) as raised:
        config.load()
    assert "CRAWLER_ONLY_TESTS" in str(raised.value)


# -- who we say we are -------------------------------------------------

def test_the_user_agent_defaults_to_the_shared_one():
    """The same string crawlkit uses, so that a site's operator sees one name
    whether the request came from the crawler or from a test in the dashboard."""
    from crawlkit.crawl import DEFAULT_USER_AGENT
    assert config.load().user_agent == DEFAULT_USER_AGENT


def test_a_user_agent_without_a_contact_is_warned_about(monkeypatch, caplog):
    monkeypatch.setenv("CRAWLER_USER_AGENT", "bot")
    with caplog.at_level("WARNING"):
        config.load()
    assert any("block" in record.getMessage() for record in caplog.records)
