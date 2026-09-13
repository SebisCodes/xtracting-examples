"""End-to-end: a stand-in API, the real collector, the real database.

Runs the collector's own code against a running archive, using a small HTTP
server that answers exactly like the Xtracting jobs API. That is the only way
to prove the parts fit - unit tests on the mapping cannot tell you whether the
SQL is right, and pointing at the live API cannot be run on demand.

    DATABASE_URL=postgresql://... python collector/tests/integration.py
"""

from __future__ import annotations

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import api, config  # noqa: E402
from app.main import collect_for_key, require_archive_schema  # noqa: E402
from app.store import Store  # noqa: E402

PROJECT = "integration-test"

EXTRACTION_EN = {
    "tag": "verify",
    "source": "https://example.invalid/pump-manual",
    "extraction": {
        "Sources": [{
            "id": "src:manual", "uri": "https://example.invalid/pump-manual",
            "articletype": "Technical Manual", "importance": "High Importance",
            "summary": "Specification sheet for a centrifugal pump.",
            "reason": "Clearly stated figures.", "trustful": True,
            "evidences": ["<1>", "<2>"],
            "importancebyperspectives": [
                {"perspective": "Maintenance", "importance": "High Importance",
                 "reason": "Service interval is stated."}],
        }],
        "Entities": [{
            "id": "ent:nordwyk", "name": "Nordwyk NW-180", "type": "Machine",
            "sourceid": "src:manual", "description": "Centrifugal pump.",
            "relationtosource": "Direct", "evidences": ["<1>"],
            "locations": [{"name": "Rotterdam", "type": "Installation",
                           "address": "Rotterdam, Netherlands",
                           "latitude": 51.92, "longitude": 4.47,
                           "relationtosource": "Direct", "evidences": ["<2>"]}],
        }],
        "Attributes": [
            {"name": "flow_rate", "type": "Throughput", "unit": "m³/h", "value": "180",
             "entityid": "ent:nordwyk", "sourceid": "src:manual",
             "relationtosource": "Direct", "evidences": ["<1>"]},
            {"name": "max_pressure", "type": "Pressure", "unit": "bar", "value": "approx. 10",
             "entityid": "ent:nordwyk", "sourceid": "src:manual",
             "relationtosource": "Direct", "evidences": ["<2>"]},
        ],
        "Events": [{"name": "Commissioning", "type": "Milestone", "sourceid": "src:manual",
                    "date": "2019-03-14", "description": "First commissioned.",
                    "relationtosource": "Direct", "evidences": ["<2>"],
                    "entityids": ["ent:nordwyk"]},
                   {"name": "Manual published", "type": "Publication", "sourceid": "src:manual",
                    "date": "2019-01-10", "description": "The manual itself.",
                    "relationtosource": "Direct", "evidences": ["<1>"]}],
        "Ratings": [{"entityid": "ent:nordwyk", "sourceid": "src:manual",
                     "ratingname": "Maintenance Risk", "perspective": "Maintenance",
                     "ratingvalue": "Good", "reason": "Interval is documented.",
                     "relationtosource": "Direct", "evidences": ["<2>"]}],
        "Connections": [],
        "MarketInsights": [{
            "entityid": "ent:nordwyk", "sourceid": "src:manual",
            "topic": "Industrial equipment",
            "longTermOutlook": "Rising", "shortTermOutlook": "Neutral",
            "longTermSentiment": "Positive", "shortTermSentiment": "Slightly Positive",
            "highRelevance": True, "reason": "Steady demand.",
            "relationtosource": "Indirect", "evidences": ["<1>"]}],
    },
}

EXTRACTION_DE = json.loads(json.dumps(EXTRACTION_EN))
EXTRACTION_DE["extraction"]["Entities"][0]["type"] = "Maschine"
EXTRACTION_DE["extraction"]["Attributes"][0]["name"] = "Fördermenge"
EXTRACTION_DE["extraction"]["Attributes"][0]["type"] = "Durchsatz"

JOB = {
    "jobId": "job_itest", "status": "COMPLETED",
    "totalTasks": 1, "completedTasks": 1, "failedTasks": 0, "totalCostChf": 0.4,
    "expiresAt": "2030-01-01T00:00:00.000Z",
    "createdAt": "2026-08-02T09:00:00.000Z",
    "completedAt": "2026-08-02T09:02:30.000Z",
    "projectId": "proj_itest", "projectName": PROJECT,
    "tasks": [{
        "taskIndex": 0, "status": "COMPLETED",
        "source": "https://example.invalid/pump-manual", "tag": "verify",
        "language": "English", "result": EXTRACTION_EN,
        "translations": [{"taskIndex": 0, "status": "COMPLETED",
                          "source": "https://example.invalid/pump-manual",
                          "tag": "verify", "language": "German",
                          "result": EXTRACTION_DE, "error": None}],
        "error": None, "costChf": 0.4, "processingTimeMs": 15000,
        "createdAt": "2026-08-02T09:00:05.000Z",
        "completedAt": "2026-08-02T09:02:20.000Z",
        "contentHash": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    }],
}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):  # keep the test output readable
        pass

    def do_GET(self):
        if self.path.startswith("/api/v1/key"):
            # What the collector asks first: the key's own name, the project
            # it opens, and whether it may read that project.
            body = {"success": True, "data": {
                "name": "integration key", "prefix": "itest",
                "project": {"projectId": "proj_itest", "name": PROJECT},
                "capabilities": {"canExtract": True, "canReadProject": True},
                "usable": True}}
        elif self.path.startswith("/api/v1/project"):
            body = {"success": True, "data": {"projectId": "proj_itest", "name": PROJECT}}
        elif self.path.startswith("/api/v1/batch/"):
            body = {"success": True, "data": JOB}
        elif self.path.startswith("/api/v1/batch"):
            body = {"success": True, "data": {
                "project": {"projectId": "proj_itest", "name": PROJECT},
                "jobs": [{k: JOB[k] for k in
                          ("jobId", "status", "totalTasks", "completedTasks",
                           "failedTasks", "totalCostChf", "expiresAt", "createdAt",
                           "completedAt", "projectId", "projectName")}]}}
        else:
            self.send_response(404); self.end_headers(); return
        payload = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def check(name: str, ok: bool, detail: str = "") -> bool:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{'  ' + detail if detail else ''}")
    return ok


def main() -> int:
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("DATABASE_URL is not set", file=sys.stderr)
        return 2

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"

    store = Store(dsn)
    with store.connect() as conn:
        for t in ("tasks", "sources", "entities", "locations", "events", "ratings",
                  "connections", "attributes", "market_insights", "event_entities",
                  "source_importances_by_perspective"):
            conn.execute(f"DELETE FROM processed_data.{t} WHERE text_project = %s", (PROJECT,))
        conn.commit()

    cfg = config.Config(database_url=dsn, api_url=base,
                        api_keys=[config.ApiKeyEntry(label="itest", secret="test.key")],
                        poll_interval_minutes=15, key_stagger_seconds=0)
    entry = cfg.api_keys[0]

    print("\nfirst pass")
    collect_for_key(cfg, store, entry)

    ok = True
    with store.connect() as conn, conn.cursor() as cur:
        def one(sql, *args):
            cur.execute(sql, args); r = cur.fetchone()
            return list(r.values())[0] if r else None

        ok &= check("task stored in both languages",
                    one("SELECT count(DISTINCT text_language) FROM processed_data.tasks "
                        "WHERE text_project=%s", PROJECT) == 2)
        ok &= check("entity carries the language-specific type",
                    one("SELECT text_type FROM processed_data.entities "
                        "WHERE text_project=%s AND text_language='German'", PROJECT) == "Maschine")
        ok &= check("attribute name is translated",
                    one("SELECT text_name FROM processed_data.attributes "
                        "WHERE text_project=%s AND text_language='German' "
                        "AND text_fk_entity_id='ent:nordwyk' AND text_unit='m³/h'",
                        PROJECT) == "Fördermenge")
        ok &= check("numeric value parsed out of prose",
                    one("SELECT float_value FROM processed_data.attributes "
                        "WHERE text_project=%s AND text_language='English' "
                        "AND text_name='max_pressure'", PROJECT) == 10.0,
                    "'approx. 10' -> 10.0")
        ok &= check("nested location extracted with coordinates",
                    one("SELECT round(float_latitude::numeric,2) FROM processed_data.locations "
                        "WHERE text_project=%s AND text_language='English'", PROJECT) is not None)
        ok &= check("market insight vocabulary stored verbatim",
                    one("SELECT text_long_term_outlook FROM processed_data.market_insights "
                        "WHERE text_project=%s AND text_language='English'", PROJECT) == "Rising")
        ok &= check("source importance by perspective stored",
                    one("SELECT count(*) FROM processed_data.source_importances_by_perspective "
                        "WHERE text_project=%s", PROJECT) == 2, "one per language")
        ok &= check("commissioned/evaluated taken from the task, not the fetch time",
                    one("SELECT date_commissioned::text FROM processed_data.tasks "
                        "WHERE text_project=%s AND text_language='English'",
                        PROJECT).startswith("2026-08-02 09:00:05"))
        ok &= check("open vocabulary learned",
                    one("SELECT count(*) FROM processed_data.entity_types "
                        "WHERE text_project=%s", PROJECT) == 2, "Machine + Maschine")
        ok &= check("run recorded in monitoring",
                    one("SELECT count(*) FROM monitoring.collector_runs "
                        "WHERE text_project=%s AND text_status='OK'", PROJECT) >= 1)
        ok &= check("content hash stored on the source",
                    one("SELECT text_content_hash FROM processed_data.sources "
                        "WHERE text_project=%s AND text_language='English'",
                        PROJECT).startswith("e3b0c442"), "sha256 from the API")
        ok &= check("content hash stored on the task too",
                    one("SELECT count(*) FROM processed_data.tasks "
                        "WHERE text_project=%s AND text_content_hash <> ''",
                        PROJECT) == 2, "one per language")
        ok &= check("dedup reads the task id out of the data itself",
                    one("SELECT count(DISTINCT text_task_id) FROM processed_data.tasks "
                        "WHERE text_project=%s", PROJECT) == 1, "one task id")
        # The heartbeat and the step log are about the services, not about
        # the archive, and carry no task - every other table does.
        ok &= check("every table carries project, language and task id",
                    one("""SELECT count(*) FROM (
                             SELECT table_name FROM information_schema.columns
                             WHERE table_schema IN ('processed_data','monitoring')
                               AND column_name IN
                                   ('text_project','text_language','text_task_id')
                             GROUP BY table_name HAVING count(*) = 3) x""") ==
                    one("""SELECT count(*) FROM information_schema.tables
                           WHERE table_schema IN ('processed_data','monitoring')
                             AND table_name NOT IN ('heartbeat', 'service_log')"""),
                    "all tables")
        # An event that names an entity gets one link row per language; an event
        # that names nothing gets none, and is still stored either way.
        ok &= check("event links are written per language",
                    one("""SELECT count(*) FROM processed_data.event_entities
                           WHERE text_project=%s""", PROJECT) == 2,
                    "one per language for the one linked event")
        ok &= check("both events survive, linked or not",
                    one("""SELECT count(*) FROM processed_data.events
                           WHERE text_project=%s AND text_language='English'""", PROJECT) == 2,
                    "linked and unlinked")
        ok &= check("the link joins event to entity",
                    one("""SELECT count(*) FROM processed_data.events ev
                           JOIN processed_data.event_entities le
                             ON le.bigint_fk_event_id = ev.bigint_id
                            AND le.text_language = ev.text_language
                           JOIN processed_data.entities e
                             ON e.text_entity_id = le.text_fk_entity_id
                            AND e.text_language = le.text_language
                           WHERE ev.text_project=%s AND ev.text_language='English'""",
                        PROJECT) == 1,
                    "one joined row")

        before = one("SELECT count(*) FROM processed_data.attributes WHERE text_project=%s", PROJECT)
        before_links = one("SELECT count(*) FROM processed_data.event_entities WHERE text_project=%s",
                           PROJECT)

    print("\nsecond pass - must change nothing")
    collect_for_key(cfg, store, entry)
    with store.connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM processed_data.attributes WHERE text_project=%s",
                    (PROJECT,))
        after = cur.fetchone()["n"]
    ok &= check("re-running does not duplicate rows", before == after, f"{before} -> {after}")
    with store.connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM processed_data.event_entities WHERE text_project=%s",
                    (PROJECT,))
        after_links = cur.fetchone()["n"]
    ok &= check("re-running does not duplicate event links",
                before_links == after_links, f"{before_links} -> {after_links}")

    # A task outside the lookback window must be treated as unseen. Checked
    # against a copy rather than by moving the real rows: date_added is the
    # partitioning column and cannot be updated in place.
    with store.connect() as conn:
        conn.execute(
            """INSERT INTO processed_data.tasks
                 (date_added, text_name, text_project, text_language, text_task_id,
                  text_job_id, text_status)
               VALUES (NOW() - INTERVAL '5 hours', 'old', %s, 'English', 'old_task',
                       'old_job', 'COMPLETED')""", (PROJECT,))
        conn.commit()
    with store.connect() as conn:
        inside = store.recently_collected(conn, PROJECT, 2)
        outside = store.recently_collected(conn, PROJECT, 8)
    ok &= check("lookback window excludes older tasks",
                "old_task" not in inside, f"2h window: {sorted(inside)}")
    ok &= check("lookback window includes them when widened",
                "old_task" in outside, "8h window")

    # Another project's identically-named vocabulary must be a separate row -
    # that is what makes an archive separable by project later.
    with store.connect() as conn, conn.cursor() as cur:
        for proj in (PROJECT, PROJECT + "-other"):
            cur.execute(
                """INSERT INTO processed_data.entity_types
                     (text_name, text_project, text_language)
                   VALUES ('Machine', %s, 'English')
                   ON CONFLICT (text_name, text_project, text_language) DO NOTHING""",
                (proj,))
        # Twice, to prove the conflict clause actually fires.
        cur.execute(
            """INSERT INTO processed_data.entity_types
                 (text_name, text_project, text_language)
               VALUES ('Machine', %s, 'English')
               ON CONFLICT (text_name, text_project, text_language) DO NOTHING""",
            (PROJECT,))
        cur.execute("SELECT count(*) AS n FROM processed_data.entity_types "
                    "WHERE text_name='Machine' AND text_project LIKE %s", (PROJECT + "%",))
        n = cur.fetchone()["n"]
        conn.commit()
    ok &= check("same name in two projects is two rows, not one", n == 2, f"{n} rows")

    # And a forced re-store of the same task must also not duplicate: this is
    # the path taken after a crash mid-job, where the task is not yet in `known`.
    with store.connect() as conn:
        store.store_task(conn, project=PROJECT, job=JOB, task=JOB["tasks"][0])
        conn.commit()
    with store.connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM processed_data.attributes WHERE text_project=%s",
                    (PROJECT,))
        forced = cur.fetchone()["n"]
    ok &= check("re-storing the same task is idempotent", forced == after, f"{after} -> {forced}")

    with store.connect() as conn:
        for t in ("tasks", "sources", "entities", "locations", "events", "ratings",
                  "connections", "attributes", "market_insights", "event_entities",
                  "source_importances_by_perspective", "entity_types", "attribute_types",
                  "unit_types", "event_types", "rating_types", "perspective_types",
                  "source_types", "market_topic_types"):
            conn.execute(f"DELETE FROM processed_data.{t} WHERE text_project = %s", (PROJECT,))
        conn.execute("DELETE FROM monitoring.collector_runs WHERE text_project = %s", (PROJECT,))
        conn.execute("DELETE FROM processed_data.entity_types WHERE text_project LIKE %s",
                     (PROJECT + "%",))
        conn.commit()

    # ── the guard against the wrong database ──────────────────────────
    # A collector pointed at a PostgreSQL without the archive schema must not
    # fail with a traceback from whichever table it touches first, which reads
    # like a bug in the collector rather than a wrong DATABASE_URL.
    print("\nwrong database")
    ok &= check("a real archive passes the schema guard",
                require_archive_schema(store) is None)

    with store.connect() as conn:
        conn.execute("ALTER TABLE processed_data.tasks RENAME TO tasks_hidden")
        conn.commit()
    try:
        require_archive_schema(store)
        ok &= check("a database without the schema is refused", False, "no error raised")
    except SystemExit as exc:
        ok &= check("a database without the schema is refused",
                    "no archive schema" in str(exc), "stops with a sentence, not a traceback")
    finally:
        with store.connect() as conn:
            conn.execute("ALTER TABLE processed_data.tasks_hidden RENAME TO tasks")
            conn.commit()

    server.shutdown()
    print("\n" + ("ALL CHECKS PASSED" if ok else "SOME CHECKS FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
