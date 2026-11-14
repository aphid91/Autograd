// Wiring: load the type table, pick a save, keep the canvas sized, and
// redraw on demand (never in a free-running loop -- the board is static).

import { Board } from "./board.js";
import { Camera, attachGestures } from "./camera.js";
import { render } from "./render.js";

const DEFAULT_SAVE = "default.npz";

const canvas = document.getElementById("board");
const ctx = canvas.getContext("2d", { alpha: false });
const picker = document.getElementById("save-select");
const fileInput = document.getElementById("save-file");
const status = document.getElementById("status");

const cam = new Camera();
let T = null;
let board = null;
let dpr = 1;

// A handle for the console and for headless tests. Not an API.
const debug = window.gates = { cam, get board() { return board; }, redraw, renderMs: 0 };

let queued = false;
function redraw() {
  if (queued) return;
  queued = true;
  requestAnimationFrame(() => {
    queued = false;
    const t0 = performance.now();
    render(ctx, board, cam, dpr);
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

async function show(name, bytes) {
  status.textContent = `loading ${name}…`;
  try {
    const t0 = performance.now();
    board = await Board.fromNpz(T, bytes);
    cam.frame(board.bounds);
    const ms = (performance.now() - t0).toFixed(0);
    status.textContent = `${name} · ${board.structs.length} machines · `
      + `${board.belts.length} belts · ${board.unders.length} undergrounds · ${ms} ms`;
  } catch (err) {
    console.error(err);
    status.textContent = `${name}: ${err.message}`;
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
  resize();
  new ResizeObserver(resize).observe(canvas);
  attachGestures(canvas, cam, redraw);

  document.getElementById("fit").addEventListener("click", () => {
    if (board) cam.frame(board.bounds);
    redraw();
  });
  fileInput.addEventListener("change", async () => {
    const f = fileInput.files[0];
    if (f) await show(f.name, await f.arrayBuffer());
    fileInput.value = "";
  });
  picker.addEventListener("change", () => loadBundled(picker.value));

  // The bundled saves are listed by the build (web/build.sh). Running from
  // the bare web/ folder there is no list, and the file picker still works.
  let saves = [];
  try {
    const res = await fetch("saves/index.json");
    if (res.ok) saves = await res.json();
  } catch { /* no bundled saves */ }
  for (const name of saves) picker.add(new Option(name, name));
  picker.hidden = saves.length === 0;

  const first = saves.includes(DEFAULT_SAVE) ? DEFAULT_SAVE : saves[0];
  if (first) {
    picker.value = first;
    await loadBundled(first).catch(e => { status.textContent = e.message; });
  } else {
    status.textContent = "open a .npz save";
  }
}

init();
