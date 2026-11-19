// The Tweakpane panel in the board's top-right corner: everything niche
// enough not to earn a button -- saves, view toggles, debug -- plus the Status
// folder a Hand tap opens.
//
// Like ui.js it only requests: every change goes out through a callback and
// main.js applies it to the board.

import { Pane } from "./vendor/tweakpane-4.0.5.min.js";
import { TUNE_DEFAULTS, TUNE_SPEC } from "./tune.js";
import { AUDIO_DEFAULTS, AUDIO_SPEC } from "./audio-params.js";

const PANE_KEY = "gates.web.pane.v1";
const DIR_NAMES = ["+X east", "+Y north", "-X west", "-Y south"];

// The status colours' shipped values (View > status colours), as hex so
// Tweakpane's picker -- a saturation/value square and a hue strip -- edits
// them directly. main.js reads the live ones; a stored key this build does
// not know is ignored.
export const STATUS_COLOUR_DEFAULTS = {
  gateRunning: "#45a854", gateStarved: "#c7423d", gateBlocked: "#dbb82e",
  beltMoving: "#50a05c", beltEmpty: "#b04842", beltStalled: "#d6b43a",
};
const STATUS_COLOUR_LABELS = [
  ["gateRunning", "gate running"], ["gateStarved", "gate starved"], ["gateBlocked", "gate blocked"],
  ["beltMoving", "belt moving"], ["beltEmpty", "belt empty"], ["beltStalled", "belt stalled"],
];

function loadPrefs() {
  try { return JSON.parse(localStorage.getItem(PANE_KEY) || "null") || {}; } catch { return {}; }
}
function savePrefs(p) {
  try { localStorage.setItem(PANE_KEY, JSON.stringify(p)); } catch { /* not persisted */ }
}

export class Panel {
  constructor(host, cb) {
    this.cb = cb;
    this.prefs = { expanded: false, view: {}, ...loadPrefs() };
    this.pane = new Pane({ container: host, title: "Gates", expanded: !!this.prefs.expanded });
    this.pane.on("fold", (ev) => { this.prefs.expanded = ev.expanded; savePrefs(this.prefs); });

    // Status first, so an inspected entity is the top of the panel.
    this.status = this.pane.addFolder({ title: "Status", expanded: true });
    this.status.hidden = true;
    this.statusRow = -1;

    const board = this.pane.addFolder({ title: "Board", expanded: false });
    this.saveState = { save: "" };
    this.saveFolder = board;
    this.saveBinding = null;
    board.addButton({ title: "Open .npz…" }).on("click", () => cb.onOpenFile());
    board.addButton({ title: "Fit board" }).on("click", () => cb.onFit());

    // Browser saves: the board as it stands, kept in this browser under a
    // name. The list below picks one to load, download as an .npz, or
    // delete; picking alone does nothing, so choosing what to export never
    // replaces the board in front of you.
    board.addBlade({ view: "separator" });
    this.local = { name: "", saved: "" };
    this.nameBinding = board.addBinding(this.local, "name", { label: "name" });
    this.localSaveBtn = board.addButton({ title: "Save to browser" });
    this.localSaveBtn.on("click", () => cb.onSaveLocal(this.local.name.trim()));
    this.localBinding = null;
    this.localActions = [
      board.addButton({ title: "Load saved" }).on("click", () => this.local.saved && cb.onLoadLocal(this.local.saved)),
      board.addButton({ title: "Export .npz" }).on("click", () => this.local.saved && cb.onExportLocal(this.local.saved)),
      board.addButton({ title: "Delete saved" }).on("click", () => this.local.saved && cb.onDeleteLocal(this.local.saved)),
    ];

    this.view = { grid: true, ports: true, labels: true, seedTint: true, notes: true, momentum: true, items: true, belts: true, gates: true, gateStatus: false, sources: true, beltStatus: false, beltActive: true, fps: false, ...this.prefs.view };
    const view = this.pane.addFolder({ title: "View", expanded: false });
    for (const [key, label] of [["grid", "grid lines"], ["ports", "ports"], ["labels", "source values"], ["seedTint", "seed tint"], ["notes", "labels (notes)"], ["momentum", "pan momentum"], ["items", "items"], ["belts", "belts"], ["gates", "gates"], ["gateStatus", "gate status colours"], ["beltStatus", "belt status colours"], ["beltActive", "stalled but moving: green"], ["sources", "sources"], ["fps", "fps counter"]]) {
      view.addBinding(this.view, key, { label }).on("change", () => {
        this.prefs.view = { ...this.view };
        savePrefs(this.prefs);
        cb.onView({ ...this.view });
      });
    }

    // The status colours, each with a picker. Persisted with the panel.
    const known = Object.keys(STATUS_COLOUR_DEFAULTS);
    const stored = Object.fromEntries(Object.entries(this.prefs.colours || {}).filter(([k, v]) => known.includes(k) && /^#[0-9a-f]{6}$/i.test(v)));
    this.colours = { ...STATUS_COLOUR_DEFAULTS, ...stored };
    const colours = view.addFolder({ title: "status colours", expanded: false });
    const pushColours = () => {
      this.prefs.colours = { ...this.colours };
      savePrefs(this.prefs);
      cb.onColours({ ...this.colours });
    };
    const colourBindings = STATUS_COLOUR_LABELS.map(([key, label]) =>
      colours.addBinding(this.colours, key, { label, view: "color" }).on("change", pushColours));
    colours.addButton({ title: "Reset colours" }).on("click", () => {
      Object.assign(this.colours, STATUS_COLOUR_DEFAULTS);
      for (const b of colourBindings) b.refresh();
      pushColours();
    });

    // Audio: sound from the ticks (audio.js). The switch is off at every
    // launch -- a browser only starts audio from a tap -- and is not
    // persisted; the dials are. Turning it on is the only thing that loads
    // any audio code.
    this.audio = { ...AUDIO_DEFAULTS };
    for (const [k, v] of Object.entries(this.prefs.audio || {})) {
      if (k in AUDIO_DEFAULTS && typeof v === typeof AUDIO_DEFAULTS[k]) this.audio[k] = v;
    }
    this.audioOn = { on: false };
    this.audioMeter = { text: "off" };
    const audio = this.pane.addFolder({ title: "Audio", expanded: false });
    this.audioOnBinding = audio.addBinding(this.audioOn, "on", { label: "sound" })
      .on("change", (ev) => {
        // Hand the keyboard back, or the next Space (play) toggles this box.
        if (document.activeElement instanceof HTMLElement) document.activeElement.blur();
        cb.onAudioEnable(ev.value);
      });
    audio.addBinding(this.audioMeter, "text", { label: "meter", readonly: true, multiline: true, rows: 4, interval: 250 });
    const pushAudio = () => {
      this.prefs.audio = { ...this.audio };
      savePrefs(this.prefs);
      cb.onAudio({ ...this.audio });
    };
    const audioBindings = [];
    let agroup = audio;
    for (const [key, label, opts] of AUDIO_SPEC) {
      if (key === "---") { agroup = audio.addFolder({ title: label, expanded: false }); continue; }
      audioBindings.push(agroup.addBinding(this.audio, key, { label, ...(opts || {}) }).on("change", pushAudio));
    }
    audio.addButton({ title: "Reset audio dials" }).on("click", () => {
      Object.assign(this.audio, AUDIO_DEFAULTS);
      for (const b of audioBindings) b.refresh();
      pushAudio();
    });

    // Dev: the tuned-by-eye numbers and the force-build switch. Persisted
    // with the rest of the panel's preferences.
    this.dev = { ...TUNE_DEFAULTS, ...(this.prefs.dev || {}) };
    const dev = this.pane.addFolder({ title: "Dev", expanded: false });
    const pushDev = () => {
      this.prefs.dev = { ...this.dev };
      savePrefs(this.prefs);
      cb.onTune({ ...this.dev });
    };
    const devBindings = [];
    // A "---" entry is a divider with a heading.
    let group = dev;
    for (const [key, label, opts] of TUNE_SPEC) {
      if (key === "---") {
        dev.addBlade({ view: "separator" });
        group = dev.addFolder({ title: label, expanded: false });
        continue;
      }
      devBindings.push(group.addBinding(this.dev, key, { label, ...(opts || {}) }).on("change", pushDev));
    }
    dev.addButton({ title: "Reset dials" }).on("click", () => {
      Object.assign(this.dev, TUNE_DEFAULTS);
      for (const b of devBindings) b.refresh();
      pushDev();
    });

    const dbg = this.pane.addFolder({ title: "Debug", expanded: false });
    dbg.addButton({ title: "Clear stamps" }).on("click", () => cb.onClearStamps());
  }

  // The audio switch, when main.js could not honour it.
  setAudioOn(on) { this.audioOn.on = on; this.audioOnBinding.refresh(); }
  // Read by the meter's monitor binding on its own interval.
  setAudioMeter(text) { this.audioMeter.text = text; }

  // The bundled saves, as a dropdown at the top of the Board folder.
  setSaves(names, current) {
    if (this.saveBinding) this.saveBinding.dispose();
    this.saveBinding = null;
    if (!names.length) return;
    this.saveState.save = current || names[0];
    const options = Object.fromEntries(names.map(n => [n, n]));
    this.saveBinding = this.saveFolder.addBinding(this.saveState, "save", { label: "save", options, index: 0 });
    this.saveBinding.on("change", (ev) => this.cb.onLoadSave(ev.value));
  }

  // The live readout's text, from main.js. A gate shows each channel's value
  // ("–" empty, "×" an output with nothing to take it, so it discards);
  // a belt shows its run and the items on it, head first.
  setLive(text) {
    if (!this.liveBinding || text === null || text === this.live.text) return;
    this.live.text = text;
    this.liveBinding.refresh();
  }

  // The name a browser save will take: the loaded board's, until edited.
  setBoardName(name) {
    this.local.name = name;
    this.nameBinding.refresh();
  }

  // The browser saves, as a dropdown just under "Save to browser". Rebuilt
  // whenever the list changes; `selected` stays picked if it still exists.
  setLocalSaves(saves, selected = this.local.saved) {
    if (this.localBinding) this.localBinding.dispose();
    const names = saves.map(e => e.name);
    this.local.saved = names.includes(selected) ? selected : (names[0] ?? "");
    const options = names.length
      ? Object.fromEntries(saves.map(e => [`${e.name}  (${Math.ceil(e.bytes / 1024)} KB)`, e.name]))
      : { "(none yet)": "" };
    const at = this.saveFolder.children.indexOf(this.localSaveBtn) + 1;
    this.localBinding = this.saveFolder.addBinding(this.local, "saved", { label: "saved", options, index: at });
    for (const b of this.localActions) b.disabled = !names.length;
  }

  // Show `info` (from Board.describe) in the Status folder, or hide it.
  showStatus(info) {
    for (const child of [...this.status.children]) child.dispose();
    this.live = null;
    this.liveBinding = null;
    this.statusRow = info ? info.row : -1;
    this.status.hidden = !info;
    if (!info) return;

    const ro = {
      type: info.td.class,
      at: `${info.x}, ${info.y}`,
      facing: DIR_NAMES[info.dir],
      size: `${info.w} × ${info.h}`,
    };
    for (const k of Object.keys(ro)) this.status.addBinding(ro, k, { disabled: true });
    if (info.td.kind === "structure" && info.td.chiral) {
      this.status.addBinding({ flipped: info.flip }, "flipped", { disabled: true });
    }

    // The two per-instance settings. Setting the value already held is not
    // a change: a number field reports per keystroke.
    if (info.emit !== null) {
      const o = { emit: info.emit };
      this.status.addBinding(o, "emit", { label: "emits" }).on("change", (ev) => {
        if (ev.value !== info.emit) { info.emit = ev.value; this.cb.onEmit(info.row, ev.value); }
      });
    }
    if (info.seedOn !== null) {
      const o = { seed: info.seedOn, value: info.seedValue };
      const push = () => {
        if (o.seed === info.seedOn && o.value === info.seedValue) return;
        info.seedOn = o.seed; info.seedValue = o.value;
        this.cb.onSeed(info.row, o.seed, o.value);
      };
      this.status.addBinding(o, "seed", { label: "seed" }).on("change", push);
      this.status.addBinding(o, "value", { label: "seed value" }).on("change", push);
    }
    // The live readout: a gate's channels or a belt's run, rewritten by
    // main.js every frame while the simulation runs (see setLive). A manual
    // monitor (interval 0), refreshed only when the text actually changes.
    if (info.td.kind === "structure" || info.td.kind === "belt" || info.td.kind === "under") {
      this.live = { text: "" };
      this.liveBinding = this.status.addBinding(this.live, "text", {
        label: info.td.kind === "structure" ? "channels" : "run",
        readonly: true, multiline: true, rows: 3, interval: 0,
      });
    } else {
      this.live = null;
      this.liveBinding = null;
    }
    this.status.addButton({ title: "Close" }).on("click", () => this.cb.onCloseStatus());

    // The panel is NOT opened for the player: a Hand tap is often just to
    // reach the number swatches, and the player opens Status when wanted.
  }
}
