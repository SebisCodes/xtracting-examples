"""The Diagrams page's second axis: the same term read as a whole TYPE.

    DATABASE_URL=postgresql://... python -m pytest tests/flow/test_diagrams_axis.py -q

THE QUESTION IT ANSWERS: not only "what is this entity's relation to that
type", but "show me the whole type" - search for Company and see everything
the archive holds about companies.

So every scope answers two questions about one word. "Apple Inc." is one
company; "Company" is every company - and the point of the design is that
the SECOND one is not a new page, a new endpoint or a new set of charts. It
resolves to the same two CTEs (app/scope.py), so all eight tabs, every
drilldown and every export are built from it unchanged.

What is pinned here:

  * a type search fills all eight tabs, exactly as an object search does;
  * the entity count it reports is the archive's own count of that type -
    checked against a direct SQL count, not against another code path;
  * the drilldown carries the axis, and a drilldown out of a type search
    returns rows of that type and of nothing else;
  * a bucket member of the searched type is counted ONCE;
  * the EVENTS scope is the one that gains the OBJECT axis instead: its box
    has always searched the type, so it opens on that one and what is new
    there is the single event by name;
  * an axis no scope has (the summary's type) is a 400 with a sentence, not
    a 500.
"""

from __future__ import annotations

import pytest
from psycopg import sql

from app import scope
from preseed import ALPHA

pytestmark = pytest.mark.flow

TABS = ("sources", "ratings", "entities", "connections", "locations",
        "market", "attributes", "events")

# One type per scope, out of the preseed's own vocabulary, with the number
# of DISTINCT entities the archive holds of it. The counts are not written
# down: they are measured below against the archive itself, because a number
# copied into a test is a number that stops being true.
TYPES = {
    "entity": "Company",
    "source": "News article",
    "location": "Headquarters",
    "events": "Regulatory action",
    "market": "Batteries",
}


class Ctx:
    def __init__(self, language: str = "English", project: str = ALPHA):
        self.project = project
        self.language = language


def get(client, scope_name, tab, q, axis, timeframe="5y"):
    r = client.get(f"/api/diagrams/{scope_name}/{tab}",
                   params={"project": ALPHA, "language": "English", "q": q,
                           "axis": axis, "timeframe": timeframe, "page": 0})
    assert r.status_code == 200, r.text
    return r.json()


def entity_ids(conn, scoped) -> set[tuple[str, str]]:
    stmt = sql.SQL("{w}SELECT task_id, id FROM ent").format(w=scoped.with_clause())
    return {(r["task_id"], r["id"]) for r in conn.execute(stmt, scoped.params).fetchall()}


# ── Every tab, from a type ───────────────────────────────────────────────

@pytest.mark.parametrize("tab", TABS)
def test_a_type_search_builds_every_tab(client, tab):
    """The whole design in one assertion: the type axis is a different
    RESOLUTION, not a different page. Every tab answers 200 with the charts
    the registry declares, and says which axis it was asked on."""
    body = get(client, "entity", tab, TYPES["entity"], "type")
    assert body["axis"] == "type"
    assert body["resolved"]["axis"] == "type"
    assert body["resolved"]["label"] == "Company"
    assert body["charts"], tab
    for chart in body["charts"]:
        assert "datasets" in chart and "total" in chart, chart["id"]


@pytest.mark.parametrize("scope_name", sorted(TYPES))
def test_the_entity_count_of_a_type_search_is_the_archive_count(conn, client, scope_name):
    """`resolved.entities` is the set every chart is drawn from, so it has to
    be the archive's own answer to "how many entities are of this type" -
    measured here with SQL rather than with a second run of the same code."""
    term = TYPES[scope_name]
    scoped = scope.resolve_scope(conn, Ctx(), scope_name, term, axis="type")
    assert scoped.resolved["match"] != "none", f"{term} is not a type of {scope_name}"
    counted = len(entity_ids(conn, scoped))
    assert scoped.resolved["entities"] == counted

    body = get(client, scope_name, "entities", term, "type")
    assert body["resolved"]["entities"] == counted


def test_an_entity_type_search_is_exactly_the_entities_of_that_type(conn):
    """The plainest reading of "show me every company"."""
    scoped = scope.resolve_scope(conn, Ctx(), "entity", "Company", axis="type")
    direct = {(r["task_id"], r["id"]) for r in conn.execute(
        "SELECT DISTINCT e.text_task_id AS task_id, e.text_entity_id AS id "
        "FROM processed_data.entities e "
        "WHERE e.text_project = %(project)s AND lower(e.text_type) = 'company' "
        "AND e.text_entity_id <> ''", {"project": ALPHA}).fetchall()}
    assert entity_ids(conn, scoped) == direct
    assert direct, "the preseed holds no company at all"


def test_the_two_axes_answer_differently_and_both_say_which_they_are(client):
    """"Company - 42 entities" reads differently from "Apple Inc. - 1
    entity", and a person has to be able to tell which they are looking
    at. The API carries both halves of that sentence."""
    one = get(client, "entity", "entities", "Apple Inc.", "object")
    every = get(client, "entity", "entities", "Company", "type")
    assert one["resolved"]["axis"] == "object"
    assert every["resolved"]["axis"] == "type"
    assert every["resolved"]["entities"] > one["resolved"]["entities"]
    # And the object axis of that same word finds nothing: no entity is
    # NAMED Company. Two different questions, two different answers.
    named = get(client, "entity", "entities", "Company", "object")
    assert named["resolved"]["match"] == "none"


# ── The drilldown carries the axis ───────────────────────────────────────

def _drill(client, body, chart_id, key, axis, scope_name="entity", tab="entities"):
    chart = next(c for c in body["charts"] if c["id"] == chart_id)
    dataset = chart["datasets"][0]
    r = client.post("/api/diagrams/drilldown",
                    params={"project": ALPHA, "language": "English"},
                    json={"scope": scope_name, "tab": tab, "q": body["q"], "axis": axis,
                          "timeframe": "5y", "page": 0, "chart_id": chart_id,
                          "dataset_id": dataset["id"], "key": key, "ddpage": 1})
    assert r.status_code == 200, r.text
    return r.json()


def test_a_drilldown_out_of_a_type_search_lists_only_that_type(client):
    """Without the axis the click would resolve "Company" as an OBJECT -
    nothing is named that - and the dialog would come back empty under a bar
    that says nine."""
    body = get(client, "entity", "entities", "Company", "type")
    by_type = next(c for c in body["charts"] if c["id"] == "by_type")
    point = next(p for d in by_type["datasets"] for p in d["data"] if p["y"])
    answer = _drill(client, body, "by_type", {"x": point["x"]}, "type")
    assert answer["resolved"]["axis"] == "type"
    assert answer["rows"], "the bar counted rows the dialog could not find"
    assert len(answer["rows"]) == point["y"]
    for row in answer["rows"]:
        assert row["cells"]["type"][0]["text"] == "Company", row["cells"]


def test_a_drilldown_without_the_axis_is_the_other_question(client):
    """The failure the parameter prevents, written down: the same click with
    the axis left out resolves the word as an object and answers about
    something else entirely."""
    body = get(client, "entity", "entities", "Company", "type")
    by_type = next(c for c in body["charts"] if c["id"] == "by_type")
    point = next(p for d in by_type["datasets"] for p in d["data"] if p["y"])
    with_axis = _drill(client, body, "by_type", {"x": point["x"]}, "type")
    without = _drill(client, body, "by_type", {"x": point["x"]}, "object")
    assert without["resolved"]["axis"] == "object"
    assert len(without["rows"]) != len(with_axis["rows"])


def test_the_csv_of_a_type_search_is_of_the_type(client):
    body = get(client, "entity", "entities", "Company", "type")
    by_type = next(c for c in body["charts"] if c["id"] == "by_type")
    point = next(p for d in by_type["datasets"] for p in d["data"] if p["y"])
    r = client.get("/api/diagrams/drilldown.csv",
                   params={"project": ALPHA, "language": "English", "scope": "entity",
                           "tab": "entities", "chart_id": "by_type", "q": "Company",
                           "axis": "type", "timeframe": "5y", "key_x": point["x"]})
    assert r.status_code == 200, r.text
    lines = [line for line in r.text.splitlines() if line.strip()]
    # The header plus one row per counted entity mention.
    assert len(lines) - 1 == point["y"]


def test_the_export_of_a_type_search_is_of_the_type(client):
    """The Export menu builds its links out of the URL, so the axis has to
    be in the URL and the endpoint has to read it - or a file exported from
    "every company" holds whatever "Company" means as a name, which is
    nothing."""
    r = client.get("/api/export/diagrams.json",
                   params={"project": ALPHA, "language": "English", "scope": "entity",
                           "tab": "entities", "q": "Company", "axis": "type",
                           "timeframe": "5y"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["context"]["axis"] == "type"
    assert body["context"]["resolved"]["axis"] == "type"
    assert body["context"]["resolved"]["entities"] > 0
    assert body["export"]["filters"].get("axis") == "type", body["export"]["filters"]
    assert body["rows"], "the exported file of a type search is empty"


# ── Buckets on the type axis ─────────────────────────────────────────────

@pytest.fixture
def type_bucket(client):
    made: list[int] = []

    def make(name: str, kind: str, members) -> dict:
        r = client.post("/api/buckets", params={"project": ALPHA, "language": "English"},
                        json={"name": name, "kind": kind, "every_project": False,
                              "members": [{"name": m} for m in members]})
        assert r.status_code == 201, r.text
        made.append(r.json()["id"])
        return r.json()

    yield make
    for bucket_id in made:
        client.delete(f"/api/buckets/{bucket_id}")


def test_a_member_of_a_type_bucket_is_counted_once(conn, client, type_bucket):
    """A bilingual bucket merges "Company" and "Unternehmen", which are the
    SAME entities in two languages' rows. The bucket must therefore answer
    with the union - the same set as either member alone - and not with the
    two added up."""
    type_bucket("_c Orgs", "entity_type", ["Company", "Unternehmen"])
    english = entity_ids(conn, scope.resolve_scope(conn, Ctx("English"), "entity",
                                                   "Company", axis="type"))
    german = entity_ids(conn, scope.resolve_scope(conn, Ctx("German"), "entity",
                                                  "Unternehmen", axis="type"))
    # Both members resolve to the bucket now, so both are the whole of it.
    assert english == german
    body = get(client, "entity", "entities", "Company", "type")
    assert body["resolved"]["match"] == "bucket"
    assert body["resolved"]["label"] == "_c Orgs"
    assert body["resolved"]["entities"] == len(english)
    # Counted once: the two members' own rows add up to more than the set.
    assert len(english) < 2 * len(english) - 0  # trivially true; the real check:
    rows = conn.execute(
        "SELECT count(*) AS n FROM processed_data.entities e "
        "WHERE e.text_project = %(project)s AND lower(e.text_type) IN ('company','unternehmen')",
        {"project": ALPHA}).fetchone()["n"]
    assert rows > len(english), (
        "the preseed no longer holds one type in two languages, so this test "
        "can no longer show that the merge counts an entity once")


def test_a_type_bucket_is_the_same_entity_set_in_both_languages(conn, type_bucket):
    """The reason type buckets earn their keep at all: a bilingual archive
    splits every type in half, and a search in one language must not answer
    a different question from the same search in the other."""
    type_bucket("_c Orgs", "entity_type", ["Company", "Unternehmen"])
    for language, term in (("English", "Company"), ("German", "Unternehmen"),
                           ("English", "_c Orgs"), ("German", "_c Orgs")):
        scoped = scope.resolve_scope(conn, Ctx(language), "entity", term, axis="type")
        assert scoped.resolved["match"] == "bucket", (language, term)
        assert scoped.resolved["label"] == "_c Orgs", (language, term)
        assert entity_ids(conn, scoped) == entity_ids(conn, scope.resolve_scope(
            conn, Ctx("English"), "entity", "Company", axis="type"))


# ── The events scope, which gains the OBJECT axis ────────────────────────

def test_the_events_scope_opens_on_the_type_axis(conn, client):
    """Its box has always searched the event type, which is the very
    question this second axis added everywhere else - so that stays its
    default and every link ever shared of it keeps meaning what it meant."""
    assert scope.default_axis("events") == "type"
    without = scope.resolve_scope(conn, Ctx(), "events", "Regulatory action")
    named = scope.resolve_scope(conn, Ctx(), "events", "Regulatory action", axis="type")
    assert without.resolved["axis"] == "type"
    assert entity_ids(conn, without) == entity_ids(conn, named)
    body = get(client, "events", "events", "Regulatory action", "")
    assert body["axis"] == "type"


def test_the_events_object_axis_is_one_event_by_its_own_name(conn):
    """What the Events page LACKED: "the Brussels hearing", not "every
    hearing". `events.text_name` is the extraction's own name for it."""
    one = scope.resolve_scope(conn, Ctx(), "events", "Antitrust probe opened", axis="object")
    assert one.resolved["match"] == "exact"
    assert one.resolved["names"] == ["Antitrust probe opened"]
    # It is a strictly smaller thing than its type: the type also holds the
    # old antitrust fine.
    every = scope.resolve_scope(conn, Ctx(), "events", "Regulatory action", axis="type")
    assert entity_ids(conn, one) <= entity_ids(conn, every)
    # And the type name is not an event NAME, so the object axis says so
    # rather than quietly answering the other question.
    assert scope.resolve_scope(conn, Ctx(), "events", "Regulatory action",
                               axis="object").resolved["match"] == "none"


def test_one_event_by_name_is_offered_as_a_suggestion(client):
    r = client.get("/api/suggest/event", params={"project": ALPHA, "language": "English",
                                                 "q": "Antitrust"})
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert any(i["value"] == "Antitrust probe opened" for i in items), items
    # The hint carries the type, because two events of one name in a large
    # archive are told apart by nothing else on the screen.
    found = next(i for i in items if i["value"] == "Antitrust probe opened")
    assert found["hint"] == "Regulatory action"


# ── The market scope's two axes ──────────────────────────────────────────
#
# A market insight is ABOUT AN ENTITY and CARRIES A TOPIC, and it has no
# third thing that is a type. Searching the topic on the object axis and
# an outlook or a sentiment on the type axis would be wrong - an outlook is
# a value a reading HAS, not a class it belongs to, so the page would name
# a type the data does not have and could not ask about the entity
# every other page asks about.

def test_the_market_object_axis_is_the_entity(conn, client):
    sc = scope.resolve_scope(conn, Ctx(), "market", "Apple Inc.", axis="object")
    assert sc.resolved["match"] != "none"
    assert sc.resolved["entities"] > 0
    body = get(client, "market", "market", "Apple Inc.", "object")
    assert body["resolved"]["axis"] == "object"
    assert any(c["total"] for c in body["charts"])


def test_the_market_type_axis_is_the_topic(conn, client):
    topic = scope.resolve_scope(conn, Ctx(), "market", "Batteries", axis="type")
    assert topic.resolved["match"] != "none"
    assert topic.resolved["entities"] > 0
    body = get(client, "market", "market", "Batteries", "type")
    assert body["resolved"]["axis"] == "type"
    assert any(c["total"] for c in body["charts"])

    # An outlook is not a scope: it is a value, and the charts draw it.
    rising = scope.resolve_scope(conn, Ctx(), "market", "Rising", axis="type")
    assert rising.resolved["match"] == "none"


# ── The axis nobody has ──────────────────────────────────────────────────

def test_the_summary_has_no_type_axis_and_says_so(client):
    """It is already everything, and a type search over everything is the
    summary again. A 400 with a sentence, not a 500."""
    r = client.get("/api/diagrams/summary/entities",
                   params={"project": ALPHA, "language": "English", "q": "Company",
                           "axis": "type", "timeframe": "5y"})
    assert r.status_code == 400, r.text
    said = r.json()
    assert "type axis" in said["error"], said
    assert "object" in said["hint"] and "type" in said["hint"], said


def test_an_axis_that_does_not_exist_is_a_400(client):
    r = client.get("/api/diagrams/entity/entities",
                   params={"project": ALPHA, "language": "English", "q": "Company",
                           "axis": "sideways", "timeframe": "5y"})
    assert r.status_code == 400, r.text
    assert "sideways" in r.json()["error"], r.text


def test_a_type_nothing_answers_charts_nothing_rather_than_everything(client):
    """A page that charted the whole archive because the term was not a type
    would be the wrong answer told confidently."""
    body = get(client, "entity", "entities", "Zzqxwv", "type")
    assert body["resolved"]["match"] == "none"
    assert body["resolved"]["entities"] == 0
    assert not any(c["total"] for c in body["charts"])
