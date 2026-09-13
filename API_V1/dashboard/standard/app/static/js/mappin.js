/* The pin every map in this product drops, in one place.
 *
 * WHY THIS FILE EXISTS. The Map view drew a pin and the small map under a
 * Diagrams tab drew a circle, and a reader who had learnt one had to learn the
 * other - on the same data, about the same places, two screens apart. The
 * request was to make the Diagrams map "exactly like the Connections view",
 * and the honest way to make two drawings the same is to have one drawing.
 *
 * A divIcon and not an image, so the states are CSS classes (css/map.css) and
 * the colours come from the page's own tokens rather than from a PNG somebody
 * would have to redraw for the dark theme.
 *
 * THE ANCHOR IS THE POINT OF THE PIN, not the centre of its box: 32 x 44 with
 * the anchor at (16, 44) puts the tip on the archive's own coordinate. A
 * circle centred there would be wrong by its own radius, with the thing it
 * marks hidden underneath it. 44 px tall is also a 44 px target, which is
 * what a map's primary object should be (WCAG 2.5.8 asks for 24).
 */

export const PIN_SVG = '<svg viewBox="0 0 32 44" aria-hidden="true" focusable="false">'
  + '<path class="pin-body" d="M16 43C16 43 29 26.5 29 16A13 13 0 1 0 3 16C3 26.5 16 43 16 43Z"/>'
  + '<circle class="pin-eye" cx="16" cy="16" r="5"/></svg>';

/** The icon for one pin. `searched` is the thing the reader asked for. */
export function pinIcon(searched) {
  return window.L.divIcon({
    className: `map-pin${searched ? " map-pin--searched" : ""}`,
    html: PIN_SVG,
    iconSize: [32, 44],
    iconAnchor: [16, 44],
    popupAnchor: [0, -40],
  });
}

export default pinIcon;
