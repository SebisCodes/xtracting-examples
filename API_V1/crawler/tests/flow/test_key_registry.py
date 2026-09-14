"""The registry, against a real archive and a platform that answers.

Two things are proved here that a unit test cannot. The first is that what the
crawler learned survives into the database in the shape the dashboard reads.
The second is the boundary, and it is the one worth the cost of a flow test: a
watchlist whose project has no working key must STOP rather than borrow a key
from another project, because a borrowed key files the documents in somebody
else's archive - and nothing downstream can notice that.
"""

from __future__ import annotations

import pytest

from app import db, keys

# tests/flow has no __init__.py - pytest imports these modules by path, so a
# relative import has no package to be relative to. conftest.py is on sys.path
# by the time a test module is imported, which is how the other flow tests reach
# the harness as well.
from conftest import KEY_LABEL, KEY_SECRET, TEST_PROJECT


class Entry:
    def __init__(self, label: str, secret: str) -> None:
        self.label, self.secret = label, secret

    @property
    def prefix(self) -> str:
        return self.secret.split(".", 1)[0]


@pytest.fixture()
def registry(crawler):
    """A platform that knows this key, and a connection to look afterwards."""
    crawler.api.state.add_key(KEY_SECRET, name="the test key",
                              project_id=TEST_PROJECT, project_name="Crawler test")
    return crawler


def refresh(harness, entries=None):
    cfg = harness.cfg
    if entries is not None:
        cfg = type(cfg)(**{**cfg.__dict__, "api_keys": entries})
    with db.connect(cfg.database_url) as conn:
        found = keys.refresh(conn, cfg)
    return found


def rows(dsn, sql, params=None):
    with db.connect(dsn) as conn:
        return db.fetch_all(conn, sql, params)


def test_a_project_appears_after_the_first_probe(registry):
    refresh(registry)

    projects = rows(registry.dsn,
                    "SELECT * FROM scraper.projects WHERE text_project_id = %s",
                    (TEST_PROJECT,))
    assert len(projects) == 1
    assert projects[0]["text_name"] == "Crawler test"

    stored = rows(registry.dsn,
                  "SELECT * FROM scraper.api_keys WHERE text_project_id = %s",
                  (TEST_PROJECT,))
    assert [k["text_prefix"] for k in stored] == ["test1234"]
    assert stored[0]["bool_can_extract"] is True
    assert stored[0]["text_label"] == KEY_LABEL


def test_a_key_taken_out_of_the_environment_disappears(registry):
    """The registry is what the crawler can currently authenticate with, not a
    history of what it once could. A key that stops answering is deleted, and
    that deletion is what makes the dashboard's warning possible."""
    refresh(registry)
    assert rows(registry.dsn, "SELECT 1 FROM scraper.api_keys WHERE text_prefix = 'test1234'")

    # The platform forgets it: the same thing, from the crawler's side, as
    # deleting it in Xtracting's dashboard. The registry then has to be empty of
    # THIS project - the fake keeps a project of its own for the tests that
    # predate the registry, so an empty dict is the wrong claim.
    registry.api.state.keys.pop(KEY_SECRET)
    refresh(registry)

    assert TEST_PROJECT not in {
        row["text_project_id"] for row in rows(
            registry.dsn, "SELECT text_project_id FROM scraper.api_keys")}
    assert not rows(registry.dsn, "SELECT 1 FROM scraper.api_keys WHERE text_prefix = 'test1234'")
    # The project stays. That is how "not used for seven days" becomes something
    # a person can be offered, instead of a row that vanishes with its last key.
    assert rows(registry.dsn, "SELECT 1 FROM scraper.projects WHERE text_project_id = %s",
                (TEST_PROJECT,))


def test_a_pinned_key_that_disappears_is_reported_by_name(registry):
    """A pinned key is a promise about settings and price. When it goes, the
    honest thing is to stop and say which watchlist stopped - not to move the
    work to the next key at another price."""
    refresh(registry)
    source_id = registry.add_source(
        "pinned", registry.site.estate_list, accept=["/rent/1"])
    with db.connect(registry.dsn) as conn:
        db.execute(conn, "UPDATE scraper_config.source_projects "
                         "SET text_key_prefix = 'test1234' "
                         "WHERE bigint_fk_source = %s AND text_project_id = %s",
                   (source_id, TEST_PROJECT))
        conn.commit()

    registry.api.state.keys.pop(KEY_SECRET)
    refresh(registry)

    problems = [row["text_message"] for row in rows(registry.dsn, """
        SELECT text_message FROM monitoring.scraper_errors
         WHERE bigint_fk_source = %s AND text_kind = 'KEY_INVALID'
    """, (source_id,))]
    # The key it named by name. That is the sentence this test is about: a
    # pinned key is a promise, and when it breaks the promise is named.
    named = [message for message in problems if "test1234" in message]
    assert len(named) == 1, problems
    assert "pinned" in named[0]

    # It was also the project's last key, and that is a SECOND fact with a
    # second thing to do about it - "the key you chose is gone" and "this
    # project has no key at all" are not the same sentence and are not the
    # same fix. Both are written, once each.
    assert len(problems) == 2, problems
    assert any("test1234" not in message for message in problems)


def test_the_default_key_is_the_first_of_the_project(registry):
    """Order is the only preference a person can express without a project-read
    key, so the first live extraction key of the project wins."""
    registry.api.state.add_key("second99.secret", name="second",
                               project_id=TEST_PROJECT, project_name="Crawler test")
    refresh(registry, [Entry(KEY_LABEL, KEY_SECRET), Entry("second", "second99.secret")])

    with db.connect(registry.dsn) as conn:
        assert keys.default_key(conn, TEST_PROJECT) == "test1234"


def test_the_fallback_never_leaves_the_project(registry):
    """The claim the whole design turns on. A project whose keys are all gone
    has no default, and `None` is the answer that must not be worked around: a
    watchlist of that project stops, rather than collecting into another
    project's archive."""
    registry.api.state.add_key("other111.secret", name="elsewhere",
                               project_id="_crawlertest_other", project_name="Elsewhere")
    refresh(registry, [Entry(KEY_LABEL, KEY_SECRET), Entry("other", "other111.secret")])

    try:
        with db.connect(registry.dsn) as conn:
            assert keys.default_key(conn, TEST_PROJECT) == "test1234"
            assert keys.default_key(conn, "_crawlertest_other") == "other111"

        # Every key of the first project goes; the second project's key stays.
        registry.api.state.keys.pop(KEY_SECRET)
        refresh(registry, [Entry("other", "other111.secret")])

        with db.connect(registry.dsn) as conn:
            assert keys.default_key(conn, TEST_PROJECT) is None
            # And the other project is untouched - the boundary holds in both
            # directions, or this would only prove that everything broke.
            assert keys.default_key(conn, "_crawlertest_other") == "other111"
    finally:
        with db.connect(registry.dsn) as conn:
            db.execute(conn, "DELETE FROM scraper.api_keys WHERE text_project_id = %s",
                       ("_crawlertest_other",))
            db.execute(conn, "DELETE FROM scraper.projects WHERE text_project_id = %s",
                       ("_crawlertest_other",))
            conn.commit()


def test_a_project_that_loses_its_last_key_says_which_watchlists_stopped(registry):
    """The other way a watchlist goes quiet. Not its own key going - the last
    key of its whole project, with nothing to fall back to because the fallback
    deliberately stops at the project boundary. Nothing else on screen would say
    why collection stopped, so this row is the only account of it."""
    refresh(registry)
    source_id = registry.add_source(
        "stranded", registry.site.estate_list, accept=["/rent/1"])
    with db.connect(registry.dsn) as conn:
        db.execute(conn, "INSERT INTO scraper_config.source_projects "
                         "(bigint_fk_source, text_project_id) VALUES (%s, %s) "
                         "ON CONFLICT DO NOTHING", (source_id, TEST_PROJECT))
        conn.commit()

    # Still fine: the project has a key.
    refresh(registry)
    assert not rows(registry.dsn, """
        SELECT 1 FROM monitoring.scraper_errors
         WHERE bigint_fk_source = %s AND text_kind = 'KEY_INVALID'
    """, (source_id,))

    registry.api.state.keys.pop(KEY_SECRET)
    refresh(registry)

    problems = rows(registry.dsn, """
        SELECT text_message FROM monitoring.scraper_errors
         WHERE bigint_fk_source = %s AND text_kind = 'KEY_INVALID'
    """, (source_id,))
    assert len(problems) == 1, problems
    assert "stranded" in problems[0]["text_message"]
    assert "wrong archive" in problems[0]["text_message"]

    # Said once. A second pass changes nothing, so it must add nothing: a
    # warning repeated every tick is a warning nobody reads.
    refresh(registry)
    again = rows(registry.dsn, """
        SELECT 1 FROM monitoring.scraper_errors
         WHERE bigint_fk_source = %s AND text_kind = 'KEY_INVALID'
    """, (source_id,))
    assert len(again) == 1, again


def test_an_old_project_is_the_one_nothing_has_used(registry):
    """`date_last_seen` is what "old" is measured against, and a probe moves it.
    Without that the badge would appear on a project in daily use."""
    refresh(registry)
    with db.connect(registry.dsn) as conn:
        db.execute(conn, "UPDATE scraper.projects SET date_last_seen = NOW() - INTERVAL '30 days' "
                         "WHERE text_project_id = %s", (TEST_PROJECT,))
        conn.commit()

    old = rows(registry.dsn, """
        SELECT date_last_seen < NOW() - make_interval(days => %(d)s) AS old
          FROM scraper.projects WHERE text_project_id = %(p)s
    """, {"d": keys.OLD_AFTER_DAYS, "p": TEST_PROJECT})
    assert old[0]["old"] is True

    refresh(registry)
    fresh = rows(registry.dsn, """
        SELECT date_last_seen < NOW() - make_interval(days => %(d)s) AS old
          FROM scraper.projects WHERE text_project_id = %(p)s
    """, {"d": keys.OLD_AFTER_DAYS, "p": TEST_PROJECT})
    assert fresh[0]["old"] is False


def test_a_label_is_migrated_into_a_prefix_once(registry):
    """Existing watchlists point at a key by a typed label. The registry knows
    each label's prefix, so the migration is a join and not a guess."""
    refresh(registry)
    # A page from before the registry: a typed label and NO project at all,
    # which is the state the backfill exists for.
    source_id = registry.add_source(
        "labelled", registry.site.estate_list, accept=["/rent/1"],
        projects=[], text_key_label=KEY_LABEL)

    with db.connect(registry.dsn) as conn:
        assert keys.backfill_prefixes(conn) >= 1
        conn.commit()

    row = rows(registry.dsn, "SELECT text_key_prefix, text_project_id "
                             "FROM scraper_config.source_projects "
                             "WHERE bigint_fk_source = %s", (source_id,))[0]
    assert row["text_key_prefix"] == "test1234"
    assert row["text_project_id"] == TEST_PROJECT
