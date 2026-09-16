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
 *  ── A CELL THAT OPENS ────────────────────────────────────────────────
 *
 *  A listing whose rows are GROUPS (the entities in one spot of the
 *  Heatmap) counts documents in its Source cell instead of naming one. That
 *  cell is a control: pressing it lists the rows behind it directly under
 *  the row, with a header of their own, newest first and a page at a time
 *  (expandCell / openNested below, `opts.expand`). In place, because a
 *  third dialog is not allowed and a link away would lose the list.
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
import { mountRows } from "./rows.js";

/* The table itself - the columns, the counter, the paging, the cell that
 * opens - lives in js/rows.js, because the Tables view draws the same
 * table on a page. Two names the rest of the product reads from here. */
export { TEXT_LINES, counterText } from "./rows.js";

/* The drilldown that is on screen, if any - the second level, so that a new
 * one replaces it instead of becoming a third. */
let openRows = null;

function el(tag, className, text) {
  const e = document.createElement(tag);
  if (className) e.className = className;
  if (text !== undefined && text !== null) e.textContent = text;
  return e;
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

  let rows = null;
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
      if (rows) rows.destroy();
      if (openRows === handle) openRows = null;
      if (opts.onClose) opts.onClose();
    },
  });
  openRows = handle;
  // Which level this is, for the stylesheet and for a test that has to be
  // able to say "the enlarged chart is still open underneath".
  handle.dialog.dataset.drilldown = String(openDialogs());

  // The body is the one scroller, in both directions, and the table's
  // sticky header and counter stick to it.
  rows = mountRows(handle.body, {
    ...opts, scroller: handle.body, status: handle.setBusy, moreButton: more,
  });
  // A filter that changes starts at row 1 again: a position kept from the
  // question before is a lie about the answer in front of the reader.
  handle.reload = rows.reload;
  return handle;
}
