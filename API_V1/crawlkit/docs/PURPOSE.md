# crawlkit - purpose

The crawl rules the crawler and the dashboard share: what a link on a list
page is, what the *form* of an address is, how a set of ticked links becomes
a rule, how one link is judged against the rules, what robots.txt allows, how
a page is fetched and turned into text, and how a file beside it is read.

It is a library. It has no process, no container and no configuration of its
own; it is copied into the crawler's image and the dashboard's image, and
both call the same functions.

## Why one library and not two implementations

The dashboard's test crawl promises what the crawler will collect later. If
the two were separate code they would drift - a trailing slash normalised
here and not there, a query parameter kept in one and dropped in the other -
and the drift would cost money: an address counted as two pages is a page
submitted and billed twice, and a page the preview promised that the crawler
never fetched is a document nobody knows is missing.

## The two halves

The **pure half** - hashing, forms, classify, legal, learn, apply, netguard -
needs nothing beyond the standard library, and every function carries its
examples as doctests, which are the specification. The dashboard's link
picker calls `learn()` on every click, and it should not have to pull in a
browser to do it.

The **I/O half** - robots, fetch, throttle, content, files, crawl, testrun -
touches the network and needs the packages in `requirements.txt`, pinned
there once for both services.
