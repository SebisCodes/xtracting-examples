/* ==========================================================================
 *  Drawing one chart from /api/diagrams, and everything that goes with it.
 *
 *  A chart in this dashboard is never just a canvas. A canvas cannot be
 *  read by a screen reader, cannot be reached by the Tab key and cannot be
 *  searched, so every chart here is three things that say the same:
 *
 *    the picture      <canvas role="img"> with an aria-label that names the
 *                     chart, its unit and its largest value;
 *    the numbers      a table under the picture, in a <details> that anyone
 *                     can open - and whose value cells are BUTTONS, so the
 *                     drilldown is reachable without a mouse - nothing in
 *                     this product may be hover-only or click-only;
 *    the legend       an HTML list (legend.js), not the canvas-drawn one,
 *                     with the same toggling by keyboard as by mouse.
 *
 *  Three kinds, from the registry (app/charts/__init__.py):
 *
 *    time    bars, one group per period of the timeframe
 *    key     horizontal bars, top 25, the longest first
 *    matrix  bubbles on two category axes
 *
 *  Magnitude is always a bar, never an area or a line: the length of a
 *  bar from a common baseline is the one encoding people read correctly.
 *
 *  MORE THAN TWO SERIES ARE STACKED, not drawn side by side. Eight
 *  sentiments next to each other over twelve months leave 3 px per bar:
 *  nobody can tell the series apart, and nobody with an unsteady hand can
 *  hit one (WCAG 2.5.8 asks 24 px of a target). Stacked, there is one bar
 *  per period - wide enough to read and to click - and the total is
 *  readable too. Only charts whose series PARTITION the rows may be
 *  stacked; the registry says which (ChartSpec.stacked), because "of those,
 *  high relevance" and "minimum, average, maximum" would otherwise be added
 *  up into a total that does not exist.
 *
 *  A point carries its own KEY (`x`), which is not always what the axis
 *  shows: "entities by name" is keyed on the entity id and labelled with
 *  the name, because names are translated and ids are not. onPick hands
 *  back the key, which is what the drilldown needs.
 * ========================================================================== */

import { renderLegend } from "./legend.js";
import { announce, toast } from "./a11y.js";
import { openDialog } from "./dialog.js";
import { count as countColour, textOn, valence } from "./palette.js";

/* What a card says when the window it was asked about holds nothing.
 *
 * Two words, because there can be eight of these cards on one screen: the
 * advice ("try a longer period, or step back with Earlier") belongs to the
 * PAGE and is given once above the grid (static/js/diagrams.js), not eight
 * times down it. The card that has nothing to draw also hides its plot box
 * (renderChart), so it collapses to title, purpose and this line instead of
 * reserving 272 px of white to say nothing in. */
export const EMPTY_PERIOD = "No rows.";


/* THE TEXT ON A CHART IS PAGE TEXT.
 *
 * Chart.js draws every tick, every axis title and every tooltip at 12 px by
 * default, which would be the only text on the dashboard under 16 px: the axis
 * of "Ratings per period" is read the same way as the sentence above it, by
 * the same eyes, and a reader who needs 16 px to read the page needs it to
 * read the numbers under the bars too. Set once, on the library, so a
 * chart that declares no font of its own inherits it - and so nobody has to
 * remember to repeat it per scale.
 *
 * The face is the page's own stack (app.css), because Chart.js otherwise
 * renders in Helvetica/Arial while everything around it is system-ui: two
 * typefaces on one card read as two documents. */
const CHART_FONT = 16;
const CHART_FAMILY = 'system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif';

/* EVERY COLOUR ON A CHART COMES OUT OF THE STYLESHEET.
 *
 * The bars arrive with their colour already decided by the server, which
 * reads the same tokens (app/charts/__init__.py reads app.css). What is
 * left here is the FURNITURE - the tick text, the grid, the tooltip, the
 * hairline between two stacked segments - and none of it is a hex typed
 * into this file. A hex in a view is a second copy of the design system,
 * and a second copy is how one meaning ends up two colours.
 *
 * token() is read once, lazily: getComputedStyle flushes layout, and this
 * runs before the first chart rather than per bar. */
function token(name, fallback) {
  try {
    const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    if (value) return value;
  } catch (e) { /* no stylesheet (a stripped page) - fall through */ }
  return fallback;
}

/* The page's own text colour, which is what the ticks and the axis titles
 * are: the numbers under a bar are read by the same eyes as the sentence
 * above it. */
const TICK = token("--text", "#1a1a1a");
const SURFACE = token("--white", "#ffffff");

/* THE GRID IS THE ONE THING THAT IS DERIVED RATHER THAN TAKEN.
 *
 * --line is 4.54:1 on white, which is right for a border that separates two
 * cards and far too heavy behind a bar chart - a grid at that strength
 * competes with the data it is there to help read. So the line token is
 * taken and softened to a wash of itself. It is still ONE colour changed in
 * ONE place; it is simply not drawn at full strength. WCAG 1.4.11 does not
 * ask 3:1 of it: a gridline carries no information that is not also in the
 * axis labels beside it.
 */
function withAlpha(colour, alpha) {
  const hex = String(colour).trim();
  const m = /^#([0-9a-f]{3}|[0-9a-f]{6})$/i.exec(hex);
  if (!m) return hex;
  const full = m[1].length === 3 ? m[1].split("").map((c) => c + c).join("") : m[1];
  const [r, g, b] = [0, 2, 4].map((i) => parseInt(full.slice(i, i + 2), 16));
  return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}
const GRID = withAlpha(token("--line", "#767676"), 0.35);

if (typeof window.Chart === "function") {
  window.Chart.defaults.font.size = CHART_FONT;
  window.Chart.defaults.font.family = CHART_FAMILY;
  window.Chart.defaults.color = TICK;
}

/* Every chart on the page, so print and a language change can reach them. */
const live = new Set();

function el(tag, className, text) {
  const e = document.createElement(tag);
  if (className) e.className = className;
  if (text !== undefined && text !== null) e.textContent = text;
  return e;
}

export function chartLibraryPresent() {
  return typeof window.Chart === "function";
}

/* "1 row", not "1 rows". A number and a noun that disagree read as a bug in
 * the page, and this one sits on every card. */
export function rowsWord(count) {
  return `${fmt(count)} ${Number(count) === 1 ? "row" : "rows"}`;
}

function fmt(value) {
  if (value === null || value === undefined) return "0";
  const n = Number(value);
  if (!Number.isFinite(n)) return String(value);
  // Averages and minima can be fractional; counts never are, and a count
  // written as "5.0" reads like a measurement.
  return Number.isInteger(n) ? n.toLocaleString() : n.toLocaleString(undefined, { maximumFractionDigits: 2 });
}

/* ── What a screen reader hears instead of the picture ─────────────────── */

/* The two axis names of a matrix, or null for a chart that has none. The
 * builder supplies them (app/charts/*.py) because the same chart can be
 * about different pairs; the numbers table, the axis titles and the
 * aria-label all read them from here. */
export function matrixAxes(chart) {
  if (chart.kind !== "matrix") return null;
  const axes = chart.axes || [];
  return axes[0] && axes[1] ? [axes[0], axes[1]] : null;
}

/* `visible` is the set of dataset ids currently drawn; leaving it out
 * describes all of them. Hiding a series with the legend has to change this
 * text as well as the picture - a label that still describes a series
 * nobody can see is simply wrong, and the label is all a screen reader
 * has. */
export function describeChart(chart, visible) {
  const kind = { time: "Bar chart over time", key: "Horizontal bar chart", matrix: "Matrix of cells" }[chart.kind]
    || "Chart";
  const parts = [`${kind}: ${chart.title}.`];
  if (chart.description) parts.push(chart.description);
  if (!chart.total) {
    parts.push("Nothing in this period.");
    return parts.join(" ");
  }
  // Which way round the two axes are. On the sentiment matrix both carry
  // the SAME eight words, so a label that does not say which is which
  // describes a picture that cannot be read - and the label is all a screen
  // reader has.
  const axes = matrixAxes(chart);
  if (axes) parts.push(`Across: ${axes[0]}. Up: ${axes[1]}.`);
  const shown = visible ? chart.datasets.filter((ds) => visible.has(ds.id)) : chart.datasets;
  if (visible && shown.length !== chart.datasets.length) {
    parts.push(shown.length
      ? `${shown.length} of ${chart.datasets.length} series shown.`
      : "Every series is hidden.");
  }
  if (stacksSeries(chart) && shown.length > 1) parts.push("The series are stacked.");
  shown.forEach((ds) => {
    const points = ds.data || [];
    const value = (p) => (chart.kind === "matrix" ? p.v : p.y);
    const total = points.reduce((sum, p) => sum + (Number(value(p)) || 0), 0);
    const top = points.reduce((best, p) => (best && Number(value(best)) >= Number(value(p)) ? best : p), null);
    const where = top ? (chart.kind === "matrix" ? `${top.x_label} and ${top.y_label}` : (top.label || top.x)) : "";
    parts.push(`${ds.label}: ${fmt(total)} in total, highest ${fmt(top ? value(top) : 0)} at ${where}.`);
  });
  return parts.join(" ");
}

/* ── The numbers, as a table you can operate ───────────────────────────── */

function dataTable(chart, onPick) {
  const wrap = el("details", "chart-table");
  // THE SUMMARY STAYS IN THE DOM AND OUT OF THE PICTURE. Nothing on the card
  // opens this - the enlarged view carries the same table, open,
  // under the bigger picture - but print.css opens every one of them for
  // paper, and it does that by setting `open`, so the element has to still
  // be a <details>. Hidden rather than removed: `hidden` takes the summary
  // out of the tab order, so the card has no dead control on it.
  const summary = el("summary", null, "Numbers behind this chart");
  summary.hidden = true;
  wrap.appendChild(summary);
  const scroll = el("div", "table-scroll");
  // print.css knows this class and prints the table full width.
  const table = el("table", "table chart-data-table");
  const caption = el("caption", null, chart.description || chart.title);
  table.appendChild(caption);

  const head = el("thead");
  const headRow = el("tr");
  const thead = (text) => { const th = el("th", null, text); th.scope = "col"; headRow.appendChild(th); };
  const body = el("tbody");

  const cell = (point, dataset, where) => {
    const td = el("td", "num");
    const value = chart.kind === "matrix" ? point.v : point.y;
    if (!value) {
      td.textContent = fmt(0);
      return td;
    }
    // A cell with a number in it is the keyboard's way into the drilldown.
    const button = el("button", "cell-button", fmt(value));
    button.type = "button";
    button.addEventListener("click", () => onPick(dataset, point));
    // On the LABEL, not on a title: a tooltip only exists while a mouse
    // hovers, so a keyboard user hears "1, button" and a screen-reader user
    // is told nothing about what Enter would do. Nothing is hover-only.
    const named = chart.datasets.length > 1 ? `${where}, ${dataset.label}` : where;
    button.setAttribute("aria-label", `Show the ${rowsWord(value)} behind ${named}`);
    td.appendChild(button);
    return td;
  };

  if (chart.kind === "matrix") {
    // The axes carry their own names (the builder's, app/charts/*.py), so
    // this table says which column is the short-term value and which the
    // long-term one instead of "First axis" and "Second axis". The third
    // column is what is being counted, by its own name.
    const axes = matrixAxes(chart) || ["First axis", "Second axis"];
    thead(axes[0]); thead(axes[1]);
    thead(chart.datasets[0] ? chart.datasets[0].label : "Rows");
    (chart.datasets[0] ? chart.datasets[0].data : []).forEach((p) => {
      const tr = el("tr");
      const th = el("th", null, p.x_label || p.x); th.scope = "row";
      tr.appendChild(th);
      tr.appendChild(el("td", null, p.y_label || p.y));
      tr.appendChild(cell(p, chart.datasets[0], `${p.x_label || p.x} and ${p.y_label || p.y}`));
      body.appendChild(tr);
    });
  } else {
    thead(chart.kind === "time" ? "Period" : "Category");
    chart.datasets.forEach((ds) => thead(ds.label));
    const rows = (chart.datasets[0] ? chart.datasets[0].data : []).length;
    for (let i = 0; i < rows; i += 1) {
      const tr = el("tr");
      const first = chart.datasets[0].data[i];
      const where = first.label || first.x;
      const th = el("th", null, where); th.scope = "row";
      tr.appendChild(th);
      chart.datasets.forEach((ds) => tr.appendChild(cell(ds.data[i], ds, where)));
      body.appendChild(tr);
    }
  }
  head.appendChild(headRow);
  table.appendChild(head);
  table.appendChild(body);
  scroll.appendChild(table);
  wrap.appendChild(scroll);
  return wrap;
}

/* ── Chart.js configuration ────────────────────────────────────────────── */

/* A chart with one category would otherwise draw a bar as tall as the card,
 * which reads as a block of colour rather than as a measurement. */
const MAX_BAR = 48;

/* ── LABELS NEVER OVERLAP, AND THE RULE IS MEASURED IN PIXELS ─────────────
 *
 *  The defect this answers: on a chart like "Entities by name" the category
 *  labels ran into each other until the axis was a smear of overlapping
 *  words. That must not happen at any width.
 *
 *  The rule is one line, and everything else follows from measuring, so it
 *  keeps working when the window changes:
 *
 *      A LABEL IS DRAWN ONLY WHEN THE SPACE IT HAS IS AT LEAST ITS OWN
 *      LINE HEIGHT.
 *
 *  Below that the labels are not rotated, not shrunk and not thinned out -
 *  they are NOT DRAWN. Rotating is what produced the smear; shrinking would
 *  take chart text under the 16 px floor the rest of the page keeps;
 *  thinning a list of names hides half of them with nothing saying which
 *  half. The card says how many categories there are and where to see them
 *  ("43 entities - open the chart to see the names"), the enlarge popup
 *  draws every one of them at a size where they fit, and the tooltip and
 *  the numbers table under the card always carry the full text.
 *
 *  A TIME AXIS IS THE ONE THAT THINS INSTEAD. Its categories are periods in
 *  order, so every nth date still says what the axis is - which is exactly
 *  what a list of names cannot do.
 */

/* One line of the chart's own text. Chart.js draws ticks at CHART_FONT with
 * a line height of 1.2, so this is the height a label actually occupies. */
export const LABEL_LINE = Math.ceil(CHART_FONT * 1.2);

/* What one category is given in the ENLARGED chart. 32 px and not 20: at
 * the size where every label is shown, every bar is also a target, and WCAG
 * 2.5.8 asks 24 px of one. */
export const ENLARGED_ROW = 32;

/* The gap either side of a tick on a time axis, so two dates never touch. */
const TICK_GAP = 12;

/**
 * Whether a category axis has room for its labels.
 *
 *   space   the plot's size along the category axis, in pixels
 *   count   how many categories are on it
 *   line    the size of one label along that axis (its line height on a
 *           vertical axis, its width on a horizontal one)
 *
 * Nothing to draw fits; a plot nobody has measured yet (0 px, a card that
 * is still hidden) is treated as fitting, because hiding is the
 * destructive answer and this runs again after the layout.
 */
export function labelsFit(space, count, line = LABEL_LINE) {
  const n = Number(count) || 0;
  if (n <= 0) return true;
  const px = Number(space);
  if (!Number.isFinite(px) || px <= 0) return true;
  return px / n >= (Number(line) || LABEL_LINE);
}

/**
 * How many ticks a TIME axis may draw: its pixel width divided by the width
 * of its widest label. Chart.js is then told to skip the rest (autoSkip),
 * so a year of days shows every nth date instead of a black smear.
 */
export function maxTicksFor(space, widestLabel) {
  const px = Number(space);
  const widest = Math.max(1, Number(widestLabel) || 1);
  if (!Number.isFinite(px) || px <= 0) return 2;
  return Math.max(2, Math.floor(px / (widest + TICK_GAP)));
}

/* An address is longer than any axis. Cutting it here, with an ellipsis and
 * the whole text still in the tooltip and in the table below, is a
 * decision; letting Chart.js clip it against the edge of the canvas is an
 * accident that looks like a bug ("Nordwyk Pum").
 *
 * The budget is in PIXELS and it is the width the axis actually got, which
 * is only known after the first layout (applyLabelRule measures
 * chartArea.left and hands it back through the instance). Before that -
 * and on a canvas with no measuring context - a character count stands in. */
const MAX_TICK = 26;
/* The first pass in the ENLARGE popup, where there is room for a whole
 * name. It is what Chart.js measures the axis against, so it is also what
 * decides how wide the axis gets to be; the measured pass then trims to the
 * width it actually got. */
const MAX_TICK_ENLARGED = 60;

/* A LABEL MAY CARRY A SECOND LINE, AND THE SEPARATOR IS A NEWLINE.
 *
 * The entities band asks for two: the name, and under it the kind of thing
 * it is. One string joined with a separator would be a line that grows with
 * the type and is then cut at the axis - so the name, which is the thing
 * being read, would lose characters to a word that is not it.
 *
 * The builder sends "name\ntype"; the axis draws the name and the plugin
 * below draws the type under it, smaller and in italics. Everything else
 * about a label is unchanged, and a label with no newline in it has one
 * line exactly as before. */
export function labelLines(text) {
  return String(text === null || text === undefined ? "" : text).split("\n");
}

function shortTick(text, cap = MAX_TICK) {
  const value = String(text === null || text === undefined ? "" : text);
  return value.length > cap ? value.slice(0, cap - 1) + "…" : value;
}

/* The longest prefix of `text` that fits in `maxPx`, with an ellipsis. */
export function fitText(ctx, text, maxPx) {
  const value = String(text === null || text === undefined ? "" : text);
  if (!ctx || !(maxPx > 0)) return shortTick(value);
  if (ctx.measureText(value).width <= maxPx) return value;
  let low = 0;
  let high = value.length;
  while (low < high) {
    const mid = Math.ceil((low + high) / 2);
    if (ctx.measureText(value.slice(0, mid) + "…").width <= maxPx) low = mid;
    else high = mid - 1;
  }
  return low > 0 ? value.slice(0, low) + "…" : "…";
}

function tickLabel(cap = MAX_TICK) {
  return function label(value) {
    const raw = this.getLabelForValue ? this.getLabelForValue(value) : value;
    // TWO BUDGETS, BECAUSE THE TWO AXES HAVE DIFFERENT ROOM. A label on a
    // vertical axis has everything to the left of the plot; one on a
    // horizontal axis has ITS COLUMN and nothing more. Measuring the
    // second against the first is what let four names in the enlarged
    // matrix be drawn at 300 px each in columns 166 px apart.
    const chart = this.chart;
    const budget = chart && (this.axis === "x" ? chart.$tickWidthX : chart.$tickWidth);
    const first = labelLines(raw)[0];
    return budget ? fitText(this.ctx, first, budget) : shortTick(first, cap);
  };
}

/* The full text, whatever the axis had room for. A tooltip that repeated
 * the truncation would leave "Nordwyk Pum…" as the only reading of the row
 * anybody can get with a mouse. */
function fullLabelTooltip(chart) {
  return (items) => {
    const item = items && items[0];
    if (!item) return "";
    const dataset = chart.datasets[chart.kind === "matrix" ? 0 : item.datasetIndex];
    const point = dataset && dataset.data ? dataset.data[item.dataIndex] : null;
    if (!point) return item.label || "";
    if (chart.kind === "matrix") return `${point.x_label || point.x}, ${point.y_label || point.y}`;
    // An array is how Chart.js takes more than one line, and the second line
    // of a two-line label is part of the answer to "what is this bar".
    return labelLines(point.label || point.x || item.label || "");
  };
}

function baseOptions(chart) {
  return {
    responsive: true,
    maintainAspectRatio: false,
    // NO ANIMATION, AND THE REASON IS CORRECTNESS RATHER THAN TASTE.
    //
    // A 300 ms grow-in was pleasant and it silently broke the bars. Chart.js
    // ANIMATES the element properties, `base` among them, and the target it
    // animates towards is computed when the update runs. On a horizontal bar
    // chart the row names are dropped when they do not fit, which widens the
    // plot AFTER that computation - so the animation's target base is the
    // old left edge, and the animator then holds every bar there and
    // overwrites any later correction.
    //
    // Measured on a real archive at 1440 px, "Sources by domain": the x
    // scale said zero was at pixel 8, an update put the bars on 9, and the
    // running animation pulled them back to 185 - twenty bars beginning at
    // the value 8 on an axis drawn from 0. "Sources by type": 127 against 8.
    // It looks exactly like `beginAtZero` being off; beginAtZero is on and
    // was never the problem.
    //
    // Without the animation the bars are laid out once and drawn where they
    // are. A dashboard is read, not watched.
    animation: false,
    // The canvas has role="img" and its own label; Chart.js's own
    // description would be read twice.
    plugins: {
      legend: { display: false },
      tooltip: {
        backgroundColor: TICK,
        titleFont: { size: CHART_FONT },
        bodyFont: { size: CHART_FONT },
        // The WHOLE label, however little of it the axis had room for -
        // and the only reading of a hidden label a mouse can get at.
        callbacks: chart ? { title: fullLabelTooltip(chart) } : {},
      },
    },
    scales: {},
  };
}

/* THE COLOUR OF ONE BAR: its own, the step of a scale, or the series'.
 *
 * `colour` is a connection colour group, chosen by somebody on the Colours
 * page. `tone` is a step of the archive's own valence scale - the number
 * `rating_values.float_value` carries - and it is what makes "Ratings by
 * value" a distribution one can read at a glance: neutral in the middle,
 * green rising to Excellent, red falling to Egregious. Neither is set on
 * most charts, and those take the one colour of their series. */
function pointColours(dataset) {
  const points = dataset.data || [];
  const own = points.map((p) => {
    if (p.colour) return p.colour;
    if (p.tone === undefined || p.tone === null) return null;
    return valence(p.tone);
  }).filter(Boolean);
  return own.length === points.length && own.length ? own : dataset.colour;
}

/* Stack, or draw side by side?
 *
 * Only a chart whose series partition the rows may be stacked at all
 * (chart.stacked, from the registry), and it is worth doing from three
 * series up: two bars in a period are still wide enough to tell apart and
 * to hit, while three or more are not. */
export function stacksSeries(chart) {
  return Boolean(chart.stacked) && (chart.datasets || []).length > 2;
}

function barDatasets(chart, stacked) {
  return chart.datasets.map((ds) => ({
    label: ds.label,
    data: ds.data.map((p) => p.y),
    backgroundColor: pointColours(ds),
    // A hairline of the card's own colour between the segments of a stack,
    // so two neighbouring steps of one scale do not read as one block.
    borderColor: stacked ? SURFACE : pointColours(ds),
    borderWidth: stacked ? 1 : 0,
    maxBarThickness: MAX_BAR,
    // One bar per period may use the period: 0.8 x 0.9 of the slot leaves a
    // fifth of the width unused, which is a fifth off every target.
    categoryPercentage: stacked ? 0.9 : 0.8,
    barPercentage: stacked ? 0.95 : 0.9,
  }));
}

function timeConfig(chart) {
  const labels = chart.labels || [];
  const stacked = stacksSeries(chart);
  return {
    type: "bar",
    data: { labels, datasets: barDatasets(chart, stacked) },
    options: Object.assign(baseOptions(chart), {
      scales: {
        // autoSkip with a limit computed from the plot's own width after
        // the first layout (applyLabelRule). maxRotation 0 because a
        // rotated date is the smear this rule exists to prevent.
        /* UPRIGHT DATES WHERE THE CHART ASKS FOR THEM (`upright_dates`).
         *
         * A date axis thins its labels when they do not fit, which is right
         * for a chart read as a shape and wrong for one read period by
         * period: "which month was that" has no answer when that month is
         * one of the four that were dropped. Turned a quarter round, every
         * date is drawn - and autoSkip goes with it, because thinning is the
         * thing being replaced. */
        x: { stacked, grid: { display: false },
             ticks: chart.upright_dates
               ? { color: TICK, autoSkip: false, maxRotation: 90, minRotation: 90 }
               : { color: TICK, autoSkip: true, maxRotation: 0, autoSkipPadding: TICK_GAP } },
        y: { stacked, beginAtZero: true, grid: { color: GRID }, ticks: { color: TICK, precision: 0 } },
      },
    }),
  };
}

/* THE SECOND LINE OF A NAME, under it and in italics.
 *
 * Chart.js draws one tick with one font, so a label that is a name and a
 * type cannot be two sizes on one axis - it would be "Apple Inc. Company" in
 * one weight, which reads as a longer name. The axis draws the name and this
 * draws the rest: smaller, italic, and cut to the same width the axis gave
 * the line above it, so the two are the same block of text.
 *
 * It runs on any chart whose labels carry a newline and does nothing on the
 * rest, and it is silent whenever the label rule has turned the axis off -
 * a second line under labels that are not drawn is a column of floating
 * words.
 */
const SUB_LABELS = {
  id: "subLabels",
  afterDatasetsDraw(instance) {
    const scale = instance.scales ? instance.scales.y : null;
    const labels = instance.data ? instance.data.labels : null;
    if (!scale || !Array.isArray(labels)) return;
    if (scale.options && scale.options.ticks && scale.options.ticks.display === false) return;
    if (!labels.some((l) => String(l).includes("\n"))) return;
    const ctx = instance.ctx;
    const budget = Math.max(0, (instance.chartArea.left || 0) - 18);
    ctx.save();
    ctx.font = `italic ${CHART_FONT - 1}px ${CHART_FAMILY}`;
    ctx.fillStyle = TICK;
    ctx.textAlign = "right";
    ctx.textBaseline = "top";
    (scale.ticks || []).forEach((tick, i) => {
      const index = tick.value === undefined ? i : tick.value;
      const parts = labelLines(labels[index]);
      if (parts.length < 2 || !parts[1]) return;
      const y = scale.getPixelForTick(i);
      // The name is drawn centred on the tick, so the type starts just
      // below the bottom of it rather than at the tick itself.
      ctx.fillText(fitText(ctx, parts[1], budget), scale.right - 6,
                   y + Math.round(CHART_FONT * 0.55));
    });
    ctx.restore();
  },
};

/* HOW MUCH OF A HORIZONTAL CHART THE NAMES MAY HAVE.
 *
 * Chart.js gives a category axis whatever its widest label measures, and
 * then the layout takes it back: on a card 320 px wide the names came out at
 * "R…", "S…", "W…" - a column of first letters, which names nothing and is
 * worse than no labels at all (the rule above draws none when there is no
 * room, precisely so that this cannot happen).
 *
 * So the axis is given a floor and a ceiling of its own. The floor is enough
 * for about fifteen characters, which is a short company name; the ceiling
 * is 45% of the card, because the bars are the measurement and a chart whose
 * names have eaten the plot is a list with a stripe down one side. Between
 * the two it takes what it measures.
 *
 * Anything still too long is cut with an ellipsis and its whole text is in
 * the tooltip, in the enlarged view and in the numbers table - three places
 * where it is never cut. */
const LABEL_FLOOR = 120;
const LABEL_SHARE = 0.45;

function nameAxisFit(scale) {
  const width = scale.chart ? scale.chart.width : 0;
  if (!width) return;
  /* NO LABELS, NO GUTTER. Where the rule has taken the names off - too many
   * rows for the height - the floor below would reserve 120 px of nothing
   * down the left of the card, which is what a reader sees as a chart that
   * has slipped sideways. The bars start at the edge instead and run the
   * whole width, which is all there is to draw. */
  const ticks = scale.options && scale.options.ticks;
  if (ticks && ticks.display === false) {
    scale.width = 0;
    return;
  }
  const ceiling = Math.max(LABEL_FLOOR, Math.round(width * LABEL_SHARE));
  scale.width = Math.min(ceiling, Math.max(scale.width, Math.min(LABEL_FLOOR, ceiling)));
}

function keyConfig(chart, opts = {}) {
  const labels = chart.labels || [];
  const stacked = stacksSeries(chart);
  const cap = opts.enlarged ? MAX_TICK_ENLARGED : MAX_TICK;
  return {
    type: "bar",
    data: { labels, datasets: barDatasets(chart, stacked) },
    plugins: [SUB_LABELS],
    options: Object.assign(baseOptions(chart), {
      indexAxis: "y",
      scales: {
        x: { stacked, beginAtZero: true, grid: { color: GRID }, ticks: { color: TICK, precision: 0 } },
        // autoSkip stays OFF on a list of names: every nth name is not a
        // reading of anything. Either they all fit, or none is drawn
        // (applyLabelRule).
        y: { stacked, grid: { display: false }, afterFit: nameAxisFit,
             ticks: { color: TICK, autoSkip: false, callback: tickLabel(cap) } },
      },
    }),
  };
}

/* ── A MATRIX IS A FIELD OF CELLS, NOT A CLOUD OF BUBBLES ────────────────
 *
 * Not bubbles sized by area with the number printed on each one. Two things
 * are wrong with that. Area is the hardest encoding there is to read - the
 * difference between one insight and two is a few pixels of radius - so the
 * number has to be printed to make it legible at all, and forty printed
 * numbers on one card is a table drawn in the wrong shape.
 *
 * Every cell is the same size and the VALUE IS THE DEPTH OF THE COLOUR:
 * one step of a ramp from almost nothing to the full series colour. That is
 * what a reader of a matrix is actually asking - where is this dense - and
 * it answers it without a single digit on the picture. The number is on
 * hover, in the tooltip, and in the numbers table under the enlarged chart,
 * which is where a number belongs.
 *
 * The cells are sized from the axes rather than fixed: a matrix four across
 * in half a card and one eight across enlarged both want cells that touch
 * their neighbours without overlapping them.
 */
const MATRIX_CELLS = {
  id: "matrixCells",
  beforeDatasetsDraw(instance) {
    const x = instance.scales ? instance.scales.x : null;
    const y = instance.scales ? instance.scales.y : null;
    if (!x || !y) return;
    const cols = Math.max(1, (x.ticks || []).length);
    const rows = Math.max(1, (y.ticks || []).length);
    const r = Math.max(5, Math.min(x.width / cols, y.height / rows) * 0.42);
    const meta = instance.getDatasetMeta(0);
    (meta.data || []).forEach((element) => {
      if (element && element.options) element.options.radius = r;
    });
  },
};

/* A colour at a given strength, for the cells above.
 *
 * The ramp is alpha on ONE colour rather than a scale of several: a matrix
 * has no legend, and a reader who has to learn six colours to tell four from
 * five is being asked to read a key that is not on the card. Depth of one
 * colour needs no key at all.
 *
 * A floor of 0.12 keeps a cell with one row in it visible: "there is
 * something here" and "there is nothing here" are different answers, and the
 * second one is drawn as an empty cell. */
function atStrength(colour, share) {
  const hex = String(colour || "").trim();
  const alpha = Math.max(0.12, Math.min(1, share));
  const full = /^#([0-9a-f]{6})$/i.exec(hex);
  const short = /^#([0-9a-f]{3})$/i.exec(hex);
  let r, g, b;
  if (full) {
    r = parseInt(full[1].slice(0, 2), 16);
    g = parseInt(full[1].slice(2, 4), 16);
    b = parseInt(full[1].slice(4, 6), 16);
  } else if (short) {
    [r, g, b] = [...short[1]].map((c) => parseInt(c + c, 16));
  } else {
    return hex;      // a colour this does not understand is drawn as it is
  }
  return `rgba(${r}, ${g}, ${b}, ${alpha.toFixed(3)})`;
}

/* The categories of one axis: the declared scale when there is one (the
 * API sends it in x_order / y_order), else the order the rows arrived in.
 * A label the scale does not know is kept, at the end - dropping a category
 * would drop its bubbles with it. */
function axisCategories(declared, found) {
  const out = (declared || []).slice();
  found.forEach((label) => { if (!out.includes(label)) out.push(label); });
  return out;
}

function matrixConfig(chart, opts = {}) {
  const cap = opts.enlarged ? MAX_TICK_ENLARGED : MAX_TICK;
  const points = chart.datasets[0] ? chart.datasets[0].data : [];
  const xs = axisCategories(chart.x_order, points.map((p) => p.x_label || p.x));
  const ys = axisCategories(chart.y_order, points.map((p) => p.y_label || p.y));
  // A category axis draws its first label at the TOP. That is right for a
  // list ordered by size (the biggest counterpart first) and wrong for a
  // scale, which everybody reads as rising upwards - so a pinned scale is
  // turned over and agreement becomes the diagonal it is expected to be.
  if (chart.y_order && chart.y_order.length) ys.reverse();
  const biggest = points.reduce((m, p) => Math.max(m, Number(p.v) || 0), 1);
  // The axis names, ON the picture. Both axes of the sentiment matrix carry
  // the same eight words, so without the titles nothing visible says which
  // one is the short-term reading - and a matrix that cannot be read the
  // right way round is worse than no matrix.
  const axes = matrixAxes(chart) || ["", ""];
  const axisTitle = (text) => ({ display: Boolean(text), text,
                                 font: { size: CHART_FONT, weight: "600" }, color: TICK });
  return {
    type: "bubble",
    plugins: [MATRIX_CELLS],
    data: {
      datasets: [{
        label: chart.datasets[0] ? chart.datasets[0].label : "",
        data: points.map((p) => ({
          x: p.x_label || p.x,
          y: p.y_label || p.y,
          // The size is the cell's, set from the axes by MATRIX_CELLS; this
          // is only what Chart.js needs before the first layout.
          r: 8,
          v: p.v,
        })),
        // THE VALUE IS THE DEPTH OF THE COLOUR. Measured from zero against
        // the biggest cell, so the darkest cell on the card is the most
        // there is here and an empty one is left empty.
        backgroundColor: points.map((p) => atStrength(
          chart.datasets[0] ? chart.datasets[0].colour : countColour(),
          (Number(p.v) || 0) / biggest)),
        // A square rather than a circle: the picture is a field, and a grid
        // of circles leaves the gaps between them looking like data.
        pointStyle: "rect",
      }],
    },
    options: Object.assign(baseOptions(chart), {
      scales: {
        // offset keeps the bubbles inside the plot: without it the first
        // and the last category sit ON the axis and are drawn half outside
        // the canvas.
        /* THE COLUMN NAMES STAND UP.
         *
         * Both axes of a matrix are lists of names, and a name written
         * across a column that is 40 px wide is cut to four letters. Turned
         * a quarter of the way round they are limited by the HEIGHT under
         * the plot instead, which is room a card has and a column has not -
         * so "Slightly Negative" is written out rather than cut to "Sligh…".
         *
         * 90 degrees exactly, and never anything between: a label at 45 is
         * a diagonal smear, and the whole rule of this file is that a label
         * is either drawn properly or not drawn at all. */
        x: { type: "category", labels: xs, offset: true, grid: { color: GRID },
             title: axisTitle(axes[0]),
             ticks: { color: TICK, autoSkip: false, maxRotation: 90, minRotation: 90,
                      callback: tickLabel(cap) } },
        y: { type: "category", labels: ys, offset: true, grid: { color: GRID },
             title: axisTitle(axes[1]),
             ticks: { color: TICK, autoSkip: false, callback: tickLabel(cap) } },
      },
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: TICK,
          titleFont: { size: CHART_FONT },
          bodyFont: { size: CHART_FONT },
          callbacks: {
            // The full pair, whatever the two axes had room for.
            title: fullLabelTooltip(chart),
            // The title above already names the pair; repeating it here
            // would say the same thing twice in a two-line box.
            label: (item) => `${chart.datasets[0] ? chart.datasets[0].label : "Rows"}: ${fmt(item.raw.v)}`,
          },
        },
      },
    }),
  };
}

/* THE FULL TEXT OF THE TICK UNDER THE POINTER, or nothing.
 *
 * Category axes only: a value axis draws numbers, which are never cut. The
 * label column of a horizontal chart is everything left of the plot; the row
 * of names under a matrix is everything below it. Outside those, and over the
 * plot itself, this answers nothing - the bars have Chart.js's own tooltip,
 * which already carries the whole label.
 */
function tickUnder(instance, event) {
  if (!instance || !instance.chartArea) return "";
  const area = instance.chartArea;
  const box = instance.canvas.getBoundingClientRect();
  const px = event.clientX - box.left;
  const py = event.clientY - box.top;
  const labels = (instance.data && instance.data.labels) || [];

  const y = instance.scales ? instance.scales.y : null;
  if (y && y.type === "category" && px < area.left && py >= area.top && py <= area.bottom) {
    const index = Math.round(y.getValueForPixel(py));
    const value = labels.length ? labels[index] : (y.getLabelForValue
      ? y.getLabelForValue(index) : "");
    return value === undefined || value === null ? "" : String(value);
  }

  const x = instance.scales ? instance.scales.x : null;
  if (x && x.type === "category" && py > area.bottom && px >= area.left && px <= area.right) {
    const index = Math.round(x.getValueForPixel(px));
    const value = x.getLabelForValue ? x.getLabelForValue(index) : "";
    return value === undefined || value === null ? "" : String(value);
  }
  return "";
}

/* What a click on the canvas means.
 *
 * A bar is the primary way into the drilldown, and over thirty periods a
 * bar can be narrower than a finger - so a click that lands NEXT to a bar,
 * within a bar's width of it and inside the plot, counts as that bar. Each
 * bar's target grows to about its share of the period; nothing outside the
 * plot area, and no empty bar, opens anything.
 */
function hitFor(instance, chart, event) {
  const direct = instance.getElementsAtEventForMode(event, "nearest", { intersect: true }, true);
  if (direct.length) return direct[0];
  if (chart.kind === "matrix") return null;
  const area = instance.chartArea;
  const box = instance.canvas.getBoundingClientRect();
  const x = event.clientX - box.left;
  const y = event.clientY - box.top;
  if (x < area.left || x > area.right || y < area.top || y > area.bottom) return null;
  const near = instance.getElementsAtEventForMode(event, "nearest", { intersect: false }, true);
  if (!near.length) return null;
  const hit = near[0];
  const element = instance.getDatasetMeta(hit.datasetIndex).data[hit.index];
  if (!element) return null;
  const horizontal = chart.kind === "key";
  const distance = Math.abs((horizontal ? y : x) - (horizontal ? element.y : element.x));
  const thickness = Math.max(horizontal ? element.height : element.width, 12);
  return distance <= thickness * 1.5 ? hit : null;
}


/* WHAT A CLICK HERE WOULD OPEN, or nothing. The click handler and the
 * CURSOR read this one function, so the two cannot disagree - and that is
 * the whole point of it. A canvas styled `cursor: pointer` all over
 * promises a drilldown over the axis, over the gap between two bars and
 * over a bar whose value is zero, none of which opens anything; a canvas
 * with no cursor at all hides the one thing on the card that is clickable.
 *
 * Chart.js offers `onHover` with the elements it found, but its
 * interaction mode is not the rule below: `hitFor` also counts a click
 * that lands NEXT to a narrow bar, and a bar reachable by the mouse but
 * not by the cursor is the same lie the other way round. So the pointer
 * is set from the same answer the click uses. */
function pickFor(instance, chart, event) {
  const hit = hitFor(instance, chart, event);
  if (!hit) return null;
  const dataset = chart.datasets[chart.kind === "matrix" ? 0 : hit.datasetIndex];
  if (!dataset) return null;
  const point = dataset.data[hit.index];
  if (!point) return null;
  // An empty bar and an empty cell open nothing, so they are not pointers.
  return (chart.kind === "matrix" ? point.v : point.y) ? { dataset, point } : null;
}


/* ── Drawing a chart, and applying the label rule to what was drawn ────── */

/* The two axes of a matrix, in the order they are drawn. Everything that
 * has to size a matrix - the enlarge popup, the label rule - asks here, so
 * the width one of them computes and the count the other tests are always
 * about the same list. */
export function matrixLabels(chart) {
  const points = chart.datasets && chart.datasets[0] ? chart.datasets[0].data : [];
  return {
    x: axisCategories(chart.x_order, points.map((p) => p.x_label || p.x)),
    y: axisCategories(chart.y_order, points.map((p) => p.y_label || p.y)),
  };
}

/* How many categories a chart has along its category axis. */
export function categoryCount(chart) {
  if (chart.kind === "time") return (chart.labels || []).length;
  if (chart.kind !== "matrix") return (chart.labels || []).length;
  const axes = matrixLabels(chart);
  return Math.max(axes.x.length, axes.y.length);
}

/* THE WIDTH OF A PIECE OF CHART TEXT, BEFORE THERE IS A CHART.
 *
 * enlargedSize() has to know how wide a name is in order to decide how wide
 * a column has to be, and it runs before the canvas it is sizing exists.
 * One offscreen 2d context, kept, measures in the chart's own font - the
 * same measurement Chart.js will make when it lays the axis out. Where
 * there is no canvas at all (a stripped page, a test harness without one)
 * an average character width stands in, which is never worse than the
 * fixed number this replaced. */
let measurer;                      // undefined: not tried; null: none here
function textWidth(text) {
  const value = String(text === null || text === undefined ? "" : text);
  if (measurer === undefined) {
    try {
      const canvas = document.createElement("canvas");
      measurer = canvas && canvas.getContext ? canvas.getContext("2d") : null;
      if (measurer) measurer.font = `${CHART_FONT}px ${CHART_FAMILY}`;
    } catch (e) { measurer = null; }
  }
  return measurer ? measurer.measureText(value).width : value.length * CHART_FONT * 0.55;
}

/* The widest of them, in pixels. */
export function widestLabel(labels) {
  return (labels || []).reduce((most, label) => Math.max(most, textWidth(label)), 0);
}

/* THE ROOM ONE LABEL ON A HORIZONTAL AXIS ASKS FOR before it may be drawn
 * at all: its own width, but never more than MAX_HORIZONTAL. Past that a
 * name is cut to its column with an ellipsis and still reads (the tooltip
 * and the numbers table carry the whole of it), whereas demanding the full
 * width of a 40-character address would hide the axis of every matrix in
 * the product. */
const MAX_HORIZONTAL = 120;
export function horizontalLabelRoom(labels) {
  return Math.min(widestLabel(labels) || CHART_FONT * 4, MAX_HORIZONTAL);
}

/* WHAT ONE COLUMN OF A MATRIX ASKS FOR, given that its name stands upright.
 *
 * Not the width of the name - up to 120 px a column, which on eight columns
 * is a canvas 1000 px wide that a card has to scroll. A rotated label is
 * limited by the height under the plot instead, so a column
 * only has to be wide enough for a line of text to stand in and for the
 * cells to breathe. That is what lets a matrix fit the card it is in, which
 * is the whole point of turning the names. */
export const MATRIX_COLUMN = Math.round(LABEL_LINE * 2.2);

/* AND THE ROOM THOSE UPRIGHT NAMES TAKE UNDER THE PLOT.
 *
 * A label lying flat costs one line of height; standing up it costs its own
 * LENGTH, and that comes out of the plot. Left out of the arithmetic, a
 * matrix sized for eight rows of 34 px had 140 px of it eaten by the names
 * along the bottom, the rows came out at 21 px, and the rule dropped every
 * name on the other axis - a matrix with no labels at all, on a card that
 * had room for them.
 *
 * Capped at the same 140 px the label budget uses: past that a name is cut
 * with an ellipsis and the whole of it is in the tooltip. */
export const UPRIGHT_MAX = 140;

export function uprightLabelRoom(labels) {
  return Math.min(UPRIGHT_MAX, Math.ceil(widestLabel(labels))) + TICK_GAP;
}

/* The word for one of them, from the registry (app/charts/__init__.py). It
 * is what makes the sentence under a hidden axis a sentence: "43 entities",
 * not "43 categories". */
function categoryWord(chart) {
  return chart.category || "categories";
}

/* THE PASS THAT DECIDES, WITH THE LAYOUT IN FRONT OF IT.
 *
 * Chart.js knows the plot's size only after it has laid the chart out, and
 * the rule is about pixels - so the chart is drawn once, measured, and told
 * what to do about its labels. One extra update(), on first render only.
 *
 * Returns {hidden, count, word}: whether the category labels were taken
 * off, how many there are and what to call them, which is everything the
 * line under the title needs.
 *
 * IT TAKES NO "ENLARGED". The popup is not exempt from the fit test - an
 * enlarged chart assumed to have room would draw its labels whatever the
 * arithmetic says, and a matrix sized only downwards would draw four names
 * on top of one another. The rule is one rule: the popup EARNS the labels
 * by being sized from the data (enlargedSize), and where even that cannot
 * buy the room, the labels come off here as they do anywhere else.
 */
export function applyLabelRule(instance, chart) {
  const area = instance.chartArea || {};
  const height = (area.bottom || 0) - (area.top || 0);
  const width = (area.right || 0) - (area.left || 0);
  const count = categoryCount(chart);
  let hidden = false;
  let changed = false;

  // The axis budget in pixels: everything to the left of the plot, less a
  // little air. tickLabel() reads it back off the instance.
  const axisRoom = Math.max(0, (area.left || 0) - 12);
  if (chart.kind !== "time" && axisRoom > 0 && instance.$tickWidth !== axisRoom) {
    instance.$tickWidth = axisRoom;
    changed = true;
  }

  if (chart.kind === "time" && chart.upright_dates) {
    // UPRIGHT DATES ARE NOT THINNED: that is the whole point of turning
    // them. What limits them is the width of a column, which is one line of
    // text, exactly as on a matrix - and where even that does not fit the
    // rule below takes them off rather than printing them over each other.
    const count = (chart.labels || []).length;
    const ticks = instance.options.scales.x.ticks;
    if (ticks.maxTicksLimit !== undefined) { ticks.maxTicksLimit = undefined; changed = true; }
    hidden = !labelsFit(width, count, LABEL_LINE);
    if (ticks.display !== !hidden) { ticks.display = !hidden; changed = true; }
  } else if (chart.kind === "time") {
    // A time axis THINS. Its widest label decides how many fit.
    const labels = chart.labels || [];
    const ctx = instance.ctx;
    let widest = 0;
    if (ctx) {
      ctx.save();
      ctx.font = `${CHART_FONT}px ${CHART_FAMILY}`;
      labels.forEach((l) => { widest = Math.max(widest, ctx.measureText(String(l)).width); });
      ctx.restore();
    }
    const limit = maxTicksFor(width, widest || CHART_FONT * 3);
    const ticks = instance.options.scales.x.ticks;
    if (ticks.maxTicksLimit !== limit) {
      ticks.maxTicksLimit = limit;
      changed = true;
    }
  } else if (chart.kind === "key") {
    // A horizontal bar chart: one label per row, so the rule is vertical.
    // Enlarged the test still runs (one row is ENLARGED_ROW px, so it
    // passes); it is the backstop, not a second opinion.
    // TWO LINES NEED THE ROOM OF TWO. A label carrying a type under the
    // name is two lines tall, so the rule that decides whether the names
    // are drawn at all has to ask for both of them - otherwise the type of
    // one row is written over the name of the next.
    hidden = !labelsFit(height, count, LABEL_LINE * labelRows(chart));
    const ticks = instance.options.scales.y.ticks;
    if (ticks.display !== !hidden) {
      ticks.display = !hidden;
      changed = true;
    }
  } else {
    // A matrix: the y axis is a list of rows (vertical), the x axis a row
    // of names (horizontal, so the rule is about WIDTH per column).
    const axes = matrixLabels(chart);
    const ys = axes.y.length;
    const xs = axes.x.length;
    // WHAT ONE X LABEL MAY BE AS LONG AS, given that it stands up: the room
    // UNDER the plot, which is what a rotated label is measured against.
    // Across the column it would be four letters at eight columns; down the
    // card it is a name. tickLabel() reads the budget back off the instance.
    /* HOW LONG AN UPRIGHT NAME MAY BE: the room under the plot.
     *
     * Enlarged, none of this applies. The popup exists to show the whole of
     * everything and has a dialog to scroll in, so the name is not cut there
     * at all - `$tickWidthX` of 0 means "no pixel budget", and the tick
     * callback falls back to its character cap. On a card the name is cut to
     * what is under the plot, and the whole of it is in the tooltip. */
    const budgetX = instance.$enlarged ? 0 : Math.max(140, Math.round(height * 0.45));
    if (instance.$tickWidthX !== budgetX) {
      instance.$tickWidthX = budgetX;
      changed = true;
    }
    // THE FIT TEST RUNS HERE WHETHER OR NOT THE CHART IS ENLARGED. The
    // popup is sized so that it passes - enlargedSize() gives every
    // column the width of the widest name on the axis - and this stays
    // as the backstop for what that sizing cannot buy (a name wider than
    // the biggest canvas a browser will draw): the label is DROPPED, and
    // never printed over its neighbour.
    const yHidden = !labelsFit(height, ys);
    // A COLUMN NEEDS ONE LINE OF WIDTH, not one label's worth: the names are
    // turned a quarter round, so what has to fit across a column is the
    // height of a line of text and nothing more.
    const xHidden = !labelsFit(width, xs, LABEL_LINE);
    hidden = yHidden || xHidden;
    const y = instance.options.scales.y.ticks;
    const x = instance.options.scales.x.ticks;
    if (y.display !== !yHidden) { y.display = !yHidden; changed = true; }
    if (x.display !== !xHidden) { x.display = !xHidden; changed = true; }
    // Upright, always: a label at 45 degrees is the diagonal smear this
    // rule exists to prevent, and one lying flat across a 40 px column is
    // four letters. 90 or nothing.
    if (x.maxRotation !== 90) { x.maxRotation = 90; x.minRotation = 90; changed = true; }
  }

  if (changed) instance.update("none");
  return { hidden, count, word: categoryWord(chart) };
}

/* The sentence under the title when the labels had to go. It names the
 * number, the thing and the way to see them - because a chart that has
 * quietly stopped labelling itself is worse than one that never did. */
export function hiddenLabelsLine(chart, count) {
  return `${fmt(count)} ${categoryWord(chart)} - open the chart to see the names.`;
}

/* Draw one chart into a box that already has a height. Returns the handle
 * everything else in this file hangs off. */
function drawChart(holder, chart, opts = {}) {
  const canvas = document.createElement("canvas");
  canvas.setAttribute("role", "img");
  canvas.setAttribute("aria-label", describeChart(chart));
  holder.appendChild(canvas);

  const config = chart.kind === "time" ? timeConfig(chart)
    : chart.kind === "key" ? keyConfig(chart, opts) : matrixConfig(chart, opts);

  if (!chartLibraryPresent()) {
    // No Chart.js (a stripped deployment, a blocked file): the table below
    // is the chart, and the page says so instead of showing a blank box.
    canvas.remove();
    holder.appendChild(el("p", "notice notice--warn",
      "The chart library is not available; the numbers are in the table below."));
    return { canvas: null, instance: null, labels: { hidden: false, count: 0 }, destroy() {} };
  }

  const instance = new window.Chart(canvas.getContext("2d"), config);
  // The popup shows the whole of everything, and the label rule reads this
  // to know that it may not cut a name to a pixel budget here.
  instance.$enlarged = Boolean(opts.enlarged);
  live.add(instance);
  const onPick = opts.onPick || (() => {});
  canvas.addEventListener("click", (event) => {
    const picked = pickFor(instance, chart, event);
    if (!picked) return;
    // FOCUS FIRST, THEN OPEN. The dialog gives the focus back to whatever
    // was focused when it opened, and a click on a canvas focuses nothing -
    // so a reader who closes the drilldown would land at the top of the
    // page, having lost the chart they were reading. The canvas is the nearest
    // thing to "the bar that was clicked" a document can hold focus on.
    if (typeof canvas.focus === "function") canvas.focus();
    onPick(picked.dataset, picked.point);
  });
  // The cursor, from the SAME answer: a pointer exactly where a click
  // opens the drilldown and nowhere else. Set on the canvas per position
  // rather than in the stylesheet, because a canvas has no elements a
  // stylesheet could tell apart.
  canvas.addEventListener("mousemove", (event) => {
    canvas.style.cursor = pickFor(instance, chart, event) ? "pointer" : "";
    /* AND THE WHOLE OF A NAME THAT DID NOT FIT.
     *
     * An axis cuts a label to the room it has - "Nordwyk Pum…" - and a
     * canvas has no elements, so there is nothing for a stylesheet or a
     * `title=` to hang on. The pointer's position IS the element: over the
     * label column of a category axis, the canvas takes the full text of
     * the tick under it as its own tooltip, and drops it again on the way
     * out. The same plain tooltip the rest of the page uses, so nothing on
     * this page explains itself in two different shapes. */
    canvas.title = tickUnder(instance, event) || "";
  });
  canvas.addEventListener("mouseleave", () => {
    canvas.style.cursor = "";
    canvas.title = "";
  });
  // Focusable, but not in the tab order: the numbers table under the card
  // is the keyboard's way into the drilldown (every value is a button), so
  // a second stop on an unreadable canvas would be noise. -1 is what lets
  // focus be GIVEN to it and taken back from a dialog.
  canvas.tabIndex = -1;

  const labels = applyLabelRule(instance, chart);

  /* THE RULE IS ABOUT PIXELS, SO IT IS RE-DECIDED WHEN THE PIXELS CHANGE.
   *
   * A window narrowed from 1440 to 1024 gives a chart a shorter plot, and a
   * decision taken at the first size would leave labels drawn in a box that
   * no longer holds them. Deferred by a frame because this runs INSIDE
   * Chart.js's own resize, and the rule ends in update() - which converges
   * (nothing changes on the second pass) but must not re-enter the layout
   * it was called from. `beforeprint` resizes every chart too, so paper
   * gets the same decision as the screen.
   *
   * The card's own line follows: a chart that hides its labels on a narrow
   * window has to say so there as well, or the sentence and the picture
   * disagree at exactly the width where it matters most. */
  instance.options.onResize = () => {
    window.requestAnimationFrame(() => {
      if (!live.has(instance)) return;
      const now = applyLabelRule(instance, chart);
      // ALWAYS, not only when the rule changed its mind. A resize re-lays
      // the axes and the elements in one pass of Chart.js's own, so the
      // bars come out of it measured against the previous plot even when
      // this rule had nothing to say - which would send the bars back to
      // floating after applyLabelRule has already put them on zero.
      if (typeof opts.onLabels === "function") opts.onLabels(now);
    });
  };

  return {
    canvas,
    instance,
    labels,
    destroy() {
      live.delete(instance);
      instance.destroy();
    },
  };
}

/* ── Saving a chart as a picture ───────────────────────────────────────── */
/*
 *  "The same image button on every chart. One per chart card, and inside
 *   the enlarge dialog too - that is where somebody actually wants the
 *   picture, because there the labels are all visible."
 *
 *  Chart.js draws on a canvas, so `canvas.toBlob()` gives a PNG directly.
 *  NOT html2canvas: that would rasterise a rasterisation and blur every
 *  label - the map needs it because a map is a stack of DOM tiles, a chart
 *  does not.
 *
 *  Three things the canvas does not carry by itself, and each of them
 *  decides whether the file is usable a week later:
 *
 *    THE WHITE GROUND. A canvas is transparent. A transparent PNG dropped
 *    into a dark document turns every label invisible - the picture looks
 *    empty and nobody can tell why.
 *
 *    THE CAPTION. The chart's title and what it was drawn of - project,
 *    language, period, and the search term if there was one. A bar chart
 *    with no caption is unusable a week later: the person who saves it
 *    pastes it somewhere and the numbers have lost their subject.
 *
 *    THE DEVICE PIXEL RATIO. The backing store is already at dpr (Chart.js
 *    sizes it that way), so the bitmap is copied at its own size and the
 *    caption is drawn at the same scale - a caption typed at CSS pixels on
 *    a 2x bitmap comes out half height.
 */

/* Room around the picture and under it, in CSS pixels before the ratio. */
const IMAGE_PAD = 16;
const IMAGE_LINE = 24;
const IMAGE_TITLE_SIZE = 17;
const IMAGE_CAPTION_SIZE = 14;

/* A caption line that does not fit is WRAPPED, never cut off the edge.
 *
 * The second line is the subject of the picture - project, language,
 * period, search term - and a subject that runs past the right edge is the
 * failure the caption exists to prevent. It breaks at the separators it is
 * built from, and only a single part longer than the whole width is
 * shortened, with an ellipsis, so the reader can see that it was. */
function fitCaption(ctx, text, font, room) {
  ctx.font = font;
  const whole = String(text);
  if (ctx.measureText(whole).width <= room) return [whole];
  const lines = [];
  let current = "";
  whole.split(" - ").forEach((part) => {
    const next = current ? `${current} - ${part}` : part;
    if (current && ctx.measureText(next).width > room) {
      lines.push(current);
      current = part;
    } else {
      current = next;
    }
  });
  if (current) lines.push(current);
  return lines.map((one) => {
    if (ctx.measureText(one).width <= room) return one;
    let cut = one;
    while (cut.length > 1 && ctx.measureText(`${cut}…`).width > room) cut = cut.slice(0, -1);
    return `${cut}…`;
  });
}

/* The offscreen canvas the file is made of: white, the chart, the caption.
 * Separated from the saving so a test can look at the pixels without a
 * download - and so the two cannot drift apart. */
export function chartImageCanvas(source, lines, ratio) {
  const scale = Math.max(1, Number(ratio) || 1);
  const pad = Math.round(IMAGE_PAD * scale);
  const line = Math.round(IMAGE_LINE * scale);
  const width = (source ? source.width : 0) + pad * 2;
  const font = (i) => `${i === 0 ? "600 " : ""}`
    + `${Math.round((i === 0 ? IMAGE_TITLE_SIZE : IMAGE_CAPTION_SIZE) * scale)}px ${CHART_FAMILY}`;

  // Measured first, on a throwaway context: how many lines the caption
  // takes decides how tall the file is.
  const ruler = document.createElement("canvas").getContext("2d");
  const said = [];
  (lines || []).filter(Boolean).forEach((text, i) => {
    fitCaption(ruler, text, font(i), width - pad * 2).forEach((one) => said.push([one, i]));
  });

  const out = document.createElement("canvas");
  out.width = width;
  out.height = (source ? source.height : 0) + pad * 2 + said.length * line;
  const ctx = out.getContext("2d");
  // The ground first, over the whole file: a transparent PNG in a dark
  // document loses its labels.
  ctx.fillStyle = SURFACE;
  ctx.fillRect(0, 0, out.width, out.height);
  if (source && source.width && source.height) ctx.drawImage(source, pad, pad);
  let y = (source ? source.height : 0) + pad + Math.round(line * 0.75);
  said.forEach(([text, i]) => {
    ctx.fillStyle = i === 0 ? TICK : withAlpha(TICK, 0.75);
    ctx.font = font(i);
    ctx.textBaseline = "alphabetic";
    ctx.fillText(text, pad, y);
    y += line;
  });
  return out;
}

/* Character for character api_export._slug(), so a folder of exports sorts
 * together whatever produced them. */
function slug(value) {
  return (String(value === null || value === undefined ? "" : value)
    .replace(/[^A-Za-z0-9_-]+/g, "_").slice(0, 40)) || "all";
}

function stampNow(date) {
  const d = date || new Date();
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getUTCFullYear()}${pad(d.getUTCMonth() + 1)}${pad(d.getUTCDate())}`
       + `-${pad(d.getUTCHours())}${pad(d.getUTCMinutes())}`;
}

export function imageFilename(chart, context) {
  const c = context || {};
  return `xtracting-chart-${slug(chart.title)}-${slug(c.project)}-${slug(c.language)}`
       + `-${stampNow()}.png`;
}

/* The two caption lines: what the chart is, and what it is of. */
export function imageCaption(chart, context) {
  const c = context || {};
  return [chart.title,
          [c.project, c.language, c.period, c.term ? `“${c.term}”` : ""]
            .filter(Boolean).join(" - ")];
}

/* Save one chart. `say` is how the caller reports back - the card's own
 * status line, or the dialog's - because a button that appears to do
 * nothing is worse than one that is not there. */
export function saveChartImage(chart, canvas, context, say) {
  const tell = say || (() => {});
  if (!canvas || !canvas.width) {
    tell("There is no picture to save yet.");
    return Promise.resolve(false);
  }
  const out = chartImageCanvas(canvas, imageCaption(chart, context),
                              window.devicePixelRatio || 1);
  const name = imageFilename(chart, context);
  return new Promise((resolve) => {
    const done = (blob) => {
      if (!blob) { tell("The picture could not be saved."); resolve(false); return; }
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = name;
      // Appended, clicked, removed: a link that is never in the document
      // is not clickable in every browser.
      document.body.appendChild(a);
      a.click();
      a.remove();
      // Long enough for the download to have started; the object URL is a
      // reference to a bitmap and holding it keeps the memory.
      setTimeout(() => URL.revokeObjectURL(url), 10000);
      /* A GREEN MESSAGE BOX, NOT A GREY LINE UNDER THE CARD.
       *
       * "Saved as xtracting-chart-….png" does not go into the card's own
       * status line - which sits UNDER the picture, below the legend and the
       * numbers, often below the fold on a tall chart, and always in the
       * same muted grey as the note about dropped labels. A file written to
       * the reader's disk whose only sign is a sentence they have to scroll
       * to find, dressed as a footnote, is a file they do not know about.
       *
       * The toast is bottom-centre, where the eye returns between actions,
       * and green because this is the one kind of news that is good. It is
       * also the one kind safe to take itself away again: nothing has gone
       * wrong, nothing has to be acted on, and the file is on the disk
       * whether or not the message is still on screen. An error still goes
       * to the card, where it stands beside the button that failed.
       *
       * role="status" (a11y.js), so writing it here IS the announcement -
       * announce() as well would say it twice.
       */
      toast(`Saved as ${name}`, { kind: "success" });
      resolve(true);
    };
    if (typeof out.toBlob === "function") out.toBlob(done, "image/png");
    else done(null);
  });
}

/* ── The key that names what the picture is made of ────────────────────── */
/*
 *  A stack of six violet bands with nothing saying which band is which is
 *  a picture of nothing. The key is HTML, never the canvas-drawn legend:
 *  it is readable, selectable, and its toggles work by keyboard.
 *
 *  Built here rather than inside the card, because the ENLARGE DIALOG
 *  needs the same key - while a modal dialog is open the card's own key is
 *  behind the backdrop and unreachable, so a dialog without one leaves the
 *  reader with bands they cannot name (js/query.js takes the same decision
 *  with its list of places). One function, so the two cannot drift apart.
 *
 *  `drawn` is the handle of the chart these controls act on - the card's
 *  instance or the dialog's - so hiding a series always toggles the
 *  picture the reader is looking at.
 */
function renderChartKey(box, chart, drawn) {
  if (!box) return;
  box.textContent = "";
  const canvas = drawn ? drawn.canvas : null;
  const instance = drawn ? drawn.instance : null;
  if (chart.datasets.length > 1) {
    // What is on screen right now, so hiding a series can be said as well
    // as drawn (the canvas has no text of its own).
    const visible = new Set(chart.datasets.map((ds) => ds.id));
    renderLegend(box, chart.datasets.map((ds) => ({
      key: ds.id, name: ds.label, colour: ds.colour,
      // WHAT THAT COLOUR IS, from the registry (app/charts/__init__.py:
      // DATASET_HINTS), carried on the dataset because half of these series
      // are named at run time. legend.js turns it into the ⓘ beside the row.
      // "Low Importance" is a phrase from a vocabulary the reader has never
      // been shown, and "Minimum" is not a count although every other bar on
      // the page is: a key that names its colours and explains none of them
      // is only half a key.
      description: ds.hint || "",
    })), {
      title: "Series",
      onToggle: (key, on) => {
        const index = chart.datasets.findIndex((d) => d.id === key);
        if (instance && index >= 0) {
          instance.setDatasetVisibility(index, on);
          instance.update();
        }
        if (on) visible.add(key); else visible.delete(key);
        if (canvas) canvas.setAttribute("aria-label", describeChart(chart, visible));
        const name = (chart.datasets.find((d) => d.id === key) || {}).label || key;
        announce(`${name} ${on ? "shown" : "hidden"}, ${visible.size} of ${chart.datasets.length} series shown`);
      },
    });
    box.dataset.ready = "true";
  } else if (chart.kind === "matrix") {
    /* A matrix has one series and needs no legend of colours - but it does
     * need somebody to say what the MARK means, which is the one thing the
     * picture cannot say about itself.
     *
     * And the mark is a cell now, not a bubble: the sentence said "bubble
     * area, and the number printed in it" under a grid of squares with no
     * number in them, which is a caption describing a chart this page no
     * longer draws. A cell's STRENGTH is the count (MATRIX_CELLS). */
    const measure = chart.datasets[0] ? chart.datasets[0].label : "rows";
    box.appendChild(el("p", "chart-key",
      `The stronger the cell, the more ${measure.charAt(0).toLowerCase() + measure.slice(1)} at that pair.`));
  }
}

/* ── The enlarge popup ─────────────────────────────────────────────────── */
/*
 *  What it is for: seeing every detail of a chart the card had no room for,
 *  drawn large enough that all of it is readable.
 *
 *  So the chart inside the popup is NOT scaled to fit the popup. Its size
 *  comes from the DATA: every category gets ENLARGED_ROW pixels, 43
 *  entities make a canvas 43 rows tall, and the dialog body scrolls. A
 *  chart squeezed into the window would have exactly the problem the
 *  enlarging was meant to solve.
 */

/* Room for the value axis, its title and the padding around the plot. */
const AXIS_ROOM = 72;
/* What Chart.js puts around the widest NAME on a category axis: the tick
 * marks, the axis title and the padding either side. Measured on the
 * connection matrix - a 316 px name gave a 357 px axis - and rounded up,
 * because reserving a little too much costs a little scrolling while
 * reserving too little takes the room out of the columns. */
const AXIS_PAD = 64;
/* No axis may eat the whole canvas: past this the names are cut to what is
 * left, which still reads (fitText). */
const MAX_AXIS_ROOM = 420;
/* A time chart is enlarged ACROSS, not down: its height is a comfortable
 * plot, its width is one column per period. */
const ENLARGED_COLUMN = 44;
const ENLARGED_TIME_HEIGHT = 420;
/* A browser will not draw a canvas of any size, and a chart nobody can
 * scroll to the end of is not a reading either. Past this the columns
 * share what there is and the label rule drops the names that still do
 * not fit - it never lets two of them be drawn on top of each other. */
const ENLARGED_MAX_WIDTH = 8000;

/* HOW MANY CATEGORIES ARE WORTH DRAWING AT ALL.
 *
 * Hundreds of rows is a canvas nobody scrolls to the end of, so the biggest
 * N are kept - and the number left out is SAID, because silently dropping
 * data is the one thing that must not happen. Every one of them is still in
 * the numbers table under the card, and the drilldown reaches the rows
 * behind each. */
export const MAX_ENLARGED = 60;

/* The canvas size an enlarged chart needs, and what had to be left out.
 * `room` is the width the dialog body gives it, `tall` its height - which
 * is a FLOOR for a matrix and nothing else: rows never get less than
 * ENLARGED_ROW, and where the popup has more room than the data asks for
 * they get it rather than leaving half the dialog white. */
export function enlargedSize(chart, room, tall) {
  const width = Math.max(320, Number(room) || 320);
  const count = categoryCount(chart);
  if (chart.kind === "time") {
    return { width: Math.max(width, count * ENLARGED_COLUMN + AXIS_ROOM),
             height: ENLARGED_TIME_HEIGHT, drawn: count, left_out: 0 };
  }
  if (chart.kind === "matrix") {
    /* BOTH AXES COME FROM THE DATA.
     *
     * A matrix sized only downwards can grow a row for every entry of its
     * y axis and not one pixel for the names along the bottom, so those
     * names would be drawn 300 px wide in columns 166 px apart - four pairs
     * of them printed on top of one another, in the one dialog that exists
     * to show every label. Its width is one column per x category, each
     * as wide as the widest name on that axis, plus the room the y axis
     * takes; the popup body scrolls sideways as it already scrolled down.
     *
     * Neither axis is cut. Dropping a row of a matrix moves its diagonal
     * (topCategories leaves a matrix alone), so nothing is left out and
     * the sentence about what was left out is not shown.
     */
    const axes = matrixLabels(chart);
    const columns = Math.max(1, axes.x.length);
    const axisRoom = Math.max(AXIS_ROOM,
                              Math.min(widestLabel(axes.y), MAX_AXIS_ROOM) + AXIS_PAD);
    // ONE COLUMN IS A LINE WIDE, because the names along the bottom stand
    // upright: sized by the width of the widest of them, a popup of eight
    // entities would be 1000 px wider than it needs to be and then scroll.
    const column = Math.max(ENLARGED_COLUMN, MATRIX_COLUMN);
    const asks = AXIS_ROOM + Math.max(1, axes.y.length) * ENLARGED_ROW
                 + uprightLabelRoom(axes.x);
    return {
      width: Math.max(width, Math.round(columns * column + axisRoom)),
      height: Math.max(asks, Math.round(Number(tall) || 0)),
      drawn: count, left_out: 0, column: Math.round(column),
    };
  }
  const drawn = Math.min(count, MAX_ENLARGED);
  // Two lines of label need the room of two here as well - the popup exists
  // to show the whole of everything, and a name whose type is written under
  // it is two lines of one label.
  return { width, height: AXIS_ROOM + drawn * ENLARGED_ROW * labelRows(chart),
           drawn, left_out: Math.max(0, count - drawn) };
}

/* ── The summary band ──────────────────────────────────────────────────── */
/*
 *  ONE BAND PER TAB, FULL WIDTH, ABOVE THE GRID: the newest sources, the
 *  most extreme ratings, the connection matrix (app/charts/__init__.py marks
 *  them with `band`). What makes a band chart different is not its data - it
 *  is drawn, drilled and exported exactly like any other - but its SIZE.
 *
 *  A GRID CARD HAS A FIXED PLOT: 17 rem, so eight cards in a row are the
 *  same height whatever is in them (css/diagrams.css), and a chart with more
 *  categories than fit that box simply loses its labels and says so. That
 *  trade is right in the grid, where the cards are compared with each other,
 *  and wrong in the band, which exists to be READ: a summary whose names are
 *  not drawn is not a summary of anything.
 *
 *  So the band's height comes from the DATA - one row per category, never a
 *  constant - which is the rule the enlarge popup and the Dashboard's
 *  importance charts already follow (enlargedSize above;
 *  static/js/dashboard.js: chartHeight). The server cuts a band to twelve
 *  categories (charts/__init__.py: BAND_LIMIT) so that "one row each" is a
 *  band and not a page, and the label rule still runs on top of all of it as
 *  the backstop it always was.
 */

/* One category of a band chart, in pixels. Over LABEL_LINE, so the name is
 * always drawn; over 24, so the bar is always a target (WCAG 2.5.8). */
export const BAND_ROW = 34;
/* Two bars in one row need two targets in it. */
const BAND_BAR = 28;
/* A time chart in a band is enlarged ACROSS, not down, exactly as in the
 * popup: its categories are periods and the axis thins them. */
const BAND_TIME_HEIGHT = 320;
/* The widest a band will draw its canvas before it gives up and lets the
 * label rule drop the names - the same number the enlarge popup stops at,
 * because it is the same constraint: a browser will not draw a canvas of any
 * size, and a chart nobody can scroll to the end of is not a reading. */
export const MAX_BAND_WIDTH = ENLARGED_MAX_WIDTH;

/* How much room one category needs: one row per bar drawn in it. Stacked
 * series share a bar, so they share the row.
 *
 * AND TWO LINES OF LABEL NEED THE ROOM OF TWO. The entities band labels each
 * bar with the name and the kind of thing it is, one under the other; asked
 * for at one line per row, the label rule found no room for the second and
 * dropped every name - a summary of nothing. The rule and this arithmetic
 * have to ask the same question, so both count the lines. */
export function labelRows(chart) {
  return (chart.labels || []).some((l) => String(l).includes("\n")) ? 2 : 1;
}

export function bandRow(chart) {
  const series = stacksSeries(chart) ? 1 : Math.max(1, (chart.datasets || []).length);
  return Math.max(BAND_ROW * labelRows(chart), series * BAND_BAR);
}

/**
 * The canvas a band chart asks for, given the width the band gives it.
 *
 * A matrix asks through enlargedSize(), which is the same arithmetic the
 * popup uses - one column per x category, each as wide as the widest name on
 * that axis - so the band draws it at the size where every label fits and
 * scrolls sideways when that is wider than the page. A key chart takes the
 * width it is given and asks for height.
 */
export function bandSize(chart, room) {
  const width = Math.max(320, Number(room) || 320);
  if (chart.kind === "time") return { width, height: BAND_TIME_HEIGHT };
  if (chart.kind === "matrix") {
    /* THE RULE'S OWN MINIMUM, NOT THE POPUP'S IDEAL.
     *
     * enlargedSize() gives every column the width of the widest NAME on the
     * axis, because the popup exists to show the whole of everything and
     * has a dialog to scroll in. A band is the top of a page: it may only
     * take the width it has to. The rule says a label is drawn when its
     * column is at least horizontalLabelRoom() wide - the name, capped at
     * MAX_HORIZONTAL, past which a name is cut to its column with an
     * ellipsis and still reads - so THAT, exactly, is the width at which
     * every label is drawn, and one pixel less is the width at which none
     * is. No gap is added on top: applyLabelRule already trims each label
     * to its own column less TICK_GAP, which is what keeps two neighbours
     * from touching, and asking for the gap as well pushed a nine-entity
     * matrix 62 px past a 1440 px window - a scrollbar bought for nothing.
     *
     * On the preseed's connection matrix the answer is under the page's own
     * width, so the band does not scroll at all; on a matrix with more
     * entities than the page is wide it asks for what it needs and the box
     * scrolls sideways rather than dropping the names.
     */
    const axes = matrixLabels(chart);
    const columns = Math.max(1, axes.x.length);
    const axisRoom = Math.max(AXIS_ROOM,
                              Math.min(widestLabel(axes.y), MAX_AXIS_ROOM) + AXIS_PAD);
    const column = MATRIX_COLUMN;
    return {
      width: Math.max(width, Math.round(columns * column + axisRoom)),
      height: AXIS_ROOM + Math.max(1, axes.y.length) * BAND_ROW
              + uprightLabelRoom(axes.x),
    };
  }
  return { width, height: AXIS_ROOM + Math.max(1, categoryCount(chart)) * bandRow(chart) };
}

/* THE SECOND PASS, WITH THE LAYOUT IN FRONT OF IT.
 *
 * bandSize() has to guess how much of the canvas the FURNITURE will take -
 * the axis names down the left, the two axis titles, the padding Chart.js
 * puts around a centred label that hangs past the last column - and a guess
 * that is 60 px short is a matrix whose names are all dropped. Measured on
 * the preseed's connection matrix at 1440: the estimate said 1294 px, the
 * plot came out narrower than nine columns of 120, and the card printed
 * "9 entities - open the chart to see the names" at the top of the tab that
 * exists to show them.
 *
 * So the guess is only the opening bid. Chart.js knows the plot's real size
 * once it has laid the chart out, and `instance.width - plot` is the
 * furniture measured rather than estimated; the box is widened to whatever
 * that leaves the columns needing, and one resize() re-runs the label rule.
 * It converges by construction - the furniture does not grow when the plot
 * does - and it runs at most once per card.
 *
 * ONLY A MATRIX. A key chart's labels are a vertical question and the height
 * already answers it; a time axis thins its ticks rather than dropping them.
 */
function growBand(card, box, chart, instance) {
  if (chart.kind !== "matrix" || !instance) return false;
  const area = instance.chartArea || {};
  const plot = (area.right || 0) - (area.left || 0);
  if (!(plot > 0) || !(instance.width > 0)) return false;
  const xs = matrixLabels(chart).x;
  const room = MATRIX_COLUMN;
  /* THE FURNITURE IS MEASURED WITH THE LABELS OFF, AND IT GROWS WHEN THEY
   * COME BACK ON. That is the whole reason this needs a margin rather than
   * an exact sum: the plot is 1080 px wide across nine columns of 120 while
   * the names are hidden, which is exactly the width at which they fit -
   * turn them on and Chart.js reserves room for the half of the first and
   * the last name that hangs past its column, the plot narrows, and the rule
   * drops them again. Measured on the preseed at 1440: the arithmetic came
   * out at 1277 px against a canvas of 1277, so nothing ever grew and the
   * card said "9 entities - open the chart to see the names" on the tab that
   * exists to show them.
   *
   * One label's width of headroom covers both overhangs - each is at most
   * half a label - and it is asked for once. */
  const furniture = instance.width - plot;
  // A browser will not draw a canvas of any size, and a chart nobody can
  // scroll to the end of is not a reading either: past the cap the label
  // rule has the last word and the card says how many names it could not
  // draw, exactly as a card in the grid does.
  const need = Math.min(MAX_BAND_WIDTH, xs.length * room + furniture + room);
  if (!(need > instance.width + 1)) return false;
  box.style.width = `${Math.round(need)}px`;
  /* IS-WIDE MEANS "THERE IS SOMETHING TO SCROLL TO", and that is a question
   * about the BOX, not about the canvas. A canvas can be narrower than the
   * card it sits in - Chart.js sizes it from its own layout - so a chart
   * that grew past the canvas and still fits in the card has nothing off
   * screen, and a card marked scrollable with nothing to scroll is a
   * horizontal scrollbar nobody can move. */
  const holder = box.parentElement;
  const seen = holder ? holder.clientWidth : instance.width;
  if (need > seen + 1) card.classList.add("is-wide");
  instance.resize();
  return true;
}

/* The chart with only its biggest N categories left, for the case where
 * even a scrolling canvas cannot show them all. The order the server sent
 * is kept (it is already "biggest first" for a key chart); a matrix is not
 * cut, because dropping a row of it moves the diagonal. */
function topCategories(chart, keep) {
  if (chart.kind !== "key" || (chart.labels || []).length <= keep) return chart;
  const total = (i) => (chart.datasets || []).reduce(
    (sum, ds) => sum + (Number(ds.data[i] ? ds.data[i].y : 0) || 0), 0);
  const order = (chart.labels || []).map((_, i) => i)
    .sort((a, b) => total(b) - total(a)).slice(0, keep)
    .sort((a, b) => a - b);
  return Object.assign({}, chart, {
    labels: order.map((i) => chart.labels[i]),
    datasets: (chart.datasets || []).map((ds) => Object.assign({}, ds, {
      data: order.map((i) => ds.data[i]),
    })),
  });
}

/* Open one chart in a dialog, drawn at the size its data asks for.
 *
 * `opts.onPick(dataset, point)` is the same callback the card uses, so a
 * click on a bar in here opens the drilldown - as a SECOND dialog, over
 * this one, which stays open underneath (js/dialog.js stacks).
 */
export function openEnlarged(chart, opts = {}) {
  const box = el("div", "chart-enlarged");
  const holder = el("div", "chart-enlarged-canvas");
  box.appendChild(holder);
  /* THE KEY AND THE NUMBERS COME INTO THE DIALOG WITH THE PICTURE.
   *
   * A backdrop covers the card, so everything the card said about this
   * chart is unreachable while the popup is open: the reader was left with
   * a six-band violet stack and nothing naming a single band, under a
   * status line pointing at "the numbers under the chart on the page" -
   * which the backdrop was covering. js/query.js already decided this the
   * other way for its enlarged map, and clones its list of places in for
   * exactly the same reason; the two popups now answer "where is the key"
   * the same way.
   *
   * Same classes as the card, so one stylesheet dresses both. Filled below,
   * once the chart has been drawn - the toggles act on THIS instance. */
  const legendBox = el("div", "chart-legend");
  const tableBox = el("div", "chart-numbers");
  box.appendChild(legendBox);
  box.appendChild(tableBox);

  let drawn = null;
  /* THE IMAGE BUTTON IS IN HERE TOO, and this is where it matters: the
   * canvas is as tall as the data, so the saved picture holds every label -
   * which is the whole reason this dialog exists. */
  const save = el("button", "button button--secondary", "Save image");
  save.type = "button";
  save.dataset.chartImage = "";
  save.setAttribute("aria-label", `Save image: ${chart.title}`);
  const handle = openDialog({
    title: chart.title,
    subtitle: opts.subtitle || chart.description || "",
    wide: true,
    content: box,
    actions: [save],
    cancel: "Close",
    onClose: () => { if (drawn) drawn.destroy(); },
  });
  save.addEventListener("click", () => {
    saveChartImage(chart, drawn && drawn.canvas, opts.context,
                   (said) => handle.setBusy(false, said));
  });
  handle.dialog.classList.add("dialog--chart");

  // The room the dialog actually gave the body, measured now that it is on
  // screen - the canvas is sized from the DATA in the other direction.
  const room = handle.body.clientWidth - 32;
  // Measured on the WHOLE chart, so "how many were left out" is a real
  // number; drawn from the cut-down one.
  const size = enlargedSize(chart, room, handle.body.clientHeight - 32);
  const shown = topCategories(chart, MAX_ENLARGED);
  holder.style.width = `${size.width}px`;
  holder.style.height = `${size.height}px`;

  if (size.left_out) {
    // SAID, not swallowed. Above the chart, where it is read before the
    // picture rather than explained after it.
    const note = el("p", "notice notice--warn chart-enlarged-note",
      `The ${fmt(size.drawn)} largest of ${fmt(size.drawn + size.left_out)} `
      + `${categoryWord(chart)} are drawn; ${fmt(size.left_out)} are not. `
      + "The numbers under the picture carry every one of them.");
    box.insertBefore(note, holder);
  }

  /* WHAT IT SAYS IT DID, AND ONLY WHAT IT DID.
   *
   * "8 entities shown at full size" is the one promise the enlarging
   * makes, and it must not be printed unconditionally - under a matrix
   * whose names are lying on top of one another. It is read off the label
   * rule: when even this size could not hold the names, the popup says so
   * and says where they are instead of claiming otherwise. */
  const said = (labels) => {
    if (labels && labels.hidden) {
      return `${fmt(size.drawn)} ${categoryWord(chart)}; their names do not fit even at this `
        + "size - the tooltip and the numbers under the picture carry them.";
    }
    // A canvas bigger than the popup is the POINT of the popup, but a
    // reader has to be told there is more of it: an overlay scrollbar
    // shows nothing until the pointer is already moving.
    const over = size.width > handle.body.clientWidth || size.height > handle.body.clientHeight;
    return `${fmt(size.drawn)} ${categoryWord(chart)} shown at full size`
      + (over ? " - scroll to see the whole of it" : "");
  };
  drawn = drawChart(holder, shown, {
    onPick: opts.onPick,
    enlarged: true,
    // The rule is re-decided when the window changes, and the sentence
    // follows it - a status that disagrees with the picture is worse than
    // no status.
    onLabels: (now) => handle.setBusy(false, said(now)),
  });

  /* The key, against the chart that is on screen HERE: a toggle in this
   * dialog hides a series in this dialog, never one behind the backdrop. */
  renderChartKey(legendBox, shown, drawn);
  /* And the numbers - of the WHOLE chart, not of the drawn subset, so the
   * categories a popup had to leave out are still reachable, and so are
   * the full names when the labels did not fit. Its cells are buttons, so
   * the drilldown opens from here too: as a second dialog over this one,
   * which is where a click on a bar already goes. */
  const numbers = dataTable(chart, opts.onPick || (() => {}));
  tableBox.appendChild(numbers);
  /* "Where it fits", measured rather than guessed: open when the body has
   * the room for the whole table, so the white half of the dialog carries
   * the figures instead of nothing - and folded back to its one summary
   * line when the picture already fills the popup, because a table that
   * pushed the chart off the screen would take back what enlarging gave. */
  numbers.open = true;
  if (handle.body.scrollHeight > handle.body.clientHeight) numbers.open = false;

  handle.setBusy(false, said(drawn.labels));
  // The focus stays on the X, where js/dialog.js put it: it is the control
  // every dialog has and the way out. The canvas takes the focus only when
  // a bar is CLICKED (drawChart), which is what brings the reader back to
  // that bar when the drilldown over it closes.
  return handle;
}

/* ── One card ──────────────────────────────────────────────────────────── */

/* THE ⓘ BESIDE THE TITLE, on every card that has a sentence to give.
 *
 * The card already carries a purpose line under the title, in two reserved
 * lines that nothing may grow past (css/diagrams.css: everything above the
 * plot is the same height on every card, or six charts draw at six sizes).
 * The hint is the sentence there was never room for - what this chart
 * counts, over what, and what ONE BAR of it is - and it is asked for rather
 * than shown, so it costs no height at all.
 *
 * A PLAIN TOOLTIP, AND THE SAME ONE THE REST OF THE PAGE USES. Not a
 * button that opens a bubble under the header: that is a second kind of
 * explanation on a page whose two strips ("About:", "Charts:") explain
 * themselves with an ordinary tooltip, so one page would have two shapes of
 * the same thing and the heavier one on the fifty-six cards. The sentence is in `title` for a
 * pointer and in the accessible name for a screen reader, and the mark is
 * focusable so a keyboard can reach it.
 *
 * LEFT OF THE TITLE: it explains the title, and a mark that stands before the
 * thing it explains is read as belonging to it.
 */
function addHint(card, chart) {
  const header = card.querySelector(".card-header");
  const title = card.querySelector(".card-title");
  if (!header || !title || header.querySelector(".strip-hint")) return;
  const said = String(chart.hint || "").trim();
  if (!said) return;
  const mark = el("span", "strip-hint", "i");
  mark.title = said;
  mark.tabIndex = 0;
  mark.setAttribute("role", "img");
  // Eight cards on one screen all called "What this is for" leave a
  // screen-reader user with eight identical marks (WCAG 2.5.3), so the name
  // carries the chart as well as the sentence.
  mark.setAttribute("aria-label", `${chart.title}: ${said}`);
  header.insertBefore(mark, title);
}

/* render(container, chart, {onPick, subtitle, band}) fills a card that is
 * already in the page: heading, the ⓘ, enlarge button, canvas, the line that
 * says what the labels did, legend and table. Returns a handle with
 * destroy(). */
export function renderChart(card, chart, opts = {}) {
  const onPick = opts.onPick || (() => {});
  const meta = card.querySelector("[data-chart-meta]");
  const note = card.querySelector("[data-chart-note]");
  const holder = card.querySelector("[data-chart-canvas]");
  const legendBox = card.querySelector("[data-chart-legend]");
  const tableBox = card.querySelector("[data-chart-table]");
  const empty = card.querySelector("[data-chart-empty]");
  const labelLine = card.querySelector("[data-chart-labels]");
  const enlarge = card.querySelector("[data-chart-enlarge]");

  addHint(card, chart);

  if (meta) meta.textContent = chart.total ? rowsWord(chart.total) : "";
  if (note) {
    note.textContent = chart.note || "";
    note.hidden = !chart.note;
  }

  holder.textContent = "";
  tableBox.textContent = "";
  legendBox.textContent = "";
  if (labelLine) { labelLine.textContent = ""; labelLine.hidden = true; }

  /* THE ENLARGE AFFORDANCE, on every card that has a picture.
   *
   * Quiet, in the card's header, and with a REAL accessible name: eight of
   * these on one screen, all called "Enlarge", leave a screen-reader user
   * with eight identical buttons and no way to tell which chart each one
   * belongs to. The visible word is inside the name, so the two agree
   * (WCAG 2.5.3). */
  if (enlarge) {
    enlarge.hidden = !chart.total;
    // THE TOOLTIP IS THE FUNCTION, THE ACCESSIBLE NAME IS THE FUNCTION AND
    // THE CHART. A pointer wants two words; a screen reader on a page with
    // eight of these needs to know which card it is on.
    enlarge.setAttribute("aria-label", `Enlarge Diagram: ${chart.title}`);
    enlarge.title = "Enlarge Diagram";
    enlarge.onclick = () => openEnlarged(chart, { onPick, subtitle: opts.subtitle,
                                                  context: opts.context });
  }

  /* THE IMAGE BUTTON, on every card that has a picture. Same accessible
   * naming as the enlarge beside it: eight buttons called "Save image" on
   * one screen are eight identical buttons to a screen reader.
   *
   * `drawnCard` is filled in below, when the chart has actually been drawn:
   * the button is wired here so the two affordances are read together, and
   * a card with nothing in it hides both. */
  let drawnCard = null;
  const status = card.querySelector("[data-chart-status]");
  function sayStatus(text) {
    if (!status) return;
    status.textContent = text || "";
    status.hidden = !text;
  }
  sayStatus("");
  /* THE NUMBERS BEHIND A CHART ARE IN THE ENLARGED VIEW, and nowhere else.
   *
   * No third icon that opens the table under the card. It would open a
   * second copy of something one press away: enlarging the chart shows the
   * same table under the bigger picture, with the same buttons in the same
   * cells. A row of three unlabelled glyphs is harder to read than two, and
   * the one left out is the one that duplicates its neighbour.
   *
   * The table itself stays in the card, closed, because it is still the
   * text of this picture: print.css opens every one of them for paper, and
   * a screen reader reaches it in the enlarged dialog where it is open by
   * default. */

  const saveImage = card.querySelector("[data-chart-image]");
  if (saveImage) {
    saveImage.hidden = !chart.total;
    saveImage.setAttribute("aria-label", `Save Diagram: ${chart.title}`);
    saveImage.title = "Save Diagram";
    saveImage.onclick = () => saveChartImage(
      chart, drawnCard && drawnCard.canvas, opts.context, sayStatus);
  }

  const nothing = !chart.total;
  if (empty) {
    empty.hidden = !nothing || Boolean(chart.note);
    // ONE cause, ONE message. A window with nothing in it says the same
    // sentence whatever the chart is: two different messages on one screen
    // read as two different problems. Only the SENTENCE is written - the
    // glyph beside it is the template's (diagrams.html), so the one place
    // that owns every icon in the product still owns this one.
    const said = empty.querySelector("[data-chart-empty-text]");
    if (nothing && said) said.textContent = EMPTY_PERIOD;
  }
  // A blank plot box is not an empty chart, it is 272 px of nothing - six
  // of them made an empty period 2.9 screens tall. The box goes, and the
  // message takes its place, so the card is title, purpose and one line.
  holder.hidden = nothing;
  if (nothing) return { destroy() {} };

  /* WHAT HAPPENED TO THE LABELS, IN ONE LINE UNDER THE TITLE.
   *
   * A chart that has silently stopped labelling its bars is worse than one
   * that never did: the reader cannot tell whether there are two categories
   * or forty. So when the plot has no room, the card says how many there
   * are and where every name can be read - and it says it again whenever
   * the window changes the answer. */
  /* A BAND CHART GROWS BEFORE IT GIVES UP, AND IT DOES IT FROM HERE.
   *
   * The label rule is re-decided every time the pixels change, and the first
   * decision is taken while the card is still settling into its grid cell -
   * so growBand() cannot be called once, after drawing, and be sure it is
   * looking at the layout the reader will see. It is called from the rule's
   * own answer instead, every time that answer is "the names did not fit",
   * and at most once per card: growing resizes the chart, which asks the
   * rule again, which is how one growth converges instead of looping.
   */
  let grown = false;
  function sayLabels(state) {
    const gone = Boolean(state && state.hidden);
    if (labelLine) {
      labelLine.textContent = gone ? hiddenLabelsLine(chart, state.count) : "";
      labelLine.hidden = !gone;
    }
    if (gone && sized && !grown && drawnCard) {
      grown = growBand(card, box, chart, drawnCard.instance);
    }
  }

  /* A BAND CHART IS SIZED FROM ITS DATA, and the box it is drawn in is what
   * carries that size: Chart.js fills its parent, so the parent is the one
   * thing that has to be told how tall the answer is (the Dashboard's
   * importance charts set --chart-h the same way).
   *
   * Where the data asks for more WIDTH than the band has - a matrix whose
   * columns each need the width of the widest name on the axis - the canvas
   * is drawn at the width it asked for inside a box that scrolls sideways,
   * which is what the enlarge popup does with the same arithmetic. Scrolling
   * is the price of every label being drawn; the alternative is columns
   * narrower than their own names, and then no labels at all. */
  let box = holder;
  /* SIZED FROM THE DATA, not from the card.
   *
   * A band always is; a grid card does it when the chart asks (`grow` on the
   * ChartSpec), which is how a full-width chart of forty names gets forty
   * rows of label instead of the rule taking every name off. Everything else
   * about the card is unchanged - it is the HEIGHT that comes from the data,
   * and the card grows with it. */
  const sized = opts.band || Boolean(chart.grow);
  if (sized) {
    const room = Math.max(320, holder.clientWidth || card.clientWidth || 320);
    const size = bandSize(chart, room);
    card.style.setProperty("--chart-h", `${Math.round(size.height)}px`);
    // The inner box is built even when the chart fits: it costs nothing at
    // `width: auto` and it is what growBand() widens after the layout, which
    // it cannot do to the box the card's own height is set on.
    box = el("div", "chart-wide");
    /* THE BOX FILLS THE CARD, AND ONLY growBand() MAY MAKE IT WIDER.
     *
     * `bandSize()` is asked for a width here as well, and it must NOT be set
     * on the box - which is wrong twice over on a card that has just been
     * appended and not yet laid out: `room` falls back to 320, so the
     * arithmetic answers "450 px" for a card 1300 px wide, and the canvas is
     * then PINNED at 450 for good. A matrix in a full-width band would come
     * out a third of the width of its own card with its names dropped.
     *
     * The width is decided after the first draw instead, by growBand(),
     * which measures the layout that actually happened. Until then the box
     * is an ordinary block and fills what it is in. */
    holder.appendChild(box);
  }

  const drawn = drawChart(box, chart, { onPick, onLabels: sayLabels });
  drawnCard = drawn;
  sayLabels(drawn.labels);
  /* IS-WIDE MEANS "THERE IS SOMETHING OFF THE RIGHT EDGE", measured rather
   * than predicted: a card marked scrollable with nothing to scroll is a
   * scrollbar nobody can move, and the class is what the tests read to know
   * which cards are the scrolling kind. */
  if (sized && box !== holder) {
    card.classList.toggle("is-wide", holder.scrollWidth > holder.clientWidth + 1);
  }

  // The key: which colour is which series, or what a bubble's size means.
  // The same one the enlarge dialog builds, from the same function, so a
  // reader who opens the picture bigger does not lose the names.
  renderChartKey(legendBox, chart, drawn);

  const table = dataTable(chart, onPick);
  tableBox.appendChild(table);

  return { destroy() { drawn.destroy(); } };
}

/* Print asks for two things. A redraw, because the page is a different
 * width on paper and a canvas sized for the screen prints cropped - and the
 * numbers, which sit in a closed <details> that a printer would leave out.
 * Every one of them is opened for the print and put back afterwards, so
 * paper carries the same figures as the screen. */
let openedForPrint = [];

window.addEventListener("beforeprint", () => {
  openedForPrint = Array.from(document.querySelectorAll("details.chart-table"))
    .filter((d) => !d.open);
  openedForPrint.forEach((d) => { d.open = true; });
  live.forEach((instance) => {
    try { instance.resize(); } catch (e) { /* a chart that is already gone */ }
  });
});

window.addEventListener("afterprint", () => {
  openedForPrint.forEach((d) => { d.open = false; });
  openedForPrint = [];
});
