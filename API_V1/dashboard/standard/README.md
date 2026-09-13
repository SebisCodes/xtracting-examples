# dashboard/standard

The general-purpose web view of the archive, and the place the crawler is
configured from. It needs no API key: it reads what the collector wrote and
what the crawler found.

## What you need

- the archive running (`../../database`, started first - the dashboard joins
  the network the database creates)
- nothing else. The collector and the crawler are optional; without them the
  dashboard shows an archive nothing is writing to.

## Start it

```sh
cd API_V1/dashboard/standard
cp .env.example .env
$EDITOR .env          # ARCHIVE_STACK and POSTGRES_PASSWORD as in ../../database/.env
docker compose up -d  # ~4 GB: the image carries a browser for the test crawl of rendered pages
```

Then open <http://127.0.0.1:8088>.

The image is built from the `API_V1/` folder because it carries `crawlkit/`
next to its own code; the compose file sets that up. To build without the
browser, about 4 GB smaller:

```sh
docker compose build --build-arg DASHBOARD_BASE=docker.io/python:3.12-slim
```

Check it:

```sh
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8088/healthz
```

`200` means it is up.

**The port is bound to `127.0.0.1` and the password gate is off until you
switch it on.** Anyone who reaches the port can read every project in the
archive, change the crawler's configuration and start a test crawl. Set
`DASHBOARD_GATE_PASSWORD` in `.env`, or put a reverse proxy with your own
authentication in front of it, before letting anyone else in.

## Then

- [`docs/PURPOSE.md`](docs/PURPOSE.md) - what it shows and what it is for
- [`docs/SETUP.md`](docs/SETUP.md) - every variable in `.env`, the password
  gate, the first things to do
- [`docs/USAGE.md`](docs/USAGE.md) - the views, the switches, watched pages,
  restarting, and what to do when something is wrong
- [`docs/DESIGN.md`](docs/DESIGN.md) - the views and the decisions behind them
