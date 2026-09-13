"""A document is never called by its id.

    python -m pytest tests/unit/test_source_names.py -q

Measured on a real archive: `sources.text_name` can be `src_1416664` in some
rows and an md5 in others, in every single row. So
the rule is on the VALUE - a name that looks like an identifier is shown
nowhere - and the heading is built from the address instead: the domain,
and the document's own slug read as words.

The cases here are real rows out of that archive and out of the demo one,
not invented strings.
"""

from __future__ import annotations

import pytest

from app.source_names import (host_of, is_identifier, source_heading,
                              title_from_uri)

# Straight out of processed_data.sources.
SERIAL = "src_1127664"
CHECKSUM = "8846ebcc63ff6a49245ef85e08e776de"
NEWS = ("https://www.news.example.com/news/Business/525665/"
        "Council-approves-new-bridge-over-the-river-by-2030")
DATED = ("https://paper.example.org/economia/noticia/2025/01/04/"
         "governo-estima-deficit-fiscal-de-2024.ghtml")
SCHEMED = "src:city-archive-foia-2006"


class TestIsIdentifier:
    @pytest.mark.parametrize("name", [SERIAL, "src_1", "src_999999999", CHECKSUM,
                                      CHECKSUM.upper()])
    def test_the_two_shapes_this_archive_has_ever_used(self, name):
        assert is_identifier(name)

    @pytest.mark.parametrize("name", [
        "", None, "   ",
        "Apple reports third-quarter results",
        # ANCHORED: a real title that mentions one is a real title.
        "Why src_1127664 was withdrawn",
        "src_", "src_12ab",
        # 31 and 33 hex characters are not the checksum.
        CHECKSUM[:-1], CHECKSUM + "0",
        # A word of hex letters is a word.
        "deadbeefdeadbeefdeadbeefdeadbeef".replace("d", "g"),
    ])
    def test_a_real_name_survives(self, name):
        assert not is_identifier(name)


class TestHostOf:
    def test_the_domain_without_www(self):
        assert host_of(NEWS) == "news.example.com"
        assert host_of("http://EXAMPLE.com/a") == "example.com"

    def test_an_address_with_no_host_has_no_domain(self):
        assert host_of(SCHEMED) == ""
        assert host_of("") == ""
        assert host_of(None) == ""


class TestTitleFromUri:
    def test_the_slug_becomes_words(self):
        assert title_from_uri(NEWS) == "Council approves new bridge over the river by 2030"

    def test_a_year_is_not_an_id_and_stays(self):
        """`…/2025/01/04/governo-…` and `src:city-archive-foia-2006` both
        carry a year, and it is the only date the title has."""
        assert title_from_uri(DATED) == "Governo estima deficit fiscal de 2024"
        assert title_from_uri(SCHEMED) == "City archive foia 2006"

    def test_the_id_segment_is_skipped_and_the_slug_beside_it_used(self):
        assert title_from_uri("https://a.example/news/525665/") == "News"
        assert title_from_uri("https://a.example/article/mondiaux-de-piste-12345678") \
            == "Mondiaux de piste"

    def test_an_address_with_no_slug_gets_no_title(self):
        assert title_from_uri("https://example.com") == ""
        assert title_from_uri("https://example.com/?id=4") == ""
        assert title_from_uri("") == ""
        assert title_from_uri(None) == ""


class TestSourceHeading:
    def test_an_identifier_is_replaced_by_the_address(self):
        head = source_heading(SERIAL, NEWS)
        assert SERIAL not in head["text"]
        assert head["domain"] == "news.example.com"
        assert head["title"].startswith("Council approves")
        assert head["derived"] is True
        assert head["text"] == "news.example.com - " + head["title"]

    def test_a_real_name_is_kept_exactly_as_it_is(self):
        """An archive that does carry names keeps them, with NOTHING added:
        the domain replaces a missing name, it does not decorate a present
        one, and a view that shows both would be showing two names."""
        head = source_heading("Apple reports third-quarter results", NEWS)
        assert head["title"] == "Apple reports third-quarter results"
        assert head["derived"] is False
        assert head["text"] == "Apple reports third-quarter results"
        # Still reported, for a view that wants to lay the two out itself.
        assert head["domain"] == "news.example.com"

    def test_a_checksum_with_a_slugless_address_falls_back_to_the_domain(self):
        head = source_heading(CHECKSUM, "https://example.com")
        assert head["text"] == "example.com"
        assert head["title"] == ""
        assert head["derived"] is False

    def test_the_demo_archive_shape(self):
        head = source_heading(SERIAL, SCHEMED)
        assert head["domain"] == ""
        assert head["text"] == "City archive foia 2006"
        assert head["derived"] is True

    def test_nothing_at_all_is_said_rather_than_an_id(self):
        assert source_heading(SERIAL, "")["text"] == ""
        assert source_heading("", "")["text"] == ""
