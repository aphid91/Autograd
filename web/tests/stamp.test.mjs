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

// Labels travel with a stamp: a label's box moves with the turn and the
// mirror, upright and unmirrored. The oracle is the machines themselves:
// each label is centred on a marker belt, and after any sequence of turns
// and mirrors it must still be centred on that same belt, whatever its own
// size. (core/stamp.py is NOT the oracle here: its rotated() and mirrored()
// turn a label inside the box and then shift it again when renormalising,
// so a label on a 3x1 run's last belt lands on the wrong end -- a desktop
// bug, recorded in web/CLAUDE.md.)
test("a stamp's labels stay on the machines they label through turns and mirrors", () => {
  const r = rng(19);
  const q = (v) => Math.round(v * 1000) / 1000;
  let checked = 0;
  for (let i = 0; i < 80; i++) {
    const base = randomStamp(r).entries.map(e => ({ ...e, dx: e.dx + 1, dy: e.dy + 1 }));
    // Marker belts on cells no machine covers, each with a label on it.
    const used = new Set(stampCells(T, { entries: base }, 0, 0).map(([x, y]) => `${x},${y}`));
    const markers = [], notes = [];
    for (let k = 0; k < 3; k++) {
      const x = Math.floor(r() * 9), y = Math.floor(r() * 9);
      if (used.has(`${x},${y}`)) continue;
      used.add(`${x},${y}`);
      markers.push({ type: "belt", dx: x, dy: y, dir: 0, flip: false, exit: false, partner: null, emit: null, seed: null });
      const w = 0.25 + Math.floor(r() * 20) * 0.25, h = 0.25 + Math.floor(r() * 12) * 0.25;   // exact in binary
      notes.push({ kind: "text", colour: 0, size: 1, text: "x", rect: [x + 0.5 - w / 2, y + 0.5 + h / 2, w, h] });
    }
    if (!markers.length) continue;
    let s = makeStamp(T, [...base, ...markers], notes).stamp;
    const n0 = base.length;
    for (let step = 0; step < 6; step++) {
      const op = Math.floor(r() * 3);
      s = op === 0 ? rotated(T, s, true) : op === 1 ? rotated(T, s, false) : mirrored(T, s);
      s.notes.forEach((n, k) => {
        const m = s.entries[n0 + k];
        const [x, y, w, h] = n.rect;
        assert.ok(Math.abs(x + w / 2 - (m.dx + 0.5)) < 1e-4 && Math.abs(y - h / 2 - (m.dy + 0.5)) < 1e-4,
          `stamp ${i} step ${step}: label at ${n.rect} is not on its belt at ${m.dx},${m.dy}`);
        assert.deepEqual([w, h], [notes[k].rect[2], notes[k].rect[3]], "a label keeps its own size");
        checked++;
      });
    }
  }
  assert.ok(checked > 500);
});

test("labels survive four turns and two mirrors, and a stamp of labels alone is valid", async () => {
  const { isValidStamp, copyStamp } = await import("../stamp.js");
  const s = makeStamp(T, [], [{ kind: "text", colour: 1, size: 2, text: "hi", rect: [3.25, 7.5, 2.5, 1.25] }]).stamp;
  assert.deepEqual([s.w, s.h], [3, 2]);
  assert.ok(isValidStamp(T, s));
  let t = s;
  for (let k = 0; k < 4; k++) t = rotated(T, t, true);
  assert.deepEqual(t.notes, s.notes);
  assert.deepEqual(mirrored(T, mirrored(T, s)).notes, s.notes);
  const c = copyStamp(s);
  c.notes[0].rect[0] = 99;
  assert.equal(s.notes[0].rect[0], s.notes[0].rect[0] === 99 ? -1 : s.notes[0].rect[0]);
  assert.notEqual(s.notes[0].rect[0], 99);
  assert.ok(!isValidStamp(T, { entries: [], w: 0, h: 0 }));
  assert.ok(isValidStamp(T, { entries: [{ type: "belt", dx: 0, dy: 0, dir: 0 }], w: 1, h: 1 }));   // stored before labels
});
