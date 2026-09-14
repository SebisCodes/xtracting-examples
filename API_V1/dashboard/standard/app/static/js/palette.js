/* ==========================================================================
 *  The three data scales, read from the stylesheet.
 *
 *  app.css defines them ONCE as custom properties (--valence-*,
 *  --relevance-*, --count-*, --no-data) with the measured contrast in the
 *  comments beside them. This module publishes the same values to
 *  JavaScript through getComputedStyle, so a chart, a legend, a pin or a
 *  pill colours itself from the stylesheet instead of carrying a second
 *  copy of the hexes - the second copy is what makes a dashboard where the
 *  same meaning is two colours on two pages.
 *
 *    valence(step)        -3 … +3 → the diverging red-to-green scale.
 *                         Ratings (`rating_values.float_value` IS the
 *                         step), sentiments, outlooks. Negative is red,
 *                         positive is green, neutral is the palest.
 *    relevance(level)     0 … 4 → the sequential violet. Importance, and
 *                         `bool_high_relevance` (false → 0, true → 4).
 *                         Never red or green: relevance is not good or bad.
 *    count(index, of)     the blue. One argument, or none, gives the single
 *                         series blue; an index and a total spread a count
 *                         that is split into ORDERED parts over the ramp,
 *                         lightest first.
 *    noData()             grey - Unset, unstated, no rows. Absence is not a
 *                         step of any scale, and grey means nothing else.
 *
 *    textOn(colour)       the text colour that reaches 4.5:1 on a filled
 *                         mark of that colour - white on the deep steps,
 *                         --text on the three palest ones. Nobody has to
 *                         remember which is which.
 *
 *    scale(name)          the whole ordered ramp, for a legend or a
 *                         gradient: "valence" (7), "relevance" (5),
 *                         "count" (6).
 *
 *  WHAT THIS MODULE DOES NOT DO: it does not decide WHICH scale a dataset
 *  is on. That is a fact about the data - a rating is a valence, a row
 *  count is a count - and it belongs with the dataset, not here.
 *
 *  The values are read on first use and cached: getComputedStyle is a
 *  layout-flushing call and a chart asks for a colour once per bar.
 * ========================================================================== */

const CACHE = new Map();

/* One read of one custom property. `fallback` is what a page without
 * app.css gets (a template test, a print preview in a stripped context):
 * a colour rather than an empty string, because an empty fill silently
 * paints a bar black. */
function token(name, fallback) {
  if (CACHE.has(name)) return CACHE.get(name);
  let value = "";
  try {
    value = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  } catch (e) { /* no document (a worker), no stylesheet - fall through */ }
  const out = value || fallback;
  CACHE.set(name, out);
  return out;
}

/* After a stylesheet swap or a theme change, if one is ever added. */
export function refresh() { CACHE.clear(); }

/* The fallbacks are the values in app.css. They are duplicated ONLY here,
 * where the duplication is visible and tests/unit/test_palette.py compares
 * the two lists character by character - so a value that is changed in the
 * stylesheet and not here fails a test rather than drifting. */
const FALLBACK = {
  "--valence-neg-3": "#630e0b",
  "--valence-neg-2": "#922018",
  "--valence-neg-1": "#ba412f",
  "--valence-0": "#9c874d",
  "--valence-pos-1": "#258442",
  "--valence-pos-2": "#197239",
  "--valence-pos-3": "#0d6134",
  "--relevance-0": "#9f7bb8",
  "--relevance-1": "#915bb9",
  "--relevance-2": "#843cb5",
  "--relevance-3": "#70259e",
  "--relevance-4": "#57137d",
  "--count-1": "#668db3",
  "--count-2": "#4575a9",
  "--count-3": "#336299",
  "--count-4": "#1d4f91",
  "--count-5": "#133f75",
  "--count-6": "#072f64",
  "--no-data": "#616670",
  "--text": "#1a1a1a",
  "--white": "#ffffff",
};

const VALENCE_TOKENS = [
  "--valence-neg-3", "--valence-neg-2", "--valence-neg-1", "--valence-0",
  "--valence-pos-1", "--valence-pos-2", "--valence-pos-3",
];
const RELEVANCE_TOKENS = ["--relevance-0", "--relevance-1", "--relevance-2", "--relevance-3", "--relevance-4"];
const COUNT_TOKENS = ["--count-1", "--count-2", "--count-3", "--count-4", "--count-5", "--count-6"];

const SCALES = {
  valence: VALENCE_TOKENS,
  relevance: RELEVANCE_TOKENS,
  count: COUNT_TOKENS,
};

/* The ordered steps of a scale, palest first for the sequential ones and
 * most negative first for the diverging one - the order a legend reads. */
export function scale(name) {
  const names = SCALES[name];
  if (!names) throw new Error(`no such scale: ${name}`);
  return names.map((n) => token(n, FALLBACK[n]));
}

function clampIndex(value, last) {
  const n = Math.round(Number(value));
  if (!Number.isFinite(n)) return null;
  return Math.min(last, Math.max(0, n));
}

/* -3 … +3. The argument is the NUMBER the archive carries, not a label:
 * `rating_values.float_value` is already the step, and a sentiment or an
 * outlook is mapped to a number by whoever knows the vocabulary. A value
 * that is not a number at all is absence, and absence is grey. */
export function valence(step) {
  const i = clampIndex(Number(step) + 3, 6);
  if (i === null) return noData();
  return token(VALENCE_TOKENS[i], FALLBACK[VALENCE_TOKENS[i]]);
}

/* 0 … 4, or a boolean for `bool_high_relevance` - true is the top step,
 * false the palest, because "not highly relevant" is still a reading. */
export function relevance(level) {
  if (typeof level === "boolean") return relevance(level ? 4 : 0);
  const i = clampIndex(level, 4);
  if (i === null) return noData();
  return token(RELEVANCE_TOKENS[i], FALLBACK[RELEVANCE_TOKENS[i]]);
}

/* The blue. `count()` is the single series; `count(i, n)` spreads n ordered
 * parts over the ramp, lightest first, using the whole ramp however many
 * parts there are. More parts than steps repeat the ramp - which is a
 * signal that the chart is stacking too much, not a colouring problem. */
export function count(index, of) {
  const ramp = scale("count");
  if (index === undefined || of === undefined || Number(of) <= 1) {
    return token("--count-4", FALLBACK["--count-4"]);
  }
  const parts = Math.max(2, Math.round(Number(of)));
  const i = Math.max(0, Math.round(Number(index)));
  if (parts <= ramp.length) {
    // Spread the parts over the ramp so two parts are its two ends and
    // six are every step - never four crowded into the light half.
    const step = (ramp.length - 1) / (parts - 1);
    return ramp[Math.round(i * step) % ramp.length];
  }
  return ramp[i % ramp.length];
}

export function noData() {
  return token("--no-data", FALLBACK["--no-data"]);
}

/* ── Text on a filled mark ─────────────────────────────────────────────── */

function channel(c) {
  const v = c / 255;
  return v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4;
}

/* #rgb, #rrggbb or rgb()/rgba() - getComputedStyle hands back whichever
 * the browser feels like. */
export function luminance(colour) {
  const s = String(colour).trim();
  let r; let g; let b;
  if (s.startsWith("#")) {
    let h = s.slice(1);
    if (h.length === 3) h = h.split("").map((c) => c + c).join("");
    if (h.length < 6) return 0;
    r = parseInt(h.slice(0, 2), 16);
    g = parseInt(h.slice(2, 4), 16);
    b = parseInt(h.slice(4, 6), 16);
  } else {
    const m = s.match(/-?[\d.]+/g);
    if (!m || m.length < 3) return 0;
    [r, g, b] = m.slice(0, 3).map(Number);
  }
  return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b);
}

export function contrastRatio(a, b) {
  const la = luminance(a);
  const lb = luminance(b);
  const hi = Math.max(la, lb);
  const lo = Math.min(la, lb);
  return (hi + 0.05) / (lo + 0.05);
}

/* The label INSIDE a bar, a pill or a pin. Whichever of the page's two text
 * colours contrasts more - and every colour in the three scales reaches
 * 4.5:1 with one of them, which is the test in tests/unit/test_palette.py. */
export function textOn(colour) {
  const dark = token("--text", FALLBACK["--text"]);
  const light = token("--white", FALLBACK["--white"]);
  return contrastRatio(colour, dark) >= contrastRatio(colour, light) ? dark : light;
}

/* Everything at once, for a caller that wants to hand a whole palette to a
 * chart library. Built fresh each call so nobody mutates the module's
 * cache through it. */
export function palette() {
  return {
    valence: scale("valence"),
    relevance: scale("relevance"),
    count: scale("count"),
    single: count(),
    noData: noData(),
  };
}

/* ── The heat ramp ──────────────────────────────────────────────────────── */
/*
 *  THE GRADIENT IS WRITTEN IN map.css AND NOWHERE ELSE. Five custom
 *  properties hold it, yellow to red, and this reads them into the shape
 *  leaflet.heat wants: `{0: yellow, 0.25: …, 1: red}`.
 *
 *  It lives here because TWO pictures are drawn with it - the Heatmap view
 *  and the field under the Locations tab of Diagrams - and the second one was
 *  drawn without it, in the plugin's own blue-to-red default. Two heat maps
 *  of one archive in two colour schemes is two answers to one question.
 *
 *  Read on every call rather than cached: the tokens follow the theme, and
 *  this is five property reads.
 */
export const HEAT_TOKENS = ["--heat-0", "--heat-1", "--heat-2", "--heat-3", "--heat-4"];

/* The alpha the faintest patch is stamped with, and therefore the bottom of
 * the key. Below about a third the palest wash is not perceivable. */
export const HEAT_MIN_ALPHA = 0.35;

export function heatStops() {
  const styles = window.getComputedStyle(document.documentElement);
  const stops = {};
  HEAT_TOKENS.forEach((token, i) => {
    const value = (styles.getPropertyValue(token) || "").trim();
    if (value) stops[i / (HEAT_TOKENS.length - 1)] = value;
  });
  return stops;
}
