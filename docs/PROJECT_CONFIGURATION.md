# Configuring an Xtracting project

How to write the two settings that decide what an extraction returns: the
**objects of interest** and the **perspectives**. Written for a person and for
an AI agent alike. The long version, with the reasoning behind every rule, is
the article [Setting Up Your First Extraction Project](https://xtracting.io/articles/f3f9d7845763).

Both settings are lists of `{ "name": ..., "description": ... }`. They are sent
to the model with every extraction the project runs, and they are the whole of
the basic configuration. Over the API they are written with
`PATCH /api/v1/project` (see [API.md](API.md)); in the dashboard they are two
cards on the project page.

---

## 1. Write down the result you want, first

One sentence:

> I want one entity per **X**, with **Y** and **Z** as its attributes, judged by **A** and **B**.

- **X** becomes an object of interest.
- **Y** and **Z** become the attributes you ask for in that object's description.
- **A** and **B** become perspectives.

If the sentence cannot be written, the configuration cannot be either. Ask the
person until it can.

## 2. Collect test documents and write expectations before the first run

Five to ten documents from the material that will really be processed, chosen
to differ:

- the longest and the shortest
- one that is mostly a table
- one in another language, if the material has them
- one that came out of OCR badly
- **one that should return almost nothing** - an unrelated press release, a page
  of boilerplate. This is the negative check: if it comes back with entities,
  the configuration matches things that are not in the text.

For each document, write down how many entities you expect and what each should
carry. Expectations written after the first run only describe the result you
happened to get.

When no real documents are available yet, generated ones can stand in for the
first rounds - but they must look like the real material (length, structure,
language, noise), and the person should confirm that they do.

## 3. Objects of interest - what one entity is

The **name** is the kind of thing one entity in the result stands for: something
a reader could point to in the text.

| Good | Weak | Why |
|---|---|---|
| `Supplier`, `Facility`, `Authority`, `Product` | `Stakeholder`, `Topic`, `Information` | a role or a subject matches little or everything |
| `Clause`, `Section`, `Chapter` | `Summary` | a part of the document becomes one entity per part |

Every mention becomes its own entity: a document naming eleven suppliers under
`Supplier` returns eleven entities.

The **description** is where you ask for the fields. Name them in this order:

- **Attributes** - the facts to bring back about it (numbers with their unit).
- **Connections** - which other objects it links to.
- **Events** - what happens to it, with dates.
- **Locations** - where, when it matters.

```
Supplier
  A named upstream party. Attributes: tier, share of spend,
  certifications, audit status. Connections: to the Organization
  buying from it and to the facilities involved. Events: audits
  and findings with their dates. Locations: the country and site.
```

**Rule for connections:** a connection joins two entities, so every object a
description connects to must be an object of interest of its own. Above,
`Organization` and `Facility` must both be on the list. A connection to
something that is not on the list comes back as an attribute or a sentence, not
as a link.

A general description ("A named upstream party.") is allowed. It lets the
model bring back whatever the document states, which is good for exploring
unknown material, but two documents then carry different fields. Name the
fields when a comparable table is the goal.

## 4. Perspectives - who the document is read for

The **name** is a **reader, a role, never a topic**: `Regulator`, `Investor`,
`Auditor`, `Affected Community`. `Environment` or `Governance` are topics: they
have no standard of their own, and their ratings all end up saying "good or
bad" in different words. If a topic matters, name the reader who cares about
it (`Environmental Regulator`).

The **description** says **how this reader judges** what they find and **what
matters** to them:

```
Auditor
  Reads as the person testing the claim. What matters: whether a
  figure is traceable to a stated method, whether the scope is
  defined, and where comparability breaks. Rate how verifiable
  the claim is as written.
```

- Three to five perspectives work well.
- Each must have a standard the others do not have; two with the same test give
  two identical ratings.
- Avoid two names sharing a word stem (`Investor` and `Investigative
  Journalist`): the post-processing may confuse them.

## 5. Limits

- 1 to 32 entries per list.
- Name 1 to 64 characters, unique within its list ignoring case.
- Description up to 1500 characters.
- The API refuses a list that breaks these with a 400 naming the field, so a
  changed limit shows up in the error rather than in a silently cut list.

## 6. Reading the first results

In this order, because each step decides whether the next is worth doing:

1. **The source record's importance per perspective.** A perspective that reads
   `Not Important` on every test document does not belong in the project.
2. **The number of entities** against the number you wrote down.
3. **Relation to source.** `Direct`/`Indirect` items cite a sentence; many
   `Unrelated` items mean the model answers from what it already knew.
4. **The reason** on a sample of items - the fastest way to spot a thing
   understood as something else.
5. **The attributes**, field by field, against the description and the text.

| What you see | Where it usually comes from |
|---|---|
| Fewer entities than expected | a name describing a role, not something in the text - or a thin document, which is correct |
| Many more, mostly noise | the name matches what the document is made of throughout; narrow it |
| Entities without attributes | the description says what the thing is but asks for nothing specific |
| The negative document returns entities | the configuration matches things that are not there; tighten names and descriptions |

## 7. Change one thing at a time

Change one setting, run the same documents again, compare against the same
written expectations. **Let the queue empty before changing anything:** a task
reads the project settings when a worker picks it up, not when it was
submitted, so a change mid-round splits the round across two configurations.

The configuration is done when the results match what was written down, and
the negative document comes back close to empty.
