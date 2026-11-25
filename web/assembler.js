// The assembler: a full-screen editor for Hack assembly, opened from the
// panel's Assembler folder. While it is open it covers the app (main.js
// pauses the simulation and gives play back on close).
//
// The editor is a plain <textarea> with a highlighted copy of its text
// underneath (transparent text, visible caret), scrolled in step. Every edit
// re-assembles (programs are small), so errors show as you type: the line
// tinted, its number red, and the message in the status strip -- tap it to
// jump there. The Binary view lists the words, in binary or decimal.
//
// Programs save to the browser (hackstore.js). The working text is also kept
// as a draft on every keystroke, so closing the editor or the app loses
// nothing; anything that would replace unsaved text asks first.

import { assemble, preprocess, stripComment, toBinary, asciiCodes } from "./hack.js";
import { listPrograms, getProgram, putProgram, deleteProgram, getDraft, putDraft } from "./hackstore.js";
import { toast } from "./toast.js";

let root = null;      // the overlay, built on first open
let ui = null;        // its parts
let state = null;     // { name, text, base, view: "code" | "bin", decimal }
let onCloseCb = null;

const esc = (s) => s.replace(/[&<>]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
const span = (cls, s) => (s ? `<span class="${cls}">${esc(s)}</span>` : "");

export function isAssemblerOpen() { return !!root && root.dataset.open === "true"; }

export function openAssembler({ onClose } = {}) {
  onCloseCb = onClose || null;
  if (!root) build();
  const d = getDraft();
  state = d
    ? { name: d.name ?? "", text: d.text, base: d.base ?? null, loadedName: d.loadedName ?? null, view: "code", decimal: !!d.decimal }
    : { name: "", text: "", base: "", loadedName: null, view: "code", decimal: false };
  ui.text.value = state.text;
  ui.name.value = state.name;
  refreshList();
  root.dataset.open = "true";
  update();
}

function close() {
  root.dataset.open = "false";
  ui.text.blur();
  ui.name.blur();
  onCloseCb?.();
}

const dirty = () => state.text !== state.base;
const keep = () => putDraft({ name: state.name, text: state.text, base: state.base, loadedName: state.loadedName, decimal: state.decimal });
const askDiscard = (what) => !dirty() || confirm(`The program has unsaved changes. ${what} anyway and lose them?`);

// --- the DOM -------------------------------------------------------------------

function build() {
  root = document.createElement("div");
  root.id = "asm";
  root.className = "asm";
  root.dataset.open = "false";
  root.setAttribute("role", "dialog");
  root.setAttribute("aria-modal", "true");
  root.setAttribute("aria-label", "Hack assembler");
  root.innerHTML = `
    <div class="asm-bar">
      <button type="button" class="b asm-btn" data-act="close">Close</button>
      <input class="asm-name" type="text" maxlength="40" placeholder="program name" autocomplete="off" spellcheck="false" aria-label="Program name">
      <button type="button" class="b asm-btn asm-save" data-act="save">Save</button>
    </div>
    <div class="asm-bar">
      <select class="asm-list" aria-label="Saved programs"></select>
      <button type="button" class="b asm-btn" data-act="load">Load</button>
      <button type="button" class="b asm-btn danger" data-act="delete">Delete</button>
      <button type="button" class="b asm-btn" data-act="new">New</button>
    </div>
    <div class="asm-bar">
      <button type="button" class="b asm-btn" data-act="import">Import .asm</button>
      <button type="button" class="b asm-btn" data-act="export">Export .asm</button>
      <span class="asm-spacer"></span>
      <button type="button" class="b asm-btn" data-act="view">Binary</button>
      <label class="asm-check"><input type="checkbox" class="asm-dec"> decimal</label>
    </div>
    <div class="asm-body">
      <div class="asm-edit">
        <div class="asm-gutter"><div class="asm-gutter-in"></div></div>
        <div class="asm-code">
          <div class="asm-hl"></div>
          <textarea class="asm-text" wrap="off" autocomplete="off" autocorrect="off" autocapitalize="off" spellcheck="false" aria-label="Hack assembly"></textarea>
        </div>
      </div>
      <div class="asm-bin" hidden></div>
    </div>
    <div class="asm-status"></div>
    <input type="file" class="asm-file" accept=".asm,.hack,.txt,text/plain" hidden>`;
  document.body.append(root);
  const q = (s) => root.querySelector(s);
  ui = {
    name: q(".asm-name"), list: q(".asm-list"), text: q(".asm-text"), hl: q(".asm-hl"),
    gutter: q(".asm-gutter-in"), edit: q(".asm-edit"), bin: q(".asm-bin"), status: q(".asm-status"),
    file: q(".asm-file"), dec: q(".asm-dec"), viewBtn: q('[data-act="view"]'), saveBtn: q(".asm-save"),
  };

  ui.text.addEventListener("input", () => { state.text = ui.text.value; keep(); update(); });
  ui.text.addEventListener("scroll", syncScroll);
  ui.text.addEventListener("keydown", (e) => {
    if (e.key === "Tab" && !e.ctrlKey && !e.metaKey && !e.altKey) {
      e.preventDefault();
      document.execCommand("insertText", false, "  ");   // keeps the textarea's own undo
    }
  });
  ui.name.addEventListener("input", () => { state.name = ui.name.value; keep(); update(); });
  ui.dec.addEventListener("change", () => { state.decimal = ui.dec.checked; keep(); update(); });
  ui.file.addEventListener("change", importFile);
  for (const b of root.querySelectorAll("[data-act]")) b.addEventListener("click", () => ACTIONS[b.dataset.act]());
  ui.status.addEventListener("click", (e) => {
    const line = Number(e.target.closest("[data-line]")?.dataset.line);
    if (line) gotoLine(line);
  });
  ui.bin.addEventListener("click", (e) => {
    const line = Number(e.target.closest("[data-line]")?.dataset.line);
    if (line) { state.view = "code"; update(); gotoLine(line); }
  });
}

function refreshList(selected) {
  const progs = listPrograms();
  const pick = selected ?? (progs.some(p => p.name === state.name) ? state.name : progs[0]?.name);
  ui.list.replaceChildren(...(progs.length ? progs : [{ name: "" }]).map(p => {
    const o = document.createElement("option");
    o.value = p.name;
    o.textContent = p.name || "(no saved programs)";
    o.selected = p.name === pick;
    return o;
  }));
  ui.list.disabled = !progs.length;
  for (const act of ["load", "delete"]) root.querySelector(`[data-act="${act}"]`).disabled = !progs.length;
}

// --- actions -------------------------------------------------------------------

function setText(text, name, base) {
  state.text = text;
  state.name = name;
  state.base = base;
  ui.text.value = text;
  ui.name.value = name;
  ui.text.scrollTop = ui.text.scrollLeft = 0;
  keep();
  update();
}

const ACTIONS = {
  close,
  save() {
    const name = state.name.trim();
    if (!name) { toast("Give the program a name first."); ui.name.focus(); return; }
    const exists = getProgram(name) !== null;
    // Saving the program you loaded is not a question; writing over another is.
    if (exists && name !== state.loadedName && !confirm(`Replace the saved program "${name}"?`)) return;
    try { putProgram(name, state.text); } catch (e) { alert(e.message); return; }
    state.name = name;
    state.loadedName = name;
    ui.name.value = name;
    state.base = state.text;
    keep();
    refreshList(name);
    const n = assemble(state.text).errors.length;
    toast(n ? `Saved "${name}" (${n} error${n === 1 ? "" : "s"}: it cannot be used until fixed).` : `Saved "${name}".`);
    update();
  },
  load() {
    const name = ui.list.value;
    const text = getProgram(name);
    if (text === null) { toast(`"${name}" is not saved any more.`); refreshList(); return; }
    if (!askDiscard(`Load "${name}"`)) return;
    state.loadedName = name;
    setText(text, name, text);
  },
  delete() {
    const name = ui.list.value;
    if (!name || !confirm(`Delete the saved program "${name}"? This cannot be undone.`)) return;
    deleteProgram(name);
    // The text stays in the editor, now unsaved.
    if (state.loadedName === name) { state.loadedName = null; state.base = null; keep(); }
    refreshList();
    toast(`Deleted "${name}".`);
    update();
  },
  new() {
    if (!askDiscard("Start a new program")) return;
    state.loadedName = null;
    setText("", "", "");
  },
  import() {
    if (!askDiscard("Import a file")) return;
    ui.file.click();
  },
  export() {
    const name = (state.name.trim() || "program").replace(/\.(asm|hack|txt)$/i, "");
    const url = URL.createObjectURL(new Blob([state.text], { type: "text/plain" }));
    const a = document.createElement("a");
    a.href = url;
    a.download = `${name}.asm`;
    document.body.append(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 10000);
  },
  view() {
    state.view = state.view === "code" ? "bin" : "code";
    update();
  },
};

async function importFile() {
  const f = ui.file.files[0];
  ui.file.value = "";
  if (!f) return;
  const text = await f.text();
  state.loadedName = null;
  // Imported text is not in the browser yet, so it counts as unsaved.
  setText(text, f.name.replace(/\.(asm|hack|txt)$/i, ""), null);
}

function gotoLine(line) {
  const lines = ui.text.value.split("\n");
  const at = lines.slice(0, line - 1).reduce((n, l) => n + l.length + 1, 0);
  ui.text.focus();
  ui.text.setSelectionRange(at, at + (lines[line - 1] || "").length);
  const lh = parseFloat(getComputedStyle(ui.text).lineHeight) || 22;
  ui.text.scrollTop = Math.max(0, (line - 3) * lh);
  syncScroll();
}

// --- drawing -------------------------------------------------------------------

function syncScroll() {
  const { scrollTop: t, scrollLeft: l } = ui.text;
  ui.hl.style.transform = `translate(${-l}px, ${-t}px)`;
  ui.gutter.style.transform = `translateY(${-t}px)`;
}

// One line of source as highlighted HTML. Lexical only -- what the line looks
// like as typed -- except that a line using a #define name shows those names
// as macros, since its real meaning appears only after replacement.
function highlight(line, defines) {
  if (/^\s*#/.test(line)) {
    const code = stripComment(line);
    const m = /^(\s*)(#\S*)(\s*)(\S*)(.*)$/.exec(code);
    const head = m ? span("pp", m[2]) + esc(m[3]) + span("mac", m[4]) + esc(m[5]) : esc(code);
    return esc(m ? m[1] : "") + head + span("cm", line.slice(code.length));
  }
  const code = stripComment(line);
  const comment = line.slice(code.length);
  let html;
  const names = defines.map(d => d.name).filter(n => code.includes(n));
  if (names.length) {
    // Split on the macro names, longest first so a name inside another loses.
    const re = new RegExp(names.sort((a, b) => b.length - a.length).map(n => n.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|"), "g");
    html = "";
    let k = 0;
    for (const m of code.matchAll(re)) {
      html += esc(code.slice(k, m.index)) + span("mac", m[0]);
      k = m.index + m[0].length;
    }
    html += esc(code.slice(k));
  } else {
    const t = code.trim();
    const lead = code.slice(0, code.length - code.trimStart().length);
    const trail = code.slice(lead.length + t.length);
    if (!t) html = esc(code);
    else if (t[0] === "(") html = esc(lead) + span("lb", t) + esc(trail);
    else if (t[0] === "@") {
      const v = t.slice(1);
      html = esc(lead) + span("at", "@") + span(/^\s*\d+\s*$/.test(v) ? "num" : "sym", v) + esc(trail);
    } else {
      // dest=comp;jump
      const m = /^([^=;]*=)?([^;]*)(;.*)?$/.exec(t);
      const dest = m[1] ? span("dst", m[1].slice(0, -1)) + span("op", "=") : "";
      const jump = m[3] ? span("op", ";") + span("jmp", m[3].slice(1)) : "";
      html = esc(lead) + dest + span("cmp", m[2]) + jump + esc(trail);
    }
  }
  return html + span("cm", comment);
}

function update() {
  const res = assemble(state.text);
  const { defines } = preprocess(state.text);
  const bad = new Map();
  for (const e of res.errors) if (!bad.has(e.line)) bad.set(e.line, e.msg);
  // ROM address of each instruction's line, for the gutter.
  const addr = new Map(res.lineOf.map((l, i) => [l, i]));

  const lines = state.text.split("\n");
  ui.hl.innerHTML = lines.map((l, i) =>
    `<div class="asm-line${bad.has(i + 1) ? " err" : ""}">${highlight(l, defines) || " "}</div>`).join("");
  ui.gutter.innerHTML = lines.map((_, i) =>
    `<div class="asm-ln${bad.has(i + 1) ? " err" : ""}"${bad.has(i + 1) ? ` title="${esc(bad.get(i + 1))}"` : ""}>` +
    `<span class="asm-num">${i + 1}</span><span class="asm-addr">${addr.has(i + 1) ? addr.get(i + 1) : ""}</span></div>`).join("");
  syncScroll();

  // Status: the errors, or a summary. Both lead with the character count,
  // which is what indexing's ASCII option needs -- a text being streamed as
  // characters need not assemble at all.
  const nc = asciiCodes(state.text).length;
  const chars = `${nc} character${nc === 1 ? "" : "s"}`;
  if (res.errors.length) {
    ui.status.dataset.error = "true";
    ui.status.innerHTML = `<div class="asm-chars">${chars}</div>` + res.errors.slice(0, 50).map(e =>
      `<div class="asm-err" data-line="${e.line}">line ${e.line}: ${esc(e.msg)}</div>`).join("") +
      (res.errors.length > 50 ? `<div>… and ${res.errors.length - 50} more</div>` : "");
  } else {
    ui.status.dataset.error = "false";
    const n = res.words.length;
    ui.status.textContent = `${n} instruction${n === 1 ? "" : "s"} · ${chars}` +
      (defines.length ? `, ${defines.length} define${defines.length === 1 ? "" : "s"}` : "") +
      (dirty() ? " · unsaved" : state.loadedName ? " · saved" : "");
  }
  ui.saveBtn.dataset.dirty = String(dirty());

  // The views.
  const bin = state.view === "bin";
  ui.edit.hidden = bin;
  ui.bin.hidden = !bin;
  ui.viewBtn.textContent = bin ? "Code" : "Binary";
  ui.dec.checked = state.decimal;
  if (bin) {
    if (res.errors.length) {
      ui.bin.innerHTML = `<div class="asm-bin-msg">Fix the errors to see the machine code.</div>`;
    } else if (!res.words.length) {
      ui.bin.innerHTML = `<div class="asm-bin-msg">No instructions yet.</div>`;
    } else {
      ui.bin.innerHTML = res.words.map((w, i) => {
        const src = stripComment(lines[res.lineOf[i] - 1] || "").trim();
        const val = state.decimal ? String(w) : `${toBinary(w).slice(0, 4)} ${toBinary(w).slice(4, 8)} ${toBinary(w).slice(8, 12)} ${toBinary(w).slice(12)}`;
        return `<div class="asm-row" data-line="${res.lineOf[i]}"><span class="asm-addr">${i}</span>` +
          `<span class="asm-word${w & 0x8000 ? " c" : ""}">${val}</span><span class="asm-src">${esc(src)}</span></div>`;
      }).join("");
    }
  }
}
