# web/ — the mobile board editor

A phone-first web interface for building and editing Gates saves (`.npz`).
The Python app is the high-performance simulator (shader tick, wake lists,
folding); this is where the *building* UX gets designed: structure
placement, belt routing, copy/paste, move/rotate/flip, seeds and Source
values, camera, toolbelt. It also runs the simulation, as a plain port of
the desktop's **CPU** tick (see "The simulation" below). Read the root
CLAUDE.md for what the game *is*; most of its engineering notes are about
the schedulers and the GPU, which do not apply here.

Current state: **the phone shell, the belt planner, machine placement,
per-instance settings, select / cut / copy / paste, and the simulation.** It
loads a save, pans (one finger) and zooms (pinch, or the wheel), lays belt
runs (auto-tunnelling under obstacles), places machines and raw belts, edits
a Source's emit value or a belt's seed, selects, moves, turns, deletes,
copies and pastes groups of entities, and ticks the board -- items on belts,
gates firing -- at a chosen rate.
Boards save to the browser and export as `.npz` files the desktop loads
(see "Saving" below). Underground has no tool flow of its own (the planner
covers it).

## Run it

```bash
bash web/build.sh && python3 -m http.server -d _site 8000
```

`build.sh` copies the app and every root `.npz` into `_site/`, writing
`saves/index.json` for the save picker. On push to `Web`,
`.github/workflows/pages.yml` does the same and deploys to GitHub Pages.
`default.npz` is loaded at startup if it exists.

## Installing it, and offline

The site is a **PWA**: `manifest.webmanifest` (name, standalone display,
icons) and `sw.js`, a service worker that caches **every file of the
site** -- the app, `types.json`, Tweakpane, the icons and every bundled save
-- on first load and serves from that cache first. So once opened online,
it installs (Chrome/Android: the install prompt or menu item; iOS Safari:
Share, Add to Home Screen) and runs with no network at all.

- **`build.sh` stamps the worker**: the precache list (every file in
  `_site/`) and a version hashed from all their contents. A deploy that
  changes anything changes `sw.js`, so the browser installs the new worker,
  which caches the new set and deletes the old. It takes over at once, but
  the open page keeps the code it loaded: **an update shows on the next
  launch**, never mid-session. Served unbuilt, the worker caches nothing.
- **Local dev**: after a rebuild, the first reload installs the new worker
  and the second shows the change.
- **Browser saves live with the installed app's storage.** On iOS a
  home-screen app has its own storage, separate from Safari's, so boards
  saved in Safari do not appear in the installed app -- export and open the
  `.npz` instead.
- The icons are drawn by `tools/make_web_icons.py` (committed PNGs; rerun
  by hand to change them). The maskable one keeps its art inside the
  central circle launchers crop to.

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
    emit value or seed; on a belt, tapping the lit number unseeds it. A
    Source also gets a **text box** after the swatches for any other value
    (see "Placing machines and raw belts").
- **Sim** (right): Reset (the desktop's ctrl+R: tick 0, every item gone,
  seeds back on, the factory untouched) and Play/Pause. Space toggles
  play. It **starts paused**, and loading a save pauses it again. **Long
  press Play/Pause** (right-click on a mouse) for the rate panel, just above
  the Sim section: presets of 1, 5, 10 and 30 ticks a second, and Custom,
  which opens a text box (0.1 to 240). It stays open until the next long
  press or its × button, and shows the tick count. The rate persists with
  the shell's localStorage; the panel's open state does not.
- **Edit**: Undo, Redo and Checkpoint (ctrl+Z, ctrl+shift+Z / ctrl+Y on a
  keyboard). See "Undo" below. Turning lives on the **thumb pads** in the
  board's corners, which show whenever there is something to turn: a held
  tool, a run, a ghost or a selection. Rotate CW, CCW, and flip act on what
  the hand holds: the held tool in Build (a stamp tool turns its stamp),
  the stamp being pasted, or the selection. A swatch draws its tool **at
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

### Undo

[history.js](history.js) keeps **states, not edits**: each entry is a whole
`Board.snapshot()` (grid, entity table, row allocator -- about 5 MB on a
512×512 board), so restoring cannot half-apply anything and no edit needs an
inverse. It keeps `UNDO_DEPTH` (3) steps behind the current state, one
constant.

- **The checkpoint** is one more state, just before the oldest: undo from
  the oldest step restores it, and redo walks forward through the history
  again from there. It is set when a board loads and whenever Checkpoint is
  pressed (which puts a selection down first). An edit made while on the
  checkpoint starts the history afresh from it.
- **Only stable states are recorded**: nothing lifted. A selection's
  entities are off the board, so select, move, put down is **one** step,
  committed when the selection ends -- and Undo while something is lifted
  throws that change away, back to the current state. `recordStep()` runs
  every frame and commits when the board's revision moved, so everything
  one gesture does (force build clearing the way, then building) is one
  step.
- Restoring drops whatever was in progress (a run, a ghost, a selection)
  and is an edit like any other, so the simulation starts fresh.
- `tests/history.test.mjs` covers the walk, the depth, the checkpoint
  landing and the truncation after an undo.

### The Dev folder

Every number tuned by eye, and one behaviour switch, as dials in the
panel's **Dev** folder ([tune.js](tune.js)): belt width, chevron size,
overlay width and edge, port size and edge, glyph size and weight, the
Source value's size, grid opacity, ghost opacity, and the tap margin round
a ghost or a run's tip. Scales multiply the shipped value, so 1 is always
"as shipped", and **Reset dials** puts them all back. They persist with the
panel's preferences; a stored key this build does not know is ignored.

**Force build** makes the builder tools win: a blocked machine or raw belt
deletes whatever is in its way and is placed, and a belt run deletes what
occupies its **blocked** cells (not the ones it tunnels under, which it
never wanted) and is built in full. Whole entities go -- one cell of a
machine takes the machine -- through the same erase as everywhere else.
Stamps are unaffected; a paste already places whatever fits.

## The belt planner

[planner.js](planner.js) is the geometry, as pure functions over a
`free(x, y)` predicate; `main.js` owns the gesture and the run state.

The gesture, with the Belt tool held:

| input | idle | planning |
|---|---|---|
| tap | start a run on that cell, **including a placed belt** (see below) | **on the tip** (or within 20 px): turn it, cycling straight, right, left -- or, on a one-cell run started on a belt, select that belt |
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
- **A run may start on a placed belt, and turns it.** With the planner
  held, a tap on a belt (not an underground end, not a machine) starts a
  run there instead of selecting it, and Build turns that belt to the run's
  first step. So successive runs chain: stop one short, start the next on
  its last belt, and that belt is re-aimed -- no need to aim a run's tip
  before confirming. The turn is **in place** (`Board.turnBelt()`, the
  desktop's empty-hand R): the row and its seed survive. The start cell
  counts as free to the planner, and while planning the belt draws only as
  the ghost, at its new facing. A one-cell run starts at the belt's own
  facing, so Build with nothing turned changes nothing, and the tip buttons
  re-aim the belt. If the planner tunnels from the very first cell (an
  obstacle right after it), the belt is replaced by the entrance, seed and
  all. The **tip** is unchanged: it still cannot land on anything. A long
  press still selects the belt's run.
- **Tapping a one-cell run started on a belt selects that belt** instead of
  turning the tip: the run is cancelled and the belt lifted, since a tap on
  a belt you just tapped means the belt. The rotate buttons and thumb pads
  still re-aim it, and a tap elsewhere still builds, so a one-cell re-aim
  is still there -- just not by tapping the tip.
- **The route switches itself when only the other one gets through.**
  `chooseRoute()` plans the player's elbow first; if it leaves a cell
  blocked and the other elbow does not, the other is used. A tunnel counts
  as getting through, and **a blocked last cell is not counted**: dragging
  the tip up onto the machine the run feeds is the common case, and it
  must not swing the route. When both get through, or neither does, the
  player's route stands. It is decided per plan and never stored: the
  Route button names the route in use, **dimmed** when it was forced, and a
  press still flips the stored preference, which shows again as soon as
  both routes work.

**Thumb pads.** While anything is planned (a run, a placement, a paste), a
tool is held, or a selection is held, the Turn buttons appear in both bottom corners of the board -- rotate
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

### Vectors

[vector.js](vector.js) is the core, pure: offsets, the vector's turns, a
**claims** overlay and the chain follow for vectored selection. With the
vector on, whatever is built repeats at `k * v` for `k = 0 .. count-1`.

**The picker.** Tap **Build while already in Build**: the hammer gets an
arrow across it and the bar becomes Cancel / Reset / Less / More (× count).
The thumb pads turn the vector instead of the tool -- rotate a quarter, flip
negates x -- and gain a − / + row. The first drag after opening sets the
**anchor** where it starts (highlighted, with every copy's cell outlined
along the vector); each drag moves the offset by the finger's motion, so
the offset never jumps under the thumb. Tapping Build again, or tapping the
board, confirms: the picker closes and Build stays **tinted** (vectored
building is on). Cancel closes it and turns vectored building off. The
vector starts at (0, 0), which makes no copies; count starts at 2 and
doubles up to 256 (`VEC_MAX`). Not persisted.

**Copies are laid in sequence** (`layCopies`): copy `k` sees copies
`0..k-1` as occupied, through the claims overlay over the board's own
`isFree`, so they never land on each other. The original is always kept
(red where blocked, as without a vector); a later copy is kept only if it
fits **whole** -- a stamp copy that would only partly fit is dropped, not
half-shown. Everything a placement shows reads the tool, so turning the
tool re-lays the copies at once (a 2-wide Shear on a (1, 0) vector of 8
ghosts as 4; turned 1×2, all 8). A tap on any copy places them all, in
order, each re-checked against the board as the ones before it left it,
with **one** re-derive for the lot; force build clears the way for the
original only.

**Belt runs.** A run begun with the vector on remembers it (`run.v0`):
copy `k` starts at `start + k * v0` and ends at `end + k * v` with `v` the
vector **now**. So a run's copies are a bus whose starts stay put while
re-picking the vector mid-run swings their ends -- a bus laid east, its
vector turned to point down its own column, fans out as nested corners;
turned the other way it twists, and the copies that cannot get through
drop out. (A run begun without a vector takes the current one for its
starts too.) Copies are planned in sequence over the claims of the ones
before -- belts and underground ends; buried tiles stay free, so a copy may
tunnel under an earlier one -- and a copy that does not get through
(`routeWorks`, the blocked last cell excused) is dropped; copy 0 always
shows, ringed, red where blocked. A copy starting on a placed belt turns it.

**One route for the whole bus, chosen for the bus**: the player's, unless
the other gets strictly more copies through. Choosing for copy 0 alone was
the first version and it was wrong in the common case: a bus whose starts
sit in one row can never go along that row first, since every copy's start
is on copy 0's first leg -- copy 0's route doomed all the others. The bus is
cached on the board revision, the run and the vector, since it is two full
plannings of up to 256 runs.

**Vectored selection** is separate from vectored building: the selection
bar's **Vector** swatch opens the same picker in its "select" form, with
its own offset (`vec.sv`, so it never disturbs the building vector). The
anchor is a cell of the first selected entity, the offset starts on it,
and the bar is Cancel / Reset / Confirm (no count: this vector runs until
the pattern breaks). On Confirm -- or a tap on the board -- the selection
as it now sits is the **pattern**, and `followChain()` steps along `v`:
if the board holds the whole pattern again at `k * v` (`matchRow()`: same
type, origin, facing, flip and underground role; a Source's value, a seed
and pairing **ignored**, or indexing Sources that hold different numbers
would stop at the first), those entities are added; the first `k` where
any is missing ends it. They are added chain step by chain step, the
pattern's own order within a step -- a selection order, which indexing
uses.

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
- **A Source's value is not restored on reload.** It carries from one
  placement to the next within a session, but `freshValue()` drops it when
  the bar is read back from localStorage, so every session's Source starts
  at 0 (`Source.EMIT_VALUE`). Restoring it made a value picked days ago
  read as "the default is 1". The raw belt's seed is still restored.
- **A Source has a custom-value box** after its swatches, here and in the
  Hand's bar. It shows (and is lit for) a value the swatches do not offer,
  and applies on Enter or when it loses focus -- never per keystroke, since
  the bar re-renders on a change and would take the field away mid-number.
  The value is stored as the float32 the row will hold, so the box and the
  board agree. It uses the plain keyboard because iOS's decimal pad has no
  minus sign.
- **Re-picking the value a Source already holds is not an edit.** Any edit
  restarts the simulation (see "The simulation"), so the Hand's swatches and
  box skip a write that would change nothing.

Underground ends draw **narrowing toward the buried side** (the front of an
entrance, the back of an exit), on the board and on the swatch.

### How machines draw

As the desktop draws them, because a design reads by it: the body, the
**control overlay** -- a faint belt straight through each cell a channel
passes unchanged (`Entity.PASS_THROUGH`) -- then the type's **glyph**
(Shear's +, Mac's ×, Swap's Z, Floor's arrow...), then small port arrows.
There are no type names; a Source shows its value instead of its dot, at
every zoom the glyphs draw at (it was first cut off at legibility, which
turned values into dots too early to be useful for finding a constant).

The overlay is **0.28 cells wide and opaque, with a thin dark edge** -- between
the desktop's faint 0.22 and a belt's 0.34 -- so it reads as a belt
running through the machine. The edge scales with the board
(0.03 cells, at least half a pixel) so zoomed out it thins rather than
striping every machine.

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
| Vector, drag the offset, Confirm | add every repeat of the selection along the vector, until it breaks (see "Vectors") |
| Turn buttons | turn / mirror the selection as one stamp |
| Delete | discard the selection |
| long press a belt or underground | select its whole run; if the whole run is already selected, deselect it |
| long press anywhere else, then drag | a box from the press point, as Box does: everything it touches is **added**. The box corner shows the moment the hold registers; lifting without dragging selects nothing |
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
  press.
- **A long press anywhere but a belt arms a box** at the press, so a box
  select needs no Box button and no first entity. `onLongPress` answers
  `"drag"` and `camera.js` hands the finger's moves and lift to the drag
  hooks, the same path the Box button's drag takes. The cost is that a
  still hold off a belt no longer ends as a tap (it used to start a run or
  a placement there); it does nothing. A hold on a **selected** entity is
  the exception and stays a press, so a slow drag from it still moves the
  selection.
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

The same file pins the route choice: kept while it gets through (tunnels
included), switched and flagged when only the other does, kept when neither
does, and a blocked last cell -- only the last -- excused.

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

## The simulation

[sim.js](sim.js) is a port of the desktop's **CPU** tick --
`core/segments.py`, `core/belts.py`, `core/gates.py`, `core/tick.py` and the
tick half of `engine.py` -- with every scheduler left out: no wake lists, no
gate sleep, no constant folding, no GPU. The two optimisations kept are the
closed-form belt (a run ticks in O(1) however many items are on it) and the
gate prefilter (an occupancy test rejects gates that cannot act before the
full firing rule runs). One module, no DOM, so Node can run it.

**It matches the desktop tick for tick**, and
[tools/check_web_sim.py](../tools/check_web_sim.py) is what says so: it runs
the desktop's `Engine` (folding off, both sleep layers latched off, no GL)
and `web/tests/sim_dump.mjs` on the same save, and compares every segment's
id, ring counters, anchor, every item's entry tick and payload bits, and
every gate's eight channel slots, after every tick. Every root save agrees
for 200 ticks with a Reset halfway. Mutation-tested: numbering segments
fresh on each rebuild fails just after the reset, and letting Merge consume
both lanes fails on `cpu.npz` at tick 23. Two mutations it does **not**
catch, both of them behaviour-neutral on today's boards: pushing before the
pop (at spacing == speed the room test cannot see the pop), and applying
direct hand-offs one at a time instead of from a snapshot (they differ only
when two gates feed one Sink lane).

Matching forced three details that would otherwise look arbitrary:

- **Segment ids come from the desktop's allocator**: a free list popped from
  the end and refilled in creation order on every rebuild. The belt phase
  visits runs in id order, and a run that drains a gate first changes what a
  later run sees, so the ids decide the result -- and they alternate between
  ascending and descending scan order on each rebuild, exactly as the
  desktop's do. A new `Sim` per loaded board, like a fresh desktop session.
- **The rebuild scans y-major** (numpy's `nonzero()` order), so the same
  cells become tails in the same order.
- **Values are float32 at the same points**: gate channels live in the
  save's own entity `Float32Array`, and ring payloads are `Float32Array`s.

**The arithmetic is data.** `types.json` now carries a `sim` record per
machine -- firing rule, port-to-channel maps, the `OPS` opcode table and
Merge's `SELECT_RULE` -- and `evalOps()` is a port of `Entity.eval_ops()`.
Nothing about a machine's semantics is written in JS; the desktop's
self-check already pins `OPS` against every `fire()`.

**Any edit is a fresh start.** `Board.derive()` ends every mutation, so it
bumps `board.revision`; `Sim.sync()` (called at the top of every frame and
every tick) sees it and rebuilds the index, flushes every item from every
belt and every gate channel, and puts the seeds back on. That is the
desktop's rule for a belt-network edit applied to *every* edit, including a
Source's value, a seed, and lifting a selection off the board. The tick
counter is left alone; Reset is what zeroes it.

**The clock banks fractional ticks, not seconds.** `Clock.advance(dt)`
adds `dt * rate` and spends the whole ticks; the remainder is `alpha`,
which the renderer feeds to the closed form so items glide between ticks.
Banking seconds and dividing (the desktop's way) left float residue that
turned a whole tick into 0.9999 of one; the unit test caught it. A frame's
dt is clamped to 0.25 s and the backlog to 240 ticks, so a backgrounded tab
never freezes to catch up. **Rendering loops only while playing**; paused,
the board is still redrawn on demand.

### How items draw

As the desktop's discs -- a dark disc with a ring, **green above zero, red
at or below**, its number on top once the zoom makes it legible (faded in
over the shader's 6-10 px glyph band) -- but **seen all the way through a
machine**, not popping out of existence at a belt's end. Where every item
is drawn is [flow.js](flow.js), a pure module over the sim; `render.js`
only turns its points into discs. Drawn after the machines, so items sit
over the control overlays.

**One path from belt to belt.** A run's head front edge is a machine's
**in-edge**; from there an item goes to its input's **wait point**, then to
an output's **out-edge**, which is the next run's back edge. Its value
switches at the wait point, as if computed there:

- **An input held in a machine sits at its wait point** -- the middle of
  its path when every way out runs straight through (Shear's y waits on the
  boundary between its cells), otherwise the centre of its own cell, so a
  lane that turns or branches (Floor, Route's a, Merge's a and b, Swap's y
  and z) waits before committing to a direction. No two lanes share one.
- **A result held in an output zone sits on the out-edge**, an ordinary
  disc half on the machine and half on the belt. (The desktop's squashed
  waiting-zone disc is gone here.)
- **A run blocked at a full input holds its head on the in-edge.**
- **Which way out** an item takes is read from the machine's own opcodes
  (`sourcesOf()` / `linkFor()`): the output whose value came from this
  input on this fire. `LINKS` only says which input ports can reach which
  output ports, as geometry; Route's choice and Swap's exchange are the
  machine's. Swap's exchange cuts **diagonally** from the cell centre to
  the other cell's port; everything else runs through cell centres. Sink is
  legacy and has no links: what enters it vanishes at the edge. A Source's
  results appear on its out-edge.

**Belts draw half a cell back** (`pos - 0.5`). The closed form's 0 is the
tail cell's centre and its length is one cell past the head's centre, inside
the next machine; shifted, 0 is the tail's back edge (where a push enters)
and the length is the head's front edge (where a blocked head waits), so a
run's ends meet its machines exactly at their ports. The back half of the
tail cell comes in from the feeding port's face. **Corners**: an item is
carried by the cell whose centre is nearest, ahead of the centre along that
cell's facing and behind it along the way the run came in, so it rounds a
corner on the belt. (This was already so here; the desktop's shader draws
along the cell's facing only, which is what makes items there vanish into a
corner and reappear across it -- see `docs/item_flow_spec.md`.)

**The tick is animated from its forecast.** `Sim.forecast()` runs the real
`step()` on the live state with an event log -- pops, fires (consumed,
produced), pushes (and where each pushed item lands), direct hand-offs --
and rolls everything back: the entity table, each run's head/tail/anchor,
the ring slots pushes wrote, the tick, the stats. Not a second copy of the
rules, so it cannot disagree with the tick it predicts. Cached on
`Sim.version` (bumped by every step and rebuild): one extra step per tick
drawn, about 1 ms on `cpu.npz`, not per frame. Over the coming tick, at
`alpha`:

- a head that will pop moves from where it is to the in-edge and on to its
  wait point;
- an input that will be consumed moves from its wait point to its out-edge
  and on -- onto the run that takes the push (as far as the closed form will
  have it next tick), or into the next machine's wait point for a direct
  hand-off; a result for a dangling output stops at the edge and is gone;
- a held output that will be pushed or handed off does the same from the
  edge; a Source's new item starts on the edge (and is not drawn until next
  tick if it will not move, or it would sit on the item just leaving).

Each covers its whole path at a constant speed. **Items speed up through
machines**, and that is the tick, not the drawing: a hop costs one tick of
latency, and on a saturated run the head pops while still a cell short
(the step advances the anchor before testing the pop), so the last belt
cell plus the first half of the machine, then the second half plus the
first belt cell, each pass in one tick.

Two overlaps are real states, not drawing bugs: a packed run's newest item
at position 0 and a result waiting in the zone behind it are both "on the
out-edge" for an instant; and a lane that turns along another's centre
line (Route's a to the right output, Merge's a) crosses that lane's discs
mid-tick.

`tests/flow.test.mjs` pins it: every fire sends each consumed input down
exactly one link and reaches each result exactly once (all machines, 200
value sets); paths start on the in-edge, pass the wait point, end on the
out-edge, and stay in the body at every orientation; the forecast equals
the step it predicts and leaves no trace; a run is one unbroken path whose
ends sit on its machines' port edges; and **the picture is continuous**:
on six saves over 80 ticks, everything drawn at alpha 1 is drawn at the
same place at the next tick's alpha 0. Mutation-tested: dropping the half
shift, the push or hand-off continuation, the pop's start position, the
corner rule or the tail's port direction each fails it.

**Status** shows a live readout under the settings: a machine's input and
output channels (`–` empty, `×` an output with nothing to take it, whose
results are discarded), or a belt's run (its id, length, whether it is fed
and drains, and the items on it, head first). Rewritten every frame; the
panel touches the DOM only when the text changes.

## Saving

The panel's **Board** folder saves the board **to this browser** under a
name (the loaded board's, until edited), and keeps a list of those saves:
pick one, then **Load saved**, **Export .npz** (a download) or **Delete
saved**. Picking alone does nothing, so choosing what to export never
replaces the board in front of you. Saving over an existing name asks first.

- **A save is a real `.npz`**, written by `Board.toNpz()` in the layout
  `core/persistence.py save()` uses: the same keys, `.w` zeroed (derived),
  `next_row` / `free_rows` / `live_count` from the board's allocator, and
  the annotations blob carried through untouched (this app does not draw
  notes, but a save that passes through it must not lose them; a save that
  predates notes gets `"[]"`, as the desktop writes). `npz.js` has the
  writer: `.npy` headers, deflate through the native `CompressionStream`,
  and a CRC-32 per member, which numpy checks.
- **localStorage, base64, one key per save** plus an index key, in
  [saves.js](saves.js). The quota is about 5 MB, and a deflated 512×512
  board is tens of kilobytes (110 KB for `cpu.npz`), so dozens fit. A full
  store is reported, not swallowed, and a failed write is not left listed.
- **A lifted selection is put down before saving**, since lifted entities
  are off the board. Anything that still does not fit is asked about rather
  than silently left out of the file.
- The simulation's items are **not** saved, as on the desktop: a loaded
  board starts paused at tick 0 with its seeds on.

`tests/npz.test.mjs` writes `cpu.npz` and `ram.npz` back out after an edit,
reads them here, and loads them through the desktop's `persistence.load()`,
asserting every array matches and exactly the edited value differs.

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
- **Redraw on demand while paused, loop while playing.** Input calls
  `redraw()`, which coalesces into one `requestAnimationFrame`; the only
  continuous frames are the ones advancing the clock. Every draw pass is one
  batched path per family, culled to the viewport, with detail gated on zoom
  (`LOD` in `render.js`).
- **The tick stays the desktop's CPU tick.** The closed-form belt transport
  and the simple gate prefilter are the only optimisations wanted. No wake
  lists, no folding, no GPU. A change to the desktop's tick semantics needs
  the same change in `sim.js`, and `tools/check_web_sim.py` must still pass.

## Files

| file | role |
|---|---|
| `index.html`, `style.css` | the shell: board, controls row, swatch bar, picker sheet |
| `main.js` | wiring: applies the shell's and panel's requests to the board; resize, redraw, the play loop |
| `sim.js` | the simulation: segment index, closed-form belts, gates, the clock -- the desktop's CPU tick; `forecast()` |
| `flow.js` | where items are drawn: belt paths, the machines' internal links and wait points, the tick animated from the forecast |
| `ui.js` | the shell's state and DOM: modes, sim and turn buttons, thumb pads, swatches, long press, picker |
| `tools.js` | the tool catalog (from `types.json`) and the swatch drawings |
| `icons.js` | line icons as path data (mostly Lucide, ISC) |
| `panel.js` | the Tweakpane panel: bundled and browser saves, export, View, Debug, Status (with the live channel readout) |
| `vendor/` | Tweakpane 4.0.5, vendored |
| `npz.js` | `.npz` reader and writer: zip directory + native `DecompressionStream` / `CompressionStream` + `.npy` headers |
| `saves.js` | browser saves: whole `.npz` files in localStorage, and their index |
| `board.js` | the save's arrays, validation, derived render lists, `entityAt`/`describe`, placing, erasing, capturing, and the two setting writers |
| `camera.js` | camera (world +Y up, zoom in CSS px/cell), pointer gestures, and taps |
| `render.js` | Canvas 2D drawing under a world transform; labels in screen space |
| `planner.js` | belt-run geometry: paths, facings, the tip, auto-undergrounds |
| `stamp.js` | stamps: rotate and mirror (port of `core/stamp.py`), cells, subsets |
| `segtrace.js` | the belt run through a cell, as `core/segments.py` would cut it |
| `ghost.js` | a stamp at a board position, in the renderer's shapes |
| `placer.js` | where a ghost's footprint sits: the port of `origin_for_cursor()` |
| `glyphs.js` | the machine glyphs, ports of the shader's `sd_*` marks |
| `tune.js` | the Dev dials: defaults, panel spec, the live values |
| `history.js` | undo, redo and the checkpoint, over board snapshots |
| `vector.js` | vectors: offsets, turns, the claims overlay, laying copies, following a chain |
| `tests/` | `node --test` suites for the pure modules |
| `types.json` | generated — do not edit |
| `build.sh` | assembles `_site/`, and stamps `sw.js` with its file list and version |
| `sw.js`, `manifest.webmanifest`, `icons/` | the install and offline layer (see "Installing it, and offline") |

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
tracer, the closed form against the naive oracle, the opcodes, the clock,
the `.npz` writer) run in Node, with no npm and no browser. The whole tick against the
desktop's is a separate, slower check (about 25 s for every save):

```bash
python tools/check_web_sim.py              # every root .npz, 200 ticks
python tools/check_web_sim.py cpu.npz 500  # one save, longer
```

`segtrace.test.mjs` imports the Python app, and `core/tick_profile.py`'s
import-time self-check is timing-based: run alongside the other suites it
occasionally fails that import under load. Rerun the file alone.

Playwright with phone emulation (`devices['Pixel 7']`) and CDP
`Input.dispatchTouchEvent` drives real multi-touch. Check counts against
Python (`entities[:,0] != 0`, grouped by type) to confirm a save parsed.
Measured on `cpu.npz` (4.9k entities) with the CPU throttled 4×: about 10 ms
a frame with the whole board visible, and 2–5 ms zoomed in.
