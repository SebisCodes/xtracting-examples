/* ==========================================================================
 *  Accessibility helpers every view shares.
 *
 *    announce(text)        say something to a screen reader without moving
 *                          focus - "12 rows loaded", "Saved". One hidden
 *                          aria-live region for the whole page, created on
 *                          first use, so the views do not each add their own
 *                          and talk over one another.
 *
 *    toast(text, opts)     the visible counterpart: a short message in the
 *                          corner. Errors stay until closed - an older user
 *                          who looked away must still find out what went
 *                          wrong (NN/g: no time limits on things that
 *                          matter). Everything else fades after a few
 *                          seconds, and never while hovered or focused.
 *
 *    trapFocus(dialog)     keep Tab inside an open <dialog>. Browsers do this
 *                          for showModal() in theory; some still let Tab
 *                          leak into the page behind, so the dialog code
 *                          adds this belt to the braces. Returns a function
 *                          that removes the listener.
 *
 *    hostToasts(node)      move the toast corner into `node`. A modal
 *                          <dialog> is in the browser's TOP LAYER, above
 *                          everything the page can paint - a toast is
 *                          `position: fixed` in the page and is therefore
 *                          drawn BEHIND the dialog's backdrop. So while a
 *                          dialog is open the corner lives inside it, and
 *                          js/dialog.js hands it back when it closes. An
 *                          error nobody can see is an error that did not
 *                          happen.
 *
 *  No framework, no state: each helper works on the document as it is.
 * ========================================================================== */

const LIVE_ID = "xd-live";
const TOASTS_ID = "xd-toasts";
const DEFAULT_TOAST_MS = 6000;

function liveRegion(assertive) {
  const id = assertive ? LIVE_ID + "-assertive" : LIVE_ID;
  let node = document.getElementById(id);
  if (!node) {
    node = document.createElement("div");
    node.id = id;
    node.className = "visually-hidden";
    node.setAttribute("aria-live", assertive ? "assertive" : "polite");
    node.setAttribute("aria-atomic", "true");
    document.body.appendChild(node);
  }
  return node;
}

/* Screen readers only speak a live region when its content CHANGES. Saying
 * "Saved" twice in a row is a change of nothing, so the text is cleared
 * first and set again a tick later. */
export function announce(text, opts = {}) {
  const node = liveRegion(Boolean(opts.assertive));
  node.textContent = "";
  window.setTimeout(() => { node.textContent = String(text || ""); }, 30);
}

/* Where the toast corner belongs right now: the topmost open modal
 * <dialog>, or the page. Appending an element that is already somewhere
 * MOVES it, with its children, so a toast raised before a dialog opened
 * travels into it and stays readable. */
function toastHost() {
  const dialogs = Array.from(document.querySelectorAll("dialog[open]"))
    .filter((d) => typeof d.matches === "function" && d.matches(":modal"));
  return dialogs.length ? dialogs[dialogs.length - 1] : document.body;
}

export function hostToasts(node) {
  const box = document.getElementById(TOASTS_ID);
  const target = node || document.body;
  if (box && box.parentNode !== target && document.contains(target)) target.appendChild(box);
  return box;
}

function toastContainer() {
  let box = document.getElementById(TOASTS_ID);
  if (!box) {
    box = document.createElement("div");
    box.id = TOASTS_ID;
    box.className = "toasts";
    // A region with a name, so it is findable by landmark; the individual
    // toasts carry role=status/alert and do the speaking.
    box.setAttribute("role", "region");
    box.setAttribute("aria-label", "Notifications");
    document.body.appendChild(box);
  }
  // `:modal` is not in every browser this has to run in; where it is not,
  // the corner simply stays in the page, which is where it has always been.
  try { hostToasts(toastHost()); } catch (e) { /* no :modal support */ }
  return box;
}

/* kind: "error" | "warn" | "success" | "info" (default). Returns the element,
 * with a close() method, so a caller can take an error down once it is fixed.
 *
 * `opts.id` shows a message at most once: a second call with the same id
 * REPLACES the first rather than stacking a duplicate. Without it, a view that
 * reloads its list every few seconds papers the corner with the same sentence.
 *
 * `opts.action` = {label, href} puts the way to the thing the message is about
 * inside the message. A sentence that names a page and cannot take you to it
 * spends the reader's attention twice. */
export function toast(text, opts = {}) {
  const kind = opts.kind || "info";
  const box = toastContainer();

  /* THEY STACK. A message is not a slot.
   *
   * If `id` meant "replace whatever carries this id" and every caller
   * passed a CONSTANT one, testing four watched pages would raise four
   * messages and the reader would see the last of them. Four pages that
   * did not run are four facts, and three of them would be thrown away by
   * a mechanism nobody can see.
   *
   * Replacing is still right for a STANDING summary - "3 watched pages have
   * never been crawled", raised again every time the list is redrawn - and
   * that caller now asks for it by name. The id on its own is only an
   * address, for a caller that wants to take its own message away again. */
  if (opts.id && opts.replace) {
    const already = box.querySelector(`[data-toast-id="${CSS.escape(opts.id)}"]`);
    if (already) already.remove();
  }

  const item = document.createElement("div");
  if (opts.id) item.dataset.toastId = opts.id;
  item.className = `toast toast--${kind}`;
  // An error interrupts (alert); anything else waits its turn (status).
  item.setAttribute("role", kind === "error" ? "alert" : "status");

  const body = document.createElement("p");
  body.className = "toast-text";
  body.textContent = String(text || "");
  item.appendChild(body);

  if (opts.hint) {
    const hint = document.createElement("p");
    hint.className = "toast-hint";
    hint.textContent = String(opts.hint);
    item.appendChild(hint);
  }

  if (opts.action && opts.action.href) {
    const link = document.createElement("a");
    link.className = "toast-action";
    link.href = opts.action.href;
    link.textContent = String(opts.action.label || "Open");
    item.appendChild(link);
  }

  /* AN X IN THE CORNER, AND A BIG ONE.
   *
   * Not a button reading "Close", which is a word competing for attention
   * with the message beside it - and on a stack of five it is the word
   * repeated five times. A cross in the top right is where every reader has
   * already learnt to look, and at 44 px it is the target size the rest of
   * this product is held to (WCAG 2.5.8 asks 24).
   *
   * The glyph is drawn, not typed: the multiplication sign and the letter x
   * are two different heights in most faces, and a screen reader reads one
   * of them aloud. This one is a line drawing with a real name on the
   * button. */
  const close = document.createElement("button");
  close.type = "button";
  close.className = "toast-close";
  close.setAttribute("aria-label", "Close this message");
  close.title = "Close";
  close.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">'
    + '<path d="M6 6l12 12M18 6L6 18" fill="none" stroke="currentColor" '
    + 'stroke-width="2.25" stroke-linecap="round"/></svg>';
  item.appendChild(close);

  let timer = null;
  const remove = () => {
    if (timer) window.clearTimeout(timer);
    timer = null;
    if (item.parentNode) item.parentNode.removeChild(item);
  };
  close.addEventListener("click", remove);
  item.close = remove;

  // Errors stay. Others go on their own, but not while someone is reading
  // them: hover or focus pauses the clock and leaving restarts it.
  const ms = opts.timeout ?? (kind === "error" ? 0 : DEFAULT_TOAST_MS);
  if (ms > 0) {
    const arm = () => { if (!timer) timer = window.setTimeout(remove, ms); };
    const disarm = () => { if (timer) { window.clearTimeout(timer); timer = null; } };
    item.addEventListener("mouseenter", disarm);
    item.addEventListener("focusin", disarm);
    item.addEventListener("mouseleave", arm);
    item.addEventListener("focusout", arm);
    arm();
  }

  /* NEWEST AT THE BOTTOM, AND AS MANY AS THERE ARE FACTS.
   *
   * No cap of five here: that is the same fault as one message per kind
   * wearing a different number - test eight watched pages and three of the
   * answers are thrown away before anybody reads them. The corner is a
   * scrolling column (`.toasts` in app.css), so eight messages are eight
   * messages and the reader scrolls.
   *
   * The number below is a runaway guard and nothing else. `api.js` raises a
   * message for every failed request that did not ask to be quiet, and an
   * endpoint failing on every keystroke would otherwise grow the DOM without
   * end. Fifty is far past anything a person generates by pressing buttons.
   */
  box.appendChild(item);
  while (box.children.length > 50) box.removeChild(box.firstChild);
  return item;
}

const FOCUSABLE = [
  "a[href]", "area[href]", "button:not([disabled])", "input:not([disabled]):not([type=hidden])",
  "select:not([disabled])", "textarea:not([disabled])", "[tabindex]:not([tabindex='-1'])",
  "summary", "iframe", "audio[controls]", "video[controls]", "[contenteditable]",
].join(",");

function focusable(root) {
  return Array.from(root.querySelectorAll(FOCUSABLE)).filter((el) => {
    if (el.closest("[hidden]") || el.getAttribute("aria-hidden") === "true") return false;
    // offsetParent is null for display:none and for fixed elements; the
    // client rects settle it for both.
    return el.getClientRects().length > 0;
  });
}

export function trapFocus(dialog) {
  function onKey(e) {
    if (e.key !== "Tab") return;
    const items = focusable(dialog);
    if (!items.length) { e.preventDefault(); return; }
    const first = items[0];
    const last = items[items.length - 1];
    const active = document.activeElement;
    if (e.shiftKey && (active === first || !dialog.contains(active))) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && (active === last || !dialog.contains(active))) {
      e.preventDefault();
      first.focus();
    }
  }
  dialog.addEventListener("keydown", onKey);
  // Focus starts inside, on the first control, unless the dialog already
  // put it somewhere deliberate (autofocus).
  if (!dialog.contains(document.activeElement)) {
    const items = focusable(dialog);
    if (items.length) items[0].focus();
  }
  return () => dialog.removeEventListener("keydown", onKey);
}
