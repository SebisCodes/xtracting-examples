# database

The archive: PostgreSQL with TimescaleDB and the schema every other component
reads or writes. It creates the network the others join, so it is the first
thing to start.

## What you need

Docker with Compose v2 (`docker compose`, two words), or Podman or nerdctl.
Nothing else - PostgreSQL, TimescaleDB and the schema all arrive in the image.

## Start it

```sh
cd API_V1/database
cp .env.example .env
$EDITOR .env          # set POSTGRES_PASSWORD - it is the one value with no default
docker compose up -d
```

The first start takes a minute or two: the container creates the database and
then runs the files in `init/` in order. Wait for it to finish before starting
anything else - a container can report "healthy" while the schema is still
being written. The reliable way to wait is to ask for the last table the init
creates:

```sh
docker compose exec -T xtracting_db \
  psql -U xtracting -d xtracting_archive -tAc \
  "SELECT to_regclass('monitoring.scraper_errors') IS NOT NULL"
```

When that prints `t`, the archive is ready and the other components can start.

## Then

- [`docs/PURPOSE.md`](docs/PURPOSE.md) - what the archive is and what it holds
- [`docs/SETUP.md`](docs/SETUP.md) - every variable in `.env`, a second archive
  on the same machine, checking the schema
- [`docs/USAGE.md`](docs/USAGE.md) - psql, retention, roles, backups, the
  scripts, applying a schema change, and what to do when something is wrong
- [`docs/DESIGN.md`](docs/DESIGN.md) - why the schema is built the way it is
