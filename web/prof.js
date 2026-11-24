// A frame profiler: Debug > profile frames. Named spans, nested into slash
// paths ("render/structs/ports"), summed per frame and reported as a mean
// over a one-second window, as a tree -- the desktop's core/tick_profile.py
// in miniature. Off, a span costs one boolean test.
//
// What it can and cannot see: these are CPU times on the main thread. Canvas
// 2D *records* draw calls there (path building, state changes, text
// shaping) and rasterises them later, on the GPU or a raster thread, so the
// pixel cost of a fill is not in any span. With `gpuSync` on, render() ends
// by reading one pixel back, which waits for that raster to finish, and the
// wait is reported as "render/gpu sync". That stalls the pipeline (it is
// what makes the number honest, and also what makes the frame slower), so it
// is its own switch.
//
// `frame` is the time between successive paints -- what the player sees --
// and `paint` the CPU part of it; the difference is idle time, raster the
// main thread did not wait for, and the compositor.

export const prof = {
  on: false,
  gpuSync: false,
  last: null,          // the latest window's report: { frames, frame, frameMax, rows: [[path, ms, calls]] }
  onReport: null,      // (report) => void, after each window
};

let stack = [];        // open spans: [path, t0]
let acc = new Map();   // path -> [ms, calls] over the window
let win = null;        // { t0, frames, frameSum, frameMax, lastPaint }

export function begin(name) {
  if (!prof.on) return;
  const parent = stack.length ? stack[stack.length - 1][0] + "/" : "";
  stack.push([parent + name, performance.now()]);
}

export function end() {
  if (!prof.on || !stack.length) return;
  const [path, t0] = stack.pop();
  const a = acc.get(path);
  const dt = performance.now() - t0;
  if (a) { a[0] += dt; a[1]++; } else acc.set(path, [dt, 1]);
}

// A span around a call; returns its result.
export function span(name, fn) {
  if (!prof.on) return fn();
  begin(name);
  try { return fn(); } finally { end(); }
}

// Called once per paint, at its start: counts the frame and, once a second,
// closes the window into a report.
export function frameStart(now) {
  if (!prof.on) { win = null; stack = []; return; }
  stack = [];
  if (!win) { win = { t0: now, frames: 0, frameSum: 0, frameMax: 0, lastPaint: now }; acc = new Map(); return; }
  const dt = now - win.lastPaint;
  // Paused, frames come on demand: a gap is idle time, not a slow frame.
  if (dt > 250) { win = { t0: now, frames: 0, frameSum: 0, frameMax: 0, lastPaint: now }; acc = new Map(); return; }
  win.lastPaint = now;
  win.frames++;
  win.frameSum += dt;
  win.frameMax = Math.max(win.frameMax, dt);
  if (now - win.t0 >= 1000) {
    prof.last = report(win, acc);
    prof.onReport?.(prof.last);
    win = { t0: now, frames: 0, frameSum: 0, frameMax: 0, lastPaint: now };
    acc = new Map();
  }
}

function report(w, a) {
  const n = Math.max(1, w.frames);
  // Pre-order: each path followed by its children, siblings by total (the
  // desktop's dump learnt that a flat sort misfiles grandchildren).
  const kids = new Map();
  for (const path of a.keys()) {
    const i = path.lastIndexOf("/");
    const parent = i < 0 ? "" : path.slice(0, i);
    if (!kids.has(parent)) kids.set(parent, []);
    kids.get(parent).push(path);
  }
  const rows = [];
  const walk = (parent) => {
    const list = (kids.get(parent) || []).sort((x, y) => a.get(y)[0] - a.get(x)[0]);
    for (const p of list) { rows.push([p, a.get(p)[0] / n, a.get(p)[1] / n]); walk(p); }
  };
  walk("");
  return { frames: w.frames, frame: w.frameSum / n, frameMax: w.frameMax, rows };
}

// The report as text: one line per span, indented by depth.
export function formatReport(r) {
  if (!r) return "profiling…";
  const lines = [`frame ${r.frame.toFixed(1)} ms (max ${r.frameMax.toFixed(0)}) · ${(1000 / r.frame).toFixed(0)} fps`];
  for (const [path, ms] of r.rows) {
    if (ms < 0.05) continue;
    const depth = path.split("/").length - 1;
    const name = path.slice(path.lastIndexOf("/") + 1);
    lines.push(`${"  ".repeat(depth)}${name.padEnd(18 - 2 * depth)} ${ms.toFixed(2).padStart(6)}`);
  }
  return lines.join("\n");
}
