// node --test web/tests/
//
// The belt planner's rules, against a fake board: a set of occupied cells.

import { test } from "node:test";
import assert from "node:assert/strict";
import {
  planPath, pathDirs, planRun, turnTip,
  ROUTE_H_FIRST, ROUTE_V_FIRST, ROUTE_LINE,
} from "../planner.js";

const MAX_GAP = 4;
const E = 0, N = 1, W = 2, S = 3;

function board(...occupied) {
  const set = new Set(occupied.map(([x, y]) => `${x},${y}`));
  return (x, y) => !set.has(`${x},${y}`);
}
function run(start, end, extra = {}) {
  return { start, end, route: ROUTE_H_FIRST, tipTurn: 0, singleDir: E, ...extra };
}
const kinds = (plan) => plan.cells.map(c => c.kind).join(" ");

// A vertical wall of `w` cells at x = 3..3+w-1 across row 0.
const wall = (w, x0 = 3) => Array.from({ length: w }, (_, k) => [x0 + k, 0]);

test("paths match the desktop's shapes", () => {
  assert.deepEqual(planPath([0, 0], [2, 1], ROUTE_H_FIRST), [[0, 0], [1, 0], [2, 0], [2, 1]]);
  assert.deepEqual(planPath([0, 0], [2, 1], ROUTE_V_FIRST), [[0, 0], [0, 1], [1, 1], [2, 1]]);
  const line = planPath([0, 0], [3, 2], ROUTE_LINE);
  for (let i = 1; i < line.length; i++) {   // 4-connected, no diagonal steps
    const d = Math.abs(line[i][0] - line[i - 1][0]) + Math.abs(line[i][1] - line[i - 1][1]);
    assert.equal(d, 1);
  }
  assert.deepEqual(pathDirs([[0, 0], [1, 0], [1, 1]]), [E, N, N]);
});

test("a clear run is all belts", () => {
  const p = planRun(run([0, 0], [5, 0]), board(), MAX_GAP);
  assert.equal(kinds(p), "belt belt belt belt belt belt");
  assert.deepEqual(p.pairs, []);
});

test("crossing four belt runs at right angles tunnels under them", () => {
  const p = planRun(run([0, 0], [9, 0]), board(...wall(4)), MAX_GAP);
  assert.equal(kinds(p), "belt belt entry buried buried buried buried exit belt belt");
  assert.deepEqual(p.pairs, [[2, 7]]);
  assert.ok(p.cells.slice(2, 8).every(c => c.dir === E));
});

test("five is too many: the desktop underground spans at most four", () => {
  const p = planRun(run([0, 0], [10, 0]), board(...wall(5)), MAX_GAP);
  assert.deepEqual(p.pairs, []);
  assert.equal(p.cells.filter(c => c.kind === "blocked").length, 5);
});

test("an immediate turn after the crossing refuses the bridge", () => {
  // Wall at x=3..4; the run turns north at x=5, the cell straight after it.
  const p = planRun(run([0, 0], [5, 3]), board(...wall(2)), MAX_GAP);
  assert.deepEqual(p.pairs, []);
  // One more straight cell before the turn and it bridges.
  const q = planRun(run([0, 0], [6, 3]), board(...wall(2)), MAX_GAP);
  assert.deepEqual(q.pairs, [[2, 5]]);
});

test("the tip's turn counts: an exit on the tip must face straight on", () => {
  const blocked = board(...wall(2));
  assert.deepEqual(planRun(run([0, 0], [5, 0]), blocked, MAX_GAP).pairs, [[2, 5]]);
  assert.deepEqual(planRun(run([0, 0], [5, 0], { tipTurn: 1 }), blocked, MAX_GAP).pairs, []);
});

test("a turn before the crossing is fine: an entrance takes input from any side", () => {
  // Down from (2,3) to (2,0), then east across the wall.
  const p = planRun(run([2, 3], [8, 0], { route: ROUTE_V_FIRST }), board(...wall(2)), MAX_GAP);
  const entry = p.cells.find(c => c.kind === "entry");
  assert.deepEqual([entry.x, entry.y, entry.dir], [2, 0, E]);
});

test("two obstacles one free cell apart are bridged whole...", () => {
  // Blocked at 3 and 5, free at 4: one bridge 2 -> 6, the free cell buried.
  const p = planRun(run([0, 0], [8, 0]), board([3, 0], [5, 0]), MAX_GAP);
  assert.deepEqual(p.pairs, [[2, 6]]);
  assert.equal(p.cells[4].kind, "buried");
});

test("...or not at all, if the whole is too long", () => {
  // Blocked 3,4 and 6,7 with 5 free: five cells between the ends -- too many.
  // Bridging just the first would put its exit on 5, where the second's
  // entrance must also go.
  const p = planRun(run([0, 0], [10, 0]), board([3, 0], [4, 0], [6, 0], [7, 0]), MAX_GAP);
  assert.deepEqual(p.pairs, []);
  assert.equal(p.cells.filter(c => c.kind === "blocked").length, 4);
});

test("two free cells between obstacles leave room for two pairs", () => {
  // Blocked 3 and 6; 4,5 free: exit on 4, entrance on 5.
  const p = planRun(run([0, 0], [9, 0]), board([3, 0], [6, 0]), MAX_GAP);
  assert.deepEqual(p.pairs, [[2, 4], [5, 7]]);
});

test("an obstacle touching either end of the run is not bridged", () => {
  assert.deepEqual(planRun(run([3, 0], [8, 0]), board(...wall(2)), MAX_GAP).pairs, []);
  assert.deepEqual(planRun(run([0, 0], [4, 0]), board(...wall(2)), MAX_GAP).pairs, []);
  assert.deepEqual(planRun(run([0, 0], [3, 0]), board(...wall(2)), MAX_GAP).pairs, []);
});

test("a bridge never spans a corner", () => {
  // H-first from (0,0) to (4,3): east to x=4 then north. Block the corner
  // region so the only crossing is around the bend.
  const p = planRun(run([0, 0], [4, 3]), board([4, 0], [4, 1]), MAX_GAP);
  assert.deepEqual(p.pairs, []);
});

test("rotating the tip never points it back into the run", () => {
  const r = run([0, 0], [4, 0]);
  const seen = [];
  for (let k = 0; k < 6; k++) { turnTip(r, true); seen.push(r.tipTurn); }
  assert.ok(!seen.includes(2));
  assert.deepEqual(seen.slice(0, 3), [3, 1, 0]);   // right, left (skipping back), straight
  const one = run([2, 2], [2, 2]);
  const dirs = [];
  for (let k = 0; k < 4; k++) { turnTip(one, false); dirs.push(one.singleDir); }
  assert.deepEqual(dirs, [N, W, S, E]);             // a one-cell run takes all four
});

test("a bridge needs a straight span even when both ends face the same way", () => {
  // A staircase steps E,E,E,E,N,E,...: block (4,0) and (4,1), the bend. The
  // cells either side, (3,0) and (5,1), both face east -- only the bend in
  // between makes this unbridgeable. (The exit-facing rule cannot see it.)
  const p = planRun(run([0, 0], [8, 2], { route: ROUTE_LINE }), board([4, 0], [4, 1]), MAX_GAP);
  assert.equal(p.cells[3].dir, E);
  assert.equal(p.cells[6].dir, E);
  assert.deepEqual(p.pairs, []);
});
