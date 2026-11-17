// Indexing: give a set of Sources and belts an order, then write each one a
// value from a table -- a Source's emitted constant, a belt's seed.
//
// Pure: the orderings, the tables, the file parser and a*x+b. main.js owns
// the gesture (which entities, which order) and writes the values.
//
// A table entry is a number or DEFAULT (null): the entity's default -- 0 for
// a Source, no seed for a belt. Defaults are not transformed by a and b.

export const DEFAULT = null;
export const TABLES = ["random", "ascending", "doubling", "file"];
export const TABLE_NAMES = { random: "Random", ascending: "Ascending", doubling: "Doubling", file: "File…" };

// NATURAL order: left to right along each row, rows from the bottom up
// (world +Y is up). `items` are { x, y }; returns their indices in order.
export function naturalOrder(items) {
  return items.map((_, i) => i).sort((a, b) => items[a].y - items[b].y || items[a].x - items[b].x);
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
// `limit` is the largest magnitude a value may have (the doubling table
// reaches it at 2^24, VALUE_LIMIT).
export function tableValues(kind, n, { file = null, seed = 1, limit = 2 ** 24 } = {}) {
  if (kind === "ascending") return Array.from({ length: n }, (_, i) => i);
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
