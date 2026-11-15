# web/ — the mobile board editor

A phone-first web interface for building and editing Gates saves (`.npz`).
It is **not** a port of the Python app. The Python app is the
high-performance simulator (shader tick, wake lists, folding); this is where
the *building* UX gets designed: structure placement, belt routing,
copy/paste, move/rotate/flip, seeds and Source values, camera, toolbelt.
Read the root CLAUDE.md for what the game *is*; most of its engineering
notes are about the tick and do not apply here.

Current state: **the phone shell, the belt planner, machine placement,
per-instance settings, and select / cut / copy / paste.** It loads a save,
pans (one finger) and zooms (pinch, or the wheel), lays belt runs
(auto-tunnelling under obstacles), places machines and raw belts, edits a
Source's emit value or a belt's seed, and selects, moves, turns, deletes,
copies and pastes groups of entities.
Nothing ticks, Underground has no tool flow of its own (the planner covers
it), and edits are not saved back to a file.

## Run it

```bash
bash web/build.sh && python3 -m http.server -d _site 8000
```

`build.sh` copies the app and every root `.npz` into `_site/`, writing
`saves/index.json` for the save picker. On push to `Web`,
`.github/workflows/pages.yml` does the same and deploys to GitHub Pages.
`default.npz` is loaded at startup if it exists.

## The phone shell

Copied from Fluoddity's **sand mode** (`aphid91/fluoddity-web`, `sand.html`
and `src/sand/sandUi.ts`), portrait "band" plan: the board, then a row of
controls, then a scrolling swatch bar. The layout tokens and darkroom colours
at the top of `style.css` are sand mode's; sizes are `N * var(--u)` at UI
scale 1.15.

- **Mode** (left): Build and Hand. It decides what the swatch bar shows.
  - **Build**: the toolbelt. Tap selects, tapping the held slot again puts it
    down, **long press** (right-click on a mouse) opens the picker for that
    slot, and the `+` swatch appends. Slots persist in localStorage.
    Tapping an existing entity **selects** it, whatever the hand holds, and
    the bar becomes the selection's: Box, Delete, Cut, Copy, then the stamp
    history, most recent first. See "Selecting" below.
  - **Hand**: tapping the board inspects: the Status folder fills in (the
    panel is **not** opened for you -- a tap is often only to reach the
    number swatches), and a tap on bare board clears it. While Status shows a Source or
    a belt, the bar offers the number swatches (0, 1, −1, 2, 4, 8) for its
    emit value or seed; on a belt, tapping the lit number unseeds it.
- **Sim** (right): Reset (ctrl+R) and Play/Pause. Neither is wired to a
  simulation; Play/Pause only swaps its icon. Space toggles it.
- **Turn**: rotate CW, CCW, and flip. They act on what the hand holds: the
  held tool in Build (a stamp tool turns its stamp), the stamp being pasted,
  or the selection. A swatch draws its tool **at
  the orientation the next placement would have**, from the placed port
  tables in `types.json`, so turning is visible with nothing on the board.
  Flip is disabled on tools with no chirality (belts, Sink); on the
  underground it swaps which end the next tap lays, as on the desktop.

Three things worth knowing:

- **A long press swallows exactly one click**: the one the finger's own
  release produces. The first version used sand mode's 800 ms timeout, and
  that ate a genuine quick tap made right after the picker closed. Now the
  next pointerdown anywhere clears the flag.
- **A slot is plain data** (`{ kind, type?, dir, flip }`), and the kind
  strings are a compatibility surface because they live in localStorage. An
  entry naming a machine this build lacks becomes an empty slot rather than
  discarding the bar, the same rule the desktop's toolbar file follows.
- **`ui.js` and `panel.js` only request.** They report intents through
  callbacks and `main.js` applies them, so the board has one writer. That is
  the desktop's panel/orchestrator split.

The **Tweakpane** panel sits in the board's top-right corner, collapsed by
default. It holds the saves, View toggles (grid, ports, labels), Debug, and
the Status folder. Tweakpane is **vendored** (`vendor/`, MIT) rather than
pulled from a CDN, which keeps the no-build, works-offline property. Status
writes the same row fields the desktop's Status window does, plus the
seed's `.z` bits, so a save edited here reads correctly there.

## The belt planner

[planner.js](planner.js) is the geometry, as pure functions over a
`free(x, y)` predicate; `main.js` owns the gesture and the run state.

The gesture, with the Belt tool held:

| input | idle | planning |
|---|---|---|
| tap | start a run on that cell | **on the tip** (or within 20 px): turn it, cycling straight, right, left |
| | | **anywhere else**: **build** the run exactly as shown |
| one-finger drag | pan | move the run's **tip** |
| two fingers | pan / zoom | pan / zoom, always |
| rotate CW / CCW | turn the swatch | turn the **tip** |
| flip | -- | nothing |
| the bar | swatches | Cancel / Route / Build |

Four rules, each chosen over an obvious alternative:

- **A drag only ever moves the tip.** The first version moved whichever end
  was nearer the finger (the tip winning ties, so tap-then-drag always
  pulled the tip off the start). In practice that read as confusing and
  bought nothing, since Cancel and a fresh tap move the start just as well.
- **A dragged tip follows the finger's motion, not its position.** It never
  jumps under the thumb, so you can drag from below the tip and keep it in
  view. The cost is that an end cannot be teleported: pan with two fingers
  and drag again.
- **The tip is a turn, not a facing**: straight, left or right off the
  run's travel direction, never back into it (the desktop's `_exit_turn`
  rule). The buttons walk that ring, skipping "backwards". It is stored
  relative, but **resets to straight whenever the tip moves to a new cell**.
  A one-cell run has no travel direction, so there the buttons cycle all
  four facings, starting from the swatch's own rotation.
- **The second tap builds what is shown**, rather than moving the tip to
  where the finger landed. Tapping is a confirmation, not an aim.

**Thumb pads.** While anything is planned (a run, a placement, a paste) or
a selection is held, the Turn buttons appear again in both bottom corners of the board -- rotate
left and right, flip above them -- so the thumb steering the ghost need not
travel to the Turn row. On a belt run, flip toggles the route. Added in its
own commit as an experiment.

A second finger mid-drag ends the drag where it is and becomes a pinch. If
it then drops back to one finger, that finger does nothing (`dead` in
`camera.js`), rather than resuming a drag from wherever it happens to be.

The route (H-first or V-first) is stored on the tool slot, so it persists
like the desktop builder's mode. `planner.js` still knows the staircase
line, which the desktop offers, but the phone's Route button does not: it
was dropped as not worth a button. A slot saved while it existed reads as
H-first.

### Placing machines and raw belts

The other one-tap tools (every machine, and the raw belt) share one flow.
The **ghost** is held as a continuous world point and the footprint is
centred on it by `placer.js`, a port of `Entity.origin_for_cursor()` that is
**tested against the Python function itself**. An even extent snaps to the
nearest cell boundary, so a 2×1 moves every half cell, and rotation turns
the footprint about its middle.

| input | idle | placing |
|---|---|---|
| tap | start placing: ghost centred on the tap | **on the ghost** (or within 20 px of it): place it, and keep placing with the ghost where it is |
| | | **anywhere else**: place it, then stop placing |
| one-finger drag | pan | move the ghost, by the finger's motion |
| two fingers | pan / zoom | pan / zoom |
| the bar | swatches | Cancel, Left, Right, and number swatches for Source and raw belt |

- **Both taps place only if every cell is free**, as the desktop's
  `StructureBuilder` does, and the tap elsewhere stops placing **even when
  the placement was refused**. So a blocked ghost never strands you in the
  mode.
- **Tapping the ghost leaves it where it is**, now over what it just built
  and so tinted red. A row of machines is tap, nudge, tap, nudge.
- **The 20 px margin** exists because a 1×2 machine at building zoom is a
  small target, and a near miss would otherwise drop you out of placing
  mid-row.
- **The ghost reads the held tool on every redraw** (type, facing, flip,
  number), so the Turn buttons and the number swatches show on it at once,
  with no second copy of the tool to keep in step.
- **The number swatches**: 0, 1, −1, 2, 4, 8, in a 3×2 grid so Cancel and
  both rotations fit beside them without scrolling. They are the Source's
  emit value (default 0, so one is always selected) or the raw belt's seed
  (default **none**, meaning unseeded; tapping the selected number again
  clears it). The choice is stored on the slot (`slot.value`), so it carries
  to the next placement.

Underground ends draw **narrowing toward the buried side** (the front of an
entrance, the back of an exit), on the board and on the swatch.

### How machines draw

As the desktop draws them, because a design reads by it: the body, the
**control overlay** -- a faint belt straight through each cell a channel
passes unchanged (`Entity.PASS_THROUGH`) -- then the type's **glyph**
(Shear's +, Mac's ×, Swap's Z, Floor's arrow...), then small port arrows.
There are no type names; a Source shows its value instead of its dot once
the number is big enough to read, and its dot below that.

- **The overlay's cells are exported**, not worked out here.
  `export_web_types.py` applies the shader's rule (an axis from
  `PASS_THROUGH`, flipped then rotated like a port; drawn on a cell with a
  port along it, or between two cells of the footprint on that axis) and
  writes `pass: [[lx, ly, axis]]` per orientation, beside the ports.
- **The glyphs are ports of the `sd_*` functions in world.frag**, as SVG
  path data in `glyphs.js`, so the board (`Path2D`) and the swatches share
  one definition. A distance field minus a half-thickness is a round-capped
  stroke, so the shapes match. They do not turn with the machine: the
  shader draws them in world-aligned footprint space.
- **Ports are small and lightly edged.** They were enlarged while there was
  no overlay to orient a machine by; with it they came back down 35%, and
  the black edge became a translucent 0.75 px line drawn only from 14
  px/cell up, where zoomed out it had been most of what was drawn.

`Board.spawnStructure()` stamps every cell with its offset within the
**placed** footprint, as `Gameboard._stamp_footprint()` does. Checked by
re-placing every machine on a board (1,026 of them, the save's own plus new
ones) with the desktop's `spawn()` and comparing every cell and row field:
no differences.

## Selecting

Selecting is **a state of Build, not a mode**. With nothing planned, a tap on
an existing entity selects it, whatever tool is held (or none); a tap on
empty board is the held tool's (a run, a ghost). While anything is
selected or a stamp is being pasted the bar shows the selection's swatches
and the held tool is set aside (`Shell.heldTool` reads null). The moment
nothing is selected and nothing is pasted, the tool is back -- so with the
belt held, tapping empty board plans a run and tapping a machine selects
it. The tap that deselects the last entity only hands the tool back; it
does not also start a run. A run or ghost already being planned keeps its
taps, even over an entity.

Selecting lifts entities **off the board**. A selected entity is deleted
from the board and held in `sel`, a stamp at a pose; it draws as a ghost
outlined in cyan. Deselecting places it back wherever the selection now is.

| input | does |
|---|---|
| tap a board entity | select it |
| tap a selected entity | deselect it, if it fits where it now is |
| tap empty board | deselect everything that fits |
| one-finger drag from a selected entity | move the whole selection |
| one-finger drag elsewhere | pan |
| Box, then a drag (from anywhere) | a rectangle: everything it touches is **added**, then Box disarms |
| Turn buttons | turn / mirror the selection as one stamp |
| Delete | discard the selection |
| long press a belt or underground | select its whole run; if the whole run is already selected, deselect it |
| Cut | the selection becomes a stamp, and pasting starts with the ghost **exactly where the selection was** |
| Copy | the selection becomes a stamp and goes back down; if all of it fit, pasting starts with the ghost over the originals |
| a stamp swatch | paste it (tap it again to stop) |
| long press a stamp swatch | rename it, or delete it from the history |

- **Deselecting is "if able", per entity.** Anything that does not fit
  (moved onto something, say) stays selected, tinted red where it clashes.
  So a deselect-all can leave a few behind, and Copy then does not start
  pasting, leaving the player to deal with them.
- **The Box only adds.** It appears only once something is selected, so a
  box select is: tap one thing that belongs in it, then Box and drag. It
  first toggled (dropping what was selected in the rectangle), which made
  growing a selection by overlapping boxes undo itself.
- **The pose is a continuous centre point**, like a placement ghost, so four
  turns come back exactly where they started. It is re-centred on the
  selection's box whenever the membership changes.
- **Pasting never coexists with a selection.** Entering a paste puts the
  selection down and deletes whatever of it could not go down.
- **Leaving Build (for Hand) puts the selection down.** What does not fit
  stays lifted and is still there on coming back.
- **Cut is a move you can carry off or cancel.** Its ghost uses the
  selection's own pose, so the stamp lands cell for cell where the
  entities were: tap the ghost to put them back, drag it and tap to move
  them, or Cancel to delete them.
- **Long press traces the run over the board and the selection together**
  (`segtrace.js`), so a run that is partly lifted is still one run; it
  lifts whatever of the run is still on the board, and only when all of it
  is lifted does it put the run down. It stops where a desktop segment
  stops -- at a machine, at the loser of a merge, at an unpaired
  entrance -- and passes through underground pairs.
  `tests/segtrace.test.mjs` checks it against `core/segments.py` on every
  belt cell of `cpu.npz`, and was mutation-tested (dropping either the
  merge check or the underground jump fails it). Long press works in Build
  with nothing planned, and while selecting; elsewhere it is an ordinary
  press. A long press on anything but a belt is an ordinary press too, so
  it ends as a tap.
- **Underground pairs.** Lifting one end of a pair releases the other
  (`Engine.unpair_under()`). Both ends lifted stay paired to each other.
  Placing back pairs the stamp's own ends first, then each unpaired end
  with its nearest candidate, the order `Engine.commit_ghosts()` uses.

Pasting is the machine-placement flow with a stamp: tap for a ghost, drag
it, tap the ghost to place and keep pasting, tap elsewhere to place and
stop. It places **partially** (whatever fits, the desktop's shift-click),
not all-or-nothing. The bar shows Cancel and the two rotations.

A stamp from the history can be **baked into a toolbar slot** from the
slot picker (long press), which lists the history after the machines. It
then places in Build with the same flow, and its Turn buttons turn its own
stamp. The stamp is stored in the slot, so it survives a reload; the
history itself is in-session.

[stamp.js](stamp.js) is the geometry, a port of `core/stamp.py`
(`rotated()`, `mirrored()`), **tested against the Python methods
themselves** on random stamps of every type and orientation.
`Board.capture()` / `eraseRows()` / `placeEntries()` port the clipboard's
capture, `Engine.erase()` and the paste commit. Checked by box-selecting
147 entities of `cpu.npz` (with 4 underground pairs inside) and putting them
back: the desktop's `SegmentIndex` found the same entities, the same 703
segment lengths and the same 334 paired underground ends, with a clean
audit; turned 180° and put back, still a clean audit.

### Auto-undergrounds

A run that crosses occupied cells tunnels under them **when the geometry
allows**, because placing underground ends by hand is clumsy on a phone.
Otherwise blocked cells are skipped and tinted red, as on the desktop, and
nothing is ever overwritten. A bridge from entrance `a` to exit `e` needs:

- `a` and `e` on the run and free: the cells either side of a blocked
  stretch, so an obstacle touching either end of the run is never bridged;
- **every step from `a` to `e` in one direction**: the pair is a straight
  span;
- **the exit's own facing in that direction too.** An exit only surfaces
  forward, so a run that turns on the cell straight after the crossing
  cannot be bridged. This includes the tip's turn when the exit is the tip;
- at most `Underground.MAX_GAP` (4) cells between them, exported in
  `types.json` rather than copied.

**Two obstacles one free cell apart are one obstacle.** Bridging them
separately would need the first exit and the second entrance on that same
cell, so they are bridged whole or not at all. Two free cells between them
leave room for exit-then-entrance, and each is decided on its own.

An entrance's input side is **not** constrained: an underground takes input
from any side, as a belt does (`_incoming()` in core/segments.py).

`tests/planner.test.mjs` pins each rule, and each was mutation-tested.
Removing the straightness check at first passed everything, because every
bent fixture also had its exit turned, so the exit rule refused it first. A
staircase (Line route) bending inside the span with both ends facing east is
the case that isolates it.

### Placing is a port, and was checked against the desktop

`Board.spawn1()`, `pairUnder()` and `buildPlan()` port `_claim_row()` (free
list first, popped from the end; row 0 is never handed out), `spawn()`,
`_stamp_footprint()` and `pair_under()`. A built board was copied into a
Python `Gameboard`: rows and cells agreed, the free list held only dead rows,
`SegmentIndex.audit()` reported nothing, and the crossing run came out as
one segment through its underground pair.

`buildPlan()` re-checks `isFree()` per cell and skips anything that has
become occupied, so a stale plan can never overwrite. It pairs only ends
that were both actually built, the dangling-partner rule the desktop
enforces.

### A testing trap: instant drags read as flings

A test drag sent as a burst of moves with no time between them looked like a
**fling** to Chrome, and the next tap anywhere was spent stopping it: the
button got `pointerdown` and `pointerup` and never a `click`. It looked
exactly like a dead button. Space the moves about 16 ms apart and pause
before lifting. The controls also carry `touch-action: manipulation`, which
is harmless and removes double-tap arbitration.

## Rules

- **No build step, no npm, no framework.** Plain ES modules and Canvas 2D.
  The point is to iterate cheaply.
- **Hold the save's own arrays as the model.** `Board` keeps the int32 grid
  and float32 entity table exactly as the file has them, so writing a save
  is a dump, and the Python app and this one read each other's files.
  Anything else (`belts`, `structs`, `unders`, `portOut`) is derived by
  `Board.derive()` and must be rebuilt after an edit, never edited itself.
- **Never hand-copy machine definitions.** `types.json` is generated by
  `tools/export_web_types.py` from `core/cell.py` and `core/entity.py`,
  including port faces **already placed** for each (direction, flip). Re-run
  it after changing either file; the Pages workflow regenerates it anyway.
- **Agree with the simulation about topology.** `Board.incoming()` mirrors
  `take_dir()` in `core/segments.py` (straight-through first, then CCW; the
  cell a belt outputs into is never its source). If the two disagree, the
  picture shows material going where it does not go.
- **Redraw on demand.** Nothing runs in a loop; input calls `redraw()`, which
  coalesces into one `requestAnimationFrame`. Every draw pass is one batched
  path per family, culled to the viewport, with detail gated on zoom
  (`LOD` in `render.js`).
- When the tick arrives: the closed-form belt transport and the simple gate
  prefilter are the only optimisations wanted. No wake lists, no folding, no
  GPU.

## Files

| file | role |
|---|---|
| `index.html`, `style.css` | the shell: board, controls row, swatch bar, picker sheet |
| `main.js` | wiring: applies the shell's and panel's requests to the board; resize, redraw |
| `ui.js` | the shell's state and DOM: modes, sim and turn buttons, thumb pads, swatches, long press, picker |
| `tools.js` | the tool catalog (from `types.json`) and the swatch drawings |
| `icons.js` | line icons as path data (mostly Lucide, ISC) |
| `panel.js` | the Tweakpane panel: saves, View, Debug, Status |
| `vendor/` | Tweakpane 4.0.5, vendored |
| `npz.js` | `.npz` reader: zip directory + native `DecompressionStream` + `.npy` headers |
| `board.js` | the save's arrays, validation, derived render lists, `entityAt`/`describe`, placing, erasing, capturing, and the two setting writers |
| `camera.js` | camera (world +Y up, zoom in CSS px/cell), pointer gestures, and taps |
| `render.js` | Canvas 2D drawing under a world transform; labels in screen space |
| `planner.js` | belt-run geometry: paths, facings, the tip, auto-undergrounds |
| `stamp.js` | stamps: rotate and mirror (port of `core/stamp.py`), cells, subsets |
| `segtrace.js` | the belt run through a cell, as `core/segments.py` would cut it |
| `ghost.js` | a stamp at a board position, in the renderer's shapes |
| `placer.js` | where a ghost's footprint sits: the port of `origin_for_cursor()` |
| `tests/` | `node --test` suites for the pure modules |
| `types.json` | generated — do not edit |
| `build.sh` | assembles `_site/` |

The camera's one primitive is `move(from, to, scale)`: the world point under
screen point `from` ends up under `to`. Drag, pinch and wheel are all that
call, so they cannot disagree. Gestures compare the first two pointers
before and after each move rather than against a stored baseline, so a
finger landing or lifting mid-gesture never makes the view jump.

`window.gates` exposes the camera, board, `redraw()` and the last render
time, for the console and headless tests.

## Testing

```bash
node --test web/tests/*.test.mjs
```

Pure-JS tests (the planner, the placer, the stamp transforms, the run
tracer) run in Node, with no npm and no browser.

Playwright with phone emulation (`devices['Pixel 7']`) and CDP
`Input.dispatchTouchEvent` drives real multi-touch. Check counts against
Python (`entities[:,0] != 0`, grouped by type) to confirm a save parsed.
Measured on `cpu.npz` (4.9k entities) with the CPU throttled 4×: about 10 ms
a frame with the whole board visible, and 2–5 ms zoomed in.
