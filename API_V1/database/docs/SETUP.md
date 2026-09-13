# Database - setting it up

The archive. Everything else reads and writes here, and it creates the network
the other components join, so it starts first.

## What you need

Docker with Compose v2 (`docker compose`, two words), or Podman or nerdctl.
Nothing else - PostgreSQL, TimescaleDB and the schema all arrive in the image.

## Three commands

```sh
cd API_V1/database
cp .env.example .env
$EDITOR .env          # set POSTGRES_PASSWORD; it is the one value with no default
docker compose up -d
```

The first start takes a minute or two: the container creates the database, then
runs `init/01-schema.sql`, `02-vocabularies.sql`, `03-scraper.sql`,
`04-roles.sh`, `05-policies.sql` and `06-indexes.sql` in that order. **Wait for
it to finish before starting anything else** - a container can report "healthy"
while the schema is still being written, because the temporary server the init
scripts run against answers on the same socket.

The reliable way to wait is to ask for the last table the init makes:

```sh
docker compose exec -T xtracting_db \
  psql -U xtracting -d xtracting_archive -tAc \
  "SELECT to_regclass('monitoring.scraper_errors') IS NOT NULL"
```

When that prints `t`, the archive is ready.

## What to put in `.env`

| Variable | What it decides |
|---|---|
| `POSTGRES_PASSWORD` | **Required.** No default, on purpose. |
| `ARCHIVE_STACK` | The name of the Compose project, the network and the volume. Change it and you get a second, entirely separate archive on the same machine. The other components must be given the **same** value. |
| `POSTGRES_PORT` | Where the database appears on your machine. Bound to `127.0.0.1` only. |
| `POSTGRES_DB`, `POSTGRES_USER` | Rarely worth changing. |
| `DASHBOARD_DB_USER` / `_PASSWORD`, `SCRAPER_DB_USER` / `_PASSWORD` | Optional. Set all four and `04-roles.sh` creates two least-privilege roles instead of everything running as the owner. Leave them empty and it is skipped. See [USAGE.md](USAGE.md#least-privilege-roles). |

## Checking it worked

```sh
docker compose exec -T xtracting_db psql -U xtracting -d xtracting_archive -f /verify.sql
docker compose exec -T xtracting_db psql -U xtracting -d xtracting_archive -f /verify-scraper.sql
```

Each ends with a line like `ALL n CHECKS PASSED`. A `FAIL` row names the check
and what it found, and is a real answer rather than a stack trace.

## A second archive on the same machine

Copy the folder, give it a different `ARCHIVE_STACK` and a different
`POSTGRES_PORT`, and the two never meet - separate network, separate volume,
separate container names. The other components get the same `ARCHIVE_STACK`
and their own ports.

`.env.test` is committed for exactly this: a throw-away archive on port 55432
that the test suites run against.

```sh
docker compose --env-file .env.test up -d
```

## Starting over

```sh
docker compose down -v      # -v takes the volume, and with it every row
docker compose up -d
```

`down` without `-v` keeps the data; `down -v` is the only command here that
destroys anything.

## Podman, nerdctl

Substitute the command - `podman-compose up -d`, `nerdctl compose up -d` -
and nothing else changes. The image is fully qualified and the bind mounts
carry `,z`, so SELinux systems and rootless runtimes work as written.
