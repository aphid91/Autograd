// Typing into a keyboard machine (keyboard.js): a hidden text field that
// takes the focus while the Hand has a keyboard inspected, so a phone raises
// its on-screen keyboard and a desktop's keys land here, and turns each
// press into an ASCII code for `onCode`.
//
// Two routes, because phones and desktops report keys differently:
//   - keydown, for a physical keyboard: ascii.js keyCode() names the code,
//     and the default is prevented, so the field never changes;
//   - input, for an on-screen one, whose keydowns say nothing (Android's
//     keyCode 229): the field's text before and after (ascii.js
//     textDiff()) -- a typed character, a backspace, a newline, or a word
//     autocorrected as backspaces and its new text.
// The field holds a few spaces between presses, so a backspace always has
// something to delete (and so to report), and is put back after each press
// -- but not while the keyboard is composing a word, which resetting would
// break.
//
// The app's own shortcuts (Space for play, ctrl+Z) ignore key presses in a
// text field (main.js), so typing a space here types a space.

import { keyCode, textDiff } from "./ascii.js";

const PAD = "    ";

export class KeyCapture {
  // `onState()` is told whenever the field gains or loses the keys.
  constructor(onCode, onState = () => {}) {
    this.onCode = onCode;
    this.composing = false;
    const t = this.el = document.createElement("textarea");
    t.className = "keycap";
    t.setAttribute("aria-label", "Keyboard input");
    t.setAttribute("autocomplete", "off");
    t.setAttribute("autocorrect", "off");
    t.setAttribute("autocapitalize", "off");
    t.setAttribute("spellcheck", "false");
    t.setAttribute("enterkeyhint", "enter");
    t.tabIndex = -1;
    this.reset();
    t.addEventListener("keydown", (e) => {
      if (e.isComposing || e.keyCode === 229) return;    // an on-screen keyboard: see `input`
      if (e.metaKey || e.altKey) return;
      const c = keyCode(e);
      if (c === null) return;
      e.preventDefault();
      e.stopPropagation();
      this.onCode(c);
    });
    t.addEventListener("compositionstart", () => { this.composing = true; });
    t.addEventListener("compositionend", () => { this.composing = false; this.settle(); });
    t.addEventListener("input", () => {
      const now = t.value;
      for (const c of textDiff(this.prev, now)) this.onCode(c);
      this.prev = now;
      if (!this.composing) this.settle();
      // Should a browser scroll the page for the caret anyway, put it back:
      // the bar belongs behind the phone's keyboard while typing.
      if (window.scrollX || window.scrollY) window.scrollTo(0, 0);
    });
    t.addEventListener("blur", () => { this.composing = false; this.reset(); onState(); });
    t.addEventListener("focus", () => onState());
    // A tap on a button -- Play, Reset, the rate, the Type button -- must
    // not take the keys away: on a phone that would also drop the on-screen
    // keyboard. The focus moves on mousedown, so that is what is stopped;
    // the button still gets its click. Nor a tap on the board: the Hand
    // decides that one (main.js inspect(): another entity or bare board
    // ends typing). On a phone this is what made tapping the keyboard work
    // at all -- the tap focused the field on touch-up, and the browser's
    // synthesized mousedown on the canvas that follows took it straight back.
    document.addEventListener("mousedown", (e) => {
      if (this.active && e.target instanceof Element && e.target.closest("button, canvas")) e.preventDefault();
    }, true);
    document.body.append(t);
  }

  get active() { return document.activeElement === this.el; }

  // Raise the keyboard. Must run inside the tap that asks for it: a phone
  // only shows its keyboard for a focus made in a user gesture.
  start() {
    if (this.active) return;
    this.reset();
    this.el.focus({ preventScroll: true });
  }

  stop() { if (this.active) this.el.blur(); }

  reset() {
    this.el.value = PAD;
    this.prev = PAD;
    try { this.el.setSelectionRange(PAD.length, PAD.length); } catch { /* not focusable yet */ }
  }

  // Back to the padding once nothing is being composed, so the field never
  // grows and a backspace always has something to take.
  settle() {
    if (this.el.value !== PAD) this.reset();
  }
}
