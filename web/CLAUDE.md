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
    the bar becomes the selection's: Delete, Cut, Copy, then the stamp
    history, most recent first. See "Selecting" below.
  - **Hand**: tapping the board inspects: the Status folder fills in (the
    panel is **not** opened for you -- a tap is often only to reach the
    number swatches), and a tap on bare board clears it. While Status shows a Source or
    a belt, the bar offers the number swatches (0, 1, −1, 2, 4, 8) for its
    emit value or seed; on a belt, tapping the lit number unseeds it. A
    **text box** after the swatches takes any other value, for a Source or
    a belt's seed (see "Placing machines and raw belts"); typing the seed a
    belt already has changes nothing, so it never unseeds.
- **Sim** (right): Reset (the desktop's ctrl+R: tick 0, every item gone,
  seeds back on, the factory untouched) and Play/Pause. Space toggles
  play. It **starts paused**, and loading a save pauses it again. **Long
  press Play/Pause** (right-click on a mouse) for the rate panel, just above
  the Sim section: presets of 1, 5, 30 and 240 ticks a second, and Custom,
  which opens a text box (0.1 to 10000). It stays open until the next long
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
default. It holds the saves, View toggles (grid, ports, source values, seed
tint -- the green/red wash on seeded belts, ghosts included -- labels, pan
momentum, items, belts (placed belts and underground ends; a planned run's
ghost still draws), gates (every machine, Sources included -- for a brief
look at the belts underneath; still there and still tappable, and items
held inside a machine still draw: View > items hides those), sources
(hidden, not removed), **gate status colours** and **belt status
colours**, a **status colours** folder of pickers for both, and an **fps
counter** -- frames and ticks per second actually reached, over
half-second windows, bottom-left of the board), Debug, and
the Status folder. Tweakpane is **vendored** (`vendor/`, MIT) rather than
pulled from a CDN, which keeps the no-build, works-offline property. Status
writes the same row fields the desktop's Status window does, plus the
seed's `.z` bits, so a save edited here reads correctly there.

### Undo

[history.js](history.js) keeps **states, not edits**: each entry is a whole
`Board.snapshot()` (grid, entity table, row allocator), so restoring
cannot half-apply anything and no edit needs an inverse. It keeps
`UNDO_DEPTH` (15) steps behind the current state, one constant.

- **A snapshot is sparse.** A dense copy was about 5 MB on a 512×512 board
  (the 4 MB grid is nearly all zeros, and the entity table is kept at its
  doubled capacity), which was why the depth was 3: 16 of them is 80 MB on
  a phone. The grid is stored as the occupied cells' indices and their four
  words, and the entity table up to its last non-zero value (compared as
  bits, so a trailing `-0` survives). About 1.2 MB on `cpu.npz`, `Ram2.npz`
  and `noram.npz`, and 2-4 ms to take or restore. `Board.toNpz(snap)`
  expands one to write it. `tests/npz.test.mjs` restores one word for word
  and saves it byte for byte as the board it was taken from.
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

### Gate status colours

View > **gate status colours** replaces each machine's body colour with what
it is doing in the state on screen (`gateStatus()` in main.js; glyphs,
ports and values stay, ghosts keep their own colours):

- **yellow, blocked**: an output with somewhere to go is holding a result it
  could not hand on. This wins over starved -- the machine already made
  something that cannot leave, and that downstream jam is the thing to
  chase. Dangling outputs discard their results and never block.
- **red, starved**: it does not have what **its own firing rule** needs
  (`Sim.inputsReady()`), not "an input is empty": a Merge with its control
  and the lane that control selects is not starved, nor is a Sink with any
  lane fed, and a Source (no inputs) never starves.
- **green, running**: neither.

All six status colours (these three and the belts' three below) are the
player's: View > **status colours** has a picker for each -- Tweakpane's
saturation/value square and hue strip -- and **Reset colours**. They persist
with the panel's preferences, as hex (`STATUS_COLOUR_DEFAULTS` in
panel.js); a stored key this build does not know, or a value that is not a
`#rrggbb`, is ignored.

**Belt status colours** do the same for belts, per run (segment), in
`beltStatus()`: **red** empty; **yellow** stalled -- the head item is at the
run's end and will not leave on the coming tick (the forecast has no pop),
so items are stuck on it; **green** carrying and moving. **Stalled but
moving: green** (on by default) keeps a stalled run green while anything on
it still moves -- an item is pushed on this tick, or its newest item is
still closing up (items pack from the head, so if the newest has stopped
they all have: one closed-form comparison per run). So yellow means
static, and a run a gate takes from every other tick does not flicker.
Traced on a sparse run hitting a dead end: yellow at the tick its head
arrives without the check, at the tick the last item packs with it.
Underground ends
take their run's colour too, and the dots over a buried span a lighter wash
of it. The seed tint still draws under.

### Pan momentum

A one-finger pan released while moving keeps gliding, slowing exponentially
(`camera.js`: time constant 325 ms, about iOS's own feel). The release
velocity is measured over the pan's last 90 ms, so a finger that stopped
before lifting does not fling; any touch catches the glide. View > **pan
momentum** turns it off.

### Labels

The desktop's text-box annotations, read from and written back to the
save's `annotations` blob in the desktop's own record
(`{ kind: "text", colour, size, text, rect: [x, y, w, h] }`, world cells,
top-left anchor), so labels made on either side show on the other.
[notes.js](notes.js) is the model; the desktop's freehand strokes are kept
and written back untouched, not drawn.

- **The Label tool** (in the slot picker) takes every tap: on a label it
  opens the editor, anywhere else -- over machines too, labelling them is
  the point -- it makes a new label there, a **default-sized** box with its
  top-left at the tap, and opens the editor. With the tool held every label
  wears a **grip** at its bottom-right corner: a drag from the grip
  resizes (top-left fixed; the grip is sized and hit in screen pixels, and
  is checked before the body so a corner grab never moves the box), a drag
  from anywhere else on the box moves it, and a drag off any label pans. In the **Hand**, a tap on a
  label edits it before the entity under it is inspected: labels draw over
  the board, so they win, as on the desktop.
- **The editor** is a sheet: the text (any length, newlines kept), size
  S / M / L (the desktop's 0.5 / 0.9 / 1.6 cells) and the desktop's six
  colours, live on the board behind it. New labels start at the last size
  and colour picked. A label left empty when the editor closes is removed.
- **The player sizes the box**; the text wraps to its width and is clipped
  to it, as the desktop draws it, so a label reads the same on both.
  (`fitRect()` in notes.js fits a box to its text and is not used today.)
- Drawn like the desktop's: a faint box in the label's colour, the text
  wrapped and clipped to it, zooming with the board; below
  Dev > text fades > "label text from" (6 px of text) only the box. View > **labels** hides them, and hidden labels take no taps.
- **One undo step per edit**: a label's own revision (`board.notesRev`) is
  part of the history's key, so moving or editing one is undoable without
  restarting the simulation, and a drag or an open editor is held back as
  a change in progress until it ends (otherwise every frame of a drag was
  a step).
- **Labels join a selection** (see "Selecting"): a box picks up every
  label it touches, and they move, turn, delete, cut, copy and paste with
  the rest, and bake into stamp slots.

### The Dev folder

Every number tuned by eye, and one behaviour switch, as dials in the
panel's **Dev** folder ([tune.js](tune.js)): belt width, chevron size,
overlay width and edge, port size and edge, glyph size and weight, the
Source value's size, grid opacity, ghost opacity, and the tap margin round
a ghost or a run's tip. Scales multiply the shipped value, so 1 is always
"as shipped", and **Reset dials** puts them all back. They persist with the
panel's preferences; a stored key this build does not know is ignored.

Below a divider, two sub-folders of **zoom thresholds**: the zoom (CSS px
per cell) below which belts turn to solid squares, and from which the grid,
glyphs and Source values, ports, port edges and belt chevrons draw; and the
**text fades**, in px of text height (what legibility depends on) -- item
numbers fading in between two values, and a label's text appearing from
one. `render.js`'s `LOD` reads them live through getters.

**Override zoom** (first in the zoom thresholds; 0 is off): above 0, the
thresholds decide as if the zoom were that value, whatever the real zoom
-- for a far-out view with full detail, or a close one without. The real
zoom also sets sizes, so it is not swapped out; each threshold is rescaled
instead (`t * z / override`, against the zoom of the render in progress),
which gives exactly the same yes or no. It applies to video frames too.

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

### Indexing

Giving a set of Sources and belts a number each, then a value from a table
by that number -- a Source's constant, a belt's seed. One-shot: the order is
not stored once applied. [indexer.js](indexer.js) is the pure part (orders,
tables, the file parser, `a*x+b`), tested in `tests/indexer.test.mjs`.

- **Index** on the selection bar starts it. The bar becomes Done / Tap order
  / Position / Values…, and every selected Source and belt wears its number
  (a badge in screen space). Selecting carries on as normal: in **tap
  order**, tapping one off and on again sends it to the end, which is how
  the order is fixed up; vectored selection adds a chain in step order.
  **Position** is left to right along each row, rows from the bottom up.
  Other selected entities are not numbered and keep their settings.
- **Values…** opens a sheet: the table -- **Random** (a shuffle of
  0..n-1, from a seed so the preview is what Apply writes; Re-roll), 
  **Ascending** (0, 1, 2…), **Doubling** (1, 2, 4…, clamped at 2²⁴), or
  **File…** (numbers separated by commas or whitespace, repeated to fill
  the index; `D` is the default: 0 for a Source, no seed for a belt; any
  other token rejects the file with its line) -- then **a** and **b**
  (1 and 0), so the value is `a*x + b`, float32, clamped; a default stays a
  default. A live preview lists the first dozen; **Apply to n** writes the
  values into the lifted entries and puts the selection down: one edit,
  one undo step.

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

### Pixels

**Pixel** ([pixel.js](pixel.js)) is a **web-only machine** that replaces
Sink: `installPixel(T)` swaps it into the loaded `types.json` under **Sink's
type id**, so every Sink in a save *is* a Pixel here, with no conversion
pass (same 2x2 footprint, same look, same row layout). It is the one
machine defined outside `core/entity.py`, by choice: the desktop is
reference material and still reads the id as a Sink. Old toolbar slots and
stamps naming "sink" resolve through `T.aliases` (`typeByName()`).

- **Four inputs, all needed to fire, no outputs.** Facing +X with cell 0
  bottom-left: **H** on cell 0's west face, **S** on the west face of the
  cell above it, **V** on cell 0's south face -- three contiguous perimeter
  spaces, V alone on its face -- and **Z** on the north face of the far
  corner. Chiral, so F mirrors it. The placed ports for all eight
  orientations come from `placePorts()`, a port of the desktop's
  flip-then-rotate, which `tests/pixel.test.mjs` checks by rebuilding every
  exported machine's ports from its authored ones.
- **A fire sets its pixel**: hue H in turns (wrapping), saturation S and
  value V clamped to 0..1, NaN as 0. **Z is the size**: 0 or less draws
  nothing; above 0, `round(Z)` clamped to 1..View > pixel max size, as half
  the side in cells -- Z = 1 covers exactly the machine (2x2), Z = 2 a 4x4,
  centred on the machine's centre, which is always a grid corner, so every
  square sits on the grid.
- **View > pixel overlay** (0..1) is the screen's opacity, and 0 is off:
  the sim then has no `Screen` and a fire records nothing (the Pixel still
  consumes its inputs). Switching it on starts blank; so do every edit,
  Reset (including a bake's), load and undo -- the sim clears it on any
  rebuild. Not saved.
- **The most recent fire draws on top** where squares overlap: the Screen
  is a Map that re-inserts a row on each fire, so iteration order is fire
  order.
- Drawn over the board and its items; while it is on, the planning ghosts
  move above it so what is being placed stays visible.
- **Not heard in a forecast**, like audio: `forecast()` runs a real step,
  so the hook is skipped while `rec` is set. A bake's clone has no screen.
- Its fires click in audio group 1. Items going in vanish at its edge, as
  they did into a Sink (it has no `LINKS`).
- `tools/check_web_sim.py` and the tests that compare with the desktop load
  `types.json` without `installPixel()`, so they still compare Sink with
  Sink.

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
| Index, then Values… | number the selected Sources and belts and write them values (see "Indexing") |
| Reverse | every selected belt and underground faces the other way, in place; Swap and Reciprocal turn half round, Merge and Route swap for each other (see below) |
| Vector, drag the offset, Confirm | add every repeat of the selection along the vector, until it breaks (see "Vectors") |
| Turn buttons | turn / mirror the selection as one stamp |
| Delete | discard the selection |
| long press a belt or underground | select its whole run; if the whole run is already selected, deselect it |
| long press anywhere else, then drag | a box from the press point: everything it touches is **added**, labels included. The box corner shows the moment the hold registers; lifting without dragging selects nothing |
| tap a selected label | put that label back |
| one-finger drag from a selected label | move the whole selection |
| Cut | the selection becomes a stamp, and pasting starts with the ghost **exactly where the selection was** |
| Copy | the selection becomes a stamp and goes back down; if all of it fit, pasting starts with the ghost over the originals |
| a stamp swatch | paste it (tap it again to stop) |
| long press a stamp swatch | rename it, or delete it from the history |

- **Deselecting is "if able", per entity.** Anything that does not fit
  (moved onto something, say) stays selected, tinted red where it clashes.
  So a deselect-all can leave a few behind, and Copy then does not start
  pasting, leaving the player to deal with them.
- **A box only adds.** It first toggled (dropping what was selected in the
  rectangle), which made growing a selection by overlapping boxes undo
  itself. There used to be a Box button on the selection bar as well; it
  was removed because the long press below was the only way it got used.
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
  select needs no first entity. `onLongPress` answers
  `"drag"` and `camera.js` hands the finger's moves and lift to the drag
  hooks. The cost is that a
  still hold off a belt no longer ends as a tap (it used to start a run or
  a placement there); it does nothing. A hold on a **selected** entity is
  the exception and stays a press, so a slow drag from it still moves the
  selection.
- **Reverse** turns the selection's belts round where they stand -- cell,
  seed and all. **A belt faces what fed it**, not the reverse of where it
  pointed: at a corner those differ (fed from the west, facing north, it
  must face west, not south), and turning every belt round broke a
  reversed run at each turn -- the first version did exactly that. Feeders
  are read from the selection as it was, all before any changes, through
  the run tracer's emit mask (the selection is lifted off the board); a
  belt fed by nothing selected -- a run's tail -- just turns round. It
  swaps each underground end's role **and** facing
  (the desktop's empty-hand R on a pair: the exit is downstream of the
  entrance by position, so swapping the role alone would leave it upstream
  of its own entrance). A pair inside the selection reverses together and
  stays paired. It changes the lifted entries; the selection going down is
  the edit, one undo step.
- **Reverse inverts the machines that have an inverse**
  (`reversedGate()` in stamp.js). Swap and Reciprocal turn half round, and
  Merge and Route each become the other, turned half round. A half turn
  keeps the box, and so the origin, where it was, and puts every input
  port exactly where an output was. The point is a tree of these: send
  signals out through it, then copy it and Reverse the copy to undo every
  swap and route. That only works if the control polarity lines up too, and
  it does: Merge takes lane `a` when `c > 0`, and the turned Route sends a
  positive control's value back out of that same cell. A half turn
  commutes with the mirror, so a flipped machine keeps its flip. Every
  other machine is left alone.

  `tests/stamp.test.mjs` runs values through each gate, joins each output
  port to the reversed gate's input on the same cell and face, and asserts
  every value comes back out of the port it went in by. It covers every
  orientation and both control signs. It is mutation-tested: no turn, a
  turn plus a mirror, and Merge/Route kept as themselves all fail. The
  turn-plus-mirror case **passed** at first, because the test looked up
  the reversed gate's orientation with the original flip and not its own.
- **Labels travel with the selection.** A box lifts every label whose box
  it touches off `board.notes` into the stamp's `notes`
  (`core/stamp.py`'s `Stamp.annotations`, rects in the stamp's frame), drawn
  dimmed with the lifted outline; hidden labels (View > labels off) are not
  picked up, as they take no taps. A label always fits, so it goes back
  whenever the whole selection does (`dropAll()`), or alone on a tap. Turns
  and mirrors move a label's **centre** with the cells and never its box or
  text, so it stays upright, readable and over what it labels. Copy, Cut and
  the paste ghost carry them; a paste places them wherever it lands
  (nothing blocks a label), and a label-only paste is a label edit, so it
  does not restart the simulation. The stamp's box is the **entities'**,
  so a label hanging outside them does not move the paste ghost; a stamp of
  labels alone takes their box on the cell lattice. Reverse, Index and the
  selection vector ignore labels.

  `tests/stamp.test.mjs` puts each label on a marker belt and asserts it is
  still centred on that belt, at its own size, after random sequences of
  turns and mirrors (mutation-tested: turning the wrong way, a no-op mirror
  and swapping w/h all fail). The desktop is **not** the oracle here:
  `core/stamp.py`'s `rotated()` and `mirrored()` turn a label inside the
  box (`rotate_in_box()`) *and then* shift it again in `normalise()`, so on
  a 3×1 run a label on the last belt comes back on the wrong end after a
  clockwise turn or a mirror (the counter-clockwise turn is only right
  there because the run is one cell tall). The two apps do not share
  stamps, so nothing crosses over, but the desktop's is a bug.
- Rounding: a label's rect is rounded to a millionth by the transforms, not
  to the thousandth a dragged label uses -- at a thousandth a box of odd
  thousandths moved half a thousandth a turn.
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

### A sheet must not close on the tap that opened it

A sheet (the label editor, the index values, the stamp menu) closes on a
tap on its dimmed backdrop -- but only when the **press began there**
(`closeOnBackdrop()` in ui.js). A tap on the board that opens a sheet is
followed on a phone by the browser's own synthesized click a moment
later, by which time the sheet is up, so that click lands on the
backdrop: closing on any backdrop click shut the label editor the instant
it opened, and an empty new label was then removed -- a box that showed
for one frame. A mouse test does not reproduce it; a touch test
(`devices['Pixel 7']`, `touchscreen.tap`) does.

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
dt is clamped to 0.25 s and the backlog to 240 ticks or a quarter second's
worth at the rate, whichever is more, so a backgrounded tab never freezes to
catch up -- a flat 240 would have capped 10000 tps at 240 ticks a frame.
`paint()` also stops stepping after `STEP_BUDGET_MS` (12 ms) of a frame and
drops the rest, so a rate the board cannot sustain runs as fast as it can
rather than starving frames into a growing backlog; the fps counter's tps is
the rate actually reached. **Rendering loops only while playing**; paused,
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

## Audio

The panel's **Audio** folder makes sound from the ticks: a drone per moving
belt run and a click per gate fire. **Off by default and off at every
launch** (a browser only starts audio from a tap). The dials travel **with
the save** (below) and are also kept in the panel's preferences as the
last ones in front of the player; the switch is never kept.

**Off costs nothing.** `audio.js` and the worklet are imported only when
the switch is ticked, and the tick's only trace is `Sim.audio`, null: one
check in `step()`, passed on to `structurePhase()`. Measured on `cpu.npz`
(best of 8 runs in node): 67 µs a tick before, 63 µs after with audio off,
73 µs with it on -- the recorder is the whole on-cost.

Three files, split by thread:

- [audio-params.js](audio-params.js) -- the dials (`AUDIO_DEFAULTS`,
  `AUDIO_SPEC`) and the gate groups. Tiny and loaded always, so the panel
  can show the dials without any audio code.
- [audio.js](audio.js) -- main thread. `Recorder` is `Sim.audio`: per tick
  it writes one bit per **candidate run** (a run no longer than the cutoff)
  and a fire count per **group** into flat typed arrays, and `paint()`
  posts the frame's ticks to the worklet (`flush()`, buffers transferred),
  or `stop()` while paused. `startAudio()` builds the graph.
- [audio-dsp.js](audio-dsp.js) -- `GatesSynth`, the synthesiser, as a plain
  class; [audio-worklet.js](audio-worklet.js) is its AudioWorklet wrapper.
  Plain so `tests/audio.test.mjs` renders it under node.

What sounds:

- **A belt run is active on a tick** if it pushed or popped, or its newest
  item moved (the belt status colours' "moving", with "stalled but moving:
  green"). Two `positionOf()` calls per candidate per tick. Its drone has
  pitch **1-tile Hz / length** (the sim's length, buried underground tiles
  included), so 10 tiles is an octave above 20 and every run is a
  subharmonic of the 1-tile pitch. Runs outside **min length** to **max
  length** get no voice (the recorder leaves them out of the candidates).
  Attack and release dials gate it per tick: short, a run moving every
  other tick buzzes at half the tick rate; long, it smooths to a drone.
  **Follow tick rate** scales the pitch by (rate / reference)^track; 0
  (the default) keeps it fixed.
- **A gate fire is a click**: an impulse into a two-pole resonator (tone,
  decay), per group -- **0: Swap, Route, Merge; 1: everything else**;
  Sources are silent (`audioGroupOf()`). Only the per-tick **count** per
  group crosses threads, so fires cost the same on any board. Clicks
  closer together than a click lasts overlap into a buzz at the tick rate,
  by design: raise the rate and the ticking becomes a pitch.
- **Normalisation is against the board, not the moment**: a drone is scaled
  by 1 / N^p with N the candidate runs, a group's clicks by 1 / N^p with N
  that group's gates. N changes only on an edit, so more activity is always
  louder; p = 0.5 puts "everything active" near full scale for
  uncorrelated voices. A tanh soft clip is the safety net (switchable). The
  meter shows the peak before it, in dB, and counts clipped samples.

**Time.** The sim ticks in bursts, a frame's worth at once; the synth plays
them back evenly at the rate they were run at, **latency** behind (60 ms).
Playback starts once that much is queued, and its speed follows the queue's
depth (smoothed, 0.25× to 1.25×): if the sim cannot reach its rate (the
frame budget drops ticks), playback slows and pitches sag rather than
gapping. A queue over 3 latencies deep is **skipped** to the target, not
raced through. Run dry -- paused, or stalled -- every voice releases and
playback waits to refill. A tick takes effect at its start (an early
version applied each tick after its own duration, at a placeholder rate of
1 tps, and so played nothing at all).

**Voices.** A voice belongs to an active run, never to a candidate as such:
13 candidates of which 4 are active take 4 voices. Past the **voice limit**,
a run turning on is **refused** (no stealing, which would click) and counted;
when a voice frees -- its run off and its release finished -- the first
refused run still on takes it. A releasing voice still holds its slot, so
at high tick rates with a long release the count runs near the limit while
few runs are active at any one instant: the meter shows both
(`voices n / limit`, `runs active k of N`). The pool is
`VOICE_LIMIT_MAX` (1024). A reconfig (any edit: the run ids are rebuilt)
orphans the old voices, which release out at their old pitch.

**The forecast is not heard.** `forecast()` runs a real `step()` and rolls
it back; `step()` skips the recorder while `rec` is set, or every drawn tick
would be heard twice. Pinned by the recorder test, which also asserts the
recorded run is tick-for-tick identical to an unrecorded one.

**Tone** (a sub-folder): a gain per run length for **lengths 1 to 6** --
the shortest runs are the highest pitches, and the usual thing to tame --
with 7 and longer at 1. Per voice, not a filter, so it takes exactly those
runs and nothing else (a filter would also take the upper harmonics of
longer runs under a triangle or saw); it follows the dial on voices already
sounding. **Low cut** is a 2nd-order Butterworth high-pass on the whole mix
against rumble (0 Hz is off), per sample, so a bake stays bit-identical.

**Start tick and fade** (`startTick`, `fadeMs`): the synth is silent while
the tick it plays is before the start tick, then fades in. It keeps
following the runs while silent, so a drone already going at the start
tick fades in rather than starting cold. Each batch carries its first
tick's number (`tick0`) for this. Default 0 and 0: no change.

**Hold on underrun** (off by default): when the queue runs dry, the voices
hold where they are instead of releasing, and playback picks up the moment
ticks arrive, with no refill wait. A hold gives up after `HOLD_MAX_S`
(0.5 s); a pause is still a stop. It is the realtime remedy for stutter at
busy moments; baking is the thorough one.

### Baking

**Bake audio…** (Audio folder) opens a sheet: seconds (default 30, at most
300) and ticks per second (default the current rate), **Bake**, **Cancel
bake** and **Download WAV** (16-bit mono). [audio-bake.js](audio-bake.js)
renders the stretch ahead of time with no frame budget and no deadline:
the same recorder and the same synth, the synth **offline** (no latency, no
speed control, no skipping), fed every tick before it renders their
samples. About 0.6 s for 4 s at 240 tps on desktop; most of the cost is
ticks, so 10000 tps is far slower to bake than 240.

- **It starts with a Reset and keeps that state** (`Sim.clone()`), and
  while the bake is live **Reset restores it** (`Sim.restoreFrom()`)
  instead of re-deriving. A rebuild renumbers the runs, alternating scan
  order, and the numbering decides the belt phase's order, so only the
  same state is sure to replay the same ticks. With no bake, or after an
  edit, Reset is the desktop's as before (`check_web_sim.py` is unaffected).
- **It runs on a clone**, in 25 ms chunks with a yield between, so the live
  board is untouched and the page stays live. A clone copies the entity
  table and each run's ring and counters; everything derived (gate table,
  edges, cell maps, the grid) is shared, since a clone never rebuilds. An
  edit or a new board cancels the bake.
- **Warm-up**: the synth runs from about half a second (attack + release)
  before the start tick, so the bake starts with the voices a realtime run
  would have.
- **Playback**: while the live sim is in the baked stretch, at the bake's
  rate, with the same dials (`bakeKey()`: everything but latency and hold),
  the buffer plays straight to the destination -- no queue, so only the
  output's own latency -- and **the audio is the clock**: each frame steps
  the sim to the tick being heard (context time less `outputLatency`), with
  a 100 ms step budget, so a busy frame drops frames rather than drifting.
  Leaving the stretch, pausing, or the bake going stale hands back to
  realtime (a small skip, accepted). The meter's last line says which bake
  exists and why it is stale.
- **A bake is the same to the bit however it is chunked**, which took three
  fixes in the synth, all inaudible but each a real dependence on where a
  render call ended: offline, tick k starts at sample `round(k * sr /
  rate)` from a sample count rather than an accumulated float; finished
  voices are freed only on a tick boundary, **after** the span is rendered
  (a refused run given the freed slot mid-loop was rendered over the whole
  span, starting before its time); and a dead click is flushed to zero per
  sample rather than at span ends. Found by baking twice and comparing.

### Audio settings in the save

Every save carries the dials and the Bake dialog's seconds and rate under
its own `audio` key: a JSON string in a 0-d array, the same shape as
`annotations` (`audioRecord()` / `parseAudio()` in audio-params.js). The
desktop ignores a key it does not know, so it still loads these files; it
does not write the key back, and is deliberately left that way.

- **Loading a save with settings applies them** over the dials and shows
  "audio settings loaded from this save" in the meter for a few seconds.
  **A save without them** (the bundled ones, anything older) leaves the dials
  as they are. Either way `board.audio` then becomes the dials in front of
  the player, so the next save writes them.
- **A dial change is a change for the autosaves**, like a label edit
  (`board.audioRev` is part of the save key, and it sets `edited`), but it
  does not restart the simulation and is not an undo step: a slider drag
  is dozens of changes. Reset audio dials is one more such change.
- **Applying a save's dials is not a change.** Tweakpane's `refresh()` fires
  `change` (it re-snaps the value to the step), so `Panel.applyAudio()`
  holds `applyingAudio` while it refreshes and the panel does not report
  it; without that, merely loading a save counted as an edit and woke the
  autosaves.
- **Cleaned on read** (`cleanDials()`): only known dials of the right type,
  clamped to the panel's ranges; a bad entry costs that dial, a corrupt
  blob costs the settings, never the board.
- The baked audio itself is not saved: memory only, and tens of MB.

Two practical notes: the `AudioContext` is created and resumed **inside the
tap** (main.js `setAudio()`), before the dynamic import, since iOS will not
start one made after an await; `navigator.audioSession.type = "playback"`
is set where the browser has it, or the iPhone's silent switch mutes it.
And ticking the box **blurs it**: otherwise the next Space (play) toggled
audio off again.

`tests/audio.test.mjs`: the octave rule and rate tracking by zero
crossings; silence when idle; the buzz's period is the tick's; the start
threshold and run-dry release; the skip; the voice limit (refusal and
reuse); stop; the groups; the recorder on `cpu.npz`; the start tick and
fade; hold on underrun; sample-exact ticks offline; the WAV header; and a
real bake -- the live sim untouched, two bakes identical, and
`restoreFrom()` replaying the same ticks; the length window (and that
changing it reconfigures); the per-length gains, live; and the low cut. Mutation-tested:
recording the forecast, never reusing a freed voice, and a wrong pitch law
each fail it.

## Video

The panel's **Video** folder renders a stretch of the simulation **offline**
into an MP4: after Fluoddity's Recording tab (`aphid91/fluoddity-web`,
`src/recorder/`), with **mediabunny** (MPL-2.0, vendored unmodified as
`vendor/mediabunny-1.55.1.min.mjs` with its licence, and imported only when
a recording starts). [video-params.js](video-params.js) is the settings and
the camera arithmetic, loaded always and tested in node;
[video.js](video.js) is the recorder, loaded on demand.

- **Settings**: width and height (128-3840, even -- H.264 4:2:0 needs it;
  the first default is the board's size on screen in device pixels),
  duration (1-120 s), **ticks / s** of video, and mediabunny's five named
  quality levels. 60 fps, fixed. The summary line reads "frames · size ·
  ticks/frame", then progress. Persisted with the panel; the end camera is
  per session.
- **Offline, on a copy.** It starts **from the current tick**, on a clone
  of the sim (as a bake does), and draws each frame into its own canvas
  with `render()`, so the frame budget and the live board's playback never
  touch it. The live sim is paused meanwhile, and an edit or a new board
  stops the render. Frame `f` is the state at tick `t0 + f * tps / 60` --
  fractional ticks drawn at that alpha, so a slow tick rate still moves
  smoothly -- stamped `f / 60`, a **computed** time, so a frame that takes a
  second to draw still lasts 1/60 s. `await video.add()` is the
  backpressure (Fluoddity's note): dropping it queues frames until the tab
  dies.
- **What is drawn**: the board as View draws it -- grid, status colours,
  the pixel overlay (the copy gets its own Screen, starting blank), labels
  -- and never the selection, ghosts or other editing marks
  (`drawVideoFrame()` in main.js; `gateStatus()`/`beltStatus()` take the sim
  to read for this).
- **Sound**: with Audio's switch on, the same ticks are baked
  (`bakeAudio(..., t0)`: a bake may now start mid-run, at the video's own
  first tick, so the soundtrack starts and ends with the picture; the start
  tick still silences what comes before it) and fed to an audio track a
  second at a time beside the frames. AAC, or Opus where AAC cannot be
  encoded (Linux Chromium); H.264, then VP9, then AV1 for the picture, each
  asked of the browser rather than assumed.
- **The camera**: the frame is the largest box of the video's aspect that
  fits the view (`cropBox()`), shown on the board while the folder is open;
  **Select end camera state** captures the view as it is, drawn dashed, and
  **Unset end camera** drops it. The move runs from the view at Record to
  the end state over **start lerp .. end lerp** (fractions of the video,
  still outside), its speed a trapezoid: **fade in** and **fade out** are
  the ramps as fractions of that window (0/0 constant speed, 0.5/0.5 a
  smooth S; scaled down if they exceed 1). Centre linear, size geometric,
  so a zoom feels even.
- **Saving**, as Fluoddity: the file picker is raised **before any other
  await** in the click (it needs the gesture), and a chosen file is
  **streamed** into (`fastStart: false`, flat memory; `finalize()` closes
  it -- closing again throws); no picker (Firefox, Safari, iPhone) or a
  failed one **buffers** in memory and downloads at the end; Cancel in the
  picker records nothing. **Cancel recording** keeps what was encoded: a
  partial video is still a video.

Checked in headless Chromium: 2 s at 480×270 with sound and an end camera
gave 120 frames of H.264 at exactly 2.00 s and an Opus track (mean
-15 dB), the first frame the whole board and the last the zoomed end
camera, the live sim still at its tick; cancelling a 1080p recording at
frame 182 gave a valid 3.03 s file. `tests/video.test.mjs` covers the move
(still outside the window, monotone, continuous, linear without fades,
symmetric), the crop box, the lerp, the settings, and a bake starting
mid-run.

## Saving

The panel's **Board** folder saves the board **to this browser** under a
name (the loaded board's, until edited), and keeps a list of those saves:
pick one, then **Load saved**, **Export .npz** (a download) or **Delete
saved**. Picking alone does nothing, so choosing what to export never
replaces the board in front of you. Saving over an existing name asks first.

- **A save is a real `.npz`**, written by `Board.toNpz()` in the layout
  `core/persistence.py save()` uses: the same keys, `.w` zeroed (derived),
  `next_row` / `free_rows` / `live_count` from the board's allocator, and
  the annotations blob re-written from `board.notes` -- the labels as
  edited here, and the desktop's strokes untouched (a save that predates
  notes gets `"[]"`, as the desktop writes). `npz.js` has the
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
- **Four saves keep themselves**, listed with the rest. Three timed tiers
  (`AUTOSAVES`): **auto save** every 30 s, **autosave5** every 5 min and
  **autosave15** every 15 min. Each writes when the board has changed since
  that tier last wrote, so the slow two are older copies to fall back on.
  The fourth is **exitsave**, whenever the app is hidden -- closed, switched away from,
  the screen locked. A phone gives no other warning of a close, and nothing
  asynchronous can be relied on to finish in that moment, so the save's
  bytes are **prepared ahead of time** (compressed after every committed
  undo step, `prepareSave()`) and the hide only writes them, synchronously.
  An edit made in the last few milliseconds before a close may miss it
  (still compressing), and the previous step is saved instead.
- **Both write the history's current state** -- the last stable board,
  `Board.toNpz(snapshot)` -- so a selection half-lifted off the board is
  saved where it was picked up, not missing.
- **Nothing is written until the board has been edited this session**: no
  tier and no exitsave (`edited`, set by `recordStep()`; loading a board
  does not count). Opening the app used to clobber the saves it was opened
  to choose between: the resumed board overwrote an older autosave before
  the player got round to loading it. A session that only looks, or only
  loads, leaves every save as it found it. The cost is that loading a
  board and closing without an edit resumes the previous one next time.
- **Opening the app loads the exitsave, unless the auto save is newer**,
  and either opens under the name of the board it holds (kept in its index
  entry), so saving it again does not write over the autosave. With
  neither, the bundled default loads as before. The 5- and 15-minute tiers
  are never resumed; they are loaded by hand.

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
| `panel.js` | the Tweakpane panel: bundled and browser saves, export, View, Audio, Debug, Status (with the live channel readout) |
| `audio-params.js` | the Audio dials and the gate groups; loaded always |
| `audio.js` | audio, main thread: the per-tick recorder and starting the graph; loaded on demand |
| `audio-dsp.js`, `audio-worklet.js` | the synthesiser, and its AudioWorklet wrapper |
| `audio-bake.js` | baking: a stretch of ticks rendered offline, and the WAV writer; loaded on demand |
| `pixel.js` | the Pixel machine (web-only, in Sink's id), its screen, and the overlay's drawing |
| `video-params.js` | video export: the settings and the camera move; loaded always |
| `video.js` | video export: the offline recorder (mediabunny); loaded on demand |
| `vendor/` | Tweakpane 4.0.5 (MIT) and mediabunny 1.55.1 (MPL-2.0, with its licence), vendored |
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
| `notes.js` | labels: the desktop's text-box records, hit-testing, wrapping, fitting a box to its text |
| `indexer.js` | indexing: the orders, the value tables, the file parser, a·x+b |
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
the `.npz` writer, audio settings in a save) run in Node, with no npm and no browser. The whole tick against the
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
