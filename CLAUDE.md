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

# Regression harness (headless; drives the orchestrator through a full
# build / cancel / erase / pan flow and asserts board + device state)
python test_drive.py
```

`test_drive.py` is the only test; there is no pytest/linter configured. It needs
a working GL 4.3 context but opens no visible window. `cell.py` and `entity.py`
also run self-checks at import.

## Controls

| Input | Action |
|---|---|
| `WASD` | pan |
| `Q` / `E` | zoom out / in |
| `F` | frame the whole board |
| Left-click | start a belt run; click again to confirm |
| `R` | while planning, cycle elbow-H → elbow-V → line |
| Right-click | cancel a plan, or drag to erase |
| `Esc` | cancel a plan |
| `F5` | reload `world.frag` |
| `Delete` | clear the board |

## Architecture

Ownership is a single chain: `app.py` → `Orchestrator` → {`Engine`, `Camera`,
`InputManager`, `UI`}. `Engine` owns the `Gameboard`, and **nothing outside
`Engine` mutates board state** — the input manager produces intents, the UI
returns requests, and the orchestrator applies both.

| file | role |
|---|---|
| [cell.py](cell.py) | grid cell bit packing; constants for types and directions |
| [entity.py](entity.py) | `Entity` view base, `Belt`, `Structure` stub, type registry |
| [gameboard.py](gameboard.py) | grid array, entity table, free list, dirty tracking, device transfer |
| [engine.py](engine.py) | path planning, place/erase, ghost lifecycle |
| [camera.py](camera.py) | pan/zoom, world↔screen, the world render pass |
| [input_manager.py](input_manager.py) | input → intents; the belt-planning state machine |
| [ui.py](ui.py) | imgui panel |
| [orchestrator.py](orchestrator.py) | wires the above; owns per-frame ordering |
| [app.py](app.py) | GLFW/context setup and the main loop |
| [world.frag](world.frag) | renders grid lines, belts, and ghosts from the grid texture |

### Cell encoding

The grid is `(H, W, 4) int32`, uploaded as an `RGBA32I` texture. Channel `.x`:

```
bits 0-1    direction   0=+X 1=+Y 2=-X 3=-Y
bits 2-8    type        0=empty, 1=belt, 2.. structures
bits 9-30   entity id   row index into the entity SSBO
bit  31     unused — deliberately clear
```

`.y` packs `(local_x, local_y)` so a multi-cell entity's cells each know their
offset from the origin. `.z` bit 0 is `is_ghost`. `.w` is reserved.

Three things to know before touching this:

- **The id field is 22 bits, not the 23 the layout has room for.** Bit 31 is the
  int32 sign bit; a negative cell word would break Python-side comparisons and
  need sign-aware GLSL unpacking. Widening the id would reintroduce that.
- **Emptiness is `type == 0`, not `word == 0`.** `pack_cell()` collapses
  `TYPE_EMPTY` to exactly `0` so both tests agree in practice, but code should
  ask `cell.is_empty()`.
- **The packing is duplicated in GLSL** at the top of [world.frag](world.frag).
  There is no way to share it; change one and you must change the other.
  `cell.py` runs round-trip assertions at import to catch drift on the Python
  side.

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
apply its requests → `to_device()` → render world → (caller renders imgui). The
device sync sits after all mutation and before rendering, so what is drawn is
always what the CPU believes. Input is polled before the UI is built so the
panel shows current state rather than lagging a frame.

### Belt planning

The state machine lives in `InputManager` (IDLE / PLANNING / ERASING). Three path
modes cycle with `R`: horizontal-first elbow, vertical-first elbow, and a
Bresenham line. All paths are **4-connected** — belts cannot step diagonally, so
`bresenham()` deliberately produces a staircase rather than diagonal steps.
For an axis-aligned drag all three modes produce the same cells, so `R` appears
to do nothing; this is correct, and the UI names the active mode to make it
legible.

Cells that are occupied or out of bounds are **skipped, not blocking**: a run
drawn across an existing belt builds on both sides and leaves the crossing cell
alone. A fully blocked path builds nothing.

**Directions are derived from the unfiltered path, then filtered alongside it**
(`Engine.plan_oriented`). This ordering matters: a belt's facing must not depend
on what happens to be in the way. Deriving directions after filtering means a
cell whose successor was blocked has no step to read and falls back to `+X`,
which is how "complete the last cell of a 2×2 loop" used to produce an
east-facing belt instead of one that closes the loop. For a genuine single-cell
path (a click with no drag) there is no step at all, so `plan_oriented` passes
the drag's dominant axis as the fallback.

Right-press is consumed by the cancel when PLANNING, so cancelling never also
starts an erase. Erase drags interpolate between the previous and current cell,
so a fast drag leaves no gaps.

## Conventions

- `world.frag`'s path is resolved relative to `camera.py`, not the cwd, so the
  app and hot-reload work from any directory.
- `mouse` picking scales window coords to framebuffer coords before converting,
  which matters on HiDPI displays; `Camera.screen_to_world` mirrors the shader's
  mapping exactly, and `test_drive.py` asserts the two agree by rendering.
- World `+Y` is up. Cell `(x, y)` covers world `[x, x+1) × [y, y+1)`.
- `InputManager` is the **only** place that reads input or consults
  `io.want_capture_keyboard` / `io.want_capture_mouse`.
- Mouse *presses* are suppressed over imgui windows but *releases* are always
  honoured, so a gesture cannot get stuck on.
- `dt` is clamped to 0.1s in the main loop so a hitch does not fling the camera.
- `imgui.ini` (saved window layout) is generated at runtime and gitignored.

## Deferred

Noted so they are not mistaken for oversights: belt item simulation; compute
shader passes (the context is 4.3 and the SSBO is bound, but nothing dispatches);
topology-aware corner rendering (belts draw as a square + direction arrow, and
corner curves would want the shader to read neighbour cells); multi-cell
`Structure` placement UX (the *encoding* supports it already); entity-row
compaction; a growable or chunked board (fixed 512×512); save/load.
