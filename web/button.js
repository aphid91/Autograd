// The button: a web-only machine the player presses while the simulation
// runs. Like a Source it has a picked value (and an ASCII flag that draws
// it as a character); unlike one, it emits only when POLLED -- any token
// arriving on its one input is consumed and answered with one token out:
// the picked value if the button is "on", else 0.
//
// What "on" means is the button's mode (cycled from the Hand):
//
//   EVENT   each press queues one event (at most BUTTON_EVENT_MAX); a poll
//           with events queued takes one and answers the value, else 0.
//   HOLD    a poll answers the value while a finger is on the button.
//   TOGGLE  each press flips it; a poll answers the value while it is on.
//
// A "press" is a finger arriving on the button while a button is inspected
// in the Hand -- a tap, a hold, or a drag from another button sliding onto
// this one (main.js pressHooks). The events, the toggle and the fingers
// are sim state (Sim.buttons), like a keyboard's queue: every fresh start
// (an edit, Reset) forgets them.
//
// 3 x 3, a yellower beige than the keyboard. The input is in the middle of
// the west face and the output straight across, in the middle of the east
// face: authored at facing +X. A 3 x 3 is symmetric about its middle row,
// so flip changes nothing and is not offered. No pass-through overlay: the
// token in is not the token out.
//
// Its type id is new (BUTTON_TYPE), so a save holding one is web-only.
//
// Row slots: the value in 25 and the mode in 26 (a belt's seed value and
// the desktop's sleep flag -- nothing reads either on a machine here), the
// ASCII flag in 24 as on a Source (board.js F_ASCII). Not F_EMIT: on a
// Source that is a dead input slot, but the button's input is live there.

import { placePorts } from "./pixel.js";

export const BUTTON_TYPE = 14;
const FOOTPRINT = [3, 3];
// [lx, ly, face] at facing +X; faces 0 +X, 1 +Y, 2 -X, 3 -Y.
const IN = [[0, 1, 2]];
const OUT = [[2, 1, 0]];
// A classic beige keycap: pale and warm.
export const BUTTON_COLOUR = [0.87, 0.83, 0.70];

export const F_BTN_VALUE = 25;
export const F_BTN_MODE = 26;

export const BTN_EVENT = 0, BTN_HOLD = 1, BTN_TOGGLE = 2;
export const BUTTON_MODES = [BTN_EVENT, BTN_HOLD, BTN_TOGGLE];
export const BUTTON_MODE_NAMES = ["Event", "Hold", "Toggle"];
// Presses queued past this many waiting events are dropped.
export const BUTTON_EVENT_MAX = 16;
// What a freshly placed button emits when on. Not 0: a button emitting 0
// when on would read exactly like one that is off.
export const BUTTON_DEFAULT_VALUE = 1;

export const modeOf = (m) => (BUTTON_MODES.includes(m) ? m : BTN_EVENT);
export const nextMode = (m) => BUTTON_MODES[(BUTTON_MODES.indexOf(modeOf(m)) + 1) % BUTTON_MODES.length];

export function installButton(T) {
  if (T.types[BUTTON_TYPE]) {
    if (T.types[BUTTON_TYPE].class === "Button") return T;
    throw new Error(`type id ${BUTTON_TYPE} is taken: the button needs a new one`);
  }
  if (T.entity.STRIDE <= F_BTN_MODE || F_BTN_VALUE < T.entity.F_EMIT + 4 * T.sim.MAX_CHANNELS) {
    throw new Error(`entity slots ${F_BTN_VALUE}, ${F_BTN_MODE} are not free for a button`);
  }
  const orient = {};
  for (let d = 0; d < 4; d++) {
    for (const fl of [0, 1]) {
      const [w, h] = d % 2 ? [FOOTPRINT[1], FOOTPRINT[0]] : FOOTPRINT;
      orient[`${d}${fl}`] = { w, h, inputs: placePorts(FOOTPRINT, IN, d, !!fl),
                              outputs: placePorts(FOOTPRINT, OUT, d, !!fl), pass: [] };
    }
  }
  T.types[BUTTON_TYPE] = {
    name: "button", class: "Button", kind: "structure", footprint: FOOTPRINT,
    colour: BUTTON_COLOUR, chiral: false, glyph: "button", orient,
    // Fires on a poll (FIRE_ALL over its one input); the constant it emits
    // is decided by Sim.buttonOut() at the fire.
    sim: { fire_rule: T.sim.FIRE_ALL, in_ch: [0], out_ch: [0], n_in: 1, n_out: 1,
           ops: [[T.sim.OPS.OP_CONST]], select: null, button: true },
  };
  return T;
}

// One button's sim state: events queued, toggled on, fingers on it.
export const freshButton = () => ({ ev: 0, on: false, held: 0 });

// The buttons' states, copied (Sim.clone(), restoreFrom(), simhost.js).
export function copyButtons(m) {
  return new Map([...(m || [])].map(([r, b]) => [r, { ev: b.ev, on: b.on, held: b.held }]));
}
