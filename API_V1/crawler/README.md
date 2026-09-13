# crawler

Fetches the pages the dashboard says to watch, decides which links on them are
documents, and submits those to Xtracting. It is the only component that
holds API keys and the only one that submits.

It has no interface of its own: watched pages are configured in the
dashboard, and the two talk through the database.

## What you need

- the archive running (`../database`, started first)
- the dashboard, if you want to configure anything (`../dashboard/standard`)
- at least one Xtracting API key that may extract
- a real contact address to put in the user agent

## Start it

```sh
cd API_V1/crawler
cp .env.example .env
$EDITOR .env          # ARCHIVE_STACK and POSTGRES_PASSWORD as in ../database/.env,
                      # XTRACTING_API_KEYS, and CRAWLER_USER_AGENT with your contact
docker compose up -d  # ~4 GB: the image carries a browser for rendered pages
```

The image is built from the `API_V1/` folder because it carries `crawlkit/`
next to its own code; the compose file sets that up. To build without the
browser, about 4 GB smaller:

```sh
docker compose build --build-arg CRAWLER_BASE=docker.io/python:3.12-slim
```

Check it:

```sh
docker compose logs --tail 20 crawler
```

You want `crawler started: N key(s), …` and then either the pause warning or
the first tick. Nothing is crawled until the switch at the top of the
dashboard's Watchlist is on and at least one watched page is enabled - that is
the intended state of a fresh installation.

## Then

- [`docs/PURPOSE.md`](docs/PURPOSE.md) - what it does and why it exists
- [`docs/SETUP.md`](docs/SETUP.md) - every variable in `.env`, the keys and
  why their order matters, dry runs
- [`docs/USAGE.md`](docs/USAGE.md) - what one tick does, the dedupe gates,
  robots.txt, step logging, restarting, and what to do when something is wrong
- [`docs/DESIGN.md`](docs/DESIGN.md) - the crawl rules in detail and why they
  are the way they are
