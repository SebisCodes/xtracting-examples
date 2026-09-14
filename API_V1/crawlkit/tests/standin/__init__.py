"""The offline stand-in site the crawl tests run against.

Four hosts - a rental portal, a car portal, a legislation search and one
whose robots.txt is broken - with the URL shapes the rules in `crawlkit` are
built for. They run on ephemeral ports on 127.0.0.1, answer deterministically, and keep an access
log, so a test can assert not only what was collected but what was never
fetched.

`StandinSite` and the `standin_site` fixture are reachable from here as well as
from `crawlkit.tests.standin.site`, but they are fetched on first use rather
than imported at the top. Importing the module here would make
`python -m crawlkit.tests.standin.site` warn that the module was already in
`sys.modules` - and that command is how a person looks at the pages by hand.
"""


def __getattr__(name: str):
    if name in ("StandinSite", "standin_site", "FakeXtracting"):
        from crawlkit.tests.standin import fake_xtracting, site
        return getattr(site, name, None) or getattr(fake_xtracting, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["FakeXtracting", "StandinSite", "standin_site"]
