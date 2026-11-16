// Where items are drawn (flow.js): the machines' links against their own
// opcodes, the path geometry at every orientation, the forecast against the
// step it predicts, and -- the point of the whole module -- that the picture
// is continuous: where an item is drawn at the end of a tick is where the
// next tick's state draws it.

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync, existsSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { Board } from "../board.js";
import { Sim, evalOps } from "../sim.js";
import { LINKS, geometry, linkFor, itemPoints, runPoint } from "../flow.js";
import { stateLine } from "./sim_state.mjs";

const here = dirname(fileURLToPath(import.meta.url));
const root = join(here, "..", "..");
const T = JSON.parse(readFileSync(join(here, "..", "types.json"), "utf8"));
const types = Object.values(T.types);
const SAVES = ["cpu.npz", "ram.npz", "Ram2.npz", "noram.npz", "Example.npz", "testing.npz"]
  .filter(f => existsSync(join(root, f)));

async function load(name) {
  const bytes = readFileSync(join(root, name));
  const board = await Board.fromNpz(T, bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength));
  return new Sim(T, board);
}

// A seeded stream of awkward values: signs, zero, fractions.
function values(n, seed) {
  const pool = [-3, -1, -0.5, 0, 0.25, 1, 2, 7];
  let s = seed;
  return Array.from({ length: n }, () => { s = (s * 1103515245 + 12345) >>> 0; return pool[s % pool.length]; });
}

test("every machine with inputs has links, except the legacy Sink", () => {
  for (const td of types) {
    if (td.kind !== "structure" || !td.sim || td.sim.n_in === 0) continue;
    if (td.class === "Sink") { assert.equal(LINKS.Sink, undefined); continue; }
    assert.ok(LINKS[td.class], `${td.class} has no links`);
    assert.equal(LINKS[td.class].length, td.sim.in_ch.length, `${td.class}: one entry per input port`);
  }
});

// The links are geometry; the opcodes decide. On every fire, each consumed
// input must find exactly one link, and each result emitted must be reached
// by exactly one of them -- or a moving item would vanish, double, or carry
// a result from an input that did not produce it.
test("each fire sends every consumed input down exactly one link, one per result", () => {
  for (const td of types) {
    const links = LINKS[td.class];
    if (!links) continue;
    const s = td.sim;
    for (let trial = 0; trial < 200; trial++) {
      const vals = values(s.n_in, trial + 1);
      const results = evalOps(T, s.ops, vals, 0);
      const consumed = s.select ? [s.select[0], vals[s.select[0]] > 0 ? s.select[1] : s.select[2]] : vals.map((_, c) => c);
      const hits = new Map();
      for (let i = 0; i < s.in_ch.length; i++) {
        if (!consumed.includes(s.in_ch[i])) continue;
        const j = linkFor(T, td, i, vals, results);
        assert.ok(j >= 0, `${td.class} input ${i} found no link for ${vals}`);
        hits.set(j, (hits.get(j) || 0) + 1);
      }
      results.forEach((r, oc) => {
        if (r === null) return;
        const j = s.out_ch.indexOf(oc);
        assert.equal(hits.get(j), 1, `${td.class} output ${j} reached ${hits.get(j) || 0} times for ${vals}`);
      });
    }
  }
});

test("paths run in-edge -> wait -> out-edge inside the footprint, at every orientation", () => {
  for (const td of types) {
    if (!LINKS[td.class]) continue;
    for (const [key, o] of Object.entries(td.orient)) {
      const geo = geometry(td, o);
      const [w, h] = [Math.max(...o.inputs.concat(o.outputs).map(p => p[0])) + 1, Math.max(...o.inputs.concat(o.outputs).map(p => p[1])) + 1];
      geo.ins.forEach((ip, i) => {
        assert.deepEqual(ip.inHalf[0], ip.edge);
        assert.deepEqual(ip.inHalf[ip.inHalf.length - 1], ip.wait);
        for (const [j, path] of ip.outs) {
          assert.deepEqual(path[0], ip.wait, `${td.class}/${key} ${i}->${j} starts at the wait point`);
          assert.deepEqual(path[path.length - 1], geo.outs[j].edge, `${td.class}/${key} ${i}->${j} ends on the out-edge`);
          for (const [x, y] of [...ip.inHalf, ...path]) {
            assert.ok(x >= -1e-9 && y >= -1e-9 && x <= w + 1e-9 && y <= h + 1e-9, `${td.class}/${key} leaves its body`);
          }
        }
      });
      // No two lanes wait on the same spot: their discs would sit on each other.
      const waits = geo.ins.map(ip => ip.wait.join(","));
      assert.equal(new Set(waits).size, waits.length, `${td.class}/${key}: two lanes share a wait point`);
    }
  }
});

test("the forecast is the next step, and leaves no trace", async () => {
  for (const name of SAVES.slice(0, 3)) {
    const sim = await load(name);
    for (let t = 0; t < 40; t++) {
      const before = stateLine(sim);
      const stats = JSON.stringify(sim.stats);
      const F = sim.forecast();
      assert.equal(stateLine(sim), before, `${name} t${t}: forecast changed the state`);
      assert.equal(JSON.stringify(sim.stats), stats);
      // Run the real step with the log on, and compare what it did.
      sim.rec = { pops: new Map(), pushes: new Map(), fires: new Map(), directs: [], slots: [] };
      sim.step();
      const rec = sim.rec;
      sim.rec = null;
      assert.deepEqual([...rec.pops.keys()].map(s => s.sid), [...F.pops.keys()].map(s => s.sid), `${name} t${t}: pops`);
      assert.deepEqual([...rec.pushes.values()], [...F.pushes.values()].map(p => p.value), `${name} t${t}: pushes`);
      assert.deepEqual([...rec.fires.keys()], [...F.fires.keys()], `${name} t${t}: fires`);
      assert.equal(rec.directs.length, [...F.directFrom.values()].length, `${name} t${t}: hand-offs`);
    }
  }
});

// Where every item is drawn at the end of a tick (alpha 1) against the
// start of the next (alpha 0), as multisets of positions. Items drawn for
// the last time (a destroyed result, one entering a Sink) are left out.
// Values are not compared: an input about to fire switches to its result
// at the wait point, on the tick boundary, by design (the link test above
// is what checks a result is the right one).
test("the picture is continuous across a tick", async () => {
  const key = (x, y) => `${x.toFixed(4)},${y.toFixed(4)}`;
  const bag = ({ pts, ends }, skipEnds) => {
    const m = new Map();
    for (let i = 0, n = 0; i < pts.length; i += 3, n++) {
      if (skipEnds && ends[n]) continue;
      const k = key(pts[i], pts[i + 1]);
      m.set(k, (m.get(k) || 0) + 1);
    }
    return m;
  };
  for (const name of SAVES) {
    const sim = await load(name);
    let compared = 0, moving = 0;
    const misses = [];
    for (let t = 0; t < 80; t++) {
      const end = bag(itemPoints(sim, 1), true);
      const F = sim.forecast();
      moving += F.pops.size + F.pushes.size + F.fires.size;
      sim.step();
      const start = bag(itemPoints(sim, 0), false);
      for (const [k, n] of end) {
        compared += n;
        const m = start.get(k) || 0;
        if (m < n) misses.push(`t${t} ${k} x${n - m}`);
      }
    }
    assert.ok(moving > 0, `${name}: nothing moved, so nothing was tested`);
    assert.deepEqual(misses.slice(0, 10), [], `${name}: ${misses.length} of ${compared} items jump between ticks`);
  }
});

// A run is drawn as one unbroken path: walking it in small steps never
// jumps (so an item rounds a corner on the belt rather than vanishing into
// one side and reappearing on the other), except across an underground's
// buried tiles. And its ends meet its machines exactly at their ports.
test("a run is one path through its corners, meeting its machines at their ports", async () => {
  const STEP = [[1, 0], [0, 1], [-1, 0], [0, -1]];
  const edgeOf = (x, y, f) => [x + 0.5 + 0.5 * STEP[f][0], y + 0.5 + 0.5 * STEP[f][1]];
  for (const name of SAVES.slice(0, 3)) {
    const sim = await load(name);
    let corners = 0, fed = 0, drained = 0;
    for (const seg of sim.segments.values()) {
      for (let i = 1; i + 1 < seg.dirs.length; i++) if (seg.dirs[i] !== seg.dirs[i - 1]) corners++;
      let last = null;
      for (let q = -0.5; q <= seg.length - 0.5 + 1e-9; q += 0.05) {
        const p = runPoint(sim, seg, q, 0.5);
        if (p && last) {
          const d = Math.hypot(p[0] - last[0], p[1] - last[1]);
          assert.ok(d < 0.05 + 1e-6, `${name} run ${seg.sid}: jumps ${d.toFixed(3)} at q=${q.toFixed(2)}`);
        }
        last = p;
      }
      if (seg.srcPort) {
        const g = sim.gateOf.get(seg.srcPort[0]);
        const [tx, ty] = seg.cells[0], f = seg.src[1];
        const port = g.outFaces.find(o => o.face === f && o.x === tx - STEP[f][0] && o.y === ty - STEP[f][1]);
        assert.deepEqual(runPoint(sim, seg, -0.5, 0.5), edgeOf(port.x, port.y, port.face), `${name} run ${seg.sid}: tail`);
        fed++;
      }
      if (seg.dstPort) {
        const [hx, hy] = seg.cells[seg.cells.length - 1], d = seg.dirs[seg.dirs.length - 1];
        assert.deepEqual(runPoint(sim, seg, seg.length - 0.5, 0.5), edgeOf(hx, hy, d), `${name} run ${seg.sid}: head`);
        drained++;
      }
    }
    assert.ok(corners > 0 && fed > 0 && drained > 0, `${name}: no corners or ports to test`);
  }
});
