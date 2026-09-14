/* ==========================================================================
 *  The glyphs a module needs, cloned out of the page rather than copied.
 *
 *  ONE PLACE OWNS EVERY ICON IN THE PRODUCT, and it is templates/_macros.html.
 *  The top bar renders `icon(id)` for all eleven views, so a module that wants
 *  a view's glyph takes the one already on the page: the popup, the drilldown
 *  row and the tab can then never show two different pictures of one view, and
 *  a view whose icon changes changes everywhere at once.
 *
 *  `__newtab` is the one glyph that belongs to no view. The bar renders it
 *  hidden for exactly this reason (_layout.html says so where it does).
 *
 *  A missing glyph is not an error: the caller keeps its word and loses its
 *  picture, which is the right way round for a decoration that carries no
 *  information the label does not.
 * ========================================================================== */

/* SCOPED TO THE BAR, and that is not tidiness - `[data-view="…"]` alone is
 * ambiguous. `<body data-view="diagrams">` carries the CURRENT view under the
 * same attribute (_layout.html), and the body comes first in document order,
 * so on the Diagrams page `[data-view="diagrams"] svg` matched the body and
 * returned whatever svg happened to be first inside it. The drilldown's
 * Diagrams link then wore the new-tab arrow, which is a picture of the wrong
 * promise. The glyphs live in the top bar; that is where they are read from. */
export function viewIcon(view) {
  const source = document.querySelector(`.topbar [data-view="${view}"] svg`);
  return source ? source.cloneNode(true) : null;
}

export function newTabIcon() {
  return viewIcon("__newtab");
}

/* ── Xtracting itself ────────────────────────────────────────────────────
 *
 * This archive is one half of a pair: the extraction runs on Xtracting and
 * the results land here. Several answers on these pages are only actionable
 * over there - a project's settings, the keys that may extract - so the
 * pages link out to it rather than describing where to click.
 *
 * The address is a SETTING (DASHBOARD_XTRACTING_URL), stamped into a <meta>
 * by the layout: a customer on their own installation is not on
 * xtracting.io, and a hard-coded host would send them somewhere they have no
 * account. The default is the public one, so a dashboard that has never been
 * configured still links somewhere true.
 */
const DEFAULT_XTRACTING = "https://xtracting.io";

export function xtractingUrl(path = "") {
  const meta = document.querySelector('meta[name="xtracting-url"]');
  const base = (meta && meta.content ? meta.content.trim() : "") || DEFAULT_XTRACTING;
  if (!path) return base.replace(/\/+$/, "");
  return `${base.replace(/\/+$/, "")}/${String(path).replace(/^\/+/, "")}`;
}

/* Where a customer manages the keys that may extract. Named rather than
 * spelled out at each call site: it is one page, and every sentence in this
 * dashboard that says "you need a key" should reach the same one. */
export function apiKeysUrl() {
  return xtractingUrl("dashboard/api-keys");
}

export function projectUrl(projectId) {
  return projectId
    ? xtractingUrl(`dashboard/projects/${encodeURIComponent(projectId)}`)
    : xtractingUrl("dashboard/projects");
}

/* A LINK THAT LEAVES THIS DASHBOARD SAYS SO, AND KEEPS THE PROMISE.
 *
 * Every link to Xtracting opens in a new tab: a reader here is in the middle
 * of something - a search, a set of ticked types, a place they had panned to
 * - and a link that took the page away would end that to answer a question
 * about one row. The arrow says it to anybody who can see it and the
 * accessible name says it in words to anybody who cannot (WCAG 3.2.5 is
 * about not surprising people).
 *
 * `rel="noopener"` because target=_blank without it hands the new page a
 * handle on this one.
 */
export function outward(link, label) {
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  const arrow = newTabIcon();
  if (arrow) link.appendChild(arrow);
  if (label) link.setAttribute("aria-label", `${label} (opens in a new tab)`);
  return link;
}
