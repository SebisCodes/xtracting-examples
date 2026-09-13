"""Running the schema files again changes nothing - and keeps what a
customer changed.

    DATABASE_URL=postgresql://... python -m pytest tests/flow/test_seed_idempotent.py -q
"""

from __future__ import annotations

import pytest

from app import schema

pytestmark = pytest.mark.flow

SEEDED_KEYS = {"competitor", "regulator", "investor", "person", "ownership", "customer",
               "supplier", "partner", "product", "location", "other"}


def _snapshot(archive):
    with archive.connect() as c:
        groups = {r["text_key"]: (r["text_name"], r["text_colour"], r["bool_fallback"]) for r in
                  c.execute("SELECT text_key, text_name, text_colour, bool_fallback FROM dashboard.colour_groups")}
        versions = [r["integer_version"] for r in c.execute("SELECT integer_version FROM dashboard.schema_version")]
        types = c.execute("SELECT count(*) AS n FROM dashboard.colour_group_types").fetchone()["n"]
        buckets = c.execute("SELECT count(*) AS n FROM dashboard.buckets").fetchone()["n"]
    return groups, versions, types, buckets


def test_second_and_third_run_are_no_ops(archive):
    before = _snapshot(archive)
    assert set(before[0]) >= SEEDED_KEYS
    assert [k for k, v in before[0].items() if v[2]] == ["other"]
    # One row per revision of dashboard/sql/01-dashboard-schema.sql, and the
    # claim FOLLOWS THE CODE rather than repeating it: a literal `== [1, 2]`
    # with a note saying a new revision adds a number here would make every
    # schema change break a test about idempotence, for a reason that has
    # nothing to do with idempotence.
    #
    # What is worth pinning is that the revisions are a complete run from one,
    # with no gap and nothing from the future, and that the newest of them is
    # the number the application believes it applied.
    assert before[1] == list(range(1, len(before[1]) + 1)), before[1]
    assert before[1][-1] == schema.SCHEMA_VERSION
    archive.apply_schema()
    archive.apply_schema()
    assert _snapshot(archive) == before


def test_a_customers_change_survives_a_reseed(archive):
    with archive.connect() as c:
        c.execute("UPDATE dashboard.colour_groups SET text_colour = '#123456', text_name = 'My suppliers' "
                  "WHERE text_key = 'supplier'")
        c.commit()
    try:
        archive.apply_schema()
        with archive.connect() as c:
            row = c.execute("SELECT text_name, text_colour FROM dashboard.colour_groups WHERE text_key = 'supplier'").fetchone()
        assert (row["text_name"], row["text_colour"]) == ("My suppliers", "#123456")
    finally:
        with archive.connect() as c:
            c.execute("UPDATE dashboard.colour_groups SET text_colour = '#30A0A6', text_name = 'Supplier' "
                      "WHERE text_key = 'supplier'")
            c.commit()


def test_places_view_is_populated_and_refreshable(archive):
    n = archive.scalar("SELECT count(*) FROM dashboard.places WHERE text_project LIKE '_preseed%%'")
    assert n >= 10
    archive.refresh_places()
    assert archive.scalar("SELECT count(*) FROM dashboard.places WHERE text_project LIKE '_preseed%%'") == n
    assert archive.scalar(
        "SELECT count(*) FROM pg_indexes WHERE schemaname = 'dashboard' AND indexname = 'idx_places_key'") == 1


# ── The colour-group suggestion, when the table is empty ─────────────────

def test_the_suggestion_runs_again_when_nothing_is_grouped(archive):
    """AN ARCHIVE WHOSE CONNECTION TYPES ARRIVE AFTER THE SCHEMA MUST NOT
    STAY MONOCHROME.

    If `seed_suggested_types` only ran on the FIRST seed, against whatever
    the archive held at that moment, an archive filled afterwards would have
    `dashboard.colour_group_types` at 0 rows against tens of thousands of
    distinct connection types, so every type would resolve to the fallback
    group and the Map, the Graph and the Colours page would be one brown -
    although `suggest_group()` classifies most of those names (Product →
    product, Manufacturer → supplier, Competitor → competitor).

    Empty is a reason to ask again. One row is not: a decision, or an
    earlier suggestion, is never overwritten by a guess."""
    import os

    from app import schema
    from app.config import Config
    from app.db import Database

    with archive.connect() as c:
        before = [(r["text_type_name"], r["bigint_fk_group"], r["text_source"]) for r in
                  c.execute("SELECT text_type_name, bigint_fk_group, text_source "
                            "FROM dashboard.colour_group_types").fetchall()]
    assert before, "the preseed assigns connection types; this test has nothing to restore"

    db = Database(Config(database_url=os.environ["DATABASE_URL"]))
    db.open()
    try:
        with archive.connect() as c:
            c.execute("DELETE FROM dashboard.colour_group_types")
            c.commit()

        schema.ensure_dashboard_schema(db, Config(database_url=os.environ["DATABASE_URL"]))

        with archive.connect() as c:
            after = {r["text_type_name"]: r["text_source"] for r in
                     c.execute("SELECT text_type_name, text_source "
                               "FROM dashboard.colour_group_types").fetchall()}
        assert after, "an archive with no grouped type at all was left ungrouped"
        # Suggested, not chosen - the Colours page shows that difference and
        # a person has to be able to see that nobody decided this.
        assert set(after.values()) == {"suggested"}, after
        # The preseed's own types are among them: these are the names the
        # keyword rules can place.
        assert "Supplier" in after and "Competitor" in after, sorted(after)

        # And once something is grouped it is left alone: the second run
        # writes nothing, because the table is no longer empty.
        again = dict(after)
        schema.ensure_dashboard_schema(db, Config(database_url=os.environ["DATABASE_URL"]))
        with archive.connect() as c:
            now = {r["text_type_name"]: r["text_source"] for r in
                   c.execute("SELECT text_type_name, text_source "
                             "FROM dashboard.colour_group_types").fetchall()}
        assert now == again
    finally:
        db.close()
        with archive.connect() as c:
            c.execute("DELETE FROM dashboard.colour_group_types")
            c.cursor().executemany(
                "INSERT INTO dashboard.colour_group_types "
                "(text_type_name, bigint_fk_group, text_source) VALUES (%s, %s, %s)", before)
            c.commit()
