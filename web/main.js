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
import { ROUTE_NAMES, planRun, turnTip } from "./planner.js";

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
//   route        0 H-first, 1 V-first, 2 line (persisted on the tool slot)
//   tipTurn      the last belt's turn off the travel direction: 0/1/3
//   singleDir    a one-cell run's facing; `defaultDir` is what it resets to
let run = null;
let grab = null;       // { which: "start" | "end", cell0, world0 } during a drag
const ROUTE_ICONS = ["routeHV", "routeVH", "routeLine"];

// A handle for the console and for headless tests. Not an API.
const debug = window.gates = {
  cam, redraw, renderMs: 0,
  get board() { return board; },
  get shell() { return shell; },
  get panel() { return panel; },
  get inspectRow() { return inspectRow; },
  get run() { return run; },
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
    render(ctx, board, cam, dpr, { ...view, selected: info, ghost: ghostOf(currentPlan()) });
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
  panel.showStatus(board ? board.describe(row) : null);
  redraw();
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
  if (shell.state.mode === "build" && beltHeld()) {
    // The second tap places the run exactly as shown; it does not move the
    // tip to where the finger landed.
    if (run) commitRun();
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
  shell.setPlanning(run ? { routeName: ROUTE_NAMES[run.route], routeIcon: ROUTE_ICONS[run.route] } : null);
  redraw();
}

function startRun(x, y) {
  const tool = shell.heldTool;
  const dir = tool.dir | 0;
  run = { start: [x, y], end: [x, y], route: tool.route | 0, tipTurn: 0, singleDir: dir, defaultDir: dir };
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

const dragHooks = {
  wants: () => !!run,
  begin(sx, sy) {
    // Whichever end is nearer the press, in screen space. `<=` gives a tie
    // to the tip, which is what makes tap-then-drag pull the tip off the
    // start rather than dragging the whole one-cell run.
    const d = (c) => {
      const [x, y] = cam.worldToScreen(c[0] + 0.5, c[1] + 0.5);
      return Math.hypot(x - sx, y - sy);
    };
    const which = d(run.end) <= d(run.start) ? "end" : "start";
    grab = { which, cell0: run[which].slice(), world0: cam.screenToWorld(sx, sy) };
  },
  move(sx, sy) {
    if (!grab || !run) return;
    // The grabbed end follows the finger's MOTION, not its position: it
    // never jumps under the thumb, so the thumb never hides it.
    const [wx, wy] = cam.screenToWorld(sx, sy);
    const clamp = (v, hi) => Math.max(0, Math.min(hi - 1, v));
    const nx = clamp(Math.floor(grab.cell0[0] + 0.5 + wx - grab.world0[0]), board.width);
    const ny = clamp(Math.floor(grab.cell0[1] + 0.5 + wy - grab.world0[1]), board.height);
    const cur = run[grab.which];
    if (cur[0] === nx && cur[1] === ny) return;
    run[grab.which] = [nx, ny];
    // A tip on a new cell goes back to straight on.
    if (grab.which === "end") { run.tipTurn = 0; run.singleDir = run.defaultDir; }
    redraw();
  },
  end() { grab = null; },
};

// --- saves ------------------------------------------------------------------

async function show(name, bytes) {
  try {
    board = await Board.fromNpz(T, bytes);
    if (run) cancelRun();
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
    },
    onPlanCancel: () => cancelRun(),
    onPlanBuild: () => commitRun(),
    onPlanRoute: () => {
      run.route = (run.route + 1) % ROUTE_NAMES.length;
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
    onEmit: (row, v) => { board.setEmit(row, v); redraw(); },
    onSeed: (row, on, v) => { board.setSeed(row, on, v); redraw(); },
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
