// The audio region: a box of the board as the sound selection, given to the
// recorder and eligibleRuns() as an object with has(y * W + x) -- the only
// call they make of a selection -- and holding through edits.

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { Board } from "../board.js";
import { Sim } from "../sim.js";
import { eligibleRuns, AUDIO_DEFAULTS } from "../audio-params.js";
import { Recorder } from "../audio.js";

const here = dirname(fileURLToPath(import.meta.url));
const T = JSON.parse(readFileSync(join(here, "..", "types.json"), "utf8"));
const load = async (name) => {
  const b = readFileSync(join(here, "..", "..", name));
  return Board.fromNpz(T, b.buffer.slice(b.byteOffset, b.byteOffset + b.byteLength));
};
const regionOf = (W, x0, y0, w, h) =>
  ({ has: (k) => { const x = k % W, y = (k - x) / W; return x >= x0 && x < x0 + w && y >= y0 && y < y0 + h; } });

test("a region picks exactly the runs touching its box", async () => {
  const board = await load("cpu.npz"), sim = new Sim(T, board);
  sim.sync();
  const W = board.width;
  const [cx, cy] = sim.order[Math.floor(sim.order.length / 2)].cells[0];   // a box round a real run
  const box = { x: cx - 8, y: cy - 6, w: 16, h: 12 };
  const inside = ([x, y]) => x >= box.x && x < box.x + box.w && y >= box.y && y < box.y + box.h;
  const got = eligibleRuns(sim, 1, 512, regionOf(W, box.x, box.y, box.w, box.h));
  const want = sim.order.filter(s => s.length >= 1 && s.length <= 512 && s.cells.some(inside));
  assert.ok(want.length > 0 && want.length < sim.order.length, "the box catches some runs, not all");
  assert.deepEqual(got.map(s => s.sid), want.map(s => s.sid));
});

test("a region holds through edits: a run built inside it afterwards sounds", async () => {
  const board = await load("cpu.npz"), sim = new Sim(T, board);
  sim.sync();
  // An empty 12x4 patch beyond the board's contents, and a box over it.
  const x0 = board.bounds.maxX + 4, y0 = board.bounds.minY + 2, W = board.width;
  const region = regionOf(W, x0, y0, 12, 4);
  const msgs = [];
  const rec = new Recorder({ postMessage: (m) => msgs.push(m) }, { ...AUDIO_DEFAULTS, beltMinLen: 1, beltCutoff: 512 }, region);
  rec.configure(sim);
  assert.equal(rec.cands.length, 0, "nothing there yet");
  for (let i = 0; i < 6; i++) board.spawnBelt(x0 + 2 + i, y0 + 1, 0);
  board.derive();
  sim.sync();
  rec.configure(sim);   // what a rebuild does on the recorder's next tick
  assert.equal(rec.cands.length, 1, "the new run, touching the box, is a candidate");
  assert.equal(rec.cands[0].length, 6);
  assert.equal(rec.selInfo.runs, 1, "normalisation counts the region only");
});
