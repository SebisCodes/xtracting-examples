/* ==========================================================================
 *  Text field with a suggestion list ("typeahead").
 *
 *  The model is the input in the Xtracting UI: the visible field is ALWAYS
 *  EMPTY, the
 *  current value is shown as its placeholder. Whoever types overwrites;
 *  whoever types nothing keeps what is set. That saves selecting and
 *  deleting an existing value - and that is exactly where people fail
 *  who want to search for something else in a hurry.
 *
 *  FOUR DECISIONS:
 *
 *  1. THE LIST OPENS ON focus OR click. Focus alone is not enough: whoever
 *     closes the list without leaving the field keeps the focus - a second
 *     focus event never comes, and the field would be dead until one
 *     clicks elsewhere and back. That case was explicitly demanded.
 *
 *  2. SUGGESTIONS COME FROM THE HTML *OR* FROM THE SERVER. The original
 *     kept them in `data-options` only. The archive has hundreds of
 *     thousands of entities, so `data-suggest` names a URL that answers
 *     `?q=<typed>&limit=<n>` with JSON. Requests are debounced (200 ms)
 *     and the previous one is aborted, so a fast typist causes one request,
 *     not ten, and a slow answer never overwrites a newer one. While a
 *     request runs the previous list stays visible (nothing flickers).
 *
 *  3. THE TYPED TEXT IS NEVER REPLACED. NN/g's search-suggestion research
 *     is clear: a suggestion is an offer, the query stays the user's own.
 *     Enter with nothing highlighted therefore submits the typed text, and
 *     an empty list says so: "no suggestion - your text is still searched".
 *     Matching characters are marked in the suggestions so one sees WHY a
 *     suggestion is there.
 *
 *  4. SEARCHED DIFFERENTLY THAN SUGGESTED. A static option may carry
 *     `extra` text that is searched (type, description) but not shown, so
 *     a word from a description finds the matching keyword without the
 *     list filling up with descriptions.
 *
 *  ARIA follows the WAI-ARIA APG "editable combobox with list autocomplete":
 *  the input is the combobox, DOM focus stays on it, the highlighted option
 *  is announced through aria-activedescendant. See
 *  /home/sebi/Documents/Github/.references/ui/typeahead-input.md for the
 *  full contract and the test that pins it.
 *
 *  CSS WARNING: the list is absolutely positioned. An `overflow: hidden`
 *  or `overflow-y: auto` on ANY ancestor cuts it off - the HTML is all
 *  there, nothing is visible. It has vanished that way twice.
 *
 *  Markup the module expects (the Jinja macro `typeahead` emits it):
 *
 *    <div class="typeahead">
 *      <input type="hidden" name="entity" value="Apple Inc.">
 *      <input type="text" data-typeahead data-target="entity"
 *             data-suggest="/api/suggest/entity" data-min="0"
 *             data-options='[...]' data-empty-label="- all -"
 *             data-submit="false" placeholder="Apple Inc.">
 *    </div>
 *
 *  Events: the text input dispatches `typeahead:choose` (bubbles) with
 *  detail {value, label, typed, hint, via, ask}.
 *
 *  `typed` is true when Enter submitted the typed text rather than a
 *  suggestion. `via` is the GESTURE that ended the choice - "enter", "pick"
 *  (a click in the list) or "ask" (a control called set()) - and it is not
 *  the same question as `typed`: Enter on a highlighted suggestion is
 *  `typed: false` and still somebody asking.
 *
 *  `ask` IS THE ONE A VIEW SHOULD READ. It is true when the reader asked for
 *  an answer - Enter, or a control whose whole job is to ask, like the "Show
 *  only ..." button in a resolved notice - and false for a click in the
 *  suggestion list, which is somebody still writing the question. Views run
 *  their query on `ask` and take the value either way; the rule is stated
 *  once in app.css and this is where it is decided.
 *
 *  THE ONE EXCEPTION IS AN EMPTY VALUE - the "- clear the search -" row. A
 *  click on it is still a click in the list, but the reason the rule exists
 *  does not apply: clearing takes work away rather than starting it, and
 *  leaving it un-run would put the page in the one state this dashboard's
 *  search fields were built to make impossible - the field and the URL
 *  saying "nothing", the picture still showing the last thing asked for.
 *
 *  No framework, no dependency, one file.
 * ========================================================================== */

const DEBOUNCE_MS = 200;
const BLUR_GRACE_MS = 150;
const STATIC_LIMIT = 200;
const SERVER_LIMIT = 12;
const CACHE_SIZE = 50;

let openList = null; // the one list that is open right now
let counter = 0; // for unique ids

function closeAll(except) {
  document.querySelectorAll(".typeahead").forEach((root) => {
    if (root._typeahead && root._typeahead.popup !== except) root._typeahead.close();
  });
  if (openList !== except) openList = except || null;
}

/* Fold case and accents so "zuerich" finds "Zürich". Without this one
 * searches correctly on a Swiss keyboard and still finds nothing. */
export function normalise(text) {
  return String(text || "")
    .toLowerCase()
    .normalize("NFD")
    .replace(/[̀-ͯ]/g, "")
    .replace(/ä/g, "a").replace(/ö/g, "o").replace(/ü/g, "u")
    .replace(/ß/g, "ss");
}

/* Every option is a string, a pair [value, label] or an object
 * {value, label, extra, count, hint}. VALUE AND LABEL ARE TWO THINGS: the
 * timeframe filter sends "7d" and must show "Last 7 days". Without the
 * split the list would show a naked "7d". `extra` is searched but not
 * shown; `hint` is shown (lighter, after the label) - for entities it is
 * the type, which is what tells "Apple (Company)" from "Apple (Fruit)". */
export function toOption(o) {
  if (o === null || o === undefined) return null;
  if (typeof o === "string" || typeof o === "number") {
    return { value: String(o), label: String(o), extra: "", count: null, hint: "" };
  }
  if (Array.isArray(o)) {
    return { value: String(o[0]), label: String(o[1] ?? o[0]), extra: "", count: null, hint: "" };
  }
  // Only English key names. /api/suggest answers value/label/hint/count and
  // setOptions() is called with plain strings. `name` and `type` stay as
  // second spellings: they cost one `??` and spare the next caller a rename.
  const value = o.value ?? o.name ?? "";
  return {
    value: String(value),
    label: String(o.label ?? value),
    extra: String(o.extra ?? ""),
    count: o.count ?? null,
    hint: String(o.hint ?? o.type ?? ""),
  };
}

/* Wrap the matched characters in <mark>. The label is rendered as text
 * nodes plus one <mark>, never as HTML, so a label with "<" stays a label. */
function labelWithMark(label, filter) {
  const frag = document.createDocumentFragment();
  const f = normalise(filter);
  const at = f ? normalise(label).indexOf(f) : -1;
  // normalise() keeps the string length only when no character folds to
  // two ("ß" → "ss"); then the index does not line up and we mark nothing
  // rather than the wrong span.
  if (at < 0 || normalise(label).length !== label.length) {
    frag.appendChild(document.createTextNode(label));
    return frag;
  }
  frag.appendChild(document.createTextNode(label.slice(0, at)));
  const mark = document.createElement("mark");
  mark.textContent = label.slice(at, at + f.length);
  frag.appendChild(mark);
  frag.appendChild(document.createTextNode(label.slice(at + f.length)));
  return frag;
}

function parseOptions(field) {
  let data;
  try {
    data = JSON.parse(field.dataset.options || "[]");
  } catch (e) {
    console.warn("typeahead: data-options is not JSON", field, e);
    data = [];
  }
  return (Array.isArray(data) ? data : []).map(toOption).filter(Boolean);
}

/* Accept the shapes a suggestion endpoint may answer with: a bare array,
 * or an object whose `items`/`suggestions`/`results` holds the array. */
function itemsOf(json) {
  if (Array.isArray(json)) return json;
  if (json && typeof json === "object") {
    for (const key of ["items", "suggestions", "results"]) {
      if (Array.isArray(json[key])) return json[key];
    }
  }
  return [];
}

export function initTypeahead(field) {
  if (field._typeaheadInit) return field._typeaheadInit;
  field._typeaheadInit = true;

  const staticOptions = parseOptions(field);
  // `let`, not `const`: one field can be asked to suggest from a DIFFERENT
  // vocabulary without being rebuilt - the Diagrams search box changes what
  // it offers when the Object/Type switch beside it is pressed, and a
  // second box beside the first is the busyness that switch exists to
  // avoid. See setSuggestUrl() at the bottom of this function.
  let suggestUrl = field.dataset.suggest || "";
  const minChars = Math.max(0, parseInt(field.dataset.min || "0", 10) || 0);
  const limit = Math.max(1, parseInt(field.dataset.limit || String(SERVER_LIMIT), 10) || SERVER_LIMIT);
  const emptyLabel = field.dataset.emptyLabel; // undefined → no reset row
  const submitOnChoose = field.dataset.submit !== "false";
  const noSuggestionText = field.dataset.noSuggestion || "No suggestion - your text is still searched";
  // Present tense and an ellipsis: it says what is happening now, and it is
  // replaced the moment the answer arrives.
  const LOADING_TEXT = "Searching\u2026";

  // The wrapper is the positioning context. The macro emits it; a bare
  // input (as in the tests) gets one here.
  let root = field.closest(".typeahead");
  if (!root) {
    root = document.createElement("div");
    root.className = "typeahead";
    field.parentNode.insertBefore(root, field);
    root.appendChild(field);
  }
  const hidden =
    root.querySelector(`input[type="hidden"][name="${CSS.escape(field.dataset.target || "")}"]`) ||
    (field.dataset.target && root.parentNode
      ? root.parentNode.querySelector(`input[type="hidden"][name="${CSS.escape(field.dataset.target)}"]`)
      : null);

  counter += 1;
  const uid = field.id || `typeahead-${counter}`;
  if (!field.id) field.id = uid;

  const popup = document.createElement("div");
  popup.className = "typeahead-popup";
  popup.hidden = true;

  const list = document.createElement("ul");
  list.className = "typeahead-list";
  list.id = `${uid}-listbox`;
  list.setAttribute("role", "listbox");
  list.setAttribute("aria-label", field.getAttribute("aria-label") || labelText(field) || "Suggestions");
  popup.appendChild(list);

  // Notes live OUTSIDE the listbox: a listbox may only contain options,
  // and "no suggestion" is a message, not a choice.
  const note = document.createElement("div");
  note.className = "typeahead-note";
  note.hidden = true;
  popup.appendChild(note);

  root.appendChild(popup);

  // A polite live region tells a screen-reader user how many suggestions
  // there are; the visual user sees the list.
  const live = document.createElement("div");
  live.className = "visually-hidden";
  live.setAttribute("aria-live", "polite");
  root.appendChild(live);

  field.setAttribute("role", "combobox");
  field.setAttribute("aria-autocomplete", "list");
  field.setAttribute("aria-expanded", "false");
  field.setAttribute("aria-controls", list.id);
  field.setAttribute("aria-haspopup", "listbox");
  field.autocomplete = "off";
  field.spellcheck = false;

  let highlighted = -1;
  let options = staticOptions; // what is currently rendered
  let serverOptions = []; // last server answer
  /* THE TERM AN ANSWER IS OUT FOR, or null when nothing is in flight.
   *
   * Without it the list says "No suggestion - your text is still searched"
   * from the first keystroke until the answer lands, which is not what is
   * happening: nothing has been asked yet. On a real archive some of these
   * lists take a second or two, and an empty popup under a field somebody
   * is typing into reads as a broken page. */
  let pending = null;
  let lastFilter = null; // the filter the list was rendered for
  let timer = null;
  let controller = null;
  let blurTimer = null;
  let requestSeq = 0;
  const labels = new Map(); // value → label of everything ever seen
  const cache = new Map(); // q → options (bounded)
  staticOptions.forEach((o) => labels.set(o.value, o));

  function labelOf(value) {
    const o = labels.get(value);
    return o ? o.label : value;
  }

  function setSetState() {
    const has = !!(hidden && hidden.value);
    root.classList.toggle("is-set", has);
    field.classList.toggle("is-set", has);
  }
  setSetState();

  function entries() {
    return list.querySelectorAll('li[role="option"]');
  }

  function setHighlight(index) {
    const lis = entries();
    highlighted = index;
    lis.forEach((li, i) => {
      const on = i === highlighted;
      li.classList.toggle("is-active", on);
      li.setAttribute("aria-selected", on ? "true" : "false");
    });
    const li = lis[highlighted];
    if (li) {
      field.setAttribute("aria-activedescendant", li.id);
      li.scrollIntoView({ block: "nearest" });
    } else {
      field.removeAttribute("aria-activedescendant");
    }
  }

  function setNote(text) {
    note.textContent = text || "";
    note.hidden = !text;
  }

  function render(filter, source) {
    const f = normalise(filter);
    const fromServer = source === "server";
    const hits = fromServer
      ? serverOptions.slice(0, limit)
      : staticOptions
          .filter((o) => !f || normalise(o.value).includes(f) || normalise(o.label).includes(f) || normalise(o.extra).includes(f))
          .slice(0, STATIC_LIMIT);
    options = hits;
    lastFilter = filter;

    list.textContent = "";
    highlighted = -1;
    field.removeAttribute("aria-activedescendant");

    // The reset row clears the filter. Without it one has to guess how to
    // get rid of a selection. It is optional: a search box has no "all".
    let n = 0;
    if (emptyLabel !== undefined) {
      const li = document.createElement("li");
      li.className = "typeahead-reset";
      li.setAttribute("role", "option");
      li.setAttribute("aria-selected", "false");
      li.id = `${uid}-opt-reset`;
      li.dataset.value = "";
      li.textContent = emptyLabel || "- all -";
      list.appendChild(li);
      n += 1;
    }

    hits.forEach((o, i) => {
      labels.set(o.value, o);
      const li = document.createElement("li");
      li.setAttribute("role", "option");
      li.setAttribute("aria-selected", "false");
      li.id = `${uid}-opt-${i}`;
      li.dataset.value = o.value;
      li.dataset.label = o.label;
      if (o.hint) li.dataset.hint = o.hint;
      const text = document.createElement("span");
      text.className = "typeahead-label";
      text.appendChild(labelWithMark(o.label, filter));
      li.appendChild(text);
      if (o.hint) {
        const h = document.createElement("span");
        h.className = "typeahead-hint";
        h.textContent = o.hint;
        // The hint is one line and is cut when it is longer than the list is
        // wide (typeahead.css). A bucket's hint names every entity merged
        // into it, so what is cut is worth keeping within reach.
        h.title = o.hint;
        li.appendChild(h);
      }
      if (o.count !== null && o.count !== undefined && o.count !== "") {
        const c = document.createElement("span");
        c.className = "typeahead-count";
        c.textContent = String(o.count);
        li.appendChild(c);
      }
      list.appendChild(li);
      n += 1;
    });

    if (!hits.length) {
      // NOTHING FOUND and NOT ASKED YET are two different answers, and only
      // one of them means "there is nothing".
      setNote(pending ? LOADING_TEXT : noSuggestionText);
      live.textContent = pending ? "Searching" : "No suggestions";
    } else {
      setNote("");
      live.textContent = `${hits.length} suggestion${hits.length === 1 ? "" : "s"}`;
    }
    return n;
  }

  function open() {
    if (!popup.hidden && lastFilter === field.value) return;
    show();
    refresh(field.value, /* immediately */ true);
  }

  function show() {
    popup.hidden = false;
    field.setAttribute("aria-expanded", "true");
    closeAll(popup);
  }

  function close() {
    if (timer) { clearTimeout(timer); timer = null; }
    popup.hidden = true;
    field.setAttribute("aria-expanded", "false");
    field.removeAttribute("aria-activedescendant");
    highlighted = -1;
    if (openList === popup) openList = null;
  }

  /* Decide where the suggestions come from and draw them. Static options
   * are drawn at once; server suggestions after the debounce, keeping the
   * previous rows on screen until the answer is in. */
  function refresh(filter, immediately) {
    if (!suggestUrl) {
      render(filter, "static");
      return;
    }
    if (filter.length < minChars) {
      pending = null;
      serverOptions = [];
      render(filter, "server");
      setNote(`Type at least ${minChars} character${minChars === 1 ? "" : "s"}`);
      return;
    }
    const cached = cache.get(filter);
    if (cached) {
      pending = null;
      serverOptions = cached;
      render(filter, "server");
      return;
    }
    if (timer) clearTimeout(timer);
    // Said while the debounce is still counting, not only once the request
    // is out: those 200 ms are part of the wait the reader sees.
    pending = filter;
    if (!popup.hidden) render(filter, "server");
    timer = setTimeout(() => {
      timer = null;
      fetchSuggestions(filter);
    }, immediately ? 0 : DEBOUNCE_MS);
  }

  function fetchSuggestions(filter) {
    if (controller) controller.abort();
    controller = new AbortController();
    const seq = ++requestSeq;
    const url = new URL(suggestUrl, window.location.href);
    url.searchParams.set("q", filter);
    url.searchParams.set("limit", String(limit));
    // The pair the page was rendered for (the layout stamps it on <body>),
    // unless the data-suggest URL pins its own. Suggestions must come from
    // the project on screen - the cookies alone can lag behind a shared
    // link that names another project.
    const body = document.body ? document.body.dataset : {};
    if (body.project && !url.searchParams.has("project")) url.searchParams.set("project", body.project);
    if (body.language && !url.searchParams.has("language")) url.searchParams.set("language", body.language);
    field.setAttribute("aria-busy", "true");
    fetch(url.toString(), { signal: controller.signal, headers: { Accept: "application/json" } })
      .then((res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        return res.json();
      })
      .then((json) => {
        if (seq !== requestSeq) return; // a newer request is on its way
        const opts = itemsOf(json).map(toOption).filter(Boolean);
        cache.set(filter, opts);
        if (cache.size > CACHE_SIZE) cache.delete(cache.keys().next().value);
        pending = null;
        serverOptions = opts;
        // Only redraw if the field still shows what we asked for; otherwise
        // the debounce is already scheduling the right request.
        if (!popup.hidden && field.value === filter) render(filter, "server");
      })
      .catch((err) => {
        if (err.name === "AbortError" || seq !== requestSeq) return;
        console.warn("typeahead: suggestions failed", err);
        pending = null;
        serverOptions = [];
        if (!popup.hidden) {
          render(filter, "server");
          setNote("Suggestions unavailable - your text is still searched");
        }
      })
      .finally(() => {
        if (seq === requestSeq) field.removeAttribute("aria-busy");
      });
  }

  /* WHAT THE FIELD SHOWS ONCE SOMETHING IS CHOSEN.
   *
   * Usually the label: the timeframe filter's value is "7d" and nobody
   * wants to read that, so "Last 7 days" is what stands in the field.
   *
   * A HINTED suggestion is the other case. There the value is not a code
   * for the label but a fuller spelling of it - an entity carries its type,
   * "Apple (Company)" behind the label "Apple" (routers/api_suggest.py) -
   * and the field has to show the fuller one, because that is what the URL
   * carries, what the notice under the box says, and what the field itself
   * shows again after a reload. Showing "Apple" there left three different
   * Apples looking like one choice. */
  function shownFor(value, label, hint) {
    if (!value) return field.dataset.placeholder || emptyLabel || "";
    const text = label || labelOf(value);
    return hint && text && text !== value ? value : text;
  }

  function choose(value, label, typed, hint, via) {
    field.value = ""; // the field stays empty
    if (hidden) hidden.value = value;
    field.placeholder = shownFor(value, label, hint);
    setSetState();
    close();
    field.dispatchEvent(new CustomEvent("typeahead:choose", {
      bubbles: true,
      detail: {
        value, label: value ? (label || labelOf(value)) : "",
        typed: !!typed, hint: hint || "", via: via || "ask",
        ask: via !== "pick" || !value,
      },
    }));
    if (submitOnChoose && field.form) {
      if (typeof field.form.requestSubmit === "function") field.form.requestSubmit();
      else field.form.submit();
    }
  }

  function chooseEntry(li, via) {
    if (!li) return;
    choose(li.dataset.value, li.dataset.label || li.textContent, false,
           li.dataset.hint || "", via);
  }

  field.dataset.placeholder = field.dataset.placeholder || field.placeholder;

  // HERE IS THE DEMANDED POINT: focus AND click.
  field.addEventListener("focus", () => { if (blurTimer) { clearTimeout(blurTimer); blurTimer = null; } open(); });
  field.addEventListener("click", open);
  field.addEventListener("input", () => {
    show();
    refresh(field.value, false);
  });

  field.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      if (popup.hidden) open();
      const lis = entries();
      if (!lis.length) return;
      // Alt+Down opens without moving the highlight (APG, optional).
      if (e.altKey) return;
      let next = highlighted + (e.key === "ArrowDown" ? 1 : -1);
      if (next < 0) next = lis.length - 1; // wrap
      if (next >= lis.length) next = 0;
      setHighlight(next);
    } else if (e.key === "Home" && !popup.hidden && highlighted >= 0) {
      e.preventDefault();
      setHighlight(0);
    } else if (e.key === "End" && !popup.hidden && highlighted >= 0) {
      e.preventDefault();
      setHighlight(entries().length - 1);
    } else if (e.key === "Enter") {
      const lis = entries();
      // A highlighted suggestion wins; otherwise the typed text counts -
      // one need not be in the list to search for it.
      e.preventDefault();
      if (!popup.hidden && highlighted >= 0 && lis[highlighted]) {
        chooseEntry(lis[highlighted], "enter");
      } else {
        const typed = field.value.trim();
        if (typed) {
          choose(typed, typed, true, "", "enter");
        } else {
          // Nothing typed: keep what is set (that is the whole point of the
          // placeholder) and let the form go if there is one.
          close();
          if (submitOnChoose && field.form) {
            if (typeof field.form.requestSubmit === "function") field.form.requestSubmit();
            else field.form.submit();
          }
        }
      }
    } else if (e.key === "Escape") {
      if (!popup.hidden) {
        e.preventDefault();
        close();
      } else if (field.value) {
        // APG: Escape on a closed popup clears the text (optional).
        field.value = "";
      }
    } else if (e.key === "Tab") {
      close();
    }
  });

  popup.addEventListener("mousedown", (e) => {
    // mousedown, not click: blur would fire first and close the list
    // before the click arrived.
    const li = e.target.closest('li[role="option"]');
    if (!li) { e.preventDefault(); return; }
    e.preventDefault();
    chooseEntry(li, "pick");
  });

  field.addEventListener("blur", () => {
    // Wait briefly so a click in the list still gets through.
    blurTimer = setTimeout(() => { blurTimer = null; close(); }, BLUR_GRACE_MS);
  });

  const api = {
    field, hidden, popup, list, open, close, choose,
    get value() { return hidden ? hidden.value : ""; },
    // ASKS, because its only caller is a button that asks: resolved.js's
    // "Show only Apple Inc." hands the term over here and expects the view
    // to answer it. A silent way to fill the field would be a different
    // method, and nothing has needed one.
    set(value, label) { choose(value, label, false, "", "ask"); },
    /* EMPTIED WITHOUT A WORD TO ANYBODY. `set("")` would do the same to the
     * value and then announce it and submit the form - which is right when
     * a person picks the reset row, and wrong when the Clear button beside
     * Search is emptying six fields at once: six announcements and six
     * searches for one press. The caller submits, once, when it is done. */
    clear() {
      field.value = "";
      if (hidden) hidden.value = "";
      field.placeholder = shownFor("", "", "");
      setSetState();
      close();
    },
    setOptions(newOptions) {
      staticOptions.length = 0;
      newOptions.map(toOption).filter(Boolean).forEach((o) => { staticOptions.push(o); labels.set(o.value, o); });
      if (!popup.hidden) render(field.value, suggestUrl ? "server" : "static");
    },
    /* Suggest from a different vocabulary from now on.
     *
     * The CACHE has to go with it: it is keyed by what was typed, not by
     * where the answer came from, so keeping it would offer the entity
     * names that were fetched a moment ago as if they were entity types.
     * An answer already in flight is dropped for the same reason. */
    setSuggestUrl(url) {
      const next = String(url || "");
      if (next === suggestUrl) return;
      suggestUrl = next;
      field.dataset.suggest = next;
      cache.clear();
      serverOptions = [];
      if (controller) { controller.abort(); controller = null; }
      if (timer) { clearTimeout(timer); timer = null; }
      if (!popup.hidden) refresh(field.value, true);
    },
  };
  root._typeahead = api;
  field._typeahead = api;
  return api;
}

function labelText(field) {
  if (field.labels && field.labels.length) return field.labels[0].textContent.trim();
  const l = field.closest("label");
  return l ? l.textContent.trim() : "";
}

export function initAll(scope) {
  (scope || document).querySelectorAll("input[data-typeahead]").forEach(initTypeahead);
}

document.addEventListener("click", (e) => {
  if (!e.target.closest(".typeahead")) closeAll(null);
});

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", () => initAll(document));
} else {
  initAll(document);
}
