"""The Watchlist API: what to watch, and what a site answers to it.

The contract the Watched pages views (templates/sources.html, source_edit.html) are
written against:

    GET    /api/sources?q=               -> {items:[{…source…, robots, schedule,
                                                     last_run, counts, snapshot}],
                                             crawler:{online, seen, worker},
                                             q, total}
    POST   /api/sources                  {…fields, patterns?, exact_urls?, file_rules?}
                                         -> 201 {id}
    GET    /api/sources/{id}             -> {source:{…, patterns, exact_urls, file_rules},
                                             robots, snapshot}
    PUT    /api/sources/{id}             same body as POST -> {id, saved:true}
    DELETE /api/sources/{id}             -> {id, deleted:true}
    POST   /api/sources/{id}/enable      {enabled:true|false} -> {id, enabled}
                                         409 while robots.txt forbids the list page
    POST   /api/sources/test             {source_id?, config?, kind, sample_subpages}
                                         -> 202 {snapshot_id, status}
                                         400 private address - 409 another test runs
    GET    /api/sources/test/{id}        -> {status, progress, steps, result, error}
    POST   /api/sources/learn            {list_url, mode, links:[{url,ticked}],
                                          existing_patterns} -> LearnResult
    GET    /api/sources/{id}/preview     -> the saved configuration applied to the
                                            newest finished test, without fetching
    GET    /api/sources/{id}/runs|documents|files
    GET    /api/export/sources.csv|.json

TWO THINGS THIS FILE REFUSES ON PURPOSE.

`bool_enabled` is not a writable field of the form. Saving a configuration and
starting a crawl are two acts, and the second one is refused while the last
verdict says robots.txt forbids the list page - a 409 with the rule text in
it, not a silent no-op.

An address that resolves to a private network is refused before a snapshot row
exists. The dashboard has no user accounts; without that check, anyone who reaches the
port has an HTTP client into the customer's own network. The flag that lifts
it (`DASHBOARD_ALLOW_PRIVATE_HOSTS`) is for the test suite's stand-in site.

A watchlist names the PROJECTS it collects into (`projects`, one row per
project) and, per project, the one key of it that evaluates the documents
(`text_key_prefix`). There may be several: a project decides what is extracted
from a document, so two projects asking different questions of the same page are
two extractions - the page is fetched once and submitted once per project, and
`scraper.seen_urls` is keyed by project so the second one is not told the
address is old. They are chosen from what the crawler found -
`GET /api/projects/crawler` - because only the crawler has the keys and only a
key can be asked which project it opens. An empty key means the project's
default: the first working extraction key of that project, which is how
collection carries on when one is switched off.

No language here, and the archived count is still counted across all projects: a
watchlist's documents are not split by the language a reader happens to be
viewing, and a count that was would look like documents going missing.
"""

from __future__ import annotations

import csv
import io
import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field

from crawlkit.apply import Rules, decide_all
from crawlkit.learn import learn as learn_links
from crawlkit.netguard import NetGuardError, check_public

from .. import config as app_config
from .. import heartbeats
from .. import sources_store as store
from ..db import Database, get_db
from ..sources_store import SnapshotStore, SourceError
from ..testrunner import TestBusy, get_runner

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["sources"])

#: A list page with more links than this is not a list page, and learning from
#: it would take longer than the editor is willing to wait. The picker never
#: shows more than the snapshot holds (2000).
MAX_LEARN_LINKS = 5000


def _bad(exc: SourceError) -> HTTPException:
    # `status` is 400 for everything the request itself got wrong and 409 for
    # a name the archive already holds - see SourceError in sources_store.py.
    return HTTPException(getattr(exc, "status", 400),
                         {"error": str(exc), "hint": exc.hint})


def _config(request: Request) -> app_config.Config:
    """The running configuration. `app.state.cfg` is what the lifespan loaded;
    the fallback is for a router exercised without one."""
    cfg = getattr(request.app.state, "cfg", None)
    if cfg is not None:
        return cfg
    return app_config.load()


def _runner(request: Request):
    """The one runner of this process, created on the first test."""
    cfg = _config(request)
    return get_runner(SnapshotStore(), timeout_seconds=cfg.test_timeout_seconds)


def _attach_snapshot(db: Database, payload: dict, source_id: int) -> None:
    """Point the draft's test snapshot at the source it has just become.

    Sent by the editor, so it is a number from somewhere else and is checked
    like one; a snapshot that already belongs to a source is left alone (see
    `sources_store.attach_snapshot`).
    """
    raw = payload.get("snapshot_id")
    if raw in (None, "", 0):
        return
    try:
        snapshot_id = int(raw)
    except (TypeError, ValueError):
        raise HTTPException(400, {"error": f"snapshot_id {raw!r} is not a test id",
                                  "hint": "leave it out unless you are saving a "
                                          "configuration that was just tested"})
    store.attach_snapshot(db, snapshot_id, source_id)


def _source_or_404(db: Database, source_id: int) -> dict[str, Any]:
    source = store.get_source(db, source_id)
    if source is None:
        raise HTTPException(404, {"error": f"there is no watched page {source_id}",
                                  "hint": "it may have been deleted in another tab"})
    return source


# ── The list, and one source ────────────────────────────────────────────


@router.get("/sources")
def list_sources(q: str = Query("", description="name, host, address or key label"),
                 db: Database = Depends(get_db)):
    """The overview, optionally narrowed by the view's search box.

    `total` is the number of sources there are, not the number returned:
    "3 of 13" is the only honest thing to put above a filtered list, and
    without the second number an empty answer reads as "you have no
    sources" to somebody who has thirteen.
    """
    term = (q or "").strip()
    items = store.list_sources(db, term)
    total = len(items) if not term else store.count_sources(db)
    return {"items": items, "q": term, "total": total,
            # TWO FACTS ABOUT THE CRAWLER, NOT ONE. `online` is whether one is
            # alive; `paused` is whether scheduled crawling is switched on at
            # all. The overview needs both to say anything useful about a row
            # whose "Crawl on a schedule" is ticked: a tick over a paused
            # watchlist promises something that is not happening, and so does
            # a tick with no crawler behind it - for different reasons and
            # with different fixes.
            "crawler": {**store.crawler_online(db), "paused": _paused(db)}}


@router.post("/sources", status_code=201)
def create_source(payload: dict = Body(...), db: Database = Depends(get_db)):
    try:
        source_id = store.create_source(db, payload)
    except SourceError as exc:
        raise _bad(exc)
    # A draft is usually tested before it is saved; its snapshot belongs to
    # the source from now on, so /preview has something to apply.
    _attach_snapshot(db, payload, source_id)
    return {"id": source_id, "saved": True}


@router.get("/sources/{source_id}")
def get_source(source_id: int, db: Database = Depends(get_db)):
    source = _source_or_404(db, source_id)
    snapshot = store.newest_snapshot(db, source_id, status="", light=True)
    return {
        "source": source,
        "robots": store.robots_verdict(db, source_id),
        # THE SAME FACTS THE LIST CARRIES, and for the row that reads this.
        # The overview redraws one card from here after a test; without the
        # fetch status it fell back to the robots verdict, so a page that had
        # just answered 403 read "robots.txt allows the list page" and only a
        # reload - which goes through the list - told the truth. Any refusal
        # outranks a robots rule that was never the thing that decided.
        "snapshot": None if snapshot is None else {
            "id": snapshot["id"], "date": snapshot["date_added"],
            "status": snapshot["text_status"], "kind": snapshot["text_kind"],
            **store.fetch_facts(snapshot),
        },
    }


@router.put("/sources/{source_id}")
def update_source(source_id: int, payload: dict = Body(...), db: Database = Depends(get_db)):
    try:
        saved = store.update_source(db, source_id, payload)
    except SourceError as exc:
        raise _bad(exc)
    if not saved:
        raise HTTPException(404, {"error": f"there is no watched page {source_id}",
                                  "hint": "it may have been deleted in another tab"})
    _attach_snapshot(db, payload, source_id)
    return {"id": source_id, "saved": True}


@router.delete("/sources/{source_id}")
def delete_source(source_id: int, db: Database = Depends(get_db)):
    if not store.delete_source(db, source_id):
        raise HTTPException(404, {"error": f"there is no watched page {source_id}",
                                  "hint": "it may already be deleted"})
    return {"id": source_id, "deleted": True}


class EnableRequest(BaseModel):
    enabled: bool = True


@router.post("/sources/{source_id}/enable")
def enable_source(source_id: int, body: EnableRequest = EnableRequest(),
                  db: Database = Depends(get_db)):
    """Start or stop the scheduled crawl of a saved source.

    Switching it OFF is always allowed. Switching it on is refused while the
    last verdict says robots.txt forbids the list page and the source still
    respects robots.txt - the reply carries the rule text, because the answer
    is either "ask the site" or "write down why you may anyway".
    """
    source = _source_or_404(db, source_id)
    verdict = store.robots_verdict(db, source_id)
    if body.enabled and source["bool_respect_robots"] and verdict["allowed"] is False:
        raise HTTPException(409, {
            "error": "robots.txt of this site forbids the list page, so the crawl "
                     "cannot be started",
            "hint": (verdict["reason"] or "the site's robots.txt disallows this address")
                    + " - either ask the site for permission and record it in the "
                      "override reason, or point the watched page at an address the site allows.",
        })
    if not store.set_enabled(db, source_id, body.enabled):
        raise HTTPException(404, {"error": f"there is no watched page {source_id}", "hint": ""})
    return {"id": source_id, "enabled": body.enabled}


# ── One real run, by hand ───────────────────────────────────────────────
#
# "Test this configuration" fetches the list page and shows what it found; it
# writes nothing and sends nothing, which is what makes it safe to press. It
# cannot answer the question that comes next: would a document from this page
# actually be accepted, extracted and archived?
#
# THIS ANSWERS THAT, AND IT COSTS. The dashboard writes a request row and the
# crawler carries it out on its next tick - within seconds, and even while the
# scheduled crawl is switched off, because a run somebody just asked for and is
# watching is not the crawler running. The button says the price before it is
# pressed.
#
# THE DASHBOARD DOES NOT CRAWL THIS ITSELF, and that is deliberate: the keys
# live in the crawler's environment and nowhere else, so the one process that
# can submit is the one that does. What comes back here is a row to follow.


class ManualRunRequest(BaseModel):
    #: '' means every project this page is assigned to, which is what the
    #: overview's button asks for. Naming one is how a person tries a single
    #: project without paying for the others.
    project_id: str = ""
    #: One is the point of the thing: content by content, by hand, looked at
    #: before the next one.
    wanted: int = Field(1, ge=1, le=25)


@router.post("/sources/{source_id}/run", status_code=202)
def start_manual_run(source_id: int, body: ManualRunRequest = ManualRunRequest(),
                     db: Database = Depends(get_db)):
    """Ask the crawler to really crawl this page and really submit.

    202, not 200: nothing has happened yet. The row is the receipt, and
    `GET /sources/{id}/run/{run_id}` is where the answer appears.
    """
    source = _source_or_404(db, source_id)
    if not source.get("projects"):
        raise HTTPException(409, {
            "error": "this watched page is not assigned to a project",
            "hint": "A document has to be collected into a project - that is "
                    "what decides how it is read. Choose at least one project "
                    "in step 1 and save, then try again.",
        })
    known = {row["text_project_id"] for row in source["projects"]}
    if body.project_id and body.project_id not in known:
        raise HTTPException(409, {
            "error": "this watched page does not collect into that project",
            "hint": "It collects into " + ", ".join(sorted(known)) + ".",
        })
    # ONE AT A TIME PER PAGE. Two presses of one button are one run: the second
    # would crawl the same list page a second time and pay for the same
    # document twice, which is the one mistake this feature must not make easy.
    with db.write() as conn:
        open_row = conn.execute("""
            SELECT bigint_id FROM scraper_config.manual_runs
             WHERE bigint_fk_source = %s AND text_status IN ('REQUESTED', 'RUNNING')
             ORDER BY date_added LIMIT 1
        """, (source_id,)).fetchone()
        if open_row:
            return {"id": int(open_row["bigint_id"]), "source_id": source_id,
                    "already_running": True}
        row = conn.execute("""
            INSERT INTO scraper_config.manual_runs
                  (bigint_fk_source, text_project_id, integer_wanted)
            VALUES (%s, %s, %s)
            RETURNING bigint_id
        """, (source_id, body.project_id, body.wanted)).fetchone()
    return {"id": int(row["bigint_id"]), "source_id": source_id,
            "already_running": False}


@router.get("/sources/{source_id}/run/{run_id}")
def manual_run_status(source_id: int, run_id: int, db: Database = Depends(get_db)):
    """Where the run got to, and where each of its documents got to.

    THREE STAGES, AND THE PAGE NAMES ALL THREE, because "it did not work" has
    three different answers and three different things to do about it:

      the crawler   did it take the request, crawl the page and queue anything
      the API       was the document accepted for extraction (SENT), or refused
      the archive   has the collector fetched the result back (ARCHIVED)

    The last one would take up to the collector's 15-minute interval to
    answer - but while a run is waiting the collector looks every half
    minute, so this normally
    settles within a minute of the extraction finishing.
    """
    _source_or_404(db, source_id)
    with db.read() as conn:
        run = conn.execute("""
            SELECT bigint_id AS id, date_added, date_started, date_finished,
                   text_status, text_progress, text_error, json_result,
                   text_project_id, integer_wanted
              FROM scraper_config.manual_runs
             WHERE bigint_id = %s AND bigint_fk_source = %s
        """, (run_id, source_id)).fetchone()
        if run is None:
            raise HTTPException(404, {"error": f"there is no run {run_id} of this "
                                               f"watched page", "hint": ""})
        run = dict(run)
        result = run.pop("json_result", None) or {}
        tags = [str(d.get("tag") or "") for d in (result.get("documents") or [])]
        documents = []
        if tags:
            rows = {r["text_tag"]: dict(r) for r in conn.execute("""
                SELECT q.text_tag, q.text_status, q.text_error, q.text_job_id,
                       q.text_source_uri, q.text_project_id, q.date_sent,
                       q.date_archived, q.integer_char_count,
                       t.text_task_id, t.text_name AS task_name,
                       t.text_project AS task_project
                  FROM scraper.submit_queue q
                  LEFT JOIN processed_data.tasks t ON t.text_tag = q.text_tag
                 WHERE q.text_tag = ANY(%s)
            """, (tags,))}
            for item in result.get("documents") or []:
                row = rows.get(str(item.get("tag") or ""), {})
                documents.append({
                    "tag": item.get("tag"),
                    "uri": row.get("text_source_uri") or item.get("uri"),
                    "project_id": row.get("text_project_id") or item.get("project_id"),
                    "chars": row.get("integer_char_count") or item.get("chars"),
                    "queue_status": row.get("text_status") or "GONE",
                    "error": row.get("text_error") or "",
                    "job_id": row.get("text_job_id") or "",
                    "sent": row.get("date_sent"),
                    "archived": row.get("date_archived"),
                    "task_id": row.get("text_task_id") or "",
                    "task_name": row.get("task_name") or "",
                    "in_archive": bool(row.get("text_task_id")),
                })
    return {**run, "documents": documents,
            "counts": result.get("counts") or {},
            "reason": result.get("reason") or "",
            "crawl_status": result.get("status") or ""}


# ── Learning, which touches nothing ─────────────────────────────────────


class LearnLink(BaseModel):
    url: str
    ticked: bool = False
    text: str = ""


class LearnRequest(BaseModel):
    list_url: str
    mode: str = "selected"
    links: list[LearnLink] = Field(default_factory=list)
    ticked: list[str] = Field(default_factory=list)
    existing_patterns: list[dict] = Field(default_factory=list)
    exact_rejects: list[str] = Field(default_factory=list)
    exact_monitors: list[str] = Field(default_factory=list)
    next_urls: list[str] = Field(default_factory=list)
    drop_legal_links: bool = True


@router.post("/sources/learn")
def learn(body: LearnRequest):
    """Patterns from the ticked links. Pure: nothing is fetched, nothing is
    stored, and the answer is the same for the same request. The editor calls
    this while the popup is open, so it has to come back in the time between
    two keystrokes."""
    if len(body.links) > MAX_LEARN_LINKS:
        raise HTTPException(400, {
            "error": f"{len(body.links)} links is more than this can learn from at once",
            "hint": f"send at most {MAX_LEARN_LINKS}; a list page with more than that "
                    "is usually the wrong address"})
    existing = {"patterns": body.existing_patterns,
                "exact_rejects": body.exact_rejects,
                "exact_monitors": body.exact_monitors}
    try:
        result = learn_links(
            body.list_url,
            [{"url": link.url, "ticked": link.ticked} for link in body.links],
            body.ticked, body.mode, existing,
            next_urls=tuple(body.next_urls), drop_legal_links=body.drop_legal_links)
    except ValueError as exc:
        raise HTTPException(400, {"error": str(exc), "hint": ""})
    return result.to_dict()


# ── Testing a configuration ─────────────────────────────────────────────


class TestRequest(BaseModel):
    __test__ = False        # a test crawl, not a pytest suite

    source_id: int | None = None
    config: dict | None = None
    kind: str = "crawl"
    sample_subpages: int = 5


@router.post("/sources/test", status_code=202)
def start_test(body: TestRequest, request: Request, db: Database = Depends(get_db)):
    """Hand a draft to the runner and answer with the id to poll.

    The draft may come from the request (nothing is saved yet - the usual
    case) or, with only a `source_id`, from the stored configuration, which is
    how "test it again" works after a save.
    """
    cfg = _config(request)
    config = body.config
    if config is None:
        if body.source_id is None:
            raise HTTPException(400, {
                "error": "there is nothing to test",
                "hint": "send the draft as `config`, or a `source_id` to test what is saved"})
        config = store.config_of(_source_or_404(db, body.source_id))

    list_url = str(config.get("text_list_url") or config.get("list_url") or "").strip()
    if not list_url:
        raise HTTPException(400, {"error": "this configuration has no list address",
                                  "hint": "fill in the address of the page that lists "
                                          "the documents"})
    for address in [list_url] + _monitor_urls(config):
        try:
            check_public(address, allow_private=cfg.allow_private_hosts)
        except NetGuardError as exc:
            # The sentence is written for the person reading it; the second
            # line says what would have to change, not how to get around it.
            raise HTTPException(400, {
                "error": str(exc),
                "hint": "only addresses on the public internet can be tested from here."})

    try:
        snapshot_id = store.create_snapshot(db, source_id=body.source_id, config=config,
                                            kind=body.kind,
                                            sample_subpages=body.sample_subpages)
    except SourceError as exc:
        raise _bad(exc)

    runner = _runner(request)
    try:
        runner.submit(snapshot_id, allow_private=cfg.allow_private_hosts,
                      timeout_seconds=cfg.test_timeout_seconds)
    except TestBusy as exc:
        # The row is removed again: a RUNNING snapshot nobody runs would show
        # as a test in progress for ever.
        store.delete_snapshot(db, snapshot_id)
        raise HTTPException(409, {
            "error": "another test is running",
            "hint": f"one test runs at a time so the site sees one visitor. Test "
                    f"{exc.snapshot_id} is still going; this one starts when it is done."})
    # Logged because this is the dashboard fetching somebody else's site on
    # request, and the log is the only record of who was visited and when.
    log.info("test %s started: %s (%s, %s)", snapshot_id, list_url, body.kind,
             "draft" if body.source_id is None else f"source {body.source_id}")
    return {"snapshot_id": snapshot_id, "status": "RUNNING"}


def _monitor_urls(config: dict[str, Any]) -> list[str]:
    """The addresses an exact-mode test would fetch itself. They are checked
    against the guard too - the list address being public says nothing about
    where the monitored addresses point."""
    out = []
    for row in config.get("exact_urls") or ():
        if isinstance(row, dict) and row.get("text_kind") == "monitor":
            url = str(row.get("text_url_canonical") or "")
            if url:
                out.append(url)
    return out


@router.get("/sources/test/{snapshot_id}")
def test_status(snapshot_id: int, db: Database = Depends(get_db)):
    """Polled every 1.5 s while a test runs. `progress` is the line to show;
    `result` is filled once the status is DONE."""
    snapshot = store.snapshot(db, snapshot_id)
    if snapshot is None:
        raise HTTPException(404, {"error": f"there is no test {snapshot_id}",
                                  "hint": "start one with POST /api/sources/test"})
    result = snapshot.get("json_result") or {}
    if not isinstance(result, dict):
        result = {}
    # While a test runs - and after one that failed - json_result holds the
    # progress, not a crawl result. Only a DONE snapshot has something the
    # popup can render, and saying so is what keeps the popup from trying.
    finished = snapshot["text_status"] == "DONE"
    return {
        "snapshot_id": snapshot["id"],
        "source_id": snapshot["source_id"],
        "kind": snapshot["text_kind"],
        "status": snapshot["text_status"],
        "progress": str(result.get("progress") or ""),
        "steps": list(result.get("steps") or ()),
        "result": result if finished else None,
        "error": snapshot.get("text_error") or "",
        "started": snapshot.get("date_started"),
        "finished": snapshot.get("date_finished"),
    }


# ── The preview: the saved configuration on the last test ───────────────


@router.get("/sources/{source_id}/preview")
def preview(source_id: int, db: Database = Depends(get_db)):
    """What the saved configuration would do with the links of the newest
    finished test. Nothing is fetched: the answer must be instant, and a
    second crawl to answer "did my change help?" would be a second visit to
    the site for a question the first visit already answered."""
    source = _source_or_404(db, source_id)
    snapshot = store.newest_snapshot(db, source_id, status="DONE")
    if snapshot is None:
        raise HTTPException(404, {
            "error": "there is no finished test for this watched page yet",
            "hint": "press Test this configuration first - the preview applies the "
                    "saved rules to what the test found"})

    result = snapshot.get("json_result") or {}
    links = [str(link.get("url") or "") for link in (result.get("links") or ())
             if isinstance(link, dict) and link.get("url")]
    pages = [p.get("url") if isinstance(p, dict) else str(p)
             for p in (result.get("pages") or ())]
    config = store.config_of(source)
    rules = Rules.from_config(config, next_urls=[p for p in pages if p])

    changed_fields = store.changed_fetch_fields(snapshot.get("json_config") or {}, config)
    decisions = decide_all(links, rules)
    by_reason: dict[str, int] = {}
    for decision in decisions:
        by_reason[decision.by] = by_reason.get(decision.by, 0) + 1
    accepted = [d for d in decisions if d.accepted]
    documents = [d for d in decisions if d.document]
    return {
        "snapshot": {"id": snapshot["id"], "date": snapshot["date_added"],
                     "kind": snapshot["text_kind"]},
        # The test ran against a draft; the address or the engine may have
        # changed since. The page says so rather than showing a preview of
        # links a different question would have found. What the preview
        # re-applies - the mode and the patterns - is not a change.
        "config_changed": bool(changed_fields),
        "changed_fields": changed_fields,
        "counts": {
            "links": len(decisions),
            "accepted": len(accepted),
            "rejected": len(documents) - len(accepted),
            # `rejected` counts documents only, so these two are what is
            # left of `links` - the editor's counter row shows them so that
            # the row adds up without opening the picker.
            "pagination": sum(1 for d in decisions if d.cls == "pagination"),
            "list_self": sum(1 for d in decisions if d.cls == "list_self"),
            "files": sum(1 for d in decisions if d.cls == "file"),
        },
        "by": by_reason,
        "paging_param": result.get("paging_param") or config.get("text_paging_param") or "",
        "links": [{"url": d.url, "class": d.cls, "accepted": d.accepted,
                   "by": d.by, "document": d.document} for d in decisions],
        "warnings": list(result.get("warnings") or ()),
    }


# ── What the crawler did ────────────────────────────────────────────────


def _page(limit: int, offset: int) -> tuple[int, int]:
    return max(1, min(int(limit), store.MAX_PAGE_SIZE)), max(0, int(offset))


@router.get("/sources/{source_id}/runs")
def source_runs(source_id: int, limit: int = store.PAGE_SIZE, offset: int = 0,
                db: Database = Depends(get_db)):
    _source_or_404(db, source_id)
    limit, offset = _page(limit, offset)
    return {"id": source_id, "items": store.runs(db, source_id, limit, offset)}


@router.get("/sources/{source_id}/documents")
def source_documents(source_id: int, limit: int = store.PAGE_SIZE, offset: int = 0,
                     db: Database = Depends(get_db)):
    _source_or_404(db, source_id)
    limit, offset = _page(limit, offset)
    return {"id": source_id, "items": store.documents(db, source_id, limit, offset)}


@router.get("/sources/{source_id}/files")
def source_files(source_id: int, limit: int = store.PAGE_SIZE, offset: int = 0,
                 db: Database = Depends(get_db)):
    _source_or_404(db, source_id)
    limit, offset = _page(limit, offset)
    return {"id": source_id, "items": store.files(db, source_id, limit, offset)}


# ── Export ──────────────────────────────────────────────────────────────
#
# Same shape as the overview, one row per source. No project or language in
# the file name: the list is not a view of one project.

EXPORT_COLUMNS = (
    ("id", lambda r: r["id"]),
    ("name", lambda r: r["text_name"]),
    ("list_url", lambda r: r["text_list_url"]),
    ("host", lambda r: r["text_host"]),
    ("key_label", lambda r: r["text_key_label"]),
    # A page may collect into several projects, and the export has to say all
    # of them: a column holding the first would read as "this is the project"
    # for a page that is extracted twice.
    ("projects", lambda r: "; ".join(
        row.get("text_project_name") or row.get("text_project_id") or ""
        for row in (r.get("projects") or []))),
    ("project_ids", lambda r: "; ".join(
        row.get("text_project_id") or "" for row in (r.get("projects") or []))),
    ("mode", lambda r: r["text_mode"]),
    ("format", lambda r: r["text_format"]),
    ("engine", lambda r: r["text_engine"]),
    ("enabled", lambda r: r["bool_enabled"]),
    ("respects_robots", lambda r: r["bool_respect_robots"]),
    ("robots_allowed", lambda r: r["robots"]["allowed"]),
    ("robots_reason", lambda r: r["robots"]["reason"]),
    ("interval_minutes", lambda r: r["integer_interval_minutes"]),
    ("politeness_seconds", lambda r: r["integer_politeness_seconds"]),
    ("last_run", lambda r: (r["last_run"] or {}).get("date")),
    ("last_status", lambda r: (r["last_run"] or {}).get("status")),
    ("accepted", lambda r: (r["last_run"] or {}).get("accepted")),
    ("new", lambda r: (r["last_run"] or {}).get("new")),
    ("submitted", lambda r: (r["last_run"] or {}).get("submitted")),
    ("pending", lambda r: r["counts"]["pending"]),
    ("sent", lambda r: r["counts"]["sent"]),
    ("archived", lambda r: r["counts"]["archived"]),
    ("accept_patterns", lambda r: r["counts"]["accept_patterns"]),
    ("reject_patterns", lambda r: r["counts"]["reject_patterns"]),
    ("exact_urls", lambda r: r["counts"]["exact_urls"]),
)


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    return str(value)


@router.get("/export/sources.csv")
def export_sources_csv(q: str = Query("", description="the overview's search box"),
                       db: Database = Depends(get_db)):
    # The same `q` the page was showing: an export is a copy of what is on
    # screen, and a file with ten rows the reader had filtered away is worse
    # than no export at all.
    rows = store.list_sources(db, q)
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow([name for name, _ in EXPORT_COLUMNS])
    for row in rows:
        writer.writerow([_cell(get(row)) for _, get in EXPORT_COLUMNS])
    # The byte order mark is what makes Excel read the file as UTF-8; without
    # it every umlaut in a source name arrives broken.
    body = "﻿" + buffer.getvalue()
    return Response(body, media_type="text/csv; charset=utf-8", headers={
        "Content-Disposition": f'attachment; filename="xtracting-sources-{_stamp()}.csv"'})


@router.get("/export/sources.json")
def export_sources_json(q: str = Query("", description="the overview's search box"),
                        db: Database = Depends(get_db)):
    rows = store.list_sources(db, q)
    items = [{name: get(row) for name, get in EXPORT_COLUMNS} for row in rows]
    return Response(
        content=_json_dump({"sources": items, "exported": datetime.now(timezone.utc)}),
        media_type="application/json; charset=utf-8", headers={
            "Content-Disposition":
                f'attachment; filename="xtracting-sources-{_stamp()}.json"'})


def _json_dump(payload: dict[str, Any]) -> str:
    import json

    def default(value: Any) -> str:
        if isinstance(value, datetime):
            return value.isoformat(timespec="seconds")
        return str(value)

    return json.dumps(payload, ensure_ascii=False, indent=1, default=default)


# ── The crawler's own switch ─────────────────────────────────
#
# ONE ROW IN dashboard.settings, AND THE CRAWLER READS IT EVERY TICK
# (crawler/app/db.py: paused). Not an environment variable, because the
# person who needs to stop a crawl in a hurry has a browser open and not a
# shell - and not a per-source flag either, because "stop everything" and
# "stop watching this page" are different acts with different consequences.
#
# OFF IS THE DEFAULT, and that is a decision rather than an oversight. A
# freshly installed archive knows nothing about the sites it is pointed at:
# whether they answer, what their list pages look like, how much a page
# yields. A crawler that starts fetching and submitting the moment it is
# deployed spends money on all of that before anybody has looked at one page.
# So a new installation comes up paused, the watchlist arrives disabled, and
# the way in is the Test button on one row at a time.
CRAWLER_PAUSED = "scraper.paused"
_TRUE = ("1", "true", "yes", "on")


class CrawlerState(BaseModel):
    #: True = crawling and submitting run. The stored row is the opposite of
    #: this, because what the crawler reads is a PAUSE flag; the page says
    #: "on", which is what a person means.
    running: bool


def _paused(db: Database) -> bool:
    with db.read() as conn:
        row = conn.execute(
            "SELECT text_value FROM dashboard.settings WHERE text_key = %s",
            (CRAWLER_PAUSED,)).fetchone()
    # No row means paused, which matches the seed. The crawler's own reading
    # of a missing row is "not paused" so that it can run without the
    # dashboard's schema at all; here the row is always written by the seed,
    # so the two only differ on an archive that has no dashboard schema - and
    # then there is no page to read this from either.
    return not row or str(row["text_value"]).strip().lower() in _TRUE


@router.get("/crawler/state")
def crawler_state(db: Database = Depends(get_db)) -> dict[str, Any]:
    """Whether scheduled crawling is on, and whether a crawler is there to do
    it. The two are different facts: a switch that is on with nothing running
    is a plan, not a state, and the page says so rather than implying that
    pages are being fetched."""
    beat = heartbeats.service(db, "crawler")
    seen = beat["seen"]
    return {
        "running": not _paused(db),
        "worker_seen": seen.isoformat() if seen else None,
        # THE SAME JUDGEMENT THE PILL MAKES, and not a second one derived from
        # the timestamp. `worker_seen` is when a crawler last wrote; whether
        # that counts as "there" is a window (heartbeats.WINDOW_SECONDS), and a
        # page that decided it for itself would disagree with the pill above it
        # the moment the window changed. The row marks are judged from this.
        "online": bool(beat["online"]),
    }


@router.post("/crawler/state")
def set_crawler_state(body: CrawlerState, db: Database = Depends(get_db)) -> dict[str, Any]:
    """Turn scheduled crawling on or off. Takes effect on the crawler's next
    tick - seconds, not minutes - and nothing already in flight is killed:
    a page being fetched finishes, and no new one starts."""
    with db.write() as conn:
        conn.execute("""
            INSERT INTO dashboard.settings (text_key, text_value, date_updated)
            VALUES (%s, %s, NOW())
            ON CONFLICT (text_key) DO UPDATE
               SET text_value = EXCLUDED.text_value, date_updated = NOW()
        """, (CRAWLER_PAUSED, "false" if body.running else "true"))
    return crawler_state(db)
