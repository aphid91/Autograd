"""Drive the orchestrator headlessly: simulate a full build/cancel/erase flow.

Patches glfw input getters so the state machine can be stepped deterministically
without a real mouse, then asserts board state after each gesture.
"""
import numpy as np
import glfw, moderngl
from imgui_bundle import imgui

import cell
import input_manager
from orchestrator import Orchestrator
from input_manager import STATE_IDLE, STATE_PLANNING, STATE_ERASING
from engine import MODE_HORIZONTAL_FIRST, MODE_VERTICAL_FIRST, MODE_LINE

W, H = 1024, 768

glfw.init()
for k, v in [(glfw.CONTEXT_VERSION_MAJOR, 4), (glfw.CONTEXT_VERSION_MINOR, 3),
             (glfw.OPENGL_PROFILE, glfw.OPENGL_CORE_PROFILE),
             (glfw.VISIBLE, glfw.FALSE)]:
    glfw.window_hint(k, v)
win = glfw.create_window(W, H, 'headless', None, None)
glfw.make_context_current(win)
ctx = moderngl.create_context()
ctx.enable(moderngl.BLEND)

imgui.create_context()
io = imgui.get_io()
io.display_size = (W, H)

# --- fake input state -------------------------------------------------
fake = {"cursor": (0.0, 0.0), "left": False, "right": False, "keys": set()}

_real_get_cursor = glfw.get_cursor_pos
glfw.get_cursor_pos = lambda w: fake["cursor"]
glfw.get_mouse_button = lambda w, b: (
    glfw.PRESS if (fake["left"] if b == glfw.MOUSE_BUTTON_LEFT else fake["right"])
    else glfw.RELEASE)
glfw.get_key = lambda w, k: glfw.PRESS if k in fake["keys"] else glfw.RELEASE
glfw.get_framebuffer_size = lambda w: (W, H)
glfw.get_window_size = lambda w: (W, H)

orch = Orchestrator(win, ctx, 64, 64)
# No imgui backend here, so skip the panel; we are testing the state machine
# and board, not the widgets.
orch.draw_ui = False
orch.seed_demo()
engine = orch.engine
board = engine.board
cam = orch.camera
cam.center[:] = (32.0, 32.0)
cam.zoom = 16.0

def put_cursor_at_cell(cx, cy):
    """Place the fake cursor at the centre of a cell."""
    sx, sy = cam.world_to_screen(cx + 0.5, cy + 0.5, (W, H))
    fake["cursor"] = (float(sx), float(sy))

def step(n=1):
    for _ in range(n):
        orch.frame(1.0 / 60.0)

def cells_of_type(t):
    return int((cell.cell_type_arr(board.grid[:, :, 0]) == t).sum())

ok = lambda m: print("  OK:", m)

print("=== initial ===")
step(2)
print("  live entities:", board.live_count, " belt cells:", cells_of_type(cell.TYPE_BELT))
assert board.live_count > 0, "seed_demo should have placed belts"
ok("seeded board renders")

print("=== plan a belt run, cycle modes, commit ===")
engine.clear_board()
step()
assert board.live_count == 0
put_cursor_at_cell(10, 10); step()

fake["left"] = True; step()           # press -> PLANNING
fake["left"] = False
assert orch.input.state == STATE_PLANNING, orch.input.state
assert orch.input.anchor == (10, 10), orch.input.anchor
ok(f"planning from {orch.input.anchor}")

put_cursor_at_cell(16, 14); step()    # move -> ghost preview
nghost = len(engine.ghost_rows)
assert nghost > 0, "no ghosts created"
ghost_cells = int((board.grid[:, :, cell.CH_Z] & cell.GHOST_BIT).sum())
assert ghost_cells == nghost, (ghost_cells, nghost)
ok(f"{nghost} ghost cells, all flagged in .z")
assert cells_of_type(cell.TYPE_BELT) == nghost, "ghosts should occupy belt cells"

mode_before = orch.input.path_mode
fake["keys"].add(glfw.KEY_R); step(); fake["keys"].discard(glfw.KEY_R); step()
assert orch.input.path_mode != mode_before, "R did not cycle mode"
ok(f"R cycled mode {mode_before} -> {orch.input.path_mode}")
# Ghost count for an elbow is the same either way, but the cells differ.
g_after = len(engine.ghost_rows)
assert g_after == nghost, (nghost, g_after)
ok("re-previewed after mode cycle")

fake["left"] = True; step()           # second click -> commit
fake["left"] = False; step()
assert orch.input.state == STATE_IDLE
assert not engine.ghost_rows, "ghosts should be consumed"
remaining_ghost_cells = int((board.grid[:, :, cell.CH_Z] & cell.GHOST_BIT).sum())
assert remaining_ghost_cells == 0, remaining_ghost_cells
assert board.live_count == nghost, (board.live_count, nghost)
ok(f"committed {board.live_count} real belts, no ghost flags left")

print("=== cancel with right click ===")
# Baseline BEFORE planning: ghosts are real rows and do count toward
# live_count, so the invariant is "cancel returns to the pre-plan count".
live_before = board.live_count
put_cursor_at_cell(40, 40); step()
fake["left"] = True; step(); fake["left"] = False
assert orch.input.state == STATE_PLANNING
put_cursor_at_cell(46, 44); step()
assert len(engine.ghost_rows) > 0
assert board.live_count > live_before, "ghosts should be live rows"
fake["right"] = True; step()
assert orch.input.state == STATE_IDLE, orch.input.state
assert not engine.ghost_rows, "cancel must drop ghosts"
assert board.live_count == live_before, (
    f"cancel must restore the pre-plan count: {board.live_count} != {live_before}")
assert int((board.grid[:, :, cell.CH_Z] & cell.GHOST_BIT).sum()) == 0
ok("right-click cancelled, nothing built")

# The cancelling right-press must NOT have started an erase.
assert orch.input.state == STATE_IDLE
fake["right"] = False; step()
ok("cancel did not fall through to erase")

print("=== crossing an existing belt skips locked cells ===")
engine.clear_board(); step()
engine.place_belt_path((5, 20), (25, 20), MODE_HORIZONTAL_FIRST)
step()
before = board.live_count
put_cursor_at_cell(15, 14); step()
fake["left"] = True; step(); fake["left"] = False
put_cursor_at_cell(15, 26); step()
gc = [(x, y) for x, y in
      zip(*np.nonzero((board.grid[:, :, cell.CH_Z] & cell.GHOST_BIT))[::-1])]
assert (15, 20) not in gc, "ghosted on top of an existing belt!"
ok(f"{len(gc)} ghost cells, crossing cell (15,20) skipped")
fake["left"] = True; step(); fake["left"] = False; step()
assert not board.is_empty(15, 20)
crossing_id = cell.cell_id(board.cell_word(15, 20))
assert crossing_id != 0
ok("committed around the obstacle; original belt intact")

print("=== blocked cells must not change belt orientation ===")
# Directions come from the unfiltered path, so what the player drew survives
# interference. Regression for: a run whose neighbour is blocked used to fall
# back to +X.
engine.clear_board(); step()
# Three sides of a 2x2 loop; completing it from the empty cell must face +Y.
engine.place_belt(0, 1, cell.DIR_PX)
engine.place_belt(1, 1, cell.DIR_NY)
engine.place_belt(1, 0, cell.DIR_NX)
path, dirs = engine.plan_oriented((0, 0), (0, 1), MODE_HORIZONTAL_FIRST)
assert path == [(0, 0)] and dirs == [cell.DIR_PY], (path, dirs)
ok("2x2 loop completes facing +Y, not +X")

for (s, t, blk, want) in [((2, 2), (2, 6), (2, 3), cell.DIR_PY),
                          ((6, 8), (2, 8), (5, 8), cell.DIR_NX)]:
    engine.clear_board()
    engine.place_belt(*blk)
    p, d = engine.plan_oriented(s, t, MODE_HORIZONTAL_FIRST)
    assert d and all(x == want for x in d), (s, t, [cell.DIR_NAMES[x] for x in d])
ok("drags through a blocker keep their heading")

# And the same holds for what actually gets committed through the UI path.
engine.clear_board(); step()
engine.place_belt(2, 3, cell.DIR_PX)
put_cursor_at_cell(2, 2); step()
fake["left"] = True; step(); fake["left"] = False
put_cursor_at_cell(2, 6); step()
fake["left"] = True; step(); fake["left"] = False; step()
built = [(x, y) for y in range(2, 7) for x in [2] if not board.is_empty(x, y)]
dirs_built = {cell.cell_dir(board.cell_word(x, y)) for x, y in built if (x, y) != (2, 3)}
assert dirs_built == {cell.DIR_PY}, dirs_built
ok("committed run through a blocker is all +Y")

print("=== right-drag erase ===")
# Own setup rather than inheriting the previous section's board.
engine.clear_board(); step()
engine.place_belt_path((5, 20), (25, 20), MODE_HORIZONTAL_FIRST); step()
live_before = board.live_count
put_cursor_at_cell(5, 20); step()
fake["right"] = True; step()
assert orch.input.state == STATE_ERASING, orch.input.state
for x in range(6, 16):
    put_cursor_at_cell(x, 20); step()
fake["right"] = False; step()
assert orch.input.state == STATE_IDLE
erased = live_before - board.live_count
print("  erased", erased, "entities")
assert erased >= 10, erased
assert board.is_empty(5, 20) and board.is_empty(12, 20)
ok("right-drag erased a contiguous run")

print("=== fast drag leaves no gaps ===")
engine.clear_board(); step()
engine.place_belt_path((0, 30), (40, 30), MODE_HORIZONTAL_FIRST); step()
put_cursor_at_cell(2, 30); step()
fake["right"] = True; step()
put_cursor_at_cell(30, 30); step()     # one huge jump
fake["right"] = False; step()
gaps = [x for x in range(2, 31) if not board.is_empty(x, 30)]
assert not gaps, f"fast drag left belts behind at {gaps}"
ok("interpolated erase across a 28-cell jump")

print("=== device round trip after all that ===")
assert board.verify_round_trip(), "grid round trip mismatch"
ok("to_device/from_device match")
raw = np.frombuffer(board.entity_buffer.read(), dtype=np.float32).reshape(board.capacity, -1)
assert np.array_equal(raw, board.entities), "SSBO diverged"
ok("entity SSBO matches CPU array")

print("=== partial upload really is partial ===")
board.to_device(force=True)
put_cursor_at_cell(50, 50); step()
fake["left"] = True; step(); fake["left"] = False
put_cursor_at_cell(52, 50); step()
cells_up, rows_up = orch.ui.last_upload
print(f"  3-cell ghost preview uploaded {cells_up} cells (board is {board.width*board.height})")
assert 0 < cells_up < 500, cells_up
ok("dirty-rect upload stayed small")
fake["right"] = True; step(); fake["right"] = False; step()

print("=== camera pan/zoom ===")
c0 = cam.center.copy(); z0 = cam.zoom
fake["keys"].add(glfw.KEY_D); step(10); fake["keys"].discard(glfw.KEY_D)
assert cam.center[0] > c0[0], (c0, cam.center)
fake["keys"].add(glfw.KEY_W); step(10); fake["keys"].discard(glfw.KEY_W)
assert cam.center[1] > c0[1]
ok(f"WASD panned to {cam.center.round(1)}")
fake["keys"].add(glfw.KEY_E); step(10); fake["keys"].discard(glfw.KEY_E)
assert cam.zoom > z0, (z0, cam.zoom)
fake["keys"].add(glfw.KEY_Q); step(30); fake["keys"].discard(glfw.KEY_Q)
assert cam.zoom < z0, (z0, cam.zoom)
ok(f"Q/E zoomed to {cam.zoom:.1f}")

print("=== imgui capture gating ===")
io.want_capture_mouse = True
engine.clear_board(); step()
put_cursor_at_cell(8, 8)
fake["left"] = True; step(); fake["left"] = False
assert orch.input.state == STATE_IDLE, "click over UI should not start a plan"
assert board.live_count == 0
io.want_capture_mouse = False
ok("clicks over imgui ignored")

print()
print("ALL ORCHESTRATOR DRIVE TESTS PASSED")
glfw.terminate()
