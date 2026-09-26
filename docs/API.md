# Xtracting API v1 - reference for agents

The short, complete reference: enough to use every endpoint without opening
the web page. The authoritative, always-current descriptions are served by the
API itself:

- `GET https://api.xtracting.io/api/v1` - every endpoint with the switch it
  needs, its fields and errors, and the setup loop, as JSON. No key needed.
- `https://api.xtracting.io/api/v1/openapi.json` - OpenAPI 3.1.
- `https://xtracting.io/llms.txt` - overview for language models.
- `https://xtracting.io/api-docs/v1` - the page for people.

If this file and the live index disagree, the live index is right.

## Basics

- **Base URL:** `https://api.xtracting.io`
- **Auth:** `Authorization: Bearer <key>` (or `X-API-Key: <key>`; if both are
  sent, `Authorization` wins). A key is `<prefix>.<secret>` - 8 hex characters,
  a dot, 64 hex characters - and is always sent whole. The **prefix** names
  the key in URLs and responses.
- **Envelope:** every answer is `{"success": true, "data": {...}}` or
  `{"success": false, "error": {"message": "...", "code": "...", "docs": "..."}}`.
  `docs` links to the part of the API page that explains the refusal. The
  index at `GET /api/v1` and `openapi.json` are plain JSON without the envelope.
- **Bodies are strict.** An unknown field is a 400 naming it; a field that
  exists but may not be changed with a key is a 403 naming it. Read the message
  and correct the request - do not retry it unchanged.
- **One key, one project.** No request ever carries a project id: the key
  decides which project is read, changed and billed.

## Capabilities

A key has up to three, ticked when it is created in the dashboard. A key can
never widen its own capabilities, and cannot create keys.

| Capability | Dashboard label | Allows |
|---|---|---|
| `canExtract` | Extract data | submitting documents, reading the results; costs money |
| `canReadProject` | Read project | `GET /api/v1/project` |
| `canEditProject` | Edit project | reading and changing the project's configuration (includes read) |

## Endpoints

### `GET /api/v1/health`
No key. Whether the API is up.

### `GET /api/v1/key` - any valid key
What this key is: `prefix`, `name`, `usable` (and `reason` when false),
`capabilities {canExtract, canReadProject, canEditProject, readsProject}`,
`project {projectId, name}`, `createdAt`, `expiresAt`. A switched-off or expired
key still gets 200 here, with `usable: false` and the reason. **Call this
first.**

### `GET /api/v1/project` - Read project or Edit project
The project's configuration and every key on it:
`projectId`, `name`, `interestEntities [{name, description}]`,
`perspectives [{name, description}]`, `defaultHighThinking`,
`defaultTranslationLanguages`, `aiModelId`, `aiServerId`,
`translationModelId`, `modelLocked`, `modelChangedFromId`, `confidential`,
`openTasks` (tasks still queued or running), and
`apiKeys [{prefix, name, isActive, canExtract, canReadProject, canEditProject, highThinking, translationLanguages, mirrorLanguages, aiModelId, aiServerId, ..., url}]`.

### `PATCH /api/v1/project` - Edit project
Changes the configuration. Every field is optional; send only what changes.

| Field | Type | Meaning |
|---|---|---|
| `interestEntities` | `[{name, description}]` | the objects of interest - **the whole list, replacing the stored one** |
| `perspectives` | `[{name, description}]` | the perspectives - the whole list, replacing the stored one |
| `defaultHighThinking` | boolean | "Complex Documents": more reasoning, higher price |
| `defaultTranslationLanguages` | string[] | output languages besides English, names as in `translationTargets` at `https://xtracting.io/api/public/languages` |
| `aiModelId`, `aiServerId`, `translationModelId`, `modelLocked` | | the model; changing it changes the price - leave to the person |
| `applyToAllApiKeys` | boolean | copy the project's run settings onto every key that may extract |
| `deactivateAllApiKeys` | `true` | switch off **every** key of the project, this one included |

Lists: 1-32 entries, names 1-64 characters and unique ignoring case,
descriptions up to 1500 characters. How to write them:
[PROJECT_CONFIGURATION.md](PROJECT_CONFIGURATION.md).

`name`, `description`, `isActive` and `confidential` of the project are
refused (403, `NOT_A_CONFIGURATION_FIELD`): the person changes those in the
dashboard.

Which settings a job uses: the objects of interest and perspectives are the
project's, read **when a worker picks the task up** (so change them only with
an empty queue). Model, Complex Documents and languages are copied onto each
key when it is created; `applyToAllApiKeys` brings the keys in line.

Response: the configuration as in `GET`, plus `keysUpdated` /
`keysDeactivated` when those were asked for.

### `PATCH /api/v1/project/keys/{prefix}` - Edit project
Changes one key of this project: `name`, `highThinking`, `translationLanguages`,
`mirrorLanguages`, `aiModelId`, `aiServerId`, `translationModelId`,
`modelLocked` (each nullable where "null = the project's"), and `isActive`.

**`isActive` accepts only `false`: the kill switch.** The key is refused from
the next request on, and only the dashboard can switch it back on.
`canExtract`, `canReadProject`, `canEditProject`, `expiresAt` and `allowedIps`
are refused by name (403).

```sh
curl -X PATCH https://api.xtracting.io/api/v1/project/keys/3f9a2c81 \
  -H "Authorization: Bearer $XTRACTING_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"isActive": false}'
```

### `POST /api/v1/extract` - Extract data
One document: `{"content": "<plain text>", "source": "<URL or path>", "tag": "<optional, yours>"}`.
`content` up to 2,000,000 characters; long documents are split at submission
and each piece is billed as one extraction. Answers **202** with
`{jobId, totalTasks, status: "QUEUED", expiresAt, pollUrl}`.

### `POST /api/v1/batch` - Extract data
Many documents: `{"tasks": [{content, source, tag}, ...]}`, 1-1000 entries.
Same 202 answer.

### `GET /api/v1/batch/{jobId}` - Extract data, the same key that submitted
Status and results: `status`, `totalTasks`, `completedTasks`, `failedTasks`,
`totalCostChf`, `expiresAt`, `projectId`, `projectName`, and
`tasks [{taskIndex, documentIndex, partIndex, partCount, status, source, tag, language, contentHash, result, translations, error, costChf, usage, createdAt, completedAt}]`.
`result` is the extraction (its fields are described at
`https://xtracting.io/output-schema`).

**Reading a complete job deletes its results on the platform** (content,
result and translations), and results nobody reads expire on their own about
an hour after the job finished. Add `?retain=true` to read without deleting, or save
the answer on the first read. The collector in `API_V1/collector` does this for
you and writes everything into the archive.

Poll every 15-60 seconds. A job is finished when `status` is `COMPLETED` or
`PARTIAL_FAILURE` (some tasks failed; each failed task carries its `error`). A
job not collected before `expiresAt` answers 410 `EXPIRED`.

## A typical setup session

```
GET   /api/v1/key                      usable? canExtract? canEditProject?
GET   /api/v1/project                  current lists, this key's prefix
PATCH /api/v1/project                  new interestEntities / perspectives
POST  /api/v1/batch                    the test documents
GET   /api/v1/batch/{jobId}?retain=true   until complete; save results
      ... compare with expectations, change one thing, repeat ...
PATCH /api/v1/project/keys/{prefix}    {"isActive": false} after the person's yes
GET   /api/v1/key                      usable: false
```

## Keeping this file current

This file describes the API as of the commit it was last changed in. Every
change to `/api/v1` on the platform is carried into this file, `AGENTS.md`, the
live index, the OpenAPI document and `llms.txt` in the same step.
