// Line icons, 24x24, stroked with currentColor. Most are Lucide shapes
// (ISC licence); kept as path data so there is no icon font or sprite file.

const P = {
  build: ["M15 12l-8.5 8.5a2.12 2.12 0 0 1-3-3L12 9",
          "M17.64 15 22 10.64",
          "M20.91 11.7l-1.25-1.25c-.6-.6-.93-1.4-.93-2.25v-.86L16.01 4.6a5.56 5.56 0 0 0-3.94-1.64H9l.92.82A6.18 6.18 0 0 1 12 8.4v1.56l2 2h2.47l2.26 1.91"],
  hand: ["M18 11V6a2 2 0 0 0-4 0v5",
         "M14 10V4a2 2 0 0 0-4 0v6",
         "M10 10.5V6a2 2 0 0 0-4 0v8",
         "M18 8a2 2 0 1 1 4 0v6a8 8 0 0 1-8 8h-2c-2.8 0-4.5-.86-5.99-2.34l-3.6-3.6a2 2 0 0 1 2.83-2.82L7 15"],
  select: ["M5 3a2 2 0 0 0-2 2", "M19 3a2 2 0 0 1 2 2", "M21 19a2 2 0 0 1-2 2",
           "M5 21a2 2 0 0 1-2-2", "M9 3h1", "M9 21h1", "M14 3h1", "M14 21h1",
           "M3 9v1", "M21 9v1", "M3 14v1", "M21 14v1"],
  reset: ["M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8", "M3 3v5h5"],
  rotCw: ["M12 5H6a2 2 0 0 0-2 2v3", "M9 8l3-3-3-3",
          "M4 14v4a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7a2 2 0 0 0-2-2h-2"],
  flip: ["M3 7l5 5-5 5V7", "M21 7l-5 5 5 5V7", "M12 20v2", "M12 14v2", "M12 8v2", "M12 2v2"],
  trash: ["M3 6h18", "M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6",
          "M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2", "M10 11v6", "M14 11v6"],
  cut: ["M20 4 8.12 15.88", "M14.47 14.48 20 20", "M8.12 8.12 12 12"],
  copy: ["M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"],
  paste: ["M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2"],
  plus: ["M12 5v14", "M5 12h14"],
  box: ["M4 4h2", "M9 4h2", "M14 4h2", "M4 4v2", "M4 9v2", "M4 14v2", "M4 16h2", "M9 16h2", "M16 4v2", "M16 9v2",
        "M13 13l8 3-3.5 1.5L16 21z"],
  left: ["M15 5l-7 7 7 7"],
  x: ["M18 6 6 18", "M6 6l12 12"],
  check: ["M20 6 9 17l-5-5"],
  routeHV: ["M4 6h12a2 2 0 0 1 2 2v12", "M15 17l3 3 3-3"],
  routeVH: ["M6 4v12a2 2 0 0 0 2 2h12", "M17 15l3 3-3 3"],
  routeLine: ["M4 20h4v-4h4v-4h4V8h4", "M17 4h3v3"],
  right: ["M9 5l7 7-7 7"],
  minus: ["M5 12h14"],
  reverse: ["M8 3 4 7l4 4", "M4 7h16", "M16 21l4-4-4-4", "M20 17H4"],
  index: ["M10 6h11", "M10 12h11", "M10 18h11", "M4 6h1v4", "M4 10h2", "M6 18H4c0-1 2-2 2-3s-1-1.5-2-1"],
  table: ["M3 3h18v18H3z", "M3 9h18", "M3 15h18", "M9 3v18"],
  vector: ["M5 19 19 5", "M11 5h8v8", "M3 21h4"],
  // Build with a vector across it, bottom right to top left: the picker.
  buildVector: ["M15 12l-8.5 8.5a2.12 2.12 0 0 1-3-3L12 9", "M21 21 4.5 4.5", "M4 10V4h6"],
  undo: ["M9 14 4 9l5-5", "M4 9h10.5a5.5 5.5 0 0 1 0 11H11"],
  eye: ["M2.06 12.35a1 1 0 0 1 0-.7 10.75 10.75 0 0 1 19.88 0 1 1 0 0 1 0 .7 10.75 10.75 0 0 1-19.88 0"],
  eyeOff: ["M10.73 5.08A10.43 10.43 0 0 1 12 5c4.84 0 8.5 3 9.94 6.65a1 1 0 0 1 0 .7 10.8 10.8 0 0 1-1.44 2.49",
           "M14.08 14.16a3 3 0 0 1-4.24-4.24", "M17.48 17.5a10.75 10.75 0 0 1-15.42-5.15 1 1 0 0 1 0-.7 10.75 10.75 0 0 1 4.45-5.14",
           "M2 2l20 20"],
  // Lucide star: a layer's active toggle (layerpanel.js fills it when on).
  star: ["M11.525 2.295a.53.53 0 0 1 .95 0l2.31 4.679a2.123 2.123 0 0 0 1.595 1.16l5.166.756a.53.53 0 0 1 .294.904l-3.736 3.638a2.123 2.123 0 0 0-.611 1.878l.882 5.14a.53.53 0 0 1-.771.56l-4.618-2.428a2.122 2.122 0 0 0-1.973 0L6.396 21.01a.53.53 0 0 1-.77-.56l.881-5.139a2.122 2.122 0 0 0-.611-1.879L2.16 9.795a.53.53 0 0 1 .294-.906l5.165-.755a2.122 2.122 0 0 0 1.597-1.16z"],
  layers: ["M12.83 2.18a2 2 0 0 0-1.66 0L2.6 6.08a1 1 0 0 0 0 1.83l8.58 3.91a2 2 0 0 0 1.66 0l8.58-3.9a1 1 0 0 0 0-1.83z",
           "M2 12a1 1 0 0 0 .58.91l8.6 3.91a2 2 0 0 0 1.65 0l8.58-3.9A1 1 0 0 0 22 12",
           "M2 17a1 1 0 0 0 .58.91l8.6 3.91a2 2 0 0 0 1.65 0l8.58-3.9A1 1 0 0 0 22 17"],
  pencil: ["M21.17 6.81a1 1 0 0 0-3.99-3.99L3.84 16.17a2 2 0 0 0-.5.83l-1.32 4.35a.5.5 0 0 0 .62.62l4.35-1.32a2 2 0 0 0 .83-.5z"],
  // Lucide volume-2 / volume-x: the board's sound button.
  soundOn: ["M11 4.702a.705.705 0 0 0-1.203-.498L6.413 7.587A1.4 1.4 0 0 1 5.416 8H3a1 1 0 0 0-1 1v6a1 1 0 0 0 1 1h2.416a1.4 1.4 0 0 1 .997.413l3.383 3.384A.705.705 0 0 0 11 19.298z",
            "M16 9a5 5 0 0 1 0 6", "M19.364 18.364a9 9 0 0 0 0-12.728"],
  soundOff: ["M11 4.702a.705.705 0 0 0-1.203-.498L6.413 7.587A1.4 1.4 0 0 1 5.416 8H3a1 1 0 0 0-1 1v6a1 1 0 0 0 1 1h2.416a1.4 1.4 0 0 1 .997.413l3.383 3.384A.705.705 0 0 0 11 19.298z",
             "M22 9l-6 6", "M16 9l6 6"],
  checkpoint: ["M4 15s1-1 4-1 5 2 8 2 4-1 4-1V3s-1 1-4 1-5-2-8-2-4 1-4 1z", "M4 22v-7"],
};

// Extra non-path children, as markup.
const EXTRA = {
  cut: '<circle cx="6" cy="6" r="3"/><circle cx="6" cy="18" r="3"/>',
  copy: '<rect x="8" y="8" width="14" height="14" rx="2"/>',
  paste: '<rect x="8" y="2" width="8" height="4" rx="1"/>',
  eye: '<circle cx="12" cy="12" r="3"/>',
  play: '<path class="fill" d="M7 4.5v15l12.5-7.5z"/>',
  pause: '<rect class="fill" x="6" y="4.5" width="4.2" height="15" rx="1"/>' +
         '<rect class="fill" x="13.8" y="4.5" width="4.2" height="15" rx="1"/>',
};

// `mirror` draws the icon reflected left-right: rotate-CCW is rotate-CW seen
// in a mirror, which keeps the pair visibly one family.
export function icon(name, { mirror = false } = {}) {
  const base = name === "rotCcw" ? "rotCw" : name === "redo" ? "undo" : name;
  const paths = (P[base] || []).map(d => `<path d="${d}"/>`).join("");
  const flip = mirror || name === "rotCcw" || name === "redo" ? ' transform="matrix(-1 0 0 1 24 0)"' : "";
  return `<svg viewBox="0 0 24 24" aria-hidden="true"><g${flip}>${paths}${EXTRA[base] || ""}</g></svg>`;
}
