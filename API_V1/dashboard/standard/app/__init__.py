"""The dashboard - a web view of the archive in ../../database.

`__version__` is what the page footer reports. `asset_stamp()` is what busts the
browser's cache, and the two are deliberately different things.

WHY THEY ARE DIFFERENT, AND WHY IT MATTERS MORE THAN IT LOOKS.

Every template asks for its files through `{{ static('js/sources.js') }}`, and
the stamp that call appends must not be this module's `__version__` - a constant
nobody remembers to raise. Otherwise the address of every stylesheet and every
script stays `?v=0.1.0` across every deploy, and a browser that has once loaded
the page keeps its copy for ever. The page itself is generated per request and
arrives new; the script that makes it work does not. That mismatch is the worst
kind of fault to be on the receiving end of: the feature is deployed, the tests
pass, the file is in the container - and the person looking at the screen is
told it is there while their browser quietly serves them last week's.

So the stamp is computed from the static files themselves: the newest modification
time under `static/`, as hex. Change a script, and every page that asks for it asks
for a different address. Change nothing, and the address is stable, so caching still
does its job. It is read once at import - a directory walk per request would be a
disk walk per request - which is right for a container, where the files cannot change
without a restart.
"""

from pathlib import Path

__version__ = "0.1.0"

_STATIC = Path(__file__).resolve().parent / "static"


def asset_stamp() -> str:
    """A token that changes when any static file changes.

    Falls back to `__version__` when the directory cannot be walked, because a
    dashboard that will not start is worse than one that caches too well.
    """
    try:
        newest = max((p.stat().st_mtime_ns for p in _STATIC.rglob("*") if p.is_file()),
                     default=0)
    except OSError:
        return __version__
    return f"{__version__}-{newest >> 20:x}" if newest else __version__


#: Read once, at import. See the note above on why this is not per request.
ASSET_STAMP = asset_stamp()
