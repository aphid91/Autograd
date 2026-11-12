"""Scenario tests: drive the orchestrator headlessly and assert board state.

Each function is an independent scenario. tests/harness.py resets the board,
camera, input and toolbelt before every one, so a failure names the scenario
that caused it rather than surfacing hundreds of lines later in an unrelated
section -- which is what the old single-script suite did.

Run with `python test_drive.py`, or a subset with
`python test_drive.py <substring>`.
"""
import numpy as np
import glfw
from imgui_bundle import imgui

from core import cell
from core.builders import BeltBuilder
from core.entity import Mac, Shear, Sink, Source, Swap, Underground
from core.toolbelt import ROW_SIZE
from engine import MODE_HORIZONTAL_FIRST, MODE_VERTICAL_FIRST, MODE_LINE
from ui.input_manager import STATE_IDLE, STATE_PLANNING, STATE_ERASING

from tests import harness
from tests.harness import (
    W, H, board, cam, click, drag_px, engine, fake, orch, ok, press_key,
    put_cursor_at, put_cursor_at_cell, select_tool, step, cells_of_type,
    ghost_cell_count, frustrated_cell_count,
)


def test_seeded_board_renders():
    """The demo layout places entities and survives a frame."""
    orch.seed_demo()
    step(2)
    assert board.live_count > 0, "seed_demo should have placed belts"
    assert cells_of_type(cell.TYPE_BELT) > 0
    ok(f"seed_demo placed {board.live_count} entities, "
       f"{cells_of_type(cell.TYPE_BELT)} belt cells")

def test_plan_a_belt_run_cycle_modes_commit():
    """Plan a belt run, cycle modes, commit."""
    select_tool("Belt")
    # Nothing *built* yet. Not live_count == 0: selecting a drag-based tool
    # starts previewing a one-cell idle run straight away, and a ghost is a
    # live row occupying a real cell -- so the count that means "empty board"
    # is belt cells minus ghosted ones.
    assert cells_of_type(cell.TYPE_BELT) - ghost_cell_count() == 0
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
    press_key(glfw.KEY_R)
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

def test_cancel_with_right_click():
    """Cancel with right click."""
    select_tool("Belt")
    # Baseline is a plain *hover*, not an untouched board: a drag-based tool
    # previews a one-cell run while idle (that ghost is what distinguishes
    # "belt planner, no plan" from "empty hand"), so cancelling a run returns
    # to one ghost rather than to none. Ghosts are real rows and do count
    # toward live_count, hence measuring the hover rather than assuming zero.
    put_cursor_at_cell(40, 40); step()
    live_hovering = board.live_count
    ghosts_hovering = len(engine.ghost_rows)
    assert ghosts_hovering == 1, (
        f"idle hover should preview exactly one belt, got {ghosts_hovering}")

    click()
    assert orch.input.state == STATE_PLANNING
    put_cursor_at_cell(46, 44); step()
    assert len(engine.ghost_rows) > ghosts_hovering
    assert board.live_count > live_hovering, "ghosts should be live rows"
    fake["right"] = True; step()
    assert orch.input.state == STATE_IDLE, orch.input.state
    assert not engine.ghost_rows, "the cancel frame itself drops every ghost"
    # The idle preview comes back on the next frame, with the cursor still
    # where it was -- the cancel forgets the hover memo precisely so the hand
    # does not look empty until the mouse moves.
    step()
    assert board.live_count == live_hovering, (
        f"cancel must restore the hovering count: {board.live_count} != "
        f"{live_hovering}")
    assert len(engine.ghost_rows) == ghosts_hovering, (
        "cancel must drop the planned run, leaving only the idle preview")
    # Nothing was *built*: every ghost flag still on the board belongs to the
    # idle preview, which is one cell.
    assert int((cell.ghost_state_arr(board.grid[:, :, cell.CH_Z])
                == cell.GHOST_PENDING).sum()) == ghosts_hovering
    ok("right-click cancelled, nothing built")

    # The cancelling right-press must NOT have started an erase.
    assert orch.input.state == STATE_IDLE
    fake["right"] = False; step()
    ok("cancel did not fall through to erase")

def test_crossing_an_existing_belt_skips_locked_cells():
    """Crossing an existing belt skips locked cells."""
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

def test_blocked_cells_must_not_change_belt_orientation():
    """Blocked cells must not change belt orientation."""
    select_tool("Belt")
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

def test_right_drag_erase():
    """Right-drag erase."""
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

def test_fast_drag_leaves_no_gaps():
    """Fast drag leaves no gaps."""
    engine.place_belt_path((0, 30), (40, 30), MODE_HORIZONTAL_FIRST); step()
    put_cursor_at_cell(2, 30); step()
    fake["right"] = True; step()
    put_cursor_at_cell(30, 30); step()     # one huge jump
    fake["right"] = False; step()
    gaps = [x for x in range(2, 31) if not board.is_empty(x, 30)]
    assert not gaps, f"fast drag left belts behind at {gaps}"
    ok("interpolated erase across a 28-cell jump")

#: The key that means "the tenth column". Worth testing separately because it
#: is the one digit with its own branch in the input manager's digit-to-column
#: map (`0` means column 9, not column -1), so it is the one that can quietly
#: disagree with the other nine about which row it addresses.
_DIGIT_0_KEY = glfw.KEY_0


def test_toolbelt_selection():
    """Toolbelt selection."""
    # The hand starts *empty*, Factorio-style -- not on slot 0. An empty hand
    # is a distinct state from a selected-but-empty slot, so check both the
    # tool and the hand.
    assert orch.toolbelt.active is None, orch.toolbelt.active
    assert orch.toolbelt.empty_handed, orch.toolbelt.selected
    # Select by name rather than by hardcoded digit: the slot order shifts
    # whenever a tool is added, and every test below cares about *which tool*,
    # never about which key reaches it. This used to carry its own copy of
    # `harness.select_tool()`, which then had to learn about rows twice --
    # so it uses the shared one and spends its assertions on the *mapping*
    # instead, which is the thing this scenario is actually about.
    for _name in ("Route", "Shear", "Max", "Underground"):
        select_tool(_name)
        ok(f"number key selected {_name}")

    # Key 0 is the tenth column, not the tenth tool: the digits address a
    # row, so 0 means "last slot of whichever row is up".
    tenth = orch.toolbelt.slots[ROW_SIZE - 1]
    select_tool(tenth.name)
    assert orch.toolbelt.selected == ROW_SIZE - 1, orch.toolbelt.selected
    assert orch.toolbelt.row == 0, orch.toolbelt.row
    ok(f"key 0 selected column 10 of row 1 ({tenth.name})")

    # Row 2 is reachable only through the row swap, and reaching it must not disturb
    # what the other row holds. Swap and Sink live there in the default
    # layout, which is the case that would silently fail if the swap broke:
    # `select_tool` would land on row 1's tool of the same column.
    row2 = [s for s in orch.toolbelt.slots[ROW_SIZE:] if s is not None]
    assert row2, "the default layout should put tools on row 2"
    select_tool(row2[0].name)
    assert orch.toolbelt.row == 1, orch.toolbelt.row
    assert orch.toolbelt.selected >= ROW_SIZE, orch.toolbelt.selected
    ok(f"X reached row 2 and a digit selected {row2[0].name}")

    # Key 0 has to respect the row like every other digit -- see
    # `_DIGIT_0_KEY`. Checked on row 2, where "column 10 of the active row"
    # and "slot 10" are different answers.
    assert orch.toolbelt.row == 1, "still on row 2 for this check"
    press_key(_DIGIT_0_KEY)
    assert orch.toolbelt.selected == ROW_SIZE * 2 - 1, (
        f"key 0 on row 2 selected slot {orch.toolbelt.selected}, not the "
        f"last slot of row 2")
    ok("key 0 addressed the active row like the other digits")

    # And the swap is purely an addressing change: it must not alter the hand.
    held = orch.toolbelt.selected
    press_key(glfw.KEY_X)
    assert orch.toolbelt.row == 0, "X should have swapped back"
    assert orch.toolbelt.selected == held, (
        f"the row swap changed the held slot from {held} to {orch.toolbelt.selected}")
    ok("X re-aimed the digits without changing what was held")

    select_tool("Belt")
    assert orch.toolbelt.active.name == "Belt"
    ok("key 1 back to Belt")

def test_structure_placement_is_click_based_and_all_or_nothing():
    """Structure placement is click-based and all-or-nothing."""
    select_tool("Shear")
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
    press_key(glfw.KEY_R)
    assert shear_tool.direction != before
    put_cursor_at_cell(30, 30); step()
    click()
    assert not board.is_empty(30, 30) and not board.is_empty(30, 31), "rotated = vertical"
    assert board.is_empty(31, 30)
    ok("R rotated the Shear to 1x2")

    # Overlapping placement frustrates and builds nothing.
    #
    # The obstacle has to be a *different* machine. Hovering this tool over an
    # entity it would have placed identically is a **match**, not a plain
    # clash: it tints green and a click substitutes it (see the matched-ghost
    # tests in test_clipboard.py). Overlapping the rotated Shear at (30,30)
    # with the same rotated Shear tool was therefore exactly the case that
    # stopped being a refusal, which is why this now places a Source to
    # collide with instead.
    select_tool("Source")
    put_cursor_at_cell(40, 40); step()
    click()
    assert not board.is_empty(40, 40), "need an obstacle to collide with"

    select_tool("Shear")
    live_before = board.live_count
    put_cursor_at_cell(40, 40); step()
    frustrated = int((cell.ghost_state_arr(board.grid[:, :, cell.CH_Z])
                      == cell.GHOST_FRUSTRATED).sum())
    matched = int((cell.ghost_state_arr(board.grid[:, :, cell.CH_Z])
                   == cell.GHOST_MATCHED).sum())
    assert frustrated > 0, "overlap should tint"
    assert matched == 0, "a different machine is not a match"
    assert len(engine.ghost_rows) == 0, "all-or-nothing must ghost nothing"
    # The occupant survives underneath the tint.
    assert cell.cell_type(board.cell_word(40, 40)) == cell.TYPE_SOURCE
    click()
    assert board.live_count == live_before, "blocked placement must build nothing"
    ok(f"overlap tinted {frustrated} cells, built nothing, occupant intact")

def test_f_flips_a_chiral_structure_shift_f_frames_the_board():
    """F mirrors a chiral structure; shift+F still frames the board."""
    select_tool("Shear")
    _tool = orch.toolbelt.active
    _tool.direction = cell.DIR_PX
    _tool.flipped = False
    assert _tool.entity_cls.is_chiral(), "Shear should be chiral"
    put_cursor_at_cell(12, 40); step()
    press_key(glfw.KEY_F)
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
    press_key(glfw.KEY_F)
    assert not _tool.flipped, "F twice should return to the original"
    ok("F is an involution")

    # An achiral structure ignores F entirely. Every machine the toolbelt
    # currently offers is chiral, so this goes through a builder made
    # directly: Sink is still a defined type (an old save can contain one),
    # it is simply not on the bar. Driving it through the toolbelt would mean
    # adding a slot purely to test a no-op.
    from core.builders import StructureBuilder
    from core.entity import Sink
    assert not Sink.is_chiral(), (
        "Sink is this check's achiral subject -- if it became chiral, pick "
        "another type rather than deleting the assertion")
    _sink_tool = StructureBuilder(Sink)
    _sink_tool.flip()
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
    select_tool("Belt")

def test_structures_are_centred_on_the_cursor_not_cornered_on_it():
    """Structures are centred on the cursor, not cornered on it."""
    select_tool("Source")
    _src_tool = orch.toolbelt.active
    assert _src_tool.name == "Source", _src_tool.name
    _src_tool.direction = cell.DIR_PX
    put_cursor_at_cell(30, 10); step()
    click()
    # A 2x1 must straddle the cursor. Its midline is a cell *boundary*, and a
    # cursor at dead centre rounds to the cell's far edge, so the footprint
    # lands on (30,10)-(31,10) -- the cursor cell plus one, never a corner at
    # the cursor with the body running off to one side only.
    assert not board.is_empty(30, 10), "cursor cell must be covered"
    assert not board.is_empty(31, 10), "footprint should extend past the cursor"
    assert board.is_empty(29, 10) and board.is_empty(32, 10)
    ok("2x1 Source straddles the cursor (30..31)")

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
    select_tool("Shear")
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

    # An odd axis has a middle cell, so the fraction must change nothing there.
    # Source is 2x1 -- even on x, odd on y -- so it exercises both rules at once:
    # moving across the cell shifts it, moving up the cell does not.
    _src = select_tool("Source")
    _src.direction = cell.DIR_PX
    put_cursor_at(30.2, 20.2); step()
    _lo = sorted(orch.last_plan.placements[0].cells())
    put_cursor_at(30.2, 20.8); step()
    _hi = sorted(orch.last_plan.placements[0].cells())
    assert _lo == _hi, f"odd axis must ignore the fraction: {_lo} vs {_hi}"
    put_cursor_at(30.8, 20.2); step()
    _right2 = sorted(orch.last_plan.placements[0].cells())
    assert _right2 != _lo, f"even axis must track the fraction: {_lo} vs {_right2}"
    ok("2x1 Source tracks the pointer across x, ignores it up y")

    select_tool("Belt")

def test_an_empty_hand_builds_nothing():
    """The default state is an empty hand, and clicking does nothing.

    Not the same as a selected-but-empty slot: an empty hand is its own
    state, and a future structure-selection tool will live on it.
    """
    assert orch.toolbelt.empty_handed, orch.toolbelt.selected
    assert orch.toolbelt.active is None

    put_cursor_at_cell(20, 20); step()
    assert not engine.ghost_rows, "an empty hand must not preview anything"
    click()
    assert board.live_count == 0, "an empty hand must not build"
    assert board.is_empty(20, 20)
    ok("an empty hand previews nothing and builds nothing")

    # Erase still works -- it is not a tool, it is the right mouse button.
    select_tool("Belt")
    put_cursor_at_cell(21, 21); step(); click(); click()
    assert not board.is_empty(21, 21), "setup: a belt should exist to erase"
    orch.toolbelt.clear(); step()
    fake["right"] = True; step(); fake["right"] = False; step()
    assert board.is_empty(21, 21), "right-drag erase must work empty-handed"
    ok("erase works with an empty hand")


def test_q_picks_up_and_puts_down():
    """Q is the pipette: fill an empty hand, or empty a full one."""
    # Build something to pick up, with a non-default orientation so the
    # clone has something to get wrong.
    _sh = select_tool("Shear")
    _sh.direction = cell.DIR_PY
    _sh.flipped = True
    put_cursor_at_cell(25, 25); step()
    click()
    _built = board.entity_at(25, 25)
    assert _built is not None, "setup: a Shear should be placed"
    _dir, _flip = _built.direction, bool(_built.flipped)

    # Empty the hand, then pipette it back.
    orch.toolbelt.clear(); step()
    assert orch.toolbelt.empty_handed
    press_key(glfw.KEY_Q)
    tool = orch.toolbelt.active
    assert tool is not None, "Q over a Shear should fill the hand"
    assert tool.name == "Shear", tool.name
    # Orientation must come across, both halves: rotation *and* flip. A
    # mirrored chiral machine looks identical in outline and wires up
    # differently, so taking only the rotation is a silent error.
    assert tool.direction == _dir, (tool.direction, _dir)
    assert tool.flipped == _flip, (tool.flipped, _flip)
    ok(f"Q cloned a Shear at {cell.DIR_NAMES[_dir]} flipped={_flip}")

    # Q again puts it down, whatever is under the cursor.
    press_key(glfw.KEY_Q)
    assert orch.toolbelt.empty_handed, orch.toolbelt.selected
    assert not engine.ghost_rows, "putting down must drop the preview"
    ok("Q with a full hand empties it")

    # Q over bare ground leaves the hand empty rather than grabbing a slot.
    put_cursor_at_cell(60, 60); step()
    press_key(glfw.KEY_Q)
    assert orch.toolbelt.empty_handed, (
        f"Q over empty ground should stay empty, got {orch.toolbelt.active}")
    ok("Q over bare ground is a no-op")


def test_q_pipettes_a_belt_and_an_underground():
    """The pipette is generic: every tool names the type it places."""
    select_tool("Belt")
    put_cursor_at_cell(30, 30); step(); click(); click()
    assert not board.is_empty(30, 30), "setup: a belt"
    orch.toolbelt.clear(); step()
    put_cursor_at_cell(30, 30); step()
    press_key(glfw.KEY_Q)
    assert orch.toolbelt.active is not None and \
        orch.toolbelt.active.name == "Belt", orch.toolbelt.active
    ok("Q picked up a belt")

    # An underground end carries its role as well as its facing: pipetting
    # an exit should leave you holding an exit, not resuming the alternation.
    _u = select_tool("Underground")
    _u.direction = cell.DIR_PY
    put_cursor_at_cell(35, 35); step(); click()      # entrance
    put_cursor_at_cell(35, 38); step(); click()      # exit, pairs upward
    _exit = board.entity_at(35, 38)
    assert _exit is not None and _exit.is_exit, "setup: an exit at (35,38)"
    orch.toolbelt.clear(); step()
    put_cursor_at_cell(35, 38); step()
    press_key(glfw.KEY_Q)
    _t = orch.toolbelt.active
    assert _t is not None and _t.name == "Underground", _t
    assert _t.direction == cell.DIR_PY, cell.DIR_NAMES[_t.direction]
    assert _t.placing_exit, "pipetting an exit should leave you laying an exit"
    ok("Q picked up an underground exit with its facing and role")


def test_q_reaches_a_tool_on_the_inactive_row():
    """The pipette scans **both** rows and switches rows to reach a match.

    Q's job is "give me the tool for this thing". Failing because the tool
    happens to sit on the row the number keys are not addressing would be a
    surprise, and it is reachable with the default layout: Swap and Sink live
    on row 2, and before the bar had two rows they had no tool at all -- which
    is exactly why pressing Q on one did nothing.

    The row has to come along, too. Selecting a slot on row 2 while the digits
    still address row 1 would leave the highlighted slot and the live digits
    in different rows, so the next number key would silently change tools.
    """
    from core.entity import Swap
    from core.toolbelt import ROW_SIZE

    # Swap is on row 2 in the default layout -- the premise of the test.
    swap_slot = next(i for i, s in enumerate(orch.toolbelt.slots)
                     if s is not None and s.name == "Swap")
    assert swap_slot >= ROW_SIZE, (
        f"Swap is at slot {swap_slot}, on row 1; this test needs a tool on "
        f"the inactive row to have anything to prove")

    engine.place_structure(Swap, 30, 30)
    engine.sync_segments()
    orch.toolbelt.clear()
    orch.toolbelt.row = 0
    step()

    put_cursor_at_cell(30, 30); step()
    press_key(glfw.KEY_Q)

    tool = orch.toolbelt.active
    assert tool is not None and tool.name == "Swap", (
        f"Q on a Swap landed on {getattr(tool, 'name', None)}")
    assert orch.toolbelt.selected == swap_slot, orch.toolbelt.selected
    assert orch.toolbelt.row == 1, (
        f"Q selected a row-2 slot but left the digits on row "
        f"{orch.toolbelt.row + 1}")
    ok("Q reached Swap on row 2 and brought the active row with it")


def test_the_raw_belt_tool_places_one_belt_per_click():
    """The blunt instrument: click places, R rotates, nothing is inferred.

    The planner reads where in a cell you pointed and derives a path, which
    is right for laying a run and wrong for fixing one cell -- two
    apparently identical clicks there produce different facings. This tool
    exists so a single belt is predictable, so the test's whole point is
    that the sub-cell position is **ignored**.
    """
    from core.entity import Belt as _Belt

    tool = select_tool("Raw belt")
    assert not tool.drag_based, "a raw belt is click-based like a structure"
    assert tool.entity_cls is _Belt, tool.entity_cls

    # Click in the far corner of a cell -- the position the planner would
    # read as "face north-east". A raw belt must ignore it and use R's facing.
    tool.direction = cell.DIR_PX
    put_cursor_at(30 + 0.95, 30 + 0.95); step()
    click()
    placed = board.entity_at(30, 30)
    assert placed is not None, "a single click should have placed a belt"
    assert placed.direction == cell.DIR_PX, (
        f"the raw belt took its facing from the cursor corner "
        f"({cell.DIR_NAMES[placed.direction]}) instead of from R")
    ok("a raw belt ignored the sub-cell position and used the tool's facing")

    # R rotates it, and the next click follows. The expected facing is
    # derived rather than spelled out, so this tests that the *tool* and the
    # *placement* agree rather than re-asserting which way R turns -- that
    # convention is core/cell.py's and is pinned there.
    want = cell.rotate_dir(cell.DIR_PX, cw=True)
    press_key(glfw.KEY_R)
    assert tool.direction == want, cell.DIR_NAMES[tool.direction]
    put_cursor_at(35 + 0.05, 35 + 0.05); step()
    click()
    second = board.entity_at(35, 35)
    assert second is not None and second.direction == want, (
        cell.DIR_NAMES[second.direction] if second else None)
    ok(f"R rotated the raw belt to {cell.DIR_NAMES[want]} and the click "
       f"followed it")

    # One click, one belt -- no run, no path planning.
    assert board.is_empty(31, 30) and board.is_empty(36, 35), (
        "a raw belt click must place exactly one cell")
    ok("each click placed exactly one belt")


def test_the_raw_belt_tool_does_not_change_belt_rotatability():
    """`Belt.ROTATABLE` stays False, and the raw tool rotates anyway.

    The flag means "a belt's facing comes from the path it is drawn along",
    which is true of the planner and is exactly what this tool exists not to
    do. Buying the raw tool's rotation by flipping a fact about the *type*
    would have made the flag lie -- so the rotation lives on the tool, and
    this pins that it does.
    """
    from core.entity import Belt as _Belt
    assert _Belt.ROTATABLE is False, (
        "a belt's facing still comes from its path, for the planner")
    tool = select_tool("Raw belt")
    before = tool.direction
    assert tool.cycle(), "the raw belt tool must rotate regardless"
    assert tool.direction != before
    ok("Belt.ROTATABLE stayed False while the raw tool still rotated")


def test_q_on_a_belt_resolves_to_the_earliest_slot():
    """Two tools place Belt, so the pipette's answer is slot order.

    A real rule rather than an accident: `select_tool_for()` is first match
    wins, searching the active row first. Worth pinning because it is the
    one place where adding a tool changed an existing gesture's outcome --
    Q on a belt could have started landing on the raw tool.
    """
    from core.entity import Belt as _Belt

    both = [(i, s) for i, s in enumerate(orch.toolbelt.slots)
            if s is not None and getattr(s, "entity_cls", None) is _Belt]
    assert len(both) >= 2, (
        f"this test needs two Belt-placing tools, found {both}")
    earliest = min(i for i, _s in both)

    engine.place_belt(30, 30, cell.DIR_PX)
    engine.sync_segments()
    orch.toolbelt.clear()
    orch.toolbelt.row = 0
    step()
    put_cursor_at_cell(30, 30); step()
    press_key(glfw.KEY_Q)

    assert orch.toolbelt.selected == earliest, (
        f"Q landed on slot {orch.toolbelt.selected}, not the earliest "
        f"Belt slot {earliest}")
    ok(f"Q on a belt resolved to slot {earliest}, the earliest Belt tool")


def test_the_belt_planner_shows_a_ghost_when_idle():
    """A drag-based tool previews while idle, so the hand is never ambiguous.

    Without this, "belt planner with no plan started" and "empty hand" are
    pixel-identical and the player cannot tell whether a click will build.
    """
    put_cursor_at_cell(40, 40); step()
    assert not engine.ghost_rows, "empty hand: nothing"

    select_tool("Belt")
    put_cursor_at_cell(40, 40); step()
    assert len(engine.ghost_rows) == 1, (
        f"the idle belt planner should ghost exactly one cell, got "
        f"{len(engine.ghost_rows)}")
    ok("the idle belt planner ghosts one cell")

    # It follows the cursor rather than sticking where it first appeared.
    put_cursor_at_cell(44, 41); step()
    assert len(engine.ghost_rows) == 1
    _g = board.entity_at(44, 41)
    assert _g is not None and _g.is_ghost, "the idle ghost should follow"
    ok("the idle ghost follows the cursor")

    # And the facing tracks the sub-cell position, which is the heading a
    # single click would actually produce.
    put_cursor_at(50.9, 50.5); step()
    assert cell.cell_dir(board.cell_word(50, 50)) == cell.DIR_PX
    put_cursor_at(50.5, 50.9); step()
    assert cell.cell_dir(board.cell_word(50, 50)) == cell.DIR_PY
    ok("the idle ghost's facing follows the pointer within the cell")


def test_underground_belts_pair_end_to_end():
    """Underground belts pair end to end."""
    _u = select_tool("Underground")
    _u.direction = cell.DIR_PX
    _u.reset()
    assert not _u.placing_exit, "a fresh underground tool lays an entrance first"

    # First click lays an entrance...
    put_cursor_at_cell(10, 40); step()
    click()
    assert _u.placing_exit, "after an entrance the tool should expect an exit"
    _ent_row = cell.cell_id(board.cell_word(10, 40))
    _ent = board.view(_ent_row)
    assert _ent.type_id == cell.TYPE_UNDER and not _ent.is_exit
    assert not _ent.paired, "an entrance alone has nothing to pair with"
    ok("first click laid an unpaired entrance")

    # ...the second lays an exit, which pairs back to it.
    put_cursor_at_cell(14, 40); step()
    click()
    _exit_row = cell.cell_id(board.cell_word(14, 40))
    _ex = board.view(_exit_row)
    assert _ex.is_exit, "second click should lay an exit"
    assert _ex.partner == _ent_row and _ent.partner == _exit_row, "ends must pair"
    assert not _u.placing_exit, "the tool should cycle back to entrance"
    ok("second click laid an exit and paired it to the entrance")

    # The pairing must reach the grid, where the renderer reads it.
    _ez = int(board.grid[40, 10, cell.CH_Z])
    _xz = int(board.grid[40, 14, cell.CH_Z])
    assert cell.is_under_paired(_ez) and cell.is_under_paired(_xz)
    assert not cell.is_under_exit(_ez) and cell.is_under_exit(_xz)
    ok("both ends publish their role and pairing into .z")

    # The tiles between stay free: that is the whole point of going under.
    for _x in range(11, 14):
        assert board.is_empty(_x, 40), f"span tile {_x} should be free"
    assert engine.place_belt(12, 40, cell.DIR_PY) is not None
    ok("the span is unobstructed; a belt crosses straight over it")

    # Beyond MAX_SPAN there is no pairing.
    engine.clear_board(); step()
    _u.reset()
    _a = engine.place_under(20, 44, cell.DIR_PX, is_exit=False)
    _b = engine.place_under(20 + Underground.MAX_SPAN + 1, 44, cell.DIR_PX,
                            is_exit=True)
    assert not _a.paired and not _b.paired, (
        f"ends {Underground.MAX_SPAN + 1} apart must not pair")
    _c = engine.place_under(20 + Underground.MAX_SPAN, 46, cell.DIR_PX,
                            is_exit=True)
    _d = engine.place_under(20, 46, cell.DIR_PX, is_exit=False)
    assert _c.paired and _d.paired, f"ends {Underground.MAX_SPAN} apart must pair"
    ok(f"pairs at {Underground.MAX_SPAN} apart, not {Underground.MAX_SPAN + 1}")

    # Opposed facings must not pair -- both ends of a run share a direction.
    engine.clear_board(); step()
    _a = engine.place_under(30, 44, cell.DIR_PX, is_exit=False)
    _b = engine.place_under(33, 44, cell.DIR_NX, is_exit=True)
    assert not _a.paired and not _b.paired, "opposed ends must not pair"
    ok("ends facing opposite ways do not pair")

    # Erasing one end must release the other, or it would point at a freed row
    # that the free list can hand to something else.
    engine.clear_board(); step()
    _a = engine.place_under(40, 44, cell.DIR_PX, is_exit=False)
    _b = engine.place_under(43, 44, cell.DIR_PX, is_exit=True)
    assert _a.paired
    engine.erase(43, 44); step()
    assert not _a.paired and _a.partner == 0, "survivor must be released"
    assert not cell.is_under_paired(int(board.grid[44, 40, cell.CH_Z])), \
        "survivor's .z still claims a partner"
    ok("erasing one end unpairs the survivor, in the row and the grid")

    # The nearest free entrance wins, so a long run chains naturally.
    engine.clear_board(); step()
    _far = engine.place_under(50, 44, cell.DIR_PX, is_exit=False)
    _near = engine.place_under(52, 44, cell.DIR_PX, is_exit=False)
    _ex2 = engine.place_under(54, 44, cell.DIR_PX, is_exit=True)
    assert _ex2.partner == _near.row, "the nearest unpaired entrance should win"
    assert not _far.paired
    ok("an exit pairs with the nearest entrance, not an older one further back")

    # Hovering a would-be exit previews the connection: the ghost claims the
    # pairing (which is what makes the shader dot the span) without touching the
    # real entrance, and shows nothing at all when out of range.
    engine.clear_board(); step()
    _u = select_tool("Underground")
    _u.direction = cell.DIR_PX
    _u.reset()
    _ent = engine.place_under(10, 50, cell.DIR_PX, is_exit=False)
    _u.placing_exit = True
    put_cursor_at_cell(14, 50); step()
    assert orch.last_plan.placements[0].under_partner == (10, 50)
    assert cell.is_under_paired(int(board.grid[50, 14, cell.CH_Z])), \
        "an in-range ghost exit must show as paired, or no span is drawn"
    assert not _ent.paired, "previewing must not pair the real entrance"
    ok("hovering an in-range exit previews the connection")

    put_cursor_at_cell(10 + Underground.MAX_SPAN + 1, 50); step()
    assert orch.last_plan.placements[0].under_partner is None
    assert not cell.is_under_paired(
        int(board.grid[50, 10 + Underground.MAX_SPAN + 1, cell.CH_Z])), \
        "an out-of-range ghost must not claim a pairing"
    ok("an out-of-range hover shows no connection")

    # Abandoning the preview must leave the entrance exactly as it was.
    engine.clear_ghosts(); step()
    assert not _ent.paired and _ent.partner == 0
    assert not cell.is_under_paired(int(board.grid[50, 10, cell.CH_Z]))
    ok("a cancelled preview leaves the real entrance untouched")

    # Cancelling mid-pair resets the tool back to laying an entrance.
    _u.placing_exit = True
    fake["keys"].add(glfw.KEY_ESCAPE); step()
    fake["keys"].discard(glfw.KEY_ESCAPE); step()
    assert not _u.placing_exit, "cancel should reset the entrance/exit step"
    ok("cancel resets the tool to laying an entrance")
    select_tool("Belt")

def test_rotating_a_structure_keeps_its_ports_on_the_outside():
    """Rotating a structure keeps its ports on the outside."""
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

def test_frustration_never_destroys_the_occupant():
    """Frustration never destroys the occupant."""
    select_tool("Belt")
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

def test_a_tool_must_not_collide_with_its_own_preview():
    """A tool must not collide with its own preview."""
    # Regression: plans were computed before the previous preview was cleared, so
    # dragging a structure one cell sideways frustrated against its own ghost.
    engine.clear_board(); step()
    select_tool("Shear")
    orch.toolbelt.active.direction = cell.DIR_PX
    for x in range(8, 18):
        put_cursor_at_cell(x, 24); step()
        assert orch.last_plan is not None and orch.last_plan.ok, (
            f"self-frustrated at x={x}: {orch.last_plan.frustrated_cells}")
    ok("dragging a Shear across 10 cells never self-frustrates")

    # The same for a 2D footprint, which overlaps its own previous position far
    # more heavily than a 1D one and so is the real test of clear-then-plan.
    # Everything here derives from FOOTPRINT, so changing a machine's shape does
    # not invalidate the test.
    # No *placeable* machine is 2D any more -- every one on the bar is 1x2 or
    # 2x1 -- so this drops a Sink builder into a slot directly rather than
    # weakening the check to a 1D footprint. Sink is still a defined type
    # that an old save can put on the board, so it is a real subject; it is
    # only absent from the toolbelt.
    engine.clear_board(); step()
    from core.builders import StructureBuilder
    _slot = len(orch.toolbelt.slots) - 1
    _saved_slot = orch.toolbelt.slots[_slot]
    orch.toolbelt.slots[_slot] = StructureBuilder(Sink)
    orch.toolbelt.select(_slot)
    _sw, _sh2 = Sink.FOOTPRINT
    assert _sw > 1 and _sh2 > 1, "this section wants a 2D footprint"
    # Restore the borrowed slot even if an assertion fires, or every later
    # scenario inherits a Sink tool where its real one should be -- the same
    # discipline the imgui and clock-phase cleanups follow.
    try:
        for x in range(8, 18):
            put_cursor_at_cell(x, 24); step()
            assert orch.last_plan is not None and orch.last_plan.ok, (
                f"Sink self-frustrated at x={x}: "
                f"{orch.last_plan.frustrated_cells}")
        click()
        assert board.live_count == 1, "a Sink is one entity"
        _ox, _oy = Sink.origin_for_cursor(17, 24, cell.DIR_PX)
        _sink_id = cell.cell_id(board.cell_word(_ox, _oy))
        for _dx in range(_sw):
            for _dy in range(_sh2):
                assert cell.cell_id(
                    board.cell_word(_ox + _dx, _oy + _dy)) == _sink_id, (_dx, _dy)
                assert cell.unpack_local(
                    int(board.grid[_oy + _dy, _ox + _dx, cell.CH_Y])) == (_dx, _dy)
        # The cursor cell is covered, and sits at the footprint's centre offset.
        assert (17, 24) in Sink.cells_for(_ox, _oy, cell.DIR_PX)
        assert cell.unpack_local(int(board.grid[24, 17, cell.CH_Y])) == \
            Sink.centre_offset(cell.DIR_PX)
        ok(f"{_sw}x{_sh2} Sink centres on the cursor, one entity, "
           f"correct offsets")
    finally:
        orch.toolbelt.slots[_slot] = _saved_slot

    engine.clear_board(); step()
    select_tool("Belt")
    put_cursor_at_cell(5, 28); step()
    click()
    for end in range(6, 20):
        put_cursor_at_cell(end, 28); step()
        assert orch.last_plan.ok, (
            f"belt self-frustrated at {end}: {orch.last_plan.frustrated_cells}")
    ok("growing a belt run never self-frustrates")
    fake["right"] = True; step(); fake["right"] = False; step()

def test_a_blocked_structure_still_previews_the_part_that_fits():
    """A blocked structure still previews the part that fits."""
    select_tool("Shear")
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
    select_tool("Belt")

def test_switching_tools_drops_an_in_flight_preview():
    """Switching tools drops an in-flight preview."""
    select_tool("Belt")
    put_cursor_at_cell(12, 12); step()
    click()
    put_cursor_at_cell(18, 12); step()
    assert len(engine.ghost_rows) > 0
    select_tool("Shear")
    assert orch.toolbelt.active.name == "Shear"
    assert orch.input.state == STATE_IDLE, "tool switch should cancel the gesture"
    belt_ghosts = [r for r in engine.ghost_rows
                   if board.is_row_alive(r)
                   and cell.cell_type(board.cell_word(*board.view(r).origin)) == cell.TYPE_BELT]
    assert not belt_ghosts, "belt preview should be gone"
    ok("tool switch cancelled the belt run")
    select_tool("Belt")

def test_save_load():
    """Save / load."""
    import os as _os
    import tempfile as _tempfile
    from core import persistence

    _savepath = _os.path.join(_tempfile.gettempdir(), "gates_test_save.npz")
    engine.clear_board(); step()
    engine.place_belt_path((2, 2), (20, 2), MODE_HORIZONTAL_FIRST)
    engine.place_structure(Shear, 10, 10, cell.DIR_PY)
    engine.place_structure(Sink, 30, 30, cell.DIR_PX)
    _ua = engine.place_under(5, 20, cell.DIR_PX, is_exit=False)
    _ub = engine.place_under(9, 20, cell.DIR_PX, is_exit=True)
    # Churn the free list so the allocator state is non-trivial.
    engine.erase(3, 2); engine.erase(5, 2); step()

    _want_grid = board.grid.copy()
    _want_ent = board.entities.copy()
    _want_alloc = (board.live_count, board._next_row, list(board._free_rows))
    _ua_row, _ub_row = _ua.row, _ub.row
    # Segment *shape*, keyed on endpoints rather than ids -- see the note on
    # the grid comparison below.
    _want_segs = {(s.cells[0], s.cells[-1], s.length)
                  for s in engine.segments.segments.values()}

    engine.save_board(_savepath)
    engine.clear_board(); step()
    assert board.live_count == 0
    _n = engine.load_board(_savepath)
    # A step so belt segments are re-derived before comparing.
    step()

    assert _n == _want_alloc[0], (_n, _want_alloc[0])
    # Channels .x/.y/.z are the persistent ones and must come back verbatim.
    # .w is excluded on purpose: it holds belt-segment membership, which is
    # derived state rebuilt from the grid, and its *ids* are arbitrary labels
    # handed out in scan order. A reload re-derives the same segmentation
    # with different ids, so comparing .w would assert on the allocator's
    # bookkeeping rather than on anything about the board. The segmentation
    # itself is checked structurally just below.
    assert np.array_equal(board.grid[:, :, :cell.CH_W],
                          _want_grid[:, :, :cell.CH_W]), \
        "grid differs after reload"
    assert np.array_equal(board.entities, _want_ent), "entity table differs"
    # The allocator is part of the state: entity ids are baked into grid cells,
    # so a wrong free list would hand out a row that is still in use.
    assert (board.live_count, board._next_row,
            list(board._free_rows)) == _want_alloc
    ok(f"save/load round trip is exact ({_n} entities, "
       f"free list {_want_alloc[2]})")

    # The segmentation itself must come back, even though its ids need not.
    # Compare the *shape* of the index: which cell runs exist, keyed by their
    # endpoints rather than by id.
    _shape = {(s.cells[0], s.cells[-1], s.length)
              for s in engine.segments.segments.values()}
    assert _shape == _want_segs, (
        f"segmentation differs after reload:\n"
        f"  before {sorted(_want_segs)}\n  after  {sorted(_shape)}")
    assert not engine.segments.audit(), engine.segments.audit()
    ok(f"belt segmentation re-derived identically ({len(_shape)} segments)")

    # Entity-level state has to survive, not just the bytes.
    _a, _b = board.view(_ua_row), board.view(_ub_row)
    assert _a.partner == _ub_row and _b.partner == _ua_row, "pairing lost"
    assert _b.is_exit and not _a.is_exit, "underground roles lost"
    ok("underground pairing and roles survive a reload")

    board.to_device()
    assert board.verify_round_trip(), "reloaded board does not reach the device"
    ok("reloaded board uploads cleanly")

    # A preview must never reach the file: ghosts belong to a gesture, not the
    # board, and would reload as entities nobody placed.
    select_tool("Belt")
    put_cursor_at_cell(40, 40); step()
    click()
    put_cursor_at_cell(46, 44); step()
    assert len(engine.ghost_rows) > 0
    engine.save_board(_savepath)
    assert not engine.ghost_rows, "save should clear the preview first"
    _saved = np.load(_savepath)
    _gs = cell.ghost_state_arr(_saved["grid"][:, :, cell.CH_Z])
    assert int((_gs != cell.GHOST_NONE).sum()) == 0, "ghosts leaked into the file"
    _saved.close()
    ok("saving clears the preview; no ghost cells in the file")

    # And persistence.save() refuses outright if ghosts are somehow still set.
    _bb2 = BeltBuilder()
    engine.preview(_bb2, (5, 50), (12, 50))
    try:
        persistence.save(board, _savepath)
        raise AssertionError("save should refuse while ghosts are set")
    except persistence.SaveError:
        pass
    engine.clear_ghosts()
    ok("persistence.save refuses to write a board with ghosts")

    # A file for a different board size is rejected rather than half-applied.
    from core.gameboard import Gameboard
    _other = Gameboard(32, 32)
    try:
        persistence.load(_other, _savepath)
        raise AssertionError("size mismatch should be rejected")
    except persistence.SaveError:
        pass
    ok("a save for a different board size is rejected")

    _os.remove(_savepath)
    engine.clear_board(); step()
    # Leave the Belt tool active: later sections assume a drag-based tool.
    select_tool("Belt")

def test_device_round_trip_after_all_that():
    """A populated board survives a full upload and readback."""
    assert board.verify_round_trip(), "grid round trip mismatch"
    ok("to_device/from_device match")
    raw = np.frombuffer(board.entity_buffer.read(), dtype=np.float32).reshape(board.capacity, -1)
    assert np.array_equal(raw, board.entities), "SSBO diverged"
    ok("entity SSBO matches CPU array")

def test_partial_upload_really_is_partial():
    """Partial upload really is partial."""
    select_tool("Belt")
    board.to_device(force=True)
    put_cursor_at_cell(50, 50); step()
    click()
    put_cursor_at_cell(52, 50); step()
    cells_up, rows_up = orch.ui.last_upload
    print(f"  3-cell ghost preview uploaded {cells_up} cells (board is {board.width*board.height})")
    assert 0 < cells_up < 500, cells_up
    ok("dirty-rect upload stayed small")
    fake["right"] = True; step(); fake["right"] = False; step()

def test_left_drag_pans_instead_of_building():
    """Left-drag pans instead of building."""
    select_tool("Belt")
    put_cursor_at_cell(30, 30); step()
    c_before = cam.center.copy()
    live_before = board.live_count
    built_before = cells_of_type(cell.TYPE_BELT) - ghost_cell_count()
    drag_px(120.0, 0.0)
    assert orch.input.state == STATE_IDLE, orch.input.state
    assert board.live_count == live_before, "a drag must not build"
    # No *planned run* survives -- but the one-cell idle preview does, since
    # the gesture ends back in IDLE with the cursor still over the board.
    assert len(engine.ghost_rows) <= 1, (
        f"a drag must leave no planned run, got {len(engine.ghost_rows)} ghosts")
    assert cells_of_type(cell.TYPE_BELT) - ghost_cell_count() == built_before, \
        "a drag must not build"
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
    press_key(glfw.KEY_ESCAPE)

def test_scroll_zooms_at_the_cursor():
    """Scroll zooms at the cursor."""
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

def test_sub_cell_aiming_through_the_full_stack():
    """Sub-cell aiming through the full stack."""
    select_tool("Belt")

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


def test_the_idle_ghost_agrees_with_the_quadrant_all_the_way_round():
    """Trace a circle inside one cell: the ghost must never disagree.

    The four axis-aligned samples above are exactly the points where the old
    threshold-box rule happened to agree with the quadrant split, which is
    how a bug covering two thirds of the cell survived them. Sweeping the
    whole circle is the test that catches it: at 24 of 36 sampled angles the
    ghost faced the wrong way, most visibly in the middle of the north-east
    arc, where "north" rendered as east.

    Checks the *ghost* specifically, because that is the feedback the player
    reads before committing -- a click that builds something other than what
    was previewed is the actual complaint.
    """
    import math
    from core.builders import quadrant_dir

    select_tool("Belt")
    wrong = []
    for deg in range(0, 360, 10):
        a = math.radians(deg)
        # 0.45 from the centre: well clear of the diagonals, so every sample
        # is unambiguously inside one quadrant.
        fx, fy = 0.5 + 0.45 * math.cos(a), 0.5 + 0.45 * math.sin(a)
        engine.clear_board()
        step()
        sx, sy = cam.world_to_screen(20 + fx, 20 + fy, (W, H))
        fake["cursor"] = (float(sx), float(sy))
        step()

        want = quadrant_dir((fx, fy))
        plan = orch.last_plan
        assert plan is not None and plan.placements, (
            f"no idle ghost at {deg} degrees")
        got = plan.placements[0].direction
        if got != want:
            wrong.append((deg, cell.DIR_NAMES[want], cell.DIR_NAMES[got]))

    assert not wrong, (
        f"the idle ghost disagreed with quadrant_dir() at {len(wrong)} of 36 "
        f"angles: {wrong[:6]}")
    ok("the belt ghost matched the quadrant at all 36 angles round the cell")


def test_a_single_cell_click_is_not_second_guessed_by_the_exit_turn():
    """What the ghost previews is what a click builds, at every angle.

    The exit-turn rule answers a different question -- "should this run's
    final belt form a corner" -- and a one-cell run has no travel direction
    to deflect from. Letting it run anyway overrode a correct quadrant
    heading, so the committed belt could differ from the previewed ghost.
    """
    import math
    from core.builders import quadrant_dir

    select_tool("Belt")
    for deg in range(0, 360, 30):
        a = math.radians(deg)
        fx, fy = 0.5 + 0.45 * math.cos(a), 0.5 + 0.45 * math.sin(a)
        engine.clear_board()
        step()
        sx, sy = cam.world_to_screen(20 + fx, 20 + fy, (W, H))
        fake["cursor"] = (float(sx), float(sy))
        step()
        ghost = orch.last_plan.placements[0].direction
        click()      # press+release at one spot -> PLANNING
        click()      # again -> commit the single cell
        assert not board.is_empty(20, 20), f"nothing built at {deg} degrees"
        built = cell.cell_dir(board.cell_word(20, 20))
        want = quadrant_dir((fx, fy))
        assert built == want == ghost, (
            f"{deg} degrees: quadrant says {cell.DIR_NAMES[want]}, ghost "
            f"showed {cell.DIR_NAMES[ghost]}, built {cell.DIR_NAMES[built]}")
    ok("a single-cell click built exactly what its ghost previewed")


def test_straight_stays_sticky_on_a_multi_cell_run():
    """The turn threshold survives the shared quadrant rule.

    Sharing the quadrant function must not make a run turn the instant the
    pointer crosses a diagonal: with a heading already established, a corner
    needs the pointer genuinely out toward that edge. Pulling *backwards*
    must not turn at all, or the final belt would reverse into its own run.
    """
    select_tool("Belt")

    def run_ending_at_frac(fx, fy):
        engine.clear_board()
        step()
        sx, sy = cam.world_to_screen(5 + 0.5, 15 + 0.5, (W, H))
        fake["cursor"] = (float(sx), float(sy))
        step()
        click()
        sx, sy = cam.world_to_screen(11 + fx, 15 + fy, (W, H))
        fake["cursor"] = (float(sx), float(sy))
        step()
        click()
        return cell.cell_dir(board.cell_word(11, 15))

    # Inside the threshold: straight on, even though the quadrant is +Y.
    assert run_ending_at_frac(0.5, 0.58) == cell.DIR_PX, "turned too eagerly"
    # Past it: a corner.
    assert run_ending_at_frac(0.5, 0.75) == cell.DIR_PY, "should have turned"
    assert run_ending_at_frac(0.5, 0.25) == cell.DIR_NY, "should have turned"
    # Backwards along the run: never a turn, whatever the quadrant says.
    assert run_ending_at_frac(0.1, 0.5) == cell.DIR_PX, (
        "pulling backwards reversed the final belt into its own run")
    ok("straight stayed sticky; only sideways deflection past 0.6 turned")

def test_camera_pan_zoom():
    """Camera pan/zoom."""
    c0 = cam.center.copy(); z0 = cam.zoom
    fake["keys"].add(glfw.KEY_D); step(10); fake["keys"].discard(glfw.KEY_D)
    assert cam.center[0] > c0[0], (c0, cam.center)
    fake["keys"].add(glfw.KEY_W); step(10); fake["keys"].discard(glfw.KEY_W)
    assert cam.center[1] > c0[1]
    ok(f"WASD panned to {cam.center.round(1)}")

    # Q and E no longer zoom -- scroll is the only zoom binding, and Q is the
    # pipette. Holding either must leave the zoom alone.
    z1 = cam.zoom
    fake["keys"].add(glfw.KEY_E); step(10); fake["keys"].discard(glfw.KEY_E)
    fake["keys"].add(glfw.KEY_Q); step(10); fake["keys"].discard(glfw.KEY_Q)
    assert cam.zoom == z1, (
        f"Q/E must not zoom any more: {z1} -> {cam.zoom}")
    ok("Q and E no longer touch the zoom")


def test_ctrl_suppresses_the_camera_pan():
    """A ctrl chord must not also drag the board.

    `D` is both pan-east and half the ctrl+D erase chord, so entering erase
    mode used to nudge the camera east for as long as the chord was held --
    small, but it moved the board out from under a selection the player was
    about to drag, which is the worst possible moment for it.

    Asserted for every WASD key rather than just `D`: no ctrl chord in the
    game means "pan", so the whole block is suppressed, and singling out `D`
    would leave the others panning for no stated reason.
    """
    for key, name in ((glfw.KEY_D, "D"), (glfw.KEY_A, "A"),
                      (glfw.KEY_W, "W"), (glfw.KEY_S, "S")):
        # The bare key still pans, or the suppression would be hiding a
        # broken binding rather than a fixed one.
        before = cam.center.copy()
        fake["keys"].add(key)
        step(4)
        fake["keys"].discard(key)
        step()
        assert not (cam.center == before).all(), (
            f"bare {name} should still pan the camera")

        before = cam.center.copy()
        fake["keys"].add(glfw.KEY_LEFT_CONTROL)
        fake["keys"].add(key)
        step(4)
        fake["keys"].discard(key)
        fake["keys"].discard(glfw.KEY_LEFT_CONTROL)
        step()
        assert (cam.center == before).all(), (
            f"ctrl+{name} must not pan: {before} -> {cam.center}")
        # ctrl+D enters erase mode; leave it so the next key starts clean.
        if orch.clipboard.active:
            press_key(glfw.KEY_ESCAPE)
    ok("ctrl suppressed the WASD pan, so ctrl+D does not drag the board")


def test_imgui_capture_gating():
    """Imgui capture gating."""
    io = imgui.get_io()
    io.want_capture_mouse = True
    try:
        put_cursor_at_cell(8, 8)
        click()
        assert orch.input.state == STATE_IDLE, \
            "click over UI should not start a plan"
        assert board.live_count == 0
    finally:
        # Restore even if the assertion fails, or every later scenario would
        # silently have its mouse input swallowed.
        io.want_capture_mouse = False
    ok("clicks over imgui ignored")
