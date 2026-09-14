/* ==========================================================================
 *  "Files found" - which files on a document page are sent as text.
 *
 *  TWO IDEAS, TWO COLUMNS, AND THEY ARE NOT THE SAME KIND OF DECISION.
 *
 *    Left: the TYPES this source sends. "Send every PDF, up to 25 MB" is a
 *    standing rule about files that do not exist yet - the one that decides
 *    what happens on the next crawl and the one after that.
 *
 *    Right: the FILES the test actually found on a sample of document
 *    pages. Every row here is an exception to the rule on the left: "this
 *    one as well, although its type is off", "not this one, although its
 *    type is on". The column says so above the list, and a row that is only
 *    following the type rule stores nothing.
 *
 *  A FILE THAT CANNOT BECOME TEXT HAS NO TICK BOX. An archive that is
 *  served as a PDF, a scan with no text layer, a 30 MB export - there is
 *  nothing to decide about them, so they are listed with their reason and
 *  no control. Xtracting takes text and nothing else; an empty task would
 *  be billed for nothing.
 * ========================================================================== */

import { fmtInt, plural } from "./api.js";
import { announce } from "./a11y.js";
import { pickerDialog, confirmDiscard } from "./linkpicker.js";

/* The types that can become text (crawlkit/files.py TEXT_TYPES). Anything
 * else the crawl finds is listed on the right with a reason. */
export const TEXT_TYPES = ["pdf", "docx", "xlsx", "csv", "txt", "md"];

const DEFAULT_MAX_MB = 25;

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

/* The size, when it is known. A file that was not downloaded (its type is
 * off) has no size, and "size unknown" seventeen times in a column reads as
 * a fault - so the column stays empty and the row's reason says why. */
function humanBytes(bytes) {
  const n = Number(bytes || 0);
  if (!n) return "";
  if (n < 1024) return `${n} bytes`;
  if (n < 1024 * 1024) return `${Math.round(n / 1024)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

/* crawlkit writes its reasons for a log file, and one of them carries a
 * Python bytes repr: "not a pdf (content type text/html, starts with
 * b'<!doctyp')". That is a fault report about our own code reaching a
 * customer. The sentence is tidied on the way to the screen - the same
 * thing sources.js does with crawlkit's warnings - and the original is
 * kept in the row's title for whoever has to answer a support call. */
function tidyReason(reason) {
  const text = String(reason || "").trim();
  const wrong = text.match(/^not an? (\w+) \(content type ([^,)]*)/i);
  if (wrong) {
    const kind = wrong[1].toUpperCase();
    const ctype = wrong[2].trim().toLowerCase();
    const got = /html/.test(ctype) ? "a web page"
      : /json|xml/.test(ctype) ? "data"
      : (ctype && ctype !== "unknown") ? `a file of type ${ctype}`
      : "something else";
    return `the address gave back ${got}, not a ${kind}`;
  }
  // Anything else keeps its words and loses the bracketed machinery.
  return text.replace(/\s*\(content type [^)]*\)\s*$/i, "");
}

function fileName(url) {
  try {
    const path = new URL(url).pathname;
    return path.split("/").filter(Boolean).pop() || url;
  } catch (e) {
    return url;
  }
}

/* The three lists of `scraper_config.source_file_rules`, read into the shape
 * the dialog works with. */
function readRules(rules) {
  const types = new Map();
  const include = new Set();
  const exclude = new Set();
  const limits = new Map();
  (rules || []).forEach((row) => {
    const value = String(row.text_value || "");
    if (row.text_kind === "type_default") {
      types.set(value.toLowerCase(), row.bool_enabled !== false);
      limits.set(value.toLowerCase(), Number(row.integer_max_mb) || DEFAULT_MAX_MB);
    } else if (row.text_kind === "file_include") include.add(value);
    else if (row.text_kind === "file_exclude") exclude.add(value);
  });
  return { types, include, exclude, limits };
}

/**
 * Open the files picker.
 *
 *   result   the test snapshot's json_result of a `files` test
 *   draft    the editor's configuration (its file_rules are the start)
 *   onApply  called with the new file_rules array
 */
export function openFilePicker({ result, draft, onApply }) {
  const files = (result.files || []).filter((file) => file && file.url);
  const state = readRules(draft.file_rules);
  const before = JSON.stringify([...state.types].sort()) + JSON.stringify([...state.include].sort())
    + JSON.stringify([...state.exclude].sort());
  let applied = false;

  const counts = new Map();
  files.forEach((file) => {
    const type = (file.type || "").toLowerCase();
    counts.set(type, (counts.get(type) || 0) + 1);
  });
  const types = Array.from(new Set([...TEXT_TYPES, ...counts.keys()]))
    .filter((type) => TEXT_TYPES.includes(type));

  const handle = pickerDialog({
    title: "Files found on the document pages",
    subtitle: `${plural(files.length, "file", "files")} on ${plural(result.sample_subpages || 0, "sampled page", "sampled pages")}.`,
    guard(close) {
      if (applied || !dirty()) return true;
      confirmDiscard(handle, {
        text: "The file rules have been changed but not used yet. Close and lose them?",
        discardLabel: "Discard the changes",
        onDiscard: close,
      });
      return false;
    },
  });

  function dirty() {
    const now = JSON.stringify([...state.types].sort()) + JSON.stringify([...state.include].sort())
      + JSON.stringify([...state.exclude].sort());
    return now !== before;
  }

  const picker = el("div", "picker");
  handle.body.appendChild(picker);

  const columns = el("div", "file-columns");
  picker.appendChild(columns);

  /* -- left: the standing rule ---------------------------------------- */

  const left = el("section", "file-column file-column--rules");
  left.appendChild(el("h3", null, "Types this watched page sends"));
  left.appendChild(el("p", "file-column-note",
    "A standing rule for every crawl, including the files this test has not seen. Tick a type and every file of it is read and its text is sent."));
  const typeList = el("ul", "type-list");
  left.appendChild(typeList);
  columns.appendChild(left);

  const typeTemplate = document.getElementById("file-type-template");
  const fileTemplate = document.getElementById("file-row-template");

  function drawTypes() {
    typeList.textContent = "";
    types.forEach((type) => {
      const row = typeTemplate.content.firstElementChild.cloneNode(true);
      row.dataset.type = type;
      const tick = row.querySelector("[data-type-tick]");
      const on = state.types.get(type) === true;
      tick.checked = on;
      row.querySelector("[data-type-name]").textContent = type;
      const seen = counts.get(type) || 0;
      row.querySelector("[data-type-count]").textContent =
        seen ? `${fmtInt(seen)} in this sample` : "none in this sample";

      const limitBox = row.querySelector("[data-limit-box]");
      const limit = row.querySelector("[data-type-limit]");
      limitBox.hidden = !on;
      limit.value = String(state.limits.get(type) || DEFAULT_MAX_MB);
      limit.addEventListener("change", () => {
        const mb = Math.max(1, Math.min(500, Number(limit.value) || DEFAULT_MAX_MB));
        limit.value = String(mb);
        state.limits.set(type, mb);
      });

      tick.addEventListener("change", () => {
        state.types.set(type, tick.checked);
        if (tick.checked && !state.limits.has(type)) state.limits.set(type, DEFAULT_MAX_MB);
        limitBox.hidden = !tick.checked;
        drawFiles();
        announce(tick.checked ? `${type} files are sent` : `${type} files are not sent`);
      });
      typeList.appendChild(row);
    });
  }

  /* -- right: the exceptions ------------------------------------------ */

  const right = el("section", "file-column");
  right.appendChild(el("h3", null, "Single files from the sample"));
  right.appendChild(el("p", "file-column-note",
    "Exceptions to the rule on the left, one address at a time. A row that agrees with its type is not stored at all."));
  const fileScroll = el("div", "picker-scroll");
  const fileList = el("ul", "file-rows");
  fileScroll.appendChild(fileList);
  right.appendChild(fileScroll);
  const fileEmpty = el("p", "picker-empty", (result.counts && result.counts.samples)
    ? "No files on the sampled pages. Either these documents link to none, or the sample was too small - test again with more pages."
    : "No document page was opened, so no file was seen. The rules have to accept at least one link before there is a page to look at; the types on the left can be set all the same.");
  fileEmpty.hidden = files.length > 0;
  right.appendChild(fileEmpty);
  columns.appendChild(right);

  /* Would this file be sent, as the rules stand now? The same three steps
   * crawlkit.files.FileRules.wanted() takes, in the same order. */
  function wanted(file) {
    const type = (file.type || "").toLowerCase();
    if (state.exclude.has(file.url)) return false;
    if (state.include.has(file.url)) return true;
    return state.types.get(type) === true;
  }

  /* A file whose type can never become text is not a decision. Anything the
   * crawl reported as unreadable for another reason (no text layer, too
   * large) is not one either. */
  function decidable(file) {
    const type = (file.type || "").toLowerCase();
    if (!TEXT_TYPES.includes(type)) return false;
    const reason = String(file.reason || "");
    if (!reason) return true;
    return /switched off|excluded/i.test(reason);
  }

  function drawFiles() {
    fileList.textContent = "";
    files.forEach((file) => {
      const row = fileTemplate.content.firstElementChild.cloneNode(true);
      const type = (file.type || "").toLowerCase();
      const can = decidable(file);
      row.dataset.type = type;
      row.dataset.sendable = can && wanted(file) ? "true" : "false";
      row.querySelector("[data-file-name]").textContent = fileName(file.url);
      row.querySelector("[data-file-from]").textContent = `on ${file.from_page || "a document page"}`;
      row.querySelector("[data-file-size]").textContent = humanBytes(file.bytes);

      const reason = row.querySelector("[data-file-reason]");
      const tickBox = row.querySelector("[data-file-tick-box]");
      const tick = row.querySelector("[data-file-tick]");
      if (!can) {
        tickBox.remove();
        const written = tidyReason(file.reason);
        reason.textContent = written
          ? `Cannot be sent as text: ${written}`
          : `Cannot be sent as text: ${type || "this type"} is not a text format`;
        if (file.reason && written !== String(file.reason).trim()) row.title = file.reason;
        row.classList.add("is-locked");
      } else {
        const on = wanted(file);
        tick.checked = on;
        row.querySelector("[data-file-tick-label]").textContent = `Send ${file.url}`;
        reason.textContent = on
          ? (state.include.has(file.url) ? "Sent as an exception" : `Sent because ${type} is on`)
          : (state.exclude.has(file.url) ? "Left out as an exception" : `Not sent because ${type} is off`);
        tick.addEventListener("change", () => {
          const byType = state.types.get(type) === true;
          state.include.delete(file.url);
          state.exclude.delete(file.url);
          if (tick.checked !== byType) {
            if (tick.checked) state.include.add(file.url);
            else state.exclude.add(file.url);
          }
          drawFiles();
        });
      }
      fileList.appendChild(row);
    });
    const sending = files.filter((file) => decidable(file) && wanted(file)).length;
    handle.status.textContent =
      `${fmtInt(sending)} of ${plural(files.length, "file", "files")} in this sample would be sent`;
  }

  /* -- the answer ------------------------------------------------------ */

  handle.action("Use these file rules", "button", () => {
    const rows = [];
    types.forEach((type) => {
      rows.push({
        text_kind: "type_default",
        text_value: type,
        bool_enabled: state.types.get(type) === true,
        integer_max_mb: state.limits.get(type) || DEFAULT_MAX_MB,
      });
    });
    state.include.forEach((url) => rows.push({
      text_kind: "file_include", text_value: url, bool_enabled: true, integer_max_mb: DEFAULT_MAX_MB,
    }));
    state.exclude.forEach((url) => rows.push({
      text_kind: "file_exclude", text_value: url, bool_enabled: true, integer_max_mb: DEFAULT_MAX_MB,
    }));
    applied = true;
    onApply(rows);
    handle.close();
  });
  handle.action("Cancel", "button--secondary", () => handle.requestClose());

  drawTypes();
  drawFiles();
  return handle;
}
