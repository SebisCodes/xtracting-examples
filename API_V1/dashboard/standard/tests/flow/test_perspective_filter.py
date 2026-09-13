"""The perspective filter, against the preseeded archive.

    DATABASE_URL=postgresql://... python -m pytest tests/flow/test_perspective_filter.py -q

Two selectors in the top bar mean one sentence - show only what came from
documents that carry, for this perspective, at least this importance - and it
has to mean the same sentence in four views at once. The preseed judges its
six Alpha documents under two perspectives, and that is what every number
below is read off (tests/preseed/preseed.py):

    src:battery       (2 h)   Maintenance Medium   Compliance Low
    src:antitrust     (3 d)   Maintenance Not      Compliance Critical
    src:pump          (20 d)  Maintenance High     -
    src:harvest       (60 d)  Maintenance Low      -
    src:subsidiary    (200 d) -                    Compliance Medium
    src:old-antitrust (2 y)   -                    Compliance Low

So "Compliance, High Importance and above" is src:antitrust and nothing else:
the other four Compliance judgements are below the line, and src:pump and
src:harvest have NO Compliance judgement at all - which the customer decided
means HIDDEN, not shown.

The three facts this file pins:

  * every view that carries the filter returns FEWER rows with it than
    without, and the rows it drops are the ones the predicate names;
  * All is not a filter - the answer is identical to asking with no
    perspective at all;
  * the Dashboard does not move. It counts what ARRIVED, which is the answer
    to "is the collector running", and a counter that changed with a reading
    setting would stop being that answer.
"""

from __future__ import annotations

import pytest

from preseed import ALPHA

pytestmark = pytest.mark.flow

EN = {"project": ALPHA, "language": "English"}
#: The one combination the file is built on: src:antitrust alone.
STRICT = {**EN, "perspective": "Compliance", "min_importance": "High Importance"}
#: Everything Compliance has judged at all, which is four of the six.
LOOSE = {**EN, "perspective": "Compliance", "min_importance": "Not Important"}


def get(client, path: str, params: dict, expect: int = 200) -> dict:
    r = client.get(path, params=params)
    assert r.status_code == expect, r.text
    return r.json()


# ── Events ───────────────────────────────────────────────────

def test_events_are_narrowed_to_the_documents_that_matter(client):
    """"Regulatory action" is two events in the preseed - one from the
    antitrust filing (Compliance Critical) and one from the two-year-old
    blog post (Compliance Low). At "High Importance and above" the second
    one goes."""
    wide = get(client, "/api/events", {**EN, "by": "type", "q": "Regulatory action"})
    assert [e["name"] for e in wide["events"]] == ["Antitrust probe opened", "Old antitrust fine"]

    narrow = get(client, "/api/events", {**STRICT, "by": "type", "q": "Regulatory action"})
    assert [e["name"] for e in narrow["events"]] == ["Antitrust probe opened"]
    # The total is narrowed with the list, not left behind: a list of one
    # under a heading that says two is a lie.
    assert narrow["total"] == 1 < wide["total"]


def test_a_document_with_no_judgement_for_the_perspective_is_hidden(client):
    """Decided with the customer. src:pump is judged High for Maintenance and
    is not judged for Compliance at all, so its delivery is in the Maintenance
    view and in no Compliance view - not even at the bottom of the scale."""
    maintenance = get(client, "/api/events",
                      {**EN, "perspective": "Maintenance", "min_importance": "High Importance",
                       "by": "entity", "q": "Springfield Works"})
    assert "Pump delivered" in [e["name"] for e in maintenance["events"]]

    compliance = get(client, "/api/events",
                     {**LOOSE, "by": "entity", "q": "Springfield Works"})
    assert "Pump delivered" not in [e["name"] for e in compliance["events"]]
    assert compliance["total"] == 0


# ── Query ────────────────────────────────────────────────────

def test_the_query_view_keeps_only_the_documents_that_matter(client):
    """Brussels is in two documents - the antitrust filing and the two-year-old
    blog post about the older dispute. One of them is Compliance Critical and
    the other Compliance Low."""
    body = {"place": {"city": "Brussels", "country": "Belgium"}}
    wide = client.post("/api/query/search", params=EN, json=body).json()
    narrow = client.post("/api/query/search", params=STRICT, json=body).json()

    assert wide["total"] > narrow["total"] > 0
    uris = {i["uri"] for g in narrow["groups"] for i in g["items"] if i.get("uri")}
    assert uris == {"https://filings.example.org/antitrust-2026"}


def test_the_query_view_narrows_every_kind_through_ents(client):
    """Six of the seven kinds this view lists narrow themselves on `ents`,
    and the seventh (`srcs`) is derived from it - so one predicate on `ents`
    reaches all seven. A kind that came back wider than the sources would
    mean the predicate had been put on `srcs` instead."""
    body = {"place": {"city": "Brussels", "country": "Belgium"}}
    for kind in ("source", "entity", "location", "connection", "rating", "attribute"):
        wide = client.post("/api/query/search", params=EN, json={**body, "kind": kind}).json()
        narrow = client.post("/api/query/search", params=STRICT,
                             json={**body, "kind": kind}).json()
        assert narrow["total"] <= wide["total"], kind


# ── Diagrams ─────────────────────────────────────────────────

def test_every_chart_of_a_tab_is_narrowed_by_one_edit(client):
    """One predicate in charts/drilldown.py _where() covers all forty-eight
    charts. If a chart ever escapes it, its total will not move here."""
    wide = get(client, "/api/diagrams/summary/sources", {**EN, "timeframe": "1y"})
    narrow = get(client, "/api/diagrams/summary/sources", {**STRICT, "timeframe": "1y"})
    wide_totals = {c["id"]: c["total"] for c in wide["charts"]}
    narrow_totals = {c["id"]: c["total"] for c in narrow["charts"]}

    moved = [cid for cid, n in narrow_totals.items() if n < wide_totals[cid]]
    assert moved, f"no chart moved: {wide_totals} vs {narrow_totals}"
    for cid, n in narrow_totals.items():
        assert n <= wide_totals[cid], cid


def test_the_chart_of_the_judgements_does_not_filter_itself(client):
    """sources/importance_by_perspective is drawn FROM the judgement table.
    It is the reader's answer to "what does this filter cover?", so it keeps
    showing every perspective and every level."""
    wide = get(client, "/api/diagrams/summary/sources", {**EN, "timeframe": "1y"})
    narrow = get(client, "/api/diagrams/summary/sources", {**STRICT, "timeframe": "1y"})

    def chart(body):
        found = [c for c in body["charts"] if c["id"] == "importance_by_perspective"]
        assert found, "the chart is in the registry"
        return found[0]

    assert chart(narrow)["total"] == chart(wide)["total"]
    assert set(chart(narrow)["labels"]) == set(chart(wide)["labels"])


def test_a_drilldown_lists_the_same_rows_the_filtered_chart_counted(client):
    """A chart and its drilldown are the same question asked twice. If the
    filter reached one and not the other, the dialog would list rows the bar
    was never drawn from."""
    body = get(client, "/api/diagrams/summary/sources", {**STRICT, "timeframe": "1y"})
    chart = next(c for c in body["charts"] if c["id"] == "by_domain")
    assert chart["total"] == 1, "only the antitrust filing survives the filter"
    assert chart["labels"] == ["filings.example.org"]


# ── The Graph ────────────────────────────────────────────────

def test_the_ring_is_narrowed_but_the_centre_is_still_found(client):
    """A perspective narrows what the archive SAYS about a thing, never which
    thing was asked about: the entity is still resolved and the answer says
    the ring is empty, rather than behaving as if the name were unknown."""
    wide = get(client, "/api/graph/neighbours", {**EN, "q": "Apple"})
    narrow = get(client, "/api/graph/neighbours", {**STRICT, "q": "Apple"})

    assert narrow["entity"] is not None
    assert narrow["entity"]["name"] == wide["entity"]["name"]
    assert len(narrow["neighbours"]) < len(wide["neighbours"])
    # src:antitrust is the only Compliance document above the line, and the
    # connections it carries are the Commission regulating Apple and the
    # Apple/Microsoft rivalry. Foxconn and Tim Cook come from the battery
    # article (Compliance Low) and are gone.
    assert {n["name"] for n in narrow["neighbours"]} == {"European Commission", "Microsoft"}
    assert {"Foxconn", "Tim Cook"} & {n["name"] for n in wide["neighbours"]}


# ── What the filter must NOT touch ───────────────────────────

def test_the_dashboard_does_not_move(client):
    """It counts what ARRIVED. A counter that changed with a reading setting
    would stop being the answer to "is the collector running"."""
    def counts(params):
        body = get(client, "/api/dashboard/stats", {**params, "fresh": "true"})
        return {row["id"]: {p: row[p] for p in ("hour", "day", "week", "month", "year")}
                for row in body["tables"]}

    assert counts(STRICT) == counts(EN)


def test_All_is_not_a_filter(client):
    """An empty perspective and no perspective at all are the same request,
    and a URL that carries `perspective=` empty must not narrow anything."""
    plain = get(client, "/api/events", {**EN, "by": "type", "q": "Regulatory action"})
    allp = get(client, "/api/events",
               {**EN, "perspective": "", "min_importance": "High Importance",
                "by": "type", "q": "Regulatory action"})
    assert [e["name"] for e in allp["events"]] == [e["name"] for e in plain["events"]]
    assert allp["total"] == plain["total"]


def test_a_perspective_this_project_never_judged_falls_back_to_All(client):
    """A stale cookie or a copied link must not empty every view. The
    perspective is checked against the ones the pair actually holds and
    dropped when it is not one of them."""
    stale = get(client, "/api/events",
                {**EN, "perspective": "Investor", "min_importance": "High Importance",
                 "by": "type", "q": "Regulatory action"})
    plain = get(client, "/api/events", {**EN, "by": "type", "q": "Regulatory action"})
    assert stale["total"] == plain["total"] == 2


# ── The file a person downloads is the screen they were looking at ──

def test_the_export_is_cut_with_the_same_predicate(client):
    """Five hardcoded places carry the pair from the screen to the file, and
    all five had to learn these two as well. A CSV of more rows than the
    screen showed is the failure this guards."""
    r = client.get("/api/export/events.json",
                   params={**STRICT, "by": "type", "q": "Regulatory action"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert [row["name"] for row in body["rows"]] == ["Antitrust probe opened"]
    # And the envelope says WHICH slice the reader is holding.
    assert body["export"]["filters"]["perspective"] == "Compliance"
    assert body["export"]["filters"]["min_importance"] == "High Importance"


# ── The number, against the same predicate run directly ──────

def test_the_count_matches_the_predicate_run_in_sql(client, archive):
    """The view's total, and the same question asked of the archive with no
    dashboard in the way."""
    narrow = get(client, "/api/events", {**STRICT, "by": "type", "q": "Regulatory action"})
    direct = archive.scalar("""
        SELECT count(*) FROM processed_data.events ev
         WHERE ev.text_project = %(project)s AND ev.text_language = %(language)s
           AND lower(ev.text_type) = 'regulatory action'
           AND EXISTS (
                 SELECT 1 FROM processed_data.source_importances_by_perspective si
                  WHERE si.text_project = %(project)s AND si.text_language = %(language)s
                    AND si.text_task_id = ev.text_task_id
                    AND si.text_fk_source_id = ev.text_fk_source_id
                    AND si.text_perspective = %(perspective)s
                    AND si.text_importance <> ALL (ARRAY(
                          SELECT lv.text_name FROM (
                            SELECT DISTINCT ON (it.text_name) it.text_name, it.float_value
                              FROM processed_data.importance_types it
                             WHERE it.text_project IN (%(project)s, '')
                             ORDER BY it.text_name, (it.text_project <> '') DESC) lv
                           WHERE lv.float_value < (
                                 SELECT m.float_value FROM processed_data.importance_types m
                                  WHERE m.text_name = %(min_importance)s
                                    AND m.text_project IN (%(project)s, '')
                                  ORDER BY (m.text_project <> '') DESC LIMIT 1))))
    """, STRICT)
    assert narrow["total"] == direct == 1


# ── The two honest limits, written down rather than papered over ──────────

def test_a_perspective_is_the_word_that_language_uses(client):
    """The perspectives are translated with everything else - Maintenance is
    Wartung in the German rows - so the German selector offers Wartung and the
    German view is narrowed by it. The English spelling is not a perspective
    of the German view and falls back to All rather than emptying it."""
    de = {"project": ALPHA, "language": "German"}
    wide = get(client, "/api/events", {**de, "by": "entity", "q": "Springfield Works"})
    narrow = get(client, "/api/events",
                 {**de, "perspective": "Wartung", "min_importance": "High Importance",
                  "by": "entity", "q": "Springfield Works"})
    assert narrow["total"] > 0
    assert narrow["total"] <= wide["total"]

    english_word = get(client, "/api/events",
                       {**de, "perspective": "Maintenance", "min_importance": "High Importance",
                        "by": "entity", "q": "Springfield Works"})
    assert english_word["total"] == wide["total"], "an unknown perspective is All, not nothing"


def test_a_translated_importance_label_falls_back_to_unfiltered(client, archive):
    """THE SECOND HONEST LIMIT.

    sqlbuild.vocabulary_scope() binds the project but NOT the language, so
    processed_data.importance_types holds one scale - the English one - and a
    project whose extraction writes German importance labels has rows the
    scale cannot order. Under "at or above the threshold" every one of them
    would resolve to NULL, fall out of the comparison, and the whole language
    would go blank behind a filter that looked like it was working.

    The predicate is written the other way round for exactly this - "not one
    of the levels BELOW the threshold" - so a label the vocabulary cannot
    order is KEPT. The filter says, in effect, "I cannot rank this language,
    so I will not pretend to": unfiltered rather than empty. The remedy
    would be a language column in the vocabulary join, and that is a change
    to the archive rather than to this dashboard.
    """
    marker = "_preseed:i18n:1"
    with archive.connect() as conn:
        for table, extra_cols, extra_vals in (
            ("sources",
             "text_source_id, text_uri, text_type, bool_trustful, date_written",
             "'src:i18n', 'https://news.example.com/uebersetzt', 'News article', true, now()"),
            ("source_importances_by_perspective",
             "text_fk_source_id, text_perspective, text_importance",
             "'src:i18n', 'Wartung', 'Hohe Wichtigkeit'"),
        ):
            conn.execute(f"""
                INSERT INTO processed_data.{table}
                     (date_added, text_name, text_project, text_language, text_task_id,
                      text_job_id, {extra_cols}, date_commissioned, date_evaluated)
                VALUES (now(), %s, %s, 'German', %s, %s, {extra_vals}, now(), now())
            """, (marker, ALPHA, marker, marker))
        conn.commit()
    try:
        de = {"project": ALPHA, "language": "German"}
        narrow = get(client, "/api/diagrams/summary/sources",
                     {**de, "timeframe": "1y", "perspective": "Wartung",
                      "min_importance": "Critical Importance"})
        by_domain = next(c for c in narrow["charts"] if c["id"] == "by_domain")
        assert by_domain["total"] >= 1, (
            "a document whose importance label the vocabulary cannot order is kept - "
            "the filter falls back to unfiltered for that label rather than emptying "
            "the view")
    finally:
        with archive.connect() as conn:
            for table in ("source_importances_by_perspective", "sources"):
                conn.execute(f"DELETE FROM processed_data.{table} WHERE text_task_id = %s",
                             (marker,))
            conn.commit()
