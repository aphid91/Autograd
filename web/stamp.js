// Stamps: a group of entities held together, transformable as one unit. A
// port of core/stamp.py's geometry -- the selection, a copied block and a
// baked stamp tool are all one of these.
//
// A stamp is plain data, so it persists in a toolbar slot as JSON:
//
//   { entries: [entry], w, h }      normalised: the covered box starts at 0,0
//   entry = { type, dx, dy, dir, flip, exit, partner, emit, seed }
//
// `type` is the type NAME (types.json `name`), not the integer id, for the
// same reason the desktop's toolbar file uses class names: it is readable,
// and it survives an id renumbering. `partner` is the index of another entry
// in this stamp (an underground pair), or null -- recorded in stamp-local
// terms so a copied pair re-pairs with itself on paste. `emit` is a Source's
// value and `seed` a belt's seed value, each null when absent.

const byNameCache = new WeakMap();

export function typeByName(T, name) {
  let m = byNameCache.get(T);
  if (!m) {
    m = new Map();
    for (const [id, td] of Object.entries(T.types)) m.set(td.name, { id: Number(id), td });
    byNameCache.set(T, m);
  }
  return m.get(name) || null;
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

// A stamp from entries in any frame: shifted so the box starts at (0, 0).
// Also returns where that box was, so a caller holding absolute entries
// knows the stamp's origin.
export function makeStamp(T, entries) {
  const b = bounds(T, entries);
  return {
    stamp: { entries: entries.map(e => ({ ...e, dx: e.dx - b.x, dy: e.dy - b.y })), w: b.w, h: b.h },
    origin: [b.x, b.y],
  };
}

export const copyStamp = (s) => ({ entries: s.entries.map(e => ({ ...e })), w: s.w, h: s.h });

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
  return makeStamp(T, out).stamp;
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
  return makeStamp(T, out).stamp;
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
export function isValidStamp(T, s) {
  return !!s && Array.isArray(s.entries) && s.entries.length > 0 &&
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
