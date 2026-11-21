// The layer panel: a small floating list in the board's top-left corner,
// shown while View > layers is on. One row per layer -- colour swatch, name
// (tap to make it current), member count, visible and active toggles --
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

    this.body.append(this.list, add, acts, this.tint.wrap, this.label.wrap);
    this.root.append(head, this.body);
    host.append(this.root);
  }

  act(art, name, onTap) {
    const b = el("button", "b ly-act", { type: "button" });
    b.innerHTML = `${art}<span>${name}</span>`;
    b.addEventListener("click", () => { if (b.dataset.disabled !== "true") onTap(); });
    return b;
  }

  slider(name, min, max, step, onInput) {
    const wrap = el("label", "ly-slider");
    const span = el("span", "ly-sname");
    span.textContent = name;
    const input = el("input", "", { type: "range", min: String(min), max: String(max), step: String(step) });
    const out = el("span", "ly-sval");
    input.addEventListener("input", () => { out.textContent = fmt(Number(input.value)); onInput(Number(input.value)); });
    wrap.append(span, input, out);
    return { wrap, input, out };
  }

  show(on) { this.root.dataset.open = String(!!on); }

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
      name.title = r.current ? "The current layer" : "Make current";
      name.addEventListener("click", () => this.cb.onSelect(r.id));

      const count = el("span", "ly-count");
      count.textContent = String(r.count);

      // The current layer is always visible and active: its toggles are
      // shown on and cannot be turned off. Hidden implies inactive, so a
      // hidden layer's active toggle shows off and is disabled.
      const vis = this.toggle("ly-vis", r.visible ? "eye" : "eyeOff", r.visible, r.current,
                              r.visible ? "Hide" : "Show", () => this.cb.onVisible(r.id, !r.visible));
      const act = this.toggle("ly-lock", r.active ? "unlock" : "lock", r.active, r.current || !r.visible,
                              r.active ? "Make inactive" : "Make active", () => this.cb.onActive(r.id, !r.active));
      row.append(sw, name, count, vis, act);
      this.list.append(row);
    }
    this.list.scrollTop = keep;
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
