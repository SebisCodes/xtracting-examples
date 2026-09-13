/* ==========================================================================
 *  The Log view.
 *
 *  Four tabs over one set of filters:
 *
 *    Problems  /api/logs/errors - what went wrong, newest first, one row
 *              per thing, with the sentence a person can act on and the
 *              technical half folded away underneath.
 *    Runs      /api/logs/runs   - the history. "Nothing went wrong" and
 *              "nothing ran at all" look the same in the first tab.
 *    Crawler   /api/logs/service - what each service DID, step by step,
 *    Collector while somebody had its switch on. Normally empty, and the
 *              panel says why rather than showing a bare empty list.
 *
 *  ONE TABLE, NOT A LADDER OF IFS. Everything that differs between the tabs
 *  is one entry in `VIEWS` below: which endpoint, which of the shared
 *  filters apply to it, what its rows are called, how one is drawn and what
 *  its empty state says. `load`, `countOthers`, `anyFilter` and `describe`
 *  read that table and know nothing else about any particular tab, so a
 *  fifth stream is one entry here and two elements in logs.html, not six
 *  `tab === "errors" ? … : …` branches that have to be found first.
 *
 *  EVERY FILTER LIVES IN THE URL (state.js), the page number included, so
 *  the view survives a reload and a link can be sent to a colleague:
 *  /logs?q=example.ch&range=week&page=2 opens on the same rows it was
 *  copied from. THE FIELDS ARE THE TRUTH: `filters()` reads the search box
 *  itself rather than a remembered copy, and every request writes what it
 *  used back into the URL - the controls on screen and the rows below them
 *  describe the same query at all times.
 *
 *  A value in the URL that the page does not know (a kind or a range that
 *  does not exist) is dropped with a sentence saying so, and the
 *  address is rewritten to what is actually shown. A log that answers a
 *  bookmark with a different query than the bookmark asked for, silently,
 *  is worse than one that says "there is no kind NOPE".
 *
 *  Nothing here polls. A log that refreshes under the reader's hands moves
 *  the row they were about to click, and a person who wants to know whether
 *  the last minute added anything presses Show.
 *
 *  BOTH NUMBERS ARE ON THE TAB STRIP BEFORE EITHER TAB IS OPENED. "Problems
 *  0" alone cannot say whether nothing went wrong or nothing ran at all -
 *  which is the one question the second tab exists to answer - so every load
 *  also asks the other endpoint for its total (one row, page_size=1).
 *
 *  A FAILED REQUEST IS REPORTED ONCE, in the panel where the rows would have
 *  been and where the way to try again is: the calls are made with the
 *  shared toast suppressed.
 * ========================================================================== */

import { api, isAbort, fmtDate, fmtInt, fmtAgo, watchSearch } from "./api.js";
import { state, set as setState, setExtra, getExtra, fillOnce } from "./state.js";
import { announce } from "./a11y.js";

const TABS = ["errors", "runs", "crawler", "collector"];
const RANGES = ["hour", "day", "week", "month", "all"];
const DEFAULT_RANGE = "day";

const form = document.getElementById("log-filters");
/* Every request on these channels turns the button quiet and writes
 * "Searching…" beside it, without this file having to remember to
 * (static/js/api.js: watchSearch). */
watchSearch(form, "log", ...TABS.map((name) => `log-count-${name}`));
const rangeField = document.getElementById("log-range");
const sourceField = document.getElementById("log-source");
const kindField = document.getElementById("log-kind");
const severityField = document.getElementById("log-severity");
const statusField = document.getElementById("log-status");
const searchField = document.getElementById("log-q");
const clearButton = document.getElementById("log-clear");
const noticeLine = document.getElementById("log-notice");

const errorList = document.getElementById("error-list");
const runRows = document.getElementById("run-rows");
const runTable = document.getElementById("run-table");
const runScroll = document.getElementById("runs-scroll");
const runScrollWrap = document.getElementById("runs-scroll-wrap");
const crawlerList = document.getElementById("crawler-list");
const collectorList = document.getElementById("collector-list");

/* Every panel names its parts the same way, so they are looked up rather
 * than listed: a fifth tab that follows the convention needs no line here.
 * The list container is the one exception - `error-list` and `run-rows`
 * predate the convention - and it lives in VIEWS with the rest of what is
 * particular to a tab. */
function partsOf(pattern) {
  const out = {};
  TABS.forEach((name) => { out[name] = document.getElementById(pattern(name)); });
  return out;
}

const panels = partsOf((name) => `panel-${name}`);
const titles = partsOf((name) => `${name}-title`);
const pagers = partsOf((name) => `${name}-pager`);
const pageInfos = partsOf((name) => `${name}-page-info`);
const metas = partsOf((name) => `${name}-count`);

/* One page number per tab. Switching from page 4 of the problems to the runs
 * has to start at their first page, not at their fourth. The URL carries the
 * page of the tab that is open. */
const pageOf = {};
TABS.forEach((name) => { pageOf[name] = 0; });
let tab = TABS.includes(state.tab) ? state.tab : "errors";

/* WHAT EACH TAB IS, IN ONE PLACE.
 *
 *   endpoint  where its rows come from.
 *   fixed     parameters that are part of the tab rather than of a filter.
 *   filters   which of the SHARED filters narrow this tab, in the order the
 *             header sentence names them. `range` and `q` apply to all four
 *             and are added by paramsFor(); a key that is not on this list
 *             is not sent - the Collector has no watched page, so sending
 *             one would empty the list for a reason nobody could see.
 *   noun      singular and plural, for "8 problems" and "1 step".
 *   list      the element the rows go in.
 *   empty     the state to show when the tab is empty and NOTHING is
 *             filtered - the sentence that has to explain the emptiness
 *             rather than leave the reader looking at nothing.
 *
 * The render and empty functions are declared below; a function declaration
 * is hoisted, so naming them here is safe and keeps the table readable. */
const VIEWS = {
  errors: {
    endpoint: "/api/logs/errors", fixed: {},
    filters: ["source_id", "kind", "severity"],
    noun: ["problem", "problems"], list: errorList,
    render: renderErrors, empty: emptyErrors,
  },
  runs: {
    endpoint: "/api/logs/runs", fixed: {},
    filters: ["source_id", "status"],
    noun: ["run", "runs"], list: runRows,
    render: renderRuns, empty: emptyRuns,
  },
  crawler: {
    endpoint: "/api/logs/service", fixed: { service: "crawler" },
    filters: ["source_id"],
    noun: ["step", "steps"], list: crawlerList,
    render: (data) => renderSteps("crawler", data),
    empty: (data) => emptySteps("crawler", data),
  },
  collector: {
    endpoint: "/api/logs/service", fixed: { service: "collector" },
    filters: [],
    noun: ["step", "steps"], list: collectorList,
    render: (data) => renderSteps("collector", data),
    empty: (data) => emptySteps("collector", data),
  },
};

/* What /api/logs/kinds says exists, so a value out of an old link can be
 * recognised as gone. Null until the lists are in: with no list to check
 * against, nothing is dropped and the API keeps the last word. */
const known = { kinds: null, severities: null, statuses: null };

/* fmtAgo() counts in hours up to two days, so a log written yesterday
 * afternoon reads "26 h ago" and the reader has to divide by 24 to know
 * which day that was. Here it rolls over at one day. The exact stamp is
 * printed next to it either way, so nothing is lost. */
function ago(iso) {
  if (!iso) return "";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "";
  const hours = (Date.now() - then) / 3600000;
  if (hours < 24) return fmtAgo(iso);
  const days = Math.round(hours / 24);
  if (days === 1) return "yesterday";
  if (days < 60) return `${days} days ago`;
  return fmtAgo(iso);   // months and years are already plain words
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = text;
  return node;
}

function withContext(href) {
  const url = new URL(href, window.location.origin);
  if (state.project) url.searchParams.set("project", state.project);
  if (state.language) url.searchParams.set("language", state.language);
  return url.pathname + url.search;
}

/* ── The line that says what had to be ignored ────────────────────────── */

const notices = [];

function notice(text) {
  if (text && !notices.includes(text)) notices.push(text);
  noticeLine.textContent = notices.join(" ");
  noticeLine.hidden = notices.length === 0;
}

function clearNotices() {
  notices.length = 0;
  notice("");
}

/* ── The filters ──────────────────────────────────────────────────────── */

/* The filters as they stand ON SCREEN. The search field is read here and
 * not from a copy kept somewhere else: changing a select while a term is
 * still in the box has to search for that term, not for the one that was
 * last submitted. */
function filters() {
  return {
    range: RANGES.includes(rangeField.value) ? rangeField.value : DEFAULT_RANGE,
    source_id: sourceField.value || "",
    kind: kindField.value || "",
    severity: severityField.value || "",
    status: statusField.value || "",
    q: searchField.value.trim(),
  };
}

/* The control each shared filter is read from and labelled by. `VIEWS[…]
 * .filters` names the keys; this says where each one lives, so the header
 * sentence and the request cannot disagree about which control they mean. */
const FIELDS = {
  source_id: sourceField, kind: kindField,
  severity: severityField, status: statusField,
};

/* What to ask the API for this tab: the two filters every tab has, plus the
 * ones that belong to it, plus whatever is part of the tab itself. Used for
 * the rows AND for the number on the other tabs, so a badge can never be
 * counted under different filters from the list it predicts. */
function paramsFor(name) {
  const view = VIEWS[name];
  const f = filters();
  const params = { ...view.fixed, range: f.range, q: f.q };
  view.filters.forEach((key) => { if (f[key]) params[key] = f[key]; });
  return params;
}

/* Is anything narrowing WHAT IS ON SCREEN? Only the filters that apply to
 * the open tab count: a kind chosen in the Problems tab does not narrow the
 * run history, so an empty run history is not "nothing matches these
 * filters", and saying so over a list it did not filter would be a lie.
 *
 * The EMPTY STATES ask this. The Clear button does not: it is always on
 * screen, beside Search, like the Clear on every other view (_macros.html:
 * search_actions), so nothing has to decide whether to show it. What is
 * filtered is said by the notice line, which is where that
 * belongs: a control that comes and goes cannot be learned. */
function anyFilter() {
  const f = filters();
  const own = VIEWS[tab].filters.some((key) => f[key]);
  return Boolean(own || f.q || f.range !== DEFAULT_RANGE);
}

/* The URL is written from the fields before every request, so the address
 * bar, the controls and the rows can never disagree. */
function rememberFilters() {
  const f = filters();
  ["range", "source_id", "kind", "severity", "status"].forEach((key) => {
    setExtra(key, f[key] && !(key === "range" && f[key] === DEFAULT_RANGE) ? f[key] : "");
  });
  setState({ q: f.q, page: pageOf[tab] });
  // ALWAYS THERE, like the Clear beside every other Search on the product
  // (_macros.html: search_actions). A Clear that appears only while
  // something is filtered becomes the sign that a filter is on - that job
  // belongs to the notice line below, which says WHAT is filtered; a
  // control that comes and goes is one the reader cannot learn.
}

function restoreFilters() {
  const range = getExtra("range");
  if (range && !RANGES.includes(range)) {
    // The API answers 400 on an unknown range on purpose. Undoing that
    // quietly here would hand the reader a day's worth of rows under a URL
    // that says "year", and they would believe the URL.
    notice(`There is no time range “${range}” - showing the last 24 hours.`);
    setExtra("range", "");
  }
  rangeField.value = RANGES.includes(range) ? range : DEFAULT_RANGE;
  statusField.value = getExtra("status");
  fillOnce(searchField, "q");
  pageOf[tab] = state.page || 0;
}

function clearFilters() {
  rangeField.value = DEFAULT_RANGE;
  [sourceField, kindField, severityField, statusField].forEach((f) => { f.value = ""; });
  searchField.value = "";
  // By name, over the table: a tab whose page number was left standing would
  // come back on page 4 of a list that has just been widened to one page.
  TABS.forEach((name) => { pageOf[name] = 0; });
  clearNotices();
  rememberFilters();
  loadFilterLists();
  load();
  announce("Filters cleared");
}

/* ── The filter lists ─────────────────────────────────────────────────── */

function option(value, label, count) {
  const node = el("option", null, count ? `${label} (${fmtInt(count)})` : label);
  node.value = value;
  // The label without its count, for the sentence in the panel header:
  // "8 problems - Error (36)" would name two different numbers in one line.
  node.dataset.plain = label;
  return node;
}

/* Keep the current choice even when this range has no rows of that kind:
 * clearing the filter a person set, because it currently matches nothing,
 * is how a view starts lying about what it shows. A value the build does
 * not know at all is a different case - dropUnknown() has removed it before
 * this runs. */
function fillSelect(field, items, allLabel, chosen, unknownLabel) {
  const want = chosen !== undefined ? chosen : field.value;
  field.replaceChildren(option("", allLabel, 0));
  let found = false;
  items.forEach((item) => {
    field.appendChild(option(String(item.value), item.label, item.count));
    if (String(item.value) === String(want)) found = true;
  });
  if (want && !found) {
    field.appendChild(option(String(want), unknownLabel ? unknownLabel(want) : String(want), 0));
  }
  field.value = want || "";
}

/* A kind, severity or run status out of an older link. Dropped rather than
 * sent: the API rejects it with a 400 and the reader would be left with an
 * empty log and a filter offering something that does not exist. */
function dropUnknown(key, field, values, what) {
  const value = getExtra(key);
  if (!value || !values || values.includes(value)) return value;
  notice(`There is no ${what} “${value}” - showing everything.`);
  setExtra(key, "");
  field.value = "";
  return "";
}

async function loadFilterLists() {
  try {
    const data = await api("/api/logs/kinds", {
      channel: "log-kinds", params: { range: filters().range }, quiet: true,
    });
    known.kinds = (data.kinds || []).map((k) => String(k.value));
    known.severities = (data.severities || []).map((s) => String(s.value));
    known.statuses = (data.run_statuses || []).map((s) => String(s.value));

    const chosen = {
      source: getExtra("source_id"),
      kind: dropUnknown("kind", kindField, known.kinds, "kind"),
      severity: dropUnknown("severity", severityField, known.severities, "severity"),
      status: dropUnknown("status", statusField, known.statuses, "result"),
    };
    // The count next to a source is the number of the OPEN tab: "Flats in
    // Zurich (2)" beside a run history has to mean two runs, not two
    // problems that are not in this list. /api/logs/kinds counts both and
    // the list itself is the union of the two tables, so a page that ran
    // without trouble can be picked as well - a list built from the error
    // table alone lacks it in exactly the case this view is opened for
    // most often.
    fillSelect(sourceField, (data.sources || []).map((s) => ({
      value: s.id, label: s.name,
      count: tab === "runs" ? (s.runs || 0) : (s.problems !== undefined ? s.problems : s.count),
    })), "Every watched page", chosen.source, (id) => `Watched page ${id}`);
    fillSelect(kindField, data.kinds || [], "Everything", chosen.kind);
    fillSelect(severityField, data.severities || [], "Every severity", chosen.severity);
    fillSelect(statusField, (data.run_statuses || []).map((s) => ({
      value: s.value, label: s.label, count: s.count })), "Every result", chosen.status);
    } catch (err) {
    if (!isAbort(err)) {
      // The lists are a convenience; the log itself still loads and says
      // what is wrong. Nothing is thrown further.
      notice("The filter lists could not be loaded; the log below is complete.");
    }
  }
}

/* ── Tabs ─────────────────────────────────────────────────────────────── */

function applyTab() {
  TABS.forEach((name) => {
    const button = document.querySelector(`[data-tab="${name}"]`);
    const on = name === tab;
    button.setAttribute("aria-selected", on ? "true" : "false");
    button.tabIndex = on ? 0 : -1;
    panels[name].hidden = !on;
  });
  // The filters that belong to another tab are hidden rather than disabled:
  // a control that is visible and does nothing is worse than one that is not
  // there, and Tab never reaches it either way. The attribute holds a LIST of
  // tab names, because the Watched page filter belongs to three of the four
  // and one attribute per tab would be the same list written sideways.
  document.querySelectorAll("[data-for-tab]").forEach((field) => {
    field.hidden = !field.dataset.forTab.split(/\s+/).includes(tab);
  });
}

function switchTab(name) {
  if (!TABS.includes(name) || name === tab) return;
  tab = name;
  setState({ tab: name === "errors" ? "" : name, page: pageOf[name] });
  applyTab();
  // The counts in the Source select belong to the tab that is open, so they
  // are re-asked for here. The chosen source is kept by fillSelect().
  loadFilterLists();
  load();
}

document.querySelectorAll("[data-tab]").forEach((button) => {
  button.addEventListener("click", () => switchTab(button.dataset.tab));
  button.addEventListener("keydown", (event) => {
    // APG: the tab strip is one stop, arrows move between the tabs.
    const index = TABS.indexOf(button.dataset.tab);
    let next = null;
    if (event.key === "ArrowRight") next = TABS[(index + 1) % TABS.length];
    if (event.key === "ArrowLeft") next = TABS[(index - 1 + TABS.length) % TABS.length];
    if (event.key === "Home") next = TABS[0];
    if (event.key === "End") next = TABS[TABS.length - 1];
    if (!next) return;
    event.preventDefault();
    switchTab(next);
    document.querySelector(`[data-tab="${next}"]`).focus();
  });
});

/* ── Rendering ────────────────────────────────────────────────────────── */

function pill(kind, label) {
  // The word is the carrier; the colour repeats it. Screen readers get the
  // word twice otherwise, so the label is not duplicated in aria-label.
  return el("span", `log-pill log-pill--${kind}`, label);
}

function emptyState(title, lines, action) {
  const box = el("div", "log-empty");
  box.appendChild(el("h3", null, title));
  lines.forEach((line) => {
    const p = el("p");
    p.innerHTML = line;
    box.appendChild(p);
  });
  if (action) box.appendChild(action);
  return box;
}

/* "Clear filters" and not "Show everything": it puts the view back to its
 * default - the last 24 hours, nothing filtered - and somebody who came
 * from "Everything kept" would otherwise be promised more and given less. */
function clearAction(label) {
  const button = el("button", "button button--secondary", label || "Clear filters");
  button.type = "button";
  button.dataset.clearFilters = "";
  button.addEventListener("click", clearFilters);
  return button;
}

/* The line under "Nothing matches these filters".
 *
 * "Widen the time range" is dropped on "Everything kept": it is the widest
 * range there is, and advice that cannot be followed is worse than none -
 * a reader who tries it finds the control already at its end and concludes
 * the page is confused rather than that they are. What is narrowing the
 * view is then the search term or the source, and Clear filters is the way
 * to both. */
function noMatchLine() {
  return filters().range === "all"
    ? "Go back to the last 24 hours with nothing filtered:"
    : "Widen the time range with the control above, or go back to the "
      + "last 24 hours with nothing filtered:";
}

/* A fragment from the API, finished into a sentence: {error, hint} are
 * lowercase and unpunctuated because the toast prints them as two lines,
 * and anything that puts them into running text has to close them (the
 * same helper as events.js). */
function sentence(text) {
  const clean = String(text == null ? "" : text).trim();
  if (!clean) return "";
  const capital = clean.charAt(0).toUpperCase() + clean.slice(1);
  return /[.!?…]$/.test(capital) ? capital : capital + ".";
}

/* Where the rows would have been: what failed, what it means, and the way
 * to try it again. A panel that keeps spinning after a failed request tells
 * the reader nothing at all. */
function failure(err) {
  const box = el("div", "log-empty log-failed");
  box.appendChild(el("h3", null, "The log could not be loaded"));
  box.appendChild(el("p", "log-failed-what", sentence(err.message) || "The request failed."));
  const hint = sentence(err.hint);
  if (hint) box.appendChild(el("p", "log-failed-hint", hint));
  const retry = el("button", "button", "Try again");
  retry.type = "button";
  retry.addEventListener("click", () => load());
  box.appendChild(retry);
  return box;
}

/* A message where the rows would have been. Every tab but the runs is a
 * list, so one `<li>` holds the box; the runs are a table and need a cell as
 * wide as the header. `place()` picks by tab so nothing else has to. */
function showInList(list, node) {
  list.replaceChildren();
  list.appendChild(el("li")).appendChild(node);
}

function place(name, node) {
  if (name === "runs") showInRuns(node);
  else showInList(VIEWS[name].list, node);
}

function showInRuns(node) {
  runRows.replaceChildren();
  const cell = el("td");
  cell.colSpan = runTable.tHead.rows[0].cells.length;
  cell.appendChild(node);
  const row = document.createElement("tr");
  row.appendChild(cell);
  runRows.appendChild(row);
  // One cell as wide as the box: whatever the table needed before, it does
  // not need it now, and a shaded edge over an empty state would lie.
  updateRunScroll();
}

function showFailure(name, err) {
  place(name, failure(err));
  // The pager belonged to rows that are no longer on the screen; "Page 1 of
  // 2" over a failure message is a promise the view cannot keep.
  pagers[name].hidden = true;
}

/* THE LOG OUTLIVES THE PAGE IT IS ABOUT. A row is kept for ninety days; a
 * watched page is deleted in a second, and the row keeps its name and its
 * id. If every name were a link, a log about a page that no longer exists
 * would send the reader to a three-step editor under a 404 banner - a dead
 * end with a Save button on it, and the button could not succeed.
 *
 * The API says which ids still resolve (`source.exists`), so a name that
 * cannot be opened is printed as text and says why. Only `exists === false`
 * counts: an older build, or an archive that cannot be asked, leaves the
 * field out and the link is offered as before. */
function sourceName(source, label, className = "log-source") {
  if (source.exists === false) {
    const gone = el("span", className, label);
    gone.classList.add("log-source--gone");
    gone.appendChild(el("span", "log-gone", " (deleted)"));
    return gone;
  }
  const link = el("a", className, label);
  link.href = withContext(`/sources/${source.id}`);
  return link;
}

function errorRow(item) {
  const row = el("li", "log-row");
  row.dataset.severity = item.severity;
  row.dataset.kind = item.kind;

  const head = el("div", "log-head");
  // Absolute AND relative, both printed: "when exactly" is what a log is
  // for, "how long ago" is the first thing anybody asks - and a tooltip
  // reaches neither a touch screen nor a keyboard.
  const time = el("time", "log-time", fmtDate(item.date));
  if (item.date) {
    time.dateTime = item.date;
    head.appendChild(time);
    head.appendChild(el("span", "log-ago", ago(item.date)));
  } else {
    head.appendChild(time);
  }
  head.appendChild(pill(item.severity, item.severity_label));
  head.appendChild(el("span", "log-kind", item.kind_label));

  if (item.source && item.source.id) {
    head.appendChild(sourceName(item.source,
      item.source.name || `Watched page ${item.source.id}`));
  } else if (item.source && item.source.name) {
    head.appendChild(el("span", "log-source", item.source.name));
  }
  // The address on the last line already starts with the host, so the head
  // names it only when the address does not: the same host twice in one row
  // wraps the head onto a second line, and on a 1024x768 screen that second
  // line costs one row of the log.
  const uriShowsHost = Boolean(item.uri && item.host && item.uri.includes(item.host));
  if (item.host && !uriShowsHost) head.appendChild(el("span", "log-host", item.host));
  if (item.http_status) head.appendChild(el("span", "log-host", `HTTP ${item.http_status}`));
  row.appendChild(head);

  row.appendChild(el("p", "log-message", item.message));

  // The address and the disclosure share the last line of the row: on a
  // 1024x768 screen every line of chrome costs a row of the log.
  const foot = el("div", "log-foot");
  if (item.detail) {
    const details = el("details", "log-detail");
    const summary = el("summary");
    summary.appendChild(el("span", "log-marker"));
    summary.appendChild(el("span", null, "Technical detail"));
    details.appendChild(summary);
    details.appendChild(el("pre", null, item.detail));
    foot.appendChild(details);
  }
  if (item.uri) foot.appendChild(el("span", "log-uri", item.uri));
  if (foot.childElementCount) row.appendChild(foot);
  return row;
}

/* The runs have their own colours, and they are NOT the severity ones: a
 * run that was skipped because robots.txt says so did what it was supposed
 * to do, and it must not be painted like the failures. Same rule as the
 * pills everywhere: the word carries it, the colour repeats it. */
const RUN_PILL = {
  OK: "done", TEST: "info", BACKPRESSURE: "warning",
  ERROR: "error", BLOCKED: "error", SKIPPED: "neutral",
};

function fmtMs(ms) {
  if (ms === null || ms === undefined || ms === "") return "-";
  const value = Number(ms);
  if (!Number.isFinite(value)) return "-";
  return value < 1000 ? `${fmtInt(value)} ms` : `${(value / 1000).toFixed(1)} s`;
}

/* One run is ONE table row plus, when there is something to say, a second
 * row holding the sentence that says it. As an eleventh column the message
 * would, on a 1024 px screen, put the only line explaining WHY a run was
 * skipped outside the box, where no scrollbar is drawn and no keyboard can
 * reach it - and its invisible wrapping would give every row a different
 * height. Both rows are appended together by renderRuns(). */
function runRow(item) {
  const row = document.createElement("tr");
  row.className = "log-run";
  const when = el("td", "log-time");
  when.appendChild(el("span", null, fmtDate(item.date)));
  when.appendChild(el("span", "log-ago", ago(item.date)));
  row.appendChild(when);

  const source = el("td");
  if (item.source && item.source.id) {
    // The same way to the source as in the Problems tab: a reader who lands
    // on a blocked run must not have to change tab to reach its editor.
    source.appendChild(sourceName(item.source,
      item.name || `Watched page ${item.source.id}`, null));
  } else {
    source.textContent = item.name;
  }
  row.appendChild(source);

  const result = el("td");
  result.appendChild(pill(RUN_PILL[item.status] || "neutral", item.status_label));
  row.appendChild(result);

  ["pages", "links", "accepted", "new", "submitted", "files"].forEach((key) => {
    row.appendChild(el("td", "num", fmtInt(item.counts[key])));
  });
  row.appendChild(el("td", "num", fmtMs(item.ms)));
  if (!item.message) return [row];

  row.classList.add("log-run--noted");
  const note = document.createElement("tr");
  note.className = "log-run-note";
  const cell = el("td");
  cell.colSpan = runTable.tHead.rows[0].cells.length;
  // Read out of the table, the sentence would otherwise arrive with no idea
  // what it is; on screen the position under the run already says it.
  cell.appendChild(el("span", "visually-hidden", "Message: "));
  cell.appendChild(el("span", "log-run-message", item.message));
  note.appendChild(cell);
  return [row, note];
}

/* THE THREE ENDINGS EVERY TAB HAS, in one function: no crawler tables at
 * all, nothing under these filters, or nothing at all. Only the last one
 * differs per tab, and that difference is `view.empty` in the table. */
function renderList(name, data, row) {
  const view = VIEWS[name];
  view.list.replaceChildren();
  if (data.available === false) {
    place(name, emptyState(
      "This archive has no crawler tables yet",
      [data.hint || "Load <code>database/init/03-scraper.sql</code> into the archive."]));
    return false;
  }
  if (!data.items.length) {
    place(name, anyFilter()
      ? emptyState("Nothing matches these filters", [noMatchLine()], clearAction())
      : view.empty(data));
    return false;
  }
  data.items.forEach((item) => view.list.appendChild(row(item)));
  return true;
}

function renderErrors(data) {
  renderList("errors", data, errorRow);
}

function emptyErrors() {
  return emptyState("The crawler has not reported anything yet",
    ["That is the good case: no page has been refused, no file was too "
     + "large and nothing was rejected in this period.",
     "If you expected rows here, check the <strong>Runs</strong> tab - a "
     + "crawler that is not running reports nothing at all. It is started "
     + "with <code>docker compose up -d</code> in <code>crawler/</code>, "
     + "and a watched page only runs once it is switched on."]);
}

function renderRuns(data) {
  runRows.replaceChildren();
  if (data.available === false) {
    showInRuns(emptyState("This archive has no crawler tables yet", [data.hint || ""]));
    return;
  }
  if (!data.items.length) {
    // TWO CAUSES, TWO ANSWERS - the split renderErrors() has always made.
    // One empty state for both said "the crawler is not running, or no
    // page is switched on" to somebody who had merely picked a page and
    // an hour, which is a diagnosis of the customer's installation drawn
    // from their own filter. The crawler may be running perfectly.
    showInRuns(anyFilter()
      ? emptyState("Nothing matches these filters", [noMatchLine()], clearAction())
      : emptyRuns());
    return;
  }
  data.items.forEach((item) => runRow(item).forEach((tr) => runRows.appendChild(tr)));
  updateRunScroll();
}

function emptyRuns() {
  return emptyState("No runs in this period",
    ["A run is written every time a watched page is crawled and every time "
     + "documents are submitted. None here means the crawler is not "
     + "running, or no watched page is switched on.",
     "It is started with <code>docker compose up -d</code> in "
     + "<code>crawler/</code>, and a watched page only runs once it is "
     + "switched on in its editor."]);
}

/* ── The two step streams ─────────────────────────────────────────────── */

/* One step is one row of the same list the Problems tab uses, and not a
 * table: the particulars of a step are one line whose length nobody
 * controls - an address, a job id, six counts - and a column of those wraps
 * to three lines on one row and none on the next. The head holds what can be
 * scanned, the detail sits under it at full width. */
function stepRow(item) {
  const row = el("li", "log-row log-step");
  const head = el("div", "log-head");
  const time = el("time", "log-time", fmtDate(item.date));
  if (item.date) {
    time.dateTime = item.date;
    head.appendChild(time);
    head.appendChild(el("span", "log-ago", ago(item.date)));
  } else {
    head.appendChild(time);
  }
  head.appendChild(el("span", "log-kind", item.action));
  if (item.source && item.source.id) {
    head.appendChild(sourceName(item.source,
      item.source.name || `Watched page ${item.source.id}`));
  }
  if (item.project) head.appendChild(el("span", "log-host", item.project));
  if (item.ms !== null && item.ms !== undefined) {
    head.appendChild(el("span", "log-host", fmtMs(item.ms)));
  }
  row.appendChild(head);
  if (item.detail) row.appendChild(el("p", "log-message", item.detail));
  if (item.run_id) {
    // The id that ties one crawl, or one manual run, together. In the foot
    // with the addresses, because it is what a reader copies into the search
    // box to see the rest of the same story.
    const foot = el("div", "log-foot");
    foot.appendChild(el("span", "log-uri", item.run_id));
    row.appendChild(foot);
  }
  return row;
}

function renderSteps(name, data) {
  renderList(name, data, stepRow);
}

/* THE SENTENCE THIS TAB EXISTS TO AVOID NOT SAYING. Both streams are empty
 * unless somebody switched them on, so an empty panel here is the normal
 * state rather than an accident - and an empty list with no explanation
 * would send the reader looking for a fault that is not there. It names the
 * switch, where the switch is, and when it starts having an effect.
 *
 * `data.setting` comes from the API (api_logs.py: SWITCHES), so the key is
 * spelled in one place rather than in two that can drift. */
function emptySteps(name, data) {
  const setting = (data && data.setting) || `${name}.debug`;
  const when = name === "crawler"
    ? "The crawler reads the switch on its next tick, a few seconds later"
    : "The collector reads the switch at the start of its next round";
  return emptyState(`No steps from the ${name} in this period`,
    [`This tab stays empty until step logging is switched on: the ${name} then `
     + "writes a row here for every step it takes, and nothing at all while the "
     + "switch is off. Off is how an archive is made, because a stream that "
     + "writes by default fills a disk while nobody is watching.",
     `The switch is the <code>${setting}</code> row of the `
     + "<code>dashboard.settings</code> table, set to <code>true</code>. "
     + `${when}, so the rows start on the next pass rather than at once - and `
     + "they are kept for 24 hours."]);
}

/* The runs table is ten columns wide and a window can be narrower than
 * that. When it is, the box scrolls sideways - and a box that scrolls in
 * silence is a box whose last columns do not exist: Chrome draws no
 * scrollbar for a trackpad, and its "a scroller with no focusable content
 * is focusable" rule does not apply here because every row holds a link. So
 * while it really scrolls the box becomes a named region that Tab reaches
 * (Adrian Roselli's responsive-table pattern), and the right edge is shaded
 * to say that the table continues. While it does not, it is a plain div
 * again - a tab stop that leads nowhere is worse than no tab stop. */
function updateRunScroll() {
  if (!runScroll || !runScrollWrap) return;
  const over = runScroll.scrollWidth - runScroll.clientWidth > 2;
  if (over) {
    runScroll.setAttribute("tabindex", "0");
    runScroll.setAttribute("role", "region");
    runScroll.setAttribute("aria-label", "Run history - scrolls sideways");
  } else if (document.activeElement !== runScroll) {
    // Never taken away from under a reader who is standing on it.
    runScroll.removeAttribute("tabindex");
    runScroll.removeAttribute("role");
    runScroll.removeAttribute("aria-label");
  }
  runScrollWrap.dataset.scrollable = over ? "true" : "false";
  const end = runScroll.scrollLeft + runScroll.clientWidth >= runScroll.scrollWidth - 2;
  runScrollWrap.dataset.atEnd = over && !end ? "false" : "true";
}

if (runScroll) {
  runScroll.addEventListener("scroll", updateRunScroll, { passive: true });
  window.addEventListener("resize", updateRunScroll);
}

/* Focus and the pager: a button that is disabled while it has focus drops
 * the reader on <body>, and the next Tab starts at the top of the page. */
function setDisabled(button, disabled, sibling, name) {
  if (disabled && document.activeElement === button) {
    const target = sibling && !sibling.disabled ? sibling : titles[name];
    if (target) target.focus();
  }
  button.disabled = disabled;
}

function renderPager(name, data) {
  const pager = pagers[name];
  const info = pageInfos[name];
  const prev = pager.querySelector("[data-page-prev]");
  const next = pager.querySelector("[data-page-next]");
  const pages = data.pages || 0;
  pager.hidden = pages <= 1;
  info.textContent = pages ? `Page ${data.page + 1} of ${pages}` : "";
  setDisabled(prev, data.page <= 0, next, name);
  setDisabled(next, data.page + 1 >= pages, prev, name);
}

/* ── The two numbers on the tab strip ─────────────────────────────────── */

function setTabCount(name, total) {
  const badge = document.querySelector(`[data-count-for="${name}"]`);
  if (badge) badge.textContent = total === "" ? "" : fmtInt(total);
}

/* What the OTHER tabs would show under the filters that are set. The strip
 * has to answer "did anything run at all" before it is opened: with a
 * number only on the tab in front of the reader, "Problems 0" and "nothing
 * ran" look the same - which is exactly the case the second tab exists for.
 * One row is fetched (page_size=1); the count comes from count(*) OVER ().
 *
 * A CHANNEL PER TAB, not one for all of them: a second call on a channel
 * aborts the first (api.js), so three badges sharing one would leave two
 * of them empty and only the last would ever arrive. */
async function countTab(name) {
  try {
    const data = await api(VIEWS[name].endpoint, {
      channel: `log-count-${name}`, quiet: true,
      params: { ...paramsFor(name), page: 0, page_size: 1 },
    });
    setTabCount(name, data && data.available === false ? "" : data.total);
  } catch (err) {
    // A number on a tab is worth no message of its own: the panel below
    // already says what could not be loaded, and one problem gets one
    // sentence. The strip simply carries no number.
    if (!isAbort(err)) setTabCount(name, "");
  }
}

function countOthers() {
  TABS.filter((name) => name !== tab).forEach(countTab);
}

/* The chosen labels are read off the fields rather than mapped again here,
 * so a renamed range cannot end up with two names. */
function chosenLabel(field) {
  const chosen = field.selectedOptions && field.selectedOptions[0];
  if (!chosen) return "";
  return chosen.dataset.plain || chosen.text;
}

/* The header line of the panel: how many, over what period, under which
 * filters - one sentence, next to the title, and the caption of the
 * printed page as well. */
function describe(name, data) {
  const view = VIEWS[name];
  const f = filters();
  const noun = view.noun[data.total === 1 ? 0 : 1];
  const parts = [`${fmtInt(data.total)} ${noun}`, chosenLabel(rangeField).toLowerCase()];
  view.filters.forEach((key) => { if (f[key]) parts.push(chosenLabel(FIELDS[key])); });
  if (f.q) parts.push(`matching “${f.q}”`);
  return parts.join(" - ");
}

/* ── Loading ──────────────────────────────────────────────────────────── */

let clamped = false;

async function load(opts = {}) {
  rememberFilters();
  // The tab AS IT IS NOW. Switching tabs aborts this request, but the
  // rejection arrives a tick later - and everything after the await must
  // still be about the list it was started for.
  const name = tab;
  const view = VIEWS[name];
  const params = { ...paramsFor(name), page: pageOf[name] };

  view.list.setAttribute("aria-busy", "true");

  try {
    // quiet: the failure is reported in the panel, where the rows were and
    // where the way to try again is. The shared toast would say the same two
    // sentences a second time, in the opposite corner.
    const data = await api(view.endpoint, { channel: "log", params, quiet: true });
    view.render(data);
    metas[name].textContent = describe(name, data);
    setTabCount(name, data.available === false ? "" : data.total);

    // A page number out of a link that has since gone out of range: land on
    // the last page there is rather than on an empty one.
    if (!clamped && data.pages && pageOf[name] + 1 > data.pages) {
      clamped = true;
      pageOf[name] = data.pages - 1;
      await load(opts);
      clamped = false;
      return;
    }
    renderPager(name, data);
    announce(metas[name].textContent);
    // And what the other tabs hold under the same filters, so the strip can
    // tell "nothing went wrong" from "nothing ran at all".
    countOthers();
    if (opts.scroll && panels[name]) {
      // After paging the reader must start at the first row of the new
      // page, not halfway down it where the old scroll position was.
      panels[name].scrollIntoView({ block: "start" });
    }
  } catch (err) {
    if (isAbort(err)) return;
    // The panel says it where the rows would have been, with the way to try
    // again; nothing else says it anywhere. The toast is suppressed above,
    // so this is what a screen reader is told - once, and interrupting,
    // because the reader is waiting for a list that is not coming.
    showFailure(name, err);
    setTabCount(name, "");
    metas[name].textContent = "could not be loaded";
    announce(`The log could not be loaded. ${sentence(err.message)}`, { assertive: true });
  } finally {
    view.list.setAttribute("aria-busy", "false");
  }
}

/* ── Events ───────────────────────────────────────────────────────────── */

form.addEventListener("submit", (event) => {
  event.preventDefault();
  TABS.forEach((name) => { pageOf[name] = 0; });
  clearNotices();
  loadFilterLists();
  load();
});

clearButton.addEventListener("click", clearFilters);

// A changed range changes the counts in every filter list, so both are
// refreshed together. The other selects only re-ask for rows.
rangeField.addEventListener("change", () => {
  pageOf[tab] = 0;
  loadFilterLists();
  load();
});
[sourceField, kindField, severityField, statusField].forEach((field) => {
  field.addEventListener("change", () => {
    pageOf[tab] = 0;
    load();
  });
});

TABS.forEach((name) => {
  pagers[name].querySelector("[data-page-prev]").addEventListener("click", () => {
    if (pageOf[name] > 0) { pageOf[name] -= 1; load({ scroll: true }); }
  });
  pagers[name].querySelector("[data-page-next]").addEventListener("click", () => {
    pageOf[name] += 1;
    load({ scroll: true });
  });
});

restoreFilters();
applyTab();
loadFilterLists().then(load);
