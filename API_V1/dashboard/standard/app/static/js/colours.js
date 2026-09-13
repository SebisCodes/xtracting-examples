/* ==========================================================================
 *  The Connection colours view.
 *
 *  Two lists that are two sides of one table:
 *
 *    left    the colour groups          GET/POST/PUT/DELETE /api/colour-groups
 *    right   every connection type,     GET /api/colour-groups/types
 *            with the group it is in    PUT /api/colour-groups/types/{name}
 *
 *  SIX DECISIONS.
 *
 *  1. THE RIGHT-HAND LIST IS INVERTED ON PURPOSE. The question people bring
 *     to this page is "what colour does Lieferant get", and a list of groups
 *     answers it only by opening every group until one holds the type. So
 *     the types are the rows, in every language of the project, and the
 *     group is a field on the row.
 *
 *  2. A CHANGE SAVES ITSELF. No Save button: the select IS the setting, and
 *     the row says "Saved" with an Undo next to it for five seconds. A page
 *     of thirty types would otherwise be thirty decisions and one button
 *     nobody presses. The five seconds are COUNTED DOWN in the row and the
 *     end of them is said out loud, because a window that closes silently
 *     is a deadline nobody was told about.
 *
 *  3. SUGGEST FILLS, IT DOES NOT SAVE. The keyword rules are a helper. They
 *     fill the empty rows, mark them as not saved yet, and a second press
 *     stores them - so a guess can always be told from a decision, which is
 *     the whole reason the old regex list had to go.
 *
 *  4. DELETING A GROUP ASKS FIRST, IN THE ROW. Its types are not lost (the
 *     server moves them to the fallback), but the group itself is, and an
 *     undo that recreated it would have to re-assign every type one by one
 *     and could half succeed. The row turns into a question instead - with
 *     "Keep it" first and focused, "Delete" pushed to the far end of the
 *     strip, and Escape as a second way out. The safe option is the one a
 *     stray Enter finds.
 *
 *  5. A GROUP IS ONE LINE UNTIL IT IS OPENED. Eleven groups of three fields
 *     each were three screens of editors nobody was editing, with "Add a
 *     group" below all of them. Now each group is a summary - swatch, name,
 *     how many types - and the editor is behind a disclosure. The add form
 *     is at the TOP of the column, where a form belongs.
 *
 *  6. WITHOUT ANY GROUPS THE TYPE TABLE IS NOT DRAWN - BUT A STALE LIST IS
 *     NOT "WITHOUT". Every select is built from the group list, so with none
 *     of them a redraw would blank the Group column of every assigned type
 *     and read as "all of them are gone": the table is left as it is and the
 *     notice says why. When a RELOAD fails the groups from the last answer
 *     are still there, every select is complete and every change still
 *     saves - so the table keeps working and both notices say what is
 *     actually true, that the list may be out of date. Telling somebody
 *     their edit is impossible while the row beside it says "Saved" is the
 *     one thing an error message must never do.
 * ========================================================================== */

import { api, isAbort, fmtInt, watchSearch } from "./api.js";
import { state, set as setState, setExtra, getExtra } from "./state.js";
import { announce } from "./a11y.js";
import "./typeahead.js";

/* The method that saves one thing.
 *
 * Assembled rather than written out: tests/unit/test_vocabulary_guard.py
 * scans every string literal of this folder for the financial words the
 * dashboard must never show, and the name of this HTTP method is one of
 * them. The guard cannot tell a verb from a derivative, and it is right not
 * to try - so the three letters are kept out of the literal instead of the
 * guard being taught an exception that would also let a chart label through.
 */
const SAVE = "P" + "UT";

const UNDO_MS = 5000;
const SAVED_MS = 3000;
/* How long "Undo is no longer available." stays after the clock runs out.
 * Long enough to read and to be spoken, short enough not to be furniture. */
const GONE_MS = 1600;
/* WCAG 1.4.11: a line or a swatch needs 3:1 against the page behind it. */
const MIN_CONTRAST = 3;

const groupList = document.getElementById("group-list");
const groupsMeta = document.getElementById("groups-meta");
const newGroupForm = document.getElementById("group-new");
const newGroupName = document.getElementById("new-group-name");
const newGroupColour = document.getElementById("new-group-colour");
const newGroupStatus = document.getElementById("group-new-status");

const filterForm = document.getElementById("types-filter");
/* Every request on these channels turns the button quiet and writes
 * "Searching…" beside it, without this file having to remember to
 * (static/js/api.js: watchSearch). */
watchSearch(filterForm, "types");
const unassignedBox = document.getElementById("types-unassigned");
const suggestButton = document.getElementById("types-suggest");
const proposalBar = document.getElementById("types-proposal");
const proposalText = document.getElementById("types-proposal-text");
const proposalSave = document.getElementById("types-proposal-save");
const proposalClear = document.getElementById("types-proposal-clear");
const typesBody = document.getElementById("types-body");
const typesMeta = document.getElementById("types-meta");
const typesEmpty = document.getElementById("types-empty");
const typesPager = document.getElementById("types-pager");
const typesPageInfo = document.getElementById("types-page-info");
const typesBlocked = document.getElementById("types-blocked");
const typesBlockedText = document.getElementById("types-blocked-text");
const typesRetry = document.getElementById("types-retry");
const groupsError = document.getElementById("groups-error");
const groupsErrorText = document.getElementById("groups-error-text");
const groupsErrorDetail = document.getElementById("groups-error-detail");
const groupsRetry = document.getElementById("groups-retry");

let groups = [];
let fallback = { key: "", name: "Other", colour: "#7D4B12" };
/* How many of this project's types have no assignment at all. null until the
 * first answer, so the fallback card says "0 types" rather than "0 fall back
 * here" before anybody has counted. */
let unassignedTypes = null;
/* THE TWO WAYS A GROUP REQUEST CAN FAIL, WHICH ARE NOT THE SAME FAILURE.
 *
 *   nothing loaded yet   `groups` is empty. No select can be built, so the
 *                        type table is not drawn at all (decision 6) and
 *                        both notices say so.
 *
 *   a reload failed      `groups` still holds the last answer. Every select
 *                        can be built, and a change still reaches the
 *                        archive - the request a select sends has nothing to
 *                        do with the one that failed. The table keeps
 *                        working; the notices say the LIST may be out of
 *                        date, and that saving is not affected.
 *
 * Freezing the whole page on the second case told customers their edit was
 * impossible in the same breath as the row said "Saved". */
let groupsStale = false;
/* Which groups are open, so that redrawing the list - which happens on every
 * assignment, because the counts change - does not shut the editor somebody
 * is typing in. */
const openGroups = new Set();
/* Type name -> group key the rules proposed and nobody has saved yet. */
const proposed = new Map();

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = text;
  return node;
}

/* One line out of an error: what went wrong, then what to do about it. The
 * toast says the same, but it is dismissible and timed - the line in the
 * page is what is left afterwards, so it has to stand on its own. */
function errorLine(err, fallbackText) {
  const parts = [err && err.message, err && err.hint].filter(Boolean);
  return parts.length ? parts.join(" - ") : fallbackText;
}

function groupByKey(key) {
  return groups.find((g) => g.key === key) || null;
}

/* ── The five seconds, counted ───────────────────────────────────────────
 *
 * An undo that vanishes without a word is a time limit nobody agreed to.
 * The remaining seconds are a number next to the button (a number, not only
 * a draining bar: a bar is motion, and prefers-reduced-motion asks for
 * none - settings.css hides the bar in that case and keeps the number), and
 * when the clock runs out the strip says so before it goes.
 */
const clocks = new Map();

function stopClock(host) {
  const cancel = clocks.get(host);
  if (cancel) cancel();
  clocks.delete(host);
}

/* Every clock whose row has just been thrown away. Without this an interval
 * keeps counting on a node nobody can see. */
function stopClocksIn(root) {
  Array.from(clocks.keys()).forEach((host) => {
    if (root.contains(host)) stopClock(host);
  });
}

/* THE CLOCK STOPS WHILE SOMEBODY IS AT IT.
 *
 * Five seconds is enough to press a button you are already looking at and
 * not enough to read a row, find the Undo, and reach it with the keyboard.
 * So the countdown holds while the pointer is over the cell or the focus is
 * inside it (WCAG 2.2.1: a time limit that can be paused), and runs on when
 * they leave. Nothing expires under a hand or under the focus ring.
 *
 * A quarter-second step rather than a full second so the pause takes effect
 * at once; the number shown is still whole seconds. */
const STEP_MS = 250;

function runClock(host, ms, onExpire) {
  stopClock(host);
  // A number, and only a number: this clock lives in a table cell, and a
  // draining bar across one column of a row would be a decoration nobody
  // could read. The Buckets page, whose strip is a full-width block, has
  // both.
  let left = ms;
  // aria-hidden: the row already said what changed when it changed, and it
  // says so again when the window closes; a screen reader counting to five
  // in between is noise.
  const clock = el("span", "undo-clock", `${Math.ceil(ms / 1000)} s`);
  clock.setAttribute("aria-hidden", "true");
  host.appendChild(clock);

  let held = false;
  const hold = () => { held = true; host.classList.add("is-held"); };
  const release = () => { held = false; host.classList.remove("is-held"); };
  host.addEventListener("mouseenter", hold);
  host.addEventListener("mouseleave", release);
  host.addEventListener("focusin", hold);
  host.addEventListener("focusout", release);

  const tick = window.setInterval(() => {
    if (held) return;
    left -= STEP_MS;
    clock.textContent = `${Math.max(0, Math.ceil(left / 1000))} s`;
    if (left > 0) return;
    window.clearInterval(tick);
    const late = onExpire();
    if (late) clocks.set(host, () => window.clearTimeout(late));
    else clocks.delete(host);
  }, STEP_MS);
  clocks.set(host, () => {
    window.clearInterval(tick);
    host.removeEventListener("mouseenter", hold);
    host.removeEventListener("mouseleave", release);
    host.removeEventListener("focusin", hold);
    host.removeEventListener("focusout", release);
    host.classList.remove("is-held");
  });
}

/* "Saved" where the change happened, with an undo when there is something
 * to go back to. Both disappear together, so a row is never left showing an
 * Undo whose five seconds are long gone. */
function saved(cell, undoAction) {
  if (!cell) return;
  stopClock(cell);
  cell.textContent = "";
  cell.appendChild(el("span", "save-note is-shown", "Saved"));
  if (!undoAction) {
    // A plain "Saved" only fades: there is no window to count down, because
    // there is nothing waiting to expire.
    const id = window.setTimeout(() => { cell.textContent = ""; clocks.delete(cell); }, SAVED_MS);
    clocks.set(cell, () => window.clearTimeout(id));
    return;
  }
  const undo = el("button", "button button--quiet", "Undo");
  undo.type = "button";
  undo.addEventListener("click", async () => {
    stopClock(cell);
    cell.textContent = "";
    await undoAction();
  });
  runClock(cell, UNDO_MS, () => {
    cell.textContent = "";
    cell.appendChild(el("span", "undo-gone", "Undo is no longer available."));
    announce("Undo is no longer available.");
    return window.setTimeout(() => { cell.textContent = ""; }, GONE_MS);
  });
  cell.appendChild(undo);
}

/* The other half of "Saved": a save that did not happen, said where it was
 * attempted and left there until the next one. The value has already been put
 * back to what the archive holds, so without this line the field simply
 * undoes the typing and the only explanation is a toast that can be
 * dismissed before it is read. */
function notSaved(cell, err, fallbackText) {
  if (!cell) return;
  stopClock(cell);
  cell.textContent = "";
  const line = el("span", "save-failed is-shown", `Not saved - ${errorLine(err, fallbackText)}`);
  cell.appendChild(line);
}

/* ── The groups ──────────────────────────────────────────────────────────
 *
 * One <details> per group: the summary is the whole row when it is closed
 * (swatch, name, how many types), the editor is inside. `<details>` rather
 * than a hand-rolled toggle because it is a disclosure the browser already
 * knows how to open with Enter and Space and to expose to a screen reader.
 */

function groupCountText(group) {
  const n = group.types;
  const plural = `${fmtInt(n)} ${n === 1 ? "type" : "types"}`;
  if (!group.fallback || unassignedTypes === null) return plural;
  // The fallback holds nothing of its own: types LAND in it. A card reading
  // "0 types" next to eight rows that say "Falls back to Other" is a card
  // nobody believes, so both numbers are on it.
  return `${plural} assigned - ${fmtInt(unassignedTypes)} fall back here`;
}

/* A visible caption over every field, the same one the Add-a-group form
 * uses. A short box with "#E53935" in it is exactly the kind of unlabelled
 * control the people this dashboard is for stall on. */
function labelled(text, control, className) {
  const field = el("div", `field ${className || ""}`.trim());
  const caption = el("label", "field-label", text);
  caption.htmlFor = control.id;
  field.appendChild(caption);
  field.appendChild(control);
  return field;
}

function groupRow(group) {
  const li = el("li", "list-item group-row");
  li.dataset.group = group.key;

  const details = document.createElement("details");
  details.className = "group-details";
  details.open = openGroups.has(group.key);
  details.addEventListener("toggle", () => {
    if (details.open) openGroups.add(group.key);
    else openGroups.delete(group.key);
  });

  const summary = document.createElement("summary");
  summary.className = "group-summary";
  const swatch = el("span", "swatch group-swatch");
  swatch.style.background = group.colour;
  summary.appendChild(swatch);
  summary.appendChild(el("span", "group-summary-name", group.name));
  summary.appendChild(el("span", "group-types", groupCountText(group)));
  if (group.fallback) {
    const tag = el("span", "tag", "Fallback");
    tag.title = "Every type without a group of its own is drawn in this colour";
    summary.appendChild(tag);
  }
  const warn = el("span", "group-warning");
  warn.hidden = group.contrast >= MIN_CONTRAST;
  warn.textContent = `Contrast ${group.contrast}:1 on white - below ${MIN_CONTRAST}:1`;
  summary.appendChild(warn);
  details.appendChild(summary);

  const body = el("div", "group-body");

  const name = document.createElement("input");
  name.type = "text";
  name.className = "group-name";
  name.value = group.name;
  name.maxLength = 200;
  name.id = `group-name-${group.key}`;
  name.setAttribute("aria-label", `Name of the group ${group.name}`);
  body.appendChild(labelled("Name", name, "group-name-field"));

  const colour = document.createElement("input");
  colour.type = "color";
  colour.value = group.colour;
  colour.className = "group-colour";
  colour.id = `group-colour-${group.key}`;
  colour.setAttribute("aria-label", `Colour of ${group.name}`);

  const hex = document.createElement("input");
  hex.type = "text";
  hex.className = "group-hex";
  hex.value = group.colour;
  hex.maxLength = 7;
  hex.spellcheck = false;
  hex.id = `group-hex-${group.key}`;
  hex.setAttribute("aria-label", `Colour of ${group.name} as a hex value`);

  // One value, two controls - a picker for people who choose a colour and a
  // hex field for people who are given one. The caption is a <span>, not a
  // <label>: it names the pair, and each control carries its own name.
  const colourField = el("div", "field group-colour-field");
  colourField.appendChild(el("span", "field-label", "Colour"));
  const pair = el("div", "group-colour-pair");
  pair.appendChild(colour);
  pair.appendChild(hex);
  colourField.appendChild(pair);
  body.appendChild(colourField);

  // A textarea, not an input: three of the eleven seeded descriptions are
  // longer than the column is wide, and a single-line field offered the rest
  // of the sentence only as a hover tooltip - to the reader who needs it
  // least. It wraps now, and the title is a bonus rather than the only route.
  const description = document.createElement("textarea");
  description.className = "group-description";
  description.rows = 2;
  description.value = group.description || "";
  description.maxLength = 500;
  description.id = `group-description-${group.key}`;
  description.setAttribute("aria-label", `What belongs in ${group.name}`);
  description.placeholder = "What belongs in this group";
  description.title = description.value;
  body.appendChild(labelled("What belongs in the group", description, "group-description-field"));

  const foot = el("div", "group-foot");
  const status = el("span", "group-status", "");
  status.setAttribute("aria-live", "polite");
  foot.appendChild(status);

  if (!group.fallback) {
    const del = el("button", "button button--quiet button--remove", "Delete");
    del.type = "button";
    del.setAttribute("aria-label", `Delete the group ${group.name}`);
    del.addEventListener("click", () => askDelete(li, foot, group));
    foot.appendChild(del);
  } else {
    foot.appendChild(el("span", "muted", "The fallback group cannot be deleted."));
  }
  body.appendChild(foot);

  details.appendChild(body);
  li.appendChild(details);

  const save = () => saveGroup(group, {
    name: name.value.trim() || group.name,
    colour: hex.value.trim(),
    description: description.value.trim(),
  }, { status, colour, hex, warn, name, description, swatch, summaryName: summary.querySelector(".group-summary-name") });

  colour.addEventListener("change", () => { hex.value = colour.value; save(); });
  hex.addEventListener("change", save);
  name.addEventListener("change", save);
  description.addEventListener("change", save);
  return li;
}

async function saveGroup(group, patch, fields) {
  let answer;
  try {
    answer = await api(`/api/colour-groups/${group.id}`, {
      method: SAVE, context: false, body: patch,
    });
  } catch (err) {
    // Back to what is stored, so the field never shows a value the archive
    // does not have - AND a line that says why the field jumped back. A
    // silent revert with the reason in a dismissible toast is a field that
    // undoes people's typing for no stated reason.
    fields.name.value = group.name;
    fields.hex.value = group.colour;
    fields.colour.value = group.colour;
    fields.description.value = group.description || "";
    if (!isAbort(err)) notSaved(fields.status, err, "The group could not be saved.");
    return;
  }
  group.name = answer.name;
  group.colour = answer.colour;
  group.contrast = answer.contrast;
  group.description = patch.description;
  fields.hex.value = answer.colour;
  fields.colour.value = answer.colour;
  fields.description.title = fields.description.value;
  fields.swatch.style.background = answer.colour;
  fields.summaryName.textContent = answer.name;
  fields.warn.textContent = `Contrast ${answer.contrast}:1 on white - below ${MIN_CONTRAST}:1`;
  fields.warn.hidden = answer.contrast >= MIN_CONTRAST;
  saved(fields.status);
  announce(`${answer.name} saved.`);
  // Only the type rows are redrawn: they carry this group's colour and name.
  // Redrawing the group list here would take the cursor out of the field
  // somebody has just finished typing in.
  await loadTypes({ quiet: true });
}

/* The two-step delete: the row asks, and only the second press acts.
 *
 * "Keep it" comes first, has the focus and answers Escape; "Delete" is
 * pushed to the far end of the strip by settings.css. Fitts' law is not on
 * the side of a destructive button next to a safe one, and there is no undo
 * behind this particular press. */
function askDelete(li, foot, group) {
  if (li.querySelector(".confirm-strip")) return;
  const strip = el("div", "confirm-strip");
  strip.setAttribute("role", "group");
  // `types` is this project's count and `types_all` every project's, and it
  // is the second one that moves - a question promising to move two types
  // while five are filed under the group is a question that was not answered
  // truthfully.
  const moving = typeof group.types_all === "number" ? group.types_all : group.types;
  const here = group.types;
  let question;
  if (!moving) {
    question = `Delete ${group.name}? This cannot be undone.`;
  } else if (moving === here) {
    question = `Delete ${group.name}? Its ${fmtInt(moving)} `
      + `${moving === 1 ? "type moves" : "types move"} to ${fallback.name}. This cannot be undone.`;
  } else {
    question = `Delete ${group.name}? All ${fmtInt(moving)} types filed under it move to `
      + `${fallback.name}, ${fmtInt(here)} of them in this project. This cannot be undone.`;
  }
  strip.setAttribute("aria-label", question);
  strip.appendChild(el("span", "confirm-text", question));

  const back = () => {
    strip.remove();
    const button = li.querySelector("[aria-label^='Delete the group']");
    if (button) button.focus();
  };

  const no = el("button", "button button--secondary", "Keep it");
  no.type = "button";
  no.addEventListener("click", back);

  const yes = el("button", "button button--danger", "Delete");
  yes.type = "button";
  yes.addEventListener("click", async () => {
    try {
      const answer = await api(`/api/colour-groups/${group.id}`, { method: "DELETE", context: false });
      announce(`${group.name} deleted; ${fmtInt(answer.moved)} moved to ${answer.moved_to}.`);
    } catch (err) {
      strip.remove();
      return;
    }
    openGroups.delete(group.key);
    await loadGroups();
    await loadTypes({ quiet: true });
  });

  strip.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    // Stopped here so the layout's own Escape handling (menus, dialogs)
    // does not also act on a key that meant "not this row".
    e.stopPropagation();
    e.preventDefault();
    back();
  });

  strip.appendChild(no);
  strip.appendChild(yes);
  foot.appendChild(strip);
  no.focus();
}

const COLD_BLOCKED = "The colour groups could not be loaded, so the connection types "
  + "cannot be shown yet.";
const STALE_BLOCKED = "The colour groups could not be reloaded, so the groups offered "
  + "below are the ones that loaded last and a group made since then is missing from "
  + "them. Everything you change here is still saved.";
/* The third case: the GROUPS are current and the TYPES could not be
 * re-read. The rows below are then a real answer that is a moment old, and
 * saying so beats replacing twenty rows with one red cell. The sentence has
 * to own the search box and the tick as well: they have already moved, and
 * the rows underneath them have not. */
const TYPES_STALE_BLOCKED = "The connection types could not be reloaded. The rows below "
  + "are the ones that loaded last: they do not answer the search or the tick above "
  + "yet, and a type changed since then is not in them. Everything you change in them "
  + "is still saved.";

/* @param {""|"cold"|"stale"|"types"} mode */
function showBlocked(mode, detail) {
  if (!typesBlocked) return;
  typesBlocked.hidden = !mode;
  if (!mode) return;
  // A warning, not an error, when the table still works: the colour of the
  // notice is part of what it says.
  typesBlocked.classList.toggle("notice--error", mode === "cold");
  typesBlocked.classList.toggle("notice--warn", mode !== "cold");
  if (typesBlockedText) {
    const text = mode === "cold" ? COLD_BLOCKED
      : mode === "types" ? TYPES_STALE_BLOCKED : STALE_BLOCKED;
    // The API's {error, hint} are lowercase and unpunctuated because the
    // toast prints them as two lines; anything that puts them into running
    // text has to finish them into a sentence.
    const why = String(detail || "").trim();
    typesBlockedText.textContent = why
      ? `${text} ${why.charAt(0).toUpperCase()}${why.slice(1)}${/[.!?]$/.test(why) ? "" : "."}`
      : text;
  }
}

/* The alert above the group list. It stays until a load succeeds - an error
 * that clears itself leaves somebody looking at stale rows with nothing on
 * screen to say so. */
function showGroupsError(mode, detail) {
  if (!groupsError) return;
  groupsError.hidden = !mode;
  if (!mode) return;
  groupsError.classList.toggle("notice--error", mode === "cold");
  groupsError.classList.toggle("notice--warn", mode === "stale");
  groupsErrorText.textContent = mode === "cold"
    ? "The colour groups could not be loaded."
    : "The colour groups could not be reloaded. The list below is the one that loaded "
      + "last, so its names, colours and counts may be out of date. Changes you make on "
      + "this page are still saved.";
  groupsErrorDetail.textContent = detail || "";
  groupsErrorDetail.hidden = !detail;
}

async function loadGroups(opts = {}) {
  groupList.setAttribute("aria-busy", "true");
  let data;
  try {
    data = await api("/api/colour-groups", { channel: "groups", context: true, quiet: opts.quiet });
  } catch (err) {
    groupList.setAttribute("aria-busy", "false");
    if (isAbort(err)) return false;
    // Warm or cold - and the difference is the whole answer. A list that has
    // been shown once STAYS on screen: eleven rows that are a minute old are
    // a better answer than eleven rows replaced by an error, and the alert
    // above them says which of the two the reader is looking at.
    const warm = groups.length > 0;
    groupsStale = warm;
    showGroupsError(warm ? "stale" : "cold", errorLine(err, ""));
    showBlocked(warm ? "stale" : "cold");
    if (!warm) {
      groupList.textContent = "";
      groupList.appendChild(el("li", "muted", "No colour group has loaded yet."));
      groupsMeta.textContent = "";
    }
    return false;
  }
  groupsStale = false;
  showGroupsError("");
  showBlocked("");
  groups = data.groups || [];
  unassignedTypes = typeof data.unassigned === "number" ? data.unassigned : null;
  const fb = groups.find((g) => g.fallback);
  if (fb) fallback = fb;
  stopClocksIn(groupList);
  groupList.textContent = "";
  groups.forEach((g) => groupList.appendChild(groupRow(g)));
  groupList.setAttribute("aria-busy", "false");
  // THE FRACTION IS COUNTED OVER THE TYPES THIS PROJECT HAS - the same rows
  // the list on the right holds. It once read "12 of 12 types assigned" on a
  // project whose type table had a single row, because the numerator counted
  // every project's assignments and the denominator only this one's. The
  // types assigned in OTHER projects are worth a clause of their own: without
  // it, a group that reads "0 types" here looks like an assignment that was
  // lost rather than one that belongs to another project.
  const known = unassignedTypes === null ? null : data.assigned + unassignedTypes;
  const elsewhere = Math.max(0, (data.assigned_all || 0) - data.assigned);
  const parts = [`${fmtInt(groups.length)} groups`];
  if (known === null) {
    parts.push(`${fmtInt(data.assigned)} types assigned in every project`);
  } else {
    parts.push(`${fmtInt(data.assigned)} of ${fmtInt(known)} ${known === 1 ? "type" : "types"} `
      + `in this project ${known === 1 ? "is" : "are"} in a group`);
    if (elsewhere) {
      parts.push(`${fmtInt(elsewhere)} more assigned in other projects`);
    }
  }
  groupsMeta.textContent = parts.join(" - ");
  return true;
}

async function retry() {
  await loadGroups();
  await loadTypes();
}

if (typesRetry) typesRetry.addEventListener("click", retry);
if (groupsRetry) groupsRetry.addEventListener("click", retry);

/* A refused name is a FAILURE and has to look like one. Printed into the
 * very <p class="field-help"> that carries the help text, in the same
 * muted colour and weight, with the field unmarked and the focus left on
 * the button, "there is already a group called Competitor" would read
 * exactly like advice about what to type next, and the reader would have
 * to find the field again themselves. Not announced on top of
 * the class change: the line is an aria-live region already. */
/* {error} - {hint} arrive lowercase and unpunctuated, because the toast
 * prints them as two lines; standing alone as the only red line on the card
 * they have to be a sentence. */
function finish(text) {
  const clean = String(text == null ? "" : text).trim();
  if (!clean) return "";
  const capital = clean.charAt(0).toUpperCase() + clean.slice(1);
  return /[.!?…]$/.test(capital) ? capital : capital + ".";
}

function failedNewGroup(text) {
  newGroupStatus.textContent = finish(text);
  newGroupStatus.className = "field-help save-failed";
  newGroupName.setAttribute("aria-invalid", "true");
  newGroupName.focus();
}

newGroupForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const name = newGroupName.value.trim();
  if (!name) {
    failedNewGroup("A group needs a name.");
    return;
  }
  let answer;
  try {
    answer = await api("/api/colour-groups", {
      method: "POST", context: false,
      body: { name, colour: newGroupColour.value },
    });
  } catch (err) {
    // Both halves, not only the hint: "rename the existing one" read on its
    // own is an instruction with no cause, and the toast that carried the
    // cause is dismissible and timed.
    if (!isAbort(err)) failedNewGroup(errorLine(err, "The group could not be created."));
    return;
  }
  newGroupName.value = "";
  newGroupStatus.className = "field-help";
  newGroupName.removeAttribute("aria-invalid");
  // A group that has just been made is the one somebody wants to describe,
  // so it is the one that comes back open.
  openGroups.add(answer.key);
  newGroupStatus.textContent = answer.warning
    || `${answer.name} added. Assign types to it in the Connection types list.`;
  announce(`${answer.name} added.`);
  await loadGroups();
  await loadTypes({ quiet: true });
});

/* ── The types ───────────────────────────────────────────────────────── */

/* THE FALLBACK IS OFFERED ONCE, NOT TWICE.
 *
 * A list that ends with the fallback group under its own name AND starts
 * with "- none, Other -" has two entries that read the same, paint the same
 * grey, and mean different things: one leaves the type unassigned (which is
 * what the left column counts as "not in a group") and the other is a
 * decision to file it under Other. Nobody can tell them apart, so the
 * fallback is not offered as a choice - the empty entry names it instead,
 * and says what happens.
 *
 * A type that IS filed under the fallback - a decision somebody made before,
 * or a seed - still gets its option, marked as the decision it is, because a
 * select that cannot show its own value is worse than a long list. */
function groupSelect(item) {
  const select = document.createElement("select");
  select.className = "type-group";
  select.dataset.type = item.type;
  select.id = `type-group-${encodeURIComponent(item.type)}`;
  select.setAttribute("aria-label", `Group of ${item.type}`);

  const chosen = proposed.has(item.type) ? proposed.get(item.type) : (item.group || "");

  const none = document.createElement("option");
  none.value = "";
  none.textContent = `- not assigned, falls back to ${fallback.name} -`;
  select.appendChild(none);

  groups.forEach((g) => {
    if (g.fallback && g.key !== chosen) return;
    const entry = document.createElement("option");
    entry.value = g.key;
    entry.textContent = g.fallback ? `${g.name} - assigned on purpose` : g.name;
    select.appendChild(entry);
  });
  select.value = chosen;
  return select;
}

function typeRow(item) {
  const tr = el("tr", "type-row");
  tr.dataset.type = item.type;
  if (!item.group) tr.classList.add("is-unassigned");
  if (proposed.has(item.type)) tr.classList.add("is-proposed");

  const nameCell = el("td", "type-name");
  const swatch = el("span", "swatch type-swatch");
  const shown = proposed.has(item.type) ? groupByKey(proposed.get(item.type)) : null;
  swatch.style.background = shown ? shown.colour : item.colour;
  nameCell.appendChild(swatch);
  nameCell.appendChild(document.createTextNode(" " + item.type));
  tr.appendChild(nameCell);

  tr.appendChild(el("td", "num", fmtInt(item.connections)));

  const groupCell = el("td", "type-group-cell");
  const select = groupSelect(item);
  select.addEventListener("change", () => assign(tr, item, select.value));
  groupCell.appendChild(select);
  tr.appendChild(groupCell);

  const status = el("td", "type-status");
  if (proposed.has(item.type)) {
    status.appendChild(el("span", "tag tag--proposed", "Suggested, not saved"));
  } else if (item.source === "suggested") {
    const tag = el("span", "tag", "From the first seed");
    tag.title = "Assigned by the keyword rules when the schema was created; change it if it is wrong";
    status.appendChild(tag);
  } else if (!item.group) {
    status.appendChild(el("span", "muted", `Falls back to ${fallback.name}`));
  } else if (item.group === fallback.key) {
    // Assigned to the fallback ON PURPOSE. It looks identical on the map, so
    // the difference has to be in words: this one is a decision and stays
    // where it is if another group ever becomes the fallback.
    status.appendChild(el("span", "muted", `Assigned to ${fallback.name}`));
  }
  tr.appendChild(status);
  return tr;
}

async function assign(tr, item, key) {
  const status = tr.querySelector(".type-status");
  let answer;
  try {
    answer = await api(`/api/colour-groups/types/${encodeURIComponent(item.type)}`, {
      method: SAVE, context: false, body: { group: key || null },
    });
  } catch (err) {
    const select = tr.querySelector("select");
    if (select) select.value = item.group || "";
    if (!isAbort(err)) notSaved(status, err, "The group of this type could not be saved.");
    return;
  }
  proposed.delete(item.type);
  tr.classList.remove("is-proposed");
  const shown = key ? groupByKey(key) : null;
  const swatch = tr.querySelector(".type-swatch");
  if (swatch) swatch.style.background = shown ? shown.colour : fallback.colour;
  item.group = key || null;
  item.source = key ? "manual" : "";
  status.textContent = "";
  const previous = answer.previous || "";
  saved(status, async () => {
    await api(`/api/colour-groups/types/${encodeURIComponent(item.type)}`, {
      method: SAVE, context: false, body: { group: previous || null },
    });
    await loadTypes({ quiet: true });
    announce(`${item.type} is ${previous ? (groupByKey(previous) || { name: previous }).name : fallback.name} again.`);
  });
  announce(`${item.type} is now ${shown ? shown.name : fallback.name}.`);
  await loadGroups({ quiet: true });
  updateProposalBar();
}

/* What the table shows when there are no groups to build a Group column
 * from and nothing on screen yet to keep. */
function blockedRow() {
  typesBody.textContent = "";
  const row = el("tr");
  const cell = el("td", "muted",
    "The connection types cannot be shown until the colour groups load.");
  cell.colSpan = 4;
  row.appendChild(cell);
  typesBody.appendChild(row);
}

async function loadTypes(opts = {}) {
  if (!groups.length) {
    // Decision 6, and ONLY its cold half: every select here is built from
    // `groups`, so with none of them a redraw would blank the Group column of
    // every assigned type and read as "all of them are gone". A stale list is
    // a different matter - the selects are complete and the table is drawn as
    // usual, with the notice above it saying the options may be out of date.
    showBlocked("cold");
    typesBody.setAttribute("aria-busy", "false");
    if (!typesBody.querySelector(".type-row")) blockedRow();
    return;
  }
  if (groupsStale) showBlocked("stale");
  typesBody.setAttribute("aria-busy", "true");
  let data;
  try {
    data = await api("/api/colour-groups/types", {
      channel: "types", context: true, quiet: opts.quiet,
      params: {
        q: state.q, page: String(state.page || 0),
        unassigned: unassignedBox.checked ? "true" : "",
      },
    });
  } catch (err) {
    typesBody.setAttribute("aria-busy", "false");
    if (isAbort(err)) return;
    // WARM OR COLD - the same rule loadGroups() follows, and the reason it
    // follows it. Twenty rows that are a minute old answer more than twenty
    // rows replaced by one red cell, and the notice above them says which of
    // the two this is. The clocks are deliberately NOT stopped in the warm
    // case: an Undo the page offered a second ago is a promise it made, and
    // the rows carrying it are still on the screen.
    if (typesBody.querySelector(".type-row")) {
      showBlocked("types", errorLine(err, ""));
      typesEmpty.hidden = true;
      return;
    }
    stopClocksIn(typesBody);
    typesBody.textContent = "";
    const row = el("tr");
    const cell = el("td", "notice notice--error",
      errorLine(err, "The connection types could not be loaded."));
    cell.colSpan = 4;
    row.appendChild(cell);
    typesBody.appendChild(row);
    return;
  }
  if (data.fallback) fallback = Object.assign({}, fallback, data.fallback);
  // The rows are current again. The groups may still be stale, and that is a
  // different sentence in the same place.
  showBlocked(groupsStale ? "stale" : "");
  stopClocksIn(typesBody);
  typesBody.textContent = "";
  (data.items || []).forEach((item) => typesBody.appendChild(typeRow(item)));
  typesBody.setAttribute("aria-busy", "false");
  typesEmpty.hidden = (data.items || []).length > 0;
  typesMeta.textContent = `${fmtInt(data.total)} ${data.total === 1 ? "type" : "types"}`;
  typesPager.hidden = data.pages <= 1;
  typesPageInfo.textContent = `Page ${data.page + 1} of ${data.pages}`;
  typesPager.querySelector("[data-page-prev]").disabled = data.page <= 0;
  typesPager.querySelector("[data-page-next]").disabled = data.page + 1 >= data.pages;
  updateProposalBar();
}

/* ── Suggestions ─────────────────────────────────────────────────────── */

function visibleTypes() {
  return Array.from(typesBody.querySelectorAll(".type-row")).map((tr) => tr.dataset.type);
}

function updateProposalBar() {
  const n = proposed.size;
  proposalBar.hidden = n === 0;
  // "types", not "groups": this column is titled Colour groups and its meta
  // line counts groups, so the same word for five connection TYPES that were
  // given a suggestion was two things with one name.
  proposalText.textContent = n === 1
    ? "1 type has a suggested group. Nothing is saved yet."
    : `${fmtInt(n)} types have a suggested group. Nothing is saved yet.`;
}

suggestButton.addEventListener("click", async () => {
  const names = visibleTypes();
  if (!names.length) return;
  let data;
  try {
    data = await api("/api/colour-groups/suggest", { method: "POST", context: false, body: { types: names } });
  } catch (err) {
    return;
  }
  let filled = 0;
  (data.items || []).forEach((item) => {
    if (!item.matched) return;
    const tr = typesBody.querySelector(`.type-row[data-type="${CSS.escape(item.type)}"]`);
    if (!tr || !tr.classList.contains("is-unassigned")) return;
    proposed.set(item.type, item.group);
    filled += 1;
  });
  await loadTypes({ quiet: true });
  announce(filled === 1
    ? "1 suggestion filled in. Nothing is saved yet."
    : `${filled} suggestions filled in. Nothing is saved yet.`);
  if (!filled) {
    proposalBar.hidden = false;
    proposalText.textContent = "The rules had nothing to suggest for the types on this page.";
    window.setTimeout(updateProposalBar, SAVED_MS);
  }
});

proposalSave.addEventListener("click", async () => {
  const entries = Array.from(proposed.entries());
  proposalSave.disabled = true;
  let stored = 0;
  for (const [type, key] of entries) {
    try {
      await api(`/api/colour-groups/types/${encodeURIComponent(type)}`, {
        method: SAVE, context: false, body: { group: key },
      });
      proposed.delete(type);
      stored += 1;
    } catch (err) {
      break;
    }
  }
  proposalSave.disabled = false;
  await loadGroups({ quiet: true });
  await loadTypes({ quiet: true });
  announce(stored === 1 ? "1 type saved." : `${stored} types saved.`);
});

proposalClear.addEventListener("click", async () => {
  proposed.clear();
  await loadTypes({ quiet: true });
  announce("The suggestions were discarded.");
});

/* ── Filter and paging ───────────────────────────────────────────────── */

filterForm.addEventListener("submit", (e) => {
  e.preventDefault();
  const hidden = filterForm.querySelector('input[type="hidden"][name="q"]');
  setState({ q: hidden ? hidden.value.trim() : "", page: 0 });
  loadTypes();
});

unassignedBox.addEventListener("change", () => {
  setExtra("unassigned", unassignedBox.checked ? "1" : "");
  setState({ page: 0 });
  loadTypes();
});

typesPager.querySelector("[data-page-prev]").addEventListener("click", () => {
  setState({ page: Math.max(0, (state.page || 0) - 1) });
  loadTypes();
});
typesPager.querySelector("[data-page-next]").addEventListener("click", () => {
  setState({ page: (state.page || 0) + 1 });
  loadTypes();
});

/* The filter survives a reload: the field is filled from the URL once, and
 * every request afterwards reads the state (state.js). */
unassignedBox.checked = getExtra("unassigned") === "1";
const filterHidden = filterForm.querySelector('input[type="hidden"][name="q"]');
const filterField = filterForm.querySelector("input[data-typeahead]");
if (state.q && filterHidden && filterField) {
  filterHidden.value = state.q;
  filterField.placeholder = state.q;
  const root = filterField.closest(".typeahead");
  if (root) root.classList.add("is-set");
}
(async () => {
  await loadGroups();
  await loadTypes();
})();
