# dashboard

The web views of the archive. This folder holds one dashboard per subfolder,
because the dashboard is the one component that varies: `database`,
`collector` and `crawler` are the same for every installation, while what
people want to see of an archive depends on what they extract.

```
dashboard/
  standard/     the general-purpose dashboard
```

## `standard/`

Counters and an activity feed, a search over terms and over places with a
map, events, charts with the rows behind every bar one click away, a map, a
heatmap, a connection graph, buckets and colour groups - and the Watchlist
pages that configure the crawler. It fits any archive and is the one to start
with.

→ [`standard/README.md`](standard/README.md)

## A dashboard for a special case

A dashboard needs one thing: a PostgreSQL with the archive schema
(`../database`). It reads `processed_data` and `monitoring`, and if it
configures the crawler it reads and writes `scraper_config` and reads
`scraper`. Nothing else in `API_V1` knows or cares which dashboard is running,
and several can run beside each other against one archive on different ports.

So a dashboard for a particular kind of archive can be

- **a further folder here** - `dashboard/<name>/` with its own `README.md`,
  `docs/`, `docker-compose.yml` and `.env.example`, built and started the same
  way as `standard/`;
- **a project of its own**, in another repository, obtained from wherever it
  is published - pointed at the same archive through `DATABASE_URL` or the
  four `POSTGRES_*` variables and the shared network.

Either way the archive, the collector and the crawler stay as they are. What
a custom dashboard has to respect is written down in the database's
[`docs/DESIGN.md`](../database/docs/DESIGN.md): the three columns on every
row, which schemas are whose, and that the archive is read-only for a
dashboard that does not own it.

## Running two at once

Each dashboard is its own Compose project. Give each its own `DASHBOARD_PORT`
in its `.env`; the `ARCHIVE_STACK` stays the archive's, so both join the same
network and read the same data.
