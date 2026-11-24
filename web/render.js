// Canvas 2D renderer. Draws in world units under one transform, so a cell is
// a 1x1 square and every path for a whole family of things is built once and
// stroked once. Only what is on screen is added to a path.
//
// Colours follow the TUNABLES block in shaders/world.frag, loosely: the point
// is that the two apps read as the same game, not that they match pixels.

const COL = {
  void: "#0b0c0f",
  bg: "#1f1f24",
  grid: "#383842",
  gridMajor: "#4d4d59",
  border: "#737385",
  belt: "#757885",
  chevron: "#bdc2d1",
  under: "#6b7380",
  underDark: "#3c414b",
  underDot: "#9ea8c2",
  in: "#5999f2",
  out: "#f2594d",
  seedPos: "rgba(77,189,92,0.55)",
  seedNeg: "rgba(209,77,71,0.55)",
  label: "#fcfcff",
  select: "#e8a33d",
  handle: "rgba(232,163,61,0.28)",
  frustrated: "rgba(230,64,56,0.55)",
  lifted: "#5fd3e6",
  liftedFill: "rgba(95,211,230,0.12)",
  // A thin, translucent edge: enough to separate an arrow from a body of a
  // similar colour, not so much that zoomed out the ports are all you see.
  portEdge: "rgba(0,0,0,0.5)",
  mark: "#e0e6f0",            // the glyph: COL_STRUCT_MARK
  pass: "#757885",            // the control overlay: belt-coloured, belt-wide,
  passEdge: "#000000",        // with a thin dark edge so it reads over any body
  passChevron: "#bdc2d1",
  // Items, from world.frag's COL_ITEM_*: a dark disc with a ring that says
  // the sign -- green above zero, red at or below.
  vector: "#4cc7b6",           // the vector picker (style.css --vec)
  itemFill: "rgb(36,38,46)",
  itemPos: "rgb(97,209,107)",
  itemNeg: "rgb(230,92,87)",
  itemNum: "rgb(247,250,255)",
  itemHalo: "rgba(15,18,23,0.85)",
};

// The colours a layer tint blends from (main.js), where nothing else has
// recoloured the entity.
export const BASE_COLOURS = { belt: COL.belt, under: COL.under };

// world.frag's ITEM_RADIUS / ITEM_RING.
const ITEM = { r: 0.22, ring: 0.055 };
// An item's number, as a fraction of a cell, and the zoom band it fades in
// over -- the shader's glyph height (8 * 0.042 cells) and its 6..10 px fade.
const ITEM_TEXT = { size: 0.3, glyph: 0.336 };   // its fade band: TUNE.itemTextMin..itemTextGood

// Below this zoom (CSS px per cell) detail is not drawn at all.
// Live from the Dev folder's zoom thresholds (tune.js).
//
// Dev > override zoom, above 0, decides the detail as if the zoom were that
// value, at any real zoom. Every threshold is compared against the real zoom
// `z` (which also sets sizes, so it cannot simply be swapped), so the
// override rescales each threshold instead: z >= t * z / o exactly when
// o >= t. `lodZ` is the zoom of the render in progress, set by render().
let lodZ = 1;
const lod = (t) => (TUNE.lodZoom > 0 ? t * lodZ / TUNE.lodZoom : t);
const LOD = {
  get beltSolid() { return lod(TUNE.lodBeltSolid); },
  get gridLines() { return lod(TUNE.lodGrid); },
  get chevrons() { return lod(TUNE.lodChevrons); },
  get ports() { return lod(TUNE.lodPorts); },
  get marks() { return lod(TUNE.lodMarks); },
  get portEdge() { return lod(TUNE.lodPortEdge); },
};

// A Source's number, in CSS px: 1.75x the first cut, bounded by the body's
// width. It is drawn as far out as the glyphs are (LOD.marks), however small
// that makes it -- a value still reads as "a number, roughly this long" well
// after its digits stop being legible. Below that nothing but bodies draws.
const EMIT_SCALE = 0.56;
const OVERLAY_WIDTH = 0.28;
function emitSize(s, z) {
  const text = fmtNumber(s.emit);
  return TUNE.emitSize * Math.min(EMIT_SCALE * z, (s.w * z * 0.8) / (text.length * 0.62));
}
const emitShown = (s, z, labels) => s.emit !== null && labels && z >= LOD.marks;

import { structGlyph } from "./glyphs.js";
import { wrapLabel } from "./layers.js";
import { TUNE } from "./tune.js";
import { itemPoints } from "./flow.js";
import { drawPixels } from "./pixel.js";
import { css as noteCss, isText, sizeOf, wrap, PAD, LINE } from "./notes.js";

const css = (c) => `rgb(${(c[0] * 255) | 0},${(c[1] * 255) | 0},${(c[2] * 255) | 0})`;
const shade = (c, k) => css(c.map(v => v * k));

// A whole number prints in full only up to 2^24, the last point where float32
// integers are contiguous; past it the trailing digits are rounding noise, so
// it goes to scientific notation like any other large value. (Every float32
// past 2^23 is whole, so without the bound the style depended on whether a
// value happened to have a fraction rather than on how big it was.)
const FULL_INT_LIMIT = 2 ** 24;

export function fmtNumber(v) {
  if (Number.isInteger(v) && Math.abs(v) <= FULL_INT_LIMIT) return String(v);
  if (Math.abs(v) >= 1e4 || Math.abs(v) < 1e-3) return v.toExponential(1);
  return String(parseFloat(v.toPrecision(3)));
}

// `opts`: the View toggles ({ grid, ports, labels }) and `selected`, the
// inspected entity's {x, y, w, h} or null.
export const DEFAULT_VIEW = { grid: true, ports: true, labels: true, seedTint: true };

// View > look: the board's colour, how round a machine's corners are (in
// cells, 0 square, 0.5 as round as a 1-wide body allows) and how opaque the
// chevrons are, on belts, underground ends and the control overlays alike.
// Set once per render, read by the drawing helpers.
const LOOK = { corner: 0, chevrons: 1 };

// Stroke the current path (or `path`) with the chevrons' opacity.
function strokeChevrons(ctx, path) {
  if (LOOK.chevrons <= 0) return;
  const a = ctx.globalAlpha;
  ctx.globalAlpha = a * LOOK.chevrons;
  if (path) ctx.stroke(path); else ctx.stroke();
  ctx.globalAlpha = a;
}

// A machine body: a rectangle, rounded by View > corner radius.
function bodyRect(ctx, x, y, w, h) {
  const r = Math.min(LOOK.corner, w / 2, h / 2);
  if (r > 0) ctx.roundRect(x, y, w, h, r); else ctx.rect(x, y, w, h);
}

export function render(ctx, board, cam, dpr, opts = {}) {
  const view = { ...DEFAULT_VIEW, ...opts };
  const z = cam.zoom;
  lodZ = z;
  LOOK.corner = Math.max(0, +view.corner || 0);
  LOOK.chevrons = view.chevronAlpha ?? 1;
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.fillStyle = COL.void;
  ctx.fillRect(0, 0, ctx.canvas.width, ctx.canvas.height);
  if (!board) return;

  // World transform: x right, y up, one unit per cell.
  ctx.setTransform(dpr * z, 0, 0, -dpr * z,
                   dpr * (cam.w / 2 - cam.cx * z), dpr * (cam.h / 2 + cam.cy * z));
  const world = ctx.getTransform();
  const px = 1 / z;   // one CSS pixel, in world units
  const v = cam.visible(1);
  const on = (x, y, w = 1, h = 1) => x + w >= v.x0 && x <= v.x1 && y + h >= v.y0 && y <= v.y1;
  const S = board.T.cell.DIR_STEPS;

  ctx.fillStyle = view.bg || COL.bg;
  ctx.fillRect(0, 0, board.width, board.height);

  // Grid, under everything, so belts read as joined across cell edges.
  if (view.grid && z >= LOD.gridLines) {
    const gx0 = Math.max(0, Math.floor(v.x0)), gx1 = Math.min(board.width, Math.ceil(v.x1));
    const gy0 = Math.max(0, Math.floor(v.y0)), gy1 = Math.min(board.height, Math.ceil(v.y1));
    for (const major of [false, true]) {
      ctx.beginPath();
      for (let x = gx0; x <= gx1; x++) {
        if ((x % 8 === 0) !== major) continue;
        ctx.moveTo(x, gy0); ctx.lineTo(x, gy1);
      }
      for (let y = gy0; y <= gy1; y++) {
        if ((y % 8 === 0) !== major) continue;
        ctx.moveTo(gx0, y); ctx.lineTo(gx1, y);
      }
      ctx.strokeStyle = major ? COL.gridMajor : COL.grid;
      ctx.lineWidth = px;
      ctx.globalAlpha = TUNE.gridOpacity;
      ctx.stroke();
      ctx.globalAlpha = 1;
    }
  }

  // Placed belts a run (or its copies) will turn draw only as the ghost.
  const hide = view.hide?.length ? new Set(view.hide.map(([x, y]) => `${x},${y}`)) : null;
  // View > belts off hides placed belts and underground ends (ghosts of a
  // run being planned still draw).
  // `lists`: the board's own lists with hidden layers' entities left out
  // (main.js filters them once per change, not per frame).
  const L = view.lists || board;
  if (view.belts !== false) {
    drawBelts(ctx, hide ? L.belts.filter(b => !hide.has(`${b.x},${b.y}`)) : L.belts, z, on, S, view.seedTint, view.beltTint || null);
    drawUnders(ctx, L.unders, z, on, S, view.underTint || view.beltTint || null);
  }
  // View > sources off: Sources are not drawn (there are hundreds on a
  // computer, and they hide the machines worth looking at). Still there.
  // View > gates off hides every machine, Sources included -- for a look at
  // the belts underneath. Still there, and still tappable.
  const structs = view.gates === false ? []
    : view.sources === false ? L.structs.filter(s => board.T.types[s.type].name !== "source") : L.structs;
  drawStructs(ctx, board.T, structs, z, px, on, S, view.ports, view.labels, view.gateTint || null);

  ctx.strokeStyle = COL.border;
  ctx.lineWidth = 2 * px;
  ctx.strokeRect(0, 0, board.width, board.height);

  // The planning ghosts go under the items -- or, while the pixel overlay
  // is on, over it, so what is being placed stays visible on the screen.
  const ghosts = (view.ghosts || [view.ghost]).filter(Boolean);
  const drawGhosts = () => { for (const g of ghosts) drawGhost(ctx, board.T, g, z, px, on, S, view.ports, view.labels, view.seedTint); };
  if (!view.pixels) drawGhosts();

  // Items last among the board's contents: cargo on a cell, over any tint,
  // so a red wash never changes the colour that says an item's sign.
  let cargo = null;
  if (view.sim && view.items !== false) cargo = drawItems(ctx, view.sim, view.alpha || 0, z, on, S, view.hiddenCells, board.width, view.dim?.cells);

  // Layers: what an inactive layer holds is washed out -- colour drained,
  // then darkened -- body, glyph, ports and items alike, so what can be
  // touched stands out. Over the items, under the editing marks.
  if (view.dim) drawDim(ctx, view.dim.rects, on, world);

  // The pixel overlay (pixel.js): over the board and its items.
  if (view.pixels) {
    drawPixels(ctx, board, view.pixels.screen, view.pixels.alpha, view.pixels.max, on, view.pixels.textBg);
    drawGhosts();
  }

  // Everything so far is the board itself; what follows is editing marks
  // and screen-space text. The bloom (bloom.js) takes the picture here, so
  // a selection or a frame box never glows, and lays its glow over it.
  if (view.onWorld) {
    ctx.save();
    view.onWorld(ctx);
    ctx.restore();
  }

  // The video frame (Video folder open): the crop box now, solid, and the
  // end camera's, dashed. Screen-pixel lines, over everything on the board.
  if (view.frameBoxes) {
    ctx.lineWidth = 2 * px;
    for (const b of view.frameBoxes) {
      ctx.setLineDash(b.dashed ? [8 * px, 6 * px] : []);
      ctx.strokeStyle = b.dashed ? "rgba(255,200,80,0.9)" : "rgba(255,255,255,0.85)";
      ctx.strokeRect(b.cx - b.hw, b.cy - b.hh, 2 * b.hw, 2 * b.hh);
    }
    ctx.setLineDash([]);
  }

  // The audio's box (Audio > Set audio region, or camera-aware audio): the
  // box itself dashed -- fainter for the camera's, which is the screen --
  // and, with a soft edge, the fringe's outer limit, rounded since distance
  // is Euclidean. Screen-pixel lines. An editing mark: never in a video.
  if (view.soundSpace) {
    const { rect: r, fringe: D, camera } = view.soundSpace;
    ctx.lineWidth = (camera ? 1.5 : 2) * px;
    ctx.setLineDash([6 * px, 4 * px]);
    ctx.strokeStyle = camera ? "rgba(95,211,230,0.55)" : "rgba(95,211,230,0.9)";
    ctx.strokeRect(r.x, r.y, r.w, r.h);
    if (D > 0) {
      ctx.setLineDash([2 * px, 5 * px]);
      ctx.strokeStyle = "rgba(95,211,230,0.35)";
      ctx.beginPath();
      ctx.roundRect(r.x - D, r.y - D, r.w + 2 * D, r.h + 2 * D, D);
      ctx.stroke();
    }
    ctx.setLineDash([]);
  }

  // The vector picker's highlights, over the ghosts they move.
  if (view.vector) drawVectorMarks(ctx, view.vector, px);

  // The inspected entity: an outline that stays a few pixels wide at any
  // zoom, drawn over everything so it cannot be hidden by what it marks.
  const sel = view.selected;
  if (sel) {
    const pad = 3 * px;
    ctx.strokeStyle = COL.select;
    ctx.lineWidth = 2.5 * px;
    ctx.strokeRect(sel.x - pad, sel.y - pad, sel.w + 2 * pad, sel.h + 2 * pad);
  }

  if (cargo) drawItemLabels(ctx, cargo, cam, dpr);
  if (view.notes !== false && board.notes?.length) drawNotes(ctx, board.notes, cam, dpr, view.noteSel ?? -1, !!view.noteGrips);
  // Labels lifted into a selection, or about to be pasted: ghosted, and a
  // selected one outlined as a lifted entity is.
  for (const g of view.ghostNotes || []) {
    ctx.save();
    ctx.globalAlpha = TUNE.ghostAlpha;
    drawNotes(ctx, g.notes, cam, dpr, -1);
    ctx.restore();
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.strokeStyle = g.lifted ? COL.lifted : COL.select;
    ctx.lineWidth = 2;
    if (!g.lifted) ctx.setLineDash([6, 4]);
    for (const n of g.notes) {
      const [x, y, w, h] = n.rect;
      const [sx0, sy0] = cam.worldToScreen(x, y);
      const [sx1, sy1] = cam.worldToScreen(x + w, y - h);
      ctx.strokeRect(sx0 - 2, sy0 - 2, sx1 - sx0 + 4, sy1 - sy0 + 4);
    }
    ctx.setLineDash([]);
  }
  if (view.indexLabels?.length) drawIndexLabels(ctx, view.indexLabels, cam, dpr);

  if (view.labels && z >= LOD.marks) {
    drawLabels(ctx, board.T, structs, cam, dpr, on, view.dim?.rows);
    ctx.globalAlpha = 0.7;
    for (const g of ghosts) if (g.structs?.length) drawLabels(ctx, board.T, g.structs, cam, dpr, on);
    ctx.globalAlpha = 1;
  }
  // Layer labels last of all, over the Source values too: they name whole
  // modules, and a value showing through one makes both unreadable.
  if (view.layerLabels?.length) drawLayerLabels(ctx, view.layerLabels, cam, dpr, view.layerLabelHits, view.layerLabelSel);
}

// `tint`: View > belt status colours -- (x, y) -> a colour, or null for the
// belt's own (main.js decides empty / stalled / moving, per run).
function drawBelts(ctx, belts, z, on, S, seedTint = true, tint = null) {
  // One batched path per colour, as before when there is one colour.
  const groups = new Map();
  for (const b of belts) {
    const c = tint?.(b.x, b.y) || COL.belt;
    if (!groups.has(c)) groups.set(c, []);
    groups.get(c).push(b);
  }

  // Seeded cells are tinted by the seed's sign, under the belt itself --
  // unless the View folder's "seed tint" is off.
  for (const sign of seedTint ? [1, -1] : []) {
    ctx.beginPath();
    for (const b of belts) if (b.seed === sign && on(b.x, b.y)) ctx.rect(b.x, b.y, 1, 1);
    ctx.fillStyle = sign > 0 ? COL.seedPos : COL.seedNeg;
    ctx.fill();
  }

  if (z < LOD.beltSolid) {
    // Zoomed far out a belt is a few pixels; a solid square reads better
    // than a hairline and costs one rect.
    for (const [col, list] of groups) {
      ctx.beginPath();
      for (const b of list) if (on(b.x, b.y)) ctx.rect(b.x + 0.2, b.y + 0.2, 0.6, 0.6);
      ctx.fillStyle = col;
      ctx.fill();
    }
    return;
  }

  // The spine: in-edge -> centre -> out-edge. With no source the spine runs
  // straight through, so an isolated belt fills its cell.
  ctx.lineWidth = 0.34 * TUNE.beltWidth;
  ctx.lineJoin = "round";
  ctx.lineCap = "butt";
  for (const [col, list] of groups) {
    ctx.beginPath();
    for (const b of list) {
      if (!on(b.x, b.y)) continue;
      const cx = b.x + 0.5, cy = b.y + 0.5;
      const o = S[b.dir];
      const i = b.inDir >= 0 ? S[b.inDir] : [-o[0], -o[1]];
      ctx.moveTo(cx + i[0] * 0.5, cy + i[1] * 0.5);
      ctx.lineTo(cx, cy);
      ctx.lineTo(cx + o[0] * 0.5, cy + o[1] * 0.5);
    }
    ctx.strokeStyle = col;
    ctx.stroke();
  }

  if (z < LOD.chevrons) return;
  ctx.beginPath();
  for (const b of belts) {
    if (!on(b.x, b.y)) continue;
    chevron(ctx, b.x + 0.5, b.y + 0.5, S[b.dir], 0.1 * TUNE.chevronSize);
  }
  ctx.strokeStyle = COL.chevron;
  ctx.lineWidth = 0.06;
  ctx.lineJoin = "miter";
  strokeChevrons(ctx);
}

// A ">" pointing along f, centred near (cx, cy).
function chevron(ctx, cx, cy, f, s) {
  const sx = -f[1], sy = f[0];
  ctx.moveTo(cx - f[0] * s + sx * s, cy - f[1] * s + sy * s);
  ctx.lineTo(cx + f[0] * s * 0.6, cy + f[1] * s * 0.6);
  ctx.lineTo(cx - f[0] * s - sx * s, cy - f[1] * s - sy * s);
}

// `tint`: View > belt status colours, as for belts -- an end and its span
// take their run's colour (the span in a lighter wash of it).
function drawUnders(ctx, unders, z, on, S, tint = null) {
  const colOf = (u) => tint?.(u.x, u.y) || null;
  const groups = new Map();
  for (const u of unders) {
    const c = colOf(u);
    if (!groups.has(c)) groups.set(c, []);
    groups.get(c).push(u);
  }
  for (const [c, list] of groups) drawUnderGroup(ctx, list, on, S, c);

  if (z < LOD.chevrons) return;
  ctx.beginPath();
  for (const u of unders) {
    if (on(u.x, u.y)) chevron(ctx, u.x + 0.5, u.y + 0.5, S[u.dir], 0.12);
  }
  ctx.strokeStyle = COL.chevron;
  ctx.lineWidth = 0.07;
  strokeChevrons(ctx);
}

// One colour's worth of underground ends and spans; `col` null is their own.
function drawUnderGroup(ctx, unders, on, S, col) {
  // The span: dots on the buried cells between a paired entrance and exit.
  ctx.beginPath();
  for (const u of unders) {
    if (!u.span) continue;
    const [ex, ey] = u.span;
    const n = Math.abs(ex - u.x) + Math.abs(ey - u.y);
    const f = S[u.dir];
    for (let k = 1; k < n; k++) {
      const x = u.x + f[0] * k, y = u.y + f[1] * k;
      if (!on(x, y)) continue;
      ctx.moveTo(x + 0.5 + 0.07, y + 0.5);
      ctx.arc(x + 0.5, y + 0.5, 0.07, 0, Math.PI * 2);
    }
  }
  ctx.fillStyle = col || COL.underDot;
  ctx.globalAlpha = col ? 0.7 : 1;
  ctx.fill();
  ctx.globalAlpha = 1;

  // Each end: a funnel, full width on the surface side and narrowing toward
  // the buried side -- the front of an entrance (diving), the back of an
  // exit (surfacing) -- with a dark bar across the narrow mouth.
  ctx.beginPath();
  const bars = new Path2D();
  for (const u of unders) {
    if (!on(u.x, u.y)) continue;
    underFunnel(ctx, bars, u.x + 0.5, u.y + 0.5, S[u.dir], u.exit ? -1 : 1);
  }
  ctx.fillStyle = col || COL.under;
  ctx.fill();
  ctx.fillStyle = COL.underDark;
  ctx.fill(bars);
}

// One underground end centred on (cx, cy). `f` is the travel direction and
// `s` +1 for an entrance, -1 for an exit, so f*s points at the buried side.
// The swatch art in tools.js draws the same shape.
const FUNNEL = { half: 0.38, wide: 0.38, narrow: 0.2, bar: 0.08 };
function underFunnel(path, bars, cx, cy, f, s) {
  const { half, wide, narrow, bar } = FUNNEL;
  const ax = f[0] * s, ay = f[1] * s;   // toward the buried side
  const sx = -ay, sy = ax;              // across
  const pt = (along, across) => [cx + ax * along + sx * across, cy + ay * along + sy * across];
  const q = [pt(-half, wide), pt(half, narrow), pt(half, -narrow), pt(-half, -wide)];
  path.moveTo(...q[0]);
  for (const p of q.slice(1)) path.lineTo(...p);
  path.closePath();
  const b = [pt(half - 2 * bar, narrow + 0.03), pt(half, narrow), pt(half, -narrow), pt(half - 2 * bar, -narrow - 0.03)];
  bars.moveTo(...b[0]);
  for (const p of b.slice(1)) bars.lineTo(...p);
  bars.closePath();
}

// A planned run: the proposed belts and undergrounds translucent, cells the
// run cannot have tinted red, and a ring on each draggable end -- the tip
// solid, the start hollow, so the two read apart at a glance.
function drawGhost(ctx, T, g, z, px, on, S, ports, labels, seedTint = true) {
  ctx.save();
  ctx.globalAlpha = TUNE.ghostAlpha;
  // Seeded ghost belts tint like real ones; drawBelts does that itself.
  drawBelts(ctx, g.belts || [], Math.max(z, LOD.chevrons), on, S, seedTint);
  drawUnders(ctx, g.unders || [], Math.max(z, LOD.chevrons), on, S);
  if (g.structs) drawStructs(ctx, T, g.structs, Math.max(z, LOD.ports), px, on, S, ports, labels);
  ctx.restore();

  ctx.beginPath();
  for (const c of g.blocked || []) if (on(c.x, c.y)) ctx.rect(c.x, c.y, 1, 1);
  ctx.fillStyle = COL.frustrated;
  ctx.fill();

  const ring = (c, filled) => {
    const r = Math.max(0.42, 14 * px);
    ctx.beginPath();
    ctx.arc(c[0] + 0.5, c[1] + 0.5, r, 0, Math.PI * 2);
    ctx.lineWidth = Math.max(0.06, 2.5 * px);
    ctx.strokeStyle = COL.select;
    ctx.stroke();
    if (filled) { ctx.fillStyle = COL.handle; ctx.fill(); }
  };
  if (g.start) ring(g.start, false);
  if (g.end) ring(g.end, true);
  // A placement ghost: an outline round the footprint, so the tap target
  // reads as one thing even when part of it is tinted red.
  if (g.box) {
    ctx.strokeStyle = COL.select;
    ctx.lineWidth = Math.max(0.05, 2 * px);
    ctx.setLineDash([6 * px, 4 * px]);
    ctx.strokeRect(g.box.x, g.box.y, g.box.w, g.box.h);
    ctx.setLineDash([]);
  }
  // A selection: each lifted entity outlined, so it reads as picked up
  // rather than as a plan.
  if (g.outlines?.length) {
    const pad = 1.5 * px;
    ctx.beginPath();
    for (const b of g.outlines) if (on(b.x, b.y, b.w, b.h)) bodyRect(ctx, b.x + pad, b.y + pad, b.w - 2 * pad, b.h - 2 * pad);
    ctx.fillStyle = COL.liftedFill;
    ctx.fill();
    ctx.strokeStyle = COL.lifted;
    ctx.lineWidth = Math.max(0.04, 2 * px);
    ctx.stroke();
  }
  // A box selection being dragged out: the cells it will touch.
  if (g.rect) {
    const r = g.rect;
    ctx.fillStyle = COL.liftedFill;
    ctx.fillRect(r.x, r.y, r.w, r.h);
    ctx.strokeStyle = COL.lifted;
    ctx.lineWidth = Math.max(0.05, 2 * px);
    ctx.setLineDash([6 * px, 4 * px]);
    ctx.strokeRect(r.x, r.y, r.w, r.h);
    ctx.setLineDash([]);
  }
}

// `tint`: View > gate status colours -- a Map of row to colour that
// replaces the body colour (main.js decides starved / blocked / running).
function drawStructs(ctx, T, structs, z, px, on, S, ports, labels, tint = null) {
  const types = T.types;
  const inset = 0.05;
  for (const s of structs) {
    if (!on(s.x, s.y, s.w, s.h)) continue;
    const c = (tint && s.row !== undefined && tint.get(s.row)) || types[s.type].colour || [0.5, 0.5, 0.5];
    ctx.fillStyle = css(c);
    ctx.strokeStyle = shade(c, 0.6);
    ctx.lineWidth = Math.max(px, 0.04);
    if (LOOK.corner > 0) {
      ctx.beginPath();
      bodyRect(ctx, s.x + inset, s.y + inset, s.w - 2 * inset, s.h - 2 * inset);
      ctx.fill();
      ctx.stroke();
    } else {
      ctx.fillRect(s.x + inset, s.y + inset, s.w - 2 * inset, s.h - 2 * inset);
      ctx.strokeRect(s.x + inset, s.y + inset, s.w - 2 * inset, s.h - 2 * inset);
    }
  }
  if (z < LOD.marks) return;

  // The control overlay: a faint belt straight through each cell a channel
  // passes unchanged (Entity.PASS_THROUGH), so which signal survives the
  // machine reads at a glance. Over the body, under the glyph. Which cells
  // carry it is exported per orientation (`o.pass`), worked out in Python
  // by the same rule the desktop shader applies.
  const spine = new Path2D(), chev = new Path2D();
  for (const s of structs) {
    if (!on(s.x, s.y, s.w, s.h)) continue;
    for (const [lx, ly, a] of s.o.pass) {
      const f = S[a], cx = s.x + lx + 0.5, cy = s.y + ly + 0.5;
      spine.moveTo(cx - f[0] * 0.5, cy - f[1] * 0.5);
      spine.lineTo(cx + f[0] * 0.5, cy + f[1] * 0.5);
      if (z >= LOD.chevrons) chevron(chev, cx, cy, f, 0.1 * TUNE.chevronSize);
    }
  }
  // 0.28 cells: between the desktop's faint 0.22 and a belt's 0.34, over a
  // slightly wider dark stroke that shows as an edge down each side. Butt
  // caps, so the edge does not close the ends: the channel reads as
  // continuing into the belts beyond.
  const ow = OVERLAY_WIDTH * TUNE.overlayWidth;
  ctx.lineCap = "butt";
  // The edge scales with the board (0.03 cells) down to half a pixel, so
  // zoomed out it fades rather than striping every machine.
  if (TUNE.overlayEdge > 0) {
    ctx.strokeStyle = COL.passEdge;
    ctx.lineWidth = ow + 2 * TUNE.overlayEdge * Math.max(0.5 * px, 0.03);
    ctx.stroke(spine);
  }
  ctx.strokeStyle = COL.pass;
  ctx.lineWidth = ow;
  ctx.stroke(spine);
  ctx.strokeStyle = COL.passChevron;
  ctx.lineWidth = 0.06;
  ctx.lineJoin = "miter";
  strokeChevrons(ctx, chev);

  // The glyph, centred on the footprint. A Source whose number is shown
  // wears the number instead.
  let fills = "";
  const widths = new Map();   // stroke width -> path data: footprints differ
  for (const s of structs) {
    if (!on(s.x, s.y, s.w, s.h)) continue;
    if (emitShown(s, z, labels)) continue;
    const g = structGlyph(types[s.type].glyph, s.x, s.y, s.w, s.h, TUNE.glyphScale, TUNE.glyphWeight);
    if (g.stroke) widths.set(g.width, (widths.get(g.width) || "") + g.stroke);
    fills += g.fill;
  }
  ctx.strokeStyle = COL.mark;
  ctx.fillStyle = COL.mark;
  ctx.lineCap = "round";
  ctx.lineJoin = "round";
  for (const [w, d] of widths) { ctx.lineWidth = w; ctx.stroke(new Path2D(d)); }
  if (fills) ctx.fill(new Path2D(fills));
  ctx.lineCap = "butt";

  if (!ports || z < LOD.ports) return;
  // Ports: blue triangles pointing in at inputs, red pointing out at outputs.
  const ins = new Path2D(), outs = new Path2D();
  for (const s of structs) {
    if (!on(s.x, s.y, s.w, s.h)) continue;
    for (const [lx, ly, f] of s.o.inputs) port(ins, s.x + lx, s.y + ly, S[f], true);
    for (const [lx, ly, f] of s.o.outputs) port(outs, s.x + lx, s.y + ly, S[f], false);
  }
  ctx.fillStyle = COL.in; ctx.fill(ins);
  ctx.fillStyle = COL.out; ctx.fill(outs);
  // The edge only once the arrows are big enough to need one; zoomed out it
  // would be most of what is drawn.
  if (z >= LOD.portEdge && TUNE.portEdge > 0) {
    ctx.strokeStyle = COL.portEdge;
    ctx.lineWidth = 0.75 * px * TUNE.portEdge;
    ctx.lineJoin = "miter";
    ctx.stroke(ins);
    ctx.stroke(outs);
  }
}

function port(path, x, y, f, inward) {
  const cx = x + 0.5, cy = y + 0.5;
  const ex = cx + f[0] * 0.45, ey = cy + f[1] * 0.45;   // just inside the face
  // Smaller again now the control overlay helps orient a machine: 35% under
  // the enlarged cut (0.169 / 0.208). The tip sits on the face, not past it.
  const k = TUNE.portScale;
  const sx = -f[1] * 0.11 * k, sy = f[0] * 0.11 * k;
  const d = 0.135 * k;
  // Inward: base on the face, tip inside. Outward: base inside, tip on face.
  const tip = inward ? [ex - f[0] * d, ey - f[1] * d] : [ex, ey];
  const base = inward ? [ex, ey] : [ex - f[0] * d, ey - f[1] * d];
  path.moveTo(base[0] + sx, base[1] + sy);
  path.lineTo(tip[0], tip[1]);
  path.lineTo(base[0] - sx, base[1] - sy);
  path.closePath();
}

// Text is drawn in screen space: under the world transform it would be
// mirrored by the y flip.
// `dimRows`: inactive layers' machines, whose values fade with their washout.
function drawLabels(ctx, T, structs, cam, dpr, on, dimRows = null) {
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  ctx.fillStyle = COL.label;
  const z = cam.zoom, a0 = ctx.globalAlpha;
  for (const s of structs) {
    if (!on(s.x, s.y, s.w, s.h) || !emitShown(s, z, true)) continue;
    const size = emitSize(s, z);
    ctx.font = `600 ${size.toFixed(1)}px system-ui, sans-serif`;
    const [sx, sy] = cam.worldToScreen(s.x + s.w / 2, s.y + s.h / 2);
    ctx.globalAlpha = dimRows?.has(s.row) ? a0 * DIM_TEXT : a0;
    ctx.fillText(fmtNumber(s.emit), sx, sy);
  }
  ctx.globalAlpha = a0;
}

// The inactive washout: the cells' colour drained ("saturation" with a grey
// takes the hue and chroma out of what is beneath) and then darkened toward
// the board's background. One path, culled to the view; under the world
// transform, which `world` restores since text passes leave screen space.
const DIM = { desat: 0.9, dark: "rgba(24,24,29,0.6)" };
const DIM_TEXT = 0.3;   // what text over a washed-out cell keeps of its alpha
function drawDim(ctx, rects, on, world) {
  ctx.save();
  ctx.setTransform(world);
  ctx.beginPath();
  let any = false;
  for (const r of rects) if (on(r[0], r[1], r[2], r[3])) { ctx.rect(r[0], r[1], r[2], r[3]); any = true; }
  if (any) {
    ctx.globalCompositeOperation = "saturation";
    ctx.globalAlpha = DIM.desat;
    ctx.fillStyle = "#808080";
    ctx.fill();
    ctx.globalCompositeOperation = "source-over";
    ctx.globalAlpha = 1;
    ctx.fillStyle = DIM.dark;
    ctx.fill();
  }
  ctx.restore();
}

// Labels (notes.js), as the desktop's annotation layer draws them: a faint
// box in the label's colour and its text wrapped to the box's width and
// clipped to it, sized in world cells so they zoom with the board. Over
// everything, in screen space. Below 6 px the text is noise and only the
// box shows; the selected label gets a solid outline.
function drawNotes(ctx, notes, cam, dpr, selected, grips = false) {
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.textBaseline = "top";
  ctx.textAlign = "left";
  const W = cam.w, H = cam.h;
  notes.forEach((n, i) => {
    if (!isText(n)) return;
    const [x, y, w, h] = n.rect;
    const [sx0, sy0] = cam.worldToScreen(x, y);
    const [sx1, sy1] = cam.worldToScreen(x + w, y - h);
    if (sx1 < 0 || sy1 < 0 || sx0 > W || sy0 > H) return;
    const sel = i === selected;
    ctx.lineWidth = sel ? 2 : 1;
    ctx.strokeStyle = noteCss(n.colour, sel ? 0.95 : 0.35);
    ctx.strokeRect(sx0, sy0, sx1 - sx0, sy1 - sy0);
    // With the Label tool held, the resize grip: a right angle in the
    // bottom-right corner, sized in screen px like its hit area (main.js).
    if (grips) {
      ctx.save();
      ctx.strokeStyle = noteCss(n.colour, 0.9);
      ctx.lineWidth = 2.5;
      ctx.beginPath();
      ctx.moveTo(sx1 - 12, sy1 - 3); ctx.lineTo(sx1 - 3, sy1 - 3); ctx.lineTo(sx1 - 3, sy1 - 12);
      ctx.stroke();
      ctx.restore();
    }
    const px = sizeOf(n) * cam.zoom;
    if (!n.text || px < TUNE.noteTextMin) return;
    ctx.save();
    ctx.beginPath();
    ctx.rect(sx0, sy0, sx1 - sx0, sy1 - sy0);
    ctx.clip();
    ctx.font = `600 ${px.toFixed(1)}px system-ui, sans-serif`;
    ctx.fillStyle = noteCss(n.colour, 1);
    const pad = PAD * px;
    const lines = wrap(n.text, (sx1 - sx0) - 2 * pad, (t) => ctx.measureText(t).width);
    lines.forEach((l, k) => ctx.fillText(l, sx0 + pad, sy0 + pad + k * LINE * px));
    ctx.restore();
  });
}

// Indexing: each indexed entity's number on a badge, in screen space so it
// stays legible at any zoom.
function drawIndexLabels(ctx, labels, cam, dpr) {
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  ctx.font = `700 12px system-ui, sans-serif`;
  for (const { x, y, n } of labels) {
    const [sx, sy] = cam.worldToScreen(x, y);
    const t = String(n);
    const w = Math.max(18, ctx.measureText(t).width + 10);
    ctx.fillStyle = COL.vector;
    ctx.beginPath();
    ctx.roundRect(sx - w / 2, sy - 9, w, 18, 9);
    ctx.fill();
    ctx.fillStyle = "#0b0c0f";
    ctx.fillText(t, sx, sy + 0.5);
  }
}

// The vector picker: the anchor cell filled, every copy's cell outlined, and
// a line through their centres so the direction reads at a glance.
function drawVectorMarks(ctx, marks, px) {
  const cells = marks.cells;
  if (!cells.length) return;
  ctx.save();
  ctx.lineWidth = 2 * px;
  ctx.strokeStyle = COL.vector;
  ctx.globalAlpha = 0.9;
  if (cells.length > 1) {
    ctx.beginPath();
    ctx.moveTo(cells[0][0] + 0.5, cells[0][1] + 0.5);
    for (const [x, y] of cells.slice(1)) ctx.lineTo(x + 0.5, y + 0.5);
    ctx.setLineDash([4 * px, 4 * px]);
    ctx.stroke();
    ctx.setLineDash([]);
  }
  ctx.beginPath();
  for (const [x, y] of cells) ctx.rect(x + 0.06, y + 0.06, 0.88, 0.88);
  ctx.stroke();
  ctx.globalAlpha = 0.35;
  ctx.fillStyle = COL.vector;
  ctx.fillRect(cells[0][0], cells[0][1], 1, 1);
  ctx.restore();
}

// --- items --------------------------------------------------------------------
//
// Where each item is drawn is flow.js's business: on its run (half a cell
// back, so a run's ends meet its machines' ports), and through a machine
// along its in -> wait -> out path, animated over the tick from the sim's
// forecast. Here they only become discs. A result waiting in an output zone
// is an ordinary disc on the out-edge, half on the machine and half on the
// belt. Items draw after the machines, so over their control overlays.
// `hidden`: a cell mask of hidden layers' entities, or null -- an item over
// one is not drawn (one byte read per item, and only while a layer is hidden).
function drawItems(ctx, sim, alpha, z, on, S, hidden = null, W = 0, dimCells = null) {
  const text = textOpacity(z);
  const { pts } = itemPoints(sim, alpha, {
    visible: (x0, y0, x1, y1) => on(x0, y0, x1 - x0, y1 - y0),
    reach: 0.5 + ITEM.r,
  });
  const pos = [], neg = [], labels = [], dimLabels = [];
  for (let i = 0; i < pts.length; i += 3) {
    const x = pts[i], y = pts[i + 1], value = pts[i + 2];
    if (!on(x - 0.5, y - 0.5)) continue;
    const k = Math.floor(y) * W + Math.floor(x);
    if (hidden && hidden[k]) continue;
    (value > 0 ? pos : neg).push(x, y);
    if (text > 0) (dimCells && dimCells[k] ? dimLabels : labels).push(x, y, value);
  }
  discs(ctx, pos, neg);
  return labels.length || dimLabels.length ? { labels, dimLabels, alpha: text } : null;
}

// Discs: the ring's colour as a full disc, then the dark interior over it.
function discs(ctx, pos, neg) {
  const r = ITEM.r, inner = ITEM.r - ITEM.ring;
  for (const [pts, col] of [[pos, COL.itemPos], [neg, COL.itemNeg]]) {
    if (!pts.length) continue;
    ctx.beginPath();
    for (let i = 0; i < pts.length; i += 2) { ctx.moveTo(pts[i] + r, pts[i + 1]); ctx.arc(pts[i], pts[i + 1], r, 0, Math.PI * 2); }
    ctx.fillStyle = col;
    ctx.fill();
  }
  if (!pos.length && !neg.length) return;
  ctx.beginPath();
  for (const pts of [pos, neg]) {
    for (let i = 0; i < pts.length; i += 2) { ctx.moveTo(pts[i] + inner, pts[i + 1]); ctx.arc(pts[i], pts[i + 1], inner, 0, Math.PI * 2); }
  }
  ctx.fillStyle = COL.itemFill;
  ctx.fill();
}

// 0 when an item's number would be noise, 1 once it reads comfortably.
function textOpacity(z) {
  const px = ITEM_TEXT.glyph * z;
  const lo = TUNE.itemTextMin, hi = Math.max(lo + 0.01, TUNE.itemTextGood);
  const t = Math.min(1, Math.max(0, (px - lo) / (hi - lo)));
  return t * t * (3 - 2 * t);
}

// The numbers, in screen space (the world transform would mirror them),
// with a dark halo so a digit spilling past the rim still reads.
function drawItemLabels(ctx, cargo, cam, dpr) {
  const { labels, alpha } = cargo;
  if (cargo.dimLabels?.length) drawItemLabels(ctx, { labels: cargo.dimLabels, alpha: alpha * DIM_TEXT }, cam, dpr);
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  ctx.font = `700 ${(ITEM_TEXT.size * cam.zoom).toFixed(1)}px system-ui, sans-serif`;
  ctx.lineJoin = "round";
  ctx.lineWidth = Math.max(2, 0.06 * cam.zoom);
  ctx.globalAlpha = alpha;
  ctx.strokeStyle = COL.itemHalo;
  ctx.fillStyle = COL.itemNum;
  for (let i = 0; i < labels.length; i += 3) {
    const [sx, sy] = cam.worldToScreen(labels[i], labels[i + 1]);
    const t = fmtNumber(labels[i + 2]);
    ctx.strokeText(t, sx, sy);
    ctx.fillText(t, sx, sy);
  }
  ctx.globalAlpha = 1;
}

// Layer labels (layers.js): each layer's name at the centre of mass of its
// entities, sized in world cells like the board's own labels, in the
// layer's colour over a black box so it reads over anything. A long name
// wraps into a few balanced lines (wrapLabel()); the wrap does not depend on
// size, so it is worked out once per name at a reference size and cached.
// Screen space, over everything. Below a few pixels of text a label is not
// drawn at all.
const LAYER_FONT = (px) => `700 ${px.toFixed(1)}px system-ui, sans-serif`;
const LAYER_LINE = 1.2;          // line height, in text heights
const wrapCache = new Map();     // name -> lines
function layerLines(ctx, text) {
  let lines = wrapCache.get(text);
  if (!lines) {
    ctx.font = LAYER_FONT(100);
    lines = wrapLabel(text, (t) => ctx.measureText(t).width / 100, LAYER_LINE);
    if (wrapCache.size > 500) wrapCache.clear();
    wrapCache.set(text, lines);
  }
  return lines;
}
// `hits`, if given, collects each drawn label's box in CSS px ({ id, x0, y0,
// x1, y1 }) for main.js to hit-test a tap against; `sel` is the id of the
// label the Hand has picked up, outlined.
function drawLayerLabels(ctx, labels, cam, dpr, hits = null, sel = null) {
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  for (const l of labels) {
    const px = l.size * cam.zoom;
    if (px < 4) continue;
    const lines = layerLines(ctx, l.text);
    const [sx, sy] = cam.worldToScreen(l.x, l.y);
    ctx.font = LAYER_FONT(px);
    const w = Math.max(...lines.map(t => ctx.measureText(t).width));
    const lh = px * LAYER_LINE;
    const padX = px * 0.35, padY = px * 0.2;
    const bw = w + 2 * padX, bh = lh * lines.length + 2 * padY;
    if (sx + bw / 2 < 0 || sx - bw / 2 > cam.w || sy + bh / 2 < 0 || sy - bh / 2 > cam.h) continue;
    ctx.fillStyle = "rgba(0,0,0,0.82)";
    ctx.fillRect(sx - bw / 2, sy - bh / 2, bw, bh);
    hits?.push({ id: l.id, x0: sx - bw / 2, y0: sy - bh / 2, x1: sx + bw / 2, y1: sy + bh / 2 });
    if (l.id === sel) {
      ctx.strokeStyle = COL.select;
      ctx.lineWidth = 2;
      ctx.setLineDash([6, 4]);
      ctx.strokeRect(sx - bw / 2 - 3, sy - bh / 2 - 3, bw + 6, bh + 6);
      ctx.setLineDash([]);
    }
    ctx.fillStyle = l.colour;
    lines.forEach((t, i) => ctx.fillText(t, sx, sy + (i - (lines.length - 1) / 2) * lh));
  }
}
