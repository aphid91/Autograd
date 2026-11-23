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
  isolate, isOnlyActive, wrapLabel, LABEL_MAX,
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
  // Background is counted but never has a centre: it never has a label.
  assert.equal(s.centres.has(BACKGROUND), false);
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

test("isolate: one layer active and current; again, every visible layer back", () => {
  const t = defaultLayers();
  const a = addLayer(t), b = addLayer(t), c = addLayer(t);
  byId(t, c).visible = false;
  assert.equal(isolate(t, b), "isolated");
  assert.equal(t.current, b);
  assert.deepEqual(t.list.map(l => isActive(t, l.id)), [false, false, true, false]);
  assert.ok(isOnlyActive(t, b));
  assert.equal(isolate(t, b), "all");
  // Every visible layer, Background included; the hidden one stays inactive.
  assert.deepEqual(t.list.map(l => isActive(t, l.id)), [true, true, true, false]);
  // Isolating a hidden layer shows it: it becomes current.
  isolate(t, c);
  assert.ok(isVisible(t, c) && isOnlyActive(t, c));
  void a;
});

test("labels: short names stay one line, long ones wrap into even lines", () => {
  const m = (s) => s.length * 0.55;   // a stand-in for measureText, in text heights
  assert.deepEqual(wrapLabel("Adder", m, 1.2), ["Adder"]);
  assert.deepEqual(wrapLabel("Layer 12", m, 1.2), ["Layer 12"]);
  assert.deepEqual(wrapLabel("Calculate bottom 13 digits in sequence", m, 1.2),
                   ["Calculate bottom 13", "digits in sequence"]);
  const long = "A very long description of the carry lookahead logic for the upper adder bank";
  const lines = wrapLabel(long, m, 1.2);
  assert.equal(lines.join(" "), long);         // every word, in order, none split
  assert.ok(lines.length >= 3);
  const ws = lines.map(m);
  assert.ok(Math.max(...ws) - Math.min(...ws) < 6, `balanced: ${lines}`);
  // A single word longer than any line is not split.
  assert.deepEqual(wrapLabel("Supercalifragilisticexpialidocious", m, 1.2), ["Supercalifragilisticexpialidocious"]);
});

test("the label slider's cap is 6, and a saved value past it is clamped", () => {
  assert.equal(LABEL_MAX, 6);
  const t = parseLayers(JSON.stringify({ list: [{ id: 1, name: "x", label: 12 }] }));
  assert.equal(byId(t, 1).label, 6);
});

test("labels: '//' forces a break, a single slash does not", () => {
  const m = (s) => s.length * 0.55;
  assert.deepEqual(wrapLabel("Carry // lookahead", m, 1.2), ["Carry", "lookahead"]);
  assert.deepEqual(wrapLabel("  Bank A//  bits 0/1 //", m, 1.2), ["Bank A", "bits 0/1"]);
  // Forced lines are used as typed: a long one is not wrapped further.
  const long = "Calculate bottom 13 digits in sequence";
  assert.deepEqual(wrapLabel(`${long}//x`, m, 1.2), [long, "x"]);
  assert.deepEqual(wrapLabel("a/b/c", m, 1.2), ["a/b/c"]);
  assert.deepEqual(wrapLabel("//", m, 1.2), [""]);
});

test("a label's offset and the labels switch survive a save; undo keeps the switch as it stands", async () => {
  const t = defaultLayers();
  const a = addLayer(t);
  byId(t, a).labelDx = 2.5; byId(t, a).labelDy = -1.25;
  t.showLabels = false;
  const back = parseLayers(serialiseLayers(t));
  assert.equal(byId(back, a).labelDx, 2.5);
  assert.equal(byId(back, a).labelDy, -1.25);
  assert.equal(back.showLabels, false);
  // Old tables have neither: no offset, labels on.
  const old = parseLayers(JSON.stringify({ list: [{ id: 1, name: "x" }] }));
  assert.equal(byId(old, 1).labelDx, 0);
  assert.equal(old.showLabels, true);

  const b = blank();
  const L = addLayer(b.layers);
  byId(b.layers, L).labelDx = 3;
  const snap = b.snapshot();
  byId(b.layers, L).labelDx = 7;     // a drag: an undo step
  b.layers.showLabels = false;       // view: not
  b.restore(snap);
  assert.equal(byId(b.layers, L).labelDx, 3);
  assert.equal(b.layers.showLabels, false);
});

// --- layers in a stamp (layers.js tagLayers / resolveLayers) ---------------

import { tagLayers, resolveLayers, layerKey } from "../layers.js";

function tableOf(...layers) {
  const t = defaultLayers();
  for (const [name, colour] of layers) {
    const id = addLayer(t);
    const l = t.list.find(x => x.id === id);
    l.name = name; l.colour = colour;
  }
  return t;
}

test("a stamp records its layers by name and colour; Background and unknown ids as null", () => {
  const t = tableOf(["RAM", "#112233"], ["ALU", "#445566"]);
  const ram = t.list[1].id, alu = t.list[2].id;
  const out = tagLayers(t, [{ layer: ram }, { layer: alu }, { layer: BACKGROUND }, { layer: 99 }]);
  assert.deepEqual(out.map(e => e.layerRef), [
    { name: "RAM", colour: "#112233" }, { name: "ALU", colour: "#445566" }, null, null]);
  assert.equal(out[0].layer, ram, "the id is still there, for a cut put back where it was");
});

test("pasting into another save: names merge (existing colour wins), new names become layers", () => {
  const src = tableOf(["RAM", "#112233"], ["Clock", "#abcdef"], ["alu", "#445566"]);
  const entries = tagLayers(src, [
    { layer: src.list[1].id }, { layer: src.list[2].id }, { layer: src.list[3].id },
    { layer: src.list[2].id }, { layer: BACKGROUND }, {},
  ]);
  entries[5].layerRef = undefined;   // a stamp from before layers were recorded

  const dst = tableOf([" ram ", "#ff0000"], ["ALU", "#00ff00"]);
  dst.current = dst.list[2].id;
  const before = dst.list.length;
  const { entries: out, created } = resolveLayers(dst, entries);

  const ram = dst.list.find(l => layerKey(l.name) === "ram");
  const alu = dst.list.find(l => layerKey(l.name) === "alu");
  assert.equal(out[0].layer, ram.id, "RAM merges into ' ram ' (case and end spaces ignored)");
  assert.equal(ram.colour, "#ff0000", "on a merge the save's own colour wins");
  assert.equal(out[2].layer, alu.id); assert.equal(alu.colour, "#00ff00");
  assert.equal(created.length, 1, "only Clock was missing");
  const clock = dst.list.find(l => l.name === "Clock");
  assert.ok(clock && clock.colour === "#abcdef", "a new layer brings the stamp's name and colour");
  assert.equal(out[1].layer, clock.id); assert.equal(out[3].layer, clock.id, "made once, used twice");
  assert.equal(dst.list.length, before + 1);
  assert.equal(out[4].layer, BACKGROUND);
  assert.equal(out[5].layer, undefined, "an untagged entry goes on the current layer");
  // Pasting again makes nothing new.
  assert.equal(resolveLayers(dst, entries).created.length, 0);
});

test("a Board places resolved entries on their layers", () => {
  const board = blank();
  const t = board.layers;
  const entries = resolveLayers(t, [
    { type: "belt", dx: 0, dy: 0, dir: 0, flip: false, exit: false, partner: null, emit: null, seed: null,
      layerRef: { name: "Imported", colour: "#123456" } },
    { type: "belt", dx: 1, dy: 0, dir: 0, flip: false, exit: false, partner: null, emit: null, seed: null, layerRef: null },
  ]).entries;
  assert.equal(board.placeEntries(entries, [[3, 3], [4, 3]]).size, 2);
  const layer = t.list.find(l => l.name === "Imported");
  assert.equal(board.layerOf(board.entityAt(3, 3)), layer.id);
  assert.equal(board.layerOf(board.entityAt(4, 3)), BACKGROUND);
});
