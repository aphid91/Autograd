// node --test web/tests/*.test.mjs
//
// The stamp transforms against core/stamp.py itself: random stamps of every
// type at every orientation, turned and mirrored both ways, must land on
// exactly the entries the desktop's Stamp.rotated()/mirrored() produce.

import { test } from "node:test";
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { makeStamp, rotated, mirrored, subset, stampCells } from "../stamp.js";

const ROOT = fileURLToPath(new URL("../..", import.meta.url));
const T = JSON.parse(readFileSync(new URL("../types.json", import.meta.url)));
const NAMES = Object.values(T.types).map(t => t.name);

function rng(seed) {
  let s = seed >>> 0;
  return () => ((s = (s * 1664525 + 1013904223) >>> 0) / 2 ** 32);
}

function randomStamp(r) {
  const n = 1 + Math.floor(r() * 5);
  const entries = [];
  for (let i = 0; i < n; i++) {
    entries.push({ type: NAMES[Math.floor(r() * NAMES.length)], dx: Math.floor(r() * 6), dy: Math.floor(r() * 6),
      dir: Math.floor(r() * 4), flip: r() < 0.5, exit: false, partner: null, emit: null, seed: null });
  }
  return makeStamp(T, entries).stamp;
}

const key = (s) => s.entries.map(e => `${e.type}@${e.dx},${e.dy}/${e.dir}${e.flip ? "f" : ""}`).sort().join(" ");

test("rotated and mirrored agree with core/stamp.py", () => {
  const r = rng(7);
  const stamps = Array.from({ length: 60 }, () => randomStamp(r));
  const py = `
import json, sys
sys.path.insert(0, ${JSON.stringify(ROOT)})
from core import cell
from core.entity import ALL_STRUCTURES, Belt, Underground
from core.stamp import Stamp, StampEntry
by = {cell.TYPE_NAMES[c.TYPE_ID]: c for c in (Belt, Underground) + ALL_STRUCTURES}
def key(s):
    return " ".join(sorted(f"{cell.TYPE_NAMES[e.entity_cls.TYPE_ID]}@{e.dx},{e.dy}/{e.direction}{'f' if e.flipped else ''}"
                           for e in s.entries))
out = []
for st in json.loads(sys.argv[1]):
    s = Stamp([StampEntry(by[e["type"]], e["dx"], e["dy"], e["dir"], e["flip"]) for e in st["entries"]]).normalise()
    out.append([key(s.rotated()), key(s.rotated(backward=True)), key(s.mirrored())])
print(json.dumps(out))`;
  const want = JSON.parse(execFileSync("python3", ["-c", py, JSON.stringify(stamps)]));
  stamps.forEach((s, i) => {
    assert.equal(key(rotated(T, s, false)), want[i][0], `ccw #${i}`);
    assert.equal(key(rotated(T, s, true)), want[i][1], `cw #${i}`);
    assert.equal(key(mirrored(T, s)), want[i][2], `mirror #${i}`);
  });
});

test("four turns and two mirrors are the identity", () => {
  const r = rng(11);
  for (let i = 0; i < 40; i++) {
    const s = randomStamp(r);
    let t = s;
    for (let k = 0; k < 4; k++) t = rotated(T, t, true);
    assert.equal(key(t), key(s));
    assert.equal(key(mirrored(T, mirrored(T, s))), key(s));
  }
});

test("a turn keeps the cell count and the box", () => {
  const r = rng(3);
  for (let i = 0; i < 40; i++) {
    const s = randomStamp(r);
    const t = rotated(T, s, true);
    assert.equal(stampCells(T, t, 0, 0).length, stampCells(T, s, 0, 0).length);
    assert.deepEqual([t.w, t.h], [s.h, s.w]);
  }
});

test("subset renumbers partners and drops links to entries that left", () => {
  const e = (partner) => ({ type: "under", dx: 0, dy: 0, dir: 0, flip: false, exit: false, partner, emit: null, seed: null });
  const kept = subset([e(2), e(null), e(0), e(4), e(3)], (i) => i !== 1 && i !== 4);
  assert.deepEqual(kept.map(k => k.partner), [1, 0, null]);
});

// Reverse on a gate: run values in through a machine, feed what comes out
// into its reverse (an output port and the input port on the same cell and
// face are joined), and every value must come back out of the port it went
// in by. That is the whole claim -- for every orientation, both control
// signs, and Merge/Route in both directions.
test("a reversed gate undoes the gate, port for port", async () => {
  const { reversedGate } = await import("../stamp.js");
  const byName = Object.fromEntries(Object.values(T.types).map(t => [t.name, t]));
  const key = p => p.join(",");
  // One fire of `td` at orientation `o`: { "x,y,face": value } in -> out.
  function fire(td, o, given) {
    const { sim } = td;
    const ch = new Array(sim.n_in).fill(null);
    o.inputs.forEach((p, i) => { if (key(p) in given) ch[sim.in_ch[i]] = given[key(p)]; });
    const out = {};
    o.outputs.forEach((p, i) => {
      const [op, ...a] = sim.ops[i];
      const v = op === 1 ? ch[a[0]]
        : op === 4 ? 1 / ch[a[0]]
        : op === 7 ? (ch[a[0]] > 0 ? ch[a[1]] : ch[a[2]])
        : op === 8 ? ((ch[a[0]] > 0) === !!a[2] ? ch[a[1]] : null)
        : (() => { throw new Error(`op ${op}`); })();
      if (v !== null && v !== undefined) out[key(p)] = v;
    });
    return out;
  }
  let checked = 0;
  for (const name of ["swap", "recip", "merge", "route"]) {
    for (let dir = 0; dir < 4; dir++) {
      for (const flip of [false, true]) {
        for (const control of [2.5, -1.5]) {
          const e = { type: name, dx: 0, dy: 0, dir, flip };
          const r = reversedGate(e);
          const a = byName[e.type], b = byName[r.type];
          const oa = a.orient[`${dir}${flip ? 1 : 0}`], ob = b.orient[`${r.dir}${r.flip ? 1 : 0}`];
          // The port sets swap roles exactly.
          assert.deepEqual(new Set(ob.inputs.map(key)), new Set(oa.outputs.map(key)), `${name} ${dir}${flip}`);
          assert.deepEqual(new Set(ob.outputs.map(key)), new Set(oa.inputs.map(key)), `${name} ${dir}${flip}`);
          // Distinct values in; the control (the one the select ops read) gets `control`.
          const given = {};
          oa.inputs.forEach((p, i) => { given[key(p)] = 10 + i; });
          const ctl = a.sim.ops.find(o => o[0] === 7 || o[0] === 8);
          if (ctl) {
            const i = a.sim.in_ch.indexOf(ctl[1]);
            given[key(oa.inputs[i])] = control;
          } else if (name === "recip") given[key(oa.inputs[0])] = 4;
          const back = fire(b, ob, fire(a, oa, given));
          assert.ok(Object.keys(back).length > 0, `${name}: nothing came back`);
          // A Merge keeps the lane it did not select, so its reverse returns
          // only what was sent; everything that does come back is in place.
          for (const [k, v] of Object.entries(back)) assert.equal(v, given[k], `${name} ${dir}${flip} c=${control} at ${k}`);
          if (name !== "merge") assert.equal(Object.keys(back).length, oa.inputs.length);
          checked++;
        }
      }
    }
  }
  assert.equal(checked, 64);
});
