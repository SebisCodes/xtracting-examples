"""A stand-in for `crawlkit.testrun.execute()`, for tests that are not about it.

The crawl engine is the I/O half of crawlkit: httpx, lxml, a browser. The
dashboard's runner, its queue, its timeout and the endpoints around it are
none of those things, and testing them through a real crawl would make every
one of those tests depend on a network and on the engine being finished.

So this module answers with the shape `03-scraper.sql` documents for
`test_snapshots.json_result`, built out of a fixed link list by the PURE half
of crawlkit - `classify()` and `decide()`, the same functions the real engine
uses for those two fields. The links are shaped like the estate stand-in site:
twenty detail pages, a pagination link, a legal footer, a sibling list, a file.

`crawlkit.testrun` replaces this the moment it exists; the flow test that
really crawls uses it and is marked xfail until then.
"""

from __future__ import annotations

import threading
import time
from urllib.parse import urlsplit

from crawlkit.apply import Rules, decide
from crawlkit.classify import Classifier
from crawlkit.hashing import canonical_uri

#: The gate the "another test is running" flow test holds shut.
GATE = threading.Event()


def links_for(list_url: str) -> list[tuple[str, str]]:
    """(url, link text) as they would stand on the list page."""
    parts = urlsplit(canonical_uri(list_url))
    root = f"{parts.scheme}://{parts.netloc}"
    links = [(f"{root}/rent/400123{n:02d}", f"Apartment {n}") for n in range(20)]
    links += [
        (canonical_uri(list_url) + "?ep=2", "2"),
        (f"{root}/impressum", "Impressum"),
        (f"{root}/rent/apartment/city-basel/matching-list", "Basel"),
        (f"{root}/files/expose-40012300.pdf", "Expose"),
    ]
    return links


def execute(config: dict, allow_private: bool = False, progress=None,
            kind: str = "crawl", sample_subpages: int = 5) -> dict:
    """The agreed signature: `execute(config, allow_private, progress)`."""
    say = progress or (lambda text: None)
    list_url = str(config.get("text_list_url") or config.get("list_url") or "")
    say("reading robots.txt")

    rules = Rules.from_config(config)
    classifier = Classifier(rules.list_url or list_url)
    say("fetching page 1")

    links = []
    accepted = 0
    for url, text in links_for(list_url):
        verdict = decide(url, rules, classifier)
        accepted += 1 if verdict.accepted else 0
        links.append({
            "url": verdict.url, "text": text, "label": verdict.by,
            "class": classifier.classify(verdict.url), "robots_allowed": True,
            "accepted_by_config": verdict.accepted, "by": verdict.by,
        })
    say(f"{len(links)} links found")

    files = [{"url": link["url"], "type": "pdf", "bytes": 12345,
              "from_page": list_url, "sendable": True, "reason": ""}
             for link in links if link["class"] == "file"] if kind == "files" else []
    if kind == "files":
        say(f"subpage 1 of {sample_subpages}")

    return {
        "engine_used": str(config.get("text_engine") or "http"),
        "robots": {"status": 200, "note": "", "list_allowed": True, "list_reason": "",
                   "crawl_delay": 1.0, "pagination_allowed": False},
        "fetch": {"status": 200, "final_url": canonical_uri(list_url), "ms": 12,
                  "bytes": 4096, "error": "", "challenge": False},
        "pages": [{"url": canonical_uri(list_url), "status": 200, "links": len(links)}],
        "links": links,
        "files": files,
        "counts": {"pages": 1, "links": len(links), "accepted": accepted,
                   "rejected": len(links) - accepted, "files": len(files)},
        "warnings": [] if accepted else ["0 links accepted by this configuration"],
    }


def gated_execute(config: dict, allow_private: bool = False, progress=None,
                  kind: str = "crawl", sample_subpages: int = 5) -> dict:
    """Waits for `GATE` before answering - a test that is still running."""
    (progress or (lambda text: None))("waiting for the site")
    GATE.wait(20)
    return execute(config, allow_private, progress, kind, sample_subpages)


def never_answers(config: dict, allow_private: bool = False, progress=None,
                  kind: str = "crawl", sample_subpages: int = 5) -> dict:
    """The crawl that hangs. The runner's timeout is what has to end it."""
    (progress or (lambda text: None))("waiting for the site")
    time.sleep(30)
    return execute(config, allow_private, progress, kind, sample_subpages)
