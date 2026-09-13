"""The HTML pages. One route per view, all rendered from _layout.html.

A view whose template cannot be found renders placeholder.html and says so:
the views are built in parallel, and a navigation with a dead link is worse
than one that leads to a page saying "not in this build".

The API routers only ever return JSON; every server-side render goes through
`render()` here so the layout gets the same variables everywhere - the
current project and language, the view name for the active nav button, and
the version for cache-busting.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from jinja2 import TemplateNotFound

from .. import ASSET_STAMP, __version__
from ..context import (Context, DEFAULT_MIN_IMPORTANCE, filter_options, list_projects,
                       resolve_context)
from ..db import get_db

log = logging.getLogger(__name__)

router = APIRouter(tags=["pages"])

templates = Jinja2Templates(directory=Path(__file__).resolve().parents[1] / "templates")
# ONE PLACE DECIDES THE ADDRESS OF A STATIC FILE, and it is here.
# Templates appending `?v={{ app_version }}` themselves would mean forty
# places have to remember to do it and one number has to be raised by hand
# for any of them to matter. `static()` carries the stamp, so a template
# asks for a file and gets an address that changes when the file does.
templates.env.globals["static"] = lambda path: f"/static/{path}?v={ASSET_STAMP}"
templates.env.globals["app_version"] = __version__

# id, nav label, path. The order is the order in the top bar.
VIEWS: tuple[tuple[str, str, str], ...] = (
    ("dashboard", "Dashboard", "/"),
    ("query", "Query", "/query"),
    ("events", "Events", "/events"),
    ("diagrams", "Diagrams", "/diagrams"),
    ("map", "Map", "/map"),
    ("heatmap", "Heatmap", "/heatmap"),
    ("graph", "Graph", "/graph"),
    ("buckets", "Buckets", "/buckets"),
    ("colours", "Colours", "/colours"),
    # "Sources" is the archive's word for a document on the Dashboard, in
    # Diagrams, in Events and in Query. The crawler's list pages are a
    # different thing entirely, so they get a different word: a customer
    # who reads "Sources 5" on the Dashboard and presses this button used
    # to land on a crawler configuration page.
    #
    # ONE WORD, because eleven buttons already fill 974 of the 1024 px this
    # dashboard is laid out for: "Watched pages" wrapped the bar onto a
    # second row and took 48 px off every view's first screen - which is
    # the height of two rows of the log and the top of the map. The page it
    # opens is the Watchlist; the rows on it are watched pages.
    ("sources", "Watchlist", "/sources"),
    ("logs", "Log", "/logs"),
)

# WHICH VIEWS THE PERSPECTIVE FILTER NARROWS, and therefore which of them say
# so under their heading. A number that is a subset must say it is one.
#
# The five that are absent are absent on purpose, not by oversight. The
# Dashboard counts what ARRIVED - it is the answer to "is the collector
# running", and a counter that moved because of a reading setting would stop
# being that answer. The Log is about the crawler, not about documents.
# Buckets, Colours and the Watchlist are settings pages: they configure what
# the archive means, and a setting that hid half of itself depending on a
# filter would be unusable.
#
# MAP AND HEATMAP ARE IN IT NOW: api_map.py calls sqlbuild.perspective_scope()
# on the heat grid, on the list behind one of its spots, and on the Map's SEED
# entities. The Map's own template adds one sentence to the notice, because
# half of that view obeys the filter and half of it deliberately does not -
# the N-hop expansion is a graph walk, and filtering each hop by the
# importance of its own source would cut paths in the middle (api_map.py:
# _SCOPE_IDS_FILTERED). A page that claimed the whole drawing was narrowed
# would be making a promise the lines on it do not keep.
FILTERED_VIEWS = frozenset({"query", "events", "diagrams", "graph", "map", "heatmap"})

# The pages under /diagrams/{scope}. "summary" is /diagrams itself.
DIAGRAM_SCOPES = {
    "summary": "Summary",
    "entity": "Entity",
    "source": "Source",
    "location": "Location",
    "market": "Market Insights",
    "events": "Events",
}


def _context_or_empty(request: Request) -> Context:
    """Pages never 400 on a wrong pair the way the API does: the layout has to
    render so the selector can be used to fix it. The JavaScript shows the
    API's error when the first fetch fails."""
    try:
        return resolve_context(request, get_db())
    except HTTPException:
        return Context(request.query_params.get("project", ""),
                       request.query_params.get("language", ""))
    except Exception:  # database not initialised, e.g. in a unit test
        return Context("", "")


def render(request: Request, template: str, view: str, title: str, **extra: Any) -> HTMLResponse:
    ctx = _context_or_empty(request)
    try:
        projects = list_projects(get_db())
    except Exception:
        projects = []
    # What the perspective filter can be set to, for THIS pair. Server-rendered
    # like the project list beside it, and cached the same way (context.
    # filter_options), so the top bar is usable before any JavaScript runs and
    # a reader without it is not left with a select holding one value.
    try:
        options = filter_options(get_db(), ctx.project, ctx.language)
    except Exception:
        options = {"perspectives": [], "importances": []}
    variables = {
        "view": view,
        "title": title,
        "views": VIEWS,
        "ctx": ctx,
        "projects": projects,
        "perspectives": options["perspectives"],
        "importances": options["importances"],
        "default_min_importance": DEFAULT_MIN_IMPORTANCE,
        "filter_applies": ctx.filtered and view in FILTERED_VIEWS,
        "diagram_scopes": DIAGRAM_SCOPES,
        "app_version": __version__,   # the footer; the cache stamp rides on static()
        **extra,
    }
    try:
        return templates.TemplateResponse(request, template, variables)
    except TemplateNotFound:
        return templates.TemplateResponse(
            request, "placeholder.html", {**variables, "missing_template": template})


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    return render(request, "dashboard.html", "dashboard", "Dashboard")


@router.get("/query", response_class=HTMLResponse)
def query(request: Request):
    return render(request, "query.html", "query", "Query")


@router.get("/events", response_class=HTMLResponse)
def events(request: Request):
    return render(request, "events.html", "events", "Events")


@router.get("/diagrams", response_class=HTMLResponse)
def diagrams_summary(request: Request):
    return render(request, "diagrams.html", "diagrams", "Diagrams",
                  scope="summary", scope_label=DIAGRAM_SCOPES["summary"])


@router.get("/diagrams/{scope}", response_class=HTMLResponse)
def diagrams(request: Request, scope: str):
    if scope not in DIAGRAM_SCOPES:
        raise HTTPException(404, {"error": f"no diagrams page {scope!r}",
                                  "hint": "one of " + ", ".join(DIAGRAM_SCOPES)})
    return render(request, "diagrams.html", "diagrams",
                  f"Diagrams - {DIAGRAM_SCOPES[scope]}",
                  scope=scope, scope_label=DIAGRAM_SCOPES[scope])


@router.get("/map", response_class=HTMLResponse)
def map_page(request: Request):
    return render(request, "map.html", "map", "Map")


@router.get("/heatmap", response_class=HTMLResponse)
def heatmap(request: Request):
    return render(request, "heatmap.html", "heatmap", "Heatmap")


@router.get("/graph", response_class=HTMLResponse)
def graph(request: Request):
    return render(request, "graph.html", "graph", "Graph")


@router.get("/buckets", response_class=HTMLResponse)
def buckets(request: Request):
    return render(request, "buckets.html", "buckets", "Buckets")


@router.get("/colours", response_class=HTMLResponse)
def colours(request: Request):
    return render(request, "colours.html", "colours", "Connection colours")


@router.get("/projects", response_class=HTMLResponse)
def projects_page(request: Request):
    """The Xtracting projects the crawler holds a key for.

    Deliberately NOT in the top bar. Eleven buttons already fill 974 of the
    1024 px this dashboard is laid out for, and the comment on VIEWS says what a
    twelfth costs: the bar wraps onto a second row and every view loses 48 px of
    its first screen. This page is reached from the Watchlist, which is the only
    place a project is a question anybody is asking.

    It marks the view "sources" so the Watchlist stays lit in the bar - a person
    who came from there has not left it, and a bar with nothing lit reads as a
    page that fell out of the application.
    """
    return render(request, "projects.html", "sources", "Projects")


@router.get("/sources", response_class=HTMLResponse)
def sources(request: Request):
    return render(request, "sources.html", "sources", "Watchlist")


@router.get("/sources/new", response_class=HTMLResponse)
def source_new(request: Request):
    return render(request, "source_edit.html", "sources", "New watched page", source_id=None)


@router.get("/sources/{source_id}", response_class=HTMLResponse)
def source_edit(request: Request, source_id: int):
    return render(request, "source_edit.html", "sources", f"Watched page {source_id}",
                  source_id=source_id)


@router.get("/logs", response_class=HTMLResponse)
def logs(request: Request):
    return render(request, "logs.html", "logs", "Log")
