"""The sleep between rounds, and the one thing that may cut it short.

    python -m pytest tests/test_wait_between_rounds.py -q

WHY THIS FILE EXISTS. The collector spends fourteen of every fifteen minutes
asleep, and the manual run from the dashboard is the one case where that is
unbearable: somebody has pressed a button, is looking at the screen, and is
asking "did it arrive?".

A cadence decided ONCE, straight after a round, would leave a document
submitted a second later sitting until the whole interval is up: submitted at
20:40, and the collector would not look until 20:50. Nothing broken, no test
red, and the feature simply not doing what it promises. These three tests are
that promise, written down.

No database and no clock: `wait_between_rounds` asks the store one question and
reads the time, and both are handed to it here, so the whole quarter of an hour
passes in microseconds.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import main  # noqa: E402


class Clock:
    """A monotonic clock that only moves when something sleeps."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


@pytest.fixture
def clock(monkeypatch) -> Clock:
    c = Clock()
    monkeypatch.setattr(main, "time", c)
    monkeypatch.setattr(main, "beat", lambda store: None)
    monkeypatch.setattr(main, "_stop", False)
    return c


def test_nobody_waiting_sleeps_the_whole_interval(clock, monkeypatch):
    monkeypatch.setattr(main, "somebody_is_waiting", lambda store: False)
    started = clock.now
    assert main.wait_between_rounds(None, 900.0, False) is False
    assert clock.now - started == pytest.approx(900.0)
    # In five-second slices, because the heartbeat is written in each of them
    # and because a stop has to be noticed within five seconds.
    assert max(clock.slept) <= 5.0


def test_a_run_that_arrives_during_the_sleep_cuts_it_short(clock, monkeypatch):
    """THE PROMISE THIS FILE IS ABOUT. Twenty seconds into a fifteen-minute sleep
    somebody presses the button; the collector must look thirty seconds later
    and not fourteen and a half minutes later."""
    started = clock.now
    monkeypatch.setattr(main, "somebody_is_waiting",
                        lambda store: clock.now - started >= 20.0)
    assert main.wait_between_rounds(None, 900.0, False) is True
    waited = clock.now - started
    assert waited == pytest.approx(20.0 + main.TEST_POLL_SECONDS, abs=5.0), waited
    assert waited < 60.0, "the sleep was not cut short"


def test_a_collector_already_in_test_mode_does_not_cut_it_again(clock, monkeypatch):
    """Already at the half-minute cadence, the sleep IS the half minute, and
    asking again could only shorten it further - a collector that talked to the
    platform every five seconds because somebody was watching."""
    monkeypatch.setattr(main, "somebody_is_waiting", lambda store: True)
    started = clock.now
    assert main.wait_between_rounds(None, float(main.TEST_POLL_SECONDS), True) is False
    assert clock.now - started == pytest.approx(float(main.TEST_POLL_SECONDS))


def test_a_stop_ends_the_sleep_at_once(clock, monkeypatch):
    """SIGTERM during the fourteen quiet minutes: the container must go down
    within a slice, not when the interval happens to be up."""
    monkeypatch.setattr(main, "somebody_is_waiting", lambda store: False)
    monkeypatch.setattr(main, "_stop", True)
    started = clock.now
    assert main.wait_between_rounds(None, 900.0, False) is False
    assert clock.now == started
