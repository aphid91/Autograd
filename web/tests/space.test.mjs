// Spatial audio (space.js): distance to the box, the falloff and the
// distance low-pass, the weights on a real board, and the synth honouring
// them -- out-of-earshot runs take no voice, a pan re-voices without a
// reconfigure, and the low-pass really takes the top off.

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { boxDist, falloff, lowpassHz, fringeOf, spaceWeights, spaceKey, LP_TOP } from "../space.js";
import { Board } from "../board.js";
import { Sim } from "../sim.js";
import { AUDIO_DEFAULTS, eligibleRuns } from "../audio-params.js";
import { GatesSynth } from "../audio-dsp.js";

const here = dirname(fileURLToPath(import.meta.url));
const T = JSON.parse(readFileSync(join(here, "..", "types.json"), "utf8"));
const load = async (name) => {
  const b = readFileSync(join(here, "..", "..", name));
  return Board.fromNpz(T, b.buffer.slice(b.byteOffset, b.byteOffset + b.byteLength));
};

test("distance: overlap is 0, an edge-sharing cell is just outside, the rest Euclidean", () => {
  const r = { x: 10, y: 10, w: 5, h: 5 };
  assert.equal(boxDist(12, 12, 13, 13, r), 0);
  assert.ok(boxDist(15, 12, 16, 13, r) > 0, "the cell just right of the box is outside");
  assert.ok(boxDist(14, 14, 15, 15, r) === 0, "the last cell inside is inside");
  assert.equal(boxDist(18, 10, 19, 11, r), 3);
  assert.equal(boxDist(18, 19, 19, 20, r), 5);   // 3, 4
});

test("falloff: 1 inside, a half cosine to 0 at the fringe; 0 is a hard edge", () => {
  assert.equal(falloff(0, 10), 1);
  assert.ok(Math.abs(falloff(5, 10) - 0.5) < 1e-12);
  assert.equal(falloff(10, 10), 0);
  assert.equal(falloff(11, 10), 0);
  assert.equal(falloff(1e-6, 0), 0, "hard edge: just outside is out");
  assert.equal(falloff(0, 0), 1);
  for (let d = 0; d < 10; d += 0.5) assert.ok(falloff(d + 0.5, 10) <= falloff(d, 10), "monotone");
});

test("the distance low-pass: none inside or at 0, closing with distance", () => {
  assert.equal(lowpassHz(0, 10, 1), 0);
  assert.equal(lowpassHz(5, 10, 0), 0);
  assert.ok(lowpassHz(2, 10, 1) > lowpassHz(8, 10, 1));
  assert.ok(Math.abs(lowpassHz(10, 10, 1) - LP_TOP / 128) < 1e-9, "7 octaves down at the edge, full dial");
  assert.equal(fringeOf({ rect: { x: 0, y: 0, w: 20, h: 10 }, fringe: 0.5 }), 10);
});

test("on a board: a hard edge is exactly 'touching the box'; a soft one adds quieter runs farther out", async () => {
  const board = await load("cpu.npz"), sim = new Sim(T, board);
  sim.sync();
  const runs = eligibleRuns(sim, 1, 512);
  const [cx, cy] = runs[Math.floor(runs.length / 2)].cells[0];
  const rect = { x: cx - 8, y: cy - 6, w: 16, h: 12 };
  const inside = ([x, y]) => x >= rect.x && x < rect.x + rect.w && y >= rect.y && y < rect.y + rect.h;
  const hard = spaceWeights(sim, runs, [], { rect, fringe: 0, lp: 0 });
  runs.forEach((s, i) => assert.equal(hard.gain[i], s.cells.some(inside) ? 1 : 0, `run ${s.sid}`));
  const soft = spaceWeights(sim, runs, [], { rect, fringe: 1, lp: 1 });
  let partial = 0;
  runs.forEach((s, i) => {
    if (hard.gain[i] === 1) assert.equal(soft.gain[i], 1);
    if (soft.gain[i] > 0 && soft.gain[i] < 1) { partial++; assert.ok(soft.lp[i] > 0, "a fringe run is filtered"); }
    if (hard.gain[i] === 1) assert.equal(soft.lp[i], 0, "inside is never filtered");
  });
  assert.ok(partial > 0, "the fringe catches runs");
  assert.ok(soft.runSum > hard.runSum);
  assert.notEqual(spaceKey({ rect, fringe: 1, lp: 1 }), spaceKey({ rect: { ...rect, x: rect.x + 0.5 }, fringe: 1, lp: 1 }));
});

// A synth with two candidates, the second active.
function synthWith(gains, lps = [0, 0]) {
  const SR = 48000, s = new GatesSynth(SR);
  s.message({ type: "params", p: { ...AUDIO_DEFAULTS, softClip: false, beltShape: 1 } });
  s.message({ type: "config", lengths: [4, 4], groups: [0, 0] });
  s.message({ type: "space", on: true, gain: Float32Array.from(gains), lp: Float32Array.from(lps), norm: gains[0] + gains[1], groups: [0, 0] });
  const tick = () => {
    const N = 10, bits = new Uint32Array(N).fill(0b10);
    s.message({ type: "ticks", n: N, words: 1, rate: 30, bits, f0: new Float32Array(N), f1: new Float32Array(N) });
  };
  return { s, SR, tick };
}

test("the synth: a run out of earshot takes no voice; a pan gives it one, and takes it back", () => {
  const { s, tick } = synthWith([1, 0]);
  tick();
  s.render(new Float32Array(4096), 4096);
  assert.equal(s.voiceOf[1], -1, "gain 0: no voice, though the run is active");
  assert.equal(s.refused, 0, "and not counted as refused");
  s.message({ type: "space", on: true, gain: Float32Array.from([0, 0.5]), lp: new Float32Array(2), norm: 0.5, groups: [0, 0] });
  assert.ok(s.voiceOf[1] >= 0, "in earshot now: voiced at once, no reconfigure");
  s.message({ type: "space", on: true, gain: Float32Array.from([1, 0]), lp: new Float32Array(2), norm: 1, groups: [0, 0] });
  assert.equal(s.vOn[s.voiceOf[1]], 0, "out again: released");
});

test("the synth: the distance low-pass takes the top off a saw", () => {
  const energy = (lp) => {
    const { s, tick } = synthWith([0, 1], [0, lp]);
    tick();
    const out = new Float32Array(9600);
    s.render(out, 9600);
    // Mean square of the first difference: a high-frequency measure, in
    // proportion to the signal's own level.
    let d = 0, e = 0;
    for (let i = 4801; i < 9600; i++) { d += (out[i] - out[i - 1]) ** 2; e += out[i] ** 2; }
    return d / e;
  };
  const open = energy(0), closed = energy(400);
  assert.ok(closed < open / 10, `filtered ${closed} vs open ${open}`);
});
