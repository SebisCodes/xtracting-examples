/* ==========================================================================
 *  The Events view.
 *
 *  One term, two meanings, chosen by the switch: "by entity" asks what
 *  happened around something (and a bucket name means every member of the
 *  bucket), "by type" asks for every event of a kind. The two typeaheads
 *  are both in the page and the inactive one is hidden, so switching keeps
 *  what was typed.
 *
 *  THE TERM LIVES IN THE URL, not in the field. state.js fills the fields
 *  once when the page loads; every request afterwards reads the state. That
 *  is what makes paging, switching the mode and reloading the page all keep
 *  the search.
 *
 *  The list shows what was RESOLVED as well as what was found: "Apple" as a
 *  bucket of two members is a different answer from "Apple" as one entity,
 *  and a person has to be able to see which of the two they got.
 * ========================================================================== */

import { api, isAbort, fmtDate, sentence, watchSearch } from "./api.js";
import { state, set as setState, setExtra, getExtra } from "./state.js";
import { announce } from "./a11y.js";
import { renderResolved, resolvedNotice, searchFor } from "./resolved.js";
import "./typeahead.js";

const MODES = ["entity", "type"];

const form = document.getElementById("events-form");
/* Every request on these channels turns the button quiet and writes
 * "Searching…" beside it, without this file having to remember to
 * (static/js/api.js: watchSearch). */
watchSearch(form, "events");
const list = document.getElementById("event-list");
const countBox = document.getElementById("events-count");
const statusBox = document.getElementById("events-status");
const resolvedBox = document.getElementById("events-resolved");
const pager = document.getElementById("events-pager");
const pageInfo = document.getElementById("events-page-info");
const fromField = document.getElementById("ev-from");
const toField = document.getElementById("ev-to");

let mode = MODES.includes(getExtra("by")) ? getExtra("by") : "entity";

function el(tag, className, text) {
  const e = document.createElement(tag);
  if (className) e.className = className;
  if (text !== undefined && text !== null) e.textContent = text;
  return e;
}

function withContext(href) {
  const url = new URL(href, window.location.origin);
  if (state.project) url.searchParams.set("project", state.project);
  if (state.language) url.searchParams.set("language", state.language);
  return url.pathname + url.search;
}

/* WHAT HAPPENED, then WHAT TO DO - two blocks, the way the toast does it. */
function errorNotice(what, err) {
  const box = el("div", "notice notice--error");
  const body = el("div", "notice-body");
  const line = el("p", "notice-what");
  line.appendChild(el("strong", null, what));
  const said = sentence(err && err.message);
  if (said) line.appendChild(document.createTextNode(" " + said));
  body.appendChild(line);
  const hint = sentence(err && err.hint);
  if (hint) body.appendChild(el("p", "notice-hint", hint));
  box.appendChild(body);
  return box;
}

/* The typeahead of one mode. Its hidden input carries the value; the text
 * field shows it as a placeholder and stays empty (see typeahead.js). */
function fieldOf(name) {
  const wrap = document.querySelector(`[data-mode-field="${name}"]`);
  return wrap ? {
    wrap,
    hidden: wrap.querySelector('input[type="hidden"]'),
    input: wrap.querySelector("input[data-typeahead]"),
    root: wrap.querySelector(".typeahead"),
  } : null;
}

/* Fill a field without telling anybody: the once-only "fill from the URL",
 * and the same three lines after a search, so that a term which has just
 * been asked for IS the set value afterwards. It must not look like
 * somebody chose something - no typeahead:choose, or search() would run a
 * second time on its own answer. */
function fillFromUrl(name, value) {
  const f = fieldOf(name);
  if (!f) return;
  f.hidden.value = value || "";
  f.input.value = "";
  f.input.placeholder = value || f.input.dataset.placeholder || "";
  f.root.classList.toggle("is-set", Boolean(value));
}

/* WHAT THE READER IS ASKING FOR, in the field of the mode that is open.
 *
 * TYPED TEXT WINS OVER WHAT IS SET, because "Show events" is the Enter key
 * with a mouse, and Enter counts the typed text ("one need not be in the
 * list to search for it", typeahead.js). typeahead.js writes into the
 * hidden input only when a suggestion or Enter confirms the text, so
 * reading the hidden input alone threw away everything somebody typed and
 * then reached for the button with: the box still read "Zzqxwv" while the
 * panel answered with every event in the project, as if that were the
 * answer. The list itself promises the opposite in writing - "no suggestion
 * - your text is still searched". */
function termOf(name) {
  const f = fieldOf(name);
  if (!f) return "";
  const typed = f.input ? f.input.value.trim() : "";
  return typed || (f.hidden ? f.hidden.value.trim() : "");
}

function setMode(next, { reload = true } = {}) {
  if (!MODES.includes(next) || next === mode) return;
  mode = next;
  applyMode();
  setExtra("by", mode === "entity" ? "" : mode);
  // The term belongs to the mode: "Apple" as an entity is not "Apple" as an
  // event type, and carrying it over would show an empty list and no reason.
  // Text left standing in the field that comes back is part of that term
  // (termOf), so it is committed here as well - a box reading "Zzqxwv" over
  // a list of everything is the same untruth whichever door it came in by.
  const term = termOf(mode);
  fillFromUrl(mode, term);
  setState({ q: term, page: 0 });
  if (reload) load();
}

function applyMode() {
  MODES.forEach((name) => {
    const f = fieldOf(name);
    if (f) f.wrap.hidden = name !== mode;
  });
  document.querySelectorAll("[data-mode]").forEach((b) => {
    b.setAttribute("aria-pressed", b.dataset.mode === mode ? "true" : "false");
  });
}

/* ── Rendering ───────────────────────────────────────────────────────── */

function eventCard(ev) {
  const li = el("li", "event-card");

  const head = el("h3");
  if (ev.type) {
    const tag = el("a", "tag", ev.type);
    tag.href = withContext(`/events?by=type&q=${encodeURIComponent(ev.type)}`);
    tag.title = `Every event of type ${ev.type}`;
    head.appendChild(tag);
    head.appendChild(document.createTextNode(" "));
  }
  head.appendChild(document.createTextNode(ev.name || "(unnamed event)"));
  li.appendChild(head);

  const when = el("p", "event-when");
  const time = el("time", null, fmtDate(ev.date, { time: false }));
  time.dateTime = ev.date || "";
  when.appendChild(time);
  if (!ev.dated) {
    when.appendChild(document.createElement("br"));
    const note = el("span", "event-undated", "date of the document");
    note.title = "The event itself carries no date, so the document's date is shown.";
    when.appendChild(note);
  }
  li.appendChild(when);

  if (ev.description && ev.description !== ev.name) {
    li.appendChild(el("p", "event-description", ev.description));
  }

  if (ev.entities && ev.entities.length) {
    const about = el("p", "event-entities");
    about.appendChild(el("span", "entities-label", "About:"));
    ev.entities.forEach((e) => {
      const chip = el("a", "chip", e.name || e.id);
      chip.href = withContext(`/diagrams/entity?q=${encodeURIComponent(e.name || "")}`);
      chip.title = e.type ? `${e.name} (${e.type}) - show the diagrams` : "Show the diagrams";
      about.appendChild(chip);
    });
    li.appendChild(about);
  } else {
    li.appendChild(el("p", "event-entities muted", "About the document as a whole"));
  }

  const meta = el("p", "event-meta");
  // WHERE IT CAME FROM, AS WORDS. `sources.text_name` can be `src_1127664`
  // or an md5 in every row of an archive, so the server sends the domain
  // and the address's own slug instead (app/source_names.py). The id
  // reaches nobody: this reads what it was given and never falls back to
  // a name it was not sent.
  if (ev.source && (ev.source.uri || ev.source.name)) {
    const span = el("span");
    span.appendChild(document.createTextNode("Source: "));
    const text = ev.source.name || ev.source.uri;
    // Only a real web address becomes a link: an archive can hold sources
    // like `src:city-archive-foia-2006`, and an anchor whose scheme no
    // browser knows is a link that looks live and does nothing.
    if (/^https?:\/\//i.test(String(ev.source.uri || ""))) {
      const a = el("a", null, text);
      a.href = ev.source.uri;
      a.rel = "noopener noreferrer";
      a.target = "_blank";
      span.appendChild(a);
    } else {
      span.appendChild(document.createTextNode(text));
    }
    meta.appendChild(span);
  }
  // NOT ev.relation. "Relation to the document: Direct" stood on every card
  // with nothing to contrast it against and no legend - extraction
  // vocabulary in front of somebody who is reading about a company. The
  // field stays in the API answer (the export carries it); the card shows
  // what A5 asks for: date, type, description, entities, source.
  if (meta.childNodes.length) li.appendChild(meta);
  return li;
}

/* WHERE THE NAMES CAME FROM, said once.
 *
 * `sources.text_name` is an identifier in every row of this archive, so
 * app/source_names.py builds a name out of the document's address instead
 * and marks it `derived`. A derivation must never read as a quotation, so
 * the list says where its names came from - once, because it is a fact
 * about the list and twenty repetitions of it would be noise. */
function noteDerivedNames(events) {
  const note = document.getElementById("events-derived-note");
  if (!note) return;
  const any = events.some((ev) => ev.source && ev.source.derived);
  note.textContent = any
    ? "Document names in this list were read from their web address."
    : "";
  note.hidden = !any;
}

/* Does the term name anything the archive holds?
 *
 * Two places on the screen depend on the answer - the notice above the list
 * and the empty line inside it - and they must never disagree, so both ask
 * this rather than each testing the response for themselves. */
function termIsUnknown(data) {
  const r = data.resolved || {};
  if (!data.q) return false;
  if (r.match === "bucket" && r.bucket) return false;
  return r.match === "none" || (!data.total && !(r.names && r.names.length));
}

/* Search for one term as if it had been chosen from the list - what the
 * "Show only Apple Inc." button in the notice does. */
function chooseTerm(term) {
  const f = fieldOf("entity");
  if (f) searchFor(f.input, term);
}

/* What the term turned into. A bucket, a prefix match on several types, or
 * nothing at all - all three change what the list means.
 *
 * The bucket half of it is shared with Diagrams, the Graph and the Map
 * (static/js/resolved.js): four views were saying it in four sets of words,
 * and three of them said an untruth for a member's name - «"Apple Inc." is
 * a bucket» when the bucket is called Apple. It also carries the control
 * that leaves the bucket for the single entity, which is why this writes
 * through renderResolved() rather than into textContent. */
function showResolved(data) {
  const r = data.resolved || {};
  const term = data.q;
  if (!term) {
    renderResolved(resolvedBox, null);
    return;
  }
  const notice = resolvedNotice(r, term);
  if (notice.text) {
    renderResolved(resolvedBox, notice, chooseTerm);
    return;
  }
  let text = "";
  if (termIsUnknown(data)) {
    text = `Nothing in this project is called “${term}”.`;
  } else if (r.match === "prefix" || r.match === "substring") {
    const names = (r.names || []).slice(0, 6).join(", ");
    text = names ? `“${term}” matched ${names}.` : "";
  }
  renderResolved(resolvedBox, { text, action: null });
}

/* "a, b, or c" - and just "a" when there is one. */
function joinRemedies(list) {
  if (list.length <= 1) return list[0] || "";
  return `${list.slice(0, -1).join(", ")}, or ${list[list.length - 1]}`;
}

/* Nothing in the list, and WHY it might be nothing.
 *
 * One fixed sentence - "No events for this search. Try a wider date range,
 * or the other way of looking them up." - offered a wider date range to
 * somebody who had set no dates at all. An empty state that names filters
 * which are not in force reads as a list of things one has done wrong, and
 * two thirds of this one was always about a control that was empty.
 *
 * So every clause names something that is on the screen with a value in it,
 * the way the Query view builds its own empty line. The one clause that is
 * dropped even when its control IS set: a date range cannot make a name the
 * archive has never heard of appear, and the notice above the list has just
 * said the name is unknown - so an unknown term is offered the other way of
 * asking and nothing else. */
function emptyLine(data) {
  const dated = Boolean(fromField.value || toField.value);
  if (!data.q) {
    return dated
      ? "No events in this date range. Try a wider one."
      : "No events in this project and language yet.";
  }
  const remedies = [];
  if (dated && !termIsUnknown(data)) remedies.push("widen the date range");
  remedies.push(mode === "entity"
    ? "look them up by type instead"
    : "look them up by entity instead");
  return `No events for this search. Try to ${joinRemedies(remedies)}.`;
}

/* One page of events shows no pager at all; more than one shows both
 * buttons with the dead one disabled and the position spelled out.
 *
 * A control that cannot act must not be offered: a "Next" on the last page
 * of eight events would walk to a page that does not exist and answer with
 * "No events in this project and language yet." - a false statement about a
 * project that has eight of them. The one case where the pager is shown
 * although there is a single page is when the URL already asks for a later
 * one: then Previous is the way back, and hiding it would strand the reader.
 */
/* THE BLOCK THIS BROWSER IS HOLDING - the Query view's arrangement, in the
 * same words: one request brings two hundred rows and the pager moves inside
 * them, so nine presses out of ten are a re-draw and not a query. Twenty are
 * drawn at a time, which is what keeps a modest machine quick: the network
 * brought two hundred, but two hundred cards in the document is what makes a
 * page crawl. */
let block = null;

function sliceFor(data, page) {
  const size = Number(data.page_size) || 20;
  const first = Number(data.block_first) || 0;
  const rows = data.events || [];
  const from = page * size - first;
  if (from < 0 || from >= rows.length) return null;
  return rows.slice(from, from + size);
}

function renderPager(data, at = null) {
  const pages = Math.max(1, data.pages || 1);
  const page = at === null ? (data.page || 0) : at;
  pager.hidden = pages <= 1 && page <= 0;
  if (pager.hidden) {
    pageInfo.textContent = "";
    return;
  }
  const events = `${data.total} event${data.total === 1 ? "" : "s"}`;
  // "Page 2 of 1" is not a sentence; a page past the end says so instead.
  pageInfo.textContent = page + 1 > pages
    ? `Past the last page - ${pages} page${pages === 1 ? "" : "s"}, ${events}`
    : `Page ${page + 1} of ${pages} - ${events}`;
  pager.querySelector("[data-page-prev]").disabled = page <= 0;
  pager.querySelector("[data-page-next]").disabled = page + 1 >= pages;
}

/* One page of the block that is in hand. Called by `load` when an answer
 * arrives and by the pager when it does not need one. */
function drawPage(data, page) {
  list.textContent = "";
  list.setAttribute("aria-busy", "false");
  // Three different things can leave the list empty, and they need three
  // different sentences: nothing archived, nothing matching, or a page
  // number past the end. The last one is not an empty archive and must
  // never be worded like one.
  const rows = sliceFor(data, page) || [];
  const pastTheEnd = data.total > 0 && !rows.length;
  if (!rows.length) {
    list.appendChild(el("li", "empty",
      pastTheEnd ? "That page is past the last one - go back with Previous."
                 : emptyLine(data)));
  } else {
    rows.forEach((ev) => list.appendChild(eventCard(ev)));
  }
  // One line where any document name was worked out from an address rather
  // than read from the archive - once for the list, not once per card,
  // because it is a fact about the list.
  noteDerivedNames(rows);
  renderPager(data, page);
  if (pastTheEnd) {
    const prev = pager.querySelector("[data-page-prev]");
    if (prev && !prev.disabled) prev.focus();
  }
}

/* Move to a page, WITHOUT ASKING THE ARCHIVE when the block already holds
 * it - which is nine presses out of ten. */
function goToPage(next) {
  const page = Math.max(0, next);
  setState({ page });
  if (block && sliceFor(block, page)) {
    drawPage(block, page);
    if (list.scrollIntoView) list.scrollIntoView({ block: "start" });
    return;
  }
  load();
}

/* ── Loading ─────────────────────────────────────────────────────────── */

async function load() {
  if (!list) return;
  // The rows in hand answer the question that fetched them and no other.
  block = null;
  list.setAttribute("aria-busy", "true");
  statusBox.textContent = "Loading…";
  const params = {
    by: mode,
    q: state.q,
    date_from: fromField.value,
    date_to: toField.value,
    page: state.page || 0,
  };
  try {
    // Not `quiet`: api.js raises the toast, from the same fragments this
    // panel is built from and finished by the same sentence() (see api.js).
    // While the view had its own copy of that finishing the two disagreed -
    // "The date range ends before it starts." in the list, "the date range
    // ends before it starts" in the corner - so the view kept the toast
    // away and raised its own. One finishing, one toast, one wording.
    const data = await api("/api/events", { channel: "events", params });
    // The rows are replaced only when the answer is here: NN/g on progress -
    // what was on screen stays there while the next page is on its way.
    list.textContent = "";
    list.setAttribute("aria-busy", "false");
    statusBox.textContent = "";
    countBox.textContent = data.total
      ? `${data.total} event${data.total === 1 ? "" : "s"}`
      : "no events";
    showResolved(data);
    block = data;
    drawPage(data, data.page || 0);
    announce(`${data.total} events, page ${data.page + 1}`);
  } catch (err) {
    if (isAbort(err)) return;
    list.setAttribute("aria-busy", "false");
    statusBox.textContent = "";
    list.textContent = "";
    // The rows are gone, so the pager that counted them goes too.
    pager.hidden = true;
    pageInfo.textContent = "";
    const li = el("li");
    // Both halves, kept apart: the API always sends a hint, so `hint ||
    // message` would throw the reason away and keep only the remedy - and
    // the two glued into one line read as neither.
    li.appendChild(errorNotice("Could not load the events.", err));
    list.appendChild(li);
    // The toast that carries this to somebody who has scrolled past the
    // panel is already up: api.js raised it, in these same words.
  }
}

function search() {
  const term = termOf(mode);
  // The field, the hidden input and the URL say the same thing from here
  // on: what was typed has just been searched for, so it is what is set.
  fillFromUrl(mode, term);
  setState({ q: term, page: 0 });
  setExtra("from", fromField.value);
  setExtra("to", toField.value);
  load();
}

/* ── Wiring ──────────────────────────────────────────────────────────── */

document.querySelectorAll("[data-mode]").forEach((button) => {
  button.addEventListener("click", () => setMode(button.dataset.mode));
});

// Choosing a suggestion searches at once: the value is set, and asking for
// a second click on "Show events" only to repeat what was just chosen is
// the kind of step people stop making.
//
// NO `if (!loading)` HERE. A guard against two requests at once would
// throw away the one thing a person has actually asked for: the view fires
// its first, termless load before anybody has typed, and while that is in
// flight - up to 1.5 s when /api/events JIT-compiles itself (see
// routers/api_events.py) - choosing "Regulatory action" would do nothing at
// all, and the full list would then arrive on top as if the choice had
// never happened. api.js aborts the previous request on the same channel, so a
// second search is safe: the older answer is superseded, not raced.
// ENTER ASKS, A CLICK IN THE LIST DOES NOT. Both gestures arrive as the same
// event, so the one thing that tells them apart is `via`: "enter" is somebody
// ordering the answer, "pick" is somebody still writing the question. The
// value is taken either way - it is what the field now says - and only the
// order runs the query. A search started by a click is one the reader did not
// ask for and cannot stop, and on a big project that is a minute of database
// each time the mouse lands in a list.
form.addEventListener("typeahead:choose", (e) => {
  if (e.detail && e.detail.ask) search();
});

form.addEventListener("submit", (e) => {
  e.preventDefault();
  search();
});

/* The date a preset stands for, as the field spells it. */
function presetStart(days) {
  const from = new Date();
  from.setDate(from.getDate() - Number(days));
  return from.toISOString().slice(0, 10);
}

/* Which preset is in force, after a click AND after a reload. Without this
 * the row is four blue words with nothing to say which of them the list is
 * showing - and after a reload only the raw date in the From field hinted at
 * it. They are toggle buttons, so `aria-pressed` is the state and app.css
 * paints the pressed one like the by entity / by type switch. */
function markPresets() {
  document.querySelectorAll("[data-preset]").forEach((button) => {
    const days = button.dataset.preset;
    const on = days
      ? fromField.value === presetStart(days) && !toField.value
      : !fromField.value && !toField.value;
    button.setAttribute("aria-pressed", on ? "true" : "false");
  });
}

document.querySelectorAll("[data-preset]").forEach((button) => {
  button.addEventListener("click", () => {
    const days = button.dataset.preset;
    fromField.value = days ? presetStart(days) : "";
    toField.value = "";
    markPresets();
    search();
  });
});

// A date typed by hand is not a preset, and the row has to stop claiming
// that it is.
[fromField, toField].forEach((field) => {
  field.addEventListener("change", markPresets);
  field.addEventListener("input", markPresets);
});

pager.querySelector("[data-page-prev]").addEventListener("click", () => {
  if (state.page > 0) goToPage(state.page - 1);
});
pager.querySelector("[data-page-next]").addEventListener("click", () => {
  goToPage((state.page || 0) + 1);
});

/* Fill the form from the URL once, then ask. */
fillFromUrl(mode, state.q);
applyMode();
fromField.value = getExtra("from");
toField.value = getExtra("to");
markPresets();
load();
