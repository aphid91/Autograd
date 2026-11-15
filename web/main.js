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

// A handle for the console and for headless tests. Not an API.
const debug = window.gates = {
  cam, redraw, renderMs: 0,
  get board() { return board; },
  get shell() { return shell; },
  get panel() { return panel; },
  get inspectRow() { return inspectRow; },
};

let queued = false;
function redraw() {
  if (queued) return;
  queued = true;
  requestAnimationFrame(() => {
    queued = false;
    const t0 = performance.now();
    const info = board ? board.describe(inspectRow) : null;
    render(ctx, board, cam, dpr, { ...view, selected: info });
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
  if (!board || shell.state.mode !== "hand") return;
  const [wx, wy] = cam.screenToWorld(sx, sy);
  // A tap on bare board closes the window, as on the desktop.
  inspect(board.entityAt(Math.floor(wx), Math.floor(wy)));
}

// --- saves ------------------------------------------------------------------

async function show(name, bytes) {
  try {
    board = await Board.fromNpz(T, bytes);
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
    // Leaving the Hand puts the Status window away.
    onChange: (s) => { if (s.mode !== "hand" && inspectRow >= 0) inspect(-1); },
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
  attachGestures(canvas, cam, redraw, onTap);

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
