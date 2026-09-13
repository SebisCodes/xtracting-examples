# The archive database

## What it is for

Extraction results are deleted an hour after they are produced. That keeps your
material off somebody else's server, and it means anything you want to keep has
to be kept somewhere of your own. This is that somewhere.

One PostgreSQL database, with a schema that fits what the Xtracting API
returns - every kind of structured extraction, every project, every language - so
that a team can
share one archive instead of everybody keeping their own spreadsheet.

## What it is

PostgreSQL 18 with [TimescaleDB](https://www.timescale.com/). Every table that
holds extraction results is a hypertable partitioned by time, in one-day
chunks, compressed a day after each chunk closes. Compressed rows stay
queryable; you never decompress anything to read it. (The vocabularies and the
crawler's own state are plain tables - the two sections below say why.)

```
processed_data.sources          what a document was, and how much weight it carries
processed_data.entities         the things found in it
processed_data.locations        where those things are, with coordinates
processed_data.attributes       typed values with units
processed_data.events           what happened, and when
processed_data.ratings          judgements, per perspective you defined
processed_data.connections      how entities relate to one another
processed_data.event_entities   which entities an event was about, one row each
processed_data.market_insights  outlook and sentiment, per horizon
processed_data.tasks            one row per task, so you can see what was done

processed_data.*_types          vocabularies, filled in as they first appear
monitoring.collector_runs       every polling round, including the failed ones

scraper_config.*                what a person configured in the dashboard's Sources pages
crawler.*                       what the crawler did with it - see "The crawler tables"
monitoring.scraper_runs         every crawl of a source, in the shape of collector_runs
monitoring.scraper_errors       one row per thing that went wrong, in a sentence
```

## What it does differently

**Three columns on every single row.**

- `text_project` - which project the row belongs to. An API key is bound to
  exactly one project, so several projects can share an archive without their
  rows ever mixing.
- `text_language` - which language version this row is. The English original is
  stored as `English`; each translation is stored as its own *complete set of
  rows* under its own language.
- `text_task_id` - which task produced it. This is what a collector reads to
  work out what it already has, and what you delete by if one task's output ever
  has to go.

All three are indexed on every table.

Which is what lets a German-speaking colleague run

```sql
SELECT text_name, text_value, text_unit
FROM processed_data.attributes
WHERE text_project = 'plant-docs' AND text_language = 'German';
```

and get a whole, self-consistent German view of the same archive an English
speaker reads.

**All three are plain B-tree indexes, deliberately not TimescaleDB dimensions.**
Partitioning by them is the first thing anyone familiar with the tool suggests,
and it would be wrong here. A time-series database partitions to keep the
working set small and to let old chunks be compressed or dropped as a unit.
Neither applies: a project does not age out, and a language chunk would only
ever fill as fast as its project does. What these columns actually get asked is
`WHERE text_language = 'German'` - an equality filter, which a B-tree answers
directly. As space dimensions they would multiply the chunk count by projects
times languages and leave every chunk too small to compress well.

**Compression segments by name, type, project and language.** Inside a
compressed chunk, rows sharing those values are packed into one compressed row,
and the segment columns stay readable without unpacking anything. That is what
makes `WHERE text_name = '...'` cheap: a name lives in a few segments, and the
rest are skipped whole.

`text_name` is the selective one. `text_project` and `text_language` are not -
every chunk holds every project and every language, so filtering on those alone
narrows nothing down. They are named anyway, because doing so makes the chunk
*smaller*: a value held once per segment costs less than the same value
compressed as a column across every row in it.

Measured on 300 000 rows in a single chunk, one hot name covering 100 000 of
them, best of five runs each:

| segment list | size | 100 000 results by name | one rare name | bulk read by project+language |
|---|---|---|---|---|
| `project, language` | 23 MB | 7.9 ms | 7.8 ms | **1.1 ms** |
| `name, type` | 34 MB | **1.1 ms** | 0.16 ms | 23.0 ms |
| `name, type, project, language` | **28 MB** | 1.2 ms | **0.09 ms** | 5.1 ms |

The third is what the schema uses. Note the direction of the size column: adding
the two non-selective columns to a name-segmented chunk does not cost space, it
saves it.

Reproduce it by loading your own data and re-running the comparison - the
numbers move with how repetitive your values are, and a column of random hashes
does not compress no matter how the chunk is segmented.

## Running a second copy

Everything this stack owns - the Compose project, the network, the data volume -
is prefixed with `ARCHIVE_STACK` from `.env`, `xtracting` by default. To run a
second, independent archive on the same machine, give that copy its own value in
`database/.env` and the matching `collector/.env`, plus a free `POSTGRES_PORT`.

Two copies left on the same value are one archive with two sets of files
pointing at it. They share the volume, and `docker compose down` in either one
takes the network with it - the other container keeps running and reporting
healthy while refusing every connection, because Docker publishes ports per
network attachment and it no longer has one.

`.env.test` in this folder is exactly such a second copy, committed for the
test suites: `ARCHIVE_STACK=xtractingtest`, port 55432, a password that
protects an empty database on 127.0.0.1. It is what the repository's tests
point at, and it is why they cannot reach the archive you keep:

```sh
docker compose --env-file .env.test up -d
docker compose --env-file .env.test down -v      # volume and all
```

## The image

`timescale/timescaledb:latest-pg18`.

Two things to know if you are coming from a `-pg17` image with data in it:

- **It is a major PostgreSQL upgrade.** PostgreSQL 18 will not read a data
  directory written by 17. Dump the old archive, start the new image on an empty
  volume, restore into it. Swapping the tag and restarting gets you a container
  that refuses to start, which is the good outcome.
- **The volume moved.** PostgreSQL 18 keeps its data in a major-version
  subdirectory, so the mount belongs on `/var/lib/postgresql` and not on
  `/var/lib/postgresql/data`. The compose file here already does this; mount the
  old path and the entrypoint stops and explains itself at length.

### What the compose file tunes, and why

Four settings, all of them there because a default was wrong for a database of
hypertables rather than because bigger is better. `database/docker-compose.yml`
carries the reasoning next to each one; this is the summary.

| Setting | Value | What it prevents |
|---|---|---|
| `max_worker_processes` | 24 | Eleven compression policies plus parallel query need more than the default 8. Too few and policies fail with "failed to start job" and chunks quietly stay uncompressed. |
| `timescaledb.max_background_workers` | 12 | The other half of the same rule: `max_worker_processes >= 1 + max_background_workers + max_parallel_workers`. |
| `max_locks_per_transaction` | 2048 | One lock per chunk. A query that cannot exclude chunks - "every address of these 1700 entities" - takes one on each of the hundreds in a hypertable, and 64 is not enough. What you get instead is `out of shared memory`, on a machine with plenty. |
| `shm_size` | 1 GB | A container gets 64 MB of `/dev/shm` by default, and a parallel query hands each worker a segment out of it. What you get instead is `could not resize shared memory segment ... No space left on device`, on a disk with plenty. |

The last two are worth recognising by their messages, because neither says what
it means: both name a resource that is not the one that ran out. If a view
starts answering 500 after an archive grows, look at the chunk count first -
`SELECT count(*) FROM timescaledb_information.chunks` - and raise
`max_locks_per_transaction` to about four times the biggest hypertable's share
of it.

## Answering "have I already extracted this file?"

`processed_data.sources` and `processed_data.tasks` both carry
`text_content_hash`: the sha256 of exactly what was submitted, as the platform
computed it. Both are indexed.

Which turns a directory into a set of hashes you can diff against the archive:

```sql
-- Everything already extracted, by content rather than by name
SELECT DISTINCT text_content_hash
FROM processed_data.tasks
WHERE text_project = 'plant-docs';
```

Hash your files locally, subtract that set, and what remains is what has changed
or is new. It survives a rename, a move, a copy and a touched modification date,
and it catches a file whose name stayed the same while its contents did not -
which is exactly the case a filename comparison gets silently wrong.

## Splitting an archive later

Every row carries its project, and nothing is ever shared between projects. An
archive that outgrows one database is therefore split by copying rows
`WHERE text_project = '...'` - there is nothing to untangle first.

That is also why the same vocabulary entry is stored once *per project* rather
than once globally. `Machine` in project A and `Machine` in project B are two
rows, enforced by `UNIQUE (text_name, text_project, text_language)`. Merging
them would save a few dozen rows and make the archive inseparable.

## Time

`date_added` is when a row was written here, and it is the partitioning column.
It is **not** when the work happened. For that, every row carries:

- `date_commissioned` - when the task was submitted
- `date_evaluated` - when its evaluation finished

Use those on a timeline. Building a chart on `date_added` shows you when your
own collector happened to be running, which is a fact about your infrastructure.

## Setting it up

```sh
cd database
cp .env.example .env        # set POSTGRES_PASSWORD
docker compose up -d
```

That is all. The schema in `init/` runs automatically on first start.

Podman, nerdctl and the other Compose-compatible runtimes work the same way -
see the [API_V1 README](../../README.md#container-runtimes).

**If port 5432 is already taken** by a PostgreSQL on your machine, set
`POSTGRES_PORT` in `.env` to something free. The port is bound to `127.0.0.1`,
so nothing is exposed to your network either way.

### Connecting to it

```sh
docker compose exec xtracting_db psql -U xtracting -d xtracting_archive
```

Or from anything else on your machine, at `localhost:5432` with the credentials
from `.env`. It is an ordinary PostgreSQL database - point Grafana, Metabase,
DBeaver or psql at it.

## Verifying it

`verify.sql` checks the structure and then proves the behaviour: it writes a
task in two languages, joins across the tables, forces a chunk to compress,
reads back out of the compressed chunk, and rolls everything back.

```sh
docker compose exec -T xtracting_db psql -U xtracting -d xtracting_archive -f /verify.sql
```

Twenty-six checks, each printing PASS or FAIL. A check that cannot be answered
prints FAIL rather than nothing - a verification that goes quiet when it breaks
is worse than none at all. It is safe against an archive holding real data:
everything it writes is rolled back.

`verify-scraper.sql` does the same for what `init/03-scraper.sql` adds - a
separate file, so the archive suite keeps its twenty-six checks and still runs
against an archive that never loaded the crawler tables:

```sh
docker compose exec -T xtracting_db psql -U xtracting -d xtracting_archive -f /verify-scraper.sql
```

Thirty-six checks: the constraints refuse what the editor refuses, a split tag
still reconciles, deleting a source takes its configuration and leaves the
crawler's state, the log keeps its retention policy, and - when the optional
roles exist - no application role can write the archive. Without roles those
last checks pass with a note saying so.

The dashboard brings a third suite for the schema it creates itself
(`dashboard/standard/sql/verify.sql`, seventeen checks - the last two are the access
tokens below: only an enabled, unexpired row counts as live, and the same
token cannot be issued twice). It is not mounted into the container, so it
goes in through stdin:

```sh
docker compose exec -T xtracting_db psql -U xtracting -d xtracting_archive \
    -f - < ../dashboard/standard/sql/verify.sql
```

## Vocabularies

Two kinds, and the difference matters.

Vocabulary tables are plain tables, not hypertables. A vocabulary grows with the
number of distinct names, which saturates - it is not a time series, and a
hypertable cannot carry a unique index that omits its partitioning column. On a
hypertable the constraint would have to include `date_added`, every insert a
second later would be a new row, and the table would gain one duplicate per task
forever.

**Closed** sets come from the platform and are seeded in
`init/02-vocabularies.sql`: rating values, outlooks, sentiments, importances,
relation types. They carry a `float_value` where an order exists, so
`ORDER BY float_value` sorts a scale correctly rather than alphabetically.
`Unset` is deliberately absent from the numeric scales rather than being zero -
"the source says nothing" is not the same statement as "the source says
neutral".

**Open** sets are yours: entity types, units, perspectives, attribute types,
market topics. They come from your project settings, so they are not seeded. The
collector records each one the first time it appears, which gives a dashboard a
filter list without scanning the data tables.

## The crawler tables

`init/03-scraper.sql` adds what the dashboard's Sources pages and the crawler
service need. It changes nothing in the archive tables above - two indexes on
`processed_data.tasks` are the whole of its footprint there - and it draws one
boundary:

```
scraper_config.sources           one row per list page a person watches: address, key label,
                                 mode, engine, cadence, politeness, whether robots.txt is honoured
scraper_config.source_projects   which projects read this page, and the key each of them uses
scraper_config.source_patterns   which links count, as a learned shape (/rent/#) - accept or reject
scraper_config.source_exact_urls single addresses: monitored as documents, or never
scraper_config.source_file_rules which file types to send as text, and the exceptions
scraper_config.test_snapshots    what a site answered to "Test this configuration"
scraper_config.manual_runs       one real crawl somebody asked for by hand, and what came of it

scraper.targets                  schedule, lease, failure count and robots verdict per source
scraper.seen_urls                every address the crawler knows, per project
scraper.documents                every page or file fetched, exactly as it was sent - hypertable
scraper.submit_queue             what is on its way to the API, by tag, with its status
scraper.files                    every file seen on a subpage and what became of it, with a reason

monitoring.heartbeat             which service was last seen when - one row each, written
                                 every minute; in monitoring because no service owns it
monitoring.service_log           what each service DID, step by step, while its debug
                                 switch is on - kept 24 hours
monitoring.scraper_runs          one row per crawl - hypertable, same shape as collector_runs
monitoring.scraper_errors        one row per problem, with the sentence the dashboard's Log shows
```

**Who writes where.** `scraper_config` is written by the dashboard and only read
by the crawler; `crawler` is written by the crawler and only read by the
dashboard; `processed_data` is read-only for both. The test snapshots sit on the
configuration side although a crawl produced them: they are made by the
dashboard's own test runner, and they are a draft plus what the site said to it.
With that split, one privilege query answers "can the dashboard reach the raw
data?", and the answer is no. `init/04-roles.sh` turns the agreement into two
database roles when you want it enforced - see below.

**Plain or hypertable.** The rule from the archive, applied the other way
round: anything that is *updated* after it is written - a queue status, a lease,
a counter - is a plain table, because an update on a compressed chunk is a
decompression and a queue needs unique indexes a hypertable cannot carry. Only
the append-only, time-growing tables are hypertables: `scraper.documents`
(compressed after two days rather than one, because the reconcile step fills in
the task id once the collector has archived it), `monitoring.scraper_runs` and
`monitoring.scraper_errors`.

**Two tables throw rows away, and both are logs.** `monitoring.scraper_errors`
compresses after thirty days and is dropped after ninety, because a log is read
while it is fresh and a year of rejected-file rows helps nobody.
`monitoring.service_log` - the step-by-step debug stream - is dropped after
twenty-four hours: it is written a row per step and is of use only while somebody
is watching. Nothing else in this archive throws anything away.

**Every retention and compression number is in `init/05-policies.sql`**, and only
there. That file creates nothing, so unlike `01-schema.sql` it can be applied to a
grown archive whenever a number changes, and it says which policies moved. It has
to drop and re-add a policy to change one, because `add_compression_policy` will
not: with `if_not_exists` it finds a policy and returns without ever looking at the
interval. With the numbers in two files that each call it, archives end up
compressing after one day while both files say thirty.

**Indexes added after the first design are in `init/06-indexes.sql`**, for the same
reason and with the same property: `01-schema.sql` cannot be applied to an archive
that already exists, so an index a later feature needs would never reach one. Every
statement in it is `CREATE INDEX IF NOT EXISTS`, so it may be run against any archive
as often as you like — and each index is also in `01-schema.sql`, so a fresh archive
is born with it. It carries the measurement that matters for this hypertable: a
B-tree index covers the *uncompressed* rows only, and a compressed chunk is skipped
by `compress_segmentby` or not at all.

**The address is the dedupe anchor, not the content.** `scraper.seen_urls` is
keyed by the hash of the canonical address, and `processed_data.tasks.text_source_uri`
is the second gate for anything submitted before the crawler existed. Content
hashes are kept to *notice* a change; whether a change is re-submitted is a
per-source switch, off by default, because a page with a date in its footer
would otherwise be extracted - and billed - on every run.

**And the anchor is per project.** `seen_urls` is keyed by *(project, address)*,
not by the address alone. A project decides what is extracted from a document -
its objects of interest and its perspectives - so two projects asking different
questions of the same page cannot share one extraction. A page assigned to both
is therefore FETCHED ONCE, which is all the site ever sees, and SUBMITTED ONCE
PER PROJECT. Keyed by the address alone, the second project would be told the
address was old and would silently get nothing; and a project added to a page a
year later would collect its back catalogue rather than starting from today,
which is what `scraper_config.source_projects` promises.

**One real crawl, by hand.** `scraper_config.manual_runs` is a request the
dashboard writes and the crawler carries out on its next tick - *even while the
scheduled crawl is switched off*, because a run somebody just asked for and is
watching is not the crawler running. It crawls the page, takes the first
addresses the project has not had, submits them at once and records the tags, so
the dashboard can follow one document from the queue into `processed_data`. It
costs what any other document costs, and the button says so before it is
pressed.

**A saved source is off.** `bool_enabled` defaults to false: saving a
configuration and starting a crawl are two acts. Ignoring `robots.txt` is a
third, and the database refuses it without a written reason
(`source_override_needs_reason`).

### Adding the crawler tables to an existing archive

`init/` only runs on an empty volume. An archive that holds only the archive
schema - your own PostgreSQL with `01-schema.sql` loaded, say - gets the
crawler's tables by applying the file by hand. It is written so that this is
safe: every statement is `IF NOT EXISTS`, nothing is dropped, altered or
updated, and running it twice is a no-op.

```sh
docker compose exec -T xtracting_db \
  psql -U xtracting -d xtracting_archive -v ON_ERROR_STOP=1 < init/03-scraper.sql
docker compose exec -T xtracting_db \
  psql -U xtracting -d xtracting_archive -f /verify-scraper.sql
```

`verify.sql` keeps passing afterwards; it counts the archive's own hypertables
and leaves the crawler's to its own suite.

### Application roles (optional)

By default everything connects as `POSTGRES_USER`, the owner. To have the
database enforce the boundary above, set `DASHBOARD_DB_USER`,
`DASHBOARD_DB_PASSWORD`, `SCRAPER_DB_USER` and `SCRAPER_DB_PASSWORD` in `.env`
before the first start. `init/04-roles.sh` then creates two logins:

| role | reads | writes |
|---|---|---|
| dashboard | `processed_data`, `monitoring`, `crawler` | `dashboard` (owns it), `scraper_config` |
| crawler | `processed_data`, `scraper_config`, `dashboard` | `crawler`, `INSERT` into `monitoring.scraper_runs` |

Neither can create objects, and neither can change a row of the archive. Use
the same values as `POSTGRES_USER` / `POSTGRES_PASSWORD` in `../dashboard/standard/.env`
and `../crawler/.env`. With all four empty the script prints one line and does
nothing; with some set and some empty it stops the container, because half a
configuration is a typo.

Set before the first start is the path to take, because the schema then does
not exist yet: the script creates it empty and gives it to the dashboard role,
which can therefore go on creating its own tables and refreshing its
materialised view. `DASHBOARD_SEED` stays `true`.

To add the roles to an archive that already exists, run the script by hand with
the variables on the command line. It finds existing roles and updates them, so
it can be repeated:

```sh
docker compose exec -T \
  -e DASHBOARD_DB_USER=dashboard -e DASHBOARD_DB_PASSWORD=... \
  -e SCRAPER_DB_USER=crawler -e SCRAPER_DB_PASSWORD=... \
  xtracting_db bash /docker-entrypoint-initdb.d/04-roles.sh
```

**One case does not work yet.** If the dashboard has already created its schema,
the script hands `dashboard` and everything in it over to the dashboard role
object by object - and stops on the first sequence that belongs to one of those
tables:

```
ERROR:  cannot change owner of sequence "colour_groups_bigint_id_seq"
DETAIL:  Sequence "colour_groups_bigint_id_seq" is linked to table "colour_groups".
```

PostgreSQL keeps such a sequence's owner tied to its table's, so the sequence
must be skipped rather than altered. The roles and the first grants are already
in place when it stops, which is half a configuration - drop the two roles and
try again after the fix, or hand the schema over by hand:

```sql
ALTER SCHEMA dashboard OWNER TO dashboard;
ALTER TABLE dashboard.colour_groups OWNER TO dashboard;   -- and the others
```

## The two scripts beside the archive

`manage_tokens.sh` and `manage_testuser.sh` sit next to `docker-compose.yml`
and are run by hand. Neither names a container: both ask Compose for one
(`docker compose ps -q xtracting_db`), so on a machine running the everyday
archive and a test copy at the same time it is the env file that decides which
of them is being changed, and a script that has been copied to a second
installation goes on working.

```sh
cd database
./manage_tokens.sh                      # the archive described by .env
ENV_FILE=.env.test ./manage_tokens.sh   # the copy described by .env.test
```

Each hands out exactly one secret, once, and writes it to the terminal rather
than to standard output. That distinction is the point: everything else these
scripts say goes through their log line, so a run can be redirected into a file
to record what was done - and a token or a password in a file is a token or a
password in a backup, in a diff, and in whatever reads that backup next. With
no terminal at the other end - a pipe, `cron`, a systemd unit - they fall back
to standard output, because a secret nobody can read is worse than a secret
somebody logged on purpose.

[docs/USAGE.md](USAGE.md) is the short version:
which menu item does what. What follows is why either of them exists.

### `manage_tokens.sh` - a second way through the gate, and why there is one

`DASHBOARD_GATE_PASSWORD` is read once, when the dashboard starts. It lives in
a file on the server, changing it means restarting the container, and - because
the gate's cookie is signed with that list - changing it ends every open
session at the same moment. All three of those are correct for the password
that *is* the installation. Withdrawing it should be one edit, it should take
effect everywhere at once, and nobody should be left holding a session that
outlives the change.

All three are wrong for "let this person in until March". That needs an expiry
somebody will not have to remember, a label saying whose it is, and a way to
take it back on a Tuesday afternoon without restarting the dashboard and
without logging out the four people who are using it. None of that fits in an
environment variable, and every installation ends up doing it by hand and badly:
one shared password, never rotated, because rotating it is an outage.

So there is a table. `dashboard.access_tokens` holds one row per token with a
label, a switch and an expiry date, `dashboard/standard/app/gate.py` reads it at most
once every 30 seconds, and a row that is enabled and not yet expired opens the
gate exactly as a password from the environment does. Adding one, or taking one
away, is this script and nothing else.

**The two sources stay independent on purpose.** The cookie is signed with the
environment passwords and a secret generated once into `dashboard.settings` -
and deliberately *not* with the tokens. If a token were in the signing key,
handing one out on a Monday morning would log every other person out on Monday
morning, and the last thing anybody would connect that to is somebody else
being let in. Changing `DASHBOARD_GATE_PASSWORD` still ends every session,
which is what keeps it the lever you pull when something has gone wrong.

**A failed read keeps the last answer** rather than emptying the set. An empty
set is an open gate on an installation whose only key is a row, so "the archive
was briefly unreachable" must not be a way in; the worst a broken read can do
is leave a revoked token working for another half minute. For the same reason
the dashboard installs its gate middleware whatever `DASHBOARD_GATE_PASSWORD`
says: mounting it only when that variable holds something would leave an
installation that hands out tokens and sets no password with no gate at all.

The table is created by `dashboard/standard/sql/01-dashboard-schema.sql`,
which the dashboard applies itself on start. Against an archive that has never
run one, the script says so in a sentence instead of failing with a psql error.

A token added and then taken away again:

```
$ cd database
$ ./manage_tokens.sh

Access tokens for the dashboard gate
  archive:   xtracting_archive as xtracting
  container: xtracting-database-xtracting_db-1

  1) List the tokens
  2) Add a token
  3) Revoke a token, by id

Action [1/2/3]: 2

A label for this token (whose it is): anna, until the audit is over
Months until it expires [6, or 0 for never]: 6
token 7 added for 'anna, until the audit is over'
it expires in 6 month(s); after that it stops working on its own

  This is the only time it is shown. Hand it over the way you would a password:

      tok_XXXXXXXXXXXXXXXXXXXXXXXX

the dashboard reads this table every 30 seconds, so it works within half a minute
```

and later, from the same menu:

```
Action [1/2/3]: 3

 ID |            Label              |            Token             | State | Enabled |  Expires   |    Last used
----+-------------------------------+------------------------------+-------+---------+------------+------------------
  7 | anna, until the audit is over | tok_XXXXXXXXXXXXXXXXXXXXXXXX | live  | yes     | in 6 months | today

Which id? (the ID column above): 7

  1) Disable it - it stops working, and the row keeps its label and its last use
  2) Delete it for good

Action [1/2]: 1
token 7 ('anna, until the audit is over') disabled - it stops working within 30 seconds
```

**Revoking asks for the id, not for the token.** You should not have to have
the secret in front of you in order to take it away - and usually you do not,
because the reason you are revoking it is that somebody else has it: it went
into a chat window, or onto a laptop that has been lost, or to a contractor
whose last day was Friday. Asking for the string back would mean going to look
for the copy you are trying to make worthless. The id, by contrast, is on the
screen already: it comes off the list the script has just printed, one column
to the left of the label that says whose token this is. It also cannot be
mistyped into a silent success - a 28-character secret pasted with one
character missing matches nothing, and `UPDATE ... WHERE text_token = ...`
would report that it changed nothing in the same tone of voice it reports
having worked.

**Disabling is the answer nine times out of ten.** The row keeps its label and
its `date_last_used`, so next week the list can still answer "when was that one
last used, and by then had we already taken it away?" - which is the question
somebody actually has after an incident. Deleting is offered as well, and asks
a second time, because a deleted row cannot answer anything.

### `manage_testuser.sh` - a login for a person, not for a service

The archive already has logins: the owner, and - where `init/04-roles.sh` has
been run - the two application roles above. Neither of them is a thing to hand
to a colleague. The owner may drop the archive and everything in it. The two
roles are cut to the shape of a job, and deliberately so: a person given the
dashboard's role cannot read the crawl data, would find that half their queries
return "permission denied", and would reasonably report it as a broken
database. Handing out either of them also makes the next question - "who did
that?" - unanswerable, because the answer is now "one of us".

So there is a third shape, and this script owns it: wide enough to be worth
having for an afternoon, and narrow enough that nothing it does is
irreversible. `testuser` may `SELECT`, `INSERT` and `UPDATE` in every schema
this archive uses, and may not `DELETE` and may not `CREATE`. A test can add
rows and correct them; it cannot empty a table and it cannot leave a new one
behind - which is also why `DROP OWNED BY` on the way out has nothing of its
own to drop.

Three details in it exist because leaving them out fails later rather than now:

- **The sequences are granted as well as the tables.** Without
  `USAGE ON ALL SEQUENCES` every `INSERT` into a table with an identity column
  fails with `permission denied for sequence` - a message that names the
  sequence and not the table, so the fault is looked for in the table grants,
  where it is not.
- **`ALTER DEFAULT PRIVILEGES` as well as `GRANT ON ALL TABLES`**, because the
  two answer different questions. The grant covers the tables that exist right
  now; the default privileges cover the ones the next migration adds. With only
  the first, a test login stops seeing half the archive the week after a schema
  change, and the error names a table rather than the grant that is missing.
- **The verification connects over TCP**, not over the socket inside the
  container. The socket trusts everybody, so a successful `psql` there would
  prove nothing at all about the connection string the script is one line away
  from printing.

```
$ cd database
$ ./manage_testuser.sh

A test login for the archive
  archive:   xtracting_archive as xtracting
  container: xtracting-database-xtracting_db-1
  role:      testuser (not there yet)

  1) Create it, or reset its password
  2) Revoke it

Action [1/2]: 1

CREATE ROLE testuser
granted SELECT, INSERT, UPDATE on processed_data,scraper,scraper_config,monitoring,dashboard

 Connected as | On port
--------------+---------
 testuser     |    5432

     Schema     | Tables | Can read | Can write
----------------+--------+----------+-----------
 dashboard      |      7 |        7 |         7
 monitoring     |      5 |        5 |         5
 processed_data |     27 |       27 |        27
 scraper        |      7 |        7 |         7
 scraper_config |      7 |        7 |         7

connected as testuser and read back its rights

  The password is shown once. From this machine:

      postgresql://testuser:XXXXXXXXXXXXXXXXXXXXXXXX@127.0.0.1:5432/xtracting_archive
```

That string is the whole point of the script, and it goes straight into DBeaver,
a notebook or a `.env`. Nothing keeps a copy of the password: run the script
again to issue a new one, which also re-issues every grant.

Taking it away is the second menu item, and the order it works in is not
arbitrary. Open sessions are terminated first, because a role with a session
open cannot be dropped and the error says only that objects depend on it -
while the session in question is somebody's idle DBeaver in another window.
`DROP OWNED BY` comes next, because the default privileges granted above live
in the owner's catalogue rather than in the role, and would otherwise be left
behind pointing at a role that no longer exists. `DROP ROLE` is last, and after
it the connection string above answers `password authentication failed`.


## Changing the schema

`init/` runs **once**, on an empty data directory. Editing those files does
nothing to a database that already exists.

To change a live archive, write a migration and apply it:

```sh
docker compose exec -T xtracting_db psql -U xtracting -d xtracting_archive -f - < my-migration.sql
```

To start over, destroying everything in the archive:

```sh
docker compose down -v && docker compose up -d
```

## A note on what ends up here

Once results are in your database they are yours: your backups, your access
control, your retention. If your extractions ever touch material with
obligations attached, those obligations move with the data. The container does
not carry them for you.
