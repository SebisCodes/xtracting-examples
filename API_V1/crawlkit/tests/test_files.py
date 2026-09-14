"""Files on a subpage: which are sent, which are not, and why not.

Every one of these cases has a row in `scraper.files` with its own status,
because "why is that PDF not in the archive?" has to be answerable on the
source's page - and "too large" is a different answer from "no text layer",
which is a different answer again from "the type is switched off".
"""

from __future__ import annotations

import io
from contextlib import contextmanager

import pytest

from crawlkit.files import (CHUNK_BYTES, FileRules, discover, download,
                            file_name, file_type, to_text)
from crawlkit.tests.standin.site import (docx_bytes, pdf_bytes, pdf_without_text,
                                         xlsx_bytes)


class FakeResponse:
    """What `download()` needs of a streaming response, and nothing else."""

    def __init__(self, status: int, headers: dict, chunks) -> None:
        self.status_code = status
        self.headers = headers
        self._chunks = chunks
        #: How much the caller actually pulled - the point of the cap test.
        self.read = 0

    def iter_bytes(self, size: int = CHUNK_BYTES):
        for chunk in self._chunks:
            self.read += len(chunk)
            yield chunk


def opener_for(response: FakeResponse):
    @contextmanager
    def opener(url: str, **_kwargs):
        yield response
    return opener


def endless(chunk_size: int = CHUNK_BYTES):
    """A body that never ends - the shape of a 30 MB plan set."""
    while True:
        yield b"%PDF-" + b"0" * (chunk_size - 5)


# -- what a file is ----------------------------------------------------

def test_type_and_name_come_from_the_address():
    assert file_type("https://a.example/f/Expose%20A.PDF?download=1") == "pdf"
    assert file_name("https://a.example/f/Expose%20A.PDF?download=1") == "Expose A.PDF"
    assert file_type("https://a.example/rent/1") == ""


def test_files_are_found_across_hosts():
    """The one place where the same-host brake is wrong: portals serve their
    documents from a file server of their own, and those are the documents
    that were the point."""
    html = ('<a href="/f/expose.pdf">Expose</a>'
            '<a href="https://files.example/plan.docx">Plan</a>'
            '<a href="/rent/2">another flat</a>')
    found = discover(html, "https://portal.example/rent/1")
    assert [(f.type, f.url) for f in found] == [
        ("pdf", "https://portal.example/f/expose.pdf"),
        ("docx", "https://files.example/plan.docx")]
    assert found[0].from_page == "https://portal.example/rent/1"


# -- the rules ---------------------------------------------------------

def test_type_defaults_and_individual_exceptions_are_different_concepts():
    rules = FileRules.from_config({"file_rules": [
        {"text_kind": "type_default", "text_value": "pdf", "bool_enabled": True},
        {"text_kind": "type_default", "text_value": "xlsx", "bool_enabled": False},
        {"text_kind": "file_include", "text_value": "https://a.example/f/one.xlsx"},
        {"text_kind": "file_exclude", "text_value": "https://a.example/f/two.pdf"},
    ]})
    assert rules.wanted("https://a.example/f/one.pdf", "pdf") == (True, "")
    assert rules.wanted("https://a.example/f/two.pdf", "pdf")[0] is False
    # An exception beats its type's default in both directions.
    assert rules.wanted("https://a.example/f/one.xlsx", "xlsx") == (True, "")
    assert rules.wanted("https://a.example/f/three.xlsx", "xlsx")[0] is False


def test_a_type_nobody_can_read_says_so_rather_than_failing_later():
    rules = FileRules.from_config({"file_rules": [
        {"text_kind": "type_default", "text_value": "zip", "bool_enabled": True}]})
    wanted, reason = rules.wanted("https://a.example/f/akten.zip", "zip")
    assert not wanted and "cannot be sent as text" in reason


# -- the download ------------------------------------------------------

def test_the_bytes_decide_not_the_extension():
    """An address ending in .pdf that answers with a login page. Without this
    check an HTML error page would sit in the archive as a PDF."""
    response = FakeResponse(200, {"content-type": "text/html"},
                            [b"<html><body>Please sign in</body></html>"])
    result = download("https://a.example/f/brochure.pdf", "pdf",
                      user_agent="test", max_bytes=1_000_000,
                      opener=opener_for(response))
    assert result.status == "WRONG_TYPE"
    assert "not a pdf" in result.detail


def test_an_announced_size_above_the_cap_is_refused_without_downloading():
    response = FakeResponse(200, {"content-type": "application/pdf",
                                  "content-length": str(30 * 1024 * 1024)},
                            [b"%PDF-" + b"0" * 100])
    result = download("https://a.example/f/huge.pdf", "pdf", user_agent="test",
                      max_bytes=1024 * 1024, opener=opener_for(response))
    assert result.status == "TOO_LARGE"
    assert response.read == 0


def test_an_unannounced_size_is_stopped_mid_stream():
    """The cap has to stop the READING. Checking afterwards would be
    bookkeeping about bytes that are already through the line - and four of
    those at once is a container out of memory."""
    response = FakeResponse(200, {"content-type": "application/pdf"}, endless())
    result = download("https://a.example/f/huge.pdf", "pdf", user_agent="test",
                      max_bytes=1024 * 1024, opener=opener_for(response))
    assert result.status == "TOO_LARGE"
    assert result.bytes_read <= 1024 * 1024 + CHUNK_BYTES
    assert response.read <= 1024 * 1024 + CHUNK_BYTES


def test_an_http_error_is_a_row_with_a_reason():
    response = FakeResponse(404, {"content-type": "text/html"}, [b"nope"])
    result = download("https://a.example/f/gone.pdf", "pdf", user_agent="test",
                      max_bytes=1000, opener=opener_for(response))
    assert result.status == "ERROR" and "404" in result.detail


def test_a_network_error_is_a_row_too():
    @contextmanager
    def opener(url: str, **_kwargs):
        raise TimeoutError("timed out")
        yield  # pragma: no cover

    result = download("https://a.example/f/x.pdf", "pdf", user_agent="test",
                      max_bytes=1000, opener=opener)
    assert result.status == "ERROR" and "timed out" in result.detail


def test_a_zip_that_is_not_a_workbook_is_wrong_type():
    response = FakeResponse(200, {"content-type": "application/zip"},
                            [b"PK\x03\x04" + b"junk"])
    result = download("https://a.example/f/rooms.xlsx", "xlsx", user_agent="test",
                      max_bytes=1_000_000, opener=opener_for(response))
    assert result.status == "WRONG_TYPE"


# -- the text ----------------------------------------------------------

def test_pdf_text_is_extracted():
    text, reason = to_text(pdf_bytes(["Expose 40012300", "Three rooms"]), "pdf")
    assert "Expose 40012300" in text and reason == ""


def test_a_pdf_without_a_text_layer_is_no_text_not_an_error():
    """A scan is a perfectly good file with nothing in it to submit. Sending
    it anyway would be paying for an empty extraction."""
    text, reason = to_text(pdf_without_text(), "pdf")
    assert text == ""
    assert reason == "the PDF has no text layer - a scan or a drawing"


def test_docx_paragraphs_become_lines():
    text, reason = to_text(docx_bytes(["Note on the flat", "Available at once."]), "docx")
    assert text.splitlines() == ["Note on the flat", "Available at once."]
    assert reason == ""


def test_xlsx_cells_become_rows_with_the_sheet_name():
    data = xlsx_bytes([["Room", "Area"], ["Living", "28"]])
    text, reason = to_text(data, "xlsx")
    assert text.splitlines() == ["Rooms", "Room\tArea", "Living\t28"]
    assert reason == ""


def test_csv_survives_the_encoding_a_spreadsheet_exports():
    """cp1252 turns up often enough that failing on it would mean losing the
    file over an umlaut."""
    text, reason = to_text("Raum;Fläche\nWohnen;28\n".encode("cp1252"), "csv")
    assert text == "Raum;Fläche\nWohnen;28"
    assert reason == ""


def test_a_broken_file_is_a_reason_not_an_exception():
    """One unreadable document must not end a run that has nineteen good ones
    left in it."""
    text, reason = to_text(b"PK\x03\x04 truncated", "docx")
    assert text == "" and reason.startswith("the file could not be read")


@pytest.mark.parametrize("kind", ["txt", "md", "csv"])
def test_an_empty_file_says_it_is_empty(kind):
    assert to_text(b"   \n", kind) == ("", "the file is empty")
