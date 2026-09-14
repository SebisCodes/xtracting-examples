/* The curve a connection is drawn as, in one place.
 *
 * WHY THIS FILE EXISTS. The Map view bows every line - "every one of them is
 * an ARC and not a chord, because a straight line covers the places that lie
 * on it" - and the small map under a Diagrams tab drew chords, so the same
 * two entities were joined by two different shapes on two pages, and on the
 * small one the pin in the middle was hidden under the line. The request was
 * for the Diagrams map to render exactly like the Map view, and the honest
 * way to make two drawings the same is to have one drawing.
 *
 * Everything is worked out in the map's PROJECTED pixels at the current zoom
 * (`project`/`unproject`), which is what makes the bow a number of pixels
 * rather than a number of degrees: a curve measured in degrees is a hairline
 * at one zoom and a semicircle at the next.
 */

export const ARC_STEPS = 40;        // segments; the curve is shallow, so this is smooth
export const ARC_BOW_RATIO = 0.16;  // of the chord on screen
// A pin is 32 x 44 px and hangs above its point, so a curve that passes 22 px
// BELOW the point clears the whole icon with room to spare - and 22 px is
// still a gentle bow on any line long enough to have a place sitting on it.
export const ARC_BOW_MIN = 22;      // px
export const ARC_BOW_MAX = 150;     // px - a long line is a bow, not a circle

/* The fan step of the i-th line between one pair of places: 1, -1, 2, -2 …
 * Two entities at one address, each connected to the same third place, drew
 * two lines on exactly the same pixels: one popup was unreachable. */
export function fanFactor(i) {
  const rank = Math.floor(i / 2) + 1;
  return i % 2 === 0 ? rank : -rank;
}

/** The curve between two points, as latitudes and longitudes Leaflet draws. */
export function arcPath(map, from, to, factor = 1) {
  const straight = [[from.lat, from.lng], [to.lat, to.lng]];
  // Before the map exists there is no projection to bow in, and a straight
  // pair of ends is a shape Leaflet can draw either way.
  if (!map) return straight;
  const zoom = map.getZoom();
  const a = map.project([from.lat, from.lng], zoom);
  const b = map.project([to.lat, to.lng], zoom);
  const dx = b.x - a.x;
  const dy = b.y - a.y;
  const chord = Math.hypot(dx, dy);
  // Two ends on one pixel have no direction to bow in, and no pin between
  // them either.
  if (!(chord > 1)) return straight;
  const bow = Math.min(ARC_BOW_MAX, Math.max(ARC_BOW_MIN, chord * ARC_BOW_RATIO)) * factor;
  // The perpendicular that points DOWN the screen. Leaflet's projected y
  // grows southwards, so this is the one with a positive y.
  const sign = dx >= 0 ? 1 : -1;
  const nx = (-dy / chord) * sign;
  const ny = (dx / chord) * sign;
  // 2 x bow: a quadratic Bezier reaches half of it at the middle.
  const cx = (a.x + b.x) / 2 + nx * bow * 2;
  const cy = (a.y + b.y) / 2 + ny * bow * 2;
  const points = [];
  for (let i = 0; i <= ARC_STEPS; i += 1) {
    const t = i / ARC_STEPS;
    const u = 1 - t;
    const x = u * u * a.x + 2 * u * t * cx + t * t * b.x;
    const y = u * u * a.y + 2 * u * t * cy + t * t * b.y;
    const ll = map.unproject([x, y], zoom);
    points.push([ll.lat, ll.lng]);
  }
  return points;
}
