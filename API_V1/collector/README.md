# collector

Fetches finished extractions from Xtracting and writes them into the archive.
It is the only component that writes `processed_data` - the tables every
dashboard reads. Start it once; submit work whenever you like; the results
appear.

## What you need

- the archive running (`../database`, started first - the collector joins the
  network the database creates)
- at least one Xtracting API key that may read its jobs

The crawler does not have to be running: the collector fetches whatever the
keys it holds have jobs for, whoever submitted them.

## Start it

```sh
cd API_V1/collector
cp .env.example .env
$EDITOR .env          # ARCHIVE_STACK and POSTGRES_PASSWORD as in ../database/.env, plus XTRACTING_API_KEYS
docker compose up -d
```

Check it:

```sh
docker compose logs --tail 20 collector
```

You want `collector started: N key(s), every 15 minute(s), …` and then a line
per key per round. `0 job(s) known to the API` is the system working with
nothing to fetch yet.

## Then

- [`docs/PURPOSE.md`](docs/PURPOSE.md) - what it does and why it exists
- [`docs/SETUP.md`](docs/SETUP.md) - every variable in `.env`, fetching by hand
- [`docs/USAGE.md`](docs/USAGE.md) - what one round does, test mode, step
  logging, restarting, and what to do when something is wrong
- [`docs/DESIGN.md`](docs/DESIGN.md) - why it is built the way it is
