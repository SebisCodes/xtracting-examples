/* Is anything actually running? Two pills, on the heading line of every view.
 *
 * WHY IT IS ON EVERY VIEW. The question a quiet archive raises - "why has
 * nothing arrived since lunchtime" - has two answers that look identical from
 * every page: the crawler has stopped, or the collector has. Only the
 * Watchlist could tell them apart, and only about the crawler. The collector
 * is the worse of the two to lose, because a collector that has crashed
 * leaves no run row at all: the one state worth seeing was the one state
 * invisible.
 *
 * WHY THE MODULE MOVES THE STRIP INSTEAD OF THE TEMPLATE PLACING IT.
 * _layout.html renders it once, at the top of <main>, and this puts it after
 * the h1 inside `.page-header`. The alternative was the same eleven lines in
 * fourteen templates, and the alternative to THAT - a right-aligned sibling
 * of the heading - is the arrangement app.css records as a mistake at
 * `.page-about`: it moved as the heading changed length, so the one control
 * on every page was never in the same place twice. Inside the header's own
 * wrapping flex line it is at the far right of the title and wraps under it
 * when the two do not fit.
 *
 * WHAT IT WRITES AND WHAT IT DOES NOT. It touches the two pills and nothing
 * else on the page. On the Watchlist, which had a crawler pill before this
 * existed, the crawler half is dropped and only the collector joins that row:
 * a second place to read one state is a second place to keep in step.
 */

import { api, fmtAgo, fmtDate, isAbort } from "./api.js";

/* Every quarter minute the words are redrawn from what is already known, so
 * "just now" becomes "1 min ago" without asking anybody; every half minute the
 * archive is asked again. The reader's window is three minutes wide, so this
 * is fast enough to see a service stop within one screenful of patience and
 * slow enough to be two statements a minute against a two-row table. */
const REDRAW_MS = 15_000;
const REFRESH_MS = 30_000;

let last = null;        // the newest answer, so a redraw needs no request
let lastAsked = 0;      // monotonic-ish; Date.now is fine at this scale

function nameOf(state) {
  const label = state.querySelector("[data-hb-state]");
  return (label && label.dataset.hbName) || "";
}

/* One pill. `row` is null while nothing has been heard from the server. */
function draw(state, row, within) {
  const button = state.querySelector("button");
  const label = state.querySelector("[data-hb-state]");
  const seen = state.querySelector("[data-hb-seen]");
  const detail = state.querySelector("[data-hb-detail]");
  const name = nameOf(state);

  if (!row) {
    button.className = "pill pill--off hint-button";
    label.textContent = name;
    seen.textContent = "- could not ask";
    detail.textContent = "The dashboard could not read the heartbeat table, "
      + "so nothing here is known about this service one way or the other.";
    return;
  }

  button.className = `pill ${row.online ? "pill--ok" : "pill--error"} hint-button`;
  // THE WORD, NOT ONLY THE DOT. Colour is never the only carrier of a state
  // in this product (app.css: the status tokens, and sources.css rule 3).
  label.textContent = `${name} ${row.online ? "running" : "not running"}`;
  seen.textContent = row.seen ? `- ${fmtAgo(row.seen)}` : "- never seen";
  detail.textContent = detailOf(row, name, within);
}

function detailOf(row, name, within) {
  const minutes = Math.max(1, Math.round((within || 180) / 60));
  const parts = [];
  parts.push(row.seen
    ? `Last heard from at ${fmtDate(row.seen)}.`
    : `Nothing has ever been written for this service.`);
  if (row.version) parts.push(`Version ${row.version}.`);
  parts.push(row.online
    ? `${name} writes one of these every minute; ${minutes} minutes of silence `
      + `and this says it is not running.`
    : `Nothing for more than ${minutes} minutes: it has stopped, or it cannot `
      + `reach the archive. What is already archived is unaffected, and the `
      + `pages here all still work.`);
  return parts.join(" ");
}

function redraw(strip) {
  const rows = new Map((last ? last.services : []).map((r) => [r.service, r]));
  strip.querySelectorAll(".service-state").forEach((state) => {
    draw(state, last ? (rows.get(state.dataset.service) || null) : null,
         last ? last.within_seconds : 0);
  });
}

async function refresh(strip) {
  lastAsked = Date.now();
  try {
    last = await api("/api/heartbeats", { context: false, quiet: true });
  } catch (err) {
    if (isAbort(err)) return;
    // Quiet on purpose: this runs on every page, on a timer, and a dashboard
    // that raises a toast every half minute because the archive is being
    // restarted is worse than two pills that say they could not ask.
    last = null;
  }
  redraw(strip);
}

/* Onto the heading line. See the header comment for why this is done here. */
function place(strip) {
  const watchlist = document.querySelector(".crawler-state");
  if (watchlist) {
    // The Watchlist answers for the crawler already, in a pill that also
    // drives the notice under its heading. Only the collector is new there.
    const mine = strip.querySelector('.service-state[data-service="crawler"]');
    if (mine) mine.remove();
    watchlist.insertBefore(strip, watchlist.querySelector("#problems-link"));
  } else {
    const h1 = document.querySelector(".main .page-header h1");
    // No heading is not a case any view has; left where the template put it,
    // the strip is still readable, which is the right failure.
    if (h1) h1.after(strip);
  }
  strip.hidden = false;
}

export function startServiceStatus() {
  const strip = document.getElementById("service-status");
  if (!strip) return;
  // The name is read out of the template before the label is overwritten, so
  // the English word lives in one place - the layout - and not here as well.
  strip.querySelectorAll("[data-hb-state]").forEach((label) => {
    label.dataset.hbName = label.textContent.trim();
  });
  place(strip);
  refresh(strip);
  window.setInterval(() => {
    // A page nobody is looking at asks nothing: the timer keeps the words
    // honest for the moment the tab comes back, and the archive is asked
    // again as soon as it does.
    if (document.hidden) return;
    if (Date.now() - lastAsked >= REFRESH_MS) refresh(strip);
    else redraw(strip);
  }, REDRAW_MS);
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && Date.now() - lastAsked >= REFRESH_MS) refresh(strip);
  });
}

export default startServiceStatus;
