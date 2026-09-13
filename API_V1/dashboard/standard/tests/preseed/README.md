# Preseed

A small archive with known contents, for the dashboard's flow tests and for
looking at the dashboard without a real archive.

```
DATABASE_URL=postgresql://... python tests/preseed/preseed.py            # load (idempotent)
DATABASE_URL=postgresql://... python tests/preseed/preseed.py --remove   # take it out again
```

Loading applies the dashboard schema files (`sql/01`, `03`, `02`, all idempotent),
removes an earlier preseed, writes the rows, refreshes `dashboard.places` and
commits - or nothing at all. Removing deletes every row whose project starts
with `_preseed`, the bucket, and only the colour assignments the preseed
itself added (their names are kept in `dashboard.settings` under
`preseed.colour_types`), so a customer's own assignment of "Supplier" survives.

## What is in it

Two projects, written the way the collector writes them: one complete row set
per language, the German rows under the **same task ids** with the **same entity
ids** and translated names, types and addresses.

| Project | Languages | Tasks | Purpose |
|---|---|---|---|
| `_preseed Alpha` | English, German | 6 | everything the views need |
| `_preseed Beta` | English | 1 | project isolation: its own Contoso, and a second Apple (Company) |

Entities in Alpha: **Apple Inc.** (Company, Cupertino) and **Apple** (Company) -
together the bucket `Apple` - and **Apple** (Fruit), which must stay out of it;
Microsoft (Redmond), Foxconn (Taipei / Taipeh), Tim Cook (Person), the European
Commission / Europäische Kommission (Regulator, Brussels / Brüssel),
**Springfield Works** (Springfield, Illinois, USA) and **Springfield Mills**
(Springfield, Ontario, Canada) for the place disambiguation, Nordwyk Pumps
(Rotterdam), Zurich Insurance (Zurich / Zürich), Linjiang Province (address
`Linjiang`, a single part).

Sources: six, from `news.example.com`, `filings.example.org` and
`blog.example.net`, with `date_written` 2 h, 3 d, 20 d, 60 d, 200 d and 2 y
ago (commissioned 30 min later, archived 10 min after that, so the chunk
prefilter holds). Summaries mention *battery*, *antitrust* and *pump*.

Connections: Supplier, Competitor, CEO, Regulator, Partner, Customer,
Subsidiary (all assigned to colour groups, in both languages) and an unmapped
**Mysterious Bond** that must take the fallback colour; reversed duplicates for
Supplier/Customer, Competitor and Subsidiary/Parent company.

Events: one per task, plus *Hearing scheduled* with no `event_entities` (about
the document only) and *Battery product launch* dated 30 days in the future.
Ratings: on Apple Inc., all seven values x two perspectives x three relations.
Attributes with and without `float_value`. Market insights covering every
outlook and every sentiment value. Vocabulary rows for every open type in
both languages; the closed vocabularies (rating values, outlooks, sentiments,
importance, relation to source) stay as the API publishes them.

The dates are relative to the moment of loading; a test that asserts on
"last 24 hours" is testing against a preseed loaded minutes ago, which the
flow conftest guarantees.
