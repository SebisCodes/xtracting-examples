"""The test runner: one at a time, a hard timeout, and never a dead worker.

No database and no network - the runner is handed a recorder instead of the
snapshot store and a function instead of the crawl engine, which is the whole
reason both are arguments.

    python -m pytest tests/unit/test_testrunner.py -q
"""

from __future__ import annotations

import threading
import time

import pytest

from app.testrunner import (FIRST_PROGRESS, TestBusy, TestRunner, call_execute,
                            get_runner, set_runner)


class Recorder:
    """A stand-in for `sources_store.SnapshotStore`. Records what the runner
    did to each snapshot, in order."""

    def __init__(self, **jobs) -> None:
        self.jobs = {int(k.lstrip("s")): v for k, v in jobs.items()} or {1: {}}
        self.events: list[tuple[int, str, object]] = []
        self.lock = threading.Lock()

    def _add(self, snapshot_id: int, what: str, value: object = None) -> None:
        with self.lock:
            self.events.append((snapshot_id, what, value))

    def job(self, snapshot_id: int):
        row = self.jobs.get(snapshot_id)
        if row is None:
            return None
        return {"id": snapshot_id, "json_config": row.get("config", {}),
                "text_kind": row.get("kind", "crawl"),
                "integer_sample_subpages": row.get("sample", 5)}

    def start(self, snapshot_id: int) -> None:
        self._add(snapshot_id, "start")

    def progress(self, snapshot_id: int, text: str) -> None:
        self._add(snapshot_id, "progress", text)

    def finish(self, snapshot_id: int, result: dict) -> None:
        self._add(snapshot_id, "finish", result)

    def fail(self, snapshot_id: int, error: str) -> None:
        self._add(snapshot_id, "fail", error)

    # ── reading back ──────────────────────────────────────────────
    def kinds(self, snapshot_id: int) -> list[str]:
        return [what for sid, what, _ in self.events if sid == snapshot_id]

    def value(self, snapshot_id: int, what: str):
        for sid, kind, value in self.events:
            if sid == snapshot_id and kind == what:
                return value
        return None

    def progresses(self, snapshot_id: int) -> list[str]:
        return [v for sid, what, v in self.events if sid == snapshot_id and what == "progress"]


@pytest.fixture
def runner_factory():
    """Builds runners and stops them afterwards, so no test leaves a thread
    behind that writes into the next test's recorder."""
    made: list[TestRunner] = []

    def make(store, **kwargs) -> TestRunner:
        runner = TestRunner(store, poll_seconds=0.02, **kwargs)
        made.append(runner)
        return runner

    yield make
    for runner in made:
        runner.stop()


def _wait(runner: TestRunner, seconds: float = 5.0) -> None:
    assert runner.wait_idle(seconds), "the runner did not become idle"


# ── the happy path ───────────────────────────────────────────────


def test_a_test_runs_and_the_result_is_stored(runner_factory):
    store = Recorder(s1={"config": {"text_list_url": "https://a.example/list"}})

    def engine(config, allow_private, progress):
        progress("robots.txt")
        progress("page 1")
        return {"links": [{"url": "https://a.example/rent/1"}], "config": config}

    runner = runner_factory(store, execute=engine)
    runner.submit(1)
    _wait(runner)

    assert store.kinds(1) == ["start", "progress", "progress", "progress", "finish"]
    assert store.progresses(1) == [FIRST_PROGRESS, "robots.txt", "page 1"]
    assert store.value(1, "finish")["config"]["text_list_url"] == "https://a.example/list"


def test_the_same_progress_line_is_not_written_twice(runner_factory):
    store = Recorder(s1={})

    def engine(config, allow_private, progress):
        for _ in range(5):
            progress("page 1 of 3")
        progress("")            # nothing to say is not a step
        progress("page 2 of 3")
        return {}

    runner = runner_factory(store, execute=engine)
    runner.submit(1)
    _wait(runner)
    assert store.progresses(1) == [FIRST_PROGRESS, "page 1 of 3", "page 2 of 3"]


def test_the_job_carries_kind_and_sample_subpages(runner_factory):
    store = Recorder(s1={"kind": "files", "sample": 3})
    seen = {}

    def engine(config, allow_private, progress, kind, sample_subpages):
        seen.update(kind=kind, sample=sample_subpages, private=allow_private)
        return {}

    runner = runner_factory(store, execute=engine)
    runner.submit(1, allow_private=True)
    _wait(runner)
    assert seen == {"kind": "files", "sample": 3, "private": True}


# ── one at a time ────────────────────────────────────────────────


def test_a_second_test_while_one_runs_is_refused(runner_factory):
    store = Recorder(s1={}, s2={})
    started = threading.Event()
    release = threading.Event()

    def engine(config, allow_private, progress):
        started.set()
        release.wait(5)
        return {}

    runner = runner_factory(store, execute=engine)
    runner.submit(1)
    assert started.wait(5)
    assert runner.busy and runner.current == 1

    with pytest.raises(TestBusy) as refused:
        runner.submit(2)
    assert refused.value.snapshot_id == 1

    release.set()
    _wait(runner)
    # And the refused one runs perfectly well once the worker is free again.
    runner.submit(2)
    _wait(runner)
    assert store.kinds(2) == ["start", "progress", "finish"]


def test_two_tests_never_overlap(runner_factory):
    store = Recorder(s1={}, s2={})
    inside = []
    peak = []

    def engine(config, allow_private, progress):
        inside.append(1)
        peak.append(len(inside))
        time.sleep(0.05)
        inside.pop()
        return {}

    runner = runner_factory(store, execute=engine)
    for snapshot_id in (1, 2):
        while runner.busy:
            time.sleep(0.01)
        runner.submit(snapshot_id)
    _wait(runner)
    assert peak == [1, 1]


# ── failures the row has to show ─────────────────────────────────


def test_a_test_that_never_answers_is_failed_after_the_timeout(runner_factory):
    store = Recorder(s1={})
    let_go = threading.Event()

    def engine(config, allow_private, progress):
        let_go.wait(10)          # the crawl that hangs in a socket read
        progress("too late")
        return {"links": []}

    runner = runner_factory(store, execute=engine, timeout_seconds=1)
    runner.submit(1, timeout_seconds=1)
    _wait(runner, 10)

    assert store.kinds(1)[-1] == "fail"
    assert "stopped after 1 seconds" in store.value(1, "fail")
    # The worker is free again even though the abandoned thread is still in
    # the engine - that is the point of the timeout.
    assert not runner.busy

    let_go.set()
    time.sleep(0.1)
    # Its late result is not stored: the snapshot is finished, and finishing
    # it twice would show a failed test as done.
    assert "finish" not in store.kinds(1)


def test_an_engine_that_raises_puts_the_message_on_the_row(runner_factory):
    store = Recorder(s1={})

    def engine(config, allow_private, progress):
        raise ValueError("robots.txt could not be read")

    runner = runner_factory(store, execute=engine)
    runner.submit(1)
    _wait(runner)
    assert store.value(1, "fail") == "ValueError: robots.txt could not be read"


def test_a_result_that_is_not_a_result_is_a_failure(runner_factory):
    store = Recorder(s1={})
    runner = runner_factory(store, execute=lambda config, allow_private, progress: "done")
    runner.submit(1)
    _wait(runner)
    assert "not a result" in store.value(1, "fail")


def test_a_build_without_the_crawl_engine_says_so(runner_factory, monkeypatch):
    store = Recorder(s1={})
    monkeypatch.setattr("app.testrunner.crawlkit_execute",
                        lambda: (_ for _ in ()).throw(ImportError("No module named 'httpx'")))
    runner = runner_factory(store)          # no execute: the real lookup runs
    runner.submit(1)
    _wait(runner)
    assert "cannot crawl" in store.value(1, "fail")
    assert "httpx" in store.value(1, "fail")


def test_a_snapshot_that_disappeared_is_not_an_error(runner_factory):
    store = Recorder(s1={})
    runner = runner_factory(store, execute=lambda **kw: {})
    runner.submit(99)                       # no such job
    _wait(runner)
    assert store.events == []
    assert not runner.busy


def test_a_failing_store_does_not_take_the_worker_down(runner_factory):
    class Broken(Recorder):
        def progress(self, snapshot_id, text):
            raise RuntimeError("the database went away")

    store = Broken(s1={}, s2={})
    runner = runner_factory(store, execute=lambda config, allow_private, progress: {"ok": True})
    runner.submit(1)
    _wait(runner)
    runner.submit(2)
    _wait(runner)
    assert store.kinds(2) == ["start", "finish"]


# ── the call into the engine ─────────────────────────────────────


def test_call_execute_binds_by_name():
    def three(config, allow_private, progress):
        return {"seen": sorted(config), "private": allow_private}

    assert call_execute(three, {"a": 1}, allow_private=True, progress=print) == {
        "seen": ["a"], "private": True}

    def with_kwargs(**kw):
        return {"names": sorted(kw)}

    assert call_execute(with_kwargs, {}, allow_private=False, progress=print)["names"] == [
        "allow_private", "config", "kind", "progress", "sample_subpages", "timeout_seconds"]

    def newer(config, allow_private, progress, kind="crawl", sample_subpages=5):
        return {"kind": kind, "sample": sample_subpages}

    assert call_execute(newer, {}, allow_private=False, progress=print, kind="files",
                        sample_subpages=2) == {"kind": "files", "sample": 2}


def test_call_execute_names_what_it_cannot_supply():
    def strange(config, engines, sink):
        return {}

    with pytest.raises(TypeError) as exc:
        call_execute(strange, {}, allow_private=False, progress=print)
    assert "engines" in str(exc.value) and "sink" in str(exc.value)


# ── the application's single runner ──────────────────────────────


def test_get_runner_returns_the_same_one_and_can_be_swapped():
    set_runner(None)
    try:
        with pytest.raises(RuntimeError):
            get_runner()
        store = Recorder(s1={})
        first = get_runner(store, timeout_seconds=7)
        assert first.timeout_seconds == 7
        assert get_runner(store) is first
        other = TestRunner(store)
        set_runner(other)
        assert get_runner() is other
    finally:
        set_runner(None)
