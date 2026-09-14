"""The dashboard application.

    uvicorn app.main:app --port 8088          (from the dashboard/standard/ folder)

`crawlkit` - the crawl rules the Watched pages views run - lives in the API_V1
folder two levels up (../../crawlkit), not inside this one, so that folder has
to be on the path. The image sets `PYTHONPATH=/srv` and pytest.ini lists it;
by hand that is `PYTHONPATH=../.. uvicorn app.main:app --port 8088`.

Start-up follows the collector's, step for step: wait for the database, refuse
a database without the archive schema, then create the dashboard's own schema.
Only after that does the place-list refresher start and the port open, so a
dashboard that answers /healthz is one that can also answer a query.

Routers are included by name and a missing one is a warning, not a crash: the
views are built in parallel, and a build with only half of them must still
start so the half that exists can be looked at.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import sys
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator
from urllib.parse import quote

import psycopg
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import (HTMLResponse, JSONResponse, PlainTextResponse,
                               RedirectResponse, Response)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from . import ASSET_STAMP, __version__, config, gate, schema
from .db import Database, require_archive_schema, set_db, wait_for_database

log = logging.getLogger("dashboard")

APP_DIR = Path(__file__).resolve().parent

# Every router. Order matters only for documentation.
ROUTERS = (
    "pages", "api_meta", "api_suggest", "api_dashboard", "api_query", "api_events",
    "api_diagrams", "api_map", "api_graph", "api_settings", "api_export", "api_sources",
    "api_logs", "api_projects", "api_status",
)


class PlacesRefresher(threading.Thread):
    """Refreshes dashboard.places every DASHBOARD_PLACES_REFRESH_MINUTES.

    Once at start as well: the archive may have changed while the dashboard
    was down, and a suggestion list that is a day old is worse than a
    fifteen-second delay on the first start.
    """

    def __init__(self, db: Database, minutes: int) -> None:
        super().__init__(name="places-refresher", daemon=True)
        self.db = db
        self.interval = minutes * 60
        self.stop_event = threading.Event()

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                if self.db and schema.refresh_places(self.db):
                    log.info("dashboard.places refreshed")
            except Exception:
                # The next round may well succeed; a refresher that dies on
                # one failure leaves the suggestions stale for good.
                log.exception("refreshing dashboard.places failed")
            self.stop_event.wait(self.interval)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    try:
        cfg = config.load()
    except config.ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        raise SystemExit(2)

    logging.basicConfig(
        level=getattr(logging, cfg.log_level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s  %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S")

    # THE GATE'S OWN START-UP LINES, REPLAYED HERE BECAUSE THIS IS WHERE
    # LOGGING BEGINS. `_mount_gate` runs while the application is being built,
    # which is before this function and therefore before the call above: at
    # that moment the root logger has no handler, and Python's last-resort one
    # drops everything under a warning without a word. So the three sentences
    # that say which sources can open this gate - the only place an operator
    # is told that a token-only installation is gated at all - were written
    # into a process that was not yet listening. They are collected there and
    # said here instead, in the order they were written.
    for level, message in getattr(app.state, "gate_startup_log", ()):
        log.log(level, message)

    wait_for_database(cfg)
    db = Database(cfg)
    db.open()
    with db.read() as conn:
        require_archive_schema(conn)
    set_db(db)
    app.state.cfg = cfg
    app.state.db = db
    seeded = schema.ensure_dashboard_schema(db, cfg)

    refresher = PlacesRefresher(db, cfg.places_refresh_minutes)
    refresher.start()
    # The port is uvicorn's to report: DASHBOARD_PORT is the host-side
    # mapping in docker-compose.yml, and by hand `--port` may say anything.
    log.info("dashboard %s started, schema %s, places every %d min",
             __version__, "present" if seeded.present else "absent",
             cfg.places_refresh_minutes)
    try:
        yield
    finally:
        refresher.stop_event.set()
        set_db(None)
        db.close()
        log.info("stopped")


def _path(request: Request) -> str:
    """The path the request line named - from the ASGI scope, not from
    `request.url`. The gate's whole decision rests on this string, and the
    URL object is assembled from the Host header as well, which is the
    sender's to write; the scope's path is what the router matches on and
    nothing in a header reaches it."""
    return str(request.scope.get("path") or "")


def _is_api(request: Request) -> bool:
    return _path(request).startswith("/api/")


# ── The gate ─────────────────────────────────────────────────
#
# AS MIDDLEWARE, NOT AS A DEPENDENCY ON EACH ROUTE. A route somebody adds and
# forgets to decorate would be open, and there are forty of them. Here the
# default is closed and the exceptions are one tuple in app/gate.py.
#
# It runs in front of everything, including /api - a dashboard whose pages
# are gated and whose API answers the whole archive to a plain GET is not
# gated at all.
#
# IT IS ALWAYS INSTALLED, AND THE GATE DECIDES PER REQUEST. Mounting it only
# when DASHBOARD_GATE_PASSWORD holds something would be wrong, because
# dashboard.access_tokens is a second way in: an installation that hands
# out tokens and sets no password would have no middleware at all, and
# would be open to everybody. So the middleware goes on, and `gate.in_force`
# answers "is there anything that could open this?" per request - a
# dictionary lookup where a password is configured, and a memoised query
# every 30 seconds where one is not. An installation that wants no gate still
# pays nothing it can notice and still cannot be locked out by one.


def _gate_config(cfg) -> gate.GateConfig:
    return gate.GateConfig(
        tokens=gate.parse_tokens(cfg.gate_password),
        turnstile_site_key=cfg.turnstile_site_key,
        turnstile_secret=cfg.turnstile_secret,
        max_age_seconds=max(1, cfg.gate_max_age_hours) * 3600)


def _mount_gate(app: FastAPI) -> None:
    cfg = config.load()
    gate_cfg = _gate_config(cfg)
    templates = Jinja2Templates(directory=APP_DIR / "templates")
    # The same two globals routers/pages.py gives its templates. The gate
    # page needs the stylesheet and the version that busts its cache, and it
    # is rendered from here rather than from that router - it must work when
    # the router did not load at all.
    # ONE PLACE DECIDES THE ADDRESS OF A STATIC FILE, and it is here.
    # Templates appending `?v={{ app_version }}` themselves would mean forty
    # places have to remember to do it and one number has to be raised by
    # hand for any of them to matter. `static()` carries the stamp, so a
    # template asks for a file and gets an address that changes when the file does.
    templates.env.globals["static"] = lambda path: f"/static/{path}?v={ASSET_STAMP}"
    templates.env.globals["app_version"] = __version__

    # COLLECTED HERE, SAID BY THE LIFESPAN. Nothing logged from this function
    # would ever be printed: it runs while the application object is being
    # built, and logging.basicConfig is called in `lifespan`, afterwards. An
    # INFO record with no handler in front of it is discarded in silence, so
    # logging them here would leave them existing only in the source. They go
    # into app.state and the lifespan writes them out the moment it has
    # somewhere to write.
    said: list[tuple[int, str]] = []

    # WHICH SOURCES CAN OPEN IT, said at start because the two behave
    # differently: the passwords are fixed until somebody edits a file and
    # restarts this container, while a token can arrive or be revoked between
    # two requests. The count of live tokens is deliberately NOT read here -
    # this runs before the connection pool exists.
    if gate_cfg.enabled:
        said.append((logging.INFO,
                     f"gate: {len(gate_cfg.tokens)} password(s) from "
                     "DASHBOARD_GATE_PASSWORD, and any live row in "
                     "dashboard.access_tokens"))
    else:
        said.append((logging.INFO,
                     "gate: DASHBOARD_GATE_PASSWORD is empty, so the gate is in "
                     "force only while dashboard.access_tokens holds a live token "
                     "- database/manage_tokens.sh lists them. With none, every "
                     "page is open."))

    # THREE STATES, AND THE MIDDLE ONE IS THE DANGEROUS ONE. Said out loud at
    # start, because nothing on the page shows which is running and the
    # difference is between "the form is protected", "the form is a password
    # box anybody may hammer" and "nobody can get in at all".
    if gate_cfg.turnstile_complete:
        said.append((logging.INFO, "gate: with the Cloudflare check on the form"))
    elif gate_cfg.turnstile_broken:
        missing = ("DASHBOARD_TURNSTILE_SECRET" if gate_cfg.turnstile_site_key
                   else "DASHBOARD_TURNSTILE_SITE_KEY")
        said.append((logging.ERROR,
                     f"gate: the Cloudflare check is configured but {missing} is "
                     "empty. NOBODY CAN PASS THE GATE until it is set - or until "
                     "both Turnstile variables are cleared, which runs the gate "
                     "on the password alone."))
    else:
        said.append((logging.INFO,
                     "gate: WITHOUT the Cloudflare check - set "
                     "DASHBOARD_TURNSTILE_SITE_KEY and DASHBOARD_TURNSTILE_SECRET "
                     "to protect the form itself"))

    app.state.gate_startup_log = said

    def form(request: Request, error: str = "", status: int = 200) -> Response:
        return templates.TemplateResponse(request, "gate.html", {
            "error": error,
            # The widget is drawn whenever there is a site key to draw it
            # with. A half-configured gate refuses everybody anyway, and a
            # form that silently dropped the widget would look like a working
            # password box.
            "turnstile_site_key": gate_cfg.turnstile_site_key,
            "next": _safe_next(request.query_params.get("next", "")),
        }, status_code=status)

    throttle = gate.Throttle()

    @app.middleware("http")
    async def close_the_gate(request: Request, call_next):
        path = _path(request)
        if gate.is_open_path(path):
            return await call_next(request)
        # THE TOKEN MEMO MAY ASK THE ARCHIVE, and a database that has to be
        # asked must not be asked on the event loop.
        cookie = request.cookies.get(gate.COOKIE_NAME, "")
        if await run_in_threadpool(gate.passed, cookie, gate_cfg):
            return await call_next(request)
        # AN API CALL GETS AN ANSWER IT CAN READ, not a redirect to an HTML
        # page it would try to parse as JSON. 401 with the same {error, hint}
        # shape every other API failure uses.
        if path.startswith("/api/"):
            return JSONResponse(status_code=401, content={
                "error": "this dashboard is closed",
                "hint": "open it in a browser and enter the password"})
        # The address that was asked for, so passing the gate lands where the
        # person was going rather than on the home page.
        target = path
        if request.url.query:
            target = f"{target}?{request.url.query}"
        return RedirectResponse(f"/gate?next={quote(target, safe='')}", status_code=303)

    @app.get("/gate", response_class=HTMLResponse, include_in_schema=False)
    def gate_form(request: Request) -> Response:
        if gate.passed(request.cookies.get(gate.COOKIE_NAME, ""), gate_cfg):
            return RedirectResponse(_safe_next(request.query_params.get("next", "")),
                                    status_code=303)
        return form(request)

    @app.post("/gate", response_class=HTMLResponse, include_in_schema=False)
    async def gate_submit(request: Request) -> Response:
        address = request.client.host if request.client else ""
        # An address that has used up its tries is answered before the
        # password is looked at; see gate.Throttle.
        if throttle.blocked(address):
            log.info("gate: throttled")
            return form(request, gate.THROTTLED, status=429)
        body = await request.form()
        # OFF THE EVENT LOOP. The attempt reads the archive and may ask
        # Cloudflare, and either one done here would hold every other
        # request still for as long as it takes.
        ok, message = await run_in_threadpool(
            gate.attempt,
            str(body.get("password", "")),
            str(body.get("cf-turnstile-response", "")),
            gate_cfg,
            address)
        if not ok:
            throttle.record_failure(address)
            await asyncio.sleep(gate.refusal_pause())
            return form(request, message, status=401)
        throttle.clear(address)
        response = RedirectResponse(_safe_next(str(body.get("next", ""))), status_code=303)
        response.set_cookie(
            gate.COOKIE_NAME, gate.seal(gate_cfg),
            max_age=gate_cfg.max_age_seconds,
            httponly=True, samesite="lax",
            # Secure only where the request arrived over TLS: set
            # unconditionally, a dashboard served over plain http on an
            # intranet would drop the cookie and loop on the form forever.
            secure=_over_tls(request),
            path="/")
        return response


def _over_tls(request: Request) -> bool:
    """Did this request reach the browser over https?

    The scheme uvicorn reports is the scheme on ITS socket, which behind a
    reverse proxy is plain http unless uvicorn was told to trust that
    proxy's forwarded headers - and the documented deployment (a proxy on
    the host, the container bound to 127.0.0.1) does not set that up. So the
    proxy's own word is read as well. Believing a header the proxy did not
    write costs nothing: the worst a forged X-Forwarded-Proto can do is mark
    the sender's own cookie Secure, and the sender's browser drops it.
    """
    if request.url.scheme == "https":
        return True
    forwarded = request.headers.get("x-forwarded-proto", "")
    return forwarded.split(",")[0].strip().lower() == "https"


def _same_site(request: Request) -> bool:
    """Was this request made by a page of this dashboard, as far as the
    browser will say?

    THE CROSS-SITE FORM. A page on another site can POST a form at any
    address the reader's browser can reach - and with the gate off, which is
    the intranet default, nothing here asked who sent it: a hidden form on
    any web page the reader opened could switch the crawler on, enable a
    watched page or request a paid run. With the gate on, the cookie's
    SameSite=lax keeps it out of such a request, but a gate that is off
    must not mean a dashboard anybody's web page may operate.

    Browsers say where a request came from in two headers: Sec-Fetch-Site
    on every request (`same-origin`, `same-site`, `none` for a typed
    address, `cross-site`), and Origin on every cross-origin one. Both are
    written by the browser and cannot be set by a page. A request that
    carries neither - curl, the tests, an old browser - is let through: it
    is not a browser carrying somebody's cookie into a form it did not
    draw.
    """
    fetch_site = request.headers.get("sec-fetch-site", "").lower()
    if fetch_site:
        return fetch_site != "cross-site"
    origin = request.headers.get("origin", "")
    if not origin or origin.lower() == "null":
        return not origin
    # The name the browser used, or the one a proxy that rewrites Host
    # says it used.
    hosts = {request.headers.get("host", "").lower(),
             request.headers.get("x-forwarded-host", "").split(",")[0].strip().lower()}
    return origin.split("://", 1)[-1].lower() in hosts - {""}


#: Methods that change something. Anything else is a read, and a read
#: is what the gate is for.
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

#: On every answer. `nosniff` stops a browser reading an export or a JSON
#: answer as a page; the referrer policy keeps the address of a query - the
#: search terms are in it - off the sites the archive links to; and the
#: frame rules stop another site drawing this dashboard inside its own page
#: and reading clicks through it.
#:
#: THE REFERRER POLICY SENDS THE ORIGIN, NOT NOTHING. `same-origin` sends no
#: Referer at all to another site, and the map tiles come from another site:
#: tile.openstreetmap.org answers a browser request without a Referer with
#: a 403 tile that reads "Access blocked" (its usage policy asks every app
#: to identify itself), and every map in the product is a grid of that
#: tile. `strict-origin-when-cross-origin` sends the full address only to
#: this dashboard, and to any other site just the origin - the host, never
#: the path, never the search terms - and nothing at all from https to
#: plain http.
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "frame-ancestors 'none'",
}


def _safe_next(value: str) -> str:
    """Where to go after the gate opens - ONLY somewhere on this dashboard.

    An open redirect on the one page a stranger can reach is worth more to
    somebody than the page itself: a link to //evil.example that arrives
    through this host's own address is a phishing page with the customer's
    hostname in front of it. Anything that is not a plain single-slash path
    becomes "/".
    """
    candidate = (value or "").strip()
    if not candidate.startswith("/") or candidate.startswith("//") or "\\" in candidate:
        return "/"
    return candidate


def create_app() -> FastAPI:
    app = FastAPI(title="Xtracting archive dashboard", version=__version__,
                  lifespan=lifespan, docs_url="/api/docs", redoc_url=None,
                  openapi_url="/api/openapi.json")

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon() -> Response:
        # Pages link an empty data: icon; this answers the browsers (and the
        # static test page) that ask for the file anyway, without a 404 in
        # the console.
        return Response(status_code=204)

    app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")

    # ADDED LAST, RUN FIRST: Starlette runs middleware in the reverse order
    # it was added, so this one wraps the gate and the answer of every
    # route, the gate's own included.
    _mount_gate(app)

    @app.middleware("http")
    async def refuse_cross_site_writes(request: Request, call_next):
        if request.method in UNSAFE_METHODS and not _same_site(request):
            log.info("refused a cross-site %s to %s", request.method, _path(request))
            body = {"error": "this request came from another site and was refused",
                    "hint": "the dashboard changes settings only from its own pages"}
            if _is_api(request):
                response: Response = JSONResponse(body, status_code=403)
            else:
                response = PlainTextResponse(body["error"], status_code=403)
        else:
            response = await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        return response

    for name in ROUTERS:
        try:
            module = importlib.import_module(f".routers.{name}", package=__package__)
        except ImportError as exc:
            # A missing router file is the documented case above. A router
            # that IS there and cannot import something is a different thing
            # entirely - the usual one is `crawlkit`, which api_sources needs
            # and which lives in the API_V1 folder (../../crawlkit) rather
            # than inside this one. Left
            # as "not available" that reads like a build choice, and the
            # Watched pages views disappear without anyone knowing why.
            here = (APP_DIR / "routers" / f"{name}.py").exists()
            if here and getattr(exc, "name", "") == "crawlkit":
                log.error("router %s could not be loaded: crawlkit is not on the "
                          "path. It is in the API_V1 folder two levels up; start "
                          "with PYTHONPATH=../.. (the image sets PYTHONPATH=/srv)", name)
            elif here:
                log.error("router %s could not be loaded: %s", name, exc)
            else:
                log.warning("router %s not available: %s", name, exc)
            continue
        app.include_router(module.router)

    # ── Errors ──────────────────────────────────────────────────────────
    #
    # API answers are JSON {error, hint} whatever went wrong, so the page
    # script can show one readable line instead of a stack trace or an HTML
    # 404 body. Pages get plain text.

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        detail = exc.detail
        if isinstance(detail, dict):
            body = {"error": str(detail.get("error", "")), "hint": str(detail.get("hint", ""))}
        else:
            body = {"error": str(detail), "hint": ""}
        if _is_api(request):
            return JSONResponse(body, status_code=exc.status_code, headers=exc.headers)
        text = body["error"] + ("\n" + body["hint"] if body["hint"] else "")
        return PlainTextResponse(text, status_code=exc.status_code, headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e.get('loc', ()) if p not in ('query', 'body'))}: {e.get('msg')}"
            for e in exc.errors())
        body = {"error": "invalid request", "hint": problems}
        if _is_api(request):
            return JSONResponse(body, status_code=400)
        return PlainTextResponse(f"{body['error']}: {problems}", status_code=400)

    @app.exception_handler(psycopg.errors.QueryCanceled)
    async def query_timeout(request: Request, exc: psycopg.errors.QueryCanceled):
        body = {"error": "the query took too long and was stopped",
                "hint": "narrow the timeframe or the search, or raise "
                        "DASHBOARD_STATEMENT_TIMEOUT_SECONDS"}
        if _is_api(request):
            return JSONResponse(body, status_code=504)
        return PlainTextResponse(body["error"] + "\n" + body["hint"], status_code=504)

    return app


app = create_app()
