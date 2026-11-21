// Layers: named, nameable groups that partition the board's entities, in the
// spirit of AutoCAD LT's layer system. They are NOT separate grids -- every
// layer shares the one board, so a hidden entity still occupies its cells --
// they are a tag on each entity plus a small table describing the tags.
//
// The tag lives in the entity row, slot F_LAYER (27): the one slot the row
// has spare at the desktop's stride (24..25 are a belt's seed, 26 the
// desktop's sleep flag). In the row it travels with everything a row already
// travels with -- snapshots, undo, saves -- for free, and a freshly spawned
// row is zeroed, so anything placed by a tool that knows nothing of layers
// (the desktop, an old save) is on layer 0, Background.
//
// The table ships inside the save as a JSON string under the key "layers",
// like the annotations blob. The desktop ignores the key.
//
// Pure: no DOM, no board. main.js owns the table and applies it.

export const F_LAYER = 27;
export const BACKGROUND = 0;

// What a new layer's colour is drawn from, in turn.
export const PALETTE = [
  "#e8a33d", "#4cc7b6", "#e0524a", "#7d8cf0", "#8fd14f", "#d36bd6",
  "#f0d24a", "#4fa3e0", "#f08a5d", "#5dd39e", "#b48cf0", "#e05a8f",
];

// The largest a label may be, in cells of text height.
export const LABEL_MAX = 12;

function background() {
  return { id: BACKGROUND, name: "Background", colour: null, visible: true, active: true, tint: 0, label: 0 };
}

export function defaultLayers() {
  return { list: [background()], current: BACKGROUND, next: 1 };
}

const isHex = (c) => typeof c === "string" && /^#[0-9a-f]{6}$/i.test(c);
const clamp01 = (v) => Math.min(1, Math.max(0, Number(v) || 0));

// The table from a save's JSON (or anything else): whatever is malformed is
// dropped or defaulted rather than refusing the board -- a layer table is
// never worth losing a factory over.
export function parseLayers(text) {
  const t = defaultLayers();
  let raw;
  try { raw = typeof text === "string" ? JSON.parse(text) : text; } catch { return t; }
  if (!raw || !Array.isArray(raw.list)) return t;
  const seen = new Set([BACKGROUND]);
  for (const l of raw.list) {
    if (!l || !Number.isInteger(l.id) || l.id <= 0 || seen.has(l.id)) {
      // Background keeps its fixed name and has no colour; only its view
      // flags are read back.
      if (l && l.id === BACKGROUND) {
        const bg = t.list[0];
        bg.visible = l.visible !== false;
        bg.active = l.active !== false;
      }
      continue;
    }
    seen.add(l.id);
    t.list.push({
      id: l.id,
      name: String(l.name ?? `Layer ${l.id}`).slice(0, 40) || `Layer ${l.id}`,
      colour: isHex(l.colour) ? l.colour.toLowerCase() : PALETTE[(l.id - 1) % PALETTE.length],
      visible: l.visible !== false,
      active: l.active !== false,
      tint: clamp01(l.tint),
      label: Math.min(LABEL_MAX, Math.max(0, Number(l.label) || 0)),
    });
  }
  const maxId = Math.max(...t.list.map(l => l.id));
  t.next = Math.max(Number.isInteger(raw.next) ? raw.next : 1, maxId + 1);
  t.current = byId(t, raw.current) ? raw.current : BACKGROUND;
  forceCurrent(t);
  return t;
}

export function serialiseLayers(t) {
  return JSON.stringify({ list: t.list, current: t.current, next: t.next });
}

export const byId = (t, id) => t.list.find(l => l.id === id) || null;

// A layer the rows name but the table does not (a table lost to an editor
// that dropped the key, say) gets an entry rather than being folded into
// Background, so the grouping survives even when its name did not.
export function adoptOrphans(t, ids) {
  for (const id of ids) {
    if (!Number.isInteger(id) || id <= 0 || byId(t, id)) continue;
    t.list.push({ id, name: `Layer ${id}`, colour: PALETTE[(id - 1) % PALETTE.length],
                  visible: true, active: true, tint: 0, label: 0 });
    t.next = Math.max(t.next, id + 1);
  }
}

// A new layer at the bottom of the list, named "Layer n" with the lowest n
// no other layer is called. Ids are handles and never reused: deleting a
// layer never renumbers another.
export function addLayer(t) {
  const names = new Set(t.list.map(l => l.name));
  let n = 1;
  while (names.has(`Layer ${n}`)) n++;
  const id = t.next++;
  t.list.push({ id, name: `Layer ${n}`, colour: PALETTE[(t.list.length - 1) % PALETTE.length],
                visible: true, active: true, tint: 0, label: 0 });
  return id;
}

// Remove a layer from the table. Background cannot go. The caller moves its
// entities to Background. If it was current, Background becomes current.
export function removeLayer(t, id) {
  if (id === BACKGROUND) return false;
  const i = t.list.findIndex(l => l.id === id);
  if (i < 0) return false;
  t.list.splice(i, 1);
  if (t.current === id) t.current = BACKGROUND;
  return true;
}

export function renameLayer(t, id, name) {
  const l = byId(t, id);
  name = String(name ?? "").trim().slice(0, 40);
  if (!l || id === BACKGROUND || !name) return false;
  l.name = name;
  return true;
}

// The current layer is always visible and active: making a layer current
// turns both on.
export function setCurrent(t, id) {
  if (!byId(t, id)) return false;
  t.current = id;
  forceCurrent(t);
  return true;
}

function forceCurrent(t) {
  const l = byId(t, t.current);
  if (l) { l.visible = true; l.active = true; }
}

// Is layer `id` drawn? An id the table does not know reads as Background.
export function isVisible(t, id) {
  const l = byId(t, id) || t.list[0];
  return l.id === t.current || l.visible;
}

// Can entities on layer `id` be touched -- selected, inspected for editing,
// built over by force? Hidden implies inactive; the current layer always is.
export function isActive(t, id) {
  const l = byId(t, id) || t.list[0];
  return l.id === t.current || (l.visible && l.active);
}

// The sliders set every active layer at once, each layer keeping its own
// value from then on. Background takes neither a tint nor a label.
export function setForActive(t, key, value) {
  for (const l of t.list) if (l.id !== BACKGROUND && isActive(t, l.id)) l[key] = value;
}

// "#rrggbb" -> [r, g, b] in 0..1.
export function hexRgb(h) {
  const n = parseInt(h.slice(1), 16);
  return [((n >> 16) & 255) / 255, ((n >> 8) & 255) / 255, (n & 255) / 255];
}

// Mix `base` toward `over` by k, both [r, g, b] in 0..1.
export const mix = (base, over, k) => base.map((v, i) => v + (over[i] - v) * k);

// "#rrggbb" or "rgb(r,g,b)" -> [r, g, b] in 0..1, or null.
export function parseCss(c) {
  if (isHex(c)) return hexRgb(c);
  const m = /^rgb\((\d+),\s*(\d+),\s*(\d+)\)$/.exec(c || "");
  return m ? [m[1] / 255, m[2] / 255, m[3] / 255] : null;
}

export const rgbCss = (c) => `rgb(${(c[0] * 255) | 0},${(c[1] * 255) | 0},${(c[2] * 255) | 0})`;

// One pass over the entity table: each layer's member count and the centre
// of mass of its cells (each footprint's centre, weighted by its area), and
// -- when a layer is hidden -- a mask of the cells hidden entities cover,
// so items on hidden belts can be left undrawn.
export function layerStats(board, t) {
  const E = board.T.entity, S = E.STRIDE, ent = board.entities;
  const stats = new Map(t.list.map(l => [l.id, { n: 0, sx: 0, sy: 0, w: 0 }]));
  const anyHidden = t.list.some(l => !isVisible(t, l.id));
  const hidden = anyHidden ? new Uint8Array(board.width * board.height) : null;
  const orphans = new Set();
  for (let r = 1; r < board.rows; r++) {
    const o = r * S;
    if (!ent[o + E.F_ALIVE]) continue;
    const id = ent[o + F_LAYER] | 0;
    let st = stats.get(id);
    if (!st) { orphans.add(id); st = stats.get(BACKGROUND); }
    const x = ent[o + E.F_ORIGIN_X], y = ent[o + E.F_ORIGIN_Y];
    const w = ent[o + E.F_W] || 1, h = ent[o + E.F_H] || 1;
    st.n++;
    st.sx += (x + w / 2) * w * h;
    st.sy += (y + h / 2) * w * h;
    st.w += w * h;
    if (hidden && !isVisible(t, stats.has(id) ? id : BACKGROUND)) {
      for (let dy = 0; dy < h; dy++) hidden.fill(1, (y + dy) * board.width + x, (y + dy) * board.width + x + w);
    }
  }
  const centres = new Map();
  for (const [id, st] of stats) if (st.w) centres.set(id, [st.sx / st.w, st.sy / st.w]);
  return { counts: new Map([...stats].map(([id, st]) => [id, st.n])), centres, hidden, orphans };
}
