// The simulation on its own thread (simworker.js), and the main thread's
// copy of it.
//
// The worker owns ticking: a Sim of its own, run on its own clock at the
// chosen rate, so the tick rate is no longer capped by how long a frame
// takes to draw. The main thread keeps its Sim as an exact MIRROR, and once
// a frame asks the worker what changed since it last asked (a delta), so
// everything that reads the simulation there -- the renderer, the forecast,
// the status colours, Status, bakes, video -- reads the mirror and works
// unchanged.
//
// A delta is small because a tick changes little (see the wake list in
// sim.js): the runs ticked (head, tail, anchor and the two activity stamps),
// the ring slots pushed into, the gate rows whose channels were written
// (their 16 channel slots and their fire stamp), the Pixel fires in order,
// and the audio recorder's bits. `Tracker` collects them as the worker ticks
// -- the Sim calls it from the same places the wake list hears of a change.
//
// Edits stay on the main thread, which owns the Board. After the mirror
// rebuilds (Sim.onRebuild), the board's arrays and the run-id allocator's
// state from just BEFORE that rebuild go to the worker, which rebuilds the
// same way and so numbers every run the same: deltas are keyed by a run's
// index in `order` and a gate's row. If the main thread changes the
// simulation's state itself -- a bake's restore, stepping while a baked
// soundtrack plays -- it sends the whole dynamic state back before the
// worker runs again (`exportFull` / `applyFull`).
//
// Every board or state message bumps an EPOCH, and a delta carries the epoch
// of the state it grew from; the main thread drops any other. So a delta in
// flight when an edit lands, or when the main thread takes over, can never
// be applied to the wrong state.
//
// Pure: no DOM, no Worker. tests/worker.test.mjs drives both ends in one
// process and holds the mirror to a reference timeline, tick for tick.

import { Sim, Clock } from "./sim.js";
import { Board } from "./board.js";

// The gate block of an entity row: input values, input held, output values,
// output held -- MAX_CHANNELS slots each, from F_IN_VALUE.
const gateSlots = (T) => 4 * T.sim.MAX_CHANNELS;

// What changed since the last delta, gathered by the worker's Sim (`track`).
export class Tracker {
  constructor(rows, runs) {
    this.rowFlag = new Uint8Array(rows); this.rows = [];
    this.runFlag = new Uint8Array(runs); this.runs = [];
    this.pushes = [];                // (run index, slot) pairs, in order
    this.pixels = [];                // row, then its four input values, per fire, in order
  }
  row(r) { if (!this.rowFlag[r]) { this.rowFlag[r] = 1; this.rows.push(r); } }
  run(oi) { if (!this.runFlag[oi]) { this.runFlag[oi] = 1; this.runs.push(oi); } }
  push(oi, slot) { this.pushes.push(oi, slot); }
  clear() {
    for (const r of this.rows) this.rowFlag[r] = 0;
    for (const i of this.runs) this.runFlag[i] = 0;
    this.rows.length = 0; this.runs.length = 0; this.pushes.length = 0; this.pixels.length = 0;
  }
}

// The worker's stand-in for the Pixel screen: it only records each fire, in
// order, for the mirror's real Screen to replay.
class PixelLog {
  constructor(tr) { this.tr = tr; }
  fire(row, values) { this.tr.pixels.push(row, values[0], values[1], values[2], values[3]); }
  clear() {}
}

// The worker's stand-in for the audio recorder (audio.js Recorder), with the
// same pre / hit / post the Sim calls, filling the same bit rows the main
// thread's Recorder would. Which bit a run sets, and each gate's click group
// and gain, come from the main thread's Recorder (`AudioTap.config`), which
// still decides what sounds.
class AudioTap {
  constructor(cfg) {
    this.gen = cfg.gen;
    this.words = cfg.words;
    this.fires = new Float64Array(3);
    this.n = 0; this.cap = 0; this.tick0 = 0; this.off = 0;
    this.bits = new Uint32Array(0); this.f0 = new Float32Array(0); this.f1 = new Float32Array(0);
  }
  // Each run's bit and each gate's group and gain, onto the worker's Sim
  // (whose runs and gates are numbered as the mirror's).
  static config(sim, cfg) {
    sim.order.forEach((seg, i) => { seg.aIdx = i < cfg.aIdx.length ? cfg.aIdx[i] : -1; });
    sim.gates.forEach((g, i) => {
      g.agroup = i < cfg.agroup.length ? cfg.agroup[i] : 2;
      g.again = i < cfg.again.length ? cfg.again[i] : 1;
    });
  }
  pre(sim) {
    if (!this.n) this.tick0 = sim.tick;
    if (this.n >= this.cap) {
      const cap = Math.max(256, this.cap * 2), W = this.words;
      const bits = new Uint32Array(cap * W), f0 = new Float32Array(cap), f1 = new Float32Array(cap);
      bits.set(this.bits.subarray(0, this.n * W)); f0.set(this.f0.subarray(0, this.n)); f1.set(this.f1.subarray(0, this.n));
      this.bits = bits; this.f0 = f0; this.f1 = f1; this.cap = cap;
    }
    this.off = this.n * this.words;
    this.bits.fill(0, this.off, this.off + this.words);
    this.fires[0] = this.fires[1] = 0;
  }
  hit(seg) {
    const i = seg.aIdx;
    if (i >= 0) this.bits[this.off + (i >>> 5)] |= 1 << (i & 31);
  }
  post() {
    this.f0[this.n] = this.fires[0];
    this.f1[this.n] = this.fires[1];
    this.n++;
  }
  take() {
    if (!this.n) return null;
    const n = this.n, W = this.words;
    const out = { gen: this.gen, words: W, n, tick0: this.tick0,
                  bits: this.bits.slice(0, n * W), f0: this.f0.slice(0, n), f1: this.f1.slice(0, n) };
    this.n = 0;
    return out;
  }
}

// --- the dynamic state, as messages -------------------------------------------

const RUN_FIELDS = 5;   // head, tail, anchor, lastPush, lastPop

// Everything a tick can change, but not the stats' rebuild count: each side
// counts its own rebuilds, and the audio recorder reconfigures on it.
function statsOf(sim) {
  const { rebuilds, ...rest } = sim.stats;
  return rest;
}

// The worker's changes since the last delta; clears the tracker.
export function takeDelta(sim, tr, extra = {}) {
  const T = sim.T, G = gateSlots(T), ST = sim.ST, base = sim.F_IN_VALUE, E = sim.board.entities;
  const runs = Int32Array.from(tr.runs), runVals = new Float64Array(runs.length * RUN_FIELDS);
  runs.forEach((oi, j) => {
    const s = sim.order[oi], o = j * RUN_FIELDS;
    runVals[o] = s.head; runVals[o + 1] = s.tail; runVals[o + 2] = s.anchor;
    runVals[o + 3] = s.lastPush; runVals[o + 4] = s.lastPop;
  });
  const pushes = Int32Array.from(tr.pushes), n = pushes.length / 2;
  const enter = new Float64Array(n), payload = new Float32Array(n);
  for (let j = 0; j < n; j++) {
    const s = sim.order[pushes[2 * j]], slot = pushes[2 * j + 1];
    enter[j] = s.enter[slot]; payload[j] = s.payload[slot];
  }
  const rows = Int32Array.from(tr.rows), chans = new Float32Array(rows.length * G), fired = new Int32Array(rows.length);
  rows.forEach((r, j) => {
    chans.set(E.subarray(r * ST + base, r * ST + base + G), j * G);
    fired[j] = sim.fired[r];
  });
  const pixels = Float64Array.from(tr.pixels);
  tr.clear();
  const d = { type: "delta", tick: sim.tick, stats: statsOf(sim), runs, runVals, pushes, enter, payload,
              rows, chans, fired, pixels, ...extra };
  d.transfer = [runs.buffer, runVals.buffer, pushes.buffer, enter.buffer, payload.buffer,
                rows.buffer, chans.buffer, fired.buffer, pixels.buffer];
  return d;
}

// A delta onto the mirror. Its wake list is stale after (the mirror did not
// tick these), so it is dropped, and rebuilt -- everything awake -- if the
// mirror is ever stepped itself (Sim.step()).
export function applyDelta(sim, d, screen = null) {
  const T = sim.T, G = gateSlots(T), ST = sim.ST, base = sim.F_IN_VALUE, E = sim.board.entities;
  for (let j = 0; j < d.runs.length; j++) {
    const s = sim.order[d.runs[j]], o = j * RUN_FIELDS;
    if (!s) continue;
    s.head = d.runVals[o]; s.tail = d.runVals[o + 1]; s.anchor = d.runVals[o + 2];
    s.lastPush = d.runVals[o + 3]; s.lastPop = d.runVals[o + 4];
  }
  for (let j = 0; j < d.enter.length; j++) {
    const s = sim.order[d.pushes[2 * j]], slot = d.pushes[2 * j + 1];
    if (!s) continue;
    s.enter[slot] = d.enter[j]; s.payload[slot] = d.payload[j];
  }
  for (let j = 0; j < d.rows.length; j++) {
    const r = d.rows[j];
    E.set(d.chans.subarray(j * G, (j + 1) * G), r * ST + base);
    sim.fired[r] = d.fired[j];
  }
  if (screen) {
    const p = d.pixels;
    for (let j = 0; j + 4 < p.length; j += 5) screen.fire(p[j], [p[j + 1], p[j + 2], p[j + 3], p[j + 4]]);
  }
  sim.tick = d.tick;
  Object.assign(sim.stats, d.stats);
  sim.version++;
  sim.fc = null;
  sim.sched = null;
}

// The whole dynamic state: every run, every ring, every gate row.
export function exportFull(sim) {
  const tr = { runs: sim.order.map((_, i) => i), rows: sim.gates.map(g => g.row), pushes: [], pixels: [], clear() {} };
  const d = takeDelta(sim, tr, { type: "state" });
  // The rings whole, run by run.
  const caps = Int32Array.from(sim.order, s => s.cap);
  const total = caps.reduce((a, b) => a + b, 0);
  const enter = new Float64Array(total), payload = new Float32Array(total);
  let at = 0;
  for (const s of sim.order) { enter.set(s.enter, at); payload.set(s.payload, at); at += s.cap; }
  Object.assign(d, { caps, ringEnter: enter, ringPayload: payload });
  d.transfer.push(caps.buffer, enter.buffer, payload.buffer);
  return d;
}

export function applyFull(sim, d) {
  applyDelta(sim, d);
  let at = 0;
  sim.order.forEach((s, i) => {
    const cap = d.caps[i];
    if (cap === s.cap) { s.enter.set(d.ringEnter.subarray(at, at + cap)); s.payload.set(d.ringPayload.subarray(at, at + cap)); }
    at += cap;
  });
}

// The board as the worker needs it: the arrays, the run-id allocator as the
// mirror's rebuild found it once the old runs were released (Sim.lastAlloc),
// and the tick (seeds are dated from it). Copies, so the main thread's board stays its own.
export function boardMessage(sim, alloc, fresh) {
  const b = sim.board;
  const grid = b.grid.slice(), entities = b.entities.slice();
  return {
    type: "board", fresh, width: b.width, height: b.height, rows: b.rows, revision: b.revision,
    grid, entities, free: alloc.free, next: alloc.next, tick: alloc.tick,
    transfer: [grid.buffer, entities.buffer],
  };
}

// --- the worker's side -----------------------------------------------------------

// A Board with only what the Sim reads: the arrays and the cell and row
// helpers (Board.prototype's own methods).
function bareBoard(T, m) {
  const b = Object.create(Board.prototype);
  Object.assign(b, { T, width: m.width, height: m.height, grid: m.grid, entities: m.entities, rows: m.rows, revision: m.revision });
  return b;
}

// Ticks per run of the loop, at most: one run of the loop (`slice()`)
// steps for up to SLICE_MS and then yields, so a delta request or a pause is
// never kept waiting long.
export const SLICE_MS = 6;
// Ticking stops when the main thread has not asked for a delta for this
// long -- a background tab, whose frames have stopped -- and starts again,
// without catching up, at its next request. As on the main thread, where a
// hidden tab's frames, and so its ticks, simply stop; ticking on unseen
// would pile up a delta (and its audio) without bound.
export const IDLE_MS = 500;
// With a direct line to the synth (`audioport`), the audio tap's ticks go
// down it at least this often while playing, not once a frame: the synth
// keeps 60 ms queued, and a frame's gap (50 ms and more on a phone) plus
// the audio thread's own bursts was enough to run it dry.
export const AUDIO_POST_MS = 5;

export class SimHost {
  // `post(message, transfer)` sends to the main thread; `now()` is the clock.
  constructor(T, post, now = () => performance.now()) {
    this.T = T;
    this.post = post;
    this.now = now;
    this.sim = null;
    this.tr = null;
    this.clock = new Clock(30);
    this.epoch = 0;
    this.playing = false;
    this.last = null;            // the loop's last look at the clock
    this.pulled = 0;             // when the main thread last asked for a delta
    this.pixelsOn = false;
    this.sleeping = true;
    this.tapCfg = null;          // the audio recorder's configuration, or null: no audio
    this.tap = null;
    this.audioPort = null;       // a MessagePort into the synth, or null: audio rides the deltas
    this.audioSent = 0;
  }

  message(m) {
    switch (m.type) {
      case "board": return this.board(m);
      case "state":
        // The main thread's state wins: anything ticked here since is dropped,
        // and the next tick starts with everything awake.
        this.epoch = m.epoch;
        if (this.sim) { applyFull(this.sim, m); this.sim.wakeAll(); this.tr?.clear(); this.tap?.take(); }
        return;
      case "play": this.clock.setRate(m.rate); this.clock.frac = m.frac ?? this.clock.frac; this.clock.paused = false; this.playing = true; this.last = null; this.pulled = this.now(); return;
      case "pause":
        this.playing = false; this.clock.paused = true; this.reply();
        // The synth's last ticks, then its stop, down the same line, so
        // nothing of ours can arrive after the stop.
        this.audioPort?.postMessage({ type: "stop" });
        return;
      case "rate": this.clock.setRate(m.rate); return;
      case "pull": this.pulled = this.now(); this.reply(); return;
      case "pixels": this.pixelsOn = m.on; this.wire(); return;
      case "sleeping": this.sleeping = m.on; this.sim?.setSleeping(m.on); return;
      case "audio": this.tapCfg = m.cfg; this.wire(); return;
      case "audioport": this.audioPort?.close?.(); this.audioPort = m.port || null; return;
    }
  }

  board(m) {
    this.epoch = m.epoch;
    const T = this.T;
    // Whatever was gathered belongs to the old runs: dropped, and nothing is
    // gathered while the rebuild seeds the new ones (the mirror seeds the
    // same).
    this.tr?.clear();
    this.tap = null;
    if (this.sim) { this.sim.track = null; this.sim.audio = null; this.sim.screen = null; }
    // A new board gets a new Sim; an edit keeps this one. Either way it then
    // rebuilds exactly as the mirror's last rebuild did: from the allocator
    // as it stood before that rebuild (so every run gets the same id, which
    // a fresh Sim's own history would not give a worker started after
    // edits), the board's new arrays, and the tick it dated its seeds from.
    if (m.fresh || !this.sim) {
      this.sim = new Sim(T, bareBoard(T, m));
    } else {
      Object.assign(this.sim.board, { width: m.width, height: m.height, grid: m.grid, entities: m.entities, rows: m.rows, revision: m.revision });
    }
    this.sim.allocOverride = { free: m.free, next: m.next };
    this.sim.tick = m.tick;
    this.sim.seen = -1;
    this.sim.sync();
    this.sim.tick = m.tick;
    this.sim.setSleeping(this.sleeping);
    this.wire();
  }

  // The tracker, the Pixel log and the audio tap, onto the current Sim.
  wire() {
    const sim = this.sim;
    if (!sim) return;
    if (!this.tr || this.tr.runFlag.length !== sim.order.length || this.tr.rowFlag.length !== sim.board.rows) {
      this.tr = new Tracker(sim.board.rows, sim.order.length);
    }
    sim.track = this.tr;
    sim.screen = this.pixelsOn ? new PixelLog(this.tr) : null;
    if (this.tapCfg && this.tapCfg.runs === sim.order.length && this.tapCfg.gates === sim.gates.length) {
      AudioTap.config(sim, this.tapCfg);
      if (!this.tap || this.tap.gen !== this.tapCfg.gen) {
        this.tap = new AudioTap(this.tapCfg);
        // As Recorder.configure() does: the wake list hears a sleeping run
        // still sliding into place only if it was told of it, so a new
        // recorder starts with everything awake. Without it, runs asleep
        // when the configuration arrived went unheard while they slid.
        sim.wakeAll();
      }
      sim.audio = this.tap;
    } else {
      sim.audio = null;
      this.tap = null;
    }
  }

  reply() {
    if (!this.sim) return;
    const d = takeDelta(this.sim, this.tr, {
      epoch: this.epoch, frac: this.clock.frac, at: performance.timeOrigin + this.now(),
      dropped: this.clock.dropped, playing: this.playing, audio: this.audioPort ? null : this.tap?.take() ?? null,
    });
    this.sendAudio();
    const transfer = d.transfer;
    delete d.transfer;
    if (d.audio) transfer.push(d.audio.bits.buffer, d.audio.f0.buffer, d.audio.f1.buffer);
    this.post(d, transfer);
  }

  // The audio tap's ticks so far, straight to the synth: the message the
  // main thread's Recorder.flush() would post, at the worker's own rate.
  sendAudio() {
    this.audioSent = this.now();
    if (!this.audioPort || !this.tap) return;
    const a = this.tap.take();
    if (!a) return;
    this.audioPort.postMessage({ type: "ticks", ...a, rate: this.clock.rate }, [a.bits.buffer, a.f0.buffer, a.f1.buffer]);
  }

  // One run of the loop: the ticks the clock says are due, for at most
  // SLICE_MS; a backlog past that is dropped, as the main thread's frame
  // budget drops it, so a rate the board cannot keep up slows rather than
  // falling ever further behind. Returns how long until it should run again.
  slice() {
    if (!this.playing || !this.sim) return null;
    const now = this.now();
    if (now - this.pulled > IDLE_MS) { this.last = null; return null; }   // a pull restarts the loop
    const dt = this.last === null ? 0 : Math.min(0.25, (now - this.last) / 1000);
    this.last = now;
    const n = this.clock.advance(dt);
    const t0 = this.now();
    for (let i = 0; i < n; i++) {
      if (i > 0 && this.now() - t0 > SLICE_MS) { this.clock.dropped += n - i; break; }
      this.sim.step();
    }
    if (this.audioPort && this.now() - this.audioSent >= AUDIO_POST_MS) this.sendAudio();
    // The next tick's due time; run again at once when there is work.
    return n > 0 ? 0 : Math.max(1, ((1 - this.clock.frac) / this.clock.rate) * 1000);
  }
}
