// The layer panel: a small floating list in the board's top-left corner,
// shown while View > layers is on. One row per layer -- colour swatch, name
// (tap to make it current, double tap to select all of it, long press to
// isolate it), member count, the
// visible toggle (an eye) and the active one (a star, filled when active) --
// then a + row, the commands (Rename, Delete, Adopt selection) and the two
// sliders (tint, label size), which set every active layer at once.
//
// Like ui.js and panel.js it only requests: every change goes out through a
// callback and main.js applies it. `update(model)` redraws the list only
// when what it shows changed, so it is cheap to call every frame -- and a
// rebuild never happens under a finger: the sliders are built once, and a
// colour is reported on `change` (the picker closing), not per drag step.

import { icon } from "./icons.js";
import { LABEL_MAX } from "./layers.js";

const HOLD_MS = 500, HOLD_SLOP = 10;   // a long press, as the shell's
const DOUBLE_TAP_MS = 350;
const MIN_W = 180, MIN_LIST_H = 28;     // CSS px: a few letters of a name; about one row
const SIZE_KEY = "gates.web.layers.size.v1";
function loadSize() {
  try { const v = JSON.parse(localStorage.getItem(SIZE_KEY) || "null"); return v && typeof v === "object" ? v : null; } catch { return null; }
}
function saveSize(v) {
  try { localStorage.setItem(SIZE_KEY, JSON.stringify({ w: v.w, listH: v.listH })); } catch { /* not kept */ }
}

const el = (tag, cls, attrs = {}) => {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
  return e;
};

export class LayerPanel {
  constructor(host, cb) {
    this.cb = cb;
    this.sig = null;
    this.root = el("div", "layerpanel", { "data-open": "false", role: "region", "aria-label": "Layers" });

    const head = el("button", "ly-head", { type: "button", "aria-expanded": "true" });
    head.innerHTML = `${icon("layers")}<span>Layers</span><span class="ly-fold"></span>`;
    head.addEventListener("click", () => {
      const folded = this.root.dataset.folded !== "true";
      this.root.dataset.folded = String(folded);
      head.setAttribute("aria-expanded", String(!folded));
    });

    this.body = el("div", "ly-body");
    this.list = el("div", "ly-list", { role: "list" });
    const add = el("button", "ly-add", { type: "button", "aria-label": "Add layer", title: "Add layer" });
    add.innerHTML = icon("plus");
    add.addEventListener("click", () => cb.onAdd());

    const acts = el("div", "ly-acts");
    this.renameBtn = this.act(icon("pencil"), "Rename", () => cb.onRename());
    this.deleteBtn = this.act(icon("trash"), "Delete", () => cb.onDelete());
    this.adoptBtn = this.act(icon("check"), "Adopt", () => cb.onAdopt());
    this.adoptBtn.title = "Put the selected entities on the current layer";
    acts.append(this.renameBtn, this.deleteBtn, this.adoptBtn);

    this.tint = this.slider("Tint", 0, 1, 0.05, (v) => cb.onTint(v));
    this.label = this.slider("Label", 0, LABEL_MAX, 0.5, (v) => cb.onLabel(v));
    // A long press on the word "Label" switches every layer label off or on;
    // off, the slider is dimmed and disabled (each layer keeps its size).
    const lname = this.label.wrap.querySelector(".ly-sname");
    lname.title = "Hold to show or hide layer labels";
    this.wirePress(lname, () => {}, () => cb.onLabelsToggle());

    this.body.append(this.list, add, acts, this.tint.wrap, this.label.wrap);
    // The resize grip, bottom-right: across sets the panel's width (the
    // names take it up; the buttons and sliders stretch), down sets how tall
    // the list is before it scrolls. Kept across sessions.
    this.grip = el("div", "ly-grip", { role: "separator", "aria-label": "Resize layers panel", title: "Drag to resize" });
    this.wireGrip(this.grip);
    this.root.append(head, this.body, this.grip);
    host.append(this.root);
    this.applySize(loadSize());
  }

  act(art, name, onTap) {
    const b = el("button", "b ly-act", { type: "button" });
    b.innerHTML = `${art}<span>${name}</span>`;
    b.addEventListener("click", () => { if (b.dataset.disabled !== "true") onTap(); });
    return b;
  }

  slider(name, min, max, step, onInput) {
    const wrap = el("div", "ly-slider");
    const span = el("span", "ly-sname");
    span.textContent = name;
    const input = el("input", "", { type: "range", min: String(min), max: String(max), step: String(step) });
    const out = el("span", "ly-sval");
    input.addEventListener("input", () => { out.textContent = fmt(Number(input.value)); onInput(Number(input.value)); });
    wrap.append(span, input, out);
    return { wrap, input, out };
  }

  show(on) { this.root.dataset.open = String(!!on); }

  // { w, listH } in CSS px, either absent for the stylesheet's default.
  // Clamped to the board, so a size saved on a larger screen still fits.
  applySize(size) {
    this.size = size || {};
    const host = this.root.parentElement?.getBoundingClientRect();
    if (Number.isFinite(this.size.w)) {
      const max = host ? host.width - 12 : Infinity;
      this.root.style.width = `${Math.round(Math.max(MIN_W, Math.min(max, this.size.w)))}px`;
    }
    if (Number.isFinite(this.size.listH)) {
      this.list.style.maxHeight = `${Math.round(Math.max(MIN_LIST_H, this.size.listH))}px`;
    }
  }

  // The grip follows the finger's motion from where it was pressed: width
  // from the panel's width then, the list's height from its height then (its
  // real height, not its limit, so a short list shrinks at once rather than
  // only after the drag passes its unused allowance).
  wireGrip(g) {
    let drag = null;
    g.addEventListener("pointerdown", (e) => {
      e.preventDefault();
      e.stopPropagation();
      g.setPointerCapture(e.pointerId);
      drag = { id: e.pointerId, x0: e.clientX, y0: e.clientY,
               w0: this.root.getBoundingClientRect().width, h0: this.list.clientHeight };
      this.root.dataset.resizing = "true";
    });
    g.addEventListener("pointermove", (e) => {
      if (!drag || e.pointerId !== drag.id) return;
      this.applySize({ w: drag.w0 + e.clientX - drag.x0, listH: drag.h0 + e.clientY - drag.y0 });
    });
    const end = (e) => {
      if (!drag || e.pointerId !== drag.id) return;
      drag = null;
      this.root.dataset.resizing = "false";
      saveSize(this.size);
    };
    g.addEventListener("pointerup", end);
    g.addEventListener("pointercancel", end);
  }

  // model: { rows: [{ id, name, colour, visible, active, current, count }],
  //          tint, label, selCount, canEdit }
  update(m) {
    const sig = JSON.stringify(m);
    if (sig === this.sig) return;
    this.sig = sig;
    this.renderList(m);
    this.renameBtn.dataset.disabled = String(!m.canEdit);
    this.deleteBtn.dataset.disabled = String(!m.canEdit);
    this.adoptBtn.dataset.disabled = String(!m.selCount);
    this.adoptBtn.querySelector("span").textContent = m.selCount ? `Adopt ${m.selCount}` : "Adopt";
    this.label.wrap.dataset.off = String(!m.labelsOn);
    this.label.input.disabled = !m.labelsOn;
    // A slider under the finger is left alone: its own input is the truth.
    for (const [s, v] of [[this.tint, m.tint], [this.label, m.label]]) {
      if (document.activeElement !== s.input) { s.input.value = String(v); s.out.textContent = fmt(v); }
    }
  }

  renderList(m) {
    const keep = this.list.scrollTop;
    this.list.replaceChildren();
    for (const r of m.rows) {
      const row = el("div", "ly-row", { role: "listitem" });
      row.dataset.current = String(r.current);
      row.dataset.hidden = String(!r.visible);

      // The colour: a native picker over a swatch. Background has none.
      const sw = el("span", "ly-swatch");
      if (r.colour) {
        sw.style.background = r.colour;
        const pick = el("input", "", { type: "color", "aria-label": `${r.name} colour` });
        pick.value = r.colour;
        pick.addEventListener("change", () => this.cb.onColour(r.id, pick.value));
        sw.append(pick);
      } else sw.dataset.none = "true";

      const name = el("button", "ly-name", { type: "button" });
      name.textContent = r.name;
      name.title = `${r.current ? "The current layer" : "Make current"}; double tap to select it all; hold to isolate`;
      // A second tap on the same layer soon after the first selects all of
      // it. Kept on the panel, not the button: the first tap makes the layer
      // current, which rebuilds the row, so the second lands on a new one.
      this.wirePress(name, () => {
        const now = performance.now(), last = this.lastTap;
        this.lastTap = { id: r.id, t: now };
        if (last && last.id === r.id && now - last.t < DOUBLE_TAP_MS) {
          this.lastTap = null;
          this.cb.onSelectAll(r.id);
        } else this.cb.onSelect(r.id);
      }, () => { this.lastTap = null; this.cb.onIsolate(r.id); });

      const count = el("span", "ly-count");
      count.textContent = String(r.count);

      // The current layer is always visible and active: its toggles are
      // shown on and cannot be turned off. Hidden implies inactive, so a
      // hidden layer's active toggle shows off and is disabled.
      const vis = this.toggle("ly-vis", r.visible ? "eye" : "eyeOff", r.visible, r.current,
                              r.visible ? "Hide" : "Show", () => this.cb.onVisible(r.id, !r.visible));
      const act = this.toggle("ly-star", "star", r.active, r.current || !r.visible,
                              r.active ? "Make inactive" : "Make active", () => this.cb.onActive(r.id, !r.active));
      row.append(sw, name, count, vis, act);
      this.list.append(row);
    }
    this.list.scrollTop = keep;
  }

  // A tap, or a long press (right-click on a mouse) -- which then swallows
  // the click its own release makes, so a hold never also selects.
  wirePress(b, onTap, onHold) {
    let timer = null, held = false, x0 = 0, y0 = 0;
    const cancel = () => { if (timer !== null) clearTimeout(timer); timer = null; };
    b.addEventListener("pointerdown", (e) => {
      held = false;
      if (e.pointerType === "mouse") return;
      x0 = e.clientX; y0 = e.clientY;
      cancel();
      timer = setTimeout(() => { timer = null; held = true; onHold(); }, HOLD_MS);
    });
    b.addEventListener("pointermove", (e) => {
      if (timer !== null && Math.hypot(e.clientX - x0, e.clientY - y0) > HOLD_SLOP) cancel();
    });
    for (const ev of ["pointerup", "pointercancel", "pointerleave"]) b.addEventListener(ev, cancel);
    b.addEventListener("contextmenu", (e) => { e.preventDefault(); if (!held) onHold(); held = true; });
    b.addEventListener("click", () => { if (held) { held = false; return; } onTap(); });
  }

  toggle(cls, art, on, disabled, label, onTap) {
    const b = el("button", `ly-tog ${cls}`, { type: "button", "aria-label": label, title: label, "aria-pressed": String(on) });
    b.innerHTML = icon(art);
    b.dataset.on = String(on);
    if (disabled) b.disabled = true;
    b.addEventListener("click", onTap);
    return b;
  }
}

function fmt(v) { return Number.isInteger(v) ? String(v) : v.toFixed(v < 1 ? 2 : 1); }
