// The synthesiser: turns batches of ticks into samples. A plain class with
// no Web Audio in it, so audio-worklet.js wraps it on the audio thread and
// the tests drive it under node.
//
// Time: every tick the sim ran arrives here stamped only by its order. The
// synth plays them back at the tick rate they were run at, `latencyMs`
// behind, so a burst of ticks run in one frame comes out evenly spaced. If
// the sim falls behind (the frame budget drops ticks), the queue shrinks and
// playback slows to match: the clicks drop in pitch rather than gapping. If
// it runs dry, every voice releases and playback waits to refill -- or,
// with `holdUnderrun`, the voices hold where they are for up to HOLD_MAX_S
// and playback picks up the moment ticks arrive, with no refill wait.
//
// Before `startTick` the synth runs silent (voices still follow the runs,
// so a drone already sounding fades in rather than starting cold), and
// fades in over `fadeMs` from it.
//
// Offline (a bake): no latency, no speed control, no skipping, never a
// release for want of ticks -- the caller feeds every tick before rendering
// its samples, so tick k starts at sample k * sampleRate / rate exactly.
//
// Messages (from audio.js):
//   { type: "config", lengths: Float64Array, groups: [n0, n1] }
//       the belt runs that may sound (their lengths), and the gate counts
//       per group; sent whenever the sim's segments are rebuilt
//   { type: "ticks", n, tick0, words, rate, bits: Uint32Array, f0, f1: Uint16Array }
//       n ticks from tick0: per tick, `words` words of run-active bits, and
//       how many gates of each group fired
//   { type: "params", p }   the panel's dials
//   { type: "stop" }        paused: drop the queue, release everything

import { AUDIO_DEFAULTS, VOICE_LIMIT_MAX } from "./audio-params.js";
import { scaleTable, scaleHz } from "./scale.js";

const TABLE = 2048;
const MAX_QUEUE_S = 1;      // a stalled audio thread never builds more lag than this
const SPEED_TAU_S = 0.3;    // how quickly playback speed follows the queue
const MAX_SPEED = 1.25;     // catching up on a small backlog: barely audible
const SKIP_AT = 3;          // a backlog this many latencies deep is skipped, not raced through
const HOLD_MAX_S = 0.5;     // holding through an underrun gives up after this long
const TILT_CAP = Math.pow(10, 24 / 20);   // +24 dB
const SPACE_GLIDE_S = 0.04; // how fast a voice's spatial gain follows the camera
// Clicks. Each group has `clickRes` resonators (5 by default): the first at
// the dial's tone, the rest spread across +-clickDetune semitones. At detune
// 0 every fire goes to the first, which is exactly the one resonator of
// before. With Audio > truthiness a group's resonators are split, the
// truthy half the larger (ceil(n / 2)) and the falsy half at least one,
// each with its own tone, decay, gain and detune.
const RES_MAX = 16;
const RES_SLOTS = 2 * (RES_MAX + 1);       // both groups, at most
// Resonator j of n's share of the spread: 0, then -1, +1, then evenly
// smaller pairs -- for n = 5 exactly [0, -1, 1, -0.5, 0.5], as always.
function detuneAt(j, n) {
  if (j === 0) return 0;
  const pairs = Math.ceil((n - 1) / 2), q = (j - 1) >> 1;
  const mag = (pairs - q) / pairs;
  return (j - 1) & 1 ? mag : -mag;
}
const BRIGHT_HZ = 18000;    // a falsy run's brightness: open, closing 7 octaves below this at 0
const MAX_EVENTS = 64;      // a group's fires in one tick become at most this many clicks
const EVENT_POOL = 8192;    // clicks scheduled ahead: the pool's starting size...
// ...which grows to this. A realtime block schedules all its ticks' clicks
// before playing any (renderBlock()): at 100k tps with jitter that passed
// 8192 on cpu.npz, and the overflow played at the block's start.
const EVENT_MAX = 1 << 20;

// A 32-bit integer hash (lowbias32), for click times that are random but
// the same in every render of the same ticks: a bake stays bit-identical.
function hash32(x) {
  x ^= x >>> 16; x = Math.imul(x, 0x7feb352d);
  x ^= x >>> 15; x = Math.imul(x, 0x846ca68b);
  x ^= x >>> 16;
  return x >>> 0;
}
const unit = (a, b, c) => hash32(hash32(hash32(a | 0) ^ b) ^ c) / 4294967296;

export class GatesSynth {
  constructor(sampleRate, { offline = false } = {}) {
    this.sr = sampleRate;
    this.offline = offline;
    this.p = { ...AUDIO_DEFAULTS };
    this.table = new Float32Array(TABLE + 1);
    this.shape = -1;
    // Candidate runs (from config).
    this.n = 0;
    this.words = 0;
    this.lens = new Float64Array(0);
    this.voiceOf = new Int32Array(0);    // candidate -> voice slot, or -1
    this.prev = new Uint32Array(0);      // the last tick's active bits
    this.groups = [0, 0];
    this.active = 0;                     // candidates on in the current tick
    this.refused = 0;                    // ...of which have no voice
    // Voices, as parallel arrays; slots 0..nv-1 are live.
    const V = VOICE_LIMIT_MAX;
    this.vSeg = new Int32Array(V);       // candidate, or -1 once orphaned by a reconfig
    this.vOn = new Uint8Array(V);
    this.vEnv = new Float64Array(V);
    this.vPhase = new Float64Array(V);
    this.vInc = new Float64Array(V);
    this.vGain = new Float64Array(V);    // the per-length gain
    this.vAtk = new Float64Array(V);     // per-voice envelope coefficients:
    this.vRel = new Float64Array(V);     // sluggishness makes them depend on length
    // Spatial audio (space.js): per voice, the smoothed gain and the
    // distance low-pass (its one-pole coefficient, 1 = open, and state).
    this.vS = new Float64Array(V);
    this.vLpA = new Float64Array(V).fill(1);
    this.vLpY = new Float64Array(V);
    // Truthiness (truthAudio): per voice, its falsy target (vFT, and at the
    // block's start vFT0), the crossfade position (0 truthy .. 1 falsy), the
    // falsy oscillator's phase and low-pass (its coefficient and state).
    this.vFT = new Uint8Array(V); this.vFT0 = new Uint8Array(V);
    this.vX = new Float64Array(V); this.vPhF = new Float64Array(V);
    this.vLpAF = new Float64Array(V).fill(1); this.vLpYF = new Float64Array(V);
    this.vAtkF = new Float64Array(V); this.vRelF = new Float64Array(V);   // the falsy envelope (= vAtk/vRel unless advanced)
    // Advanced audio: the tremolo/vibrato, per half, as per-sample gain and
    // pitch factors over the span being rendered (lfo()); off, never read.
    this.lfoOn = false; this.lfoOff = 0;
    this.lfGT = new Float64Array(0); this.lfPT = new Float64Array(0);
    this.lfGF = new Float64Array(0); this.lfPF = new Float64Array(0);
    this.cTruth = new Uint8Array(0);     // per candidate: its run's truthiness (1 truthy)
    this.tableF = new Float32Array(TABLE + 1);
    this.shapeF = -1;
    this.ampSF = -1;
    // Per candidate: its spatial gain and cutoff (Hz, 0 = none), whether it
    // is waiting for a voice (refused), and whether a space is in force.
    this.sGain = new Float32Array(0);
    this.sLp = new Float32Array(0);
    this.isRef = new Uint8Array(0);
    this.spaced = false;
    this.bNorm = 0;                      // the weights' sum: what the drones normalise by
    this.ampS = -1;                      // the drones' smoothed normalised gain (spaced only)
    this.nv = 0;
    // The tick queue.
    this.queue = [];
    this.qi = 0;                         // next tick within queue[0]
    this.queued = 0;                     // ticks waiting
    this.rate = 1;                       // tick rate of the tick being played
    this.trackRate = -1;                 // rate the voices' pitches were set for
    this.playing = false;
    this.tickPos = 0;                    // fraction of the current tick elapsed
    this.speed = 1;
    this.played = 0;                     // offline: ticks applied since playback began
    this.sampleNo = 0;                   // offline: samples rendered since then
    this.held = false;                   // holding through an underrun
    this.heldS = 0;
    this.curTick = -1;                   // the tick being played, absolute
    this.fade = 0;                       // 0..1, the fade in from startTick
    // Click resonators: group g's are g*resS.. (truthy, then falsy):
    // y = c1*y1 + c2*y2 + x. Sized for the most there can be.
    const R = RES_SLOTS;
    this.R = 0; this.resS = 0; this.resT = 0; this.resF = 0;
    this.rowHas = new Uint8Array(R);
    // Advanced audio's noise exciter, per resonator: the share of a click
    // that is a noise burst (0: the impulse alone, never read), its scale,
    // its per-sample decay, the burst's level now, and its own noise
    // generator -- one per resonator, so a bake is the same however it is
    // chunked.
    this.rNoise = new Float64Array(R); this.rNScale = new Float64Array(R); this.rNDec = new Float64Array(R);
    this.nAmp = new Float64Array(R); this.nzS = new Uint32Array(R).map((_, r) => 0x9e3779b9 ^ (r * 2654435761));
    this.ry1 = new Float64Array(R); this.ry2 = new Float64Array(R);
    this.rc1 = new Float64Array(R); this.rc2 = new Float64Array(R); this.rx = new Float64Array(R);
    this.hx1 = this.hx2 = this.hy1 = this.hy2 = 0;   // the low cut's state
    this.imp = new Float64Array(R);      // impulse due on the next span's first sample
    // Clicks scheduled later than that (clickJitter): absolute sample,
    // resonator, amplitude. Unordered; span() takes the ones it covers.
    this.evAt = new Float64Array(EVENT_POOL); this.evR = new Uint8Array(EVENT_POOL);
    this.evA = new Float64Array(EVENT_POOL); this.evN = 0;
    this.inj = new Float64Array(0);      // span() scratch: per resonator, per sample
    this.abs = 0;                        // samples rendered, ever: the clicks' clock
    this.tps = 0;                        // realtime: ticks per sample, as last played
    // Realtime rendering by block (renderBlock()): the ticks due in a block
    // are applied first, each voice change logged with its sample offset,
    // then every voice renders the whole block switching at its own change
    // points. `batch` false is the old per-tick renderer, kept as the
    // reference tests/audio.test.mjs compares against.
    this.batch = true;
    this.logging = false;                // the walk is logging voice changes
    this.logAt = 0;                      // ...at this offset into the block
    this.lgN = 0;
    this.lgOff = new Int32Array(1024); this.lgV = new Int32Array(1024); this.lgVal = new Uint8Array(1024);
    this.lgNext = new Int32Array(1024);
    this.vHead = new Int32Array(V); this.vTgt0 = new Uint8Array(V);
    this.segOff = new Int32Array(64); this.segTick = new Float64Array(64); this.segN = 0;
    // Meter, since the last read.
    this.peak = 0; this.clipped = 0; this.ticksPlayed = 0;
    // Since the start, for the meter: realtime playback ran out of ticks
    // (an underrun: the ticks came late), or had a backlog skipped.
    this.underruns = 0; this.skips = 0;
    this.applyParams();
  }

  message(m) {
    if (m.type === "ticks") this.push(m);
    else if (m.type === "config") this.configure(m.lengths, m.groups, m.gen, m.truth);
    else if (m.type === "space") this.setSpace(m);
    else if (m.type === "params") { Object.assign(this.p, m.p); this.applyParams(); }
    else if (m.type === "stop") this.stop();
  }

  applyParams() {
    const p = this.p, sr = this.sr;
    // The scale's wavelength table, rebuilt only when what it depends on
    // changes (a slider drag elsewhere must not recompile the formula).
    const sk = `${p.scaleMode}|${p.scaleExpr}|${p.scaleLut}|${p.beltMinLen}`;
    if (sk !== this.scaleKey) { this.scaleKey = sk; this.scale = scaleTable(p).f; }
    const adv = this.adv = !!p.advAudio;
    // The drone's waveform: the shape dial, blended toward a pulse with
    // advanced audio (rebuilt only when what it depends on changes).
    const tk = adv && p.beltPulse > 0 ? `${p.beltShape}|${p.beltPulse}|${p.beltPW}` : p.beltShape;
    if (tk !== this.shape) { this.shape = tk; this.fillTable(this.table, p.beltShape, adv ? p.beltPulse : 0, p.beltPW); }
    // The resonators' layout; a new one drops whatever was ringing.
    const N = Math.min(RES_MAX, Math.max(1, Math.round(p.clickRes) || 5)), tr = !!p.truthAudio;
    const nT = tr ? Math.ceil(N / 2) : N, nF = tr ? Math.max(1, N - nT) : 0;
    if (nT !== this.resT || nF !== this.resF) {
      this.resT = nT; this.resF = nF; this.resS = nT + nF; this.R = 2 * this.resS;
      this.ry1.fill(0); this.ry2.fill(0); this.imp.fill(0); this.nAmp.fill(0); this.evN = 0;
    }
    const tune = (g, base, n, tone0, decay, detune, noise, noiseMs) => {
      const r = Math.exp(-1 / Math.max(1, sr * decay / 1000));
      const nLen = Math.max(1, sr * noiseMs / 1000);
      for (let j = 0; j < n; j++) {
        const k = base + j;
        const tone = Math.min(tone0 * Math.pow(2, (detune || 0) * detuneAt(j, n) / 12), sr * 0.45);
        const w = 2 * Math.PI * tone / sr;
        this.rc1[k] = 2 * r * Math.cos(w); this.rc2[k] = -r * r;
        this.rx[k] = Math.sin(w);        // impulse scale: the response peaks near 1
        // The noise burst: about as loud as the impulse it replaces.
        this.rNoise[k] = adv ? Math.min(1, Math.max(0, noise || 0)) : 0;
        this.rNScale[k] = this.rx[k] * 4 / Math.sqrt(nLen);
        this.rNDec[k] = Math.exp(-1 / nLen);
      }
    };
    for (let g = 0; g < 2; g++) {
      tune(g, g * this.resS, nT, p[`g${g}Tone`], p[`g${g}DecayMs`], p.clickDetune, p.clickNoise, p.clickNoiseMs);
      if (nF) tune(g, g * this.resS + nT, nF, p[`g${g}ToneF`], p[`g${g}DecayMsF`], p.clickDetuneF, p.clickNoiseF, p.clickNoiseMsF);
    }
    // Tremolo and vibrato, per half, with advanced audio.
    this.lfoT = adv && (p.tremDepth > 0 || p.vibDepth > 0);
    this.lfoF = adv && tr && (p.tremDepthF > 0 || p.vibDepthF > 0);
    this.lfoOn = this.lfoT || this.lfoF;
    if (!tr) { this.trF = 1; this.xStep = 1; }
    // The falsy drone: its waveform, transposition, low-pass and crossfade.
    if (tr) {
      const tkF = adv && p.beltPulseF > 0 ? `${p.beltShapeF}|${p.beltPulseF}|${p.beltPWF}` : p.beltShapeF;
      if (tkF !== this.shapeF) { this.shapeF = tkF; this.fillTable(this.tableF, p.beltShapeF, adv ? p.beltPulseF : 0, p.beltPWF); }
      this.trF = Math.pow(2, (p.beltTransposeF || 0) / 12);
      this.xStep = p.truthXfadeMs > 0 ? 1000 / (p.truthXfadeMs * sr) : 1;
      for (let v = 0; v < this.nv; v++) if (this.vSeg[v] >= 0) this.vLpAF[v] = this.lpCoefF(this.vSeg[v]);
    }
    this.trackRate = -1;                 // re-pitch every voice on the next tick
    for (let v = 0; v < this.nv; v++) if (this.vSeg[v] >= 0) { this.vGain[v] = this.lenGain(this.vSeg[v]); this.envFor(v, this.vSeg[v]); }
    // Low cut: an RBJ high-pass, Q = 1/sqrt(2) (Butterworth), on the mix.
    const fc = Math.min(p.lowCutHz, sr * 0.45);
    this.hpOn = fc > 0;
    if (this.hpOn) {
      const w = 2 * Math.PI * fc / sr, alpha = Math.sin(w) / Math.SQRT2, cw = Math.cos(w), a0 = 1 + alpha;
      this.hb0 = (1 + cw) / 2 / a0; this.hb1 = -(1 + cw) / a0; this.hb2 = this.hb0;
      this.ha1 = -2 * cw / a0; this.ha2 = (1 - alpha) / a0;
    } else { this.hx1 = this.hx2 = this.hy1 = this.hy2 = 0; }
  }

  // A voice's attack and release: the dials' times are a 10-tile run's, and
  // sluggishness s scales them by (length / 10)^s -- 0 the same for all, 1 a
  // 20-tile run twice as slow, -1 twice as quick.
  envFor(v, i) {
    const p = this.p, k = p.sluggish ? Math.pow(this.lens[i] / 10, p.sluggish) : 1;
    const coef = (ms) => 1 - Math.exp(-1 / Math.max(1, this.sr * ms * k / 1000));
    this.vAtk[v] = coef(p.attackMs);
    this.vRel[v] = coef(p.releaseMs);
    // The falsy half's own envelope, with advanced audio and truthiness.
    const own = this.adv && p.truthAudio;
    this.vAtkF[v] = own ? coef(p.attackMsF) : this.vAtk[v];
    this.vRelF[v] = own ? coef(p.releaseMsF) : this.vRel[v];
  }

  // Advanced audio's tremolo and vibrato over `len` samples from absolute
  // sample `abs0`: a function of the sample clock alone, so how the
  // rendering is split never changes it. The truthy half's dials, then the
  // falsy half's; a half with nothing set stays at 1.
  lfo(len, abs0, off) {
    this.lfoOff = off;
    if (this.lfGT.length < len) for (const k of ["lfGT", "lfPT", "lfGF", "lfPF"]) this[k] = new Float64Array(Math.max(len, 128));
    const p = this.p, sr = this.sr, TAU = 2 * Math.PI;
    const fill = (on, G, P, tR, tD, vR, vD) => {
      for (let q = 0; q < len; q++) {
        const t = (abs0 + q) / sr;
        G[q] = on && tD > 0 ? 1 - tD * 0.5 * (1 - Math.cos(TAU * tR * t)) : 1;
        P[q] = on && vD > 0 ? Math.pow(2, vD * Math.sin(TAU * vR * t) / 12) : 1;
      }
    };
    fill(this.lfoT, this.lfGT, this.lfPT, p.tremRate, p.tremDepth, p.vibRate, p.vibDepth);
    fill(this.lfoF, this.lfGF, this.lfPF, p.tremRateF, p.tremDepthF, p.vibRateF, p.vibDepthF);
  }

  // A run's own gain: the tone dials for lengths 1-6, 1 for anything longer.
  // Times the pitch tilt: (length / min length)^tilt, so a min-length run is
  // unchanged and each doubling of length is 6 dB louder per unit of tilt --
  // a small speaker (and an ear) loses about that much per octave down.
  // Capped at +24 dB so a long run at a steep tilt cannot swamp the mix.
  lenGain(i) {
    const p = this.p, L = Math.round(this.lens[i]);
    const g = L >= 1 && L <= 6 ? p[`lenGain${L}`] : 1;
    if (!p.pitchTilt) return g;
    return g * Math.min(TILT_CAP, Math.pow(this.lens[i] / Math.max(1, p.beltMinLen), p.pitchTilt));
  }

  // One cycle, blended sine -> triangle -> saw, with a guard sample.
  buildTable(s) {
    this.shape = s;
    this.fillTable(this.table, s);
  }
  // `pulse` (advanced audio) blends toward a pulse wave `pw` of the cycle
  // high, centred so it has no offset; 0 leaves the shape exactly as it was.
  fillTable(table, s, pulse = 0, pw = 0.5) {
    for (let i = 0; i <= TABLE; i++) {
      const x = (i % TABLE) / TABLE;
      const sine = Math.sin(2 * Math.PI * x);
      const tri = x < 0.25 ? 4 * x : x < 0.75 ? 2 - 4 * x : 4 * x - 4;
      const saw = x < 0.5 ? 2 * x : 2 * x - 2;
      table[i] = s < 0.5 ? sine + (tri - sine) * s * 2 : tri + (saw - tri) * (s - 0.5) * 2;
      if (pulse > 0) {
        const w = Math.min(0.5, Math.max(0.01, pw));
        const pv = (x < w ? 1 : -1) - (2 * w - 1);   // mean zero
        table[i] += (pv - table[i]) * pulse;
      }
    }
  }

  configure(lengths, groups, gen = null, truth = null) {
    // The recorder's generation: a tick batch from another one is dropped.
    // Ticks can come straight from the sim worker (a second port) while
    // configs come from the main thread, so the two are not ordered.
    this.gen = gen;
    // Old voices keep sounding with their pitch and release out; they just
    // no longer belong to a candidate.
    for (let v = 0; v < this.nv; v++) { this.vSeg[v] = -1; this.vOn[v] = 0; }
    this.lens = Float64Array.from(lengths);
    this.n = this.lens.length;
    this.words = (this.n + 31) >>> 5;
    this.voiceOf = new Int32Array(this.n).fill(-1);
    this.prev = new Uint32Array(this.words);
    this.groups = [Number(groups[0]) || 0, Number(groups[1]) || 0];
    this.active = 0; this.refused = 0;
    this.sGain = new Float32Array(this.n).fill(1);
    this.sLp = new Float32Array(this.n);
    this.isRef = new Uint8Array(this.n);
    this.cTruth = truth && truth.length === this.n ? Uint8Array.from(truth) : new Uint8Array(this.n).fill(1);
    this.spaced = false;
    // Ticks already queued were recorded against the old candidates.
    this.queue = []; this.qi = 0; this.queued = 0;
  }

  push(b) {
    if (b.words !== this.words || !b.n) return;   // from before the last config
    if (b.gen != null && this.gen != null && b.gen !== this.gen) return;
    this.queue.push(b);
    this.queued += b.n;
    if (this.offline) return;
    // Never build more than MAX_QUEUE_S of lag: drop the oldest ticks --
    // but always keep room for a few ticks. At 1 tps one tick is a second,
    // and a flat 1 s cap held less than the 1.5 ticks playback waits for,
    // so every other tick was dropped and the drone moved at half speed.
    const rate = Math.max(b.rate, 1e-3);
    const cap = Math.max(MAX_QUEUE_S, (SKIP_AT + 1) * 1.5 / rate);
    while (this.queue.length > 1 && this.queued / rate > cap) {
      const old = this.queue.shift();
      this.flips(old, this.qi, old.n);   // its truthiness changes still hold
      this.queued -= old.n - this.qi;
      this.qi = 0;
    }
  }

  // Drop `seconds` of the oldest queued ticks unplayed. Their run changes
  // are not lost: the next tick applied is compared against the last one
  // applied. Their clicks are.
  skip(seconds) {
    if (!this.queue.length) return;
    let drop = Math.floor(seconds * Math.max(this.queue[0].rate, 1e-3));
    while (drop > 0 && this.queue.length) {
      const b = this.queue[0], left = b.n - this.qi, d = Math.min(drop, left);
      if (d === left && this.queue.length === 1) break;   // keep the newest tick
      this.flips(b, this.qi, this.qi + d);
      this.qi += d; this.queued -= d; drop -= d;
      if (this.qi >= b.n) { this.queue.shift(); this.qi = 0; }
    }
  }

  stop() {
    this.queue = []; this.qi = 0; this.queued = 0;
    this.evN = 0;                        // clicks scheduled ahead are dropped too
    this.gatesOff();
  }

  // Stop playback and release every voice; the next tick played gates
  // whatever it says afresh.
  gatesOff() {
    this.playing = false;
    this.held = false; this.heldS = 0;
    this.tickPos = 0;
    for (let v = 0; v < this.nv; v++) { if (this.logging && this.vOn[v]) this.logOn(v, 0); this.vOn[v] = 0; }
    this.prev.fill(0);
    // The flags with the count: left set, a later off() took the count
    // below zero (the meter showed "-1 refused" after an underrun).
    this.isRef.fill(0);
    this.active = 0; this.refused = 0;
  }

  // --- voices -------------------------------------------------------------------

  pitch(i) {
    const p = this.p;
    const track = p.beltTrack > 0 ? Math.pow(this.rate / Math.max(p.trackRef, 1e-3), p.beltTrack) : 1;
    // The dial is a min-length run's pitch: raising min length transposes
    // everything up, the shortest audible run always at the dial's pitch.
    // The scale (scale.js) decides the rest: maxHz * f(min) / f(length),
    // which for the standard f(n) = n is maxHz * min / length.
    return Math.min(scaleHz(this.scale, this.lens[i], p.beltMaxHz, p.beltMinLen) * track, this.sr * 0.45);
  }

  // A run out of earshot (spatial gain 0) takes no voice: on a big board
  // seen through a small screen, the runs far away would otherwise fill the
  // voice limit with silence.
  on(i) {
    if (this.sGain[i] <= 0) return;
    const v = this.voiceOf[i];
    if (v >= 0) { if (this.logging && !this.vOn[v]) this.logOn(v, 1); this.vOn[v] = 1; return; }
    if (this.nv >= Math.min(this.p.voiceLimit, VOICE_LIMIT_MAX)) {
      if (!this.isRef[i]) { this.isRef[i] = 1; this.refused++; }
      return;
    }
    this.alloc(i);
  }

  // New spatial weights for the same candidates (the camera moved): runs
  // already on take or give up their voices to match, and every voice's
  // filter follows; its gain glides there (span()).
  setSpace(m) {
    if (m.gain.length !== this.n) return;   // for an older config
    this.spaced = !!m.on;
    this.sGain = Float32Array.from(m.gain);
    this.sLp = Float32Array.from(m.lp);
    this.bNorm = m.norm;
    this.groups = [Number(m.groups[0]) || 0, Number(m.groups[1]) || 0];
    for (let v = 0; v < this.nv; v++) if (this.vSeg[v] >= 0) {
      this.vLpA[v] = this.lpCoef(this.sLp[this.vSeg[v]]);
      if (this.p.truthAudio) this.vLpAF[v] = this.lpCoefF(this.vSeg[v]);
    }
    for (let w = 0; w < this.words; w++) {
      let bits = this.prev[w];
      while (bits) {
        const b = 31 - Math.clz32(bits);
        bits &= ~(1 << b);
        const i = (w << 5) | b, v = this.voiceOf[i];
        if (this.sGain[i] <= 0) {
          if (v >= 0) this.vOn[v] = 0;
          if (this.isRef[i]) { this.isRef[i] = 0; this.refused--; }
        } else this.on(i);
      }
    }
  }

  lpCoef(hz) { return hz > 0 ? 1 - Math.exp(-2 * Math.PI * Math.min(hz, this.sr * 0.45) / this.sr) : 1; }
  // A falsy drone's low-pass: the distance cutoff, closed further by the
  // brightness dial (7 octaves at 0), from 18 kHz where there is none.
  lpCoefF(i) {
    const b = this.p.beltBrightF, hz = this.sLp[i];
    if (!(b < 1)) return this.lpCoef(hz);
    return this.lpCoef((hz > 0 ? hz : BRIGHT_HZ) * Math.pow(2, -7 * (1 - Math.max(0, b))));
  }

  // Ticks [k0, k1) of batch b's truthiness changes, onto the candidates and
  // their voices (logged when inside a block walk).
  flips(b, k0, k1) {
    if (!b.tf || !this.p.truthAudio || k1 <= k0) return;
    const to = b.to, tf = b.tf, a = k0 > 0 ? to[k0 - 1] : 0, e = to[k1 - 1];
    for (let q = a; q < e; q++) {
      const i = tf[q] >> 1, bit = tf[q] & 1;
      if (i >= this.n) continue;
      this.cTruth[i] = bit;
      const v = this.voiceOf[i], f = 1 - bit;
      if (v >= 0 && this.vFT[v] !== f) { if (this.logging) this.logOn(v, 2 + f); this.vFT[v] = f; }
    }
  }

  alloc(i) {
    const v = this.nv++;
    this.vSeg[v] = i; this.vOn[v] = 1; this.vEnv[v] = 0; this.vPhase[v] = 0;
    this.vInc[v] = this.pitch(i) / this.sr;
    // Allocated mid-block: silent until its offset (target 0, envelope 0),
    // and its phase wound back so it is 0 there, as if it started then.
    if (this.logging) {
      this.vTgt0[v] = 0;
      this.logOn(v, 1);
      // Under vibrato the pitch moved over those samples: wound back by
      // what they actually advanced.
      let span = this.logAt;
      if (this.lfoOn) { span = 0; for (let q = 0; q < this.logAt; q++) span += this.lfPT[q]; }
      const ph = -span * this.vInc[v];
      this.vPhase[v] = ph - Math.floor(ph);
    }
    this.vGain[v] = this.lenGain(i);
    this.vS[v] = this.sGain[i];
    this.vLpA[v] = this.lpCoef(this.sLp[i]); this.vLpY[v] = 0;
    // Truthiness: straight into its run's state, no crossfade.
    this.vFT[v] = this.vFT0[v] = this.vX[v] = this.cTruth[i] ? 0 : 1;
    // The falsy oscillator is wound back as the truthy one is, at its own
    // rate, so both are at 0 where the voice starts.
    let spanF = this.logAt;
    if (this.logging && this.lfoOn) { spanF = 0; for (let q = 0; q < this.logAt; q++) spanF += this.lfPF[q]; }
    const phF = this.logging && this.p.truthAudio ? -spanF * Math.min(this.vInc[v] * this.trF, 0.45) : 0;
    this.vPhF[v] = phF - Math.floor(phF); this.vLpYF[v] = 0;
    if (this.p.truthAudio) this.vLpAF[v] = this.lpCoefF(i);
    this.envFor(v, i);
    this.voiceOf[i] = v;
    if (this.isRef[i]) { this.isRef[i] = 0; this.refused--; }
  }

  off(i) {
    const v = this.voiceOf[i];
    if (v >= 0) { if (this.logging && this.vOn[v]) this.logOn(v, 0); this.vOn[v] = 0; }
    if (this.isRef[i]) { this.isRef[i] = 0; this.refused--; }
  }

  free(v) {
    const last = --this.nv;
    if (this.vSeg[v] >= 0) this.voiceOf[this.vSeg[v]] = -1;
    if (v !== last) {
      this.vSeg[v] = this.vSeg[last]; this.vOn[v] = this.vOn[last]; this.vEnv[v] = this.vEnv[last];
      this.vPhase[v] = this.vPhase[last]; this.vInc[v] = this.vInc[last]; this.vGain[v] = this.vGain[last];
      this.vAtk[v] = this.vAtk[last]; this.vRel[v] = this.vRel[last];
      this.vS[v] = this.vS[last]; this.vLpA[v] = this.vLpA[last]; this.vLpY[v] = this.vLpY[last];
      this.vFT[v] = this.vFT[last]; this.vFT0[v] = this.vFT0[last]; this.vX[v] = this.vX[last];
      this.vAtkF[v] = this.vAtkF[last]; this.vRelF[v] = this.vRelF[last];
      this.vPhF[v] = this.vPhF[last]; this.vLpAF[v] = this.vLpAF[last]; this.vLpYF[v] = this.vLpYF[last];
      if (this.vSeg[v] >= 0) this.voiceOf[this.vSeg[v]] = v;
    }
    // A run refused a voice earlier takes the one just freed.
    if (this.refused > 0) {
      for (let w = 0; w < this.words; w++) {
        let bits = this.prev[w];
        while (bits) {
          const b = 31 - Math.clz32(bits);
          bits &= ~(1 << b);
          const i = (w << 5) | b;
          if (this.voiceOf[i] < 0 && this.isRef[i]) { this.alloc(i); return; }
        }
      }
    }
  }

  // Apply the next queued tick, or report that there is none.
  nextTick() {
    if (!this.queued) return false;
    const b = this.queue[0], k = this.qi;
    this.rate = b.rate;
    this.curTick = (b.tick0 ?? 0) + k;
    if (this.rate !== this.trackRate) {
      this.trackRate = this.rate;
      for (let v = 0; v < this.nv; v++) if (this.vSeg[v] >= 0) this.vInc[v] = this.pitch(this.vSeg[v]) / this.sr;
    }
    const p = this.p, norm = p.norm;
    this.clicks(0, b.f0[k] * p.g0Gain / Math.pow(Math.max(1, this.groups[0]), norm), b.f0[k]);
    this.clicks(1, b.f1[k] * p.g1Gain / Math.pow(Math.max(1, this.groups[1]), norm), b.f1[k]);
    if (p.truthAudio && b.f0F) {
      this.clicks(0, b.f0F[k] * p.g0GainF / Math.pow(Math.max(1, this.groups[0]), norm), b.f0F[k], 1);
      this.clicks(1, b.f1F[k] * p.g1GainF / Math.pow(Math.max(1, this.groups[1]), norm), b.f1F[k], 1);
      this.flips(b, k, k + 1);
    }
    const W = this.words, off = k * W, prev = this.prev, bits = b.bits;
    for (let w = 0; w < W; w++) {
      const now = bits[off + w];
      let d = now ^ prev[w];
      if (!d) continue;
      prev[w] = now;
      while (d) {
        const bit = 31 - Math.clz32(d);
        d &= ~(1 << bit);
        const i = (w << 5) | bit;
        if ((now >>> bit) & 1) { this.active++; this.on(i); } else { this.active--; this.off(i); }
      }
    }
    if (++this.qi >= b.n) { this.queue.shift(); this.qi = 0; }
    this.queued--;
    this.ticksPlayed++;
    return true;
  }

  // A group's fires in the tick being applied: `amp` in all, from `count`
  // fires (a weighted count under a space, so not always whole).
  //
  // clickJitter 0: one impulse at the tick's start, as many fires only
  // louder -- the original sound. Above 0 each fire is its own click at a
  // uniformly random time up to clickJitter ticks after the tick's start:
  // a Poisson process conditioned on the tick's count, so 1 spreads the
  // fires through their own tick and more blurs the tick grid away. The
  // times are a hash of (tick, group, fire), so a bake renders the same.
  // Many fires add in power rather than amplitude, so a busy tick is
  // denser but quieter than at 0 -- deliberately.
  clicks(g, amp, count, half = 0) {
    if (!(amp > 0)) return;
    const p = this.p, J = p.clickJitter || 0, spread = ((half ? p.clickDetuneF : p.clickDetune) || 0) > 0;
    // This half's resonators, and its own salt for the hash (the truthy
    // half's is the one clicks always had).
    const base = g * this.resS + (half ? this.resT : 0), NR = half ? this.resF : this.resT, salt = 2 * g + 4 * half;
    // In a block walk the tick starts logAt samples into the block, so
    // "now" is an event there rather than the block's first sample.
    const at = this.logging ? this.logAt : 0;
    if (J <= 0 && !spread) {
      if (at > 0 && this.evRoom()) { const e = this.evN++; this.evAt[e] = this.abs + at; this.evR[e] = base; this.evA[e] = amp; }
      else this.imp[base] += amp;
      return;
    }
    const n = Math.max(1, Math.min(MAX_EVENTS, Math.ceil(count - 1e-6))), a = amp / n;
    const t = this.curTick, sr = this.sr;
    // Where tick k (the one being applied) starts, and where an offset of
    // o ticks lands: exact sample arithmetic offline, the playback speed
    // realtime (tickPos is how far past the tick's start playback already is).
    const k = this.played, rate = Math.max(this.rate, 1e-3);
    const exact = this.offline && !this.held;
    const k0 = exact ? Math.round(k * sr / rate) : 0;
    const tps = this.tps > 0 ? this.tps : rate / sr;
    for (let j = 0; j < n; j++) {
      const r = base + (spread ? Math.min(NR - 1, Math.floor(unit(t, salt + 1, j) * NR)) : 0);
      const o = J > 0 ? unit(t, salt, j) * J : 0;
      const d0 = exact ? Math.round((k + o) * sr / rate) - k0 : Math.round((o - this.tickPos) / tps);
      const d = Math.max(0, d0) + at;
      if (d <= 0) { this.imp[r] += a; continue; }
      if (!this.evRoom()) { this.imp[r] += a; continue; }   // full: play it now
      const e = this.evN++;
      this.evAt[e] = this.abs + d; this.evR[e] = r; this.evA[e] = a;
    }
  }

  // Room for one more scheduled click, growing the pool if need be.
  evRoom() {
    if (this.evN < this.evAt.length) return true;
    const m = this.evAt.length * 2;
    if (m > EVENT_MAX) return false;
    const grow = (a, T) => { const b = new T(m); b.set(a); return b; };
    this.evAt = grow(this.evAt, Float64Array); this.evR = grow(this.evR, Uint8Array); this.evA = grow(this.evA, Float64Array);
    return true;
  }

  // --- rendering --------------------------------------------------------------------

  // Fill out[0..n) with mono samples.
  render(out, n) {
    const sr = this.sr, p = this.p;
    // Start once the queue holds the latency's worth; follow the queue's
    // depth with the playback speed.
    // At a slow tick rate a single tick outlasts the latency (200 ms at 5
    // tps against 60 ms), so the target is at least a tick and a half --
    // otherwise one queued tick reads as a backlog to skip and the queue
    // runs dry between every pair of ticks.
    const qRate = () => Math.max(this.queue.length ? this.queue[0].rate : this.rate, 1e-3);
    const target = Math.max(p.latencyMs / 1000, 1.5 / qRate());
    let queuedS = this.queued / qRate();
    // A tick takes effect at its start: tickPos is how far through the one
    // already applied playback is.
    if (this.held) {
      // Ticks again: carry straight on. Too long without: let go.
      if (this.queued) { this.held = false; this.heldS = 0; this.tickPos = 0; if (this.nextTick()) this.played++; }
      else if ((this.heldS += n / sr) > HOLD_MAX_S && !this.offline) this.gatesOff();
    }
    if (!this.playing && this.queued && (this.offline || queuedS >= target)) {
      this.playing = true; this.speed = 1; this.tickPos = 0; this.played = 0;
      this.tps = this.rate / sr;
      this.played = this.nextTick() ? 1 : 0; this.sampleNo = 0;
      // Measured again: starting took a tick off the queue. Skipping on the
      // stale figure once skip()'d an empty queue -- a throw on the audio
      // thread, and an AudioWorkletProcessor that throws is silent for good.
      queuedS = this.queued / qRate();
    }
    if (this.offline) this.speed = 1;
    else {
      if (this.playing && queuedS > SKIP_AT * target) { this.skips++; this.skip(queuedS - target); }
      if (this.playing) {
        const want = Math.min(MAX_SPEED, Math.max(0.25, queuedS / target));
        this.speed += (want - this.speed) * Math.min(1, n / sr / SPEED_TAU_S);
      }
    }
    if (!this.offline && this.batch) this.renderBlock(out, n);
    else for (let i = 0; i < n; ) {
      let span = n - i;
      if (this.offline && this.playing && !this.held) {
        // Exact: tick k begins at sample round(k * sr / rate), however the
        // caller splits the rendering -- so a bake is the same to the bit
        // whatever its chunks.
        const next = Math.round(this.played * sr / this.rate);
        if (next <= this.sampleNo) {
          if (this.nextTick()) this.played++; else this.held = true;
          continue;
        }
        span = Math.min(span, next - this.sampleNo);
        this.span(out, i, span, this.sampleNo + span === next);
        i += span; this.sampleNo += span;
        continue;
      }
      const tps = this.rate * this.speed / sr;   // ticks per sample
      this.tps = tps;
      const moving = this.playing && !this.held;
      if (moving) span = Math.min(span, Math.max(1, Math.ceil((1 - this.tickPos) / tps)));
      // Realtime frees voices at the end of the call (below), as renderBlock()
      // does, so this path is its exact reference; a bake frees on tick edges.
      this.span(out, i, span, false);
      i += span;
      if (moving) {
        this.tickPos += span * tps;
        while (this.tickPos >= 1) {
          this.tickPos -= 1;
          if (!this.nextTick()) {
            if (!this.offline) this.underruns++;
            if (this.offline || this.p.holdUnderrun) { this.held = true; this.heldS = 0; this.tickPos = 0; }
            else this.gatesOff();
            break;
          }
        }
      }
    }
    if (!this.offline && !this.batch) for (let v = this.nv - 1; v >= 0; v--) if (!this.vOn[v] && this.vEnv[v] < 1e-4) this.free(v);
    // The low cut, per sample (so a bake is the same however it is split).
    if (this.hpOn) {
      const b0 = this.hb0, b1 = this.hb1, b2 = this.hb2, a1 = this.ha1, a2 = this.ha2;
      let x1 = this.hx1, x2 = this.hx2, y1 = this.hy1, y2 = this.hy2;
      for (let s = 0; s < n; s++) {
        const x = out[s];
        let y = b0 * x + b1 * x1 + b2 * x2 - a1 * y1 - a2 * y2;
        if (y < 1e-20 && y > -1e-20) y = 0;
        x2 = x1; x1 = x; y2 = y1; y1 = y;
        out[s] = y;
      }
      this.hx1 = x1; this.hx2 = x2; this.hy1 = y1; this.hy2 = y2;
    }
    // Master, the meter, and the clipper.
    const master = p.master, clip = p.softClip;
    let peak = this.peak, clipped = this.clipped;
    for (let s = 0; s < n; s++) {
      const x = out[s] * master, a = x < 0 ? -x : x;
      if (a > peak) peak = a;
      if (a > 1) clipped++;
      out[s] = clip ? Math.tanh(x) : x;
    }
    this.peak = peak; this.clipped = clipped;
  }

  // Realtime, one block at a time. The old way (`batch` false) cut the
  // block at every tick boundary and ran every voice's setup per piece: at
  // 20k tps that is a piece every 2.4 samples, so the cost grew with the
  // tick rate until a phone missed its deadline. Here the block's ticks are
  // applied first -- the same walk, the same boundaries, each voice's
  // change logged with its sample offset -- and then each voice renders the
  // whole block, switching its target at its own change points. The same
  // sound, at a cost of O(voices + changes) per block rather than
  // O(ticks x voices). Three small differences from the old way, none
  // audible: a finished voice is freed at the block's end, not at a tick
  // boundary inside it (so a refused run waits up to a block for its
  // voice); a voice allocated mid-block runs silent from the block's start
  // with its phase wound back to 0 at its offset; and the spatial glide
  // steps once a block.
  renderBlock(out, n) {
    const sr = this.sr, nv0 = this.nv;
    for (let v = 0; v < nv0; v++) this.vTgt0[v] = this.vOn[v];
    // Truthiness, or advanced audio's tremolo/vibrato: the two-oscillator
    // voice (truthRun()); otherwise the plain one, exactly as before.
    const truth = !!this.p.truthAudio || this.lfoOn;
    if (truth) for (let v = 0; v < nv0; v++) this.vFT0[v] = this.vFT[v];
    // Before the walk, which allocates voices against it (alloc()).
    if (this.lfoOn) this.lfo(n, this.abs, 0);
    this.lgN = 0; this.segN = 0;
    this.pushSeg(0);
    this.logging = true; this.logAt = 0;
    try {
      let i = 0;
      while (i < n && this.playing && !this.held) {
        const tps = this.rate * this.speed / sr;   // ticks per sample
        this.tps = tps;
        const span = Math.min(n - i, Math.max(1, Math.ceil((1 - this.tickPos) / tps)));
        i += span;
        this.tickPos += span * tps;
        this.logAt = i;
        let applied = false;
        while (this.tickPos >= 1) {
          this.tickPos -= 1;
          if (!this.nextTick()) {
            this.underruns++;
            if (this.p.holdUnderrun) { this.held = true; this.heldS = 0; this.tickPos = 0; }
            else this.gatesOff();
            break;
          }
          applied = true;
        }
        if (applied) this.pushSeg(i);
      }
      if (!this.playing || this.held) this.tps = this.rate * this.speed / sr;
    } finally { this.logging = false; }

    this.resonate(out, 0, n);

    // Each voice's changes, in order: a chain through the log.
    const nv = this.nv, head = this.vHead, next = this.lgNext, lgV = this.lgV;
    for (let v = 0; v < nv; v++) head[v] = -1;
    for (let k = this.lgN - 1; k >= 0; k--) { const v = lgV[k]; next[k] = head[v]; head[v] = k; }
    const T = this.table, off = this.lgOff, val = this.lgVal;
    let amp = this.p.beltGain / Math.pow(Math.max(1, this.n), this.p.norm);
    const glide = this.spaced ? 1 - Math.exp(-n / (sr * SPACE_GLIDE_S)) : 1;
    if (this.spaced) {
      const want = this.p.beltGain / Math.pow(Math.max(1, this.bNorm), this.p.norm);
      this.ampS = this.ampS < 0 ? want : this.ampS + (want - this.ampS) * glide;
      amp = this.ampS;
    } else this.ampS = -1;
    if (truth) {
      const ampF = this.ampFalsy(glide);
      for (let v = 0; v < nv; v++) {
        if (this.spaced && this.vSeg[v] >= 0) this.vS[v] += (this.sGain[this.vSeg[v]] - this.vS[v]) * glide;
        const sg = this.vGain[v] * (this.spaced ? this.vS[v] : 1);
        let tgt = this.vTgt0[v], ft = this.vFT0[v], s = 0, k = head[v];
        for (;;) {
          const e = k >= 0 ? off[k] : n;
          this.truthRun(out, v, s, e, tgt, ft, amp * sg, ampF * sg);
          s = e;
          if (k < 0) break;
          if (val[k] >= 2) ft = val[k] - 2; else tgt = val[k];
          k = next[k];
        }
      }
    } else for (let v = 0; v < nv; v++) {
      let env = this.vEnv[v], ph = this.vPhase[v];
      if (this.spaced && this.vSeg[v] >= 0) this.vS[v] += (this.sGain[this.vSeg[v]] - this.vS[v]) * glide;
      const inc = this.vInc[v], atk = this.vAtk[v], rel = this.vRel[v];
      const g = amp * this.vGain[v] * (this.spaced ? this.vS[v] : 1), a = this.vLpA[v];
      let y = this.vLpY[v], tgt = this.vTgt0[v], s = 0, k = head[v];
      for (;;) {
        const e = k >= 0 ? off[k] : n, coef = tgt ? atk : rel;
        if (a >= 1) {
          for (; s < e; s++) {
            env += (tgt - env) * coef;
            const fi = ph * TABLE, j = fi | 0;
            out[s] += g * env * (T[j] + (T[j + 1] - T[j]) * (fi - j));
            ph += inc;
            if (ph >= 1) ph -= 1;
          }
        } else {
          for (; s < e; s++) {
            env += (tgt - env) * coef;
            const fi = ph * TABLE, j = fi | 0;
            y += a * ((T[j] + (T[j + 1] - T[j]) * (fi - j)) - y);
            out[s] += g * env * y;
            ph += inc;
            if (ph >= 1) ph -= 1;
          }
        }
        if (k < 0) break;
        tgt = val[k]; k = next[k];
      }
      this.vEnv[v] = env; this.vPhase[v] = ph; this.vLpY[v] = y;
    }
    for (let v = this.nv - 1; v >= 0; v--) if (!this.vOn[v] && this.vEnv[v] < 1e-4) this.free(v);

    // Silent before startTick, then the fade in, by the tick each stretch plays.
    const p = this.p;
    for (let j = 0; j < this.segN; j++) {
      const a0 = this.segOff[j], a1 = j + 1 < this.segN ? this.segOff[j + 1] : n;
      if (a1 <= a0) continue;
      if (this.segTick[j] < p.startTick) { this.fade = 0; out.fill(0, a0, a1); }
      else if (this.fade < 1) {
        const inc = p.fadeMs > 0 ? 1000 / (p.fadeMs * sr) : 1;
        let f = this.fade;
        for (let q = a0; q < a1; q++) { f = Math.min(1, f + inc); out[q] *= f; }
        this.fade = f;
      }
    }
  }

  // The falsy drones' normalised gain: as the truthy one (amp), from
  // beltGainF, glided under a space as ampS is.
  ampFalsy(glide) {
    const p = this.p;
    if (!this.spaced) { this.ampSF = -1; return p.beltGainF / Math.pow(Math.max(1, this.n), p.norm); }
    const want = p.beltGainF / Math.pow(Math.max(1, this.bNorm), p.norm);
    this.ampSF = this.ampSF < 0 ? want : this.ampSF + (want - this.ampSF) * glide;
    return this.ampSF;
  }

  // Truthiness: voice v over out[s..e), its envelope heading for `tgt` and
  // its crossfade for `ft` (1 falsy) at the dial's pace. Two oscillators --
  // the truthy one as ever, the falsy one with its own waveform, pitch and
  // low-pass -- each computed only while it is heard.
  truthRun(out, v, s, e, tgt, ft, gT, gF) {
    if (s >= e) return;
    const T = this.table, TF = this.tableF, xs = this.xStep;
    const inc = this.vInc[v], incF = Math.min(inc * this.trF, 0.45);
    // The envelope runs at the pace of the half it is heading for (the two
    // are the same unless advanced audio gives the falsy half its own).
    const coef = tgt ? (ft ? this.vAtkF[v] : this.vAtk[v]) : (ft ? this.vRelF[v] : this.vRel[v]);
    const aT = this.vLpA[v], aF = this.vLpAF[v];
    const L = this.lfoOn, lo = this.lfoOff, GT = this.lfGT, PT = this.lfPT, GF = this.lfGF, PF = this.lfPF;
    let env = this.vEnv[v], ph = this.vPhase[v], phF = this.vPhF[v], x = this.vX[v], yT = this.vLpY[v], yF = this.vLpYF[v];
    for (; s < e; s++) {
      if (x !== ft) x = ft > x ? Math.min(ft, x + xs) : Math.max(ft, x - xs);
      env += (tgt - env) * coef;
      let o = 0, gt = gT, gf = gF, iT = inc, iF = incF;
      if (L) { const q = s - lo; gt *= GT[q]; gf *= GF[q]; iT *= PT[q]; iF *= PF[q]; }
      if (x < 1) {
        const fi = ph * TABLE, j = fi | 0;
        let w = T[j] + (T[j + 1] - T[j]) * (fi - j);
        if (aT < 1) { yT += aT * (w - yT); w = yT; }
        o += (1 - x) * gt * w;
      }
      if (x > 0) {
        const fi = phF * TABLE, j = fi | 0;
        let w = TF[j] + (TF[j + 1] - TF[j]) * (fi - j);
        if (aF < 1) { yF += aF * (w - yF); w = yF; }
        o += x * gf * w;
      }
      out[s] += env * o;
      ph += iT; if (ph >= 1) ph -= 1;
      phF += iF; if (phF >= 1) phF -= 1;
    }
    this.vEnv[v] = env; this.vPhase[v] = ph; this.vPhF[v] = phF; this.vX[v] = x; this.vLpY[v] = yT; this.vLpYF[v] = yF;
  }

  // A voice's target changed during the walk, `logAt` samples into the block.
  logOn(v, on) {
    if (this.lgN >= this.lgOff.length) {
      const m = this.lgOff.length * 2, grow = (a, T) => { const b = new T(m); b.set(a); return b; };
      this.lgOff = grow(this.lgOff, Int32Array); this.lgV = grow(this.lgV, Int32Array);
      this.lgVal = grow(this.lgVal, Uint8Array); this.lgNext = new Int32Array(m);
    }
    const k = this.lgN++;
    this.lgOff[k] = this.logAt; this.lgV[k] = v; this.lgVal[k] = on;
  }

  // From `at` on, the block plays tick curTick (for startTick and the fade).
  pushSeg(at) {
    if (this.segN >= this.segOff.length) {
      const m = this.segOff.length * 2;
      const o = new Int32Array(m); o.set(this.segOff); this.segOff = o;
      const t = new Float64Array(m); t.set(this.segTick); this.segTick = t;
    }
    this.segOff[this.segN] = at; this.segTick[this.segN] = this.curTick; this.segN++;
  }

  // The clicks over out[at..at+len): every resonator, with the events
  // scheduled inside the span injected at their samples. Writes (not adds):
  // it clears the span first.
  resonate(out, at, len) {
    const end = at + len, R = this.R, rowHas = this.rowHas;
    // Clicks scheduled inside this span, onto each resonator's scratch row.
    if (this.evN) {
      const lim = this.abs + len;
      if (this.inj.length < R * len) this.inj = new Float64Array(R * Math.max(len, 128));
      for (let e = 0; e < this.evN; ) {
        if (this.evAt[e] < lim) {
          const r = this.evR[e], o = Math.max(0, this.evAt[e] - this.abs);
          this.inj[r * len + o] += this.evA[e];
          rowHas[r] = 1;
          const l = --this.evN;
          this.evAt[e] = this.evAt[l]; this.evR[e] = this.evR[l]; this.evA[e] = this.evA[l];
        } else e++;
      }
    }
    out.fill(0, at, end);
    for (let r = 0; r < R; r++) {
      let y1 = this.ry1[r], y2 = this.ry2[r];
      const has = rowHas[r];
      rowHas[r] = 0;
      if (y1 === 0 && y2 === 0 && this.imp[r] === 0 && !has && this.nAmp[r] === 0) continue;   // silent
      const c1 = this.rc1[r], c2 = this.rc2[r], rx = this.rx[r];
      const m = this.rNoise[r];
      if (m > 0 || this.nAmp[r] > 0) {
        // Advanced audio's noise exciter: a share m of each click is a
        // burst of noise into the resonator, decaying over the dial's time.
        const ns = this.rNScale[r], dec = this.rNDec[r], inj = this.inj, o = r * len;
        let na = this.nAmp[r] + this.imp[r] * m * ns, st = this.nzS[r], x = this.imp[r] * rx * (1 - m);
        this.imp[r] = 0;
        for (let s = 0; s < len; s++) {
          if (has) { const a = inj[o + s]; if (a !== 0) { x += a * rx * (1 - m); na += a * m * ns; inj[o + s] = 0; } }
          if (na > 0) {
            st = (Math.imul(st, 1664525) + 1013904223) >>> 0;
            x += na * (st / 2147483648 - 1);
            na *= dec;
            if (na < 1e-12) na = 0;
          }
          let y = c1 * y1 + c2 * y2 + x;
          x = 0;
          if (y < 1e-20 && y > -1e-20) y = 0;
          y2 = y1; y1 = y;
          out[at + s] += y;
        }
        this.nAmp[r] = na; this.nzS[r] = st;
        this.ry1[r] = y1; this.ry2[r] = y2;
        continue;
      }
      let x = this.imp[r] * rx;
      this.imp[r] = 0;
      // A click that has died away is flushed to zero per sample (no
      // denormals, and the same wherever a span ends).
      if (has) {
        const inj = this.inj, o = r * len;
        for (let s = 0; s < len; s++) {
          x += inj[o + s] * rx; inj[o + s] = 0;
          let y = c1 * y1 + c2 * y2 + x;
          x = 0;
          if (y < 1e-20 && y > -1e-20) y = 0;
          y2 = y1; y1 = y;
          out[at + s] += y;
        }
      } else {
        for (let s = at; s < end; s++) {
          let y = c1 * y1 + c2 * y2 + x;
          x = 0;
          if (y < 1e-20 && y > -1e-20) y = 0;
          y2 = y1; y1 = y;
          out[s] += y;
        }
      }
      this.ry1[r] = y1; this.ry2[r] = y2;
    }
    this.abs += len;
  }

  // `len` samples at out[at..]: the clicks, then every voice over the span.
  // `edge`: the span ends on a tick boundary (or nothing is playing), the
  // only place a finished voice is freed, so where the caller splits the
  // rendering never changes what is heard.
  span(out, at, len, edge = true) {
    const end = at + len;
    this.resonate(out, at, len);
    const T = this.table;
    let amp = this.p.beltGain / Math.pow(Math.max(1, this.n), this.p.norm);
    // Spatial: normalised by the weights' sum rather than the count, so a
    // screen's worth fills the mix -- and, since that sum moves as the
    // camera does, glided rather than stepped, as each voice's own gain is.
    const glide = this.spaced ? 1 - Math.exp(-len / (this.sr * SPACE_GLIDE_S)) : 1;
    if (this.spaced) {
      const want = this.p.beltGain / Math.pow(Math.max(1, this.bNorm), this.p.norm);
      this.ampS = this.ampS < 0 ? want : this.ampS + (want - this.ampS) * glide;
      amp = this.ampS;
    } else this.ampS = -1;
    if (this.p.truthAudio || this.lfoOn) {
      if (this.lfoOn) this.lfo(len, this.abs - len, at);
      const ampF = this.ampFalsy(glide);
      for (let v = 0; v < this.nv; v++) {
        if (this.spaced && this.vSeg[v] >= 0) this.vS[v] += (this.sGain[this.vSeg[v]] - this.vS[v]) * glide;
        const sg = this.vGain[v] * (this.spaced ? this.vS[v] : 1);
        this.truthRun(out, v, at, end, this.vOn[v], this.vFT[v], amp * sg, ampF * sg);
      }
    } else for (let v = 0; v < this.nv; v++) {
      let env = this.vEnv[v], ph = this.vPhase[v];
      if (this.spaced && this.vSeg[v] >= 0) this.vS[v] += (this.sGain[this.vSeg[v]] - this.vS[v]) * glide;
      const inc = this.vInc[v], tgt = this.vOn[v], coef = tgt ? this.vAtk[v] : this.vRel[v];
      const g = amp * this.vGain[v] * (this.spaced ? this.vS[v] : 1), a = this.vLpA[v];
      if (a >= 1) {
        for (let s = at; s < end; s++) {
          env += (tgt - env) * coef;
          const fi = ph * TABLE, j = fi | 0;
          out[s] += g * env * (T[j] + (T[j + 1] - T[j]) * (fi - j));
          ph += inc;
          if (ph >= 1) ph -= 1;
        }
      } else {
        // The distance low-pass: one pole on the oscillator, so a far run
        // has lost its upper harmonics.
        let y = this.vLpY[v];
        for (let s = at; s < end; s++) {
          env += (tgt - env) * coef;
          const fi = ph * TABLE, j = fi | 0;
          y += a * ((T[j] + (T[j + 1] - T[j]) * (fi - j)) - y);
          out[s] += g * env * y;
          ph += inc;
          if (ph >= 1) ph -= 1;
        }
        this.vLpY[v] = y;
      }
      this.vEnv[v] = env; this.vPhase[v] = ph;
    }
    // Free finished voices after the span, from the top: a freed slot takes
    // the last voice (already checked), and a refused run given the slot
    // starts sounding on the next span -- not retroactively over this one.
    if (edge) {
      for (let v = this.nv - 1; v >= 0; v--) if (!this.vOn[v] && this.vEnv[v] < 1e-4) this.free(v);
    }
    // Silent before startTick, then the fade in.
    const p = this.p;
    if (this.curTick < p.startTick) {
      this.fade = 0;
      out.fill(0, at, end);
    } else if (this.fade < 1) {
      const inc = p.fadeMs > 0 ? 1000 / (p.fadeMs * this.sr) : 1;
      let f = this.fade;
      for (let s = at; s < end; s++) { f = Math.min(1, f + inc); out[s] *= f; }
      this.fade = f;
    }
  }

  // What the panel shows; resets the peak and clip count.
  meter() {
    const rate = this.queue.length ? this.queue[0].rate : this.rate;
    const m = {
      type: "meter", voices: this.nv, limit: Math.min(this.p.voiceLimit, VOICE_LIMIT_MAX),
      active: this.active, refused: this.refused, candidates: this.n,
      peak: this.peak, clipped: this.clipped, ticks: this.ticksPlayed,
      lagMs: this.queued / Math.max(rate, 1e-3) * 1000, speed: this.playing ? this.speed : 0,
      underruns: this.underruns, skips: this.skips,
    };
    this.peak = 0; this.clipped = 0; this.ticksPlayed = 0;
    return m;
  }
}
