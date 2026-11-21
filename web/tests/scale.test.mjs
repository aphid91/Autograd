// Scales (scale.js): the formula language, the presets, tables, and that
// the standard scale is exactly the original pitch law.

import { test } from "node:test";
import assert from "node:assert/strict";
import { compileScale, parseLut, scaleTable, scaleHz, degree, SCALE_STEPS, SCALE_PRESETS, noteName } from "../scale.js";
import { AUDIO_DEFAULTS, cleanDials } from "../audio-params.js";

const at = (src, n, m = 1) => compileScale(src)(n, m);

test("the formula language: precedence, power, unary minus, modulo", () => {
  assert.equal(at("1 + 2 * 3", 0), 7);
  assert.equal(at("(1 + 2) * 3", 0), 9);
  assert.equal(at("2 ** 3 ** 2", 0), 512);        // right-associative
  assert.equal(at("2 ^ 10", 0), 1024);            // ^ is **
  assert.equal(at("-2 ** 2", 0), -4);             // as Python: -(2**2)
  assert.equal(at("2 ** -1", 0), 0.5);
  assert.equal(at("-7 % 3", 0), 2);               // never negative
  assert.equal(at("n * 2 + m", 5, 3), 13);
  assert.equal(at("fract(2.75) + floor(-0.5)", 0), -0.25);
  assert.equal(at("max(n, 3) + min(1, 2) + clamp(9, 0, 4)", 1), 8);
  assert.ok(Math.abs(at("2 ** (fract(n * 20) / 12)", 1.0125) - 2 ** (0.25 / 12)) < 1e-12);
  assert.equal(at(".5e1", 0), 5);
});

test("the formula language refuses what it does not know, readably", () => {
  for (const [src, re] of [["", /empty/], ["2 +", /ends too soon/], ["(n", /expected "\)"/],
                           ["foo(n)", /no function "foo"/], ["x * 2", /no name "x"/],
                           ["min(1)", /takes 2 arguments/], ["n n", /unexpected "n"/],
                           ["n; alert(1)", /unexpected ";"/]]) {
    assert.throws(() => compileScale(src), re, src);
  }
});

test("scale degrees continue by octaves both ways", () => {
  assert.deepEqual([0, 1, 2, 6, 7, 8, -1].map(k => degree(SCALE_STEPS.major, k)), [0, 2, 4, 11, 12, 14, -1]);
  assert.deepEqual([0, 4, 5, -1].map(k => degree(SCALE_STEPS.pent, k)), [0, 9, 12, -3]);
});

test("the standard scale is the original law exactly", () => {
  const { f, bad, error } = scaleTable(AUDIO_DEFAULTS);
  assert.equal(error, null);
  assert.equal(bad, 0);
  for (const m of [1, 3, 10]) {
    for (const n of [m, m + 1, 2 * m + 5, 64, 512]) {
      assert.equal(scaleHz(f, n, 1760, m), 1760 * Math.max(1, m) / n, `n=${n} m=${m}`);
    }
  }
});

test("presets: semitones climb a semitone a tile, the major scale its steps, from the min length", () => {
  const p = (expr, m = 4) => ({ ...AUDIO_DEFAULTS, scaleExpr: expr, beltMinLen: m });
  for (const [, expr] of SCALE_PRESETS) assert.equal(scaleTable(p(expr)).error, null, expr);
  const semis = scaleTable(p("2 ** ((n - m) / 12)")).f;
  assert.ok(Math.abs(scaleHz(semis, 4, 440, 4) - 440) < 1e-9);          // the min length is the dial
  assert.ok(Math.abs(scaleHz(semis, 16, 440, 4) - 220) < 1e-9);         // 12 tiles longer: an octave down
  const major = scaleTable(p("2 ** (major(n - m) / 12)")).f;
  const down = [4, 5, 6, 7, 8, 9, 10, 11].map(n => Math.round(12 * Math.log2(440 / scaleHz(major, n, 440, 4))));
  assert.deepEqual(down, [0, 2, 4, 5, 7, 9, 11, 12]);
});

test("tables: f(1) onwards, the last value holding; bad files refused", () => {
  const lut = "1, 2\n4 8";
  assert.deepEqual(parseLut(lut), [1, 2, 4, 8]);
  const { f } = scaleTable({ ...AUDIO_DEFAULTS, scaleMode: "lut", scaleLut: lut });
  assert.deepEqual([f[1], f[2], f[4], f[9]], [1, 2, 8, 8]);
  assert.throws(() => parseLut("1 two"), /line 1: "two"/);
  assert.throws(() => parseLut("1\n-3"), /line 2: "-3" is not a positive/);
  assert.throws(() => parseLut(" \n"), /no numbers/);
});

test("a formula that cannot be played falls back to the standard pitch, and says so", () => {
  const broken = scaleTable({ ...AUDIO_DEFAULTS, scaleExpr: "2 *" });
  assert.match(broken.error, /ends too soon/);
  assert.equal(broken.f[7], 7);
  const partly = scaleTable({ ...AUDIO_DEFAULTS, scaleExpr: "n - 10" });   // <= 0 for n <= 10
  assert.equal(partly.bad, 10);
  assert.equal(partly.f[3], 3);
  assert.equal(partly.f[15], 5);
});

test("the scale dials survive cleaning, and a bad mode is dropped", () => {
  assert.deepEqual(cleanDials({ scaleMode: "lut", scaleExpr: "2 ** n", scaleLut: "1 2" }),
                   { scaleMode: "lut", scaleExpr: "2 ** n", scaleLut: "1 2" });
  assert.deepEqual(cleanDials({ scaleMode: "eval" }), {});
});

test("note names", () => {
  assert.equal(noteName(440), "A4");
  assert.equal(noteName(261.6256), "C4");
  assert.equal(noteName(440 * 2 ** (0.1 / 12)), "A4 +10");
});

test("the synth: a scale change re-pitches a voice already sounding", async () => {
  const { GatesSynth } = await import("../audio-dsp.js");
  const SR = 48000, s = new GatesSynth(SR);
  s.message({ type: "params", p: { ...AUDIO_DEFAULTS, beltMinLen: 4, beltMaxHz: 880 } });
  s.message({ type: "config", lengths: [4, 16], groups: [0, 0] });
  const W = s.words, N = 10;
  const tick = () => {
    const bits = new Uint32Array(N * W);
    for (let k = 0; k < N; k++) bits[k * W] = 0b10;   // the 16-long run is active
    s.message({ type: "ticks", n: N, words: W, rate: 30, bits, f0: new Uint16Array(N), f1: new Uint16Array(N) });
  };
  tick();
  s.render(new Float32Array(4096), 4096);
  const hz = () => s.vInc[s.voiceOf[1]] * SR;
  assert.ok(Math.abs(hz() - 880 * 4 / 16) < 1e-6, `standard: ${hz()}`);
  s.message({ type: "params", p: { scaleExpr: "2 ** ((n - m) / 12)" } });
  tick();
  s.render(new Float32Array(4096), 4096);
  assert.ok(Math.abs(hz() - 440) < 1e-6, `semitones: 12 tiles longer, an octave down: ${hz()}`);
});
