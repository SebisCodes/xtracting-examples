/* ==========================================================================
 *  The Diagrams view.
 *
 *  Eight tabs about one subject. ONLY THE OPEN TAB IS FETCHED - that is the
 *  whole reason the page is fast enough to be useful - and each tab keeps
 *  its OWN timeframe and page, because "the last 24 hours of events" and
 *  "the last three years of ratings" are two different questions and moving
 *  from one tab to the next should not reset the answer to either.
 *
 *  THE TERM LIVES IN THE URL (state.js), never in the field. The search box
 *  is filled from ?q once, by the server, when the page renders; every
 *  request afterwards reads state.q. That is what makes switching tabs,
 *  stepping through periods and reloading the page all keep the search -
 *  and it is what tests/ui/test_diagrams_q_preserved.py pins.
 *
 *  The field is read at ONE moment only, when "Show diagrams" is pressed:
 *  what is typed there and never confirmed is still what the reader is
 *  asking for (term()), and it goes into the URL immediately. So the box
 *  and the address bar can never say two different things.
 *
 *  EVERY SCOPE HAS THE BOX, THE SUMMARY INCLUDED. On the summary the term
 *  is put to all five kinds at once and the page is the union of what they
 *  answer (app/scope.py), which is the only scope where an EMPTY box is a
 *  picture rather than a prompt: it is the whole project.
 *
 *  THE AXIS IS PART OF THE TERM, AND IT LIVES IN THE URL TOO. The same word
 *  answers two questions - "Apple Inc." the company and "Company" the class
 *  - and the switch in front of the box says which is being asked. It goes
 *  into ?axis= for the same reason ?q= does: a reload, a tab switch, a
 *  shared link and the Export menu all have to show the same picture. And
 *  it travels with every drilldown, or clicking a bar in a type search
 *  would list the rows of something else entirely.
 *
 *  Clicking a bar - or pressing Enter on a number in the table under it -
 *  opens the drilldown: the rows that were counted at that point, page by
 *  page, with the same CSV behind a link.
 * ========================================================================== */

import { api, isAbort, watchSearch } from "./api.js";
import { state, set as setState, setExtra, onChange } from "./state.js";
import { announce } from "./a11y.js";
import { renderChart, rowsWord } from "./charts.js";
import { openDrilldown } from "./drilldown.js";
import { renderResolved, resolvedNotice, searchFor } from "./resolved.js";
import { createMiniMap } from "./minimap.js";
import "./typeahead.js";

const grid = document.getElementById("charts");
/* THE SUMMARY BAND, above the grid: the charts the registry marks `band`
 * (app/charts/__init__.py). It is filled from the same answer the grid is -
 * one request, one sort - so a band and a grid can never be of two different
 * periods, and it is emptied and hidden by every state where the grid has
 * nothing to show. */
const band = document.getElementById("chart-band");
/* The section the band and the map live in, at the foot of the page behind a
 * rule. It carries the heading, so it is what is shown and hidden - a rule
 * and the word "Summary" over nothing is worse than no summary at all. */
const summaryBox = document.getElementById("chart-summary");
const scope = grid ? grid.dataset.scope || "summary" : "summary";
const tabStrip = document.querySelector('[role="tablist"]');
const tabs = Array.from(document.querySelectorAll('[role="tab"]'));
const toolbar = document.querySelector("[data-timeframe-toolbar]");
const caption = toolbar ? toolbar.querySelector("[data-period-caption]") : null;
const timeframeSelect = toolbar ? toolbar.querySelector("[data-timeframe-select]") : null;
const searchForm = document.getElementById("diagrams-search");
/* Every request on these channels turns the button quiet and writes
 * "Searching…" beside it, without this file having to remember to
 * (static/js/api.js: watchSearch). */
watchSearch(searchForm, "diagrams");
const resolvedBox = document.getElementById("diagrams-resolved");
const statusLine = document.querySelector("[data-grid-status]");
const axisSwitch = searchForm ? searchForm.querySelector("[data-axis-switch]") : null;

/* WHICH AXIS THE TERM IS READ ON. The server renders the page with it
 * already decided (the URL's ?axis=, or this scope's own default - "type"
 * on the events page, whose box has always searched the event type), and
 * the switch is drawn pressed on that half. Reading it back off the form
 * rather than re-deriving it here means the page and the server cannot
 * disagree about which question is being asked. */
let axis = (searchForm && searchForm.dataset.axis) || "object";

/* What the field asks for on each axis of this scope: the label, the
 * suggestion list and the placeholder. The template renders the current
 * one; this is the other one, so the switch can change the field without a
 * round trip. Kept in step with templates/diagrams.html by
 * tests/ui/test_diagrams_type_search.py. */
const AXIS_FIELDS = {
  entity: {
    object: ["Entity or bucket", "/api/suggest/entity_or_bucket", "Search for an entity or bucket"],
    type: ["Entity type", "/api/suggest/entity_type", "Search for an entity type"],
  },
  source: {
    object: ["Source or host", "/api/suggest/source", "Search for a source or host"],
    type: ["Source type", "/api/suggest/source_type", "Search for a source type"],
  },
  location: {
    object: ["Place", "/api/suggest/address", "Search for a place"],
    type: ["Place type", "/api/suggest/location_type", "Search for a place type"],
  },
  market: {
    object: ["Entity or bucket", "/api/suggest/entity_or_bucket", "Search for an entity or bucket"],
    type: ["Topic", "/api/suggest/market_topic", "Search for a market topic"],
  },
  events: {
    object: ["Event", "/api/suggest/event", "Search for an event by name"],
    type: ["Event type", "/api/suggest/event_type", "Search for an event type"],
  },
};

/* The word for what one axis of one scope searches, for the sentence that
 * says what was found and for the one that says nothing was. */
const AXIS_SUBJECT = {
  entity: { object: "entity", type: "entity type" },
  source: { object: "source", type: "source type" },
  location: { object: "place", type: "place type" },
  market: { object: "entity", type: "topic" },
  events: { object: "event", type: "event type" },
};

const DEFAULT_TAB = tabs.length ? tabs[0].dataset.tab : "sources";

/* WHAT THE EXPORT MENU ASKS FOR, AND WHY IT IS IN THE URL.
 *
 * The Export menu in the top bar is shared by every view (_macros.html) and
 * static/js/export.js builds its two links out of the CURRENT URL - the same
 * parameters the view itself was opened with, which is what makes the file
 * hold what the screen holds. This page has two parameters a URL can easily
 * fail to carry: the SCOPE is a path segment (/diagrams/entity) and the open
 * TAB is easy to leave out whenever it is the default one - and then
 * "Export ▾ → CSV" asks for the summary's Sources tab whatever is on the
 * screen.
 *
 * So the page writes both into the URL instead of rewriting the menu's links
 * behind its back: export.js is imported lazily by layout.js and therefore
 * always refreshes them last, and two modules writing one href is a race
 * nobody can read afterwards. The URL is the state on this dashboard
 * (state.js); making it complete answers the menu, the reload and the Back
 * button in one place.
 *
 * The endpoint is GET /api/export/diagrams.csv|.json and it reads the same
 * parameter names the drilldown file already uses (routers/api_diagrams.py:
 * scope, tab, q, timeframe, page).
 */
function writeScopeToUrl() {
  setExtra("scope", scope);
  // The axis belongs in the URL for exactly the reasons the scope and the
  // tab do: the Export menu builds its links out of it, a reload has to
  // come back to the same picture, and a link that does not carry it shows
  // the reader a different question from the one that was asked.
  if (axisSwitch) setExtra("axis", axis);
}

function tabLabel(id) {
  const button = tabs.find((t) => t.dataset.tab === id);
  return button ? button.textContent.trim() : id;
}

/* Each tab's own timeframe and page. The ACTIVE tab's pair is mirrored into
 * the URL, so a reload comes back to the same picture; the others live for
 * as long as the page does, which is what a tab strip promises.
 *
 * A tab that is opened for the FIRST time inherits whatever timeframe is in
 * the URL - the one the reader is looking at. Somebody who has set "Last
 * year" and switches tabs means to keep looking at the last year; only a
 * tab they have already set a period on keeps its own. */
const perTab = new Map();
let activeTab = tabs.some((t) => t.dataset.tab === state.tab) ? state.tab : DEFAULT_TAB;
let rendered = [];

function frameOf(tab) {
  if (!perTab.has(tab)) {
    perTab.set(tab, {
      // The period on screen, which is the URL's when there is one and the
      // server's default otherwise - the select was rendered with exactly
      // that value, so reading it here cannot disagree with the page.
      timeframe: (tab === activeTab && state.timeframe)
        || (timeframeSelect ? timeframeSelect.value : "7d"),
      page: tab === activeTab ? (state.page || 0) : 0,
    });
  }
  return perTab.get(tab);
}

function el(tag, className, text) {
  const e = document.createElement(tag);
  if (className) e.className = className;
  if (text !== undefined && text !== null) e.textContent = text;
  return e;
}

function sentence(text) {
  const clean = String(text == null ? "" : text).trim();
  if (!clean) return "";
  const capital = clean.charAt(0).toUpperCase() + clean.slice(1);
  return /[.!?…]$/.test(capital) ? capital : capital + ".";
}

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

/* ── Tabs (WAI-ARIA APG: click, arrows, Home/End) ──────────────────────── */

function applyTab() {
  tabs.forEach((tab) => {
    const on = tab.dataset.tab === activeTab;
    tab.setAttribute("aria-selected", on ? "true" : "false");
    tab.tabIndex = on ? 0 : -1;
  });
  if (grid) grid.setAttribute("aria-labelledby", `tab-${activeTab}`);
  if (toolbar) {
    toolbar.dataset.timeframeToolbar = activeTab;
    toolbar.setAttribute("aria-label", `Timeframe for ${activeTab}`);
  }
  const frame = frameOf(activeTab);
  if (timeframeSelect) timeframeSelect.value = frame.timeframe;
  syncUrl();
}

function syncUrl() {
  const frame = frameOf(activeTab);
  // THE PERIOD IS ALWAYS WRITTEN OUT, even when it is the default one.
  // Leaving it out whenever it equals "the current default" loses the
  // choice: the default is read from the URL the page was opened with, so
  // choosing "Last year" makes "Last year" the default and the parameter
  // is then dropped - and the reload comes back on Last 7 days with the
  // page number still set. A period nobody can see in the URL is
  // also a period the printed header (static/js/export.js) cannot name.
  // THE TAB IS ALWAYS WRITTEN OUT TOO, for the same reason as the period: a
  // URL that leaves out "the default tab" cannot be exported (the file would
  // be of whatever the server calls the first tab) and cannot be read by
  // anyone who has to know what a link shows.
  setState({ tab: activeTab,
             timeframe: frame.timeframe,
             page: frame.page });
}

/* THE STRIP THAT WAS JUST PRESSED, AT THE TOP OF THE SCREEN.
 *
 * Two strips stand above the charts - "About:", which picks the subject, and
 * "Charts:", which picks the pictures - and they look alike. Pressing a tab
 * halfway down a long page left the reader looking at whatever they had
 * scrolled to, with the strip they had just used off screen and the other
 * one nowhere in sight; the next press then went to the wrong strip.
 *
 * So the Charts strip comes to the top when a tab is chosen. Only when the
 * page has actually been scrolled past it: scrolling a page that is already
 * at the top is a jump for no reason.
 *
 * `smooth` unless the reader has asked for less motion, which is the same
 * rule the rest of this product follows for anything that moves by itself. */
function showTabStrip() {
  const row = document.querySelector(".tabs-row");
  if (!row || typeof row.scrollIntoView !== "function") return;
  const box = row.getBoundingClientRect();
  if (box.top >= 0 && box.top <= 24) return;
  const still = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  row.scrollIntoView({ behavior: still ? "auto" : "smooth", block: "start" });
}

function selectTab(next, { focus = false } = {}) {
  if (!next || next === activeTab) return;
  activeTab = next;
  applyTab();
  if (focus) {
    const button = tabs.find((t) => t.dataset.tab === next);
    if (button) button.focus();
  }
  /* AFTER THE NEW CHARTS ARE DRAWN, NOT BEFORE THEM.
   *
   * Scrolling first and loading afterwards is scrolling to where the strip
   * WAS: the cards are replaced, the page gets taller or shorter, and on the
   * tabs whose new content is a different height the reader lands somewhere
   * else - which is exactly what "it only works on some tabs" looks like.
   * The strip is brought up once the page has settled into its new size. */
  load().then(showTabStrip, showTabStrip);
}

tabs.forEach((tab) => {
  tab.addEventListener("click", () => selectTab(tab.dataset.tab));
});

/* Arrows MOVE, Enter and Space CHOOSE (WAI-ARIA APG: manual activation).
 * Each tab is a request to the archive, and selection-follows-focus would
 * fire one per keystroke - arrowing from the first tab to the last would
 * ask for eight tabs and show the wrong charts seven times on the way. The
 * focused tab is reachable by the arrows even while another one is
 * selected, so tabindex follows the focus. */
if (tabStrip) {
  tabStrip.addEventListener("keydown", (event) => {
    const here = tabs.findIndex((t) => t === document.activeElement);
    const index = here >= 0 ? here : tabs.findIndex((t) => t.dataset.tab === activeTab);
    let next = null;
    if (event.key === "ArrowRight") next = tabs[(index + 1) % tabs.length];
    else if (event.key === "ArrowLeft") next = tabs[(index - 1 + tabs.length) % tabs.length];
    else if (event.key === "Home") next = tabs[0];
    else if (event.key === "End") next = tabs[tabs.length - 1];
    else if (event.key === "Enter" || event.key === " ") {
      const chosen = tabs[index];
      if (!chosen) return;
      event.preventDefault();
      selectTab(chosen.dataset.tab, { focus: true });
      return;
    }
    if (!next) return;
    event.preventDefault();
    tabs.forEach((t) => { t.tabIndex = t === next ? 0 : -1; });
    next.focus();
  });
}

/* ── The timeframe toolbar ─────────────────────────────────────────────── */

function stepPeriod(delta) {
  const frame = frameOf(activeTab);
  // Page 0 is the window that ends now; a higher page is further back, so
  // "Earlier" counts up. Below zero is the future and there is nothing there.
  const next = Math.max(0, frame.page + delta);
  if (next === frame.page) return;
  frame.page = next;
  syncUrl();
  load();
}

if (toolbar) {
  const prev = toolbar.querySelector("[data-timeframe-prev]");
  const next = toolbar.querySelector("[data-timeframe-next]");
  if (prev) prev.addEventListener("click", () => stepPeriod(1));
  if (next) next.addEventListener("click", () => stepPeriod(-1));
  if (timeframeSelect) {
    timeframeSelect.addEventListener("change", () => {
      const frame = frameOf(activeTab);
      frame.timeframe = timeframeSelect.value;
      // A new timeframe starts at the window that ends now: page 3 of
      // "last 24 hours" is a different time from page 3 of "last year",
      // and keeping the number would jump somewhere nobody asked for.
      frame.page = 0;
      syncUrl();
      load();
    });
  }
}

/* The dates a window covers, in UTC and in words.
 *
 * UTC ON PURPOSE: the buckets are cut in UTC (app/timeframes.py), so a
 * browser in Zurich formatting the exclusive end (30 September, 24:00 UTC)
 * in local time prints 1 October and the period reads one day too long.
 */
const DAY_PARTS = { day: "numeric", month: "short", year: "numeric", timeZone: "UTC" };
const MINUTE_PARTS = { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit",
                       hour12: false, timeZone: "UTC" };

function periodRange(window_) {
  if (!window_ || !window_.start || !window_.end) return "";
  const start = new Date(window_.start);
  // The end is exclusive; the last second inside the window is the date a
  // reader means by "to".
  const last = new Date(new Date(window_.end).getTime() - 1000);
  if (Number.isNaN(start.getTime()) || Number.isNaN(last.getTime())) return "";
  const hourly = window_.unit === "hour";
  const parts = hourly ? MINUTE_PARTS : DAY_PARTS;
  const text = `${start.toLocaleString(undefined, parts)} - ${last.toLocaleString(undefined, parts)}`;
  return hourly ? `${text} UTC` : text;
}

/* What the toolbar's caption says.
 *
 * NOT the timeframe's name: the select standing right next to it already
 * says "Last year", and a caption that repeats its own control adds
 * nothing - at page 0, which is where every reader starts, the toolbar read
 * "◀ [Last year ▾] ▶ Last year". The dates are the thing the select cannot
 * say, and they are the same kind of answer on every page. */
function periodCaption(window_) {
  return periodRange(window_) || (window_ ? window_.caption : "");
}

/* The period where there is no select to read it against - the drilldown
 * dialog, which is opened from a card and stands on its own. There the name
 * of the timeframe is worth its words, and the dates pin it down. */
function periodWords(window_) {
  if (!window_) return "";
  const range = periodRange(window_);
  return range ? `${window_.label} (${range})` : window_.caption;
}

function updateToolbar(window_) {
  const frame = frameOf(activeTab);
  // A missing window leaves the caption alone rather than blanking it: the
  // server writes the period into it for the first paint, and a failed
  // request must not take the page's own answer to "which period is this"
  // away with it.
  if (caption && window_) caption.textContent = periodCaption(window_);
  if (toolbar) toolbar.dataset.page = String(frame.page);
  const next = toolbar ? toolbar.querySelector("[data-timeframe-next]") : null;
  if (next) next.disabled = frame.page <= 0;
}

/* ── Searching ─────────────────────────────────────────────────────────── */

/* WHAT THE READER IS ASKING FOR.
 *
 * TYPED TEXT WINS OVER WHAT IS SET, because "Show diagrams" is the Enter
 * key with a mouse, and Enter counts the typed text ("one need not be in
 * the list to search for it", typeahead.js). typeahead.js writes into the
 * hidden input only when a suggestion or Enter confirms the text, so
 * reading the hidden input alone threw away everything somebody typed and
 * then reached for the button with - and the grid answered "Nothing is
 * charted yet. Search for a market topic above", telling the reader to do
 * the thing they had just done. */
function term() {
  const wrap = searchForm ? searchForm.querySelector(".typeahead") : null;
  const hidden = wrap ? wrap.querySelector('input[type="hidden"][name="q"]') : null;
  const input = wrap ? wrap.querySelector("input[data-typeahead]") : null;
  const typed = input ? input.value.trim() : "";
  return typed || (hidden ? hidden.value.trim() : "");
}

/* Show a term the way the typeahead shows a chosen one: the value in the
 * hidden input, the term as the field's placeholder, the field itself
 * empty. Nothing is dispatched - typeahead:choose would send search()
 * round a second time on its own answer. */
function fillField(value) {
  const wrap = searchForm ? searchForm.querySelector(".typeahead") : null;
  if (!wrap) return;
  const hidden = wrap.querySelector('input[type="hidden"][name="q"]');
  const input = wrap.querySelector("input[data-typeahead]");
  if (hidden) hidden.value = value || "";
  if (input) {
    input.value = "";
    input.placeholder = value || input.dataset.placeholder || "";
  }
  wrap.classList.toggle("is-set", Boolean(value));
}

function search() {
  const next = term();
  // The field, the hidden input and the URL say the same thing from here
  // on, whether or not the term changed anything.
  fillField(next);
  // A PRESS ASKS AGAIN. The same term twice is somebody saying "look once
  // more", and answering it from memory is a button that does nothing.
  if (next === state.q) { load({ fresh: true }); return; }
  // A new subject starts at the newest period again: the page a reader was
  // on for one entity means nothing for another.
  perTab.forEach((frame) => { frame.page = 0; });
  setState({ q: next, page: 0 });
  syncUrl();
  load();
}

if (searchForm) {
  searchForm.addEventListener("submit", (event) => { event.preventDefault(); search(); });
  // Enter asks, a click in the list does not - see events.js for why the
  // two gestures have to be told apart at all.
  searchForm.addEventListener("typeahead:choose", (event) => {
    if (event.detail && event.detail.ask) search();
  });
}

/* ── The axis switch ───────────────────────────────────────────────────── */

/* THE SWITCH CHANGES THE QUESTION, NOT THE ANSWER IN THE BOX.
 *
 * Pressing "Type" re-labels the field, points its suggestion list at the
 * vocabulary table and asks the archive the same word again as a class.
 * What was typed is KEPT: "Company" typed as an object and then switched to
 * a type is exactly the move somebody makes when the first answer was not
 * what they meant, and clearing the box would make them type it twice. If
 * the word is not a type either, the page says so in those words - which is
 * the whole point of naming the axis in the sentence. */
function applyAxisToField() {
  const per = AXIS_FIELDS[scope];
  const spec = per && per[axis];
  const wrap = searchForm ? searchForm.querySelector(".typeahead") : null;
  if (!spec || !wrap) return;
  const [label, url, placeholder] = spec;
  const input = wrap.querySelector("input[data-typeahead]");
  const labelEl = wrap.querySelector(".field-label");
  if (labelEl) labelEl.textContent = label;
  if (input) {
    input.dataset.placeholder = placeholder;
    // The field shows what is SET as its placeholder (typeahead.js), so a
    // set term keeps standing there and only an empty field takes the new
    // prompt.
    if (!wrap.classList.contains("is-set")) input.placeholder = placeholder;
    // The list itself, through the typeahead's own API: it holds the URL
    // and a cache keyed by what was typed, and writing the data attribute
    // alone would leave the field labelled "Entity type" and still
    // offering entity names.
    if (input._typeahead && typeof input._typeahead.setSuggestUrl === "function") {
      input._typeahead.setSuggestUrl(url);
    } else {
      input.dataset.suggest = url;
    }
  }
  if (axisSwitch) {
    axisSwitch.querySelectorAll("[data-axis]").forEach((button) => {
      button.setAttribute("aria-pressed", button.dataset.axis === axis ? "true" : "false");
    });
  }
  if (searchForm) searchForm.dataset.axis = axis;
}

function chooseAxis(next) {
  if (!next || next === axis) return;
  axis = next;
  applyAxisToField();
  // A different question starts at the newest period again, exactly as a
  // different term does.
  perTab.forEach((frame) => { frame.page = 0; });
  setState({ page: 0 });
  syncUrl();
  setExtra("axis", axis);
  announce(`Searching by ${axis === "type" ? "type" : "object"}: ${(AXIS_FIELDS[scope] || {})[axis][0]}`);
  load();
}

if (axisSwitch) {
  axisSwitch.addEventListener("click", (event) => {
    const button = event.target.closest("[data-axis]");
    if (button) chooseAxis(button.dataset.axis);
  });
}

/* What one page of this scope is about, in a phrase that fits into a
 * sentence. The same words as the search field's label and the page's own
 * purpose line (diagrams.html), because they answer the same question -
 * and it changes with the AXIS, because "search for an entity" and "search
 * for an entity type" are two different instructions. */
const SCOPE_SUBJECT = {
  entity: { object: "an entity or a bucket", type: "an entity type" },
  source: { object: "a source or a host", type: "a source type" },
  location: { object: "a place", type: "a place type" },
  market: { object: "a market topic", type: "an outlook or a sentiment" },
  events: { object: "an event", type: "a kind of event" },
};

function subject() {
  const per = SCOPE_SUBJECT[scope];
  return (per && per[axis]) || "one";
}

/* "an entity type", "a place type" - the article the noun actually takes.
 * A sentence that reads "pick a entity type" is a sentence nobody wrote on
 * purpose, and it is in front of a reader who has just failed to find
 * something. */
function anWord(noun) {
  return /^[aeiou]/i.test(noun) ? `an ${noun}` : `a ${noun}`;
}

/* The noun for one thing on this axis ("entity type"), for the sentence
 * that says how big the answer is. */
function axisWord() {
  const per = AXIS_SUBJECT[scope];
  return (per && per[axis]) || "match";
}

/* "42 entities", "1 entity" - the SIZE of what the term resolved to, which
 * is the thing a person needs to know to tell a type search from an object
 * search at a glance. */
function entitiesWord(n) {
  const count = Number(n) || 0;
  return `${count.toLocaleString()} ${count === 1 ? "entity" : "entities"}`;
}

/* WHAT THE SUMMARY FOUND, KIND BY KIND.
 *
 * The summary's magnifier searches everything: the term goes to all five
 * kinds at once and the page is the union of what they answer (app/scope.py,
 * _resolve_everything). So "matched Apple Inc., Apple" would be a half
 * answer - the charts also hold two documents whose address says apple, and
 * a reader who cannot see that cannot tell why the numbers are bigger than
 * the bucket. `resolved.kinds` carries one entry per kind that answered, and
 * this turns it into the sentence. */
const KIND_WORDS = {
  entity: "entities", source: "documents", location: "places",
  market: "market topics", events: "event types",
};

/* THE NAME OF THE GROUPING IS THE GROUPING'S, NEVER THE TYPED TERM.
 *
 * `the bucket “${q}”` would produce «"Federal Bureau of Investigation"
 * matched the bucket "Federal Bureau of Investigation" (Federal Bureau of
 * Investigation)» - the term three times and the bucket's real name
 * nowhere, although the answer carries it. That is the fault
 * static/js/resolved.js exists to prevent, and this view goes through the
 * same rule. */
function describeKind(entry) {
  const all = entry.names || [];
  const shown = all.slice(0, 3).join(", ");
  const more = all.length > 3 ? ", …" : "";
  if (entry.match === "bucket") {
    const group = entry.bucket || {};
    // "colour group" where the grouping is one - a sentence calling it a
    // bucket would send the reader to a page where they will not find it.
    const what = group.source === "colour group" ? "colour group" : "bucket";
    const name = group.name || shown;
    return `the ${what} “${name}”${shown ? ` (${shown}${more})` : ""}`;
  }
  const word = KIND_WORDS[entry.kind] || entry.kind;
  return shown ? `${word} ${shown}${more}` : word;
}

/* Search for one term as if it had been chosen from the list - what the
 * "Show only Apple Inc." button in the notice does. */
function chooseTerm(term) {
  const input = searchForm ? searchForm.querySelector("input[data-typeahead]") : null;
  searchFor(input, term);
}

function showResolved(data) {
  if (!resolvedBox) return;
  const r = data.resolved || {};
  let text = "";
  if (!data.q) {
    // The grid itself says what to do (nothingSearchedState); a second
    // instruction three lines above it is the same answer twice.
    renderResolved(resolvedBox, null);
    return;
  }
  if (scope === "summary") {
    // Before the generic branches, and not only for the wording: a term
    // that matched a document with no entities in it has entities = 0 and
    // charts full of rows, and "nothing is called that" would be a lie.
    const kinds = r.kinds || [];
    // ONE SENTENCE AND ONE CONTROL, FROM THE ONE FUNCTION THE OTHER THREE
    // VIEWS USE. Where a single kind answered and a bucket won, the Summary
    // says word for word what the Map, the Graph and the Events page say;
    // where several kinds answered, the union sentence is the summary's own
    // (a term that also matched two documents cannot be described by the
    // bucket alone) and every bucket in it is named from the BUCKET. Either
    // way the way out of the bucket - "Show only Apple Inc." - is the same
    // control, built by the same function.
    const notice = resolvedNotice(r, data.q);
    if (!kinds.length) {
      renderResolved(resolvedBox, { text: `Nothing in this project is called “${data.q}”.`,
                                    action: null });
      return;
    }
    text = (kinds.length === 1 && notice.text)
      ? notice.text
      : `“${data.q}” matched ${kinds.map(describeKind).join(", ")}.`;
    renderResolved(resolvedBox, { text, action: notice.action }, chooseTerm);
    return;
  }
  // The bucket, and the single member of one, in the words all four views
  // use and with the control that goes to the other of the two
  // (static/js/resolved.js). The size sentence is appended to it, because a
  // bucket search has an axis and a size like any other.
  const notice = resolvedNotice(r, data.q);
  if (notice.text) {
    renderResolved(resolvedBox, { text: `${notice.text} ${sizeSentence(r)}`.trim(),
                                  action: notice.action }, chooseTerm);
    return;
  }
  if (r.match === "none" || !r.entities) {
    text = nothingCalledThat(data.q);
  } else if (r.match === "prefix" || r.match === "substring") {
    const names = (r.names || []).slice(0, 6).join(", ");
    text = names ? `“${data.q}” matched ${names}. ${sizeSentence(r)}` : sizeSentence(r);
  } else {
    // An exact match still says which question was asked and how big the
    // answer is: "Company - 42 entities" and "Apple Inc. - 1 entity" are
    // the same shape of sentence about two very different pictures, and
    // without it nobody can tell which of the two they are looking at.
    text = sizeSentence(r);
  }
  renderResolved(resolvedBox, { text, action: null });
}

/* WHICH AXIS PRODUCED THE ANSWER, AND HOW BIG IT IS.
 *
 * "Company (entity type) - 42 entities." against "Apple Inc. (entity) -
 * 1 entity." The label is what the SERVER resolved to, not what was typed,
 * so a bucket says the bucket's name; the count is the entity set every one
 * of the eight tabs is built from, which is the number that explains why
 * one search fills the charts and another draws one bar. */
function sizeSentence(resolved) {
  const label = resolved.label || state.q;
  if (!label) return "";
  return `“${label}” (${axisWord()}) - ${entitiesWord(resolved.entities)}.`;
}

/* THE EMPTY ANSWER NAMES THE AXIS. "Nothing in this project is called
 * Widget" is the right sentence for an object search and the wrong one for
 * a type search - the archive may well hold a hundred things called Widget
 * and simply not use it as a type. */
function nothingCalledThat(q) {
  return axis === "type"
    ? `Nothing in this project has the ${axisWord()} “${q}”.`
    : `Nothing in this project is called “${q}”.`;
}

/* ── The drilldown ─────────────────────────────────────────────────────── */

function keyOf(chart, point) {
  if (chart.drill === "bucket") return { bucket: point.x };
  if (chart.drill === "xy") return { x: point.x, y: point.y };
  return { x: point.x };
}

function csvUrl(chart, dataset, key) {
  const frame = frameOf(activeTab);
  const p = new URLSearchParams({
    scope, tab: activeTab, chart_id: chart.id, dataset_id: dataset.id,
    // The axis, or the file is of a different question from the screen.
    axis, timeframe: frame.timeframe, page: String(frame.page),
  });
  if (state.q) p.set("q", state.q);
  if (state.project) p.set("project", state.project);
  if (state.language) p.set("language", state.language);
  if (key.bucket) p.set("key_bucket", key.bucket);
  if (key.x) p.set("key_x", key.x);
  if (key.y) p.set("key_y", key.y);
  return `/api/diagrams/drilldown.csv?${p.toString()}`;
}

/* WHERE A ROW'S TWO OTHER LINKS GO.
 *
 * The domain's own diagrams and the entity's own diagrams - the second and
 * the third link of every row. They are built HERE, not in
 * the drilldown module and not on the server, because the project and the
 * language are the page's own state and a link that loses them lands the
 * reader in a different archive.
 *
 * The axis is written out too: /diagrams/entity opens on the OBJECT axis,
 * which is what a single name is, whatever axis this page was searched on.
 */
function scopeUrl(kind, term) {
  const p = new URLSearchParams();
  if (state.project) p.set("project", state.project);
  if (state.language) p.set("language", state.language);
  p.set("q", term);
  p.set("axis", "object");
  // The tab and the period the reader is on now: a link out of the Ratings
  // tab of the last year that lands on the Sources tab of the last week has
  // answered a question nobody asked.
  const frame = frameOf(activeTab);
  p.set("tab", activeTab);
  p.set("timeframe", frame.timeframe);
  return `/diagrams/${kind}?${p.toString()}`;
}

function openFor(chart, dataset, point) {
  const key = keyOf(chart, point);
  const frame = frameOf(activeTab);
  const where = chart.kind === "matrix"
    ? `${point.x_label || point.x} - ${point.y_label || point.y}`
    : (point.label || point.x);
  const value = chart.kind === "matrix" ? point.v : point.y;
  // The period in words, never the token "1y" - that spelling appears
  // nowhere else a reader can see.
  const period = lastWindow ? periodWords(lastWindow) : frame.timeframe;
  openDrilldown({
    title: `${chart.title} - ${where}`,
    subtitle: `${dataset.label} - ${rowsWord(value)} - ${state.q ? `“${state.q}” - ` : ""}${period}`,
    csvUrl: csvUrl(chart, dataset, key),
    rowsLabel: `Rows behind ${where}`,
    emptyText: "No rows behind this point any more - the archive may have changed.",
    // The second and the third link of every row (see scopeUrl): that
    // domain's diagrams, and that entity's.
    links: { source: (domain) => scopeUrl("source", domain),
             entity: (name) => scopeUrl("entity", name) },
    load: (page) => api("/api/diagrams/drilldown", {
      channel: "drilldown",
      body: {
        // WITH THE AXIS. Without it a click on a bar of a type search would
        // resolve the same word as an object and list the rows of
        // something else - the bar would say 42 and the dialog would show
        // one, with nothing on screen to explain the difference.
        scope, tab: activeTab, q: state.q, axis,
        timeframe: frame.timeframe, page: frame.page,
        chart_id: chart.id, dataset_id: dataset.id, key, ddpage: page,
      },
    }),
  });
}

/* ── The list at the foot of a tab ─────────────────────────────────────── */
/*
 * THREE TABS END IN ROWS RATHER THAN IN A PICTURE, and each of the three
 * answers a different question, so each is laid out for its own:
 *
 *   sources      WHICH documents came in last - the drilldown's own table,
 *                because a reader who clicks a bar meets that shape already
 *   attributes   WHAT the value IS - "name: value unit" on the left, what the
 *                document said about it on the right
 *   market       WHICH readings are strong - the entity, the topic, and the
 *                two horizons side by side
 *
 * A LIST IS ALLOWED TO BE LONG HERE. That is the whole reason the summary
 * moved to the foot of the page: above the grid, twenty-five rows pushed
 * every chart off the first screen.
 *
 * WHAT IS TRUNCATED CAN BE OPENED. A sentence cut at four hundred characters
 * with no way to the rest is a sentence the reader has to go and find
 * somewhere else; "More" opens it where it stands. It is a button and not a
 * link because it goes nowhere - and the whole text is in the DOM either way,
 * so a screen reader and the printer both have all of it.
 */

const LONG_TEXT = 180;

function summaryText(text) {
  const whole = String(text || "").trim();
  const box = el("div", "summary-text");
  if (!whole) return box;
  if (whole.length <= LONG_TEXT) {
    box.textContent = whole;
    return box;
  }
  const shown = el("span", "summary-text-short", whole.slice(0, LONG_TEXT).trimEnd() + "…");
  const rest = el("span", "summary-text-rest", whole);
  rest.hidden = true;
  const more = el("button", "button button--quiet summary-more", "More");
  more.type = "button";
  more.setAttribute("aria-expanded", "false");
  more.addEventListener("click", () => {
    const open = rest.hidden;
    rest.hidden = !open;
    shown.hidden = open;
    more.textContent = open ? "Less" : "More";
    more.setAttribute("aria-expanded", open ? "true" : "false");
  });
  box.append(shown, rest, more);
  return box;
}

/* AN ADDRESS IS ONLY AN ADDRESS WHEN IT IS ONE.
 *
 * `sources.text_uri` is not always a URL. On the legislation archive it holds
 * things like "src:fbi-foi-pa-deleted-17cv03956-appendix" - an identifier the
 * extraction wrote where the document had no address - and a link built from
 * that is a link that goes nowhere, which is worse than no link: it looks
 * like the way to the document and is not. So a row is a link where there is
 * an address, and the text where there is not. */
function linkable(uri) {
  return /^https?:\/\//i.test(String(uri || "").trim());
}

/* The document a row came out of. Never an empty link: a row whose source the
 * archive holds no address for still has to say which document it was. */
function sourceLine(row) {
  const name = String(row.source || row.name || "").trim();
  const uri = String(row.uri || "").trim();
  if (!name && !uri) return null;
  if (!linkable(uri)) return el("span", "summary-source", name || uri);
  const link = el("a", "summary-source", name || uri);
  link.href = uri;
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  return link;
}

function summaryRow(summary, row) {
  const item = el("li", "summary-row");
  const left = el("div", "summary-left");
  const right = el("div", "summary-right");

  if (summary.layout === "attributes") {
    // NAME: VALUE UNIT, and the entity it belongs to under it. The value is
    // the fact; the name without it is a label for nothing.
    const line = el("p", "summary-fact");
    line.append(el("span", "summary-name", `${row.name}: `));
    line.append(el("strong", "summary-value", [row.value, row.unit].filter(Boolean).join(" ")));
    left.appendChild(line);
    const under = [row.entity, row.type].filter(Boolean).join(" - ");
    if (under) left.appendChild(el("p", "summary-meta", under));
  } else if (summary.layout === "connections") {
    /* THE TIE AS A SENTENCE, which is what a connection is.
     *
     * "Bureau Veritas is a certifier of Nordwyk Pumps" says in one line what
     * the matrix above counts and the map below places. The two names are
     * emphasised and the word between them is not, so a column of these
     * reads down the names; the number after it is how many connections say
     * the same thing, because the same tie is usually recorded once per
     * document that states it. */
    const line = el("p", "summary-fact");
    line.append(el("strong", "summary-name", row.parent || "?"));
    line.append(el("span", "summary-meta", ` is a ${row.relation || "party"} of `));
    line.append(el("strong", "summary-name", row.child || "?"));
    left.appendChild(line);
    left.appendChild(el("p", "summary-meta",
                        row.count === 1 ? "1 connection" : `${row.count} connections`));
  } else if (summary.layout === "market") {
    const line = el("p", "summary-fact");
    line.append(el("span", "summary-name", row.entity || "-"));
    if (row.topic) line.append(el("span", "summary-meta", ` - ${row.topic}`));
    left.appendChild(line);
    // BOTH HORIZONS, NAMED. "Very Negative" alone does not say over what.
    const readings = [
      row.short_sentiment ? `short term ${row.short_sentiment}` : "",
      row.long_sentiment ? `long term ${row.long_sentiment}` : "",
    ].filter(Boolean).join(" - ");
    if (readings) left.appendChild(el("p", "summary-meta", readings));
    const under = [row.entity_type, row.high ? "high relevance" : ""].filter(Boolean).join(" - ");
    if (under) left.appendChild(el("p", "summary-meta", under));
  } else {
    const line = el("p", "summary-fact");
    line.append(el("span", "summary-name", row.name || row.uri || "-"));
    left.appendChild(line);
    const under = [row.type, row.importance,
                   row.trustful === false ? "not trustful" : ""].filter(Boolean).join(" - ");
    if (under) left.appendChild(el("p", "summary-meta", under));
  }

  if (row.date) {
    const when = el("p", "summary-meta summary-date", String(row.date).slice(0, 10));
    left.appendChild(when);
  }

  right.appendChild(summaryText(row.summary));
  const source = sourceLine(row);
  if (source && summary.layout !== "sources") {
    const holder = el("p", "summary-meta");
    holder.appendChild(source);
    right.appendChild(holder);
  } else if (summary.layout === "sources" && row.uri && linkable(row.uri)) {
    const holder = el("p", "summary-meta");
    const link = el("a", "summary-source", row.uri);
    link.href = row.uri;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    holder.appendChild(link);
    right.appendChild(holder);
  }

  item.append(left, right);
  return item;
}

/* The whole panel: a heading that says what the list is, the sentence that
 * says how it was narrowed, the rows, and - when there are more than the
 * page holds - how many were left out. Never a silent cut. */
function summaryPanel(summary) {
  const panel = el("section", "summary-panel");
  panel.appendChild(el("h3", "summary-panel-title", summary.title || "Summary"));
  if (summary.note) panel.appendChild(el("p", "summary-panel-note", summary.note));
  const rows = summary.rows || [];
  if (!rows.length) {
    // NOTHING IS AN ANSWER AND HAS TO BE GIVEN. The Market list keeps only
    // readings at an end of the scale, and a whole archive can hold none -
    // which is a fact about the archive, not a page that failed to load. The
    // note above already says what the filter is; this says it found nothing.
    panel.appendChild(el("p", "empty", summary.empty
      || "Nothing in this period matches. The charts above count everything there is."));
    return panel;
  }
  const list = el("ul", `summary-list summary-list--${summary.layout}`);
  rows.forEach((row) => list.appendChild(summaryRow(summary, row)));
  panel.appendChild(list);
  const total = Number(summary.total) || 0;
  const shown = (summary.rows || []).length;
  if (total > shown) {
    panel.appendChild(el("p", "summary-panel-note",
      `${shown} of ${total} shown. The charts above count all of them; `
      + "open a bar to page through the rest."));
  }
  return panel;
}

/* ── The last row of the grid ──────────────────────────────────────────── */
/*
 * NINE CARDS IN A GRID FOUR WIDE LEAVE THREE HOLES, and a hole at the end of
 * a grid of boxes reads as a card that failed to load. It is not: there is no
 * tenth chart. So the holes are filled with something that says "nothing goes
 * here" - a faint dashed cell, no title, no border of a card, nothing to
 * click and nothing for a screen reader to read.
 *
 * ONLY WHERE THERE IS A ROW TO FINISH. On a narrow window the grid is one
 * column wide, every row is full by definition, and a placeholder under the
 * last chart would be an empty box for no reason. One column, none; two or
 * more, as many as the row is short.
 *
 * THE COLUMN COUNT IS MEASURED OFF THE CARDS THEMSELVES - how many of them
 * share the top edge of the first row - and not read out of
 * `grid-template-columns`. The grid is `auto-fit`, which COLLAPSES the tracks
 * it does not need, so the resolved value counts tracks that are not there:
 * on a wide screen with one chart it reads four columns, and the row would be
 * "finished" with three ghosts under a card that is already full width. What
 * the reader sees is what the cards are doing, so that is what is counted.
 */
function fillLastRow() {
  if (!grid) return;
  Array.from(grid.querySelectorAll(".chart-placeholder")).forEach((n) => n.remove());
  const cards = grid.querySelectorAll(".chart-card").length;
  if (!cards) return;
  const columns = gridColumns();
  if (columns < 2) return;
  const short = (columns - (cards % columns)) % columns;
  for (let i = 0; i < short; i += 1) {
    const cell = document.createElement("div");
    cell.className = "chart-placeholder";
    // Nothing here is content: it is the shape of a row being finished.
    cell.setAttribute("aria-hidden", "true");
    grid.appendChild(cell);
  }
}

function gridColumns() {
  const cards = Array.from(grid.querySelectorAll(".chart-card"));
  if (!cards.length) return 1;
  const top = cards[0].offsetTop;
  let n = 0;
  for (const node of cards) {
    if (node.offsetTop !== top) break;
    n += 1;
  }
  return Math.max(1, n);
}

/* The window changes width, the grid re-flows, and a row that was full is
 * suddenly short by two. Cheap enough to do on every resize - it counts the
 * cards it already has and adds or removes empty divs - but debounced all the
 * same, because a drag over the edge of the screen fires it a hundred times. */
let rowTimer = 0;
window.addEventListener("resize", () => {
  window.clearTimeout(rowTimer);
  rowTimer = window.setTimeout(fillLastRow, 120);
});

/* ── Loading and drawing ───────────────────────────────────────────────── */

/* Everything in the band, taken down. Called before the band is filled and
 * by every state that has no charts to put in it - a band left standing over
 * "nothing in this period" would be an answer to a question that has just
 * been withdrawn. The title stays: it is the band's accessible name. */
function clearBand() {
  if (!band) return;
  Array.from(band.querySelectorAll(".chart-card")).forEach((node) => node.remove());
  Array.from(band.querySelectorAll(".summary-panel")).forEach((node) => node.remove());
  syncSummary();
}

/* THE RULE AND THE HEADING FOLLOW THE CONTENT, and there is exactly one place
 * that decides it: the section is shown when the band holds something or the
 * map is up, and hidden otherwise. Two callers hiding the band and the map
 * separately is how a page ends up with a heading, a rule, and a blank
 * half-screen under it. */
function syncSummary() {
  if (!summaryBox) return;
  /* THE SUMMARY IS THE LIST, AND NOTHING ELSE.
   *
   * It held the map and the summary charts as well, and they have moved up
   * into the grid: a map is one of the pictures and belongs where the reader
   * meets it, and a chart is a chart wherever it stands. What is left down
   * here is the one thing that is not a picture - the rows themselves - so
   * the section is shown when there are rows and hidden when there are not.
   *
   * The map card is parked in this section while it is not in the grid
   * (parkMap), which is why the test is for the band's contents rather than
   * for the section having children. */
  const parked = mapCard && mapCard.parentElement === summaryBox;
  summaryBox.hidden = !(band && band.children.length);
  if (parked && summaryBox.hidden) mapCard.hidden = true;
}

function card(chart) {
  const template = document.getElementById("chart-card-template");
  const node = template.content.firstElementChild.cloneNode(true);
  node.dataset.chart = chart.id;
  if (chart.band) node.classList.add("chart-card--band");
  if (chart.width) node.classList.add(`chart-card--${chart.width}`);
  // THE FULL TITLE IS ALWAYS AVAILABLE. The visible one runs to two lines
  // and is cut past that (css/diagrams.css); the enlarge dialog prints it
  // whole as well.
  const heading = node.querySelector(".card-title");
  heading.textContent = chart.title;
  heading.title = chart.title;
  // NOT HIDDEN WHEN EMPTY. The line is two lines high whatever it holds, so
  // that the picture below it starts at the same height on every card; a
  // card that hid it would pull its own chart 44 px up and be the odd one
  // out - which is exactly what this line prevents.
  const purpose = node.querySelector(".chart-purpose");
  purpose.textContent = chart.description || "";
  if (chart.description) purpose.title = chart.description;
  return node;
}

let loading = false;
let lastWindow = null;
/* What the last answer said the term resolved to, for the sentence that
 * names the subject when a period holds nothing. */
let lastResolved = null;
/* The tab the cards ON SCREEN were drawn for. It is not always the selected
 * one: a tab switch that fails leaves the previous tab's charts standing
 * (they are still true), and the reader has to be told which is which. */
let shownTab = null;

/* What the page is doing, in a sentence, above the grid.
 *
 * The old charts stay on screen while the new ones are fetched (NN/g on
 * progress indicators) - which is right, but on its own it means the tab
 * strip already says "Ratings" while the cards still show Sources. The dim
 * is a hint only somebody who notices it can read; this line says it. It is
 * a live region, so it is also the announcement. */
function setStatus(text, opts = {}) {
  if (!statusLine) return;
  statusLine.textContent = text || "";
  statusLine.hidden = !text;
  statusLine.classList.toggle("is-error", Boolean(text) && Boolean(opts.error));
  // A failure that leaves the previous charts standing has to offer the way
  // back, and it belongs beside the sentence that reports it - not in place
  // of the cards.
  if (text && opts.retry) {
    const again = el("button", "button button--secondary", "Try again");
    again.type = "button";
    again.addEventListener("click", opts.retry);
    statusLine.appendChild(again);
  }
  // A way out of the state the sentence describes, beside the sentence:
  // the period that holds the data this one does not.
  if (text && opts.action) {
    const go = el("button", "button button--secondary", opts.action.label);
    go.type = "button";
    go.dataset.widen = "";
    go.addEventListener("click", opts.action.run);
    statusLine.appendChild(go);
  }
}

/* AN EMPTY SEARCH IS NOT AN EMPTY PERIOD.
 *
 * A scope page with no term charts nothing (app/scope.py: an empty term on a
 * scoped kind resolves to nothing rather than to everything, so a page
 * cannot accidentally chart the whole archive - that is what the summary is
 * for). The page must not say three different things about that state at
 * once - the field promising "Every entity", the banner asking for a search,
 * and six cards plus the status line blaming the PERIOD ("No rows for Aug 31
 * - Sep 6 - try a longer period"). There is one answer, in the place where
 * the charts would be. */
function nothingSearchedState() {
  const box = el("div", "notice chart-grid-empty");
  const body = el("div", "notice-body");
  body.appendChild(el("p", "notice-what", "Nothing is charted yet."));
  body.appendChild(el("p", "notice-hint",
    `Search for ${subject()} above; the charts are drawn for what you pick.`));
  box.appendChild(body);
  return box;
}

function noMatchState(q) {
  const box = el("div", "notice notice--warn chart-grid-empty");
  const body = el("div", "notice-body");
  body.appendChild(el("p", "notice-what", nothingCalledThat(q)));
  body.appendChild(el("p", "notice-hint", axis === "type"
    // The way out of a type search that found nothing is usually the other
    // axis, so the sentence points at the switch by its own words.
    ? `Check the spelling, pick ${anWord(axisWord())} from the list as you type, `
      + "or switch to Object to search for one thing by name."
    : "Check the spelling, or pick a name from the list as you type."));
  box.appendChild(body);
  return box;
}

/* NOTHING IN THIS PERIOD, AND SOMETHING OUTSIDE IT.
 *
 * "Nothing in the last 7 days - the newest for Apple Inc. is four months
 * old", with the control that goes there. The date and the period both come from
 * the server (`outside`), which found them on the tab's own tables with the
 * window taken out and nothing else changed - so the period it offers is a
 * period that really does hold rows.
 */
function newestWords(iso) {
  const when = new Date(iso);
  if (Number.isNaN(when.getTime())) return "";
  return when.toLocaleString(undefined, DAY_PARTS);
}

/* Move to the period the server said holds rows. Used by the status line's
 * button and by the card in the grid, so the two cannot drift apart. */
function widenTo(widen) {
  const frame = frameOf(activeTab);
  frame.timeframe = widen.timeframe;
  frame.page = widen.page || 0;
  if (timeframeSelect) timeframeSelect.value = frame.timeframe;
  syncUrl();
  load();
}

/* The same sentence as a card, where the charts would be. It repeats the
 * status line on purpose: that line is the live region a screen reader
 * hears, and this is what a reader sees. The button is the one the status
 * line offers, built once here and used by both. */
function nothingInThisPeriodState(outside, window_) {
  const box = el("div", "notice chart-grid-empty");
  const body = el("div", "notice-body");
  const subject = state.q ? `“${(lastResolved && lastResolved.label) || state.q}”` : "this project";
  const when = window_ && window_.page === 0
    ? `the ${String(window_.label || "").toLowerCase()}`
    : periodRange(window_);
  body.appendChild(el("p", "notice-what", `Nothing in ${when}.`));
  body.appendChild(el("p", "notice-hint",
    `The search worked - the newest row for ${subject} is `
    + `${newestWords(outside.newest)}. This period has none.`));
  if (outside.widen) {
    const go = el("button", "button button--secondary", `Show ${outside.widen.label.toLowerCase()}`);
    go.type = "button";
    go.addEventListener("click", () => widenTo(outside.widen));
    body.appendChild(go);
  } else {
    body.appendChild(el("p", "notice-hint", "Step back with Earlier to reach it."));
  }
  box.appendChild(body);
  return box;
}

function emptyButNotElsewhere(outside, window_) {
  const subject = state.q ? `“${(lastResolved && lastResolved.label) || state.q}”` : "this project";
  // "the last 7 days" on the newest window, the dates on an older one -
  // "last 7 days" said of a window three steps back would be false.
  const when = window_ && window_.page === 0
    ? `the ${String(window_.label || "").toLowerCase()}`
    : periodRange(window_);
  const said = `Nothing in ${when}. The newest for ${subject} `
    + `is ${newestWords(outside.newest)}.`;
  const widen = outside.widen;
  if (!widen) {
    setStatus(`${said} Step back with Earlier to reach it.`);
    return;
  }
  setStatus(`${said} Show ${widen.label.toLowerCase()} instead?`, {
    action: {
      // The word says which period it goes to, not "Widen": a button whose
      // label is a verb makes the reader press it to find out what it did.
      label: `Show ${widen.label.toLowerCase()}`,
      run: () => widenTo(widen),
    },
  });
}

/* ── What has already been asked ──────────────────────────────────────────
 *
 * Eight tabs, and a reader compares them: Sources, then Entities, then back
 * to Sources. Without a memory every one of those is a fresh walk over the
 * archive for an answer that has not changed - on a real project a tab is
 * seconds, and going back is the same seconds again.
 *
 * So an answer is kept under what it was an answer TO: the scope, the tab,
 * the term, the axis, the timeframe and the page. Anything that changes the
 * question changes the key, so a stale answer cannot be shown under a new
 * one; nothing has to be invalidated by hand.
 *
 * SMALL, AND OLDEST OUT FIRST. Eight tabs times a few periods is what a
 * reader moves through in one sitting; past that the early ones are cold and
 * holding their charts costs memory on a machine that may not have much.
 */
const ANSWERS = new Map();
const ANSWER_LIMIT = 24;

function answerKey(frame) {
  return JSON.stringify([scope, activeTab, state.q || "", axis,
                         frame.timeframe, frame.page]);
}

function remember(key, data) {
  ANSWERS.delete(key);
  ANSWERS.set(key, data);
  while (ANSWERS.size > ANSWER_LIMIT) ANSWERS.delete(ANSWERS.keys().next().value);
}

/* A search asks the archive again, whatever is remembered: the reader
 * pressed the button, and "nothing happened because I already knew" is not
 * an answer to a press. */
function forgetAnswers() {
  ANSWERS.clear();
  mapShowing = "";
}


/* ── The map under the charts ─────────────────────────────────────────────
 *
 * Two of the eight tabs count things that are somewhere: Locations is
 * addresses, Connections is pairs of entities that have them. On those two a
 * map is drawn UNDER the grid, of the same search and the same period the
 * bars above it are drawn from - one more question to the archive
 * (/api/diagrams/{scope}/map), asked once per tab and kept in the same
 * cache the charts are kept in, so moving between tabs never asks twice.
 *
 * ASKED AFTER THE CHARTS, NOT WITH THEM. The bars are what the reader came
 * for and they must not wait behind a walk over every address in the scope;
 * the map fills in a moment later, under the fold, and a map that fails
 * leaves the charts standing.
 *
 * THE PERIOD MEANS SOMETHING SLIGHTLY DIFFERENT ON EACH, and the caption
 * says which (app/charts/maps.py): on Locations it is the whole question,
 * on Connections it is the question about the LINES - the addresses are the
 * standing ones, because an office does not stop existing in a week nobody
 * wrote about it.
 */
const MAP_TABS = { connections: "connections", locations: "locations" };
const mapCard = document.getElementById("diagrams-map-card");
const mapBox = document.getElementById("diagrams-map");
const mapTitle = document.getElementById("diagrams-map-title");
const mapMeta = document.getElementById("diagrams-map-meta");
const mapCaption = document.getElementById("diagrams-map-caption");
const mapOpen = document.getElementById("diagrams-map-open");
let miniMap = null;
/* Which answer is on the map right now. A tab switched twice while the
 * first request is still out would otherwise draw the older answer over the
 * newer one; the key is the same one the charts are remembered under. */
let mapShowing = "";
/* Whether that answer put anything on the screen. A map that found nothing to
 * draw is remembered under the same key as one that drew - both are answered
 * and neither is asked for again - and only one of them has a card to put
 * back when the grid is rebuilt under it. */
let mapShown = false;

/* WHERE THE MAP LIVES WHEN IT IS NOT IN THE GRID.
 *
 * The card is one of the pictures now, not a summary, so it is moved INTO
 * the grid at the place its tab gives it (`map_after`). The grid is emptied
 * on every load, which would take the card with it - so it is parked back in
 * the section below before anything clears the grid, and moved in again on
 * the next draw. One element, moved; never rebuilt, because rebuilding a
 * Leaflet map re-fetches every tile. */
function parkMap() {
  if (mapCard && summaryBox && mapCard.parentElement !== summaryBox) {
    summaryBox.appendChild(mapCard);
  }
}

/* THE CARD THE MAP FOLLOWS, held from the draw that laid the grid out.
 *
 * The map is fetched after the charts, and the card is parked out of the grid
 * while that request is out (a half-drawn map of the last tab under this
 * tab's charts is worse than no map). So the answer arrives to find the card
 * somewhere else, and "put it back" has to mean the place it had - appending
 * it would have put the map after the matrix, which is not where the reader
 * met it and not what `map_after` says. */
let mapAnchor = null;

function placeMap(into) {
  if (!mapCard) return;
  if (into) mapAnchor = into;
  const after = mapAnchor;
  if (!after || !after.parentElement) return;
  mapCard.classList.add("chart-card--full");
  after.parentElement.insertBefore(mapCard, after.nextSibling);
}

function hideMap() {
  if (mapCard) mapCard.hidden = true;
  mapShown = false;
  parkMap();
  mapShowing = "";
  syncSummary();
}

/* Leaflet is built once, on the first tab that has a map, and kept: a map
 * torn down and rebuilt on every tab switch re-fetches its tiles. */
function ensureMap() {
  if (!miniMap && mapBox) {
    // "always": every map in the product zooms on the wheel with the pointer
    // over it (minimap.js says why there is no second rule).
    miniMap = createMiniMap(mapBox, { wheel: "always" });
  }
  return miniMap;
}

function placesWord(n) {
  return n === 1 ? "1 place" : `${n} places`;
}

function mapCaptionText(tab, data, window_) {
  const period = periodWords(window_);
  const shown = data.shown || 0;
  if (tab === "locations") {
    if (!shown) {
      const rows = data.unplaced || 0;
      if (!rows) return `No address in ${period} has a point on the world.`;
      return `${rowsWord(rows)} in ${period}, and the archive has a point on the `
        + "world for none of them: a document has to write an address out "
        + "before a place can be drawn from it.";
    }
    const of = data.capped ? ` of ${data.total}` : "";
    // THE SENTENCE FOLLOWS THE PICTURE. Not "a bigger dot is more rows",
    // which is true of dots and says nothing about a heat field - where what
    // a reader is reading is the COLOUR, and where two addresses a street
    // apart are one warm patch rather than two dots.
    return `${placesWord(shown)}${of} in ${period}. `
      + "The warmer the colour, the more rows stand at or near that point.";
  }
  if (!shown) return `No connection in ${period} joins two entities.`;
  // A LINE NEEDS AN ADDRESS AT BOTH ENDS, and on a real archive most pairs
  // have one at neither. Saying "0 pairs drawn … 8 more have no address" put
  // a "more" after a nothing; the two cases are different sentences.
  const drawn = data.drawn || 0;
  // Every pair in the period, whether or not the archive can place it.
  const total = data.total || shown;
  if (!drawn) {
    const pairs = total || data.unplaced || 0;
    return `None of the ${pairs} ${pairs === 1 ? "pair" : "pairs"} in ${period} `
      + "can be drawn: the archive has no address for one of the two ends.";
  }
  /* WHAT IS ON THE MAP, AND NOT A FRACTION.
   *
   * It read "74 of 331 drawn", which is two numbers about two different
   * things: the pairs that could be placed, and every pair in the period
   * whether or not the archive knows where either end of it is. A reader
   * counting lines on the picture can only ever find the first, so the
   * second made the map look like a sample of itself.
   *
   * The line says what was drawn. What could not be is a second sentence,
   * which is where it belongs: it is a fact about the ADDRESSES, not about
   * the map. */
  // Against every pair in the period, not against the ones that came back:
  // the answer now carries only pairs it can place (charts/maps.py), so the
  // ones with no address are the difference from the total.
  const missing = Math.max(0, total - drawn);
  const unplaced = missing > 0
    ? ` ${missing} more ${missing === 1 ? "connection has" : "connections have"} `
      + "no address on one end."
    : "";
  return `${drawn} ${drawn === 1 ? "connection" : "connections"} drawn, from the `
    + `connections in ${period}; each entity sits at the address it carries `
    + `most often, whenever that was written.${unplaced}`;
}

/* The card with its map taken out of it: a title, and one sentence saying
 * that the rows are there and their whereabouts are not. */
function sayNoPlaces(tab, data) {
  if (!mapCard) return;
  mapShown = true;
  placeMap();
  parkMapNotice(true);
  mapTitle.textContent = tab === "locations"
    ? "Where these places cluster" : "Where these connections run";
  mapMeta.textContent = "";
  mapCaption.textContent = mapCaptionText(tab, data, data.window);
  mapCard.hidden = false;
  syncSummary();
}

function parkMapNotice(on) {
  mapCard.classList.toggle("diagrams-map-card--unplaced", Boolean(on));
  if (mapOpen) mapOpen.hidden = Boolean(on);
}

function drawMap(tab, data) {
  if (!mapCard || !mapBox) return;
  mapShown = true;
  placeMap();
  parkMapNotice(false);
  mapTitle.textContent = tab === "locations"
    ? "Where these places cluster" : "Where these connections run";
  mapMeta.textContent = tab === "locations"
    ? placesWord(data.shown || 0)
    : `${data.drawn || 0} ${(data.drawn || 0) === 1 ? "connection" : "connections"} drawn`;
  if (mapOpen) {
    // THE VIEW THIS PICTURE IS A SMALL COPY OF, and not always the same one:
    // Connections is the Map view's pins and lines, Locations is the Heatmap's
    // field. Sending a reader who has just looked at heat to a page of pins
    // would be sending them to a different question.
    const heat = tab === "locations";
    const url = new URL(heat ? "/heatmap" : "/map", window.location.origin);
    if (data.q) {
      // The Heatmap's entity box is `entity`; the Map's subject is `q`.
      url.searchParams.set(heat ? "entity" : "q", data.q);
      if (data.axis) url.searchParams.set(heat ? "entity_axis" : "axis", data.axis);
    }
    if (!heat) url.searchParams.set("mode", "connections");
    if (state.project) url.searchParams.set("project", state.project);
    if (state.language) url.searchParams.set("language", state.language);
    mapOpen.href = url.pathname + url.search;
    mapOpen.textContent = heat ? "Open the heatmap" : "Open the map view";
  }
  mapCaption.textContent = mapCaptionText(tab, data, data.window);
  // THE CARD IS UNHIDDEN BEFORE LEAFLET IS EVER TOLD ABOUT THE BOX.
  //
  // A map built inside a display:none container believes it is 0 x 0, and
  // invalidateSize() afterwards is not the whole cure: the tiles come back
  // but every circle is placed against the origin the 0 x 0 map had, so the
  // picture is a correct world with all of its dots painted off to the left
  // of it. Unhide, THEN build, THEN draw.
  mapCard.hidden = false;
  syncSummary();
  const map = ensureMap();
  if (!map) return;
  // And still invalidate: on the second and later tabs the map already
  // exists, and the card it lives in has been display:none since the last
  // one (minimap.js says what a stale size looks like).
  map.invalidate();
  /* TWO TABS, TWO PICTURES, EACH THE ONE ITS OWN VIEW DRAWS.
   *
   * Connections gets the Map view's pins and lines - the same shape, the same
   * popup, the same click - because a reader who has just come from that view
   * is looking at the same pairs and must not have to learn a second drawing.
   * Locations gets the Heatmap view's field: a tab about WHERE THERE IS MOST
   * of something is the question heat answers, and forty pins on one town is
   * a blue wall that answers nothing.
   *
   * NO LEGEND ON EITHER. The colours here are the connection groups', which
   * the Connections view names in full one click away; a legend under a
   * 22 rem picture is a second table of contents for a picture that has none. */
  if (tab === "connections") {
    map.setHeat([]);
    map.setLines(data.lines || []);
    map.setPoints(data.places || [], { pins: true });
  } else {
    map.setLines([]);
    map.setPoints([]);
    map.setHeat(data.places || []);
  }
  map.fit();
}

/* The map for whatever is on screen now, from the cache when it is there.
 * Called after the charts have been drawn, and never for a tab that has no
 * map or a search that found nothing. */
async function loadMap() {
  const tab = MAP_TABS[activeTab];
  if (!tab || !mapCard) { hideMap(); return; }
  const frame = frameOf(activeTab);
  const key = "map:" + answerKey(frame);
  if (mapShowing === key) {
    /* THE ANSWER ON THE CARD IS ALREADY THE RIGHT ONE - but the grid it was
     * standing in has been rebuilt since (load() empties the grid and parks
     * the card first), so the card has to be put back in its place. It is
     * not redrawn: rebuilding a Leaflet map re-fetches every tile, and this
     * early return is what keeps a tab switch from doing that.
     *
     * Going away to Entities and coming back left the map card parked in the
     * summary section, which is display:none on a tab with no list - so the
     * Locations tab came back with no map at all and nothing to say why. */
    if (mapShown) { placeMap(); mapCard.hidden = false; }
    return;
  }
  const known = ANSWERS.get(key);
  if (!known) hideMap();
  const params = { tab, q: state.q, axis, timeframe: frame.timeframe,
                   page: String(frame.page) };
  try {
    const data = known
      || await api(`/api/diagrams/${scope}/map`, { channel: "diagrams-map", params });
    if (!known) remember(key, data);
    // The tab may have moved on while this was out.
    if (("map:" + answerKey(frameOf(activeTab))) !== key) return;
    if (!(data.places || []).length && !(data.lines || []).some((c) => c.drawable)) {
      /* NOTHING TO PUT ON A MAP IS NOT A MAP - but it is not nothing either,
       * and which of the two it is depends on whether the archive HAS rows.
       *
       *   no rows at all      the tab found nothing; the charts above say so
       *                       and a grey square would repeat it. No card.
       *   rows, no addresses  the reader is on the one tab that promises a
       *                       map and there is none, which reads as a page
       *                       that failed. So the card stays, WITHOUT the
       *                       empty 26 rem square, and says why: the rows
       *                       are there, the archive has no point on the
       *                       world for them.
       *
       * That second case is what a real archive mostly looks like: an
       * address is a fact a document has to state, and most do not. */
      mapShowing = key;
      if (data.unplaced) sayNoPlaces(tab, data); else hideMap();
      return;
    }
    mapShowing = key;
    drawMap(tab, data);
  } catch (err) {
    if (isAbort(err)) return;
    // A MAP THAT DID NOT COME IS NOT AN ERROR ON THE PAGE. The charts above
    // it are the answer; the map is the illustration. It says so where it
    // would have been and takes no room anywhere else.
    if (!mapCard) return;
    mapShowing = "";
    mapTitle.textContent = "Where these rows are";
    mapMeta.textContent = "";
    mapCaption.textContent = ["Could not draw the map.",
                              sentence(err && err.message)].filter(Boolean).join(" ");
    mapCard.hidden = false;
    // Leaflet is taken down BEFORE the box it lives in is emptied: a map
    // whose panes have been removed from under it throws on the way out
    // (minimap.js: destroy says what that looks like).
    if (miniMap) { miniMap.destroy(); miniMap = null; }
    if (mapBox) mapBox.textContent = "";
  }
}

async function load({ fresh = false } = {}) {
  if (!grid) return;
  const frame = frameOf(activeTab);
  const key = answerKey(frame);
  if (fresh) { ANSWERS.delete(key); ANSWERS.delete("map:" + key); mapShowing = ""; }
  const known = ANSWERS.get(key);
  loading = true;
  grid.setAttribute("aria-busy", "true");
  grid.classList.add("is-loading");
  // Only for a change of tab or period: the first load has the server's own
  // "Loading the charts…" inside the empty grid and does not need two.
  if (grid.querySelector(".chart-card")) setStatus(`Loading ${tabLabel(activeTab)}…`);
  // The last result ("Sources: 6 of 6 charts have data…") is about the tab
  // that is being replaced; leaving it standing while another one loads
  // makes it the answer to the wrong question.
  announce("");
  const params = { q: state.q, axis, timeframe: frame.timeframe, page: String(frame.page) };
  try {
    // ASKED ONCE. A tab the reader has already been on is drawn from what
    // came back then; only a question nobody has asked reaches the archive.
    const data = known
      || await api(`/api/diagrams/${scope}/${activeTab}`, { channel: "diagrams", params });
    if (!known) remember(key, data);
    /* THE TAB MAY HAVE MOVED ON WHILE THIS WAS OUT - and not only to another
     * request on the same channel, which would have aborted this one. A tab
     * whose answer is remembered is drawn at once, with no request at all,
     * so nothing aborts the fetch of the tab the reader left: it would come
     * back after the remembered one is on screen and draw itself over it -
     * Entities' three cards under a Locations strip, and the map card parked
     * out of the grid with nothing to say why. The answer is kept (it was
     * remembered above); only the drawing is skipped. */
    if (answerKey(frameOf(activeTab)) !== key) return;
    // The old charts stay on screen until the new ones are here; only then
    // is the grid replaced.
    rendered.forEach((r) => r.destroy());
    rendered = [];
    parkMap();
    // The card the map follows is one of the cards about to be thrown away.
    mapAnchor = null;
    grid.textContent = "";
    clearBand();
    grid.classList.remove("is-loading");
    grid.setAttribute("aria-busy", "false");
    lastWindow = data.window;
    lastResolved = data.resolved || null;
    updateToolbar(data.window);
    showResolved(data);
    setStatus("");   // whatever it said while loading; the result speaks below
    // A term that matched nothing has no charts to draw: eight empty cards,
    // each advising a longer timeframe, would send the reader through every
    // period of an archive that has never heard of the word. One answer,
    // and it is the true one.
    if (data.q && data.resolved && data.resolved.match === "none") {
      hideMap();
      grid.appendChild(noMatchState(data.q));
      announce(`Nothing in this project is called ${data.q}`);
      return;
    }
    if (!data.q && scope !== "summary") {
      hideMap();
      grid.appendChild(nothingSearchedState());
      announce(`Nothing is charted yet. Search for ${subject()} above.`);
      return;
    }
    shownTab = activeTab;
    // The band first, and only where something was counted: a band of empty
    // cards above a grid of full ones reads as a broken summary rather than
    // as an honest "nothing extreme here". The chart itself is still in the
    // answer, in the grid's own order, so nothing is lost from the export.
    const inBand = data.charts.filter((c) => c.band && c.total);
    /* WHERE A CARD GOES, when "one cell of the grid" is the wrong answer.
     *
     * The grid is `auto-fit` with a 30rem minimum, so on a wide screen it is
     * three or four cards across. That is right for a bar chart and wrong
     * for two things: a matrix at a third of the width draws its axis names
     * over each other, and the chart that IS the tab's subject reads as one
     * card among six.
     *
     * So a chart may ask for the whole row ("full") or for half of it
     * ("half"). A half is a PAIR: two consecutive halves go into one row of
     * their own, which spans the grid and splits itself down the middle - so
     * each gets just under half whatever the window does, and on a narrow
     * screen they stack like everything else. */
    /* THE GRID IS TWO COLUMNS, AND EVERY CARD SAYS WHICH IT WANTS.
     *
     * "full" takes the row; anything else takes one cell, which is half the
     * page. The MAP is one of the pictures now rather than a summary, and it
     * goes where the reader meets it: after the chart `map_after` names.
     *
     * A card left alone at the end of the grid is stretched across it: half
     * a row of chart and half a row of nothing is not a layout, and there is
     * no second card coming to fill it. */
    let column = 0;
    const placed = [];
    data.charts.forEach((chart) => {
      const node = card(chart);
      {
        grid.appendChild(node);
        placed.push({ node, full: chart.width === "full" });
        column = chart.width === "full" ? 0 : (column + 1) % 2;
        if (data.map_after && chart.id === data.map_after) {
          // The map comes next in the reading order, across the page - and
          // the card it follows is remembered, because the map itself is
          // still being fetched and will come back after this loop is over.
          placeMap(node);
          column = 0;
        }
      }
      rendered.push(renderChart(node, chart, {
        // A chart may be sized from its data rather than from the card
        // (`grow` on the ChartSpec, read in static/js/charts.js): a chart of
        // forty names needs forty rows of label, and a card of a fixed
        // height would make the rule drop every one of them.
        band: false,
        onPick: (dataset, point) => openFor(chart, dataset, point),
        // What the enlarged copy of this chart is of - the same three facts
        // the drilldown's subtitle carries, so the two dialogs stacked on
        // each other agree about what is being looked at.
        subtitle: [chart.total ? rowsWord(chart.total) : "",
                   state.q ? `“${state.q}”` : "",
                   periodWords(data.window)].filter(Boolean).join(" - "),
        // WHAT A SAVED PICTURE IS OF. The caption drawn under the chart in
        // the PNG (static/js/charts.js): a bar chart with no subject on it
        // is unusable a week after it was pasted somewhere.
        context: { project: data.project || state.project,
                   language: data.language || state.language,
                   period: periodWords(data.window), term: data.q || "" },
      }));
    });
    /* NOTHING IS LEFT HALF A ROW WIDE. A card with no neighbour coming after
     * it is stretched across the grid: the alternative is a chart in one
     * column and a hole in the other, which reads as a card that failed to
     * load rather than as the end of the list. */
    if (column === 1 && placed.length) {
      const last = placed[placed.length - 1];
      if (!last.full) last.node.classList.add("chart-card--full");
    }

    // THE LIST AT THE FOOT, where this tab has one. After the charts, so the
    // band is filled in the order the reader meets it, and before the summary
    // section is shown - a heading over nothing is what syncSummary prevents.
    // EVEN WHEN IT IS EMPTY. A tab whose filter matched nothing still ends in
    // the sentence that says so - a summary that quietly disappears reads as a
    // page that did not finish loading.
    if (band && data.summary) band.appendChild(summaryPanel(data.summary));
    syncSummary();
    fillLastRow();
    const filled = data.charts.filter((c) => c.total).length;
    const period = periodCaption(data.window);
    if (!filled) {
      // Nothing was counted, so there is nothing to place: the map would be
      // an empty grey square under a card that has just explained why.
      hideMap();
      // NOT once per card. Eight cards each advising a longer period is the
      // same answer eight times, and the answer belongs where the reader is
      // already looking - above the grid, before the scrolling starts. The
      // cards say "No rows." and collapse (static/js/charts.js).
      // The line is a live region of its own, so writing it here is also
      // what a screen reader hears; announcing it again would say it twice.
      //
      // AND WHERE THE DATA IS ONLY OUTSIDE THE WINDOW, THE LINE SAYS SO AND
      // OFFERS IT. "No rows" for a subject with seven thousand of them
      // reads as a broken search, and the period is the one thing between
      // the reader and the answer.
      if (data.outside && data.outside.newest) {
        emptyButNotElsewhere(data.outside, data.window);
        // AND IN FRONT OF THE READER, NOT ONLY IN THE STATUS LINE.
        //
        // The line above the grid alone is read past: eight empty cards are
        // what the eye lands on, so a search that finds 810 entities and
        // draws nothing looks like a search that does not work - a field
        // that offers suggestions and then charts nothing reads as a broken
        // field, not as an empty period.
        //
        // It happens on a whole archive, not in a corner: an archive that
        // stops writing locations, entities, ratings, attributes,
        // connections and market insights months before it stops writing
        // sources and events has, on a seven-day window, six of the eight
        // scopes empty for every term anybody types.
        parkMap();
    grid.textContent = "";
        clearBand();
        grid.appendChild(nothingInThisPeriodState(data.outside, data.window));
      } else {
        setStatus(`No rows for ${period}. Try a longer period, or step back to an earlier one.`);
      }
    } else {
      setStatus("");
      announce(`${data.tab_title}: ${filled} of ${data.charts.length} charts have data for ${period}`);
      // The picture under the bars, on the two tabs that have one. Not
      // awaited: the charts are drawn and the reader can read them while
      // the addresses are being looked up.
      loadMap();
    }
  } catch (err) {
    if (isAbort(err)) return;
    hideMap();
    grid.classList.remove("is-loading");
    grid.setAttribute("aria-busy", "false");
    // The caption keeps saying which period was being asked for: a page
    // that fails AND stops naming the period leaves nothing to try again
    // with. And "try again" is a button here, not an instruction to reload.
    updateToolbar(lastWindow);

    // A FAILED REQUEST IS NOT A REASON TO TAKE THE ANSWER AWAY.
    //
    // Loading keeps the six cards that are on screen and only dims them;
    // failing must not wipe them and leave a notice alone on the page, or a
    // reader who has just read a number loses it to a network hiccup. The
    // cards stay, the failure is said in the live region above them - with
    // the Try again button beside it - and the sentence names the tab they
    // belong to, because the tab strip has already moved on.
    const standing = grid.querySelector(".chart-card");
    const what = `Could not load ${tabLabel(activeTab)}.`;
    if (standing) {
      const kept = shownTab && shownTab !== activeTab
        ? ` The charts below are still ${tabLabel(shownTab)}.` : "";
      const said = [what, sentence(err && err.message), sentence(err && err.hint)]
        .filter(Boolean).join(" ") + kept;
      // The status line is a live region (role=status, diagrams.html), so
      // writing the sentence there IS the announcement - announce() as well
      // would say it twice.
      setStatus(said, { error: true, retry: () => load({ fresh: true }) });
    } else {
      parkMap();
    grid.textContent = "";
      clearBand();
      setStatus("");
      const notice = errorNotice(what, err);
      const again = el("button", "button button--secondary", "Try again");
      again.type = "button";
      again.addEventListener("click", () => load({ fresh: true }));
      notice.querySelector(".notice-body").appendChild(again);
      grid.appendChild(notice);
    }
  } finally {
    loading = false;
  }
}

/* Back/Forward changes the URL; the page follows it rather than arguing. */
onChange((next, changed) => {
  if (loading) return;
  if (changed.includes("tab") && next.tab && next.tab !== activeTab) {
    selectTab(next.tab);
  }
});

writeScopeToUrl();
// The switch was rendered pressed on the right half by the server; this
// makes the FIELD agree with it after a Back button, which restores the URL
// without re-rendering the page.
applyAxisToField();
applyTab();
load();
