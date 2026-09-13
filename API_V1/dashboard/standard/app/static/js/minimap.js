/* ==========================================================================
 *  The small map on the Query view - and the big one in the dialog, which
 *  is the same thing at a different size.
 *
 *  Leaflet, vendored (static/vendor/leaflet), loaded by the template as a
 *  plain script so `L` is a global by the time this module runs. When it is
 *  not there the map degrades to a sentence: the page still works, because
 *  the list of places under the map is the real content and the map is the
 *  picture of it (MN.gov's interactive-map guidance, and the reason the
 *  list is never hidden).
 *
 *  What it draws: one circle for "within N km of this point", and one
 *  marker per address. Nothing else - no clustering, no heat; those are the
 *  Map and Heatmap views.
 *
 *  Deliberate settings:
 *    scrollWheelZoom       on for both sizes - see below for the mode that
 *                          exists for a map that must ask first, and why
 *                          neither of these two uses it.
 *    keyboard on           +/- and the arrow keys work, markers are
 *                          focusable and Enter opens their popup.
 *    tap/drag left alone   there is no drag-only or hover-only function:
 *                          everything a marker says is also in the list.
 *
 *  THE WHEEL, AND WHY THE TWO SIZES ANSWER DIFFERENTLY.
 *
 *  "One should be able to zoom in and out by scrolling" is the request, and
 *  on the Map and the Heatmap it is plainly right: those views are the whole
 *  window, there is nothing to scroll past them, and the wheel has no other
 *  job. The small map is different - it sits inside a form a reader
 *  scrolls - so it asks first: `wheel: "click"` leaves the wheel
 *  off, says so in the map's own corner ("Click the map to zoom with the
 *  wheel") and arms it once somebody clicks or focuses INSIDE the map,
 *  disarming again when the pointer leaves.
 *
 *  IN PRACTICE ONE RULE BEATS TWO. query.js builds BOTH of its maps with
 *  `wheel: "always"` and neither carries a hint: every map in the product
 *  zooms on the wheel with the pointer over it, so there is nothing to learn
 *  twice. The page still scrolls everywhere the map is not, and
 *  tests/ui/test_query_minimap_popup.py measures exactly that pair of facts.
 *
 *  `wheel: "click"` is kept, wired and documented rather than deleted: it is
 *  the mode a map embedded in a longer scrolling page would want, and the
 *  argument for it is above. Nothing in the product asks for it today.
 *
 *  The +/- buttons stay in both cases: a wheel is not available to everyone,
 *  and swapping a visible control for a hidden gesture is a step back.
 * ========================================================================== */

const DEFAULT_TILE = "https://tile.openstreetmap.org/{z}/{x}/{y}.png";
const ATTRIBUTION = '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors';

/* The tile server is a setting (DASHBOARD_TILE_URL): customers who may not
 * call out to openstreetmap.org put their own in. The template stamps it
 * into a <meta>; the default is only there so the page works before anybody
 * has set anything. */
import { pinIcon } from "./mappin.js";
import { heatStops, HEAT_MIN_ALPHA } from "./palette.js";
import { arcPath } from "./mapline.js";

/* The same numbers the Heatmap view uses (static/js/heatmap.js): a radius
 * wide enough that two nearby squares melt into one patch, and a blur of
 * two thirds of it, which is what turns points into an area. */
const HEAT_RADIUS = 30;
const HEAT_BLUR_SHARE = 0.66;

export function tileUrl() {
  const meta = document.querySelector('meta[name="tile-url"]');
  const value = meta && meta.content ? meta.content.trim() : "";
  return value || DEFAULT_TILE;
}

function say(container, text) {
  container.textContent = "";
  const p = document.createElement("p");
  p.className = "minimap-empty";
  p.textContent = text;
  container.appendChild(p);
}

/* Used when Leaflet is not in the build: every method exists and does
 * nothing, so the caller needs no `if (map)` anywhere. */
function noMap(container) {
  say(container, "The map could not be loaded. The places are listed below.");
  return {
    available: false,
    wheelZooms: false,
    setCircle() {}, setMarkers() {}, setPoints() {}, setLines() {},
    fit() {}, invalidate() {}, destroy() {},
  };
}

/* What a dot on a Diagrams map says. The same three facts a pin says - what
 * is there, where, and of what kind - plus the number the dot's size stands
 * for, because a size is a comparison and never a value. */
function pointTitle(place) {
  const names = (place.names || []).filter(Boolean);
  const first = names[0] || place.address || "";
  // `named` is how many are AT the point; `names` is the handful the popup
  // lists. "+3" counted off the short list would be a lie on a pin that
  // stands for nine things.
  const held = Math.max(Number(place.named) || 0, names.length);
  const more = held > 1 ? ` +${held - 1}` : "";
  const count = Number(place.count) || 0;
  return count > 1 ? `${first}${more} - ${count}` : `${first}${more}`;
}

/* THE TWO LINES A MAP TOOLTIP HAS, and the one place they are built.
 *
 * The Map view's pin says the entity and, under it, the type and the address
 * that tell two pins of one company apart (static/js/map.js). This is the
 * same pin on the Diagrams map and it says the same two things, out of what
 * a Diagrams place carries: the names standing at the point, and where that
 * point is.
 *
 * A SUMMARY, NEVER THE ONLY PLACE A FACT LIVES - a tooltip needs a pointer,
 * so the name is also the pin's accessible name and the popup says all of
 * this and more. */
function tipBody(name, where) {
  const box = document.createElement("div");
  box.className = "map-tip-body";
  const first = document.createElement("span");
  first.className = "map-tip-name";
  first.textContent = name;
  box.appendChild(first);
  if (where) {
    const second = document.createElement("span");
    second.className = "map-tip-where";
    second.textContent = where;
    box.appendChild(second);
  }
  return box;
}

function tipForPoint(place) {
  const names = (place.names || []).filter(Boolean);
  // `named` is how many are AT the point; `names` is the handful the popup
  // lists, so the "+3" is counted off the full number and not off the list.
  const held = Math.max(Number(place.named) || 0, names.length);
  const name = (names[0] || place.address || "")
    + (held > 1 ? ` +${held - 1}` : "");
  const count = Number(place.count) || 0;
  const rows = count > 1 ? `${count} rows` : "";
  return tipBody(name, [(place.types || [])[0], place.address, rows]
    .filter(Boolean).join(" - "));
}

function popupForPoint(place) {
  const box = document.createElement("div");
  const names = (place.names || []).filter(Boolean);
  const title = document.createElement("span");
  title.className = "popup-title";
  title.textContent = names[0] || place.address || "";
  box.appendChild(title);
  if (place.address && place.address !== title.textContent) {
    const address = document.createElement("span");
    address.className = "popup-address";
    address.textContent = place.address;
    box.appendChild(address);
  }
  const kinds = (place.types || []).filter(Boolean);
  if (kinds.length) {
    const type = document.createElement("div");
    type.className = "popup-type";
    type.textContent = kinds.join(" - ");
    box.appendChild(type);
  }
  // The rest of the names, when the dot stands for more than one thing: a
  // dot labelled "Apple Inc. +3" that cannot say which three is a dot the
  // reader has to go somewhere else to understand. Past a handful it says
  // how many it is not naming, rather than growing a popup taller than the
  // map it sits on.
  const held = Math.max(Number(place.named) || 0, names.length);
  if (names.length > 1 || held > names.length) {
    const rest = document.createElement("div");
    rest.className = "popup-type";
    const hidden = held - names.length;
    rest.textContent = names.slice(1).join(" - ")
      + (hidden > 0 ? `${names.length > 1 ? " - " : ""}and ${hidden} more` : "");
    box.appendChild(rest);
  }
  const count = Number(place.count) || 0;
  if (count) {
    const n = document.createElement("div");
    n.className = "popup-type";
    n.textContent = count === 1 ? "1 row" : `${count} rows`;
    box.appendChild(n);
  }
  return box;
}

/* ONE TIE, TWO SENTENCES - one per direction, one line each.
 *
 * Not "Nordwyk - Nordwyk Pumps Asia" and under it both role words at once
 * ("Ownership - Group"), which says that the two are tied and leaves the
 * reader to guess which of them owns the other. A connection is directed
 * and the archive knows both directions, so the popup writes them out:
 * "Nordwyk is a Ownership of Nordwyk Pumps Asia" and "Nordwyk Pumps Asia
 * is a Subsidiary of Nordwyk".
 *
 * EACH SENTENCE STAYS ON ITS OWN LINE. A name long enough to break it is cut
 * with an ellipsis and the whole sentence is on the element's title, which
 * is the same bargain every truncated label on this page makes.
 */
function sentence(from, to) {
  const line = document.createElement("div");
  line.className = "popup-line";
  const text = `${from.name || "?"} is a ${from.role || "party"} of ${to.name || "?"}`;
  line.textContent = text;
  line.title = text;
  return line;
}

function popupForLine(line) {
  const box = document.createElement("div");
  box.className = "popup-lines";
  box.appendChild(sentence(line.from, line.to));
  box.appendChild(sentence(line.to, line.from));
  /* THE COLOUR'S NAME, and only when it is not one of the two words the
   * sentences have just used. The groups are named after the ties in them, so
   * on a Customer/Supplier line the group is often called "Customer" - and a
   * third line repeating a word the reader has read twice is the "it shows
   * both types" this popup was rewritten to stop doing. */
  const roles = [line.from.role, line.to.role].map((w) => String(w || "").toLowerCase());
  if (line.group_name && !roles.includes(String(line.group_name).toLowerCase())) {
    const group = document.createElement("div");
    group.className = "popup-type";
    group.textContent = line.group_name;
    box.appendChild(group);
  }
  const count = Number(line.count) || 0;
  if (count) {
    const n = document.createElement("div");
    n.className = "popup-type";
    n.textContent = count === 1 ? "1 connection" : `${count} connections`;
    box.appendChild(n);
  }
  return box;
}


export function createMiniMap(container, opts = {}) {
  if (!container) throw new Error("minimap: no container");
  if (!window.L) return noMap(container);
  const L = window.L;

  // "always" (the dialog, which fills the window) or "click" (the small map
  // inside the form, which has to be asked first). See the header.
  const wheel = opts.wheel === "always" ? "always" : "click";

  container.textContent = "";
  const map = L.map(container, {
    scrollWheelZoom: wheel === "always",
    zoomControl: true,
    keyboard: true,
    attributionControl: true,
  }).setView(opts.center || [20, 0], opts.zoom || 1);

  /* THE HINT, AND THE TWO GESTURES THAT SWITCH THE WHEEL.
   *
   * A gesture nobody is told about is not a feature, so the map says what it
   * wants before it will take the wheel, in its own bottom-left corner where
   * it cannot cover the zoom buttons or the attribution. The hint goes away
   * while the wheel is armed - a line that is always there stops being read -
   * and comes back when the pointer leaves, which is also when the wheel is
   * disarmed so the page scrolls normally again.
   *
   * `mouseleave` on the container, not Leaflet's own `mouseout`: the DOM
   * event does not bubble out of the tiles and the markers, so moving from
   * one tile to the next does not disarm the map under the reader's hand. */
  let hint = null;
  if (wheel === "click") {
    hint = document.createElement("p");
    hint.className = "minimap-hint";
    hint.textContent = "Click the map to zoom with the wheel";
    /* STYLED FROM HERE, AND ONLY WITH TOKENS. The Query page's stylesheet is
     * not this module's to edit, and this element exists only because this
     * module drew it - so it carries its own box, built out of the page's own
     * custom properties (app.css) rather than any colour of its own.
     * `pointer-events: none` matters: the hint sits over the map, and a click
     * aimed at the map through it has to reach the map, which is the very
     * gesture that makes the hint go away. z-index 900 is above Leaflet's
     * panes (400-700) and below its controls (1000), so it can cover neither
     * the zoom buttons nor the attribution. */
    hint.style.cssText = [
      "position: absolute", "left: 0.5rem", "bottom: 0.5rem", "z-index: 900",
      "margin: 0", "padding: 0.25rem 0.5rem", "max-width: calc(100% - 1rem)",
      "pointer-events: none", "font-size: 1rem", "line-height: 1.35",
      "background: var(--surface)", "color: var(--text)",
      "border: 1px solid var(--line)", "border-radius: var(--radius)",
    ].join(";");
    container.appendChild(hint);
    const arm = () => {
      map.scrollWheelZoom.enable();
      hint.hidden = true;
    };
    const disarm = () => {
      map.scrollWheelZoom.disable();
      hint.hidden = false;
    };
    // A click anywhere in the map arms it, and so does the keyboard reaching
    // it: a reader who tabs into the map has asked for it as plainly as one
    // who clicked.
    map.on("click", arm);
    container.addEventListener("pointerdown", arm);
    container.addEventListener("focusin", arm);
    container.addEventListener("mouseleave", disarm);
  }

  L.tileLayer(tileUrl(), {
    maxZoom: 18,
    // crossOrigin so html2canvas can put the tiles into the PDF export;
    // without it the canvas is tainted and the export loses the map.
    crossOrigin: true,
    attribution: ATTRIBUTION,
  }).addTo(map);

  /* TWO LAYERS, AND THE PLACES ARE ALWAYS ON TOP OF THE LINES.
   *
   * Leaflet's default panes decide this and the numbers are what make it
   * true: a polyline goes into the overlay pane (z-index 400) and a marker
   * into the marker pane (600), so a line can never cover the pin it ends
   * at. The dots drawn by setPoints() are circles, which would land in the
   * overlay pane WITH the lines and be covered by whichever was added last
   * - so they are put into the marker pane by hand (`pane` below).
   *
   * The Map view follows the same order: a place, and the number on it, are
   * what the reader is looking for, and a line between two of them must
   * never cover one. One rule, on every map in the product. */
  const lineLayer = L.layerGroup().addTo(map);
  const markerLayer = L.layerGroup().addTo(map);
  let circle = null;
  /* The heat field, made only if a caller ever asks for one. Kept out of the
   * two layers above on purpose: heat is a picture of DENSITY and belongs
   * under everything, while a pin and a line are things to click. */
  let heat = null;
  let heatBounds = null;
  let drawnLines = [];
  let heatPoints = [];
  let heatRadius = HEAT_RADIUS;
  // THE SPOTS AS THE ANSWER GAVE THEM, beside the intensities the layer was
  // handed: a heat point is three numbers and says nothing about what it
  // counted, and the tooltip is about exactly that.
  let heatSpots = [];
  let heatTip = null;

  /* EVERYTHING THE FIELD IS CONFIGURED WITH, and it is what the Heatmap view
   * configures its own field with (static/js/heatmap.js: heatOptions).
   *
   * Two of these were wrong here and the field was the difference:
   *
   *   `maxZoom` is not a limit, it is the zoom at which a point counts for
   *   its whole weight - leaflet.heat divides the intensity by
   *   2^(maxZoom - zoom) below it. Pinned at 12 while the card sits at zoom
   *   3 or 4, every weight was divided by 2^8 and the field was invisible.
   *   It is the CURRENT zoom, re-set whenever the map moves.
   *
   *   The gradient was the plugin's own blue-to-red default rather than the
   *   product's yellow-to-red ramp (map.css, read by palette.js), so the
   *   summary under the Locations tab and the Heatmap view were two colour
   *   schemes for one question. */
  function heatSettings() {
    return {
      radius: heatRadius,
      blur: Math.max(1, Math.round(heatRadius * HEAT_BLUR_SHARE)),
      minOpacity: HEAT_MIN_ALPHA,
      maxZoom: map ? map.getZoom() : 12,
      gradient: heatStops(),
    };
  }

  /* HOW MANY ARE UNDER THE POINTER. The same sentence the Heatmap view shows
   * over its own field (static/js/heatmap.js), because it is the same
   * picture: a wash is a shape and not a value, and "how many is that patch"
   * is the first question a reader has of one.
   *
   * Within one heat radius of a counted spot and nowhere else - a tooltip
   * that follows the pointer across empty ocean is a sentence about nowhere.
   * One tooltip, moved: a field can hold four hundred spots. */
  function nearestSpot(latlng) {
    if (!heatSpots.length) return null;
    const at = map.latLngToContainerPoint(latlng);
    let best = null;
    let bestGap = Infinity;
    heatSpots.forEach((spot) => {
      const q = map.latLngToContainerPoint([Number(spot.lat), Number(spot.lng)]);
      const gap = Math.hypot(q.x - at.x, q.y - at.y);
      // A tie goes to the busier spot: it is what the wash is mostly made of.
      if (gap < bestGap || (gap === bestGap && best && (spot.count || 0) > (best.count || 0))) {
        bestGap = gap;
        best = spot;
      }
    });
    return bestGap <= heatRadius ? best : null;
  }

  function spotWords(spot) {
    const n = Number(spot.count) || 0;
    const where = spot.address || (spot.names || [])[0]
      || `${Number(spot.lat).toFixed(3)}, ${Number(spot.lng).toFixed(3)}`;
    return tipBody(`${n} ${n === 1 ? "location" : "locations"}`, where);
  }

  function showHeatTip(spot) {
    if (!heatTip) {
      heatTip = L.tooltip({ direction: "top", offset: [0, -6], opacity: 1,
                            className: "map-tip" });
    }
    heatTip.setLatLng([Number(spot.lat), Number(spot.lng)]).setContent(spotWords(spot));
    if (!map.hasLayer(heatTip)) heatTip.addTo(map);
  }

  function hideHeatTip() {
    if (heatTip && map.hasLayer(heatTip)) map.removeLayer(heatTip);
  }

  map.on("mousemove", (e) => {
    if (!heat) return;
    const spot = nearestSpot(e.latlng);
    if (spot) showHeatTip(spot); else hideHeatTip();
  });
  map.on("mouseout", hideHeatTip);
  map.on("movestart", hideHeatTip);

  function popupFor(place) {
    const box = document.createElement("div");
    const title = document.createElement("span");
    title.className = "popup-title";
    title.textContent = place.entity || place.address || "";
    box.appendChild(title);
    if (place.address) {
      const address = document.createElement("span");
      address.className = "popup-address";
      address.textContent = place.address;
      box.appendChild(address);
    }
    if (place.type || place.entity_type) {
      const type = document.createElement("div");
      type.className = "popup-type";
      type.textContent = [place.entity_type, place.type].filter(Boolean).join(" - ");
      box.appendChild(type);
    }
    return box;
  }

  /* A FIELD DRAWN ONCE IS WRONG THE MOMENT THE MAP MOVES: `maxZoom` is the
   * zoom at which a point counts for its whole weight, so zooming out
   * without re-setting it fades the whole picture away. The Heatmap view
   * re-configures its layer on every draw; this does it on every move. */
  map.on("zoomend", () => {
    if (heat) heat.setOptions(heatSettings());
    hideHeatTip();
    // The bow is measured in pixels, so every arc is the wrong shape at the
    // new zoom. Re-pathed, not rebuilt: `setLatLngs` keeps the layer, its
    // popup and its place in the pane.
    drawnLines.forEach((line) => line.path.setLatLngs(arcPath(map, line.from, line.to, 1)));
  });

  const api = {
    available: true,
    map,

    /* One circle, or none. Leaflet takes metres. */
    setCircle(lat, lng, km) {
      if (circle) { circle.remove(); circle = null; }
      if (lat === null || lat === undefined || lng === null || lng === undefined) return;
      circle = L.circle([lat, lng], {
        radius: Math.max(0, Number(km) || 0) * 1000,
        color: "#1d4f91", weight: 2, fillColor: "#1d4f91", fillOpacity: 0.08,
      }).addTo(map);
      circle.bindPopup(`Within ${Number(km) || 0} km of ${Number(lat).toFixed(4)}, ${Number(lng).toFixed(4)}`);
    },


    /* ── The second kind of place, and the lines between them ───────────
     *
     * setMarkers draws ONE PIN PER ADDRESS, which is right when there are a
     * handful of them and the reader is looking for a particular one - the
     * Query view's map. The maps under the Diagrams charts show up to four
     * hundred, each standing for a number of rows, and four hundred pin
     * images is a slow page and a solid block of blue.
     *
     * So a point is a CIRCLE whose size says how much is there, in the
     * marker pane so it stays above the lines (see the layers above), with
     * the count in its tooltip and the names in its popup. Same data, same
     * popup shape, a different weight of drawing - which is the whole
     * difference between "find this address" and "see where this is".
     */
    setPoints(places, opts = {}) {
      markerLayer.clearLayers();
      const list = (places || []).filter(
        (p) => p && p.lat !== null && p.lat !== undefined && p.lng !== null && p.lng !== undefined);
      const most = list.reduce((n, p) => Math.max(n, Number(p.count) || 1), 1);
      const colour = opts.colour || "#1d4f91";
      /* PINS WHERE THE CALLER ASKS FOR THE MAP VIEW'S DRAWING.
       *
       * The circle below is right for a field of places - it says how much is
       * where. It is wrong where the reader has just come from the Map view
       * and is looking at the same connections: the same data drawn two ways,
       * two screens apart, is two things to learn. So the Diagrams map asks
       * for pins, out of static/js/mappin.js, which is the Map view's own. */
      if (opts.pins) {
        list.forEach((place) => {
          const marker = L.marker([place.lat, place.lng], {
            icon: pinIcon(Number(place.level) === 0),
            keyboard: true,
            title: pointTitle(place),
          });
          /* WHOSE PIN THIS IS, ON HOVER, WITHOUT OPENING ANYTHING - the
           * Map view's own bargain, on the Map view's own pin. `sticky` so
           * it follows the pointer over the whole pin, `top` so it never
           * covers the pin below it, and the 38 px offset is the pin's own
           * height. */
          marker.bindTooltip(tipForPoint(place), {
            direction: "top", offset: [0, -38], sticky: true, opacity: 1,
            className: "map-tip",
          });
          marker.bindPopup(popupForPoint(place));
          marker.addTo(markerLayer);
        });
        return;
      }
      list.forEach((place) => {
        const dot = L.circleMarker([place.lat, place.lng], {
          // 5 px for one row, 13 for the biggest - by the SQUARE ROOT of the
          // share, because a circle's area is what the eye reads as "more"
          // and its radius is not. Ten times the rows is about three times
          // the radius, which is a dot that has grown rather than a blob
          // that has swallowed the map.
          radius: 5 + 8 * Math.sqrt(Math.min(1, ((Number(place.count) || 1) - 1) / Math.max(1, most - 1))),
          pane: "markerPane",
          color: "#ffffff", weight: 1.5, opacity: 0.9,
          fillColor: place.colour || colour, fillOpacity: 0.85,
          keyboard: true,
        });
        const title = pointTitle(place);
        if (title) dot.bindTooltip(title, { direction: "top" });
        dot.bindPopup(popupForPoint(place));
        dot.addTo(markerLayer);
      });
    },

    /* One straight line per pair, in the colour its type belongs to.
     *
     * STRAIGHT, WHERE THE MAP VIEW BOWS ITS LINES. The bow is there to pull
     * a line clear of the pins it passes over and to separate two lines
     * between the same two places; on a 22 rem picture under a chart there
     * is no room for either to matter, and the arithmetic that does it
     * (static/js/map.js) is re-derived on every zoom. A reader who wants
     * that map has it one click away.
     */
    /* EVERY LINE IS AN ARC, exactly as the Map view draws it.
     *
     * A straight line covers the places that lie on it - which on this map
     * is a pin, drawn over the line and then hidden under it - and the two
     * pages showed the same pair of entities as two different shapes. The
     * curve comes from static/js/mapline.js, which the Map view uses too.
     *
     * The bow is a number of PIXELS, so it is wrong the moment the zoom
     * changes: the lines are kept and re-pathed on every zoom rather than
     * rebuilt, so an open popup stays open. */
    setLines(lines) {
      lineLayer.clearLayers();
      drawnLines = [];
      const list = (lines || []).filter((c) => c && c.drawable);
      const most = list.reduce((n, c) => Math.max(n, Number(c.count) || 1), 1);
      list.forEach((line) => {
        const share = (Number(line.count) || 1) / most;
        const path = L.polyline(arcPath(map, line.from, line.to, 1), {
          color: line.colour || "#6b7280",
          weight: 1 + Math.round(3 * share),
          opacity: 0.75,
        });
        // Wider than Leaflet's 300 px default: a sentence with two entity
        // names in it is longer than that, and it is one line by contract.
        path.bindPopup(popupForLine(line), { maxWidth: 560 });
        path.addTo(lineLayer);
        drawnLines.push({ path, from: line.from, to: line.to });
      });
    },

    setMarkers(places) {
      markerLayer.clearLayers();
      (places || []).forEach((place) => {
        if (place.lat === null || place.lat === undefined) return;
        if (place.lng === null || place.lng === undefined) return;
        const marker = L.marker([place.lat, place.lng], {
          title: place.entity || place.address || "",
          alt: `${place.entity || ""} ${place.address || ""}`.trim(),
          keyboard: true,
        });
        marker.bindPopup(popupFor(place));
        marker.addTo(markerLayer);
      });
    },

    /* THE SAME PICTURE THE HEATMAP VIEW DRAWS, from the points a tab already
     * has.
     *
     * A field of addresses is not a set of things to click: forty pins on a
     * town is a blue wall, and the question a reader asks of the Locations
     * tab - WHERE is there most of this - is exactly the question heat
     * answers and pins do not. So this tab draws the Heatmap view's layer,
     * from the same `places` the bars above it are counted from: one point
     * per address, weighted by how many rows stand on it.
     *
     * The weights are normalised HERE and not left to the layer: simpleheat
     * treats its input as 0..1 and clamps anything above, so raw counts make
     * every point maximum-red the moment one address has two rows. Divided by
     * the largest, the field says what it means - and the smallest weight is
     * held above zero so a place with one row is still visible rather than
     * being drawn as nothing at all.
     *
     * Without leaflet.heat on the page nothing is drawn and nothing breaks:
     * the caller has the bars, which are the answer; this is the picture. */
    setHeat(places, opts = {}) {
      const list = (places || []).filter(
        (p) => p && Number.isFinite(Number(p.lat)) && Number.isFinite(Number(p.lng)));
      if (heat) { heat.remove(); heat = null; }
      heatPoints = [];
      heatSpots = list;
      hideHeatTip();
      heatBounds = null;
      if (!list.length || !L.heatLayer) return;
      heatBounds = L.latLngBounds(list.map((p) => [Number(p.lat), Number(p.lng)]));
      const most = list.reduce((n, p) => Math.max(n, Number(p.count) || 1), 1);
      heatPoints = list.map((p) => [
        Number(p.lat), Number(p.lng),
        Math.max(HEAT_MIN_ALPHA, (Number(p.count) || 1) / most),
      ]);
      heatRadius = Number(opts.radius) || HEAT_RADIUS;
      heat = L.heatLayer(heatPoints, heatSettings()).addTo(map);
    },

    /* Show everything there is: the circle wins, because it is the thing
     * the person set. With neither, stay where we are. */
    fit(padding = 24) {
      const layers = [];
      markerLayer.eachLayer((l) => layers.push(l));
      if (circle) {
        map.fitBounds(circle.getBounds(), { padding: [padding, padding] });
        return;
      }
      if (layers.length === 1) {
        map.setView(layers[0].getLatLng(), 11);
        return;
      }
      if (layers.length > 1) {
        map.fitBounds(L.featureGroup(layers).getBounds(), { padding: [padding, padding] });
        return;
      }
      // A HEAT FIELD HAS NO MARKERS TO FIT TO. Without this the Locations tab
      // drew its field correctly and left the map on the whole world, so the
      // one thing the tab is about was four pixels of orange in the Atlantic.
      if (heatBounds) map.fitBounds(heatBounds, { padding: [padding, padding] });
    },

    /* Whether the wheel zooms right now - the readable way to ask, for a
     * caller or a test that would otherwise reach into Leaflet. The Query
     * tests measure the tiles' own zoom instead, because what they are
     * about is what the reader sees, not what a flag says. */
    get wheelZooms() {
      return Boolean(map.scrollWheelZoom && map.scrollWheelZoom.enabled());
    },

    /* A map drawn while its container was hidden (a dialog, a folded card)
     * believes it is 0 x 0 and shows one grey tile. This is the remedy, and
     * it has to run AFTER the container has a size. */
    invalidate() {
      map.invalidateSize();
    },

    destroy() {
      if (hint && hint.parentNode) hint.parentNode.removeChild(hint);
      /* A MAP TAKEN AWAY IN THE MIDDLE OF A ZOOM STILL GETS ITS
       * transitionend, AND LEAFLET THEN READS PANES IT HAS JUST DELETED.
       *
       * The enlarged map on Query fits itself to its markers as soon as the
       * dialog has been laid out, which is a zoom ANIMATION; closing the
       * dialog in the second that follows calls this, and the CSS
       * transition on the map pane ends afterwards. Leaflet's
       * _onZoomTransitionEnd then walks _mapPane, which remove() has
       * deleted: "Cannot read properties of undefined (reading
       * '_leaflet_pos')", thrown into a console nobody is watching.
       *
       * Both entry points to that handler are guarded by _animatingZoom, so
       * clearing it is what tells Leaflet the animation is over. stop()
       * alone does not - it cancels a pan and a flyTo, not a zoom
       * transition that is already running - so both are done, in that
       * order. */
      try { map.stop(); } catch (e) { /* nothing was moving */ }
      map._animatingZoom = false;
      map.remove();
    },
  };
  return api;
}
