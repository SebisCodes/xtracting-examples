"""Closing the loop: what has arrived in the archive?

The crawler submits, the platform extracts, the collector archives. Only then
can this service say that a document really made it - and only the tag can say
which submission an archived task belongs to.

    scraper.submit_queue.text_tag  ==  processed_data.tasks.text_tag
                                   OR  text_tag LIKE tag || '-%'

The second half is the auto-split: content the platform cut into parts comes
back as `xs_7_ab12cd34ef56-01`, `-02`. That is why the tag itself carries no
hyphen (a CHECK in the schema enforces it) - otherwise the suffix could not be
told from the tag.

WHAT THIS IS FOR. Without it "submitted" is the last thing anyone knows about a
document, and the Watched pages view can show a count of submissions but not of
results. With it the loop is closed and the overview can say: 40 accepted, 38
new, 38 submitted, 36 archived - and the two missing ones can be looked up.

It also fills the three identity columns in `scraper.documents`. They are empty
when the row is written, because the crawler cannot know which task the
platform will make of it. The UPDATE lands inside the two-day window before
compression; a document the collector takes longer to archive is updated on a
compressed chunk, which works and is merely slower.
"""

from __future__ import annotations

import logging

import psycopg

from . import db

log = logging.getLogger(__name__)


def tick(conn: psycopg.Connection, *, lookback_hours: int = 72) -> int:
    """Mark everything that has arrived. Returns how many were archived.

    `lookback_hours` bounds the work: an item that has been SENT for three days
    without turning up is not going to. It stays SENT and visible rather than
    being asked about forever.
    """
    rows = db.fetch_all(conn, """
        SELECT q.text_tag, q.date_document_added, q.bigint_fk_document,
               t.text_project, t.text_language, t.text_task_id
          FROM scraper.submit_queue q
          JOIN LATERAL (
                SELECT text_project, text_language, text_task_id
                  FROM processed_data.tasks
                 WHERE text_tag = q.text_tag
                    OR text_tag LIKE q.text_tag || '-%%'
                 ORDER BY date_added
                 LIMIT 1
               ) t ON true
         WHERE q.text_status = 'SENT'
           AND q.date_sent > NOW() - make_interval(hours => %(hours)s)
    """, {"hours": lookback_hours})

    if not rows:
        return 0

    for row in rows:
        db.execute(conn, """
            UPDATE scraper.documents
               SET text_project = %(project)s,
                   text_language = %(language)s,
                   text_task_id = %(task)s
             WHERE date_added = %(added)s AND bigint_id = %(id)s
        """, {"project": row["text_project"] or "",
              "language": row["text_language"] or "English",
              "task": row["text_task_id"] or "",
              "added": row["date_document_added"],
              "id": row["bigint_fk_document"]})

    tags = [row["text_tag"] for row in rows]
    db.execute(conn, """
        UPDATE scraper.submit_queue
           SET text_status = 'ARCHIVED', date_archived = NOW(), date_updated = NOW()
         WHERE text_tag = ANY(%s)
    """, (tags,))

    # AND THE ADDRESS IS COLLECTED FOR GOOD.
    #
    # `scraper.seen_urls` is written when the crawler SUBMITS, which is a
    # promise rather than a fact: until the result is in the archive, nobody
    # knows whether it ever will be. Otherwise a collector that is down for
    # an afternoon would cost that afternoon's pages for ever - the crawler
    # having written them off as seen, never to offer them again.
    #
    # This is where the promise is kept. The rows above are exactly the
    # submissions that have just turned up in processed_data.tasks, so their
    # addresses stop being provisional here and nowhere else
    # (crawler/app/store.py: SUBMISSION_GRACE).
    #
    # The join goes through the document because the queue row knows the
    # project and the document knows the address, and the pair is what the
    # table is keyed by.
    db.execute(conn, """
        UPDATE scraper.seen_urls s
           SET date_confirmed = NOW()
          FROM scraper.submit_queue q
          JOIN scraper.documents d
            ON d.date_added = q.date_document_added
           AND d.bigint_id = q.bigint_fk_document
         WHERE q.text_tag = ANY(%s)
           AND s.text_project_id = q.text_project_id
           AND s.text_uri_hash = d.text_uri_hash
    """, (tags,))
    conn.commit()
    log.info("%d submitted document(s) have arrived in the archive", len(rows))
    return len(rows)


def archived_counts(conn: psycopg.Connection, source_id: int) -> int:
    """How many documents of this source are in the archive.

    Over the tag prefix, so it also counts what an older queue row no longer
    knows about: the tag carries the source's id, and the split suffix does not
    get in the way of a LIKE.
    """
    row = db.fetch_one(conn, """
        SELECT count(*) AS n FROM processed_data.tasks
         WHERE text_tag LIKE %s
    """, (f"xs_{source_id}_%",))
    return int((row or {}).get("n") or 0)
