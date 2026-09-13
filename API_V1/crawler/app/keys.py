"""Which projects the keys open, and what each key may do.

The crawler is the only thing in this repository that can know either. It holds
the keys; a key is the only way to ask Xtracting which project it belongs to.
So it asks - on start and on a timer - and writes the answers into
`scraper.projects` and `scraper.api_keys`, where the dashboard reads them.

WHY THIS EXISTS AT ALL. A watchlist carrying only a free-typed label matched
against `XTRACTING_API_KEYS` and validated by nothing means a typo leaves
documents sitting PENDING for ever, and a person choosing a key has no way to
see which project it collects into, what it costs, or whether it still works.
The registry turns all three into something the dashboard can show.

ONE CALL PER KEY. `GET /api/v1/key` answers with the key's name, its project and
its three capabilities, and needs no capability of its own. Before it existed
this had to be inferred from refusals - a 403 EXTRACT_NOT_ALLOWED meant "alive
but may not extract", a 401 meant dead - which could not tell a deactivated key
from a wrong one and could not name a key at all.

The second call is made only for a key that says it may read its project, and
only then: `GET /api/v1/project` for the key list with names, settings and the
effort each key runs at. An ordinary extraction key is never asked, because
asking it would cost a refused request to learn something it already told us.

WHICH VARIABLE A KEY SITS IN DECIDES NOTHING. `XTRACTING_API_KEYS` and
`XTRACTING_PROJECT_KEYS` are both parsed, both probed, and a key works from
either. The split is for a person's own tidiness - "these are the ones that
submit, that one is the read key" - and placement must not be a thing anybody
has to get right.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

import httpx
import psycopg

from . import db, errorlog

log = logging.getLogger(__name__)

#: How long a project may go unused before the dashboard offers to delete it.
#: Seven days rather than a smaller number because a crawler that was off over a
#: holiday should not come back to a list of projects marked for removal.
OLD_AFTER_DAYS = 7


@dataclass(frozen=True)
class KeyFacts:
    """What one key answered about itself.

    `usable` is the platform's own word for "would the endpoints that do work
    accept this right now". `reason` says why not, in the sentence the refusal
    would have used - which is the whole reason the endpoint answers 200 for a
    switched-off key instead of 403.
    """

    prefix: str
    label: str
    order: int
    name: str = ""
    project_id: str = ""
    project_name: str = ""
    can_extract: bool = False
    can_read_project: bool = False
    can_edit_project: bool = False
    usable: bool = False
    reason: str = ""
    created_at: str = ""

    @property
    def reads_project(self) -> bool:
        """Edit is read and write. The same rule the API applies.

        >>> KeyFacts("p", "l", 0, can_edit_project=True).reads_project
        True
        >>> KeyFacts("p", "l", 0).reads_project
        False
        """
        return self.can_read_project or self.can_edit_project


@dataclass
class ProjectFacts:
    """A project, and the keys of it this crawler holds."""

    project_id: str
    name: str
    keys: list[KeyFacts] = field(default_factory=list)
    detailed: bool = False
    defaults: dict[str, Any] | None = None
    #: Prefix -> the detail only a project-read key can see.
    detail: dict[str, dict[str, Any]] = field(default_factory=dict)


def effort_step(double_check: bool | None, high_thinking: bool | None) -> int:
    """What a key costs, as one number a person can compare.

    Four steps, because those are the four combinations that change the price.
    Translations are a separate charge and deliberately not on this ladder: a
    key that translates into three languages is not "more effort", it is more
    output, and mixing the two would make the cheapest key look expensive.

    >>> effort_step(False, False)
    1
    >>> effort_step(False, True)
    2
    >>> effort_step(True, False)
    3
    >>> effort_step(True, True)
    4
    >>> effort_step(None, None)
    1
    """
    return 1 + (2 if double_check else 0) + (1 if high_thinking else 0)


class KeyProbe:
    """Asks Xtracting about each key, once per refresh.

    A separate client per key, because the key is a header set at construction
    and the whole point here is to try several of them.
    """

    def __init__(self, base_url: str, *, timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _get(self, path: str, secret: str) -> tuple[int, dict]:
        with httpx.Client(timeout=self.timeout, headers={
            "Authorization": f"Bearer {secret}",
            "Accept": "application/json",
            "User-Agent": "xtracting-crawler/1.0",
        }) as client:
            response = client.get(f"{self.base_url}{path}")
        try:
            body = response.json()
        except Exception:  # noqa: BLE001
            body = {}
        return response.status_code, (body if isinstance(body, dict) else {})

    def identify(self, entry, order: int) -> KeyFacts | None:
        """One key, described by itself. None when it is not a key of ours.

        A 401 is the only answer that means "throw this away": the key is wrong,
        was deleted, or is a typo in the environment. Everything else - switched
        off, expired, on a stopped project - comes back as a described key whose
        `usable` is false, and the log can then say which of those it was.

        A network failure is also None, and that is a deliberate flattening: the
        caller deletes what it cannot confirm, so a platform that is briefly
        unreachable empties the registry and the next successful refresh fills
        it again. Collection does not stop meanwhile - the submit queue keeps
        its rows - but the dashboard shows no keys, which is honest about what
        we currently know.
        """
        try:
            status, body = self._get("/api/v1/key", entry.secret)
        except httpx.HTTPError as exc:
            log.warning("could not ask about key %s: %s", entry.prefix, exc)
            return None

        if status == 401:
            return None
        if status != 200 or not body.get("success"):
            log.warning("unexpected answer for key %s: HTTP %s", entry.prefix, status)
            return None

        data = body.get("data") or {}
        caps = data.get("capabilities") or {}
        project = data.get("project") or {}
        return KeyFacts(
            prefix=str(data.get("prefix") or entry.prefix),
            label=entry.label,
            order=order,
            name=str(data.get("name") or ""),
            project_id=str(project.get("projectId") or ""),
            project_name=str(project.get("name") or ""),
            can_extract=bool(caps.get("canExtract")),
            can_read_project=bool(caps.get("canReadProject")),
            can_edit_project=bool(caps.get("canEditProject")),
            usable=bool(data.get("usable")),
            reason=str(data.get("reason") or ""),
            created_at=str(data.get("createdAt") or ""),
        )

    def project_detail(self, secret: str) -> dict | None:
        """The project as a key that may read it sees it, or None."""
        try:
            status, body = self._get("/api/v1/project", secret)
        except httpx.HTTPError as exc:
            log.warning("could not read the project: %s", exc)
            return None
        if status != 200 or not body.get("success"):
            return None
        return body.get("data") or {}


def gather(probe: KeyProbe, entries: list) -> dict[str, ProjectFacts]:
    """Everything this crawler can find out about its keys, by project.

    Keys keep the order they were written in, because that order is the only way
    a person can express a preference without holding a project-read key: the
    default for a watchlist is the first live extraction key of its project.
    """
    projects: dict[str, ProjectFacts] = {}

    for order, entry in enumerate(entries):
        facts = probe.identify(entry, order)
        if facts is None:
            continue
        if not facts.project_id:
            log.warning("key %s named no project - ignoring it", facts.prefix)
            continue

        project = projects.get(facts.project_id)
        if project is None:
            project = ProjectFacts(
                project_id=facts.project_id,
                # The collector's rule, word for word (collector/app/main.py):
                # the name, or the id when there is none. Anything else and the
                # registry stops lining up with processed_data.tasks.text_project.
                name=facts.project_name or facts.project_id,
            )
            projects[facts.project_id] = project
        project.keys.append(facts)

        # Only a key that may read the project is asked for the detail, and only
        # once per project: the answer is the same whichever of its keys asks.
        if facts.reads_project and facts.usable and not project.detailed:
            detail = probe.project_detail(entry.secret)
            if detail is not None:
                project.detailed = True
                project.defaults = _defaults_of(detail)
                project.detail = {
                    str(k.get("prefix") or ""): k for k in (detail.get("apiKeys") or [])
                }

    return projects


def _defaults_of(detail: dict) -> dict[str, Any]:
    """The four project defaults, and nothing else from the answer."""
    return {
        "doubleCheck": detail.get("defaultDoubleCheck"),
        "highThinking": detail.get("defaultHighThinking"),
        "translationLanguages": detail.get("defaultTranslationLanguages") or [],
        "version": detail.get("defaultVersion"),
    }


# ----------------------------------------------------------------------
# Writing what was learned
# ----------------------------------------------------------------------

_UPSERT_PROJECT = """
    INSERT INTO scraper.projects
          (text_project_id, text_name, bool_keys_detailed, json_defaults)
    VALUES (%(id)s, %(name)s, %(detailed)s, %(defaults)s)
    ON CONFLICT (text_project_id) DO UPDATE SET
        text_name          = EXCLUDED.text_name,
        date_last_seen     = NOW(),
        bool_keys_detailed = EXCLUDED.bool_keys_detailed,
        -- Keep what a read key once told us rather than blanking it the day the
        -- read key is taken out of the environment: stale detail beside a
        -- warning is more useful than an empty chooser.
        json_defaults      = COALESCE(EXCLUDED.json_defaults, scraper.projects.json_defaults)
"""

_UPSERT_KEY = """
    INSERT INTO scraper.api_keys
          (text_prefix, text_label, integer_order, text_project_id,
           bool_can_extract, bool_can_read_project, date_seen,
           text_name, bool_double_check, bool_high_thinking,
           integer_effort, text_translations, date_created)
    VALUES (%(prefix)s, %(label)s, %(order)s, %(project)s,
            %(extract)s, %(read)s, NOW(),
            %(name)s, %(double)s, %(thinking)s,
            %(effort)s, %(translations)s, %(created)s)
    ON CONFLICT (text_prefix) DO UPDATE SET
        text_label            = EXCLUDED.text_label,
        integer_order         = EXCLUDED.integer_order,
        text_project_id       = EXCLUDED.text_project_id,
        bool_can_extract      = EXCLUDED.bool_can_extract,
        bool_can_read_project = EXCLUDED.bool_can_read_project,
        date_seen             = NOW(),
        text_name             = EXCLUDED.text_name,
        bool_double_check     = EXCLUDED.bool_double_check,
        bool_high_thinking    = EXCLUDED.bool_high_thinking,
        integer_effort        = EXCLUDED.integer_effort,
        text_translations     = EXCLUDED.text_translations,
        date_created          = EXCLUDED.date_created
"""


def store(conn: psycopg.Connection, projects: dict[str, ProjectFacts]) -> list[str]:
    """Write the registry and return the prefixes that disappeared.

    Only keys that authenticated and are usable are stored. A key that is
    switched off is described by the API but is not offered as a choice, because
    offering it would be offering a watchlist that collects nothing.

    The returned prefixes are what the caller warns about: a watchlist pinned to
    one of them has stopped being evaluated, and it can now be named.
    """
    before = {row["text_prefix"] for row in db.fetch_all(
        conn, "SELECT text_prefix FROM scraper.api_keys")}
    kept: set[str] = set()

    for project in projects.values():
        db.execute(conn, _UPSERT_PROJECT, {
            "id": project.project_id,
            "name": project.name,
            "detailed": project.detailed,
            "defaults": json.dumps(project.defaults) if project.defaults else None,
        })

        for facts in project.keys:
            if not facts.usable:
                log.info("key %s (%s) is not usable: %s",
                         facts.prefix, facts.label, facts.reason or "no reason given")
                continue
            detail = project.detail.get(facts.prefix) or {}
            double = detail.get("doubleCheck")
            thinking = detail.get("highThinking")
            db.execute(conn, _UPSERT_KEY, {
                "prefix": facts.prefix,
                "label": facts.label,
                "order": facts.order,
                "project": facts.project_id,
                "extract": facts.can_extract,
                "read": facts.reads_project,
                "name": facts.name or detail.get("name") or "",
                "double": double,
                "thinking": thinking,
                # 0 says "nobody could tell us", which is what an ordinary key
                # leaves behind and what the dashboard hides the chooser for.
                "effort": effort_step(double, thinking) if project.detailed else 0,
                "translations": ", ".join(detail.get("translationLanguages") or []),
                "created": facts.created_at or None,
            })
            kept.add(facts.prefix)

    gone = sorted(before - kept)
    if gone:
        db.execute(conn, "DELETE FROM scraper.api_keys WHERE text_prefix = ANY(%s)",
                   (gone,))
    return gone


def pinned_to(conn: psycopg.Connection, prefixes: list[str]) -> list[dict]:
    """The watchlists that named one of these keys, so a warning can name them.

    Three places can pin a key - the page's row for one of its projects, a link
    group, one address - and all three are the same question to a person:
    "what stopped?"
    """
    if not prefixes:
        return []
    return db.fetch_all(conn, """
        SELECT s.bigint_id, s.text_name, sp.text_key_prefix AS prefix,
               'watchlist' AS where_
          FROM scraper_config.source_projects sp
          JOIN scraper_config.sources s ON s.bigint_id = sp.bigint_fk_source
         WHERE sp.text_key_prefix = ANY(%(p)s)
        UNION ALL
        SELECT s.bigint_id, s.text_name, p.text_key_prefix, 'link group'
          FROM scraper_config.source_patterns p
          JOIN scraper_config.sources s ON s.bigint_id = p.bigint_fk_source
         WHERE p.text_key_prefix = ANY(%(p)s)
        UNION ALL
        SELECT s.bigint_id, s.text_name, e.text_key_prefix, 'address'
          FROM scraper_config.source_exact_urls e
          JOIN scraper_config.sources s ON s.bigint_id = e.bigint_fk_source
         WHERE e.text_key_prefix = ANY(%(p)s)
         ORDER BY 2
    """, {"p": prefixes})


def _stranded(conn: psycopg.Connection) -> dict[int, tuple[str, str]]:
    """Watchlists whose project has no key left that could evaluate them.

    By source id, so two calls can be compared: what matters is which
    watchlists became stranded, not how many are stranded now. A warning about
    a state that has not changed is a warning repeated every tick.
    """
    rows = db.fetch_all(conn, """
        SELECT s.bigint_id, s.text_name,
               string_agg(COALESCE(p.text_name, sp.text_project_id), ', '
                          ORDER BY sp.integer_order) AS project
          FROM scraper_config.sources s
          JOIN scraper_config.source_projects sp ON sp.bigint_fk_source = s.bigint_id
          LEFT JOIN scraper.projects p ON p.text_project_id = sp.text_project_id
         WHERE NOT EXISTS (SELECT 1 FROM scraper.api_keys k
                            WHERE k.text_project_id = sp.text_project_id
                              AND k.bool_can_extract)
         GROUP BY s.bigint_id, s.text_name
    """)
    return {row["bigint_id"]: (row["text_name"], row["project"]) for row in rows}


def refresh(conn: psycopg.Connection, cfg) -> dict[str, ProjectFacts]:
    """One pass: ask about every key, write the registry, warn about losses.

    Called on start and on a timer beside the configuration sync. One error row
    per change and never one per tick, which is the rule configsync already
    follows: a warning repeated every five seconds is a warning nobody reads.
    """
    probe = KeyProbe(cfg.api_url)
    projects = gather(probe, cfg.api_keys)

    # Which watchlists were already stranded, so that only the ones that became
    # stranded by THIS refresh are reported.
    was_stranded = _stranded(conn)

    gone = store(conn, projects)
    for row in pinned_to(conn, gone):
        errorlog.record(conn, source_id=row["bigint_id"],
                        source_name=row["text_name"],
                        **errorlog.key_gone_row(prefix=row["prefix"],
                                                source_name=row["text_name"],
                                                where=row["where_"]))

    # And the other way a watchlist stops: not its own key going, but the last
    # key of its whole project. The fallback deliberately stops at the project
    # boundary, so there is nothing left to fall back to - and that is the one
    # case where nothing else on screen would say why collection went quiet.
    for source_id, (name, project) in _stranded(conn).items():
        if source_id in was_stranded:
            continue
        errorlog.record(conn, source_id=source_id, source_name=name,
                        **errorlog.no_key_row(project_name=project, source_name=name))

    conn.commit()

    if not projects:
        log.warning("no key answered - nothing can be submitted until one does")
    else:
        log.info("%d project(s), %d usable key(s)", len(projects),
                 sum(1 for p in projects.values() for k in p.keys if k.usable))
    return projects


def backfill_prefixes(conn: psycopg.Connection) -> int:
    """Turn the old free-typed labels into a project assignment, once.

    Every watchlist from before the registry points at a key by a label
    somebody typed. The registry knows each label's project and prefix, so the
    migration is a join rather than a guess - and a label that matches no live
    key is left alone, because the honest answer there is "this watchlist names
    a key I do not have", which the dashboard already shows.

    Only for a watchlist that has NO project yet: once somebody has assigned
    projects by hand, a label from before is a nickname and not an instruction.
    """
    written = db.execute(conn, """
        INSERT INTO scraper_config.source_projects
              (bigint_fk_source, text_project_id, text_key_prefix, integer_order)
        SELECT s.bigint_id, k.text_project_id, k.text_prefix, 0
          FROM scraper_config.sources s
          JOIN scraper.api_keys k ON k.text_label = s.text_key_label
         WHERE s.text_key_label <> ''
           AND NOT EXISTS (SELECT 1 FROM scraper_config.source_projects sp
                            WHERE sp.bigint_fk_source = s.bigint_id)
        ON CONFLICT (bigint_fk_source, text_project_id) DO NOTHING
    """)
    if written:
        db.execute(conn, """
            UPDATE scraper_config.sources SET date_updated = NOW()
             WHERE bigint_id IN (SELECT bigint_fk_source
                                   FROM scraper_config.source_projects)
               AND date_updated < NOW() - INTERVAL '1 second'
        """)
    return written


def default_key(conn: psycopg.Connection, project_id: str) -> str | None:
    """The key a watchlist of this project uses when it names none.

    The first live extraction key of that project, in the order the keys were
    written in the environment. `NULL` when the project has none, and that is
    the answer that must NOT be worked around: a watchlist of a project whose
    keys are all gone stops, rather than borrowing a key from another project
    and filing its documents in the wrong archive.
    """
    row = db.fetch_one(conn, """
        SELECT text_prefix FROM scraper.api_keys
         WHERE text_project_id = %(p)s AND bool_can_extract
         ORDER BY integer_order
         LIMIT 1
    """, {"p": project_id})
    return (row or {}).get("text_prefix")
