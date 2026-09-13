/* ==========================================================================
 *  Legend panel - "this colour means that group".
 *
 *  The map and the graph both colour connections by colour group, and a
 *  legend floating over the drawing would cover it. Here the legend is an
 *  ordinary block the page puts in its own grid column; this module only
 *  fills it. Nothing here is positioned,
 *  so the legend can never cover the graph, the suggestion list or the
 *  detail panel - test_graph_legend_no_overlap pins that.
 *
 *  Usage:
 *    import { renderLegend } from "./legend.js";
 *    const legend = renderLegend(panelEl, groups, { title: "Connection colours",
 *                                                    onToggle: (key, on) => ... });
 *    legend.update(groups);      // redraw with new groups
 *    legend.setCount(key, n);    // show a count next to a group
 *    legend.highlight(key);      // emphasise one row (null clears)
 *
 *  `groups` is [{key, name, colour, count?, description?}]. When `onToggle`
 *  is given, each row is a button with aria-pressed, so groups can be
 *  hidden by keyboard as well as mouse; without it the rows are plain
 *  list items. A `description` becomes a tooltip on the row AND part of the
 *  accessible name of its control - a key has to be small enough to stand
 *  beside the picture, and a bubble under it pushed the card down by three
 *  lines every time somebody asked what a colour meant.
 *
 *  `ungrouped: true` says this legend decodes CONNECTION COLOUR GROUPS, so
 *  it can drop the swatch column and say why when every row of it falls back
 *  to "Other" (see below). A legend of chart series has no such state and
 *  leaves the option alone.
 * ========================================================================== */

function swatch(colour) {
  const s = document.createElement("span");
  s.className = "legend-swatch";
  s.style.setProperty("--swatch", colour || "#777");
  s.setAttribute("aria-hidden", "true");
  return s;
}

/* ── The ⓘ, built in script ───────────────────────────────────────────── */
/*
 *  THE SAME COMPONENT THE FORMS USE, AND NOT A SECOND ONE.
 *
 *  templates/_macros.html emits `help_icon(target)` and `help_text(target)`
 *  beside every field of every form; static/js/hints.js opens them from ONE
 *  listener delegated off the document, so a pair built here - minutes after
 *  that listener was installed, inside a card that did not exist then -
 *  behaves exactly like one that came with the page. There is nothing to
 *  wire and nothing to re-wire.
 *
 *  WHY IT LIVES IN THIS FILE. Two callers need it: a legend entry (below)
 *  and a chart card's title (static/js/charts.js). charts.js already imports
 *  renderLegend from here, so here is the one end of that pair that can hold
 *  it without making the import a cycle.
 *
 *  THE GLYPH IS A COPY OF ONE PATH, AND THE COPY IS PINNED. Every icon in
 *  the product is drawn once, in _macros.html, and static/js/map.js CLONES
 *  the ones it needs out of the page rather than carrying a second copy -
 *  which is the right answer when the page is guaranteed to hold one. Here
 *  it is not: a chart card's ⓘ is the FIRST hint on the Diagrams page, so
 *  there is nothing yet to clone from. So the path is written out, once, and
 *  tests/unit/test_chart_registry.py reads both files and fails if the two
 *  ever differ - the same way the three copies of MACHINE_NAME_REGEX are
 *  held together (app/sqlbuild.py).
 */
const INFO_PATH = "M12 21a9 9 0 100-18 9 9 0 000 18M12 11v5M12 7.6v.5";
const SVG_NS = "http://www.w3.org/2000/svg";

let hintCount = 0;

function infoIcon() {
  // Cloned from the page where the page has one - a form on this view, or a
  // hint already built - so a product that changes the glyph changes it here
  // too, and the literal above is only the fallback it cannot always avoid.
  const found = document.querySelector(".hint-button .icon");
  if (found) return found.cloneNode(true);
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("class", "icon");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("width", "20");
  svg.setAttribute("height", "20");
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("fill", "none");
  svg.setAttribute("stroke", "currentColor");
  svg.setAttribute("stroke-width", "1.75");
  svg.setAttribute("stroke-linecap", "round");
  svg.setAttribute("stroke-linejoin", "round");
  const path = document.createElementNS(SVG_NS, "path");
  path.setAttribute("d", INFO_PATH);
  svg.appendChild(path);
  return svg;
}

/**
 * The button and the bubble hints.js opens, as two elements to place.
 *
 * `about` names what is being explained and goes into the accessible name:
 * eight cards on one screen with eight buttons called "What this is for"
 * leave a screen-reader user with eight identical buttons and no way to tell
 * which chart each belongs to (WCAG 2.5.3, the same rule the enlarge and
 * save buttons follow).
 */
export function hintPair(text, about) {
  const said = String(text == null ? "" : text).trim();
  if (!said) return null;
  hintCount += 1;
  const id = `chart-hint-${hintCount}`;

  const button = document.createElement("button");
  button.type = "button";
  button.className = "hint-button";
  button.dataset.hint = id;
  button.setAttribute("aria-expanded", "false");
  button.setAttribute("aria-controls", id);
  button.title = about ? `About ${about}` : "What this is";
  button.setAttribute("aria-label", about ? `What ${about} is` : "What this is for");
  button.appendChild(infoIcon());

  const bubble = document.createElement("div");
  bubble.className = "hint-bubble";
  bubble.id = id;
  bubble.setAttribute("role", "note");
  bubble.hidden = true;
  bubble.textContent = said;

  return { button, bubble, id };
}

/* ── When nothing has been grouped ────────────────────────────────────── */
/*
 * A SWATCH COLUMN THAT IS THE SAME ON EVERY ROW ASSERTS A CODING THAT DOES
 * NOT EXIST.
 *
 * `dashboard.colour_group_types` can be empty - 0 rows against tens of
 * thousands of distinct connection types on an archive whose types arrived
 * after the schema - and then every type resolves to the fallback group.
 * Panels that do not adapt would have the Graph's Details panel list
 * Product 250 (36%), Manufacturer 71 (10%), Competitor 46, Supplier 43,
 * Client 40 … eight different names beside eight identical brown squares,
 * and the Map's checklist do the same for a hundred types. Eight
 * squares of one colour is not a key, it is a claim that these eight things
 * are alike - which is the opposite of what the list is saying in words.
 *
 * So when EVERY row falls back, the colour is not drawn at all and the
 * reason is said once, with the page that fixes it named. One row is not a
 * column and needs no such sentence, but it gets the same treatment for
 * consistency: the fallback is not a group somebody chose either way.
 *
 * `app/colours.py` FALLBACK_KEY is the other end of this constant.
 */
export const UNGROUPED_KEY = "other";

export const UNGROUPED_NOTE =
  "No connection type is grouped yet, so every line is drawn in the fallback "
  + "colour. Group them on the Colours page.";

export function allUngrouped(rows) {
  const list = (rows || []).filter(Boolean);
  return list.length > 0 && list.every((r) => (r.group ?? r.key) === UNGROUPED_KEY);
}

/* The Colours page, carrying the project and the language the reader is
 * looking at - a link that drops the context lands them on another
 * project's colours. */
function coloursHref() {
  const url = new URL("/colours", window.location.origin);
  const now = new URLSearchParams(window.location.search);
  ["project", "language"].forEach((key) => {
    const value = now.get(key);
    if (value) url.searchParams.set(key, value);
  });
  return url.pathname + url.search;
}

/* The sentence as an element, with "the Colours page" as the link, so the
 * one place it is written down is this file. */
export function ungroupedNote(className = "legend-note") {
  const p = document.createElement("p");
  p.className = className;
  const [before, after] = UNGROUPED_NOTE.split("the Colours page");
  p.append(before);
  const link = document.createElement("a");
  link.href = coloursHref();
  link.textContent = "the Colours page";
  p.appendChild(link);
  p.append(after === undefined ? "" : after);
  return p;
}

export function renderLegend(panel, groups, opts = {}) {
  const options = Object.assign({ title: "Legend", onToggle: null, ungrouped: false,
                                  emptyText: "Nothing to show yet" }, opts);
  panel.classList.add("legend");
  panel.setAttribute("role", "region");

  let heading = panel.querySelector(".legend-title");
  if (!heading) {
    heading = document.createElement("h2");
    heading.className = "legend-title";
    panel.appendChild(heading);
  }
  heading.textContent = options.title;
  if (!heading.id) heading.id = `legend-title-${Math.random().toString(36).slice(2, 8)}`;
  panel.setAttribute("aria-labelledby", heading.id);

  let list = panel.querySelector(".legend-list");
  if (!list) {
    list = document.createElement("ul");
    list.className = "legend-list";
    panel.appendChild(list);
  }

  const state = { groups: [], hidden: new Set(), rows: new Map() };

  function draw() {
    list.textContent = "";
    const said = panel.querySelector(".legend-note");
    if (said) said.remove();
    state.rows.clear();
    if (!state.groups.length) {
      const li = document.createElement("li");
      li.className = "legend-empty";
      li.textContent = options.emptyText;
      list.appendChild(li);
      return;
    }
    // Every row the fallback: the colour column says nothing, so it is not
    // drawn and the note under the list says why (options.ungrouped is what
    // says this legend is about connection colour GROUPS - a chart's series
    // legend has its own colours and no such state).
    const blank = Boolean(options.ungrouped) && allUngrouped(state.groups);
    state.groups.forEach((g) => {
      const li = document.createElement("li");
      li.className = "legend-row";
      li.dataset.key = g.key;
      // A row without a name would be a swatch with nothing to say.
      const name = g.name || g.key;
      let host = li;
      if (options.onToggle) {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "legend-toggle";
        btn.setAttribute("aria-pressed", state.hidden.has(g.key) ? "false" : "true");
        btn.title = state.hidden.has(g.key) ? `Show ${name}` : `Hide ${name}`;
        btn.addEventListener("click", () => {
          const nowHidden = !state.hidden.has(g.key);
          if (nowHidden) state.hidden.add(g.key); else state.hidden.delete(g.key);
          btn.setAttribute("aria-pressed", nowHidden ? "false" : "true");
          btn.title = nowHidden ? `Show ${name}` : `Hide ${name}`;
          li.classList.toggle("is-hidden", nowHidden);
          options.onToggle(g.key, !nowHidden);
        });
        li.appendChild(btn);
        host = btn;
      }
      if (!blank) host.appendChild(swatch(g.colour));
      const label = document.createElement("span");
      label.className = "legend-name";
      label.textContent = name;
      host.appendChild(label);
      const count = document.createElement("span");
      count.className = "legend-count";
      count.textContent = g.count !== undefined && g.count !== null ? String(g.count) : "";
      host.appendChild(count);
      /* WHAT THIS COLOUR IS: a tooltip on the row, and words in the
       * accessible name.
       *
       * Not an ⓘ that opens a bubble UNDER the legend, which would push the
       * rest of the card down by three lines and make a key of eight
       * entries a column of eight buttons. A legend is a key, not a form: it
       * has to be readable at a glance and small enough to stand beside the
       * picture it explains.
       *
       * NOT HOVER-ONLY, which is the rule this product keeps everywhere a
       * tooltip appears: `title` is for a pointer, and the same sentence
       * goes into the accessible name of the control, so a screen reader
       * hears it as part of the entry rather than not at all. */
      if (g.description) {
        li.title = g.description;
        if (host !== li) {
          host.setAttribute("aria-label", `${name}. ${g.description}`);
        } else {
          li.setAttribute("role", "listitem");
          li.setAttribute("aria-label", `${name}. ${g.description}`);
        }
      }
      li.classList.toggle("is-hidden", state.hidden.has(g.key));
      list.appendChild(li);
      state.rows.set(g.key, li);
    });
    if (blank) panel.appendChild(ungroupedNote());
  }

  const api = {
    panel, list,
    update(newGroups) {
      state.groups = (newGroups || []).map((g) => ({ key: String(g.key ?? g.name), name: g.name, colour: g.colour, count: g.count, description: g.description }));
      draw();
    },
    setCount(key, n) {
      const li = state.rows.get(key);
      if (li) li.querySelector(".legend-count").textContent = n === null || n === undefined ? "" : String(n);
    },
    highlight(key) {
      state.rows.forEach((li, k) => li.classList.toggle("is-highlighted", key !== null && k === key));
    },
    get hidden() { return new Set(state.hidden); },
  };
  api.update(groups);
  return api;
}
