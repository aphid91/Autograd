// Audio presets: the Audio dials (and the Bake dialog's seconds and rate)
// kept under a name, to carry from one save to another. A save still holds
// its own dials (audio-params.js audioRecord); a preset is the same record
// held apart from any board, in this browser, and as a .json file to move
// between devices.
//
// The record is exactly a save's -- { dials, bake } -- so loading one is
// loading a save's audio settings, cleaned the same way (cleanDials: only
// known dials, of the right type, clamped). What is NOT in it: the sound
// switch, the audio selection and the audio region, which are about one
// board, not a sound.
//
// Pure apart from the storage, which is passed in (localStorage in the app,
// a Map-backed stand-in in the tests).

import { AUDIO_DEFAULTS, cleanDials, BAKE_MAX_SECONDS } from "./audio-params.js";

export const PRESETS_KEY = "gates.web.audioPresets.v1";
export const PRESET_FORMAT = "gates-audio-preset";
const NAME_MAX = 60;

const cleanName = (n) => String(n ?? "").trim().slice(0, NAME_MAX);

function cleanBake(b) {
  return b && Number.isFinite(b.seconds) && Number.isFinite(b.rate) && b.seconds > 0 && b.rate > 0
    ? { seconds: Math.min(b.seconds, BAKE_MAX_SECONDS), rate: b.rate } : null;
}

// A record as a preset stores it: every dial, defaults filled in for any
// the record lacks, so loading a preset sets the whole sound -- a dial the
// preset was made without goes back to its default rather than keeping
// whatever the board in front of you had.
export function presetRecord(dials, bake) {
  return { dials: { ...AUDIO_DEFAULTS, ...cleanDials(dials) }, bake: cleanBake(bake) };
}

// { name: record } from storage. Anything malformed is dropped.
export function readPresets(store) {
  let raw;
  try { raw = JSON.parse(store.getItem(PRESETS_KEY) || "{}"); } catch { return {}; }
  const out = {};
  if (!raw || typeof raw !== "object") return out;
  for (const [name, r] of Object.entries(raw)) {
    const n = cleanName(name);
    if (n && r && typeof r === "object") out[n] = presetRecord(r.dials, r.bake);
  }
  return out;
}

export const presetNames = (store) => Object.keys(readPresets(store)).sort((a, b) => a.localeCompare(b));

// Store `record` under `name` (replacing one of that name). Throws a
// readable message when the browser refuses.
export function putPreset(store, name, dials, bake) {
  const n = cleanName(name);
  if (!n) throw new Error("a preset needs a name");
  const all = readPresets(store);
  all[n] = presetRecord(dials, bake);
  try { store.setItem(PRESETS_KEY, JSON.stringify(all)); }
  catch (err) { throw new Error(`the browser would not store it (${err.message})`); }
  return n;
}

export function deletePreset(store, name) {
  const all = readPresets(store);
  if (!(name in all)) return;
  delete all[name];
  store.setItem(PRESETS_KEY, JSON.stringify(all));
}

// The .json file: tagged, so importing some other JSON is refused rather
// than read as a preset of all defaults.
export function presetFile(name, record) {
  return JSON.stringify({ format: PRESET_FORMAT, version: 1, name: cleanName(name), ...record }, null, 2);
}

// A file's text back into { name, record }. Throws a readable message.
export function parsePresetFile(text, fallbackName = "Imported") {
  let v;
  try { v = JSON.parse(text); } catch { throw new Error("not a JSON file"); }
  if (!v || typeof v !== "object" || v.format !== PRESET_FORMAT) throw new Error("not an audio preset file");
  return { name: cleanName(v.name) || cleanName(fallbackName) || "Imported", record: presetRecord(v.dials, v.bake) };
}
