// Advanced audio (advAudio): off, its dials change nothing at all; on at its
// defaults, nothing either; then each feature does what it says -- the pulse
// wave, tremolo, vibrato, the noise exciter and the falsy envelope -- and the
// block renderer still matches the per-tick reference, and a bake its own
// rechunking.

import { test } from "node:test";
import assert from "node:assert/strict";
import { GatesSynth } from "../audio-dsp.js";
import { AUDIO_DEFAULTS } from "../audio-params.js";

const SR = 48000;
const ADV = {   // every advanced dial away from its default
  beltPulse: 0.8, beltPW: 0.2, beltPulseF: 0.5, beltPWF: 0.3, tremRate: 6, tremDepth: 0.7, vibRate: 4, vibDepth: 1,
  tremRateF: 3, tremDepthF: 0.5, vibRateF: 2, vibDepthF: 0.5, clickNoise: 0.6, clickNoiseMs: 5, clickNoiseF: 1, clickNoiseMsF: 9,
  attackMsF: 80, releaseMsF: 200,
};

function synth(params, offline = true) {
  const s = new GatesSynth(SR, { offline });
  s.message({ type: "params", p: { ...AUDIO_DEFAULTS, softClip: false, ...params } });
  return s;
}
// A scene: three runs flickering on and off, clicks in both groups (and
// both halves, with truthiness), flips; rendered in chunks of `chunk`.
function scene(params, { n = 120, rate = 240, chunk = 128, batch = true, offline = true } = {}) {
  const s = synth(params, offline);
  s.batch = batch;
  const truth = !!params.truthAudio;
  s.message({ type: "config", lengths: Float64Array.of(2, 5, 9), groups: [2, 3], truth: truth ? Uint8Array.of(1, 0, 1) : null });
  const out = new Float32Array(Math.ceil((n + 2) * SR / rate));
  let at = 0;
  for (let f = 0; f * 10 < n; f++) {
    const k = 10, tf = [], to = new Uint32Array(k);
    for (let j = 0; j < k; j++) { if ((f + j) % 4 === 0) tf.push(((f + j) % 3) * 2 + ((f + j) & 1)); to[j] = tf.length; }
    s.message({ type: "ticks", n: k, tick0: f * k, words: 1, rate, bits: new Uint32Array(k).map((_, j) => (f * k + j) % 5 ? 7 : 2),
                f0: new Float32Array(k).map((_, j) => j % 3), f1: new Float32Array(k).fill(1),
                ...(truth ? { f0F: new Float32Array(k).fill(1), f1F: new Float32Array(k).map((_, j) => j & 1), tf: Int32Array.from(tf), to } : {}) });
    // Realtime, frame by frame; offline, every tick is fed before rendering.
    if (offline) continue;
    const end = Math.min(out.length, Math.round((f + 1) * k * SR / rate));
    for (; at < end; at += chunk) { const c = Math.min(chunk, out.length - at); s.render(out.subarray(at, at + c), c); }
  }
  for (; at < out.length; at += chunk) { const c = Math.min(chunk, out.length - at); s.render(out.subarray(at, at + c), c); }
  return out;
}
const same = (a, b) => a.length === b.length && a.every((x, i) => x === b[i]);
const rms = (a) => Math.sqrt(a.reduce((m, x) => m + x * x, 0) / a.length);

test("off, the advanced dials change nothing -- to the bit, truthiness on or off", () => {
  for (const truthAudio of [false, true]) {
    const base = scene({ truthAudio });
    assert.ok(base.some(x => x !== 0));
    assert.ok(same(scene({ truthAudio, ...ADV }), base), `truthiness ${truthAudio}: dials ignored while off`);
    assert.ok(same(scene({ truthAudio, advAudio: true }), base), `truthiness ${truthAudio}: on at its defaults`);
    assert.ok(!same(scene({ truthAudio, advAudio: true, ...ADV }), base), "and on with the dials set, it is heard");
  }
});

test("a bake with every advanced feature is the same however it is chunked", () => {
  const p = { truthAudio: true, advAudio: true, ...ADV, clickJitter: 1 };
  assert.ok(same(scene(p, { chunk: 128 }), scene(p, { chunk: 37 })));
});

test("realtime by block is the per-tick reference, with every advanced feature on", () => {
  const p = { truthAudio: true, advAudio: true, ...ADV, beltBrightF: 1, clickJitter: 1 };
  const a = scene(p, { offline: false, batch: true }), b = scene(p, { offline: false, batch: false });
  assert.ok(a.some(x => x !== 0));
  assert.ok(same(a, b));
});

// One run sounding steadily for a second, as a buffer.
function drone(params) {
  const s = synth({ beltMaxHz: 220, attackMs: 0.5, releaseMs: 5, ...params });
  s.message({ type: "config", lengths: Float64Array.of(1), groups: [0, 0] });
  const n = 60;
  s.message({ type: "ticks", n, tick0: 0, words: 1, rate: 60, bits: new Uint32Array(n).fill(1), f0: new Float32Array(n), f1: new Float32Array(n) });
  const out = new Float32Array(SR);
  for (let i = 0; i < SR; i += 128) s.render(out.subarray(i, i + 128), 128);
  return out.subarray(SR / 4);
}

test("the pulse mix makes a pulse wave: two levels, high for the width's share", () => {
  const w = drone({ advAudio: true, beltShape: 0, beltPulse: 1, beltPW: 0.25 });
  const peak = Math.max(...w);
  const high = w.filter(x => x > peak * 0.5).length / w.length;
  assert.ok(Math.abs(high - 0.25) < 0.03, `high ${high} of the time`);
  const sine = drone({ beltShape: 0 });
  assert.ok(Math.abs(sine.filter(x => x > Math.max(...sine) * 0.5).length / sine.length - 1 / 3) < 0.03, "a sine is not");
});

test("tremolo swings the level at its rate and depth; vibrato swings the pitch", () => {
  // Windows of five whole periods of the 220 Hz drone.
  const env = (a, win = 1091) => { const o = []; for (let i = 0; i + win <= a.length; i += win) o.push(rms(a.subarray(i, i + win))); return o; };
  const t = env(drone({ advAudio: true, tremDepth: 0.8, tremRate: 4 }));
  const lo = Math.min(...t), hi = Math.max(...t);
  assert.ok(lo / hi < 0.4, `tremolo dips to ${lo / hi}`);
  const flat = env(drone({}));
  assert.ok(Math.min(...flat) / Math.max(...flat) > 0.95, `no tremolo, no dip: ${Math.min(...flat) / Math.max(...flat)}`);
  // Vibrato: the spacing of upward zero crossings varies.
  const periods = (a) => { const z = []; for (let i = 1; i < a.length; i++) if (a[i - 1] < 0 && a[i] >= 0) z.push(i); return z.slice(1).map((x, i) => x - z[i]); };
  const v = periods(drone({ advAudio: true, vibDepth: 1, vibRate: 5 }));
  assert.ok(Math.max(...v) - Math.min(...v) > 10, `vibrato spreads the period: ${Math.min(...v)}..${Math.max(...v)}`);
  const p = periods(drone({}));
  assert.ok(Math.max(...p) - Math.min(...p) <= 1);
});

test("a noise exciter rings as noise, not as one tone", () => {
  const click = (params) => {
    const s = synth({ g1Tone: 2000, g1DecayMs: 2, clickJitter: 0, ...params });
    s.message({ type: "config", lengths: new Float64Array(0), groups: [0, 1] });
    s.message({ type: "ticks", n: 1, tick0: 0, words: 0, rate: 60, bits: new Uint32Array(0), f0: Float32Array.of(0), f1: Float32Array.of(1) });
    const out = new Float32Array(4800);
    for (let i = 0; i < out.length; i += 128) s.render(out.subarray(i, i + 128), Math.min(128, out.length - i));
    return out;
  };
  const tail = (a) => a.subarray(1000, 2000);  // after an impulse at 2 ms decay has died away
  assert.ok(rms(tail(click({}))) < 1e-4, "an impulse is gone by then");
  const n = click({ advAudio: true, clickNoise: 1, clickNoiseMs: 30 });
  assert.ok(rms(tail(n)) > 1e-3, `a 30 ms burst is still sounding: ${rms(tail(n))}`);
  assert.ok(rms(n) > rms(click({})) * 0.2 && rms(n) < rms(click({})) * 20, "at a comparable level");
});

test("the falsy envelope: a falsy run's own attack", () => {
  const rise = (params) => {
    const s = synth({ truthAudio: true, advAudio: true, beltMaxHz: 220, attackMs: 1, attackMsF: 200, ...params });
    s.message({ type: "config", lengths: Float64Array.of(1), groups: [0, 0], truth: Uint8Array.of(params.truthy ? 1 : 0) });
    const n = 30;
    s.message({ type: "ticks", n, tick0: 0, words: 1, rate: 60, bits: new Uint32Array(n).fill(1), f0: new Float32Array(n), f1: new Float32Array(n),
                f0F: new Float32Array(n), f1F: new Float32Array(n), tf: new Int32Array(0), to: new Uint32Array(n) });
    const out = new Float32Array(SR / 4);
    for (let i = 0; i < out.length; i += 128) s.render(out.subarray(i, i + 128), 128);
    return rms(out.subarray(480, 1440)) / rms(out.subarray(SR / 4 - 2400));   // 10-30 ms, against the end
  };
  assert.ok(rise({ truthy: true }) > 0.9, "truthy: up at once");
  assert.ok(rise({ truthy: false }) < 0.3, "falsy: 200 ms attack");
});
