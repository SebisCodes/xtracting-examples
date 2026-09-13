/* ==========================================================================
 *  The Query view.
 *
 *  The whole page is one sentence: "relevant texts WITHIN A PLACE, with a
 *  RADIUS, in a DATE RANGE". Three groups, in that order, and only the
 *  first is required.
 *
 *  THE ONE RULE THAT SHAPES EVERYTHING: a place name is a question, not an
 *  answer. Typing "Springfield" and getting a list of results is a lie when
 *  the archive holds two of them, so the page asks
 *  /api/query/places first and, when more than one candidate comes back,
 *  shows them as radio buttons and does not search until one is chosen. The
 *  Search button stays live and says why, rather than going grey and dead -
 *  a disabled button cannot be focused and cannot explain itself.
 *
 *  AND THE OTHER HALF OF THAT RULE: IT ASKS ONLY WHAT IT CANNOT ANSWER.
 *  A value PICKED FROM THE SUGGESTION LIST is not a word, it is a choice -
 *  the list holds the archive's own addresses - so it is resolved with
 *  `exact` and the chooser never appears on that path. Somebody picked
 *  "Houston, Texas, USA" and was asked which Houston they meant, then
 *  offered NRG Stadium, Rice University and NASA's Johnson Space Center:
 *  six places INSIDE Houston, a question with no answer. Nor does the
 *  chooser ever grow past ten rows: more than that is not a choice, so the
 *  places are searched together and the line under the field says how many.
 *  The rules and their reasoning are in app/places.py.
 *
 *  AND THE RULE HOLDS FOR A LINK. An address in the URL is the same word,
 *  not somebody's answer about it: a shared /query?address=Springfield used
 *  to search both Springfields at once under the label "Springfield" - five
 *  results from two countries, counted as one place. Every path into a
 *  search goes through resolveAddress(), and start() is one of them.
 *
 *  Where has two forms and never both at once (the API answers 400 for
 *  both, and for neither): an address, or a point with a distance. Choosing
 *  an address clears the coordinates; typing a coordinate clears the
 *  address. The line under the group always says which one is in force.
 *
 *  The mini-map is a picture of the answer. Everything on it is also in the
 *  list underneath it, so a person who cannot see the map, or whose tile
 *  server is unreachable, still has the places.
 * ========================================================================== */

import { api, isAbort, fmtDate, fmtInt, sentence, watchSearch } from "./api.js";
import { state, set as setState, setExtra, getExtra } from "./state.js";
import { announce } from "./a11y.js";
// The one dialog shell (js/dialog.js) - the drilldown has none of its own.
// So the enlarged map closes the way every
// popup in this product closes: by its Cancel button or by the X in the top
// right, and by nothing else.
import { openDialog } from "./dialog.js";
import { createMiniMap } from "./minimap.js";
import "./typeahead.js";
import "./chips.js";

const DEFAULT_RADIUS_KM = 25;

const form = document.getElementById("query-form");
/* Every request on these channels turns the button quiet and writes
 * "Searching…" beside it, without this file having to remember to
 * (static/js/api.js: watchSearch). */
watchSearch(form, "query-places", "query-search");
const addressHidden = document.querySelector('input[name="address"]');
const addressField = document.getElementById("ta-address");
const addressRoot = addressHidden ? addressHidden.closest(".typeahead") : null;
const choicesBox = document.getElementById("place-choices");
const whereSummary = document.getElementById("where-summary");
/* ── PLACE OR COORDINATES, one at a time ────────────────────────────────
 *
 * Not an address field with a disclosure under it: that puts both ways of
 * naming a place on the page at once - and the server refuses a request
 * that carries both (400: "a place or a point, not both"). The switch makes
 * the form say what the server means.
 *
 * The half that is not chosen is `hidden`: out of the page and out of the
 * tab order. Its VALUES are cleared with it, because a latitude left behind
 * a switched-away field is a value the reader cannot see and cannot undo -
 * and it is exactly what would make the request the server rejects. */
const whereSwitch = document.querySelector("[data-where-switch]");
const wherePanels = document.querySelectorAll("[data-where-field]");

function setWhereMode(mode, { clear = true } = {}) {
  const wanted = mode === "point" ? "point" : "place";
  wherePanels.forEach((panel) => { panel.hidden = panel.dataset.whereField !== wanted; });
  if (whereSwitch) {
    whereSwitch.querySelectorAll("[data-where]").forEach((button) => {
      button.setAttribute("aria-pressed", button.dataset.where === wanted ? "true" : "false");
    });
  }
  if (!clear) return;
  // Switching away is a decision, so the other half stops being part of the
  // question - and the map, the caption and the URL follow it.
  if (wanted === "point") {
    setAddressField("");
    hideChoices();
    where = { kind: "", address: "", note: "", match: "", lat: null, lng: null, km: currentKm() };
  } else {
    latField.value = "";
    lngField.value = "";
    where = { kind: "", address: "", note: "", match: "", lat: null, lng: null, km: currentKm() };
  }
  describeWhere();
  drawCircle();
}
const latField = document.getElementById("q-lat");
const lngField = document.getElementById("q-lng");
const kmNumber = document.getElementById("q-distance");
const kmRange = document.getElementById("q-distance-range");
const termsHidden = document.querySelector('input[name="terms"]');
const allTerms = document.getElementById("q-all-terms");
const fromField = document.getElementById("q-from");
const toField = document.getElementById("q-to");
const statusBox = document.getElementById("q-status");
/* WHICH KIND OF OBJECT COMES BACK. The walk to it is always the same -
 * locations, then the entities standing at them, then what hangs off those
 * entities - and this is the last step. One at a time: see the fieldset in
 * templates/query.html for why that is a radio group and not tick boxes. */
const kindGroup = document.getElementById("q-kind-group");

function objectKind() {
  const chosen = kindGroup && kindGroup.querySelector("input[name=object_kind]:checked");
  return chosen ? chosen.value : "source";
}

function setObjectKind(value) {
  if (!kindGroup) return;
  const button = kindGroup.querySelector(`input[name=object_kind][value="${value}"]`);
  if (button) button.checked = true;
}
const resultsBox = document.getElementById("results");
const resultsMeta = document.getElementById("results-meta");
const pager = document.getElementById("results-pager");
const pageInfo = document.getElementById("results-page-info");
const mapBox = document.getElementById("minimap");
const mapCaption = document.getElementById("minimap-caption");
const mapList = document.getElementById("minimap-places");
const enlargeButton = document.getElementById("minimap-enlarge");

/* What "where" is set to right now. `kind` is "", "place" or "point"; an
 * empty kind means the question has not been answered yet. `match` is set
 * only when the chosen row stands for SEVERAL places - "all 221 places
 * called United States" - and then it is the reading the server searches
 * them all with. */
let where = { kind: "", address: "", note: "", match: "", lat: null, lng: null, km: DEFAULT_RADIUS_KM };
let lastPlaces = [];
let miniMap = null;
let dialogMap = null;

function el(tag, className, text) {
  const e = document.createElement(tag);
  if (className) e.className = className;
  if (text !== undefined && text !== null) e.textContent = text;
  return e;
}

function withContext(href) {
  const url = new URL(href, window.location.origin);
  if (state.project) url.searchParams.set("project", state.project);
  if (state.language) url.searchParams.set("language", state.language);
  return url.pathname + url.search;
}

function setStatus(text, blocking = false) {
  statusBox.textContent = text || "";
  statusBox.classList.toggle("is-blocking", Boolean(text) && blocking);
}

/* A failure with no panel of its own to fail into: the place lookup. It is
 * reported twice - the line beside the Search button, because that is where
 * the eye is a moment after pressing it, and a toast for whoever has
 * scrolled away - and BOTH have to be the same two sentences.
 *
 * The toast is api.js's, and it is already those two sentences: api.js
 * finishes the server's `{error, hint}` fragments with the same sentence()
 * this line imports (see the head of api.js). While that finishing lived
 * here instead, the page had to keep api.js quiet and raise its own toast,
 * or the corner said "the date range ends before it starts" beside a line
 * reading "The date range ends before it starts." - one failure, two
 * registers, one screen. So this only writes the line by the button. */
function reportProblem(err, fallback) {
  const what = sentence(err.message || fallback);
  const hint = sentence(err.hint);
  setStatus([what, hint].filter(Boolean).join(" "), true);
}

/* The same two halves as two blocks, for the Results panel: WHAT HAPPENED in
 * one line, WHAT TO DO under it, the way the toast lays them out. */
function errorNotice(what, err) {
  const box = el("div", "notice notice--error");
  const body = el("div", "notice-body");
  const line = el("p", "notice-what");
  line.appendChild(el("strong", null, what));
  const said = sentence(err && err.message);
  if (said) line.appendChild(document.createTextNode(" " + said));
  body.appendChild(line);
  const hint = sentence(err && err.hint);
  if (hint) body.appendChild(el("p", "notice-hint", hint));
  box.appendChild(body);
  return box;
}

/* ── The terms ───────────────────────────────────────────────────────── */

function terms() {
  if (!termsHidden) return [];
  try {
    const parsed = JSON.parse(termsHidden.value || "[]");
    return Array.isArray(parsed) ? parsed.map(String).filter(Boolean) : [];
  } catch (e) {
    return [];
  }
}

function setTerms(list) {
  const chips = termsHidden ? termsHidden.closest("[data-chips]") : null;
  if (chips && chips._chips) chips._chips.values = list;
  else if (termsHidden) termsHidden.value = JSON.stringify(list);
}

/* ── Where ───────────────────────────────────────────────────────────── */

function setAddressField(value) {
  if (!addressHidden || !addressField) return;
  addressHidden.value = value || "";
  addressField.value = "";
  addressField.placeholder = value || addressField.dataset.placeholder || "";
  if (addressRoot) addressRoot.classList.toggle("is-set", Boolean(value));
}

function describeWhere() {
  whereSummary.textContent = "";
  // While "which Springfield?" is on the screen, that question is already
  // asked twice - by the radio list itself and by the line next to Search.
  // A third "No place chosen yet." in a third place is the same condition
  // announced again, so this line steps aside until it has something to say.
  whereSummary.hidden = where.kind === "" && !choicesBox.hidden;
  if (whereSummary.hidden) return;
  if (where.kind === "place") {
    whereSummary.appendChild(document.createTextNode("Searching in "));
    whereSummary.appendChild(el("strong", null, where.address));
    // A country is not one address but every address in it, and that is
    // worth saying: "USA" alone reads like a place somebody could point at.
    if (where.note) whereSummary.appendChild(el("span", "muted", ` - ${where.note}`));
  } else if (where.kind === "point") {
    whereSummary.appendChild(document.createTextNode("Searching within "));
    whereSummary.appendChild(el("strong", null, `${where.km} km`));
    whereSummary.appendChild(el("span", "muted", ` of ${where.lat}, ${where.lng}`));
  } else {
    whereSummary.appendChild(el("span", "muted", "No place chosen yet."));
  }
}

/* The chosen place. The list of candidates STAYS on the screen with the
 * chosen one ticked: somebody who picked the wrong Springfield switches with
 * one click instead of typing the name again. */
function usePlace(address, note = "", match = "") {
  where = { kind: "place", address, note, match, lat: null, lng: null, km: where.km };
  latField.value = "";
  lngField.value = "";
  setAddressField(address);
  describeWhere();
  setExtra("address", address);
  setExtra("lat", "");
  setExtra("lng", "");
}

function usePoint() {
  const lat = parseFloat(latField.value);
  const lng = parseFloat(lngField.value);
  if (!Number.isFinite(lat) || !Number.isFinite(lng)) {
    if (where.kind === "point") {
      where = { kind: "", address: "", note: "", match: "", lat: null, lng: null, km: where.km };
      describeWhere();
    }
    return;
  }
  where = { kind: "point", address: "", note: "", match: "", lat, lng, km: currentKm() };
  setAddressField("");
  hideChoices();
  describeWhere();
  setExtra("address", "");
  setExtra("lat", String(lat));
  setExtra("lng", String(lng));
  drawCircle();
}

function currentKm() {
  const km = parseFloat(kmNumber.value);
  return Number.isFinite(km) && km > 0 ? km : DEFAULT_RADIUS_KM;
}

function hideChoices() {
  choicesBox.hidden = true;
  choicesBox.textContent = "";
  describeWhere();   // the "where" line speaks again once the question is gone
}

/* The disambiguation list. Radios, not a select: there are two or three of
 * them, each needs a count next to it, and a select would hide exactly the
 * information that makes the choice possible. */
function showChoices(candidates, match, term, { focus = true } = {}) {
  choicesBox.textContent = "";
  const question = el("p", "field-label", `Which ${term} do you mean?`);
  question.id = "place-choices-question";
  // The visible question IS the group's name; a static aria-label next to
  // it would be a second, staler name for the same thing.
  choicesBox.removeAttribute("aria-label");
  choicesBox.setAttribute("aria-labelledby", question.id);
  choicesBox.appendChild(question);

  // "All of them" is the last row, and it is a real answer rather than an
  // escape: two towns of one name are often both meant ("what does the
  // archive say about Springfield"), and without it the only way to ask
  // that is to run the search twice and add the numbers up.
  const everyone = {
    label: `All ${candidates.length} of them`,
    address: term,
    search_match: match,
    covers: candidates.length,
    whole: false,
    locations: candidates.reduce((n, c) => n + (c.locations || 0), 0),
    entities: candidates.reduce((n, c) => n + (c.entities || 0), 0),
  };

  [...candidates, everyone].forEach((c, i) => {
    const id = `place-choice-${i}`;
    const label = el("label", "radio");
    label.htmlFor = id;
    const radio = document.createElement("input");
    radio.type = "radio";
    radio.name = "place-choice";
    radio.id = id;
    radio.value = String(i);
    label.appendChild(radio);
    label.appendChild(document.createTextNode(" " + (c.label || "")));
    const count = el("span", "place-count",
      ` - ${c.locations} location${c.locations === 1 ? "" : "s"}, ${c.entities} entit${c.entities === 1 ? "y" : "ies"}`);
    label.appendChild(count);
    radio.addEventListener("change", () => {
      usePlace(addressOf(c, match), noteFor(c, match), matchOf(c));
      setStatus("");
      search({ resetPage: true });
    });
    choicesBox.appendChild(label);
  });
  choicesBox.hidden = false;
  describeWhere();
  // Focus moves to the question when somebody just pressed Search or picked
  // a suggestion - they asked, and the answer is a question back. It does
  // NOT move when the page merely opened on a link: nobody pressed anything,
  // and taking the focus off the top of a page somebody is still reading is
  // the disorienting kind of help. The status line and the live region say
  // it instead.
  const first = choicesBox.querySelector("input");
  if (focus && first) first.focus();
}

/* What the chosen candidate covers, in words.
 *
 * Three different things can stand in one row of the chooser, and a person
 * has to be able to tell them apart: ONE place, an AREA with places in it
 * (a country, or an address the matches all sit inside), or SEVERAL places
 * that happen to share a name and are searched together. */
function noteFor(candidate, match) {
  const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`;
  if (candidate.whole && match === "country") {
    return `all ${plural(candidate.addresses, "address", "addresses")} in the country`;
  }
  if (candidate.whole) {
    return `all ${plural(candidate.covers, "address", "addresses")} in this area`;
  }
  if (candidate.covers > 1) {
    // A name several places carry, or - when the term was a fragment of an
    // address rather than a place - the addresses it turns up in. Two
    // different facts, so two sentences: "all 12 places with this name" is
    // wrong for "a", which is in 65,355 addresses and is nobody's name.
    return match === "address"
      ? `all ${plural(candidate.covers, "address", "addresses")} containing it`
      : `all ${plural(candidate.covers, "place", "places")} with this name`;
  }
  return plural(candidate.locations, "location", "locations");
}


/* The line the API is asked with. The server names it: `address` is the
 * whole address of an address candidate, the parts of a city candidate, the
 * country of a country candidate, and the term itself where the row stands
 * for several places. The server splits whatever it gets with the archive's
 * own address rule. */
function addressOf(candidate, match) {
  if (candidate && candidate.address) return candidate.address;
  if (match === "address") return (candidate && candidate.city) || "";
  return [candidate.city, candidate.region, candidate.country].filter(Boolean).join(", ");
}

/* How to search this row: one place by its own address, or SEVERAL places
 * by the reading the name was given ("every city called Springfield"). */
function matchOf(candidate) {
  return (candidate && candidate.search_match) || "";
}

/* Ask what a name means. Returns true when the search may go ahead.
 *
 * EVERY path into a search comes through here - typing and pressing Search,
 * picking a suggestion, and opening a link that carries an address. The
 * third must not skip it (see start()): a shared link to an ambiguous place
 * would otherwise land on the results for whichever candidate the archive
 * happens to sort first.
 *
 * `exact` says the term came OUT OF THE SUGGESTION LIST. Then it names a
 * place already and the server answers with that place: the chooser is not
 * a step on this path at all. Typing the same letters and pressing Search
 * is a different act and still asks, because typed text is a word and a
 * suggestion is a choice. */
async function resolveAddress(term, { focus = true, exact = false } = {}) {
  const data = await api("/api/query/places", {
    channel: "query-places", params: exact ? { q: term, exact: "1" } : { q: term },
  });
  const candidates = data.candidates || [];
  if (!candidates.length) {
    hideChoices();
    setStatus(`No place in this project is called “${term}”. The suggestions come from the archive's own addresses.`, true);
    return false;
  }
  if (candidates.length === 1) {
    // One candidate is not always one PLACE: it is also "all 221 places
    // called United States" and "all 7 addresses in this area", both of
    // which are answers rather than questions. The note under the field
    // says which of the three it is, and covers/search_match carry it into
    // the search.
    hideChoices();
    const only = candidates[0];
    usePlace(addressOf(only, data.match), noteFor(only, data.match), matchOf(only));
    if (only.covers > 1 && !only.whole) {
      // Said out loud, because it is the one case where the page decided
      // something on the person's behalf. It is not hidden state: the line
      // under the field carries it for as long as the search stands, in
      // these same words (noteFor).
      announce(data.match === "address"
        ? `${only.covers} addresses contain ${term}. All of them are searched.`
        : `${only.covers} places are called ${term}. All of them are searched.`);
    }
    return true;
  }
  showChoices(candidates, data.match, term, { focus });
  setStatus(`${candidates.length} places are called “${term}”. Choose one.`, true);
  announce(`${candidates.length} places are called ${term}. Choose one.`);
  return false;
}

/* ── The map ─────────────────────────────────────────────────────────── */

function ensureMap() {
  // `wheel: "always"` - the wheel zooms as soon as the pointer is over the
  // map, on this one as on the big ones. minimap.js keeps a "click" mode for
  // a map that must ask before taking the wheel, and says why nothing in the
  // product uses it: one gesture on every map beats two.
  if (!miniMap) miniMap = createMiniMap(mapBox, { center: [20, 0], zoom: 1, wheel: "always" });
  return miniMap;
}

function drawCircle() {
  const map = ensureMap();
  if (where.kind === "point") {
    map.setCircle(where.lat, where.lng, where.km);
    map.fit();
  } else {
    map.setCircle(null, null, 0);
  }
}

/* Where the search is looking, as a phrase that follows a count. */
function scopePhrase() {
  if (where.kind === "place") return ` in ${where.address}`;
  if (where.kind === "point") return ` within ${where.km} km of ${where.lat}, ${where.lng}`;
  return "";
}

/* THE MAP IS THE QUESTION, NOT THE ANSWER.
 *
 * `places` in the search response is every address of the PLACE SCOPE - the
 * markers query runs on the place predicate alone and never sees the words -
 * so its count has nothing to do with how many texts matched. Captioned as
 * "3 addresses with coordinates." beside a Results panel reading "no
 * results", the page answered one question two ways on one screen, at the
 * same weight, and the map's number was the one that looked like a total.
 *
 * So the caption says what those pins ARE: the ground the search covered.
 * And when nothing matched it says that too, in the same breath - which is
 * the sentence that stops the two panels contradicting each other.
 *
 * `found` is the result total of the render this map belongs to, or null
 * when no search has run (the form was just cleared). */
function drawPlaces(list, found = null) {
  lastPlaces = list || [];
  const map = ensureMap();
  map.setMarkers(lastPlaces);
  drawCircle();
  map.fit();

  if (lastPlaces.length) {
    const n = lastPlaces.length;
    const covered =
      `${n} address${n === 1 ? "" : "es"}${scopePhrase()} ${n === 1 ? "is" : "are"} inside this search.`;
    mapCaption.textContent = found === 0
      ? `${covered} None of them has a text that matched.`
      : covered;
  } else if (where.kind) {
    mapCaption.textContent = "No coordinates for this search - the archive has none for these places.";
  } else {
    mapCaption.textContent = "The map fills once a search has run.";
  }

  mapList.textContent = "";
  lastPlaces.slice(0, 50).forEach((p) => {
    const li = el("li");
    li.appendChild(el("span", "place-entity", p.entity || ""));
    if (p.address) {
      li.appendChild(document.createTextNode(" - "));
      li.appendChild(el("span", "place-address", p.address));
    }
    mapList.appendChild(li);
  });
  if (lastPlaces.length > 50) {
    mapList.appendChild(el("li", "muted", `and ${lastPlaces.length - 50} more`));
  }
}

/* Enlarge: the same map, in a native <dialog>. A map in a box that was
 * hidden a moment ago is 0 x 0 as far as Leaflet knows, so invalidate()
 * runs after the dialog has been laid out - twice, because the first frame
 * is sometimes still the old size. */
function enlarge() {
  const box = el("div", "map-dialog");
  const big = el("div", "minimap");
  big.setAttribute("role", "region");
  big.setAttribute("aria-label", "Map of the places this search covers, enlarged");
  const caption = el("p", "minimap-caption", mapCaption.textContent);
  box.appendChild(big);
  box.appendChild(caption);
  // The list of places comes into the dialog too. It is the map's text
  // alternative - the real content, of which the map is only the picture -
  // and while a modal dialog is open the copy out in the page is behind the
  // backdrop and unreachable. Carrying only the caption across left the
  // enlarged map as the ONE way to those addresses.
  if (mapList.children.length) {
    const places = mapList.cloneNode(true);
    places.removeAttribute("id");   // one id, one element; the original keeps it
    places.setAttribute("aria-label", "The places on this map");
    box.appendChild(places);
  }

  openDialog({
    title: "Where you are searching",
    subtitle: where.kind ? whereSummary.textContent : "",
    wide: true,
    content: box,
    onClose() {
      if (dialogMap) { dialogMap.destroy(); dialogMap = null; }
    },
  });

  // The enlarged map fills the window, so there is nothing to scroll past it
  // and the wheel has no other job: it zooms straight away.
  dialogMap = createMiniMap(big, { center: [20, 0], zoom: 1, wheel: "always" });
  dialogMap.setMarkers(lastPlaces);
  if (where.kind === "point") dialogMap.setCircle(where.lat, where.lng, where.km);
  // Both of these land AFTER the dialog may already have been closed - a
  // dialog that is dismissed within the frame leaves `dialogMap` null and
  // `dialogMap.invalidate()` then throws into the console, where nothing in
  // the page catches it. Neither call has anything to do once the map is
  // gone, so both ask first.
  window.requestAnimationFrame(() => {
    if (!dialogMap) return;
    dialogMap.invalidate();
    dialogMap.fit();
    window.setTimeout(() => { if (dialogMap) { dialogMap.invalidate(); dialogMap.fit(); } }, 120);
  });
}

/* ── Results ─────────────────────────────────────────────────────────── */

function escapeRe(text) {
  return String(text).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/* The matched words, marked in the text. Built as text nodes and <mark>
 * elements, never as HTML: a summary containing "<" is a summary, not
 * markup. */
function highlight(text, words) {
  const frag = document.createDocumentFragment();
  const clean = String(text || "");
  const useful = (words || []).filter(Boolean);
  if (!useful.length || !clean) {
    frag.appendChild(document.createTextNode(clean));
    return frag;
  }
  const re = new RegExp("(" + useful.map(escapeRe).join("|") + ")", "gi");
  let last = 0;
  let m = re.exec(clean);
  while (m !== null) {
    if (m.index > last) frag.appendChild(document.createTextNode(clean.slice(last, m.index)));
    const mark = document.createElement("mark");
    mark.textContent = m[0];
    frag.appendChild(mark);
    last = m.index + m[0].length;
    if (m[0].length === 0) re.lastIndex += 1;
    m = re.exec(clean);
  }
  if (last < clean.length) frag.appendChild(document.createTextNode(clean.slice(last)));
  return frag;
}

/* WHERE THE HEADINGS CAME FROM, said once.
 *
 * A name worked out from an address must never read as a quotation, so the
 * list says so - once, because it is a fact about the list and one line per
 * card would be twenty copies of the same caveat. */
function noteDerivedNames(items) {
  const note = document.getElementById("results-derived-note");
  if (!note) return;
  const any = (items || []).some((item) => item && item.derived);
  note.textContent = any
    ? "Document names in this list were read from their web address."
    : "";
  note.hidden = !any;
}


function resultItem(item, words) {
  const li = el("li", "result");

  // THE HEADING IS NEVER AN IDENTIFIER. `sources.text_name` is
  // `src_1127664` or an md5 in every row of this archive, so the server
  // heads a document with its domain and the slug of its own address
  // instead (app/source_names.py) and marks that `derived`.
  const title = el("h4", "result-title");
  // Only a real web address becomes a link: an archive can hold sources
  // like `src:city-archive-foia-2006`, and an anchor whose scheme no
  // browser knows is a link that looks live and does nothing.
  if (/^https?:\/\//i.test(String(item.uri || ""))) {
    const a = el("a");
    a.href = item.uri;
    a.rel = "noopener noreferrer";
    a.target = "_blank";
    a.appendChild(highlight(item.title, words));
    title.appendChild(a);
  } else {
    title.appendChild(highlight(item.title, words));
  }
  li.appendChild(title);

  /* THE SUMMARY IS CUT, AND THE REST IS ONE PRESS AWAY.
   *
   * These are whole extraction summaries - several hundred characters is
   * ordinary and some run to a thousand - and twenty of them down a page
   * turn a list of results into a wall the reader has to scroll past to
   * find the next heading. Cut to three lines, with the number of results
   * kept visible; "Show more" reveals the rest of THAT one and nothing
   * else.
   *
   * Cut by LINES, not characters: the CSS clamp knows the width the text
   * actually got, and a character count guesses it. The button is only
   * added when there is something behind it - measured after the clamp, so
   * a summary that already fits carries no control at all.
   *
   * The full text stays in the DOM either way, so find-in-page still finds
   * it and the matched words stay highlighted inside it. */
  if (item.body) {
    const body = el("p", "result-body is-clamped");
    body.appendChild(highlight(item.body, words));
    li.appendChild(body);

    const more = el("button", "button button--quiet result-more", "Show more");
    more.type = "button";
    more.hidden = true;
    more.setAttribute("aria-expanded", "false");
    more.addEventListener("click", () => {
      const open = body.classList.toggle("is-clamped");
      more.textContent = open ? "Show more" : "Show less";
      more.setAttribute("aria-expanded", open ? "false" : "true");
    });
    li.appendChild(more);
    // After layout: scrollHeight against clientHeight is the only honest
    // test of "is anything hidden", and it needs the element to be in the
    // page with its real width.
    requestAnimationFrame(() => {
      more.hidden = body.scrollHeight <= body.clientHeight + 1;
    });
  }

  const meta = el("p", "result-meta");
  if (item.about) {
    const span = el("span");
    span.appendChild(document.createTextNode("About: "));
    item.about.split(", ").forEach((name) => {
      const chip = el("a", "chip", name);
      chip.href = withContext(`/diagrams/entity?q=${encodeURIComponent(name)}`);
      span.appendChild(chip);
    });
    meta.appendChild(span);
  }
  if (item.type) meta.appendChild(el("span", null, item.type));
  if (item.date) {
    const time = el("time", null, fmtDate(item.date, { time: false }));
    time.dateTime = item.date;
    meta.appendChild(time);
  }
  if (item.matched_terms && item.matched_terms.length) {
    meta.appendChild(el("span", null, `Matched: ${item.matched_terms.join(", ")}`));
  }
  li.appendChild(meta);

  /* THE WHOLE ROW OPENS WHAT IS BEHIND IT.
   *
   * A result card carries a heading, a cut summary and three facts; the
   * archive holds a good deal more about the same row - what the entity IS,
   * what the document said, where it came from. That belongs one press away
   * and not in two hundred cards: see routers/api_query.py: /detail for why
   * it is its own small request.
   *
   * A click on a link inside the card is that link's business - the heading
   * opens the document, a chip opens the entity's diagrams - so those are
   * left alone. Everything else opens the popup. */
  li.addEventListener("click", (event) => {
    if (event.target.closest("a, button")) return;
    openDetail(item);
  });
  li.tabIndex = 0;
  li.setAttribute("role", "button");
  li.setAttribute("aria-label", `More about ${item.title}`);
  li.addEventListener("keydown", (event) => {
    if (event.key !== "Enter" && event.key !== " ") return;
    if (event.target !== li) return;
    event.preventDefault();
    openDetail(item);
  });
  return li;
}

/* ── What is behind a row ─────────────────────────────────────────────── */

/* A timestamp as the archive stores it - 2026-09-08T18:19:27.653093+00:00 -
 * is a string nobody reads. The rest of the dashboard prints dates through
 * fmtDate and so does this. */
const ISO = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/;

function factList(facts) {
  const list = el("dl", "detail-facts");
  Object.entries(facts || {}).forEach(([label, value]) => {
    list.appendChild(el("dt", null, label.charAt(0).toUpperCase() + label.slice(1)));
    const text = ISO.test(String(value)) ? fmtDate(String(value), { time: true }) : String(value);
    list.appendChild(el("dd", null, text));
  });
  return list;
}

function detailSection(heading, node) {
  const section = el("section", "detail-block");
  section.appendChild(el("h3", null, heading));
  section.appendChild(node);
  return section;
}

async function openDetail(item) {
  // THE ID IS SPLIT AT THE LAST COLON. A task id has colons of its own -
  // `_preseed:alpha:1` - so splitting at the first one asks the archive
  // about a task called "_preseed" and is told, correctly, that there is no
  // such row.
  const id = String(item.id || "");
  const cut = id.lastIndexOf(":");
  if (cut < 0) return;
  const body = el("div", "detail-body");
  body.appendChild(el("p", "loading", "Reading the archive…"));
  const handle = openDialog({
    title: item.title || "This row",
    subtitle: item.type || "",
    wide: true,
    content: body,
    cancel: "Close",
  });
  try {
    const data = await api("/api/query/detail", {
      channel: "query-detail",
      method: "POST",
      body: { kind: item.kind, task_id: id.slice(0, cut), row_id: Number(id.slice(cut + 1)) },
    });
    body.textContent = "";
    if (Object.keys(data.row || {}).length) {
      body.appendChild(detailSection(data.label || "This row", factList(data.row)));
    }
    (data.entities || []).forEach((entity) => {
      const box = el("div");
      box.appendChild(factList(entity.detail));
      const links = el("p", "detail-links");
      [["Diagrams", `/diagrams/entity?q=${encodeURIComponent(entity.name)}`],
       ["Graph", `/graph?q=${encodeURIComponent(entity.name)}`],
       ["Map", `/map?q=${encodeURIComponent(entity.name)}`]].forEach(([label, href]) => {
        const a = el("a", "chip", label);
        a.href = withContext(href);
        a.target = "_blank";
        a.rel = "noopener noreferrer";
        links.appendChild(a);
      });
      box.appendChild(links);
      const named = entity.type ? `${entity.name} (${entity.type})` : entity.name;
      body.appendChild(detailSection(named || "Entity", box));
    });
    if (data.source) {
      const box = el("div");
      if (data.source.uri && /^https?:\/\//i.test(data.source.uri)) {
        const p = el("p", "detail-links");
        const a = el("a", "chip", "Open the document");
        a.href = data.source.uri;
        a.target = "_blank";
        a.rel = "noopener noreferrer";
        p.appendChild(a);
        box.appendChild(p);
      }
      box.appendChild(factList(data.source.detail));
      body.appendChild(detailSection(data.source.heading || "Document", box));
    }
    if (!body.childNodes.length) {
      // "holds" would trip the vocabulary guard (tests/unit/
      // test_vocabulary_guard.py): "hold" is one of the words this product
      // may never say, because it is advice in a financial reading.
      body.appendChild(el("p", "empty", "There is nothing more about this row."));
    }
  } catch (err) {
    if (isAbort(err)) return;
    body.textContent = "";
    body.appendChild(errorNotice("Could not read that row.", err));
  }
  return handle;
}

/* Both buttons say what they can do, and the pager is only there when it
 * can act at all - except on a page past the last one, where Previous is the
 * way back and hiding it would strand the reader. */
/* THE PAGER COUNTS WHAT IS IN HAND.
 *
 * Not the archive's answer: that is only right while the browser holds one
 * page of it and draws all of it. Neither is so: the rows come in blocks of
 * two hundred, the reader chooses how many to draw at once, and
 * ticking a subject out of the About list hides rows without asking the
 * archive anything. "Page 4 of 32" would then be a number nothing on the
 * screen agrees with.
 *
 * So it says both: where the reader is inside the loaded rows, and how much
 * there is behind them. Previous and Next step inside the block and, at
 * either end of it, fetch the block next door - that is the one press in
 * eight that costs a query.
 */
function renderPager(data, at = null) {
  const page = at === null ? blockPage : at;
  const pages = blockPages(data);
  const kept = keptRows(data).length;
  const loaded = ((data.groups && data.groups[0] && data.groups[0].items) || []).length;
  const total = Number(data.total) || 0;
  const first = Number(data.block_first) || 0;
  const more = total > loaded;

  pager.hidden = pages <= 1 && !more && page <= 0;
  if (pager.hidden) {
    pageInfo.textContent = "";
    return;
  }
  // PAST THE END IS SAID IN THE EVENTS VIEW'S WORDS, because it is the same
  // state and a reader who learnt one view should not have to learn the other
  // (static/js/events.js has the same sentence). "Page 1 of 1" over a panel
  // that says "that page is past the last one" is two answers to one question,
  // and it was what this line said: `Math.min` clamped the number rather than
  // naming the state.
  //
  // The test is the same one the panel uses - no rows on this page while the
  // answer has some - and not `page + 1 > pages`: the block a link past the
  // end opens on holds nothing, so its page count is 1 and its page is 0, and
  // an arithmetic test would call that an ordinary first page.
  const here = sliceFor(data, page);
  const pastTheEnd = total > 0 && !(here && here.length);
  const where = pastTheEnd
    ? `Past the last page - ${fmtInt(pages)} page${pages === 1 ? "" : "s"}, `
      + `${fmtInt(total)} result${total === 1 ? "" : "s"}`
    : `Page ${Math.min(page + 1, pages)} of ${pages}`;
  // What the pages are OF, said only when it is not the whole answer: on a
  // small answer "8 results" twice over is noise.
  const parts = [where];
  // NOTHING ELSE WHEN THERE IS NOTHING ON THE PAGE. The parts below describe
  // the rows that are on screen, and past the end there are none: "rows
  // 201-200 of 1" is what the arithmetic produces, and it is worse than
  // saying nothing.
  if (!pastTheEnd) {
    if (kept < loaded) parts.push(`${fmtInt(kept)} of ${fmtInt(loaded)} loaded shown`);
    if (more) {
      parts.push(`rows ${fmtInt(first + 1)}-${fmtInt(first + loaded)} of ${fmtInt(total)}`);
    } else if (kept < loaded) {
      parts.push(`${fmtInt(total)} found`);
    }
  }
  pageInfo.textContent = parts.join(" \u00b7 ");

  // The ends of the block are where a press costs a query: at the far end
  // there is a block after this one, at the near end a block before it.
  pager.querySelector("[data-page-prev]").disabled = page <= 0 && first <= 0;
  pager.querySelector("[data-page-next]").disabled = page + 1 >= pages
    && first + loaded >= total;
}

/* Nothing found, and WHY it might be nothing.
 *
 * One fixed sentence offered all three remedies at once - "Remove a word,
 * widen the date range, or try a larger distance" - to everybody, and two
 * thirds of it was always about something the person had not set: a search
 * of a named place has no distance to enlarge, and a search with no dates
 * has no range to widen. An empty state that lists filters that are not in
 * force reads as a list of things one has done wrong.
 *
 * So the sentence is built from what the form is actually holding, and each
 * clause names a control that is on the screen with something in it. */
function joinRemedies(list) {
  if (list.length <= 1) return list[0] || "";
  return `${list.slice(0, -1).join(", ")}, or ${list[list.length - 1]}`;
}

function emptyLine(words) {
  const dated = Boolean(fromField.value || toField.value);
  const remedies = [];
  // "use another word" rather than "try another word": the distance clause
  // below already starts with "try", and "Try another word, or try a larger
  // distance" reads like a stutter.
  if (words.length) remedies.push(words.length > 1 ? "remove a word" : "use another word");
  if (words.length > 1 && allTerms.checked) remedies.push("untick “Every word must appear”");
  if (dated) remedies.push("widen the date range");
  if (where.kind === "point") remedies.push("try a larger distance");

  // One word is not "those words": the remedy clause below already tells
  // the two apart, and the sentence in front of it has to agree with it.
  const what = words.length
    ? words.length > 1
      ? "Nothing here contains those words."
      : "Nothing here contains that word."
    : remedies.length
      ? "Nothing is archived for this search."
      : "Nothing is archived for this place yet.";
  return [what, sentence(joinRemedies(remedies))].filter(Boolean).join(" ");
}

/* THE BLOCK THIS BROWSER IS HOLDING.
 *
 * One request brings two hundred rows (routers/api_query.py: BLOCK_SIZE) and
 * the pager moves inside them: seven of every eight presses are a re-draw
 * and not a query. The eighth - the one that steps out of the block - asks
 * the archive for the next two hundred.
 *
 * Only twenty-five are drawn at a time, which is the half that matters on a
 * modest machine: the network brought two hundred, but two hundred cards in
 * the document is what makes a page crawl, not two hundred rows in memory.
 */
let block = null;
//: WHICH PAGE OF THE BLOCK IS ON SCREEN. Not the page of the whole answer:
//: with subjects ticked out and a page size the reader chose, "page 4 of the
//: archive's answer" is a number nothing on screen agrees with. `state.page`
//: stays the SERVER's page of the block's first row, so a link still opens
//: where it was shared from; this is the position inside it.
let blockPage = 0;

const perPage = document.getElementById("q-per-page");

/* How many rows to draw at once. A view setting, not a filter: the rows are
 * already here, so it costs one re-render and no request. */
function pageSize() {
  const wanted = Number(perPage && perPage.value);
  return Number.isFinite(wanted) && wanted > 0 ? wanted : 25;
}

/* THE ROWS THE TICKS HAVE LEFT. The filter is applied here and nowhere
 * else: one search fetched these rows, and leaving a subject out is a
 * question about what to SHOW, not a reason to ask the archive again. */
function keptRows(data) {
  const items = (data.groups && data.groups[0] && data.groups[0].items) || [];
  if (!excluded.size) return items;
  return items.filter((item) => {
    const subjects = item.subjects && item.subjects.length
      ? item.subjects
      : [{ name: item.about || "", type: item.type || "" }];
    // A row goes when EVERY subject it is about has been ticked out. A
    // document about Donald Trump and Chevron is still a document about
    // Chevron, and dropping it because one of its two subjects was unwanted
    // would take away a row nobody asked to lose.
    return subjects.some((s) => !excluded.has(subjectKey(s.name, s.type)));
  });
}

/* The rows of one page of the block, over the KEPT rows - so the pages
 * renumber as the ticks change: page four of a list that is now two pages
 * long is not a page. `page` is the position INSIDE the block. */
function sliceFor(data, page) {
  const size = pageSize();
  const items = keptRows(data);
  const from = page * size;
  if (from < 0 || from >= items.length) return null;
  return items.slice(from, from + size);
}

/* How many pages the block holds, at the size that is set. */
function blockPages(data) {
  return Math.max(1, Math.ceil(keptRows(data).length / pageSize()));
}

function renderResults(data, { page = null } = {}) {
  const words = data.terms || [];
  const at = page === null ? blockPage : page;
  resultsBox.textContent = "";
  resultsBox.setAttribute("aria-busy", "false");
  // Cleared before any of the early returns below: a note left standing
  // over an empty list would describe rows that are no longer there.
  noteDerivedNames([]);

  if (!data.total) {
    resultsBox.appendChild(el("p", "empty", emptyLine(words)));
    resultsMeta.textContent = "no results";
    renderPager(data);
    return;
  }

  // Results exist, but not on the page that was asked for. That is a
  // different message from "nothing matched", and it needs the way back.
  const rows = sliceFor(data, at);
  const shown = rows ? rows.length : 0;
  if (!shown) {
    resultsBox.appendChild(el("p", "empty", "That page is past the last one - go back with Previous."));
    resultsMeta.textContent = `${data.total} result${data.total === 1 ? "" : "s"} in ${data.where.label}`;
    renderPager(data);
    const prev = pager.querySelector("[data-page-prev]");
    if (prev && !prev.disabled) prev.focus();
    return;
  }

  const group = (data.groups || [])[0] || { label: "Results", kind: data.object_kind };
  const section = el("section", "results-group");
  const total = (data.counts && data.counts[group.kind]) || data.total || rows.length;
  section.appendChild(el("h3", null, `${group.label} (${rows.length} of ${total})`));
  const list = el("ul", "results-list");
  rows.forEach((item) => list.appendChild(resultItem(item, words)));
  section.appendChild(list);
  resultsBox.appendChild(section);
  noteDerivedNames(rows);

  resultsMeta.textContent = `${data.total} result${data.total === 1 ? "" : "s"} in ${data.where.label}`;
  renderPager(data, at);
}

/* Move to a page. WITHOUT ASKING THE ARCHIVE when the block already holds
 * it, which is seven presses out of eight. */
/* Step a page. INSIDE THE BLOCK IT IS A RE-DRAW; off either end of it, the
 * block next door is fetched - and `state.page` moves to that block's first
 * server-sized page, which is what a shared link then opens on. */
function goToPage(next) {
  if (!block) { search(); return; }
  const pages = blockPages(block);
  if (next >= 0 && next < pages) {
    blockPage = next;
    // THE POSITION INSIDE THE BLOCK IS IN THE ADDRESS TOO. `page` names the
    // block a link opens on; without this the link opened at its first page
    // and a reader who sent "look at this one" sent the wrong screen.
    setExtra("at", blockPage ? String(blockPage) : "");
    renderResults(block, { page: blockPage });
    // The list is redrawn under the reader's hands; the top of it is where
    // the next row is.
    if (resultsBox.scrollIntoView) resultsBox.scrollIntoView({ block: "start" });
    return;
  }
  const perBlock = Number(block.pages_per_block) || 8;
  const at = Number(block.block) || 0;
  const wanted = next < 0 ? at - 1 : at + 1;
  if (wanted < 0) return;
  // The far end of the new block when stepping back into it, so Previous
  // lands on the row before the one that was on screen and not at its start.
  blockPage = next < 0 ? -1 : 0;
  setExtra("at", "");
  setState({ page: wanted * perBlock });
  search();
}

/* ── Searching ───────────────────────────────────────────────────────── */

/* ── What the answer is about, and what to leave out of the next one ─────
 *
 * The state is a SET OF KEYS, one per unticked row, and a key is the name
 * and the type folded to lower case with a newline between them. Not an id:
 * one person is many entity rows in this archive - one per document that
 * mentions them - so an id would leave out a single mention and keep the
 * other four hundred. Name and type are what the reader ticked and what the
 * server matches on (routers/api_query.py: Subject).
 *
 * A newline as the separator because it is the one character an entity name
 * cannot contain and a URL carries without argument (%0A).
 */
const aboutPanel = document.getElementById("about-panel");
const aboutMeta = document.getElementById("about-meta");
const aboutRows = document.getElementById("about-rows");
const aboutNote = document.getElementById("about-note");
const aboutCountHead = document.getElementById("about-count-head");
const aboutFindName = document.getElementById("about-find-name");
const aboutFindType = document.getElementById("about-find-type");

const SEP = "\n";
let excluded = new Set();
let aboutItems = [];

function subjectKey(name, type) {
  return `${(name || "").trim().toLowerCase()}${SEP}${(type || "").trim().toLowerCase()}`;
}

/* WHAT THE RESULTS ON SCREEN WERE ASKED WITH. Compared against the ticks to
 * decide whether the answer still stands - see `search`. */
let shownExclusions = "";

function excludedSignature() {
  return [...excluded].sort().join("|");
}

function excludedList() {
  return [...excluded].map((key) => {
    const [name, type] = key.split(SEP);
    return { name, type };
  });
}

/* The exclusions in the address, so a narrowed search is a link somebody can
 * send - which is the whole point of doing this in the URL and not in a
 * variable. Two parallel lists rather than one joined string: a name and a
 * type pasted together with a separator are a name containing the separator
 * waiting to happen. */
function saveExcluded() {
  const list = excludedList();
  setExtra("not", list.map((s) => s.name).join(SEP));
  setExtra("not_types", list.map((s) => s.type).join(SEP));
}

function loadExcluded() {
  const names = getExtra("not").split(SEP).filter(Boolean);
  const types = getExtra("not_types").split(SEP);
  excluded = new Set(names.map((name, i) => subjectKey(name, types[i] || "")));
}

/* Which rows are on screen. The two boxes narrow what is SHOWN and never
 * what is ticked: a filter that unticked what it hid would throw away a
 * decision the reader had already made, and they would not see it happen. */
function aboutShown() {
  const wantName = (aboutFindName ? aboutFindName.value : "").trim().toLowerCase();
  const wantType = (aboutFindType ? aboutFindType.value : "").trim().toLowerCase();
  return aboutItems.filter((item) => {
    const name = (item.name || "").toLowerCase();
    const type = (item.type || "").toLowerCase();
    return (!wantName || name.includes(wantName)) && (!wantType || type.includes(wantType));
  });
}

/* Every row that shares a name, or a type, with this one.
 *
 * OVER THE WHOLE LIST, not over what the two boxes have left on screen. The
 * name IS the subject: one person is twenty-one rows here, because the
 * archive types them differently in different documents, and "not him"
 * means all twenty-one whether or not a filter is hiding some of them. The
 * button says how many it will act on, so the number is never a surprise.
 */
function sameBy(field, value) {
  const want = (value || "").trim().toLowerCase();
  return aboutItems.filter((item) => (item[field] || "").trim().toLowerCase() === want);
}

/* A group of rows goes the way the group is not: all ticked becomes all
 * unticked, anything else becomes all ticked. That is the only rule under
 * which one press always changes something, and a second press puts it
 * back. */
function toggleGroup(items) {
  const keys = items.map((item) => subjectKey(item.name, item.type));
  const allIn = keys.every((key) => !excluded.has(key));
  keys.forEach((key) => { if (allIn) excluded.add(key); else excluded.delete(key); });
  saveExcluded();
  renderAbout();
  showKept();
}

function setRow(key, row, tick, on) {
  if (on) excluded.delete(key);
  else excluded.add(key);
  if (tick) tick.checked = on;
  if (row) row.classList.toggle("is-left-out", !on);
  saveExcluded();
  sayAboutState();
  showKept();
}

/* THE TICK ACTS AT ONCE, because it costs nothing: the rows are already
 * here and leaving a subject out is a question about what to draw. There is
 * no press to wait for and no query to spend. */
function showKept() {
  if (!block) return;
  blockPage = Math.min(blockPage, blockPages(block) - 1);
  renderResults(block, { page: blockPage });
}

function renderAbout() {
  if (!aboutPanel) return;
  const shown = aboutShown();
  aboutRows.textContent = "";
  shown.forEach((item) => {
    const key = subjectKey(item.name, item.type);
    const row = document.createElement("tr");
    row.className = "about-row";
    if (excluded.has(key)) row.classList.add("is-left-out");

    const tickCell = el("td", "about-tick");
    const tick = document.createElement("input");
    tick.type = "checkbox";
    tick.checked = !excluded.has(key);
    // The row's own words as the label: "Search Donald Trump (Politician)"
    // is what a screen reader should say, not "checkbox, 412".
    tick.setAttribute("aria-label",
      `Search ${item.name}${item.type ? ` (${item.type})` : ""}`);
    tick.addEventListener("change", () => setRow(key, row, null, tick.checked));
    tickCell.appendChild(tick);
    row.appendChild(tickCell);

    // THE NAME AND THE TYPE ARE BUTTONS, and each acts on its whole group.
    // Donald Trump is twenty-one rows of this list; unticking him one row at
    // a time is twenty-one presses for one decision, and the decision was
    // never about the twenty-one types. Real <button>s, so the keyboard
    // reaches them and a screen reader is told what a press would do.
    row.appendChild(groupCell("about-name", item.name || "Unnamed", "name", item));
    row.appendChild(groupCell("about-type", item.type || "-", "type", item));
    row.appendChild(el("td", "num", fmtInt(item.rows)));

    // AND THE ROW ITSELF IS THE SINGLE TICK. A click anywhere else in it
    // does what the box does - a row of a tick-list is a target, and hunting
    // a 20 px box across a wide table is the thing this spares. The two
    // buttons stop the click before it gets here, so a name click is never
    // also a row click.
    row.addEventListener("click", (event) => {
      if (event.target.closest("button, input, a")) return;
      setRow(key, row, tick, excluded.has(key));
    });
    aboutRows.appendChild(row);
  });
  if (!shown.length) {
    const row = document.createElement("tr");
    const cell = el("td", "empty", aboutItems.length
      ? "No row of this list matches those two boxes."
      : "Nothing to narrow: this search is about nobody the archive names.");
    cell.colSpan = 4;
    row.appendChild(cell);
    aboutRows.appendChild(row);
  }
  sayAboutState();
}

/* One line that says what the ticks would do, and whether they have been
 * acted on yet. "Leaving out 3" beside a list that still holds their rows is
 * the sentence a reader needs to know a press is owed. */
function sayAboutState() {
  if (!aboutNote) return;
  const shown = aboutShown().length;
  const parts = [];
  if (aboutItems.length > shown) parts.push(`${fmtInt(shown)} of ${fmtInt(aboutItems.length)} shown`);
  if (excluded.size) parts.push(`${fmtInt(excluded.size)} left out`);
  aboutNote.textContent = parts.join(" \u00b7 ");
  aboutNote.hidden = !parts.length;

}

function tickEveryShown(on) {
  aboutShown().forEach((item) => {
    const key = subjectKey(item.name, item.type);
    if (on) excluded.delete(key);
    else excluded.add(key);
  });
  saveExcluded();
  renderAbout();
  showKept();
}

/* The list itself, asked for beside the results and never instead of them.
 * A failure here is a missing list and nothing else: the answer is on the
 * screen either way, so this says so quietly rather than replacing it. */
/* One cell whose text is a button over its whole group. */
function groupCell(className, text, field, item) {
  const cell = el("td", className);
  const button = document.createElement("button");
  button.type = "button";
  button.className = "about-group";
  button.textContent = text;
  const group = sameBy(field, item[field]);
  const allIn = group.every((one) => !excluded.has(subjectKey(one.name, one.type)));
  const word = field === "name" ? "named" : "of type";
  button.title = group.length > 1
    ? `${allIn ? "Leave out" : "Search"} all ${fmtInt(group.length)} rows ${word} ${text}`
    : `${allIn ? "Leave out" : "Search"} ${text}`;
  button.setAttribute("aria-label", button.title);
  button.addEventListener("click", (event) => {
    event.stopPropagation();
    toggleGroup(group);
  });
  cell.appendChild(button);
  return cell;
}

/* THE LIST IS BUILT FROM THE ROWS THAT ARE ALREADY HERE.
 *
 * Not a second question to the archive - "count what this search is about"
 * - which on a place the size of Houston takes half a minute for an answer
 * the reader has, in effect, already been sent. Every
 * row carries the subjects it is about with their types (routers/
 * api_query.py: `subjects`), so the list is a count over what is in hand.
 * ONE SEARCH, ONE QUERY.
 *
 * The counts are therefore of the BLOCK, not of the whole archive: two
 * hundred rows, which is what a reader is about to page through. The panel
 * says so beside its heading, because "53" that means "53 of the two hundred
 * loaded" and "53" that means "53 of 781" are different numbers and only one
 * of them is true here.
 */
function buildAbout(rows) {
  const counts = new Map();
  (rows || []).forEach((item) => {
    const subjects = item.subjects && item.subjects.length
      ? item.subjects
      : [{ name: item.about || "", type: item.type || "" }];
    subjects.forEach((subject) => {
      const name = (subject.name || "").trim();
      if (!name) return;
      const type = (subject.type || "").trim();
      const key = subjectKey(name, type);
      const seen = counts.get(key);
      if (seen) seen.rows += 1;
      else counts.set(key, { name, type, rows: 1 });
    });
  });
  aboutItems = [...counts.values()].sort((a, b) => b.rows - a.rows
    || a.name.localeCompare(b.name));
  if (!aboutPanel) return;
  aboutPanel.hidden = !aboutItems.length;
  if (aboutMeta) {
    aboutMeta.textContent = `${fmtInt(aboutItems.length)} in the rows loaded`;
  }
  renderAbout();
}

let running = false;

async function search({ resetPage = false } = {}) {
  if (running) return;
  if (resetPage) {
    setState({ page: 0 });
    // A new question starts at its first page, inside the block as well as
    // across them.
    blockPage = 0;
    setExtra("at", "");
  }

  // Something typed into the address field but never chosen: resolve it
  // first. This is the path that puts the "which Springfield?" question on
  // the screen, and it is why Search does not always search.
  const typed = (addressField && addressField.value.trim()) || "";
  if (typed) {
    running = true;
    try {
      const ready = await resolveAddress(typed);
      if (!ready) return;
    } catch (err) {
      if (!isAbort(err)) reportProblem(err, "Could not look that place up.");
      return;
    } finally {
      running = false;
    }
  }

  if (where.kind === "") {
    if (!choicesBox.hidden) {
      setStatus("Choose which place you mean first.", true);
      const first = choicesBox.querySelector("input");
      if (first) first.focus();
    } else {
      setStatus("Name a place, or open “Use coordinates and a distance instead”.", true);
      if (addressField) addressField.focus();
    }
    return;
  }

  const body = {
    terms: terms(),
    kind: objectKind(),
    match_all: Boolean(allTerms.checked),
    date_from: fromField.value || null,
    date_to: toField.value || null,
    page: state.page || 0,
  };
  if (where.kind === "place") {
    body.place = { address: where.address };
    // Only where the row stands for several places; one place is searched
    // by its own address, as it always was.
    if (where.match) body.place.match = where.match;
  } else { body.lat = where.lat; body.lng = where.lng; body.radius_km = where.km; }

  // The URL is the state: a reload, a bookmark and a shared link all show
  // this same search again.
  setExtra("terms", body.terms.join(","));
  setExtra("kind", body.kind === "source" ? "" : body.kind);
  setExtra("all", body.match_all ? "1" : "");
  setExtra("from", body.date_from || "");
  setExtra("to", body.date_to || "");
  setExtra("km", where.kind === "point" ? String(where.km) : "");
  // The reading, when the chosen row stands for several places. Without it
  // "All 6 of them" was a choice a reload threw away: the link carried the
  // name alone, and a name alone is a question again.
  setExtra("match", where.kind === "place" ? (where.match || "") : "");

  running = true;
  resultsBox.setAttribute("aria-busy", "true");
  setStatus("Searching…");
  // AN ANSWER THAT IS KNOWN TO BE WRONG IS TAKEN AWAY WHILE THE RIGHT ONE IS
  // FETCHED.
  //
  // Every other view in this dashboard keeps its rows while it loads and only
  // dims them, and that is right there: an answer being refreshed is still an
  // answer to the same question. This is the one case where it is not. Press
  // "Search without 21" and the list on the screen is the list WITH those
  // twenty-one in it - the reader has just said they do not want them, is
  // looking straight at them, and has no way to tell the old answer from the
  // new one until the numbers change. So the rows go, and the panel says what
  // it is doing instead.
  //
  // Only when the ticks have actually moved. A second press of the same
  // search is the ordinary case and keeps what is there.
  block = null;
  if (excludedSignature() !== shownExclusions) {
    resultsBox.textContent = "";
    resultsBox.appendChild(el("p", "loading", "Searching without what you ticked out…"));
    resultsMeta.textContent = "";
    pager.hidden = true;
  }
  try {
    const data = await api("/api/query/search", {
      channel: "query-search", method: "POST", body,
    });
    setStatus("");
    shownExclusions = excludedSignature();
    block = data;
    // The list of subjects is a count over the rows that just arrived, not a
    // second question to the archive: see buildAbout.
    buildAbout((data.groups && data.groups[0] && data.groups[0].items) || []);
    // -1 is "the far end", set by a Previous that stepped out of the block.
    // Anything else is kept: the boot reads a shared link's own position out
    // of the address, and a search that asked a NEW question set it to zero
    // itself (see `search`).
    blockPage = blockPage < 0 ? Math.max(0, blockPages(data) - 1)
                              : Math.min(blockPage, Math.max(0, blockPages(data) - 1));
    renderResults(data, { page: blockPage });
    drawPlaces(data.places, data.total);
    announce(`${data.total} results in ${data.where.label}`);
  } catch (err) {
    if (isAbort(err)) return;
    resultsBox.setAttribute("aria-busy", "false");
    // ONE readable place, not three. The same failure must not stand next
    // to the Search button, in the Results panel and in the toast at the
    // same time - three copies of one run-on line. The panel is where the answer
    // was going to appear, so the panel says what happened instead; api.js's
    // toast is the interruption that carries it to somebody who has scrolled
    // away, in those same words. The line by the button is for what the FORM
    // still needs ("choose which place you mean"), so it steps aside here.
    setStatus("");
    // Leaving "Name a place… then press Search." standing there would tell
    // somebody to do the thing they just did.
    resultsBox.textContent = "";
    resultsBox.appendChild(errorNotice("The search failed.", err));
    resultsMeta.textContent = "";
    pager.hidden = true;
  } finally {
    running = false;
  }
}

/* ── Wiring ──────────────────────────────────────────────────────────── */

form.addEventListener("submit", (e) => {
  e.preventDefault();
  search({ resetPage: true });
});

/* The About list: two boxes that narrow it and two buttons that tick what
 * they left. Nothing here searches - the ticks are a decision about the NEXT
 * press, which is the rule the whole view is built on (app.css). */
[aboutFindName, aboutFindType].forEach((field) => {
  if (field) field.addEventListener("input", renderAbout);
});
const aboutAll = document.getElementById("about-all");
const aboutNone = document.getElementById("about-none");
if (aboutAll) aboutAll.addEventListener("click", () => tickEveryShown(true));
if (aboutNone) aboutNone.addEventListener("click", () => tickEveryShown(false));

// Choosing an address from the list is an answer to "where": resolve it at
// once, so the "which Springfield?" question comes up while the person is
// still looking at the field.
if (addressField) {
  addressField.addEventListener("typeahead:choose", (e) => {
    const value = e.detail.value;
    if (!value) return;
    setStatus("");
    // `typed` is the typeahead's own word for "Enter on text that was not
    // in the list". Everything else came OUT OF THE LIST and is therefore
    // one of the archive's own addresses - a choice already made, which is
    // exactly what `exact` tells the server. Picking "Houston, Texas, USA"
    // and being asked which Houston was meant is the fault this line
    // prevents.
    const chosen = !e.detail.typed;
    // CHOOSING A PLACE IS NOT ASKING FOR THE ANSWER. Running the search
    // straight off this event would mean a click in a suggestion list starts
    // a query over the whole archive - and on a big project that is a minute
    // of database, spent by somebody who is still deciding what to ask.
    //
    // The place is still RESOLVED here, because that is the cheap half and it
    // is what puts "which Springfield?" on the screen while the reader is still
    // looking at the field. What waits for Enter or the Search button is the
    // search itself.
    resolveAddress(value, { exact: chosen })
      .then((ready) => {
        if (ready) setStatus("Press Search to run this.");
      })
      .catch((err) => { if (!isAbort(err)) reportProblem(err, "Could not look that place up."); });
  });
}

// `input` as well as `change`: the line under the group should say what is
// set while the number is being typed, not only when the field is left.
[latField, lngField].forEach((field) => {
  field.addEventListener("input", usePoint);
  field.addEventListener("change", usePoint);
});

/* Slider and number field are one value. Both write it, both show it, and
 * the circle follows immediately - the distance is the one setting whose
 * effect is only visible on the map. */
function syncKm(from) {
  const value = from === "range" ? kmRange.value : kmNumber.value;
  const km = Math.max(1, Number(value) || DEFAULT_RADIUS_KM);
  kmNumber.value = String(km);
  // The slider only goes to its own maximum; a larger number stays valid in
  // the field and parks the slider at the end.
  kmRange.value = String(Math.min(km, Number(kmRange.max)));
  if (where.kind === "point") {
    where.km = km;
    setExtra("km", String(km));
    describeWhere();
    drawCircle();
    // The circle follows at once; the results do not. Saying so is the
    // difference between "it does not work" and "press the button".
    if (resultsMeta.textContent) setStatus("The distance changed - press Search to use it.");
  }
}
kmRange.addEventListener("input", () => syncKm("range"));
kmNumber.addEventListener("input", () => syncKm("number"));

/* The date a preset stands for, as the field spells it. */
function presetStart(days) {
  const from = new Date();
  from.setDate(from.getDate() - Number(days));
  return from.toISOString().slice(0, 10);
}

/* Which preset is in force - after a click and after a reload. They are
 * toggle buttons and `aria-pressed` is the state; without it the row was
 * four blue words that never said which of them the results obeyed. The
 * Events page does the same, in the same words: both set a START and leave
 * the end open, so both say "From …". */
function markPresets() {
  document.querySelectorAll("[data-preset]").forEach((button) => {
    const days = button.dataset.preset;
    const on = days
      ? fromField.value === presetStart(days) && !toField.value
      : !fromField.value && !toField.value;
    button.setAttribute("aria-pressed", on ? "true" : "false");
  });
}

document.querySelectorAll("[data-preset]").forEach((button) => {
  button.addEventListener("click", () => {
    const days = button.dataset.preset;
    fromField.value = days ? presetStart(days) : "";
    toField.value = "";
    markPresets();
    if (where.kind) search({ resetPage: true });
  });
});

[fromField, toField].forEach((field) => {
  field.addEventListener("change", markPresets);
  field.addEventListener("input", markPresets);
});

if (whereSwitch) {
  whereSwitch.addEventListener("click", (event) => {
    const button = event.target.closest("[data-where]");
    if (!button || button.getAttribute("aria-pressed") === "true") return;
    setWhereMode(button.dataset.where);
    setExtra("where", button.dataset.where === "point" ? "point" : "");
    // The focus goes into the half that is now on the page: the reader
    // pressed this to type something, and the field they meant is new.
    const field = document.querySelector('[data-where-field]:not([hidden]) input');
    if (field) field.focus();
  });
}

document.getElementById("q-clear").addEventListener("click", () => {
  where = { kind: "", address: "", note: "", match: "", lat: null, lng: null, km: DEFAULT_RADIUS_KM };
  setAddressField("");
  latField.value = "";
  lngField.value = "";
  kmNumber.value = String(DEFAULT_RADIUS_KM);
  kmRange.value = String(DEFAULT_RADIUS_KM);
  setTerms([]);
  allTerms.checked = false;
  fromField.value = "";
  toField.value = "";
  markPresets();
  hideChoices();
  describeWhere();
  ["address", "match", "lat", "lng", "km", "terms", "all", "from", "to"]
    .forEach((k) => setExtra(k, ""));
  setState({ page: 0 });
  resultsBox.textContent = "";
  resultsBox.appendChild(el("p", "empty", "Name a place, or give a point and a distance, then press Search."));
  resultsMeta.textContent = "";
  pager.hidden = true;
  drawPlaces([], null);
  setStatus("");
  if (addressField) addressField.focus();
});

pager.querySelector("[data-page-prev]").addEventListener("click", () => goToPage(blockPage - 1));
pager.querySelector("[data-page-next]").addEventListener("click", () => goToPage(blockPage + 1));

/* HOW MANY AT ONCE. A re-draw of rows that are already here, and the page
 * goes back to the first: page three of twenty-five is not page three of a
 * hundred, and landing somewhere in the middle of a list that just changed
 * shape is how a reader loses their place. */
if (perPage) {
  perPage.addEventListener("change", () => {
    setExtra("per", perPage.value === "25" ? "" : perPage.value);
    blockPage = 0;
    if (block) renderResults(block, { page: 0 });
  });
}

enlargeButton.addEventListener("click", enlarge);

/* ── Start: fill everything from the URL, then search if there is a where ─ */

(function start() {
  const km = Number(getExtra("km"));
  if (Number.isFinite(km) && km > 0) {
    kmNumber.value = String(km);
    kmRange.value = String(Math.min(km, Number(kmRange.max)));
  }
  const savedTerms = getExtra("terms");
  if (savedTerms) setTerms(savedTerms.split(",").map((t) => t.trim()).filter(Boolean));
  allTerms.checked = getExtra("all") === "1";
  // A link carries what it searched for, so a shared search comes back as the
  // same search rather than as its default.
  setObjectKind(getExtra("kind") || "source");
  // A NARROWED SEARCH IS A LINK SOMEBODY CAN SEND. The ticks are read back
  // before the first search runs, so the answer a shared address opens on is
  // the narrowed one it was shared as - not the wide one it started from.
  loadExcluded();
  // Where in the block the link was sent from, and how many rows it drew.
  blockPage = Math.max(0, Number(getExtra("at")) || 0);
  const per = getExtra("per");
  if (perPage && per && [...perPage.options].some((o) => o.value === per)) perPage.value = per;
  fromField.value = getExtra("from");
  toField.value = getExtra("to");
  markPresets();

  const address = getExtra("address");
  const lat = parseFloat(getExtra("lat"));
  const lng = parseFloat(getExtra("lng"));
  ensureMap();

  if (address) {
    // A LINK IS NOT AN ANSWER. The address in the URL is text somebody
    // shared, and "Springfield" means two towns here whether it was typed
    // into the field or arrived in a link. Trusting it and searching
    // straight away would show, for a shared link to an ambiguous place, the
    // results for whichever candidate the archive sorts first, with nothing
    // on the screen to say the other one exists.
    //
    // So it goes through the same lookup as every other path: one meaning
    // and the search runs (with "all 3 addresses in the country" in the line
    // under the field); more than one and the chooser comes up
    // instead, exactly as if the name had just been typed - only without
    // taking the focus, because nobody pressed anything to get here.
    //
    // UNLESS THE LINK CARRIES THE ANSWER TOO. `match` is only ever written
    // by a search that ran on a row standing for several places - "All 6 of
    // them" - so a URL that has it is not a bare name any more, it is
    // somebody's decision about one. It is resolved the way a picked
    // suggestion is: settled, and counted again so the line under the field
    // still says what it covers.
    setAddressField(address);
    setStatus(`Looking up “${address}”…`);
    resolveAddress(address, { focus: false, exact: Boolean(getExtra("match")) })
      .then((ready) => { if (ready) { setStatus(""); search(); } })
      .catch((err) => { if (!isAbort(err)) reportProblem(err, "Could not look that place up."); });
  } else if (Number.isFinite(lat) && Number.isFinite(lng)) {
    latField.value = String(lat);
    lngField.value = String(lng);
    setWhereMode("point", { clear: false });
    where = { kind: "point", address: "", note: "", match: "", lat, lng, km: currentKm() };
    describeWhere();
    drawCircle();
    search();
  } else {
    // A LINK THAT SAID "coordinates" OPENS ON COORDINATES, even with the
    // fields still empty: the URL is the state of this page, and a reader
    // who sent the link chose that half.
    if (getExtra("where") === "point") setWhereMode("point", { clear: false });
    describeWhere();
  }
})();
