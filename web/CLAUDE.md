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
per-instance settings, select / cut / copy / paste, layers, and the simulation.** It
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

`build.sh` copies the app and every `.npz` in **`web/saves/`** into
`_site/`, writing `saves/index.json` for the save picker. On push to `Web`,
`.github/workflows/pages.yml` does the same and deploys to GitHub Pages.
`Default.npz` is loaded at startup if it exists. The bundled boards are
**CPU experiments** (the root's `cpu.npz`), **RAM experiments** (the root's
`quicksave.npz`), **GameOfLife_SLOW** and **Default**; the `.npz` files at
the repo root are not shipped -- they are the desktop's and the tests'
fixtures (`check_web_sim.py`, the web tests and the desktop's profiling
tools load them by name), so they stay where they are.

Each presentation's board is in the picker too, after the bundled saves, as
`<name> (presentation)` (`build.sh` writes `presentations/index.json`;
`loadBundled()` takes `presentations/<name>.npz`). It loads as a plain board
to edit: the board only, none of the show's views. **`test`** is one: a
large, long-running board (3.4k machines, 16.5k belts, 1.6k undergrounds)
kept as the renderer's profiling load -- see "Profiling a frame".

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
  which opens a text box (0.1 to 1,000,000). It stays open until the next long
  press or its × button, and shows the tick count. The rate (30 by default)
  and the panel's open state persist with the shell's localStorage. The
  panel is **open on a first visit**, so it is found; the first time it is
  ever closed, a hint over the Sim section says "long press play/pause for
  tick controls" for a couple of seconds (`simHinted`, kept, so once).
  **With a custom rate** (not a preset), a vertical slider shows on the
  board's right edge while the panel is open: the clock runs at the custom
  rate times the slider (0..1, 1 by default -- the custom value is the
  maximum), its readout the rate that gives. It hides when the panel closes
  or a preset is picked, and keeps its value (`rateScale`, persisted with
  the shell); hidden on a custom rate, it still applies. Presentations
  ignore it (their rates are their own), and a bake's playback sets the
  exact rate, putting it back to 1.
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
default. Top to bottom: Status, Board, View, Audio, Video, Presentation,
Assembler, Dev, Debug. It holds the saves, View toggles (seed
tint -- the green/red wash on seeded belts, ghosts included -- labels,
items, **item numbers** (on by default; off, items are just their
green/red discs), belts (placed belts and underground ends; a planned run's
ghost still draws), gates (every machine, Sources included -- for a brief
look at the belts underneath; still there and still tappable, and items
held inside a machine still draw: View > items hides those), sources
(hidden, not removed), **gate status colours** and **belt status
colours** (both on by default), a **status
colours** folder of pickers for both, and an **fps counter** -- frames and
ticks per second actually reached, over half-second windows, top-left of
the board under the sound button), Dev, Debug, and
the Status folder. **The rarely changed View and Audio controls live in
Dev** -- grid lines (off by default), ports, source values, pan momentum,
stalled but moving: green and pixel max size under **Dev > View**, and
Audio's follow tick rate and at tick rate under **Dev > Audio > belts** --
in sub-folders named after where they came from (see "The Dev folder"). Tweakpane is **vendored** (`vendor/`, MIT) rather than
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
`beltStatus()`: **red** empty; **yellow** stalled -- holding items, none of
them moving and nothing pushed or popped lately; **green** moving.
**Stalled but moving: green** (Dev > View, on by default) counts the whole run: a push,
a pop, or any item still closing up. Off, only the head counts -- a run is
stalled once its head reaches the end and sits there, whatever is still
packing behind it. Underground ends take their run's colour too, and the
dots over a buried span a lighter wash of it. The seed tint still draws
under.

#### The afterglow

View > status colours > **afterglow** (ticks, 0..250, 15 by default, 0
off): a machine or run lights up the moment it acts and cools back to its
resting colour over that many ticks -- for machines from RUNNING to blocked
or starved, for runs from MOVING to stalled or empty. **Ticks, not
seconds**, so the share of the time something spends lit -- how bright a
busy board looks -- is the same at every tick rate; it **freezes while
paused**. (It was seconds at first, stored as `statusFade`; the new key is
`statusGlow`, so a stored seconds value is never read as ticks.)

**Afterglow by time** (off by default) measures the fade in **seconds**
instead (its own dial, 0..5 s, 0.5 by default; only the dial in use
shows): the same pace on screen at any tick rate, where ticks keep the
share of the time something is lit -- and so a busy board's brightness --
the same at any rate. It is still a span of **sim** time: the seconds are
turned into ticks at the current rate (`glowTicks()`), so the colour stays
a function of the state on screen, it freezes while paused, a rate change
rescales a glow already under way, and a video uses its own ticks/s. The
two-tick hold below is in ticks either way. Both keys (`statusGlowTime`,
`statusGlowS`) ride presentations' views; a view saved before them keeps
whatever the player has.

**Glow sluggishness** (-2..2, 0 by default) scales a **run's** fade by
(length / 10)^s -- the sim's length, buried tiles included, the law and
reference length Audio's sluggishness uses -- so at 1 a 20-tile run glows
twice as long as a 10-tile one, and below 0 long runs cool faster.
Machines are unaffected. One multiply per run per frame, the factor looked
up per length: `beltStatus()` measured 0.20 ms a frame on `test` with it on
and off. Both dials ride presentations' views. **Reset colours** asks
before resetting, and leaves both dials alone.

- **Activity is read back, not predicted.** The sim stamps the tick a gate
  last fired (`Sim.fired[row]`) and a run last pushed or popped
  (`seg.lastPush`, `seg.lastPop`) -- one write per event, never read by the
  tick, skipped while forecasting, cleared on every rebuild, carried by
  `clone()` and `restoreFrom()`. An item travelling down a run makes no
  event, so a run is also hot until **`Sim.restTick()`**: when its newest
  item (or its head, with the option off) comes to rest if nothing more
  happens -- the later of its entry tick plus its packed place over the
  speed, and the last pop plus spacing over speed (the anchor's way back
  to the end). That is a constant between pops, so the colour is a pure
  function of the state on screen and the time: no per-frame history, so
  a gate that fired once between two frames at 5000 tps still lights, and
  a video's copy colours as the screen does. `tests/sim.test.mjs` pins
  `restTick()` against running the run on with nothing offered or taken
  (150 random runs, speeds and spacings), and that it does not move
  without a pop; mutation-tested (drop the anchor term, an off-by-one, the
  wrong stamp, the wrong packed place: each fails).
- **Two ticks of look-back replace the old look-ahead.** Anything that
  acted in the last `AFTERGLOW_HOLD` (2) ticks is fully hot at any fade, so
  a run a gate takes from every other tick does not flicker. The status
  colours used to ask the forecast "will the head pop on the coming tick?"
  for that; they no longer read the forecast at all. A machine that is
  ready and unblocked (it fires on the coming tick) is RUNNING, as before.
- **Heat is rounded to 16 levels**, and every (state, level) colour is
  built once per colour change (`statusPalette()`), so a fading board is a
  few dozen colours and render.js's batching by colour holds.
- **Cost**: a gate's resting state is worked out once per sim version (at
  most once a frame); heat is per frame, one stamp read per machine and run.
  On `test`, both together about 0.5 ms a frame.

### Truthiness

One bit per gate and per run, saying which side of zero the thing it just
did was, for the afterglow's colours and the audio. **Recorded only while
something reads it** -- View > **truthiness colours** or Audio >
**truthiness** (`Sim.truthOn`, set by `paint()` each frame) -- and off,
nothing is written: one boolean test per fire and per push, measured as no
change to the tick on `cpu.npz` and `test`. So turning it on starts from
stale bits: 1 (truthy, today's colours) until each gate fires and each run
pushes again.

- **A gate's bit** (`Sim.truth[row]`, written by `fire()`) is 1 iff the
  value `truthSource()` names is > 0 (NaN is not). That is read from the
  machine's own exported data, not a table: a select rule (Merge) or a
  conditional op (Swap's `OP_SEL`, Route's `OP_GATED`) gives the
  **control** input; otherwise the first output that is not a plain copy
  (`OP_IN`, the pass-throughs) -- Shear's sum, Mac's accumulator, Max,
  Floor, 1/a, a Source's emitted value; a machine with no outputs (Pixel)
  is always truthy.
- **A run's bit** (`seg.truth`) is its newest item's sign, set on a push
  (seeds included) and never on a pop.
- Both ride the worker's deltas while on (`runTruth`, `rowTruth`), and
  clones, bakes and `restoreFrom()`. A forecast writes neither.
- **Colours**: a falsy fire or push glows from View > status colours'
  **gate running (falsy)** / **belt moving (falsy)** (blue by default)
  instead of the running / moving colour, cooling to the same resting
  colours. Read per row only when the switch is on.
- **Audio**: see "Truthiness in the audio" under Audio.

`tests/truth.test.mjs`: what each machine reads; nothing written while off
and the tick unchanged while on; every fire's bit against the forecast's
values and every run's against its newest item on `cpu.npz`; the worker's
mirror and the recorder's falsy fires and flips against a main-thread
reference; truthy plus falsy fires is every fire; the synth's falsy clicks,
transposition and crossfade time; and the block renderer against the
per-tick reference. Mutation-tested: dropping the delta's run bits, or the
synth applying no flips, each fails it.

### Pan momentum

A one-finger pan released while moving keeps gliding, slowing exponentially
(`camera.js`: time constant 325 ms, about iOS's own feel). The release
velocity is measured over the pan's last 90 ms, so a finger that stopped
before lifting does not fling; any touch catches the glide. Dev > View > **pan
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

### Layers

AutoCAD-LT-style layers: named groups that **partition** the entities, all
on the one shared grid. [layers.js](layers.js) is the model (pure, tested in
`tests/layers.test.mjs`), [layerpanel.js](layerpanel.js) the panel, and
main.js's "layers" section applies them. View > **layers** shows the panel
(top left, under the sound button); while it is off, layers change
nothing -- everything draws and can be touched -- but new entities still go
on the current layer.

- **The tag is in the entity row, slot `F_LAYER` (27)**: the last slot
  spare at the desktop's stride (24..25 a belt's seed, 26 the desktop's
  sleep flag). Not exported in `types.json` (the desktop is not changed for
  this), so `Board` checks at load that nothing else claims it. In the row
  it rides snapshots, undo and saves for free, and a zeroed row -- anything
  placed by the desktop or an old save -- is on 0, **Background**.
- **The table** (names, colours, flags, sliders, current) is a JSON string
  under the save's `layers` key, like `annotations`; the desktop ignores it.
  Malformed entries are repaired, not refused. Ids are handles, never
  reused. A row naming an id the table lacks gets a "Layer n" entry rather
  than being folded into Background.
- **Current** is always visible and active; making a layer current turns
  both on. **Hidden implies inactive** (the stored `active` survives, so
  showing a layer again restores it). Background cannot be renamed or
  deleted and takes no tint or label.
- **Inactive means untouchable**: every cell lookup an interaction makes
  goes through `pickAt()` -- tap-select, box, long press (the run is traced
  whole and only its active cells lift; a hold on a locked belt arms a box),
  vectored selection's match, a run starting on a belt, force build (which
  never deletes a locked entity). The Hand inspects through `seeAt()`:
  hidden entities are not there, inactive ones inspect **read only** (no
  number swatches; Status edits are refused). Hidden entities still occupy
  their cells and still simulate.
- **Which layer a new entity gets**: a builder tool (belts, machines, raw
  belts, a baked stamp, the planner's undergrounds) places on the current
  layer (`Board.tagLayer`). A selection keeps each entity's layer through
  lift, move, turn and put-down (`capture()` records it, `placeEntries()`
  honours `entry.layer`). A **cut**'s first placement is a move and keeps
  them by id (`place.keepLayers`). **Every other paste keeps them too, by
  name**: Copy and Cut record each entry's layer as `layerRef`
  (`tagLayers()`: `{ name, colour }`, `null` for Background), and a paste
  resolves it against the board's table (`resolveLayers()`) -- a name the
  save already has merges into that layer (case and end spaces ignored,
  and **its own colour wins**); a name it lacks becomes a new layer with
  the stamp's name and colour. So a stamp copied in one save and pasted in
  another brings its layers. An entity that does not fit makes no layer.
  A stamp from before this has no `layerRef` and goes on the current
  layer, as copies used to. A paste landing on hidden or inactive layers
  says so in a toast (only while View > layers is on). New layers are part
  of the paste's undo step. **Adopt**
  rewrites the lifted entries' layer; it lands when the selection goes
  down, and a Copy after it records the adopted layer.
- **Undo**: `board.layersRev` is part of the history key, bumped for
  membership, names and colours (deleting a layer retags its rows to
  Background without a derive, so none of this restarts the simulation).
  Visibility, current and the sliders are view: not steps, and a restore
  keeps the view as it stands.
- **Drawing** (`layerView()` in main.js) is rebuilt only when the board, the
  table or its view changes: the visible lists (only while something is
  hidden), a hidden-cell mask that culls items (one byte read per item),
  each layer's member count and centre of mass, and the tints. A tint
  blends the layer's colour over what the entity would otherwise wear --
  its own colour **or a status colour** -- by the layer's own amount, so at
  1 the layer wins and below it both read. Lifted selections and ghosts are
  not tinted.
- **The tint slider sets every active layer** (not Background); each layer
  keeps its value after. It shows the current layer's value (with
  Background current, the first active layer's).
- **The label slider sets the current layer only**, and is disabled with
  Background current, which never has a label. The label is the layer's
  name at its centre of mass, its height in world cells (zooming like the
  board's own labels, 0 to 6, 0 off), in its colour over a black box, drawn
  **last of all** -- over the Source values too. Background is counted in
  `layerStats()` but never given a centre of mass.
- **A long name wraps** (`wrapLabel()`): into about `sqrt(width / (4 ×
  line height))` lines, so the block comes out roughly four times as wide
  as tall, then the narrowest width that keeps that many lines, so they are
  even rather than one long line and a tail. Words are never split, and
  short names stay one line. Wrapping is the same at every size, so the
  renderer measures once at 100 px and caches the lines per name.
  **`//` forces the breaks** (`LABEL_BREAK`; a single slash is ordinary
  text, so `in/out` survives): the lines are used exactly as typed, each
  trimmed and empty ones dropped, with no automatic wrapping on top. The
  panel and the Hand chip show the name as typed, `//` and all.
- **Long press a layer's name to isolate it**: it becomes current and every
  other layer, Background included, inactive. Long press it again while it
  is the only active layer and every **visible** layer is active again (a
  hidden one keeps its own flag). View, not an undo step. Right-click on a
  mouse; the hold swallows the click its release makes.
  **On touch the hold is the panel's own timer, never the browser's
  contextmenu** (`wirePress()`). Android Chrome fires one for the same long
  press, with its haptic, a moment after the timer -- and the isolate has
  by then rebuilt every row, so it lands on a *new* button that never saw
  the press, and a per-button "already held" flag let it hold again: the
  isolate undone a frame later, unless the finger lifted in the gap. So the
  pointer kind and the swallowed click live on the panel, not the button.
  Reproduced in Chromium as a Pixel 7 (a CDP touch hold, then a touch
  contextmenu at whatever is under the finger 100 and 300 ms later):
  undone before, kept after; a right-click still isolates.
- **Double tap a layer's name to select all of it**: the first tap makes it
  current as usual, the second (within 350 ms) lifts every entity on it
  into the selection -- adding, as selecting always does. Selecting is a
  state of Build, so the shell switches there, dropping a run, placement or
  paste in progress. The timing is kept on the panel (`lastTap`), not the
  button: the first tap makes the layer current, which rebuilds its row, so
  the second tap lands on a new element. Background's 5,145 entities on
  `cpu.npz` lift in about 16 ms.
- **Inactive layers are washed out**: their cells get a `saturation`
  composite with grey (colour drained) and then a dark wash, in one batched
  pass after the items, so body, glyph, ports and items all fade; Source
  values and item numbers over them, drawn later in screen space, fade to
  30%. Not in video frames: what can be touched is an editing matter, like
  the selection.
- **The toggles**: an eye for visible, a **star** for active -- filled and
  amber when active, a dim outline when not -- since fill against outline
  reads at a glance where an open lock against a closed one did not. The
  current layer's pair show on (and cannot be turned off); a hidden
  layer's star is faint and disabled.
- **The Hand's bar** shows the inspected entity's layer -- its colour box
  and name (shortened to about 110 px) -- with a small icon-only **Make
  current** button under them, unless it already is current. No "Layer"
  word: the colour box says what it is.
- **Long press the word "Label"** (beside its slider) to switch every
  layer label off or on (`showLabels` on the table, saved with it). Off,
  the word is struck through and the slider dimmed and disabled; each
  layer keeps its own size, so on again brings them all back as they were.
  View, not an undo step, like visibility.
- **In the Hand, a tap on a layer label picks it up** (a dashed outline) and
  a drag from it moves it: an offset in cells from the layer's centre of
  mass (`labelDx`/`labelDy`, saved with the table), so the label keeps its
  place relative to the layer as members come and go. A drag follows the
  finger's motion, never pans, and is one undo step when it ends. Layer
  labels draw over everything, so they win a tap before notes and
  entities; with one picked up, any other tap only puts it down. Leaving
  the Hand, switching labels off or turning View > layers off also puts it
  down. Hit-tested against the boxes the last render drew
  (`labelHits`, filled by `drawLayerLabels()`), since only drawing knows a
  label's measured size; the topmost wins.
- **The grip** in the panel's bottom-right corner resizes it: across sets
  the width (the name column takes it all, so names show more or less;
  the count, eye and star keep their size, and Rename / Delete / Adopt and
  the sliders stretch, their labels ellipsizing when narrow), down sets
  how tall the list may grow before it scrolls. It follows the finger's
  motion from the press, the list from its **real** height rather than its
  limit (so on a short list dragging up shrinks it at once). At least 180
  px wide and one row tall, clamped to the board; kept in localStorage
  (`gates.web.layers.size.v1`), not in the save, since it is about the
  screen rather than the board. The grip captures the pointer, so a drag
  from it never pans the board.
- The panel redraws only when its model changes, and never under a finger:
  the sliders are built once, and a colour is reported on `change` (the
  picker closing), since rebuilding a row mid-pick would close the picker.

### The Dev folder

Every number tuned by eye, and one behaviour switch, as dials in the
panel's **Dev** folder ([tune.js](tune.js)): belt width, chevron size,
overlay width and edge, port size and edge, glyph size and weight, the
Source value's size, grid opacity, ghost opacity, and the tap margin round
a ghost or a run's tip. Scales multiply the shipped value, so 1 is always
"as shipped", and **Reset dials** puts them all back. They persist with the
panel's preferences; a stored key this build does not know is ignored.

**It also holds controls moved out of other folders because they are
rarely changed**, under sub-folders named for where they came from, so
their grouping reads the same: **Dev > View** (grid lines, ports, source
values, pan momentum, stalled but moving: green, pixel max size) and **Dev
> Audio > belts** (follow tick rate, at tick rate). Only where they show
moved: they are still View and Audio settings, under the same keys, so
presets, presentation views, saves and Reset audio dials treat them as
before, and Reset dials leaves them alone. Moving another one is a
line in panel.js (`DEV_VIEW` / `DEV_AUDIO`); its sub-folder is made from
the group it was in. The Dev folder sits just above Debug.

Below a divider, two sub-folders of **zoom thresholds**: the zoom (CSS px
per cell) below which belts turn to solid squares (0 by default: never), and from which the grid,
glyphs and Source values, ports, port edges and belt chevrons draw, and
from which items are ringed discs rather than plain squares (6); and the
**text fades**, in px of text height (what legibility depends on) -- item
numbers fading in between two values, and a label's text appearing from
one. `render.js`'s `LOD` reads them live through getters.

**View > simple view** is exactly override zoom 1, and wins over the Dev
dial while ticked (unticked, the dial applies as usual). It is in View, so
it rides presets and presentation views.

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
  **Ascending** (0, 1, 2…), **Doubling** (1, 2, 4…, clamped at 2²⁴),
  **Alternating** (0, 1, 0, 1…),
  **Hack…** (below), or **File…** (numbers separated by commas or whitespace, repeated to fill
  the index; `D` is the default: 0 for a Source, no seed for a belt; any
  other token rejects the file with its line) -- then **a** and **b**
  (1 and 0), so the value is `a*x + b`, float32, clamped; a default stays a
  default. A live preview lists the first dozen; **Apply to n** writes the
  values into the lifted entries and puts the selection down: one edit,
  one undo step.
- **Position has three switches** on the bar while it is the order:
  **Transpose** (up each column, columns left to right), **Reverse X**
  and **Reverse Y**. Together they reach all eight ways a rectangle can be
  read, so a block placed rotated or mirrored is still numbered in its own
  reading order. Session-only (`shell.state.indexOrder`).
- **Hack…** is the sixth table: a program saved in the Assembler (below),
  assembled on the spot. Its words fill from #0 and the rest get 0; a
  program longer than the index is cut short, said in the preview and in a
  toast on Apply. a and b do not apply (they are disabled): the values are
  machine code, written as **unsigned** integers (a C-instruction is
  57344 or more). A program with errors is refused in the preview.

### The assembler

The panel's **Assembler** folder opens a full-screen Hack editor
([assembler.js](assembler.js)) over the app; the simulation pauses while it
is open and resumes on close if it was running. Its only link to the board
is indexing's Hack… table. [hack.js](hack.js) is the pure assembler,
tested in `tests/hack.test.mjs` (the book's Max.asm, word for word
but for the comp bits).

- **The comp bits are not the book's.** `COMP_A` holds the control bits of
  this project's own ALU, which decodes c1..c6 differently: `1`, `-1`, `D`,
  `A`, `!D`, `!A` and the four ±1 forms all differ (`D+1`/`D-1` and
  `A+1`/`A-1` are each the book's codes swapped). The `a` bit and the bit
  order are standard, so an M form is still its A form with `a=1`. A test
  pins the whole table, since a transposed pair still assembles cleanly.

- **Standard Hack**: `@n` (0..32767) or `@symbol`, `dest=comp;jump`,
  `(LABEL)`, `//` comments, whitespace ignored. Predefined symbols (SP, LCL,
  ARG, THIS, THAT, R0-R15, SCREEN, KBD) are one table, `PREDEFINED`, and
  variables are allocated from `VAR_BASE` (16): change either there for a
  platform with a different memory map. Two leniencies: dest letters in any
  order, and the commutative comps either way round (`A+D`).
- **`#define NAME replacement`**: plain-text replacement, in definition
  order (a define's replacement is expanded by the ones before it), over the
  whole file. `#define gg @` makes `gg100` read `@100`. No word boundaries,
  on purpose.
- **Highlighting is a textarea over a coloured copy** of its text, scrolled
  in step. Every edit re-assembles: a bad line is tinted and its error is in
  the status strip (tap to jump). The gutter shows each instruction's ROM
  address. **Binary** lists the words, with a **decimal** toggle.
- **Programs save to the browser** ([hackstore.js](hackstore.js), one
  localStorage key), import and export as `.asm`. The working text is kept
  as a draft on every keystroke, so closing loses nothing; New, Load and
  Import over unsaved changes, Delete, and saving over a different program
  all ask first.

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
  nothing; above 0, `round(Z)` clamped to 1..Dev > View > pixel max size (64 by default: in practice
  the Pixel's own Z sets the size), as half
  the side in cells -- Z = 1 covers exactly the machine (2x2), Z = 2 a 4x4,
  centred on the machine's centre, which is always a grid corner, so every
  square sits on the grid.
- **A V above 1 is a character.** `floor(V)` is an ASCII code and its
  fractional part the value -- except a fractional part of exactly 0 means
  *full* value, so 55 is a bright `7` and 55.5 a half-bright one (55.25 is
  a quarter, not three quarters). 33..126 draw their glyph in a monospace
  font, scaled so the *measured ink* from the top of `M` to the bottom of
  `g` is the square's height, centred -- not the font's declared cell,
  whose metrics vary by browser and pushed glyphs up out of the square on
  a phone;
  32 draws an empty square; every other code draws the ordinary square at
  that same fractional value. V of exactly 1 is still a plain full square.
  **View > pixel text on black** puts a black square behind a character
  (and behind a space); off, the board shows through. The glyph is drawn
  at a fixed 100px font and scaled into place, since canvas text a few
  world units tall is quantised or floored by some browsers.
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
tick half of `engine.py` -- with the desktop's schedulers left out (no
constant folding, no GPU) and **a wake list of its own** (below), exact
where the desktop's gate sleep was a heuristic. The other optimisations are
the closed-form belt (a run ticks in O(1) however many items are on it) and
the gate prefilter (an occupancy test rejects gates that cannot act before
the full firing rule runs). One module, no DOM, so Node can run it.

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

**What the tick costs, and the two fixes that took it 4-6x.** Measured in
Node on `test` (3.4k machines, 2k runs): 0.74 ms a tick before, 0.13 after
(`cpu.npz` 0.11 -> 0.028, `Ram2.npz` 0.074 -> 0.018, `GameOfLife_SLOW`
2.0 -> 0.70); in the editor at 10000 tps with the whole board on screen,
555 -> 2376 ticks a second reached. Both are exact -- `check_web_sim.py`
passes on every save, `test` and `GameOfLife_SLOW` included:

- **The prefilter tests the connected outputs.** It asked whether *any*
  output was free, and a dangling output always is, so every Source with an
  unconnected face passed and `fire()` ran ~1770 times a tick to fire about
  once. It now asks `fire()`'s own question of `drains`: a generator needs
  one free, anything else all of them.
- **A run's ports are plain numbers** (`srcRow`/`srcCh`, `dstRow`/`dstCh`,
  resolved in `buildGates()`). `offer()` and `accept()` re-ran `livePort()`
  -- an alive flag and a grid read for the ghost bit -- on every run every
  tick, ~2000 calls to learn what only an edit (a rebuild) can change.

That left three scans over everything -- every gate, every run, every
direct edge -- to find the few with work: on `test` 0.27% of runs and 0.08%
of gates change in a tick. The wake list is what removed them.

### The wake list

`Sim.sleepStep()` visits only what can act; `Sim.fullStep()` is the old scan,
kept for the forecast and as the reference. **They are the same tick** --
every rule below only ever skips work that would change nothing -- and
`check_web_sim.py` still passes on every save against the desktop. The
reasoning is in the comment above `makeSched()`; in short:

- **A gate's firing reads only its own row**, so it is asked again only
  when one of its channels was written (`touch()`, from the four channel
  setters). In row order: Pixels fired in one tick paint in that order, and
  a spatial recorder sums float gains in it.
- **A direct edge** is checked only when its source or target row changed
  since the last hand-off. All decided, then all applied, as before.
- **A run touches only its own two lanes** (each lane has exactly one run
  or edge on it), so runs never see each other within the belt phase, in
  any order. One that ticks with no pop, no push and its anchor at the end
  sleeps until its producer fills the zone it takes from (`setOut()` wakes
  it), its consumer empties the lane it pops into (`clearIn()`), or -- by
  the closed form alone, with nobody ticking it -- its head reaches the end
  or room opens behind its newest item: a **timed wake** at entry +
  length / speed or entry + spacing / speed (`wakeAt`). That is the case
  the desktop's belt sleep once got wrong (a seeded item sliding down a
  sleeping run, never popped); here the arrival tick is computed. Asleep, a
  run's anchor is always at the end, so its skipped ticks lose nothing.
- **Anything structural wakes everything** -- an edit's rebuild, Reset, a
  bake's restore, a clone, a new audio recorder (`makeSched()`) -- and
  waking too much is always safe, so every rule errs that way.
- **The forecast runs `fullStep()`** with the scheduler set aside, so it
  has nothing of the wake list's to roll back.
- **Audio rides on it.** The recorder no longer scans: the sim calls
  `Recorder.hit(seg)` for each run active on the tick -- every run it
  ticks, by the drone's rule as before, and every sleeping run whose newest
  item is still sliding into place, by the closed form (`moveUntil`,
  `movers`; with the anchor at the end it moves on every tick before entry
  + its packed place / speed). The bits are identical to the scan's. Only
  **candidates** (`seg.aIdx >= 0`) are asked or tracked as movers: `hit()`
  ignores every other run, so the rest cost nothing (see "Muted belts cost
  nothing").

**Measured** (Node, `step()` alone, best of 5):

| board | full scan | wake list |
|---|---|---|
| `test` (3.4k gates, 2k runs) | 102 µs | **4.4 µs** -- 230k ticks/s |
| `Ram2.npz` | 22 µs | **0.9 µs** |
| `cpu.npz` | 21.5 µs | **6.1 µs** |
| `GameOfLife_SLOW` (15% of runs active) | 690 µs | **265 µs** |

**Debug > wake list** switches it off (and on, waking everything), for
comparing: the same ticks either way, only the cost differs. Kept across
board loads in the session, not stored.

So the tick now costs what the board *does*, not what it *holds*: per tick
on `cpu.npz` about 13 gates asked, 12 runs ticked, 12 edges checked. On the
main thread, `GameOfLife_SLOW` reached only ~500 tps: its frame is 27 ms of
rendering, and the step got its 12 ms budget per frame, so the frame rate
capped the tick rate. Moving the tick to a worker (below) is what unhooked
the two.

**`tests/sleep.test.mjs` is the differential check**: a sleeping and a
full timeline on six saves (and four more at belt constants other than
the shipped ones), compared every tick -- every ring, counter and anchor,
every gate channel, the activity stamps, the Pixel screen in paint order,
and the audio recorder's bits and fire counts -- with a recorder attached
partway and a Reset halfway. It also asserts the wake list actually slept
(under 60% of the work visited) and fired timed wakes, so a run that never
slept cannot pass it. **Mutation-tested, eleven ways**, every one caught:
no timed wakes, no head-arrival wake, no room wake, no wake on a consumer
emptying or a producer filling, a pop's input or a push's emptied zone not
marking the gate, direct edges never checked, sleeping with the anchor
short, sliding sleepers not heard, gates asked out of row order. Three of
those passed at first: the room wake and the anchor rule cannot matter at
the shipped constants (spacing == speed: a run with no room has just
pushed, and one tick after a pop the anchor is back), hence the runs under
other constants; and gate order shows only on the Pixel screen, hence
comparing it.

### The tick on a worker

While the sim plays, the ticking happens on a **Web Worker**
([simworker.js](simworker.js), driving a `SimHost` from
[simhost.js](simhost.js)), so the tick rate is no longer capped by how long a
frame takes to draw. The ceiling is `RATE_MAX`, a million tps.

| board | main thread | worker |
|---|---|---|
| `test` | 100k (the ceiling) | 100k (the ceiling) |
| `GameOfLife_SLOW` | ~500 | **~3,700** |

(Headless Chromium, the whole board on screen.) At 3,700 tps Game of Life
is the worker's own CPU: 265 µs a tick. The frame cost of the worker is
applying its delta, 3.6 ms on Game of Life.

**The main thread keeps its Sim as an exact mirror.** Each frame it asks the
worker for a delta -- what changed since the last one, collected by a
`Tracker` the worker's Sim calls from the same places the wake list hears of
a change: the runs ticked, the ring slots pushed into, the gate rows whose
channels were written, the Pixel fires in order, the audio recorder's bits.
`applyDelta()` writes it into the mirror, so everything that reads the sim
-- the renderer, the forecast, status colours, Status, bakes, video -- reads
the mirror and needed no change. A delta is small because a tick changes
little; that is the wake list paying twice.

**Edits stay on the main thread**, which owns the Board. After the mirror
rebuilds, `Sim.onRebuild` sends the board's arrays and the run-id
allocator's state from just *before* that rebuild (`Sim.lastAlloc`, taken
after the old runs' ids are released), and the worker rebuilds the same way
(`allocOverride`) and so numbers every run the same -- deltas are keyed by a
run's index and a gate's row. Taking the allocator before the release was
the first version's bug, and broke every test from tick 1. Anything the
worker ticked past the mirror on the old board is dropped: the board
message carries the mirror's tick.

**The main thread still steps the mirror itself** while a baked soundtrack
plays (the audio clock decides each frame's tick), and whenever the worker
is off. Taking over pauses the worker and bumps the **epoch**, so any delta
already in flight is dropped; the next time the worker plays, it is sent the
mirror's whole dynamic state first (`exportFull`/`applyFull`, with
everything awake). A bake's restore does the same. Every board or state
message bumps the epoch, and a delta carries the epoch it grew from, so a
delta can never land on the wrong state. A Bake pauses and resets in one
go, so a board message sent while the clock is paused but the worker is not
yet told pauses the worker first -- otherwise it ticks the new board past
where the mirror stands.

Smaller parts:

- **Audio.** The recorder no longer hears ticks it did not run: the worker
  has an `AudioTap` filling the same bits and click counts from the
  recorder's own configuration (`Recorder.tapConfig()`, sent whenever its
  generation or the gains change). Measured identical to the main-thread
  recorder: 244,083 run bits and 96,328 clicks either way over `test`'s
  active stretch. With sound on, the tap's ticks go **straight to the
  synth** down a port of their own, not through the deltas -- see
  "Realtime at high tick rates" under Audio; without that line (the tests'
  default) they ride the deltas and `Recorder.ingest()` appends them. A
  new tap wakes the worker's Sim, as `Recorder.configure()` does: the wake
  list hears a sleeping run still sliding into place only if it was told
  of it, and without the wake those runs went unheard until something
  woke them. The first worker test compared only ticks after a takeover
  (which wakes everything), so it never saw this; the direct-line test
  did, on its first run.
- **Alpha.** Items glide between deltas: `clock.frac` is the worker's frac
  when it answered, plus the time since times the rate, held below the next
  tick, which the mirror does not have yet.
- **A background tab.** The worker stops ticking when it has not been asked
  for a delta in `IDLE_MS` (500 ms), and starts again without catching up at
  the next request -- what the main thread did when a hidden tab's frames
  stopped. Ticking on unseen would grow the delta and its audio without
  bound.
- **The loop** runs a slice of at most `SLICE_MS` (6 ms) at a time, so a
  request or a pause is never kept waiting, and chains slices through a
  `MessageChannel` post rather than `setTimeout`, which clamps to 4 ms. At a
  slow rate it sleeps until the next tick is due.
- **Debug > sim worker** switches it off, for comparing; off, `paint()`
  steps the mirror within `STEP_BUDGET_MS` as before. If the worker fails to
  start (or posts an error), it stays off for the session and the main
  thread ticks.

**`tests/worker.test.mjs` holds the mirror to a reference timeline** -- an
ordinary Sim on its own copy of the board -- after every delta, on three
saves: every ring, counter, anchor and channel, the activity stamps, the
Pixel screen in paint order, and the recorder's bits and clicks. Both ends
run in one process and every message goes through `structuredClone`, so
anything that would not survive the trip to a real worker fails there too.
The cases: deltas of 1 to 60 ticks; an edit and a Reset with the worker
ticked ahead unseen; the main thread taking over with a stale delta in
flight; a worker started late, after edits; audio recorded under a sound
selection that changed while it was in flight; the loop's rate, pause and
idle. **Mutation-tested, eleven ways**, ten caught: untracked pushes or
ticked runs, the allocator or the rebuild's tick not carried, stale changes
kept across a rebuild, Pixel fires not replayed, a full state without its
rings, a board message keeping the old epoch, a worker that never idles, and
stale audio ingested (which needed its own case: the selection reconfigures
the recorder with no rebuild, so the epoch alone cannot drop it). The one
survivor is equivalent: the fire stamp's row mark, since every fire also
writes one of that row's channels, which marks it anyway -- kept so the
stamp does not rely on that. In the browser the same comparison held
through edits while playing, pause and resume, a Reset while playing, the
wake list switched off mid-play, Pixels, and a bake's playback handing over
to the worker at its end.

One trap: a version of `deliverDirect()` over flat `Int32Array` offsets
measured 25x *slower* in the step (0.38 ms) and was also wrong -- it ended
`test` with 1922 items in flight where the real tick has 1696. Reverted; the
check would have caught it, and the timing alone would have looked like a
V8 oddity.

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
When the main thread ticks (see "The tick on a worker"), `paint()` also
stops stepping after `STEP_BUDGET_MS` (12 ms) of a frame and
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
belt run and a click per gate fire. **On by default, and remembered across
sessions** (`gates.web.sound.v1` in localStorage, written only by the
player's own tap). A browser starts audio only from a gesture, so a launch
with sound on shows the button on and starts it on the **first tap or key
anywhere** (`armSound()`: pointerup, touchend, keydown -- the events that
count as activation on a touch screen; a tap on the sound button itself is
left to its own click, which turns sound off). It is switched by the
**sound button** in the board's top-left corner -- a speaker, crossed out
while off -- not from the panel; the Audio folder holds the meter and the
dials. The dials travel **with
the save** (below) and are also kept in the panel's preferences as the
last ones in front of the player; the switch is never in a save.

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
  green"). The sim reports it (`Recorder.hit()`, see "The wake list"), so
  it costs nothing for a run that is asleep and still. Its drone has
  pitch **min-length Hz × min length / length** (the sim's length, buried
  underground tiles included): the dial is the pitch of the shortest run
  that sounds, so raising min length transposes everything up and the
  shortest audible run stays at the dial's pitch, 10 tiles is still an
  octave above 20, and every run is a subharmonic of that pitch. (The key
  is still `beltMaxHz`, so saves keep it; at min length 1 nothing moved.) Runs outside **min length** to **max
  length** get no voice (the recorder leaves them out of the candidates).
  Attack and release dials gate it per tick: short, a run moving every
  other tick buzzes at half the tick rate; long, it smooths to a drone.
  The dials are a **10-tile run's** times: **sluggishness** s (-2..2, 0 by
  default) scales them by (length / 10)^s, so at 1 a 20-tile run is twice
  as slow and a 5-tile one twice as quick. Per voice (`envFor()`), set on
  allocation and on any dial change, so a sounding voice follows it.
  **Follow tick rate** (Dev > Audio > belts, with its reference, **at
  tick rate**) scales the pitch by (rate / reference)^track; 0
  (the default) keeps it fixed.
- **A gate fire is a click**: an impulse into a two-pole resonator (tone,
  decay), per group -- **0: Swap, Route, Merge; 1: everything else**;
  Sources are silent (`audioGroupOf()`). Only the per-tick **count** per
  group crosses threads, so fires cost the same on any board.
- **Click timing** (a sub-folder; `clicks()` in audio-dsp.js). **Jitter**
  (ticks, 0..4, default 1): at 0 a group's fires in one tick are a single
  impulse at the tick's start -- many fires only louder, and a buzz at the
  tick rate, the original sound. Above 0 each fire is its own click at a
  uniformly random time up to `jitter` ticks after its tick's start: a
  Poisson process conditioned on the tick's count. 1 spreads fires through
  their own tick; more blurs the tick grid away (at the cost of up to that
  many ticks of extra delay). Many fires then add in **power**, not
  amplitude, so a busy tick is denser and quieter than at 0 --
  deliberately: it should sound like many events in sequence. The times
  are a hash of (tick, group, fire), not `Math.random`, so a bake is still
  bit-identical however it is chunked: offline a fire at offset o in tick k
  lands at sample `round((k + o) * sr / rate)`; realtime it follows the
  playback speed. A group's fires in a tick become at most 64 clicks (the
  weight shared), and at most 8192 are scheduled ahead (past that, one
  plays at once). A stop drops the scheduled ones. **Detune** (semitones,
  0..12, default 0): each group has five resonators, at the tone and spread
  ±detune about it, and each fire picks one at random, so a dense patch
  rings on several pitches rather than one struck faster. At 0 every fire
  uses the first, which is exactly the single resonator.
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

**The scale** ([scale.js](scale.js); Audio > **Customize scale…**). A
scale is a **wavelength** function f(n), normalised so the min-length run
keeps the dial's pitch: `Hz(n) = (min-length Hz) × f(m) / f(n)`, m the min
length. f(n) = n is the original law (the Standard preset), and
`tests/scale.test.mjs` pins that it gives exactly the old pitch.

- **The sheet sits at the top of the screen** (`.sheet-top`), for room,
  and scrolls if the screen is short. Top to bottom: a short scrolling
  list of the presets with their formulas, ending in **Table…**; the
  formula box; copies of the **min-length Hz** (logarithmic, 50-8000,
  snapped to 10) and **min length** dials, so the pitch can be retuned
  without leaving the sheet (they and the panel's are the same dials);
  then **Notes** or **Lengths** below.
- **Notes** lists the lengths from the min length with Hz, the nearest
  note and cents. **Lengths** is a histogram of every run that may sound
  -- inside the min and max length and, with an audio selection, touching
  it -- one bar per length, coloured by the note it plays (hue = pitch
  class, so a scale's shape shows), heights on a **square-root** scale (one
  length usually dwarfs the rest, and a lone run would be a pixel tall).
  A tap or drag on it reads out that length's count, Hz and note. The runs
  come from `eligibleRuns()` in audio-params.js, which the recorder now
  uses too, so the plot and what sounds cannot disagree.
- **Presets are formulas** (Standard, Semitones, Major, Minor, Harmonic
  minor, Pentatonic, Minor pentatonic, Blues, Whole tone, Dorian): picking
  one fills the formula box, so a preset is a starting point to edit.
  **Table…** loads a file; with one loaded and a formula playing, it
  switches back to that table instead, and tapped while the table plays it
  loads a new one. The
  scale ones are `2 ** (major(n - m) / 12)` and so on: longer runs walk
  down the scale from the min length, which is the root.
- **The formula language** is parsed, never `eval`ed, since a formula
  rides in save files: numbers, `n`, `m`, `pi`, `e`, `+ - * / % ** ^ ( )`
  (`%` never negative, `**` right-associative, `-2 ** 2` is -4), and
  `floor ceil round abs sqrt exp log log2 log10 sin cos tan sign fract min
  max pow mod clamp`, plus one function per scale (`major minor harmonic
  pent minpent blues whole dorian`) giving degree k's semitones, continuing
  by octaves both ways. Errors name what went wrong.
- **A table file** ("Table…"): numbers separated by spaces, commas or
  lines; the i-th is f(i) from n = 1, and past the end the last holds.
- **It applies as it changes.** Every edit that parses goes straight to
  the dials (`setScale()`): the synth re-pitches sounding voices on its next
  tick. A half-typed formula shows red and the last good one keeps
  playing. The preview lists the lengths from the min length, with Hz and
  the nearest note and cents.
- **The synth compiles it** (`scaleTable()` in `applyParams()`, only when
  the formula, table or min length changed), so live playback, a bake and
  a video's soundtrack read the same dials the same way. Three dials:
  `scaleMode` (`formula` / `lut`), `scaleExpr`, `scaleLut` (the numbers as
  text); saved with the board and the panel's preferences like any dial,
  and a bake under another scale goes stale. A length whose f is not a
  positive number falls back to the standard pitch, and the preview says
  how many did.

**Tone** (a sub-folder): a gain per run length for **lengths 1 to 6** --
the shortest runs are the highest pitches, and the usual thing to tame --
with 7 and longer at 1. Per voice, not a filter, so it takes exactly those
runs and nothing else (a filter would also take the upper harmonics of
longer runs under a triangle or saw); it follows the dial on voices already
sounding. **Pitch tilt** (0..2) multiplies that by (length / min length)^tilt:
a min-length run is unchanged and each doubling of length is +6 dB per unit
of tilt -- about what a small speaker (and an ear) loses per octave down,
so near 1 a single run sounds about equally loud at every length and above
1 the low runs dominate. Capped at +24 dB. Phone speakers barely play
below 150-200 Hz at all, which the waveform dial helps with too: a saw
gives a low run harmonics the speaker can reproduce. **Low cut** is a 2nd-order Butterworth high-pass on the whole mix
against rumble (0 Hz is off), per sample, so a bake stays bit-identical.

**Start tick and fade** (`startTick`, `fadeMs`): the synth is silent while
the tick it plays is before the start tick, then fades in. It keeps
following the runs while silent, so a drone already going at the start
tick fades in rather than starting cold. Each batch carries its first
tick's number (`tick0`) for this. Default 0 and 0: no change.

**Set audio selection** (a button): with entities selected, only they
sound -- a run if any of its cells is selected (underground ends count),
a gate if any of its cells is -- and with nothing selected everything does
again. The cells are taken where the selection sits at the press, and kept
after deselecting; the meter adds "sound selection: n runs, m gates".
Unselected gates click in the silent group, as a Source does, so it costs
nothing per tick, and **normalisation counts only the selection**, so an
isolated part fills the mix. A change of sound selection makes a bake stale,
and a video's soundtrack uses it.

**Set audio region** (the next button) is the other way to choose: a
**place**, not a set of entities. The button arms it ("drag a box…"); the
next one-finger drag on the board, in Build or the Hand, draws a box (a
still hold then a drag starts it at the finger, not a selection), and
every run and gate touching the box sounds -- whatever is built, moved or
deleted there afterwards, since the recorder re-asks on every rebuild. It
shows as a dashed cyan outline (an editing mark, not in videos). A tap
while armed, or a box touching no entity, goes back to everything. Edits
never reset it, only choosing again (either kind -- they replace each
other) or loading a board. Its `cells` is an object with `has(y*W+x)`
rather than a Set: that is the only call the recorder, `eligibleRuns()`, a
bake and a video make of a selection, so none of them knows which kind
they have. `tests/region.test.mjs` checks a region picks exactly the runs
touching its box on `cpu.npz`, and that a run built inside it afterwards
becomes a candidate.

**Space: camera-aware audio, and soft edges** ([space.js](space.js);
Audio > **space**). The region is no longer a cell filter but a **space**: a
box, and every run and gate weighted by its distance from it. Inside
(anything touching the box) is full volume and unfiltered; out to the
**soft edge** -- a fraction of the box's longer side, so a camera box feels
the same at any zoom -- the volume falls along a half cosine to 0, and the
**distance low-pass** (a one-pole per voice) closes from 18 kHz by up to 7
octaves at the fringe's edge. Beyond it nothing is heard. **Soft edge 0 is
a hard edge** -- touching or nothing, exactly the region as it was -- and
both dials default to 0.

- **Camera-aware** (a switch) makes the box the screen -- the whole board
  view, scaled about its centre by **screen scale** (0.5: a box half the
  screen's size). It takes over from a drawn region while on. An entity
  selection still narrows what can sound, and the box weighs it.
- **Panning never re-voices.** The candidates (length window and entity
  selection) are fixed; distance is a per-candidate gain and cutoff sent
  as their own message (`Recorder.setSpace()` -> the synth's `space`),
  only when the box moves by a hundredth of a cell or a dial changes
  (`spaceKey()`). A reconfigure would orphan every voice. On `cpu.npz`
  panning took the heard runs from 110 to 245 of a fixed 613.
- **Out of earshot takes no voice**: a run at gain 0 is not voiced even
  while active (and not counted as refused), so a big board seen through a
  small screen does not fill the voice limit with silence; moving it into
  earshot voices it at once.
- **Gains glide** (40 ms) per voice, and the drones normalise by the
  weights' **sum** rather than the count -- glided too, since it moves with
  the camera -- so a screen's worth fills the mix as an isolated part does.
- **Gates click by their weight**: a fire adds its gate's gain to its
  group's count, and groups normalise by the gains' sums. They get the
  falloff but not the low-pass, since a group's clicks share one
  resonator.
- **Shown** as the box dashed and, with a soft edge, the fringe's limit
  dotted with round corners (distance is Euclidean). The region's always
  shows; the camera's only while the Audio folder is open in an expanded
  pane. The video frame deliberately does **not** follow that rule (see
  "Video"): it stays up with the pane collapsed. A sub-folder folding inside Audio or Video bubbles a fold
  event up; those are ignored by target.
- **Bakes and videos**: a bake uses the space at the press; a video's
  soundtrack with camera-aware on hears the **video's** frame, which moves
  with the end camera (`videoSpaceAt()`: tick -> frame -> crop box, scaled).
  A bake does not follow a pan made after it.
- Without a space every path is unchanged to the bit (the existing audio
  tests, bit-identical bakes included, pass untouched);
  `tests/space.test.mjs` pins the curves, a hard edge being exactly
  "touching the box" on `cpu.npz`, the soft fringe, and the synth: no
  voice at gain 0, re-voiced on a pan without a reconfigure, and the
  low-pass cutting a saw's high-frequency energy tenfold.

**Any change to the board resets** an entity selection, and since selecting is itself an
edit here (the selection is lifted off), "a change" means a change of
**content**: `Board.contentHash()` -- every occupied cell's type, facing,
footprint offset and flags, a Source's value and a belt's seed value, but
**not row ids** (putting a selection back where it was can take different
rows) and not gate channels (rewritten every tick). The reference is the
first stable board after the press, so deselecting in place keeps it and
moving the selection, any other edit, an undo or a load resets it (labels
are not content). `Recorder.setSelection()` syncs the sim and reconfigures
at once rather than at the next tick -- configuring against a sim that had
not yet seen the lift read rows the lift had freed. Tested: the hash
survives a lift and put-back, ticking and a label, and moves with a moved
machine or a Source's value; the recorder takes exactly the touched runs
and gates.

**A slow tick rate must not kill the worklet.** At 5 tps one tick lasts
200 ms, longer than the skip threshold (3 × 60 ms latency): starting
playback took the only queued tick, and the skip test, on the depth
measured *before* that, `skip()`'d an empty queue -- a throw, and an
AudioWorkletProcessor that throws is silent for good, so sound came back
only by switching it off and on. Now the start threshold is at least 1.5
ticks (`max(latency, 1.5 / rate)`), the depth is re-measured after the
start, `skip()` returns on an empty queue, and the worklet catches any
throw, stops the synth (it restarts on the next ticks) and posts
`{type: "error"}`, which main.js logs. Pinned by a test streaming 1000 →
5 → 10 → 2 → 1000 tps that asserts no 100 ms window goes silent.

**The queue's lag cap scales with the tick.** It drops the oldest ticks
past 1 s queued, which at 1 tps is one tick -- less than the 1.5 ticks
playback waits for, so ticks were dropped and the drone moved every other
tick (or, fed perfectly evenly, never started). The cap is now at least
`(SKIP_AT + 1) * 1.5` ticks; at 1 tps the audio runs about 1.5 s behind
the picture, the price of never running dry between ticks. Pinned by a
test that every tick plays at 0.5, 1 and 2 tps.

**Hold on underrun** (off by default): when the queue runs dry, the voices
hold where they are instead of releasing, and playback picks up the moment
ticks arrive, with no refill wait. A hold gives up after `HOLD_MAX_S`
(0.5 s); a pause is still a stop. It is the realtime remedy for stutter at
busy moments; baking is the thorough one.

### Realtime at high tick rates

Above about 20k tps the realtime sound hiccuped where a bake of the same
ticks did not. Two causes, both fixed, and neither touches a bake:

- **The synth's cost grew with the tick rate.** Realtime playback cut
  every 128-sample block at every tick boundary and ran every voice's
  setup for each piece -- at 20k tps a piece every 2.4 samples, at 100k
  every sample -- so the voices' setup, not their samples, was the cost.
  `renderBlock()` walks the block's ticks first, applying them at the
  same samples and logging each voice's on/off changes with their
  offsets, then renders each voice over the whole block, switching at its
  own change points: O(voices + changes) a block instead of O(ticks x
  voices). In Node, per 128-sample block (budget 2.67 ms):

  | board | rate | per tick | by block |
  |---|---|---|---|
  | `cpu.npz` | 20k | 0.32 ms | **0.21** |
  | `cpu.npz` | 100k | 0.79 ms (worst 2.64) | **0.30** (worst 0.46) |
  | `test` | 20k | 0.34 ms | **0.20** |
  | `GameOfLife_SLOW` | 20k | 0.62 ms | **0.48** |

  What remains on Game of Life is real work a bake does too: applying
  6,386 runs' bits a tick, and scheduling each jittered click.
- **Ticks reached the synth once a frame.** The worker's ticks went to the
  main thread in each frame's delta and on to the synth from there, so a
  frame's gap plus the audio thread's own bursts (3-4 blocks at a time in
  Chromium) had to fit in the latency. They did not always: `test`
  carries a 20 ms latency in its save, and with frames 17-48 ms apart it
  ran dry about three times a second, at **any** tick rate. Now the main
  thread hands the worker a port into the synth (`audio.js tickPort()`,
  the worklet's `tickport` message) and the worker posts its tap's ticks
  down it every `AUDIO_POST_MS` (5 ms) and on every delta request; a
  pause sends the last ticks and then `stop` down the same line, so
  nothing arrives after it. In the browser, `test` went from 14-54
  underruns per 5 s to **none**, at 1k, 20k, 100k and 1M tps. Configs
  still come from the main thread, on the worklet's own port, so the two
  are not ordered: configs and tick batches both carry the recorder's
  generation, and the synth drops a batch from another one.

**Muted belts cost nothing.** With the belt gain at 0 -- and the falsy
belt gain too while truthiness is on (`beltsMuted()` in audio-params.js)
-- no run is a candidate: the recorder (and so the worker's tap) records
no belt bits and ships none, the sim skips the audio's per-run work, and
the synth keeps no voices and walks no bits (`dropVoices()`). Each voice
would only ever have added exactly 0.0, so the sound is **bit-identical**
to before -- checked end to end against the previous commit's sim,
recorder and synth on `cpu.npz` and `GameOfLife_SLOW`, at 240 and 40k
tps, realtime and offline, with truthiness and with vibrato and noise:
not one sample differs. Unmuting reconfigures, and the next tick voices
every run active then. Before, a muted belt cost everything a sounding one
did. Per 128-sample block at 40k tps (budget 2.67 ms, Node):

| | sounding | muted, before | muted, now |
|---|---|---|---|
| `GameOfLife_SLOW` | 0.59 ms | 0.59 | **0.25** |
| ...with vibrato | 0.89 | 0.86 | **0.25** |
| `cpu.npz` | 0.16 | 0.15 | **0.02** |

And the recorder's share of the tick, on `cpu.npz`: +46% sounding, +27%
muted; the belt bits shipped (up to 32 MB/s on Game of Life at 40k tps)
go to nothing. **Muted gates already cost nothing**: `clicks()` returns at
gain 0 and a silent resonator is skipped. `tests/audio.test.mjs` holds
both halves: muted sounds exactly like no belts at all, with no voices;
and a sounding, muted, sounding round trip voices the same runs as a
synth never muted (mutation-tested: walking the bits while muted, and not
forgetting them on mute, each fail it).

Where a high tick rate's time goes otherwise, on `GameOfLife_SLOW` at 40k
tps: about 0.20 ms a block walking every candidate's bit each tick, 0.26
ms scheduling jittered clicks (jitter 1 times up to 64 clicks per group
per tick separately; jitter 0 is one impulse, almost free -- but louder,
since it adds in amplitude), 0.12 ms for 256 voices -- and **+0.3 ms when
vibrato, tremolo or truthiness is on**, which puts every voice on the
two-oscillator path. The noise exciter measured as free.

**The meter shows "ran dry n · skipped m"** once either happens: the
queue running out of ticks (an underrun: they came late, so raise the
latency or lighten the frame) or a backlog skipped. Counted since sound
was switched on. Its "refused" count could also read -1 after an underrun,
since `gatesOff()` zeroed the count and left each run's refused flag set;
it clears both now.

**It is the same sound, to the bit**: the per-tick renderer is kept as the
reference (`synth.batch = false`), and `tests/audio.test.mjs` renders a
real save's ticks both ways from 30 to 100k tps and compares every sample.
That took moving one thing in the reference: realtime now frees finished
voices at the end of each render call rather than at tick boundaries
inside it, so a refused run waits up to a block for a voice. (A bake still
frees on tick edges; its exactness is about chunking, not this.) Three
more details the walk needed: a voice allocated mid-block runs silent from
the block's start with its phase wound back to 0 at its offset; a click
due "now" lands at its tick's sample, not the block's first; and the click
pool **grows** (to 2^20), since a block schedules all its ticks' clicks
before any play -- 300k tps of 64 fires a tick is 51k clicks a block. One
deliberate difference: with a space, a voice's gain glides once a block,
not at each tick boundary, which the test holds to under 0.1% of the
signal. Mutation-tested eight ways (a change logged a sample early, a
run's off not logged, the phase not wound back, either kind of click at
the block's start, the start tick ignored, voices never switching
mid-block, the pool not growing): each fails it -- three only once cases
for unjittered clicks, a start tick and a click burst were added. The
direct line is mutation-tested in `tests/worker.test.mjs`: the new tap's
wake, audio left in the deltas, no send on a request, the stop before the
last ticks, the synth ignoring the generation.

### Baking

**Bake audio…** (Audio folder) opens a sheet: seconds (default 30, at most
300) and ticks per second (default the current rate), **Bake**, **Cancel
bake**, **Play / Pause** and **Download WAV** (16-bit mono). Play / Pause
plays the baked buffer on its own -- the simulation untouched -- from where
it was paused, from the start once it has played through; on the sound's
context, or one made in the tap when sound is off. Closing the dialog, a
new bake or Play on the board stops it. [audio-bake.js](audio-bake.js)
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

### Truthiness in the audio

Audio > **truthiness** splits the sound by the bits above (Sim.truthOn is
on while it is). Off, none of it runs and the synth renders exactly as
before (every existing audio test, the bit-identical bakes included, is
untouched). The falsy dials (Audio > **falsy (truthiness)**) default to
copies of the truthy ones, so switching on changes nothing until they are
tuned; the truthy half is the existing dials. All of them ride saves,
audio presets and bakes like any dial.

- **Clicks**: Audio > click timing > **resonators** (1-16, 5 by default)
  is each group's total. Off, all of them are the group's, as before (at 5
  the layout, the detune spread `detuneAt()` and the hash salts are
  exactly the old ones). On, the truthy half gets `ceil(n / 2)` and the
  falsy half the rest, at least one; each half has its own gain, tone,
  decay and detune, and its own hash salt. The recorder counts a falsy
  fire in `fires[group + 3]`, sent as `f0F`/`f1F` beside `f0`/`f1`.
- **Belts: no extra voices.** A run's voice switches settings when its
  bit changes: falsy gain, waveform, **transpose** (semitones) and
  **brightness** (a low-pass closing up to 7 octaves, on top of the
  distance low-pass). A change crossfades over **belt crossfade ms**
  (0: a cut) between the two oscillators, each computed only while
  heard (`truthRun()`). A newly voiced run starts straight in its state.
- **Only changes cross threads**: the recorder lists flips (`tf`, 2 x
  candidate + bit, with per-tick ends `to`) rather than a second bitset per
  tick, so the busiest stream does not double at high tick rates; the
  config carries every candidate's bit to start from. Ticks dropped or
  skipped still apply their flips. The worker's tap records the same.
- In the block renderer a flip is a change point like on/off (log values
  2/3). It matches the per-tick reference to the bit with brightness open;
  closed, a voice allocated mid-block warms its filter over the block's
  silent start, as the distance low-pass does (under 1%).

### Advanced audio

Audio > **advanced audio** (a switch beside truthiness) shows Audio >
**advanced** and turns on four timbre features. Off, the folder is hidden
and the synth reads none of its dials: every advanced branch is behind a
flag set in `applyParams()`, and `tests/advaudio.test.mjs` asserts the
output is **bit-identical** whatever the advanced dials hold, and also with
the switch on at its defaults. Each feature has a truthy dial set (applied
to everything) and a falsy one (only with truthiness on). The dials ride
saves, audio presets and bakes like any dial.

- **Falsy envelope** (`attackMsF`, `releaseMsF`): a falsy run's own attack
  and release, sluggishness included. The envelope runs at the pace of the
  half it is heading for (`vAtkF`/`vRelF`, equal to the truthy ones
  otherwise), so e.g. truthy runs sustain while falsy ones pluck.
- **Pulse wave** (`beltPulse`, `beltPW`): blends the sine/tri/saw table
  toward a mean-zero pulse of the given width -- a square at 0.5, nasal
  below. Only the table changes (rebuilt when the dials move), so it costs
  nothing per sample.
- **Tremolo and vibrato** (`trem*`, `vib*`: Hz, and depth -- a share of
  the gain, or semitones): per-sample gain and pitch factors for each half,
  computed once per span (`lfo()`) as a function of the **absolute sample
  clock**, so how the rendering is split never changes them; then every
  voice multiplies by them. With either on, voices take the two-oscillator
  path (`truthRun()`) even with truthiness off. A voice allocated mid-block
  winds its phase back by the pitch it would actually have advanced, or
  the block renderer drifts from the per-tick reference under vibrato.
- **Noise exciter** (`clickNoise`, `clickNoiseMs`): a share of each click
  becomes a burst of noise into its resonator, decaying over the dial's
  time, scaled to about the impulse's loudness -- a "tsk" rather than a
  ping, and with a long burst a snare-like wash. Each resonator has its
  own noise generator, so a bake is the same however it is chunked;
  resonators with no noise take the old loop.

`tests/advaudio.test.mjs`: off and at defaults bit-identical; with every
feature on, a bake chunked two ways identical and the block renderer equal
to the per-tick reference; the pulse's duty cycle; tremolo's dip and
vibrato's spread of periods; a noise burst still ringing where an impulse
has died; and a falsy run's own attack. Mutation-tested: the noise read
while off, and the vibrato wind-back dropped, each fail it.

**Presets** (Audio > presets; [audiopresets.js](audiopresets.js)) carry a
sound between saves: the same record a save holds (every dial, the scale
included, and the Bake dialog's seconds and rate -- not the switch, the
audio selection or the region), kept under a name in this browser
(`gates.web.audioPresets.v1`) and exported or imported as a tagged
`.json` file. A preset stores **every** dial, defaults filled in, so
loading one sets the whole sound rather than only the dials it recorded.
Loading is a dial change like any other: it lands in the board's record,
so the next save carries it. Importing applies the file at once and keeps
it in the list under its own name (asking before replacing one). Cleaned
on read with `cleanDials()`, like a save's. `tests/audiopresets.test.mjs`.

Two practical notes: the `AudioContext` is created and resumed **inside the
tap** (main.js `setAudio()`), before the dynamic import, since iOS will not
start one made after an await; `navigator.audioSession.type = "playback"`
is set where the browser has it, or the iPhone's silent switch mutes it.
The sound button **blurs itself** after a tap: otherwise the next Space
(play) toggled audio off again.

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

## Look: background, corners, chevrons and bloom

View > **look** (per player: the panel's preferences, and in a
presentation's views like the rest of View; not in saves yet), with
**Reset look**:

- **background**: the board's colour (`COL.bg`; the void outside the board
  and the grid lines are unchanged).
- **corner radius** (cells, 0..0.5): machine bodies drawn with `roundRect`
  (`bodyRect()` in render.js), ghosts and the lifted outlines too; 0 is the
  old `fillRect` path.
- **chevron opacity**: belts', underground ends' and the control overlays'
  chevrons alike (`strokeChevrons()`).
- **bloom intensity** (0 is off), **cutoff** (luminance 0..1) and
  **radius** (1..8 levels). [bloom.js](bloom.js) is the one piece of WebGL2
  in the app -- Canvas 2D can blur but not threshold -- imported only once
  the intensity is raised; without WebGL2 the bloom is simply off. It has a
  canvas of its own, never shown: render() calls `onWorld` at the point
  where the board is drawn and the editing marks are not; the 2D canvas is
  uploaded, bright-passed (a soft knee about the cutoff) to half size,
  halved down a chain with a 13-tap filter and summed back up with a tent,
  and the glow drawn into the 2D canvas with `"lighter"`. So selections,
  frame boxes, the audio box and the screen-space text (Source values, item
  numbers, notes, layer labels) draw crisp over the glow and never glow
  themselves. A plain luminance cutoff, by choice: the player makes what
  should glow the brightest thing on screen (the chevrons, `#bdc2d1`, are
  bright -- chevron opacity is the usual lever). Video frames use a second
  instance, so a recording glows as the screen does.

## Motion blur

View > look > **motion blur** (samples per render, 1..64; **1 is off**):
temporal supersampling. Each frame shown is the mean of k renders spread
over the span of sim time the frame covers, each at its own tick and its
own camera, bloomed once at the end -- a model of a real exposure rather
than a smear filter. For video above all, and for realtime on a machine
that can afford k renders a frame. Per player and in views (presets,
presentations), like the rest of look. [mblur.js](mblur.js) is the timing
(pure, `tests/mblur.test.mjs`), [blur.js](blur.js) the averaging.

- **Off is the old path, not a cheap version of the new one.** At 1,
  paint() takes exactly its old branches, no accumulator is created and the
  worker cuts nothing (`W.blurG` 0); the only additions are a number
  compared per frame and render.js's `part` tests. Measured in the
  browser: with the setting at 1, zero accumulators exist and zero frames
  take the blur path.
- **What is blurred**: the board -- belts, machines, items **and their
  numbers** (they move), status colours and afterglow (each sample is
  coloured at its own time), the pixel overlay. **What is not**: ghosts,
  the selection, frame and audio boxes, vector marks, Source values,
  notes, index badges and layer labels, drawn once, sharp, over the mean
  from the frame's final state (render.js `part: "board"` /
  `"overlay"`). A paused board with a still camera draws ordinarily.
- **Bloom is taken from the samples without their numbers.** Item numbers
  never glowed (they were drawn after bloom), so with bloom on each sample
  is captured twice -- before its numbers (render.js `onCapture`) and whole
  -- into two sums: the frame is the whole sum's mean, and the glow is
  taken from the other's (`Bloom.apply(ctx, params, source)`). Bloom off,
  one capture per sample.
- **Averaged in floating point and linear light.** Each sample is uploaded
  to a WebGL2 texture and added into a half-float buffer, sRGB decoded
  before and encoded after. Canvas alpha would round every step to 8 bits
  and band by 64 samples; sRGB averaging would darken a bright item
  smeared over the dark board. Needs `EXT_color_buffer_float`; without it
  motion blur is off (a console warning), as bloom is without WebGL2.

**Where the samples sit.** A **video** knows each frame's time, so frame
f's samples end on the frame's own tick and step back evenly through it,
`T_f - (k-1-i) * span / k` (`frameSampleTimes()`): the last sample is the
frame, so the blur trails motion and adds no lag; frame 0's do not reach
back before the video's first tick. The camera moves **per sample** --
`frameBox()` at the sample's fractional frame -- so a fast zoom blurs the
whole picture, as a real shutter would.

**Realtime** cannot do that: the worker ticks ahead on its own clock and
the screen sees its state once a frame. So the samples sit on a fixed
**grid** of sim time, `m * g` for whole m, with `g` a frame's ticks over k
(`gridSpacing()`: rate x the smoothed frame interval / k, resent to the
worker only when it moves by a tenth). The worker **cuts its delta at the
grid's ticks** (`isCut()`, in `SimHost.slice()`): each cut is a delta of
its own, and a reply carries the slices ahead of the delta (`d.chunks`).
The screen holds them (`W.queue`) and, for each grid point in the frame's
span (`gridSampleTimes()`: `(S, E]`, S what the last frame showed),
applies the slices up to that tick and renders -- so **every sample is a
real state**, never an extrapolation, and the forecast animates each one's
fraction of a tick as usual. The camera is interpolated from the last
frame's to this one's (`lerpCam()`: centre linear, zoom geometric), so a
pan blurs too -- and while paused, a moving camera takes k samples of the
one state.

- A frame that runs long holds more grid points; past 2k they are thinned
  evenly, keeping the newest. A frame with **no** grid point in its span
  (the worker's next delta had not come) shows the last frame's mean again
  (`MB.held`): a sharp frame among blurred ones reads as flicker -- 3 frames
  in 10 did in headless Chromium before this, none after. Only while
  playing and only for the same board revision, canvas size and camera, so
  an edit or a pan is never shown stale.
- Both sides compute a grid point as the same `m * g` in doubles and the
  worker tests the neighbouring m's with that very `floor()`, so they agree
  on the tick to the bit rather than by algebra that could round the other
  way. A slice cut on an old grid (g just changed) is still applied in
  order; its sample is drawn from the latest state at or before its tick.
- At most `MAX_CHUNKS` (256) slices wait between replies; past that the
  worker stops cutting and the changes ride the next slice. Merging is
  safe, dropping would not be -- a delta is only what changed since the
  last.
- **The main thread** (worker off, or a baked soundtrack playing) steps the
  mirror to each grid point itself, inside its usual frame budget, and
  draws there; what does not fit is dropped as before.
- The audio flush, the tick counter and the Status readout run after the
  samples, so they see the frame's final state as always.

`tests/mblur.test.mjs` holds the realtime claim: the worker runs its real
loop on a fake clock with the grid on, and at **every grid point** the
mirror, stepped through the slices, is checked against an ordinary Sim
stepped to exactly that tick (every ring, counter, anchor, channel and the
Pixel screen) -- at grid spacings of 0.4, 1, 7.5 and 40 ticks on
`cpu.npz`. Mutation-tested: no cuts fails all four; cutting a tick late
fails the two coarse grids (at 1 tick or less every tick is cut anyway,
so it changes nothing there). Plus the pure parts: cuts against a brute
force over 5000 ticks at nine spacings, the span and its thinning, a
video frame's times, the camera lerp. In headless Chromium every path ran
without an error -- worker, main thread, bloom (both sums filled), a
paused pan, a video frame -- and a video frame of a moving run at 3 ticks
a frame came out smeared along the belts with the machines sharp.

**Cost** is k renders a frame, plus an upload and a blend each: in headless
Chromium's software GL a whole-board frame at 16 samples is a few frames a
second. That is why it is a slider: a video does not care, and a
realtime view wants as many as the machine affords.

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
- **Motion blur** (View > look): each frame is k samples over the frame's
  ticks, the camera moving per sample (see "Motion blur").
- **What is drawn**: the board as View draws it -- grid, status colours,
  the pixel overlay (the copy gets its own Screen, starting blank), labels,
  **layers** (hidden ones left out, items included, tints and layer labels)
  -- and never the selection, ghosts or other editing marks
  (`drawVideoFrame()` in main.js; `gateStatus()`/`beltStatus()` take the sim
  to read for this). The layers are the view taken at Record
  (`recLayers`), applied through the same `layerOpts()` the screen uses, so
  the two cannot draw them differently; the copy shares the live board's
  grid, table and lists (an edit stops the recording), so that view is
  exact for every frame. Changing a layer's visibility or sliders mid-render
  does not reach the video.
- **Sound**: with Audio's switch on, the same ticks are baked
  (`bakeAudio(..., t0)`: a bake may now start mid-run, at the video's own
  first tick, so the soundtrack starts and ends with the picture; the start
  tick still silences what comes before it) and fed to an audio track a
  second at a time beside the frames. AAC, or Opus where AAC cannot be
  encoded (Linux Chromium); H.264, then VP9, then AV1 for the picture, each
  asked of the browser rather than assumed.
- **The camera**: the frame is the largest box of the video's aspect that
  fits the view (`cropBox()`), shown on the board while the folder is open
  -- **including with the whole pane collapsed**, which is how the player
  gets the panel out of the way to frame the shot (it once hid with the
  pane, which defeated that; Audio's camera box still does);
  **Select end camera state** captures the view as it is, drawn dashed, and
  **Unset end camera** drops it. The move runs from the view at Record to
  the end state over **start lerp .. end lerp** (fractions of the video,
  still outside), its speed a trapezoid: **fade in** and **fade out** are
  the ramps as fractions of that window (0/0 constant speed, 0.5/0.5 a
  smooth S; scaled down if they exceed 1). Centre linear, size geometric,
  so a zoom feels even.
- **The file name** is the board's name and a short timestamp, e.g.
  `CPU experiments 11-20 12_07_33.mp4` (underscores, not dots), so several
  takes of one scene do not collide.
- **Saving**, as Fluoddity: the file picker is raised **before any other
  await** in the click (it needs the gesture), and a chosen file is
  **streamed** into (`fastStart: false`, flat memory; `finalize()` closes
  it -- closing again throws); no picker (Firefox, Safari, iPhone) or a
  failed one **buffers** in memory and downloads at the end; Cancel in the
  picker records nothing. **Cancel recording** keeps what was encoded: a
  partial video is still a video.

**The dry run**: with an end camera set, pressing play flies the view along
the path a recording started now would take, to tune the start view and
the fades without rendering. The view at the press is the start, and the
**tick on screen** picks the frame (frame f is tick `t0 + f * tps / 60`, as
in video.js), so the camera matches the content at any play rate -- at the
video's own ticks/s it runs in real time. The view is set so the crop box
*is* that frame, and holds the end view once the path is done. The video
and camera dials apply live. **Any disturbance restores the view** from the
press: pause, a pan or zoom of any kind, an edit (undo included), Reset, a
load (which then frames the new board), unsetting the end camera, starting
a recording. A manual move is detected by the camera no longer being where
the dry run last put it (`driveDryRun()`), which catches drag, pinch,
wheel, the pan glide and Fit board without touching camera.js. Checked in
the browser: a second into a 2 s path at the video's rate the zoom is the
geometric midpoint, a pause or a pan puts back the exact start view, and a
full run holds the end view.

Checked in headless Chromium: 2 s at 480×270 with sound and an end camera
gave 120 frames of H.264 at exactly 2.00 s and an Opus track (mean
-15 dB), the first frame the whole board and the last the zoomed end
camera, the live sim still at its tick; cancelling a 1080p recording at
frame 182 gave a valid 3.03 s file. `tests/video.test.mjs` covers the move
(still outside the window, monotone, continuous, linear without fades,
symmetric), the crop box, the lerp, the settings, and a bake starting
mid-run.

## Presentation mode

`…/#b=<name>` opens a **presentation**: `presentations/<name>.json` and the
board it names (`<name>.npz` beside it), as a show rather than an editor.
[present.js](present.js) is the page; main.js hands it a small interface
(`presentApi`) and three hooks (taps, the frame, Space). The editor makes
them: the panel's **Presentation** folder, and **Export presentation
(.zip)**, which zips the pair. Unzip it into `web/presentations/` and push;
`build.sh` copies the pairs into the site. A change of hash reloads.

- **The JSON**: `rate` and up to four other `rates`; `startCamera` and
  `endCamera` as world boxes `{cx, cy, hw, hh}` (`viewBox()`), fitted to
  whatever screen opens it (`fitBox()`); `schedule` -- ticks, and the
  video's lerp start/end and fade in/out; `views`, one or more, the first
  the default. A view is the panel's View, status colours and Dev dials
  (`Panel.captureView()` / `applyView()`) plus each layer's visible, active,
  tint and label size and the labels' master switch (`captureViewState()`
  / `applyViewState()` in main.js; layers are matched by name, below).
- **Export** takes the board as it stands -- a selection put down first;
  audio settings and labels ride in the save -- the stored standard view as
  "default" (the view in front of the player if none is set), the captured
  detail view if set, and the folder's
  draft: name, start rate and four rate boxes (blank or 0 unused), the
  cameras (no start camera: the whole board) and the schedule. The draft
  persists with the panel's preferences.
- **The views** (Presentation > views): **Set standard view** and **Set
  detail view** capture the view in front of the player into the draft;
  **Load** puts one back in front of the player, so changing one setting
  of a view is Load, change, Set -- not rebuilding it from memory; **Clear**
  drops it (Load and Clear are disabled while it is unset). A cleared
  standard view means "the live view at Export", as before it existed, so
  a draft from before keeps its behaviour. `#edit=` fills both from the
  record.
- **View presets** (View > presets, beside the settings they hold;
  [viewpresets.js](viewpresets.js)): a view -- the same record
  `captureViewState()` makes, minus its name -- kept under a name in this
  browser (`gates.web.viewPresets.v1`) and exported or imported as a tagged
  `.json` (`gates-view-preset`, so an audio preset is refused). Loading one
  sets the view in front of the player through `applyViewState()`, from
  where Set standard / detail view makes it either view. Cleaning on read
  is structural only (scalars, layer entries with an integer id);
  `applyViewState()` already takes only keys the panel knows, of the right
  type.
- **Layers in a view are matched by name** (`layerKey()`: case and end
  spaces ignored, as a paste's are), presets and presentation views alike,
  so a view carries to another board: a layer whose name the view does not
  mention, or a name the board lacks, is left alone; two layers of one name
  both take it. A view recorded before names were (an older presentation
  JSON) has none, and matches by id as it always did.
  `tests/viewpresets.test.mjs`.
- **The page** shows the board, the sound button, Reset and Play/Pause
  (bottom middle), an **i** button cycling the views (hidden with only
  one), and the **speed** panel (bottom left, level with the buttons: the
  rates wrap to two rows of three, and a rate of 1000 or more reads as
  "1k" -- a million as "1M" -- so five never reach the centred buttons), the starting rate lit --
  no panel at all without other rates. Everything else is hidden by
  `body.presenting`. It writes **nothing** to the browser's storage:
  `freezePersist()` runs before the shell and the panel are built, and
  every store (panel prefs, the shell's, the layer panel size, saves and
  autosaves) checks `persistEnabled()`; no resume, no autosave timers.
- **Tap to begin**: the board loads dimmed under a cover that takes the
  whole gesture, tap or drag, so the drag that dismisses it neither pans nor
  ends the schedule; its lift also starts the sound. Then the sim plays and
  a toast says "drag to pan, pinch to zoom".
- **The camera schedule** places the camera from the **tick on screen**
  (`sim.tick + alpha` over the schedule's ticks, through `pathBox()`), so a
  pause holds it and Reset puts it back at the start pose. It ends **for
  good** (until a reload) the moment the camera is not where it last put it
  -- a drag, pinch, wheel or glide, detected as the dry run does -- or the
  rate is changed. 0 ticks, or no end camera: no schedule.
- **A tap on a machine** (visible, not a belt or underground) pauses if
  playing and opens a large panel with its name (the mini example board is
  a later phase). Any tap or drag on the panel only dismisses it, and plays
  on if it was playing. Long press, selection drags and editing keys are
  all off.

- **The hammer** (bottom right, over the tap-to-begin cover) goes to the
  editor: `#edit=<name>`, which loads that presentation's board, applies
  its default view, fills the Presentation folder from its record (name,
  rates, cameras, schedule, detail view) and sets the start camera -- so a
  deployed show can be changed and exported again. The hash is dropped once
  read, so a reload resumes as usual. Loading it is not an edit, so no save
  is written until something changes.
- **Preview** (Presentation > Preview presentation) shows the presentation
  the folder describes -- the record Export would write -- on the board in
  front of the player, **in the page**, with no reload: the hammer or Esc
  comes back with the undo history, stamps, tool bar and board untouched
  (`enterPreview()` / `exitPreview()` in main.js). The preview starts from
  Reset, as a viewer's would. On the way out the view (View, colours, Dev
  dials, layer visibility), camera, rate and play state are put back, all
  while the stores are still frozen (`unfreezePersist()` comes last), so
  localStorage is byte-identical after a preview. `startPresentation()`
  takes the record directly (`opts.rec`, no board load) and returns before
  the cover is tapped, so its `close()` can take everything it made back
  off the page at any point.

`tests/present.test.mjs` covers the hash, the record, the speed panel and
the schedule. Preview and the hammer, driven as a Pixel 7: an edit, a
preview, a tap to begin, the hammer back -- undo still there, view, camera
and board restored, no presentation element left, localStorage
byte-identical; the hammer straight off the cover and Esc likewise; and
`#b=test`'s hammer opening it in the editor with its settings. The show
itself, also as a Pixel 7: export, unzip, open; the cover's drag leaves the
camera; the schedule zooms with the ticks and Reset
returns it; i cycles; a machine tap pauses and the dismiss resumes; a pan
and a rate change each end the schedule; localStorage byte-identical after.

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

## Profiling a frame

**Debug > profile frames** shows a timing tree under the fps counter, a
mean per frame over one-second windows ([prof.js](prof.js), after the
desktop's `core/tick_profile.py`): `begin()`/`end()`/`span()` name nested
spans (`paint/render/structs/ports`), and off they cost one boolean test.
`window.gates.setProfiling(true)` and `gates.profText()` do the same from
the console or a headless test.

**These are main-thread CPU times.** Canvas 2D *records* draw calls there
and rasterises them later, so a fill's pixel cost is in no span: `frame`
(paint to paint) minus `paint` is idle, raster and compositing. **GPU sync**
ends each render by reading one pixel back, which waits for the raster, and
reports the wait as `render/gpu sync` -- honest, and it slows the frame, so
it is its own switch.

Measured on `test` (above), whole board on screen, playing at 30 tps,
headless Chromium (software raster, so the raster numbers are not a
phone's):

| | before | after |
|---|---|---|
| 1280×800, DPR 1 | 50.7 ms/frame (JS 30) | **19.2** (JS 7.5) |
| 412×915, DPR 2.6 | 58.4 (JS 31) | **34.5** (JS 8) |
| zoomed in (19 px/cell) | 16.7 (JS 4.2) | 16.7 (JS 1.6) |

What it took, each found by the tree:

- **`closePath()` is quadratic in Chrome.** Its cost grows with the length
  of the path it is called on, so n polygons closed with it cost O(n²):
  3200 triangles took 40 ms against 2.4 ms closed with a `lineTo` back to
  the start. The underground funnels alone were 17 ms of a whole-board
  frame. **Never call it** on a batched path: `closePoly()` in render.js
  (a `lineTo` to the first vertex and on to the second, so a stroke still
  joins the corner). `arc()`, `rect()` and SVG `Z` in a `Path2D` string
  are all linear.
- **Status colours at most once per frame.** They are worked out in
  `paint()` after the frame's ticks, for the state on screen only, and
  cached on that state and the time, so a frame with no new tick (a slow
  rate, or panning while paused) reuses them. (Now the afterglow's; see
  "Gate status colours".)
- **The forecast rolls back with an undo log**, not a copy of the entity
  table: `setF()` logs each value it replaces while a forecast runs. The
  copy was 3.6 MB on a 32k-row board and three quarters of the forecast.
- **Belts are culled before they are grouped by tint**, so a zoomed-in view
  asks the tint about what is on screen, not all 16k belts.
- **Machine bodies are one path per colour** (fill, then edge), not a
  `fillRect` + `strokeRect` and two style changes per machine.
- **Items below Dev > `item rings from` (6 px/cell) are plain squares** in
  the ring's colour: a disc there is a pixel or two, and rects are far
  cheaper than arcs to build and to rasterise.

Tried and not kept: filling belt spines as rects instead of stroking them
with round joins -- slower in this rasteriser, and unmeasurable on a phone
from here. Solid-square belts (`solid belts below`) are slower too.

What is left, zoomed out on a phone, is mostly **raster**: tens of
thousands of sub-pixel shapes. The next steps, cheapest first: a
far-zoom tier drawing the board as a bitmap at a few texels per cell
(written from the grid, redrawn on an edit or a tick's status change, then
one `drawImage`); and past that, the board in WebGL2 the way the desktop
draws it -- the grid as an integer texture and one fragment pass, O(pixels)
whatever the entity count -- with Canvas 2D kept on top for text, ghosts
and editing marks. `itemPoints()` (flow.js, ~2 ms whole-board) is the
largest JS span left.

## Rules

- **No build step, no npm, no framework.** Plain ES modules and Canvas 2D
  (the bloom's post-pass is the one WebGL2, optional and off by default).
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
  (`LOD` in `render.js`). No `closePath()` on a batched path (see
  "Profiling a frame").
- **The tick stays the desktop's CPU tick.** The closed-form belt transport,
  the gate prefilter and the wake list are its optimisations, all exact:
  `tools/check_web_sim.py` must still pass, and so must
  `tests/sleep.test.mjs` (sleeping against full, every tick), and
  `tests/worker.test.mjs` (the main thread's mirror against a reference).
  No folding, no GPU yet. A change to the desktop's tick semantics needs the same
  change in `sim.js` -- and any new way a gate's channels or a run's state
  can change needs a wake, or the sleeping tick drifts from the full one.

## Files

| file | role |
|---|---|
| `index.html`, `style.css` | the shell: board, controls row, swatch bar, picker sheet |
| `main.js` | wiring: applies the shell's and panel's requests to the board; resize, redraw, the play loop |
| `sim.js` | the simulation: segment index, closed-form belts, gates, the wake list, the clock -- the desktop's CPU tick; `forecast()` |
| `rate.js` | `RATE_MIN`/`RATE_MAX`, the tick-rate bounds: the one place the cap is stated. The clock, the speed box, video ticks/s and audio's reference rate all read it; import-free so the AudioWorklet can load it |
| `simhost.js` | the sim on a worker: the deltas, the board and state messages, the epoch, `SimHost` and its loop's slices, motion blur's grid-tick slices; pure, no DOM |
| `simworker.js` | the worker itself: a `SimHost` and the loop that runs it |
| `flow.js` | where items are drawn: belt paths, the machines' internal links and wait points, the tick animated from the forecast |
| `ui.js` | the shell's state and DOM: modes, sim and turn buttons, thumb pads, swatches, long press, picker |
| `tools.js` | the tool catalog (from `types.json`) and the swatch drawings |
| `icons.js` | line icons as path data (mostly Lucide, ISC) |
| `panel.js` | the Tweakpane panel: bundled and browser saves, export, View, Audio, Debug, Status (with the live channel readout) |
| `audio-params.js` | the Audio dials and the gate groups; loaded always |
| `audio.js` | audio, main thread: the per-tick recorder and starting the graph; loaded on demand |
| `audio-dsp.js`, `audio-worklet.js` | the synthesiser, and its AudioWorklet wrapper |
| `space.js` | spatial audio: distance to the region or screen box, the falloff and low-pass curves, per-run and per-gate weights |
| `scale.js` | belt scales: the formula language, presets, tables, the wavelength table the synth reads |
| `audiopresets.js` | audio presets: the dials under a name, in the browser and as `.json` files |
| `viewpresets.js` | view presets: a presentation view (View, colours, Dev, layers) under a name, in the browser and as `.json` files |
| `audio-bake.js` | baking: a stretch of ticks rendered offline, and the WAV writer; loaded on demand |
| `pixel.js` | the Pixel machine (web-only, in Sink's id), its screen, and the overlay's drawing |
| `present.js` | presentation mode: the `#b=` route, its controls, the camera schedule, the info panel |
| `prof.js` | Debug > profile frames: named spans, the per-frame timing tree |
| `persist.js` | the one switch that stops every localStorage write (presentation mode) |
| `presentations/` | presentation pairs, `<name>.json` + `<name>.npz`, copied into the site |
| `video-params.js` | video export: the settings and the camera move; loaded always |
| `video.js` | video export: the offline recorder (mediabunny); loaded on demand |
| `vendor/` | Tweakpane 4.0.5 (MIT) and mediabunny 1.55.1 (MPL-2.0, with its licence), vendored |
| `npz.js` | `.npz` reader and writer: zip directory + native `DecompressionStream` / `CompressionStream` + `.npy` headers |
| `saves.js` | browser saves: whole `.npz` files in localStorage, and their index |
| `board.js` | the save's arrays, validation, derived render lists, `entityAt`/`describe`, placing, erasing, capturing, and the two setting writers |
| `camera.js` | camera (world +Y up, zoom in CSS px/cell), pointer gestures, and taps |
| `render.js` | Canvas 2D drawing under a world transform; labels in screen space; `part` splits a frame into a blur sample and the sharp marks over it |
| `planner.js` | belt-run geometry: paths, facings, the tip, auto-undergrounds |
| `stamp.js` | stamps: rotate and mirror (port of `core/stamp.py`), cells, subsets |
| `segtrace.js` | the belt run through a cell, as `core/segments.py` would cut it |
| `ghost.js` | a stamp at a board position, in the renderer's shapes |
| `placer.js` | where a ghost's footprint sits: the port of `origin_for_cursor()` |
| `glyphs.js` | the machine glyphs, ports of the shader's `sd_*` marks |
| `tune.js` | the Dev dials: defaults, panel spec, the live values |
| `bloom.js` | View > look's bloom: a WebGL2 post-pass over the 2D canvas; loaded on demand |
| `mblur.js` | motion blur's timing: the sample grid, the worker's cut ticks, a video frame's sample times, the camera lerp; pure |
| `blur.js` | motion blur's averaging: a WebGL2 half-float accumulator, in linear light, two layers for bloom |
| `history.js` | undo, redo and the checkpoint, over board snapshots |
| `notes.js` | labels: the desktop's text-box records, hit-testing, wrapping, fitting a box to its text |
| `indexer.js` | indexing: the orders, the value tables, the file parser, a·x+b |
| `hack.js` | the Hack assembler and its `#define` preprocessor (pure) |
| `hackstore.js` | saved Hack programs and the editor's draft, in localStorage |
| `assembler.js` | the full-screen Hack editor |
| `toast.js` | a brief message that fades by itself |
| `vector.js` | vectors: offsets, turns, the claims overlay, laying copies, following a chain |
| `layers.js` | layers: the table, its rules (current, visible, active), parsing, stats (counts, centres, hidden cells) |
| `layerpanel.js` | the layer panel's DOM: rows, +, Rename / Delete / Adopt, the tint and label sliders |
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
the `.npz` writer, audio settings in a save, the wake list against the
full tick, and the worker's mirror against a reference) run in Node, with no npm and no browser. The whole tick against the
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
