# xtracting-examples

Tools for working with [Xtracting](https://xtracting.io): an archive for your
extraction results, two services that fill it, a dashboard that shows it, and
the library the crawling parts share. Everything runs in containers; the only
thing you have to install is a container runtime.

```sh
git clone https://github.com/SebisCodes/xtracting-examples.git
cd xtracting-examples/API_V1
```

## How the repository is laid out

The code lives under one folder per API version. `API_V1/` is the current one:

```
API_V1/
  database/     the archive - PostgreSQL with TimescaleDB and the schema
  collector/    fetches finished extractions from Xtracting into the archive
  crawler/      watches web pages and submits new documents to Xtracting
  crawlkit/     the crawl rules both the crawler and the dashboard run (a library)
  dashboard/    the web views - one folder per dashboard version
    standard/   the general-purpose dashboard
```

**`database`, `collector` and `crawler` are general purpose.** They fit the
Xtracting API as it is, whatever you extract and whatever you do with the
result. Their schema, their configuration and their behaviour are the same
for every installation, and they are meant to be taken as they are.

**Only the dashboard varies.** What people want to see of an archive depends
on what they extract, so a dashboard is written for a purpose. `standard/` is
the general-purpose one: counters, search, charts, maps, a connection graph,
and the pages that configure the crawler. A dashboard for a special case can
be built as a further folder under `API_V1/dashboard/`, or obtained from
somewhere else entirely - it needs a PostgreSQL with the archive schema and
nothing else, so a dashboard from another repository or from a vendor plugs
into the same archive.

## Where to start

Every folder has a `README.md` that takes you from a fresh clone to
`docker compose up -d`, and a `docs/` folder for what comes after:

| File | What it answers |
|---|---|
| `README.md` | what to do until the component is running |
| `docs/PURPOSE.md` | what the component is and what it is for |
| `docs/SETUP.md` | how to set it up, in detail |
| `docs/USAGE.md` | how to use it, configure it and restart it |
| `docs/DESIGN.md` | why it is built the way it is |

[`API_V1/README.md`](API_V1/README.md) says how the components fit together
and in which order to start them.

## Requirements

One of Docker, Podman or nerdctl. About 200 MB of disk for the archive to
start with; the crawler and the dashboard images are about 4 GB each because
they carry a browser, and both can be built without one.

To run the tests rather than the containers you also need Python 3.12 and, for
the crawler's rendered-mode test, Chromium.

## Licence

MIT - use it, change it, ship it inside something else. See [LICENSE](LICENSE).
