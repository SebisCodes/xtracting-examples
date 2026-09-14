"""Windows, paging and labels - no database.

    python -m pytest tests/unit/test_timeframes.py -q
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app import timeframes
from app.timeframes import TIMEFRAMES, TimeframeError, add_units, truncate, window_for

# A Wednesday afternoon, so week and month boundaries are not trivially aligned.
NOW = datetime(2026, 3, 11, 14, 37, 12, tzinfo=timezone.utc)


def test_every_timeframe_has_a_date_trunc_unit():
    for tf in TIMEFRAMES.values():
        assert tf.unit in timeframes.DATE_TRUNC_UNITS
        assert 6 <= tf.buckets <= 30, f"{tf.key}: {tf.buckets} bars is not a readable chart"


def test_get_defaults_and_refuses_unknown():
    assert timeframes.get(None).key == timeframes.DEFAULT_TIMEFRAME
    assert timeframes.get("").key == timeframes.DEFAULT_TIMEFRAME
    assert timeframes.get("1y").key == "1y"
    with pytest.raises(TimeframeError, match="unknown timeframe"):
        timeframes.get("2w")


def test_page_zero_ends_at_the_end_of_the_current_bucket():
    w = window_for("7d", 0, NOW)
    assert w.end == datetime(2026, 3, 12, tzinfo=timezone.utc)      # end of today
    assert w.start == datetime(2026, 3, 5, tzinfo=timezone.utc)     # seven whole days
    assert w.start <= NOW < w.end
    assert len(w.buckets()) == 7


def test_page_one_is_the_window_before():
    w0 = window_for("7d", 0, NOW)
    w1 = window_for("7d", 1, NOW)
    assert w1.end == w0.start
    assert w1.start == w0.start - timedelta(days=7)


def test_hours_window():
    w = window_for("24h", 0, NOW)
    assert w.end == datetime(2026, 3, 11, 15, tzinfo=timezone.utc)
    assert w.start == w.end - timedelta(hours=24)
    assert len(w.buckets()) == 24
    assert w.label(w.buckets()[0]) == "15:00"


def test_weeks_start_on_monday_like_date_trunc():
    w = window_for("90d", 0, NOW)
    assert w.unit == "week"
    for b in w.buckets():
        assert b.weekday() == 0
    assert len(w.buckets()) == 13


def test_month_and_quarter_windows_use_calendar_units():
    w = window_for("6m", 0, NOW)
    assert w.start == datetime(2025, 10, 1, tzinfo=timezone.utc)
    assert w.end == datetime(2026, 4, 1, tzinfo=timezone.utc)
    assert [w.label(b) for b in w.buckets()][:2] == ["Oct 2025", "Nov 2025"]

    q = window_for("3y", 0, NOW)
    assert q.unit == "quarter"
    assert q.buckets()[0].month in (1, 4, 7, 10)
    assert q.label(datetime(2026, 1, 1, tzinfo=timezone.utc)) == "Q1 2026"
    assert len(q.buckets()) == 12


def test_add_months_clamps_to_month_end():
    jan31 = datetime(2026, 1, 31, tzinfo=timezone.utc)
    assert add_units(jan31, "month", 1) == datetime(2026, 2, 28, tzinfo=timezone.utc)
    assert add_units(jan31, "month", -1) == datetime(2025, 12, 31, tzinfo=timezone.utc)
    assert add_units(jan31, "quarter", 1) == datetime(2026, 4, 30, tzinfo=timezone.utc)


def test_truncate_matches_postgres():
    t = datetime(2026, 3, 11, 14, 37, 12, 5, tzinfo=timezone.utc)
    assert truncate(t, "hour") == datetime(2026, 3, 11, 14, tzinfo=timezone.utc)
    assert truncate(t, "day") == datetime(2026, 3, 11, tzinfo=timezone.utc)
    assert truncate(t, "week") == datetime(2026, 3, 9, tzinfo=timezone.utc)
    assert truncate(t, "month") == datetime(2026, 3, 1, tzinfo=timezone.utc)
    assert truncate(t, "quarter") == datetime(2026, 1, 1, tzinfo=timezone.utc)
    with pytest.raises(TimeframeError):
        truncate(t, "fortnight")


def test_negative_page_is_refused():
    with pytest.raises(TimeframeError, match="page"):
        window_for("7d", -1, NOW)


def test_naive_now_is_treated_as_utc_and_offsets_are_converted():
    offset = timezone(timedelta(hours=2))
    local = NOW.astimezone(offset)
    assert window_for("7d", 0, local) == window_for("7d", 0, NOW)


def test_caption_names_the_timeframe_only_for_the_current_window():
    assert window_for("7d", 0, NOW).caption == "Last 7 days"
    assert window_for("7d", 1, NOW).caption == "2026-02-26 to 2026-03-04"
    assert "UTC" in window_for("24h", 1, NOW).caption


def test_as_dict_carries_every_bucket_with_a_label():
    d = window_for("30d", 0, NOW).as_dict()
    assert d["timeframe"] == "30d" and d["page"] == 0 and d["unit"] == "day"
    assert len(d["buckets"]) == 30
    assert all(b["label"] and b["start"] for b in d["buckets"])
    assert d["start"] < d["end"]


def test_has_earlier_page_needs_something_older():
    w = window_for("7d", 0, NOW)
    assert timeframes.has_earlier_page(w, None) is False
    assert timeframes.has_earlier_page(w, w.start) is False
    assert timeframes.has_earlier_page(w, w.start - timedelta(seconds=1)) is True
