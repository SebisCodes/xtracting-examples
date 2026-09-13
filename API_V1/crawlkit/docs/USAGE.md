# crawlkit - using it

## What the services call

| Need | Function | Where it is |
|---|---|---|
| one spelling of an address | `canonical_uri(url)`, `uri_hash(url)` | `hashing.py` |
| the form of an address, and a pattern over forms | `split_form(url)`, `Pattern` | `forms.py` |
| what a link on a list page is | `classify(url, list_url, ...)` | `classify.py` |
| rules out of ticked links | `learn(list_url, links, ticked, mode, existing)` | `learn.py` |
| one link, one verdict | `decide(link, Rules.from_config(config))` | `apply.py` |
| is this address safe to fetch | `check_public(url, allow_private)` | `netguard.py` |
| what robots.txt says | `robots.py` | |
| a page, as text or as cleaned HTML | `content.py`, `fetch/` | |
| a file beside a page, as text | `files.py` | |
| a whole crawl | `run(source, sink, dry_run)` | `crawl.py` |
| the dashboard's test crawl | `execute()` | `testrun.py` |

Every pure function carries its examples as doctests; read those first. The
dashboard stores the rules `learn()` produces as a draft in
`scraper_config.*`, and the crawler reads them back through
`Rules.from_config()` - the same dictionary, both ways.

## The stand-in sites

`tests/standin/site.py` is four small web sites in one process, on ephemeral
ports: a rental portal, a car portal, a JavaScript-built listings search, and
a host whose robots.txt answers 503. They answer deterministically and keep an
access log, so a test can assert not only what was collected but what was
**never fetched**. All three suites drive them: crawlkit's own, the crawler's
flow tests, and the dashboard's Watchlist tests.

Look at them in a browser:

```sh
cd API_V1
python -m crawlkit.tests.standin.site
```

`tests/standin/fake_xtracting.py` is the API with the two habits that decide
the whole reconciliation: auto-split, and the `-01` tag suffix that comes
with it.

## Tests

```sh
cd API_V1
python -m pytest crawlkit -q
```

Run this suite first when something in either service looks wrong. A broken
link rule fails here in seconds; the same defect found through a service's
flow test takes minutes and points at the wrong folder.

## Changing a rule

A rule lives here once, so a change here changes the test crawl and the
scheduled crawl together. Add the case as a doctest or to
`tests/test_rules.py`, run the suite, then rebuild both service images (see
[SETUP.md](SETUP.md)).
