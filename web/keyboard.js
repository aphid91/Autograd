// The keyboard: a web-only machine that types into a running simulation.
//
// To the engine it is a generator (no inputs, one output) like a Source,
// with one difference: it has nothing to emit until a key is pressed. Each
// press of a key, with the keyboard inspected in the Hand, queues one ASCII
// code (ascii.js keyCode()/inputCodes()) on that keyboard (Sim.inject()),
// and the keyboard emits the queue's head whenever its output zone is free
// -- one token per tick at most, back pressure holding the rest.
//
// 5 x 3, beige, its one output in the middle of a long side: authored at
// facing +X with the output on the north face of the middle top cell.
// Flip would only do what a half turn does, so it is not offered.
//
// Its type id is new (KEYBOARD_TYPE, past every desktop type), so a save
// holding one is web-only: the desktop does not know the id. Accepted.

import { placePorts } from "./pixel.js";

export const KEYBOARD_TYPE = 13;
const FOOTPRINT = [5, 3];
const OUT = [[2, 2, 1]];   // [lx, ly, face] at facing +X: north face, middle top cell
export const KEYBOARD_COLOUR = [0.84, 0.77, 0.62];
// A press queued past this many waiting tokens is dropped.
export const KEY_QUEUE_MAX = 256;

export function installKeyboard(T) {
  if (T.types[KEYBOARD_TYPE]) {
    if (T.types[KEYBOARD_TYPE].class === "Keyboard") return T;
    throw new Error(`type id ${KEYBOARD_TYPE} is taken: the keyboard needs a new one`);
  }
  const orient = {};
  for (let d = 0; d < 4; d++) {
    for (const fl of [0, 1]) {
      const [w, h] = d % 2 ? [FOOTPRINT[1], FOOTPRINT[0]] : FOOTPRINT;
      orient[`${d}${fl}`] = { w, h, inputs: [], outputs: placePorts(FOOTPRINT, OUT, d, !!fl), pass: [] };
    }
  }
  T.types[KEYBOARD_TYPE] = {
    name: "keyboard", class: "Keyboard", kind: "structure", footprint: FOOTPRINT,
    colour: KEYBOARD_COLOUR, chiral: false, glyph: "keys", orient,
    // A generator whose constant is the queue's head (Sim.fire()).
    sim: { fire_rule: T.sim.FIRE_ALL, in_ch: [], out_ch: [0], n_in: 0, n_out: 1,
           ops: [[T.sim.OPS.OP_CONST]], select: null, keyboard: true },
  };
  return T;
}
