/* ==========================================================================
 *  The drilldown: the rows behind one clicked point, as a TABLE.
 *
 *  THE SHELL IS NOT HERE. A second, slightly different <dialog>
 *  implementation of its own - with a backdrop that closes it and an
 *  Escape that closes it - would give the product two dialog shells and
 *  two answers to "how do I get out of this". There is one
 *  (js/dialog.js): a title, an X in the top right of every dialog, a
 *  Cancel that does the same thing, focus trapped and given back, no light
 *  dismiss, and a second dialog that opens OVER the first instead of
 *  replacing it. Everything below is about the ROWS.
 *
 *  ── THE SHAPE ────────────────────────────────────────────────────────
 *
 *    running number | the source | the entity | the subject columns |
 *    the date | the free text, last and widest
 *
 *  One row per line, in a real table with a header, because that is what
 *  lets an eye run down one column and compare - which a paragraph per row
 *  never allowed. The columns come from the SERVER, per tab
 *  (app/charts/drilldown.py COLUMNS), so the header and the cells cannot
 *  drift apart.
 *
 *  There is no id column. `src:battery` and `ent:1822949` identify a row to
 *  a machine and say nothing to a reader; the first link carries the
 *  DOMAIN and the TITLE as text instead, so twenty rows can be scanned
 *  without hovering over any of them.
 *
 *  ── THE COUNTER ──────────────────────────────────────────────────────
 *
 *  "Row 87-106 of 312 - 120 loaded", stuck to the top of the dialog body
 *  so it survives scrolling. Two facts, both of them missing before: where
 *  you are, and how much there is. The total rides on the same statement
 *  that fetched the rows (`count(*) OVER ()`), so it costs no second
 *  query. It updates on scroll, throttled to an animation frame, and it is
 *  NOT a live region: a screen reader announcing eighty positions while
 *  somebody scrolls is worse than silence. The announcement happens when
 *  LOADING finishes, once.
 *
 *  ── TWO LEVELS, NEVER THREE ──────────────────────────────────────────
 *
 *  A drilldown is opened from a chart - and from an ENLARGED chart, which
 *  is itself a dialog, so the drilldown is the second level and the
 *  enlarged chart is still there underneath when it closes (that is the
 *  whole point: a person is exploring one chart, and closing their context
 *  to show them a detail throws away what they were doing). A third level
 *  would be a stack nobody can hold in their head, so a drilldown opened
 *  while one is already up REPLACES it rather than piling onto it.
 * ========================================================================== */

import { openDialog, openDialogs } from "./dialog.js";
import { announce } from "./a11y.js";
import { valence, relevance, noData } from "./palette.js";
import { viewIcon } from "./icons.js";

/* The drilldown that is on screen, if any - the second level, so that a new
 * one replaces it instead of becoming a third. */
let openRows = null;

/* How many lines of the free text are shown before the control that opens
 * the rest. Two: enough to tell one reason from another, short enough that
 * twenty rows still fit on a screen. Kept in step with diagrams.css
 * (`--drilldown-lines`), which does the clamping. */
export const TEXT_LINES = 2;

function el(tag, className, text) {
  const e = document.createElement(tag);
  if (className) e.className = className;
  if (text !== undefined && text !== null) e.textContent = text;
  return e;
}

/* "1 row", not "1 rows". The dialog says the count three times - in the
 * subtitle, in the footer and to a screen reader - and a number and a noun
 * that disagree read as a bug in the page. */
function rowsWord(count) {
  const n = Number(count) || 0;
  return `${n.toLocaleString()} ${n === 1 ? "row" : "rows"}`;
}

function fmt(n) {
  return (Number(n) || 0).toLocaleString();
}

/* ── The colour beside a word ──────────────────────────────────────────── */

/* THE WORD STAYS IN THE CELL; THE COLOUR IS BESIDE IT.
 *
 * The server says which scale a value is on and where on it
 * (app/charts/drilldown.py), never what colour it is; the hex comes from
 * the stylesheet through js/palette.js. So a rating is the same red on
 * this table, on its chart and on its legend, and the table still reads
 * with the colour taken away - on a printer, to a dichromat, and to
 * anybody reading the words rather than the picture. */
function swatchFor(part) {
  if (!part || !part.scale) return null;
  let colour = null;
  if (part.scale === "valence") colour = valence(part.step);
  else if (part.scale === "relevance") colour = relevance(part.step);
  else if (part.scale === "none") colour = noData();
  if (!colour) return null;
  const dot = el("span", "dd-swatch");
  dot.style.setProperty("--swatch", colour);
  // Decoration: the word beside it is the value, and a screen reader
  // reading "square, Excellent" has been told the same thing twice.
  dot.setAttribute("aria-hidden", "true");
  return dot;
}

/* ── One cell ──────────────────────────────────────────────────────────── */

function partNode(part, links) {
  // ONE BOX PER PART, so a swatch is never left on the line above the word
  // it belongs to: a colour with no word beside it is the one thing the
  // "colour is beside the word, never instead of it" rule forbids.
  const box = el("span", "dd-part");
  const dot = swatchFor(part);
  if (dot) box.appendChild(dot);
  const text = part.text || "";
  if (part.entity && text && links && links.entity) {
    const a = el("a", "dd-link", text);
    a.href = links.entity(part.entity);
    box.appendChild(a);
  } else if (part.iso) {
    const time = el("time", null, text);
    time.dateTime = part.iso;
    box.appendChild(time);
  } else {
    box.appendChild(document.createTextNode(text));
  }
  return box;
}

/* A cell whose value the archive never recorded. An empty box reads as a
 * rendering fault; a dash says "the source did not say", which is a fact.
 *
 * The dash is for the eye and the words are for the ear: a screen reader
 * announcing "en dash" has been told nothing, and a title attribute is a
 * tooltip - unreachable on a touch screen, which is the reason this
 * product does not put content in one. */
function nothing() {
  const box = el("span", "dd-none");
  const dash = el("span", null, "-");
  dash.setAttribute("aria-hidden", "true");
  box.appendChild(dash);
  box.appendChild(el("span", "visually-hidden", "not stated"));
  return box;
}

function cellNode(column, parts, links) {
  const td = el("td", `dd-cell dd-${column.key}`);
  const shown = (parts || []).filter((p) => p && p.text);
  if (!shown.length) {
    td.appendChild(nothing());
    return td;
  }
  shown.forEach((part, i) => {
    if (i) td.appendChild(document.createTextNode(column.sep || ", "));
    td.appendChild(partNode(part, links));
  });
  return td;
}

/* THE FREE TEXT EXPANDS IN PLACE.
 *
 * Not a tooltip - unreachable on a touch screen and unreadable at length -
 * and not a third dialog, which the product does not allow and nobody
 * wants for one paragraph. Two lines and a control; the control says which
 * of the two states pressing it produces, and the expanded state survives
 * the next page of rows because the rows already on screen are never
 * redrawn. */
function wideCell(column, parts, expanded, key, register) {
  const td = el("td", "dd-cell dd-wide");
  const text = (parts || []).map((p) => p.text).filter(Boolean).join(column.sep || ", ");
  if (!text) {
    td.appendChild(nothing());
    return td;
  }
  const body = el("p", "dd-text", text);
  td.appendChild(body);
  const open = expanded.has(key);
  body.classList.toggle("is-clamped", !open);
  const more = el("button", "button button--quiet dd-more", open ? "Show less" : "Show more");
  more.type = "button";
  more.hidden = true;
  more.addEventListener("click", () => {
    const nowOpen = body.classList.contains("is-clamped");
    body.classList.toggle("is-clamped", !nowOpen);
    more.textContent = nowOpen ? "Show less" : "Show more";
    if (nowOpen) expanded.add(key); else expanded.delete(key);
  });
  td.appendChild(more);
  // WHETHER THERE IS MORE IS RE-DECIDED, NOT DECIDED ONCE. A button that
  // opens two lines into two lines is a thing to press that does nothing -
  // and a text that BECOMES clipped, because the next page brought a wider
  // column or the window narrowed, needs the control it did not need
  // before. The caller re-runs this after every page and after a resize.
  register({ body, more });
  return td;
}

/* Show the control on the texts that are actually cut off, hide it on the
 * ones that are not, and leave whatever the reader has opened open. */
function refitText(cells) {
  cells.forEach(({ body, more }) => {
    if (!body.isConnected) return;
    const open = !body.classList.contains("is-clamped");
    more.hidden = !open && body.scrollHeight - body.clientHeight <= 2;
  });
}

/* ── The counter ───────────────────────────────────────────────────────── */

/* WHERE YOU ARE AND HOW MUCH THERE IS, in one line.
 *
 * Every edge case here is the difference between a line that reads as
 * careful and one that reads as careless:
 *   fewer rows than a screen  "12 rows" - a position nobody needs
 *   everything loaded         no "loaded" half; it only matters while it
 *                             is incomplete
 *   the end reached           "end of 312", so a person knows the list
 *                             stopped rather than the loading breaking
 */
export function counterText(state) {
  const { total, loaded, first, last, more, scrolls } = state;
  if (!loaded) return "";
  // Everything on one screen: a position is an answer to a question nobody
  // has. "12 rows" is the whole of what there is to say. Decided by
  // MEASURING whether the body scrolls, not by guessing from a page size -
  // the dialog is as tall as the window it is in.
  if (!more && !scrolls) return rowsWord(total);
  const position = first && last
    ? `Row ${fmt(first)}-${fmt(last)} of ${fmt(total)}`
    : rowsWord(total);
  // The "loaded" half only matters while it is incomplete; when it is not,
  // saying so once tells a person the list stopped rather than the loading
  // breaking.
  return more ? `${position} - ${fmt(loaded)} loaded` : `${position} - end of ${fmt(total)}`;
}

export function openDrilldown(opts) {
  // Two levels only. Whatever opened this one (a card, or a bar inside the
  // enlarged chart) stays where it is; the drilldown that was on top does
  // not.
  if (openRows) {
    const stale = openRows;
    openRows = null;
    stale.dismiss();
  }

  const actions = [];
  if (opts.csvUrl) {
    const a = el("a", "button button--secondary", "Download CSV");
    a.href = opts.csvUrl;
    a.setAttribute("download", "");
    actions.push(a);
  }
  const more = el("button", "button button--secondary dialog-more", "Load more");
  more.type = "button";
  actions.push(more);

  const handle = openDialog({
    title: opts.title,
    subtitle: opts.subtitle,
    wide: opts.wide !== false,
    actions,
    // "Close", not "Cancel": there is nothing here to cancel - the dialog
    // reads rows out of the archive and changes nothing - and a button
    // offering to undo what was never done is a question the reader has to
    // stop and answer.
    cancel: "Close",
    onClose: () => {
      if (observer) observer.disconnect();
      handle.body.removeEventListener("scroll", onScroll);
      window.removeEventListener("resize", refitSoon);
      if (openRows === handle) openRows = null;
      if (opts.onClose) opts.onClose();
    },
  });
  openRows = handle;
  // Which level this is, for the stylesheet and for a test that has to be
  // able to say "the enlarged chart is still open underneath".
  handle.dialog.dataset.drilldown = String(openDialogs());

  // The counter, stuck to the top of the body. NOT a live region - see the
  // note at the top of this file.
  const counter = el("p", "drilldown-count");
  counter.dataset.drilldownCount = "";
  handle.body.appendChild(counter);

  // NOT `.table-scroll`. An `overflow-x: auto` box is a scroll container in
  // BOTH axes (the other axis computes to `auto` with it), and a sticky
  // table header then sticks to that box - which never scrolls vertically -
  // instead of to the dialog body that does. The header ended up pushed
  // down over the first row. The body is the one scroller here, in both
  // directions, and the header sticks to it.
  const scroller = el("div", "drilldown-scroll");
  const table = el("table", "table drilldown-table");
  // ONE NUMBER, IN ONE PLACE. The stylesheet does the clamping and this is
  // what it clamps to; diagrams.css carries the same figure only as the
  // fallback for a page rendered without this module.
  table.style.setProperty("--drilldown-lines", String(TEXT_LINES));
  const caption = el("caption", null, opts.rowsLabel || "Rows");
  table.appendChild(caption);
  const head = el("thead");
  const headRow = el("tr");
  head.appendChild(headRow);
  table.appendChild(head);
  const body = el("tbody");
  table.appendChild(body);
  scroller.appendChild(table);
  handle.body.appendChild(scroller);
  const sentinel = el("div", "drilldown-sentinel");
  sentinel.setAttribute("aria-hidden", "true");
  handle.body.appendChild(sentinel);

  let page = 0;
  let hasMore = true;
  let loading = false;
  let loaded = 0;
  let total = 0;
  let columns = null;
  let links = { source: true, entity: false };
  let observer = null;
  const expanded = new Set();
  // Every free-text cell on screen, so the "Show more" controls can be
  // re-decided when the columns change width.
  const texts = [];
  let refitting = false;
  function refitSoon() {
    if (refitting) return;
    refitting = true;
    requestAnimationFrame(() => { refitting = false; refitText(texts); });
  }
  window.addEventListener("resize", refitSoon);

  function drawHead() {
    headRow.textContent = "";
    const number = el("th", "dd-num", "#");
    number.scope = "col";
    headRow.appendChild(number);
    const source = el("th", "dd-source", "Source");
    source.scope = "col";
    headRow.appendChild(source);
    if (links.entity) {
      const entity = el("th", "dd-entity", "Entity");
      entity.scope = "col";
      headRow.appendChild(entity);
    }
    (columns || []).forEach((column) => {
      const th = el("th", `dd-${column.key}${column.wide ? " dd-wide" : ""}`, column.label);
      th.scope = "col";
      /* WHAT AN ABBREVIATION IN A HEADING MEANS, on hover and in the
       * accessible name. "Outlook ST / LT" is two readings of one insight
       * and four letters nobody is born knowing; the words do not fit in
       * the column, so they stand beside it.
       *
       * `title` for a pointer, `aria-label` for a screen reader: the
       * sentence is never ONLY in a hover, which is the rule this product
       * keeps everywhere a tooltip appears. */
      if (column.hint) {
        const mark = el("span", "dd-hint", "i");
        mark.title = column.hint;
        mark.setAttribute("role", "img");
        mark.setAttribute("aria-label", column.hint);
        th.appendChild(mark);
      }
      headRow.appendChild(th);
    });
  }

  /* THE FIRST LINK CARRIES THE DOMAIN AND THE TITLE AS TEXT, the second
   * goes to that domain's own diagrams, and the third to the entity's -
   * with the first carrying the words a person is actually looking for,
   * not a bare icon. */
  function sourceCell(row) {
    const td = el("td", "dd-cell dd-source");
    const link = row.link || {};
    const domain = link.domain || "";
    const title = link.title || "";
    if (link.uri) {
      const a = el("a", "dd-doc");
      a.href = link.uri;
      a.rel = "noopener noreferrer";
      a.target = "_blank";
      a.appendChild(el("strong", "dd-domain", domain || link.uri));
      if (title) {
        a.appendChild(document.createTextNode(" - "));
        a.appendChild(document.createTextNode(title));
      }
      td.appendChild(a);
    } else if (domain || title) {
      td.appendChild(el("span", "dd-domain", domain || title));
    } else {
      td.appendChild(nothing());
    }
    if (domain && opts.links && opts.links.source) {
      // THE VIEW'S OWN GLYPH, cloned out of the top bar (static/js/icons.js),
      // so a link to Diagrams from a drilldown row and the Diagrams tab
      // itself cannot show two different pictures of one view. A word alone
      // in a dense table is a word the eye has to read; the glyph is what
      // makes it findable at a glance.
      const charts = el("a", "dd-scope");
      charts.href = opts.links.source(domain);
      const glyph = viewIcon("diagrams");
      if (glyph) charts.appendChild(glyph);
      charts.appendChild(document.createTextNode("Diagrams"));
      charts.setAttribute("aria-label", `Diagrams for ${domain}`);
      td.appendChild(charts);
    }
    return td;
  }

  function entityCell(row) {
    const td = el("td", "dd-cell dd-entity");
    const name = row.entity || "";
    if (!name) {
      td.appendChild(nothing());
      return td;
    }
    if (opts.links && opts.links.entity) {
      const a = el("a", "dd-link");
      a.href = opts.links.entity(name);
      // The same glyph the Diagrams tab carries: this link goes there, about
      // this entity.
      const glyph = viewIcon("diagrams");
      if (glyph) a.appendChild(glyph);
      a.appendChild(document.createTextNode(name));
      td.appendChild(a);
    } else {
      td.appendChild(document.createTextNode(name));
    }
    return td;
  }

  function drawRow(row, number) {
    const tr = el("tr", "drilldown-row");
    tr.dataset.row = String(number);
    const num = el("th", "dd-num", fmt(number));
    num.scope = "row";
    tr.appendChild(num);
    tr.appendChild(sourceCell(row));
    if (links.entity) tr.appendChild(entityCell(row));
    (columns || []).forEach((column) => {
      const parts = (row.cells || {})[column.key];
      tr.appendChild(column.wide
        ? wideCell(column, parts, expanded, `${number}:${column.key}`,
                   (cell) => texts.push(cell))
        : cellNode(column, parts, opts.links));
    });
    return tr;
  }

  /* Which rows are on screen. A linear scan from the top is fine for the
   * hundreds of rows a drilldown holds, and it runs at most once per
   * animation frame. */
  function visibleRange() {
    const rows = body.children;
    if (!rows.length) return [0, 0];
    const box = handle.body.getBoundingClientRect();
    const top = box.top + counter.offsetHeight;
    let first = 0;
    let last = 0;
    for (let i = 0; i < rows.length; i += 1) {
      const r = rows[i].getBoundingClientRect();
      if (r.bottom > top && r.top < box.bottom) {
        if (!first) first = Number(rows[i].dataset.row) || i + 1;
        last = Number(rows[i].dataset.row) || i + 1;
      }
    }
    if (!first) { first = 1; last = Math.min(loaded, 1); }
    return [first, last];
  }

  let ticking = false;
  function updateCounter() {
    const [first, last] = visibleRange();
    const scrolls = handle.body.scrollHeight - handle.body.clientHeight > 4;
    counter.textContent = counterText({ total, loaded, first, last, more: hasMore, scrolls });
    counter.hidden = !counter.textContent;
    // What the table's own sticky header has to clear. Measured rather than
    // guessed: the counter is one line on a wide window and two on a narrow
    // one, and a header that overlaps the first row hides a row.
    handle.body.style.setProperty("--dd-count-h",
      `${counter.hidden ? 0 : counter.offsetHeight}px`);
  }
  function onScroll() {
    if (ticking) return;
    ticking = true;
    requestAnimationFrame(() => { ticking = false; updateCounter(); });
  }
  handle.body.addEventListener("scroll", onScroll, { passive: true });

  async function loadNext() {
    if (loading || !hasMore) return;
    loading = true;
    more.disabled = true;
    handle.setBusy(true, page === 0 ? "Loading…" : "Loading more…");
    try {
      const res = await opts.load(page + 1);
      page += 1;
      const rows = (res && res.rows) || [];
      hasMore = !!(res && res.more);
      if (!columns) {
        columns = (res && res.columns) || [];
        links = (res && res.row_links) || links;
        drawHead();
      }
      // The running number counts up ACROSS pages: row 21 on page 2 is 21.
      // The offset is the
      // server's, so a page fetched out of order still numbers correctly.
      const start = (res && Number.isFinite(res.offset) ? res.offset : loaded) + 1;
      rows.forEach((row, i) => body.appendChild(drawRow(row, start + i)));
      loaded += rows.length;
      total = res && Number.isFinite(res.total) ? Math.max(res.total, loaded) : loaded;
      if (!loaded) {
        const tr = el("tr", "drilldown-empty");
        const td = el("td", null, opts.emptyText || "No rows in this range.");
        td.colSpan = headRow.children.length || 1;
        tr.appendChild(td);
        body.appendChild(tr);
      }
      updateCounter();
      // The new rows may have widened a column, which changes which texts
      // are cut off - including the ones that were already on screen.
      refitSoon();
      handle.setBusy(false, hasMore ? `${rowsWord(loaded)} shown, more available` : rowsWord(loaded));
      // ANNOUNCED WHEN LOADING FINISHES, and only then. The counter itself
      // changes on every scroll frame and says nothing out loud.
      announce(hasMore ? `${rowsWord(rows.length)} more loaded, ${fmt(loaded)} of ${fmt(total)}`
                       : `All ${rowsWord(loaded)} loaded`);
    } catch (err) {
      console.warn("drilldown: page failed", err);
      handle.setBusy(false, "Could not load rows. Try again.");
    } finally {
      loading = false;
      more.disabled = !hasMore;
      more.hidden = !hasMore;
    }
  }

  more.addEventListener("click", loadNext);

  if ("IntersectionObserver" in window) {
    observer = new IntersectionObserver((entries) => {
      if (entries.some((e) => e.isIntersecting)) loadNext();
    }, { root: handle.body, rootMargin: "120px" });
    observer.observe(sentinel);
  }

  loadNext();
  // A filter that changes starts at row 1 again: a position kept from the
  // question before is a lie about the answer in front of the reader.
  handle.reload = () => {
    body.textContent = "";
    expanded.clear();
    texts.length = 0;
    page = 0; hasMore = true; loaded = 0; total = 0; columns = null;
    return loadNext();
  };
  return handle;
}
