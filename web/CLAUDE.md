# web/ — the mobile board editor

A phone-first web interface for building and editing Gates saves (`.npz`).
It is **not** a port of the Python app. The Python app is the
high-performance simulator (shader tick, wake lists, folding); this is where
the *building* UX gets designed: structure placement, belt routing,
copy/paste, move/rotate/flip, seeds and Source values, camera, toolbelt.
Read the root CLAUDE.md for what the game *is*; most of its engineering
notes are about the tick and do not apply here.

Current state: **the phone shell, the belt planner, and per-instance
settings.** It loads a save, pans (one finger) and zooms (pinch, or the
wheel), lays belt runs (auto-tunnelling under obstacles), and edits a
Source's emit value or a belt's seed from the Status panel. Machines are not
placeable yet, nothing ticks, and edits are not saved back to a file.

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

- **Mode** (left): Build, Hand, Select. It decides what the swatch bar shows.
  - **Build**: the toolbelt. Tap selects, tapping the held slot again puts it
    down, **long press** (right-click on a mouse) opens the picker for that
    slot, and the `+` swatch appends. Slots persist in localStorage.
  - **Select**: Delete, Cut, Copy, Paste, then the stamp history, most recent
    first. Tapping a stamp means "paste this one"; Paste alone takes the most
    recent. No selection rectangle exists yet, so stamps come only from
    the panel's Debug folder.
  - **Hand**: the bar is empty (deferred). Tapping the board inspects: the
    Status folder opens in the panel, and a tap on bare board closes it.
- **Sim** (right): Reset (ctrl+R) and Play/Pause. Neither is wired to a
  simulation; Play/Pause only swaps its icon. Space toggles it.
- **Turn**: rotate CW, CCW, and flip. They act on what the hand holds: the
  held tool in Build, the pasted stamp in Select. A swatch draws its tool **at
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
| tap | start a run on that cell | **build** the run exactly as shown |
| one-finger drag | pan | move the run's **nearer end** |
| two fingers | pan / zoom | pan / zoom, always |
| rotate CW / CCW | turn the swatch | turn the **tip** |
| flip | -- | nothing |
| the bar | swatches | Cancel / Route / Build |

Four rules, each chosen over an obvious alternative:

- **The end starts on the start, and a tie goes to the end**, so tap-then-
  drag always pulls the tip off the start rather than dragging a one-cell
  run around. Nearness is measured in screen space, from where the drag
  began.
- **A dragged end follows the finger's motion, not its position.** It never
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

A second finger mid-drag ends the drag where it is and becomes a pinch. If
it then drops back to one finger, that finger does nothing (`dead` in
`camera.js`), rather than resuming a drag from wherever it happens to be.

The route (H-first, V-first, line) is stored on the tool slot, so it
persists like the desktop builder's mode.

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
| `ui.js` | the shell's state and DOM: modes, sim and turn buttons, swatches, long press, picker |
| `tools.js` | the tool catalog (from `types.json`) and the swatch drawings |
| `icons.js` | line icons as path data (mostly Lucide, ISC) |
| `panel.js` | the Tweakpane panel: saves, View, Debug, Status |
| `vendor/` | Tweakpane 4.0.5, vendored |
| `npz.js` | `.npz` reader: zip directory + native `DecompressionStream` + `.npy` headers |
| `board.js` | the save's arrays, validation, derived render lists, `entityAt`/`describe`, and the two setting writers |
| `camera.js` | camera (world +Y up, zoom in CSS px/cell), pointer gestures, and taps |
| `render.js` | Canvas 2D drawing under a world transform; labels in screen space |
| `planner.js` | belt-run geometry: paths, facings, the tip, auto-undergrounds |
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

Pure-JS tests (the planner) run in Node, with no npm and no browser.

Playwright with phone emulation (`devices['Pixel 7']`) and CDP
`Input.dispatchTouchEvent` drives real multi-touch. Check counts against
Python (`entities[:,0] != 0`, grouped by type) to confirm a save parsed.
Measured on `cpu.npz` (4.9k entities) with the CPU throttled 4×: about 10 ms
a frame with the whole board visible, and 2–5 ms zoomed in.
