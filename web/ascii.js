// Characters: the one place that says what a number means as ASCII, so the
// board (ASCII sources, item numbers), the value picker, the index preview
// and the keyboard all agree. Pure, tested in tests/ascii.test.mjs.
//
// A value names a character by its whole part: floor(v). 0..127 is ASCII;
// anything else (negative, past 127, NaN, infinite) is not.

export const END_OF_STREAM = -1;

export function codeOf(v) {
  return Number.isFinite(v) ? Math.floor(v) : NaN;
}

// floor(v) in 0..127.
export function isAscii(v) {
  const c = codeOf(v);
  return c >= 0 && c <= 127;
}

// floor(v) a printable character: space through tilde.
export function isPrintable(v) {
  const c = codeOf(v);
  return c >= 32 && c <= 126;
}

// How a character shows on the board. Printable characters as themselves
// (space as an open box, so it is visible); the control codes and DEL as
// their Unicode control pictures (U+2400..U+241F, U+2421), which is what a
// terminal shows for them -- "␊" for a newline, "␈" for backspace. Not
// ASCII at all: null, drawn as the null glyph (a hollow box).
export function glyphOf(v) {
  const c = codeOf(v);
  if (!(c >= 0 && c <= 127)) return null;
  if (c === 32) return "␣";
  if (c > 32 && c < 127) return String.fromCharCode(c);
  if (c === 127) return "␡";
  return String.fromCharCode(0x2400 + c);
}

export const NULL_GLYPH = "□";

// Readable text for a list or a preview: 'A', '\n', ' ', ...
export function nameOf(v) {
  const c = codeOf(v);
  if (!(c >= 0 && c <= 127)) return null;
  const esc = { 0: "\\0", 8: "\\b", 9: "\\t", 10: "\\n", 13: "\\r", 27: "esc", 127: "del" };
  if (esc[c] !== undefined) return esc[c];
  if (c < 32) return `^${String.fromCharCode(c + 64)}`;
  return `'${String.fromCharCode(c)}'`;
}

// What the custom value box means by `text` (trimmed; "," read as "."):
//   on an ASCII source, a single character is that character, so "5" is 53
//   and "=" is 61, and anything longer is a number ("-1", "53");
//   on a plain Source (or a belt), a number is a number, and a single
//   character that is not one is its code, so "=" is 61.
// Returns the number, or null when the text means nothing.
export function parseValue(text, ascii) {
  const raw = String(text);
  // A lone space is a character, not empty input.
  if (raw === " ") return 32;
  const t = raw.trim();
  if (!t) return null;
  const chars = Array.from(t);
  if (ascii && chars.length === 1) return chars[0].codePointAt(0);
  const v = Number(t.replace(",", "."));
  if (Number.isFinite(v)) return v;
  if (chars.length === 1) return chars[0].codePointAt(0);
  return null;
}

// The value as the box shows it: an ASCII source's character when it has
// one (a control code by its name), otherwise the number.
export function valueText(v, ascii, fmt = String) {
  if (ascii && isAscii(v) && v === codeOf(v)) {
    const c = codeOf(v);
    return c > 32 && c < 127 ? String.fromCharCode(c) : nameOf(c);
  }
  return fmt(v);
}

// The six most recent distinct values picked in the picker for an ASCII
// source, most recent first. Kept in this browser; picks in the picker only
// (not indexing). Before any, a few useful ones.
const RECENT_KEY = "gates.web.asciiRecent.v1";
export const RECENT_DEFAULTS = [32, 65, 48, 10, 46, 45];   // space A 0 \n . -
export const RECENT_N = 6;

export function pushRecent(list, v) {
  const out = [v, ...list.filter(x => x !== v)];
  return out.slice(0, RECENT_N);
}

export function loadRecent() {
  try {
    const v = JSON.parse(localStorage.getItem(RECENT_KEY) || "null");
    if (Array.isArray(v)) {
      const nums = v.filter(Number.isFinite);
      // Topped up from the defaults, so there are always six.
      for (const d of RECENT_DEFAULTS) if (nums.length < RECENT_N && !nums.includes(d)) nums.push(d);
      return nums.slice(0, RECENT_N);
    }
  } catch { /* storage absent or corrupt */ }
  return RECENT_DEFAULTS.slice();
}

export function saveRecent(list) {
  try { localStorage.setItem(RECENT_KEY, JSON.stringify(list)); } catch { /* not kept */ }
}

// The ASCII code a key press means, or null for none. `ev` is a
// KeyboardEvent-like { key, ctrlKey }. Enter is a newline (10, matching the
// ASCII stream's "\n"), Backspace BS (8), Delete DEL (127), Tab 9, Escape 27.
export function keyCode(ev) {
  const k = ev.key;
  const named = { Enter: 10, Backspace: 8, Delete: 127, Tab: 9, Escape: 27 };
  if (named[k] !== undefined) return named[k];
  if (typeof k !== "string") return null;
  const chars = Array.from(k);
  if (chars.length !== 1) return null;   // "ArrowLeft", "Shift", ...
  const c = chars[0].codePointAt(0);
  // ctrl+letter is the control code, as a terminal sends it.
  if (ev.ctrlKey && /^[a-z]$/i.test(k)) return k.toUpperCase().charCodeAt(0) - 64;
  return c;
}

// An ASCII source's body: lime, where a plain Source is light blue.
export const ASCII_COLOUR = [0.6, 0.86, 0.2];

// What a phone keyboard typed, from a text field's value before and after
// one `input` event: phone keyboards report no usable keys (Android sends
// keyCode 229), and a word being composed or autocorrected arrives as a
// replacement. The text from the first difference to the last is the
// change: a backspace (8) for each character it took away, then the codes
// of what it put in.
export function textDiff(prev, now) {
  const a = Array.from(prev), b = Array.from(now);
  let i = 0;
  while (i < a.length && i < b.length && a[i] === b[i]) i++;
  let j = 0;
  while (j < a.length - i && j < b.length - i && a[a.length - 1 - j] === b[b.length - 1 - j]) j++;
  const out = [];
  for (let k = 0; k < a.length - i - j; k++) out.push(8);
  for (const ch of b.slice(i, b.length - j)) out.push(ch.codePointAt(0));
  return out;
}
