// The tool catalog, and the little drawings the swatches wear.
//
// A slot is plain data -- { kind, type?, dir, flip } -- so it persists as
// JSON and the toolbar never holds anything a reload cannot rebuild. Kind
// strings end up in localStorage, so treat them as a compatibility surface:
// renaming one silently empties a player's slot.
//
// Machines come from types.json, so a new machine in core/entity.py appears
// in the picker with no change here.

import { isValidStamp, typeByName } from "./stamp.js";
import { structGlyph } from "./glyphs.js";

export const CW = (d) => (d + 3) % 4;    // directions are CCW quarter turns
export const CCW = (d) => (d + 1) % 4;

const css = (c) => `rgb(${(c[0] * 255) | 0},${(c[1] * 255) | 0},${(c[2] * 255) | 0})`;

// Every tool a slot can hold, in picker order.
export function catalog(T) {
  const out = [
    { kind: "belt", label: "Belt" },
    { kind: "rawbelt", label: "Raw belt" },
    { kind: "under", label: "Underground" },
    // Text labels (notes.js): not an entity, never on the grid.
    { kind: "label", label: "Label" },
  ];
  for (const td of Object.values(T.types)) {
    if (td.kind !== "structure") continue;
    out.push({ kind: "structure", type: td.name, label: td.class });
  }
  return out;
}

export function sameTool(a, b) {
  if (a.kind !== b.kind) return false;
  if (a.kind === "stamp") return a.stamp === b.stamp;
  return a.kind !== "structure" || a.type === b.type;
}

export function labelFor(T, slot) {
  if (slot.kind === "stamp") return slot.name || "Stamp";
  const hit = catalog(T).find(c => sameTool(c, slot));
  return hit ? hit.label : "?";
}

// Does this slot still name something this build has? An entry naming a
// machine that no longer exists costs that slot, not the whole bar.
export function isKnown(T, slot) {
  if (slot && slot.kind === "stamp") return isValidStamp(T, slot.stamp);
  return !!slot && catalog(T).some(c => sameTool(c, slot));
}

function typeOf(T, name) {
  return Object.values(T.types).find(t => t.name === name);
}

// The number a placement carries. A Source always emits something and
// starts at 0 (Source.EMIT_VALUE); a raw belt starts with no seed (null).
export const VALUE_CHOICES = [0, 1, -1, 2, 4, 8];

export function hasValue(slot) {
  return !!slot && (slot.kind === "rawbelt" || (slot.kind === "structure" && slot.type === "source"));
}

export function toolValue(slot) {
  if (!hasValue(slot)) return null;
  if (slot.kind === "rawbelt") return slot.value ?? null;
  return slot.value ?? 0;
}

// Whether flip means anything for this tool. Belts have no ports; a machine
// that mirrors onto itself (Sink) is achiral. The underground uses flip for
// "which end does the next tap lay", as on the desktop.
export function canFlip(T, slot) {
  if (slot.kind === "under" || slot.kind === "stamp") return true;
  if (slot.kind !== "structure") return false;
  const td = typeOf(T, slot.type);
  return !!(td && td.chiral);
}

// --- swatch art -------------------------------------------------------------
// Drawn in world units with +Y up (the group flips SVG's y), from the same
// placed port tables the board renderer uses, so a swatch shows exactly the
// orientation the next placement would have.

const STEP = [[1, 0], [0, 1], [-1, 0], [0, -1]];

function chevron(cx, cy, d, s) {
  const [fx, fy] = STEP[d], sx = -fy, sy = fx;
  return `M${cx - fx * s + sx * s} ${cy - fy * s + sy * s}` +
         `L${cx + fx * s * 0.6} ${cy + fy * s * 0.6}` +
         `L${cx - fx * s - sx * s} ${cy - fy * s - sy * s}`;
}

function port(x, y, d, inward) {
  const [fx, fy] = STEP[d];
  const cx = x + 0.5, cy = y + 0.5;
  const ex = cx + fx * 0.45, ey = cy + fy * 0.45;
  const sx = -fy * 0.127, sy = fx * 0.127, k = 0.17;   // the board's ports, at swatch scale
  const tip = inward ? [ex - fx * k, ey - fy * k] : [ex, ey];
  const base = inward ? [ex, ey] : [ex - fx * k, ey - fy * k];
  return `M${base[0] + sx} ${base[1] + sy}L${tip[0]} ${tip[1]}L${base[0] - sx} ${base[1] - sy}Z`;
}

function frame(w, h, body) {
  const m = Math.max(w, h) * 0.12 + 0.1;
  const side = Math.max(w, h) + 2 * m;
  const ox = (side - w) / 2, oy = (side - h) / 2;
  return `<svg viewBox="0 0 ${side} ${side}" aria-hidden="true">` +
    `<g transform="translate(${ox} ${side - oy}) scale(1 -1)">${body}</g></svg>`;
}

export function toolArt(T, slot) {
  if (slot.kind === "stamp") return stampArt(T, slot.stamp);
  if (slot.kind === "label") {
    // A label: a yellow box with a T in it, as the board draws one.
    return `<svg viewBox="0 0 24 24" aria-hidden="true">` +
      `<rect x="3" y="5" width="18" height="14" rx="1.5" fill="none" stroke="#ffd933" stroke-width="1.4" opacity="0.6"/>` +
      `<path d="M8 9h8M12 9v7" stroke="#ffd933" stroke-width="2" stroke-linecap="round" fill="none"/></svg>`;
  }
  const d = slot.dir | 0;
  if (slot.kind === "belt" || slot.kind === "rawbelt") {
    const [fx, fy] = STEP[d];
    const spine = `M${0.5 - fx * 0.5} ${0.5 - fy * 0.5}L${0.5 + fx * 0.5} ${0.5 + fy * 0.5}`;
    const box = slot.kind === "rawbelt"
      ? `<rect x="0.04" y="0.04" width="0.92" height="0.92" fill="none" stroke="#bdc2d1" stroke-width="0.05" stroke-dasharray="0.1 0.08"/>` : "";
    return frame(1, 1, box +
      `<path d="${spine}" stroke="#757885" stroke-width="0.34"/>` +
      `<path d="${chevron(0.5, 0.5, d, 0.14)}" fill="none" stroke="#bdc2d1" stroke-width="0.08"/>`);
  }
  if (slot.kind === "under") {
    // The dark bar is the buried side: the front of an entrance, the back
    // of an exit. Flip on the tool swaps which end the next tap lays.
    // Narrowing toward the buried side, as on the board (render.js).
    const [fx, fy] = STEP[d], s = slot.flip ? -1 : 1;
    const ax = fx * s, ay = fy * s, sx = -ay, sy = ax;
    const pt = (al, ac) => `${0.5 + ax * al + sx * ac} ${0.5 + ay * al + sy * ac}`;
    const body = `M${pt(-0.38, 0.38)}L${pt(0.38, 0.2)}L${pt(0.38, -0.2)}L${pt(-0.38, -0.38)}Z`;
    const bar = `M${pt(0.22, 0.23)}L${pt(0.38, 0.2)}L${pt(0.38, -0.2)}L${pt(0.22, -0.23)}Z`;
    return frame(1, 1,
      `<path d="${body}" fill="#6b7380"/><path d="${bar}" fill="#3c414b"/>` +
      `<path d="${chevron(0.5, 0.5, d, 0.14)}" fill="none" stroke="#bdc2d1" stroke-width="0.08"/>`);
  }
  const td = typeOf(T, slot.type);
  if (!td) return "";
  const o = td.orient[`${d}${slot.flip ? 1 : 0}`];
  const c = td.colour || [0.5, 0.5, 0.5];
  const ins = o.inputs.map(([x, y, f]) => port(x, y, f, true)).join("");
  const outs = o.outputs.map(([x, y, f]) => port(x, y, f, false)).join("");
  // The control overlay and the glyph, as on the board.
  const pass = o.pass.map(([x, y, a]) => {
    const [fx, fy] = STEP[a];
    return `M${x + 0.5 - fx * 0.5} ${y + 0.5 - fy * 0.5}L${x + 0.5 + fx * 0.5} ${y + 0.5 + fy * 0.5}`;
  }).join("");
  const g = structGlyph(td.glyph, 0, 0, o.w, o.h);
  return frame(o.w, o.h,
    `<rect x="0.05" y="0.05" width="${o.w - 0.1}" height="${o.h - 0.1}" fill="${css(c)}" ` +
    `stroke="${css(c.map(v => v * 0.6))}" stroke-width="0.06"/>` +
    (pass ? `<path d="${pass}" fill="none" stroke="#000" stroke-width="0.34" stroke-linecap="butt"/>` +
            `<path d="${pass}" fill="none" stroke="#757885" stroke-width="0.28" stroke-linecap="butt"/>` : "") +
    (g.stroke ? `<path d="${g.stroke}" fill="none" stroke="#e0e6f0" stroke-width="${g.width}" stroke-linecap="round" stroke-linejoin="round"/>` : "") +
    (g.fill ? `<path d="${g.fill}" fill="#e0e6f0"/>` : "") +
    `<path d="${ins}" fill="#5999f2" stroke="rgba(0,0,0,0.5)" stroke-width="0.025"/>` +
    `<path d="${outs}" fill="#f2594d" stroke="rgba(0,0,0,0.5)" stroke-width="0.025"/>`);
}

// A stamp drawn small: machines as their coloured bodies, belts as their
// spines, underground ends as dark squares. Enough to tell two stamps apart
// and to see which way one is turned.
export function stampArt(T, stamp) {
  if (!stamp || !stamp.entries) return "";
  let body = "";
  for (const e of stamp.entries) {
    const t = typeByName(T, e.type);
    if (!t) continue;
    const o = t.td.orient[`${e.dir}${e.flip ? 1 : 0}`];
    if (t.td.kind === "belt") {
      const [fx, fy] = STEP[e.dir];
      const cx = e.dx + 0.5, cy = e.dy + 0.5;
      body += `<path d="M${cx - fx * 0.5} ${cy - fy * 0.5}L${cx + fx * 0.5} ${cy + fy * 0.5}" stroke="#757885" stroke-width="0.4"/>`;
    } else if (t.td.kind === "under") {
      body += `<rect x="${e.dx + 0.15}" y="${e.dy + 0.15}" width="0.7" height="0.7" fill="#6b7380"/>`;
    } else {
      const c = t.td.colour || [0.5, 0.5, 0.5];
      body += `<rect x="${e.dx + 0.06}" y="${e.dy + 0.06}" width="${o.w - 0.12}" height="${o.h - 0.12}" fill="${css(c)}"/>`;
    }
  }
  return frame(Math.max(1, stamp.w), Math.max(1, stamp.h), body);
}
