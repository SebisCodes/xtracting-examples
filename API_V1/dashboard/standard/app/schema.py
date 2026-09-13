"""The dashboard's own schema, created by the dashboard.

`processed_data` is the collector's and stays read-only. Buckets, colour groups
and the place list live in a schema called `dashboard`, and the SQL that
creates it is in ../sql, in the order it has to run:

    01-dashboard-schema.sql   CREATE SCHEMA / CREATE TABLE ... IF NOT EXISTS
    03-places-view.sql        the materialized view over locations
    02-dashboard-seed.sql     the default colour groups, ON CONFLICT DO NOTHING

Everything in them is idempotent, so a restart is a no-op, and the three run
in one transaction so a failure half way leaves nothing behind. With
DASHBOARD_SEED=false nothing is run: the schema is only checked, for
databases where the application user may not create anything and somebody
ran the files by hand.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import psycopg

from .config import Config
from .db import Database

log = logging.getLogger(__name__)

SQL_DIR = Path(__file__).resolve().parents[1] / "sql"
SQL_FILES = ("01-dashboard-schema.sql", "03-places-view.sql", "02-dashboard-seed.sql")

# Bumped when a file above changes shape. Recorded in dashboard.schema_version
# so a later migration can tell what it is upgrading from. The SQL file
# records the same number itself, so the row is there whether the files were
# run by the dashboard or by hand.
SCHEMA_VERSION = 1

MISSING = (
    "DASHBOARD_SEED is false and the dashboard schema is not there - "
    "dashboard.colour_groups does not exist. Run dashboard/sql/01-dashboard-schema.sql, "
    "03-places-view.sql and 02-dashboard-seed.sql against the archive as a user who "
    "may create schemas, or set DASHBOARD_SEED=true to let the dashboard do it."
)


@dataclass
class SeedResult:
    # True when this start created the schema - the moment for one-off work
    # such as suggesting colour groups for the connection types already in
    # the archive.
    first_seed: bool = False
    applied: list[str] = field(default_factory=list)
    present: bool = False


def _present(conn: psycopg.Connection, name: str) -> bool:
    row = conn.execute("SELECT to_regclass(%s) IS NOT NULL AS present", (name,)).fetchone()
    return bool(row and (row["present"] if isinstance(row, dict) else row[0]))


def ensure_dashboard_schema(db: Database, cfg: Config) -> SeedResult:
    result = SeedResult()

    if not cfg.seed:
        with db.read() as conn:
            result.present = _present(conn, "dashboard.colour_groups")
        if not result.present:
            raise SystemExit(MISSING)
        log.info("dashboard schema present (DASHBOARD_SEED=false, nothing run)")
        return result

    files = [SQL_DIR / name for name in SQL_FILES if (SQL_DIR / name).is_file()]
    if not files:
        # The skeleton without its SQL. Not fatal - every view that needs the
        # schema says so on its own - but worth one loud line.
        log.warning("no schema files in %s; the dashboard schema was not created", SQL_DIR)
        return result

    with db.write() as conn:
        # `first_seed` is decided before the files run: afterwards the
        # version table exists whether or not it did before.
        result.first_seed = not _present(conn, "dashboard.schema_version")
        for path in files:
            # No parameters, so psycopg sends the file as one simple query and
            # PostgreSQL accepts the many statements in it.
            conn.execute(path.read_text(encoding="utf-8"))
            result.applied.append(path.name)
        if _present(conn, "dashboard.schema_version"):
            conn.execute(
                "INSERT INTO dashboard.schema_version (integer_version) VALUES (%s) "
                "ON CONFLICT DO NOTHING", (SCHEMA_VERSION,))
        result.present = _present(conn, "dashboard.colour_groups")
        if not result.present:
            raise SystemExit(
                "the schema files ran but dashboard.colour_groups still does not exist - "
                "check dashboard/sql/01-dashboard-schema.sql")

        # THE SUGGESTION RUNS WHEN THERE IS NOTHING TO SUGGEST AGAINST, NOT
        # ONLY ON THE FIRST SEED.
        #
        # `dashboard.colour_group_types` decides the colour of every
        # connection on the Map and the Graph. Written only once - the first
        # time this schema is created - against whatever the archive holds
        # at that moment, an archive whose connection types arrive AFTER the
        # dashboard schema (a scrape that runs later, a schema created
        # against an empty database, a restore) would never get the
        # suggestion at all: 0 rows against tens of thousands of distinct
        # connection types, every one of them resolving to the fallback and
        # the whole colour dimension of the product one brown.
        # `suggest_group()` classifies most of those names.
        #
        # Empty is the only state that re-runs it: one row means somebody -
        # or an earlier run - has decided something, and a decision is never
        # overwritten by a guess (suggest_for_names skips what is assigned).
        if result.first_seed or _nothing_is_grouped(conn):
            _first_seed_hook(conn)

    log.info("dashboard schema %s (%s)",
             "created" if result.first_seed else "checked",
             ", ".join(result.applied))
    return result


def _nothing_is_grouped(conn: psycopg.Connection) -> bool:
    """No connection type has a colour group at all. One cheap EXISTS on a
    small table, asked once per start-up; the walk over the archive's own
    types behind it (colours.DISTINCT_TYPES_SQL) only runs when this is
    true, and writing one row makes it false for good."""
    if not _present(conn, "dashboard.colour_group_types"):
        return False
    row = conn.execute(
        "SELECT NOT EXISTS (SELECT 1 FROM dashboard.colour_group_types) AS empty").fetchone()
    # Written to work with and without dict_row, like _present above.
    return bool(row and (row["empty"] if isinstance(row, dict) else row[0]))


def _first_seed_hook(conn: psycopg.Connection) -> None:
    """Runs once, inside the seeding transaction, the first time the schema is
    created. The colour module (when present) uses it to suggest a group for
    every connection type already in the archive - a suggestion the user can
    see and change, which is why it is stored with text_source='suggested'."""
    try:
        from . import colours  # noqa: WPS433 - optional, written by another part
    except ImportError:
        return
    hook = getattr(colours, "seed_suggested_types", None)
    if hook is None:
        return
    try:
        count = hook(conn)
        if count:
            log.info("%s connection type(s) given a suggested colour group", count)
        else:
            # Worth a line: it means every start-up will ask again, and that
            # the Map and the Graph will draw every connection in the
            # fallback colour until somebody groups a type by hand.
            log.warning("no connection type could be given a suggested colour group; "
                        "connections stay in the fallback colour until they are grouped "
                        "on the Colours page")
    except Exception:
        # A failed suggestion must not stop the dashboard from starting; the
        # types simply fall back to the default group until someone assigns them.
        log.exception("suggesting colour groups failed; types keep the fallback colour")


# THE LISTS THE SUGGESTION BOXES ARE ANSWERED FROM (sql/03-places-view.sql).
#
# Both are materialized views over tables that grow for ever, both are read
# on every keystroke, and both are therefore refreshed on the same timer -
# one list, so another one cannot be added to the SQL and forgotten here.
SUGGESTION_VIEWS = ("dashboard.places", "dashboard.source_hosts",
                    "dashboard.source_names")


def _refresh_view(db: Database, name: str) -> None:
    with db.write() as conn:
        try:
            # CONCURRENTLY so readers are not blocked; it needs the unique
            # index 03-places-view.sql creates and a populated view.
            conn.execute(f"REFRESH MATERIALIZED VIEW CONCURRENTLY {name}")
            return
        except psycopg.Error as exc:
            log.warning("concurrent refresh of %s failed (%s); refreshing plainly",
                        name, str(exc).strip().splitlines()[0])
    with db.write() as conn:
        conn.execute(f"REFRESH MATERIALIZED VIEW {name}")


def refresh_places(db: Database) -> bool:
    """Refresh the lists behind the address and host suggestions. True when a
    refresh happened, False when the views do not exist yet.

    A view that is not there is skipped rather than raised: an installation
    seeded before source_hosts existed keeps its places refreshed, and the
    next seed creates the missing one."""
    with db.write() as conn:
        present = [name for name in SUGGESTION_VIEWS if _present(conn, name)]
    if not present:
        return False
    for name in present:
        _refresh_view(db, name)
    return True
