// Truthiness (Sim.truthOn): what each machine's bit reads, that it is only
// written while on, that a run's bit is its newest item's sign, that the
// worker's mirror and the audio recorder carry it exactly, and the synth's
// falsy halves: clicks on their own resonators, a drone transposed and
// crossfaded, and the block renderer equal to the per-tick reference.

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync, existsSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { Board } from "../board.js";
import { Sim, truthSource, truthOf } from "../sim.js";
import { SimHost, applyDelta, exportFull, boardMessage } from "../simhost.js";
import { Recorder } from "../audio.js";
import { GatesSynth } from "../audio-dsp.js";
import { AUDIO_DEFAULTS } from "../audio-params.js";
import { stateLine } from "./sim_state.mjs";

const here = dirname(fileURLToPath(import.meta.url));
const root = join(here, "..", "..");
const T = JSON.parse(readFileSync(join(here, "..", "types.json"), "utf8"));
const SR = 48000;
const save = join(root, "cpu.npz");

async function load(path = save) {
  const bytes = readFileSync(path);
  return Board.fromNpz(T, bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength));
}
const byClass = (cls) => Object.values(T.types).find(t => t && t.class === cls).sim;

test("each machine's truthiness reads the value the design names", () => {
  const O = T.sim.OPS;
  const src = (cls) => truthSource(byClass(cls), O);
  assert.deepEqual(src("Shear"), { tKind: 2, tCh: 1 }, "Shear: the sum, not the pass-through");
  assert.deepEqual(src("Mac"), { tKind: 2, tCh: 2 }, "Mac: the accumulator");
  assert.deepEqual(src("Max"), { tKind: 2, tCh: 0 });
  assert.deepEqual(src("Floor"), { tKind: 2, tCh: 0 });
  assert.deepEqual(src("Reciprocal"), { tKind: 2, tCh: 0 });
  assert.deepEqual(src("Source"), { tKind: 2, tCh: 0 }, "Source: its emitted value");
  assert.deepEqual(src("Swap"), { tKind: 1, tCh: 0 }, "Swap: its control x");
  assert.deepEqual(src("Route"), { tKind: 1, tCh: 1 }, "Route: its control b");
  assert.deepEqual(src("Merge"), { tKind: 1, tCh: byClass("Merge").select[0] }, "Merge: its control c");
  assert.deepEqual(src("Sink"), { tKind: 0, tCh: 0 }, "no outputs: always truthy");
  // > 0, so zero and NaN are falsy.
  const g = { tKind: 2, tCh: 1 };
  assert.equal(truthOf(g, null, [5, 0]), 0);
  assert.equal(truthOf(g, null, [5, NaN]), 0);
  assert.equal(truthOf(g, null, [-5, 0.5]), 1);
  assert.equal(truthOf({ tKind: 0, tCh: 0 }, [], []), 1);
});

test("off writes nothing and changes nothing; on, a run's bit is its newest item's sign and a gate's is its fire's",
  { skip: !existsSync(save) }, async () => {
  const plain = new Sim(T, await load()), on = new Sim(T, await load());
  // Rebuilt once more, both, so the seeds go on with truthiness recording.
  on.truthOn = true;
  for (const s of [plain, on]) { s.seen = -1; s.sync(); }
  let checked = 0, falsy = 0;
  for (let i = 0; i < 80; i++) {
    const fc = on.forecast();
    plain.step(); on.step();
    for (const [row, f] of fc.fires) {
      const g = on.gateOf.get(row);
      assert.equal(on.truth[row], truthOf(g, f.values.length ? f.values : null, f.results), `row ${row} at tick ${on.tick}`);
      checked++;
      if (!on.truth[row]) falsy++;
    }
  }
  assert.ok(stateLine(on) === stateLine(plain), "recording truthiness does not perturb the tick");
  assert.ok(checked > 100 && falsy > 0, `${checked} fires checked, ${falsy} falsy`);
  let runs = 0;
  for (const seg of on.order) {
    if (seg.tail === seg.head || seg.lastPush < 0) continue;
    assert.equal(seg.truth, seg.payload[(seg.tail - 1) % seg.cap] > 0 ? 1 : 0, `run ${seg.sid}`);
    runs++;
  }
  assert.ok(runs > 10, `${runs} runs checked`);
  // Off: every bit still at its default.
  assert.ok(plain.truth.every(b => b === 1) && plain.order.every(s => s.truth === 1), "nothing written while off");
});

// The worker's ticks reach the mirror with their bits, and the recorder
// hears the same falsy fires and run flips as one on the main thread would.
test("the worker's mirror and audio carry truthiness exactly", { skip: !existsSync(save) }, async () => {
  const P = { ...AUDIO_DEFAULTS, beltCutoff: 512, truthAudio: true };
  const ref = new Sim(T, await load()), refLog = [];
  const refRec = new Recorder({ postMessage: (m) => { if (m.type === "ticks") refLog.push(m); } }, P);
  refRec.attach(ref);
  ref.truthOn = true;

  const mirror = new Sim(T, await load()), inbox = [], log = [];
  mirror.truthOn = true;
  const host = new SimHost(T, (m) => inbox.push(structuredClone(m)));
  const send = (m) => { const { transfer, ...rest } = m; host.message(structuredClone(rest)); };
  send({ ...boardMessage(mirror, mirror.lastAlloc, true), epoch: 1 });
  send({ ...exportFull(mirror), epoch: 1 });
  send({ type: "truth", on: true });
  const rec = new Recorder({ postMessage: (m) => { if (m.type === "ticks") log.push(m); } }, P);
  rec.attach(mirror);
  let gen = -1;
  const frame = () => {
    mirror.sync();
    rec.ensure(mirror);
    if (rec.cfgGen !== gen) { send({ type: "audio", cfg: rec.tapConfig(mirror) }); gen = rec.cfgGen; }
    send({ type: "pull" });
    for (const d of inbox.splice(0)) { applyDelta(mirror, d); rec.ingest(d.audio); }
    rec.flush(30);
  };
  frame();
  for (const n of [1, 9, 30, 4, 50]) {
    for (let i = 0; i < n; i++) { host.sim.step(); ref.step(); }
    frame(); refRec.flush(30);
    ref.sync();
    assert.ok(stateLine(mirror) === stateLine(ref), "the mirror follows");
    assert.deepEqual([...mirror.truth], [...ref.truth], "gate bits");
    assert.deepEqual(mirror.order.map(s => s.truth), ref.order.map(s => s.truth), "run bits");
  }
  const flat = (L) => {
    const o = { f0: [], f0F: [], f1F: [], flips: [] };
    for (const b of L) for (let k = 0; k < b.n; k++) {
      o.f0.push(b.f0[k]); o.f0F.push(b.f0F[k]); o.f1F.push(b.f1F[k]);
      const a = k ? b.to[k - 1] : 0;
      o.flips.push(Array.from(b.tf.subarray(a, b.to[k])).join(","));
    }
    return o;
  };
  const a = flat(log), b = flat(refLog);
  assert.deepEqual(a, b);
  assert.ok(a.f1F.some(x => x > 0), "some fires were falsy");
  assert.ok(a.flips.some(x => x.length), "some runs flipped");
});

test("the recorder splits each group's fires: truthy plus falsy is every fire", { skip: !existsSync(save) }, async () => {
  const run = async (truthAudio) => {
    const sim = new Sim(T, await load()), log = [];
    const rec = new Recorder({ postMessage: (m) => { if (m.type === "ticks") log.push(m); } }, { ...AUDIO_DEFAULTS, truthAudio });
    rec.attach(sim);
    for (let i = 0; i < 60; i++) sim.step();
    rec.flush(30);
    return log;
  };
  const off = await run(false), on = await run(true);
  assert.ok(off.every(b => !b.f0F && !b.tf), "off: nothing extra is sent");
  const sum = (L, f) => L.flatMap(b => Array.from(b[f]));
  const tot0 = sum(off, "f0"), tot1 = sum(off, "f1");
  const t0 = sum(on, "f0"), f0 = sum(on, "f0F"), t1 = sum(on, "f1"), f1 = sum(on, "f1F");
  assert.deepEqual(t0.map((x, k) => x + f0[k]), tot0);
  assert.deepEqual(t1.map((x, k) => x + f1[k]), tot1);
});

// --- the synth -----------------------------------------------------------------

function synth(params, offline = false) {
  const s = new GatesSynth(SR, { offline });
  s.message({ type: "params", p: { ...AUDIO_DEFAULTS, softClip: false, truthAudio: true, ...params } });
  return s;
}
function render(s, samples) {
  const out = new Float32Array(samples);
  for (let i = 0; i < samples; i += 128) s.render(out.subarray(i, i + 128), Math.min(128, samples - i));
  return out;
}
function freq(buf) {
  let first = -1, last = -1, n = 0;
  for (let i = 1; i < buf.length; i++) if (buf[i - 1] < 0 && buf[i] >= 0) { if (first < 0) first = i; last = i; n++; }
  return (n - 1) * SR / (last - first);
}
// n ticks of one run active, with its flips (tick -> bit) and fires.
function batch(n, flips = {}, f1 = 0, f1F = 0) {
  const tf = [], to = new Uint32Array(n);
  for (let k = 0; k < n; k++) { if (k in flips) tf.push(flips[k]); to[k] = tf.length; }
  return { type: "ticks", n, tick0: 0, words: 1, rate: 60, bits: new Uint32Array(n).fill(1),
           f0: new Float32Array(n), f1: new Float32Array(n).fill(f1), f0F: new Float32Array(n), f1F: new Float32Array(n).fill(f1F),
           tf: Int32Array.from(tf), to };
}

test("falsy clicks ring at the falsy tone, truthy at the truthy one", () => {
  const ring = (f1, f1F) => {
    const s = synth({ g1Tone: 1000, g1ToneF: 3000, g1DecayMs: 40, g1DecayMsF: 40, clickJitter: 0 }, true);
    s.message({ type: "config", lengths: new Float64Array(0), groups: [0, 1] });
    s.message({ ...batch(1, {}, f1, f1F), words: 0, bits: new Uint32Array(0) });
    return freq(render(s, 2400).subarray(10));
  };
  assert.ok(Math.abs(ring(1, 0) - 1000) < 30, `truthy ${ring(1, 0)}`);
  assert.ok(Math.abs(ring(0, 1) - 3000) < 60, `falsy ${ring(0, 1)}`);
});

test("the resonators are shared out: the truthy half gets the larger share", () => {
  const s = synth({ clickRes: 5 });
  assert.deepEqual([s.resT, s.resF], [3, 2]);
  s.message({ type: "params", p: { clickRes: 1 } });
  assert.deepEqual([s.resT, s.resF], [1, 1], "at least one each");
  s.message({ type: "params", p: { truthAudio: false, clickRes: 7 } });
  assert.deepEqual([s.resT, s.resF], [7, 0], "off: all of them, one class");
});

test("a falsy run plays transposed, and a flip crossfades over the dial's time", () => {
  const P = { beltMaxHz: 440, beltTransposeF: 12, truthXfadeMs: 50, attackMs: 0.5 };
  const s = synth(P, true);
  s.message({ type: "config", lengths: Float64Array.of(1), groups: [0, 0], truth: Uint8Array.of(0) });
  s.message(batch(60));
  const out = render(s, SR / 2);
  assert.ok(Math.abs(freq(out.subarray(SR / 8)) - 880) < 10, "falsy from the start: an octave up");

  const t = synth(P, true);
  t.message({ type: "config", lengths: Float64Array.of(1), groups: [0, 0], truth: Uint8Array.of(1) });
  t.message(batch(60, { 30: 0 * 2 + 0 }));      // run 0 turns falsy on tick 30 (half a second in)
  const o = render(t, SR);
  assert.ok(Math.abs(freq(o.subarray(SR / 8, SR * 3 / 8)) - 440) < 10, "truthy before");
  assert.ok(Math.abs(freq(o.subarray(SR * 5 / 8)) - 880) < 10, "falsy after");
  assert.equal(t.vX[0], 1);
  // The crossfade takes the dial's time: 50 ms in, it has only just finished.
  const s2 = synth(P, true);
  s2.message({ type: "config", lengths: Float64Array.of(1), groups: [0, 0], truth: Uint8Array.of(1) });
  s2.message(batch(60, { 30: 0 }));
  render(s2, SR / 2 + SR * 0.025);
  assert.ok(s2.vX[0] > 0.3 && s2.vX[0] < 0.7, `half way at 25 ms: ${s2.vX[0]}`);
});

// Exact with the falsy low-pass open. With it closed, a voice allocated
// mid-block runs its filter over the block's silent start, as the distance
// low-pass does (the block renderer's documented difference), so the two
// differ by a hair there and nowhere else.
test("realtime by block is the per-tick reference, with truthiness on", () => {
  const P = { beltTransposeF: 7, beltShapeF: 0.9, truthXfadeMs: 15, clickJitter: 1, clickDetune: 3, clickDetuneF: 5, clickRes: 8 };
  const go = (batchMode, extra) => {
    const s = synth({ ...P, ...extra });
    s.batch = batchMode;
    s.message({ type: "config", lengths: Float64Array.of(3, 5, 9), groups: [2, 3], truth: Uint8Array.of(1, 0, 1) });
    const out = new Float32Array(768 * 40);
    for (let at = 0, f = 0; at < out.length; at += 768, f++) {
      const n = 13, tf = [], to = new Uint32Array(n);
      for (let k = 0; k < n; k++) { if ((f * n + k) % 11 === 0) tf.push(((f + k) % 3) * 2 + ((f * k) & 1)); to[k] = tf.length; }
      s.message({ type: "ticks", n, tick0: f * n, words: 1, rate: 800, bits: new Uint32Array(n).map((_, k) => (f + k) % 7 ? 7 : 2),
                  f0: new Float32Array(n).fill(f % 3), f1: new Float32Array(n).fill(1), f0F: new Float32Array(n).fill(1), f1F: new Float32Array(n).fill(f % 2),
                  tf: Int32Array.from(tf), to });
      for (let i = at; i < at + 768; i += 128) s.render(out.subarray(i, i + 128), 128);
    }
    return out;
  };
  const first = (a, b) => a.findIndex((x, i) => x !== b[i]);
  const a = go(true, {}), b = go(false, {});
  assert.ok(a.some(x => x !== 0));
  assert.equal(first(a, b), -1, "open: to the bit");
  const c = go(true, { beltBrightF: 0.4 }), d = go(false, { beltBrightF: 0.4 });
  let worst = 0, peak = 0;
  for (let i = 0; i < c.length; i++) { worst = Math.max(worst, Math.abs(c[i] - d[i])); peak = Math.max(peak, Math.abs(d[i])); }
  assert.ok(worst < peak * 0.01, `closed: within 1% (${worst} of ${peak})`);
});
