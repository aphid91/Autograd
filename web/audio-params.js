// The Audio dials: defaults and the panel's spec. Tiny, importing only
// rate.js (one constant), so the panel can show them without loading any
// audio code -- audio.js and the worklet are only fetched when the player
// switches audio on.

import { RATE_MAX } from "./rate.js";

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
  // Gates at steady loudness: their gain x sqrt(GATE_REF_RATE / tick rate),
  // so a faster sim does not make the clicks louder (see audio-dsp.js
  // nextTick()). At 30 tps it changes nothing.
  gateSteady: false,
  g0Gain: 1,            // Swap, Route, Merge
  g0Tone: 1800,
  g0DecayMs: 3,
  g1Gain: 1,            // every other machine
  g1Tone: 1800,
  g1DecayMs: 3,
  // Both groups: when in its tick a fire clicks, and how many tones it may
  // click at (audio-dsp.js, clicks()).
  clickJitter: 1,       // 0: a tick's fires are one click at its start; 1: each at a random time in its tick; more: blurred across ticks
  clickDetune: 0,       // semitones: fires pick at random from the resonators spread +-this about the tone; 0 is one resonator
  clickRes: 5,          // resonators per group (with truthiness: the truthy half gets the larger share)
  // Truthiness (Sim.truthOn): a falsy fire (its result, or Swap/Route/Merge's
  // control, <= 0) clicks on its group's falsy resonators, and a run whose
  // newest item is <= 0 plays its drone with the falsy settings, crossfaded
  // on a change. Off, none of it runs. The falsy dials start as copies of
  // the truthy ones; the truthy half is the existing dials.
  truthAudio: false,
  beltGainF: 1,
  beltShapeF: 0.3,
  beltTransposeF: 0,    // semitones, against the run's own pitch
  beltBrightF: 1,       // 1 open; below, a low-pass closing by up to 7 octaves
  truthXfadeMs: 20,     // a sounding run's crossfade when its truthiness changes
  g0GainF: 1, g0ToneF: 1800, g0DecayMsF: 3,
  g1GainF: 1, g1ToneF: 1800, g1DecayMsF: 3,
  clickDetuneF: 0,
  // Advanced (advAudio): off, none of these is read and the synth renders
  // exactly as without them. The truthy half's set applies to every voice
  // and click; the falsy half's (...F) only with truthiness on.
  advAudio: false,
  attackMsF: 5, releaseMsF: 40,           // the falsy drone's own envelope
  beltPulse: 0, beltPW: 0.5,              // blend toward a pulse wave, and its width
  beltPulseF: 0, beltPWF: 0.5,
  tremRate: 5, tremDepth: 0,              // tremolo: Hz, 0..1 of the gain
  vibRate: 5, vibDepth: 0,                // vibrato: Hz, semitones
  tremRateF: 5, tremDepthF: 0,
  vibRateF: 5, vibDepthF: 0,
  clickNoise: 0, clickNoiseMs: 8,         // a click's exciter: 0 an impulse, 1 a noise burst this long
  clickNoiseF: 0, clickNoiseMsF: 8,
  // Space (space.js): camera-aware audio hears the screen, scaled about its
  // centre; the fringe and low-pass soften the screen's edge and the audio
  // region's alike. 0 and 0 is a hard edge, as the region always was.
  camAudio: false,
  camScale: 1,          // the screen box, about its centre, before it is heard
  spaceFringe: 0,       // fade-out distance, as a fraction of the box's longer side
  spaceLp: 0,           // how far the low-pass closes at the fringe's edge, 0..1
  // Buttons (button.js, audio-keys.js): a click when a finger lands on one
  // (press) and when it leaves (release). Played at once, never baked.
  btnGain: 1, btnTone: 2200, btnAttackMs: 0.3, btnReleaseMs: 35,
  btnUpGain: 0.6, btnUpTone: 3200, btnUpAttackMs: 0.3, btnUpReleaseMs: 20,
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
  ["truthAudio", "truthiness"],
  ["advAudio", "advanced audio"],
  ["gateSteady", "gates: steady loudness"],
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
  ["trackRef", "at tick rate", { min: 1, max: RATE_MAX, step: 1 }],
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
  ["---", "click timing"],
  ["clickJitter", "jitter (ticks)", { min: 0, max: 4, step: 0.05 }],
  ["clickDetune", "detune (semis)", { min: 0, max: 12, step: 0.05 }],
  ["clickRes", "resonators", { min: 1, max: 16, step: 1 }],
  ["---", "falsy (truthiness)"],
  ["beltGainF", "belt gain", { min: 0, max: 4, step: 0.01 }],
  ["beltShapeF", "belt sine→tri→saw", { min: 0, max: 1, step: 0.01 }],
  ["beltTransposeF", "belt transpose (semis)", { min: -24, max: 24, step: 0.5 }],
  ["beltBrightF", "belt brightness", { min: 0, max: 1, step: 0.01 }],
  ["truthXfadeMs", "belt crossfade ms", { min: 0, max: 500, step: 1 }],
  ["g0GainF", "s/r/m gain", { min: 0, max: 4, step: 0.01 }],
  ["g0ToneF", "s/r/m tone Hz", { min: 50, max: 12000, step: 10 }],
  ["g0DecayMsF", "s/r/m decay ms", { min: 0.2, max: 100, step: 0.1 }],
  ["g1GainF", "gates gain", { min: 0, max: 4, step: 0.01 }],
  ["g1ToneF", "gates tone Hz", { min: 50, max: 12000, step: 10 }],
  ["g1DecayMsF", "gates decay ms", { min: 0.2, max: 100, step: 0.1 }],
  ["clickDetuneF", "click detune (semis)", { min: 0, max: 12, step: 0.05 }],
  ["---", "advanced", { adv: true }],
  ["beltPulse", "belt pulse mix", { min: 0, max: 1, step: 0.01 }],
  ["beltPW", "belt pulse width", { min: 0.05, max: 0.5, step: 0.01 }],
  ["tremDepth", "tremolo depth", { min: 0, max: 1, step: 0.01 }],
  ["tremRate", "tremolo Hz", { min: 0.1, max: 30, step: 0.1 }],
  ["vibDepth", "vibrato (semis)", { min: 0, max: 2, step: 0.01 }],
  ["vibRate", "vibrato Hz", { min: 0.1, max: 30, step: 0.1 }],
  ["clickNoise", "click noise", { min: 0, max: 1, step: 0.01 }],
  ["clickNoiseMs", "click noise ms", { min: 0.2, max: 100, step: 0.1 }],
  ["attackMsF", "falsy attack ms", { min: 0.5, max: 200, step: 0.5 }],
  ["releaseMsF", "falsy release ms", { min: 1, max: 1000, step: 1 }],
  ["beltPulseF", "falsy pulse mix", { min: 0, max: 1, step: 0.01 }],
  ["beltPWF", "falsy pulse width", { min: 0.05, max: 0.5, step: 0.01 }],
  ["tremDepthF", "falsy tremolo depth", { min: 0, max: 1, step: 0.01 }],
  ["tremRateF", "falsy tremolo Hz", { min: 0.1, max: 30, step: 0.1 }],
  ["vibDepthF", "falsy vibrato (semis)", { min: 0, max: 2, step: 0.01 }],
  ["vibRateF", "falsy vibrato Hz", { min: 0.1, max: 30, step: 0.1 }],
  ["clickNoiseF", "falsy click noise", { min: 0, max: 1, step: 0.01 }],
  ["clickNoiseMsF", "falsy click noise ms", { min: 0.2, max: 100, step: 0.1 }],
  ["---", "buttons"],
  ["btnGain", "press gain", { min: 0, max: 4, step: 0.01 }],
  ["btnTone", "press tone Hz", { min: 100, max: 12000, step: 10 }],
  ["btnAttackMs", "press attack ms", { min: 0.05, max: 50, step: 0.05 }],
  ["btnReleaseMs", "press release ms", { min: 1, max: 500, step: 1 }],
  ["btnUpGain", "release gain", { min: 0, max: 4, step: 0.01 }],
  ["btnUpTone", "release tone Hz", { min: 100, max: 12000, step: 10 }],
  ["btnUpAttackMs", "release attack ms", { min: 0.05, max: 50, step: 0.05 }],
  ["btnUpReleaseMs", "release release ms", { min: 1, max: 500, step: 1 }],
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
// Belts silent whatever happens: their gain is 0, and with truthiness on the
// falsy one too. The recorder then records no run at all and the synth keeps
// no voices -- each would only ever have added exactly 0.0.
// The tick rate at which "gates: steady loudness" changes nothing.
export const GATE_REF_RATE = 30;

export const beltsMuted = (p) => !(p.beltGain > 0) && !(p.truthAudio && p.beltGainF > 0);

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
// The buttons' clicks are the player's touches, never in a bake, so their
// dials are realtime-only too.
export const REALTIME_ONLY = ["latencyMs", "holdUnderrun",
  "btnGain", "btnTone", "btnAttackMs", "btnReleaseMs", "btnUpGain", "btnUpTone", "btnUpAttackMs", "btnUpReleaseMs"];
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
