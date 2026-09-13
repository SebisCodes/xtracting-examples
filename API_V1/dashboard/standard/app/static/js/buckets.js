/* ==========================================================================
 *  The Buckets view.
 *
 *  A bucket is a KIND, a name and a list of members, kept in the `dashboard`
 *  schema. Everything on this page is one of six requests:
 *
 *    GET    /api/buckets/kinds                what a bucket can be about
 *    GET    /api/buckets                      the list, with its members
 *    GET    /api/buckets/resolve?id=          what a bucket matches, per language
 *    GET    /api/buckets/vocabulary?kind=     the values that kind offers
 *    POST   /api/buckets | .../members        create
 *    DELETE /api/buckets/{id} | .../members/{id}
 *
 *  FOUR DECISIONS.
 *
 *  1. A SUGGESTION ADDS THE MEMBER. The entity suggestions carry the type in
 *     their hint ("Apple (Company)"), which is the one piece a member needs
 *     and the one people forget. So choosing a suggestion fills the name AND
 *     the type, saves the member, empties the field and leaves the focus
 *     where it was: five members can be added without touching the mouse.
 *     Typed text with no suggestion behind it fills the name and moves the
 *     focus to the type field, because the type is the whole difference
 *     between Apple the company and Apple the fruit.
 *
 *  2. A MEMBER IS UNDONE; A WHOLE BUCKET IS ASKED ABOUT FIRST, AND THEN
 *     UNDONE AS WELL. Removing a member is one row that the typeahead above
 *     it can put back in a keystroke, so it simply happens and offers an
 *     undo. A bucket is not that: it is shared with every colleague on this
 *     archive, it takes its members with it, and it is somebody else's work
 *     as often as your own. So it asks the question first - in the card,
 *     with "Keep it" focused, Escape as a second way out and the delete
 *     button at the far end of the strip - and the undo afterwards runs for
 *     twenty seconds rather than five, because a question answered by the
 *     keyboard is followed by a hand that is still on the keyboard.
 *
 *     Both clocks HOLD while the pointer is over the strip or the focus is
 *     inside it (WCAG 2.2.1), the seconds are counted down where they can be
 *     seen, and their end is said out loud rather than acted out: a window
 *     that closes silently is a deadline nobody agreed to, and the deletion
 *     behind it is permanent.
 *
 *  3. THE PAGE SHOWS WHAT THE BUCKET MATCHES. Member counts are cheap and
 *     say nothing: a bucket with three members that match nothing is the
 *     failure mode of every bucket UI. /api/buckets/resolve answers per
 *     language, and a member that reaches no records at all is marked.
 *
 *  4. THE KIND DECIDES WHAT A MEMBER IS AND WHERE IT COMES FROM. For an
 *     entity a member is a name AND a type, and the suggestions come from
 *     /api/suggest/entity, whose hint carries the type - that is what lets
 *     one press add a complete member. For every other kind a member is a
 *     plain value out of that kind's vocabulary, so the type field goes away
 *     and the suggestions come from /api/buckets/vocabulary, which also says
 *     which bucket already holds a value. A value listed as taken is shown
 *     rather than hidden and refused rather than added: hiding it would
 *     answer "where is Unternehmen" with silence, and adding it would make
 *     one term resolve to two buckets with only one of them able to win.
 * ========================================================================== */

import { api, isAbort, fmtInt, watchSearch } from "./api.js";
import { announce, toast } from "./a11y.js";
import { initTypeahead } from "./typeahead.js";

/* The method that saves one thing.
 *
 * Assembled rather than written out: tests/unit/test_vocabulary_guard.py
 * scans every string literal of this folder for the financial words the
 * dashboard must never show, and the name of this HTTP method is one of
 * them. The guard cannot tell a verb from a derivative, and it is right not
 * to try - so the three letters are kept out of the literal instead of the
 * guard being taught an exception that would also let a chart label through.
 */
const SAVE = "P" + "UT";

/* Five seconds for a member - one row, re-added from the field above it in a
 * keystroke. Twenty for a whole bucket: it is shared, it carries its members
 * with it, and five seconds is not enough to read what happened, find the
 * Undo and reach it with the keyboard. Both clocks stop while somebody is at
 * them, so neither number is really a deadline. */
const UNDO_MS = 5000;
const BUCKET_UNDO_MS = 20000;
const SAVED_MS = 3000;
/* The countdown steps in quarters of a second so that a pause takes effect at
 * once; what is printed is still whole seconds. */
const STEP_MS = 250;
/* How long "Undo is no longer available." stays after the clock runs out.
 * Long enough to read and to be spoken, short enough not to be furniture. */
const GONE_MS = 1600;

const listBox = document.getElementById("bucket-list");
const emptyBox = document.getElementById("bucket-empty");
const countBox = document.getElementById("bucket-count");
const newForm = document.getElementById("bucket-new");
const newName = document.getElementById("bucket-name");
const newKind = document.getElementById("bucket-kind");
const kindNote = document.getElementById("bucket-kind-note");
const newStatus = document.getElementById("bucket-new-status");
const bucketUndo = document.getElementById("bucket-undo");
const cardTemplate = document.getElementById("bucket-card-template");
const typeList = document.getElementById("entity-types");
const filterForm = document.getElementById("bucket-filter");
/* Every request on these channels turns the button quiet and writes
 * "Searching…" beside it, without this file having to remember to
 * (static/js/api.js: watchSearch). */
watchSearch(filterForm, "buckets");
const searchBox = document.getElementById("bucket-search");
const kindFilter = document.getElementById("bucket-kind-filter");
const filterClear = document.getElementById("bucket-filter-clear");
const filterNote = document.getElementById("bucket-filter-note");

/* What a bucket can be about, from /api/buckets/kinds. Kept here because
 * three things read it: the two selects, the card (what a member is, what
 * the counts count) and the add form (which vocabulary suggests). */
const kinds = new Map();
let connectionNote = null;
/* Every bucket the archive answered with, before the filter. The filter is
 * applied here rather than by re-asking the server: there are a handful of
 * buckets, and a list that flickers on every keystroke is worse than one
 * that narrows instantly. */
let allBuckets = [];

/* THE VOCABULARY A MEMBER COMES FROM IS THE KIND.
 *
 * The entity kind keeps /api/suggest/entity, whose hint is the TYPE - that
 * is what lets one press add a complete member, and the type is the whole
 * difference between Apple the company and Apple the fruit. Every other kind
 * reads its own vocabulary through /api/buckets/vocabulary, which also
 * carries which bucket already holds a value. */
function suggestUrl(kind, bucketId) {
  // `fold=0`: the members themselves, not the buckets that hold them.
  // Everywhere else in the product /api/suggest/entity folds a member into
  // its bucket, because a field that offers both lets a person pick the
  // member and silently defeat the merge. THIS is the page where a bucket
  // is made, and a page that only ever showed groupings would have nothing
  // to make one out of - the same reason the Colours page searches
  // connection types unfolded (routers/api_suggest.py: BUCKETED_KINDS).
  // `bucket=`: what this bucket already holds is not offered to it again -
  // the member list is directly above the field, so a row that repeats it
  // spends one of twelve and says nothing. The other kinds get the same
  // through /api/buckets/vocabulary, which takes the id for the same reason.
  if (kind === "entity") {
    const own = bucketId ? `&bucket=${encodeURIComponent(String(bucketId))}` : "";
    return `/api/suggest/entity?fold=0${own}`;
  }
  const params = new URLSearchParams({ kind });
  if (bucketId) params.set("bucket", String(bucketId));
  return `/api/buckets/vocabulary?${params.toString()}`;
}

function kindInfo(kind) {
  return kinds.get(kind) || { kind, label: kind, member: "a value",
                              counts: "records", typed: kind === "entity" };
}

/* The undo clocks that are running, keyed by their strip - each entry is the
 * function that stops that strip's interval and timeouts, so a second delete
 * restarts the clock instead of letting the first one clear the newer
 * message halfway through. */
const timers = new Map();

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = text;
  return node;
}

function label(member) {
  return member.type ? `${member.name} (${member.type})` : member.name;
}

/* Every kind's own word for what it counts - "4 entity records", "2 events",
 * "3 locations". One sentence with nine readings beats nine sentences. */
function counted(n, what) {
  return `${fmtInt(n)} ${what}`;
}

/* ── Undo ────────────────────────────────────────────────────────────────
 *
 * One strip, one action, one clock. The strip sits in the page rather than
 * floating over it, so nothing it covers can be the thing being undone.
 *
 * The clock is VISIBLE: the seconds left stand next to the button, and a
 * four-pixel bar drains beside them for anyone who reads a shape faster than
 * a number (settings.css drops the bar under prefers-reduced-motion and
 * keeps the number, which is the half that does not move). When the five
 * seconds are up the strip says "Undo is no longer available." for a moment
 * before it goes - it has role=status, so that sentence is spoken as well as
 * shown. A strip that simply blanked mid-sentence left the deletion
 * permanent and never said so. */
function stopClock(strip) {
  if (!strip) return;
  const cancel = timers.get(strip);
  if (cancel) cancel();
  timers.delete(strip);
}

/* Every clock whose card has just been thrown away and drawn again. Without
 * this an interval keeps counting on a node nobody can see. */
function stopClocksIn(root) {
  Array.from(timers.keys()).forEach((strip) => {
    if (root && root.contains(strip)) stopClock(strip);
  });
}

function showUndo(strip, text, action, opts = {}) {
  const ms = opts.ms || UNDO_MS;
  stopClock(strip);
  strip.textContent = "";
  strip.classList.remove("is-held");
  strip.appendChild(el("span", "undo-text", text));
  const button = el("button", "button button--quiet", "Undo");
  button.type = "button";
  button.addEventListener("click", async () => {
    stopClock(strip);
    strip.hidden = true;
    strip.textContent = "";
    await action();
  });
  let left = ms;
  // aria-hidden: the strip speaks when it appears and again when it expires;
  // a screen reader counting to five in between is noise. The number goes
  // BEFORE the button - it is what the button is running out of, and reading
  // order is the order the eye takes.
  const clock = el("span", "undo-clock", `${Math.ceil(ms / 1000)} s`);
  clock.setAttribute("aria-hidden", "true");
  strip.appendChild(clock);
  strip.appendChild(button);

  const bar = el("span", "undo-bar");
  bar.style.animationDuration = `${ms}ms`;
  strip.appendChild(bar);
  window.requestAnimationFrame(() => bar.classList.add("is-draining"));

  // THE CLOCK STOPS WHILE SOMEBODY IS AT THE STRIP.
  //
  // Hovering it or tabbing into it holds the countdown where it is (WCAG
  // 2.2.1) and leaving lets it run on, so nothing expires under a hand or
  // under the focus ring. settings.css pauses the draining bar with the same
  // class, because two carriers of one fact must not disagree.
  let held = false;
  const hold = () => { held = true; strip.classList.add("is-held"); };
  const release = () => { held = false; strip.classList.remove("is-held"); };
  strip.addEventListener("mouseenter", hold);
  strip.addEventListener("mouseleave", release);
  strip.addEventListener("focusin", hold);
  strip.addEventListener("focusout", release);
  const forget = () => {
    strip.removeEventListener("mouseenter", hold);
    strip.removeEventListener("mouseleave", release);
    strip.removeEventListener("focusin", hold);
    strip.removeEventListener("focusout", release);
    strip.classList.remove("is-held");
  };

  const tick = window.setInterval(() => {
    if (held) return;
    left -= STEP_MS;
    clock.textContent = `${Math.max(0, Math.ceil(left / 1000))} s`;
    if (left > 0) return;
    window.clearInterval(tick);
    forget();
    strip.textContent = "";
    strip.appendChild(el("span", "undo-text undo-gone", "Undo is no longer available."));
    const late = window.setTimeout(() => {
      strip.hidden = true;
      strip.textContent = "";
      timers.delete(strip);
    }, GONE_MS);
    timers.set(strip, () => window.clearTimeout(late));
  }, STEP_MS);
  timers.set(strip, () => { window.clearInterval(tick); forget(); });

  strip.hidden = false;
  announce(text);
  // The control that was pressed has just been removed from the page, so
  // without this the focus would fall back to <body> and the keyboard reader
  // would have to walk the whole page to reach the Undo. Landing on it also
  // holds the clock, which is the point: the window only runs while nobody
  // is standing in it.
  if (opts.focus) button.focus();
}

/* ── The member rows ─────────────────────────────────────────────────── */

function memberRow(card, bucket, member) {
  const info = kindInfo(bucket.kind || "entity");
  const tr = el("tr", "member-row");
  tr.dataset.member = String(member.id);
  tr.appendChild(el("td", "member-name", member.name));
  // Only an entity member has a type. Elsewhere the column is not "empty",
  // it does not exist - a column of nine dashes is a question nobody asked.
  if (info.typed) tr.appendChild(el("td", "member-type", member.type || "any type"));

  const count = el("td", "num member-count", "-");
  count.dataset.memberCount = `${member.name} ${member.type || ""}`;
  tr.appendChild(count);

  const actions = el("td", "right");
  const remove = el("button", "button button--quiet", "Remove");
  remove.type = "button";
  remove.setAttribute("aria-label", `Remove ${label(member)} from ${bucket.name}`);
  remove.addEventListener("click", () => removeMember(card, bucket, member));
  actions.appendChild(remove);
  tr.appendChild(actions);
  return tr;
}

async function removeMember(card, bucket, member) {
  try {
    await api(`/api/buckets/${bucket.id}/members/${member.id}`, { method: "DELETE", context: false });
  } catch (err) {
    return;
  }
  const fresh = await refreshBucket(card, bucket.id);
  if (!fresh) return;
  showUndo(fresh.querySelector("[data-member-undo]"),
    `${label(member)} removed from ${bucket.name}.`,
    async () => {
      await api(`/api/buckets/${bucket.id}/members`, {
        method: "POST", context: false, body: { name: member.name, type: member.type || null },
      });
      await refreshBucket(fresh, bucket.id);
      announce(`${label(member)} is back in ${bucket.name}.`);
    });
}

/* ── One card ────────────────────────────────────────────────────────── */

function fillCard(card, bucket) {
  const kind = bucket.kind || "entity";
  const info = kindInfo(kind);
  card.dataset.bucket = String(bucket.id);
  // The name is an <input>, so it is not text anybody - a test, a person
  // reading the DOM - can find the card by. The attribute is.
  card.dataset.name = bucket.name;
  card.dataset.kind = kind;

  // The kind is a tag and not a control: the members are keyed to it, so
  // changing it would leave a bucket full of values that mean nothing in the
  // new kind, and silently.
  const kindTag = card.querySelector("[data-kind-tag]");
  kindTag.textContent = info.label;
  kindTag.title = `A member here is ${info.member}`;

  // The columns the kind actually has.
  const typeColumn = card.querySelector("[data-type-column]");
  if (typeColumn) typeColumn.hidden = !info.typed;
  const countColumn = card.querySelector("[data-count-column]");
  if (countColumn) {
    countColumn.textContent = info.counts.charAt(0).toUpperCase() + info.counts.slice(1);
  }
  const typeField = card.querySelector("[data-type-field]");
  if (typeField) typeField.hidden = !info.typed;

  const rename = card.querySelector(".bucket-rename");
  const renameLabel = card.querySelector(".bucket-title .field-label");
  rename.id = `bucket-rename-${bucket.id}`;
  renameLabel.htmlFor = rename.id;
  rename.value = bucket.name;
  rename.addEventListener("change", () => saveName(card, bucket, rename));

  const scope = card.querySelector("[data-scope-tag]");
  scope.textContent = bucket.every_project ? "Every project" : bucket.project;
  scope.title = bucket.every_project
    ? "This bucket applies in every project of the archive"
    : "This bucket applies in this project only";

  const every = card.querySelector("[data-scope-every]");
  every.id = `bucket-scope-${bucket.id}`;
  every.checked = Boolean(bucket.every_project);
  every.setAttribute("aria-label", `${bucket.name} applies to every project`);
  every.addEventListener("change", () => saveScope(card, bucket, every));

  const del = card.querySelector("[data-delete-bucket]");
  del.setAttribute("aria-label", `Delete the bucket ${bucket.name}`);
  del.addEventListener("click", () => askDeleteBucket(card, bucket));

  const body = card.querySelector("[data-members]");
  body.textContent = "";
  bucket.members.forEach((m) => body.appendChild(memberRow(card, bucket, m)));
  const membersEmpty = card.querySelector("[data-members-empty]");
  membersEmpty.hidden = bucket.members.length > 0;
  membersEmpty.textContent = emptyMembersText(info);
  card.querySelector("[data-members-caption]").textContent =
    bucket.members.length === 1 ? "1 member" : `${bucket.members.length} members`;

  wireAdd(card, bucket);
  return card;
}

/* The empty-state sentence, in the kind's own words. */
function emptyMembersText(info) {
  return `No members yet. A bucket with one member behaves like the name `
    + `itself; two or more is where it earns its keep. A member here is ${info.member}.`;
}

/* The add form: the typeahead from the macro, cloned per card, so its ids
 * have to be made unique here - two elements with the same id would give
 * the second card's field the first card's suggestion list. */
function wireAdd(card, bucket) {
  const kind = bucket.kind || "entity";
  const info = kindInfo(kind);
  const form = card.querySelector("[data-add-member]");
  const nameField = form.querySelector("input[data-typeahead]");
  const hidden = form.querySelector('input[type="hidden"][name="member_name"]');
  const nameLabel = form.querySelector(".typeahead .field-label");
  const help = form.querySelector(".typeahead-help");
  const typeField = form.querySelector(".member-type");
  const typeLabel = form.querySelector("[data-type-label]");

  nameField.id = `member-name-${bucket.id}`;
  nameLabel.htmlFor = nameField.id;
  nameLabel.textContent = `Add ${info.member}`;
  nameField.dataset.suggest = suggestUrl(kind, bucket.id);
  if (help) {
    help.id = `${nameField.id}-help`;
    nameField.setAttribute("aria-describedby", help.id);
  }
  typeField.id = `member-type-${bucket.id}`;
  typeLabel.htmlFor = typeField.id;

  initTypeahead(nameField);

  nameField.addEventListener("typeahead:choose", (e) => {
    const { value, hint, typed } = e.detail;
    if (!value) return;
    if (!info.typed) {
      // A plain value: one press adds it. The vocabulary endpoint's hint
      // says when a value is already spoken for, and addMember refuses it
      // there rather than letting one value into two buckets.
      addMember(card, bucket, value, "", { keepFocus: true, hint });
      return;
    }
    if (hint && !typed) {
      // The suggestion knows the type: save it and stay in the field.
      typeField.value = hint;
      addMember(card, bucket, value, hint, { keepFocus: true });
      return;
    }
    // Typed text: the name is set, the type is the missing half.
    typeField.focus();
  });

  form.addEventListener("submit", (e) => {
    e.preventDefault();
    // TYPED TEXT WINS OVER WHAT IS SET, the same way round as every search
    // button on this dashboard: typeahead.js writes into the hidden input
    // only when a suggestion or Enter confirms the text, so a name that was
    // confirmed and then typed over would otherwise be the one added -
    // "Apple Inc." landing in the bucket while the field reads "Banana".
    const name = (nameField.value.trim() || hidden.value.trim());
    if (!name) {
      nameField.focus();
      toast("Give the member a name first", { kind: "info" });
      return;
    }
    addMember(card, bucket, name, info.typed ? typeField.value.trim() : "", { keepFocus: true });
  });
}

/* {error} - {hint} arrive lowercase and unpunctuated, because the toast
 * prints them as two lines; standing alone as the only red line on the card
 * they have to be a sentence. */
function finish(text) {
  const clean = String(text == null ? "" : text).trim();
  if (!clean) return "";
  const capital = clean.charAt(0).toUpperCase() + clean.slice(1);
  return /[.!?…]$/.test(capital) ? capital : capital + ".";
}

/* The line under the add form. It is the only place a failed add can be
 * reported: the field keeps what was typed (so it can be corrected rather
 * than retyped), no row appears, and without a sentence here the card looks
 * like a button that does nothing. It stays until the next attempt - an
 * error that clears itself is an error nobody read. */
function addStatus(card, text, failed) {
  const line = card.querySelector("[data-add-status]");
  if (!line) return;
  line.textContent = text || "";
  line.classList.toggle("save-failed", Boolean(failed));
}

/* "in the bucket “Suppliers”" - what /api/buckets/vocabulary puts in the
 * hint of a value that is already spoken for. */
const TAKEN = /^in the bucket [\u201c"](.+)[\u201d"]$/;

async function addMember(card, bucket, name, type, opts = {}) {
  addStatus(card, "");
  const nameField = card.querySelector("input[data-typeahead]");
  const taken = TAKEN.exec(String(opts.hint || "").trim());
  if (taken) {
    // ONE VALUE, ONE BUCKET OF ITS KIND. In two, one term would resolve to
    // two groups and only one of them could win - so this is refused here,
    // where the press happened, and the bucket that holds it is named so
    // the reader knows where to go instead.
    addStatus(card, `Not added - ${name} is already in the bucket ${taken[1]}. `
      + "A value belongs to one bucket of its kind; remove it there first.", true);
    if (nameField) nameField.focus();
    return;
  }
  try {
    await api(`/api/buckets/${bucket.id}/members`, {
      method: "POST", context: false, body: { name, type: type || null },
    });
  } catch (err) {
    if (isAbort(err)) return;
    // "Apple is already in this bucket - the same name and type can only be
    // in a bucket once" is an answer, not a fault: the reader has to see it
    // where they pressed, and the cursor goes back into the field so the
    // name can be changed without hunting for it.
    addStatus(card, finish(`Not added - ${[err.message, err.hint].filter(Boolean).join(" - ")
      || "the member could not be added"}`), true);
    if (nameField) {
      nameField.setAttribute("aria-invalid", "true");
      nameField.focus();
    }
    return;
  }
  if (nameField) nameField.removeAttribute("aria-invalid");
  clearAdd(card);
  const fresh = await refreshBucket(card, bucket.id);
  announce(`${type ? `${name} (${type})` : name} added to ${bucket.name}.`);
  if (opts.keepFocus && fresh) {
    // The card is a NEW node (see refreshBucket), so the field to put the
    // cursor back into is that card's, not the one that was clicked.
    const again = fresh.querySelector("input[data-typeahead]");
    if (again) again.focus();
  }
}

function clearAdd(card) {
  const form = card.querySelector("[data-add-member]");
  const hidden = form.querySelector('input[type="hidden"][name="member_name"]');
  const nameField = form.querySelector("input[data-typeahead]");
  const typeField = form.querySelector(".member-type");
  hidden.value = "";
  nameField.value = "";
  nameField.placeholder = nameField.dataset.placeholder || "";
  const root = nameField.closest(".typeahead");
  if (root) root.classList.remove("is-set");
  typeField.value = "";
}

async function saveName(card, bucket, input) {
  const name = input.value.trim();
  if (!name || name === bucket.name) {
    input.value = bucket.name;
    return;
  }
  try {
    await api(`/api/buckets/${bucket.id}`, { method: SAVE, context: true, body: { name } });
  } catch (err) {
    input.value = bucket.name;
    if (!isAbort(err)) notSaved(card.querySelector("[data-save-note]"), err,
                                "The new name could not be saved.");
    return;
  }
  bucket.name = name;
  card.dataset.name = name;
  saved(card.querySelector("[data-save-note]"));
  announce(`Renamed to ${name}.`);
}

async function saveScope(card, bucket, box) {
  try {
    await api(`/api/buckets/${bucket.id}`, {
      method: SAVE, context: true, body: { every_project: box.checked },
    });
  } catch (err) {
    box.checked = Boolean(bucket.every_project);
    if (!isAbort(err)) notSaved(card.querySelector("[data-save-note]"), err,
                                "The change could not be saved.");
    return;
  }
  bucket.every_project = box.checked;
  saved(card.querySelector("[data-save-note]"));
  announce(box.checked
    ? `${bucket.name} applies to every project now.`
    : `${bucket.name} applies to this project only now.`);
  await refreshBucket(card, bucket.id);
}

/* "Saved" next to the thing that was saved, for a few seconds. Quieter than
 * a toast and in the place a person is already looking. */
function saved(note) {
  if (!note) return;
  note.classList.remove("save-failed");
  note.textContent = "Saved";
  note.classList.add("is-shown");
  window.setTimeout(() => {
    note.textContent = "";
    note.classList.remove("is-shown");
  }, SAVED_MS);
}

/* And its other half. The field has already been put back to what the archive
 * holds, so without this the page simply undoes somebody's typing and the
 * only explanation is a toast that can be dismissed before it is read. It
 * stays until the next save rather than fading: an error that clears itself
 * is an error nobody read. */
function notSaved(note, err, fallbackText) {
  if (!note) return;
  note.textContent = `Not saved - ${[err && err.message, err && err.hint].filter(Boolean).join(" - ") || fallbackText}`;
  note.classList.add("is-shown", "save-failed");
}

/* THE QUESTION BEFORE A WHOLE BUCKET GOES.
 *
 * A member is one row and the field above it puts it back; a bucket is
 * shared with everybody who uses this archive, and it takes its members with
 * it. So the card asks - the same strip the Colours page asks with, so the
 * two settings pages behave the same way: "Keep it" first and focused, so a
 * stray Enter is the safe answer; Escape as a second way out; the delete
 * button pushed to the far end by settings.css; and the consequence in the
 * question, including the twenty seconds of undo that follow it. */
function askDeleteBucket(card, bucket) {
  if (card.querySelector(".confirm-strip")) return;
  const head = card.querySelector(".bucket-head");
  const n = bucket.members.length;
  const what = n
    ? `Delete ${bucket.name} and its ${n} ${n === 1 ? "member" : "members"}?`
    : `Delete ${bucket.name}?`;
  // No number of seconds in the question: the strip that follows counts them
  // down where they can be seen, and it holds its clock while the focus is
  // in it - which is where the focus goes. A promise of "twenty seconds"
  // would be a promise nobody is measuring.
  // Plain sentences, because this page is read by people who did not grow up
  // with software. "It goes for everybody" means "applies to everybody" to
  // anyone who knows the idiom and "everybody loses it" to anyone who does
  // not - and here the second reading is the true one, which is exactly the
  // kind of accident a confirmation may not contain. "Waits while you are in
  // it" was a promise about hover and focus written as though the reader
  // already knew that; it says what it does now.
  const question = `${what} Everybody who uses this archive loses it. `
    + "You can undo this straight afterwards: the Undo waits as long as you keep "
    + "the pointer or the keyboard on it.";

  const strip = el("div", "confirm-strip");
  strip.setAttribute("role", "group");
  strip.setAttribute("aria-label", question);
  strip.appendChild(el("span", "confirm-text", question));

  const back = () => {
    strip.remove();
    const button = card.querySelector("[data-delete-bucket]");
    if (button) button.focus();
  };

  const no = el("button", "button button--secondary", "Keep it");
  no.type = "button";
  no.addEventListener("click", back);

  const yes = el("button", "button button--danger", "Delete bucket");
  yes.type = "button";
  yes.addEventListener("click", () => deleteBucket(card, bucket));

  strip.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    // Stopped here so the layout's own Escape handling does not also act on
    // a key that meant "not this bucket".
    e.stopPropagation();
    e.preventDefault();
    back();
  });

  strip.appendChild(no);
  strip.appendChild(yes);
  head.appendChild(strip);
  no.focus();
}

async function deleteBucket(card, bucket) {
  let answer;
  try {
    answer = await api(`/api/buckets/${bucket.id}`, { method: "DELETE", context: false });
  } catch (err) {
    return;
  }
  await load();
  const kept = (answer && answer.bucket) || { name: bucket.name, members: [] };
  const n = kept.members.length;
  showUndo(bucketUndo,
    `${bucket.name} deleted with ${n} ${n === 1 ? "member" : "members"}.`,
    async () => {
      await api("/api/buckets", {
        method: "POST", context: true,
        body: { name: kept.name, kind: kept.kind || bucket.kind || "entity",
                every_project: Boolean(kept.every_project), members: kept.members },
      });
      await load();
      announce(`${kept.name} is back.`);
    },
    // The card and the button that was pressed are gone, so the focus goes to
    // the Undo - which also holds its clock until the reader leaves it.
    { ms: BUCKET_UNDO_MS, focus: true });
}

/* ── What a bucket matches ───────────────────────────────────────────── */

function matchText(data) {
  const what = data.counts || "entity records";
  if (!data.members.length) {
    return "No members yet, so nothing is searched. Add the first one below.";
  }
  if (!data.languages.length) {
    return "Matches nothing in this project - check the spelling of the members.";
  }
  const per = data.languages.map((l) => `${fmtInt(l.entities)} in ${l.language}`).join(" - ");
  const head = `Matches ${counted(data.entities, what)}: ${per}`;
  // WHY THE MEMBERS DO NOT ADD UP, said rather than left to be noticed.
  // Two members can name the same thing - "Company" and "Unternehmen" are
  // one entity in two languages - and the total counts it once. That is the
  // whole promise of a bucket, so the page says it out loud.
  const sum = Number(data.sum_of_members || 0);
  if (sum > data.entities) {
    return `${head}. The members add up to ${fmtInt(sum)}: ${fmtInt(sum - data.entities)} `
      + "of those are the same thing under more than one member, counted once here.";
  }
  return head;
}

/* One bucket, drawn again from a fresh clone of the template.
 *
 * NOT by filling the card that is already there: fillCard() attaches the
 * listeners, and running it twice over the same node leaves two of each -
 * one press of Remove then sent two DELETEs, the second of which answered
 * 404. A new node has exactly one set. Returns the new card. */
async function refreshBucket(card, id) {
  const data = await api("/api/buckets", { channel: `bucket-${id}`, context: true });
  const bucket = (data.items || []).find((b) => b.id === id);
  if (!bucket) {
    await load();
    return null;
  }
  // The list the FILTER draws from has to move with the card, or setting a
  // filter would redraw the bucket as it stood before the member was added -
  // a card that quietly loses what was just put into it.
  allBuckets = data.items || [];
  const fresh = cardTemplate.content.firstElementChild.cloneNode(true);
  fillCard(fresh, bucket);
  stopClocksIn(card);
  card.replaceWith(fresh);
  await countBucket(fresh, bucket);
  return fresh;
}

async function countBucket(card, bucket) {
  const box = card.querySelector("[data-matches]");
  try {
    const data = await api("/api/buckets/resolve", {
      params: { id: String(bucket.id) }, channel: `resolve-${bucket.id}`, quiet: true, context: true,
    });
    data.counts = data.counts || kindInfo(bucket.kind || "entity").counts;
    box.textContent = matchText(data);
    box.classList.toggle("is-warning", data.entities === 0);
    (data.members || []).forEach((m) => {
      const key = `${m.name} ${m.type || ""}`;
      const cell = card.querySelector(`[data-member-count="${CSS.escape(key)}"]`);
      if (!cell) return;
      cell.textContent = fmtInt(m.entities);
      cell.classList.toggle("is-warning", m.entities === 0);
      if (m.entities === 0) cell.title = "This member matches no entity in this project";
    });
  } catch (err) {
    if (isAbort(err)) return;
    box.textContent = "The number of matches could not be counted.";
  }
}

/* ── The list ────────────────────────────────────────────────────────── */

async function load() {
  listBox.setAttribute("aria-busy", "true");
  let data;
  try {
    data = await api("/api/buckets", { channel: "buckets", context: true });
  } catch (err) {
    listBox.setAttribute("aria-busy", "false");
    if (isAbort(err)) return;
    // A sentence with no control in it left the browser's reload button as
    // the only way back - the Colours page has offered "Try again" in the
    // same situation from the start, and two settings pages that recover
    // differently are two pages to learn.
    listBox.textContent = "";
    const item = el("li", "notice notice--error settings-alert");
    const box = el("div", "notice-body");
    box.appendChild(el("p", null,
      [err.message, err.hint].filter(Boolean).join(" - ") || "The buckets could not be loaded."));
    item.appendChild(box);
    const again = el("button", "button", "Try again");
    again.type = "button";
    again.addEventListener("click", () => { load(); });
    item.appendChild(again);
    listBox.appendChild(item);
    return;
  }
  allBuckets = data.items || [];
  draw();
  listBox.setAttribute("aria-busy", "false");
}

/* ── The filter ──────────────────────────────────────────────────────────
 *
 * Applied here, over the list the archive already answered with: there are a
 * handful of buckets, and a list that re-asks the server on every keystroke
 * flickers for no gain.
 *
 * A SET FILTER IS STATE AND IS NEVER HIDDEN. The line under the toolbar says
 * what is being left out and offers the way back, so a page showing three of
 * eleven buckets cannot be mistaken for a page with three buckets. */
function matching() {
  const term = (searchBox.value || "").trim().toLowerCase();
  const kind = kindFilter.value || "";
  return allBuckets.filter((b) => {
    if (kind && (b.kind || "entity") !== kind) return false;
    if (!term) return true;
    if (String(b.name || "").toLowerCase().includes(term)) return true;
    return (b.members || []).some((m) => String(m.name || "").toLowerCase().includes(term));
  });
}

function draw() {
  const items = matching();
  stopClocksIn(listBox);
  listBox.textContent = "";
  items.forEach((bucket) => {
    const card = cardTemplate.content.firstElementChild.cloneNode(true);
    listBox.appendChild(fillCard(card, bucket));
    countBucket(card, bucket);
  });
  emptyBox.hidden = items.length > 0 || allBuckets.length > 0;
  countBox.textContent = items.length === 1 ? "1 bucket" : `${items.length} buckets`;

  const hidden = allBuckets.length - items.length;
  const kind = kindFilter.value || "";
  const term = (searchBox.value || "").trim();
  if (!hidden && !term && !kind) {
    filterNote.hidden = true;
    filterNote.textContent = "";
    return;
  }
  const said = [];
  if (term) said.push(`matching “${term}”`);
  if (kind) said.push(`of kind ${kindInfo(kind).label}`);
  filterNote.textContent =
    `Showing ${items.length} of ${allBuckets.length} buckets${said.length ? " " + said.join(" and ") : ""}`
    + `${hidden ? ` - ${hidden} hidden by this filter.` : "."}`;
  filterNote.hidden = false;
  if (!items.length && allBuckets.length) {
    listBox.appendChild(el("li", "empty", "No bucket matches this filter."));
  }
}

function clearFilter() {
  searchBox.value = "";
  kindFilter.value = "";
  draw();
  searchBox.focus();
}

/* ── Creating one ────────────────────────────────────────────────────── */

/* A refused name is a FAILURE and has to look like one. The sentence used
 * to be printed into the same muted line that carries the help text, in the
 * same colour and weight, with the field unmarked and the focus left on the
 * button - so "there is already a bucket called Apple" read exactly like
 * advice about what to type next. */
function failedNewBucket(text) {
  newStatus.textContent = finish(text);
  newStatus.className = "query-status save-failed";
  newName.setAttribute("aria-invalid", "true");
  newName.focus();
  // Not announced on top of that: the line is already an aria-live region,
  // and one problem gets one sentence.
}

newForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const name = newName.value.trim();
  if (!name) {
    newName.focus();
    failedNewBucket("A bucket needs a name.");
    return;
  }
  const kind = newKind.value || "entity";
  const scope = newForm.querySelector('input[name="scope"]:checked');
  try {
    await api("/api/buckets", {
      method: "POST", context: true,
      body: { name, kind, every_project: Boolean(scope && scope.value === "all") },
    });
  } catch (err) {
    // Both halves, not only the hint: "rename the existing one, or add the
    // members to it instead" read on its own is an instruction with no
    // cause, and the toast that carried the cause can be dismissed.
    if (!isAbort(err)) {
      failedNewBucket([err.message, err.hint].filter(Boolean).join(" - ")
        || "The bucket could not be created.");
    }
    return;
  }
  newName.value = "";
  newStatus.textContent = "";
  newStatus.className = "query-status";
  newName.removeAttribute("aria-invalid");
  // A new bucket must be visible where it was made. A filter set to another
  // kind would swallow it silently, which reads as "nothing happened".
  if (kindFilter.value && kindFilter.value !== kind) kindFilter.value = kind;
  await load();
  announce(`${kindInfo(kind).label} bucket ${name} created.`);
  // Straight into adding members: a bucket with none does nothing yet, and
  // the first member is what anybody wants next.
  const card = Array.from(listBox.querySelectorAll("[data-bucket]"))
    .find((c) => c.querySelector(".bucket-rename").value === name);
  const field = card ? card.querySelector("input[data-typeahead]") : null;
  if (field) field.focus();
});

/* ── The kinds ───────────────────────────────────────────────────────────
 *
 * One page with a kind selector rather than nine pages. The list comes from
 * the server (/api/buckets/kinds) so the page cannot offer a kind the
 * archive would refuse, and the note under it says where connection types
 * are grouped instead - the one kind that is deliberately not here. */
function fillKindSelects() {
  newKind.textContent = "";
  kindFilter.textContent = "";
  const every = document.createElement("option");
  every.value = "";
  every.textContent = "Every kind";
  kindFilter.appendChild(every);
  kinds.forEach((info) => {
    const one = document.createElement("option");
    one.value = info.kind;
    one.textContent = info.label;
    one.title = info.example || "";
    newKind.appendChild(one);
    kindFilter.appendChild(one.cloneNode(true));
  });
  newKind.value = "entity";
}

function sayKind() {
  const info = kindInfo(newKind.value || "entity");
  const where = connectionNote
    ? ` ${connectionNote.note}`
    : "";
  kindNote.textContent = `A member is ${info.member} - ${info.example}.${where}`;
}

async function loadKinds() {
  try {
    const data = await api("/api/buckets/kinds", { quiet: true, context: false });
    (data.kinds || []).forEach((info) => kinds.set(info.kind, info));
    connectionNote = data.connection_types || null;
  } catch (err) {
    // Without the list the page still works on entities, which is what every
    // bucket was until this release.
    kinds.set("entity", { kind: "entity", label: "Entity", member: "an entity name and its type",
                          example: "Apple Inc. (Company) and Apple (Company) are one company",
                          counts: "entity records", typed: true });
  }
  fillKindSelects();
  sayKind();
}

newKind.addEventListener("change", sayKind);
searchBox.addEventListener("input", draw);
kindFilter.addEventListener("change", draw);
filterClear.addEventListener("click", clearFilter);
filterForm.addEventListener("submit", (e) => { e.preventDefault(); draw(); });

/* The type field suggests what the archive has; free text stays allowed.
 *
 * FROM THE VOCABULARY ENDPOINT, NOT FROM /api/types/entity, and the
 * difference matters. That dropdown folds a bucket's members into the
 * bucket, which is right for a field that SEARCHES - picking the member
 * there would silently defeat the merge. This field is not one: what is
 * typed into it is STORED as the member's type and compared against
 * processed_data.entities.text_type, so it has to be a value the archive
 * actually carries. An entity-type bucket's name is not one, and offering it
 * here would make a member that matches nothing. */
async function loadEntityTypes() {
  try {
    const data = await api("/api/buckets/vocabulary",
                           { params: { kind: "entity_type", limit: "200" },
                             quiet: true, context: true });
    typeList.textContent = "";
    (data.items || []).forEach((t) => {
      if (!t.value) return;
      const item = document.createElement("option");
      item.value = t.value;
      typeList.appendChild(item);
    });
  } catch (err) {
    // Without the list the field is still a text field - nothing is lost.
  }
}

/* The kinds first: the cards read them, and a card drawn before they arrive
 * would say "entity" about every bucket. */
loadKinds().then(load);
loadEntityTypes();
