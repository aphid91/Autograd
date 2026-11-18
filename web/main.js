// Wiring: the board, the camera, the phone shell (ui.js) and the Tweakpane
// panel. The shell and the panel only report intents; this file is the one
// place that applies them to the board -- the same split the desktop keeps
// between its panel and its orchestrator.
//
// Rendering is on demand while the simulation is paused: input calls
// redraw(), which coalesces into one frame. While it plays, each frame asks
// for the next, banks real time on the clock and spends it in whole ticks.

import { Board } from "./board.js";
import { Camera, attachGestures } from "./camera.js";
import { render } from "./render.js";
import { Shell } from "./ui.js";
import { Panel } from "./panel.js";
import { ROUTE_NAMES, ROUTE_H_FIRST, ROUTE_V_FIRST, chooseRoute, planRun, routeWorks, turnTip } from "./planner.js";
import { originFor } from "./placer.js";
import { VALUE_CHOICES, hasValue, toolValue } from "./tools.js";
import { makeStamp, mirrored, rotated, stampCells, subset } from "./stamp.js";
import { emitMask, emitsWith, stampGhost } from "./ghost.js";
import { traceRun } from "./segtrace.js";
import { TUNE, setTune } from "./tune.js";
import { Sim, Clock } from "./sim.js";
import { History } from "./history.js";
import { DEFAULT, naturalOrder, tableValues, finalValues } from "./indexer.js";
import { entrySize } from "./stamp.js";
import { noteAt, gripAt, newRect, MIN_W, MIN_H } from "./notes.js";
import { Claims, followChain, offsets, layCopies, isZero, rotateVec, flipVec, moreCount, lessCount } from "./vector.js";
import { deleteSave, getSave, listSaves, putSave } from "./saves.js";

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
// The simulation of the loaded board, and the clock that paces it. A new
// Sim per board, as a fresh desktop session per load: segment ids come from
// an allocator that outlives rebuilds, and the tick visits runs by id.
let sim = null;
const clock = new Clock();

// The belt run being planned, or null. Plain data; planner.js turns it into
// cells against the live board on every redraw, so the ghost can never show
// a plan computed against a board that has since changed.
//   start, end   cells; the end starts ON the start
//   route        H-first or V-first (persisted on the tool slot). The planner
//                still knows the staircase line; the phone does not offer it
//   tipTurn      the last belt's turn off the travel direction: 0/1/3
//   singleDir    a one-cell run's facing; `defaultDir` is what it resets to
//   over         the row of the placed belt the run starts on, or 0. That
//                belt is turned to follow the run when it is built, rather
//                than blocking it
//   v0           the vector when the run began (vectored), or null. The
//                copies' STARTS stay at k * v0 whatever the vector does
//                after; their ENDS follow the current vector
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
// A tap within TUNE.tapMarginPx (20 by default) of a ghost counts as on it.

// The SELECTION: entities lifted off the board, held as a stamp at a pose,
// or null. Selecting an entity deletes it from the board and adds it here;
// deselecting places it back, wherever the selection now is. Its pose is a
// continuous centre point, like a placement ghost, so turning the selection
// four times comes back exactly where it started.
//   { stamp, px, py }
let sel = null;

// Undo, redo and the checkpoint (history.js). A state is a board snapshot,
// and only STABLE states are recorded: nothing lifted. A selection is a
// change in progress -- its entities are off the board -- so lifting,
// moving and putting down is one step, committed when the selection ends,
// and Undo while something is lifted throws that change away. `committed`
// is the board revision the history's current state was taken at; any
// other revision with nothing lifted is a new step.
const history = new History();
let committed = -1;

// THE VECTOR (vector.js). While `on`, what is built repeats at k * v for
// k = 0 .. count-1, each copy laid in sequence and dropped if it does not fit.
//   picking   null, or which picker is open: "build" (tap Build while in
//             Build) or "select" (the selection's Vector swatch)
//   v         the offset, in cells. Starts at (0, 0): no copies
//   anchor    the cell the picker's highlights start from -- wherever the
//             first drag after opening the picker began
//   sv        the selection picker's own offset, so picking one never
//             disturbs the building vector
let vec = { on: false, picking: null, v: [0, 0], count: 2, anchor: null, sv: [0, 0] };

// The offset the open picker edits.
const pickV = () => (vec.picking === "select" ? vec.sv : vec.v);
function setPickV(v) { if (vec.picking === "select") vec.sv = v; else vec.v = v; }

// A handle for the console and for headless tests. Not an API.
const debug = window.gates = {
  cam, redraw, renderMs: 0,
  get board() { return board; },
  get sim() { return sim; },
  clock,
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
let lastNow = null;    // the previous playing frame's time, or null
function redraw() {
  if (queued) return;
  queued = true;
  requestAnimationFrame(paint);
}

function paint(now) {
  queued = false;
  if (sim) {
    // An edit since the last frame re-derives the runs and starts fresh --
    // here rather than at each edit site, so no edit path can forget it.
    sim.sync();
    if (!clock.paused) {
      // A stalled frame (a backgrounded tab) is clamped, and the clock caps
      // the backlog too, so coming back never freezes to catch up.
      const dt = lastNow === null ? 0 : Math.min(0.25, (now - lastNow) / 1000);
      const n = clock.advance(dt);
      for (let i = 0; i < n; i++) sim.step();
    }
    shell.setTick(sim.tick);
    if (inspectRow >= 0) panel.setLive(liveText(inspectRow));
  }
  lastNow = sim && !clock.paused ? now : null;
  recordStep();
  const t0 = performance.now();
  const info = board ? board.describe(inspectRow) : null;
  const ghosts = [
    selGhost(),
    // The run's own start and tip are ringed; its copies are not.
    ...(run ? runCopies().copies.map((c, k) => ({ ...ghostOf(c.plan, c.over), ...(k === 0 ? { start: c.start, end: c.end } : {}) }))
        : placements().map(placeGhost)),
    boxRect ? { rect: boxRect } : null,
  ];
  // The belt a run starts on draws only as the run's ghost, at its new facing.
  const hide = run ? runCopies().copies.filter(c => c.over).map(c => c.start) : null;
  render(ctx, board, cam, dpr, { ...view, selected: info, ghosts, sim, alpha: clock.alpha, hide, vector: vectorMarks(),
                                indexLabels: indexLabels(), noteSel: noteEdit, noteGrips: labelHeld() });
  debug.renderMs = performance.now() - t0;
  if (sim && !clock.paused) redraw();
}

// --- undo ------------------------------------------------------------------

// Commit the board as a new step if it changed since the last one and is
// stable. Called every frame, so all the edits one gesture makes in one go
// (clearing the way, then building) are one step.
// The board's edit key: entities and labels both. A label edit is an undo
// step but not a board edit -- it must not restart the simulation.
const editKey = () => `${board.revision}/${board.notesRev}`;

function recordStep() {
  if (!board || sel || editKey() === committed) return;
  // A label being dragged or typed into is a change in progress too: one
  // step when the drag ends or the editor closes, not one per frame.
  if (grab?.note || noteEdit >= 0) return;
  history.commit(board.snapshot());
  committed = editKey();
  syncHistory();
}

function syncHistory() {
  // Something lifted is an uncommitted change Undo can throw away.
  shell.setHistory(!!sel || (!!board && editKey() !== committed) || history.canUndo, history.canRedo);
}

// Put a history state back. Whatever was in progress -- a run, a ghost, a
// lifted selection -- is dropped: it belonged to the board being replaced.
function restoreState(state) {
  run = null;
  place = null;
  grab = null;
  boxRect = null;
  sel = null;
  shell.setSelection(0);
  if (shell.state.pasteStamp !== null) shell.setPasting(null);
  board.restore(state);
  noteEdit = -1;
  committed = editKey();
  if (inspectRow >= 0 && !board.describe(inspectRow)) inspect(-1);
  else syncInspect();
  syncBar();
  syncHistory();
}

function undo() {
  if (!board) return;
  // An uncommitted change (something lifted, or an edit not yet recorded)
  // is undone first, back to the current state.
  if (sel || editKey() !== committed) { restoreState(history.current); return; }
  const s = history.undo();
  if (s) restoreState(s);
}

function redo() {
  if (!board) return;
  if (sel || editKey() !== committed) return;
  const s = history.redo();
  if (s) restoreState(s);
}

// The checkpoint is the board as it stands. Something lifted is put down
// first, so the checkpoint is a whole board.
function checkpoint() {
  if (!board) return;
  dropAll();
  recordStep();
  history.setCheckpoint(history.current);
  syncHistory();
}

// What the Status pane's live readout says about a row: a gate's channels,
// or the run a belt is on and what it carries. Recomputed every frame; the
// panel touches the DOM only when the text changes.
function liveText(row) {
  const info = board?.describe(row);
  if (!info || !sim) return null;
  const fmt = (v) => (Number.isInteger(v) ? String(v) : String(parseFloat(v.toPrecision(4))));
  if (info.td.kind === "structure") {
    const g = sim.gateOf.get(row);
    if (!g) return null;
    const lines = [];
    if (g.sim.n_in) {
      const ins = [];
      for (let c = 0; c < g.sim.n_in; c++) ins.push(`${c}: ${sim.inHeld(row, c) ? fmt(sim.inValue(row, c)) : "–"}`);
      lines.push(`in   ${ins.join("   ")}`);
    }
    if (g.sim.n_out) {
      const outs = [];
      for (let c = 0; c < g.sim.n_out; c++) {
        const dangling = !g.drains.includes(c);
        outs.push(`${c}: ${sim.outHeld(row, c) ? fmt(sim.outValue(row, c)) : dangling ? "×" : "–"}`);
      }
      lines.push(`out  ${outs.join("   ")}`);
    }
    lines.push(`tick ${sim.tick}`);
    return lines.join("\n");
  }
  const seg = sim.segmentAt(info.x, info.y);
  if (!seg) return null;
  const items = sim.items(seg);
  const shown = items.slice(0, 12).map(it => fmt(it.value)).join(" ");
  const ends = `${seg.srcPort ? "fed" : "unfed"} → ${seg.dstPort ? "drains" : "dangles"}`;
  return [
    `run ${seg.sid}: ${seg.length} long, ${items.length} item${items.length === 1 ? "" : "s"}`,
    ends,
    items.length ? `head→ ${shown}${items.length > 12 ? " …" : ""}` : "empty",
  ].join("\n");
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
  if (run) {
    // The button names the route in use; dimmed when the other one was
    // forced because only it gets through. A press still flips the
    // player's preference, which shows again once both routes work.
    const { route, forced } = runCopies();
    p = { kind: "belt", routeName: ROUTE_NAMES[route], routeIcon: ROUTE_ICONS[route], forced };
  }
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
  // Setting the value already held is not a change: an edit restarts the
  // simulation, and re-tapping the lit swatch should not.
  if (info.emit !== null) { if (Math.fround(v) !== info.emit) board.setEmit(inspectRow, v); }
  else if (info.seedOn !== null) {
    // Tapping the lit number again unseeds the belt, as on the raw belt tool.
    const off = info.seedOn && info.seedValue === v;
    board.setSeed(inspectRow, !off, off ? info.seedValue : v);
  }
  syncInspect();
}

// --- the vector picker ------------------------------------------------------
//
// Tap Build while in Build to open it; the bar becomes Cancel / Reset /
// Less / More and the thumb pads turn the vector (rotate a quarter, flip
// negates x) and count (-, +). The first drag after opening sets the ANCHOR
// where it starts; every drag moves the offset by the finger's motion.
// Tapping Build again -- or tapping the board -- confirms: the picker
// closes and vectored building stays on (Build stays tinted). Cancel closes
// it and turns vectored building off. Whatever is being planned (a ghost,
// a run) keeps showing, copies and all, while the vector changes.

function syncVector() {
  shell.setVector({ on: vec.on, picking: vec.picking, count: vec.count });
  syncBar();
}

function openBuildPicker() {
  vec.picking = "build";
  vec.on = true;
  vec.anchor = null;
  grab = null;
  syncVector();
}

// Close the picker. Building: confirmed keeps vectored building on; not
// confirmed (Cancel) turns it off.
function closePicker(confirmed) {
  const which = vec.picking;
  vec.picking = null;
  vec.anchor = null;
  grab = null;
  if (which === "build" && !confirmed) vec.on = false;
  syncVector();
}

function vectorTurn(cw) { setPickV(rotateVec(pickV(), cw)); syncVector(); }
function vectorFlip() { setPickV(flipVec(pickV())); syncVector(); }
function vectorReset() { setPickV([0, 0]); syncVector(); }
function vectorCount(more) { vec.count = more ? moreCount(vec.count) : lessCount(vec.count); syncVector(); }

// The picker's highlights: the anchor and each copy's cell along v.
function vectorMarks() {
  if (!vec.picking || !vec.anchor) return null;
  const n = vec.picking === "build" ? vec.count : 2;
  const cells = offsets(pickV(), n).map(([dx, dy]) => [vec.anchor[0] + dx, vec.anchor[1] + dy]);
  return { cells };
}

// --- labels -----------------------------------------------------------------------
//
// The desktop's text-box annotations (notes.js). With the Label tool held:
// a tap on empty board (or over machines -- labelling them is the point)
// makes a label there and opens its editor; a tap on a label edits it; a
// drag from a label moves it. In the Hand, a tap on a label edits it too,
// before the entity under it is inspected: labels draw over the board, so
// they win the tap, as on the desktop. A label's box is fitted to its text
// on every edit (the desktop wraps at the box width, so it reads the same
// there). Hidden labels (View > labels) take no taps.
//
// A new label gets a default-sized box (it is not fitted to its text: the
// player sizes it). With the Label tool held every label wears a grip at
// its bottom-right corner; a drag from the grip resizes, the top-left
// staying put, and a drag from anywhere else on the box moves it.

let noteEdit = -1;    // the label being edited, by index, or -1
let lastNote = { size: 1, colour: 0 };   // what a new label starts as

const labelHeld = () => shell?.state.mode === "build" && shell.heldTool?.kind === "label" && !vec.picking;
const notesOn = () => view.notes !== false;

function noteHit(sx, sy) {
  if (!board || !notesOn()) return -1;
  const [wx, wy] = cam.screenToWorld(sx, sy);
  return noteAt(board.notes, wx, wy);
}

// Is screen point (sx, sy) on a label's resize grip? The grip is sized and
// hit in screen pixels, so it is the same size at every zoom.
function gripHit(sx, sy) {
  if (!board || !notesOn()) return -1;
  return gripAt(board.notes, (x, y) => cam.worldToScreen(x, y), sx, sy, GRIP_PX);
}
const GRIP_PX = 22;

function touchNotes() { board.notesRev++; redraw(); }

function createNote(wx, wy) {
  const n = { kind: "text", colour: lastNote.colour, size: lastNote.size, text: "", rect: newRect(wx, wy, lastNote.size) };
  board.notes.push(n);
  editNote(board.notes.length - 1, true);
}

// Open the editor on label i. A label left empty when the editor closes is
// removed: an empty label nobody can read is litter.
function editNote(i, fresh = false) {
  noteEdit = i;
  touchNotes();
  const n = board.notes[i];
  shell.openNoteEditor({ text: n.text, size: n.size, colour: n.colour }, {
    onChange: (c) => {
      Object.assign(n, c);
      if (c.size !== undefined || c.colour !== undefined) lastNote = { size: n.size, colour: n.colour };
      touchNotes();
    },
    onDelete: () => { removeNote(n); },
    onClose: () => {
      noteEdit = -1;
      if (!String(n.text).trim()) removeNote(n);
      else touchNotes();
    },
  });
  void fresh;
}

function removeNote(n) {
  const i = board.notes.indexOf(n);
  if (i >= 0) board.notes.splice(i, 1);
  noteEdit = -1;
  touchNotes();
}

// --- indexing -------------------------------------------------------------------
//
// The selection's Index swatch numbers its Sources and belts -- in the order
// they were selected, or by position (left to right, rows from the bottom
// up) -- and the numbers are drawn on them while selecting goes on as
// normal: tap one off and back on and it goes to the end. Values… then
// writes each one the table's value for its number, a * x + b (a default,
// D, is 0 for a Source and no seed for a belt), into the lifted entries,
// and puts the selection down: one edit, one undo step. Indexing is one-shot:
// the order is not kept anywhere once applied.

// The indexed entries: { i (index into the selection), e (absolute entry) },
// in index order.
function indexed() {
  if (!sel || !shell.state.indexing) return [];
  const abs = selAbs();
  const items = abs.map((e, i) => ({ i, e })).filter(({ e }) => e.type === "source" || e.type === "belt");
  if (shell.state.indexing === "natural") {
    return naturalOrder(items.map(({ e }) => ({ x: e.dx, y: e.dy }))).map(k => items[k]);
  }
  return items;
}

// Each indexed entity's number, at the middle of its footprint.
function indexLabels() {
  return indexed().map(({ e }, n) => {
    const { w, h } = entrySize(T, e);
    return { x: e.dx + w / 2, y: e.dy + h / 2, n };
  });
}

function indexValues(o) {
  const list = indexed();
  const table = tableValues(o.kind, list.length, { file: o.file, seed: o.seed, limit: T.sim.VALUE_LIMIT });
  return { list, values: finalValues(table, o.a, o.b, T.sim.VALUE_LIMIT) };
}

function indexPreview(o) {
  const { list, values } = indexValues(o);
  if (!list.length) return { n: 0, error: "Nothing indexed: select some Sources or belts." };
  const fmt = (v) => (v === DEFAULT ? "default" : String(parseFloat(v.toPrecision(6))));
  const lines = list.slice(0, 12).map(({ e }, n) => `#${n}  ${e.type === "source" ? "Source" : "belt  "}  ${fmt(values[n])}`);
  if (list.length > 12) lines.push(`… and ${list.length - 12} more`);
  return { n: list.length, text: lines.join("\n") };
}

function indexApply(o) {
  const { list, values } = indexValues(o);
  if (!list.length) return;
  list.forEach(({ i, e }, n) => {
    const entry = sel.stamp.entries[i];
    const v = values[n];
    if (e.type === "source") entry.emit = v === DEFAULT ? 0 : v;
    else entry.seed = v;                 // null: no seed
  });
  shell.setIndexing(null);
  dropAll();
  redraw();
}

// --- vectored selection ------------------------------------------------------
//
// The selection's Vector swatch opens the picker in its "select" form: the
// anchor is a cell of one of the selected entities (the first), the offset
// starts on it, and the bar is Cancel / Reset / Confirm. On Confirm (or a
// tap on the board) the selection, as it now sits, is the PATTERN: if the
// board holds the whole pattern again at 1 * v -- every entity of the same
// type, facing and flip at the same place, settings (a Source's value, a
// seed, underground pairing) ignored -- those are added, then at 2 * v, and
// so on until the pattern breaks. Added in order, chain step by chain step
// and the pattern's own order within a step, so it is a selection ORDER too.

function openSelectPicker() {
  if (!sel) return;
  const first = selAbs()[0];
  vec.picking = "select";
  vec.sv = [0, 0];
  vec.anchor = [first.dx, first.dy];
  grab = null;
  syncVector();
}

function confirmSelectVector() {
  const v = vec.sv;
  closePicker(true);
  if (!sel || isZero(v)) return;
  const pattern = selAbs();
  const rows = [];
  for (const off of followChain(v, (o) => {
    const found = pattern.map(e => matchRow(e, e.dx + o[0], e.dy + o[1]));
    if (found.some(r => r < 0)) return false;
    rows.push(...found);
    return true;
  })) void off;
  if (rows.length) lift(rows);
}

// The board row matching entry `e` with its origin at (x, y): same type,
// facing, flip and underground role -- or -1. Settings are not compared.
function matchRow(e, x, y) {
  const row = board.entityAt(x, y);
  if (row < 0) return -1;
  const E = T.entity;
  if ((board.field(row, E.F_ORIGIN_X) | 0) !== x || (board.field(row, E.F_ORIGIN_Y) | 0) !== y) return -1;
  const type = board.field(row, E.F_TYPE) | 0;
  if (T.types[type].name !== e.type) return -1;
  if ((board.field(row, E.F_DIR) | 0) !== e.dir) return -1;
  if (!!board.field(row, E.F_FLIPPED) !== !!e.flip) return -1;
  if (type === T.cell.TYPE_UNDER && !!board.field(row, E.F_UNDER_IS_EXIT) !== !!e.exit) return -1;
  return row;
}

// --- taps -------------------------------------------------------------------

function onTap(sx, sy) {
  if (!board) return;
  const [wx, wy] = cam.screenToWorld(sx, sy);
  const x = Math.floor(wx), y = Math.floor(wy);
  const mode = shell.state.mode;
  if (mode === "hand") {
    // A label wins the tap: it draws over the board.
    const ni = noteHit(sx, sy);
    if (ni >= 0) { editNote(ni); return; }
    // A tap on bare board closes the window, as on the desktop.
    inspect(board.entityAt(x, y));
    return;
  }
  if (mode !== "build") return;
  // A tap while the picker is open confirms it, as Build does.
  if (vec.picking) { if (vec.picking === "build") closePicker(true); else confirmSelectVector(); return; }
  // The Label tool takes every tap: one on a label edits it, anywhere else
  // makes a new one there -- over machines too.
  if (labelHeld()) {
    if (!notesOn()) return;
    const ni = noteHit(sx, sy);
    if (ni >= 0) editNote(ni); else createNote(wx, wy);
    return;
  }
  const tool = placer();
  // Something already planned keeps the tap, even over an entity.
  if (place || shell.activeStamp) {
    if (!place) startPlace(wx, wy);
    else if (onGhost(sx, sy)) placeOnce();
    else {
      placeOnce();
      // A paste ends the way placing does: a tap elsewhere places and
      // leaves -- and with nothing selected, that is back to the tool.
      if (tool?.pasting) exitPaste();
      else endPlace();
    }
    return;
  }
  if (run) {
    // A tap on the tip turns it (straight, right, left). Any other second
    // tap places the run exactly as shown; it does not move the tip to
    // where the finger landed.
    if (onCells(sx, sy, run.end[0], run.end[1], 1, 1)) {
      // A one-cell run started on a placed belt: the tap means "that belt",
      // so the run is dropped and the belt selected. Its rotate buttons
      // still re-aim it, and a tap elsewhere still builds.
      const one = run.start[0] === run.end[0] && run.start[1] === run.end[1];
      if (one && run.over && overStart(run.start[0], run.start[1])) {
        const row = run.over;
        cancelRun();
        lift([row]);
        return;
      }
      turnTip(run, true);
      redraw();
    } else commitRun();
    return;
  }
  if (sel) { selectTap(x, y); return; }
  // Idle: a tap on an entity selects it, whatever the hand holds; a tap on
  // empty board is the held tool's.
  const row = board.entityAt(x, y);
  // With the belt planner held, a tap on a placed belt starts a run FROM it:
  // that belt is turned to follow the run on Build. Machines and underground
  // ends still select; a long press still selects a belt's whole run.
  if (row >= 0 && !tool && beltHeld() && isBeltRow(row)) startRun(x, y, row);
  else if (row >= 0) lift([row]);
  else if (tool) startPlace(wx, wy);
  else if (beltHeld() && x >= 0 && y >= 0 && x < board.width && y < board.height) startRun(x, y);
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

function isBeltRow(row) {
  return (board.field(row, T.entity.F_TYPE) | 0) === T.cell.TYPE_BELT;
}

// The run's start cell, when it is a placed belt the run will turn, counts
// as free: the plan may claim it.
function overStart(x, y) {
  return !!run?.over && x === run.start[0] && y === run.start[1] && board.entityAt(x, y) === run.over;
}

// The route actually taken, and its plan: see chooseRoute() in planner.js.
// `free` counts the start cell when the run turns the belt it starts on.
function routeChoice() {
  return chooseRoute(run, (x, y) => overStart(x, y) || board.isFree(x, y), T.under.MAX_GAP);
}

function currentPlan() {
  if (!run || !board) return null;
  return routeChoice().plan;
}

// Every run a Build would lay: the run, and with the vector on, a copy per
// k whose start is start + k * v0 (the vector the run began with) and whose
// end is end + k * v (the vector now). So once a run has begun, picking a
// new vector swings the copies' ENDS about their fixed starts: a bus laid
// east and re-vectored north fans its tips up a column.
//
// Copies are planned in sequence over the claims of the ones before -- their
// belts and underground ends; a tunnel's buried tiles stay free -- so a copy
// may tunnel under an earlier one, and a copy that does not get through
// (routeWorks: a blocked last cell excused) is dropped. Copy 0 always shows,
// red where blocked. A copy whose start lands on a placed belt turns it, as
// the run itself does.
//
// ONE ROUTE FOR THE WHOLE BUS, so it stays parallel -- and it is chosen for
// the bus, not for copy 0 alone: the player's route, unless the other gets
// strictly more of the copies through. (A bus whose starts sit in a row
// can never go H-first along that row: every copy's start lies on copy 0's
// first leg. Copy 0's own route would doom all the others.)
// Returns { route, forced, copies: [{ plan, over, start, end }] }.
function runCopies() {
  if (!run || !board) return { route: run?.route, forced: false, copies: [] };
  if (!vectoring()) {
    const { route, forced, plan } = routeChoice();
    return { route, forced, copies: [{ plan, over: run.over && overStart(run.start[0], run.start[1]) ? run.over : 0, start: run.start, end: run.end }] };
  }
  const key = `${board.revision}|${JSON.stringify(run)}|${vec.v}|${vec.count}`;
  if (busCache?.key === key) return busCache.bus;
  const mine = busFor(run.route);
  const otherRoute = run.route === ROUTE_H_FIRST ? ROUTE_V_FIRST : ROUTE_H_FIRST;
  const other = busFor(otherRoute);
  const bus = other.through > mine.through
    ? { route: otherRoute, forced: true, copies: other.copies }
    : { route: run.route, forced: false, copies: mine.copies };
  busCache = { key, bus };
  return bus;
}
let busCache = null;

// The bus laid on one route: its kept copies, and how many get through.
function busFor(route) {
  const v0 = run.v0 && !isZero(run.v0) ? run.v0 : vec.v;
  const claims = new Claims((x, y) => board.isFree(x, y));
  const copies = [];
  let through = 0;
  for (let k = 0; k < vec.count; k++) {
    const start = [run.start[0] + k * v0[0], run.start[1] + k * v0[1]];
    const end = [run.end[0] + k * vec.v[0], run.end[1] + k * vec.v[1]];
    if (!inBoard(start) || !inBoard(end)) continue;
    const row = board.entityAt(start[0], start[1]);
    const over = k === 0 ? (run.over && overStart(start[0], start[1]) ? run.over : 0)
                         : row >= 0 && isBeltRow(row) ? row : 0;
    const free = (x, y) => (over && x === start[0] && y === start[1]) || claims.free(x, y);
    const plan = planRun({ ...run, route, start, end, over }, free, T.under.MAX_GAP);
    const ok = routeWorks(plan);
    if (ok) through++;
    if (k > 0 && !ok) continue;
    claims.claim(plan.cells.filter(p => p.kind !== "blocked" && p.kind !== "buried").map(p => [p.x, p.y]));
    copies.push({ plan, over, start, end });
  }
  return { copies, through };
}

const inBoard = ([x, y]) => x >= 0 && y >= 0 && x < board.width && y < board.height;

// The plan in the renderer's own shapes, so ghosts draw with the same code
// as the real thing.
function ghostOf(plan, over = run?.over && overStart(run.start[0], run.start[1]) ? run.over : 0) {
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
    // The placed belt a run starts on keeps its seed, so its ghost shows it.
    let seed = 0;
    if (i === 0 && over && board.field(over, T.entity.F_SEED_ON)) {
      seed = board.field(over, T.entity.F_SEED_VALUE) > 0 ? 1 : -1;
    }
    belts.push({ x: c.x, y: c.y, dir: c.dir, inDir, seed });
  });
  return { belts, unders, blocked };
}

function startRun(x, y, over = 0) {
  const tool = shell.heldTool;
  // Starting on a placed belt, a one-cell run keeps that belt's facing until
  // turned, so Build with nothing changed changes nothing.
  const dir = over ? board.field(over, T.entity.F_DIR) | 0 : tool.dir | 0;
  // A slot saved while the line route existed may still say 2.
  const route = ROUTES.includes(tool.route) ? tool.route : ROUTE_H_FIRST;
  run = { start: [x, y], end: [x, y], route, tipTurn: 0, singleDir: dir, defaultDir: dir, over,
          v0: vectoring() ? vec.v.slice() : null };
  syncBar();
}

function cancelRun() {
  run = null;
  grab = null;
  syncBar();
}

function commitRun() {
  const { copies } = runCopies();
  copies.forEach(({ plan, over }, k) => {
    // Force build: what blocks the run is deleted and the run goes down
    // over it. Only the cells the run could not have -- not the ones it
    // tunnels under, which it never wanted -- and only for the run itself:
    // a copy that does not get through was never kept.
    if (k === 0 && TUNE.forceBuild) {
      const blocked = plan.cells.filter(c => c.kind === "blocked");
      if (blocked.length) {
        clearCells(blocked);
        plan = { ...plan, cells: plan.cells.map(c => (c.kind === "blocked" ? { ...c, kind: "belt" } : c)) };
      }
    }
    if (over && board.entityAt(plan.cells[0].x, plan.cells[0].y) === over) {
      // The belt a run starts on: turned in place to its planned facing, so
      // its row and seed survive. If the planner made it an underground
      // entrance instead (an obstacle right after the start), it is replaced.
      const c = plan.cells[0];
      if (c.kind === "belt") board.turnBelt(over, c.dir);
      else board.eraseRows([over]);
    }
    board.buildPlan(plan, { derive: false });
  });
  if (copies.length) board.derive();
  cancelRun();
}

// Delete whatever occupies these cells (whole entities: one cell of a
// machine takes the machine, as erasing does on the desktop).
function clearCells(cells) {
  const rows = new Set();
  for (const c of cells) { const r = board.entityAt(c.x, c.y); if (r >= 0) rows.add(r); }
  if (!rows.size) return;
  if (rows.has(inspectRow)) inspect(-1);
  board.eraseRows([...rows]);
}

// --- placing a machine, a raw belt or a stamp -----------------------------
//
// A tap starts placing: a ghost centred on the tap. A drag moves it. Then:
//   tap ON the ghost (or within the tap margin of it)  place it, and keep the
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
  const w = tool.kind === "stamp" ? tool.stamp.w : tool.kind === "rawbelt" ? 1 : orientOf(tool).w;
  const h = tool.kind === "stamp" ? tool.stamp.h : tool.kind === "rawbelt" ? 1 : orientOf(tool).h;
  return placementAt(tool, originFor(w, h, place.px, place.py));
}

function orientOf(tool) {
  return T.types[typeIdOf(tool.type)].orient[`${tool.dir | 0}${tool.flip ? 1 : 0}`];
}

// The cells a placement would occupy, as [x, y].
function placementCells(pl) {
  if (pl.kind === "stamp") return stampCells(T, pl.tool.stamp, pl.origin[0], pl.origin[1]).map(([x, y]) => [x, y]);
  const out = [];
  for (let dx = 0; dx < pl.w; dx++) for (let dy = 0; dy < pl.h; dy++) out.push([pl.origin[0] + dx, pl.origin[1] + dy]);
  return out;
}

// Is the vector making copies right now?
const vectoring = () => vec.on && !isZero(vec.v);

// Every placement a tap would make: the ghost, and with the vector on, its
// copies at k * v -- each laid in sequence and dropped unless it fits whole
// on the board as the earlier copies leave it. The original is always kept,
// red where it is blocked, as without a vector.
function placements() {
  const base = currentPlacement();
  if (!base) return [];
  if (!vectoring()) return [base];
  return layCopies(offsets(vec.v, vec.count), (x, y) => board.isFree(x, y), (off, claims) => {
    const pl = off[0] || off[1] ? placementAt(base.tool, [base.origin[0] + off[0], base.origin[1] + off[1]]) : base;
    const cells = placementCells(pl);
    const ok = claims.fits(cells);
    // A partly blocked original stamp still builds what fits.
    const claim = ok ? cells : pl.kind === "stamp" ? cells.filter(([x, y]) => claims.free(x, y)) : [];
    return { pl, cells, ok, claim };
  }).map(c => c.pl);
}

// The placement of `tool` with its footprint's corner at `origin`.
function placementAt(tool, origin) {
  if (tool.kind === "stamp") {
    const st = tool.stamp;
    const at = st.entries.map(e => [origin[0] + e.dx, origin[1] + e.dy]);
    const ghost = stampGhost(board, st.entries, at);
    return { tool, kind: "stamp", origin, w: st.w, h: st.h, entries: st.entries, at, ghost, blocked: ghost.blocked };
  }
  const dir = tool.dir | 0, flip = !!tool.flip;
  if (tool.kind === "rawbelt") {
    const free = board.isFree(origin[0], origin[1]);
    return { tool, kind: "rawbelt", origin, dir, w: 1, h: 1, value: toolValue(tool),
             blocked: free ? [] : [{ x: origin[0], y: origin[1] }] };
  }
  const type = typeIdOf(tool.type);
  const o = T.types[type].orient[`${dir}${flip ? 1 : 0}`];
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

// Build what the ghosts show: the original and every copy the vector kept,
// in order, with one re-derive for the lot. A machine or raw belt only if
// every cell is free; a stamp, whatever of it fits. Returns whether
// anything was placed.
function placeOnce() {
  const pls = placements();
  let any = false;
  pls.forEach((pl, k) => { if (placeOne(pl, k === 0)) any = true; });
  if (any) board.derive();
  redraw();
  return any;
}

// One placement, without re-deriving. Each is checked against the board as
// the ones before it left it. Force build clears the way for the original
// machine or raw belt only -- never for a copy, and never for a stamp,
// which already places whatever fits.
function placeOne(pl, original) {
  if (pl.kind === "stamp") return board.placeEntries(pl.entries, pl.at, { derive: false }).size > 0;
  const blocked = placementCells(pl).filter(([x, y]) => !board.isFree(x, y));
  if (blocked.length) {
    if (!original || !TUNE.forceBuild) return false;
    clearCells(blocked.map(([x, y]) => ({ x, y })));
  }
  const [x, y] = pl.origin;
  if (pl.kind === "rawbelt") board.spawnBelt(x, y, pl.dir, pl.value);
  else board.spawnStructure(pl.type, pl.origin, pl.dir, pl.flip, { emit: pl.value, derive: false });
  return true;
}

// Is screen point (sx, sy) on the w x h cell box at (x, y), or within
// TUNE.tapMarginPx of it?
function onCells(sx, sy, x, y, w, h) {
  const [x0, y1] = cam.worldToScreen(x, y + h);
  const [x1, y0] = cam.worldToScreen(x + w, y);
  const m = TUNE.tapMarginPx;
  return sx >= x0 - m && sx <= x1 + m && sy >= y1 - m && sy <= y0 + m;
}

// On the ghost, or on any of its copies.
function onGhost(sx, sy) {
  return placements().some(pl => onCells(sx, sy, pl.origin[0], pl.origin[1], pl.w, pl.h));
}

// --- the selection ----------------------------------------------------------
//
// Not a mode: tapping an entity in Build starts it, and it ends -- handing
// the taps back to the held tool -- whenever nothing is selected.
//
//   tap a board entity        select it: lifted off the board into `sel`
//   tap a selected entity     deselect it: placed back where it now is,
//                             if it fits -- otherwise it stays selected
//   tap empty board           deselect everything that fits
//   drag from a selected one  move the whole selection
//   drag with Box armed       a rectangle that adds what it touches
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
  // Nothing left selected: the selection's picker has nothing to repeat,
  // and there is nothing left to index.
  if (!sel && vec.picking === "select") closePicker(false);
  if (!sel) shell.setIndexing(null);
  syncHistory();
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

// Add every board entity the box touches to the selection. It only adds:
// what is already selected stays selected.
function boxAdd(r) {
  const rows = new Set();
  for (let y = r.y; y < r.y + r.h; y++) {
    for (let x = r.x; x < r.x + r.w; x++) {
      const row = board.entityAt(x, y);
      if (row >= 0) rows.add(row);
    }
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

// Delete, Cut and Copy act on the selection; none of them is a mode. Cut
// and Copy start pasting with the ghost exactly where the selection was --
// the selection's own pose, so the stamp lands cell for cell on it. A cut
// is therefore a selection turned into a stamp you can carry off or
// cancel; a copy's ghost sits over the originals.
function selectCommand(c) {
  if (!sel) return;
  if (c === "delete") { setSel([]); return; }
  const pose = [sel.px, sel.py];
  const id = shell.addStamp(sel.stamp);
  if (c === "cut") { setSel([]); enterPaste(id, pose); return; }
  // Copy: the selection goes back down. If all of it fit, there is nothing
  // left selected and the copy is ready to paste; anything that could not go
  // back stays selected for the player to deal with.
  dropAll();
  if (!sel) enterPaste(id, pose);
}

// Entering paste puts the selection down first, and deletes whatever of it
// could not go down: a paste never coexists with a selection. `pose`, if
// given, puts the ghost up straight away at that centre point.
function enterPaste(id, pose = null) {
  dropAll();
  if (sel) setSel([]);
  place = pose ? { px: pose[0], py: pose[1] } : null;
  shell.setPasting(id);
  syncBar();
}

// --- long press: a whole belt run ------------------------------------------
//
// In Build with nothing planned, or while selecting, a long press on a belt
// or underground selects the whole run through it (core/segments.py's
// segment). If every cell of that run is already selected, it deselects
// them instead. The run is traced over the board AND the selection
// together, so a run that is partly lifted is still one run.
function onLongPress(sx, sy) {
  if (!board || shell.state.mode !== "build" || vec.picking) return false;
  if (run || place || shell.activeStamp) return false;
  const [x, y] = cellUnder(sx, sy);
  const abs = sel ? selAbs() : [];
  const at = abs.map(e => [e.dx, e.dy]);
  const W = board.width;
  const lifted = new Map();   // cell key -> selection index, belts and undergrounds only
  abs.forEach((e, i) => { if (e.type === "belt" || e.type === "under") lifted.set(e.dy * W + e.dx, i); });
  const node = (cx, cy) => {
    const i = lifted.get(cy * W + cx);
    if (i !== undefined) {
      const e = abs[i];
      return { kind: e.type, dir: e.dir, exit: !!e.exit, key: `s${i}`, sel: i,
               partner: e.partner !== null && e.partner !== undefined ? at[e.partner] : null };
    }
    return board.beltNode(cx, cy);
  };
  const emits = emitsWith(board, emitMask(board, abs, at));
  const cells = traceRun(node, (cx, cy, d) => board.incoming(cx, cy, d, emits), x, y);
  if (!cells.length) {
    // A hold on a selected entity stays a press: a slow drag from it still
    // moves the selection, as it always did.
    if (sel && selIndexAt(x, y) >= 0) return false;
    // Anywhere else but a belt, the hold arms a box selection at the press,
    // drawn at once so the hold reads as having taken. The finger now drags
    // the box out; lifting it without moving does nothing.
    const world0 = cam.screenToWorld(sx, sy);
    grab = { box: world0, held: [sx, sy] };
    boxRect = rectOf(world0, world0);
    redraw();
    return "drag";
  }
  const rows = cells.filter(c => c.row !== undefined).map(c => c.row);
  if (rows.length) lift(rows);
  else drop(cells.map(c => c.sel));
  return true;
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

// How far a held finger must move before its box counts as dragged (CSS px).
const HELD_SLOP = 8;

function cellUnder(sx, sy) {
  const [wx, wy] = cam.screenToWorld(sx, sy);
  return [Math.floor(wx), Math.floor(wy)];
}

const dragHooks = {
  // (sx, sy): where the press was. Called with no point after a pinch drops
  // back to one finger, which never starts a selection drag of its own.
  wants(sx, sy) {
    if (vec.picking) return sx !== undefined;
    if (labelHeld()) return sx !== undefined && (gripHit(sx, sy) >= 0 || noteHit(sx, sy) >= 0);
    if (run || place) return true;
    if (!shell.selecting) return false;
    if (sx === undefined) return false;
    if (shell.state.boxArmed) return true;
    return selIndexAt(...cellUnder(sx, sy)) >= 0;
  },
  begin(sx, sy) {
    const world0 = cam.screenToWorld(sx, sy);
    if (labelHeld()) {
      // The grip first: grabbing the corner must never read as grabbing the box.
      const g = gripHit(sx, sy);
      const i = g >= 0 ? g : noteHit(sx, sy);
      if (i >= 0) grab = { note: board.notes[i], r0: board.notes[i].rect.slice(), world0, resize: g >= 0 };
      return;
    }
    if (vec.picking) {
      // The first drag after opening the picker sets the anchor where it starts.
      if (!vec.anchor) vec.anchor = [Math.floor(world0[0]), Math.floor(world0[1])];
      grab = { vec: true, v0: pickV().slice(), world0 };
      redraw();
      return;
    }
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
    if (grab.box) {
      if (grab.held && Math.hypot(sx - grab.held[0], sy - grab.held[1]) > HELD_SLOP) grab.held = null;
      boxRect = rectOf(grab.box, [wx, wy]);
      redraw();
      return;
    }
    const dx = wx - grab.world0[0], dy = wy - grab.world0[1];
    if (grab.note) {
      // A label moves with the finger, kept to a thousandth of a cell as the
      // desktop saves it.
      const r = (v) => Math.round(v * 1000) / 1000;
      const [x0, y0, w0, h0] = grab.r0;
      // Resizing: the top-left stays put; world +Y is up and the box hangs
      // down from it, so dragging the grip down (dy < 0) makes it taller.
      grab.note.rect = grab.resize
        ? [x0, y0, r(Math.max(MIN_W, w0 + dx)), r(Math.max(MIN_H, h0 - dy))]
        : [r(x0 + dx), r(y0 + dy), w0, h0];
      touchNotes();
      return;
    }
    if (grab.vec) {
      const v = [grab.v0[0] + Math.round(dx), grab.v0[1] + Math.round(dy)];
      const cur = pickV();
      if (v[0] !== cur[0] || v[1] !== cur[1]) { setPickV(v); syncVector(); }
      return;
    }
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
    syncBar();
  },
  end() {
    // A held box the finger never dragged out selects nothing.
    if (grab?.box && grab.held) boxRect = null;
    if (grab?.box && boxRect) {
      const r = boxRect;
      boxRect = null;
      boxAdd(r);
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
    panel.setBoardName(name.replace(/\.npz$/i, ""));
    sim = new Sim(T, board);
    clock.reset();
    shell.setPaused(true);   // a freshly loaded board waits to be started
    run = null;
    place = null;
    sel = null;
    shell.setSelection(0);
    if (shell.state.pasteStamp !== null) shell.setPasting(null);
    // A loaded board is a fresh history, and its own checkpoint.
    history.reset(board.snapshot());
    noteEdit = -1;
    committed = editKey();
    syncHistory();
    syncBar();
    cam.frame(board.bounds);
    inspect(-1);
  } catch (err) {
    console.error(err);
    alert(`${name}: ${err.message}`);
  }
  redraw();
}

// --- browser saves ----------------------------------------------------------

// Save the board as it stands under `name`. A selection is lifted OFF the
// board, so it is put down first (what fits goes back, as leaving Build
// does); anything that still does not fit would be missing from the save,
// so that is asked about rather than lost quietly.
async function saveLocal(name) {
  if (!board) return;
  name = name || "board";
  dropAll();
  const lifted = sel ? sel.stamp.entries.length : 0;
  if (lifted && !confirm(`${lifted} selected ${lifted === 1 ? "entity does" : "entities do"} not fit back on the board and will not be in the save. Save anyway?`)) return;
  if (listSaves().some(e => e.name === name) && !confirm(`Replace the saved board "${name}"?`)) return;
  try {
    putSave(name, await board.toNpz());
  } catch (err) {
    alert(`Could not save "${name}": ${err.message}`);
    return;
  }
  panel.setBoardName(name);
  panel.setLocalSaves(listSaves(), name);
}

async function loadLocal(name) {
  const bytes = getSave(name);
  if (!bytes) { alert(`"${name}" is not in this browser's storage any more.`); panel.setLocalSaves(listSaves()); return; }
  await show(name, bytes.buffer);
}

// Download a browser save as an .npz the desktop app can load.
function exportLocal(name) {
  const bytes = getSave(name);
  if (!bytes) { alert(`"${name}" is not in this browser's storage any more.`); return; }
  const url = URL.createObjectURL(new Blob([bytes], { type: "application/octet-stream" }));
  const a = document.createElement("a");
  a.href = url;
  a.download = /\.npz$/i.test(name) ? name : `${name}.npz`;
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10000);
}

function deleteLocal(name) {
  if (!confirm(`Delete the saved board "${name}" from this browser?`)) return;
  deleteSave(name);
  panel.setLocalSaves(listSaves());
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
      // Leaving Build closes a picker, keeping the vector as it stands.
      if (s.mode !== "build" && vec.picking) closePicker(true);
      // Leaving Build puts the selection down. Anything that does not fit
      // stays lifted, and is still there on coming back.
      const left = lastMode === "build" && s.mode !== "build";
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
    // ctrl+R: tick 0, every item gone, seeds back on; the factory stays.
    onReset: () => { if (sim) { sim.reset(); clock.reset(); } redraw(); },
    onPause: (paused) => { clock.paused = paused; redraw(); },
    onRate: (rate) => { clock.setRate(rate); redraw(); },
    onBuildTap: () => {
      if (vec.picking === "build") closePicker(true);
      else if (!vec.picking) openBuildPicker();
    },
    onVectorCancel: () => closePicker(false),
    onVectorReset: () => vectorReset(),
    onVectorCount: (more) => vectorCount(more),
    onVectorTurn: (cw) => vectorTurn(cw),
    onVectorFlip: () => vectorFlip(),
    onVectorConfirm: () => confirmSelectVector(),
    onSelectVector: () => { if (vec.picking === "select") closePicker(false); else if (!vec.picking) openSelectPicker(); },
    onIndexPreview: (o) => indexPreview(o),
    onIndexApply: (o) => indexApply(o),
    onUndo: () => undo(),
    onRedo: () => redo(),
    onCheckpoint: () => checkpoint(),
  });
  lastMode = shell.state.mode;
  clock.setRate(shell.state.rate);
  clock.paused = shell.state.paused;

  panel = new Panel(document.getElementById("pane-host"), {
    onLoadSave: (name) => loadBundled(name).catch(e => alert(e.message)),
    onOpenFile: () => fileInput.click(),
    onFit: () => { if (board) cam.frame(board.bounds); redraw(); },
    onView: (v) => { view = v; redraw(); },
    onTune: (t) => { setTune(t); redraw(); },
    onClearStamps: () => shell.clearStamps(),
    onCloseStatus: () => inspect(-1),
    onEmit: (row, v) => { board.setEmit(row, v); shell.setInspectValue(handValueOf(row)); redraw(); },
    onSeed: (row, on, v) => { board.setSeed(row, on, v); shell.setInspectValue(handValueOf(row)); redraw(); },
    onSaveLocal: (name) => saveLocal(name),
    onLoadLocal: (name) => loadLocal(name),
    onExportLocal: (name) => exportLocal(name),
    onDeleteLocal: (name) => deleteLocal(name),
  });
  panel.setLocalSaves(listSaves());
  view = { ...panel.view };
  setTune(panel.dev);

  resize();
  new ResizeObserver(resize).observe(canvas);
  attachGestures(canvas, cam, redraw, { onTap, onLongPress, drag: dragHooks, momentum: () => view.momentum !== false });

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
    // ctrl+Z / ctrl+shift+Z (or ctrl+Y) for a keyboard.
    if ((e.ctrlKey || e.metaKey) && e.code === "KeyZ") { e.preventDefault(); if (e.shiftKey) redo(); else undo(); }
    if ((e.ctrlKey || e.metaKey) && e.code === "KeyY") { e.preventDefault(); redo(); }
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

// Offline: the service worker caches the whole site on first load (sw.js).
// Registration failing (a file:// page, a browser without workers) only
// costs the offline copy.
if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("sw.js").catch(e => console.warn("no offline copy:", e));
}
