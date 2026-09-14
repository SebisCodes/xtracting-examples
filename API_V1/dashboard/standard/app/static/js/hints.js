/* The explanation of one control, beside its label, out of the way until asked for.
 *
 * WHY THIS FILE EXISTS AT ALL. Every form in this product carried its help as a
 * permanently visible grey sentence under each control. On a form that asks about
 * twenty things that is twenty sentences to read past, every time, for ever - and
 * the sentence is wanted at the moment of doubt, not at every other moment.
 *
 * WHY THE ICON IS A BUTTON. This product says in four places - drilldown.js twice,
 * settings.css and map.js - that content may never live only in a hover, because a
 * tooltip needs a mouse. Half the people who need the sentence are on a tablet, and
 * a keyboard user has no hover at all. So there are three ways in and they behave
 * differently on purpose:
 *
 *   pointer in     the bubble appears while the pointer is there
 *   click / Enter  the bubble is PINNED - it stays when the pointer leaves
 *   Tab to it      same as a click, because a keyboard has no "leaves" either
 *
 * and three ways out: click it again, Escape, or click anything else.
 *
 * ONE LISTENER FOR THE WHOLE PAGE. Delegated from the document, so a bubble in a
 * dialog that did not exist when this ran works exactly like one in the form -
 * which matters, because the link picker and the project chooser are built at the
 * moment they are opened.
 *
 * WITHOUT JAVASCRIPT the bubble carries `hidden` from the template and stays that
 * way. That is not perfect and it is the right failure: nothing else on these pages
 * works without JavaScript either, and a page that cannot open its own help is
 * better than one that shows every sentence at once.
 */

/** The bubble a hint button controls, or null. */
function bubbleOf(button) {
  const id = button.dataset.hint;
  return id ? document.getElementById(id) : null;
}

function show(button, { pinned }) {
  const bubble = bubbleOf(button);
  if (!bubble) return;
  bubble.hidden = false;
  button.setAttribute("aria-expanded", "true");
  // Pinned is a property of the BUTTON, not of the bubble: it says how the
  // bubble was opened, and that decides whether the pointer leaving closes it.
  if (pinned) button.dataset.pinned = "1";
}

function hide(button, { force = false } = {}) {
  if (!force && button.dataset.pinned === "1") return;
  const bubble = bubbleOf(button);
  if (bubble) bubble.hidden = true;
  button.setAttribute("aria-expanded", "false");
  delete button.dataset.pinned;
}

/** Close every open bubble except the one asked for. */
function closeOthers(keep) {
  document.querySelectorAll(".hint-button[aria-expanded='true']").forEach((other) => {
    if (other !== keep) hide(other, { force: true });
  });
}

export function startHints(root = document) {
  if (root.dataset && root.dataset.hintsWired === "1") return;
  if (root.dataset) root.dataset.hintsWired = "1";

  /* Pointer. `pointerover`/`pointerout` rather than mouseenter/mouseleave so one
   * delegated pair covers buttons that do not exist yet, and so a pen and a touch
   * that hovers behave like a mouse. A touch that TAPS fires a click as well,
   * which pins it - the behaviour a finger needs. */
  root.addEventListener("pointerover", (event) => {
    const button = event.target.closest(".hint-button");
    if (button) show(button, { pinned: false });
  });
  root.addEventListener("pointerout", (event) => {
    const button = event.target.closest(".hint-button");
    // Moving INSIDE the button (over the svg and back) is not leaving it.
    if (button && !button.contains(event.relatedTarget)) hide(button);
  });

  /* Click pins and unpins. */
  root.addEventListener("click", (event) => {
    const button = event.target.closest(".hint-button");
    if (!button) {
      // Anything else on the page closes what is open - including a click
      // inside a bubble, which would otherwise be a box nobody can shut.
      closeOthers(null);
      return;
    }
    event.preventDefault();
    closeOthers(button);
    // A POINTER FOCUSES BEFORE IT CLICKS, and that would shut the bubble in
    // the same gesture that opened it: mousedown moves the focus, the handler
    // below pins the bubble, and then THIS handler reads "already pinned" and
    // hides it again. With a mouse it merely looks like a click that undoes the
    // hover; with a FINGER, which has no hover at all, the detail could not be
    // opened by any means - the one outcome this file exists to prevent. So a
    // pin that is a millisecond old is the opening half of one gesture, not
    // the closing half of two.
    const opening = button.dataset.justFocused === "1";
    delete button.dataset.justFocused;
    if (!opening && button.dataset.pinned === "1") hide(button, { force: true });
    else show(button, { pinned: true });
  });

  /* The keyboard. Focus opens - a keyboard user cannot hover - and Escape
   * closes without moving the focus, so Tab carries on from where it was. */
  root.addEventListener("focusin", (event) => {
    const button = event.target.closest(".hint-button");
    if (button) {
      closeOthers(button);
      show(button, { pinned: true });
      // Which click this pin belongs to - see the click handler above.
      button.dataset.justFocused = "1";
    }
  });
  root.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    const open = document.querySelector(".hint-button[aria-expanded='true']");
    if (!open) return;
    event.stopPropagation();
    hide(open, { force: true });
  });
}

export default startHints;
