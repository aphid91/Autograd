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
import { makeStamp, mirrored, rotated, stampCells, subset } from "./stamp.js";
import { stampGhost } from "./ghost.js";

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
// During a one-finger drag, what it is moving:
//   { cell0, world0 }         a run's tip
//   { p0, world0 }            a placement ghost
//   { sel: true, p0, world0 } the selection
//   { box: world0 }           a box selection being dragged out
let grab = null;
let boxRect = null;    // the box being dragged out, in cells, for drawing
const ROUTES = [ROUTE_H_FIRST, ROUTE_V_FIRST];
const ROUTE_ICONS = { [ROUTE_H_FIRST]: "routeHV", [ROUTE_V_FIRST]: "routeVH" };

// A machine, raw belt or stamp being placed, or null: the ghost's centre as
// a continuous world point. Everything else -- type, facing, flip, number,
// the stamp's shape -- is read from the held tool on every redraw, so
// turning the tool turns the ghost with no second copy to keep in step.
let place = null;
const GHOST_TAP_PX = 20;   // a tap this close to the ghost counts as on it

// The SELECTION: entities lifted off the board, held as a stamp at a pose,
// or null. Selecting an entity deletes it from the board and adds it here;
// deselecting places it back, wherever the selection now is. Its pose is a
// continuous centre point, like a placement ghost, so turning the selection
// four times comes back exactly where it started.
//   { stamp, px, py }
let sel = null;

// A handle for the console and for headless tests. Not an API.
const debug = window.gates = {
  cam, redraw, renderMs: 0,
  get board() { return board; },
  get shell() { return shell; },
  get panel() { return panel; },
  get inspectRow() { return inspectRow; },
  get run() { return run; },
  get place() { return place; },
  get sel() { return sel; },
  selEntries: () => (sel ? selAbs() : []),
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
    const ghosts = [
      selGhost(),
      run ? ghostOf(currentPlan()) : placeGhost(currentPlacement()),
      boxRect ? { rect: boxRect } : null,
    ];
    render(ctx, board, cam, dpr, { ...view, selected: info, ghosts });
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

// What the bar should show while something is planned, kept in step with
// the run / placement / paste state from one place.
function syncBar() {
  let p = null;
  if (run) p = { kind: "belt", routeName: ROUTE_NAMES[run.route], routeIcon: ROUTE_ICONS[run.route] };
  else if (place || shell.activeStamp) p = { kind: "place", values: hasValue(placer()) ? VALUE_CHOICES : null };
  const cur = shell.state.planning;
  if (JSON.stringify(cur) !== JSON.stringify(p)) shell.setPlanning(p);
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

// --- taps -------------------------------------------------------------------

function onTap(sx, sy) {
  if (!board) return;
  const [wx, wy] = cam.screenToWorld(sx, sy);
  const x = Math.floor(wx), y = Math.floor(wy);
  const mode = shell.state.mode;
  if (mode === "hand") {
    // A tap on bare board closes the window, as on the desktop.
    inspect(board.entityAt(x, y));
    return;
  }
  const tool = placer();
  if (tool) {
    if (!place) startPlace(wx, wy);
    else if (onGhost(sx, sy)) placeOnce();
    else {
      placeOnce();
      // A paste ends the way placing does: a tap elsewhere places and
      // leaves, back to plain selecting.
      if (tool.pasting) exitPaste();
      else endPlace();
    }
    return;
  }
  if (mode === "build" && beltHeld()) {
    // A tap on the tip turns it (straight, right, left). Any other second
    // tap places the run exactly as shown; it does not move the tip to
    // where the finger landed.
    if (run && onCells(sx, sy, run.end[0], run.end[1], 1, 1)) { turnTip(run, true); redraw(); }
    else if (run) commitRun();
    else if (x >= 0 && y >= 0 && x < board.width && y < board.height) startRun(x, y);
    return;
  }
  if (mode === "select") selectTap(x, y);
}

// --- planning a belt run --------------------------------------------------
//
// Tap starts a run on the tapped cell, a drag moves its tip, a tap on the
// tip turns it, and any other tap -- or Build -- places it. Two fingers stay
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

function startRun(x, y) {
  const tool = shell.heldTool;
  const dir = tool.dir | 0;
  // A slot saved while the line route existed may still say 2.
  const route = ROUTES.includes(tool.route) ? tool.route : ROUTE_H_FIRST;
  run = { start: [x, y], end: [x, y], route, tipTurn: 0, singleDir: dir, defaultDir: dir };
  syncBar();
}

function cancelRun() {
  run = null;
  grab = null;
  syncBar();
}

function commitRun() {
  const plan = currentPlan();
  if (plan) board.buildPlan(plan);
  cancelRun();
}

// --- placing a machine, a raw belt or a stamp -----------------------------
//
// A tap starts placing: a ghost centred on the tap. A drag moves it. Then:
//   tap ON the ghost (or within GHOST_TAP_PX of it)  place it, and keep the
//                       ghost where it is -- so a row of machines is a row
//                       of taps on the ghost, nudging it between them;
//   tap anywhere else   place it, and stop placing.
// A machine places only if every cell is free, as the desktop does. A stamp
// places PARTIALLY -- whatever fits, the desktop's shift-click paste. The
// tap elsewhere leaves placing even when nothing was placed.

function placeable(tool) {
  return !!tool && (tool.kind === "structure" || tool.kind === "rawbelt" || tool.kind === "stamp");
}

// What a tap would place: the held Build tool, or the stamp Select is
// pasting (`pasting: true`). Null when taps do not place.
function placer() {
  if (!shell) return null;
  if (shell.state.mode === "build" && placeable(shell.heldTool)) return shell.heldTool;
  const st = shell.activeStamp;
  return st ? { kind: "stamp", stamp: st.stamp, pasting: true } : null;
}

function typeIdOf(name) {
  return Number(Object.keys(T.types).find(k => T.types[k].name === name));
}

// The tool at the ghost's position: what would be built, and where it is
// blocked. Null when not placing.
function currentPlacement() {
  const tool = placer();
  if (!place || !board || !tool) return null;
  if (tool.kind === "stamp") {
    const st = tool.stamp;
    const origin = originFor(st.w, st.h, place.px, place.py);
    const at = st.entries.map(e => [origin[0] + e.dx, origin[1] + e.dy]);
    const ghost = stampGhost(board, st.entries, at);
    return { tool, kind: "stamp", origin, w: st.w, h: st.h, entries: st.entries, at, ghost, blocked: ghost.blocked };
  }
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
  if (pl.kind === "stamp") return { ...pl.ghost, box };
  if (pl.kind === "rawbelt") {
    const seed = pl.value === null ? 0 : pl.value > 0 ? 1 : -1;
    return { belts: [{ x, y, dir: pl.dir, inDir: board.incoming(x, y, pl.dir), seed }],
             blocked: pl.blocked, box };
  }
  const emit = T.types[pl.type].name === "source" ? pl.value : null;
  return { structs: [{ type: pl.type, x, y, dir: pl.dir, flip: pl.flip, w: pl.w, h: pl.h, o: pl.o, emit }],
           blocked: pl.blocked, box };
}

function startPlace(wx, wy) {
  place = { px: wx, py: wy };
  syncBar();
}

function endPlace() {
  place = null;
  grab = null;
  syncBar();
}

// Build what the ghost shows. A machine or raw belt only if every cell is
// free; a stamp, whatever of it fits. Returns whether anything was placed.
function placeOnce() {
  const pl = currentPlacement();
  if (!pl) return false;
  if (pl.kind === "stamp") {
    const placed = board.placeEntries(pl.entries, pl.at);
    redraw();
    return placed.size > 0;
  }
  if (pl.blocked.length) return false;
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

// --- the selection ----------------------------------------------------------
//
//   tap a board entity        select it: lifted off the board into `sel`
//   tap a selected entity     deselect it: placed back where it now is,
//                             if it fits -- otherwise it stays selected
//   tap empty board           deselect everything that fits
//   drag from a selected one  move the whole selection
//   drag with Box armed       a rectangle that toggles what it touches
//   Turn buttons              turn / mirror the selection as one stamp

const selOrigin = () => originFor(sel.stamp.w, sel.stamp.h, sel.px, sel.py);

// The selection's entries with absolute origins in dx/dy.
function selAbs() {
  const [ox, oy] = selOrigin();
  return sel.stamp.entries.map(e => ({ ...e, dx: ox + e.dx, dy: oy + e.dy }));
}

// Replace the selection with these absolute entries. The pose is re-centred
// on their box, so a turn after a membership change pivots on what is
// actually selected.
function setSel(abs) {
  if (!abs.length) sel = null;
  else {
    const { stamp, origin } = makeStamp(T, abs);
    sel = { stamp, px: origin[0] + stamp.w / 2, py: origin[1] + stamp.h / 2 };
  }
  shell.setSelection(sel ? sel.stamp.entries.length : 0);
  redraw();
}

// The selected entry covering cell (x, y), or -1.
function selIndexAt(x, y) {
  if (!sel) return -1;
  const [ox, oy] = selOrigin();
  const hit = stampCells(T, sel.stamp, ox, oy).find(([cx, cy]) => cx === x && cy === y);
  return hit ? hit[2] : -1;
}

function lift(rows) {
  rows = [...new Set(rows)].filter(r => r > 0);
  if (!rows.length) return;
  const abs = sel ? selAbs() : [];
  const caught = board.capture(rows).map(e => ({ ...e, partner: e.partner === null ? null : e.partner + abs.length }));
  board.eraseRows(rows);
  if (rows.includes(inspectRow)) inspect(-1);
  setSel([...abs, ...caught]);
}

// Place the selected entries `which` (indices) back on the board where the
// selection now has them. Each goes back only if it fits; the rest stay.
function drop(which) {
  if (!sel) return;
  const abs = selAbs();
  const want = new Set(which);
  const order = abs.map((_, i) => i).filter(i => want.has(i));
  const sub = subset(abs, i => want.has(i));
  const placed = board.placeEntries(sub, sub.map(e => [e.dx, e.dy]));
  const placedOrig = new Set([...placed].map(k => order[k]));
  setSel(subset(abs, i => !placedOrig.has(i)));
}

function dropAll() { if (sel) drop(sel.stamp.entries.map((_, i) => i)); }

function selectTap(x, y) {
  const i = selIndexAt(x, y);
  if (i >= 0) { drop([i]); return; }
  const row = board.entityAt(x, y);
  if (row >= 0) lift([row]);
  else dropAll();
}

// Toggle-if-able: whatever was selected in the box tries to go back down,
// and whatever was on the board in it is selected. The board's entities are
// collected FIRST, so something dropped by this same box is not picked
// straight back up.
function boxToggle(r) {
  const rows = new Set();
  for (let y = r.y; y < r.y + r.h; y++) {
    for (let x = r.x; x < r.x + r.w; x++) {
      const row = board.entityAt(x, y);
      if (row >= 0) rows.add(row);
    }
  }
  if (sel) {
    const [ox, oy] = selOrigin();
    const inBox = new Set();
    for (const [cx, cy, i] of stampCells(T, sel.stamp, ox, oy)) {
      if (cx >= r.x && cx < r.x + r.w && cy >= r.y && cy < r.y + r.h) inBox.add(i);
    }
    if (inBox.size) drop([...inBox]);
  }
  lift([...rows]);
}

function selGhost() {
  if (!sel || !board) return null;
  const abs = selAbs();
  const g = stampGhost(board, abs, abs.map(e => [e.dx, e.dy]));
  return { ...g, outlines: g.boxes };
}

function selectTurn(cw) {
  if (!sel) return;
  sel.stamp = rotated(T, sel.stamp, cw);
  redraw();
}

function selectFlip() {
  if (!sel) return;
  sel.stamp = mirrored(T, sel.stamp);
  redraw();
}

// Delete, Cut and Copy act on the selection; none of them is a mode.
function selectCommand(c) {
  if (!sel) return;
  if (c === "delete") { setSel([]); return; }
  const id = shell.addStamp(sel.stamp);
  if (c === "cut") { setSel([]); enterPaste(id); return; }
  // Copy: the selection goes back down. If all of it fit, there is nothing
  // left selected and the copy is ready to paste; anything that could not go
  // back stays selected for the player to deal with.
  dropAll();
  if (!sel) enterPaste(id);
}

// Entering paste puts the selection down first, and deletes whatever of it
// could not go down: a paste never coexists with a selection.
function enterPaste(id) {
  dropAll();
  if (sel) setSel([]);
  place = null;
  shell.setPasting(id);
  syncBar();
}

function exitPaste() {
  place = null;
  grab = null;
  shell.setPasting(null);
  syncBar();
}

// --- one-finger drags -----------------------------------------------------
// Whatever is dragged follows the finger's MOTION, not its position: it
// never jumps under the thumb, so the thumb never hides it.

function cellUnder(sx, sy) {
  const [wx, wy] = cam.screenToWorld(sx, sy);
  return [Math.floor(wx), Math.floor(wy)];
}

const dragHooks = {
  // (sx, sy): where the press was. Called with no point after a pinch drops
  // back to one finger, which never starts a selection drag of its own.
  wants(sx, sy) {
    if (run || place) return true;
    if (shell.state.mode !== "select" || shell.state.pasteStamp !== null) return false;
    if (sx === undefined) return false;
    if (shell.state.boxArmed) return true;
    return selIndexAt(...cellUnder(sx, sy)) >= 0;
  },
  begin(sx, sy) {
    const world0 = cam.screenToWorld(sx, sy);
    // A run's drag always moves its TIP. Moving the start too was tried and
    // read as confusing in practice: Cancel and re-tap is the way to move it.
    if (run) grab = { cell0: run.end.slice(), world0 };
    else if (place) grab = { p0: [place.px, place.py], world0 };
    else if (shell.state.boxArmed) { grab = { box: world0 }; boxRect = rectOf(world0, world0); }
    else if (sel) grab = { sel: true, p0: [sel.px, sel.py], world0 };
  },
  move(sx, sy) {
    if (!grab) return;
    const [wx, wy] = cam.screenToWorld(sx, sy);
    if (grab.box) { boxRect = rectOf(grab.box, [wx, wy]); redraw(); return; }
    const dx = wx - grab.world0[0], dy = wy - grab.world0[1];
    if (grab.sel) {
      if (!sel) return;
      sel.px = grab.p0[0] + dx;
      sel.py = grab.p0[1] + dy;
      redraw();
      return;
    }
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
  end() {
    if (grab?.box && boxRect) {
      const r = boxRect;
      boxRect = null;
      boxToggle(r);
      // Box is one-shot: after its rectangle, back to plain selecting.
      shell.state.boxArmed = false;
      shell.changed();
    }
    grab = null;
    redraw();
  },
};

// The cells a box from world point a to b touches, as {x, y, w, h}.
function rectOf(a, b) {
  const x0 = Math.floor(Math.min(a[0], b[0])), x1 = Math.floor(Math.max(a[0], b[0]));
  const y0 = Math.floor(Math.min(a[1], b[1])), y1 = Math.floor(Math.max(a[1], b[1]));
  return { x: x0, y: y0, w: x1 - x0 + 1, h: y1 - y0 + 1 };
}

// --- saves ------------------------------------------------------------------

async function show(name, bytes) {
  try {
    board = await Board.fromNpz(T, bytes);
    run = null;
    place = null;
    sel = null;
    shell.setSelection(0);
    if (shell.state.pasteStamp !== null) shell.setPasting(null);
    syncBar();
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

  let lastMode = null;
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
      if (run && !(s.mode === "build" && beltHeld())) { run = null; grab = null; }
      if (place && !placer()) { place = null; grab = null; }
      // Leaving Select puts the selection down. Anything that does not fit
      // stays lifted, and is still there on coming back.
      const left = lastMode === "select" && s.mode !== "select";
      lastMode = s.mode;
      if (left) dropAll();
      if (board) syncBar();   // the ghost reads the tool: a turn or a number shows at once
    },
    onPlanCancel: () => cancelRun(),
    onPlanBuild: () => commitRun(),
    onPlaceCancel: () => { if (shell.activeStamp) exitPaste(); else endPlace(); },
    onInspectValue: (v) => setInspectValue(v),
    onPlanRoute: () => {
      run.route = ROUTES[(ROUTES.indexOf(run.route) + 1) % ROUTES.length];
      shell.heldTool.route = run.route;   // the tool remembers its route
      shell.changed();
      syncBar();
    },
    onPlanTurn: (cw) => { turnTip(run, cw); redraw(); },
    onSelectTurn: (cw) => selectTurn(cw),
    onSelectFlip: () => selectFlip(),
    onSelectCommand: (c) => selectCommand(c),
    // Tapping the stamp being pasted puts it away; any other starts pasting it.
    onPasteStamp: (id) => { if (shell.state.pasteStamp === id) exitPaste(); else enterPaste(id); },
    onReset: () => console.info("reset: no simulation yet"),
    onPause: (paused) => console.info(paused ? "paused" : "playing"),
  });
  lastMode = shell.state.mode;

  panel = new Panel(document.getElementById("pane-host"), {
    onLoadSave: (name) => loadBundled(name).catch(e => alert(e.message)),
    onOpenFile: () => fileInput.click(),
    onFit: () => { if (board) cam.frame(board.bounds); redraw(); },
    onView: (v) => { view = v; redraw(); },
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
