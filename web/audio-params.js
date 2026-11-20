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
  beltMaxHz: 1760,      // a 1-tile run; 10 tiles is an octave above 20
  beltMinLen: 1,        // runs shorter than this get no voice
  beltCutoff: 64,       // runs longer than this get no voice
  // Per-length gain for the shortest (highest) runs; 7 and up are at 1.
  lenGain1: 1, lenGain2: 1, lenGain3: 1, lenGain4: 1, lenGain5: 1, lenGain6: 1,
  lowCutHz: 0,          // high-pass on the mix, against rumble; 0 = off
  beltShape: 0.3,       // 0 sine, 0.5 triangle, 1 saw
  attackMs: 5,
  releaseMs: 40,
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
  ["beltMaxHz", "1-tile Hz", { min: 50, max: 8000, step: 10 }],
  ["beltMinLen", "min length", { min: 1, max: 512, step: 1 }],
  ["beltCutoff", "max length", { min: 1, max: 512, step: 1 }],
  ["beltShape", "sine→tri→saw", { min: 0, max: 1, step: 0.01 }],
  ["attackMs", "attack ms", { min: 0.5, max: 200, step: 0.5 }],
  ["releaseMs", "release ms", { min: 1, max: 1000, step: 1 }],
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
  ["lowCutHz", "low cut Hz", { min: 0, max: 1000, step: 5 }],
  ["---", "swap / route / merge"],
  ["g0Gain", "gain", { min: 0, max: 4, step: 0.01 }],
  ["g0Tone", "tone Hz", { min: 50, max: 12000, step: 10 }],
  ["g0DecayMs", "decay ms", { min: 0.2, max: 100, step: 0.1 }],
  ["---", "other gates"],
  ["g1Gain", "gain", { min: 0, max: 4, step: 0.01 }],
  ["g1Tone", "tone Hz", { min: 50, max: 12000, step: 10 }],
  ["g1DecayMs", "decay ms", { min: 0.2, max: 100, step: 0.1 }],
];

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
