/* ==========================================================================
 *  URL ⇄ state.
 *
 *  Six things describe what a view shows: q (the search term), project,
 *  language, timeframe, page and tab. They live in the URL, and only there:
 *  reloading the page shows the same picture, a copied link shows it to a
 *  colleague, and the Export links (export.js) can read what to export by
 *  reading location.search.
 *
 *  The rule that follows from this:
 *  the search field is filled FROM q once, when the page loads; every
 *  request reads state.q. Loading the diagrams, switching a tab, paging -
 *  none of them touches the field, so none of them can lose the term.
 *
 *  `set()` uses replaceState, not pushState: paging through a chart is not
 *  navigation, and a Back button that walks through forty pages of a
 *  drilldown is a trap.
 *
 *  Every change fires `state:change` on `document` with {detail: {changed,
 *  state}} so listeners (export links, the top bar) can follow without
 *  polling. Views subscribe with `onChange(fn)`.
 * ========================================================================== */

/* `perspective` and `min_importance` are here for the same reason project and
 * language are: they are not a search, they are which slice of the archive
 * every view is showing, and every request and every Export link has to carry
 * them or the file a person downloads holds rows the screen never showed.
 * "" for the perspective is All, which is no filter. */
export const KEYS = ["q", "project", "language", "perspective", "min_importance",
                     "timeframe", "page", "tab"];

export const state = {
  q: "",
  project: "",
  language: "",
  perspective: "",
  min_importance: "",
  timeframe: "",
  page: 0,
  tab: "",
};

function clean(key, value) {
  if (value === null || value === undefined) return key === "page" ? 0 : "";
  if (key === "page") {
    const n = parseInt(value, 10);
    return Number.isFinite(n) && n > 0 ? n : 0;
  }
  return String(value).trim();
}

/* Load the state from the current URL. Called once on import and again on
 * popstate, so a Back/Forward that does change the URL is honoured. */
export function read() {
  const p = new URLSearchParams(window.location.search);
  KEYS.forEach((k) => { state[k] = clean(k, p.get(k)); });
  return state;
}

/* Write the state into the URL. Empty values are removed rather than left
 * as `?q=&page=0`, so a clean view has a clean URL. Keys the state does not
 * own (view-specific ones such as `by` or `levels`) are kept as they are. */
export function writeUrl() {
  const p = new URLSearchParams(window.location.search);
  KEYS.forEach((k) => {
    const v = state[k];
    if (v === "" || v === 0 || v === null || v === undefined) p.delete(k);
    else p.set(k, String(v));
  });
  const qs = p.toString();
  const url = window.location.pathname + (qs ? "?" + qs : "") + window.location.hash;
  window.history.replaceState(window.history.state, "", url);
}

export function get(key) {
  return state[key];
}

/* set({q: "Apple", page: 0}) → state + URL + one event. Returns the keys
 * that actually changed, so a caller can skip a fetch when nothing did. */
export function set(patch) {
  const changed = [];
  Object.keys(patch || {}).forEach((k) => {
    if (!KEYS.includes(k)) return;
    const v = clean(k, patch[k]);
    if (state[k] !== v) {
      state[k] = v;
      changed.push(k);
    }
  });
  if (changed.length) {
    writeUrl();
    document.dispatchEvent(new CustomEvent("state:change", { detail: { changed, state: { ...state } } }));
  }
  return changed;
}

/* Extra URL parameters a view owns (`by`, `levels`, `types`…) go through
 * here so they share the replaceState discipline without joining the six. */
export function setExtra(key, value) {
  const p = new URLSearchParams(window.location.search);
  if (value === "" || value === null || value === undefined) p.delete(key);
  else p.set(key, String(value));
  const qs = p.toString();
  window.history.replaceState(window.history.state, "",
    window.location.pathname + (qs ? "?" + qs : "") + window.location.hash);
  document.dispatchEvent(new CustomEvent("state:change", { detail: { changed: [key], state: { ...state } } }));
}

export function getExtra(key) {
  return new URLSearchParams(window.location.search).get(key) || "";
}

export function onChange(fn) {
  const handler = (e) => fn(e.detail.state, e.detail.changed);
  document.addEventListener("state:change", handler);
  return () => document.removeEventListener("state:change", handler);
}

/* The query string for a request: project and language always, the rest
 * when set, plus whatever the caller adds. api.js uses this. */
export function params(extra) {
  const p = new URLSearchParams();
  if (state.project) p.set("project", state.project);
  if (state.language) p.set("language", state.language);
  if (state.q) p.set("q", state.q);
  if (state.timeframe) p.set("timeframe", state.timeframe);
  if (state.page) p.set("page", String(state.page));
  if (extra) {
    const more = extra instanceof URLSearchParams ? extra : new URLSearchParams(extra);
    more.forEach((v, k) => {
      if (v === "" || v === null || v === undefined) p.delete(k);
      else p.set(k, v);
    });
  }
  return p;
}

/* Fill a form field from the state once - the "search input is filled from
 * q once" rule, made explicit. */
export function fillOnce(input, key) {
  if (!input) return;
  const v = state[key];
  if (v !== "" && v !== 0 && input.value !== String(v)) input.value = String(v);
}

read();
window.addEventListener("popstate", () => {
  const before = { ...state };
  read();
  const changed = KEYS.filter((k) => before[k] !== state[k]);
  if (changed.length) {
    document.dispatchEvent(new CustomEvent("state:change", { detail: { changed, state: { ...state } } }));
  }
});
