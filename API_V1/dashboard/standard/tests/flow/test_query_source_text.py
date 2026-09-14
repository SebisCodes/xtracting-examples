"""What "a source's name, description and summary" means in this archive.

    DATABASE_URL=postgresql://... python -m pytest tests/flow/test_query_source_text.py -q

`processed_data.sources` has no `text_description`. What the extraction
writes about a document in prose is two columns: `text_summary` (what the
document says) and `text_reason` (why it was judged the way it was). A
search that reads only the first one cannot find a document by the sentence
that explains it, so both are searched - and because the result card marks
the terms in the text it shows, both are also what the card shows.

`text_keywords` is not searched, and this file holds that decision to a
measurement rather than an opinion: it is a list of subject areas, not
prose, and on the preseeded archive one keyword matches every source there
is (see `test_the_keyword_list_is_not_searched`). A search box whose answer
is always "everything" is not a search box.
"""

from __future__ import annotations

import pytest

from preseed import ALPHA

pytestmark = pytest.mark.flow

EN = {"project": ALPHA, "language": "English"}

# Every preseeded source carries `text_reason = 'preseed'` and
# `text_keywords = 'battery, antitrust, pump'`; the summaries differ. That
# is what makes the pair testable: one word lives only in the reason, one
# lives in every keyword list.
ONLY_IN_THE_REASON = "preseed"
IN_EVERY_KEYWORD_LIST = "battery"


def search(client, body: dict, expect: int = 200) -> dict:
    r = client.post("/api/query/search", params=EN, json=body)
    assert r.status_code == expect, r.text
    return r.json()


def sources_of(data: dict) -> list[dict]:
    return [i for g in data["groups"] for i in g["items"] if i["kind"] == "source"]


def usa(**extra) -> dict:
    return {"place": {"address": "USA"}, **extra}


def test_a_word_that_is_only_in_the_reason_finds_the_document(client):
    """The gap this file closes: before `text_reason` was searched, the
    answer to this question was nothing at all."""
    data = search(client, usa(terms=[ONLY_IN_THE_REASON]))
    found = sources_of(data)
    assert found, "a word from text_reason found no document"
    assert data["counts"]["source"] == len(found)


def test_the_card_shows_the_sentence_that_matched(client):
    """A result whose term cannot be seen in it reads as a wrong result.
    The row's body therefore carries both prose columns, so the page has
    something to mark - and `matched_terms` says the same thing."""
    data = search(client, usa(terms=[ONLY_IN_THE_REASON]))
    for item in sources_of(data):
        assert item["matched_terms"] == [ONLY_IN_THE_REASON], item
        assert ONLY_IN_THE_REASON in (item["body"] or "").lower(), item


def test_the_summary_is_still_searched_and_still_shown(client):
    """The column that was already searched keeps working, and the summary
    is still the first half of what the card shows."""
    data = search(client, usa(terms=["antitrust"]))
    found = sources_of(data)
    assert found, "no document mentions antitrust"
    for item in found:
        assert "antitrust" in (item["body"] or "").lower(), item


def test_the_keyword_list_is_not_searched(client, conn):
    """The measurement behind the decision, not an assertion about taste.

    Every preseeded source carries the same keyword list, so a search that
    read `text_keywords` would answer "every document in the archive" to a
    term that three documents actually discuss.
    """
    row = conn.execute("""
        SELECT count(*) FILTER (WHERE text_keywords ILIKE %(q)s) AS in_keywords,
               count(*) FILTER (WHERE text_summary  ILIKE %(q)s) AS in_summary,
               count(*)                                          AS all_sources
          FROM processed_data.sources
         WHERE text_project = %(project)s AND text_language = 'English'
    """, {"q": f"%{IN_EVERY_KEYWORD_LIST}%", "project": ALPHA}).fetchone()
    assert row["in_keywords"] == row["all_sources"] > row["in_summary"] > 0

    # And the search agrees with the smaller number, not the larger one.
    data = search(client, usa(terms=[IN_EVERY_KEYWORD_LIST]))
    assert 0 < data["counts"]["source"] < row["all_sources"]
