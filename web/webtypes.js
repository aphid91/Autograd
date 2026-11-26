// The machines this app has and the desktop does not, installed into a
// loaded types.json: Pixel (over Sink's id), the Keyboard and the Button
// (new ids). Call once, right after loading it; idempotent.

import { installPixel } from "./pixel.js";
import { installKeyboard } from "./keyboard.js";
import { installButton } from "./button.js";

export function installWebTypes(T) {
  return installButton(installKeyboard(installPixel(T)));
}
