"""Which (project, language) a request is about - the pure decision in
app/context.py, without FastAPI or a database.

The order is query string, then cookies, then the archive's first pair. A
pair the archive lacks is an error with a hint; a half pair is completed
from the first matching one.
"""

from __future__ import annotations

import pytest

from app.context import Context, ContextError, pick_context


def test_query_string_wins_over_cookies(pairs):
    ctx = pick_context("_preseed Beta", "English", "_preseed Alpha", "German", pairs)
    assert ctx == Context("_preseed Beta", "English")


def test_cookies_are_used_when_the_query_is_silent(pairs):
    ctx = pick_context("", "", "_preseed Alpha", "German", pairs)
    assert ctx == Context("_preseed Alpha", "German")


def test_first_pair_when_nothing_is_asked(pairs):
    assert pick_context("", "", "", "", pairs) == Context("_preseed Alpha", "English")
    assert pick_context(None, None, None, None, pairs) == Context("_preseed Alpha", "English")


def test_project_alone_takes_its_first_language(pairs):
    ctx = pick_context("_preseed Beta", "", "_preseed Alpha", "German", pairs)
    # The cookie's German does not apply: Beta has no German rows, and the
    # project asked for wins over a language that was not asked for.
    assert ctx == Context("_preseed Beta", "English")


def test_language_alone_takes_the_first_project_with_it(pairs):
    assert pick_context("", "German", "", "", pairs) == Context("_preseed Alpha", "German")


def test_unknown_language_alone_falls_back_quietly(pairs):
    # Nothing else was asked for; a 400 here would leave a fresh browser
    # with a stale cookie on an error page.
    assert pick_context("", "Klingon", "", "", pairs) == Context("_preseed Alpha", "English")


def test_unknown_project_is_an_error_with_the_projects_as_hint(pairs):
    with pytest.raises(ContextError) as exc:
        pick_context("Gamma", "English", "", "", pairs)
    assert "Gamma" in str(exc.value)
    assert "_preseed Alpha" in exc.value.hint and "_preseed Beta" in exc.value.hint


def test_known_project_unknown_language_lists_its_languages(pairs):
    with pytest.raises(ContextError) as exc:
        pick_context("_preseed Beta", "German", "", "", pairs)
    assert "German" in str(exc.value)
    assert exc.value.hint == "languages this project has: English"


def test_stale_cookie_pair_is_an_error_too(pairs):
    # The cookie names a pair that no longer exists (the project was
    # removed). The layout still renders - pages catch this - but the API
    # must not silently answer for another pair.
    with pytest.raises(ContextError):
        pick_context("", "", "Gone", "English", pairs)


def test_values_are_trimmed_and_capped(pairs):
    ctx = pick_context("  _preseed Alpha ", " German ", "", "", pairs)
    assert ctx == Context("_preseed Alpha", "German")
    long = "x" * 1000
    with pytest.raises(ContextError) as exc:
        pick_context(long, "English", "", "", pairs)
    assert len(str(exc.value)) < 400


def test_empty_archive_renders_rather_than_refusing():
    ctx = pick_context("", "", "", "", [])
    assert ctx.language == "English"
    assert ctx.project == ""
    # A request that names a pair keeps it, so the selector shows the choice.
    assert pick_context("Alpha", "German", "", "", []) == Context("Alpha", "German")


def test_params_carry_the_pair(pairs):
    ctx = pick_context("", "", "", "", pairs)
    assert ctx.params == {"project": "_preseed Alpha", "language": "English"}
