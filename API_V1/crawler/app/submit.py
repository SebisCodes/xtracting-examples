"""Handing documents to Xtracting.

Per key label, because one key belongs to one project: a batch can never carry
the documents of two keys.

THE MOST DANGEROUS MOMENT in this service is between a successful POST and the
UPDATE that records the jobId. If the process dies in between, the batch is
submitted and paid for while the queue knows nothing about it - it would be
submitted again, and the archive would hold a job nobody can attribute.

It cannot be excluded completely - two systems, no shared transaction. It is
cushioned twice: the write happens immediately after the answer, in its own
short transaction and before any other work; and a 202 without a jobId is
treated as a failure that must NOT be retried, because retrying it would pay
twice for certain.

THE ERROR TRIAGE is the other half of this file, and it matters as much as the
sending. Three groups with three different right answers:

  402, 429       Not a fault but a state: no balance, or too many open tasks.
                 The items stay untouched and go out on the next round. Counting
                 them as an attempt would retire healthy documents after three
                 rounds because an account was empty.
  400, 401, 403, 451
                 The request or the key is wrong. A retry changes nothing.
  5xx, network   Temporary. Try again, with distance.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
import psycopg

from crawlkit.words import plural

from . import db, errorlog, servicelog

log = logging.getLogger(__name__)

#: What a batch carries besides the contents, as JSON overhead.
ENVELOPE_BYTES = 64
PER_TASK_OVERHEAD = 80

#: How long a key rests after the API asked for room. Fifteen minutes is long
#: enough that a queue can drain and short enough that a topped-up account is
#: noticed within one lunch break.
BACKPRESSURE_PAUSE_SECONDS = 15 * 60


# ----------------------------------------------------------------------
# The API
# ----------------------------------------------------------------------

class ApiError(RuntimeError):
    def __init__(self, message: str, *, status: int = 0, code: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.code = code

    @property
    def is_backpressure(self) -> bool:
        """Not a fault but a request for patience.

        402: the balance is used up. 429: too many open tasks. Both go away by
        themselves - one by topping up, the other by processing.
        """
        return self.status in (402, 429)

    @property
    def is_permanent(self) -> bool:
        """A retry changes nothing."""
        return self.status in (400, 401, 403, 451)


@dataclass
class SubmitResult:
    job_id: str
    total_tasks: int
    status: str = "QUEUED"


def explain(status: int) -> str:
    """Plain words for the status codes that really occur.

    >>> explain(402)
    ' (the balance is used up)'
    >>> explain(418)
    ''
    """
    return {
        400: " (the request does not match the schema)",
        401: " (the API key was rejected - check XTRACTING_API_KEYS)",
        402: " (the balance is used up)",
        403: " (key disabled, project inactive or account blocked)",
        413: " (request too large - lower CRAWLER_MAX_REQUEST_BYTES)",
        429: " (too many open tasks - let them finish first)",
        451: " (content refused for legal reasons)",
        503: " (maintenance)",
    }.get(status, "")


class Client:
    """The submitting half of the Xtracting API - one endpoint.

    `POST /api/v1/batch {tasks: [{content, source, tag}]} -> 202`, and the
    answer names a jobId and totalTasks. `totalTasks` can be LARGER than the
    number of documents submitted: the platform splits long content at sentence
    boundaries and bills each part. The crawler does not know the split
    threshold and should not; it records what it was told, and `reconcile`
    accepts `tag-01` as well as `tag`.
    """

    def __init__(self, base_url: str, api_key: str, *, timeout: float = 120.0) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(timeout=timeout, headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "xtracting-crawler/1.0",
        })

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "Client":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def submit_batch(self, tasks: list[dict[str, Any]], *, retries: int = 3,
                     backoff: float = 2.0) -> SubmitResult:
        """Submit one batch.

        Retries only for temporary failures. A 402 or 429 is passed on at once:
        pushing against it makes it worse, and the caller knows better what to
        do about it.
        """
        last: Exception | None = None
        for attempt in range(1, retries + 1):
            try:
                response = self._client.post(f"{self.base_url}/api/v1/batch",
                                             json={"tasks": tasks})
            except httpx.HTTPError as exc:
                last = exc
                log.warning("submitting failed (%d/%d): %s", attempt, retries, exc)
                if attempt < retries:
                    time.sleep(backoff ** attempt)
                continue

            if response.status_code == 202:
                return self._accepted(response, tasks)

            code, message = _error_of(response)
            if response.status_code in (400, 401, 402, 403, 429, 451):
                raise ApiError(message, status=response.status_code, code=code)

            last = ApiError(message, status=response.status_code, code=code)
            log.warning("submitting failed (%d/%d): %s", attempt, retries, message)
            if attempt < retries:
                time.sleep(backoff ** attempt)

        raise ApiError(f"gave up submitting after {retries} attempts: {last}",
                       status=getattr(last, "status", 0))

    def _accepted(self, response: httpx.Response, tasks: list[dict]) -> SubmitResult:
        body = response.json()
        if not body.get("success", False):
            raise ApiError(f"the API reported success=false: "
                           f"{str(body.get('error'))[:300]}", status=202)
        data = body.get("data") or {}
        job_id = data.get("jobId")
        if not job_id:
            # Submitted and billed, and not attributable. The items are set to
            # FAILED rather than retried: a retry would certainly pay twice,
            # and this way a person sees it.
            raise ApiError(
                "202 without a jobId - the batch is submitted but cannot be "
                "attributed. The items are marked FAILED so they are not "
                "submitted and paid for a second time.", status=202)
        return SubmitResult(job_id=str(job_id),
                            total_tasks=int(data.get("totalTasks") or len(tasks)),
                            status=str(data.get("status") or "QUEUED"))


def _error_of(response: httpx.Response) -> tuple[str, str]:
    """A usable message out of an answer.

    The API answers `{"success": false, "error": {"message", "code"}}`. When
    the gateway in front of it fails (413, 502) the body is HTML - then it is
    cut, or thirty lines of markup end up in the log.
    """
    try:
        body = response.json()
        error = body.get("error") or {}
        if isinstance(error, dict):
            code, message = str(error.get("code") or ""), str(error.get("message") or "")
        else:
            code, message = "", str(error)
    except Exception:  # noqa: BLE001
        code, message = "", response.text[:200]
    return code, (f"HTTP {response.status_code}{explain(response.status_code)}: "
                  f"{message or '(no message)'}")


# ----------------------------------------------------------------------
# Batching
# ----------------------------------------------------------------------

@dataclass
class Batch:
    tasks: list[dict[str, Any]] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    bytes_used: int = 0

    def __len__(self) -> int:
        return len(self.tasks)


@dataclass
class Rejected:
    tag: str
    reason: str


def build_batches(rows: list[dict[str, Any]], *, max_tasks: int, max_bytes: int,
                  max_chars: int) -> tuple[list[Batch], list[Rejected]]:
    """Spread queued documents over batches, against four limits at once.

        <= 1000 tasks per batch           (the API's schema)
        <  5 MB per request               (the gateway in front of the API)
        <= 2 million characters per task  (the API's schema)
        source unique within a batch      (our own rule)

    THE LAST ONE IS NOT THE API'S. `reconcile` attributes answers by tag, and
    the platform appends a suffix on a split - two items with the same source
    in one batch would be indistinguishable exactly when it matters. The second
    one is not lost, it goes into the next batch.

    >>> rows = [{"text_tag": "xs_1_a", "text_source_uri": "https://x/1", "content": "x" * 100}]
    >>> batches, rejected = build_batches(rows, max_tasks=10, max_bytes=10_000, max_chars=1000)
    >>> len(batches), len(batches[0]), rejected
    (1, 1, [])

    Content that is too long is REJECTED, not truncated - a shortened document
    would look complete in the archive:

    >>> rows = [{"text_tag": "xs_1_b", "text_source_uri": "https://x/2", "content": "x" * 5000}]
    >>> batches, rejected = build_batches(rows, max_tasks=10, max_bytes=10_000, max_chars=1000)
    >>> batches, rejected[0].reason[:34]
    ([], 'the content is 5000 characters lon')

    The same source twice: the second one goes into the next batch.

    >>> rows = [{"text_tag": "xs_1_c", "text_source_uri": "https://x/3", "content": "a"},
    ...         {"text_tag": "xs_1_d", "text_source_uri": "https://x/3", "content": "b"}]
    >>> [len(batch) for batch in build_batches(rows, max_tasks=10, max_bytes=10_000,
    ...                                        max_chars=1000)[0]]
    [1, 1]
    """
    batches: list[Batch] = []
    rejected: list[Rejected] = []
    current = Batch()
    sources_in_batch: set[str] = set()

    def flush() -> None:
        nonlocal current, sources_in_batch
        if current.tasks:
            batches.append(current)
        current = Batch()
        sources_in_batch = set()

    for row in rows:
        tag = row["text_tag"]
        source = row["text_source_uri"] or ""
        content = row.get("content") or ""

        if not content.strip():
            rejected.append(Rejected(tag, "the content is empty - there is "
                                          "nothing to extract from it"))
            continue
        if len(content) > max_chars:
            rejected.append(Rejected(
                tag, f"the content is {len(content)} characters long, at most "
                     f"{max_chars} are allowed. It is NOT truncated - a "
                     f"shortened document would look complete in the archive."))
            continue

        task = {"content": content, "source": source[:2000], "tag": tag[:255]}
        size = len(json.dumps(task, ensure_ascii=False).encode("utf-8")) + PER_TASK_OVERHEAD

        if size + ENVELOPE_BYTES > max_bytes:
            rejected.append(Rejected(
                tag, f"{size} bytes on its own, and the request limit is "
                     f"{max_bytes}"))
            continue

        needs_flush = (len(current) >= max_tasks
                       or current.bytes_used + size + ENVELOPE_BYTES > max_bytes
                       or source in sources_in_batch)
        if needs_flush and current.tasks:
            flush()

        current.tasks.append(task)
        current.tags.append(tag)
        current.bytes_used += size
        sources_in_batch.add(source)

    flush()
    return batches, rejected


# ----------------------------------------------------------------------
# The queue
# ----------------------------------------------------------------------

#: Which queued rows belong to one key.
#:
#: Three branches, because a row can name its key in three states and all three
#: have to keep working across the upgrade:
#:
#:   1. it names this prefix - somebody pinned this key to the watchlist, the
#:      link group or the address, and that choice is a promise: if the key is
#:      gone the row waits and the crawler says so rather than quietly
#:      submitting it under a key with different settings and a different price
#:   2. it names nothing at all - the default of the project THE ROW IS FOR,
#:      which is the first live extraction key of that project in the order the
#:      keys were written. The row carries its own project (a page may serve
#:      several, and each submission is for exactly one of them), and that is
#:      what keeps the fallback inside the project: a row of a project whose
#:      keys are all gone matches no key and waits, rather than being collected
#:      by another project's key and filed in the wrong archive
#:   3. it names a label and no prefix - queued before the registry had
#:      probed the key, and matched by label until the backfill catches up
_BELONGS_TO_KEY = """
    q.text_key_prefix = %(prefix)s
 OR (q.text_key_prefix = '' AND q.text_key_label = '' AND %(prefix)s = (
        SELECT k.text_prefix
          FROM scraper.api_keys k
         WHERE k.text_project_id = q.text_project_id
           AND k.bool_can_extract
         ORDER BY k.integer_order
         LIMIT 1))
 OR (q.text_key_prefix = '' AND q.text_key_label = %(label)s)
"""


def lease(conn: psycopg.Connection, *, key_label: str, key_prefix: str,
          limit: int, max_attempts: int,
          only_tags: list[str] | None = None) -> list[dict]:
    """Claim open items of this key.

    `FOR UPDATE SKIP LOCKED` in a subquery, then an UPDATE on the picked rows:
    two senders get disjoint sets rather than the same row twice, which would
    mean submitting - and paying - twice.

    `only_tags` narrows it to named items and is what a manual run uses. A
    person pressing "crawl this page and send one" asked for ONE document; the
    backlog that happens to be waiting is not what they agreed to pay for, and
    the ordinary round will take it in its own time.
    """
    rows = db.fetch_all(conn, f"""
        UPDATE scraper.submit_queue q2
           SET text_status      = 'SENDING',
               integer_attempts = q2.integer_attempts + 1,
               date_updated     = NOW()
          FROM (SELECT q.text_tag
                  FROM scraper.submit_queue q
                 WHERE q.text_status = 'PENDING'
                   AND q.integer_attempts < %(max)s
                   AND (%(tags)s::text[] IS NULL OR q.text_tag = ANY(%(tags)s))
                   AND ({_BELONGS_TO_KEY})
                 ORDER BY q.date_added
                 LIMIT %(limit)s
                   FOR UPDATE SKIP LOCKED) picked
         WHERE q2.text_tag = picked.text_tag
        RETURNING q2.text_tag, q2.bigint_fk_source, q2.text_source_uri,
                  q2.date_document_added, q2.bigint_fk_document,
                  q2.integer_char_count, q2.integer_attempts
    """, {"label": key_label, "prefix": key_prefix, "tags": only_tags,
          "limit": limit, "max": max_attempts})
    conn.commit()
    return rows


def recover_stuck(conn: psycopg.Connection, *, older_than_minutes: int = 30) -> int:
    """Put items back that were claimed and never finished.

    The gap this closes: between claiming an item (PENDING -> SENDING) and
    recording its outcome the process can die. Without this the item would stay
    SENDING forever - not failed, not sent, and not visible as either. The
    attempt it already cost is kept, so a document that crashes the sender
    twice still runs out of attempts instead of looping.

    Thirty minutes is longer than any submission can take and short enough that
    a restart clears the backlog within one round.
    """
    recovered = db.execute(conn, """
        UPDATE scraper.submit_queue
           SET text_status = 'PENDING',
               text_error  = 'the sender stopped before this batch was finished '
                             '- it goes out again',
               date_updated = NOW()
         WHERE text_status = 'SENDING'
           AND date_updated < NOW() - make_interval(mins => %(minutes)s)
    """, {"minutes": older_than_minutes})
    conn.commit()
    if recovered:
        log.warning("%d item(s) were left in SENDING by an earlier run and go "
                    "out again", recovered)
    return recovered


def load_content(conn: psycopg.Connection, rows: list[dict]) -> list[dict]:
    """Fetch the text for the claimed items.

    Separate from the claiming because `scraper.documents` is a hypertable: the
    lookup carries its partitioning column (`date_document_added`), so it hits
    one chunk instead of all of them.
    """
    out: list[dict] = []
    for row in rows:
        document = db.fetch_one(conn, """
            SELECT text_content FROM scraper.documents
             WHERE date_added = %s AND bigint_id = %s
        """, (row["date_document_added"], row["bigint_fk_document"]))
        row = dict(row)
        if document is None:
            # Can happen after a retention sweep: the document is gone, the
            # queue item is still there. It cannot be fulfilled any more.
            row["content"] = ""
            row["_missing"] = True
        else:
            row["content"] = document["text_content"]
        out.append(row)
    return out


def finish(conn: psycopg.Connection, tags: list[str], *, status: str,
           error: str = "", job_id: str = "", total_tasks: int | None = None,
           keep_attempt: bool = True) -> None:
    """Record the outcome for a set of items.

    `keep_attempt=False` undoes the attempt. That is for 402 and 429: nothing
    went wrong, there was simply no room. If the counter stayed, an empty
    account would permanently retire healthy documents after three rounds.
    """
    if not tags:
        return
    db.execute(conn, """
        UPDATE scraper.submit_queue
           SET text_status = %(status)s,
               text_job_id = CASE WHEN %(job)s <> '' THEN %(job)s ELSE text_job_id END,
               integer_total_tasks = COALESCE(%(total)s, integer_total_tasks),
               integer_attempts = CASE WHEN %(keep)s THEN integer_attempts
                                       ELSE GREATEST(0, integer_attempts - 1) END,
               date_sent = CASE WHEN %(status)s = 'SENT' THEN NOW() ELSE date_sent END,
               text_error = %(error)s,
               date_updated = NOW()
         WHERE text_tag = ANY(%(tags)s)
    """, {"status": status, "job": job_id, "total": total_tasks,
          "keep": keep_attempt, "error": (error[:2000] or None), "tags": tags})
    conn.commit()


class Submitter:
    """Everything the submitting side has to remember between two ticks.

    Which is exactly one thing: which keys are resting. That is deliberately
    not in the database - a pause is a property of this process's conversation
    with the API, it is over in fifteen minutes, and a restart may perfectly
    well try again straight away.
    """

    def __init__(self, cfg, on_run=None) -> None:
        self.cfg = cfg
        self._paused_until: dict[str, float] = {}
        #: Called with (status, message, label) so main can write a run row.
        self._on_run = on_run
        #: Step logging, set by the loop once per tick (main.Runner). An
        #: attribute rather than an argument because the submitter outlives
        #: the tick - the pause after a 402 is what it is kept for - and the
        #: switch belongs to the pass, not to the object.
        self.steps = False

    def paused(self, label: str) -> bool:
        return time.monotonic() < self._paused_until.get(label, 0.0)

    def pause(self, label: str, seconds: float = BACKPRESSURE_PAUSE_SECONDS) -> None:
        self._paused_until[label] = time.monotonic() + seconds

    def tick(self) -> int:
        """One round over every key. Returns how many documents went out."""
        sent = 0
        with db.connect(self.cfg.database_url) as conn:
            recover_stuck(conn)
        for entry in self.cfg.api_keys:
            if self.paused(entry.label):
                continue
            try:
                sent += self.send_for_key(entry)
            except Exception:  # noqa: BLE001 - one key must not stop the rest
                log.exception("[%s] unhandled error while submitting", entry.label)
        return sent

    def send_now(self, tags: list[str]) -> int:
        """Submit exactly these items, now, over whichever keys own them.

        What a manual run calls the moment its documents are queued: the person
        is watching, and "it is in the queue, the next round will take it" is
        not an answer to "did it go?".
        """
        if not tags:
            return 0
        sent = 0
        for entry in self.cfg.api_keys:
            if self.paused(entry.label):
                continue
            try:
                sent += self.send_for_key(entry, only_tags=tags)
            except Exception:  # noqa: BLE001 - one key must not stop the rest
                log.exception("[%s] unhandled error while submitting", entry.label)
        return sent

    def send_for_key(self, entry, only_tags: list[str] | None = None) -> int:
        cfg = self.cfg
        with db.connect(cfg.database_url) as conn:
            rows = lease(conn, key_label=entry.label, key_prefix=entry.prefix,
                         limit=cfg.max_batch_tasks * 4,
                         max_attempts=cfg.max_attempts, only_tags=only_tags)
            if not rows:
                return 0
            rows = load_content(conn, rows)
            missing = [row["text_tag"] for row in rows if row.get("_missing")]
            if missing:
                finish(conn, missing, status="FAILED",
                       error="the document this item points at no longer exists - "
                             "probably removed by a retention policy")
                for row in (r for r in rows if r.get("_missing")):
                    errorlog.record(
                        conn, kind="SUBMIT_REJECTED", severity="error",
                        source_id=row.get("bigint_fk_source"),
                        uri=row.get("text_source_uri") or "",
                        run_id=f"submit-{entry.label}",
                        message=(
                            "A queued document is gone before it was submitted - "
                            "its text was removed while it waited, so it can "
                            "never be sent. Nothing is lost from the archive; "
                            "the address is crawled again on a later run."),
                        detail=f"tag {row['text_tag']}")
                rows = [row for row in rows if not row.get("_missing")]

        batches, rejected = build_batches(
            rows, max_tasks=cfg.max_batch_tasks, max_bytes=cfg.max_request_bytes,
            max_chars=cfg.max_content_chars)

        if rejected:
            by_tag = {row["text_tag"]: row for row in rows}
            with db.connect(cfg.database_url) as conn:
                for item in rejected:
                    finish(conn, [item.tag], status="FAILED", error=item.reason)
                    # One row per document, not one per round: each of these is
                    # a document that will never reach the archive, and the
                    # reason differs per document. A summary would hide the
                    # sixth one, which is somebody's missing paper.
                    source = by_tag.get(item.tag) or {}
                    errorlog.record(
                        conn, kind="SUBMIT_REJECTED", severity="error",
                        source_id=source.get("bigint_fk_source"),
                        uri=source.get("text_source_uri") or "",
                        run_id=f"submit-{entry.label}",
                        message=(
                            f"A document from this watched page cannot be submitted and "
                            f"was not sent: {item.reason} It is marked failed - "
                            f"nothing is retried, and nothing was truncated."),
                        detail=f"tag {item.tag}")
            log.warning("[%s] %d item(s) cannot be submitted: %s", entry.label,
                        len(rejected), "; ".join(item.reason for item in rejected[:3]))

        sent = 0
        with Client(cfg.api_url, entry.secret,
                    timeout=cfg.request_timeout_seconds) as client:
            for batch in batches:
                try:
                    result = client.submit_batch(batch.tasks,
                                                 retries=cfg.max_attempts)
                except ApiError as exc:
                    stop = self._handle_error(entry, batch, exc)
                    if stop:
                        break
                    continue

                # WRITE IT DOWN AT ONCE. Anything that came in between would
                # risk a paid job nobody can attribute.
                with db.connect(cfg.database_url) as conn:
                    finish(conn, batch.tags, status="SENT", job_id=result.job_id,
                           total_tasks=result.total_tasks)
                    if self.steps:
                        # On the same connection as the SENT, because the
                        # step and the fact it describes belong together:
                        # `record` takes a savepoint, so a debug row that
                        # cannot be written cannot undo a submission that
                        # has already been paid for.
                        servicelog.record(
                            conn, action="submitted a batch",
                            detail=(f"job {result.job_id} - "
                                    f"{plural(len(batch), 'document')} on key "
                                    f"{entry.label!r}, "
                                    f"{plural(result.total_tasks, 'task')} "
                                    f"at the platform"),
                            run_id=f"submit-{entry.label}")
                sent += len(batch)
                split = result.total_tasks - len(batch)
                log.info("[%s] job %s: %d document(s) submitted%s", entry.label,
                         result.job_id, len(batch),
                         f", split by the API into {result.total_tasks} tasks"
                         if split > 0 else "")
        return sent

    def _handle_error(self, entry, batch: Batch, exc: ApiError) -> bool:
        """Put the batch back correctly. Returns True when this key is done
        for this round."""
        with db.connect(self.cfg.database_url) as conn:
            # Every branch below writes the queue's own record; the log row
            # next to it is what the Watched pages view and /logs read, and it says
            # in one sentence which of the four this was.
            def logged(**over) -> None:
                row = errorlog.submit_row(status=exc.status, message=str(exc),
                                          tags=len(batch), key_label=entry.label,
                                          **over)
                errorlog.record(conn, run_id=f"submit-{entry.label}", **row)

            if exc.is_backpressure:
                finish(conn, batch.tags, status="PENDING", error=str(exc),
                       keep_attempt=False)
                self.pause(entry.label)
                log.warning("[%s] %s - %d item(s) wait and go out later; nothing "
                            "is lost", entry.label, exc, len(batch))
                self._run_row("BACKPRESSURE", entry.label, str(exc))
                logged(backpressure=True)
                # The next batch would fail the same way. Stop and come back.
                return True

            if exc.status == 202:
                finish(conn, batch.tags, status="FAILED", error=str(exc))
                log.error("[%s] %s", entry.label, exc)
                self._run_row("ERROR", entry.label, str(exc))
                logged()
                return False

            if exc.is_permanent:
                finish(conn, batch.tags, status="FAILED", error=str(exc))
                logged()
                if exc.status == 401:
                    db.execute(conn, """
                        UPDATE scraper.targets t
                           SET text_key_status = %(status)s, date_updated = NOW()
                          FROM scraper_config.sources s
                         WHERE s.bigint_id = t.bigint_fk_source
                           AND s.text_key_label = %(label)s
                    """, {"status": "the API rejected this key (401)",
                           "label": entry.label})
                    conn.commit()
                log.error("[%s] %s", entry.label, exc)
                self._run_row("ERROR", entry.label, str(exc))
                return False

            # Temporary, and the client has already retried. Back to PENDING;
            # the attempt counter decides when it is enough.
            finish(conn, batch.tags, status="PENDING", error=str(exc))
            log.warning("[%s] %s - trying again later", entry.label, exc)
            logged(temporary=True)
        return False

    def _run_row(self, status: str, label: str, message: str) -> None:
        if self._on_run is not None:
            self._on_run(status, label, message)


__all__ = ["ApiError", "BACKPRESSURE_PAUSE_SECONDS", "Batch", "Client", "Rejected",
           "SubmitResult", "Submitter", "build_batches", "explain", "finish",
           "lease", "load_content", "recover_stuck"]
