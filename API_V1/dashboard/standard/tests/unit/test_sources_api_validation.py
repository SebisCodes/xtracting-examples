"""What the Sources API accepts, and what it refuses before the database does.

Three things are checked here, all without a database:

* the writable-field whitelist covers every column of `scraper_config.sources`
  that a person edits, and nothing else;
* the ranges and word lists in `sources_store.FIELDS` are the ones
  `database/init/03-scraper.sql` writes as CHECK constraints - the two are read
  out of the SQL file and compared, so a widened CHECK that nobody mirrored
  fails here rather than as a 500 in front of a customer;
* the shapes the two pure endpoints take: `/learn` (which stores nothing) and
  the children of a save.

    python -m pytest tests/unit/test_sources_api_validation.py -q
"""

from __future__ import annotations

import doctest
import re
import time
from pathlib import Path

import pytest
from fastapi import HTTPException

from app import sources_store as store
from app.routers import api_sources
from app.sources_store import SourceError
from crawlkit.forms import split_form

SCHEMA_SQL = (Path(__file__).resolve().parents[4] / "database" / "init"
              / "03-scraper.sql").read_text(encoding="utf-8")

LIST_URL = "https://estate.example/rent/apartment/city-zurich/matching-list"


def test_doctests():
    failed, _ = doctest.testmod(store)
    assert failed == 0


# ── the schema, read back ────────────────────────────────────────


def sources_table_columns() -> list[str]:
    """The column names of scraper_config.sources, straight out of the file
    that creates it - a column definition starts in the fourth column of the
    line, a CONSTRAINT or a comment does not."""
    block = re.search(r"CREATE TABLE IF NOT EXISTS scraper_config\.sources \((.*?)\n\);",
                      SCHEMA_SQL, re.S)
    assert block, "the sources table is not in 03-scraper.sql any more"
    return re.findall(r"^    ((?:bigint|date|text|bool|integer|float)_\w+)",
                      block.group(1), re.M)


def check_in_list(column: str) -> tuple[str, ...]:
    match = re.search(rf"{column}\s+IN\s*\(([^)]*)\)", SCHEMA_SQL)
    assert match, f"no CHECK ... {column} IN (...) in 03-scraper.sql"
    return tuple(v.strip().strip("'") for v in match.group(1).split(","))


def check_between(column: str) -> tuple[int, int]:
    match = re.search(rf"{column}\s+BETWEEN\s+(\d+)\s+AND\s+(\d+)", SCHEMA_SQL)
    assert match, f"no CHECK ... {column} BETWEEN ... in 03-scraper.sql"
    return int(match.group(1)), int(match.group(2))


#: Columns of the table that the form must NOT write, with the reason. Every
#: other column has to be on the whitelist - a new column that nobody can edit
#: is a feature nobody can reach, and this test is where that shows up.
SERVER_OWNED = {
    "bigint_id": "the identity",
    "date_added": "set by the database",
    "date_updated": "set on every save",
    "text_list_url_canonical": "derived from the address",
    "text_host": "derived from the address",
    "bool_enabled": "its own endpoint, so the robots check cannot be skipped",
}


def test_every_editable_column_is_on_the_whitelist():
    columns = sources_table_columns()
    assert len(columns) > 20, columns
    for column in columns:
        if column in SERVER_OWNED:
            assert column not in store.WRITABLE, f"{column} is server-owned"
            continue
        assert column in store.WRITABLE, f"{column} cannot be edited by anyone"
    for column in store.WRITABLE:
        assert column in columns, f"{column} is not a column of scraper_config.sources"


def test_the_three_word_lists_are_the_ones_the_database_checks():
    assert store.MODES == check_in_list("text_mode")
    assert store.FORMATS == check_in_list("text_format")
    assert store.ENGINES == check_in_list("text_engine")
    assert store.PATTERN_ORIGINS == check_in_list("text_origin")
    # text_kind is used by four tables; the sets are compared by content.
    kinds = {frozenset(k) for k in
             (tuple(v.strip().strip("'") for v in group.split(","))
              for group in re.findall(r"text_kind\s+IN\s*\(([^)]*)\)", SCHEMA_SQL))}
    for expected in (store.PATTERN_KINDS, store.EXACT_KINDS, store.FILE_RULE_KINDS,
                     store.SNAPSHOT_KINDS):
        assert frozenset(expected) in kinds, expected


@pytest.mark.parametrize("column", [f.name for f in store.FIELDS if f.kind == "int"])
def test_every_number_range_is_the_one_the_database_checks(column):
    low, high = check_between(column)
    spec = store.WRITABLE[column]
    assert (spec.minimum, spec.maximum) == (low, high)


def test_the_file_rule_size_range_is_mirrored_too():
    low, high = check_between("integer_max_mb")
    with pytest.raises(SourceError):
        store.clean_file_rules([{"text_kind": "type_default", "text_value": "pdf",
                                 "integer_max_mb": high + 1}])
    assert store.clean_file_rules([{"text_kind": "type_default", "text_value": "pdf",
                                    "integer_max_mb": low}])[0]["integer_max_mb"] == low


# ── the whitelist at work ────────────────────────────────────────


def test_values_are_typed_and_trimmed():
    clean = store.clean_fields({
        "text_name": "  Estate rentals  ", "text_list_url": LIST_URL,
        "integer_max_pages": "4", "bool_sub_sub": "yes", "bool_follow_pagination": False,
        "text_mode": "all_except_rejected",
    })
    assert clean == {"text_name": "Estate rentals", "text_list_url": LIST_URL,
                     "integer_max_pages": 4, "bool_sub_sub": True,
                     "bool_follow_pagination": False, "text_mode": "all_except_rejected"}


def test_unknown_fields_are_ignored_and_owned_ones_are_named():
    assert store.clean_fields({"text_notes": "n", "colour": "blue", "id": 3}) == {"text_notes": "n"}
    for column, in ((c,) for c in store.REFUSED):
        with pytest.raises(SourceError) as exc:
            store.clean_fields({column: "x"})
        assert column in str(exc.value)
        assert exc.value.hint


@pytest.mark.parametrize("payload, expected", [
    ({"text_mode": "everything"}, "text_mode must be one of"),
    ({"text_format": "pdf"}, "text_format must be one of"),
    ({"text_engine": "curl"}, "text_engine must be one of"),
    ({"integer_max_pages": 0}, "at least 1"),
    ({"integer_max_pages": 1001}, "at most 1000"),
    ({"integer_interval_minutes": "soon"}, "whole number"),
    ({"bool_sub_sub": "maybe"}, "true or false"),
    ({"text_name": "   "}, "cannot be empty"),
    ({"text_list_url": "ftp://estate.example/list"}, "not an address"),
])
def test_the_form_refuses_what_the_database_would(payload, expected):
    with pytest.raises(SourceError) as exc:
        store.clean_fields(payload)
    assert expected in str(exc.value)


def test_ignoring_robots_needs_a_written_reason():
    with pytest.raises(SourceError) as exc:
        store.check_override({"bool_respect_robots": False, "text_override_reason": " "})
    assert "written reason" in str(exc.value)
    store.check_override({"bool_respect_robots": False,
                          "text_override_reason": "written permission, ticket 1234"})
    store.check_override({"bool_respect_robots": True, "text_override_reason": ""})


def test_the_canonical_address_and_host_are_derived():
    assert store.derived_url_fields("HTTPS://Estate.Example:443/rent/list?b=2&a=1#top") == {
        "text_list_url_canonical": "https://estate.example/rent/list?a=1&b=2",
        "text_host": "estate.example"}


# ── the children ─────────────────────────────────────────────────


def pattern_row(url: str, kind: str = "accept", **extra) -> dict:
    row = {"text_kind": kind, "json_form": split_form(url).pattern.to_dict()}
    row.update(extra)
    return row


def test_label_and_regex_are_re_derived_from_the_form():
    # A caller that sends a hand-written regex beside the form gets the regex
    # the form means: 03-scraper.sql says the form is the source of truth.
    rows = store.clean_patterns([pattern_row("https://estate.example/rent/4001234",
                                             text_regex="^anything$", text_label="mine")])
    assert rows[0]["text_label"] == "/rent/#"
    assert "/rent/[0-9]+" in rows[0]["text_regex"]
    assert rows[0]["text_origin"] == "learned"


def test_two_patterns_with_the_same_regex_are_one():
    rows = store.clean_patterns([pattern_row("https://estate.example/rent/1"),
                                 pattern_row("https://estate.example/rent/2")])
    assert len(rows) == 1


@pytest.mark.parametrize("row, expected", [
    ({"text_kind": "maybe", "json_form": {}}, "text_kind must be one of"),
    (pattern_row("https://estate.example/rent/1", text_origin="invented"),
     "text_origin must be one of"),
    ({"text_kind": "accept", "json_form": {"nonsense": True}}, "cannot be read"),
    ("not a row", "must be an object"),
])
def test_a_pattern_that_cannot_be_read_is_refused(row, expected):
    with pytest.raises(SourceError) as exc:
        store.clean_patterns([row])
    assert expected in str(exc.value)


def test_exact_addresses_are_canonical_and_have_one_verdict():
    rows = store.clean_exact_urls([
        {"text_kind": "reject", "text_url_canonical": "HTTPS://Estate.Example/rent/2?b=1&a=2"},
        {"text_kind": "monitor", "text_url_canonical": "https://estate.example/watch"},
    ])
    assert {r["text_url_canonical"] for r in rows} == {
        "https://estate.example/rent/2?a=2&b=1", "https://estate.example/watch"}
    with pytest.raises(SourceError) as exc:
        store.clean_exact_urls([{"text_kind": "monitor", "text_url_canonical": "https://a.example/x"},
                                {"text_kind": "reject", "text_url_canonical": "https://a.example/x"}])
    assert "both as monitor and as reject" in str(exc.value)
    with pytest.raises(SourceError):
        store.clean_exact_urls([{"text_kind": "reject", "text_url_canonical": "mailto:a@b.example"}])


def test_file_rules_keep_types_and_single_files_apart():
    rows = store.clean_file_rules([
        {"text_kind": "type_default", "text_value": ".PDF"},
        {"text_kind": "type_default", "text_value": "pdf"},          # the same rule
        {"text_kind": "file_exclude", "text_value": "HTTPS://Estate.Example/f/a.pdf"},
    ])
    assert [(r["text_kind"], r["text_value"]) for r in rows] == [
        ("type_default", "pdf"),
        ("file_exclude", "https://estate.example/f/a.pdf")]
    with pytest.raises(SourceError):
        store.clean_file_rules([{"text_kind": "type_default", "text_value": "  "}])


def test_a_draft_config_is_the_shape_the_rules_read():
    source = {"id": 5, "text_name": "Estate", "text_list_url": LIST_URL,
              "text_mode": "selected", "bool_drop_legal_links": True,
              "patterns": [{"id": 9, **store.clean_patterns(
                  [pattern_row("https://estate.example/rent/1")])[0]}],
              "exact_urls": [{"id": 3, "text_kind": "reject",
                              "text_url_canonical": "https://estate.example/rent/2"}],
              "file_rules": []}
    config = store.config_of(source)
    assert "id" not in config and all("id" not in p for p in config["patterns"])
    from crawlkit.apply import Rules, decide
    rules = Rules.from_config(config)
    assert decide("https://estate.example/rent/3", rules).accepted
    assert decide("https://estate.example/rent/2", rules).by == "exact"


def test_only_the_fields_that_decide_what_was_fetched_count_as_a_change():
    saved = {"text_list_url": LIST_URL, "text_engine": "http", "text_mode": "selected"}
    assert store.changed_fetch_fields(saved, saved) == []
    # What the preview re-applies is not a change - that is what it is for.
    assert store.changed_fetch_fields({**saved, "text_mode": "exact"}, saved) == []
    assert store.changed_fetch_fields({**saved, "text_engine": "playwright"}, saved) == [
        "text_engine"]
    # A draft that never carried the field says nothing about it.
    assert store.changed_fetch_fields({"text_mode": "selected"}, saved) == []


# ── the result that gets stored ──────────────────────────────────


def test_a_huge_link_list_is_capped_and_the_page_is_told():
    result = store.trim_result({"links": [{"url": f"https://a.example/{i}"} for i in range(2500)],
                                "counts": {"links": 2500}})
    assert len(result["links"]) == store.MAX_STORED_LINKS
    assert result["counts"]["links"] == 2500          # what was found is not rewritten
    assert "2500 links found" in result["warnings"][0]
    small = {"links": [{"url": "https://a.example/1"}]}
    assert store.trim_result(small) is small


# ── /learn: pure, and fast enough to run while the popup is open ──


def learn_request(**extra):
    body = {"list_url": LIST_URL, "mode": "selected", "links": [], "ticked": []}
    body.update(extra)
    return api_sources.LearnRequest(**body)


def test_learn_returns_patterns_for_the_ticked_links():
    hits = [f"https://estate.example/rent/400123{n:02d}" for n in range(20)]
    shown = hits + [LIST_URL + "?ep=2", "https://estate.example/impressum",
                    "https://estate.example/rent/apartment/city-basel/matching-list"]
    answer = api_sources.learn(learn_request(
        links=[{"url": url, "ticked": url in hits} for url in shown]))
    assert [p["text_label"] for p in answer["accept"]] == ["/rent/#"]
    assert answer["paging_param"] == "ep"
    assert answer["reject"] == []
    assert "20 selected links" in answer["explanation"]


def test_learn_in_all_except_mode_produces_rejects():
    hits = [f"https://estate.example/rent/400123{n:02d}" for n in range(20)]
    shown = hits + ["https://estate.example/rent/apartment/city-basel/matching-list"]
    answer = api_sources.learn(learn_request(
        mode="all_except_rejected",
        links=[{"url": url, "ticked": url in hits} for url in shown]))
    assert [p["text_label"] for p in answer["reject"]] == [
        "/rent/apartment/city-basel/matching-list"]


def test_learn_merges_what_is_already_stored():
    existing = store.clean_patterns([pattern_row("https://estate.example/rent/1")])
    answer = api_sources.learn(learn_request(
        existing_patterns=existing,
        links=[{"url": "https://estate.example/buyx/7", "ticked": True}]))
    assert sorted(p["text_label"] for p in answer["accept"]) == ["/buyx/#", "/rent/#"]


def test_learn_refuses_an_impossible_mode_and_too_many_links():
    with pytest.raises(HTTPException) as exc:
        api_sources.learn(learn_request(mode="everything"))
    assert exc.value.status_code == 400

    many = [{"url": f"https://estate.example/rent/{i}"} for i in range(api_sources.MAX_LEARN_LINKS + 1)]
    with pytest.raises(HTTPException) as exc:
        api_sources.learn(learn_request(links=many))
    assert exc.value.status_code == 400
    assert "more than this can learn from" in exc.value.detail["error"]


def test_learn_over_two_thousand_links_is_quick():
    """The popup calls this between two keystrokes; the budget is 50 ms for
    2000 links. The assertion is far looser so a busy machine cannot fail
    the suite - what it catches is a change that turns learning quadratic."""
    links = [{"url": f"https://estate.example/rent/{i}", "ticked": i % 2 == 0}
             for i in range(2000)]
    body = learn_request(links=links)
    started = time.monotonic()
    answer = api_sources.learn(body)
    took = time.monotonic() - started
    assert answer["accept"], answer
    assert took < 1.0, f"learning from 2000 links took {took:.3f} s"
