// Wiring: the board, the camera, the phone shell (ui.js) and the Tweakpane
// panel. The shell and the panel only report intents; this file is the one
// place that applies them to the board -- the same split the desktop keeps
// between its panel and its orchestrator.
//
// Rendering is on demand: nothing runs in a loop.

import { Board } from "./board.js";
import { Camera, attachGestures } from "./camera.js";
import { render } from "./render.js";
import { Shell } from "./ui.js";
import { Panel } from "./panel.js";
import { ROUTE_NAMES, ROUTE_H_FIRST, ROUTE_V_FIRST, planRun, turnTip } from "./planner.js";
import { originFor } from "./placer.js";
import { VALUE_CHOICES, hasValue, toolValue } from "./tools.js";

const DEFAULT_SAVE = "default.npz";

const canvas = document.getElementById("board");
const ctx = canvas.getContext("2d", { alpha: false });
const fileInput = document.getElementById("save-file");

const cam = new Camera();
let T = null;
let board = null;
let dpr = 1;
let shell = null;
let panel = null;
let view = {};
let inspectRow = -1;   // a row, not a description: re-read it every time

// The belt run being planned, or null. Plain data; planner.js turns it into
// cells against the live board on every redraw, so the ghost can never show
// a plan computed against a board that has since changed.
//   start, end   cells; the end starts ON the start
//   route        H-first or V-first (persisted on the tool slot). The planner
//                still knows the staircase line; the phone does not offer it
//   tipTurn      the last belt's turn off the travel direction: 0/1/3
//   singleDir    a one-cell run's facing; `defaultDir` is what it resets to
let run = null;
let grab = null;       // during a drag: { cell0, world0 } for a run, { p0, world0 } for a ghost
const ROUTES = [ROUTE_H_FIRST, ROUTE_V_FIRST];
const ROUTE_ICONS = { [ROUTE_H_FIRST]: "routeHV", [ROUTE_V_FIRST]: "routeVH" };

// A machine or raw belt being placed, or null: the ghost's centre as a
// continuous world point. Everything else -- type, facing, flip, number --
// is read from the held tool on every redraw, so turning the tool turns the
// ghost with no second copy to keep in step.
let place = null;
const GHOST_TAP_PX = 20;   // a tap this close to the ghost counts as on it

// A handle for the console and for headless tests. Not an API.
const debug = window.gates = {
  cam, redraw, renderMs: 0,
  get board() { return board; },
  get shell() { return shell; },
  get panel() { return panel; },
  get inspectRow() { return inspectRow; },
  get run() { return run; },
  get place() { return place; },
  placement: () => currentPlacement(),
  plan: () => currentPlan(),
};

let queued = false;
function redraw() {
  if (queued) return;
  queued = true;
  requestAnimationFrame(() => {
    queued = false;
    const t0 = performance.now();
    const info = board ? board.describe(inspectRow) : null;
    render(ctx, board, cam, dpr, { ...view, selected: info, ghost: run ? ghostOf(currentPlan()) : placeGhost(currentPlacement()) });
    debug.renderMs = performance.now() - t0;
  });
}

function resize() {
  dpr = window.devicePixelRatio || 1;
  const r = canvas.getBoundingClientRect();
  canvas.width = Math.round(r.width * dpr);
  canvas.height = Math.round(r.height * dpr);
  cam.resize(r.width, r.height);
  redraw();
}

// --- inspecting (the Hand) ------------------------------------------------

function inspect(row) {
  inspectRow = row;
  syncInspect();
}

// The Status pane and the Hand's number swatches both show the inspected
// row; either can change its number, so both are refreshed after any edit.
// The Status pane's own fields are not rebuilt on an edit made there: a
// number field reports per keystroke and rebuilding would steal its focus.
function syncInspect() {
  panel.showStatus(board ? board.describe(inspectRow) : null);
  shell.setInspectValue(handValueOf(inspectRow));
  redraw();
}

// What the Hand's number swatches show for a row: a Source's emit value or
// a belt's seed (null when unseeded), or nothing for anything else.
function handValueOf(row) {
  const info = board?.describe(row);
  if (!info) return null;
  if (info.emit !== null) return { kind: "emit", values: VALUE_CHOICES, current: info.emit };
  if (info.seedOn !== null) return { kind: "seed", values: VALUE_CHOICES, current: info.seedOn ? info.seedValue : null };
  return null;
}

function setInspectValue(v) {
  const info = board?.describe(inspectRow);
  if (!info) return;
  if (info.emit !== null) board.setEmit(inspectRow, v);
  else if (info.seedOn !== null) {
    // Tapping the lit number again unseeds the belt, as on the raw belt tool.
    const off = info.seedOn && info.seedValue === v;
    board.setSeed(inspectRow, !off, off ? info.seedValue : v);
  }
  syncInspect();
}

function onTap(sx, sy) {
  if (!board) return;
  const [wx, wy] = cam.screenToWorld(sx, sy);
  const x = Math.floor(wx), y = Math.floor(wy);
  if (shell.state.mode === "hand") {
    // A tap on bare board closes the window, as on the desktop.
    inspect(board.entityAt(x, y));
    return;
  }
  if (shell.state.mode === "build" && placeable(shell.heldTool)) {
    if (!place) startPlace(wx, wy);
    else if (onGhost(sx, sy)) placeOnce();
    else { placeOnce(); endPlace(); }
    return;
  }
  if (shell.state.mode === "build" && beltHeld()) {
    // A tap on the tip turns it (straight, right, left). Any other second
    // tap places the run exactly as shown; it does not move the tip to
    // where the finger landed.
    if (run && onCells(sx, sy, run.end[0], run.end[1], 1, 1)) { turnTip(run, true); redraw(); }
    else if (run) commitRun();
    else if (x >= 0 && y >= 0 && x < board.width && y < board.height) startRun(x, y);
  }
}

// --- planning a belt run --------------------------------------------------
//
// Tap starts a run on the tapped cell, a drag moves whichever end of it is
// nearer the finger (the tip on a tie, so tap-then-drag always pulls the tip
// off the start), and a second tap -- or Build -- places it. Two fingers stay
// the camera throughout.

const STEPS = [[1, 0], [0, 1], [-1, 0], [0, -1]];
const dirToward = (from, to) => STEPS.findIndex(s => s[0] === to.x - from.x && s[1] === to.y - from.y);

function beltHeld() {
  const t = shell?.heldTool;
  return !!t && t.kind === "belt";
}

function currentPlan() {
  if (!run || !board) return null;
  return planRun(run, (x, y) => board.isFree(x, y), T.under.MAX_GAP);
}

// The plan in the renderer's own shapes, so ghosts draw with the same code
// as the real thing.
function ghostOf(plan) {
  if (!plan) return null;
  const belts = [], unders = [], blocked = [];
  plan.cells.forEach((c, i) => {
    if (c.kind === "blocked") { blocked.push(c); return; }
    if (c.kind === "buried") return;
    if (c.kind === "entry" || c.kind === "exit") {
      const pair = plan.pairs.find(([a]) => a === i);
      const mate = pair ? plan.cells[pair[1]] : null;
      unders.push({ x: c.x, y: c.y, dir: c.dir, exit: c.kind === "exit", span: mate ? [mate.x, mate.y] : null });
      return;
    }
    // A belt's incoming arm: from the cell before it, if that one will exist
    // and feed it; the first belt asks the board who feeds it now.
    const prev = plan.cells[i - 1];
    let inDir = -1;
    if (!prev) inDir = board.incoming(c.x, c.y, c.dir);
    else if (prev.kind !== "blocked" && prev.kind !== "buried" && prev.kind !== "entry") inDir = dirToward(c, prev);
    belts.push({ x: c.x, y: c.y, dir: c.dir, inDir, seed: 0 });
  });
  return { belts, unders, blocked, start: run.start, end: run.end };
}

function syncPlanUI() {
  shell.setPlanning(run ? { kind: "belt", routeName: ROUTE_NAMES[run.route], routeIcon: ROUTE_ICONS[run.route] } : null);
  redraw();
}

function startRun(x, y) {
  const tool = shell.heldTool;
  const dir = tool.dir | 0;
  // A slot saved while the line route existed may still say 2.
  const route = ROUTES.includes(tool.route) ? tool.route : ROUTE_H_FIRST;
  run = { start: [x, y], end: [x, y], route, tipTurn: 0, singleDir: dir, defaultDir: dir };
  syncPlanUI();
}

function cancelRun() {
  run = null;
  grab = null;
  syncPlanUI();
}

function commitRun() {
  const plan = currentPlan();
  if (plan) board.buildPlan(plan);
  cancelRun();
}

// One-finger drags while something is planned. Whatever is dragged follows
// the finger's MOTION, not its position: it never jumps under the thumb, so
// the thumb never hides it.
const dragHooks = {
  wants: () => !!run || !!place,
  begin(sx, sy) {
    const world0 = cam.screenToWorld(sx, sy);
    // A run's drag always moves its TIP. Moving the start too was tried and
    // read as confusing in practice: Cancel and re-tap is the way to move it.
    if (run) grab = { cell0: run.end.slice(), world0 };
    else if (place) grab = { p0: [place.px, place.py], world0 };
  },
  move(sx, sy) {
    if (!grab) return;
    const [wx, wy] = cam.screenToWorld(sx, sy);
    const dx = wx - grab.world0[0], dy = wy - grab.world0[1];
    if (place && grab.p0) {
      place.px = grab.p0[0] + dx;
      place.py = grab.p0[1] + dy;
      redraw();
      return;
    }
    if (!run || !grab.cell0) return;
    const clamp = (v, hi) => Math.max(0, Math.min(hi - 1, v));
    const nx = clamp(Math.floor(grab.cell0[0] + 0.5 + dx), board.width);
    const ny = clamp(Math.floor(grab.cell0[1] + 0.5 + dy), board.height);
    if (run.end[0] === nx && run.end[1] === ny) return;
    run.end = [nx, ny];
    // A tip on a new cell goes back to straight on.
    run.tipTurn = 0;
    run.singleDir = run.defaultDir;
    redraw();
  },
  end() { grab = null; },
};

// --- placing a machine or a raw belt -------------------------------------
//
// A tap starts placing: a ghost centred on the tap. A drag moves it. Then:
//   tap ON the ghost (or within GHOST_TAP_PX of it)  place it, and keep the
//                       ghost where it is -- so a row of machines is a row
//                       of taps on the ghost, nudging it between them;
//   tap anywhere else   place it, and stop placing.
// Both place only if every cell is free, as the desktop does, and the second
// leaves placing even when the placement was refused.

function placeable(tool) {
  return !!tool && (tool.kind === "structure" || tool.kind === "rawbelt");
}

function typeIdOf(name) {
  return Number(Object.keys(T.types).find(k => T.types[k].name === name));
}

// The held tool at the ghost's position: what would be built, and where it
// is blocked. Null when not placing.
function currentPlacement() {
  const tool = shell?.heldTool;
  if (!place || !board || !placeable(tool)) return null;
  const dir = tool.dir | 0, flip = !!tool.flip;
  if (tool.kind === "rawbelt") {
    const origin = originFor(1, 1, place.px, place.py);
    const free = board.isFree(origin[0], origin[1]);
    return { tool, kind: "rawbelt", origin, dir, w: 1, h: 1, value: toolValue(tool),
             blocked: free ? [] : [{ x: origin[0], y: origin[1] }] };
  }
  const type = typeIdOf(tool.type);
  const o = T.types[type].orient[`${dir}${flip ? 1 : 0}`];
  const origin = originFor(o.w, o.h, place.px, place.py);
  const { blocked } = board.footprint(type, origin, dir, flip);
  return { tool, kind: "structure", type, origin, dir, flip, w: o.w, h: o.h, o,
           value: toolValue(tool), blocked };
}

function placeGhost(pl) {
  if (!pl) return null;
  const [x, y] = pl.origin;
  const box = { x, y, w: pl.w, h: pl.h };
  if (pl.kind === "rawbelt") {
    const seed = pl.value === null ? 0 : pl.value > 0 ? 1 : -1;
    return { belts: [{ x, y, dir: pl.dir, inDir: board.incoming(x, y, pl.dir), seed }],
             blocked: pl.blocked, box };
  }
  const emit = T.types[pl.type].name === "source" ? pl.value : null;
  return { structs: [{ type: pl.type, x, y, dir: pl.dir, flip: pl.flip, w: pl.w, h: pl.h, o: pl.o, emit }],
           blocked: pl.blocked, box };
}

function syncPlaceUI() {
  const tool = shell.heldTool;
  shell.setPlanning(place ? { kind: "place", values: hasValue(tool) ? VALUE_CHOICES : null } : null);
  redraw();
}

function startPlace(wx, wy) {
  place = { px: wx, py: wy };
  syncPlaceUI();
}

function endPlace() {
  place = null;
  grab = null;
  syncPlaceUI();
}

// Build what the ghost shows, if every cell is free. Returns whether it did.
function placeOnce() {
  const pl = currentPlacement();
  if (!pl || pl.blocked.length) return false;
  const [x, y] = pl.origin;
  if (pl.kind === "rawbelt") {
    board.spawnBelt(x, y, pl.dir, pl.value);
    board.derive();
  } else {
    board.spawnStructure(pl.type, pl.origin, pl.dir, pl.flip, { emit: pl.value });
  }
  redraw();
  return true;
}

// Is screen point (sx, sy) on the w x h cell box at (x, y), or within
// GHOST_TAP_PX of it?
function onCells(sx, sy, x, y, w, h) {
  const [x0, y1] = cam.worldToScreen(x, y + h);
  const [x1, y0] = cam.worldToScreen(x + w, y);
  const m = GHOST_TAP_PX;
  return sx >= x0 - m && sx <= x1 + m && sy >= y1 - m && sy <= y0 + m;
}

function onGhost(sx, sy) {
  const pl = currentPlacement();
  return !!pl && onCells(sx, sy, pl.origin[0], pl.origin[1], pl.w, pl.h);
}

// --- saves ------------------------------------------------------------------

async function show(name, bytes) {
  try {
    board = await Board.fromNpz(T, bytes);
    if (run) cancelRun();
    if (place) endPlace();
    cam.frame(board.bounds);
    inspect(-1);
  } catch (err) {
    console.error(err);
    alert(`${name}: ${err.message}`);
  }
  redraw();
}

async function loadBundled(name) {
  const res = await fetch(`saves/${encodeURIComponent(name)}`);
  if (!res.ok) throw new Error(`${name}: HTTP ${res.status}`);
  await show(name, await res.arrayBuffer());
}

async function init() {
  T = await (await fetch("types.json")).json();

  shell = new Shell(T, {
    controls: document.getElementById("controls"),
    bar: document.getElementById("bar"),
    swrow: document.getElementById("swrow"),
    swatches: document.getElementById("swatches"),
    left: document.getElementById("sw-left"),
    right: document.getElementById("sw-right"),
    picker: document.getElementById("picker"),
  }, {
    onChange: (s) => {
      // Leaving the Hand puts the Status window away; leaving the belt tool
      // abandons a run, as switching tools does on the desktop.
      if (s.mode !== "hand" && inspectRow >= 0) inspect(-1);
      if (run && !(s.mode === "build" && beltHeld())) cancelRun();
      if (place && !(s.mode === "build" && placeable(shell.heldTool))) endPlace();
      redraw();   // the ghost reads the tool: a turn or a new number shows at once
    },
    onPlanCancel: () => cancelRun(),
    onPlanBuild: () => commitRun(),
    onPlaceCancel: () => endPlace(),
    onInspectValue: (v) => setInspectValue(v),
    onPlanRoute: () => {
      run.route = ROUTES[(ROUTES.indexOf(run.route) + 1) % ROUTES.length];
      shell.heldTool.route = run.route;   // the tool remembers its route
      shell.changed();
      syncPlanUI();
    },
    onPlanTurn: (cw) => { turnTip(run, cw); redraw(); },
    onReset: () => console.info("reset: no simulation yet"),
    onPause: (paused) => console.info(paused ? "paused" : "playing"),
  });

  panel = new Panel(document.getElementById("pane-host"), {
    onLoadSave: (name) => loadBundled(name).catch(e => alert(e.message)),
    onOpenFile: () => fileInput.click(),
    onFit: () => { if (board) cam.frame(board.bounds); redraw(); },
    onView: (v) => { view = v; redraw(); },
    onAddStamp: () => shell.addStamp(),
    onClearStamps: () => shell.clearStamps(),
    onCloseStatus: () => inspect(-1),
    onEmit: (row, v) => { board.setEmit(row, v); shell.setInspectValue(handValueOf(row)); redraw(); },
    onSeed: (row, on, v) => { board.setSeed(row, on, v); shell.setInspectValue(handValueOf(row)); redraw(); },
  });
  view = { ...panel.view };

  resize();
  new ResizeObserver(resize).observe(canvas);
  attachGestures(canvas, cam, redraw, { onTap, drag: dragHooks });

  fileInput.addEventListener("change", async () => {
    const f = fileInput.files[0];
    if (f) await show(f.name, await f.arrayBuffer());
    fileInput.value = "";
  });

  // Space toggles pause, as on the desktop -- through the shell's button so
  // the icon cannot disagree with the state.
  document.addEventListener("keydown", (e) => {
    if (e.target instanceof HTMLInputElement) return;
    if (e.code === "Space") { e.preventDefault(); document.getElementById("btn-play").click(); }
  });

  // The bundled saves are listed by web/build.sh. Served from the bare web/
  // folder there is no list, and Open still works.
  let saves = [];
  try {
    const res = await fetch("saves/index.json");
    if (res.ok) saves = await res.json();
  } catch { /* no bundled saves */ }
  const first = saves.includes(DEFAULT_SAVE) ? DEFAULT_SAVE : saves[0];
  panel.setSaves(saves, first);
  if (first) await loadBundled(first).catch(e => alert(e.message));
}

init();
