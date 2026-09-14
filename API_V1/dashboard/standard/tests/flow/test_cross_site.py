"""What a page on another site may do to this dashboard: nothing that writes.

    DATABASE_URL=postgresql://... python -m pytest tests/flow/test_cross_site.py -q

With no gate password the flow client is an intranet installation, which is
exactly the case the check in main.py exists for: no cookie is needed, so a
form on any web page could otherwise switch the crawler on. The browser says
where a request came from, and a write from elsewhere is refused before the
route runs.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.flow


def test_a_write_from_another_site_changes_nothing(client, archive):
    before = client.get("/api/crawler/state").json()["running"]
    answer = client.post("/api/crawler/state", json={"running": not before},
                         headers={"Origin": "https://evil.example"})
    assert answer.status_code == 403
    assert client.get("/api/crawler/state").json()["running"] == before

    answer = client.post("/api/sources/999999/enable",
                         headers={"Sec-Fetch-Site": "cross-site"})
    assert answer.status_code == 403


def test_the_dashboard_s_own_page_may_write(client):
    before = client.get("/api/crawler/state").json()["running"]
    try:
        answer = client.post("/api/crawler/state", json={"running": not before},
                             headers={"Origin": "http://testserver",
                                      "Sec-Fetch-Site": "same-origin"})
        assert answer.status_code == 200
        assert answer.json()["running"] is (not before)
    finally:
        client.post("/api/crawler/state", json={"running": before})


def test_every_answer_carries_the_security_headers(client):
    for path in ("/api/projects", "/query", "/api/export/views", "/api/nope"):
        response = client.get(path)
        assert response.headers["x-content-type-options"] == "nosniff", path
        assert response.headers["referrer-policy"] == "strict-origin-when-cross-origin", path
        assert response.headers["x-frame-options"] == "DENY", path
