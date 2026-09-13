/* ==========================================================================
 *  The Map view.
 *
 *  Leaflet, vendored, loaded by the template as a plain script (so `L` is a
 *  global here), plus markercluster for the pins. What is drawn:
 *
 *    pins    one per (entity, address) the archive holds coordinates for,
 *            clustered while they crowd, with a popup that names the entity
 *            and its address. A REAL PIN, anchored at its tip: a circle
 *            centred on a coordinate is wrong by its own radius, and the
 *            thing it marks is underneath it. The pin of the entity that was
 *            SEARCHED FOR is drawn in the accent colour and says "searched"
 *            in its title, its popup and its row - "which pin is the thing I
 *            typed" is a daily question, and the picture answers it.
 *            Two entities at ONE address are nudged apart so neither hides
 *            the other (`spread`);
 *    lines   one per pair of connected entities, in the colour of the
 *            relationship's GROUP (app/colours.py, never a regex here) and
 *            as thick as the pair is busy. Every one of them is an ARC and
 *            not a chord (`arcPath`), because a straight line covers the
 *            places that lie on it.
 *
 *  THE LISTS ARE THE MAP. Every pin and every line is also a row in the
 *  right-hand column, and activating a row opens the same popup. That is
 *  the text alternative the map needs (MN.gov's interactive-map guidance,
 *  WCAG 1.1.1) and it is also the only way to reach a line by keyboard -
 *  Leaflet makes markers focusable, polylines not. The two have to be
 *  usable TOGETHER, which is why the map is sticky (map.css) and why
 *  activating a row also scrolls the map into view and says in words what
 *  the popup now shows.
 *
 *  ONE FILTER, TWO WIDGETS. The legend and the type checklist filter the
 *  same lines. They are both drawn from one set of switched-off type names,
 *  which lives in the URL as `hide=` - two sets meant two answers to one
 *  question, and a customer being told by one widget that nothing was
 *  hidden while the other had hidden it.
 *
 *  TWO CHECKLISTS, BECAUSE THERE ARE TWO KINDS OF THING ON THE MAP. The
 *  connection types filter the LINES (`hide=`) and the location types filter
 *  the PINS (`hidepins=`). The second one is the one thing a person does
 *  with a map full of pins: "show me the factories". Both are client-side - the answer
 *  already holds every pin and every line - and both have a box that narrows
 *  the LIST (never the map: a filter you cannot see the whole of is a filter
 *  you cannot undo) and All / None in one gesture each.
 *
 *  ONE BOX, TWO QUESTIONS. The term is read either as ONE THING - an entity
 *  or a bucket - or as an EVENT TYPE, and the switch in front of the field
 *  says which. An event type resolves to the entities its events are about
 *  (app/scope.py), so the map of "Product launch" is the map of everybody a
 *  product launch happened to, with the same levels and the same lines.
 *  Nothing after the request knows which axis was asked - only the words
 *  around the picture change, and they have to: "Company - 42 entities"
 *  reads differently from "Apple Inc. - 1 entity".
 *
 *  THE PICTURE CAN LEAVE THE PAGE. "Save image" writes what is on the
 *  screen to a PNG - with the colour key and the attribution in it, and
 *  without the controls (static/js/mapimage.js says why each of those).
 *
 *  Nothing here hides information behind hover: a popup opens on click or
 *  on Enter, and everything a popup says is in the list as well.
 * ========================================================================== */

import { api, isAbort, fmtInt, watchSearch } from "./api.js";
import { pinIcon } from "./mappin.js";
import { arcPath as arcOf, fanFactor } from "./mapline.js";
import { state, set as setState, setExtra, getExtra } from "./state.js";
import { announce } from "./a11y.js";
import { allUngrouped, renderLegend, ungroupedNote } from "./legend.js";
import { tileUrl } from "./minimap.js";
import { newTabIcon, viewIcon } from "./icons.js";
import { saveMapImage, addSaveControl } from "./mapimage.js";
import "./typeahead.js";

const ATTRIBUTION = '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors';
/* The same credit as words, for the picture: a canvas has no links, and the
 * file needs the text. Derived from the one constant above rather than
 * written twice, so a deployment that changes the credit changes both. */
const ATTRIBUTION_TEXT = ATTRIBUTION.replace(/<[^>]*>/g, "");
const LEVELS = ["1", "2", "3"];

const form = document.getElementById("map-form");
/* Every request on these channels turns the button quiet and writes
 * "Searching…" beside it, without this file having to remember to
 * (static/js/api.js: watchSearch). */
watchSearch(form, "map");
const statusBox = document.getElementById("map-status");
const captionBox = document.getElementById("map-caption");
const mapBox = document.getElementById("map");
const legendBox = document.getElementById("map-legend");
const typesCard = document.getElementById("map-types-card");
const typesList = document.getElementById("map-types");
const typesAll = document.getElementById("types-all");
const typesNone = document.getElementById("types-none");
const typesFind = document.getElementById("map-types-find");
const typesNote = document.getElementById("map-types-note");
const typesCount = document.getElementById("map-types-count");
const placeTypesCard = document.getElementById("map-places-types-card");
const placeTypesList = document.getElementById("map-place-types");
const placeTypesAll = document.getElementById("place-types-all");
const placeTypesNone = document.getElementById("place-types-none");
const placeTypesFind = document.getElementById("map-place-types-find");
const placeTypesNote = document.getElementById("map-place-types-note");
const placeTypesCount = document.getElementById("map-place-types-count");
const placesList = document.getElementById("map-places");
const placesCount = document.getElementById("places-count");
const linesList = document.getElementById("map-lines");
const linesCount = document.getElementById("lines-count");
/* CONNECTIONS OR LOCATIONS - one question at a time (templates/map.html
 * says why). `mode` is the whole state; the two helpers below read like the
 * checkboxes they replaced so the call sites still say what they mean. */
const modeSwitch = document.querySelector("[data-map-mode]");
let mode = "connections";
const drawingConnections = () => mode === "connections";

/* Sets the mode, presses the right half of the switch and writes it into the
 * URL, so a reloaded page draws what it drew (state.js keeps the rest). */
function setMode(next) {
  mode = next === "locations" ? "locations" : "connections";
  setExtra("mode", mode === "connections" ? "" : mode);
  syncGrouping();
  if (!modeSwitch) return;
  modeSwitch.querySelectorAll("[data-mode]").forEach((button) => {
    button.setAttribute("aria-pressed", button.dataset.mode === mode ? "true" : "false");
  });
}
/* ENTITIES OR BUCKETS - what a node IS, which is a different question from
 * what is drawn (mode, above). The archive stores connections between
 * entities; a bucket is this dashboard's own idea of which of them a reader
 * means as one thing, and the server does the regrouping on the network it
 * already fetched (routers/api_map.py: _regroup says how).
 *
 * Only Connections has ends to group, so the switch is hidden on Locations
 * rather than disabled: a control that is present and refuses is a control a
 * reader tries twice. */
const groupSwitch = document.querySelector("[data-map-group]");
const groupField = document.getElementById("group-field");
let grouping = "entities";

function setGrouping(next) {
  grouping = next === "buckets" ? "buckets" : "entities";
  setExtra("group", grouping === "entities" ? "" : grouping);
  if (!groupSwitch) return;
  groupSwitch.querySelectorAll("[data-group]").forEach((button) => {
    button.setAttribute("aria-pressed", button.dataset.group === grouping ? "true" : "false");
  });
}

function syncGrouping() {
  if (groupField) groupField.hidden = !drawingConnections();
}

const levelButtons = document.getElementById("map-levels");
const retryBox = document.getElementById("map-retry");
const retryButton = document.getElementById("map-retry-button");
const imageButton = document.getElementById("map-image");
const axisSwitch = form ? form.querySelector("[data-axis-switch]") : null;

let map = null;
let markerLayer = null;
let lineLayer = null;
let legend = null;
let answer = null;
/* WHICH QUESTION THE BOX IS ASKING. "object" is one entity or a bucket;
 * "type" is an EVENT TYPE, and then the map holds every entity the events
 * of that kind are about (app/routers/api_map.py: MAP_AXES). The server
 * renders the page with it already decided from ?axis=, and this reads that
 * decision back off the form rather than re-deriving it, so the page and
 * the server cannot disagree about what was asked. It lives in the URL for
 * the same reason `q` does: a reload, a shared link and the Export menu all
 * have to show the same picture. */
let axis = (form && form.dataset.axis === "type") ? "type" : "object";

/* What the field asks for on each axis: the label, the suggestion list, the
 * placeholder. The template renders the current one (templates/map.html
 * holds the same table); this is the other one, so the switch can change
 * the field without a round trip. */
const AXIS_FIELDS = {
  object: ["Entity or bucket", "/api/suggest/entity_or_bucket", "Search for an entity or bucket"],
  type: ["Event type", "/api/suggest/event_type", "Search for an event type"],
};

/* The word for what one axis searches, for the sentences that say what was
 * found and what was not. */
const AXIS_SUBJECT = { object: "entity", type: "event type" };

/* THE ONE FILTER STATE ON THIS PAGE.
 *
 * The type names that are switched OFF - kept as "off" rather than "on" so a
 * type that appears for the first time after a wider search is shown, not
 * silently filtered away. It lives in the URL as `hide=`, which is what makes
 * a filtered map survive a reload, a Print and an Export.
 *
 * The legend keeps no SECOND set of its own, of colour groups. Two switches
 * filtering the same lines while neither shows the other's state means a
 * customer who switches "Supplier" off in the legend still sees Supplier
 * ticked in the checklist, and a screen reader is told aria-pressed="true"
 * about a group that is off. So the legend holds no state - a group is off
 * when every type in it is off, and both widgets are drawn from this one
 * set (`applyFilters`). */
let offTypes = new Set();
/* The LOCATION type names that are switched off, kept the same way and in
 * the URL as `hidepins=`. A separate set from `offTypes` because it filters
 * a different thing - the pins, not the lines - and one set for two
 * questions is how the legend and the checklist came to disagree. */
let offPlaceTypes = new Set();
/* What is typed into the two boxes above the checklists. They narrow the
 * LISTS and never the map. */
let typeFilter = "";
let placeTypeFilter = "";
const markers = new Map();   // "entity|address" -> marker
const lines = new Map();     // "a|b" -> the polyline the popup is on
/* How much wider than the colour the invisible click target is. 20 px around
 * a 1.5 px line is a 21.5 px band, which is the 24 px WCAG 2.5.8 asks for
 * once the fan puts two of them side by side. It paints nothing. */
const HIT_MARGIN = 20;
/* How many kinds of connection one popup lists. Twelve is what fits in the
 * box without scrolling on the shortest screen this dashboard is laid out
 * for; past that the popup is a list somebody has to scroll to reach the
 * links under it. */
const POPUP_TYPE_ROWS = 12;
/* Every drawn connection with the three strokes that make it up and the
 * geometry it was drawn from, so `reflowArcs` can bend them again at the
 * new zoom without rebuilding the layer (which would close an open popup
 * and re-cluster every pin). */
let arcs = [];
const typeChecks = new Map();  // type name -> its checkbox, for syncing
const placeTypeChecks = new Map();
/* Where a pin is actually DRAWN. Two entities at one address are one pixel
 * in the archive; `spread` moves them apart, and everything that has to
 * agree with a pin - the line that ends on it, the "show me this one"
 * button in the list, the fit - reads the drawn position from here rather
 * than from the answer. `drawnPlaces` is keyed like `markers`
 * ("entity|address"), `drawnEntities` by entity id for the line ends. */
const drawnPlaces = new Map();
const drawnEntities = new Map();

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function levelValue() {
  const value = getExtra("levels");
  return LEVELS.includes(value) ? value : "1";
}

function setStatus(text, warn) {
  if (!statusBox) return;
  statusBox.textContent = text || "";
  statusBox.classList.toggle("is-warning", Boolean(warn));
}

/* THE WAY OUT OF A FAILED REQUEST.
 *
 * With the endpoint at 500 the page must not throw the search away: a
 * caption falling back to "Search for an entity to show it on the map."
 * while the term is still in the field, the server's own hint ("boom. try
 * again") as the only text, and both lists saying "Nothing on the map yet."
 * are three statements that are each false. The graph does it this way:
 * a sentence of our own, the server's hint after it, and a button that
 * repeats the request. This is the same, on the same wording. */
let retryTask = null;
/* Whether the last request failed. The two lists say "Nothing on the map
 * yet." when nothing was searched, and that is the wrong sentence after a
 * 500: nothing was searched is a state a reader caused, a failed request is
 * not. */
let failed = false;

function showRetry(again) {
  if (!retryBox) return;
  retryTask = again || null;
  retryBox.hidden = !again;
}

/* A fragment from the API, finished into a sentence. `{error, hint}` are
 * written as lowercase fragments without a full stop, because the toast
 * prints them as two lines; anything that puts them into running text has to
 * close them (the same helper as graph.js, dashboard.js and diagrams.js). */
function sentence(text) {
  const clean = String(text == null ? "" : text).trim();
  if (!clean) return "";
  const capital = clean.charAt(0).toUpperCase() + clean.slice(1);
  return /[.!?\u2026]$/.test(capital) ? capital : `${capital}.`;
}

/* One line a person can act on: what failed, what the server said, and what
 * to do about it. */
function errorSentence(what, err) {
  // What failed first, because it is the fact; what the server said and what
  // to do about it follow it.
  return [what, sentence(err && err.message), sentence(err && err.hint)]
    .filter(Boolean).join(" ");
}

/* ── The map itself ──────────────────────────────────────────────────── */

function createMap() {
  if (!window.L || !mapBox) {
    if (mapBox) {
      mapBox.textContent = "";
      mapBox.appendChild(el("p", "map-empty",
        "The map could not be loaded. The places and connections are listed beside it."));
    }
    return null;
  }
  const L = window.L;
  const instance = L.map(mapBox, {
    // THE WHEEL ZOOMS, PLAINLY. This view is the whole window - there is
    // nothing to scroll past it, so the wheel has no other job here. (The
    // Query page's small map sits inside a form the reader scrolls and has
    // to be asked first; see the header of minimap.js.) The +/- buttons
    // stay: a wheel is not available to everyone, and replacing a visible
    // control with a hidden gesture is a step back.
    scrollWheelZoom: true,
    keyboard: true,
    zoomControl: true,
  }).setView([20, 0], 2);
  L.tileLayer(tileUrl(), {
    maxZoom: 18,
    // crossOrigin so the PDF export can put the tiles on the page.
    crossOrigin: true,
    attribution: ATTRIBUTION,
  }).addTo(instance);
  lineLayer = L.layerGroup().addTo(instance);
  markerLayer = L.markerClusterGroup
    ? L.markerClusterGroup({ showCoverageOnHover: false, maxClusterRadius: 45 })
    : L.layerGroup();
  instance.addLayer(markerLayer);

  /* A POPUP NEVER LEAVES THE MAP IT BELONGS TO.
   *
   * Leaflet caps a popup at 300 px wide and NOTHING tall, and a connection
   * popup has a table of up to twelve rows plus four links under it. On the
   * map as it is laid out here that runs off the bottom edge: the buttons -
   * the only part one can act on - are the part that is cut, and no amount
   * of scrolling reaches them, because it is the map that is scrolled, not
   * the popup. `autoPan` cannot save it either; a popup taller than the
   * container cannot be panned into view, so it silently gives up.
   *
   * So the size is taken from the map at the moment the popup opens, which
   * is the only time both are known: the container's own width and height
   * less the tip, the shadow and the room autoPan needs to work in. Anything
   * longer than that scrolls INSIDE the popup - `leaflet-popup-scrolled` is
   * Leaflet's own class for it - and every row stays reachable.
   *
   * `update()` rather than new options at bind time: the map may be resized,
   * the window may be, and a popup bound at 1440 px is opened at 1024. */
  instance.on("popupopen", (e) => {
    const popup = e.popup;
    if (!popup || !popup.options) return;
    const size = instance.getSize();
    // 40 px of side room and 110 px of vertical room: the tip is 20, the
    // anchor lifts the box another 20 off the point, and autoPan wants a
    // little of the container left over or it pans for ever.
    const maxWidth = Math.max(220, Math.min(360, size.x - 40));
    const maxHeight = Math.max(160, size.y - 110);
    /* THE PLACING IS TAKEN OFF LEAFLET AND DONE HERE, IN ONE STEP.
     *
     * `autoPan` ANIMATES, always: `panBy(offset, {animate: false})` still
     * animates when the offset fits inside the map, because its guard reads
     * `options.animate !== true` rather than `=== false`. Two of those pans
     * run for every popup - one when Leaflet adds it at the default size, one
     * for the size set here - and on a clustered map the movement makes
     * markercluster re-cluster, which moves the marker, which re-opens the
     * popup, which pans again. Measured: the close button of a pin's popup
     * never stood still long enough to be clicked, and a click on it timed
     * out after thirty seconds.
     *
     * So autoPan is switched off and `keepInside` below does the same job
     * synchronously, while the popup is laid out and nothing is moving. */
    if (popup.options.maxWidth !== maxWidth || popup.options.maxHeight !== maxHeight) {
      popup.options.maxWidth = maxWidth;
      popup.options.maxHeight = maxHeight;
      popup.update();
    }
    /* AND WHERE THERE IS NO ROOM ABOVE THE POINT, IT OPENS LOWER.
     *
     * A popup opens above its anchor, and on a map fitted to two places on
     * opposite sides of the world that anchor is near the top edge AND the
     * map is already at its vertical limit - so panning cannot make room
     * that is not there. Measured at 1024x768: a connection popup 309 px
     * tall opened with its top 36 px above the map, and the heading of the
     * thing just clicked was off the screen.
     *
     * `offset` moves the box down the anchor rather than cutting it: the
     * whole popup is still there and still readable, it simply overlaps the
     * point it belongs to, which is the lesser of the two evils on a map
     * that cannot be scrolled any further. Sizing it to the room instead was
     * tried first and is worse - the popup then scrolls inside itself, and
     * how much of it a reader sees depends on where the map happened to
     * settle. */
    const shift = shiftDown(instance, popup);
    if (shift !== popup.options.offset[1]) {
      popup.options.offset = L.point(popup.options.offset[0] || 0, shift);
      popup.update();
    }
    keepInside(instance, popup);
  });

  /* HOW FAR DOWN ITS ANCHOR A POPUP HAS TO OPEN to stay inside the map: the
   * amount by which its top would otherwise be above the top edge, and zero
   * whenever there is room. Measured from the element Leaflet has already
   * laid out, so it is the real height with the real content in it. */
  function shiftDown(map, popup) {
    const box = popup.getElement && popup.getElement();
    if (!box || !popup.getLatLng) return 0;
    const at = map.latLngToContainerPoint(popup.getLatLng());
    const height = box.offsetHeight;
    if (!height) return 0;
    const pad = 8;
    // Where the top of the box sits now, in the map's own coordinates: the
    // anchor, less the height, less the tip Leaflet leaves under it.
    const top = at.y - height - 12;
    return top < pad ? Math.round(pad - top) : 0;
  }

  /* WHAT IS LEFT OF THE POPUP OUTSIDE THE MAP, TAKEN OUT IN ONE STEP.
   *
   * Leaflet's `autoPan` gets it nearly right and stops a few pixels short:
   * its padding is measured against its own idea of the box, and the tip,
   * the shadow and the anchor offset are not all in that number. Measured at
   * 1024x768, a popup it considered placed sat 3 px above the top edge of
   * the map - which is not a rounding error to a reader, it is a heading cut
   * in half.
   *
   * `setView` and not `panBy`: see the comment on the caller. This is the
   * arithmetic of Leaflet's own non-animated branch, asked for in a way that
   * cannot be talked out of it, so the correction lands in one frame and
   * nothing is left moving underneath it. */
  function keepInside(map, popup) {
    const box = popup.getElement && popup.getElement();
    if (!box) return;
    const rect = box.getBoundingClientRect();
    const frame = map.getContainer().getBoundingClientRect();
    if (!rect.height || !frame.height) return;
    // A popup taller than the map cannot be brought inside, and shoving it
    // about would only swap which end is cut off. It scrolls inside itself
    // instead - see the size block above.
    const pad = 8;
    if (rect.height > frame.height - 2 * pad) return;
    let dy = 0;
    if (rect.top < frame.top + pad) dy = rect.top - (frame.top + pad);
    else if (rect.bottom > frame.bottom - pad) dy = rect.bottom - (frame.bottom - pad);
    if (!dy) return;
    const centre = map.project(map.getCenter()).add(L.point(0, dy));
    map.setView(map.unproject(centre), map.getZoom(), { animate: false });
  }

  /* The way to take the picture away, on the picture (mapimage.js says why
   * it is here and not in the row of controls under the map). */
  addSaveControl(L, instance, { label: "Save image of the map", run: () => saveImage() });
  return instance;
}

/* EVERY POPUP ON THIS MAP IS PLACED BY US AND NOT BY LEAFLET.
 *
 * `autoPan` is Leaflet's own "keep the popup inside the map", and it is off
 * here for two measured reasons rather than a preference:
 *
 *   IT ANIMATES, ALWAYS. `panBy(offset, {animate: false})` still animates
 *   when the offset fits inside the map - the guard reads
 *   `options.animate !== true` rather than `=== false` - and it runs TWICE
 *   for every popup, once as Leaflet adds it at the default size and once
 *   for the size this file sets afterwards. On a clustered map that movement
 *   makes markercluster re-cluster, which moves the marker, which re-opens
 *   the popup, which pans again: a pin's close button never stood still long
 *   enough to be clicked, and the click timed out after thirty seconds.
 *
 *   AND IT RUNS BEFORE WE CAN CORRECT IT. `popupopen` is fired after Leaflet
 *   has already laid out and panned for a popup of the wrong size, so any
 *   measurement in the handler is taken against a map that is mid-animation.
 *   Every attempt to correct it afterwards landed the popup somewhere else
 *   again - 3 px out, then 27 the other way, depending on which frame the
 *   correction happened to fall in.
 *
 * With it off nothing moves on its own, `keepInside` measures a map that is
 * standing still, and one `setView` puts the popup where it belongs. */
const POPUP_PLACING = { autoPan: false };

/* ── The pin ─────────────────────────────────────────────────────────── */
/*
 * A PIN, AND ITS TIP IS THE COORDINATE. `iconAnchor: [16, 44]` is the bottom
 * centre of the 32x44 box, so Leaflet puts the point of the pin on the
 * archive's own coordinate; a circle centred there would be wrong by its own
 * radius, with the thing it marks hidden underneath it.
 *
 * A divIcon rather than an image, so the two states are two CSS classes
 * (map.css) and the colours come from the page's own tokens rather than from
 * a PNG somebody would have to redraw. 44 px tall is also a 44 px target,
 * which is what this view's primary object should be (WCAG 2.5.8 asks 24).
 *
 * IT LIVES IN static/js/mappin.js NOW, because the small map under a Diagrams
 * tab draws the same pin: two copies of a shape is two shapes the day one of
 * them is changed.
 */
/* WHICH PIN IS THE THING I SEARCHED FOR. Level 0 is what the term resolved
 * to - the entity itself, or every member of the bucket - and it is the one
 * question a reader asks of a map of forty pins. Colour is not the only
 * carrier: the word is in the title, the popup and the list row too. */
function isSearched(place) {
  return Number(place.level) === 0;
}

/* A link to another view, carrying the project and the language. The graph's
 * detail panel builds the same two links the same way (graph.js:
 * contextHref). There is no entity-data view to link to, so both ends of
 * a pair lead to the views that hold the answer. */
function contextHref(path, q) {
  const url = new URL(path, window.location.origin);
  url.searchParams.set("q", q);
  if (state.project) url.searchParams.set("project", state.project);
  if (state.language) url.searchParams.set("language", state.language);
  return url.pathname + url.search;
}

/* This view's own name for the shared cloner (static/js/icons.js), which
 * takes the glyph off the top bar so a button in a popup and the tab it
 * opens cannot show two different pictures of one view. `__newtab` is the
 * one glyph that belongs to no view. */
function iconFor(view) {
  return view === "__newtab" ? newTabIcon() : viewIcon(view);
}

/* Icon, then the new-tab arrow, then the name.
 *
 * The arrow is a promise about what pressing it does, and it is kept: these
 * open in a new tab. A reader following a connection is in the middle of
 * something on this map - a search, a set of ticked types, a place they had
 * panned to - and a link that took the page away would end that to answer a
 * question about one end of one line.
 *
 * `rel="noopener"` because target=_blank without it hands the new page a
 * handle on this one.
 */
function linkRow(entries) {
  const row = el("div", "popup-links");
  entries.forEach(([text, href, view]) => {
    const link = el("a", "button button--secondary popup-link");
    link.href = href;
    link.target = "_blank";
    link.rel = "noopener";
    const glyph = iconFor(view);
    if (glyph) link.appendChild(glyph);
    const arrow = iconFor("__newtab");
    if (arrow) link.appendChild(arrow);
    link.appendChild(el("span", "popup-link-name", text));
    // The word "new tab" is in the accessible name and not on screen: the
    // arrow says it to a reader who can see it, and a screen reader is owed
    // the same fact in words (WCAG 3.2.5 is about not surprising anybody).
    link.setAttribute("aria-label", `${text} (opens in a new tab)`);
    row.appendChild(link);
  });
  return row;
}

/* The pin's name, for the pointer. The popup's first two lines and nothing
 * else: a tooltip that repeats the whole popup is a popup nobody opens, and
 * one that says only "Apple Inc." leaves two pins of Apple Inc. looking
 * identical. */
function tooltipForPlace(place) {
  const box = el("div", "map-tip-body");
  box.appendChild(el("span", "map-tip-name", place.entity));
  const facts = [place.entity_type, place.address].filter(Boolean).join(" - ");
  if (facts) box.appendChild(el("span", "map-tip-where", facts));
  return box;
}

function popupForPlace(place) {
  const box = document.createElement("div");
  box.appendChild(el("span", "popup-title", place.entity));
  if (place.address) box.appendChild(el("span", "popup-address", place.address));
  const facts = [place.entity_type, place.type, `level ${place.level}`,
    isSearched(place) ? "searched" : ""].filter(Boolean);
  box.appendChild(el("div", "popup-type", facts.join(" - ")));
  // A POPUP THAT ENDS IN A FULL STOP IS A DEAD END. It links where the
  // answer actually is - this entity's own charts, and this entity in the
  // middle of its own graph.
  box.appendChild(linkRow([
    [place.entity, contextHref("/diagrams/entity", place.entity), "diagrams"],
    [place.entity, contextHref("/graph", place.entity), "graph"],
  ]));
  return box;
}

/* One type's share of the pair, as a whole percent. Shown as "count
 * (percent%)" in both the map's connection popup and the graph's panel; the
 * arithmetic is on data the answer already carries, and without it "3"
 * beside "Supplier" is a number with nothing to compare it to. */
function share(count, total) {
  if (!total) return "";
  return `${Math.round((count / total) * 100)}%`;
}

/* WHICH END PLAYS THE ROLE, AND WHICH ONE IT PLAYS IT FOR.
 *
 * The archive stores a role at each end: (Foxconn, Apple, "Supplier",
 * "Customer") is Foxconn supplying Apple. `_type_entries` (routers/api_map.py)
 * hands each kind over with `entity` set to the end that PLAYS it, so one
 * pair can carry "Foxconn: Supplier" and "Apple: Customer" at once - two
 * readings of one fact, pointing opposite ways.
 *
 * `from` and `to` are not that. They are the pair's two ids IN ID ORDER
 * (_pair_key sorts them so one fact written twice is drawn once), so a
 * heading built from them points whichever way two opaque strings happen to
 * sort. The direction has to be read off the KIND, and this is where.
 */
function endsOf(line, kind) {
  const fromIsSource = kind && kind.entity === line.from.id;
  return fromIsSource
    ? { source: line.from.name, target: line.to.name }
    : { source: line.to.name, target: line.from.name };
}

/* THE TWO ENDS, ONE ABOVE THE OTHER, POINTING THE WAY THE CONNECTION RUNS.
 *
 * Stacked rather than side by side: two names on one line compete for the
 * width of a popup, a long one pushes the other out of sight, and neither
 * can be read. Each name has the whole width and up to three lines of it
 * (map.css clamps them), and the arrow between them points down, from the
 * end that plays the part to the end it plays it for.
 *
 * The direction is the BIGGEST kind's, because that is the connection the
 * pair mostly is. Where a pair carries both readings the list below says
 * so, kind by kind, in words.
 */
function endBlock(role, name) {
  const box = el("div", "popup-end");
  box.appendChild(el("span", "popup-end-role", role));
  box.appendChild(el("span", "popup-end-name", name));
  return box;
}

/* One kind of connection as the sentence it is: who, what part, and for
 * whom - one line each, so a long name never pushes the rest out of view. */
function relSentence(line, kind) {
  const ends = endsOf(line, kind);
  const said = el("p", "popup-rel-said");
  said.appendChild(el("span", "popup-rel-end", ends.source));
  // "an Investor", not "a Investor". The role names are the archive's own
  // words, so the article has to be chosen from the word rather than baked
  // into the sentence.
  const article = /^[aeiou]/i.test(kind.name || "") ? "an" : "a";
  said.appendChild(el("span", "popup-rel-verb", `is ${article} ${kind.name} of`));
  said.appendChild(el("span", "popup-rel-end", ends.target));
  return said;
}

function popupForLine(line) {
  const box = document.createElement("div");
  // Sorted by share, biggest first - which is by count, since every kind of
  // one pair is a share of the same total. The first is what the pair mostly
  // is, so it is also what the heading points along.
  const all = (line.types || []).slice().sort((a, b) => (b.count || 0) - (a.count || 0));
  const total = all.reduce((sum, t) => sum + (t.count || 0), 0);
  const ends = endsOf(line, all[0]);

  const pair = el("div", "popup-pair");
  pair.appendChild(endBlock("Source", ends.source));
  const arrow = el("span", "popup-arrow", "↓");
  arrow.setAttribute("aria-hidden", "true");
  pair.appendChild(arrow);
  pair.appendChild(endBlock("Target", ends.target));
  box.appendChild(pair);

  /* THREE PARTS, AND THE READER CAN SEE WHERE ONE ENDS. The pair above, what
   * the archive holds about it here, and the ways out at the bottom. They
   * ran together as one column of fragments; each of the last two now opens
   * with a rule (map.css: .popup-section, .popup-links). */
  const holds = el("div", "popup-section");
  holds.appendChild(el("span", "popup-address",
    `${line.count} ${line.count === 1 ? "connection" : "connections"} in the archive`));

  /* A LIST OF SENTENCES, NOT A GRID OF FRAGMENTS.
   *
   * Not a four-column table - end, kind, count, share - where a reader has
   * to work out for themselves which of the two names the kind belongs to.
   * "Foxconn / is a Supplier of / Apple Inc." says it, and there is nothing
   * left to work out.
   *
   * The numbers sit to the right of the sentence and take as little width as
   * they can: the share is the comparable figure, so it is the big one, and
   * the count under it in brackets is what the share is a share OF. Both
   * centred, so the column reads down.
   *
   * The colour is the line's own group (legend.js), and it is dropped
   * entirely where nothing is grouped - one brown square repeated down a
   * column says only that nobody has assigned these types yet, and the
   * legend beside the map already carries that sentence.
   */
  const ungrouped = allUngrouped(all);
  const list = el("ul", "popup-rels");

  /* THE BUSIEST FEW, NOT ALL OF THEM. A pair like Israel and the USA carries
   * 259 kinds in a real archive, and 259 of anything in a popup is the
   * confusion this was meant to end. They are sorted, so the ones cut are
   * the rarest, and the line below says how many were cut and what they add
   * up to. The whole list is in the row beside the map, which is not in a
   * 300 px box. */
  const shown = all.slice(0, POPUP_TYPE_ROWS);
  shown.forEach((t) => {
    const item = el("li", "popup-rel");
    if (!ungrouped) {
      const swatch = el("span", "swatch");
      swatch.style.background = t.colour;
      swatch.setAttribute("aria-hidden", "true");
      item.appendChild(swatch);
    }
    item.appendChild(relSentence(line, t));
    const num = el("p", "popup-rel-num");
    num.appendChild(el("span", "popup-rel-share", share(t.count, total) || "-"));
    // In brackets, and under the share: it is the number the share was
    // worked out from, not a second measurement.
    num.appendChild(el("span", "popup-rel-count", `[${fmtInt(t.count)}]`));
    item.appendChild(num);
    list.appendChild(item);
  });
  holds.appendChild(list);

  const hidden = all.length - shown.length;
  if (hidden > 0) {
    const rest = all.slice(POPUP_TYPE_ROWS).reduce((sum, t) => sum + (t.count || 0), 0);
    holds.appendChild(el("p", "popup-more",
      `and ${fmtInt(hidden)} more ${hidden === 1 ? "kind" : "kinds"} of connection, `
      + `${fmtInt(rest)} between them. The row beside the map lists every one.`));
  }
  box.appendChild(holds);

  box.appendChild(linkRow([
    [line.from.name, contextHref("/diagrams/entity", line.from.name), "diagrams"],
    [line.to.name, contextHref("/diagrams/entity", line.to.name), "diagrams"],
    [line.from.name, contextHref("/graph", line.from.name), "graph"],
    [line.to.name, contextHref("/graph", line.to.name), "graph"],
  ]));
  return box;
}

/* ── Two pins at one address ─────────────────────────────────────────── */
/*
 * THE ARCHIVE KNOWS ONE POINT PER ADDRESS, AND A POINT CAN HOLD TWO
 * COMPANIES. Two entities registered at the same building are two rows with
 * the same pair of numbers, and Leaflet draws the second pin exactly on top
 * of the first: one of them is invisible, unclickable, and nothing on the
 * page says it is there. So the duplicates are nudged around a circle in
 * 45 degree steps, with two rules.
 *
 * ONE PIN STAYS PUT. Index 0 keeps the archive's own coordinate, so the
 * true spot is still occupied and a place with nothing on top of it never
 * moves at all; only the pins that would be hidden are nudged.
 *
 * THE STEP IS 0.005 DEGREES, NOT 0.01. The nudge is a lie about where a
 * building is, so it wants to be the smallest lie that works - and what it
 * has to beat is the CLUSTER RADIUS: markercluster keeps two pins in one
 * blob while they are within 45 px of each other (createMap), so a nudge
 * that is smaller than that at every zoom is never seen at all - the pins
 * stay in the cluster and hide inside it instead.
 *
 * 0.005 degrees is 555 m of latitude. In the projection Leaflet draws in
 * that is 58 px at zoom 14 on the equator and more towards the poles, so
 * the two pins come apart at the zoom where somebody is looking at one
 * town; further out they are honestly drawn as one cluster of two, which is
 * what they are at that scale. 0.01 degrees (1.1 km) buys nothing above
 * that and tells twice the lie.
 *
 * The longitude step is divided by cos(latitude), because a degree of
 * longitude is that much shorter than a degree of latitude there: without
 * it the ring is an ellipse that flattens towards the poles.
 *
 * WHAT IS NEVER MOVED: the popup, the list row and the caption. They name
 * the address the archive holds, and they read it from the answer.
 */
const SPREAD_DEG = 0.005;
const SPREAD_STEPS = 8;          // 45 degrees apart

function placeKey(entityId, address) {
  return `${entityId}|${address}`;
}

function coordKey(lat, lng) {
  return `${Number(lat).toFixed(6)}|${Number(lng).toFixed(6)}`;
}

/* Fills `drawnPlaces` and `drawnEntities` from the answer. Called once per
 * answer, not per draw: a pin must not walk around when a checkbox is
 * ticked. */
function spread() {
  drawnPlaces.clear();
  drawnEntities.clear();
  const all = (answer && answer.markers) || [];
  const groups = new Map();
  all.forEach((place) => {
    const key = coordKey(place.lat, place.lng);
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(place);
  });
  groups.forEach((group) => {
    group.forEach((place, i) => {
      let lat = place.lat;
      let lng = place.lng;
      if (i > 0) {
        // Ring 1 holds eight, ring 2 the next eight half a step round, and
        // so on - a ninth company at one address is rare enough that a
        // wider ring is a better answer than a pin nobody can hit.
        const ring = 1 + Math.floor((i - 1) / SPREAD_STEPS);
        const step = (i - 1) % SPREAD_STEPS;
        const angle = (step * (2 * Math.PI / SPREAD_STEPS)) + (ring - 1) * (Math.PI / SPREAD_STEPS);
        const radius = SPREAD_DEG * ring;
        // cos(lat) turns metres of longitude into degrees at this latitude;
        // 0.2 is the floor, so a pin at the pole is nudged, not flung.
        const shrink = Math.max(0.2, Math.cos((lat * Math.PI) / 180));
        lat += radius * Math.sin(angle);
        lng += (radius * Math.cos(angle)) / shrink;
      }
      drawnPlaces.set(placeKey(place.entity_id, place.address), { lat, lng });
    });
  });
  // The line ends. An entity's line is drawn from its FIRST address - the
  // one `api_map.py` puts into `entities[].lat/lng` - so the end of a line
  // has to land on that pin and not on the archive's point under it. The
  // point it came FROM is kept as well, so `endAt` can tell whether the
  // line it is asked about is really that address's line.
  all.forEach((place) => {
    if (drawnEntities.has(place.entity_id)) return;
    const at = drawnPlaces.get(placeKey(place.entity_id, place.address));
    if (at) drawnEntities.set(place.entity_id, { ...at, from: { lat: place.lat, lng: place.lng } });
  });
}

/* Where a pin is drawn, and where a line ends. Both fall back to what the
 * answer says, so nothing here can lose a place. */
function placeAt(place) {
  return drawnPlaces.get(placeKey(place.entity_id, place.address))
    || { lat: place.lat, lng: place.lng };
}

function endAt(end) {
  const spot = drawnEntities.get(end.id);
  // An entity whose first address is not the one this end names (it has
  // several, or it is not on the map at all) keeps the answer's own point:
  // better a line that is a little off than one that ends in another town.
  if (spot && Math.abs(spot.from.lat - end.lat) < 1e-9
      && Math.abs(spot.from.lng - end.lng) < 1e-9) {
    return { lat: spot.lat, lng: spot.lng };
  }
  return { lat: end.lat, lng: end.lng };
}

/* ── An arc, not a chord ─────────────────────────────────────────────── */
/*
 * A STRAIGHT LINE COVERS THE PLACES ON IT. Leaflet draws the shortest line
 * on the SCREEN, so a pin that happens to lie between two connected places
 * would disappear under the stroke: three places on one route, and the
 * middle one could not be seen or clicked. That is what the arc is for.
 *
 * The shape is a quadratic Bezier whose control point sits perpendicular to
 * the middle of the chord. A quadratic passes through HALF the control
 * point's offset at t=0.5, so the control point is moved 2 x the bow the
 * curve should have in the middle.
 *
 * THE BOW IS MEASURED IN PIXELS, WHICH IS WHY IT IS RE-DERIVED ON ZOOM.
 * A bow that is a fixed fraction of the chord makes a short hop as round as
 * a long one; a bow of fixed degrees is invisible at zoom 3 and a circle at
 * zoom 15. So it is a fraction of the chord ON SCREEN, floored so it always
 * clears a pin and capped so a flight across the world does not become a
 * loop - and, because that depends on the zoom, `reflowArcs` computes it
 * again every time the zoom changes.
 *
 * THE BOW GOES DOWN THE SCREEN. A pin stands UP from its coordinate
 * (32x44 px, anchored at the tip - see `pinIcon`), so the room below the
 * point is empty and the room above it is the pin. Bowing south therefore
 * clears a place on the line by the whole bow; bowing north would have to
 * clear the height of the icon before it cleared anything at all.
 *
 * SEVERAL CONNECTIONS BETWEEN THE SAME TWO PLACES ARE FANNED. Two companies
 * at one address, each connected to the same third place, drew two lines on
 * exactly the same pixels: one popup was unreachable. Each line in such a
 * group takes the next step of the fan, alternating sides - 1, -1, 2, -2 -
 * so every one of them can be hit on its own.
 */
/* The arc, the fan and their numbers are in static/js/mapline.js: the small
 * map under a Diagrams tab draws the same curve, and one drawing needs one
 * definition. `arcPath` there takes the map as an argument; here it is
 * always this view's own. */
function arcPath(from, to, factor) {
  return arcOf(map, from, to, factor);
}

/* The zoom changed, so every bow is the wrong size. The polylines are the
 * SAME objects - `setLatLngs`, not a new layer - so an open popup stays
 * open, the lists keep working and the pins are not re-clustered. */
function reflowArcs() {
  arcs.forEach((arc) => {
    const path = arcPath(arc.from, arc.to, arc.factor);
    arc.parts.forEach((part) => part.setLatLngs(path));
  });
}

function draw() {
  if (!map || !answer) return;
  const L = window.L;
  markerLayer.clearLayers();
  lineLayer.clearLayers();
  markers.clear();
  lines.clear();
  // The curves are about to be built again, so the list `reflowArcs` bends
  // must not still hold the layers that have just been thrown away.
  arcs = [];

  visiblePlaces().forEach((place) => {
    // `placeAt`, not the answer: two pins on one address were one pin.
    const at = placeAt(place);
    const searched = isSearched(place);
    const marker = L.marker([at.lat, at.lng], {
      icon: pinIcon(searched),
      title: `${place.entity} - ${place.address}${searched ? " - searched" : ""}`,
      alt: `${place.entity}, ${place.address}`,
      keyboard: true,
      // The searched entity's pin is drawn over the others where they
      // overlap: it is the one the reader came for.
      zIndexOffset: searched ? 1000 : 0,
    });
    // A divIcon is a <div>, and Leaflet only writes `alt` onto an <img>. The
    // name has to reach a screen reader all the same, and it is what the
    // title says.
    marker.on("add", () => {
      const node = marker.getElement();
      if (node) node.setAttribute("aria-label", marker.options.title);
    });
    /* WHOSE PIN THIS IS, ON HOVER, WITHOUT OPENING ANYTHING.
     *
     * A map of forty pins is forty questions of the form "which one is
     * that?", and opening a popup to answer one of them closes the last and
     * moves the map. A tooltip answers it for the price of a pointer: the
     * entity, and under it the address that separates two pins of the same
     * company.
     *
     * `sticky` so it follows the pointer over the whole pin rather than
     * clinging to the anchor point, and `direction: "top"` so it sits above
     * the pin and never covers the one below it. It is NOT the accessible
     * name - `title`/`aria-label` on the marker already carry that, and a
     * tooltip needs a mouse - so nothing here is only in the hover. */
    marker.bindTooltip(tooltipForPlace(place), {
      direction: "top", offset: [0, -38], sticky: true, opacity: 1,
      className: "map-tip",
    });
    // The popup names the ADDRESS THE ARCHIVE HOLDS. `placeAt` moved the
    // pin, not the place.
    marker.bindPopup(popupForPlace(place), POPUP_PLACING);
    markerLayer.addLayer(marker);
    markers.set(placeKey(place.entity_id, place.address), marker);
  });

  // How many lines already run between the same two points, so each of them
  // takes its own step of the fan and none of them is buried.
  const fan = new Map();
  visibleLines().forEach((line) => {
    if (!line.drawable) return;
    const from = endAt(line.from);
    const to = endAt(line.to);
    const pair = [coordKey(from.lat, from.lng), coordKey(to.lat, to.lng)].sort().join("~");
    const factor = fanFactor(fan.get(pair) || 0);
    fan.set(pair, (fan.get(pair) || 0) + 1);
    const path = arcPath(from, to, factor);
    // ONE VISIBLE STROKE PER CONNECTION: the colour, and nothing around it.
    //
    // Not three strokes - 1 px of near-black outside 1.5 px of white outside
    // the colour - although there is a case for them: the group colours are
    // checked against white (3:1, WCAG 1.4.11), but a line here is not on
    // white, it is on map tiles, and measured against ocean #aad3df or land
    // #f2efe9 ten of the eleven groups fall under 3:1. A casing would be what
    // the colour is seen against and a keyline what makes the line
    // perceivable at all.
    //
    // It is one stroke because what a reader wants to see on this map is the
    // colour coding, and a 6.5 px band around a 1.5 px colour is mostly band.
    // What that costs is written down rather than hidden - over a pale tile a
    // pale group is a low-contrast line, and the answer to "which group is
    // this" is the legend, the checklist and the popup, none of which depend
    // on telling two colours apart on tiles.
    //
    // WHAT DID NOT GO IS THE CLICK TARGET. An SVG stroke is hit only where it
    // is painted, so a line whose clickable part is a 1.5 px colour is not a
    // target, it is a dare (WCAG 2.5.8 asks 24). The widest stroke is still
    // there and still the one that carries the popup - it is simply painted
    // with `transparent`, which is a paint and therefore hit-testable, unlike
    // an opacity of zero, which is not painted and would not be hit.
    //
    // Leaflet draws in insertion order, so the invisible one goes first and the
    // colour sits on top of it; `interactive: false` on the colour keeps every
    // click on the one below, which is where the popup is.
    const hit = L.polyline(path, {
      color: "transparent", weight: line.weight + HIT_MARGIN, opacity: 1,
      className: "map-line-hit",
    });
    hit.bindPopup(popupForLine(line), POPUP_PLACING);
    lineLayer.addLayer(hit);
    const shape = L.polyline(path, {
      color: line.colour, weight: line.weight,
      opacity: line.level > 1 ? 0.8 : 1, interactive: false,
      // Named, like the invisible one above it. Nothing styles it - the
      // colour and the width come from the answer - but a line that can be
      // asked for by name is one a test can read the stroke off, which is
      // how "the connections are really in the saved picture" is checked
      // (tests/ui/test_map_view.py).
      className: "map-line",
    });
    lineLayer.addLayer(shape);
    // The list row opens the popup, so it wants the object the popup is on.
    lines.set(`${line.from.id}|${line.to.id}`, hit);
    arcs.push({ from, to, factor, parts: [hit, shape] });
  });
}

/* What the map draws right now, after the two Draw switches, the legend's
 * own switches and the type checklist. The LISTS use the same two functions,
 * so a row can never describe a line that is not there. */
function visiblePlaces() {
  // Both modes draw pins; the server decides WHICH - every address of the
  // search on Locations, one address per entity on Connections.
  if (!answer) return [];
  // The location checklist filters the pins the way the connection checklist
  // filters the lines. A pin the archive gives no type is under the name the
  // checklist lists it by, so unticking that row hides it too.
  return (answer.markers || []).filter((m) => !offPlaceTypes.has(m.type || ""));
}

function visibleLines() {
  if (!answer || !drawingConnections()) return [];
  return (answer.connections || []).filter(
    (line) => (line.types || []).some((t) => !offTypes.has(t.name)));
}

/* The type names of one colour group, as this map uses them. The checklist
 * (`answer.types`) carries each type's group, so the legend needs no second
 * list - and a group with no type on this map cannot be switched at all. */
function typesOfGroup(key) {
  return (answer && answer.types ? answer.types : [])
    .filter((t) => t.group === key).map((t) => t.name);
}

function groupIsOff(key) {
  const names = typesOfGroup(key);
  return names.length > 0 && names.every((name) => offTypes.has(name));
}

function toggleGroup(key, name) {
  const names = typesOfGroup(key);
  if (!names.length) return;
  const wasOff = groupIsOff(key);
  names.forEach((n) => (wasOff ? offTypes.delete(n) : offTypes.add(n)));
  writeHide();
  applyFilters();
  announce(`${name} ${wasOff ? "shown" : "hidden"}`);
}

function writeHide() {
  setExtra("hide", [...offTypes].join(","));
}

function writeHidePins() {
  setExtra("hidepins", [...offPlaceTypes].join(","));
}

/* The location types on this map, with how many pins each has. Built from
 * the markers themselves rather than from a list the server sends, so a type
 * can never be offered that has no pin and no pin can exist without a row
 * that switches it off. */
function placeTypes() {
  const counts = new Map();
  ((answer && answer.markers) || []).forEach((m) => {
    const name = m.type || "";
    counts.set(name, (counts.get(name) || 0) + 1);
  });
  return [...counts.entries()]
    .map(([name, count]) => ({ name, count }))
    .sort((a, b) => b.count - a.count || a.name.localeCompare(b.name));
}

/* Everything the filter touches, redrawn from the one set: the lines on the
 * map, the list beside it, the sentence above it, the ticks in the checklist
 * and the pressed state of the legend. One function, so the five cannot
 * disagree - which is exactly how the legend and the checklist came to tell
 * a customer two different things about the same line. */
function applyFilters() {
  draw();
  renderPlaces();
  renderLines();
  updateCaption();
  syncTypeChecks();
  updateLegend();
}

function fit() {
  if (!map) return;
  const points = [];
  visiblePlaces().forEach((m) => { const at = placeAt(m); points.push([at.lat, at.lng]); });
  visibleLines().forEach((line) => {
    if (!line.drawable) return;
    const from = endAt(line.from);
    const to = endAt(line.to);
    points.push([from.lat, from.lng], [to.lat, to.lng]);
  });
  if (!points.length) return;
  if (points.length === 1) map.setView(points[0], 9);
  else map.fitBounds(window.L.latLngBounds(points), { padding: [30, 30] });
}

/* ── The lists beside the map ────────────────────────────────────────── */

/* A row was activated: bring the map into view, mark the row, and SAY what
 * happened.
 *
 * The lists are much taller than the map, so a reader working through the
 * Connections card is far down the page. Before this, clicking a row there
 * opened a popup 578 px above the top of the screen - the click produced no
 * perceivable result at all. The map is sticky now (map.css), which keeps it
 * on screen in the two-column layout; `scrollIntoView` covers the narrow
 * one-column layout, where the map is above the lists and nothing sticks.
 * The status line repeats the popup in words, so the answer is also
 * announced (aria-live) instead of only drawn. */
function reveal(button, text) {
  if (map) map.getContainer().scrollIntoView({ block: "nearest" });
  if (button) {
    button.closest("ul").querySelectorAll(".map-item.is-active")
      .forEach((other) => other.classList.remove("is-active"));
    button.classList.add("is-active");
  }
  setStatus(text);
  announce(text);
}

function renderPlaces() {
  placesList.textContent = "";
  const places = visiblePlaces();
  placesCount.textContent = places.length ? `${fmtInt(places.length)} drawn` : "";
  if (!places.length) {
    placesList.appendChild(el("li", "empty", emptyPlacesText()));
    return;
  }
  places.forEach((place) => {
    const item = el("li");
    const button = el("button", "map-item");
    button.type = "button";
    const facts = [place.address, place.type, `level ${place.level}`,
      isSearched(place) ? "searched" : ""].filter(Boolean).join(" - ");
    const title = el("span", "item-title", place.entity);
    if (isSearched(place)) title.classList.add("is-searched");
    button.appendChild(title);
    button.appendChild(el("span", "item-sub", facts));
    button.addEventListener("click", () => {
      const marker = markers.get(placeKey(place.entity_id, place.address));
      if (!marker || !map) return;
      reveal(button, `${place.entity} - ${facts}`);
      const at = placeAt(place);
      map.setView([at.lat, at.lng], Math.max(map.getZoom(), 8));
      if (markerLayer.zoomToShowLayer) markerLayer.zoomToShowLayer(marker, () => marker.openPopup());
      else marker.openPopup();
    });
    item.appendChild(button);
    placesList.appendChild(item);
  });
}

function renderLines() {
  linesList.textContent = "";
  const drawn = visibleLines();
  linesCount.textContent = drawn.length ? `${fmtInt(drawn.length)} shown` : "";
  if (!drawn.length) {
    linesList.appendChild(el("li", "empty", emptyLinesText()));
    return;
  }
  const undrawable = drawn.filter((line) => !line.drawable).length;
  drawn.forEach((line) => {
    const item = el("li");
    const button = el("button", "map-item");
    button.type = "button";
    const title = el("span", "item-title");
    const swatch = el("span", "swatch");
    swatch.style.background = line.colour;
    swatch.setAttribute("aria-hidden", "true");
    title.appendChild(swatch);
    title.appendChild(document.createTextNode(`${line.from.name} ⇄ ${line.to.name}`));
    button.appendChild(title);
    const facts = [`${line.count} in the archive`, (line.types || []).map((t) => t.name).join(", "),
      line.drawable ? "" : "no coordinates - not drawn"].filter(Boolean).join(" - ");
    button.appendChild(el("span", "item-sub", facts));
    button.addEventListener("click", () => {
      const pair = `${line.from.name} ⇄ ${line.to.name}`;
      const shape = lines.get(`${line.from.id}|${line.to.id}`);
      if (!shape || !map) {
        // A pair without coordinates has no line to open. Saying so is the
        // answer; a button that does nothing is not.
        reveal(button, `${pair} - ${facts}`);
        return;
      }
      reveal(button, `${pair} - ${facts}`);
      /* THE POPUP OPENS WHEN THE MAP HAS STOPPED MOVING, and the order is the
       * whole of it. `fitBounds` ANIMATES: opening the popup on the next line
       * lets Leaflet place it against where the map was, and the pan then
       * carries it out of the container - measured at 1024x768, the popup
       * ends 36 px above the top edge of a map it is supposed to live
       * inside, which is a click that appears to do nothing at all. At 1440
       * the same fault lands inside the container by luck of the extra
       * height, which is why it would read as a small-screen problem.
       *
       * `once("moveend")` and not a timeout: Leaflet fires it at the end of
       * the animation, and also immediately when the bounds are already
       * fitted and nothing moves (`panBy` of nothing fires it too), so the
       * popup opens either way. */
      map.fitBounds(shape.getBounds(), { padding: [40, 40], animate: false });
      shape.openPopup();
    });
    item.appendChild(button);
    linesList.appendChild(item);
  });
  if (undrawable) {
    linesList.appendChild(el("li", "empty",
      `${undrawable} of them cannot be drawn: one of the two entities has no coordinates.`));
  }
}

/* Why a list is empty is three different facts, and a person acts on each of
 * them differently: nothing searched, nothing found, or switched off by hand. */
function emptyPlacesText() {
  if (failed) return "Nothing could be loaded. Use \u201cTry again\u201d beside the map.";
  if (!answer || !answer.entities.length) return "Nothing on the map yet.";
  if (answer.markers.length && offPlaceTypes.size) {
    return "Every location type is switched off in the checklist.";
  }
  return "None of these entities has coordinates in the archive.";
}

function emptyLinesText() {
  if (!drawingConnections()) {
    return "Show is set to Locations, which draws every address of the search and no lines.";
  }
  if (failed) return "Nothing could be loaded. Use “Try again” beside the map.";
  if (!answer || !answer.connections.length) return "Nothing on the map yet.";
  return "Every connection is switched off in the legend or the checklist.";
}

/* A CHECKLIST, AND IT IS THE SAME OBJECT TWICE.
 *
 * The connection types filter the lines and the location types filter the
 * pins; everything else about the two is identical, so they are one function
 * with a small description of which is which. Both grow with a real archive,
 * which is why both have the box that narrows the list and the two buttons -
 * ticking sixty rows one at a time is not a filter, it is a chore.
 *
 * The box narrows the LIST and never the map: a filter whose whole extent
 * cannot be seen is a filter nobody can undo. The count in the header and
 * the note under the list say so, and say how many rows are hidden and how
 * many types are switched off - state that is not on the screen is state
 * that cannot be got back out of.
 */
function renderChecklist(spec) {
  const { list, card, count, note, checks, off, filter, colours, onToggle } = spec;
  list.textContent = "";
  const said = card.querySelector(".legend-note");
  if (said) said.remove();
  checks.clear();
  const types = spec.types();
  card.hidden = !types.length;
  // NOTHING GROUPED, SO NO COLUMN OF ONE COLOUR. 113 connection types beside
  // 113 identical brown squares is a claim that they are alike; the sentence
  // under the list says why there is no colour instead (legend.js).
  const ungrouped = Boolean(colours) && allUngrouped(types);
  const needle = filter.trim().toLowerCase();
  const shown = types.filter(
    (type) => !needle || (type.name || "").toLowerCase().includes(needle));
  shown.forEach((type) => {
    const item = el("li");
    const label = el("label", "type-check");
    const box = document.createElement("input");
    box.type = "checkbox";
    box.checked = !off.has(type.name);
    box.addEventListener("change", () => {
      if (box.checked) off.delete(type.name);
      else off.add(type.name);
      onToggle(type, box.checked);
    });
    label.appendChild(box);
    if (colours && !ungrouped) {
      const swatch = el("span", "swatch");
      swatch.style.background = type.colour;
      swatch.setAttribute("aria-hidden", "true");
      label.appendChild(swatch);
    }
    label.appendChild(el("span", "type-name", type.name || spec.unnamed));
    label.appendChild(el("span", "type-count", fmtInt(type.count)));
    item.appendChild(label);
    list.appendChild(item);
    checks.set(type.name, box);
  });
  if (types.length && !shown.length) {
    list.appendChild(el("li", "empty", `No ${spec.word} contains “${filter.trim()}”.`));
  }
  const hidden = types.filter((type) => off.has(type.name)).length;
  count.textContent = `${fmtInt(types.length)} ${types.length === 1 ? spec.word : `${spec.word}s`}`;
  const states = [];
  if (needle) states.push(`${fmtInt(shown.length)} of them listed`);
  if (hidden) states.push(`${fmtInt(hidden)} switched off`);
  note.textContent = states.length
    ? `${states.join(", ")}. Typing in the box hides rows; only a tick changes the map.`
    : "";
  if (ungrouped) card.appendChild(ungroupedNote());
}

function renderTypes() {
  renderChecklist({
    types: () => (answer ? answer.types || [] : []),
    list: typesList, card: typesCard, count: typesCount, note: typesNote,
    checks: typeChecks, off: offTypes, filter: typeFilter,
    colours: true, word: "connection type", unnamed: "Without a type",
    onToggle: (type, on) => {
      writeHide();
      applyFilters();
      announce(`${type.name} ${on ? "shown" : "hidden"}`);
    },
  });
}

function renderPlaceTypes() {
  renderChecklist({
    types: placeTypes,
    list: placeTypesList, card: placeTypesCard, count: placeTypesCount,
    note: placeTypesNote, checks: placeTypeChecks, off: offPlaceTypes,
    filter: placeTypeFilter, colours: false,
    word: "location type", unnamed: "Without a type",
    onToggle: (type, on) => {
      writeHidePins();
      applyFilters();
      announce(`${type.name || "Without a type"} ${on ? "shown" : "hidden"}`);
    },
  });
}

/* The ticks, after something else changed them. Setting `checked` rather
 * than rebuilding the list keeps the focus where the reader put it. */
function syncTypeChecks() {
  typeChecks.forEach((box, name) => { box.checked = !offTypes.has(name); });
  placeTypeChecks.forEach((box, name) => { box.checked = !offPlaceTypes.has(name); });
}

/* The legend, with the counts the map can actually show and the pressed
 * state read back out of `hide`.
 *
 * HOW MANY OF THEM ARE ON THE MAP. A group's count is how many connections
 * of that group this search found - and four of the six around Apple have an
 * end without coordinates, so they are in the list and not on the map. A
 * legend that counts six beside a picture with two lines in it is a legend
 * that cannot be checked, so the row says both numbers.
 *
 * The pressed state is written here rather than left to legend.js: that
 * module keeps its own set of hidden keys for pages that have no other
 * filter, and on this page the truth is `offTypes`. The click handler below
 * ignores the on/off it is handed for the same reason. */
function updateLegend() {
  if (!legend) return;
  const drawnNow = visibleLines().filter((line) => line.drawable);
  const found = answer ? (answer.connections || []) : [];
  // A KEY EXPLAINS WHAT IS ON THE MAP; WITH NOTHING ON IT, IT EXPLAINS
  // NOTHING.
  //
  // The card listed all eleven groups with a "0" beside each after a search
  // that found nothing and with both Draw switches off - eleven colours
  // presented as this map's colours, none of which was anywhere on it. Only
  // the groups this ANSWER holds a connection for are listed (not only the
  // drawn ones: a group switched off in the legend has to keep the row that
  // switches it back on), and when there is no such group the card says in
  // one line why there is nothing to explain.
  const groups = (answer && answer.legend ? answer.legend : [])
    .filter((g) => found.some((c) => c.group === g.key))
    .map((g) => {
      const total = found.filter((c) => c.group === g.key).length;
      const painted = drawnNow.filter((c) => c.group === g.key).length;
      let count = g.count;
      if (total) count = painted === total ? String(total)
        : `${total} (${painted ? `${painted} drawn` : "none drawn"})`;
      return { ...g, count };
    });
  legend.update(groups);
  if (!groups.length) sayWhyNoColours();
  applyLegendState();
}

/* The one line that replaces the list of groups. legend.js takes its empty
 * text once, when the panel is built, and this page has four different
 * reasons for an empty key - so the row it wrote is given the sentence that
 * fits, in the same place a group row would have been. */
function sayWhyNoColours() {
  const row = legendBox.querySelector(".legend-empty");
  if (!row) return;
  if (failed) {
    row.textContent = "Nothing could be loaded, so there are no colours to explain.";
    return;
  }
  if (!answer) return;                        // the panel's own "search first"
  if (!drawingConnections()) {
    row.textContent = "Show is set to Locations, so there are no lines to colour.";
  } else {
    row.textContent = "Nothing is drawn, so there are no colours to explain.";
  }
}

function applyLegendState() {
  legendBox.querySelectorAll(".legend-row").forEach((row) => {
    const off = groupIsOff(row.dataset.key);
    const button = row.querySelector(".legend-toggle");
    if (button) {
      const name = row.querySelector(".legend-name");
      button.setAttribute("aria-pressed", off ? "false" : "true");
      button.title = `${off ? "Show" : "Hide"} ${name ? name.textContent : "this group"}`;
    }
    row.classList.toggle("is-hidden", off);
  });
}

/* ── Loading ─────────────────────────────────────────────────────────── */

/* The sentence over the map, and it counts what is ON the map.
 *
 * Not "6 connections" beside a picture with two lines in it, because four
 * of the six have an end the archive holds no coordinates for, and that fact
 * is otherwise only in a list far below. A reader who counts the lines has to arrive
 * at the number in the caption, so the caption says how many of them are
 * drawn and why the rest are not. */
function connectionsPhrase() {
  const all = answer.connections || [];
  const total = all.length;
  if (!total) return "no connections";
  const noCoords = all.filter((c) => !c.drawable).length;
  const painted = visibleLines().filter((c) => c.drawable).length;
  const head = `${fmtInt(total)} ${total === 1 ? "connection" : "connections"}`;
  if (painted === total) return head;
  const why = [];
  if (noCoords) why.push(`${fmtInt(noCoords)} ${noCoords === 1 ? "has" : "have"} no coordinates`);
  const off = total - noCoords - painted;
  if (off > 0) why.push(`${fmtInt(off)} switched off`);
  // "1 connection, none of them drawn" is a plural about one thing.
  const nothing = total === 1 ? "not drawn" : "none of them drawn";
  return `${head}, ${painted ? `${fmtInt(painted)} of them drawn` : nothing}`
    + (why.length ? ` (${why.join(", ")})` : "");
}

/* THE CAPTION DESCRIBES WHAT IS DRAWN, INCLUDING NOTHING.
 *
 * With both Draw boxes unticked it must not read "2 entities around Apple -
 * the bucket and every member - 1 place - no connections - level 2." over an
 * empty world: that promises a place that is not there, and "no connections"
 * reads as a fact about the archive rather than as a switch the reader has
 * turned off. A caption is the line directly above the picture, so it says
 * what the switches did before it counts anything. */
/* What an empty page asks for, in the words of the axis it is on. */
function emptyPrompt() {
  return axis === "type"
    ? "Search for an event type to show what it happened to on the map."
    : "Search for an entity to show it on the map.";
}

/* A term that WAS understood and still draws nothing. Two different facts,
 * two different sentences, and neither of them may be the prompt that says
 * "search for something": a reader whose term is still in the field must
 * never be told their search is gone (the same rule as after a failed
 * request, and a type search is a second way to break it).
 *
 * An event type is the case that makes this necessary. An archive can hold
 * 29,000 "Sports Event" events and no row at all in event_entities - the
 * extraction only began naming the entities an event is about later - and
 * then the type resolves exactly and names nobody. That is a true and
 * useful answer; silence is not. */
function nothingText() {
  const resolved = (answer && answer.resolved) || {};
  const term = (answer && answer.q) || "";
  if (resolved.match === "none") {
    return axis === "type"
      ? `No event in this project has the type “${term}”.`
      : `Nothing in this project is called “${term}”.`;
  }
  const about = resolved.label || term;
  return axis === "type"
    ? `“${about}” is an event type in this project, but no event of that kind names an entity - there is nothing to draw.`
    : `“${about}” was found, but nothing about it can be drawn on the map.`;
}

function captionText() {
  if (!answer || !answer.q) return emptyPrompt();
  if (!answer.entities.length) return nothingText();
  const resolved = answer.resolved || {};
  const about = `${resolved.label || answer.q}`;
  const bits = [];
  // WHICH AXIS PRODUCED THIS, AND HOW BIG IT IS. A number of entities
  // "around Apple" and the same number "in Product launch events" are two
  // different pictures, and the caption is the only line that says which
  // one is on the screen (the page has no room for the notice band the
  // other views use - see templates/map.html).
  const many = `${fmtInt(answer.entities.length)} ${answer.entities.length === 1 ? "entity" : "entities"}`;
  bits.push(axis === "type"
    ? `${many} in “${about}” events (event type)`
    : `${many} around ${about}`);
  // FROM THE BUCKET'S NAME, NOT FROM WHAT WAS TYPED. "Apple Inc." is not a
  // bucket, it is a member of one, and a map that answered it with two
  // entities under the word "Apple" left a reader looking at a picture of
  // something else with nothing to say so. `resolved.member` is the member
  // that was typed (app/scope.py); when it is the bucket's own name there
  // is nothing to distinguish and the short clause is the true one.
  //
  // The three views with room for it put this in a notice with a "Show only
  // Apple Inc." button (static/js/resolved.js). This page has no room above
  // the picture (map.html), so the way to one member here is the suggestion
  // list, whose rows now carry the type: "Apple Inc. - Company".
  if (axis === "type") {
    // A type search is already a group, so there is no bucket clause to
    // add - and `values` is what the type resolved to, which is worth a
    // word when a type BUCKET merged several spellings into one.
    if (resolved.match === "bucket") {
      bits.push(`the bucket of ${fmtInt((resolved.values || []).length)} type spellings`);
    }
  } else if (resolved.match === "bucket") {
    const member = resolved.member && resolved.member.name;
    bits.push(member && member.toLowerCase() !== String(about).toLowerCase()
      ? `the bucket, which “${member}” is a member of`
      : "the bucket and every member");
  } else if (resolved.type && resolved.in_bucket) {
    bits.push(`this entity alone, not the bucket “${resolved.in_bucket.name}”`);
  }
  // THE CAPTION COUNTS WHAT IS ON THE MAP, so it counts the pins the
  // location checklist leaves, not every pin the answer holds - the same
  // rule the connection clause has always followed.
  const drawnPins = visiblePlaces().length;
  const allPins = answer.markers.length;
  const head = `${fmtInt(drawnPins)} ${drawnPins === 1 ? "place" : "places"}`;
  bits.push(drawnPins === allPins ? head
    : `${head} of ${fmtInt(allPins)} (${fmtInt(allPins - drawnPins)} switched off by location type)`);
  // WHICH QUESTION IS ON SCREEN. The two modes count different things, and
  // a reader who does not know which one they are looking at cannot read
  // the number in front of it: "3 places" is every address of one person on
  // Locations and one address each of three companies on Connections.
  if (drawingConnections()) {
    bits.push(connectionsPhrase());
    bits.push(`level ${answer.levels}`);
  } else {
    bits.push("every address of the search, without its connections");
  }
  return `${bits.join(" - ")}.`;
}

function updateCaption() {
  captionBox.textContent = captionText();
}

async function load() {
  const term = state.q;
  // A new answer is a new sentence: whatever the status line said before
  // the last picture was saved is no longer true of this one.
  statusBeforeImage = null;
  statusBeforeImageWarned = false;
  if (!term) {
    answer = null;
    failed = false;
    spread();
    showRetry(null);
    captionBox.textContent = emptyPrompt();
    renderPlaces();
    renderLines();
    renderTypes();
    renderPlaceTypes();
    updateLegend();
    setStatus("");
    return;
  }
  setStatus(`Looking up ${term}…`);
  showRetry(null);
  try {
    answer = await api("/api/map/entity", {
      channel: "map",
      params: {
        q: term,
        axis,
        levels: levelValue(),
        mode,
        group: grouping,
      },
    });
  } catch (err) {
    if (isAbort(err)) return;
    failed = true;
    const said = errorSentence(`The map of ${term} could not be loaded.`, err);
    // ONE PLACE PER FAULT. Not three times on one screenful - in the
    // caption, in the status line and, in its own words, in both lists. It
    // belongs in the status line: that is the live region, and the Try
    // again button stands next to it.
    //
    // The caption keeps its own job, which is the SUBJECT. Reverting it to
    // "Search for an entity to show it on the map." would tell a reader
    // their search is gone while it is still in the field, so it names the
    // term and the state instead - and when
    // there is a map on screen from before, it goes on describing THAT,
    // because that map is still true.
    if (answer) updateCaption();
    else captionBox.textContent = `Nothing is drawn for “${term}” yet.`;
    setStatus(said, true);
    showRetry(() => load());
    // What was already drawn STAYS drawn: a failed request is not a
    // reason to take away the map a reader was looking at. Only when there
    // is nothing to keep do the lists get their sentence rewritten.
    if (!answer) { renderPlaces(); renderLines(); updateLegend(); }
    // No announce(): #map-status is aria-live (map.html), so writing the
    // sentence there IS the announcement. Saying it through the toast region
    // as well is how a screen reader came to hear it twice.
    return;
  }
  showRetry(null);
  failed = false;

  // Once per ANSWER, not once per draw: a pin that walks about when a
  // checkbox is ticked is worse than a pin that is hidden.
  spread();
  renderTypes();
  renderPlaceTypes();
  applyFilters();
  fit();

  const resolved = answer.resolved || {};
  if (resolved.match === "none" || !answer.entities.length) {
    // WHAT HAPPENED IS IN THE CAPTION (nothingText, which names the axis:
    // "nothing is called Delivery" is the wrong answer to a search for an
    // event type). The status line carries the NEXT STEP and never the same
    // sentence again - one place per fault, or a reader gets the same words
    // twice on one screen and looks for the difference between them.
    setStatus(axis === "type"
      ? "Try another event type, or switch to Object and search for the entity itself."
      : "Check the project and language in the top bar.", true);
  } else if (answer.capped) {
    // Two different cuts wear one word on the wire. With a limit set, it is
    // that limit and a reader can raise it; with none, it is the per-hop edge
    // cap, and the way to a complete picture is a smaller search rather than a
    // bigger number - so the sentence has to say which one it met.
    setStatus(answer.max_entities
      ? `Only the first ${fmtInt(answer.max_entities)} entities are drawn. Use fewer levels for a complete picture, or raise DASHBOARD_MAP_MAX_ENTITIES.`
      : "This network is larger than one hop can return, so part of it is missing. Use fewer levels for a complete picture.", true);
  } else if (!answer.markers.length && answer.entities.length) {
    setStatus("None of these entities has coordinates in the archive; the connections are listed beside the map.", true);
  } else {
    setStatus("");
  }
  announce(captionText());
}

/* ── Controls ────────────────────────────────────────────────────────── */

function markLevels() {
  const current = levelValue();
  levelButtons.querySelectorAll("[data-levels]").forEach((button) => {
    button.setAttribute("aria-pressed", button.dataset.levels === current ? "true" : "false");
  });
}

/* Show a term the way the typeahead shows a chosen one: value in the hidden
 * input, the term as the field's placeholder, field empty. Used for the URL
 * when the page opens and for text that has just been drawn; it dispatches
 * nothing, or the choose handler below would draw the same map again. */
function fillFromUrl(value) {
  const wrap = form.querySelector(".typeahead");
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

/* ── The axis switch ─────────────────────────────────────────────────── */

/* THE SWITCH CHANGES THE QUESTION, NOT THE ANSWER IN THE BOX.
 *
 * Pressing "Type" re-labels the field, points its suggestion list at the
 * event types and asks the archive the same word again as a class. What is
 * SET is kept: "Delivery" searched as an entity and then switched to a type
 * is exactly the move somebody makes when the first answer was not what
 * they meant, and clearing the box would make them type it twice. If the
 * word is not an event type either, the status line says so in those words
 * - which is the whole point of naming the axis in the sentence. */
function applyAxisToField() {
  const spec = AXIS_FIELDS[axis];
  const wrap = form ? form.querySelector(".typeahead") : null;
  if (!spec || !wrap) return;
  const [label, url, placeholder] = spec;
  const input = wrap.querySelector("input[data-typeahead]");
  const labelEl = wrap.querySelector(".field-label");
  if (labelEl) labelEl.textContent = label;
  if (input) {
    input.dataset.placeholder = placeholder;
    // The field shows what is SET as its placeholder (typeahead.js), so a
    // set term keeps standing there and only an empty field takes the new
    // prompt.
    if (!wrap.classList.contains("is-set")) input.placeholder = placeholder;
    // Through the typeahead's own API: it holds the URL and a cache keyed
    // by what was typed, and writing the data attribute alone would leave
    // the field labelled "Event type" and still offering entity names.
    if (input._typeahead && typeof input._typeahead.setSuggestUrl === "function") {
      input._typeahead.setSuggestUrl(url);
    } else {
      input.dataset.suggest = url;
    }
  }
  if (axisSwitch) {
    axisSwitch.querySelectorAll("[data-axis]").forEach((button) => {
      button.setAttribute("aria-pressed", button.dataset.axis === axis ? "true" : "false");
    });
  }
  if (form) form.dataset.axis = axis;
}

function chooseAxis(next) {
  if (!next || next === axis || !AXIS_FIELDS[next]) return;
  axis = next;
  applyAxisToField();
  // "object" is the default, so it is not written into the URL: a plain map
  // keeps a plain address.
  setExtra("axis", axis === "object" ? "" : axis);
  announce(`Searching by ${AXIS_SUBJECT[axis]}: ${AXIS_FIELDS[axis][0]}`);
  load();
}

/* ── The picture as a file ───────────────────────────────────────────── */

/* The line of words that goes into the image. The caption already says what
 * is drawn; the project and the language say which archive it was drawn
 * from, and a picture without those is unusable a week later. */
function imageCaption() {
  const who = [state.project, state.language].filter(Boolean).join(" - ");
  return `Map${who ? ` - ${who}` : ""} - ${captionText()}`;
}

/* WHAT THE STATUS LINE SAID ABOUT THE DRAWING before a picture was saved.
 *
 * That line is this page's one live region, and it may already carry a fact
 * a reader must not lose - "Only the first 500 entities are drawn". A
 * sentence about a saved file may not push it off the page, so the two are
 * said together, and the map's own half is remembered here rather than read
 * back off the line: pressing the button twice would otherwise stack two
 * copies of it. A new answer clears it (load). */
let statusBeforeImage = null;
let statusBeforeImageWarned = false;

function sayAfterSaving(said, warn) {
  setStatus([statusBeforeImage, said].filter(Boolean).join(" "),
            warn || statusBeforeImageWarned);
  announce(said);
}

async function saveImage() {
  // NOT GUARDED ON A BUTTON. Saving is asked for from the control on the
  // map itself (mapimage.js: addSaveControl); there is no text button in
  // the query row, and a guard on one would turn the feature off without
  // saying so.
  if (statusBeforeImage === null) {
    // ONLY A WARNING IS KEPT. That is the state that may not be hidden; an
    // ordinary line is either empty or transient ("Looking up Apple…") and
    // carrying it into the sentence about a saved file would say something
    // that stopped being true while the picture was being drawn.
    const warned = Boolean(statusBox && statusBox.classList.contains("is-warning"));
    statusBeforeImage = warned ? statusBox.textContent.trim() : "";
    statusBeforeImageWarned = warned;
  }
  if (imageButton) imageButton.disabled = true;
  setStatus("Drawing the picture…");
  try {
    const saved = await saveMapImage({
      container: mapBox,
      // THE PICTURE IS THE MAP, AND NOTHING ELSE. A colour key composed
      // under it would be the taller half of the file: twelve rows of names
      // and counts below a drawing somebody wanted for the drawing. It is
      // on the page beside the map, which is where it is read.
      legend: null,
      caption: imageCaption(),
      credit: ATTRIBUTION_TEXT,
      view: "map",
    });
    sayAfterSaving(saved.tiles
      ? `Saved ${saved.name}. The picture is the map itself, with its connections and the map credit.`
      : `Saved ${saved.name} WITHOUT the map tiles - they come from a host that does not allow it. `
        + "The pins and the connection lines are in the picture.", !saved.tiles);
  } catch (err) {
    sayAfterSaving(errorSentence("The picture could not be saved.", err), true);
  } finally {
    if (imageButton) imageButton.disabled = false;
  }
}

function wire() {
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    const wrap = form.querySelector(".typeahead");
    const hidden = wrap && wrap.querySelector('input[type="hidden"]');
    const input = wrap && wrap.querySelector("input[data-typeahead]");
    // TYPED TEXT WINS OVER WHAT IS SET, because "Show on map" is the Enter
    // key with a mouse, and Enter counts the typed text ("one need not be
    // in the list to search for it", typeahead.js). typeahead.js writes
    // into the hidden input only when a suggestion or Enter confirms the
    // text, so reading the hidden input alone threw away everything
    // somebody typed and then reached for the button with: the box still
    // read "Zzqxwv" while the map showed the whole project.
    const typed = input ? input.value.trim() : "";
    const term = typed || (hidden ? hidden.value.trim() : "");
    // The field, the hidden input and the URL say the same thing from here
    // on: what was typed has just been asked for, so it is what is set.
    fillFromUrl(term);
    setState({ q: term });
    load();
  });
  form.addEventListener("typeahead:choose", (e) => {
    // The term is always taken. Whether the map is redrawn depends on the
    // gesture: Enter is a question being asked, a click in the suggestion
    // list is one still being narrowed down, and the network of a
    // well-connected entity is far too long a query to start on somebody's
    // behalf while they are still choosing.
    const term = (e.detail && e.detail.value) || "";
    fillFromUrl(term);
    setState({ q: term });
    if (e.detail && e.detail.ask) load();
  });

  if (axisSwitch) {
    axisSwitch.addEventListener("click", (e) => {
      const button = e.target.closest("[data-axis]");
      if (button) chooseAxis(button.dataset.axis);
    });
  }

  if (imageButton) imageButton.addEventListener("click", () => { saveImage(); });

  levelButtons.addEventListener("click", (e) => {
    const button = e.target.closest("[data-levels]");
    if (!button) return;
    setExtra("levels", button.dataset.levels);
    markLevels();
    load();
  });

  modeSwitch.addEventListener("click", (e) => {
    const button = e.target.closest("[data-mode]");
    if (!button || button.dataset.mode === mode) return;
    setMode(button.dataset.mode);
    // Both modes are a different QUESTION for the server - one expands the
    // connections and thins the pins to one per entity, the other does
    // neither - so this is a reload, not a filter.
    load();
  });

  if (groupSwitch) {
    groupSwitch.addEventListener("click", (e) => {
      const button = e.target.closest("[data-group]");
      if (!button || button.dataset.group === grouping) return;
      setGrouping(button.dataset.group);
      // A reload for the same reason the mode is: the server regroups the
      // network, and the ends, the pins and the type checklist all change with
      // it. Filtering what is on screen could not produce a bucket that is not
      // there.
      load();
    });
  }

  typesAll.addEventListener("click", () => {
    offTypes = new Set();
    writeHide();
    applyFilters();
    renderTypes();
    announce("Every connection type is shown again");
  });
  // "No types" is not "all types": it draws no line at all, and the caption
  // and the empty row say which switch did it.
  if (typesNone) {
    typesNone.addEventListener("click", () => {
      offTypes = new Set((answer ? answer.types || [] : []).map((t) => t.name));
      writeHide();
      applyFilters();
      renderTypes();
      announce("No connection type is shown");
    });
  }
  if (typesFind) {
    typesFind.addEventListener("input", () => { typeFilter = typesFind.value; renderTypes(); });
  }
  if (placeTypesAll) {
    placeTypesAll.addEventListener("click", () => {
      offPlaceTypes = new Set();
      writeHidePins();
      applyFilters();
      renderPlaceTypes();
      announce("Every location type is shown again");
    });
  }
  if (placeTypesNone) {
    placeTypesNone.addEventListener("click", () => {
      offPlaceTypes = new Set(placeTypes().map((t) => t.name));
      writeHidePins();
      applyFilters();
      renderPlaceTypes();
      announce("No location type is shown");
    });
  }
  if (placeTypesFind) {
    placeTypesFind.addEventListener("input", () => {
      placeTypeFilter = placeTypesFind.value;
      renderPlaceTypes();
    });
  }

  if (retryButton) {
    retryButton.addEventListener("click", () => { if (retryTask) retryTask(); });
  }

  // THE BOW IS A NUMBER OF PIXELS, SO IT IS WRONG AT THE NEXT ZOOM. The
  // curves are bent again rather than rebuilt, so an open popup stays open
  // and the pins are not re-clustered under the reader.
  if (map) map.on("zoomend", reflowArcs);

  // Printing a map that was drawn at another size shows grey tiles.
  window.addEventListener("beforeprint", () => { if (map) map.invalidateSize(); });
}

function init() {
  if (!form || !mapBox) return;
  offTypes = new Set(getExtra("hide").split(",").map((t) => t.trim()).filter(Boolean));
  offPlaceTypes = new Set(getExtra("hidepins").split(",").map((t) => t.trim()).filter(Boolean));
  setMode(getExtra("mode") === "locations" ? "locations" : "connections");
  setGrouping(getExtra("group") === "buckets" ? "buckets" : "entities");
  syncGrouping();
  markLevels();
  fillFromUrl(state.q);
  legend = renderLegend(legendBox, [], {
    title: "Connection colours",
    // Say it once when nothing is grouped, and drop the swatch column
    // rather than repeating one brown down the list (legend.js).
    ungrouped: true,
    emptyText: "Search for an entity to see which colours are on the map.",
    // The second argument legend.js offers - its own idea of on/off - is
    // deliberately ignored: `offTypes` is the only filter state on this page,
    // and reading two of them is how the legend and the checklist started
    // contradicting each other.
    onToggle: (key) => {
      const row = legendBox.querySelector(`.legend-row[data-key="${CSS.escape(key)}"] .legend-name`);
      toggleGroup(key, row ? row.textContent : key);
    },
  });
  map = createMap();
  wire();
  load();
}

init();
