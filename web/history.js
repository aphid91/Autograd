// Undo, redo and the checkpoint.
//
// The history is a short list of STATES, not of edits: each entry is a whole
// snapshot of the board (see Board.snapshot()), so restoring one cannot
// half-apply anything and nothing has to know how to invert an edit. The
// list keeps UNDO_DEPTH steps behind the current state -- UNDO_DEPTH + 1
// states -- and drops the oldest beyond that.
//
// The CHECKPOINT is one more state, set when the board loads and whenever
// the player presses Checkpoint. It sits just before the oldest entry: undo
// from the oldest goes to the checkpoint, and redo from there walks forward
// through the history again. An edit made while on the checkpoint starts the
// history afresh from it.
//
// Pure: states are opaque values. main.js decides what a state is and when
// one is committed (only when nothing is lifted -- see main.js), and applies
// whatever undo() / redo() hand back.

export const UNDO_DEPTH = 15;

export class History {
  constructor(depth = UNDO_DEPTH) {
    this.depth = depth;
    this.reset(null);
  }

  // A fresh history at `state`, which is also the checkpoint.
  reset(state) {
    this.states = state === null ? [] : [state];
    this.cursor = state === null ? -1 : 0;   // -1: on the checkpoint
    this.checkpoint = state;
  }

  get current() { return this.cursor < 0 ? this.checkpoint : this.states[this.cursor]; }

  // A new state after an edit. Anything ahead of the cursor (undone steps)
  // is discarded, as in any editor.
  commit(state) {
    const kept = this.cursor < 0 ? [this.checkpoint] : this.states.slice(0, this.cursor + 1);
    kept.push(state);
    while (kept.length > this.depth + 1) kept.shift();
    this.states = kept;
    this.cursor = kept.length - 1;
  }

  setCheckpoint(state) { this.checkpoint = state; }

  // Undo past the oldest kept state lands on the checkpoint -- unless the
  // checkpoint IS that state (a fresh load), where there is nothing older.
  get canUndo() {
    if (this.cursor > 0) return true;
    return this.cursor === 0 && this.checkpoint !== null && this.checkpoint !== this.states[0];
  }

  get canRedo() { return this.cursor < this.states.length - 1; }

  // The state to restore, or null.
  undo() {
    if (!this.canUndo) return null;
    this.cursor--;
    return this.current;
  }

  redo() {
    if (!this.canRedo) return null;
    this.cursor++;
    return this.current;
  }
}
