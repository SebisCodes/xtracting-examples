# Database - purpose

The archive for your extraction results. Xtracting keeps a result for about an
hour after it is produced; this is where you keep the ones you want, for as
long as you want them.

It is a PostgreSQL database with TimescaleDB, in a container, with a schema
that fits everything the API returns: sources, entities, connections,
locations, events, ratings, attributes and market insights, in every language
a job asked for. Every row carries the project it belongs to, the language it
is in and the task that produced it, so one database holds several projects
and every translation without them ever mixing.

The same database also holds what the other components need to talk to each
other: what the crawler watches, what it has seen and queued, what the
dashboard's settings are, and a log of every run and every problem.

## Who uses it

| Component | What it does here |
|---|---|
| collector | writes the extraction results (`processed_data`) |
| crawler | reads its configuration, writes what it fetched and queued (`scraper`) |
| dashboard | reads everything, writes its own settings and the crawler's configuration (`dashboard`, `scraper_config`) |
| you | anything: psql, a notebook, a BI tool, your own code |

The archive is general purpose. It does not know or care what you extract; it
holds what the API returns, and every dashboard - the standard one or one of
your own - reads the same tables.

## What it is not

It is not a copy of the platform. Results are fetched into it by the collector
or written by your own code; nothing here talks to Xtracting itself.
