"""crawlkit - the crawl rules shared by the dashboard and the crawler.

A library, not a service. It is copied into both images so that the "test
this configuration" crawl the dashboard runs and the scheduled crawl the
crawler runs are literally the same code: one `canonical_uri()`, one form of
an address, one `learn()`, one `decide()`. Two copies of any of these would
drift, and the drift would show up as pages the preview promised and the
crawler never fetched.

The pure half (this package's top-level modules) has no I/O and no
dependencies beyond the standard library; every function is doctested. The
I/O half (robots, fetch engines, content, files, crawl, testrun) builds on it
and lists its dependencies in `requirements.txt`.
"""

from __future__ import annotations

from crawlkit.apply import Decision, Rules, decide, decide_all
from crawlkit.classify import (CLASSES, Classifier, classify,
                               looks_like_next_label)
from crawlkit.forms import (PAGING_KEYS, Form, Pattern, Segment, form_of,
                            split_form)
from crawlkit.hashing import canonical_uri, same_document, sha256_text, uri_hash
from crawlkit.learn import Conflict, LearnedPattern, LearnResult, learn
from crawlkit.legal import is_legal, legal_word
from crawlkit.netguard import NetGuardError, check_public, is_public_ip
from crawlkit.words import plural

__all__ = [
    "CLASSES", "Classifier", "Conflict", "Decision", "Form", "LearnResult",
    "LearnedPattern", "NetGuardError", "PAGING_KEYS", "Pattern", "Rules",
    "Segment", "canonical_uri", "check_public", "classify", "decide",
    "decide_all", "form_of", "is_legal", "is_public_ip", "learn",
    "legal_word", "looks_like_next_label", "plural", "same_document",
    "sha256_text", "split_form", "uri_hash",
]
