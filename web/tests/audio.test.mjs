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
import { AUDIO_DEFAULTS, audioGroupOf, audioRecord, parseAudio, BAKE_MAX_SECONDS } from "../audio-params.js";
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
  const s = synth({ g1DecayMs: 3, clickJitter: 0 });
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
  s.message({ type: "params", p: { ...AUDIO_DEFAULTS, softClip: false, clickJitter: 0 } });
  s.message({ type: "config", lengths: new Float64Array(0), groups: [0, 1] });
  const n = 40, W = 0, f1 = new Uint16Array(n);
  f1[37] = 1;                                  // one fire, on tick 37
  s.message({ type: "ticks", n, tick0: 0, words: W, rate: 300, bits: new Uint32Array(0), f0: new Uint16Array(n), f1 });
  const out = render(s, Math.ceil(40 * SR / 300));
  const first = out.findIndex(x => x !== 0);
  assert.equal(first, Math.round(37 * SR / 300));
});

// --- click jitter and detune ------------------------------------------------------

// An offline synth fed n ticks at `rate` with f1 fires per tick, rendered in
// chunks of `chunk` samples.
function clickBake(params, n, rate, f1, chunk = 128) {
  const s = new GatesSynth(SR, { offline: true });
  s.message({ type: "params", p: { ...AUDIO_DEFAULTS, softClip: false, ...params } });
  s.message({ type: "config", lengths: new Float64Array(0), groups: [0, 1] });
  const fires = typeof f1 === "number" ? new Uint16Array(n).fill(f1) : f1;
  s.message({ type: "ticks", n, tick0: 0, words: 0, rate, bits: new Uint32Array(0), f0: new Uint16Array(n), f1: fires });
  const total = Math.ceil((n + 2) * SR / rate), out = new Float32Array(total);
  for (let at = 0; at < total; at += chunk) { const k = Math.min(chunk, total - at); s.render(out.subarray(at, at + k), k); }
  return out;
}

test("jitter 1: a fire clicks somewhere inside its own tick, not at its start", () => {
  const n = 40, f1 = new Uint16Array(n);
  f1[37] = 1;
  const out = clickBake({ clickJitter: 1 }, n, 300, f1);
  const first = out.findIndex(x => x !== 0), t0 = Math.round(37 * SR / 300), t1 = Math.round(38 * SR / 300);
  assert.ok(first > t0 && first < t1, `${first} in (${t0}, ${t1})`);
});

test("jitter: a tick's fires are separate clicks spread through it, and a bake is the same however it is chunked", () => {
  // 8 fires on one tick at 10 tps: 4800 samples to spread over. At jitter 0
  // they are one impulse; at 1, eight onsets.
  const n = 4, f1 = new Uint16Array(n);
  f1[1] = 8;
  const onsets = (a) => {           // a click starting: sound after 100 samples of near silence
    let c = 0, quiet = 100;
    for (let i = 0; i < a.length; i++) {
      if (Math.abs(a[i]) < 1e-6) quiet++;
      else { if (quiet >= 100) c++; quiet = 0; }
    }
    return c;
  };
  const p = { g1DecayMs: 0.3, g1Tone: 3100 };
  assert.equal(onsets(clickBake({ ...p, clickJitter: 0 }, n, 10, f1)), 1);
  const a = clickBake({ ...p, clickJitter: 1 }, n, 10, f1);
  assert.ok(onsets(a) >= 6, `onsets ${onsets(a)}`);
  const b = clickBake({ ...p, clickJitter: 1 }, n, 10, f1, 97);
  assert.deepEqual(Buffer.from(a.buffer), Buffer.from(b.buffer), "bit-identical across chunking");
});

test("jitter 1 breaks the tick-rate buzz: one fire a tick no longer repeats at the tick's period", () => {
  const out = clickBake({ g1DecayMs: 1, clickJitter: 1 }, 1000, 1000, 1);
  const a = out.subarray(SR / 2);
  let c = 0, e = 0;
  for (let i = 0; i + 48 < a.length; i++) { c += a[i] * a[i + 48]; e += a[i] * a[i]; }
  assert.ok(c / e < 0.5, `lag 48: ${c / e}`);
});

test("detune: 0 is one resonator; above 0 the clicks ring at several tones", () => {
  // Energy near the tone vs a semitone-and-more away, by a single DFT bin.
  const bin = (a, hz) => { let re = 0, im = 0; for (let i = 0; i < a.length; i++) { const w = 2 * Math.PI * hz * i / SR; re += a[i] * Math.cos(w); im += a[i] * Math.sin(w); } return Math.hypot(re, im); };
  const p = { g1DecayMs: 60, g1Tone: 1000, clickJitter: 1 };
  const one = clickBake({ ...p, clickDetune: 0 }, 200, 50, 4);
  const many = clickBake({ ...p, clickDetune: 4 }, 200, 50, 4);
  const up = 1000 * Math.pow(2, 4 / 12);       // the top resonator
  assert.ok(bin(one, up) < 0.2 * bin(one, 1000), "undetuned: one tone");
  assert.ok(bin(many, up) > 0.3 * bin(many, 1000), "detuned: the spread is heard");
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

// --- audio settings in a save ------------------------------------------------------

test("a save carries its audio settings through write and read; older saves have none", { skip: !existsSync(save) }, async () => {
  const bytes = readFileSync(save);
  const board = await Board.fromNpz(T, bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength));
  assert.equal(board.audio, null, "a save from before has no audio settings");
  const dials = { ...AUDIO_DEFAULTS, beltMaxHz: 990, lenGain2: 0.25, softClip: false, startTick: 120 };
  board.audio = audioRecord(dials, { seconds: 12, rate: 480 });
  const out = new Uint8Array(await board.toNpz());
  const back = await Board.fromNpz(T, out.buffer);
  assert.deepEqual(back.audio, { dials, bake: { seconds: 12, rate: 480 } });
  // Through the autosave's path too (a snapshot).
  const snapOut = new Uint8Array(await board.toNpz(board.snapshot()));
  assert.deepEqual((await Board.fromNpz(T, snapOut.buffer)).audio.dials, dials);
});

test("a save's audio record is cleaned: unknown, mistyped and out-of-range dials go, the rest stay", () => {
  const rec = parseAudio(JSON.stringify({
    dials: { master: 0.8, beltCutoff: 99999, fadeMs: -5, softClip: "yes", nonsense: 3, beltShape: NaN, lenGain1: 0.3 },
    bake: { seconds: 9999, rate: 30 },
  }));
  assert.deepEqual(rec.dials, { master: 0.8, beltCutoff: 512, fadeMs: 0, lenGain1: 0.3 });
  assert.deepEqual(rec.bake, { seconds: BAKE_MAX_SECONDS, rate: 30 });
  assert.equal(parseAudio("{not json"), null, "a corrupt blob costs the settings, not the board");
  assert.equal(parseAudio(""), null);
  assert.equal(parseAudio(JSON.stringify({ dials: {}, bake: { seconds: -1, rate: 5 } })).bake, null);
});

// --- the sound selection ----------------------------------------------------------

async function loadCpu() {
  const bytes = readFileSync(save);
  return Board.fromNpz(T, bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength));
}

test("the board's content hash: same after lifting and putting back in place, ticking, or a label edit; not after a move or a value change", { skip: !existsSync(save) }, async () => {
  const board = await loadCpu();
  const h0 = board.contentHash();
  // Ticking rewrites gate channels in the entity table: not content.
  const sim = new Sim(T, board);
  for (let i = 0; i < 30; i++) sim.step();
  assert.equal(board.contentHash(), h0, "ticking is not a change");
  // Lift some machines and put them back where they were: new rows, same board.
  const rows = board.structs.slice(0, 6).map(s => s.row);
  const entries = board.capture(rows);
  board.eraseRows(rows);
  assert.notEqual(board.contentHash(), h0, "lifted is different");
  board.placeEntries(entries, entries.map(e => [e.dx, e.dy]));
  assert.equal(board.contentHash(), h0, "put back in place is the same board");
  board.notes.push({ kind: "text", colour: "white", size: 0.9, text: "x", rect: [0, 0, 2, 1] });
  assert.equal(board.contentHash(), h0, "a label is not sound content");
  // Moved: a change.
  const one = board.structs[0].row, [e] = board.capture([one]);
  board.eraseRows([one]);
  let moved = false;
  for (const [mx, my] of [[0, 40], [40, 0], [0, -40], [-40, 0]]) {
    if (!board.entryBlocked(e, e.dx + mx, e.dy + my).length) { board.placeEntries([e], [[e.dx + mx, e.dy + my]]); moved = true; break; }
  }
  assert.ok(moved);
  const h1 = board.contentHash();
  assert.notEqual(h1, h0, "moved is a change");
  // A Source's value is content.
  const src = board.structs.find(s => T.types[s.type].name === "source");
  board.setEmit(src.row, 12.5);
  assert.notEqual(board.contentHash(), h1, "a Source's value is content");
});

test("a sound selection: only runs and gates touching its cells are heard, and the normalisation counts them", { skip: !existsSync(save) }, async () => {
  const board = await loadCpu();
  const sim = new Sim(T, board), W = board.width;
  const seg = sim.order.find(s => s.length >= 3 && s.length <= 10);
  const gate = sim.gates.find(g => g.td.class === "Shear");
  const [gx, gy] = sim.originOf(gate.row);
  // One cell of the run (its middle: a part selects the whole run) and one of the gate.
  const [mx, my] = seg.cells[1];
  const cells = new Set([my * W + mx, gy * W + gx]);
  const msgs = [];
  const rec = new Recorder({ postMessage: (m) => msgs.push(m) }, AUDIO_DEFAULTS, cells);
  rec.attach(sim);
  sim.step();
  const cfg = msgs.find(m => m.type === "config");
  const want = sim.order.filter(s => s.length <= AUDIO_DEFAULTS.beltCutoff && s.cells.some(([x, y]) => cells.has(y * W + x)));
  assert.deepEqual([...cfg.lengths], want.map(s => s.length));
  assert.ok(want.includes(seg));
  assert.deepEqual(cfg.groups, [0, 1], "one gate, group 1");
  assert.deepEqual(rec.selInfo, { runs: want.length, gates: 1 });
  // Every other gate fires into the silent group.
  assert.ok(sim.gates.filter(g => g !== gate).every(g => g.agroup === 2));
  // Cleared: the whole board again.
  rec.setSelection(null);
  sim.step();
  assert.ok(msgs.filter(m => m.type === "config").pop().lengths.length > want.length);
  assert.equal(rec.selInfo, null);
});

test("sluggishness: attack and release scale with (length / 10)^s, exact for a 10-tile run", () => {
  const coef = (ms) => 1 - Math.exp(-1 / (SR * ms / 1000));
  for (const s of [0, 1, -1, 2]) {
    const syn = synth({ attackMs: 20, releaseMs: 80, sluggish: s });
    syn.message({ type: "config", lengths: new Float64Array([10, 20, 5]), groups: [0, 0] });
    stream(syn, 0.2, 1000, [0, 1, 2]);
    const v = (i) => syn.voiceOf[i];
    for (const [i, L] of [[0, 10], [1, 20], [2, 5]]) {
      const k = Math.pow(L / 10, s);
      assert.ok(Math.abs(syn.vAtk[v(i)] - coef(20 * k)) < 1e-12, `s ${s} L ${L} attack`);
      assert.ok(Math.abs(syn.vRel[v(i)] - coef(80 * k)) < 1e-12, `s ${s} L ${L} release`);
    }
  }
  // And a sounding voice follows the dial.
  const syn = synth({ attackMs: 20, sluggish: 0 });
  syn.message({ type: "config", lengths: new Float64Array([20]), groups: [0, 0] });
  stream(syn, 0.2, 1000, [0]);
  syn.message({ type: "params", p: { ...AUDIO_DEFAULTS, softClip: false, attackMs: 20, sluggish: 1 } });
  assert.ok(Math.abs(syn.vAtk[syn.voiceOf[0]] - coef(40)) < 1e-12);
});

test("min-length Hz: the dial is a min-length run's pitch, so a run twice as long is an octave down", () => {
  const s = synth({ beltShape: 0, beltMinLen: 2 });
  s.message({ type: "config", lengths: new Float64Array([4]), groups: [0, 0] });
  const f = freq(stream(s, 1, 1000, [0]));
  assert.ok(Math.abs(f - AUDIO_DEFAULTS.beltMaxHz / 2) < 2, `${f} Hz`);
});

test("pitch tilt: gain times (length / min length)^tilt, capped at +24 dB", () => {
  const level = (L, p) => {
    const s = synth({ beltShape: 0, ...p });
    s.message({ type: "config", lengths: new Float64Array([L]), groups: [0, 0] });
    return peakOf(stream(s, 0.5, 1000, [0]).subarray(SR / 4));
  };
  const base = { beltMinLen: 4 };
  assert.ok(Math.abs(level(16, { ...base, pitchTilt: 1 }) / level(16, base) - 4) < 0.05, "tilt 1: 4x the length, 4x the amplitude");
  assert.ok(Math.abs(level(8, { ...base, pitchTilt: 2 }) / level(8, base) - 4) < 0.05, "tilt 2: 2x the length, 4x");
  assert.ok(Math.abs(level(4, { ...base, pitchTilt: 2 }) / level(4, base) - 1) < 0.01, "a min-length run is unchanged");
  const cap = Math.pow(10, 24 / 20);
  assert.ok(Math.abs(level(64, { ...base, beltCutoff: 64, pitchTilt: 2, master: 0.01 }) / level(64, { ...base, beltCutoff: 64, master: 0.01 }) - cap) < 0.1, "capped at +24 dB");
});

test("a slow tick rate keeps sounding: no crash, no dropouts, and back to fast after", () => {
  // 1000 tps, then 5 and 10 (one tick outlasts the 60 ms latency), then 1000.
  // Once, the start-up took the only queued tick and the skip check, on its
  // stale figure, skip()'d an empty queue: a throw, which silences a real
  // AudioWorkletProcessor for good.
  const s = synth({ releaseMs: 40 });
  s.message({ type: "config", lengths: new Float64Array([4, 8]), groups: [0, 1] });
  const quietest = (out) => {
    let lo = Infinity;
    for (let i = 0; i + 4800 <= out.length; i += 4800) lo = Math.min(lo, peakOf(out.subarray(i, i + 4800)));
    return lo;
  };
  stream(s, 1, 1000, [0, 1], 0, 1);
  for (const rate of [5, 10, 2]) {
    const out = stream(s, 3, rate, [0, 1], 0, 1);
    assert.ok(quietest(out.subarray(SR)) > 0.05, `${rate} tps: never silent for 100 ms once started`);
  }
  const back = stream(s, 1, 1000, [0, 1], 0, 1);
  assert.ok(quietest(back.subarray(SR / 4)) > 0.05, "fast again");
});

test("at 1 tps every tick is played, one a second", () => {
  // A flat 1 s cap on the queue held less than the 1.5 ticks playback waits
  // for, so it dropped ticks: the drone moved every other tick, or not at all.
  for (const rate of [0.5, 1, 2]) {
    const s = synth();
    s.message({ type: "config", lengths: new Float64Array([4]), groups: [0] });
    stream(s, 14 / rate, rate, [0], 0, 0);
    const behind = 14 - s.ticksPlayed;
    assert.ok(behind >= 1 && behind <= 3, `${rate} tps: ${s.ticksPlayed} of 14 ticks played`);
  }
});

// Realtime rendering by block (renderBlock()) against the old renderer,
// which cut every block at every tick boundary and is kept as its
// reference (`batch` false): a real save's ticks, streamed a frame at a
// time as the page does, rendered both ways, from 30 to 100k ticks a
// second. Bit for bit -- voices allocated and freed, refused runs, jittered
// and detuned clicks -- since the walk applies the same ticks at the same
// samples and only the order of the work differs. With a spatial weighting
// the one deliberate difference shows: a voice's gain glides once a block,
// not at each tick boundary inside it, so that run is held to within a
// thousandth of the signal instead.
async function renderBoth(rate, extra, space) {
  const bytes = readFileSync(save);
  const sim = new Sim(T, await Board.fromNpz(T, bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength)));
  for (let i = 0; i < 300; i++) sim.step();
  const params = { ...AUDIO_DEFAULTS, softClip: false, ...extra };
  const msgs = [];
  const rec = new Recorder({ postMessage: (m) => msgs.push(m) }, params);
  rec.attach(sim);
  const A = synth(params), B = synth(params);
  B.batch = false;
  const oa = new Float32Array(128), ob = new Float32Array(128);
  let bank = 0, blocks = 0, voiced = 0, sig = 0, diff = 0, first = null;
  for (let f = 0; f < 30; f++) {
    bank += rate / 60;
    const n = Math.floor(bank); bank -= n;
    for (let i = 0; i < n; i++) sim.step();
    rec.flush(rate);
    if (space && f === 10) {   // the camera moves: a spatial weighting arrives mid-run
      const gain = new Float32Array(A.n).map((_, i) => (i % 3) / 2), lp = new Float32Array(A.n).map((_, i) => (i % 2) * 2000);
      msgs.push({ type: "space", on: true, gain, lp, norm: A.n / 2, groups: A.groups });
    }
    for (const m of msgs.splice(0)) { A.message(structuredClone(m)); B.message(structuredClone(m)); }
    for (; blocks < Math.round((f + 1) * SR / 60 / 128); blocks++) {
      A.render(oa, 128); B.render(ob, 128);
      for (let s = 0; s < 128; s++) {
        const d = oa[s] - ob[s];
        if (d !== 0 && first === null) first = `block ${blocks} sample ${s}: ${oa[s]} vs ${ob[s]}`;
        diff += d * d; sig += ob[s] * ob[s];
      }
      voiced = Math.max(voiced, B.nv);
    }
  }
  assert.ok(sig > 1 && voiced > 10, `${rate} tps: something sounded (${voiced} voices)`);
  return { first, ratio: Math.sqrt(diff / sig) };
}

test("realtime by block is the per-tick renderer to the bit, at any tick rate", { skip: !existsSync(save) }, async () => {
  for (const [rate, extra] of [[30, {}], [1000, { clickDetune: 3, startTick: 450, fadeMs: 30 }], [5000, { clickJitter: 0 }],
                               [20000, { voiceLimit: 64 }], [100000, { clickJitter: 2 }]]) {
    const r = await renderBoth(rate, extra, false);
    assert.equal(r.first, null, `${rate} tps: the two renderers differ at ${r.first}`);
  }
});

test("realtime by block, with a spatial weighting: within a thousandth of the per-tick renderer", { skip: !existsSync(save) }, async (t) => {
  for (const rate of [30, 20000]) {
    const r = await renderBoth(rate, {}, true);
    t.diagnostic(`${rate} tps: difference ${r.ratio.toExponential(2)} of the signal`);
    assert.ok(r.first !== null, `${rate} tps: the glide is stepped differently, so some difference is expected`);
    assert.ok(r.ratio < 1e-3, `${rate} tps: difference ${r.ratio.toExponential(2)} of the signal`);
  }
});

// A block's clicks are all scheduled before any plays, so a busy one can
// hold far more than the old renderer ever had pending: 300k ticks a second
// of 64 fires each is 51k clicks a block. The pool grows to hold them, or
// the overflow would all land on the block's first sample.
test("realtime by block: a block of many thousands of clicks lands each where the per-tick renderer does", () => {
  const A = synth({ clickJitter: 4 }), B = synth({ clickJitter: 4 });
  B.batch = false;
  for (const s of [A, B]) s.message({ type: "config", lengths: new Float64Array([10]), groups: [0, 64] });
  const oa = new Float32Array(128), ob = new Float32Array(128), rate = 300000;
  for (let f = 0; f < 12; f++) {
    const n = rate / 60, m = { type: "ticks", n, words: 1, rate, bits: new Uint32Array(n), f0: new Uint16Array(n), f1: new Uint16Array(n).fill(64) };
    A.message(structuredClone(m)); B.message(structuredClone(m));
    for (let b = 0; b < 6; b++) {
      A.render(oa, 128); B.render(ob, 128);
      for (let s = 0; s < 128; s++) assert.equal(oa[s], ob[s], `frame ${f} block ${b} sample ${s}`);
    }
  }
  assert.ok(A.evAt.length > 8192, `the pool grew (${A.evAt.length})`);
});

// Ticks can reach the synth by a second port, straight from the sim worker,
// while configs come from the main thread: a batch from another recorder
// generation is dropped, whatever its words.
test("a tick batch from an older recorder configuration is dropped", () => {
  const s = synth();
  s.message({ type: "config", gen: 2, lengths: new Float64Array([10]), groups: [0, 0] });
  const batch = (gen) => ({ type: "ticks", gen, n: 5, words: 1, rate: 100, bits: new Uint32Array(5).fill(1), f0: new Uint16Array(5), f1: new Uint16Array(5) });
  s.message(batch(1));
  assert.equal(s.queued, 0, "an older generation is dropped");
  s.message(batch(2));
  assert.equal(s.queued, 5, "this generation is queued");
  s.message(batch(undefined));
  assert.equal(s.queued, 10, "a batch with no generation (an older caller) is queued");
});

test("an underrun clears the refused runs with their count: it never goes below zero", () => {
  const s = synth({ voiceLimit: 1 });
  s.message({ type: "config", lengths: new Float64Array([10, 12, 14]), groups: [0, 0] });
  for (const i of [0, 1, 2]) s.on(i);
  assert.equal(s.refused, 2, "two runs refused with one voice");
  s.gatesOff();                       // the queue ran dry: every run off, none refused
  assert.equal(s.refused, 0);
  for (const i of [1, 2]) s.on(i);    // on again, refused again...
  assert.equal(s.refused, 2);
  for (const i of [1, 2]) s.off(i);   // ...and off
  assert.equal(s.refused, 0);
});

// Belts muted (gain 0, and the falsy gain 0 with truthiness): the synth
// keeps no voices and skips the per-tick bit walk -- each voice would only
// ever have added exactly 0.0 -- so the sound must be exactly the gates
// alone: the same as a board whose runs are all outside the length window
// and so never candidates. Unmuting starts from no bits, and the next tick
// turns on every run active then.
async function streamed(params, rate, onFrame = null) {
  const bytes = readFileSync(save);
  const sim = new Sim(T, await Board.fromNpz(T, bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength)));
  for (let i = 0; i < 300; i++) sim.step();
  const msgs = [];
  const rec = new Recorder({ postMessage: (m) => msgs.push(m) }, params);
  rec.attach(sim);
  const s = synth(params), o = new Float32Array(128), out = [];
  let bank = 0, blocks = 0;
  for (let f = 0; f < 40; f++) {
    onFrame?.(f, s);
    bank += rate / 60;
    const n = Math.floor(bank); bank -= n;
    for (let i = 0; i < n; i++) sim.step();
    rec.flush(rate);
    for (const m of msgs.splice(0)) s.message(structuredClone(m));
    for (; blocks < Math.round((f + 1) * SR / 60 / 128); blocks++) { s.render(o, 128); out.push(...o); }
  }
  return { s, out };
}

test("muted belts keep no voices and sound exactly like no belts at all", { skip: !existsSync(save) }, async () => {
  for (const [rate, extra] of [[240, {}], [40000, {}], [40000, { truthAudio: true, beltGainF: 0, clickDetune: 3 }],
                               [20000, { advAudio: true, vibDepth: 0.3, clickNoise: 0.5, clickNoiseMs: 20 }]]) {
    const muted = await streamed({ ...AUDIO_DEFAULTS, beltGain: 0, ...extra }, rate);
    const none = await streamed({ ...AUDIO_DEFAULTS, beltMinLen: 500, beltCutoff: 512, ...extra }, rate);
    assert.equal(muted.s.nv, 0, `${rate} tps: no voices while muted`);
    assert.ok(muted.out.some(x => x !== 0), `${rate} tps: the gates still sound`);
    const at = muted.out.findIndex((x, i) => x !== none.out[i]);
    assert.equal(at, -1, `${rate} tps: muted belts differ from no belts at sample ${at}`);
  }
  // Truthiness on with only the truthy gain at 0: the falsy belts still sound, so not muted.
  const half = await streamed({ ...AUDIO_DEFAULTS, beltGain: 0, truthAudio: true, beltGainF: 1 }, 240);
  assert.ok(half.s.nv > 0, "falsy belts audible: voices kept");
});

test("unmuting the belts voices the runs active at that moment", { skip: !existsSync(save) }, async () => {
  const ref = await streamed({ ...AUDIO_DEFAULTS }, 240);
  // Sounding, then muted (with runs active: stale bits to forget), then back.
  const back = await streamed({ ...AUDIO_DEFAULTS }, 240, (f, s) => {
    if (f === 10) { assert.ok(s.active > 0, "runs active before the mute"); s.message({ type: "params", p: { beltGain: 0 } }); }
    if (f === 25) s.message({ type: "params", p: { beltGain: AUDIO_DEFAULTS.beltGain } });
  });
  assert.ok(back.s.nv > 0, "voices again after unmuting");
  assert.equal(back.s.active, ref.s.active, "the same runs active as a synth that was never muted");
  assert.equal([...back.s.prev].join(), [...ref.s.prev].join(), "the same bits");
});

// Audio > gates: steady loudness. Fires land at random times, so their power
// adds and a gate's loudness grows as the square root of the tick rate; the
// switch scales the gates' gain by sqrt(30 / rate), so 30 tps is unchanged.
function gateRms(params, rate, fires) {
  const s = new GatesSynth(SR, { offline: true });
  s.message({ type: "params", p: { ...AUDIO_DEFAULTS, softClip: false, ...params } });
  s.message({ type: "config", lengths: new Float64Array([]), groups: [0, 8] });
  const out = new Float32Array(SR * 2), n = Math.round(rate * 2) + 2;
  s.message({ type: "ticks", n, words: 0, rate, bits: new Uint32Array(0), f0: new Uint16Array(n), f1: new Uint16Array(n).fill(fires) });
  for (let i = 0; i < out.length; i += 128) s.render(out.subarray(i, i + 128), 128);
  let e = 0;
  for (let i = SR / 2; i < out.length; i++) e += out[i] * out[i];
  return Math.sqrt(e / (out.length - SR / 2));
}
const dB = (a, b) => 20 * Math.log10(a / b);

test("gates: steady loudness changes nothing at 30 tps", { skip: !existsSync(save) }, async () => {
  const on = await streamed({ ...AUDIO_DEFAULTS, gateSteady: true }, 30);
  const off = await streamed({ ...AUDIO_DEFAULTS, gateSteady: false }, 30);
  assert.ok(off.out.some(x => x !== 0), "something sounded");
  assert.equal(on.out.findIndex((x, i) => x !== off.out[i]), -1, "the same samples at 30 tps");
});

test("gates: steady loudness holds the clicks' level as the tick rate climbs", () => {
  const base = gateRms({ gateSteady: true }, 30, 2), raw = gateRms({}, 30, 2);
  for (const rate of [100, 300, 1000, 3000, 10000, 40000]) {
    const steady = dB(gateRms({ gateSteady: true }, rate, 2), base);
    assert.ok(Math.abs(steady) < 6, `${rate} tps: ${steady.toFixed(1)} dB from 30 tps with the switch on`);
  }
  // ...where without it they climb (about +31 dB from 30 to 40k tps).
  const loud = dB(gateRms({}, 40000, 2), raw);
  assert.ok(loud > 20, `without the switch 40k tps is ${loud.toFixed(1)} dB louder`);
});
