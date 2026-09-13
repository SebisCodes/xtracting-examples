"""Four web sites in one process, for the crawl tests.

WHY NOT RECORD THE REAL ONES: homegate, immoscout24, autoscout24 and fedlex
are what the rules in `crawlkit` were built from, and a test suite that
fetches them is a test suite that fails when a portal redesigns, that cannot
run in CI, and that puts load on somebody else's site every time somebody runs
`pytest`. What the tests actually need is the SHAPE of those sites - the URL
patterns, the robots.txt rules, the JavaScript-built list - and that is what
these four hosts are.

    estate   a rental portal: /rent/<8 digits> detail pages, an /impressum
             footer, a sibling list for another city, ?ep= pagination that
             robots.txt forbids, files on the detail pages, and a /challenge
             page that answers 403 with a browser check. Crawl-delay: 1.
    cars     a car portal: /de/d/<make>-<model>-<8 digits> with /fr twins,
             ?page= pagination and a sort= parameter, both forbidden.
    law      a legislation search: the delivered HTML has an empty <main> and
             a script that injects 62 links; without a browser there is
             nothing to collect. /eli/oc/... and /eli/cc/..., ?print= forbidden.
    broken   robots.txt answers 503 - the case where nothing may be fetched.

Each host is its own port, so "same host" and "other host" are real to the
code under test. The access log is the point of several tests: "the imprint
was never fetched" is a statement about requests, not about results.

Run it by hand to look at the pages:

    python -m crawlkit.tests.standin.site
"""

from __future__ import annotations

import io
import json
import threading
import zipfile
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest

#: The twenty flats of the estate list, and the ids they carry.
FLAT_IDS = [40012300 + n for n in range(20)]

#: Pages two and three of the same list carry DIFFERENT flats. A list whose
#: pages repeat themselves would be stopped by the "nothing new" rule before
#: the loop-back rule could ever be reached, and then only one of the four
#: stop rules would be under test here.
FLAT_PAGES = {1: FLAT_IDS,
              2: [40012320 + n for n in range(20)],
              3: [40012340 + n for n in range(20)]}
ALL_FLAT_IDS = [flat for page in FLAT_PAGES.values() for flat in page]

#: The immoscout-shaped twins on the same list: /de/d/<slug>/<id>.
FLAT_SLUGS = [("wohnung-mieten-zuerich", 4711), ("wohnung-mieten-oerlikon", 4712),
              ("wohnung-mieten-altstetten", 4713)]

#: The cars, as make-model-id, each with a /de and a /fr address.
CAR_IDS = [(make, model, 30045500 + n) for n, (make, model) in enumerate([
    ("audi", "a4"), ("bmw", "320d"), ("skoda", "octavia"), ("vw", "golf"),
    ("volvo", "v60"), ("seat", "leon"), ("opel", "astra"), ("ford", "focus"),
    ("mazda", "cx5"), ("kia", "ceed"), ("fiat", "500"), ("mini", "cooper"),
    ("dacia", "duster"), ("tesla", "model3"), ("honda", "civic"),
    ("nissan", "qashqai"), ("hyundai", "i30"), ("renault", "clio"),
    ("peugeot", "308"), ("citroen", "c3"),
])]

#: The legislation entries: twenty in the official compilation (oc), twenty in
#: the classified compilation (cc), each with a German and a French address.
LAW_NUMBERS = list(range(101, 121))
#: What page two of the search adds - different entries, so that following the
#: pagination is worth something and the "nothing new" stop rule is not what
#: ends it.
LAW_PAGE_TWO = list(range(121, 131))

HUGE_FILE_BYTES = 30 * 1024 * 1024


# ----------------------------------------------------------------------
# Small file builders - real PDFs, real spreadsheets
# ----------------------------------------------------------------------

def pdf_bytes(lines: list[str]) -> bytes:
    """A one-page PDF with a real text layer, built by hand.

    Hand-built rather than with a library because the test needs to control
    exactly one thing - whether there is text to extract - and a PDF library
    in the test dependencies would be a second implementation of the thing
    under test.
    """
    drawn = "".join(f"({line.replace('(', '').replace(')', '')}) Tj 0 -24 Td\n"
                    for line in lines)
    stream = f"BT /F1 14 Tf 72 720 Td\n{drawn}ET"
    return _pdf_with_stream(stream)


def pdf_without_text(width: int = 200) -> bytes:
    """A PDF that has a page and no text - a scan, as far as anyone can tell.

    `files.to_text()` must call this NO_TEXT and not an error: the file is
    perfectly fine, there is simply nothing in it to submit.
    """
    return _pdf_with_stream(f"0.5 0.5 0.5 rg 72 600 {width} 120 re f")


def _pdf_with_stream(stream: str) -> bytes:
    objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources "
        "<< /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for number, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n{obj}\nendobj\n".encode("latin-1")
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n"
            f"{xref}\n%%EOF\n").encode()
    return bytes(out)


def xlsx_bytes(rows: list[list[str]]) -> bytes:
    """A minimal .xlsx - a zip with the parts openpyxl insists on."""
    from openpyxl import Workbook
    book = Workbook()
    sheet = book.active
    sheet.title = "Rooms"
    for row in rows:
        sheet.append(row)
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


def docx_bytes(paragraphs: list[str]) -> bytes:
    """A .docx with the one part that matters: word/document.xml."""
    body = "".join(f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>" for text in paragraphs)
    document = ('<?xml version="1.0" encoding="UTF-8"?>'
                '<w:document xmlns:w="http://schemas.openxmlformats.org/'
                'wordprocessingml/2006/main"><w:body>' + body + "</w:body></w:document>")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml",
                         '<?xml version="1.0"?><Types xmlns="http://schemas.'
                         'openxmlformats.org/package/2006/content-types"/>')
        archive.writestr("word/document.xml", document)
    return buffer.getvalue()


# ----------------------------------------------------------------------
# Page builders
# ----------------------------------------------------------------------

def _page(title: str, body: str, *, footer: bool = True) -> str:
    legal = ('<footer><a href="/impressum">Impressum</a> '
             '<a href="/agb">AGB</a> '
             '<a href="/datenschutz">Datenschutz</a></footer>') if footer else ""
    return (f"<!doctype html><html lang=\"de\"><head><meta charset=\"utf-8\">"
            f"<title>{title}</title></head><body><h1>{title}</h1>{body}"
            f"{legal}</body></html>")


def _estate_list(page: int, changed: int) -> str:
    flats = "".join(
        f'<li><a href="/rent/{flat_id}">Wohnung {flat_id}</a></li>'
        for flat_id in FLAT_PAGES.get(page, FLAT_IDS))
    twins = "".join(
        f'<li><a href="/de/d/{slug}/{number}">Wohnung {number}</a></li>'
        for slug, number in FLAT_SLUGS)
    if page == 1:
        paging = ('<a href="?ep=2">Weiter</a> <a href="?ep=3">3</a>')
    elif page == 2:
        paging = ('<a href="?ep=1">1</a> <a href="?ep=3">Weiter</a>')
    else:
        # The last page links back to the first, and at its plain address -
        # which is how sites write it, and the loop the crawl has to stop by
        # itself.
        paging = ('<a href="/rent/apartment/city-zurich/matching-list">'
                  'Weiter</a>')
    return _page(
        f"Mietwohnungen Zürich (Seite {page})",
        f"<!-- v{changed} --><ul>{flats}{twins}</ul>"
        f'<nav class="paging">{paging}</nav>'
        f'<p><a href="/rent/apartment/city-basel/matching-list">'
        f'Auch in Basel suchen</a></p>'
        f'<p><a href="/search-srp-new/zurich">Neue Suche</a> '
        f'<a href="/challenge">Anmelden</a> '
        f'<a href="/files/private/preisliste.pdf">Preisliste</a></p>')


def _estate_detail(flat_id: int, changed: int) -> str:
    files = (f'<a href="/files/expose-{flat_id}.pdf">Exposé (PDF)</a> '
             f'<a href="/files/floorplan-{flat_id}.csv">Grundriss (CSV)</a> '
             f'<a href="/files/rooms-{flat_id}.xlsx">Raumliste (XLSX)</a>')
    if flat_id == FLAT_IDS[0]:
        files += (f'<a href="/files/brochure-{flat_id}.pdf">Broschüre (PDF)</a> '
                  f'<a href="/files/huge-{flat_id}.pdf">Pläne (PDF)</a>')
    return _page(f"Wohnung {flat_id}",
                 f"<p>Drei Zimmer, {70 + flat_id % 30} m², Zürich.</p>"
                 f"<p>Stand {changed}.</p><p>{files}</p>")


def _cars_list(page: int) -> str:
    entries = "".join(
        f'<li><a href="/de/d/{make}-{model}-{number}">{make} {model}</a>'
        f'<a href="/fr/d/{make}-{model}-{number}" hreflang="fr">fr</a></li>'
        for make, model, number in CAR_IDS)
    paging = ('<a href="?page=2">Weiter</a>' if page == 1
              else '<a href="?page=1">Zurück</a>')
    return _page(f"Alle Marken (Seite {page})",
                 f"<ul>{entries}</ul><nav>{paging}</nav>"
                 f'<p><a href="/de/autos/alle-marken?sort=price">Nach Preis '
                 f'sortieren</a></p>')


def _law_search(page: int) -> str:
    """The fedlex shape: nothing in the HTML, everything in the script.

    THIS IS THE WHOLE POINT OF RENDERED MODE. A plain HTTP request gets a
    navigation-free page with an empty <main>; the sixty-two links appear
    fifty milliseconds after the application starts.
    """
    numbers = LAW_NUMBERS if page == 1 else LAW_PAGE_TWO
    links = []
    for number in numbers:
        links.append(f"/eli/oc/2024/{number}/de")
        links.append(f"/eli/oc/2024/{number}/fr")
        if page == 1:
            links.append(f"/eli/cc/2024/{number}/de")
    links.append("/impressum")
    if page == 1:
        # The next page as an anchor as well as a <link>: the anchor is what a
        # person clicks, and it is the sixty-second link the rendered page
        # carries. Over plain HTTP neither of them exists yet.
        links.append("/search?query=umwelt&currentPage=2")
    payload = json.dumps(links)
    next_link = (f'<link rel="next" href="/search?query=umwelt&currentPage=2">'
                 if page == 1 else "")
    return (f"<!doctype html><html lang=\"de\"><head><meta charset=\"utf-8\">"
            f"<title>Suche</title>{next_link}</head><body>"
            f"<main id=\"results\"></main>"
            f"<script>const hits = {payload};"
            f"setTimeout(function () {{"
            f"  document.getElementById('results').innerHTML ="
            f"    hits.map(function (href) {{"
            f"      return '<a href=\"' + href + '\">' + href + '</a>';"
            f"    }}).join('');"
            f"}}, 50);</script></body></html>")


def _law_detail(kind: str, number: int, language: str) -> str:
    return _page(f"Verordnung {kind.upper()} 2024/{number} ({language})",
                 f"<p>Artikel 1. Diese Verordnung regelt {number}.</p>"
                 f'<p><a href="/eli/{kind}/2024/{number}/{language}?print=1">'
                 f'Druckansicht</a> '
                 f'<a href="/files/{kind}-{number}.pdf">PDF</a></p>',
                 footer=False)


CHALLENGE_HTML = _page("Just a moment...",
                       "<p>Checking your browser before access.</p>", footer=False)


# ----------------------------------------------------------------------
# The servers
# ----------------------------------------------------------------------

ROBOTS = {
    "estate": ("User-agent: *\n"
               "Disallow: /*-srp-new/*\n"
               "Disallow: /*?*ep=\n"
               "Disallow: /files/private/\n"
               "Crawl-delay: 1\n"),
    "cars": ("User-agent: *\n"
             "Disallow: /*sort=\n"
             "Disallow: /*?*page=\n"),
    "law": ("User-agent: *\n"
            "Allow: /\n"
            "Disallow: /*?print=\n"),
    "broken": "",
}


@dataclass
class SiteState:
    """What the four hosts share: the access log and the content version."""

    requests: list[tuple[str, str]] = field(default_factory=list)
    bytes_sent: dict[str, int] = field(default_factory=dict)
    #: Bumped by `change()`; appears in the detail pages so that a second run
    #: sees different content without a different address.
    version: int = 1
    lock: threading.Lock = field(default_factory=threading.Lock)

    def log(self, host: str, path: str) -> None:
        with self.lock:
            self.requests.append((host, path))

    def paths(self, host: str) -> list[str]:
        with self.lock:
            return [path for name, path in self.requests if name == host]

    def fetched(self, host: str, needle: str) -> bool:
        return any(needle in path for path in self.paths(host))

    def count(self, host: str, needle: str) -> int:
        return len([path for path in self.paths(host) if needle in path])


class _Handler(BaseHTTPRequestHandler):
    """One handler class per host; `site_name` is set by the subclass."""

    site_name = ""
    state: SiteState
    protocol_version = "HTTP/1.1"

    def log_message(self, *args: object) -> None:
        pass  # no noise in the test output

    # -- helpers -------------------------------------------------------

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _html(self, body: str, status: int = 200) -> None:
        self._send(status, body.encode("utf-8"), "text/html; charset=utf-8")

    def _text(self, body: str, status: int = 200,
              content_type: str = "text/plain; charset=utf-8") -> None:
        self._send(status, body.encode("utf-8"), content_type)

    def _not_found(self) -> None:
        self._html(_page("404", "<p>Nicht gefunden.</p>", footer=False), 404)

    def _stream_huge(self, path: str) -> None:
        """Thirty megabytes, WITHOUT announcing the size.

        Without Content-Length the client cannot refuse it up front, which is
        exactly the case the size cap has to survive: the reading stops in the
        middle, and `bytes_sent` shows how little actually went over the wire.
        """
        self.send_response(200)
        self.send_header("Content-Type", "application/pdf")
        self.send_header("Connection", "close")
        self.end_headers()
        chunk = b"%PDF-1.4\n" + b"0" * (64 * 1024 - 9)
        written = 0
        try:
            while written < HUGE_FILE_BYTES:
                self.wfile.write(chunk)
                written += len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            with self.state.lock:
                self.state.bytes_sent[path] = written
            self.close_connection = True

    # -- routing -------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - the base class names it
        parsed = urlsplit(self.path)
        query = parse_qs(parsed.query, keep_blank_values=True)
        self.state.log(self.site_name, self.path)

        if parsed.path == "/robots.txt":
            if self.site_name == "broken":
                self._text("robots.txt is unavailable", 503)
                return
            self._text(ROBOTS[self.site_name])
            return

        handler = getattr(self, f"_route_{self.site_name}")
        handler(parsed.path, query)

    # -- estate --------------------------------------------------------

    def _route_estate(self, path: str, query: dict) -> None:
        version = self.state.version
        if path == "/rent/apartment/city-zurich/matching-list":
            page = int((query.get("ep") or ["1"])[0] or 1)
            self._html(_estate_list(page, version))
            return
        if path == "/rent/apartment/city-basel/matching-list":
            self._html(_page("Mietwohnungen Basel", "<ul><li>Nichts hier.</li></ul>"))
            return
        if path.startswith("/rent/"):
            try:
                flat_id = int(path.rsplit("/", 1)[-1])
            except ValueError:
                self._not_found()
                return
            if flat_id in ALL_FLAT_IDS:
                self._html(_estate_detail(flat_id, version))
                return
            self._not_found()
            return
        if path.startswith("/de/d/"):
            self._html(_page(f"Wohnung {path.rsplit('/', 1)[-1]}",
                             "<p>Zwei Zimmer, Zürich.</p>"))
            return
        if path in ("/impressum", "/agb", "/datenschutz"):
            self._html(_page(path.strip("/").title(), "<p>Rechtliches.</p>"))
            return
        if path == "/challenge":
            self._html(CHALLENGE_HTML, 403)
            return
        if path.startswith("/files/"):
            self._estate_file(path)
            return
        if path.startswith("/search-srp-new/"):
            self._html(_page("Neue Suche", "<p>Nur mit JavaScript.</p>"))
            return
        self._not_found()

    def _estate_file(self, path: str) -> None:
        name = path.rsplit("/", 1)[-1]
        if path.startswith("/files/private/"):
            self._send(200, pdf_bytes(["Interne Preisliste"]), "application/pdf")
            return
        if name.startswith("huge-"):
            self._stream_huge(path)
            return
        if name.startswith("brochure-"):
            # An address ending in .pdf that answers with a login page. The
            # reason files.py checks the bytes and not the extension.
            self._html(_page("Anmelden", "<p>Bitte anmelden.</p>", footer=False))
            return
        if name.startswith("expose-"):
            flat = name[len("expose-"):-len(".pdf")]
            self._send(200, pdf_bytes([f"Expose {flat}",
                                       "Drei Zimmer, Zuerich",
                                       f"Miete {2000 + int(flat) % 900} pro Monat"]),
                       "application/pdf")
            return
        if name.startswith("scan-"):
            self._send(200, pdf_without_text(), "application/pdf")
            return
        if name.startswith("floorplan-"):
            self._text("Raum;Fläche\nWohnen;28\nSchlafen;16\nKüche;9\n",
                       content_type="text/csv; charset=utf-8")
            return
        if name.startswith("rooms-"):
            self._send(200, xlsx_bytes([["Raum", "Fläche"], ["Wohnen", "28"]]),
                       "application/vnd.openxmlformats-officedocument."
                       "spreadsheetml.sheet")
            return
        if name.startswith("notes-"):
            self._send(200, docx_bytes(["Notiz zur Wohnung", "Bezug per sofort."]),
                       "application/vnd.openxmlformats-officedocument."
                       "wordprocessingml.document")
            return
        self._not_found()

    # -- cars ----------------------------------------------------------

    def _route_cars(self, path: str, query: dict) -> None:
        if path == "/de/autos/alle-marken":
            page = int((query.get("page") or ["1"])[0] or 1)
            self._html(_cars_list(page))
            return
        if path.startswith(("/de/d/", "/fr/d/")):
            self._html(_page(f"Fahrzeug {path.rsplit('-', 1)[-1]}",
                             "<p>Occasion, 120 000 km.</p>"))
            return
        if path in ("/impressum", "/agb", "/datenschutz"):
            self._html(_page("Rechtliches", "<p>Rechtliches.</p>"))
            return
        self._not_found()

    # -- law -----------------------------------------------------------

    def _route_law(self, path: str, query: dict) -> None:
        if path == "/search":
            page = int((query.get("currentPage") or ["1"])[0] or 1)
            self._html(_law_search(page))
            return
        if path.startswith("/eli/"):
            parts = [piece for piece in path.split("/") if piece]
            if len(parts) == 5:
                _, kind, _year, number, language = parts
                self._html(_law_detail(kind, int(number), language))
                return
            self._not_found()
            return
        if path.startswith("/files/"):
            self._send(200, pdf_bytes(["Verordnung", "Artikel 1"]), "application/pdf")
            return
        if path == "/impressum":
            self._html(_page("Impressum", "<p>Rechtliches.</p>"))
            return
        self._not_found()

    # -- broken --------------------------------------------------------

    def _route_broken(self, path: str, query: dict) -> None:
        # Reachable, and never reached: robots.txt answers 503, so the crawl
        # refuses the whole host. A test that finds this page in the access
        # log has found a bug.
        self._html(_page("Liste", '<ul><li><a href="/a/1">Eins</a></li></ul>'))


class StandinSite:
    """The four hosts. Start it, use the base URLs, stop it.

    >>> with StandinSite() as site:
    ...     import httpx
    ...     httpx.get(site.estate + "/robots.txt").text.splitlines()[1]
    'Disallow: /*-srp-new/*'
    """

    names = ("estate", "cars", "law", "broken")

    def __init__(self, host: str = "127.0.0.1") -> None:
        self.state = SiteState()
        self._servers: dict[str, ThreadingHTTPServer] = {}
        self._threads: list[threading.Thread] = []
        self.urls: dict[str, str] = {}
        for name in self.names:
            handler = type(f"{name.title()}Handler", (_Handler,),
                           {"site_name": name, "state": self.state})
            server = ThreadingHTTPServer((host, 0), handler)
            server.daemon_threads = True
            self._servers[name] = server
            self.urls[name] = f"http://{host}:{server.server_address[1]}"

    # -- lifecycle -----------------------------------------------------

    def start(self) -> "StandinSite":
        for server in self._servers.values():
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self._threads.append(thread)
        return self

    def stop(self) -> None:
        for server in self._servers.values():
            server.shutdown()
            server.server_close()

    def __enter__(self) -> "StandinSite":
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

    # -- what the tests ask it -----------------------------------------

    @property
    def estate(self) -> str:
        return self.urls["estate"]

    @property
    def cars(self) -> str:
        return self.urls["cars"]

    @property
    def law(self) -> str:
        return self.urls["law"]

    @property
    def broken(self) -> str:
        return self.urls["broken"]

    @property
    def estate_list(self) -> str:
        return self.estate + "/rent/apartment/city-zurich/matching-list"

    @property
    def cars_list(self) -> str:
        return self.cars + "/de/autos/alle-marken"

    @property
    def law_search(self) -> str:
        return self.law + "/search?query=umwelt"

    def paths(self, host: str) -> list[str]:
        """Every path requested of one host, in order - the access log."""
        return self.state.paths(host)

    def fetched(self, host: str, needle: str) -> bool:
        return self.state.fetched(host, needle)

    def count(self, host: str, needle: str) -> int:
        return self.state.count(host, needle)

    def bytes_sent(self, path_suffix: str) -> int:
        with self.state.lock:
            for path, written in self.state.bytes_sent.items():
                if path.endswith(path_suffix):
                    return written
        return 0

    def change(self) -> int:
        """Change the content of every detail page, keeping the addresses.

        This is what a site does between two runs: same address, a corrected
        sentence. Whether that leads to a second submission is the source's
        decision (`bool_resubmit_on_change`), and the test that checks it
        needs exactly this.
        """
        with self.state.lock:
            self.state.version += 1
            return self.state.version

    def reset_log(self) -> None:
        with self.state.lock:
            self.state.requests.clear()
            self.state.bytes_sent.clear()


@pytest.fixture(scope="session")
def standin_site():
    """The four hosts, once per test session.

    Session-scoped: starting four servers costs nothing, but a fresh port per
    test would make the access log useless across a scenario that runs a crawl
    twice. Tests that look at the log call `site.reset_log()` first.
    """
    site = StandinSite().start()
    try:
        yield site
    finally:
        site.stop()


def main() -> int:
    """Run the four hosts until Ctrl-C, for looking at them by hand."""
    site = StandinSite().start()
    print("The stand-in site is running. Ctrl-C to stop.\n")
    print(f"  estate  {site.estate_list}")
    print(f"  cars    {site.cars_list}")
    print(f"  law     {site.law_search}")
    print(f"  broken  {site.broken}/list   (robots.txt answers 503)")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        site.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
