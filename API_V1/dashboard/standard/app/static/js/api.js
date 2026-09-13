/* ==========================================================================
 *  Talking to /api/*.
 *
 *    const data = await api("/api/dashboard/stats", { channel: "stats" });
 *
 *  Three things every call gets for free:
 *
 *    - project and language from the state (state.js), so no view can
 *      forget to bind them - the archive answers nothing sensible without;
 *
 *    - one in-flight request per CHANNEL. A second call on the same channel
 *      aborts the first: typing "Appl" then "Apple" must never show the
 *      answer for "Appl" arriving late. The aborted call rejects with an
 *      error whose `aborted` flag is true, and callers ignore those - they
 *      are not failures, just superseded;
 *
 *    - errors as one readable line. The server answers {error, hint} for
 *      everything (main.py); this shows it as a toast (a11y.js) and rejects
 *      with an ApiError carrying status, error and hint, so a view can also
 *      put the message where it belongs. Pass quiet:true to keep the toast
 *      away, for example while polling.
 *
 *  AND THE FRAGMENTS ARE FINISHED HERE, ONCE. The server writes `error` and
 *  `hint` as lowercase pieces without a full stop, because a toast prints
 *  them on two lines and a heading does not want punctuation. Every view
 *  that ALSO shows the failure in the page - in a panel, beside a button -
 *  has to close them into sentences first, or the two run together into
 *  "the date range ends before it starts swap the two dates". Nine view
 *  modules had grown their own private copy of that four-line function, and
 *  the ones that had not were printing "The date range ends before it
 *  starts." in the panel and "the date range ends before it starts" in the
 *  toast beside it: one piece of news, twice, in two registers.
 *
 *  So `sentence()` lives here, next to the fetch that reads the fragments;
 *  the toast this file raises is already finished, and a view that words the
 *  failure itself imports the same function rather than copying it.
 * ========================================================================== */

import { params as stateParams } from "./state.js";
import { toast } from "./a11y.js";

export class ApiError extends Error {
  constructor(message, opts = {}) {
    super(message);
    this.name = "ApiError";
    this.status = opts.status || 0;
    this.hint = opts.hint || "";
    this.aborted = Boolean(opts.aborted);
    this.url = opts.url || "";
  }
}

export function isAbort(err) {
  return Boolean(err && (err.aborted || err.name === "AbortError"));
}

/* A server fragment, finished into a sentence: capital at the front, full
 * stop at the back, and nothing at all for an empty one (so `[what, hint]
 * .filter(Boolean).join(" ")` does not leave a stray space or a lone dot).
 * Idempotent - a string that is already a sentence comes back unchanged -
 * which is what lets a view pass its OWN wording through it as well. */
export function sentence(text) {
  const clean = String(text == null ? "" : text).trim();
  if (!clean) return "";
  const capital = clean.charAt(0).toUpperCase() + clean.slice(1);
  return /[.!?…]$/.test(capital) ? capital : capital + ".";
}

const controllers = new Map();

/* Abort whatever is in flight on a channel - a view leaving, a dialog closing. */
export function abort(channel) {
  const c = controllers.get(channel);
  if (c) {
    c.abort();
    controllers.delete(channel);
  }
}

function buildUrl(path, opts) {
  const url = new URL(path, window.location.origin);
  // From the state: the pair always (unless context:false), and q /
  // timeframe / page only when the caller asks for them with withState -
  // a request for /api/projects has no business carrying a search term.
  const merged = new URLSearchParams();
  if (opts.context !== false) {
    const own = stateParams();
    // The pair AND the perspective filter: all four say which slice of the
    // archive a request is about, and a call that carried only the pair
    // would answer from a wider set of rows than the page is showing.
    ["project", "language", "perspective", "min_importance"]
      .forEach((k) => { if (own.has(k)) merged.set(k, own.get(k)); });
    if (opts.withState) ["q", "timeframe", "page"].forEach((k) => { if (own.has(k)) merged.set(k, own.get(k)); });
  }
  // The caller's params on top: a caller who passes project deliberately wins.
  if (opts.params) {
    const extra = opts.params instanceof URLSearchParams ? opts.params : new URLSearchParams(opts.params);
    extra.forEach((v, k) => {
      if (v === "" || v === null || v === undefined) merged.delete(k);
      else merged.set(k, v);
    });
  }
  // And anything already written into the path itself wins over both.
  url.searchParams.forEach((v, k) => merged.set(k, v));
  url.search = merged.toString();
  return url.toString();
}

async function readError(res) {
  let error = `${res.status} ${res.statusText || ""}`.trim();
  let hint = "";
  try {
    const body = await res.json();
    if (body && typeof body === "object") {
      if (body.error) error = String(body.error);
      if (body.hint) hint = String(body.hint);
    }
  } catch (_) {
    // Not JSON - a proxy page or an empty body; the status line has to do.
  }
  return { error, hint };
}

/* opts: channel, params, withState, method, body (object → JSON), quiet,
 *       signal, context:false (do not add project/language). */
export async function api(path, opts = {}) {
  const url = buildUrl(path, opts);
  const method = (opts.method || (opts.body !== undefined ? "POST" : "GET")).toUpperCase();

  let controller = null;
  if (opts.channel) {
    abort(opts.channel);
    controller = new AbortController();
    controllers.set(opts.channel, controller);
    if (opts.signal) opts.signal.addEventListener("abort", () => controller.abort(), { once: true });
  }
  const signal = controller ? controller.signal : opts.signal;

  const init = { method, headers: { Accept: "application/json" }, signal, credentials: "same-origin" };
  if (opts.body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = typeof opts.body === "string" ? opts.body : JSON.stringify(opts.body);
  }

  markBusy(opts.channel, true);
  let res;
  try {
    res = await fetch(url, init);
  } catch (err) {
    if (err && err.name === "AbortError") {
      throw new ApiError("request superseded", { aborted: true, url });
    }
    const e = new ApiError("the dashboard could not be reached", {
      hint: "check that the dashboard is running and reload the page", url,
    });
    if (!opts.quiet) toast(sentence(e.message), { kind: "error", hint: sentence(e.hint) });
    throw e;
  } finally {
    if (controller && controllers.get(opts.channel) === controller) controllers.delete(opts.channel);
    // The fetch is what takes the time; reading the body is not, and a page
    // that says "Searching…" while it parses JSON is saying it twice.
    markBusy(opts.channel, false);
  }

  if (!res.ok) {
    const { error, hint } = await readError(res);
    // The ApiError keeps the RAW fragments - a view may want the bare words
    // for a heading - and only what is shown gets finished.
    const e = new ApiError(error, { status: res.status, hint, url });
    if (!opts.quiet) toast(sentence(error), { kind: "error", hint: sentence(hint) });
    throw e;
  }
  if (res.status === 204) return null;
  const type = res.headers.get("Content-Type") || "";
  if (type.includes("json")) return res.json();
  return res.text();
}

/* THE PAGE SAYS IT IS WORKING.
 *
 * A query that takes eight seconds with nothing on screen reads as a broken
 * page - and on a real archive several of them do take that long. So every
 * view that can be searched says so in the same place and the same words:
 * the button goes quiet and busy, and the line beside it reads "Searching…"
 * until the answer is there.
 *
 * The line is `aria-live="polite"`, so it is heard as well as seen, and the
 * button keeps its own text rather than turning into a spinner with no
 * name: a control whose label changes under the pointer is a control the
 * reader has to re-read.
 *
 * Call it in a `finally`, always - a request that fails has stopped too,
 * and a page left saying "Searching…" over an error message is the worse
 * of the two lies.
 */
/* WHICH FORM IS ASKING, so that every view says it is working without
 * every view having to remember to.
 *
 * A view registers its search form against the channel its requests use;
 * api() then turns the busy state on when a request starts on that channel
 * and off when it ends - including when it fails, which is the case a
 * hand-written `finally` in ten different files is most likely to miss.
 *
 * Counted rather than flagged: a second search on the same channel aborts
 * the first, and the aborted one's cleanup must not switch off the state
 * the new one has just switched on. */
const searchForms = new Map();
const inFlight = new Map();

export function watchSearch(form, ...channels) {
  channels.forEach((channel) => { if (form && channel) searchForms.set(channel, form); });
}

function markBusy(channel, on) {
  const form = channel && searchForms.get(channel);
  if (!form) return;
  const n = Math.max(0, (inFlight.get(channel) || 0) + (on ? 1 : -1));
  inFlight.set(channel, n);
  searching(form, n > 0);
}


export function searching(form, on, word = "Searching…") {
  if (!form) return;
  const button = form.querySelector('button[type="submit"]');
  if (button) {
    button.setAttribute("aria-busy", on ? "true" : "false");
    button.disabled = Boolean(on);
  }
  const line = form.querySelector("[data-search-status]");
  // Only OUR word is cleared. A view may have put its own sentence here -
  // "both a place and coordinates" - and finishing a request must not wipe
  // what the reader still needs to read.
  if (line && (on || line.textContent === word)) line.textContent = on ? word : "";
}


/* Small formatting helpers the views share - kept here rather than copied
 * into every page script. */
export function fmtInt(n) {
  if (n === null || n === undefined || n === "") return "-";
  const v = Number(n);
  return Number.isFinite(v) ? v.toLocaleString(undefined, { maximumFractionDigits: 0 }) : String(n);
}

/* "1 link", "12 links" - never "12 link(s)".
 *
 * The bracketed plural is how a program writes a number when nobody has
 * decided what the sentence says, and it reads as machine output - the one
 * register these views spend their whole vocabulary avoiding. `many`
 * defaults to `one` with an "s"; it is an argument so that "one entry / many
 * entries" can be said as well. crawlkit/words.py is the same helper on the
 * Python side, for the sentences the crawler writes. */
export function plural(n, one, many) {
  const value = Number(n) || 0;
  return `${fmtInt(value)} ${value === 1 ? one : (many || one + "s")}`;
}

export function fmtDate(iso, opts = {}) {
  if (!iso) return "-";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return String(iso);
  return opts.time === false
    ? d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" })
    : d.toLocaleString(undefined, { year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

/* "3 hours ago" for the activity list; the exact date sits in the title. */
export function fmtAgo(iso) {
  if (!iso) return "";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "";
  const s = Math.max(0, Math.round((Date.now() - then) / 1000));
  if (s < 60) return "just now";
  const m = Math.round(s / 60);
  if (m < 60) return `${m} min ago`;
  const h = Math.round(m / 60);
  if (h < 48) return `${h} h ago`;
  const d = Math.round(h / 24);
  if (d < 60) return `${d} days ago`;
  const mo = Math.round(d / 30);
  if (mo < 24) return `${mo} months ago`;
  return `${Math.round(mo / 12)} years ago`;
}
