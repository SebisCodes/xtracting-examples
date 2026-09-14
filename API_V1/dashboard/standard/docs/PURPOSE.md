# Dashboard (standard) - purpose

The general-purpose web view of the archive: what is in it, how it changes,
where things are and how they are connected - and the place where the crawler
is told what to watch.

It reads the archive and never writes it. What it writes is its own -
settings, buckets, colour groups - and the crawler's configuration. Access
tokens are issued and revoked by `database/manage_tokens.sh`; the dashboard
reads them and stamps `date_last_used` when one opens the gate.

## What it shows

| View | What it answers |
|---|---|
| Dashboard | how much is in the archive, per period, and what arrived last |
| Query | which documents, entities, places, connections, readings and attributes are around a term or a place |
| Events | what happened, by the entity it is about or by its type |
| Diagrams | charts on eight tabs across six subjects, with the rows behind every bar one click away |
| Map | entities at their addresses and the connections between them |
| Heatmap | where the archive's addresses cluster |
| Graph | one entity in the middle and its connections around it, ring by ring |
| Buckets | which spellings are one thing - "Apple" and "Apple Inc.", "Company" and "Unternehmen" |
| Colours | which colour a connection type is drawn in |
| Watchlist | the pages the crawler watches, with a test crawl that shows what would be collected |
| Projects | which projects the crawler's keys open |
| Log | what went wrong, what ran, and what each service did step by step |

Every view prints, and every view hands you what is on it as CSV or JSON -
the Watchlist and Projects pages hand out the watchlist, the Log hands out
the tab that is open, problems or runs. Every view is narrowed by the same
project and language, chosen once in the top bar; the perspective and the
importance threshold beside them narrow Query, Events, Diagrams, the Graph,
the Map and the Heatmap, and leave the Dashboard, the Log, Buckets, Colours,
the Watchlist and Projects whole.

## Why it is one of several

The archive, the collector and the crawler are the same for every
installation. What people want to see of an archive is not: a dashboard is
written for a purpose, and this one is written for no purpose in particular -
it fits any archive and shows everything the schema holds. A dashboard for a
special case is a further folder beside this one, or a project of its own,
against the same archive.
