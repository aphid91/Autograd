// Spatial audio: how loud, and how muffled, each run and gate is by its
// distance from a box -- the audio region drawn on the board, or the screen
// itself (camera-aware audio).
//
// Inside the box (anything touching it) is full volume and unfiltered. Out
// to the FRINGE -- a fraction of the box's longer side, so a camera box
// keeps the same feel at any zoom -- the volume falls along a half cosine
// to nothing, and the distance low-pass closes from LP_TOP Hz down by up to
// LP_OCTAVES octaves. Beyond the fringe an entity is not heard at all. A
// fringe of 0 is a hard edge: touching or nothing, the region as it was.
//
// Pure, so the recorder (main thread), the scale sheet's histogram and the
// tests share one definition.

export const LP_TOP = 18000;     // Hz: the distance low-pass fully open
export const LP_OCTAVES = 7;     // how far it closes at the fringe's edge, at full dial

// Distance from the axis-aligned box [x0, x1] x [y0, y1] to rect r
// ({ x, y, w, h }), in cells. 0 when they overlap; a box only sharing an
// edge with r is just outside (a tiny positive distance), so a hard-edged
// integer region takes exactly the cells inside it.
export function boxDist(x0, y0, x1, y1, r) {
  const rx1 = r.x + r.w, ry1 = r.y + r.h;
  if (x0 < rx1 && r.x < x1 && y0 < ry1 && r.y < y1) return 0;
  const dx = Math.max(r.x - x1, x0 - rx1, 0), dy = Math.max(r.y - y1, y0 - ry1, 0);
  return Math.max(Math.hypot(dx, dy), 1e-6);
}

// The fringe's width in cells for `space` ({ rect, fringe, lp }).
export const fringeOf = (space) => Math.max(0, space.fringe || 0) * Math.max(space.rect.w, space.rect.h);

// Volume at distance d with fringe width D: 1 inside, a half cosine to 0 at
// D, 0 beyond. D = 0 is a hard edge.
export function falloff(d, D) {
  if (d <= 0) return 1;
  if (!(D > 0) || d >= D) return 0;
  return 0.5 + 0.5 * Math.cos(Math.PI * d / D);
}

// The distance low-pass's cutoff in Hz, or 0 for none (inside the box, or
// the dial at 0). `lp` 0..1 is how far it closes at the fringe's edge.
export function lowpassHz(d, D, lp) {
  if (!(lp > 0) || d <= 0 || !(D > 0)) return 0;
  return LP_TOP * Math.pow(2, -LP_OCTAVES * lp * Math.min(1, d / D));
}

// A run's distance: its nearest cell's.
export function runDist(seg, r) {
  let best = Infinity;
  for (const [x, y] of seg.cells) {
    const d = boxDist(x, y, x + 1, y + 1, r);
    if (d < best) { best = d; if (d === 0) break; }
  }
  return best;
}

// Every candidate run's gain and low-pass cutoff, every gate's gain, and the
// sums the synth normalises by. `gates` are sim gate records (row);
// `space` is { rect, fringe, lp } or null (everything at 1, unfiltered).
export function spaceWeights(sim, runs, gates, space) {
  const gain = new Float32Array(runs.length).fill(1), lp = new Float32Array(runs.length);
  const gateGain = new Float32Array(gates.length).fill(1);
  if (!space) return { gain, lp, gateGain, runSum: runs.length };
  const r = space.rect, D = fringeOf(space);
  let runSum = 0;
  runs.forEach((seg, i) => {
    const d = runDist(seg, r);
    gain[i] = falloff(d, D);
    lp[i] = gain[i] > 0 ? lowpassHz(d, D, space.lp) : 0;
    runSum += gain[i];
  });
  gates.forEach((g, i) => {
    const [ox, oy] = sim.originOf(g.row), o = sim.orientOf(g.row);
    gateGain[i] = falloff(boxDist(ox, oy, ox + o.w, oy + o.h, r), D);
  });
  return { gain, lp, gateGain, runSum };
}

// A key that changes only when the weights would: the box to a hundredth of
// a cell and the two dials. Panning by less than that sends nothing.
export function spaceKey(space) {
  if (!space) return "";
  const r = space.rect, q = (v) => Math.round(v * 100);
  return `${q(r.x)},${q(r.y)},${q(r.w)},${q(r.h)}|${space.fringe}|${space.lp}`;
}
