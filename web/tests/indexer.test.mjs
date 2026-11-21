// Indexing's pure parts (indexer.js): orders, tables, the parser, a*x+b.

import { test } from "node:test";
import assert from "node:assert/strict";
import { DEFAULT, naturalOrder, parseTable, tableValues, finalValues } from "../indexer.js";

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
