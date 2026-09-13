"""Where a crawl's results become rows.

This is the sink `crawlkit.crawl.run()` writes through. The dashboard passes a
sink that keeps nothing; the crawler passes this one. Same crawl, two endings.

THE MOST IMPORTANT PLACE IS `document()`: the document, the seen_urls entry and
the queue item are written in ONE transaction. A document without a queue item
would be silently lost - it would sit in the database, never be submitted, and
nothing would point at it. A queue item without a document would be an order
with no content.

THE SECOND MOST IMPORTANT IS THE DEDUPE, and it rests on one decision:

    THE ADDRESS IS THE ANCHOR, NOT THE CONTENT.

Checking (address, content hash) sounds more precise and is wrong in
production: a page with a date, a visitor counter or rotating adverts has a
different hash on every fetch. The check would see a new document every time,
and the same page would be submitted - and billed - on every run. A loop that
does not stop by itself and that looks like diligence from the outside.

`bool_resubmit_on_change` turns the content check back on per source. For
addresses that are watched precisely because they change (exact mode) that is
the point; everywhere else the safe answer is the default.

THE THIRD IS THAT THE ANCHOR IS PER PROJECT.

    THE PAGE IS FETCHED ONCE AND SUBMITTED ONCE PER PROJECT.

A project decides what is extracted from a document - its objects of interest
and its perspectives - so two projects asking different questions of the same
page are two extractions and two archived documents. "Have I had this address
before" is therefore a question about a project: a page the patents project
collected an hour ago is new to the companies project, and the gate that
answers globally would silently give the second one nothing.

The site sees one visitor either way. What doubles is the extraction, and the
editor says so before a second project is added.
"""

from __future__ import annotations

import logging
import secrets
from typing import Iterable

import psycopg

from crawlkit.crawl import CrawlSink, Document, FileRecord, Source
from crawlkit.words import plural

from . import db, errorlog, servicelog

log = logging.getLogger(__name__)

#: HOW LONG A SUBMISSION COUNTS AS COLLECTED BEFORE ANYTHING HAS COME BACK.
#:
#: The crawler writes an address into `scraper.seen_urls` the moment it queues
#: the document, and that must not be final: an address that counts as
#: collected whether or not a result ever arrives means a collector that is
#: down for an afternoon does not merely delay the archive, it LOSES that
#: afternoon's pages - permanently and without a word, because the crawler has
#: already written them off and will never offer them again.
#:
#: So a submission is provisional. `date_submitted` starts this clock and
#: `date_confirmed` stops it, and only the reconcile pass sets the second -
#: when the document is really in processed_data.tasks, which is to say when
#: the collector has fetched and filed it. An address whose result has not
#: come back within this window is offered again.
#:
#: WHAT IT COSTS, AND WHY IT IS WORTH IT. An extraction that takes longer than
#: this is submitted a second time and paid for twice: the second dedupe gate
#: (processed_data.tasks.text_source_uri) can only catch it once the first
#: result has landed. A page collected twice is money; a page collected never
#: is the thing the archive is for. Raise this if a platform is slower - it is
#: one number, read by the one statement that decides what is new.
SUBMISSION_GRACE = "2 hours"


def new_tag(source_id: int) -> str:
    """The tag a document travels under: `xs_<source id>_<12 hex>`.

    NO HYPHEN, and that is not cosmetic. The tag goes to the API, and on an
    auto-split the API appends `-01`, `-02`. A hyphen inside the tag itself
    would make the split suffix unparseable, and `reconcile` could no longer
    tell which archived task belongs to which submission. The database
    enforces the rule as a CHECK as well.

    >>> tag = new_tag(7)
    >>> tag.startswith("xs_7_"), "-" in tag, len(tag)
    (True, False, 29)
    """
    return f"xs_{source_id}_{secrets.token_hex(12)}"


class Store(CrawlSink):
    """The persisting sink for one crawl of one source."""

    #: A dry run must not be able to write through this object even if it is
    #: handed one by mistake - `crawl.run()` wraps it when `dry_run=True`.
    persists = True

    def __init__(self, dsn: str, source: Source, *, key_label: str = "",
                 on_progress=None, run_id: str = "",
                 only_project: str = "", steps: bool = False) -> None:
        #: Step logging, as the tick that started this crawl read it. Passed
        #: in rather than read here: a crawl belongs to one pass, and a sink
        #: that re-asked would write half a crawl's steps.
        self.steps = steps
        self.dsn = dsn
        self.source = source
        self.key_label = key_label or source.key_label
        #: A manual run may ask for one project of the several a page has, so
        #: that trying a configuration by hand costs one extraction and not
        #: one per project. '' is every project the page is assigned to.
        self.only_project = only_project
        #: prefix -> project id, so an override on a link group is applied to
        #: the project whose key it names and passed over by the others. Read
        #: once per crawl rather than per document.
        self._key_projects: dict[str, str] | None = None
        self._conn: psycopg.Connection | None = None
        self._on_progress = on_progress
        #: The id of the run these rows belong to; travels into the error log
        #: so a blocked page and the files it carried read as one story.
        self.run_id = run_id
        #: What happened, for the run row.
        self.queued = 0
        self.unchanged = 0
        self.duplicates = 0
        self.files_queued = 0
        #: What was queued, per project - the manual run reports it back.
        self.tags: list[dict] = []

    # -- which projects this crawl is for ------------------------------

    @property
    def projects(self) -> list[str]:
        """The projects this crawl collects into.

        A page with no project collects into nothing and is a configuration
        the editor refuses to save; a crawl of one is a crawl that writes
        nothing, which is better than writing it nowhere.
        """
        ids = list(self.source.project_ids)
        if self.only_project:
            return [self.only_project] if self.only_project in ids else []
        return ids

    def key_projects(self) -> dict[str, str]:
        """prefix -> project id, out of the registry the key probe writes."""
        if self._key_projects is None:
            rows = db.fetch_all(self.conn,
                                "SELECT text_prefix, text_project_id FROM scraper.api_keys")
            self.conn.commit()
            self._key_projects = {row["text_prefix"]: row["text_project_id"] for row in rows}
        return self._key_projects

    def project_names(self) -> dict[str, str]:
        """project id -> the name the platform files documents under.

        The archive's own gate (`processed_data.tasks`) records the project by
        NAME, because that is what the API answers with; the configuration
        records it by id. This is the one place the two have to meet.

        A project the key probe has not reached yet is NOT in the answer, and
        every caller has to treat that as "I do not know which archive this is"
        rather than as "there is no archive". Guessing '' there would compare
        every task against the empty string, match nothing, and quietly turn
        the second dedupe gate off - which costs money on the very first run
        after a restore.
        """
        rows = db.fetch_all(self.conn, """
            SELECT text_project_id, text_name FROM scraper.projects
             WHERE text_project_id = ANY(%s)
        """, (self.projects,))
        self.conn.commit()
        return {row["text_project_id"]: row["text_name"] for row in rows
                if row["text_name"]}

    # -- connection ----------------------------------------------------

    @property
    def conn(self) -> psycopg.Connection:
        if self._conn is None or self._conn.closed:
            self._conn = db.connect(self.dsn)
        return self._conn

    def close(self) -> None:
        if self._conn is not None and not self._conn.closed:
            self._conn.close()
        self._conn = None

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- the sink ------------------------------------------------------

    def unseen(self, urls: list[str], *, kind: str = "page") -> list[str]:
        """Which of these addresses have not been fetched before?

        One statement, one round trip. The cost grows with the number of links
        on the list page, not with the size of the archive - that is the whole
        purpose of `scraper.seen_urls`.

        TWO GATES, and both are needed. `seen_urls` is what THIS crawler has
        fetched. `processed_data.tasks.text_source_uri` is what has ever been
        extracted - by hand, by an earlier installation, by a second source
        that lists the same page. Without the second gate the same document is
        paid for twice.

        A source with `bool_resubmit_on_change` asks for the opposite: it wants
        its addresses fetched every run, because a change IS the news. They are
        returned unfiltered and `document()` decides on the content hash.

        ONE PROJECT SHORT IS ENOUGH. An address is worth fetching if ANY of
        the page's projects has not had it: the fetch is shared, and
        `document()` then decides project by project who gets a submission.
        """
        if not urls:
            return []
        if self.source.resubmit_on_change:
            return list(urls)
        projects = self.projects
        if not projects:
            return []

        from crawlkit.hashing import uri_hash
        hashes = [uri_hash(url) for url in urls]
        names = self.project_names()
        # A project whose name we do not know yet is compared against the
        # archive WITHOUT a project - the careful side. `t.text_project = ANY`
        # of an empty list is false, so NULL is used to mean "any project".
        wanted = [names.get(pid) for pid in projects]
        rows = db.fetch_all(self.conn, """
            SELECT c.uri
              FROM unnest(%(hashes)s::text[], %(uris)s::text[]) AS c(uri_hash, uri)
             WHERE EXISTS (
                     SELECT 1
                       FROM unnest(%(projects)s::text[], %(names)s::text[]) AS p(id, name)
                      WHERE NOT EXISTS (SELECT 1 FROM scraper.seen_urls s
                                         WHERE s.text_project_id = p.id
                                           AND s.text_uri_hash = c.uri_hash
                                           AND (s.date_submitted IS NULL
                                                OR s.date_confirmed IS NOT NULL
                                                OR s.date_submitted
                                                   > NOW() - %(grace)s::interval))
                        AND NOT EXISTS (SELECT 1 FROM processed_data.tasks t
                                         WHERE t.text_source_uri = c.uri
                                           AND (p.name IS NULL
                                                OR t.text_project = p.name)))
        """, {"hashes": hashes, "uris": list(urls), "projects": projects,
              "names": wanted, "grace": SUBMISSION_GRACE})
        self.conn.commit()
        known = {row["uri"] for row in rows}
        # The order of the list page is the only ranking it gives us; keep it.
        wanted_urls = [url for url in urls if url in known]
        if self.steps:
            # THE STEP THE CUSTOMER ARGUES WITH. "It found 200 links and sent
            # nothing" is answered here and nowhere else: the addresses that
            # got this far were accepted by the rules, and this line says how
            # many of them the two gates had already had.
            servicelog.record(
                self.conn, action="decided the links",
                detail=(f"{plural(len(urls), kind)} to consider, "
                        f"{len(wanted_urls)} not collected yet"),
                run_id=self.run_id, source_id=self.source.id)
            self.conn.commit()
        return wanted_urls

    def document(self, document: Document) -> str:
        """Store a fetched page and queue it - once per project that wants it.

        ONE FETCH, N SUBMISSIONS. The page came down the wire once; every
        project that has not had this address gets its own document row, its
        own tag and its own queue item, because every one of them is a
        separate extraction with separate settings and a separate price.

        The verdict returned is the best thing that happened to any project:
        "queued" if at least one of them will get it, otherwise the reason the
        rest of them did not - which is what the run row and the log report.
        """
        wanted, verdict = self._projects_wanting(document)
        if not wanted:
            self.conn.commit()
            if self.steps:
                # A document that was fetched and NOT queued is the more
                # surprising of the two outcomes, so it gets a row of its own
                # rather than being left out: "unchanged" and "duplicate" are
                # the answers to "why did this page cost nothing this time?".
                servicelog.record(
                    self.conn, action="left a document alone",
                    detail=f"{verdict}: {document.uri_canonical}",
                    run_id=self.run_id, source_id=self.source.id)
                self.conn.commit()
            return verdict

        for project_id in wanted:
            self._queue_for(document, project_id)
        # EVERY PROJECT HAS NOW SEEN THE ADDRESS; ONLY `wanted` IS WAITING FOR
        # A RESULT. A project that did not want this document - unchanged, or
        # a duplicate - has nothing in flight, so its row carries no clock and
        # stays seen for good. The ones that were submitted are provisional
        # until the reconcile pass confirms them (SUBMISSION_GRACE).
        self._mark_seen(document.uri_canonical, document.uri_hash,
                        kind=document.kind, content_hash=document.content_hash,
                        projects=self.projects, submitted=set(wanted))
        self.conn.commit()
        self.queued += 1
        if document.kind == "file":
            self.files_queued += 1
            db.execute(self.conn, """
                UPDATE scraper.files SET text_status = 'QUEUED', date_updated = NOW()
                 WHERE bigint_fk_source = %s AND text_file_uri_canonical = %s
            """, (self.source.id, document.uri_canonical))
            self.conn.commit()
        if self.steps:
            # ONE ROW FOR THE DOCUMENT, NOT ONE PER PROJECT. The projects are
            # named in the detail because a page serving two of them is
            # charged twice, and that is the number somebody watching wants
            # to see - but four rows for one fetch would read as four pages.
            servicelog.record(
                self.conn, action="queued a document",
                detail=(f"{document.uri_canonical} for "
                        f"{plural(len(wanted), 'project')}: {', '.join(wanted)}"),
                run_id=self.run_id, source_id=self.source.id,
                project=wanted[0] if len(wanted) == 1 else "")
            self.conn.commit()
        return "queued"

    def _projects_wanting(self, document: Document) -> tuple[list[str], str]:
        """Which projects should be sent this document, and why not the rest.

        The two gates of the module docstring, asked once per project:
        `seen_urls` for what this crawler fetched for that project, and
        `processed_data.tasks` for what was ever extracted under that
        project's name - by hand, by an earlier installation, by a second
        watchlist that lists the same page.
        """
        projects = self.projects
        if not projects:
            return [], "no project"

        names = self.project_names()
        # AND WHETHER THAT SIGHTING WAS EVER CONFIRMED. A row whose submission
        # never came back is not a reason to leave a document alone: the whole
        # point of the grace window is that the archive did NOT get it, and
        # "unchanged since the copy nobody has" is not an answer. Without this
        # the address would be offered again by `unseen()` and then dropped
        # here, one gate later, for a reason that is no longer true.
        seen = {}
        pending: set[str] = set()
        for row in db.fetch_all(self.conn, """
                    SELECT text_project_id, text_content_hash_last,
                           (date_submitted IS NOT NULL
                            AND date_confirmed IS NULL
                            AND date_submitted <= NOW() - %(grace)s::interval)
                               AS lost
                      FROM scraper.seen_urls
                     WHERE text_uri_hash = %(hash)s AND text_project_id = ANY(%(p)s)
                """, {"hash": document.uri_hash, "p": projects,
                       "grace": SUBMISSION_GRACE}):
            seen[row["text_project_id"]] = row["text_content_hash_last"]
            if row["lost"]:
                pending.add(row["text_project_id"])
        # Every task filed under this address, by project name. Read whole
        # rather than filtered by name, because a project whose name is not
        # known yet has to be compared against ALL of them - see
        # project_names(): not knowing which archive it is means treating any
        # of them as a match, not none.
        archived = {row["text_project"]: row["text_tag"]
                    for row in db.fetch_all(self.conn, """
                        SELECT text_project, min(text_tag) AS text_tag
                          FROM processed_data.tasks
                         WHERE text_source_uri = %(uri)s
                         GROUP BY text_project
                    """, {"uri": document.uri_canonical})}

        wanted: list[str] = []
        reasons: list[str] = []
        for project_id in projects:
            if project_id in seen and project_id not in pending:
                if (seen[project_id] == document.content_hash
                        or not self.source.resubmit_on_change):
                    self.unchanged += 1
                    reasons.append("unchanged")
                    continue
                wanted.append(project_id)
                continue
            # A PENDING PROJECT FALLS THROUGH TO THE ARCHIVE GATE, and that is
            # the whole reason this is not an early `wanted.append`. "Nothing
            # came back" is a statement about the queue, not about the archive:
            # if the document IS filed under this project's name - archived
            # before the reconcile pass got to it, or collected by hand - then
            # sending it again is paying twice for a document we already have,
            # which is the one thing both gates exist to prevent.
            name = names.get(project_id)
            if name is None:
                # The project's name is not in the registry yet, so this
                # document cannot be attributed to one archive. Anything at
                # this address counts: better to miss a document once than to
                # pay for one twice.
                tag = next(iter(archived.values()), None)
            else:
                tag = archived.get(name)
            if tag:
                # In the archive there is no content hash, only the address, so
                # the decision is made on the address alone - the careful side:
                # better to miss a change once than to pay for a document
                # twice.
                log.info("watched page %s: %s is already in the archive of %s (%s) "
                         "- no new task", self.source.id, document.uri_canonical,
                         names.get(project_id) or project_id, tag)
                self.duplicates += 1
                reasons.append("duplicate")
                continue
            if project_id in pending:
                log.info("watched page %s: %s was submitted for %s and never came "
                         "back - sending it again", self.source.id,
                         document.uri_canonical,
                         names.get(project_id) or project_id)
            wanted.append(project_id)

        if not wanted:
            # Every project refused it, and they may have refused it for
            # different reasons; the first one is the one the caller is told,
            # the same way a single-project crawl was told before.
            self._mark_seen(document.uri_canonical, document.uri_hash,
                            kind=document.kind, content_hash=document.content_hash,
                            projects=projects)
            return [], reasons[0] if reasons else "unchanged"
        return wanted, ""

    def _queue_for(self, document: Document, project_id: str) -> None:
        """One document row and one queue item, for one project."""
        tag = new_tag(self.source.id)
        prefix = self.source.key_prefix_for(document.uri_canonical, project_id,
                                            self.key_projects())
        row = db.fetch_one(self.conn, """
            INSERT INTO scraper.documents
                  (text_name, bigint_fk_source, text_key_label, text_uri,
                   text_uri_canonical, text_uri_hash, text_kind, text_file_type,
                   text_format, text_content, text_content_hash,
                   integer_char_count, integer_http_status, text_content_type,
                   integer_bytes, text_title, text_tag)
            VALUES (%(name)s, %(source)s, %(label)s, %(uri)s, %(canon)s, %(hash)s,
                    %(kind)s, %(ftype)s, %(format)s, %(content)s, %(chash)s,
                    %(chars)s, %(status)s, %(ctype)s, %(bytes)s, %(title)s, %(tag)s)
            RETURNING date_added, bigint_id
        """, {
            "name": (document.title or document.uri_canonical)[:500],
            "source": self.source.id, "label": self.key_label,
            "uri": document.uri, "canon": document.uri_canonical,
            "hash": document.uri_hash, "kind": document.kind,
            "ftype": document.file_type, "format": document.text_format,
            "content": document.content, "chash": document.content_hash,
            "chars": document.char_count, "status": document.http_status,
            "ctype": document.content_type, "bytes": document.bytes,
            "title": (document.title or "")[:500], "tag": tag,
        })
        assert row is not None

        db.execute(self.conn, """
            INSERT INTO scraper.submit_queue
                  (text_tag, bigint_fk_source, text_project_id, text_key_prefix,
                   text_key_label, date_document_added, bigint_fk_document,
                   text_source_uri, text_content_hash, integer_char_count, text_status)
            VALUES (%(tag)s, %(source)s, %(project)s, %(prefix)s, %(label)s,
                    %(added)s, %(doc)s, %(uri)s, %(chash)s, %(chars)s, 'PENDING')
        """, {"tag": tag, "source": self.source.id, "label": self.key_label,
              "project": project_id,
              # Decided here, when the document is queued, and not at submit
              # time: the choice belongs to the configuration as it stood when
              # the page was collected. A key switched off an hour later must not
              # silently re-price work that was already costed under another one.
              "prefix": prefix,
              "added": row["date_added"], "doc": row["bigint_id"],
              "uri": document.uri_canonical, "chash": document.content_hash,
              "chars": document.char_count})
        self.tags.append({"tag": tag, "project_id": project_id,
                          "uri": document.uri_canonical,
                          "chars": document.char_count,
                          "key_prefix": prefix})

    def file(self, record: FileRecord) -> None:
        """One row per file seen, including the ones not sent.

        A skipped PDF that disappears without a trace means nobody ever finds
        out that a project's papers are missing. A row without content is an
        answer, not a defect.
        """
        db.execute(self.conn, """
            INSERT INTO scraper.files
                  (bigint_fk_source, text_page_uri, text_file_uri_canonical,
                   text_file_type, text_status, integer_bytes, text_detail)
            VALUES (%(source)s, %(page)s, %(url)s, %(type)s, %(status)s,
                    %(bytes)s, %(detail)s)
            ON CONFLICT (bigint_fk_source, text_file_uri_canonical) DO UPDATE
               SET text_status  = EXCLUDED.text_status,
                   text_detail  = EXCLUDED.text_detail,
                   integer_bytes = GREATEST(scraper.files.integer_bytes,
                                            EXCLUDED.integer_bytes),
                   date_updated = NOW()
        """, {"source": self.source.id, "page": record.from_page[:2000],
              "url": record.url, "type": record.type, "status": record.status,
              "bytes": record.bytes, "detail": (record.reason or None)})
        self.conn.commit()

        # This table answers "what became of that file?" on the source's own
        # page; the log answers "what went wrong today?" across every source.
        # A file the configuration switched off leaves NO log row - that is a
        # decision somebody took, not something that went wrong, and a log
        # full of decisions is a log nobody reads.
        row = errorlog.file_row(status=record.status, url=record.url,
                                file_type=record.type, reason=record.reason,
                                page_uri=record.from_page)
        if row is not None:
            errorlog.record(self.conn, source_id=self.source.id,
                            source_name=self.source.name,
                            host=self.source.host, run_id=self.run_id, **row)
            self.conn.commit()

    def seen(self, url: str, *, kind: str = "page", content_hash: str = "") -> None:
        """Note an address without fetching it - forbidden by robots, or a
        fetch that failed. Without this the same address is tried on every run
        and counted as new every time."""
        from crawlkit.hashing import uri_hash
        self._mark_seen(url, uri_hash(url), kind=kind, content_hash=content_hash)
        self.conn.commit()

    def progress(self, message: str) -> None:
        log.debug("watched page %s: %s", self.source.id, message)
        if self._on_progress is not None:
            self._on_progress(message)

    # -- helpers -------------------------------------------------------

    def _mark_seen(self, url: str, url_hash: str, *, kind: str,
                   content_hash: str, projects: list[str] | None = None,
                   submitted: set[str] | None = None) -> None:
        """One row per project, because the gate is per project.

        A page fetched for two projects is known to both afterwards, and one
        fetched for one of them stays new to the other - which is what makes
        adding a project later collect the back catalogue rather than nothing.

        `submitted` names the projects that have a document in flight. Theirs
        is a provisional entry: it counts as seen only until SUBMISSION_GRACE
        has passed without a result.
        """
        for project_id in (projects if projects is not None else self.projects):
            self._mark_seen_for(project_id, url, url_hash, kind=kind,
                                content_hash=content_hash,
                                submitted=bool(submitted and project_id in submitted))

    def _mark_seen_for(self, project_id: str, url: str, url_hash: str, *,
                       kind: str, content_hash: str,
                       submitted: bool = False) -> None:
        db.execute(self.conn, """
            INSERT INTO scraper.seen_urls
                  (text_project_id, text_uri_hash, text_uri_canonical,
                   bigint_fk_source, text_kind, text_content_hash_last,
                   date_last_change, date_submitted)
            VALUES (%(project)s, %(hash)s, %(uri)s, %(source)s, %(kind)s, %(chash)s,
                    CASE WHEN %(chash)s <> '' THEN NOW() END,
                    CASE WHEN %(submitted)s THEN NOW() END)
            ON CONFLICT (text_project_id, text_uri_hash) DO UPDATE SET
                date_last_seen     = NOW(),
                integer_times_seen = scraper.seen_urls.integer_times_seen + 1,
                -- A NEW SUBMISSION STARTS A NEW CLOCK, and clears the old
                -- confirmation with it: what is in flight now is this
                -- document, and the one that arrived last month says nothing
                -- about whether this one will. A sighting that submitted
                -- nothing leaves both alone.
                date_submitted = CASE WHEN %(submitted)s THEN NOW()
                                      ELSE scraper.seen_urls.date_submitted END,
                date_confirmed = CASE WHEN %(submitted)s THEN NULL
                                      ELSE scraper.seen_urls.date_confirmed END,
                -- Only when the content really changed. Otherwise every repeat
                -- would look like a change and "when did this page last
                -- change?" would stop being answerable.
                date_last_change   = CASE
                    WHEN EXCLUDED.text_content_hash_last <> ''
                     AND scraper.seen_urls.text_content_hash_last
                         IS DISTINCT FROM EXCLUDED.text_content_hash_last
                    THEN NOW() ELSE scraper.seen_urls.date_last_change END,
                text_content_hash_last = CASE
                    WHEN EXCLUDED.text_content_hash_last <> ''
                    THEN EXCLUDED.text_content_hash_last
                    ELSE scraper.seen_urls.text_content_hash_last END
        """, {"project": project_id, "hash": url_hash, "uri": url,
              "source": self.source.id, "kind": kind, "chash": content_hash,
              "submitted": submitted})


# ----------------------------------------------------------------------
# The run row
# ----------------------------------------------------------------------

def record_run(conn: psycopg.Connection, *, source_id: int, name: str,
               key_label: str, status: str, counts: dict, ms: int,
               message: str = "") -> None:
    """One row per crawl, in the shape of `monitoring.collector_runs`.

    These rows are the only history there is. Without them nobody can say
    whether three links found today is few - and "the site changed its layout"
    looks exactly like "there was nothing new today" in a single run.
    """
    db.execute(conn, """
        INSERT INTO monitoring.scraper_runs
              (text_name, text_key_label, text_status, integer_rows, text_message,
               bigint_fk_source, integer_pages, integer_links, integer_accepted,
               integer_new, integer_files, integer_submitted, integer_ms)
        VALUES (%(name)s, %(label)s, %(status)s, %(rows)s, %(message)s,
                %(source)s, %(pages)s, %(links)s, %(accepted)s, %(new)s,
                %(files)s, %(submitted)s, %(ms)s)
    """, {"name": name[:500] or f"source-{source_id}", "label": key_label,
          "status": status, "rows": counts.get("queued", 0),
          "message": (message[:4000] or None), "source": source_id,
          "pages": counts.get("pages", 0), "links": counts.get("links", 0),
          "accepted": counts.get("accepted", 0),
          "new": counts.get("queued", 0) + counts.get("unchanged", 0),
          "files": counts.get("files_sendable", 0),
          "submitted": counts.get("queued", 0), "ms": ms})
    conn.commit()


def pending_counts(conn: psycopg.Connection, source_id: int) -> dict:
    """What the overview shows per source: queued, sent, archived, failed."""
    rows = db.fetch_all(conn, """
        SELECT text_status, count(*) AS n FROM scraper.submit_queue
         WHERE bigint_fk_source = %s GROUP BY text_status
    """, (source_id,))
    return {row["text_status"]: row["n"] for row in rows}
