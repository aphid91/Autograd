// The machine glyphs (Shear's +, Swap's Z, ...), as SVG path data in world
// units with +Y up. Ports of the sd_* functions in shaders/world.frag, so
// the two apps draw the same marks. Path data rather than canvas calls so
// one definition serves the board (new Path2D(d)) and the swatches (<path>).
//
// The desktop draws each mark as a distance field of segments minus a
// half-thickness `t`; here that is a stroke of width 2t with round caps,
// which is the same shape. Dots and the obelus's dots are fills.

// MARK_RADIUS and MARK_THICK in world.frag, per unit of the footprint's
// shorter side.
export const MARK_RADIUS = 0.26;
export const MARK_THICK = 0.045;

const seg = (x0, y0, x1, y1) => `M${x0} ${y0}L${x1} ${y1}`;
const circle = (cx, cy, r) =>
  `M${cx + r} ${cy}A${r} ${r} 0 1 0 ${cx - r} ${cy}A${r} ${r} 0 1 0 ${cx + r} ${cy}Z`;

// { stroke, fill } path data for glyph `name` centred on (cx, cy), with arm
// reach `r` and half-thickness `t`. The stroke is drawn 2t wide.
export function glyphPaths(name, cx, cy, r, t) {
  const S = (x0, y0, x1, y1) => seg(cx + x0, cy + y0, cx + x1, cy + y1);
  switch (name) {
    case "plus":
      return { stroke: S(-r, 0, r, 0) + S(0, -r, 0, r), fill: "" };
    case "cross": {
      const a = r * Math.SQRT1_2;
      return { stroke: S(-a, -a, a, a) + S(-a, a, a, -a), fill: "" };
    }
    case "zed":
      return { stroke: S(-r, r, r, r) + S(r, r, -r, -r) + S(-r, -r, r, -r), fill: "" };
    case "dot":
      return { stroke: "", fill: circle(cx, cy, Math.max(t * 2, r * 0.35)) };
    case "divide": {
      const gap = r * 0.55;
      return { stroke: S(-r, 0, r, 0),
               fill: circle(cx, cy + gap, t * 1.5) + circle(cx, cy - gap, t * 1.5) };
    }
    case "chevrons": {
      const s = r * 0.55;
      const chev = (ox) => S(ox - s, s, ox + s, 0) + S(ox + s, 0, ox - s, -s);
      return { stroke: chev(-s * 0.9) + chev(s * 0.9), fill: "" };
    }
    case "arrow_down":
      return { stroke: S(0, r, 0, -r) + S(0, -r, r * 0.6, -r * 0.4) + S(0, -r, -r * 0.6, -r * 0.4), fill: "" };
    case "wye":
    case "wye_down": {
      const k = name === "wye_down" ? -1 : 1;
      return { stroke: S(-r, k * r, 0, 0) + S(r, k * r, 0, 0) + S(0, 0, 0, -k * r), fill: "" };
    }
    case "keys": {
      // The keyboard (keyboard.js): two rows of keys and a space bar.
      const k = r * 0.36, g = r * 0.14, cols = 5;
      const w0 = cols * k + (cols - 1) * g, x0 = cx - w0 / 2;
      const rect = (x, y, w, h) => `M${x} ${y}h${w}v${h}h${-w}Z`;
      let fill = "";
      for (let row = 0; row < 2; row++) {
        const y = cy + r * 0.5 - row * (k + g);
        for (let c = 0; c < cols; c++) fill += rect(x0 + c * (k + g), y - k / 2, k, k);
      }
      fill += rect(x0 + (k + g), cy + r * 0.5 - 2 * (k + g) - k / 2, 3 * k + 2 * g, k);
      return { stroke: "", fill };
    }
    default:
      return { stroke: "", fill: "" };
  }
}

// The glyph for a structure covering w x h cells at (x, y): scaled to the
// footprint's shorter side, as the shader does, so it stays inside a 2x1.
export function structGlyph(name, x, y, w, h, scale = 1, weight = 1) {
  const s = Math.min(w, h);
  const t = MARK_THICK * s * weight;
  return { ...glyphPaths(name, x + w / 2, y + h / 2, MARK_RADIUS * s * scale, t), width: 2 * t };
}
