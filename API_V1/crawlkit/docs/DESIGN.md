# crawlkit - design

What the library holds, how the rules fit together, and how the services
import it.

## What it holds

The **pure half** has no I/O and no dependencies beyond the standard library.
Every function carries its examples as doctests, and those doctests are the
specification:

| Module | What it does |
|---|---|
| `hashing.py` | `canonical_uri()`, `uri_hash()`, `sha256_text()`. **The only place a URL is normalised.** The file header says why nothing else may touch a URL. |
| `forms.py` | The *form* of an address: path pieces become `#` (all digits), `*` (contains a digit or over 24 characters), `{lang}` (a language code) or the literal; query keys stay, values go, paging keys (`page`, `ep`, `currentPage` ...) are set aside. `Pattern` is a form, optionally *pinned* at a position (`/eli/oc/#/#/de`, `/eli/(cc|oc)/#/#/de`). `to_json()`/`from_json()` is how it lives in `scraper_config.source_patterns.json_form`; `label()` is what the UI shows; `to_regex()` is a rendering for the eye and for `psql`; `matches()` evaluates the tuple, never the regex. |
| `classify.py` | What a link on a list page is before any rule applies: `list_self`, `pagination`, `legal`, `asset`, `external`, `file`, `candidate`. Also the "next page" label words. |
| `legal.py` | The short list of path words that are never a hit: imprint, terms, privacy, cookie policy, contact, login ... |
| `learn.py` | `learn(list_url, links, ticked, mode, existing)` turns the ticked and the un-ticked links of the picker into patterns, exact rejects and a paging key, with a one-sentence explanation and a list of conflicts for the UI to ask about. Pure: same input, same `LearnResult`; stores nothing. |
| `apply.py` | `decide(link, rules)` - one link, one verdict, in a fixed order: exact reject, exact monitor, reject pattern, legal, asset, external, file, then the mode. `Rules.from_config()` reads the same dictionary the dashboard stores as a draft and the crawler reads back. |
| `netguard.py` | `check_public(url, allow_private)` - the SSRF guard. Only `http(s)`, host resolved, every IP must be public; returns the IPs so the caller can pin them. `DASHBOARD_ALLOW_PRIVATE_HOSTS=true` is the only way past it (the test stand-in site lives on loopback). |

The **I/O half** builds on the pure half and needs the packages listed in
`requirements.txt`:

| Module | What it does |
|---|---|
| `robots.py` | The robots.txt verdict, per host and cached. `protego`, because `urllib.robotparser` matches prefixes only and would allow what the real sites forbid. 2xx: the rules apply. 4xx: no restriction, noted as *inferred* rather than read. 5xx, network error, timeout: **everything forbidden** - the answer is not "there are no rules" but "I cannot say which rules apply". `Crawl-delay` honoured up to 60 s; above that the watched page is reported as too slow to crawl. |
| `fetch/` | Two engines behind one interface: `httpx_engine` (the default) and `playwright_engine` (Rendered). The rendered one says in one sentence when the image was built without a browser, instead of failing with "Executable doesn't exist". |
| `throttle.py` | One host at a time, and a pause after each visit. The brake belongs to the **host**, not to the watched page: two watched pages on one site are two configuration rows and one server on the other side. |
| `content.py` | What is actually submitted: boilerplate-stripped text (`plain`) or cleaned HTML (`html` - `script/style/noscript/template/svg/iframe` dropped, attributes trimmed). The hash is taken of exactly that, which is what lets `scraper.documents` be reconciled against `processed_data.sources.text_content_hash`. |
| `files.py` | Files on a subpage as text: pdf (`pypdf`), docx, xlsx, csv/txt/md. Magic bytes rather than the extension, a size cap that aborts mid-stream, and a reason recorded for everything not sent. |
| `crawl.py` | `run(source, sink, dry_run)` - the whole crawl: robots, list page, links, pagination with its four stop rules, the decision per link, subpages, files. The sink is the only difference between a scheduled crawl and a preview. |
| `testrun.py` | `execute()` - a `dry_run` crawl built from a draft configuration, returning the JSON the dashboard shows and stores. At most three list pages and a few sampled subpages, because the number it exists for is the character count of a typical page in the chosen format: HTML costs three to ten times as much as plain text, and that is worth knowing before saving rather than after the first invoice. |

`requirements.txt` is where the packages of the I/O half are pinned - once, for
both services, because two pin lists would eventually be two versions of the
code that decides what a link is. **The `playwright` pin and the Playwright
base-image tags in `dashboard/standard/Dockerfile` and `crawler/Dockerfile` must name
the same version**: a pip package newer than the browsers in the image fails at
the first rendered fetch with "Executable doesn't exist", a message that never
mentions versions. Raise one, raise the other two.

## How the rules fit together

```
picker ticks  ──►  learn()  ──►  patterns + exact URLs  ──►  saved as configuration
                                                                    │
list page links ──►  classify()  ──►  decide(link, Rules.from_config(config))
                                          │
                              accepted / rejected by <label>
```

The observation everything rests on: hits on a list page occur many times and
look alike; the imprint occurs once. `learn()` does not need to know what a
hit looks like - it needs the form the ticked links share, and the un-ticked
links tell it where that form is too wide. When a form cannot be separated
from an un-ticked link (`/rent/1` ticked, `/rent/2` not - ids are never
pinned), the un-ticked address becomes an exact reject, the pattern is flagged
for review and the UI asks whether that one address was meant.

## How the services import it

The `API_V1/` folder is the import root: `import crawlkit` works when that
folder is on `sys.path`. Each service's `pytest.ini` adds it (`pythonpath = .
..` for the crawler, `. ../..` for the dashboard), and each `Dockerfile` is
built with `API_V1/` as the context and copies `crawlkit/` next to its own
`app/`.

Both service `requirements.txt` files include this folder's:

```
-r ../crawlkit/requirements.txt          # crawler
-r ../../crawlkit/requirements.txt       # dashboard/standard
```

## The stand-in sites

`tests/standin/site.py` is four small web sites in one process, on ephemeral
ports: a rental portal, a car portal, a JavaScript-built legislation search,
and a host whose robots.txt answers 503. They are modelled on the URL shapes of
the real ones - the filtered list pages a `Disallow` covers, the `/de` and
`/fr` twins, the detail pages with a PDF beside them - they answer
deterministically, and they keep an access log, so a test can assert not only
what was collected but what was **never fetched**.

They belong to crawlkit rather than to one of the services because all three
suites drive them: crawlkit's own, the crawler's flow tests, and the
dashboard's Watchlist tests. Look at them in a browser:

```sh
python -m crawlkit.tests.standin.site
```

`tests/standin/fake_xtracting.py` is the API with the two habits that decide
the whole reconciliation: auto-split, and the `-01` tag suffix that comes with
it.

## Tests

```sh
cd API_V1 && python -m pytest crawlkit -q
```

Doctests included (`pytest.ini` sets `--doctest-modules`): in the pure half the
doctests **are** the specification, and they run without a database, a network
or a browser.

`tests/test_rules.py` carries every worked example as a named scenario - a
rental portal, a listings portal, a car portal with `/fr` twins, a legislation
site with `oc`/`cc` widening - plus the regex-versus-tuple equivalence on
generated URLs and a timing test: `learn()` on 2000 links stays under 50 ms,
because the picker calls it on every click. `tests/test_robots.py` drives a
fake transport through every verdict, `tests/test_crawl_rules.py` runs the
pagination rules against a real `http.server`, and `tests/test_netguard.py`
uses a dictionary as the resolver, so nothing here touches DNS.

Run this suite first when something in either service looks wrong. A broken
link rule fails here in seconds; the same defect found through a service's
flow test takes minutes and points at the wrong folder.
