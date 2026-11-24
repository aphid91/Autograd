// View presets (viewpresets.js): a presentation view under a name, in
// storage and as a .json file.

import { test } from "node:test";
import assert from "node:assert/strict";
import { VIEW_PRESETS_KEY, viewRecord, readViewPresets, viewPresetNames, putViewPreset, deleteViewPreset,
         viewPresetFile, parseViewPresetFile } from "../viewpresets.js";
import { presetFile } from "../audiopresets.js";

function store() {
  const m = new Map();
  return { getItem: (k) => (m.has(k) ? m.get(k) : null), setItem: (k, v) => m.set(k, String(v)), m };
}

const VIEW = {
  name: "detail",
  view: { grid: true, ports: false, corner: 0.2, background: "#101010" },
  colours: { running: "#00ff00" },
  dev: { beltWidth: 1.2, forceBuild: false },
  layers: [{ id: 0, visible: true, active: true, tint: 0, label: 0 },
           { id: 3, visible: false, active: false, tint: 0.7, label: 2 }],
  showLabels: false,
};

test("save, list, load and delete; the whole view survives, its name does not", () => {
  const st = store();
  assert.equal(putViewPreset(st, "  Close up ", VIEW), "Close up");
  putViewPreset(st, "Area", { ...VIEW, showLabels: true });
  assert.deepEqual(viewPresetNames(st), ["Area", "Close up"]);
  const r = readViewPresets(st)["Close up"];
  const { name, ...rest } = VIEW;
  assert.deepEqual(r, rest);
  assert.ok(!("name" in r));
  deleteViewPreset(st, "Area");
  assert.deepEqual(viewPresetNames(st), ["Close up"]);
  assert.throws(() => putViewPreset(st, "  ", VIEW), /name/);
});

test("stored records are cleaned: bad layers, non-scalar values and junk entries dropped", () => {
  const st = store();
  st.setItem(VIEW_PRESETS_KEY, JSON.stringify({
    x: { view: { grid: true, nested: { a: 1 } }, colours: [1, 2], layers: [{ id: "2" }, { id: 4, tint: "lots", visible: true }, null] },
    "": { view: {} }, y: 5,
  }));
  const all = readViewPresets(st);
  assert.deepEqual(Object.keys(all), ["x"]);
  assert.deepEqual(all.x.view, { grid: true });
  assert.deepEqual(all.x.colours, {});
  assert.deepEqual(all.x.layers, [{ id: 4, visible: true }]);
  assert.equal(all.x.showLabels, true);
  st.setItem(VIEW_PRESETS_KEY, "not json");
  assert.deepEqual(readViewPresets(st), {});
});

test("the file round-trips, and other JSON (an audio preset included) is refused", () => {
  const text = viewPresetFile("Close up", VIEW);
  const got = parseViewPresetFile(text);
  assert.equal(got.name, "Close up");
  assert.deepEqual(got.record, viewRecord(VIEW));
  assert.equal(parseViewPresetFile(viewPresetFile("", VIEW), "from file").name, "from file");
  assert.throws(() => parseViewPresetFile("{"), /JSON/);
  assert.throws(() => parseViewPresetFile(JSON.stringify(VIEW)), /view preset/);
  assert.throws(() => parseViewPresetFile(presetFile("a", { dials: {}, bake: null })), /view preset/);
});
