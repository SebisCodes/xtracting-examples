# API_V1

The components for the Xtracting API, version 1: four services and one shared
library. Take the whole loop or one folder of it.

`database`, `collector` and `crawler` are general purpose - the same for every
installation. `dashboard/` holds one folder per dashboard version;
`dashboard/standard/` is the general-purpose one, and a dashboard for a special
case is a further folder here or a project of its own.

---

## What is in here

### [`database/`](database/) - an archive for your extraction results

PostgreSQL with TimescaleDB and a schema that fits everything the API returns.
Every row in every table carries the same three indexed columns - the project it
belongs to, the language it is in, and the task that produced it - so one
database holds several projects and every translation without them ever mixing,
and a project that outgrows the machine is split off by copying rows rather than
by untangling them.

Submitted content is also fingerprinted: the SHA-256 of each input is stored
alongside the result, so you can ask what you have already extracted rather than
guessing from filenames.

Extraction results are deleted from the platform an hour after they are
produced. This is where you keep the ones you want.

→ [`database/README.md`](database/README.md) · [`database/docs/`](database/docs/PURPOSE.md)

### [`collector/`](collector/) - fills the archive by itself

A small Python service that polls the jobs API every fifteen minutes and writes
what it finds. Start it once; submit work whenever you like; the results appear.

Idempotent, non-consuming, and cheap when idle - running it for a year does not
accumulate work.

→ [`collector/README.md`](collector/README.md) · [`collector/docs/`](collector/docs/PURPOSE.md)

### [`crawler/`](crawler/) - fills the archive from pages you watch

The collector brings in what you submit. This brings in what a web site
publishes: it reads a list page on a schedule, works out which links are new,
turns the pages and their PDFs into text, and submits them to Xtracting - from
where the collector picks the results up as usual.

It has no interface of its own. Everything a person does is done in the
dashboard's Watchlist pages, and the two services talk through the database.
Defaults are deliberately slow - every six hours per source, six seconds
between requests - and robots.txt is honoured unless somebody writes down a
reason not to, which the database insists on.

It is also the only thing here that can say which Xtracting projects exist:
it holds the keys, and a key is the only way to ask. So it asks - on start and
on a timer - and writes what it learned where the dashboard reads it. That is
why a watchlist is created inside a project and why the key list a person
chooses from is never a name they typed.

And it is the only thing that can submit, which is why *Crawl and send one
document* in the dashboard is a request rather than an action: the dashboard
writes a row, the crawler carries it out on its next tick - even while the
scheduled crawl is switched off - and the dashboard follows the document from
the queue into the archive.

→ [`crawler/README.md`](crawler/README.md) · [`crawler/docs/`](crawler/docs/PURPOSE.md)

### [`crawlkit/`](crawlkit/) - the crawl rules both of them run

A library, not a service: nothing in here starts, and there is no container.
The dashboard and the crawler import it, and each of their images carries a
copy.

It exists because those two have to agree exactly. The dashboard's *Test this
configuration* button promises what the crawler will collect later; if they
were two implementations, they would drift - a trailing slash normalised here
and not there - and the drift would show up as pages the preview promised and
the crawler never fetched, or as one address counted as two, submitted twice
and billed twice. So there is one `canonical_uri()`, one notion of the *form*
of an address, one link classification, one robots.txt verdict, one
`learn()`/`decide()` pair, and both services call them.

→ [`crawlkit/README.md`](crawlkit/README.md) · [`crawlkit/docs/`](crawlkit/docs/PURPOSE.md)

### [`dashboard/standard/`](dashboard/standard/) - look at what is in the archive

A web interface for the archive: counters and an activity feed, a search over
terms and over places with a map, events, charts on eight tabs across six
subjects with the rows behind every bar one click away, a map, a heatmap, a
connection graph - and the two settings pages that make the rest read properly:
buckets, which say that "Apple" and "Apple Inc." are one thing, and colour
groups for connection types. Every view prints, and every view hands you what
is on it as CSV or JSON.

It is also where the crawler is configured: which page to watch, which links on
it count, which files to send - with a test crawl that shows what *would* be
collected before anything is saved or enabled.

The port is bound to `127.0.0.1`, and the password gate is off until you
switch it on. Put your own reverse proxy in front of it before letting anyone
else in.

→ [`dashboard/README.md`](dashboard/README.md) · [`dashboard/standard/README.md`](dashboard/standard/README.md) · [`dashboard/standard/docs/`](dashboard/standard/docs/PURPOSE.md)

---

## How the four fit together

The archive is the middle of everything. The crawler and the dashboard close a
loop around it:

```
      ┌── you, in a browser
      │
      ▼
 ┌───────────┐  a source: which page to watch,   ┌───────────┐
 │ dashboard │ ───── which links count ────────► │  crawler  │
 └───────────┘        (scraper_config.*)         └───────────┘
      ▲                                                │
      │ reads the archive                              │ fetches the pages,
      │ (read-only)                                    │ sends them as text
      │                                                ▼
 ┌───────────┐        ┌───────────┐            ┌─────────────┐
 │  archive  │ ◄───── │ collector │ ◄───────── │  Xtracting  │
 │ (Postgres)│ writes └───────────┘  polls,    │  (the API)  │
 └───────────┘                       results   └─────────────┘
                                     live ~1 h
```

Any of them can be left out. Submit your documents by hand and the collector
still fills the archive. Run the collector alone and the archive fills without
anyone looking at it. Run the dashboard alone and you can read an archive
nothing else is writing. The crawler is the only one that needs company: it is
configured in the dashboard, and its results come back through the collector.

---

## Running all four

Each component is started on its own, from its own folder, with its own
`.env`. There is no file in this folder that starts all four: one that did
would be a second place where the same settings live, and the first question
after any of them fails to come up would be which of the two was actually read.

Everything else writes to the database or reads from it, so that one goes
first. The remaining three are independent of each other and can be started in
any order - or left out:

```sh
cd database
cp .env.example .env        # set POSTGRES_PASSWORD
docker compose up -d

cd ../collector
cp .env.example .env        # your API keys, same POSTGRES_PASSWORD
docker compose up -d

cd ../crawler
cp .env.example .env        # same keys and password, plus a user agent
docker compose up -d        # ~4 GB: this image carries a browser

cd ../dashboard/standard
cp .env.example .env        # same password; no API key needed here
docker compose up -d        # ~4 GB, for the same reason
```

Then open <http://127.0.0.1:8088> and choose a project.

Four separate Compose projects that find each other through a named network.
That is deliberate: you can run the database alone and write to it from your own
code, run the collector against a database you already have, or run the
dashboard against an archive nothing else is touching.

**`ARCHIVE_STACK` must be the same in all four `.env` files.** It names the
network, the Compose project and the volume; a folder with a different value
joins a network the database is not on and waits for a host that never
resolves. The services that need it say so rather than hanging quietly.

You will need an Xtracting API key for the collector and the crawler, which
means an account with a balance. Each key belongs to exactly one project, and
both services ask the API which - you never configure project names anywhere.
The dashboard needs no key at all: it reads the archive, and the projects it
offers are the ones the crawler's keys turned out to open.

**Order matters in `XTRACTING_API_KEYS`.** A watchlist collects into one or
more projects, and the key it uses for a project it names none for is the first
key of *that* project in this list which still works and may extract. If one is
switched off, collection carries on with the next - within the same project and
never across one, because another project's key would file the documents in the
wrong archive. A watchlist can also pin a key per project, and then a key that
disappears stops those pages rather than moving them to a key with a different
price; the dashboard says which watchlist stopped and why.

**One page, several projects.** A project decides what is extracted from a
document - its objects of interest and its perspectives - so two projects asking
different questions of the same page cannot share one extraction. Assigning a
page to both means it is FETCHED ONCE, which is all the site ever sees, and
SUBMITTED ONCE PER PROJECT, which is what it costs. The editor says so before
the second project is ticked, and the "have I had this address" gate is per
project, so a project added later collects the back catalogue rather than
nothing.

`XTRACTING_PROJECT_KEYS` is optional and only for keeping the file readable:
every key in both variables is probed the same way, so a key in the "wrong" one
still works. A key with **Read project** buys one thing - the dashboard can then
show what each key of that project is set to run with, and offer *lowest effort*
and *highest effort* when you pick one. It is read-only and cannot change the
project.

Both images that carry a browser can be built without one, about 4 GB smaller
each, if no source of yours needs JavaScript rendered:

```sh
(cd crawler            && docker compose build --build-arg CRAWLER_BASE=docker.io/python:3.12-slim)
(cd dashboard/standard && docker compose build --build-arg DASHBOARD_BASE=docker.io/python:3.12-slim)
```

Rendered sources then fail with a sentence that says exactly that.

## Container runtimes

You need one of **Docker**, **Podman** or **nerdctl**. Any of the three runs
everything here; the instructions use Docker because it needs the fewest words.

To use one of the others, substitute the command - nothing else changes:

```sh
docker compose up -d       # Docker
podman-compose up -d       # Podman
nerdctl compose up -d      # nerdctl
```

Rancher Desktop, Colima and OrbStack provide a Docker-compatible socket, so the
Docker commands work as written.

Four details in these files exist to keep that true, in case you wonder why
they look the way they do:

- **Images are fully qualified.** `docker.io/timescale/timescaledb:latest-pg18`,
  not `timescale/timescaledb:latest-pg18`. Docker quietly prepends `docker.io`
  to a short name; Podman refuses to guess and stops with *"did not resolve to
  an alias"*. Spelling out the registry costs nothing and works everywhere.
- **Bind mounts carry `,z`.** With SELinux enforcing - Fedora, RHEL, and the
  distributions where Podman is the default - a container cannot read a mounted
  directory that has not been relabelled, so the schema would never load. The
  flag is ignored on systems without SELinux, which is why it costs nothing.
- **The connection string is not assembled in Compose.** Each service is given
  its database credentials as separate variables and builds the DSN itself,
  because a DSN needs its parts percent-encoded and Compose does not do that.
  Given the password `p@ss/word`, Compose produces:

  ```
  postgresql://archivist:p@ss/word@db:5432/archive
  ```

  which libpq reads as the host `ss/word@db`. The application encodes each part
  before assembling them, which is the only place that can be done correctly.
- **The dashboard and the crawler are built from this folder.** Their compose
  files set the build context to `API_V1/`, because each image carries
  `crawlkit/` next to its own code - one copy of the crawl rules, not two
  implementations of them. Building from inside those folders cannot work: the
  library is not in the context.

Podman and nerdctl run this **rootless** without anything special: the published
ports are 5432 and 8088, both well above the 1024 that would need extra
privileges. If you set `POSTGRES_PORT` or `DASHBOARD_PORT` to something lower,
either change it back or configure `net.ipv4.ip_unprivileged_port_start`.

**The four folders share a named network.** `database/docker-compose.yml`
creates it; the other three join it as external. The database has to be up
first, or they will not start.

The name comes from `ARCHIVE_STACK` in the `.env` files - `xtracting` by
default, giving `xtracting-archive`. That one variable also prefixes the Compose
project and the data volume, which is what lets a second copy of this repository
run on the same machine without touching the first. Give the second copy its own
value in **every** `.env` it has, and the two share nothing: separate
containers, separate network, separate data.

Leave two copies on the default and they share all three - so `docker compose
down` in one pulls the network out from under the other, which leaves a database
that runs, reports healthy, and refuses every connection. If the values
disagree, the service that joins refuses to start and says so: *network
&lt;name&gt;-archive declared as external, but could not be found*.

## Requirements

A container runtime, and about 200 MB of disk for the archive to start with. It
grows with your extractions; TimescaleDB compresses anything older than a month,
which for this kind of data is usually a factor of five to fifteen.

The dashboard and the crawler images are about 4 GB each, because they are
built on the Playwright base and carry Chromium - that is what a source set to
*Rendered* needs. Both can be built on plain Python instead (see above) if you
never want a page rendered, which is the usual case.

To run the tests rather than the containers you also need Python 3.12 and, for
the crawler's rendered-mode test, Chromium: `python -m playwright install chromium`.

The database image is `timescale/timescaledb:latest-pg18`. PostgreSQL 18 keeps
its data at `/var/lib/postgresql` inside the container, not at
`/var/lib/postgresql/data`; the compose file mounts the volume there. Moving
an archive between major PostgreSQL versions is a dump and a restore, not an
image swap.

## Tests

Four jobs in CI on every push (`.github/workflows/tests.yml`), one per
component, and the same commands on a laptop.

**Never point a suite at the archive you keep.** The flow tests write into it
and remove their rows again by prefix, and a mistake there costs
you data rather than a red test. `database/.env.test` is committed for exactly
this: a second Compose project with its own network, its own volume and its own
port, holding nothing you care about.

```sh
cd database
docker compose --env-file .env.test up -d          # a throw-away archive on 55432
export DATABASE_URL=postgresql://xtracting:test_password@127.0.0.1:55432/xtracting_archive
```

Then, from `database/` where that left you - each block is a suite that can be
run on its own:

```sh
# 1. the schema, checked and then exercised
docker compose --env-file .env.test exec -T xtracting_db \
    psql -U xtracting -d xtracting_archive -f /verify.sql             # ALL n CHECKS PASSED
docker compose --env-file .env.test exec -T xtracting_db \
    psql -U xtracting -d xtracting_archive -f /verify-scraper.sql     # ALL n CHECKS PASSED

# 2. the collector
cd ../collector
python -m pytest tests/test_store.py -q     # the mapping, no database needed
python tests/integration.py                 # a stand-in API, the real database

# 3. the shared crawl library - run this first when something looks wrong,
#    a broken link rule fails here in seconds instead of in a flow test
cd .. && python -m pytest crawlkit -q

# 4. the crawler: unit tests and doctests, then the whole thing against the
#    stand-in web sites and a fake Xtracting
cd crawler
python -m pytest -q
python -m pytest tests -q

# 5. the dashboard: unit and flow tests in ONE command, plus the schema it
#    creates itself
cd ../dashboard/standard
pip install -r requirements.txt -r requirements-dev.txt
python tests/preseed/preseed.py
DASHBOARD_ALLOW_PRIVATE_HOSTS=true python -m pytest tests/unit tests/flow -q
cd ../../database && docker compose --env-file .env.test exec -T xtracting_db \
    psql -U xtracting -d xtracting_archive -f - < ../dashboard/standard/sql/verify.sql
```

Each `verify` suite checks the structure and then proves the behaviour. The
archive's writes a task in two languages, joins across the tables, forces a
chunk to compress and reads back out of the compressed one; the crawler's makes
the constraints refuse what the editor refuses and a split tag still reconcile;
the dashboard's proves its own tables, the seeded colours and the rule that
exactly one group is the fallback. All three roll everything back, so they are
safe against an archive holding real data. A check that cannot be answered
prints FAIL rather than nothing, because a verification that goes quiet when it
breaks is worse than none.

`DASHBOARD_ALLOW_PRIVATE_HOSTS=true` is for the Sources tests only: the
stand-in web sites they crawl live on loopback, and the dashboard refuses
private addresses by design. It belongs in a test environment and nowhere else.

**One test run at a time, against one database.** The flow suite fills the
archive with a preseed and removes it again when its session ends - so two
runs sharing a database take each other's rows away halfway through. The
symptom is never "the preseed is gone": it is dozens of failures in files
nobody touched, which pass again when the suite is run alone. A Postgres
advisory lock (`dashboard/standard/tests/archive_gate.py`) makes a second
*process* wait. If you must run two at once, give each its own database.

When you are done, in `database/`:

```sh
docker compose --env-file .env.test down -v      # volume and all: it is gone
```
