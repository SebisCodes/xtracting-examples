"""Testing a configuration from the dashboard, end to end.

    DASHBOARD_ALLOW_PRIVATE_HOSTS=true DATABASE_URL=postgresql://... \\
        python -m pytest tests/flow/test_sources_test_runner.py -q

What is checked here is the path a person walks: press Test, watch the
progress line, get a snapshot with what the site answered - and the three
refusals around it (a private address, a second test while one runs, a site
that never answers).

The crawl engine itself (`crawlkit.testrun`, the I/O half) is not what these
tests are about, so most of them run against `tests/stub_testrun.py`, which
answers in the documented shape without a network. The one test that really
crawls uses the engine against a stand-in list page served on loopback; it is
marked xfail while the engine is not in the build yet.
"""

from __future__ import annotations

import importlib.util
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest

from app.sources_store import SnapshotStore
from app.testrunner import TestRunner, set_runner

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import stub_testrun  # noqa: E402

pytestmark = pytest.mark.flow

#: The engine is the I/O half of crawlkit. On a build without it, the one
#: test that really crawls is reported as expected-to-fail rather than
#: skipped: skipped reads as "not needed", and this one is needed.
HAVE_ENGINE = importlib.util.find_spec("crawlkit.testrun") is not None
HAVE_STANDIN = importlib.util.find_spec("crawlkit.tests.standin.site") is not None
ENGINE_NOTE = ("crawlkit.testrun and its stand-in site (the crawl engine, the "
               "I/O half of crawlkit) are not in this build")

# A public address that resolves without a network: the guard looks every
# host up before it lets a test start, and a name that does not resolve is a
# refusal in its own right. Nothing is fetched - the stub engine answers out
# of the configuration.
LIST_URL = "http://93.184.216.34/rent/apartment/city-zurich/matching-list"
DRAFT = {"text_name": "_sourcestest draft", "text_list_url": LIST_URL,
         "text_mode": "all_except_rejected", "bool_drop_legal_links": True}


# ── fixtures ─────────────────────────────────────────────────────


@pytest.fixture
def snapshots(archive):
    """Removes the snapshots a test made - drafts have no source to cascade
    from, so they would pile up in the table run after run."""
    with archive.connect() as conn:
        before = conn.execute("SELECT COALESCE(max(bigint_id), 0) AS top "
                              "FROM scraper_config.test_snapshots").fetchone()["top"]

    def count(status: str = "") -> int:
        with archive.connect() as conn:
            return conn.execute(
                "SELECT count(*) AS n FROM scraper_config.test_snapshots "
                "WHERE bigint_id > %s AND (%s = '' OR text_status = %s)",
                (before, status, status)).fetchone()["n"]

    def row(snapshot_id: int) -> dict:
        with archive.connect() as conn:
            return dict(conn.execute("SELECT * FROM scraper_config.test_snapshots "
                                     "WHERE bigint_id = %s", (snapshot_id,)).fetchone())

    yield type("Snapshots", (), {"count": staticmethod(count), "row": staticmethod(row),
                                 "first_id": before})
    with archive.connect() as conn:
        conn.execute("DELETE FROM scraper_config.test_snapshots WHERE bigint_id > %s", (before,))
        conn.execute("DELETE FROM scraper_config.sources WHERE text_name LIKE %s",
                     ("_sourcestest %",))
        conn.commit()


@pytest.fixture
def runner_with(client):
    """Puts a runner with a given engine in front of the endpoints."""
    def install(execute, **kwargs) -> TestRunner:
        runner = TestRunner(SnapshotStore(), execute=execute,
                            poll_seconds=0.02, **kwargs)
        set_runner(runner)
        return runner

    yield install
    stub_testrun.GATE.set()
    set_runner(None)
    stub_testrun.GATE.clear()


@pytest.fixture
def policy(client):
    """The two settings the endpoint reads per request: whether a private
    address may be tested, and how long a test may take. Set here rather than
    from the environment, so a test that needs the other answer says so
    instead of relying on how the suite was launched."""
    original = client.app.state.cfg

    def set_to(private: bool, timeout: int | None = None) -> None:
        client.app.state.cfg = replace(
            original, allow_private_hosts=private,
            test_timeout_seconds=timeout or original.test_timeout_seconds)

    yield set_to
    client.app.state.cfg = original


def wait_for(client, snapshot_id: int, status: str = "DONE", seconds: float = 15.0) -> dict:
    deadline = time.monotonic() + seconds
    body: dict = {}
    while time.monotonic() < deadline:
        body = client.get(f"/api/sources/test/{snapshot_id}").json()
        if body["status"] == status:
            return body
        time.sleep(0.05)
    raise AssertionError(f"snapshot {snapshot_id} is {body.get('status')}, not {status}: "
                         f"{body.get('error') or body.get('progress')}")


# ── a draft, tested ──────────────────────────────────────────────


def test_a_draft_is_tested_and_the_snapshot_holds_what_the_site_answered(
        client, snapshots, runner_with, policy):
    policy(private=False)                 # a public address needs no exception
    runner_with(stub_testrun.execute)

    answer = client.post("/api/sources/test",
                         json={"config": DRAFT, "kind": "crawl", "sample_subpages": 5})
    assert answer.status_code == 202, answer.text
    snapshot_id = answer.json()["snapshot_id"]
    assert answer.json()["status"] == "RUNNING"

    body = wait_for(client, snapshot_id)
    result = body["result"]
    # The shape B2 documents, and the fields the popup reads out of it.
    assert set(result) >= {"engine_used", "robots", "fetch", "pages", "links", "files",
                           "counts", "warnings"}
    assert result["robots"]["list_allowed"] is True
    assert result["fetch"]["status"] == 200
    assert result["counts"]["links"] == len(result["links"])
    classes = {link["class"] for link in result["links"]}
    # classify() runs in the engine, so the popup can group by these words.
    assert {"candidate", "pagination", "legal", "file"} <= classes
    # all_except_rejected takes the candidates and leaves legal, pagination,
    # files and the sibling list alone.
    assert result["counts"]["accepted"] == 21
    assert [link["by"] for link in result["links"] if link["class"] == "legal"] == ["legal"]

    row = snapshots.row(snapshot_id)
    assert row["text_status"] == "DONE"
    assert row["bigint_fk_source"] is None          # a draft belongs to nobody yet
    assert row["date_started"] and row["date_finished"]
    assert row["json_config"]["text_list_url"] == LIST_URL


def test_the_progress_line_is_readable_while_the_test_runs(
        client, snapshots, runner_with, policy):
    policy(private=False)
    runner_with(stub_testrun.gated_execute)
    stub_testrun.GATE.clear()

    snapshot_id = client.post("/api/sources/test", json={"config": DRAFT}).json()["snapshot_id"]
    for _ in range(100):                              # the poll the editor does
        body = client.get(f"/api/sources/test/{snapshot_id}").json()
        if body["progress"] == "waiting for the site":
            break
        time.sleep(0.05)
    assert body["status"] == "RUNNING"
    assert body["progress"] == "waiting for the site"
    assert body["steps"][0] == "starting the test"
    assert body["result"] is None                     # nothing to show yet

    stub_testrun.GATE.set()
    assert wait_for(client, snapshot_id)["result"]["counts"]["links"] == 24


def test_a_second_test_while_one_runs_is_refused_and_leaves_no_row(
        client, snapshots, runner_with, policy):
    policy(private=False)
    runner_with(stub_testrun.gated_execute)
    stub_testrun.GATE.clear()

    first = client.post("/api/sources/test", json={"config": DRAFT}).json()["snapshot_id"]
    second = client.post("/api/sources/test", json={"config": DRAFT})
    assert second.status_code == 409
    assert second.json()["error"] == "another test is running"
    assert str(first) in second.json()["hint"]
    # The refused request must not leave a RUNNING snapshot nobody runs.
    assert snapshots.count("RUNNING") == 1

    stub_testrun.GATE.set()
    wait_for(client, first)

    again = client.post("/api/sources/test", json={"config": DRAFT})
    assert again.status_code == 202
    wait_for(client, again.json()["snapshot_id"])
    assert snapshots.count("DONE") == 2


def test_an_address_in_a_private_network_is_refused_before_anything_is_stored(
        client, snapshots, runner_with, policy):
    policy(private=False)
    runner_with(stub_testrun.execute)

    for address in ("http://127.0.0.1:8099/list", "http://169.254.169.254/latest/meta-data/",
                    "http://[::1]/list"):
        answer = client.post("/api/sources/test",
                             json={"config": {**DRAFT, "text_list_url": address}})
        assert answer.status_code == 400, address
        assert "private network" in answer.json()["error"]
        assert "public internet" in answer.json()["hint"]
    assert snapshots.count() == 0

    # An exact-mode draft is checked the same way: the list address being
    # public says nothing about the addresses it monitors.
    answer = client.post("/api/sources/test", json={"config": {
        **DRAFT, "text_mode": "exact",
        "exact_urls": [{"text_kind": "monitor", "text_url_canonical": "http://10.1.2.3/page"}]}})
    assert answer.status_code == 400
    assert "10.1.2.3" in answer.json()["error"]
    assert snapshots.count() == 0

    # With the flag set for a test site, the same address is accepted.
    policy(private=True)
    answer = client.post("/api/sources/test",
                         json={"config": {**DRAFT, "text_list_url": "http://127.0.0.1:8099/list"}})
    assert answer.status_code == 202


def test_a_configuration_without_an_address_is_refused(client, snapshots, runner_with):
    runner_with(stub_testrun.execute)
    answer = client.post("/api/sources/test", json={"config": {"text_name": "x"}})
    assert answer.status_code == 400
    assert "no list address" in answer.json()["error"]
    answer = client.post("/api/sources/test", json={})
    assert answer.status_code == 400
    assert "nothing to test" in answer.json()["error"]


def test_a_site_that_never_answers_ends_as_a_failed_snapshot(
        client, snapshots, runner_with, policy):
    policy(private=False, timeout=1)
    runner_with(stub_testrun.never_answers, timeout_seconds=1)

    snapshot_id = client.post("/api/sources/test", json={"config": DRAFT}).json()["snapshot_id"]
    body = wait_for(client, snapshot_id, "FAILED")
    assert "stopped after 1 seconds" in body["error"]
    assert "DASHBOARD_TEST_TIMEOUT_SECONDS" in body["error"]
    # Nothing to render: a failed test has a sentence, not a result.
    assert body["result"] is None
    assert snapshots.row(snapshot_id)["date_finished"] is not None

    # And the worker is free: the next test starts straight away.
    runner_with(stub_testrun.execute)
    wait_for(client, client.post("/api/sources/test",
                                 json={"config": DRAFT}).json()["snapshot_id"])


def test_a_build_without_the_crawl_engine_says_so_on_the_row(
        client, snapshots, runner_with, policy, monkeypatch):
    policy(private=False)
    monkeypatch.setattr("app.testrunner.crawlkit_execute",
                        lambda: (_ for _ in ()).throw(ImportError("No module named 'httpx'")))
    runner_with(None)                     # no engine given: the real lookup runs

    snapshot_id = client.post("/api/sources/test", json={"config": DRAFT}).json()["snapshot_id"]
    body = wait_for(client, snapshot_id, "FAILED")
    assert "cannot crawl" in body["error"]


# ── the test and the source it becomes ───────────────────────────


def test_a_draft_snapshot_follows_the_source_it_is_saved_as(
        client, snapshots, runner_with, policy):
    policy(private=False)
    runner_with(stub_testrun.execute)
    snapshot_id = client.post("/api/sources/test", json={"config": DRAFT}).json()["snapshot_id"]
    wait_for(client, snapshot_id)

    created = client.post("/api/sources", json={**DRAFT, "snapshot_id": snapshot_id})
    assert created.status_code == 201
    source_id = created.json()["id"]
    assert snapshots.row(snapshot_id)["bigint_fk_source"] == source_id

    # Which is what makes the preview work without fetching anything again.
    preview = client.get(f"/api/sources/{source_id}/preview")
    assert preview.status_code == 200
    assert preview.json()["snapshot"]["id"] == snapshot_id
    assert preview.json()["counts"]["accepted"] == 21
    assert preview.json()["config_changed"] is False


def test_a_saved_source_can_be_tested_by_its_id(client, snapshots, runner_with, policy):
    policy(private=False)
    runner_with(stub_testrun.execute)
    source_id = client.post("/api/sources", json=DRAFT).json()["id"]

    answer = client.post("/api/sources/test", json={"source_id": source_id})
    assert answer.status_code == 202
    snapshot_id = answer.json()["snapshot_id"]
    body = wait_for(client, snapshot_id)
    assert body["source_id"] == source_id
    assert snapshots.row(snapshot_id)["json_config"]["text_list_url"] == LIST_URL
    assert client.get(f"/api/sources/{source_id}").json()["snapshot"]["status"] == "DONE"


# ── the real thing ───────────────────────────────────────────────


@pytest.fixture
def standin():
    """crawlkit's four stand-in hosts, on loopback and on ephemeral ports.

    The same site the crawl tests use, so "what the dashboard's test crawl
    finds" and "what the crawler finds" are statements about one site with one
    robots.txt - which is the point of sharing the engine in the first place.
    """
    from crawlkit.tests.standin.site import StandinSite

    with StandinSite() as site:
        yield site


@pytest.mark.xfail(not (HAVE_ENGINE and HAVE_STANDIN), reason=ENGINE_NOTE, run=False)
def test_a_real_crawl_of_the_stand_in_list_page_fills_the_snapshot(
        client, snapshots, policy, standin):
    """The whole path with the real engine: robots.txt, the list page, its
    links classified, the snapshot stored. It needs the private-host
    exception, because a stand-in site lives on loopback - which is exactly
    the setting CI uses (`DASHBOARD_ALLOW_PRIVATE_HOSTS=true`)."""
    policy(private=True, timeout=60)
    set_runner(None)                     # the endpoint builds the real runner

    config = {**DRAFT, "text_list_url": standin.estate_list,
              "integer_politeness_seconds": 1, "integer_max_pages": 2}
    answer = client.post("/api/sources/test", json={"config": config})
    assert answer.status_code == 202, answer.text
    body = wait_for(client, answer.json()["snapshot_id"], seconds=90)

    result = body["result"]
    assert result["robots"]["list_allowed"] is True
    assert result["fetch"]["status"] == 200
    assert result["engine_used"] == "http"

    urls = {link["url"] for link in result["links"]}
    assert sum(1 for url in urls if "/rent/40012" in url) == 20
    classes = {link["class"] for link in result["links"]}
    assert {"candidate", "legal"} <= classes
    assert result["counts"]["accepted"] >= 20

    # robots.txt of the estate host forbids the ?ep= pages, and the result
    # says so in a sentence - the editor shows it as a verdict banner rather
    # than leaving the reader to wonder where page 2 went.
    assert any("forbids page 2" in warning for warning in result["warnings"]), \
        result["warnings"]

    # A crawl test reads list pages and nothing else. That the imprint was
    # never fetched is a statement about requests, so it is asserted on the
    # site's access log rather than on the result.
    assert not standin.fetched("estate", "/impressum")
    assert not standin.fetched("estate", "/rent/40012300")

    # And the progress line the editor showed while this ran was about pages.
    assert any("page" in step.lower() for step in body["steps"]), body["steps"]


@pytest.mark.xfail(not (HAVE_ENGINE and HAVE_STANDIN), reason=ENGINE_NOTE, run=False)
def test_a_files_test_samples_subpages_and_lists_what_it_found(
        client, snapshots, policy, standin):
    """kind=files: the same crawl, plus a few subpages, so the editor can show
    what a document costs in characters and which files are on it."""
    policy(private=True, timeout=90)
    set_runner(None)

    config = {**DRAFT, "text_list_url": standin.estate_list, "bool_sub_sub": True,
              "integer_politeness_seconds": 0, "integer_max_pages": 1,
              "file_rules": [{"text_kind": "type_default", "text_value": "pdf",
                              "bool_enabled": True, "integer_max_mb": 25}]}
    answer = client.post("/api/sources/test",
                         json={"config": config, "kind": "files", "sample_subpages": 2})
    assert answer.status_code == 202, answer.text
    result = wait_for(client, answer.json()["snapshot_id"], seconds=120)["result"]

    assert result["kind"] == "files"
    assert result["counts"]["samples"] == 2
    # The character count is the number the format switch is decided on.
    assert result["counts"]["sample_chars"] > 0
    assert result["files"], "the detail pages carry a PDF and a CSV"
    assert {record["type"] for record in result["files"]} >= {"pdf"}
    assert standin.count("estate", "/rent/40012") >= 2


@pytest.mark.xfail(not (HAVE_ENGINE and HAVE_STANDIN), reason=ENGINE_NOTE, run=False)
def test_test_then_learn_then_save_then_preview(client, snapshots, policy, standin):
    """The loop the editor walks, with nothing faked in the middle.

    Test the draft, tick the twenty flats in the popup, let /learn turn them
    into a pattern, save the source with it - and the preview then explains
    every link of that same test without fetching anything again.
    """
    policy(private=True, timeout=60)
    set_runner(None)

    draft = {"text_name": "_sourcestest loop", "text_list_url": standin.estate_list,
             "text_mode": "selected", "integer_politeness_seconds": 0,
             "integer_max_pages": 1}
    snapshot_id = client.post("/api/sources/test",
                              json={"config": draft}).json()["snapshot_id"]
    result = wait_for(client, snapshot_id, seconds=90)["result"]

    ticked = [link["url"] for link in result["links"] if "/rent/40012" in link["url"]]
    assert len(ticked) == 20
    learned = client.post("/api/sources/learn", json={
        "list_url": draft["text_list_url"], "mode": "selected",
        "links": [{"url": link["url"], "ticked": link["url"] in ticked}
                  for link in result["links"]]}).json()
    assert [p["text_label"] for p in learned["accept"]] == ["/rent/#"]
    assert learned["paging_param"] == "ep"

    created = client.post("/api/sources", json={
        **draft, "patterns": learned["accept"] + learned["reject"],
        "text_paging_param": learned["paging_param"] or "",
        "snapshot_id": snapshot_id})
    assert created.status_code == 201, created.text
    source_id = created.json()["id"]

    preview = client.get(f"/api/sources/{source_id}/preview").json()
    assert preview["counts"]["accepted"] == 20
    assert preview["by"]["/rent/#"] == 20
    assert preview["by"]["legal"] >= 1
    assert preview["changed_fields"] == []
    assert preview["paging_param"] == "ep"
