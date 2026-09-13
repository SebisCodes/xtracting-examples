"""The Xtracting jobs API, as much of it as this collector needs.

Four endpoints:

    GET /api/v1/key              what the key in the header is: its name, the
                                 project it opens and what it may do
    GET /api/v1/project          the project as a key with "Read project"
                                 sees it - its name and its keys
    GET /api/v1/batch            list the jobs this key has submitted,
                                 plus the project the key belongs to
    GET /api/v1/batch/{jobId}    one job with its tasks, results and
                                 translations

All are read-only. The collector never submits work - it archives what you
submitted, however you submitted it.

The first two are how the log lines get their names. The label in front of a
key in XTRACTING_API_KEYS is yours, for telling entries apart; what the key is
called and which project it opens come from the platform, so a log line can
say both without anybody typing a project name into a file.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

import requests

log = logging.getLogger(__name__)

# 410 means the job's results were dropped by the platform's retention sweep.
# It is a normal outcome, not a fault: results live for an hour and a collector
# that starts late will meet expired jobs.
GONE = 410


class ApiError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


@dataclass
class Project:
    project_id: str
    name: str | None


@dataclass(frozen=True)
class KeyFacts:
    """What one key says about itself, from GET /api/v1/key.

    `known` is False when the platform did not answer the question - an older
    platform without the endpoint, or a network fault - and then nothing else
    here means anything. The round carries on either way: the names are for
    the log, and a log line with the label alone is still a log line.
    """

    known: bool = False
    name: str = ""
    project_id: str = ""
    project_name: str = ""
    can_extract: bool = False
    can_read_project: bool = False
    usable: bool = True
    reason: str = ""


class Client:
    def __init__(self, base_url: str, api_key: str, timeout: int = 120,
                 retries: int = 3):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.retries = retries
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
            "User-Agent": "xtracting-archive-collector/1.0",
        })

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        last: Exception | None = None
        for attempt in range(self.retries):
            try:
                r = self.session.get(url, params=params, timeout=self.timeout)
            except requests.RequestException as exc:
                last = exc
                # Network trouble is usually brief; a 15 minute poll has room
                # to wait a few seconds rather than skipping a whole round.
                wait = 2 ** attempt
                log.warning("GET %s failed (%s), retrying in %ss", path, exc, wait)
                time.sleep(wait)
                continue

            if r.status_code == GONE:
                raise ApiError("job expired", status=GONE)
            if r.status_code == 429 or r.status_code >= 500:
                wait = 2 ** attempt
                log.warning("GET %s returned %s, retrying in %ss", path, r.status_code, wait)
                time.sleep(wait)
                last = ApiError(f"HTTP {r.status_code}", status=r.status_code)
                continue
            if r.status_code == 401 or r.status_code == 403:
                # Not retryable, and the cause is always the key. Say so plainly
                # rather than burying it in a stack trace.
                raise ApiError(
                    f"HTTP {r.status_code}: the API key was rejected. Check "
                    f"XTRACTING_API_KEYS in .env.", status=r.status_code)
            if not r.ok:
                raise ApiError(f"HTTP {r.status_code}: {r.text[:200]}", status=r.status_code)

            body = r.json()
            if not body.get("success", False):
                raise ApiError(str(body.get("error", "unknown API error")))
            return body.get("data", {})

        raise ApiError(f"GET {path} failed after {self.retries} attempts: {last}")

    def describe_key(self) -> KeyFacts:
        """Ask the key what it is. Never raises: an answer the platform cannot
        give is the same as no answer, and the round goes on without a name."""
        try:
            data = self._get("/api/v1/key")
        except ApiError as exc:
            log.info("the key did not describe itself (%s); the log keeps the label", exc)
            return KeyFacts()
        caps = data.get("capabilities") or {}
        project = data.get("project") or {}
        return KeyFacts(
            known=True,
            name=str(data.get("name") or ""),
            project_id=str(project.get("projectId") or ""),
            project_name=str(project.get("name") or ""),
            can_extract=bool(caps.get("canExtract")),
            can_read_project=bool(caps.get("canReadProject") or caps.get("canEditProject")),
            usable=bool(data.get("usable", True)),
            reason=str(data.get("reason") or ""),
        )

    def project_name(self) -> str:
        """The project's name as a key with "Read project" sees it, or "".

        Only asked of a key that says it may read its project: asking any
        other key costs a refused request to learn something it cannot tell.
        """
        try:
            data = self._get("/api/v1/project")
        except ApiError as exc:
            log.info("the project could not be read (%s)", exc)
            return ""
        return str(data.get("name") or "")

    def list_jobs(self) -> tuple[Project, list[dict[str, Any]]]:
        """Every job this key has submitted, newest first, with the project.

        The project comes back even when there are no jobs at all, which is what
        lets a fresh archive know where rows will belong before any work exists.
        """
        data = self._get("/api/v1/batch")
        p = data.get("project") or {}
        return Project(project_id=p.get("projectId") or "", name=p.get("name")), data.get("jobs", [])

    def get_job(self, job_id: str, retain: bool = True) -> dict[str, Any]:
        """One job in full.

        retain=true matters: without it, reading a completed job is the one look
        the platform gives you and the results are dropped afterwards. An
        archiver that consumed the results it archives would make the dashboard
        go blank the moment it ran.
        """
        return self._get(f"/api/v1/batch/{job_id}", params={"retain": "true" if retain else "false"})
