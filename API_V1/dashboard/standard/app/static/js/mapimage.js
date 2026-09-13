/* ==========================================================================
 *  The map, as a file.
 *
 *  The Map and the Heatmap are the two views whose ANSWER IS THE DRAWING.
 *  Every other view can be exported as rows (CSV, JSON) or as a sheet of
 *  paper (Print, PDF); a map put into a report is a picture, and without
 *  this button the only way to get one was a screenshot of half a browser
 *  window. So: one PNG of what is on the screen, saved under the name the
 *  CSV exports use, so a folder of exports sorts together.
 *
 *  WHAT GOES INTO THE PICTURE, AND WHY EACH OF THE THREE MATTERS.
 *
 *    THE ATTRIBUTION. OpenStreetMap's terms require the credit to be shown
 *    with the map, and an image is the one case where it cannot be inferred
 *    from the page around it - the file is mailed, pasted into a slide, put
 *    on a wall, and the page it came from is not there. It is not left to
 *    html2canvas to happen to draw the 11 px control at the corner of the
 *    map: the control is taken OUT of the capture and its own text is drawn
 *    onto the picture as a footer line, in a size somebody can read. The
 *    text is READ FROM THAT CONTROL (never a constant of this file), so a
 *    deployment that points DASHBOARD_TILE_URL at another tile server and
 *    changes the credit gets the credit it set.
 *
 *    THE KEY, ON THE HEATMAP. Its field is a wash whose shades mean counts,
 *    and without the scale beside them the picture says nothing; it lives in
 *    the column BESIDE the map (map.css), so the picture is composed - the
 *    map, then the key under it, on one canvas.
 *
 *    THE MAP'S KEY IS NOT IN ITS PICTURE. It would be the larger half of
 *    the file: twelve rows of colours and counts under a picture somebody
 *    wanted for the picture. The map's colours are also the one thing the
 *    page itself says next to the drawing, so what the file leaves out is a
 *    copy, not the only copy. `legend: null` is how a caller asks for the
 *    map alone - the Heatmap passes its scale.
 *
 *    THE CAPTION. What the page says the drawing shows - the term, the
 *    project, the language, how many places and connections - is one line
 *    of text that makes the file readable a week later. A picture of pins
 *    with no words is a puzzle.
 *
 *  WHAT STAYS OUT. The zoom buttons, and anything the page marks
 *  `no-print` / `no-export`: a control means nothing in a still image.
 *
 *  WHEN THE TILES CANNOT BE DRAWN. A tile server without CORS headers
 *  taints the canvas and `toDataURL` throws - the same failure the PDF
 *  export already handles, and handled the same way here: draw again
 *  WITHOUT the tile layer and say so, rather than save a grey rectangle
 *  that looks like a broken map. The pins, the lines and the key are all
 *  still in the file; only the basemap is missing, and the sentence says
 *  which of the two happened.
 *
 *  html2canvas is vendored (static/vendor/, see VENDOR.md) and loaded only
 *  when somebody asks for an image - most sessions never do.
 * ========================================================================== */

import { exportFilename } from "./export.js";

const HTML2CANVAS = "/static/vendor/html2canvas/html2canvas.min.js";

/* Layout of the composed picture, in CSS pixels; everything is multiplied
 * by the capture scale so the file is as sharp as the screen it came from. */
const PAD = 12;
const CAPTION_SIZE = 15;
const CAPTION_LINE = 21;
const CREDIT_SIZE = 12;
const CREDIT_LINE = 18;
const INK = "#212121";
const MUTED = "#5c5c5c";
const PAPER = "#ffffff";
const FONT = 'system-ui, -apple-system, "Segoe UI", Roboto, sans-serif';

let scriptPromise = null;

function loadHtml2Canvas() {
  if (window.html2canvas) return Promise.resolve(window.html2canvas);
  if (!scriptPromise) {
    scriptPromise = new Promise((resolve, reject) => {
      const tag = document.createElement("script");
      tag.src = HTML2CANVAS;
      tag.async = true;
      tag.onload = () => resolve();
      tag.onerror = () => reject(new Error(`could not load ${HTML2CANVAS}`));
      document.head.appendChild(tag);
    }).then(() => {
      if (!window.html2canvas) throw new Error("html2canvas did not define itself");
      return window.html2canvas;
    });
    // A failed load must not be remembered as a failure for ever: the next
    // press tries again.
    scriptPromise.catch(() => { scriptPromise = null; });
  }
  return scriptPromise;
}

/* The one error that means "the tiles are foreign". Word for word the test
 * export.js makes, because it is the same failure. */
export function isTaintError(err) {
  const message = String((err && (err.message || err)) || "");
  return (err && err.name === "SecurityError") || /tainted|cross-origin|insecure/i.test(message);
}

function captureScale() {
  return Math.min(2, window.devicePixelRatio || 1);
}

/* WHAT THE CAPTURE LEAVES OUT. Controls, because a button in a picture is a
 * lie; the attribution, because it is drawn as a readable footer instead
 * (see the header); and, on the second attempt, the tiles.
 *
 * Exported because it IS the rule, and a rule is worth a test of its own:
 * tests/ui/test_map_view.py asks it about the zoom control, the attribution
 * and the tile pane rather than hunting for their pixels in a PNG. */
export function leftOut(node, ignoreTiles) {
  if (!node || !node.classList) return false;
  // THE CONNECTION LINES ARE DRAWN BY US, further down - see paintVectors.
  // Left in, html2canvas puts them in the wrong PLACE, which is worse than
  // leaving them out: a map whose lines all meet in Siberia instead of
  // California is a picture that lies rather than one that is missing
  // something. Only the <svg> is taken out; the pane also holds the
  // heatmap's <canvas>, which html2canvas copies correctly.
  if (node.tagName && node.tagName.toLowerCase() === "svg"
      && node.closest && node.closest(".leaflet-overlay-pane")) return true;
  if (node.classList.contains("leaflet-control-zoom")) return true;
  if (node.classList.contains("leaflet-control-attribution")) return true;
  // The camera itself. It is a control like the zoom buttons and it was in
  // every picture it took, in the top right corner - the one thing in the
  // file that cannot be true of the file.
  if (node.classList.contains("map-save-control")) return true;
  if (ignoreTiles && node.classList.contains("leaflet-tile-pane")) return true;
  return Boolean(node.closest && node.closest(".no-print, .no-export, .typeahead-popup"));
}

/* One <svg> as an <img>, at the size the browser gives it.
 *
 * The clone loses its `transform`, and that is the whole trick: Leaflet moves
 * its overlay pane and the svg inside it with CSS transforms, and
 * getBoundingClientRect() already reports the result of those. Drawing a
 * still-transformed copy at the transformed rectangle would apply the move
 * twice. Everything else is kept - the viewBox above all, which is what maps
 * the path coordinates into the box. */
async function vectorImage(svg, rect) {
  const clone = svg.cloneNode(true);
  clone.style.transform = "none";
  clone.style.willChange = "auto";
  clone.setAttribute("xmlns", "http://www.w3.org/2000/svg");
  clone.setAttribute("width", String(rect.width));
  clone.setAttribute("height", String(rect.height));
  const markup = new XMLSerializer().serializeToString(clone);
  const img = new Image();
  // A data URI, not a blob: the canvas this lands on must stay untainted, and
  // a same-origin blob would too - but a data URI needs no revoking and
  // cannot outlive the function.
  img.src = "data:image/svg+xml;charset=utf-8," + encodeURIComponent(markup);
  // DECODE, DO NOT JUST LOAD. `onload` on an SVG image means the markup has
  // been parsed, NOT that a raster exists: drawing on that event put ONE of
  // four hundred and fifty lines into the file and left the rest blank, which
  // is the sort of half-picture nobody would think to distrust. decode()
  // resolves when there is something to draw.
  await img.decode();
  return img;
}

/* THE VECTOR LAYERS, PUT BACK WHERE THE BROWSER HAS THEM.
 *
 * WHY THIS EXISTS. html2canvas re-implements CSS layout, and on a transformed
 * <svg> with a negative viewBox origin - which is exactly what Leaflet builds
 * - it gets the position wrong. On a map of Apple's connections every line
 * met at a point in Siberia; on screen they met on the American west coast,
 * where the pin with 44 places on it sits. The pins were right, so the
 * picture looked plausible and was wrong, which is the worst thing a saved
 * map can be.
 *
 * WHAT IS TRUSTED INSTEAD: getBoundingClientRect(), on the svg and on the
 * container, and nothing else. That pair is the browser's own answer to
 * "where is this on the screen", so the offset here is the offset a reader
 * sees - including the part of the overlay that hangs off the left edge of
 * the map, which is where the difference showed. Nothing about Leaflet's
 * zoom, its panes or its transforms is re-derived here.
 */
async function paintVectors(target, container, scale) {
  const svgs = Array.from(container.querySelectorAll(".leaflet-overlay-pane svg"));
  if (!svgs.length) return;
  const box = container.getBoundingClientRect();
  const paint = target.getContext("2d");
  // THE CANVAS COMES BACK WITH A TRANSFORM ON IT. html2canvas draws through a
  // scaled and translated context and does not put it back, so a drawImage()
  // in the caller's own coordinates lands somewhere else entirely: the lines
  // went in 380 px above where they belong, and all but the one that happened
  // to still overlap the picture fell off it. The identity matrix is what
  // "the coordinates I measured" means.
  paint.save();
  paint.setTransform(1, 0, 0, 1, 0, 0);
  for (const svg of svgs) {
    const rect = svg.getBoundingClientRect();
    if (!rect.width || !rect.height) continue;
    const img = await vectorImage(svg, rect);           // eslint-disable-line no-await-in-loop
    paint.drawImage(img,
                    (rect.left - box.left) * scale, (rect.top - box.top) * scale,
                    rect.width * scale, rect.height * scale);
  }
  paint.restore();
}

/* THE PINS, AGAIN, SO THEY END UP ON TOP.
 *
 * Leaflet paints the marker pane above the overlay pane, so on the screen a
 * pin sits over the lines that run to it. The lines are drawn by us after the
 * capture (paintVectors), which puts them over everything - and the pin every
 * line converges on is exactly the one that then disappears under two hundred
 * strokes. So the pins are captured once more and go on last.
 *
 * THE CONTAINER, NOT THE PANE. `.leaflet-marker-pane` measures 0 x 0 - its
 * children are absolutely positioned and hang out of it - and html2canvas
 * given a box of no size does not come back at all. Capturing the container
 * again keeps one origin for both canvases, so the second goes onto the first
 * at (0, 0) with nothing to compute.
 *
 * AND ITS BACKGROUND HAS TO GO for the length of the capture: html2canvas
 * paints an element's own background whatever `backgroundColor: null` says,
 * and `.leaflet-container` has one - a grey sheet that went straight over the
 * tiles and the lines underneath. It is put back in a `finally`, so a capture
 * that throws does not leave the map a different colour.
 */
async function paintPins(target, node, scale) {
  if (!node.querySelector(".leaflet-marker-icon")) return;
  const html2canvas = await loadHtml2Canvas();
  const own = node.style.background;
  node.style.background = "transparent";
  let pins;
  try {
    pins = await html2canvas(node, {
      useCORS: true,
      allowTaint: false,
      scale,
      backgroundColor: null,
      logging: false,
      // Everything the main pass drew, minus the pins: the tiles, the lines,
      // the controls - and the popup pane, which the main pass already has.
      ignoreElements: (el) => leftOut(el, true)
        || Boolean(el.classList && (el.classList.contains("leaflet-shadow-pane")
                                    || el.classList.contains("leaflet-popup-pane"))),
    });
  } finally {
    node.style.background = own;
  }
  const paint = target.getContext("2d");
  paint.save();
  paint.setTransform(1, 0, 0, 1, 0, 0);
  paint.drawImage(pins, 0, 0);
  paint.restore();
}


async function shoot(node, { ignoreTiles, scale }) {
  const html2canvas = await loadHtml2Canvas();
  const canvas = await html2canvas(node, {
    useCORS: true,
    allowTaint: false,
    scale,
    backgroundColor: PAPER,
    logging: false,
    ignoreElements: (el) => leftOut(el, ignoreTiles),
  });
  // The lines go on after the tiles and the pins, because that is the order
  // Leaflet paints them in: the overlay pane sits above the tile pane and
  // below the markers... and above them here, which is the one difference and
  // the harmless one - a line crossing a pin is a line, and a pin hidden
  // under thirty of them would be a pin nobody could find in either version.
  await paintVectors(canvas, node, scale);
  await paintPins(canvas, node, scale);
  // toDataURL is where a tainted canvas throws. Do it here, on one pixel,
  // so the retry happens before anything is composed.
  const probe = document.createElement("canvas");
  probe.width = 1;
  probe.height = 1;
  probe.getContext("2d").drawImage(canvas, 0, 0, 1, 1, 0, 0, 1, 1);
  probe.toDataURL("image/png");
  return canvas;
}

/* One string over several lines, none wider than `width`. Returns the lines;
 * the caller decides how tall the block is. */
export function wrapLines(ctx, text, width) {
  const words = String(text || "").split(/\s+/).filter(Boolean);
  if (!words.length) return [];
  const lines = [];
  let line = words[0];
  for (let i = 1; i < words.length; i += 1) {
    const next = `${line} ${words[i]}`;
    if (ctx.measureText(next).width <= width) line = next;
    else { lines.push(line); line = words[i]; }
  }
  lines.push(line);
  return lines;
}

/* ── The picture ───────────────────────────────────────────────────────── */

/* Compose the parts onto one canvas: caption, map, key, credit.
 *
 * `parts` are canvases already drawn at `scale`; the text is drawn here at
 * the same scale, so nothing in the file is softer than anything else. */
function compose({ map, legend, caption, credit, scale }) {
  const width = Math.max(map.width, legend ? legend.width : 0);
  const inner = width - 2 * PAD * scale;

  // Measured on a scratch context: setting a canvas's size resets its own
  // context, so the lines are counted before the real one is sized.
  const ruler = document.createElement("canvas").getContext("2d");
  ruler.font = `${CAPTION_SIZE * scale}px ${FONT}`;
  const captionLines = wrapLines(ruler, caption, inner);
  ruler.font = `${CREDIT_SIZE * scale}px ${FONT}`;
  const creditLines = wrapLines(ruler, credit, inner);

  const canvas = document.createElement("canvas");

  const head = captionLines.length
    ? PAD * scale + captionLines.length * CAPTION_LINE * scale
    : 0;
  const foot = creditLines.length
    ? creditLines.length * CREDIT_LINE * scale + PAD * scale
    : 0;
  canvas.width = width;
  canvas.height = head + map.height + (legend ? legend.height : 0) + foot;

  const paint = canvas.getContext("2d");
  paint.fillStyle = PAPER;
  paint.fillRect(0, 0, canvas.width, canvas.height);

  let y = 0;
  if (captionLines.length) {
    paint.fillStyle = INK;
    paint.font = `${CAPTION_SIZE * scale}px ${FONT}`;
    paint.textBaseline = "top";
    y = (PAD / 2) * scale;
    captionLines.forEach((line) => {
      paint.fillText(line, PAD * scale, y);
      y += CAPTION_LINE * scale;
    });
    y = head;
  }
  paint.drawImage(map, 0, y);
  y += map.height;
  if (legend) {
    paint.drawImage(legend, 0, y);
    y += legend.height;
  }
  if (creditLines.length) {
    // THE CREDIT, AS TEXT IN THE PICTURE. Dark on white rather than the
    // 11 px grey-on-translucent the map itself shows: this is the copy that
    // has to survive being pasted into a slide.
    paint.fillStyle = MUTED;
    paint.font = `${CREDIT_SIZE * scale}px ${FONT}`;
    paint.textBaseline = "top";
    y += (PAD / 2) * scale;
    creditLines.forEach((line) => {
      paint.fillText(line, PAD * scale, y);
      y += CREDIT_LINE * scale;
    });
  }
  return canvas;
}

function download(canvas, name) {
  return new Promise((resolve, reject) => {
    canvas.toBlob((blob) => {
      if (!blob) { reject(new Error("the picture could not be encoded")); return; }
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = name;
      // Firefox needs the link in the document before it is clicked.
      link.style.display = "none";
      document.body.appendChild(link);
      link.click();
      // The blob is held by the download; a moment is enough for the
      // browser to take it, and keeping it would leak the whole bitmap.
      window.setTimeout(() => {
        URL.revokeObjectURL(url);
        link.remove();
      }, 4000);
      resolve(blob);
    }, "image/png");
  });
}

/* The credit the picture must carry, from the map's own attribution control
 * - never a constant here, so a deployment that changed the tile server and
 * its credit gets the credit it set. `fallback` is for a page whose Leaflet
 * did not load at all. */
export function creditOf(container, fallback) {
  const control = container && container.querySelector(".leaflet-control-attribution");
  const said = control ? (control.textContent || "").replace(/\s+/g, " ").trim() : "";
  return said || fallback || "";
}

/**
 * Save the map as a PNG.
 *
 *   container  the Leaflet container (#map / #heat-map)
 *   legend     the element holding the key, or null
 *   caption    one line of words: what the drawing shows
 *   credit     fallback attribution when the control is not there
 *   view       "map" / "heatmap" - the name of the file, as the CSV uses it
 *
 * Returns {name, width, height, tiles, legend} - `tiles` is false when the
 * basemap had to be left out, which the caller must SAY.
 */
export async function saveMapImage({ container, legend, caption, credit, view }) {
  if (!container) throw new Error("there is no map to save");
  const scale = captureScale();
  const words = creditOf(container, credit);
  let tiles = true;
  let mapCanvas;
  try {
    mapCanvas = await shoot(container, { ignoreTiles: false, scale });
  } catch (err) {
    if (!isTaintError(err)) throw err;
    tiles = false;
    mapCanvas = await shoot(container, { ignoreTiles: true, scale });
  }
  let legendCanvas = null;
  if (legend && legend.getBoundingClientRect().height > 0) {
    legendCanvas = await shoot(legend, { ignoreTiles: false, scale });
  }
  const canvas = compose({
    map: mapCanvas, legend: legendCanvas, caption,
    credit: words, scale,
  });
  const name = exportFilename(view || "map", "png");
  await download(canvas, name);
  return {
    name,
    width: canvas.width,
    height: canvas.height,
    tiles,
    legend: Boolean(legendCanvas),
    credit: words,
  };
}


/* ── THE CAMERA IN THE CORNER OF THE MAP ──────────────────────────────────
 *
 * A Leaflet control at the top right, beside the zoom buttons, because that
 * is where a map's own controls are and because the answer of these two
 * views IS the drawing: the way to take it away belongs on it, not in a row
 * of form controls under it.
 *
 * It replaces the "Save image" button that stood in the query row. One
 * control, one place - and the row under the fields is left holding only
 * the two things that ask a question (Search, Clear), which is what it is
 * on every other view.
 *
 * The glyph is the same path as templates/_macros.html: icon("image"). It
 * is repeated here rather than shared because this is built in the browser
 * from Leaflet's own DOM helpers and there is no template to render.
 */
const IMAGE_ICON =
  '<svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true" fill="none" '
  + 'stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">'
  + '<path d="M4 5h16v14H4zM4 15l4-4 3 3 4-5 5 6"/></svg>';

export function addSaveControl(L, map, { label, run }) {
  if (!L || !map || !L.Control) return null;
  const Save = L.Control.extend({
    options: { position: "topright" },
    onAdd() {
      const bar = L.DomUtil.create("div", "leaflet-bar map-save-control");
      const button = L.DomUtil.create("a", "", bar);
      button.href = "#";
      button.setAttribute("role", "button");
      // Both, and for two different readers: the tooltip for a pointer, the
      // label for a screen reader. An icon with neither is a mystery.
      button.title = label;
      button.setAttribute("aria-label", label);
      button.innerHTML = IMAGE_ICON;
      L.DomEvent.on(button, "click", (event) => {
        L.DomEvent.stop(event);        // not a link, and not a map click
        run();
      });
      L.DomEvent.disableClickPropagation(bar);
      return bar;
    },
  });
  const control = new Save();
  map.addControl(control);
  return control;
}
