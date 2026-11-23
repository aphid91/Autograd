// Pixel: a web-only machine that draws. It takes over Sink's type id, so
// every Sink in a save IS a Pixel here, with no conversion pass -- same 2x2
// footprint, same look, same row layout. The desktop is reference material
// and still reads the id as a Sink.
//
// Four input ports, all needed to fire (FIRE_ALL), no outputs. Authored at
// facing +X, cell 0 the bottom-left:
//
//          Z
//     [ ][ ]
//  S →[ ][ ]
//  H →[0][ ]
//      ↑
//      V
//
// H, S and V set the hue, saturation and value of the square it projects;
// Z its size. A V above 1 is a character instead (see pixelOf()). While View > pixel overlay is above 0, each fire records them
// on the Screen; at 0 a fire does nothing beyond consuming its inputs.

export const PIXEL_TYPE = 6;   // Sink's id

// [lx, ly, face] at facing +X, unflipped; port index = channel (H S V Z).
// Faces: 0 +X, 1 +Y, 2 -X, 3 -Y.
const AUTHORED = [[0, 0, 2], [0, 1, 2], [0, 0, 3], [1, 1, 1]];
export const CH = { H: 0, S: 1, V: 2, Z: 3 };

// Entity.place_local() and the face half of faces_at_origin(): flip about
// the authored X axis first (ly -> h-1-ly, +Y <-> -Y), then rotate CCW by
// `d` quarter turns and shift back into the +X/+Y quadrant.
export function placePorts([w, h], ports, d, flipped) {
  const rot = (x, y) => { for (let i = 0; i < d; i++) [x, y] = [-y, x]; return [x, y]; };
  const corners = [[0, 0], [w - 1, 0], [0, h - 1], [w - 1, h - 1]].map(([x, y]) => rot(x, y));
  const mx = Math.min(...corners.map(c => c[0])), my = Math.min(...corners.map(c => c[1]));
  return ports.map(([lx, ly, f]) => {
    if (flipped) { ly = h - 1 - ly; if (f === 1) f = 3; else if (f === 3) f = 1; }
    const [rx, ry] = rot(lx, ly);
    return [rx - mx, ry - my, (f + d) % 4];
  });
}

// Replace Sink in a loaded types.json with Pixel. Call once, right after
// loading it. Idempotent. Old toolbar slots and stamps naming "sink" find
// the Pixel through T.aliases (stamp.js typeByName).
export function installPixel(T) {
  const sink = T.types[PIXEL_TYPE];
  if (!sink || sink.class === "Pixel") return T;
  const fp = sink.footprint, orient = {};
  for (let d = 0; d < 4; d++) {
    for (const fl of [0, 1]) {
      const [w, h] = d % 2 ? [fp[1], fp[0]] : fp;
      orient[`${d}${fl}`] = { w, h, inputs: placePorts(fp, AUTHORED, d, !!fl), outputs: [], pass: [] };
    }
  }
  T.types[PIXEL_TYPE] = {
    ...sink, name: "pixel", class: "Pixel", chiral: true, orient,
    sim: { fire_rule: T.sim.FIRE_ALL, in_ch: [0, 1, 2, 3], out_ch: [], n_in: 4, n_out: 0, ops: [], select: null },
  };
  T.aliases = { ...(T.aliases || {}), sink: "pixel" };
  return T;
}

// A fire's four values as a colour and a size: hue in turns, wrapping;
// saturation clamped to 0..1; NaN reads as 0. z is kept raw (the size is
// worked out when drawn, against the View's max).
//
// V is the value (brightness), clamped, while it is 1 or less. Strictly
// above 1 it is a character: floor(V) an ASCII code, and its fractional
// part the value -- except that a fractional part of exactly 0 means full
// value, so 55 is a full-brightness '7' and 55.5 a half-brightness one.
// The printable codes 33..126 draw their character and 32 an empty space;
// any other code draws the ordinary square, at the same fractional value.
// `ch` is the character to draw, or null for the square.
export function pixelOf(values) {
  const n = (x) => (x === null || x === undefined || x !== x ? 0 : x);
  const h = n(values[CH.H]), s = n(values[CH.S]), raw = n(values[CH.V]);
  let v = Math.min(1, Math.max(0, raw)), ch = null;
  if (raw > 1) {
    const code = Math.floor(raw), f = raw - code;
    v = f === 0 ? 1 : f;
    if (code >= 32 && code <= 126) ch = String.fromCharCode(code);
  }
  return {
    h: ((h % 1) + 1) % 1,
    s: Math.min(1, Math.max(0, s)),
    v, ch,
    z: n(values[CH.Z]),
  };
}

// Half the side of the drawn square, in cells: 0 for none. Z = 1 covers the
// machine exactly (2x2 cells); each whole step adds a cell all round.
export function pixelSize(z, max) {
  if (!(z > 0)) return 0;
  return Math.min(Math.max(1, Math.round(z)), Math.max(1, max));
}

export function hsvToRgb(h, s, v) {
  const i = Math.floor(h * 6) % 6, f = h * 6 - Math.floor(h * 6);
  const p = v * (1 - s), q = v * (1 - f * s), t = v * (1 - (1 - f) * s);
  return [[v, t, p], [q, v, p], [p, v, t], [p, q, v], [t, p, v], [v, p, q]][i];
}

// What every Pixel last showed. A Map in fire order: a fire re-inserts its
// row, so iterating draws the most recent fire on top where squares
// overlap. Cleared by the sim on any rebuild or Reset, and by main.js when
// the overlay is switched on.
export class Screen {
  constructor() { this.px = new Map(); this.rev = 0; }
  clear() { if (this.px.size) { this.px.clear(); this.rev++; } }
  fire(row, values) {
    const p = pixelOf(values);
    const [r, g, b] = hsvToRgb(p.h, p.s, p.v);
    p.css = `rgb(${Math.round(r * 255)},${Math.round(g * 255)},${Math.round(b * 255)})`;
    this.px.delete(row);
    this.px.set(row, p);
    this.rev++;
  }
}

// Draw the screen in world space (the renderer's transform): each Pixel's
// square centred on its footprint's centre, a grid corner, `alpha` opaque.
// A character pixel draws its glyph in that square instead -- the font's
// full cell (ascent + descent) as tall as the square, centred -- over a
// black square when `textBg` is set, and over nothing otherwise. A space
// draws only that background.
export function drawPixels(ctx, board, screen, alpha, max, on, textBg = false) {
  if (!screen?.px.size || !(alpha > 0)) return;
  const E = board.T.entity;
  ctx.globalAlpha = Math.min(1, alpha);
  for (const [row, p] of screen.px) {
    const k = pixelSize(p.z, max);
    if (!k || !board.alive(row)) continue;
    const cx = board.field(row, E.F_ORIGIN_X) + board.field(row, E.F_W) / 2;
    const cy = board.field(row, E.F_ORIGIN_Y) + board.field(row, E.F_H) / 2;
    if (!on(cx - k, cy - k, 2 * k, 2 * k)) continue;
    if (p.ch === null || p.ch === undefined) {
      ctx.fillStyle = p.css;
      ctx.fillRect(cx - k, cy - k, 2 * k, 2 * k);
      continue;
    }
    if (textBg) {
      ctx.fillStyle = "#000";
      ctx.fillRect(cx - k, cy - k, 2 * k, 2 * k);
    }
    if (p.ch !== " ") drawGlyph(ctx, p.ch, p.css, cx, cy, 2 * k);
  }
  ctx.globalAlpha = 1;
}

// The glyph font is set at a fixed size and scaled into place: canvas text
// at a size of a few world units is quantised (or floored at a minimum
// size) by some browsers, and the scale keeps it exact at any zoom.
const GLYPH_PX = 100;
const GLYPH_FONT = `${GLYPH_PX}px ui-monospace, Menlo, Consolas, "DejaVu Sans Mono", monospace`;
let glyphCell = null;   // { asc, desc } of GLYPH_FONT, measured once

function drawGlyph(ctx, ch, css, cx, cy, side) {
  ctx.save();
  ctx.font = GLYPH_FONT;
  if (!glyphCell) {
    const m = ctx.measureText("M");
    const asc = m.fontBoundingBoxAscent, desc = m.fontBoundingBoxDescent;
    glyphCell = asc > 0 && desc >= 0 ? { asc, desc } : { asc: 0.8 * GLYPH_PX, desc: 0.2 * GLYPH_PX };
  }
  const { asc, desc } = glyphCell, sc = side / (asc + desc);
  // The world transform has y up; text wants y down, so flip it locally.
  ctx.translate(cx, cy);
  ctx.scale(sc, -sc);
  ctx.textAlign = "center";
  ctx.textBaseline = "alphabetic";
  ctx.fillStyle = css;
  ctx.fillText(ch, 0, (asc - desc) / 2);
  ctx.restore();
}
