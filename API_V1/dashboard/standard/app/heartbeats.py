"""Which services are alive - one table, one threshold, three readers.

`monitoring.heartbeat` holds one row per service, overwritten once a minute by
whoever it belongs to: the crawler from its tick, the collector from its sleep
slices. Nothing else writes there and nothing here writes at all.

WHY THIS IS A MODULE OF ITS OWN. The threshold below decides whether a pill is
green or red, and two places ask it - the Watchlist's `crawler_online()` and
the crawler-state endpoint beside it. Two copies of the SQL would be one
drift away from a page that says a service
is running in one corner and stopped in another. It is also read from a router
that must load on an installation without `crawlkit`, which the Watchlist's own
store cannot promise, so the shared answer lives where nothing heavy imports it.

WHY THREE MINUTES. It is three heartbeats: one write may be lost to a database
that was briefly away, and a service that is restarting takes a few seconds to
say anything at all. Shorter and the pill blinks red at a service that is
merely busy; longer and it stays green through a crash for the whole time
somebody is asking why nothing is arriving.
"""

from __future__ import annotations

from typing import Any

from .db import Database

#: A row younger than this means "running". Written once, read everywhere.
WINDOW_SECONDS = 180

#: The services a reader expects to be told about, in the order they work in:
#: the crawler fetches the pages, the collector brings the results back. A
#: service that has never written a row is still listed - "never seen" is the
#: answer to the question, and a widget that simply leaves it out looks like a
#: widget that is still loading.
SERVICES: tuple[str, ...] = ("crawler", "collector")


def read(db: Database, within_seconds: int = WINDOW_SECONDS) -> list[dict[str, Any]]:
    """Every service, whether or not it has ever been heard from.

    `online` is computed by the DATABASE, not here: the archive's clock is the
    one both writers stamp with, and a dashboard container whose clock is a
    minute out would otherwise report a healthy service as dead.
    """
    with db.read() as conn:
        rows = conn.execute("""
            SELECT text_service AS service, date_seen AS seen,
                   text_version AS version,
                   (NOW() - date_seen) < make_interval(secs => %s) AS online
              FROM monitoring.heartbeat""", (within_seconds,)).fetchall()

    seen = {r["service"]: dict(r) for r in rows}
    out = [seen.pop(name, None) or _never(name) for name in SERVICES]
    # Anything else the archive holds a row for - a service this dashboard is
    # older than - is listed after the two it knows, rather than hidden.
    out.extend(seen[name] for name in sorted(seen))
    return [{**row, "online": bool(row["online"])} for row in out]


def service(db: Database, name: str,
            within_seconds: int = WINDOW_SECONDS) -> dict[str, Any]:
    """One service, in the same shape. For the two readers that ask about the
    crawler alone."""
    for row in read(db, within_seconds):
        if row["service"] == name:
            return row
    return _never(name)


def _never(name: str) -> dict[str, Any]:
    return {"service": name, "seen": None, "version": "", "online": False}
