"""A fake Xtracting API for the flow tests.

Only as much as the crawler's submit and the collector really use - but that
much exactly. It reproduces two habits of the real platform that decide how
the whole loop is reconciled and that nobody sees until they hurt in
production:

  AUTO-SPLIT. Long content is cut into several tasks. `totalTasks` is then
  LARGER than the number of documents submitted, and each part is billed and
  archived on its own.

  THE TAG SUFFIX. On a split, every part gets a zero-padded suffix: the tag
  `xs_7_ab12cd34ef56` becomes `xs_7_ab12cd34ef56-01`, `-02`. That is why the
  tag itself may not contain a hyphen - a rule the database enforces with a
  CHECK - and why `reconcile` matches `tag OR tag||'-%'`.

Without those two the tests would be green and reconcile blind.

It can also be told to misbehave - a status code for the next submission, a
job whose results have expired - so that the triage in `submit.py` can be
checked case by case rather than in principle.

    POST /api/v1/batch          submit
    GET  /api/v1/batch          the project and its jobs (what the collector polls)
    GET  /api/v1/batch/{jobId}  one job with its tasks
    GET  /api/v1/key            what the key in the header is (what the crawler asks)
    GET  /api/v1/project        the project's keys and defaults (a read key only)
"""

from __future__ import annotations

import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse


class FakeState:
    """The platform's state - and the switches that make it misbehave."""

    def __init__(self) -> None:
        self.jobs: dict[str, dict] = {}
        self.counter = 0
        self.project = {"projectId": "proj-1", "name": "crawler-test"}
        #: The keys this platform knows, by their full `prefix.secret`. Empty
        #: AND never filled means "any Bearer value works", which is what every
        #: test that predates the key registry assumes and must keep assuming.
        #:
        #: An entry is what GET /api/v1/key answers: see `add_key`.
        self.keys: dict[str, dict] = {}
        #: Set by add_key and never cleared, so that a test which takes its last
        #: key away gets "no key of ours" rather than the permissive default. A
        #: registry that became permissive by emptying would make "the key was
        #: removed" indistinguishable from "the platform has no opinion".
        self.keys_declared = False
        #: Every batch as it arrived, for assertions about batching.
        self.submissions: list[list[dict]] = []

        # -- the switches --
        #: Split content at this many characters. 0 = never split.
        self.split_at = 0
        #: The status the next submission gets (0 = behave normally). A list
        #: is worked through one call at a time, which is how "500, 500, then
        #: 202" is written in a test.
        self.next_submit_status: int | list[int] = 0
        #: Jobs that answer 410 - results expire after about an hour.
        self.expired: set[str] = set()
        #: Do not inherit the tag when splitting - the case where a submitted
        #: document can never be reconciled.
        self.drop_tag_on_split = False

    def add_key(self, secret: str, *, name: str = "", project_id: str = "",
                project_name: str = "", extract: bool = True,
                read_project: bool = False, edit_project: bool = False,
                usable: bool = True, reason: str = "") -> dict:
        """Teach the platform one key, and answer for it.

        The moment a test calls this, every OTHER Bearer value becomes a 401 -
        which is the point: a registry test needs "this key is not one of ours"
        to be a real answer and not a shrug.
        """
        prefix = secret.split(".", 1)[0]
        entry = {
            "prefix": prefix,
            "name": name or prefix,
            "usable": usable,
            "reason": reason or None,
            "capabilities": {
                "canExtract": extract,
                "canReadProject": read_project,
                "canEditProject": edit_project,
                "readsProject": read_project or edit_project,
            },
            "project": {
                "projectId": project_id or self.project["projectId"],
                "name": project_name or self.project["name"],
            },
            "createdAt": "2026-01-01T00:00:00.000Z",
            "expiresAt": None,
        }
        self.keys[secret] = entry
        self.keys_declared = True
        return entry

    def reset(self) -> None:
        self.__init__()  # noqa: PLC2801 - deliberate, this is a test helper

    def take_status(self) -> int:
        """The status for this submission, consuming it if it was a one-off."""
        value = self.next_submit_status
        if isinstance(value, list):
            return value.pop(0) if value else 0
        self.next_submit_status = 0
        return value


def _split(content: str, at: int) -> list[str]:
    """How the platform cuts content - here simply every `at` characters.

    >>> _split("abcdef", 0)
    ['abcdef']
    >>> _split("abcdef", 2)
    ['ab', 'cd', 'ef']
    """
    if not at or len(content) <= at:
        return [content]
    return [content[i:i + at] for i in range(0, len(content), at)]


def split_tag(tag: str, index: int, parts: int) -> str:
    """The tag of one part - zero-padded to at least two digits.

    >>> split_tag("xs_7_ab12cd34ef56", 0, 1)
    'xs_7_ab12cd34ef56'
    >>> split_tag("xs_7_ab12cd34ef56", 0, 3)
    'xs_7_ab12cd34ef56-01'
    >>> split_tag("xs_7_ab12cd34ef56", 9, 12)
    'xs_7_ab12cd34ef56-10'
    """
    if parts <= 1:
        return tag
    return f"{tag}-{str(index + 1).zfill(max(2, len(str(parts))))}"


class Handler(BaseHTTPRequestHandler):
    state: FakeState
    protocol_version = "HTTP/1.1"

    def log_message(self, *args: object) -> None:
        pass

    # -- helpers -------------------------------------------------------

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _secret(self) -> str:
        auth = self.headers.get("Authorization", "")
        return auth[7:].strip() if auth.startswith("Bearer ") else ""

    def _authorised(self) -> bool:
        """Whether the caller holds a key of ours. Not whether it works.

        The loose default is deliberate: a dozen flow tests predate the key
        registry and hand this a made-up secret. As soon as a test calls
        add_key, the registry decides - and an unknown key is a 401, which is
        what the crawler's probe has to see to throw a key away.

        A key that is REAL but switched off passes this and is refused later,
        by _usable(), with a 403 - except on /api/v1/key, which describes it.
        That split is the whole point of that endpoint and the fake has to have
        it, or a test would prove the opposite of what the platform does.
        """
        secret = self._secret()
        # The original rule, kept exactly: `Bearer ` plus more than three
        # characters. Tests that predate the registry hand this a deliberately
        # short nonsense key and expect a 401, and loosening it by one character
        # would quietly turn those into passes.
        if len(secret) <= 3:
            return False
        if not self.state.keys_declared:
            return True
        return secret in self.state.keys

    def _usable(self) -> bool:
        """Whether the key would be accepted by an endpoint that does work."""
        entry = self.state.keys.get(self._secret())
        return entry is None or bool(entry["usable"])

    def _refuse_unusable(self) -> bool:
        if self._usable():
            return False
        entry = self.state.keys[self._secret()]
        self._json(403, {"success": False,
                         "error": {"code": "FORBIDDEN",
                                   "message": entry.get("reason") or "API key is deactivated"}})
        return True

    # -- POST /api/v1/batch --------------------------------------------

    def do_POST(self) -> None:  # noqa: N802 - the base class names it
        state = self.state
        if not self._authorised():
            self._json(401, {"success": False,
                             "error": {"code": "UNAUTHORIZED",
                                       "message": "no usable key"}})
            return
        if self._refuse_unusable():
            return

        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length) or b"{}")
        submitted = payload.get("tasks") or []
        state.submissions.append(submitted)

        forced = state.take_status()
        if forced == 202:
            # 202 without a jobId: submitted, billed, and not attributable.
            self._json(202, {"success": True, "data": {"totalTasks": len(submitted)}})
            return
        if forced:
            self._json(forced, {"success": False,
                                "error": {"code": str(forced),
                                          "message": "forced by the test"}})
            return

        if not 1 <= len(submitted) <= 1000:
            self._json(400, {"success": False,
                             "error": {"code": "BAD_REQUEST",
                                       "message": "between 1 and 1000 tasks"}})
            return

        state.counter += 1
        job_id = f"job-{state.counter}"
        tasks: list[dict] = []
        for item in submitted:
            content = item.get("content") or ""
            source = item.get("source") or ""
            tag = item.get("tag") or ""
            parts = _split(content, state.split_at)
            for index, part in enumerate(parts):
                part_tag = ("" if state.drop_tag_on_split and len(parts) > 1
                            else split_tag(tag, index, len(parts)))
                tasks.append({
                    "taskIndex": len(tasks),
                    "status": "COMPLETED",
                    "source": source,
                    "tag": part_tag,
                    "language": "English",
                    "contentHash": hashlib.sha256(part.encode("utf-8")).hexdigest(),
                    "error": None,
                    "costChf": 0.2,
                    "processingTimeMs": 1234,
                    "createdAt": "2026-08-08T00:00:00Z",
                    "completedAt": "2026-08-08T00:01:00Z",
                    "translations": [],
                    "result": fake_result(source, part),
                })

        state.jobs[job_id] = {
            "jobId": job_id, "status": "COMPLETED",
            "totalTasks": len(tasks), "completedTasks": len(tasks),
            "failedTasks": 0, "totalCostChf": 0.2 * len(tasks),
            "expiresAt": "2026-12-31T00:00:00Z",
            "createdAt": "2026-08-08T00:00:00Z",
            "completedAt": "2026-08-08T00:01:00Z",
            "projectId": state.project["projectId"],
            "projectName": state.project["name"],
            "tasks": tasks,
        }
        self._json(202, {"success": True, "data": {
            "jobId": job_id, "totalTasks": len(tasks),
            "originalItems": len(submitted), "status": "QUEUED",
            "expiresAt": "2026-12-31T00:00:00Z",
            "pollUrl": f"/api/v1/batch/{job_id}",
        }})

    # -- GET /api/v1/batch[/{jobId}] -----------------------------------

    def do_GET(self) -> None:  # noqa: N802
        state = self.state
        if not self._authorised():
            self._json(401, {"success": False,
                             "error": {"code": "UNAUTHORIZED", "message": "no"}})
            return

        parsed = urlparse(self.path)
        parts = [piece for piece in parsed.path.strip("/").split("/") if piece]

        # What this key is. Needs no capability, and answers 200 even for a key
        # that is switched off - the state is in the body, not in the status.
        if parts == ["api", "v1", "key"]:
            entry = state.keys.get(self._secret())
            if entry is None:
                if state.keys_declared:
                    self._json(401, {"success": False,
                                     "error": {"code": "UNAUTHORIZED",
                                               "message": "Invalid API key"}})
                    return
                # No registry: describe the one project this fake has, so the
                # tests that never named a key still get a usable answer.
                entry = {
                    "prefix": self._secret().split(".", 1)[0] or "fake0000",
                    "name": "fake", "usable": True, "reason": None,
                    "capabilities": {"canExtract": True, "canReadProject": False,
                                     "canEditProject": False, "readsProject": False},
                    "project": dict(state.project),
                    "createdAt": "2026-01-01T00:00:00.000Z", "expiresAt": None,
                }
            self._json(200, {"success": True, "data": entry})
            return

        # The project itself, for a key that may read it.
        if self._refuse_unusable():
            return

        if parts == ["api", "v1", "project"]:
            entry = state.keys.get(self._secret())
            if entry is not None and not entry["capabilities"]["readsProject"]:
                self._json(403, {"success": False,
                                 "error": {"code": "PROJECT_READ_NOT_ALLOWED",
                                           "message": "This key may not read its project."}})
                return
            project_id = (entry or {}).get("project", state.project)["projectId"]
            self._json(200, {"success": True, "data": {
                **state.project,
                "defaultDoubleCheck": False,
                "defaultHighThinking": False,
                "defaultTranslationLanguages": [],
                "defaultVersion": None,
                "apiKeys": [
                    {"prefix": k["prefix"], "name": k["name"],
                     "isActive": k["usable"],
                     "canExtract": k["capabilities"]["canExtract"],
                     "canReadProject": k["capabilities"]["canReadProject"],
                     "canEditProject": k["capabilities"]["canEditProject"],
                     "doubleCheck": k.get("doubleCheck"),
                     "highThinking": k.get("highThinking"),
                     "translationLanguages": k.get("translationLanguages") or [],
                     "version": None, "createdAt": k["createdAt"],
                     "lastUsedAt": None, "expiresAt": None}
                    for k in state.keys.values()
                    if k["project"]["projectId"] == project_id
                ],
            }})
            return

        if parts == ["api", "v1", "batch"]:
            jobs = [{key: value for key, value in job.items() if key != "tasks"}
                    for job in state.jobs.values()]
            self._json(200, {"success": True,
                             "data": {"project": state.project, "jobs": jobs}})
            return

        if len(parts) == 4 and parts[:3] == ["api", "v1", "batch"]:
            job_id = parts[3]
            if job_id in state.expired:
                self._json(410, {"success": False,
                                 "error": {"code": "EXPIRED",
                                           "message": "results have expired"}})
                return
            job = state.jobs.get(job_id)
            if job is None:
                self._json(404, {"success": False,
                                 "error": {"code": "NOT_FOUND",
                                           "message": "unknown job"}})
                return
            retain = parse_qs(parsed.query).get("retain", ["false"])[0] == "true"
            if not retain:
                # Like the original: without retain=true, reading is
                # destructive. The collector sets it; this is what proves it.
                state.jobs[job_id] = {**job, "tasks": [
                    {**task, "result": None} for task in job["tasks"]]}
            self._json(200, {"success": True, "data": job})
            return

        self._json(404, {"success": False,
                         "error": {"code": "NOT_FOUND", "message": self.path}})


def fake_result(source: str, content: str) -> dict:
    """An extraction in the shape `collector/app/store.py` writes.

    Section names are that module's `SECTIONS` keys, not the ones an older
    schema used: a result the collector cannot store would make the flow test
    green about the wrong thing.
    """
    return {
        "source": source,
        "input": content[:200],
        "extraction": {
            "Sources": [{
                "id": "src-1", "uri": source, "articletype": "Publication",
                "importance": "Medium Importance", "summary": "Test summary",
                "reason": "Test", "trustful": True,
                "importancebyperspectives": [
                    {"sourceid": "src-1", "perspective": "Property",
                     "importance": "High Importance", "reason": "Test"}],
            }],
            "Entities": [{
                "id": "ent-1", "name": "Test entry", "sourceid": "src-1",
                "type": "Organisation", "description": "Test",
                "locations": [{"name": "Zurich", "type": "City",
                               "address": "Zurich, ZH, Switzerland",
                               "latitude": 47.37, "longitude": 8.54}],
            }],
            "Events": [], "Ratings": [], "Connections": [],
            "Attributes": [], "MarketInsights": [],
        },
    }


class FakeXtracting:
    """Starts the fake API on a free port.

    >>> with FakeXtracting() as api:
    ...     import httpx
    ...     answer = httpx.post(api.url + "/api/v1/batch",
    ...                         headers={"Authorization": "Bearer test-key"},
    ...                         json={"tasks": [{"content": "hello",
    ...                                          "source": "https://a.example/1",
    ...                                          "tag": "xs_1_abcdef123456"}]})
    ...     answer.status_code, answer.json()["data"]["totalTasks"]
    (202, 1)
    """

    def __init__(self, host: str = "127.0.0.1") -> None:
        self.state = FakeState()
        handler = type("BoundHandler", (Handler,), {"state": self.state})
        self.server = ThreadingHTTPServer((host, 0), handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.url = f"http://{host}:{self.port}"
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self) -> "FakeXtracting":
        self._thread.start()
        return self

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def __enter__(self) -> "FakeXtracting":
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

    # -- what the tests ask it -----------------------------------------

    @property
    def jobs(self) -> dict[str, dict]:
        return self.state.jobs

    def job(self, job_id: str) -> dict:
        return self.state.jobs[job_id]

    def tags(self) -> list[str]:
        """Every tag the platform has recorded, split suffixes included."""
        return [task["tag"] for job in self.state.jobs.values()
                for task in job["tasks"]]


__all__ = ["FakeState", "FakeXtracting", "Handler", "fake_result", "split_tag"]
