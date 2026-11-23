// Pixel (pixel.js): the web-only machine that replaces Sink. Its ports, its
// firing, what it records on the screen, and the size and colour rules.

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync, existsSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { installPixel, placePorts, pixelOf, pixelSize, hsvToRgb, Screen, PIXEL_TYPE } from "../pixel.js";
import { typeByName } from "../stamp.js";
import { Board } from "../board.js";
import { Sim } from "../sim.js";

const here = dirname(fileURLToPath(import.meta.url));
const raw = () => JSON.parse(readFileSync(join(here, "..", "types.json"), "utf8"));

test("placePorts is the desktop's transform: it rebuilds every exported type's ports from its authored ones", () => {
  const T = raw();
  let checked = 0;
  for (const td of Object.values(T.types)) {
    if (!td.orient || td.kind !== "structure") continue;
    const base = td.orient["00"];        // facing +X unflipped is the authored layout
    for (const [key, o] of Object.entries(td.orient)) {
      const d = Number(key[0]), fl = key[1] === "1";
      for (const side of ["inputs", "outputs"]) {
        assert.deepEqual(placePorts(td.footprint, base[side], d, fl), o[side], `${td.class} ${key} ${side}`);
        checked++;
      }
    }
  }
  assert.ok(checked > 100);
});

test("Pixel takes Sink's id: H and V on cell 0, S above H, Z on the far corner; all four to fire", () => {
  const T = installPixel(raw());
  const td = T.types[PIXEL_TYPE];
  assert.equal(td.class, "Pixel");
  assert.deepEqual(td.orient["00"].inputs, [[0, 0, 2], [0, 1, 2], [0, 0, 3], [1, 1, 1]]);
  assert.deepEqual(td.orient["00"].outputs, []);
  assert.equal(td.sim.fire_rule, T.sim.FIRE_ALL);
  assert.equal(td.sim.n_in, 4);
  assert.ok(td.chiral);
  // Mirrored: the flip sends S below and Z to the bottom corner.
  assert.deepEqual(td.orient["01"].inputs, [[0, 1, 2], [0, 0, 2], [0, 1, 1], [1, 0, 3]]);
  // Every port faces out of the footprint, at every orientation.
  const S = T.cell.DIR_STEPS;
  for (const o of Object.values(td.orient)) {
    for (const [x, y, f] of o.inputs) {
      const nx = x + S[f][0], ny = y + S[f][1];
      assert.ok(nx < 0 || ny < 0 || nx >= o.w || ny >= o.h);
    }
  }
  assert.equal(typeByName(T, "sink").id, PIXEL_TYPE, "old 'sink' slots and stamps find the Pixel");
  assert.equal(installPixel(T), T, "installing twice changes nothing");
});

test("colour and size: hue wraps in turns, S and V clamp, NaN is 0; Z rounds within 1..max", () => {
  assert.deepEqual(pixelOf([1.25, 2, -1, 3]), { h: 0.25, s: 1, v: 0, ch: null, z: 3 });
  assert.deepEqual(pixelOf([-0.25, NaN, 0.5, null]), { h: 0.75, s: 0, v: 0.5, ch: null, z: 0 });
  assert.equal(pixelSize(0, 8), 0); assert.equal(pixelSize(-2, 8), 0); assert.equal(pixelSize(NaN, 8), 0);
  assert.equal(pixelSize(0.2, 8), 1, "any positive Z draws at least the machine");
  assert.equal(pixelSize(1, 8), 1); assert.equal(pixelSize(2.6, 8), 3); assert.equal(pixelSize(40, 8), 8);
  assert.deepEqual(hsvToRgb(0, 1, 1), [1, 0, 0]);
  assert.deepEqual(hsvToRgb(1 / 3, 1, 1), [0, 1, 0]);
  assert.deepEqual(hsvToRgb(0.5, 0, 0.5), [0.5, 0.5, 0.5]);
});

test("V above 1 is an ASCII code; its fractional part is the value, 0 meaning full", () => {
  const V = (v) => { const p = pixelOf([0, 0, v, 1]); return [p.ch, p.v]; };
  assert.deepEqual(V(1), [null, 1], "exactly 1 is still the square at full value");
  assert.deepEqual(V(0.5), [null, 0.5]);
  assert.deepEqual(V(55), ["7", 1]);
  assert.deepEqual(V(55.5), ["7", 0.5]);
  assert.deepEqual(V(55.25), ["7", 0.25], "the fractional part is the value itself, not 1 - it");
  assert.deepEqual(V(33), ["!", 1]); assert.deepEqual(V(126.75), ["~", 0.75]);
  assert.deepEqual(V(32), [" ", 1], "a space is printable: an empty square");
  // Non-printable codes are the square, with the same value rule.
  assert.deepEqual(V(1.5), [null, 0.5]);
  assert.deepEqual(V(10), [null, 1]);
  assert.deepEqual(V(127.25), [null, 0.25]);
  assert.deepEqual(V(200), [null, 1]);
  assert.deepEqual(V(1e9 + 0.5), [null, 0.5]);
  const scr = new Screen(); scr.fire(0, [0, 0, 55.5, 1]);
  assert.equal(scr.px.get(0).css, "rgb(128,128,128)", "a half-value character is drawn at half value");
});

const save = join(here, "..", "..", "cpu.npz");
async function load() {
  const T = installPixel(raw());
  const bytes = readFileSync(save);
  return Board.fromNpz(T, bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength));
}

test("a save's Sinks load as Pixels", { skip: !existsSync(save) }, async () => {
  const board = await load();
  const sim = new Sim(board.T, board);
  const pixels = sim.gates.filter(g => g.pixel);
  assert.equal(pixels.length, 9, "cpu.npz's nine Sinks");
  assert.ok(pixels.every(g => g.td.class === "Pixel" && g.sim.n_in === 4));
});

test("a fire records its colour on the screen, most recent last; not without all four, not in a forecast, cleared by an edit", { skip: !existsSync(save) }, async () => {
  const board = await load();
  const a = board.spawnStructure(PIXEL_TYPE, [500, 500], 0, false);
  const b = board.spawnStructure(PIXEL_TYPE, [504, 500], 0, false);
  const sim = new Sim(board.T, board), screen = new Screen();
  sim.screen = screen;
  const feed = (row, vals) => vals.forEach((v, c) => { if (v !== null) sim.setIn(row, c, v); });

  feed(a, [0.5, 1, 1, null]);                 // no Z: not ready
  sim.step();
  assert.equal(screen.px.size, 0);
  feed(a, [0.5, 1, 1, 2]);
  sim.forecast();                             // a forecast fires it and rolls back: not recorded
  assert.equal(screen.px.size, 0);
  sim.step();
  assert.deepEqual({ ...screen.px.get(a), css: undefined }, { h: 0.5, s: 1, v: 1, ch: null, z: 2, css: undefined });
  assert.equal(screen.px.get(a).css, "rgb(0,255,255)");
  assert.ok(!sim.inHeld(a, 0) && !sim.inHeld(a, 3), "its inputs were consumed");

  feed(b, [0, 1, 1, 1]); sim.step();
  feed(a, [0.25, 1, 1, 1]); sim.step();
  assert.deepEqual([...screen.px.keys()], [b, a], "the latest fire draws last");

  sim.screen = null;                          // overlay off: a fire records nothing
  screen.clear();
  feed(a, [0, 1, 1, 1]); sim.step();
  assert.equal(screen.px.size, 0);
  assert.ok(!sim.inHeld(a, 0), "but the Pixel still consumed its inputs");

  sim.screen = screen;
  feed(a, [0, 1, 1, 1]); sim.step();
  assert.equal(screen.px.size, 1);
  board.revision++;                           // any edit: the screen starts blank
  sim.step();
  assert.equal(screen.px.size, 0);
});
