"""No endpoint ever answers with a document's identifier.

    DATABASE_URL=postgresql://... python -m pytest tests/flow/test_source_names_api.py -q

Measured on a real archive: `sources.text_name` can be `src_1416664` in some
rows and an md5 in others, in every row. Three views print it - the
Dashboard's two panels and the Events list would show "Source: src_1127664"
under every row, and the Query card would be headed with it - which is a
heading that identifies the row to a machine and says nothing to a reader.

A document that DOES carry a real name keeps it, untouched - the OTHER half
of the rule, and the reason the test is on the value rather than on the
field. So this file adds one document of each of the three shapes, in a
project of its own, and asks the endpoints what they say about them.
"""

from __future__ import annotations

from typing import Iterator
from urllib.parse import parse_qs, urlsplit

import pytest

#: ITS OWN PROJECT, not the preseed's.
#:
#: These rows change what an archive holds - three more documents, three
#: more entities, three more places - and half this suite counts the
#: preseed's rows to the unit. Added to `_preseed Alpha`, they would reach
#: every other test through the stats cache - "Sources 8" where 5 is
#: asserted, a minute after these rows were already gone. A project of its
#: own cannot do that to anybody, and the endpoints do not care which
#: project they are asked about.
PROJECT = "_srcname project"

# Far above anything the preseed writes, and prefixed so a leaked row is
# recognisable in a database somebody is looking at by hand.
TASK = "_srcname task"
SERIAL_ID = "_srcname-serial"
CHECKSUM_ID = "_srcname-checksum"
NAMED_ID = "_srcname-named"

SERIAL_NAME = "src_9990001"
CHECKSUM_NAME = "8846ebcc63ff6a49245ef85e08e776de"
REAL_NAME = "A document that came with its own name"

URI = "https://www.srcname.example/world/2026/01/09/a-title-worth-reading"
DOMAIN = "srcname.example"
DERIVED = "A title worth reading"

#: One entity and one location per document, because the Query view is
#: anchored on a place: a source no address reaches is a source that view
#: never shows, and the assertion about its heading would pass by being
#: unreachable rather than by being right. The address is invented and far
#: from the preseed's own, so it cannot collide with another file's counts.
ADDRESS = "Srcname Harbour, Testland"
LAT, LNG = 12.5, 42.5

#: A PROJECT EXISTS BECAUSE A TASK SAYS SO. /api/projects reads
#: `processed_data.tasks` (app/context.py, PROJECTS_SQL), so a project made
#: only of sources is a project every request answers 400 for. One task row
#: per project is what makes the pair real.
_TASK = """
    INSERT INTO processed_data.tasks
      (date_added, text_name, text_project, text_language, text_task_id, text_job_id,
       integer_task_index, text_status, text_source_uri, text_content_hash, text_tag,
       float_cost_chf, bigint_processing_ms, date_commissioned, date_evaluated)
    VALUES (now(), %(task)s, %(project)s, 'English', %(task)s, %(task)s,
            0, 'COMPLETED', %(uri)s, %(task)s, 'srcname', 0.01, 1200, now(), now())
"""

_INSERT_ENTITY = """
    INSERT INTO processed_data.entities
      (date_added, text_name, text_project, text_language, text_task_id, text_job_id,
       text_entity_id, text_fk_source_id, text_type, text_description, text_keywords,
       text_relation_to_source, date_commissioned, date_evaluated)
    VALUES (now(), %(entity_name)s, %(project)s, 'English', %(task)s, %(task)s,
            %(entity)s, %(id)s, 'Company', 'srcname', 'srcname', 'Direct', now(), now())
"""

_INSERT_LOCATION = """
    INSERT INTO processed_data.locations
      (date_added, text_name, text_project, text_language, text_task_id, text_job_id,
       text_fk_entity_id, text_fk_source_id, text_type, text_description, text_address,
       text_relation_to_source, float_latitude, float_longitude,
       date_commissioned, date_evaluated)
    VALUES (now(), 'Office', %(project)s, 'English', %(task)s, %(task)s,
            %(entity)s, %(id)s, 'Office', '', %(address)s, 'Direct', %(lat)s, %(lng)s,
            now(), now())
"""

_INSERT = """
    INSERT INTO processed_data.sources
      (date_added, text_name, text_project, text_language, text_task_id, text_job_id,
       text_source_id, text_uri, text_content_hash, text_type, text_importance,
       text_summary, text_reason, text_keywords, bool_trustful,
       date_written, date_commissioned, date_evaluated)
    VALUES (now(), %(name)s, %(project)s, 'English', %(task)s, %(task)s,
            %(id)s, %(uri)s, %(id)s, 'Article', 'High',
            'A summary that says something.', 'srcname', 'srcname', true,
            now(), now(), now())
"""


@pytest.fixture(scope="module", autouse=True)
def rows(archive) -> Iterator[None]:
    """Three documents in a project of this file's own: one named by serial,
    one by checksum, one by a person. Removed again whatever happens."""
    _remove(archive)
    with archive.connect(autocommit=True) as conn:
        conn.execute(_TASK, {"task": TASK, "project": PROJECT, "uri": URI})
        for source_id, name in ((SERIAL_ID, SERIAL_NAME), (CHECKSUM_ID, CHECKSUM_NAME),
                                (NAMED_ID, REAL_NAME)):
            fields = {"name": name, "project": PROJECT, "task": TASK, "id": source_id,
                      "uri": URI, "entity": f"ent-{source_id}",
                      # Its own name: an ENTITY is allowed a real one, and
                      # reusing the document's would have put the id back
                      # into the feed through a row this rule is not about.
                      "entity_name": f"Srcname Holdings {source_id[-6:]}",
                      "address": ADDRESS, "lat": LAT, "lng": LNG}
            conn.execute(_INSERT, fields)
            conn.execute(_INSERT_ENTITY, fields)
            conn.execute(_INSERT_LOCATION, fields)
    archive.refresh_places()
    yield
    _remove(archive)
    archive.refresh_places()


def _remove(archive) -> None:
    with archive.connect(autocommit=True) as conn:
        for table in ("locations", "entities", "sources", "tasks"):
            conn.execute(f"DELETE FROM processed_data.{table} WHERE text_task_id = %s", (TASK,))


def params(**extra) -> dict:
    return {"project": PROJECT, "language": "English", **extra}


def texts(value) -> str:
    """Every string anywhere in a response, as one blob to search."""
    if isinstance(value, dict):
        return " ".join(texts(v) for v in value.values())
    if isinstance(value, list):
        return " ".join(texts(v) for v in value)
    return str(value)


def test_the_activity_feed_names_a_document_by_its_address(client):
    body = client.get("/api/dashboard/activities", params=params(limit=50)).json()
    blob = texts(body)
    assert SERIAL_NAME not in blob, "the feed is still printing the serial id"
    assert CHECKSUM_NAME not in blob, "the feed is still printing the checksum"

    ours = [i for i in body["items"] if i["table"] == "sources" and DOMAIN in i["name"]]
    assert len(ours) >= 2, f"the two id-named documents are not in the feed: {body['items']}"
    for item in ours:
        assert item["name"] == f"{DOMAIN} - {DERIVED}"
        assert item["derived"] is True
        # THE LINK SEARCHES A TERM THAT RESOLVES. What the row is called is
        # two facts joined by a middle dot, and that whole string matches
        # nothing; /diagrams/source searches the host, so the host is what
        # goes in `q`.
        assert item["link"], "the document has no link"
        query = parse_qs(urlsplit(item["link"]).query)
        assert query["q"] == [DOMAIN], query


def test_the_link_on_a_renamed_document_still_finds_it(client):
    """The heading changed; the search behind it has to keep working. Asked
    of the archive rather than of the URL: the term is put through
    /diagrams/source and has to come back with the documents."""
    body = client.get("/api/dashboard/activities", params=params(limit=50)).json()
    ours = [i for i in body["items"] if i["table"] == "sources" and DOMAIN in i["name"]]
    assert ours
    term = parse_qs(urlsplit(ours[0]["link"]).query)["q"][0]
    r = client.get("/api/diagrams/source/sources", params=params(q=term, timeframe="5y"))
    assert r.status_code == 200, r.text
    resolved = r.json()["resolved"]
    assert resolved.get("sources"), f"{term!r} resolves to no document: {resolved}"


def test_a_document_that_has_a_real_name_keeps_it(client):
    body = client.get("/api/dashboard/activities", params=params(limit=50)).json()
    named = [i for i in body["items"] if i["name"] == REAL_NAME]
    assert named, "a real name was replaced by its address"
    assert named[0]["derived"] is False


def test_the_query_card_is_headed_by_the_address_not_the_id(client):
    r = client.post("/api/query/search", params=params(),
                    json={"terms": [], "match_all": False,
                          "place": {"address": ADDRESS}})
    assert r.status_code == 200, r.text
    body = r.json()
    blob = texts(body.get("groups", []))
    assert SERIAL_NAME not in blob
    assert CHECKSUM_NAME not in blob
    titles = {item["title"] for g in body.get("groups", []) for item in g.get("items", [])}
    assert f"{DOMAIN} - {DERIVED}" in titles, titles
    assert REAL_NAME in titles, "a real name was replaced in the Query card too"


def test_no_endpoint_that_lists_documents_leaks_the_identifier(client):
    """The three lists a reader meets, asked one after the other. A view
    added later that forgets the rule fails here rather than shipping."""
    answers = {
        "/api/dashboard/activities": client.get("/api/dashboard/activities",
                                                params=params(limit=50)).json(),
        "/api/dashboard/latest-events": client.get("/api/dashboard/latest-events",
                                                   params=params()).json(),
        "/api/events": client.get("/api/events", params=params()).json(),
    }
    for where, body in answers.items():
        blob = texts(body)
        assert SERIAL_NAME not in blob, f"{where} answers with a serial id"
        assert CHECKSUM_NAME not in blob, f"{where} answers with a checksum"
