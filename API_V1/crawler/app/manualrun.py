"""One real crawl, on request, by hand - and the submission that follows it.

WHAT THIS IS FOR. "Test this configuration" fetches the list page and shows what
it found; it writes nothing and sends nothing, which is what makes it safe to
press. It cannot answer the question that comes next: would a document from this
page actually be accepted, extracted and archived?

This module is that question. The dashboard writes a `scraper_config.manual_runs`
row; the crawler picks it up on its next tick, crawls the page, takes the first
`integer_wanted` addresses the project has not had yet, stores them, queues them
AND SUBMITS THEM AT ONCE. Then the dashboard follows each tag through
`scraper.submit_queue` and into `processed_data.tasks` and says where it got to.

THE PAUSE IS NOT IGNORED, IT IS ANSWERED. The switch means "do not crawl on a
schedule". It was never meant to mean "and refuse what a person asks for by hand
while watching the screen". A manual run is one page, at one moment, on one
person's press, and the button says what it costs before it is pressed.

NO NEW TRIGGER, AND THAT IS ON PURPOSE. The loop already ticks every five
seconds for the heartbeat, the pause and the configuration sync, so the check
below rides on a round trip that happens anyway - one cheap statement against a
partial index of the rows that are waiting. A doorbell would be a second
mechanism for a delay nobody can feel.

IT SENDS ONLY ITS OWN DOCUMENTS. `Submitter.send_now(tags)` names them, so the
backlog that happens to be waiting is not swept up and paid for as a side effect
of pressing a button that promised one page.
"""

from __future__ import annotations

import dataclasses
import logging
import secrets
import time

import psycopg

from crawlkit import crawl as crawlkit_crawl
from crawlkit.robots import AllowAll, RobotsCache
from crawlkit.words import plural

from . import configsync, db, errorlog, servicelog, store

log = logging.getLogger(__name__)

#: A run that claimed a row and died leaves it RUNNING. Longer than any single
#: page can take, short enough that a restart clears it within one round.
STUCK_AFTER_MINUTES = 30


def claim(conn: psycopg.Connection) -> dict | None:
    """Take the oldest waiting request, or None.

    `FOR UPDATE SKIP LOCKED`, like the queue: two crawlers against one archive
    take different requests rather than the same one twice.
    """
    rows = db.fetch_all(conn, """
        UPDATE scraper_config.manual_runs m
           SET text_status  = 'RUNNING',
               date_started = NOW(),
               date_updated = NOW(),
               text_progress = 'starting'
          FROM (SELECT bigint_id FROM scraper_config.manual_runs
                 WHERE text_status = 'REQUESTED'
                 ORDER BY date_added
                 LIMIT 1
                   FOR UPDATE SKIP LOCKED) picked
         WHERE m.bigint_id = picked.bigint_id
        RETURNING m.bigint_id, m.bigint_fk_source, m.text_project_id,
                  m.integer_wanted
    """)
    conn.commit()
    return rows[0] if rows else None


def reap_stuck(conn: psycopg.Connection) -> int:
    """A run whose crawler died is a failure, not a run that never ends."""
    count = db.execute(conn, """
        UPDATE scraper_config.manual_runs
           SET text_status   = 'FAILED',
               text_error    = 'the crawler stopped while this run was going. '
                               'Nothing was left half-sent: what had already been '
                               'submitted is in the queue and is followed as usual.',
               date_finished = NOW(),
               date_updated  = NOW()
         WHERE text_status = 'RUNNING'
           AND date_started < NOW() - make_interval(mins => %s)
    """, (STUCK_AFTER_MINUTES,))
    conn.commit()
    return count


def _say(conn: psycopg.Connection, run_id: int, message: str) -> None:
    """Where it has got to, in the crawl's own words - the dashboard shows it."""
    db.execute(conn, """
        UPDATE scraper_config.manual_runs
           SET text_progress = %(text)s, date_updated = NOW()
         WHERE bigint_id = %(id)s
    """, {"id": run_id, "text": message[:500]})
    conn.commit()


def _finish(conn: psycopg.Connection, run_id: int, *, status: str,
            result: dict | None = None, error: str = "") -> None:
    import json
    db.execute(conn, """
        UPDATE scraper_config.manual_runs
           SET text_status    = %(status)s,
               json_result    = %(result)s,
               text_error     = %(error)s,
               date_finished  = NOW(),
               date_updated   = NOW()
         WHERE bigint_id = %(id)s
    """, {"id": run_id, "status": status, "error": error[:2000],
          "result": json.dumps(result) if result is not None else None})
    conn.commit()


def tick(cfg, submitter, *, steps: bool = False) -> bool:
    """Carry out at most one waiting request. True if one was taken.

    One per tick on purpose: a manual run is a person watching one page, and
    doing four of them at once would make the progress line meaningless and the
    politeness brake pointless.

    `steps` is the debug switch as the tick that called this read it. Passed
    down rather than read again, so the whole run is in the log or none of it
    is; `--run-now` from a shell has no tick and therefore no steps.
    """
    with db.connect(cfg.database_url) as conn:
        reap_stuck(conn)
        request = claim(conn)
    if request is None:
        return False
    try:
        _execute(cfg, submitter, request, steps=steps)
    except Exception as exc:  # noqa: BLE001 - a bad request must not stop the loop
        log.exception("manual run %s failed", request["bigint_id"])
        with db.connect(cfg.database_url) as conn:
            _finish(conn, request["bigint_id"], status="FAILED",
                    error=f"{type(exc).__name__}: {exc}")
    return True


def _execute(cfg, submitter, request: dict, *, steps: bool = False) -> None:
    run_id = int(request["bigint_id"])
    source_id = int(request["bigint_fk_source"])
    wanted = max(1, int(request["integer_wanted"] or 1))
    only_project = str(request["text_project_id"] or "")

    with db.connect(cfg.database_url) as conn:
        entry = configsync.source_of(conn, source_id)
        if entry is None:
            _finish(conn, run_id, status="FAILED",
                    error="the watched page is gone - it was deleted after this "
                          "run was asked for.")
            return
        source = entry.source
        projects = ([only_project] if only_project else list(source.project_ids))
        if not projects:
            _finish(conn, run_id, status="FAILED",
                    error="this watched page is not assigned to a project, so "
                          "there is nowhere to send a document. Choose at least "
                          "one project in step 1 and save.")
            return
        _say(conn, run_id, f"crawling {source.list_url}")

    # AT MOST `wanted` DOCUMENTS, WHATEVER THE CONFIGURATION SAYS. A watchlist
    # set to 25 per run must not turn one press of a button into 25 paid
    # extractions. Politeness is left alone: the site's own pace is not ours to
    # shorten because a person is waiting.
    source = dataclasses.replace(source, max_new_per_run=wanted)
    run_tag = f"m{run_id}-{secrets.token_hex(4)}"
    sink = store.Store(cfg.database_url, source, key_label=source.key_label,
                       run_id=run_tag, only_project=only_project, steps=steps,
                       on_progress=lambda message: _progress(cfg, run_id, message))
    if steps:
        # AFTER THE TAG IS MADE, not at the claim above: every later row of
        # this run carries `run_tag`, and a first row under a different id
        # would be the one line of the story the reader cannot group with the
        # rest. The claim itself is a moment earlier and nothing happens in
        # between that could go wrong unseen.
        servicelog.record(
            sink.conn, action="claimed a manual run",
            detail=(f"request {run_id} for {source.name} - "
                    f"at most {plural(wanted, 'document')} to "
                    f"{plural(len(projects), 'project')}"),
            run_id=run_tag, source_id=source.id,
            project=only_project)
        sink.conn.commit()
    started = time.monotonic()
    try:
        guard = cfg.guard()
        robots = (RobotsCache(cfg.user_agent, timeout=source.timeout_seconds,
                              guard=guard)
                  if source.respect_robots else AllowAll(cfg.user_agent))
        result = crawlkit_crawl.run(source, sink, False, user_agent=cfg.user_agent,
                                    robots=robots, guard=guard,
                                    deadline=started + cfg.lease_minutes * 60)
    finally:
        queued = list(sink.tags)
        counts = {"queued": sink.queued, "unchanged": sink.unchanged,
                  "duplicates": sink.duplicates}
        sink.close()

    elapsed_ms = int((time.monotonic() - started) * 1000)

    if not queued:
        with db.connect(cfg.database_url) as conn:
            _finish(conn, run_id, status="DONE",
                    result={"documents": [], "counts": counts,
                            "status": result.status, "ms": elapsed_ms,
                            "reason": _nothing_reason(result, counts)})
        log.info("manual run %s: nothing to send (%s)", run_id, counts)
        return

    tags = [item["tag"] for item in queued]
    with db.connect(cfg.database_url) as conn:
        _say(conn, run_id, f"submitting {len(tags)} document(s)")
    sent = submitter.send_now(tags)

    with db.connect(cfg.database_url) as conn:
        rows = {row["text_tag"]: row for row in db.fetch_all(conn, """
            SELECT text_tag, text_status, text_error, text_job_id
              FROM scraper.submit_queue WHERE text_tag = ANY(%s)
        """, (tags,))}
        documents = []
        for item in queued:
            row = rows.get(item["tag"], {})
            documents.append({**item,
                              "status": row.get("text_status", "PENDING"),
                              "job_id": row.get("text_job_id", ""),
                              "error": row.get("text_error") or ""})
        _finish(conn, run_id, status="DONE",
                result={"documents": documents, "counts": counts,
                        "status": result.status, "ms": elapsed_ms, "sent": sent})
        # A RUN ROW, LIKE EVERY OTHER CRAWL, AND ONE LOG ROW OF ITS OWN KIND.
        # The run table is where somebody looks a day later, and a run that
        # cost money belongs in it whether or not anybody was watching the
        # screen. The Log page's Problems tab gets the same sentence as a
        # `MANUAL_RUN` row - the one kind there that is not a failure - so
        # a person reading the log sees the hand-made run between the
        # things that went wrong, with severity info, which the problem
        # count leaves out.
        message = manual_run_message(documents)
        store.record_run(
            conn, source_id=source.id, name=source.name,
            key_label=source.key_label, status=result.status,
            counts={**result.counts, **counts}, ms=elapsed_ms,
            message=message)
        errorlog.record(
            conn, source_id=source.id, source_name=source.name,
            host=source.host, run_id=run_tag, project=only_project,
            **errorlog.manual_run_row(message=message, uri=source.list_url))
    log.info("manual run %s: %d document(s) submitted in %d ms",
             run_id, sent, elapsed_ms)


def manual_run_message(documents: list[dict]) -> str:
    """The sentence the run row carries, so a reader can tell it from a
    scheduled run of the same page.

    >>> manual_run_message([{"project_id": "p1", "tag": "xs_1_a", "uri": "https://a.example/1"},
    ...                     {"project_id": "p2", "tag": "xs_1_b", "uri": "https://a.example/1"}])
    'manual run: 2 documents submitted to 2 projects - xs_1_a https://a.example/1; xs_1_b https://a.example/1'
    >>> manual_run_message([])
    'manual run: 0 documents submitted to 0 projects'
    """
    projects = {d["project_id"] for d in documents}
    head = (f"manual run: {plural(len(documents), 'document')} submitted to "
            f"{plural(len(projects), 'project')}")
    listed = "; ".join(f"{d['tag']} {d['uri']}" for d in documents[:5])
    return f"{head} - {listed}" if listed else head


def _progress(cfg, run_id: int, message: str) -> None:
    """The crawl's own progress line, written through on its own connection.

    Best effort: a run that cannot say where it is should still finish.
    """
    try:
        with db.connect(cfg.database_url) as conn:
            _say(conn, run_id, message)
    except psycopg.Error:
        log.debug("could not record progress for manual run %s", run_id)


def _nothing_reason(result, counts: dict) -> str:
    """Why a run that worked sent nothing - in the words the reader needs.

    "0 documents" with no sentence is the worst outcome this feature can
    produce: it looks like a fault and is usually the system working.

    The verdicts are `crawlkit.crawl.run()`'s: SKIPPED is robots.txt, BLOCKED
    is the site's answer (4xx, 5xx or a browser check), ERROR is everything
    that stopped the run before it had a page. Each gets its own sentence, so
    a forbidden page is not reported as "no link counts as a document".

    >>> from crawlkit.crawl import CrawlResult
    >>> _nothing_reason(CrawlResult(status="SKIPPED"), {})[:31]
    'robots.txt forbids this page, s'
    >>> _nothing_reason(CrawlResult(status="BLOCKED", error="HTTP 403 for the list page."), {})
    "HTTP 403 for the list page. Nothing was fetched from it; the Log page has the site's answer."
    >>> _nothing_reason(CrawlResult(status="ERROR", error="the site did not answer in time."), {})
    'the site did not answer in time.'
    >>> _nothing_reason(CrawlResult(status="OK", counts={"accepted": 0}), {})[:34]
    'no link on this page counts as a d'
    >>> _nothing_reason(CrawlResult(status="OK", counts={"accepted": 3}), {"duplicates": 3})[:41]
    'the addresses on this page are already in'
    >>> _nothing_reason(CrawlResult(status="OK", counts={"accepted": 3}), {"unchanged": 3})[:39]
    'every address on this page has already '

    THE ORDER OF THE QUESTIONS IS THE ORDER OF THE CRAWL, and the count that
    answers each of them lives in a different place. Whether any link COUNTS is
    the crawl's own `accepted`; whether a counted one was NEW is the gate in
    store.unseen(), which runs before a document is ever built and therefore
    leaves the sink's own counters at zero - reading only those would say "no
    link counts as a document" about a page where twenty do and all twenty are
    already collected, which is the opposite of what happened.
    """
    if result.status == "SKIPPED":
        return ("robots.txt forbids this page, so nothing was fetched. The "
                "verdict is on the watched page under the site's own rules.")
    if result.status == "BLOCKED":
        return (result.error or "the site refused the list page.") + (
            " Nothing was fetched from it; the Log page has the site's answer.")
    if result.status == "ERROR":
        return result.error or "the site did not answer."
    if not result.counts.get("accepted"):
        return ("no link on this page counts as a document yet. Open "
                "“Choose the links” and tick what is worth collecting.")
    if counts.get("duplicates"):
        return ("the addresses on this page are already in the archive under "
                "this project, so they were not sent again. Nothing was charged.")
    return ("every address on this page has already been collected for this "
            "project. Nothing was sent, and nothing was charged - a page that "
            "adds a new document will send that one.")


__all__ = ["STUCK_AFTER_MINUTES", "claim", "reap_stuck", "tick"]
