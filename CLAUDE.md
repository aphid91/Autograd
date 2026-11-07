# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**Gates** — a grid-based factory game in the spirit of Factorio. A GLFW/moderngl
app renders a cell grid and its entities entirely from a fragment shader that
reads game state out of a GPU texture, with an imgui control panel on top.

Currently implemented: the gameboard, belts, belt-drawing UX (elbow/line
planning with a ghost preview), erase, and camera pan/zoom. Belt *simulation*
(items moving) is deliberately not implemented — see "Deferred" below.

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
| [tests/test_item_render.py](tests/test_item_render.py) | reads the framebuffer back: do discs appear where the simulation says? |

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
| `Q` / `E` | zoom out / in |
| Scroll | zoom, anchored at the cursor |
| Left-drag | pan the camera |
| `F` | **per-tool**: flip (mirror) the held structure |
| `shift+F` | frame the whole board |
| `1`-`9`, `0` | select a toolbelt slot |
| Left-click | build with the active tool (fires on **release**) |
| `R` / `shift+R` | **per-tool**: belt path shape, or structure rotation |
| Right-click | cancel a plan, or drag to erase |
| `Esc` | cancel a plan, or a tool's multi-step state |
| `F5` | reload the world shaders |
| `Delete` | clear the board |
| `Space` | pause / resume the simulation |
| `.` | run exactly one tick (works while paused) |

`R` and `F` deliberately mean different things in different tools — the input
manager reports only that they were pressed and the active builder decides.
Adding a tool with its own notion of "cycle" or "flip" needs no input-layer
change. `F` took the bare key from frame-the-board (now `shift+F`) because
flipping is the far more frequent action.

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
| [core/builders.py](core/builders.py) | `EntityBuilder`/`BuildPlan`; `BeltBuilder`, `StructureBuilder`, `UndergroundBuilder` |
| [core/glsl_types.py](core/glsl_types.py) | emits the per-type GLSL tables from the entity classes |
| [core/persistence.py](core/persistence.py) | save/load the board to a single `.npz` (`.w` zeroed — derived) |
| [core/toolbelt.py](core/toolbelt.py) | ten slots, one active tool |
| [core/tick.py](core/tick.py) | the simulation clock: tick counter, fixed timestep, phase order |
| [core/segments.py](core/segments.py) | belt runs coalesced into the O(1) simulation unit; derived from the grid |
| [core/belts.py](core/belts.py) | closed-form item transport, and the naive oracle it is tested against |
| [core/belt_device.py](core/belt_device.py) | packs segment + item state into SSBOs for the renderer |
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
ghost state (`0` real / `1` ghost / `2` frustrated — see "Frustrated cells"
below) and bit 2 the flip flag.

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

### Item rendering

Items draw as discs with a coloured ring — **green above zero, red at or
below** — sized by `ITEM_RADIUS` in [world.frag](shaders/world.frag)'s
tunables. No text: glyph rendering is a project of its own, and sign is what
is worth reading at a glance.

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

The machine set lives in `core/entity.py` and is listed in `STRUCTURES`, which
is what the toolbelt builds its tools from. All are authored at `DIR_PX` and
rotated; all are continuous rectangles.

| type | footprint | signature |
|---|---|---|
| `Swap` | 1×2 | x,y,z → x, (y,z if x>0 else z,y) |
| `Shear` | 2×1 | x,y → x, x+y |
| `Mac` | 2×1 | x,y,z → x, y, z + x·y |
| `Source` | 4×1 | one output on the +X tip |
| `Sink` | 3×3 | one input at the middle of each side |

The three arithmetic machines share an idiom: a **pass-through channel**
crosses the body on one axis unchanged, while the channel taking the *long
way* across the footprint is the one that gets mutated. `Swap`'s x runs
vertically through the whole 1×2 body (in at the top cell's north face, out at
the bottom cell's south) while y and z cross their own cell horizontally.
`Shear` passes x down through cell 0 only; `Mac` is `Shear` plus a second
vertical control channel down cell 1.

**Adding a machine is one Python class.** An `Entity` subclass declaring
`FOOTPRINT`, `INPUTS`, `OUTPUTS`, `PASS_THROUGH`, `COLOUR` and `GLYPH`, plus
an entry in `STRUCTURES` and `cell.TYPE_NAMES`. **No GLSL changes at all** —
see "Generated GLSL tables" below. The builders, the toolbelt, the stub
logic and the port arrows are all generic.

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
  port layout and compares. `Swap`, `Shear` and `Mac` are chiral; `Source` and
  `Sink` are symmetric about the authored X axis, so `F` is a deliberate no-op
  for them rather than a special case in the toolbelt.
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
| `// @generated type-ids` | `TYPE_*` constants, from `cell.TYPE_NAMES` |
| `// @generated type-tables` | `type_footprint`, `input_face_mask_local`, `output_face_mask_local`, `pass_through_mask`, `type_colour`, `type_glyph` |

**No shader names a machine by hand.** Adding a type in Python makes it
appear in all six tables automatically.

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
`Mac` an X, `Swap` a Z, `Source`/`Sink` a dot — chosen by `body_glyph()` and
drawn by `sd_glyph()`, with the colour from `body_colour()`.
Both carry short port arrows — blue pointing inward at inputs, red pointing
outward at outputs — plus **connection stubs**: a short belt-coloured bar from
the body edge out to the cell boundary, drawn only on a port face that
actually has something on the other side, so a structure and its neighbours
read as physically joined. Stubs are drawn *under* the body so the body edge
caps their inner end.

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
- The toolbelt always has `SLOT_COUNT` (10) slots; empty ones are selectable
  and simply have no active tool. Switching tools cancels any in-flight gesture
  and its preview — a half-drawn belt run means nothing to the structure tool.
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
- `imgui.ini` (saved window layout) is generated at runtime and gitignored.

## Deferred

Noted so they are not mistaken for oversights: **structure** simulation
(belts now carry items — the closed form runs on the belt phase and is
oracle-verified — but no gate fires: none of the signatures in the machine
table are evaluated, no arithmetic happens, and `Source`/`Sink` neither
produce nor consume. `Engine.debug_feed` stands in for them);
**item-to-gate transit** (items pop in and out of existence at the ends of a
run rather than being seen to enter or leave a machine — deliberate for now);
**numbers on items** (a disc shows only the sign of its value, via colour);
**incremental segment rebuild** (currently whole-board on any real edit;
the inspector reports rebuild counts so there is a number to point at before
anyone optimises it);
compute shader passes (the context is 4.3 and the SSBO is bound, but nothing
dispatches); curved corner geometry (corners are currently a right-angle join
of two straight arms, which reads fine but is not a true curve); entity-row
compaction; a growable or chunked board (fixed 512×512).

**Simulation is being built CPU-first, then ported.** The tick phases, the
belt segment representation and the gate logic are all designed so the port to
compute shaders replaces phase-callback *bodies* with dispatches and leaves
the sequencing alone — see "The simulation clock and the two-phase tick". The
CPU implementation is kept afterwards as the test oracle, per
[docs/belt_report.md](docs/belt_report.md).

Tools the builder abstraction is shaped for but which do not exist yet:
blueprint capture/paste, and undo (which would record the rows a committed
`BuildPlan` produced).
