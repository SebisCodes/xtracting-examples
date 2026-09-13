""""Test this configuration" - one crawl, no consequences.

The dashboard runs this when someone presses the button in the source editor,
and the crawler can run it from the command line for debugging. It is not a
second implementation of the crawl: it builds a `Source` from the draft, calls
`crawl.run(dry_run=True)` and turns the result into the JSON the dashboard
stores in `scraper_config.test_snapshots.json_result` and the editor renders.

WHAT dry_run MEANS HERE: at most three list pages, at most
`sample_subpages` subpages (none at all for `kind="crawl"`), nothing written
anywhere. The sample exists for one number - the character count of a typical
subpage in the chosen format - because HTML costs three to ten times as much
as plain text and that is worth knowing before saving, not after the first
invoice.

THE GUARD IS NOT OPTIONAL. Anyone who can reach the dashboard's port can start
a crawl with an address of their choosing; without the check that the address
resolves to a public IP, that is a request generator pointed at the inside of
the customer's network. `allow_private=True` exists for the tests, which run
against a stand-in site on 127.0.0.1.
"""

from __future__ import annotations

import logging
import time
from typing import Callable

from crawlkit.crawl import (DEFAULT_USER_AGENT, CrawlResult, CrawlSink, Document,
                            Source, run)
from crawlkit.netguard import NetGuardError, check_public

log = logging.getLogger(__name__)

#: What a test crawl is allowed to take, in seconds. The dashboard passes its
#: own DASHBOARD_TEST_TIMEOUT_SECONDS; this is the fallback.
DEFAULT_TIMEOUT_SECONDS = 120.0

KINDS = ("crawl", "files")


class SampleSink(CrawlSink):
    """Collects what a test fetched, and keeps none of it.

    Only the measurements: how long the text of a subpage is in the chosen
    format, and what its title was. The text itself is deliberately not kept -
    a snapshot is configuration, not a copy of the site.
    """

    def __init__(self, progress: Callable[[str], None] | None = None) -> None:
        self.samples: list[dict] = []
        self.messages: list[str] = []
        self._progress = progress

    def document(self, document: Document) -> str:
        self.samples.append({"url": document.uri_canonical,
                             "title": document.title,
                             "chars": document.char_count,
                             "kind": document.kind,
                             "format": document.text_format})
        return "queued"

    def progress(self, message: str) -> None:
        self.messages.append(message)
        if self._progress is not None:
            self._progress(message)


def execute(config: dict, allow_private: bool = False, *, kind: str = "crawl",
            sample_subpages: int = 5, user_agent: str = DEFAULT_USER_AGENT,
            timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
            progress: Callable[[str], None] | None = None,
            engine=None, sleep: Callable[[float], None] = time.sleep) -> dict:
    """Run the draft `config` against the site and return the test result.

    Raises `NetGuardError` for an address that points into a private network -
    the API turns that into a 400 before it ever gets here, and this is the
    second lock on the same door.

    >>> result = execute({"text_list_url": "http://127.0.0.1:9/list"},
    ...                  allow_private=False)
    Traceback (most recent call last):
    ...
    crawlkit.netguard.NetGuardError: This address points at a private network (127.0.0.1) and cannot be tested.
    """
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}, got {kind!r}")

    source = Source.from_config(config)
    if not source.list_url:
        raise ValueError("the configuration has no list address")

    # Before anything is fetched, and again on every redirect hop inside the
    # engine - a 302 into the private range is the interesting case.
    check_public(source.list_url, allow_private)

    def guard(url: str) -> None:
        check_public(url, allow_private)

    sink = SampleSink(progress)
    started = time.monotonic()
    result = run(source, sink, True, user_agent=user_agent, guard=guard,
                 engine=engine, sleep=sleep,
                 sample_subpages=sample_subpages if kind == "files" else 0,
                 deadline=started + timeout_seconds)
    return to_json(result, source, sink, kind=kind,
                   sample_subpages=sample_subpages if kind == "files" else 0)


def to_json(result: CrawlResult, source: Source, sink: SampleSink | None = None,
            *, kind: str = "crawl", sample_subpages: int = 0) -> dict:
    """The result in the shape the dashboard stores and renders.

    The keys are what `sources.js` reads; `status`,
    `error` and `samples` are here as well because a snapshot that says
    "FAILED" has to be able to say why, and because the character count of a
    sample is what the editor shows next to the format switch.
    """
    samples = list(sink.samples) if sink else []
    counts = dict(result.counts)
    counts["samples"] = len(samples)
    counts["sample_chars"] = sum(sample["chars"] for sample in samples)
    counts["sample_chars_avg"] = (counts["sample_chars"] // len(samples)
                                  if samples else 0)
    return {
        "kind": kind,
        "status": result.status,
        "error": result.error,
        "config_hash": result.config_hash,
        "engine_used": result.engine_used,
        "engine_asked": source.engine,
        "text_format": source.text_format,
        "ms": result.ms,
        "robots": result.robots,
        "fetch": result.fetch,
        "pages": [{"url": page.url, "final_url": page.final_url,
                   "status": page.status, "ms": page.ms, "bytes": page.bytes,
                   "links": page.links, "error": page.error}
                  for page in result.pages],
        "links": [link.to_dict() for link in result.links],
        "files": [record.to_dict() for record in result.files],
        "samples": samples,
        "sample_subpages": sample_subpages,
        "counts": counts,
        "warnings": list(result.warnings),
    }


def failure(config: dict, exc: Exception) -> dict:
    """The result shape for a test that could not even start.

    A snapshot without a result would leave the editor with a spinner and no
    sentence; this gives it the sentence.
    """
    source = Source.from_config(config)
    empty = CrawlResult(status="FAILED", engine_used=source.engine,
                        config_hash=source.config_hash())
    empty.error = str(exc)
    empty.warn(str(exc))
    return to_json(empty, source)


__all__ = ["DEFAULT_TIMEOUT_SECONDS", "KINDS", "SampleSink", "execute", "failure",
           "to_json"]
