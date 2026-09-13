# Database - using it

How to reach the archive, what is in it, how long things are kept, how to
change it, how to back it up, the scripts beside it, and what to do when
something is wrong.

## Restarting, stopping, upgrading

```sh
cd API_V1/database
docker compose restart          # the same container, the same data
docker compose stop             # stopped; the volume stays
docker compose up -d            # started again, or recreated after a change
docker compose down             # container and network gone; the volume stays
docker compose down -v          # AND the volume: every row is gone
```

Compose reads `.env` when a container is **created**, not when it is
restarted. After changing `.env`, use `down` and `up -d` rather than `restart`.

To move to a newer image:

```sh
docker compose pull && docker compose up -d
```

The other components sit on the network this stack creates, so `down` here
takes the network away from them as well; start the database first, then
`up -d` the others again.

## Getting a psql

```sh
docker compose exec xtracting_db psql -U xtracting -d xtracting_archive
```

No host, no port, no password: it runs inside the container as the owner, over
the local socket. That is the shape every command in this documentation uses,
and it is why none of them need a password.

For a second stack, add its env file:

```sh
docker compose --env-file .env.test exec xtracting_db psql -U xtracting -d xtracting_archive
```

From your own machine, the archive is published on `127.0.0.1:POSTGRES_PORT`:

```sh
psql "postgresql://xtracting:<password>@127.0.0.1:5432/xtracting_archive"
```

## What is where

| Schema | Who writes it | What is in it |
|---|---|---|
| `processed_data` | collector | the archive itself: sources, entities, connections, locations, events, ratings, attributes, market insights |
| `scraper_config` | dashboard | what is watched: pages, their projects, their rules, test snapshots, manual runs |
| `scraper` | crawler | what the crawler knows: targets, seen addresses, documents, the submit queue, the key registry |
| `dashboard` | dashboard | its own settings, colour groups, buckets, access tokens |
| `monitoring` | everyone | runs, problems, step logs, heartbeats |

The split is the boundary: under the least-privilege roles the dashboard cannot
read the raw crawl data and the crawler cannot write the archive.

## How long things are kept

| Table | Compressed after | Dropped after |
|---|---|---|
| `monitoring.service_log` | never | **24 hours** |
| `monitoring.scraper_errors` | 30 days | **90 days** |
| `monitoring.scraper_runs` | 30 days | never |
| `monitoring.collector_runs` | 30 days | never |
| `processed_data.*`, `scraper.documents` | 30 days | never |

Only two tables throw anything away, and both are logs. The step log keeps a
day because it is written a row per step and is only useful while somebody is
watching; the problem log keeps a quarter because "why did this source stop
collecting three months ago" is a real question.

**Every one of those numbers lives in `init/05-policies.sql`**, and that file
creates nothing, so it can be applied to an archive that already exists, as
often as you like:

```sh
docker compose exec -T xtracting_db \
  psql -U xtracting -d xtracting_archive -f /docker-entrypoint-initdb.d/05-policies.sql
```

Change a number, run that, and it says which policies moved. It has to drop
and re-add a policy to change one - `add_compression_policy` looks at whether
a policy exists, never at its interval, so a file that merely called it would
promise a change it could not make.

## Least-privilege roles

By default everything connects as the owner, which is fine for one machine
you control. To tighten it, set all four of `DASHBOARD_DB_USER`,
`DASHBOARD_DB_PASSWORD`, `SCRAPER_DB_USER`, `SCRAPER_DB_PASSWORD` in `.env`
**before the first start** and `04-roles.sh` creates the two roles. Half a
configuration is refused with a sentence naming the missing variable - half a
configuration is a typo, not a choice.

On an archive that already exists, run it by hand:

```sh
docker compose exec -T \
  -e DASHBOARD_DB_USER=... -e DASHBOARD_DB_PASSWORD=... \
  -e SCRAPER_DB_USER=...   -e SCRAPER_DB_PASSWORD=... \
  xtracting_db bash /docker-entrypoint-initdb.d/04-roles.sh
```

Then put those credentials into the dashboard's and the crawler's `.env` and
recreate them (`down`, `up -d`). Running it twice is safe: it resets the
passwords and re-issues the grants.

## Backing it up

```sh
docker compose exec -T xtracting_db \
  pg_dump -U xtracting -d xtracting_archive --format=custom > archive.dump
```

Restore into an empty archive with `pg_restore`. The volume itself
(`<ARCHIVE_STACK>-database_archive-data`) can also be copied, but only while
the container is stopped.

## Applying a schema change

The `init/` files are idempotent - every `CREATE` is `IF NOT EXISTS` - so the
way to change the schema is to edit the file and apply it again:

```sh
docker compose exec -T xtracting_db \
  psql -U xtracting -d xtracting_archive -v ON_ERROR_STOP=1 -f /docker-entrypoint-initdb.d/03-scraper.sql
```

Then re-run `verify-scraper.sql`. Something that changes an existing column is
a migration, not a re-application; [DESIGN.md](DESIGN.md#changing-the-schema)
says how to write one.

The dashboard's own tables work differently: it applies its `sql/*.sql` itself
on every start, so a dashboard restart is enough.

## The scripts

Two shell scripts live beside the archive. Neither needs a host, a port or a
password: each asks Compose which container is the archive
(`docker compose ps -q xtracting_db`) and then runs `docker exec` against it,
as the owner, over the container's local socket. That is also why they work
on any stack - a second archive with its own `ARCHIVE_STACK` is found the same
way, and `ENV_FILE=.env.test ./manage_tokens.sh` aims them at the throw-away
one.

Run them from the `database/` folder.

### `manage_tokens.sh` - who may open the dashboard

The dashboard's password gate accepts two kinds of password: the
comma-separated list in `DASHBOARD_GATE_PASSWORD`, which needs a restart to
change, and the rows in `dashboard.access_tokens`, which do not. This script
manages the second kind.

```sh
./manage_tokens.sh
```

It asks what you want to do:

| | |
|---|---|
| **1) List tokens** | id, label, the token itself, whether it is enabled, when it expires, when it was last used |
| **2) Add a token** | asks for a label and for how many months it should last (six by default), makes a random token and prints it **once** |
| **3) Revoke a token** | asks for the **id** - the number in the first column of the list - and offers to disable it or delete it outright |

A token is a password: it is typed into the same form as the ones from the
environment. The label is for you, so a list of six tokens says who has which.

**Revoking uses the id, not the token.** You should not have to have the
secret in front of you in order to take it away - the whole point of revoking
one is usually that somebody else has it.

Changing a token does **not** log everybody else out. The gate's cookie is
signed with the *environment* passwords plus a secret of this installation, so
adding or revoking a database token leaves every open session alone. Changing
`DASHBOARD_GATE_PASSWORD` still ends every session, which is what you want
from that one.

### `manage_testuser.sh` - a database account for trying things

Creates (or removes) a role called `testuser` with **read and write** on the
archive: `processed_data`, `scraper`, `scraper_config`, `monitoring` and
`dashboard`, including the sequences and the default privileges, so tables
made later are covered too.

Write means `INSERT`, `UPDATE` and `DELETE` - it has to include the last one,
or an account meant for trying things out cannot clear up what it tried. It
does **not** include `TRUNCATE`, and it cannot create or drop tables:
`CREATE TABLE` in one of those schemas answers *permission denied for schema*,
which is the right answer for a login that is there to look at an archive
rather than to change its shape.

```sh
./manage_testuser.sh
```

Creating prints the password **once**, verifies that the account can really
connect, and prints a connection string you can paste into a client or into a
`.env`. The password is random and is not stored anywhere else - run it again
to get a new one.

Removing terminates the account's open sessions, drops what it owns and drops
the role.

This is for a person or a tool that wants to look at the archive directly: a
notebook, a BI client, a colleague debugging a query. It is **not** how the
components connect - they have their own roles, or run as the owner.

### `init/04-roles.sh` - the two least-privilege roles

Not run by hand in the ordinary case: the database container runs it once, on
the first start, if all four role variables are set in `.env`. See
[Least-privilege roles](#least-privilege-roles) above for running it on an
archive that already exists.

## When something is wrong

### The other components cannot reach it

The symptom is the same in every log: a connection that is refused, or a host
that does not resolve.

**`could not translate host name "xtracting_db"`** - the component is not on
the archive's network. Its `ARCHIVE_STACK` must be **exactly** the same string
as the database's; that value names the network. Check both `.env` files and
recreate the component.

**`connection refused` from inside a container, with `POSTGRES_HOST=127.0.0.1`**
- that address means "this container", and the database is in another one.
Inside the network the host is `xtracting_db` and the port is **5432**,
whatever `POSTGRES_PORT` says. `POSTGRES_PORT` is only where the database
appears on *your* machine. This is the single most common mistake when a
second stack is set up: the published port gets copied into every file, and
the components that run in containers must have 5432.

**`password authentication failed`** - the password in the component's `.env`
and the one in `database/.env` disagree. Note that the database only reads
`POSTGRES_PASSWORD` when the volume is **created**; changing it later changes
nothing until you either alter the role by hand or start over with `down -v`.

### The schema did not finish

If a component says *"this database is not a complete archive - missing: …"*,
the init scripts did not get to the end. Look at why:

```sh
docker compose logs xtracting_db | grep -iE "error|fatal" | head -20
```

A single failing statement stops the whole file, so everything after it is
missing - which is why the message names one table but usually means a
hundred. Fix the cause, then apply that file again by hand (see
[Applying a schema change](#applying-a-schema-change)) rather than recreating
the volume.

### `verify.sql` reports a FAIL

Each row names the check and what it found. The one that shows up on a grown
archive rather than a fresh one is **a compression or retention policy with the
wrong interval** - the numbers were changed after the archive was made. Apply
`init/05-policies.sql`, which is the one file that owns them:

```sh
docker compose exec -T xtracting_db \
  psql -U xtracting -d xtracting_archive -f /docker-entrypoint-initdb.d/05-policies.sql
```

It prints which policies moved. Re-running `03-scraper.sql` does **not** fix
this: `add_compression_policy(..., if_not_exists => TRUE)` looks at whether a
policy exists and never at its interval, so it returns happily and changes
nothing. That is why the numbers live in a file of their own, which drops a
differing policy before adding it.

`verify.sql` and `verify-scraper.sql` are **bind-mounted** into the container.
A tool that replaces the file rather than writing into it leaves the container
holding the old inode; `docker compose up -d --force-recreate xtracting_db`
re-resolves the mount.

### It is slow

Ask what it is doing:

```sh
docker compose exec xtracting_db psql -U xtracting -d xtracting_archive -c "
SELECT pid, now() - query_start AS running, left(query, 80) AS query
  FROM pg_stat_activity
 WHERE state = 'active' AND datname = 'xtracting_archive'
 ORDER BY 2 DESC;"
```

Two known shapes:

- **A query that plans for hundreds of milliseconds** and then runs quickly is
  scanning too many chunks. `scraper.documents` is chunked by the day, so a
  one-year window is 365 chunks to plan across. A larger `chunk_time_interval`
  is the remedy and it is a migration, not a setting.
- **`ILIKE '%something%'`** cannot use an ordinary index. `pg_trgm` indexes
  would help; none are created by default.

### The disk is filling up

```sh
docker compose exec xtracting_db psql -U xtracting -d xtracting_archive -c "
SELECT hypertable_name, pg_size_pretty(hypertable_size(format('%I.%I', hypertable_schema, hypertable_name)::regclass)) AS size
  FROM timescaledb_information.hypertables ORDER BY 2 DESC;"
```

`scraper.documents` holds the full text of every page fetched, once per
project it was collected for, and is normally the largest by a wide margin. If
`monitoring.service_log` is large, the step logging has been left switched on
- see the dashboard's [USAGE.md](../../dashboard/standard/docs/USAGE.md).

### Something wrote nothing at all

Check the grants before the code. Under the least-privilege roles a service
that lacks a grant fails quietly by design - the crawler's error log, for
instance, swallows its own failure rather than losing a four-minute crawl to
it.

```sh
docker compose exec xtracting_db psql -U xtracting -d xtracting_archive -c "
SELECT has_table_privilege('<role>', 'monitoring.service_log', 'INSERT');"
```
