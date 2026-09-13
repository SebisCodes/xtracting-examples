/* ==========================================================================
 *  The Heatmap view.
 *
 *  One question: how many archived addresses sit in each small square of
 *  the world. The server counts them on a fixed grid (three decimals of a
 *  degree, about 110 m) so the picture does not change with the zoom, and
 *  the counts are painted as a HEAT FIELD - leaflet.heat, a wash from yellow
 *  through orange to red, with the radius and the blur set so neighbouring
 *  squares melt into an area instead of standing about as separate blobs.
 *  That is what a heat map is.
 *
 *  TWO THINGS THE ENGINE MAKES EASY TO GET WRONG, AND WHAT THIS PAGE DOES.
 *  In simpleheat (the engine inside leaflet.heat) the painted ALPHA *is* the
 *  value: a point is stamped with `globalAlpha = max(count / max,
 *  minOpacity)` and the colour is then read out of the ramp at that alpha.
 *  Two things follow, and both are handled here rather than escaped:
 *
 *    * A KEY CAN DESCRIBE COLOURS THE CANVAS NEVER PAINTS. A bar of CSS
 *      stops beside a canvas coloured by another rule is two statements
 *      about one drawing, and they drift. The key is SAMPLED FROM THE
 *      GRADIENT ITSELF: `heatColour(count)` builds the same 1x256
 *      gradient the layer is handed and reads the pixel at the same alpha
 *      the layer will stamp - so a swatch is the colour of that count, by
 *      construction, and cannot drift from it. The ramp lives in map.css as
 *      five custom properties, so there is one definition of it in the
 *      product and none of it in this file.
 *    * THE TOP OF THE RAMP IS EASY TO NEVER REACH. leaflet.heat divides every
 *      intensity by `2^(maxZoom - zoom)`, which is 4096 at world zoom, so
 *      every square would end up at `minOpacity` whatever it counted. The layer
 *      is therefore told `maxZoom: <the zoom it is being drawn at>` on every
 *      redraw (see `heatOptions`), which makes that divisor exactly 1 and
 *      the alpha exactly `count / max`.
 *
 *  A FIELD CANNOT CARRY A NUMBER, so the exact reading stays in words and
 *  figures: the list beside the map names the busiest squares and counts
 *  each of them, a click on the field opens the same reading for the square
 *  under the pointer, and both are reachable with the keyboard. That list is
 *  the text alternative (WCAG 1.1.1, MN.gov's interactive-map guide) and it
 *  is also the answer to the field's real weakness - a wash is a shape, not
 *  a value, and somebody has to be able to ask "how many exactly".
 *
 *  AND THE NEXT QUESTION AFTER "HOW MANY" IS "WHO", which is what a click
 *  now opens: the drilldown dialog, listing the ENTITIES in that spot, most
 *  results first, a bucket as one row under its own name, loading more as
 *  the reader scrolls (openSpot below). It is the same dialog the Diagrams
 *  charts and the Dashboard's bars open, from the same module - a reader
 *  who has learnt one listing does not have to learn a second - and it
 *  replaced a Leaflet popup that could say what the spot IS and never what
 *  is in it. Nothing that popup said was lost: the address is the dialog's
 *  title and its two other lines are the sentence under it.
 *
 *  THE SEARCH BOX ASKS FOR A PLACE, and `q` on this page IS a place. Every
 *  view's box searches the thing that view is about - an entity on the Map
 *  and the Graph, an event type on Events - and this view counts addresses,
 *  so "Springfield" and "USA" are what it takes. The server reads the name
 *  with the Query view's rule and answers with the places it counted
 *  (`place.names`), which is what the caption says out loud: a name that
 *  fits two towns counts both, and a reader must not have to guess which
 *  one the number is about.
 *
 *  BECAUSE `q` HERE IS A PLACE AND NOT AN ENTITY, nothing may carry a term
 *  from another view into this one: "Apple" is an entity everywhere else
 *  and no address at all, and a page that opened on it would answer "the
 *  archive has no address in Apple" to a question nobody asked. The entity
 *  question has its own field instead (`entity`), AND-ed with the place, so
 *  "where is Apple concentrated" is answerable without the place box
 *  losing its meaning - two questions, two fields.
 *
 *  AND THAT ENTITY FIELD HAS TWO AXES, the ones the Diagrams pages and the
 *  Map have: its term is read as ONE entity or bucket, or as an EVENT TYPE,
 *  and then the squares counted are the addresses of every entity the
 *  events of that kind are about. It is `entity_axis` in the URL and not
 *  `axis`, because "axis" already means something on this page - the place
 *  and the entity are two axes of one question - and one word may not mean
 *  two things (app/routers/api_map.py says the same in its own docstring).
 *
 *  THE PICTURE CAN LEAVE THE PAGE. "Save image" writes the field, its key
 *  and the attribution to a PNG (static/js/mapimage.js).
 * ========================================================================== */

import { api, isAbort, fmtInt, watchSearch } from "./api.js";
import { state, set as setState, setExtra, getExtra } from "./state.js";
import { announce } from "./a11y.js";
import { tileUrl } from "./minimap.js";
import { saveMapImage, addSaveControl } from "./mapimage.js";
import { heatStops } from "./palette.js";
import { openDrilldown } from "./drilldown.js";
import "./typeahead.js";

const ATTRIBUTION = '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors';
/* The same credit as words, for the picture: a canvas has no links, and the
 * file needs the text. Derived from the one constant above rather than
 * written twice. */
const ATTRIBUTION_TEXT = ATTRIBUTION.replace(/<[^>]*>/g, "");

/* THE GRADIENT IS NOT WRITTEN HERE, AND NOT READ HERE EITHER. Five custom
 * properties in map.css hold it - yellow to red - and static/js/palette.js
 * reads them, because the field under the Locations tab of Diagrams is drawn
 * with the same ramp and two heat maps of one archive may not be two colour
 * schemes. */
/* The alpha the faintest square is stamped with, and therefore the bottom of
 * the key. It is handed to the layer as `minOpacity` and used by
 * `heatColour`, so the two are the same number by construction rather than
 * by agreement. Below about a third the palest wash is not perceivable over
 * a land tile at all - which is the honest weakness of a heat field, and why
 * every square that matters is also a row with a figure in it. */
const MIN_ALPHA = 0.35;
const DEFAULT_RADIUS = 30;    // px: wide enough that two nearby squares melt
/* How much of the radius is the soft edge. Two thirds is what makes a field
 * out of a set of points: less and each square keeps its own hard blob,
 * which is the "separate blobs" this drawing exists not to be. */
const BLUR_SHARE = 0.66;
const TOP_ROWS = 20;
/* Above this many squares the field is repainted from more points than a
 * frame can afford, and the busiest ones are kept. */
const MAX_POINTS = 4000;
/* THE KEY IS A SCALE OF THE RAMP, AND THESE TWO NUMBERS DECIDE ITS ROWS.
 *
 * The rows are alphas spaced EVENLY between the palest and the darkest the
 * field actually paints - both ends included, because those two are the
 * colours a reader has in front of them - and there are as many of them as
 * can be told apart: at most KEY_MAX_ROWS, fewer whenever two neighbours
 * would come closer than KEY_MIN_DE in CIE76. 2.3 is the just-noticeable
 * difference; 12 is comfortably clear of it on a screen that is not
 * colour-managed, at the size this key draws a swatch.
 *
 * WHY THEY ARE NOT DERIVED FROM THE OBSERVED COUNTS. Derived that way, on
 * a large archive the five rows read rgb(254,179,79), (254,175,77),
 * (253,172,75), (253,163,71), (190,1,38) - four of them within dE 8.5 of
 * each other and two pairs below the JND, because 3,995 squares hold 12 to
 * 22,715 locations and four squares hold the rest. Four rows spent on one
 * visible orange: a reader cannot carry a colour off the map back to a
 * row, which is the one thing a key is for. */
const KEY_MAX_ROWS = 9;
const KEY_MIN_DE = 12;
/* The just-noticeable difference. Where even the two ENDS of what is
 * painted are closer than this - a field of nearly equal squares - there is
 * one colour on the map and the key says so with one row. */
const JND = 2.3;

const form = document.getElementById("heat-form");
/* Every request on these channels turns the button quiet and writes
 * "Searching…" beside it, without this file having to remember to
 * (static/js/api.js: watchSearch). */
watchSearch(form, "heat");
const mapBox = document.getElementById("heat-map");
const statusBox = document.getElementById("heat-status");
const captionBox = document.getElementById("heat-caption");
const sizeInput = document.getElementById("heat-radius");
const sizeValue = document.getElementById("heat-radius-value");
const fitButton = document.getElementById("heat-fit");
const visibleOnly = document.getElementById("heat-visible-only");
const fromField = document.getElementById("heat-from");
const toField = document.getElementById("heat-to");
const scaleList = document.getElementById("heat-steps");
const scaleNote = document.getElementById("heat-scale-note");
const typesCard = document.getElementById("heat-types-card");
const typesList = document.getElementById("heat-types");
const typesAll = document.getElementById("heat-types-all");
const typesNone = document.getElementById("heat-types-none");
const typesFind = document.getElementById("heat-types-find");
const typesNote = document.getElementById("heat-types-note");
const typesCount = document.getElementById("heat-types-count");
const topList = document.getElementById("heat-top");
const topCount = document.getElementById("heat-top-count");
const retryBox = document.getElementById("heat-retry");
const retryButton = document.getElementById("heat-retry-button");
const imageButton = document.getElementById("heat-image");
const axisSwitch = form ? form.querySelector("[data-axis-switch]") : null;
/* The key, for the picture: the card the steps are listed in, not the list
 * alone - a key without its heading is a column of coloured boxes. */
const scaleCard = scaleList ? scaleList.closest("section, .card") : null;

let map = null;
let layer = null;
let answer = null;
/* HOW THE ENTITY FIELD READS ITS TERM: "object" (one entity or a bucket) or
 * "type" (an event type). The server renders the page with it decided from
 * ?entity_axis=, and this reads that decision back off the form so the page
 * and the server cannot disagree about what was asked. */
let entityAxis = (form && form.dataset.entityAxis === "type") ? "type" : "object";

/* What the entity field asks for on each axis: the label, the suggestion
 * list, the placeholder. templates/heatmap.html renders the current one and
 * holds the same table. */
const AXIS_FIELDS = {
  object: ["Entity or bucket", "/api/suggest/entity_or_bucket", "Every entity"],
  type: ["Event type", "/api/suggest/event_type", "Every event type"],
};

/* The word for what one axis searches, for the sentences that say what was
 * counted and what was not found. */
const AXIS_SUBJECT = { object: "entity", type: "event type" };

let offTypes = new Set();
/* What is typed into the box above the checklist. It narrows the LIST, never
 * the map: a filter you cannot see the whole of is a filter you cannot
 * undo, so hiding a row here never changes what is counted. */
let typeFilter = "";
let moveTimer = null;
let retryTask = null;
/* A place search asks about a part of the world the reader is not looking at
 * yet, so the request that goes to find it is NOT bounded by the viewport,
 * even with "Only the visible area" ticked: searching Springfield from a map
 * of Europe would otherwise answer "nothing in the part of the map you are
 * looking at", which is true of the screen and false of the archive. The map
 * then moves to what came back and the next pan counts the visible area
 * again. It stays set through a failed request, so "Try again" repeats the
 * request that was made and not a narrower one. */
let movingToPlace = false;
/* The place the map was last moved to. A new place is a new part of the
 * world and the map goes there; a tick in the checklist is the same part of
 * the world seen differently and the map must not jump under the reader.
 * `null` is "nothing has been drawn yet", which is not the same as "" - the
 * whole project - or the map would not fit itself on the first load. */
let fittedTo = null;
/* Where a square is, by its key, so a row in the list and a click on the
 * field can both find the same square. The field itself is one canvas and
 * has no per-square object to hold. */
/* TWO WORDS FOR ONE THING, AND THE DIFFERENCE IS DELIBERATE.
 *
 * The unit of counting IS a square: the server rounds every address to
 * three decimals, which lays a grid of about 110 metres over the world and
 * counts what falls in each cell (app/routers/api_map.py: map_heat). This
 * file calls that a square, because that is what it is.
 *
 * The reader is told "spot". Nothing square is ever drawn - the field is a
 * radial gradient per point, and what is on screen is a soft blob that
 * melts into its neighbours. Printing "square" beside a picture with no
 * straight edge in it makes the reader look for something that is not
 * there, and reads as a claim about the shape rather than about the
 * arithmetic. The grid is the About disclosure's business; the key and the
 * lists say spot. */
const squares = new Map();    // "lat|lng" -> [lat, lng, count]

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function number(input, fallback) {
  const value = parseInt(input.value, 10);
  return Number.isFinite(value) ? value : fallback;
}

function setStatus(text, warn) {
  statusBox.textContent = text || "";
  statusBox.classList.toggle("is-warning", Boolean(warn));
}

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
  return [what, sentence(err && err.message), sentence(err && err.hint)]
    .filter(Boolean).join(" ");
}

/* ── The colour ──────────────────────────────────────────────────────── */

/* THE GRADIENT, ONCE, AS THE LAYER WILL SEE IT.
 *
 * `{0: yellow, 0.25: …, 1: red}` is the shape leaflet.heat wants, and the
 * values come off the root element, so map.css is the only place the ramp is
 * written down. Read on every call rather than cached: the tokens can change
 * with a theme, and this is five property reads. */

/* The gradient as 256 pixels, which is what simpleheat turns it into before
 * it colours anything: a 1x256 canvas filled with a linear gradient of the
 * same stops, read back as bytes. Cached, because a canvas per swatch would
 * be one canvas per key row per redraw. */
let gradientBytes = null;
function gradientPixels() {
  if (gradientBytes) return gradientBytes;
  const canvas = document.createElement("canvas");
  canvas.width = 1;
  canvas.height = 256;
  const ctx = canvas.getContext("2d");
  if (!ctx) return null;
  const grad = ctx.createLinearGradient(0, 0, 0, 256);
  const stops = heatStops();
  Object.keys(stops).forEach((at) => grad.addColorStop(Number(at), stops[at]));
  ctx.fillStyle = grad;
  ctx.fillRect(0, 0, 1, 256);
  gradientBytes = ctx.getImageData(0, 0, 1, 256).data;
  return gradientBytes;
}

/* One point of the gradient, 0 to 1, as [r, g, b]. This is the same lookup
 * simpleheat does (`_colorize` indexes the ramp by the painted alpha byte),
 * so a colour taken from here is a colour the canvas paints. */
function sampleRgb(at) {
  const px = gradientPixels();
  const t = Math.min(1, Math.max(0, Number(at) || 0));
  if (!px) return null;
  const i = Math.round(t * 255) * 4;
  return [px[i], px[i + 1], px[i + 2]];
}

/* The same point as an rgb() string, which is what a swatch is set to. */
function sampleGradient(at) {
  const rgb = sampleRgb(at);
  return rgb ? `rgb(${rgb[0]}, ${rgb[1]}, ${rgb[2]})` : "";
}

/* HOW FAR APART TWO COLOURS LOOK, in CIE76 dE - sRGB to linear to XYZ (D65)
 * to L*a*b*, then the distance between the two Lab points. It is the oldest
 * and simplest of the difference formulae and the one this key needs: the
 * question here is only "can these two be told apart", whose answer is
 * about 2.3 (the just-noticeable difference), and dE76 is accurate enough
 * for a yes/no that far above the threshold. */
function linear(c) {
  const v = c / 255;
  return v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4;
}

function lab(rgb) {
  if (!rgb) return [0, 0, 0];
  const [r, g, b] = rgb.map(linear);
  // D65, the white point sRGB is defined against.
  const x = (r * 0.4124 + g * 0.3576 + b * 0.1805) / 0.95047;
  const y = (r * 0.2126 + g * 0.7152 + b * 0.0722);
  const z = (r * 0.0193 + g * 0.1192 + b * 0.9505) / 1.08883;
  const f = (t) => (t > 0.008856 ? Math.cbrt(t) : (7.787 * t) + (16 / 116));
  const [fx, fy, fz] = [f(x), f(y), f(z)];
  return [(116 * fy) - 16, 500 * (fx - fy), 200 * (fy - fz)];
}

function deltaE(a, b) {
  const [l1, a1, b1] = lab(a);
  const [l2, a2, b2] = lab(b);
  return Math.hypot(l1 - l2, a1 - a2, b1 - b2);
}

/* THE ONE PLACE A COUNT BECOMES A COLOUR, and it is simpleheat's own
 * arithmetic written out: the layer stamps a square with
 * `globalAlpha = max(count / max, minOpacity)` and then reads the ramp at
 * that alpha. `heatOptions` hands it the same `minOpacity` and the same
 * gradient, so the key and the field cannot disagree about a colour.
 *
 * Where two squares overlap the field adds their alphas up and the wash goes
 * darker than either of them alone - which is the point of a field, and what
 * the note under the key says out loud. */
function alphaFor(count) {
  const top = rampMax();
  return Math.max(MIN_ALPHA, Math.min(1, Math.max(0, Number(count) || 0) / top));
}

function heatColour(count) {
  return sampleGradient(alphaFor(count));
}

/* The busiest square there is, and how many DIFFERENT counts there are to
 * tell apart. */
function scaleNow() {
  const points = drawnPoints();
  return { max: (answer && answer.max) || 1, classes: new Set(points.map((p) => p[2])).size };
}

/* WHAT COUNT THE TOP OF THE RAMP STANDS FOR - and it is not always the
 * busiest square.
 *
 * The ramp is relative to what is drawn, which is what makes it readable at
 * any zoom; it also means a filter that leaves ONE count behind would
 * repaint it. Linjiang, one location, the palest thing on the unfiltered
 * map, would be "the busiest there is" as soon as it is the only square
 * left. With
 * nothing to compare it to there is no scale to read, so the top of the ramp
 * is pushed out of reach and the one count is drawn at the BOTTOM, where one
 * location sits everywhere else on this page. The note under the key says
 * the shades are relative.
 *
 * The layer and the key are both given this number (heatOptions, alphaFor),
 * so they cannot disagree about what a colour means. */
function rampMax() {
  const scale = scaleNow();
  if (scale.classes <= 1) {
    const points = drawnPoints();
    const only = points.length ? points[0][2] : 1;
    return Math.max(1, only / MIN_ALPHA);
  }
  return Math.max(1, scale.max);
}

/* The squares that are PAINTED, busiest last. Everything downstream - the
 * field, the key, the cap warning - works from this one list, so the key can
 * never name a colour that is only on a square the cap left out. */
function drawnPoints() {
  if (!answer || !answer.points) return [];
  return [...answer.points].sort((a, b) => a[2] - b[2]).slice(-MAX_POINTS);
}

/* The alphas the key draws a row at: evenly spaced from the palest painted
 * to the darkest, as many as can be told apart. Both ends are always rows -
 * they are the two colours actually on the map - so the number of steps is
 * what gives way, not the span. */
function keyStops(from, to) {
  if (!(to > from)) return [from];
  const spaced = (n) => {
    const out = [];
    for (let i = 0; i < n; i += 1) out.push(from + ((to - from) * i) / (n - 1));
    return out;
  };
  const apart = (at, least) => at.every(
    (a, i) => i === 0 || deltaE(sampleRgb(a), sampleRgb(at[i - 1])) >= least);
  for (let n = KEY_MAX_ROWS; n > 1; n -= 1) {
    const at = spaced(n);
    if (apart(at, KEY_MIN_DE)) return at;
  }
  // Two ends closer than KEY_MIN_DE but still separable: two rows, which is
  // the honest reading of a field with little range in it.
  return deltaE(sampleRgb(from), sampleRgb(to)) > JND ? [from, to] : [from];
}

/* THE KEY IS THE RAMP, IN STEPS A READER CAN TELL APART.
 *
 * Not built from the observed counts - one row per colour that happens to
 * be on the field. On a large archive that puts four rows on one visible
 * orange: 3,995 squares holding 12 to 22,715 locations and four squares
 * holding 25,955 to 71,512, so the ramp's whole span is spent on five
 * squares and everything else collapses into its bottom. The five swatches
 * measure dE 2.21, 1.34, 3.53 and 4.99 apart - two pairs below the
 * just-noticeable difference - and a key whose rows cannot be told apart
 * cannot be carried back to the map, which is the only thing it is for.
 *
 * So the rows come from the RAMP and the counts are read off it: walk the
 * gradient between the palest and the darkest alpha the field actually
 * paints, start a new row wherever the colour has moved KEY_MIN_DE from the
 * one above it, and say which counts that row covers (`alphaFor` inverted -
 * alpha is count/rampMax, so count is alpha*rampMax). Consecutive swatches
 * therefore differ by more than a JND by construction.
 *
 * The two ends are the field's own: the first row is the colour of the
 * faintest square drawn and the last is the colour of the busiest, so a key
 * with ONE row (one count, nothing to compare it to) is still that square's
 * colour and nothing else. A row's squares count can be 0 - that is the
 * scale saying "no square is this dark yet", which is a fact about the map
 * and not a gap in the key. */
function scaleRows() {
  const points = drawnPoints();
  if (!points.length) return [];
  let low = points[0][2];
  let high = points[0][2];
  points.forEach((p) => {
    if (p[2] < low) low = p[2];
    if (p[2] > high) high = p[2];
  });
  const top = rampMax();
  const from = alphaFor(low);
  const to = alphaFor(high);
  const stops = keyStops(from, to);
  const rows = [];
  stops.forEach((at, i) => {
    const next = stops[i + 1];
    // Where this step starts, in counts - `alphaFor` read backwards. The
    // bottom row starts at the faintest square there is rather than at the
    // count its alpha works out to: everything below `minOpacity` is stamped
    // at the floor, so all of those counts are in this row and painted this
    // colour.
    const edge = i === 0 ? low : Math.max(low, Math.ceil(at * top));
    const first = rows.length ? Math.max(edge, rows[rows.length - 1].high + 1) : edge;
    const last = next === undefined ? high : Math.min(high, Math.ceil(next * top) - 1);
    // A STEP THAT NO WHOLE COUNT FALLS INTO IS NOT A ROW. Counts are
    // integers, and on a small map there are three of them: without this,
    // five steps of the ramp become five rows of which four say "2
    // locations", in four shades, three of which nothing is painted in.
    if (last < first) return;
    rows.push({
      // THE COLOUR THE FIELD ACTUALLY PAINTS THIS ROW'S LOWEST COUNT, not
      // the colour at the step's own alpha. Where the counts are far apart
      // the two are the same to the eye; where they are not - three counts,
      // nine steps - the step's alpha is a colour no square carries, and a
      // swatch has to be a colour on the map.
      colour: heatColour(first),
      low: first,
      high: last,
      squares: 0,
    });
  });
  if (rows.length) {
    // The last row always reaches the busiest square: a step that was
    // dropped above must not take the top of the scale with it.
    rows[rows.length - 1].high = Math.max(rows[rows.length - 1].high, high);
    rows.forEach((row) => {
      row.squares = points.filter((p) => p[2] >= row.low && p[2] <= row.high).length;
    });
  }
  return rows;
}

function renderScale() {
  scaleList.textContent = "";
  const rows = scaleRows();
  if (!rows.length) {
    scaleList.appendChild(el("li", "empty", "Nothing counted yet."));
    scaleNote.textContent = "";
    return;
  }
  rows.forEach((row) => {
    const item = el("li", "heat-step");
    const swatch = el("span", "heat-swatch");
    swatch.style.background = row.colour;
    swatch.setAttribute("aria-hidden", "true");
    const counted = row.low === row.high
      ? `${fmtInt(row.low)} ${row.low === 1 ? "location" : "locations"}`
      : `${fmtInt(row.low)} to ${fmtInt(row.high)} locations`;
    item.appendChild(swatch);
    item.appendChild(el("span", "heat-step-label", counted));
    item.appendChild(el("span", "heat-step-count", row.squares
      ? `${fmtInt(row.squares)} ${row.squares === 1 ? "spot" : "spots"}`
      : "no spots"));
    scaleList.appendChild(item);
  });
  // WHAT THE NOTE IS FOR, and what it is not for.
  //
  // The ramp is a share of the busiest spot drawn NOW, so a reader who
  // filters and watches the colours move has to be told that this is the
  // scale moving and not the archive. And a field ADDS where it overlaps,
  // which is the one thing a key of single colours cannot show. Those two
  // are worth a sentence each; they are not guessable.
  //
  // Not "Red means more archived locations in one square, not more
  // importance." Both halves are wrong to print: a key that runs from few
  // to many beside a ramp that runs from yellow to red says the first half
  // by itself, and the second half answers a question nobody asks - it
  // names a confusion (with the importance scale on other views) that the
  // reader has not made until it is mentioned.
  const busiest = fmtInt(scaleNow().max);
  scaleNote.textContent =
    `The shades are set by the busiest spot drawn now (${busiest}), so they change when you filter. `
    + "Each row above is one step of that ramp and the counts it stands for, so two rows are always "
    + "far enough apart to be told apart on the map. "
    + "Where spots lie close together the field adds them up and goes darker than either alone; "
    + "the list below gives the exact count of each spot.";
}

/* ── The map ─────────────────────────────────────────────────────────── */

function createMap() {
  if (!window.L) {
    mapBox.textContent = "";
    mapBox.appendChild(el("p", "map-empty",
      "The map could not be loaded. The busiest places are listed beside it."));
    return null;
  }
  const L = window.L;
  const instance = L.map(mapBox, {
    // THE WHEEL ZOOMS, PLAINLY. This view is the whole window - there is
    // nothing to scroll past it and the wheel has no other job here. (The
    // Query page's small map sits inside a form and has to ask first; see
    // the header of minimap.js.) The +/- buttons stay: a wheel is not
    // available to everyone.
    scrollWheelZoom: true,
    keyboard: true,
    zoomControl: true,
    preferCanvas: true,
  }).setView([20, 0], 2);
  L.tileLayer(tileUrl(), { maxZoom: 18, crossOrigin: true, attribution: ATTRIBUTION }).addTo(instance);
  // THE ONE LINE THAT MAKES THE FIELD REACH THE TOP OF ITS RAMP. leaflet.heat
  // divides every intensity by 2^(maxZoom - zoom), which is 4096 at world
  // zoom, so unless the layer believes the current zoom IS its maximum every
  // square is stamped at minOpacity whatever it counts. Set before the
  // layer's own moveend redraw, which Leaflet fires after zoomend.
  instance.on("zoomend", () => {
    if (layer) layer.options.maxZoom = instance.getZoom();
  });
  instance.on("moveend", () => {
    if (!visibleOnly.checked) return;
    // Panning must not fire a request per pixel.
    if (moveTimer) window.clearTimeout(moveTimer);
    moveTimer = window.setTimeout(() => load(), 400);
  });
  // A FIELD HAS NO CLICKABLE PARTS, so the map answers for the square under
  // the pointer: the same dialog, the same sentence in the live region and
  // the same row marked in the list as activating that row would give. The
  // reading a graduated circle would carry in its own shape is kept this
  // way, without the circles.
  //
  // A CLICK THAT LANDS ON NOTHING IS NOT A QUESTION, and it must stay that
  // way given that a click opens a modal dialog: `nearestSquare` answers only
  // within one heat radius of a counted square, so a click on empty ocean
  // still leaves the map alone (and the keyboard on it).
  instance.on("click", (e) => {
    const near = nearestSquare(e.latlng);
    if (near) reveal(rowFor(near), near[0], near[1], near[2], { move: false });
  });

  /* HOW MANY ARE UNDER THE POINTER, BEFORE ANYTHING IS CLICKED.
   *
   * A wash is a shape and not a value: two patches that look alike can be
   * four locations and forty, and the only way to learn which was to open
   * the spot. The tooltip answers the first question where it is asked -
   * "how many are in there" - and the click still answers the second, which
   * is who they are.
   *
   * The same square a click would find (nearestSquare), so what the reader
   * is told is what they would open; and nothing at all outside one radius
   * of a counted square, because a sentence that follows the pointer across
   * empty ocean is a sentence about nowhere.
   */
  instance.on("mousemove", (e) => {
    const near = nearestSquare(e.latlng);
    // AND THE POINTER SAYS THE PATCH CAN BE OPENED. A click on the field
    // opens the spot, and a field that gives no sign of it is a control
    // nobody knows is there: the cursor is the one place a pointer user
    // looks for that, and it changes exactly where the click would land
    // (nearestSquare) - not over the whole map, which would promise a
    // dialog on empty ocean.
    overSpot(Boolean(near));
    if (!near) { hideSpotTip(); return; }
    showSpotTip(near[0], near[1], near[2]);
  });
  instance.on("mouseout", () => { hideSpotTip(); overSpot(false); });
  instance.on("movestart", hideSpotTip);


  /* The way to take the picture away, on the picture (mapimage.js says why
   * it is here and not in the row of controls under the map). */
  addSaveControl(L, instance, { label: "Save image of the heat field", run: () => saveImage() });

  return instance;
}

/* The counted square nearest a point, within one heat radius of it in
 * PIXELS - so what a click finds is what the click looked as if it hit,
 * whatever the zoom. */
function nearestSquare(latlng) {
  if (!map) return null;
  const points = drawnPoints();
  if (!points.length) return null;
  const reach = number(sizeInput, DEFAULT_RADIUS);
  const at = map.latLngToContainerPoint(latlng);
  let best = null;
  let bestGap = Infinity;
  points.forEach((point) => {
    const q = map.latLngToContainerPoint([point[0], point[1]]);
    const gap = Math.hypot(q.x - at.x, q.y - at.y);
    // A tie goes to the busier square: it is the one the wash under the
    // pointer is mostly made of.
    if (gap < bestGap || (gap === bestGap && best && point[2] > best[2])) {
      bestGap = gap;
      best = point;
    }
  });
  return bestGap <= reach ? best : null;
}

/* The row in the list beside the map for a square, when it has one - only
 * the busiest are listed, so a click on the field often has none. */
function rowFor(point) {
  const key = squareKey(point[0], point[1]);
  return [...topList.querySelectorAll(".map-item")]
    .find((button) => button.dataset.square === key) || null;
}

function squareKey(lat, lng) {
  return `${lat.toFixed(3)}|${lng.toFixed(3)}`;
}

/* The address of a square, when the archive knows one. Only the busiest
 * squares carry it (the server sends `top` for the list beside the map), so
 * the rest of them are named by their coordinates - two decimal numbers are
 * not a place, but they are better than nothing at all. */
function addressOf(lat, lng) {
  if (!answer) return "";
  const key = squareKey(lat, lng);
  const hit = (answer.top || []).find((s) => squareKey(s.lat, s.lng) === key);
  return hit ? hit.address : "";
}

function counted(n) {
  return `${fmtInt(n)} ${n === 1 ? "location" : "locations"}`;
}

/* ONE TOOLTIP, MOVED - never one per square. A heat field can hold four
 * hundred of them, and four hundred Leaflet tooltips are four hundred
 * elements in the DOM for one that is ever on the screen. */
let spotTip = null;

/* The class the stylesheet turns into a pointer (css/map.css). On the
 * container, because that is the element Leaflet sets `grab` on. */
function overSpot(on) {
  const box = map && map.getContainer ? map.getContainer() : null;
  if (box) box.classList.toggle("is-over-spot", Boolean(on));
}

function showSpotTip(lat, lng, count) {
  if (!map || !window.L) return;
  if (!spotTip) {
    spotTip = window.L.tooltip({
      direction: "top", offset: [0, -6], opacity: 1, className: "map-tip",
    });
  }
  // THE SAME TWO-LINE BOX EVERY MAP IN THIS PRODUCT HOVERS WITH (css/views.css):
  // what is there on the first line, where it is on the second.
  const box = el("div", "map-tip-body");
  box.appendChild(el("span", "map-tip-name", counted(count)));
  const where = addressOf(lat, lng) || `${lat.toFixed(3)}, ${lng.toFixed(3)}`;
  box.appendChild(el("span", "map-tip-where", where));
  spotTip.setLatLng([lat, lng]).setContent(box);
  if (!map.hasLayer(spotTip)) spotTip.addTo(map);
}

function hideSpotTip() {
  if (spotTip && map && map.hasLayer(spotTip)) map.removeLayer(spotTip);
}


function squareSentence(lat, lng, count) {
  const where = addressOf(lat, lng) || `${lat.toFixed(3)}, ${lng.toFixed(3)}`;
  return `${where} - ${counted(count)} in this spot of about 110 metres`;
}

/* Everything the heat layer is configured with, in one place, so the key and
 * the field are handed the same gradient and the same `minOpacity` and no
 * caller can pass one of them a different idea of the ramp. */
function heatOptions() {
  const radius = number(sizeInput, DEFAULT_RADIUS);
  return {
    radius,
    // The soft edge is what turns points into an area (BLUR_SHARE).
    blur: Math.max(1, Math.round(radius * BLUR_SHARE)),
    max: rampMax(),
    minOpacity: MIN_ALPHA,
    // See createMap: without this the intensity is divided by 2^(maxZoom -
    // zoom) and nothing ever reaches the top of the ramp.
    maxZoom: map ? map.getZoom() : 18,
    gradient: heatStops(),
  };
}

function draw() {
  if (!map || !window.L) return;
  const L = window.L;
  squares.clear();
  const drawn = drawnPoints();
  drawn.forEach((point) => squares.set(squareKey(point[0], point[1]), point));
  if (!drawn.length) {
    if (layer) { map.removeLayer(layer); layer = null; }
    return;
  }
  if (!L.heatLayer) {
    // The plugin is not in this build: say so where the drawing would be,
    // rather than leaving an empty map beside a list full of counts.
    if (!mapBox.querySelector(".map-empty")) {
      mapBox.appendChild(el("p", "map-empty",
        "The heat layer could not be loaded. The busiest spots are listed beside it."));
    }
    return;
  }
  if (!layer) {
    layer = L.heatLayer(drawn, heatOptions());
    layer.addTo(map);
  } else {
    layer.setLatLngs(drawn);
    layer.setOptions(heatOptions());
  }
}

/* The radius slider changes how wide one location is painted. The layer
 * keeps its points and is only re-configured, so a drag of the slider is a
 * repaint rather than a rebuild. */
function resize() {
  if (!layer) return;
  layer.setOptions(heatOptions());
}

function fit() {
  if (!map || !answer || !answer.bounds) return;
  const [[south, west], [north, east]] = answer.bounds;
  if (south === north && west === east) map.setView([south, west], 9);
  else map.fitBounds(window.L.latLngBounds([[south, west], [north, east]]), { padding: [30, 30] });
}

/* ── The list beside the map ─────────────────────────────────────────── */

/* A row was activated: mark it, move the map, open the spot and SAY what
 * happened. Before this, a click on a row silently setView()ed and that was
 * all - no live region, no reading, no marked row - while the Map page did
 * all three for a row that looks exactly the same. Two pages must not teach
 * two behaviours for one gesture, which is also why a click ON THE FIELD
 * comes through here: one gesture, one answer, wherever it was made. */
function reveal(button, lat, lng, count, opts = {}) {
  const said = squareSentence(lat, lng, count);
  if (button) {
    topList.querySelectorAll(".map-item.is-active")
      .forEach((other) => other.classList.remove("is-active"));
    button.classList.add("is-active");
  }
  if (map) {
    // A ROW HAS TO BRING THE MAP TO THE SQUARE; A CLICK ON THE MAP IS
    // ALREADY THERE. Re-centring under the pointer that has just clicked
    // would move the answer away from the question - so `move` is true only
    // for the list, which is where the reader cannot see the square yet.
    if (opts.move !== false) {
      map.getContainer().scrollIntoView({ block: "nearest" });
      map.setView([lat, lng], Math.max(map.getZoom(), 9));
    }
  }
  setStatus(said);
  announce(said);
  openSpot(lat, lng, count);
}

/* ── What is inside a spot ───────────────────────────────────────────── */

/* WHO IS STANDING THERE - THE DRILLDOWN DIALOG, NOT A POPUP OF OUR OWN.
 *
 * A Leaflet popup with three lines in it - the address, the count, and the
 * coordinates - says what the spot IS and cannot say what is IN it, which
 * is the question a reader has once they have found the hot spot - and the
 * list is long enough on a real archive that it has to load as they scroll.
 *
 * WHAT A POPUP WOULD SAY IS STILL SAID. The address is the dialog's title,
 * the count and the "about 110 metres" sentence are the line under it, and
 * the coordinates are on that line too.
 *
 * IT IS THE SAME DIALOG THE DIAGRAMS CHARTS AND THE DASHBOARD'S BARS OPEN
 * (static/js/drilldown.js), and it is a third CALLER rather than a third
 * component: the counter, the scroll container, the sentinel that loads the
 * next page, the "Load more" button and the focus handling all come with
 * it. A panel written here would be a second answer to "how do I get out of
 * this", which the product does not allow (tests/ui/test_dialog_shell.py).
 */
function openSpot(lat, lng, count) {
  const at = { lat: lat.toFixed(3), lng: lng.toFixed(3) };
  const coords = `${at.lat}, ${at.lng}`;
  const where = addressOf(lat, lng);
  // Declared before the call because `load` below closes over it: the first
  // page is fetched from inside openDrilldown, and the line that rewrites
  // the subtitle runs after that fetch - by which time this is assigned.
  let handle = null;
  handle = openDrilldown({
    title: where || coords,
    subtitle: spotSentence(count, coords),
    rowsLabel: `The entities in the spot at ${coords}`,
    emptyText: "Nothing is in this spot any more - the archive may have changed.",
    // No Download CSV: the file behind a drilldown is the ROWS of the
    // archive, and these are groups of them. The link on every row is that
    // entity's own Diagrams, which is the next question a reader has.
    links: { entity: (term) => diagramsUrl(term) },
    load: async (page) => {
      const data = await api("/api/map/spot", {
        // Its own channel: a page of this list must not abort the count
        // that is drawing the field underneath it.
        channel: "heat-spot",
        params: {
          lat: at.lat, lng: at.lng,
          // THE FOUR FILTERS THE FIELD WAS DRAWN WITH, sent again. The
          // server re-applies them (routers/api_map.py: _heat_filters);
          // without them the dialog would list rows the spot was never
          // counted from and say a bigger number than the row beside the
          // map does.
          q: place(), types: wantedTypes(),
          entity: entityTerm(), entity_axis: entityAxis,
          date_from: dateRange().from, date_to: dateRange().to,
          ddpage: page,
        },
      });
      // THE SENTENCE UNDER THE TITLE BECOMES THE SERVER'S. The dialog is
      // given a subtitle before it can know anything - the count the field
      // was painted with - and the first page brings the number the server
      // counted under the same filters. They agree, and the point of
      // replacing it is that they have to: if a filter were ever dropped on
      // the way, this is the line that would say so out loud rather than
      // the dialog quietly listing more than the spot holds.
      if (page === 1) saySpot(handle, spotSentence(data.locations, coords));
      return data;
    },
  });
}

/* The line under the dialog's title: how much is here, on what grid, where
 * - and every narrowing that is in force, because a count with a filter
 * behind it and no word about the filter is a wrong answer to the question
 * the reader thinks they asked. */
function spotSentence(count, coords) {
  const narrowed = `${entityClause()}${placeClause()}${datesClause()}`
    + (offTypes.size ? " of the ticked location types" : "");
  return `${counted(count)} in this spot of about 110 metres${narrowed} - ${coords}`;
}

/* THE SUBTITLE IS SET WHEN THE DIALOG OPENS AND THIS IS THE ONLY WAY BACK
 * AT IT. dialog.js takes the line as an option and owns the element; the
 * number in it is not known until the first page has been fetched. Reaching
 * for the element the shell built is the smaller of two wrongs - the other
 * being a second, private line inside the body saying the same thing in a
 * different place from every other dialog in the product. */
function saySpot(handle, text) {
  const line = handle && handle.dialog
    ? handle.dialog.querySelector(".dialog-subtitle") : null;
  if (line) line.textContent = text;
}

/* That entity's own Diagrams, with the pair appended - the link every
 * drilldown row carries, built the way dashboard.js and diagrams.js build
 * it. The Locations tab, because the row came off a map. */
function diagramsUrl(term) {
  const p = new URLSearchParams();
  if (state.project) p.set("project", state.project);
  if (state.language) p.set("language", state.language);
  p.set("q", term);
  // A single name is read as one thing, whatever axis this page was
  // searched on: the row is an entity or a bucket, never an event type.
  p.set("axis", "object");
  p.set("tab", "locations");
  return `/diagrams/entity?${p.toString()}`;
}

/* The busiest squares, by NAME. Two decimal numbers are not a place - and
 * this list is the drawing's text alternative, the one thing a reader who
 * cannot see a canvas has. The archive knows what is at 37.332, -122.031
 * (`text_address`), so the row says "Cupertino, California, USA" and keeps
 * the coordinates underneath for whoever wants them. */
function renderTop() {
  topList.textContent = "";
  const rows = answer ? (answer.top || []).slice(0, TOP_ROWS) : [];
  topCount.textContent = answer && answer.cells
    ? `${fmtInt(answer.cells)} ${answer.cells === 1 ? "spot" : "spots"}` : "";
  if (!rows.length) {
    topList.appendChild(el("li", "empty", EMPTY_ROW[emptyReason()]()));
    return;
  }
  rows.forEach((square) => {
    const { lat, lng, count } = square;
    const where = `${lat.toFixed(3)}, ${lng.toFixed(3)}`;
    const item = el("li");
    const button = el("button", "map-item");
    button.type = "button";
    button.dataset.square = squareKey(lat, lng);
    const counted = `${fmtInt(count)} ${count === 1 ? "location" : "locations"}`;
    const title = el("span", "item-title");
    // The row carries the square's own colour, so the list and the drawing
    // can be matched by eye as well as by name.
    const swatch = el("span", "swatch");
    swatch.style.background = heatColour(count);
    swatch.setAttribute("aria-hidden", "true");
    title.appendChild(swatch);
    title.appendChild(document.createTextNode(
      square.address ? `${counted} - ${square.address}` : counted));
    button.appendChild(title);
    button.appendChild(el("span", "item-sub", where));
    button.addEventListener("click", () => reveal(button, lat, lng, count));
    item.appendChild(button);
    topList.appendChild(item);
  });
}

/* THE CHECKLIST, AND ITS TWO CONTROLS.
 *
 * The list grows with the archive - a real one has dozens of location types
 * - so it gets two controls:
 * a box that narrows the LIST (never the count: a filter you cannot see the
 * whole of is a filter you cannot undo), and All / None in one gesture each.
 *
 * The count in the header and the note underneath are there because both of
 * those hide rows: "13 types, 4 shown, 2 switched off" is state, and state
 * that is not on the screen is state nobody can get back out of. */
function renderTypes() {
  typesList.textContent = "";
  const types = answer ? answer.types || [] : [];
  typesCard.hidden = !types.length;
  const needle = typeFilter.trim().toLowerCase();
  const shown = types.filter(
    (type) => !needle || (type.name || "").toLowerCase().includes(needle));
  shown.forEach((type) => {
    const item = el("li");
    const label = el("label", "type-check");
    const box = document.createElement("input");
    box.type = "checkbox";
    box.checked = !offTypes.has(type.name);
    box.addEventListener("change", () => {
      if (box.checked) offTypes.delete(type.name);
      else offTypes.add(type.name);
      setExtra("hide", [...offTypes].join(","));
      load();
    });
    label.appendChild(box);
    label.appendChild(el("span", "type-name", type.name || "Without a type"));
    label.appendChild(el("span", "type-count", fmtInt(type.count)));
    item.appendChild(label);
    typesList.appendChild(item);
  });
  if (types.length && !shown.length) {
    typesList.appendChild(el("li", "empty", `No location type contains “${typeFilter.trim()}”.`));
  }
  const off = types.filter((type) => offTypes.has(type.name)).length;
  typesCount.textContent = `${fmtInt(types.length)} ${types.length === 1 ? "type" : "types"}`;
  const said = [];
  if (needle) said.push(`${fmtInt(shown.length)} of them shown by the box above`);
  if (off) said.push(`${fmtInt(off)} switched off`);
  typesNote.textContent = said.length
    ? `${said.join(", ")}. Typing in the box hides rows; only a tick changes what is counted.`
    : "";
}

function wantedTypes() {
  if (!answer) return "";
  const all = (answer.types || []).map((t) => t.name);
  const on = all.filter((name) => !offTypes.has(name));
  // Every box ticked means "no filter": sending the full list would drop
  // every location whose type is not in the (capped) checklist.
  return on.length === all.length ? "" : on.join(",");
}

/* NO BOX TICKED IS NOT THE SAME REQUEST AS EVERY BOX TICKED.
 *
 * `types=` (an empty list) is how this page asks for "no filter", so
 * unticking the LAST type sent exactly the request an untouched page sends
 * and the map came back full: 4 ticked = 13 locations, 3 = 8, 2 = 3, 1 = 1,
 * NONE = 13 again. Unticking the last one has to answer "then there is
 * nothing to count", and the only way to say that through a parameter that
 * means "everything" is not to send it - so the request is not made at all.
 */
function noneTicked() {
  if (!answer) return false;
  const all = (answer.types || []).map((t) => t.name);
  return all.length > 0 && all.every((name) => offTypes.has(name));
}

/* ── The place ───────────────────────────────────────────────────────── */

/* What is in the search box. `state.q` and nothing else: the field is filled
 * from it once (fillFromUrl) and every request reads the state, so paging,
 * ticking and panning cannot lose the place a reader typed. */
function place() {
  return state.q;
}

/* A name the archive has never seen. The server says so itself (match
 * "none"), and it is a different sentence from "nothing there yet": one is
 * answered by typing something else, the other by waiting for the archive
 * to fill up. */
function placeUnknown() {
  return Boolean(answer && answer.place && answer.place.match === "none");
}

/* The places that were counted, for the caption: the names the server
 * resolved, not the text that was typed. One name is the place; two are
 * both named, because "2 locations in Springfield" with one Springfield
 * meant is a wrong answer; more than two are counted rather than listed. */
function placePhrase() {
  const found = (answer && answer.place) || null;
  const names = (found && found.names) || [];
  if (!names.length) return "";
  if (names.length === 1) return names[0];
  if (names.length === 2) return `${names[0]} and ${names[1]}`;
  return `${fmtInt(found.places)} places called “${place()}”`;
}

/* The place a sentence about NOTHING has to name - the typed text, which is
 * what the reader is looking at, and empty when no place is set so every
 * sentence below still reads as the sentence it was before the search box
 * existed. */
function placeClause() {
  const term = place();
  return term ? ` in “${term}”` : "";
}

/* ── The other three narrowings ──────────────────────────────────────── */

/* The entity axis. It is in the URL like everything else on this page, so a
 * reload and a shared link count the same thing. */
function entityTerm() {
  return getExtra("entity").trim();
}

function entityClause() {
  const who = entityTerm();
  if (!who) return "";
  // The axis is IN the sentence: "for “Delivery”" and "for “Delivery”
  // events" are two different counts, and a reader must not have to look at
  // the switch to know which one they are reading.
  return entityAxis === "type" ? ` for “${who}” events` : ` for “${who}”`;
}

/* A name no entity is called - or, on the type axis, a word no event type
 * is. The server resolves it through the same app/scope.py every other view
 * uses and says so; "the archive has no such entity" and "that entity has
 * no address" are two different answers. */
function entityUnknown() {
  return Boolean(answer && answer.resolved && answer.resolved.match === "none");
}

/* The sentence for a term the archive does not know, in the words of the
 * axis it was typed on. */
function entityUnknownText() {
  return entityAxis === "type"
    ? `No event in this project has the type “${entityTerm()}”.`
    : `Nothing in this project is called “${entityTerm()}”.`;
}

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

/* What the map is empty FOR, in the caption and in the list beside it -
 * causes a reader acts on differently. The two must not disagree with each
 * other: a caption saying "No coordinates in this part of the archive."
 * beside a list saying "No coordinates in this project yet." about a
 * project whose other 12 addresses are one tick away.
 *
 * The order is the order a reader can act in: a thing they typed that the
 * archive does not know beats a filter they set, which beats the state of
 * the archive itself. */
function emptyReason() {
  if (noneTicked()) return "none-ticked";
  if (placeUnknown()) return "no-such-place";
  if (entityUnknown()) return "no-such-entity";
  if (entityTerm()) return "entity";
  if (datesSet()) return "dates";
  if (visibleOnly.checked) return "viewport";
  if (offTypes.size) return "filtered";
  if (place()) return "place";
  return "empty";
}

/* Functions rather than strings because most of them now name something the
 * reader typed; without one they build exactly the sentences this page said
 * before it had a search box. */
const EMPTY_CAPTION = {
  "none-ticked": () => "No location type is ticked, so nothing is counted.",
  "no-such-place": () => `No place in this project is called “${place()}”. The suggestions come from the archive's own addresses.`,
  "no-such-entity": () => `${entityUnknownText()} Clear the entity field to count every address again.`,
  entity: () => `No coordinates${entityClause()}${placeClause()}${datesClause()}. Clear the entity field to count every address again.`,
  dates: () => `No addresses${placeClause()}${datesClause()}. Clear From and To to count every date again.`,
  viewport: () => `No addresses${placeClause()} in the part of the map you are looking at. Use “Fit to data”, or untick “Only the visible area”.`,
  filtered: () => `No addresses of the ticked location types${placeClause()}.`,
  place: () => `No coordinates for “${place()}” - the archive knows the place but not where it is.`,
  empty: () => "No coordinates in this project yet.",
};

const EMPTY_ROW = {
  "none-ticked": () => "No location type is ticked.",
  "no-such-place": () => `No place in this project is called “${place()}”.`,
  "no-such-entity": () => entityUnknownText(),
  entity: () => `No coordinates${entityClause()}${placeClause()}${datesClause()}.`,
  dates: () => `No addresses${placeClause()}${datesClause()}.`,
  viewport: () => `No addresses${placeClause()} in the part of the map you are looking at.`,
  filtered: () => `No addresses of the ticked location types${placeClause()}.`,
  place: () => `No coordinates for “${place()}”.`,
  empty: () => "No coordinates in this project yet.",
};

/* ── Loading ─────────────────────────────────────────────────────────── */

/* What the entity box became, for the caption: a bucket answers under its
 * own name and the caption has to say so, or a reader is looking at four
 * companies under the word they typed with nothing to tell them. */
function entityPhrase() {
  const found = (answer && answer.resolved) || null;
  if (!found || !entityTerm()) return "";
  const label = found.label || entityTerm();
  // WHICH AXIS PRODUCED THE COUNT. "42 locations for Delivery" and "42
  // locations for Delivery events" are answers to two questions, and the
  // caption is where a reader finds out which one they got.
  if (entityAxis === "type") {
    return found.match === "bucket"
      ? `“${label}” events (event type, ${fmtInt((found.values || []).length)} spellings merged)`
      : `“${label}” events (event type)`;
  }
  if (found.match === "bucket") {
    return `${label} and every member of that bucket`;
  }
  return label;
}

function captionText(data) {
  if (!data.cells) return EMPTY_CAPTION[emptyReason()]();
  const spots = `${fmtInt(data.cells)} ${data.cells === 1 ? "spot" : "spots"}`;
  const locations = `${fmtInt(data.locations)} ${data.locations === 1 ? "location" : "locations"}`;
  // The place goes into the count sentence rather than beside it: "5
  // locations in 3 squares" is a different fact from "5 locations in 3
  // squares in USA", and a reader who prints the page has only this line.
  // Every other narrowing that is in force is named for the same reason: a
  // count with a filter behind it and no word about the filter is a wrong
  // answer to the question the page appears to be answering.
  const where = placePhrase();
  const who = entityPhrase();
  return `${locations} in ${spots} of about 110 metres`
    + `${who ? ` for ${who}` : ""}${where ? ` in ${where}` : ""}${datesClause()}. `
    + `The busiest spot counts ${fmtInt(data.max)}.`;
}

/* The whole page, drawn for "nothing is ticked". The answer that is already
 * in hand keeps its checklist (that is what the reader has to tick again);
 * everything that describes a drawing is emptied, because there is none. */
function showNothingTicked() {
  if (map && layer) { map.removeLayer(layer); layer = null; }
  squares.clear();
  answer = { ...answer, points: [], top: [], cells: 0, locations: 0, max: 0, capped: false };
  captionBox.textContent = captionText(answer);
  renderScale();
  renderTypes();
  renderTop();
  setStatus("Tick a location type, or use “All types”.");
  announce(captionBox.textContent);
}

async function load() {
  // A new count is a new sentence: whatever the status line said before the
  // last picture was saved is no longer true of this one.
  statusBeforeImage = null;
  statusBeforeImageWarned = false;
  if (noneTicked()) { showNothingTicked(); return; }
  setStatus("Counting the archived addresses…");
  showRetry(null);
  let data;
  try {
    data = await api("/api/map/heat", {
      channel: "heat",
      params: {
        q: place(), bbox: bboxParam(), types: wantedTypes(),
        entity: entityTerm(), entity_axis: entityAxis,
        date_from: dateRange().from, date_to: dateRange().to,
      },
    });
  } catch (err) {
    if (isAbort(err)) return;
    const said = errorSentence("The grid could not be counted.", err);
    setStatus(said, true);
    showRetry(() => load());
    announce(said);
    return;
  }
  const first = answer === null;
  answer = data;
  if (first && offTypes.size) {
    // The first request cannot carry the filter: the checklist it applies to
    // arrives with the answer. A URL that already hides types therefore
    // costs one more request, once, on the first load.
    renderTypes();
    load();
    return;
  }
  captionBox.textContent = captionText(data);
  renderScale();
  renderTypes();
  renderTop();
  draw();
  // A new place is a new part of the world, so the map goes there - and only
  // then: a tick in the checklist or a wider square is the same part of the
  // world seen differently, and a map that re-fitted itself under a reader
  // who ticked a box would move the answer away from the question.
  if (fittedTo === null || data.q !== fittedTo) { fit(); fittedTo = data.q; }
  movingToPlace = false;
  if (placeUnknown()) {
    setStatus(`No place in this project is called “${place()}”. Pick one from the list, or clear the field to count everywhere.`, true);
  } else if (data.capped) {
    setStatus(`Only the ${fmtInt(data.limit)} busiest spots are counted. Zoom in and tick “Only the visible area” for the rest.`, true);
  } else if (entityUnknown()) {
    setStatus(`${entityUnknownText()} Pick one from the list, or clear the field to count every address.`, true);
  } else if (data.cells > MAX_POINTS) {
    setStatus(`${fmtInt(data.cells)} spots were counted and the ${fmtInt(MAX_POINTS)} busiest are painted. Zoom in and tick “Only the visible area” for the rest.`, true);
  } else {
    setStatus("");
  }
  announce(captionBox.textContent);
}

function bboxParam() {
  if (!map || !visibleOnly.checked || movingToPlace) return "";
  const bounds = map.getBounds();
  return [bounds.getSouth(), bounds.getWest(), bounds.getNorth(), bounds.getEast()]
    .map((n) => n.toFixed(4)).join(",");
}

/* THE TWO TYPEAHEADS ARE TOLD APART BY NAME, not by position: this form has
 * a place field and an entity field now, and `form.querySelector(".typeahead")`
 * would quietly answer with whichever came first in the markup. */
function wrapFor(name) {
  const hidden = form.querySelector(`input[type="hidden"][name="${name}"]`);
  return hidden ? hidden.closest(".typeahead") : null;
}

/* A field is filled from the URL ONCE, and with the placeholder rather
 * than the value - the typeahead contract: the box stays empty and takes a
 * new place without deleting the old one first, while still showing what is
 * set. The same four lines as map.js, for the same reason. */
function fillField(name, value) {
  const wrap = wrapFor(name);
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

function fillFromUrl(value) {
  fillField("q", value);
}

/* What is typed in a field right now, which beats what is set: a button is
 * the Enter key with a mouse and Enter counts the typed text ("one need not
 * be in the list to search for it", typeahead.js). typeahead.js writes into
 * the hidden input only when a suggestion or Enter confirms the text, so
 * reading the hidden input alone throws away what somebody typed and then
 * reached for the button with. */
function termFrom(name) {
  const wrap = wrapFor(name);
  if (!wrap) return "";
  const hidden = wrap.querySelector('input[type="hidden"]');
  const input = wrap.querySelector("input[data-typeahead]");
  const typed = input ? input.value.trim() : "";
  return typed || (hidden ? hidden.value.trim() : "");
}

/* A place was asked for: put it in the URL (so a reload and a shared link
 * show the same squares), show it in the field the way the typeahead does,
 * tell the request not to bound itself by a viewport the reader has not
 * moved yet, and count. */
/* `run` IS THE GESTURE, NOT THE VALUE. The term is set either way - the
 * field, the hidden input and the URL all say it from here on - and only an
 * order draws the map again. Enter and the Search button order; a click in a
 * suggestion list does not, because that click is somebody still choosing,
 * and every one of them was counting a million locations into spots. */
function searchPlace(value, run = true) {
  const term = (value || "").trim();
  setState({ q: term });
  fillFromUrl(term);
  if (!run) return;
  movingToPlace = true;
  load();
}

/* The entity axis. It goes into the URL beside the place and never instead
 * of it: both are true at once, which is the whole point of the second box. */
function searchEntity(value, run = true) {
  const term = (value || "").trim();
  setExtra("entity", term);
  fillField("entity", term);
  if (run) load();
}

/* ── The axis switch ─────────────────────────────────────────────────── */

/* THE SWITCH CHANGES THE QUESTION, NOT THE ANSWER IN THE BOX. Pressing
 * "Type" re-labels the entity field, points its suggestion list at the
 * event types and counts the same word again as a class. What is set is
 * kept: switching after an answer that was not what was meant is exactly
 * why the switch is there. The place field is not touched - a place is a
 * place on either axis. */
function applyAxisToField() {
  const spec = AXIS_FIELDS[entityAxis];
  const wrap = wrapFor("entity");
  if (!spec || !wrap) return;
  const [label, url, placeholder] = spec;
  const input = wrap.querySelector("input[data-typeahead]");
  const labelEl = wrap.querySelector(".field-label");
  if (labelEl) labelEl.textContent = label;
  if (input) {
    input.dataset.placeholder = placeholder;
    if (!wrap.classList.contains("is-set")) input.placeholder = placeholder;
    // Through the typeahead's own API: it holds the URL and a cache keyed
    // by what was typed, so writing the attribute alone would leave the
    // field labelled "Event type" and still offering entity names.
    if (input._typeahead && typeof input._typeahead.setSuggestUrl === "function") {
      input._typeahead.setSuggestUrl(url);
    } else {
      input.dataset.suggest = url;
    }
  }
  if (axisSwitch) {
    axisSwitch.querySelectorAll("[data-axis]").forEach((button) => {
      button.setAttribute("aria-pressed", button.dataset.axis === entityAxis ? "true" : "false");
    });
  }
  if (form) form.dataset.entityAxis = entityAxis;
}

function chooseAxis(next) {
  if (!next || next === entityAxis || !AXIS_FIELDS[next]) return;
  entityAxis = next;
  applyAxisToField();
  // "object" is the default, so it is not written into the URL.
  setExtra("entity_axis", entityAxis === "object" ? "" : entityAxis);
  announce(`Searching by ${AXIS_SUBJECT[entityAxis]}: ${AXIS_FIELDS[entityAxis][0]}`);
  load();
}

/* ── The picture as a file ───────────────────────────────────────────── */

/* The line of words that goes into the image: what the caption says, plus
 * the archive it was counted in. A wash of colour with no words is a
 * puzzle a week later. */
function imageCaption() {
  const who = [state.project, state.language].filter(Boolean).join(" - ");
  const said = (captionBox && captionBox.textContent) || "";
  return `Heatmap${who ? ` - ${who}` : ""}${said ? ` - ${said}` : ""}`;
}

/* WHAT THE STATUS LINE SAID ABOUT THE COUNT before a picture was saved.
 * It may already carry a fact a reader must not lose ("Only the 20,000
 * busiest squares are counted"), so the save's sentence is added to it
 * rather than written over it, and the count's own half is remembered here
 * so pressing the button twice does not stack two copies. A new count
 * clears it (load). */
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
      // The key goes into the picture: a field of colour whose shades are
      // counts is unreadable without the scale beside it.
      legend: scaleCard,
      caption: imageCaption(),
      credit: ATTRIBUTION_TEXT,
      view: "heatmap",
    });
    sayAfterSaving(saved.tiles
      ? `Saved ${saved.name}. The picture carries the key and the map credit.`
      : `Saved ${saved.name} WITHOUT the map tiles - they come from a host that does not allow it. `
        + "The heat field and the key are in the picture.", !saved.tiles);
  } catch (err) {
    sayAfterSaving(errorSentence("The picture could not be saved.", err), true);
  } finally {
    if (imageButton) imageButton.disabled = false;
  }
}

function wire() {
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    // Both boxes are read, because both are part of the question the button
    // asks. The entity goes into the URL first so the one request that
    // follows carries it.
    const who = termFrom("entity");
    setExtra("entity", who);
    fillField("entity", who);
    searchPlace(termFrom("q"));
  });
  // ENTER ASKS, A CLICK IN THE LIST DOES NOT. Both gestures arrive as this
  // one event and `via` is what tells them apart. The value is taken either
  // way - the reset rows included, which arrive with an empty value and
  // widen the count again - and only the order redraws.
  form.addEventListener("typeahead:choose", (e) => {
    const which = e.target && e.target.dataset ? e.target.dataset.target : "";
    const value = (e.detail && e.detail.value) || "";
    const run = Boolean(e.detail && e.detail.ask);
    if (which === "entity") searchEntity(value, run);
    else searchPlace(value, run);
  });
  if (axisSwitch) {
    axisSwitch.addEventListener("click", (e) => {
      const button = e.target.closest("[data-axis]");
      if (button) chooseAxis(button.dataset.axis);
    });
  }

  if (imageButton) imageButton.addEventListener("click", () => { saveImage(); });

  [fromField, toField].forEach((field) => {
    if (!field) return;
    field.addEventListener("change", () => {
      setExtra(field === fromField ? "from" : "to", field.value);
      load();
    });
  });
  sizeInput.addEventListener("input", () => {
    sizeValue.textContent = `${number(sizeInput, DEFAULT_RADIUS)} px`;
    setExtra("radius", sizeInput.value);
    resize();
  });
  fitButton.addEventListener("click", () => { fit(); announce("Moved to the archived addresses"); });
  visibleOnly.addEventListener("change", () => {
    setExtra("area", visibleOnly.checked ? "visible" : "");
    load();
  });
  typesAll.addEventListener("click", () => {
    offTypes = new Set();
    setExtra("hide", "");
    load();
    announce("Every location type is counted again");
  });
  // "No types" is not "all types": it counts nothing, and the map says so
  // rather than quietly answering the wider question (see noneTicked).
  if (typesNone) {
    typesNone.addEventListener("click", () => {
      const all = answer ? (answer.types || []).map((t) => t.name) : [];
      if (!all.length) return;
      offTypes = new Set(all);
      setExtra("hide", all.join(","));
      load();
      announce("No location type is counted");
    });
  }
  // The box narrows the LIST and nothing else, so it redraws the checklist
  // and makes no request.
  if (typesFind) {
    typesFind.addEventListener("input", () => {
      typeFilter = typesFind.value;
      renderTypes();
    });
  }
  if (retryButton) {
    retryButton.addEventListener("click", () => { if (retryTask) retryTask(); });
  }
  window.addEventListener("beforeprint", () => { if (map) map.invalidateSize(); });
}

function init() {
  if (!form || !mapBox) return;
  fillFromUrl(state.q);
  fillField("entity", getExtra("entity"));
  if (fromField) fromField.value = getExtra("from");
  if (toField) toField.value = getExtra("to");
  // A link that already names a place is a place search: the first request
  // must not be cut down to a world view the reader has not chosen.
  movingToPlace = Boolean(state.q);
  offTypes = new Set(getExtra("hide").split(",").map((t) => t.trim()).filter(Boolean));
  const size = parseInt(getExtra("radius"), 10);
  if (Number.isFinite(size)) sizeInput.value = String(size);
  sizeValue.textContent = `${number(sizeInput, DEFAULT_RADIUS)} px`;
  visibleOnly.checked = getExtra("area") === "visible";
  map = createMap();
  wire();
  load();
}

init();
