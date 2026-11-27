// The buttons' press and release clicks (audio-keys.js): heard at once, with
// no ticks queued; each half answers to its own dials; a click ends at its
// release time; and the dials never make a bake stale.

import { test } from "node:test";
import assert from "node:assert/strict";
import { GatesSynth } from "../audio-dsp.js";
import { AUDIO_DEFAULTS, AUDIO_SPEC, bakeKey } from "../audio-params.js";

const SR = 48000;
const synth = (p = {}) => {
  const s = new GatesSynth(SR);
  s.message({ type: "params", p: { softClip: false, master: 1, ...p } });
  return s;
};
// Render `ms` of sound in 128-sample blocks.
function render(s, ms) {
  const n = Math.round(ms * SR / 1000), out = new Float32Array(n);
  for (let i = 0; i < n; i += 128) {
    const b = new Float32Array(128);
    s.render(b, 128);
    out.set(b.subarray(0, Math.min(128, n - i)), i);
  }
  return out;
}
const peak = (a, i0 = 0, i1 = a.length) => { let m = 0; for (let i = i0; i < i1; i++) m = Math.max(m, Math.abs(a[i])); return m; };

test("a press clicks at once, with nothing ticking, and ends at its release time", () => {
  const s = synth();
  assert.equal(peak(render(s, 20)), 0, "silent before");
  s.message({ type: "key", down: true });
  const out = render(s, 200);
  const first = out.findIndex(x => x !== 0);
  assert.ok(first >= 0 && first < 128, "heard in the first block");
  assert.ok(peak(out) > 0.1 && peak(out) < 1.5, `a sensible level: ${peak(out)}`);
  const end = Math.ceil((AUDIO_DEFAULTS.btnAttackMs + AUDIO_DEFAULTS.btnReleaseMs) * SR / 1000) + 128;
  assert.equal(peak(out, end), 0, "and over by its release");
});

test("press and release answer to their own dials", () => {
  const level = (down, p) => { const s = synth(p); s.message({ type: "key", down }); return peak(render(s, 100)); };
  assert.equal(level(true, { btnGain: 0 }), 0, "press gain 0 is silent");
  assert.ok(level(false, { btnGain: 0 }) > 0, "...and leaves the release alone");
  assert.equal(level(false, { btnUpGain: 0 }), 0, "release gain 0 is silent");
  assert.ok(level(true, { btnUpGain: 0 }) > 0);
  const a = level(true, { btnGain: 1 }), b = level(true, { btnGain: 2 });
  assert.ok(Math.abs(b / a - 2) < 1e-6, "gain scales the click");
  // A longer release rings longer.
  const tail = (ms) => { const s = synth({ btnReleaseMs: ms }); s.message({ type: "key", down: true }); return peak(render(s, 300), Math.round(0.06 * SR)); };
  assert.equal(tail(30), 0);
  assert.ok(tail(200) > 0);
});

test("a click is the same every time, and overlapping ones add", () => {
  const one = () => { const s = synth(); s.message({ type: "key", down: true }); return render(s, 60); };
  assert.deepEqual(one(), one());
  const s = synth();
  s.message({ type: "key", down: true }); s.message({ type: "key", down: true });
  const two = render(s, 60), o = one();
  for (let i = 0; i < o.length; i++) assert.ok(Math.abs(two[i] - 2 * o[i]) < 1e-5);
});

test("the buttons' dials are in their own folder and never make a bake stale", () => {
  const keys = ["btnGain", "btnTone", "btnAttackMs", "btnReleaseMs", "btnUpGain", "btnUpTone", "btnUpAttackMs", "btnUpReleaseMs"];
  const at = AUDIO_SPEC.findIndex(e => e[0] === "---" && e[1] === "buttons");
  assert.ok(at >= 0);
  assert.deepEqual(AUDIO_SPEC.slice(at + 1, at + 1 + keys.length).map(e => e[0]), keys);
  const base = bakeKey(AUDIO_DEFAULTS);
  for (const k of keys) assert.equal(bakeKey({ ...AUDIO_DEFAULTS, [k]: AUDIO_DEFAULTS[k] * 2 + 1 }), base, k);
});
