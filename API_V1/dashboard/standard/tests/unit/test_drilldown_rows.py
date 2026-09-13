"""The shape of one drilldown row: the columns, the words, and the colour.

    python -m pytest tests/unit/test_drilldown_rows.py -q

The drilldown is a table, not a paragraph per row, with this shape:

    running number | the source | the entity | the subject columns |
    the date | the free text, last and widest

Everything here is pure: `shape_row` takes a database row as a dict and
returns what the dialog draws, so the columns of every tab, the colour of
every value and the words that carry them can be pinned without a database
and without a browser.

TWO RULES ARE CHECKED EVERYWHERE:

  * NO ID IS SHOWN. `src:battery`, `ent:1822949` and a content hash name a
    row to a machine and say nothing to a reader.
  * THE WORD IS ALWAYS IN THE CELL. Colour travels beside it as a scale and
    a step (app/static/js/palette.js turns the pair into the hex from
    app.css), never instead of it - so the table reads on a printer, to a
    dichromat, and to anyone reading the words rather than the picture.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.charts import drilldown as dd

NOW = datetime(2026, 5, 29, 9, 30, tzinfo=timezone.utc)

# What each tab's row carries, in the order the columns are read. Every
# column is a fact the row's own facts already show.
EXPECTED: dict[str, tuple[str, ...]] = {
    "sources": ("importance", "trust", "type", "date", "text"),
    "entities": ("entity", "type", "relation", "date", "text"),
    "locations": ("name", "type", "relation", "address", "coordinates", "date", "text"),
    "events": ("type", "eventdate", "entities", "relation", "date", "text"),
    "ratings": ("value", "rating", "perspective", "relation", "date", "text"),
    # THE TWO NAMES SIT EITHER SIDE OF THE WORD THAT TIES THEM, and the
    # header is the sentence with the value's place left open - "Reference |
    # is a ... of | Target". "Connection: Ownership" between two type names
    # would ask the reader which end owns which. No entity-type columns: a
    # row is read as a sentence about two named things, and what kind of
    # thing each is, is one click away on its own page.
    "connections": ("reference", "connection", "target",
                    "reverse", "relation", "date", "text"),
    "attributes": ("attribute", "type", "number", "relation", "date", "text"),
    "market_insights": ("topic", "relevance", "outlook", "sentiment", "relation",
                        "date", "text"),
    "source_importances_by_perspective": ("perspective", "importance", "date", "text"),
}


@pytest.mark.parametrize("table,keys", sorted(EXPECTED.items()))
def test_every_tab_has_its_columns(table, keys):
    assert tuple(c.key for c in dd.columns_for(table)) == keys


@pytest.mark.parametrize("table", sorted(EXPECTED))
def test_the_free_text_is_last_and_widest_and_the_date_is_before_it(table):
    columns = dd.columns_for(table)
    assert columns[-1].wide, f"{table}: the free text is the last column"
    assert not any(c.wide for c in columns[:-1]), f"{table}: only one wide column"
    assert columns[-2].kind == "date", f"{table}: the date sits before the free text"
    # Its heading is the archive's own word for it, never "Text".
    assert columns[-1].label in ("Summary", "Reason", "Description"), columns[-1].label


@pytest.mark.parametrize("table", sorted(EXPECTED))
def test_no_column_shows_an_id(table):
    for column in dd.columns_for(table):
        assert "id" not in column.key.split("_"), column.key
        assert "hash" not in column.key and "hash" not in column.label.lower()


# ── The rows themselves ──────────────────────────────────────

def shaped(table, **row):
    row.setdefault("source_name", "src:battery")
    row.setdefault("source_uri", "https://www.news.example.com/apple-battery")
    row.setdefault("row_date", NOW)
    return dd.shape_row(table, row)


def words(item, key):
    return " / ".join(p["text"] for p in item["cells"][key] if p["text"])


def test_the_link_carries_the_domain_and_the_title_and_no_id():
    item = shaped("sources", name="src:battery",
                  uri="https://www.news.example.com/2026/apple-unveils-a-battery-a1b2",
                  type="News article", importance="High Importance", trustful=True,
                  summary="Apple unveils a battery.", importance_value=3.0)
    # www. is noise in a column that is scanned twenty rows at a time.
    assert item["link"]["domain"] == "news.example.com"
    # "src:battery" IS AN ID AND NOT A TITLE - it is what the extraction calls
    # the document when it found nothing better, and the rule is that a
    # reader is never shown one. The title comes off the URL's own slug
    # instead (app/source_names.py: IDENTIFIER_REGEX).
    assert item["link"]["title"] == "Apple battery"
    assert "src:battery" not in str(item)
    assert item["link"]["uri"].startswith("https://")


def test_every_row_has_exactly_the_cells_its_columns_ask_for():
    for table in EXPECTED:
        item = shaped(table)
        assert set(item["cells"]) == {c.key for c in dd.columns_for(table)}


def test_a_rating_takes_its_step_from_the_number_the_archive_carries():
    for value, number, step in (("Egregious", -3.0, -3), ("Neutral", 0.0, 0),
                                ("Excellent", 3.0, 3)):
        item = shaped("ratings", rating_name="Reputation", value=value,
                      perspective="Compliance", reason="because", relation="Direct",
                      rating_value=number)
        part = item["cells"]["value"][0]
        assert part["text"] == value, "the word stays in the cell"
        assert part["scale"] == "valence" and part["step"] == step


def test_a_rating_the_vocabulary_does_not_know_keeps_its_word_and_loses_its_colour():
    item = shaped("ratings", rating_name="Reputation", value="Sideways",
                  perspective="Compliance", reason="", relation="Direct", rating_value=None)
    part = item["cells"]["value"][0]
    assert part["text"] == "Sideways"
    assert part["scale"] == dd.ABSENT and "step" not in part


def test_importance_is_on_the_violet_ramp_from_its_own_number():
    for name, number, step in (("Not Important", 0.0, 0), ("Critical Importance", 4.0, 4)):
        item = shaped("sources", name="d", uri="https://a.example.com/x", type="News article",
                      importance=name, trustful=True, summary="", importance_value=number)
        part = item["cells"]["importance"][0]
        assert part["text"] == name
        assert part["scale"] == "relevance" and part["step"] == step


def test_trust_is_two_steps_of_the_valence_scale_with_the_word_beside_them():
    trusted = shaped("sources", name="d", uri="https://a.example.com/x", type="t",
                     importance="", trustful=True, summary="", importance_value=None)
    doubted = shaped("sources", name="d", uri="https://a.example.com/x", type="t",
                     importance="", trustful=False, summary="", importance_value=None)
    yes = trusted["cells"]["trust"][0]
    no = doubted["cells"]["trust"][0]
    assert yes["text"] == "Trusted" and yes["scale"] == "valence" and yes["step"] > 0
    assert no["text"] == "Not trusted" and no["scale"] == "valence" and no["step"] < 0


def test_an_outlook_and_a_sentiment_are_valence_and_unset_is_grey():
    item = shaped("market_insights", topic="Antitrust", short_outlook="Rising",
                  long_outlook="Unset", short_sentiment="Very Negative",
                  long_sentiment="Unset", high_relevance=True, reason="r", relation="Direct")
    rising, unset = item["cells"]["outlook"]
    assert rising["text"] == "Rising" and rising["scale"] == "valence" and rising["step"] > 0
    # Unset is absence, not a step of the scale: it takes the grey.
    assert unset["text"] == "Unset" and unset["scale"] == dd.ABSENT
    worst = item["cells"]["sentiment"][0]
    assert worst["text"] == "Very Negative" and worst["step"] == -3
    # Both horizons in one cell, short term first.
    assert words(item, "outlook") == "Rising / Unset"


def test_high_relevance_is_the_top_step_of_the_violet_ramp():
    high = shaped("market_insights", topic="t", high_relevance=True, reason="",
                  relation="", short_outlook="", long_outlook="",
                  short_sentiment="", long_sentiment="")
    part = high["cells"]["relevance"][0]
    assert part["text"] == "High relevance" and part["scale"] == "relevance" and part["step"] == 4


def test_the_entity_link_carries_the_NAME_and_never_the_id():
    """/diagrams/entity resolves a term against entities.text_name, so a
    link built from `ent:1822949` lands on a page that finds nothing."""
    item = shaped("connections", type="Supplier", reverse_type="Customer",
                  reference_name="Foxconn", target_name="Apple Inc.",
                  reason="", relation="Direct")
    assert item["cells"]["reference"][0]["entity"] == "Foxconn"
    assert item["cells"]["target"][0]["entity"] == "Apple Inc."
    # And the row reads as the sentence its header promises.
    assert words(item, "connection") == "Supplier"


def test_an_event_names_the_entities_it_is_about_one_link_each():
    item = shaped("events", name="Launch", type="Product launch", description="d",
                  eventdate=NOW, relation="Direct", entity_names=["Apple Inc.", "Foxconn"])
    assert [p["entity"] for p in item["cells"]["entities"]] == ["Apple Inc.", "Foxconn"]
    # An event with none is about the DOCUMENT, which is a fact rather than
    # a gap - so the cell says so instead of standing empty.
    alone = shaped("events", name="Launch", type="t", description="", eventdate=None,
                   relation="", entity_names=None)
    assert words(alone, "entities") == "About the document"


def test_which_tables_carry_the_entity_link_as_a_column_of_its_own():
    """Exactly four tables. A document has no single entity; the others
    reach the same page through a cell that already carries a name."""
    assert dd.row_links("ratings") == {"source": True, "entity": True}
    assert dd.row_links("sources") == {"source": True, "entity": False}
    assert dd.row_links("connections")["entity"] is False
    assert dd.row_links("entities")["entity"] is False


# ── The file behind the dialog ───────────────────────────────

def test_the_csv_carries_the_same_columns_and_only_the_words():
    row = {"rating_name": "Reputation", "value": "Excellent", "perspective": "Compliance",
           "relation": "Direct", "reason": "because", "rating_value": 3.0,
           "entity_name": "Apple Inc.", "source_name": "src:battery",
           "source_uri": "https://news.example.com/x", "row_date": NOW}
    columns = dd.csv_columns("ratings")
    assert columns[:5] == ["row", "domain", "source", "source_uri", "entity"]
    assert columns[5:] == [c.key for c in dd.columns_for("ratings")]
    out = dd.csv_row("ratings", row, 21)
    assert out["row"] == "21"
    assert out["domain"] == "news.example.com"
    assert out["entity"] == "Apple Inc."
    assert out["value"] == "Excellent"
    # A file carries values, and a value is a word: no scale, no step, no hex.
    assert all(isinstance(v, str) for v in out.values())
    assert "valence" not in " ".join(out.values())


# ── No colour is written in a view ───────────────────────────

VIEW_DIR = __import__("pathlib").Path(__file__).resolve().parents[2] / "app" / "static"
HEX = __import__("re").compile(r"#[0-9a-fA-F]{6}\b")


@pytest.mark.parametrize("name", ["js/drilldown.js", "js/charts.js", "js/diagrams.js"])
def test_no_colour_is_written_into_a_view(name):
    """The three scales live in app.css and reach a view through
    js/palette.js. A hex typed into a view is the second copy of the design
    system, and a second copy is how one meaning ends up two colours.

    The one shape allowed is `token("--name", "#fallback")`: a page rendered
    without the stylesheet (a stripped template test, a print preview) needs
    a colour rather than an empty string, which silently paints black - and
    tests/unit/test_palette.py compares those fallbacks with app.css
    character by character.
    """
    text = (VIEW_DIR / name).read_text(encoding="utf-8")
    for line in text.splitlines():
        for found in HEX.findall(line):
            assert "token(" in line, f"{name}: {found} is written into the view: {line.strip()}"


def test_the_drilldown_stylesheet_paints_from_tokens_only():
    """The dialog has a stylesheet of its own (css/drilldown.css), because
    the module that draws it is shared: a Diagrams chart and the landing
    page's importance charts open the SAME dialog."""
    css = (VIEW_DIR / "css" / "drilldown.css").read_text(encoding="utf-8")
    block = css[css.index("── Drilldown rows"):css.index("── Print")]
    assert not HEX.search(block), "a colour is written into drilldown.css"
    assert "var(--" in block
