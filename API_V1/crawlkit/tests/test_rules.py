"""Tests for the pure rules: forms, classify, learn, decide.

These run without a network and without a database: the rules are where the
mistakes live, and they are pure. Every worked example is here as a test,
so a change to the rules shows up as a named scenario and
not as a surprise on a customer's list page.

    python -m pytest crawlkit -q
"""

from __future__ import annotations

import json
import random
import re
import time

import pytest

from crawlkit.apply import Decision, Rules, decide, decide_all
from crawlkit.classify import Classifier, classify, looks_like_next_label
from crawlkit.forms import LANG_CODES, PAGING_KEYS, Pattern, Segment, form_of, split_form
from crawlkit.hashing import canonical_uri
from crawlkit.learn import LearnedPattern, learn
from crawlkit.legal import is_legal, legal_word

# ---------------------------------------------------------------------------
# forms: the segment rules
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("piece, kind, value", [
    ("2024", "num", ""),
    ("0", "num", ""),
    ("4001231", "num", ""),
    ("bmw-320d-12345678", "any", ""),            # contains a digit
    ("objekt-a1b2c3", "any", ""),
    ("neubau-einfamilienhaus-musterweg", "any", ""),   # > 24 characters
    ("de", "lang", ""),
    ("FR", "lang", ""),
    ("oc", "lit", "oc"),                          # fedlex: not a language
    ("cc", "lit", "cc"),
    ("d", "lit", "d"),
    ("News", "lit", "news"),
    ("matching-list", "lit", "matching-list"),
])
def test_segment_rules(piece, kind, value):
    seg = Segment.of(piece)
    assert (seg.kind, seg.value) == (kind, value)
    assert seg.accepts(piece)


@pytest.mark.parametrize("url, label, keys, paging", [
    ("https://www.homegate.ch/rent/4001231", "/rent/#", (), None),
    ("https://www.homegate.ch/rent/apartment/city-zurich/matching-list?ep=2",
     "/rent/apartment/city-zurich/matching-list", (), "ep"),
    ("https://www.immoscout24.ch/de/d/wohnung-mieten-zuerich/4001231", "/{lang}/d/wohnung-mieten-zuerich/#", (), None),
    ("https://www.immoscout24.ch/de/d/4-zimmer-wohnung-mieten-in-zuerich/4001231", "/{lang}/d/*/#", (), None),
    ("https://www.autoscout24.ch/de/d/bmw-320d-12345678", "/{lang}/d/*", (), None),
    ("https://www.fedlex.admin.ch/eli/oc/2024/123/de", "/eli/oc/#/#/{lang}", (), None),
    ("https://www.fedlex.admin.ch/de/search?currentPage=2&type=oc", "/{lang}/search?type", ("type",), "currentPage"),
    ("https://www.autoscout24.ch/de/autos/alle-marken?page=1&sort=price", "/{lang}/autos/alle-marken?sort", ("sort",), "page"),
    ("https://a.example/", "/", (), None),
    ("https://a.example/d?id=7&lang=de", "/d?id&lang", ("id", "lang"), None),
])
def test_form_label_query_keys_and_paging(url, label, keys, paging):
    form = split_form(url)
    assert form.pattern.label() == label
    assert form.pattern.query_keys == keys
    assert form.paging_param == paging


def test_paging_keys_are_removed_from_the_form():
    for key in PAGING_KEYS:
        assert form_of(f"https://a.example/list?{key}=2") == form_of("https://a.example/list")


def test_pattern_json_round_trip():
    p = split_form("https://www.fedlex.admin.ch/eli/oc/2024/123/de").pattern
    p = p.pin(4, ["de"]).pin(1, ["cc", "oc"])
    text = p.to_json()
    assert Pattern.from_json(text) == p
    assert Pattern.from_json(json.loads(text)) == p          # jsonb comes back as a dict
    assert Pattern.from_json(p.to_dict()) == p
    assert json.loads(text)["pinned"] == {"1": ["cc", "oc"], "4": ["de"]}


def test_pinning_never_at_variable_positions_by_learn_but_allowed_by_class():
    # The class loads whatever an older row holds; the never-pin-ids rule is
    # a rule of learn(), tested below.
    p = split_form("https://a.example/rent/1").pattern
    assert p.pin(1, ["1"]).matches("https://a.example/rent/1")
    assert not p.pin(1, ["1"]).matches("https://a.example/rent/2")
    with pytest.raises(ValueError):
        p.pin(7, ["x"])


# ---------------------------------------------------------------------------
# regex <-> tuple equivalence on generated URLs
# ---------------------------------------------------------------------------

_HOSTS = ["a.example", "www.b.example"]
_LITERALS = ["rent", "news", "d", "eli", "oc", "cc", "matching-list", "Auflagen"]
_LANGS = sorted(LANG_CODES)[:6] + ["tv", "xx"]
_SLUGS = ["bmw-320d-12345678", "haus-3", "neubau-einfamilienhaus-musterweg-lang", "objekt-a1b2c3"]


def _random_url(rng: random.Random) -> str:
    pieces = []
    for _ in range(rng.randint(0, 5)):
        pool = rng.choice([_LITERALS, _LANGS, _SLUGS, ["7", "2024", "4001231"]])
        pieces.append(rng.choice(pool))
    url = f"https://{rng.choice(_HOSTS)}/" + "/".join(pieces)
    if pieces and rng.random() < 0.5:
        url += "/"
    keys = rng.sample(["id", "lang", "type", "page", "ep", "currentPage", "q"], rng.randint(0, 3))
    if keys:
        url += "?" + "&".join(f"{k}={rng.randint(0, 9)}" for k in keys)
    return url


def test_regex_and_tuple_agree_on_generated_urls():
    rng = random.Random(20240906)
    urls = [_random_url(rng) for _ in range(200)]
    patterns = [split_form(u).pattern for u in urls]
    # Pin a few of them, at language and literal positions, as learn() would.
    pinned = []
    for p in patterns:
        for i, seg in enumerate(p.segments):
            if seg.kind in ("lang", "lit"):
                pinned.append(p.pin(i, [seg.value or "de", "fr"]))
                break
    disagreements = []
    for p in patterns + pinned:
        rx = p.compile()
        for u in urls:
            canon = canonical_uri(u)
            if bool(rx.match(canon)) != p.matches(canon):
                disagreements.append((p.label(), canon))
    assert disagreements == []


def test_regex_is_anchored_and_ignores_paging_only():
    p = split_form("https://a.example/rent/4001231").pattern
    rx = p.compile()
    assert rx.match("https://a.example/rent/4001299")
    assert rx.match("https://a.example/rent/4001299/?page=2")
    assert not rx.match("https://a.example/rent/4001299?sort=price")
    assert not rx.match("https://a.example/rent/4001299/details")
    assert not rx.match("https://other.example/rent/4001299")
    assert re.compile(p.to_regex().replace("PG", "x")).pattern.startswith("^https?://")


# ---------------------------------------------------------------------------
# legal and classify
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("url, word", [
    ("https://a.example/impressum", "impressum"),
    ("https://a.example/de/agb.html", "agb"),
    ("https://a.example/gtc", "gtc"),
    ("https://a.example/terms", "terms"),
    ("https://a.example/en/terms-and-conditions", "terms-and-conditions"),
    ("https://a.example/legal-notice", "legal-notice"),
    ("https://a.example/cookie-policy", "cookie-policy"),
    ("https://a.example/privacy-policy?x=1", "privacy-policy"),
    ("https://a.example/datenschutz#top", "datenschutz"),
    ("https://a.example/rent/4001231", None),
    ("https://a.example/searchlight/1", None),
    ("https://a.example/agbau/1", None),
    ("https://contact.example/rent/1", None),          # host, not path
])
def test_legal_words(url, word):
    assert legal_word(url) == word
    assert is_legal(url) is (word is not None)


L_HOMEGATE = "https://www.homegate.ch/rent/apartment/city-zurich/matching-list"


@pytest.mark.parametrize("url, cls", [
    (L_HOMEGATE, "list_self"),
    (L_HOMEGATE + "#results", "list_self"),
    (L_HOMEGATE + "?utm_source=x", "list_self"),
    (L_HOMEGATE + "?ep=2", "pagination"),
    (L_HOMEGATE + "?ep=3", "pagination"),
    ("https://www.homegate.ch/impressum", "legal"),
    ("https://www.homegate.ch/static/app.css", "asset"),
    ("https://www.homegate.ch/img/logo.svg", "asset"),
    ("https://www.homegate.ch/fonts/x.woff2", "asset"),
    ("https://www.homegate.ch/video/tour.mp4", "asset"),
    ("https://cdn.other.example/x", "external"),
    ("https://homegate.ch/rent/4001231", "candidate"),      # www. does not count
    ("https://www.homegate.ch/files/expose-4001231.pdf", "file"),
    ("https://www.homegate.ch/files/floorplan-4001231.csv", "file"),
    ("https://www.homegate.ch/rent/4001231", "candidate"),
    ("https://www.homegate.ch/rent/apartment/city-basel/matching-list", "candidate"),
])
def test_classify(url, cls):
    assert classify(url, L_HOMEGATE) == cls


def test_classify_next_url_from_the_page_counts_as_pagination():
    nxt = "https://www.homegate.ch/liste/seite-2"
    assert classify(nxt, L_HOMEGATE, next_urls=[nxt]) == "pagination"
    assert classify(nxt, L_HOMEGATE) == "candidate"


def test_classify_list_already_on_page_two():
    c = Classifier(L_HOMEGATE + "?ep=2")
    assert c.paging_param == "ep"
    assert c.classify(L_HOMEGATE + "?ep=3") == "pagination"
    assert c.classify(L_HOMEGATE) == "pagination"


@pytest.mark.parametrize("text, expected", [
    ("Weiter", True), ("nächste Seite »", True), ("›", True), ("Next", True),
    ("suivant", True), ("Mehr laden", True),
    ("vorherige", False), ("« zurück", False), ("Weitere Informationen", False),
    ("", False), ("Weiter zu den nächsten Schritten der Anmeldung", False),
])
def test_next_label_words(text, expected):
    assert looks_like_next_label(text) is expected


# ---------------------------------------------------------------------------
# learn: the worked examples
# ---------------------------------------------------------------------------

HOMEGATE_HITS = [f"https://www.homegate.ch/rent/400123{n:02d}" for n in range(20)]
HOMEGATE_FOOTER = ["https://www.homegate.ch/impressum", "https://www.homegate.ch/agb",
                   "https://www.homegate.ch/datenschutz"]
HOMEGATE_SIBLING = "https://www.homegate.ch/rent/apartment/city-basel/matching-list"
HOMEGATE_SHOWN = (HOMEGATE_HITS + [L_HOMEGATE + "?ep=2", L_HOMEGATE + "?ep=3", L_HOMEGATE,
                                   HOMEGATE_SIBLING, "https://www.homegate.ch/static/app.css"]
                  + HOMEGATE_FOOTER)


def test_homegate_selected():
    r = learn(L_HOMEGATE, HOMEGATE_SHOWN, HOMEGATE_HITS)
    assert r.labels() == ["/rent/#"]
    assert r.accept[0].examples == 20
    assert r.accept[0].needs_review is False
    assert r.reject == [] and r.exact_rejects == [] and r.conflicts == []
    assert r.paging_param == "ep"
    assert "20 selected links" in r.explanation
    assert "legal list" in r.explanation
    # The result round-trips through the API shape into rules the crawler uses.
    rules = Rules.from_config({"text_list_url": L_HOMEGATE, "text_mode": "selected",
                               "patterns": r.to_dict()["accept"]})
    assert decide("https://www.homegate.ch/rent/4009999", rules).accepted
    assert decide(HOMEGATE_SIBLING, rules).by == "no pattern"


def test_homegate_all_except_rejected():
    ticked = HOMEGATE_HITS                                  # default: everything but legal/pagination/assets
    r = learn(L_HOMEGATE, HOMEGATE_SHOWN, ticked + [HOMEGATE_SIBLING], mode="all_except_rejected")
    assert r.accept == [] and r.reject == []                # nothing un-ticked but the legal footer
    assert "legal" in r.explanation and "no patterns needed" in r.explanation
    # Un-tick the sibling list: it becomes a reject pattern.
    r = learn(L_HOMEGATE, HOMEGATE_SHOWN, ticked, mode="all_except_rejected")
    assert r.labels("reject") == ["/rent/apartment/city-basel/matching-list"]
    assert r.accept == []
    rules = Rules.from_config({"text_list_url": L_HOMEGATE, "text_mode": "all_except_rejected",
                               "patterns": r.to_dict()["reject"]})
    assert decide(HOMEGATE_SIBLING, rules).by == "/rent/apartment/city-basel/matching-list"
    assert decide("https://www.homegate.ch/rent/4009999", rules).by == "everything else"
    assert decide("https://www.homegate.ch/impressum", rules).by == "legal"


def test_legal_footer_in_all_except_with_switch_off_becomes_reject_patterns():
    r = learn(L_HOMEGATE, HOMEGATE_SHOWN, HOMEGATE_HITS + [HOMEGATE_SIBLING],
              mode="all_except_rejected", drop_legal_links=False)
    assert sorted(r.labels("reject")) == ["/agb", "/datenschutz", "/impressum"]
    assert "legal list" not in r.explanation


L_IMMOSCOUT = "https://www.immoscout24.ch/de/immobilien/mieten/ort-zuerich"
# Real immoscout slugs are short and digit-free ("wohnung-mieten-zuerich"):
# literals under the segment rule, so learn() has to generalise the cluster.
IMMOSCOUT_SLUGS = ["wohnung-mieten-zuerich", "haus-kaufen-uster", "loft-mieten-altstetten",
                   "attika-mieten-oerlikon", "studio-mieten-wiedikon", "villa-kaufen-kilchberg",
                   "maisonette-mieten-seebach", "wohnung-mieten-wollishofen", "haus-mieten-affoltern",
                   "wohnung-kaufen-enge", "dachwohnung-mieten-hoengg", "wohnung-mieten-albisrieden"]
IMMOSCOUT_HITS = [f"https://www.immoscout24.ch/de/d/{slug}/400{n:04d}" for n, slug in enumerate(IMMOSCOUT_SLUGS)]


def test_immoscout_one_unticked_listing_needs_review():
    unticked = IMMOSCOUT_HITS[5]
    ticked = [u for u in IMMOSCOUT_HITS if u != unticked]
    r = learn(L_IMMOSCOUT, IMMOSCOUT_HITS + ["https://www.immoscout24.ch/impressum"], ticked)
    assert r.labels() == ["/{lang}/d/*/#"]
    assert r.accept[0].needs_review is True
    assert r.exact_rejects == [canonical_uri(unticked)]
    assert len(r.conflicts) == 1
    assert r.conflicts[0].reason == "same shape; only the number and the slug differ"
    assert r.conflicts[0].question == "Exclude just that address?"
    assert "exact reject" in r.explanation and "review" in r.explanation
    # Applied: the exact reject wins over the pattern, everything else is taken.
    rules = Rules.from_config({"text_list_url": L_IMMOSCOUT, "text_mode": "selected",
                               "patterns": r.to_dict()["accept"],
                               "exact_urls": [{"text_kind": "reject", "text_url_canonical": u}
                                              for u in r.exact_rejects]})
    assert decide(unticked, rules).by == "exact"
    assert decide(IMMOSCOUT_HITS[0], rules).accepted


L_AUTOSCOUT = "https://www.autoscout24.ch/de/autos/alle-marken"
AUTOSCOUT_DE = [f"https://www.autoscout24.ch/de/d/bmw-320d-{n:08d}" for n in range(10)]
AUTOSCOUT_FR = [u.replace("/de/d/", "/fr/d/") for u in AUTOSCOUT_DE]


def test_autoscout_fr_twins_are_pinned_away():
    shown = AUTOSCOUT_DE + AUTOSCOUT_FR + [L_AUTOSCOUT + "?page=2", L_AUTOSCOUT + "?page=1&sort=price"]
    r = learn(L_AUTOSCOUT, shown, AUTOSCOUT_DE)
    assert r.labels() == ["/de/d/*"]
    assert r.accept[0].needs_review is False
    assert r.exact_rejects == []
    assert r.paging_param == "page"
    p = r.accept[0].pattern
    assert p.matches("https://www.autoscout24.ch/de/d/audi-a4-99999999")
    assert not p.matches("https://www.autoscout24.ch/fr/d/audi-a4-99999999")
    assert p.to_regex() == p.to_regex()  # deterministic rendering
    assert Pattern.from_json(p.to_json()) == p
    # The sort= list is another form (it carries a query key) and is not a hit.
    assert classify(L_AUTOSCOUT + "?page=1&sort=price", L_AUTOSCOUT) == "candidate"
    assert not p.matches(L_AUTOSCOUT + "?page=1&sort=price")


def test_autoscout_all_except_pins_the_reject_side():
    shown = AUTOSCOUT_DE + AUTOSCOUT_FR
    r = learn(L_AUTOSCOUT, shown, AUTOSCOUT_DE, mode="all_except_rejected")
    assert r.labels("reject") == ["/fr/d/*"]
    assert r.exact_rejects == []


L_FEDLEX = "https://www.fedlex.admin.ch/de/search?type=oc"
FEDLEX_OC_DE = [f"https://www.fedlex.admin.ch/eli/oc/2024/{n}/de" for n in range(1, 21)]
FEDLEX_OC_FR = [u[:-2] + "fr" for u in FEDLEX_OC_DE]
FEDLEX_CC_DE = [f"https://www.fedlex.admin.ch/eli/cc/2024/{n}/de" for n in range(1, 6)]
FEDLEX_CC_FR = [u[:-2] + "fr" for u in FEDLEX_CC_DE]
FEDLEX_NEXT = "https://www.fedlex.admin.ch/de/search?type=oc&currentPage=2"


def test_fedlex_pinned_language_and_paging():
    shown = FEDLEX_OC_DE + FEDLEX_OC_FR + FEDLEX_CC_DE + FEDLEX_CC_FR + [FEDLEX_NEXT]
    r = learn(L_FEDLEX, shown, FEDLEX_OC_DE, next_urls=[FEDLEX_NEXT])
    assert r.labels() == ["/eli/oc/#/#/de"]
    assert r.paging_param == "currentPage"
    assert r.reject == [] and r.exact_rejects == []
    p = r.accept[0].pattern
    assert p.matches("https://www.fedlex.admin.ch/eli/oc/2025/999/de")
    assert not p.matches("https://www.fedlex.admin.ch/eli/oc/2025/999/fr")
    assert not p.matches("https://www.fedlex.admin.ch/eli/cc/2025/999/de")


def test_fedlex_widening_when_oc_and_cc_are_both_ticked():
    shown = FEDLEX_OC_DE + FEDLEX_OC_FR + FEDLEX_CC_DE + FEDLEX_CC_FR
    r = learn(L_FEDLEX, shown, FEDLEX_OC_DE + FEDLEX_CC_DE)
    assert r.labels() == ["/eli/(cc|oc)/#/#/de"]
    assert r.accept[0].examples == 25
    assert "merged" in r.explanation
    p = r.accept[0].pattern
    assert p.matches("https://www.fedlex.admin.ch/eli/cc/2025/1/de")
    assert p.matches("https://www.fedlex.admin.ch/eli/oc/2025/1/de")
    assert not p.matches("https://www.fedlex.admin.ch/eli/oc/2025/1/fr")
    assert not p.matches("https://www.fedlex.admin.ch/eli/xx/2025/1/de")
    assert Pattern.from_json(p.to_json()) == p


def test_widening_needs_two_examples_each_and_never_crosses_hosts():
    one_cc = FEDLEX_CC_DE[:1]
    r = learn(L_FEDLEX, FEDLEX_OC_DE + one_cc, FEDLEX_OC_DE + one_cc)
    assert sorted(r.labels()) == ["/eli/cc/#/#/{lang}", "/eli/oc/#/#/{lang}"]
    other_host = [f"https://mirror.example/eli/cc/2024/{n}/de" for n in range(1, 6)]
    r = learn(L_FEDLEX, FEDLEX_OC_DE + other_host, FEDLEX_OC_DE + other_host)
    assert len(r.accept) == 2


def test_never_pin_ids():
    shown = ["https://a.example/rent/1", "https://a.example/rent/2"]
    r = learn("https://a.example/list", shown, ["https://a.example/rent/1"])
    assert r.labels() == ["/rent/#"]
    assert r.accept[0].needs_review is True
    assert r.exact_rejects == ["https://a.example/rent/2"]
    assert r.conflicts[0].reason == "same shape; only the number differs"
    assert r.accept[0].pattern.pins == {}
    rules = Rules.from_config({"text_list_url": "https://a.example/list", "text_mode": "selected",
                               "patterns": r.to_dict()["accept"],
                               "exact_urls": [{"text_kind": "reject", "text_url_canonical": u}
                                              for u in r.exact_rejects]})
    assert decide("https://a.example/rent/3", rules).accepted
    assert decide("https://a.example/rent/2", rules).by == "exact"


def test_exact_mode_monitors_the_ticked_addresses():
    shown = ["https://a.example/notice", "https://a.example/other", "https://a.example/impressum"]
    r = learn("https://a.example/", shown, ["https://a.example/notice"], mode="exact")
    assert r.exact_monitors == ["https://a.example/notice"]
    assert r.accept == [] and r.reject == [] and r.exact_rejects == []
    assert "monitored exactly" in r.explanation
    rules = Rules.from_config({"text_list_url": "https://a.example/", "text_mode": "exact",
                               "exact_urls": [{"text_kind": "monitor", "text_url_canonical": u}
                                              for u in r.exact_monitors]})
    assert decide("https://a.example/notice", rules).by == "monitor"
    assert decide("https://a.example/other", rules).by == "exact mode"


def test_learn_accepts_picker_dicts_and_is_pure():
    links = [{"url": u, "ticked": True} for u in HOMEGATE_HITS] + \
            [{"url": u, "ticked": False} for u in HOMEGATE_FOOTER + [HOMEGATE_SIBLING]]
    a = learn(L_HOMEGATE, links)
    b = learn(L_HOMEGATE, links)
    assert a == b
    assert a.labels() == ["/rent/#"]
    assert json.dumps(a.to_dict())          # JSON-serialisable as it is


def test_learn_several_candidate_forms():
    bau = [f"https://a.example/bau/{n}" for n in range(3)]
    plan = [f"https://a.example/plan/{n}" for n in range(3)]
    r = learn("https://a.example/", bau + plan + ["https://a.example/kontakt"], bau + plan)
    # Two candidate forms are allowed; they differ in exactly one literal
    # position with >= 2 examples each, so widening folds them into one
    # pattern that matches exactly the union of the two.
    assert r.labels() == ["/(bau|plan)/#"]
    assert r.accept[0].examples == 6
    assert r.accept[0].pattern.matches("https://a.example/plan/77")
    assert not r.accept[0].pattern.matches("https://a.example/kontakt/77")


def test_learn_files_are_kept_out_of_patterns():
    files = ["https://a.example/files/expose-1.pdf", "https://a.example/files/expose-2.pdf"]
    r = learn("https://a.example/list", HOMEGATE_HITS[:0] + files + ["https://a.example/rent/1"],
              files + ["https://a.example/rent/1"])
    assert r.labels() == ["/rent/#"]
    assert "file link" in r.explanation


def test_learn_rejects_unknown_mode():
    with pytest.raises(ValueError):
        learn("https://a.example/", [], [], mode="everything")


# ---------------------------------------------------------------------------
# learn: merging with existing rows
# ---------------------------------------------------------------------------


def test_merge_keeps_existing_patterns_and_updates_relearned_counts():
    old = LearnedPattern(split_form("https://www.homegate.ch/buy/1").pattern, "accept", 4,
                         "https://www.homegate.ch/buy/1", origin="manual")
    r = learn(L_HOMEGATE, HOMEGATE_SHOWN, HOMEGATE_HITS, existing=[old.to_row()])
    assert sorted(r.labels()) == ["/buy/#", "/rent/#"]
    kept = next(p for p in r.accept if p.label == "/buy/#")
    assert kept.origin == "manual" and kept.examples == 4
    # Re-learned: the fresh count wins.
    r2 = learn(L_HOMEGATE, HOMEGATE_SHOWN, HOMEGATE_HITS, existing=r.to_dict()["accept"])
    assert next(p for p in r2.accept if p.label == "/rent/#").examples == 20


def test_merge_existing_accept_that_matches_unticked_links_gets_exact_rejects():
    old = LearnedPattern(split_form("https://a.example/rent/1").pattern, "accept", 9,
                         "https://a.example/rent/1")
    r = learn("https://a.example/list", ["https://a.example/rent/7", "https://a.example/news/1"],
              ["https://a.example/news/1"], existing={"patterns": [old.to_row()]})
    assert r.exact_rejects == ["https://a.example/rent/7"]
    assert next(p for p in r.accept if p.label == "/rent/#").needs_review is True


def test_merge_existing_reject_that_would_match_a_tick_is_dropped():
    old = LearnedPattern(split_form("https://a.example/news/1").pattern, "reject", 2,
                         "https://a.example/news/1")
    r = learn("https://a.example/list", ["https://a.example/news/1", "https://a.example/news/2"],
              ["https://a.example/news/1"], mode="all_except_rejected",
              existing={"patterns": [old.to_row()]})
    assert r.reject == []
    assert r.exact_rejects == ["https://a.example/news/2"]
    assert "dropped" in r.explanation


def test_merge_exact_rejects_beat_patterns_but_a_tick_beats_a_stale_reject():
    existing = {"patterns": [], "exact_urls": [
        {"text_kind": "reject", "text_url_canonical": "https://a.example/rent/5"},
        {"text_kind": "reject", "text_url_canonical": "https://a.example/rent/6"},
        {"text_kind": "monitor", "text_url_canonical": "https://a.example/board"},
    ]}
    r = learn("https://a.example/list", ["https://a.example/rent/1", "https://a.example/rent/6"],
              ["https://a.example/rent/1", "https://a.example/rent/6"], existing=existing)
    assert r.exact_rejects == ["https://a.example/rent/5"]      # 6 was ticked now
    assert r.exact_monitors == ["https://a.example/board"]
    rules = Rules.from_config({"text_list_url": "https://a.example/list", "text_mode": "selected",
                               "patterns": r.to_dict()["accept"],
                               "exact_urls": [{"text_kind": "reject", "text_url_canonical": u}
                                              for u in r.exact_rejects]})
    assert decide("https://a.example/rent/5", rules).by == "exact"
    assert decide("https://a.example/rent/6", rules).accepted


# ---------------------------------------------------------------------------
# decide: the order of the ladder
# ---------------------------------------------------------------------------

P_RENT = split_form("https://a.example/rent/1").pattern
P_NEWS = split_form("https://a.example/news/1").pattern
RULES = Rules("https://a.example/list", accept=((P_RENT, "/rent/#"),), reject=((P_NEWS, "/news/#"),),
              exact_monitor=frozenset({"https://a.example/board", "https://a.example/news/9"}),
              exact_reject=frozenset({"https://a.example/rent/2"}),
              next_urls=frozenset({"https://a.example/list/page-2"}))


@pytest.mark.parametrize("url, accepted, by, document", [
    ("https://a.example/list", False, "list_self", False),
    ("https://a.example/list?page=2", False, "pagination", False),
    ("https://a.example/list/page-2", False, "pagination", False),
    ("https://a.example/rent/2", False, "exact", True),
    ("https://a.example/board", True, "monitor", True),
    ("https://a.example/news/9", True, "monitor", True),        # monitor beats the reject pattern
    ("https://a.example/news/3", False, "/news/#", True),
    ("https://a.example/impressum", False, "legal", True),
    ("https://a.example/x.css", False, "asset", True),
    ("https://other.example/rent/1", False, "external", True),
    ("https://a.example/files/x.pdf", False, "file", True),
    ("https://a.example/rent/3", True, "/rent/#", True),
    ("https://a.example/about", False, "no pattern", True),
])
def test_decide_ladder(url, accepted, by, document):
    d = decide(url, RULES)
    assert (d.accepted, d.by, d.document) == (accepted, by, document)


def test_decide_legal_switch_and_same_host_switch():
    r = Rules("https://a.example/list", mode="all_except_rejected", drop_legal_links=False,
              same_host_only=False)
    assert decide("https://a.example/impressum", r).by == "everything else"
    assert decide("https://other.example/x", r).by == "everything else"


def test_decide_all_deduplicates_by_canonical_url_in_page_order():
    urls = ["https://a.example/rent/1", "https://a.example/rent/1#x", "https://a.example/rent/2?utm_source=q",
            "https://a.example/rent/2", ""]
    out = decide_all(urls, RULES)
    assert [d.url for d in out] == ["https://a.example/rent/1", "https://a.example/rent/2"]
    assert all(isinstance(d, Decision) for d in out)


def test_rules_reject_unknown_mode():
    with pytest.raises(ValueError):
        Rules("https://a.example/", mode="maybe")


# ---------------------------------------------------------------------------
# speed: the picker calls learn() on every "compute patterns" click
# ---------------------------------------------------------------------------


def test_learn_on_two_thousand_links_is_instant():
    hits = [f"https://www.homegate.ch/rent/4001{n:04d}" for n in range(1500)]
    twins = [f"https://www.homegate.ch/{lang}/d/some-slug-{n}/{n}" for n in range(150) for lang in ("de", "fr")]
    lists = [f"https://www.homegate.ch/rent/apartment/city-{n}/matching-list" for n in range(198)]
    shown = hits + twins + lists + [L_HOMEGATE + "?ep=2", "https://www.homegate.ch/impressum"]
    assert len(shown) == 2000
    learn(L_HOMEGATE, shown, hits)                      # warm up: regex compile, imports
    best = min(_timed(lambda: learn(L_HOMEGATE, shown, hits, mode=mode))
               for mode in ("selected", "all_except_rejected") for _ in range(3))
    # The budget is deliberately far above what this costs on an idle machine
    # (a few milliseconds). What it guards is the SHAPE of the work: pairing
    # every tick against every un-tick, or re-deriving a form inside a loop,
    # turns 2000 links into seconds and would blow through this by a factor of
    # ten. A tighter number measures the load average of whatever else the
    # machine is doing - it failed at 81 ms on a box running four test suites,
    # which said nothing about the code.
    assert best < 0.250, f"learn() took {best * 1000:.1f} ms"


def _timed(fn) -> float:
    start = time.perf_counter()
    fn()
    return time.perf_counter() - start


# ---------------------------------------------------------------------------
# generalising: where the wildcard is and is not allowed
# ---------------------------------------------------------------------------


def test_three_root_level_singles_never_become_a_wildcard():
    # "/bau", "/plan" and "/news" ticked once each: three words under the
    # empty parent path. A "/*" here would also take "/impressum".
    shown = ["https://a.example/bau", "https://a.example/plan", "https://a.example/news",
             "https://a.example/impressum"]
    r = learn("https://a.example/", shown, shown[:3])
    assert sorted(r.labels()) == ["/bau", "/news", "/plan"]
    assert not any(p.pattern.matches("https://a.example/kontakt") for p in r.accept)


def test_title_slugs_of_mixed_length_become_one_wildcard_pattern():
    # Two of the immoscout slugs are over 24 characters and are "*" from the
    # start; the ten short ones are literals. One pattern comes out.
    r = learn(L_IMMOSCOUT, IMMOSCOUT_HITS, IMMOSCOUT_HITS)
    assert r.labels() == ["/{lang}/d/*/#"]
    assert r.accept[0].examples == 12
    assert r.accept[0].needs_review is False
    assert r.accept[0].pattern.matches("https://www.immoscout24.ch/de/d/haus-kaufen-thalwil/4009999")
    assert not r.accept[0].pattern.matches("https://www.immoscout24.ch/de/d/haus-kaufen-thalwil")


def test_two_slugs_only_stay_separate_patterns():
    two = IMMOSCOUT_HITS[:2]
    r = learn(L_IMMOSCOUT, two, two)
    assert len(r.accept) == 2
    assert all(not p.pattern.matches("https://www.immoscout24.ch/de/d/other/1") for p in r.accept)


def test_wildcard_pattern_regex_matches_the_tuple():
    r = learn(L_IMMOSCOUT, IMMOSCOUT_HITS, IMMOSCOUT_HITS)
    p = r.accept[0].pattern
    rx = p.compile()
    for url in IMMOSCOUT_HITS + ["https://www.immoscout24.ch/de/d/x/1?page=2",
                                 "https://www.immoscout24.ch/de/d/x/1?sort=1",
                                 "https://www.immoscout24.ch/de/d/1", "https://www.immoscout24.ch/de/d/x/y"]:
        canon = canonical_uri(url)
        assert bool(rx.match(canon)) == p.matches(canon), canon
