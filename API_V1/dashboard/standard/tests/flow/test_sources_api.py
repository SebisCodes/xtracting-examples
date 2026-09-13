"""The Sources API against a real archive: saving, enabling, and the joins.

    DATABASE_URL=postgresql://... python -m pytest tests/flow/test_sources_api.py -q

Nothing here crawls anything - the test runner has its own file. What is
checked is the boundary: what the form may write, what it may not, that a
saved source is off until somebody enables it, that enabling is refused while
robots.txt forbids the list page, and that the overview really joins the
crawler's runtime tables and the archive's tags.

Every row this file writes carries the `_sourcestest` prefix and is removed
again, including the rows it fakes on the crawler's side.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from psycopg.types.json import Jsonb

from app import sources_store as store
from app.routers import api_sources
from crawlkit.forms import split_form

pytestmark = pytest.mark.flow

PREFIX = "_sourcestest "
LIST_URL = "https://estate.example/rent/apartment/city-zurich/matching-list"


def pattern_row(url: str, kind: str = "accept", **extra) -> dict:
    return {"text_kind": kind, "json_form": split_form(url).pattern.to_dict(), **extra}


@pytest.fixture
def api(client, archive):
    """Sources created through the API, and everything they left behind.

    The clean-up also removes the crawler-side rows the tests fake, because
    those tables have no foreign key to the source on purpose - deleting a
    source does not delete them, and the next run of the suite would count
    them again.
    """
    def sweep(conn) -> None:
        conn.execute("DELETE FROM scraper_config.sources WHERE text_name LIKE %s",
                     (PREFIX + "%",))
        conn.execute("DELETE FROM processed_data.tasks WHERE text_name LIKE %s",
                     (PREFIX + "%",))
        for table, column in (("scraper.targets", "bigint_fk_source"),
                              ("scraper.submit_queue", "bigint_fk_source"),
                              ("scraper.documents", "bigint_fk_source"),
                              ("scraper.files", "bigint_fk_source"),
                              ("monitoring.scraper_runs", "bigint_fk_source")):
            conn.execute(f"DELETE FROM {table} WHERE {column} = ANY(%s)", (made,))

    made: list[int] = []
    with archive.connect() as conn:
        # A previous run that was cut off leaves rows behind; start clean.
        conn.execute("DELETE FROM scraper_config.sources WHERE text_name LIKE %s",
                     (PREFIX + "%",))
        conn.commit()

    def create(name: str = "estate", **fields) -> int:
        payload = {"text_name": PREFIX + name, "text_list_url": LIST_URL, **fields}
        answer = client.post("/api/sources", json=payload)
        assert answer.status_code == 201, answer.text
        source_id = answer.json()["id"]
        made.append(source_id)
        return source_id

    yield SimpleNamespace(create=create, client=client, made=made, prefix=PREFIX)

    with archive.connect() as conn:
        sweep(conn)
        conn.commit()
    store.invalidate_archived_counts()


def source_row(archive, source_id: int) -> dict:
    with archive.connect() as conn:
        return dict(conn.execute("SELECT * FROM scraper_config.sources WHERE bigint_id = %s",
                                 (source_id,)).fetchone())


# ── saving ───────────────────────────────────────────────────────


def test_a_saved_source_is_off_and_carries_its_canonical_address(api, archive):
    source_id = api.create("estate", text_list_url="HTTPS://Estate.Example/rent/list?b=2&a=1")
    row = source_row(archive, source_id)
    assert row["bool_enabled"] is False
    assert row["text_list_url_canonical"] == "https://estate.example/rent/list?a=1&b=2"
    assert row["text_host"] == "estate.example"
    # And the defaults 03-scraper.sql promises are what the row got.
    assert (row["text_mode"], row["text_format"], row["text_engine"]) == ("selected", "plain", "http")
    assert row["integer_interval_minutes"] == 360 and row["integer_politeness_seconds"] == 6

    answer = api.client.get(f"/api/sources/{source_id}")
    assert answer.status_code == 200
    assert answer.json()["source"]["text_name"] == PREFIX + "estate"
    assert answer.json()["robots"] == {"allowed": None, "reason": "", "checked": None,
                                       "from": "none"}


def test_the_form_cannot_switch_the_crawl_on(api):
    source_id = api.create("switch")
    answer = api.client.put(f"/api/sources/{source_id}", json={"bool_enabled": True})
    assert answer.status_code == 400
    body = answer.json()
    assert "bool_enabled" in body["error"]
    assert "enable" in body["hint"]


def test_a_value_the_database_would_refuse_is_refused_with_a_sentence(api):
    answer = api.client.post("/api/sources", json={
        "text_name": PREFIX + "bad", "text_list_url": LIST_URL, "text_mode": "everything"})
    assert answer.status_code == 400
    assert "text_mode must be one of" in answer.json()["error"]

    answer = api.client.post("/api/sources", json={
        "text_name": PREFIX + "bad", "text_list_url": LIST_URL,
        "bool_respect_robots": False})
    assert answer.status_code == 400
    assert "written reason" in answer.json()["error"]


def test_a_name_that_is_already_taken_is_answered_in_words(api, archive):
    """`text_name` is UNIQUE, and the constraint must not reach the browser
    as a bare 500 with no JSON body at all - the one answer the page script
    can say nothing about. The editor checks the list it loaded first, so this is
    the race: a second tab, or a rename between load and save."""
    first = api.create("twice")

    answer = api.client.post("/api/sources", json={
        "text_name": PREFIX + "twice", "text_list_url": LIST_URL})
    assert answer.status_code == 409
    body = answer.json()
    assert PREFIX + "twice" in body["error"] and "already exists" in body["error"]
    assert "different name" in body["hint"]

    # Renaming one source onto another's name is the same refusal, and
    # nothing is written on the way.
    other = api.create("other")
    answer = api.client.put(f"/api/sources/{other}", json={
        "text_name": PREFIX + "twice", "text_list_url": LIST_URL})
    assert answer.status_code == 409
    assert source_row(archive, other)["text_name"] == PREFIX + "other"
    assert source_row(archive, first)["text_name"] == PREFIX + "twice"


def test_ignoring_robots_with_a_reason_is_allowed(api, archive):
    source_id = api.create("override", bool_respect_robots=False,
                           text_override_reason="written permission of the site, ticket 1234")
    assert source_id
    row = source_row(archive, source_id)
    assert row["bool_respect_robots"] is False and row["text_override_reason"]


def test_children_are_replaced_as_a_whole_and_move_date_updated(api, archive):
    source_id = api.create("patterns", patterns=[
        pattern_row("https://estate.example/rent/1"),
        pattern_row("https://estate.example/buyx/2", kind="reject")])
    before = source_row(archive, source_id)["date_updated"]

    answer = api.client.get(f"/api/sources/{source_id}").json()["source"]
    assert sorted(p["text_label"] for p in answer["patterns"]) == ["/buyx/#", "/rent/#"]

    # One pattern instead of two, plus an exact reject: the whole set is what
    # was learned, so the whole set is written.
    saved = api.client.put(f"/api/sources/{source_id}", json={
        "patterns": [pattern_row("https://estate.example/rent/1")],
        "exact_urls": [{"text_kind": "reject",
                        "text_url_canonical": "https://estate.example/rent/2"}]})
    assert saved.status_code == 200
    after = api.client.get(f"/api/sources/{source_id}").json()["source"]
    assert [p["text_label"] for p in after["patterns"]] == ["/rent/#"]
    assert [u["text_url_canonical"] for u in after["exact_urls"]] == [
        "https://estate.example/rent/2"]
    assert source_row(archive, source_id)["date_updated"] > before

    # A save that does not mention the patterns leaves them alone …
    api.client.put(f"/api/sources/{source_id}", json={"text_notes": "unchanged"})
    assert len(api.client.get(f"/api/sources/{source_id}").json()["source"]["patterns"]) == 1
    # … and an empty list is a change, not a missing field.
    api.client.put(f"/api/sources/{source_id}", json={"patterns": []})
    assert api.client.get(f"/api/sources/{source_id}").json()["source"]["patterns"] == []


def test_a_broken_pattern_leaves_the_whole_save_undone(api, archive):
    source_id = api.create("atomic", text_notes="first")
    answer = api.client.put(f"/api/sources/{source_id}", json={
        "text_notes": "second", "patterns": [{"text_kind": "accept", "json_form": {"x": 1}}]})
    assert answer.status_code == 400
    assert source_row(archive, source_id)["text_notes"] == "first"


def test_file_rules_survive_the_round_trip(api):
    source_id = api.create("files", bool_sub_sub=True, file_rules=[
        {"text_kind": "type_default", "text_value": "PDF", "bool_enabled": True},
        {"text_kind": "type_default", "text_value": "xlsx", "bool_enabled": False},
        {"text_kind": "file_exclude", "text_value": "https://estate.example/f/a.pdf",
         "integer_max_mb": 5}])
    rules = api.client.get(f"/api/sources/{source_id}").json()["source"]["file_rules"]
    assert {(r["text_kind"], r["text_value"], r["bool_enabled"]) for r in rules} == {
        ("type_default", "pdf", True), ("type_default", "xlsx", False),
        ("file_exclude", "https://estate.example/f/a.pdf", True)}


def test_a_source_can_be_deleted_and_is_then_gone(api):
    source_id = api.create("gone")
    assert api.client.delete(f"/api/sources/{source_id}").status_code == 200
    assert api.client.get(f"/api/sources/{source_id}").status_code == 404
    assert api.client.delete(f"/api/sources/{source_id}").status_code == 404


def test_a_source_that_does_not_exist_answers_404_everywhere(api):
    for path in ("", "/preview", "/runs", "/documents", "/files"):
        answer = api.client.get(f"/api/sources/999999{path}")
        assert answer.status_code == 404, path
        assert answer.json()["error"]


# ── enabling ─────────────────────────────────────────────────────


def store_snapshot(archive, source_id, *, allowed: bool, reason: str = "",
                   status: str = "DONE", result: dict | None = None,
                   config: dict | None = None) -> int:
    """A finished test snapshot, as the runner would have left it."""
    payload = result or {}
    payload.setdefault("robots", {"status": 200, "list_allowed": allowed,
                                  "list_reason": reason, "note": ""})
    with archive.connect() as conn:
        row = conn.execute("""
            INSERT INTO scraper_config.test_snapshots
                (bigint_fk_source, json_config, text_kind, text_status, date_finished, json_result)
            VALUES (%s, %s, 'crawl', %s, NOW(), %s) RETURNING bigint_id""",
            (source_id, Jsonb(config or {}), status, Jsonb(payload))).fetchone()
        conn.commit()
    return row["bigint_id"]


def test_enabling_is_refused_while_robots_forbids_the_list_page(api, archive):
    source_id = api.create("forbidden")
    store_snapshot(archive, source_id, allowed=False,
                   reason="Disallow: /*-srp-new/* for User-agent: *")

    answer = api.client.post(f"/api/sources/{source_id}/enable", json={"enabled": True})
    assert answer.status_code == 409
    assert "robots.txt" in answer.json()["error"]
    # The rule text is in the answer: without it nobody knows what to ask for.
    assert "srp-new" in answer.json()["hint"]
    assert source_row(archive, source_id)["bool_enabled"] is False

    # Saving stays possible - only Enable is refused.
    assert api.client.put(f"/api/sources/{source_id}",
                          json={"text_notes": "asked the site"}).status_code == 200

    # And a source that does not respect robots.txt (with its written reason)
    # is not held back by the verdict.
    api.client.put(f"/api/sources/{source_id}", json={
        "bool_respect_robots": False, "text_override_reason": "written permission, ticket 1234"})
    answer = api.client.post(f"/api/sources/{source_id}/enable", json={"enabled": True})
    assert answer.status_code == 200 and answer.json()["enabled"] is True


def test_switching_off_is_never_refused(api, archive):
    source_id = api.create("off")
    api.client.post(f"/api/sources/{source_id}/enable", json={"enabled": True})
    store_snapshot(archive, source_id, allowed=False, reason="Disallow: /")
    answer = api.client.post(f"/api/sources/{source_id}/enable", json={"enabled": False})
    assert answer.status_code == 200
    assert source_row(archive, source_id)["bool_enabled"] is False


def test_the_newer_verdict_wins(api, archive):
    source_id = api.create("verdict")
    with archive.connect() as conn:
        conn.execute("""
            INSERT INTO scraper.targets (bigint_fk_source, bool_robots_allowed,
                                         text_robots_reason, date_robots_checked)
            VALUES (%s, false, 'Disallow: / (yesterday)', NOW() - INTERVAL '1 day')""",
            (source_id,))
        conn.commit()
    assert api.client.post(f"/api/sources/{source_id}/enable").status_code == 409

    # The site changed its robots.txt and a test said so this minute.
    store_snapshot(archive, source_id, allowed=True)
    assert api.client.post(f"/api/sources/{source_id}/enable").status_code == 200


def test_a_refusal_reaches_both_readers_of_a_snapshot(api, archive):
    """The list and the single read have to say the same thing.

    The overview redraws one card from GET /api/sources/{id} after a test, and
    it draws the same pill from the list on a reload. The single read carried
    no fetch status, so a page that had just answered 403 read "robots.txt
    allows the list page" until somebody reloaded - and the reload then said
    403. Two answers for one fact, and the wrong one first.
    """
    source_id = api.create("refused")
    store_snapshot(archive, source_id, allowed=True,
                   result={"fetch": {"status": "403", "error": "", "challenge": False}})

    one = api.client.get(f"/api/sources/{source_id}").json()["snapshot"]
    assert one["http_status"] == 403, one

    listed = [row for row in api.client.get("/api/sources").json()["items"]
              if row["id"] == source_id][0]["snapshot"]
    assert listed["http_status"] == 403, listed
    # Same shape, not merely the same number: the page reads three keys.
    assert set(("http_status", "error", "challenge")) <= set(one) & set(listed)


def test_a_bot_check_is_carried_by_the_single_read_too(api, archive):
    source_id = api.create("challenged")
    store_snapshot(archive, source_id, allowed=True,
                   result={"fetch": {"status": "200", "error": "", "challenge": True}})
    snapshot = api.client.get(f"/api/sources/{source_id}").json()["snapshot"]
    assert snapshot["challenge"] is True, snapshot


# ── the overview ─────────────────────────────────────────────────


def test_the_overview_joins_the_crawlers_state_and_the_archives_tags(api, archive):
    source_id = api.create("overview", patterns=[pattern_row("https://estate.example/rent/1")])
    tag = f"xs_{source_id}_abc123"
    with archive.connect() as conn:
        conn.execute("""
            INSERT INTO scraper.targets (bigint_fk_source, bool_robots_allowed,
                text_robots_reason, date_robots_checked, date_last_run, date_next_run,
                integer_consecutive_failures, text_key_status)
            VALUES (%s, true, 'robots.txt allows this address', NOW(), NOW(),
                    NOW() + INTERVAL '6 hours', 0, '')""", (source_id,))
        conn.execute("""
            INSERT INTO monitoring.scraper_runs
                (text_name, text_status, bigint_fk_source, integer_pages, integer_links,
                 integer_accepted, integer_new, integer_files, integer_submitted, integer_ms)
            VALUES (%s, 'OK', %s, 2, 24, 20, 3, 1, 3, 812)""",
            (PREFIX + "overview run", source_id))
        conn.execute("""
            INSERT INTO scraper.submit_queue
                (text_tag, bigint_fk_source, date_document_added, bigint_fk_document,
                 text_source_uri, text_status)
            VALUES (%s, %s, NOW(), 1, 'https://estate.example/rent/1', 'ARCHIVED')""",
            (tag, source_id))
        conn.execute("""
            INSERT INTO processed_data.tasks
                (text_name, text_project, text_language, text_task_id, text_status,
                 text_source_uri, text_tag, date_commissioned)
            VALUES (%s, '_sourcestest', 'English', %s, 'COMPLETED',
                    'https://estate.example/rent/1', %s, NOW())""",
            (PREFIX + "task", tag, tag))
        conn.commit()
    store.invalidate_archived_counts()

    rows = api.client.get("/api/sources").json()
    row = next(r for r in rows["items"] if r["id"] == source_id)
    assert row["robots"]["allowed"] is True and row["robots"]["from"] == "crawler"
    assert row["last_run"]["status"] == "OK" and row["last_run"]["accepted"] == 20
    assert row["schedule"]["next_run"] is not None
    assert row["counts"]["archived"] == 1
    assert row["counts"]["accept_patterns"] == 1
    assert "online" in rows["crawler"]


def test_the_overview_shows_a_source_that_has_never_run(api):
    source_id = api.create("never")
    row = next(r for r in api.client.get("/api/sources").json()["items"] if r["id"] == source_id)
    assert row["last_run"] is None
    assert row["robots"]["allowed"] is None
    assert row["counts"] == {"accept_patterns": 0, "reject_patterns": 0, "exact_urls": 0,
                             "pending": 0, "sent": 0, "failed": 0, "archived": 0}


def test_runs_documents_and_files_come_back_newest_first(api, archive):
    source_id = api.create("history")
    with archive.connect() as conn:
        for minutes, status in ((10, "OK"), (5, "BLOCKED")):
            conn.execute("""
                INSERT INTO monitoring.scraper_runs
                    (date_added, text_name, text_status, bigint_fk_source, integer_pages)
                VALUES (NOW() - make_interval(mins => %s), %s, %s, %s, 1)""",
                (minutes, f"{PREFIX}run {minutes}", status, source_id))
        conn.execute("""
            INSERT INTO scraper.documents
                (text_name, bigint_fk_source, text_uri_canonical, text_content,
                 text_content_hash, integer_char_count, text_tag)
            VALUES ('Apartment 1', %s, 'https://estate.example/rent/1', 'a lot of text',
                    'deadbeef', 13, %s)""", (source_id, f"xs_{source_id}_a"))
        conn.execute("""
            INSERT INTO scraper.files
                (bigint_fk_source, text_page_uri, text_file_uri_canonical, text_file_type,
                 text_status, integer_bytes, text_detail)
            VALUES (%s, 'https://estate.example/rent/1',
                    'https://estate.example/f/a.pdf', 'pdf', 'NO_TEXT', 4096,
                    'no text layer - a scan')""", (source_id,))
        conn.commit()

    runs = api.client.get(f"/api/sources/{source_id}/runs").json()["items"]
    assert [r["status"] for r in runs] == ["BLOCKED", "OK"]

    documents = api.client.get(f"/api/sources/{source_id}/documents").json()["items"]
    assert documents[0]["uri"] == "https://estate.example/rent/1"
    assert documents[0]["characters"] == 13
    # The content itself is never in the list: it is what was sent, and it can
    # be two million characters.
    assert "text_content" not in documents[0] and "content" not in documents[0]

    files = api.client.get(f"/api/sources/{source_id}/files").json()["items"]
    assert files[0]["status"] == "NO_TEXT" and "scan" in files[0]["detail"]


# ── preview ──────────────────────────────────────────────────────


def snapshot_result(links: list[str], accepted: set[str]) -> dict:
    return {"links": [{"url": url, "class": "candidate", "accepted_by_config": url in accepted}
                      for url in links],
            "pages": [{"url": LIST_URL, "status": 200}],
            "robots": {"list_allowed": True}, "warnings": ["one page only"]}


def test_preview_applies_the_saved_rules_to_the_last_test(api, archive):
    source_id = api.create("preview", patterns=[pattern_row("https://estate.example/rent/1")])
    links = [f"https://estate.example/rent/{n}" for n in range(1, 6)] + [
        "https://estate.example/impressum", "https://estate.example/about",
        LIST_URL + "?ep=2"]
    store_snapshot(archive, source_id, allowed=True,
                   result=snapshot_result(links, set(links[:5])),
                   config={"text_list_url": "https://estate.example/rent/older-list"})

    answer = api.client.get(f"/api/sources/{source_id}/preview")
    assert answer.status_code == 200
    body = answer.json()
    assert body["counts"]["links"] == len(links)
    assert body["counts"]["accepted"] == 5
    assert body["counts"]["pagination"] == 1
    assert body["by"]["/rent/#"] == 5
    assert body["by"]["legal"] == 1
    assert body["by"]["no pattern"] == 1
    assert body["warnings"] == ["one page only"]
    # The test fetched a different address, and the page has to say so.
    assert body["config_changed"] is True
    assert body["changed_fields"] == ["text_list_url"]

    # In all_except_rejected everything that is not legal or pagination counts.
    api.client.put(f"/api/sources/{source_id}", json={"text_mode": "all_except_rejected"})
    body = api.client.get(f"/api/sources/{source_id}/preview").json()
    assert body["counts"]["accepted"] == 6
    assert body["by"]["everything else"] == 6


def test_the_preview_counts_account_for_every_link_it_was_given(api, archive):
    """The editor's counter row read "Links found 31 / Would be
    collected 20 / Rejected 10" and one link was nowhere to be found: the
    paging link. `rejected` counts DOCUMENTS that no rule keeps, and neither
    a paging link nor the list page's own address is a document - they are
    exactly the two classes `rejected` leaves out. The row cannot add up
    unless the API counts them, so it does:

        links = accepted + rejected + pagination + list_self
    """
    source_id = api.create("arithmetic", patterns=[pattern_row("https://estate.example/rent/1")])
    links = [f"https://estate.example/rent/{n}" for n in range(1, 6)] + [
        "https://estate.example/impressum",   # legal      -> rejected
        "https://estate.example/about",       # no pattern -> rejected
        LIST_URL + "?ep=2",                   # pagination
        LIST_URL]                             # the list page itself
    store_snapshot(archive, source_id, allowed=True,
                   result=snapshot_result(links, set(links[:5])))

    counts = api.client.get(f"/api/sources/{source_id}/preview").json()["counts"]
    assert counts["links"] == 9
    assert counts["accepted"] == 5
    assert counts["rejected"] == 2
    assert counts["pagination"] == 1
    assert counts["list_self"] == 1
    assert (counts["accepted"] + counts["rejected"]
            + counts["pagination"] + counts["list_self"]) == counts["links"]


def test_preview_says_what_to_do_when_there_is_no_test_yet(api):
    source_id = api.create("untested")
    answer = api.client.get(f"/api/sources/{source_id}/preview")
    assert answer.status_code == 404
    assert "no finished test" in answer.json()["error"]
    assert "Test this configuration" in answer.json()["hint"]


def test_preview_ignores_a_test_that_failed(api, archive):
    source_id = api.create("failed-test")
    store_snapshot(archive, source_id, allowed=True, status="FAILED",
                   result={"progress": "reading robots.txt"})
    assert api.client.get(f"/api/sources/{source_id}/preview").status_code == 404


def test_a_snapshot_of_the_saved_configuration_is_not_reported_as_changed(api, archive):
    source_id = api.create("same")
    source = api.client.get(f"/api/sources/{source_id}").json()["source"]
    store_snapshot(archive, source_id, allowed=True, result=snapshot_result([], set()),
                   config=store.config_of(source))
    body = api.client.get(f"/api/sources/{source_id}/preview").json()
    assert body["config_changed"] is False and body["changed_fields"] == []
    # A later change of the address does show up.
    api.client.put(f"/api/sources/{source_id}",
                   json={"text_list_url": "https://estate.example/rent/new-list"})
    assert api.client.get(f"/api/sources/{source_id}/preview").json()["changed_fields"] == [
        "text_list_url"]


# ── export ───────────────────────────────────────────────────────


def test_export_csv_carries_the_byte_order_mark_and_one_row_per_source(api):
    api.create("export-a")
    api.create("export-b")
    answer = api.client.get("/api/export/sources.csv")
    assert answer.status_code == 200
    assert answer.headers["content-type"].startswith("text/csv")
    assert "xtracting-sources-" in answer.headers["content-disposition"]
    text = answer.content.decode("utf-8")
    assert text.startswith("﻿"), "Excel needs the byte order mark to read UTF-8"
    lines = [line for line in text.splitlines() if line.strip()]
    assert lines[0].lstrip("﻿").startswith("id,name,list_url,host")
    assert sum(1 for line in lines if PREFIX in line) == 2
    assert ",no," in lines[1] or ",no," in lines[-1]      # enabled is a word, not a bool


def test_export_json_is_the_same_columns(api):
    api.create("export-json")
    answer = api.client.get("/api/export/sources.json")
    assert answer.status_code == 200
    assert "attachment" in answer.headers["content-disposition"]
    rows = json.loads(answer.content.decode("utf-8"))["sources"]
    mine = [r for r in rows if r["name"].startswith(PREFIX)]
    assert mine and set(mine[0]) == {name for name, _ in api_sources.EXPORT_COLUMNS}


# ── the view's search box ────────────────────────────────────────


def names_of(answer) -> set[str]:
    return {item["text_name"] for item in answer.json()["items"]}


def test_the_search_looks_at_the_four_things_the_card_shows(api):
    """Name, host, address of the list page, key label - one substring over
    the four things a card says in words, so "everything on that portal" and
    "everything on that key" are both one search."""
    estate = api.create("estate zurich", text_key_label="alpha")
    cars = api.create("car portal", text_list_url="https://cars.example/de/list",
                      text_key_label="beta")

    by_name = api.client.get("/api/sources", params={"q": "zurich"})
    assert names_of(by_name) == {PREFIX + "estate zurich"}
    assert by_name.json()["q"] == "zurich"

    by_host = api.client.get("/api/sources", params={"q": "cars.example"})
    assert names_of(by_host) == {PREFIX + "car portal"}

    by_path = api.client.get("/api/sources", params={"q": "/de/list"})
    assert names_of(by_path) == {PREFIX + "car portal"}

    by_key = api.client.get("/api/sources", params={"q": "alpha"})
    assert names_of(by_key) == {PREFIX + "estate zurich"}

    # Case does not matter, and neither does where in the word it sits.
    assert names_of(api.client.get("/api/sources", params={"q": "ZURICH"})) == \
        {PREFIX + "estate zurich"}

    assert {estate, cars} <= {r["id"] for r in api.client.get("/api/sources").json()["items"]}


def test_a_search_that_matches_nothing_still_says_how_many_there_are(api):
    """"0 of 13" and "you have no sources" are different sentences, and the
    page can only tell them apart if the answer carries both numbers."""
    api.create("counted")
    everything = api.client.get("/api/sources").json()
    assert everything["q"] == "" and everything["total"] == len(everything["items"]) >= 1

    nothing = api.client.get("/api/sources", params={"q": "_no_source_is_called_this_"}).json()
    assert nothing["items"] == []
    assert nothing["total"] == everything["total"]
    assert nothing["q"] == "_no_source_is_called_this_"


def test_a_search_term_is_a_substring_and_not_a_pattern(api):
    """`%` and `_` are ordinary characters in a search box. Unescaped, a
    single `%` would match every source and read as a search that ignores
    what was typed."""
    api.create("percent")
    # A bare `%` is a character somebody typed, not "show me everything".
    assert api.client.get("/api/sources", params={"q": "%"}).json()["items"] == []
    # `_` is one character in LIKE and a letter in a search box: unescaped,
    # "p_rcent" would find "percent".
    assert api.client.get("/api/sources", params={"q": "p_rcent"}).json()["items"] == []
    # And escaped it is still findable - the prefix of these rows has one.
    assert names_of(api.client.get("/api/sources", params={"q": "_sourcestest"})) >= \
        {PREFIX + "percent"}
    # The blank one is not a search at all.
    assert api.client.get("/api/sources", params={"q": "   "}).json()["q"] == ""


def test_the_export_writes_the_rows_the_page_was_showing(api):
    """An export that ignores the search hands somebody a file they did not
    ask for."""
    # Distinct addresses: the fixture's default list URL has "city-zurich"
    # in its path, and a search that reads the address would match both.
    api.create("exported zurich", text_list_url="https://estate.example/zurich/list")
    api.create("exported bern", text_list_url="https://estate.example/bern/list")

    csv_text = api.client.get("/api/export/sources.csv",
                              params={"q": "zurich"}).content.decode("utf-8")
    assert PREFIX + "exported zurich" in csv_text
    assert PREFIX + "exported bern" not in csv_text

    rows = json.loads(api.client.get("/api/export/sources.json", params={"q": "zurich"})
                      .content.decode("utf-8"))["sources"]
    assert [r["name"] for r in rows if r["name"].startswith(PREFIX)] == \
        [PREFIX + "exported zurich"]
