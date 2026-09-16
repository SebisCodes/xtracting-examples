/* ==========================================================================
 *  The row table: rows out of the archive, drawn as a TABLE, a page at a
 *  time as the reader scrolls.
 *
 *  ONE TABLE, TWO PLACES. The drilldown dialog (js/drilldown.js) mounts
 *  this in its body; the Tables view mounts it on the page. The columns
 *  come from the SERVER per table (app/charts/drilldown.py COLUMNS), so a
 *  row reads the same wherever it is drawn, and a person who has learned
 *  the dialog has learned the page.
 *
 *  ── THE SHAPE ────────────────────────────────────────────────────────
 *
 *    running number | the source | the entity | the subject columns |
 *    the date | the free text, last and widest
 *
 *  One row per line, in a real table with a header, because that is what
 *  lets an eye run down one column and compare - which a paragraph per row
 *  never allowed.
 *
 *  There is no id column. `src:battery` and `ent:1822949` identify a row to
 *  a machine and say nothing to a reader; the first link carries the
 *  DOMAIN and the TITLE as text instead, so twenty rows can be scanned
 *  without hovering over any of them.
 *
 *  ── THE COUNTER ──────────────────────────────────────────────────────
 *
 *  "Row 87-106 of 312 - 120 loaded", stuck to the top of the scroller so
 *  it survives scrolling. Two facts: where you are, and how much there is.
 *  The total comes with the first page and is kept when a later page
 *  carries none - a count is taken once, not once per page. It updates on
 *  scroll, throttled to an animation frame, and it is NOT a live region: a
 *  screen reader announcing eighty positions while somebody scrolls is
 *  worse than silence. The announcement happens when LOADING finishes,
 *  once.
 *
 *  ── A CELL THAT OPENS ────────────────────────────────────────────────
 *
 *  A listing whose rows are GROUPS (the entities in one spot of the
 *  Heatmap, the names on the Tables view sorted by how often they occur)
 *  counts rows in its Source cell instead of naming a document. That cell
 *  is a control: pressing it lists the rows behind it directly under the
 *  row, with a header of their own, newest first and a page at a time
 *  (expandCell / openNested below, `opts.expand`). In place, because a
 *  third dialog is not allowed and a link away would lose the list.
 *
 *  ── WHAT THE CALLER SUPPLIES ─────────────────────────────────────────
 *
 *    root       where the table and the sentinel go
 *    counterRoot where the counter goes (root by default)
 *    sourceColumn false leaves the Source column out - for a listing
 *               that is one document's rows
 *    scroller   the element that scrolls (the dialog body), or null for
 *               the window
 *    load(page) a promise of one page: {rows, columns, row_links, more,
 *               offset, total}
 *    status(busy, text)   the caller's own status line
 *    moreButton a "Load more" the caller placed (the dialog's footer);
 *               without one the table puts its own under the rows
 *    links      {source(domain), entity(name), rows(source)} - the hrefs
 *               a row's links point at; a link the caller leaves out is
 *               not drawn
 *    expand     {noun, label(row), load(row, page), emptyText} for group
 *               rows; `button` is the word on the control of a row that
 *               ALSO names a document, and `render(row, box, hooks)`
 *               builds what opens under such a row in place of the
 *               nested table (see openNested)
 * ========================================================================== */

import { announce } from "./a11y.js";
import { valence, relevance, noData } from "./palette.js";
import { viewIcon } from "./icons.js";

/* How many lines of the free text are shown before the control that opens
 * the rest. Two: enough to tell one reason from another, short enough that
 * twenty rows still fit on a screen. Kept in step with diagrams.css
 * (`--drilldown-lines`), which does the clamping. */
export const TEXT_LINES = 2;


/* http and https and nothing else. The address is a value out of the
 * archive, and an href takes any scheme it is given - a `javascript:` one
 * would run in this page the moment somebody clicked what looked like a
 * document. An address of another kind is shown as text. */
function isWebAddress(uri) {
  return /^https?:\/\//i.test(String(uri || "").trim());
}

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


/* ── The table ─────────────────────────────────────────────────────────── */

export function mountRows(root, opts) {
  const scrollBox = opts.scroller || null;
  const status = opts.status || (() => {});
  const counter = el("p", "drilldown-count");
  counter.dataset.drilldownCount = "";
  // The counter stands where the caller says - above the card that holds
  // the rows, on a page - and at the top of the root otherwise.
  (opts.counterRoot || root).appendChild(counter);

  // NOT `.table-scroll`. An `overflow-x: auto` box is a scroll container in
  // BOTH axes (the other axis computes to `auto` with it), and a sticky
  // table header then sticks to that box - which never scrolls vertically -
  // instead of to the scroller that does. The header would end up pushed
  // down over the first row. `opts.scroller` (the dialog body, the page's
  // one scroll box) is the one scroller, in both directions, and the
  // header sticks to it.
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
  root.appendChild(scroller);
  const sentinel = el("div", "drilldown-sentinel");
  sentinel.setAttribute("aria-hidden", "true");
  root.appendChild(sentinel);
  // "Load more", for a reader who would rather press than scroll. The
  // dialog places it in its footer and hands it in; on a page it stands
  // under the rows.
  let more = opts.moreButton || null;
  if (!more) {
    const foot = el("div", "rows-foot");
    more = el("button", "button button--secondary rows-more", "Load more");
    more.type = "button";
    foot.appendChild(more);
    root.appendChild(foot);
  }

  let page = 0;
  let hasMore = true;
  let loading = false;
  let loaded = 0;
  let total = 0;
  let columns = null;
  let links = { source: true, entity: false };
  let sourceLabel = "";
  let observer = null;
  // What the status line last said about the outer list, so a nested
  // load can hand it back.
  let lastStatus = "";
  // Sentinel element -> the function that loads the next page after it.
  const loaders = new Map();
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
    // "Source" for a listing of rows, each with the document it came from;
    // the server says otherwise for a listing of GROUPS, whose first cell
    // counts rows and names no document (`source_label`).
    if (opts.sourceColumn !== false) {
      const source = el("th", "dd-source", sourceLabel || "Source");
      source.scope = "col";
      headRow.appendChild(source);
    }
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
  function sourceCell(row, number) {
    const td = el("td", "dd-cell dd-source");
    const link = row.link || {};
    const domain = link.domain || "";
    const title = link.title || "";
    const opens = row.key !== undefined && Boolean(opts.expand);
    if (!link.uri && opens) {
      // A COUNT OF DOCUMENTS RATHER THAN ONE DOCUMENT, and it opens (see
      // expandCell): the rows behind it are listed under this row.
      td.appendChild(expandCell(row, number, title));
    } else if (isWebAddress(link.uri)) {
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
    } else if (domain || title || link.uri) {
      td.appendChild(el("span", "dd-domain", domain || title || link.uri));
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
    if (row.source && row.source.task && row.source.id && (domain || title || link.uri)
        && opts.links && opts.links.rows) {
      // EVERY ROW CAN REACH ITS DOCUMENT: the Tables view opens the document
      // with all its rows under it, so a reader who has found one row is
      // one press from everything the archive read out of the same page.
      // Not offered where the archive has no document row for the key - a
      // link to a page that can only say so is a dead end.
      const rows = el("a", "dd-scope dd-rows");
      rows.href = opts.links.rows(row.source);
      const glyph = viewIcon("tables");
      if (glyph) rows.appendChild(glyph);
      rows.appendChild(document.createTextNode("Rows"));
      rows.setAttribute("aria-label", `All rows of ${domain || title || "this document"}`);
      td.appendChild(rows);
    }
    if (link.uri && opens) {
      // A DOCUMENT THAT OPENS AS WELL AS LINKS. The Tables view marks every
      // document row with its key, and what opens under it is everything
      // the archive read out of that page (opts.expand.render). The link
      // to the document stays where every other row has it; the control
      // stands under it, on the line the other ways out of a row share.
      td.appendChild(expandCell(row, number, opts.expand.button || "All rows",
                                "every row of this document"));
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

  /* One row. `shape` is the columns and links the row is drawn with - the
   * listing's own by default, and a nested listing's for the rows under an
   * opened cell (expandCell), which have a header of their own. `prefix`
   * keeps the "Show more" state of a nested row apart from the outer row
   * with the same number. */
  function drawRow(row, number, shape = null, prefix = "") {
    const cols = shape ? shape.columns : columns;
    const rowLinks = shape ? shape.links : links;
    const tr = el("tr", "drilldown-row");
    tr.dataset.row = String(number);
    const num = el("th", "dd-num", fmt(number));
    num.scope = "row";
    tr.appendChild(num);
    // Every row of a listing names its document - except where the listing
    // IS one document's rows (the Tables view's opened document), and the
    // name over the table has said it once.
    if (opts.sourceColumn !== false) tr.appendChild(sourceCell(row, `${prefix}${number}`));
    if (rowLinks.entity) tr.appendChild(entityCell(row));
    (cols || []).forEach((column) => {
      const parts = (row.cells || {})[column.key];
      tr.appendChild(column.wide
        ? wideCell(column, parts, expanded, `${prefix}${number}:${column.key}`,
                   (cell) => texts.push(cell))
        : cellNode(column, parts, opts.links));
    });
    return tr;
  }

  /* ── The rows behind one cell, listed under its row ────────────────── */

  /* "2 DOCUMENTS" OPENS, IN PLACE.
   *
   * A listing whose rows are GROUPS - the entities in a spot of the
   * Heatmap, each counted from several documents - ends every Source cell
   * in a number, and the reader's next question is which documents those
   * are. Not a third dialog (the product allows two, see js/drilldown.js)
   * and not a link away from the list they are reading: the cell is a
   * control, and pressing it lists the rows behind it directly under
   * the row, with a header of their own, the newest first, a page at a
   * time - the same paging the outer table does, with a "Load more" and a
   * sentinel that loads the next page as the reader scrolls to it.
   *
   * The caller supplies `opts.expand`: `load(row, page)` answers with the
   * same envelope every drilldown page has (columns, row_links, rows, more,
   * offset, total), and `label(row)` is the sentence over the nested rows.
   * A row opens when it carries `key` and no document of its own.
   *
   * Pressing the control again folds the rows away and keeps them: opening
   * a cell twice must not fetch twice, and a reader comparing two entities
   * opens and closes them more than once. */
  let nestedId = 0;
  function expandCell(row, number, title, said = `the ${title || "rows"} behind this row`) {
    const button = el("button", "dd-expand", null);
    button.type = "button";
    button.setAttribute("aria-expanded", "false");
    const mark = el("span", "dd-expand-mark");
    mark.setAttribute("aria-hidden", "true");
    button.appendChild(mark);
    button.appendChild(document.createTextNode(title || "Open"));
    nestedId += 1;
    const id = `dd-nested-${nestedId}`;
    button.setAttribute("aria-controls", id);
    // THE CONTROL SAYS WHAT PRESSING IT DOES, to a screen reader as well as
    // to the eye: "2 documents" alone is a number, "Show the 2 documents"
    // is an action.
    button.setAttribute("aria-label", `Show ${said}`);
    button.title = `Show ${title || "rows"}`;
    let nested = null;
    button.addEventListener("click", () => {
      const open = button.getAttribute("aria-expanded") !== "true";
      button.setAttribute("aria-expanded", String(open));
      button.setAttribute("aria-label", `${open ? "Hide" : "Show"} ${said}`);
      button.title = `${open ? "Hide" : "Show"} ${title || "rows"}`;
      if (!nested) {
        nested = openNested(row, number, id, button);
      } else {
        nested.hidden = !open;
        // What is on screen changed height, so which rows are visible did.
        updateCounter();
      }
    });
    return button;
  }

  /* The nested listing: one table row spanning the outer table, holding a
   * sentence, a table of its own and the controls that page it. Built once
   * per cell and inserted directly after the row it belongs to. */
  function openNested(row, number, id, button) {
    const tr = el("tr", "drilldown-nested");
    tr.id = id;
    const td = el("td", "drilldown-nested-cell");
    td.colSpan = headRow.children.length || 1;
    tr.appendChild(td);
    const region = el("div", "drilldown-nested-box");
    region.setAttribute("role", "region");
    const said = opts.expand.label ? opts.expand.label(row) : "";
    if (said) region.setAttribute("aria-label", said);
    if (said) region.appendChild(el("p", "drilldown-nested-title", said));
    if (opts.expand.render && row.link && row.link.uri) {
      /* THE CALLER BUILDS WHAT OPENS UNDER A DOCUMENT. One nested table is
       * the shape of a group row; a DOCUMENT opens into several - its
       * entities, its ratings, its events, each a table of its own - and
       * only the view knows how many. So for a row that names a document
       * the box under it is handed over, with the two things the view
       * cannot reach: the status line, and the counter, which has to be
       * re-measured when the box changes height. The nested table below is
       * not built for it; a group row always gets the nested table. */
      td.appendChild(region);
      button.closest("tr").after(tr);
      opts.expand.render(row, region, {
        status: (busy, text) => status(busy, busy ? text : (text || lastStatus)),
        resized: updateCounter,
      });
      return tr;
    }
    const table = el("table", "table drilldown-table drilldown-nested-table");
    table.style.setProperty("--drilldown-lines", String(TEXT_LINES));
    if (said) table.appendChild(el("caption", null, said));
    const nestedHead = el("thead");
    const nestedHeadRow = el("tr");
    nestedHead.appendChild(nestedHeadRow);
    table.appendChild(nestedHead);
    const nestedBody = el("tbody");
    table.appendChild(nestedBody);
    region.appendChild(table);
    // The line under the nested rows: where the loading stands, and the
    // control that fetches the next page for a reader who would rather
    // press than scroll.
    const foot = el("div", "drilldown-nested-foot");
    // `line`, not `status`: that name is the caller's status function, which
    // loadNested below reports to, and a local of the same name would
    // shadow it into a TypeError on the first nested page.
    const line = el("p", "drilldown-nested-count");
    foot.appendChild(line);
    const more = el("button", "button button--secondary drilldown-nested-more", "Load more");
    more.type = "button";
    more.hidden = true;
    foot.appendChild(more);
    region.appendChild(foot);
    const sentinel = el("div", "drilldown-sentinel");
    sentinel.setAttribute("aria-hidden", "true");
    region.appendChild(sentinel);
    td.appendChild(region);
    const parent = button.closest("tr");
    parent.after(tr);

    let page = 0;
    let hasMore = true;
    let loading = false;
    let loaded = 0;
    let total = 0;
    let shape = null;
    const noun = opts.expand.noun || "rows";
    function drawNestedHead() {
      nestedHeadRow.textContent = "";
      const num = el("th", "dd-num", "#");
      num.scope = "col";
      nestedHeadRow.appendChild(num);
      const source = el("th", "dd-source", "Source");
      source.scope = "col";
      nestedHeadRow.appendChild(source);
      if (shape.links.entity) {
        const entity = el("th", "dd-entity", "Entity");
        entity.scope = "col";
        nestedHeadRow.appendChild(entity);
      }
      shape.columns.forEach((column) => {
        const th = el("th", `dd-${column.key}${column.wide ? " dd-wide" : ""}`, column.label);
        th.scope = "col";
        nestedHeadRow.appendChild(th);
      });
    }
    // Where the loading stands - said only while it is incomplete, and once
    // more when a list that took several pages has reached its end. A list
    // that fitted in one page says nothing: the sentence over it already
    // carries the count.
    function sayNested() {
      if (!loaded || (page <= 1 && !hasMore)) {
        line.textContent = "";
      } else if (hasMore) {
        line.textContent = `${fmt(loaded)} of ${fmt(total)} ${noun} loaded`;
      } else {
        line.textContent = `All ${fmt(total)} ${noun} loaded`;
      }
      line.hidden = !line.textContent;
    }
    async function loadNested() {
      if (loading || !hasMore) return;
      loading = true;
      more.disabled = true;
      status(true, page === 0 ? "Loading…" : "Loading more…");
      try {
        const res = await opts.expand.load(row, page + 1);
        page += 1;
        const rows = (res && res.rows) || [];
        hasMore = !!(res && res.more);
        if (!shape) {
          shape = { columns: (res && res.columns) || [],
                    links: (res && res.row_links) || { source: true, entity: false } };
          drawNestedHead();
        }
        const start = (res && Number.isFinite(res.offset) ? res.offset : loaded) + 1;
        rows.forEach((r, i) => nestedBody.appendChild(drawRow(r, start + i, shape, `${number}.`)));
        loaded += rows.length;
        total = res && Number.isFinite(res.total) ? Math.max(res.total, loaded) : loaded;
        if (!loaded) {
          const empty = el("tr", "drilldown-empty");
          const cell = el("td", null, opts.expand.emptyText || "Nothing behind this row any more - the archive may have changed.");
          cell.colSpan = nestedHeadRow.children.length || 1;
          empty.appendChild(cell);
          nestedBody.appendChild(empty);
        }
        sayNested();
        updateCounter();
        refitSoon();
        // The caller's status line goes back to what it said about the
        // outer list; the nested list has a line of its own (sayNested).
        status(false, lastStatus);
        announce(hasMore ? `${fmt(rows.length)} ${noun} loaded, ${fmt(loaded)} of ${fmt(total)}`
                         : `All ${fmt(loaded)} ${noun} loaded`);
      } catch (err) {
        console.warn("drilldown: nested page failed", err);
        status(false, "Could not load rows. Try again.");
      } finally {
        loading = false;
        more.disabled = !hasMore;
        more.hidden = !hasMore;
      }
    }
    more.addEventListener("click", loadNested);
    if (observer) {
      loaders.set(sentinel, loadNested);
      observer.observe(sentinel);
    }
    loadNested();
    return tr;
  }

  /* The box the rows are seen through: the scroller's, or the window's. */
  function viewportBox() {
    return scrollBox ? scrollBox.getBoundingClientRect()
                     : { top: 0, bottom: window.innerHeight };
  }

  /* Which rows are on screen. A linear scan from the top is fine for the
   * hundreds of rows a drilldown holds, and it runs at most once per
   * animation frame. */
  function visibleRange() {
    const rows = body.children;
    if (!rows.length) return [0, 0];
    const box = viewportBox();
    const top = box.top + counter.offsetHeight;
    let first = 0;
    let last = 0;
    for (let i = 0; i < rows.length; i += 1) {
      // The rows behind an opened cell sit in the list without a number of
      // their own: they are the row above them, opened.
      if (!rows[i].dataset.row) continue;
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
    const scrolls = scrollBox
      ? scrollBox.scrollHeight - scrollBox.clientHeight > 4
      : document.documentElement.scrollHeight - window.innerHeight > 4;
    counter.textContent = counterText({ total, loaded, first, last, more: hasMore, scrolls });
    counter.hidden = !counter.textContent;
    // What the table's own sticky header has to clear. Measured rather than
    // guessed: the counter is one line on a wide window and two on a narrow
    // one, and a header that overlaps the first row hides a row.
    root.style.setProperty("--dd-count-h",
      `${counter.hidden ? 0 : counter.offsetHeight}px`);
  }
  function onScroll() {
    if (ticking) return;
    ticking = true;
    requestAnimationFrame(() => { ticking = false; updateCounter(); });
  }
  (scrollBox || window).addEventListener("scroll", onScroll, { passive: true });

  async function loadNext() {
    if (loading || !hasMore) return;
    loading = true;
    more.disabled = true;
    status(true, page === 0 ? "Loading…" : "Loading more…");
    try {
      const res = await opts.load(page + 1);
      page += 1;
      const rows = (res && res.rows) || [];
      hasMore = !!(res && res.more);
      if (!columns) {
        columns = (res && res.columns) || [];
        links = (res && res.row_links) || links;
        sourceLabel = (res && res.source_label) || "";
        drawHead();
      }
      // The running number counts up ACROSS pages: row 21 on page 2 is 21.
      // The offset is the
      // server's, so a page fetched out of order still numbers correctly.
      const start = (res && Number.isFinite(res.offset) ? res.offset : loaded) + 1;
      rows.forEach((row, i) => body.appendChild(drawRow(row, start + i)));
      loaded += rows.length;
      // A count is taken once: a later page that carries no total keeps
      // the one the first page brought.
      total = res && Number.isFinite(res.total) ? Math.max(res.total, loaded) : Math.max(total, loaded);
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
      lastStatus = hasMore ? `${rowsWord(loaded)} shown, more available` : rowsWord(loaded);
      status(false, lastStatus);
      // ANNOUNCED WHEN LOADING FINISHES, and only then. The counter itself
      // changes on every scroll frame and says nothing out loud.
      announce(hasMore ? `${rowsWord(rows.length)} more loaded, ${fmt(loaded)} of ${fmt(total)}`
                       : `All ${rowsWord(loaded)} loaded`);
    } catch (err) {
      console.warn("drilldown: page failed", err);
      status(false, "Could not load rows. Try again.");
    } finally {
      loading = false;
      more.disabled = !hasMore;
      more.hidden = !hasMore;
    }
  }

  more.addEventListener("click", loadNext);

  // ONE OBSERVER, SEVERAL SENTINELS: the table's own at the foot of the
  // list, and one under every nested listing that has been opened
  // (openNested). Which loader an entry belongs to is looked up by its
  // element, so a nested list scrolled into view fetches ITS next page and
  // not the outer table's.
  if ("IntersectionObserver" in window) {
    loaders.set(sentinel, loadNext);
    observer = new IntersectionObserver((entries) => {
      entries.forEach((e) => {
        if (!e.isIntersecting) return;
        const load = loaders.get(e.target);
        if (load) load();
      });
    }, { root: scrollBox, rootMargin: "120px" });
    observer.observe(sentinel);
  }

  loadNext();
  // A filter that changes starts at row 1 again: a position kept from the
  // question before is a lie about the answer in front of the reader.
  function reload() {
    body.textContent = "";
    headRow.textContent = "";
    loaders.forEach((_, node) => { if (node !== sentinel && observer) observer.unobserve(node); });
    loaders.forEach((_, node) => { if (node !== sentinel) loaders.delete(node); });
    expanded.clear();
    texts.length = 0;
    page = 0; hasMore = true; loaded = 0; total = 0; columns = null;
    return loadNext();
  }
  /* Let go of everything that outlives the table: the observer, the scroll
   * and resize listeners. Idempotent, so a dialog closing twice is safe. */
  let destroyed = false;
  function destroy() {
    if (destroyed) return;
    destroyed = true;
    if (observer) observer.disconnect();
    (scrollBox || window).removeEventListener("scroll", onScroll);
    window.removeEventListener("resize", refitSoon);
    loaders.clear();
  }
  return {
    table, counter, moreButton: more, reload, loadNext, destroy,
    get state() { return { loaded, total, hasMore, page }; },
  };
}
