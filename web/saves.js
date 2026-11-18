// Boards saved in the browser: whole .npz files in localStorage, base64.
//
// localStorage holds strings and has a small quota (about 5 MB per site), so
// a save is the same deflated .npz an export downloads -- a 512x512 board is
// mostly zeros and comes to tens of kilobytes. The index is a separate key,
// so listing the saves never decodes one. Every access is guarded: storage
// can be absent, full, or throw in a private window.

const INDEX_KEY = "gates.web.saves.v1";
const ITEM_KEY = (name) => `gates.web.save.v1:${name}`;

function readIndex() {
  try {
    const v = JSON.parse(localStorage.getItem(INDEX_KEY) || "[]");
    return Array.isArray(v) ? v.filter(e => e && typeof e.name === "string") : [];
  } catch { return []; }
}

// Every browser save, most recently saved first: { name, savedAt, bytes }.
export function listSaves() {
  return readIndex().sort((a, b) => (b.savedAt || 0) - (a.savedAt || 0));
}

function toBase64(bytes) {
  let s = "";
  for (let i = 0; i < bytes.length; i += 0x8000) s += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  return btoa(s);
}

function fromBase64(text) {
  const s = atob(text);
  const out = new Uint8Array(s.length);
  for (let i = 0; i < s.length; i++) out[i] = s.charCodeAt(i);
  return out;
}

// Store `bytes` under `name`, replacing a save of the same name. Throws with
// a readable message when the browser refuses (quota, storage off).
// `meta` is kept in the index beside it (the autosaves record which board
// they are of).
export function putSave(name, bytes, meta = {}) {
  const index = readIndex().filter(e => e.name !== name);
  index.push({ ...meta, name, savedAt: Date.now(), bytes: bytes.length });
  try {
    localStorage.setItem(ITEM_KEY(name), toBase64(bytes));
    localStorage.setItem(INDEX_KEY, JSON.stringify(index));
  } catch (err) {
    // Do not leave a half-written save listed, or an item nothing lists.
    try { if (!readIndex().some(e => e.name === name)) localStorage.removeItem(ITEM_KEY(name)); } catch { /* */ }
    throw new Error(`the browser would not store it (${err.name === "QuotaExceededError" ? "storage is full -- delete an old save" : err.message})`);
  }
}

// The .npz bytes of a save, or null.
export function getSave(name) {
  try {
    const text = localStorage.getItem(ITEM_KEY(name));
    return text === null ? null : fromBase64(text);
  } catch { return null; }
}

export function deleteSave(name) {
  try {
    localStorage.removeItem(ITEM_KEY(name));
    localStorage.setItem(INDEX_KEY, JSON.stringify(readIndex().filter(e => e.name !== name)));
  } catch { /* nothing to do */ }
}
