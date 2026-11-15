// The phone shell's controls: the mode buttons, the sim and turn buttons, the
// scrolling swatch bar and the slot picker. Layout and sizes follow
// Fluoddity's sand mode (portrait "band" plan); see style.css.
//
// This file owns UI STATE ONLY. It does not touch the board: it reports
// intents through callbacks, the same way the desktop's panel only requests.
// Nothing here is wired to a simulation yet -- Reset and Play/Pause just
// report, and the turn buttons change the held tool's orientation.

import { icon } from "./icons.js";
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
    this.cb = callbacks;     // { onChange(state), onReset(), onPause(paused) }
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
      ? saved.slots.map(s => (isKnown(T, s) ? { dir: s.dir | 0, flip: !!s.flip, ...s } : null))
      : DEFAULT_SLOTS.map(s => ({ ...s }));
    this.state = {
      mode: MODES.includes(saved.mode) ? saved.mode : "build",
      slots,
      selected: Number.isInteger(saved.selected) && saved.selected < slots.length ? saved.selected : -1,
      boxArmed: false,     // select mode: the next drag is a box
      selCount: 0,         // how many entities are selected (main.js says)
      pasteStamp: null,    // select mode: the stamp being pasted, by id
      stamps: [],          // most recent first; in-session, like the desktop
      paused: false,
      // While main.js is planning something, what the bar should show:
      //   { kind: "belt", routeName, routeIcon }   a belt run
      //   { kind: "place", values }                a machine or raw belt;
      //                                            `values` null, or the
      //                                            number swatches to offer
      planning: null,
      // Hand: { values, current } while Status shows a Source or a belt.
      inspectValue: null,
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
    store({ mode: s.mode, slots: s.slots, selected: s.selected });
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
      </section>
      <section class="sec" id="turnsec">
        <div class="lbl">Turn</div>
        <div class="turnrow">
          <button type="button" class="b round small" id="btn-cw" aria-label="Rotate clockwise">${icon("rotCw")}</button>
          <button type="button" class="b round small" id="btn-ccw" aria-label="Rotate counter-clockwise">${icon("rotCcw")}</button>
          <button type="button" class="b round small" id="btn-flip" aria-label="Flip">${icon("flip")}</button>
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
      b.addEventListener("click", () => this.setMode(m));
      modes.append(b);
    }
    c.querySelector("#btn-reset").addEventListener("click", () => this.cb.onReset?.());
    c.querySelector("#btn-play").addEventListener("click", () => {
      this.state.paused = !this.state.paused;
      this.cb.onPause?.(this.state.paused);
      this.render();
    });
    c.querySelector("#btn-cw").addEventListener("click", () => this.turn(CW));
    c.querySelector("#btn-ccw").addEventListener("click", () => this.turn(CCW));
    c.querySelector("#btn-flip").addEventListener("click", () => this.flip());
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
        <button type="button" class="b tp-cw" aria-label="Rotate clockwise">${icon("rotCw")}</button>`;
      pad.querySelector(".tp-ccw").addEventListener("click", () => this.turn(CCW));
      pad.querySelector(".tp-cw").addEventListener("click", () => this.turn(CW));
      pad.querySelector(".tp-flip").addEventListener("click", () => {
        if (this.state.planning?.kind === "belt") this.cb.onPlanRoute?.();
        else this.flip();
      });
    }
  }

  renderThumbs() {
    const p = this.state.planning;
    // Also while a selection is held: turning it wants a thumb too.
    const show = !!p || this.selecting;
    const flippable = p?.kind === "belt" || (this.heldTool ? canFlip(this.T, this.heldTool) : !!this.activeStamp || this.selecting);
    for (const pad of this.thumbs) {
      pad.dataset.show = String(show);
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

  // --- the swatch bar -------------------------------------------------------

  render() {
    const s = this.state;
    const c = this.els.controls;
    for (const b of c.querySelectorAll("[data-mode]")) {
      b.dataset.selected = String(b.dataset.mode === s.mode);
    }
    const play = c.querySelector("#btn-play");
    play.innerHTML = icon(s.paused ? "play" : "pause");
    play.setAttribute("aria-label", s.paused ? "Play" : "Pause");
    play.title = s.paused ? "Play (space)" : "Pause (space)";

    const planning = !!s.planning;
    const beltRun = s.planning?.kind === "belt";
    const live = beltRun || !!(this.heldTool || this.activeStamp) || this.selecting;
    const flippable = !beltRun && (this.heldTool ? canFlip(this.T, this.heldTool) : !!this.activeStamp || this.selecting);
    c.querySelector("#btn-cw").disabled = !live;
    c.querySelector("#btn-ccw").disabled = !live;
    c.querySelector("#btn-flip").disabled = !flippable;

    const rowMode = planning ? s.planning.kind
      : s.mode === "hand" && s.inspectValue ? "inspect"
      : this.selectActive ? "select" : s.mode;
    this.els.bar.dataset.mode = rowMode;
    const row = this.els.swatches;
    const keep = row.scrollLeft;
    const sameRow = row.dataset.mode === rowMode;
    row.replaceChildren();
    row.dataset.mode = rowMode;
    if (beltRun) this.renderPlanning(row);
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
    btn("plan-route", "route", icon(p.routeIcon), p.routeName, () => this.cb.onPlanRoute?.());
    btn("plan-build", "build", icon("check"), "Build", () => this.cb.onPlanBuild?.());
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
  wireHold(b, onHold) {
    b.addEventListener("contextmenu", (e) => { e.preventDefault(); onHold(); });
    let timer = null, x0 = 0, y0 = 0;
    const cancel = () => { if (timer !== null) clearTimeout(timer); timer = null; };
    b.addEventListener("pointerdown", (e) => {
      if (e.pointerType === "mouse") return;
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
