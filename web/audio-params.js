// The Audio dials: defaults and the panel's spec. Tiny and dependency-free,
// so the panel can show them without loading any audio code -- audio.js and
// the worklet are only fetched when the player switches audio on.

export const AUDIO_DEFAULTS = {
  master: 0.5,          // output gain, before the soft clipper
  norm: 0.5,            // normalisation exponent p: each voice is scaled by 1 / N^p
  softClip: true,       // tanh on the way out; a safety net, not a compressor
  latencyMs: 60,        // how far the audio runs behind the sim
  holdUnderrun: false,  // realtime: hold the voices through a short underrun instead of releasing
  startTick: 0,         // silent before this tick (skips the start-up spike)
  fadeMs: 0,            // fade in from the start tick
  // Belts: a drone per active run, pitch = maxHz / length (positions).
  beltGain: 1,
  beltMaxHz: 1760,      // the pitch of a min-length run; pitch = this * min length / length
  beltMinLen: 1,        // runs shorter than this get no voice
  beltCutoff: 64,       // runs longer than this get no voice
  // The scale (scale.js): pitch = maxHz * f(min length) / f(length), f a
  // formula or a table. "n" is the original law. Set from the Customize
  // scale dialog, not a dial in the panel.
  scaleMode: "formula", // "formula" or "lut"
  scaleExpr: "n",
  scaleLut: "",         // the table's numbers, as text
  // Per-length gain for the shortest (highest) runs; 7 and up are at 1.
  lenGain1: 1, lenGain2: 1, lenGain3: 1, lenGain4: 1, lenGain5: 1, lenGain6: 1,
  lowCutHz: 0,          // high-pass on the mix, against rumble; 0 = off
  pitchTilt: 0,         // gain * (length / min length)^tilt: low runs louder, capped at +24 dB
  beltShape: 0.3,       // 0 sine, 0.5 triangle, 1 saw
  attackMs: 5,          // for a 10-tile run; see sluggishness
  releaseMs: 40,
  sluggish: 0,          // attack and release scale with (length / 10)^this
  beltTrack: 0,         // 0 fixed pitch; 1 pitch proportional to tick rate
  trackRef: 30,         // the tick rate at which tracking changes nothing
  voiceLimit: 256,      // belt voices at once
  // Gate fires, two groups (Sources are always silent): a click, a short
  // damped resonance per fire. Same sound for both until they diverge.
  g0Gain: 1,            // Swap, Route, Merge
  g0Tone: 1800,
  g0DecayMs: 3,
  g1Gain: 1,            // every other machine
  g1Tone: 1800,
  g1DecayMs: 3,
  // Space (space.js): camera-aware audio hears the screen, scaled about its
  // centre; the fringe and low-pass soften the screen's edge and the audio
  // region's alike. 0 and 0 is a hard edge, as the region always was.
  camAudio: false,
  camScale: 1,          // the screen box, about its centre, before it is heard
  spaceFringe: 0,       // fade-out distance, as a fraction of the box's longer side
  spaceLp: 0,           // how far the low-pass closes at the fringe's edge, 0..1
};

export const VOICE_LIMIT_MAX = 1024;

// [key, label, opts] for a dial, [key, label] for a switch, ["---", title]
// to start a sub-folder.
export const AUDIO_SPEC = [
  ["master", "volume", { min: 0, max: 2, step: 0.01 }],
  ["norm", "normalise (p)", { min: 0, max: 1, step: 0.05 }],
  ["softClip", "soft clip"],
  ["latencyMs", "latency ms", { min: 20, max: 250, step: 5 }],
  ["holdUnderrun", "hold on underrun"],
  ["startTick", "audio from tick", { min: 0, max: 5000, step: 1 }],
  ["fadeMs", "fade in ms", { min: 0, max: 10000, step: 10 }],
  ["---", "belts"],
  ["beltGain", "gain", { min: 0, max: 4, step: 0.01 }],
  ["beltMaxHz", "min-length Hz", { min: 50, max: 8000, step: 10 }],
  ["beltMinLen", "min length", { min: 1, max: 512, step: 1 }],
  ["beltCutoff", "max length", { min: 1, max: 512, step: 1 }],
  ["beltShape", "sine→tri→saw", { min: 0, max: 1, step: 0.01 }],
  ["attackMs", "attack ms", { min: 0.5, max: 200, step: 0.5 }],
  ["releaseMs", "release ms", { min: 1, max: 1000, step: 1 }],
  ["sluggish", "sluggishness", { min: -2, max: 2, step: 0.05 }],
  ["beltTrack", "follow tick rate", { min: 0, max: 1, step: 0.05 }],
  ["trackRef", "at tick rate", { min: 1, max: 10000, step: 1 }],
  ["voiceLimit", "voice limit", { min: 8, max: VOICE_LIMIT_MAX, step: 8 }],
  ["---", "tone"],
  ["lenGain1", "length 1", { min: 0, max: 2, step: 0.01 }],
  ["lenGain2", "length 2", { min: 0, max: 2, step: 0.01 }],
  ["lenGain3", "length 3", { min: 0, max: 2, step: 0.01 }],
  ["lenGain4", "length 4", { min: 0, max: 2, step: 0.01 }],
  ["lenGain5", "length 5", { min: 0, max: 2, step: 0.01 }],
  ["lenGain6", "length 6", { min: 0, max: 2, step: 0.01 }],
  ["pitchTilt", "pitch tilt", { min: 0, max: 2, step: 0.05 }],
  ["lowCutHz", "low cut Hz", { min: 0, max: 1000, step: 5 }],
  ["---", "swap / route / merge"],
  ["g0Gain", "gain", { min: 0, max: 4, step: 0.01 }],
  ["g0Tone", "tone Hz", { min: 50, max: 12000, step: 10 }],
  ["g0DecayMs", "decay ms", { min: 0.2, max: 100, step: 0.1 }],
  ["---", "other gates"],
  ["g1Gain", "gain", { min: 0, max: 4, step: 0.01 }],
  ["g1Tone", "tone Hz", { min: 50, max: 12000, step: 10 }],
  ["g1DecayMs", "decay ms", { min: 0.2, max: 100, step: 0.1 }],
  ["---", "space"],
  ["camAudio", "camera-aware"],
  ["camScale", "screen scale", { min: 0.1, max: 3, step: 0.05 }],
  ["spaceFringe", "soft edge", { min: 0, max: 2, step: 0.05 }],
  ["spaceLp", "distance low-pass", { min: 0, max: 1, step: 0.05 }],
];

// The belt runs that may sound: a run of the simulation whose length is
// within [minLen, maxLen] and, with a sound selection (a Set of y * width + x
// cell indices), that touches it. One rule for the recorder (audio.js) and
// the scale dialog's histogram, so the two cannot disagree.
export function eligibleRuns(sim, minLen, maxLen, selection = null) {
  const W = sim.board.width;
  const picked = (cells) => !selection || cells.some(([x, y]) => selection.has(y * W + x));
  return sim.order.filter(seg => seg.length >= minLen && seg.length <= maxLen && picked(seg.cells));
}

// Which click group a machine's fires go to: 0, 1, or 2 for silent.
export function audioGroupOf(cls) {
  if (cls === "Source") return 2;
  return cls === "Swap" || cls === "Route" || cls === "Merge" ? 0 : 1;
}

// Dials that only steer realtime playback, so changing them leaves a bake
// valid. Everything else changes what a bake would have rendered.
export const REALTIME_ONLY = ["latencyMs", "holdUnderrun"];
export function bakeKey(p) {
  const o = {};
  for (const k of Object.keys(AUDIO_DEFAULTS)) if (!REALTIME_ONLY.includes(k)) o[k] = p[k];
  return JSON.stringify(o);
}

// The Bake dialog's limits and defaults.
export const BAKE_DEFAULT_SECONDS = 30;
export const BAKE_MAX_SECONDS = 300;

// --- audio settings in a save -----------------------------------------------
//
// A save carries the dials and the bake dialog's seconds and rate as JSON
// under its own `audio` key, beside the annotations (the desktop ignores a
// key it does not know). The "sound" switch is never saved.

// Only known dials, of the right type, clamped to the panel's range: the
// file may be hand-edited, older or newer. An unknown or bad entry costs
// that dial, never the rest.
export function cleanDials(d) {
  const out = {};
  if (!d || typeof d !== "object") return out;
  const spec = Object.fromEntries(AUDIO_SPEC.filter(e => e[0] !== "---").map(e => [e[0], e[2]]));
  for (const [k, v] of Object.entries(d)) {
    if (!(k in AUDIO_DEFAULTS) || typeof v !== typeof AUDIO_DEFAULTS[k]) continue;
    if (k === "scaleMode" && v !== "formula" && v !== "lut") continue;
    if ((k === "scaleExpr" || k === "scaleLut") && v.length > 100000) continue;
    if (typeof v === "number") {
      if (!Number.isFinite(v)) continue;
      const o = spec[k];
      out[k] = o ? Math.min(o.max, Math.max(o.min, v)) : v;
    } else out[k] = v;
  }
  return out;
}

// The record a save holds: { dials, bake: { seconds, rate } }.
export function audioRecord(dials, bake) {
  return { dials: { ...dials }, bake: bake ? { seconds: bake.seconds, rate: bake.rate } : null };
}

// A save's `audio` blob back into a record, or null when it has none (or
// it is unreadable: a corrupt blob costs the sound settings, never the board).
export function parseAudio(blob) {
  if (typeof blob !== "string" || !blob.trim()) return null;
  try {
    const v = JSON.parse(blob);
    if (!v || typeof v !== "object") return null;
    const b = v.bake;
    const bake = b && Number.isFinite(b.seconds) && Number.isFinite(b.rate) && b.seconds > 0 && b.rate > 0
      ? { seconds: Math.min(b.seconds, BAKE_MAX_SECONDS), rate: b.rate } : null;
    return { dials: cleanDials(v.dials), bake };
  } catch {
    return null;
  }
}
