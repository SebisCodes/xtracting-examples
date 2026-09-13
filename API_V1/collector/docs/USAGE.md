# Collector - using it

## Restarting, stopping, upgrading

```sh
cd API_V1/collector
docker compose logs -f collector      # watch it
docker compose restart                # the same container, the same settings
docker compose down && docker compose up -d     # after a change to .env
docker compose build && docker compose up -d    # after a change to the code
docker compose stop                   # stopped until the next up
```

Compose reads `.env` when a container is **created**: `restart` keeps the old
values, `down` and `up -d` pick up the new ones.

A round in flight is lost and repeated on the next start. Nothing is lost with
it: a task already written is not written twice, and a job already read is
still on the platform unless clearing was switched on.

## What one round does

For each key in turn, staggered by `KEY_STAGGER_SECONDS`:

0. asks the key what it is - its name, the project it opens, what it may do -
   and, if it may read its project, the project's name; that is what the
   lines below are labelled with
1. asks the API which jobs that key has
2. skips the ones still running, and the ones whose tasks are already stored
3. fetches the rest
4. writes each completed task once **per language** - the English original
   plus every translation the job asked for
5. records the round in `monitoring.collector_runs`, whether it worked or not

Then it sleeps until the next interval, measured **from the start** of the
round, so a slow round does not push the schedule later and later.

## Test mode: 30 seconds instead of 15 minutes

Fifteen minutes is the right interval for work nobody is waiting for. It is
the wrong one for the dashboard's **Crawl and send one document**, where
somebody pressed a button and is looking at the screen.

So: while a manual run has a document that was submitted and is not archived
yet, the loop shortens its sleep to **30 seconds**, for at most **15 minutes**
from the submission. Long enough for a slow extraction; short enough that a
document the platform never finishes cannot pin the collector at that cadence
for ever. The moment the last one is archived it goes back to the configured
interval, and says so in the log:

```
test mode on: a manual run is waiting for its result, looking every 30s
test mode off: back to every 15 minute(s)
```

A manual run that arrives during the sleep is noticed within seconds: the
loop wakes in five-second slices anyway, in order to be able to stop, and asks
the question in each of them.

## Fetching now

```sh
docker compose exec collector python -m app.main --once
docker compose exec collector python -m app.main --once --key patents
```

One round, then it exits. The same code the loop calls.

## Step logging

Off by default. Switch it on in the dashboard and, **at the start of its next
round**, the collector writes a row per step into `monitoring.service_log`,
shown in the Log view under **Collector**. Rows are dropped after 24 hours.

It is read at the start of a round rather than continuously, so a round is
either fully logged or not logged at all - half a round in the log is worse
than none.

## Is it alive?

It writes `monitoring.heartbeat` every minute, and the pill at the right of
every dashboard page title shows it. This matters more for the collector than
for the crawler: a collector that has crashed writes no run row either, so
without the heartbeat the one state worth seeing would be the one state
invisible.

## What is kept on the platform

Jobs are fetched with `retain=true`, so a result stays on the platform after
it is read and a second collector - or a re-run after a restore - can still
get it. Set `CLEAR_CONTENT_ON_FETCH=true` to clear it instead; the
log says at start-up which of the two is in force.

## Reading what arrived

```sh
cd ../database
docker compose exec xtracting_db psql -U xtracting -d xtracting_archive -c "
SELECT text_project, text_language, count(*) AS sources, max(date_added) AS newest
  FROM processed_data.sources GROUP BY 1, 2 ORDER BY 1, 2;"
```

The dashboard's project selector is built from exactly this - from what was
collected, not from configuration - which is why a project appears there only
after its first document arrives, and stays after its configuration is
deleted.

## When something is wrong

### Documents are SENT but never ARCHIVED

The crawler's half worked and this one has not. Ask what the collector sees:

```sh
docker compose logs --tail 40 collector
```

- **`0 job(s) known to the API`** for a key that definitely submitted - the
  key that fetches is not the key that submitted, or it may not read its own
  jobs. The crawler and the collector normally hold the same list.
- **nothing at all for fifteen minutes** - that is the interval, and it is
  normal. To not wait: `docker compose exec collector python -m app.main --once`.
- **a job that answers 410** - the result expired on the platform before it
  was fetched. Results do not live for ever; a collector that was down for a
  long weekend can lose them. It is logged and the round carries on.

### It fetches, but nothing appears in the dashboard

The dashboard shows one project and one language at a time, and the pair is
in the top bar. A round that wrote 40 rows under `German` shows nothing while
`English` is selected.

```sh
cd ../database
docker compose exec xtracting_db psql -U xtracting -d xtracting_archive -c "
SELECT text_project, text_language, count(*) FROM processed_data.sources GROUP BY 1,2;"
```

### The same document arrives twice

It should not, and if it does the cause is almost always two collectors
against one archive with the same keys. One is enough; a second doubles the
API calls and races the first for every job.

A page assigned to **two projects** is a different matter and is intended: it
is fetched once and extracted once per project, so two rows under two project
names is the design.

### `no project for this key`

The key answered, but the platform did not say which project it belongs to.
Nothing can be filed, because the project is part of every row's identity.
Check the key in Xtracting; a key with no project is a key that cannot be
used here.

### The round takes longer and longer

`KEY_STAGGER_SECONDS` is spent per key, so ten keys spend at least
three minutes before the last one starts. That is deliberate - ten keys
hitting the API in the same second is what it avoids - but with many keys it
wants a longer interval as well.

### Test mode will not stop

It ends when the last manual run's document is archived, or 15 minutes after
that document was submitted, whichever comes first. If the log keeps saying
`test mode on`, something is submitting manual runs repeatedly - check
`scraper_config.manual_runs`:

```sh
docker compose exec xtracting_db psql -U xtracting -d xtracting_archive -c "
SELECT bigint_id, bigint_fk_source, text_status, date_added, date_finished
  FROM scraper_config.manual_runs ORDER BY date_added DESC LIMIT 10;"
```

### The Collector log tab is empty

Its switch is off. It is read at the start of a round, so after switching it
on the first rows appear when the next round begins - up to fifteen minutes
later, or at once with `--once`.

### It will not start

- **`configuration error: …`** - a required variable is missing; the message
  names it.
- **`connected, but this database is not a complete archive`** - see
  [the database's USAGE.md](../../database/docs/USAGE.md#the-schema-did-not-finish).
- **`0 key(s)`** - `XTRACTING_API_KEYS` did not parse. Comma-separated,
  optionally `label:key`.
- **`network …-archive declared as external, but could not be found`** -
  the database stack is not running, or its `ARCHIVE_STACK` differs from this
  one.
