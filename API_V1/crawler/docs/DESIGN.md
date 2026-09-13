# The crawler

## What it is for

This is the part that feeds the archive with the pages you watch. You configure a
list page in the dashboard's **Watchlist** - a search result, a publication list, a set of
addresses - and this service reads it on a schedule, turns what it finds into
text, and hands it to Xtracting. The results come back through the collector
into the same archive the dashboard draws from.

That closes the loop:

```
    dashboard ──configures──► scraper_config.*
                                   │
                                   ▼
    ┌──────────┐  fetches   ┌──────────────┐  POST /api/v1/batch  ┌───────────┐
    │ web site │ ─────────► │   crawler    │ ───────────────────► │ Xtracting │
    └──────────┘            └──────────────┘                      └───────────┘
                                   │                                    │
                                   │ scraper.documents                  │ results
                                   │ scraper.submit_queue               ▼
                                   │                              ┌───────────┐
                                   └──── reconcile ◄───────────── │ collector │
                                         (by tag)                 └───────────┘
                                                                        │
                                                                        ▼
                                                              processed_data.*
                                                              (the archive, and
                                                               what the dashboard
                                                               shows)
```

## What it is

A small Python service in a container. It has no port and no interface of its
own: everything a person does is done in the dashboard, and the two services
talk through the database.

    scraper_config.*   what a person configures. The dashboard writes it,
                       the crawler only reads it.
    crawler.*          what the crawler does with it: schedule, leases, what it
                       has seen, what it fetched, what it submitted. The
                       crawler writes it, the dashboard only reads it.
    monitoring.*       one row per run in `scraper_runs`, and one row per thing
                       that went wrong in `crawler_errors` - which is what the
                       dashboard's Log view shows.
    processed_data.*   the archive. Read-only for both.

The crawling itself is not in this folder. It lives in `../crawlkit`, and the
dashboard runs the very same code when somebody presses "Test this
configuration". That is the point of the shared library: what the test promised
is what the crawler does, because it is the same function.

## What it does

Every `CRAWLER_TICK_SECONDS`:

1. **Heartbeat** - one row, so the dashboard can say "crawler online".
2. **Configuration sync**, but only when something changed. A page someone
   just enabled becomes due now; one whose rules changed becomes due now again;
   a deleted one loses its target row.
3. **Leases** - expired ones are freed, then what is due is claimed, at most
   `CRAWLER_CONCURRENCY` at a time and never two on the same host.
4. **Submit**, every `CRAWLER_SUBMIT_INTERVAL_SECONDS`.
5. **Reconcile**, every `CRAWLER_RECONCILE_MINUTES`: which submitted documents
   have arrived in the archive.

A crawl of one watched page is:

    robots.txt for the list page       forbidden -> the run is SKIPPED
    fetch the list page                4xx/5xx/challenge -> the run is BLOCKED
    collect the links, follow pages    four stop rules, see below
    decide about every link            the patterns from the picker
    ask what is new                    seen_urls, then the archive
    fetch those, prepare the text      plain text or cleaned HTML
    files on each page (optional)      pdf, docx, xlsx, csv, txt as text
    one row in monitoring.scraper_runs

## Setting it up

The archive database has to be running first, with the crawler tables in it -
they come from `database/init/03-scraper.sql`, which a fresh volume applies by
itself.

```sh
cd ../database
cp .env.example .env        # set POSTGRES_PASSWORD
docker compose up -d

cd ../crawler
cp .env.example .env        # put your API keys in it, same POSTGRES_PASSWORD
docker compose up -d
```

Podman, nerdctl and the other Compose-compatible runtimes work the same way -
see the [API_V1 README](../../README.md#container-runtimes).

Two settings need thought.

**The keys**, and here the label matters more than it does in the collector:

```
XTRACTING_API_KEYS=flats:ab12cd34.xxxx,law:ef56gh78.yyyy
```

Every watched page in the dashboard points at one of these labels, and that is how
it knows which project its documents end up in. A watched page whose label is not
in this list is not an error - the crawler writes "unknown key label" on it, the
dashboard shows it, and the documents wait in the queue until the key appears.

**Who you are:**

```
CRAWLER_USER_AGENT=xtracting-crawler/1.0 (+https://example.com/crawler; crawler@example.com)
```

Put a name and an address someone can reach you at. An operator who wants you
to slow down will write to you; one who cannot reach you will block you.

**The image is about 4 GB**, because it carries Chromium - that is what pages
set to "Rendered" need. If none of yours does:

```sh
docker compose build --build-arg CRAWLER_BASE=docker.io/python:3.12-slim
```

Rendered pages then fail with a sentence saying exactly that, instead of
failing mysteriously. The pip pin in `../crawlkit/requirements.txt` and the tag
of the Playwright base image must name the same version - a newer pip package
fails at the first rendered fetch with "Executable doesn't exist", and that
message never mentions versions.

## Watching it

```sh
docker compose logs -f
```

One line per watched page per run. Nothing for six hours is normal - that is the
default interval.

The run table is the honest view:

```sql
SELECT date_added, text_name, text_status, integer_pages, integer_links,
       integer_accepted, integer_new, integer_submitted, text_message
  FROM monitoring.scraper_runs
 WHERE date_added > now() - interval '1 day'
 ORDER BY date_added DESC;
```

`text_status` is one of `OK`, `SKIPPED` (robots.txt forbids the list page),
`BLOCKED` (the site answered 4xx, 5xx or a browser check), `BACKPRESSURE` (the
API asked for a pause), `ERROR`, `TEST`.

What is on its way, and what has arrived:

```sql
SELECT text_status, count(*) FROM scraper.submit_queue GROUP BY 1;

SELECT text_file_type, text_status, count(*)
  FROM scraper.files GROUP BY 1, 2 ORDER BY 1, 2;
```

And what went wrong, in sentences rather than status words:

```sql
SELECT date_added, text_name, text_kind, text_severity, text_message
  FROM monitoring.scraper_errors
 WHERE text_severity <> 'info'
 ORDER BY date_added DESC LIMIT 20;
```

That is the table behind the dashboard's **Log** page, which is the same list
with filters and a link per watched page. A run row says a crawl was `SKIPPED`; the
error row says *which rule* forbade *which address*, and what you could do
about it. The two are written on the same transaction - but the log write sits
in a savepoint of its own and never raises, because a crawl that ran for four
minutes must not be lost to a full disk in the log table.

To ask a watched page what it would collect, without waiting for its schedule and
without writing anything:

```sh
docker compose exec crawler python -m app.main --test 7    # 7 = the watched page's id
```

Same dry run the dashboard's "Test this configuration" does - the same
function, printed as JSON on stdout. It is for debugging from a shell; the
button is the ordinary way.

To really crawl one page and really submit one document - the question the dry
run cannot answer, which is whether a document from this page is accepted,
extracted and archived:

```sh
docker compose exec crawler python -m app.main --run-now 7          # one document
docker compose exec crawler python -m app.main --run-now 7 --wanted 3
docker compose exec crawler python -m app.main --run-now 7 --project clx1a2b3
```

**This costs**: every document sent is an extraction, once per project the page
is assigned to. It writes the same `scraper_config.manual_runs` row the
dashboard's *Crawl and send one document* button writes and lets the same code
carry it out, so the two cannot drift apart. It runs **even while
`scraper.paused` is on**: the switch means "do not crawl on a schedule", and a
run somebody asked for by hand while watching is not the crawler running.

## Decisions worth knowing

**The address is the dedupe anchor, not the content.** A page with a date, a
visitor counter or a rotating advert has a different content hash on every
fetch. Deduplicating on the content would submit - and pay for - the same page
on every run, in a loop that does not stop by itself and that looks like
diligence from the outside. `bool_resubmit_on_change` turns the content check
back on per page; the editor sets it for *exact* mode, where a change is the
whole point.

**A robots.txt verdict is a feature, not a technicality.** 2xx: the rules apply.
4xx: no restriction - "not there" does not mean "forbidden", and a 403 from a
protection layer is a 4xx too (the dashboard shows that the permission was
inferred rather than read). 5xx, a network error or a timeout: **everything is
forbidden**, because the answer is not "there are no rules" but "I cannot say
which rules apply". `Crawl-delay` is honoured up to sixty seconds; above that
the page is reported as too slow to crawl rather than crawled for hours.

**4xx and 5xx from the site itself are different things.** A 404 is about the
address and the banner says so; a 500 is the site's own problem and there is
nothing to change here; a 403 with a browser check in the body is a site that
wants a browser, and Rendered mode sometimes gets through. A 429 or 503 is not
a fault at all - it is a request for room, the run stops, and the next one waits
longer.

**Files are sent as text.** Xtracting takes text and nothing else, so a PDF is
not attached anywhere: its text layer is extracted and submitted like a page.
What has no text layer - a scan, a drawing, an empty sheet - is not submitted,
because an empty task is billed and yields nothing. Every file that is not sent
still leaves a row in `scraper.files` with its reason: `TOO_LARGE`,
`WRONG_TYPE`, `NO_TEXT`, `ROBOTS`, `NOT_SELECTED`, `ERROR`. "Why is that PDF not
in the archive?" is the question that table exists to answer.

**Pagination has four stop rules**, and each of them has been needed: no next
link (the ordinary end), the page cap (for a site that answers 200 to any page
number), an address already read in this run (the last page often links back to
the first), and a next page that brings no new links (a site that ignores the
paging parameter looks like an endless list). The next link itself is removed
from the harvest - it is an ordinary `<a href>`, and left in it would be
fetched as a document and counted as new on every run.

**The same code runs the dashboard's tests.** `crawlkit.crawl.run()` is the only
crawl in this repository. The crawler passes a sink that writes rows; the
dashboard passes one that keeps nothing and calls it with `dry_run=True`. Two
implementations would drift, and the drift would show up as pages the preview
promised and the crawler never fetched - which nobody would look for here.

**The tag has no hyphen.** A document travels as `xs_<source id>_<12 hex>`, and
on an auto-split the platform appends `-01`, `-02`. A hyphen inside the tag
would make that suffix unparseable and `reconcile` could no longer tell which
archived task belongs to which submission. The database enforces the rule with
a CHECK.

## Politeness, and the legal part

Defaults are deliberately slow: **every six hours** per watched page, **six seconds**
between requests, at most twenty-five new addresses per run. The site's operator
notices the second number, not the first.

**robots.txt permission is not a licence.** That a file does not forbid
something says nothing about copyright, terms of use, personal data or the law
where you and the site are. This service makes it easy to fetch a page; it does
not make it lawful to keep or process what is on it.

**You are responsible for the sites you crawl.** Every request carries your
`CRAWLER_USER_AGENT`, comes from your address, and was configured by somebody
in your organisation. Nobody here knows which sites you added, and no default
can decide for you whether a site may be read by a program. Read the terms of
the sites you watch, keep the intervals slow, and take a page out when its
operator asks - the address in the user agent exists so that they can ask.

Ignoring robots.txt is therefore a decision that has to stand there with its
reason:

```sql
CONSTRAINT source_override_needs_reason CHECK (
    bool_respect_robots OR btrim(text_override_reason) <> '')
```

A CHECK rather than a form rule, so that nothing that writes to this table can
skip it. Write the actual reason - "written permission from the operator,
ticket 4711" - not "needed".

`dashboard.settings` carries `scraper.paused`. Switching it on stops all
scheduled crawling and submitting on the next tick: the emergency brake for the
person who has a browser open and not a shell. A **manual run** is the one thing
it does not stop, and deliberately: it is one page, at one moment, on one
person's press, with the price on the button - which is not what the switch was
put there to prevent.

**What comes back is a report, not advice.** Pages submitted from here are
extracted into the archive's vocabulary, and where a source talks about markets
that becomes **Market Insights**: an outlook and a sentiment describing *what
that source reported*. It is not a recommendation, it is not a signal, and
nothing in this repository turns it into one. A crawl over a hundred property
listings tells you what a hundred listings said, which is a different sentence
from what anything is worth.

## Tests

```sh
pip install -r requirements.txt -r requirements-dev.txt
python -m playwright install chromium      # for the one rendered-mode test
```

A throw-away archive to run them against - `database/.env.test` is a second
Compose project with its own volume and its own port, so a test run cannot
reach the archive you keep:

```sh
cd ../database
docker compose --env-file .env.test up -d
export DATABASE_URL=postgresql://xtracting:test_password@127.0.0.1:55432/xtracting_archive
```

Then, back here:

```sh
cd ../crawler

# Unit tests and doctests - no database, no network. This is what `pytest`
# with no arguments runs; the doctests in app/ specify backoff, batching and
# the tag.
python -m pytest -q

# Everything, against that archive, the stand-in site and the fake API
python -m pytest tests -q

# The shared crawl library, first when something looks wrong: a broken link
# rule fails here in seconds instead of in a flow test minutes later
cd .. && python -m pytest crawlkit -q
```

Without `DATABASE_URL` the flow tests skip themselves rather than fail - the
unit suite is the one that runs anywhere.

One run at a time against one database. The flow tests here and the dashboard's
share the archive and clean up by prefix; two runs on the same database delete
each other's rows halfway through, and the result is a page of failures in
files nobody touched. Give a second run its own database.

The flow tests run against four web sites that live in this repository
(`crawlkit/tests/standin/site.py`) - a rental portal, a car portal, a
JavaScript-built legislation search and a host whose robots.txt answers 503.
They are modelled on the URL shapes of the real ones, they answer
deterministically, and they keep an access log, so a test can assert not only
what was collected but what was **never fetched**. You can look at them:

```sh
cd .. && python -m crawlkit.tests.standin.site
```

Xtracting is faked too (`crawlkit/tests/standin/fake_xtracting.py`), with the
two habits of the real platform that decide the whole reconciliation:
auto-split, and the tag suffix that comes with it.

Every row the flow tests write is prefixed `_crawlertest` and removed
afterwards, so they are safe against an archive that holds real data - but
point `DATABASE_URL` at a throw-away one anyway.

## When something is wrong

| What you see | What it means |
|---|---|
| `SKIPPED` on every run | robots.txt forbids the list page. The pill in the dashboard shows the rule. Either the address is wrong, or that site does not want to be read by a program |
| `robots.txt of ... could not be read` | 5xx or a network error. Nothing is fetched from that host until it answers again - "I cannot say which rules apply" is not permission |
| `BLOCKED` with a browser check | the site answered with a bot check instead of a page. Rendered mode gets through some of them |
| `0 links found` | with `http`: the list is probably built by JavaScript - switch the watched page to Rendered. With `playwright`: check the address, or give a selector to wait for |
| the queue fills up with `PENDING` | the API asked for room (402: no balance, 429: too many open tasks). Nothing is lost; look for `BACKPRESSURE` in `monitoring.scraper_runs` |
| `FAILED` with `the API key was rejected` | the key in `.env` is wrong or revoked. The affected watched pages also carry it in `scraper.targets.text_key_status` |
| `SENT` that never becomes `ARCHIVED` | the collector is not running, or its key is for another project. `reconcile` matches by tag; check `processed_data.tasks.text_tag` |
| `Executable doesn't exist` on a rendered page | the image was built on the slim base, or the playwright pin and the base image are different versions |
| `network ...-archive declared as external, but could not be found` | either the database stack is not running - start `../database` first - or `ARCHIVE_STACK` here does not match the one in `../database/.env` |
| nothing at all for six hours | normal. That is the default interval |
