/* ==========================================================================
 *  The shape every dialog in this dashboard has.
 *
 *  One module, so a popup cannot be half a popup: whatever opens it, it has
 *  a title, an X in the top right, a body, a footer, focus that is trapped
 *  while it is open and given back when it closes - and it closes in
 *  exactly two ways.
 *
 *    openDialog(opts) → handle
 *      opts.title      the heading (required in practice: a dialog with no
 *                      name is unnavigable for a screen reader)
 *      opts.subtitle   a line under it - a count, a period, a caveat
 *      opts.wide       true for a dialog that holds a picture or a table
 *      opts.content    an element, a string, or function(body)
 *      opts.actions    elements for the footer, left to right; the Cancel
 *                      is added by opts.cancel, not by hand
 *      opts.focusCancel  open with the focus on Cancel rather than on the X.
 *                      For a question whose confirming button throws work
 *                      away: confirmDialog sets it from `danger`.
 *      opts.cancel     true (default) → a Cancel button in the footer.
 *                      A string renames it ("Keep editing"). false only
 *                      where the dialog has no action to cancel, and the
 *                      X is then the whole of its closing.
 *      opts.unsaved    function() → boolean. While it says true, Cancel and
 *                      the X ask before discarding instead of closing.
 *      opts.beforeClose function() → boolean, for the dialog that asks its
 *                      OWN question. Returning false keeps it open, having
 *                      asked in its own way; the link and file pickers put
 *                      that question inside the body, above the list they
 *                      are about, and close themselves when it is answered
 *                      (static/js/linkpicker.js). It is consulted before
 *                      opts.unsaved, and a dialog uses one or the other.
 *      opts.onClose    called once, after it has closed
 *
 *      → { dialog, body, status, footer, close, setBusy, depth }
 *
 *    confirmDialog(opts) → Promise<boolean>
 *      The same shell as a question. It STACKS: asked from inside a dialog
 *      it opens over it, and the one underneath is still there afterwards.
 *
 *    openDialogs()   how many are open, top last
 *    closeAll()      shut everything (a language change, a navigation)
 *
 *  ── HOW A DIALOG CLOSES, AND WHY IT IS ONLY THIS ──────────────────────
 *
 *  Its Cancel button, or the X in the top right. Nothing else.
 *
 *  A CLICK ON THE BACKDROP DOES NOTHING. Light dismiss is switched off in
 *  this product: the dialogs here are opened to read something out of them
 *  (the rows behind a bar, the links a test found, a map at a size that can
 *  be read), a person drags across them to select text, and losing the
 *  whole thing to a stray pointer-up outside the box costs them the query
 *  they were exploring. There is therefore no listener for it: not a
 *  disabled one, none.
 *
 *  ESCAPE DOES NOTHING EITHER, and that is the deliberate part: the WAI-
 *  ARIA dialog pattern expects Escape to close and screen-reader users are
 *  taught it, so this WILL surprise them - the accessible alternative, if
 *  it is ever wanted, is one line (in `onCancelKey` below): let Escape
 *  close the TOPMOST dialog only and never the one underneath it, which
 *  keeps "I lost my place" solved while honouring the convention. Until
 *  then the X is focusable, is the first control in the dialog and is
 *  reachable with one Tab from anywhere in it, so nobody is trapped.
 *
 *  ── STACKING ─────────────────────────────────────────────────────────
 *
 *  A second dialog opens OVER the first and the first stays open
 *  underneath - a click on a bar inside an enlarged chart shows the rows
 *  without throwing away the chart. Native <dialog>s stack in the browser's
 *  top layer in open order, so this needs no z-index at all; what it does
 *  need is that each one is a sibling in <body> (not nested), or the focus
 *  trap of the outer one would fight the inner one's.
 * ========================================================================== */

import { trapFocus, hostToasts } from "./a11y.js";

/* Open dialogs, oldest first. The last one is the one on top. */
const stack = [];
let seq = 0;

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

export function openDialogs() {
  return stack.length;
}

export function openDialog(opts = {}) {
  const opener = document.activeElement;
  seq += 1;
  const id = `dialog-${seq}`;

  const dialog = el("dialog", "dialog" + (opts.wide ? " dialog--wide" : ""));
  dialog.id = id;
  dialog.setAttribute("aria-labelledby", `${id}-title`);
  if (opts.subtitle) dialog.setAttribute("aria-describedby", `${id}-subtitle`);

  // ── Header: the title, then the X. Always this order, always this
  // corner: a close button that moves between dialogs has to be looked
  // for every time.
  const header = el("header", "dialog-header");
  const title = el("h2", "dialog-title", opts.title || "");
  title.id = `${id}-title`;
  header.appendChild(title);
  if (opts.subtitle) {
    const sub = el("p", "dialog-subtitle", opts.subtitle);
    sub.id = `${id}-subtitle`;
    header.appendChild(sub);
  }
  const closeBtn = el("button", "dialog-close");
  closeBtn.type = "button";
  // The name is "Close"; the glyph is decoration, or a screen reader says
  // "multiplication sign".
  // EVERY ICON-ONLY BUTTON SAYS WHAT IT DOES ON HOVER as well as to a screen
  // reader: the glyph is a picture of the function, not a name for it.
  closeBtn.title = "Close";
  closeBtn.setAttribute("aria-label", "Close");
  const glyph = el("span", "dialog-x", "✕");
  glyph.setAttribute("aria-hidden", "true");
  closeBtn.appendChild(glyph);
  header.appendChild(closeBtn);
  dialog.appendChild(header);

  const body = el("div", "dialog-body");
  dialog.appendChild(body);

  const footer = el("footer", "dialog-footer");
  const status = el("p", "dialog-status");
  status.setAttribute("aria-live", "polite");
  footer.appendChild(status);
  const actions = el("div", "dialog-actions");
  (opts.actions || []).forEach((a) => actions.appendChild(a));

  // Cancel and the X do the same thing, and both ask when there is unsaved
  // input. Cancel is last, on the right, where the eye leaves the footer.
  let cancelBtn = null;
  if (opts.cancel !== false) {
    cancelBtn = el("button", "button button--secondary",
      typeof opts.cancel === "string" ? opts.cancel : "Cancel");
    cancelBtn.type = "button";
    actions.appendChild(cancelBtn);
  }
  footer.appendChild(actions);
  dialog.appendChild(footer);

  if (typeof opts.content === "function") opts.content(body);
  else if (opts.content instanceof Node) body.appendChild(opts.content);
  else if (opts.content !== undefined && opts.content !== null) body.textContent = String(opts.content);

  // A sibling of every other dialog, never a child of one.
  document.body.appendChild(dialog);

  let closed = false;
  let untrap = null;

  function finish() {
    if (closed) return;
    closed = true;
    if (untrap) untrap();
    const at = stack.indexOf(handle);
    if (at >= 0) stack.splice(at, 1);
    // A toast raised while this dialog was open is moved INTO it, because
    // a modal dialog is in the top layer and a `position: fixed` box in the
    // page is drawn behind its backdrop - an error nobody can see is an
    // error that did not happen. It moves to whatever is on top now (the
    // dialog underneath, or the page), before this one is removed and
    // takes it with it.
    const below = stack[stack.length - 1];
    hostToasts(below ? below.dialog : document.body);
    try { dialog.close(); } catch (e) { /* never opened, or already closed */ }
    dialog.remove();
    stack.forEach((h, i) => h.dialog.setAttribute("data-depth", String(i + 1)));
    if (opts.onClose) opts.onClose();
    // Back where they came from. A keyboard user who lands at the top of
    // the page after closing has lost their place (NN/g on modal dialogs).
    if (opener && typeof opener.focus === "function" && document.contains(opener)) {
      opener.focus();
    } else {
      const under = stack[stack.length - 1];
      if (under) under.dialog.querySelector(".dialog-close")?.focus();
    }
  }

  /* The one way out, from either control. With unsaved input it asks
   * first - and the question is itself a dialog, stacked over this one. */
  function requestClose() {
    if (closed) return;
    // The dialog's own question, where it has one. It answers by closing
    // through `dismiss` when the person says to; until then this returns
    // false and the dialog stays exactly as it was.
    if (typeof opts.beforeClose === "function" && !opts.beforeClose()) return;
    if (typeof opts.unsaved === "function" && opts.unsaved()) {
      confirmDialog({
        title: "Discard your changes?",
        question: "This dialog has changes that have not been saved. Closing it throws them away.",
        confirmLabel: "Discard them",
        cancelLabel: "Keep editing",
        danger: true,
      }).then((yes) => { if (yes) finish(); });
      return;
    }
    finish();
  }

  closeBtn.addEventListener("click", requestClose);
  if (cancelBtn) cancelBtn.addEventListener("click", requestClose);

  /* ESCAPE, AND THE ONE LINE THAT WOULD CHANGE IT.
   *
   * The browser fires `cancel` for Escape (and for the close-request
   * gesture of a phone). Preventing it is what makes Escape do nothing -
   * deliberate, per the product's rule that a dialog closes only by Cancel
   * or the X. To honour the ARIA pattern instead, replace the body of this
   * function with: `e.preventDefault(); if (stack[stack.length - 1] ===
   * handle) requestClose();` - Escape then closes the TOP dialog only and
   * never the one underneath. */
  function onCancelKey(e) { e.preventDefault(); }
  dialog.addEventListener("cancel", onCancelKey);

  // NO backdrop listener. See the note at the top of this file: light
  // dismiss is off, and the way that is written is that nothing listens.

  const handle = {
    dialog,
    body,
    status,
    footer,
    close: requestClose,
    /* Closing without asking - for the caller that has just saved. */
    dismiss: finish,
    setBusy(on, text) {
      dialog.setAttribute("aria-busy", on ? "true" : "false");
      status.textContent = text || "";
    },
    get depth() { return stack.indexOf(handle) + 1; },
  };

  stack.push(handle);
  stack.forEach((h, i) => h.dialog.setAttribute("data-depth", String(i + 1)));

  if (typeof dialog.showModal === "function") dialog.showModal();
  else dialog.setAttribute("open", "");   // jsdom and very old browsers

  untrap = trapFocus(dialog);
  // Focus the X first: it is the control every dialog has, it is the way
  // out, and a screen reader reads the title straight after it.
  //
  // A DANGEROUS QUESTION IS THE EXCEPTION. When the confirming button throws
  // work away, focus lands on the named safe answer instead - "Keep editing",
  // not a glyph. The X is safe too, but a reader who answers with Enter should
  // be answering a sentence they have read, and older readers make more
  // mistakes and recover from them less easily (NN/g). The expensive answer
  // here is usually not recoverable at all.
  if (opts.focusCancel && cancelBtn) cancelBtn.focus();
  else closeBtn.focus();
  return handle;
}

/* A question, in the same shell. Resolves true when the confirming button
 * was pressed and false for Cancel or the X - so "do nothing" is what an
 * accidental close means, never "yes". */
export function confirmDialog(opts = {}) {
  return new Promise((resolve) => {
    let answer = false;
    const confirm = el("button", "button" + (opts.danger ? " button--danger" : ""),
      opts.confirmLabel || "Yes");
    confirm.type = "button";
    const handle = openDialog({
      title: opts.title || "Are you sure?",
      content: opts.question || "",
      actions: [confirm],
      cancel: opts.cancelLabel || true,
      // A destructive question opens with the safe answer under the cursor.
      focusCancel: Boolean(opts.danger),
      onClose: () => resolve(answer),
    });
    confirm.addEventListener("click", () => {
      answer = true;
      handle.dismiss();
    });
  });
}

/* Everything shut, top first, without asking: for a page that is leaving. */
export function closeAll() {
  while (stack.length) stack[stack.length - 1].dismiss();
}
