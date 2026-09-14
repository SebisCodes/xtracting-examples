"""Nothing in this period, and something outside it.

    python -m pytest tests/unit/test_empty_period_offer.py -q

From a real archive: Apple has 7093 rows, the search finds nothing, and the
reason is that the newest entity-linked data is months old while the
default period is the last seven days. An empty table reads as a broken
search, so the page says when the newest row is and offers the period that
holds it.

This is the pure half of that answer: given a date, which window to offer.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.routers.api_diagrams import widen_to

NOW = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)


def offer(days=0, hours=0):
    return widen_to(NOW - timedelta(days=days, hours=hours), NOW)


def test_the_smallest_period_that_reaches_the_data_is_the_one_offered():
    """Not the biggest. "Last 5 years" for a row three days old buries six
    bars in twenty empty ones."""
    assert offer(hours=3)["timeframe"] == "24h"
    assert offer(days=3)["timeframe"] == "7d"
    assert offer(days=20)["timeframe"] == "30d"
    assert offer(days=60)["timeframe"] == "90d"
    assert offer(days=200)["timeframe"] == "1y"
    assert offer(days=730)["timeframe"] == "3y"


def test_the_offer_says_the_period_in_the_words_the_toolbar_uses():
    """The button reads "Show last 30 days", so the label has to be the
    select's own wording - a button naming a token nobody can see ("30d")
    makes the reader press it to find out where it goes."""
    assert offer(days=20)["label"] == "Last 30 days"
    assert offer(days=20)["page"] == 0


def test_data_older_than_every_period_is_reached_by_stepping_back():
    """Past five years there is no window ending now that holds it, so the
    offer is the widest one, stepped back until it does."""
    old = offer(days=365 * 8)
    assert old["timeframe"] == "5y"
    assert old["page"] >= 1
    assert old["caption"], "a page other than the newest is named by its dates"


def test_a_date_in_the_future_is_not_offered_as_a_period():
    """An event dated next month is not a period to widen to: the newest
    window already ends after it, and there is nothing to step back to."""
    assert widen_to(NOW + timedelta(days=400), NOW) is None


def test_the_offer_is_a_window_that_really_contains_the_date():
    from app import timeframes
    for days in (0, 2, 9, 45, 120, 400, 900, 1800):
        when = NOW - timedelta(days=days)
        chosen = widen_to(when, NOW)
        assert chosen, days
        window = timeframes.window_for(chosen["timeframe"], chosen["page"], NOW)
        assert window.start <= when < window.end, (days, chosen)
