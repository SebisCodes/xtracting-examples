# The collector

## What it is for

This is the part that does the collecting of finished jobs from xtracting.
Start it, submit work whenever you like, and the results are in your archive within fifteen minutes.

## What it is

A small Python service in a container. About three hundred lines, no framework,
two dependencies: `requests` and `psycopg`.

It only reads, and by default it deletes nothing: results are fetched with
`retain=true`, so the content stays on the platform until the platform's own
cleanup removes it about an hour later. There is one setting that changes that,
and it carries a warning - see [Clearing content early](#clearing-content-early).

## What it does

Every `POLL_INTERVAL_MINUTES`, for each API key in turn, `KEY_STAGGER_SECONDS`
apart:

1. `GET /api/v1/batch` - the jobs this key has, and **the project the key
   belongs to**. The project comes back even when there are no jobs, which is
   how a fresh archive knows where rows will go before any work exists.
2. Skip jobs still running, and jobs whose tasks are already stored.
3. `GET /api/v1/batch/{jobId}?retain=true` for the rest.
4. Write each completed task once per language - the English original plus every
   translation the job requested.
5. Record the round in `monitoring.collector_runs`, whether it worked or not.

Then it sleeps until the next interval, measured **from the start** of the
round, so a slow round does not push the schedule later and later.

**Except while somebody is watching.** Fifteen minutes is the right interval for
work nobody is waiting for. It is the wrong one for the dashboard's *Crawl and
send one document* button, where a person pressed it, is looking at the screen
and is asking a question a quarter of an hour does not answer. So while a manual
run has a document that was submitted and is not archived yet, the loop shortens
its sleep to **30 seconds**, for at most **15 minutes** from the submission -
long enough for a slow extraction, short enough that a document the platform
never finishes cannot pin the collector at that cadence. The moment the last one
is archived it goes back to the configured interval, and says so in the log.

That check is one cheap statement per round. It needs no port and no second
mechanism: the loop already wakes in five-second slices to be able to stop.

To fetch a result straight away instead of waiting for the next interval:

```sh
docker compose exec collector python -m app.main --once
docker compose exec collector python -m app.main --once --key flats
```

One round over every key, or over one of them, and then it exits. The same
`collect_for_key()` the loop calls, so there is one implementation and not two
that look alike.

## Setting it up

The archive database has to be running first - the collector joins its network.

```sh
cd ../database
cp .env.example .env        # set POSTGRES_PASSWORD
docker compose up -d

cd ../collector
cp .env.example .env        # put your API keys in it, same POSTGRES_PASSWORD
docker compose up -d
```

Podman, nerdctl and the other Compose-compatible runtimes work the same way -
see the [API_V1 README](../../README.md#container-runtimes).

Only one setting really needs thought:

```
XTRACTING_API_KEYS=plant-docs:ab12cd34.xxxx,market:ef56gh78.yyyy
```

Comma separated. The `label:` part is optional and only affects log lines and
`monitoring.collector_runs` - the label never contains the secret, and neither
do the logs. The names in those lines - what the key is called and which
project it opens - come from the platform: the collector asks each key what it
is before it fetches anything, and a key with **Read project** enabled is also
asked for the project's name. The label is only your own marker for which
entry does what ([SETUP.md](SETUP.md#the-label-and-where-the-names-come-from)).

**Writing to a database somewhere else?** Set `DATABASE_URL` in `.env` to a full
connection string and remove the `networks:` blocks from `docker-compose.yml`.
The collector needs a PostgreSQL carrying the archive schema, not specifically
the one next door.

Otherwise it builds the connection string from `POSTGRES_USER`,
`POSTGRES_PASSWORD`, `POSTGRES_DB` and `POSTGRES_HOST`. That assembly happens in
the application rather than in the compose file, because a nested `${...}`
default is not portable across Compose implementations, and because a password
containing `@` or `/` breaks a hand-built URL silently.

## Watching it

```sh
docker compose logs -f
```

One line per key per round. Nothing for fifteen minutes is normal.

```sh
cd ../database
docker compose exec -T xtracting_db psql -U xtracting -d xtracting_archive -c \
  "SELECT date_added, text_project, text_status, integer_jobs_new, integer_rows
     FROM monitoring.collector_runs ORDER BY date_added DESC LIMIT 10;"
```

## Decisions worth knowing about

**`retain=true` on every read.** Without it, reading a completed job is the one
look the platform gives you and the results are dropped afterwards. An archiver
that consumed what it archives would make your dashboard go blank the first time
it ran.

**It remembers by reading its own output.** Every table carries
`text_project`, `text_language` and `text_task_id`, all three indexed. Each
round the collector reads the task ids already in `processed_data.tasks` for
this project within the last `COLLECTED_LOOKBACK_HOURS`, and fetches whatever
is not there.

No separate ledger, on purpose: a second table can disagree with reality, and a
row's own existence is the only proof that cannot. The rows are written in one
transaction, so a crash leaves the task looking uncollected and it is simply
picked up again.

The window is what keeps it cheap. Results live about an hour on the platform,
so a task older than that cannot be fetched again whether or not the collector
remembers it; `date_added` is the partitioning column, so a two-hour lookup
touches one or two chunks and stays the same size after a year as on the first
day. Scanning the whole archive every fifteen minutes would grow without bound
and return mostly task ids nobody can fetch.

**Delete-then-insert per task.** Storing a task removes any existing rows for
that task id first. Re-fetching a job - after a crash, a restart, or two
collectors briefly overlapping - leaves the archive exactly as it was.

**Timestamps come from the API.** `date_commissioned` and `date_evaluated` are
the task's own, not the time this collector happened to fetch it. A row whose
only timestamp is the fetch time cannot be put on a timeline.

**One key's failure is contained.** An unreachable API, a rejected key, a job
that fails to store - each is logged and the round continues. Nothing one key
does can stop another from being collected.

**The content hash is stored, not computed.** Each task comes back with a
`contentHash` - the SHA-256 of exactly what was submitted, taken by the worker
that read it - and the collector writes it to `text_content_hash` on both
`processed_data.tasks` and `processed_data.sources`, indexed on each.

That answers a different question than the task id does. The task id tells the
collector whether it has fetched a *job*; the hash tells you whether you have
already extracted a *file*. It survives renames, moves and copies, and it
catches the file whose name stayed the same while its contents changed - the
case a filename comparison gets wrong silently:

```sh
sha256sum ./docs/* | awk '{print $1}' | LC_ALL=C sort -u > local.txt

psql -At -c "SELECT DISTINCT text_content_hash
             FROM processed_data.tasks
             WHERE text_project = 'plant-docs'
               AND text_content_hash <> ''" | LC_ALL=C sort -u > archived.txt

comm -23 local.txt archived.txt        # hashes not in the archive yet
```

Both sides go through `LC_ALL=C sort` because `comm` compares line by line and
assumes the same ordering on each - letting the database sort one side and the
shell the other is the way this quietly returns nonsense.

The collector never computes a hash itself. Hashing the text it received would
only prove the text survived the network; the value here is that it was taken
where the content was read.

**Numbers are parsed, not enforced.** `float_value` on an attribute holds the
numeric reading where there is one - `approx. 12.5 bar` becomes `12.5` - while
`text_value` keeps exactly what the source said. A column that could only hold a
number would quietly discard the qualification, which is often the point.

## Clearing content early

`CLEAR_CONTENT_ON_FETCH=true` sends `retain=false`, which tells the platform to
delete the results as it hands them over.

**The deletion happens before this collector has committed anything.** If the
database is unreachable, a disk is full, or the container is killed in that
window, those extractions are gone. They cannot be fetched again, and you have
already paid for them.

The default is off, and off is right for almost everyone: the platform clears
the content on its own schedule anyway, about an hour later, and until then a
failed write costs nothing but a retry on the next round. The only reason to
turn it on is a requirement that content leave the platform as early as
possible - which buys roughly one hour at the cost of your only safety net.

The collector says which mode it is in on every start:

```
collector started: 1 key(s), every 15 minute(s), 20s apart, https://api.xtracting.io
dedup window 2h, content on the platform is left for the platform to expire
```

## Tests

```sh
# the mapping, no database needed
docker build -t xtracting-collector .
docker run --rm -v "$PWD/tests":/srv/tests --user root xtracting-collector \
  sh -c "pip install -q pytest && cd /srv && python -m pytest tests -q"

# end to end: a stand-in API, the real collector, the real database
docker run --rm --network host -v "$PWD/tests":/srv/tests \
  -e DATABASE_URL="postgresql://xtracting:YOURPASS@127.0.0.1:5432/xtracting_archive" \
  xtracting-collector python tests/integration.py
```

The integration test writes into a project called `integration-test` and removes
it again afterwards, so it is safe against an archive holding real data.

## When it cannot reach the database

`POSTGRES_HOST` decides this, and the default only works in one of three
situations. The collector says which one it thinks you are in before it starts
retrying.

| Your database is | `POSTGRES_HOST` | `POSTGRES_PORT` |
|---|---|---|
| the container from `../database` | `xtracting_db` (default) | `5432` |
| on another machine | its hostname or IP | its port |
| on this machine, outside a container | `host.docker.internal` (Podman: `host.containers.internal`) | its port |

Two things that catch people, both of which the collector now names in its log
rather than leaving you to the resolver error:

- **`localhost` is not your machine.** Inside a container it is the container.
  Use the third row instead.
- **`POSTGRES_PORT` is not the port from `../database/.env`.** That one is where
  the database appears on *your* machine. Reaching it as `db` goes over the
  shared network straight to 5432, whatever you published it as.

If it connects and then stops with *"this database has no archive schema"*, the
credentials were right and the database was the wrong one - load
`database/init/01-schema.sql` into it, or point at the one from `../database`.

## When something is wrong

| What you see | What it means |
|---|---|
| `the API key was rejected` | the key in `.env` is wrong, revoked, or for another environment |
| `job ... expired before it could be archived` | results are kept for an hour; the collector started too late. Not recoverable, and not a fault |
| `waiting for the database` | normal at start-up, for a few seconds |
| `network ...-archive declared as external, but could not be found` | either the database stack is not running - start `../database` first - or `ARCHIVE_STACK` here does not match the one in `../database/.env` |
| nothing at all for 15 minutes | also normal. One line per key per round |
