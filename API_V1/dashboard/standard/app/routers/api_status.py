"""Are the two services alive? One endpoint, read by every page.

It belongs to no view, which is why it has a router of its own rather than a
corner of the Watchlist's: the status widgets sit beside the heading of the
Dashboard, the Diagrams, the Log and every other page, and a page that has
nothing to do with crawling should not have to load the crawler's router to
find out whether anything is running.

Deliberately tiny and deliberately cheap: two rows out of a two-row table, no
project, no language, no cache. It is asked once a page and then every half
minute by a timer, on a table that is never longer than the number of services
this product has.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from .. import heartbeats
from ..db import Database, get_db

router = APIRouter(prefix="/api", tags=["status"])


@router.get("/heartbeats")
def service_heartbeats(db: Database = Depends(get_db)) -> dict[str, Any]:
    """Every service, when it was last heard from, and whether that counts as
    running.

    `online` is the archive's own verdict rather than a comparison made here,
    and `within_seconds` is published beside it so the page can SAY what
    "offline" means instead of leaving a red dot to be guessed at.
    """
    services = [
        {"service": row["service"],
         "seen": row["seen"].isoformat() if row["seen"] else None,
         "version": row["version"] or "",
         "online": row["online"]}
        for row in heartbeats.read(db)
    ]
    return {"services": services, "within_seconds": heartbeats.WINDOW_SECONDS}
