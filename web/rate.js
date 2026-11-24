// The tick-rate bounds: the one place the cap is stated. sim.js's Clock
// clamps to it and re-exports it; anything else offering a ticks/s dial --
// the speed box, video ticks/s, audio's reference rate -- reads it from here.
// A module of its own, with no imports, so the AudioWorklet (which loads
// audio-params.js) need not pull in the whole sim to learn one number.
export const RATE_MIN = 0.1;
export const RATE_MAX = 1000000;
