"""The home view's three endpoints against the preseeded archive.

    DATABASE_URL=postgresql://... python -m pytest tests/flow/test_dashboard_stats.py -q

Every number here can be traced back to tests/preseed/preseed.py: the six
Alpha documents were written 2 hours, 3 days, 20 days, 60 days, 200 days and
2 years ago, and the counters below follow from that and from nothing else.

The three facts this file pins:

  * the counters match the seeded dates, per period;
  * there is NO total - the endpoint deliberately never scans the whole
    archive, and a "total" key creeping in would be a full scan;
  * German and English are the same rows in two languages, so their counters
    are equal, while Beta is a different project and must not leak in.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

# From the preseed itself, not `from conftest import ...`: with tests/unit
# collected in the same run, `conftest` names the unit suite's file. The
# flow conftest puts tests/preseed on the path before this module loads.
from preseed import ALPHA, BETA

pytestmark = pytest.mark.flow

PERIODS = ("hour", "day", "week", "month", "year")


def stats(client, project: str, language: str, fresh: bool = True) -> dict:
    r = client.get("/api/dashboard/stats",
                   params={"project": project, "language": language, "fresh": str(fresh).lower()})
    assert r.status_code == 200, r.text
    return {row["id"]: row for row in r.json()["tables"]}


def test_counters_follow_the_seeded_dates(client):
    tables = stats(client, ALPHA, "English")

    # The six Alpha documents were commissioned 2 h, 3 d, 20 d, 60 d, 200 d
    # and 2 years ago (30 minutes after each was written, which is why these
    # numbers are the same on both clocks and why this file alone cannot
    # catch the clock defect pinned below). The two-year-old one is outside
    # every window, including the year.
    sources = tables["sources"]
    assert sources["hour"] == 0
    assert sources["day"] == 1
    assert sources["week"] == 2
    assert sources["month"] == 3
    assert sources["year"] == 5

    # EVENTS ARE COUNTED HERE ON THE ARRIVAL CLOCK, LIKE EVERY OTHER TILE.
    #
    # Counted on date_eventdate - the date the event happened, the CONTENT
    # clock the Diagrams are drawn on - under a heading that says "Archived,
    # per period", six tiles would mean one thing and two of them another,
    # with nothing on the page saying which is which. On a real archive that
    # reads Sources 3/4/6/9 beside Entities 158/158/158/158 for a single
    # import.
    #
    # So all eight are on date_commissioned, and the events follow their
    # TASKS: alpha:1 (two events) 2 hours ago, alpha:2 (two) 3 days,
    # alpha:3 (one) 20 days, alpha:4 (one) 60 days, alpha:5 (one) 200 days,
    # alpha:6 (one) two years - and the preseed commissions 30 minutes after
    # it writes, so the newest task is an hour and a half old.
    #
    # A second rule is tested below: a window is closed at BOTH ends, so an
    # eventdate in the future - the announced launch, the scheduled hearing
    # - is in no window at all. It simply does not decide which period an
    # event is counted in.
    events = tables["events"]
    assert events["hour"] == 0, "the newest task was commissioned 90 minutes ago"
    assert events["day"] == 2, "both events of the 2-hour-old task"
    assert events["week"] == 4, "and both of the 3-day-old one"
    assert events["month"] == 5, "and the 20-day-old delivery"
    assert events["year"] == 7, "all but the two-year-old fine"

    for table in tables.values():
        assert table["hour"] <= table["day"] <= table["week"] <= table["month"] <= table["year"], table


def test_every_archive_table_is_counted_and_nothing_is_totalled(client):
    r = client.get("/api/dashboard/stats", params={"project": ALPHA, "language": "English"})
    body = r.json()
    ids = [row["id"] for row in body["tables"]]
    assert ids == ["sources", "entities", "connections", "locations", "events",
                   "ratings", "attributes", "market_insights"]
    for row in body["tables"]:
        assert set(PERIODS) <= set(row), row
        assert "total" not in row, "an unbounded total is a full scan and was deliberately dropped"
        assert row["label"] and row["tab"]
    assert "total" not in body


def test_the_two_languages_count_the_same_rows(client):
    english = stats(client, ALPHA, "English")
    german = stats(client, ALPHA, "German")
    for name, row in english.items():
        assert {p: row[p] for p in PERIODS} == {p: german[name][p] for p in PERIODS}, name


def test_the_other_project_is_not_counted(client):
    alpha = stats(client, ALPHA, "English")
    beta = stats(client, BETA, "English")
    # Beta is one document, written five days ago.
    assert beta["sources"]["year"] == 1
    assert beta["sources"]["week"] == 1
    assert beta["sources"]["day"] == 0
    assert beta["entities"]["year"] == 2
    assert alpha["sources"]["year"] > beta["sources"]["year"]


def test_the_counters_are_cached_for_a_minute(client):
    first = client.get("/api/dashboard/stats", params={"project": ALPHA, "language": "English"}).json()
    again = client.get("/api/dashboard/stats", params={"project": ALPHA, "language": "English"}).json()
    assert first["cached_at"] == again["cached_at"], "a second call within the minute is the cached one"
    fresh = client.get("/api/dashboard/stats",
                       params={"project": ALPHA, "language": "English", "fresh": "true"}).json()
    assert fresh["cached_at"] != first["cached_at"]


# ── The activity feed ────────────────────────────────────────

def test_activities_are_the_newest_rows_across_the_tables(client):
    r = client.get("/api/dashboard/activities",
                   params={"project": ALPHA, "language": "English", "limit": 20})
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert items, "the preseed has rows in every table"

    known = {"sources", "entities", "locations", "events", "ratings",
             "connections", "attributes", "market_insights"}
    for item in items:
        assert item["table"] in known
        assert item["label"]
        assert item["name"]

    # Ordered by arrival, which for the preseed is the same order as the
    # date the documents were commissioned (added = commissioned + 10 min).
    dates = [datetime.fromisoformat(i["date_commissioned"]) for i in items if i["date_commissioned"]]
    assert dates == sorted(dates, reverse=True)

    # The newest document is two hours old, so the feed starts there.
    assert dates[0] > datetime.now(timezone.utc) - timedelta(hours=3)


def test_activities_name_the_entity_a_row_is_about(client):
    r = client.get("/api/dashboard/activities",
                   params={"project": ALPHA, "language": "English", "limit": 50})
    items = r.json()["items"]
    about = [i for i in items if i["about"]]
    assert about, "ratings, connections, attributes and locations are about an entity"
    for item in about:
        for name in item["about"]:
            assert not name.startswith("ent:"), "the raw extraction id must never reach the page"

    links = [i["link"] for i in items if i["link"]]
    assert links
    for link in links:
        assert "project=" in link and "language=" in link, "a link that loses the pair lands elsewhere"


def test_activities_stay_inside_the_project(client):
    beta = client.get("/api/dashboard/activities",
                      params={"project": BETA, "language": "English", "limit": 50}).json()["items"]
    names = " ".join(i["name"] + " " + i["detail"] for i in beta)
    assert "Foxconn" not in names and "Springfield" not in names
    assert any("Contoso" in (i["name"] + " " + " ".join(i["about"])) for i in beta)


# ── The newest events ────────────────────────────────────────

def test_latest_events_carry_their_entities_and_their_source(client):
    r = client.get("/api/dashboard/latest-events",
                   params={"project": ALPHA, "language": "English", "limit": 6})
    assert r.status_code == 200, r.text
    events = r.json()["events"]
    assert len(events) == 6

    by_name = {e["name"]: e for e in events}
    announcement = by_name["Battery announcement"]
    assert announcement["type"] == "Product announcement"
    assert sorted(e["name"] for e in announcement["entities"]) == ["Apple Inc.", "Foxconn"]
    assert announcement["source"]["uri"] == "https://news.example.com/apple-battery"
    assert announcement["dated"] is True

    # The preseed has one event that is about the document and nothing in it.
    hearing = by_name["Hearing scheduled"]
    assert hearing["entities"] == []


def test_latest_events_are_translated(client):
    english = client.get("/api/dashboard/latest-events",
                         params={"project": ALPHA, "language": "English"}).json()["events"]
    german = client.get("/api/dashboard/latest-events",
                        params={"project": ALPHA, "language": "German"}).json()["events"]
    assert len(english) == len(german)
    assert "Product announcement" in {e["type"] for e in english}
    assert "Produktankündigung" in {e["type"] for e in german}


# ── Which clock the tiles count on ───────────────────────────

def test_a_year_old_document_that_arrived_just_now_is_in_the_last_hour(client, archive):
    """THE DEFECT THIS PINS, AND WHY THE PRESEED CANNOT SHOW IT.

    "Archived, per period" is a promise about ARRIVAL. With sources counted
    on date_written and events on date_eventdate, a batch of old documents
    imported this morning would show almost nothing in any window - on a
    real archive, date_added spanning one second while date_written spans
    five years, and the card reading Sources 3/4/6/9 beside Entities
    158/158/158/158.

    Every row in the preseed is commissioned thirty minutes after it is
    written, so on that data the two clocks agree to within half an hour and
    every window is a day or more: the defect is invisible to the rest of
    the suite. This test writes the one row the preseed does not have - written a year
    ago, commissioned a minute ago - and asks the card about the last hour.
    """
    marker = "_preseed:clock:1"
    with archive.connect() as conn:
        conn.execute("""
            INSERT INTO processed_data.sources
                 (date_added, text_name, text_project, text_language, text_task_id,
                  text_job_id, text_source_id, text_uri, text_type, text_importance,
                  bool_trustful, date_written, date_commissioned, date_evaluated)
            VALUES (now(), %s, %s, 'English', %s, %s, 'src:clock',
                    'https://news.example.com/one-year-old', 'News article',
                    'Low Importance', true,
                    now() - interval '1 year', now() - interval '1 minute', now())
        """, (marker, ALPHA, marker, marker))
        conn.commit()
    try:
        tables = stats(client, ALPHA, "English")
        assert tables["sources"]["hour"] == 1, (
            "a document that arrived a minute ago belongs in the last hour, "
            "whatever date the document itself carries")
        # And it is in every wider window as well - the windows nest, they
        # are not five separate questions.
        for period in ("day", "week", "month", "year"):
            assert tables["sources"][period] >= 1, period
    finally:
        with archive.connect() as conn:
            conn.execute("DELETE FROM processed_data.sources WHERE text_task_id = %s", (marker,))
            conn.commit()
        client.get("/api/dashboard/stats",
                   params={"project": ALPHA, "language": "English", "fresh": "true"})
