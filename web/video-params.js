// Video export: the settings, and the arithmetic of the camera move. Pure and
// tiny, so the panel can show the settings and node can test the camera
// without loading video.js (which pulls in mediabunny, only when a recording
// starts).

export const VIDEO_FPS = 60;          // the file's frame rate, not a rate anything keeps up with
export const MIN_DIM = 128, MAX_DIM = 3840;
export const MAX_DURATION = 120;
// mediabunny's named quality levels, worst first (Fluoddity's list).
export const QUALITIES = ["very-low", "low", "medium", "high", "very-high"];

export const VIDEO_DEFAULTS = {
  width: 0, height: 0,     // 0: the board's size on screen, filled in at startup
  duration: 10,            // seconds
  tps: 30,                 // ticks per second of video
  quality: "high",
  lerpStart: 0, lerpEnd: 1,  // the stretch of the video the camera moves in
  fadeIn: 0.25, fadeOut: 0.25,  // speed ramps, as fractions of that stretch
};

export const VIDEO_SPEC = [
  ["width", "width", { min: MIN_DIM, max: MAX_DIM, step: 2 }],
  ["height", "height", { min: MIN_DIM, max: MAX_DIM, step: 2 }],
  ["duration", "duration (s)", { min: 1, max: MAX_DURATION, step: 1 }],
  ["tps", "ticks / s", { min: 1, max: 10000, step: 1 }],
  ["quality", "quality", { options: Object.fromEntries(QUALITIES.map(q => [q, q])) }],
];
export const CAMERA_SPEC = [
  ["lerpStart", "start lerp", { min: 0, max: 1, step: 0.01 }],
  ["lerpEnd", "end lerp", { min: 0, max: 1, step: 0.01 }],
  ["fadeIn", "fade in", { min: 0, max: 1, step: 0.01 }],
  ["fadeOut", "fade out", { min: 0, max: 1, step: 0.01 }],
];

// H.264 with 4:2:0 chroma needs even dimensions: round down, within bounds.
export const evenDim = (v) => {
  const n = Math.min(MAX_DIM, Math.max(MIN_DIM, Math.trunc(v)));
  return n - (n % 2);
};

export const frameCount = (s) => Math.max(1, Math.round(s.duration * VIDEO_FPS));
export const tickCount = (s) => Math.max(1, Math.round(s.duration * s.tps));

// Stored settings back into a clean record: known keys, right types, in range.
export function cleanVideo(v) {
  const out = { ...VIDEO_DEFAULTS };
  if (!v || typeof v !== "object") return out;
  const spec = Object.fromEntries([...VIDEO_SPEC, ...CAMERA_SPEC].map(e => [e[0], e[2]]));
  for (const [k, def] of Object.entries(VIDEO_DEFAULTS)) {
    const x = v[k];
    if (typeof x !== typeof def) continue;
    if (k === "quality") { if (QUALITIES.includes(x)) out[k] = x; continue; }
    if (!Number.isFinite(x)) continue;
    if ((k === "width" || k === "height") && x === 0) { out[k] = 0; continue; }
    out[k] = Math.min(spec[k].max, Math.max(spec[k].min, x));
  }
  return out;
}

// --- the camera ---------------------------------------------------------------
//
// A camera state is what the board view showed: { cx, cy, zoom, w, h } (world
// centre, CSS px per cell, view size in CSS px). The video frame is the
// largest box of the video's aspect that fits in that view -- the crop box
// drawn on screen -- in world units: { cx, cy, hw, hh } (half extents).

export function cropBox(cam, aspect) {
  let hw = cam.w / 2 / cam.zoom, hh = cam.h / 2 / cam.zoom;
  if (hw / hh > aspect) hw = hh * aspect; else hh = hw / aspect;
  return { cx: cam.cx, cy: cam.cy, hw, hh };
}

// How far along the move the camera is, 0..1, at video fraction u (0..1).
// Static before lerpStart and after lerpEnd. Between them the speed ramps up
// linearly over the first `fadeIn` of the stretch, holds, and ramps down
// over the last `fadeOut` (a trapezoid; scaled down if the two exceed 1):
// 0 and 0 is a constant-speed move, 0.5 and 0.5 a smooth S.
export function moveProgress(u, s) {
  const a = Math.min(s.lerpStart, s.lerpEnd), b = Math.max(s.lerpStart, s.lerpEnd);
  if (b - a < 1e-9) return u >= b ? 1 : 0;
  if (u <= a) return 0;
  if (u >= b) return 1;
  const x = (u - a) / (b - a);
  let fi = Math.max(0, s.fadeIn), fo = Math.max(0, s.fadeOut);
  if (fi + fo > 1) { const k = 1 / (fi + fo); fi *= k; fo *= k; }
  const v = 1 / (1 - fi / 2 - fo / 2);           // the cruising speed
  if (x < fi) return v * x * x / (2 * fi);
  if (x > 1 - fo) return 1 - v * (1 - x) * (1 - x) / (2 * fo);
  return v * (fi / 2 + (x - fi));
}

// The frame between two crop boxes at progress k: the centre linearly, the
// size geometrically (so a zoom feels even rather than rushing at one end).
export function lerpBox(a, b, k) {
  const g = (p, q) => p * Math.pow(q / p, k);
  return { cx: a.cx + (b.cx - a.cx) * k, cy: a.cy + (b.cy - a.cy) * k, hw: g(a.hw, b.hw), hh: g(a.hh, b.hh) };
}

// Where along a move from box `a` to box `b` (or a still, with no `b`) the
// camera is at fraction u of its whole stretch, under the lerp window and
// fades in `s` ({ lerpStart, lerpEnd, fadeIn, fadeOut }). One path for a
// video, its dry run and a presentation's camera schedule.
export function pathBox(a, b, s, u) {
  return b ? lerpBox(a, b, moveProgress(Math.min(1, Math.max(0, u)), s)) : a;
}

// The whole view a camera state shows, as a box: what a presentation stores
// as a pose, so it can be fitted to any screen.
export const viewBox = (cam) => ({ cx: cam.cx, cy: cam.cy, hw: cam.w / 2 / cam.zoom, hh: cam.h / 2 / cam.zoom });

// The camera (centre and zoom) that FITS box `b` into a w x h view: all of
// it shows, centred, with room to spare along one axis when the aspects
// differ.
export function fitBox(b, w, h) {
  return { cx: b.cx, cy: b.cy, zoom: Math.min(w / (2 * b.hw), h / (2 * b.hh)) };
}

// The crop box for video frame f of `frames`, with no end camera a still.
export function frameBox(startCam, endCam, s, f, frames) {
  const aspect = s.width / s.height;
  const a = cropBox(startCam, aspect);
  if (!endCam) return a;
  const u = frames > 1 ? f / (frames - 1) : 1;
  return pathBox(a, cropBox(endCam, aspect), s, u);
}
