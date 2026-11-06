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
from core.entity import STRUCTURES, Mac, Shear, Sink, Source, Swap

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
assert orch.toolbelt.active.name == "Swap", orch.toolbelt.active.name
ok("key 2 selected Swap")
fake["keys"].add(glfw.KEY_3); step(); fake["keys"].discard(glfw.KEY_3); step()
assert orch.toolbelt.active.name == "Shear"
ok("key 3 selected Shear")
fake["keys"].add(glfw.KEY_6); step(); fake["keys"].discard(glfw.KEY_6); step()
assert orch.toolbelt.active.name == "Sink"
ok("key 6 selected Sink")
fake["keys"].add(glfw.KEY_0); step(); fake["keys"].discard(glfw.KEY_0); step()
assert orch.toolbelt.selected == 9 and orch.toolbelt.active is None
ok("key 0 selected empty slot 10")
fake["keys"].add(glfw.KEY_1); step(); fake["keys"].discard(glfw.KEY_1); step()
assert orch.toolbelt.active.name == "Belt"
ok("key 1 back to Belt")

print("=== structure placement is click-based and all-or-nothing ===")
engine.clear_board(); step()
fake["keys"].add(glfw.KEY_3); step(); fake["keys"].discard(glfw.KEY_3); step()
shear_tool = orch.toolbelt.active
assert shear_tool.name == "Shear"
assert not shear_tool.drag_based
shear_tool.direction = cell.DIR_PX

# Hovering alone must ghost, with no click at all.
put_cursor_at_cell(20, 20); step()
assert len(engine.ghost_rows) == 1, len(engine.ghost_rows)
assert orch.input.state == STATE_IDLE, "structures never enter PLANNING"
ok("Shear ghosts on hover without a click")

# One click places it; it covers two cells.
click()
assert not board.is_empty(20, 20) and not board.is_empty(21, 20)
placed_id = cell.cell_id(board.cell_word(20, 20))
assert cell.cell_id(board.cell_word(21, 20)) == placed_id, "both cells, one entity"
assert cell.cell_type(board.cell_word(20, 20)) == cell.TYPE_SHEAR
# Each cell of the footprint carries its own local offset, which is what the
# per-cell port lookup keys on.
assert cell.unpack_local(int(board.grid[20, 20, cell.CH_Y])) == (0, 0)
assert cell.unpack_local(int(board.grid[20, 21, cell.CH_Y])) == (1, 0)
ok("single click placed a 2x1 Shear sharing one entity id")

# Rotation changes the footprint orientation.
before = shear_tool.direction
fake["keys"].add(glfw.KEY_R); step(); fake["keys"].discard(glfw.KEY_R); step()
assert shear_tool.direction != before
put_cursor_at_cell(30, 30); step()
click()
assert not board.is_empty(30, 30) and not board.is_empty(30, 31), "rotated = vertical"
assert board.is_empty(31, 30)
ok("R rotated the Shear to 1x2")

# Overlapping placement frustrates and builds nothing.
live_before = board.live_count
put_cursor_at_cell(30, 30); step()
frustrated = int((cell.ghost_state_arr(board.grid[:, :, cell.CH_Z])
                  == cell.GHOST_FRUSTRATED).sum())
assert frustrated > 0, "overlap should tint"
assert len(engine.ghost_rows) == 0, "all-or-nothing must ghost nothing"
# The occupant survives underneath the tint.
assert cell.cell_type(board.cell_word(30, 30)) == cell.TYPE_SHEAR
click()
assert board.live_count == live_before, "blocked placement must build nothing"
ok(f"overlap tinted {frustrated} cells, built nothing, occupant intact")

print("=== F flips a chiral structure, shift+F frames the board ===")
engine.clear_board(); step()
fake["keys"].add(glfw.KEY_3); step(); fake["keys"].discard(glfw.KEY_3); step()
_tool = orch.toolbelt.active
_tool.direction = cell.DIR_PX
_tool.flipped = False
assert _tool.entity_cls.is_chiral(), "Shear should be chiral"
put_cursor_at_cell(12, 40); step()
fake["keys"].add(glfw.KEY_F); step(); fake["keys"].discard(glfw.KEY_F); step()
assert _tool.flipped, "F should flip a chiral structure"
ok("F flipped the held Shear")

# The flip must reach the board, and land in .z where the renderer reads it.
click()
_fl = [c for c in _tool.entity_cls.cells_for(12, 40, cell.DIR_PX, True)]
assert all(cell.is_flipped(int(board.grid[y, x, cell.CH_Z])) for x, y in _fl), \
    "flip bit missing from the placed cells"
# ...and the ports must match the flipped layout, not the plain one.
_ins, _outs = _tool.entity_cls.faces_for(12, 40, cell.DIR_PX, True)
_plain_in, _ = _tool.entity_cls.faces_for(12, 40, cell.DIR_PX, False)
assert sorted(_ins) != sorted(_plain_in), "flip should change the port layout"
ok("flipped Shear placed with the flip bit set in .z")

# F is an involution.
fake["keys"].add(glfw.KEY_F); step(); fake["keys"].discard(glfw.KEY_F); step()
assert not _tool.flipped, "F twice should return to the original"
ok("F is an involution")

# An achiral structure ignores F entirely.
fake["keys"].add(glfw.KEY_6); step(); fake["keys"].discard(glfw.KEY_6); step()
_sink_tool = orch.toolbelt.active
assert _sink_tool.name == "Sink" and not _sink_tool.entity_cls.is_chiral()
fake["keys"].add(glfw.KEY_F); step(); fake["keys"].discard(glfw.KEY_F); step()
assert not _sink_tool.flipped, "F must be a no-op for an achiral type"
ok("F does nothing to an achiral Sink")

# shift+F still frames the board, and plain F does not.
_before = tuple(orch.camera.center)
fake["keys"].add(glfw.KEY_F)
fake["keys"].add(glfw.KEY_LEFT_SHIFT); step()
fake["keys"].discard(glfw.KEY_F); fake["keys"].discard(glfw.KEY_LEFT_SHIFT)
step()
assert tuple(orch.camera.center) != _before or orch.camera.center[0] == board.width * 0.5
ok("shift+F framed the board")
fake["keys"].add(glfw.KEY_1); step(); fake["keys"].discard(glfw.KEY_1); step()

print("=== structures are centred on the cursor, not cornered on it ===")
engine.clear_board(); step()
fake["keys"].add(glfw.KEY_5); step(); fake["keys"].discard(glfw.KEY_5); step()
_src_tool = orch.toolbelt.active
assert _src_tool.name == "Source", _src_tool.name
_src_tool.direction = cell.DIR_PX
put_cursor_at_cell(30, 10); step()
click()
# A 4x1 placed at (30,10) must straddle the cursor, not start there.
assert not board.is_empty(30, 10), "cursor cell must be covered"
assert not board.is_empty(29, 10), "footprint should extend behind the cursor"
assert not board.is_empty(32, 10) and board.is_empty(33, 10)
ok("4x1 Source straddles the cursor (29..32, not 30..33)")

# Rotating turns about the middle: the cursor cell stays covered.
engine.clear_board(); step()
for _d in range(4):
    _src_tool.direction = _d
    put_cursor_at_cell(30, 20); step()
    assert orch.last_plan is not None and orch.last_plan.ok
    _p = orch.last_plan.placements[0]
    assert (30, 20) in _p.cells(), (
        f"cursor left the footprint at {cell.DIR_NAMES[_d]}: {_p.cells()}")
ok("the cursor stays inside the footprint through a full rotation")

# On an EVEN axis the midline is a cell boundary, so the sub-cell position
# picks *which* boundary -- the structure tracks the pointer every half cell
# rather than jumping a whole one.
def put_cursor_at(wx, wy):
    """Place the fake cursor at an arbitrary world point, not a cell centre."""
    sx, sy = cam.world_to_screen(wx, wy, (W, H))
    fake["cursor"] = (float(sx), float(sy))

engine.clear_board(); step()
fake["keys"].add(glfw.KEY_3); step(); fake["keys"].discard(glfw.KEY_3); step()
_sh = orch.toolbelt.active
_sh.direction = cell.DIR_PX
_sh.flipped = False
put_cursor_at(30.2, 20.5); step()          # left half of cell 30
_left = sorted(orch.last_plan.placements[0].cells())
put_cursor_at(30.8, 20.5); step()          # right half of the same cell
_right = sorted(orch.last_plan.placements[0].cells())
assert _left != _right, (
    f"a 2x1 must shift within one cell: {_left} vs {_right}")
assert _left == [(29, 20), (30, 20)], _left
assert _right == [(30, 20), (31, 20)], _right
assert (30, 20) in _left and (30, 20) in _right, "cursor cell always covered"
ok(f"2x1 seam follows the pointer within a cell: {_left} -> {_right}")

# What you see is what you build: committing must land on the ghosted cells,
# not on a different rounding of the same cursor cell.
put_cursor_at(30.2, 20.5); step()
_ghosted = sorted(orch.last_plan.placements[0].cells())
click()
_built = sorted(c for c in _ghosted if not board.is_empty(*c))
assert _built == _ghosted, f"built {_built}, ghosted {_ghosted}"
assert board.is_empty(31, 20), "built on the wrong side of the cursor"
ok(f"commit lands exactly where the ghost was: {_built}")

# An odd axis has a middle cell, so the fraction must change nothing.
fake["keys"].add(glfw.KEY_6); step(); fake["keys"].discard(glfw.KEY_6); step()
assert orch.toolbelt.active.name == "Sink"
put_cursor_at(30.1, 20.1); step()
_a = sorted(orch.last_plan.placements[0].cells())
put_cursor_at(30.9, 20.9); step()
_b = sorted(orch.last_plan.placements[0].cells())
assert _a == _b, f"3x3 should ignore the sub-cell position: {_a} vs {_b}"
ok("3x3 Sink ignores the sub-cell position, as an odd extent should")

fake["keys"].add(glfw.KEY_1); step(); fake["keys"].discard(glfw.KEY_1); step()

print("=== rotating a structure keeps its ports on the outside ===")
# Regression: the shader looked its port tables up with the placed .y offset
# instead of inverting it, so a rotated structure's ports moved to interior
# edges. Sink is the sharpest case -- 3x3, rotation-invariant extent.
engine.clear_board(); step()
for _cls in (Sink, Source, Swap, Shear, Mac):
    for _d in range(4):
        for _fl in (False, True):
            _ins, _outs = _cls.faces_for(20, 20, _d, _fl)
            _occ = set(_cls.cells_for(20, 20, _d, _fl))
            _w, _h = _cls.footprint_for(_d)
            for (_xy, _f) in _ins + _outs:
                assert _xy in _occ, (_cls.__name__, _d, _fl, _xy)
                # A port must face *out* of the footprint: the cell one step
                # along the face direction must be outside it.
                _nx = _xy[0] + cell.DIR_STEPS[_f][0]
                _ny = _xy[1] + cell.DIR_STEPS[_f][1]
                assert (_nx, _ny) not in _occ, (
                    f"{_cls.__name__} facing {cell.DIR_NAMES[_d]} flip={_fl}: "
                    f"port at {_xy} faces {cell.DIR_NAMES[_f]} into the "
                    f"footprint's interior")
ok("every port faces outward at every rotation and flip")

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
fake["keys"].add(glfw.KEY_3); step(); fake["keys"].discard(glfw.KEY_3); step()
orch.toolbelt.active.direction = cell.DIR_PX
for x in range(8, 18):
    put_cursor_at_cell(x, 24); step()
    assert orch.last_plan is not None and orch.last_plan.ok, (
        f"self-frustrated at x={x}: {orch.last_plan.frustrated_cells}")
ok("dragging a Shear across 10 cells never self-frustrates")

# The same, for the largest footprint -- a 3x3 overlaps its own previous
# position far more heavily, so it is the real test of the clear-then-plan
# ordering.
engine.clear_board(); step()
fake["keys"].add(glfw.KEY_6); step(); fake["keys"].discard(glfw.KEY_6); step()
assert orch.toolbelt.active.name == "Sink"
for x in range(8, 18):
    put_cursor_at_cell(x, 24); step()
    assert orch.last_plan is not None and orch.last_plan.ok, (
        f"Sink self-frustrated at x={x}: {orch.last_plan.frustrated_cells}")
click()
assert board.live_count == 1, "3x3 Sink is one entity"
# Structures are centred on the cursor, so a 3x3 clicked at (17,24) has its
# origin one cell down and left.
_ox, _oy = Sink.origin_for_cursor(17, 24, cell.DIR_PX)
assert (_ox, _oy) == (16, 23), (_ox, _oy)
_sink_id = cell.cell_id(board.cell_word(_ox, _oy))
for _dx in range(3):
    for _dy in range(3):
        assert cell.cell_id(
            board.cell_word(_ox + _dx, _oy + _dy)) == _sink_id, (_dx, _dy)
        assert cell.unpack_local(
            int(board.grid[_oy + _dy, _ox + _dx, cell.CH_Y])) == (_dx, _dy)
# The cursor cell really is the middle one.
assert cell.unpack_local(int(board.grid[24, 17, cell.CH_Y])) == (1, 1)
ok("3x3 Sink centres on the cursor, one entity, correct local offsets")

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
fake["keys"].add(glfw.KEY_3); step(); fake["keys"].discard(glfw.KEY_3); step()
# An earlier section rotated this tool; put it back to horizontal so the
# footprint is (20,30)+(21,30) and the blocker below actually clashes.
orch.toolbelt.active.direction = cell.DIR_PX
blocker = engine.place_belt(21, 30, cell.DIR_PX)
blocker_id = blocker.row
put_cursor_at_cell(20, 30); step()      # Shear would cover (20,30) and (21,30)
assert cell.is_ghost(int(board.grid[30, 20, cell.CH_Z])), "free half must ghost"
assert cell.is_frustrated(int(board.grid[30, 21, cell.CH_Z])), "blocked half must tint"
assert cell.cell_id(board.cell_word(21, 30)) == blocker_id, "blocker clobbered"
assert cell.cell_type(board.cell_word(21, 30)) == cell.TYPE_BELT
ok("half-blocked Shear ghosts the half that fits, tints the other")

# ...but committing it still builds nothing: half a structure cannot exist.
live_before = board.live_count
click()
assert board.live_count == live_before, "a fragment was committed!"
# The half-ghost is still shown (the cursor has not moved), but it must still
# be a *ghost* -- never promoted to a real entity.
assert cell.is_ghost(int(board.grid[30, 20, cell.CH_Z])), "fragment went real!"
assert not engine.ghost_rows, "nothing committable should remain"
assert cell.cell_id(board.cell_word(21, 30)) == blocker_id
ok("committing the blocked Shear built nothing; fragment stayed a ghost")

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
assert orch.toolbelt.active.name == "Shear"
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
    ("FLIP_SHIFT", cell.FLIP_SHIFT),
    ("TYPE_BELT", cell.TYPE_BELT), ("TYPE_SWAP", cell.TYPE_SWAP),
    ("TYPE_SHEAR", cell.TYPE_SHEAR), ("TYPE_MAC", cell.TYPE_MAC),
    ("TYPE_SOURCE", cell.TYPE_SOURCE), ("TYPE_SINK", cell.TYPE_SINK),
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
#
# Ports are *per-cell*, so this has to agree cell by cell, not merely type by
# type: a port belongs to one cell of a footprint, and a shader that spread it
# across all of them would draw arrows on the wrong cells and stub to the
# wrong neighbours. Parse the authored per-cell masks out of the GLSL and
# compare every (type, local cell) pair against Entity.face_masks_at().
def _glsl_cell_masks(fn_name, type_name):
    """{(lx, ly): mask} the shader uses for a type, authored (unrotated)."""
    body = _re.search(
        rf"int {fn_name}\(int type, ivec2 lcl\)\s*\{{(.*?)\n\}}", _src, _re.S)
    assert body, f"{fn_name} not found in shader"
    # The branch for this type runs until the next `else if (type ==` or the
    # closing of the chain.
    branch = _re.search(
        rf"type == {type_name}\)\s*\{{(.*?)\}}", body.group(1), _re.S)
    if not branch:
        return {}
    out = {}
    for lx, ly, expr in _re.findall(
            r"lcl == ivec2\((\d+),\s*(\d+)\)\)\s*return ([^;]+);",
            branch.group(1)):
        mask = 0
        for face in _re.findall(r"FACE\((DIR_\w+)\)", expr):
            mask |= 1 << getattr(cell, face)
        out[(int(lx), int(ly))] = mask
    return out

_checked = 0
for _cls in STRUCTURES:
    _tname = "TYPE_" + cell.TYPE_NAMES[_cls.TYPE_ID].upper()
    _glsl_in = _glsl_cell_masks("input_face_mask_local", _tname)
    _glsl_out = _glsl_cell_masks("output_face_mask_local", _tname)
    _w, _h = _cls.FOOTPRINT
    for _lx in range(_w):
        for _ly in range(_h):
            _want_in, _want_out = _cls.face_masks_at(_lx, _ly)
            _got_in = _glsl_in.get((_lx, _ly), 0)
            _got_out = _glsl_out.get((_lx, _ly), 0)
            assert _got_in == _want_in, (
                f"{_cls.__name__} cell {(_lx, _ly)}: shader inputs "
                f"{_got_in:04b} != entity {_want_in:04b}")
            assert _got_out == _want_out, (
                f"{_cls.__name__} cell {(_lx, _ly)}: shader outputs "
                f"{_got_out:04b} != entity {_want_out:04b}")
            _checked += 1
    # The shader must not carry ports for cells outside the footprint.
    _authored = {(x, y) for x in range(_w) for y in range(_h)}
    _stray = (set(_glsl_in) | set(_glsl_out)) - _authored
    assert not _stray, f"{_cls.__name__}: shader ports off the footprint: {_stray}"

    # And the full shader pipeline must put each port on the SAME CELL as
    # faces_for() does, at every orientation.
    #
    # Regression: the shader used to look its tables up with the *placed*
    # offset straight out of .y while the tables are authored at DIR_PX. That
    # un-rotated a port's face while leaving its cell put, so a rotated
    # structure's ports moved to interior edges. A 3x3 Sink hid it best --
    # its extent is rotation-invariant, so nothing else looked wrong.
    #
    # Checking an aggregate mask over the whole footprint cannot catch that:
    # the set of faces is right, it is the cell-to-face pairing that breaks.
    # So compare the (cell, face) pairs.
    for _facing in range(4):
        for _flip in (False, True):
            for _which, _tbl in (("inputs", _glsl_in), ("outputs", _glsl_out)):
                _want = {
                    (xy, f) for xy, f in
                    _cls.faces_for(0, 0, _facing, _flip)[0 if _which == "inputs" else 1]
                }
                # Reproduce what the shader does, cell by cell: for each cell
                # of the PLACED footprint, invert to authored space, read the
                # table, then flip and rotate the faces.
                _got = set()
                _ew, _eh = _cls.footprint_for(_facing)
                for _px in range(_ew):
                    for _py in range(_eh):
                        _a = _cls.unplace_local(_px, _py, _facing, _flip)
                        _m = _tbl.get(_a, 0)
                        for _d in range(4):
                            if not _m & (1 << _d):
                                continue
                            _f = cell.flip_dir_y(_d) if _flip else _d
                            _got.add(((_px, _py),
                                      cell.rotate_dir_ccw_n(_f, _facing)))
                assert _got == _want, (
                    f"{_cls.__name__} {_which} facing "
                    f"{cell.DIR_NAMES[_facing]} flip={_flip}:\n"
                    f"  shader {sorted(_got)}\n  entity {sorted(_want)}")
ok(f"GLSL per-cell port masks match Entity.face_masks_at() ({_checked} cells)")
ok("GLSL ports land on the right cell at every rotation and flip")

# The simulation above reproduces the shader's *tables*, but a table can be
# perfect while the lookup is wrong -- which is exactly how the rotation bug
# happened. So pin the lookup itself: the public mask functions must invert
# the placed offset before indexing, and must apply the flip to the faces.
for _fn in ("input_face_mask", "output_face_mask"):
    _body = _re.search(
        rf"int {_fn}\(int type, ivec2 lcl, int facing, bool flipped\)"
        rf"\s*\{{(.*?)\n\}}", _src, _re.S)
    assert _body, f"{_fn} not found with the expected signature"
    _b = _body.group(1)
    assert "unplace_local(type, lcl, facing, flipped)" in _b, (
        f"{_fn} must invert the placed offset before the table lookup -- "
        f"indexing with the raw .y offset puts ports on interior edges")
    assert "flip_face_mask" in _b, f"{_fn} must mirror faces when flipped"
    assert "rotate_face_mask" in _b, f"{_fn} must rotate faces into the facing"
ok("GLSL mask functions invert the placed offset before lookup")

# The two neighbour probes are the call sites where the flip flag is easy to
# get wrong: each must read the *neighbour's* own flip bit out of its .z
# rather than reusing the calling cell's, exactly as it already does for the
# neighbour's local offset and facing.
for _probe in ("feeds_into", "accepts_from"):
    _pb = _re.search(rf"bool {_probe}\([^)]*\)\s*\{{(.*?)\n\}}", _src, _re.S)
    assert _pb, f"{_probe} not found"
    assert "is_flipped(n.z)" in _pb.group(1), (
        f"{_probe} must read the neighbour's own flip bit (is_flipped(n.z))")
# The structure-drawing path must pass its own cell's flip bit too.
assert _re.search(r"input_face_mask\(type, lcl, facing, flipped\)", _src), \
    "the structure draw path must pass its cell's flip flag"
assert _re.search(r"output_face_mask\(type, lcl, facing, flipped\)", _src), \
    "the structure draw path must pass its cell's flip flag"
ok("GLSL port mask callers pass the flip flag")

# The control-channel overlay is declared on both sides and must agree.
for _cls in STRUCTURES:
    _tname = "TYPE_" + cell.TYPE_NAMES[_cls.TYPE_ID].upper()
    _m = _re.search(rf"type == {_tname}\)\s*return ([^;]+);",
                    _re.search(r"int pass_through_mask\(int type\)\s*\{(.*?)\n\}",
                               _src, _re.S).group(1))
    _got = 0
    if _m:
        for _face in _re.findall(r"FACE\((DIR_\w+)\)", _m.group(1)):
            _got |= 1 << getattr(cell, _face)
    _want = 0
    for _f in _cls.PASS_THROUGH:
        _want |= 1 << _f
    assert _got == _want, (
        f"{_cls.__name__}: shader pass-through {_got:04b} != "
        f"Entity.PASS_THROUGH {_want:04b}")
    # A declared pass-through axis must actually have ports on it, or the
    # overlay would draw a channel the machine has no way to carry.
    for _f in _cls.PASS_THROUGH:
        _faces = {f for _c, f in _cls.faces_for(0, 0, cell.DIR_PX)[1]}
        assert _f in _faces, (
            f"{_cls.__name__}: PASS_THROUGH names {cell.DIR_NAMES[_f]} but "
            f"no output face points that way")
ok("GLSL pass_through_mask() matches Entity.PASS_THROUGH")

# The shader's authored footprint table must match Entity.FOOTPRINT, since
# unplace_local() uses it to invert the placement transform.
for _cls in STRUCTURES:
    _tname = "TYPE_" + cell.TYPE_NAMES[_cls.TYPE_ID].upper()
    _m = _re.search(rf"type == {_tname}\)\s*return ivec2\((\d+),\s*(\d+)\);",
                    _src)
    assert _m, f"{_tname} missing from type_footprint() in the shader"
    _got_fp = (int(_m.group(1)), int(_m.group(2)))
    assert _got_fp == _cls.FOOTPRINT, (
        f"{_cls.__name__}: shader footprint {_got_fp} != {_cls.FOOTPRINT}")
ok("GLSL type_footprint() matches Entity.FOOTPRINT")

# The shader's footprint probe must reach far enough to span the largest
# machine, or a multi-cell body would draw as two separate shapes.
_reach = int(_re.search(r"#define PROBE_REACH\s+(\d+)", _src).group(1))
_longest = max(max(c.FOOTPRINT) for c in STRUCTURES)
assert _reach >= _longest - 1, (
    f"PROBE_REACH {_reach} too small for a {_longest}-cell footprint")
ok(f"PROBE_REACH {_reach} spans the longest footprint ({_longest})")

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
