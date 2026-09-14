"""A connection is read as a sentence: "Foxconn is a Supplier of Apple Inc."

The archive holds a connection as (parent, child, the parent's role, the
child's role), and the parent's role is the word that completes "Parent is a
X of Child" - that is how the extraction is asked for it. Two role words under
"Foxconn → Apple Inc." leave the reader to work out which end supplies which;
the card and the popup say it in full, both ways round.
"""

from __future__ import annotations

from preseed import ALPHA

EN = {"project": ALPHA, "language": "English"}
# The search is a search AROUND A PLACE; Cupertino is where the preseed's
# Apple stands, and the Foxconn/Apple connection has one end there.
CUPERTINO = (37.3318, -122.0312)


def _connection_items(client) -> list[dict]:
    lat, lng = CUPERTINO
    r = client.post("/api/query/search", params=EN,
                    json={"lat": lat, "lng": lng, "radius_km": 25, "kind": "connection"})
    assert r.status_code == 200, r.text
    return [i for group in r.json()["groups"] for i in group["items"] if i["kind"] == "connection"]


def test_the_card_reads_both_directions_as_sentences(client):
    items = _connection_items(client)
    assert items, "the preseed's Foxconn/Apple connection was not found"
    bodies = {i["body"] for i in items}
    assert any("Foxconn is a Supplier of Apple Inc." in b for b in bodies), bodies
    assert any("Apple Inc. is a Customer of Foxconn." in b for b in bodies), bodies
    # Never the raw pair of words, and never a role beside a blank name.
    for body in bodies:
        assert "Supplier / Customer" not in body, body
        assert " is a  of " not in body and "of ." not in body, body


def test_the_popup_says_who_is_what_of_whom(client):
    item = next(i for i in _connection_items(client)
                if i["title"].startswith("Foxconn") and "Supplier of" in i["body"])
    task_id, _, row_id = item["id"].rpartition(":")
    r = client.post("/api/query/detail", params=EN,
                    json={"kind": "connection", "task_id": task_id, "row_id": int(row_id)})
    assert r.status_code == 200, r.text
    row = r.json()["row"]
    assert row["connection"] == "Foxconn is a Supplier of Apple Inc.", row
    assert row["the other way round"] == "Apple Inc. is a Customer of Foxconn.", row
    # The two role columns are what the sentences are made of, so they are
    # not printed a second time as bare words.
    assert "type parent to child" not in row and "type child to parent" not in row, row
    # Both ends are there as entities, parent first.
    names = [e["name"] for e in r.json()["entities"]]
    assert names[:2] == ["Foxconn", "Apple Inc."], names
