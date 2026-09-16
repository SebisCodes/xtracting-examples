/* ==========================================================================
 *  The Tables view: the archive as rows, one kind at a time.
 *
 *  Eight tabs - the eight the Diagrams view draws pictures of - and on each
 *  the rows themselves, drawn by static/js/rows.js: the same table the
 *  drilldown dialog shows behind a clicked bar, mounted on the page. A
 *  reader who has learned the dialog has learned this page.
 *
 *  THE STATE LIVES IN THE URL (state.js): the tab, the name, the type, the
 *  sort, whether the buckets are shown, and the one document the page may
 *  be narrowed to. A reload comes back to the same rows, a copied link
 *  shows them to a colleague, and the Export menu reads what to export off
 *  the same address. Back and Forward change the URL and the page follows.
 *
 *  TWO SPEEDS. Name and Type are read when Search is pressed, like every
 *  other search box on this dashboard; the tab and the sort reload at
 *  once, because each is one press and a reader who has to press twice to
 *  see a tab reads the page as broken.
 *
 *  THREE THINGS OPEN IN PLACE, none of them in a dialog:
 *    a GROUP ROW (sort by most or fewest) opens into the rows it counts -
 *      rows.js does that, with the loader below;
 *    a DOCUMENT ROW opens into everything the archive read out of that
 *      page, one table per kind (renderDocument) - the rows.js
 *      `expand.render` hook hands this module the box under the row;
 *    a BUCKET opens into the documents that mention its members.
 *
 *  ONE DOCUMENT. The Rows link on every row of the product lands here with
 *  `?source=<task>|<id>`: the Sources tab, that document alone, already
 *  opened, and a line saying so with the way back to every document.
 * ========================================================================== */

import { api, isAbort, sentence, watchSearch } from "./api.js";
import { state, set as setState, onChange, contextHref } from "./state.js";
import { announce } from "./a11y.js";
import { mountRows } from "./rows.js";

const form = document.getElementById("tables-search");
const nameField = document.getElementById("tables-q");
const nameLabel = document.querySelector("[data-name-label]");
const typeField = document.getElementById("tables-type");
const sortField = document.getElementById("tables-sort");
const bucketsField = document.getElementById("tables-buckets");
const statusLine = document.getElementById("tables-status");
const oneLine = document.getElementById("tables-one");
const allLink = document.getElementById("tables-all");
const panel = document.getElementById("tables-panel");
const scrollBox = document.getElementById("tables-scroll");
const panelTitle = document.getElementById("tables-panel-title");
const panelMeta = document.getElementById("tables-panel-meta");
const counterRoot = document.getElementById("tables-counter");
const bucketsMeta = document.getElementById("tables-buckets-meta");
const bucketsBox = document.getElementById("tables-buckets-box");
const bucketsBody = document.getElementById("tables-buckets-body");
const rowsRoot = document.getElementById("tables-rows");
const tabStrip = document.querySelector('[role="tablist"]');
const tabs = Array.from(document.querySelectorAll('[role="tab"]'));

const SORTS = ["newest", "oldest", "most", "fewest"];
const DEFAULT_TAB = tabs.length ? tabs[0].dataset.tab : "sources";

function el(tag, className, text) {
  const e = document.createElement(tag);
  if (className) e.className = className;
  if (text !== undefined && text !== null) e.textContent = text;
  return e;
}

function fmt(n) {
  return (Number(n) || 0).toLocaleString();
}

function rowsWord(n) {
  const v = Number(n) || 0;
  return `${fmt(v)} ${v === 1 ? "row" : "rows"}`;
}

function tabLabel(id) {
  const button = tabs.find((t) => t.dataset.tab === id);
  return button ? button.textContent.trim() : id;
}

/* ── What the page is showing ─────────────────────────────────────────── */

/* Read once from the URL, written back on every change. The tab and the
 * sort fall back to their first values rather than to "", so a request
 * never asks for a sort the server does not know. */
function activeTab() {
  return tabs.some((t) => t.dataset.tab === state.tab) ? state.tab : DEFAULT_TAB;
}
function activeSort() {
  return SORTS.includes(state.sort) ? state.sort : "newest";
}
function bucketsOn() {
  return state.buckets === "1";
}
/* "<task>|<id>", the address the Rows link carries, as its two halves. */
function oneDocument() {
  const raw = state.source || "";
  const at = raw.indexOf("|");
  if (at < 0) return null;
  const task = raw.slice(0, at);
  const id = raw.slice(at + 1);
  return task && id ? { task, id } : null;
}
/* A CHANGE THIS MODULE MAKES IS NOT NEWS TO IT. state.js tells every
 * listener about every change, this module included, and the listener at
 * the foot of this file reloads the rows for a Back or a Forward. A search
 * this module has just sent would then load twice. So its own writes go
 * through here, flagged, and the listener lets them pass. */
let ownChange = false;
function own(patch) {
  ownChange = true;
  try { return setState(patch); } finally { ownChange = false; }
}

/* ── The links every row carries ──────────────────────────────────────── */

/* The second link of a row goes to that host's own diagrams, the third to
 * the entity's, and the Rows link to this page with the document alone. All
 * of them keep the project and the language (state.js: contextHref). */
const links = {
  source: (domain) => contextHref("/diagrams/source", { q: domain, axis: "object" }),
  entity: (name) => contextHref("/diagrams/entity", { q: name, axis: "object" }),
  rows: (src) => contextHref("/tables", { tab: "sources", source: `${src.task}|${src.id}` }),
};

/* ── The status line ──────────────────────────────────────────────────── */

function say(text, { error = false } = {}) {
  if (!statusLine) return;
  statusLine.textContent = text || "";
  statusLine.hidden = !text;
  statusLine.classList.toggle("is-error", Boolean(error));
}

/* What rows.js reports while it loads: "Loading…" while busy, the count
 * when done. The form's own status line (api.js: searching) says
 * "Searching…" beside the button while a request is in flight; this line
 * is about the rows.
 *
 * ONLY THE CURRENT LISTING SPEAKS. A new question replaces the table and
 * aborts the request of the old one, and the old table then reports that
 * its page failed - which is true and not news. Each listing is numbered,
 * and a report from a number that is not the current one is dropped. */
let listing = 0;
/* A COUNT IS NOT SAID TWICE. When a page has loaded, rows.js reports the
 * count ("6 rows", "20 rows shown, more available") - and the counter at
 * the top of the box says the same, one line lower, with the position.
 * So the line above the box says what is happening while it happens and
 * what went wrong when it did; a plain count goes to the counter alone,
 * which gives a 768 px window a line of rows back. */
const PLAIN_COUNT = /^[\d,.]+ rows?( shown, more available)?$/;
function rowsStatus(busy, text) {
  const words = text || "";
  if (!busy && PLAIN_COUNT.test(words)) { say(""); return; }
  say(words, { error: !busy && /^Could not/.test(words) });
}
function statusFor(number) {
  return (busy, text) => { if (number === listing) rowsStatus(busy, text); };
}

/* ── The scroll box ───────────────────────────────────────────────────── */

/* THE BOX IS A LITTLE SHORTER THAN THE WINDOW, MEASURED. With the page
 * scrolled so that the counter sits at the top of the window, the whole
 * card has to fit under it: the counter, the card's title and paddings,
 * the box, and a margin. Everything but the box is measured off the page,
 * so a title that wraps or a hint that opens is accounted for; only the
 * box's own height is set. Re-measured on resize and whenever the page
 * above the box changes height. */
const BOX_MARGIN = 24;
const MIN_BOX_HEIGHT = 240;
function fitBox() {
  if (!scrollBox || !panel) return;
  const above = counterRoot
    ? scrollBox.getBoundingClientRect().top - counterRoot.getBoundingClientRect().top
    : scrollBox.getBoundingClientRect().top - panel.getBoundingClientRect().top;
  const below = panel.getBoundingClientRect().bottom - scrollBox.getBoundingClientRect().bottom;
  const room = Math.floor(window.innerHeight - above - below - BOX_MARGIN);
  scrollBox.style.maxHeight = `${Math.max(MIN_BOX_HEIGHT, room)}px`;
}
let fitting = false;
function fitSoon() {
  if (fitting) return;
  fitting = true;
  requestAnimationFrame(() => { fitting = false; fitBox(); });
}
window.addEventListener("resize", fitSoon);
if ("ResizeObserver" in window) {
  const watch = new ResizeObserver(fitSoon);
  document.querySelectorAll("#tables-counter, #tables-panel > .card-header")
    .forEach((node) => watch.observe(node));
}
fitBox();

/* ── Requests ─────────────────────────────────────────────────────────── */

/* One page of the open tab, as the search row asks for it. The one-document
 * mode asks for that document's rows instead of a name: the field then
 * shows the document's label, which is not a search term. */
function pageParams(page) {
  const doc = oneDocument();
  const p = {
    sort: doc ? "newest" : activeSort(),
    ddpage: page,
  };
  if (doc) {
    p.task = doc.task;
    p.source = doc.id;
  } else {
    if (state.q) p.q = state.q;
    if (state.type) p.type = state.type;
    if (bucketsOn() && page === 1) p.buckets = "1";
  }
  return p;
}

function loadPage(page) {
  return api(`/api/tables/${encodeURIComponent(activeTab())}`, {
    channel: "tables", params: pageParams(page),
  });
}

/* The rows behind a group row: the same table, one name and one type
 * exactly, newest first. The key is the server's own (name and type, or
 * entity id and type), handed back as it came. */
function loadGroup(row, page) {
  const key = row.key || {};
  const p = { exact: "1", sort: "newest", ddpage: page };
  Object.keys(key).forEach((k) => {
    if (key[k] !== "" && key[k] !== null && key[k] !== undefined) p[k] = key[k];
  });
  return api(`/api/tables/${encodeURIComponent(activeTab())}`, { params: p });
}

/* The sentence over what opens under a row: the document's own words for
 * a document row, the group's name and type for a group row. */
function groupLabel(row) {
  const link = row.link || {};
  if (link.uri) return [link.domain, link.title].filter(Boolean).join(" - ") || "This document";
  const key = row.key || {};
  const cells = row.cells || {};
  const named = key.name || row.entity
    || ((cells.name || cells.entity || [])[0] || {}).text || "";
  return [named, key.type].filter(Boolean).join(" - ") || "This group";
}

/* ── The document that opens under a row ──────────────────────────────── */

/* EVERYTHING THE ARCHIVE READ OUT OF ONE PAGE, one table per kind.
 *
 * The first page of every kind comes in one answer (/api/tables/source),
 * so a document opens in one round trip; a kind that has more pages fetches
 * them from the tab's own endpoint, narrowed to this document. Kinds with
 * no rows are named in one muted line rather than drawn as empty tables:
 * five headings over nothing would be five things to read past.
 *
 * `box` is the region rows.js built under the row; `hooks.status` is the
 * page's status line and `hooks.resized` tells the outer table that what
 * is on screen changed height. */
function renderDocument(row, box, hooks) {
  const src = row.source || row.key || {};
  const holder = el("div", "tables-doc");
  box.appendChild(holder);
  hooks.status(true, "Loading the document…");
  api("/api/tables/source", { params: { task: src.task, id: src.id } })
    .then((data) => {
      const kinds = (data && data.kinds) || [];
      // The label the search field shows in one-document mode: the
      // document's own words, not its key.
      const doc = oneDocument();
      if (doc && doc.task === src.task && doc.id === src.id && data && data.source) {
        const link = data.source.link || {};
        const label = [link.domain, link.title].filter(Boolean).join(" - ");
        if (label && nameField) nameField.value = label;
      }
      const empty = [];
      kinds.forEach((kind) => {
        const total = Number(kind.total) || 0;
        if (!total) { empty.push(kind.title || tabLabel(kind.tab)); return; }
        const section = el("section", "tables-doc-kind");
        section.appendChild(el("h3", "tables-doc-title",
          `${kind.title || tabLabel(kind.tab)} - ${rowsWord(total)}`));
        const root = el("div");
        section.appendChild(root);
        holder.appendChild(section);
        const first = { rows: kind.rows || [], columns: kind.columns || [],
                        row_links: kind.row_links || { source: true, entity: false },
                        more: Boolean(kind.more), offset: 0, total };
        mountRows(root, {
          scroller: scrollBox,
          status: hooks.status,
          // The document stands over the table; naming it on every row
          // would say the same thing forty-two times.
          sourceColumn: false,
          rowsLabel: `${kind.title || tabLabel(kind.tab)} of this document`,
          emptyText: "No rows of this kind any more - the archive may have changed.",
          links: { source: links.source, entity: links.entity },
          load: (page) => (page === 1
            ? Promise.resolve(first)
            : api(`/api/tables/${encodeURIComponent(kind.tab)}`, {
                params: { task: src.task, source: src.id, sort: "newest", ddpage: page },
              })),
        });
      });
      if (empty.length) {
        holder.appendChild(el("p", "tables-doc-none",
          `No rows of: ${empty.join(", ")}.`));
      }
      if (!kinds.length) {
        holder.appendChild(el("p", "tables-doc-none", "Nothing in the archive about this document."));
      }
      hooks.status(false, "");
      hooks.resized();
      announce(`Document opened: ${kinds.length - empty.length} kinds of rows`);
    })
    .catch((err) => {
      if (isAbort(err)) return;
      console.warn("tables: document failed", err);
      holder.appendChild(el("p", "tables-doc-none",
        [sentence(err && err.message), sentence(err && err.hint)].filter(Boolean).join(" ")
        || "Could not load the document."));
      hooks.status(false, "");
      hooks.resized();
    });
}

/* ── The table ────────────────────────────────────────────────────────── */

let rows = null;
/* The first answer of the current listing, kept for the bucket block: the
 * buckets ride on page 1 and are drawn once per search. */
let firstPage = null;

function emptyText() {
  const doc = oneDocument();
  // A link names a document by its key; an archive that holds rows about
  // a document it has no row for answers nothing here, and says so.
  if (doc) return "The archive has no document row for this link.";
  return state.q || state.type ? "No rows match." : "No rows on this tab.";
}

/* The listing, built anew for every question: the columns change with the
 * tab and the group rows change with the sort, and rows.js starts at row 1
 * for either. */
function mount() {
  if (!rowsRoot || !panel) return;
  if (rows) rows.destroy();
  rowsRoot.textContent = "";
  bucketsBody.textContent = "";
  bucketsBox.hidden = true;
  firstPage = null;
  const tab = activeTab();
  const doc = oneDocument();
  const onSources = tab === "sources";
  listing += 1;
  if (counterRoot) counterRoot.textContent = "";
  if (panelTitle) panelTitle.textContent = activeSort() === "most" || activeSort() === "fewest"
    ? `${tabLabel(tab)} by name` : tabLabel(tab);
  if (panelMeta) panelMeta.textContent = "";
  rows = mountRows(rowsRoot, {
    scroller: scrollBox,
    counterRoot,
    status: statusFor(listing),
    rowsLabel: `${tabLabel(tab)} rows`,
    emptyText: emptyText(),
    // The Rows link is not drawn on the Sources tab: every document row
    // there opens in place, which is what the link would lead to.
    links: onSources ? { source: links.source, entity: links.entity } : links,
    load: (page) => loadPage(page).then((res) => {
      if (page === 1) {
        firstPage = res;
        drawBuckets(res && res.buckets);
        cardMeta(res);
        examplePlaceholders(res && res.example);
        if (doc && res && res.rows && res.rows.length) openFirstRow();
      }
      // A GROUP ROW SAYS ITS COUNT ONCE. Its first cell is the control
      // that opens it and reads "6 rows"; the count column the answer also
      // carries (for the file the Export menu writes) would say the same
      // number a second time on the same line.
      if (res && Array.isArray(res.columns) && (activeSort() === "most" || activeSort() === "fewest")) {
        res.columns = res.columns.filter((c) => c.key !== "count");
      }
      // EVERY DOCUMENT ROW OPENS. rows.js opens a row that carries a key;
      // a document's key is its task and id, which every row carries as
      // `source`. Group rows keep the key the server gave them.
      if (onSources && res && Array.isArray(res.rows)) {
        res.rows.forEach((r) => {
          if (r.key === undefined && r.source && r.source.task && r.source.id) {
            r.key = { task: r.source.task, id: r.source.id };
          }
        });
      }
      return res;
    }),
    expand: {
      noun: "rows",
      button: "All rows",
      label: groupLabel,
      load: loadGroup,
      emptyText: "No rows behind this group any more - the archive may have changed.",
      // A document row opens into its kinds (rows.js hands this module
      // the box under the row); a group row keeps the nested table rows.js
      // draws itself.
      render: renderDocument,
    },
  });
}

/* The count in the card's header: how many rows or groups the whole
 * listing holds, beside the title, the way every card on this dashboard
 * says what is in it. */
function cardMeta(res) {
  if (!panelMeta) return;
  const total = res && Number.isFinite(res.total) ? res.total : null;
  if (total === null) { panelMeta.textContent = ""; return; }
  const grouped = activeSort() === "most" || activeSort() === "fewest";
  panelMeta.textContent = grouped
    ? `${fmt(total)} ${total === 1 ? "name" : "names"}`
    : rowsWord(total);
}

/* THE PLACEHOLDER IS THE FIRST ROW. What stands greyed in the Name and
 * Type fields is the name and the type of the first row on screen - an
 * example out of this archive, in this tab, in the spelling the field
 * matches - rather than a made-up one. A tab with no rows keeps the words
 * that say the fields are empty. */
function examplePlaceholders(example) {
  if (!example) return;
  if (nameField && example.name) nameField.placeholder = example.name;
  if (typeField && example.type) typeField.placeholder = example.type;
}

/* The one-document mode opens its row as soon as it is drawn. rows.js
 * draws the rows in the continuation of the load, so the control exists
 * one frame after the promise resolves. */
function openFirstRow() {
  requestAnimationFrame(() => {
    const control = rowsRoot.querySelector(".dd-expand");
    if (control && control.getAttribute("aria-expanded") !== "true") control.click();
  });
}

/* ── Buckets ──────────────────────────────────────────────────────────── */

function drawBuckets(list) {
  if (!bucketsBox || !bucketsBody) return;
  bucketsBody.textContent = "";
  const buckets = Array.isArray(list) ? list : [];
  bucketsBox.hidden = !(bucketsOn() && buckets.length);
  if (bucketsBox.hidden) return;
  if (bucketsMeta) bucketsMeta.textContent = `${fmt(buckets.length)} ${buckets.length === 1 ? "bucket" : "buckets"}`;
  buckets.forEach((bucket, i) => {
    const tr = el("tr", "drilldown-row");
    const num = el("th", "dd-num", fmt(i + 1));
    num.scope = "row";
    tr.appendChild(num);
    const name = el("td", "dd-cell");
    name.appendChild(bucketControl(bucket));
    tr.appendChild(name);
    tr.appendChild(el("td", "dd-cell", bucket.kind_label || bucket.kind || ""));
    const members = el("td", "dd-cell tables-bucket-members");
    const held = Number(bucket.member_count) || (bucket.members || []).length;
    members.appendChild(el("span", "tables-bucket-count", fmt(held)));
    const names = (bucket.members || []).slice(0, 5).map((m) => m.name).filter(Boolean);
    const rest = held - names.length;
    members.appendChild(document.createTextNode(
      names.join(", ") + (rest > 0 ? `${names.length ? ", " : ""}and ${fmt(rest)} more` : "")));
    tr.appendChild(members);
    bucketsBody.appendChild(tr);
  });
}

/* A bucket opens into the documents that mention its members, newest
 * first, under its row - the same control and the same nested box the
 * group rows use (css/drilldown.css: .dd-expand, .drilldown-nested). */
let bucketId = 0;
function bucketControl(bucket) {
  const button = el("button", "dd-expand");
  button.type = "button";
  button.setAttribute("aria-expanded", "false");
  const mark = el("span", "dd-expand-mark");
  mark.setAttribute("aria-hidden", "true");
  button.appendChild(mark);
  button.appendChild(document.createTextNode(bucket.name || "Bucket"));
  bucketId += 1;
  const id = `tables-bucket-${bucketId}`;
  button.setAttribute("aria-controls", id);
  button.setAttribute("aria-label", `Show the documents of ${bucket.name || "this bucket"}`);
  button.title = "Show documents";
  let nested = null;
  let table = null;
  button.addEventListener("click", () => {
    const open = button.getAttribute("aria-expanded") !== "true";
    button.setAttribute("aria-expanded", String(open));
    button.setAttribute("aria-label", `${open ? "Hide" : "Show"} the documents of ${bucket.name || "this bucket"}`);
    button.title = `${open ? "Hide" : "Show"} documents`;
    if (!nested) {
      nested = el("tr", "drilldown-nested");
      nested.id = id;
      const td = el("td", "drilldown-nested-cell");
      td.colSpan = 4;
      nested.appendChild(td);
      const region = el("div", "drilldown-nested-box");
      region.setAttribute("role", "region");
      region.setAttribute("aria-label", `Documents of ${bucket.name || "this bucket"}`);
      td.appendChild(region);
      button.closest("tr").after(nested);
      table = mountRows(region, {
        scroller: null,
        status: rowsStatus,
        rowsLabel: `Documents of ${bucket.name || "this bucket"}`,
        emptyText: "No documents mention this bucket's members.",
        links,
        load: (page) => api("/api/tables/bucket/sources", {
          params: { kind: bucket.kind, name: bucket.name, ddpage: page },
        }),
      });
    } else {
      nested.hidden = !open;
    }
    });
  return button;
}

/* ── The form ─────────────────────────────────────────────────────────── */

/* Read at ONE moment, when Search is pressed: what is typed and never
 * confirmed is still what the reader is asking for. A search leaves the
 * one-document mode: the field then carries a term, not a label. */
function submit(event) {
  if (event) event.preventDefault();
  own({
    q: nameField ? nameField.value.trim() : "",
    type: typeField ? typeField.value.trim() : "",
    sort: sortField ? sortField.value : "newest",
    buckets: bucketsField && bucketsField.checked ? "1" : "",
    source: "",
    page: 0,
  });
  syncOneLine();
  mount();
}
if (form) {
  form.addEventListener("submit", submit);
  watchSearch(form, "tables");
}
if (sortField) {
  sortField.addEventListener("change", () => {
    own({ sort: sortField.value });
    mount();
  });
}
if (bucketsField) {
  // The checkbox is part of the search, and pressing Search is what sends
  // it - but a reader who ticks it while the answer is on screen has asked
  // one thing of a listing already loaded, so the block is drawn from the
  // first page kept for it rather than fetched again.
  bucketsField.addEventListener("change", () => {
    own({ buckets: bucketsField.checked ? "1" : "" });
    if (bucketsField.checked && !(firstPage && firstPage.buckets)) mount();
    else drawBuckets(firstPage && firstPage.buckets);
  });
}

/* The way back from one document to every document. */
if (allLink) {
  allLink.href = contextHref("/tables", { tab: "sources" });
  allLink.addEventListener("click", (event) => {
    event.preventDefault();
    own({ source: "", q: "" });
    if (nameField) nameField.value = "";
    syncOneLine();
    mount();
  });
}
function syncOneLine() {
  if (oneLine) oneLine.hidden = !oneDocument();
}

/* ── Tabs (WAI-ARIA APG: click, arrows, Home/End) ─────────────────────── */

function applyTab() {
  const active = activeTab();
  tabs.forEach((tab) => {
    const on = tab.dataset.tab === active;
    tab.setAttribute("aria-selected", on ? "true" : "false");
    tab.tabIndex = on ? 0 : -1;
  });
  if (panel) panel.setAttribute("aria-labelledby", `tab-${active}`);
  // "Document" on the Sources tab: a document has a title and a host, not
  // a name, and the field asks for the word the tab's rows carry.
  if (nameLabel) nameLabel.textContent = active === "sources" ? "Document" : "Name";
  own({ tab: active });
}

function selectTab(next, { focus = false } = {}) {
  if (!next || next === activeTab()) return;
  // A tab is a different kind of row; the one document belongs to the
  // Sources tab and does not follow.
  own({ tab: next, source: "" });
  syncOneLine();
  applyTab();
  if (focus) {
    const button = tabs.find((t) => t.dataset.tab === next);
    if (button) button.focus();
  }
  mount();
}

tabs.forEach((tab) => {
  tab.addEventListener("click", () => selectTab(tab.dataset.tab));
});

/* Arrows MOVE, Enter and Space CHOOSE (manual activation): each tab is a
 * request, and selection-follows-focus would fire one per keystroke. */
if (tabStrip) {
  tabStrip.addEventListener("keydown", (event) => {
    const here = tabs.findIndex((t) => t === document.activeElement);
    const index = here >= 0 ? here : tabs.findIndex((t) => t.dataset.tab === activeTab());
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

/* ── Back and Forward ─────────────────────────────────────────────────── */

/* The URL changed under the page; the page follows it rather than arguing.
 * The fields are refilled from the state, because a Back that restores a
 * search has to restore what the search said. */
onChange((next, changed) => {
  if (ownChange) return;
  const mine = ["tab", "q", "type", "sort", "buckets", "source"];
  if (!changed.some((k) => mine.includes(k))) return;
  if (nameField && !oneDocument()) nameField.value = next.q || "";
  if (typeField) typeField.value = next.type || "";
  if (sortField) sortField.value = activeSort();
  if (bucketsField) bucketsField.checked = bucketsOn();
  syncOneLine();
  applyTab();
  mount();
});

/* ── First paint ──────────────────────────────────────────────────────── */

// The one document's tab is the Sources tab, whatever the URL says beside
// it: a document's row is a sources row.
if (oneDocument() && state.tab !== "sources") own({ tab: "sources" });
syncOneLine();
applyTab();
// The sort and the buckets are always written out, for the reason the
// Diagrams view writes its tab: a URL that leaves out "the default" cannot
// be exported and cannot be read by anyone who has to know what it shows.
own({ sort: activeSort() });
mount();
