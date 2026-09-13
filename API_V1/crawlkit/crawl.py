"""One crawl, used by both sides.

THIS FILE IS THE REASON `crawlkit` EXISTS. The dashboard's "Test this
configuration" and the crawler's scheduled run are the same function: same
robots verdict, same link extraction, same pagination rules, same decision per
link. What differs is only what happens with the result, and that is the
`sink`:

    crawler    a sink that writes documents, seen_urls and the submit queue
    dashboard  the default sink, which writes nothing - `dry_run=True`

Two implementations would drift, and the drift would surface as pages the
preview promised and the crawler never fetched. Nobody would look here for
that; they would look at the site.

THE ORDER OF A RUN, and every step is load-bearing:

  1. robots.txt for the list page. Forbidden -> the run is SKIPPED, and that
     is a result, not a failure.
  2. Fetch the list page with the source's engine. 4xx/5xx/challenge -> the
     run is BLOCKED and the failure counter goes up.
  3. Harvest links, follow the pagination while the four stop rules allow it.
  4. Decide about every link (`crawlkit.apply.decide`).
  5. Ask the sink which of the accepted addresses are new - THE ADDRESS IS THE
     ANCHOR, not the content.
  6. Fetch those, prepare the text, hand each to the sink.
  7. If the source collects files: find them on the subpages, apply the file
     rules, download with a cap, extract the text.

Politeness is a `Pacer`: it waits BETWEEN requests, never before the first,
and takes the larger of the source's setting and the site's Crawl-delay.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable

from crawlkit import content as content_mod
from crawlkit import files as files_mod
from crawlkit.apply import Rules, decide_all
from crawlkit.fetch import EngineUnavailable, FetchError, FetchResult, engine_for
from crawlkit.hashing import canonical_uri, uri_hash
from crawlkit.robots import AllowAll, RobotsCache, RobotsVerdict
from crawlkit.throttle import Pacer
from crawlkit.words import plural

log = logging.getLogger(__name__)

#: Named after the project, with a contact route. A crawler that says who it
#: is can be asked to stop; one that hides gets blocked instead.
DEFAULT_USER_AGENT = ("xtracting-crawler/1.0 "
                      "(+https://github.com/xtracting/xtracting-examples)")

#: A test crawl reads at most this many list pages. The point of a test is to
#: see what the site answers, not to walk it.
DRY_RUN_MAX_PAGES = 3

#: A test result carries at most this many links. Above that the popup stops
#: being usable long before the JSON stops being storable.
MAX_LINKS_IN_RESULT = 2000


# ----------------------------------------------------------------------
# The source, as the crawl needs it
# ----------------------------------------------------------------------

@dataclass(frozen=True)
class Source:
    """A row of `scraper_config.sources` plus its patterns, exact URLs and
    file rules - or the same shape as a draft that has never been saved.

    `from_config()` reads exactly the dictionary the dashboard stores in
    `test_snapshots.json_config`, so a draft and a saved source produce the
    same crawl. That is what makes "Test" trustworthy.

    >>> source = Source.from_config({"bigint_id": 7, "text_name": "Flats",
    ...     "text_list_url": "https://a.example/rent/list", "text_mode": "selected"})
    >>> source.id, source.host, source.mode
    (7, 'a.example', 'selected')
    """

    id: int = 0
    name: str = ""
    list_url: str = ""
    key_label: str = ""
    #: WHICH PROJECTS READ THIS PAGE, in the order they were chosen, as
    #: `{"text_project_id": ..., "text_key_prefix": ...}`. A page may serve
    #: several: a project decides what is extracted from a document, so two
    #: projects asking different questions of the same page are two
    #: extractions, and the page is fetched once and submitted once each.
    #:
    #: The key prefix is the key that evaluates the page FOR THAT PROJECT;
    #: '' means the project's default, the first live extraction key of it.
    projects: tuple[dict, ...] = ()
    mode: str = "selected"
    sub_sub: bool = False
    text_format: str = "plain"
    engine: str = "http"
    wait_for_selector: str = ""
    wait_after_load_ms: int = 0
    respect_robots: bool = True
    same_host_only: bool = True
    drop_legal_links: bool = True
    follow_pagination: bool = True
    paging_param: str = ""
    next_selector: str = ""
    max_pages: int = 10
    politeness_seconds: float = 6.0
    max_new_per_run: int = 25
    timeout_seconds: float = 30.0
    resubmit_on_change: bool = False
    content_selector: str = ""
    drop_selectors: str = ""
    patterns: tuple[dict, ...] = ()
    exact_urls: tuple[dict, ...] = ()
    file_rules: tuple[dict, ...] = ()

    @property
    def list_url_canonical(self) -> str:
        return canonical_uri(self.list_url)

    @property
    def host(self) -> str:
        from urllib.parse import urlsplit
        return urlsplit(self.list_url_canonical).netloc.lower()

    @property
    def project_ids(self) -> tuple[str, ...]:
        """The projects this page collects into, in the order they were chosen.

        >>> Source(projects=({"text_project_id": "p1"}, {"text_project_id": "p2"})).project_ids
        ('p1', 'p2')
        """
        return tuple(str(row.get("text_project_id") or "") for row in self.projects
                     if str(row.get("text_project_id") or ""))

    def key_prefix_for(self, uri_canonical: str, project_id: str = "",
                       key_projects: dict | None = None) -> str:
        r"""Which key evaluates this one address, for one project.

        Three places can name a key and they are asked most specific first: the
        exact address, then the link group whose pattern matches it, then the
        project's own row. '' at the end of all three means "the project's
        default", which the crawler resolves when the row is queued.

        The order is the order a person would expect from the screen: a choice
        made on one row beats a choice made on the group it sits in, which beats
        a choice made for the whole page.

        A KEY BELONGS TO ONE PROJECT, so an override on a row or a group can
        only ever apply to THAT key's project. `key_projects` is the prefix ->
        project map the crawler reads out of `scraper.api_keys`; an override
        naming a key of another project is not this project's business and is
        passed over. Without the map - the dashboard's dry run has none -
        overrides apply as written, which is what the editor shows.

        >>> s = Source(projects=({"text_project_id": "p1", "text_key_prefix": "aaaa1111"},),
        ...            patterns=({"text_kind": "accept", "text_regex": r"/rent/\d+$",
        ...                       "text_key_prefix": "bbbb2222"},),
        ...            exact_urls=({"text_kind": "monitor",
        ...                         "text_url_canonical": "https://a.example/one",
        ...                         "text_key_prefix": "cccc3333"},))
        >>> s.key_prefix_for("https://a.example/one", "p1")
        'cccc3333'
        >>> s.key_prefix_for("https://a.example/rent/42", "p1")
        'bbbb2222'
        >>> s.key_prefix_for("https://a.example/anything-else", "p1")
        'aaaa1111'

        A group with no key of its own falls through to the project rather
        than to nothing:

        >>> Source(projects=({"text_project_id": "p1", "text_key_prefix": "aaaa1111"},),
        ...        patterns=({"text_kind": "accept", "text_regex": "/x/"},)
        ...        ).key_prefix_for("https://a.example/x/1", "p1")
        'aaaa1111'

        And an override that names another project's key is not an override
        here - that project gets its own default instead:

        >>> s.key_prefix_for("https://a.example/rent/42", "p2",
        ...                  {"bbbb2222": "p1", "cccc3333": "p1"})
        ''

        A page with exactly one project needs no argument, which is what
        every caller that predates several of them passes:

        >>> Source(projects=({"text_project_id": "p1",
        ...                   "text_key_prefix": "aaaa1111"},)).key_prefix_for("https://a/x")
        'aaaa1111'
        """
        if not project_id:
            ids = self.project_ids
            project_id = ids[0] if len(ids) == 1 else ""

        def mine(prefix: str) -> bool:
            """Is this key one of `project_id`'s?"""
            if not key_projects:
                return True
            owner = key_projects.get(prefix)
            return owner is None or owner == project_id

        for exact in self.exact_urls:
            prefix = str(exact.get("text_key_prefix") or "")
            if (exact.get("text_url_canonical") == uri_canonical
                    and prefix and mine(prefix)):
                return prefix

        for pattern in self.patterns:
            if pattern.get("text_kind") != "accept":
                continue
            prefix = str(pattern.get("text_key_prefix") or "")
            if not prefix or not mine(prefix):
                continue
            try:
                if re.search(str(pattern.get("text_regex") or ""), uri_canonical):
                    return prefix
            except re.error:
                # A stored pattern that no longer compiles is the dashboard's
                # problem to report, not a reason to lose the document here.
                continue

        for row in self.projects:
            if str(row.get("text_project_id") or "") == project_id:
                return str(row.get("text_key_prefix") or "")
        return ""

    @staticmethod
    def from_config(config: dict) -> "Source":
        def flag(name: str, default: bool) -> bool:
            value = config.get(name, default)
            return default if value is None else bool(value)

        def number(name: str, default):
            value = config.get(name, default)
            return default if value is None else type(default)(value)

        return Source(
            id=int(config.get("bigint_id") or config.get("id") or 0),
            name=str(config.get("text_name") or ""),
            list_url=str(config.get("text_list_url") or config.get("list_url") or ""),
            key_label=str(config.get("text_key_label") or ""),
            projects=tuple(config.get("projects") or ()),
            mode=str(config.get("text_mode") or "selected"),
            sub_sub=flag("bool_sub_sub", False),
            text_format=str(config.get("text_format") or "plain"),
            engine=str(config.get("text_engine") or "http"),
            wait_for_selector=str(config.get("text_wait_for_selector") or ""),
            wait_after_load_ms=number("integer_wait_after_load_ms", 0),
            respect_robots=flag("bool_respect_robots", True),
            same_host_only=flag("bool_same_host_only", True),
            drop_legal_links=flag("bool_drop_legal_links", True),
            follow_pagination=flag("bool_follow_pagination", True),
            paging_param=str(config.get("text_paging_param") or ""),
            next_selector=str(config.get("text_next_selector") or ""),
            max_pages=number("integer_max_pages", 10),
            politeness_seconds=number("integer_politeness_seconds", 6.0),
            max_new_per_run=number("integer_max_new_per_run", 25),
            timeout_seconds=number("integer_timeout_seconds", 30.0),
            resubmit_on_change=flag("bool_resubmit_on_change", False),
            content_selector=str(config.get("text_content_selector") or ""),
            drop_selectors=str(config.get("text_drop_selectors") or ""),
            patterns=tuple(config.get("patterns") or ()),
            exact_urls=tuple(config.get("exact_urls") or ()),
            file_rules=tuple(config.get("file_rules") or ()),
        )

    def to_config(self) -> dict:
        """The dictionary shape again - what a run stores as `json_config`."""
        return {
            "bigint_id": self.id, "text_name": self.name,
            "text_list_url": self.list_url, "text_key_label": self.key_label,
            "projects": [dict(row) for row in self.projects],
            "text_mode": self.mode, "bool_sub_sub": self.sub_sub,
            "text_format": self.text_format, "text_engine": self.engine,
            "text_wait_for_selector": self.wait_for_selector,
            "integer_wait_after_load_ms": self.wait_after_load_ms,
            "bool_respect_robots": self.respect_robots,
            "bool_same_host_only": self.same_host_only,
            "bool_drop_legal_links": self.drop_legal_links,
            "bool_follow_pagination": self.follow_pagination,
            "text_paging_param": self.paging_param,
            "text_next_selector": self.next_selector,
            "integer_max_pages": self.max_pages,
            "integer_politeness_seconds": self.politeness_seconds,
            "integer_max_new_per_run": self.max_new_per_run,
            "integer_timeout_seconds": self.timeout_seconds,
            "bool_resubmit_on_change": self.resubmit_on_change,
            "text_content_selector": self.content_selector,
            "text_drop_selectors": self.drop_selectors,
            "patterns": list(self.patterns), "exact_urls": list(self.exact_urls),
            "file_rules": list(self.file_rules),
        }

    def config_hash(self) -> str:
        """A hash over everything that changes what a crawl does.

        `text_name`, notes and the schedule are deliberately NOT in it: a
        renamed source is the same crawl, and re-running it because someone
        fixed a typo would be a request the site did not need to answer.

        >>> a = Source.from_config({"text_list_url": "https://a.example/l"})
        >>> b = Source.from_config({"text_list_url": "https://a.example/l",
        ...                         "text_name": "renamed"})
        >>> a.config_hash() == b.config_hash()
        True
        >>> c = Source.from_config({"text_list_url": "https://a.example/l",
        ...                         "text_mode": "exact"})
        >>> a.config_hash() == c.config_hash()
        False
        """
        import json
        from crawlkit.hashing import sha256_text
        config = self.to_config()
        for ignored in ("text_name", "bigint_id", "integer_interval_minutes"):
            config.pop(ignored, None)
        return sha256_text(json.dumps(config, sort_keys=True, default=str))


# ----------------------------------------------------------------------
# What a run produces
# ----------------------------------------------------------------------

@dataclass
class PageRecord:
    url: str
    final_url: str = ""
    status: int = 0
    ms: int = 0
    bytes: int = 0
    links: int = 0
    error: str = ""


@dataclass
class LinkRecord:
    """One link, with everything the picker and the crawler need to agree on."""

    url: str
    text: str = ""
    #: `crawlkit.classify`: candidate, pagination, legal, asset, external, file
    cls: str = "candidate"
    #: The pattern label that decided, or one of the fixed words of `decide()`
    by: str = ""
    label: str = ""
    accepted_by_config: bool = False
    robots_allowed: bool | None = None
    from_page: str = ""

    def to_dict(self) -> dict:
        return {"url": self.url, "text": self.text, "label": self.label,
                "class": self.cls, "robots_allowed": self.robots_allowed,
                "accepted_by_config": self.accepted_by_config, "by": self.by}


@dataclass
class FileRecord:
    url: str
    type: str = ""
    bytes: int = 0
    from_page: str = ""
    sendable: bool = False
    reason: str = ""
    status: str = "NOT_SELECTED"
    chars: int = 0

    def to_dict(self) -> dict:
        return {"url": self.url, "type": self.type, "bytes": self.bytes,
                "from_page": self.from_page, "sendable": self.sendable,
                "reason": self.reason, "status": self.status, "chars": self.chars}


@dataclass
class Document:
    """A page or a file, ready to be submitted. What the sink is handed."""

    source_id: int
    uri: str
    uri_canonical: str
    uri_hash: str
    kind: str = "page"          # page | file
    file_type: str = ""
    text_format: str = "plain"  # plain | html | file_text
    content: str = ""
    content_hash: str = ""
    char_count: int = 0
    http_status: int | None = None
    content_type: str = ""
    bytes: int = 0
    title: str = ""
    key_label: str = ""
    from_page: str = ""


@dataclass
class CrawlResult:
    status: str = "OK"          # OK | SKIPPED | BLOCKED | ERROR
    engine_used: str = "http"
    config_hash: str = ""
    robots: dict = field(default_factory=dict)
    fetch: dict = field(default_factory=dict)
    pages: list[PageRecord] = field(default_factory=list)
    links: list[LinkRecord] = field(default_factory=list)
    files: list[FileRecord] = field(default_factory=list)
    counts: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    error: str = ""
    ms: int = 0
    #: Seconds the site asked for after a 429/503, when it named a number.
    retry_after: float | None = None

    @property
    def accepted(self) -> list[str]:
        return [link.url for link in self.links if link.accepted_by_config]

    def warn(self, message: str) -> None:
        if message not in self.warnings:
            self.warnings.append(message)


# ----------------------------------------------------------------------
# The sink
# ----------------------------------------------------------------------

class CrawlSink:
    """Where a run's results go. This one keeps them nowhere.

    That is not a stub: it IS the dashboard's sink for a test crawl. The
    crawler's `store.py` subclasses it and turns the same three calls into
    rows. Anything a sink can be asked has to answer sensibly without a
    database, or `dry_run=True` would take a different path through the code
    than the real run - which is the drift this package exists to prevent.
    """

    #: True for a sink that writes somewhere a test must not reach. A dry run
    #: wraps such a sink so that nothing can be written even if it was passed
    #: by mistake; a sink that only collects (the dashboard's snapshot) leaves
    #: this False and still sees every document.
    persists = False

    def unseen(self, urls: list[str], *, kind: str = "page") -> list[str]:
        """Which of these addresses have not been fetched before?"""
        return list(urls)

    def document(self, document: Document) -> str:
        """Store a fetched page or file. Returns what became of it:
        `queued`, `unchanged` (known address, same content) or `duplicate`."""
        return "queued"

    def file(self, record: FileRecord) -> None:
        """Record a file that was seen - including the ones not sent."""

    def seen(self, url: str, *, kind: str = "page", content_hash: str = "") -> None:
        """Note that this address was looked at."""

    def progress(self, message: str) -> None:
        """One line for the person watching a test crawl."""


# ----------------------------------------------------------------------
# The crawl
# ----------------------------------------------------------------------

def run(source: Source, sink: CrawlSink | None = None, dry_run: bool = False, *,
        user_agent: str = DEFAULT_USER_AGENT,
        robots: RobotsCache | None = None,
        engine=None,
        guard: Callable[[str], None] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        sample_subpages: int | None = None,
        deadline: float | None = None) -> CrawlResult:
    """Crawl one source. The only crawl in this repository.

    `dry_run` caps the list pages at `DRY_RUN_MAX_PAGES`, fetches at most
    `sample_subpages` subpages (none by default) and leaves the sink alone -
    the default sink writes nothing anyway, but a test must not be able to
    change the archive even if someone passes the crawler's sink by mistake.

    `sleep` is injected so the tests can check the politeness delay by its
    value instead of by waiting for it; `deadline` is a `time.monotonic()`
    stamp after which the run stops with what it has - the dashboard's hard
    timeout.
    """
    started = time.monotonic()
    sink = sink or CrawlSink()
    if dry_run and getattr(sink, "persists", False):
        # A dry run may never write, whatever it was handed.
        sink = _ReadOnly(sink)

    result = CrawlResult(engine_used=source.engine, config_hash=source.config_hash())
    robots = robots or (RobotsCache(user_agent) if source.respect_robots
                        else AllowAll(user_agent))

    def out_of_time() -> bool:
        return deadline is not None and time.monotonic() > deadline

    # -- 1. robots.txt for the list page ---------------------------------
    sink.progress("reading robots.txt")
    list_url = source.list_url_canonical
    verdict = robots.check(list_url)
    pagination_verdict = _pagination_verdict(robots, source, list_url)
    result.robots = {
        "status": robots.status(list_url),
        "note": robots.note(list_url) or verdict.reason,
        "list_allowed": verdict.allowed,
        "list_reason": verdict.reason,
        "crawl_delay": verdict.crawl_delay,
        "pagination_allowed": pagination_verdict,
    }
    if not verdict.allowed:
        result.status = "SKIPPED"
        result.error = verdict.reason
        result.warn(verdict.reason)
        result.counts = _counts(result)
        result.ms = int((time.monotonic() - started) * 1000)
        return result
    if pagination_verdict is False:
        result.warn("robots.txt forbids the paginated addresses of this list - "
                    "only the first page will be read")

    pacer = Pacer(source.politeness_seconds, robots_delay=verdict.crawl_delay,
                  sleep=sleep)

    # -- 2. the list page, then the pagination ---------------------------
    from contextlib import ExitStack
    with ExitStack() as stack:
        try:
            opened = stack.enter_context(
                engine if engine is not None
                else engine_for(source.engine, user_agent,
                                timeout=source.timeout_seconds, guard=guard))
        except EngineUnavailable as exc:
            result.status = "ERROR"
            result.error = str(exc)
            result.warn(str(exc))
            result.counts = _counts(result)
            return result
        result.engine_used = getattr(opened, "name", source.engine)

        try:
            harvest, next_urls = _walk_list(source, opened, robots, pacer, result,
                                            sink, dry_run, out_of_time)
        except FetchError as exc:
            result.status = "BLOCKED" if exc.status else "ERROR"
            result.error = str(exc)
            result.retry_after = exc.retry_after
            result.fetch = {"status": exc.status, "final_url": "", "ms": 0,
                            "bytes": 0, "error": str(exc),
                            "challenge": bool(exc.challenge)}
            result.warn(_explain_fetch_error(exc, source))
            result.counts = _counts(result)
            result.ms = int((time.monotonic() - started) * 1000)
            return result

        # -- 3. decide about every link ----------------------------------
        rules = Rules.from_config({**source.to_config(),
                                   "text_list_url": list_url}, next_urls=next_urls)
        by_url = {link.url: link for link in harvest}
        for decision in decide_all(list(by_url), rules):
            link = by_url.get(decision.url)
            record = LinkRecord(
                url=decision.url, text=link.text if link else "",
                cls=decision.cls, by=decision.by,
                label=decision.by if decision.document else decision.cls,
                accepted_by_config=decision.accepted,
                robots_allowed=_robots_for(robots, decision.url,
                                           want=decision.accepted),
                from_page=getattr(link, "from_page", "") if link else "")
            result.links.append(record)
        if len(result.links) > MAX_LINKS_IN_RESULT:
            result.warn(f"{len(result.links)} links found; the result keeps the "
                        f"first {MAX_LINKS_IN_RESULT}")
            del result.links[MAX_LINKS_IN_RESULT:]

        if not result.links:
            result.warn(
                "0 links found on this page. Sites that build their list with "
                "JavaScript deliver an empty page to a plain HTTP request - try "
                "Rendered mode."
                if source.engine == "http" else
                "0 links found on this page, even with a browser. Check the "
                "address, or give a selector to wait for.")

        # -- 4. what is new ----------------------------------------------
        wanted = _documents_of(source, result, rules)
        new_urls = [url for url in sink.unseen(wanted)
                    if _robots_for(robots, url, want=True) is not False]
        skipped_by_robots = [url for url in wanted
                             if _robots_for(robots, url, want=True) is False]
        for url in skipped_by_robots:
            sink.seen(url, kind="page")

        # A dry run opens no subpages unless it was asked to: a test of kind
        # "crawl" is about what the list page answers, and fetching twenty-five
        # detail pages to answer that would be a load nobody asked for.
        cap = (0 if sample_subpages is None else sample_subpages) if dry_run \
            else source.max_new_per_run
        if len(new_urls) > cap:
            # No warning when the cap is zero: that is a test crawl of kind
            # "crawl", which is not supposed to open subpages at all, and
            # "this run takes 0 of them" would read like a fault.
            #
            # TWO SENTENCES, BECAUSE THEY ARE TWO DIFFERENT LIMITS. A real
            # run stops at `integer_max_new_per_run` and picks the rest up
            # next time. A dry run stops at the SAMPLE the caller asked for,
            # which is not that field and has no "next time" - saying "the
            # rest on the next one" sent people to a Schedule field holding a
            # number that was not the one in the sentence.
            if cap and dry_run:
                result.warn(f"{len(new_urls)} new addresses on the list; this "
                            f"test opens {cap} of them as a sample")
            elif cap:
                result.warn(f"{len(new_urls)} new addresses; this run takes "
                            f"{cap} of them and the rest on the next one")
            new_urls = new_urls[:cap]

        # -- 5. fetch them -----------------------------------------------
        file_rules = files_mod.FileRules.from_config({"file_rules": list(source.file_rules)})
        for index, url in enumerate(new_urls, start=1):
            if out_of_time():
                result.warn("the time limit for this run was reached")
                break
            sink.progress(f"subpage {index} of {len(new_urls)}")
            pacer.wait()
            try:
                page = opened.fetch(url, wait_for=source.wait_for_selector,
                                    wait_after_load_ms=source.wait_after_load_ms)
            except FetchError as exc:
                result.warn(f"{url}: {exc}")
                sink.seen(url, kind="page")
                if exc.throttled:
                    # The site asked for room. Stop, and remember how much
                    # room: the doubled delay is what the next run starts
                    # from, and the warning says the number out loud so that a
                    # person can see what the crawler concluded.
                    slower = pacer.slow_down()
                    result.retry_after = exc.retry_after
                    result.status = "BLOCKED"
                    result.error = str(exc)
                    result.warn(_explain_fetch_error(exc, source))
                    result.warn(f"the delay between requests was doubled to "
                                f"{slower:.0f} seconds")
                    break
                continue

            prepared = content_mod.prepare(
                page.html, text_format=source.text_format,
                content_selector=source.content_selector,
                drop_selectors=source.drop_selectors)
            document = Document(
                source_id=source.id, uri=page.url,
                uri_canonical=canonical_uri(page.final_url or page.url),
                uri_hash=uri_hash(canonical_uri(page.final_url or page.url)),
                kind="page", text_format=prepared.text_format,
                content=prepared.content, content_hash=prepared.content_hash,
                char_count=prepared.char_count, http_status=page.status,
                content_type=page.content_type, bytes=page.bytes,
                title=prepared.title, key_label=source.key_label)
            outcome = sink.document(document)
            result.counts[outcome] = result.counts.get(outcome, 0) + 1

            if source.sub_sub:
                _collect_files(source, page, file_rules, robots, pacer, result,
                               sink, dry_run, user_agent, out_of_time)

    result.counts = _counts(result)
    result.ms = int((time.monotonic() - started) * 1000)
    return result


# ----------------------------------------------------------------------
# The pieces
# ----------------------------------------------------------------------

class _ReadOnly(CrawlSink):
    """A sink with its writing hand tied - what `dry_run=True` wraps around
    whatever it was given. Reads (`unseen`) and progress still pass through,
    so a test crawl in the dashboard can still say "I already know this one"."""

    def __init__(self, inner: CrawlSink) -> None:
        self._inner = inner

    def unseen(self, urls: list[str], *, kind: str = "page") -> list[str]:
        return list(urls)

    def progress(self, message: str) -> None:
        self._inner.progress(message)


def _walk_list(source: Source, engine, robots: RobotsCache, pacer: Pacer,
               result: CrawlResult, sink: CrawlSink, dry_run: bool,
               out_of_time: Callable[[], bool]) -> tuple[list, list[str]]:
    """The list page and its pagination. Returns (links, next page addresses).

    FOUR STOP RULES, and every one of them has been needed:

      * no next link - the ordinary end of a list;
      * the page cap, for a site that answers 200 to any page number;
      * the address was already fetched in this run, because the last page
        often links back to the first;
      * the next page brings no NEW link, which is how a site that ignores the
        paging parameter looks from the outside: an endless list.

    And the next link itself is removed from the harvest. It is an ordinary
    `<a href>`; left in, it would be fetched as a detail page AND counted as a
    new link on every round, which would blunt the fourth rule.
    """
    max_pages = min(source.max_pages, DRY_RUN_MAX_PAGES) if dry_run else source.max_pages
    if not source.follow_pagination:
        max_pages = 1

    sink.progress("fetching page 1")
    # The first wait is free (`Pacer`), but it has to be ASKED for here, or
    # the free one would be spent on page two and the site would see page one
    # and page two back to back.
    pacer.wait()
    first = engine.fetch(source.list_url_canonical,
                         wait_for=source.wait_for_selector,
                         wait_after_load_ms=source.wait_after_load_ms)
    result.fetch = {"status": first.status, "final_url": first.final_url,
                    "ms": first.elapsed_ms, "bytes": first.bytes,
                    "error": "", "challenge": False}

    harvest: list[content_mod.Link] = []
    next_urls: list[str] = []
    seen_pages = {canonical_uri(first.final_url), source.list_url_canonical}
    page = first
    page_url = canonical_uri(first.final_url) or source.list_url_canonical

    for number in range(1, max_pages + 1):
        links = content_mod.extract_links(page.html, page_url)
        following = content_mod.next_page(page.html, page_url,
                                          paging_param=source.paging_param,
                                          selector=source.next_selector,
                                          links=links)
        if following:
            next_urls.append(canonical_uri(following))
        fresh = [link for link in links
                 if link.url not in {l.url for l in harvest}
                 and link.url not in next_urls
                 and link.url not in seen_pages]
        result.pages.append(PageRecord(url=page_url, final_url=page.final_url,
                                       status=page.status, ms=page.elapsed_ms,
                                       bytes=page.bytes, links=len(links)))
        if number > 1 and not fresh:
            result.warn("the next page brought no new links - the pagination "
                        "was stopped there")
            result.pages.pop()
            break
        harvest.extend(fresh)

        if number >= max_pages or not following or out_of_time():
            if following and number >= max_pages:
                result.warn(f"stopped after {max_pages} pages; the list has more")
            break
        if canonical_uri(following) in seen_pages:
            result.warn("the pagination links back to a page already read - "
                        "stopped there")
            break
        if source.respect_robots and not robots.check(following).allowed:
            result.warn(f"robots.txt forbids page {number + 1} of this list - "
                        f"only the first {plural(number, 'page')} are read")
            break

        pacer.wait()
        sink.progress(f"fetching page {number + 1}")
        try:
            page = engine.fetch(following, wait_for=source.wait_for_selector,
                                wait_after_load_ms=source.wait_after_load_ms)
        except FetchError as exc:
            # Not a failure of the run: the pages already read are good, and a
            # broken page four must not discard the first three.
            result.warn(f"page {number + 1} could not be read ({exc}) - it "
                        f"stays at {plural(number, 'page')}")
            break
        page_url = canonical_uri(page.final_url) or canonical_uri(following)
        seen_pages.add(page_url)

    return harvest, next_urls


def _documents_of(source: Source, result: CrawlResult, rules: Rules) -> list[str]:
    """The addresses this run should fetch.

    In `exact` mode that is the monitored addresses themselves - the list page
    is then only the thing that was configured, not a source of links. In the
    other two modes it is what `decide()` accepted.
    """
    if source.mode == "exact":
        return sorted(rules.exact_monitor)
    return [link.url for link in result.links if link.accepted_by_config]


def _collect_files(source: Source, page: FetchResult, rules: files_mod.FileRules,
                   robots: RobotsCache, pacer: Pacer, result: CrawlResult,
                   sink: CrawlSink, dry_run: bool, user_agent: str,
                   out_of_time: Callable[[], bool]) -> None:
    """The files of one subpage: found, judged, and - if wanted - read.

    Every file leaves a record, including the ones that are not sent. "Why is
    that PDF not in the archive?" has to have an answer on the source's page,
    and "the type is switched off" is a different answer from "no text layer".
    """
    page_url = canonical_uri(page.final_url or page.url)
    for found in files_mod.discover(page.html, page_url):
        if out_of_time():
            return
        wanted, reason = rules.wanted(found.url, found.type)
        record = FileRecord(url=found.url, type=found.type, from_page=page_url,
                            sendable=wanted, reason=reason,
                            status="NOT_SELECTED" if not wanted else "QUEUED")
        if not wanted:
            result.files.append(record)
            sink.file(record)
            continue

        if source.respect_robots and not robots.check(found.url).allowed:
            record.status, record.sendable = "ROBOTS", False
            record.reason = "robots.txt forbids this address"
            result.files.append(record)
            sink.file(record)
            continue

        pacer.wait()
        loaded = files_mod.download(found.url, found.type, user_agent=user_agent,
                                    max_bytes=rules.cap_bytes(found.type),
                                    timeout=max(60.0, source.timeout_seconds * 2))
        record.bytes = loaded.bytes_read
        if not loaded.ok:
            record.status, record.sendable = loaded.status, False
            record.reason = loaded.detail
            result.files.append(record)
            sink.file(record)
            continue

        text, why = files_mod.to_text(loaded.data, found.type)
        if not text:
            record.status, record.sendable, record.reason = "NO_TEXT", False, why
            result.files.append(record)
            sink.file(record)
            continue

        record.chars = len(text)
        result.files.append(record)
        sink.file(record)
        if dry_run:
            continue
        from crawlkit.hashing import sha256_text
        sink.document(Document(
            source_id=source.id, uri=found.url,
            uri_canonical=canonical_uri(found.url),
            uri_hash=uri_hash(canonical_uri(found.url)), kind="file",
            file_type=found.type, text_format="file_text", content=text,
            content_hash=sha256_text(text), char_count=len(text),
            http_status=200, content_type=loaded.content_type,
            bytes=loaded.bytes_read, title=files_mod.file_name(found.url),
            key_label=source.key_label, from_page=page_url))


def _robots_for(robots: RobotsCache, url: str, *, want: bool) -> bool | None:
    """The robots verdict for a link - but only where it is free or needed.

    A list page can carry links to twenty hosts. Asking about each of them
    would fetch twenty robots.txt files to fill in a column of a popup, and
    nineteen of those hosts are never crawled. So: hosts already known are
    answered from the cache, an address about to be fetched is asked for, and
    the rest stay `None` - "not asked", which the picker shows as nothing
    rather than as a lock.
    """
    if want or robots.known(url):
        return robots.check(url).allowed
    return None


def _pagination_verdict(robots: RobotsCache, source: Source, list_url: str) -> bool | None:
    """Would robots.txt allow page 2? Answered before anything is fetched,
    because "only page 1, the rest is forbidden" is a banner the editor shows
    while a person is still configuring."""
    if not source.paging_param:
        return None
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
    parts = urlsplit(list_url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if k.lower() != source.paging_param.lower()]
    query.append((source.paging_param, "2"))
    probe = urlunsplit((parts.scheme, parts.netloc, parts.path,
                        urlencode(query), ""))
    return robots.check(probe).allowed


def _explain_fetch_error(exc: FetchError, source: Source) -> str:
    """The banner text for a failed fetch: what happened, and what to do."""
    if exc.challenge:
        return ("The site answered with a browser check instead of the page. "
                "Rendered mode gets through some of those; where it does not, "
                "the site does not want to be read by a program.")
    if exc.status == 404:
        return "The address does not exist (404). Check the list address."
    if exc.status in (429, 503):
        return ("The site asked for room (HTTP %d). The run stopped, and the "
                "schedule puts the next one off - a site that is being polite "
                "about being overloaded should not be asked again straight "
                "away." % exc.status)
    if 400 <= exc.status < 500:
        return (f"The site refused the request (HTTP {exc.status}). It may want "
                f"a browser - try Rendered mode.")
    if exc.status >= 500:
        return (f"The site has a problem of its own (HTTP {exc.status}). "
                f"Nothing to change here; try again later.")
    return f"The address could not be fetched: {exc}"


def _counts(result: CrawlResult) -> dict:
    documents = [link for link in result.links if link.accepted_by_config]
    return {
        "pages": len(result.pages),
        "links": len(result.links),
        "accepted": len(documents),
        "rejected": len([l for l in result.links if not l.accepted_by_config
                         and l.cls not in ("pagination", "list_self")]),
        "pagination": len([l for l in result.links if l.cls == "pagination"]),
        # The list page linking to itself. Counted because the editor's
        # counter row has to add up - links = accepted + rejected +
        # pagination + list_self - and these two are the only classes
        # `rejected` leaves out.
        "list_self": len([l for l in result.links if l.cls == "list_self"]),
        "files": len(result.files),
        "files_sendable": len([f for f in result.files if f.sendable]),
        "queued": result.counts.get("queued", 0),
        "unchanged": result.counts.get("unchanged", 0),
        "duplicate": result.counts.get("duplicate", 0),
    }


__all__ = ["CrawlResult", "CrawlSink", "DEFAULT_USER_AGENT", "DRY_RUN_MAX_PAGES",
           "Document", "FileRecord", "LinkRecord", "MAX_LINKS_IN_RESULT",
           "PageRecord", "Source", "run"]
