# Crawler - setting it up

Fetches the pages the dashboard says to watch, decides which links on them are
documents, and submits those to Xtracting. It is the only component that holds
API keys.

**Start the database first**, and the dashboard too if you want to configure
anything.

## Three commands

```sh
cd API_V1/crawler
cp .env.example .env
$EDITOR .env          # the keys go here
docker compose up -d
```

The image is built from the `API_V1/` folder, not from this one, because it
carries `crawlkit/` beside its own code. The compose file sets the build
context, so `docker compose build` from here works; a plain `docker build .`
does not.

The Playwright base image is about 4 GB because it ships Chromium, which is
what a watched page set to *Rendered* needs. If none of your pages needs a
browser:

```sh
docker compose build --build-arg CRAWLER_BASE=docker.io/python:3.12-slim
```

Rendered pages then fail with a sentence saying exactly that.

## What to put in `.env`

Every variable `app/config.py` reads, with its default. The first block is
what a fresh installation has to set; the rest is commented in `.env.example`
and rarely needs touching.

| Variable | What it decides |
|---|---|
| `ARCHIVE_STACK` | Must be **exactly** the same as in `database/.env`. Compose reads it; the crawler itself does not. |
| `XTRACTING_API_KEYS` | **The important one.** Comma-separated, optionally `label:key`; order decides a project's default key (below). |
| `XTRACTING_PROJECT_KEYS` | Optional, and only for tidiness: keys here are probed exactly the same way. |
| `XTRACTING_API_URL` | `https://api.xtracting.io`. |
| `POSTGRES_PASSWORD` | The one the database was created with. |
| `POSTGRES_USER` / `POSTGRES_DB` | `xtracting` / `xtracting_archive`, as in `database/.env`. |
| `POSTGRES_HOST` / `POSTGRES_PORT` | `xtracting_db` and **`5432`** - the port inside the network, never the published one. `POSTGRES_PORT_INTERNAL` is read when `POSTGRES_PORT` is empty, as in the collector. |
| `DATABASE_URL` | Empty. A full connection string that replaces the five `POSTGRES_*` values. |
| `SCRAPER_DB_USER` / `SCRAPER_DB_PASSWORD` | Empty. Only with the least-privilege roles from `database/.env`; they replace `POSTGRES_USER` and `POSTGRES_PASSWORD` for this service. |
| `CRAWLER_USER_AGENT` | How the crawler introduces itself. Put a real contact address in it - a site owner who wants to complain should be able to. |
| `CRAWLER_CONCURRENCY` | How many sources are crawled at the same time. 2; never two on the same host whatever this says. |
| `CRAWLER_TICK_SECONDS` | How often the loop wakes. 5; there is no reason to change it. |
| `CRAWLER_LEASE_MINUTES` | 30. How long a claimed page stays claimed; a run stops with what it has before its lease ends. |
| `CRAWLER_FILE_MAX_MB` | The cap for one downloaded file. 25; a watched page can lower it per file type. |
| `CRAWLER_SUBMIT_INTERVAL_SECONDS` | 60. How often the queue goes out to the API; the same round puts back items left at `SENDING`. |
| `CRAWLER_MAX_BATCH_TASKS` | 1000, the API's own cap; a higher value is cut to 1000. |
| `CRAWLER_MAX_REQUEST_BYTES` | 4500000. Must stay below the gateway's 5 MB. |
| `CRAWLER_MAX_CONTENT_CHARS` | 2000000, the API's own cap per document; a higher value is cut to it. |
| `CRAWLER_MAX_ATTEMPTS` | 3. How often a document is offered before it is marked `FAILED`, and how often one batch is retried on a temporary error. |
| `REQUEST_TIMEOUT_SECONDS` | 120. One request to the API. |
| `CRAWLER_RECONCILE_MINUTES` | 15. How often submitted documents are looked up in the archive. |
| `CRAWLER_KEY_REFRESH_SECONDS` | 300, at least 30. How often each key is asked what it opens. |
| `CRAWLER_TEST_TIMEOUT_SECONDS` | 120. The cap for one `--test` run from the shell. |
| `CRAWLER_ONLY_TESTS` | `false`. `true` keeps the heartbeat and the sync and crawls nothing - a container that exists for `--test`. |
| `CRAWLER_ALLOW_PRIVATE_HOSTS` | `false`. A run refuses addresses inside your own network; `true` is for a test site on the same machine and nothing else. |
| `LOG_LEVEL` | `INFO`. |

### The keys, and why their order matters

A key belongs to exactly one project, and the crawler asks Xtracting which -
you never configure a project name anywhere. So:

```
XTRACTING_API_KEYS=patents:ab12cd34.secret,companies:ef56gh78.secret
```

The label before the `:` is yours, for telling entries apart: it appears in
the log and on the watched page, so a line about a failure names which entry
it was. The key's own name and the project it opens come from the platform -
the crawler asks every key what it is, on start and on a timer, and a key
with **Read project** enabled is also asked for the project's name and
settings, which is what the dashboard's key chooser then shows.

A watched page collects into one or more projects, and for a project where it
names no key it uses **the first key of that project in this list** that still
works and may extract. If one is switched off, collection carries on with the
next - **within the same project and never across one**, because another
project's key would file the documents in the wrong archive. A page whose
project has no working key stops, and says so.

A page can also pin a key per project. Then a key that disappears **stops**
those pages rather than moving them to a key with a different price, and the
dashboard says which page stopped and why.

### The user agent

robots.txt is matched against `CRAWLER_USER_AGENT`, and it is the only way a
site operator can reach you. Put in a URL and an address that really exist and
that you really read. A crawler that cannot be contacted gets blocked outright
instead of asked to slow down.

## Checking it worked

```sh
docker compose logs --tail 20 crawler
```

You want two lines: `crawler started: N key(s), …` and then either the pause
warning or the first tick. `0 key(s)` means `XTRACTING_API_KEYS` did not parse
- check for a stray space or a missing comma.

Then look at the dashboard: the crawler's pill at the right of the page title
turns green within a minute.

## Nothing is crawled yet

Two switches have to be on, and both start off:

1. **the crawler's own switch**, at the top of the Watchlist - it governs
   everything
2. **each watched page's own `Crawl on a schedule`** tick

Until then the crawler ticks, keeps its heartbeat and reads the configuration,
and fetches nothing. That is the intended state of a fresh installation.

To try one page without switching anything on, use **Crawl and send one
document** in the editor, or from a shell:

```sh
docker compose exec crawler python -m app.main --run-now 7
```

**That costs**: every document sent is an extraction, once per project the
page is assigned to.

## A dry run that costs nothing

```sh
docker compose exec crawler python -m app.main --test 7
```

The same fetch the dashboard's *Test this configuration* does, printed as
JSON. It writes nothing and sends nothing.
