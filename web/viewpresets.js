// View presets: everything a presentation's view holds (the "i" button's
// standard and detail views) kept under a name, to carry a look from one
// board or one view to another. The same record main.js captureViewState()
// makes and applyViewState() takes:
//
//   { view, colours, dev,            the panel's View, status colours, Dev dials
//     layers: [{ id, name, visible, active, tint, label }],   matched by name on load
//     showLabels }                   the labels' master switch
//
// Kept in this browser and as a tagged .json file, like audio presets
// (audiopresets.js). The name of the view ("default", "detail") is not
// part of it: a preset is a look, and can become either view.
//
// Cleaning here is structural only (objects, a list of layer entries with
// an integer id); applyViewState() already takes a key only when the panel
// knows it and the type matches, and a layer only when the board has one of
// that name -- so a preset made on another board sets the panel and
// whichever layers share a name, and leaves the rest alone.
//
// Pure apart from the storage, which is passed in (localStorage in the app,
// a Map-backed stand-in in the tests).

export const VIEW_PRESETS_KEY = "gates.web.viewPresets.v1";
export const VIEW_PRESET_FORMAT = "gates-view-preset";
const NAME_MAX = 60;

const cleanName = (n) => String(n ?? "").trim().slice(0, NAME_MAX);
const plain = (o) => {
  if (!o || typeof o !== "object" || Array.isArray(o)) return {};
  const out = {};
  for (const [k, v] of Object.entries(o)) if (["number", "boolean", "string"].includes(typeof v)) out[k] = v;
  return out;
};

function cleanLayer(l) {
  if (!l || typeof l !== "object" || !Number.isInteger(l.id)) return null;
  const out = { id: l.id };
  if (typeof l.name === "string") out.name = l.name;
  if (typeof l.visible === "boolean") out.visible = l.visible;
  if (typeof l.active === "boolean") out.active = l.active;
  if (Number.isFinite(l.tint)) out.tint = l.tint;
  if (Number.isFinite(l.label)) out.label = l.label;
  return out;
}

// A view state as a preset stores it. `name` is dropped (see above).
export function viewRecord(v) {
  const s = v && typeof v === "object" ? v : {};
  return {
    view: plain(s.view), colours: plain(s.colours), dev: plain(s.dev),
    layers: Array.isArray(s.layers) ? s.layers.map(cleanLayer).filter(Boolean) : [],
    showLabels: s.showLabels !== false,
  };
}

// { name: record } from storage. Anything malformed is dropped.
export function readViewPresets(store) {
  let raw;
  try { raw = JSON.parse(store.getItem(VIEW_PRESETS_KEY) || "{}"); } catch { return {}; }
  const out = {};
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return out;
  for (const [name, r] of Object.entries(raw)) {
    const n = cleanName(name);
    if (n && r && typeof r === "object") out[n] = viewRecord(r);
  }
  return out;
}

export const viewPresetNames = (store) => Object.keys(readViewPresets(store)).sort((a, b) => a.localeCompare(b));

// Store the view state `v` under `name` (replacing one of that name).
// Throws a readable message when the browser refuses.
export function putViewPreset(store, name, v) {
  const n = cleanName(name);
  if (!n) throw new Error("a preset needs a name");
  const all = readViewPresets(store);
  all[n] = viewRecord(v);
  try { store.setItem(VIEW_PRESETS_KEY, JSON.stringify(all)); }
  catch (err) { throw new Error(`the browser would not store it (${err.message})`); }
  return n;
}

export function deleteViewPreset(store, name) {
  const all = readViewPresets(store);
  if (!(name in all)) return;
  delete all[name];
  store.setItem(VIEW_PRESETS_KEY, JSON.stringify(all));
}

// The .json file: tagged, so importing an audio preset (or any other JSON)
// is refused rather than read as an empty look.
export function viewPresetFile(name, record) {
  return JSON.stringify({ format: VIEW_PRESET_FORMAT, version: 1, name: cleanName(name), ...viewRecord(record) }, null, 2);
}

// A file's text back into { name, record }. Throws a readable message.
export function parseViewPresetFile(text, fallbackName = "Imported") {
  let v;
  try { v = JSON.parse(text); } catch { throw new Error("not a JSON file"); }
  if (!v || typeof v !== "object" || v.format !== VIEW_PRESET_FORMAT) throw new Error("not a view preset file");
  return { name: cleanName(v.name) || cleanName(fallbackName) || "Imported", record: viewRecord(v) };
}
