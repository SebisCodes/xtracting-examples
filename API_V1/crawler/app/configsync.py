"""From the dashboard's configuration to the crawler's schedule.

`scraper_config.*` is what a person edits; `scraper.targets` is what the
scheduler works through. This module is the one-way street between them, and
it is one-way on purpose: the crawler never writes into the configuration, so
"who changed this source?" always has one answer.

WHAT MAKES A TARGET DUE. Every source carries a hash over the fields that
change what a crawl does (`Source.config_hash`). When that hash moves, the
target is set due immediately - somebody has just changed the rules and wants
to see the effect, and waiting six hours to show it is how a person concludes
that the switch did nothing. A rename does not move the hash, so fixing a typo
does not send a request the site did not need to answer.

WHY THE SYNC IS DRIVEN BY max(date_updated). Reading every source on every
tick would be a query per five seconds for a table that changes twice a week.
The maximum of one indexed column is the cheapest possible way to ask "has
anything changed?", and the answer is almost always no.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

import psycopg

from crawlkit.crawl import Source

from . import db, errorlog

log = logging.getLogger(__name__)


@dataclass
class ConfiguredSource:
    """One row of `scraper_config.sources` with everything that belongs to it."""

    config: dict
    source: Source
    enabled: bool
    interval_minutes: int
    updated: datetime | None = None

    @property
    def id(self) -> int:
        return self.source.id

    @property
    def name(self) -> str:
        return self.source.name


def latest_change(conn: psycopg.Connection) -> datetime | None:
    row = db.fetch_one(conn, "SELECT max(date_updated) AS latest "
                             "FROM scraper_config.sources")
    return (row or {}).get("latest")


def load_sources(conn: psycopg.Connection, *, source_id: int | None = None,
                 only_enabled: bool = False) -> list[ConfiguredSource]:
    """Every source with its projects, patterns, exact addresses and file rules.

    Five statements, not five per source: a customer with two hundred sources
    would otherwise pay a thousand round trips for a table that fits in
    memory twice over.
    """
    where = []
    params: dict = {}
    if source_id is not None:
        where.append("bigint_id = %(id)s")
        params["id"] = source_id
    if only_enabled:
        where.append("bool_enabled")
    clause = (" WHERE " + " AND ".join(where)) if where else ""

    rows = db.fetch_all(conn, f"SELECT * FROM scraper_config.sources{clause} "
                              f"ORDER BY bigint_id", params)
    if not rows:
        return []
    ids = [row["bigint_id"] for row in rows]

    patterns = db.fetch_all(conn, """
        SELECT * FROM scraper_config.source_patterns
         WHERE bigint_fk_source = ANY(%s) ORDER BY bigint_id
    """, (ids,))
    exact = db.fetch_all(conn, """
        SELECT * FROM scraper_config.source_exact_urls
         WHERE bigint_fk_source = ANY(%s) ORDER BY bigint_id
    """, (ids,))
    files = db.fetch_all(conn, """
        SELECT * FROM scraper_config.source_file_rules
         WHERE bigint_fk_source = ANY(%s) ORDER BY bigint_id
    """, (ids,))
    # In the order they were chosen. The first one is the project the log
    # names when it has room for one name only.
    projects = db.fetch_all(conn, """
        SELECT * FROM scraper_config.source_projects
         WHERE bigint_fk_source = ANY(%s)
         ORDER BY integer_order, text_project_id
    """, (ids,))

    out: list[ConfiguredSource] = []
    for row in rows:
        config = dict(row)
        config["patterns"] = [dict(p) for p in patterns
                              if p["bigint_fk_source"] == row["bigint_id"]]
        config["exact_urls"] = [dict(e) for e in exact
                                if e["bigint_fk_source"] == row["bigint_id"]]
        config["file_rules"] = [dict(f) for f in files
                                if f["bigint_fk_source"] == row["bigint_id"]]
        config["projects"] = [dict(pr) for pr in projects
                              if pr["bigint_fk_source"] == row["bigint_id"]]
        out.append(ConfiguredSource(
            config=config, source=Source.from_config(config),
            enabled=bool(row["bool_enabled"]),
            interval_minutes=int(row["integer_interval_minutes"]),
            updated=row.get("date_updated")))
    return out


def sync_targets(conn: psycopg.Connection, sources: list[ConfiguredSource],
                 known_labels: list[str]) -> dict[str, int]:
    """Make `scraper.targets` match the configuration. Returns what changed.

    Three cases, and the middle one is the interesting one:

      new       a target due now - a source someone just enabled should run on
                the next tick, not in six hours.
      changed   the configuration hash moved: due now again, for the same
                reason.
      gone      the source was deleted or disabled. The target row goes; the
                documents and the queue stay, because they are what happened,
                not what is planned.
    """
    counts = {"new": 0, "changed": 0, "gone": 0, "unchanged": 0}
    enabled = [entry for entry in sources if entry.enabled]
    wanted = {entry.id: entry for entry in enabled}

    # text_key_status comes along because two decisions below rest on it:
    # whether the target row has to be touched at all, and whether the log has
    # already been told about a key label nothing answers to.
    existing = {row["bigint_fk_source"]: row for row in db.fetch_all(
        conn, "SELECT bigint_fk_source, text_config_hash, text_key_status "
              "FROM scraper.targets")}

    for source_id, entry in wanted.items():
        config_hash = entry.source.config_hash()
        key_status = ("" if not entry.source.key_label
                      or entry.source.key_label in known_labels
                      else f"unknown key label {entry.source.key_label!r}")
        row = existing.get(source_id)
        # A label nothing answers to means this source will crawl and then
        # never submit. It is logged when it APPEARS - on the target's first
        # sync or when it changes - and not on every tick: the sync runs
        # whenever anything in the configuration moves, and a row per tick
        # would bury the log under one mistake.
        if key_status and key_status != (row or {}).get("text_key_status", ""):
            errorlog.record(
                conn, source_id=source_id, source_name=entry.name,
                host=entry.source.host, run_id=f"sync-{source_id}",
                **errorlog.key_row(key_label=entry.source.key_label,
                                   source_name=entry.name, known=known_labels))
        if row is None:
            db.execute(conn, """
                INSERT INTO scraper.targets
                      (bigint_fk_source, text_config_hash, date_next_run,
                       text_key_status)
                VALUES (%(id)s, %(hash)s, NOW(), %(key)s)
                ON CONFLICT (bigint_fk_source) DO UPDATE
                   SET text_config_hash = EXCLUDED.text_config_hash,
                       date_next_run    = NOW(),
                       text_key_status  = EXCLUDED.text_key_status,
                       date_updated     = NOW()
            """, {"id": source_id, "hash": config_hash, "key": key_status})
            counts["new"] += 1
        elif row["text_config_hash"] != config_hash:
            db.execute(conn, """
                UPDATE scraper.targets
                   SET text_config_hash = %(hash)s,
                       date_next_run    = NOW(),
                       -- A changed configuration is a fresh start: the three
                       -- failures the previous one collected say nothing about
                       -- the new one, and leaving them would keep the new
                       -- configuration in a sixteen-fold backoff nobody asked
                       -- for.
                       integer_consecutive_failures = 0,
                       text_key_status  = %(key)s,
                       date_updated     = NOW()
                 WHERE bigint_fk_source = %(id)s
            """, {"id": source_id, "hash": config_hash, "key": key_status})
            counts["changed"] += 1
        else:
            if row.get("text_key_status", "") != key_status:
                db.execute(conn, """
                    UPDATE scraper.targets SET text_key_status = %(key)s,
                           date_updated = NOW()
                     WHERE bigint_fk_source = %(id)s
                """, {"id": source_id, "key": key_status})
            counts["unchanged"] += 1

    stale = [source_id for source_id in existing if source_id not in wanted]
    if stale:
        counts["gone"] = db.execute(conn, """
            DELETE FROM scraper.targets WHERE bigint_fk_source = ANY(%s)
        """, (stale,))
    conn.commit()

    if counts["new"] or counts["changed"] or counts["gone"]:
        log.info("configuration synced: %d new, %d changed, %d removed, %d unchanged",
                 counts["new"], counts["changed"], counts["gone"], counts["unchanged"])
    return counts


def source_of(conn: psycopg.Connection, source_id: int) -> ConfiguredSource | None:
    """One source, for a run that is about to start or for `--test`."""
    found = load_sources(conn, source_id=source_id)
    return found[0] if found else None
