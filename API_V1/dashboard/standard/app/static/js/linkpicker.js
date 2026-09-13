/* ==========================================================================
 *  "Links found" - the dialog that turns a test into rules.
 *
 *  The site answered with a page full of links. Some of them are documents,
 *  most of them are not, and the person in front of the screen is the only
 *  one who knows which is which. This dialog asks that question in a way
 *  that can be answered in half a minute for a page with two hundred links:
 *
 *    GROUPS, NOT ROWS. Links of the same shape (/rent/# - twenty flats) are
 *    one group with one tick box. Ticking twenty rows by hand is what makes
 *    people give up and take everything (patterns.js explains why the shape
 *    is computed here and what happens to it afterwards).
 *
 *    AND THE GROUPS ARE WHAT IS ON SCREEN. Every group starts folded, with
 *    its name, "n of m ticked" and three example addresses in the head.
 *    Opened, eighty rows of one shape put the second shape eight screens
 *    down and hide the very tick boxes that make the list tickable; folded,
 *    the whole answer fits in one window and the rows are one press away.
 *    A search opens what it matched, and a snapshot with one shape in it
 *    opens that one - there is nothing to choose between.
 *
 *    A TAG FOR EVERYTHING THAT IS NOT A DOCUMENT. Paging links, imprint
 *    pages, images and links to other sites carry a word saying so, and
 *    start un-ticked. They are the four kinds of link that are on every
 *    list page and interesting on none.
 *
 *    ROBOTS.TXT IS NOT NEGOTIABLE HERE. A link the site's rules forbid has
 *    no tick box at all - it says "not allowed by robots.txt" in words, is
 *    greyed, and carries a padlock. Three carriers, none of them colour.
 *    And it is out of every count: a group head says how many of its
 *    TICKABLE links are ticked and names the rest in words, so the heads
 *    add up to the footer instead of sending somebody hunting for a link
 *    that can never be ticked.
 *
 *    CLOSING ASKS FIRST. Ticking a hundred links is work. The confirmation
 *    appears inside the dialog, above the list, not as a second dialog
 *    over the first - and it asks in the explanation view too, where the
 *    ticks AND the shapes worked out from them are on the table and
 *    nothing has been applied yet. Only "Use these rules" makes closing
 *    free. Closing itself is Cancel or the X, as everywhere else in the
 *    product: Escape does nothing here either, which is what stopped this
 *    dialog throwing a hundred ticks away on the key next to Tab.
 *
 *  The footer button hands the ticks to POST /api/sources/learn - pure
 *  server-side rule learning, nothing is fetched and nothing is stored -
 *  and shows the answer in the dialog before anything is applied to the
 *  editor: "2 shapes from 118 ticked links" one statement per line, each
 *  conflict as a question WITH ITS TWO ANSWERS - a question nobody can
 *  answer is a statement in disguise - and only then a button that changes
 *  the configuration.
 *
 *  The dialog shell (`pickerDialog`) lives here and is used by
 *  filepicker.js as well; the two pickers are the same object with a
 *  different body. It is openDialog() from static/js/dialog.js with a
 *  footer they fill themselves - so every popup in the dashboard, these
 *  two included, opens, stacks, traps focus and CLOSES in one way, decided
 *  in one file.
 * ========================================================================== */

import { api, isAbort, fmtInt, plural } from "./api.js";
import { announce } from "./a11y.js";
import { openDialog, confirmDialog } from "./dialog.js";
import { shapeOf } from "./patterns.js";

/* How many rows of a group are drawn before the "show the rest" button.
 * A snapshot carries up to two thousand links; two thousand rows with a
 * checkbox each make the dialog slow to open and slow to tick, and the
 * group tick already covers every one of them. */
const ROWS_AT_ONCE = 50;

/* How many addresses of a group are printed in its head. Three is what a
 * person needs to recognise a shape; a fourth adds a line and no
 * information. */
const EXAMPLES = 3;

/* The word for each class of link, and whether it is a document at all.
 * `candidate` gets no tag: it is the ordinary case, and a tag on every row
 * would say nothing. */
const CLASS_TAGS = {
  pagination: "paging link",
  legal: "imprint or terms",
  asset: "image or script",
  external: "other site",
  list_self: "this page",
  file: "file",
};

const CLASS_GROUPS = {
  pagination: "Paging links",
  legal: "Imprint, terms and privacy",
  asset: "Images, styles and scripts",
  external: "Links to other sites",
  list_self: "This page itself",
};

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

/* ── The dialog shell ────────────────────────────────────────────────── */

/* THE PICKERS ARE ORDINARY DIALOGS. This is dialog.js's openDialog() with
 * the three conveniences the two pickers need on top - a footer they build
 * button by button, a subtitle they rewrite, and their own question before
 * closing. Not a second copy of the shell: a second copy is how one
 * product ends up with two answers to "how does a popup close" - one that
 * also closes on Escape, alone in the whole dashboard, in the two dialogs a
 * person is most likely to be mid-selection in.
 *
 * So HOW IT CLOSES is not decided here. It is decided in
 * dialog.js, once, for every popup in the product: its Cancel button and
 * the X in the top right, and nothing else - not the backdrop, not Escape.
 *
 * `guard()` is asked before every close and may refuse, which is how the
 * "you would throw the ticks away" bar works; it is handed the function
 * that closes without asking again. */
export function pickerDialog({ title, subtitle = "", guard = null, onClose = null }) {
  const shell = openDialog({
    title,
    subtitle: subtitle || " ",
    wide: true,
    // The pickers put their own Cancel beside the button that acts, so the
    // shell does not add a second one.
    cancel: false,
    onClose,
    beforeClose: () => (guard ? guard(() => shell.dismiss()) : true),
  });

  const sub = shell.dialog.querySelector(".dialog-subtitle");
  const actions = shell.footer.querySelector(".dialog-actions");

  return {
    dialog: shell.dialog,
    body: shell.body,
    footer: shell.footer,
    status: shell.status,
    actions,
    /* Closing without asking - for the caller that has just applied. */
    close: shell.dismiss,
    /* Closing the way the X does, question and all. */
    requestClose: shell.close,
    setSubtitle(text) { if (sub) sub.textContent = text; },
    action(label, kind = "button--secondary", onClick = null) {
      const button = el("button", `button ${kind}`, label);
      button.type = "button";
      if (onClick) button.addEventListener("click", onClick);
      actions.appendChild(button);
      return button;
    },
  };
}

/* THE QUESTION IS A DIALOG, not a strip inside the one being closed.
 *
 * Not a bar pushed in at the top of the picker's own body. Two things are
 * wrong with that: the question about closing appears INSIDE the thing it
 * is about - so the list it is asking to throw away is still scrolling
 * behind it and the bar reads as one more notice on a busy screen - and a
 * reader who has scrolled down never sees it at all, because the top of
 * the body is not where they are standing.
 *
 * A dialog is modal: it is the only thing that can be pressed until it is
 * answered, it takes the focus, and Escape means "keep looking" rather
 * than "throw the work away" - which is what an accidental close has to
 * mean for a question like this one (dialog.js: confirmDialog resolves
 * false for Cancel and for the X).
 */
export function confirmDiscard(handle, { text, discardLabel, onDiscard }) {
  confirmDialog({
    title: "Close without using this?",
    question: text,
    confirmLabel: discardLabel,
    cancelLabel: "Keep looking",
    danger: true,
  }).then((discard) => { if (discard) onDiscard(); });
}

/* ── The links ───────────────────────────────────────────────────────── */

function tagFor(link) {
  return CLASS_TAGS[link.class] || "";
}

function isForbidden(link) {
  return link.robots_allowed === false;
}

/* What starts ticked. Not a table of defaults per mode but one rule, which
 * happens to produce the documented defaults: START FROM WHAT THIS
 * CONFIGURATION WOULD ALREADY TAKE.
 *
 *   selected, no shapes yet  -> nothing is accepted -> nothing ticked
 *   everything except …      -> everything but paging, legal, images,
 *                               files and other sites -> those ticked
 *   exact addresses          -> nothing, until somebody names one
 *
 * The snapshot answers it per link (`accepted_by_config`), so a source that
 * already has rules opens with its own links ticked and the person sees
 * what changing something does, instead of starting from an empty page. */
function defaultTicked(links, mode) {
  const ticked = new Set();
  const accepted = links.filter((link) => link.accepted_by_config);
  if (accepted.length) {
    accepted.forEach((link) => ticked.add(link.url));
    return ticked;
  }
  if (mode === "all_except_rejected") {
    links.forEach((link) => {
      if (link.class === "candidate" && !isForbidden(link)) ticked.add(link.url);
    });
  }
  return ticked;
}

/* A shape that fits one link is not a shape - it is that link with a
 * heading over it. Three of the nine groups on the stand-in estate list
 * were of that kind, and each cost a head, a tick box and a line of
 * "Tick every link in /files/private/preisliste.pdf". They go into one
 * trailing group instead, with their rows and tags intact, so that the
 * heads left standing are the ones worth a decision. */
const LONE = 2;

function groupsOf(links) {
  const groups = new Map();
  links.forEach((link) => {
    const known = CLASS_GROUPS[link.class];
    const shape = known ? { key: `class:${link.class}`, label: known } : shapeOf(link.url);
    let group = groups.get(shape.key);
    if (!group) {
      group = { key: shape.key, label: shape.label, links: [], kind: known ? "class" : "shape" };
      groups.set(shape.key, group);
    }
    group.links.push(link);
  });

  const all = Array.from(groups.values());
  const lone = all.filter((group) => group.kind === "shape" && group.links.length < LONE);
  const rest = all.filter((group) => !lone.includes(group));
  // Shapes first and the biggest of them at the top - that is where the
  // documents are. The named classes come after, in a fixed order, so the
  // dialog looks the same on every site.
  const order = ["pagination", "legal", "asset", "external", "list_self"];
  rest.sort((a, b) => {
    if (a.kind !== b.kind) return a.kind === "shape" ? -1 : 1;
    if (a.kind === "shape") return b.links.length - a.links.length || a.label.localeCompare(b.label);
    return order.indexOf(a.key.slice(6)) - order.indexOf(b.key.slice(6));
  });
  if (!lone.length) return rest;
  const others = {
    key: "other:links", kind: "other", label: "Other links",
    links: lone.reduce((acc, group) => acc.concat(group.links), []),
  };
  return rest.concat([others]);
}

/**
 * Open the picker.
 *
 *   result   the test snapshot's json_result
 *   draft    the editor's current configuration (mode, patterns, …)
 *   onApply  called with the answer of /api/sources/learn once the person
 *            has seen it and pressed the button
 */
export function openLinkPicker({ result, draft, onApply }) {
  const links = (result.links || []).filter((link) => link && link.url);
  const mode = draft.text_mode || "selected";
  const ticked = defaultTicked(links, mode);
  const initial = new Set(ticked);
  const groups = groupsOf(links);
  const expanded = new Set();   // groups whose rows are drawn beyond ROWS_AT_ONCE
  /* Which groups are unfolded. A group is folded until somebody opens it -
   * except while a search is narrowing the list (what matched is what you
   * are looking at) and except when the whole page has one shape in it, in
   * which case there is nothing to choose between. `closed` remembers a
   * group that was folded away BY HAND, so a redraw does not helpfully
   * open it again. */
  const opened = new Set();
  const closed = new Set();
  let query = "";
  /* THESE TWO ARE NOT THE SAME THING, and reading them as one threw away
   * the most expensive half-minute in the dialog. `learned` is the ANSWER
   * that came back from /api/sources/learn - a preview, sitting in the
   * dialog, applied to nothing. `applied` is set only when the person has
   * pressed "Use these rules" and the editor has taken them. Between the
   * two, closing has to ask: twenty ticks and a learned shape are exactly
   * what the guard exists to protect. */
  let learned = null;
  let applied = false;

  function isOpen(group) {
    if (closed.has(group.key)) return false;
    if (opened.has(group.key)) return true;
    if (query) return true;
    return groups.length === 1;
  }

  function setOpen(group, node, open) {
    node.dataset.open = open ? "true" : "false";
    node.querySelector("[data-group-toggle]").setAttribute("aria-expanded", open ? "true" : "false");
    if (open) { opened.add(group.key); closed.delete(group.key); }
    else { closed.add(group.key); opened.delete(group.key); }
  }

  /* Written once: "Back to the links" restores it, and a shorter sentence
   * there dropped the "from n pages" half without any reason to. */
  const SUBTITLE = `The test followed ${plural((result.pages || []).length, "page", "pages")} of your `
    + `list and found ${plural(links.length, "link", "links")}. Select the ones that lead to `
    + "something worth collecting - the crawler works out the shape of their addresses and "
    + "follows that shape from now on."

  const handle = pickerDialog({
    title: "Which of these pages should be collected?",
    subtitle: SUBTITLE,
    guard(close) {
      if (!dirty()) return true;
      confirmDiscard(handle, {
        text: learned
          ? "The selection and the rules worked out from it have not been saved yet. Close and lose them?"
          : "This selection has not been used yet. Close and lose it?",
        discardLabel: "Discard the selection",
        onDiscard: close,
      });
      return false;
    },
  });

  function dirty() {
    // Applied: the rules are in the editor and closing loses nothing.
    if (applied) return false;
    // Learned but not applied: the answer on screen is the work.
    if (learned) return true;
    if (ticked.size !== initial.size) return true;
    for (const url of ticked) if (!initial.has(url)) return true;
    return false;
  }

  /* -- the body ------------------------------------------------------- */

  const picker = el("div", "picker");
  handle.body.appendChild(picker);

  /* Why the numbers in the heads do not add up to the number in the title.
   * The head says "31 links"; the footer counts 28, because three of them
   * are out of reach. That gap is explained once, here, under the subtitle,
   * instead of being discovered in a group whose last row has no box. */
  const blockedTotal = links.filter(isForbidden).length;
  const forbiddenNote = el("p", "picker-note", blockedTotal === 1
    ? "1 of these links is not allowed by robots.txt and cannot be collected."
    : `${fmtInt(blockedTotal)} of these links are not allowed by robots.txt and cannot be collected.`);
  forbiddenNote.hidden = !blockedTotal;
  picker.appendChild(forbiddenNote);

  const toolbar = el("div", "picker-toolbar");
  const searchField = el("div", "field");
  const searchLabel = el("label", "field-label", "Search these links");
  searchLabel.setAttribute("for", "picker-search");
  const search = document.createElement("input");
  search.type = "search";
  search.id = "picker-search";
  search.autocomplete = "off";
  search.placeholder = "part of an address or a link text";
  searchField.appendChild(searchLabel);
  searchField.appendChild(search);
  toolbar.appendChild(searchField);

  const tickAll = el("button", "button button--secondary", "Select all shown");
  tickAll.type = "button";
  const tickNone = el("button", "button button--secondary", "Clear the selection");
  tickNone.type = "button";
  /* One press for somebody who wants to read every address rather than
   * trust the three in the head - and the same press to put them away
   * again, because eighty rows is what the folding exists to avoid. */
  const foldAll = el("button", "button button--secondary", "Open every group");
  foldAll.type = "button";
  foldAll.id = "picker-expand";
  toolbar.appendChild(tickAll);
  toolbar.appendChild(tickNone);
  toolbar.appendChild(foldAll);
  picker.appendChild(toolbar);

  /* The keyboard line is about rows, so it is on screen while rows are -
   * it stood over the learn explanation, where there is not a row in
   * sight, telling people how to tick something that was not there. */
  const hintLine = el("p", "picker-hint",
    "Links of the same shape are grouped. A group starts folded and its head shows the name, how many are selected and three of the addresses - enough to decide without opening it. Clicking anywhere on a row or a group head selects it; Space does the same from the keyboard, the arrow keys move between rows, and G folds a group away.");
  picker.appendChild(hintLine);

  const scroll = el("div", "picker-scroll");
  const list = el("ul", "picker-groups");
  scroll.appendChild(list);
  picker.appendChild(scroll);

  const empty = el("p", "picker-empty", "");
  empty.hidden = true;
  picker.appendChild(empty);

  const learnBox = el("div", "learn-result");
  learnBox.hidden = true;
  picker.appendChild(learnBox);

  const groupTemplate = document.getElementById("link-group-template");
  const rowTemplate = document.getElementById("link-row-template");

  function matches(link) {
    if (!query) return true;
    const needle = query.toLowerCase();
    return (link.url || "").toLowerCase().includes(needle)
        || (link.text || "").toLowerCase().includes(needle);
  }

  function tickable(group) {
    return group.links.filter((link) => matches(link) && !isForbidden(link));
  }

  function renderRow(link, onToggle) {
    const row = rowTemplate.content.firstElementChild.cloneNode(true);
    const tick = row.querySelector("[data-tick]");
    const forbidden = isForbidden(link);
    row.dataset.url = link.url;
    row.dataset.class = link.class || "candidate";
    row.dataset.forbidden = forbidden ? "true" : "false";
    row.querySelector("[data-text]").textContent = link.text || "(no link text)";
    row.querySelector("[data-url]").textContent = link.url;

    const tags = row.querySelector("[data-tags]");
    const tag = tagFor(link);
    if (tag) tags.appendChild(el("span", "tag", tag));
    if (forbidden) {
      const lock = el("span", "tag link-lock", "🔒 not allowed by robots.txt");
      tags.appendChild(lock);
    }

    row.querySelector("[data-tick-label]").textContent =
      forbidden ? `${link.url} - not allowed by robots.txt`
                : `Collect ${link.url}`;
    tick.checked = ticked.has(link.url);
    tick.disabled = forbidden;
    if (forbidden) tick.setAttribute("aria-disabled", "true");
    tick.addEventListener("change", () => {
      if (tick.checked) ticked.add(link.url); else ticked.delete(link.url);
      if (onToggle) onToggle();
      refreshCounts();
    });
    return row;
  }

  function renderGroup(group) {
    const shown = group.links.filter(matches);
    if (!shown.length) return null;
    const node = groupTemplate.content.firstElementChild.cloneNode(true);
    const open = isOpen(group);
    node.dataset.key = group.key;
    node.dataset.open = open ? "true" : "false";
    node.querySelector("[data-group-label]").textContent = group.label;
    node.querySelector("[data-group-toggle]").setAttribute("aria-expanded", open ? "true" : "false");

    /* Three addresses in the head. A shape is a line of punctuation until
     * real addresses stand under it, and with three of them the group can
     * be ticked or left alone without ever being opened. */
    const examples = node.querySelector("[data-group-example]");
    shown.slice(0, EXAMPLES).forEach((link) => examples.appendChild(el("li", null, link.url)));
    if (shown.length > EXAMPLES) {
      const rest = el("li", null, `and ${fmtInt(shown.length - EXAMPLES)} more`);
      rest.dataset.rest = "true";
      examples.appendChild(rest);
    }

    const rows = node.querySelector("[data-rows]");
    const limit = expanded.has(group.key) ? shown.length : Math.min(shown.length, ROWS_AT_ONCE);
    shown.slice(0, limit).forEach((link) =>
      rows.appendChild(renderRow(link, () => updateGroup(node, group))));

    const more = node.querySelector("[data-more]");
    if (shown.length > limit) {
      more.hidden = false;
      more.textContent = `Show the other ${fmtInt(shown.length - limit)} in this group`;
      more.addEventListener("click", () => { expanded.add(group.key); opened.add(group.key); closed.delete(group.key); draw(); });
    }

    const groupTick = node.querySelector("[data-group-tick]");
    const choices = tickable(group);
    /* A box that looks tickable and does nothing reads as a broken page.
     * When robots.txt forbids every link of a group there is nothing to
     * tick, so the box says so to a screen reader (aria-disabled), to the
     * pointer (a class that greys it and sets `not-allowed`) and in words
     * in the head - three carriers, and the words are the one that counts. */
    node.querySelector("[data-group-tick-label]").textContent = choices.length
      ? `Select every link in ${group.label}`
      : `${group.label}: none of these may be collected - robots.txt forbids them`;
    groupTick.disabled = !choices.length;
    if (!choices.length) {
      groupTick.setAttribute("aria-disabled", "true");
      const box = node.querySelector(".group-tick");
      box.classList.add("group-tick--off");
      box.title = "robots.txt forbids every link in this group";
    }
    groupTick.addEventListener("change", () => {
      const on = groupTick.checked;
      choices.forEach((link) => {
        if (on) ticked.add(link.url); else ticked.delete(link.url);
      });
      // The rows that are drawn follow; the ones behind "show the rest" are
      // in the set already and are drawn ticked when they appear. Redrawing
      // the whole list here would take the focus off the box that was just
      // pressed, which is exactly where a keyboard user is standing.
      node.querySelectorAll(".link-row:not([data-forbidden='true']) input[type=checkbox]")
        .forEach((box) => { box.checked = on; });
      updateGroup(node, group);
      refreshCounts();
      announce(on
        ? `${choices.length} links selected in ${group.label}`
        : `${group.label} cleared`);
    });

    const toggle = node.querySelector("[data-group-toggle]");
    toggle.addEventListener("click", () => {
      setOpen(group, node, node.dataset.open === "false");
    });

    /* THE WHOLE HEAD SELECTS THE GROUP, like the whole of a row selects its
     * link. Not a label around the head, though: the fold caret is a button
     * INSIDE it, and a label would select the group every time somebody
     * opened it. So the head listens, and hands the click on to the box
     * unless it came from the caret or from the box itself.
     *
     * `.click()` rather than setting `.checked`: the box's own change
     * handler above is what ticks the links, announces it and redraws the
     * counts, and going around it would leave the group looking selected
     * with nothing selected in it. */
    const head = node.querySelector(".group-head");
    head.addEventListener("click", (event) => {
      if (event.target.closest("[data-group-toggle], label, input")) return;
      groupTick.click();
    });

    updateGroup(node, group);
    return node;
  }

  /* The group's own state: none, some, all - a tri-state box, because "some"
   * is the normal case while somebody is working and a box that shows "off"
   * for it would be a lie. Recomputed after every tick in the group, in
   * place: a redraw would move the row under the pointer. */
  function updateGroup(node, group) {
    const groupTick = node.querySelector("[data-group-tick]");
    const count = node.querySelector("[data-group-count]");
    const choices = tickable(group);
    const shown = group.links.filter(matches).length;
    const on = choices.filter((link) => ticked.has(link.url)).length;
    groupTick.checked = choices.length > 0 && on === choices.length;
    groupTick.indeterminate = on > 0 && on < choices.length;
    /* THE DENOMINATOR IS WHAT CAN BE TICKED, NOT WHAT IS ON SCREEN. Counting
     * the robots-forbidden rows in it produced "3 of 4 ticked" next to a box
     * that was fully checked, and a footer that said "28 of 28" at the same
     * moment: the reader goes looking for a fourth link that can never be
     * ticked. The heads now add up to the footer, and the ones that are out
     * of reach are named in words instead of hidden in a number. */
    const blocked = shown - choices.length;
    const locked = blocked === 1
      ? " - 1 not allowed by robots.txt"
      : ` - ${fmtInt(blocked)} not allowed by robots.txt`;
    count.textContent = choices.length
      ? `${fmtInt(on)} of ${fmtInt(choices.length)} selected` + (blocked ? locked : "")
      : "none of these may be collected - robots.txt forbids them";
  }

  function draw() {
    list.textContent = "";
    let shown = 0;
    groups.forEach((group) => {
      const node = renderGroup(group);
      if (node) {
        shown += group.links.filter(matches).length;
        list.appendChild(node);
      }
    });
    empty.hidden = shown > 0;
    if (!shown) {
      empty.textContent = query
        ? `No link here contains "${query}". Clear the search to see all ${fmtInt(links.length)} again.`
        : "The page answered without a single link. The banner on the page behind this one says what to try.";
    }
    refreshCounts();
  }

  function refreshCounts() {
    const total = links.filter((link) => !isForbidden(link)).length;
    handle.status.textContent = `${fmtInt(ticked.size)} of ${fmtInt(total)} selected`;
    useButton.disabled = ticked.size === 0 && mode !== "all_except_rejected";
    // A search opens whatever it matched, so the button has nothing left to
    // do while one is on: it says what it would do and waits.
    const searching = Boolean(query);
    const allOpen = !searching && groups.length > 0 && groups.every((group) => isOpen(group));
    foldAll.textContent = allOpen ? "Fold every group" : "Open every group";
    foldAll.disabled = searching;
  }

  search.addEventListener("input", () => {
    query = search.value.trim();
    draw();
  });

  tickAll.addEventListener("click", () => {
    groups.forEach((group) => tickable(group).forEach((link) => ticked.add(link.url)));
    draw();
    announce(`${ticked.size} links selected`);
  });
  tickNone.addEventListener("click", () => {
    ticked.clear();
    draw();
    announce("Nothing selected yet");
  });
  foldAll.addEventListener("click", () => {
    const allOpen = groups.every((group) => isOpen(group));
    groups.forEach((group) => {
      if (allOpen) { closed.add(group.key); opened.delete(group.key); }
      else { opened.add(group.key); closed.delete(group.key); }
    });
    draw();
    announce(allOpen ? "Every group is folded away" : "Every group is open");
  });

  /* Keyboard: the arrow keys walk the tick boxes, G folds the group the
   * keyboard is in. Space is the browser's own and is left alone. */
  scroll.addEventListener("keydown", (event) => {
    const boxes = Array.from(scroll.querySelectorAll(".link-row:not([data-forbidden='true']) input[type=checkbox]"));
    const here = boxes.indexOf(document.activeElement);
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      if (here < 0) return;
      event.preventDefault();
      const next = event.key === "ArrowDown"
        ? Math.min(boxes.length - 1, here + 1)
        : Math.max(0, here - 1);
      boxes[next].focus();
    } else if (event.key === "g" || event.key === "G") {
      const node = event.target.closest(".picker-group");
      if (!node) return;
      event.preventDefault();
      const group = groups.find((candidate) => candidate.key === node.dataset.key);
      if (!group) return;
      const open = node.dataset.open !== "false";
      setOpen(group, node, !open);
      if (open) node.querySelector("[data-group-toggle]").focus();
      refreshCounts();
    }
  });

  /* -- learning ------------------------------------------------------- */

  const useButton = handle.action("Work out the rules", "button", () => runLearn());
  useButton.id = "picker-learn";
  const cancelButton = handle.action("Cancel", "button--secondary", () => handle.requestClose());
  cancelButton.id = "picker-cancel";

  async function runLearn() {
    useButton.disabled = true;
    handle.status.textContent = "Working out the shapes…";
    try {
      const payload = {
        list_url: draft.text_list_url,
        mode,
        links: links.map((link) => ({ url: link.url, ticked: ticked.has(link.url) })),
        existing_patterns: (draft.patterns || []).filter((row) => row.text_origin === "manual"),
        exact_rejects: (draft.exact_urls || []).filter((row) => row.text_kind === "reject")
          .map((row) => row.text_url_canonical),
        exact_monitors: (draft.exact_urls || []).filter((row) => row.text_kind === "monitor")
          .map((row) => row.text_url_canonical),
        next_urls: (result.pages || []).map((page) => page.url).filter(Boolean),
        drop_legal_links: draft.bool_drop_legal_links !== false,
      };
      learned = await api("/api/sources/learn", { body: payload, context: false, channel: "learn" });
      showLearned(learned);
    } catch (error) {
      learned = null;
      if (!isAbort(error)) handle.status.textContent = error.message || "The shapes could not be worked out.";
      useButton.disabled = false;
    }
  }

  /* The server writes the explanation as one line with semicolons in it -
   * four facts in a row that nobody finishes reading. This cuts it back
   * into the statements it was built from, ignoring the semicolons inside
   * brackets ("(same shape; only the number differs)" is one clause of one
   * statement), and each statement starts with a capital letter like the
   * sentence it is. */
  function statementsOf(text) {
    const out = [];
    let depth = 0;
    let current = "";
    String(text || "").split("").forEach((ch) => {
      if (ch === "(") depth += 1;
      if (ch === ")") depth = Math.max(0, depth - 1);
      if (ch === ";" && depth === 0) { out.push(current); current = ""; return; }
      current += ch;
    });
    out.push(current);
    return out.map((part) => part.trim()).filter(Boolean)
      .map((part) => part.charAt(0).toUpperCase() + part.slice(1));
  }

  function showLearned(answer) {
    scroll.hidden = true;
    toolbar.hidden = true;
    hintLine.hidden = true;
    forbiddenNote.hidden = true;
    empty.hidden = true;
    learnBox.hidden = false;
    /* The list is gone and three lines have taken its place. Without this the
     * dialog kept the full window height it needs for a hundred rows, and the
     * explanation sat above 500 px of white with its two buttons stranded at
     * the bottom edge - a page that looks like it failed to load the rest. */
    handle.dialog.classList.add("dialog--fit");
    learnBox.textContent = "";
    handle.status.textContent = "";
    handle.setSubtitle("What the crawler will collect from now on");

    const lines = statementsOf(answer.explanation);
    const explanation = el("ul", "learn-explanation");
    if (!lines.length) lines.push("Nothing to learn from this selection.");
    lines.forEach((line) => explanation.appendChild(el("li", null, line)));
    learnBox.appendChild(explanation);

    const kept = (answer.accept || []).map((row) => row.text_label);
    const dropped = (answer.reject || []).map((row) => row.text_label);
    if (kept.length) learnBox.appendChild(el("p", null, `Kept: ${kept.join(", ")}`));
    if (dropped.length) learnBox.appendChild(el("p", null, `Rejected: ${dropped.join(", ")}`));
    const exactNote = el("p", null, "");
    exactNote.dataset.exactRejects = "true";
    learnBox.appendChild(exactNote);
    if ((answer.exact_monitors || []).length) {
      learnBox.appendChild(el("p", null,
        `${plural(answer.exact_monitors.length, "address", "addresses")} will be watched.`));
    }
    if (answer.paging_param) {
      learnBox.appendChild(el("p", null,
        `The list turns pages with "${answer.paging_param}"; the crawl follows that.`));
    }

    function drawExactNote() {
      const n = (answer.exact_rejects || []).length;
      exactNote.hidden = !n;
      exactNote.textContent = n === 1
        ? "1 address is left out on its own."
        : `${fmtInt(n)} addresses are left out on their own.`;
    }
    drawExactNote();

    /* A conflict is a question, and a question with no way to answer it is
     * a statement in disguise. The two answers are here, the one that is
     * in force is marked, and pressing the other one really changes what
     * "Use these rules" will apply. */
    (answer.conflicts || []).forEach((conflict) => {
      const negatives = (conflict.negatives || []).slice();
      const box = el("div", "notice notice--warn learn-conflict");
      box.appendChild(el("p", "learn-question", conflict.question || "Is that what you meant?"));
      box.appendChild(el("p", null,
        `${conflict.label}: ${conflict.reason}. That is why ${negatives.length === 1
          ? "this address cannot be told"
          : `these ${fmtInt(negatives.length)} addresses cannot be told`} apart from the ones you selected:`));
      const rest = el("ul", "learn-negatives");
      negatives.slice(0, 5).forEach((url) => rest.appendChild(el("li", null, url)));
      if (negatives.length > 5) {
        rest.appendChild(el("li", null, `and ${fmtInt(negatives.length - 5)} more`));
      }
      box.appendChild(rest);

      const answers = el("div", "learn-answers");
      const out = el("button", "button button--secondary", "Leave them out one by one");
      out.type = "button";
      const keep = el("button", "button button--secondary", "Keep them, and review the shape later");
      keep.type = "button";
      const state = el("p", "learn-answered", "");
      state.setAttribute("aria-live", "polite");

      function choose(leaveOut) {
        const set = new Set(answer.exact_rejects || []);
        negatives.forEach((url) => { if (leaveOut) set.add(url); else set.delete(url); });
        answer.exact_rejects = Array.from(set).sort();
        out.setAttribute("aria-pressed", leaveOut ? "true" : "false");
        keep.setAttribute("aria-pressed", leaveOut ? "false" : "true");
        state.textContent = leaveOut
          ? "Left out: the shape is kept, and these addresses are refused by name."
          : "Kept: the shape decides on its own, and it is marked for a second look.";
        drawExactNote();
      }
      out.addEventListener("click", () => choose(true));
      keep.addEventListener("click", () => choose(false));
      answers.appendChild(out);
      answers.appendChild(keep);
      box.appendChild(answers);
      box.appendChild(state);
      choose(true);
      learnBox.appendChild(box);
    });

    handle.actions.textContent = "";
    const apply = handle.action("Use these rules", "button", () => {
      onApply(answer);
      learned = answer;
      applied = true;      // and only here: this is the press that keeps them
      handle.close();
    });
    apply.id = "picker-apply";
    handle.action("Back to the links", "button--secondary", () => {
      learned = null;
      learnBox.hidden = true;
      scroll.hidden = false;
      toolbar.hidden = false;
      hintLine.hidden = false;
      forbiddenNote.hidden = !blockedTotal;
      handle.dialog.classList.remove("dialog--fit");
      handle.actions.textContent = "";
      handle.actions.appendChild(useButton);
      handle.actions.appendChild(cancelButton);
      useButton.disabled = false;
      handle.setSubtitle(SUBTITLE);
      draw();
    });
  }

  draw();
  search.focus();
  return handle;
}
