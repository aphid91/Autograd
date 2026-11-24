// Presentation mode: `…/#b=<name>` opens web/presentations/<name>.json and
// the board it names (<name>.npz beside it) as a show, not an editor.
//
// The JSON (written by the editor's Presentation folder, see
// presentationRecord() below):
//   { version: 1, board: "<name>.npz",
//     rate,                        the ticks/s it starts at
//     rates: [..],                 up to 4 more for the speed panel (none: no panel)
//     startCamera, endCamera,      world boxes { cx, cy, hw, hh }, fitted to any screen
//     schedule: { ticks, lerpStart, lerpEnd, fadeIn, fadeOut },   ticks 0: no schedule
//     views: [{ name, view, colours, dev, layers, showLabels }, ...] }   1 or more; the first is the default
//
// The page shows only the board, the sound button, play/pause and reset
// (bottom middle), an "i" button cycling the views (bottom right), and the
// speed panel (bottom left). Nothing is written to the browser's storage
// (persist.js), and nothing on the board can be edited.
//
// main.js hands over a small interface (`api`) rather than this module
// reaching into its state.

import { icon } from "./icons.js";
import { pathBox, fitBox } from "./video-params.js";

export const PRESENT_DIR = "presentations";

// The presentation the URL asks for, or null: `#b=<name>`.
export function presentationName(hash = location.hash) {
  const m = /^#b=(.+)$/.exec(hash || "");
  if (!m) return null;
  try { return decodeURIComponent(m[1]).trim() || null; } catch { return null; }
}

// The presentation the editor is asked to open for editing, or null:
// `#edit=<name>` -- where a deployed presentation's hammer leads.
export function editName(hash = location.hash) {
  const m = /^#edit=(.+)$/.exec(hash || "");
  if (!m) return null;
  try { return decodeURIComponent(m[1]).trim() || null; } catch { return null; }
}

// A file name a presentation may have: letters, digits, space, - and _.
export const safeName = (s) => String(s || "").trim().replace(/[^\w -]+/g, "_").replace(/^[ _-]+|[ _-]+$/g, "");

// The JSON record from the editor's draft and the views it captured.
export function presentationRecord(name, draft, views) {
  const rates = (draft.rates || []).map(Number).filter(r => Number.isFinite(r) && r > 0).slice(0, 4);
  return {
    version: 1,
    board: `${name}.npz`,
    rate: Number(draft.rate) > 0 ? Number(draft.rate) : 30,
    rates,
    startCamera: draft.startCamera,
    endCamera: draft.endCamera || null,
    schedule: { ticks: Math.max(0, Math.round(Number(draft.ticks) || 0)),
                lerpStart: draft.lerpStart, lerpEnd: draft.lerpEnd, fadeIn: draft.fadeIn, fadeOut: draft.fadeOut },
    views,
  };
}

// The speed panel's rates: the starting one and the others, once each, in
// order. Empty when there are no others: no panel at all.
export function speedRates(rec) {
  const others = (rec.rates || []).filter(r => r > 0 && r !== rec.rate);
  if (!others.length) return [];
  return [...new Set([rec.rate, ...others])].sort((a, b) => a - b);
}

// A rate as its button shows it: 10000 as "10k", so five fit two rows.
export const rateLabel = (r) => (r >= 1000 && r % 100 === 0 ? `${r / 1000}k` : String(r));

// The camera schedule as a value: where it puts the camera at a tick, or
// null when there is none (no end camera, or 0 ticks).
export function schedulePose(rec, tick) {
  const s = rec.schedule;
  if (!rec.endCamera || !s || !(s.ticks > 0)) return null;
  return pathBox(rec.startCamera, rec.endCamera, s, tick / s.ticks);
}

const el = (tag, cls, html = "") => { const e = document.createElement(tag); if (cls) e.className = cls; e.innerHTML = html; return e; };

// The record and the board's bytes of a deployed presentation.
export async function fetchPresentation(name) {
  const res = await fetch(`${PRESENT_DIR}/${encodeURIComponent(name)}.json`);
  if (!res.ok) throw new Error(`no ${PRESENT_DIR}/${name}.json (HTTP ${res.status})`);
  const rec = await res.json();
  if (!rec || !Array.isArray(rec.views) || !rec.views.length) throw new Error("its JSON has no views");
  const boardFile = rec.board || `${name}.npz`;
  const bres = await fetch(`${PRESENT_DIR}/${encodeURIComponent(boardFile)}`);
  if (!bres.ok) throw new Error(`no ${PRESENT_DIR}/${boardFile} (HTTP ${bres.status})`);
  return { rec, bytes: await bres.arrayBuffer() };
}

// `opts.rec`: a record to show instead of fetching one -- the editor's
// preview, which shows the board already loaded (no `api.loadBoard`).
// `opts.onEdit`: what the hammer button (bottom right) does: back to the
// editor. Resolves once the page is built, before "tap to begin" is
// tapped; the returned presenter's `close()` takes every element it made
// back off the page (the preview's exit).
export async function startPresentation(name, api, opts = {}) {
  const fail = (msg) => { showMessage(`Could not open “${name}”: ${msg}`); };
  let rec = opts.rec;
  if (!rec) {
    try {
      const got = await fetchPresentation(name);
      rec = got.rec;
      await api.loadBoard(name, got.bytes);
    } catch (e) {
      return fail(e.message);
    }
  }

  // --- state -----------------------------------------------------------------
  let viewIndex = 0;
  let scheduled = !!schedulePose(rec, 0);   // aborted for good by a pan, a pinch or a rate change
  let placed = null;                        // where the schedule last put the camera
  let info = null;                          // the info panel, while open: { wasPlaying }

  api.applyView(rec.views[0]);
  api.setRate(rec.rate);
  api.setPlaying(false);
  const placeCamera = (box) => {
    const f = fitBox(box, api.cam.w, api.cam.h);
    api.cam.cx = f.cx; api.cam.cy = f.cy; api.cam.zoom = f.zoom;
    placed = { cx: f.cx, cy: f.cy, zoom: f.zoom };
  };
  if (rec.startCamera) placeCamera(rec.startCamera);
  api.redraw();

  // --- the controls ----------------------------------------------------------
  const stage = document.getElementById("stage");
  const ui = el("div", "pr-ui");
  const simBox = el("div", "pr-sim");
  const resetBtn = el("button", "pr-btn", icon("reset"));
  resetBtn.setAttribute("aria-label", "Reset");
  const playBtn = el("button", "pr-btn");
  const syncPlay = () => {
    playBtn.innerHTML = icon(api.isPlaying() ? "pause" : "play");
    playBtn.setAttribute("aria-label", api.isPlaying() ? "Pause" : "Play");
  };
  simBox.append(resetBtn, playBtn);
  const infoBtn = el("button", "pr-btn pr-i", "i");
  infoBtn.setAttribute("aria-label", "Change view");
  infoBtn.hidden = rec.views.length < 2;
  // The hammer: to the editor. Above the tap-to-begin cover, so a preview
  // can be left without starting it.
  const editBtn = el("button", "pr-btn pr-edit", icon("build"));
  editBtn.setAttribute("aria-label", "Edit");
  editBtn.hidden = !opts.onEdit;
  editBtn.onclick = () => opts.onEdit?.();
  ui.append(simBox, infoBtn);

  const rates = speedRates(rec);
  let speed = null;
  if (rates.length) {
    speed = el("div", "pr-speed", `<div class="pr-speed-label">speed</div><div class="pr-speed-row"></div>`);
    for (const r of rates) {
      const b = el("button", "pr-rate", rateLabel(r));
      b.dataset.rate = String(r);
      b.onclick = () => {
        if (api.rate() === r) return;
        api.setRate(r);
        scheduled = false;   // changing the rate ends the schedule
        syncSpeed();
      };
      speed.lastChild.append(b);
    }
    ui.append(speed);
  }
  const syncSpeed = () => { if (speed) for (const b of speed.querySelectorAll(".pr-rate")) b.dataset.selected = String(Number(b.dataset.rate) === api.rate()); };
  stage.append(ui);
  for (const b of ui.querySelectorAll("button")) b.addEventListener("click", () => b.blur());

  resetBtn.onclick = () => { api.reset(); if (scheduled) frame(); api.redraw(); };
  playBtn.onclick = () => { api.setPlaying(!api.isPlaying()); syncPlay(); };
  infoBtn.onclick = () => {
    viewIndex = (viewIndex + 1) % rec.views.length;
    api.applyView(rec.views[viewIndex]);
    infoBtn.dataset.view = String(viewIndex);
  };
  syncPlay(); syncSpeed();

  // --- tap to begin ------------------------------------------------------------
  // The cover takes the whole gesture -- a tap or a drag -- so the drag that
  // dismisses it neither pans nor ends the schedule. Its lift is also the
  // gesture a browser needs before sound may start. Not awaited: the
  // presenter exists (and can be closed) while the cover is up.
  let begun = false, closed = false;
  const cover = el("div", "pr-cover", `<div class="pr-cover-msg">tap to begin</div>`);
  document.body.append(cover);
  stage.append(editBtn);   // in the stage, beside the other buttons; z-index puts it over the cover
  cover.addEventListener("pointerdown", (e) => { e.preventDefault(); cover.setPointerCapture?.(e.pointerId); });
  cover.addEventListener("pointerup", () => {
    cover.remove();
    if (closed) return;
    begun = true;
    api.setPlaying(true);
    syncPlay();
    toast("drag to pan, pinch to zoom");
  }, { once: true });

  // --- the info panel ----------------------------------------------------------
  // A tap on a machine pauses and shows its name; any tap or drag anywhere
  // dismisses it -- and only dismisses it -- and plays on if it was playing.
  const openInfo = (name) => {
    const wasPlaying = api.isPlaying();
    if (wasPlaying) { api.setPlaying(false); syncPlay(); }
    const catcher = el("div", "pr-catch");
    const panel = el("div", "pr-info");
    const title = el("div", "pr-info-title");
    title.textContent = name;
    panel.append(title);
    catcher.append(panel);
    document.body.append(catcher);
    info = { wasPlaying, catcher };
    catcher.addEventListener("pointerdown", (e) => {
      e.preventDefault();
      catcher.setPointerCapture?.(e.pointerId);
      catcher.addEventListener("pointerup", () => {
        catcher.remove();
        if (info?.wasPlaying) { api.setPlaying(true); syncPlay(); }
        info = null;
      }, { once: true });
    });
  };

  // --- what main.js calls ------------------------------------------------------
  // Each frame: the schedule places the camera by the tick on screen -- so a
  // pause holds it and a slow device never drifts -- unless the camera is no
  // longer where it last put it, which means the player moved it (a drag,
  // a pinch, the wheel, a glide): then the schedule is over for good.
  function frame() {
    if (!scheduled || closed) return;
    const c = api.cam;
    if (placed && (Math.abs(c.cx - placed.cx) > 1e-6 || Math.abs(c.cy - placed.cy) > 1e-6 || Math.abs(c.zoom - placed.zoom) > 1e-9)) {
      scheduled = false;
      return;
    }
    placeCamera(schedulePose(rec, api.tick()));
  }
  return {
    frame,
    tap(entityName) { if (begun && !closed && entityName && !info) openInfo(entityName); },
    togglePlay() { if (begun && !closed) playBtn.click(); },
    // Everything this page put on the screen, taken off again.
    close() {
      closed = true;
      for (const n of [ui, cover, editBtn, info?.catcher, ...document.querySelectorAll(".pr-toast")]) n?.remove();
      info = null;
    },
    get scheduled() { return scheduled; },
    get viewIndex() { return viewIndex; },
    record: rec,
  };
}

function toast(text) {
  document.querySelectorAll(".pr-toast").forEach(t => t.remove());
  const t = el("div", "pr-toast");
  t.textContent = text;
  document.body.append(t);
  requestAnimationFrame(() => t.dataset.show = "true");
  setTimeout(() => { t.dataset.show = "false"; setTimeout(() => t.remove(), 600); }, 2600);
}

function showMessage(text) {
  const m = el("div", "pr-cover");
  const d = el("div", "pr-cover-msg pr-error");
  d.textContent = text;
  m.append(d);
  document.body.append(m);
}
