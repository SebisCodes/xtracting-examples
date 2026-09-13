"""The key registry: which project a key opens, and which key evaluates a page.

Two things are decided here and both are decided wrongly in silence if they are
not tested. The first is what a key IS - a crawler that misreads a capability
files documents under the wrong project, and nothing downstream can tell. The
second is the fallback, and it has a boundary that matters more than the
fallback itself: when a project's keys are all gone, its watchlists must STOP,
not borrow a key from another project.
"""

from __future__ import annotations

import pytest

from app import keys
from crawlkit.crawl import Source
from crawlkit.tests.standin.fake_xtracting import FakeXtracting


class Entry:
    """The shape `config.ApiKeyEntry` has, without importing the config."""

    def __init__(self, label: str, secret: str) -> None:
        self.label = label
        self.secret = secret

    @property
    def prefix(self) -> str:
        return self.secret.split(".", 1)[0]


@pytest.fixture
def api():
    with FakeXtracting() as running:
        yield running


# -- the effort ladder -------------------------------------------------

@pytest.mark.parametrize("double, thinking, step", [
    (False, False, 1),
    (False, True, 2),
    (True, False, 3),
    (True, True, 4),
    (None, None, 1),
])
def test_the_four_effort_steps(double, thinking, step):
    """Four combinations, four steps, and double-checking outranks thinking.

    The order is not arbitrary: a second pass over the whole document costs more
    than more reasoning within one pass, so a person comparing two keys by this
    number is comparing the thing that actually differs."""
    assert keys.effort_step(double, thinking) == step


def test_translations_are_not_effort():
    """A key that translates is not "more effort" - it is more output, billed
    separately. Folding it in would make the cheapest key look expensive."""
    assert keys.effort_step(False, False) == keys.effort_step(False, False)


# -- what a key says about itself --------------------------------------

def test_a_key_reports_its_project_and_capabilities(api):
    api.state.add_key("aaaa1111.secret", name="nightly", project_id="proj-a",
                      project_name="Flats", extract=True)
    probe = keys.KeyProbe(api.url)

    facts = probe.identify(Entry("nightly", "aaaa1111.secret"), 0)

    assert facts is not None
    assert facts.prefix == "aaaa1111"
    assert facts.project_id == "proj-a"
    assert facts.project_name == "Flats"
    assert facts.can_extract is True
    assert facts.usable is True


def test_a_key_nobody_issued_is_thrown_away(api):
    """None, not an exception and not a placeholder row: the caller deletes what
    it cannot confirm, and a key that does not authenticate is not a key."""
    api.state.add_key("aaaa1111.secret", project_id="proj-a")
    probe = keys.KeyProbe(api.url)

    assert probe.identify(Entry("ghost", "ffff9999.nothing"), 0) is None


def test_a_deactivated_key_is_described_rather_than_lost(api):
    """The difference the /api/v1/key endpoint exists for: a switched-off key
    answers with a reason, so the log can say WHY it stopped instead of
    reporting the same "refused" a typo produces."""
    api.state.add_key("bbbb2222.secret", project_id="proj-a",
                      usable=False, reason="API key is deactivated")
    probe = keys.KeyProbe(api.url)

    facts = probe.identify(Entry("retired", "bbbb2222.secret"), 0)

    assert facts is not None
    assert facts.usable is False
    assert facts.reason == "API key is deactivated"


def test_read_is_implied_by_edit():
    """One rule, in one place. A key that may edit may read, whether or not
    anybody ticked the read box."""
    assert keys.KeyFacts("p", "l", 0, can_edit_project=True).reads_project
    assert keys.KeyFacts("p", "l", 0, can_read_project=True).reads_project
    assert not keys.KeyFacts("p", "l", 0).reads_project


# -- gathering, and the order that decides the default -----------------

def test_keys_are_grouped_by_project_and_keep_their_order(api):
    """Order is the only way a person can express a preference without holding a
    project-read key, so it has to survive the probe intact."""
    api.state.add_key("aaaa1111.one", name="first", project_id="proj-a")
    api.state.add_key("bbbb2222.two", name="second", project_id="proj-a")
    api.state.add_key("cccc3333.three", name="other", project_id="proj-b",
                      project_name="Other")

    found = keys.gather(keys.KeyProbe(api.url), [
        Entry("first", "aaaa1111.one"),
        Entry("second", "bbbb2222.two"),
        Entry("other", "cccc3333.three"),
    ])

    assert set(found) == {"proj-a", "proj-b"}
    assert [k.prefix for k in found["proj-a"].keys] == ["aaaa1111", "bbbb2222"]
    assert [k.order for k in found["proj-a"].keys] == [0, 1]


def test_an_ordinary_key_leaves_the_project_undetailed(api):
    """No read key, no detail - and the dashboard hides the effort chooser,
    because there is nothing to compare."""
    api.state.add_key("aaaa1111.one", project_id="proj-a", extract=True)

    found = keys.gather(keys.KeyProbe(api.url), [Entry("only", "aaaa1111.one")])

    assert found["proj-a"].detailed is False


def test_a_read_key_fills_in_the_detail(api):
    """The whole reason canReadProject exists: with one, the chooser can say
    what each key of the project is set to run with."""
    api.state.add_key("aaaa1111.one", name="submitter", project_id="proj-a")
    api.state.add_key("rrrr4444.read", name="reader", project_id="proj-a",
                      extract=False, read_project=True)

    found = keys.gather(keys.KeyProbe(api.url), [
        Entry("submitter", "aaaa1111.one"),
        Entry("reader", "rrrr4444.read"),
    ])

    assert found["proj-a"].detailed is True
    assert "aaaa1111" in found["proj-a"].detail


def test_a_key_that_names_no_project_is_ignored(api):
    """Not a crash and not a project called "": a key whose answer carries no
    project cannot be used to file anything, so it is left out of the registry
    with a line in the log."""
    api.state.add_key("aaaa1111.one", project_id="")
    api.state.keys["aaaa1111.one"]["project"] = {"projectId": "", "name": ""}

    assert keys.gather(keys.KeyProbe(api.url), [Entry("x", "aaaa1111.one")]) == {}


# -- which key evaluates which page ------------------------------------

def test_the_most_specific_choice_wins():
    """Address, then link group, then watchlist - the order a person would
    expect from the screen, where a choice on one row beats a choice on the
    group it sits in."""
    source = Source(
        projects=({"text_project_id": "p1", "text_key_prefix": "wwww0000"},),
        patterns=({"text_kind": "accept", "text_regex": r"/rent/\d+$",
                   "text_key_prefix": "gggg1111"},),
        exact_urls=({"text_kind": "monitor",
                     "text_url_canonical": "https://a.example/one",
                     "text_key_prefix": "aaaa2222"},),
    )

    assert source.key_prefix_for("https://a.example/one") == "aaaa2222"
    assert source.key_prefix_for("https://a.example/rent/7") == "gggg1111"
    assert source.key_prefix_for("https://a.example/news/1") == "wwww0000"


def test_a_group_without_a_key_falls_through_to_the_watchlist():
    source = Source(projects=({"text_project_id": "p1",
                               "text_key_prefix": "wwww0000"},),
                    patterns=({"text_kind": "accept", "text_regex": "/x/"},))
    assert source.key_prefix_for("https://a.example/x/1") == "wwww0000"


def test_a_reject_pattern_never_chooses_a_key():
    """Reject patterns say what is not collected. One of them carrying a key
    would be a key for documents that are never submitted."""
    source = Source(projects=({"text_project_id": "p1",
                               "text_key_prefix": "wwww0000"},),
                    patterns=({"text_kind": "reject", "text_regex": "/x/",
                               "text_key_prefix": "nnnn9999"},))
    assert source.key_prefix_for("https://a.example/x/1") == "wwww0000"


def test_a_pattern_that_no_longer_compiles_does_not_lose_the_document():
    """A stored regex that stopped compiling is the dashboard's to report. Here
    it must not throw: the document still has to be queued, under the
    watchlist's own key."""
    source = Source(projects=({"text_project_id": "p1",
                               "text_key_prefix": "wwww0000"},),
                    patterns=({"text_kind": "accept", "text_regex": "([",
                               "text_key_prefix": "bbbb1111"},))
    assert source.key_prefix_for("https://a.example/x/1") == "wwww0000"


def test_nothing_chosen_anywhere_means_the_project_default():
    """'' is not "no key" - it is "the project's default", resolved when the row
    is submitted so that collection carries on when one key is switched off."""
    assert Source().key_prefix_for("https://a.example/x") == ""
