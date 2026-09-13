# The dashboard

## What it is for

This is the part that lets you look at the archive, and the part where you say
which pages the crawler should watch. The collector fills the database; this
reads it. Point it at the archive, open `http://127.0.0.1:8088`, choose a
project and a language, and everything on every page is about that pair.

It never writes to `processed_data`. What you change here - buckets, connection
colours, sources - lives in schemas of its own.

## What it is

A Python service in a container: FastAPI, Jinja2, plain ES modules, Chart.js
and Leaflet. There is no build step and no Node in the image; the JavaScript in
`app/static/js` is the JavaScript the browser runs, and the two libraries are
vendored (`app/static/vendor/`, see `VENDOR.md`) so that no page loads anything
from a CDN.

What it may touch, and how, is the design:

    processed_data.*    the archive. Read-only, always, from a pool whose
                        transactions are opened read-only by libpq itself.
    crawler.*           what the crawler did - read-only here. The Sources
    monitoring.*        page and the Log are windows onto the crawler's own
                        tables, which only the crawler writes.
    dashboard.*         its own: buckets, colour groups, the place list,
                        settings. Created and seeded by the app on start.
    scraper_config.*    the sources you configure. The crawler reads them;
                        it never writes them.

Only the last two are written from here, and only through a second connection
pool that exists for them.

`crawlkit/` - a sibling folder, not a package inside this one - is the crawl
code the Sources page runs when you press *Test this configuration*. It is the
same code the crawler runs later, which is the only way a preview and a crawl
cannot drift apart. That is why the image is built from the API_V1 folder
and why `PYTHONPATH` has to include it when you run the app by hand.

## There is no login, only a gate

**Anyone who gets past the port has the whole dashboard.** They can read every
row of every project in the archive, edit buckets and colours, add or change a
source, and start a test crawl from your network. There are no users and no
roles in this application - only one password gate in front of everything
(`app/gate.py`), which is off until `DASHBOARD_GATE_PASSWORD` or a token in
`dashboard.access_tokens` switches it on.

The port is therefore bound to `127.0.0.1` by `docker-compose.yml`, and it is
meant to stay there. To let anyone else in, switch the gate on, and put your
own reverse proxy in front of it and let that decide who gets through - the
authentication you already run for everything else is better than one invented
here. Do not publish `0.0.0.0:8088` and hope.

The test crawl is the sharper edge of this: it fetches an address *from this
container*. Addresses that resolve to loopback, private or link-local ranges
are refused (`crawlkit.netguard`, re-checked on every redirect hop), so the
dashboard cannot be used to knock on the doors of your internal network;
`DASHBOARD_ALLOW_PRIVATE_HOSTS=true` turns that guard off and belongs in a test
environment and nowhere else.

## What it does

Eleven views, all of them bound to the project and language in the top bar, all
of them printable and exportable.

**The top bar also carries a reading.** Beside Project and Language stand
**Perspective** and **Show only**: a document is judged once per perspective a
customer watches ("for an Investor this filing is critical, for a Sustainable
Customer it is beside the point"), and choosing one narrows Query, Events,
Diagrams, the Graph, the Map and the Heatmap to what came from documents
carrying, *for that perspective*, the chosen importance or a higher one.
Perspective starts on **All**, which is no filter; *Show only* is greyed out
until a perspective is chosen and then starts at **Low Importance and above**.
A document with no judgement for the chosen perspective is **hidden**. Every
narrowed view says so under its heading, the choice travels in the URL, the
cookies and the Export links, and the Dashboard, the Log, Buckets, Colours and
the Watchlist are never narrowed by it — the Dashboard counts what *arrived*,
which is the answer to "is the collector running".

**The Map obeys it in half, and says which half.** The filter decides where
the map STARTS — the entities the search term resolves to — and the network
drawn around them is then drawn whole. Filtering each hop of the expansion by
the importance of the document that recorded it would cut paths in the middle:
a supplier two steps away would vanish because the one document naming the
company between them is Low Importance, and the reader would be shown two
disconnected halves of a network with nothing saying a link had been removed.
The Map carries that sentence under its heading whenever the filter is on.

Two limits of the archive that this filter has to live with, both written into
the code rather than hidden: the perspective list is read from the judgements
themselves and not from `processed_data.perspective_types`, because the
collector harvests that table from ratings only; and the importance scale is
not bound to a language, so an importance label the vocabulary cannot order
(a translated one) is *kept* rather than dropped — the filter falls back to
unfiltered for that label instead of silently emptying the view.

| Page | What is on it |
|---|---|
| Dashboard | How much of what arrived, per period; the activity feed; the latest events |
| Query | Search by term, by address or by a circle around a place; results with a mini-map |
| Events | Events by entity or by type, with the entities each was about |
| Diagrams | Six pages (Summary, Entity, Source, Location, Market Insights, Events), eight tabs on each, 47 charts in all. Each page searches its own kind of thing on TWO axes, chosen by one switch in front of the field: **Object** is one thing (an entity, a document or its host, an address, a topic, one event) and **Type** is the whole class ("Company" is every company). Both resolve to the same entity set, so all eight tabs are drawn from either, and the line under the box says which axis answered and how big it is. The Summary's magnifier searches everything at once - an entity, a bucket, a document, a host, a place, a topic or an event type - and an empty box there is the whole project; it has no type axis, because it is already everything. Click a bar and the rows behind that point open in a dialog. A card with more bars than its plot has room for draws no labels and says so; **Enlarge** opens it at one row per category, and a click inside that opens the rows as a second dialog over it |
| Map | Entities that have coordinates, their connections drawn between them. The box reads its term on TWO axes, like the Diagrams pages: **Object** is an entity or a bucket, **Type** is an EVENT TYPE, and then the map holds every entity the events of that kind are about. **Save image** writes what is on screen to a PNG - with the colour key and the map credit in it, and without the controls |
| Heatmap | The same places as density, per area or per visible box. Its entity field carries the same Object / Type switch, AND-ed with the place; **Save image** writes the field and its key to a PNG. A click on a hot spot - or on its row in the list beside the map - opens what is INSIDE it: the entities standing there, the ones with the most locations first, a bucket as one row under its own name, twenty at a time and more as you scroll. It is the same drilldown dialog the Diagrams charts open, narrowed by the same place, entity, types and dates the field was drawn with |
| Graph | One entity in the middle, its connections in rings; expand a neighbour to walk on |
| Buckets | Name groups of values that mean one thing. Nine kinds: entities ("Apple" and "Apple Inc."), entity types ("Company" and "Unternehmen"), document types, places, location types, event types, attribute types, units, market topics. A search resolves the whole bucket and says so |
| Colours | Which colour a connection type is drawn in, grouped; plus the inverted view: every type and the group it is in |
| Watchlist | What the crawler watches: projects, keys, address, mode, patterns, files, schedule - with a test crawl before you save, and one real document afterwards |
| Log | What the crawler had trouble with, and every run it made, newest first |

Plus one page that is not in the top bar, reached from the Watchlist because
that is the only place a project is a question anybody is asking - and because
eleven buttons already fill 974 of the 1024 px the bar is laid out for:

| Page | What is on it |
|---|---|
| Projects | The Xtracting projects the crawler's keys open, the keys of each, and the one act that is the customer's: deleting a project's configuration. That takes the project off every watchlist and deletes the watchlists that had no other project; the archive keeps every document, and the project keeps appearing in the selector at the top of every view, because that list is built from what was collected rather than from the registry |

**The editor is four steps, and they are in the order the work is done in.**

| Step | What it asks | When it opens |
|---|---|---|
| 1 Where to look | the name, the address of the list page, which projects read it, how the page is read | at once |
| 2 What the page answers | *Test this configuration* - one fetch, from the dashboard, writing nothing and costing nothing | as soon as there is an address |
| 3 What to collect | *Choose the links* first, then the mode, the format and the rules it learned | as soon as a test has answered |
| 4 When and how often | cadence, politeness, the site's own rules, notes | as soon as something is collected |

Three cards, all open, in an order nobody can work in - the card headed *what
to collect* telling the reader to run a test that lives in the card below it -
would be a form read backwards. A step that cannot be taken yet is folded and
says which control unlocks it; one that can be taken opens by itself; one
already taken stays open,
because the page is the record of what was decided. Any of them can still be
opened by hand - the fold is a recommendation about order, not a gate - and
**saving, deleting and going back are never folded**, whatever step somebody is
on.

**A watchlist collects into one or more projects.** A project decides what is
extracted from a document, so two projects asking different questions of the
same page are two extractions: the page is fetched once and submitted once per
project, and *Configure projects* says what that costs before the second one is
ticked. Each project carries its own key. Both lists come from
`scraper.projects` and `scraper.api_keys`, which the crawler writes by asking
Xtracting what each of its keys is - the dashboard has no key and cannot ask.
Leaving a key on *Default* means the first working key of that project in the
crawler's own order; naming one pins it, and a pinned key that disappears stops
those pages rather than moving the work to a key with a different price. The
fallback never crosses into another project.

**And then one real document.** Under the four steps, a saved page offers
*Crawl and send one document*: a real crawl and a real submission, without
waiting for the crawler's schedule and even while the crawler's switch is off.
The dashboard cannot do it itself - the keys are in the crawler's environment -
so it writes a request the crawler carries out within seconds, and then follows
the document through three stages that fail in three different ways: did the
crawler find one, did Xtracting accept it, has the collector fetched the result
back. While a run is open the collector looks every half minute instead of every
fifteen. It costs one extraction per project, and the sentence above the button
says so.

Timeframes are `24h`, `7d`, `30d`, `90d`, `6m`, `1y`, `3y`, `5y`, and each one
picks its own granularity - `24h` is bars per hour, `5y` is bars per quarter.
The choice is in the URL, so a page is a link you can send to somebody.

Every view answers CSV and JSON on the same parameters the screen used:

```sh
curl "http://127.0.0.1:8088/api/export/events.csv?project=plant-docs&language=English&by=type"
curl "http://127.0.0.1:8088/api/export/query.json?project=plant-docs&language=English&terms=battery&address=Springfield"
```

A file holds what the screen holds, which means the parameters a view needs it
needs here too: a search is anchored to a place, so `terms` alone answers *"a
search needs a place or a point"* rather than scanning the whole archive, and
the Graph export wants its subject (`q=`) exactly as the page does.

`/api/docs` lists every endpoint with the parameters it reads.

## Setting it up

The archive database has to be running first - the dashboard joins its network.

```sh
cd ../../database
cp .env.example .env        # set POSTGRES_PASSWORD
docker compose up -d

cd ../dashboard/standard
cp .env.example .env        # same POSTGRES_PASSWORD
docker compose up -d
```

Then `http://127.0.0.1:8088`.

Podman, nerdctl and the other Compose-compatible runtimes work the same way -
see the [API_V1 README](../../../README.md#container-runtimes).

**Writing to a database somewhere else?** Set `DATABASE_URL` in `.env` to a
full connection string and remove the `networks:` blocks from
`docker-compose.yml`. The dashboard needs a PostgreSQL carrying the archive
schema, not specifically the one next door.

Otherwise the connection string is assembled in the application from
`POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB`, `POSTGRES_HOST` and
`POSTGRES_PORT`, exactly as the collector does it - a password containing `@`
or `/` breaks a URL built by hand, and silently.

**The image is about 4 GB**, because it is built on the Playwright base and
carries Chromium: that is what a source set to *Rendered* needs when you test
it. A dashboard that will never test a JavaScript-built list page can be built
without it:

```sh
docker compose build --build-arg DASHBOARD_BASE=docker.io/python:3.12-slim
```

The Sources page then says *"Rendered mode is not available in this build"*
instead of failing at the first rendered fetch. If you do keep the
browser, the `playwright` pin in `../crawlkit/requirements.txt` and the tag in
`Dockerfile` must name the same version - a newer pip package fails with
*"Executable doesn't exist"*, and that message never mentions versions.

### Running it without a container

```sh
pip install -r requirements.txt
PYTHONPATH=.. DATABASE_URL=postgresql://xtracting:...@127.0.0.1:5432/xtracting_archive \
    python -m uvicorn app.main:app --host 127.0.0.1 --port 8088
```

`PYTHONPATH=..` is not decoration: `crawlkit` is the folder next door, and
without it the Sources router is dropped at start-up with an error saying so.

### Running as a restricted database role (optional)

By default the dashboard connects as the archive owner, which is fine on a
machine only you reach. To have the database enforce what this application may
touch, set `DASHBOARD_DB_USER`, `DASHBOARD_DB_PASSWORD`, `SCRAPER_DB_USER` and
`SCRAPER_DB_PASSWORD` in **`../database/.env`** before the archive's first
start; `database/init/04-roles.sh` then creates a login that can read the
archive and write only the dashboard's own schema and `scraper_config` (see
[the archive's documentation](../../../database/docs/DESIGN.md#application-roles-optional)).

Those four variables are read by the *database*, not by this service. Here you
put the role it created into the ordinary connection settings:

```
POSTGRES_USER=xtracting_dashboard
POSTGRES_PASSWORD=...
```

`DASHBOARD_SEED` stays `true`: the script gives the `dashboard` schema to that
role, so the dashboard goes on creating its own tables and refreshing its
materialised view. Only set it to `false` for a database where the role does
not own the schema - then run `sql/01-dashboard-schema.sql`,
`03-places-view.sql` and `02-dashboard-seed.sql` once as the owner instead.

## Decisions worth knowing about

**Two connection pools.** Every view reads through a pool whose connections set
`default_transaction_read_only=on` at connection time. A mistake in a query
builder therefore cannot change the archive - the database refuses it. The
second pool exists for the two schemas the dashboard owns and is used nowhere
else. It is the same boundary `database/init/04-roles.sh` can enforce with real
roles; the pools make it true even when everything connects as the owner.

**The dashboard creates its own schema.** On start it waits for the database,
refuses a database without the archive schema, and then runs
`sql/01-dashboard-schema.sql`, `03-places-view.sql` and `02-dashboard-seed.sql`
in one transaction. All three are idempotent, so every later start is a no-op.
`DASHBOARD_SEED=false` turns creation off and only checks - for a database
where the application user may not create anything and someone ran the files by
hand.

**Address suggestions come from the archive, not from a geocoder.** The Query
page suggests places it has actually seen, and the coordinates it draws are the
ones the extraction produced. Nothing is sent anywhere to be looked up: a
dashboard that asked a geocoder would leak what you are searching for to a
third party, and would offer you places your archive cannot answer for. The
list is a materialized view refreshed every
`DASHBOARD_PLACES_REFRESH_MINUTES` - refreshed once at start too, because a
suggestion list a day old is worse than a fifteen-second wait.

**The host list beside it, for the same reason.** The archive stores a
document's URI, not its host, and cutting the host out of every URI in the
project on every keystroke costs 10.1 s per keystroke on an archive of 750,000
documents, which is a field nobody can type into.
`dashboard.source_hosts` (in the same SQL file, refreshed on the same timer)
holds one row per host with how many documents came from it - 13,594 rows
there - and the same lookups take 1.2 ms. The archive's schema is the
collector's and read-only to the dashboard, so an expression index on
`processed_data.sources` was not open to us. A host first seen since the last
refresh is not suggested yet; it is still found, because the search itself
matches the URI in the archive rather than this list.

**Buckets are names, not ids.** A bucket is a list of name plus type, matched
case-insensitively, and it works across languages and across tasks. Entity ids
are stable within a task set, but an archive holds many; a bucket that pinned
ids would quietly stop covering an entity the next extraction called by the
same name.

**A bucket has a kind, and a bucket wins over a literal match.** The nine
kinds are in `dashboard.buckets.text_kind`. Whichever
kind, the rule is the same: a term matching the bucket's NAME or any of its
members resolves to the whole bucket, and the result is shown as ONE thing
with the bucket named beside it. A bilingual type bucket is what makes
"Company" and "Unternehmen" one answer instead of two half-answers, and a
member of a bucket is therefore not offered separately in the vocabulary
dropdown for its kind - picking it there would silently defeat the merge. That
holds for the suggestion lists too, all of them, including the composed ones
(`entity_or_bucket` on the Map, the Graph and Events; `anything` behind the
Summary's magnifier), where the fold is per ROW because the rows are of
several kinds at once. The one carve-out is the page where a bucket is MADE:
the Buckets page asks `/api/suggest/entity?fold=0`, because a page that showed
only groupings would have nothing to make one out of - the same reason the
Colours page searches connection types unfolded.

**And a field only offers a bucket it can answer with.** A composed list
reads `dashboard.buckets` for its bucket half, and it reads only the kinds
the search behind that field resolves (`api_suggest.COMPOSED_KINDS`:
`entity_or_bucket` takes entity buckets, the Summary's `anything` takes
entity, location, market topic and event type). Without that filter an
archive's only bucket - "Fire", of kind `event_type` - would be the FIRST
offer of the entity field on the Map, the Heatmap, the Graph and Events "by
entity", and picking it there would not merely be empty but wrong: `scope.py`
looks a bucket up by kind, finds no entity bucket of that name, falls through
to the literal term and maps a Team called Fire in Portland while the four
event types are dropped without a word. One named object may not mean two
different things on two views.

**A document is never called by its identifier.** Measured on the production
archive rather than guessed: `sources.text_name` is `src_1416664` in the older
rows and an md5 checksum in the newer ones - a serial id in one era, a checksum
in the other, and never a title. The Dashboard's two
panels and the Events list printed it under every row ("Source: src_1127664")
and the Query card was headed with it. So the heading is built from what
actually exists - the DOMAIN of `text_uri`, and the address's own slug read as
words ("Council approves new bridge over the river") - and the rule is on the
VALUE, not on the field: an archive that does carry real names keeps them,
untouched. `app/source_names.py` is the one place that decides, the views that
show a worked-out name say so in one line under the list, and
`tests/flow/test_source_names_api.py` puts a document of each shape into the
archive and asks every endpoint what it answers.

**Connection types are grouped once, by the Colours page.** They are
deliberately not a bucket kind: `app/scope.py` reads a COLOUR GROUP wherever
it reads a bucket, so grouping them is done in one place and used everywhere.
One thing grouped in two places would sooner or later be grouped two
different ways.

**Until they are grouped, the colour column is not drawn at all.** The
suggestion that fills `dashboard.colour_group_types` ran only on the first
seed, so an archive whose connection types arrived afterwards held 0 rows
against 25,621 distinct types: every type resolved to the fallback, and the
Graph's Details panel printed eight different names beside eight identical
brown squares while both legends collapsed to one "Other". A swatch column
that is the same on every row asserts a coding nobody has made, so where
EVERY row falls back the swatch goes and one line takes its place - "No
connection type is grouped yet … Group them on the Colours page"
(`static/js/legend.js`, one sentence for the two legends, the Map's checklist
and the Graph's panel). Separately, the suggestion is asked again at start-up
whenever that table is EMPTY, so a real archive is not shipped monochrome; one
row means a decision was made and nothing overwrites it.

**Colour groups are keyed by the type name.** `processed_data.connection_types`
has one row per project *and language*, so a German archive spells a relation
differently from the English one. The Colours page therefore also has the
inverted view - every type with the group it is in - so assigning the
translations is one pass down a list rather than a hunt.

**Market Insights is what a source reports, never a recommendation.** The
archive holds outlooks (Rising, Neutral, Declining) and sentiments (Very
Negative to Very Positive) - a description of what a document said, not an
instruction to a reader. This dashboard shows that vocabulary and no other:
no Buy/Sell, no Put/Call, no "high impact opportunity". `app/vocabulary.py` holds the list and
`tests/unit/test_vocabulary_guard.py` scans the chart registry, the templates
and the JavaScript for the forbidden words, so the rule cannot be undone by
accident.

The COLOUR of that scale is red for negative and green for positive
(`--valence-*` in `app/static/css/app.css`), because it is the clearest
encoding there is. Red and green on market data
do read as buy and sell to anyone trained in finance, so the rest of the view
must not add to that reading: the wording stays as the archive publishes it
("Negative reporting", never "Sell"), there are no arrows, triangles or trend
glyphs on those charts, and Market Insights keeps the sentence saying it
describes what a source reported. The colour carries the valence; nothing else
may carry an instruction. Both halves of that rule are a list in
`app/vocabulary.py` - `FORBIDDEN_TERMS` and `FORBIDDEN_GLYPHS` - and the guard
scans the chart registry and the views that draw those charts for either. Left
and right arrows are deliberately not on the glyph list: "◀ Last 7 days ▶"
steps the period and "Parent → Child" names the direction of a connection, and
neither says anything about a value.

**A category may not wear a scale's colour.** Four hue families are reserved
and mean one thing each: red-to-green is valence, violet is relevance, blue is
a count, grey is "no reading at all". The two CATEGORICAL palettes - the
eleven connection colour groups in `sql/02-dashboard-seed.sql` and the
free-text chart colours in `app/charts` - are none of those things, so they
live in the hues nothing else claims: teal, amber and brown, olive, magenta
and rose, told apart from each other by lightness within a hue. A palette
picked by eye lands squarely on the reserved ones (a group colour that IS
`--count-4`, two greys beside "no data" on one screen), and "far enough from
every step of every scale" cannot be a contrast number: the three ramps together cover the
whole lightness range, so the only way to be far from a whole ramp is to be
out of its hue. `tests/unit/test_categorical_palette.py` measures both
palettes and says so.

**A chart label is drawn only when it has room for itself.** The rule is one
line and it is measured in pixels (`labelsFit` in `static/js/charts.js`): the
space per category against the label's own line height. Below that, no labels
are drawn at all - not rotated, not shrunk under the page's 16 px floor, not
thinned - because a list of names has no every-nth reading and a rotated smear
is what this replaced. The card then says how many categories there are and
where to read them, the tooltip and the numbers table always carry the full
text, and **Enlarge** opens the chart at one row per category so the popup
scrolls rather than squeezing it. A time axis is the exception: its categories
are periods in order, so it thins its ticks (`maxTicksFor`) instead of hiding
them.

**And the graph keeps the same rule.** A bubble's name truncated to its own
radius and never checked against the neighbour would, on a busy entity of 121
nodes, put 290 pairs of intersecting names on the screen. The names are laid
out by measurement (`layoutLabels` in `static/js/graph.js`):
the centre first, then the busiest bubbles, and a name whose box would touch
one already drawn is not drawn at all. The line under the toolbar says how
many were left out, and every name stays in the bubble's tooltip, in its
aria-label and in the panel beside the drawing.

**A cluster badge on the map is a count, so it is blue.** markercluster's
vendor green is 16 degrees from the positive end of the valence scale - a
colour with a meaning in this product, on a badge whose content is a number
of locations. It is `--count-2` on a `--count-1` halo, with the same
near-black ring every other symbol on those two pages carries.

**The heat key is a scale of the ramp, not a list of what happened to be
painted.** Rows taken one per observed colour would, on a skewed archive,
spend four of five rows on one visible orange (dE 1.34 between two of them,
below the just-noticeable difference) while 3,995 squares share the bottom
one. The rows are alphas spaced evenly between the palest and the darkest
the field actually paints, as many as can be told apart - at least a dE of 12
between neighbours - and each says which counts it covers.

**A dialog closes by its Cancel button or by the X in its top right, and by
nothing else.** One shell (`static/js/dialog.js`) builds every popup in the
product, so there is one answer to "how do I get out of this": no light
dismiss (a person drags across a drilldown to select text, and losing it to a
stray pointer-up costs them the query they were exploring) and no Escape - the
trade-off is stated at the top of that module along with the one line that
would restore the ARIA convention. Dialogs STACK: a click on a bar inside an
enlarged chart opens the rows over it and the chart is still there when they
close, with the focus going back to the chart that was clicked. Two levels,
never three.

**Every query is bound to a project AND a language.** Not just the ones where it
seems to matter. A summary that mixed two languages would count the same event
twice - a translation is a complete second set of rows - and the mistake looks
like growth rather than like a bug.

**Every query has a time limit.** `DASHBOARD_STATEMENT_TIMEOUT_SECONDS` (30 by
default) is set on the connection, not around the code, so nothing can hold a
connection open while somebody wonders why the page is white. A view that hits
it answers *"the query took too long and was stopped"* and says what to do -
narrow the timeframe or the search, or raise the limit.

**One toolbar rule, and it does not stop at a dialog.** Every control in a
row is the same height, their boxes share a top edge, and a label sits ABOVE
its control rather than beside one - written as CSS in `app.css`, and it holds
in every view in the navigation at both viewports. It holds in the DIALOGS
too - the enlarge popup, the drilldown over it, the map popup, the link
picker, the file checklist and the strip that asks before work is thrown
away. A dialog is not a view, and a rule that stops at its edge has "Load
more" sitting six pixels below "Download CSV" in every drilldown because a
`margin` from the
days when it was a block button under the rows travelled with it into the
footer.

**Print is a supported output, not a screenshot.** `print.css` drops the
navigation, the toolbars and the suggestion lists, keeps cards from being cut
across a page break, and fills a header line with view, project, language,
period and search, so a printed page says what it shows. A drilldown dialog
that is open is what the reader is looking at, so `export.js` copies it into
`<main>` before printing - a `<dialog>` lives in the browser's top layer, where
no stylesheet can reposition it, and printing without that copy gives you the
page behind the dialog. PDF export draws the same `<main>` and slices it onto
A4 pages rather than squashing a screenshot onto one.

**Map tiles come from wherever you say.** `DASHBOARD_TILE_URL` defaults to
OpenStreetMap's public server, whose policy is written for light, attributed
use - fine for a few people, not for a busy intranet. It is also how you get a
different BASEMAP: point it at a satellite or terrain service and every map in
the product draws on that, with no change to any code. A PDF export drops the
tile layer and says so in a notice when the tiles come from a host that does
not send CORS headers: they taint the canvas, and the alternative to the notice
is an export that throws.

**A saved map carries its own credit.** "Save image" on the Map and the
Heatmap writes a PNG of what is on screen: the drawing, the colour key from
the column beside it, a caption naming the project, the language and what is
drawn, and the ATTRIBUTION as a line of text at the foot - taken from the
map's own attribution control, so a deployment that changed the tile server
gets the credit it set. OpenStreetMap's terms require the credit to be shown
with the map, and a picture is the one case where it cannot be inferred from
the page around it. The zoom buttons stay out; a control means nothing in a
still image. When the tiles come from a host without CORS the picture is
saved without them and the page says so, rather than handing you a grey
rectangle.

**The test crawl runs in this process, one at a time.** So that you can find
out what a list page answers before the crawler container exists at all. One
worker thread, `409` while it is busy rather than a queue that grows, and a
watchdog that gives up after `DASHBOARD_TEST_TIMEOUT_SECONDS` and marks the
snapshot FAILED - a crawl stuck in a socket read would otherwise hold the only
worker for ever. All the addresses of one test are on one host, which is
exactly why a second worker is not the answer: that would be the burst of
requests the crawler's politeness delay exists to avoid.

**A missing router is a warning, not a crash.** The views were built in
parallel, and a partial build still has to start so the half that exists can be
looked at. The one case that is logged as an error instead is `api_sources`
failing to import `crawlkit`: dropped silently, the Sources pages would simply
be gone and every `/api/sources` call would answer 404, which reads like a
broken page rather than a missing path.

## Watching it

```sh
docker compose logs -f
```

One line at start-up saying the version, whether the dashboard schema was
found, and the refresh interval. `GET /healthz` answers `{"status":"ok"}` only
after the database is reachable and the archive schema is there, so a container
that reports healthy is one that can answer a query.

## Tests

Two suites. The unit tests need nothing; the flow tests need an archive.

```sh
pip install -r requirements.txt -r requirements-dev.txt
```

A throw-away archive to run them against - never the one you keep, because
these suites fill it with a preseed and delete that preseed again at the end:

```sh
cd ../../database
docker compose --env-file .env.test up -d          # its own volume, port 55432
export DATABASE_URL=postgresql://xtracting:test_password@127.0.0.1:55432/xtracting_archive
```

Then, back here:

```sh
cd ../dashboard/standard

# the pure ones: SQL builders, timeframes, places, scope, colours, vocabulary
python -m pytest tests/unit -q

# the small known archive the flow tests assert against
python tests/preseed/preseed.py

# everything, in ONE command - see the warning below
DASHBOARD_ALLOW_PRIVATE_HOSTS=true python -m pytest tests/unit tests/flow -q

# the dashboard's own schema, the way database/verify.sql checks the archive's
cd ../../database && docker compose --env-file .env.test exec -T xtracting_db \
    psql -U xtracting -d xtracting_archive -f - < ../dashboard/standard/sql/verify.sql
```

`sql/verify.sql` prints `ALL 15 CHECKS PASSED`: the six tables and the
recorded schema version, the seeded groups, that exactly one colour group is
the fallback and a second one is refused, that a colour must be six-digit hex,
that every seeded colour clears 3:1 contrast on white, that a bucket member
belongs to a bucket, and that the place view answers.

`DASHBOARD_ALLOW_PRIVATE_HOSTS=true` is there for the Sources tests: the
stand-in web sites they crawl live on loopback, which the SSRF guard refuses by
design. It belongs in the test environment and nowhere else.

The crawl library has its own suite, and it is worth running first - a broken
link rule fails there in two seconds instead of in a flow test a minute in:

```sh
cd ../.. && python -m pytest crawlkit -q
```

### One test run at a time

**Give a test run its own database, and do not start a second one beside it.**
The flow suite fills the archive with the preseed and removes it again when
its session ends. Two runs against the same database therefore take each
other's data away halfway through, and the symptom is never "the preseed is
gone": it is dozens of failures spread over files nobody has touched - empty
suggestion lists, zero counters, "0 cards" - that pass again when the suite is
run on its own.

`tests/archive_gate.py` takes a Postgres advisory lock for the length of a
session so the second *process* waits instead. It is counted per process,
which is why `pytest tests/unit tests/flow` in one command is right and two
commands in two terminals are not. If you really must run two at once, give
each run its own database.

If a run seems stuck for more than a few minutes, ask the database rather than
waiting:

```sql
SELECT pid, state, wait_event_type, wait_event, left(query, 60), now() - query_start
  FROM pg_stat_activity WHERE state <> 'idle';
SELECT pid, granted FROM pg_locks WHERE locktype = 'advisory';
```

A `granted = false` row is another run holding the archive - or a `psql` window
somebody left open on it.

## When something is wrong

| What you see | What it means |
|---|---|
| `connected, but this database has no archive schema` | the credentials were right and the database was the wrong one. Load `database/init/01-schema.sql` into it, or point at the one from `../database` |
| `waiting for the database` | normal at start-up, for a few seconds |
| `router api_sources could not be loaded: crawlkit is not on the path` | started without the API_V1 folder on `PYTHONPATH`. In the image that is `/srv`; by hand, `PYTHONPATH=..` |
| `DASHBOARD_SEED is false and the dashboard schema is not there` | the app was told not to create anything and nobody ran `sql/01-dashboard-schema.sql`. Run the three files in `sql/`, in the order `01`, `03`, `02` |
| the project selector says `No projects yet` | the archive has no rows. The list comes from the data, not from configuration - there is nothing to choose until the collector (or the crawler, through it) has written something |
| a chart is empty but the page works | the timeframe. Charts are cut to the period in the toolbar, and an archive filled yesterday has nothing in *Last 24 hours* |
| `the query took too long and was stopped` | `DASHBOARD_STATEMENT_TIMEOUT_SECONDS` was reached. Use a shorter timeframe or a narrower search before raising it - the limit is what keeps one page from occupying a connection |
| the map is grey | tiles are not loading. `DASHBOARD_TILE_URL` is wrong, or the container has no route to it |
| `Rendered mode is not available in this build` | the image was built with `DASHBOARD_BASE=docker.io/python:3.12-slim`. Correct, and only a limit on testing rendered sources |
| `another test is running` on the Sources page | one test crawl at a time, on purpose. It ends by itself at `DASHBOARD_TEST_TIMEOUT_SECONDS` at the latest |
| `This address points at a private network ... and cannot be tested` | the SSRF guard. It is doing its job unless the address really is a test site on this machine - then `DASHBOARD_ALLOW_PRIVATE_HOSTS=true` |
| `No crawler is running` on the Sources page | nothing is wrong with the dashboard: no crawler has written a heartbeat. Adding, changing and testing sources works without it; scheduled crawling does not |
| `network ...-archive declared as external, but could not be found` | either the database stack is not running - start `../database` first - or `ARCHIVE_STACK` here does not match the one in `../database/.env` |
