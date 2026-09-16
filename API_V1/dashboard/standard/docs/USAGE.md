# Dashboard (standard) - using it

## Restarting, stopping, upgrading

```sh
cd API_V1/dashboard/standard
docker compose logs -f dashboard      # watch it
docker compose restart                # the same container, the same settings
docker compose down && docker compose up -d     # after a change to .env
docker compose build && docker compose up -d    # after a change to the code
docker compose stop                   # stopped until the next up
```

Compose reads `.env` when a container is **created**: `restart` keeps the old
values, `down` and `up -d` pick up the new ones. A changed password or port
needs `down` and `up -d`.

The dashboard applies its own SQL (`sql/*.sql`) on every start, so a change
to its own tables needs nothing else. Your browser picks up new stylesheets
and scripts by itself: their addresses carry a stamp taken from the newest
modification time under `app/static/`, read once at start, so a changed file
has a changed address after the restart.

Nothing saved is lost by a restart. A test crawl that was running is lost with
the process: its queue is in memory, its snapshot row stays `RUNNING` and
nothing finishes it. Press *Test this configuration* again; the new process
takes it.

## The top bar

**Project and language** decide which rows every view reads. The list comes
from what is in the archive, so a project appears only after its first
document has arrived - and stays after its configuration is deleted.

**Perspective and Show only** sit beside them and decide **which sources any
number in this dashboard is allowed to come from**:

| | |
|---|---|
| **Perspective** | the question the archive was read with - *Maintenance*, *Compliance*, *Investor*. **All** is the default and filters nothing. |
| **Show only** | *… Importance and above*. Greyed out while Perspective is **All**, because "important for what?" has no answer without one. Default once a perspective is chosen: **Low Importance and above**. |

It applies to **Query, Events, Diagrams, Map, Heatmap and Graph** - every
chart, every drilldown, every export, the same predicate in all of them, so a
CSV and the screen it came from can never disagree. Each of those views says
under its heading which perspective and threshold it is showing, because a
number that is a subset has to say so.

**A source with no rating for that perspective is hidden.** That is the whole
point - the perspective is the reason a document is in the answer - but it
does mean an archive collected before the perspective existed goes quiet
under it.

Deliberately outside it:

- **the Dashboard**, which is the one place that shows the whole system,
  perspective or no
- **the Log, buckets, colour groups and watchlists**, which are configuration
  rather than findings

Two honest limits, both visible rather than papered over. The perspective
list is harvested from the ratings actually present, so a perspective used
only in a source's importances is not offered. And the importance scale is
held in one language: where the extraction wrote a translated label, the
filter **falls back to showing the row** rather than silently emptying the
view - better a number that is too large and says so than a blank page that
looks like an empty archive.

**Print and Export** are on every view. Export hands you what is on the
screen - the same query, the same filters - as CSV or JSON; Print lays the
view out for paper.

## Is anything alive?

Two small pills sit at the right of every page title: the crawler and the
collector, each with its last heartbeat and a green or red dot. Each service
writes its row every minute; a row older than three minutes reads as offline.

Red does not always mean broken. A crawler that is switched off still writes
its heartbeat - the two facts are separate, and the page shows them
separately.

## The switch at the top of the Watchlist

**Scheduled crawling.** One switch over the whole watchlist, off on a new
archive on purpose: a crawler that starts fetching and submitting the moment
it is deployed spends money before anybody has looked at a page. It is the
`scraper.paused` row of `dashboard.settings`, which the crawler reads every
tick, so it takes effect in seconds and nothing in flight is killed. The line
beside it says whether a crawler is there to do it: a switch that is on with
no crawler running is a plan, not a state.

## Step logging

The crawler and the collector can each write a row per step into
`monitoring.service_log`, which the Log view shows under **Crawler** and
**Collector**. Both are off by default and there is no button for them: the
switch is a row of `dashboard.settings` - `crawler.debug` or
`collector.debug`, set to `true` - which the empty tab names. A change takes
effect on that service's **next pass**, not at once - the crawler on its next
tick, the collector at the start of its next round - because a service that
re-read the setting mid-round would write half a round and leave you
wondering about the other half.

Switch it on while you are watching something, and off again afterwards. The
rows are dropped after 24 hours in any case.

## The four steps of a watched page

| Step | What it asks | Opens when |
|---|---|---|
| 1 Where to look | name, address, **which projects read it**, how the page is read | at once |
| 2 What the page answers | *Test this configuration* - one fetch, from the dashboard, writing nothing and costing nothing | there is an address |
| 3 What to collect | *Choose the links*, then the mode, the format and the rules it learns | a test has answered |
| 4 When and how often | interval, politeness, the site's own rules, notes | something is collected |

A step that cannot be taken yet is folded and says which control unlocks it;
one that can be taken opens by itself; one already taken stays open. Any of
them can be opened by hand - the folding is a recommendation about order, not
a gate. **Saving, deleting and going back always work**, at any point.

**Save and Enable are two separate acts.** A saved page stays off until its
`Crawl on a schedule` tick is on, and nothing is crawled while the switch at
the top of the Watchlist is off.

## One page, several projects

A project decides what is extracted from a document - its objects of interest
and its perspectives - so two projects asking different questions of the same
page cannot share one extraction. Assign a page to both and it is **fetched
once** (the site sees one visitor) and **submitted once per project** (two
extractions, two bills). *Configure projects* says what the second one costs
before it is ticked.

Because of that, "have I had this address before" is a question about a
project. A project added to a page a year later collects that page's back
catalogue rather than nothing.

## Trying one real document

Under the four steps, a saved page offers **Crawl and send one document**. It
really crawls and really submits - without waiting for the schedule, and
**even while the crawl switch is off**, because a run somebody just asked for
and is watching is not the crawler running. It costs one extraction per
project.

The panel then follows the document through three stages, which fail in three
different ways and want three different remedies:

1. **the crawler** - did it take the request, and did it find anything new
2. **Xtracting** - was the document accepted for extraction, or refused
3. **the archive** - has the collector fetched the result back

While a run is open the collector looks every 30 seconds instead of every 15
minutes, so this normally settles within a minute of the extraction
finishing.

## The line on every "per period" chart

Every time chart on the Diagrams pages - *Sources per period*, *Ratings per
period*, *Events per period* and the rest - draws a **second series on an
axis of its own at the right: how many sources the archive evaluated in each
period**, over the whole project, whatever the chart itself is about. The bars
are the count; the line is what they came out of. A month with twice the
ratings and twice the sources is a month with more reporting, not a month
with more to say about the subject - and without the line the two draw the
same picture.

The line follows the reader's perspective filter like every other count on
the page, and it is not narrowed by the search: under a search for Apple the
bars are Apple's and the line is still every document of the period. It is
in the key beside the chart (and can be hidden there), in the numbers table
under it, in the enlarged view and in the CSV export as its own dataset,
`sources_evaluated`. It is not clickable: there is nothing behind it that the
chart could list. The one chart that carries no line is *Sources per period*
with nothing searched for, whose bars are that count already.

## What is behind a hot spot on the Heatmap

A click on a hot spot lists the entities standing in it, the ones with the
most locations first. The **Source** cell of each row counts the documents
that put that entity there - "2 documents" - and it opens: press it and the
documents are listed directly under the row, the newest first, each one a
link to the document itself with the place as that document named it, its
type, the address, the date and the document's summary. Twenty at a time;
the next twenty load as you scroll to the end of them, or on **Load more**.
Press the cell again to fold them away - they stay loaded, so a second look
costs nothing.

## The Tables view

Eight tabs, the same eight the Diagrams pages have, and each one a table of
that kind: documents, ratings, entities, connections, locations, market
insights, attributes, events. **Name** and **Type** narrow it; on the Sources
tab the name is the document's title or host. **Sort** is *Newest* or
*Oldest* - by the moment the archive received the row, which is the order
the archive is stored in and the one it answers fastest; the date in the
row stays the row's own - or *Most rows* and *Fewest rows*, where a row is one name and type
and says how many rows carry it - press the count to list them under it. A
connection is read from each of its ends, so "Foxconn is a Supplier of Apple
Inc." and "Apple Inc. is a Customer of Foxconn" are two rows of one tie.

**Show buckets** lists the buckets of the tab's kind first - a document type
bucket on Sources, entity and entity type buckets on Entities, colour groups
on Connections - each one opening into the documents that hold one of its
members, the newest first, more as you scroll.

Every row of every table, and every row of a drilldown dialog, carries
**Rows** beside its document: it opens the Sources tab with that one
document, already opened - one table per kind of everything the archive
read out of it, each with its count and the newest first. A document on the
Sources tab opens the same way from its own row. *Show all documents* clears
the one document again.

## Settings search only when you press Search

On every view with a search form - Map, Heatmap, Graph, Events, Diagrams,
Query - the switches and fields beside the term are settings for the next
search: connection levels, Show, Group by, Search by, the date range and
its presets. Pressing one writes it into the address bar and changes
nothing on screen; **Search** (or Enter in the field) asks the question
with all of them at once. The Map says so under the picture ("Press Search
to draw the map with these settings") until you do.

## What a pin and a line on the Map mean

**Show** decides what a pin is. *Connections* draws every entity once, at
its **main location** - the address the archive gives it most often - and
the lines between them; *Locations* draws every address of the thing you
searched for and no lines. A main location is blue, any other address of an
entity is grey, and the pin of the entity you searched for carries a ring in
its head and says "searched" in its row.

One name is one entity. The extraction hands the same company several ids
over time, and the map folds them into one pin and one line per partner,
with their connections added up; the popup says how many archive entries
went into a pin. **The popup opens rows**: press the address in a pin's
popup and the location rows behind that pin open in the drilldown dialog;
press a sentence in a line's popup - "Foxconn is a Supplier of Apple Inc."
- and the connection rows that sentence adds up open, read from Foxconn's
side; press the count above the sentences and every connection of the pair
opens. The small maps on Query and Diagrams do the same. A line is drawn in
every colour its connection types have,
each along the share of its length that type's count is of the pair's total
- a pair that is mostly Supplier and partly Competitor is mostly teal and
partly pink. The swatch in the row beside the map is the same bar.

**What a level is.** Level 1 is everything the searched entity is
connected to. Level 2 takes every entity of level 1 in turn and draws what
a search for it would draw at one level - all of its connections, under
every id the archive holds it by; level 3 does the same for every entity
of level 2. So an entity on the last level shows only its links back to
the level before: its own network is one level further. While the answer
is built, a bar under the map says which level is being followed and how
many of its entities are done.

Every connection in the archive is fetched for each level, up to
`DASHBOARD_MAP_MAX_EDGE_ROWS` rows per level (50,000 by default), busiest
entities first. Past that the page says the network was cut; fewer levels
give the complete picture.

## What each Diagrams tab ends in

Three tabs end in rows rather than in a picture, under the charts and behind
a rule headed **Summary**, because a bar is the wrong answer to the question
a reader arrives with there:

| Tab | The list at the foot |
|---|---|
| Sources | the newest documents, newest first by the date the document carries |
| Attributes | the newest attributes - *name: value unit*, with what the document said beside it |
| Events | the newest events, newest first by the date the event carries |

The list sits at the **foot** of the page and not at the top because twenty-
five rows are a screen tall; above the grid they would push the charts the tab
is named after off the first screen. It is drawn from the same search, period
and filters as the charts, and a search that resolved to nothing is not asked.

Two tabs carry a **map inside the grid**, not at the foot: Connections draws
the pins and lines of the Map view after the *by type* chart (what the ties
are, then where they run), and Locations draws the heat field of the Heatmap
view after the *by address* chart (which places, then where they cluster).
The other tabs - Ratings, Entities, Market Insights - are their charts.

**A short last row of charts is filled with faint empty cells.** Nine cards in
a grid four wide leave three holes, and a hole at the end of a grid reads as a
card that failed to load. On a narrow window, where the grid is one column,
there is nothing to fill and none are drawn.

## Buckets and colours

**Buckets** say that several spellings are one thing: "Apple" and "Apple
Inc." as one entity, "Company" and "Unternehmen" as one type, "t" and "tonne"
as one unit. A bucket has a kind, and every view resolves a typed term
through the buckets of its kind before it searches. They are rows in the
database, so a bucket a colleague made is a bucket everybody searches with.
Members are **ticked**: type into the member field, tick every row you want,
and each is a member the moment it is ticked; the field and the list stay,
so the next tick costs no second search. A ticked row ticked again is
removed. A name the list does not offer is typed and confirmed with its
type and *Add member*.

**Colours** decide which colour a connection type is drawn in, on the Map and
in the Graph. Eleven groups; every type in the archive is a row, in every
language; a type nobody has assigned takes the fallback group. *Suggest
groups* proposes an assignment for every unassigned type and saves nothing
until you say so.

## The Log, four tabs

| Tab | What is in it | Kept |
|---|---|---|
| Problems | one row per thing that went wrong, with the sentence saying what to do - and one row of kind *Manual run* per crawl asked for by hand, saying what it sent | 90 days |
| Runs | one row per crawl: pages, links, accepted, submitted, how long | for ever |
| Crawler | every step the crawler took, while its switch is on | 24 hours |
| Collector | every step the collector took, while its switch is on | 24 hours |

## Backing up what you configured

The watched pages, their rules, the colour groups and the buckets are
configuration, not data, and they live in `scraper_config` and `dashboard`:

```sh
cd ../../database
docker compose exec -T xtracting_db pg_dump -U xtracting -d xtracting_archive \
  --schema=scraper_config --schema=dashboard --format=custom > config.dump
```

## When something is wrong

### A change I deployed is not on screen

First check that you are looking at the dashboard you think you are:

```sh
ss -ltnp | grep 8088          # or whatever DASHBOARD_PORT says
docker ps --filter name=dashboard
```

Another program answering on that port looks exactly like a dashboard that
will not let you in.

Then check that the container really has the new code:

```sh
docker compose exec dashboard grep -c "the-thing-you-changed" /srv/app/static/js/sources.js
```

If it is there and the screen still disagrees, force a reload
(Ctrl-Shift-R). Static addresses are stamped from the newest modification
time under `app/static/`, taken once at start, so a restart after a change is
enough - but a service worker or a proxy in front of the dashboard can still
cache.

### The password will not let me in

The log says at start-up which sources can open the gate and whether the
bot check is on, and on every attempt what happened:

- **`gate: DASHBOARD_GATE_PASSWORD is empty, so the gate is in force only
  while dashboard.access_tokens holds a live token - database/manage_tokens.sh
  lists them. With none, every page is open.`** - no password configured;
  with no live token either, there is no gate.
- **`gate: N password(s) from DASHBOARD_GATE_PASSWORD, and any live row in
  dashboard.access_tokens`** - the gate is on; either source opens it.
- **`gate: the Cloudflare check is configured but DASHBOARD_TURNSTILE_SECRET
  is empty. NOBODY CAN PASS THE GATE until it is set`** (or `_SITE_KEY`) - one
  Turnstile variable is set and the other is not, so the gate refuses
  everybody. Set both or neither.
- **`gate: the bot check said no (...)`** - Turnstile is configured and did
  not agree. A production site key cannot validate on `127.0.0.1`; comment
  both variables out for local work.
- **`gate: refused`** - the password matched nothing.
- **`gate: throttled`** - the address has used up its tries; see
  [SETUP.md](SETUP.md#locking-it) for the count and the reverse-proxy
  setting behind it.

Passwords are comma-separated `label:password`. A comma **inside** a password
splits it into two entries, and a `$` or a backtick is read by the shell
before the dashboard ever sees it.

### The project list is empty

It is built from what has been collected, not from configuration - so it is
empty until the collector has written something. That is also why a project
keeps appearing in it after its configuration is deleted.

### A view is far emptier than it should be

Look at the two selectors beside the project: **Perspective** and **Show
only**. With a perspective chosen, a document is only in the answer if it
carries a rating for *that* perspective at *that* importance or higher - so
an archive collected before the perspective existed shows almost nothing
under it. Each of the six views says under its heading which pair it is
showing; set Perspective back to **All** and the numbers return.

The Dashboard is the exception and never narrows, so "the Dashboard says
4 000 sources and Query says 12" is the filter working rather than data
missing.

### A watched page says "403 Forbidden" or another code

The pill on the row is what the **site** answered, not what robots.txt says:

| It says | What it means |
|---|---|
| `403 Forbidden` | the site refused the request itself - usually a bot protection, not a robots.txt rule. Nothing was read and nothing was charged. |
| `401 Unauthorized` | the page needs a login; this crawler has none |
| `404 Not Found` / `410 Gone` | the list address has moved or was taken down - fix it in step 1 |
| `429 Too Many Requests` | the site wants a slower pace - raise the politeness delay in step 4 |
| `a bot check, not the page` | a captcha or challenge came back instead of the page; Rendered mode sometimes gets through |
| `no answer in time` | raise the timeout in step 4, or the site is simply slow |
| `robots.txt forbids the list page` | this one *is* robots.txt, and the page will not be fetched |
| `robots.txt not checked yet` | nobody has fetched this address yet - open it and press *Test this configuration* |

Hover any of them for the sentence that says what to do about it.

### The Crawler or Collector log tab is empty

Its switch is off: the `crawler.debug` or `collector.debug` row of
`dashboard.settings` is not `true`. Both start off and each takes effect on
that service's **next pass**. If it is on and the tab is still empty after a minute, check
that the service is running at all (the two pills at the right of the page
title) and, under least-privilege roles, that it has `INSERT` on
`monitoring.service_log`.

### A watched page will not save

The form refuses in a box that names the control:

- **no name / that name is taken** - names are how the log and the runs are
  read back, so each needs its own.
- **no project** - a page that collects into nothing would crawl a site and
  throw the result away. *Configure projects* in step 1.
- **a crawl against the site's rules needs a written reason** - the database
  has a CHECK for this. Write who allowed it and when.

### A key says it no longer works

The crawler holds the keys and asks Xtracting what each one opens; the
dashboard only shows the answer. A key struck through has stopped
authenticating - check `XTRACTING_API_KEYS` in `crawler/.env`, and see the
crawler's [USAGE.md](../../../crawler/docs/USAGE.md#a-key-stopped-working).

### "Rendered mode is not available in this build"

The image was built without the browser. Either use HTTP mode for that page,
or build the image on the Playwright base (the default) - see
[SETUP.md](SETUP.md).

### The whole page is slow

The Diagrams and the Query run real aggregations. Two known shapes, both in
[the database's USAGE.md](../../../database/docs/USAGE.md#it-is-slow): a
query that spends hundreds of milliseconds planning is scanning too many
chunks, and a `%substring%` search cannot use an index.

### It will not start at all

```sh
docker compose logs --tail 40 dashboard
```

- **`waiting for the database (1/30): ...`** repeated until it gives up
  with `database did not become available` - it could not reach the archive;
  the first attempt also prints a hint about the address or the password. See
  [the database's USAGE.md](../../../database/docs/USAGE.md#the-other-components-cannot-reach-it).
- **`connected, but this database has no archive schema - processed_data.tasks
  does not exist`** - the credentials were right and the database was the
  wrong one. Point it at the archive from `../../database`, or load
  `database/init/01-schema.sql` into this one.
- **`DASHBOARD_SEED is false and the dashboard schema is not there`** - the
  dashboard's own tables are missing and `DASHBOARD_SEED=false` forbade
  making them. Set it to true for one start, or run the three files in
  `sql/` in the order `01`, `03`, `02`.
- **`router api_sources could not be loaded: crawlkit is not on the path`** -
  started outside the container without `PYTHONPATH=../..`; see
  [SETUP.md](SETUP.md#without-a-container).
