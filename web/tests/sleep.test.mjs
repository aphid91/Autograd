// The wake list (Sim.sleepStep()) against the full tick (Sim.fullStep()):
// two timelines on each save, one sleeping and one visiting everything,
// compared EVERY tick -- every run's ring, counters and anchor, every gate's
// channels, the activity stamps the afterglow reads, and what an audio
// recorder hears. A sleeping tick that differs from the full one in anything
// at all is a missed wake. Halfway, both Reset (an edit's rebuild); a
// recorder is attached partway in, when some runs are already asleep with
// items still sliding.
//
// Not vacuous: each save must actually sleep (most runs and gates skipped)
// and fire timed wakes -- a checker whose board never slept, or never woke
// by the clock, would pass whatever the wake rules were.
//
// Two rules are unreachable at the shipped belt constants (speed == spacing)
// and are covered by re-running saves under other ones: the room-opens wake
// (with spacing == speed a run with no room has just pushed, so is awake)
// and sleeping only with the anchor at the end (one tick after a pop it is
// back). Mutation-tested: dropping either passed every shipped-constant
// save, and fails these. The Pixel screen is compared too, in order: gates
// must be asked in row order, since a Pixel fired later paints over one
// fired earlier, and nothing else in the state can see that order.

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
import { stateLine } from "./sim_state.mjs";

const here = dirname(fileURLToPath(import.meta.url));
const root = join(here, "..", "..");
const T = installPixel(JSON.parse(readFileSync(join(here, "..", "types.json"), "utf8")));

const SAVES = ["cpu.npz", "Ram2.npz", "ram.npz", "noram.npz", "web/presentations/test.npz", "web/saves/GameOfLife_SLOW.npz"];

async function load(path, types = T) {
  const bytes = readFileSync(path);
  const board = await Board.fromNpz(types, bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength));
  const sim = new Sim(types, board);
  sim.screen = new Screen();
  return sim;
}

// Belt constants other than the shipped ones, in UNITs: [speed, spacing].
const withBelts = (v, d) => ({ ...T, sim: { ...T.sim, SPEED: v * T.sim.UNIT, SPACING: d * T.sim.UNIT } });
const screenLine = (sim) => [...sim.screen.px].map(([r, p]) => `${r}:${p.css}:${p.z}`).join(" ");

// Everything stateLine() leaves out that is still the tick's output: the
// activity stamps the status colours' afterglow and the audio read.
function stamps(sim) {
  const runs = sim.order.map(s => `${s.lastPush},${s.lastPop}`).join(" ");
  const gates = sim.gates.map(g => sim.fired[g.row]).join(" ");
  return runs + " | " + gates;
}

function recorder(log) {
  return new Recorder({ postMessage: (m) => { if (m.type === "ticks") log.push(m); } }, { ...AUDIO_DEFAULTS, beltCutoff: 512 });
}

const CASES = [
  ...SAVES.map(name => [name, T, ""]),
  ["cpu.npz", withBelts(1, 2), " (spacing 2, speed 1)"],
  ["cpu.npz", withBelts(2, 3), " (spacing 3, speed 2)"],
  ["web/presentations/test.npz", withBelts(1, 3), " (spacing 3, speed 1)"],
  ["Ram2.npz", withBelts(3, 1), " (spacing 1, speed 3)"],
];

for (const [name, types, note] of CASES) {
  const path = join(root, name);
  test(`the wake list ticks exactly as the full step: ${name}${note}`, { skip: !existsSync(path) }, async () => {
    const asleep = await load(path, types), full = await load(path, types);
    full.setSleeping(false);
    assert.ok(asleep.sched && !full.sched);
    const N = 300, RESET = 150, HEAR = 40;
    const heardA = [], heardF = [];
    const recA = recorder(heardA), recF = recorder(heardF);
    let visitedRuns = 0, timed = 0, visitedGates = 0;
    for (let t = 0; t < N; t++) {
      if (t === HEAR) { recA.attach(asleep); recF.attach(full); }
      if (t === RESET) { asleep.reset(); full.reset(); }
      const s0 = { ...asleep.sched.stats };
      asleep.step();
      full.step();
      const s1 = asleep.sched.stats;
      visitedRuns += s1.runs - (t === RESET ? 0 : s0.runs);
      visitedGates += s1.gates - (t === RESET ? 0 : s0.gates);
      timed += s1.timed - (t === RESET ? 0 : s0.timed);
      if (t % 10 === 9 && t >= HEAR) { recA.flush(30); recF.flush(30); }
      const a = stateLine(asleep), f = stateLine(full);
      if (a !== f) {
        // Name the first run or gate that differs, not a 100 kB line.
        const pa = a.split(" s"), pf = f.split(" s");
        const i = pa.findIndex((x, j) => x !== pf[j]);
        assert.fail(`tick ${t}: the sleeping timeline differs, first at: ${pa[i]?.slice(0, 160)} vs ${pf[i]?.slice(0, 160)}`);
      }
      assert.equal(stamps(asleep), stamps(full), `tick ${t}: activity stamps differ`);
      assert.equal(screenLine(asleep), screenLine(full), `tick ${t}: the Pixel screen differs`);
    }
    // What the two recorders heard, tick for tick.
    assert.equal(heardA.length, heardF.length);
    heardA.forEach((b, i) => {
      const c = heardF[i];
      assert.equal(b.n, c.n);
      assert.deepEqual([...b.f0.slice(0, b.n)], [...c.f0.slice(0, c.n)], `batch ${i}: fire counts`);
      assert.deepEqual([...b.f1.slice(0, b.n)], [...c.f1.slice(0, c.n)], `batch ${i}: fire counts`);
      assert.deepEqual([...b.bits], [...c.bits], `batch ${i}: which runs were heard`);
    });
    assert.ok(heardA.some(b => b.bits.some(w => w !== 0)), "some runs were heard");
    // ...and the wake list did something.
    const runs = asleep.order.length, gates = asleep.gates.length;
    assert.ok(visitedRuns < 0.6 * runs * N, `runs visited ${visitedRuns} of ${runs * N}: barely slept`);
    assert.ok(visitedGates < 0.6 * gates * N, `gates asked ${visitedGates} of ${gates * N}: barely slept`);
    assert.ok(timed > 0, "no timed wake fired: the clock's half is untested");
  });
}

// The forecast runs the full step with the wake list set aside, and must
// leave the scheduler exactly as it found it.
test("a forecast leaves the wake list as it was", { skip: !existsSync(join(root, "cpu.npz")) }, async () => {
  const a = await load(join(root, "cpu.npz")), b = await load(join(root, "cpu.npz"));
  for (let t = 0; t < 120; t++) {
    a.forecast();
    a.step();
    b.step();
    assert.equal(stateLine(a), stateLine(b), `tick ${t}`);
  }
  assert.ok(a.sched, "still sleeping after forecasts");
});
