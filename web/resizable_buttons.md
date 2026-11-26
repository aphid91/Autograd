# Resizable buttons — spec

Status: **not built**. The fixed 3×3 button ([button.js](button.js), "The
button" in [CLAUDE.md](CLAUDE.md)) is. This file specs the second step: a
button whose footprint the player resizes by dragging its corner, to any
whole-number rectangle. It is written so another agent can pick it up cold.
Read web/CLAUDE.md's "The button", "Selecting" and "Placing machines" first.

## What the player sees

- A button is any **w × h** rectangle of whole cells, `1 ≤ w, h ≤ BUTTON_MAX_SIDE`
  (suggest 16). A fresh one is 3 × 3, as now.
- In **Build**, with **exactly one button selected** (and nothing else), a
  **resize grip** draws at the selection ghost's **screen bottom-right
  corner**: a small right-angle mark, sized in screen pixels (about 22 CSS px,
  never in world units — a world-sized grip vanishes zoomed out and covers
  the button zoomed in). Same reasoning as the desktop's text-box grip
  (root CLAUDE.md, "And resizing it, from a corner grip").
- Dragging the grip resizes the lifted button live: the ghost snaps to whole
  cells, the **top-left corner stays put** (the grip is bottom-right, so the
  opposite corner is the anchor), clashes tint red exactly as a moved
  selection does. Lifting the finger leaves it lifted; the usual deselect
  ("tap empty board", "tap it") puts it down **if it fits**, like any other
  selection. That gives clash handling, the red tint and the undo step for
  free — the reason the grip lives in Build, not the Hand (where a touch on
  a button is a press).
- The grip is checked **before** the body: grabbing the corner must never
  read as dragging the selection.
- The resize is an **edit** (as placing is): the simulation restarts when it
  goes down. One undo step.

## Storage

- Width and height already live in every row: `F_W` / `F_H` (5, 6) hold the
  **placed** (rotated) extent. For a button they become the source of truth
  instead of being copied from the type table.
- Keep an authored size too, so rotation is unambiguous: the authored
  `[aw, ah]` (at facing +X) is `[F_W, F_H]` for dir 0/2 and `[F_H, F_W]` for
  dir 1/3. No new slot is needed — derive it.
- Stamp entries (`board.capture()`, `stamp.js`) gain `size: [aw, ah]`
  (authored), null for everything else. Copy, cut, paste, baked slots, undo
  snapshots and saves all carry it. A stamp entry without `size` is 3 × 3
  (old stamps, old saves). Toolbar slots may carry `size` too (a resized
  button picked up with the pipette-equivalent, or a later "size" control);
  absent means 3 × 3.

## Ports (the core rule)

Authored at facing +X, with `aw × ah` the authored size:

- **Input**: west face (`f = 2`) of cell `(0, my)`.
- **Output**: east face (`f = 0`) of cell `(aw − 1, my)` — straight across,
  same row.
- `my = floor((ah − 1) / 2)`: the middle row, and for an **even** `ah` the
  **lower** of the two middle rows. Arbitrary, because flip picks the other.

Then flip-then-rotate exactly as every machine does (`placePorts()` in
pixel.js, a port of the desktop's `Entity.place_local`). So:

- **Chirality depends on the size.** With `ah` odd the ports sit on the
  centre row and a mirror maps them onto themselves: flip does nothing and
  must be disabled (like the 3 × 3 today). With `ah` even, flip moves both
  ports to the other middle row: a real second orientation. `canFlip()`,
  the thumb pads and `stamp.js mirrored()` (its `chiral` test) must ask the
  **instance**, not `td.chiral`.
- A 1-wide button (`aw = 1`) has input and output on the same cell, opposite
  faces. Legal. A 1-tall one is fine too.
- No pass-through overlay (`pass: []`), at any size.

Provide one function and use it everywhere:

```js
// button.js
export function buttonOrient(aw, ah, dir, flip)  // -> { w, h, inputs, outputs, pass: [] }
```

memoised on `${aw}x${ah}:${dir}${flip}` (there are few distinct sizes).

## The one refactor this needs

Every reader of ports and extent goes through the **type's** table today:
`td.orient[\`${dir}${flip}\`]`. Thirteen call sites (grep `orient\[`):

| file | what it reads it for |
|---|---|
| sim.js | `orientOf(row)`: ports for `buildGates()` / drains / the run tracer's port probing |
| board.js (×3) | `footprint()` (cells + blocked), the render list, `entryBlocked()`/capture extents |
| ghost.js (×2) | ghost extents and ports for placement/stamp ghosts |
| stamp.js | stamp bounding box |
| tools.js (×2) | swatch art, stamp art |
| main.js (×2) | the held tool's orientation, `currentPlacement()` |
| keyboard.js, pixel.js | building their own tables (leave alone) |

Replace them with one accessor, in board.js (or a small `orient.js`):

```js
export function orientFor(T, typeId, dir, flip, size = null)
// size: authored [aw, ah] for a button, ignored for anything else
```

and two thin wrappers: `board.orientOfRow(row)` (reads type, dir, flip and,
for a button, `F_W`/`F_H` → authored size) and `orientOfEntry(T, e)` (a stamp
entry or a tool slot, using `e.size`). **The sim must use `orientOfRow`**,
or a resized button's ports are read at 3 × 3 and it silently connects to
the wrong cells — the "GLSL disagrees with Python" class of bug, here "the
sim disagrees with the board".

`board.footprint(type, origin, dir, flip)` and
`spawnStructure(..., { size })` take the size; `spawnStructure` writes the
placed extent into `F_W`/`F_H` from it.

Also check every place that **assumes the type's footprint**:
`segtrace.js` (port probing), `flow.js` (reads `F_W`/`F_H` — fine),
`layers.js` (reads `F_W`/`F_H` — fine), `pixel.js` centre (not a button),
`render.js` hit boxes, `main.js` `onGhost()` / tap margins.

## The gesture

In `main.js`, Build, selecting:

- `gripHit(sx, sy)` already exists for labels — add `buttonGripHit(sx, sy)`:
  true when the selection is exactly one button and the point is within the
  grip's screen square at the ghost's screen bottom-right (rotation does not
  matter: it is the **screen** corner of the axis-aligned box).
- `dragHooks.wants()` claims the drag when `buttonGripHit()`, checked
  **before** the selection-body test. `begin()` records `{ resize: true,
  world0, aw0, ah0 }` anchored at the **press** (the camera.js convention).
- `move()` converts the world delta to cells: width grows with +x, height
  grows with **−y** (world +Y is up, the grip is at the bottom). Round to
  whole cells, clamp to `1..BUTTON_MAX_SIDE`. Map placed → authored through
  the dir (`dir` odd swaps them). Move the selection's origin so the
  top-left stays fixed (`origin.y` decreases as the height grows). Rebuild
  the selection ghost; red where it clashes.
- `end()` leaves it lifted. A resize to the size it already had changes
  nothing.

## Rendering

- Body, value text, lit overlay: all already read `s.w`/`s.h` from the render
  list, so a correct render list is enough. The value text scales with the
  box (render.js `emitSize()`); check a 1 × 1 and a 16 × 2 stay legible and
  inside the body.
- The glyph (`structGlyph("button", ...)`) scales to the shorter side already.
- Draw the grip only for a single selected button, in screen space, after
  the selection outline.

## Saves and the desktop

- Type id 14 is already web-only, so nothing new breaks on the desktop.
- `F_W`/`F_H` are already saved. A save holding a button from before this
  change has 3 × 3 there, so it loads unchanged.

## Tests to write

- `buttonOrient()`: for every size up to 6 × 6, every dir and flip, the
  input and output are on opposite faces of the same middle row, facing out
  of the footprint (the desktop's "a port always faces out" invariant), and
  flip is the identity iff the authored height is odd.
- A 4 × 2 button (even height) flipped connects on the other middle row —
  drive a poll through each orientation like `tests/button.test.mjs`'s rig.
- Resize then copy/paste/undo/save-load round-trips the size and the ports.
- The sim and the board agree: for random sizes/orientations,
  `sim.orientOf(row)` equals `board.orientOfRow(row)` (the refactor's
  failure mode).
- The gesture (Playwright, real touch): drag the grip of a selected 3 × 3 to
  5 × 2; the top-left stays put; deselect places it; the board's row has
  `F_W = 5, F_H = 2`; a clash leaves it lifted and red.
- Mutation-test: reading ports from the type table in sim.js must fail the
  agreement test; anchoring the resize at the bottom-left must fail the
  gesture test.

## Open questions for the player

- Should a toolbar slot remember a size (so a row of identical 4 × 2 buttons
  is quick to place)? Suggest: yes, the selection's size is copied into
  stamps anyway, and a baked stamp slot already gives this for free — so
  probably no extra UI.
- Max side: 16 is a guess.
