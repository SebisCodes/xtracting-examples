# Collector - purpose

The collector fills the archive. Xtracting produces extraction results and
keeps them for about an hour; the collector polls the jobs API on a timer,
fetches every finished result the keys it holds can see, and writes it into
the archive database - one row per task per language.

It is the only component that writes the archive's own tables
(`processed_data`). Whatever submitted the work - the crawler, a script of
yours, somebody pressing a button on the platform - the collector brings the
result home.

## What it promises

- **Idempotent.** A task already stored is not stored twice; re-fetching a
  job after a crash or a restart is harmless.
- **Non-consuming.** Results stay on the platform after they are read, so a
  second collector or a re-run after a restore still finds them.
- **Cheap when idle.** One small query per key per round; running it for a
  year does not accumulate work.
- **Named by the platform.** Every round starts by asking each key what it
  is and which project it opens - and, with Read project on the key, the
  project's own name - so the log names things without anybody typing a
  project name into a file. The label in front of a key is only your marker.
- **Complete per language.** The English original plus every translation the
  job asked for, each a full set of rows carrying the project, the language
  and the task.

## What it is not

It does not submit anything and it holds no opinion about what is extracted.
It is general purpose: the same collector serves every project, every
dashboard and every kind of document.
