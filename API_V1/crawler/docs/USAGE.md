# Crawler - using it

## Restarting, stopping, upgrading

```sh
cd API_V1/crawler
docker compose logs -f crawler        # watch it
docker compose restart                # the same container, the same settings
docker compose down && docker compose up -d     # after a change to .env
docker compose build && docker compose up -d    # after a change to the code
docker compose stop                   # stopped until the next up
```

Compose reads `.env` when a container is **created**: `restart` keeps the old
values, `down` and `up -d` pick up the new ones. A changed key or user agent
needs `down` and `up -d`.

The switch in the dashboard is the brake - it stops scheduled crawling and
submitting on the next tick, and nothing in flight is killed. `docker compose
stop crawler` is the harder version; the schedule lives in the database, so
nothing fires early when it comes back.

A crawl in flight at a restart is lost and re-run; a document already queued
is submitted by the new process; a batch already sent is reconciled by it.
There is nothing to drain.

## What one tick does

Every five seconds, in this order:

1. writes its heartbeat (at most once a minute - the reader only asks whether
   the row is younger than three minutes)
2. **carries out one manual run if one is waiting** - before the pause is
   checked, because a run somebody just asked for and is watching is not the
   crawler running
3. checks the pause switch, and stops here if it is on
4. refreshes the key registry on a timer: asks Xtracting what each key opens,
   and writes the answer where the dashboard reads it
5. re-reads the configuration if anything changed
6. claims whatever is due and crawls it
7. submits what is queued, and reconciles what came back

## Configuring what it crawls

Everything about a watched page is configured in the dashboard's Watchlist:
the address, which projects it collects into and with which key, how the page
is read (HTTP or Rendered), which links count as documents, which files to
send, how often and how politely. The crawler notices a saved change on its
next tick.

Changing a page's rules from SQL is possible but has one rule: move
`date_updated` with it, because the crawler re-reads a source only when that
column moves.

## The two dedupe gates

An address is fetched when **both** say it is new:

- `scraper.seen_urls` - what this crawler has fetched, **per project**
- `processed_data.tasks` - what has ever been extracted, by anyone, including
  by hand

Both are about the **address**, never the content: a page with a date or a
visitor counter in its footer has a different hash on every fetch, and
deduplicating on content would submit it - and bill it - on every run. A loop
that does not stop by itself and looks like diligence from the outside.

`Send a document again when its text changes` turns the content check back on
for one page. In *Exact addresses* mode it is on by default, because there a
change **is** the news.

## A submission counts only until it is confirmed

The crawler writes an address into `seen_urls` the moment it **queues** the
document - before anything has come back. That is a promise, not a fact, so
a submission is provisional:

| | |
|---|---|
| `date_submitted` | when the crawler handed the address to the platform |
| `date_confirmed` | when the document really turned up in `processed_data.tasks` |

The crawler's own reconcile pass sets the second one. Until then the address
counts as collected for **two hours**; after that, with nothing back, it is
fetched and submitted again. A page the crawler fetched and deliberately did
*not* send - unchanged, or a duplicate - has no clock at all and stays
collected.

Without this, a collector that is down for an afternoon would not merely
delay the archive - it would lose that afternoon's pages, because the crawler
would have written them off as collected and never offer them again.

**What that costs, said plainly:** an extraction that takes longer than two
hours is submitted a second time and paid for twice, because the second gate
can only recognise it once the first result has landed. A page collected
twice is money; a page collected never is the thing the archive is for. The
window is one number - `SUBMISSION_GRACE` in `crawler/app/store.py` - and
raising it is the answer if a platform is slower.

## What it does about robots.txt

It reads it, honours it, and refuses to start a scheduled crawl of a list
page that robots.txt forbids. To crawl one anyway, untick *Follow this site's
robots.txt* and write down who allowed it and when - the database refuses the
row without that sentence.

Permission from robots.txt is not a licence. It says what a program may
fetch, not what may be done with the text. Terms of use, copyright and the
law where you and the server are, are separate questions, and they are yours.

## Step logging

Off by default. Switch it on in the dashboard and, **on the next tick**, the
crawler starts writing a row per step into `monitoring.service_log`, which the
Log view shows under **Crawler**. Rows are dropped after 24 hours.

It answers "what is it doing right now". For "what went wrong", the Problems
tab is already there and is always written.

## From a shell

```sh
docker compose exec crawler python -m app.main --test 7            # dry run, free
docker compose exec crawler python -m app.main --run-now 7         # real, one document
docker compose exec crawler python -m app.main --run-now 7 --wanted 3
docker compose exec crawler python -m app.main --run-now 7 --project clx1a2b3
```

`7` is the id of the watched page, shown in the editor's address. `--run-now`
writes the same request row the dashboard's button writes and lets the same
code carry it out, so the two cannot drift apart. It runs **even while the
pause is on**.

## Being a good guest

- **Six seconds between two requests** by default. The site's own
  `Crawl-delay` wins when it asks for more.
- **At most 10 list pages and 25 new documents per run**, so a first crawl
  of a large site is spread over several runs instead of arriving as a flood.
- One test at a time across the whole dashboard, so a site never sees two of
  us at once.
- Politeness is per host, not per watched page: two pages on one site queue
  behind each other.

## When something is wrong

### It is running and fetching nothing

Almost always one of three, in this order:

1. **The switch is off.** The log says so once: `paused: dashboard.settings
   has scraper.paused switched on`. Turn it on at the top of the Watchlist.
2. **No watched page is enabled.** The global switch governs; each page has
   its own `Crawl on a schedule` tick, and a saved page starts off.
3. **Nothing is due yet.** A page with a six-hour interval is crawled every
   six hours. `scraper.targets.date_next_run` says when.

```sh
cd ../database
docker compose exec xtracting_db psql -U xtracting -d xtracting_archive -c "
SELECT s.text_name, s.bool_enabled, t.date_next_run, t.text_last_error
  FROM scraper_config.sources s
  LEFT JOIN scraper.targets t ON t.bigint_fk_source = s.bigint_id
 ORDER BY t.date_next_run NULLS FIRST LIMIT 10;"
```

### `missing: scraper.targets`

It reached the database but the archive is incomplete - the init scripts did
not finish. See [the database's USAGE.md](../../database/docs/USAGE.md#the-schema-did-not-finish);
the remedy is to apply `03-scraper.sql` again, not to recreate the volume.

### `0 key(s)`

`XTRACTING_API_KEYS` did not parse. It is comma-separated and optionally
`label:key`; a stray space around a comma is fine, a missing comma is not.
Nothing can be submitted until at least one key answers.

### A key stopped working

The crawler asks Xtracting what each key opens, so it can tell three states
apart:

| What it saw | What it means |
|---|---|
| 200, active | alive; the answer says which project and what the key may do |
| 200, not active | real, but switched off or expired at Xtracting |
| 401 | not a key of ours: wrong, revoked, or a typo |

A key that stops authenticating is removed from the registry, and any page
that pinned it gets a `KEY_INVALID` row in the Problems log naming the page. A
page that named no key moves to the project's next key.

**A project whose keys are all gone stops**, and that is deliberate:
borrowing another project's key would file its documents in the wrong
archive, which is worse than collecting nothing.

### Documents sit at PENDING

```sh
docker compose exec xtracting_db psql -U xtracting -d xtracting_archive -c "
SELECT text_status, count(*), min(date_added) FROM scraper.submit_queue GROUP BY 1;"
```

- **PENDING and not moving** - no live key for that row's project. The
  Problems log says which page and why.
- **SENDING and not moving** - a sender died mid-batch. Rows older than 30
  minutes are put back automatically on the next tick.
- **SENT and never ARCHIVED** - the crawler's half worked and the collector's
  has not. See the [collector's USAGE.md](../../collector/docs/USAGE.md#documents-are-sent-but-never-archived).

### A site answers 403 to us and 200 to a browser

Some sites refuse an unfamiliar user agent. Put a real one, with a contact
address, in `CRAWLER_USER_AGENT`. If it still refuses, that is an answer: it
does not want to be crawled by a program, and the honest response is to ask
rather than to disguise the crawler.

### The test finds no links

The list is probably built by JavaScript. In step 1 of the editor choose
**Rendered** instead of HTTP, and if it still comes back short give it an
element to wait for under *Waiting rules*. Rendered mode needs the image with
the browser in it.

### A manual run says nothing was sent

The panel gives the reason, and none of them is a fault:

- every address on the page has already been collected **for that project**
- the addresses are already in the archive under that project
- no link on the page counts as a document yet - open *Choose the links*
- robots.txt forbids the page

### The same page is collected twice

That is the design when the page is assigned to two projects: fetched once,
submitted once per project, because a project decides what is extracted and
two projects cannot share one extraction. The editor says so before the
second project is ticked. If it was not wanted, take one project off the
page.
