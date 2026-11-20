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

const TABLE = 2048;
const MAX_QUEUE_S = 1;      // a stalled audio thread never builds more lag than this
const SPEED_TAU_S = 0.3;    // how quickly playback speed follows the queue
const MAX_SPEED = 1.25;     // catching up on a small backlog: barely audible
const SKIP_AT = 3;          // a backlog this many latencies deep is skipped, not raced through
const HOLD_MAX_S = 0.5;     // holding through an underrun gives up after this long

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
    // Click resonators, one per group: y = c1*y1 + c2*y2 + x.
    this.ry1 = [0, 0]; this.ry2 = [0, 0]; this.rc1 = [0, 0]; this.rc2 = [0, 0]; this.rx = [0, 0];
    this.imp = [0, 0];                   // impulse due on the next sample
    // Meter, since the last read.
    this.peak = 0; this.clipped = 0; this.ticksPlayed = 0;
    this.applyParams();
  }

  message(m) {
    if (m.type === "ticks") this.push(m);
    else if (m.type === "config") this.configure(m.lengths, m.groups);
    else if (m.type === "params") { Object.assign(this.p, m.p); this.applyParams(); }
    else if (m.type === "stop") this.stop();
  }

  applyParams() {
    const p = this.p, sr = this.sr;
    if (p.beltShape !== this.shape) this.buildTable(p.beltShape);
    this.atk = 1 - Math.exp(-1 / Math.max(1, sr * p.attackMs / 1000));
    this.rel = 1 - Math.exp(-1 / Math.max(1, sr * p.releaseMs / 1000));
    for (let g = 0; g < 2; g++) {
      const tone = Math.min(p[`g${g}Tone`], sr * 0.45), decay = p[`g${g}DecayMs`];
      const r = Math.exp(-1 / Math.max(1, sr * decay / 1000)), w = 2 * Math.PI * tone / sr;
      this.rc1[g] = 2 * r * Math.cos(w); this.rc2[g] = -r * r;
      this.rx[g] = Math.sin(w);          // impulse scale: the response peaks near 1
    }
    this.trackRate = -1;                 // re-pitch every voice on the next tick
  }

  // One cycle, blended sine -> triangle -> saw, with a guard sample.
  buildTable(s) {
    this.shape = s;
    for (let i = 0; i <= TABLE; i++) {
      const x = (i % TABLE) / TABLE;
      const sine = Math.sin(2 * Math.PI * x);
      const tri = x < 0.25 ? 4 * x : x < 0.75 ? 2 - 4 * x : 4 * x - 4;
      const saw = x < 0.5 ? 2 * x : 2 * x - 2;
      this.table[i] = s < 0.5 ? sine + (tri - sine) * s * 2 : tri + (saw - tri) * (s - 0.5) * 2;
    }
  }

  configure(lengths, groups) {
    // Old voices keep sounding with their pitch and release out; they just
    // no longer belong to a candidate.
    for (let v = 0; v < this.nv; v++) { this.vSeg[v] = -1; this.vOn[v] = 0; }
    this.lens = Float64Array.from(lengths);
    this.n = this.lens.length;
    this.words = (this.n + 31) >>> 5;
    this.voiceOf = new Int32Array(this.n).fill(-1);
    this.prev = new Uint32Array(this.words);
    this.groups = [groups[0] | 0, groups[1] | 0];
    this.active = 0; this.refused = 0;
    // Ticks already queued were recorded against the old candidates.
    this.queue = []; this.qi = 0; this.queued = 0;
  }

  push(b) {
    if (b.words !== this.words || !b.n) return;   // from before the last config
    this.queue.push(b);
    this.queued += b.n;
    if (this.offline) return;
    // Never build more than MAX_QUEUE_S of lag: drop the oldest ticks.
    while (this.queue.length > 1 && this.queued / Math.max(b.rate, 1e-3) > MAX_QUEUE_S) {
      const old = this.queue.shift();
      this.queued -= old.n - this.qi;
      this.qi = 0;
    }
  }

  // Drop `seconds` of the oldest queued ticks unplayed. Their run changes
  // are not lost: the next tick applied is compared against the last one
  // applied. Their clicks are.
  skip(seconds) {
    let drop = Math.floor(seconds * Math.max(this.queue[0].rate, 1e-3));
    while (drop > 0 && this.queue.length) {
      const b = this.queue[0], left = b.n - this.qi, d = Math.min(drop, left);
      if (d === left && this.queue.length === 1) break;   // keep the newest tick
      this.qi += d; this.queued -= d; drop -= d;
      if (this.qi >= b.n) { this.queue.shift(); this.qi = 0; }
    }
  }

  stop() {
    this.queue = []; this.qi = 0; this.queued = 0;
    this.gatesOff();
  }

  // Stop playback and release every voice; the next tick played gates
  // whatever it says afresh.
  gatesOff() {
    this.playing = false;
    this.held = false; this.heldS = 0;
    this.tickPos = 0;
    for (let v = 0; v < this.nv; v++) this.vOn[v] = 0;
    this.prev.fill(0);
    this.active = 0; this.refused = 0;
  }

  // --- voices -------------------------------------------------------------------

  pitch(i) {
    const p = this.p;
    const track = p.beltTrack > 0 ? Math.pow(this.rate / Math.max(p.trackRef, 1e-3), p.beltTrack) : 1;
    return Math.min(p.beltMaxHz / this.lens[i] * track, this.sr * 0.45);
  }

  on(i) {
    const v = this.voiceOf[i];
    if (v >= 0) { this.vOn[v] = 1; return; }
    if (this.nv >= Math.min(this.p.voiceLimit, VOICE_LIMIT_MAX)) { this.refused++; return; }
    this.alloc(i);
  }

  alloc(i) {
    const v = this.nv++;
    this.vSeg[v] = i; this.vOn[v] = 1; this.vEnv[v] = 0; this.vPhase[v] = 0;
    this.vInc[v] = this.pitch(i) / this.sr;
    this.voiceOf[i] = v;
  }

  off(i) {
    const v = this.voiceOf[i];
    if (v >= 0) this.vOn[v] = 0; else this.refused--;
  }

  free(v) {
    const last = --this.nv;
    if (this.vSeg[v] >= 0) this.voiceOf[this.vSeg[v]] = -1;
    if (v !== last) {
      this.vSeg[v] = this.vSeg[last]; this.vOn[v] = this.vOn[last]; this.vEnv[v] = this.vEnv[last];
      this.vPhase[v] = this.vPhase[last]; this.vInc[v] = this.vInc[last];
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
          if (this.voiceOf[i] < 0) { this.refused--; this.alloc(i); return; }
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
    this.imp[0] += b.f0[k] * p.g0Gain / Math.pow(Math.max(1, this.groups[0]), norm);
    this.imp[1] += b.f1[k] * p.g1Gain / Math.pow(Math.max(1, this.groups[1]), norm);
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

  // --- rendering --------------------------------------------------------------------

  // Fill out[0..n) with mono samples.
  render(out, n) {
    const sr = this.sr, p = this.p;
    // Start once the queue holds the latency's worth; follow the queue's
    // depth with the playback speed.
    const target = p.latencyMs / 1000;
    const queuedS = this.queued / Math.max(this.queue.length ? this.queue[0].rate : this.rate, 1e-3);
    // A tick takes effect at its start: tickPos is how far through the one
    // already applied playback is.
    if (this.held) {
      // Ticks again: carry straight on. Too long without: let go.
      if (this.queued) { this.held = false; this.heldS = 0; this.tickPos = 0; if (this.nextTick()) this.played++; }
      else if ((this.heldS += n / sr) > HOLD_MAX_S && !this.offline) this.gatesOff();
    }
    if (!this.playing && this.queued && (this.offline || queuedS >= target)) {
      this.playing = true; this.speed = 1; this.tickPos = 0;
      this.played = this.nextTick() ? 1 : 0; this.sampleNo = 0;
    }
    if (this.offline) this.speed = 1;
    else {
      if (this.playing && queuedS > SKIP_AT * target) this.skip(queuedS - target);
      if (this.playing) {
        const want = Math.min(MAX_SPEED, Math.max(0.25, queuedS / target));
        this.speed += (want - this.speed) * Math.min(1, n / sr / SPEED_TAU_S);
      }
    }
    let i = 0;
    while (i < n) {
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
      const moving = this.playing && !this.held;
      if (moving) span = Math.min(span, Math.max(1, Math.ceil((1 - this.tickPos) / tps)));
      this.span(out, i, span, moving && this.tickPos + span * tps >= 1 || !moving);
      i += span;
      if (moving) {
        this.tickPos += span * tps;
        while (this.tickPos >= 1) {
          this.tickPos -= 1;
          if (!this.nextTick()) {
            if (this.offline || this.p.holdUnderrun) { this.held = true; this.heldS = 0; this.tickPos = 0; }
            else this.gatesOff();
            break;
          }
        }
      }
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

  // `len` samples at out[at..]: the clicks, then every voice over the span.
  // `edge`: the span ends on a tick boundary (or nothing is playing), the
  // only place a finished voice is freed, so where the caller splits the
  // rendering never changes what is heard.
  span(out, at, len, edge = true) {
    const end = at + len;
    for (let g = 0; g < 2; g++) {
      let y1 = this.ry1[g], y2 = this.ry2[g];
      const c1 = this.rc1[g], c2 = this.rc2[g];
      let x = this.imp[g] * this.rx[g];
      this.imp[g] = 0;
      // A click that has died away is flushed to zero per sample (no
      // denormals, and the same wherever a span ends).
      for (let s = at; s < end; s++) {
        let y = c1 * y1 + c2 * y2 + x;
        x = 0;
        if (y < 1e-20 && y > -1e-20) y = 0;
        y2 = y1; y1 = y;
        if (g === 0) out[s] = y; else out[s] += y;
      }
      this.ry1[g] = y1; this.ry2[g] = y2;
    }
    const T = this.table, amp = this.p.beltGain / Math.pow(Math.max(1, this.n), this.p.norm);
    const atk = this.atk, rel = this.rel;
    for (let v = 0; v < this.nv; v++) {
      let env = this.vEnv[v], ph = this.vPhase[v];
      const inc = this.vInc[v], tgt = this.vOn[v], coef = tgt ? atk : rel;
      for (let s = at; s < end; s++) {
        env += (tgt - env) * coef;
        const fi = ph * TABLE, j = fi | 0;
        out[s] += amp * env * (T[j] + (T[j + 1] - T[j]) * (fi - j));
        ph += inc;
        if (ph >= 1) ph -= 1;
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
    };
    this.peak = 0; this.clipped = 0; this.ticksPlayed = 0;
    return m;
  }
}
