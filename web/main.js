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
import { render, BASE_COLOURS } from "./render.js";
import { LayerPanel } from "./layerpanel.js";
import { BACKGROUND, byId, addLayer, removeLayer, renameLayer, setCurrent, isVisible, isActive,
         setForActive, layerStats, mix, parseCss, rgbCss, isolate } from "./layers.js";
import { Shell } from "./ui.js";
import { Panel, STATUS_COLOUR_DEFAULTS } from "./panel.js";
import { ROUTE_NAMES, ROUTE_H_FIRST, ROUTE_V_FIRST, chooseRoute, planRun, routeWorks, turnTip } from "./planner.js";
import { originFor } from "./placer.js";
import { VALUE_CHOICES, hasValue, toolValue } from "./tools.js";
import { makeStamp, mirrored, noteTouches, reversedGate, rotated, shiftNote, stampCells, subset } from "./stamp.js";
import { emitMask, emitsWith, stampGhost } from "./ghost.js";
import { traceRun } from "./segtrace.js";
import { TUNE, setTune } from "./tune.js";
import { Sim, Clock, RATE_MIN, RATE_MAX } from "./sim.js";
import { History } from "./history.js";
import { icon } from "./icons.js";
import { DEFAULT, naturalOrder, tableValues, finalValues } from "./indexer.js";
import { entrySize, typeByName } from "./stamp.js";
import { installPixel, Screen } from "./pixel.js";
import { cropBox, evenDim, frameBox, frameCount } from "./video-params.js";
import { noteAt, gripAt, isText, newRect, MIN_W, MIN_H } from "./notes.js";
import { Claims, followChain, offsets, layCopies, isZero, rotateVec, flipVec, moreCount, lessCount } from "./vector.js";
import { deleteSave, getSave, listSaves, putSave } from "./saves.js";
import { bakeKey, audioRecord, BAKE_DEFAULT_SECONDS, BAKE_MAX_SECONDS } from "./audio-params.js";

const DEFAULT_SAVE = "Default.npz";

const canvas = document.getElementById("board");
const ctx = canvas.getContext("2d", { alpha: false });
const fileInput = document.getElementById("save-file");

const cam = new Camera();
let T = null;
let board = null;
let dpr = 1;
let shell = null;
let panel = null;
let layerPanel = null;
let view = {};
let statusColours = { ...STATUS_COLOUR_DEFAULTS };   // View > status colours
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
  lift: (rows) => { lift(rows); redraw(); },
  dropAll: () => { dropAll(); redraw(); },
  get soundSel() { return soundSel; },
  placement: () => currentPlacement(),
  plan: () => currentPlan(),
};

// --- the pixel overlay --------------------------------------------------------
//
// View > pixel overlay above 0 hands the sim the Screen, and each Pixel's
// fire records its colour on it (pixel.js); at 0 the sim has none and a
// fire records nothing. Switching on starts blank, as does every edit and
// Reset (the sim clears it on a rebuild).
const screen = new Screen();
function syncScreen() {
  if (!sim) return;
  const on = view.pixelAlpha > 0;
  if (on && sim.screen !== screen) { screen.clear(); sim.screen = screen; }
  else if (!on && sim.screen) { sim.screen = null; screen.clear(); }
}

// --- audio -------------------------------------------------------------------
//
// Off by default and loaded on demand: with it off, audio.js and the worklet
// are never fetched and the tick's only cost is Sim.audio's null check.
let audio = null;          // the running engine (audio.js startAudio), or null
let audioStarting = false;
let audioCtx = null;       // its context, from the tap that switched it on
let soundWanted = false;   // the sound button's state (top-left of the board)
// The player's choice, kept across sessions; on until they turn it off.
const SOUND_KEY = "gates.web.sound.v1";
function soundStored() {
  try { return localStorage.getItem(SOUND_KEY) !== "off"; } catch { return true; }
}
function storeSound(on) {
  try { localStorage.setItem(SOUND_KEY, on ? "on" : "off"); } catch { /* not kept */ }
}
// A browser starts audio only from a gesture, so a launch with sound on
// shows the button on and starts it on the first tap or key anywhere --
// pointerup / touchend / keydown, the events that count as activation on
// a touch screen. A context made before then would only be suspended.
// Until it runs, each gesture tries again (a resume can be refused).
function armSound() {
  const evs = ["pointerup", "touchend", "keydown"];
  const go = (e) => {
    if (!soundWanted) return done();
    if (e.target?.closest?.("#sound-btn")) return;   // the button's own click decides
    if (!audio && !audioStarting) setAudio(true);
    else audioCtx?.resume?.();
    if (audioCtx?.state === "running") done();
  };
  const done = () => { for (const ev of evs) document.removeEventListener(ev, go, true); };
  for (const ev of evs) document.addEventListener(ev, go, true);
}
// The sound button: a speaker, crossed out while sound is off.
function setSoundBtn(on) {
  const b = document.getElementById("sound-btn");
  if (!b) return;
  b.innerHTML = icon(on ? "soundOn" : "soundOff");
  b.setAttribute("aria-pressed", String(on));
  b.title = on ? "Sound on (tap for off)" : "Sound off (tap for on)";
}
function setAudio(on) {
  soundWanted = on;
  setSoundBtn(on);
  if (!on) {
    bakedPlay = null;
    if (audio) { audio.close(); audio = null; }
    audioCtx = null;
    panel.setAudioMeter("off");
    return;
  }
  if (audio || audioStarting) return;
  const Ctx = window.AudioContext || window.webkitAudioContext;
  if (!Ctx || !window.AudioWorkletNode) { soundWanted = false; setSoundBtn(false); panel.setAudioMeter("no Web Audio here"); return; }
  // Made and resumed here, inside the tap: iOS will not start a context
  // created after an await.
  const ctx = audioCtx = new Ctx({ latencyHint: "interactive" });
  ctx.resume();
  audioStarting = true;
  panel.setAudioMeter("starting…");
  import("./audio.js")
    .then(m => m.startAudio(ctx, { ...panel.audio }, sim, (mm) => {
      if (!audio) return;
      if (mm.type === "error") { console.warn("audio:", mm.message); return; }   // the worklet recovered; say so in the console
      panel.setAudioMeter(meterText(mm));
    }))
    .then(a => {
      audioStarting = false;
      if (!soundWanted) { a.close(); return; }   // switched off while starting
      audio = a;
      a.rec.setSelection(soundSel?.cells ?? null);
      redraw();
    })
    .catch(e => {
      audioStarting = false;
      ctx.close().catch(() => {});
      soundWanted = false;
      setSoundBtn(false);
      panel.setAudioMeter(`failed: ${e.message}`);
    });
}
function meterText(m) {
  const db = m.peak > 0 ? (20 * Math.log10(m.peak)).toFixed(1) : "-inf";
  return `voices ${m.voices} / ${m.limit}` + (m.refused ? `  (${m.refused} refused)` : "") + "\n" +
    `runs active ${m.active} of ${m.candidates}\n` +
    `peak ${m.peak.toFixed(2)} (${db} dB)` + (m.clipped ? `  clipped ${m.clipped}` : "") + "\n" +
    `lag ${Math.round(m.lagMs)} ms · speed ${m.speed.toFixed(2)}` +
    (bakedPlay ? "  (baked)" : "") + "\n" + bakeStatus() +
    (audioNote && performance.now() < audioNote.until ? `\n${audioNote.text}` : "") +
    (audio?.rec.selInfo ? `\nsound selection: ${audio.rec.selInfo.runs} runs, ${audio.rec.selInfo.gates} gates` : "");
}

// --- audio settings in the save ------------------------------------------------
//
// Every save carries the dials and the bake dialog's seconds and rate
// (board.audio; audio-params.js has the format). Loading a save that has
// them applies them; one without leaves the dials as they are. Either way
// board.audio is then the dials in front of the player, so the next save
// writes them. A dial change is a change for the autosaves, like a label
// edit, but it does not restart the simulation and is not an undo step.
let bakeOpts = null;        // { seconds, rate } last used in the Bake dialog
let audioNote = null;       // { text, until }: a line for the meter
function loadAudioSettings() {
  const saved = board.audio;
  if (saved) {
    panel.applyAudio(saved.dials);
    if (saved.bake) bakeOpts = saved.bake;
    if (audio) audio.setParams({ ...panel.audio });
    audioNote = { text: "audio settings loaded from this save", until: performance.now() + 6000 };
    if (!audio) panel.setAudioMeter(`off\n${audioNote.text}`);
  } else if (!audio) panel.setAudioMeter("off");
  board.audio = audioRecord(panel.audio, bakeOpts);
}
// The dials (or the bake options) moved: the board's record follows, and
// the autosaves will write it.
function touchAudio() {
  if (!board) return;
  board.audio = audioRecord(panel.audio, bakeOpts);
  board.audioRev++;
  edited = true;
  prepareSave();
}

// --- video export ---------------------------------------------------------------
//
// Video (panel) renders a stretch of the simulation offline into an MP4
// (video.js, after Fluoddity's recorder). It starts from the current tick,
// on a copy of the sim, so the live board is not touched -- it is paused
// while the render runs, and any edit stops it, keeping what was encoded.
// With sound on, the same ticks are baked into its audio track.
//
// The camera moves from where it is when Record is pressed to the end state
// captured by "Select end camera state" (if any), over the lerp window.
let endCam = null;          // { cx, cy, zoom, w, h }, or null: a still camera
let videoOpen = false;      // the Video folder is open: show the frame on the board
let recording = null;       // { cancel } while a video renders
const camState = () => ({ cx: cam.cx, cy: cam.cy, zoom: cam.zoom, w: cam.w, h: cam.h });

// What the video frame covers, for the preview: the crop box now, and the
// end camera's (dashed), in the video's aspect.
function videoBoxes() {
  if (!videoOpen) return null;
  const v = panel.video, aspect = v.width / v.height;
  const boxes = [{ ...cropBox(camState(), aspect), dashed: false }];
  if (endCam) boxes.push({ ...cropBox(endCam, aspect), dashed: true });
  return boxes;
}

// One video frame: the board as View draws it, never the editing marks.
// Layers too, as the board shows them: the view taken when Record was
// pressed (`recLayers`), since the copy shares the live board's grid, table
// and lists and an edit stops the recording anyway.
let recLayers = null;
function drawVideoFrame(ctx, s, vcam, alpha) {
  render(ctx, s.board, vcam, 1, {
    ...view, sim: s, alpha, ghosts: [],
    ...layerOpts(recLayers, view.gateStatus ? gateStatus(s) : null, view.beltStatus ? beltStatus(s) : null, true),
    pixels: s.screen ? { screen: s.screen, alpha: view.pixelAlpha, max: view.pixelMax } : null,
  });
}

// Fluoddity's saveFile.ts: the picker must be raised before any other await
// in the click, or it loses the gesture. Three outcomes: a file to stream
// into, no picker here (buffer and download), or the player said no.
async function chooseVideoFile(name) {
  if (typeof window.showSaveFilePicker !== "function") return { kind: "unavailable" };
  try {
    const handle = await window.showSaveFilePicker({ suggestedName: name, types: [{ description: "MP4 video", accept: { "video/mp4": [".mp4"] } }] });
    return { kind: "file", writable: await handle.createWritable() };
  } catch (e) {
    return e?.name === "AbortError" ? { kind: "cancelled" } : { kind: "unavailable" };
  }
}

async function recordVideoNow() {
  if (recording) { recording.cancel = true; return; }
  if (!sim || !board) return;
  const settings = { ...panel.video };
  // A short timestamp in the name, so takes of one scene do not collide:
  // "CPU experiments 11-20 12_07_33.mp4" (underscores: no dots in a name).
  const base = (panel.local.name || "gates").replace(/[^\w .-]+/g, "_").trim() || "gates";
  const d = new Date(), two = (n) => String(n).padStart(2, "0");
  const fileName = `${base} ${two(d.getMonth() + 1)}-${two(d.getDate())} ${two(d.getHours())}_${two(d.getMinutes())}_${two(d.getSeconds())}.mp4`;
  endDryRun();       // a recording starts from the view play took over from
  const choice = await chooseVideoFile(fileName);       // first await: keeps the gesture
  if (choice.kind === "cancelled") return;
  const own = { cancel: false };
  recording = own;
  panel.setVideoRecording(true);
  shell.setPaused(true);
  clock.paused = true;
  const copy = sim.clone();
  if (view.pixelAlpha > 0) copy.screen = new Screen();
  const startCam = camState(), rev = board.revision, live = sim;
  recLayers = layerView();
  const withSound = !!audio;
  panel.setVideoProgress("starting…");
  try {
    const [{ recordVideo }, bakeMod] = await Promise.all([import("./video.js"), withSound ? import("./audio-bake.js") : null]);
    const res = await recordVideo({
      sim: copy, settings, startCam, endCam, draw: drawVideoFrame,
      audio: withSound ? { params: { ...panel.audio }, bake: bakeMod.bakeAudio, selection: soundSel?.cells ?? null } : null,
      writable: choice.kind === "file" ? choice.writable : null,
      onProgress: (p) => panel.setVideoProgress(p.phase === "audio"
        ? `baking audio ${Math.round(p.fraction * 100)}%`
        : `frame ${p.frame} of ${p.frames} (${Math.round(p.fraction * 100)}%)`),
      shouldStop: () => own.cancel || sim !== live || board.revision !== rev,
    });
    if (res.kind === "buffered") {
      const a = document.createElement("a");
      a.href = URL.createObjectURL(res.blob);
      a.download = fileName;
      a.click();
      setTimeout(() => URL.revokeObjectURL(a.href), 10000);
    }
    panel.setVideoProgress(res.kind === "empty" ? "nothing recorded"
      : `saved ${res.frames} frames${res.frames < frameCount(settings) ? " (stopped early)" : ""}`);
  } catch (e) {
    console.error(e);
    panel.setVideoProgress(`failed: ${e.message}`);
  } finally {
    recording = null;
    recLayers = null;
    panel.setVideoRecording(false);
  }
}

// --- the sound selection ------------------------------------------------------------
//
// Audio > Set audio selection: with entities selected, only they sound (a
// run if any of its cells is selected, a gate if any of its cells is); with
// nothing selected, everything does again. The cells are taken where the
// selection sits at the press, and kept after deselecting.
//
// It is reset by any change to the board -- and selecting is itself an edit
// here (the selection is lifted off the board), so "a change" is a change of
// CONTENT: Board.contentHash(), which ignores row ids and per-tick channel
// values. The reference is the first stable board after the press (nothing
// lifted), so deselecting in place keeps it and moving the selection, any
// other edit, an undo or a load resets it. Normalisation then counts only
// what is selected (audio.js), so an isolated part fills the mix.
let soundSel = null;      // { cells: Set<y*W+x>, ref: hash | null, rev, id }
let soundSelN = 0;
const soundSelId = () => soundSel?.id ?? 0;
function setSoundSelection() {
  if (!sel || !sel.stamp.entries.length) { clearSoundSel(); return; }
  const [ox, oy] = selOrigin(), W = board.width;
  const cells = new Set(stampCells(T, sel.stamp, ox, oy).map(([x, y]) => y * W + x));
  soundSel = { cells, ref: null, rev: -1, id: ++soundSelN };
  audio?.rec.setSelection(cells);
  panel.setAudioSelection(true);
}
function clearSoundSel() {
  if (!soundSel) return;
  soundSel = null;
  audio?.rec.setSelection(null);
  panel.setAudioSelection(false);
}
// Each frame: once nothing is lifted, any change of content since the
// reference resets it.
function checkSoundSel() {
  if (!soundSel || sel || !board || board.revision === soundSel.rev) return;
  soundSel.rev = board.revision;
  const h = board.contentHash();
  if (soundSel.ref === null) soundSel.ref = h;
  else if (h !== soundSel.ref) clearSoundSel();
}

// --- the camera's dry run -----------------------------------------------------------
//
// With an end camera set, pressing play flies the view along the path a
// recording started now would take: the view at the press is the start, and
// the tick on screen decides the frame (frame f is tick t0 + f * tps / 60,
// as in video.js), so the camera always matches the content, at any play
// rate. The view is set so the crop box IS that frame. It holds the end
// view once the path is done. The video and camera dials apply live, so the
// fades can be tuned while watching.
//
// Any disturbance -- pause, a pan or zoom of any kind, an edit (undo
// included), Reset, a load, unsetting the end camera, starting a real
// recording -- puts the view back where it was when play was pressed. A
// manual camera move is detected by the camera no longer being where the
// dry run last put it, which catches drag, pinch, wheel, the pan glide and
// Fit board alike without touching camera.js.
let dry = null;   // { start, t0, sim, rev, set }: set = the view last applied
function startDryRun() {
  if (!endCam || !sim || recording) { dry = null; return; }
  dry = { start: camState(), t0: sim.tick, sim, rev: board.revision, set: null };
}
function endDryRun() {
  if (!dry) return;
  const st = dry.start;
  dry = null;
  cam.cx = st.cx; cam.cy = st.cy; cam.zoom = st.zoom;
  redraw();
}
function driveDryRun() {
  if (!dry) return;
  const moved = dry.set && (cam.cx !== dry.set.cx || cam.cy !== dry.set.cy || cam.zoom !== dry.set.zoom);
  if (moved || !endCam || clock.paused || sim !== dry.sim || board.revision !== dry.rev) { endDryRun(); return; }
  const v = panel.video, frames = frameCount(v);
  const f = Math.min(frames - 1, Math.max(0, (sim.tick + clock.alpha - dry.t0) * 60 / v.tps));
  const box = frameBox(dry.start, endCam, v, f, frames);
  // The view whose crop box is `box`: the crop is the largest box of the
  // video's aspect in the view, so its half-width is min(w/2, h/2 * aspect) / zoom.
  const aspect = v.width / v.height;
  cam.cx = box.cx; cam.cy = box.cy;
  cam.zoom = Math.min(cam.w / 2, (cam.h / 2) * aspect) / box.hw;
  dry.set = { cx: cam.cx, cy: cam.cy, zoom: cam.zoom };
}

// --- baked audio ------------------------------------------------------------------
//
// A stretch of ticks rendered ahead of time (audio-bake.js), played back
// from a buffer while the live sim is on exactly that stretch. It starts
// with a Reset and keeps that tick-0 state (`snap`): while the bake is
// valid, Reset restores it rather than rebuilding, since a rebuild renumbers
// the runs and the numbering decides the belt phase's order -- only the
// same state is sure to replay the same ticks.
//
// While it plays, the audio is the clock: each frame steps the sim to the
// tick being heard, with a bigger step budget, so frames drop rather than
// the board drifting from the sound.
let bake = null;           // { snap, sim, revision, rebuilds, rate, key, t0, end, samples, sampleRate, buf }
let bakedPlay = null;      // { t0Time }: context time at which tick bake.t0 is heard
let baking = null;         // { cancel } while a bake runs
const BAKED_BUDGET_MS = 100;

// Same board, same run numbering, same sim: Reset can return to the bake's start.
const bakeLive = () => !!(bake && sim === bake.sim && board.revision === bake.revision &&
                          sim.stats.rebuilds === bake.rebuilds);
function bakeStale() {
  if (!bake) return null;
  if (!bakeLive()) return "the board changed";
  if (bake.key !== bakeKey(panel.audio)) return "the dials changed";
  if (bake.sel !== soundSelId()) return "the sound selection changed";
  if (clock.rate !== bake.rate) return `the rate is not ${bake.rate}`;
  return null;
}
// The live sim is inside the baked stretch and the bake still says what it would play.
const bakePlayable = () => !!(audio && bake && !bakeStale() && sim.tick < bake.end);
function bakeStatus() {
  if (baking) return "bake: baking…";
  if (!bake) return "bake: none";
  const why = bakeStale(), secs = ((bake.end - bake.t0) / bake.rate).toFixed(1);
  return `bake: ${secs} s @ ${bake.rate} tps, ticks ${bake.t0}–${bake.end}` + (why ? ` -- stale: ${why}` : "");
}

function startBakedPlay() {
  audio.rec.stop();
  audio.rec.detach();                // nothing to record: the buffer is the sound
  if (!bake.buf) bake.buf = audio.buffer(bake.samples, bake.sampleRate);
  const ac = audio.ctx, lead = 0.03;
  const at = sim.tick + clock.alpha;
  // Tick `at` starts sounding `lead` from now (out of the speakers a
  // further outputLatency on, which the clock below takes off again).
  const t0Time = ac.currentTime + lead - (at - bake.t0) / bake.rate;
  audio.playBaked(bake.buf, Math.max(ac.currentTime + lead, t0Time), Math.max(0, (at - bake.t0) / bake.rate));
  bakedPlay = { t0Time };
}
function stopBakedPlay() {
  if (!bakedPlay) return;
  bakedPlay = null;
  if (audio) { audio.stopBaked(); audio.rec.attach(sim); }
}
// The fractional tick being heard now.
function bakedTick() {
  const ac = audio.ctx, out = ac.outputLatency || ac.baseLatency || 0;
  return bake.t0 + (ac.currentTime - out - bakedPlay.t0Time) * bake.rate;
}

// The Bake dialog's Play / Pause: the baked buffer on its own, from where it
// was paused (from the start once it has played through), with the
// simulation left alone. On the sound's context if sound is on, else on one
// made in this tap. Stopped by closing the dialog, a new bake, or Play on
// the board.
let preview = null;       // { src, ctx, t0, from } while playing
let previewAt = 0;        // seconds into the bake to resume from
let previewCtx = null;    // our own context, when sound is off
function stopPreview(keepPlace = true) {
  if (!preview) { if (!keepPlace) previewAt = 0; return; }
  const p = preview;
  preview = null;
  previewAt = keepPlace ? p.from + (p.ctx.currentTime - p.t0) : 0;
  p.src.onended = null;
  try { p.src.stop(); } catch { /* ended */ }
  p.src.disconnect();
  syncPreviewBtn();
}
function syncPreviewBtn() {
  const b = document.getElementById("bake-play");
  if (b) { b.textContent = preview ? "Pause" : "Play"; b.disabled = !bake || !!baking; }
}
function togglePreview() {
  if (preview) { stopPreview(); return; }
  if (!bake) return;
  let ctx = audio?.ctx;
  if (!ctx) {
    const Ctx = window.AudioContext || window.webkitAudioContext;
    if (!Ctx) return;
    ctx = previewCtx ||= new Ctx();
  }
  ctx.resume();
  const buf = ctx.createBuffer(1, bake.samples.length, bake.sampleRate);
  buf.copyToChannel(bake.samples, 0);
  if (previewAt >= buf.duration - 0.01) previewAt = 0;
  const src = ctx.createBufferSource();
  src.buffer = buf;
  src.connect(ctx.destination);
  src.start(0, previewAt);
  src.onended = () => { if (preview?.src === src) { preview = null; previewAt = 0; syncPreviewBtn(); } };
  preview = { src, ctx, t0: ctx.currentTime, from: previewAt };
  syncPreviewBtn();
}

function openBakeDialog() {
  const m = document.getElementById("bakemenu");
  const secs = m.querySelector("#bake-seconds"), rate = m.querySelector("#bake-rate");
  const status = m.querySelector("#bake-status"), go = m.querySelector("#bake-go");
  const cancel = m.querySelector("#bake-cancel"), wav = m.querySelector("#bake-wav");
  secs.value = String(bakeOpts?.seconds ?? (Number(secs.value) || BAKE_DEFAULT_SECONDS));
  rate.value = String(bakeOpts?.rate ?? clock.rate);
  const show = (text) => {
    status.textContent = text ?? (`Starts with a Reset. Audio from tick ${panel.audio.startTick}` +
      (panel.audio.fadeMs ? `, fading in over ${panel.audio.fadeMs} ms` : "") + ".\n" + bakeStatus());
    go.disabled = !!baking; cancel.disabled = !baking; wav.disabled = !bake || !!baking;
    syncPreviewBtn();
  };
  const close = () => { stopPreview(); m.dataset.open = "false"; };
  m.querySelector("#bake-play").onclick = () => togglePreview();
  m.querySelector("#bake-close").onclick = close;
  let down = false;
  m.onpointerdown = (e) => { down = e.target === m; };
  m.onclick = (e) => { if (e.target === m && down) close(); down = false; };
  cancel.onclick = () => { if (baking) baking.cancel = true; };
  wav.onclick = () => bake && downloadWav();
  go.onclick = () => {
    const sv = Number(secs.value), rv = Number(rate.value);
    if (!(sv > 0 && sv <= BAKE_MAX_SECONDS)) { show(`Duration must be between 0 and ${BAKE_MAX_SECONDS} s.`); return; }
    if (!(rv >= RATE_MIN && rv <= RATE_MAX)) { show(`Tick rate must be ${RATE_MIN} to ${RATE_MAX}.`); return; }
    if (bakeOpts?.seconds !== sv || bakeOpts?.rate !== rv) { bakeOpts = { seconds: sv, rate: rv }; touchAudio(); }
    // Playing it back needs sound on; this tap is the gesture that may start it.
    if (!audio && !audioStarting) { setAudio(true); storeSound(true); }
    runBake(sv, rv, show);
  };
  show();
  m.dataset.open = "true";
}

async function runBake(seconds, rate, show) {
  if (!sim || baking) return;
  shell.setPaused(true);
  clock.paused = true;
  stopBakedPlay();
  stopPreview(false);
  bake = null;
  sim.reset();
  clock.reset();
  redraw();
  const snap = sim.clone();
  const own = { cancel: false };
  baking = own;
  const startSim = sim, revision = board.revision, rebuilds = sim.stats.rebuilds;
  const params = { ...panel.audio }, key = bakeKey(params), sel = soundSelId();
  const t = performance.now();
  const { bakeAudio } = await import("./audio-bake.js");
  let res = null;
  try {
    res = await bakeAudio({
      start: snap, params, rate, seconds, sampleRate: audioCtx?.sampleRate || 48000, selection: soundSel?.cells ?? null,
      onProgress: (f, tick, end) => show(`Baking… ${Math.round(f * 100)}%  (tick ${tick} of ${end})`),
      shouldStop: () => own.cancel || sim !== startSim || board.revision !== revision,
    });
  } catch (e) {
    show(`Bake failed: ${e.message}`);
  }
  baking = null;
  if (!res) { show(res === null ? "Bake cancelled (or the board changed)." : undefined); return; }
  bake = { snap, sim: startSim, revision, rebuilds, rate, key, sel, ...res, buf: null };
  shell.setRate(rate);     // play it at the rate it was baked at
  show(`Baked ${seconds} s at ${rate} tps in ${((performance.now() - t) / 1000).toFixed(1)} s. Press play.\n` + bakeStatus());
  redraw();
}

function downloadWav() {
  import("./audio-bake.js").then(({ wavBytes }) => {
    const blob = new Blob([wavBytes(bake.samples, bake.sampleRate)], { type: "audio/wav" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `${(panel.local.name || "gates").replace(/[^\w.-]+/g, "_")}-${bake.rate}tps-t${bake.t0}.wav`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 10000);
  });
}

let queued = false;
let lastNow = null;    // the previous playing frame's time, or null
// Stepping stops after this many ms of a frame and drops the rest of the
// ticks due, so a rate the board cannot sustain slows the sim instead of
// starving the frame (and every later frame, as the backlog grows).
const STEP_BUDGET_MS = 12;

// View > fps counter: frames and ticks counted over a half-second window.
const fpsEl = document.getElementById("fps");
const fpsWin = { t0: null, frames: 0, ticks: 0 };
function countFrame(now, ticks) {
  if (!view.fps) { fpsEl.hidden = true; fpsWin.t0 = null; return; }
  fpsEl.hidden = false;
  // Paused, frames are drawn only on demand, so a rate would mean nothing.
  if (!sim || clock.paused) { fpsEl.textContent = "paused"; fpsWin.t0 = null; return; }
  if (fpsWin.t0 === null) { Object.assign(fpsWin, { t0: now, frames: 0, ticks: 0 }); return; }
  fpsWin.frames++; fpsWin.ticks += ticks;
  const dt = now - fpsWin.t0;
  if (dt >= 500) {
    fpsEl.textContent = `${Math.round(fpsWin.frames * 1000 / dt)} fps · ${Math.round(fpsWin.ticks * 1000 / dt)} tps`;
    Object.assign(fpsWin, { t0: now, frames: 0, ticks: 0 });
  }
}
function redraw() {
  if (queued) return;
  queued = true;
  requestAnimationFrame(paint);
}

function paint(now) {
  queued = false;
  let ran = 0;
  if (sim) {
    // An edit since the last frame re-derives the runs and starts fresh --
    // here rather than at each edit site, so no edit path can forget it.
    sim.sync();
    if (!clock.paused && bakePlayable()) {
      // Baked: step to the tick being heard. The buffer runs on its own
      // clock, so falling behind costs frames, never sync.
      if (!bakedPlay) startBakedPlay();
      const target = Math.min(bakedTick(), bake.end);
      const t0 = performance.now();
      while (sim.tick < Math.floor(target) && performance.now() - t0 < BAKED_BUDGET_MS) { sim.step(); ran++; }
      clock.frac = Math.min(1, Math.max(0, target - sim.tick));
    } else if (!clock.paused) {
      if (bakedPlay) stopBakedPlay();   // out of the baked stretch: realtime from here
      // A stalled frame (a backgrounded tab) is clamped, and the clock caps
      // the backlog too, so coming back never freezes to catch up.
      const dt = lastNow === null ? 0 : Math.min(0.25, (now - lastNow) / 1000);
      const n = clock.advance(dt);
      const t0 = performance.now();
      for (; ran < n; ran++) {
        if (ran > 0 && performance.now() - t0 > STEP_BUDGET_MS) { clock.dropped += n - ran; break; }
        sim.step();
      }
    }
    shell.setTick(sim.tick);
    if (inspectRow >= 0) panel.setLive(liveText(inspectRow));
  }
  driveDryRun();
  checkSoundSel();
  // Ship this frame's ticks to the audio thread; paused, let it fall silent.
  if (audio) {
    if (clock.paused) stopBakedPlay();
    if (bakedPlay) { /* the buffer is playing */ }
    else if (sim && !clock.paused) audio.rec.flush(clock.rate);
    else audio.rec.stop();
  }
  lastNow = sim && !clock.paused ? now : null;
  countFrame(now, ran);
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
  const lv = layerView();
  const gTint = view.gateStatus ? gateStatus() : null;
  const bTint = view.beltStatus ? beltStatus() : null;
  render(ctx, board, cam, dpr, { ...view, selected: info, ghosts, sim, alpha: clock.alpha, hide, vector: vectorMarks(),
                                indexLabels: indexLabels(), noteSel: noteEdit, noteGrips: labelHeld(),
                                ghostNotes: ghostNotes(),
                                pixels: sim?.screen ? { screen, alpha: view.pixelAlpha, max: view.pixelMax } : null,
                                frameBoxes: videoBoxes(),
                                ...layerOpts(lv, gTint, bTint) });
  syncLayerPanel(lv);
  debug.renderMs = performance.now() - t0;
  if (sim && !clock.paused) redraw();
}

// --- undo ------------------------------------------------------------------

// Commit the board as a new step if it changed since the last one and is
// stable. Called every frame, so all the edits one gesture makes in one go
// (clearing the way, then building) are one step.
// The board's edit key: entities and labels both. A label edit is an undo
// step but not a board edit -- it must not restart the simulation.
const editKey = () => `${board.revision}/${board.notesRev}/${board.layersRev}`;

function recordStep() {
  if (!board || sel || editKey() === committed) return;
  // A label being dragged or typed into is a change in progress too: one
  // step when the drag ends or the editor closes, not one per frame.
  if (grab?.note || noteEdit >= 0) return;
  history.commit(board.snapshot());
  committed = editKey();
  edited = true;   // the autosaves may write from now on
  syncHistory();
  prepareSave();
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

// --- layers -------------------------------------------------------------------
//
// layers.js is the model; the table lives on the board (board.layers) so it
// rides in snapshots and saves, and each entity's tag in its row. While
// View > layers is off, layers change nothing: everything is drawn and
// everything can be touched (new entities still go on the current layer).
//
// Every interaction that finds an entity under a cell goes through
// pickAt() -- an inactive layer's entities are not there to select, run
// over, force-build through or match -- and the Hand's inspect through
// seeAt(), which also skips hidden layers but lets an inactive entity be
// inspected, read only, so its layer can be made current.

const layersOn = () => !!board && view.layers === true;

function pickable(row) {
  return row >= 0 && (!layersOn() || isActive(board.layers, board.layerOf(row)));
}
function pickAt(x, y) { const r = board.entityAt(x, y); return pickable(r) ? r : -1; }
function seeAt(x, y) {
  const r = board.entityAt(x, y);
  return r >= 0 && (!layersOn() || isVisible(board.layers, board.layerOf(r))) ? r : -1;
}

// What the layers mean for drawing, rebuilt only when the board, the table
// or its view changed -- not per frame: the visible lists (only while
// something is hidden), the hidden-cell mask for items, each layer's label
// at its centre of mass, and the tints. A tint blends the layer's colour
// over whatever the entity would otherwise wear (its own colour, or a
// status colour), by that layer's own amount.
let lvCache = null;
function layerView() {
  if (!layersOn()) return null;
  const t = board.layers;
  const key = `${board.revision}/${board.layersRev}/${JSON.stringify(t)}`;
  if (lvCache?.board === board && lvCache.key === key) return lvCache.lv;
  const stats = layerStats(board, t);
  const vis = (row) => isVisible(t, board.layerOf(row));
  const lists = stats.hidden ? {
    belts: board.belts.filter(b => vis(b.row)),
    unders: board.unders.filter(u => vis(u.row)),
    structs: board.structs.filter(st => vis(st.row)),
  } : null;
  const tints = new Map();
  for (const l of t.list) if (l.colour && l.tint > 0 && isVisible(t, l.id)) tints.set(l.id, { rgb: hexRgb(l.colour), k: l.tint });
  const labels = [];
  for (const l of t.list) {
    if (l.id === BACKGROUND || !(l.label > 0) || !isVisible(t, l.id)) continue;
    const c = stats.centres.get(l.id);
    if (c) labels.push({ x: c[0], y: c[1], text: l.name, colour: l.colour, size: l.label });
  }
  const strs = new Map();   // "layer|base css" -> blended css, so a belt costs a lookup
  const blend = (id, base) => {
    const k = `${id}|${base}`;
    let c = strs.get(k);
    if (!c) {
      const tt = tints.get(id);
      c = rgbCss(mix(parseCss(base) || [0.46, 0.47, 0.52], tt.rgb, tt.k));
      strs.set(k, c);
    }
    return c;
  };
  // Visible but inactive: what cannot be touched, washed out so it reads as
  // such. Cell rects for the one batched pass, plus the rows and cells the
  // renderer's screen-space text (Source values, item numbers) fades by.
  let dim = null;
  if (t.list.some(l => isVisible(t, l.id) && !isActive(t, l.id))) {
    const W = board.width, cells = new Uint8Array(W * board.height), rows = new Set(), rects = [];
    const dimmed = (row) => { const id = board.layerOf(row); return isVisible(t, id) && !isActive(t, id); };
    const add = (row, x, y, w, h) => {
      if (!dimmed(row)) return;
      rows.add(row);
      rects.push([x, y, w, h]);
      for (let dy = 0; dy < h; dy++) cells.fill(1, (y + dy) * W + x, (y + dy) * W + x + w);
    };
    for (const b of board.belts) add(b.row, b.x, b.y, 1, 1);
    for (const u of board.unders) add(u.row, u.x, u.y, 1, 1);
    for (const st of board.structs) add(st.row, st.x, st.y, st.w, st.h);
    dim = { rects, rows, cells };
  }
  let plainGate = null;     // the machines' map with no status colours, built once
  const lv = {
    lists, labels, stats, dim, hidden: stats.hidden,
    // `status`: View > gate status colours' row -> rgb map, or null.
    gateTint(status) {
      if (!tints.size) return status;
      if (!status && plainGate) return plainGate;
      const out = new Map(status || []);
      for (const st of board.structs) {
        const tt = tints.get(board.layerOf(st.row));
        if (!tt) continue;
        const base = status?.get(st.row) || T.types[st.type].colour || [0.5, 0.5, 0.5];
        out.set(st.row, mix(base, tt.rgb, tt.k));
      }
      if (!status) plainGate = out;
      return out;
    },
    // `status`: View > belt status colours' (x, y) -> css, or null.
    // `base`: the belt's (or underground end's) own colour.
    beltTint(status, base) {
      if (!tints.size) return status;
      const W = board.width;
      return (x, y) => {
        const s0 = status?.(x, y) || null;
        const id = board.layerOf(board.cellId(board.grid[(y * W + x) * 4]));
        return tints.has(id) ? blend(id, s0 || base) : s0;
      };
    },
  };
  lvCache = { board, key, lv };
  return lv;
}

// What a layer view adds to a render: tints over the status colours (or in
// their place), the visible lists, the hidden-cell mask and the labels.
// One place, so the screen and a video frame cannot draw layers differently.
// `forVideo`: the inactive washout is left out -- what can be touched is an
// editing matter, like the selection and the ghosts.
function layerOpts(lv, gTint, bTint, forVideo = false) {
  return {
    dim: !forVideo && lv?.dim || null,
    gateTint: lv ? lv.gateTint(gTint) : gTint,
    beltTint: lv ? lv.beltTint(bTint, BASE_COLOURS.belt) : bTint,
    underTint: lv ? lv.beltTint(bTint, BASE_COLOURS.under) : null,
    lists: lv?.lists || null, hiddenCells: lv?.hidden || null,
    layerLabels: lv?.labels || null,
  };
}

// The panel's model, pushed every frame (the panel redraws only on change).
// Counts include what is lifted in the selection, so Adopt shows at once.
function syncLayerPanel(lv = layerView()) {
  if (!layerPanel) return;
  layerPanel.show(layersOn());
  shell.setInspectLayer(inspectLayerOf(inspectRow));
  if (!lv) return;
  const t = board.layers;
  const counts = new Map(lv.stats.counts);
  if (sel) {
    for (const e of sel.stamp.entries) {
      const id = byId(t, e.layer) ? e.layer : BACKGROUND;
      counts.set(id, (counts.get(id) || 0) + 1);
    }
  }
  // The sliders show the current layer's values -- or, with Background
  // current, the first active layer's, since that is what they would move.
  const cur = byId(t, t.current);
  const ref = cur && cur.id !== BACKGROUND ? cur : t.list.find(l => l.id !== BACKGROUND && isActive(t, l.id));
  layerPanel.update({
    rows: t.list.map(l => ({ id: l.id, name: l.name, colour: l.colour, visible: isVisible(t, l.id),
                             active: isActive(t, l.id), current: l.id === t.current, count: counts.get(l.id) || 0 })),
    tint: ref?.tint ?? 0, label: ref?.label ?? 0,
    selCount: sel ? sel.stamp.entries.length : 0,
    canEdit: t.current !== BACKGROUND,
  });
}

// The Hand bar's layer chip for a row, or null.
function inspectLayerOf(row) {
  if (!layersOn() || !board.describe(row)) return null;
  const t = board.layers;
  const l = byId(t, board.layerOf(row)) || t.list[0];
  return { name: l.name, colour: l.colour, current: l.id === t.current };
}

// After any layer change. `edit`: an undo step (membership, names,
// colours) rather than view (visibility, current, sliders).
function layersChanged(edit) {
  if (!board) return;
  if (edit) board.layersRev++;
  if (inspectRow >= 0 && seeAtRow(inspectRow) < 0) inspect(-1);
  shell.setInspectValue(handValueOf(inspectRow));
  syncHistory();
  redraw();
}
const seeAtRow = (row) => (!layersOn() || isVisible(board.layers, board.layerOf(row)) ? row : -1);

const layerActions = {
  onSelect: (id) => { setCurrent(board.layers, id); layersChanged(false); },
  onIsolate: (id) => { if (isolate(board.layers, id)) layersChanged(false); },
  onAdd: () => { addLayer(board.layers); layersChanged(true); },
  onRename: () => {
    const t = board.layers, l = byId(t, t.current);
    if (!l || l.id === BACKGROUND) return;
    const name = prompt("Rename layer", l.name);
    if (name !== null && renameLayer(t, l.id, name)) layersChanged(true);
  },
  // Deleting a layer moves its entities -- on the board and lifted -- to
  // Background, after asking.
  onDelete: () => {
    const t = board.layers, l = byId(t, t.current);
    if (!l || l.id === BACKGROUND) return;
    const n = (layerView()?.stats.counts.get(l.id) || 0) + (sel ? sel.stamp.entries.filter(e => e.layer === l.id).length : 0);
    const what = n ? ` Its ${n} ${n === 1 ? "entity moves" : "entities move"} to Background.` : "";
    if (!confirm(`Delete layer "${l.name}"?${what}`)) return;
    board.retag(l.id, BACKGROUND);
    if (sel) for (const e of sel.stamp.entries) if (e.layer === l.id) e.layer = BACKGROUND;
    removeLayer(t, l.id);
    layersChanged(true);
  },
  // The selection's entities join the current layer. They are lifted, so
  // the change lands -- one undo step -- when the selection goes down.
  onAdopt: () => {
    if (!sel) return;
    for (const e of sel.stamp.entries) e.layer = board.layers.current;
    layersChanged(false);
  },
  onVisible: (id, v) => { const l = byId(board.layers, id); if (l) l.visible = v; layersChanged(false); },
  onActive: (id, v) => { const l = byId(board.layers, id); if (l) l.active = v; layersChanged(false); },
  onColour: (id, hex) => { const l = byId(board.layers, id); if (l && l.colour !== hex) { l.colour = hex; layersChanged(true); } },
  onTint: (v) => { setForActive(board.layers, "tint", v); redraw(); },
  onLabel: (v) => { setForActive(board.layers, "label", v); redraw(); },
};

// View > gate status colours: each machine's body coloured by what it is
// doing in the state on screen. BLOCKED (yellow) when an output with
// somewhere to go is holding a result it could not hand on -- that wins,
// since the gate made something that cannot leave; STARVED (red) when it
// does not have what its own firing rule needs (a Merge needs its control
// and the lane that selects, a Sink any one lane; a Source never starves);
// RUNNING (green) otherwise. Dangling outputs discard and never block.
// The colours are the player's (View > status colours, panel.js).
const hexRgb = (h) => [1, 3, 5].map(i => parseInt(h.slice(i, i + 2), 16) / 255);
// `s` is the sim to read (the live one, or a video's copy).
function gateStatus(s = sim) {
  const out = new Map();
  if (!s) return out;
  s.sync();
  const STATUS_COL = { blocked: hexRgb(statusColours.gateBlocked), starved: hexRgb(statusColours.gateStarved), running: hexRgb(statusColours.gateRunning) };
  for (const g of s.gates) {
    const blocked = g.drains.some(c => s.outHeld(g.row, c));
    const starved = g.sim.n_in > 0 && !s.inputsReady(g);
    out.set(g.row, blocked ? STATUS_COL.blocked : starved ? STATUS_COL.starved : STATUS_COL.running);
  }
  return out;
}

// View > belt status colours, per run (segment), in the state on screen:
// RED empty; YELLOW stalled -- its head item is at the end and will not
// leave on the coming tick (the forecast has no pop), so items are stuck on
// it (and, by default, nothing on it is moving either -- see below); GREEN
// otherwise (carrying, and moving). Returns (x, y) -> colour.
function beltStatus(s = sim) {
  if (!s) return null;
  const BELT_STATUS_COL = { empty: statusColours.beltEmpty, stalled: statusColours.beltStalled, moving: statusColours.beltMoving };
  s.sync();
  const F = s.forecast();
  const bySid = new Map();
  for (const seg of s.segments.values()) {
    let st = "moving";
    if (seg.head === seg.tail) st = "empty";
    else if (s.positionOf(seg, seg.head, s.tick) >= seg.length * s.UNIT && !F.pops.has(seg)) {
      st = "stalled";
      // View > "stalled but moving: green": a stalled run whose items are
      // still moving -- one is pushed on this tick, or the newest is still
      // closing up (items pack from the head, so if the newest has stopped
      // they all have) -- is not static, and reads green. O(1) per run.
      if (view.beltActive !== false) {
        const k = seg.tail - 1;
        const moving = s.positionOf(seg, k, s.tick, 1) > s.positionOf(seg, k, s.tick, 0);
        if (moving || F.pushes.has(seg)) st = "moving";
      }
    }
    bySid.set(seg.sid, BELT_STATUS_COL[st]);
  }
  const W = s.board.width;
  return (x, y) => bySid.get(s.segAt[y * W + x]) || null;
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
  // A first video size: the board's own size on screen, in device pixels --
  // here, once the canvas has a real size (before, it is the default 300x150).
  if (panel && (!panel.video.width || !panel.video.height) && r.width > 0 && r.height > 0) {
    panel.setVideoSize(evenDim(canvas.width), evenDim(canvas.height));
  }
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
  shell.setInspectLayer(inspectLayerOf(inspectRow));
  redraw();
}

// What the Hand's number swatches show for a row: a Source's emit value or
// a belt's seed (null when unseeded), or nothing for anything else.
function handValueOf(row) {
  const info = board?.describe(row);
  // An inactive layer's entities can be inspected but not edited.
  if (!info || !pickable(row)) return null;
  if (info.emit !== null) return { kind: "emit", values: VALUE_CHOICES, current: info.emit };
  if (info.seedOn !== null) return { kind: "seed", values: VALUE_CHOICES, current: info.seedOn ? info.seedValue : null };
  return null;
}

function setInspectValue(v) {
  const info = board?.describe(inspectRow);
  if (!info || !pickable(inspectRow)) return;
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
  // A selection of labels alone has no entity to anchor on: its box's corner.
  const first = selAbs()[0];
  vec.picking = "select";
  vec.sv = [0, 0];
  vec.anchor = first ? [first.dx, first.dy] : selOrigin();
  grab = null;
  syncVector();
}

function confirmSelectVector() {
  const v = vec.sv;
  closePicker(true);
  if (!sel || isZero(v)) return;
  const pattern = selAbs();
  if (!pattern.length) return;   // labels are not followed: no pattern to match
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
  const row = pickAt(x, y);
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
    // A tap on bare board closes the window, as on the desktop. A hidden
    // layer's entities are not there to tap; an inactive one's can be
    // inspected (read only), which is how its layer is made current.
    inspect(seeAt(x, y));
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
  if (sel) { selectTap(x, y, wx, wy); return; }
  // Idle: a tap on an entity selects it, whatever the hand holds; a tap on
  // empty board is the held tool's. An inactive layer's entity is not
  // there to select.
  const row = pickAt(x, y);
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
    const row = pickAt(start[0], start[1]);
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
  // Force build never deletes what an inactive layer holds.
  for (const c of cells) { const r = pickAt(c.x, c.y); if (r >= 0) rows.add(r); }
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
  return typeByName(T, name)?.id ?? NaN;
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
    const notes = (st.notes || []).map(n => shiftNote(n, origin[0], origin[1]));
    return { tool, kind: "stamp", origin, w: st.w, h: st.h, entries: st.entries, at, ghost, blocked: ghost.blocked, notes };
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
  // Only the cut's first placement is the move; pasting it again copies.
  if (any && place) place.keepLayers = false;
  if (any) board.derive();
  redraw();
  return any;
}

// One placement, without re-deriving. Each is checked against the board as
// the ones before it left it. Force build clears the way for the original
// machine or raw belt only -- never for a copy, and never for a stamp,
// which already places whatever fits.
function placeOne(pl, original) {
  if (pl.kind === "stamp") {
    // A cut being put down is a move: its entities keep their layers. Any
    // other paste makes copies, which go on the current layer.
    const keep = original && place?.keepLayers;
    const entries = keep ? pl.entries : pl.entries.map(e => ({ ...e, layer: undefined }));
    // Labels always fit. A label edit is not a board edit (it must not
    // restart the simulation), so only placed entities count for the derive.
    if (pl.notes.length) { board.notes.push(...pl.notes.map(n => ({ ...n, rect: n.rect.slice() }))); board.notesRev++; }
    return board.placeEntries(entries, pl.at, { derive: false }).size > 0;
  }
  const blocked = placementCells(pl).filter(([x, y]) => !board.isFree(x, y));
  if (blocked.length) {
    if (!original || !TUNE.forceBuild) return false;
    clearCells(blocked.map(([x, y]) => ({ x, y })));
    if (placementCells(pl).some(([x, y]) => !board.isFree(x, y))) return false;   // an inactive layer is in the way
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
//
// Labels join it too: a box picks up every (visible) label it touches, and
// they are lifted off board.notes into the stamp's `notes`, so they move,
// turn, delete, cut, copy and paste with the rest (stamp.js keeps them
// upright). A label always fits, so it goes back down whenever the whole
// selection does; a tap on a selected label puts that one back.

const selOrigin = () => originFor(sel.stamp.w, sel.stamp.h, sel.px, sel.py);

// The selection's entries with absolute origins in dx/dy.
function selAbs() {
  const [ox, oy] = selOrigin();
  return sel.stamp.entries.map(e => ({ ...e, dx: ox + e.dx, dy: oy + e.dy }));
}

// The selection's labels with absolute rects.
function selNotes() {
  if (!sel) return [];
  const [ox, oy] = selOrigin();
  return (sel.stamp.notes || []).map(n => shiftNote(n, ox, oy));
}

// The selected label at world point (wx, wy), or -1.
const selNoteAt = (wx, wy) => (sel && notesOn() ? noteAt(selNotes(), wx, wy) : -1);

// Replace the selection with these absolute entries and labels (by default
// the labels it already holds). The pose is re-centred on their box, so a
// turn after a membership change pivots on what is actually selected.
function setSel(abs, notes = selNotes()) {
  if (!abs.length && !notes.length) sel = null;
  else {
    const { stamp, origin } = makeStamp(T, abs, notes);
    sel = { stamp, px: origin[0] + stamp.w / 2, py: origin[1] + stamp.h / 2 };
  }
  shell.setSelection(sel ? sel.stamp.entries.length + sel.stamp.notes.length : 0);
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

// Lift entity rows, and labels (the board.notes records themselves), into
// the selection.
function lift(rows, notes = []) {
  rows = [...new Set(rows)].filter(r => r > 0);
  notes = [...new Set(notes)].filter(n => board.notes.includes(n));
  if (!rows.length && !notes.length) return;
  const abs = sel ? selAbs() : [];
  const held = selNotes();
  let caught = [];
  if (rows.length) {
    caught = board.capture(rows).map(e => ({ ...e, partner: e.partner === null ? null : e.partner + abs.length }));
    board.eraseRows(rows);
    if (rows.includes(inspectRow)) inspect(-1);
  }
  if (notes.length) {
    for (const n of notes) board.notes.splice(board.notes.indexOf(n), 1);
    noteEdit = -1;   // indices moved; no editor is open during a selection anyway
    board.notesRev++;
  }
  setSel([...abs, ...caught], [...held, ...notes]);
}

// Put the selection's labels `which` (indices into selNotes()) back on the
// board where the selection now has them. A label always fits.
function dropNotes(which) {
  if (!sel || !which.length) return;
  const all = selNotes();
  const want = new Set(which);
  for (const k of which) board.notes.push(all[k]);
  board.notesRev++;
  setSel(selAbs(), all.filter((_, k) => !want.has(k)));
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

// Everything that fits goes down, and every label.
function dropAll() {
  if (!sel) return;
  dropNotes(sel.stamp.notes.map((_, k) => k));
  if (sel) drop(sel.stamp.entries.map((_, i) => i));
}

function selectTap(x, y, wx, wy) {
  // A selected label draws over the rest, so it wins the tap: it goes back.
  const k = selNoteAt(wx, wy);
  if (k >= 0) { dropNotes([k]); return; }
  const i = selIndexAt(x, y);
  if (i >= 0) { drop([i]); return; }
  const row = pickAt(x, y);
  if (row >= 0) lift([row]);
  else dropAll();
}

// Add every board entity the box touches to the selection, and every label
// whose box it touches (if labels are showing: hidden ones take no taps,
// so they take no boxes either). It only adds: what is already selected
// stays selected.
function boxAdd(r) {
  const rows = new Set();
  for (let y = r.y; y < r.y + r.h; y++) {
    for (let x = r.x; x < r.x + r.w; x++) {
      const row = pickAt(x, y);
      if (row >= 0) rows.add(row);
    }
  }
  const notes = notesOn() ? board.notes.filter(n => isText(n) && noteTouches(n, r)) : [];
  lift([...rows], notes);
}

// Labels off the board that still draw: the selection's (outlined, as a
// lifted entity is) and those a paste would place, ghosted.
function ghostNotes() {
  if (!board || !notesOn()) return null;
  const out = [];
  const held = selNotes();
  if (held.length) out.push({ notes: held, lifted: true });
  for (const pl of placements()) if (pl.kind === "stamp" && pl.notes.length) out.push({ notes: pl.notes, lifted: false });
  return out.length ? out : null;
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

// Reverse the selection's belts and undergrounds, in place: items run the
// other way along the same cells. A belt turns to face WHAT FED IT -- at a
// corner that is the side it came in by, not the reverse of where it was
// pointing (a corner fed from the west and facing north must face west,
// not south, or the reversed run breaks at every turn). A belt whose feeder
// is not in the selection (a run's tail) simply turns round. Feeders are
// read from the selection as it was, all at once, before any is changed --
// through the same emit mask the run tracer uses, since the selection is
// lifted off the board. An underground end swaps entrance for exit AND
// reverses, as the desktop's empty-hand R does to a placed pair. The machines
// with a natural inverse turn into it (reversedGate(): Swap and Reciprocal
// half-turn, Merge and Route trade places and half-turn), so a copied tree of
// them, reversed, undoes what the original did; other machines are not
// touched. The change lands when the selection goes down.
function selectReverse() {
  if (!sel) return;
  const abs = selAbs();
  const at = abs.map(e => [e.dx, e.dy]);
  const mine = emitMask(board, abs, at);
  const W = board.width;
  // Only the selection's own entries count as feeders.
  const emits = (nx, ny, d) => { const k = ny * W + nx; return mine.has(k) && !!(mine.get(k) & (1 << d)); };
  const dirs = abs.map(e => {
    if (e.type === "under") return (e.dir + 2) % 4;
    if (e.type !== "belt") return e.dir;
    const from = board.incoming(e.dx, e.dy, e.dir, emits);
    return from >= 0 ? from : (e.dir + 2) % 4;
  });
  sel.stamp.entries = sel.stamp.entries.map((e, i) => {
    if (e.type === "belt") return { ...e, dir: dirs[i] };
    if (e.type === "under") return { ...e, dir: dirs[i], exit: !e.exit };
    return reversedGate(e);
  });
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
  if (c === "delete") { setSel([], []); return; }
  const pose = [sel.px, sel.py];
  const id = shell.addStamp(sel.stamp);
  if (c === "cut") { setSel([], []); enterPaste(id, pose); if (place) place.keepLayers = true; return; }
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
  if (sel) setSel([], []);
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
  // A hold on an inactive layer's belt is a hold on nothing: it arms a box.
  const lockedHere = board.entityAt(x, y) >= 0 && pickAt(x, y) < 0 && selIndexAt(x, y) < 0;
  const cells = lockedHere ? [] : traceRun(node, (cx, cy, d) => board.incoming(cx, cy, d, emits), x, y);
  if (!cells.length) {
    // A hold on a selected entity stays a press: a slow drag from it still
    // moves the selection, as it always did.
    if (sel && (selIndexAt(x, y) >= 0 || selNoteAt(...cam.screenToWorld(sx, sy)) >= 0)) return false;
    // Anywhere else but a belt, the hold arms a box selection at the press,
    // drawn at once so the hold reads as having taken. The finger now drags
    // the box out; lifting it without moving does nothing.
    const world0 = cam.screenToWorld(sx, sy);
    grab = { box: world0, held: [sx, sy] };
    boxRect = rectOf(world0, world0);
    redraw();
    return "drag";
  }
  // The run is traced whole, but only its active cells are lifted.
  const rows = cells.filter(c => c.row !== undefined && pickable(c.row)).map(c => c.row);
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
    return selIndexAt(...cellUnder(sx, sy)) >= 0 || selNoteAt(...cam.screenToWorld(sx, sy)) >= 0;
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
    endDryRun();      // before the new board's framing, which then stands
    clearSoundSel();  // a new board: everything sounds again
    board = await Board.fromNpz(T, bytes);
    panel.setBoardName(name.replace(/\.npz$/i, ""));
    loadAudioSettings();
    sim = new Sim(T, board);
    syncScreen();
    stopBakedPlay();
    if (audio) audio.rec.attach(sim);
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
    boardName = name.replace(/\.npz$/i, "");
    loadGen++;
    prepareSave();
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

// --- autosave and the save on close -------------------------------------------
//
// Browser saves the app keeps by itself, listed with the others:
//   "auto save"  every 30 s, when the board changed since that slot's last
//   "autosave5"  every 5 min, likewise -- an older copy to fall back on
//   "autosave15" every 15 min, likewise
//   "exitsave"   whenever the app is hidden -- closed, switched away from,
//                the screen locked. On a phone that is the only warning a
//                close gives, and nothing asynchronous can be relied on to
//                finish in it, so the save's bytes are PREPARED ahead of time
//                (compressed after every committed step) and the hide only
//                writes them, synchronously, to localStorage.
// Both write the history's current state -- the last stable board -- so a
// selection half-lifted off the board is saved where it was picked up. On
// opening, the exitsave is loaded, unless the auto save is newer; each
// remembers the name of the board it holds. The two slower tiers are
// backups loaded by hand; resuming never picks them.
//
// NOTHING is written -- no tier, no exitsave -- until the player has edited
// the board this session. Opening the app must not clobber the saves it was
// opened to choose between: an old autosave could otherwise be overwritten
// by whatever the app resumed before the player got round to loading it.
// Loading a board is not an edit.
const AUTO_NAME = "auto save", EXIT_NAME = "exitsave";
const AUTOSAVES = [
  { name: AUTO_NAME, ms: 30 * 1000 },
  { name: "autosave5", ms: 5 * 60 * 1000 },
  { name: "autosave15", ms: 15 * 60 * 1000 },
];
let edited = false;       // has the player changed the board this session?
let boardName = "";
let loadGen = 0;          // which load the prepared bytes belong to
let ready = null;         // { key, bytes }: the current state, compressed
let preparing = false;
const saveKey = () => `${loadGen}:${committed}:${board?.audioRev ?? 0}`;

async function prepareSave() {
  if (!board || preparing) return;
  const key = saveKey();
  if (ready?.key === key) return;
  preparing = true;
  try {
    ready = { key, bytes: new Uint8Array(await board.toNpz(history.current)), name: boardName };
  } catch (e) {
    console.warn("could not prepare the autosave:", e);
  } finally {
    preparing = false;
  }
  if (saveKey() !== key) prepareSave();   // it moved on while compressing
}

function writeAuto(name) {
  if (!ready || !edited) return false;
  try {
    putSave(name, ready.bytes, { board: ready.name });
    return true;
  } catch (e) {
    console.warn(`${name} failed:`, e);
    return false;
  }
}

// One tier's tick: write if the board moved on since this tier last wrote.
function autosave(tier) {
  if (!ready || ready.key === tier.key) return;
  if (writeAuto(tier.name)) {
    tier.key = ready.key;
    panel.setLocalSaves(listSaves());
  }
}

function exitSave() {
  // Commit whatever was edited this very frame, if the board is stable; its
  // bytes may not be ready (compressing is asynchronous), in which case the
  // state one step back is written -- the price of saving with no warning.
  recordStep();
  writeAuto(EXIT_NAME);
}

// The save to open with: the exitsave, unless the auto save is newer.
function resumeSave() {
  const saves = listSaves();
  const exit = saves.find(e => e.name === EXIT_NAME), auto = saves.find(e => e.name === AUTO_NAME);
  if (auto && (!exit || (auto.savedAt || 0) > (exit.savedAt || 0))) return auto;
  return exit || null;
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
  boardName = name;
  panel.setLocalSaves(listSaves(), name);
}

async function loadLocal(name) {
  const bytes = getSave(name);
  if (!bytes) { alert(`"${name}" is not in this browser's storage any more.`); panel.setLocalSaves(listSaves()); return; }
  // An autosave opens under the name of the board it holds, so saving it
  // again does not write over the autosave itself.
  const entry = listSaves().find(e => e.name === name);
  await show(entry?.board || name, bytes.buffer);
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
  T = installPixel(await (await fetch("types.json")).json());

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
    onSelectReverse: () => selectReverse(),
    onSelectCommand: (c) => selectCommand(c),
    // Tapping the stamp being pasted puts it away; any other starts pasting it.
    onPasteStamp: (id) => { if (shell.state.pasteStamp === id) exitPaste(); else enterPaste(id); },
    // ctrl+R: tick 0, every item gone, seeds back on; the factory stays.
    // With a live bake, back to the bake's own tick-0 state, so playing
    // replays exactly what was baked.
    onReset: () => {
      endDryRun();
      if (sim) {
        stopBakedPlay();
        if (bakeLive()) sim.restoreFrom(bake.snap); else sim.reset();
        clock.reset();
        if (audio) audio.rec.stop();
      }
      redraw();
    },
    onPause: (paused) => {
      clock.paused = paused;
      if (paused) endDryRun(); else { startDryRun(); stopPreview(); }
      redraw();
    },
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
    onMakeLayerCurrent: () => {
      if (inspectRow < 0) return;
      setCurrent(board.layers, board.layerOf(inspectRow));
      layersChanged(false);
    },
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
    onView: (v) => { view = v; syncScreen(); layersChanged(false); },
    onColours: (c) => { statusColours = c; redraw(); },
    onAudioEnable: (on) => setAudio(on),
    onBake: () => openBakeDialog(),
    onAudioSelection: () => { setSoundSelection(); redraw(); },
    onVideoRecord: () => recordVideoNow(),
    onVideoEndCamera: (set) => { endDryRun(); endCam = set ? camState() : null; panel.setVideoEndCamera(!!endCam); redraw(); },
    onVideoOpen: (open) => { videoOpen = open; redraw(); },
    onVideo: () => redraw(),
    onAudio: (p) => { if (audio) audio.setParams(p); touchAudio(); },
    onTune: (t) => { setTune(t); redraw(); },
    onClearStamps: () => shell.clearStamps(),
    onCloseStatus: () => inspect(-1),
    // An inactive layer's entity is read only: the edit is refused and the
    // pane shows the value it still holds.
    onEmit: (row, v) => { if (!pickable(row)) { syncInspect(); return; } board.setEmit(row, v); shell.setInspectValue(handValueOf(row)); redraw(); },
    onSeed: (row, on, v) => { if (!pickable(row)) { syncInspect(); return; } board.setSeed(row, on, v); shell.setInspectValue(handValueOf(row)); redraw(); },
    onSaveLocal: (name) => saveLocal(name),
    onLoadLocal: (name) => loadLocal(name),
    onExportLocal: (name) => exportLocal(name),
    onDeleteLocal: (name) => deleteLocal(name),
  });
  panel.setLocalSaves(listSaves());
  view = { ...panel.view };
  syncScreen();

  statusColours = { ...panel.colours };
  layerPanel = new LayerPanel(document.getElementById("stage"), layerActions);
  setTune(panel.dev);

  resize();
  new ResizeObserver(resize).observe(canvas);
  const soundBtn = document.getElementById("sound-btn");
  soundWanted = soundStored();
  setSoundBtn(soundWanted);
  if (soundWanted) { panel.setAudioMeter("starts on your first tap"); armSound(); }
  soundBtn.addEventListener("click", () => { setAudio(!soundWanted); storeSound(soundWanted); soundBtn.blur(); });
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
  // Pick up where the last session left off, if it saved anything.
  const resume = resumeSave();
  if (resume) await loadLocal(resume.name);
  else if (first) await loadBundled(first).catch(e => alert(e.message));

  for (const tier of AUTOSAVES) setInterval(() => autosave(tier), tier.ms);
  document.addEventListener("visibilitychange", () => { if (document.visibilityState === "hidden") exitSave(); });
  window.addEventListener("pagehide", exitSave);
}

init();

// Offline: the service worker caches the whole site on first load (sw.js).
// Registration failing (a file:// page, a browser without workers) only
// costs the offline copy.
if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("sw.js").catch(e => console.warn("no offline copy:", e));
}
