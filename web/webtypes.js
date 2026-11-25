// The machines this app has and the desktop does not, installed into a
// loaded types.json: Pixel (over Sink's id) and the Keyboard (a new id).
// Call once, right after loading it; idempotent.

import { installPixel } from "./pixel.js";
import { installKeyboard } from "./keyboard.js";

export function installWebTypes(T) {
  return installKeyboard(installPixel(T));
}
