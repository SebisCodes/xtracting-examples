# Dashboard (standard) - setting it up

The web view of the archive, and the place everything else is configured
from. It needs no API key: it reads what the collector wrote and what the
crawler found.

**Start the database first** - the dashboard joins the network the database
creates.

## Three commands

```sh
cd API_V1/dashboard/standard
cp .env.example .env
$EDITOR .env
docker compose up -d
```

Then open <http://127.0.0.1:8088>.

The image is built from the `API_V1/` folder, not from this one, because it
carries `crawlkit/` beside its own code - the crawl rules the crawler runs,
so the test crawl and the real crawl cannot disagree. The compose file sets
the build context; `docker compose build` from here works, a plain
`docker build .` does not.

The Playwright base image is about 4 GB because it ships Chromium, which the
test crawl of a page set to *Rendered* needs. Without it:

```sh
docker compose build --build-arg DASHBOARD_BASE=docker.io/python:3.12-slim
```

The Watchlist then says "Rendered mode is not available in this dashboard
build" for those pages, and everything else works.

## What to put in `.env`

| Variable | What it decides |
|---|---|
| `ARCHIVE_STACK` | Must be **exactly** the same as in `database/.env`. It names the network. |
| `POSTGRES_PASSWORD` | The same one the database was created with. |
| `POSTGRES_HOST` | `xtracting_db` - the service name on the shared network. |
| `POSTGRES_PORT` | **`5432`**, not the port the database is published on. Inside the network everything talks to 5432; the published port is only for your own machine. |
| `DASHBOARD_PORT` | Where the dashboard appears on your machine. Change it if 8088 is taken - and check that it really is free, because another program answering on that port looks exactly like a dashboard that will not let you in. |
| `DASHBOARD_XTRACTING_URL` | Where the links to Xtracting point. Defaults to `https://xtracting.io`. |
| `DASHBOARD_TILE_URL` | Where map tiles come from. OpenStreetMap by default; a tile server of your own for an installation without internet access. |
| `DASHBOARD_SEED` | `true` by default: the dashboard creates its own tables on start. `false` when it connects as a role that may not. |
| `POSTGRES_USER` | `xtracting` by default. With the least-privilege roles from `database/.env`, the dashboard's role and its password go here and in `POSTGRES_PASSWORD`. |
| `DATABASE_URL` | Optional: a complete connection string to a database somewhere else. Then remove the `networks:` blocks from the compose file. |

The rest of the file is commented and rarely needs touching.

### Locking it

Empty means no gate, which is right on a laptop and wrong on anything with a
public address. Fill it in and every page asks for a password:

```
DASHBOARD_GATE_PASSWORD=alice:first-password,bob:second-password
```

Comma-separated, `label:password`. The label is only for the log - it never
appears on screen. **Avoid `,` (it separates entries), `$` and backticks (the
shell reads them), and `#` at the start of a value (a comment).** A colon is
fine inside a password; only the first one separates.

Remove an entry to withdraw that access - every session ends at that moment,
because the cookie is signed with the whole set - then `docker compose down`
and `up -d`.

**What the gate is, and what it is not.** It is a password gate against the
outside, nothing more. There are no user accounts, no roles and no per-person
rights in this dashboard, and none are intended: whoever is past the gate has
the whole dashboard. Its one purpose is that every person with access gets a
personal key that can be rotated or withdrawn at any time without touching
anybody else's.

**The alternative, without a restart: tokens in the archive.** Instead of, or
as well as, the list in `.env`, keys can live in `dashboard.access_tokens`,
managed with `manage_tokens.sh` beside the archive (see
[the database's USAGE.md](../../../database/docs/USAGE.md#the-scripts)):

```sh
cd API_V1/database
./manage_tokens.sh        # 1) list  2) add one, with a label and an expiry  3) disable or delete one by id
```

The dashboard reads that table every 30 seconds, so a token works or stops
working within half a minute, no restart, and nobody else's session ends. The
script needs bash and docker on the machine that runs the archive - Linux, or
a Docker-compatible shell; the table itself can be edited with any SQL
client.

Optionally, a Cloudflare Turnstile check in front of the password form:

```
DASHBOARD_TURNSTILE_SITE_KEY=...
DASHBOARD_TURNSTILE_SECRET=...
```

**Both or neither.** With only one set, the gate refuses everybody rather
than letting everybody in - a misconfiguration must not open the door.

For anything beyond one personal key per person, put a reverse proxy with
your own authentication in front of the port.

## Checking it worked

```sh
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8088/healthz
```

`200` means it is up. With the gate on, a page answers `303` (to the password
form) and an API call answers `401` - both are the gate working, not a fault.

## What to do first

1. Choose a project and a language in the top bar. The list comes from what is
   in the archive, so it is empty until something has been collected.
2. **Watchlist → Add a watched page.** Four steps: where to look, what the
   page answers, what to collect, how often. Each opens when it can be taken.
3. **Nothing is crawled until you switch it on.** The switch is at the top of
   the Watchlist and starts off.

## A second dashboard on the same machine

Give it the second archive's `ARCHIVE_STACK` and its own `DASHBOARD_PORT`.
Everything else is the same. Two dashboards on one archive - this one and one
for a special case - share the `ARCHIVE_STACK` and differ in the port.

## Without a container

For development, from this folder, with the archive published on your
machine:

```sh
pip install -r requirements.txt
export DATABASE_URL=postgresql://xtracting:<password>@127.0.0.1:5432/xtracting_archive
PYTHONPATH=../.. python -m uvicorn app.main:app --host 127.0.0.1 --port 8088
```

`PYTHONPATH=../..` is what makes `import crawlkit` resolve - it is a sibling
folder under `API_V1/`, not a package inside this one.
