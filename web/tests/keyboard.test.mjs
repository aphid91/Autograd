// The keyboard (keyboard.js): a generator that emits what is typed. These
// hold the sim's side of it -- the queue, the firing, back pressure, the
// wake list, the forecast, a fresh start -- and the worker's: a key sent to
// the worker while it ticks reaches the mirror exactly as a reference
// timeline that was typed into directly.

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { Board, F_ASCII } from "../board.js";
import { Sim } from "../sim.js";
import { installWebTypes } from "../webtypes.js";
import { KEYBOARD_TYPE } from "../keyboard.js";
import { SimHost, applyDelta, exportFull, boardMessage } from "../simhost.js";
import { stateLine } from "./sim_state.mjs";

const here = dirname(fileURLToPath(import.meta.url));
const root = join(here, "..", "..");
const T = installWebTypes(JSON.parse(readFileSync(join(here, "..", "types.json"), "utf8")));
const buf = (b) => b.buffer.slice(b.byteOffset, b.byteOffset + b.byteLength);

// cpu.npz with a keyboard in a free corner, feeding a run of `len` belts
// north from its output that ends dangling (items pile up at its head).
async function rig(len = 8) {
  const board = await Board.fromNpz(T, buf(readFileSync(join(root, "cpu.npz"))));
  const x = 440, y = 440;
  for (let dx = 0; dx < 5; dx++) for (let dy = 0; dy < 3 + len; dy++) assert.ok(board.isFree(x + dx, y + dy), "the corner is free");
  const row = board.spawnStructure(KEYBOARD_TYPE, [x, y], 0, false, { derive: false });
  assert.ok(row > 0);
  for (let i = 0; i < len; i++) board.spawnBelt(x + 2, y + 3 + i, 1);
  board.derive();
  return { board, row, x, y };
}

const runOf = (sim, x, y) => sim.order.find(s => s.cells.some(([cx, cy]) => cx === x && cy === y));
const itemsOf = (sim, seg) => { const out = []; for (let k = seg.head; k < seg.tail; k++) out.push(seg.payload[k % seg.cap]); return out; };
const codes = (s) => Array.from(s, c => c.codePointAt(0));

test("the keyboard type: 5x3, one output in the middle of a long side", () => {
  const td = T.types[KEYBOARD_TYPE];
  assert.equal(td.class, "Keyboard");
  for (let d = 0; d < 4; d++) {
    const o = td.orient[`${d}0`];
    assert.deepEqual([o.w, o.h], d % 2 ? [3, 5] : [5, 3]);
    assert.equal(o.outputs.length, 1);
    assert.equal(o.inputs.length, 0);
    const [lx, ly, f] = o.outputs[0];
    // The middle of a long side, facing out of it.
    if (d % 2 === 0) { assert.equal(lx, 2); assert.equal(f % 2, 1); assert.ok(ly === 0 || ly === 2); }
    else { assert.equal(ly, 2); assert.equal(f % 2, 0); assert.ok(lx === 0 || lx === 2); }
  }
  assert.equal(installWebTypes(T), T, "installing twice changes nothing");
});

test("typed codes come out in order, one a tick, and nothing without a key", async () => {
  const { board, row, x, y } = await rig();
  const sim = new Sim(T, board);
  for (let t = 0; t < 10; t++) sim.step();
  const seg = runOf(sim, x + 2, y + 3);
  assert.ok(seg, "the keyboard's run");
  assert.equal(itemsOf(sim, seg).length, 0, "an untouched keyboard emits nothing");
  for (const c of codes("hi\n")) assert.ok(sim.inject(row, c));
  assert.equal(sim.keysWaiting(row), 3);
  sim.step();
  assert.equal(sim.keysWaiting(row), 2, "one token a tick");
  for (let t = 0; t < 20; t++) sim.step();
  assert.deepEqual(itemsOf(sim, seg), [104, 105, 10], "in the order typed");
  assert.equal(sim.keysWaiting(row), 0);
  assert.equal(sim.inject(sim.gates.find(g => g.td.name === "source").row, 65), false, "only a keyboard takes keys");
});

test("back pressure holds the rest in the queue", async () => {
  const { board, row, x, y } = await rig(1);   // one belt: it holds two items
  const sim = new Sim(T, board);
  for (const c of codes("abcdef")) sim.inject(row, c);
  for (let t = 0; t < 30; t++) sim.step();
  const seg = runOf(sim, x + 2, y + 3);
  const on = itemsOf(sim, seg).length;
  const zone = sim.outHeld(row, 0) ? 1 : 0;
  assert.equal(on + zone + sim.keysWaiting(row), 6, "nothing lost, nothing made");
  assert.ok(sim.keysWaiting(row) > 0, "some still waiting");
  assert.deepEqual(itemsOf(sim, seg), codes("abcdef").slice(0, on), "the oldest went first");
});

test("the wake list: a sleeping and a full timeline agree, typed into mid-run", async () => {
  const a = await rig(30), b = await rig(30);
  const sa = new Sim(T, a.board), sb = new Sim(T, b.board);
  sb.setSleeping(false);
  const text = codes("The quick brown fox.\n");
  let k = 0;
  for (let t = 0; t < 400; t++) {
    // Keys at irregular ticks, sometimes several at once, after long quiet
    // stretches in which the keyboard has gone to sleep.
    if ((t > 40 && t % 37 === 0) || (t > 200 && t % 5 === 0)) {
      const n = 1 + (t % 3);
      for (let i = 0; i < n && k < text.length; i++, k++) { sa.inject(a.row, text[k]); sb.inject(b.row, text[k]); }
    }
    sa.step(); sb.step();
    assert.equal(stateLine(sa), stateLine(sb), `tick ${t}`);
    assert.deepEqual([...sa.keys], [...sb.keys], `the queues, tick ${t}`);
  }
  assert.equal(k, text.length, "everything was typed");
  assert.deepEqual(itemsOf(sa, runOf(sa, a.x + 2, a.y + 3)), text, "and arrived");
  assert.ok(sa.sched.stats.gates < sa.sched.stats.ticks * sa.gates.length * 0.6, "the wake list slept");
});

test("a forecast does not eat a token; a fresh start forgets them", async () => {
  const { board, row } = await rig();
  const sim = new Sim(T, board);
  sim.step();
  sim.inject(row, 65);
  const fc = sim.forecast();
  assert.ok(fc.fires.has(row), "the forecast sees the coming fire");
  assert.equal(sim.keysWaiting(row), 1, "but the token is still there");
  sim.step();
  assert.equal(sim.keysWaiting(row), 0);
  sim.inject(row, 66); sim.inject(row, 67);
  const c = sim.clone();
  assert.equal(c.keysWaiting(row), 2, "a clone carries the queue");
  c.step();
  assert.equal(sim.keysWaiting(row), 2, "and owns its copy");
  sim.reset();
  assert.equal(sim.keysWaiting(row), 0, "Reset empties it");
  sim.inject(row, 68);
  board.setEmit(board.structs.find(s => s.emit !== null).row, 5);
  sim.sync();
  assert.equal(sim.keysWaiting(row), 0, "so does an edit");
});

test("keys sent to the worker reach the mirror as typed into a reference", async () => {
  const a = await rig(30), r = await rig(30);
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
  const text = codes("echo hello");
  let k = 0;
  for (let f = 0; f < 60; f++) {
    // Keys go to the worker, between frames, as main.js sends them.
    if (f % 4 === 1 && k < text.length) {
      for (const c of text.slice(k, k + 2)) { send({ type: "key", row: a.row, code: c, revision: a.board.revision }); ref.inject(r.row, c); }
      k += 2;
    }
    const n = 1 + (f % 3);
    for (let i = 0; i < n; i++) { host.sim.step(); ref.step(); }
    frame();
    assert.equal(stateLine(mirror), stateLine(ref), `frame ${f}`);
    assert.equal(mirror.keysWaiting(a.row), ref.keysWaiting(r.row), `the mirror's queue, frame ${f}`);
  }
  assert.deepEqual(itemsOf(mirror, runOf(mirror, a.x + 2, a.y + 3)), text);
  // A key for an older board is dropped: typed, then an edit, then delivered.
  send({ type: "key", row: a.row, code: 90, revision: a.board.revision - 1 });
  assert.equal(host.sim.keysWaiting(a.row), 0, "a stale key is dropped");
  // The main thread takes over with keys waiting: its full state carries them.
  mirror.inject(a.row, 33); mirror.inject(a.row, 34);
  send({ ...exportFull(mirror), epoch: ++epoch });
  assert.equal(host.sim.keysWaiting(a.row), 2, "a full state carries the queue to the worker");
});

// The ASCII flag (board.js F_ASCII, slot 24): cosmetic, as on a Source --
// it says how the board shows values while this keyboard is inspected. A
// fresh keyboard starts with it on; it travels through a copy and paste,
// and toggling it is not an edit, so the keys flow the same either way.
test("a keyboard carries an ASCII flag, on by default, that changes nothing it emits", async () => {
  const { board, row, x, y } = await rig();
  assert.equal(board.describe(row).ascii, true, "on for a fresh keyboard");
  assert.equal(board.field(row, F_ASCII), 1);
  const rev = board.revision;
  assert.ok(board.setAscii(row, false));
  assert.equal(board.describe(row).ascii, false);
  assert.equal(board.revision, rev, "not an edit");
  // Copied with the flag off, it pastes off: an explicit flag beats the default.
  const [e] = board.capture([row]);
  assert.equal(e.ascii, false);
  const keyboards = () => { const out = []; for (let r = 1; r < board.rows; r++) if (board.describe(r)?.td.name === "keyboard") out.push(r); return out; };
  const before = new Set(keyboards());
  assert.equal(board.placeEntries([e], [[x, y - 20]], { derive: false }).size, 1);
  const pasted = keyboards().filter(r => !before.has(r));
  assert.equal(pasted.length, 1);
  assert.equal(board.describe(pasted[0]).ascii, false);
  board.setAscii(pasted[0], true);
  assert.equal(board.capture([pasted[0]])[0].ascii, true);
  // A row with slot 24 clear (an older save) reads as off.
  board.setField(row, F_ASCII, 0);
  assert.equal(board.describe(row).ascii, false);
  // The keys are unchanged either way.
  board.derive();
  const sim = new Sim(T, board);
  board.setAscii(row, false);
  assert.ok(sim.inject(row, 65));
  for (let t = 0; t < 3; t++) sim.step();
  board.setAscii(row, true);
  assert.ok(sim.inject(row, 66));
  for (let t = 0; t < 12; t++) sim.step();
  assert.deepEqual(itemsOf(sim, runOf(sim, x + 2, y + 3)), [65, 66]);
});
