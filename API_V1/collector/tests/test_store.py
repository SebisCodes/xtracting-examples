"""Tests for the mapping from an extraction to rows.

These run without a database: the mapping is where the mistakes live, and it is
pure. The end-to-end path is covered by tests/integration.py, which needs a
running archive.

    python -m pytest collector/tests -q
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.store import _num, _txt, _ev, _ids, _rows  # noqa: E402


def test_num_reads_plain_numbers():
    assert _num("180") == 180.0
    assert _num("12.5") == 12.5
    assert _num(42) == 42.0
    assert _num(3.5) == 3.5


def test_num_reads_a_number_out_of_a_sentence():
    # "approx. 12.5 bar" is a real answer from a real document.
    assert _num("approx. 12.5 bar") == 12.5
    assert _num("-4 °C") == -4.0
    assert _num("1'200 kg") == 1200.0
    assert _num("12,5") == 12.5


def test_num_gives_up_rather_than_guessing():
    assert _num("several") is None
    assert _num("") is None
    assert _num(None) is None
    # A boolean is not a measurement, however tempting int(True) is.
    assert _num(True) is None


def test_txt_takes_the_first_present_key():
    assert _txt({"name": "A", "id": "B"}, "name", "id") == "A"
    assert _txt({"id": "B"}, "name", "id") == "B"
    assert _txt({"name": "  "}, "name", "id", default="-") == "-"
    assert _txt({}, "name", default="fallback") == "fallback"


def test_txt_serialises_structures_rather_than_dropping_them():
    out = _txt({"v": {"a": 1}}, "v")
    assert out == '{"a": 1}'


def test_evidences_join_into_one_field():
    assert _ev({"evidences": ["<1>", "<2>"]}) == "<1>,<2>"
    assert _ev({"evidences": "<3>"}) == "<3>"
    assert _ev({}) is None


def test_rows_survives_anything_the_api_might_send():
    assert _rows(None, "Entities") == []
    assert _rows({}, "Entities") == []
    assert _rows({"extraction": None}, "Entities") == []
    assert _rows({"extraction": {"Entities": "nope"}}, "Entities") == []
    assert _rows({"extraction": {"Entities": [{"id": "x"}, "junk"]}}, "Entities") == [{"id": "x"}]


# ── Connection diagnostics ────────────────────────────────────────────────
# connection_hint turns a resolver error into the one sentence that names the
# setting to change. It is pure, so it belongs here rather than in the
# integration suite.

from app.main import connection_hint  # noqa: E402


def _dsn(host: str) -> str:
    return f"postgresql://xtracting:pw@{host}:5432/xtracting_archive"


def test_hint_names_localhost_as_the_container_itself():
    hint = connection_hint(_dsn("localhost"), OSError("Connection refused"))
    assert hint is not None
    assert "means this container" in hint
    assert "host.docker.internal" in hint
    # The same trap, spelled two other ways. IPv6 is bracketed in a DSN.
    assert connection_hint(_dsn("127.0.0.1"), OSError("refused")) is not None
    assert connection_hint(_dsn("[::1]"), OSError("refused")) is not None


def test_hint_explains_the_default_host_when_the_database_is_not_running():
    hint = connection_hint(
        _dsn("xtracting_db"), OSError("[Errno -3] Temporary failure in name resolution"))
    assert hint is not None
    assert "archive network" in hint
    assert "Start API_V1/database first" in hint


def test_hint_points_at_the_setting_for_any_other_unresolvable_host():
    hint = connection_hint(
        _dsn("db.example.test"), OSError("Name or service not known"))
    assert hint is not None
    assert "db.example.test" in hint
    assert "POSTGRES_HOST" in hint


def test_hint_stays_quiet_when_it_has_nothing_useful_to_add():
    # A wrong password is not a configuration puzzle - the error already says so,
    # and a hint here would be noise on every real failure.
    assert connection_hint(
        _dsn("xtracting_db"), OSError("password authentication failed")) is None


def test_ids_returns_one_entry_per_reference():
    # Not _txt, which would hand back '["ent:a", "ent:b"]' as a single string.
    # Every id becomes its own row in event_entities, so they have to be split.
    assert _ids({"entityids": ["ent:a", "ent:b"]}, "entityids") == ["ent:a", "ent:b"]


def test_ids_of_an_absent_or_empty_field_is_no_rows():
    # The ordinary case: an event about the document names nothing, and that is
    # not a fault to be reported anywhere.
    assert _ids({}, "entityids") == []
    assert _ids({"entityids": []}, "entityids") == []
    assert _ids({"entityids": None}, "entityids") == []


def test_ids_survives_anything_the_api_might_send():
    assert _ids({"entityids": "ent:a, ent:b"}, "entityids") == ["ent:a", "ent:b"]
    assert _ids({"entityids": {"ent:a": 1}}, "entityids") == []
    assert _ids({"entityids": 7}, "entityids") == []


def test_ids_drops_blanks_and_repeats_but_keeps_order():
    assert _ids({"entityids": ["ent:b", "", "  ", "ent:a", "ent:b"]}, "entityids") \
        == ["ent:b", "ent:a"]
