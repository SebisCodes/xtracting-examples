"""Which project and language a request is about.

Every row in the archive carries a project and a language, and every query the
dashboard runs binds both - a German view of a project is a complete second
set of rows, not a translation layered on top. So every request needs the pair,
and it comes from, in order:

1. the query string (`?project=…&language=…`), which is what links and the
   Export URLs carry so a page can be bookmarked and shared;
2. the cookies `xd.project` / `xd.language`, which the top-bar selector writes
   so the choice survives from one view to the next;
3. the first pair the archive knows about, for someone who has just started
   the dashboard and chosen nothing yet.

A pair that names something the archive does not have is a 400, not a silent
empty page: an empty page looks like "no data", and the difference matters.

TWO MORE THINGS TRAVEL THE SAME WAY, and they are here rather than in a view
because they narrow SIX views at once: `perspective` and `min_importance`.
Together they mean "show only what came from documents that are at least this
important for this reader". They are resolved from the same three places in
the same order, and a Context carries them so that every endpoint which
already takes a ContextDep - including the Export router, which calls the
views' own functions in Python - gets them without a single extra argument.
"" for the perspective is All, which is no filter at all.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, replace
from typing import Annotated, Any
from urllib.parse import unquote

from fastapi import Depends, HTTPException, Request

from .db import Database, get_db

COOKIE_PROJECT = "xd.project"
COOKIE_LANGUAGE = "xd.language"
# THE READER'S PERSPECTIVE, and how much a document must matter to be shown.
# They travel exactly like the pair above - query string, then cookie, then
# the default - because they are the same kind of thing: not a search, but
# which slice of the archive every view is about. Five other places carry the
# pair and every one of them had to learn these two as well, or the CSV a
# person exports would hold rows the screen never showed them.
COOKIE_PERSPECTIVE = "xd.perspective"
COOKIE_MIN_IMPORTANCE = "xd.min_importance"

# What "Show only" starts at once a perspective is chosen. Not the bottom of
# the scale: "Not Important and above" is every judged document, which is a
# filter that looks active and does nothing.
DEFAULT_MIN_IMPORTANCE = "Low Importance"

# Cookies are the only place a pair must not end up with a newline in it.
_MAX_LEN = 200


class ContextError(ValueError):
    """The requested pair does not exist. Carries a hint for the JSON error."""

    def __init__(self, message: str, hint: str) -> None:
        super().__init__(message)
        self.hint = hint


@dataclass(frozen=True)
class Context:
    project: str
    language: str
    #: "" is All - no filter at all, which is what every view starts on.
    perspective: str = ""
    #: The lowest importance still shown, by NAME. The name and not a number,
    #: because the ordering lives in processed_data.importance_types.
    #: float_value and this dashboard does not keep a second copy of a scale
    #: the archive already owns.
    min_importance: str = ""

    @property
    def params(self) -> dict[str, str]:
        """The pair as query parameters, for links the server renders.

        The filter rides along ONLY when it is on: a URL that says
        `perspective=` on every link is noise, and an absent parameter and an
        empty one mean the same thing here."""
        out = {"project": self.project, "language": self.language}
        if self.perspective:
            out["perspective"] = self.perspective
            out["min_importance"] = self.min_importance or DEFAULT_MIN_IMPORTANCE
        return out

    @property
    def filtered(self) -> bool:
        """Whether a view is showing a subset. A number that is a subset has
        to say so, so every view that carries the filter asks this."""
        return bool(self.perspective)


def _clean(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return text[:_MAX_LEN]


def pick_context(query_project: Any, query_language: Any,
                 cookie_project: Any, cookie_language: Any,
                 available: list[tuple[str, str]]) -> Context:
    """The pure decision, kept apart from FastAPI so it can be unit-tested.

    `available` is the list of (project, language) pairs the archive holds, in
    the order /api/projects presents them. A partial request - a project but no
    language, or the other way round - takes the missing half from the first
    matching pair, so a link with only `?project=plant-docs` still works.
    """
    project = _clean(query_project) or _clean(cookie_project)
    language = _clean(query_language) or _clean(cookie_language)
    # A language that only came from the cookie was chosen for some other
    # project. When the URL names a project the cookie's language does not
    # fit, the project wins and its first language completes the pair - a
    # shared link must not 400 because of the reader's last visit.
    language_asked = bool(_clean(query_language))
    pairs = [(p, l) for p, l in available]

    if not pairs:
        # An archive with no tasks yet. Every query answers nothing anyway;
        # the pages say so rather than refusing to render.
        return Context(project or "", language or "English")

    if project and language:
        if (project, language) in pairs:
            return Context(project, language)
        languages = sorted({l for p, l in pairs if p == project})
        if languages and not language_asked:
            for p, l in pairs:
                if p == project:
                    return Context(p, l)
        if languages:
            raise ContextError(
                f"project {project!r} has no rows in language {language!r}",
                "languages this project has: " + ", ".join(languages))
        raise ContextError(
            f"the archive has no project named {project!r}",
            "projects: " + ", ".join(sorted({p for p, _ in pairs})))

    if project:
        for p, l in pairs:
            if p == project:
                return Context(p, l)
        raise ContextError(
            f"the archive has no project named {project!r}",
            "projects: " + ", ".join(sorted({p for p, _ in pairs})))

    if language:
        for p, l in pairs:
            if l == language:
                return Context(p, l)
        # A language the archive lacks entirely is not worth a 400 when
        # nothing else was asked for: fall through to the default pair.

    return Context(*pairs[0])


def pick_filter(query_perspective: Any, query_min: Any,
                cookie_perspective: Any, cookie_min: Any,
                available: list[str]) -> tuple[str, str]:
    """The perspective filter, kept apart from FastAPI like pick_context.

    `available` is the perspectives the CURRENT project and language actually
    hold. A perspective that is not among them is dropped rather than
    honoured, and that is the whole reason this function needs a list at all:
    the choice lives in a cookie, a person switches project, and a
    perspective the new project has never heard of would empty every view on
    every page with nothing on screen saying why. The same trap the language
    half of pick_context has its own paragraph about.

    A perspective is matched case-insensitively against the list and comes
    back SPELLED AS THE ARCHIVE SPELLS IT, because the predicate compares it
    to the stored value.

    Returns ("", "") for All, which is no filter.
    """
    asked = _clean(query_perspective) or _clean(cookie_perspective)
    if not asked:
        return "", ""
    match = next((p for p in available if p.lower() == asked.lower()), "")
    if not match:
        return "", ""
    level = _clean(query_min) or _clean(cookie_min) or DEFAULT_MIN_IMPORTANCE
    return match, level


# ── The pairs the archive knows ─────────────────────────────────────────
#
# Asked for on every request, so cached for a minute. A pair that is missing
# from the cache is looked up once more before it is rejected, which is what
# keeps a project that arrived a second ago from being refused for a minute.

PROJECTS_SQL = """
    SELECT text_project  AS project,
           text_language AS language,
           count(*)      AS tasks,
           max(date_evaluated) AS last_evaluated
      FROM processed_data.tasks
     GROUP BY text_project, text_language
     ORDER BY text_project,
              -- English first: it is the original the translations were made
              -- from, and the one a new user expects to land on.
              (text_language <> 'English'),
              text_language
"""

_CACHE_SECONDS = 60
_lock = threading.Lock()
_cached: tuple[float, list[dict[str, Any]]] = (0.0, [])


def list_projects(db: Database, *, fresh: bool = False) -> list[dict[str, Any]]:
    global _cached
    with _lock:
        stamp, rows = _cached
        if not fresh and rows and time.monotonic() - stamp < _CACHE_SECONDS:
            return rows
    with db.read() as conn:
        rows = [dict(r) for r in conn.execute(PROJECTS_SQL).fetchall()]
    with _lock:
        _cached = (time.monotonic(), rows)
    return rows


def invalidate_projects() -> None:
    global _cached
    with _lock:
        _cached = (0.0, [])


# ── What the perspective filter can be set to ───────────────────────────
#
# THE PERSPECTIVES COME FROM THE DATA, NOT FROM THE VOCABULARY, and that is a
# limit of the archive rather than a preference. `processed_data.
# perspective_types` is harvested by the collector from RATINGS only
# (collector/app/store.py: the perspective of a rating is written to the
# vocabulary, the perspective of a source importance is not), so a
# perspective a customer watches documents under but has never rated an
# entity under is simply missing from that table. Filling the selector from
# /api/types/perspective would therefore offer some of the perspectives and
# silently omit others, and the ones omitted are exactly the ones this filter
# is for. So the selector reads the DISTINCT perspectives the importance
# table actually holds for this project and language.
#
# The importance scale does come from the vocabulary: it is a closed, ordered
# set seeded by database/init/02-vocabularies.sql, and the order is
# float_value, which is the same number the predicate compares.
PERSPECTIVES_SQL = """
    SELECT DISTINCT text_perspective AS name
      FROM processed_data.source_importances_by_perspective
     WHERE text_project = %(project)s AND text_language = %(language)s
       AND text_perspective <> ''
     ORDER BY 1
"""

IMPORTANCES_SQL = """
    SELECT text_name AS name, float_value AS value
      FROM processed_data.importance_types
     WHERE text_project IN (%(project)s, '') AND float_value IS NOT NULL
     ORDER BY float_value, text_name
"""

_filter_lock = threading.Lock()
_filter_cache: dict[tuple[str, str], tuple[float, dict[str, list]]] = {}


def filter_options(db: Database, project: str, language: str) -> dict[str, list]:
    """{"perspectives": [name…], "importances": [{name, value}…]} for one pair.

    Cached for a minute per pair, like the project list, and for the same
    reason: it is asked for on every page render and every request that
    carries a perspective, and neither answer changes between two clicks.
    """
    key = (project, language)
    with _filter_lock:
        hit = _filter_cache.get(key)
        if hit and time.monotonic() - hit[0] < _CACHE_SECONDS:
            return hit[1]
    args = {"project": project, "language": language}
    with db.read() as conn:
        perspectives = [r["name"] for r in conn.execute(PERSPECTIVES_SQL, args).fetchall()]
        importances = [{"name": r["name"], "value": r["value"]}
                       for r in conn.execute(IMPORTANCES_SQL, args).fetchall()]
    data = {"perspectives": perspectives, "importances": importances}
    with _filter_lock:
        _filter_cache[key] = (time.monotonic(), data)
    return data


def invalidate_filter_options() -> None:
    """For tests and for anything that knows the archive has just changed."""
    with _filter_lock:
        _filter_cache.clear()


def resolve_context(request: Request, db: Database,
                    project: str = "", language: str = "") -> Context:
    """The request's pair. Raises HTTPException(400) for a pair the archive
    does not have; the JSON error handler in main.py turns that into
    `{error, hint}`."""
    # layout.js writes the cookies percent-encoded (a project name may hold a
    # space or a semicolon, which a raw cookie value may not), and Starlette
    # hands them back as written.
    cookie_project = unquote(request.cookies.get(COOKIE_PROJECT, ""))
    cookie_language = unquote(request.cookies.get(COOKIE_LANGUAGE, ""))
    query_project = project or request.query_params.get("project", "")
    query_language = language or request.query_params.get("language", "")

    pairs = [(r["project"], r["language"]) for r in list_projects(db)]
    try:
        ctx = pick_context(query_project, query_language, cookie_project, cookie_language, pairs)
    except ContextError:
        pairs = [(r["project"], r["language"]) for r in list_projects(db, fresh=True)]
        try:
            ctx = pick_context(query_project, query_language,
                               cookie_project, cookie_language, pairs)
        except ContextError as exc:
            raise HTTPException(status_code=400, detail={"error": str(exc), "hint": exc.hint})
    return _with_filter(request, db, ctx)


def _with_filter(request: Request, db: Database, ctx: Context) -> Context:
    """The pair, plus the perspective filter that applies to it.

    THE LOOKUP IS SKIPPED WHEN NOTHING IS ASKED FOR, which is the common
    case: every view starts on All, and resolve_context runs on every single
    request. A perspective is only checked against the archive when one was
    actually named, so the default path costs nothing at all.

    A perspective the pair does not hold is dropped rather than refused. It
    is not a 400 the way a wrong project is: the pair still names real rows,
    the view is correct, and it is simply unfiltered - whereas refusing would
    lock a person out of a project with a stale cookie and no way back.
    """
    asked = _clean(request.query_params.get("perspective", "")) or \
        _clean(unquote(request.cookies.get(COOKIE_PERSPECTIVE, "")))
    if not asked:
        return ctx
    available = filter_options(db, ctx.project, ctx.language)["perspectives"]
    perspective, level = pick_filter(
        request.query_params.get("perspective", ""),
        request.query_params.get("min_importance", ""),
        unquote(request.cookies.get(COOKIE_PERSPECTIVE, "")),
        unquote(request.cookies.get(COOKIE_MIN_IMPORTANCE, "")),
        available)
    return replace(ctx, perspective=perspective, min_importance=level)


def current_context(request: Request, project: str = "", language: str = "",
                    db: Database = Depends(get_db)) -> Context:
    """FastAPI dependency: `ctx: ContextDep` on any endpoint. Declaring
    `project` and `language` here is what puts them in every endpoint's
    documentation."""
    return resolve_context(request, db, project, language)


ContextDep = Annotated[Context, Depends(current_context)]
