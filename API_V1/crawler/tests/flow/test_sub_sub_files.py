"""Files on a subpage: sent as text, or listed with a reason.

Xtracting takes text and nothing else, so a PDF is not attached anywhere - its
text layer is extracted and submitted like a page. Everything that does not get
that far still leaves a row, because "why is that PDF not in the archive?" has
to be answerable on the source's page.
"""

from __future__ import annotations

import pytest

FLAT = 40012300      # the one listing that carries all five kinds of file

#: What the stand-in site serves for `huge-*.pdf` (crawlkit/tests/standin).
HUGE_FILE_BYTES = 30 * 1024 * 1024


@pytest.fixture()
def files_source(crawler):
    """One watched listing with files switched on: pdf and csv yes, xlsx no.

    The pdf cap is one megabyte, so the thirty-megabyte plan set can show what
    a cap that stops the reading looks like.
    """
    address = f"{crawler.site.estate}/rent/{FLAT}"
    source_id = crawler.add_source(
        "estate files", crawler.site.estate_list, text_mode="exact",
        monitor=[address], bool_sub_sub=True,
        file_types={"pdf": (True, 1), "csv": True, "xlsx": False})
    crawler.crawl(source_id)
    return source_id


def status_of(crawler, source_id: int, needle: str) -> dict:
    for row in crawler.files(source_id):
        if needle in row["text_file_uri_canonical"]:
            return row
    raise AssertionError(f"no file row for {needle}")


def test_a_pdf_with_a_text_layer_is_queued_as_a_document(crawler, files_source):
    row = status_of(crawler, files_source, f"expose-{FLAT}.pdf")
    assert row["text_status"] == "QUEUED"

    documents = [document for document in crawler.documents(files_source)
                 if document["text_kind"] == "file"]
    expose = [document for document in documents if "expose-" in document["text_uri"]]
    assert expose and expose[0]["text_file_type"] == "pdf"
    assert expose[0]["text_format"] == "file_text"
    assert f"Expose {FLAT}" in expose[0]["text_content"]
    # And it is queued for submission like any other document.
    assert any(item["text_source_uri"] == expose[0]["text_uri_canonical"]
               for item in crawler.queue(files_source))


def test_a_csv_is_sent_as_text(crawler, files_source):
    row = status_of(crawler, files_source, f"floorplan-{FLAT}.csv")
    assert row["text_status"] == "QUEUED"
    document = [d for d in crawler.documents(files_source)
                if "floorplan-" in d["text_uri"]][0]
    assert "Wohnen;28" in document["text_content"]


def test_a_type_that_is_switched_off_is_not_downloaded_at_all(crawler, files_source):
    """A type default is a decision about a hundred files. It is taken before
    anything is fetched - otherwise switching xlsx off would still cost the
    download."""
    row = status_of(crawler, files_source, f"rooms-{FLAT}.xlsx")
    assert row["text_status"] == "NOT_SELECTED"
    assert "switched off" in row["text_detail"]
    assert not crawler.site.fetched("estate", f"rooms-{FLAT}.xlsx")


def test_an_html_page_served_under_a_pdf_address_is_wrong_type(crawler, files_source):
    """The bytes decide, not the extension. Without that check a login page
    would sit in the archive as a PDF."""
    row = status_of(crawler, files_source, f"brochure-{FLAT}.pdf")
    assert row["text_status"] == "WRONG_TYPE"
    assert "not a pdf" in row["text_detail"]
    assert not any("brochure-" in document["text_uri"]
                   for document in crawler.documents(files_source))


def test_a_thirty_megabyte_file_is_stopped_after_the_cap(crawler, files_source):
    """The cap stops the READING. Checking the size afterwards would be
    bookkeeping about bytes that are already through the line.

    The claim is "a small fraction of the file", not an exact figure. What the
    server has managed to WRITE by the time the reader hangs up depends on the
    socket buffers and on how the two processes were scheduled - it measured
    1.75, 2.1 and 3.5 MiB on three runs of the same code - and a test that
    asserts a number the operating system chooses fails for reasons that have
    nothing to do with the cap. A quarter of thirty megabytes still tells the
    two outcomes apart: reading stopped early, or the whole file came down.
    """
    row = status_of(crawler, files_source, f"huge-{FLAT}.pdf")
    assert row["text_status"] == "TOO_LARGE"
    sent = crawler.site.bytes_sent(f"huge-{FLAT}.pdf")
    assert 0 < sent < HUGE_FILE_BYTES // 4


def test_every_file_leaves_a_row_whatever_became_of_it(crawler, files_source):
    rows = {row["text_file_uri_canonical"].rsplit("/", 1)[-1]: row["text_status"]
            for row in crawler.files(files_source)}
    assert rows == {
        f"expose-{FLAT}.pdf": "QUEUED",
        f"floorplan-{FLAT}.csv": "QUEUED",
        f"rooms-{FLAT}.xlsx": "NOT_SELECTED",
        f"brochure-{FLAT}.pdf": "WRONG_TYPE",
        f"huge-{FLAT}.pdf": "TOO_LARGE",
    }


def test_files_stay_off_unless_the_source_asks_for_them(crawler):
    """"Also collect files" is a switch someone has to turn on: files cost
    downloads, and their text costs extractions."""
    address = f"{crawler.site.estate}/rent/{FLAT}"
    source_id = crawler.add_source(
        "estate no files", crawler.site.estate_list, text_mode="exact",
        monitor=[address], bool_sub_sub=False,
        file_types={"pdf": True, "csv": True})
    crawler.crawl(source_id)

    assert crawler.files(source_id) == []
    assert not crawler.site.fetched("estate", "expose-")
