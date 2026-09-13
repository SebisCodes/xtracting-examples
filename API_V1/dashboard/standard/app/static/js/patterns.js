/* ==========================================================================
 *  Patterns, as a person reads them.
 *
 *  Three jobs, all of them about the same thing - a rule that decides which
 *  links a crawl takes must be readable without a regular expression:
 *
 *    shapeOf(url)        the shape of an address, for GROUPING the picker:
 *                        /rent/# instead of twenty separate rows.
 *    renderRuleChips()   the chips in the editor: human label, how many
 *                        links of the last test it explains, the regex one
 *                        disclosure away, and a way to take it off again.
 *    checkAddress()      the live verdict under the "add one address" field.
 *
 *  WHY THE SHAPE IS COMPUTED TWICE. crawlkit/forms.py owns the real rule
 *  and its result is what gets stored; this file mirrors it for the picker
 *  only, so that twenty flats collapse into one group WHILE the dialog is
 *  open, with no request per keystroke. The moment "Use the ticked links"
 *  is pressed, the server answers with the real patterns and their labels
 *  replace what was computed here. A drift between the two would show as a
 *  group split in two - never as a wrong rule, because no rule is made
 *  here. The mirrored rules are: a path piece of digits is #, a piece with
 *  a digit in it or longer than 24 characters is *, a two-letter code from
 *  the list is {lang}, everything else stays as it is written; query keys
 *  are kept in sorted order and paging keys are dropped.
 * ========================================================================== */

/* Query keys a site turns pages with. Same list as crawlkit/forms.py. */
export const PAGING_KEYS = new Set([
  "page", "currentpage", "p", "pn", "s", "seite", "offset", "start", "ep",
  "pg", "skip",
]);

const LONG_SEGMENT = 24;

const LANG_CODES = new Set([
  "de", "fr", "it", "rm", "en", "es", "pt", "nl", "pl", "cs", "sk", "sl",
  "hu", "ro", "bg", "hr", "sr", "bs", "sq", "mk", "el", "tr", "ru", "uk",
  "be", "lt", "lv", "et", "fi", "sv", "da", "no", "nb", "nn", "is", "ga",
  "cy", "ca", "eu", "gl", "ar", "he", "fa", "hi", "ur", "bn", "ta", "th",
  "vi", "id", "ms", "ko", "ja", "zh",
]);

function segmentLabel(piece) {
  if (/^[0-9]+$/.test(piece)) return "#";
  if (piece.length > LONG_SEGMENT || /[0-9]/.test(piece)) return "*";
  if (LANG_CODES.has(piece.toLowerCase())) return "{lang}";
  return piece.toLowerCase();
}

/* {host, path, label, key} for one address. `key` includes the host, so two
 * sites with the same path shape are two groups - a link to another site is
 * a different decision from a link on this one. */
export function shapeOf(url) {
  let parsed;
  try {
    parsed = new URL(url);
  } catch (e) {
    return { host: "", path: "", label: String(url || ""), key: String(url || "") };
  }
  const pieces = parsed.pathname.split("/").filter(Boolean).map(segmentLabel);
  const keys = [];
  parsed.searchParams.forEach((_value, key) => {
    if (!PAGING_KEYS.has(key.toLowerCase()) && !keys.includes(key)) keys.push(key);
  });
  keys.sort();
  let label = "/" + pieces.join("/");
  if (keys.length) label += "?" + keys.join("&");
  return { host: parsed.host, path: parsed.pathname, label, key: parsed.host + label };
}

/* The paging key of an address, as it is written there ("currentPage", not
 * "currentpage") - what the editor shows in the paging field. */
export function pagingKeyOf(url) {
  try {
    for (const [key] of new URL(url).searchParams) {
      if (PAGING_KEYS.has(key.toLowerCase())) return key;
    }
  } catch (e) { /* not an address; no paging key */ }
  return "";
}

export function isHttpUrl(value) {
  const text = String(value || "").trim();
  if (!/^https?:\/\//i.test(text)) return false;
  try {
    return Boolean(new URL(text).host);
  } catch (e) {
    return false;
  }
}

/* The live verdict under the "add one address" field: {ok, message}. It is
 * a sentence, not a red border - a border says something is wrong and not
 * what (NN/g on error messages). */
export function checkAddress(value, known = []) {
  const text = String(value || "").trim();
  if (!text) return { ok: false, message: "" };
  if (!isHttpUrl(text)) {
    return { ok: false, message: "An address starts with http:// or https:// - paste it from the browser." };
  }
  const canon = text.replace(/#.*$/, "");
  if (known.some((row) => (row.text_url_canonical || "") === canon)) {
    return { ok: false, message: "This address is already in the list below." };
  }
  return { ok: true, message: "Ready to add." };
}

/* Live validation of a hand-typed address against the last test: how many
 * of the links it found have the same shape, and what that shape is called.
 * It is the answer to the question somebody typing an address is really
 * asking - "is this one of many, or the only one?" - and it is the sentence
 * that teaches what a pattern is without the word appearing. */
export function shapeNote(url, links) {
  if (!isHttpUrl(url)) return "";
  const shape = shapeOf(url);
  const same = (links || []).filter((link) => shapeOf(link.url).key === shape.key).length;
  if (!links || !links.length) return `Its shape is ${shape.label}.`;
  if (!same) return `Its shape is ${shape.label}; no link of the last test has it.`;
  return same === 1
    ? `Its shape is ${shape.label}, like 1 link of the last test.`
    : `Its shape is ${shape.label}, like ${same} links of the last test.`;
}

/* ── The chips ───────────────────────────────────────────────────────── */

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function explains(row) {
  const n = Number(row.integer_examples || 0);
  if (!n) return "no example from the last test";
  return n === 1 ? "1 link of the last test" : `${n} links of the last test`;
}

/* One chip: label, what it explains, the regex behind a disclosure, and a
 * remove button that names what it removes (a row of buttons all labelled
 * "Remove" is unusable with a screen reader). */
/* Which key evaluates the links one rule matches.
 *
 * Only on accept rules: a reject rule says what is never collected, and a key
 * on it would be a key for documents that are never submitted.
 *
 * Only when there is a list to choose from - a project with keys. Without one
 * the control would be an empty box next to a rule, which reads as a setting
 * that failed to load rather than one that does not apply. */
function keyChooser(row, keys, onKey, index) {
  const wrap = el("div", "rule-key");
  const id = `rule-key-${index}`;
  const label = el("label", "rule-key-label", "Key");
  label.htmlFor = id;
  wrap.appendChild(label);

  const select = document.createElement("select");
  select.id = id;
  select.className = "rule-key-select";

  const fallback = document.createElement("option");
  fallback.value = "";
  fallback.textContent = "As the watchlist";
  select.appendChild(fallback);

  keys.forEach((key) => {
    const option = document.createElement("option");
    option.value = key.prefix;
    option.textContent = key.name;
    select.appendChild(option);
  });

  const chosen = row.text_key_prefix || "";
  if (chosen && !keys.some((k) => k.prefix === chosen)) {
    /* Pinned to a key the crawler no longer has. Kept selected and named,
     * because these links have stopped being evaluated and a control that
     * silently fell back to "As the watchlist" would hide that. */
    const gone = document.createElement("option");
    gone.value = chosen;
    gone.textContent = `${chosen} - no longer works`;
    select.appendChild(gone);
  }
  select.value = chosen;
  select.addEventListener("change", () => onKey(index, select.value));
  wrap.appendChild(select);
  return wrap;
}

function ruleChip(row, onRemove, { keys = null, onKey = null, index = 0 } = {}) {
  const chip = el("li", "rule-chip");
  chip.dataset.kind = row.text_kind || "accept";
  chip.dataset.review = row.bool_needs_review ? "true" : "false";
  chip.dataset.label = row.text_label || "";

  const body = el("div");
  body.appendChild(el("span", "rule-label", row.text_label || row.text_regex || "?"));
  const count = el("span", "rule-count", " - " + explains(row));
  body.appendChild(count);

  if (row.bool_needs_review) {
    const note = el("p", "rule-count", "Checked by hand: this shape also fitted a link you left out, so that one address is rejected on its own.");
    body.appendChild(note);
  }

  const detail = el("details", "rule-detail");
  const summary = el("summary", null, "What it matches exactly");
  detail.appendChild(summary);
  const pre = el("pre", null, row.text_regex || "");
  detail.appendChild(pre);
  if (row.text_example_url) {
    detail.appendChild(el("p", "rule-count", "For example: " + row.text_example_url));
  }
  body.appendChild(detail);

  if (keys && keys.length && onKey && (row.text_kind || "accept") === "accept") {
    body.appendChild(keyChooser(row, keys, onKey, index));
  }
  chip.appendChild(body);

  if (onRemove) {
    const remove = el("button", "rule-remove", "×");
    remove.type = "button";
    remove.setAttribute("aria-label", `Remove the rule ${row.text_label || row.text_regex}`);
    remove.addEventListener("click", () => onRemove(row));
    chip.appendChild(remove);
  }
  return chip;
}

/* Draw the chips of one group. `empty` is the paragraph shown instead when
 * there are none - never an empty box: a box with nothing in it says
 * "loading" to most people. */
export function renderRuleChips(list, rows, { empty = null, onRemove = null,
                                              keys = null, onKey = null } = {}) {
  if (!list) return;
  list.textContent = "";
  (rows || []).forEach((row, index) =>
    list.appendChild(ruleChip(row, onRemove, { keys, onKey, index })));
  if (empty) empty.hidden = Boolean(rows && rows.length);
}

/* The single addresses. Two kinds in one list, each saying which it is -
 * "watched" and "never collected" are opposites and must not be told apart
 * by a colour. */
export function renderExactChips(list, rows, { empty = null, onRemove = null } = {}) {
  if (!list) return;
  list.textContent = "";
  (rows || []).forEach((row) => {
    const monitor = (row.text_kind || "monitor") === "monitor";
    const chip = el("li", "rule-chip");
    chip.dataset.kind = monitor ? "accept" : "reject";
    const body = el("div");
    body.appendChild(el("span", "rule-label", row.text_url_canonical || ""));
    body.appendChild(el("span", "rule-count", monitor ? " - watched" : " - never collected"));
    chip.appendChild(body);
    if (onRemove) {
      const remove = el("button", "rule-remove", "×");
      remove.type = "button";
      remove.setAttribute("aria-label", `Remove ${row.text_url_canonical}`);
      remove.addEventListener("click", () => onRemove(row));
      chip.appendChild(remove);
    }
    list.appendChild(chip);
  });
  if (empty) empty.hidden = Boolean(rows && rows.length);
}
