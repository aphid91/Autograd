// Indexing's pure parts (indexer.js): orders, tables, the parser, a*x+b.

import { test } from "node:test";
import assert from "node:assert/strict";
import { DEFAULT, naturalOrder, parseTable, tableValues, finalValues } from "../indexer.js";
import { asciiCodes, END_OF_STREAM } from "../hack.js";

test("natural order is left to right, rows from the bottom up", () => {
  const items = [{ x: 5, y: 2 }, { x: 1, y: 2 }, { x: 9, y: 0 }, { x: 0, y: 7 }];
  assert.deepEqual(naturalOrder(items), [2, 1, 0, 3]);
});

test("ascending and doubling", () => {
  assert.deepEqual(tableValues("ascending", 4), [0, 1, 2, 3]);
  assert.deepEqual(tableValues("doubling", 4), [1, 2, 4, 8]);
  assert.deepEqual(tableValues("alternating", 5), [0, 1, 0, 1, 0]);
  assert.equal(tableValues("doubling", 30)[29], 2 ** 24, "clamped at the value limit");
});

test("random is a shuffle of 0..n-1, fixed by its seed", () => {
  const a = tableValues("random", 50, { seed: 7 });
  assert.deepEqual([...a].sort((x, y) => x - y), Array.from({ length: 50 }, (_, i) => i));
  assert.deepEqual(tableValues("random", 50, { seed: 7 }), a, "same seed, same table");
  assert.notDeepEqual(tableValues("random", 50, { seed: 8 }), a, "a re-roll changes it");
});

test("a file repeats to fill the index, and D is the default", () => {
  const f = parseTable("5, D\n");
  assert.deepEqual(f, [5, DEFAULT]);
  assert.deepEqual(tableValues("file", 5, { file: f }), [5, DEFAULT, 5, DEFAULT, 5]);
  assert.deepEqual(parseTable("1\n2 3\n-4.5,d"), [1, 2, 3, -4.5, DEFAULT]);
  assert.throws(() => parseTable("1\nx"), /line 2: "x"/);
  assert.throws(() => parseTable("  \n"), /no numbers/);
});

test("a*x+b, defaults untouched, float32 and clamped", () => {
  assert.deepEqual(finalValues([0, 1, 2, DEFAULT], -1, -1), [-1, -2, -3, DEFAULT]);
  assert.equal(finalValues([0.1], 1, 0)[0], Math.fround(0.1));
  assert.equal(finalValues([1e9], 1, 0)[0], 2 ** 24);
});

test("position order: transpose and the two reversals reach all eight readings", () => {
  // A 2x2 block: indices 0..3 at (0,0) (1,0) (0,1) (1,1).
  const it = [{ x: 0, y: 0 }, { x: 1, y: 0 }, { x: 0, y: 1 }, { x: 1, y: 1 }];
  const seen = new Set();
  for (const transpose of [false, true]) for (const revX of [false, true]) for (const revY of [false, true]) {
    seen.add(naturalOrder(it, { transpose, revX, revY }).join(""));
  }
  assert.equal(seen.size, 8);
  assert.deepEqual(naturalOrder(it, { transpose: true }), [0, 2, 1, 3], "up each column, columns left to right");
  assert.deepEqual(naturalOrder(it, { revX: true }), [1, 0, 3, 2], "rows still bottom up, each right to left");
  assert.deepEqual(naturalOrder(it, { revY: true }), [2, 3, 0, 1]);
});

test("a hack program fills from the start, pads with 0 and is cut when long", () => {
  assert.deepEqual(tableValues("hack", 5, { file: [7, 64528] }), [7, 64528, 0, 0, 0]);
  assert.deepEqual(tableValues("hack", 2, { file: [1, 2, 3] }), [1, 2]);
});

test("ascii: the text's character codes, then -1 for ever", () => {
  const codes = asciiCodes("Hi\r\n@1 // x\n");
  assert.deepEqual(codes, [72, 105, 10, 64, 49, 32, 47, 47, 32, 120, 10], "raw text, CRLF as one newline, comments kept");
  assert.deepEqual(asciiCodes("a\rb"), [97, 10, 98], "a lone CR is a newline too");
  assert.deepEqual(asciiCodes("é😀"), [233, 0x1f600], "one code point per character");
  assert.deepEqual(tableValues("hack", 6, { file: asciiCodes("ok"), pad: END_OF_STREAM }), [111, 107, -1, -1, -1, -1]);
  assert.deepEqual(tableValues("hack", 1, { file: asciiCodes("ok"), pad: END_OF_STREAM }), [111], "cut when short");
  assert.deepEqual(tableValues("hack", 3, { file: [5] }), [5, 0, 0], "machine code still pads with 0");
});
