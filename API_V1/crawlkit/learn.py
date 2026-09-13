"""Learning patterns from ticked and un-ticked links: `learn()`.

The link picker in the dashboard shows every link of a list page and the
user ticks the ones that are hits. What `learn()` gets is therefore not a
list of examples but two lists: P, the ticked links, and N, the ones shown
and NOT ticked. A learner with P alone has to guess the rest from how often
a form occurs; with N the guessing stops - an
un-ticked link that has the same form as a ticked one is a statement, and
this module's job is to turn that statement into a rule.

The steps, in order:

1. Classify every link. The list page itself, its pagination and assets are
   neither positives nor negatives; pagination only tells us the paging key.
2. Candidates are the forms of P - several are allowed, a municipality links
   building permits and planning notices under different paths.
3. For each candidate, the negatives with the same form: none - accept the
   form as it is. Some - try to *pin* a ``{lang}`` position where the ticked
   and the un-ticked values are disjoint (``/eli/oc/#/#/de`` against the
   ``/fr`` twins). ``#`` and ``*`` positions are never pinned: the number is
   the very thing that changes from hit to hit. Still conflicting - the
   negatives become exact rejects, the pattern is flagged ``needs_review``
   and a `Conflict` explains why ("same shape; only the number differs"),
   so the UI can ask "exclude just that address?".
4. In *all_except_rejected* the roles are swapped: reject patterns are the
   forms of N that are not candidate forms. A reject that would match a
   positive degrades to exact rejects. Legal links produce no pattern while
   the source drops them anyway - the explanation says so.
5. Generalising. Two patterns that differ in exactly one literal position
   and have at least two examples each merge into one with that position
   pinned to the union (``/eli/(cc|oc)/#/#/de``). A cluster of three or
   more that are mostly single links - title slugs without a digit, like
   immoscout's ``wohnung-mieten-zuerich`` - becomes one pattern with ``*``
   at that position (``/{lang}/d/*/#``). Never across hosts.
6. Merge with what the source already has. An exact reject beats a pattern;
   a ticked link beats a stale exact reject.

Everything here is pure: the same input gives the same `LearnResult`, and
nothing is stored. The dashboard's ``/learn`` endpoint returns
`LearnResult.to_dict()` unchanged.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Iterable
from urllib.parse import urlsplit

from crawlkit.classify import Classifier
from crawlkit.forms import Form, Pattern, Segment, split_form
from crawlkit.hashing import canonical_uri

MODES = ("selected", "all_except_rejected", "exact")

#: Widening merges two patterns only when each is backed by this many links.
#: One example is an accident; two are a shape.
MIN_WIDEN_EXAMPLES = 2


@dataclass(frozen=True)
class LearnedPattern:
    """One row of ``scraper_config.source_patterns`` before it is stored.

    ``examples`` is how many links of this round the pattern explains,
    ``example_url`` the first of them - the picker shows both next to the
    label. ``origin`` is ``learned`` for what this module produced and is
    carried over unchanged from rows that already existed.

    >>> p = LearnedPattern(split_form("https://a.example/rent/4001231").pattern,
    ...                    "accept", 20, "https://a.example/rent/4001231")
    >>> p.label, p.needs_review
    ('/rent/#', False)
    >>> sorted(p.to_row())
    ['bool_needs_review', 'integer_examples', 'json_form', 'text_example_url', 'text_kind', 'text_label', 'text_origin', 'text_regex']
    >>> LearnedPattern.from_row(p.to_row()) == p
    True
    """

    pattern: Pattern
    kind: str
    examples: int = 0
    example_url: str = ""
    needs_review: bool = False
    origin: str = "learned"

    @property
    def label(self) -> str:
        return self.pattern.label()

    @property
    def regex(self) -> str:
        return self.pattern.to_regex()

    def to_row(self) -> dict:
        """The table shape (column names as in ``03-scraper.sql``)."""
        return {
            "text_kind": self.kind,
            "text_label": self.label,
            "text_regex": self.regex,
            "json_form": self.pattern.to_dict(),
            "text_origin": self.origin,
            "bool_needs_review": self.needs_review,
            "text_example_url": self.example_url,
            "integer_examples": self.examples,
        }

    @staticmethod
    def from_row(row) -> "LearnedPattern":
        """Accepts a table row (dict) or a `LearnedPattern` unchanged."""
        if isinstance(row, LearnedPattern):
            return row
        return LearnedPattern(
            pattern=Pattern.from_json(row["json_form"]),
            kind=str(row.get("text_kind") or "accept"),
            examples=int(row.get("integer_examples") or 0),
            example_url=str(row.get("text_example_url") or ""),
            needs_review=bool(row.get("bool_needs_review", False)),
            origin=str(row.get("text_origin") or "learned"),
        )

    def _with(self, **changes) -> "LearnedPattern":
        values = dict(pattern=self.pattern, kind=self.kind, examples=self.examples,
                      example_url=self.example_url, needs_review=self.needs_review,
                      origin=self.origin)
        values.update(changes)
        return LearnedPattern(**values)


@dataclass(frozen=True)
class Conflict:
    """A pattern that could not tell the ticked from the un-ticked links.

    ``negatives`` are the addresses that became exact rejects because of it;
    ``question`` is what the UI should ask - the user, not the code, knows
    whether the un-ticked link was an oversight or the one exception.
    """

    label: str
    reason: str
    positives: tuple[str, ...]
    negatives: tuple[str, ...]
    question: str = "Exclude just that address?"

    def to_dict(self) -> dict:
        return {"label": self.label, "reason": self.reason,
                "positives": list(self.positives), "negatives": list(self.negatives),
                "question": self.question}


@dataclass
class LearnResult:
    """What `learn()` returns and the ``/learn`` endpoint serialises.

    ``exact_monitors`` is only filled in *exact* mode, where the ticked
    addresses themselves are the configuration; the other fields are empty
    there. ``paging_param`` is the query key the list turns pages with, as
    it was written on the page (``currentPage``, not ``currentpage``).
    """

    accept: list[LearnedPattern] = field(default_factory=list)
    reject: list[LearnedPattern] = field(default_factory=list)
    exact_rejects: list[str] = field(default_factory=list)
    paging_param: str | None = None
    conflicts: list[Conflict] = field(default_factory=list)
    explanation: str = ""
    exact_monitors: list[str] = field(default_factory=list)

    @property
    def needs_review(self) -> bool:
        return any(p.needs_review for p in self.accept + self.reject)

    def labels(self, kind: str = "accept") -> list[str]:
        return [p.label for p in (self.accept if kind == "accept" else self.reject)]

    def to_dict(self) -> dict:
        return {
            "accept": [p.to_row() for p in self.accept],
            "reject": [p.to_row() for p in self.reject],
            "exact_rejects": list(self.exact_rejects),
            "exact_monitors": list(self.exact_monitors),
            "paging_param": self.paging_param,
            "conflicts": [c.to_dict() for c in self.conflicts],
            "explanation": self.explanation,
            "needs_review": self.needs_review,
        }


@dataclass(frozen=True)
class _Link:
    canon: str
    form: Form
    cls: str


# ---------------------------------------------------------------------------
# input
# ---------------------------------------------------------------------------

def _normalise_links(links, ticked) -> tuple[list[str], set[str]]:
    """Canonical, de-duplicated, in page order; ticked as a canonical set.

    ``links`` may be plain URLs or the picker's ``{url, ticked}`` dicts, in
    which case ``ticked`` may be left out. A ticked address that is not among
    the shown ones still counts as a positive - the API is not required to
    send the whole page twice.
    """
    shown: list[str] = []
    seen: set[str] = set()
    ticked_set: set[str] = set()
    for item in links or ():
        if isinstance(item, dict):
            url = item.get("url") or ""
            is_ticked = bool(item.get("ticked"))
        else:
            url, is_ticked = item, False
        canon = canonical_uri(str(url))
        if not canon:
            continue
        if canon not in seen:
            seen.add(canon)
            shown.append(canon)
        if is_ticked:
            ticked_set.add(canon)
    for url in ticked or ():
        canon = canonical_uri(str(url))
        if canon:
            ticked_set.add(canon)
            if canon not in seen:
                seen.add(canon)
                shown.append(canon)
    return shown, ticked_set


def _split_existing(existing) -> tuple[list[LearnedPattern], set[str], set[str]]:
    """Existing rows as (patterns, exact monitors, exact rejects).

    ``existing`` may be None, a list of pattern rows, or the draft dict with
    ``patterns`` and ``exact_urls`` - the shape ``Rules.from_config()`` reads.
    """
    if not existing:
        return [], set(), set()
    rows = existing.get("patterns") or () if isinstance(existing, dict) else existing
    patterns = [LearnedPattern.from_row(row) for row in rows]
    monitors: set[str] = set()
    rejects: set[str] = set()
    if isinstance(existing, dict):
        for row in existing.get("exact_urls") or ():
            canon = canonical_uri(str(row.get("text_url_canonical") or row.get("url") or ""))
            if canon:
                (monitors if row.get("text_kind") == "monitor" else rejects).add(canon)
        for url in existing.get("exact_rejects") or ():
            canon = canonical_uri(str(url))
            if canon:
                rejects.add(canon)
        for url in existing.get("exact_monitors") or ():
            canon = canonical_uri(str(url))
            if canon:
                monitors.add(canon)
    return patterns, monitors, rejects


# ---------------------------------------------------------------------------
# separating positives from negatives of the same form
# ---------------------------------------------------------------------------

def _separate(form: Pattern, keep: list[_Link], drop: list[_Link]) -> tuple[Pattern, list[_Link]]:
    """Pin ``form`` so that it matches ``keep`` and not ``drop``.

    Only ``{lang}`` positions can be pinned: two links of the same form have
    the same literals by construction, and pinning a number would turn the
    pattern into a list of the ids seen today. Returns the (possibly pinned)
    pattern and the links of ``drop`` it still matches.

    >>> P = split_form("https://a.example/de/d/bmw-320d-12345678")
    >>> N = split_form("https://a.example/fr/d/bmw-320d-12345678")
    >>> P.pattern == N.pattern
    True
    >>> pinned, left = _separate(P.pattern, [_Link(P.pattern.host, P, "candidate")],
    ...                          [_Link(N.pattern.host, N, "candidate")])
    >>> pinned.label(), left
    ('/de/d/*', [])
    """
    if not drop:
        return form, []
    for i, seg in enumerate(form.segments):
        if seg.kind != "lang" or i in form.pins:
            continue
        keep_values = {link.form.values[i] for link in keep}
        drop_values = {link.form.values[i] for link in drop}
        if keep_values.isdisjoint(drop_values):
            return form.pin(i, keep_values), []
    return form, list(drop)


def _conflict_reason(form: Pattern, keep: list[_Link], drop: list[_Link]) -> str:
    """Why the two sides could not be told apart, in one clause."""
    kinds: set[str] = set()
    for i, seg in enumerate(form.segments):
        keep_values = {link.form.values[i] for link in keep}
        drop_values = {link.form.values[i] for link in drop}
        if keep_values != drop_values:
            kinds.add(seg.kind)
    if kinds == {"num"}:
        return "same shape; only the number differs"
    if kinds == {"any"}:
        return "same shape; only the slug differs"
    if kinds == {"lang"}:
        return "same shape; the language codes overlap"
    if kinds:
        return "same shape; only the number and the slug differ"
    if form.query_keys:
        return "same shape; only the query values differ"
    return "the same address shape"


def _examples(links: list[_Link], limit: int = 3) -> tuple[str, ...]:
    return tuple(link.canon for link in links[:limit])


# ---------------------------------------------------------------------------
# generalising: widening and wildcards
# ---------------------------------------------------------------------------

def _values_at(pattern: Pattern, i: int) -> tuple[str, ...] | None:
    """The literal values a pattern allows at position ``i``, or None when the
    position is variable (``#``, ``*``, ``{lang}`` without a pin)."""
    pins = pattern.pins
    if i in pins:
        return pins[i]
    seg = pattern.segments[i]
    return (seg.value,) if seg.kind == "lit" else None


def _one_literal_apart(a: Pattern, b: Pattern) -> int | None:
    """The single position where ``a`` and ``b`` differ by a literal, or None.

    A ``*`` on one side against a literal on the other counts as that
    position too: a 26-character slug and a 20-character one are the same
    kind of thing, and the cluster must see them together.
    """
    if (a.host != b.host or a.query_keys != b.query_keys
            or len(a.segments) != len(b.segments)):
        return None
    differing = []
    for i, (sa, sb) in enumerate(zip(a.segments, b.segments)):
        va, vb = _values_at(a, i), _values_at(b, i)
        if va is None and vb is None:
            # A variable position must be the same variable on both sides,
            # and pinned the same way (the pin is part of what it accepts).
            if sa.kind != sb.kind or a.pins.get(i) != b.pins.get(i):
                return None
            continue
        if va is None or vb is None:
            wild = sa if va is None else sb
            if wild.kind != "any" or i in (a.pins if va is None else b.pins):
                return None
            differing.append(i)
        elif set(va) != set(vb):
            differing.append(i)
    return differing[0] if len(differing) == 1 else None


@dataclass
class _Candidate:
    pattern: Pattern
    links: list[_Link]

    @property
    def examples(self) -> int:
        return len(self.links)


def _generalise(candidates: list[_Candidate]) -> tuple[list[_Candidate], list[str]]:
    """Step 5, in two flavours, on a cluster of candidates that differ in
    exactly one literal position:

    * most of the cluster are single links (``/{lang}/d/wohnung-mieten-zuerich/#``,
      ``/{lang}/d/haus-kaufen-uster/#`` ...) - the position is a title slug,
      and the cluster becomes one pattern with ``*`` there. Three distinct
      values at least: two words are a coincidence, three are a shape.
    * otherwise the members with at least `MIN_WIDEN_EXAMPLES` links each
      are pinned to the union (``/eli/(cc|oc)/#/#/de``); single links stay
      their own pattern rather than turning the union into a list of what
      happened to be on the page today.

    Never across hosts: `_one_literal_apart()` refuses a different host.
    """
    notes: list[str] = []
    result = list(candidates)
    changed = True
    while changed:
        changed = False
        for x, anchor in enumerate(result):
            clusters: dict[int, list[int]] = {}
            for y, other in enumerate(result):
                if y == x:
                    continue
                pos = _one_literal_apart(anchor.pattern, other.pattern)
                if pos is not None:
                    clusters.setdefault(pos, []).append(y)
            for pos, members in clusters.items():
                idx = [x] + members
                group = [result[k] for k in idx]
                singles = sum(1 for c in group if c.examples < MIN_WIDEN_EXAMPLES)
                # No wildcard at the root: "/impressum", "/agb" and "/kontakt"
                # are three single links under the empty parent path and
                # would otherwise look like a list.
                if len(group) >= 3 and singles * 2 >= len(group) and len(anchor.pattern.segments) >= 2:
                    segments = list(anchor.pattern.segments)
                    segments[pos] = Segment("any")
                    pins = {k: v for k, v in anchor.pattern.pins.items() if k != pos}
                    merged = Pattern(anchor.pattern.host, tuple(segments), anchor.pattern.query_keys,
                                     tuple(sorted(pins.items())))
                    links = [l for c in group for l in c.links]
                    notes.append(f"{len(group)} slugs under {merged.label()} merged into one pattern")
                else:
                    strong = [c for c in group
                              if c.examples >= MIN_WIDEN_EXAMPLES and _values_at(c.pattern, pos) is not None]
                    if len(strong) < 2:
                        continue
                    idx = [k for k in idx if result[k] in strong]
                    values = sorted({v for c in strong for v in _values_at(c.pattern, pos)})
                    segments = list(strong[0].pattern.segments)
                    segments[pos] = Segment("lit", values[0])
                    merged = Pattern(strong[0].pattern.host, tuple(segments),
                                     strong[0].pattern.query_keys, strong[0].pattern.pinned).pin(pos, values)
                    links = [l for c in strong for l in c.links]
                    notes.append(" and ".join(c.pattern.label() for c in strong)
                                 + f" merged into {merged.label()}")
                result = [c for k, c in enumerate(result) if k not in idx] + [_Candidate(merged, links)]
                changed = True
                break
            if changed:
                break
    # Generalising can make two candidates identical (a cluster of short
    # slugs next to a form that was ``*`` from the start); fold them.
    folded: dict[Pattern, _Candidate] = {}
    for cand in result:
        if cand.pattern in folded:
            folded[cand.pattern].links.extend(cand.links)
        else:
            folded[cand.pattern] = _Candidate(cand.pattern, list(cand.links))
    return list(folded.values()), notes


def _build_side(keep: list[_Link], drop: list[_Link]
                ) -> tuple[list[tuple[Pattern, list[_Link], list[_Link]]], list[str]]:
    """Patterns that explain ``keep`` and, as far as possible, not ``drop``.

    Returns (pattern, the links it explains, the links of ``drop`` it still
    matches) per candidate - the caller decides what an unresolved link
    means for its side - and the widening notes for the explanation.
    """
    groups: dict[Pattern, list[_Link]] = {}
    for link in keep:
        groups.setdefault(link.form.pattern, []).append(link)
    candidates, notes = _generalise([_Candidate(form, links) for form, links in groups.items()])
    out = []
    for cand in candidates:
        hit = [l for l in drop if cand.pattern.matches_form(l.form)]
        pattern, unresolved = _separate(cand.pattern, cand.links, hit)
        out.append((pattern, cand.links, unresolved))
    return out, notes


# ---------------------------------------------------------------------------
# learn
# ---------------------------------------------------------------------------

def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def learn(list_url: str, links, ticked: Iterable[str] | None = None,
          mode: str = "selected", existing=None, *,
          next_urls=(), drop_legal_links: bool = True) -> LearnResult:
    """Patterns from the ticked (P) and un-ticked (N) links of one list page.

    Homegate-like, *selected*: twenty ticked hits, pagination, a sibling list
    and the legal footer un-ticked - one pattern, one paging key, no rejects.

    >>> L = "https://www.homegate.ch/rent/apartment/city-zurich/matching-list"
    >>> hits = [f"https://www.homegate.ch/rent/400123{n:02d}" for n in range(20)]
    >>> shown = hits + [L + "?ep=2", L + "?ep=3", "https://www.homegate.ch/impressum",
    ...                 "https://www.homegate.ch/rent/apartment/city-basel/matching-list"]
    >>> r = learn(L, shown, hits)
    >>> r.labels(), r.labels("reject"), r.paging_param, r.exact_rejects
    (['/rent/#'], [], 'ep', [])
    >>> r.explanation
    '1 rule from 20 selected links; pagination via ?ep= kept aside; /impressum left out and already on the legal list'

    The same page in *all_except_rejected*: the sibling list becomes a reject
    pattern, the legal link needs none.

    >>> r = learn(L, shown, hits, mode="all_except_rejected")
    >>> r.labels(), r.labels("reject")
    ([], ['/rent/apartment/city-basel/matching-list'])

    Ids are never pinned: ``/rent/2`` un-ticked next to ``/rent/1`` ticked is
    an exact reject and a question for the user, and ``/rent/3`` still fits.

    >>> r = learn("https://a.example/list", ["https://a.example/rent/1", "https://a.example/rent/2"],
    ...           ["https://a.example/rent/1"])
    >>> r.accept[0].label, r.accept[0].needs_review, r.exact_rejects
    ('/rent/#', True, ['https://a.example/rent/2'])
    >>> r.conflicts[0].reason
    'same shape; only the number differs'
    >>> r.accept[0].pattern.matches("https://a.example/rent/3")
    True

    *exact* keeps the ticked addresses themselves:

    >>> learn("https://a.example/list", ["https://a.example/x", "https://a.example/y"],
    ...       ["https://a.example/x"], mode="exact").exact_monitors
    ['https://a.example/x']
    """
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}, got {mode!r}")

    shown, ticked_set = _normalise_links(links, ticked)
    classifier = Classifier(list_url, next_urls)
    paging_param = classifier.paging_param
    paging_seen: set[str] = set()

    positives: list[_Link] = []
    negatives: list[_Link] = []
    dropped: Counter = Counter()
    for canon in shown:
        form = split_form(canon)
        cls = classifier.classify(canon, form)
        if cls == "pagination":
            if form.paging_param:
                paging_seen.add(form.paging_param)
            continue
        if cls in ("list_self", "asset"):
            dropped[cls] += 1
            continue
        link = _Link(canon, form, cls)
        (positives if canon in ticked_set else negatives).append(link)
    if paging_param is None and paging_seen:
        paging_param = sorted(paging_seen)[0]

    old_patterns, old_monitors, old_rejects = _split_existing(existing)
    # A ticked link beats a stale exact reject: the tick is the newer word.
    exact_rejects: set[str] = {u for u in old_rejects if u not in ticked_set}
    exact_monitors: set[str] = set(old_monitors)
    conflicts: list[Conflict] = []
    notes: list[str] = []
    accept: list[LearnedPattern] = []
    reject: list[LearnedPattern] = []

    # Files are the business of the file rules on subpages, and `decide()`
    # never fetches them as pages - a pattern learned from them would promise
    # what the crawler will not do. They are kept out of P and N and named.
    files = [l for l in positives if l.cls == "file"]
    positives = [l for l in positives if l.cls != "file"]
    negatives = [l for l in negatives if l.cls != "file"]

    legal_negatives = [l for l in negatives if l.cls == "legal"] if drop_legal_links else []
    if drop_legal_links:
        negatives = [l for l in negatives if l.cls != "legal"]

    if mode == "exact":
        exact_monitors.update(link.canon for link in positives)

    elif mode == "selected":
        built, widened = _build_side(positives, negatives)
        for pattern, pos, unresolved in built:
            if unresolved:
                exact_rejects.update(l.canon for l in unresolved)
                conflicts.append(Conflict(pattern.label(), _conflict_reason(pattern, pos, unresolved),
                                          _examples(pos), tuple(l.canon for l in unresolved)))
            accept.append(LearnedPattern(pattern, "accept", len(pos), pos[0].canon,
                                         needs_review=bool(unresolved)))
        notes.extend(widened)

    else:  # all_except_rejected
        built, widened = _build_side(negatives, positives)
        for pattern, negs, unresolved in built:
            if unresolved:
                # The reject would also match ticked links - it degrades to
                # exact rejects of the un-ticked addresses, and the user is
                # asked whether that is what was meant.
                exact_rejects.update(l.canon for l in negs)
                conflicts.append(Conflict(pattern.label(), _conflict_reason(pattern, unresolved, negs),
                                          _examples(unresolved), tuple(l.canon for l in negs)))
                continue
            reject.append(LearnedPattern(pattern, "reject", len(negs), negs[0].canon))
        notes.extend(widened)

    # -- step 6: merge with what the source already has -----------------------
    learned_forms = {p.pattern for p in accept + reject}
    for old in old_patterns:
        if old.pattern in learned_forms:
            continue  # re-learned this round; the fresh counts win
        if old.kind == "accept":
            hit = [l for l in negatives if old.pattern.matches_form(l.form)]
            if hit:
                exact_rejects.update(l.canon for l in hit)
                conflicts.append(Conflict(old.label, "an existing rule also matches links you did not select",
                                          (), tuple(l.canon for l in hit)))
                old = old._with(needs_review=True)
            accept.append(old)
        else:
            hit = [l for l in positives if old.pattern.matches_form(l.form)]
            if hit:
                # A reject that contradicts a tick is dropped; the un-ticked
                # links it covered stay rejected by address.
                covered = [l for l in negatives if old.pattern.matches_form(l.form)]
                exact_rejects.update(l.canon for l in covered)
                notes.append(f"{old.label} dropped: it would reject "
                             f"{_plural(len(hit), 'selected link')}")
                continue
            reject.append(old)

    accept.sort(key=lambda p: (-p.examples, p.label))
    reject.sort(key=lambda p: (-p.examples, p.label))

    # -- the explanation --------------------------------------------------------
    parts: list[str] = []
    n_ticked = len(positives)
    if mode == "exact":
        parts.append(f"{_plural(len(exact_monitors), 'address')} monitored exactly")
    elif mode == "selected":
        parts.append(f"{_plural(len(accept), 'rule')} from {_plural(n_ticked, 'selected link')}")
    else:
        if reject:
            parts.append(f"{_plural(len(reject), 'reject pattern')} from "
                         f"{_plural(len(negatives), 'link you did not select')}")
        else:
            parts.append(f"no patterns needed: everything except what is rejected is collected"
                         + (f" ({_plural(n_ticked, 'selected link')})" if n_ticked else ""))
    if paging_param:
        parts.append(f"pagination via ?{paging_param}= kept aside")
    if legal_negatives:
        if len(legal_negatives) == 1:
            parts.append(f"{_path(legal_negatives[0].canon)} left out and already on the legal list")
        else:
            parts.append(f"{len(legal_negatives)} legal links left out and already on the legal list")
    new_rejects = sorted(exact_rejects - old_rejects)
    if new_rejects:
        shown_rejects = ", ".join(_path(u) for u in new_rejects[:3])
        more = f" and {len(new_rejects) - 3} more" if len(new_rejects) > 3 else ""
        parts.append(f"{_plural(len(new_rejects), 'exact reject')}: {shown_rejects}{more} "
                     f"({'; '.join(sorted({c.reason for c in conflicts}))}) - review the pattern")
    if files:
        parts.append(f"{_plural(len(files), 'file link')} selected - files are chosen in the files step")
    if dropped["asset"]:
        parts.append(f"{_plural(dropped['asset'], 'asset link')} ignored")
    parts.extend(notes)

    return LearnResult(accept=accept, reject=reject, exact_rejects=sorted(exact_rejects),
                       paging_param=paging_param, conflicts=conflicts,
                       explanation="; ".join(parts), exact_monitors=sorted(exact_monitors))


def _path(canon: str) -> str:
    """The path of a canonical URL, for the explanation - the host is the
    list page's host and would only repeat itself."""
    parts = urlsplit(canon)
    return (parts.path or "/") + (f"?{parts.query}" if parts.query else "")


__all__ = ["MODES", "MIN_WIDEN_EXAMPLES", "Conflict", "LearnResult", "LearnedPattern", "learn"]
