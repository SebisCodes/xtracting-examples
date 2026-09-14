"""The Xtracting projects the crawler found, and the keys of each.

    GET    /api/projects/crawler        -> {items:[{project_id, name, keys:[…],
                                                    last_seen, old, detailed,
                                                    watchlists}], old_after_days}
    DELETE /api/projects/crawler/{id}   -> {project_id, deleted:true, watchlists:n}

Read-only from `crawler.*`, which the crawler owns and writes: this is the one
place the dashboard can learn which project a key opens, because only the
crawler holds the keys and only a key can be asked.

WHY DELETE IS HERE AND NOTHING ELSE IS. A project nothing has used for seven
days is one a person may want gone, together with the watchlists and rules that
configured it. That is the customer's act, not the crawler's, and it is the one
write the dashboard is granted on `scraper.projects` (database/init/04-roles.sh).

What the delete does NOT touch is the point of it: `processed_data` keeps every
document, and the project selector at the top of every view keeps offering the
project, because that list is built from `processed_data.tasks.text_project`
(app/context.py) and not from this table. Configuration is disposable; what was
collected is not, and a person deleting a stale configuration must not discover
afterwards that they deleted a year of data.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from ..db import Database, get_db

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/projects", tags=["projects"])

#: Mirrors crawler/app/keys.py. Repeated rather than imported: the dashboard
#: does not import the crawler, and the number is shown to a person here.
OLD_AFTER_DAYS = 7

_PROJECTS = """
    SELECT p.text_project_id       AS project_id,
           p.text_name             AS name,
           p.date_first_seen       AS first_seen,
           p.date_last_seen        AS last_seen,
           p.bool_keys_detailed    AS detailed,
           p.json_defaults         AS defaults,
           p.date_last_seen < NOW() - make_interval(days => %(old)s) AS old,
           (SELECT count(*) FROM scraper_config.source_projects sp
             WHERE sp.text_project_id = p.text_project_id) AS watchlists
      FROM scraper.projects p
     ORDER BY p.text_name, p.text_project_id
"""

_KEYS = """
    SELECT text_prefix           AS prefix,
           text_label            AS label,
           text_project_id       AS project_id,
           text_name             AS name,
           bool_can_extract      AS can_extract,
           bool_can_read_project AS can_read_project,
           bool_double_check     AS double_check,
           bool_high_thinking    AS high_thinking,
           integer_effort        AS effort,
           text_translations     AS translations,
           integer_order         AS position,
           date_seen             AS seen
      FROM scraper.api_keys
     ORDER BY text_project_id, integer_order
"""

#: What the four effort steps are called on screen.
#:
#: "cheapest" and "most expensive" were the first words for these and they are
#: misleading: the price of a document depends on its length as much as on the
#: switches, so a person comparing two keys by those names would be comparing
#: the wrong thing. Effort is what actually differs.
EFFORT_NAMES = {
    0: "unknown",
    1: "lowest effort",
    2: "more thinking",
    3: "double-checked",
    4: "highest effort",
}


def _key_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "prefix": row["prefix"],
        "label": row["label"],
        # The name Xtracting knows it by, when some key of the project could
        # read the project. Falls back to the local nickname, which is always
        # there, so the chooser never has a blank line in it.
        "name": row["name"] or row["label"] or row["prefix"],
        "can_extract": row["can_extract"],
        "can_read_project": row["can_read_project"],
        "double_check": row["double_check"],
        "high_thinking": row["high_thinking"],
        "effort": row["effort"],
        "effort_name": EFFORT_NAMES.get(row["effort"] or 0, "unknown"),
        "translations": row["translations"],
        "position": row["position"],
        "seen": row["seen"].isoformat() if row["seen"] else None,
    }


@router.get("/crawler")
def crawler_projects(db: Database = Depends(get_db)) -> dict[str, Any]:
    """Every project the crawler has a key for, with those keys.

    Keys keep the order they were written in the environment, because that
    order is what "the project's default key" means: the first of them that is
    live and may extract.
    """
    with db.read() as conn:
        projects = [dict(r) for r in conn.execute(_PROJECTS, {"old": OLD_AFTER_DAYS}).fetchall()]
        keys = [dict(r) for r in conn.execute(_KEYS).fetchall()]

    by_project: dict[str, list[dict]] = {}
    for row in keys:
        by_project.setdefault(row["project_id"], []).append(_key_row(row))

    items = []
    for project in projects:
        mine = by_project.get(project["project_id"], [])
        # Only keys that may extract are offered as a choice: a key that cannot
        # submit cannot evaluate a page, and offering it would be offering a
        # watchlist that collects nothing.
        extraction = [k for k in mine if k["can_extract"]]
        items.append({
            "project_id": project["project_id"],
            "name": project["name"] or project["project_id"],
            "first_seen": project["first_seen"].isoformat() if project["first_seen"] else None,
            "last_seen": project["last_seen"].isoformat() if project["last_seen"] else None,
            "old": bool(project["old"]),
            # Whether the effort of each key is known, which decides whether the
            # editor may offer "lowest effort" and "highest effort" at all.
            "detailed": bool(project["detailed"]),
            "defaults": project["defaults"],
            "watchlists": project["watchlists"],
            "keys": extraction,
            "keys_total": len(mine),
            # The key a watchlist of this project uses when it names none.
            "default_key": extraction[0]["prefix"] if extraction else None,
        })

    return {"items": items, "old_after_days": OLD_AFTER_DAYS}


@router.delete("/crawler/{project_id}")
def delete_crawler_project(project_id: str, db: Database = Depends(get_db)) -> dict[str, Any]:
    """Remove a project's configuration. The archive is untouched.

    The watchlists go first and by hand rather than by a cascade, because there
    is no foreign key across the boundary: `scraper_config` is the dashboard's
    to write and `crawler` is the crawler's, and a constraint between them would
    stop the dashboard deleting a watchlist while the crawler is mid-run.
    """
    with db.write() as conn:
        found = conn.execute(
            "SELECT text_name FROM scraper.projects WHERE text_project_id = %s",
            (project_id,)).fetchone()
        if not found:
            raise HTTPException(404, "no such project")

        # A PAGE MAY READ FOR SEVERAL PROJECTS, so deleting one project takes
        # its assignment off every page and then deletes only the pages THIS
        # project was on that now have none left. A watchlist that also
        # collects for two other projects is not this project's to remove -
        # and neither is one that never had a project at all, which is what a
        # bare "delete every source with no project" would have swept up.
        # TWO STATEMENTS, NOT ONE CTE. Every branch of a data-modifying CTE
        # reads the same snapshot, so a `NOT EXISTS` in the second half would
        # still see the assignment the first half had just deleted, and no page
        # would ever be found empty. Written as two, the second sees the first.
        touched = [row["bigint_fk_source"] for row in conn.execute("""
            DELETE FROM scraper_config.source_projects WHERE text_project_id = %s
            RETURNING bigint_fk_source
        """, (project_id,))]
        removed = len(touched)
        orphans = 0
        if touched:
            orphans = conn.execute("""
                DELETE FROM scraper_config.sources s
                 WHERE s.bigint_id = ANY(%s)
                   AND NOT EXISTS (SELECT 1 FROM scraper_config.source_projects sp
                                    WHERE sp.bigint_fk_source = s.bigint_id)
            """, (touched,)).rowcount
        conn.execute("DELETE FROM scraper.projects WHERE text_project_id = %s",
                     (project_id,))

    log.info("project %s deleted: taken off %d watchlist(s), %d of which had no "
             "other project and were removed; processed_data untouched",
             project_id, removed, orphans)
    return {"project_id": project_id, "deleted": True, "watchlists": removed,
            "removed_watchlists": orphans}
