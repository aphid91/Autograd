// The simulation on a worker (simhost.js), driven in one process: a main
// side holding the mirror, a worker side (SimHost) holding the Sim that
// ticks, and every message between them passed through structuredClone --
// so anything that would not survive the trip to a real worker fails here
// too. The mirror is held to a REFERENCE timeline (an ordinary Sim on its
// own copy of the board, stepped directly) after every delta: every ring,
// counter, anchor and channel, the activity stamps, the Pixel screen in
// paint order, and what an audio recorder hears.
//
// The cases are the ones the protocol exists for: deltas of 1 to 60 ticks;
// an edit mid-run (the worker rebuilds as the mirror did, numbering every
// run the same, or nothing after it lines up); a Reset; the main thread
// taking over -- stepping the mirror itself, then handing the whole state
// back -- with a stale delta still in flight, which the epoch must drop.

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync, existsSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { Board } from "../board.js";
import { Sim } from "../sim.js";
import { installPixel, Screen } from "../pixel.js";
import { Recorder } from "../audio.js";
import { AUDIO_DEFAULTS } from "../audio-params.js";
import { SimHost, applyDelta, exportFull, boardMessage, IDLE_MS } from "../simhost.js";
import { stateLine } from "./sim_state.mjs";

const here = dirname(fileURLToPath(import.meta.url));
const root = join(here, "..", "..");
const T = installPixel(JSON.parse(readFileSync(join(here, "..", "types.json"), "utf8")));

async function loadBoard(path) {
  const bytes = readFileSync(path);
  return Board.fromNpz(T, bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength));
}

const stamps = (sim) => sim.order.map(s => `${s.lastPush},${s.lastPop}`).join(" ") + " | " + sim.gates.map(g => sim.fired[g.row]).join(" ");
const screenLine = (sim) => [...sim.screen.px].map(([r, p]) => `${r}:${p.css}:${p.z}`).join(" ");

// Every tick a recorder has posted, as one list per field.
function heard(log) {
  const bits = [], f0 = [], f1 = [];
  for (const b of log) {
    const W = b.words;
    for (let k = 0; k < b.n; k++) {
      bits.push(Array.from(b.bits.subarray(k * W, (k + 1) * W)).join(","));
      f0.push(b.f0[k]); f1.push(b.f1[k]);
    }
  }
  return { bits, f0, f1 };
}

// The main thread and the worker, in one process. `mirror` is an existing
// Sim to attach a (late-starting) worker to; by default a fresh one.
class Pair {
  constructor(board, mirror = null) {
    this.epoch = 0;
    this.inbox = [];
    this.mirror = mirror || new Sim(T, board);
    this.mirror.screen ||= new Screen();
    this.host = new SimHost(T, (m) => this.inbox.push(structuredClone(m)));
    this.mirror.onRebuild = (alloc) => this.send({ ...boardMessage(this.mirror, alloc, false), epoch: ++this.epoch });
    this.send({ ...boardMessage(this.mirror, this.mirror.lastAlloc, true), epoch: ++this.epoch });
    // A mirror that has already ticked hands over its state too.
    if (mirror) this.send({ ...exportFull(this.mirror), epoch: this.epoch });
    this.send({ type: "pixels", on: true });
    this.log = [];
    this.rec = new Recorder({ postMessage: (m) => { if (m.type === "ticks") this.log.push(m); } }, { ...AUDIO_DEFAULTS, beltCutoff: 512 });
    this.rec.attach(this.mirror);
    this.sentGen = -1;
    this.applied = 0;
    this.dropped = 0;
  }
  send(m) { const { transfer, ...rest } = m; this.host.message(structuredClone(rest)); }
  // What paint() does each frame with the worker running: sync (an edit
  // rebuilds here, and onRebuild tells the worker), keep the worker's audio
  // tap configured, ask for a delta, apply what comes back.
  frame() {
    this.mirror.sync();
    this.rec.ensure(this.mirror);
    if (this.rec.cfgGen !== this.sentGen) { this.send({ type: "audio", cfg: this.rec.tapConfig(this.mirror) }); this.sentGen = this.rec.cfgGen; }
    this.send({ type: "pull" });
    this.drain();
  }
  drain() {
    for (const d of this.inbox.splice(0)) {
      if (d.epoch !== this.epoch) { this.dropped++; continue; }
      applyDelta(this.mirror, d, this.mirror.screen);
      this.rec.ingest(d.audio);
      this.applied++;
    }
    this.rec.flush(30);
  }
  // The main thread takes over: its own steps, then the whole state back.
  takeOver(n) {
    for (let i = 0; i < n; i++) this.mirror.step();
    this.send({ ...exportFull(this.mirror), epoch: ++this.epoch });
  }
}

function reference(board) {
  const sim = new Sim(T, board);
  sim.screen = new Screen();
  const log = [];
  const rec = new Recorder({ postMessage: (m) => { if (m.type === "ticks") log.push(m); } }, { ...AUDIO_DEFAULTS, beltCutoff: 512 });
  rec.attach(sim);
  return { sim, log, rec };
}

function same(pair, ref, at) {
  const a = stateLine(pair.mirror), b = stateLine(ref.sim);
  if (a !== b) {
    const pa = a.split(" s"), pb = b.split(" s");
    const i = pa.findIndex((x, j) => x !== pb[j]);
    assert.fail(`${at}: the mirror differs, first at ${pa[i]?.slice(0, 140)} vs ${pb[i]?.slice(0, 140)}`);
  }
  assert.equal(stamps(pair.mirror), stamps(ref.sim), `${at}: activity stamps`);
  assert.equal(screenLine(pair.mirror), screenLine(ref.sim), `${at}: the Pixel screen`);
}

const SAVES = ["cpu.npz", "web/presentations/test.npz", "web/saves/GameOfLife_SLOW.npz"];
for (const name of SAVES) {
  const path = join(root, name);
  test(`the mirror follows the worker exactly: ${name}`, { skip: !existsSync(path) }, async () => {
    const pair = new Pair(await loadBoard(path));
    const ref = reference(await loadBoard(path));
    const chunks = [1, 7, 60, 3, 25, 1, 40];
    let t = 0, frames = 0;
    const run = (n) => { for (let i = 0; i < n; i++) { pair.host.sim.step(); ref.sim.step(); } t += n; };
    const frame = (label) => { pair.frame(); ref.sim.sync(); same(pair, ref, `${label}, tick ${t}`); frames++; };

    for (let i = 0; i < 14; i++) { run(chunks[i % chunks.length]); frame("running"); }

    // The worker ticks on past the last delta, unseen -- Pixels firing among
    // it -- and then an edit lands: those ticks are the old board's, and
    // nothing of them (a Pixel fire included) may reach the mirror.
    for (let i = 0; i < 9; i++) pair.host.sim.step();
    // An edit on both boards: a Source's value (a rebuild on each side).
    const src = pair.mirror.gates.find(g => g.td.name === "source");
    pair.mirror.board.setEmit(src.row, 3.5);
    ref.sim.board.setEmit(src.row, 3.5);
    frame("after an edit");
    for (let i = 0; i < 6; i++) { run(chunks[i]); frame("after an edit"); }

    // A Reset on both (tick 0, a rebuild), with the worker ahead again.
    for (let i = 0; i < 9; i++) pair.host.sim.step();
    pair.mirror.reset(); ref.sim.reset(); t = 0;
    frame("after a Reset");
    for (let i = 0; i < 6; i++) { run(chunks[i + 1]); frame("after a Reset"); }

    // The main thread takes over with a delta in flight: the worker ticks
    // on unseen, then the mirror steps itself and sends its state back.
    for (let i = 0; i < 9; i++) pair.host.sim.step();
    pair.send({ type: "pull" });                  // a reply the takeover makes stale
    pair.takeOver(12);
    for (let i = 0; i < 12; i++) ref.sim.step();
    t += 12;
    pair.drain();
    assert.ok(pair.dropped > 0, "the stale delta was dropped by its epoch");
    same(pair, ref, `after the takeover, tick ${t}`);
    for (let i = 0; i < 6; i++) { run(chunks[i]); frame("after the takeover"); }

    // What the two recorders heard, tick for tick. The mirror's recorder
    // missed the ticks between a rebuild and its new configuration reaching
    // the worker, and heard the takeover's own steps itself; so compare the
    // reference over the ticks the mirror's recorder has, matched by count.
    ref.rec.flush(30);
    const a = heard(pair.log), b = heard(ref.log);
    assert.ok(a.bits.length > 100, `the mirror's recorder heard ${a.bits.length} ticks`);
    assert.ok(a.bits.some(x => /[1-9]/.test(x)), "some runs were heard");
    assert.ok(a.f0.concat(a.f1).some(v => v > 0), "some gates were heard");
    // The last stretch, after the takeover, is configured on both and unbroken.
    const tail = 6 * 25;
    assert.deepEqual(a.bits.slice(-tail), b.bits.slice(-tail), "run bits over the last ticks");
    assert.deepEqual(a.f0.slice(-tail), b.f0.slice(-tail), "group 0 clicks over the last ticks");
    assert.deepEqual(a.f1.slice(-tail), b.f1.slice(-tail), "group 1 clicks over the last ticks");
    assert.ok(pair.applied >= frames, "every frame applied a delta");
  });
}

// A worker that starts after the board has been edited, reset and run: its
// fresh Sim must still number every run as the mirror does, and carry on
// from the mirror's state.
test("a worker started late, after edits, lines up with the mirror", { skip: !existsSync(join(root, "cpu.npz")) }, async () => {
  const path = join(root, "cpu.npz");
  const mirror = new Sim(T, await loadBoard(path)), ref = reference(await loadBoard(path));
  mirror.screen = new Screen();
  const src = mirror.gates.find(g => g.td.name === "source");
  for (const sim of [mirror, ref.sim]) {
    for (let i = 0; i < 40; i++) sim.step();
    sim.board.setEmit(src.row, 2);
    for (let i = 0; i < 30; i++) sim.step();
    sim.reset();
    for (let i = 0; i < 25; i++) sim.step();
  }
  same({ mirror }, ref, "before the worker");
  const pair = new Pair(null, mirror);
  for (let f = 0; f < 10; f++) {
    for (let i = 0; i < 11; i++) { pair.host.sim.step(); ref.sim.step(); }
    pair.frame();
    same(pair, ref, `frame ${f}`);
  }
});

// The loop itself: a slice steps what the clock says is due and no more
// than SLICE_MS of it, and a paused host does nothing.
test("the worker's loop ticks at its rate and stops when paused", { skip: !existsSync(join(root, "cpu.npz")) }, async () => {
  let now = 0;
  const posted = [];
  const host = new SimHost(T, (m) => posted.push(m), () => now);
  const board = await loadBoard(join(root, "cpu.npz"));
  const mirror = new Sim(T, board);
  host.message(structuredClone({ ...boardMessage(mirror, { free: [], next: 1, tick: 0 }, true), transfer: undefined, epoch: 1 }));
  assert.equal(host.slice(), null, "paused: no slice");
  host.message({ type: "play", rate: 600, frac: 0 });
  host.slice();                                  // the first look only starts the clock
  for (let f = 0; f < 60; f++) { now += 1000 / 60; host.message({ type: "pull" }); host.slice(); }
  const ticked = host.sim.tick;
  assert.ok(ticked >= 599 && ticked <= 600, `one second at 600 tps is 600 ticks, give or take float residue: ${ticked}`);
  host.message({ type: "pause" });
  const d = posted.at(-1);
  assert.equal(d.type, "delta");
  assert.equal(d.tick, ticked);
  assert.equal(d.playing, false);
  now += 1000;
  assert.equal(host.slice(), null);
  assert.equal(host.sim.tick, ticked, "nothing ticks while paused");

  // Playing, but nobody asking: a background tab. It idles after IDLE_MS
  // and the next request starts it again without catching up.
  host.message({ type: "play", rate: 600, frac: 0 });
  host.slice();
  for (let f = 0; f < 120; f++) { now += 1000 / 60; host.slice(); }
  const idle = host.sim.tick - ticked;
  assert.ok(idle > 0 && idle <= 600 * IDLE_MS / 1000 + 1, `ticked ${idle} unasked, then stopped`);
  assert.equal(host.slice(), null, "idle: the loop stops");
  host.message({ type: "pull" });
  host.slice();
  now += 1000 / 60;
  host.slice();
  assert.ok(host.sim.tick - ticked - idle <= 11, "a pull restarts it without catching up");
  assert.ok(host.sim.tick - ticked - idle >= 9, "...and it ticks again");
});

// The recorder reconfigures without a rebuild when the sound selection
// changes, so a delta already in flight carries bits numbered for the old
// candidates under the same epoch: its generation is what drops them.
test("audio recorded under an old configuration is dropped", { skip: !existsSync(join(root, "cpu.npz")) }, async () => {
  const pair = new Pair(await loadBoard(join(root, "cpu.npz")));
  for (let f = 0; f < 4; f++) { for (let i = 0; i < 10; i++) pair.host.sim.step(); pair.frame(); }
  const heardBefore = heard(pair.log).bits.length;
  assert.ok(heardBefore >= 30, `heard ${heardBefore} ticks before the change`);
  // Ticks the worker records under the current configuration, in flight...
  for (let i = 0; i < 10; i++) pair.host.sim.step();
  pair.host.message({ type: "pull" });
  // ...when the selection changes: half the board.
  const W = pair.mirror.board.width, sel = new Set();
  for (const seg of pair.mirror.order) for (const [x, y] of seg.cells) if (x < W / 2) sel.add(y * W + x);
  pair.rec.setSelection(sel);
  const before = heard(pair.log).bits.length;
  pair.drain();
  assert.equal(heard(pair.log).bits.length, before, "the stale batch reached the synth");
  // The next frame sends the new configuration -- what the worker ticked
  // before it arrived is under the old one, and dropped too -- and
  // recording carries on.
  for (let f = 0; f < 3; f++) { for (let i = 0; i < 10; i++) pair.host.sim.step(); pair.frame(); }
  assert.equal(heard(pair.log).bits.length, before + 20, "recording resumes under the new configuration");
});
