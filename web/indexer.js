// Indexing: give a set of Sources and belts an order, then write each one a
// value from a table -- a Source's emitted constant, a belt's seed.
//
// Pure: the orderings, the tables, the file parser and a*x+b. main.js owns
// the gesture (which entities, which order) and writes the values.
//
// A table entry is a number or DEFAULT (null): the entity's default -- 0 for
// a Source, no seed for a belt. Defaults are not transformed by a and b.

export const DEFAULT = null;
export const TABLES = ["random", "ascending", "doubling", "alternating", "file", "hack"];
export const TABLE_NAMES = { random: "Random", ascending: "Ascending", doubling: "Doubling", alternating: "Alternating", file: "File…", hack: "Hack…" };

// NATURAL order: left to right along each row, rows from the bottom up
// (world +Y is up). `items` are { x, y }; returns their indices in order.
//
// Three switches reach the other seven orders a rectangle can be read in, so
// a block laid out rotated or mirrored can still be numbered the way it is
// meant to be read:
//   transpose  columns first: bottom to top up each column, columns left to
//              right (before the reversals below)
//   revX       right to left instead of left to right
//   revY       top to bottom instead of bottom to top
export function naturalOrder(items, { transpose = false, revX = false, revY = false } = {}) {
  const sx = revX ? -1 : 1, sy = revY ? -1 : 1;
  const key = (i) => {
    const x = items[i].x * sx, y = items[i].y * sy;
    return transpose ? [x, y] : [y, x];
  };
  return items.map((_, i) => i).sort((a, b) => {
    const ka = key(a), kb = key(b);
    return ka[0] - kb[0] || ka[1] - kb[1];
  });
}

// A small seeded generator, so the Random table the preview shows is the one
// Apply writes, and Re-roll is just a new seed.
export function rng(seed) {
  let t = seed >>> 0;
  return () => {
    t = (t + 0x6d2b79f5) >>> 0;
    let r = Math.imul(t ^ (t >>> 15), 1 | t);
    r ^= r + Math.imul(r ^ (r >>> 7), 61 | r);
    return ((r ^ (r >>> 14)) >>> 0) / 4294967296;
  };
}

// An uploaded table: numbers (or D) separated by commas and/or whitespace,
// one line or many. Anything else is refused with its line and text, rather
// than quietly read as 0.
export function parseTable(text) {
  const out = [];
  text.split(/\r?\n/).forEach((line, li) => {
    for (const tok of line.split(/[\s,;]+/)) {
      if (!tok) continue;
      if (/^d$/i.test(tok)) { out.push(DEFAULT); continue; }
      const v = Number(tok);
      if (!Number.isFinite(v)) throw new Error(`line ${li + 1}: "${tok}" is not a number or D`);
      out.push(v);
    }
  });
  if (!out.length) throw new Error("the file holds no numbers");
  return out;
}

// The table's first n entries. A file shorter than n repeats from its start.
// A Hack program (`file` is its words) does not: it fills from the start and
// the rest are `pad` (0; the ASCII stream pads with -1, its end-of-stream
// mark), and a program longer than n is cut short -- the caller reports
// that. Its values are machine code (or character codes) and are never put
// through a*x+b.
// `limit` is the largest magnitude a value may have (the doubling table
// reaches it at 2^24, VALUE_LIMIT).
export function tableValues(kind, n, { file = null, seed = 1, limit = 2 ** 24, pad = 0 } = {}) {
  if (kind === "ascending") return Array.from({ length: n }, (_, i) => i);
  if (kind === "alternating") return Array.from({ length: n }, (_, i) => i % 2);
  if (kind === "doubling") return Array.from({ length: n }, (_, i) => Math.min(2 ** i, limit));
  if (kind === "random") {
    const a = Array.from({ length: n }, (_, i) => i);
    const r = rng(seed);
    for (let i = n - 1; i > 0; i--) {
      const j = Math.floor(r() * (i + 1));
      [a[i], a[j]] = [a[j], a[i]];
    }
    return a;
  }
  if (kind === "hack") {
    const words = file || [];
    return Array.from({ length: n }, (_, i) => (i < words.length ? words[i] : pad));
  }
  if (kind === "file") {
    if (!file || !file.length) return Array(n).fill(DEFAULT);
    return Array.from({ length: n }, (_, i) => file[i % file.length]);
  }
  throw new Error(`unknown table ${kind}`);
}

// a*x + b for each entry, as the float32 the row will hold, clamped to the
// value limit. A default stays a default.
export function finalValues(table, a, b, limit = 2 ** 24) {
  return table.map(x => {
    if (x === DEFAULT) return DEFAULT;
    const v = Math.fround(a * x + b);
    return v > limit ? limit : v < -limit ? -limit : v;
  });
}

// --- recent table files ------------------------------------------------------
//
// The File… table remembers the last RECENT_FILES files loaded, newest
// first, so the same indices need not be browsed for again: { name, values }
// each, values as parsed (DEFAULT as null). Kept in localStorage; a file
// loaded again under the same name replaces its old entry and moves to the
// front. Pure apart from the two storage calls, which take the store.
export const RECENT_FILES = 8;
export const RECENT_FILES_KEY = "gates.web.indexFiles.v1";

export function pushRecentFile(list, entry, max = RECENT_FILES) {
  return [entry, ...list.filter(e => e.name !== entry.name)].slice(0, max);
}

// Only well-formed entries: a name, and values that are numbers or null.
export function cleanRecentFiles(v) {
  if (!Array.isArray(v)) return [];
  return v.filter(e => e && typeof e.name === "string" && Array.isArray(e.values) && e.values.length &&
    e.values.every(x => x === DEFAULT || Number.isFinite(x))).slice(0, RECENT_FILES);
}

export function loadRecentFiles(store = globalThis.localStorage) {
  try { return cleanRecentFiles(JSON.parse(store?.getItem(RECENT_FILES_KEY) || "null")); } catch { return []; }
}

export function saveRecentFiles(list, store = globalThis.localStorage) {
  try { store?.setItem(RECENT_FILES_KEY, JSON.stringify(list)); } catch { /* full or absent: not kept */ }
}
