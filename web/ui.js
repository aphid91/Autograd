// The phone shell's controls: the mode buttons, the sim and turn buttons, the
// scrolling swatch bar and the slot picker. Layout and sizes follow
// Fluoddity's sand mode (portrait "band" plan); see style.css.
//
// This file owns UI STATE ONLY. It does not touch the board: it reports
// intents through callbacks, the same way the desktop's panel only requests.
// Reset, Play/Pause and the tick rate report to main.js, which owns the
// simulation; the turn buttons change the held tool's orientation.

import { icon } from "./icons.js";
import { RATE_MAX, RATE_MIN, RATE_PRESETS } from "./sim.js";
import { CW, CCW, canFlip, catalog, isKnown, labelFor, sameTool, stampArt, toolArt, toolValue } from "./tools.js";
import { copyStamp, mirrored, rotated } from "./stamp.js";

const STORE_KEY = "gates.web.shell.v1";
const LONG_PRESS_MS = 500;   // as sand mode
const LONG_PRESS_SLOP = 10;  // px a held finger may wander
const STAMP_HISTORY = 12;

export const MODES = ["build", "hand"];
// Select mode's bar: Box is a one-shot tool (the next drag is a rectangle);
// the other three are COMMANDS on the current selection, not modes.
export const COMMANDS = ["delete", "cut", "copy"];
const COMMAND_ICON = { delete: "trash", cut: "cut", copy: "copy" };
const COMMAND_LABEL = { delete: "Delete", cut: "Cut", copy: "Copy" };

const DEFAULT_SLOTS = [
  { kind: "belt" }, { kind: "under" },
  { kind: "structure", type: "shear" }, { kind: "structure", type: "source" },
  { kind: "structure", type: "swap" }, { kind: "structure", type: "merge" },
  { kind: "structure", type: "route" },
].map(s => ({ dir: 0, flip: false, ...s }));

// A Source tool starts every session at 0 (Source.EMIT_VALUE). Its number
// carries from one placement to the next within a session, which is what
// makes a row of same-valued Sources quick to lay, but it is not restored
// from storage: a value picked days ago and silently stamped onto every new
// Source read as "the default is 1".
function freshValue(slot) {
  if (slot.kind === "structure" && slot.type === "source") delete slot.value;
  return slot;
}

// A custom rate as the Custom button shows it: short, and never "0.30000004".
function fmtRate(r) { return String(parseFloat(r.toPrecision(3))); }

// localStorage can be absent or throw (private windows, blocked storage); the
// shell must work without it, so every access is guarded.
function loadStored() {
  try { return JSON.parse(localStorage.getItem(STORE_KEY) || "null"); } catch { return null; }
}
function store(data) {
  try { localStorage.setItem(STORE_KEY, JSON.stringify(data)); } catch { /* not persisted */ }
}

export class Shell {
  constructor(T, els, callbacks) {
    this.T = T;
    this.els = els;          // { controls, bar, swatches, swrow, left, right, picker }
    this.cb = callbacks;     // { onChange(state), onReset(), onPause(paused), onRate(tps) }
    // A long press is followed by its finger's release, which the browser
    // turns into a click. That one click is swallowed -- and only that one:
    // the next press anywhere clears the flag, so a quick tap after closing
    // the picker is never eaten. (A timeout alone did eat it.)
    this.swallowClick = false;
    document.addEventListener("pointerdown", () => { this.swallowClick = false; }, true);
    this.nextStampId = 1;

    const saved = loadStored() || {};
    // A bad entry costs one slot, never the bar; an unreadable file costs
    // nothing but the defaults.
    const slots = Array.isArray(saved.slots)
      ? saved.slots.map(s => (isKnown(T, s) ? freshValue({ dir: s.dir | 0, flip: !!s.flip, ...s }) : null))
      : DEFAULT_SLOTS.map(s => ({ ...s }));
    this.state = {
      mode: MODES.includes(saved.mode) ? saved.mode : "build",
      slots,
      selected: Number.isInteger(saved.selected) && saved.selected < slots.length ? saved.selected : -1,
      boxArmed: false,     // select mode: the next drag is a box
      selCount: 0,         // how many entities are selected (main.js says)
      pasteStamp: null,    // select mode: the stamp being pasted, by id
      stamps: [],          // most recent first; in-session, like the desktop
      // The simulation starts paused, so loading a save never sets it going.
      paused: true,
      // Ticks per second. Persisted; a stored value out of range is dropped.
      rate: Number.isFinite(saved.rate) && saved.rate >= RATE_MIN && saved.rate <= RATE_MAX ? saved.rate : 1,
      // The rate panel above the Sim buttons, toggled by a long press on
      // Play/Pause. Not persisted: it is opened to change something.
      simPanel: false,
      customOpen: false,   // the Custom entry row is showing
      // While main.js is planning something, what the bar should show:
      //   { kind: "belt", routeName, routeIcon }   a belt run
      //   { kind: "place", values }                a machine or raw belt;
      //                                            `values` null, or the
      //                                            number swatches to offer
      planning: null,
      // Hand: { values, current } while Status shows a Source or a belt.
      inspectValue: null,
      // The vector (main.js owns it; see vector.js):
      //   on       vectored building is on: what is built repeats along v
      //   picking  the vector picker has the bar and the drags: "build"
      //            (tap the Build button) or "select" (the Vector swatch)
      //   count    how many copies, the original included
      vector: { on: false, picking: null, count: 2 },
    };

    this.buildControls();
    this.buildThumbs();
    this.wireScroll();
    this.wirePicker();
    this.render();
  }

  // --- persistence and notification -------------------------------------

  changed() {
    const s = this.state;
    store({ mode: s.mode, slots: s.slots, selected: s.selected, rate: s.rate });
    this.render();
    this.cb.onChange?.(s);
  }

  // The Build tool that taps use. Null while selecting or pasting: the
  // selection takes the bar and the taps, and the tool comes back the
  // moment nothing is selected or pasted.
  get heldTool() {
    const s = this.state;
    if (this.selectActive) return null;
    return s.mode === "build" && s.selected >= 0 ? s.slots[s.selected] : null;
  }

  // Selection is a state of Build, not a mode: entered by tapping an entity,
  // left when nothing is selected and nothing is being pasted.
  get selectActive() {
    const s = this.state;
    return s.mode === "build" && (s.selCount > 0 || s.pasteStamp !== null);
  }

  // The stamp being pasted in Select, or null. `.stamp` is its geometry.
  get activeStamp() {
    const s = this.state;
    if (s.mode !== "build" || s.pasteStamp === null) return null;
    return s.stamps.find(t => t.id === s.pasteStamp) || null;
  }

  get selecting() {
    return this.state.mode === "build" && this.state.pasteStamp === null && this.state.selCount > 0;
  }

  // --- the controls row ---------------------------------------------------

  buildControls() {
    const c = this.els.controls;
    c.innerHTML = `
      <section class="sec" id="modesec">
        <div class="lbl">Mode</div>
        <div class="grid" id="modes"></div>
      </section>
      <section class="sec" id="simsec">
        <div class="lbl">Sim</div>
        <div class="simrow">
          <button type="button" class="b round reset" id="btn-reset" aria-label="Reset simulation" title="Reset (ctrl+R)">${icon("reset")}</button>
          <button type="button" class="b round" id="btn-play" aria-label="Pause"></button>
        </div>
        <div class="simpanel" id="simpanel" data-open="false" role="dialog" aria-label="Simulation speed">
          <div class="simpanel-head">
            <span class="lbl">Ticks / sec</span>
            <span class="simpanel-tick" id="sim-tick"></span>
            <button type="button" class="b simpanel-x" id="sim-close" aria-label="Close">${icon("x")}</button>
          </div>
          <div class="simpanel-rates" id="sim-rates"></div>
          <form class="simpanel-custom" id="sim-custom" data-open="false">
            <input id="sim-custom-in" type="text" inputmode="decimal" autocomplete="off" spellcheck="false"
                   aria-label="Custom ticks per second" placeholder="${RATE_MIN}–${RATE_MAX}">
            <button type="submit" class="b simpanel-set">Set</button>
          </form>
        </div>
      </section>
      <section class="sec" id="turnsec">
        <div class="lbl">Edit</div>
        <div class="turnrow">
          <button type="button" class="b round small" id="btn-undo" aria-label="Undo" title="Undo">${icon("undo")}</button>
          <button type="button" class="b round small" id="btn-redo" aria-label="Redo" title="Redo">${icon("redo")}</button>
          <button type="button" class="b round small" id="btn-checkpoint" aria-label="Checkpoint" title="Checkpoint: undo from the oldest step comes back here">${icon("checkpoint")}</button>
        </div>
      </section>`;

    const modes = c.querySelector("#modes");
    const names = { build: "Build", hand: "Hand", select: "Select" };
    for (const m of MODES) {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "b tool";
      b.dataset.mode = m;
      b.innerHTML = `${icon(m)}<span>${names[m]}</span>`;
      // Tapping Build while already in Build toggles the vector picker.
      b.addEventListener("click", () => {
        if (m === "build" && this.state.mode === "build") this.cb.onBuildTap?.();
        else this.setMode(m);
      });
      modes.append(b);
    }
    c.querySelector("#btn-reset").addEventListener("click", () => this.cb.onReset?.());
    const play = c.querySelector("#btn-play");
    play.addEventListener("click", () => {
      if (this.swallowClick) { this.swallowClick = false; return; }
      this.setPaused(!this.state.paused);
    });
    // A long press (right-click on a mouse) toggles the rate panel.
    this.wireHold(play, () => this.toggleSimPanel());
    this.buildSimPanel();
    c.querySelector("#btn-undo").addEventListener("click", () => this.cb.onUndo?.());
    c.querySelector("#btn-redo").addEventListener("click", () => this.cb.onRedo?.());
    const cp = c.querySelector("#btn-checkpoint");
    cp.addEventListener("click", () => {
      this.cb.onCheckpoint?.();
      // The press changes nothing visible on the board, so the button says so.
      cp.dataset.flash = "true";
      setTimeout(() => { cp.dataset.flash = "false"; }, 450);
    });
  }

  // What Undo and Redo can do now (main.js owns the history).
  setHistory(canUndo, canRedo) {
    const c = this.els.controls;
    c.querySelector("#btn-undo").disabled = !canUndo;
    c.querySelector("#btn-redo").disabled = !canRedo;
  }

  // The thumb pads: Turn's three buttons in the board's bottom corners while
  // something is planned. Flip on a belt run toggles the route instead,
  // since a run has nothing to mirror.
  buildThumbs() {
    this.thumbs = ["thumb-left", "thumb-right"].map(id => document.getElementById(id)).filter(Boolean);
    for (const pad of this.thumbs) {
      pad.innerHTML = `
        <button type="button" class="b tp-flip" aria-label="Flip">${icon("flip")}</button>
        <button type="button" class="b tp-ccw" aria-label="Rotate counter-clockwise">${icon("rotCcw")}</button>
        <button type="button" class="b tp-cw" aria-label="Rotate clockwise">${icon("rotCw")}</button>
        <button type="button" class="b tp-less" aria-label="Fewer copies">${icon("minus")}</button>
        <button type="button" class="b tp-more" aria-label="More copies">${icon("plus")}</button>`;
      pad.querySelector(".tp-ccw").addEventListener("click", () => this.turn(CCW));
      pad.querySelector(".tp-cw").addEventListener("click", () => this.turn(CW));
      pad.querySelector(".tp-flip").addEventListener("click", () => {
        if (this.state.vector.picking) this.cb.onVectorFlip?.();
        else if (this.state.planning?.kind === "belt") this.cb.onPlanRoute?.();
        else this.flip();
      });
      pad.querySelector(".tp-less").addEventListener("click", () => this.cb.onVectorCount?.(false));
      pad.querySelector(".tp-more").addEventListener("click", () => this.cb.onVectorCount?.(true));
    }
  }

  renderThumbs() {
    const p = this.state.planning;
    // Also while a selection is held, or a tool is held to turn before
    // placing: the pads are the only Turn buttons now (the row's became
    // Undo, Redo and Checkpoint).
    const picking = this.state.vector.picking;
    const show = !!picking || !!p || this.selecting || (this.state.mode === "build" && !!this.heldTool);
    const flippable = !!picking || p?.kind === "belt" || (this.heldTool ? canFlip(this.T, this.heldTool) : !!this.activeStamp || this.selecting);
    for (const pad of this.thumbs) {
      pad.dataset.show = String(show);
      // The vector picker for building adds a count row: fewer, more.
      pad.dataset.count = String(picking === "build");
      pad.querySelector(".tp-flip").disabled = !flippable;
    }
  }

  setMode(m) {
    if (this.state.mode === m) return;
    this.state.mode = m;
    // Leaving Build ends a paste or a Box; the stamp history survives.
    if (m !== "build") { this.state.pasteStamp = null; this.state.boxArmed = false; }
    this.changed();
  }

  // R and F act on whatever the hand holds: a tool in Build (a stamp tool
  // turns its stamp), the stamp being pasted, or the selection -- which
  // main.js owns, so that one is a request.
  turn(fn) {
    // While the vector picker is open, the buttons turn the vector.
    if (this.state.vector.picking) { this.cb.onVectorTurn?.(fn === CW); return; }
    // While a belt run is planned the buttons turn its tip, not the tool.
    // While placing, they turn the tool -- the ghost follows it.
    if (this.state.planning?.kind === "belt") { this.cb.onPlanTurn?.(fn === CW); return; }
    const cw = fn === CW;
    const tool = this.heldTool;
    if (tool?.kind === "stamp") { tool.stamp = rotated(this.T, tool.stamp, cw); this.changed(); return; }
    if (tool) { tool.dir = fn(tool.dir | 0); this.changed(); return; }
    const st = this.activeStamp;
    if (st) { st.stamp = rotated(this.T, st.stamp, cw); this.changed(); return; }
    if (this.selecting) this.cb.onSelectTurn?.(cw);
  }

  flip() {
    if (this.state.vector.picking) { this.cb.onVectorFlip?.(); return; }
    if (this.state.planning?.kind === "belt") return;   // nothing to mirror on a run
    const tool = this.heldTool;
    if (tool?.kind === "stamp") { tool.stamp = mirrored(this.T, tool.stamp); this.changed(); return; }
    if (tool) {
      if (!canFlip(this.T, tool)) return;
      tool.flip = !tool.flip;
      this.changed();
      return;
    }
    const st = this.activeStamp;
    if (st) { st.stamp = mirrored(this.T, st.stamp); this.changed(); return; }
    if (this.selecting) this.cb.onSelectFlip?.();
  }

  // --- the simulation controls ---------------------------------------------

  setPaused(paused) {
    if (this.state.paused === paused) return;
    this.state.paused = paused;
    this.cb.onPause?.(paused);
    this.render();
  }

  setRate(rate) {
    this.state.rate = rate;
    this.changed();
    this.cb.onRate?.(rate);
  }

  toggleSimPanel(open = !this.state.simPanel) {
    this.state.simPanel = open;
    if (!open) this.state.customOpen = false;
    this.render();
  }

  // The panel's buttons: a preset per rate, then Custom, which opens a text
  // entry. The panel stays open until the next long press or its close
  // button, so rates can be compared without reopening it.
  buildSimPanel() {
    const c = this.els.controls;
    const rates = c.querySelector("#sim-rates");
    for (const r of RATE_PRESETS) {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "b simpanel-rate";
      b.dataset.rate = String(r);
      b.textContent = String(r);
      b.addEventListener("click", () => { this.state.customOpen = false; this.setRate(r); });
      rates.append(b);
    }
    const custom = document.createElement("button");
    custom.type = "button";
    custom.className = "b simpanel-rate";
    custom.id = "sim-custom-btn";
    custom.textContent = "Custom";
    custom.addEventListener("click", () => {
      this.state.customOpen = !this.state.customOpen;
      this.render();
      if (this.state.customOpen) {
        const input = c.querySelector("#sim-custom-in");
        input.value = RATE_PRESETS.includes(this.state.rate) ? "" : String(this.state.rate);
        input.removeAttribute("aria-invalid");
        input.focus();
      }
    });
    rates.append(custom);

    const form = c.querySelector("#sim-custom");
    const input = c.querySelector("#sim-custom-in");
    form.addEventListener("submit", (e) => {
      e.preventDefault();
      const v = Number(input.value.trim().replace(",", "."));
      if (!input.value.trim() || !Number.isFinite(v) || v < RATE_MIN || v > RATE_MAX) {
        input.setAttribute("aria-invalid", "true");
        return;
      }
      input.removeAttribute("aria-invalid");
      input.blur();
      this.setRate(v);
    });
    input.addEventListener("input", () => input.removeAttribute("aria-invalid"));
    c.querySelector("#sim-close").addEventListener("click", () => this.toggleSimPanel(false));
  }

  // The tick counter in the panel's header. Cheap enough to call every frame:
  // it touches the DOM only when the number changes.
  setTick(tick) {
    if (tick === this.shownTick) return;
    this.shownTick = tick;
    this.els.controls.querySelector("#sim-tick").textContent = `tick ${tick}`;
  }

  // --- the swatch bar -------------------------------------------------------

  render() {
    const s = this.state;
    const c = this.els.controls;
    for (const b of c.querySelectorAll("[data-mode]")) {
      b.dataset.selected = String(b.dataset.mode === s.mode);
    }
    // Build wears the vector: tinted while vectored building is on, and an
    // arrow across the hammer while its picker is open.
    const bb = c.querySelector('[data-mode="build"]');
    const picking = s.vector.picking === "build";
    bb.dataset.vector = String(s.vector.on || picking);
    if (bb.dataset.picking !== String(picking)) {
      bb.dataset.picking = String(picking);
      bb.innerHTML = `${picking ? icon("buildVector") : icon("build")}<span>Build</span>`;
    }
    const play = c.querySelector("#btn-play");
    play.innerHTML = icon(s.paused ? "play" : "pause");
    play.setAttribute("aria-label", s.paused ? "Play" : "Pause");
    play.title = s.paused ? "Play (space) -- hold for speed" : "Pause (space) -- hold for speed";

    const panel = c.querySelector("#simpanel");
    panel.dataset.open = String(s.simPanel);
    const preset = RATE_PRESETS.includes(s.rate);
    for (const b of panel.querySelectorAll("[data-rate]")) {
      b.dataset.selected = String(Number(b.dataset.rate) === s.rate);
    }
    const customBtn = panel.querySelector("#sim-custom-btn");
    customBtn.dataset.selected = String(!preset || s.customOpen);
    customBtn.textContent = preset ? "Custom" : `${fmtRate(s.rate)}`;
    panel.querySelector("#sim-custom").dataset.open = String(s.customOpen);

    const planning = !!s.planning;
    const beltRun = s.planning?.kind === "belt";

    const rowMode = s.vector.picking ? "vector" : planning ? s.planning.kind
      : s.mode === "hand" && s.inspectValue ? "inspect"
      : this.selectActive ? "select" : s.mode;
    this.els.bar.dataset.mode = rowMode;
    const row = this.els.swatches;
    const keep = row.scrollLeft;
    const sameRow = row.dataset.mode === rowMode;
    row.replaceChildren();
    row.dataset.mode = rowMode;
    if (s.vector.picking) this.renderVector(row);
    else if (beltRun) this.renderPlanning(row);
    else if (planning) this.renderPlacing(row);
    else if (this.selectActive) this.renderSelect(row);
    else if (s.mode === "build") this.renderBuild(row);
    else if (s.mode === "hand") this.renderHand(row);
    // Re-rendering the same row must not throw away the player's scroll.
    if (sameRow) row.scrollLeft = keep;
    this.updateArrows();
    this.renderThumbs();
  }

  swatch({ art, name, selected = false, secondary = false, empty = false, onTap, onHold, cls = "" }) {
    const item = document.createElement("div");
    item.className = `sw ${cls}`;
    item.dataset.selected = String(selected);
    if (secondary) item.dataset.secondary = "true";
    if (empty) item.dataset.empty = "true";
    const b = document.createElement("button");
    b.type = "button";
    b.className = "swb";
    b.setAttribute("aria-label", name);
    b.innerHTML = art;
    const label = document.createElement("span");
    label.className = "sw-name";
    label.textContent = name;
    b.addEventListener("click", () => {
      if (this.swallowClick) { this.swallowClick = false; return; }
      onTap?.();
    });
    if (onHold) this.wireHold(b, onHold);
    else b.addEventListener("contextmenu", (e) => e.preventDefault());
    item.append(b, label);
    return item;
  }

  renderBuild(row) {
    const s = this.state;
    s.slots.forEach((slot, i) => {
      const empty = !slot;
      row.append(this.swatch({
        art: empty ? "" : toolArt(this.T, slot),
        name: empty ? "Empty" : labelFor(this.T, slot),
        selected: s.selected === i,
        empty,
        // Tap selects; tapping the held slot again puts it down.
        onTap: () => { s.selected = s.selected === i ? -1 : i; this.changed(); },
        onHold: () => this.openPicker(i),
      }));
    });
    row.append(this.swatch({
      art: icon("plus"), name: "Add", cls: "addsw",
      onTap: () => this.openPicker(null),
    }));
  }

  // While a belt run is planned the bar is its controls. The row keeps its
  // height, so the board does not jump when planning starts or ends.
  renderPlanning(row) {
    const p = this.state.planning;
    const btn = (id, cls, html, label, fn) => {
      const b = document.createElement("button");
      b.type = "button";
      b.className = `b planbtn ${cls}`;
      b.id = id;
      b.innerHTML = `${html}<span>${label}</span>`;
      b.addEventListener("click", fn);
      row.append(b);
    };
    btn("plan-cancel", "cancel", icon("x"), "Cancel", () => this.cb.onPlanCancel?.());
    btn("plan-route", `route${p.forced ? " forced" : ""}`, icon(p.routeIcon), p.routeName, () => this.cb.onPlanRoute?.());
    btn("plan-build", "build", icon("check"), "Build", () => this.cb.onPlanBuild?.());
  }

  // The vector picker's bar. Building: Cancel (vectored building off),
  // Reset (the offset back on the anchor), Less and More (halve or double
  // the copies). Selecting: Cancel, Reset and Confirm -- that vector runs
  // until the pattern stops repeating, so it has no count.
  renderVector(row) {
    const v = this.state.vector;
    const btn = (id, cls, html, label, fn) => {
      const b = document.createElement("button");
      b.type = "button";
      b.className = `b planbtn compact ${cls}`;
      b.id = id;
      b.innerHTML = `${html}<span>${label}</span>`;
      b.addEventListener("click", fn);
      row.append(b);
    };
    btn("vec-cancel", "cancel", icon("x"), "Cancel", () => this.cb.onVectorCancel?.());
    btn("vec-reset", "", icon("reset"), "Reset", () => this.cb.onVectorReset?.());
    if (v.picking === "build") {
      btn("vec-less", "", icon("minus"), "Less", () => this.cb.onVectorCount?.(false));
      btn("vec-more", "", icon("plus"), `More · ${v.count}`, () => this.cb.onVectorCount?.(true));
    } else {
      btn("vec-confirm", "build", icon("check"), "Confirm", () => this.cb.onVectorConfirm?.());
    }
  }

  setVector(v) {
    this.state.vector = { ...this.state.vector, ...v };
    this.render();
  }

  // Placing a machine or a raw belt: Cancel, the two rotations, and -- for
  // the two tools with a number to choose -- the number swatches. The
  // rotations duplicate the Turn row on purpose: they sit under the thumb
  // that is already in the bar.
  renderPlacing(row) {
    const btn = (id, cls, html, label, fn) => {
      const b = document.createElement("button");
      b.type = "button";
      b.className = `b planbtn compact ${cls}`;
      b.id = id;
      b.innerHTML = `${html}<span>${label}</span>`;
      b.addEventListener("click", fn);
      row.append(b);
    };
    btn("place-cancel", "cancel", icon("x"), "Cancel", () => this.cb.onPlaceCancel?.());
    btn("place-ccw", "", icon("rotCcw"), "Left", () => this.turn(CCW));
    btn("place-cw", "", icon("rotCw"), "Right", () => this.turn(CW));
    const values = this.state.planning.values;
    const tool = this.heldTool;
    if (!values || !tool) return;
    row.append(this.numberGrid(values, toolValue(tool), (v) => this.setValue(v)));
    // A Source can emit anything: a box for a number the swatches lack.
    if (tool.kind === "structure") row.append(this.customValue(values, toolValue(tool), (v) => this.setValue(v)));
  }

  // A text box beside the number swatches, for a Source value they do not
  // offer. It shows the current value when that value is a custom one (and
  // is lit, as a swatch would be), and is empty otherwise. It applies on
  // Enter or when it loses focus, never per keystroke -- the bar re-renders
  // on a change and would take the field away mid-number.
  customValue(presets, current, onPick) {
    const custom = current !== null && !presets.includes(current);
    const wrap = document.createElement("form");
    wrap.className = "numcustom";
    wrap.dataset.selected = String(custom);
    const input = document.createElement("input");
    input.type = "text";
    // The plain keyboard: iOS's decimal pad has no minus sign.
    input.autocomplete = "off";
    input.spellcheck = false;
    input.placeholder = "other";
    input.setAttribute("aria-label", "Custom value");
    input.value = custom ? String(current) : "";
    const apply = () => {
      const t = input.value.trim().replace(",", ".");
      if (!t) return;
      const v = Number(t);
      if (!Number.isFinite(v)) { input.setAttribute("aria-invalid", "true"); return; }
      input.removeAttribute("aria-invalid");
      // Stored as the float32 the entity row will hold, so the box and the
      // board show the same number.
      const f = Math.fround(v);
      if (f !== current) onPick(f);
    };
    wrap.addEventListener("submit", (e) => { e.preventDefault(); input.blur(); });
    input.addEventListener("change", apply);
    input.addEventListener("input", () => input.removeAttribute("aria-invalid"));
    wrap.append(input);
    return wrap;
  }

  // Two rows of three number swatches, so they fit beside a few buttons
  // without the row scrolling them out of reach. `current` is the lit one,
  // or null for none.
  numberGrid(values, current, onPick) {
    const grid = document.createElement("div");
    grid.className = "numgrid";
    for (const v of values) {
      const sw = this.swatch({
        art: `<span class="num">${v}</span>`, name: String(v), cls: `numsw ${v > 0 ? "pos" : "neg"}`,
        selected: current === v,
        onTap: () => onPick(v),
      });
      sw.dataset.value = String(v);
      grid.append(sw);
    }
    return grid;
  }

  // The Hand: while Status is open on a Source or a belt, the bar offers
  // its number -- the emit value, or the seed (tap the lit one to unseed).
  renderHand(row) {
    const iv = this.state.inspectValue;
    if (!iv) return;
    const label = document.createElement("span");
    label.className = "rowlabel";
    label.textContent = iv.kind === "emit" ? "Emits" : "Seed";
    row.append(label, this.numberGrid(iv.values, iv.current, (v) => this.cb.onInspectValue?.(v)));
    if (iv.kind === "emit") row.append(this.customValue(iv.values, iv.current, (v) => this.cb.onInspectValue?.(v)));
  }

  setInspectValue(iv) {
    this.state.inspectValue = iv;
    this.render();
  }

  // The held tool's number: a Source's emit value, or a raw belt's seed.
  // A Source always has one; a raw belt may have none (no seed), so tapping
  // its selected number again clears it.
  setValue(v) {
    const tool = this.heldTool;
    if (!tool) return;
    tool.value = tool.kind === "rawbelt" && toolValue(tool) === v ? null : v;
    this.changed();
  }

  setPlanning(p) {
    this.state.planning = p;
    this.render();
  }

  renderSelect(row) {
    const s = this.state;
    row.append(this.swatch({
      art: icon("box"), name: "Box", cls: "actsw act-box",
      selected: s.boxArmed,
      onTap: () => { s.boxArmed = !s.boxArmed; this.changed(); },
    }));
    for (const c of COMMANDS) {
      const sw = this.swatch({
        art: icon(COMMAND_ICON[c]), name: COMMAND_LABEL[c], cls: `actsw act-${c}`,
        onTap: () => { if (s.selCount) this.cb.onSelectCommand?.(c); },
      });
      if (!s.selCount) sw.dataset.disabled = "true";
      row.append(sw);
    }
    // The history, most recent first. Tapping one pastes it.
    for (const st of s.stamps) {
      row.append(this.swatch({
        art: stampArt(this.T, st.stamp), name: st.name, cls: "stampsw",
        selected: s.pasteStamp === st.id,
        onTap: () => this.cb.onPasteStamp?.(st.id),
        onHold: () => this.openStampMenu(st.id),
      }));
    }
  }

  // A cut or copy lands at the front of the history. Returns its id.
  addStamp(stamp) {
    const s = this.state;
    const id = this.nextStampId++;
    s.stamps.unshift({ id, name: `Stamp ${id}`, stamp: copyStamp(stamp) });
    s.stamps.length = Math.min(s.stamps.length, STAMP_HISTORY);
    this.changed();
    return id;
  }

  // Long press on a stamp: rename it, or delete it from the history. A
  // stamp baked into a toolbar slot is a copy, so deleting the history's
  // one leaves the slot alone.
  openStampMenu(id) {
    const m = document.getElementById("stampmenu");
    const s = this.state;
    const st = s.stamps.find(t => t.id === id);
    if (!m || !st) return;
    const name = m.querySelector("#stampmenu-name");
    name.value = st.name;
    const close = () => { m.dataset.open = "false"; name.blur(); };
    const rename = () => {
      const v = name.value.trim();
      if (v) st.name = v;
      close();
      this.changed();
    };
    m.querySelector("#stampmenu-rename").onclick = rename;
    name.onkeydown = (e) => { if (e.key === "Enter") rename(); if (e.key === "Escape") close(); };
    m.querySelector("#stampmenu-cancel").onclick = close;
    m.onclick = (e) => { if (e.target === m) close(); };
    m.querySelector("#stampmenu-delete").onclick = () => {
      close();
      s.stamps = s.stamps.filter(t => t.id !== id);
      if (s.pasteStamp === id) this.cb.onPasteStamp?.(id);   // stops the paste
      this.changed();
    };
    m.dataset.open = "true";
  }

  setPasting(id) {
    this.state.pasteStamp = id;
    this.state.boxArmed = false;
    this.changed();
  }

  setSelection(n) {
    const s = this.state;
    if (s.selCount === n) return;
    s.selCount = n;
    // Nothing left to add to: a Box armed for this selection goes with it.
    if (!n) s.boxArmed = false;
    this.render();
  }

  clearStamps() {
    const s = this.state;
    s.stamps = [];
    s.pasteStamp = null;
    this.changed();
  }

  // --- long press -------------------------------------------------------------
  // Right-click on a mouse; on touch, holding still for LONG_PRESS_MS. The
  // release that follows a long press must not also count as a tap.
  //
  // A touch long press ALSO fires the browser's own contextmenu (Android
  // Chrome, at about the same moment as our timer), so contextmenu acts only
  // for a mouse. Acting on both ran onHold twice -- harmless for the
  // picker, but the rate panel is a toggle, so it opened and shut at once
  // unless the hold happened to land between the two.
  wireHold(b, onHold) {
    let touch = false;
    b.addEventListener("contextmenu", (e) => { e.preventDefault(); if (!touch) onHold(); });
    let timer = null, x0 = 0, y0 = 0;
    const cancel = () => { if (timer !== null) clearTimeout(timer); timer = null; };
    b.addEventListener("pointerdown", (e) => {
      touch = e.pointerType !== "mouse";
      if (!touch) return;
      x0 = e.clientX; y0 = e.clientY;
      cancel();
      timer = setTimeout(() => {
        timer = null;
        this.swallowClick = true;
        onHold();
      }, LONG_PRESS_MS);
    });
    b.addEventListener("pointermove", (e) => {
      if (timer !== null && Math.hypot(e.clientX - x0, e.clientY - y0) > LONG_PRESS_SLOP) cancel();
    });
    for (const ev of ["pointerup", "pointercancel", "pointerleave"]) b.addEventListener(ev, cancel);
  }

  // --- scrolling ----------------------------------------------------------------
  // A finger scrolls the row natively; the arrows page it for everyone else.

  wireScroll() {
    const row = this.els.swatches;
    row.addEventListener("scroll", () => this.updateArrows(), { passive: true });
    new ResizeObserver(() => this.updateArrows()).observe(row);
    const page = (dir) => row.scrollBy({ left: dir * Math.max(60, row.clientWidth - 60), behavior: "smooth" });
    this.els.left.innerHTML = icon("left");
    this.els.right.innerHTML = icon("right");
    this.els.left.addEventListener("click", () => page(-1));
    this.els.right.addEventListener("click", () => page(1));
  }

  updateArrows() {
    const row = this.els.swatches;
    const max = row.scrollWidth - row.clientWidth;
    this.els.bar.dataset.overflowing = String(max > 1);
    this.els.swrow.dataset.left = String(row.scrollLeft > 1);
    this.els.swrow.dataset.right = String(row.scrollLeft < max - 1);
  }

  // --- the picker -----------------------------------------------------------------
  // `slot` null appends; an index replaces that slot (or removes it).

  wirePicker() {
    const p = this.els.picker;
    p.addEventListener("click", (e) => { if (e.target === p) this.closePicker(); });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && p.dataset.open === "true") this.closePicker();
    });
  }

  openPicker(slot) {
    const p = this.els.picker;
    const s = this.state;
    const current = slot === null ? null : s.slots[slot];
    p.querySelector("#picker-title").textContent =
      slot === null ? "Add a tool" : `Slot ${slot + 1}`;
    const list = p.querySelector("#picker-list");
    list.replaceChildren();
    // The machines, then the stamp history: baking a stamp into a slot is
    // how one from deep in the history is kept to hand.
    const entries = [
      ...catalog(this.T),
      ...s.stamps.map(st => ({ kind: "stamp", label: st.name, stamp: st.stamp, name: st.name })),
    ];
    for (const entry of entries) {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "pick";
      if (current && sameTool(current, entry)) b.dataset.current = "true";
      b.innerHTML = `<span class="pick-art">${toolArt(this.T, { ...entry, dir: 0, flip: false })}</span>` +
                    `<span>${entry.label}</span>`;
      b.addEventListener("click", () => {
        const tool = entry.kind === "stamp"
          ? { kind: "stamp", name: entry.name, stamp: copyStamp(entry.stamp), dir: 0, flip: false }
          : { kind: entry.kind, ...(entry.type ? { type: entry.type } : {}), dir: 0, flip: false };
        if (slot === null) { s.slots.push(tool); s.selected = s.slots.length - 1; }
        else { s.slots[slot] = tool; s.selected = slot; }
        this.closePicker();
        this.changed();
        if (slot === null) this.els.swatches.scrollLeft = this.els.swatches.scrollWidth;
      });
      list.append(b);
    }
    const remove = p.querySelector("#picker-remove");
    remove.hidden = slot === null;
    remove.onclick = () => {
      s.slots.splice(slot, 1);
      if (s.selected === slot) s.selected = -1;
      else if (s.selected > slot) s.selected--;
      this.closePicker();
      this.changed();
    };
    p.querySelector("#picker-cancel").onclick = () => this.closePicker();
    p.dataset.open = "true";
  }

  closePicker() { this.els.picker.dataset.open = "false"; }
}
