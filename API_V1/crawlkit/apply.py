"""Applying a saved configuration to a link: `decide()`.

The crawler runs this on every link of every list page; the dashboard runs
it on the links of the last test snapshot for the preview. One function,
so the preview never promises what the crawler will not fetch.

The order is fixed and deliberate:

1. The list page itself and its pagination are not documents at all.
2. An exact reject wins over everything - it is the user's most explicit "no".
3. An exact monitor wins over patterns - the user's most explicit "yes".
4. A reject pattern.
5. Legal links, when the source drops them.
6. Assets, files and other hosts are never fetched as pages (files are the
   business of the file rules on subpages, not of the page patterns).
7. Only then the mode: *selected* needs a matching accept pattern,
   *all_except_rejected* takes what is left, *exact* takes nothing that was
   not an exact monitor.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from crawlkit.classify import Classifier
from crawlkit.forms import Pattern, split_form
from crawlkit.hashing import canonical_uri

MODES = ("selected", "all_except_rejected", "exact")


@dataclass(frozen=True)
class Rules:
    """The part of a source that `decide()` needs. Exact URLs are canonical.

    `from_config()` reads the draft shape the dashboard stores in
    ``scraper_config.test_snapshots.json_config`` (the source columns, plus
    ``patterns`` and ``exact_urls`` in their table shape), so that the crawler
    and the preview build their rules from the same dictionary.
    """

    list_url: str
    mode: str = "selected"
    same_host_only: bool = True
    drop_legal_links: bool = True
    accept: tuple[tuple[Pattern, str], ...] = ()   # (pattern, label)
    reject: tuple[tuple[Pattern, str], ...] = ()
    exact_monitor: frozenset[str] = field(default_factory=frozenset)
    exact_reject: frozenset[str] = field(default_factory=frozenset)
    next_urls: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        if self.mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}, got {self.mode!r}")

    @staticmethod
    def from_config(config: dict, next_urls=()) -> "Rules":
        """
        >>> r = Rules.from_config({
        ...     "text_list_url": "https://a.example/rent/list",
        ...     "text_mode": "selected", "bool_drop_legal_links": True,
        ...     "patterns": [{"text_kind": "accept", "text_label": "/rent/#",
        ...                   "json_form": split_form("https://a.example/rent/1").pattern.to_dict()}],
        ...     "exact_urls": [{"text_kind": "reject", "text_url_canonical": "https://a.example/rent/2"}],
        ... })
        >>> decide("https://a.example/rent/3", r).accepted, decide("https://a.example/rent/2", r).by
        (True, 'exact')
        """
        accept, reject = [], []
        for row in config.get("patterns") or ():
            pattern = Pattern.from_json(row["json_form"])
            entry = (pattern, str(row.get("text_label") or pattern.label()))
            (accept if row.get("text_kind") == "accept" else reject).append(entry)
        monitor, rejects = set(), set()
        for row in config.get("exact_urls") or ():
            canon = canonical_uri(row.get("text_url_canonical") or row.get("text_url") or "")
            if not canon:
                continue
            (monitor if row.get("text_kind") == "monitor" else rejects).add(canon)
        return Rules(
            list_url=str(config.get("text_list_url") or config.get("list_url") or ""),
            mode=str(config.get("text_mode") or "selected"),
            same_host_only=bool(config.get("bool_same_host_only", True)),
            drop_legal_links=bool(config.get("bool_drop_legal_links", True)),
            accept=tuple(accept), reject=tuple(reject),
            exact_monitor=frozenset(monitor), exact_reject=frozenset(rejects),
            next_urls=frozenset(canonical_uri(u) for u in next_urls if u),
        )


@dataclass(frozen=True)
class Decision:
    """``by`` is what the UI shows: a pattern label, or one of the fixed words
    ``exact``, ``monitor``, ``legal``, ``asset``, ``file``, ``external``,
    ``everything else``, ``no pattern``, ``exact mode``. ``document`` is False
    for the list page and its pagination - they are neither accepted nor
    rejected, they are the list."""

    url: str
    cls: str
    accepted: bool
    by: str
    document: bool = True


def decide(url: str, rules: Rules, classifier: Classifier | None = None) -> Decision:
    """One link, one verdict.

    >>> P = split_form("https://a.example/rent/4001231").pattern
    >>> r = Rules("https://a.example/rent/list", accept=((P, "/rent/#"),))
    >>> decide("https://a.example/rent/4001299", r)
    Decision(url='https://a.example/rent/4001299', cls='candidate', accepted=True, by='/rent/#', document=True)
    >>> decide("https://a.example/impressum", r).by
    'legal'
    >>> decide("https://a.example/rent/list?page=2", r).document
    False

    In *all_except_rejected* the un-matched candidate is taken, in *exact*
    only a monitored address is:

    >>> decide("https://a.example/about", Rules("https://a.example/rent/list", mode="all_except_rejected")).by
    'everything else'
    >>> decide("https://a.example/about", Rules("https://a.example/rent/list", mode="exact")).by
    'exact mode'
    """
    classifier = classifier or Classifier(rules.list_url, rules.next_urls)
    canon = canonical_uri(url)
    form = split_form(canon)
    cls = classifier.classify(canon, form)

    if cls in ("list_self", "pagination"):
        return Decision(canon, cls, False, cls, document=False)
    if canon in rules.exact_reject:
        return Decision(canon, cls, False, "exact")
    if canon in rules.exact_monitor:
        return Decision(canon, cls, True, "monitor")
    for pattern, label in rules.reject:
        if pattern.matches_form(form):
            return Decision(canon, cls, False, label)
    if cls == "legal" and rules.drop_legal_links:
        return Decision(canon, cls, False, "legal")
    if cls == "asset":
        return Decision(canon, cls, False, "asset")
    if cls == "external" and rules.same_host_only:
        return Decision(canon, cls, False, "external")
    if cls == "file":
        return Decision(canon, cls, False, "file")

    if rules.mode == "selected":
        for pattern, label in rules.accept:
            if pattern.matches_form(form):
                return Decision(canon, cls, True, label)
        return Decision(canon, cls, False, "no pattern")
    if rules.mode == "all_except_rejected":
        return Decision(canon, cls, True, "everything else")
    return Decision(canon, cls, False, "exact mode")


def decide_all(urls, rules: Rules) -> list[Decision]:
    """`decide()` over a page's links, deduplicated by canonical URL, in page
    order - the order is the only ranking a list page gives us."""
    classifier = Classifier(rules.list_url, rules.next_urls)
    seen: set[str] = set()
    out: list[Decision] = []
    for url in urls:
        canon = canonical_uri(url)
        if not canon or canon in seen:
            continue
        seen.add(canon)
        out.append(decide(canon, rules, classifier))
    return out


__all__ = ["MODES", "Rules", "Decision", "decide", "decide_all"]
