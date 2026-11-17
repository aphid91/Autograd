// Undo, redo and the checkpoint (history.js), on opaque string states.

import { test } from "node:test";
import assert from "node:assert/strict";
import { History, UNDO_DEPTH } from "../history.js";

function walk(h, ...ops) {
  return ops.map(op => (op === "u" ? h.undo() : h.redo()));
}

test("undo and redo walk the kept states", () => {
  const h = new History(3);
  h.reset("L");
  for (const s of ["a", "b", "c"]) h.commit(s);
  assert.deepEqual(walk(h, "u", "u", "u"), ["b", "a", "L"]);
  assert.equal(h.canUndo, false, "the load is the checkpoint: nothing older");
  assert.deepEqual(walk(h, "r", "r", "r", "r"), ["a", "b", "c", null]);
});

test("only UNDO_DEPTH steps are kept; past the oldest is the checkpoint", () => {
  const h = new History(3);
  h.reset("L");
  for (const s of ["a", "b", "c", "d", "e"]) h.commit(s);
  // Kept: b c d e (three steps back from e). Then the checkpoint, L.
  assert.deepEqual(walk(h, "u", "u", "u"), ["d", "c", "b"]);
  assert.equal(h.undo(), "L", "undo from the oldest restores the checkpoint");
  assert.equal(h.undo(), null);
  // ...and redo walks back through the history from there.
  assert.deepEqual(walk(h, "r", "r", "r", "r"), ["b", "c", "d", "e"]);
});

test("a pressed checkpoint is where the oldest undo lands", () => {
  const h = new History(3);
  h.reset("L");
  h.commit("a");
  h.setCheckpoint(h.current);          // checkpoint at "a"
  for (const s of ["b", "c", "d", "e"]) h.commit(s);
  assert.deepEqual(walk(h, "u", "u", "u", "u", "u"), ["d", "c", "b", "a", null]);
});

test("an edit after undoing discards the undone steps", () => {
  const h = new History(3);
  h.reset("L");
  for (const s of ["a", "b", "c"]) h.commit(s);
  h.undo(); h.undo();                  // at a
  h.commit("x");
  assert.equal(h.canRedo, false);
  assert.deepEqual(walk(h, "u", "u"), ["a", "L"]);
});

test("an edit made on the checkpoint starts the history from it", () => {
  const h = new History(3);
  h.reset("L");
  for (const s of ["a", "b", "c", "d"]) h.commit(s);
  while (h.undo() !== null);           // down to the checkpoint L
  assert.equal(h.current, "L");
  h.commit("y");
  assert.deepEqual(walk(h, "u", "u"), ["L", null]);
  assert.deepEqual(walk(h, "r"), ["y"]);
});

test("the depth is one constant", () => {
  assert.equal(new History().depth, UNDO_DEPTH);
});
