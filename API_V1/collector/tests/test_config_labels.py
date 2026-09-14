"""The label a key is reported under never contains the secret.

    python -m pytest tests/test_config_labels.py -q

The label goes into every log line about the key and into
`monitoring.collector_runs.text_key_label`, so it must be safe to show whatever
shape the key has. A key of the form `id.secret` is labelled with its id; a key
without that separator has no non-secret part and gets a short prefix instead.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import config  # noqa: E402


def test_an_explicit_label_is_used_as_written(monkeypatch):
    monkeypatch.setenv("XTRACTING_API_KEYS", "plant-docs:ab12cd34.verysecretpart")
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h/db")
    [entry] = config.load().api_keys
    assert entry.label == "plant-docs"
    assert entry.secret == "ab12cd34.verysecretpart"


def test_a_dotted_key_is_labelled_with_its_id_only():
    assert config.label_for("ab12cd34.verysecretpart") == "ab12cd34"


def test_a_key_without_a_separator_never_becomes_its_own_label():
    secret = "nodotsinthiskeyatall0123456789"
    label = config.label_for(secret)
    assert label == "nodo…"
    assert secret not in label
    assert len(label) < len(secret)


def test_a_short_key_without_a_separator_is_unlabelled():
    assert config.label_for("abcdefgh") == "unlabelled"
    assert config.label_for(".short") == "unlabelled"


@pytest.mark.parametrize("raw", [
    "nodotsinthiskeyatall0123456789",
    "ab12cd34.verysecretpart",
    "short",
])
def test_load_never_puts_the_secret_into_the_label(monkeypatch, raw):
    monkeypatch.setenv("XTRACTING_API_KEYS", raw)
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h/db")
    [entry] = config.load().api_keys
    assert entry.label != entry.secret
    assert entry.secret not in entry.label
    assert entry.prefix == entry.label
