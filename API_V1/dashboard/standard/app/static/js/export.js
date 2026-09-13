/* ==========================================================================
 *  Print and export.
 *
 *  Three ways out of a view, all reachable from the top bar:
 *
 *    Print   → window.print(); print.css hides what is not content, and a
 *              `beforeprint` listener here fills the header line with
 *              view / project / language / period / search / date, so a
 *              printout says what it shows. It also redraws what does not
 *              redraw itself: a Chart.js canvas keeps its screen size on
 *              paper unless it is resized, and a Leaflet map drawn at one
 *              width shows grey tiles at another until invalidateSize().
 *
 *    PDF     → html2canvas draws <main> to a canvas, jsPDF slices it onto
 *              A4 pages - not a single squashed screenshot; slicing keeps
 *              the text readable. Map tiles from
 *              a foreign host without CORS taint the canvas and make
 *              toDataURL throw - then we draw again WITHOUT the tile pane
 *              and say so in a notice rather than failing.
 *
 *    CSV/JSON→ links to /api/export/<view>.csv|json carrying the current
 *              URL parameters, refreshed whenever the menu opens, so the
 *              file holds what the screen holds. The endpoint runs each
 *              view's own query function (app/routers/api_export.py).
 *
 *  html2canvas and jsPDF are vendored (static/vendor/, see VENDOR.md) and
 *  loaded only when a PDF is asked for - most sessions never need them.
 *
 *  Markup (the Jinja macro `print_export_buttons(view)` emits it):
 *
 *    <button type="button" data-print>Print</button>
 *    <details class="export-menu" data-export data-view="diagrams">
 *      <summary>Export</summary>
 *      <button type="button" data-export-pdf>PDF</button>
 *      <a data-export-format="csv" href="#">CSV</a>
 *      <a data-export-format="json" href="#">JSON</a>
 *      <p class="export-notice" aria-live="polite"></p>
 *    </details>
 * ========================================================================== */

import { announce } from "./a11y.js";

const VENDOR = {
  html2canvas: "/static/vendor/html2canvas/html2canvas.min.js",
  jspdf: "/static/vendor/jspdf/jspdf.umd.min.js",
};

const loaded = new Map();

function loadScript(src) {
  if (loaded.has(src)) return loaded.get(src);
  const p = new Promise((resolve, reject) => {
    const s = document.createElement("script");
    s.src = src;
    s.async = true;
    s.onload = () => resolve();
    s.onerror = () => reject(new Error(`could not load ${src}`));
    document.head.appendChild(s);
  });
  loaded.set(src, p);
  return p;
}

/* What the export links carry: everything in the URL. state.js mirrors
 * q/project/language/timeframe/page/tab into the URL with replaceState,
 * so reading location.search is reading the state. */
export function currentParams() {
  return new URLSearchParams(window.location.search);
}

export function exportUrl(view, format, params) {
  const p = new URLSearchParams(params || currentParams());
  const qs = p.toString();
  return `/api/export/${encodeURIComponent(view)}.${format}${qs ? "?" + qs : ""}`;
}

/* UTC, like the server's. The name in Content-Disposition is the one the
 * browser actually saves under; this one only shows in the menu, and two
 * clocks producing two names for one file is a bug report waiting to be
 * written. */
export function stamp(date) {
  const d = date || new Date();
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getUTCFullYear()}${pad(d.getUTCMonth() + 1)}${pad(d.getUTCDate())}`
       + `-${pad(d.getUTCHours())}${pad(d.getUTCMinutes())}`;
}

export function exportFilename(view, ext, params) {
  const p = params || currentParams();
  // Character for character api_export._slug().
  const clean = (s) => (String(s === null || s === undefined ? "" : s)
    .replace(/[^A-Za-z0-9_-]+/g, "_").slice(0, 40) || "all");
  return `xtracting-${clean(view)}-${clean(p.get("project"))}-${clean(p.get("language"))}-${stamp()}.${ext}`;
}

/* ── The header line ──────────────────────────────────────────────────── */

function text(node) {
  return node ? (node.textContent || "").replace(/\s+/g, " ").trim() : "";
}

/* THE PERIOD AS THE SCREEN SPELLS IT.
 *
 * The URL carries a token - `timeframe=1y` - and the toolbar prints what it
 * means: "Oct 2025 - Sep 2026". A printout headed "Timeframe: 1y" makes the
 * reader decode an internal token to know which twelve months they are
 * holding, and a page moved back with ◀ carries the same token for a
 * different year. So the caption wins wherever there is one; the token is
 * the fallback for a view without a period toolbar. */
function periodLabel(params) {
  const caption = text(document.querySelector("[data-period-caption]"));
  return caption || params.get("timeframe") || "";
}

/* Which tab, for the views that have one. The tab strip itself does not
 * print (it is a control), so without this a printout of the Log's run
 * history is indistinguishable from one of its problems. */
function tabLabel() {
  const tab = document.querySelector('[role="tab"][aria-selected="true"], .tab.is-active');
  if (!tab) return "";
  const copy = tab.cloneNode(true);
  // The count badge is a live number about the screen, not part of the name.
  copy.querySelectorAll(".tab-count, .visually-hidden").forEach((n) => n.remove());
  return text(copy);
}

/* What was searched for. `q` on most views; the Query page has no single
 * term - it searches for words within a distance of a place, and those live
 * in their own parameters. */
function searchLabel(params) {
  const parts = [];
  if (params.get("q")) parts.push(params.get("q"));
  if (params.get("terms")) parts.push(params.get("terms").split(",").join(", "));
  const address = params.get("address");
  const lat = params.get("lat");
  const lng = params.get("lng");
  if (address) parts.push(`near ${address}`);
  else if (lat && lng) parts.push(`near ${lat}, ${lng}`);
  if ((address || (lat && lng)) && params.get("km")) parts.push(`within ${params.get("km")} km`);
  return parts.join(" - ");
}

/* Fill the print header line. print.css shows it; on screen it is hidden. */
export function fillPrintHeader() {
  const p = currentParams();
  const parts = [];
  const page = document.querySelector("[data-print-title]") || document.querySelector("[data-print-view]");
  const name = page ? (page.dataset.printTitle || page.dataset.printView) : "";
  if (name) parts.push(name);
  const tab = tabLabel();
  if (tab) parts.push(tab);
  if (p.get("project")) parts.push(`Project: ${p.get("project")}`);
  if (p.get("language")) parts.push(`Language: ${p.get("language")}`);
  // WHICH SLICE, ON THE PAPER TOO. A printed page that is a subset has to
  // say so on the line that says what it is: the notice in the body prints
  // as well, but a sheet that has been photocopied out of its context is
  // read from this line and nothing else.
  if (p.get("perspective")) {
    const level = p.get("min_importance");
    parts.push(`Perspective: ${p.get("perspective")}${level ? ` (${level} and above)` : ""}`);
  }
  const period = periodLabel(p);
  if (period) parts.push(`Period: ${period}`);
  const from = p.get("from") || p.get("date_from");
  const to = p.get("to") || p.get("date_to");
  if (from || to) parts.push(`Dates: ${from || "…"} to ${to || "…"}`);
  const search = searchLabel(p);
  if (search) parts.push(`Search: ${search}`);
  parts.push(`Printed: ${new Date().toLocaleString()}`);
  document.querySelectorAll("[data-print-header]").forEach((h) => { h.textContent = parts.join(" - "); });
  return parts.join(" - ");
}

/* ── Redrawing for a different page width ─────────────────────────────── */

/* Leaflet has no registry of its maps and a container element does not lead
 * back to the map that owns it, so the only way to reach them all is to be
 * told as they are made. `addInitHook` is Leaflet's own way of asking for
 * that. map.js and heatmap.js already invalidate their own map on
 * `beforeprint`; what this catches is the maps they do not own - the full
 * map inside the Query page's Enlarge dialog, which is built the moment it
 * is opened, long after those listeners were registered. */
const leafletMaps = new Set();
let hooked = false;

function watchLeaflet() {
  const L = window.L;
  if (hooked || !L || !L.Map || typeof L.Map.addInitHook !== "function") return;
  hooked = true;
  L.Map.addInitHook(function registerForPrint() {
    leafletMaps.add(this);
    this.on("unload", () => leafletMaps.delete(this));
  });
}

/* Chart.js does not react to `print` media: the canvas keeps the pixel size
 * it was laid out at on screen and prints cropped or stretched. Every chart
 * is asked to resize itself against the width it now has. `Chart.getChart`
 * is the public way from a canvas back to its chart, so this reaches charts
 * built after charts.js registered its own listener too - and a second
 * resize of an already-correct chart costs nothing. */
export function prepareVisuals() {
  watchLeaflet();
  const Chart = window.Chart;
  if (Chart && typeof Chart.getChart === "function") {
    document.querySelectorAll("canvas").forEach((canvas) => {
      try {
        const chart = Chart.getChart(canvas);
        if (chart) chart.resize();
      } catch (e) { /* a canvas that is not a chart, or a chart already gone */ }
    });
  }
  leafletMaps.forEach((map) => {
    try { map.invalidateSize({ animate: false }); } catch (e) { /* removed map */ }
  });
}

/* ── An open dialog on paper ──────────────────────────────────────────── */

/* A modal <dialog> lives in the TOP LAYER. The browser lays it out against
 * the viewport whatever an author writes - `position: static` computes to
 * `absolute` there, and `inset: auto` resolves to the top left corner of the
 * page box - so on paper it lands over the page behind it, and a list of two
 * hundred rows is cut off at the first page boundary instead of continuing
 * on the second. No stylesheet can fix that.
 *
 * So the contents are COPIED into the flow of <main> for the length of the
 * print and removed again afterwards, where the ordinary page-break rules
 * apply. The copy carries `hidden` and `aria-hidden` and no ids at all: the
 * real dialog is still the one a screen reader reads and the one every
 * `aria-labelledby` points at, and a print that is cancelled between the two
 * events leaves nothing visible behind.
 *
 * The page BEHIND the dialog does not print: it is inert and covered by a
 * backdrop, and printing what the reader cannot see is not printing what
 * they are looking at. The header line stays, because that is what says
 * which project and period the rows belong to. */
const PRINT_COPY = "print-dialog-copy";
const PRINT_BODY = "has-print-dialog";

export function copyOpenDialogIntoFlow() {
  removeDialogCopies();
  // THE TOP ONE, not the first. Dialogs stack (js/dialog.js): a click on a
  // bar inside an enlarged chart opens the rows OVER it, and both are open
  // at once. What the reader is looking at - and therefore what belongs on
  // the paper - is the last one.
  const dialogs = document.querySelectorAll("dialog[open]");
  const open = dialogs[dialogs.length - 1];
  const main = document.querySelector("main");
  if (!open || !main) return null;
  const copy = document.createElement("section");
  copy.className = `card ${PRINT_COPY}`;
  copy.setAttribute("data-print-dialog", "");
  copy.setAttribute("aria-hidden", "true");
  // `hidden`, not a class: print.css is loaded with media="print", so a
  // `@media screen` rule in it never runs and could not hide the copy. The
  // UA's `[hidden] { display: none }` is not !important, so the print rule
  // `.print-dialog-copy { display: block !important }` still wins on paper -
  // and a print that is cancelled leaves nothing visible behind.
  copy.hidden = true;
  Array.from(open.children).forEach((child) => copy.appendChild(child.cloneNode(true)));
  // A CLONED CANVAS IS A BLANK CANVAS. cloneNode copies the element, never
  // the bitmap, so a dialog holding a chart (the Diagrams enlarge popup)
  // would print an empty white box where the picture is. The pixels are
  // taken as an image instead, from the live canvas, in the order the two
  // lists share.
  const live = open.querySelectorAll("canvas");
  copy.querySelectorAll("canvas").forEach((blank, i) => {
    const source = live[i];
    if (!source) return;
    let drawn = null;
    try {
      drawn = document.createElement("img");
      drawn.src = source.toDataURL("image/png");
    } catch (e) {
      return;   // a tainted canvas: leave the blank rather than throw
    }
    drawn.alt = source.getAttribute("aria-label") || "";
    drawn.style.width = `${source.clientWidth}px`;
    drawn.style.maxWidth = "100%";
    blank.replaceWith(drawn);
  });
  // Two elements with one id is invalid, and the second one is what an
  // `aria-labelledby` in the real dialog might then be pointed at.
  copy.querySelectorAll("[id]").forEach((node) => node.removeAttribute("id"));
  main.appendChild(copy);
  // A class rather than `main:has(.print-dialog-copy)`: one selector fewer to
  // wonder about, and it works in a browser without :has().
  document.body.classList.add(PRINT_BODY);
  return copy;
}

export function removeDialogCopies() {
  document.querySelectorAll(`.${PRINT_COPY}`).forEach((node) => node.remove());
  document.body.classList.remove(PRINT_BODY);
}

/* ── PDF ──────────────────────────────────────────────────────────────── */

/* What goes into the PDF: the main column, and any dialog that is open.
 * A drilldown dialog is appended to <body>, not to <main>, so exporting
 * only <main> while the reader is looking at the dialog would hand them a
 * PDF of the page behind it. */
function printRoots(root) {
  const roots = [root];
  document.querySelectorAll("dialog[open]").forEach((d) => {
    if (!root.contains(d)) roots.push(d);
  });
  return roots;
}

async function drawCanvas(root, ignoreTiles, scale) {
  const html2canvas = window.html2canvas;
  const canvas = await html2canvas(root, {
    useCORS: true,
    allowTaint: false,
    scale: scale || Math.min(2, window.devicePixelRatio || 1),
    backgroundColor: "#ffffff",
    logging: false,
    ignoreElements: (node) => {
      if (node.closest && node.closest(".no-print, .no-export, .typeahead-popup")) return true;
      if (node.closest && node.closest("dialog") && !node.closest("dialog[open]")) return true;
      if (ignoreTiles && node.classList && node.classList.contains("leaflet-tile-pane")) return true;
      return false;
    },
  });
  // toDataURL is where a tainted canvas throws - do it once, up front, so
  // the retry happens before any page is written.
  const probe = document.createElement("canvas");
  probe.width = 1; probe.height = 1;
  probe.getContext("2d").drawImage(canvas, 0, 0, 1, 1, 0, 0, 1, 1);
  probe.toDataURL("image/png");
  return canvas;
}

/* Render `roots` into A4 pages. Returns the jsPDF document. */
async function renderPdf(roots, { ignoreTiles, scale }) {
  await Promise.all([loadScript(VENDOR.html2canvas), loadScript(VENDOR.jspdf)]);
  if (!window.html2canvas || !window.jspdf) throw new Error("PDF libraries missing");
  const { jsPDF } = window.jspdf;

  const canvases = [];
  for (const root of roots) canvases.push(await drawCanvas(root, ignoreTiles, scale));

  const pdf = new jsPDF({ orientation: "portrait", unit: "mm", format: "a4" });
  const pageW = pdf.internal.pageSize.getWidth();
  const pageH = pdf.internal.pageSize.getHeight();
  const margin = 10;
  const usableW = pageW - 2 * margin;
  const usableH = pageH - 2 * margin;

  let pageNo = 0;
  canvases.forEach((canvas) => {
    if (!canvas.width || !canvas.height) return;
    const pxPerMm = canvas.width / usableW;
    const sliceHeightPx = Math.max(1, Math.floor(usableH * pxPerMm));
    let y = 0;
    while (y < canvas.height) {
      const h = Math.min(sliceHeightPx, canvas.height - y);
      const slice = document.createElement("canvas");
      slice.width = canvas.width;
      slice.height = h;
      slice.getContext("2d").drawImage(canvas, 0, y, canvas.width, h, 0, 0, canvas.width, h);
      if (pageNo > 0) pdf.addPage();
      pdf.addImage(slice.toDataURL("image/jpeg", 0.92), "JPEG", margin, margin, usableW, h / pxPerMm);
      pdf.setFontSize(8);
      pdf.text(`${pageNo + 1}`, pageW - margin, pageH - 4, { align: "right" });
      y += h;
      pageNo += 1;
    }
  });
  return pdf;
}

function isTaintError(err) {
  const msg = String(err && (err.message || err));
  return (err && err.name === "SecurityError") || /tainted|cross-origin|insecure/i.test(msg);
}

export async function exportPdf(opts = {}) {
  const root = opts.root || document.querySelector("main") || document.body;
  const view = opts.view || (document.querySelector("[data-export]") || {}).dataset?.view || "page";
  const notice = opts.notice || (() => {});
  fillPrintHeader();
  prepareVisuals();
  root.classList.add("is-exporting");
  const roots = printRoots(root);
  try {
    let pdf;
    try {
      pdf = await renderPdf(roots, { ignoreTiles: false, scale: opts.scale });
    } catch (err) {
      if (!isTaintError(err)) throw err;
      notice("Map tiles come from a host without CORS and were left out of the PDF.");
      pdf = await renderPdf(roots, { ignoreTiles: true, scale: opts.scale });
    }
    const name = exportFilename(view, "pdf");
    pdf.save(name);
    announce(`PDF ${name} ready`);
    return name;
  } finally {
    root.classList.remove("is-exporting");
  }
}

/* ── Wiring ───────────────────────────────────────────────────────────── */

export function initExport(scope) {
  const doc = scope || document;

  doc.querySelectorAll("[data-print]").forEach((btn) => {
    btn.addEventListener("click", () => {
      // beforeprint fires for the print dialog too, but Safari has been
      // known not to; doing it here as well is idempotent.
      fillPrintHeader();
      prepareVisuals();
      copyOpenDialogIntoFlow();
      window.print();
      // Chromium's window.print() returns when the dialog closes and
      // afterprint has already fired; calling this again costs nothing and
      // covers a browser that fires neither.
      removeDialogCopies();
    });
  });

  doc.querySelectorAll("[data-export]").forEach((menu) => {
    const view = menu.dataset.view || "page";
    const noticeEl = menu.querySelector(".export-notice");
    const notice = (text_) => { if (noticeEl) noticeEl.textContent = text_; announce(text_); };

    function refreshLinks() {
      menu.querySelectorAll("[data-export-format]").forEach((a) => {
        const fmt = a.dataset.exportFormat;
        a.href = exportUrl(view, fmt);
        a.setAttribute("download", exportFilename(view, fmt));
      });
    }
    refreshLinks();
    menu.addEventListener("toggle", refreshLinks);
    window.addEventListener("popstate", refreshLinks);
    document.addEventListener("state:change", refreshLinks);

    const pdfBtn = menu.querySelector("[data-export-pdf]");
    if (pdfBtn) {
      pdfBtn.addEventListener("click", async () => {
        pdfBtn.disabled = true;
        notice("Preparing PDF…");
        try {
          await exportPdf({ view, notice });
          if (noticeEl && /Preparing/.test(noticeEl.textContent)) noticeEl.textContent = "";
        } catch (err) {
          console.warn("export: PDF failed", err);
          notice("PDF export failed. Use Print and choose \"Save as PDF\" instead.");
        } finally {
          pdfBtn.disabled = false;
        }
      });
    }
  });

  window.addEventListener("beforeprint", () => {
    fillPrintHeader();
    prepareVisuals();
    copyOpenDialogIntoFlow();
  });
  window.addEventListener("afterprint", removeDialogCopies);
  watchLeaflet();
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", () => initExport(document));
} else {
  initExport(document);
}
