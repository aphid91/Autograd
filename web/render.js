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
};

// Below this zoom (CSS px per cell) detail is not drawn at all.
const LOD = { beltSolid: 4, gridLines: 6, chevrons: 10, ports: 8, labels: 22 };

const css = (c) => `rgb(${(c[0] * 255) | 0},${(c[1] * 255) | 0},${(c[2] * 255) | 0})`;
const shade = (c, k) => css(c.map(v => v * k));

function fmtNumber(v) {
  if (Number.isInteger(v)) return String(v);
  if (Math.abs(v) >= 1e4 || Math.abs(v) < 1e-3) return v.toExponential(1);
  return String(parseFloat(v.toPrecision(3)));
}

// `opts`: the View toggles ({ grid, ports, labels }) and `selected`, the
// inspected entity's {x, y, w, h} or null.
export const DEFAULT_VIEW = { grid: true, ports: true, labels: true };

export function render(ctx, board, cam, dpr, opts = {}) {
  const view = { ...DEFAULT_VIEW, ...opts };
  const z = cam.zoom;
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.fillStyle = COL.void;
  ctx.fillRect(0, 0, ctx.canvas.width, ctx.canvas.height);
  if (!board) return;

  // World transform: x right, y up, one unit per cell.
  ctx.setTransform(dpr * z, 0, 0, -dpr * z,
                   dpr * (cam.w / 2 - cam.cx * z), dpr * (cam.h / 2 + cam.cy * z));
  const px = 1 / z;   // one CSS pixel, in world units
  const v = cam.visible(1);
  const on = (x, y, w = 1, h = 1) => x + w >= v.x0 && x <= v.x1 && y + h >= v.y0 && y <= v.y1;
  const S = board.T.cell.DIR_STEPS;

  ctx.fillStyle = COL.bg;
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
      ctx.stroke();
    }
  }

  drawBelts(ctx, board.belts, z, on, S);
  drawUnders(ctx, board.unders, z, on, S);
  drawStructs(ctx, board, z, px, on, S, view.ports);

  ctx.strokeStyle = COL.border;
  ctx.lineWidth = 2 * px;
  ctx.strokeRect(0, 0, board.width, board.height);

  if (view.ghost) drawGhost(ctx, view.ghost, z, px, on, S);

  // The inspected entity: an outline that stays a few pixels wide at any
  // zoom, drawn over everything so it cannot be hidden by what it marks.
  const sel = view.selected;
  if (sel) {
    const pad = 3 * px;
    ctx.strokeStyle = COL.select;
    ctx.lineWidth = 2.5 * px;
    ctx.strokeRect(sel.x - pad, sel.y - pad, sel.w + 2 * pad, sel.h + 2 * pad);
  }

  if (view.labels && z >= LOD.labels) drawLabels(ctx, board, cam, dpr, on);
}

function drawBelts(ctx, belts, z, on, S) {

  // Seeded cells are tinted by the seed's sign, under the belt itself.
  for (const sign of [1, -1]) {
    ctx.beginPath();
    for (const b of belts) if (b.seed === sign && on(b.x, b.y)) ctx.rect(b.x, b.y, 1, 1);
    ctx.fillStyle = sign > 0 ? COL.seedPos : COL.seedNeg;
    ctx.fill();
  }

  if (z < LOD.beltSolid) {
    // Zoomed far out a belt is a few pixels; a solid square reads better
    // than a hairline and costs one rect.
    ctx.beginPath();
    for (const b of belts) if (on(b.x, b.y)) ctx.rect(b.x + 0.2, b.y + 0.2, 0.6, 0.6);
    ctx.fillStyle = COL.belt;
    ctx.fill();
    return;
  }

  // The spine: in-edge -> centre -> out-edge. With no source the spine runs
  // straight through, so an isolated belt fills its cell.
  ctx.beginPath();
  for (const b of belts) {
    if (!on(b.x, b.y)) continue;
    const cx = b.x + 0.5, cy = b.y + 0.5;
    const o = S[b.dir];
    const i = b.inDir >= 0 ? S[b.inDir] : [-o[0], -o[1]];
    ctx.moveTo(cx + i[0] * 0.5, cy + i[1] * 0.5);
    ctx.lineTo(cx, cy);
    ctx.lineTo(cx + o[0] * 0.5, cy + o[1] * 0.5);
  }
  ctx.strokeStyle = COL.belt;
  ctx.lineWidth = 0.34;
  ctx.lineJoin = "round";
  ctx.lineCap = "butt";
  ctx.stroke();

  if (z < LOD.chevrons) return;
  ctx.beginPath();
  for (const b of belts) {
    if (!on(b.x, b.y)) continue;
    chevron(ctx, b.x + 0.5, b.y + 0.5, S[b.dir], 0.1);
  }
  ctx.strokeStyle = COL.chevron;
  ctx.lineWidth = 0.06;
  ctx.lineJoin = "miter";
  ctx.stroke();
}

// A ">" pointing along f, centred near (cx, cy).
function chevron(ctx, cx, cy, f, s) {
  const sx = -f[1], sy = f[0];
  ctx.moveTo(cx - f[0] * s + sx * s, cy - f[1] * s + sy * s);
  ctx.lineTo(cx + f[0] * s * 0.6, cy + f[1] * s * 0.6);
  ctx.lineTo(cx - f[0] * s - sx * s, cy - f[1] * s - sy * s);
}

function drawUnders(ctx, unders, z, on, S) {
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
  ctx.fillStyle = COL.underDot;
  ctx.fill();

  // Each end: a body with a dark bar on its buried side -- the front of an
  // entrance (diving), the back of an exit (surfacing).
  ctx.beginPath();
  const bars = new Path2D();
  for (const u of unders) {
    if (!on(u.x, u.y)) continue;
    ctx.rect(u.x + 0.12, u.y + 0.12, 0.76, 0.76);
    const f = S[u.dir];
    const s = u.exit ? -1 : 1;
    const bx = u.x + 0.5 + f[0] * s * 0.3, by = u.y + 0.5 + f[1] * s * 0.3;
    const hw = f[0] ? 0.08 : 0.38, hh = f[0] ? 0.38 : 0.08;
    bars.rect(bx - hw, by - hh, hw * 2, hh * 2);
  }
  ctx.fillStyle = COL.under;
  ctx.fill();
  ctx.fillStyle = COL.underDark;
  ctx.fill(bars);

  if (z < LOD.chevrons) return;
  ctx.beginPath();
  for (const u of unders) {
    if (on(u.x, u.y)) chevron(ctx, u.x + 0.5, u.y + 0.5, S[u.dir], 0.12);
  }
  ctx.strokeStyle = COL.chevron;
  ctx.lineWidth = 0.07;
  ctx.stroke();
}

// A planned run: the proposed belts and undergrounds translucent, cells the
// run cannot have tinted red, and a ring on each draggable end -- the tip
// solid, the start hollow, so the two read apart at a glance.
function drawGhost(ctx, g, z, px, on, S) {
  ctx.save();
  ctx.globalAlpha = 0.6;
  drawBelts(ctx, g.belts, Math.max(z, LOD.chevrons), on, S);
  drawUnders(ctx, g.unders, Math.max(z, LOD.chevrons), on, S);
  ctx.restore();

  ctx.beginPath();
  for (const c of g.blocked) if (on(c.x, c.y)) ctx.rect(c.x, c.y, 1, 1);
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
  ring(g.start, false);
  ring(g.end, true);
}

function drawStructs(ctx, board, z, px, on, S, ports) {
  const types = board.T.types;
  const inset = 0.05;
  for (const s of board.structs) {
    if (!on(s.x, s.y, s.w, s.h)) continue;
    const c = types[s.type].colour || [0.5, 0.5, 0.5];
    ctx.fillStyle = css(c);
    ctx.fillRect(s.x + inset, s.y + inset, s.w - 2 * inset, s.h - 2 * inset);
    ctx.strokeStyle = shade(c, 0.6);
    ctx.lineWidth = Math.max(px, 0.04);
    ctx.strokeRect(s.x + inset, s.y + inset, s.w - 2 * inset, s.h - 2 * inset);
  }

  if (!ports || z < LOD.ports) return;
  // Ports: blue triangles pointing in at inputs, red pointing out at outputs.
  const ins = new Path2D(), outs = new Path2D();
  for (const s of board.structs) {
    if (!on(s.x, s.y, s.w, s.h)) continue;
    for (const [lx, ly, f] of s.o.inputs) port(ins, s.x + lx, s.y + ly, S[f], true);
    for (const [lx, ly, f] of s.o.outputs) port(outs, s.x + lx, s.y + ly, S[f], false);
  }
  ctx.fillStyle = COL.in; ctx.fill(ins);
  ctx.fillStyle = COL.out; ctx.fill(outs);
}

function port(path, x, y, f, inward) {
  const cx = x + 0.5, cy = y + 0.5;
  const ex = cx + f[0] * 0.45, ey = cy + f[1] * 0.45;   // just inside the face
  const sx = -f[1] * 0.13, sy = f[0] * 0.13;
  const d = 0.16;
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
function drawLabels(ctx, board, cam, dpr, on) {
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  ctx.fillStyle = COL.label;
  const z = cam.zoom;
  for (const s of board.structs) {
    if (!on(s.x, s.y, s.w, s.h)) continue;
    const text = s.emit !== null ? fmtNumber(s.emit)
      : board.T.types[s.type].name.toUpperCase();
    const size = Math.min(0.32 * z, (s.w * z * 0.8) / (text.length * 0.62));
    if (size < 7) continue;
    ctx.font = `600 ${size.toFixed(1)}px system-ui, sans-serif`;
    const [sx, sy] = cam.worldToScreen(s.x + s.w / 2, s.y + s.h / 2);
    ctx.fillText(text, sx, sy);
  }
}
