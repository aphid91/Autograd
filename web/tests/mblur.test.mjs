// Motion blur's timing (mblur.js) and the worker's slices (simhost.js).
//
// The claim the realtime blur rests on: every sample is a REAL state. The
// worker cuts its delta at the grid's ticks, and the screen steps its mirror
// through the slices, drawing at each grid point -- so at every grid point
// the mirror must equal an ordinary Sim stepped to exactly that tick. Tested
// tick for tick against such a reference, on a real save, at grid spacings
// below one tick, near one and many ticks apart.

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync, existsSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { Board } from "../board.js";
import { Sim } from "../sim.js";
import { installPixel, Screen } from "../pixel.js";
import { SimHost, applyDelta, boardMessage, MAX_CHUNKS } from "../simhost.js";
import { isCut, gridSpacing, gridSampleTimes, frameSampleTimes, lerpCam } from "../mblur.js";
import { stateLine } from "./sim_state.mjs";

const here = dirname(fileURLToPath(import.meta.url));
const root = join(here, "..", "..");
const T = installPixel(JSON.parse(readFileSync(join(here, "..", "types.json"), "utf8")));

async function loadBoard(path) {
  const bytes = readFileSync(path);
  return Board.fromNpz(T, bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength));
}

test("a tick is cut exactly when some grid point floors to it", () => {
  for (const g of [0.3, 0.999, 1, 1.7, 2.5, 7.5, 40, 333.33, 1234.5678]) {
    const want = new Set();
    for (let m = 0; m * g < 5000; m++) want.add(Math.floor(m * g));
    for (let n = 0; n < 4990; n++) assert.equal(isCut(n, g), want.has(n), `g ${g}, tick ${n}`);
  }
  assert.equal(isCut(10, 0), false, "no grid, no cuts");
});

test("a frame's grid points are the ones in its span, oldest first", () => {
  const g = 2.5;
  const t = gridSampleTimes(10.2, 20, g);
  assert.deepEqual(t, [12.5, 15, 17.5, 20], "in (S, E], E included");
  assert.deepEqual(gridSampleTimes(10, 10, g), [], "an empty span has none");
  assert.deepEqual(gridSampleTimes(12, 10, g), [], "a span backwards has none");
  // Thinned evenly to the cap, the newest kept.
  const many = gridSampleTimes(0, 1000, 1, 8);
  assert.equal(many.length, 8);
  assert.equal(many[many.length - 1], 1000, "the newest is the frame's own state");
  for (let i = 1; i < many.length; i++) assert.ok(many[i] > many[i - 1]);
  assert.ok(Math.abs(gridSpacing(12000, 1 / 60, 5) - 40) < 1e-9, "12000 tps, 60 fps, 5 samples: 40 ticks apart");
});

test("a video frame's samples end on the frame and step back evenly", () => {
  const t = frameSampleTimes(400, 200, 5);
  assert.deepEqual(t, [240, 280, 320, 360, 400]);
  const f = frameSampleTimes(0.3, 0.5, 2, 0);
  assert.ok(Math.abs(f[0] - 0.05) < 1e-12 && f[1] === 0.3, "fractional");
  assert.deepEqual(frameSampleTimes(100, 200, 3, 50), [50, 50, 100], "never before the video's start");
});

test("the camera moves linearly in centre and geometrically in zoom", () => {
  const a = { cx: 0, cy: 10, zoom: 4 }, b = { cx: 8, cy: -2, zoom: 64 };
  assert.deepEqual(lerpCam(a, b, 0), a);
  assert.deepEqual(lerpCam(a, b, 1), b);
  const m = lerpCam(a, b, 0.5);
  assert.equal(m.cx, 4); assert.equal(m.cy, 4);
  assert.ok(Math.abs(m.zoom - 16) < 1e-9, "halfway in zoom is the geometric mean");
});

// The worker ticks on a fake clock through its real loop (slice(), which is
// where it cuts); the main side steps its mirror through each reply's slices
// at the grid points, as paint() does, and compares with the reference.
async function sliced(g, rate, frames) {
  const path = join(root, "cpu.npz");
  let now = 0;
  const inbox = [];
  const host = new SimHost(T, (m) => inbox.push(structuredClone(m)), () => now);
  const mirror = new Sim(T, await loadBoard(path));
  mirror.screen = new Screen();
  const ref = new Sim(T, await loadBoard(path));
  ref.screen = new Screen();
  host.message(structuredClone({ ...boardMessage(mirror, mirror.lastAlloc, true), transfer: undefined, epoch: 1 }));
  host.message({ type: "pixels", on: true });
  host.message({ type: "blur", g });
  host.message({ type: "play", rate, frac: 0 });
  host.slice();
  let S = mirror.tick, samples = 0, sliceCount = 0;
  for (let f = 0; f < frames; f++) {
    now += 1000 / 60;
    host.message({ type: "pull" });   // keeps the loop from idling; its reply comes first
    inbox.length = 0;
    host.slice();
    host.message({ type: "pull" });
    const d = inbox.pop();
    const q = [...(d.chunks || []), d];
    sliceCount += (d.chunks || []).length;
    const E = d.tick;
    for (const t of gridSampleTimes(S, E, g)) {
      while (q.length && q[0].tick <= Math.floor(t)) applyDelta(mirror, q.shift(), mirror.screen);
      while (ref.tick < Math.floor(t)) ref.step();
      assert.equal(mirror.tick, Math.floor(t), `g ${g}: the mirror is at the sample's own tick ${Math.floor(t)}`);
      assert.equal(stateLine(mirror), stateLine(ref), `g ${g}: the state at grid point ${t}`);
      samples++;
    }
    while (q.length) applyDelta(mirror, q.shift(), mirror.screen);
    while (ref.tick < mirror.tick) ref.step();
    assert.equal(stateLine(mirror), stateLine(ref), `g ${g}: the frame's final state`);
    S = E;
  }
  return { samples, sliceCount };
}

for (const [g, rate] of [[0.4, 30], [1, 600], [7.5, 600], [40, 12000]]) {
  test(`every grid point is a real state: grid ${g} at ${rate} tps`, { skip: !existsSync(join(root, "cpu.npz")) }, async () => {
    const { samples, sliceCount } = await sliced(g, rate, 30);
    assert.ok(samples > 20, `vacuous: only ${samples} samples checked`);
    assert.ok(sliceCount > 0, "the worker cut no slices");
  });
}

test("blur off: the worker cuts nothing", { skip: !existsSync(join(root, "cpu.npz")) }, async () => {
  let now = 0;
  const inbox = [];
  const host = new SimHost(T, (m) => inbox.push(m), () => now);
  const mirror = new Sim(T, await loadBoard(join(root, "cpu.npz")));
  host.message(structuredClone({ ...boardMessage(mirror, mirror.lastAlloc, true), transfer: undefined, epoch: 1 }));
  host.message({ type: "play", rate: 600, frac: 0 });
  host.slice();
  for (let f = 0; f < 10; f++) { now += 1000 / 60; host.message({ type: "pull" }); host.slice(); }
  assert.ok(inbox.every(d => !d.chunks), "no slices with blur off");
  assert.ok(MAX_CHUNKS >= 64);
});
