# Collector - setting it up

Fetches finished extractions from Xtracting and writes them into the archive.

**Start the database first.** The crawler does not have to be running: the
collector fetches whatever the keys it holds have jobs for, whoever submitted
them.

## Three commands

```sh
cd API_V1/collector
cp .env.example .env
$EDITOR .env
docker compose up -d
```

## What to put in `.env`

| Variable | What it decides |
|---|---|
| `ARCHIVE_STACK` | Must be **exactly** the same as in `database/.env`. |
| `POSTGRES_PASSWORD` | The one the database was created with. |
| `POSTGRES_HOST` / `POSTGRES_PORT` | `xtracting_db` and **`5432`** - inside the network, never the published port. |
| `XTRACTING_API_KEYS` | Which keys to fetch results for. Normally the same list the crawler has. |
| `POLL_INTERVAL_MINUTES` | How often it looks. 15 by default. |
| `KEY_STAGGER_SECONDS` | Seconds between keys, so ten of them do not hit the API in the same second. 20 by default. |
| `CLEAR_CONTENT_ON_FETCH` | `false` by default: results stay on the platform after they are read. |
| `DATABASE_URL` | Optional: a complete connection string to a database somewhere else. Then remove the `networks:` blocks from the compose file. |

Keys are comma-separated, optionally `label:key`. A key that fetches must be
able to read its jobs; a key that only submits will fetch nothing and say so.

### The label, and where the names come from

The label before the `:` is yours, for telling entries apart - it appears in
the log and in `monitoring.collector_runs`, so a line about a failure names
which entry it was. Several keys go in the same variable, comma separated, and
each collects into its own project's archive.

The names come from the platform, not from the label. At the start of every
round the collector asks each key what it is (`GET /api/v1/key`): the key's
own name, the project it opens and what it may do. With **Read project**
enabled on the key it also reads the project itself (`GET /api/v1/project`)
and takes the project's name from there. Only then does it fetch the results.
So the log reads

```
[plant-docs] key 'archive reader' opens project Plant documentation
[plant-docs] project Plant documentation: 3 job(s) known to the API
```

and the label is nothing but your own marker for which entry does what. A key
without Read project still works; the names then come from what the jobs
endpoint says about the project, and if the platform cannot answer at all, the
label alone is what the log shows.

## Checking it worked

```sh
docker compose logs --tail 20 collector
```

You want `collector started: N key(s), every 15 minute(s), …` and then a line
per key per round, even when there is nothing to fetch: `0 job(s) known to the
API` is the system working.

The collector's pill at the right of every dashboard page title turns green
within a minute.

## Fetching now instead of in fifteen minutes

```sh
docker compose exec collector python -m app.main --once
docker compose exec collector python -m app.main --once --key patents
```

One round over every key, or over one of them, and then it exits. It calls
exactly the same code the loop calls.

You will rarely need it: while a manual run from the dashboard is waiting for
its result, the collector shortens its own interval to 30 seconds by itself.

## What arrives

One row per task per **language**: the English original, plus a complete set
of rows for each translation the job asked for. Every row carries the project,
the language and the task it came from, which is how several projects share
one archive without their rows ever mixing.

## Against a database somewhere else

Set `DATABASE_URL` in `.env` and remove the `networks:` blocks from
`docker-compose.yml`. The collector needs a PostgreSQL with the archive schema
(`database/init/01-schema.sql` and `02-vocabularies.sql`), not specifically
the container from `../database`.
