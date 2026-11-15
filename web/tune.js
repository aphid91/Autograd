// The Dev dials: numbers that have been tuned by eye, and one behaviour
// switch, gathered so they can be changed from the panel without an edit.
// Read live by render.js, tools.js and main.js; written only by panel.js
// (through main.js), and persisted with the panel's preferences.
//
// Scales multiply the built-in value, so 1 is always "as shipped".

export const TUNE_DEFAULTS = {
  forceBuild: false,     // a blocked machine, raw belt or run deletes what blocks it
  beltWidth: 1,          // belt spine width (0.34 cells)
  chevronSize: 1,        // the direction chevrons on belts and overlays
  overlayWidth: 1,       // the control overlay through a machine (0.28 cells)
  overlayEdge: 1,        // its dark edge
  portScale: 1,          // the port arrows
  portEdge: 1,           // their outline
  glyphScale: 1,         // the machine glyph's reach
  glyphWeight: 1,        // its stroke weight
  emitSize: 1,           // a Source's value
  gridOpacity: 1,        // the grid lines
  ghostAlpha: 0.6,       // planned and selected ghosts
  tapMarginPx: 20,       // how near a ghost or a run's tip a tap counts as on it
};

// What the panel shows, in order: [key, label, { min, max, step }] for a
// number, [key, label] for a switch.
export const TUNE_SPEC = [
  ["forceBuild", "force build"],
  ["beltWidth", "belt width", { min: 0.3, max: 2, step: 0.05 }],
  ["chevronSize", "chevrons", { min: 0, max: 2, step: 0.05 }],
  ["overlayWidth", "overlay width", { min: 0.3, max: 2, step: 0.05 }],
  ["overlayEdge", "overlay edge", { min: 0, max: 4, step: 0.1 }],
  ["portScale", "port size", { min: 0.3, max: 2.5, step: 0.05 }],
  ["portEdge", "port edge", { min: 0, max: 4, step: 0.1 }],
  ["glyphScale", "glyph size", { min: 0.3, max: 2, step: 0.05 }],
  ["glyphWeight", "glyph weight", { min: 0.3, max: 3, step: 0.05 }],
  ["emitSize", "source value", { min: 0.3, max: 2, step: 0.05 }],
  ["gridOpacity", "grid", { min: 0, max: 1, step: 0.05 }],
  ["ghostAlpha", "ghost opacity", { min: 0.1, max: 1, step: 0.05 }],
  ["tapMarginPx", "tap margin px", { min: 0, max: 60, step: 1 }],
];

export const TUNE = { ...TUNE_DEFAULTS };

// Take whatever of `values` names a known dial; ignore the rest, so a stale
// stored preference cannot break anything.
export function setTune(values) {
  for (const k of Object.keys(TUNE_DEFAULTS)) {
    if (values && typeof values[k] === typeof TUNE_DEFAULTS[k]) TUNE[k] = values[k];
  }
}
