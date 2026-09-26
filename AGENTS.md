# Instructions for AI agents

This file is for an AI coding agent (Claude Code, Codex, Cursor and the like)
working in this repository. Claude Code reads it through `CLAUDE.md`. A person
can read it too: it is the procedure the article
[Let an AI Set Up Your Extraction Project](https://xtracting.io/articles)
describes from the person's side.

## What this repository is

Tools around [Xtracting](https://xtracting.io), an API that turns documents
into structured JSON: an archive database, a collector that fetches finished
extractions into it, a crawler that submits web pages, and a dashboard. All of
it runs in containers. Start with `README.md` and `API_V1/README.md`; every
component has a `README.md` and a `docs/` folder.

## Where to learn the API - read these, not the web page

| What | Where |
|---|---|
| Every endpoint, capability, field and error, short | [`docs/API.md`](docs/API.md) |
| The same, machine-readable, always current | `GET https://api.xtracting.io/api/v1` (no key needed) |
| OpenAPI 3.1 | `https://api.xtracting.io/api/v1/openapi.json` |
| Overview for language models | `https://xtracting.io/llms.txt` |
| How to write objects of interest and perspectives | [`docs/PROJECT_CONFIGURATION.md`](docs/PROJECT_CONFIGURATION.md) |

If `docs/API.md` and the live `GET /api/v1` disagree, the live answer is right;
say so to the person. Errors are meant to be read: a 400 names the field, a 403
names what the key may not do. Act on the message instead of retrying the same
request.

## Task: set up an extraction project for the person

The person has created a project in the dashboard (from any template), created
an API key on it with **Extract data** and **Edit project** ticked, and handed
you the key together with a description of what they want to extract. Your job
is to turn that description into a working configuration, test it, get the
person's approval, and then switch the key off.

### Rules that apply throughout

- **Ask when anything is unclear.** One question too many is cheaper than a
  round of extractions on a guess. Ask before the first extraction, not after.
- **The key is a secret.** Put it in `.env` files only (they are git-ignored),
  never in a file you commit, a log or a command you echo.
- **Extractions cost the person money.** Say how many documents you are about to
  submit before each round, and keep rounds small (5-10 documents). Ask the
  person for the price per extraction shown in the dashboard's AI settings;
  every finished task reports what it cost in `costChf`, and the job its
  `totalCostChf`.
- **Change one thing at a time**, and only when the queue is empty (see
  `docs/PROJECT_CONFIGURATION.md`, section 7).
- **Results are deleted from the platform when read.** Reading a finished job
  without `?retain=true` removes its results. Either save every result to disk
  on the first read, or add `?retain=true` while testing.
- You cannot rename the project, change its description or switch it off with
  the key, and you cannot create keys. That is by design; the person does
  those in the dashboard.

### Steps

1. **Check the key.** `GET /api/v1/key`. It must say `usable: true`, and
   `capabilities.canExtract` and `capabilities.canEditProject` must both be
   true. If not, tell the person exactly which box is missing.
2. **Read the project.** `GET /api/v1/project`. Note the current objects of
   interest, perspectives, model and price, and this key's `prefix`.
3. **Set up the tools.** Install a container runtime if there is none (Podman
   or Docker - ask which the person prefers). Clone this repository, then set up
   `API_V1/database` and `API_V1/collector` following their READMEs, with the
   key in the collector's `.env`. The crawler and dashboard are optional; ask.
4. **Understand the use case.** Restate it to the person as one sentence -
   *one entity per X, with Y and Z as attributes, judged by A and B* - and
   wait for their confirmation.
5. **Test documents.** Ask for real documents first. If there are none, write
   5-10 sample documents that look like the real material (length, structure,
   language, noise), including **one that should return almost nothing**.
   Show them to the person.
6. **Expectations.** For each document, write down the entities you expect and
   what each should carry. Save them next to the documents
   (e.g. `setup/expectations.md`).
7. **Configure.** Write the objects of interest and perspectives following
   `docs/PROJECT_CONFIGURATION.md`, and send them with
   `PATCH /api/v1/project`. Show the person what you sent.
8. **Run a round.** Submit the documents with `POST /api/v1/batch`, poll
   `GET /api/v1/batch/{jobId}?retain=true` until the job is complete, and save
   the results under `setup/rounds/<n>/`.
9. **Compare.** Read the results in the order of
   `docs/PROJECT_CONFIGURATION.md`, section 6, against the expectations. Write
   down what matched and what did not.
10. **Iterate.** Change one thing, run again. Stop after the results match, or
    after about five rounds, and report what is still off.
11. **Ask for approval.** Show the person the final configuration, a summary of
    each round and the remaining differences. Ask them to check a few results
    themselves against the documents. Do not continue without an explicit yes.
12. **Switch the key off.** After the yes:
    `PATCH /api/v1/project/keys/{prefix}` with `{"isActive": false}`, where
    `{prefix}` is this key's prefix. Confirm with `GET /api/v1/key` that it
    now reports `usable: false`. Tell the person that only the dashboard can
    switch it back on, and that for regular operation they should create a new
    key with **Extract data** only and put that one into the collector's
    `.env`.

If anything goes wrong mid-way - costs rising unexpectedly, results you cannot
explain, a key you suspect has leaked - stop and switch the key off first,
then tell the person. `PATCH /api/v1/project` with
`{"deactivateAllApiKeys": true}` switches off every key of the project at once,
including keys other programs of the person's use - so only when they ask for
it, or when a leaked key cannot be identified.
