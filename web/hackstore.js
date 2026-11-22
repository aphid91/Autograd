// Hack programs saved in the browser: { name -> { text, savedAt } } under one
// localStorage key (programs are a few KB of text), plus the editor's
// unsaved draft under another, so closing the editor or the app never loses
// typing. Every access is guarded: storage can be absent, full, or throw.

const KEY = "gates.web.hack.v1";
const DRAFT_KEY = "gates.web.hack.draft.v1";

function read() {
  try {
    const v = JSON.parse(localStorage.getItem(KEY) || "{}");
    return v && typeof v === "object" ? v : {};
  } catch { return {}; }
}

// Every saved program, most recently saved first: [{ name, savedAt, size }].
export function listPrograms() {
  return Object.entries(read())
    .filter(([, p]) => p && typeof p.text === "string")
    .map(([name, p]) => ({ name, savedAt: p.savedAt || 0, size: p.text.length }))
    .sort((a, b) => b.savedAt - a.savedAt);
}

export function getProgram(name) {
  const p = read()[name];
  return p && typeof p.text === "string" ? p.text : null;
}

// Throws with a readable message when the browser refuses.
export function putProgram(name, text) {
  const all = read();
  all[name] = { text, savedAt: Date.now() };
  try { localStorage.setItem(KEY, JSON.stringify(all)); } catch (e) {
    throw new Error(`The browser refused to save "${name}" (${e.name || "storage error"}).`);
  }
}

export function deleteProgram(name) {
  const all = read();
  delete all[name];
  try { localStorage.setItem(KEY, JSON.stringify(all)); } catch { /* nothing more to do */ }
}

// The editor's working state: { name, text, base, loadedName, decimal } -- `base` is the text as
// last saved or loaded, so "unsaved changes" survives a reload too.
export function getDraft() {
  try {
    const v = JSON.parse(localStorage.getItem(DRAFT_KEY) || "null");
    return v && typeof v.text === "string" ? v : null;
  } catch { return null; }
}
export function putDraft(d) {
  try { localStorage.setItem(DRAFT_KEY, JSON.stringify(d)); } catch { /* not persisted */ }
}
