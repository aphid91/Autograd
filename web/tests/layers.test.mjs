// Layers: the table (layers.js) and the tag each entity carries in its row
// (board.js), through placing, lifting, pasting, undo and a save.

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { Board } from "../board.js";
import { readNpz } from "../npz.js";
import {
  F_LAYER, BACKGROUND, defaultLayers, parseLayers, serialiseLayers, addLayer, removeLayer,
  renameLayer, setCurrent, isVisible, isActive, setForActive, layerStats, byId,
} from "../layers.js";

const here = dirname(fileURLToPath(import.meta.url));
const root = join(here, "..", "..");
const T = JSON.parse(readFileSync(join(here, "..", "types.json"), "utf8"));
const buf = (b) => b.buffer.slice(b.byteOffset, b.byteOffset + b.byteLength);
const load = async (bytes) => Board.fromNpz(T, buf(new Uint8Array(bytes)));
const typeId = (name) => Number(Object.keys(T.types).find(k => T.types[k].name === name));

// An empty 32x32 board, built the way a save would hand it over.
function blank() {
  const W = 32, H = 32, rows = 64;
  return new Board(T, {
    version: { data: new Int32Array([1]), shape: [1] },
    size: { data: new Int32Array([W, H]), shape: [2] },
    grid: { data: new Int32Array(W * H * 4), shape: [H, W, 4] },
    entities: { data: new Float32Array(rows * T.entity.STRIDE), shape: [rows, T.entity.STRIDE] },
  });
}

test("the tag's slot is spare in the row", () => {
  assert.ok(T.entity.STRIDE > F_LAYER);
  for (const [k, v] of Object.entries(T.entity)) if (k !== "STRIDE") assert.notEqual(v, F_LAYER, k);
});

test("the table: names, ids that are never reused, Background fixed", () => {
  const t = defaultLayers();
  const a = addLayer(t), b = addLayer(t);
  assert.deepEqual(t.list.map(l => l.name), ["Background", "Layer 1", "Layer 2"]);
  assert.ok(removeLayer(t, a));
  // The lowest free NAME is reused; the id never is.
  const c = addLayer(t);
  assert.equal(byId(t, c).name, "Layer 1");
  assert.ok(c > b);
  assert.equal(removeLayer(t, BACKGROUND), false);
  assert.equal(renameLayer(t, BACKGROUND, "x"), false);
  assert.equal(renameLayer(t, b, "  "), false);
  assert.ok(renameLayer(t, b, "Adder"));
  assert.equal(byId(t, b).name, "Adder");
});

test("current is always visible and active; hidden implies inactive", () => {
  const t = defaultLayers();
  const a = addLayer(t);
  byId(t, a).visible = false;
  assert.equal(isVisible(t, a), false);
  assert.equal(isActive(t, a), false);
  byId(t, a).visible = true;
  byId(t, a).active = false;
  assert.equal(isActive(t, a), false);
  setCurrent(t, a);
  assert.ok(byId(t, a).visible && byId(t, a).active);
  // Deleting the current layer hands current back to Background.
  removeLayer(t, a);
  assert.equal(t.current, BACKGROUND);
});

test("the sliders set every active layer, never Background", () => {
  const t = defaultLayers();
  const a = addLayer(t), b = addLayer(t), c = addLayer(t);
  byId(t, b).active = false;
  byId(t, c).visible = false;
  setForActive(t, "tint", 0.5);
  assert.deepEqual(t.list.map(l => l.tint), [0, 0.5, 0, 0]);
  void a;
});

test("a malformed table is repaired, not refused", () => {
  assert.deepEqual(parseLayers("not json"), defaultLayers());
  const t = parseLayers(JSON.stringify({
    list: [{ id: 0, name: "Renamed", visible: false }, { id: 3, name: "M", colour: "nope", tint: 7 }, { id: 3 }, { id: -1 }],
    current: 99, next: 2,
  }));
  assert.deepEqual(t.list.map(l => l.id), [0, 3]);
  assert.equal(t.list[0].name, "Background");
  assert.equal(t.list[1].tint, 1);
  assert.match(t.list[1].colour, /^#[0-9a-f]{6}$/);
  assert.equal(t.next, 4);
  // An unknown current falls back to Background, which current forces on.
  assert.equal(t.current, BACKGROUND);
  assert.equal(t.list[0].visible, true);
  assert.deepEqual(parseLayers(serialiseLayers(t)), t);
});

test("placing tags the current layer; lifting and putting down keeps it", () => {
  const b = blank();
  const L = addLayer(b.layers);
  const belt = b.spawnBelt(1, 1, 0);
  assert.equal(b.layerOf(belt), BACKGROUND);
  setCurrent(b.layers, L);
  const shear = b.spawnStructure(typeId("shear"), [4, 4], 0, false);
  assert.equal(b.layerOf(shear), L);

  // A selection: captured, erased, and put back elsewhere -- with Background
  // now current, each keeps its own layer.
  setCurrent(b.layers, BACKGROUND);
  const caught = b.capture([belt, shear]);
  assert.deepEqual(caught.map(e => e.layer), [BACKGROUND, L]);
  b.eraseRows([belt, shear]);
  b.placeEntries(caught, caught.map(e => [e.dx + 10, e.dy + 10]));
  assert.equal(b.layerOf(b.entityAt(11, 11)), BACKGROUND);
  assert.equal(b.layerOf(b.entityAt(14, 14)), L);

  // A copy (no layer on its entries) goes on the current layer.
  setCurrent(b.layers, L);
  const copy = caught.map(e => ({ ...e, layer: undefined }));
  b.placeEntries(copy, copy.map(e => [e.dx + 20, e.dy]));
  assert.equal(b.layerOf(b.entityAt(21, 1)), L);
  // An entry naming a layer that no longer exists falls to the current one.
  b.placeEntries([{ ...caught[0], layer: 999 }], [[1, 20]]);
  assert.equal(b.layerOf(b.entityAt(1, 20)), L);
});

test("deleting a layer moves its entities to Background", () => {
  const b = blank();
  const L = addLayer(b.layers);
  setCurrent(b.layers, L);
  const r1 = b.spawnBelt(1, 1, 0), r2 = b.spawnBelt(2, 1, 0);
  assert.equal(b.retag(L, BACKGROUND), 2);
  removeLayer(b.layers, L);
  assert.equal(b.layerOf(r1), BACKGROUND);
  assert.equal(b.layerOf(r2), BACKGROUND);
});

test("undo restores membership and names but keeps the view", () => {
  const b = blank();
  const L = addLayer(b.layers);
  setCurrent(b.layers, L);
  const row = b.spawnBelt(3, 3, 0);
  const snap = b.snapshot();
  renameLayer(b.layers, L, "After");
  b.retag(L, BACKGROUND);
  setCurrent(b.layers, BACKGROUND);
  byId(b.layers, L).visible = false;
  byId(b.layers, L).tint = 0.7;
  b.restore(snap);
  assert.equal(byId(b.layers, L).name, "Layer 1");
  assert.equal(b.layerOf(row), L);
  assert.equal(byId(b.layers, L).visible, false);
  assert.equal(byId(b.layers, L).tint, 0.7);
  assert.equal(b.layers.current, BACKGROUND);
});

test("stats: counts, centre of mass, and the hidden-cell mask", () => {
  const b = blank();
  const L = addLayer(b.layers);
  setCurrent(b.layers, L);
  b.spawnBelt(2, 2, 0);
  b.spawnBelt(4, 2, 0);
  setCurrent(b.layers, BACKGROUND);
  b.spawnBelt(10, 10, 0);
  let s = layerStats(b, b.layers);
  assert.equal(s.counts.get(L), 2);
  assert.equal(s.counts.get(BACKGROUND), 1);
  assert.deepEqual(s.centres.get(L), [3.5, 2.5]);
  assert.equal(s.hidden, null);
  byId(b.layers, L).visible = false;
  s = layerStats(b, b.layers);
  assert.equal(s.hidden[2 * b.width + 2], 1);
  assert.equal(s.hidden[2 * b.width + 3], 0);
  assert.equal(s.hidden[10 * b.width + 10], 0);
});

test("a save carries the table and the tags; rows naming an unknown layer get one", async () => {
  const src = readFileSync(join(root, "ram.npz"));
  const b = await Board.fromNpz(T, buf(src));
  const L = addLayer(b.layers);
  renameLayer(b.layers, L, "RAM bank");
  const row = b.structs[0].row;
  b.setField(row, F_LAYER, L);
  b.setField(b.structs[1].row, F_LAYER, 42);     // a layer the table does not know
  const back = await load(await b.toNpz());
  assert.equal(byId(back.layers, L).name, "RAM bank");
  assert.equal(back.layerOf(row), L);
  assert.equal(byId(back.layers, 42).name, "Layer 42");
  assert.ok(back.layers.next > 42);
  const raw = await readNpz(buf(new Uint8Array(await b.toNpz())));
  assert.ok(JSON.parse(raw.layers.data).list.length >= 2);
});
