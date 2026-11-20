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
import { bakeAudio, wavBytes } from "../audio-bake.js";
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

// --- start tick, fade, hold, baking ---------------------------------------------

function ticksAt(s, tick0, n, rate, active = [], f0 = 0, f1 = 0) {
  const W = s.words, bits = new Uint32Array(n * W);
  for (let k = 0; k < n; k++) for (const i of active) bits[k * W + (i >>> 5)] |= 1 << (i & 31);
  s.message({ type: "ticks", n, tick0, words: W, rate, bits, f0: new Uint16Array(n).fill(f0), f1: new Uint16Array(n).fill(f1) });
}
const peakOf = (a) => a.reduce((m, x) => Math.max(m, Math.abs(x)), 0);

test("silent before the start tick, then a fade in over fadeMs", () => {
  const s = new GatesSynth(SR, { offline: true });
  s.message({ type: "params", p: { ...AUDIO_DEFAULTS, softClip: false, beltShape: 0, startTick: 100, fadeMs: 200 } });
  s.message({ type: "config", lengths: new Float64Array([10]), groups: [0, 0] });
  ticksAt(s, 0, 1000, 1000, [0]);            // 1 tick per ms
  const out = render(s, SR);
  assert.equal(peakOf(out.subarray(0, 99 * 48)), 0, "silent before tick 100");
  const early = peakOf(out.subarray(100 * 48, 110 * 48)), late = peakOf(out.subarray(400 * 48, 500 * 48));
  assert.ok(early > 0 && early < late * 0.15, `fading in: ${early} then ${late}`);
  assert.ok(peakOf(out.subarray(250 * 48, 260 * 48)) < late * 0.9, "still fading at 150 ms");
});

test("hold on underrun: the voices sound through a gap in the ticks, and carry on", () => {
  for (const hold of [false, true]) {
    const s = synth({ holdUnderrun: hold, latencyMs: 20, releaseMs: 5 });
    s.message({ type: "config", lengths: new Float64Array([10]), groups: [0, 0] });
    stream(s, 0.2, 1000, [0]);
    const gap = render(s, SR / 10);          // 100 ms with no ticks
    // The queue's last 20 ms play out first; by the gap's end a release
    // is long finished and a hold is still sounding.
    const tail = peakOf(gap.subarray(gap.length * 0.9));
    if (hold) assert.ok(tail > 0.01, `held: ${tail}`);
    else assert.ok(tail < 1e-4, `released: ${tail}`);
    // And the next ticks carry on at once, with no refill wait.
    if (hold) {
      ticks(s, 5, 1000, [0]);
      render(s, 128);
      assert.ok(s.meter().ticks > 0, "playing again");
    }
  }
});

test("offline, tick k starts at sample k * sampleRate / rate exactly", () => {
  const s = new GatesSynth(SR, { offline: true });
  s.message({ type: "params", p: { ...AUDIO_DEFAULTS, softClip: false } });
  s.message({ type: "config", lengths: new Float64Array(0), groups: [0, 1] });
  const n = 40, W = 0, f1 = new Uint16Array(n);
  f1[37] = 1;                                  // one fire, on tick 37
  s.message({ type: "ticks", n, tick0: 0, words: W, rate: 300, bits: new Uint32Array(0), f0: new Uint16Array(n), f1 });
  const out = render(s, Math.ceil(40 * SR / 300));
  const first = out.findIndex(x => x !== 0);
  assert.equal(first, Math.round(37 * SR / 300));
});

test("WAV: a 16-bit PCM mono header and the samples", () => {
  const buf = wavBytes(new Float32Array([0, 1, -1, 0.5]), 44100), v = new DataView(buf);
  const str = (o, n) => String.fromCharCode(...new Uint8Array(buf, o, n));
  assert.equal(str(0, 4), "RIFF"); assert.equal(str(8, 4), "WAVE"); assert.equal(str(36, 4), "data");
  assert.equal(v.getUint32(24, true), 44100);
  assert.equal(v.getUint16(34, true), 16);
  assert.deepEqual([0, 1, 2, 3].map(i => v.getInt16(44 + 2 * i, true)), [0, 32767, -32768, 16383]);
});

test("baking a real save: the live sim is untouched, a bake is repeatable, restoreFrom replays it", { skip: !existsSync(save) }, async () => {
  const bytes = readFileSync(save);
  const board = await Board.fromNpz(T, bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength));
  const live = new Sim(T, board);
  live.reset();
  const snap = live.clone();
  const before = stateLine(live);
  const params = { ...AUDIO_DEFAULTS, startTick: 20, fadeMs: 50 };
  const opts = { start: snap, params, rate: 240, seconds: 0.5, sampleRate: SR };
  const a = await bakeAudio(opts), b = await bakeAudio(opts);
  assert.equal(stateLine(live), before, "the live sim never moved");
  assert.equal(stateLine(snap), before, "nor did the snapshot");
  assert.equal(a.t0, 20); assert.equal(a.end, 20 + 120);
  assert.equal(a.samples.length, Math.round(120 * SR / 240));
  assert.ok(peakOf(a.samples) > 0, "it made a sound");
  assert.deepEqual(a.samples, b.samples, "the same bake twice is the same audio");

  // Played on, then Reset to the bake's start: the same ticks again.
  for (let i = 0; i < 50; i++) live.step();
  const mid = stateLine(live);
  live.restoreFrom(snap);
  assert.equal(stateLine(live), before, "restoreFrom is the bake's start");
  for (let i = 0; i < 50; i++) live.step();
  assert.equal(stateLine(live), mid, "and replays the same ticks");
});

// --- length window, per-length gain, low cut -------------------------------------

test("only runs between the min and max length are candidates", { skip: !existsSync(save) }, async () => {
  const bytes = readFileSync(save);
  const board = await Board.fromNpz(T, bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength));
  const sim = new Sim(T, board);
  const msgs = [];
  const rec = new Recorder({ postMessage: (m) => msgs.push(m) }, { ...AUDIO_DEFAULTS, beltMinLen: 4, beltCutoff: 12 });
  rec.attach(sim);
  sim.step();
  const want = sim.order.filter(s => s.length >= 4 && s.length <= 12).map(s => s.length);
  assert.ok(want.length > 0 && want.length < sim.order.length);
  assert.deepEqual([...msgs.find(m => m.type === "config").lengths], want);
  rec.setLengths(1, 64);                     // a change reconfigures on the next tick
  sim.step();
  assert.deepEqual([...msgs.filter(m => m.type === "config").pop().lengths],
                   sim.order.filter(s => s.length <= 64).map(s => s.length));
});

test("per-length gain scales lengths 1-6 only, live", () => {
  const level = (L, p, change) => {
    const s = synth({ beltShape: 0, ...p });
    s.message({ type: "config", lengths: new Float64Array([L]), groups: [0, 0] });
    const out = stream(s, 0.5, 1000, [0]);
    if (!change) return peakOf(out.subarray(SR / 4));
    s.message({ type: "params", p: { ...AUDIO_DEFAULTS, softClip: false, beltShape: 0, ...change } });
    return peakOf(stream(s, 0.5, 1000, [0]).subarray(SR / 4));
  };
  const full = level(3, {});
  assert.ok(Math.abs(level(3, { lenGain3: 0.5 }) / full - 0.5) < 0.01, "length 3 at half");
  assert.ok(Math.abs(level(4, { lenGain3: 0.5 }) / level(4, {}) - 1) < 0.01, "length 4 untouched");
  assert.ok(Math.abs(level(7, { lenGain1: 0, lenGain6: 0 }) / level(7, {}) - 1) < 0.01, "7 and up at 1");
  assert.ok(Math.abs(level(3, {}, { lenGain3: 0.25 }) / full - 0.25) < 0.01, "a sounding voice follows the dial");
});

test("the low cut takes out the rumble and leaves the middle", () => {
  const level = (L, cut) => {
    const s = synth({ beltShape: 0, lowCutHz: cut });
    s.message({ type: "config", lengths: new Float64Array([L]), groups: [0, 0] });
    return peakOf(stream(s, 0.6, 1000, [0]).subarray(SR / 3));
  };
  // 1760 / 40 = 44 Hz against a 200 Hz cut; 1760 / 2 = 880 Hz.
  assert.ok(level(40, 200) < level(40, 0) * 0.1, `44 Hz: ${level(40, 200)} vs ${level(40, 0)}`);
  assert.ok(level(2, 200) > level(2, 0) * 0.95, "880 Hz passes");
});
