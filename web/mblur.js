// Motion blur's timing: pure, so node can test it (tests/mblur.test.mjs).
//
// View > look > motion blur is temporal supersampling: each displayed frame
// is the average of `k` renders spread over the time the frame covers, each
// at its own tick and its own camera, then bloomed once. 1 is off, and the
// renderer then takes exactly its ordinary path.
//
// Where the samples sit:
//
//   - **A video** knows every frame's time exactly, so frame f's samples end
//     on the frame's own tick and step back evenly through it:
//     T_f - (k-1-i) * span / k, i = 0..k-1 (`frameSampleTimes`). The last is
//     the frame itself, so the blur trails motion and adds no lag.
//   - **Realtime** cannot: the worker ticks ahead on its own clock, and the
//     screen sees its state once a frame, as a delta. So the samples sit on a
//     fixed GRID of ticks, m * g for whole m, with g about a frame's ticks
//     over k (`gridSpacing`). The worker cuts its delta into slices at the
//     grid's ticks (`isCut`), the screen renders the grid points that fall in
//     the frame's span of sim time (`gridSampleTimes`), each from the state
//     at its tick -- so every sample is a real state, never an extrapolation.
//     A frame that runs long simply holds more grid points.
//
// Both sides compute a grid point as the same `m * g` in doubles, so they
// agree exactly on which tick each point needs.

// Ticks between grid points: a frame's worth of ticks over k samples. Never
// zero (a zero grid would cut every tick at any rate).
export function gridSpacing(rate, frameSec, k) {
  const g = (rate * frameSec) / Math.max(1, k);
  return Math.max(1e-6, g);
}

// Is tick n one the worker must cut at -- the floor of some grid point m * g?
// Checked against the neighbouring m's, so it uses the very `floor(m * g)`
// the screen uses rather than an algebraically equal test that could round
// the other way.
export function isCut(n, g) {
  if (!(g > 0)) return false;
  const m0 = Math.ceil(n / g);
  for (let m = m0 - 1; m <= m0 + 1; m++) if (m >= 0 && Math.floor(m * g) === n) return true;
  return false;
}

// The grid points in (S, E], oldest first. More than `cap` are thinned evenly
// (a frame that stalled for a second must not render hundreds), always keeping
// the newest, which is the state the frame shows.
export function gridSampleTimes(S, E, g, cap = Infinity) {
  if (!(E > S) || !(g > 0)) return [];
  const lo = Math.floor(S / g) + 1, hi = Math.floor(E / g);
  const out = [];
  for (let m = lo; m <= hi; m++) out.push(m * g);
  if (out.length <= cap) return out;
  const thin = [];
  for (let i = 0; i < cap; i++) thin.push(out[Math.round(out.length - 1 - (cap - 1 - i) * (out.length - 1) / Math.max(1, cap - 1))]);
  return thin;
}

// A video frame's k sample times, ending on the frame's own time `T` and
// spread over the `span` (sim ticks) the frame covers, never before `t0`.
export function frameSampleTimes(T, span, k, t0 = -Infinity) {
  const out = [];
  for (let i = 0; i < k; i++) out.push(Math.max(t0, T - ((k - 1 - i) * span) / k));
  return out;
}

// The camera part-way from `a` to `b` (u in 0..1): the centre linearly, the
// zoom geometrically, as the video's camera move does, so a zoom is even.
export function lerpCam(a, b, u) {
  return {
    cx: a.cx + (b.cx - a.cx) * u,
    cy: a.cy + (b.cy - a.cy) * u,
    zoom: a.zoom * Math.pow(b.zoom / a.zoom, u),
  };
}

export const camMoved = (a, b) => !a || !b || a.cx !== b.cx || a.cy !== b.cy || a.zoom !== b.zoom;
