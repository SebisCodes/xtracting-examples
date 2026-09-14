/* ==========================================================================
 *  The top bar, on every page.
 *
 *    - project + language selector: filled from /api/projects, which also
 *      says which pair the server resolved for this request. Choosing
 *      writes the cookies xd.project / xd.language (so the next page,
 *      reached by any link, keeps the pair) AND the URL (so this page can
 *      be bookmarked), then reloads: the pages are rendered server-side
 *      with the pair in them, and a reload is the one way to be sure every
 *      part of the page agrees.
 *
 *    - perspective + minimum importance: which reader's judgement of a
 *      document, and how important it has to be, for Query, Events,
 *      Diagrams, Graph, Map and Heatmap. Server-rendered from the
 *      perspectives the data holds; the same cookie-plus-URL-plus-reload
 *      behaviour as the pair, for the same reason.
 *
 *    - nav links carry the pair and the filter as well, so a link copied
 *      from the address bar after clicking through the views still says
 *      what it shows.
 *
 *    - Print and Export live in export.js. It is imported here
 *      dynamically: when it is missing, Print still prints and the CSV/JSON
 *      links still work - only the PDF button says so.
 *
 *    - the info bubbles beside form labels (hints.js). Started from here so
 *      that every page has them without every page importing them, and so a
 *      bubble inside a dialog that is built later works like one in the form.
 *
 *    - the two service pills on the heading line (status.js). Here for the
 *      same reason: the question "is anything running" is asked from
 *      whichever page the reader happens to be on, so it is answered on all
 *      of them.
 *
 *  Loaded as a module from _layout.html; nothing here depends on a view.
 * ========================================================================== */

import { startHints } from "./hints.js";
import { startServiceStatus } from "./status.js";

import { api, isAbort } from "./api.js";
import { state, set as setState, read as readState } from "./state.js";
import { announce, toast } from "./a11y.js";

const COOKIE_PROJECT = "xd.project";
const COOKIE_LANGUAGE = "xd.language";
/* The perspective filter survives from one view to the next exactly as the
 * pair does, and for the same reason: it is what the reader is looking at,
 * not what they last searched for. app/context.py reads these four. */
const COOKIE_PERSPECTIVE = "xd.perspective";
const COOKIE_MIN_IMPORTANCE = "xd.min_importance";
const COOKIE_MAX_AGE = 60 * 60 * 24 * 365;

function writeCookie(name, value) {
  const v = encodeURIComponent(value || "");
  // SameSite=Lax and no Secure flag: the dashboard runs on plain http behind
  // a reverse proxy on many customer networks, and a Secure cookie would
  // silently never be stored there.
  document.cookie = `${name}=${v}; Path=/; Max-Age=${COOKIE_MAX_AGE}; SameSite=Lax`;
}

function option(value, text) {
  const o = document.createElement("option");
  o.value = value;
  o.textContent = text;
  return o;
}

/* Fill the two selects from the pairs. Projects are unique names; the
 * language list follows the chosen project. */
function fillSelector(pairs, current) {
  const projectSel = document.getElementById("ctx-project");
  const languageSel = document.getElementById("ctx-language");
  if (!projectSel || !languageSel) return;

  const projects = [];
  pairs.forEach((p) => { if (!projects.includes(p.project)) projects.push(p.project); });

  projectSel.textContent = "";
  if (!projects.length) {
    projectSel.appendChild(option("", "No projects in the archive yet"));
    projectSel.disabled = true;
    languageSel.textContent = "";
    languageSel.appendChild(option("", "-"));
    languageSel.disabled = true;
    return;
  }
  projectSel.disabled = false;
  languageSel.disabled = false;
  projects.forEach((name) => {
    const tasks = pairs.filter((p) => p.project === name).reduce((n, p) => n + (p.tasks || 0), 0);
    projectSel.appendChild(option(name, tasks ? `${name} (${tasks.toLocaleString()} tasks)` : name));
  });
  projectSel.value = projects.includes(current.project) ? current.project : projects[0];

  const fillLanguages = () => {
    languageSel.textContent = "";
    const langs = pairs.filter((p) => p.project === projectSel.value).map((p) => p.language);
    langs.forEach((l) => languageSel.appendChild(option(l, l)));
    languageSel.value = langs.includes(current.language) ? current.language : (langs[0] || "");
  };
  fillLanguages();

  const apply = () => {
    const project = projectSel.value;
    const language = languageSel.value;
    writeCookie(COOKIE_PROJECT, project);
    writeCookie(COOKIE_LANGUAGE, language);
    setState({ project, language, page: 0 });
    announce(`Switching to ${project}, ${language}`);
    // A full reload, on purpose - see the header comment.
    window.location.assign(window.location.href);
  };
  projectSel.addEventListener("change", () => { fillLanguages(); apply(); });
  languageSel.addEventListener("change", apply);
}

/* ── Perspective and minimum importance ────────────────────────────────────
 *
 * The two selects are already filled by the server (_layout.html) from the
 * perspectives this project and language actually hold, so this only binds
 * the behaviour: greying "Show only" out while Perspective is All, and
 * reloading on a change.
 *
 * A RELOAD, LIKE PROJECT AND LANGUAGE, and for the reason in this file's
 * header: the pages are rendered server-side with the choice in them, and
 * the choice narrows six views at once. Re-fetching each view's data in
 * place would leave the heading, the counts and the sentence under them
 * describing the wider set for as long as the requests took.
 *
 * SHOW ONLY IS DISABLED, NOT HIDDEN, WHILE THE PERSPECTIVE IS ALL. With no
 * perspective there is nothing to rank documents by, so the control has no
 * meaning - but a control that vanishes and comes back moves everything
 * beside it, and a reader cannot learn that the second half exists. It stays
 * where it is, greyed, with ctx-filter-help saying why: a disabled control
 * with no reason beside it is a dead end.
 */
function initPerspective() {
  const perspectiveSel = document.getElementById("ctx-perspective");
  const importanceSel = document.getElementById("ctx-importance");
  if (!perspectiveSel || !importanceSel) return;

  // THE SERVER'S RESOLVED CHOICE IS THE TRUTH FOR THIS PAGE. It may have
  // come from a cookie rather than from the URL, and until it is in the
  // state every nav link, API call and Export URL would say "no filter"
  // while the page shows a filtered view. Mirrored from the body's data
  // attributes, which is where the render put it.
  const body = document.body;
  const shown = body.getAttribute("data-perspective") || "";
  const shownLevel = body.getAttribute("data-min-importance") || "";
  if (shown !== state.perspective || shownLevel !== state.min_importance) {
    setState({ perspective: shown, min_importance: shownLevel });
  }
  // The cookies too, and even when the URL already agreed - the same rule
  // loadProjects() follows for the pair. A link that names a perspective
  // must BECOME the perspective the next click uses, or a reader who
  // followed one and then pressed Events would land back on everything.
  // Writing what the server resolved also clears a cookie naming a
  // perspective this project does not have: the server dropped it, and the
  // cookie should stop offering it.
  writeCookie(COOKIE_PERSPECTIVE, shown);
  writeCookie(COOKIE_MIN_IMPORTANCE, shownLevel);
  decorateNav();

  const apply = () => {
    const perspective = perspectiveSel.value;
    // Choosing a perspective for the first time starts at whatever the
    // server rendered as selected, which is "Low Importance and above" -
    // the bottom of the scale is every judged document, a filter that looks
    // active and does nothing.
    const level = perspective ? importanceSel.value : "";
    writeCookie(COOKIE_PERSPECTIVE, perspective);
    writeCookie(COOKIE_MIN_IMPORTANCE, level);
    setState({ perspective, min_importance: level, page: 0 });
    announce(perspective
      ? `Showing ${level} and above for ${perspective}`
      : "Showing every perspective");
    // A full reload, on purpose - see the block comment above.
    window.location.assign(window.location.href);
  };

  perspectiveSel.addEventListener("change", () => {
    importanceSel.disabled = !perspectiveSel.value;
    apply();
  });
  importanceSel.addEventListener("change", () => {
    if (perspectiveSel.value) apply();
  });
}

/* Nav links: add ?project=&language= (and the perspective filter, when one
 * is on) so the URL keeps saying what it shows. All four travel together -
 * a link that carried the pair and dropped the perspective would take a
 * reader from a narrowed view to an unnarrowed one with nothing said. */
function decorateNav() {
  document.querySelectorAll(".topbar-nav a[href]").forEach((a) => {
    try {
      const url = new URL(a.getAttribute("href"), window.location.origin);
      if (state.project) url.searchParams.set("project", state.project);
      if (state.language) url.searchParams.set("language", state.language);
      if (state.perspective) {
        url.searchParams.set("perspective", state.perspective);
        if (state.min_importance) url.searchParams.set("min_importance", state.min_importance);
      }
      a.setAttribute("href", url.pathname + (url.search || ""));
    } catch (_) {
      // A malformed href is left alone; the server rendered it.
    }
  });
}

async function loadProjects() {
  let data;
  try {
    data = await api("/api/projects", { channel: "projects", quiet: true });
  } catch (err) {
    if (isAbort(err)) return;
    // The selector shows the server-rendered pair; the error is visible
    // once, not on every page in a row.
    toast("Could not load the project list", { kind: "error", hint: err.hint || err.message });
    return;
  }
  const pairs = (data && data.projects) || [];
  const current = (data && data.current) || { project: state.project, language: state.language };

  // The server's resolved pair is the truth for this page; mirror it into
  // the URL and the cookies so the two never disagree with what is shown.
  if (current.project && (current.project !== state.project || current.language !== state.language)) {
    setState({ project: current.project, language: current.language });
  }
  // The cookies are written even when the URL already agreed: a link that
  // names a project must also become the pair the next click uses, and
  // the API calls that carry no pair (suggestions) resolve through them.
  if (current.project) {
    writeCookie(COOKIE_PROJECT, current.project);
    writeCookie(COOKIE_LANGUAGE, current.language);
  }
  fillSelector(pairs, current);
  decorateNav();

  const empty = document.getElementById("archive-empty");
  if (empty) empty.hidden = pairs.length > 0;
}

/* Print/Export: export.js when present, plain fallbacks otherwise. */
async function initPrintExport() {
  try {
    await import("./export.js");
    return; // export.js binds [data-print] and [data-export] itself
  } catch (err) {
    console.warn("layout: export.js not available, using plain Print", err);
  }
  document.querySelectorAll("[data-print]").forEach((btn) => {
    btn.addEventListener("click", () => window.print());
  });
  document.querySelectorAll("[data-export]").forEach((menu) => {
    const pdf = menu.querySelector("[data-export-pdf]");
    if (pdf) {
      pdf.addEventListener("click", () => {
        toast("PDF export is not available in this build. Use Print and choose \"Save as PDF\".", { kind: "info" });
      });
    }
    const refresh = () => {
      const p = new URLSearchParams(window.location.search);
      menu.querySelectorAll("[data-export-format]").forEach((a) => {
        const base = a.getAttribute("href").split("?")[0];
        a.setAttribute("href", base + (p.toString() ? "?" + p.toString() : ""));
      });
    };
    refresh();
    menu.addEventListener("toggle", refresh);
    document.addEventListener("state:change", refresh);
  });
}

/* A <details> menu closes when one clicks elsewhere or presses Escape -
 * the browser does neither on its own. */
function initMenus() {
  document.addEventListener("click", (e) => {
    document.querySelectorAll("details.export-menu[open]").forEach((d) => {
      if (!d.contains(e.target)) d.removeAttribute("open");
    });
  });
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    document.querySelectorAll("details.export-menu[open]").forEach((d) => {
      d.removeAttribute("open");
      const s = d.querySelector("summary");
      if (s) s.focus();
    });
  });
}

function init() {
  readState();
  decorateNav();
  initMenus();
  initPerspective();
  loadProjects();
  initPrintExport();
  startServiceStatus();
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", init);
} else {
  init();
}


/* ── CLEAR, beside the button that asks ──────────────────────────────────
 *
 * Every query view has one (_macros.html: search_actions), and unless the
 * view has claimed it (`data-search-clear="custom"`) this is what it does:
 * empty the form and send it once.
 *
 * WHAT COUNTS AS THE QUERY. Text, dates, numbers, chips and typeaheads are
 * what a person typed, so they go. A <select> goes back to its FIRST option,
 * which is "Every kind" / "Every type" by convention on this product. A
 * checkbox is a narrowing ("only the visible area", "every word must
 * appear") and is unticked.
 *
 * WHAT DOES NOT. The segmented switches - Object/Type, Connections/
 * Locations, the level buttons - are not the query, they are which question
 * is being asked. Clearing them would answer a different question than the
 * one on screen, so Clear leaves them exactly where they are.
 *
 * One submit at the end, not one per field: six fields cleared one at a
 * time would be six searches, and the first five would be thrown away.
 */
function clearQuery(form) {
  form.querySelectorAll(".typeahead").forEach((root) => {
    if (root._typeahead && root._typeahead.clear) root._typeahead.clear();
  });
  form.querySelectorAll("[data-chips]").forEach((root) => {
    if (root._chips && root._chips.clear) root._chips.clear();
  });
  form.querySelectorAll("input").forEach((input) => {
    // The typeahead's own two inputs have just been dealt with, and its
    // hidden value must not be emptied a second time by name.
    if (input.closest(".typeahead") || input.closest("[data-chips]")) return;
    const type = (input.type || "text").toLowerCase();
    if (type === "checkbox") input.checked = false;
    else if (type === "radio" || type === "hidden" || type === "submit" || type === "button") return;
    else if (type === "range") input.value = input.defaultValue;
    else input.value = "";
  });
  form.querySelectorAll("select").forEach((select) => { select.selectedIndex = 0; });
  if (typeof form.requestSubmit === "function") form.requestSubmit();
  else form.submit();
}

document.addEventListener("click", (event) => {
  const button = event.target.closest("[data-search-clear]");
  if (!button || button.dataset.searchClear === "custom") return;
  const form = button.closest("form");
  if (form) clearQuery(form);
});

/* Every page, once. Delegated from the document, so it also covers the popups
 * the pickers build long after this runs. */
startHints(document);
