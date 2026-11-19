// The Audio dials: defaults and the panel's spec. Tiny and dependency-free,
// so the panel can show them without loading any audio code -- audio.js and
// the worklet are only fetched when the player switches audio on.

export const AUDIO_DEFAULTS = {
  master: 0.5,          // output gain, before the soft clipper
  norm: 0.5,            // normalisation exponent p: each voice is scaled by 1 / N^p
  softClip: true,       // tanh on the way out; a safety net, not a compressor
  latencyMs: 60,        // how far the audio runs behind the sim
  // Belts: a drone per active run, pitch = maxHz / length (positions).
  beltGain: 1,
  beltMaxHz: 1760,      // a 1-tile run; 10 tiles is an octave above 20
  beltCutoff: 64,       // runs longer than this get no voice
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
  ["---", "belts"],
  ["beltGain", "gain", { min: 0, max: 4, step: 0.01 }],
  ["beltMaxHz", "1-tile Hz", { min: 50, max: 8000, step: 10 }],
  ["beltCutoff", "max length", { min: 1, max: 512, step: 1 }],
  ["beltShape", "sine→tri→saw", { min: 0, max: 1, step: 0.01 }],
  ["attackMs", "attack ms", { min: 0.5, max: 200, step: 0.5 }],
  ["releaseMs", "release ms", { min: 1, max: 1000, step: 1 }],
  ["beltTrack", "follow tick rate", { min: 0, max: 1, step: 0.05 }],
  ["trackRef", "at tick rate", { min: 1, max: 10000, step: 1 }],
  ["voiceLimit", "voice limit", { min: 8, max: VOICE_LIMIT_MAX, step: 8 }],
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
