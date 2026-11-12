# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**Gates** — a grid-based factory game in the spirit of Factorio. A GLFW/moderngl
app renders a cell grid and its entities entirely from a fragment shader that
reads game state out of a GPU texture, with an imgui control panel on top.

Currently implemented: the gameboard, belts and underground belts, the
belt-drawing UX (elbow/line planning with a ghost preview), copy/paste/erase
modes, camera pan/zoom, save/load — and the **simulation**: belts carry items
in closed form, gates fire on a two-phase tick, and a saturated chain moves
one item per tick. See "Deferred" for what is still missing.

## Commands

```bash
# Activate venv (Python 3.13)
.venv\Scripts\activate

# Install deps (already present in .venv)
pip install -r requirements.txt

# Run
python app.py

# Test suite (headless; needs a GL 4.3 context, opens no visible window)
python test_drive.py

# ...or just the scenarios whose name contains a substring
python test_drive.py underground
```

### Tests

`test_drive.py` is a thin runner; the tests live under [tests/](tests/).
There is no pytest/linter configured.

| file | holds |
|---|---|
| [tests/harness.py](tests/harness.py) | the fake mouse/keyboard, the shared GL context, and `reset()` |
| [tests/test_scenarios.py](tests/test_scenarios.py) | drive the orchestrator and assert board state |
| [tests/test_glsl.py](tests/test_glsl.py) | static checks on the generated GLSL — no context, no board |
| [tests/test_tick.py](tests/test_tick.py) | the clock as wired into the frame loop: rate, `alpha`, keys, phase order |
| [tests/test_segments.py](tests/test_segments.py) | belt-run derivation: tracing, cuts, undergrounds, merges, audit under churn |
| [tests/test_belts.py](tests/test_belts.py) | the closed form against the naive oracle, on fixed and randomised configs |
| [tests/test_item_render.py](tests/test_item_render.py) | reads the framebuffer back: do discs appear where the simulation says, do the labels say what the formatter says, and does the pass fill a resized window? |
| [tests/test_gates.py](tests/test_gates.py) | firing rules, arithmetic, back pressure, dangling-output discard, and the bubble-free claim |
| [tests/test_stamp.py](tests/test_stamp.py) | what the stamp transforms preserve: wiring, topology, arithmetic |
| [tests/test_clipboard.py](tests/test_clipboard.py) | the copy/paste/erase mode machine, and matched-stamp substitution, driven through the fake mouse and keyboard |
| [tests/test_inspect.py](tests/test_inspect.py) | the Status window, the per-Source emit value, the per-belt seed, the save filename box, the persisted toolbar |
| [tests/test_turn.py](tests/test_turn.py) | the empty hand's R/F: the footprint invariant, the settings that must survive an in-place turn, the underground reversal, and ctrl+R's restart |
| [tests/test_annotations.py](tests/test_annotations.py) | the note overlay: the draw/erase gestures, the text-box flow, what the Show-notes flag gates, and how notes travel through copy, paste and saves |

**Scenarios are independent.** `harness.reset()` clears the board, camera,
input state and toolbelt before each one, so a failure names the scenario
that caused it. This matters: the suite used to be a single 1172-line script
whose sections shared state through side effects, and twice a change broke an
assertion hundreds of lines away for unrelated reasons. Order-independence is
worth preserving — the scenarios pass reversed and shuffled.

Two habits that keep it that way: a scenario **sets up what it needs** rather
than inheriting a previous one's board, and anything that mutates global state
(imgui's `want_capture_mouse`, say) restores it in a `finally` so a failing
assertion cannot poison everything after.

`core/cell.py`, `core/entity.py` and `core/tick.py` also run self-checks at
import, so the cell encoding, the entity geometry and the clock's timing
contract are exercised by importing anything.

A scenario that registers a phase callback on the shared clock must remove it
in a `finally`, **with `clock.off_phase(phase, fn)` and not by clearing the
phase** — the engine's belt transport lives on `PHASE_BELT`, so a blanket
clear stops the simulation for every later scenario. `harness.reset()`
asserts the callback survived, because when this happened the symptom
appeared three files away as "the belt phase ran but nothing moved". Same
reasoning as restoring imgui's `want_capture_mouse`, with a sharper failure
mode.

`harness.reset()` also restores the **toolbar layout and the active row**,
for the same class of reason. `select_tool()` finds a tool by *name*, so a
scenario that re-assigned a slot through the type menu would have an
unrelated later one die of `StopIteration` a long way from the cause — and a
left-over active row would have every digit land on the wrong tool. Restoring
the defaults is also what keeps the harness independent of the player's
persisted bar: it never loads one, and now it cannot acquire one either.

`select_tool()` **presses the row swap to reach row 2** and goes through the
real keys rather than calling `Toolbelt.select()`, so a break in the digit map
or the row swap surfaces in whichever scenario asked for a row-2 tool rather
than only in the one test that covers rows directly.

Modes that are reached by a **chord** get a named helper —
`enter_erase_mode()` / `enter_cut_mode()` rather than
`ctrl_key(glfw.KEY_D)` spelled out at thirty call sites. That binding has
already moved once (bare `X` → `ctrl+D`, when `X` took the row swap), and a
scenario cares that it is *in erase mode*, not which keys got it there.

**Beware exact float multiples when asserting tick counts.** `0.3 // 0.1` is
2, not 3, so a single `advance()` spanning several periods runs an unreliable
number of ticks. Tests that care about the count use `request_step(n)` or
one `advance()` per tick rather than pinning the division.

`harness.reset()` restores the tick **rate** as well as the timeline.
`clock.reset()` deliberately keeps the rate (it is configuration, not
timeline), so without this a scenario that set 30/s left every later one
banking half a tick per frame — `alpha` landing mid-tick and every item
rendering half a cell from where a whole-tick assertion expected it. That
surfaced as one shuffle seed in twelve failing a pixel test.

## Controls

| Input | Action |
|---|---|
| `WASD` | pan |
| `Q` | **pipette**: fill an empty hand from the board, or empty a full one |
| Scroll | zoom, anchored at the cursor |
| Left-drag | pan the camera |
| `F` | **per-tool**: flip (mirror) the held structure; with an **empty hand**, mirror what is under the cursor |
| `shift+F` | frame the whole board |
| `1`-`9`, `0` | select a slot in the **active** toolbelt row |
| `X` | swap which toolbelt row the number keys address |
| Right-click a slot | the **type menu**: put any tool in that slot |
| `ctrl+C` | copy mode — left-drag a selection box |
| `ctrl+V` | paste mode, if anything has been copied |
| `ctrl+D` | erase mode — left-drag a box to delete what it touched (annotations included, by bounding box) |
| `ctrl+shift+D` | cut: the same box, captured as a stamp, then straight to paste |
| shift+scroll | **paste mode**: walk the copy history (older / newer) |
| Left-click | build with the active tool (fires on **release**); with an **empty hand**, inspect what is there |
| `R` / `shift+R` | **per-tool**: belt path shape, or structure rotation; with an **empty hand**, turn what is under the cursor |
| Right-click | cancel a plan, or drag to erase |
| `Esc` | cancel a plan, or a tool's multi-step state |
| `F5` | reload the world shaders |
| `Delete` | clear the board |
| `Space` | pause / resume the simulation |
| `.` | run exactly one tick (works while paused) |
| `ctrl+R` | restart the simulation: tick back to 0, every item discarded |

With a **note tool** held (see "Annotations"), left-drag draws or sizes a
text box and right-drag is the brush eraser, so neither the camera pan nor
the board erase is available — pan with `WASD` and scroll. `R` walks the
width/size, `F` the colour.

`R` and `F` deliberately mean different things in different tools — the input
manager reports only that they were pressed and the active builder decides.
Adding a tool with its own notion of "cycle" or "flip" needs no input-layer
change. `F` took the bare key from frame-the-board (now `shift+F`) because
flipping is the far more frequent action.

`Q` is resolved the same way: the input manager reports the press, the
**orchestrator** decides, because only it knows what the hand holds. Zoom has
no key binding at all now — scroll is the only one.

### The empty hand is a real state

The default is holding **nothing**, Factorio-style: no slot is highlighted,
and clicking the board **inspects** rather than builds (see "The Status
window" below). `Toolbelt.selected` is `NO_SELECTION` (`-1`) then, which is
deliberately *not* the same as selecting an empty slot — `empty_handed`
distinguishes them where `active is None` cannot. The distinction earns its
keep twice: selecting an empty slot is a deliberate act that should look
different from holding nothing, and the inspect gesture lives on the empty
hand rather than on a slot.

**An empty hand is click-based**, not drag-based. It has no plan to build up,
so the `drag_based=True` fallback would park it in `STATE_PLANNING` on the
first click and only commit on the second — which for inspect means the
Status window needing two clicks to open. That fallback was harmless only for
as long as the empty hand had no gesture at all.

`harness.reset()` leaves the hand empty, so every scenario starts from the
app's real default and declares the tool it needs. That also stops `reset()`
leaving a stray ghost — its trailing `step()` would otherwise have a
drag-based tool preview its idle run, and a scenario opening with
`live_count == 0` would fail on a board it never touched.

### R and F with an empty hand turn what is already built

The empty hand's second and third gestures, alongside inspect, and resolved
the same way: the input manager reports that `R` was pressed, and the
**orchestrator** sends it to the board rather than to a tool because only it
knows the hand is empty. With a tool held, `R` and `F` still belong to the
tool — which is what keeps the feature from being a hazard, since a player
rotating a held structure over a built-up area must not be silently turning
the machines underneath it.

**In place, never erase-and-replace.** The row is kept and only its
orientation changes, so per-instance settings travel with the turn for free —
a Source's emit value, a belt's seed. Re-placing would lose them *invisibly*,
since the board looks identical either way: same type, same cells, same
facing. That is why every test in [tests/test_turn.py](tests/test_turn.py)
asserts on a setting or a row id rather than on the resulting direction.

**A turn may only ask for cells the entity already owns**, which is the
invariant the whole gesture rests on. It means a turn can never be blocked,
needs no placement check, and cannot half-succeed — a Shear boxed in on every
side still turns. `Entity.inplace_turns()` is the predicate, and it returns
the rotations that leave the *extent* alone:

- A **square** footprint (a 1×1 belt or underground, a 2×2 Sink, a
  hypothetical n×n) takes quarter turns, so `R` and `shift+R` differ.
- An **oblong** takes only the half turn, so `R` and `shift+R` are the same
  thing on it. Deliberate rather than an oversight: with one legal rotation,
  making shift mean anything else would mean moving the machine, and making
  it a no-op would leave the player unable to turn a Shear with shift held.
- Since `cells_for()` fills the bounding box, footprint-preserving *is*
  cell-set-preserving — so the extent test is the whole check for every type
  that exists.

**Flip needs no geometry test at all**, because mirroring permutes cells
within the bounding box and so preserves the footprint for every type. What
it needs is for the mirror to be *visible*: an achiral type (only `Sink`) and
a belt (no ports) report no change rather than dirtying the board, matching
what `F` already does on the toolbelt.

**A shape with no footprint-preserving rotation fails silently**, as asked.
Nothing is shaped that way yet and nothing can be — every rectangle has a
half turn, and `FOOTPRINT` *is* the cell set — so `INPLACE_TURNABLE = False`
is the declared veto in the meantime, pinned by a test so it is known to work
before anything depends on it. A real L-tetromino would compare cell sets
instead, and would almost certainly then answer "no turns".

**`INPLACE_TURNABLE` is deliberately not `ROTATABLE`.** That older flag is a
statement about *tools* — a belt's facing comes from the path it is drawn
along, so the planner does not offer `R`, and `core/entity.py` asserts it
stays `False`. A belt already on the board is the opposite case: turning it is
exactly the edit the player means. Keying the in-place turn off `ROTATABLE`
would have made belts, the most common thing on any board, the one type the
gesture could not touch. Same split the raw belt tool made, one layer down.

**The orientation must reach the grid, not just the row.** `.x` carries a copy
of the facing and `.z` the flip bit, both read per-pixel with no row lookup,
so `Engine._restamp_turned()` restamps — and because every grid write funnels
through `Gameboard.write_cell()`, the segment index re-derives on its own: a
turned belt joins a different run. Skipping the restamp leaves the entity
*drawn* at its old orientation while simulating the new one, which is silent
in any row-level assertion; that is why the tests check `cell_dir()` out of
the grid and the segmentation count, not only the view.

#### An underground reverses instead of turning

`R` and `F` both mean "reverse this pair" on a placed underground end. It is
1×1, so a quarter turn would be footprint-legal — but it would take the end
off the axis it shares with its partner and break a pair the player never
asked to break. Reversing a run is the edit actually wanted, and doing it to
both ends at once is what makes reversing a long belt cheap instead of a
teardown of every underground along it.

**Swapping the roles must also reverse the facing.** The exit is downstream of
the entrance *by position*, so toggling `is_exit` alone would leave an exit
sitting upstream of its own entrance — geometrically incoherent rather than
merely surprising. Both ends flip role and facing together, neither cell
moves, and the pairing survives. Two consequences:

- **Either end is a handle on the whole pair.** The player reaches for
  whichever is nearer, so hovering the exit must not give a different result
  from hovering the entrance.
- **Two reversals are the identity.** Worth stating separately: role-swap and
  facing-reversal are each involutions, but an implementation that reversed
  only the hovered end's facing would come back with a coherent-*looking*
  pair pointing the wrong way.

An unpaired end reverses alone, since an unpaired end is a legal thing to
leave on the board. A one-way ghost pairing is ignored (see "A ghost exit's
pairing is one-way"), or a preview would license editing a real entity.

**`F` on the underground *tool* swaps which end the next click lays.** The
entrance→exit alternation is a convenience for laying a fresh pair and is
wrong whenever the player wants the far half — arriving at the other side of a
gap primed for an entrance. Without it the only way across is to place the
unwanted end and erase it. On the tool `R` and `F` stay separate (facing and
role); on a *placed* end they deliberately coincide, which makes "R and F are
interchangeable for undergrounds" an easy over-generalisation to make one
layer up — hence a test for each.

### ctrl+R restarts the simulation

Everything a belt-network edit does — every item in flight discarded, belts
*and* gate channels, via `Engine.flush_material()` — plus the one thing an
edit deliberately does not do: `clock.reset()`, so the tick counter goes back
to 0. The factory itself is untouched, which is the difference from `Delete`.

The counter has to be part of it. An item's position is a closed form over
`clock.tick` and its own entry tick, so a flush that left the counter where it
was would restart the factory at tick four million — arithmetically fine, but
"start over" then leaves the clock somewhere the player cannot see. A board
replacement already resets it for the same reason; this is that half without
the board half.

Seeds go back on **after** the reset, for both reasons at once: a flush
empties every ring, and `seed_segments()` dates its pushes from `clock.tick`,
so seeding first would both be wiped and carry entry ticks from the old
timeline.

`ctrl+R` is **subtracted** from the bare key's meaning rather than merely
checked beside it (`cycle = r_key and not ctrl`). Both halves matter: a bare
`R` that also reset would be catastrophic and invisible, since it is the
most-pressed key in the game; and a `ctrl+R` that also reached the tool would
silently turn whatever is in hand while resetting. Same split `C` and `V` use.

**Q never consults the board when the hand is full.** It is the universal
"put this down", so it must not depend on what happens to be under the
cursor. Only an empty hand looks at the board, and picking up copies the
*orientation* as well as the type (`EntityBuilder.adopt()`) — both halves,
since a mirrored chiral machine looks identical in outline and wires up
differently. `Engine.entity_at()` skips ghosts for this: a preview ghost sits
under the cursor when Q is pressed, and picking up the topmost entity would
hand back what the player already holds.

Every tool names the type it places (`EntityBuilder.entity_cls`), so
`Toolbelt.select_tool_for()` is a generic lookup rather than a per-type
branch. A type with no builder fails softly and leaves the hand empty.

**Q scans both rows, and brings the active row with it.** Failing because
the tool happens to sit on the row the digits are not addressing would be a
surprise — and with Swap and Sink defaulting to row 2 it would be the common
case. The active row is searched *first*, so a type with tools on both rows
resolves to the one whose digits are already live; the row then follows the
selection, or the highlighted slot and the live digits would be in different
rows and the next number key would silently change tools.

Within a row it is **first match wins**, which is a real rule rather than an
accident now that two tools can place the same type: the belt planner and the
raw belt tool both claim `Belt`, so `Q` on a belt lands on whichever sits in
the earlier slot. That makes the answer depend on the player's layout, which
is the price of a customisable bar, and
`test_q_on_a_belt_resolves_to_the_earliest_slot` pins it so the rule is
stated rather than discovered.

### Two rows, and a customisable bar

The toolbelt is **two rows of ten**. The number keys address the *active* row
and `X` swaps which one that is, so twenty tools are each one modifier-free
key away. `X` took the bare key from erase mode (now `ctrl+D`) because
re-aiming the digits is much the more frequent action — the same trade that
sent frame-the-board to `shift+F`.

**The rows are a view onto one flat list.** Row `r` owns indices
`r*ROW_SIZE .. (r+1)*ROW_SIZE`, and `selected` is an index into that flat
list rather than a (row, column) pair. That matters at every call site: a
stashed slot, a persisted layout and `select_tool_for()`'s answer are all one
integer, and none of them has to agree with the orchestrator about which row
is up.

**The row swap changes addressing, not state.** The hand keeps whatever it holds,
even when that tool is now on the inactive row, and any in-flight gesture
survives. Clearing the hand (or jumping to the same column in the new row)
would make it destructive mid-belt-run. The panel draws the active row on
*top* so the swap is visible as an exchange rather than a moved highlight,
and dims the inactive row's digits since they are not live.

**A digit is a column; a clicked slot is a flat index.** The input manager
reports which digit was pressed and `Orchestrator._select_column()` adds the
active row's offset — the same division of labour that has input report `R`
rather than what `R` means. A panel button already knows its own row, so it
selects across rows without needing the swap first. Key `0` is the one digit
with its own branch in that map (it means column 9), which makes it the one
that can quietly keep addressing row 1 while 1–9 follow the swap; tested
separately for exactly that reason.

#### The type menu, and a persisted layout

Right-clicking a slot opens a menu of every tool it could hold, Factorio
style. The list is **generated**: the belt-ish tools from
`builders.BUILDER_KINDS` and the machines from `STRUCTURES`, so adding a
machine puts it in the menu with no change to the panel — the same property
the GLSL tables have.

**The panel only requests.** `UIRequests.set_slot` carries a *description* —
`(slot, kind, type_name)` — and the orchestrator resolves it, matching how
`set_emit` and `set_seed` carry a row. That is load-bearing for the `"stamp"`
kind, whose source is the live clipboard the panel has no business reaching
into.

The layout persists in `gates_config.json` beside the quicksave filename, and
by the same route: `app.py` loads it and calls `Orchestrator.apply_toolbar()`,
and `toolbar_dirty` flags a change for `app.py` to write. **Nothing loads it
implicitly** — the harness builds a real `Orchestrator`, so an implicit load
would give every test run the player's own bar (see core/config.py). It is
written on the edit rather than at shutdown, like the filename, so a crash
cannot lose an arrangement built up over a session.

Three things about the stored form:

- **A kind string is a compatibility surface.** It ends up in the player's
  file, so renaming one silently empties their slot. Structures are not
  listed individually — `"structure"` takes the type *name*, so a new machine
  needs no new kind.
- **Types travel as class names, not the integer ids saves use.** The two
  formats have opposite priorities: a `.npz` holds thousands of grid words
  and wants ids, while this holds a handful of entries and wants to stay
  readable and hand-editable.
- **A bad entry costs one slot.** The file is hand-editable and an older
  version may have written it, so an entry naming a machine this build does
  not have empties that slot and leaves the rest alone. Refusing the whole
  layout would discard the arrangement; *shifting* the survivors left would
  be worse, since position is the content — hence a `None` placeholder rather
  than a skip. A layout longer than the bar is truncated and a shorter one
  padded, so changing `SLOT_COUNT` does not invalidate a saved file.

An absent layout (`[]`) means "no saved layout" and leaves
`default_builders()` alone, so a first run looks exactly as it did before any
of this existed.

#### A baked stamp is a slot tool

The type menu offers **Stamp** whenever something has been copied. It bakes
the clipboard's *current* stamp into a `StampBuilder` nailed to that slot, so
a blueprint worth keeping survives later copies, row swaps and restarts —
paste mode places `Clipboard.stamp`, which one more `ctrl+C` moves on from.

`StampBuilder` subclasses `PasteBuilder` because placing a stamp is placing a
stamp: the plan, the per-click all-or-nothing policy and the intra-stamp
underground pairing are identical. Two differences follow from being a slot
tool rather than a mode:

- **`R` and `F` transform this tool's own stamp.** In paste mode those keys
  go to `Clipboard.transform_active()` because the stamp outlives the plan;
  here the stamp belongs to the tool, so the toolbelt's generic
  `cycle()`/`flip()` dispatch reaches it with no mode branch.
- **It is the one kind carrying data rather than a name**, so its entries
  round-trip through JSON — and what they carry is exactly the per-instance
  settings (a Source's emit value, a belt's seed) that the matched-ghost
  substitution below exists to apply. Losing them on a restart would defeat
  the reason for baking a stamp at all.

### Raw belts: the blunt instrument

`BeltBuilder` is a *route planner* — it reads where in a cell you pointed,
derives a path, and turns corners for you. That is right for laying a run and
wrong for fixing one cell, where the sub-cell rules mean two apparently
identical clicks produce different facings.

`RawBeltBuilder` is the other tool: click places one belt, `R` rotates, and
**nothing is inferred from the pointer**. Both `start_frac` and `end_frac`
are ignored on purpose.

Shaped like `StructureBuilder` rather than subclassing it — a belt is 1×1
with no ports, so centred placement, chirality and the footprint arithmetic
all collapse to nothing, and inheriting them would mean carrying machinery
whose every branch is dead.

**It does not consult `Belt.ROTATABLE`.** That flag says a belt's facing
comes from the path it is drawn along, which is true of the planner and is
precisely what this tool exists not to do. Buying the rotation by flipping a
fact about the *type* would have made the flag lie (and `core/entity.py`
asserts it is `False`), so the rotation lives on the tool.

### Copy, paste and erase are modes, not tools

[core/clipboard.py](core/clipboard.py) owns a mode flag, a stashed toolbelt
slot, and the stamp history. These modes **override** whatever the hand holds
and give it back when they finish, so `ctrl+C` mid-belt-run does not lose the
belt tool. `Orchestrator._active_tool()` is the single accessor that resolves
the override, which is why nothing downstream — gesture style, preview,
commit — needs a mode branch.

**Every exit restores the stashed tool except two,** and both exceptions say
something different:

- **`Q`** drops the stash and leaves the hand empty. Right-press, a plain
  click and `Esc` all mean "done with this mode"; `Q` means "I want to be
  holding nothing".
- **Selecting a toolbelt slot** also drops it, and lands on the picked tool.
  A slot pick is a *new choice* rather than an interruption ending, so
  restoring the stash on top of it would override what the player just asked
  for. `Orchestrator._select_slot()` is the single path for this — both the
  number keys and the panel's slot buttons go through it — and it uses
  `exit_to_empty()` precisely so the `select()` after it is the final word.
  Without that the outcome would depend on the order of those two
  operations.

A stash of `None` is a real value — an empty hand must be restored as empty,
not collapsed to slot 0.

**Copy and erase have no camera pan.** Left-drag is their selection gesture,
so the camera cannot also have it. Paste mode keeps left-drag for panning,
since the stamp follows the cursor and needs no drag of its own. An empty
selection returns you to your tool rather than stranding you in a paste mode
with nothing to paste.

#### Erase mode is copy's gesture with a different consequence

`ctrl+D` enters it and it reuses the whole selection machine — arming, the
threshold promotion, the live rectangle, the aborts — because only the
*release* differs. Two differences, both load-bearing:

- **A release erases and the mode survives.** Copy's release hands its
  rectangle over and leaves; erasing is a stroke you repeat, so the rectangle
  is not a commitment that ends the mode. Sharing the gesture machine makes
  it cheap to accidentally share the exit too, hence a test for exactly this.
- **`ctrl+shift+D` is a cut**: the same rectangle, captured *before* the entities
  are deleted, then straight to paste mode holding it. The stash survives the
  whole chain (`_stash()` refuses to overwrite), so cut → paste → `Esc` gives
  back the tool you held before the cut — same property as `ctrl+C` →
  `ctrl+V`. An empty cut leaves you erasing rather than in a paste mode with
  no stamp.

`cutting` lives on the mode and every exit clears it. Otherwise a player who
cut once would find their next plain erase quietly copying — invisible until
the history filled with things they meant to delete.

**A bare click in erase mode does nothing but leave the mode.** Deleting the
cell under a stray click is too easy to do by accident, and right-drag
already covers cell-by-cell erasing outside the mode entirely. Note a
same-cell "drag" never passes `DRAG_THRESHOLD_PX` and so *is* a click — a
test asserting a one-cell rectangle has to drag far enough to promote.

Erasing is per cell and **touching, not containing**: `Engine.erase()` takes
a multi-cell entity from any one of its cells and releases an underground's
partner, so a rectangle clipping one corner of a Sink takes the whole
machine.

The stash survives the copy→paste switch (`Clipboard._stash()` refuses to
overwrite an existing stash), or capturing would replace the player's real
tool with copy mode's own empty hand.

#### Paste policy is per click, not per tool

A plain click is all-or-nothing; shift places what fits and rejects only the
clashes. Since the preview is built continuously *before* the click, the
orchestrator watches the shift modifier and rebuilds `PasteBuilder` when it
changes — the previewed plan must use the policy the click will actually use.

`R` and `F` in paste mode transform the **stamp**, not the tool: a stamp
outlives any one plan, so they go to `Clipboard.transform_active()` and the
builder is rebuilt around the result. The panel's R/F buttons route through
the same path rather than reaching past the mode into the toolbelt.

#### A pasted underground pair must join to itself

`capture()` records intra-stamp pairing as an **index into the stamp**, and
`PasteBuilder` turns that into the partner's cell.
`Engine.commit_ghosts(paired_cells)` joins those *before* its
nearest-candidate search runs, because the search would otherwise hand a
pasted exit to an unrelated unpaired end sitting at the paste site and leave
the pair's real other half dangling.

Two traps here, both of which bit:

- **A preview's pairing is one-way**, so `Underground.paired` reads true on a
  ghost exit that nothing points back at. Code deciding whether a pair is
  already *made* must check both directions
  (`Engine._under_mutually_paired()`); code deciding whether an end is
  *taken* must ignore a link to the partner it is about to be joined to
  (`_under_spoken_for()`). Using `paired` directly breaks one case or the
  other — the first attempt at each broke the opposite one.
- **Only pair ends that both actually got built.** A partial paste may place
  one half, and a survivor pointing at a row that was never created is
  exactly the dangling state `unpair_under()` exists to prevent.

#### The selection rectangle is screen furniture

Nothing is stored per cell for it. The orchestrator holds the live rect and
`Camera.render()` passes it as `select_lo`/`select_hi`/`select_on` uniforms in
**world** units, so the shader needs no cell arithmetic. It draws over
everything including the board border — a transient overlay must never be
occluded by what it is dragged across — and the outline band is clamped to the
box, since `edge` goes large and negative far away and would otherwise light
up the whole screen. Pinned by framebuffer-readback tests in
[tests/test_item_render.py](tests/test_item_render.py), the only place that
can tell a drawn rectangle from an undrawn one.

`harness.reset()` also clears the **annotation store**, the selected note and
any in-flight stroke, for the same class of reason: a note left behind would
have the next scenario's copy rectangle capture it into a stamp or its `X`
drag report an extra erase, both of which look like a bug in whatever is
under test. It restores `Show notes` to on, since that flag gates clicking
and copying as well as drawing.

`harness.reset()` clears the mode, the stash and the history. Without it a
scenario that left paste mode on would hand the next one a different gesture
machine, and a leftover stamp would make a paste test pass for the wrong
reason. It also clears `inspect_row` and `ui.save_name` for the same class of
reason: a scenario that left the Status window open would have the next one's
`clear_board()` free the row under it, and a leftover filename would point
the save tests somewhere unexpected.

### The Status window

Clicking the board with an empty hand opens **Status** on whatever entity is
there; clicking bare board closes it. That is the whole interaction, and the
click-away dismissal is why bare board is not simply ignored.

**An annotation under the cursor wins the click**, because notes draw over
the board — a label sitting on a factory has to be reachable or it could
never be edited. Only when no note is hit does the click fall through to the
entity beneath, so bare board still closes the window. The window itself
branches on `inspect_note` vs `inspect_row`, never both.

With a text box open, a **drag** inside it moves the box rather than panning
— see "Dragging the open text box".

The orchestrator holds `inspect_row` — a **row, not a view**. Views must never
outlive the frame they were made in, since a row can be freed and handed to an
unrelated entity, so `Orchestrator.inspected()` rebuilds a view per call and
returns `None` once the row is dead. Erasing the inspected entity therefore
closes the window rather than reporting a recycled row's contents under the
erased entity's name.

Ghosts are skipped (`Engine.entity_at()`), for the same reason the pipette
skips them: a ghost is about to be despawned. An empty hand has no preview of
its own, so this is reachable only on the frame a tool was just put down —
which is exactly when it happens.

The panel only **requests**: `close_status`, `set_emit` and `set_seed` come
back in `UIRequests` and the orchestrator applies them, so the window never
mutates board state. `set_emit` carries a `(row, value)` pair and `set_seed`
a `(row, on, value)` triple, for the same reason `inspect_row` is a row.
`set_seed` carries both halves of the setting because the checkbox and the
value box are one thing — whichever widget moved, the other's current value
has to come along or the setter would read a half-update as a change.

Two types have an affordance so far: a **Source**'s emitted value and a
**belt**'s seed, both below. Everything else just reports type, orientation,
footprint, and channel contents — an occupied output zone shows as
`(waiting)`, which is back pressure made visible.

#### A Source's emitted value is per instance

`Source.EMIT_VALUE` is now only the value a freshly placed one *starts* at.
The live value lives in the entity row at `Source.F_EMIT`, and `fire()` reads
it, so two Sources on one board can emit different numbers.

**Both per-instance defaults are `0.0`** — `Source.EMIT_VALUE` and
`Belt.SEED_VALUE`. Arithmetically neutral, so placing either does not inject
a number the player has to notice and correct, and a seeded zero is still
visible since the item disc's ring tints by sign (zero reads red).

That has a consequence for tests, and it bit three of them: **the default is
now indistinguishable from a zeroed row**, so `initialise()` writing the
field and `initialise()` doing nothing at all look identical on a fresh view.
Anything asserting a default has to dirty the field first, or recycle a row
(`test_a_placed_source_starts_at_the_type_default` erases a configured Source
and expects the free list to hand the row back). The same trap caught two
*integration* tests that fed gates from default Sources: `x+y == 0` and
`x == 0` are true of a belt carrying nothing, so those now use distinct
non-zero values — which also makes them catch a swapped channel pair, which
matching values never could.

**`F_EMIT` deliberately squats on an input slot** (`F_IN_VALUE + 0`). A
generator has no input channels — `INPUTS` is empty — so nothing in the tick,
the port pass or the renderer can ever read those twelve slots on a Source.
That matters because the row was exactly full: `MAX_CHANNELS = 6` consumes all
24 post-prefix slots, so appending a field would have pushed `ENTITY_STRIDE`
from 32 to 36 and widened **every** row in the game, including every belt's,
to carry a number one machine has. A self-check asserts `Source.in_channels()
== 0`, because giving Source an input would alias its emit value onto that
channel.

Two consequences:

- **`initialise()` seeds the default.** A zeroed row reads as `0.0`, which is
  a legal emit value and so indistinguishable from a deliberate one. Done on
  the Source class rather than in `Gameboard.spawn()`, the same seam
  Underground uses for its role and partner fields.
- **Changing it flushes every item in flight** (`Engine.flush_material()`),
  belts *and* gate channels — the same thing a belt-network edit does, and
  the same thing the panel's Clear-items button does. `flush_material()` is
  the single meaning of "start fresh"; `clear_items()` is the belts-only
  half and is **not** what a caller usually wants, since material already
  inside a gate re-enters the network next tick (see "An edit drops the
  items…" under Belt segments, where exactly that was a live bug).

  Setting the *current* value is not a change and does not flush, because
  imgui reports a change per keystroke and typing `-1` into a Source already
  at `-1` would otherwise wipe the board mid-edit.

The value travels with copy/paste (`StampEntry.emit` → `Placement.emit` →
`Engine._spawn_fields()`), or a configured Source would paste back at the
default — invisibly, since the paste looks identical. `Placement.emit`
defaults to `None` so the *toolbelt* still places the type default rather than
inheriting whatever was last copied.

#### A belt cell can seed its run

The Status window's other affordance. Ticking **Seed** on a belt makes that
cell put one item of a chosen value onto its run every time segments are
derived — an edit, a board clear, a load. That gives a run something to carry
without wiring a Source to it.

**The seed is attached to the cell, not to the segment.** A segment is
rebuilt from the grid and gets a fresh id each time, so storing it there
would lose it at exactly the moment it is supposed to fire. It lives in the
belt's entity row (`Belt.F_SEED_ON` / `F_SEED_VALUE`), and the flag is
separate from the value because **zero is a legal thing to seed** — the same
trap `Source.initialise()` exists to avoid.

Unlike `F_EMIT`, this could *not* squat on an existing slot. All 24
post-prefix slots belong to the gate block, and while a belt reads none of
them, aliasing would make a belt's seed and a machine's channel one piece of
storage — a coincidence that breaks the first time a belt gains a channel. So
`ENTITY_STRIDE` grew 24 → 28, one vec4, widening every row in the game.
**That invalidates save files written before it**, which the persistence shape
check rejects.

Three consequences:

- **Items enter at the tail, as ordinary pushes.** The closed form only
  accepts an item at a run's entrance, so a seed cannot materialise at its
  own cell's offset — it rides from the start like anything else.
- **Several seeds on one run need staggered entry ticks.** They all enter on
  the same tick, so pushing them all at `clock.tick` puts the first at
  position 0 and has `has_room()` refuse every one after it: four seeded
  cells produced a single item. `Engine.seed_segments()` backdates each
  successive seed by one `d/v`, which is the state a producer running at full
  rate would have reached, so the `min` treats them as the compressed clump
  they are. The oldest must go on *first* — position grows with age and the
  ring requires positions to decrease in `k` — which is why the seeds are
  enumerated up front and the stagger counts down from the total.
- **Changing a seed clears the board's items**, because it invalidates the
  segment index and a rebuild drops everything. Consistent with every other
  edit to the belt network. Setting the value it already has is not a change
  and does not rebuild, for the same reason the emit value checks: imgui
  reports a change per keystroke, and re-deriving on each one would wipe the
  factory mid-edit.

Seeding happens in `sync_segments()` **after** `flush_material()` — a flush
empties every ring, so seeding before it would wipe exactly what was placed.

A seeded cell is **tinted by the sign** of its value, green above zero and
red at or below, matching the item discs' own split so a cell and the item it
produces agree on colour. The sign is mirrored into `.z` (bits 5 and 6)
because the shader reads it from the grid texture with no entity-row lookup —
so changing a seed through the row alone leaves the grid stale, and
`_set_seed()` restamps. The cell carries the sign rather than the value since
that is all a tint can convey.

The seed travels with copy/paste (`StampEntry.seed` → `Placement.seed`) and
through a save, for the same reasons the emit value does; `Placement.seed`
defaults to `None` so a freshly drawn run does not inherit a copied seed —
which would otherwise have every later belt quietly injecting items.

### Annotations: notes on the board

A telestrator layer for labelling a factory — freehand strokes and text
boxes, drawn over the world. [core/annotations.py](core/annotations.py) holds
the model, [ui/annotation_layer.py](ui/annotation_layer.py) draws it, and the
two tools live in `builders.BUILDER_KINDS` alongside the real ones.

**They are not entities, and could not be.** Three independent reasons, each
fatal on its own:

- **Sub-cell and overlapping.** A circle round a region crosses forty cells
  and two notes routinely want the same one; a grid cell holds exactly one
  entity id in `.x`. Same wall frustration hit, which is why that is an
  overlay in `.z`.
- **They carry data no row can hold** — a colour, a thickness, a string.
  `ENTITY_STRIDE` is sized by the gate block and widening it invalidates
  every save (see "A belt cell can seed its run", which paid exactly that).
- **Continuous coordinates.** A stroke point is a world position, so snapping
  to the lattice would make a circle a staircase.

So they are a side table with stable integer ids, and **nothing in
`core/cell.py` or `core/entity.py` changed to add them**.

**Ids are handles, not indices.** Deleting one never renumbers the others and
an id is never reused, which is what makes it safe for the Status window to
hold one across frames — the same hazard `inspect_row` documents, with the
same resolution: a stale id reads as `None` rather than as somebody else's
note. Hence `inspect_note` beside `inspect_row`, at most one set, rather than
one field trying to mean both.

#### Why imgui, and not the world shader

Annotations are mostly *labels*, and the shader's text is a 6×8 bitfield
font: blocky by design, uppercase-only — a fine machine readout and poor
prose. imgui already has a proportional font with kerning and lowercase.

What made it viable is that imgui-bundle 1.92 bakes glyphs **dynamically per
size**. Measured in this venv: 1000 labels at 1000 *distinct* sizes cost the
same as 1000 at one size (~0.5 ms), and a 120-frame continuous zoom sweep
with 300 labels ran at 0.18 ms mean / 0.47 ms worst. Sizing text by zoom —
the thing a world-space label needs — is therefore free. An older imgui,
which baked one atlas font per size, would have thrashed the atlas every
frame and made this unusable.

Two costs, accepted:

- **They draw over everything.** imgui renders after the world pass, so a
  note cannot be occluded or sit under an item disc. Correct layering for a
  telestrator mark anyway.
- **The framebuffer-readback tests cannot see them.** They are tested
  through the model and the projection instead, which is a weaker *visual*
  guarantee and a stronger *semantic* one: a test asserts the exact string,
  not a glyph-shape match.

#### `active` is not `enabled`

Two flags, and collapsing them breaks one case or the other:

- **`enabled`** is the player's Show-notes checkbox, and `draw()` returns on
  it before any imgui call, store iteration or projection — so an unchecked
  box costs one bool test, the same guarantee the segment inspector's window
  guard gives. Pinned by handing `draw()` a camera whose `world_to_screen`
  raises.
- **`active`** is "is there an imgui frame at all", tracked from
  `Orchestrator.draw_ui`. The headless harness builds a real Orchestrator
  with no imgui backend and never calls `new_frame()`, and
  `imgui.get_foreground_draw_list()` outside a frame **takes the process down
  natively** — no exception, no traceback, a silent exit 0 that reads exactly
  like "no tests matched". It deliberately does not gate the *model*, so
  every annotation behaviour except the pixels is testable headlessly.

#### Hiding is not just visual

`enabled` also gates clicking, copying and rectangle-erasing. An invisible
note that still ate clicks or joined stamps would be worse than one that is
simply off, so the panel says so explicitly when the box is unticked — a
player who has forgotten it would otherwise think the notes were lost.

#### Two eraser rules, deliberately different

- **The brush** (right-drag on the draw tool) tests real distance to the
  ink. A bbox test would delete a circle for passing near its empty middle.
- **The `X` rectangle** tests the **bounding box**, for every type. Circling
  a whole ALU therefore gives a large mostly-empty square that a rectangle
  clipping any corner of will take. That is the chosen trade: one rule
  stated once beats a per-type rule learned by surprise, and for a short
  underline or a label the box *is* the ink.

A test pins each, because a suite exercising only one would miss the other's
rule entirely.

#### A paste previews its notes

The stamp's annotations draw as ghosts at `GHOST_ALPHA` alongside the entity
ghosts, positioned from the live plan's own `paste_origin` — the same number
the commit uses, so what is previewed and what is placed cannot disagree
about position. `place_annotations()` returns copies, so previewing never
touches the store or the stamp.

Without it a pasted label is indistinguishable from one already on the
board, and the player discovers the labels only on release.

#### Dragging the open text box

With the Status window open on a text box and an **empty hand**, a left-drag
starting inside that box moves it instead of panning the camera. Not a tool
gesture — the hand is empty — so it is a third `note` kind (`NOTE_MOVE`)
rather than a third annotation tool.

Four decisions, each of which had an obvious wrong alternative:

- **Having the window open is what claims the gesture**, not merely hovering
  a note. Left-drag is the camera pan everywhere else, and stealing it
  wherever a label happens to lie would make panning unreliable across an
  annotated board. Opening the window is the player naming the box they are
  working on.
- **Text boxes only.** A stroke's bbox is mostly empty space, so a drag
  starting "on" a circled region would grab it from a point nowhere near its
  ink.
- **It goes through `STATE_ARMED`**, because a press over the box is
  ambiguous in exactly the way a press anywhere else is: click or drag. Only
  past `DRAG_THRESHOLD_PX` does it become a move, so a plain click still
  selects and a click on bare board still dismisses.
- **The move is a delta, anchored at the press.** A delta so the box keeps
  its grab offset rather than teleporting its origin under the cursor; and
  anchored at the *press* rather than at the threshold crossing, so the
  frames spent crossing the threshold are part of the drag. Anchoring at the
  crossing left the box lagging the pointer by the threshold on every grab —
  the test asserts the delta is exact to 1e-6 for precisely that reason.

**The claim has to be latched, not re-derived per frame.** This is the
subtle one and it was a live bug. `note` comes from the orchestrator asking
"is the cursor over the open box?", and a drag falsifies that within a frame
or two — the pointer outruns the box. Re-deriving it each frame handed the
half-armed gesture back to the generic machine, which promoted it to a camera
pan, and **the box never moved at all**. So `_note_moving` is set by the
press that claims the gesture and cleared on release, and the branch runs on
the flag. `InputManager.cancel()` clears it too, or a cancel mid-drag would
leave the next left-press anywhere on the board dragging the open box.

`cursor_world_now()` exists because the orchestrator must decide *which
gesture to poll* before `poll()` has published `cursor_world` — and last
frame's position misjudges the frame a press lands on, which is the only
frame that matters for arming.

#### And resizing it, from a corner grip

The selected box draws a right-angle grip at its **bottom-right**, and
dragging it resizes instead of moving. It reuses the move gesture's whole
machine — the arming, the latch, the delta — and differs only in what the
delta is applied to, which is why `NOTE_RESIZE` is a second gesture kind
rather than a flag.

- **The grip is sized and hit-tested in screen pixels**, not world units. A
  world-sized grip would be sub-pixel when zoomed out and cover the whole
  label when zoomed in. The drawn target and the clickable target have to be
  the same shape, so `_on_resize_handle()` projects to screen space too.
- **The grip is checked before the body**, so grabbing the corner can never
  be read as grabbing the box.
- **The anchor does not move.** The grip is the bottom-right and the anchor
  is the top-left, so a resize changes the extent only — dragging the corner
  must not drag the whole label. World +Y is up and the box hangs downward,
  so dragging the grip *down* (negative dy) makes it taller.
- **`_note_resizing` latches with the move claim**, for the same reason: a
  resize drags the grip away from the cursor, so re-asking per frame would
  flip to a move partway through.
- **It clamps to `MIN_BOX_W`/`MIN_BOX_H`.** A box dragged to nothing would
  be invisible and then unclickable — unrecoverable except through the
  panel's clear-all.

Strokes get no grip: only text boxes are resizable.

#### The text tool defers to an existing box

A drag whose **start** lands on an existing text box makes nothing and opens
that box instead; so does a plain click. Stacking a fresh empty label on one
already there is almost never the intent — the two would overlap illegibly
and the new one would swallow clicks meant for the old — so the gesture is
reinterpreted as "edit this one". That also means fixing a typo does not
require putting the tool down, matching what the empty hand already does
over a label.

Two narrowing decisions:

- **The start of the drag, not the released rectangle.** Where the gesture
  began is what the player was pointing at when they committed to it;
  testing the whole rectangle would refuse any drag that merely swept past a
  label.
- **Only *boxes* block a box** (`_text_box_at`, narrower than
  `AnnotationStore.hit()`). A stroke is not something a label collides with
  illegibly, and refusing to label a circled region would be a surprise.

#### Notes travel with a stamp, and text stays upright

A copy rectangle touching a note's bbox takes the note
(`capture(..., annotations=...)`), so copying a labelled block reproduces its
labels. They ride in `Stamp.annotations`, separate from `entries` because
they are a different kind of thing — no cells, no orientation, no row — which
is what leaves `place()` entity-only and every existing caller unaffected.

Three rules, each load-bearing:

- **An annotation-only stamp is a real stamp.** `Stamp.__bool__` counts
  both, so a block of commentary is reusable on its own. `__len__` stays the
  *entity* count, since that is what the paste status line and the type menu
  mean by it; `len(stamp.annotations)` is the other number.

  Two consequences worth knowing. `bounds()` falls back to the annotations'
  own box when there are no entities — a zero box would centre the paste on
  nothing and land it at the cursor's corner — and floors/ceils it to the
  cell lattice, since a stamp's box is a cell rectangle everywhere else.
  And `BuildPlan.ok` is true for a plan with **no placements but notes to
  place**: there is nothing to be blocked by, and without that the
  `all_or_nothing and not ok` test reads "zero entities, so nothing
  succeeded" and refuses the paste.
- **Notes do not contribute to `bounds()` while there are entities.** A
  label hanging outside the machines must not move the box and change where
  the paste cursor sits. They shift with `normalise()` regardless, so a
  label keeps its position relative to what it describes.
- **Position transforms; glyphs never do.** A rotated stamp moves its labels
  with their machines and leaves the text readable — the same rule the
  Source emit label already follows ("The label does not rotate with the
  facing"). Mirrored text is simply broken, so the mirror reflects position
  only. The observable form is that a turn must not swap a text box's `w`/`h`
  — and four quarter turns must be the identity, which is what separates a
  real rotation from a drift that compounds.

#### They ship inside the save

Not a sidecar: a note is about a particular factory, so loading a different
board must bring that board's notes and leave none of the previous one's
floating over it. One file also means a save cannot be separated from its
annotations by being moved.

Stored as a **JSON string in a 0-d array** rather than more numpy — the other
arrays are bulk numeric data `.npz` is right for, while this is a handful of
short records with strings in them.

**No `FORMAT_VERSION` bump.** The key is absent from the load's `missing`
check, so every save written before this loads as having no notes — which is
every save in existence. The load clears the store *before* reading, which is
what makes that case correct rather than leaving the outgoing board's notes
over the incoming one. A corrupt blob costs the notes and keeps the factory,
since the factory is the irreplaceable half.

#### The tools are not builders

`AnnotationTool` is deliberately outside the `EntityBuilder` hierarchy rather
than a subclass with stubbed methods: it never produces a `BuildPlan`, never
touches the grid, and never spawns a row. It implements only the small
protocol the toolbelt and panel actually use. `entity_cls` is `None`, which
is what keeps `Q` from ever resolving a board type onto one.

`R` walks the width/size and **`F` walks the palette** — `F` means "mirror"
on every other tool, which a note has no notion of, so rather than leave the
key dead on the two tools whose whole job is choosing a pen it picks the
colour. The pen persists with the slot, like a baked stamp's stamp.

Neither is in the default layout; right-click a slot to add one.

**A clipboard mode wins outright over an annotation tool.**
`_note_tool()` checks `clipboard.active` before `_active_tool()`, because
that accessor only substitutes the *paste* tool — copy and erase mode express
themselves through the input manager's selection branch instead. Asking
`_active_tool()` alone still reported the stashed annotation tool while `X`
was running, and the annotation branch (which returns early) ate the
selection drag: the symptom was an erase rectangle *drawing a new stroke*
instead of deleting one.

**Both mouse buttons are spoken for**, so the branch returns early and
suppresses the camera's left-drag pan and the board's right-drag erase —
exactly as copy and erase mode already do. Panning stays on WASD and
scroll-zoom.

### Persistent UI preferences

[core/config.py](core/config.py) holds a flat JSON file of panel state that
should survive a restart — the quicksave filename, and the customised toolbar
(see "Two rows, and a customisable bar"). Not part of the `.npz`: a save file
is a *board*, and putting the filename there would change it every time you
loaded a different file, while a toolbar belongs to the player rather than to
any one factory.

**Nothing loads it implicitly.** `app.py` calls `load()` at startup and
`save()` when the value changes; `Orchestrator` and `UI` take it as plain
data. That is not ceremony — the test harness builds a real `Orchestrator`,
so a constructor that read the config would give every test run the player's
own filename, and a test exercising the save path would then write wherever
the player had last pointed it. A test asserts neither `__init__` calls into
`config`, checked against the compiled code rather than the source text.

The filename box drives both buttons. **Empty means the default quicksave**,
so the pair behaves exactly as it did before the box existed; a bare name
gets `.npz` appended, and anything with an extension or a directory component
is left alone.

### A drag-based tool previews while idle

The belt planner ghosts a single cell under the cursor even with no plan
started. Without it, "belt planner, no plan" and "empty hand" are
pixel-identical and the player cannot tell whether a click will build. The
ghost's facing comes from `quadrant_dir()`, so it swivels within the cell to
show exactly the heading a single click would produce.

Cancelling a run clears the input manager's hover memo, so the idle preview
returns on the **next** frame rather than waiting for the cursor to move —
otherwise a cancel leaves the hand looking empty. Tests that assert the
settled state after a cancel need that extra `step()`.

### A left-press is ambiguous, so building happens on release

Left-drag pans and left-click builds, which means a press alone cannot be
acted on: it might become either. A press therefore *arms* a gesture
(`STATE_ARMED`) and the next thing the mouse does decides — moving past
`DRAG_THRESHOLD_PX` (6px) turns it into `STATE_PANNING`, releasing without
moving that far is a click.

Two consequences worth knowing:

- A press that arrives mid-plan sets `_pending_commit`. If it becomes a drag
  instead, `STATE_PANNING` returns to `PLANNING` rather than `IDLE` — panning
  must not throw away a half-drawn belt run.
- The threshold is in framebuffer pixels, not cells, so it means the same
  thing at every zoom level.

## Architecture

Ownership is a single chain: `app.py` → `Orchestrator` → {`Engine`, `Camera`,
`InputManager`, `UI`}. `Engine` owns the `Gameboard`, and **nothing outside
`Engine` mutates board state** — the input manager produces intents, the UI
returns requests, and the orchestrator applies both.

The spine — `app.py`, `orchestrator.py`, `engine.py` — stays at the top level;
everything else is grouped by layer.

| file | role |
|---|---|
| [core/cell.py](core/cell.py) | grid cell bit packing; type, direction, and ghost-state constants |
| [core/entity.py](core/entity.py) | `Entity` view base, `Belt`, `Structure` and the machine set, type registry |
| [core/gameboard.py](core/gameboard.py) | grid array, entity table, free list, dirty tracking, device transfer |
| [core/engine_paths.py](core/engine_paths.py) | pure path geometry (elbows, Bresenham) |
| [core/builders.py](core/builders.py) | `EntityBuilder`/`BuildPlan`; `BeltBuilder`, `RawBeltBuilder`, `StructureBuilder`, `UndergroundBuilder`, `PasteBuilder`, `StampBuilder`; the serialisable tool registry |
| [core/stamp.py](core/stamp.py) | copy-stamps: entries, the rotate/mirror algebra, the history ring |
| [core/clipboard.py](core/clipboard.py) | copy/paste mode state, the stashed tool, and `capture()` |
| [core/glsl_types.py](core/glsl_types.py) | emits the per-type GLSL tables from the entity classes |
| [core/font.py](core/font.py) | the 6×8 bitfield font, and the reference number formatter |
| [core/persistence.py](core/persistence.py) | save/load the board to a single `.npz` (`.w` zeroed — derived) |
| [core/config.py](core/config.py) | persistent panel preferences (quicksave filename, toolbar layout), loaded only by `app.py` |
| [core/annotations.py](core/annotations.py) | strokes and text labels: the side table, its ids, and the hit rules |
| [ui/annotation_layer.py](ui/annotation_layer.py) | draws annotations through imgui's draw lists; the only place they become pixels |
| [core/toolbelt.py](core/toolbelt.py) | two rows of ten slots, one active tool, one active row |
| [core/tick.py](core/tick.py) | the simulation clock: tick counter, fixed timestep, phase order |
| [core/segments.py](core/segments.py) | belt runs coalesced into the O(1) simulation unit; derived from the grid |
| [core/belts.py](core/belts.py) | closed-form item transport, and the naive oracle it is tested against |
| [core/belt_device.py](core/belt_device.py) | packs segment + item state into SSBOs for the renderer |
| [core/gates.py](core/gates.py) | gate firing, and the port callbacks joining gates to belts |
| [engine.py](engine.py) | preview/commit a plan, erase, ghost lifecycle |
| [render/camera.py](render/camera.py) | pan/zoom, world↔screen, the world render pass |
| [render/shader_loader.py](render/shader_loader.py) | reads `shaders/`, resolves `#include` |
| [ui/input_manager.py](ui/input_manager.py) | input → intents; the gesture state machine |
| [ui/panel.py](ui/panel.py) | imgui panel and the toolbelt bar |
| [orchestrator.py](orchestrator.py) | wires the above; owns per-frame ordering |
| [app.py](app.py) | GLFW/context setup and the main loop |
| [shaders/common.glsl](shaders/common.glsl) | cell encoding + SDF helpers, included by the rest |
| [shaders/world.vert](shaders/world.vert) | fullscreen quad |
| [shaders/world.frag](shaders/world.frag) | grid, belts, structures, ghosts, frustration tints |

`ui/ui.py` is named `panel.py` so `from ui import …` is unambiguous inside the
`ui` package.

### Cell encoding

The grid is `(H, W, 4) int32`, uploaded as an `RGBA32I` texture. Channel `.x`:

```
bits 0-1    direction   0=+X 1=+Y 2=-X 3=-Y
bits 2-8    type        0=empty, 1=belt, 2.. machines (see cell.TYPE_NAMES)
bits 9-30   entity id   row index into the entity SSBO
bit  31     unused — deliberately clear
```

`.y` packs `(local_x, local_y)` so a multi-cell entity's cells each know their
offset from the origin — the **placed** offset, after flip and rotation, not
the authored one (see "Orientation" under Structures). `.z` bits 0-1 hold the
ghost state (`0` real / `1` ghost / `2` frustrated / `3` matched — see
"Frustrated cells" below; the field is now full), bit 2 the flip flag,
bits 3-4 an underground end's role and pairing,
and bits 5-6 a belt's seed and that seed's sign. Everything in `.z` past the
ghost state is there for one reason: the renderer resolves it per-cell
straight from the grid texture, with no entity-row lookup — so each is
mirrored from the row and a change that skips `restamp()` leaves the grid
stale.

`.w` carries belt-segment membership: bits 0-15 the offset along the segment
from its tail, bits 16-30 the segment id (`0` = not on a segment), bit 31
clear as in `.x`. Both live in the cell because the fragment shader needs
them for a texel it has **already fetched** — deciding whether an item disc
covers this pixel costs no extra texture reads. This is derived state (see
"Belt segments"), stored rather than recomputed because the renderer cannot
walk a run per pixel.

Three things to know before touching this:

- **The id field is 22 bits, not the 23 the layout has room for.** Bit 31 is the
  int32 sign bit; a negative cell word would break Python-side comparisons and
  need sign-aware GLSL unpacking. Widening the id would reintroduce that.
- **Emptiness is `type == 0`, not `word == 0`.** `pack_cell()` collapses
  `TYPE_EMPTY` to exactly `0` so both tests agree in practice, but code should
  ask `cell.is_empty()`.
- **The packing is duplicated in GLSL**, in
  [shaders/common.glsl](shaders/common.glsl) — the single GLSL copy, which
  every shader `#include`s. `cell.py` runs round-trip assertions at import, and
  `tests/test_glsl.py` parses the constants back out of the shader source
  and compares them against `cell.py`, so the two copies cannot silently
  diverge.

### Entities are views, not objects

`Gameboard.entities` is a `(capacity, ENTITY_STRIDE) float32` array and is the
single source of truth. An `Entity` holds only `(gameboard, row)` and reads and
writes through properties, so there is no second copy to drift. Views are cheap
and disposable — **never cache one across frames**, since a row can be freed and
reused underneath it.

Rows come from a free list, so **an entity id stays valid for that entity's
life** regardless of unrelated deletions. That is what makes it safe to embed
the id in grid cells. The table grows by doubling and never shrinks within a
session; compacting it would require a grid-wide id remap and is not implemented.

`is_ghost` is the one piece of entity state that lives in the grid rather than
the row (the renderer needs it per-cell). A view caches it in `_ghost` so it can
be stamped before the cells exist; `None` there means "read the grid".

Footprints and faces are **class** data rotated on demand
(`footprint_for`, `cells_for`, `faces_for`), not per-instance storage. The row
stores the already-rotated extent so `footprint_w/h` describe the entity as it
actually sits on the board.

### Dirty tracking and device transfer

`Gameboard` keeps a union rectangle for the grid and a union row-range for the
entity table. `to_device()` uploads only those regions
(`texture.write(..., viewport=...)` / `buffer.write(..., offset=...)`) and clears
them. A moving ghost preview dirties only a handful of cells per frame, so the
preview continuously exercises the partial-upload path — a 3-cell preview on a
512×512 board uploads 3 cells.

`from_device()` is **not** symmetric: moderngl 5.12's `Texture.read()` has no
viewport parameter, so it always transfers the whole texture and slices on the
CPU. Its `rect` argument is a convenience, not a bandwidth saving. Readback
exists for verification (`verify_round_trip()`, surfaced in the UI) and for
future compute passes, not for per-frame use.

### Frame order

`Orchestrator.frame()` runs: poll input → apply to camera/engine → build UI and
apply its requests → **advance the clock** → `to_device()` → render world →
(caller renders imgui). The device sync sits after all mutation and before
rendering, so what is drawn is always what the CPU believes. Input is polled
before the UI is built so the panel shows current state rather than lagging a
frame.

The clock advances after player mutation and before the device sync — the one
window where both hold: a tick acts on the board as just edited, and its
effects reach the GPU the same frame rather than a frame late.

### The simulation clock and the two-phase tick

[core/tick.py](core/tick.py) owns *time*. Nothing else decides when a tick
happens or in what order its phases run. It is separate from `engine.py`
because everything tick-dependent is one concern and `engine.py` already
carries the placement API.

**Fixed timestep.** The sim runs at `TICKS_PER_SEC` regardless of framerate;
`advance(dt)` banks real time and fires whole ticks out of it, so the same
tick sequence produces the same board at any framerate. `alpha` is the
leftover, 0..1 — how far into the next tick we are — and reaches the shader
as `sim_tick` + `sim_alpha` so items interpolate smoothly instead of snapping
between slots. The split keeps integer precision: the tick counter grows
without bound and a float32 loses sub-tick resolution past ~16.7M ticks.

**The backlog is capped** (`MAX_CATCHUP_TICKS`) and, when it trips, the
accumulator is *zeroed* rather than charged per tick. Charging only for the
ticks that ran would leave the dropped time banked for the next call to fire —
the backlog the cap exists to prevent — and subtracting all of it can leave a
near-full period from float residue, parking `alpha` at ~1.0 and having the
renderer interpolate a whole tick past the last one that ran.

**A tick is two phases, and the order is load-bearing:**

1. **structure** — gates that can fire, fire. Reads input ports and waiting
   zones; writes waiting zones and clears inputs. Touches no belt state.
2. **belt** — waiting zones drain onto belts (or straight into a
   directly-connected gate's input port), belts advance, belt heads load into
   gate input ports.

Two phases rather than one because a gate's fire decision depends on whether
its output is backed up. Updating together would make that answer depend on
whether the downstream belt had already advanced this tick — execution-order
dependent, and so nondeterministic the moment it becomes a compute dispatch.
Split, each phase reads state frozen for the whole phase and writes state the
other reads.

Structure *before* belt specifically, because that is what lets a well-fed
gate fire every tick with no bubble: the structure phase gets first look at a
zone the belt phase emptied one phase earlier. Reverse them and a gate sees a
full zone every other tick and runs at half rate. Consequences:

- **Bubble-free output needs `d == v`** — spacing equals speed, items
  nose-to-tail. A belt accepts a new item only once the previous moved a full
  `d`, i.e. every `d/v` ticks, so a sustained one-item-per-tick is specifically
  the maximum-density configuration.
- **A stall costs exactly one tick to recover**, and that tick is unavoidable
  without letting the structure phase read belt state.
- **One tick of latency per hop**, so a chain of `k` gates has `k` ticks of
  pipeline latency. It applies equally to a belt hop and a direct gate-to-gate
  connection, which is what keeps the two consistent.

**Phases are registered, not hardcoded.** Subsystems register a callable per
phase; the clock knows nothing about gates or belts. That keeps it testable
with no board, and means the GPU port replaces the *bodies* of those callbacks
with compute dispatches while the sequencing stays as it is. The barrier
between phases is implicit now (one returns before the next is called) and
becomes a real `glMemoryBarrier` later.

A board replacement — `clear_board()`, `load_board()` — calls `clock.reset()`.
Item positions are a closed-form function of the tick counter, so a new board
must start a new timeline or every item on it is dated to a tick that never
happened for it.

### Belt segments

[docs/belt_report.md](docs/belt_report.md) gives a closed form for every
item's position on a "pure" belt — one line, no branching, no mid-line
insertion, uniform speed. Nothing per-item is updated after the item enters,
so a whole run ticks in O(1) however many items are on it.

That is only valid for a **maximal run between two nodes**, so the unit of
simulation is the *segment*, not the cell. The player draws cells;
[core/segments.py](core/segments.py) coalesces them.

**Segments are a cache over the grid.** The grid stays the single source of
truth — delete every segment and they recompute from it. Membership is still
*stored*, in `.w`, because the renderer needs it per-pixel.

- **An edit drops the items on the segments it touches.** Splitting a run
  means dividing a ring's live contents between two new rings, and every
  position is relative to its segment's anchor, so it is not expressible in
  the closed form. Dropping them is the price of O(1); edits are human-paced.

  **And it must drop gate state too, which it did not.** A rebuild empties
  every ring, but material that has already *left* a belt sits in an entity
  row — a value in a gate's input channel, a result in its output waiting
  zone — and segment code never touches those. The next tick pushed them
  back onto the fresh runs, so items the player watched vanish reappeared a
  tick later. `sync_segments()` now calls `Engine.flush_material()`, which
  clears belts *and* gate channels.

  Two things made this hide well. It is **bounded, not continuous** — about
  one stray value per gate port that happened to be holding something — so
  it reads exactly like a Source refilling. And a test asserted the old
  behaviour by name (`gate_state_survives_a_segment_rebuild`), on the
  reasonable-sounding grounds that gate state lives in rows and a rebuild is
  derived-state churn. The lesson is that "derived state" is about where a
  value is *stored*, not about whether it is live material.

  The flush is deliberately **board-wide**. An item is a payload and an entry
  tick with no record of which machine produced it, so "only the gates near
  the edit" is not a question the closed form can answer. Pinned by
  `test_an_edit_does_not_resurrect_items_onto_the_belts`, which changes the
  Source's emitted value at the same time as the edit so any surviving old
  value is identifiable — asserting merely that the belts emptied is not
  enough, since they do empty and then refill from the gates.
- **Ring capacity is a power of two.** Required, not tidy: slots are indexed
  `k % cap` on a monotonically growing counter, which only survives unsigned
  wraparound when `cap` is a power of two.
- **Undergrounds add capacity without cells.** A paired entrance/exit
  contributes `span + 1` positions — the two ends plus buried tiles that hold
  items nobody draws. The run continues through as though the gap were belt,
  which costs the O(1) trick nothing. This is why a segment's `length`
  (positions) is separate from its cell list, and why each cell stores its own
  offset rather than using its index in that list.

  **A paired exit is therefore not a tail**, even though nothing adjacent
  feeds it — its entrance is several cells away across the span, and
  `_incoming()` only probes the four neighbours. Missing this started a
  second trace from the exit over cells the entrance's run already claimed;
  the duplicate only *appeared* when the exit came first in the row-major
  scan, so westward and southward runs broke while eastward ones looked fine.
- **Rebuilds are deferred to one per frame.** `Engine.invalidate_segments()`
  only marks stale; `sync_segments()` rebuilds once, before the tick. An erase
  drag over fifty cells rebuilds once.

**Invalidation is hooked at the board, not at each call site.** Every grid
write funnels through `Gameboard.write_cell()` / `clear_cell()`, which notify
`on_grid_change` — so a new edit path cannot forget to invalidate.

**The hook compares *real* cell content, not raw writes** (`_real_content()`).
A moving ghost preview rewrites cells every frame, and since derivation skips
ghosts those writes cannot change its answer; notifying anyway rebuilt the
whole index once per frame for an identical result. Preview churn now causes
**zero** rebuilds.

That guard became load-bearing rather than merely an optimisation when
`sync_segments()` started flushing material (above). A spurious rebuild used
to cost a wasted whole-board scan; it now **empties the board**, so without
this a ghost dragged across a running factory would wipe it once per frame.
`test_preview_churn_does_not_destroy_items` asserts the consequence rather
than the rebuild count — removing the guard takes a 19-item belt to 0.

**Frustrated cells count as real.** This is the subtle one, and it was a live
bug: frustration is an overlay that leaves `.x` untouched precisely so the
occupant survives, so skipping those cells cut a run in two for as long as the
player held a blocked structure over it — which, once items exist, would
silently destroy them. Only `GHOST_PENDING` is fictional.

#### Merges are resolved, not supported

Belts never merge here, but nothing stops a player pointing two at one cell.
`take_dir()` picks exactly one source: **straight-through first, then
CCW-most** from the receiving cell's own facing. Losers are simply not
connected.

It is mirrored in `common.glsl` and **both sides must agree** — the renderer
previously drew an incoming arm per candidate, showing material arriving down
a path nothing travels (the "phantom merge"). `draw_belt()` now calls
`take_dir()` and draws the one arm the simulation actually uses.

A related rule: **the cell we output into is never a source**, even when it
faces back at us. Without it two nose-to-nose belts each name the other as
upstream, neither is a tail, and the pair falls through the closed-loop path
as one bogus segment.

**A machine can feed a run from any side, so `_node_behind()` probes all
four.** It used to look only at the cell directly behind the belt's own
facing, which silently missed the normal way to use a multi-port machine — a
Source's north face feeding an eastward run read as *dangling*, so items
flowed nowhere and the generator blocked with a full zone. Contention there is
resolved by the same `take_dir()` the belt path and renderer use, so a cell
with several things pointing at it takes from exactly one.

`_node_ahead()` deliberately stays single-directional: a belt delivers to the
one cell it faces.

### Belt transport: the closed form

[core/belts.py](core/belts.py) implements docs/belt_report.md. Every item's
position is an expression, never a stored value:

```
pos(k, t) = min( (t - enter_tick[k]) * v,  anchor - (k - head) * d )
```

The first term is where the item would be if never blocked, the second where
it would be if packed tight behind the head. The `min` decides which without
anything tracking the clump boundary, and since both grow at rate `v`, **once
an item is packed it stays packed** — Factorio's permanent compression, for
free. `enter_tick` is written once; `head`, `tail` and `anchor` are the only
mutable state, which is what makes a run O(1) however many items are on it.

**Tick order is advance-anchor → pop → push**, straight from the report.

**Fixed point, not floats.** `L`, `v`, `d` are integers in `UNIT`-ths of a
cell because the pop test is `pos == L` *exactly*; float drift would
eventually miss it and stall a belt permanently with an item a hair short of
the end. At the default `v == d == UNIT` positions are whole cells.

**`alpha` interpolation is exact, not approximate.** Both terms are linear in
time, so evaluating at `tick + alpha` is the same expression — clumps form
smoothly with no snapping.

Two bugs here, both found by the randomised oracle comparison and neither
visible at whole ticks:

- **The pop's `anchor = L - d` assumes `L >= d`** — a requirement the report
  states and a short run can violate. A 1-cell belt with 3-cell spacing put
  the anchor at -2 cells, and the next item to enter took that as its packed
  position and rendered *behind* the belt's start. Now clamped at 0, which
  changes nothing where the precondition holds.
- **The fractional anchor needs the same clamp as the whole-tick one.**
  `tick_segment()` does `min(anchor + v, L)`; the render path omitted it, so a
  fully blocked clump slid forward as `alpha` rose and snapped back on the
  tick, with the head drifting clean off the end. The report's render formula
  has it: `A_render = min(A + α*v, L)`.

#### The oracle is the point

`naive_tick()` is the report's reference implementation — store every
position, move them one at a time. Obviously correct, far too slow to ship,
and its only job is to disagree with the closed form.
[tests/test_belts.py](tests/test_belts.py) runs both on identical
producer/consumer decisions and compares **positions and pop/push events**
every tick, across 600 randomised configurations plus fixed edge cases
(`v > d`, `d > v`, `d > L`, 1-cell runs, ring wraparound).

This matters because the closed form is the part most likely to be subtly
wrong and least likely to *look* wrong: an item a fraction of a cell out of
place reads as plausible motion. Keep the oracle when this moves to compute
shaders — the report recommends exactly that, and it is how the GPU port will
be validated.

The randomised sweep also checks **fractional-time invariants** at every
tick, which is what caught the second bug above; whole-tick sampling alone
missed it entirely.

#### Driving it before gates exist

`BeltSim.tick()` takes `offer(seg)` / `accept(seg, payload)` callbacks per
segment, mirroring the report's `producerOffers` / `consumerAccepts`. Belt
transport knows nothing about gates. Until the gate layer lands,
`Engine.debug_feed` stands in: set it and every run with a **dangling**
upstream end is offered that value each tick, and every dangling downstream
end accepts. It deliberately skips runs a real gate already feeds, so it
cannot conjure items into the middle of a chain.

Registered on `PHASE_BELT` at `Engine` construction. **Remove a phase
callback with `Clock.off_phase()`, never by clearing the phase** — the
engine's transport is on that list, and a blanket clear silently stops the
whole simulation. A test cleanup did exactly that, and the symptom surfaced
three files later as "the belt phase ran but nothing moved"; `harness.reset()`
now asserts the callback is still there.

### Gates

[core/gates.py](core/gates.py) fires machines; [core/entity.py](core/entity.py)
says what each one computes. A gate fires when its inputs are ready **and
every output waiting zone is clear**, then consumes its inputs and puts
results in those zones.

| machine | operation |
|---|---|
| `Shear` | x,y → x, x+y |
| `Mac` | x,y,z → x, y, z + x·y |
| `Reciprocal` | a → 1/a |
| `Max` | a,b → max(a,b) on **both** outputs |
| `Floor` | a → ⌊a⌋ |
| `Route` | a,b → a down **one** of two outputs, on the sign of b |
| `Merge` | a,b,c → whichever of a,b the control c selects |
| `Source` | → a per-instance constant on every connected port |
| `Swap` | x,y,z → x, (z,y if x>0 else y,z) |
| `Sink` | destroys whatever arrives, up to 4 per tick |

**A positive control is always the active case.** `Swap` swaps when x>0,
`Route` sends right when b>0, `Merge` selects `a` when c>0. Zero counts as
the inactive branch, so a control reads as a plain condition. Swap's two
branches were the other way round at first, which reads backwards — the
machine sat exchanging its lanes whenever its control said not to. Pinned in
`core/entity.py`'s self-check and in [tests/test_gates.py](tests/test_gates.py);
note [tests/test_stamp.py](tests/test_stamp.py) also asserts Swap's polarity,
since its mirror-invariance check needs to know which branch is the
pass-through.

**`Swap` and `Sink` are placeable again.** They were hidden when the toolbelt
had exactly ten slots and every one was spoken for, which made "is this
machine worth a slot?" a real question — Sink lost it to the rule that a
dangling output destroys what it is sent. Two rows and a right-click type
menu remove the question: a machine the player never picks costs nothing.

What settled it was that hiding a machine was not actually free. The pipette
resolves a type *through its tool* (`Toolbelt.select_tool_for()`), so `Q` on
a Sink already sitting on the board did nothing at all — a dead key on a real
entity. Sink is still redundant for discarding an output (leave the face
unconnected instead), but it is no longer unreachable.

**The mechanism survives with an empty set.** `HIDDEN_STRUCTURES` is `()` now
rather than deleted, because the split it expresses is the load-bearing one:
`STRUCTURES` is what *tools* derive from and `ALL_STRUCTURES` is what
anything reasoning about *types* iterates — the type-geometry self-checks,
the GLSL tables. A machine moved into the hidden set keeps its class and its
type id, so a saved board containing one still loads, draws and simulates;
only the tool goes away. That asymmetry is what made hiding a machine a
one-line change, and it is worth keeping available.

**Type ids are append-only** for the same reason they always were: they are
baked into every saved grid word, so renumbering would silently reinterpret
old saves. Re-offering a machine changes no id.

`test_a_machine_placed_without_a_tool_still_works` is the descendant of the
old hidden-machine test. It now pins the *mechanism* — the two sets'
relationship, and that a machine placed straight through the engine with no
tool involved simulates correctly, which is the path a **load** takes.

#### A dangling output is a sink

A result sent to an output face with **nothing on the far side is destroyed
at fire time**, rather than filling a waiting zone nobody will drain. So
leaving a `Shear`'s pass-through unconnected no longer stalls the machine: it
runs at full rate and the pass-through evaporates.

This is a deliberate end to reversible computing here — a gate may now
destroy information. When "backwards" mode arrives, these discard points are
where a machine will have to remember a little of what it dropped.

It **collapsed a special case rather than adding one**.
`GateSim._connected_channels()` used to be consulted only by generators, on
the grounds that a machine *consuming* inputs must write every result or
silently lose material. Now that losing it is the specification, every gate
consults the same connectivity, and it decides two things: which zones can
block it (`outputs_clear(drains)`) and which results are discarded.

**Generators keep exactly one difference**: `_fire_generator()` fills
whichever free zones it finds rather than requiring *all* of them clear.
`outputs_clear()` over the connected set is all-or-nothing, so using it there
would let one slow belt stall every other belt the same Source feeds — the
opposite of what a multi-port generator is for. Skipping dangling faces still
matters *more* for a generator, though, and for the reason it always did: a
generator creates material from nothing, so emitting into a dangling face
would be a pure print-and-shred loop rather than a result being dropped. With
**no** channel connected it stays inert.

Pinned by `test_a_dangling_output_destroys_and_does_not_block`, which asserts
the **rate** — one discard and one item per tick — rather than merely that
the gate kept firing. A half-rate gate is exactly the symptom a mistake here
would produce, and "it fired at least once" would not catch it. That test is
the inverted descendant of `test_an_undrained_output_blocks_the_whole_gate`.

A consequence for the tests: a bare gate on an empty board no longer fills
its zones, so anything wanting to observe back pressure must attach a real
consumer and let it back up (`_drain_every_output()`). Note a one-cell run
holds **two** items at the default spacing, so those tests loop until blocked
rather than assuming a fire count.

**A face only counts as connected if its channel actually owns the run.** A
2×1 Source has two `+Y` faces and a long run touches both, but a segment
takes from exactly one port (`take_dir()`); the loser must read as
unconnected or it fills a zone nothing will ever drain and sits there as a
permanently stuck item. `_face_has_consumer()` asks `_src_port()` rather than
comparing `seg.src` by hand, because a face *direction* cannot tell two ports
of the same gate apart — the gate **cell** adjacent to the tail can.

This is the one place the structure phase reads the grid, and it is safe
because it reads belt *presence* (static for the whole phase) rather than belt
*state*, whose read order would make firing nondeterministic.

**Firing is declared, not special-cased.** `FIRE_RULE` is `FIRE_ALL`
(every input needed — a Shear cannot compute x+y from x alone), `FIRE_ANY`
(Sink's rule; waiting for all four channels would deadlock a sink fed from
one side), or `FIRE_SELECT` (Merge's). A type with **no** inputs is
vacuously ready under any of them, so `Source` fires whenever its output is
clear with no special case anywhere — and its rate self-regulates to
whatever the belt will take.

`inputs_ready()` is **overridable**, because an enum cannot express a rule
that depends on the values held rather than on occupancy. `FIRE_SELECT`
names that case and the base class deliberately does not resolve it — a type
declaring it must override, and the self-check pins that it does. Falling
through to the `FIRE_ALL` branch instead would make such a machine wait for
a lane it is supposed to be able to ignore: silent, and exactly the bug the
enum value exists to make visible. The enum survives because it is also the
declared home for a future per-type rate limit.

#### Two mechanisms the second gate vocabulary needed

Both are general, and neither is special-cased anywhere in the tick:

- **Emitting on one output and not the other.** `fire()` returning `None`
  for a channel already meant "emit nothing"; it is now a load-bearing
  decision. `Route` sends its item down one of two paths and the other stays
  **silent** — not a copy that is then destroyed, which would look identical
  on the taken path. `test_route_emits_nothing_on_the_path_not_taken` runs
  15 ticks to tell those apart, since one fire cannot distinguish "nothing
  was emitted" from "not arrived yet".

- **Consuming only some inputs.** `structure_phase()` used to clear every
  held channel, giving the machine no say. `Entity.consumed()` now reports
  which channels a fire used up, defaulting to every fed one. `Merge` waits
  for its control and takes only the lane that control names; the other
  **stays loaded in the machine**, neither emitted nor destroyed, until a
  later tick's control selects it. The default would have silently eaten it.

  It is returned rather than mutated, so the structure phase stays the only
  writer of gate state — the same discipline that keeps `fire()` a pure
  function of its inputs.

**Values are clamped, not allowed to go infinite.** `VALUE_LIMIT` is 2²⁴,
the point past which float32 integers stop being contiguous. `1/0` clamps
there (keeping the sign of the zero) rather than reaching a belt as an
infinity nothing downstream can represent or render, and `Floor` clamps
*before* flooring because `math.floor(inf)` raises rather than returning a
value. **NaN passes through unchanged** — it is the one value meaning "this
computation had no answer", and `Max` reports it rather than ordering it.
A real decision about saturating vs. wrapping vs. an error item is deferred.

**Each machine's arithmetic is one `fire()` method** taking channel values
and returning channel values, indexed by declaration order. That indexing is
the only contract between a machine's geometry and its arithmetic.

#### Channels are not port faces

`in_channel()` / `out_channel()` map a port face to a gate-state slot,
defaulting to the port's index. `Sink` overrides it to fold its eight faces
onto four channels, one per footprint cell.

**A channel is one item per tick, because it is one slot** with one occupancy
flag. That single fact decides every fold on the board, and it cuts both ways:

- **`Sink` folds, but only per cell.** The eight faces really are
  interchangeable as *values* — a sink destroys whatever arrives — so folding
  them looked free. It was not: all eight on channel 0 meant the first belt
  to load it made `accept_from()` refuse the rest until the next structure
  phase, so **two saturated belts into one sink delivered one belt's
  throughput**. Measured at 20 items per 20 ticks against the correct 40.
  Per cell, each of the four tiles drains independently.
- **`Source` does not fold at all.** Same arithmetic from the output side:
  two belts sharing a channel would split one *emit* per tick instead of each
  getting a full run. That is what makes "one Source fills two belts" work
  (`test_one_source_fills_two_belts`).

So the two machines pull in opposite directions for the same reason, and
`test_two_saturated_belts_feed_one_sink_at_full_rate` asserts the **rate**
rather than merely that items arrived — one belt alone still passes a
non-zero check, which is exactly how the bottleneck went unnoticed.

The remaining cost is that a single tile's two faces share a slot, so feeding
one corner from both sides gets one item per tick between them. Spread the
belts across tiles.

This matters for sizing: `MAX_CHANNELS` is 4, now set by **Sink**, so the
gate block needs `8 + 4×4 = 24` slots. One channel per *face* would have
needed 8 and widened every row in the game, including every belt's.

Source used to set it at 6, carrying all six perimeter faces; narrowing that
machine to north-and-east is what let both constants come down (stride 32 →
24, a 25% narrower row for every entity on the board). `ENTITY_STRIDE` is now
**28**: the belt seed added one vec4 past the gate block (see "A belt cell can
seed its run"). Note the stride is baked into save files, so the persistence
shape check rejects older ones — each of these changes invalidates saves
written before it.

The renderer needs the same mapping — `output_channel_at()` is generated from
Python, because "port index == channel" is false for Sink and indexing by
face would read slots that do not exist.

#### Back pressure is the only mechanism

Nothing anywhere asks "is the belt full". A gate reads **its own** output
waiting zones; the belt phase empties them when it can. If a belt is full the
zone stays occupied and the gate does not fire next tick. That is required,
not merely tidy: a gate must not read belt state during the structure phase,
or firing would become order-dependent.

**An occupied waiting zone at the end of a tick is a renderable state** — it
means the gate produced something the belt refused. `draw_waiting()` draws it
squished into the gap between the body edge and the cell boundary, compressed
along the travel axis so it reads as *nowhere* rather than mispositioned.
While material flows the zones are always empty by end of tick, so nothing
shows.

A practical consequence when building: **every output must be drained, not
just the interesting one.** A Shear emits x unchanged as well as x+y, and
leaving the pass-through dangling stalls the machine completely after a
single fire rather than degrading it. Pinned by a test.

#### The port work happens *inside* the belt tick

This is the subtle part, and getting it wrong is cheap to do and expensive to
notice. `offer_to()` / `accept_from()` are passed to `BeltSim.tick()` as the
producer and consumer callbacks, so one belt tick does
**advance-anchor → pop → push** exactly as docs/belt_report.md requires.

Doing the gate work in separate passes *around* the belt tick — drain, then
transport, then load — breaks that sequence. The drain pushes before the
anchor has advanced and before the pop, so it sees a belt one slot fuller
than it is about to be and declines. A saturated chain loses roughly **one
tick in seven** (measured: 26 items over 30 ticks against the correct 30).

It presents exactly like the one-tick stall recovery the design *does*
document, which is what makes it easy to accept. It was written that way
first, and `test_well_fed_gate_fires_every_tick` caught it — which is the
main reason that test exists. Note that merely transposing the pop and push
blocks *within* `tick_segment` is a different and much louder bug: the
oracle comparison in `tests/test_belts.py` catches that one immediately,
while the throughput test does not notice.

A separate `took()` callback clears the waiting zone only after a push
actually happened, so offering is not committing. That split is **defensive
rather than load-bearing today**: `BeltSim` tests for room *before* calling
`offer()`, so the only refusal possible is the gate having nothing to give,
and clearing the zone inside `offer_to()` would behave identically — no test
catches a mutation that does. It stays split because the protocol is then
correct by construction rather than by where the room check happens to sit,
and a producer that can be asked speculatively (a splitter choosing between
two belts) would make it matter at once.

**Direct gate-to-gate connections** (`deliver_direct()`) are handled apart
from the belt path, since there is no segment to own the transfer. One tick
per hop, same as a belt, which keeps the two cases consistent.

A port face has exactly one neighbour, so each (gate, channel) pair has one
upstream source and the writes are disjoint — no contention even though many
gates write inputs in the same pass.

### Text rendering

[core/font.py](core/font.py) holds a **6×8 on/off bitfield per character**,
two 24-bit ints, generated into `common.glsl` at the ids marker exactly as
the per-type tables are. A pixel finds which glyph cell it lands in, tests
one bit, and takes a box distance against that cell — so a character costs
an integer op and one `sd_box` whichever character it is. No texture, no
sampler binding, no asset on disk, and no 36-way branch over glyph shapes.

This replaced a seven-segment display that could draw ten digits and a
handful of fakeable letters. The alternatives and why not:

- **A bitmap or SDF atlas** is cheapest per pixel but is a *resource* — a
  texture to generate or ship, a binding, and for SDF either a committed
  binary asset or a startup generation pass. Both cut against "nothing is
  written to disk, `F5` needs no build step".
- **Per-glyph stroke SDFs**, which is what `sd_glyph` does for the nine
  machine marks, are prettiest and scale perfectly — but 10 digits plus the
  alphabet is 36+ hand-authored functions behind a branch chain evaluated
  per pixel per column. Fine at nine, poor at thirty-six.

The visible trade is that glyphs are **blocky** — a dot-matrix readout
rather than smooth type. Deliberate for a factory game's machine displays,
and it antialiases through the usual `fill()` because each lit cell is a
real box in world space, not a magnified texel.

**6×8 rather than the classic 5×7**: 5×7 fits in one int but compromises
`4`, `M`, `W` and `X` and rules out lowercase. 48 bits needs two ints
either way and two ints cover 8×8, so the wider cell is free.

#### The formatter has a Python oracle

`format_number()` exists twice: in GLSL, which ships, and in
`core/font.py`, which exists to disagree with it — the same relationship
`belts.naive_tick()` has to the closed form.

That is worth the duplication because **a formatter bug draws a perfectly
crisp wrong number**. There is no visual artefact, and the pixel tests can
only see that something value-dependent appeared. The seven-segment
formatter had no Python counterpart and so was pinned by regexes over the
shader source, which can assert that a branch exists but never that it
computes the right thing.

Policy is a few knobs rather than a rewrite: `sig` digits after the point,
`trim` to drop trailing zeros (which is what turns `-1.00` into `-1`), and
`NUM_FIXED_HI_E`/`LO_E` for the fixed↔scientific boundary — mirrored by
`font.FIXED_HI_E`/`LO_E`, since the reference is only an oracle if it
switches format where the shader does.

**A minus costs a column, and trimming paid for it.** The seven-segment
version spent all three columns on digits and left the sign to the ring
alone; `-1` is two glyphs where `1.00` was three.

**`NUM_MAX_CODES` bounds every buffer** (GLSL has no dynamic arrays).
Overflowing it does not crash — `num_push()` drops the excess — it
*silently truncates a number*, so `tests/test_glsl.py` sweeps the formatter
and asserts nothing reaches the bound. It was 10 against a true worst case
of 13 (`-1234567.0000`) until that sweep included the length-maximising
values rather than tidy representative ones.

#### Reading the pixels back

[tests/test_item_render.py](tests/test_item_render.py) identifies each
rendered glyph by downsampling it onto the 6×8 grid and matching against the
font table, so a pixel test can assert **what the number said** rather than
that something was drawn. Three things this needs:

- **Slice at the layout's advances, not at blank columns.** A `1` has a
  blank column inside it, so gap-finding segmented `1.11` into nine
  fragments. The expected string reconstructs the pen positions instead.
- **The decimal point's narrow advance has to be followed on both counts** —
  it advances `NUM_POINT_ADV` rather than `FONT_W` *and* is drawn shifted
  left within it. Missing either reads the next digit one cell late.
- **Aspect ratio breaks ties between solid rectangles.** A `-` is 4×1 cells
  and a `.` is 2×2; cropped to their own ink both are filled boxes that
  match each other perfectly, and `2.5` read as `2-5` until the score
  accounted for shape.

#### What the font tests cannot see

Every other font check compares the table **against itself** — the GLSL is
generated from it, and the pixel tests identify glyphs by matching back to
it. So swapping two glyphs' shapes is self-consistent and passes
everything. Mutation-tested, and it did: exchanging `2` and `5` was
invisible to the entire suite.

`test_each_digit_has_the_shape_its_name_implies` is the independent
statement — enclosed holes, vertical symmetry, and where each digit's
full-width bar sits — and it names the two dangerous pairs explicitly:
`0`/`O` (identical at 6×8 without the zero's slash, which is why the slash
is there and why `0` has *two* holes) and `2`/`5` (vertical mirrors with no
holes and no symmetry, so only bar placement separates them).

### Item rendering

Items draw as discs with a coloured ring — **green above zero, red at or
below** — sized by `ITEM_RADIUS` in [world.frag](shaders/world.frag)'s
tunables, and carry their value as text (see "Text rendering" below).

**The ring and the number say the same thing at different zooms.** The
number is exact and the ring is only a sign, but the ring survives all the
way down the zoom range while the text fades out below legibility
(`NUM_MIN_PX` / `NUM_GOOD_PX`, in *glyph pixel height* rather than zoom,
because that is the quantity legibility actually depends on). So zooming out
degrades gracefully rather than going blank. That redundancy is why dropping
the sign glyph would have been defensible and why adding it was still worth
it — the ring cannot tell you a value, only which side of zero it is on.

**The shader recomputes positions; it never reads them.** Materialising
O(items) of state per frame is exactly the cost the closed form avoids, so
[core/belt_device.py](core/belt_device.py) uploads `head`/`tail`/`anchor`,
the ring contents, and the constants, and `item_position()` in
`common.glsl` evaluates `min(unblocked, packed)` per pixel. It is a second
implementation of the formula, so `tests/test_glsl.py` pins its pieces
against `BeltSim.position_of()`.

Layout: two parallel arrays of fixed-size vectors (`ivec4` + `vec4` per
segment, indexed by segment id) plus a flat item arena carved by prefix sum
over `cap`. Not one packed struct — std430 alignment for mixed-type structs
is a reliable source of silent corruption. Bindings 1/2/3, pinned against
`render/camera.py` by a test.

**The scan's `reach` is gated on the labels being drawn.** A number may spill
past the disc rim, so when labels are visible the scan has to reach half the
widest string the formatter can produce — about 2 cells, against
`ITEM_RADIUS`'s 0.22. Paying that unconditionally widened the bisect bracket
and ran the `ITEM_SCAN` loop far more often than needed at *every* zoom,
including the whole range below ~18 px/cell where `text_opacity()` has
already faded the labels to nothing — which is precisely where the most items
are on screen. Gated, the hidden-label case is **3.5× narrower**.

`text_opacity()` is therefore evaluated once for the cell, above the loop,
rather than per item: it decides `reach` before the bisect begins. Pinned
structurally in `tests/test_glsl.py` rather than by timing, because the win
is a smaller scan window that no test here can observe — and
mutation-testing shows the pixel tests do not notice the guard's removal at
all.

**Finding which items cover a pixel is by bisection over `k`.** A pixel knows
its segment and offset from `.w`; the items that could reach it are found by
bisecting on position, then scanning a few forward. The only property that
always holds is monotonicity — position strictly decreases in `k` whichever
term of the `min` binds — so bisection is correct in every case. Two
cheaper-looking brackets were tried first and both drew discs in some
situations and not others:

- Inverting the **packed** term algebraically. It is an upper bound derived
  from the anchor, which on a free-flowing belt sits far ahead of every real
  item, so the result lands past `tail` and finds nothing.
- Clamping that result into range. It then skips the *oldest* items — a run
  drew its first four discs and dropped the rest.

**Items draw last, over the frustration tint.** An item is cargo *on* a cell,
not part of it. The tint is a 55%-alpha red wash over the whole cell, so
drawing items under it made a green item read as red-brown — reporting the
wrong sign — for as long as the player held a blocked structure over the
belt.

**Only `GHOST_PENDING` suppresses items**, not `GHOST_FRUSTRATED`: a
frustrated cell is a real belt with real items, the same distinction segment
derivation makes. (The guard is currently defensive rather than
load-bearing — a ghost only spawns on an empty cell and `clear_cell()` zeroes
`.w`, so `draw_items()` returns early anyway. A future tool that previews
*over* existing belt would make it matter.)

Buried underground positions hold items but have no cell to draw in, so items
pop out of existence across a span and back — deliberate, per the design.

#### Pixel tests

[tests/test_item_render.py](tests/test_item_render.py) renders offscreen and
reads the framebuffer back. These are the only tests that can catch a bug
living between a correct simulation and a correct-looking picture, and writing
them found four:

1. **`Segment.anchor` was initialised in cells, not fixed point.** The
   simulation hid it completely — the unblocked term of the `min` is the
   smaller one until a belt backs up — so it surfaced only when the renderer
   read the anchor directly and drew nothing. `BeltSim.audit()` now checks it
   on a fresh segment.
2. and 3. The two broken brackets above.
4. **Items drawn under the frustration tint** (above).

Three habits these tests need:

- **Sample by peak colour cast, not mean.** A disc is antialiased and blended
  over the belt; averaging in the surrounding grey drops the hue below any
  threshold.
- **Pin `alpha` to 0** (`_at_whole_tick()`) when comparing against whole-cell
  offsets. `harness.step()` banks real dt, so two screenshots either side of
  one are taken at different sub-tick times and items legitimately move
  between them — which looks exactly like a rendering bug.
- **Reload the shader first.** `Camera` compiles once at harness import, so an
  edit to the shader source afterwards is invisible otherwise. This hid a
  whole round of mutation testing.

Note that a *red* item under the red frustration tint is indistinguishable
from the tint alone — the frustration test uses green items for that reason,
and passed for the wrong reason until it did.

#### Observability

Segments are derived state, whose failure mode is disagreeing with its source
while staying internally consistent. Two tools, both free when off:

- **`SegmentIndex.audit()`** checks the index against the grid cell by cell —
  stamps, offsets, ring invariants, double-claims, orphaned belt cells.
  Asserted clean throughout [tests/test_segments.py](tests/test_segments.py),
  including after 40 random edits.
- **`BeltSim.audit()`** checks the *items* rather than the segmentation:
  positions within bounds, ordering, no pair closer than the spacing, the
  anchor not past the end. The two fail for quite different reasons, so the
  inspector's Audit button runs both.
- **The `SEGMENT_DEBUG` shader overlay** tints each run by a hash of its
  segment id, with a dot on the tail. Behind `#ifdef`, so toggling it
  recompiles and the disabled build does not contain the code at all —
  `shader_loader.load()` takes a `defines` set, injected after `#version`.
  `Camera.set_define()` drives it.

The **Segments** inspector window (checkbox in the main panel) shows counts,
id-pool state, capacity totals, the hovered segment's full ring state, and a
button for each of the above. Everything expensive is inside the
window-open guard.

It also lists the hovered run's **items** — position, payload, entry tick,
and whether each is `packed` or `free`, i.e. which term of the `min` is
binding. That distinction is the heart of the closed form and otherwise
completely invisible. The positions shown are computed live, not read back
from storage, because there is nowhere to read them from.

### Builders: one mechanism for every placement tool

**Belt-drawing and structure-placement are not separate systems.** Both are
`EntityBuilder`s that turn a gesture into a `BuildPlan`; the engine knows only
how to preview and commit a plan. Future tools — blueprint paste, copy/paste,
undo — are meant to fit the same shape rather than grow beside it.

```
EntityBuilder.plan(engine, start, end) -> BuildPlan
BuildPlan.placements   every entity the gesture wants
         .buildable    the subset that will actually be created
         .frustrated_cells   cells to tint red
         .all_or_nothing     policy flag
```

The two builders differ in exactly two ways:

| | `BeltBuilder` | `StructureBuilder` |
|---|---|---|
| placements | many 1×1, one per path cell | one, possibly multi-cell |
| `all_or_nothing` | `False` — build around obstacles | `True` — any clash blocks it |
| `drag_based` | `True` (press, move, click) | `False` (hover, click) |
| `R` means | cycle path shape | rotate 90° (`shift+R` reverses) |
| `F` means | nothing | mirror, if the type is chiral |
| `adopt()` takes | nothing | the rotation and the flip |

`drag_based` is the one place the gesture style leaks out: a click-based tool
never enters `PLANNING`, it previews under the cursor while `IDLE` and commits
on a press. Both styles hand the builder the same `(anchor, target)` pair, so
builders do not care which produced them.

Both styles must also keep `anchor_frac`/`target_frac` current — the `IDLE`
hover branch updates them alongside `anchor`/`target`. They used to be
maintained only on the drag path, which left a click-based tool planning from
whatever fraction some earlier gesture happened to end on; harmless while
structures ignored the sub-cell position, and a visible mis-placement as soon
as they didn't.

### Frustrated cells are an overlay, not an entity

This is the subtle part of the design. A frustrated cell is one a pending build
wants but cannot have — which means **something is already there**. A grid cell
holds exactly one entity id in `.x`, so spawning a ghost entity on top would
destroy the occupant. Frustration therefore lives **only in `.z`** and leaves
`.x` completely untouched, which is what lets the renderer tint the cell red
*over* the existing entity instead of replacing it: you can see what is in your
way.

Consequences worth knowing:

- `Engine.clear_ghosts()` clears **two** things — ghost entities (which own
  rows and cells, and are despawned) and frustration overlays (which own
  neither, and are unset without disturbing the occupant).
- `Gameboard` tracks the frustrated cell list so it can clear them without
  scanning the board.
- A frustrated cell is *not* a ghost. `cell.is_ghost()` is false for it;
  `cell.is_frustrated()` is the separate test.

#### A matched cell is frustration that means the opposite

`GHOST_MATCHED` is a frustration whose occupant is **exactly what the
placement wanted** — same type, origin, orientation and flip. Two structures
of the same type at the same rotation look identical in outline, so a red
tint over a perfect match says "blocked" when the truth is "already
correct". Matched cells tint green instead, and that colour is the whole
feature: it is how the player sees a stamp is *lined up*.

It cost **no storage**: the ghost field is 2 bits and value `3` was spare.

**It is a flavour of frustration, not a third kind of cell.** `.x` is
untouched exactly as before, and `cell.is_frustrated()` covers *both* — that
is the broad "is this an overlay?" question, and everything asking it wants
both: segment derivation (which must not cut a run at a tinted cell) and
`clear_frustrated()` (which must unset the overlay without disturbing the
occupant). `cell.is_matched()` is the narrow test, used in exactly two
places: the shader's tint and the commit path. Getting the breadth wrong is
a real bug in either direction — reading matched as un-frustrated cuts a belt
run under a lined-up stamp; reading it as a ghost entity has
`clear_ghosts()` despawn the occupant's row. The shader folds matched onto
frustrated in `layer_of()` for the same reason, or a half-lined-up stamp
would split a run into three rendering layers.

#### Substituting a lined-up stamp

A bare click over a plan whose **every** clash is a match deletes the matched
entities and places the stamp in their stead
(`Orchestrator._substitute()`). That is the point of the whole mechanism:
per-instance settings are reachable only one entity at a time through the
Status window, so changing twenty Sources means twenty trips. Instead,
configure one, copy it, rotate until it lines up, click.

- **Matching is per *placement*, never per cell, and the origin is part of
  it.** Two 2×1 Shears offset by one cell share a cell that compares equal
  type-and-facing on its own, so a per-cell test would call a half-overlap a
  match and move a machine one cell from where the player saw it.
  Mutation-testing shows the origin comparison is *redundant* given the
  "every cell is that same row" loop — two instances of one type can only
  disagree about their origin by covering a cell the other does not — but it
  states the invariant the loop merely implies.
- **Orientation and flip are part of identity.** The flip is the dangerous
  one: a mirrored chiral machine looks identical in outline and wires up
  differently, which is why `adopt()` copies it too. That makes it the match
  a player could not spot by eye, and it is mutation-tested — an earlier
  version of its test passed against a build with the check removed, because
  the mirror had changed the *direction* as well and the refusal came from
  there.
- **All-or-nothing.** `Engine.matched_rows()` returns `None` if anything
  clashes that is not a match, because deleting the matched entities would
  not unblock the real clash — the plan would still refuse and the player
  would have lost them for nothing. An empty list is the different answer
  "nothing was blocked".
- **Erase everything, then re-plan.** The plan in hand was computed against
  the occupied board, so every placement carries `blocked_cells` and
  `buildable` is empty; it cannot be committed as-is. Clearing the whole
  matched set up front also means the re-plan never sees a half-deleted
  underground pair, whose partner `erase()` releases.
- **Bare clicks only.** Shift already means "place what fits, reject the
  clashes" and overloading it would leave no way to ask for that. The guard
  is *defensive rather than load-bearing* — shift makes a paste `partial`,
  which makes the plan not `all_or_nothing`, so the branch is already
  unreachable — but that reasoning runs through two other objects, and a
  tool that was all-or-nothing *and* honoured shift would otherwise silently
  acquire substitution.

A note for the tests: a substitution that quietly did nothing and a refusal
leave **identical-looking boards**, since the old and new entities are the
same type in the same place. So every test asserts on something that
distinguishes them — a changed emit value, a changed seed, a live count.
"A Source is still there" passes either way. The row id is *not* such a
signal: the erase frees the row and the free list hands it straight back.

### Previewing is not the same as building

Two distinctions in `BuildPlan` that are easy to collapse and shouldn't be:

- `buildable` — what commit will create. Empty for a frustrated
  all-or-nothing plan.
- `previewable` — what to *show*. A blocked structure still previews the part
  that would fit, because the shape tells the player how far to move to clear
  the obstruction.

So a half-blocked `Shear` ghosts its free cell and tints its blocked one, and
commits nothing. Those partial ghosts go in `Engine.preview_only_rows` rather
than `ghost_rows`: on commit they are **despawned, not promoted** — half a
structure is not a thing that can exist. They are stamped with
`Gameboard.spawn_cell_ghost()`, which claims a row but writes exactly one cell
with a 1×1 extent; a normal `spawn()` would stamp the whole footprint and
overwrite the very cells doing the blocking.

### Previews must not see themselves

`Engine.preview(builder, start, end)` clears the old preview **before** asking
the builder to plan. This ordering is load-bearing: ghosts are real entities in
real cells, so a plan computed against a board that still holds last frame's
preview collides with it — drag a structure one cell sideways and the overlap
with its own previous position reads as blocked, flickering red as you move.
`preview_plan(plan)` (taking an already-built plan) exists for tests and has
the same hazard, which is why callers should prefer `preview()`.

Because belts are 1×1 and not all-or-nothing, a belt run across an obstacle
still commits — only the overlapping cells are frustrated. A structure with any
cell blocked commits nothing but still tints every blocked cell. Same
mechanism, different policy.

### Belt path planning

Three path modes cycle with `R`: horizontal-first elbow, vertical-first elbow,
and a Bresenham line. All paths are **4-connected** — belts cannot step
diagonally, so `bresenham()` deliberately produces a staircase. For an
axis-aligned drag all three modes produce the same cells, so `R` appears to do
nothing; this is correct, and the UI names the active mode to make it legible.

**Directions are derived from the unfiltered path, then filtered alongside it**
(`BeltBuilder.plan`). This ordering matters: a belt's facing must not depend on
what happens to be in the way. Deriving directions after filtering means a cell
whose successor was blocked has no step to read and falls back to `+X`, which is
how "complete the last cell of a 2×2 loop" used to produce an east-facing belt
instead of one that closes the loop.

**Where in a cell you point steers the ends of a run.** `InputManager` reports
the cursor's fractional position within its cell (`cursor_frac`, 0..1 each
axis, y up) alongside the cell index, and the builder uses it at both ends:

- **First belt of a single-cell run** — there is no step to read a heading
  from, so `quadrant_dir()` splits the cell by its diagonals and names the
  direction. Clicking the right of a square gives an east belt, the top a
  north one. Dead centre is `+X`.
- **Last belt of any run** — goes straight on by default, but if the pointer
  is more than `TURN_THRESHOLD` (0.6) of the way toward a side edge, it turns
  that way and forms a corner. Only *sideways* deflection counts: pushing
  along the travel axis already means "keep going", and pulling backwards
  would reverse the belt into the run it came from.

**`quadrant_dir()` is the single authority, and both questions go through
it.** They did not always, and the bug is instructive: `_exit_turn` carried
its own threshold-box version, testing one axis directly. A box has corner
regions where *both* axes are past the threshold, and whichever axis the code
tested first won — and since that test ran perpendicular to the diagonal
split, it flipped the answer systematically. Tracing a circle around the cell
centre, **24 of 36 sampled angles faced the wrong way**, with the middle of
the north-east arc reading as east.

Two things keep it fixed, and only the first is load-bearing:

- `_exit_turn` asks `quadrant_dir()` which way to turn, then applies the
  threshold on the axis the turn is actually onto. So the turn a corner
  produces and the heading a single click produces can never be different
  directions for the same pointer position. The threshold still does its one
  real job — making *straight* sticky, since the quadrant alone would turn
  the moment the pointer crossed a diagonal.
- A single-cell run skips the exit-turn entirely. Mutation-testing shows this
  guard is **redundant**: `_exit_turn` now returns `None` there anyway,
  because a one-cell run's heading *is* the quadrant and so reads as
  "straight on". It stays because asking a question that does not apply and
  relying on the answer being `None` is a subtler thing to depend on than not
  asking.

Pinned by a 36-angle sweep of the **ghost** (what the player reads before
committing) plus a 12-angle sweep asserting the committed belt matches its
own preview. The four axis-aligned samples the older test used are exactly
the points where the two rules happened to agree, which is how a bug covering
two thirds of the cell survived them.

`StructureBuilder` uses `end_frac` too, but for a different purpose: picking
which cell boundary an even-sized footprint centres on (see "Structures").
A tool that cares about neither simply ignores both.

### Underground belts

A pair of 1×1 ends that jump a gap, Factorio-style. `Underground.MAX_GAP` is
4 tiles, putting the ends at most `MAX_SPAN` (5) apart — matching the base
Factorio underground, with faster tiers left for later.

**The two ends are separate entities**, each holding the other's row in
`F_PARTNER`. That matches how they are placed — one click each,
`UndergroundBuilder` alternating entrance then exit — and makes an unpaired
entrance a perfectly legal thing to leave on the board. A single entity
spanning the gap would be the obvious alternative and is wrong: `Gameboard`
stamps every cell of a footprint, so the span would claim the tiles it must
leave free. **Nothing is stored for the tiles between the ends** — belts and
machines build straight over them, which is the entire point.

Both ends share a `direction`, the travel direction, and the exit is
downstream of the entrance. Pairing happens in `Engine`, not the builder:
`find_under_partner()` scans along the facing for the **nearest** unpaired
end of the opposite role, so laying a long run chains naturally instead of
latching onto an older entrance further back. Nothing blocks a span.

- **Pairing is mirrored into `.z`** (bit 3 = this end is an exit, bit 4 =
  paired) because the renderer draws the funnels and the dotted span from the
  grid texture alone. `Gameboard.restamp()` exists for exactly this: changing
  the row without it would leave the grid stale.
- **Erasing one end releases the other** (`Engine.unpair_under`). A survivor
  left pointing at a freed row would draw a span to nothing, and the free
  list could hand that row to an unrelated entity.
- **A ghost exit's pairing is one-way.** While you hover a placeable exit it
  points at a real entrance that does not point back, purely so the span
  draws in the preview; the entrance is never mutated, so abandoning the
  preview leaves no trace. `commit_ghosts()` therefore clears that one-way
  link before pairing properly — otherwise `paired` reads true and the real
  two-way link never forms. The shader draws the span when **either** end
  claims the pairing, which is what makes the preview visible at all.
- Out of range, nothing is drawn — no "would not connect" feedback.

Rendering: each end is a funnel whose **mouth opens toward the surface belt
it talks to** — an entrance opens backwards at the run feeding it, an exit
forwards at the run it feeds — tapering to a throat on the buried side.
`sd_funnel()` draws one shape and mirrors it for the entrance. The span is
dotted by the cells *between* the ends, each scanning outward for a paired
run that covers it; nothing is stored per-cell. `UNDER_MAX_SPAN` in the
shader is pinned against `Underground.MAX_SPAN` by `tests/test_glsl.py`.

An underground is **not** a `Structure` — `is_structure()` excludes it, since
it has no port tables and moves material along one facing like a belt. Only
an *exit* emits onto the surface; an entrance is diving, so `feeds_into()`
reports nothing for it.

### Structures

The machine set lives in `core/entity.py`. **Three tuples, and the
distinction matters**: `STRUCTURES` is what the toolbelt builds tools from,
`HIDDEN_STRUCTURES` holds types that are defined but not placeable, and
`ALL_STRUCTURES` is their union — what anything reasoning about *types*
rather than about *tools* should iterate, including the geometry self-checks.
All are authored at `DIR_PX` and rotated; all are continuous rectangles.

| type | footprint | signature |
|---|---|---|
| `Shear` | 2×1 | x,y → x, x+y |
| `Source` | 2×1 | outputs on its north and east faces (3 ports) |
| `Route` | 2×1 | a,b → a down one of two outputs; b passes through |
| `Merge` | 2×1 | a,b,c → the lane c selects; c passes through |
| `Reciprocal` | 1×2 | a → 1/a |
| `Max` | 2×1 | a,b → max(a,b) on both outputs |
| `Mac` | 2×1 | x,y,z → x, y, z + x·y |
| `Floor` | 2×1 | a → ⌊a⌋ |
| `Swap` | 1×2 | x,y,z → x, (z,y if x>0 else y,z) |
| `Sink` | 2×2 | two inputs per cell, folded to one channel each |

The **default** layout is the belt planner, the raw belt tool and underground
in slots 1–3, then this table's order from slot 4 — thirteen tools, so Swap
and Sink start on row 2. It is only the starting arrangement: the bar is
customisable and persisted (see "Two rows, and a customisable bar").

The machines carrying a control share an idiom: a **pass-through channel**
crosses the body on one axis unchanged, while the channel taking the *long
way* across the footprint is the one that gets mutated. `Swap`'s x runs
vertically through the whole 1×2 body (in at the top cell's north face, out at
the bottom cell's south) while y and z cross their own cell horizontally.
`Shear` passes x down through cell 0 only; `Mac` is `Shear` plus a second
vertical control channel down cell 1. `Route` and `Merge` run their control
horizontally across both cells instead, west face in and east face out.

**A machine that mutates every channel it touches declares no
`PASS_THROUGH`**, so no faint belt is drawn across it — `Reciprocal`, `Max`
and `Floor`. That absence is the visual difference from `Shear`, where the
overlay means "this signal survives unchanged".

**Adding a machine is one Python class.** An `Entity` subclass declaring
`FOOTPRINT`, `INPUTS`, `OUTPUTS`, `PASS_THROUGH`, `COLOUR` and `GLYPH`, plus
an entry in `STRUCTURES` and `cell.TYPE_NAMES`. **No GLSL changes at all**
unless it wants a new glyph — see "Generated GLSL tables" below. The
builders, the toolbelt, the stub logic and the port arrows are all generic.
Five machines were added at once this way and needed no change to any of
them.

The in-place turn is generic too: a new machine gets `R`/`F` with an empty
hand for free, and which turns it permits follow from `FOOTPRINT` (see "R and
F with an empty hand"). Only a type that is *not* a solid rectangle would need
to say anything, by setting `INPLACE_TURNABLE = False`.

A new *glyph* is the one thing still hand-written: a `sd_*` function in
[world.frag](shaders/world.frag), a `GLYPH_*` constant in
[common.glsl](shaders/common.glsl), and an entry in `glsl_types.GLYPHS`.
They are abstract marks rather than text — a divide sign for `Reciprocal`,
stacked chevrons for `Max` — because "1/x" and "MX" are multi-character
strings and a bitmap font is its own project.

- **Ports are per-cell, not per-type.** A port belongs to one cell of a
  footprint — `Shear`'s y-input is on cell 0 only, `Source` has one port on a
  4-cell body. `Entity.face_masks_at(lx, ly)` is the Python authority and the
  GLSL `*_face_mask(type, lcl, facing, flipped)` pair mirrors it. Spreading a
  port across every cell of a footprint would draw arrows on the wrong cells
  and stub to the wrong neighbours, so `tests/test_glsl.py` pins them **cell by
  cell**, at every orientation, and rejects shader ports outside the
  footprint.

#### Orientation: authored space vs placed space

This is the subtle part, and it has already caused one visible bug.

A type is authored at `DIR_PX` and placed by **flipping first, then rotating**
(`Entity.place_local`). `.y` therefore holds the **placed** offset, not the
authored one — `Gameboard._stamp_footprint()` writes offsets within the
*rotated* bounding box.

So anything that reads `.y` back and wants to ask about ports **must invert it
first**, via `Entity.unplace_local()` / the GLSL `unplace_local()`. The
renderer skipping that inversion is what put a rotated `Sink`'s ports on
interior edges: the port's *face* was being un-rotated while its *cell* stayed
put, so the two no longer agreed.

**A cell and its faces must always transform together.** The invariant worth
remembering is that a port always faces *out* of the footprint — the cell one
step along a port's direction is never part of the same entity. `core/entity.py`
asserts exactly that for every type, rotation and flip, which is a far
stronger check than comparing face masks: an aggregate mask over the footprint
stays correct even when the cell-to-face pairing is scrambled.

- **Flip is `.z` bit 2**, alongside the ghost state, because the renderer
  resolves ports per-cell straight from the grid texture and so needs the flag
  without a round trip through the entity row (where it also lives, as
  `F_FLIPPED`). A neighbour probe must read the *neighbour's* flip bit
  (`is_flipped(n.z)`), not the calling cell's.
- **Chirality is derived, not declared** (`Entity.is_chiral()`): it mirrors the
  port layout and compares. **Only `Sink` is achiral**, its ports covering
  the whole perimeter so a mirror permutes them onto each other; `F` is a
  deliberate no-op for it rather than a special case in the toolbelt. Every
  other machine is chiral.

  **Ports on a body's centre line do not make it achiral**, which is easy to
  get backwards: the mirror is about the authored **X** axis, so it exchanges
  north and south. A `Reciprocal`'s north input lands where its south output
  was, meaning a flipped one takes its input at the bottom — `F` reverses
  which way material flows through it, a genuinely useful second orientation.

  The self-check asserts this per type **against `is_chiral()` itself**
  rather than against a hardcoded list of machine names, in both directions
  (a chiral type's mirror must differ at every facing; an achiral type's must
  not). That is what caught the wrong assumption above, and it means a new
  machine is covered automatically whichever it turns out to be.

  **`Source` became chiral for free** when it dropped to north-and-east
  ports — the six-face version was symmetric and `F` did nothing on it.
  Nothing in the toolbelt or the input layer changed, which is the payoff for
  deriving this from the ports instead of declaring it: narrowing a machine's
  port set gave it a working mirror as a side effect. Mirroring sends the two
  north faces south and leaves the east port on the far cell.
- **Faces are derived, not stored** (`Entity.faces_for`). Every instance of a
  type has the same port layout, so storing it per-row would be duplication.
  If ports ever become per-instance configurable, that is where it changes.
- **The origin is the footprint's lower-left corner**, and `cells_for()`
  extends in +X/+Y from it rather than pivoting about an interior point. That
  is the storage model everywhere — rows, cells, `.y` offsets.
- **Placement is centred on the cursor, not cornered on it.** A structure
  straddles the pointer so a rotation turns about its middle instead of
  swinging the body around a corner. `Entity.origin_for_cursor()` converts
  cursor position → origin, and `StructureBuilder.plan()` is the single
  place that calls it: a gesture becomes a placement there, and nothing
  downstream needs to know.

  **An even axis snaps to the nearest cell boundary, not the nearest cell.**
  An odd extent has a middle cell, so the footprint centres on the cursor's
  cell and the sub-cell position is irrelevant. An even extent has no middle
  cell — its midline is a cell *boundary* — so the pointer picks which
  boundary, giving twice the placement resolution: a 2×1 follows the cursor
  every half cell, and a footprint even on both axes snaps its centre to the
  nearest corner. Working in continuous coordinates makes this fall out:
  round the desired midline onto the lattice the extent allows (integers for
  even, half-integers for odd).

  Two invariants hold for every type, rotation and sub-cell position, and
  `core/entity.py` asserts both at import: the cursor is always inside what
  it is placing, and the footprint's midline tracks the pointer to within
  half a cell.
- **Erasing any cell of a multi-cell entity removes the whole entity.** There
  is no such thing as half a structure.
- The renderer recovers a footprint's extent by probing neighbours for a
  matching entity id, so a multi-cell entity draws as one merged rectangle with
  a single arrow. Merged edges extend slightly *past* the cell boundary —
  stopping exactly at it leaves a visible antialiasing seam.

Right-press is consumed by the cancel when PLANNING, so cancelling never also
starts an erase. Erase drags interpolate between the previous and current cell,
so a fast drag leaves no gaps.

## Rendering

All visual constants — colours, line widths, chevron spacing, port geometry —
live in one `TUNABLES` block at the top of
[shaders/world.frag](shaders/world.frag) as `#define`s. They are expected to
change often; nothing below that block hardcodes a number you would want to
tweak. `F5` hot-reloads.

**Draw order is grid → entities → border.** Grid lines go down first, *under*
the entities, so belts visibly join across cell boundaries and a multi-cell
structure is not sliced up by them. Only the board border draws over
everything, so the playfield edge stays readable.

### The window is resizable, so the viewport is re-stated every frame

`Camera.render()` sets `ctx.viewport` from the size it was handed, on every
pass rather than once at startup. The GL viewport does not follow the window
and the default framebuffer never resizes its own, so without this the pass
keeps drawing into the rectangle the context was created with: enlarge the
window and the new region stays the clear colour, as unused grey.

Everything *else* was already resolution-independent and stayed untouched —
`resolution`, `screen_to_world`, `frame_board` and the shader's
`gl_FragCoord.xy - resolution * 0.5` all derive from the live framebuffer
size, which is why the bug showed up as dead space rather than as a wrong
projection.

Two things worth knowing:

- **The passed-in size is the authority, not `ctx.screen.size`.** A caller
  rendering to its own framebuffer (the pixel tests) passes that target's
  size and must not have the window's imposed on it. Note moderngl caches
  `ctx.screen.size` at context creation and **never refreshes it**, so after
  a resize it is stale and `ctx.screen.use()` restores the old viewport —
  re-stating per pass is what makes that harmless. `ctx.clear()` is
  unaffected (it ignores the cached size with no scissor set), so `app.py`
  needs no resize handling and has none.
- **Only a pixel test can see this**, and the obvious two ways to write it
  both pass against the broken build. Binding a framebuffer **resets the
  viewport to that framebuffer's full size**, so a test that binds a large
  target and renders covers it whether or not the pass sets the viewport —
  `test_the_world_pass_fills_a_resized_window` deliberately leaves a stale
  too-small viewport set before rendering, which is the state a resized
  window is actually in. And the sentinel must be a colour the world never
  draws: cleared to the app's own dark grey, an undrawn pixel and a
  legitimately-empty one are identical. Both mistakes were made first.

GLSL has no `#include`, so [render/shader_loader.py](render/shader_loader.py)
implements a minimal one by textual substitution, with an implicit include
guard (a file already pulled in is skipped). That exists so the cell encoding
has exactly one GLSL copy.

**`packed` and `patch` are GLSL reserved words.** Both were used as local
variable names and both produced a bare `syntax error, unexpected '='` with
no mention of the keyword. Worth knowing before debugging one.

`load()` also takes a set of `defines`, injected as `#define` lines
immediately after `#version` (which GLSL requires to stay first). Optional
features live behind `#ifdef` rather than a uniform branch so a disabled
overlay is **not compiled at all** — toggling one is a recompile, which is
the point. `Camera.set_define()` drives it; `SEGMENT_DEBUG` is the first user.

### Generated GLSL tables

Everything the shaders know about a *specific* machine is generated from the
Python class by [core/glsl_types.py](core/glsl_types.py) and spliced in by the
loader at two markers in [shaders/common.glsl](shaders/common.glsl):

| marker | emits |
|---|---|
| `// @generated type-ids` | `TYPE_*` constants from `cell.TYPE_NAMES`, plus the entity row layout (`ENTITY_STRIDE`, `MAX_CHANNELS`, the `F_*` gate-state offsets) |
| `// @generated type-tables` | `type_footprint`, `input_face_mask_local`, `output_face_mask_local`, `input_channel_at`, `output_channel_at`, `pass_through_mask`, `type_colour`, `type_glyph` |

**No shader names a machine by hand.** Adding a type in Python makes it
appear in all eight tables automatically.

This replaced six hand-mirrored GLSL tables that had to be kept in step with
Python by test assertions. The mechanism failed twice in practice — ports
landing on interior edges of a rotated structure, and a flipped machine's
control overlay pointing the wrong way — both of them "GLSL disagrees with
Python about a type". Generation makes that class of bug structurally
impossible rather than merely detectable, which matters more as simulation
adds per-type data (throughput, transforms, buffer sizes) that would each
need the same mirroring.

Three consequences worth knowing:

- **Order matters in `common.glsl`.** The ids marker must precede anything
  naming a `TYPE_*`; the tables marker must follow `FACE()` and the `GLYPH_*`
  constants, which the generated bodies reference. Hence two markers rather
  than one.
- **Nothing is written to disk.** The marker stays in the source, so there is
  no generated file to go stale or to commit, and `F5` picks up a change to
  `core/entity.py` with no build step.
- **The tests changed shape.** They no longer pin two copies against each
  other — they test the *generator* (does it emit what `face_masks_at()`
  says?) and the hand-written GLSL that *consumes* the tables, which is where
  the authored↔placed conversion still lives and is still worth pinning.

Type names in `cell.TYPE_NAMES` are upper-cased into the GLSL constants, so
they must be short and identifier-safe.

### Belts read their 5-cell neighbourhood

A belt draws as a thick spine through the cell plus chevrons showing travel
direction. Topology comes from the cell and its 4 orthogonal neighbours — no
lookup table, no precomputed tile index:

- The **outgoing** arm always runs centre → the facing edge.
- A neighbour is a *candidate* source iff it emits at us: a belt whose
  direction points back at us, **or a structure with an output face pointing
  at us**. Candidates are then passed to `take_dir()`, which picks **exactly
  one** — so at most one incoming arm is ever drawn. See "Merges are resolved,
  not supported": drawing an arm per candidate was the phantom-merge goof, and
  the renderer must agree with the simulation about which source wins.
- With no incoming neighbour the spine extends backwards too, so an isolated
  belt fills its cell rather than looking like a half tile.
- **The cell we output into is never a source**, even if it emits back at us.
  A machine can have an output face pointing at the very belt that feeds it;
  without this rule that belt would draw two opposed chevron streams on one
  arm. The belt's own facing wins — it is leaving, not arriving.

**Chevron phase comes from world position**, not from the cell: the lattice is
a function of distance along the travel axis, so the pattern is continuous
across cell boundaries and a run reads as one march. It necessarily resets at
a corner, where the axis changes — which looks natural, since the belt visibly
turns there. `CHEVRON_PERIOD` sets the spacing in cells; `1.0` gives one per
cell, `0.5` two.

There is no merge case left to draw: `take_dir()` admits one source, so a
cell has at most one incoming and one outgoing arm. A corner therefore stays
continuous — one stream arrives, one leaves. `MERGE_GAP` survives as a
tunable for the control-signal overlay, where channels can still legitimately
converge.

`same_layer()` decides what counts as a neighbour: currently ghosts chain only
with ghosts and real belts only with real belts, so a preview reads as its own
run instead of fusing with what is already built. That is a one-line predicate
if you want to change it.

### Structures

Each machine draws a glyph centred on its whole footprint — `Shear` a Plus,
`Mac` an X, `Swap` a Z, `Source`/`Sink` a dot, `Reciprocal` a divide sign,
`Max` stacked chevrons, `Floor` a down arrow, `Merge` a Y and `Route` the
same Y inverted — chosen by `body_glyph()` and drawn by `sd_glyph()`, with
the colour from `body_colour()`. The marks are abstract rather than
lettered; see "Adding a machine" above.
Both carry short port arrows — blue pointing inward at inputs, red pointing
outward at outputs — plus **connection stubs**: a short belt-coloured bar from
the body edge out to the cell boundary, drawn only on a port face that
actually has something on the other side, so a structure and its neighbours
read as physically joined. Stubs are drawn *under* the body so the body edge
caps their inner end.

#### A Source shows its emitted value instead of a glyph

The dot a `Source` used to draw said only "this is a generator", which the
colour and the port arrows already say. The emitted value is the one thing
that differs between two Sources, and it was reachable only by clicking.

**Nothing new is uploaded for this.** The entity SSBO is already bound and
the cell already carries the row id, so the renderer reads `F_EMIT` straight
out of the row. The offset is *generated* from `Source.F_EMIT` rather than
spelled as `F_IN_VALUE` in the shader: the two are equal only because a
generator has no input channels, and a hand-written alias would silently
read a channel if `F_EMIT` ever moved (see "A Source's emitted value is per
instance").

Two things the glyph did not need:

- **The label does not rotate with the facing.** Text has a reading
  orientation a mark does not, so a south-facing Source must still read
  left-to-right. `pf` is footprint-centred and already world-aligned, so the
  fix is simply not to rotate it.
- **Below the legibility cutoff the type glyph comes back.** A number too
  small to read is noise, but a blank body is worse than the dot — a Source
  would stop being identifiable at a glance. This differs from the item
  labels, which just fade, because a disc still has its ring to carry the
  sign and a machine body has nothing.

Sizing these labels is a real tension and both ends were hit while tuning
it: too small and a glyph lands inside the fade band at ordinary build zoom,
so a `137` renders as fragments and reads as `37` — which looks exactly like
a formatter bug and is not one. Too large and a saturated belt becomes
wall-to-wall text with the discs and their rings lost underneath.

### Control-signal overlay

A machine draws a faint belt straight through itself on each axis it passes
**unchanged** — the control channels, which in the standard layout run
vertically while the mutated channel crosses horizontally. It makes "which
signal survives this operation" readable at a glance. Drawn over the body and
under the glyph; `PASS_ALPHA` in [world.frag](shaders/world.frag) sets the
opacity, with `PASS_WIDTH` and `PASS_CHEVRON` alongside it.

**The axes are declared, not derived** (`Entity.PASS_THROUGH`, mirrored by
`pass_through_mask()` in GLSL and pinned by `tests/test_glsl.py`). This is the one
piece of port information geometry cannot recover: a mutated channel enters
one face and leaves the opposite one *exactly* as a pass-through does —
`Shear`'s y enters `-X` and leaves `+X` just as its x enters `+Y` and leaves
`-Y`. Only the machine's semantics distinguish them. An early version
inferred the axes from the masks and drew the mutated channel as a control
channel on every machine.

Only cells that actually carry the channel draw it, so `Shear` marks its
first cell and not its second. A cell qualifies if it has a port on that
axis, or if it is an interior link between two that do — which is what
carries `Swap`'s x channel across both cells of its 1×2 body, where the input
is on one cell and the output on the other.

### Stubs

A face stubs when the neighbour is a genuine counterpart: an **input** face
needs something emitting at it (`feeds_into`), an **output** face needs
something that would take it — a belt, or another structure whose own input
face points back (`accepts_from`). Both sides of a structure-to-structure join
must agree, so both predicates exist; checking only for belts on the output
side left one half of such a join silent.

Ports are derived in GLSL by `input_face_mask()` / `output_face_mask()` in
[shaders/common.glsl](shaders/common.glsl), mirroring `Entity.INPUTS` /
`OUTPUTS`. They are **per-cell** — a port belongs to one cell of a footprint,
and the mask functions take that cell's local offset — so the shader answers
"is this face a port?" from one cell with no footprint-wide reasoning. When
probing a *neighbour*, pass that neighbour's own `cell_local(n.y)`, not yours.
`tests/test_glsl.py` pins these masks against `Entity.face_masks_at()` for every
type, cell and facing.

Multi-cell entities recover their extent by probing neighbours for a matching
entity id, then draw in *footprint space* so the glyph is centred on the entity
rather than on one of its cells. Two things matter here and both caused visible
bugs:

- Merged body edges extend slightly **past** the cell boundary. Stopping
  exactly at it leaves both cells antialiasing the shared edge, which shows as
  a seam.
- The glyph's antialiasing width must **not** be scaled by the footprint size.
  Scaling it made the Plus get antialiased twice at the interior boundary —
  the same seam, in the glyph rather than the body.

## Conventions

- Shader paths resolve relative to the package, not the cwd, so the app and
  hot-reload work from any directory.
- `mouse` picking scales window coords to framebuffer coords before converting,
  which matters on HiDPI displays; `Camera.screen_to_world` mirrors the shader's
  mapping exactly, and `tests/test_scenarios.py` asserts the two agree.
- World `+Y` is up. Cell `(x, y)` covers world `[x, x+1) × [y, y+1)`.
- `InputManager` is the **only** place that reads input or consults
  `io.want_capture_keyboard` / `io.want_capture_mouse`. It reports *that* `R`
  was pressed, never what `R` means.
- The toolbelt always has `SLOT_COUNT` (20) slots — `ROW_COUNT` rows of
  `ROW_SIZE` — and `selected` is a flat index into all of them. Empty slots
  are selectable and simply have no active tool. Switching tools cancels any
  in-flight gesture and its preview — a half-drawn belt run means nothing to
  the structure tool. The row swap (`X`) is the exception: it re-aims the keys and
  deliberately disturbs neither the hand nor the gesture.
- Scroll is event-based, so `InputManager` installs a glfw scroll callback and
  **chains to imgui's**; dropping the previous callback would break scrolling
  in the UI panels.
- Scroll zoom is **cursor-anchored** (`Camera.zoom_at`): sample the world
  point under the cursor, change the zoom, then shift the centre by however
  much that point moved. The drive test asserts the anchor holds to within
  0.05 cells.
- Mouse *presses* are suppressed over imgui windows but *releases* are always
  honoured, so a gesture cannot get stuck on.
- `dt` is clamped to 0.1s in the main loop so a hitch does not fling the camera.
- `imgui.ini` (saved window layout) and `gates_config.json` (panel
  preferences) are generated at runtime and gitignored.

## Deferred

Simulation now works end to end — belts carry items, gates fire, and a
saturated chain moves one item per tick. What is still missing:

**Cooldown ticks** (every gate fires every tick it can; a per-type rate limit
is the obvious next knob, and `FIRE_RULE` is where it would go);
**backwards mode** (gates may now destroy information — a dangling output is
a sink — so running a machine in reverse will need each discard point to
remember a little of what it dropped. `GateSim.structure_phase()`'s discard
branch is the single place that would record it);
**a numeric policy** (`VALUE_LIMIT` clamps `1/0` and `floor(±inf)` to 2²⁴ as
a placeholder; saturating vs. wrapping vs. a distinct error item is
undecided, and NaN currently propagates while infinities do not);
**a Sink's record** (it destroys what it eats; a stack per input channel is
the stated next step — though Sink is now off the toolbelt, since a dangling
output does the same job);
**item-to-gate transit** (items pop in and out of existence at the ends of a
run rather than being seen to enter or leave a machine — deliberate for now);
**int and bool item types** (the storage is float32 throughout, as designed;
nothing yet reinterprets it — though the formatter would need a policy per
type, since `trim` and `sig` mean nothing for a bool);
**lowercase and a general text layer in the *shader*** (the bitfield font is
uppercase-only, and its only callers are the item labels and a Source's
value — a machine wanting a *name* on its body would want a string-drawing
entry point rather than the number-shaped one `sd_number()` provides. The
annotation layer now covers arbitrary text with a real font, so the open
question is narrower than it was: whether the *structure* labels should move
to imgui too. The investigation says they are a good candidate — tens of
them, already world-aligned rather than rotated with the facing — while the
**item** labels should stay in the shader, since a saturated board at max
zoom would put thousands on screen and `text_opacity()` already fades them
out instead);
**incremental segment rebuild** (currently whole-board on any real edit;
the inspector reports rebuild counts so there is a number to point at before
anyone optimises it);
**compute shader passes** (the context is 4.3 and all four SSBOs are bound
and read, but nothing dispatches);
curved corner geometry (corners are currently a right-angle join of two
straight arms, which reads fine but is not a true curve); entity-row
compaction; a growable or chunked board (fixed 512×512).

**Simulation is being built CPU-first, then ported.** The tick phases, the
belt segment representation and the gate logic are all designed so the port to
compute shaders replaces phase-callback *bodies* with dispatches and leaves
the sequencing alone — see "The simulation clock and the two-phase tick". The
CPU implementation is kept afterwards as the test oracle, per
[docs/belt_report.md](docs/belt_report.md).

Copy/paste landed as `PasteBuilder` plus [core/clipboard.py](core/clipboard.py)
and needed no change to the builder abstraction, which is the evidence that
shape was right. `StampBuilder` is the second piece of evidence: baking a
stamp into a toolbelt slot was a subclass of `PasteBuilder` and nothing else.

Blueprints are now **half** persisted: a stamp baked into a slot survives a
restart, so the thing worth keeping can be kept. What is still missing is
*naming* them and a library independent of the twenty slots — the in-session
history is still in-session. **Undo** remains shaped for but not built (it
would record the rows a committed `BuildPlan` produced); note the
matched-stamp substitution is the first gesture that deletes and places in
one click, which makes it the first that would want a single undo entry
rather than two.

The empty hand's gesture is now **inspect** rather than the
structure-selection tool originally planned for it — that slot is taken, so a
selection tool would need a different home.

**The machine abstraction held up under a vocabulary change.** Five machines
arrived at once and the builders, toolbelt, placement, stubs, port arrows and
GLSL tables all needed no change — the evidence that generating per-type data
from the classes was the right call. What *did* need changing was the
**firing protocol**, in three places, and the pattern is worth noting: each
was a case of the engine holding a decision the machine should have owned.
Readiness was an enum the type could not override; consumption was implicit
in the tick; "which outputs may be skipped" was a generator-only special
case. Each became a method on `Entity` with a sensible default.

If a future machine does not fit, that is the shape to look for first — a
question the tick currently answers on every machine's behalf. The remaining
candidates are rate (every gate fires as fast as it can) and whether a gate
may hold state between fires (only `Merge` does, and only by leaving an input
loaded).

The toolbelt was full at ten slots, which is what prompted hiding machines
and what the second row and the type menu answered. Twenty slots hold the
thirteen current tools with room to spare, and a machine no longer has to
earn a slot to exist — Swap and Sink came back on the strength of that alone.
What is *not* solved is a bar with more tools than slots: a third row would
need a key, and twenty is already more than the number row addresses.

Per-machine configuration now has two worked examples and still no general
mechanism, and the pair shows why one is wanted. `Source.F_EMIT` reuses an
input slot that happens to be dead on a generator — exact, but not a pattern.
`Belt.F_SEED_ON`/`F_SEED_VALUE` is the case with no such slot available, and
it did what the note predicted: grew `ENTITY_STRIDE` (24 → 28) and widened
every row in the game to carry two values one type uses. A third setting
should probably arrive with a real mechanism rather than a third ad-hoc
answer — a side table keyed by row, say, which costs nothing for the types
that have no settings.

The tests for both also want folding together: `_set_emit` and `_set_seed`
are the same shape (guard the row is alive and the right type, ignore a
no-op, apply, invalidate) and only differ in what they invalidate.
