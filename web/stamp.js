// Stamps: a group of entities held together, transformable as one unit. A
// port of core/stamp.py's geometry -- the selection, a copied block and a
// baked stamp tool are all one of these.
//
// A stamp is plain data, so it persists in a toolbar slot as JSON:
//
//   { entries: [entry], w, h, notes }   normalised: the covered box starts at 0,0
//   entry = { type, dx, dy, dir, flip, exit, partner, emit, ascii, seed }
//   notes = [label]: the desktop's label records (notes.js), rect in the
//           stamp's frame -- core/stamp.py's Stamp.annotations. Optional:
//           a stamp stored before labels travelled has none.
//
// `type` is the type NAME (types.json `name`), not the integer id, for the
// same reason the desktop's toolbar file uses class names: it is readable,
// and it survives an id renumbering. `partner` is the index of another entry
// in this stamp (an underground pair), or null -- recorded in stamp-local
// terms so a copied pair re-pairs with itself on paste. `emit` is a Source's
// value and `seed` a belt's seed value, each null when absent; `ascii` a
// Source's ASCII flag (board.js F_ASCII), absent or false on a plain one.

const byNameCache = new WeakMap();

export function typeByName(T, name) {
  let m = byNameCache.get(T);
  if (!m) {
    m = new Map();
    for (const [id, td] of Object.entries(T.types)) m.set(td.name, { id: Number(id), td });
    byNameCache.set(T, m);
  }
  // An alias (pixel.js: "sink" is now "pixel") for names saved before.
  return m.get(name) || m.get(T.aliases?.[name]) || null;
}

// The footprint an entry covers at its orientation: { w, h }.
export function entrySize(T, e) {
  const t = typeByName(T, e.type);
  const o = t.td.orient[`${e.dir}${e.flip ? 1 : 0}`];
  return { w: o.w, h: o.h };
}

// The box every entry covers, measured over cells (not origins).
export function bounds(T, entries) {
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
  for (const e of entries) {
    const { w, h } = entrySize(T, e);
    x0 = Math.min(x0, e.dx); y0 = Math.min(y0, e.dy);
    x1 = Math.max(x1, e.dx + w); y1 = Math.max(y1, e.dy + h);
  }
  if (!isFinite(x0)) return { x: 0, y: 0, w: 0, h: 0 };
  return { x: x0, y: y0, w: x1 - x0, h: y1 - y0 };
}

// A stamp from entries and labels in any frame: shifted so the box starts
// at (0, 0). Also returns where that box was, so a caller holding absolute
// entries knows the stamp's origin.
//
// The box is the ENTRIES' box: a label hanging outside the machines must
// not move it, or where the paste ghost sits would change (core/stamp.py
// bounds()). With no entries, a stamp of labels alone takes their box,
// floored and ceiled onto the cell lattice -- a zero box would centre the
// paste on nothing.
export function makeStamp(T, entries, notes = []) {
  const b = entries.length ? bounds(T, entries) : notesBounds(notes);
  return {
    stamp: {
      entries: entries.map(e => ({ ...e, dx: e.dx - b.x, dy: e.dy - b.y })),
      w: b.w, h: b.h,
      notes: notes.map(n => shiftNote(n, -b.x, -b.y)),
    },
    origin: [b.x, b.y],
  };
}

const copyNote = (n) => ({ ...n, rect: n.rect.slice() });
export const copyStamp = (s) => ({ entries: s.entries.map(e => ({ ...e })), w: s.w, h: s.h, notes: (s.notes || []).map(copyNote) });

// --- labels in a stamp -------------------------------------------------------
// A label's rect is [x, y, w, h] with (x, y) its TOP-left (world +Y is up,
// so it hangs down to y - h). Positions transform with the stamp; the box
// and its text never do -- a turned stamp keeps its labels upright and
// readable, and mirrored text is simply broken (core/stamp.py), so a label
// moves its centre and keeps its w and h.

// Rounded to a millionth: tidy in a save, and fine enough that a label
// does not drift over many turns (rounding to the thousandth a dragged
// label uses moved a box of odd-thousandth height by half a thousandth a
// turn).
const r3 = (v) => Math.round(v * 1e6) / 1e6;

export function shiftNote(n, dx, dy) {
  const [x, y, w, h] = n.rect;
  return { ...n, rect: [r3(x + dx), r3(y + dy), w, h] };
}

// The label with its centre sent through `f(cx, cy) -> [cx', cy']`.
function moveNote(n, f) {
  const [x, y, w, h] = n.rect;
  const [cx, cy] = f(x + w / 2, y - h / 2);
  return { ...n, rect: [r3(cx - w / 2), r3(cy + h / 2), w, h] };
}

// The cells the labels' boxes cover, on the lattice.
export function notesBounds(notes) {
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
  for (const n of notes) {
    const [x, y, w, h] = n.rect;
    x0 = Math.min(x0, x); x1 = Math.max(x1, x + w);
    y0 = Math.min(y0, y - h); y1 = Math.max(y1, y);
  }
  if (!isFinite(x0)) return { x: 0, y: 0, w: 0, h: 0 };
  x0 = Math.floor(x0); y0 = Math.floor(y0);
  return { x: x0, y: y0, w: Math.max(1, Math.ceil(x1) - x0), h: Math.max(1, Math.ceil(y1) - y0) };
}

// Does a label's box touch the cell rectangle { x, y, w, h }? (Cells x ..
// x+w-1: the box covers world [x, x+w] x [y, y+h].)
export function noteTouches(n, r) {
  const [x, y, w, h] = n.rect;
  return x <= r.x + r.w && x + w >= r.x && y - h <= r.y + r.h && y >= r.y;
}

// A quarter turn, clockwise if `cw`. Each entry's whole cell span turns and
// the new span's lower-left becomes its origin: a footprint extends +X/+Y
// from its origin whatever its facing, so rotating the bare origin would
// leave a 2x1 hanging off the wrong side (core/stamp.py rotated()).
export function rotated(T, s, cw) {
  const turns = cw ? 3 : 1;   // CCW quarter turns, as cell.rotate_offset()
  const out = s.entries.map(e => ({ ...e }));
  for (const e of out) {
    for (let i = 0; i < turns; i++) {
      const { h } = entrySize(T, e);
      // (cx, cy) -> (-cy, cx) over the span [dx, dx+w) x [dy, dy+h).
      const dx = -(e.dy + h - 1), dy = e.dx;
      e.dx = dx; e.dy = dy;
      e.dir = (e.dir + 1) % 4;
    }
  }
  // A label's centre turns with the cells: cell (cx, cy) -> (-cy, cx) is
  // the continuous (x, y) -> (1 - y, x), applied the same number of times.
  const notes = (s.notes || []).map(n => moveNote(n, (x, y) => {
    for (let i = 0; i < turns; i++) [x, y] = [1 - y, x];
    return [x, y];
  }));
  return makeStamp(T, out, notes).stamp;
}

// Mirrored about the stamp's vertical centre line. A chiral machine toggles
// its flip; an achiral one (belts, Sink) would store a difference that does
// not exist (core/stamp.py mirror_orientation()).
export function mirrored(T, s) {
  const out = s.entries.map(e => {
    const { w } = entrySize(T, e);
    const chiral = typeByName(T, e.type).td.chiral;
    return { ...e, dx: -(e.dx + w - 1), dir: (6 - e.dir) % 4, flip: chiral ? !e.flip : e.flip };
  });
  // Cell cx -> -cx is the continuous x -> 1 - x; the text is not mirrored.
  const notes = (s.notes || []).map(n => moveNote(n, (x, y) => [1 - x, y]));
  return makeStamp(T, out, notes).stamp;
}

// Every cell of every entry with the box at (ox, oy), as [x, y, index].
export function stampCells(T, s, ox, oy) {
  const out = [];
  s.entries.forEach((e, i) => {
    const { w, h } = entrySize(T, e);
    for (let lx = 0; lx < w; lx++) {
      for (let ly = 0; ly < h; ly++) out.push([ox + e.dx + lx, oy + e.dy + ly, i]);
    }
  });
  return out;
}

// Keep only entries `keep` (a predicate on index), renumbering partners and
// dropping a partner link to an entry that left.
export function subset(entries, keep) {
  const map = new Map();
  const kept = [];
  entries.forEach((e, i) => { if (keep(i)) { map.set(i, kept.length); kept.push({ ...e }); } });
  for (const e of kept) e.partner = e.partner !== null && map.has(e.partner) ? map.get(e.partner) : null;
  return kept;
}

// Does every entry name a type this build has? A slot holding a stamp with
// an unknown machine costs that slot, not the bar.
// A stamp of labels alone is a real stamp (core/stamp.py Stamp.__bool__).
export function isValidStamp(T, s) {
  if (!s || !Array.isArray(s.entries)) return false;
  const notes = s.notes === undefined ? [] : s.notes;
  if (!Array.isArray(notes) || !notes.every(n => n && Array.isArray(n.rect) && n.rect.length === 4)) return false;
  return (s.entries.length > 0 || notes.length > 0) &&
    s.entries.every(e => e && typeByName(T, e.type));
}

// The machines with a natural inverse, and what Reverse makes of each. Every
// one is a half turn about the footprint's centre, which leaves the box (and
// so the origin) where it was and puts each input port exactly where an
// output was, and vice versa. Swap and Reciprocal are their own inverses;
// Merge and Route are each other's: a Route turned half round has Merge's
// ports with every direction reversed, and its control polarity lines up --
// Merge takes lane `a` when c > 0, and the turned Route sends a positive
// control's value back out of that very cell. The half turn commutes with
// the mirror, so a flipped machine keeps its flip.
// Pinned by tests/stamp.test.mjs, which runs values through a gate and its
// reverse and asserts they come back out where they went in.
const INVERSE = { swap: "swap", recip: "recip", merge: "route", route: "merge" };

export function reversedGate(e) {
  const type = INVERSE[e.type];
  return type ? { ...e, type, dir: (e.dir + 2) % 4 } : e;
}
