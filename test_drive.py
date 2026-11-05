"""Drive the orchestrator headlessly: simulate a full build/cancel/erase flow.

Patches glfw input getters so the state machine can be stepped deterministically
without a real mouse, then asserts board state after each gesture.
"""
import numpy as np
import glfw, moderngl
from imgui_bundle import imgui

from core import cell
from ui import input_manager
from orchestrator import Orchestrator
from ui.input_manager import STATE_IDLE, STATE_PLANNING, STATE_ERASING
from engine import MODE_HORIZONTAL_FIRST, MODE_VERTICAL_FIRST, MODE_LINE
from core.builders import BeltBuilder, StructureBuilder
from core.entity import Add, Mul

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

def click():
    """A full press+release without moving: the build gesture.

    Building happens on release now, because a press alone is ambiguous --
    it might become a camera drag.
    """
    fake["left"] = True; step()
    fake["left"] = False; step()

def drag_px(dx, dy, steps=4):
    """Hold left and move the cursor by a pixel delta, to pan the camera."""
    sx, sy = fake["cursor"]
    fake["left"] = True; step()
    for i in range(1, steps + 1):
        fake["cursor"] = (sx + dx * i / steps, sy + dy * i / steps)
        step()
    fake["left"] = False; step()

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

click()                               # press+release -> PLANNING
assert orch.input.state == STATE_PLANNING, orch.input.state
assert orch.input.anchor == (10, 10), orch.input.anchor
ok(f"planning from {orch.input.anchor}")

put_cursor_at_cell(16, 14); step()    # move -> ghost preview
nghost = len(engine.ghost_rows)
assert nghost > 0, "no ghosts created"
ghost_cells = int((cell.ghost_state_arr(board.grid[:, :, cell.CH_Z]) == cell.GHOST_PENDING).sum())
assert ghost_cells == nghost, (ghost_cells, nghost)
ok(f"{nghost} ghost cells, all flagged in .z")
assert cells_of_type(cell.TYPE_BELT) == nghost, "ghosts should occupy belt cells"

tool = orch.toolbelt.active
mode_before = tool.mode
fake["keys"].add(glfw.KEY_R); step(); fake["keys"].discard(glfw.KEY_R); step()
assert tool.mode != mode_before, "R did not cycle the belt tool's mode"
ok(f"R cycled belt mode {mode_before} -> {tool.mode}")
# Ghost count for an elbow is the same either way, but the cells differ.
g_after = len(engine.ghost_rows)
assert g_after == nghost, (nghost, g_after)
ok("re-previewed after mode cycle")

click()                               # second click -> commit
assert orch.input.state == STATE_IDLE
assert not engine.ghost_rows, "ghosts should be consumed"
remaining_ghost_cells = int((cell.ghost_state_arr(board.grid[:, :, cell.CH_Z]) == cell.GHOST_PENDING).sum())
assert remaining_ghost_cells == 0, remaining_ghost_cells
assert board.live_count == nghost, (board.live_count, nghost)
ok(f"committed {board.live_count} real belts, no ghost flags left")

print("=== cancel with right click ===")
# Baseline BEFORE planning: ghosts are real rows and do count toward
# live_count, so the invariant is "cancel returns to the pre-plan count".
live_before = board.live_count
put_cursor_at_cell(40, 40); step()
click()
assert orch.input.state == STATE_PLANNING
put_cursor_at_cell(46, 44); step()
assert len(engine.ghost_rows) > 0
assert board.live_count > live_before, "ghosts should be live rows"
fake["right"] = True; step()
assert orch.input.state == STATE_IDLE, orch.input.state
assert not engine.ghost_rows, "cancel must drop ghosts"
assert board.live_count == live_before, (
    f"cancel must restore the pre-plan count: {board.live_count} != {live_before}")
assert int((cell.ghost_state_arr(board.grid[:, :, cell.CH_Z]) == cell.GHOST_PENDING).sum()) == 0
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
click()
put_cursor_at_cell(15, 26); step()
gc = [(x, y) for x, y in
      zip(*np.nonzero(cell.ghost_state_arr(board.grid[:, :, cell.CH_Z]) == cell.GHOST_PENDING)[::-1])]
assert (15, 20) not in gc, "ghosted on top of an existing belt!"
ok(f"{len(gc)} ghost cells, crossing cell (15,20) skipped")
click()
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
_bb = BeltBuilder()
_bb.mode = MODE_HORIZONTAL_FIRST
plan = _bb.plan(engine, (0, 0), (0, 1))
built = [(p.x, p.y, p.direction) for p in plan.buildable]
assert built == [(0, 0, cell.DIR_PY)], built
ok("2x2 loop completes facing +Y, not +X")

for (s, t, blk, want) in [((2, 2), (2, 6), (2, 3), cell.DIR_PY),
                          ((6, 8), (2, 8), (5, 8), cell.DIR_NX)]:
    engine.clear_board()
    engine.place_belt(*blk)
    plan = _bb.plan(engine, s, t)
    dirs = [p.direction for p in plan.buildable]
    assert dirs and all(x == want for x in dirs), (
        s, t, [cell.DIR_NAMES[x] for x in dirs])
ok("drags through a blocker keep their heading")

# And the same holds for what actually gets committed through the UI path.
engine.clear_board(); step()
engine.place_belt(2, 3, cell.DIR_PX)
put_cursor_at_cell(2, 2); step()
click()
put_cursor_at_cell(2, 6); step()
click()
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

print("=== toolbelt selection ===")
engine.clear_board(); step()
assert orch.toolbelt.active.name == "Belt", orch.toolbelt.active.name
fake["keys"].add(glfw.KEY_2); step(); fake["keys"].discard(glfw.KEY_2); step()
assert orch.toolbelt.active.name == "Add", orch.toolbelt.active.name
ok("key 2 selected Add")
fake["keys"].add(glfw.KEY_3); step(); fake["keys"].discard(glfw.KEY_3); step()
assert orch.toolbelt.active.name == "Mul"
ok("key 3 selected Mul")
fake["keys"].add(glfw.KEY_0); step(); fake["keys"].discard(glfw.KEY_0); step()
assert orch.toolbelt.selected == 9 and orch.toolbelt.active is None
ok("key 0 selected empty slot 10")
fake["keys"].add(glfw.KEY_1); step(); fake["keys"].discard(glfw.KEY_1); step()
assert orch.toolbelt.active.name == "Belt"
ok("key 1 back to Belt")

print("=== structure placement is click-based and all-or-nothing ===")
engine.clear_board(); step()
fake["keys"].add(glfw.KEY_2); step(); fake["keys"].discard(glfw.KEY_2); step()
add_tool = orch.toolbelt.active
assert not add_tool.drag_based

# Hovering alone must ghost, with no click at all.
put_cursor_at_cell(20, 20); step()
assert len(engine.ghost_rows) == 1, len(engine.ghost_rows)
assert orch.input.state == STATE_IDLE, "structures never enter PLANNING"
ok("Add ghosts on hover without a click")

# One click places it; it covers two cells.
click()
assert not board.is_empty(20, 20) and not board.is_empty(21, 20)
placed_id = cell.cell_id(board.cell_word(20, 20))
assert cell.cell_id(board.cell_word(21, 20)) == placed_id, "both cells, one entity"
assert cell.cell_type(board.cell_word(20, 20)) == cell.TYPE_ADD
ok("single click placed a 2x1 Add sharing one entity id")

# Rotation changes the footprint orientation.
before = add_tool.direction
fake["keys"].add(glfw.KEY_R); step(); fake["keys"].discard(glfw.KEY_R); step()
assert add_tool.direction != before
put_cursor_at_cell(30, 30); step()
click()
assert not board.is_empty(30, 30) and not board.is_empty(30, 31), "rotated = vertical"
assert board.is_empty(31, 30)
ok("R rotated the Add to 1x2")

# Overlapping placement frustrates and builds nothing.
live_before = board.live_count
put_cursor_at_cell(30, 30); step()
frustrated = int((cell.ghost_state_arr(board.grid[:, :, cell.CH_Z])
                  == cell.GHOST_FRUSTRATED).sum())
assert frustrated > 0, "overlap should tint"
assert len(engine.ghost_rows) == 0, "all-or-nothing must ghost nothing"
# The occupant survives underneath the tint.
assert cell.cell_type(board.cell_word(30, 30)) == cell.TYPE_ADD
click()
assert board.live_count == live_before, "blocked placement must build nothing"
ok(f"overlap tinted {frustrated} cells, built nothing, occupant intact")

print("=== frustration never destroys the occupant ===")
engine.clear_board(); step()
fake["keys"].add(glfw.KEY_1); step(); fake["keys"].discard(glfw.KEY_1); step()
engine.place_belt(8, 8, cell.DIR_PY)
want_id = cell.cell_id(board.cell_word(8, 8))
put_cursor_at_cell(6, 8); step()
click()
put_cursor_at_cell(11, 8); step()
assert cell.is_frustrated(int(board.grid[8, 8, cell.CH_Z])), "crossing cell tinted"
assert cell.cell_id(board.cell_word(8, 8)) == want_id, "occupant id clobbered!"
assert cell.cell_dir(board.cell_word(8, 8)) == cell.DIR_PY, "occupant dir clobbered!"
ok("belt crossing tints the blocker without touching its entity")
click()
assert int((cell.ghost_state_arr(board.grid[:, :, cell.CH_Z])
            == cell.GHOST_FRUSTRATED).sum()) == 0, "commit must clear tints"
assert cell.cell_id(board.cell_word(8, 8)) == want_id
ok("commit cleared the tint, occupant still intact")

print("=== a tool must not collide with its own preview ===")
# Regression: plans were computed before the previous preview was cleared, so
# dragging a structure one cell sideways frustrated against its own ghost.
engine.clear_board(); step()
fake["keys"].add(glfw.KEY_2); step(); fake["keys"].discard(glfw.KEY_2); step()
orch.toolbelt.active.direction = cell.DIR_PX
for x in range(8, 18):
    put_cursor_at_cell(x, 24); step()
    assert orch.last_plan is not None and orch.last_plan.ok, (
        f"self-frustrated at x={x}: {orch.last_plan.frustrated_cells}")
ok("dragging an Add across 10 cells never self-frustrates")

engine.clear_board(); step()
fake["keys"].add(glfw.KEY_1); step(); fake["keys"].discard(glfw.KEY_1); step()
put_cursor_at_cell(5, 28); step()
click()
for end in range(6, 20):
    put_cursor_at_cell(end, 28); step()
    assert orch.last_plan.ok, (
        f"belt self-frustrated at {end}: {orch.last_plan.frustrated_cells}")
ok("growing a belt run never self-frustrates")
fake["right"] = True; step(); fake["right"] = False; step()

print("=== a blocked structure still previews the part that fits ===")
engine.clear_board(); step()
fake["keys"].add(glfw.KEY_2); step(); fake["keys"].discard(glfw.KEY_2); step()
# An earlier section rotated this tool; put it back to horizontal so the
# footprint is (20,30)+(21,30) and the blocker below actually clashes.
orch.toolbelt.active.direction = cell.DIR_PX
blocker = engine.place_belt(21, 30, cell.DIR_PX)
blocker_id = blocker.row
put_cursor_at_cell(20, 30); step()      # Add would cover (20,30) and (21,30)
assert cell.is_ghost(int(board.grid[30, 20, cell.CH_Z])), "free half must ghost"
assert cell.is_frustrated(int(board.grid[30, 21, cell.CH_Z])), "blocked half must tint"
assert cell.cell_id(board.cell_word(21, 30)) == blocker_id, "blocker clobbered"
assert cell.cell_type(board.cell_word(21, 30)) == cell.TYPE_BELT
ok("half-blocked Add ghosts the half that fits, tints the other")

# ...but committing it still builds nothing: half a structure cannot exist.
live_before = board.live_count
click()
assert board.live_count == live_before, "a fragment was committed!"
# The half-ghost is still shown (the cursor has not moved), but it must still
# be a *ghost* -- never promoted to a real entity.
assert cell.is_ghost(int(board.grid[30, 20, cell.CH_Z])), "fragment went real!"
assert not engine.ghost_rows, "nothing committable should remain"
assert cell.cell_id(board.cell_word(21, 30)) == blocker_id
ok("committing the blocked Add built nothing; fragment stayed a ghost")

# Moving away must clean the fragment up entirely.
put_cursor_at_cell(40, 36); step()
assert board.is_empty(20, 30), "fragment leaked after moving away"
ok("fragment cleared once the cursor moved on")
fake["keys"].add(glfw.KEY_1); step(); fake["keys"].discard(glfw.KEY_1); step()

print("=== switching tools drops an in-flight preview ===")
engine.clear_board(); step()
put_cursor_at_cell(12, 12); step()
click()
put_cursor_at_cell(18, 12); step()
assert len(engine.ghost_rows) > 0
fake["keys"].add(glfw.KEY_3); step(); fake["keys"].discard(glfw.KEY_3); step()
assert orch.toolbelt.active.name == "Mul"
assert orch.input.state == STATE_IDLE, "tool switch should cancel the gesture"
belt_ghosts = [r for r in engine.ghost_rows
               if board.is_row_alive(r)
               and cell.cell_type(board.cell_word(*board.view(r).origin)) == cell.TYPE_BELT]
assert not belt_ghosts, "belt preview should be gone"
ok("tool switch cancelled the belt run")
fake["keys"].add(glfw.KEY_1); step(); fake["keys"].discard(glfw.KEY_1); step()

print("=== shader loader ===")
from render import shader_loader
_src = shader_loader.load("world.frag")
assert "#include" not in _src, "an include was left unresolved"
assert "DIR_VECS" in _src and "sd_chevron" in _src, "common.glsl not spliced in"
# The encoding constants must appear exactly once -- a second copy would be a
# silent divergence from core/cell.py.
assert _src.count("const int TYPE_SHIFT") == 1, "common.glsl included twice"
_deps = [d.replace("\\", "/").rsplit("/", 1)[-1]
         for d in shader_loader.dependencies("world.frag")]
assert set(_deps) == {"world.frag", "common.glsl"}, _deps
ok(f"world.frag resolves includes ({len(_src)} chars, deps {_deps})")

# The shader-side encoding must agree with cell.py. Parse the constants back
# out of the GLSL and compare -- this is the one place the two copies can
# drift, so it is worth pinning.
import re as _re
def _glsl_int(name):
    m = _re.search(rf"\b{name}\s*=\s*(0x[0-9A-Fa-f]+|\d+)", _src)
    assert m, f"{name} not found in shader source"
    return int(m.group(1), 0)

for _name, _want in [
    ("DIR_SHIFT", cell.DIR_SHIFT), ("DIR_MASK", cell.DIR_MASK),
    ("TYPE_SHIFT", cell.TYPE_SHIFT), ("TYPE_MASK", cell.TYPE_MASK),
    ("ID_SHIFT", cell.ID_SHIFT), ("ID_MASK", cell.ID_MASK),
    ("GHOST_MASK", cell.GHOST_MASK), ("LOCAL_SHIFT", cell.LOCAL_SHIFT),
    ("TYPE_BELT", cell.TYPE_BELT), ("TYPE_ADD", cell.TYPE_ADD),
    ("TYPE_MUL", cell.TYPE_MUL),
    ("GHOST_PENDING", cell.GHOST_PENDING),
    ("GHOST_FRUSTRATED", cell.GHOST_FRUSTRATED),
]:
    assert _glsl_int(_name) == _want, (
        f"{_name}: shader says {_glsl_int(_name)}, cell.py says {_want}")
ok("GLSL cell encoding matches core/cell.py")

# The shader derives structure ports itself (input_face_mask /
# output_face_mask in common.glsl) so it can decide belt topology and draw
# connection stubs. Those masks must agree with Entity.faces_for(), which is
# the Python-side authority. Reimplement the shader's rotation here and
# compare against the real thing for every type and facing.
def _glsl_mask(fn_name, type_name):
    """The authored (unrotated) mask the shader uses for a type."""
    body = _re.search(rf"int {fn_name}\(int type, int facing\)\s*\{{(.*?)\n\}}",
                      _src, _re.S)
    assert body, f"{fn_name} not found in shader"
    m = _re.search(rf"type == {type_name}\)\s*m = ([^;]+);", body.group(1))
    assert m, f"{type_name} not handled in {fn_name}"
    expr = m.group(1)
    mask = 0
    for shift in _re.findall(r"1 << (DIR_\w+)", expr):
        mask |= 1 << getattr(cell, shift)
    return mask

for _cls, _tname in [(Add, "TYPE_ADD"), (Mul, "TYPE_MUL")]:
    for _facing in range(4):
        _ins, _outs = _cls.faces_for(0, 0, _facing)
        _want_in = 0
        for _c, _f in _ins:
            _want_in |= 1 << _f
        _want_out = 0
        for _c, _f in _outs:
            _want_out |= 1 << _f
        # Shader rotates the authored mask by `facing`.
        _got_in = 0
        for _d in range(4):
            if _glsl_mask("input_face_mask", _tname) & (1 << _d):
                _got_in |= 1 << ((_d + _facing) % 4)
        _got_out = 0
        for _d in range(4):
            if _glsl_mask("output_face_mask", _tname) & (1 << _d):
                _got_out |= 1 << ((_d + _facing) % 4)
        assert _got_in == _want_in, (
            f"{_cls.__name__} facing {cell.DIR_NAMES[_facing]}: shader inputs "
            f"{_got_in:04b} != entity {_want_in:04b}")
        assert _got_out == _want_out, (
            f"{_cls.__name__} facing {cell.DIR_NAMES[_facing]}: shader outputs "
            f"{_got_out:04b} != entity {_want_out:04b}")
ok("GLSL structure port masks match Entity.faces_for()")

print("=== device round trip after all that ===")
assert board.verify_round_trip(), "grid round trip mismatch"
ok("to_device/from_device match")
raw = np.frombuffer(board.entity_buffer.read(), dtype=np.float32).reshape(board.capacity, -1)
assert np.array_equal(raw, board.entities), "SSBO diverged"
ok("entity SSBO matches CPU array")

print("=== partial upload really is partial ===")
board.to_device(force=True)
put_cursor_at_cell(50, 50); step()
click()
put_cursor_at_cell(52, 50); step()
cells_up, rows_up = orch.ui.last_upload
print(f"  3-cell ghost preview uploaded {cells_up} cells (board is {board.width*board.height})")
assert 0 < cells_up < 500, cells_up
ok("dirty-rect upload stayed small")
fake["right"] = True; step(); fake["right"] = False; step()

print("=== left-drag pans instead of building ===")
engine.clear_board(); step()
fake["keys"].add(glfw.KEY_1); step(); fake["keys"].discard(glfw.KEY_1); step()
put_cursor_at_cell(30, 30); step()
c_before = cam.center.copy()
live_before = board.live_count
drag_px(120.0, 0.0)
assert orch.input.state == STATE_IDLE, orch.input.state
assert board.live_count == live_before, "a drag must not build"
assert not engine.ghost_rows, "a drag must leave no preview"
assert abs(cam.center[0] - c_before[0]) > 1.0, (c_before, cam.center)
ok(f"left-drag panned {c_before[0]:.1f} -> {cam.center[0]:.1f}, built nothing")

# Dragging right moves the view left: the world follows the cursor.
c2 = cam.center.copy()
drag_px(-120.0, 0.0)
assert cam.center[0] > c2[0], "drag direction inverted"
ok("drag direction follows the cursor")

# A press that does NOT move past the threshold is still a click.
put_cursor_at_cell(34, 30); step()
sx, sy = fake["cursor"]
fake["left"] = True; step()
fake["cursor"] = (sx + 2.0, sy + 1.0)   # inside DRAG_THRESHOLD_PX
step()
fake["left"] = False; step()
assert orch.input.state == STATE_PLANNING, (
    f"a small wobble should still click, got {orch.input.state}")
ok("sub-threshold wobble still counts as a click")
fake["keys"].add(glfw.KEY_ESCAPE); step(); fake["keys"].discard(glfw.KEY_ESCAPE); step()

print("=== scroll zooms at the cursor ===")
engine.clear_board(); step()
cam.center[:] = (32.0, 32.0); cam.zoom = 20.0
# Point at a cell well off-centre and remember what is under the cursor.
put_cursor_at_cell(44, 40); step()
mx, my = fake["cursor"]
before_world = cam.screen_to_world(mx, my, (W, H))
z0 = cam.zoom
input_manager_obj = orch.input
input_manager_obj._scroll_accum = 3.0      # three notches in
step()
assert cam.zoom > z0, (z0, cam.zoom)
after_world = cam.screen_to_world(mx, my, (W, H))
drift = max(abs(after_world[0] - before_world[0]),
            abs(after_world[1] - before_world[1]))
assert drift < 0.05, f"world point under cursor moved {drift:.3f} cells"
ok(f"zoomed {z0:.1f} -> {cam.zoom:.1f}, cursor anchor held (drift {drift:.4f})")

input_manager_obj._scroll_accum = -3.0     # and back out
step()
assert cam.zoom < z0 * 1.01
after2 = cam.screen_to_world(mx, my, (W, H))
drift2 = max(abs(after2[0] - before_world[0]), abs(after2[1] - before_world[1]))
assert drift2 < 0.05, drift2
ok(f"zoomed back out to {cam.zoom:.1f}, anchor still held")

# Scroll no longer touches the toolbelt.
slot_before = orch.toolbelt.selected
input_manager_obj._scroll_accum = 2.0
step()
assert orch.toolbelt.selected == slot_before, "scroll must not change tools"
ok("scroll leaves the toolbelt alone")

print("=== sub-cell aiming through the full stack ===")
engine.clear_board(); step()

def click_at_frac(cx, cy, fx, fy):
    """Click at a specific sub-cell position."""
    sx, sy = cam.world_to_screen(cx + fx, cy + fy, (W, H))
    fake["cursor"] = (float(sx), float(sy))
    step()
    click()

for (fx, fy), want in [((0.85, 0.5), cell.DIR_PX), ((0.15, 0.5), cell.DIR_NX),
                       ((0.5, 0.85), cell.DIR_PY), ((0.5, 0.15), cell.DIR_NY)]:
    engine.clear_board(); step()
    click_at_frac(12, 12, fx, fy)     # press+release -> PLANNING at (12,12)
    click_at_frac(12, 12, fx, fy)     # click again -> commit the single cell
    assert not board.is_empty(12, 12), f"nothing built at frac {(fx, fy)}"
    got = cell.cell_dir(board.cell_word(12, 12))
    assert got == want, (
        f"frac {(fx, fy)}: want {cell.DIR_NAMES[want]}, got {cell.DIR_NAMES[got]}")
ok("a single-cell click aims by quadrant, not always east")

# The last belt of a run turns when the pointer is past the threshold.
engine.clear_board(); step()
click_at_frac(5, 15, 0.5, 0.5)                    # start planning
sx, sy = cam.world_to_screen(11 + 0.5, 15 + 0.8, (W, H))
fake["cursor"] = (float(sx), float(sy)); step()   # high in the final cell
click()
dirs = [cell.cell_dir(board.cell_word(x, 15)) for x in range(5, 12)]
assert dirs[:-1] == [cell.DIR_PX] * 6, [cell.DIR_NAMES[d] for d in dirs]
assert dirs[-1] == cell.DIR_PY, (
    f"last belt should turn north, got {cell.DIR_NAMES[dirs[-1]]}")
ok("final belt turns north when the pointer is high in its cell")

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
click()
assert orch.input.state == STATE_IDLE, "click over UI should not start a plan"
assert board.live_count == 0
io.want_capture_mouse = False
ok("clicks over imgui ignored")

print()
print("ALL ORCHESTRATOR DRIVE TESTS PASSED")
glfw.terminate()
