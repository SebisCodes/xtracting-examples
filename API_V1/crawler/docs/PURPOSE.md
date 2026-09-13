# Crawler - purpose

The crawler brings in what a web site publishes. The collector fills the
archive with what you submit; the crawler is what submits, on a schedule, from
pages you have said to watch.

For each watched page it reads the list page, works out which links on it are
documents and which of those are new, fetches them - and their PDFs, spread
sheets and other files - turns them into text and submits them to Xtracting.
The collector then fetches the results back into the archive as usual.

## What it promises

- **It follows the dashboard.** Which page to watch, which links count, which
  files to send, how often: all of it is configured in the dashboard's
  Watchlist and read from the database. The crawler has no interface of its
  own.
- **It is slow by default.** Every six hours per source, six seconds between
  requests, at most ten list pages and twenty-five new documents per run, one
  request at a time per host. The site's own `Crawl-delay` wins when it asks
  for more.
- **It honours robots.txt** unless somebody writes down a reason not to,
  which the database insists on.
- **It fetches once and submits once per project.** A page assigned to two
  projects costs two extractions and one visit to the site.
- **It never pays twice for the same address** - two gates, both on the
  address rather than the content, and a submission counts as collected only
  once its result has come back.
- **It holds the keys and asks what they open.** It is the only component
  that can say which projects exist, and it writes what it learned where the
  dashboard reads it.

## What it is not

It is general purpose: it knows nothing about what you extract. What counts
as a document on a given site is decided by the rules a person ticks in the
dashboard, and those rules are `crawlkit`'s - the same code the dashboard's
test crawl runs.
