// The Tweakpane panel in the board's top-right corner: everything niche
// enough not to earn a button -- saves, view toggles, debug -- plus the Status
// folder a Hand tap opens.
//
// Like ui.js it only requests: every change goes out through a callback and
// main.js applies it to the board.

import { Pane } from "./vendor/tweakpane-4.0.5.min.js";
import { TUNE_DEFAULTS, TUNE_SPEC } from "./tune.js";
import { AUDIO_DEFAULTS, AUDIO_SPEC } from "./audio-params.js";
import { VIDEO_SPEC, CAMERA_SPEC, VIDEO_FPS, cleanVideo, frameCount } from "./video-params.js";
import { persistEnabled } from "./persist.js";

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
  if (!persistEnabled()) return;
  try { localStorage.setItem(PANE_KEY, JSON.stringify(p)); } catch { /* not persisted */ }
}

// The presentation draft as the panel stores it: text boxes for the rates
// (blank or 0: unused), the schedule's dials, and the captured cameras and
// detail view (plain data, or null).
export const PRESENT_DEFAULTS = {
  name: "presentation", rate: "30", rate1: "", rate2: "", rate3: "", rate4: "",
  ticks: 0, lerpStart: 0, lerpEnd: 1, fadeIn: 0.25, fadeOut: 0.25,
  startCamera: null, endCamera: null, detail: null,
};
function cleanPresent(p) {
  const out = { ...PRESENT_DEFAULTS };
  if (!p || typeof p !== "object") return out;
  for (const k of Object.keys(PRESENT_DEFAULTS)) {
    const def = PRESENT_DEFAULTS[k], v = p[k];
    if (def === null) { if (v && typeof v === "object") out[k] = v; }
    else if (typeof v === typeof def && (typeof v !== "number" || Number.isFinite(v))) out[k] = v;
  }
  return out;
}

export class Panel {
  constructor(host, cb) {
    this.cb = cb;
    this.prefs = { expanded: false, view: {}, ...loadPrefs() };
    this.pane = new Pane({ container: host, title: "Gates", expanded: !!this.prefs.expanded });
    // A folder's fold bubbles up to the pane: only the pane's own counts.
    this.paneOpen = !!this.prefs.expanded;
    this.pane.on("fold", (ev) => {
      if (ev.target && ev.target !== this.pane) return;
      this.paneOpen = ev.expanded;
      this.prefs.expanded = ev.expanded; savePrefs(this.prefs);
      cb.onPaneFold?.();
    });

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

    this.view = { grid: false, ports: true, labels: true, seedTint: true, notes: true, momentum: true, items: true, belts: true, gates: true, gateStatus: true, sources: true, beltStatus: true, beltActive: true, fps: false, pixelAlpha: 0, pixelMax: 8, layers: false, ...this.prefs.view };
    const view = this.pane.addFolder({ title: "View", expanded: false });
    this.viewBindings = [];
    for (const [key, label] of [["grid", "grid lines"], ["ports", "ports"], ["labels", "source values"], ["seedTint", "seed tint"], ["notes", "labels (notes)"], ["momentum", "pan momentum"], ["items", "items"], ["belts", "belts"], ["gates", "gates"], ["gateStatus", "gate status colours"], ["beltStatus", "belt status colours"], ["beltActive", "stalled but moving: green"], ["sources", "sources"], ["fps", "fps counter"], ["layers", "layers"]]) {
      this.viewBindings.push(view.addBinding(this.view, key, { label }).on("change", () => {
        if (this.applyingView) return;
        this.prefs.view = { ...this.view };
        savePrefs(this.prefs);
        cb.onView({ ...this.view });
      }));
    }
    // The pixel overlay (pixel.js): how opaque the Pixels' screen is (0 is
    // off, and a Pixel's fire then records nothing), and the largest square
    // a Pixel's Z may ask for, in cells each side of its centre.
    for (const [key, label, opts] of [["pixelAlpha", "pixel overlay", { min: 0, max: 1, step: 0.01 }],
                                      ["pixelMax", "pixel max size", { min: 1, max: 64, step: 1 }]]) {
      this.viewBindings.push(view.addBinding(this.view, key, { label, ...opts }).on("change", () => {
        if (this.applyingView) return;
        this.prefs.view = { ...this.view };
        savePrefs(this.prefs);
        cb.onView({ ...this.view });
      }));
    }

    // The status colours, each with a picker. Persisted with the panel.
    const known = Object.keys(STATUS_COLOUR_DEFAULTS);
    const stored = Object.fromEntries(Object.entries(this.prefs.colours || {}).filter(([k, v]) => known.includes(k) && /^#[0-9a-f]{6}$/i.test(v)));
    this.colours = { ...STATUS_COLOUR_DEFAULTS, ...stored };
    const colours = view.addFolder({ title: "status colours", expanded: false });
    const pushColours = () => {
      if (this.applyingView) return;
      this.prefs.colours = { ...this.colours };
      savePrefs(this.prefs);
      cb.onColours({ ...this.colours });
    };
    const colourBindings = this.colourBindings = STATUS_COLOUR_LABELS.map(([key, label]) =>
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
    this.audioMeter = { text: "off" };
    const audio = this.pane.addFolder({ title: "Audio", expanded: false });
    // Open (in an expanded pane), the board shows camera-aware audio's box.
    this.audioOpen = false;
    audio.on("fold", (ev) => { if (ev.target && ev.target !== audio) return; this.audioOpen = ev.expanded; cb.onAudioOpen?.(); });
    audio.addBinding(this.audioMeter, "text", { label: "meter", readonly: true, multiline: true, rows: 5, interval: 250 });
    const pushAudio = () => {
      this.prefs.audio = { ...this.audio };
      savePrefs(this.prefs);
      // A refresh from applyAudio() fires change too (Tweakpane re-snaps
      // to the step): a load, not the player, so not reported.
      if (!this.applyingAudio) cb.onAudio({ ...this.audio });
    };
    const audioBindings = this.audioBindings = [];
    let agroup = audio;
    for (const [key, label, opts] of AUDIO_SPEC) {
      if (key === "---") { agroup = audio.addFolder({ title: label, expanded: false }); continue; }
      audioBindings.push(agroup.addBinding(this.audio, key, { label, ...(opts || {}) }).on("change", pushAudio));
    }
    // The sound selection: the selected entities are the only ones heard
    // (nothing selected: everything). Kept after deselecting; reset by any
    // change to the board (main.js).
    this.audioSelBtn = audio.addButton({ title: "Set audio selection" });
    this.audioSelBtn.on("click", () => cb.onAudioSelection());
    // Or a region: the next drag on the board draws a box, and whatever
    // touches it sounds, as it is built and rebuilt.
    this.audioRegionBtn = audio.addButton({ title: "Set audio region" });
    this.audioRegionBtn.on("click", () => cb.onAudioRegion());
    audio.addButton({ title: "Customize scale…" }).on("click", () => cb.onScale());
    audio.addButton({ title: "Bake audio…" }).on("click", () => cb.onBake());
    audio.addButton({ title: "Reset audio dials" }).on("click", () => {
      Object.assign(this.audio, AUDIO_DEFAULTS);
      for (const b of audioBindings) b.refresh();
      pushAudio();
    });

    // Video: render a stretch of the simulation offline into an MP4
    // (video.js; main.js runs it). After Fluoddity's Recording tab. The
    // settings persist with the panel; the end camera is per session.
    this.video = cleanVideo(this.prefs.video);
    const vid = this.pane.addFolder({ title: "Video", expanded: false });
    // Open, the board shows the frame the video will cover.
    vid.on("fold", (ev) => { if (ev.target && ev.target !== vid) return; cb.onVideoOpen(ev.expanded); });
    const pushVideo = () => {
      this.prefs.video = { ...this.video };
      savePrefs(this.prefs);
      this.updateVideoText();
      cb.onVideo({ ...this.video });
    };
    this.videoBindings = [];
    for (const [key, label, opts] of VIDEO_SPEC) {
      this.videoBindings.push(vid.addBinding(this.video, key, { label, ...opts }).on("change", pushVideo));
    }
    const camF = vid.addFolder({ title: "camera", expanded: false });
    this.endCamBtn = camF.addButton({ title: "Select end camera state" });
    this.endCamBtn.on("click", () => cb.onVideoEndCamera(true));
    camF.addButton({ title: "Unset end camera" }).on("click", () => cb.onVideoEndCamera(false));
    for (const [key, label, opts] of CAMERA_SPEC) {
      this.videoBindings.push(camF.addBinding(this.video, key, { label, ...opts }).on("change", pushVideo));
    }
    this.videoText = { text: "" };
    this.videoStatus = "";
    vid.addBinding(this.videoText, "text", { label: "", readonly: true, multiline: true, rows: 2, interval: 250 });
    this.recordBtn = vid.addButton({ title: "Begin recording" });
    this.recordBtn.on("click", () => cb.onVideoRecord());
    this.updateVideoText();

    // Presentation: what a `#b=<name>` page shows (present.js). The draft --
    // the dials and the captured cameras and detail view -- persists with the
    // panel; Export bundles it with the board as it stands (main.js).
    this.present = cleanPresent(this.prefs.present);
    const pres = this.pane.addFolder({ title: "Presentation", expanded: false });
    const pushPresent = () => { this.prefs.present = { ...this.present }; savePrefs(this.prefs); this.updatePresentText(); };
    this.presentBindings = [];
    const pb = (folder, key, opts) => this.presentBindings.push(folder.addBinding(this.present, key, opts).on("change", pushPresent));
    pb(pres, "name", { label: "name" });
    pb(pres, "rate", { label: "start rate" });
    for (let i = 1; i <= 4; i++) pb(pres, `rate${i}`, { label: `rate ${i}` });
    const pcam = pres.addFolder({ title: "camera", expanded: false });
    pcam.addButton({ title: "Set start camera" }).on("click", () => cb.onPresent("start"));
    pcam.addButton({ title: "Set end camera" }).on("click", () => cb.onPresent("end"));
    pcam.addButton({ title: "Clear end camera" }).on("click", () => cb.onPresent("clearEnd"));
    pb(pcam, "ticks", { label: "ticks", min: 0, max: 1000000, step: 1 });
    for (const [key, label, opts] of CAMERA_SPEC) pb(pcam, key, { label, ...opts });
    pres.addButton({ title: "Set detail view" }).on("click", () => cb.onPresent("detail"));
    pres.addButton({ title: "Clear detail view" }).on("click", () => cb.onPresent("clearDetail"));
    this.presentText = { text: "" };
    pres.addBinding(this.presentText, "text", { label: "", readonly: true, multiline: true, rows: 3, interval: 250 });
    pres.addButton({ title: "Export presentation (.zip)" }).on("click", () => cb.onPresent("export"));
    this.updatePresentText();

    // Dev: the tuned-by-eye numbers and the force-build switch. Persisted
    // with the rest of the panel's preferences.
    this.dev = { ...TUNE_DEFAULTS, ...(this.prefs.dev || {}) };
    const dev = this.pane.addFolder({ title: "Dev", expanded: false });
    const pushDev = () => {
      if (this.applyingView) return;
      this.prefs.dev = { ...this.dev };
      savePrefs(this.prefs);
      cb.onTune({ ...this.dev });
    };
    const devBindings = this.devBindings = [];
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

    // The Hack assembler: a full-screen editor (assembler.js). Its programs
    // reach the board only through indexing's Hack… table.
    const asm = this.pane.addFolder({ title: "Assembler", expanded: false });
    asm.addButton({ title: "Open Hack editor" }).on("click", () => cb.onAssembler?.());

    const dbg = this.pane.addFolder({ title: "Debug", expanded: false });
    dbg.addButton({ title: "Clear stamps" }).on("click", () => cb.onClearStamps());
  }

  // --- view settings as one value -----------------------------------------------
  //
  // What a presentation's views hold: every View toggle and dial (the pixel
  // overlay included), the status colours and the Dev dials. Captured as
  // plain data; applied in one go, the bindings refreshed under a guard
  // (Tweakpane's refresh() fires change) and each callback told once.
  // Unknown keys are dropped and missing ones left as they are, so a view
  // written by an older or newer build applies what it can.
  captureView() {
    return { view: { ...this.view }, colours: { ...this.colours }, dev: { ...this.dev } };
  }

  applyView(s) {
    const take = (into, from) => {
      if (!from || typeof from !== "object") return;
      for (const [k, v] of Object.entries(from)) if (k in into && typeof v === typeof into[k]) into[k] = v;
    };
    take(this.view, s.view);
    take(this.colours, s.colours);
    take(this.dev, s.dev);
    this.applyingView = true;
    try { for (const b of [...this.viewBindings, ...this.colourBindings, ...this.devBindings]) b.refresh(); }
    finally { this.applyingView = false; }
    this.prefs.view = { ...this.view }; this.prefs.colours = { ...this.colours }; this.prefs.dev = { ...this.dev };
    savePrefs(this.prefs);   // a no-op in presentation mode (persist.js)
    this.cb.onView({ ...this.view });
    this.cb.onColours({ ...this.colours });
    this.cb.onTune({ ...this.dev });
  }

  // A save's dials (already cleaned: audio-params.js cleanDials) over the
  // ones in front of the player. Loading is not a change the player made, so
  // this does not call onAudio; main.js passes them on to the synth itself.
  applyAudio(dials) {
    Object.assign(this.audio, dials);
    this.applyingAudio = true;
    try { for (const b of this.audioBindings) b.refresh(); } finally { this.applyingAudio = false; }
    this.prefs.audio = { ...this.audio };
    savePrefs(this.prefs);
  }

  get audioVisible() { return this.paneOpen && this.audioOpen; }

  setAudioSelection(on) { this.audioSelBtn.title = on ? "Set audio selection ✓" : "Set audio selection"; }
  // null, "armed" (the next drag draws the box) or "set".
  setAudioRegion(state) {
    this.audioRegionBtn.title = state === "armed" ? "Set audio region: drag a box…"
      : state === "set" ? "Set audio region ✓" : "Set audio region";
  }

  // --- video ---------------------------------------------------------------

  updateVideoText() {
    const v = this.video, tpf = v.tps / VIDEO_FPS;
    this.videoText.text = `${frameCount(v)} frames · ${v.width}×${v.height} · ` +
      `${tpf >= 10 ? Math.round(tpf) : +tpf.toFixed(2)} ticks/frame` + (this.videoStatus ? `\n${this.videoStatus}` : "");
  }
  // The presentation draft changed from main.js (a camera or view captured).
  setPresent(patch) {
    Object.assign(this.present, patch);
    this.prefs.present = { ...this.present };
    savePrefs(this.prefs);
    for (const b of this.presentBindings) b.refresh();
    this.updatePresentText();
  }
  updatePresentText(extra = "") {
    const p = this.present;
    const box = (b) => (b ? `${b.cx.toFixed(1)},${b.cy.toFixed(1)} ±${b.hw.toFixed(1)}` : "–");
    this.presentText.text = `start ${p.startCamera ? box(p.startCamera) : "fit board"} · end ${box(p.endCamera)}\n` +
      `detail view: ${p.detail ? "set" : "–"}` + (extra ? `\n${extra}` : "");
  }
  setVideoProgress(text) { this.videoStatus = text; this.updateVideoText(); }
  setVideoRecording(on) { this.recordBtn.title = on ? "Cancel recording" : "Begin recording"; }
  setVideoEndCamera(set) { this.endCamBtn.title = set ? "Select end camera state ✓" : "Select end camera state"; }
  setVideoSize(w, h) {
    this.video.width = w; this.video.height = h;
    for (const b of this.videoBindings) b.refresh();
    this.prefs.video = { ...this.video };
    savePrefs(this.prefs);
    this.updateVideoText();
  }

  // The audio switch, when main.js could not honour it.

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
