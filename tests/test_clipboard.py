"""Copy and paste modes, driven through the real input stack.

core/stamp.py's self-check covers the transform algebra and
tests/test_stamp.py covers what it preserves. These are the tests for the
*interaction*: the mode machine, what each way out of a mode does, and the
gestures -- all of it through the fake mouse and keyboard, because the thing
most likely to break is the wiring between the layers rather than the
geometry.

One habit matters throughout: **ghosts occupy real cells**, so
`board.is_empty()` says "taken" for a preview. `real_cells_at()` is the
question these tests actually want, and using `is_empty` instead made an
early version of the all-or-nothing test pass for entirely the wrong reason.
"""

import glfw

from core import cell
from core.clipboard import (MODE_COPY, MODE_OFF, MODE_PASTE, Clipboard,
                            capture)
from core.entity import Sink, Source, Underground
from core.stamp import Stamp

from tests import harness
from tests.harness import (board, click, ctrl_key, drag_cells, drag_px, engine,
                           fake, ok, orch, cam, press_key, put_cursor_at_cell,
                           real_cells_at, select_tool, step)


def _copy_a_run(a=(10, 10), b=(14, 10), sel=None):
    """Build a belt run and copy it, leaving us in paste mode."""
    select_tool("Belt")
    engine.place_belt_path(a, b)
    engine.sync_segments()
    sel = sel or ((a[0] - 1, a[1] - 1), (b[0] + 1, b[1] + 1))
    ctrl_key(glfw.KEY_C)
    drag_cells(*sel)
    return orch.clipboard.stamp


def test_input_mode_constants_match_the_clipboard():
    """The input layer mirrors the mode values rather than importing them."""
    from ui import input_manager as im
    assert im._MODE_OFF == MODE_OFF
    assert im._MODE_COPY == MODE_COPY
    assert im._MODE_PASTE == MODE_PASTE
    ok("input_manager's mode constants match core/clipboard.py")


def test_ctrl_c_enters_copy_and_stashes_the_tool():
    """Copy mode is an override, not a slot: the held tool comes back."""
    select_tool("Shear")
    assert orch.clipboard.mode == MODE_OFF
    ctrl_key(glfw.KEY_C)
    assert orch.clipboard.mode == MODE_COPY, orch.clipboard.mode
    # The tool is stashed, not lost -- and not still active either.
    assert orch.clipboard._stashed_slot is not None
    ok("ctrl+C entered copy mode with the Shear stashed")

    # A plain click (no drag) cancels and hands the tool back.
    put_cursor_at_cell(50, 50)
    step()
    click()
    assert orch.clipboard.mode == MODE_OFF, orch.clipboard.mode
    assert orch.toolbelt.active is not None
    assert orch.toolbelt.active.name == "Shear", orch.toolbelt.active.name
    ok("a plain click left copy mode and restored the Shear")


def test_bare_c_and_v_do_nothing():
    """Only ctrl+C / ctrl+V enter the modes, so C and V stay free."""
    select_tool("Belt")
    press_key(glfw.KEY_C)
    assert orch.clipboard.mode == MODE_OFF, "bare C must not copy"
    press_key(glfw.KEY_V)
    assert orch.clipboard.mode == MODE_OFF, "bare V must not paste"
    ok("bare C and V are not bound")


def test_ctrl_v_needs_something_copied():
    """There is no empty paste mode to be in."""
    select_tool("Belt")
    assert not orch.clipboard.history
    ctrl_key(glfw.KEY_V)
    assert orch.clipboard.mode == MODE_OFF, (
        "paste mode with nothing copied is a dead end")
    assert orch.toolbelt.active.name == "Belt"
    ok("ctrl+V with an empty history does nothing")


def test_a_selection_drag_captures_and_enters_paste():
    """Copy's whole gesture: drag a box, get a stamp, land in paste mode."""
    select_tool("Belt")
    engine.place_belt_path((10, 10), (14, 10))
    engine.sync_segments()
    ctrl_key(glfw.KEY_C)

    # The rectangle is live while dragging.
    put_cursor_at_cell(9, 9)
    step()
    fake["left"] = True
    step()
    put_cursor_at_cell(15, 11)
    step()
    assert orch.select_rect is not None, "no rubber band during the drag"
    assert orch.input.state == 5, orch.input.state     # STATE_SELECTING
    fake["left"] = False
    step()

    assert orch.select_rect is None, "the band should clear on release"
    assert orch.clipboard.mode == MODE_PASTE, orch.clipboard.mode
    s = orch.clipboard.stamp
    assert s is not None and len(s) == 5, s
    assert s.bounds() == (0, 0, 5, 1), s.bounds()
    ok(f"a selection drag captured {len(s)} belts and entered paste mode")


def test_an_empty_selection_returns_to_the_tool():
    """Nothing caught means nothing to paste, so give the tool back."""
    select_tool("Shear")
    ctrl_key(glfw.KEY_C)
    drag_cells((60, 60), (64, 64))        # bare ground
    assert orch.clipboard.mode == MODE_OFF, orch.clipboard.mode
    assert orch.toolbelt.active is not None
    assert orch.toolbelt.active.name == "Shear"
    assert not orch.clipboard.history, "nothing should have been pushed"
    ok("an empty selection restored the tool instead of entering paste")


def test_paste_centres_the_stamp_on_the_cursor():
    """A long copied run straddles the pointer, middle cell under it."""
    _copy_a_run((10, 10), (14, 10))
    put_cursor_at_cell(30, 30)
    step()
    click()
    got = real_cells_at(30, range(25, 36))
    assert got == [28, 29, 30, 31, 32], got
    dirs = {cell.DIR_NAMES[cell.cell_dir(board.cell_word(x, 30))] for x in got}
    assert dirs == {"+X"}, dirs
    ok(f"a 5-cell run pasted centred on the cursor: {got}")


def test_paste_is_all_or_nothing_but_shift_places_what_fits():
    """The policy is per *click*, read from the modifier at that moment."""
    _copy_a_run((10, 10), (14, 10))
    engine.place_belt(31, 30, cell.DIR_PY)       # one cell in the way
    engine.sync_segments()

    put_cursor_at_cell(30, 30)
    step()
    assert orch.last_plan is not None and not orch.last_plan.ok
    assert orch.last_plan.all_or_nothing
    click()
    # Only the obstacle is real: a blocked all-or-nothing paste builds
    # nothing. (is_empty() would also see the ghosts -- see the module note.)
    assert real_cells_at(30, range(25, 36)) == [31], \
        real_cells_at(30, range(25, 36))
    ok("a plain click over an obstacle pasted nothing at all")

    # Shift re-plans as partial, and the preview must show that policy.
    fake["keys"].add(glfw.KEY_LEFT_SHIFT)
    try:
        put_cursor_at_cell(30, 30)
        step()
        assert orch.paste_tool.partial, "shift should arm a partial paste"
        assert not orch.last_plan.all_or_nothing, (
            "the previewed plan must use the policy the click will use")
        click()
    finally:
        fake["keys"].discard(glfw.KEY_LEFT_SHIFT)
        step()
    got = real_cells_at(30, range(25, 36))
    assert got == [28, 29, 30, 31, 32], got
    ok(f"shift+click placed the 4 that fit around the obstacle: {got}")


def test_r_and_f_transform_the_stamp_not_the_tool():
    """In paste mode the keys act on the stamp, which outlives a plan."""
    s = _copy_a_run((10, 10), (14, 10))
    assert s.bounds() == (0, 0, 5, 1), s.bounds()

    press_key(glfw.KEY_R)
    rotated = orch.clipboard.stamp
    assert rotated is not s, "R should replace the active stamp"
    assert rotated.bounds() == (0, 0, 1, 5), rotated.bounds()
    assert orch.paste_tool.stamp is rotated, "the tool must follow the stamp"
    put_cursor_at_cell(30, 30)
    step()
    click()
    col = [y for y in range(25, 36)
           if not board.is_empty(30, y)
           and not cell.is_ghost(board.grid[y, 30, cell.CH_Z])]
    assert col == [28, 29, 30, 31, 32], col
    dirs = {cell.DIR_NAMES[cell.cell_dir(board.cell_word(30, y))] for y in col}
    assert dirs == {"+Y"}, dirs
    ok(f"R rotated the stamp; it pasted as a vertical run {col}")

    # F mirrors: an eastward run becomes westward.
    _copy_a_run((10, 20), (14, 20), sel=((9, 19), (15, 21)))
    press_key(glfw.KEY_F)
    put_cursor_at_cell(40, 40)
    step()
    click()
    row = real_cells_at(40, range(35, 46))
    dirs = {cell.DIR_NAMES[cell.cell_dir(board.cell_word(x, 40))] for x in row}
    assert dirs == {"-X"}, dirs
    ok(f"F mirrored the stamp; the run pasted facing {dirs}")


def test_right_press_and_q_leave_paste_differently():
    """Right-press hands the tool back; Q empties the hand."""
    _copy_a_run()
    assert orch.clipboard.mode == MODE_PASTE
    fake["right"] = True
    step()
    fake["right"] = False
    step()
    assert orch.clipboard.mode == MODE_OFF, orch.clipboard.mode
    assert orch.toolbelt.active is not None, "right-press should restore"
    assert orch.toolbelt.active.name == "Belt"
    ok("right-press left paste mode and restored the Belt tool")

    ctrl_key(glfw.KEY_V)           # back in, history still has the stamp
    assert orch.clipboard.mode == MODE_PASTE
    press_key(glfw.KEY_Q)
    assert orch.clipboard.mode == MODE_OFF, orch.clipboard.mode
    assert orch.toolbelt.empty_handed, (
        "Q must empty the hand rather than restore the stash")
    ok("Q left paste mode holding nothing")


def test_esc_leaves_either_mode():
    """Esc is the keyboard way out, and restores like a right-press."""
    select_tool("Belt")
    ctrl_key(glfw.KEY_C)
    press_key(glfw.KEY_ESCAPE)
    assert orch.clipboard.mode == MODE_OFF
    assert orch.toolbelt.active.name == "Belt"
    _copy_a_run()
    press_key(glfw.KEY_ESCAPE)
    assert orch.clipboard.mode == MODE_OFF
    ok("Esc left both modes and restored the tool")


def test_copy_mode_has_no_camera_pan():
    """A left-drag is the selection, so the camera cannot have it."""
    select_tool("Belt")
    ctrl_key(glfw.KEY_C)
    put_cursor_at_cell(30, 30)
    step()
    before = cam.center.copy()
    fake["left"] = True
    step()
    for _ in range(4):
        fake["cursor"] = (fake["cursor"][0] + 30.0, fake["cursor"][1])
        step()
    moved = abs(cam.center[0] - before[0])
    rect = orch.select_rect
    fake["left"] = False
    step()
    assert moved < 1e-6, f"copy mode must not pan, moved {moved}"
    assert rect is not None, "the drag should have been a selection"
    ok("copy mode drew a selection and left the camera alone")


def test_paste_mode_still_pans():
    """Paste keeps left-drag for the camera -- the stamp needs no drag."""
    _copy_a_run()
    put_cursor_at_cell(30, 30)
    step()
    before = cam.center.copy()
    drag_px(120.0, 0.0)
    assert abs(cam.center[0] - before[0]) > 1.0, (
        f"paste mode should still pan: {before} -> {cam.center}")
    assert orch.clipboard.mode == MODE_PASTE, "panning must not end the mode"
    ok("paste mode panned the camera and stayed in the mode")


def test_shift_scroll_walks_the_stamp_history():
    """Factorio's history walk, and it must not also zoom."""
    first = _copy_a_run((10, 10), (12, 10), sel=((9, 9), (13, 11)))
    second = _copy_a_run((20, 20), (25, 20), sel=((19, 19), (26, 21)))
    assert len(orch.clipboard.history) == 2
    assert orch.clipboard.stamp is second, "newest is active"

    fake["keys"].add(glfw.KEY_LEFT_SHIFT)
    try:
        zoom_before = cam.zoom
        orch.input._scroll_accum = -1.0
        step()
        assert orch.clipboard.stamp is first, "scroll down goes older"
        assert orch.paste_tool.stamp is first, "the tool must follow"
        assert cam.zoom == zoom_before, "shift+scroll must not zoom in paste"
        orch.input._scroll_accum = 1.0
        step()
        assert orch.clipboard.stamp is second, "scroll up goes newer"
    finally:
        fake["keys"].discard(glfw.KEY_LEFT_SHIFT)
        step()

    # Without shift, scroll is still the zoom.
    zoom_before = cam.zoom
    orch.input._scroll_accum = 1.0
    step()
    assert cam.zoom != zoom_before, "plain scroll should still zoom"
    ok("shift+scroll walked the history; plain scroll still zooms")


def test_a_copied_chain_still_runs_after_pasting():
    """The end-to-end claim: paste a working machine group, it works.

    Ports lining up and material actually flowing are different things, and
    this asks for the second.
    """
    engine.place_structure(Source, 10, 10)
    for x in range(12, 17):
        engine.place_belt(x, 10, cell.DIR_PX)
    engine.place_structure(Sink, 17, 10)
    engine.sync_segments()

    ctrl_key(glfw.KEY_C)
    drag_cells((9, 9), (19, 12))
    s = orch.clipboard.stamp
    assert len(s) == 7, f"expected Source + 5 belts + Sink, got {len(s)}"

    put_cursor_at_cell(40, 40)
    step()
    click()
    engine.sync_segments()
    for k in engine.gates.stats:
        engine.gates.stats[k] = 0
    for _ in range(25):
        engine.clock.request_step()
        engine.clock.advance(0.0)
    assert engine.gates.stats["consumed"] > 0, (
        f"the pasted chain moved nothing: {engine.gates.stats}")
    assert not engine.segments.audit()
    ok(f"a pasted Source->belt->Sink chain moved "
       f"{engine.gates.stats['consumed']} items")


def test_a_mirrored_chain_still_runs_after_pasting():
    """Same, through the flip -- the case the port algebra exists for."""
    engine.place_structure(Source, 10, 10)
    for x in range(12, 17):
        engine.place_belt(x, 10, cell.DIR_PX)
    engine.place_structure(Sink, 17, 10)
    engine.sync_segments()
    ctrl_key(glfw.KEY_C)
    drag_cells((9, 9), (19, 12))
    press_key(glfw.KEY_F)

    put_cursor_at_cell(40, 40)
    step()
    click()
    engine.sync_segments()
    for k in engine.gates.stats:
        engine.gates.stats[k] = 0
    for _ in range(25):
        engine.clock.request_step()
        engine.clock.advance(0.0)
    assert engine.gates.stats["consumed"] > 0, (
        f"the mirrored pasted chain moved nothing: {engine.gates.stats}")
    ok(f"a mirrored pasted chain moved "
       f"{engine.gates.stats['consumed']} items")


def test_a_copied_underground_pair_rejoins_itself():
    """A pasted pair must join to each other, not to a nearby stranger."""
    select_tool("Underground")
    put_cursor_at_cell(10, 20)
    step()
    click()
    put_cursor_at_cell(14, 20)
    step()
    click()
    a, b = board.entity_at(10, 20), board.entity_at(14, 20)
    assert a is not None and b is not None, "setup: a pair at y=20"
    assert a.partner == b.row and b.partner == a.row, "setup: paired"

    ctrl_key(glfw.KEY_C)
    drag_cells((9, 19), (15, 21))
    s = orch.clipboard.stamp
    assert len(s) == 2, len(s)
    # Pairing is recorded as stamp-local indices, so it survives the copy.
    assert sorted(e.partner for e in s.entries) == [0, 1], \
        [e.partner for e in s.entries]

    put_cursor_at_cell(40, 40)
    step()
    click()
    pa, pb = board.entity_at(38, 40), board.entity_at(42, 40)
    assert pa is not None and pb is not None, "both ends should be placed"
    assert pa.partner == pb.row and pb.partner == pa.row, (
        "the pasted pair should be joined to each other")
    assert (pa.is_exit, pb.is_exit) == (False, True), (
        f"roles lost: {pa.is_exit}, {pb.is_exit}")
    ok("a copied underground pair pasted and rejoined itself, roles intact")


def test_a_pasted_pair_ignores_a_nearby_stranger():
    """The intra-stamp link beats find_under_partner's nearest-candidate."""
    select_tool("Underground")
    put_cursor_at_cell(10, 20)
    step()
    click()
    put_cursor_at_cell(14, 20)
    step()
    click()
    ctrl_key(glfw.KEY_C)
    drag_cells((9, 19), (15, 21))

    # An unpaired entrance sitting right where the pasted exit will land,
    # one cell upstream -- exactly what a nearest-candidate scan would grab.
    orch.clipboard.mode = MODE_OFF
    select_tool("Underground")
    put_cursor_at_cell(41, 40)
    step()
    click()
    stranger = board.entity_at(41, 40)
    assert stranger is not None and not stranger.paired

    ctrl_key(glfw.KEY_V)
    put_cursor_at_cell(40, 40)
    step()
    fake["keys"].add(glfw.KEY_LEFT_SHIFT)       # place what fits
    try:
        click()
    finally:
        fake["keys"].discard(glfw.KEY_LEFT_SHIFT)
        step()

    pa, pb = board.entity_at(38, 40), board.entity_at(42, 40)
    if pa is not None and pb is not None:
        assert pa.partner == pb.row and pb.partner == pa.row, (
            "the pasted pair joined the stranger instead of itself")
        assert not stranger.paired, "the stranger should be untouched"
        ok("a pasted pair ignored an unpaired stranger at the paste site")
    else:
        ok("the paste was refused near the stranger (also acceptable)")


def test_capture_takes_whole_entities_touched_by_the_box():
    """Touching, not containing: a sloppy drag over a machine gets it all."""
    engine.place_structure(Sink, 20, 20)        # 2x2 over (20,20)-(21,21)
    engine.sync_segments()
    # A box covering only one of its four cells.
    s = capture(board, 20, 20, 20, 20)
    assert len(s) == 1, len(s)
    assert s.bounds() == (0, 0, 2, 2), (
        f"the whole 2x2 should come along, got {s.bounds()}")
    ok("a box over one cell of a 2x2 captured the whole entity")


def test_capture_skips_ghosts():
    """A preview is not board state and must not be copied."""
    select_tool("Belt")
    put_cursor_at_cell(30, 30)
    step()
    assert engine.ghost_rows, "setup: the idle preview should be showing"
    s = capture(board, 28, 28, 32, 32)
    assert not s, f"ghosts leaked into a capture: {s}"
    ok("capture ignored the preview ghosts")


def test_clipboard_stash_survives_copy_to_paste():
    """A capture switches copy -> paste without losing the original tool."""
    cb = Clipboard()
    cb.enter_copy(3)
    assert cb._stashed_slot == 3
    cb.adopt(Stamp())                 # the mode switch a capture performs
    assert cb.mode == MODE_PASTE
    assert cb._stashed_slot == 3, "the stash must survive the mode switch"
    slot, had = cb.exit_to_stash()
    assert (slot, had) == (3, True)
    ok("the stashed slot survived copy -> paste -> exit")


def test_an_empty_hand_is_stashed_as_empty():
    """Restoring must give back an empty hand, not slot 0."""
    assert orch.toolbelt.empty_handed
    engine.place_belt_path((10, 10), (12, 10))
    engine.sync_segments()
    ctrl_key(glfw.KEY_C)
    drag_cells((9, 9), (13, 11))
    assert orch.clipboard.mode == MODE_PASTE
    fake["right"] = True
    step()
    fake["right"] = False
    step()
    assert orch.toolbelt.empty_handed, (
        f"an empty hand must be restored as empty, got slot "
        f"{orch.toolbelt.selected}")
    ok("an empty hand was stashed and restored as empty")
