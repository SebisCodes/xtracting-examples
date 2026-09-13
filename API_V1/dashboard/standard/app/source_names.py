"""What to call a document, when the archive never gave it a name.

Measured on a real archive rather than guessed:

    sources.text_name  some rows:   src_1416664, src_1416663, …
                       other rows:  8846ebcc63ff6a49245ef85e08e776de

`text_name` need not hold a title at all - a serial id in some rows, a
checksum in others. Printing it is correct and useless: "Source:
src_1127664" tells a reader nothing about where the row came from, and a
list of twenty of them tells them nothing twenty times.

THE RULE, and it is on the VALUE and not on the field: a name that looks
like an identifier is shown NOWHERE - not as a heading, not in a footer
line, not in a tooltip. If a future archive does carry a real name, it is
shown, which is why `is_identifier` decides per row instead of the caller
deciding per table.

What is shown instead is built from what actually exists:

  * THE DOMAIN of `text_uri` - "news.example.com". The one fact a person
    scanning a list needs, and the term /diagrams/source already matches
    (app/scope.py searches the host as well as the name).
  * A TITLE DERIVED FROM THE URL'S OWN SLUG where there is one:
    ".../525665/Council-approves-new-bridge-over-the-river" becomes
    "Council approves new bridge over the river". It is DERIVED, so nothing
    around it may imply the source called itself that - the views that
    print it say "from the address" beside it.

An archive can also hold documents whose uri is not a URL at all
(`src:city-archive-foia-2006`). urlsplit gives those a scheme and a path
and no host, so they get no domain and a title from the path - which is
the right answer: the slug is all there is.

Nothing here touches a database or a request, so it is a plain module and
its test (tests/unit/test_source_names.py) is a plain unit test.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import unquote, urlsplit

#: The shapes `text_name` has ever had in this archive, and the one the
#: extraction itself writes. Anchored, and case-insensitive for the checksum:
#: a name that merely CONTAINS "src_12" is a real name that happens to
#: mention one.
#:
#: `src:` AND `ent:` ARE THE EXTRACTION'S OWN IDS. The output contract gives
#: every document an id of "src:" plus a slug and every entity one of "ent:"
#: plus a slug, and those travel into the archive as names wherever nothing
#: better was found. They are not unique across projects and they are not
#: what anything is called - a reader looking at "src:antitrust" learns
#: nothing except that we had nothing to show - so they count as identifiers
#: and the domain or the derived title is shown instead.
#: ONE SPELLING OF IT, AND EVERYTHING READS IT FROM HERE - this module, the
#: SQL expression that labels a document for a group (app/sqlbuild.py:
#: MACHINE_NAME_REGEX), and sql/03-places-view.sql, which is the same text
#: again because a materialized view cannot call Python. Two spellings is how
#: "src:pump" came to be hidden from the suggestion list and printed as the
#: title of a document in the drilldown beside it.
#:
#: The 40 and 64 hex forms are sha1 and sha256: the archive has not written
#: them, and a name of that shape is not a title whoever wrote it.
IDENTIFIER_REGEX = (r"^(src_[0-9]+|src:\S+|ent:\S+"
                    r"|[0-9a-f]{32}|[0-9a-f]{40}|[0-9a-f]{64})$")

_IDENTIFIER = re.compile(IDENTIFIER_REGEX, re.I)

#: A path segment that is an id rather than a slug: all digits, or a long
#: run of hex. A YEAR IS NOT AN ID - "…/2025/01/04/council-approves…" and
#: "src:city-archive-foia-2006" both carry one, and dropping it would throw
#: away the only date the title has. So: six digits or more, or eight hex
#: characters or more with at least one digit in them (which "archive"
#: cannot satisfy, being all letters and too short anyway).
_ID_SEGMENT = re.compile(r"^(?:\d{6,}|(?=[0-9a-fA-F]*\d)[0-9a-fA-F]{8,})$")

#: A file extension on the last segment: ".html", ".php", ".ghtml" - the
#: Brazilian archive uses that last one, which is why this is a shape and
#: not a list of endings somebody has to keep adding to. All-digit suffixes
#: are excluded: "…-part.2" is a part number, not a file type.
_EXTENSION = re.compile(r"\.(?=[a-z]*[a-z])[a-z0-9]{1,6}$", re.IGNORECASE)


def is_identifier(name: Any) -> bool:
    """True when this "name" is a machine's, not a person's."""
    return bool(_IDENTIFIER.match(str(name or "").strip()))


def host_of(uri: Any) -> str:
    """The domain of a document's URI, for the text of its link and for the
    link to that domain's own diagrams. `www.` is dropped: it is noise in a
    column that has to be scanned twenty rows at a time, and
    /diagrams/source matches the host as a substring either way."""
    text = str(uri or "").strip()
    if not text:
        return ""
    try:
        host = urlsplit(text).hostname or ""
    except ValueError:
        return ""
    host = host.lower()
    return host[4:] if host.startswith("www.") else host


def title_from_uri(uri: Any) -> str:
    """The document's own slug, read as words.

    The LAST path segment that is not an id, hyphens and underscores turned
    into spaces, the file extension dropped, the first letter raised. An
    empty string when the address carries no slug at all - a bare domain, a
    query-string-only URL - because a made-up title is worse than none.
    """
    text = str(uri or "").strip()
    if not text:
        return ""
    try:
        parts = urlsplit(text)
    except ValueError:
        return ""
    # No host: `src:city-archive-foia-2006` puts everything in `path`.
    path = parts.path or ""
    segments = [s for s in unquote(path).split("/") if s]
    if not segments:
        return ""
    # From the end, skipping ids: most sites put the slug last
    # ("…/525665/Council-approves-…"), but some put the id there instead.
    slug = ""
    for segment in reversed(segments):
        candidate = _EXTENSION.sub("", segment)
        if not candidate or _ID_SEGMENT.match(candidate):
            continue
        slug = candidate
        break
    if not slug:
        return ""
    words = [w for w in re.split(r"[-_+.]+", slug) if w]
    # A trailing id on the slug itself: "…-mondiaux-de-piste-12345678".
    while words and _ID_SEGMENT.match(words[-1]):
        words.pop()
    if not words:
        return ""
    text = " ".join(words).strip()
    if not text:
        return ""
    return text[0].upper() + text[1:]


def source_heading(name: Any, uri: Any) -> dict[str, Any]:
    """How one document is named on screen.

    Returns the three facts a view needs and one sentence-ready `text`:

        domain   the host, or "" when the address has none
        title    the document's own name, or the slug-derived stand-in
        derived  True when `title` came from the address rather than the
                 archive - the views say so, so nobody reads a guess as a
                 quotation
        text     domain and title joined the way a list shows them, and
                 never empty while there is anything at all to say
    """
    raw = str(name or "").strip()
    domain = host_of(uri)
    if raw and not is_identifier(raw):
        # A REAL NAME IS LEFT ALONE. This is the whole point of testing the
        # value rather than the field: an archive that does carry document
        # names keeps them, exactly as they are, with nothing prepended -
        # the domain is a REPLACEMENT for a missing name, not a decoration
        # on a present one.
        return {"domain": domain, "title": raw, "derived": False, "text": raw}
    title = title_from_uri(uri)
    # The domain first, because that is the half a person scanning twenty
    # rows is actually looking for; the slug behind it when there is one.
    text = " - ".join(p for p in (domain, title) if p)
    if not text:
        # Nothing to say but the address itself. Better than an id, and
        # better than a blank line where a link should be.
        text = str(uri or "").strip()
    return {"domain": domain, "title": title, "derived": bool(title), "text": text}


def source_payload(name: Any, uri: Any) -> dict[str, Any]:
    """The `source` object a view's JSON carries, for the lists that show
    where a row came from - the Dashboard's two panels and the Events view.

    `name` is what the link SAYS, and it is never the archive's identifier;
    `domain`, `title` and `derived` are there so a view can lay the two
    halves out itself and say which half it worked out. The key is called
    `name` because that is what every view reads.
    """
    head = source_heading(name, uri)
    return {"name": head["text"], "uri": str(uri or ""),
            "domain": head["domain"], "title": head["title"],
            "derived": head["derived"]}
