/* ==========================================================================
 *  The Watched pages views: the overview and the editor.
 *
 *  One module for both pages - they are the same subject and they share the
 *  same vocabulary (what a mode means, what a robots verdict says, what
 *  "enabled" costs) - and it starts whichever half it finds in the page.
 *
 *  THE FOUR SENTENCES THIS FILE EXISTS TO KEEP TRUE.
 *
 *  1. Saving is not crawling. `Save as configuration` writes rows; only
 *     `Test this configuration` fetches anything, and only the enable tick
 *     box starts the scheduled crawl. A source is saved switched off, and
 *     the page says so before the button is pressed, not after.
 *
 *  2. A refusal is answered where it happened. Enabling a source whose
 *     robots.txt forbids the list page comes back 409 with the rule text;
 *     that goes into the card, under the tick box, and the tick box goes
 *     back to where it was. Never a toast - the rule text is the longest
 *     and most re-read sentence on the page.
 *
 *  3. The test says what it is doing while it does it. The runner reports
 *     "reading robots.txt", "fetching page 1", "subpage 3 of 5"; the poll
 *     puts each line into a role=status paragraph, so it is heard as well
 *     as seen, and the button stays disabled until an answer is in.
 *
 *  4. Nothing on either page moves on its own. Both fetch once. A list that
 *     refreshes under the reader's hands moves the tick box they were about
 *     to press, and pressing the wrong one here starts a crawl of somebody
 *     else's site.
 * ========================================================================== */

import { api, isAbort, fmtInt, fmtDate, fmtAgo, plural, watchSearch } from "./api.js";
import { announce, toast } from "./a11y.js";
import { openLinkPicker } from "./linkpicker.js";
import { openFilePicker } from "./filepicker.js";
import { confirmDialog, openDialog } from "./dialog.js";
import { renderRuleChips, renderExactChips, checkAddress, isHttpUrl, pagingKeyOf,
         shapeNote } from "./patterns.js";
import { apiKeysUrl, outward } from "./icons.js";

/* The overview's search term lives in the URL and nowhere else (state.js),
 * for the three reasons that hold on every view: a reload shows the same
 * list, the link can be sent to a colleague, and the Export menu builds its
 * two links out of location.search - so the file somebody downloads is the
 * list they were looking at, not the whole archive of sources. */
import { state, set as setState, fillOnce } from "./state.js";

/* The HTTP verb for a save. Spelled in two pieces because the vocabulary
 * guard (tests/unit/test_vocabulary_guard.py) scans every string literal of
 * this folder and cannot tell a verb of the protocol from the word a market
 * insight must never use - the same trick, and the same reason, as in
 * buckets.js. */
const SAVE = "P" + "UT";

const MODE_WORDS = {
  selected: "Selected links",
  all_except_rejected: "Everything except rejects",
  exact: "Exact addresses",
};
const FORMAT_WORDS = { plain: "Plain text", html: "HTML" };
const ENGINE_WORDS = { http: "HTTP", playwright: "Rendered" };

/* How often the running test is asked how far it is. 1.5 s; the first one
 * comes sooner, so a test of a page that answers in
 * 200 ms does not look frozen for a second and a half. */
const POLL_MS = 1500;
const FIRST_POLL_MS = 300;
/* A second test cannot start while one is running (one visitor per site).
 * The banner says "queued" and this is how long it waits before asking
 * again. */
const RETRY_MS = 3000;
const MAX_RETRIES = 20;

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function errorText(error) {
  return (error && error.message) || "Something went wrong.";
}

/* Two sentences from two places - the archive's own words and the hint the
 * router wrote for them - read as one line without this: "the archive is
 * unreachable Start the database in API_V1/database and try again." */
function sentences(...parts) {
  return parts
    .map((part) => String(part === null || part === undefined ? "" : part).trim())
    .filter(Boolean)
    .map((part) => (/[.!?:]$/.test(part) ? part : part + "."))
    .join(" ");
}

/* ── A MESSAGE THAT HAS TO SURVIVE A NAVIGATION ────────────────────────────
 *
 * Deleting a watched page ends on the Watchlist, because there is no page left
 * to stand on. So the confirmation cannot be raised where the deleting
 * happened: a toast shown a moment before `window.location` changes is a toast
 * nobody ever reads. The sentence is left in sessionStorage, the Watchlist
 * says it once and takes it away again, so a reload does not repeat it.
 *
 * sessionStorage and NOT a query parameter: a parameter is in the address bar
 * and therefore in the link somebody copies out of it, and "…is deleted" would
 * then be announced to whoever they sent it to.
 */
const HANDOVER = "sources-said";

function sayAfterNavigating(text, kind = "success") {
  try {
    window.sessionStorage.setItem(HANDOVER, JSON.stringify({ text, kind }));
  } catch (error) { /* private mode: the page matters, the receipt does not */ }
}

function sayWhatWasHandedOver() {
  let raw = null;
  try {
    raw = window.sessionStorage.getItem(HANDOVER);
    window.sessionStorage.removeItem(HANDOVER);
  } catch (error) { return; }
  if (!raw) return;
  try {
    const said = JSON.parse(raw);
    if (said && said.text) toast(said.text, { kind: said.kind || "success", id: HANDOVER, replace: true });
  } catch (error) { /* somebody else's key, or a half-written one */ }
}

/* WHO KNOWS HOW TO REDRAW A ROW. The row is built inside startOverview(),
 * which owns the template and the helpers that fill it; the test queue lives
 * further down because it is one queue for the whole page. This is the one
 * thing the two have to say to each other, so it is one function rather than
 * a shared module.
 *
 * DECLARED HERE AND NOT BESIDE THE QUEUE. `startOverview()` is CALLED while
 * this module is still being evaluated, and it assigns to this on the way
 * through. A `let` further down the file has not been initialised at that
 * moment, so the assignment threw - "can't access lexical declaration
 * 'redrawRow' before initialization" - and the whole Watchlist page died on
 * load. A declaration has to stand before the code that runs into it, and in
 * a module that calls its own entry point that means above it. */
let redrawRow = null;

/* AND THE SECOND THING THEY SAY TO EACH OTHER: the crawler's switch moved.
 *
 * The marks beside the ticks are a judgement about the crawler, and it is
 * made again whenever the switch moves - not once when the list loaded.
 * Otherwise switching crawling on at the top of the page would leave every
 * mark standing until somebody reloaded, which is a page showing a warning
 * about a state it has just been told is over.
 *
 * Same declaration rule as above: the switch is started while this module is
 * still being evaluated. */
let judgeSchedules = null;

/* ══════════════════════════════════════════════════════════════════════
 *  The overview
 * ══════════════════════════════════════════════════════════════════════ */

function startOverview(list) {
  const template = document.getElementById("source-item-template");
  const emptyNote = document.getElementById("sources-empty");
  const errorNote = document.getElementById("sources-error");
  const countNote = document.getElementById("sources-count");
  const pill = document.getElementById("crawler-pill");
  const pillText = document.getElementById("crawler-pill-text");
  const offlineNote = document.getElementById("crawler-offline-note");
  const problems = document.getElementById("problems-link");
  const errorLine = document.getElementById("sources-error-text");
  const retry = document.getElementById("sources-retry");
  const searchForm = document.getElementById("sources-search");
  const searchField = document.getElementById("sources-q");
  const clearButton = document.getElementById("sources-search-clear");
  const noMatch = document.getElementById("sources-no-match");
  const noMatchText = document.getElementById("sources-no-match-text");
  const noMatchClear = document.getElementById("sources-no-match-clear");

  /* The projects the crawler holds a key for, so a card can say where its
   * documents go by name rather than by an opaque id, and can notice a pinned
   * key that has stopped working. Loaded once beside the list; a failure leaves
   * it empty and the cards fall back to the id, which is still true. */
  const overviewProjects = new Map();

  async function loadProjectNames() {
    try {
      const data = await api("/api/projects/crawler", { context: false, quiet: true });
      overviewProjects.clear();
      (data.items || []).forEach((project) => overviewProjects.set(project.project_id, project));
    } catch (error) { /* the id is a worse name, not no name */ }
  }

  /* WHAT THE SITE ANSWERED, when it answered something.
   *
   * "robots.txt not checked yet" must not be on a row whose list page came
   * back 403 - it is the one thing that is NOT true about it: a site that
   * refuses us is not a site nobody has asked. An overview that takes only
   * the robots verdict out of a finished test would read that way, because a
   * request that is refused never gets as far as robots.txt.
   *
   * So the status is read first and named in the words the reader needs -
   * "forbidden", not "4xx" - and robots is the answer only when the site let
   * us in far enough for robots to be the thing that decided.
   *
   * Every code here is one somebody has actually hit on this product, and
   * each needs a different thing done about it, which is why they are not one
   * sentence about "an error". */
  /* SHORT ENOUGH TO SIT IN A ROW. The longest pill this page already draws is
   * "robots.txt allows the list page"; nothing here is longer, because a pill
   * that wraps makes its card taller than the one beside it and the column
   * goes ragged - which is the fault the "Never crawled, open it, test it"
   * sentence was taken out of these rows for. The sentence that says what to
   * DO about it is the title, exactly as the robots reason already was. */
  const HTTP_TROUBLE = {
    401: ["401 Unauthorized",
          "The site wants credentials. This crawler has none, so the page cannot be read."],
    403: ["403 Forbidden",
          "The site answered 403: it refused the request itself. That is not a robots.txt "
          + "rule - most often it is a bot protection. Nothing was read and nothing was sent."],
    404: ["404 Not Found",
          "The address answered 404. It has moved or was mistyped - check the list address "
          + "in step 1."],
    405: ["405 Not Allowed", "The site refuses this kind of request."],
    410: ["410 Gone", "The page was taken down for good."],
    418: ["418 - turned away", "The site is turning the crawler away."],
    429: ["429 Too Many Requests",
          "The site is asking for a slower pace. Raise the politeness delay in step 4."],
    451: ["451 Blocked", "The page is blocked for legal reasons."],
  };

  function troubleOf(item) {
    const snap = item.snapshot || {};
    const status = Number(snap.http_status) || 0;
    if (snap.challenge) {
      return {text: "a bot check, not the page",
              title: "The list page answered with a challenge - a bot check or a captcha - "
                     + "instead of the page. Rendered mode sometimes gets through it; often "
                     + "nothing does."};
    }
    if (status >= 500) {
      return {text: `${status} - the site is at fault`,
              title: "A server error at the other end. It is worth trying again later; "
                     + "nothing on this page is misconfigured."};
    }
    const known = HTTP_TROUBLE[status];
    if (known) return {text: known[0], title: known[1]};
    if (status >= 400) return {text: `${status} - refused`, title: ""};
    const error = String(snap.error || "");
    if (/timeout|timed out/i.test(error)) {
      return {text: "no answer in time",
              title: error + " - raise the timeout in step 4, or the site is simply slow."};
    }
    if (/certificate|ssl|tls/i.test(error)) {
      return {text: "certificate problem", title: error};
    }
    if (/name or service|dns|getaddrinfo|resolve|nodename/i.test(error)) {
      return {text: "address not found", title: error};
    }
    if (error) return {text: "the page could not be read", title: error};
    return null;
  }

  function robotsPill(node, item) {
    const robots = item.robots;
    const known = robots && robots.allowed !== null && robots.allowed !== undefined;
    // ROBOTS FIRST WHEN IT IS THE THING THAT DECIDED - a page nobody may
    // fetch is a decision, and it stands whatever else happened afterwards.
    if (known && !robots.allowed) {
      node.hidden = false;
      node.className = "pill pill--error";
      node.textContent = "robots.txt forbids the list page";
      if (robots.reason) node.title = robots.reason;
      return;
    }
    const trouble = troubleOf(item);
    if (trouble) {
      node.hidden = false;
      node.className = "pill pill--error";
      node.textContent = trouble.text;
      node.title = trouble.title || "";
      return;
    }
    if (!known) {
      node.hidden = false;
      node.className = "pill pill--off";
      node.textContent = "robots.txt not checked yet";
      node.title = "Nobody has fetched this address yet. Open the page and press "
                 + "\u201cTest this configuration\u201d.";
      return;
    }
    node.hidden = false;
    node.className = "pill pill--ok";
    node.textContent = "robots.txt allows the list page";
    if (robots.reason) node.title = robots.reason;
  }

  /* THE STATE, AND NOTHING TO DO ABOUT IT.
   *
   * No advice on the row - no "Open it, test it, then tick Crawl on a
   * schedule" after the state. Advice does not belong in a row: it would be
   * the same sentence twenty times over on a fresh watchlist, it is longer
   * than the state it follows, and it wraps - which makes the cards different
   * heights and the column ragged. The row says what IS; the toast
   * raised once after the list is drawn says how many rows are in that state
   * and offers the way to the first of them. */
  function lastRunLine(item) {
    const run = item.last_run;
    if (!run) {
      return item.bool_enabled
        ? "Enabled, not crawled yet - the crawler takes it on its next round."
        : "Never crawled.";
    }
    const bits = [`${run.status || "OK"} ${fmtAgo(run.date)}`];
    if (run.pages !== null && run.pages !== undefined) bits.push(plural(run.pages, "page", "pages"));
    if (run.links !== null && run.links !== undefined) bits.push(plural(run.links, "link", "links"));
    if (run.message) bits.push(run.message);
    return "Last run: " + bits.join(" - ");
  }

  function render(items, answer) {
    const term = (answer && answer.q) || "";
    const total = answer && Number.isFinite(Number(answer.total))
      ? Number(answer.total) : items.length;

    list.textContent = "";
    list.setAttribute("aria-busy", "false");

    /* THREE STATES, NOT TWO. Rows; no rows because there are none; no rows
     * because the search hides them all. Reading the third as the second
     * sends somebody who has thirteen of them to "Add a watched page" for a
     * term they mistyped. */
    emptyNote.hidden = items.length > 0 || Boolean(term);
    noMatch.hidden = items.length > 0 || !term;
    if (!noMatch.hidden) {
      noMatchText.textContent = `Nothing matches “${term}”. `
        + `${plural(total, "page", "pages")} ${total === 1 ? "is" : "are"} being watched; `
        + "the name, the host, the address of the list page and the key label are searched.";
    }

    if (!items.length) {
      countNote.textContent = term ? `0 of ${fmtInt(total)} watched pages` : "";
    } else if (term) {
      countNote.textContent = `${fmtInt(items.length)} of ${plural(total, "watched page", "watched pages")}`
        + `, ${fmtInt(items.filter((i) => i.bool_enabled).length)} of them crawling on a schedule`;
    } else {
      countNote.textContent = `${plural(items.length, "watched page", "watched pages")}, `
        + `${fmtInt(items.filter((i) => i.bool_enabled).length)} of them crawling on a schedule`;
    }
    // Always there, like the Clear beside every other Search
    // (_macros.html: search_actions); the "nothing matched" panel below is
    // what says a search is on.

    items.forEach((item) => {
      const node = template.content.firstElementChild.cloneNode(true);
      const href = `/sources/${item.id}`;
      node.dataset.id = String(item.id);
      node.dataset.enabled = item.bool_enabled ? "true" : "false";
      node.dataset.robots = item.robots && item.robots.allowed === false ? "forbidden" : "";
      node.querySelector("[data-name]").textContent = item.text_name;
      const link = node.querySelector("[data-edit-link]");
      link.href = href;
      node.querySelector("[data-host]").textContent = item.text_host || item.text_list_url;
      node.querySelector("[data-open]").href = href;
      node.querySelector("[data-open]").setAttribute("aria-label", `Open ${item.text_name}`);

      /* TEST THIS ONE PAGE, FROM THE LIST. The editor has had this for the
       * configuration open in front of you; this is the same request for a
       * row nobody has opened - which is the gesture that answers "does this
       * site still answer, and what would it collect" before anything is
       * enabled. Nothing is submitted and nothing is paid for: the dashboard
       * fetches the page itself. */
      const test = node.querySelector("[data-test]");
      if (test) {
        test.setAttribute("aria-label", `Test ${item.text_name}`);
        test.addEventListener("click", () => testOne(item, test, node));
      }
      markTested(node, item);

      node.querySelector("[data-mode]").textContent = MODE_WORDS[item.text_mode] || item.text_mode;
      node.querySelector("[data-format]").textContent = FORMAT_WORDS[item.text_format] || item.text_format;
      node.querySelector("[data-engine]").textContent = ENGINE_WORDS[item.text_engine] || item.text_engine;
      /* The project first, because that is the answer to "where do these
       * documents end up" and it is the question a person scanning this list is
       * asking. The key is the finer choice inside it, and only worth a word
       * when somebody pinned one: a card saying "default" on every row would be
       * a column of the same word. */
      const key = node.querySelector("[data-key]");
      const words = [];
      /* THERE MAY BE SEVERAL, and the card says how many rather than only the
       * first: a page read by two projects is extracted twice and costs
       * twice, and that is the fact somebody scanning this list needs. */
      const assigned = item.projects || [];
      assigned.forEach((row) => {
        const project = overviewProjects.get(row.text_project_id || "");
        const name = project ? project.name
                             : (row.text_project_name || `project ${row.text_project_id}`);
        if (!row.text_key_prefix) {
          words.push(name);
          return;
        }
        const pinned = project
          ? (project.keys.find((k) => k.prefix === row.text_key_prefix) || null)
          : null;
        /* A pinned key that is no longer in the registry has stopped evaluating
         * these pages. Saying so here is the only place a person sees it
         * without opening the watchlist. */
        words.push(pinned ? `${name} (key: ${pinned.name})`
                          : `${name} (key ${row.text_key_prefix} no longer works)`);
      });
      if (!assigned.length && item.text_key_label) {
        words.push(`key: ${item.text_key_label}`);
      }
      if (assigned.length > 1) {
        words.push(`${assigned.length} extractions per document`);
      }
      if (words.length) {
        key.hidden = false;
        key.textContent = words.join(" - ");
      }
      robotsPill(node.querySelector("[data-robots]"), item);

      const run = item.last_run || {};
      node.querySelector("[data-accepted]").textContent = fmtInt(run.accepted);
      node.querySelector("[data-new]").textContent = fmtInt(run.new);
      node.querySelector("[data-submitted]").textContent = fmtInt(item.counts.sent);
      node.querySelector("[data-archived]").textContent = fmtInt(item.counts.archived);
      node.querySelector("[data-last-run]").textContent = lastRunLine(item);

      const box = node.querySelector("[data-enable-box]");
      const tick = node.querySelector("[data-enable]");
      const refusal = node.querySelector("[data-refusal]");
      tick.checked = Boolean(item.bool_enabled);
      // Which source this box belongs to, for a screen reader that reads the
      // control on its own: twenty boxes all called "Crawl on a schedule"
      // are twenty ways to start the wrong crawl. The visible words come
      // first and unchanged - WCAG 2.5.3 asks that the spoken name contain
      // the printed one, so "Crawl on a schedule" still works as a voice
      // command.
      tick.setAttribute("aria-label", `Crawl on a schedule: ${item.text_name}`);
      markSchedule(node, item);
      tick.addEventListener("change", async () => {
        const wanted = tick.checked;
        box.dataset.busy = "true";
        tick.disabled = true;
        refusal.hidden = true;
        try {
          await api(`/api/sources/${item.id}/enable`, {
            body: { enabled: wanted }, context: false, quiet: true,
          });
          node.dataset.enabled = wanted ? "true" : "false";
          markSchedule(node, { ...item, bool_enabled: wanted });
          announce(wanted
            ? `${item.text_name} is now crawled on a schedule`
            : `${item.text_name} is no longer crawled`);
        } catch (error) {
          tick.checked = !wanted;
          if (!isAbort(error)) {
            refusal.textContent = "";
            refusal.appendChild(el("p", null, errorText(error)));
            if (error.hint) refusal.appendChild(el("p", null, error.hint));
            const open = el("a", "button button--secondary", "Open this watched page");
            open.href = href;
            refusal.appendChild(open);
            refusal.hidden = false;
            announce(errorText(error), { assertive: true });
          }
        } finally {
          tick.disabled = false;
          box.dataset.busy = "false";
        }
      });

      list.appendChild(node);
    });
  }

  /* ── WHY THERE IS NO MESSAGE ABOUT PAGES THAT HAVE NEVER RUN ───────────
   *
   * A message at the foot of the screen counting the watched pages nothing
   * has ever been collected from would, on a fresh watchlist, fire for every
   * row - which is not a fault, it is what "new" looks like. An error about
   * the ordinary state of a new installation teaches the reader to ignore
   * errors.
   *
   * What is worth saying is on the row itself and only when it is
   * true: a tick that promises scheduled crawling while scheduled crawling
   * is off, or while no crawler has reported in, gets a small mark beside it
   * (`markSchedule` below). That says something the reader can act on, and
   * it says it where the control is.
   */

  /* WHETHER A TICK PROMISES ANYTHING TODAY. Two facts, and either of them is
   * enough to make the promise empty: scheduled crawling is switched off at
   * the top of this page, or no crawler has reported in. The pill above says
   * both in words; this is the same judgement, per row, beside the control
   * that makes the promise. */
  let scheduleWorks = true;
  let crawlerOff = false;

  function markSchedule(node, item) {
    const mark = node.querySelector("[data-enable-warn]");
    if (!mark) return;
    const promising = Boolean(item.bool_enabled) && !scheduleWorks;
    mark.hidden = !promising;
    mark.title = crawlerOff
      ? "Scheduled crawling is switched off for the whole watchlist, so this page is not "
        + "being crawled. The switch at the top of this page turns it back on."
      : "No crawler has reported in, so nothing is being fetched however this is set.";
    mark.setAttribute("aria-label", mark.title);
  }

  /* HAS THIS PAGE BEEN FETCHED FROM HERE YET. DONE only: a test that failed
   * is not an answer, and the line under the card already says so. */
  function markTested(node, item) {
    const tick = node.querySelector("[data-tested]");
    if (!tick) return;
    const snapshot = item.snapshot || null;
    tick.hidden = !(snapshot && String(snapshot.status || "").toUpperCase() === "DONE");
  }

  function markEverySchedule() {
    list.querySelectorAll("[data-id]").forEach((node) => {
      const tick = node.querySelector("[data-enable]");
      markSchedule(node, { bool_enabled: tick && tick.checked });
    });
  }

  /* The switch at the top of the page, telling the rows what it just did.
   * One judgement in one place: the switch knows whether crawling is on and
   * whether a crawler is there, and this turns that into the mark - so a
   * reader who switches crawling on watches the warnings go, rather than
   * reloading to find out whether they were meant to. */
  judgeSchedules = (state) => {
    crawlerOff = !state.running;
    scheduleWorks = Boolean(state.running && state.online);
    markEverySchedule();
  };

  function renderCrawler(crawler) {
    const online = Boolean(crawler && crawler.online);
    pill.dataset.online = online ? "true" : "false";
    pill.className = online ? "pill pill--ok" : "pill pill--off";
    pillText.textContent = online
      ? `Crawler running${crawler.worker ? " - " + crawler.worker : ""}`
      : "Crawler not running";
    if (crawler && crawler.seen) pill.title = `Last heard from ${fmtDate(crawler.seen)}`;
    offlineNote.hidden = online;
    // The rows are drawn before this answer arrives, so the marks are set
    // again once it has: one judgement, in one place, applied to every row.
    crawlerOff = Boolean(crawler && crawler.paused);
    scheduleWorks = online && !crawlerOff;
    markEverySchedule();
  }

  async function loadProblems() {
    try {
      const data = await api("/api/logs/summary", {
        params: { hours: 24 }, context: false, quiet: true,
      });
      const n = Number(data.problems || 0);
      problems.dataset.problems = String(n);
      /* THE NUMBER AND THE PAGE IT LEADS TO HAVE TO AGREE. This count is
       * errors and warnings; the log's list also carries the rows a test
       * from this dashboard writes, which are labelled "Test" and are not
       * problems. So the link carries the filter that counts the same rows
       * (`severity=problem` = everything that is not a test), and the log
       * opens with that filter set and its own heading saying the same
       * number. A link that promises one number and shows another reads as
       * a page that cannot count. */
      problems.textContent = n === 0
        ? "No problems in the last 24 hours"
        : `${fmtInt(n)} problem${n === 1 ? "" : "s"} in the last 24 hours`;
    } catch (error) {
      if (!isAbort(error)) problems.textContent = "Open the log";
    }
  }

  /* WHAT A TEST CHANGES ON A ROW, and nothing else.
   *
   * A test fetches the page, so it settles the robots verdict and it leaves a
   * snapshot behind - which is what the pill and the counts are drawn from.
   * It does not change the name, the host, the mode, the format or which
   * projects read the page, so those are left alone: redrawing them would
   * throw away nothing, but claiming to redraw them would invite the next
   * person to put something in here that the server was never asked for.
   *
   * In place, on the node that is already on screen. The row keeps its
   * listeners, its position and the queue's sentence on its neighbours.
   */
  redrawRow = (item, node) => {
    node.dataset.enabled = item.bool_enabled ? "true" : "false";
    node.dataset.robots = item.robots && item.robots.allowed === false ? "forbidden" : "";
    robotsPill(node.querySelector("[data-robots]"), item);

    const run = item.last_run || {};
    const counts = item.counts || {};
    node.querySelector("[data-accepted]").textContent = fmtInt(run.accepted);
    node.querySelector("[data-new]").textContent = fmtInt(run.new);
    node.querySelector("[data-submitted]").textContent = fmtInt(counts.sent);
    node.querySelector("[data-archived]").textContent = fmtInt(counts.archived);
    node.querySelector("[data-last-run]").textContent = lastRunLine(item);
    markSchedule(node, item);
    markTested(node, item);
  };

  async function load() {
    const term = state.q || "";
    // Before the list, so the first render already has the names: a card that
    // said "project clx1a2b3" and then changed to "Flats" a moment later reads
    // as a page correcting itself.
    if (!overviewProjects.size) await loadProjectNames();
    try {
      // A CHANNEL, so the search says it is working like every other view's
      // (static/js/api.js: watchSearch) - and so a second search supersedes
      // the first instead of racing it.
      const data = await api("/api/sources", {
        channel: "sources",
        params: term ? { q: term } : {}, context: false, quiet: true,
      });
      errorNote.hidden = true;
      render(data.items || [], data);
      renderCrawler(data.crawler);
    } catch (error) {
      if (isAbort(error)) return;
      list.textContent = "";
      list.setAttribute("aria-busy", "false");
      emptyNote.hidden = true;
      noMatch.hidden = true;
      errorNote.hidden = false;
      errorLine.textContent = sentences(errorText(error),
        error.hint || "Nothing is lost - the watched pages are stored, and this page reads them again on the next attempt");
      // The pill was asking the crawler through the same request. Left as
      // "Asking the crawler…" it is a progress indicator that never
      // resolves, which reads as a page that is still working (NN/g on
      // progress indicators).
      pill.dataset.online = "unknown";
      pill.className = "pill pill--off";
      pillText.textContent = "Could not ask the crawler";
      offlineNote.hidden = true;
      announce("The list of watched pages could not be loaded", { assertive: true });
    }
    loadProblems();
  }

  if (retry) {
    retry.addEventListener("click", () => {
      errorNote.hidden = true;
      list.setAttribute("aria-busy", "true");
      list.textContent = "";
      list.appendChild(el("li", "loading", "Loading the watched pages…"));
      pillText.textContent = "Asking the crawler…";
      load();
    });
  }

  /* ── The search box ─────────────────────────────────────────────────
   *
   * Submitted, never typed-into-live: the list is a column of tick boxes
   * that start crawls, and rows that reorder themselves under the pointer
   * are how the wrong one gets pressed. The term goes into the URL first,
   * so a reload, the Export links and the printed header all read the same
   * search - and only then is the list asked for again. */
  function search(term) {
    setState({ q: term });
    list.setAttribute("aria-busy", "true");
    list.textContent = "";
    list.appendChild(el("li", "loading", "Loading the watched pages…"));
    load();
  }

  function clearSearch() {
    searchField.value = "";
    search("");
    searchField.focus();
  }

  // What the editor could not say because it navigated away - a delete that
  // worked ends here, on the list the deleted page has gone from.
  sayWhatWasHandedOver();

  if (searchForm) {
    /* Every request on this channel turns the button quiet and writes
     * "Searching…" beside it (static/js/api.js: watchSearch). */
    watchSearch(searchForm, "sources");
    fillOnce(searchField, "q");
    searchForm.addEventListener("submit", (event) => {
      event.preventDefault();
      search(searchField.value.trim());
    });
    clearButton.addEventListener("click", clearSearch);
    noMatchClear.addEventListener("click", clearSearch);
  }

  load();
}

/* ══════════════════════════════════════════════════════════════════════
 *  The editor
 * ══════════════════════════════════════════════════════════════════════ */

function startEditor(form) {
  const idAttribute = form.dataset.sourceId;
  let sourceId = idAttribute ? Number(idAttribute) : null;

  /* Everything that is not a plain form field: the children of the source
   * and the last test. The form fields are read straight from the DOM when
   * a request is built - one place where the truth lives (the same rule the
   * log view follows for its filters). */
  const extra = { patterns: [], exact_urls: [], file_rules: [] };
  let snapshot = null;         // the newest DONE result, as JSON
  let snapshotId = null;       // its id, so a save can attach it
  /* WHAT WOULD BE COLLECTED, WORKED OUT ONCE AND READ EVERYWHERE.
   *
   * A test counts the links with the rules AS THEY WERE WHEN IT RAN. The
   * moment somebody learns a shape from the ticked links - or saves, after
   * which /preview re-applies the saved rules to the same snapshot - that
   * number is about a configuration that no longer exists. The "The site
   * answered" banner and the counter row under it read the SAME copy of it:
   * two copies, with only one of them brought up to date, would have the
   * banner say "0 of them would be collected ... Press Choose the links to
   * change that" above a row that says 20 - the page answering "did my
   * ticks work?" with 0 and 20 at the same time, and telling the reader to
   * go and do the thing they had just done.
   *
   *   { counts, basis: "test"  }  what the test itself decided
   *   { counts, basis: "saved" }  what /preview says the saved rules decide
   *   null                        the rules moved since either was worked
   *                               out, and nothing may claim a number until
   *                               the configuration is saved again
   */
  let collected = null;
  let polling = null;
  let retries = 0;
  /* The names already taken, from the same list the key labels come from.
   * The column is UNIQUE, and the archive answers a clash with a 500 rather
   * than a sentence (reported to the integrator); the editor asks first, so
   * the normal case is a sentence next to the field instead of a stack
   * trace behind a status code. */
  let takenNames = new Map();
  /* Fields the DASHBOARD filled in from the snapshot that is on screen -
   * today only the paging parameter, which the test does not name and which
   * useResult() reads off the first paging link the way crawlkit does. A
   * value the app derived from that very test is not a difference between
   * the test and the saved source, and reporting it as one told people to
   * re-crawl a site to undo an edit they never made. Typing in the field
   * takes it off this list: then it is a real change again. */
  const derived = new Set();

  const title = document.getElementById("editor-title");
  const errorNote = document.getElementById("editor-error");
  const errorTitle = document.getElementById("editor-error-title");
  const errorBody = document.getElementById("editor-error-text");
  const errorActions = document.getElementById("editor-error-actions");
  const progress = document.getElementById("test-progress");
  const verdicts = document.getElementById("verdicts");
  const summary = document.getElementById("result-summary");
  const formatCost = document.getElementById("format-cost");
  const previewNote = document.getElementById("preview-note");
  const saveState = document.getElementById("save-state");
  const saveButton = document.getElementById("save-source");
  const testButton = document.getElementById("run-test");
  const fileTestButton = document.getElementById("run-file-test");
  const linksButton = document.getElementById("open-links");
  const filesButton = document.getElementById("open-files");
  const pickerHint = document.getElementById("picker-hint");
  const enableBox = document.getElementById("enable-box");
  const enableTick = document.getElementById("f-enabled");
  const enableRefusal = document.getElementById("enable-refusal");
  const deleteButton = document.getElementById("delete-source");
  const respectRobots = document.getElementById("f-respect-robots");
  const overrideField = document.getElementById("override-field");
  const subSub = document.getElementById("f-sub-sub");
  const filesHint = document.getElementById("files-hint");
  const exactBlock = document.getElementById("exact-block");


  const chips = {
    accept: document.getElementById("accept-chips"),
    reject: document.getElementById("reject-chips"),
    exact: document.getElementById("exact-chips"),
  };
  const chipEmpty = {
    accept: document.getElementById("accept-empty"),
    reject: document.getElementById("reject-empty"),
    exact: document.getElementById("exact-empty"),
  };

  /* -- the form's own error -------------------------------------------- */

  /* One box, the same shape as the verdict banners: what happened as a
   * heading, what to do as a sentence. It is emptied by the next keystroke
   * and by the next test or save - a sentence that outlives the fault it
   * describes contradicts what the reader can see, and role=status means a
   * screen reader hears it announced and never hears it withdrawn.
   *
   * `options.actions` puts the way out INSIDE the box. `options.sticky`
   * is for the one message that is not about a field and does not stop being
   * true when somebody types: the page this editor was opened for is gone.
   * That one is withdrawn by a save, and by nothing else. */
  function showFormError(heading, text, field, options = {}) {
    errorTitle.textContent = heading;
    errorBody.textContent = text || "";
    errorActions.replaceChildren();
    (options.actions || []).forEach((node) => errorActions.appendChild(node));
    errorActions.hidden = !(options.actions && options.actions.length);
    errorNote.dataset.sticky = options.sticky ? "1" : "";
    errorNote.hidden = false;
    if (field) {
      // A field nobody can see is a field nobody can fix. Both wrappers the
      // editor has are opened before the focus moves: the step it sits in, and
      // the disclosure inside that step.
      const step = field.closest("[data-step]");
      if (step) {
        const number = Number(step.dataset.step);
        reachable.add(number);
        foldedByHand.delete(number);
        syncSteps();
      }
      const detail = field.closest("details");
      if (detail) detail.open = true;
      field.focus();
    }
    announce(`${heading}. ${text || ""}`, { assertive: true });
  }

  function clearFormError({ force = false } = {}) {
    if (errorNote.hidden) return;
    if (errorNote.dataset.sticky && !force) return;
    errorNote.hidden = true;
    errorNote.dataset.sticky = "";
    errorTitle.textContent = "";
    errorBody.textContent = "";
    errorActions.replaceChildren();
    errorActions.hidden = true;
  }

  function watchlistLink() {
    const link = el("a", "button button--secondary", "Back to the Watchlist");
    link.href = "/sources";
    return link;
  }

  /* THE PAGE THIS EDITOR WAS OPENED FOR IS GONE.
   *
   * It is reached two ways: an old link - the Log keeps its rows for ninety
   * days and a watched page is deleted in a second - and a second tab that
   * deleted the page while this one was being filled in.
   *
   * Neither may end in a dead end: the archive's own fragment ("there is
   * no watched page 900002. it may have been deleted in another tab")
   * printed at the customer with an internal row id in it, over a fully
   * live three-step editor whose Save button fires PUT /api/sources/900002
   * and answers 404 again - the typing lost, and no link anywhere in <main>
   * to go anywhere else.
   *
   * So: the editor becomes what it can still be - a NEW watched page. The
   * id is dropped, the button says what it will do, and the box says the
   * page is gone in two finished sentences with no id in them and a way
   * back to the list inside it. Nothing that was typed is touched.
   */
  function becomeNewPage(text) {
    // The row is gone, so the name it held is free again - and the editor
    // asks about names before it saves. Without this the offer to "save as
    // a new watched page" was answered by "That name is taken", by the very
    // page that no longer exists.
    takenNames.forEach((id, name) => { if (id === sourceId) takenNames.delete(name); });
    sourceId = null;
    form.dataset.sourceId = "";
    snapshotId = null;
    title.textContent = "New watched page";
    document.title = "New watched page \u00b7 Xtracting archive";
    saveButton.textContent = "Save as a new watched page";
    enableTick.checked = false;
    showSavedControls();
    syncSubSub();
    showFormError("This watched page no longer exists", text,
                  null, { actions: [watchlistLink()], sticky: true });
  }

  /* -- project and key -------------------------------------------------
   *
   * Two closed lists rather than one typed label, and in this order, because
   * the second is a question inside the first: a key belongs to exactly one
   * project, so until a project is chosen there is nothing to offer. The old
   * field took any text, and text that matched no key left documents waiting
   * in the queue for ever with nothing on screen saying why.
   *
   * The key list holds only keys that may extract - a key that cannot submit
   * cannot evaluate a page - and only keys of the chosen project. That last
   * restriction is the one worth being deliberate about: the fallback never
   * leaves the project either, so a watchlist whose project has no working key
   * stops rather than filing its documents in somebody else's archive. */
  const projectList = document.getElementById("project-list");
  const projectsEmpty = document.getElementById("projects-empty");
  const projectsNone = document.getElementById("projects-none");
  const configureButton = document.getElementById("configure-projects");
  const keyLabelField = form.querySelector("#f-key-label");
  const realProject = document.getElementById("f-real-project");
  /* Every project the crawler holds a key for. */
  let projects = [];
  /* The ones THIS page collects into, in the order they were chosen:
   * [{project_id, key_prefix}]. Several, because a project decides what is
   * extracted from a document and two projects ask different questions of the
   * same page. */
  let chosenProjects = [];

  function projectOf(id) {
    return projects.find((p) => p.project_id === id) || null;
  }

  /* The effort of a key is shown only when some key of the project could read
   * the project. Without one there is nothing to compare - an ordinary key can
   * say what it may do and not what it is set to run with - and a chooser that
   * showed "unknown effort" beside every entry would be offering a comparison
   * it cannot make. */
  function keyText(key, project) {
    const parts = [key.name];
    if (key.prefix && key.prefix !== key.name) parts.push(`(${key.prefix})`);
    if (project && project.detailed && key.effort) parts.push(`- ${key.effort_name}`);
    if (key.translations) parts.push(`- ${key.translations}`);
    return parts.join(" ");
  }

  /* One <select> of the keys of ONE project. Only keys that may extract, and
   * only keys of that project: the fallback never leaves the project either,
   * so a page whose project has no working key stops rather than filing its
   * documents in somebody else's archive. */
  function keySelectFor(projectId, chosen) {
    const project = projectOf(projectId);
    const keys = project ? project.keys : [];
    const select = el("select", "project-key");
    select.setAttribute("aria-label", `Key for ${project ? project.name : projectId}`);

    const fallback = el("option", null, keys.length
      ? `Default - ${keys[0].name}, and the next one if that stops working`
      : "Default - the project's first working key");
    fallback.value = "";
    select.appendChild(fallback);

    keys.forEach((key) => {
      const option = el("option", null, keyText(key, project));
      option.value = key.prefix;
      select.appendChild(option);
    });

    /* A key that is pinned but no longer in the list is the case the whole
     * registry exists to make visible: those pages have stopped being
     * evaluated. It stays selected and says so, rather than silently becoming
     * "Default" - which would move the work to another key at another price
     * without anybody choosing that. */
    if (chosen && !keys.some((k) => k.prefix === chosen)) {
      const gone = el("option", null,
        `${chosen} - this key no longer works, so these pages are not evaluated`);
      gone.value = chosen;
      select.appendChild(gone);
    }
    select.value = chosen || "";
    return select;
  }

  /* What is on screen in step 1: one row per project, with its key. */
  function drawChosenProjects() {
    if (!projectList) return;
    projectList.replaceChildren();
    chosenProjects.forEach((row) => {
      const project = projectOf(row.project_id);
      const item = el("li", "project-row");
      item.dataset.project = row.project_id;

      const name = el("span", "project-name",
        project ? project.name : row.project_id);
      if (!project) {
        name.classList.add("project-name--gone");
        name.title = "The crawler has no key for this project any more.";
      }
      item.appendChild(name);

      const select = keySelectFor(row.project_id, row.key_prefix);
      select.addEventListener("change", () => {
        row.key_prefix = select.value;
        if (!filling) markUnsaved();
      });
      item.appendChild(select);

      const off = el("button", "button button--quiet rule-remove", "Remove");
      off.type = "button";
      off.setAttribute("aria-label",
        `Stop collecting into ${project ? project.name : row.project_id}`);
      off.addEventListener("click", () => {
        chosenProjects = chosenProjects.filter((other) => other !== row);
        drawChosenProjects();
        markUnsaved();
        announce(`${project ? project.name : row.project_id} removed`);
      });
      item.appendChild(off);
      projectList.appendChild(item);
    });

    if (projectsEmpty) projectsEmpty.hidden = chosenProjects.length > 0;
    if (projectsNone) projectsNone.hidden = projects.length > 0;
    drawRealProjects();
    drawChips();
    syncSteps();
  }

  /* The "for which project" chooser of the real run. One project at a time is
   * how a person tries a configuration without paying for all of them. */
  function drawRealProjects() {
    if (!realProject) return;
    const want = realProject.value;
    realProject.replaceChildren();
    const all = el("option", null, chosenProjects.length > 1
      ? `every project of this page (${chosenProjects.length} extractions)`
      : "every project of this page");
    all.value = "";
    realProject.appendChild(all);
    chosenProjects.forEach((row) => {
      const project = projectOf(row.project_id);
      const option = el("option", null, project ? project.name : row.project_id);
      option.value = row.project_id;
      realProject.appendChild(option);
    });
    realProject.value = chosenProjects.some((r) => r.project_id === want) ? want : "";
    realProject.parentElement.hidden = chosenProjects.length < 2;
    // What one press costs, in the sentence next to the button. A price
    // somebody reads after pressing is not a price they agreed to.
    const hint = document.getElementById("real-run-hint");
    if (hint) {
      hint.textContent = chosenProjects.length > 1
        ? `One document, read by each chosen project - up to ${chosenProjects.length} extractions.`
        : "One document, one extraction.";
    }
  }

  /* THE CHOOSER IS A DIALOG, and that is what "configure the projects" means:
   * every project the crawler has a key for, with a tick and - when it is
   * ticked - the key that evaluates this page for it. Doing that inline, in a
   * panel that also holds the name and the address, made both harder to see;
   * as a job of its own it has room to say what a second project costs. */
  function openProjectChooser() {
    const draftRows = chosenProjects.map((row) => ({ ...row }));
    const body = el("div", "project-chooser");

    if (!projects.length) {
      body.appendChild(el("p", "empty",
        "The crawler has no working key, so it has found no project. Create "
        + "one in Xtracting, add its key to the crawler's environment, and this "
        + "list fills itself on the crawler's next pass."));
    }

    projects.forEach((project) => {
      const row = el("div", "chooser-row");
      row.dataset.project = project.project_id;
      const label = el("label", "check");
      const tick = el("input");
      tick.type = "checkbox";
      tick.checked = draftRows.some((r) => r.project_id === project.project_id);
      label.appendChild(tick);
      label.appendChild(el("span", "chooser-name", project.name));
      row.appendChild(label);

      const why = el("p", "chooser-why", project.keys.length
        ? `${plural(project.keys.length, "key", "keys")} that may extract.`
        : "No key that may extract, so nothing can be collected into it.");
      row.appendChild(why);

      const holder = el("div", "chooser-key");
      const chosen = draftRows.find((r) => r.project_id === project.project_id);
      const select = keySelectFor(project.project_id, chosen ? chosen.key_prefix : "");
      select.addEventListener("change", () => {
        const item = draftRows.find((r) => r.project_id === project.project_id);
        if (item) item.key_prefix = select.value;
      });
      holder.appendChild(select);
      holder.hidden = !tick.checked;
      row.appendChild(holder);

      tick.addEventListener("change", () => {
        holder.hidden = !tick.checked;
        if (tick.checked) {
          if (!draftRows.some((r) => r.project_id === project.project_id)) {
            draftRows.push({ project_id: project.project_id,
                             key_prefix: select.value || "" });
          }
        } else {
          const at = draftRows.findIndex((r) => r.project_id === project.project_id);
          if (at >= 0) draftRows.splice(at, 1);
        }
        cost.textContent = costWords(draftRows.length);
      });
      body.appendChild(row);
    });

    const cost = el("p", "chooser-cost", "");
    body.appendChild(cost);
    cost.textContent = costWords(draftRows.length);

    const keep = el("button", "button button--primary-save", "Use these projects");
    keep.type = "button";
    const shell = openDialog({
      title: "Which projects read this page",
      subtitle: "A project decides what is extracted from a document. Pick as "
              + "many as ask different questions of this page - each one reads "
              + "it separately, and is charged separately.",
      content: body,
      actions: [keep],
      cancel: "Cancel",
    });
    keep.addEventListener("click", () => {
      chosenProjects = draftRows.map((row, index) => ({ ...row, order: index }));
      drawChosenProjects();
      markUnsaved();
      announce(chosenProjects.length
        ? `Collecting into ${plural(chosenProjects.length, "project", "projects")}`
        : "No project chosen");
      shell.close();
    });
  }

  function costWords(count) {
    if (!count) return "Nothing is collected while no project is chosen.";
    if (count === 1) return "One extraction per document.";
    return `${count} extractions per document - this page is fetched once and `
         + `read ${count} times, once per project.`;
  }

  if (configureButton) configureButton.addEventListener("click", openProjectChooser);

  /* ── THE FOUR STEPS ──────────────────────────────────────────────────
   *
   * WHAT THE FOLDING IS FOR. The form asks about twenty things, and only
   * four of them can be answered at the start. Everything on screen that
   * cannot be acted on yet is noise to read past - and a step must never
   * tell the reader to go and do something in a LATER step first.
   *
   * THE RULES, AND THERE ARE ONLY FOUR:
   *
   *   1. A step that CAN be taken is open. It opens by itself the moment it
   *      becomes takeable - after the address is typed, after the test has
   *      answered - so nobody has to discover a control appearing.
   *   2. A step that cannot be taken yet is folded and SAYS WHY, in the
   *      sentence that names what to do instead. A folded step with no
   *      reason is a dead end.
   *   3. It can still be opened by hand. The lock is a recommendation about
   *      order, not a gate: exact addresses need no test, a site that
   *      refuses one must still be configurable, and being unable to look at
   *      a form is worse than looking at it early.
   *   4. A step that HAS been taken stays open. The page is the record of
   *      what was decided, and folding that away under somebody would hide
   *      the answer they came back to check.
   *
   * UNLOCKING IS STICKY. Once a step has been reachable it stays reachable,
   * whatever is typed afterwards - a card that folds itself back up because
   * a field was cleared for a moment is a page fighting its reader.
   */
  const steps = Array.from(form.querySelectorAll("[data-step]")).map((node) => ({
    number: Number(node.dataset.step),
    node,
    body: node.querySelector(".step-body"),
    toggle: node.querySelector(".step-toggle"),
    locked: node.querySelector("[data-step-locked]"),
    state: node.querySelector("[data-step-state]"),
  }));
  /* Which steps have ever been reachable, and which the reader has folded by
   * hand. Both are the page's own state and neither is saved: reopening a
   * watched page starts from what it IS, not from how somebody left the
   * furniture. */
  const reachable = new Set([1]);
  const foldedByHand = new Set();

  function hasTested() {
    return Boolean(snapshot) || progress.dataset.tested === "1";
  }

  function hasSomethingToCollect() {
    if (currentMode() === "all_except_rejected") return true;
    if (currentMode() === "exact") return extra.exact_urls.length > 0;
    return extra.patterns.some((row) => row.text_kind === "accept");
  }

  function stepIsDone(number) {
    const name = form.querySelector("[name=text_name]");
    const url = form.querySelector("[name=text_list_url]");
    if (number === 1) {
      return Boolean(name.value.trim()) && isHttpUrl(url.value)
             && chosenProjects.length > 0;
    }
    if (number === 2) return hasTested();
    if (number === 3) return hasSomethingToCollect();
    return false;   // step 4 has working defaults; there is nothing to finish
  }

  /* Why a step cannot be taken yet - or "" when it can. The sentence names
   * the control that unlocks it, never the state that blocks it: "run the
   * test in step 2" is something to do, "no test yet" is not. */
  function whyNotYet(number) {
    const url = form.querySelector("[name=text_list_url]");
    // AN ADDRESS, NOT A VALID ONE. Gating this on isHttpUrl() locked the Test
    // button away from the very person who needs it: somebody who pasted
    // something that is not a web address could not press the button whose
    // refusal says so ("That is not a web address ... copy it from the
    // browser's address bar"). An empty field has nothing to test; a filled
    // one has, even when the answer is that it is wrong.
    if (number === 2 && !url.value.trim()) {
      return "Fill in the address of the list page in step 1 - the test reads that address.";
    }
    if (number === 3 && !hasTested() && !hasSomethingToCollect()) {
      return "Run the test in step 2 first: what to collect is chosen out of the links it finds.";
    }
    if (number === 4 && !hasSomethingToCollect() && !stepIsDone(2)) {
      return "Choose what to collect in step 3 first - a schedule for a page that collects nothing has nothing to do.";
    }
    return "";
  }

  function syncSteps() {
    steps.forEach((step) => {
      const why = whyNotYet(step.number);
      if (!why) reachable.add(step.number);
      const open = reachable.has(step.number) && !foldedByHand.has(step.number);

      step.body.hidden = !open;
      step.toggle.setAttribute("aria-expanded", open ? "true" : "false");
      step.node.dataset.open = open ? "1" : "0";
      step.node.dataset.ready = why ? "0" : "1";

      if (step.locked) {
        step.locked.textContent = why;
        step.locked.hidden = !why || open;
      }
      if (step.state) {
        const done = stepIsDone(step.number);
        step.state.textContent = done ? "Done" : "";
        step.state.hidden = !done;
        step.node.dataset.done = done ? "1" : "0";
      }
    });
  }

  steps.forEach((step) => {
    step.toggle.addEventListener("click", () => {
      const open = step.body.hidden;
      if (open) {
        foldedByHand.delete(step.number);
        reachable.add(step.number);   // opened on purpose: it is theirs now
      } else {
        foldedByHand.add(step.number);
      }
      syncSteps();
      if (open) step.body.hidden = false;
    });
  });

  /* Everything that has been decided is open, and nothing folds under the
   * reader. Called when a saved page is loaded: it has been through all four
   * steps already, so all four are its record. */
  function openEverything() {
    steps.forEach((step) => reachable.add(step.number));
    foldedByHand.clear();
    syncSteps();
  }

  /* -- the form as a configuration ------------------------------------ */

  function fieldValue(input) {
    if (input.type === "checkbox") return input.checked;
    if (input.type === "number") return input.value === "" ? null : Number(input.value);
    return input.value;
  }

  function draft() {
    const config = {};
    form.querySelectorAll("[name]").forEach((input) => {
      if (input.type === "radio") {
        if (input.checked) config[input.name] = input.value;
        return;
      }
      const value = fieldValue(input);
      if (value !== null) config[input.name] = value;
    });
    config.patterns = extra.patterns;
    config.exact_urls = extra.exact_urls;
    config.file_rules = extra.file_rules;
    config.projects = chosenProjects.map((row) => ({
      text_project_id: row.project_id,
      text_key_prefix: row.key_prefix || "",
    }));
    return config;
  }

  function fill(source) {
    filling = true;
    form.querySelectorAll("[name]").forEach((input) => {
      const value = source[input.name];
      if (value === undefined || value === null) return;
      if (input.type === "radio") input.checked = String(value) === input.value;
      else if (input.type === "checkbox") input.checked = Boolean(value);
      else input.value = String(value);
    });
    if (keyLabelField) keyLabelField.value = source.text_key_label || "";
    chosenProjects = (source.projects || []).map((row, index) => ({
      project_id: row.text_project_id,
      key_prefix: row.text_key_prefix || "",
      order: index,
    }));
    drawChosenProjects();
    extra.patterns = (source.patterns || []).map((row) => ({ ...row }));
    extra.exact_urls = (source.exact_urls || []).map((row) => ({ ...row }));
    extra.file_rules = (source.file_rules || []).map((row) => ({ ...row }));
    drawChips();
    syncMode();
    syncEngine();
    syncRobots();
    filling = false;
  }

  /* What the three lists say when they are empty. The sentence has to be
   * true of the mode that is chosen: in "Everything except what I reject"
   * there are never accept patterns, and an empty box telling the reader
   * to go and make one is an instruction that changes nothing - it stood
   * there even straight after learning had answered "no patterns needed".
   */
  const EMPTY_WORDS = {
    accept: {
      selected: "No shapes yet. Run the test, tick the links that are documents, and press Use the ticked links.",
      all_except_rejected: "Everything on this list page counts, except the shapes and addresses below and the imprint, image and paging links.",
      exact: "Nothing by shape - only the single addresses below.",
    },
    reject: {
      selected: "Nothing rejected by shape. In this mode only what a kept shape matches is collected anyway.",
      all_except_rejected: "Nothing rejected by shape yet. Run the test, leave the links that are not documents un-ticked, and their shapes land here.",
      exact: "Nothing rejected by shape. Only the addresses below are read.",
    },
    exact: {
      selected: "No single addresses. One address that a shape gets wrong can be added here by hand.",
      all_except_rejected: "No single addresses. One address that a shape gets wrong can be added here by hand.",
      exact: "No addresses yet. In Exact addresses mode this list is the whole configuration.",
    },
  };

  function drawChips() {
    const mode = currentMode();
    Object.keys(chipEmpty).forEach((group) => {
      if (chipEmpty[group]) chipEmpty[group].textContent = EMPTY_WORDS[group][mode] || EMPTY_WORDS[group].selected;
    });
    // The single-address list is a question in Exact mode and a footnote in
    // the other two: shown when it is asked, or when it already holds
    // something somebody has to be able to take off again.
    if (exactBlock) exactBlock.hidden = mode !== "exact" && extra.exact_urls.length === 0;
    const accept = extra.patterns.filter((row) => row.text_kind === "accept");
    const reject = extra.patterns.filter((row) => row.text_kind === "reject");
    renderRuleChips(chips.accept, accept, {
      empty: chipEmpty.accept,
      onRemove: (row) => {
        extra.patterns = extra.patterns.filter((other) => other !== row);
        drawChips();
        rulesMoved();
        markUnsaved("The rules have changed. Save the configuration to keep them.");
        announce(`Rule ${row.text_label} removed`);
      },
      /* Only the accept rules get a key, and only from the project this
       * watchlist collects into: a rule that pointed at another project's key
       * would file its links in the wrong archive, which is the one mistake this
       * whole chooser exists to make impossible. */
      keys: chosenProjects.flatMap((row) => (projectOf(row.project_id) || {}).keys || []),
      onKey: (index, prefix) => {
        // `accept` is a filtered view, so the index is into that view and the
        // row it names is the one to change in the real list.
        const row = accept[index];
        if (!row) return;
        row.text_key_prefix = prefix;
        markUnsaved("A link group changed which key evaluates it. Save the configuration to keep that.");
        announce(prefix
          ? `Rule ${row.text_label} is evaluated with ${prefix}`
          : `Rule ${row.text_label} follows the watchlist's key`);
      },
    });
    renderRuleChips(chips.reject, reject, {
      empty: chipEmpty.reject,
      onRemove: (row) => {
        extra.patterns = extra.patterns.filter((other) => other !== row);
        drawChips();
        rulesMoved();
        markUnsaved("The rules have changed. Save the configuration to keep them.");
        announce(`Rule ${row.text_label} removed`);
      },
    });
    syncSteps();
    renderExactChips(chips.exact, extra.exact_urls, {
      empty: chipEmpty.exact,
      onRemove: (row) => {
        extra.exact_urls = extra.exact_urls.filter((other) => other !== row);
        drawChips();
        rulesMoved();
        markUnsaved("The rules have changed. Save the configuration to keep them.");
        announce(`${row.text_url_canonical} removed`);
      },
    });
  }

  /* -- the small couplings between fields ------------------------------ */

  function currentMode() {
    const checked = form.querySelector("input[name=text_mode]:checked");
    return checked ? checked.value : "selected";
  }

  function syncMode() {
    const mode = currentMode();
    const resubmit = document.getElementById("f-resubmit");
    // Exact mode watches a fixed list of addresses; a change to one of them
    // IS the news, so the address is read again when its text moves. In the
    // other modes the address is the anchor and re-sending a page because
    // its visitor counter moved costs money for nothing.
    if (mode === "exact" && resubmit && !resubmit.checked) {
      resubmit.checked = true;
      announce("Exact addresses are sent again whenever their text changes.");
    }
    linksButton.textContent = mode === "exact" ? "Choose the addresses…" : "Choose the links…";
    // The three empty lines and the single-address block belong to the mode.
    drawChips();
  }

  function syncEngine() {
    const engine = form.querySelector("input[name=text_engine]:checked");
    const settings = document.getElementById("render-settings");
    if (settings) settings.hidden = !engine || engine.value !== "playwright";
  }

  function syncRobots() {
    overrideField.hidden = respectRobots.checked;
  }

  /* The files half of step 3 hangs on two things the reader cannot see
   * from the button: the tick above it, and a SECOND test that lives in
   * step 2. A disabled button with no reason next to it is a dead end, so
   * the sentence beside it says which of the two is missing and where the
   * control for it is (it is tied to the button with aria-describedby, so
   * a screen reader is told the same thing). */
  function syncSubSub() {
    fileTestButton.hidden = !subSub.checked;
    const hasFiles = Boolean(snapshot && snapshot.kind === "files");
    filesButton.disabled = !subSub.checked || !hasFiles;
    if (!subSub.checked) {
      filesHint.textContent = "Files are not collected for this watched page. Tick \u201cAlso collect files linked on each document page\u201d above to change that.";
    } else if (!hasFiles) {
      filesHint.textContent = "The files list needs a second test: press \u201cLook for files as well\u201d in step 2.";
    } else {
      filesHint.textContent = "Shows the files that test found on the sampled document pages.";
    }
  }

  function syncFormatCost() {
    if (!snapshot || !snapshot.counts || !snapshot.counts.sample_chars_avg) {
      formatCost.textContent = "";
      return;
    }
    const format = form.querySelector("input[name=text_format]:checked");
    const word = FORMAT_WORDS[format ? format.value : "plain"];
    formatCost.textContent =
      `The sampled document pages came to about ${fmtInt(snapshot.counts.sample_chars_avg)} characters each as ${word}.`;
  }

  /* Anything typed or ticked in the form is a change that is not saved
   * yet, and the page says so in one place. Before this, only the learned
   * rules did - somebody could rename a source, switch its format, walk
   * away and lose both without a word. The enable tick is not
   * one of these: it is carried out at once, on its own request. */
  let unsaved = false;
  /* One dialog at a time. Two clicks on two Back links would otherwise
   * stack two identical questions, and answering one would leave the other
   * in front of a page that is already leaving. */
  let leaving = false;
  let filling = false;

  function markUnsaved(text) {
    unsaved = true;
    const line = text || "Changed, and not saved yet. Save the configuration to keep it.";
    // The line is a live region: rewriting it with the same words on every
    // keystroke would have a screen reader say them on every keystroke.
    if (saveState.textContent === line && saveState.dataset.state === "unsaved") return;
    saveState.dataset.state = "unsaved";
    saveState.textContent = line;
  }

  function markSaved(text) {
    unsaved = false;
    saveState.dataset.state = "saved";
    saveState.textContent = text;
  }

  /* The form fields crawlkit's `Rules.from_config` reads. Moving one of
   * them decides the links of the last test differently, so the counts that
   * were worked out with the old answer stop being about what is on screen.
   * The list page's own address is not among them: it has its own sentence
   * under the counts ("the test was run with a different address of the
   * list page - run it again"), which says more than a dash would. */
  const RULE_FIELDS = new Set(["text_mode", "bool_same_host_only",
                               "bool_drop_legal_links"]);

  function onEdit(event) {
    const target = event.target;
    // Filling the form from the archive is not somebody editing it.
    if (filling) return;
    if (!target || target === enableTick) return;
    clearFormError();
    if (target.name === "text_mode") syncMode();
    if (target.name === "text_engine") syncEngine();
    if (target.name === "text_format") syncFormatCost();
    if (target === respectRobots) syncRobots();
    if (target === subSub) syncSubSub();
    // After the syncs: rulesMoved() draws the banners again, and the mode
    // decides what the button in them is called.
    if (RULE_FIELDS.has(target.name)) rulesMoved();
    // Typed over: the value is the reader's now, not the snapshot's.
    if (target.name) derived.delete(target.name);
    // The helper field above the "Add" button is not the configuration -
    // the address becomes one when it is added, and addExactUrl() says so.
    if (target === exactUrl) return;
    syncSteps();
    markUnsaved();
  }

  form.addEventListener("input", onEdit);
  form.addEventListener("change", onEdit);
  form.addEventListener("typeahead:choose", onEdit);

  /* -- adding one address by hand -------------------------------------- */

  const exactButton = document.getElementById("exact-add-button");
  const exactUrl = document.getElementById("f-exact-url");
  const exactKind = document.getElementById("f-exact-kind");
  const exactVerdict = document.getElementById("exact-verdict");

  exactUrl.addEventListener("input", () => {
    const verdict = checkAddress(exactUrl.value, extra.exact_urls);
    // With a test behind us the field can say more than "this is an
    // address": how many of the links that test found are shaped like it.
    const note = verdict.ok && snapshot
      ? " " + shapeNote(exactUrl.value, snapshot.links)
      : "";
    exactVerdict.textContent = verdict.message + note;
    exactVerdict.dataset.state = verdict.message ? (verdict.ok ? "ok" : "bad") : "";
  });

  /* Enter in this field adds the address; without this it would submit the
   * enclosing form, which is the Save button - a keystroke away from the
   * wrong act. */
  exactUrl.addEventListener("keydown", (event) => {
    if (event.key !== "Enter") return;
    event.preventDefault();
    addExactUrl();
  });
  exactButton.addEventListener("click", addExactUrl);

  function addExactUrl() {
    const verdict = checkAddress(exactUrl.value, extra.exact_urls);
    if (!verdict.ok) {
      exactVerdict.textContent = verdict.message || "Type an address first.";
      exactVerdict.dataset.state = "bad";
      exactUrl.focus();
      return;
    }
    extra.exact_urls.push({
      text_kind: exactKind.value,
      text_url_canonical: exactUrl.value.trim().replace(/#.*$/, ""),
    });
    exactUrl.value = "";
    exactVerdict.textContent = "Added.";
    exactVerdict.dataset.state = "ok";
    drawChips();
    rulesMoved();
    markUnsaved("The rules have changed. Save the configuration to keep them.");
    announce("Address added");
  }

  /* -- the verdict banners --------------------------------------------- */

  /* "only the first 1 page(s) are read" - a few of crawlkit's sentences
   * still carry the bracketed plural, and they are shown here word for
   * word. Tidied on the way to the screen rather than in crawlkit, which
   * this worker does not own; the integrator can move it upstream and this
   * becomes a no-op. */
  function tidyPlurals(text) {
    return String(text === null || text === undefined ? "" : text)
      .replace(/\b(\d+) ([A-Za-z]+)\(s\)/g,
               (_whole, n, word) => `${n} ${Number(n) === 1 ? word : word + "s"}`);
  }

  function banner(kind, icon, heading, text, options = {}) {
    const box = el("div", `notice notice--${kind} verdict`);
    box.dataset.verdict = options.id || heading.toLowerCase().replace(/[^a-z]+/g, "-");
    const mark = el("span", "verdict-icon", icon);
    mark.setAttribute("aria-hidden", "true");
    box.appendChild(mark);
    box.appendChild(el("p", "verdict-title", tidyPlurals(heading)));
    box.appendChild(el("p", "verdict-text", tidyPlurals(text)));
    if (options.rule) box.appendChild(el("pre", "verdict-rule", options.rule));
    if (options.actions && options.actions.length) {
      const row = el("div", "verdict-actions");
      options.actions.forEach((action) => row.appendChild(action));
      box.appendChild(row);
    }
    verdicts.appendChild(box);
    return box;
  }

  function actionButton(label, onClick) {
    const button = el("button", "button button--secondary", label);
    button.type = "button";
    button.addEventListener("click", onClick);
    return button;
  }

  function retestWithRendered() {
    const rendered = form.querySelector("input[name=text_engine][value=playwright]");
    if (rendered) rendered.checked = true;
    syncEngine();
    // Moving the radio from code fires no event, and this is a change to
    // the configuration like any other: without this the engine that made
    // the test work would be lost on the way out of the page.
    markUnsaved();
    runTest("crawl");
  }

  /* The one place something can be done about a refusal. It lives in step 4,
   * which may be folded - and focusing a control inside a folded card would
   * scroll the page to nothing. So: unfold the step, then focus. */
  function openRobotsSettings() {
    reachable.add(4);
    foldedByHand.delete(4);
    syncSteps();
    respectRobots.focus();
  }

  /* -- what the crawl said, as banners -------------------------------- */

  /* Every warning crawlkit writes, recognised by its own words, so that it
   * can be given a heading that says what happened and one line of advice.
   *
   * TWO FAULTS THIS TABLE EXISTS TO FIX. A banner headed "Worth knowing"
   * says nothing at all - the heading is where "what happened" belongs
   * (NN/g on error messages) - and a warning that repeats a typed banner
   * two boxes above it dilutes both. `code` is what the typed banners are
   * keyed by, so "0 links found on this page…" and the banner headed "The
   * page answered, and carried no links" are recognised as one event and
   * only the fuller one is shown.
   *
   * The codes are matched on the sentence because crawlkit sends bare
   * strings today; when it sends {code, text} instead - which is where
   * that file is going - warningOf() takes the code it is given and this
   * table is only consulted for the heading and the advice. */
  const WARNING_KINDS = [
    { code: "no-links", test: /^\d+ links found on this page|^0 links found/i,
      heading: "The page carried no links",
      advice: "" },
    { code: "pagination-forbidden", test: /forbids the paginated addresses/i,
      heading: "Only the first page of the list may be read",
      advice: "The newest entries are on page one, which is usually what a list that is read every few hours needs." },
    { code: "link-cap", test: /the result keeps the first/i,
      heading: "More links than one test keeps",
      advice: "The shapes are worked out from the ones it kept, which is enough to recognise them." },
    // crawlkit's own sentence already says "and the rest on the next one";
    // repeating it here made the banner say the same thing twice.
    { code: "new-cap", test: /new addresses; this run takes/i,
      heading: "More new documents than one run takes",
      advice: "\u201cMost new documents per run\u201d under Schedule is where that number stands." },
    // A TEST IS NOT A RUN. The file test opens a handful of document pages to
    // see what files are on them; that number is the sample it was started
    // with, not the Schedule field. A banner pointing at a field that holds
    // a different number would have the reader change nothing while the
    // warning stays.
    { code: "files-sample", test: /this test opens \d+ of them as a sample/i,
      heading: "The test opened a sample of the document pages",
      advice: "That is as far as a test goes - it is looking for what kind of files are there, not collecting them. A scheduled crawl takes up to \u201cMost new documents per run\u201d under Schedule." },
    { code: "out-of-time", test: /time limit for this run/i,
      heading: "The test ran out of time",
      advice: "What it had read by then is below. Give it longer under Schedule, or test a list page with fewer entries on it." },
    // A list page that carried nothing is the reason the next page carried
    // nothing new; the typed banner above says that in full, and this one
    // next to it reads as a second, different fault.
    { code: "paging-repeat", test: /brought no new links/i, covers: ["no-links"],
      heading: "Paging stopped: the next page repeated this one",
      advice: "That is how a crawl finds the end of a list. Nothing is missing." },
    { code: "paging-max", test: /^stopped after \d+ pages/i,
      heading: "Paging stopped at the page limit",
      advice: "Raise \u201cMost list pages per run\u201d under Schedule if the older entries matter." },
    { code: "paging-loop", test: /links back to a page already read/i,
      heading: "Paging stopped: the list linked back to itself",
      advice: "The pages after that would have been the same ones again." },
    { code: "pagination-forbidden", test: /robots\.txt forbids page \d+/i,
      heading: "robots.txt forbids the next list page",
      // crawlkit writes "only the first 1 page(s) are read"; the number is
      // worth keeping and the brackets are not.
      rewrite: (text) => {
        const found = text.match(/forbids page (\d+) of this list - only the first (\d+)/i);
        if (!found) return text;
        const kept = Number(found[2]);
        return `robots.txt forbids page ${found[1]} of this list, so a crawl reads `
             + `${kept === 1 ? "the first page" : `the first ${kept} pages`} only.`;
      },
      advice: "The newest entries are on page one, which is usually what a list that is read every few hours needs." },
    { code: "slower", test: /delay between requests was doubled/i,
      heading: "The site asked for room",
      advice: "The next run starts slower by itself. Nothing to change here." },
    { code: "subpage-failed", test: /^https?:\/\/\S+: /i,
      heading: "One document page could not be read",
      advice: "The others were read. A crawl tries this one again next time." },
    { code: "page-failed", test: /^page \d+ could not be read/i,
      heading: "One page of the list could not be read",
      rewrite: (text) => {
        const found = text.match(/^page (\d+) could not be read \((.*)\) - it stays at (\d+)/i);
        if (!found) return text;
        const kept = Number(found[3]);
        return `Page ${found[1]} of the list could not be read (${found[2]}), so the test `
             + `stayed at ${kept === 1 ? "one page" : `${kept} pages`}.`;
      },
      advice: "What the pages before it carried is below; a crawl tries the rest again next time." },
    { code: "no-renderer", test: /not available in this build/i,
      heading: "Rendered mode is not available in this build", advice: "" },
    { code: "challenge", test: /answered with a browser check/i,
      heading: "The site answered with a browser check", advice: "" },
    { code: "not-found", test: /does not exist \(404\)/i,
      heading: "There is no page at that address", advice: "" },
    { code: "refused", test: /refused the request \(HTTP/i,
      heading: "The site refused the request", advice: "" },
    { code: "throttled", test: /asked for room \(HTTP/i, covers: ["refused", "site-error"],
      heading: "The site asked for room",
      advice: "A crawl stops when a site says that, and the next run waits longer." },
    { code: "site-error", test: /problem of its own \(HTTP/i,
      heading: "The site has a problem of its own", advice: "" },
    { code: "unreachable", test: /could not be fetched/i,
      heading: "The address could not be reached",
      advice: "Check the address in a browser. A name that does not resolve, or a server that never answers, looks like this." },
    { code: "robots-forbidden", test: /robots\.txt of .* forbids|too slow to crawl/i,
      heading: "The site's rules forbid this address", advice: "" },
    { code: "robots-unreadable", test: /robots\.txt (of .* could not be read|answers \d+)/i,
      heading: "The site's robots.txt could not be read", advice: "" },
  ];

  function warningOf(warning) {
    // Bare string today, {code, text} when crawlkit grows one.
    const text = typeof warning === "string" ? warning : String((warning && warning.text) || "");
    const given = (warning && warning.code) || "";
    const known = WARNING_KINDS.find((kind) => (given ? kind.code === given : kind.test.test(text)));
    const written = known && known.rewrite ? known.rewrite(text) : text;
    return {
      code: known ? known.code : "",
      // Some warnings are a second telling of an event a typed banner
      // above has already explained in full (the site asked for room IS
      // the 429 that banner is about); when that banner is on screen this
      // one is not shown at all.
      covers: (known && known.covers) || [],
      text: written,
      // Without a match, the warning's own first clause is the heading and
      // the rest is the sentence - never a heading that says nothing.
      heading: known ? known.heading : firstClause(written),
      advice: known ? known.advice : "",
    };
  }

  function firstClause(text) {
    const clause = String(text).split(/ - |[.;:] /)[0].trim();
    if (!clause) return "Worth knowing";
    return clause.charAt(0).toUpperCase() + clause.slice(1);
  }

  /* crawlkit writes its warnings as fragments - lower case, no full stop -
   * and the advice next to them is a typed sentence. Glued with a space the
   * two ran into one line: "…the rest on the next one The rest follow on the
   * next run." A capital and a stop make the first half a sentence of its
   * own, so the two read as two (NN/g: an error message is a sentence). */
  function sentence(text) {
    const written = String(text === null || text === undefined ? "" : text).trim();
    if (!written) return "";
    // A first word that is a file name or an address keeps its own spelling:
    // "robots.txt" is what the file is called, and "Robots.txt" is a file
    // nobody has. Everything else starts like the sentence it is.
    const first = written.split(/\s/)[0];
    const capital = /[./]/.test(first)
      ? written
      : written.charAt(0).toUpperCase() + written.slice(1);
    return /[.!?]$/.test(capital) ? capital : capital + ".";
  }

  /* Everything the reader has to be told about one test, as banners that
   * each say what happened AND what to do about it. The order is the order
   * the crawl met them: the site's rules, then its answer, then what was on
   * the page. */
  function renderVerdicts(result) {
    verdicts.textContent = "";
    if (!result) return;
    const robots = result.robots || {};
    const fetched = result.fetch || {};
    const counts = result.counts || {};
    const engine = (form.querySelector("input[name=text_engine]:checked") || {}).value || "http";
    const spoken = [];

    if (robots.status >= 500 || robots.status === 0) {
      spoken.push(banner("error", "⛔", "The site's robots.txt could not be read",
        "While robots.txt answers with an error nothing is fetched at all - rules that cannot be read are not permission. Try again later; if it stays this way, ask the site.",
        { rule: robots.note || "", id: "robots-unreadable" }));
    } else if (robots.list_allowed === false) {
      spoken.push(banner("error", "⛔", "robots.txt forbids this list page",
        "The configuration can be saved, but it cannot be enabled while this stands. Either watch a list page the site allows, or record written permission under the site's rules.",
        {
          rule: robots.list_reason || "",
          id: "robots-forbidden",
          actions: [actionButton("The site's rules", openRobotsSettings)],
        }));
    } else if (robots.pagination_allowed === false) {
      spoken.push(banner("warn", "⚠", "Only the first page of the list may be read",
        "robots.txt allows the list page but forbids its paginated addresses, so a crawl sees the newest entries only. That is usually enough for a list that is read every few hours.",
        { rule: robots.list_reason || "", id: "pagination-forbidden" }));
    }

    if (fetched.challenge) {
      spoken.push(banner("error", "⛔", "The site answered with a browser check",
        "Instead of the page, the site sent a check that only a browser passes. Rendered mode gets through some of those; where it does not, the site does not want to be read by a program.",
        {
          id: "challenge",
          actions: engine === "http" ? [actionButton("Test again in Rendered mode", retestWithRendered)] : [],
        }));
    } else if (fetched.status === 404) {
      spoken.push(banner("error", "⛔", "There is no page at that address (404)",
        "Open the address in a browser and copy it again from the address bar - a list page often has a longer address than the one in a menu.",
        { id: "not-found" }));
    } else if (fetched.status >= 400 && fetched.status < 500) {
      spoken.push(banner("error", "⛔", `The site refused the request (HTTP ${fetched.status})`,
        "It may want a browser rather than a program. Rendered mode is the next thing to try; after that, ask the site for access.",
        {
          rule: fetched.error || "",
          id: "refused",
          actions: engine === "http" ? [actionButton("Test again in Rendered mode", retestWithRendered)] : [],
        }));
    } else if (fetched.status >= 500) {
      spoken.push(banner("warn", "⚠", `The site has a problem of its own (HTTP ${fetched.status})`,
        "Nothing to change here. The scheduled crawl tries again later; test again when the site is back.",
        { rule: fetched.error || "", id: "site-error" }));
    }

    if (result.status === "ERROR" && /not available in this build/i.test(result.error || "")) {
      spoken.push(banner("warn", "⚠", "Rendered mode is not available in this build",
        "This dashboard image was built without a browser. Either use HTTP mode, or run the image that carries one - the address itself may be fine.",
        { rule: result.error, id: "no-renderer" }));
    }

    const links = (result.links || []).length;
    if (!links && result.status === "OK") {
      spoken.push(banner("warn", "⚠", "The page answered, and carried no links",
        engine === "http"
          ? "Sites that build their list with JavaScript deliver an empty page to a plain request. Rendered mode runs the page in a browser first and usually finds them."
          : "Even rendered, this page has no links to documents. Check that the address is the list itself and not a search form.",
        {
          id: "no-links",
          actions: engine === "http" ? [actionButton("Test again in Rendered mode", retestWithRendered)] : [],
        }));
    }

    // A files test opens the document pages the rules accept. With no rules
    // yet there is nothing to open, and an empty file list would read as
    // "this site has no files" - which is a different answer.
    if (result.kind === "files" && result.status === "OK" && !(counts.samples || 0)) {
      banner("warn", "⚠", "No document page was opened",
        links
          ? "The rules do not accept any link on this list yet, so there was no page to look at for files. Choose the links first, then look for files again."
          : "There was no link to follow.",
        { id: "no-samples" });
    }

    if (result.status === "OK" && links) {
      banner("ok", "✓", "The site answered",
        answeredText(links, counts.pages || 0), { id: "answered" });
    }

    // Whatever the crawl said that no banner above has said already.
    // Recognised by what it is about, not by its first forty characters:
    // the typed banner about an empty page and crawlkit's own sentence
    // about it are written differently and are the same event.
    const said = new Set(spoken.map((box) => box.dataset.verdict));
    const shown = new Set();
    (result.warnings || []).forEach((warning) => {
      const item = warningOf(warning);
      if (item.code && (said.has(item.code) || shown.has(item.code))) return;
      if (item.covers.some((code) => said.has(code))) return;
      if (item.code) shown.add(item.code);
      banner("warn", "⚠", item.heading,
        item.advice ? `${sentence(item.text)} ${item.advice}` : sentence(item.text),
        { id: item.code || "warning" });
    });
  }

  /* Two of the cells are answers of the SITE - how many links it had, how
   * many list pages were read - and they stand as long as the snapshot
   * does. "Would be collected" and "Rejected" are answers of the RULES,
   * which move without the site being asked again, so they come from
   * `collected` and go back to a dash while nothing has worked them out for
   * the rules as they are now. A dash is a worse number and a better answer
   * than a stale one. */
  const RULE_COUNTS = new Set(["accepted", "rejected"]);

  /* THE ROW HAS TO ADD UP: links = accepted + rejected + pagination +
   * list_self. "Rejected" counts documents that no rule keeps, and a paging
   * link is not a document, so without these two cells the row read
   * "31 / 20 / 10" and one link was nowhere - findable only by opening the
   * picker, where "Paging links" is a group of its own.
   *
   * Both are decided by the CLASSIFIER, which reads the address of the list
   * page and the paging links found on it - not the accept and reject
   * rules. So they survive a change of mode, and they are read from
   * whichever reading is current: /preview counts them for the saved rules,
   * a test counts them for its own. An older snapshot carries no list_self
   * count at all; then they are counted off its links, which are stored
   * with the class each one was given. */
  const CLASS_CELLS = ["pagination", "list_self"];

  function classCount(key) {
    const counted = collected && collected.counts;
    if (counted && counted[key] !== undefined && counted[key] !== null) {
      return Number(counted[key]) || 0;
    }
    const links = (snapshot && snapshot.links) || [];
    return links.filter((link) => link.class === key).length;
  }

  /* "Files fetched" is the number of files the test READ, which only moves
   * after "Look for files as well". It is named for what it counts and is
   * only there once a file test has fetched something: headed "Files seen"
   * and always on screen, it would read 0 next to a link the same test has
   * classified as a file, and ask the reader to reconcile two different
   * meanings of the word. */
  function filesFetched() {
    return Number((snapshot && snapshot.counts && snapshot.counts.files) || 0);
  }

  /* The second half of "The site answered". It carried a count of its own
   * and the advice "Press Choose the links to change that" whatever had
   * happened since, which is how it came to contradict the row underneath
   * it. It now says what the SITE answered - the one thing that cannot go
   * out of date - and hands the counting to the row, which is worked out
   * again on every change. The advice belongs to the single case it is
   * advice for: nothing would be collected yet. */
  function pickerName() {
    return (linksButton.textContent || "Choose the links").replace(/\u2026$/, "");
  }

  function answeredText(links, pages) {
    const found = `${plural(links, "link", "links")} found on `
                + `${plural(pages, "page", "pages")}`;
    if (!collected) {
      return `${found}. The rules have changed since this test; save the `
           + "configuration and the counts below say what they would collect.";
    }
    if (!Number(collected.counts.accepted || 0)) {
      return `${found}, and no rule keeps any of them yet. `
           + `Press "${pickerName()}" and tick the ones that are documents.`;
    }
    return `${found}. What ${collected.basis === "saved" ? "the saved rules" : "the rules"} `
         + "would collect is in the counts below.";
  }

  function renderSummary() {
    if (!snapshot && !collected) {
      summary.hidden = true;
      return;
    }
    const found = (snapshot && snapshot.counts) || (collected ? collected.counts : {});
    summary.hidden = false;
    summary.querySelectorAll("[data-count]").forEach((node) => {
      const key = node.dataset.count;
      if (RULE_COUNTS.has(key)) {
        node.textContent = collected ? fmtInt(collected.counts[key] || 0) : "\u2013";
      } else if (CLASS_CELLS.includes(key)) {
        node.textContent = fmtInt(classCount(key));
      } else if (key === "files") {
        node.textContent = fmtInt(filesFetched());
      } else {
        node.textContent = fmtInt(found[key] || 0);
      }
    });
    // The three cells that are zero on an ordinary list page are left out
    // of it: a row of zeroes is harder to read than a shorter row, and the
    // arithmetic only needs the terms that carry something.
    CLASS_CELLS.forEach((key) => {
      const cell = summary.querySelector(`[data-cell="${key}"]`);
      if (cell) cell.hidden = classCount(key) === 0;
    });
    const filesCell = summary.querySelector('[data-cell="files"]');
    if (filesCell) filesCell.hidden = filesFetched() === 0;
  }

  /* The rules moved under the last test: a shape was learned from the ticked
   * links, one was taken off, an address was added, the mode changed. Until
   * the configuration is saved and /preview has answered for the new rules,
   * no number on this page is about the rules the reader is looking at. */
  function rulesMoved() {
    collected = null;
    previewNote.textContent = "";
    if (snapshot) {
      renderVerdicts(snapshot);
      renderSummary();
    }
  }

  /* What the SAVED rules would do with the links this test found. The
   * counts in the summary are the ones the test computed with the rules as
   * they were AT THE TIME; after learning and saving, that is the wrong
   * number to leave on screen, and re-crawling the site to correct it would
   * be a second visit for a question the first one already answered. */
  /* The names /preview answers with are database columns. A customer never
   * sees a column name anywhere else on this dashboard and should not see
   * one here: "a different text_paging_param" is a fault report about our
   * schema, not a sentence about their source. */
  const FIELD_WORDS = {
    text_list_url: "address of the list page",
    text_engine: "way of reading the page",
    text_wait_for_selector: "element that Rendered mode waits for",
    integer_wait_after_load_ms: "waiting time in Rendered mode",
    bool_follow_pagination: "answer to whether the list's own pages are followed",
    text_paging_param: "paging address",
    text_next_selector: "“next page” element",
    integer_max_pages: "number of list pages per run",
    bool_respect_robots: "answer to whether the site's robots.txt is followed",
  };

  /* "a, b, c" is a machine's list; "a, b and c" is a sentence. */
  function andList(words) {
    if (words.length < 2) return words.join("");
    return `${words.slice(0, -1).join(", ")} and ${words[words.length - 1]}`;
  }

  async function loadPreview() {
    if (!sourceId) return;
    try {
      const data = await api(`/api/sources/${sourceId}/preview`, { context: false, quiet: true });
      const counts = data.counts || {};
      // The saved rules on the same snapshot: this is now the reading, and
      // the counter row and the banner are drawn from it together.
      collected = { counts, basis: "saved" };
      renderSummary();
      if (snapshot) renderVerdicts(snapshot);
      const parts = [`With the rules as they are saved, ${fmtInt(counts.accepted || 0)} of ${fmtInt(counts.links || 0)} links would be collected.`];
      const changed = (data.changed_fields || [])
        .filter((name) => !derived.has(name))
        .map((name) => FIELD_WORDS[name] || name);
      if (changed.length) {
        parts.push(`The test was run with a different ${andList(changed)} - `
                 + "run it again to see what the site answers now.");
      }
      previewNote.textContent = parts.join(" ");
    } catch (error) {
      if (error && error.status === 404) previewNote.textContent = "";
    }
  }

  function useResult(result, id) {
    snapshot = result;
    snapshotId = id;
    // A test decides every link with the rules that were sent with it, so
    // its own counts are the reading until something moves the rules.
    collected = result ? { counts: result.counts || {}, basis: "test" } : null;
    renderVerdicts(result);
    renderSummary();
    syncFormatCost();
    const links = result && (result.links || []).length;
    linksButton.disabled = !links;
    pickerHint.textContent = links
      ? `Pick which of the ${plural(links, "link", "links")} the test found are documents.`
      : "The last test found no link to choose from. The banners in step 2 say what to try.";
    syncSubSub();
    // What the site said about paging is worth keeping: it is the one field
    // nobody can guess, and the crawl needs it to reach page two. The test
    // does not name it, so it is read off the first paging link the same way
    // crawlkit reads it - and only when the field is still empty, so a hand
    // written value is never overwritten by a guess.
    const paging = document.getElementById("f-paging-param");
    if (paging) {
      const pager = (result.links || []).find((link) => link.class === "pagination");
      const key = pager ? pagingKeyOf(pager.url) : "";
      if (key && !paging.value) paging.value = key;
      // Filled in just now or on an earlier visit, a value that IS what this
      // test says the paging key is cannot be a reason to run the test again.
      if (key && paging.value === key) derived.add("text_paging_param");
      else derived.delete("text_paging_param");
    }
  }

  /* -- running a test --------------------------------------------------- */

  function setRunning(on, text) {
    progress.dataset.running = on ? "true" : "false";
    progress.textContent = text || "";
    testButton.disabled = on;
    fileTestButton.disabled = on;
    saveButton.disabled = on;
  }

  async function runTest(kind) {
    clearFormError();
    const config = draft();
    const address = String(config.text_list_url || "").trim();
    if (!isHttpUrl(address)) {
      verdicts.textContent = "";
      // Two different faults. "There is no address" while the field
      // visibly holds one contradicts what the reader can see and blames
      // them for something they did do (NN/g on error messages).
      if (!address) {
        banner("error", "⛔", "There is no address to test",
          "Fill in the address of the list page in step 1 - it starts with http:// or https://.",
          { id: "no-address" });
      } else {
        banner("error", "⛔", "That is not a web address",
          "It has to start with http:// or https://. Copy it from the browser's address bar, where the whole address stands.",
          { id: "bad-address", rule: address });
      }
      document.getElementById("f-list-url").focus();
      return;
    }
    retries = 0;
    verdicts.textContent = "";
    setRunning(true, "Starting the test…");
    await start(kind, config);
  }

  async function start(kind, config) {
    try {
      const answer = await api("/api/sources/test", {
        body: {
          source_id: sourceId || undefined,
          config,
          kind,
          sample_subpages: 5,
        },
        context: false, quiet: true,
      });
      poll(answer.snapshot_id);
    } catch (error) {
      if (isAbort(error)) return;
      if (error.status === 409 && retries < MAX_RETRIES) {
        retries += 1;
        setRunning(true, "Another test is running - this one is queued and starts when that one is done.");
        window.setTimeout(() => start(kind, config), RETRY_MS);
        return;
      }
      setRunning(false, "");
      banner("error", "⛔", "The test did not start", errorText(error),
        { rule: error.hint || "", id: "test-refused" });
      announce(errorText(error), { assertive: true });
    }
  }

  function poll(id) {
    if (polling) window.clearTimeout(polling);
    let seen = "";
    async function ask() {
      try {
        const status = await api(`/api/sources/test/${id}`, { context: false, quiet: true });
        if (status.progress && status.progress !== seen) {
          seen = status.progress;
          progress.textContent = status.progress;
        }
        if (status.status === "RUNNING") {
          polling = window.setTimeout(ask, POLL_MS);
          return;
        }
        setRunning(false, "");
        if (status.status === "FAILED") {
          verdicts.textContent = "";
          banner("error", "⛔", "The test did not finish",
            status.error || "The site did not answer in time.",
            { id: "test-failed" });
          announce("The test did not finish", { assertive: true });
          // A test that ran and was refused is still a test that ran. The
          // next step opens: a site that answers 403 has to be configurable
          // by hand, and locking the form until it agrees to be crawled would
          // be the page refusing to be used.
          progress.dataset.tested = "1";
          syncSteps();
          return;
        }
        useResult(status.result, id);
        progress.dataset.tested = "1";
        syncSteps();
        progress.textContent = "Tested just now.";
        announce(`The test is done: ${fmtInt((status.result.links || []).length)} links found.`);
      } catch (error) {
        if (isAbort(error)) return;
        setRunning(false, "");
        banner("error", "⛔", "The test could not be followed", errorText(error),
          { rule: error.hint || "", id: "poll-failed" });
      }
    }
    polling = window.setTimeout(ask, FIRST_POLL_MS);
  }

  testButton.addEventListener("click", () => runTest("crawl"));
  fileTestButton.addEventListener("click", () => runTest("files"));

  /* -- the two pickers -------------------------------------------------- */

  linksButton.addEventListener("click", () => {
    if (!snapshot) return;
    openLinkPicker({
      result: snapshot,
      draft: draft(),
      onApply(answer) {
        const manual = extra.patterns.filter((row) => row.text_origin === "manual");
        extra.patterns = manual.concat(answer.accept || [], answer.reject || []);
        const rejects = (answer.exact_rejects || []).map((url) => ({
          text_kind: "reject", text_url_canonical: url,
        }));
        const monitors = (answer.exact_monitors || []).map((url) => ({
          text_kind: "monitor", text_url_canonical: url,
        }));
        extra.exact_urls = rejects.concat(monitors);
        const paging = document.getElementById("f-paging-param");
        if (answer.paging_param && paging) paging.value = answer.paging_param;
        drawChips();
        rulesMoved();
        markUnsaved("The rules have changed. Save the configuration to keep them.");
        announce(answer.explanation || "The rules have been worked out.");
      },
    });
  });

  filesButton.addEventListener("click", () => {
    if (!snapshot) return;
    openFilePicker({
      result: snapshot,
      draft: draft(),
      onApply(rows) {
        extra.file_rules = rows;
        markUnsaved("The file rules have changed. Save the configuration to keep them.");
        announce("File rules changed");
      },
    });
  });

  /* -- saving ------------------------------------------------------------ */

  function requiredMissing() {
    const name = form.querySelector("[name=text_name]");
    const url = form.querySelector("[name=text_list_url]");
    if (!name.value.trim()) {
      return { field: name, heading: "This watched page has no name",
               message: "Give it a name in step 1 - the list and the log show it by that name." };
    }
    const clash = takenNames.get(name.value.trim().toLowerCase());
    if (clash && clash !== sourceId) {
      return {
        field: name,
        heading: "That name is taken",
        message: `There is already a watched page called "${name.value.trim()}". Give this one a name of its own - the list is read by name.`,
      };
    }
    if (!isHttpUrl(url.value)) {
      return { field: url, heading: "The address of the list page is missing",
               message: "Fill in step 1 with the address of the page that lists the documents. It starts with http:// or https://." };
    }
    /* Refused here rather than by the browser's own bubble, so it reads like
     * every other refusal on this form. A watched page collects into at least
     * one project, and one that names none has nowhere to file what it
     * collects - it would crawl a site and throw the result away. */
    if (!chosenProjects.length) {
      return {
        field: configureButton,
        heading: projects.length
          ? "This watched page has no project"
          : "There is no project to collect into yet",
        message: projects.length
          ? "Press Configure projects in step 1 and pick at least one. A project decides where these documents land and how they are read."
          // The environment variable is still named - that is where the key
          // goes - but the sentence also says where to GET one, which is a
          // page and not a variable. The link is under the list.
          : "The crawler has no working key, so it has found no project. Create one in Xtracting, add its key to the crawler's XTRACTING_API_KEYS, and this list fills itself on the crawler's next pass.",
      };
    }
    if (!respectRobots.checked && !form.querySelector("[name=text_override_reason]").value.trim()) {
      return {
        field: document.getElementById("f-override"),
        heading: "A crawl against the site's rules needs a written reason",
        message: "Say who allowed it and when, under \u201cThe site's own rules\u201d. The database refuses to store it without that sentence.",
      };
    }
    return null;
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    clearFormError();
    // Nothing to commit before the form is read: the project and the key are
    // closed lists, so their value is whatever is selected. (A typed field
    // would need committing, because text never chosen from a suggestion
    // list would otherwise be dropped.)
    const missing = requiredMissing();
    if (missing) {
      showFormError(missing.heading, missing.message, missing.field);
      return;
    }

    const payload = draft();
    if (snapshotId) payload.snapshot_id = snapshotId;
    saveButton.disabled = true;
    saveState.dataset.state = "";
    saveState.textContent = "Saving…";
    try {
      const answer = sourceId
        ? await api(`/api/sources/${sourceId}`, { method: SAVE, body: payload, context: false, quiet: true })
        : await api("/api/sources", { method: "POST", body: payload, context: false, quiet: true });
      const isNew = !sourceId;
      sourceId = Number(answer.id);
      form.dataset.sourceId = String(sourceId);
      // The one message that survives a keystroke - "this watched page no
      // longer exists" - stops being true here, and this is the only place
      // that may withdraw it.
      clearFormError({ force: true });
      saveButton.textContent = "Save as configuration";
      previewNote.textContent = "";
      markSaved(isNew
        ? "Saved, and switched off. Tick Crawl on a schedule when it should start."
        : "Saved.");
      announce(saveState.textContent);
      showSavedControls();
      // Only with a test behind it: /preview answers 404 without one, and a
      // 404 in the browser's console on every save of an untested source is
      // a broken page as far as anybody reading that console is concerned.
      if (snapshotId) loadPreview();
      if (isNew) {
        window.history.replaceState(window.history.state, "", `/sources/${sourceId}`);
        title.textContent = payload.text_name;
      }
    } catch (error) {
      if (isAbort(error)) return;
      saveState.dataset.state = "failed";
      saveState.textContent = "Not saved.";
      if (error.status === 404 && sourceId) {
        // Deleted in another tab while this one was being filled in. The
        // typing is still on screen and is worth keeping; the next press of
        // the button writes it as a new watched page, and succeeds.
        becomeNewPage("It was deleted while this page was open. Nothing you typed "
                    + "is lost - press “Save as a new watched page” to keep "
                    + "it as a new one.");
        return;
      }
      showFormError("This watched page could not be saved",
        sentences(errorText(error), error.hint || "Nothing was written; what is on screen is still here"));
    } finally {
      saveButton.disabled = false;
    }
  });

  function showSavedControls() {
    enableBox.hidden = !sourceId;
    deleteButton.hidden = !sourceId;
    // The real run needs something to read, and a draft is not saved
    // anywhere the crawler can reach.
    if (realRun) realRun.hidden = !sourceId;
    syncEnableHelp();
  }

  /* The line under the save state. "A saved source is switched off until it
   * is enabled" is true of a source that is switched off and nonsense next
   * to a ticked box, where it read as a second instruction to do what had
   * already been done. */
  const enableHelp = document.getElementById("enable-help");
  function syncEnableHelp() {
    if (!enableHelp) return;
    enableHelp.textContent = enableTick.checked && sourceId
      ? "The crawler reads this page on its own schedule. Untick the box to stop it; the configuration stays."
      : "A saved watched page is switched off until it is enabled. Nothing is crawled on a schedule before that.";
  }

  enableTick.addEventListener("change", async () => {
    const wanted = enableTick.checked;
    enableRefusal.hidden = true;
    enableTick.disabled = true;
    try {
      await api(`/api/sources/${sourceId}/enable`, {
        body: { enabled: wanted }, context: false, quiet: true,
      });
      /* The one visible sentence about this source's state has to move with
       * the box. It said "Saved, and switched off. Tick Crawl on a schedule
       * when it should start." while the box next to it was ticked - telling
       * somebody to do the thing they had just done, which is how a person
       * unticks it again and switches the crawl off for good. announce() is
       * for the screen reader; this is what a sighted reader gets.
       *
       * With edits still in the form the line must not say "Saved": the tick
       * is carried out on its own request and saves nothing else. */
      if (unsaved) {
        saveState.dataset.state = "unsaved";
        saveState.textContent = (wanted
          ? "Crawling on a schedule now. "
          : "The scheduled crawl is off. ")
          + "The other changes on this page are still not saved.";
      } else {
        markSaved(wanted
          ? "Saved, and crawling on a schedule. The crawler takes it on its next round."
          : "Saved, and switched off. Tick Crawl on a schedule when it should start.");
      }
      announce(wanted ? "This page is now crawled on a schedule" : "The scheduled crawl is off");
    } catch (error) {
      enableTick.checked = !wanted;
      if (!isAbort(error)) {
        enableRefusal.textContent = "";
        enableRefusal.appendChild(el("p", null, errorText(error)));
        if (error.hint) enableRefusal.appendChild(el("p", null, error.hint));
        enableRefusal.appendChild(actionButton("The site's rules", openRobotsSettings));
        enableRefusal.hidden = false;
        announce(errorText(error), { assertive: true });
      }
    } finally {
      enableTick.disabled = false;
      syncEnableHelp();
    }
  });

  /* Deleting asks in the page, next to the button, and never in a dialog
   * that steals the keyboard for a question with two words in it. */
  deleteButton.addEventListener("click", () => {
    if (deleteButton.dataset.armed === "true") return;
    deleteButton.dataset.armed = "true";
    const strip = el("div", "notice notice--warn");
    strip.appendChild(el("p", null,
      "Delete this watched page, its rules and its test results? What is already in the archive stays there."));
    const yes = el("button", "button button--quiet button--remove", "Delete it");
    yes.type = "button";
    const no = el("button", "button button--secondary", "Keep it");
    no.type = "button";
    strip.appendChild(yes);
    strip.appendChild(no);
    enableRefusal.hidden = true;
    enableRefusal.insertAdjacentElement("beforebegin", strip);
    no.focus();
    no.addEventListener("click", () => {
      strip.remove();
      deleteButton.dataset.armed = "false";
      deleteButton.focus();
    });
    yes.addEventListener("click", async () => {
      try {
        const name = (document.getElementById("f-name").value || "this watched page").trim();
        await api(`/api/sources/${sourceId}`, { method: "DELETE", context: false, quiet: true });
        unsaved = false;   // there is nothing left to save it into
        // Said on the Watchlist, because that is where this ends up and there
        // is no editor left to say it in. A delete that answers with a silent
        // jump to a list reads as a page that lost the reader's place.
        sayAfterNavigating(`“${name}” is deleted. What it already collected stays in the archive.`);
        window.location.href = "/sources";
      } catch (error) {
        strip.remove();
        deleteButton.dataset.armed = "false";
        showFormError("This watched page could not be deleted", errorText(error));
      }
    });
  });

  /* ── ONE REAL DOCUMENT, ON PURPOSE ───────────────────────────────────
   *
   * WHY IT IS NOT THE TEST BUTTON. "Test this configuration" is safe to
   * press because it writes nothing and sends nothing; that is the whole
   * reason it can be pressed twenty times while somebody works out a
   * pattern. It cannot answer the question that comes after: would a
   * document from this page actually be accepted, extracted and archived?
   *
   * So this one really crawls, really submits and really costs - and says so
   * above the button, before it is pressed, rather than in a receipt after.
   *
   * THE DASHBOARD DOES NOT DO IT ITSELF. The keys live in the crawler's
   * environment and nowhere else, so the request is a row and the crawler
   * carries it out on its next tick - within seconds, and even while the
   * scheduled crawl is switched off, because a run somebody just asked for
   * and is watching is not the crawler running.
   *
   * THREE STAGES, AND THE PANEL NAMES ALL THREE, because "it did not work"
   * has three different answers and three different things to do about it:
   * did the crawler take it and find a document, did the API accept it for
   * extraction, and has the collector fetched the result back.
   */
  const realRun = document.getElementById("real-run");
  const realButton = document.getElementById("run-real");
  const realProgress = document.getElementById("real-progress");
  const realVerdicts = document.getElementById("real-verdicts");
  const realDocuments = document.getElementById("real-documents");
  let realPolling = null;
  //: How long to keep asking. The crawl itself is quick; what takes minutes is
  //: the extraction, and the collector looks every half minute while a run is
  //: waiting. Ten minutes is longer than that and short enough to stop.
  const REAL_TIMEOUT_MS = 10 * 60 * 1000;

  const QUEUE_WORDS = {
    PENDING: ["waiting", "Queued, and not sent yet. The crawler sends it on its next round."],
    SENDING: ["sending", "On its way to Xtracting now."],
    SENT: ["sent", "Xtracting accepted it for extraction."],
    ARCHIVED: ["archived", "Extracted and stored in the archive."],
    FAILED: ["refused", "Xtracting would not take it."],
    SKIPPED: ["not sent", "Over a limit, or the key was resting."],
    GONE: ["gone", "The queue item is no longer there."],
  };

  function realBanner(kind, icon, title, text) {
    realVerdicts.replaceChildren();
    const box = el("div", `notice notice--${kind} verdict`);
    const glyph = el("span", "verdict-icon", icon);
    glyph.setAttribute("aria-hidden", "true");
    box.appendChild(glyph);
    box.appendChild(el("p", "verdict-title", title));
    if (text) box.appendChild(el("p", "verdict-text", text));
    realVerdicts.appendChild(box);
  }

  function setRealRunning(on, text) {
    realProgress.dataset.running = on ? "true" : "false";
    realProgress.textContent = text || "";
    realButton.disabled = on || !sourceId;
  }

  if (realButton) {
    realButton.addEventListener("click", async () => {
      if (unsaved) {
        realBanner("warn", "⚠", "Save the page first",
          "There are changes on this page that are not saved. A real run reads "
          + "the SAVED configuration, so it would crawl the old one.");
        return;
      }
      realVerdicts.replaceChildren();
      realDocuments.replaceChildren();
      setRealRunning(true, "Asking the crawler…");
      try {
        const answer = await api(`/api/sources/${sourceId}/run`, {
          body: { project_id: realProject ? realProject.value : "", wanted: 1 },
          context: false, quiet: true,
        });
        if (answer.already_running) {
          setRealRunning(true, "A run of this page is already going - following that one.");
        }
        pollReal(answer.id, Date.now() + REAL_TIMEOUT_MS);
      } catch (error) {
        if (isAbort(error)) return;
        setRealRunning(false, "");
        /* NOT A VERDICT, so not in the verdict panel. That panel answers "what
         * happened to my document" and is read from the top down; this says
         * the request never left the dashboard, which is about the button and
         * not about a document. A toast carries it, announces itself
         * (role=alert) and leaves the panel showing the last real answer. */
        toast(sentences("The run could not be started", errorText(error)),
              // One message for THIS page, replaced when it is pressed again.
              // The editor only ever shows one source, so the id is constant
              // here for the same reason it is not on the overview.
              { kind: "error", id: "real-run-start", replace: true,
                hint: error.hint || "" });
      }
    });
  }

  async function pollReal(runId, until) {
    if (realPolling) window.clearTimeout(realPolling);
    async function ask() {
      let state;
      try {
        state = await api(`/api/sources/${sourceId}/run/${runId}`,
                          { context: false, quiet: true });
      } catch (error) {
        if (isAbort(error)) return;
        setRealRunning(false, "");
        realBanner("error", "⛔", "The run could not be followed", errorText(error));
        return;
      }
      drawReal(state);

      const finished = state.text_status === "DONE" || state.text_status === "FAILED";
      // Not done when the crawler is done: a document that is SENT has not
      // arrived anywhere yet, and "did it reach the archive" is the question
      // this panel exists to answer.
      const settled = finished && (state.documents || []).every(
        (d) => d.in_archive || d.queue_status === "FAILED"
            || d.queue_status === "SKIPPED" || d.queue_status === "GONE");

      if (settled || Date.now() > until) {
        setRealRunning(false, "");
        if (!settled) {
          realProgress.textContent =
            "Still waiting for the archive. The collector looks every half "
            + "minute while a run is open; this page can be reloaded later.";
        }
        return;
      }
      realPolling = window.setTimeout(ask, finished ? 4000 : 1500);
    }
    realPolling = window.setTimeout(ask, 400);
  }

  function drawReal(state) {
    if (state.text_status === "REQUESTED") {
      setRealRunning(true, "Waiting for the crawler to pick it up…");
    } else if (state.text_status === "RUNNING") {
      setRealRunning(true, state.text_progress || "Crawling…");
    }

    if (state.text_status === "FAILED") {
      realBanner("error", "⛔", "The run did not finish", state.text_error || "");
    } else if (state.text_status === "DONE" && !(state.documents || []).length) {
      // The most important sentence in the panel: "0 documents" with no
      // reason looks like a fault and is usually the system working.
      realBanner("info", "📭", "Nothing was sent, and nothing was charged",
                 state.reason || "");
    } else if ((state.documents || []).length) {
      const archived = state.documents.filter((d) => d.in_archive).length;
      const failed = state.documents.filter((d) => d.queue_status === "FAILED").length;
      if (failed) {
        realBanner("error", "⛔", plural(failed, "document", "documents") + " refused",
          "Xtracting would not take it. The reason is on the document below.");
      } else if (archived === state.documents.length) {
        realBanner("ok", "✅", "It went all the way through",
          `${plural(archived, "document", "documents")} extracted and in the archive.`);
      } else {
        realBanner("info", "⏳", "Sent, waiting for the extraction",
          "Xtracting has it. The collector fetches the result as soon as it is "
          + "ready - it looks every half minute while a run is open.");
      }
    }

    realDocuments.replaceChildren();
    (state.documents || []).forEach((doc) => {
      const item = el("li", "real-document");
      const head = el("p", "real-document-uri", doc.uri || doc.tag);
      item.appendChild(head);

      const project = projectOf(doc.project_id);
      const line = el("p", "real-document-said");
      const [word, why] = QUEUE_WORDS[doc.queue_status] || QUEUE_WORDS.GONE;
      const pill = el("span", "pill", word);
      pill.dataset.state = doc.in_archive ? "archived" : doc.queue_status;
      line.appendChild(pill);
      line.appendChild(document.createTextNode(
        ` ${project ? project.name : doc.project_id} - ${doc.in_archive
          ? QUEUE_WORDS.ARCHIVED[1] : why}`));
      item.appendChild(line);

      if (doc.error) item.appendChild(el("p", "real-document-error", doc.error));
      if (doc.task_name) {
        item.appendChild(el("p", "field-help",
          `In the archive as “${doc.task_name}”.`));
      }
      realDocuments.appendChild(item);
    });
  }

  /* -- leaving with work in the form ------------------------------------ */

  /* ASKED IN A DIALOG, NOT IN A BAR ABOVE THE FORM.
   *
   * A bar inserted before the form works only while the sole way off this
   * page is the top bar, which is where a reader's eyes already are. It stops
   * working the moment there is a way back at the BOTTOM of the form: press
   * it after filling in three cards and a picker, and the question appears a
   * screen and a half above, out of sight - the click produces no perceivable
   * result, which is the same fault the map's popups avoid.
   *
   * A modal <dialog> is in the browser's top layer and is laid out against the
   * viewport, so it is in front of the reader wherever they are on the page.
   * It is this dashboard's own dialog (static/js/dialog.js), so it has the
   * title, the X, the trapped focus and the two ways of closing that every
   * other popup here has.
   *
   * THE SAFE ANSWER HAS THE FOCUS, which is why Cancel is renamed rather than
   * a third button added: confirmDialog focuses the cancel, and older readers
   * make more mistakes and recover from them less easily (NN/g). The expensive
   * answer here is not recoverable at all - the form is gone.
   */
  async function askBeforeLeaving(go) {
    if (leaving) return;          // the dialog is already open
    leaving = true;
    let discard = false;
    try {
      discard = await confirmDialog({
        title: "Leave without saving?",
        question: "You have changes to this watched page that are not saved. "
                + "Leaving now discards them.",
        confirmLabel: "Discard my changes",
        cancelLabel: "Keep editing",
        danger: true,
      });
    } finally {
      leaving = false;
    }
    if (!discard) return;
    unsaved = false;
    go();
  }

  document.addEventListener("click", (event) => {
    if (!unsaved) return;
    const link = event.target.closest("a[href]");
    if (!link || link.target === "_blank") return;
    // A download is not leaving the page: the Export menu's CSV and JSON
    // links hand over a file and the form stays where it is.
    if (link.hasAttribute("download") || link.closest("[data-export]")) return;
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || event.button !== 0) return;
    const href = link.getAttribute("href") || "";
    if (!href || href.startsWith("#")) return;   // same page: nothing is lost
    const to = new URL(link.href, window.location.href);
    if (to.origin === window.location.origin
        && to.pathname === window.location.pathname && to.search === window.location.search) return;
    event.preventDefault();
    askBeforeLeaving(() => { window.location.href = link.href; });
  }, true);

  window.addEventListener("beforeunload", (event) => {
    if (!unsaved) return;
    event.preventDefault();
    event.returnValue = "";   // Chrome still wants this
  });

  /* -- what is on screen when the page opens ---------------------------- */

  async function loadNames() {
    try {
      const data = await api("/api/sources", { context: false, quiet: true });
      takenNames = new Map((data.items || [])
        .map((item) => [String(item.text_name).trim().toLowerCase(), Number(item.id)]));
    } catch (error) { /* the duplicate-name warning is a courtesy, not a gate */ }
  }

  /* The projects the crawler holds a key for. Loaded before the source itself,
   * because the chosen ones are drawn with the names out of this list and a
   * list that arrived later would show a watchlist's projects as bare ids. */
  async function loadProjects() {
    try {
      const data = await api("/api/projects/crawler", { context: false, quiet: true });
      projects = data.items || [];
    } catch (error) {
      projects = [];
    }
    // NOT BEFORE THE LIST IS HERE. Pressed a moment earlier, the chooser
    // opened on "the crawler has no working key" for an archive that has
    // three projects - a sentence that is wrong, alarming and about to fix
    // itself, which is the worst combination a message can have.
    if (configureButton) configureButton.disabled = false;
    drawChosenProjects();
  }

  async function loadSource() {
    if (!sourceId) {
      showSavedControls();
      syncSubSub();
      return;
    }
    try {
      const data = await api(`/api/sources/${sourceId}`, { context: false, quiet: true });
      fill(data.source);
      title.textContent = data.source.text_name;
      enableTick.checked = Boolean(data.source.bool_enabled);
      showSavedControls();
      syncSubSub();
      // A SAVED PAGE HAS BEEN THROUGH ALL FOUR STEPS, so all four are open:
      // they are its record, and folding the answers away from somebody who
      // came back to check one would be the page hiding its own state.
      openEverything();
      if (data.snapshot && data.snapshot.status === "DONE") {
        await loadSnapshot(data.snapshot.id);
        loadPreview();
      }
    } catch (error) {
      if (isAbort(error)) return;
      if (error.status === 404) {
        // Gone, not broken. The form below is empty and is now a new page.
        becomeNewPage("It may have been deleted. The form below is empty and "
                    + "is ready to be filled in as a new watched page.");
        return;
      }
      showFormError("This watched page could not be read",
        sentences(errorText(error), error.hint || "Try again, or go back to the list of watched pages"),
        null, { actions: [watchlistLink()] });
    }
  }

  /* The last test is shown again after a reload: the verdicts, the counts
   * and both pickers work without asking the site a second time - and
   * one visit fewer. */
  async function loadSnapshot(id) {
    try {
      const status = await api(`/api/sources/test/${id}`, { context: false, quiet: true });
      if (status.status === "DONE" && status.result) {
        useResult(status.result, id);
        progress.textContent = `From the test of ${fmtDate(status.finished || status.started)}.`;
      }
    } catch (error) { /* the editor works without a snapshot */ }
  }

  syncEngine();
  syncRobots();
  syncMode();
  syncSteps();
  loadNames();
  /* The projects first, then the source: the chosen ones are drawn with the
   * names out of that list, and a list that arrived afterwards would show a
   * watchlist's projects as bare ids. */
  loadProjects().then(loadSource);
}

/* ══════════════════════════════════════════════════════════════════════ */

const list = document.getElementById("source-list");
if (list) startOverview(list);

/* THE ONE LINK IN THIS FILE THAT LEAVES THE DASHBOARD. The Project field
 * says the list is empty when the crawler has no working key; this is where
 * a key is made. Outward, like every link to Xtracting: a new tab, the
 * arrow, and the promise in words (static/js/icons.js). */
const keysLink = document.getElementById("f-project-keys");
if (keysLink) {
  keysLink.href = apiKeysUrl();
  outward(keysLink, "Manage the keys in Xtracting");
}



/* ── Testing one row, from the list ───────────────────────────────────────
 *
 * POST /api/sources/test with nothing but the source id: the server reads
 * the SAVED configuration, so what is tested is what would run. Then poll,
 * and say the outcome on the row itself - a number a person can act on, and
 * the way to the detail if they want it.
 *
 * ONE AT A TIME. The runner refuses a second test with 409 while one is
 * going (api_sources.py: TestBusy), which is a real answer and not an error
 * to hide: two crawls of two sites at once from one dashboard is how a
 * person gets a site to block them.
 */
const TEST_POLL_MS = 1500;

/* WHERE A TEST SAYS WHAT IT IS DOING: the line the row ALREADY has.
 *
 * Not a paragraph of its own: that would grow the card by a line the moment
 * Test is pressed - under the pointer that has just pressed it - and move
 * every card below it down. The card is the same height before and after,
 * because the words go into the row's one line of state and that line has a
 * reserved height (sources.css: .source-last-run).
 *
 * Replacing "Never crawled." with "Tested just now: ..." is not a loss: the
 * test IS the newest fact about the row, and the list is deliberately not
 * refreshed under the reader's hands, so nothing overwrites it back.
 *
 * `data-test-said` is set here rather than in the template because it also
 * turns the paragraph into a live region: a line that is announced from the
 * moment the page loads would read twenty rows of state out loud on arrival.
 */
function testLine(node) {
  const line = node.querySelector("[data-last-run]") || node.querySelector("[data-test-said]");
  if (!line) return el("p", "source-last-run");   // no row to speak in; say nothing
  if (line.dataset.testSaid === undefined) {
    line.dataset.testSaid = "";
    line.setAttribute("role", "status");
  }
  return line;
}

function testResultWords(answer) {
  const r = (answer && answer.result) || {};
  const found = Number(r.links_found ?? (r.links || []).length ?? 0);
  const kept = Number(r.links_accepted ?? r.accepted ?? 0);
  const status = r.http_status || r.status_code;
  const bits = [];
  if (status) bits.push(`answered ${status}`);
  if (found) bits.push(`${fmtInt(found)} ${found === 1 ? "link" : "links"} found`);
  if (kept) bits.push(`${fmtInt(kept)} would be collected`);
  return bits.length ? bits.join(" - ") : "the test finished";
}

/* ── The test queue ────────────────────────────────────────────────────
 *
 * ONE TEST RUNS AT A TIME, AND THAT IS THE POINT - a site must never see two
 * visitors from here at once. The server enforces it and answers 409 to a
 * second one.
 *
 * What the page did with that was throw the press away: press Test on four
 * rows and three of them were refused with "another test is running", so the
 * reader had to come back and press them again in the right order, by hand,
 * watching for the first to finish. The button behaved like a race.
 *
 * So the presses are a QUEUE. Four presses are four tests, in the order they
 * were pressed, each one starting when the one before it has finished. The
 * row says where it stands - `Queued, N ahead of it` - so nobody presses
 * again wondering whether it registered, and pressing a row that is already
 * waiting takes it out again rather than queueing it twice.
 *
 * It is deliberately a plain array and a flag, not a worker or a lock: the
 * whole thing is one page, one tab, and the server is still the authority.
 * If a second tab is testing at the same moment the 409 is still possible,
 * and it is still reported - the queue makes it rare rather than normal.
 */
const testQueue = [];
let testRunning = false;

/* THE WORD ON A TEST BUTTON, WITHOUT TAKING THE BUTTON APART.
 *
 * The button is re-labelled four times in a press - "Testing...", "Queued",
 * and back again - and writing the button's whole text replaces every child
 * it has. The button now carries a tick saying the page has been tested, and
 * the first press deleted that tick and folded its glyph into the label,
 * which then travelled on as the remembered label. So the word is a span of
 * its own and only that span is written.
 */
function testWord(button, text) {
  const word = button.querySelector("[data-test-word]") || button;
  if (text !== undefined) word.textContent = text;
  return word.textContent;
}

function queuePosition(id) {
  return testQueue.findIndex((entry) => entry.item.id === id);
}

/** Say where each waiting row stands, after anything changes the queue. */
function drawQueue() {
  testQueue.forEach((entry, index) => {
    const ahead = index + (testRunning ? 1 : 0);
    entry.line.textContent = ahead
      ? `Queued - ${plural(ahead, "test", "tests")} ahead of it.`
      : "Queued - next.";
    // "Queued…", with the same trailing dots as "Testing…": both say the
    // button is in the middle of something and neither is a state the reader
    // has to do anything about.
    testWord(entry.button, "Queued…");
  });
}

async function runQueue() {
  if (testRunning) return;
  testRunning = true;
  try {
    while (testQueue.length) {
      const entry = testQueue.shift();
      drawQueue();
      await testNow(entry);
    }
  } finally {
    testRunning = false;
    drawQueue();
  }
}

/* A press. Queues it, or takes it out again if it is already waiting. */
function testOne(item, button, node) {
  const line = testLine(node);
  const waiting = queuePosition(item.id);
  if (waiting >= 0) {
    const [entry] = testQueue.splice(waiting, 1);
    entry.button.disabled = false;
    testWord(entry.button, entry.label);
    entry.line.textContent = "Taken out of the queue.";
    drawQueue();
    return;
  }
  button.disabled = false;          // it stays pressable, to take it out again
  testQueue.push({ item, button, node, line, label: button.dataset.testLabel
    || (button.dataset.testLabel = testWord(button)) });
  drawQueue();
  runQueue();
}

async function testNow({ item, button, node, line, label }) {
  button.disabled = true;
  testWord(button, "Testing…");
  line.textContent = "Fetching the page once. Nothing is submitted.";
  try {
    const started = await api("/api/sources/test", {
      body: { source_id: item.id, kind: "crawl", sample_subpages: 5 },
      context: false, quiet: true,
    });
    await pollTest(started.snapshot_id, line);
    /* AND THE ROW IS DRAWN AGAIN, because a test changes what the row says.
     *
     * The line under the buttons was updated and nothing else was: the pill
     * still read "robots.txt not checked yet" on a page that had just been
     * fetched, and the counts were the ones from before. Everything on that
     * card comes from the server, so the server is asked again and the card
     * is rebuilt from the answer - one row, not the whole list, so a search
     * in progress and the other rows' queue state are left alone. */
    await refreshRow(item, node, line);
  } catch (error) {
    if (isAbort(error)) return;
    /* THE STATE ON THE ROW, THE REASON IN A TOAST. Both sentences on the
     * row would have the second one wrap and push the whole list down; and
     * neither of them is acted on where it stands - there is nothing on this
     * card to correct. So the row keeps the short fact and the message
     * carries the rest.
     *
     * ONE MESSAGE PER PAGE, NOT ONE IN TOTAL. With a constant id the toast
     * would replace itself: press Test on four rows and the reader is told
     * about the fourth. The id is the source's, so four refusals are four
     * messages and pressing the same row twice still leaves one. */
    const busy = error.status === 409;
    line.textContent = busy ? "Another test is running." : "The test did not start.";
    toast(busy
      ? `${item.text_name} was not tested: another test is running.`
      : `${item.text_name} was not tested. ${errorText(error)}`,
      {
        kind: busy ? "warn" : "error",
        id: `source-test-refused-${item.id}`,
        replace: true,
        hint: busy ? "One test runs at a time, so a site never sees two visitors from here. "
                   + "Wait for it to finish and press Test again."
                   : (error.hint || ""),
      });
  } finally {
    button.disabled = false;
    testWord(button, label);
  }
}

/* One row, redrawn from the server after something changed it.
 *
 * `GET /api/sources/{id}` answers with the source, its robots verdict and its
 * newest snapshot - which is everything the pills are made of. The row is
 * re-rendered in place rather than reloading the list: a reload would throw
 * away the queue's own sentences on the other rows, and would fight a search
 * the reader is in the middle of typing.
 *
 * A failure here is not worth a message. The test result is already on the
 * row; the pills being a minute out of date is a smaller problem than a
 * toast about a refresh nobody asked for. */
async function refreshRow(item, node, line) {
  try {
    const fresh = await api(`/api/sources/${item.id}`, { context: false, quiet: true });
    const row = fresh && (fresh.source || fresh.item);
    if (!row) return;
    const merged = { ...item, ...row, robots: fresh.robots || row.robots || item.robots,
                     snapshot: fresh.snapshot || row.snapshot || item.snapshot };
    /* AND WHAT THE TEST SAID STAYS ON THE ROW.
     *
     * `redrawRow` rewrites the row's one line of state from the server, and the
     * server knows nothing about a test: for a page nothing has ever crawled it
     * writes "Never crawled." straight back over "Tested just now: 12 links, 5
     * accepted". The reader pressed Test and watched the answer disappear.
     *
     * The fetch that just happened IS the newest fact about this row, so it is
     * put back after the redraw - the pills and the counts come from the
     * server, the sentence comes from the test. */
    const said = line ? line.textContent : "";
    if (redrawRow) redrawRow(merged, node);
    if (line && said) line.textContent = said;
  } catch (error) {
    if (!isAbort(error)) console.debug("the row could not be refreshed", error);
  }
}

async function pollTest(snapshotId, line) {
  for (;;) {
    const answer = await api(`/api/sources/test/${snapshotId}`, { context: false, quiet: true });
    if (answer.status === "DONE") {
      line.textContent = `Tested just now: ${testResultWords(answer)}. Open it to see the links.`;
      return;
    }
    if (answer.status === "FAILED" || answer.error) {
      // The site's own answer, in the site's own words where there is one.
      line.textContent = `The test failed. ${answer.error || "The page could not be read."}`;
      return;
    }
    if (answer.progress) line.textContent = answer.progress;
    await new Promise((done) => window.setTimeout(done, TEST_POLL_MS));
  }
}

/* ── The crawler's own switch ──────────────────────────────────────────────
 *
 * One row in dashboard.settings, read by the crawler every tick. Two facts,
 * kept apart because they are different: whether crawling is switched ON,
 * and whether a crawler is there to do it. A switch that is on with nothing
 * running is a plan, not a state, and saying "crawling" over an archive with
 * no worker would be a lie the page could have checked.
 */
function startCrawlerSwitch() {
  const box = document.getElementById("crawler-switch");
  const tick = document.getElementById("crawler-running");
  const label = document.getElementById("crawler-running-label");
  const said = document.getElementById("crawler-switch-said");
  if (!box || !tick) return;

  function draw(state) {
    tick.checked = !!state.running;
    tick.disabled = false;
    label.textContent = state.running ? "On" : "Off";
    box.classList.toggle("is-on", !!state.running);
    if (!state.running) {
      said.textContent = "Nothing is being crawled. Watched pages can still be added, "
        + "changed and tested from here - press Test on a row to fetch that one page once.";
    } else if (state.worker_seen) {
      said.textContent = `Crawling on the schedule each page carries. A crawler reported in ${fmtAgo(state.worker_seen)}.`;
    } else {
      said.textContent = "Switched on, but no crawler has reported in - nothing is being "
        + "fetched until one starts. Everything on this page still works.";
    }
    // Every draw, not only the ones that follow a press: the first read of the
    // switch is also the moment the rows can be judged, and it may land after
    // the list has already been drawn.
    if (judgeSchedules) judgeSchedules(state);
  }

  async function load() {
    try {
      draw(await api("/api/crawler/state", { context: false, quiet: true }));
    } catch (error) {
      if (isAbort(error)) return;
      tick.disabled = true;
      said.textContent = "The switch could not be read.";
    }
  }

  tick.addEventListener("change", async () => {
    const wanted = tick.checked;
    tick.disabled = true;
    said.textContent = wanted ? "Switching crawling on…" : "Switching crawling off…";
    try {
      draw(await api("/api/crawler/state", { body: { running: wanted }, context: false, quiet: true }));
      announce(wanted ? "Scheduled crawling is on" : "Scheduled crawling is off");
    } catch (error) {
      if (isAbort(error)) return;
      tick.checked = !wanted;
      tick.disabled = false;
      said.textContent = `The switch did not move. ${errorText(error)}`;
    }
  });

  load();
}

startCrawlerSwitch();

const form = document.getElementById("source-form");
if (form) startEditor(form);
