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

import { audioGroupOf, eligibleRuns } from "./audio-params.js";
import { spaceWeights, spaceKey } from "./space.js";

export class Recorder {
  // `selection`: a Set of cell indices (y * width + x) -- only runs touching
  // one and gates covering one sound -- or null for the whole board.
  // `space`: { rect, fringe, lp } (space.js) -- each candidate weighted by
  // its distance from the box -- or null. `spaceAt(tick)`, if given, is
  // asked each tick instead (a video's soundtrack, whose camera moves).
  constructor(port, params, selection = null, space = null, spaceAt = null) {
    this.port = port;
    this.selection = selection;
    this.space = space;
    this.spaceAt = spaceAt;
    this.spaceKey = spaceKey(space);
    this.gateList = [];
    this.selInfo = null;
    this.minLen = params.beltMinLen;
    this.cutoff = params.beltCutoff;
    this.sim = null;
    this.rebuilds = -1;
    this.cands = [];
    this.words = 0;
    this.fires = new Float64Array(3);
    this.n = 0;
    this.cap = 0;
    // Fire "counts" are sums of the firing gates' spatial gains (1 each
    // without a space), so a far gate clicks quieter.
    this.bits = new Uint32Array(0); this.f0 = new Float32Array(0); this.f1 = new Float32Array(0);
    this.stopped = true;
  }

  attach(sim) {
    if (this.sim && this.sim !== sim) this.sim.audio = null;
    this.sim = sim;
    this.rebuilds = -1;              // reconfigure on its first tick
    if (sim) sim.audio = this;
  }
  detach() { if (this.sim) this.sim.audio = null; this.sim = null; }

  // A new sound selection takes effect now (not at the next tick), so the
  // meter's counts are right even while paused.
  setSelection(cells) {
    if (cells === this.selection) return;
    this.selection = cells;
    this.rebuilds = -1;
    // Synced first: the board may have been edited since the last tick (a
    // lift, say), and the gate table must describe the board as it is.
    if (this.sim) { this.sim.sync(); this.configure(this.sim); }
  }

  // A new space (the camera moved, the region or a dial changed): the
  // candidates stay as they are -- a reconfigure would orphan every voice
  // -- and only their weights are sent. Ticks already recorded go first,
  // under the old weights.
  setSpace(space) {
    const key = spaceKey(space);
    if (key === this.spaceKey) return;
    this.space = space;
    this.spaceKey = key;
    if (this.sim && this.rebuilds === this.sim.stats.rebuilds) { this.flush(this.lastRate || 1); this.sendSpace(this.sim); }
  }

  // The weights for the current candidates and gates, to the synth.
  sendSpace(sim) {
    this.gainGen = (this.gainGen || 0) + 1;     // the gates' gains, for a worker's tap
    const w = spaceWeights(sim, this.cands, this.gateList, this.space);
    const groups = [0, 0];
    this.gateList.forEach((g, i) => { g.again = w.gateGain[i]; groups[g.agroup] += g.again; });
    if (this.space) {
      let runs = 0;
      for (const g of w.gain) if (g > 0) runs++;
      let gates = 0;
      for (const g of w.gateGain) if (g > 0) gates++;
      this.selInfo = { runs, gates };
    }
    this.port.postMessage({ type: "space", on: !!this.space, gain: w.gain, lp: w.lp, norm: w.runSum, groups });
  }

  // Which run lengths may sound; a change reconfigures on the next tick.
  setLengths(min, max) {
    if (min !== this.minLen || max !== this.cutoff) { this.minLen = min; this.cutoff = max; this.rebuilds = -1; }
  }

  // Which runs may sound, and each gate's click group. Flushes first: the
  // ticks so far belong to the old candidates.
  configure(sim) {
    this.flush(this.lastRate || 1);
    this.rebuilds = sim.stats.rebuilds;
    // Which bit each run sets changes here: ticks recorded under an older
    // configuration (a worker's, simhost.js) no longer fit and are dropped.
    this.cfgGen = (this.cfgGen || 0) + 1;
    this.gainGen = (this.gainGen || 0) + 1;
    const sel = this.selection, W = sim.board.width;
    const picked = (cells) => !sel || cells.some(([x, y]) => sel.has(y * W + x));
    // A run sounds if any of its cells is selected, underground ends included.
    this.cands = eligibleRuns(sim, this.minLen, this.cutoff, sel);
    const n = this.cands.length;
    // Each run's bit, for hit(): -1 for a run that does not sound.
    for (const seg of sim.order) seg.aIdx = -1;
    this.cands.forEach((seg, i) => { seg.aIdx = i; });
    // The sim's wake list hears sleeping runs only from when it is told about
    // them (Sim.tickAwake()), so a new recorder starts with every run awake.
    sim.wakeAll?.();
    this.words = (n + 31) >>> 5;
    this.cap = 0;
    const groups = [0, 0];
    this.gateList = [];
    for (const g of sim.gates) {
      g.agroup = audioGroupOf(g.td.class);
      g.again = 1;
      // Outside the sound selection a gate clicks in the silent group, as a
      // Source does: no cost per tick.
      if (sel && g.agroup < 2) {
        const [ox, oy] = sim.originOf(g.row), o = sim.orientOf(g.row), cells = [];
        for (let x = 0; x < o.w; x++) for (let y = 0; y < o.h; y++) cells.push([ox + x, oy + y]);
        if (!picked(cells)) g.agroup = 2;
      }
      if (g.agroup < 2) { groups[g.agroup]++; this.gateList.push(g); }
    }
    // Normalisation counts the selection only, so isolating part of a machine
    // fills the mix rather than playing it as quietly as it sits in the whole.
    this.selInfo = sel ? { runs: this.cands.length, gates: groups[0] + groups[1] } : null;
    this.port.postMessage({ type: "config", gen: this.cfgGen, lengths: Float64Array.from(this.cands, s => s.length), groups });
    // With a space the synth weighs every candidate (and normalises by the
    // weights, not the count); without one a config alone is the old state.
    if (this.space) this.sendSpace(sim);
  }

  // Called by Sim.step() after its sync, before the structure phase: this
  // tick's row of run bits, cleared. The sim then calls hit() for each run
  // active on the tick -- it pushed or popped, or its newest item moved --
  // since only the sim knows which runs it visited (Sim.tickHeard(), and its
  // wake list's sleeping runs still sliding into place, Sim.hearMovers()).
  // The recorder used to compare every candidate's newest item before and
  // after the step itself, two positionOf() calls per run per tick: up to
  // half the tick on a big board.
  pre(sim) {
    if (sim.stats.rebuilds !== this.rebuilds) this.configure(sim);
    if (this.spaceAt) this.setSpace(this.spaceAt(sim.tick));
    if (!this.n) this.tick0 = sim.tick;
    if (this.n >= this.cap) this.grow();
    const W = this.words, off = this.n * W, bits = this.bits;
    for (let w = 0; w < W; w++) bits[off + w] = 0;
    this.off = off;
    this.fires[0] = this.fires[1] = 0;
  }

  hit(seg) {
    const i = seg.aIdx;
    if (i >= 0) this.bits[this.off + (i >>> 5)] |= 1 << (i & 31);
  }

  // --- with the simulation on a worker (simhost.js) ---------------------------

  // Reconfigure after a rebuild, as pre() would, without a tick of our own.
  ensure(sim) {
    if (sim.stats.rebuilds !== this.rebuilds) this.configure(sim);
  }

  // What the worker's tap needs to fill the same bits and click counts: each
  // run's bit (by its index in `order`) and each gate's group and gain (in
  // `sim.gates` order), under this configuration's generation.
  tapConfig(sim) {
    return {
      gen: this.cfgGen, words: this.words, runs: sim.order.length, gates: sim.gates.length,
      aIdx: Int32Array.from(sim.order, s => s.aIdx ?? -1),
      agroup: Int8Array.from(sim.gates, g => g.agroup ?? 2),
      again: Float32Array.from(sim.gates, g => g.again ?? 1),
    };
  }

  // A worker's recorded ticks, appended as if recorded here. A batch from an
  // older configuration is dropped: its bits mean other runs.
  ingest(a) {
    if (!a || a.gen !== this.cfgGen || a.words !== this.words) return;
    const W = this.words;
    if (!this.n) this.tick0 = a.tick0;
    while (this.n + a.n > this.cap) this.grow();
    this.bits.set(a.bits.subarray(0, a.n * W), this.n * W);
    this.f0.set(a.f0.subarray(0, a.n), this.n);
    this.f1.set(a.f1.subarray(0, a.n), this.n);
    this.n += a.n;
  }

  // Called by Sim.step() once the tick counter has moved.
  post(sim) {
    const k = this.n++;
    this.f0[k] = this.fires[0];
    this.f1[k] = this.fires[1];
  }

  grow() {
    const cap = Math.max(256, this.cap * 2), W = this.words;
    const bits = new Uint32Array(cap * W), f0 = new Float32Array(cap), f1 = new Float32Array(cap);
    bits.set(this.bits.subarray(0, this.n * W)); f0.set(this.f0.subarray(0, this.n)); f1.set(this.f1.subarray(0, this.n));
    this.bits = bits; this.f0 = f0; this.f1 = f1; this.cap = cap;
  }

  // Post the ticks recorded since the last flush, played at `rate`.
  flush(rate) {
    this.lastRate = rate;
    if (!this.n) return;
    this.stopped = false;
    const n = this.n, W = this.words;
    const m = { type: "ticks", gen: this.cfgGen, n, tick0: this.tick0, words: W, rate,
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
    setParams(p) { node.port.postMessage({ type: "params", p }); rec.setLengths(p.beltMinLen, p.beltCutoff); },
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
    // A second port into the synth, for the sim worker to send its ticks
    // straight down (simhost.js), with no main-thread frame in between.
    tickPort() {
      const ch = new MessageChannel();
      node.port.postMessage({ type: "tickport", port: ch.port1 }, [ch.port1]);
      return ch.port2;
    },
    async close() { this.stopBaked(); rec.detach(); node.port.onmessage = null; node.disconnect(); await ctx.close(); },
  };
}
