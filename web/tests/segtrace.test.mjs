// node --test web/tests/*.test.mjs
//
// The run a long press selects, against core/segments.py itself: for every
// belt and underground cell of cpu.npz, the cells traceRun() finds through
// it must be exactly the cells of the desktop segment that holds it.

import { test } from "node:test";
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { readFileSync, existsSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { Board } from "../board.js";
import { traceRun } from "../segtrace.js";

const ROOT = fileURLToPath(new URL("../..", import.meta.url));
const SAVE = ROOT + "cpu.npz";
const T = JSON.parse(readFileSync(new URL("../types.json", import.meta.url)));

test("traceRun finds exactly the desktop's segment, for every belt cell of cpu.npz", { skip: !existsSync(SAVE) }, async () => {
  const buf = readFileSync(SAVE);
  const board = await Board.fromNpz(T, buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.byteLength));
  const py = `
import json, sys
sys.path.insert(0, ${JSON.stringify(ROOT)})
from core.gameboard import Gameboard
from core import persistence
from core.segments import SegmentIndex
b = Gameboard(512, 512)
persistence.load(b, ${JSON.stringify(SAVE)})
idx = SegmentIndex(b); idx.rebuild()
out = {}
for s in idx.segments.values() if hasattr(idx, "segments") else idx._segments.values():
    key = " ".join(sorted(f"{x},{y}" for (x, y) in s.cells))
    for (x, y) in s.cells: out[f"{x},{y}"] = key
print(json.dumps(out))`;
  const want = JSON.parse(execFileSync("python3", ["-c", py], { maxBuffer: 64 << 20 }));
  const node = (x, y) => board.beltNode(x, y);
  const taken = (x, y, d) => board.incoming(x, y, d);
  let checked = 0;
  for (const cell of Object.keys(want)) {
    const [x, y] = cell.split(",").map(Number);
    const got = traceRun(node, taken, x, y).map(c => `${c.x},${c.y}`).sort().join(" ");
    assert.equal(got, want[cell], `run through ${cell}`);
    checked++;
  }
  assert.ok(checked > 1000, `only ${checked} cells checked`);
});
