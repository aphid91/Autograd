// Audio: the synth (audio-dsp.js) rendered under node, and the recorder
// (audio.js) on a real save. No Web Audio here -- the worklet is a thin
// wrapper over GatesSynth, and the recorder only needs a port.

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync, existsSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { GatesSynth } from "../audio-dsp.js";
import { Recorder } from "../audio.js";
import { AUDIO_DEFAULTS, audioGroupOf } from "../audio-params.js";
import { Board } from "../board.js";
import { Sim } from "../sim.js";
import { stateLine } from "./sim_state.mjs";

const here = dirname(fileURLToPath(import.meta.url));
const root = join(here, "..", "..");
const T = JSON.parse(readFileSync(join(here, "..", "types.json"), "utf8"));
const SR = 48000;

function synth(params = {}) {
  const s = new GatesSynth(SR);
  s.message({ type: "params", p: { ...AUDIO_DEFAULTS, softClip: false, ...params } });
  return s;
}

// n ticks at `rate`, every one with the same active set and fire counts.
function ticks(s, n, rate, active = [], f0 = 0, f1 = 0) {
  const W = s.words, bits = new Uint32Array(n * W);
  for (let k = 0; k < n; k++) for (const i of active) bits[k * W + (i >>> 5)] |= 1 << (i & 31);
  s.message({ type: "ticks", n, words: W, rate, bits, f0: new Uint16Array(n).fill(f0), f1: new Uint16Array(n).fill(f1) });
}

// As paint() and the worklet interleave them: each 16 ms frame posts the
// ticks it ran, and the audio thread renders a frame's worth.
function stream(s, seconds, rate, active = [], f0 = 0, f1 = 0) {
  const frame = SR * 16 / 1000, out = new Float32Array(Math.round(seconds * SR));
  let bank = 0;
  for (let at = 0; at < out.length; at += frame) {
    bank += rate * 0.016;
    const n = Math.floor(bank); bank -= n;
    if (n) ticks(s, n, rate, active, f0, f1);
    const end = Math.min(out.length, at + frame);
    for (let i = at; i < end; i += 128) s.render(out.subarray(i, Math.min(end, i + 128)), Math.min(128, end - i));
  }
  return out;
}

function render(s, samples) {
  const out = new Float32Array(samples);
  for (let i = 0; i < samples; i += 128) s.render(out.subarray(i, i + 128), Math.min(128, samples - i));
  return out;
}

// Frequency by upward zero crossings over the steady middle of a buffer.
function freq(buf) {
  const a = buf.subarray(buf.length / 4, buf.length * 3 / 4);
  let first = -1, last = -1, n = 0;
  for (let i = 1; i < a.length; i++) {
    if (a[i - 1] < 0 && a[i] >= 0) { if (first < 0) first = i; last = i; n++; }
  }
  return (n - 1) * SR / (last - first);
}

test("a belt's pitch is the 1-tile pitch over its length: 10 tiles an octave above 20", () => {
  const f = {};
  for (const L of [10, 20]) {
    const s = synth({ beltShape: 0 });
    s.message({ type: "config", lengths: new Float64Array([L]), groups: [0, 0] });
    f[L] = freq(stream(s, 1, 1000, [0]));
  }
  assert.ok(Math.abs(f[10] - AUDIO_DEFAULTS.beltMaxHz / 10) < 1, `10 tiles: ${f[10]} Hz`);
  assert.ok(Math.abs(f[10] / f[20] - 2) < 0.01, `ratio ${f[10] / f[20]}`);
});

test("following the tick rate scales belt pitch by (rate / reference)^track", () => {
  const s = synth({ beltShape: 0, beltTrack: 1, trackRef: 500 });
  s.message({ type: "config", lengths: new Float64Array([10]), groups: [0, 0] });
  const f = freq(stream(s, 1, 1000, [0]));
  assert.ok(Math.abs(f - 2 * AUDIO_DEFAULTS.beltMaxHz / 10) < 2, `${f} Hz`);
});

test("nothing active and nothing firing is silence", () => {
  const s = synth();
  s.message({ type: "config", lengths: new Float64Array([4, 8, 16]), groups: [5, 5] });
  ticks(s, 1000, 1000);
  const out = render(s, SR / 2);
  assert.equal(Math.max(...out.map(Math.abs)), 0);
});

test("fires every tick are a buzz at the tick rate", () => {
  const s = synth({ g1DecayMs: 3 });
  s.message({ type: "config", lengths: new Float64Array(0), groups: [0, 1] });
  const out = stream(s, 1, 1000, [], 0, 1);
  // The clicks repeat every tick (48 samples), more closely than at any
  // shorter lag: the buzz's period is the tick's.
  const a = out.subarray(SR / 2);
  const corr = (lag) => { let c = 0, e = 0; for (let i = 0; i + lag < a.length; i++) { c += a[i] * a[i + lag]; e += a[i] * a[i]; } return c / e; };
  const at = corr(48);
  assert.ok(at > 0.95, `lag 48: ${at}`);
  for (let lag = 8; lag < 44; lag++) assert.ok(corr(lag) < at - 0.05, `lag ${lag}: ${corr(lag)} vs ${at}`);
});

test("playback waits for the latency's worth, and an empty queue falls silent", () => {
  const s = synth({ latencyMs: 100, releaseMs: 10 });
  s.message({ type: "config", lengths: new Float64Array([4]), groups: [0, 0] });
  ticks(s, 50, 1000, [0]);        // 50 ms: not enough to start
  assert.equal(Math.max(...render(s, 4800).map(Math.abs)), 0);
  ticks(s, 100, 1000, [0]);       // 150 ms queued: plays, then runs dry
  const out = render(s, SR);
  assert.ok(Math.max(...out.subarray(0, 4800).map(Math.abs)) > 0, "plays");
  assert.equal(s.meter().voices, 0, "every voice released once the queue ran dry");
  assert.equal(Math.max(...out.subarray(SR / 2).map(Math.abs)), 0, "silent after");
});

test("a deep backlog is skipped, not raced through", () => {
  const s = synth({ latencyMs: 60 });
  s.message({ type: "config", lengths: new Float64Array(0), groups: [0, 1] });
  ticks(s, 1000, 1000, [], 0, 1);     // a whole second at once
  render(s, 128);
  const m = s.meter();
  assert.ok(m.lagMs <= 61, `lag ${m.lagMs} ms`);
  assert.ok(m.speed <= 1.25, `speed ${m.speed}`);
});

test("the voice limit: only active runs take voices, the rest are refused, a freed voice is reused", () => {
  // 13 candidate runs, a limit of 8: with 4 active there is room for all.
  const s = synth({ voiceLimit: 8, releaseMs: 5 });
  s.message({ type: "config", lengths: new Float64Array(13).fill(4), groups: [0, 0] });
  stream(s, 0.3, 1000, [0, 1, 2, 3]);
  let m = s.meter();
  assert.deepEqual([m.voices, m.active, m.refused], [4, 4, 0]);
  // All 13 at once: 8 sound, 5 refused.
  stream(s, 0.3, 1000, [...Array(13).keys()]);
  m = s.meter();
  assert.deepEqual([m.voices, m.active, m.refused], [8, 13, 5]);
  // Two go quiet: once their voices have released, two refused runs take them.
  stream(s, 0.3, 1000, [...Array(13).keys()].filter(i => i !== 0 && i !== 1));
  m = s.meter();
  assert.deepEqual([m.voices, m.active, m.refused], [8, 11, 3]);
});

test("a stop drops the queue and releases everything", () => {
  const s = synth();
  s.message({ type: "config", lengths: new Float64Array([4]), groups: [0, 0] });
  stream(s, 0.5, 1000, [0]);
  s.message({ type: "stop" });
  render(s, SR / 2);
  const m = s.meter();
  assert.equal(m.voices, 0);
  assert.equal(m.lagMs, 0);
});

test("Sources are silent; Swap, Route and Merge are group 0, the rest group 1", () => {
  assert.equal(audioGroupOf("Source"), 2);
  for (const c of ["Swap", "Route", "Merge"]) assert.equal(audioGroupOf(c), 0);
  for (const c of ["Shear", "Mac", "Sink", "Floor", "Max", "Reciprocal"]) assert.equal(audioGroupOf(c), 1);
});

const save = join(root, "cpu.npz");
test("the recorder on a real save: hears every tick but no forecast, and changes nothing", { skip: !existsSync(save) }, async () => {
  const load = async () => {
    const bytes = readFileSync(save);
    const board = await Board.fromNpz(T, bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength));
    return new Sim(T, board);
  };
  const plain = await load(), heard = await load();
  const msgs = [];
  const rec = new Recorder({ postMessage: (m) => msgs.push(m) }, AUDIO_DEFAULTS);
  rec.attach(heard);
  const N = 60;
  for (let i = 0; i < N; i++) {
    plain.step();
    heard.step();
    heard.forecast();               // must not be recorded
    if (i % 10 === 9) rec.flush(30);
  }
  assert.equal(stateLine(heard), stateLine(plain), "recording does not perturb the tick");

  const config = msgs.find(m => m.type === "config");
  const batches = msgs.filter(m => m.type === "ticks");
  assert.equal(batches.reduce((n, b) => n + b.n, 0), N, "one recorded tick per step");
  const cands = heard.order.filter(s => s.length <= AUDIO_DEFAULTS.beltCutoff);
  assert.deepEqual([...config.lengths], cands.map(s => s.length));
  const groups = [0, 0];
  for (const g of heard.gates) { const k = audioGroupOf(g.td.class); if (k < 2) groups[k]++; }
  assert.deepEqual(config.groups, groups);
  assert.ok(heard.gates.some(g => g.td.class === "Source"), "the save has Sources to leave out");

  // Some runs move and some gates fire, and no more fire than there are.
  let fires = 0, anyActive = false;
  for (const b of batches) {
    for (let k = 0; k < b.n; k++) {
      assert.ok(b.f0[k] <= groups[0] && b.f1[k] <= groups[1]);
      fires += b.f0[k] + b.f1[k];
    }
    if (b.bits.some(w => w !== 0)) anyActive = true;
  }
  assert.ok(fires > 0, "gates fired");
  assert.ok(anyActive, "runs moved");

  // Detached, the sim no longer calls it.
  rec.detach();
  assert.equal(heard.audio, null);
});
