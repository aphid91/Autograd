// The buttons' sound (button.js): a click when a finger lands on a button
// and another when it leaves, evoking a mechanical keyboard switch. Not a
// tick sound -- it is the player's own touch, so it plays the moment the
// synth hears of it, with none of the ticks' latency, and is never in a
// bake (presses are not on the board).
//
// One click: white noise under an attack/release envelope, rung through a
// two-pole resonator at the tone (the bright "tick"), plus a sine a little
// over an octave and a half below under the same envelope (the "pop" of the
// keycap bottoming out). Attack is linear; release is exponential, -60 dB
// at the release time, where the click ends. The noise is a per-click
// xorshift, so a click is the same every time.

const RING_S = 0.002;      // the resonator's own ring: short, so it stays a tick
const POP_RATIO = 0.3;     // the pop's pitch, against the tone
const NOISE_MIX = 0.75, POP_MIX = 0.45;
const NORM = 0.5;          // a click's peak at gain 1, about (tuned in tests/audio.test.mjs)
const MAX_CLICKS = 32;     // overlapping clicks at once; past this the oldest is dropped

export class KeyClicks {
  constructor(sampleRate) {
    this.sr = sampleRate;
    this.live = [];
  }

  // A finger landed (down) or left: one click, with that half's dials
  // ({ gain, tone, attackMs, releaseMs }).
  trigger(d) {
    if (!(d.gain > 0)) return;
    const sr = this.sr;
    const A = Math.max(1, Math.round(d.attackMs * sr / 1000));
    const R = Math.max(1, Math.round(d.releaseMs * sr / 1000));
    const w = 2 * Math.PI * Math.min(d.tone, sr * 0.45) / sr;
    const r = Math.exp(-1 / (RING_S * sr));
    // The resonator scaled to about unit peak for a noise drive at its tone.
    const g = (1 - r * r) * 0.5;
    if (this.live.length >= MAX_CLICKS) this.live.shift();
    this.live.push({
      t: 0, A, R, k: Math.log(1000) / R, amp: d.gain * NORM,
      c1: 2 * r * Math.cos(w), c2: -r * r, g, y1: 0, y2: 0,
      pw: w * POP_RATIO, seed: 0x9e3779b9 >>> 0,
    });
  }

  // Add the clicks sounding into out[0..n).
  render(out, n) {
    if (!this.live.length) return;
    for (const c of this.live) {
      let { t, y1, y2, seed } = c;
      const { A, R, k, amp, c1, c2, g, pw } = c;
      const end = A + R;
      for (let s = 0; s < n && t < end; s++, t++) {
        const env = t < A ? (t + 1) / A : Math.exp(-(t - A) * k);
        seed ^= seed << 13; seed >>>= 0; seed ^= seed >>> 17; seed ^= seed << 5; seed >>>= 0;
        const x = (seed / 2147483648 - 1) * env;
        const y = g * x + c1 * y1 + c2 * y2;
        y2 = y1; y1 = y;
        out[s] += amp * (NOISE_MIX * y * 4 + POP_MIX * Math.sin(pw * t) * env);
      }
      c.t = t; c.y1 = y1; c.y2 = y2; c.seed = seed;
    }
    this.live = this.live.filter(c => c.t < c.A + c.R);
  }
}

// The dials of one half, from the synth's params: down (a press) or up.
export function keyDials(p, down) {
  return down
    ? { gain: p.btnGain, tone: p.btnTone, attackMs: p.btnAttackMs, releaseMs: p.btnReleaseMs }
    : { gain: p.btnUpGain, tone: p.btnUpTone, attackMs: p.btnUpAttackMs, releaseMs: p.btnUpReleaseMs };
}
