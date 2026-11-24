// Video export's pure half (video-params.js): the camera move and the
// settings. video.js itself needs a browser (mediabunny, WebCodecs).

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync, existsSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { moveProgress, cropBox, lerpBox, frameBox, cleanVideo, evenDim, frameCount, tickCount, VIDEO_DEFAULTS } from "../video-params.js";
import { bakeAudio } from "../audio-bake.js";
import { AUDIO_DEFAULTS } from "../audio-params.js";
import { Board } from "../board.js";
import { Sim } from "../sim.js";
import { RATE_MAX } from "../rate.js";

const near = (a, b, e = 1e-9) => Math.abs(a - b) < e;

test("the move: still outside its window, 0 to 1 inside, continuous and never backwards", () => {
  for (const [fi, fo] of [[0, 0], [0.25, 0.25], [0.5, 0.5], [1, 0], [0, 1], [0.8, 0.9]]) {
    const s = { lerpStart: 0.5, lerpEnd: 1, fadeIn: fi, fadeOut: fo };
    assert.equal(moveProgress(0, s), 0);
    assert.equal(moveProgress(0.5, s), 0, "static before the window");
    assert.equal(moveProgress(1, s), 1);
    let prev = 0;
    for (let i = 0; i <= 1000; i++) {
      const p = moveProgress(i / 1000, s);
      assert.ok(p >= prev - 1e-12 && p <= 1 + 1e-12, `fi ${fi} fo ${fo} at ${i}`);
      assert.ok(p - prev < 0.01, "no jumps");
      prev = p;
    }
  }
  // No fades: constant speed, so linear across the window.
  const lin = { lerpStart: 0.2, lerpEnd: 0.6, fadeIn: 0, fadeOut: 0 };
  assert.ok(near(moveProgress(0.4, lin), 0.5));
  assert.ok(near(moveProgress(0.3, lin), 0.25));
  // Equal fades are symmetric: halfway through the window is halfway there.
  assert.ok(near(moveProgress(0.5, { lerpStart: 0, lerpEnd: 1, fadeIn: 0.3, fadeOut: 0.3 }), 0.5));
  // A fade starts slow: in its first part the move is behind the linear one.
  assert.ok(moveProgress(0.1, { lerpStart: 0, lerpEnd: 1, fadeIn: 0.5, fadeOut: 0 }) < 0.1);
  // A zero-length window jumps at its moment.
  const pt = { lerpStart: 0.5, lerpEnd: 0.5, fadeIn: 0, fadeOut: 0 };
  assert.equal(moveProgress(0.49, pt), 0); assert.equal(moveProgress(0.5, pt), 1);
});

test("the crop box is the largest box of the video's aspect inside the view", () => {
  const cam = { cx: 10, cy: 20, zoom: 10, w: 400, h: 800 };    // a portrait phone view: 40 x 80 cells
  const wide = cropBox(cam, 16 / 9);
  assert.ok(near(wide.hw, 20) && near(wide.hh, 20 * 9 / 16), "width-limited");
  const tall = cropBox(cam, 0.25);
  assert.ok(near(tall.hh, 40) && near(tall.hw, 10), "height-limited");
  assert.equal(wide.cx, 10); assert.equal(wide.cy, 20);
});

test("the camera lerps its centre linearly and its size geometrically; still without an end", () => {
  const a = { cx: 0, cy: 0, hw: 10, hh: 5 }, b = { cx: 100, cy: -50, hw: 40, hh: 20 };
  const m = lerpBox(a, b, 0.5);
  assert.ok(near(m.cx, 50) && near(m.cy, -25));
  assert.ok(near(m.hw, 20) && near(m.hh, 10), "halfway in a 4x zoom is 2x");
  const s = { ...VIDEO_DEFAULTS, width: 1920, height: 1080 };
  const start = { cx: 0, cy: 0, zoom: 10, w: 1920, h: 1080 }, end = { cx: 50, cy: 50, zoom: 40, w: 1920, h: 1080 };
  assert.deepEqual(frameBox(start, null, s, 300, 600), cropBox(start, 16 / 9));
  assert.deepEqual(frameBox(start, end, s, 0, 600), cropBox(start, 16 / 9));
  const last = frameBox(start, end, s, 599, 600), want = cropBox(end, 16 / 9);
  assert.ok(near(last.cx, want.cx) && near(last.hw, want.hw), "the last frame is the end camera");
});

test("settings: cleaned on load, even sizes, counts", () => {
  assert.deepEqual(cleanVideo(null), VIDEO_DEFAULTS);
  const c = cleanVideo({ width: 99999, height: 0, duration: -3, tps: 1e9, quality: "ultra", lerpEnd: 2, fadeIn: "x" });
  assert.equal(c.width, 3840); assert.equal(c.height, 0, "0 means 'the screen's size'");
  assert.equal(c.duration, 1); assert.equal(c.tps, RATE_MAX, "video ticks/s is capped at the sim's own cap"); assert.equal(c.quality, VIDEO_DEFAULTS.quality);
  assert.equal(c.lerpEnd, 1); assert.equal(c.fadeIn, VIDEO_DEFAULTS.fadeIn);
  assert.equal(evenDim(1081), 1080); assert.equal(evenDim(5), 128);
  assert.equal(frameCount({ duration: 10 }), 600);
  assert.equal(tickCount({ duration: 10, tps: 30 }), 300);
});

const save = join(dirname(fileURLToPath(import.meta.url)), "..", "..", "cpu.npz");
test("a bake can start mid-run on the video's own first tick", { skip: !existsSync(save) }, async () => {
  const T = JSON.parse(readFileSync(join(dirname(fileURLToPath(import.meta.url)), "..", "types.json"), "utf8"));
  const bytes = readFileSync(save);
  const board = await Board.fromNpz(T, bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength));
  const sim = new Sim(T, board);
  for (let i = 0; i < 37; i++) sim.step();
  const r = await bakeAudio({ start: sim, params: { ...AUDIO_DEFAULTS, startTick: 50 }, rate: 120, seconds: 0.5, sampleRate: 48000, t0: sim.tick });
  assert.equal(r.t0, 37, "starts with the video, not at the start tick");
  assert.equal(r.end, 37 + 60);
  assert.equal(r.samples.length, 60 * 400, "exactly the video's ticks");
  const silentUntil = (50 - 37) * 400;   // the start tick (50) still silences what comes before it
  assert.equal(r.samples.subarray(0, silentUntil - 1).reduce((m, x) => Math.max(m, Math.abs(x)), 0), 0);
  assert.equal(sim.tick, 37, "the live sim is untouched");
});

test("the shared camera path: a still without an end, clamped, and a box fitted to any screen", async () => {
  const { pathBox, fitBox, viewBox } = await import("../video-params.js");
  const s = { lerpStart: 0, lerpEnd: 1, fadeIn: 0, fadeOut: 0 };
  const a = { cx: 0, cy: 0, hw: 10, hh: 5 }, b = { cx: 10, cy: 0, hw: 40, hh: 20 };
  assert.deepEqual(pathBox(a, null, s, 0.5), a);
  assert.deepEqual(pathBox(a, b, s, -1), pathBox(a, b, s, 0));
  assert.deepEqual(pathBox(a, b, s, 2), b);
  assert.equal(pathBox(a, b, s, 0.5).hw, 20, "size geometric: halfway between 10 and 40 is 20");
  // A 20x10 box fitted to a tall screen: the width decides; all of it shows.
  const f = fitBox(a, 400, 800);
  assert.equal(f.zoom, 20);
  const back = viewBox({ cx: f.cx, cy: f.cy, zoom: f.zoom, w: 400, h: 800 });
  assert.ok(back.hw >= a.hw - 1e-9 && back.hh >= a.hh - 1e-9);
});
