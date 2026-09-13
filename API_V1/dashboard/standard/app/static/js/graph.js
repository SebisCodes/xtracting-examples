/* ==========================================================================
 *  The connection graph.
 *
 *  One entity in the middle, its neighbours on a ring around it, and one
 *  more ring for every node somebody opens. The layout is radial placement
 *  grouped by colour, then an iterative collision solver so two bubbles
 *  never overlap. Four rules that are easy to get wrong:
 *
 *  1. THE LEGEND IS NOT HERE. It is a grid cell of the page (legend.js in
 *     the right column, see graph.css); this module only fills it. A
 *     legend floating over the drawing covers it.
 *
 *  2. EXPANSION DOES NOT RE-FIT. Re-fitting after every click moves every
 *     node on the screen, and the one thing a person needs after a click is
 *     to still know where they were (Cambridge Intelligence on orientation
 *     after a layout change). The view PANS, by the smallest amount that
 *     brings the new ring into sight, and the node that was clicked stays
 *     visible.
 *
 *  3. AN EDGE IS DRAWN ONCE. Expanding Microsoft, which is connected back
 *     to Apple, must not draw a second Apple-Microsoft line: an existing
 *     `min|max` edge is skipped outright ("cycle refusal"). A neighbour
 *     that is already on the canvas but not yet joined to this node gets a
 *     DASHED cross-link and no new bubble - the same entity twice would be
 *     two answers to one question.
 *
 *  4. IT WORKS WITHOUT A MOUSE. Nodes and connections are focusable, Enter
 *     opens or CLOSES them, the arrow keys move a focused node, and the zoom
 *     and reset buttons do what the wheel and the drag do. Nothing on this
 *     page is drag-only or hover-only.
 *
 *  EXPANDING IS A SWITCH, NOT A RATCHET. Clicking a node that is already
 *  open closes it again: its ring goes, and with it every node that hangs
 *  ONLY from it - a node another open node also holds stays where it is. The
 *  count of both goes into the sentence above the drawing, because a graph
 *  that silently loses nodes is frightening, and expanding the same node
 *  again restores exactly the picture that was there before. The one node
 *  that cannot be closed is the ROOT: it is what the graph is of, and the
 *  way to a different centre is the search field.
 *
 *  A NODE CAN BE ARRANGED BY HAND. Dragging a bubble moves it and it STAYS
 *  where it is dropped - the collision solver treats a dragged node the way
 *  it treats the root and never pushes it back, which is the difference
 *  between arranging a graph and fighting it. A dropped node is pinned until
 *  it is dragged again or Reset is pressed, the line above the drawing says
 *  how many are pinned, and the Reset button says that it lets them go.
 *  Drag and click are told apart by DISTANCE MOVED, exactly as the panning
 *  code already does it.
 * ========================================================================== */

import { api, isAbort, fmtDate, watchSearch } from "./api.js";
import { state, set as setState, setExtra, getExtra } from "./state.js";
import { announce } from "./a11y.js";
import { allUngrouped, renderLegend, ungroupedNote } from "./legend.js";
import { renderResolved, resolvedNotice, searchFor } from "./resolved.js";
import "./typeahead.js";

const SVG_NS = "http://www.w3.org/2000/svg";
const ROOT_RADIUS = 30;
const MIN_RADIUS = 15;
const MAX_RADIUS = 30;
const RING_MIN_ROOT = 220;
const RING_MIN_CHILD = 155;
const COLLISION_PAD = 18;
const COLLISION_ROUNDS = 300;
const ZOOM_STEP = 1.25;
const ZOOM_MIN = 0.15;
const ZOOM_MAX = 4;
const PAN_MS = 320;
/* How far a pointer must travel before it is a DRAG and not a click. The
 * same five pixels the panning code uses, so one gesture is read the same
 * way whether it starts on a bubble or on the background. */
const DRAG_SLOP_PX = 5;
/* One press of an arrow key, in screen pixels. Big enough to see, small
 * enough to place a node exactly with a few presses. */
const NUDGE_PX = 12;
/* The "pinned" mark on a bubble's shoulder, in screen pixels. */
const PIN_MARK_PX = 10;

/* ── The screen-pixel half of the drawing ────────────────────────────────
 *
 * A graph has two coordinate systems and this is where they meet. Positions
 * are in GRAPH units - the ring layout, the collision solver, the viewBox -
 * and the zoom turns those into pixels. Everything a PERSON has to read or
 * hit stays in pixels instead, and is divided by the zoom on its way into
 * the drawing:
 *
 *   text     labels in SVG units would scale with the fit - at 13 units and
 *            a fit of 0.73 the node names come out at 9.5 px, the smallest
 *            text on a dashboard whose readers include older eyes, and they
 *            shrink further with every node added. Each label carries
 *            `scale(1/zoom)` about its own anchor, so 16 px in graph.css is
 *            16 px on the screen at any zoom.
 *   targets  the same arithmetic would make the click targets 20-28 px. Opening a
 *            node is this view's primary action (WCAG 2.5.8 asks 24 px, the
 *            plan 44 for a primary control), so every node carries an
 *            invisible 44 px hit circle and every connection a 28 px handle.
 *   bubbles  a 16 px number needs a bubble to sit in, so the drawn circle
 *            has a floor of its own.
 *
 * The three floors are capped at `node.gap` - half the distance to the
 * nearest other node - so growing a target can never make two of them
 * overlap, which is the other half of 2.5.8 (the spacing exception). */
const LABEL_GAP_PX = 8;        // between the top of a bubble and its name
const LABEL_CLEAR_PX = 3;      // the gap two drawn names must keep between them
const LABEL_CHAR_PX = 8.5;     // about one character of the 16 px label face
const COUNT_BASELINE_PX = 5.5; // half the cap height of the number inside
const PLUS_GAP_PX = 17;        // between the bottom of a bubble and the "+"
const NODE_TARGET_PX = 44;
const EDGE_TARGET_PX = 28;
const MIN_BUBBLE_PX = 30;
const FIT_MARGIN_PX = 56;      // the room a label needs around the drawing

const form = document.getElementById("graph-form");
/* Every request on these channels turns the button quiet and writes
 * "Searching…" beside it, without this file having to remember to
 * (static/js/api.js: watchSearch). */
watchSearch(form, "graph");
const canvas = document.getElementById("graph-canvas");
const summaryBox = document.getElementById("graph-summary");
const resolvedBox = document.getElementById("graph-resolved");
const detailBox = document.getElementById("graph-detail");
const legendBox = document.getElementById("graph-legend");
const pinnedBox = document.getElementById("graph-pinned");
const labelsBox = document.getElementById("graph-labels-note");
const fromField = document.getElementById("graph-from");
const toField = document.getElementById("graph-to");

const graph = {
  nodes: new Map(),     // node id -> node
  edges: new Map(),     // "a|b" (sorted) -> edge
  aliases: new Map(),   // entity id -> the node that stands for it
  rootId: null,
  tx: 0, ty: 0, scale: 1,
  selection: null,      // {kind: "node"|"edge", key}
  focusKey: null,       // what to give the focus back to after a redraw
};

let svg = null;
let legend = null;
let panTimer = null;
let allGroups = [];   // the whole colour scale, from /api/graph/legend
/* What was drawn last, kept so the zoom can resize the text and the hit
 * areas without searching the DOM or re-reading numbers out of attributes. */
let painted = { nodes: [], edges: [] };
let paintedScale = 0;

/* ── Small helpers ───────────────────────────────────────────────────── */

function make(tag, attrs, text) {
  const node = document.createElementNS(SVG_NS, tag);
  Object.entries(attrs || {}).forEach(([k, v]) => node.setAttribute(k, String(v)));
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function edgeKey(a, b) {
  return a <= b ? `${a}|${b}` : `${b}|${a}`;
}

/* How many lines touch a bubble - and therefore the one number this page
 * calls "connections".
 *
 * Not how many ROWS the archive holds about the pair (Apple: 6) while the
 * sentence above the drawing counts the lines (4), the key calls both
 * "connections" and the Details panel counts the lines again - three
 * numbers, one word. This is the number the reader can check
 * by looking - the lines meeting the bubble - so it is the one the bubble
 * shows, the key explains and the summary counts. How much the archive says
 * about a pair is still on the edge itself (its Details panel and its
 * aria-label say "N in the archive"). */
function degreeOf(id) {
  let n = 0;
  graph.edges.forEach((edge) => { if (edge.a === id || edge.b === id) n += 1; });
  return n;
}

function connectionsWord(n) {
  return `${n} ${n === 1 ? "connection" : "connections"}`;
}

function reducedMotion() {
  return window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

function truncate(text, max) {
  const value = String(text || "");
  return value.length > max ? `${value.slice(0, max - 1)}…` : value;
}

/* The typeahead's hidden input carries the term; the visible field shows it
 * as a placeholder and stays empty. Filling it - from the URL when the page
 * opens, and from the text that was just drawn - must not look like
 * somebody chose something, so nothing is dispatched: the choose handler
 * below would draw the same graph a second time. */
function fillFromUrl(value) {
  const wrap = form && form.querySelector(".typeahead");
  if (!wrap) return;
  const hidden = wrap.querySelector('input[type="hidden"]');
  const input = wrap.querySelector("input[data-typeahead]");
  if (hidden) hidden.value = value || "";
  if (input) {
    input.value = "";
    input.placeholder = value || input.dataset.placeholder || "";
  }
  wrap.classList.toggle("is-set", Boolean(value));
}

/* WHAT THE READER IS ASKING FOR.
 *
 * TYPED TEXT WINS OVER WHAT IS SET, because "Draw graph" is the Enter key
 * with a mouse, and Enter counts the typed text ("one need not be in the
 * list to search for it", typeahead.js). typeahead.js writes into the
 * hidden input only when a suggestion or Enter confirms the text, so
 * reading the hidden input alone threw away everything somebody typed and
 * then reached for the button with - the box read "Zzqxwv" while the page
 * drew nothing and asked for a search that had just been made. */
function termFromForm() {
  const wrap = form && form.querySelector(".typeahead");
  const hidden = wrap && wrap.querySelector('input[type="hidden"]');
  const input = wrap && wrap.querySelector("input[data-typeahead]");
  const typed = input ? input.value.trim() : "";
  return typed || (hidden ? hidden.value.trim() : "");
}

/* ── The date range ──────────────────────────────────────────────────── */
/*
 * "WHO WAS THIS CONNECTED TO LAST YEAR" has to be askable, so From/To go
 * with every graph request and the service honours them. The two fields
 * live in the URL like every other piece of this
 * view's state (`from` / `to`, the names the Events view uses), so a reload
 * and a shared link draw the same graph - and every expansion carries them,
 * or a second ring would answer a wider question than the first.
 */
function dateRange() {
  return { from: (fromField && fromField.value) || "", to: (toField && toField.value) || "" };
}

function datesSet() {
  const { from, to } = dateRange();
  return Boolean(from || to);
}

function datesClause() {
  const { from, to } = dateRange();
  if (from && to) return ` between ${from} and ${to}`;
  if (from) return ` from ${from}`;
  if (to) return ` up to ${to}`;
  return "";
}

function dateParams() {
  const { from, to } = dateRange();
  return { date_from: from, date_to: to };
}

/* Search for one term as if it had been chosen from the list - what the
 * "Show only Apple Inc." button in the notice does. */
function chooseTerm(term) {
  searchFor(form && form.querySelector("input[data-typeahead]"), term);
}

/* WHY THE GRAPH SAYS IT TWICE.
 *
 * The summary line under the toolbar counts what is drawn ("5 entities and
 * 4 connections around Apple"), and it is an aria-live region that is
 * rewritten on every expansion. The notice is the other thing: it says
 * what the TERM became - "Apple Inc. belongs to the bucket Apple" - and
 * carries the button that leaves the bucket for the single entity. It
 * belongs outside the live region, or every expansion would read the
 * button's label out again. */
function showResolved(data, term) {
  renderResolved(resolvedBox, resolvedNotice(data && data.resolved, term), chooseTerm);
}

/* ── Data ────────────────────────────────────────────────────────────── */

function radiusFor(count) {
  return Math.min(MAX_RADIUS, MIN_RADIUS + Math.sqrt(Math.max(0, count)) * 3);
}

/* One answer merged into the picture. Returns what happened, because that
 * is what the status line and the detail panel have to say afterwards. */
function mergeNeighbours(sourceId, neighbours) {
  const source = graph.nodes.get(sourceId);
  const added = [];
  const crossed = [];
  let skipped = 0;

  (neighbours || []).forEach((n) => {
    const targetId = graph.aliases.get(n.id) || n.id;
    if (targetId === sourceId) return;          // the centre itself
    const key = edgeKey(sourceId, targetId);
    if (graph.edges.has(key)) { skipped += 1; return; }   // cycle refusal

    const known = graph.nodes.has(targetId);
    if (!known) {
      graph.nodes.set(targetId, {
        id: targetId, name: n.name || targetId, type: n.type || "",
        level: (source ? source.level : 0) + 1,
        x: null, y: null, r: radiusFor(n.count), expanded: false,
        count: n.count || 0, group: n.group || "other", colour: n.colour || "#7D4B12",
        parentId: sourceId,
      });
      graph.aliases.set(n.id, targetId);
      added.push(targetId);
    } else {
      crossed.push(targetId);
    }
    graph.edges.set(key, {
      key, a: sourceId, b: targetId, cross: known,
      types: n.types || [], count: n.count || 0,
      group: n.group || "other", colour: n.colour || "#7D4B12",
      first: n.first || null, last: n.last || null,
      level: Math.max(source ? source.level : 0, graph.nodes.get(targetId).level),
    });
  });
  return { added, crossed, skipped };
}

function registerAliases(entity, nodeId) {
  (entity && entity.ids ? entity.ids : []).forEach((id) => graph.aliases.set(id, nodeId));
}

async function startGraph(term) {
  if (!term) return;
  setStatus(`Loading the connections of ${term}…`);
  if (!graph.nodes.size) canvasMessage("Loading…");
  let data;
  try {
    data = await api("/api/graph/neighbours",
      { params: { q: term, ...dateParams() }, channel: "graph" });
  } catch (err) {
    if (isAbort(err)) return;
    // WHAT WENT WRONG GOES WHERE THE GRAPH WOULD HAVE BEEN. The drawing area
    // is the largest thing on the page, and it must not be left reading
    // "Loading…" for ever after a failed request - the biggest region of the
    // page stating the opposite of what has happened, while the aria-live
    // line says nothing at all. It carries the sentence and a button that
    // repeats the request.
    const said = errorSentence("The connections could not be loaded.", err);
    setStatus(said);
    if (!graph.nodes.size) canvasMessage(said, { retry: () => startGraph(term) });
    showDetail(errorPanel("The connections could not be loaded.", err, () => startGraph(term)));
    announce(said);
    return;
  }
  if (!data.entity) {
    graph.nodes.clear(); graph.edges.clear(); graph.aliases.clear();
    graph.rootId = null;
    render();
    // The same sentence the map gives for the same case, second clause and
    // all: two views that answer one question differently teach a reader
    // that the answer depends on the page.
    setStatus(`Nothing in this project is called “${term}”. `
      + "Check the project and language in the top bar.");
    showDetail(notFoundPanel(term));
    showResolved(null, term);
    return;
  }

  graph.nodes.clear();
  graph.edges.clear();
  graph.aliases.clear();
  graph.selection = null;
  graph.focusKey = null;
  graph.tx = 0; graph.ty = 0; graph.scale = 1;
  graph.rootId = data.entity.id;
  graph.nodes.set(data.entity.id, {
    id: data.entity.id, name: data.entity.name || term, type: data.entity.type || "",
    level: 0, x: 0, y: 0, r: ROOT_RADIUS, expanded: true,
    // THE ROOT COUNTS TOO. The bubble in the middle is the first thing a
    // reader looks at, and it must not be the one bubble with no number in
    // it - while the key beside the drawing says the number in a bubble is
    // how many connections it has. What is PRINTED is worked out at drawing
    // time (degreeOf), so this is only what the archive says about the pair,
    // which is what sizes the bubble.
    count: (data.neighbours || []).reduce((sum, n) => sum + (n.count || 0), 0),
    group: "root", colour: "#1d4f91", parentId: null,
    kind: data.entity.kind, members: data.entity.members || [],
  });
  registerAliases(data.entity, data.entity.id);

  mergeNeighbours(data.entity.id, data.neighbours);
  layoutNewNodes();
  fitView();
  render();
  updateLegend(data.legend);
  selectNode(data.entity.id, { focus: false });
  showResolved(data, term);
  setStatus(summaryText(data));
}

/* ── Closing a node again ────────────────────────────────────────────── */

/* Everything that can still be reached from the root by walking OUT OF
 * EXPANDED NODES ONLY. That is the whole rule for what a graph holds: the
 * root is there because it was searched for, and every other node is there
 * because some open node brought it in. Close one of those and whatever it
 * alone was holding has nothing left to hold it - while a node a SECOND open
 * node also reaches is still reached, and stays.
 *
 * Written as a walk rather than as bookkeeping, so it cannot get out of step
 * with the picture: the answer is derived from the nodes and edges that are
 * actually drawn, every time it is asked. */
function reachable() {
  const keep = new Set();
  if (!graph.rootId || !graph.nodes.has(graph.rootId)) return keep;
  keep.add(graph.rootId);
  const queue = [graph.rootId];
  while (queue.length) {
    const id = queue.shift();
    const node = graph.nodes.get(id);
    if (!node || !node.expanded) continue;
    graph.edges.forEach((edge) => {
      const other = edge.a === id ? edge.b : (edge.b === id ? edge.a : null);
      if (!other || keep.has(other)) return;
      keep.add(other);
      queue.push(other);
    });
  }
  return keep;
}

/* Close a node: it stops being expanded, and every node the walk above can
 * no longer reach goes with it. Says what happened in the same breath,
 * because a drawing that loses six bubbles without a word is frightening -
 * and names the ones that STAYED, which is the fact that stops a reader
 * thinking the graph is broken. */
function collapseNode(id) {
  const node = graph.nodes.get(id);
  if (!node || !node.expanded) return;
  // THE ROOT IS NOT COLLAPSIBLE. It is what the graph is OF: closing it
  // would leave one bubble and no way back except clicking the same bubble,
  // and for a bucket centre there is no id to ask the server about anyway
  // (the centre of a bucket graph is `bucket:<id>`, which is not an entity).
  // The way to a different centre is the search field, and this says so.
  if (id === graph.rootId) {
    selectNode(id);
    const said = `${node.name} is the centre of this graph, so it stays open. `
      + "Search for another entity to draw a different centre.";
    appendNote(said);
    setStatus(said);
    announce(said);
    return;
  }

  // What this node itself brought in - its own ring. Some of those may be
  // held by another open node and survive; that is the number a reader needs.
  const ring = [...graph.nodes.values()].filter((n) => n.parentId === id).map((n) => n.id);
  node.expanded = false;
  const keep = reachable();
  const removed = [...graph.nodes.keys()].filter((nid) => !keep.has(nid));
  const kept = ring.filter((nid) => keep.has(nid));

  removed.forEach((nid) => graph.nodes.delete(nid));
  // AN EDGE IS THERE BECAUSE AN OPEN NODE HAS IT. Every edge on this canvas
  // was created by expanding one of its two ends (mergeNeighbours), so an
  // edge whose ends are now both closed is a line nothing is holding - the
  // cross-link an expansion drew between two nodes that were already here is
  // exactly that case. Dropping it is what makes closing a node the undo of
  // opening it: expand, collapse, expand again gives the same picture and
  // the same counts, which is the one promise a collapse has to keep.
  [...graph.edges.keys()].forEach((key) => {
    const edge = graph.edges.get(key);
    const a = graph.nodes.get(edge.a);
    const b = graph.nodes.get(edge.b);
    if (!a || !b || !(a.expanded || b.expanded)) graph.edges.delete(key);
  });
  // An alias that points at a bubble which is no longer there would make the
  // next expansion draw an edge to nothing.
  [...graph.aliases.entries()].forEach(([entityId, nodeId]) => {
    if (!graph.nodes.has(nodeId)) graph.aliases.delete(entityId);
  });
  if (graph.selection && graph.selection.kind === "node" && removed.includes(graph.selection.key)) {
    graph.selection = null;
  }
  if (graph.selection && graph.selection.kind === "edge" && !graph.edges.has(graph.selection.key)) {
    graph.selection = null;
  }

  graph.focusKey = `node:${id}`;
  render();
  updateLegend();
  selectNode(id, { focus: false });
  const bits = [`${removed.length} removed`];
  if (kept.length) {
    bits.push(`${kept.length} kept, still connected elsewhere`);
  }
  const said = `${node.name} collapsed - ${bits.join(", ")}. ${totalsText()}`;
  setStatus(said);
  announce(said);
}

/* One gesture, two outcomes, decided by the node: a click or Enter opens a
 * closed node and closes an open one. */
function toggleNode(id) {
  const node = graph.nodes.get(id);
  if (!node) return;
  if (node.expanded) collapseNode(id);
  else expandNode(id);
}

async function expandNode(id) {
  const node = graph.nodes.get(id);
  if (!node) return;
  if (node.expanded) { collapseNode(id); return; }
  node.expanded = true;
  // Say WHICH node is being opened before the answer arrives; the panel is
  // filled again from the answer.
  selectNode(id, { focus: false });
  setStatus(`Loading the connections of ${node.name}…`);
  let data;
  try {
    data = await api("/api/graph/neighbours",
      { params: { id, ...dateParams() }, channel: "graph" });
  } catch (err) {
    node.expanded = false;
    if (isAbort(err)) return;
    // The graph that is already drawn stays drawn - a failed expansion is
    // not a reason to take away what a reader was looking at.
    const said = errorSentence(`The connections of ${node.name} could not be loaded.`, err);
    setStatus(said);
    showDetail(errorPanel(`The connections of ${node.name} could not be loaded.`, err,
      () => expandNode(id)));
    announce(said);
    return;
  }
  // CLOSED WHILE IT WAS LOADING. A second click on a node whose request is
  // still on its way collapses it, and the answer that arrives afterwards
  // would then draw a ring around a node the reader has just closed. The
  // answer is thrown away instead; clicking again asks for it again.
  if (!node.expanded) return;
  registerAliases(data.entity, id);
  const result = mergeNeighbours(id, data.neighbours);
  layoutNewNodes();
  render();
  updateLegend(data.legend);
  // The view moves as little as it can, and never so far that the node
  // somebody just opened leaves the screen.
  panToInclude(result.added.map((n) => graph.nodes.get(n)), node);
  selectNode(id, { focus: false });
  setStatus(expansionText(node, result));
  announce(expansionText(node, result));
}

function summaryText(data) {
  const nodes = graph.nodes.size;
  const edges = graph.edges.size;
  const centre = graph.nodes.get(graph.rootId);
  const parts = [`${nodes} ${nodes === 1 ? "entity" : "entities"} and ${edges} ${edges === 1 ? "connection" : "connections"} around ${centre ? centre.name : ""}${datesClause()}.`];
  if (centre && centre.kind === "bucket" && centre.members.length) {
    parts.push(`${centre.name} is a bucket of ${centre.members.length} entities.`);
  }
  if (data && data.capped) parts.push(`Only the ${data.limit} busiest connections are drawn.`);
  if (!edges) {
    // WHY IT IS EMPTY IS TWO DIFFERENT FACTS, and a reader acts on each of
    // them differently: one is answered by clearing a field, the other by
    // waiting for the archive to fill up. The server counts what the range
    // left out (`without_dates`) so this line can tell them apart.
    const without = data && data.without_dates;
    if (datesSet() && without) {
      parts.push(`Nothing is connected to it${datesClause()}. `
        + `Without the date range there ${without === 1 ? "is 1 connection" : `are ${without} connections`}: `
        + "clear From and To to draw them.");
    } else {
      parts.push("Nothing is connected to it in this project.");
    }
  }
  return parts.join(" ");
}

function totalsText() {
  const nodes = graph.nodes.size;
  const edges = graph.edges.size;
  return `${nodes} ${nodes === 1 ? "entity" : "entities"} and `
    + `${edges} ${edges === 1 ? "connection" : "connections"} in total.`;
}

function expansionText(node, result) {
  const bits = [];
  if (result.added.length) bits.push(`${result.added.length} new`);
  if (result.crossed.length) bits.push(`${result.crossed.length} already on the graph, joined with a dashed line`);
  if (result.skipped) bits.push(`${result.skipped} already connected`);
  if (!bits.length) return `${node.name} has no further connections in this project.`;
  return `${node.name}: ${bits.join(", ")}. ${totalsText()}`;
}

/* A fragment from the API, finished into a sentence. `{error, hint}` are
 * written as lowercase fragments without a full stop, because the toast
 * prints them as two lines; anything that puts them into running text has to
 * close them (the same helper as dashboard.js and diagrams.js). */
function sentence(text) {
  const clean = String(text == null ? "" : text).trim();
  if (!clean) return "";
  const capital = clean.charAt(0).toUpperCase() + clean.slice(1);
  return /[.!?…]$/.test(capital) ? capital : `${capital}.`;
}

/* One line a person can act on: what failed, what the server said, and what
 * to do about it. */
function errorSentence(what, err) {
  return [what, sentence(err && err.message), sentence(err && err.hint)]
    .filter(Boolean).join(" ");
}

/* ── Layout ──────────────────────────────────────────────────────────── */

/* Places every node that has no coordinates yet on a ring around its
 * parent, ordered by colour group so neighbours of one kind sit together,
 * then pushes overlapping bubbles apart. */
function layoutNewNodes() {
  const byParent = new Map();
  graph.nodes.forEach((n) => {
    if (n.x !== null && n.x !== undefined) return;
    if (!byParent.has(n.parentId)) byParent.set(n.parentId, []);
    byParent.get(n.parentId).push(n);
  });

  byParent.forEach((children, parentId) => {
    const p = graph.nodes.get(parentId) || { x: 0, y: 0, level: 0 };
    children.sort((a, b) => String(a.group).localeCompare(String(b.group)) || b.count - a.count
      || a.name.localeCompare(b.name));

    // The ring is wide enough for everyone on it before the solver starts.
    const need = children.reduce((sum, c) => sum + 2 * (c.r + 16), 0);
    const isRoot = p.level === 0;
    const radius = Math.max(isRoot ? RING_MIN_ROOT : RING_MIN_CHILD, need / (2 * Math.PI) + 40);
    // A second ring opens AWAY from the middle of the graph, so the new
    // bubbles land in empty space instead of on top of the root.
    const outward = Math.atan2(p.y, p.x) || 0;
    const span = isRoot ? 2 * Math.PI : Math.PI * 1.1;

    children.forEach((n, i) => {
      const t = children.length === 1 ? 0.5
        : i / (isRoot ? children.length : Math.max(1, children.length - 1));
      const angle = isRoot ? t * 2 * Math.PI : outward - span / 2 + t * span;
      const ring = radius + (i % 2) * 26;   // a slight stagger before relaxation
      n.x = p.x + ring * Math.cos(angle);
      n.y = p.y + ring * Math.sin(angle);
    });
  });

  resolveCollisions();
}

/* Pushes every overlapping pair apart until none is left. The root does not
 * move: it is the thing everything else is placed around. */
function resolveCollisions() {
  const nodes = [...graph.nodes.values()];
  for (let round = 0; round < COLLISION_ROUNDS; round += 1) {
    let moved = false;
    for (let i = 0; i < nodes.length; i += 1) {
      for (let j = i + 1; j < nodes.length; j += 1) {
        const a = nodes[i];
        const b = nodes[j];
        let dx = b.x - a.x;
        let dy = b.y - a.y;
        let dist = Math.hypot(dx, dy);
        const minDist = a.r + b.r + COLLISION_PAD;
        if (dist >= minDist) continue;
        if (dist < 0.001) {
          // Exactly on top of each other: nudge in a direction that depends
          // only on the two positions, so a redraw does not shuffle them.
          const angle = (((i * 37 + j * 71) % 360) * Math.PI) / 180;
          dx = Math.cos(angle); dy = Math.sin(angle); dist = 1;
        }
        const push = (minDist - dist) / dist;
        // A NODE SOMEBODY PUT THERE IS AS FIXED AS THE ROOT. The solver's
        // whole job is to stop two bubbles overlapping, and doing that by
        // moving both of them would mean every new ring shoves a
        // hand-placed node back out of the place it has been put in. A
        // pinned node is a fact about what the reader wants, so the other
        // bubble is the one that moves.
        const aFixed = a.id === graph.rootId || a.pinned;
        const bFixed = b.id === graph.rootId || b.pinned;
        const aShare = aFixed ? 0 : (bFixed ? 1 : 0.5);
        const bShare = bFixed ? 0 : (aFixed ? 1 : 0.5);
        a.x -= dx * push * aShare; a.y -= dy * push * aShare;
        b.x += dx * push * bShare; b.y += dy * push * bShare;
        moved = true;
      }
    }
    if (!moved) break;
  }
  measureGaps(nodes);
}

/* Half the distance to the nearest other node, per node.
 *
 * It is the ceiling for everything that grows to stay hittable: a 44 px
 * circle around a bubble is only an improvement while it does not reach into
 * its neighbour, and two overlapping targets are a worse answer than one
 * small one. A lone node has no neighbour and therefore no ceiling. */
function measureGaps(nodes) {
  nodes.forEach((n) => { n.gap = Infinity; });
  for (let i = 0; i < nodes.length; i += 1) {
    for (let j = i + 1; j < nodes.length; j += 1) {
      const half = Math.hypot(nodes[j].x - nodes[i].x, nodes[j].y - nodes[i].y) / 2;
      if (half < nodes[i].gap) nodes[i].gap = half;
      if (half < nodes[j].gap) nodes[j].gap = half;
    }
  }
}

/* The three radii of one node in GRAPH units, at the current zoom. */
function radii(node) {
  const ceiling = Number.isFinite(node.gap) ? node.gap : Infinity;
  const draw = Math.max(node.r, Math.min(MIN_BUBBLE_PX / 2 / graph.scale, ceiling));
  const hit = Math.max(draw, Math.min(NODE_TARGET_PX / 2 / graph.scale, ceiling));
  return { draw, hit, ceiling };
}

/* How much of a name fits beside its neighbours, in characters.
 *
 * The labels no longer shrink with the drawing, so at a deep zoom-out two of
 * them would run into each other instead of becoming unreadable. Truncation
 * is the trade the other way round ("smart truncation and tooltips",
 * Cambridge Intelligence): the room a node has is the distance to its
 * nearest neighbour, and the whole name is a hover away and in the panel. */
function labelBudget(node, ceiling) {
  if (!Number.isFinite(ceiling)) return 24;
  return Math.max(6, Math.min(24, Math.round((2 * ceiling * graph.scale) / LABEL_CHAR_PX)));
}

function size() {
  return {
    w: canvas.clientWidth || 900,
    h: canvas.clientHeight || 560,
  };
}

/* The rectangle the viewBox shows, in graph coordinates. */
function viewRect() {
  const { w, h } = size();
  const s = graph.scale;
  return {
    x0: -w / 2 / s - graph.tx, y0: -h / 2 / s - graph.ty,
    x1: w / 2 / s - graph.tx, y1: h / 2 / s - graph.ty,
  };
}

/* Everything on screen, with a margin. Used by the search and by Fit, never
 * by an expansion. */
function fitView() {
  const nodes = [...graph.nodes.values()].filter((n) => n.x !== null);
  if (!nodes.length) return;
  const minX = Math.min(...nodes.map((n) => n.x - n.r));
  const maxX = Math.max(...nodes.map((n) => n.x + n.r));
  const minY = Math.min(...nodes.map((n) => n.y - n.r));
  const maxY = Math.max(...nodes.map((n) => n.y + n.r));
  const { w, h } = size();
  // The margin has to hold a LABEL, and a label is 16 px whatever the zoom -
  // so the room it needs is in pixels, and the pixels depend on the zoom,
  // which depends on the room. One refinement closes that circle far enough:
  // fit the bubbles, then fit them again with the margin that zoom implies.
  const zoom = (pad) => Math.max(ZOOM_MIN, Math.min(
    w / (maxX - minX + 2 * pad), h / (maxY - minY + 2 * pad), 1.4));
  graph.scale = zoom(FIT_MARGIN_PX / zoom(0));
  graph.tx = -(minX + maxX) / 2;
  graph.ty = -(minY + maxY) / 2;
  updateViewBox();
}

/* Bring `points` into view by moving as little as possible.
 *
 * No re-fitting: after an expansion the picture has to look like the same
 * picture, or nobody can tell what was added (Cambridge Intelligence on
 * orientation after a layout change). So the centre of the view stays where
 * it is, and only two things happen - the view zooms OUT if the new ring is
 * larger than the screen, around the point it is already looking at, and
 * then it slides by the smallest distance that brings the ring inside.
 */
function panToInclude(points, keep) {
  const wanted = (points || []).filter(Boolean);
  if (!wanted.length) return;
  const box = keep ? wanted.concat([keep]) : wanted;

  /* The rectangle the new ring needs, in graph units.
   *
   * Room for the LABEL as well as the bubble: a node brought "into view"
   * without its name is a circle with a number in it, and a name is up to
   * 24 characters of 16 px text centred on the bubble - far more than the
   * bubble is wide ("European Commission" is 178 px). Both are measured in
   * PIXELS and divided by the zoom, so the box has to be worked out again
   * after a zoom: the same 56 px cover more graph units at a smaller scale.
   */
  const bounds = () => {
    const margin = FIT_MARGIN_PX / graph.scale;
    const half = (n) => Math.min(String(n.name || "").length, 24) * LABEL_CHAR_PX / 2 / graph.scale;
    return {
      minX: Math.min(...box.map((n) => Math.min(n.x - n.r - margin, n.x - half(n)))),
      maxX: Math.max(...box.map((n) => Math.max(n.x + n.r + margin, n.x + half(n)))),
      minY: Math.min(...box.map((n) => n.y - n.r - margin)),
      maxY: Math.max(...box.map((n) => n.y + n.r + margin)),
    };
  };

  const { w, h } = size();
  let b = bounds();
  // Zooming keeps tx/ty, so the point in the middle of the screen stays in
  // the middle of the screen: the graph gets smaller, it does not jump. Two
  // passes, because zooming out grows the margins in graph units - the same
  // refinement fitView makes for the same reason.
  for (let pass = 0; pass < 2; pass += 1) {
    const needX = (b.maxX - b.minX) * graph.scale;
    const needY = (b.maxY - b.minY) * graph.scale;
    if (needX <= w && needY <= h) break;
    const factor = Math.min(w / needX, h / needY, 1);
    const next = Math.max(ZOOM_MIN, graph.scale * factor);
    if (next === graph.scale) break;
    graph.scale = next;
    updateViewBox();
    b = bounds();
  }

  /* THE SIGN OF THE PAN.
   *
   * `graph.tx` moves the CONTENT: the visible rectangle is
   * [-w/2/s - tx, w/2/s - tx], so a bigger tx shows what is further LEFT.
   * The two clamps below work out how far the window has to move, and the
   * result is ADDED - subtracting it pans away from the new ring instead of
   * towards it. Measured at 1024x768 with the sign wrong: expanding Apple
   * Inc. on a Tim Cook graph puts European Commission at graph x -401 with
   * the window ending at -335, and the pan moves the window from -397 to
   * -335: the node it was supposed to bring into view ends up 162 px off
   * the left edge of the canvas, on every expansion that adds a node on the
   * far side of the click.
   */
  const rect = viewRect();
  let dx = 0;
  let dy = 0;
  if (b.maxX > rect.x1) dx -= b.maxX - rect.x1;
  if (b.minX + dx < rect.x0) dx += rect.x0 - (b.minX + dx);
  if (b.maxY > rect.y1) dy -= b.maxY - rect.y1;
  if (b.minY + dy < rect.y0) dy += rect.y0 - (b.minY + dy);
  if (!dx && !dy) return;
  animatePan(graph.tx + dx, graph.ty + dy);
}

function animatePan(toX, toY) {
  if (panTimer) { cancelAnimationFrame(panTimer); panTimer = null; }
  if (reducedMotion()) {
    graph.tx = toX; graph.ty = toY;
    updateViewBox();
    return;
  }
  const fromX = graph.tx;
  const fromY = graph.ty;
  const started = performance.now();
  const step = (now) => {
    const t = Math.min(1, (now - started) / PAN_MS);
    const eased = 1 - (1 - t) * (1 - t);
    graph.tx = fromX + (toX - fromX) * eased;
    graph.ty = fromY + (toY - fromY) * eased;
    updateViewBox();
    panTimer = t < 1 ? requestAnimationFrame(step) : null;
  };
  panTimer = requestAnimationFrame(step);
}

function zoomBy(factor) {
  graph.scale = Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, graph.scale * factor));
  updateViewBox();
}

function updateViewBox() {
  if (!svg) return;
  const { w, h } = size();
  const s = graph.scale;
  svg.setAttribute("viewBox",
    `${-w / 2 / s - graph.tx} ${-h / 2 / s - graph.ty} ${w / s} ${h / s}`);
  refreshScale();
}

/* Re-lay the text and the hit areas after a zoom.
 *
 * Panning does not change the zoom, and an animated pan calls this on every
 * frame, so the whole walk is skipped unless the scale actually moved. */
function refreshScale(force) {
  if (!svg) return;
  if (!force && paintedScale === graph.scale) return;
  paintedScale = graph.scale;
  const k = 1 / graph.scale;
  painted.nodes.forEach((drawn) => {
    const { node } = drawn;
    const { draw, hit, ceiling } = radii(node);
    drawn.circle.setAttribute("r", draw);
    drawn.hit.setAttribute("r", hit);
    // The TEXT is layoutLabels' business - it puts every name back before it
    // measures, so a second pass over the same drawing cannot leave a label
    // it hid the first time empty for good.
    place(drawn.label, node.x, node.y - draw - LABEL_GAP_PX * k, k);
    place(drawn.count, node.x, node.y + COUNT_BASELINE_PX * k, k);
    place(drawn.plus, node.x, node.y + draw + PLUS_GAP_PX * k, k);
    if (drawn.pinMark) {
      // On the bubble's upper-right shoulder, at 45 degrees, and PIN_MARK_PX
      // across on the screen whatever the zoom - like every other mark here.
      drawn.pinMark.setAttribute("cx", node.x + draw * 0.71);
      drawn.pinMark.setAttribute("cy", node.y - draw * 0.71);
      drawn.pinMark.setAttribute("r", (PIN_MARK_PX / 2) * k);
    }
  });
  const side = EDGE_TARGET_PX * k;
  painted.edges.forEach((drawn) => {
    drawn.handle.setAttribute("x", drawn.mid[0] - side / 2);
    drawn.handle.setAttribute("y", drawn.mid[1] - side / 2);
    drawn.handle.setAttribute("width", side);
    drawn.handle.setAttribute("height", side);
    drawn.handle.setAttribute("rx", side / 3.5);
  });
  // NOT WHILE A BUBBLE IS UNDER THE HAND. moveDrawn calls this on every
  // pointermove, and deciding which names to drop sixty times a second would
  // flicker the drawing under somebody who is arranging it. onPointerUp runs
  // the pass once when the node is dropped.
  if (!(nodeDrag && nodeDrag.moved)) layoutLabels();
}

/* ── Names that never overlap ─────────────────────────────────────────── */
/*
 * THE RULE THE CHARTS ALREADY KEEP, ON THE ONE VIEW THAT NEVER HAD IT.
 *
 * `labelBudget` above truncates a name to the room its OWN bubble has and
 * never asks what the neighbour is doing, and the collision solver
 * (resolveCollisions) separates BUBBLES, not the text over them. Drawing
 * "Apple Inc. (Company)" on a real archive puts 121 nodes on the screen and,
 * unchecked, 290 pairs of intersecting label boxes with them - a smear of half-names
 * ("iFiPhE…", "(PeaP.c", "QubAC7") where the picture should have carried the
 * few names a reader can use. The charts decide this from pixels
 * (charts.js labelsFit: a label is drawn only when it has the room), and so
 * does this.
 *
 * MEASURED, NOT GUESSED, AND MEASURED ONCE. Every label is written and
 * placed first, then every box is read, then the ones that lose are cleared:
 * one layout for the whole drawing rather than one per node. Hiding a label
 * cannot move another, so the boxes read in the middle pass stay true.
 *
 * WHICH NAME SURVIVES. The centre of the graph first - it is the thing that
 * was searched for - then the busiest bubbles, which are the ones a reader
 * is looking for, and ties in draw order so the picture does not reshuffle
 * between two redraws of the same graph.
 *
 * NOTHING IS LOST. The whole name stays in the bubble's <title> (the
 * tooltip), in its aria-label, in the detail panel and in the pinned/summary
 * lines; the line under the toolbar says how many were left out, the way a
 * crowded chart card does.
 */
function boxesTouch(a, b) {
  return a.left < b.right + LABEL_CLEAR_PX && b.left < a.right + LABEL_CLEAR_PX
    && a.top < b.bottom + LABEL_CLEAR_PX && b.top < a.bottom + LABEL_CLEAR_PX;
}

function layoutLabels() {
  const labelled = painted.nodes.filter((drawn) => drawn.label);
  if (!labelled.length) {
    sayHiddenLabels(0, 0);
    return;
  }
  labelled.forEach((drawn) => {
    const { ceiling } = radii(drawn.node);
    drawn.label.textContent = truncate(drawn.node.name, labelBudget(drawn.node, ceiling));
    drawn.label.removeAttribute("data-hidden");
  });
  const order = labelled
    .map((drawn, index) => ({ drawn, index }))
    .sort((a, b) => {
      const rootA = a.drawn.node.id === graph.rootId ? 0 : 1;
      const rootB = b.drawn.node.id === graph.rootId ? 0 : 1;
      if (rootA !== rootB) return rootA - rootB;
      const degrees = degreeOf(b.drawn.node.id) - degreeOf(a.drawn.node.id);
      if (degrees) return degrees;
      return a.index - b.index;
    });
  // Read after every write above; nothing below writes until every box is in.
  const boxes = order.map((entry) => entry.drawn.label.getBoundingClientRect());
  const kept = [];
  const hide = [];
  order.forEach((entry, i) => {
    const box = boxes[i];
    if (!box.width || !box.height) return;
    if (kept.some((other) => boxesTouch(box, other))) hide.push(entry.drawn);
    else kept.push(box);
  });
  hide.forEach((drawn) => {
    // Emptied, not removed and not `display: none`: the element keeps its
    // place in the group and its 16 px face, so a redraw fills it in again
    // and nothing measuring the drawing trips over a node without a label.
    drawn.label.textContent = "";
    drawn.label.setAttribute("data-hidden", "true");
  });
  sayHiddenLabels(hide.length, labelled.length);
}

/* The line under the toolbar, in the grammar the chart cards use. */
function sayHiddenLabels(hidden, total) {
  if (!labelsBox) return;
  labelsBox.hidden = !hidden;
  labelsBox.textContent = hidden
    ? `${hidden} of ${total} names hidden - they would overlap here. `
      + "Zoom in, or point at a bubble to read its name; the panel beside the "
      + "graph names the one you select."
    : "";
}

/* A piece of text at a point in the drawing, drawn at its CSS size.
 *
 * The anchor is the transform, not x/y: `scale(1/zoom)` around it undoes the
 * viewBox's zoom for the glyphs only, so the label stays with its bubble and
 * stays 16 px. */
function place(text, x, y, k) {
  if (!text) return;
  text.setAttribute("transform", `translate(${x} ${y}) scale(${k})`);
}

/* ── Drawing ─────────────────────────────────────────────────────────── */

function render() {
  const nodes = [...graph.nodes.values()];
  // Redrawing throws away the element that had the focus. Putting it back
  // is right only when the focus WAS in the drawing; stealing it from the
  // search field after a search would be worse than losing it.
  const hadFocus = canvas.contains(document.activeElement);
  canvas.textContent = "";
  svg = null;
  painted = { nodes: [], edges: [] };
  if (!nodes.length) {
    canvasMessage("Search for an entity above to draw its connections.");
    return;
  }

  svg = make("svg", {
    id: "graph-svg",
    role: "group",
    "aria-label": `Connection graph: ${nodes.length} entities, ${graph.edges.size} connections. Every entity and every connection can be reached with the Tab key; the panel beside the graph says what is selected.`,
    preserveAspectRatio: "xMidYMid meet",
  });

  const edgeLayer = make("g", { class: "g-edges" });
  const nodeLayer = make("g", { class: "g-nodes" });
  svg.appendChild(edgeLayer);
  svg.appendChild(nodeLayer);

  graph.edges.forEach((edge) => edgeLayer.appendChild(drawEdge(edge)));
  nodes.forEach((node) => nodeLayer.appendChild(drawNode(node)));

  canvas.appendChild(svg);
  refreshScale(true);
  updateViewBox();
  // THE VIEWBOX DECIDES WHERE A NAME LANDS ON THE SCREEN, and it is written
  // one line above this: the pass inside refreshScale measured the drawing
  // as it was framed a moment ago. Run once more now that the frame is the
  // one the reader will see. It is idempotent - every name goes back before
  // any box is read - so the second pass replaces the first one's answer
  // rather than adding to it.
  layoutLabels();
  wirePanning();
  markPinned();
  if (hadFocus) restoreFocus();
}

/* The drawing area with a sentence in it instead of a graph: nothing
 * searched yet, nothing found, or a request that failed. An error gets a
 * button as well - "Try again" is the difference between a page that
 * reports a problem and a page a person can get out of. */
function canvasMessage(text, opts = {}) {
  canvas.textContent = "";
  svg = null;
  painted = { nodes: [], edges: [] };
  sayHiddenLabels(0, 0);
  const box = el("div", "graph-empty");
  box.id = "graph-empty";
  box.appendChild(el("p", "graph-empty-text", text));
  if (opts.retry) {
    const again = el("button", "button", "Try again");
    again.type = "button";
    again.id = "graph-retry";
    again.addEventListener("click", opts.retry);
    box.appendChild(again);
  }
  canvas.appendChild(box);
}

/* The shape of one connection. A new edge is a straight line to the node it
 * has just brought in. A CROSS-LINK joins two nodes that were both already
 * on the canvas, and it is bowed: two neighbours of the same centre have
 * that centre exactly between them, so a straight chord would run through
 * the middle of the graph and vanish under the lines already there. */
function edgeShape(edge, a, b) {
  const mid = [(a.x + b.x) / 2, (a.y + b.y) / 2];
  if (!edge.cross) {
    return { tag: "line", attrs: { x1: a.x, y1: a.y, x2: b.x, y2: b.y }, mid };
  }
  const dx = b.x - a.x;
  const dy = b.y - a.y;
  const len = Math.hypot(dx, dy) || 1;
  const bow = Math.min(90, len * 0.2);
  const cx = mid[0] - (dy / len) * bow;
  const cy = mid[1] + (dx / len) * bow;
  return {
    tag: "path",
    attrs: { d: `M ${a.x} ${a.y} Q ${cx} ${cy} ${b.x} ${b.y}`, fill: "none" },
    // The middle of a quadratic curve, which is where the handle goes.
    mid: [(a.x + 2 * cx + b.x) / 4, (a.y + 2 * cy + b.y) / 4],
  };
}

function drawEdge(edge) {
  const a = graph.nodes.get(edge.a);
  const b = graph.nodes.get(edge.b);
  const selected = graph.selection && graph.selection.kind === "edge" && graph.selection.key === edge.key;
  const group = make("g", {
    class: `g-edge-group${selected ? " is-selected" : ""}`,
    "data-pair": edge.key,
    tabindex: "0",
    role: "button",
    "aria-label": `Connection between ${a ? a.name : edge.a} and ${b ? b.name : edge.b}: `
      + `${edge.count} ${edge.count === 1 ? "row" : "rows"} in the archive, `
      + `${edge.types.map((t) => t.name).join(", ")}`,
  });
  if (!a || !b) return group;

  const shape = edgeShape(edge, a, b);
  const line = (extra) => make(shape.tag, { ...shape.attrs, ...extra });
  group.appendChild(line({
    class: "g-edge-halo", stroke: "transparent", "stroke-width": 10,
    "vector-effect": "non-scaling-stroke",
  }));
  group.appendChild(line({
    class: `g-edge${edge.cross ? " is-cross" : ""}`,
    stroke: edge.colour, "stroke-width": 3, "stroke-opacity": edge.level > 1 ? 0.75 : 1,
    "vector-effect": "non-scaling-stroke",
  }));
  group.appendChild(line({
    class: "g-edge-hit", stroke: "transparent", "stroke-width": 16,
    "vector-effect": "non-scaling-stroke",
  }));
  // A HANDLE IN THE MIDDLE, and it is not decoration. A line's own box is
  // its geometry without the stroke, so a vertical or horizontal edge
  // measures zero pixels wide - untouchable for anything that goes by
  // element boxes, which is every screen reader, every browser automation
  // and every "click the middle of it". The handle is a square at the
  // midpoint, EDGE_TARGET_PX across on the screen whatever the zoom (WCAG
  // 2.5.8 asks 24), and the thing the focus ring is drawn around.
  const handle = make("rect", { class: "g-edge-handle", fill: "transparent" });
  group.appendChild(handle);
  // The three strokes are kept as well as the handle: dragging a node has to
  // move the lines that touch it, one attribute at a time, rather than
  // rebuilding the drawing on every pointer move.
  painted.edges.push({
    edge, handle, mid: shape.mid,
    parts: [...group.querySelectorAll(".g-edge-halo, .g-edge, .g-edge-hit")],
  });
  group.addEventListener("click", () => { if (!dragged) selectEdge(edge.key); });
  group.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); selectEdge(edge.key); }
  });
  return group;
}

function drawNode(node) {
  const selected = graph.selection && graph.selection.kind === "node" && graph.selection.key === node.id;
  const classes = ["g-node"];
  if (node.id === graph.rootId) classes.push("is-root");
  if (!node.expanded) classes.push("is-open");
  if (selected) classes.push("is-selected");
  if (node.pinned) classes.push("is-pinned");

  const group = make("g", {
    class: classes.join(" "),
    "data-id": node.id,
    "data-level": String(node.level),
    tabindex: "0",
    role: "button",
    "aria-label": `${node.name}${node.type ? `, ${node.type}` : ""}, level ${node.level}, `
      + `${connectionsWord(degreeOf(node.id))} drawn. `
      + `${node.expanded
        ? (node.id === graph.rootId
          ? "The centre of the graph."
          : "Expanded. Press Enter to collapse it.")
        : "Press Enter to draw its connections."}`
      + `${node.pinned ? " Pinned where you dropped it." : ""}`
      + " Use the arrow keys to move it.",
  });
  // The whole name in a tooltip, because the label is truncated to keep the
  // drawing readable ("smart truncation and tooltips", Cambridge
  // Intelligence). aria-label above already carries it for a screen reader.
  group.appendChild(make("title", {}, node.name));
  // The hit circle first, so the bubble is painted over it; both are in this
  // group, so either one reaches the same click handler.
  const hit = make("circle", { class: "g-node-hit", cx: node.x, cy: node.y, fill: "transparent" });
  const circle = make("circle", {
    class: "g-body", cx: node.x, cy: node.y,
    stroke: node.id === graph.rootId ? "#1d4f91" : node.colour,
    "vector-effect": "non-scaling-stroke",
  });
  group.appendChild(hit);
  group.appendChild(circle);
  // A three-digit number does not fit in a bubble; the exact count is in the
  // aria-label and in the detail panel either way.
  const drawn = degreeOf(node.id);
  const count = drawn
    ? make("text", { class: "g-count" }, drawn > 99 ? "99+" : drawn)
    : null;
  if (count) group.appendChild(count);
  const label = make("text", { class: "g-label" }, node.name);
  group.appendChild(label);
  const plus = node.expanded ? null : make("text", { class: "g-plus" }, "+");
  if (plus) group.appendChild(plus);
  [count, label, plus].forEach((t) => t && t.setAttribute("text-anchor", "middle"));
  // The "held where you dropped it" mark. Drawn for every node and shown by
  // CSS only for a pinned one, so pinning and unpinning is a class change
  // rather than a rebuild of the drawing.
  const pinMark = make("circle", { class: "g-pin-mark" });
  group.appendChild(pinMark);
  painted.nodes.push({ node, group, circle, hit, label, count, plus, pinMark });
  // toggleNode does the selecting, in every one of its outcomes. Selecting
  // here as well would rebuild the panel a second time and wipe the line it
  // had just written into it.
  group.addEventListener("click", () => {
    // `dragged` is true when the pointer travelled more than DRAG_SLOP_PX,
    // whether it was panning the canvas or moving this bubble: a drag that
    // ends on a node must not open it.
    if (dragged) return;
    graph.focusKey = `node:${node.id}`;
    toggleNode(node.id);
  });
  group.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      graph.focusKey = `node:${node.id}`;
      toggleNode(node.id);
      return;
    }
    // THE KEYBOARD CAN ARRANGE THE GRAPH TOO. Everything a mouse can do here
    // a keyboard can do; a node that could only be placed by dragging would
    // be the one drag-only function on the page. preventDefault because the
    // arrows would otherwise scroll the page out from under the drawing.
    const step = NUDGE[e.key];
    if (!step) return;
    e.preventDefault();
    nudgeNode(node.id, step[0], step[1]);
  });
  // Dragging starts here and not on the <svg>, and it stops the event
  // reaching the canvas so one gesture cannot both move a bubble and pan the
  // picture behind it.
  group.addEventListener("pointerdown", (e) => {
    if (e.button !== undefined && e.button !== 0) return;
    e.stopPropagation();
    nodeDrag = { id: node.id, moved: false };
    dragging = false;
    dragged = false;
    lastX = e.clientX;
    lastY = e.clientY;
    canvas.classList.add("is-dragging-node");
  });
  return group;
}

/* Which way each arrow moves a node, in screen pixels. */
const NUDGE = {
  ArrowLeft: [-NUDGE_PX, 0],
  ArrowRight: [NUDGE_PX, 0],
  ArrowUp: [0, -NUDGE_PX],
  ArrowDown: [0, NUDGE_PX],
};

/* Move a node by a number of SCREEN pixels and hold it there. The arrow keys
 * and the pointer both come through here, so the two gestures cannot end up
 * meaning different things. */
function nudgeNode(id, dxPx, dyPx) {
  const node = graph.nodes.get(id);
  if (!node) return;
  node.x += dxPx / graph.scale;
  node.y += dyPx / graph.scale;
  pinNode(node);
  moveDrawn(node);
  measureGaps([...graph.nodes.values()]);
  refreshScale(true);
  updatePinned();
}

function pinNode(node) {
  if (node.id === graph.rootId) return;   // the root never moved to begin with
  node.pinned = true;
}

/* Redraw the parts of the picture one node's position changes: its own
 * circles and text, and every line that ends on it. Attributes, not a
 * rebuild - a rebuild per pointer move would throw away the element the
 * pointer is on, close an open popup and lose the focus. */
function moveDrawn(node) {
  const drawn = painted.nodes.find((n) => n.node.id === node.id);
  if (drawn) {
    drawn.circle.setAttribute("cx", node.x);
    drawn.circle.setAttribute("cy", node.y);
    drawn.hit.setAttribute("cx", node.x);
    drawn.hit.setAttribute("cy", node.y);
    // The text and the mark hang off the position too; refreshScale puts
    // all four of them where they belong from the node's own coordinates.
    refreshScale(true);
  }
  painted.edges.forEach((e) => {
    if (e.edge.a !== node.id && e.edge.b !== node.id) return;
    const a = graph.nodes.get(e.edge.a);
    const b = graph.nodes.get(e.edge.b);
    if (!a || !b) return;
    const shape = edgeShape(e.edge, a, b);
    e.parts.forEach((part) => {
      Object.entries(shape.attrs).forEach(([k, v]) => part.setAttribute(k, String(v)));
    });
    e.mid = shape.mid;
  });
}

/* How many nodes are held where somebody put them, said out loud. A pin is
 * state, and state that is not on the screen is state nobody can undo. */
function updatePinned() {
  if (!pinnedBox) return;
  const pinned = [...graph.nodes.values()].filter((n) => n.pinned);
  // THE LINE KEEPS ITS ROW EVEN WHEN IT HAS NOTHING TO SAY. It sits directly
  // above a drawing of a fixed height, so appearing would push the whole
  // graph down - and the moment it appears is the moment a node has just
  // been dropped, which would move the drawing out from under the hand that
  // dropped it. Empty text rather than `hidden`: an empty paragraph is
  // nothing to a screen reader and 24 px to the layout, which is exactly
  // what is wanted (graph.css reserves the row).
  pinnedBox.hidden = false;
  pinnedBox.textContent = !pinned.length ? ""
    : (pinned.length === 1
      ? "1 node is pinned where you dropped it. Reset lets it go."
      : `${pinned.length} nodes are pinned where you dropped them. Reset lets them go.`);
}

/* Let go of every pinned node. The Reset button does this as well as fitting
 * the view, and its own title says so - a control that quietly undoes
 * somebody's arrangement is a control nobody can predict. */
function unpinAll() {
  let count = 0;
  graph.nodes.forEach((n) => { if (n.pinned) { n.pinned = false; count += 1; } });
  return count;
}

function restoreFocus() {
  if (!graph.focusKey || !svg) return;
  const [kind, ...rest] = graph.focusKey.split(":");
  const key = rest.join(":");
  const target = kind === "node"
    ? svg.querySelector(`.g-node[data-id="${CSS.escape(key)}"]`)
    : svg.querySelector(`[data-pair="${CSS.escape(key)}"]`);
  if (target && document.activeElement !== target) target.focus({ preventScroll: true });
}

/* ── Panning and zooming ─────────────────────────────────────────────── */

let dragging = false;
let dragged = false;
let lastX = 0;
let lastY = 0;
/* The node being dragged, or null while the gesture is a pan. `moved` is
 * what tells a drag from a click, by DISTANCE, the same way the pan does. */
let nodeDrag = null;

/* The drag listeners live on the window, so a pointer that leaves the SVG
 * mid-drag still moves the graph - and they are registered ONCE, not on
 * every redraw, or every expansion would add another copy of them. */
window.addEventListener("pointermove", onPointerMove);
window.addEventListener("pointerup", onPointerUp);

function wirePanning() {
  if (!svg) return;
  svg.addEventListener("pointerdown", (e) => {
    if (e.button !== undefined && e.button !== 0) return;
    dragging = true;
    dragged = false;
    lastX = e.clientX;
    lastY = e.clientY;
    canvas.classList.add("is-panning");
  });
  // No setPointerCapture: it would retarget the click to the <svg>, and a
  // click on a bubble has to reach the bubble.
  svg.addEventListener("wheel", (e) => {
    e.preventDefault();
    zoomBy(e.deltaY < 0 ? 1.15 : 1 / 1.15);
  }, { passive: false });
}

function onPointerMove(e) {
  const dx = e.clientX - lastX;
  const dy = e.clientY - lastY;
  if (nodeDrag) {
    // A BUBBLE IS BEING MOVED, not the picture. The threshold is the same
    // five pixels the pan uses, so a hand that shakes on a click does not
    // pin a node and swallow the click that was meant.
    if (Math.abs(dx) + Math.abs(dy) > DRAG_SLOP_PX) {
      nodeDrag.moved = true;
      dragged = true;
    }
    lastX = e.clientX;
    lastY = e.clientY;
    if (!nodeDrag.moved) return;
    const node = graph.nodes.get(nodeDrag.id);
    if (!node) return;
    node.x += dx / graph.scale;
    node.y += dy / graph.scale;
    moveDrawn(node);
    return;
  }
  if (!dragging) return;
  if (Math.abs(dx) + Math.abs(dy) > DRAG_SLOP_PX) dragged = true;
  graph.tx += dx / graph.scale;
  graph.ty += dy / graph.scale;
  lastX = e.clientX;
  lastY = e.clientY;
  updateViewBox();
}

function onPointerUp() {
  if (nodeDrag) {
    const node = graph.nodes.get(nodeDrag.id);
    const moved = nodeDrag.moved;
    nodeDrag = null;
    canvas.classList.remove("is-dragging-node");
    if (node && moved) {
      // IT STAYS WHERE IT WAS DROPPED. Pinning is what makes that true the
      // next time a ring is added and the solver runs (resolveCollisions).
      pinNode(node);
      measureGaps([...graph.nodes.values()]);
      refreshScale(true);
      markPinned();
      updatePinned();
      const said = `${node.name} moved. It stays where you dropped it until you drag it again or press Reset.`;
      setStatus(said);
      announce(said);
    }
    // `dragged` has to survive until the click that follows this pointerup,
    // or a drag that ended on a bubble would open it.
    window.setTimeout(() => { dragged = false; }, 0);
    return;
  }
  if (!dragging) return;
  dragging = false;
  canvas.classList.remove("is-panning");
  window.setTimeout(() => { dragged = false; }, 0);
}

/* The pinned nodes carry a class, so the drawing says which ones are held
 * as well as the line above it saying how many. */
function markPinned() {
  if (!svg) return;
  painted.nodes.forEach((drawn) => {
    drawn.group.classList.toggle("is-pinned", Boolean(drawn.node.pinned));
  });
}

/* ── The right column ────────────────────────────────────────────────── */

/* The legend is the WHOLE colour scale as long as nothing is drawn, and the
 * groups actually on the canvas once something is - with the number of
 * connections drawn in each. Both readings are true and neither can drift
 * from the edges, because the counts are taken from the edges. */
function updateLegend(groups) {
  if (!legend) return;
  (groups || []).forEach((g) => {
    if (!allGroups.some((known) => known.key === g.key)) allGroups.push({ ...g });
  });
  const counts = new Map();
  graph.edges.forEach((e) => counts.set(e.group, (counts.get(e.group) || 0) + 1));
  const withCounts = allGroups.map((g) => ({ ...g, count: counts.get(g.key) || 0 }));
  const used = withCounts.filter((g) => g.count > 0);
  legend.update(used.length ? used : withCounts.map((g) => ({ ...g, count: undefined })));
}

function setStatus(text) {
  if (summaryBox) summaryBox.textContent = text || "";
}

function showDetail(nodes) {
  if (!detailBox) return;
  detailBox.textContent = "";
  const title = el("h2", "card-title", "Details");
  title.id = "detail-title";
  detailBox.appendChild(title);
  (Array.isArray(nodes) ? nodes : [nodes]).forEach((n) => n && detailBox.appendChild(n));
}

function appendNote(text) {
  if (!detailBox) return;
  const note = el("p", "detail-note", text);
  detailBox.appendChild(note);
}

/* A breakdown with its shares - "count (percent%)", in this panel and in
 * the map's connection popup. The arithmetic is on numbers the answer
 * already carries: without it "3" beside "Supplier" is a
 * number with nothing to compare it to, and a reader cannot tell a pair that
 * is mostly one relationship from one that is evenly split. */
function typeList(types) {
  const list = el("ul", "detail-types");
  const rows = types || [];
  const total = rows.reduce((sum, t) => sum + (Number(t.count) || 0), 0);
  // EIGHT NAMES BESIDE EIGHT IDENTICAL BROWN SQUARES is what this panel drew
  // on an archive where no connection type is grouped - a colour column that
  // asserts a coding nobody has made. The sentence under the list says so
  // once instead (legend.js).
  const ungrouped = allUngrouped(rows);
  rows.forEach((t) => {
    const item = el("li");
    if (!ungrouped) {
      const swatch = el("span", "swatch");
      swatch.style.background = t.colour || "#777";
      swatch.setAttribute("aria-hidden", "true");
      item.appendChild(swatch);
    }
    item.appendChild(el("span", "type-name", t.name || "-"));
    const count = Number(t.count) || 0;
    const pct = total ? `${Math.round((count / total) * 100)}%` : "";
    item.appendChild(el("span", "type-count",
      t.count === undefined || t.count === null ? ""
        : (pct ? `${count} (${pct})` : String(count))));
    list.appendChild(item);
  });
  if (!ungrouped) return list;
  // The panel takes a list, so the sentence travels with it: a fragment
  // holding the rows and the one line that explains their colour.
  const box = document.createDocumentFragment();
  box.appendChild(list);
  box.appendChild(ungroupedNote());
  return box;
}

function contextHref(path, q) {
  const url = new URL(path, window.location.origin);
  url.searchParams.set("q", q);
  if (state.project) url.searchParams.set("project", state.project);
  if (state.language) url.searchParams.set("language", state.language);
  return url.pathname + url.search;
}

function selectNode(id, opts = {}) {
  const node = graph.nodes.get(id);
  if (!node) return;
  graph.selection = { kind: "node", key: id };
  markSelection();

  const parts = [];
  const sub = el("p", "detail-sub");
  sub.textContent = [node.type || (node.kind === "bucket" ? "Bucket" : "Unknown type"),
    `level ${node.level}`,
    node.expanded ? "expanded" : "not expanded yet",
    node.pinned ? "pinned where you dropped it" : ""].filter(Boolean).join(" - ");
  parts.push(headline(node.name), sub);

  if (node.kind === "bucket" && node.members && node.members.length) {
    parts.push(el("p", "detail-sub",
      `Bucket of ${node.members.length}: ${node.members.map((m) => m.name).join(", ")}`));
  }

  const own = [];
  graph.edges.forEach((edge) => {
    if (edge.a === id || edge.b === id) own.push(edge);
  });
  if (own.length) {
    const types = new Map();
    own.forEach((edge) => (edge.types || []).forEach((t) => {
      const seen = types.get(t.name) || { ...t, count: 0 };
      seen.count += t.count || 0;
      types.set(t.name, seen);
    }));
    parts.push(el("p", "detail-sub", `${own.length} ${own.length === 1 ? "connection" : "connections"} drawn:`));
    parts.push(typeList([...types.values()].sort((a, b) => b.count - a.count)));
  } else {
    parts.push(el("p", "detail-empty", "No connections drawn for this entity yet."));
  }

  const actions = el("div", "detail-actions");
  if (!node.expanded) {
    const expand = el("button", "button", "Draw its connections");
    expand.type = "button";
    expand.addEventListener("click", () => expandNode(id));
    actions.appendChild(expand);
  } else if (id !== graph.rootId) {
    // The same switch the click and Enter are, as a button: a gesture that
    // exists only on the drawing is a gesture half the readers never find.
    const close = el("button", "button button--secondary", "Collapse it");
    close.type = "button";
    close.addEventListener("click", () => collapseNode(id));
    actions.appendChild(close);
  }
  if (node.pinned) {
    const release = el("button", "button button--secondary", "Let it go");
    release.type = "button";
    release.addEventListener("click", () => {
      node.pinned = false;
      markPinned();
      updatePinned();
      selectNode(id, { focus: false });
      const said = `${node.name} is no longer pinned. It will move with the layout again.`;
      setStatus(said);
      announce(said);
    });
    actions.appendChild(release);
  }
  const onMap = el("a", "button button--secondary", "Show on map");
  onMap.href = contextHref("/map", node.name);
  actions.appendChild(onMap);
  const inDiagrams = el("a", "button button--secondary", "Diagrams");
  inDiagrams.href = contextHref("/diagrams/entity", node.name);
  actions.appendChild(inDiagrams);
  parts.push(actions);

  showDetail(parts);
  if (opts.focus) detailBox.focus();
}

function selectEdge(key) {
  const edge = graph.edges.get(key);
  if (!edge) return;
  graph.selection = { kind: "edge", key };
  graph.focusKey = `edge:${key}`;
  markSelection();

  const a = graph.nodes.get(edge.a);
  const b = graph.nodes.get(edge.b);
  const parts = [headline(`${a ? a.name : edge.a} ⇄ ${b ? b.name : edge.b}`)];
  const facts = [`${edge.count} in the archive`];
  if (edge.cross) facts.push("both were already on the graph");
  if (edge.first) facts.push(`from ${fmtDate(edge.first, { time: false })}`);
  if (edge.last && edge.last !== edge.first) facts.push(`to ${fmtDate(edge.last, { time: false })}`);
  parts.push(el("p", "detail-sub", facts.join(" - ")));
  parts.push(typeList(edge.types));
  showDetail(parts);
  if (legend) legend.highlight(edge.group);
}

function headline(text) {
  const head = el("h2", null, text);
  head.id = "detail-title";
  return head;
}

function markSelection() {
  if (!svg) return;
  svg.querySelectorAll(".is-selected").forEach((n) => n.classList.remove("is-selected"));
  if (!graph.selection) return;
  const target = graph.selection.kind === "node"
    ? svg.querySelector(`.g-node[data-id="${CSS.escape(graph.selection.key)}"]`)
    : svg.querySelector(`[data-pair="${CSS.escape(graph.selection.key)}"]`);
  if (target) target.classList.add("is-selected");
}

function errorPanel(what, err, retry) {
  const box = el("div", "notice notice--error");
  const body = el("div", "notice-body");
  body.appendChild(el("p", "notice-what", `${what} ${sentence(err && err.message)}`.trim()));
  const hint = sentence(err && err.hint);
  if (hint) body.appendChild(el("p", "notice-hint", hint));
  if (retry) {
    const again = el("button", "button", "Try again");
    again.type = "button";
    again.addEventListener("click", retry);
    body.appendChild(again);
  }
  box.appendChild(body);
  return box;
}

function notFoundPanel(term) {
  const box = el("div", "notice notice--warn");
  const body = el("div", "notice-body");
  body.appendChild(el("p", "notice-what", `Nothing in this project is called “${term}”.`));
  body.appendChild(el("p", "notice-hint",
    "Check the project and language in the top bar, or pick a name from the suggestions."));
  box.appendChild(body);
  return box;
}

/* ── Start ───────────────────────────────────────────────────────────── */

/* Nothing searched: an empty drawing that says so, which is what the
 * "- clear the search -" row in the suggestion list asks for. Every other
 * view's search box has that row; this one had no way back to a blank page
 * at all except editing the URL. */
function clearGraph() {
  graph.nodes.clear();
  graph.edges.clear();
  graph.aliases.clear();
  graph.rootId = null;
  graph.selection = null;
  graph.focusKey = null;
  graph.tx = 0; graph.ty = 0; graph.scale = 1;
  render();
  updateLegend();
  updatePinned();
  showResolved(null, "");
  setStatus("");
  showDetail(el("p", "detail-empty",
    "Nothing selected. Click a node or a connection, or reach them with the Tab key."));
}

function draw(term) {
  if (term) startGraph(term);
  else clearGraph();
}

function wireControls() {
  if (form) {
    form.addEventListener("submit", (e) => {
      e.preventDefault();
      const term = termFromForm();
      // The field, the hidden input and the URL say the same thing from
      // here on: what was typed has just been drawn, so it is what is set.
      fillFromUrl(term);
      setState({ q: term });
      draw(term);
    });
    // Enter asks, a click in the list does not. The term is taken either
    // way; drawing a well-connected entity's network is a long query, and
    // starting one on every click in a suggestion list means somebody is
    // waiting for a picture they had not asked for yet.
    form.addEventListener("typeahead:choose", (e) => {
      const term = (e.detail && e.detail.value) || "";
      fillFromUrl(term);
      setState({ q: term });
      if (e.detail && e.detail.ask) draw(term);
    });
  }
  // A DATE RANGE IS PART OF THE QUESTION, so changing one asks it again -
  // and it goes into the URL first, so the request that follows carries it.
  [fromField, toField].forEach((field) => {
    if (!field) return;
    field.addEventListener("change", () => {
      setExtra(field === fromField ? "from" : "to", field.value);
      // The whole graph is drawn again rather than re-filtered in place: a
      // ring somebody opened may have nothing in it inside the new range,
      // and a picture half in and half out of a date range is not a picture
      // of anything.
      draw(state.q);
    });
  });
  const zoomIn = document.getElementById("graph-zoom-in");
  const zoomOut = document.getElementById("graph-zoom-out");
  const reset = document.getElementById("graph-reset");
  if (zoomIn) zoomIn.addEventListener("click", () => zoomBy(ZOOM_STEP));
  if (zoomOut) zoomOut.addEventListener("click", () => zoomBy(1 / ZOOM_STEP));
  if (reset) {
    reset.addEventListener("click", () => {
      // RESET UNDOES BOTH THINGS THE READER CAN DO TO THE VIEW: the zoom and
      // the pan (fitView), and the arrangement (unpinAll). The button's own
      // title says so, so nothing here is a surprise.
      const freed = unpinAll();
      if (freed) { layoutNewNodes(); render(); }
      fitView();
      markPinned();
      updatePinned();
      const said = freed
        ? `Fitted to the whole graph and let go of ${freed} ${freed === 1 ? "node" : "nodes"} you had dragged.`
        : "Fitted to the whole graph.";
      setStatus(said);
      announce(said);
    });
  }

  let resizeTimer = null;
  window.addEventListener("resize", () => {
    if (resizeTimer) window.clearTimeout(resizeTimer);
    resizeTimer = window.setTimeout(() => updateViewBox(), 120);
  });
}

async function loadLegend() {
  if (!legendBox) return;
  legend = renderLegend(legendBox, [], {
    title: "Connection colours",
    // Say it once when nothing is grouped, and drop the swatch column
    // rather than repeating one brown down the list (legend.js).
    ungrouped: true,
    emptyText: "The colour groups could not be loaded.",
  });
  try {
    const data = await api("/api/graph/legend", { channel: "legend", quiet: true });
    allGroups = (data.groups || []).map((g) => ({ ...g }));
    updateLegend();
  } catch (err) {
    if (!isAbort(err)) legend.update([]);
  }
}

function init() {
  if (!canvas) return;
  wireControls();
  loadLegend();
  fillFromUrl(state.q);
  if (fromField) fromField.value = getExtra("from");
  if (toField) toField.value = getExtra("to");
  updatePinned();
  if (state.q) startGraph(state.q);
}

init();
