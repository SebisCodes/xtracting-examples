"""From HTML to the text that is submitted.

The two formats are two products with two price tags, and the tests here are
mostly about the difference between them: what plain text drops, what cleaned
HTML keeps, and what neither of them may ever contain.
"""

from __future__ import annotations

from crawlkit.content import (KEEP_ATTRIBUTES, Link, extract_links, next_page,
                              prepare)
from crawlkit.hashing import sha256_text

PAGE = """<!doctype html>
<html><head><title>Flat 40012300 | Portal</title>
<link rel="stylesheet" href="/app.css"></head>
<body>
  <nav><a href="/">Home</a><a href="/impressum">Impressum</a></nav>
  <header>Portal</header>
  <main class="content" data-track="detail-view">
    <h1>Three rooms in Zurich</h1>
    <p class="lead">Available from 1 April.</p>
    <ul><li>Rent 2400</li><li>Floor 3</li></ul>
    <a href="/files/expose-40012300.pdf" title="Expose">Expose (PDF)</a>
  </main>
  <script>tracker({page: "detail"});</script>
  <style>.lead { color: red }</style>
  <footer>Portal AG, Zurich</footer>
</body></html>"""


def test_plain_text_is_the_reading_of_the_page():
    prepared = prepare(PAGE)
    assert prepared.title == "Three rooms in Zurich"
    assert "Available from 1 April." in prepared.content
    assert "Rent 2400" in prepared.content
    # Navigation, header and footer are furniture, and they are on every page
    # of the site: submitting them means paying for the same words per
    # document, per run, forever.
    assert "Home" not in prepared.content
    assert "Portal AG" not in prepared.content


def test_script_and_style_are_in_neither_format():
    """The one rule both formats share. Cleaned HTML with a script in it would
    send code to be read as prose - and be billed for it."""
    for text_format in ("plain", "html"):
        content = prepare(PAGE, text_format=text_format).content
        assert "tracker(" not in content
        assert "color: red" not in content
        assert "<script" not in content


def test_html_keeps_the_structure_and_drops_the_decoration():
    prepared = prepare(PAGE, text_format="html")
    assert "<h1>" in prepared.content and "<li>" in prepared.content
    # Classes, data attributes and inline styles are layout, and layout is
    # paid for by the character.
    assert "data-track" not in prepared.content
    assert 'class="lead"' not in prepared.content
    # The attributes that carry meaning stay.
    assert 'href="/files/expose-40012300.pdf"' in prepared.content
    assert 'title="Expose"' in prepared.content
    assert set(KEEP_ATTRIBUTES) == {"href", "src", "alt", "title"}


def test_html_costs_more_than_plain_and_the_test_can_say_how_much():
    """The number the editor shows next to the format switch."""
    plain = prepare(PAGE)
    html = prepare(PAGE, text_format="html")
    assert html.char_count > plain.char_count


def test_the_hash_is_over_what_is_sent():
    """This is the number the platform reports back and the dashboard lines up
    with `processed_data.sources.text_content_hash`. If it were taken over
    anything but the submitted string, every document would look changed."""
    prepared = prepare(PAGE)
    assert prepared.content_hash == sha256_text(prepared.content)
    assert prepared.content_hash == prepare(PAGE).content_hash
    assert prepared.content_hash != prepare(PAGE, text_format="html").content_hash


def test_a_changed_page_gives_a_different_hash():
    changed = PAGE.replace("Rent 2400", "Rent 2500")
    assert prepare(changed).content_hash != prepare(PAGE).content_hash


def test_the_content_selector_narrows_and_survives_going_stale():
    narrowed = prepare(PAGE, content_selector="main .lead")
    assert narrowed.content == "Available from 1 April."
    # A selector that matches nothing must not produce an empty document: the
    # site changed, and the whole body is still better than nothing.
    fallback = prepare(PAGE, content_selector=".gone")
    assert "Rent 2400" in fallback.content


def test_drop_selectors_are_comma_separated_as_the_column_stores_them():
    prepared = prepare(PAGE, drop_selectors="ul, .lead")
    assert "Rent 2400" not in prepared.content
    assert "Available from 1 April." not in prepared.content
    assert "Three rooms in Zurich" in prepared.content


def test_links_are_canonical_deduplicated_and_keep_their_text():
    links = extract_links(PAGE, "https://portal.example/rent/40012300")
    urls = [link.url for link in links]
    assert "https://portal.example/impressum" in urls
    assert "https://portal.example/files/expose-40012300.pdf" in urls
    assert len(urls) == len(set(urls))
    assert links[0].text == "Home"


def test_the_stylesheet_is_not_a_link():
    """`<link rel=stylesheet>` is not `<a href>`; a crawl that followed it
    would fetch CSS and call it a document."""
    urls = [link.url for link in extract_links(PAGE, "https://portal.example/x")]
    assert not any(url.endswith("app.css") for url in urls)


def test_pagination_prefers_the_learned_parameter_over_a_wrong_rel_next():
    """The parameter was learned from links a person ticked; `rel=next` is
    whatever the site put in its markup. When they disagree the learned one
    wins - it is the one that matched the pages that were actually wanted."""
    html = ('<a rel="next" href="/newsletter">Newsletter</a>'
            '<a href="?ep=2">2</a><a href="?ep=3">3</a>')
    base = "https://portal.example/list"
    assert next_page(html, base, paging_param="ep") == "https://portal.example/list?ep=2"
    assert next_page(html, base) == "https://portal.example/newsletter"


def test_pagination_stops_at_the_last_page():
    html = '<a href="?ep=1">1</a><a href="?ep=2">2</a>'
    assert next_page(html, "https://portal.example/list?ep=2", paging_param="ep") is None


def test_pagination_ignores_a_paging_link_of_another_list():
    """`?ep=2` on a sibling list is page two of THAT list."""
    html = '<a href="/other/list?ep=2">Basel, page 2</a>'
    assert next_page(html, "https://portal.example/list", paging_param="ep") is None


def test_links_can_be_handed_in_so_the_page_is_parsed_once():
    links = [Link("https://portal.example/list?ep=2", "2")]
    assert next_page("", "https://portal.example/list", paging_param="ep",
                     links=links) == "https://portal.example/list?ep=2"
