// Labels: the desktop's text-box annotations (core/annotations.py), read
// from and written back to the save's `annotations` blob, so a label made
// here shows there and the other way round.
//
// A record is the desktop's own:
//   { kind: "text", colour, size, text, rect: [x, y, w, h] }
// in world cells, with (x, y) the box's TOP-left -- world +Y is up, so the
// box runs from y - h to y. `size` indexes TEXT_SIZES (text height in
// cells), `colour` indexes PALETTE. Records of any other kind (the desktop's
// freehand strokes) are kept untouched and written back as they came: this
// app does not draw them, but a save passing through it must not lose them.

export const TEXT_SIZES = [0.5, 0.9, 1.6];
export const SIZE_NAMES = ["S", "M", "L"];
export const PALETTE = [
  ["yellow", [1.0, 0.85, 0.2]],
  ["red", [1.0, 0.33, 0.28]],
  ["green", [0.4, 0.95, 0.45]],
  ["cyan", [0.35, 0.85, 1.0]],
  ["white", [0.96, 0.96, 0.98]],
  ["orange", [1.0, 0.58, 0.18]],
];
export const css = (colour, a = 1) => {
  const [r, g, b] = (PALETTE[colour] || PALETTE[0])[1];
  return `rgba(${Math.round(r * 255)},${Math.round(g * 255)},${Math.round(b * 255)},${a})`;
};

// Padding inside a box, and line height, as fractions of the text size.
export const PAD = 0.25;
export const LINE = 1.2;

export function parseNotes(blob) {
  if (typeof blob !== "string" || !blob.trim()) return [];
  try {
    const v = JSON.parse(blob);
    return Array.isArray(v) ? v.filter(n => n && typeof n === "object") : [];
  } catch {
    return [];   // a corrupt blob costs the notes, never the board
  }
}

export const isText = (n) => n.kind === "text" && Array.isArray(n.rect) && n.rect.length === 4;
export const sizeOf = (n) => TEXT_SIZES[Math.max(0, Math.min(TEXT_SIZES.length - 1, n.size | 0))];

// The topmost text box containing world point (x, y), or -1. Later records
// draw over earlier ones, so the search runs backwards.
export function noteAt(notes, x, y) {
  for (let i = notes.length - 1; i >= 0; i--) {
    const n = notes[i];
    if (!isText(n)) continue;
    const [bx, by, w, h] = n.rect;
    if (x >= bx && x <= bx + w && y >= by - h && y <= by) return i;
  }
  return -1;
}

// The text's lines, wrapped to `maxW` with `measure(s)` giving a string's
// width (same units). Explicit newlines are kept; a word longer than the
// line goes on a line of its own rather than being split.
export function wrap(text, maxW, measure) {
  const out = [];
  for (const para of String(text).split("\n")) {
    const words = para.split(/\s+/).filter(Boolean);
    if (!words.length) { out.push(""); continue; }
    let line = words[0];
    for (const w of words.slice(1)) {
      if (measure(`${line} ${w}`) <= maxW) line += ` ${w}`;
      else { out.push(line); line = w; }
    }
    out.push(line);
  }
  return out;
}

// A new label's box, top-left at (x, y): room for a few words at its size.
export const MIN_W = 1, MIN_H = 0.6;
export function newRect(x, y, size) {
  const s = TEXT_SIZES[size] ?? TEXT_SIZES[1];
  const r = (v) => Math.round(v * 1000) / 1000;
  return [r(x), r(y), r(Math.max(4, 8 * s)), r((LINE + 2 * PAD) * s * 1.5)];
}

// The topmost label whose resize grip -- its bottom-right corner, a square
// `px` across in screen space -- holds screen point (sx, sy), or -1.
// `toScreen(x, y)` maps a world point to the screen.
export function gripAt(notes, toScreen, sx, sy, px) {
  for (let i = notes.length - 1; i >= 0; i--) {
    const n = notes[i];
    if (!isText(n)) continue;
    const [x, y, w, h] = n.rect;
    const [cx, cy] = toScreen(x + w, y - h);
    if (sx >= cx - px && sx <= cx + px * 0.4 && sy >= cy - px && sy <= cy + px * 0.4) return i;
  }
  return -1;
}

// Size a box to its text: as wide as its widest line, as tall as its lines,
// plus padding. `measure(s)` is a string's width in cells at this size. The
// desktop wraps at the box width, so a box fitted here reads the same there.
export function fitRect(n, measure) {
  const s = sizeOf(n);
  const lines = String(n.text || " ").split("\n");
  const w = Math.max(s, ...lines.map(l => measure(l))) + 2 * PAD * s;
  const h = lines.length * LINE * s + 2 * PAD * s;
  const r = (v) => Math.round(v * 1000) / 1000;
  return [r(n.rect[0]), r(n.rect[1]), r(w), r(h)];
}
