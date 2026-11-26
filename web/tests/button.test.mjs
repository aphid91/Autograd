// The button (button.js): a machine that answers each poll on its input
// with its value while "on", else 0. These hold the sim's side -- the three
// modes, the forecast, a fresh start, the wake list -- and the worker's:
// presses sent to the worker while it ticks reach the mirror exactly as a
// reference timeline pressed directly.

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { Board } from "../board.js";
import { Sim } from "../sim.js";
import { installWebTypes } from "../webtypes.js";
import { BUTTON_TYPE, BTN_EVENT, BTN_HOLD, BTN_TOGGLE, BUTTON_EVENT_MAX, BUTTON_DEFAULT_VALUE, F_BTN_VALUE } from "../button.js";
import { SimHost, applyDelta, exportFull, boardMessage } from "../simhost.js";
import { stateLine } from "./sim_state.mjs";

const here = dirname(fileURLToPath(import.meta.url));
const root = join(here, "..", "..");
const T = installWebTypes(JSON.parse(readFileSync(join(here, "..", "types.json"), "utf8")));
const buf = (b) => b.buffer.slice(b.byteOffset, b.byteOffset + b.byteLength);
const SOURCE = Number(Object.keys(T.types).find(k => T.types[k].name === "source"));
const V = 7;

// cpu.npz with a button in a free strip. A Source two belts to its west
// polls it every tick (its east port feeds the button's input); `len` belts
// east of the button take its answers to a dangling end.
async function rig(mode = BTN_EVENT, len = 140) {
  const board = await Board.fromNpz(T, buf(readFileSync(join(root, "cpu.npz"))));
  const x = 300, y = 440;
  for (let dx = -4; dx < 3 + len; dx++) for (let dy = 0; dy < 3; dy++) assert.ok(board.isFree(x + dx, y + dy), "the corner is free");
  const row = board.spawnStructure(BUTTON_TYPE, [x, y], 0, false, { emit: V, mode, derive: false });
  assert.ok(row > 0);
  const src = board.spawnStructure(SOURCE, [x - 4, y + 1], 0, false, { emit: 99, derive: false });
  assert.ok(src > 0);
  board.spawnBelt(x - 2, y + 1, 0); board.spawnBelt(x - 1, y + 1, 0);
  for (let i = 0; i < len; i++) board.spawnBelt(x + 3 + i, y + 1, 0);
  board.derive();
  return { board, row, x, y };
}

const runOf = (sim, x, y) => sim.order.find(s => s.cells.some(([cx, cy]) => cx === x && cy === y));
const itemsOf = (sim, seg) => { const out = []; for (let k = seg.head; k < seg.tail; k++) out.push(seg.payload[k % seg.cap]); return out; };
const out = (sim, r) => itemsOf(sim, runOf(sim, r.x + 3, r.y + 1));
const count = (xs, v) => xs.filter(a => a === v).length;
const tap = (sim, row) => { assert.ok(sim.press(row, 1)); sim.press(row, -1); };

test("the button type: 3x3, in and out across the middle, flip is a no-op", () => {
  const td = T.types[BUTTON_TYPE];
  assert.equal(td.class, "Button");
  assert.equal(td.chiral, false);
  for (let d = 0; d < 4; d++) {
    const o = td.orient[`${d}0`], f = td.orient[`${d}1`];
    assert.deepEqual([o.w, o.h], [3, 3]);
    assert.equal(o.inputs.length, 1); assert.equal(o.outputs.length, 1);
    assert.deepEqual(o.pass, []);
    const [ix, iy, fi] = o.inputs[0], [ox, oy, fo] = o.outputs[0];
    assert.equal(fi, (d + 2) % 4, "input faces against the facing");
    assert.equal(fo, d, "output faces along it");
    // The middle of opposite sides, straight across.
    if (d % 2 === 0) { assert.equal(iy, 1); assert.equal(oy, 1); assert.equal(Math.abs(ix - ox), 2); }
    else { assert.equal(ix, 1); assert.equal(ox, 1); assert.equal(Math.abs(iy - oy), 2); }
    assert.deepEqual(f, o, "a mirror changes nothing");
  }
  assert.equal(installWebTypes(T), T, "installing twice changes nothing");
});

test("a placed button: its value, mode and ASCII flag in the row, a default value of 1", async () => {
  const { board, row, x, y } = await rig(BTN_HOLD);
  const info = board.describe(row);
  assert.equal(info.emit, V); assert.equal(info.mode, BTN_HOLD); assert.equal(info.ascii, false);
  // Copied and pasted, all three travel.
  board.setAscii(row, true);
  const [e] = board.capture([row]);
  assert.deepEqual([e.emit, e.mode, e.ascii], [V, BTN_HOLD, true]);
  const r2 = board.spawnStructure(BUTTON_TYPE, [x, y + 5], 1, false, {});
  assert.equal(board.describe(r2).emit, BUTTON_DEFAULT_VALUE);
  assert.equal(board.describe(r2).mode, BTN_EVENT);
  // The input channel is live: polling must not touch the value.
  const sim = new Sim(T, board);
  for (let t = 0; t < 10; t++) sim.step();
  assert.equal(board.field(row, F_BTN_VALUE), V);
  assert.equal(board.describe(row).emit, V);
});

test("event: each press answers one poll with the value, every other poll 0", async () => {
  const r = await rig(BTN_EVENT);
  const sim = new Sim(T, r.board);
  for (let t = 0; t < 10; t++) sim.step();
  assert.ok(out(sim, r).length > 3, "polled and answered");
  assert.equal(count(out(sim, r), V), 0, "unpressed, only 0s");
  tap(sim, r.row); tap(sim, r.row);
  // A held finger is one press, however long it stays.
  sim.press(r.row, 1);
  for (let t = 0; t < 10; t++) sim.step();
  sim.press(r.row, -1);
  for (let t = 0; t < 10; t++) sim.step();
  assert.equal(count(out(sim, r), V), 3);
  assert.ok(out(sim, r).every(v => v === 0 || v === V));
  // The cap.
  for (let i = 0; i < BUTTON_EVENT_MAX + 5; i++) tap(sim, r.row);
  for (let t = 0; t < 30; t++) sim.step();
  assert.equal(count(out(sim, r), V), 3 + BUTTON_EVENT_MAX);
});

test("hold: the value while a finger is on it, 0 after", async () => {
  const r = await rig(BTN_HOLD);
  const sim = new Sim(T, r.board);
  for (let t = 0; t < 10; t++) sim.step();
  const before = out(sim, r).length;
  sim.press(r.row, 1);
  sim.press(r.row, 1);   // two fingers
  for (let t = 0; t < 5; t++) sim.step();
  sim.press(r.row, -1);
  for (let t = 0; t < 3; t++) sim.step();
  sim.press(r.row, -1);
  const during = out(sim, r).slice(before);
  assert.equal(during.length, 8, "one poll a tick");
  assert.ok(during.every(v => v === V), "held by one finger or two");
  for (let t = 0; t < 5; t++) sim.step();
  assert.ok(out(sim, r).slice(before + 8).every(v => v === 0), "let go");
  assert.equal(sim.buttonLit(r.row), false);
});

test("toggle: each press flips it", async () => {
  const r = await rig(BTN_TOGGLE);
  const sim = new Sim(T, r.board);
  for (let t = 0; t < 5; t++) sim.step();
  tap(sim, r.row);
  assert.ok(sim.buttonOn(r.row) && sim.buttonLit(r.row));
  const n0 = out(sim, r).length;
  for (let t = 0; t < 6; t++) sim.step();
  assert.ok(out(sim, r).slice(n0).every(v => v === V));
  tap(sim, r.row);
  const n1 = out(sim, r).length;
  for (let t = 0; t < 6; t++) sim.step();
  assert.ok(out(sim, r).slice(n1).every(v => v === 0));
  assert.equal(sim.press(sim.gates.find(g => g.td.name === "source").row, 1), false, "only a button is pressed");
});

test("a forecast does not take an event; a clone owns its state; a fresh start forgets", async () => {
  const r = await rig(BTN_EVENT);
  const sim = new Sim(T, r.board);
  for (let t = 0; t < 5; t++) sim.step();
  tap(sim, r.row);
  const fc = sim.forecast();
  assert.ok(fc.fires.has(r.row), "the forecast sees the coming fire");
  assert.equal(sim.buttons.get(r.row).ev, 1, "but the event is still there");
  sim.step();
  assert.equal(sim.buttons.get(r.row).ev, 0);
  tap(sim, r.row);
  const c = sim.clone();
  c.step();
  assert.equal(sim.buttons.get(r.row).ev, 1, "the clone took its own copy");
  sim.reset();
  assert.equal(sim.buttonOn(r.row), false, "Reset forgets");
  tap(sim, r.row);
  r.board.setEmit(r.row, 3);
  sim.sync();
  assert.equal(sim.buttonOn(r.row), false, "so does an edit");
});

test("the wake list: a sleeping and a full timeline agree, pressed mid-run", async () => {
  for (const mode of [BTN_EVENT, BTN_HOLD, BTN_TOGGLE]) {
    const a = await rig(mode, 20), b = await rig(mode, 20);
    const sa = new Sim(T, a.board), sb = new Sim(T, b.board);
    sb.setSleeping(false);
    for (let t = 0; t < 200; t++) {
      if (t % 23 === 5) { sa.press(a.row, 1); sb.press(b.row, 1); }
      if (t % 23 === 9) { sa.press(a.row, -1); sb.press(b.row, -1); }
      sa.step(); sb.step();
      assert.equal(stateLine(sa), stateLine(sb), `mode ${mode}, tick ${t}`);
    }
  }
});

test("presses sent to the worker reach the mirror as pressed into a reference", async () => {
  const a = await rig(BTN_EVENT), r = await rig(BTN_EVENT);
  const mirror = new Sim(T, a.board), ref = new Sim(T, r.board);
  let epoch = 0;
  const inbox = [];
  const host = new SimHost(T, (m) => inbox.push(structuredClone(m)));
  const send = (m) => { const { transfer, ...rest } = m; host.message(structuredClone(rest)); };
  mirror.onRebuild = (alloc) => send({ ...boardMessage(mirror, alloc, false), epoch: ++epoch });
  send({ ...boardMessage(mirror, mirror.lastAlloc, true), epoch: ++epoch });
  const frame = () => {
    mirror.sync();
    send({ type: "pull" });
    for (const d of inbox.splice(0)) if (d.epoch === epoch) applyDelta(mirror, d);
  };
  for (let f = 0; f < 40; f++) {
    if (f % 5 === 1) for (const delta of [1, -1]) {
      send({ type: "press", row: a.row, delta, revision: a.board.revision });
      ref.press(r.row, delta);
    }
    const n = 1 + (f % 3);
    for (let i = 0; i < n; i++) { host.sim.step(); ref.step(); }
    frame();
    assert.equal(stateLine(mirror), stateLine(ref), `frame ${f}`);
    assert.deepEqual(mirror.buttons.get(a.row) ?? null, ref.buttons.get(r.row) ?? null, `the mirror's button, frame ${f}`);
  }
  assert.ok(count(out(mirror, a), V) >= 7, "the presses came out");
  send({ type: "press", row: a.row, delta: 1, revision: a.board.revision - 1 });
  assert.equal(host.sim.buttons.get(a.row)?.ev ?? 0, 0, "a stale press is dropped");
  // The main thread takes over with an event waiting: its full state carries it.
  mirror.press(a.row, 1);
  send({ ...exportFull(mirror), epoch: ++epoch });
  assert.equal(host.sim.buttons.get(a.row).ev, 1, "a full state carries the button");
});
