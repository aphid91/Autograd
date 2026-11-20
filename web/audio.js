// Audio, main-thread half: records what each tick did and ships it to the
// worklet once a frame. Imported only when the player switches audio on,
// so with it off nothing here is loaded and the sim's only cost is a null
// check per tick (Sim.audio).
//
// Per tick it records, into flat typed arrays:
//   - for each candidate run (a run no longer than the cutoff), one bit:
//     active this tick -- it pushed or popped, or its newest item moved (the
//     belt status colours' "moving", with "stalled but moving: green");
//   - how many gates of each group fired (Sim.structurePhase counts into
//     `fires`; Sources go to the silent group 2).
// paint() calls flush() after stepping, which posts the frame's ticks with
// their buffers transferred, and stop() while paused.

import { audioGroupOf } from "./audio-params.js";

export class Recorder {
  constructor(port, params) {
    this.port = port;
    this.cutoff = params.beltCutoff;
    this.sim = null;
    this.rebuilds = -1;
    this.cands = [];
    this.h = new Int32Array(0); this.t = new Int32Array(0); this.pos = new Float64Array(0);
    this.words = 0;
    this.fires = new Int32Array(3);
    this.n = 0;
    this.cap = 0;
    this.bits = new Uint32Array(0); this.f0 = new Uint16Array(0); this.f1 = new Uint16Array(0);
    this.stopped = true;
  }

  attach(sim) {
    if (this.sim && this.sim !== sim) this.sim.audio = null;
    this.sim = sim;
    this.rebuilds = -1;              // reconfigure on its first tick
    if (sim) sim.audio = this;
  }
  detach() { if (this.sim) this.sim.audio = null; this.sim = null; }

  setCutoff(c) { if (c !== this.cutoff) { this.cutoff = c; this.rebuilds = -1; } }

  // Which runs may sound, and each gate's click group. Flushes first: the
  // ticks so far belong to the old candidates.
  configure(sim) {
    this.flush(this.lastRate || 1);
    this.rebuilds = sim.stats.rebuilds;
    this.cands = sim.order.filter(seg => seg.length <= this.cutoff);
    const n = this.cands.length;
    this.h = new Int32Array(n); this.t = new Int32Array(n); this.pos = new Float64Array(n);
    this.words = (n + 31) >>> 5;
    this.cap = 0;
    const groups = [0, 0];
    for (const g of sim.gates) {
      g.agroup = audioGroupOf(g.td.class);
      if (g.agroup < 2) groups[g.agroup]++;
    }
    this.port.postMessage({ type: "config", lengths: Float64Array.from(this.cands, s => s.length), groups });
  }

  // Called by Sim.step() after its sync, before the structure phase.
  pre(sim) {
    if (sim.stats.rebuilds !== this.rebuilds) this.configure(sim);
    if (!this.n) this.tick0 = sim.tick;
    const c = this.cands, h = this.h, t = this.t, pos = this.pos;
    for (let i = 0; i < c.length; i++) {
      const seg = c[i];
      h[i] = seg.head; t[i] = seg.tail;
      pos[i] = seg.tail > seg.head ? sim.positionOf(seg, seg.tail - 1, sim.tick) : -1;
    }
    this.fires[0] = this.fires[1] = 0;
  }

  // Called by Sim.step() once the tick counter has moved.
  post(sim) {
    const W = this.words;
    if (this.n >= this.cap) this.grow();
    const k = this.n++, off = k * W, bits = this.bits;
    for (let w = 0; w < W; w++) bits[off + w] = 0;
    const c = this.cands, h = this.h, t = this.t, pos = this.pos;
    for (let i = 0; i < c.length; i++) {
      const seg = c[i];
      let on = seg.head !== h[i] || seg.tail !== t[i];
      if (!on && t[i] > h[i]) on = sim.positionOf(seg, t[i] - 1, sim.tick) > pos[i];
      if (on) bits[off + (i >>> 5)] |= 1 << (i & 31);
    }
    this.f0[k] = Math.min(65535, this.fires[0]);
    this.f1[k] = Math.min(65535, this.fires[1]);
  }

  grow() {
    const cap = Math.max(256, this.cap * 2), W = this.words;
    const bits = new Uint32Array(cap * W), f0 = new Uint16Array(cap), f1 = new Uint16Array(cap);
    bits.set(this.bits.subarray(0, this.n * W)); f0.set(this.f0.subarray(0, this.n)); f1.set(this.f1.subarray(0, this.n));
    this.bits = bits; this.f0 = f0; this.f1 = f1; this.cap = cap;
  }

  // Post the ticks recorded since the last flush, played at `rate`.
  flush(rate) {
    this.lastRate = rate;
    if (!this.n) return;
    this.stopped = false;
    const n = this.n, W = this.words;
    const m = { type: "ticks", n, tick0: this.tick0, words: W, rate,
                bits: this.bits.slice(0, n * W), f0: this.f0.slice(0, n), f1: this.f1.slice(0, n) };
    this.port.postMessage(m, [m.bits.buffer, m.f0.buffer, m.f1.buffer]);
    this.n = 0;
  }

  // Paused: drop what is recorded and queued, and let every voice release.
  stop() {
    this.n = 0;
    if (this.stopped) return;
    this.stopped = true;
    this.port.postMessage({ type: "stop" });
  }
}

// Start audio on an AudioContext the caller made inside the user's gesture
// (iOS will not start one created after an await). Returns the live engine.
export async function startAudio(ctx, params, sim, onMeter) {
  try { if (navigator.audioSession) navigator.audioSession.type = "playback"; } catch { /* older Safari */ }
  await ctx.audioWorklet.addModule(new URL("./audio-worklet.js", import.meta.url));
  const node = new AudioWorkletNode(ctx, "gates-audio", { numberOfInputs: 0, outputChannelCount: [2] });
  node.connect(ctx.destination);
  node.port.postMessage({ type: "params", p: params });
  node.port.onmessage = (e) => onMeter?.(e.data);
  const rec = new Recorder(node.port, params);
  rec.attach(sim);
  return {
    ctx, node, rec,
    setParams(p) { node.port.postMessage({ type: "params", p }); rec.setCutoff(p.beltCutoff); },
    // A bake's samples, as an AudioBuffer for this context.
    buffer(samples, sampleRate) {
      const b = ctx.createBuffer(1, samples.length, sampleRate);
      b.copyToChannel(samples, 0);
      return b;
    },
    // Play `buf` from `offset` seconds into it, starting at context time
    // `when`; any earlier one stops. Straight to the destination: no queue,
    // so the only latency is the output's own.
    playBaked(buf, when, offset) {
      this.stopBaked();
      const src = ctx.createBufferSource();
      src.buffer = buf;
      src.connect(ctx.destination);
      src.start(Math.max(when, ctx.currentTime), Math.max(0, offset));
      this.src = src;
    },
    stopBaked() {
      if (!this.src) return;
      try { this.src.stop(); } catch { /* already ended */ }
      this.src.disconnect();
      this.src = null;
    },
    async close() { this.stopBaked(); rec.detach(); node.port.onmessage = null; node.disconnect(); await ctx.close(); },
  };
}
