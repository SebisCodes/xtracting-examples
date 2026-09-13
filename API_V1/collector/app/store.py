"""Turning an extraction into rows.

One task becomes one set of rows per language: the English original is stored
under 'English', and every translation the job requested is stored under its own
language with the same task id. That is what makes a query like

    WHERE text_project = 'plant-docs' AND text_language = 'German'

return a complete, self-consistent German view of the archive.

Writes are idempotent. A job can be fetched again - after a crash, after a
restart, after someone runs the collector twice - and the second pass changes
nothing, because every row carries the task id and the language and is deleted
before being rewritten.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Iterable

import psycopg
from psycopg.rows import dict_row

log = logging.getLogger(__name__)

# Sections of an extraction, and the table each one lands in.
SECTIONS = {
    "Sources": "sources",
    "Entities": "entities",
    "Attributes": "attributes",
    "Events": "events",
    "Ratings": "ratings",
    "Connections": "connections",
    "MarketInsights": "market_insights",
}

_NUMBER = re.compile(r"-?\d+(?:[.,]\d+)?")


def _num(value: Any) -> float | None:
    """The numeric reading of a value, where there is one.

    Kept beside the original string rather than instead of it: "approx. 12.5"
    and "12.5-14" are real answers, and a column that can only hold one number
    would quietly discard what the source actually said.
    """
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if not isinstance(value, str):
        return None
    m = _NUMBER.search(value.replace("'", ""))
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", "."))
    except ValueError:
        return None


def _txt(obj: dict[str, Any], *names: str, default: str = "") -> str:
    """First present key wins. The API uses lowercase keys throughout, but
    accepting a couple of spellings costs nothing and survives a rename."""
    for n in names:
        v = obj.get(n)
        if v is None:
            continue
        if isinstance(v, (list, dict)):
            return json.dumps(v, ensure_ascii=False)
        s = str(v).strip()
        if s:
            return s
    return default


def _ev(obj: dict[str, Any]) -> str | None:
    v = obj.get("evidences")
    if v is None:
        return None
    if isinstance(v, list):
        return ",".join(str(x) for x in v)
    return str(v)


def _ids(obj: dict[str, Any], key: str) -> list[str]:
    """The id list on an item, cleaned, de-duplicated, order preserved.

    Not _txt: that serialises a list into one JSON string, which is what you
    want for evidences and exactly wrong here, because every id becomes its own
    row. Absent, empty, or anything that is not a list at all yields no rows -
    an event that names nothing is the ordinary case, not a fault.
    """
    v = obj.get(key)
    if isinstance(v, str):
        v = [p for p in v.split(",")]
    if not isinstance(v, list):
        return []
    seen: set[str] = set()
    out: list[str] = []
    for entry in v:
        if entry is None:
            continue
        text = str(entry).strip()
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return out


def _rows(result: Any, section: str) -> list[dict[str, Any]]:
    if not isinstance(result, dict):
        return []
    extraction = result.get("extraction")
    if not isinstance(extraction, dict):
        return []
    items = extraction.get(section)
    return [i for i in items if isinstance(i, dict)] if isinstance(items, list) else []


class Store:
    def __init__(self, dsn: str):
        self.dsn = dsn

    def connect(self) -> psycopg.Connection:
        return psycopg.connect(self.dsn, row_factory=dict_row, autocommit=False)

    # ── what is already archived ─────────────────────────────────────
    def recently_collected(self, conn: psycopg.Connection, project: str,
                           hours: int) -> set[str]:
        """Task ids archived for this project within the last `hours`.

        Read straight out of the data rather than from a ledger beside it: a
        second table can disagree with reality, and the row's own existence is
        the only proof that cannot.

        A window rather than the whole history, because results live about an
        hour on the platform - a task older than that cannot be fetched again
        whether or not this collector remembers it. Scanning everything would
        make the lookup grow with the archive forever and return mostly task
        ids that are no longer reachable. With the window, `date_added` is the
        partitioning column, so this touches one or two chunks and stays the
        same size after a year as on the first day.

        Keyed on task id AND project: two projects can hold tasks with the same
        index, and a set that ignored the project would silently skip work.

        Read from `tasks` rather than `sources` for one reason: an extraction
        that produced no sources is legitimate, and a task with no row in a data
        table would be fetched again on every round until it expired. Every
        completed task writes exactly one row per language into `tasks`, in the
        same transaction as its data.
        """
        with conn.cursor() as cur:
            cur.execute(
                """SELECT DISTINCT text_task_id FROM processed_data.tasks
                   WHERE text_project = %s
                     AND date_added > NOW() - make_interval(hours => %s)""",
                (project, hours))
            return {r["text_task_id"] for r in cur.fetchall()}

    # ── writing ──────────────────────────────────────────────────────
    def store_task(self, conn: psycopg.Connection, *, project: str, job: dict[str, Any],
                   task: dict[str, Any]) -> int:
        """One task, in every language it exists in. Returns rows written."""
        task_id = f"{job['jobId']}:{task.get('taskIndex', 0)}"
        written = 0

        versions: list[tuple[str, Any]] = [(task.get("language") or "English", task.get("result"))]
        for tr in task.get("translations") or []:
            if isinstance(tr, dict) and tr.get("language"):
                versions.append((tr["language"], tr.get("result")))

        with conn.cursor() as cur:
            # Delete first, then insert: re-fetching a job must not double it.
            for table in list(SECTIONS.values()) + ["source_importances_by_perspective",
                                                     "event_entities"]:
                cur.execute(
                    f"DELETE FROM processed_data.{table} "
                    "WHERE text_project = %s AND text_task_id = %s", (project, task_id))
            cur.execute(
                "DELETE FROM processed_data.tasks WHERE text_project = %s AND text_task_id = %s",
                (project, task_id))

            for language, result in versions:
                written += self._write_version(
                    cur, project=project, language=language, job=job, task=task,
                    task_id=task_id, result=result)
        return written

    def _write_version(self, cur, *, project: str, language: str, job: dict[str, Any],
                       task: dict[str, Any], task_id: str, result: Any) -> int:
        commissioned = task.get("createdAt") or job.get("createdAt")
        evaluated = task.get("completedAt") or job.get("completedAt")
        # sha256 of the submitted content, as the API reports it. Stored
        # rather than recomputed: this collector never sees the content.
        content_hash = task.get("contentHash") or ""

        cur.execute(
            """INSERT INTO processed_data.tasks
                 (text_name, text_project, text_language, text_job_id, text_task_id,
                  integer_task_index, text_status, text_source_uri, text_content_hash,
                  text_tag, text_error, float_cost_chf, bigint_processing_ms,
                  date_commissioned, date_evaluated)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (task_id, project, language, job["jobId"], task_id,
             task.get("taskIndex", 0), task.get("status", ""), task.get("source") or "",
             content_hash, task.get("tag"), task.get("error"),
             float(task.get("costChf") or 0), task.get("processingTimeMs"),
             commissioned, evaluated))
        n = 1

        base = dict(project=project, language=language, task_id=task_id,
                    job_id=job["jobId"], commissioned=commissioned, evaluated=evaluated)

        for item in _rows(result, "Sources"):
            cur.execute(
                """INSERT INTO processed_data.sources
                     (text_name, text_project, text_language, text_task_id, text_job_id,
                      text_source_id, text_uri, text_content_hash, text_type,
                      text_importance, text_summary,
                      text_reason, text_keywords, text_evidences, bool_trustful,
                      date_written, date_edited,
                      date_commissioned, date_evaluated)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (_txt(item, "id", "uri", default=task_id), project, language,
                 base["task_id"], base["job_id"],
                 _txt(item, "id"), _txt(item, "uri"), content_hash,
                 _txt(item, "articletype"),
                 _txt(item, "importance"), _txt(item, "summary"), _txt(item, "reason"),
                 _txt(item, "evaluationalkeywords"), _ev(item),
                 bool(item.get("trustful", False)),
                 item.get("created"), item.get("edited"),
                 base["commissioned"], base["evaluated"]))
            n += 1

            for imp in item.get("importancebyperspectives") or []:
                if not isinstance(imp, dict):
                    continue
                cur.execute(
                    """INSERT INTO processed_data.source_importances_by_perspective
                         (text_name, text_project, text_language, text_task_id,
                          text_job_id, text_fk_source_id, text_perspective,
                          text_importance, text_reason, date_commissioned, date_evaluated)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (f"{_txt(item, 'id')}|{_txt(imp, 'perspective')}", project, language,
                     base["task_id"], base["job_id"], _txt(item, "id"),
                     _txt(imp, "perspective"), _txt(imp, "importance"), _txt(imp, "reason"),
                     base["commissioned"], base["evaluated"]))
                n += 1

        for item in _rows(result, "Entities"):
            cur.execute(
                """INSERT INTO processed_data.entities
                     (text_name, text_project, text_language, text_task_id, text_job_id,
                      text_entity_id, text_fk_source_id, text_type, text_description,
                      text_keywords, text_relation_to_source, text_evidences,
                      date_commissioned, date_evaluated)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (_txt(item, "name", "id", default="(unnamed)"), project, language,
                 base["task_id"], base["job_id"],
                 _txt(item, "id"), _txt(item, "sourceid"), _txt(item, "type"),
                 _txt(item, "description"), _txt(item, "descriptivekeywords"),
                 _txt(item, "relationtosource"), _ev(item),
                 base["commissioned"], base["evaluated"]))
            n += 1

            # Locations are nested inside their entity rather than being a
            # section of their own.
            for loc in item.get("locations") or []:
                if not isinstance(loc, dict):
                    continue
                cur.execute(
                    """INSERT INTO processed_data.locations
                         (text_name, text_project, text_language, text_task_id,
                          text_job_id, text_fk_entity_id, text_fk_source_id, text_type,
                          text_description, text_address, text_relation_to_source,
                          text_evidences, float_latitude, float_longitude,
                          date_commissioned, date_evaluated)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (_txt(loc, "name", default="(unnamed)"), project, language,
                     base["task_id"], base["job_id"], _txt(item, "id"),
                     _txt(loc, "sourceid") or _txt(item, "sourceid"), _txt(loc, "type"),
                     _txt(loc, "description"), _txt(loc, "address"),
                     _txt(loc, "relationtosource"), _ev(loc),
                     _num(loc.get("latitude") if loc.get("latitude") is not None else loc.get("lat")),
                     _num(loc.get("longitude") if loc.get("longitude") is not None else loc.get("lng")),
                     base["commissioned"], base["evaluated"]))
                n += 1

        for item in _rows(result, "Attributes"):
            value = _txt(item, "value")
            cur.execute(
                """INSERT INTO processed_data.attributes
                     (text_name, text_project, text_language, text_task_id, text_job_id,
                      text_fk_entity_id, text_fk_source_id, text_type, text_unit, text_value,
                      float_value, text_description, text_reason, text_relation_to_source,
                      text_evidences, date_commissioned, date_evaluated)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (_txt(item, "name", default="(unnamed)"), project, language,
                 base["task_id"], base["job_id"], _txt(item, "entityid"),
                 _txt(item, "sourceid"), _txt(item, "type"), _txt(item, "unit"), value,
                 _num(value), _txt(item, "description"), _txt(item, "reason"),
                 _txt(item, "relationtosource"), _ev(item),
                 base["commissioned"], base["evaluated"]))
            n += 1

        for item in _rows(result, "Events"):
            cur.execute(
                """INSERT INTO processed_data.events
                     (text_name, text_project, text_language, text_task_id, text_job_id,
                      text_fk_source_id, text_type, text_description, text_reason,
                      text_relation_to_source, text_evidences, date_eventdate,
                      date_commissioned, date_evaluated)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   RETURNING bigint_id""",
                (_txt(item, "name", default="(unnamed)"), project, language,
                 base["task_id"], base["job_id"], _txt(item, "sourceid"), _txt(item, "type"),
                 _txt(item, "description"), _txt(item, "reason"),
                 _txt(item, "relationtosource"), _ev(item), item.get("date") or None,
                 base["commissioned"], base["evaluated"]))
            n += 1

            # dict_row cursor: the key, not a position.
            event_key = cur.fetchone()["bigint_id"]
            event_name = _txt(item, "name", default="(unnamed)")
            for entity_id in _ids(item, "entityids"):
                cur.execute(
                    """INSERT INTO processed_data.event_entities
                         (text_name, text_project, text_language, text_task_id, text_job_id,
                          text_fk_source_id, bigint_fk_event_id, text_fk_entity_id,
                          date_commissioned, date_evaluated)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (f"{event_name}->{entity_id}", project, language, base["task_id"],
                     base["job_id"], _txt(item, "sourceid"), event_key, entity_id,
                     base["commissioned"], base["evaluated"]))
                n += 1

        for item in _rows(result, "Ratings"):
            cur.execute(
                """INSERT INTO processed_data.ratings
                     (text_name, text_project, text_language, text_task_id, text_job_id,
                      text_fk_entity_id, text_fk_source_id, text_rating_name, text_perspective,
                      text_rating_value, text_reason, text_relation_to_source, text_evidences,
                      date_commissioned, date_evaluated)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (f"{_txt(item, 'entityid')}|{_txt(item, 'ratingname')}|{_txt(item, 'perspective')}",
                 project, language, base["task_id"], base["job_id"],
                 _txt(item, "entityid"), _txt(item, "sourceid"), _txt(item, "ratingname"),
                 _txt(item, "perspective"), _txt(item, "ratingvalue"), _txt(item, "reason"),
                 _txt(item, "relationtosource"), _ev(item),
                 base["commissioned"], base["evaluated"]))
            n += 1

        for item in _rows(result, "Connections"):
            cur.execute(
                """INSERT INTO processed_data.connections
                     (text_name, text_project, text_language, text_task_id, text_job_id,
                      text_fk_source_id, text_fk_parent_entity_id, text_fk_child_entity_id,
                      text_type_parent_to_child, text_type_child_to_parent, text_reason,
                      text_relation_to_source, text_evidences, date_commissioned, date_evaluated)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (f"{_txt(item, 'parententityid')}->{_txt(item, 'childentityid')}",
                 project, language, base["task_id"], base["job_id"], _txt(item, "sourceid"),
                 _txt(item, "parententityid"), _txt(item, "childentityid"),
                 _txt(item, "parentrelationtypetochild"), _txt(item, "childrelationtypetoparent"),
                 _txt(item, "reason"), _txt(item, "relationtosource"), _ev(item),
                 base["commissioned"], base["evaluated"]))
            n += 1

        for item in _rows(result, "MarketInsights"):
            cur.execute(
                """INSERT INTO processed_data.market_insights
                     (text_name, text_project, text_language, text_task_id, text_job_id,
                      text_fk_entity_id, text_fk_source_id, text_topic,
                      text_long_term_outlook, text_short_term_outlook,
                      text_long_term_sentiment, text_short_term_sentiment,
                      bool_high_relevance, text_reason, text_relation_to_source,
                      text_evidences, date_commissioned, date_evaluated)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (f"{_txt(item, 'entityid')}|{_txt(item, 'topic')}", project, language,
                 base["task_id"], base["job_id"], _txt(item, "entityid"),
                 _txt(item, "sourceid"), _txt(item, "topic"),
                 _txt(item, "longTermOutlook", "longtermoutlook", default="Unset"),
                 _txt(item, "shortTermOutlook", "shorttermoutlook", default="Unset"),
                 _txt(item, "longTermSentiment", "longtermsentiment", default="Unset"),
                 _txt(item, "shortTermSentiment", "shorttermsentiment", default="Unset"),
                 bool(item.get("highRelevance", item.get("highrelevance", False))),
                 _txt(item, "reason"), _txt(item, "relationtosource"), _ev(item),
                 base["commissioned"], base["evaluated"]))
            n += 1

        self._learn_types(cur, project, language, task_id, result)
        return n

    def _learn_types(self, cur, project: str, language: str, task_id: str,
                     result: Any) -> None:
        """Record vocabulary the extraction used but the seed did not know.

        Entity types, units and perspectives are yours, not ours - they come
        from the project settings. Collecting them as they appear means a
        dashboard can offer a filter list without scanning every data table.
        """
        pairs: list[tuple[str, Iterable[str]]] = [
            ("entity_types", (_txt(i, "type") for i in _rows(result, "Entities"))),
            ("attribute_types", (_txt(i, "type") for i in _rows(result, "Attributes"))),
            ("unit_types", (_txt(i, "unit") for i in _rows(result, "Attributes"))),
            ("event_types", (_txt(i, "type") for i in _rows(result, "Events"))),
            ("rating_types", (_txt(i, "ratingname") for i in _rows(result, "Ratings"))),
            ("perspective_types", (_txt(i, "perspective") for i in _rows(result, "Ratings"))),
            ("source_types", (_txt(i, "articletype") for i in _rows(result, "Sources"))),
            ("market_topic_types", (_txt(i, "topic") for i in _rows(result, "MarketInsights"))),
        ]
        for table, values in pairs:
            for value in {v for v in values if v}:
                # UNIQUE (text_name, text_project, text_language) on the
                # vocabulary tables, so this is a genuine no-op the second time.
                # On a hypertable the constraint would have to include the
                # timestamp and every task would add a duplicate.
                cur.execute(
                    f"""INSERT INTO processed_data.{table}
                          (text_name, text_project, text_language, text_task_id)
                        VALUES (%s, %s, %s, %s)
                        ON CONFLICT (text_name, text_project, text_language)
                        DO NOTHING""",
                    (value, project, language, task_id))

    def heartbeat(self, conn: psycopg.Connection, service: str, version: str) -> None:
        """One row per service, overwritten: "this one was alive just now".

        WHY THE COLLECTOR NEEDS ONE AT ALL. Every other thing the dashboard
        knows about this service is a consequence of work it did - a run row,
        a task, a source. A collector that has crashed does none of that, so
        the one state worth seeing was the one state invisible: an archive
        that has been quiet for an hour looks exactly the same whether the
        platform had nothing to give or this container died at breakfast.

        The table is shared with the crawler and keyed on the SERVICE, so
        both answer the same question in the same place.
        """
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO monitoring.heartbeat (text_service, date_seen, text_version)
                   VALUES (%s, NOW(), %s)
                   ON CONFLICT (text_service) DO UPDATE
                      SET date_seen = NOW(), text_version = EXCLUDED.text_version""",
                (service, version))

    def record_run(self, conn: psycopg.Connection, *, project: str, key_label: str,
                   status: str, jobs_seen: int, jobs_new: int, rows: int,
                   message: str | None = None) -> None:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO monitoring.collector_runs
                     (text_name, text_project, text_key_label, text_status,
                      integer_jobs_seen, integer_jobs_new, integer_rows, text_message)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                (f"{key_label}@{project or 'unknown'}", project, key_label, status,
                 jobs_seen, jobs_new, rows, message))
