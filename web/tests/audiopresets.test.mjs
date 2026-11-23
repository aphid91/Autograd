// Audio presets (audiopresets.js): the dials under a name, in storage and as
// a .json file.

import { test } from "node:test";
import assert from "node:assert/strict";
import { AUDIO_DEFAULTS } from "../audio-params.js";
import { PRESETS_KEY, readPresets, presetNames, putPreset, deletePreset, presetFile, parsePresetFile } from "../audiopresets.js";

function store() {
  const m = new Map();
  return { getItem: (k) => (m.has(k) ? m.get(k) : null), setItem: (k, v) => m.set(k, String(v)), m };
}

test("save, list, load and delete a preset; every dial is in it", () => {
  const st = store();
  const dials = { ...AUDIO_DEFAULTS, beltMaxHz: 440 };
  delete dials.fadeMs;   // a dial the record lacks comes back at its default
  assert.equal(putPreset(st, "  Warm drone ", dials, { seconds: 20, rate: 60 }), "Warm drone");
  putPreset(st, "Bright", AUDIO_DEFAULTS, null);
  assert.deepEqual(presetNames(st), ["Bright", "Warm drone"]);
  const r = readPresets(st)["Warm drone"];
  assert.equal(r.dials.beltMaxHz, 440);
  assert.equal(r.dials.fadeMs, AUDIO_DEFAULTS.fadeMs);
  assert.deepEqual(Object.keys(r.dials).sort(), Object.keys(AUDIO_DEFAULTS).sort());
  assert.deepEqual(r.bake, { seconds: 20, rate: 60 });
  deletePreset(st, "Bright");
  assert.deepEqual(presetNames(st), ["Warm drone"]);
  assert.throws(() => putPreset(st, "   ", dials), /name/);
});

test("stored and imported dials are cleaned: unknown keys dropped, ranges clamped", () => {
  const st = store();
  st.setItem(PRESETS_KEY, JSON.stringify({ x: { dials: { beltMaxHz: 1e9, nonsense: 3, sound: true } }, "": { dials: {} }, y: 5 }));
  const all = readPresets(st);
  assert.deepEqual(Object.keys(all), ["x"]);
  assert.equal(all.x.dials.beltMaxHz, 8000);
  assert.ok(!("nonsense" in all.x.dials) && !("sound" in all.x.dials));
  st.setItem(PRESETS_KEY, "not json");
  assert.deepEqual(readPresets(st), {});
});

test("a preset round-trips through its .json file; other JSON is refused", () => {
  const st = store();
  putPreset(st, "Keep", { ...AUDIO_DEFAULTS, scaleMode: "formula", scaleExpr: "2 ** (major(n - m) / 12)" }, { seconds: 10, rate: 30 });
  const text = presetFile("Keep", readPresets(st).Keep);
  const got = parsePresetFile(text);
  assert.equal(got.name, "Keep");
  assert.deepEqual(got.record, readPresets(st).Keep);
  assert.equal(parsePresetFile(JSON.stringify({ format: "gates-audio-preset", dials: {} }), "file").name, "file");
  assert.throws(() => parsePresetFile("{}"), /not an audio preset/);
  assert.throws(() => parsePresetFile("nope"), /not a JSON/);
});
