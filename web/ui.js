// The phone shell's controls: the mode buttons, the sim and turn buttons, the
// scrolling swatch bar and the slot picker. Layout and sizes follow
// Fluoddity's sand mode (portrait "band" plan); see style.css.
//
// This file owns UI STATE ONLY. It does not touch the board: it reports
// intents through callbacks, the same way the desktop's panel only requests.
// Nothing here is wired to a simulation yet -- Reset and Play/Pause just
// report, and the turn buttons change the held tool's orientation.

import { icon } from "./icons.js";
import { CW, CCW, canFlip, catalog, isKnown, labelFor, sameTool, stampArt, toolArt } from "./tools.js";

const STORE_KEY = "gates.web.shell.v1";
const LONG_PRESS_MS = 500;   // as sand mode
const LONG_PRESS_SLOP = 10;  // px a held finger may wander
const STAMP_HISTORY = 12;

export const MODES = ["build", "hand", "select"];
export const ACTIONS = ["delete", "cut", "copy", "paste"];
const ACTION_ICON = { delete: "trash", cut: "cut", copy: "copy", paste: "paste" };
const ACTION_LABEL = { delete: "Delete", cut: "Cut", copy: "Copy", paste: "Paste" };

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
      action: null,        // select mode: which of ACTIONS is up
      pasteStamp: null,    // ...and, for paste, which stamp
      stamps: [],          // most recent first; in-session, like the desktop
      paused: false,
      planning: null,      // { routeName, routeIcon } while a belt run is planned
    };

    this.buildControls();
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

  get heldTool() {
    const s = this.state;
    return s.mode === "build" && s.selected >= 0 ? s.slots[s.selected] : null;
  }

  // The stamp the turn buttons act on: the one Paste is holding.
  get activeStamp() {
    const s = this.state;
    if (s.mode !== "select" || s.action !== "paste") return null;
    return s.stamps.find(t => t.id === s.pasteStamp) || null;
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

  setMode(m) {
    if (this.state.mode === m) return;
    this.state.mode = m;
    // Leaving Select ends its gesture; the stamp history survives.
    if (m !== "select") { this.state.action = null; this.state.pasteStamp = null; }
    this.changed();
  }

  // R and F act on whatever the hand holds: a tool in Build, the stamp being
  // pasted in Select. In Hand they will turn what is tapped -- deferred.
  turn(fn) {
    // While a run is planned the buttons turn its tip, not the tool.
    if (this.state.planning) { this.cb.onPlanTurn?.(fn === CW); return; }
    const target = this.heldTool || this.activeStamp;
    if (!target) return;
    target.dir = fn(target.dir | 0);
    this.changed();
  }

  flip() {
    if (this.state.planning) return;   // nothing to mirror on a belt run
    const tool = this.heldTool;
    if (tool) {
      if (!canFlip(this.T, tool)) return;
      tool.flip = !tool.flip;
      this.changed();
      return;
    }
    const st = this.activeStamp;
    if (st) { st.flip = !st.flip; this.changed(); }
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
    const live = planning || !!(this.heldTool || this.activeStamp);
    const flippable = !planning && (this.heldTool ? canFlip(this.T, this.heldTool) : !!this.activeStamp);
    c.querySelector("#btn-cw").disabled = !live;
    c.querySelector("#btn-ccw").disabled = !live;
    c.querySelector("#btn-flip").disabled = !flippable;

    const rowMode = planning ? "plan" : s.mode;
    this.els.bar.dataset.mode = rowMode;
    const row = this.els.swatches;
    const keep = row.scrollLeft;
    const sameRow = row.dataset.mode === rowMode;
    row.replaceChildren();
    row.dataset.mode = rowMode;
    if (planning) this.renderPlanning(row);
    else if (s.mode === "build") this.renderBuild(row);
    else if (s.mode === "select") this.renderSelect(row);
    // Re-rendering the same row must not throw away the player's scroll.
    if (sameRow) row.scrollLeft = keep;
    this.updateArrows();
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

  setPlanning(p) {
    this.state.planning = p;
    this.render();
  }

  renderSelect(row) {
    const s = this.state;
    for (const a of ACTIONS) {
      row.append(this.swatch({
        art: icon(ACTION_ICON[a]), name: ACTION_LABEL[a], cls: `actsw act-${a}`,
        selected: s.action === a,
        onTap: () => {
          if (s.action === a) { s.action = null; s.pasteStamp = null; }
          else {
            s.action = a;
            // Paste defaults to the most recent stamp.
            s.pasteStamp = a === "paste" ? (s.stamps[0]?.id ?? null) : null;
          }
          this.changed();
        },
      }));
    }
    for (const st of s.stamps) {
      row.append(this.swatch({
        art: stampArt(st), name: st.name, cls: "stampsw",
        selected: s.action === "paste" && s.pasteStamp === st.id,
        // Tapping a stamp is "paste this one".
        onTap: () => {
          if (s.action === "paste" && s.pasteStamp === st.id) { s.action = null; s.pasteStamp = null; }
          else { s.action = "paste"; s.pasteStamp = st.id; }
          this.changed();
        },
      }));
    }
  }

  // A cut or copy lands here, most recent first. Called by the board side
  // once cut/copy exist; for now the debug panel calls it.
  addStamp(name) {
    const s = this.state;
    const id = this.nextStampId++;
    s.stamps.unshift({ id, name: name || `Stamp ${id}`, dir: 0, flip: false });
    s.stamps.length = Math.min(s.stamps.length, STAMP_HISTORY);
    if (s.action === "paste" && !s.stamps.some(t => t.id === s.pasteStamp)) s.pasteStamp = s.stamps[0].id;
    this.changed();
    return id;
  }

  clearStamps() {
    const s = this.state;
    s.stamps = [];
    if (s.action === "paste") s.pasteStamp = null;
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
    for (const entry of catalog(this.T)) {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "pick";
      if (current && sameTool(current, entry)) b.dataset.current = "true";
      b.innerHTML = `<span class="pick-art">${toolArt(this.T, { ...entry, dir: 0, flip: false })}</span>` +
                    `<span>${entry.label}</span>`;
      b.addEventListener("click", () => {
        const tool = { kind: entry.kind, ...(entry.type ? { type: entry.type } : {}), dir: 0, flip: false };
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
