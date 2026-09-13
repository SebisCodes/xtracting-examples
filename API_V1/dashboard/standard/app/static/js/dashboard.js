/* ==========================================================================
 *  The landing view.
 *
 *  Three blocks, three requests, no dependency between them: each one fills
 *  its own block and each one fails on its own. A slow "latest events" must
 *  never keep the counters off the screen - the page is what somebody looks
 *  at first, and half of it now beats all of it in four seconds.
 *
 *  Every block therefore does the same four things, and the helpers below
 *  are what they share: show a loading line, replace it with rows, say
 *  "nothing yet" when the archive has nothing, and put an error INSIDE the
 *  block it belongs to. That is why all three calls are `quiet`, and it is
 *  the one place on the dashboard where quiet is not about wording: an
 *  archive that is down fails all three at once, and three toasts stacked in
 *  the corner - none of which can say WHICH block it came from, because they
 *  are all the same sentence - are worse than three notices sitting in the
 *  three blocks that are empty. The sentences themselves come from api.js
 *  (`sentence()`), so the notices read as the toasts elsewhere do.
 *
 *  Links carry the project and the language, because every page needs the
 *  pair and a link that loses it lands on somebody else's project.
 * ========================================================================== */

import { api, isAbort, fmtInt, fmtDate, fmtAgo, sentence } from "./api.js";
import { state } from "./state.js";
import { count as countColour, relevance as relevanceColour } from "./palette.js";
import { openDrilldown } from "./drilldown.js";

/* The four shorter periods; the year is the tile's big number. */
const PERIODS = [["hour", "hour"], ["day", "day"], ["week", "week"], ["month", "month"]];

function el(tag, className, text) {
  const e = document.createElement(tag);
  if (className) e.className = className;
  if (text !== undefined && text !== null) e.textContent = text;
  return e;
}

/* A link inside the dashboard, with the pair appended. Absolute paths only:
 * these all point at another view, never at the outside world. */
function withContext(href) {
  const url = new URL(href, window.location.origin);
  if (state.project && !url.searchParams.has("project")) url.searchParams.set("project", state.project);
  if (state.language && !url.searchParams.has("language")) url.searchParams.set("language", state.language);
  return url.pathname + url.search;
}

function link(href, text, className) {
  const a = el("a", className, text);
  a.href = withContext(href);
  return a;
}

/* WHAT HAPPENED, then WHAT TO DO - two blocks, the way the toast does it.
 * One line holding both was unreadable: the remedy started mid-sentence
 * with no capital and no full stop in front of it. */
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

function failed(container, what, err) {
  container.setAttribute("aria-busy", "false");
  container.textContent = "";
  const p = errorNotice(`Could not load ${what}.`, err);
  if (container.tagName === "UL") {
    const li = el("li");
    li.appendChild(p);
    container.appendChild(li);
  } else {
    container.appendChild(p);
  }
}

/* ── Stat tiles ──────────────────────────────────────────────────────── */

/* One tile per archive table. The big number is the last YEAR, not a total:
 * the endpoint deliberately has no total (a full scan for a number nobody
 * acts on), and "in the last year" is the honest label for what it is.
 *
 * Under it the four shorter periods, each as a number AND as a bar. Twelve
 * numbers in a grid have to be read and ranked one by one; a length is seen
 * before it is read (NN/g on preattentive attributes). The bars share one
 * scale per tile - the periods nest, so the month is the tallest - and never
 * one scale across tiles, which would compare five sources with 49 ratings.
 * That per-tile scale is then PRINTED under the bars: two tiles drawn to two
 * different maxima look identical, and the eye compares them regardless of
 * what the numbers say, so the tile has to name the number it is drawn to.
 *
 * The tile is a link, and it says so in words: `stat-go` is the resting cue
 * that a mouse-free reader needs (see views.css). */
function periodBar(value, scale) {
  const bar = el("span", "stat-bar");
  bar.setAttribute("aria-hidden", "true");   // the number beside it is the datum
  const fill = el("i", value > 0 ? null : "is-zero");
  fill.style.height = scale > 0 ? `${Math.round((value / scale) * 100)}%` : "0%";
  bar.appendChild(fill);
  return bar;
}

function tile(row) {
  const label = row.label || row.id;
  const t = link(row.tab ? `/diagrams?tab=${encodeURIComponent(row.tab)}` : "/diagrams",
                 null, "stat-tile stat-tile--link");
  t.appendChild(el("p", "stat-label", label));
  t.appendChild(el("p", "stat-value", fmtInt(row.year)));
  t.appendChild(el("p", "muted small", "in the last year"));

  const scale = Math.max(...PERIODS.map(([key]) => Number(row[key]) || 0));
  const scaleLine = scale > 0 ? `A full bar is ${fmtInt(scale)}.` : "Nothing in the last month.";

  // THE LABEL IS THE WHOLE TILE, because it replaces the whole tile.
  //
  // An aria-label on the <a> is the accessible name of everything inside it:
  // whatever it does not say is not there for anybody listening. Naming
  // two of the five numbers - the year and the day - would leave the four
  // bars that are the point of the tile, and the scale that makes them
  // quantities, simply missing. A picture with a text alternative that
  // describes a different picture is worse than none.
  //
  // So it is the tile read out, in the tile's own order and its own words,
  // and it is built from the same values the elements below are built from.
  t.setAttribute("aria-label", [
    `${label}: ${fmtInt(row.year)} in the last year.`,
    sentence(PERIODS.map(([key, text]) => `last ${text} ${fmtInt(Number(row[key]) || 0)}`).join(", ")),
    scaleLine,
    "Show the diagrams.",
  ].join(" "));

  const dl = el("dl", "stat-periods");
  PERIODS.forEach(([key, text]) => {
    const value = Number(row[key]) || 0;
    const wrap = el("div");
    wrap.appendChild(el("dt", "k", text));
    // The bar lives inside the <dd>: a <dl> group takes dt then dd and
    // nothing else, and the bar is a picture of exactly this value.
    const dd = el("dd", "v");
    dd.appendChild(periodBar(value, scale));
    dd.appendChild(el("span", "n", fmtInt(value)));
    wrap.appendChild(dd);
    dl.appendChild(wrap);
  });
  t.appendChild(dl);
  // THE SCALE, SAID OUT LOUD. The four bars are drawn against this tile's
  // own largest value, and a bar whose scale is not stated is a picture
  // rather than a number: "1 in the last month" fills its bar exactly as far
  // as "47" does on the tile beside it, and the two tiles then say the same
  // thing in the channel the eye reads first. Six plain words say what full
  // height means here, which is what makes the four lengths quantities
  // again - and short enough to stay on one line in the narrowest tile.
  t.appendChild(el("p", "stat-scale", scaleLine));
  t.appendChild(el("p", "stat-go", "Show the diagrams \u2192"));
  return t;
}

/* TWO ROWS, THE SAME NUMBER OF TILES IN EACH.
 *
 * The grid was `auto-fit`, which fills each row with as many tiles as fit and
 * leaves whatever is over on the last one: eight tiles came out 3 - 3 - 2 at
 * one width and 5 - 3 at another, and the block changed shape as the window
 * moved. Nothing on the page means anything by that - the tiles are eight
 * equal things - so the shape was noise the reader had to look past.
 *
 * Two rows, ceil(n / 2) per row. An odd count leaves one gap, and the gap is
 * a real cell rather than a stretched last tile, so the tiles above and below
 * it stay the same width as all the others. It is aria-hidden: there is
 * nothing in it, and a screen reader counting nine tiles where there are
 * eight would be worse than the alignment is good.
 *
 * The number of columns is a custom property because CSS cannot count
 * children; views.css uses it, and drops back to one column per tile on a
 * narrow screen where two rows of four would each be 90 px wide. */
function layOutTwoRows(box, count) {
  box.style.setProperty("--tiles-per-row", String(Math.max(1, Math.ceil(count / 2))));
  if (count % 2 === 0) return;
  const filler = el("div", "stat-tile stat-tile--empty");
  filler.setAttribute("aria-hidden", "true");
  box.appendChild(filler);
}

async function loadStats() {
  const box = document.getElementById("stats");
  if (!box) return;
  try {
    const data = await api("/api/dashboard/stats", { channel: "dashboard-stats", quiet: true });
    const rows = (data && data.tables) || [];
    box.textContent = "";
    box.setAttribute("aria-busy", "false");
    if (!rows.length) {
      box.appendChild(el("p", "empty", "Nothing archived for this project and language yet."));
      return;
    }
    rows.forEach((r) => box.appendChild(tile(r)));
    layOutTwoRows(box, rows.length);
    const meta = document.getElementById("stats-meta");
    if (meta && data.cached_at) meta.textContent = `counted ${fmtAgo(data.cached_at) || "just now"}`;
  } catch (err) {
    if (isAbort(err)) return;
    failed(box, "the counts", err);
  }
}

/* ── Activity feed ───────────────────────────────────────────────────── */

function activityItem(a) {
  const li = el("li", "list-item");

  const title = el("p", "list-title");
  if (a.label) {
    title.appendChild(el("span", "tag", a.label));
    title.appendChild(document.createTextNode(" "));
  }
  const name = a.name || "(unnamed)";
  if (a.link) {
    const anchor = el("a", null, name);
    anchor.href = a.link;   // the server already put the pair in it
    title.appendChild(anchor);
  } else {
    title.appendChild(document.createTextNode(name));
  }
  li.appendChild(title);

  const when = el("time", "list-time", fmtAgo(a.date_commissioned));
  when.dateTime = a.date_commissioned || "";
  when.title = fmtDate(a.date_commissioned);
  li.appendChild(when);

  if (a.detail) li.appendChild(el("p", "list-sub", a.detail));
  if (a.about && a.about.length) {
    const about = el("p", "list-about");
    about.appendChild(el("span", "visually-hidden", "About: "));
    a.about.forEach((who, i) => {
      if (i) about.appendChild(document.createTextNode(" → "));
      about.appendChild(link(`/diagrams/entity?q=${encodeURIComponent(who)}`, who, "chip"));
    });
    li.appendChild(about);
  }
  return li;
}

async function loadActivities() {
  const list = document.getElementById("activities");
  if (!list) return;
  try {
    const data = await api("/api/dashboard/activities", { channel: "dashboard-activities", quiet: true });
    const items = (data && data.items) || [];
    list.textContent = "";
    list.setAttribute("aria-busy", "false");
    if (!items.length) {
      list.appendChild(el("li", "empty", "No rows archived yet."));
      return;
    }
    items.forEach((a) => list.appendChild(activityItem(a)));
    noteDerivedNames(list, items, (a) => a);
  } catch (err) {
    if (isAbort(err)) return;
    failed(list, "the activity list", err);
  }
}

/* ── Latest events ───────────────────────────────────────────────────── */

function eventItem(ev) {
  const li = el("li", "list-item");

  const title = el("p", "list-title");
  if (ev.type) {
    const tag = link(`/events?by=type&q=${encodeURIComponent(ev.type)}`, ev.type, "tag");
    tag.title = `Every event of type ${ev.type}`;
    title.appendChild(tag);
    title.appendChild(document.createTextNode(" "));
  }
  title.appendChild(document.createTextNode(ev.name || ev.description || "(no description)"));
  li.appendChild(title);

  // An event dated in the future - an announced launch - is normal here, so
  // the date is shown as a date and not as "in 3 weeks".
  const when = el("time", "list-time", fmtDate(ev.date, { time: false }));
  when.dateTime = ev.date || "";
  if (!ev.dated) when.title = "The event carries no date; this is when the document was commissioned.";
  li.appendChild(when);

  if (ev.description && ev.description !== ev.name) li.appendChild(el("p", "list-sub", ev.description));

  if (ev.entities && ev.entities.length) {
    const about = el("p", "list-about");
    about.appendChild(el("span", "visually-hidden", "About: "));
    ev.entities.forEach((e) => {
      const chip = link(`/diagrams/entity?q=${encodeURIComponent(e.name || "")}`, e.name || e.id, "chip");
      chip.title = e.type ? `${e.name} (${e.type}) - show the diagrams` : "Show the diagrams";
      about.appendChild(chip);
    });
    li.appendChild(about);
  }

  // WHERE IT CAME FROM, AS WORDS. The archive's own `text_name` is
  // `src_1127664` or an md5 in every row it has, so the server sends the
  // domain and the address's own slug instead (app/source_names.py) and
  // this reads whatever it was given. The id reaches nobody.
  if (ev.source && (ev.source.uri || ev.source.name)) {
    const src = el("p", "list-sub event-source");
    src.appendChild(document.createTextNode("Source: "));
    const text = ev.source.name || ev.source.uri;
    // Only a real web address becomes a link: an archive can hold sources
    // like `src:city-archive-foia-2006`, and an anchor whose scheme no
    // browser knows is a link that looks live and does nothing.
    if (isWebAddress(ev.source.uri)) {
      const a = el("a", null, text);
      a.href = ev.source.uri;
      a.rel = "noopener noreferrer";
      a.target = "_blank";
      src.appendChild(a);
    } else {
      src.appendChild(document.createTextNode(text));
    }
    li.appendChild(src);
  }
  return li;
}

/* http and https and nothing else - see the note above. */
function isWebAddress(uri) {
  return /^https?:\/\//i.test(String(uri || ""));
}

/* One line under a list where any name was worked out from an address
 * rather than read from the archive, so a derivation is never mistaken for
 * something the document called itself. Once per list, not once per row:
 * twenty repetitions of the same caveat is noise, and this is a fact about
 * the list. */
function noteDerivedNames(list, rows, getSource) {
  const holder = list && list.parentElement;
  if (!holder) return;
  const existing = holder.querySelector("[data-derived-note]");
  const any = (rows || []).some((r) => {
    const s = getSource(r);
    return s && s.derived;
  });
  if (!any) {
    if (existing) existing.remove();
    return;
  }
  const note = existing || el("p", "card-meta");
  note.dataset.derivedNote = "";
  note.textContent = "Document names in this list were read from their web address.";
  if (!existing) holder.appendChild(note);
}

async function loadEvents() {
  const list = document.getElementById("latest-events");
  if (!list) return;
  try {
    const data = await api("/api/dashboard/latest-events", { channel: "dashboard-events", quiet: true });
    const events = (data && data.events) || [];
    list.textContent = "";
    list.setAttribute("aria-busy", "false");
    if (!events.length) {
      list.appendChild(el("li", "empty", "No events archived yet."));
      return;
    }
    events.forEach((ev) => list.appendChild(eventItem(ev)));
    noteDerivedNames(list, events, (ev) => ev.source);
  } catch (err) {
    if (isAbort(err)) return;
    failed(list, "the latest events", err);
  }
}

/* The one link the server rendered without the pair. */
const allEvents = document.getElementById("all-events-link");
if (allEvents) allEvents.href = withContext("/events");

loadStats();
loadActivities();
loadEvents();


/* ── Importances by perspective ───────────────────────────────────────────
 *
 * For each perspective a customer watches, how many judgements of each KIND
 * the archive holds: Critical, High, Medium, Low, Not Important. One bar per
 * perspective, stacked by level - the height says how much was judged for
 * that reader, the stack says how much of it mattered.
 *
 * Two periods, because they answer two questions: the last 24 hours is what
 * the collector has just done, the last 7 days is whether that is a run or a
 * week.
 *
 * VIOLET, NEVER RED AND GREEN. Importance is how much something matters, not
 * whether it is good, so the levels are drawn on the sequential relevance
 * ramp (static/js/palette.js) - the same colours every importance chart in
 * the product uses.
 *
 * Chart.js is loaded with `defer`, so it may not be there when this module
 * runs: the drawing waits for it rather than racing it, and a browser where
 * it never arrives gets the numbers as text instead of an empty box.
 */
const DASH_CHARTS = [...document.querySelectorAll("[data-importances]")];

function whenChartReady() {
  if (window.Chart) return Promise.resolve(window.Chart);
  return new Promise((resolve) => {
    const tick = () => (window.Chart ? resolve(window.Chart) : setTimeout(tick, 50));
    setTimeout(tick, 50);
  });
}

/* ── The rows behind one segment ──────────────────────────────────────────
 *
 * THE SAME DIALOG THE DIAGRAMS CHARTS OPEN, from the same module: a bar here
 * and a bar there are both "some rows in the archive", and a reader who has
 * learnt one listing should not have to learn a second.
 *
 * A click carries two things - which PERSPECTIVE and which LEVEL - because
 * the bars are stacked and the segment under the pointer is one level of one
 * perspective. Chart.js's own hit test decides which; a click that lands on
 * no segment is not a question anybody asked.
 *
 * No Download CSV, where the Diagrams drilldown offers one: a segment of a
 * whole-project bar is hundreds of rows and each carries a document lookup
 * that costs a fifth of a second (routers/api_dashboard.py says why). The
 * dialog pages through them instead of building a file nobody can wait for.
 */
function openImportance(card, data, index, seriesIndex) {
  const grain = card.dataset.importances;
  const perspective = (data.labels || [])[index];
  const set = (data.datasets || [])[seriesIndex];
  if (!perspective || !set) return;
  const value = (set.data || [])[index] || 0;
  openDrilldown({
    title: `${set.label} - ${perspective}`,
    subtitle: `${rowsWord(value)} - ${data.caption}`,
    rowsLabel: `Rows behind ${set.label} for ${perspective}`,
    emptyText: "No rows behind this bar any more - the archive may have changed.",
    // The second link of every row: that domain's own diagrams. There is no
    // entity link - an importance judgement is about a document.
    links: { source: (domain) => withContext(
      `/diagrams/source?q=${encodeURIComponent(domain)}&axis=object&tab=sources`) },
    load: (page) => api("/api/dashboard/importances/drilldown", {
      channel: "importance-drilldown",
      body: { grain, perspective, level: set.id, ddpage: page },
    }),
  });
}

function rowsWord(count) {
  const n = Number(count) || 0;
  return `${fmtInt(n)} ${n === 1 ? "row" : "rows"}`;
}

/* The colour of one importance level: its step on the archive's own scale,
 * or - for a level the vocabulary has never heard of - a plain count colour,
 * so an unexpected word is drawn rather than dropped. */
function levelColour(set, index, of) {
  return set.step === null || set.step === undefined
    ? countColour(index, of)
    : relevanceColour(set.step);
}

/* HOW TALL THE PICTURE IS, from how many bars there are.
 *
 * A horizontal bar chart has one ROW per category, so its height is data and
 * not a constant: three perspectives need three rows, and a project that
 * watches nine needs nine. The outer box is sized here and the canvas fills
 * it (css: .dash-chart reads --chart-h), which is also what the empty state
 * measures itself against - a card with nothing to draw stands exactly as
 * tall as one that drew.
 *
 * The same two numbers the Diagrams charts use for an enlarged plot
 * (static/js/charts.js: AXIS_ROOM and ENLARGED_ROW), so a bar here is the
 * weight of a bar there. */
const AXIS_ROOM = 64;      // the value axis and the padding under it
const BAR_ROW = 52;        // one perspective
const LEGEND_ROW = 26;     // one line of the legend, which Chart.js draws
const LEGEND_PER_ROW = 4;  // how many keys fit on a line at this width

/* Chart.js draws its legend INSIDE the canvas, so a box sized for the bars
 * alone gives the bars whatever the legend leaves. The legend's own room is
 * added here instead, and the bars get all of theirs. */
function chartHeight(rows, keys) {
  const legend = Math.ceil(Math.max(1, keys) / LEGEND_PER_ROW) * LEGEND_ROW;
  return AXIS_ROOM + Math.max(1, rows) * BAR_ROW + legend;
}

function drawImportances(card, data, Chart) {
  const canvas = card.querySelector("[data-imp-canvas]");
  const box = canvas.closest(".chart-canvas");
  const status = card.querySelector("[data-imp-status]");
  const meta = card.querySelector("[data-imp-meta]");
  const purpose = card.querySelector("[data-imp-purpose]");
  const emptyBox = card.querySelector("[data-imp-empty]");
  const emptyText = card.querySelector("[data-imp-empty-text]");
  const fold = card.querySelector("[data-imp-fold]");
  if (purpose) {
    // The invitation only where there is something to accept: "click a bar"
    // over a card with no bars is an instruction the reader cannot follow.
    purpose.textContent = data.total
      ? `${data.description} Click a bar for the documents behind it.`
      : data.description;
  }
  if (meta) meta.textContent = `${fmtInt(data.total)} judgements - ${data.caption}`;
  if (!data.total) {
    // NOTHING TO DRAW: no plot, no fold, and the answer in the middle of
    // where the picture would have been (app.css: .chart-empty). The fold
    // goes because it would open on an empty table, and the status line
    // stays empty because the block below already says it - twice is two
    // problems.
    status.textContent = "";
    box.hidden = true;
    if (card._chart) { card._chart.destroy(); card._chart = null; }
    if (fold) { fold.open = false; fold.hidden = true; }
    if (emptyText) emptyText.textContent = "Nothing was judged in this period.";
    if (emptyBox) emptyBox.hidden = false;
    return;
  }
  status.textContent = "";
  box.hidden = false;
  if (emptyBox) emptyBox.hidden = true;
  if (fold) fold.hidden = false;
  // The box, then the canvas inside it: Chart.js fills its parent, so the
  // parent is the one thing that has to be told how tall the answer is.
  card.style.setProperty("--chart-h",
    `${chartHeight((data.labels || []).length, (data.datasets || []).length)}px`);
  const sets = (data.datasets || []).map((one, i) => ({
    label: one.label,
    data: one.data,
    backgroundColor: levelColour(one, i, data.datasets.length),
    borderWidth: 0,
  }));
  if (card._chart) card._chart.destroy();
  card._chart = new Chart(canvas.getContext("2d"), {
    type: "bar",
    data: { labels: data.labels, datasets: sets },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      // HORIZONTAL, as every "by category" chart in the product is
      // (static/js/charts.js: a key chart is indexAxis "y"). A perspective
      // is a NAME, and a name written along the bottom of a chart is either
      // rotated or dropped; beside its own bar it is simply read.
      indexAxis: "y",
      // STACKED, because the question is "how much was judged for this
      // reader, and how much of it mattered" - and five levels side by side
      // in every perspective would make the total, which is the first thing
      // a reader takes off a bar, something they have to add up themselves.
      scales: { x: { stacked: true, beginAtZero: true, ticks: { precision: 0 } },
                y: { stacked: true, ticks: { autoSkip: false } } },
      plugins: { legend: { position: "bottom" } },
      // WHAT A CLICK MEANS, and what the pointer says before it happens: a
      // picture that answers a click without ever looking clickable is one
      // nobody clicks.
      onHover: (event, hit) => {
        const target = event.native && event.native.target;
        if (target) target.style.cursor = hit.length ? "pointer" : "default";
      },
      onClick: (event, hit) => {
        if (!hit.length) return;
        openImportance(card, data, hit[0].index, hit[0].datasetIndex);
      },
    },
  });
  // AND WITHOUT A MOUSE. Chart.js draws on a canvas, which has no elements
  // to tab to, so the numbers behind the picture are the keyboard's way in -
  // the same answer the Diagrams charts give (static/js/charts.js).
  drawImportanceNumbers(card, data);
}

/* One row per perspective, one cell per level, every cell a button into the
 * same dialog the bars open. Folded away under the chart, so it costs no
 * height until somebody wants it. */
function drawImportanceNumbers(card, data) {
  const box = card.querySelector("[data-imp-numbers]");
  if (!box) return;
  box.textContent = "";
  const table = el("table", "table");
  table.appendChild(el("caption", null, `${data.label}: judgements per perspective and level`));
  const head = el("thead");
  const headRow = el("tr");
  headRow.appendChild(el("th", null, "Perspective"));
  (data.datasets || []).forEach((set) => headRow.appendChild(el("th", null, set.label)));
  head.appendChild(headRow);
  table.appendChild(head);
  const body = el("tbody");
  (data.labels || []).forEach((perspective, index) => {
    const tr = el("tr");
    const th = el("th", null, perspective);
    th.scope = "row";
    tr.appendChild(th);
    (data.datasets || []).forEach((set, seriesIndex) => {
      const td = el("td");
      const value = (set.data || [])[index] || 0;
      if (value) {
        const button = el("button", "cell-button", fmtInt(value));
        button.type = "button";
        button.setAttribute("aria-label",
          `${value} ${set.label} for ${perspective} - open the rows`);
        button.addEventListener("click", () => openImportance(card, data, index, seriesIndex));
        td.appendChild(button);
      } else {
        td.textContent = "0";
      }
      tr.appendChild(td);
    });
    body.appendChild(tr);
  });
  table.appendChild(body);
  const wrap = el("div", "table-scroll");
  wrap.appendChild(table);
  box.appendChild(wrap);
}

async function loadImportances() {
  if (!DASH_CHARTS.length) return;
  const Chart = await whenChartReady();
  await Promise.all(DASH_CHARTS.map(async (card) => {
    const status = card.querySelector("[data-imp-status]");
    try {
      const data = await api("/api/dashboard/importances", {
        channel: `importances-${card.dataset.importances}`,
        params: { grain: card.dataset.importances },
      });
      drawImportances(card, data, Chart);
    } catch (err) {
      if (isAbort(err)) return;
      status.textContent = "These numbers could not be counted.";
    }
  }));
}

loadImportances();
