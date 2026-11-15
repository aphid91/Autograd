// The closed-form belt against the naive oracle, as tests/test_belts.py does
// on the desktop, plus the opcode edge cases. The whole tick is compared
// against the desktop's on real saves by tools/check_web_sim.py.

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { Sim, Clock, evalOps, takeDir } from "../sim.js";

const here = dirname(fileURLToPath(import.meta.url));
const T = JSON.parse(readFileSync(join(here, "..", "types.json"), "utf8"));
const UNIT = T.sim.UNIT;

// core/belts.py naive_tick(): every position stored, moved one at a time.
function naiveTick(pos, length, speed, spacing, offer, accept) {
  const next = [];
  pos.forEach((p, i) => next.push(Math.min(p + speed, i === 0 ? length : next[i - 1] - spacing)));
  const popped = next.length > 0 && next[0] === length && accept;
  if (popped) next.shift();
  const pushed = offer && (!next.length || next[next.length - 1] >= spacing);
  if (pushed) next.push(0);
  return next;
}

// A Sim with no board: just enough to tick one segment, with the port
// callbacks replaced by the scripted producer and consumer.
function beltOnly(length, speed, spacing) {
  const sim = Object.create(Sim.prototype);
  Object.assign(sim, { T, UNIT, speed, spacing, stats: { pushed: 0, popped: 0 } });
  const cap = 64;
  const seg = { sid: 1, length, head: 0, tail: 0, anchor: length * UNIT, cap,
                enter: new Float64Array(cap), payload: new Float32Array(cap) };
  return { sim, seg };
}

function run(length, offers, accepts, speed = UNIT, spacing = UNIT) {
  const { sim, seg } = beltOnly(length, speed, spacing);
  let naive = [];
  offers.forEach((o, t) => {
    sim.offer = () => (o ? 1 : null);
    sim.took = () => {};
    sim.accept = () => accepts[t];
    sim.tickSegment(seg, t);
    naive = naiveTick(naive, length * UNIT, speed, spacing, o, accepts[t]);
    const closed = [];
    for (let k = seg.head; k < seg.tail; k++) closed.push(sim.positionOf(seg, k, t));
    assert.deepEqual(closed, naive, `tick ${t}, length ${length}, v ${speed}, d ${spacing}`);
  });
  return seg;
}

test("the closed form agrees with the naive oracle on fixed cases", () => {
  const n = 24;
  const all = (v) => Array(n).fill(v);
  run(5, all(true), all(true));                                   // saturated
  run(5, all(true), all(false));                                  // blocked
  run(6, all(true), [...Array(8).fill(false), ...Array(16).fill(true)]);   // stall, then drain
  run(8, Array.from({ length: n }, (_, i) => [0, 3, 5].includes(i % 6)), all(true));
  run(6, all(true), all(true), UNIT, 2 * UNIT);                   // d > v
  run(6, all(true), all(true), 2 * UNIT, UNIT);                   // v > d
  run(1, all(true), all(true));                                   // one cell
});

test("a blocked run fills to L/d + 1 and no further", () => {
  const seg = run(5, Array(40).fill(true), Array(40).fill(false));
  assert.equal(seg.tail - seg.head, 6);
});

test("the closed form agrees with the oracle on random configurations", () => {
  let s = 12345;
  const rnd = () => ((s = (s * 1103515245 + 12345) % 2 ** 31) / 2 ** 31);
  for (let i = 0; i < 300; i++) {
    const length = 1 + Math.floor(rnd() * 9);
    const speed = UNIT * (1 + Math.floor(rnd() * 3));
    const spacing = UNIT * (1 + Math.floor(rnd() * 3));
    const pOffer = rnd(), pAccept = rnd();
    const ticks = 60;
    const offers = Array.from({ length: ticks }, () => rnd() < pOffer);
    const accepts = Array.from({ length: ticks }, () => rnd() < pAccept);
    run(length, offers, accepts, speed, spacing);
  }
});

test("take_dir: straight through first, then counter-clockwise", () => {
  for (let f = 0; f < 4; f++) {
    const behind = (f + 2) % 4, side = (f + 1) % 4;
    assert.equal(takeDir(f, [side, behind]), behind);
    assert.equal(takeDir(f, [(f + 3) % 4, side]), side);
  }
  assert.equal(takeDir(0, []), null);
});

test("the opcodes clamp and propagate as eval_ops does", () => {
  const O = T.sim.OPS, L = T.sim.VALUE_LIMIT;
  assert.deepEqual(evalOps(T, [[O.OP_RECIP, 0]], [0], 0), [L]);
  assert.deepEqual(evalOps(T, [[O.OP_RECIP, 0]], [-0], 0), [-L]);
  assert.deepEqual(evalOps(T, [[O.OP_RECIP, 0]], [4], 0), [0.25]);
  assert.deepEqual(evalOps(T, [[O.OP_FLOOR, 0]], [Infinity], 0), [L]);
  assert.deepEqual(evalOps(T, [[O.OP_FLOOR, 0]], [-2.5], 0), [-3]);
  assert.ok(Number.isNaN(evalOps(T, [[O.OP_MAX, 0, 1]], [NaN, 1], 0)[0]));
  assert.deepEqual(evalOps(T, [[O.OP_MAC, 0, 1, 2]], [2, 3, 4], 0), [10]);
  // Route: one path emits, the other is silent rather than a copy.
  const route = T.types[Object.keys(T.types).find(k => T.types[k].class === "Route")].sim.ops;
  assert.deepEqual(evalOps(T, route, [7, 1], 0), [null, 7, 1]);
  assert.deepEqual(evalOps(T, route, [7, -1], 0), [7, null, -1]);
  assert.deepEqual(evalOps(T, [[O.OP_CONST]], [], 5), [5]);
});

test("the clock spends banked time in whole ticks and keeps the remainder", () => {
  const c = new Clock(10);
  assert.equal(c.advance(1), 0, "paused");
  c.paused = false;
  assert.equal(c.advance(0.25), 2);
  assert.ok(Math.abs(c.alpha - 0.5) < 1e-9);
  c.setRate(1);
  assert.ok(Math.abs(c.alpha - 0.5) < 1e-9, "a rate change keeps alpha");
  assert.equal(c.advance(0.5), 1);
  assert.equal(c.advance(1000), 240, "a stall is capped");
  assert.equal(c.alpha, 0);
});
