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
from core.clipboard import (MODE_COPY, MODE_ERASE, MODE_OFF, MODE_PASTE,
                            Clipboard, capture)
from core.entity import Shear, Sink, Source, Underground
from core.stamp import Stamp

from tests import harness
from tests.harness import (board, click, ctrl_key, drag_cells, drag_px,
                           engine, enter_cut_mode, enter_erase_mode, fake, ok,
                           orch, cam, press_key, put_cursor_at_cell,
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
    assert im._MODE_ERASE == MODE_ERASE
    # The input layer's "this mode drags a rectangle" set must agree with the
    # clipboard's own answer, or a mode would get copy's gesture in one layer
    # and not the other.
    cb = Clipboard()
    for mode in (MODE_OFF, MODE_COPY, MODE_PASTE, MODE_ERASE):
        cb.mode = mode
        assert cb.selecting == (mode in im._MODE_SELECTS), mode
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


# --- erase mode ------------------------------------------------------------
# Erase reuses copy's whole selection gesture and differs only in what the
# release does, so these tests concentrate on that difference: what gets
# deleted, and that the mode survives to be used again.


def _row_survivors(y, xs):
    """Cells in a row still holding a *real* entity.

    `real_cells_at()` rather than `is_empty()`: a selected tool previews a
    ghost under the cursor, ghosts occupy real cells, and `is_empty()` counts
    them as taken. Same trap this module's docstring warns about -- and the
    reason these tests count cells rather than `board.live_count`, which
    includes the preview's row.
    """
    return real_cells_at(y, xs)


def test_x_enters_erase_mode_and_stashes_the_tool():
    """Bare X is erase mode; like copy, it gives the held tool back."""
    select_tool("Shear")
    assert orch.clipboard.mode == MODE_OFF
    enter_erase_mode()
    assert orch.clipboard.mode == MODE_ERASE, orch.clipboard.mode
    assert not orch.clipboard.cutting, "bare X is not a cut"
    assert orch.clipboard._has_stash
    ok("X entered erase mode with the Shear stashed")

    # A plain click does nothing to the board and leaves the mode. Erasing on
    # a stray click is too easy to do by accident to be worth the shortcut.
    engine.place_belt(40, 40)
    engine.sync_segments()
    put_cursor_at_cell(40, 40)
    step()
    click()
    assert not board.is_empty(40, 40), "a bare click must not erase"
    assert orch.clipboard.mode == MODE_OFF
    assert orch.toolbelt.active.name == "Shear", orch.toolbelt.active.name
    ok("a plain click erased nothing and restored the Shear")


def test_an_erase_drag_deletes_what_the_rectangle_touched():
    """The rectangle's cells go, and nothing outside it does."""
    select_tool("Belt")
    engine.place_belt_path((10, 10), (16, 10))
    engine.place_belt_path((10, 14), (16, 14))
    engine.sync_segments()
    assert len(_row_survivors(10, range(10, 17))) == 7

    enter_erase_mode()
    drag_cells((11, 9), (13, 11))

    # The rectangle spans x=11..13, y=9..11, so the three cells of row 10
    # inside it go and the rest of the run stays.
    assert _row_survivors(10, range(10, 17)) == [10, 14, 15, 16], (
        _row_survivors(10, range(10, 17)))
    # The untouched row is the control: a rectangle erase must not be a
    # whole-board operation.
    assert len(_row_survivors(14, range(10, 17))) == 7
    ok("the rectangle's cells were erased and the next row left alone")


def test_erasing_a_rectangle_stays_in_erase_mode():
    """Erasing is a repeated stroke, not a one-shot that ends the mode.

    This is the one real difference from copy mode, whose release hands its
    rectangle over and leaves. Pinned because sharing the gesture machine
    makes it cheap to accidentally share the exit too.
    """
    select_tool("Belt")
    engine.place_belt_path((10, 10), (16, 10))
    engine.sync_segments()
    enter_erase_mode()

    drag_cells((11, 9), (12, 11))
    assert orch.clipboard.mode == MODE_ERASE, (
        "the first stroke must not end the mode")
    # A second stroke with no re-entry, which is the point.
    drag_cells((14, 9), (16, 11))
    assert orch.clipboard.mode == MODE_ERASE
    # Two rectangles: x=11..12 then x=14..16. The cells between and outside
    # them survive, which is also what says the second stroke really erased
    # rather than the first one having taken the whole row.
    assert _row_survivors(10, range(10, 17)) == [10, 13], (
        _row_survivors(10, range(10, 17)))
    assert orch.select_rect is None, "the rectangle should clear on release"
    ok("two strokes erased in one visit to the mode")


def test_erase_mode_leaves_no_stamp_behind():
    """A plain erase is a deletion, not a cut: nothing reaches the history."""
    engine.place_belt_path((10, 10), (14, 10))
    engine.sync_segments()
    enter_erase_mode()
    drag_cells((9, 9), (15, 11))
    assert _row_survivors(10, range(9, 16)) == [], (
        _row_survivors(10, range(9, 16)))
    assert not orch.clipboard.history, "a plain erase must not copy"
    assert orch.clipboard.mode == MODE_ERASE
    ok("a plain erase deleted without pushing a stamp")


def test_escape_and_right_press_leave_erase_mode():
    """Both of copy mode's aborts work here too, since the machine is shared."""
    select_tool("Belt")
    enter_erase_mode()
    press_key(glfw.KEY_ESCAPE)
    assert orch.clipboard.mode == MODE_OFF, orch.clipboard.mode
    assert orch.toolbelt.active.name == "Belt"

    enter_erase_mode()
    assert orch.clipboard.mode == MODE_ERASE
    fake["right"] = True
    step()
    fake["right"] = False
    step()
    assert orch.clipboard.mode == MODE_OFF, orch.clipboard.mode
    ok("Esc and a right-press both left erase mode")


def test_erase_mode_takes_a_whole_structure_from_any_cell():
    """Touching, not containing -- the rule capture() uses.

    `Engine.erase()` removes a multi-cell entity from any one of its cells,
    so a rectangle clipping one corner of a Sink takes the whole machine.
    """
    sink_cells = [(20, 20), (21, 20), (20, 21), (21, 21)]
    engine.place_structure(Sink, 20, 20)       # 2x2, cells (20,20)..(21,21)
    engine.sync_segments()
    assert all(not board.is_empty(*c) for c in sink_cells), "setup"

    enter_erase_mode()
    # A rectangle covering only the top-right cell of the four. It has to be
    # a real drag: a same-cell "drag" never passes DRAG_THRESHOLD_PX and so
    # is a click, which deliberately erases nothing.
    drag_cells((21, 21), (23, 23))
    assert all(board.is_empty(*c) for c in sink_cells), (
        "one cell of a Sink should have taken all of it: "
        f"{[c for c in sink_cells if not board.is_empty(*c)]} left")
    ok("a rectangle clipping one cell erased the whole Sink")


def test_x_means_two_things_split_by_ctrl():
    """One key, two meanings -- and they must not both fire.

    `X` is the toolbar row swap and `ctrl+X` is the cut, which is only safe
    because the swap is gated on *not* ctrl. Without that gate a cut would
    also re-aim the number keys, silently changing which tool the next digit
    selects. The two are read from a single `_edge()` call for the same
    reason: `_edge()` consumes the press, so asking twice would always give
    the second caller False and kill whichever binding asked last.
    """
    engine.place_belt_path((20, 24), (23, 24))
    engine.sync_segments()

    # Bare X swaps the row and starts no mode.
    row = orch.toolbelt.row
    press_key(glfw.KEY_X)
    assert orch.toolbelt.row != row, "bare X should swap the toolbar row"
    assert not orch.clipboard.active, (
        "bare X must not enter a clipboard mode")
    press_key(glfw.KEY_X)
    assert orch.toolbelt.row == row, "and swap back"

    # ctrl+X cuts and leaves the row alone.
    enter_cut_mode()
    assert orch.clipboard.cutting, "ctrl+X should arm the cut"
    assert orch.toolbelt.row == row, (
        "ctrl+X must not also swap the row -- the swap is gated on not-ctrl")
    press_key(glfw.KEY_ESCAPE)

    # ctrl+D is the other entry to the same mode, without the cut.
    enter_erase_mode()
    assert orch.clipboard.erasing and not orch.clipboard.cutting, (
        "ctrl+D should erase rather than cut")
    assert orch.toolbelt.row == row
    press_key(glfw.KEY_ESCAPE)
    ok("X swaps the row, ctrl+X cuts, ctrl+D erases -- no collisions")


def test_ctrl_x_cuts_into_paste_mode():
    """A cut captures before deleting, then hands you the stamp."""
    engine.place_belt_path((20, 20), (23, 20))
    engine.sync_segments()
    assert len(_row_survivors(20, range(20, 24))) == 4

    enter_cut_mode()
    assert orch.clipboard.mode == MODE_ERASE, orch.clipboard.mode
    assert orch.clipboard.cutting, "ctrl+X must arm the cut"

    drag_cells((19, 19), (24, 21))
    assert _row_survivors(20, range(20, 24)) == [], (
        "the cut must delete what it took")
    assert orch.clipboard.mode == MODE_PASTE, orch.clipboard.mode
    s = orch.clipboard.stamp
    assert s is not None and len(s) == 4, s
    ok("ctrl+X captured four belts, deleted them, and went to paste mode")

    # And it pastes back, which is the whole point of a cut.
    put_cursor_at_cell(21, 20)
    step()
    click()
    assert _row_survivors(20, range(20, 24)) == [20, 21, 22, 23], (
        _row_survivors(20, range(20, 24)))
    ok("the cut stamp pasted back onto the board")


# --- matched ghosts: substituting a lined-up stamp -------------------------
#
# The feature these cover: a frustrated cell whose occupant is *exactly* what
# the placement wanted tints green instead of red, and a bare click over a
# wholly-matched plan deletes the old entities and places the new ones. It
# exists so per-instance settings -- a Source's emit value, a belt's seed --
# can be applied to many entities at once, which is otherwise one trip
# through the Status window each.
#
# What makes these worth writing rather than trusting: a substitution that
# silently did nothing and a refusal look identical on a board where the old
# and new entities are the same type at the same place. So every test here
# asserts on something that *distinguishes* them -- a changed emit value, a
# changed row id, or a live count.

def _ghost_states():
    """Count of each ghost state on the board, as {state: n}."""
    arr = cell.ghost_state_arr(board.grid[:, :, cell.CH_Z])
    return {s: int((arr == s).sum())
            for s in (cell.GHOST_PENDING, cell.GHOST_FRUSTRATED,
                      cell.GHOST_MATCHED)}


def _copy_a_source(at=(10, 10), emit=7.5):
    """Place a Source configured to `emit`, copy it, and hold the stamp."""
    src = engine.place_structure(Source, *at)
    src.emit_value = emit
    engine.sync_segments()
    ctrl_key(glfw.KEY_C)
    drag_cells((at[0] - 1, at[1] - 1), (at[0] + 2, at[1] + 1))
    assert orch.clipboard.mode == MODE_PASTE, orch.clipboard.mode
    return orch.clipboard.stamp


def test_a_lined_up_stamp_tints_matched_not_frustrated():
    """The readable half: green means "already correct", not "blocked"."""
    _copy_a_source(at=(10, 10), emit=7.5)
    # Hover the stamp back over the very Source it was copied from.
    put_cursor_at_cell(10, 10); step()

    states = _ghost_states()
    assert states[cell.GHOST_MATCHED] > 0, (
        f"a stamp over its own original should tint matched: {states}")
    assert states[cell.GHOST_FRUSTRATED] == 0, (
        f"nothing here is genuinely blocked: {states}")
    # And the occupant survives under the tint, as with any frustration.
    assert cell.cell_type(board.cell_word(10, 10)) == cell.TYPE_SOURCE
    ok(f"a lined-up stamp tinted {states[cell.GHOST_MATCHED]} matched cells")


def test_a_misaligned_stamp_still_tints_frustrated():
    """The guard: only an *exact* match counts, origin included.

    A 2x1 Source overlapping another by one cell is the case a per-cell match
    test would get wrong -- the shared cell has the same type and facing, so
    cell-wise it looks like a match, while the footprints are a cell apart.
    Substituting there would move a machine one cell from where it was shown.
    """
    _copy_a_source(at=(10, 10), emit=7.5)
    # One cell east: the two 2x1 footprints overlap in exactly one cell.
    put_cursor_at_cell(11, 10); step()

    states = _ghost_states()
    assert states[cell.GHOST_FRUSTRATED] > 0, (
        f"a half-overlap is a genuine clash: {states}")
    assert states[cell.GHOST_MATCHED] == 0, (
        f"a one-cell offset is not a match: {states}")
    ok("a stamp offset by one cell tinted frustrated, not matched")


def test_a_rotated_stamp_is_not_a_match():
    """Orientation is part of identity: a turned stamp must not substitute."""
    _copy_a_source(at=(10, 10), emit=7.5)
    put_cursor_at_cell(10, 10); step()
    assert _ghost_states()[cell.GHOST_MATCHED] > 0, "should start matched"

    press_key(glfw.KEY_R)
    step()
    states = _ghost_states()
    assert states[cell.GHOST_MATCHED] == 0, (
        f"a rotated stamp wires up differently and is not a match: {states}")
    ok("rotating the stamp dropped it out of the matched state")


def test_a_mirrored_stamp_is_not_a_match():
    """Flip is part of identity, and this is the dangerous case.

    A mirrored chiral machine looks **identical in outline** and wires up
    differently -- that is why `EntityBuilder.adopt()` copies the flip as
    well as the rotation. So a flipped stamp over an unflipped machine is
    exactly the match that must be refused and the one a player could not
    spot by eye. Mutation-tested: without the flip comparison in
    `Engine.placement_matches()` every other test here still passes.
    """
    _copy_a_source(at=(10, 10), emit=7.5)
    put_cursor_at_cell(10, 10); step()
    assert _ghost_states()[cell.GHOST_MATCHED] > 0, "should start matched"
    assert Source.is_chiral(), (
        "this test needs a chiral type, or F is a no-op and it proves nothing")

    press_key(glfw.KEY_F)
    step()
    entry = orch.clipboard.stamp.entries[0]
    assert entry.flipped, "F should have mirrored the stamp"
    # A mirror is a flip *and* a direction change (see core/stamp.py), so put
    # the facing back: with the direction differing too, the match would be
    # refused for *that* reason and the flip comparison would go untested --
    # which is exactly how an earlier version of this test passed against a
    # build with the flip check removed.
    entry.direction = cell.DIR_PX
    orch._refresh_paste_tool()
    assert orch.paste_tool.stamp.entries[0].flipped
    assert orch.paste_tool.stamp.entries[0].direction == cell.DIR_PX

    # Re-preview explicitly. The cursor has not moved, so stepping alone
    # leaves the previous frame's tint on the board -- the other trap this
    # test fell into, and one that makes any assertion here read stale state.
    orch._preview(orch.paste_tool)

    states = _ghost_states()
    assert states[cell.GHOST_MATCHED] == 0, (
        f"a flipped Source over an unflipped one must not match: {states}")
    assert states[cell.GHOST_FRUSTRATED] > 0, states
    ok("a mirrored stamp tinted frustrated, not matched")


def test_a_bare_click_substitutes_a_matched_stamp():
    """The feature: click to swap configured copies in over the originals.

    Asserts the **emit value**, not merely that a Source is still there. A
    substitution that quietly did nothing would leave an identical-looking
    board, so the per-instance setting is the only thing that can tell the
    two apart -- which is also the entire reason the feature exists.
    """
    old = engine.place_structure(Source, 20, 20)
    old.emit_value = 1.0
    engine.sync_segments()

    _copy_a_source(at=(10, 10), emit=7.5)
    put_cursor_at_cell(20, 20); step()
    assert _ghost_states()[cell.GHOST_MATCHED] > 0, "should be lined up"

    live_before = board.live_count
    click()
    step()

    placed = engine.entity_at(20, 20)
    assert placed is not None, "the substitution left the cell empty"
    assert isinstance(placed, Source), type(placed)
    assert placed.emit_value == 7.5, (
        f"the substituted Source emits {placed.emit_value}, so the old one "
        f"was never replaced")
    # Note the row is usually *the same integer*: the erase frees it and the
    # free list hands it straight back. That is correct and is why the emit
    # value above is the assertion with teeth -- "a different row" would be
    # testing the allocator, not the substitution.
    assert board.live_count == live_before, (
        f"one out, one in: {live_before} -> {board.live_count}")
    ok("a bare click substituted a configured Source over the old one")


def test_substitution_is_refused_when_anything_is_genuinely_blocked():
    """All-or-nothing: a partial match must not delete the matched half.

    Deleting the lined-up entities would not unblock the real clash, so the
    plan would still refuse -- and the player would have lost the matched
    ones for nothing. The whole gesture has to be inert.
    """
    # Two Sources side by side, then copy both as one stamp.
    a = engine.place_structure(Source, 10, 10)
    a.emit_value = 7.5
    b = engine.place_structure(Source, 10, 13)
    b.emit_value = 7.5
    engine.sync_segments()
    ctrl_key(glfw.KEY_C)
    drag_cells((9, 9), (12, 14))
    assert len(orch.clipboard.stamp) == 2, len(orch.clipboard.stamp)

    # A target where one half lines up and the other hits a different
    # machine. The stamp is centred on the cursor, so a click at (20,20)
    # lands its halves at y=19 and y=22 -- the obstacles have to go where the
    # stamp actually falls, not where the cursor is.
    engine.place_structure(Source, 20, 19)
    engine.place_structure(Shear, 20, 22)
    engine.sync_segments()
    live_before = board.live_count
    rows_before = {engine.entity_at(20, 19).row, engine.entity_at(20, 22).row}

    put_cursor_at_cell(20, 20); step()
    states = _ghost_states()
    assert states[cell.GHOST_FRUSTRATED] > 0, (
        f"the Shear is a genuine clash: {states}")
    assert states[cell.GHOST_MATCHED] > 0, (
        f"the Source half does line up, so this is a *partial* match: "
        f"{states}")

    click()
    step()
    assert board.live_count == live_before, (
        f"a partially-matched click must build and delete nothing: "
        f"{live_before} -> {board.live_count}")
    rows_after = {engine.entity_at(20, 19).row, engine.entity_at(20, 22).row}
    assert rows_after == rows_before, (
        "the matched half was deleted even though the plan was refused")
    ok("a partially-matched stamp built nothing and deleted nothing")


def test_shift_click_keeps_the_old_partial_paste_behaviour():
    """Shift is the documented "place what fits" policy and must stay so.

    Substitution is a *bare click* feature. Shift already means something
    else -- place the non-clashing entities and reject the rest -- and
    overloading it would leave no way to ask for the old behaviour.
    """
    old = engine.place_structure(Source, 20, 20)
    old.emit_value = 1.0
    old_row = old.row
    engine.sync_segments()

    _copy_a_source(at=(10, 10), emit=7.5)
    put_cursor_at_cell(20, 20); step()

    fake["keys"].add(glfw.KEY_LEFT_SHIFT)
    try:
        step()
        click()
        step()
    finally:
        fake["keys"].discard(glfw.KEY_LEFT_SHIFT)
        step()

    survivor = engine.entity_at(20, 20)
    assert survivor is not None and survivor.row == old_row, (
        "shift+click should have left the existing Source alone")
    assert survivor.emit_value == 1.0, (
        f"shift+click substituted anyway: emit is {survivor.emit_value}")
    ok("shift+click rejected the clash instead of substituting")


def test_a_matched_belt_substitutes_its_seed():
    """The other per-instance setting, and the reason this is not Source-only.

    A seeded belt is the case where the entities are *1x1 and identical in
    every visible way*, so nothing but the seed can distinguish a successful
    substitution from a no-op.
    """
    # A plain run, then a seeded belt copied from elsewhere.
    engine.place_belt(20, 20, cell.DIR_PX)
    engine.sync_segments()
    plain = board.entity_at(20, 20)
    assert not plain.seed_on, "the target should start unseeded"

    seeded = engine.place_belt(10, 10, cell.DIR_PX)
    orch._set_seed(seeded.row, True, 4.0)
    engine.sync_segments()
    ctrl_key(glfw.KEY_C)
    # A selection rectangle has to *drag*: a same-cell "drag" never passes
    # DRAG_THRESHOLD_PX and so is a plain click, which leaves copy mode
    # instead of capturing. The box covers only this belt either way.
    drag_cells((9, 9), (11, 11))
    assert orch.clipboard.mode == MODE_PASTE, orch.clipboard.mode
    assert len(orch.clipboard.stamp) == 1, len(orch.clipboard.stamp)

    put_cursor_at_cell(20, 20); step()
    assert _ghost_states()[cell.GHOST_MATCHED] > 0, (
        "a belt over an identically-facing belt should match")

    click()
    step()
    now = board.entity_at(20, 20)
    assert now is not None and now.seed_on, (
        "the substituted belt is not seeded, so the old one survived")
    assert now.seed_value == 4.0, now.seed_value
    ok("a matched belt substituted and brought its seed with it")


def test_a_belt_facing_the_other_way_is_not_a_match():
    """Direction is identity for a belt too -- it decides where items go."""
    engine.place_belt(20, 20, cell.DIR_PY)
    engine.sync_segments()

    engine.place_belt(10, 10, cell.DIR_PX)
    engine.sync_segments()
    ctrl_key(glfw.KEY_C)
    drag_cells((9, 9), (11, 11))
    assert len(orch.clipboard.stamp) == 1, len(orch.clipboard.stamp)

    put_cursor_at_cell(20, 20); step()
    states = _ghost_states()
    assert states[cell.GHOST_MATCHED] == 0, (
        f"an eastward belt over a northward one is not a match: {states}")
    assert states[cell.GHOST_FRUSTRATED] > 0, states
    ok("a belt facing the other way tinted frustrated, not matched")


def test_substitution_clears_its_own_preview_before_replanning():
    """`_substitute()` must drop the old ghosts before it re-plans.

    Same hazard `Engine.preview()` exists for: ghosts occupy real cells, so a
    re-plan against a board that still holds them collides with the tool's
    own preview. Here it bites harder than usual, because the re-plan is what
    decides whether the substitution goes through at all -- a plan that
    collides with its own leftovers reads as still-blocked and the whole
    gesture silently does nothing after the old entities were already erased.

    A **belt run** is what exposes it. A single matched placement leaves no
    ghosts to go stale (its cells are all occupied, so nothing is ghosted),
    while a stamp mixing matched and free cells does -- the free ones ghost,
    and those ghosts are what the re-plan would trip over.
    """
    # A stamp of three belts, of which the middle one will line up and the
    # outer two will land on empty cells.
    engine.place_belt_path((10, 10), (12, 10))
    engine.sync_segments()
    ctrl_key(glfw.KEY_C)
    drag_cells((9, 9), (13, 11))
    assert len(orch.clipboard.stamp) == 3, len(orch.clipboard.stamp)

    # One matching belt at the centre of where the stamp will land.
    engine.place_belt(21, 20, cell.DIR_PX)
    engine.sync_segments()
    live_before = board.live_count

    put_cursor_at_cell(21, 20); step()
    states = _ghost_states()
    assert states[cell.GHOST_MATCHED] > 0, f"the centre should match: {states}"
    assert states[cell.GHOST_PENDING] > 0, (
        f"the two free cells should ghost, which is what can go stale: "
        f"{states}")

    click()
    step()
    for x in (20, 21, 22):
        assert not board.is_empty(x, 20), (
            f"({x},20) is empty: the substitution collided with its own "
            f"leftover ghosts and committed nothing")
    assert board.live_count == live_before + 2, (
        f"one replaced and two added: {live_before} -> {board.live_count}")
    ok("substitution re-planned against a cleared board and placed all three")


def test_a_matched_multi_entity_stamp_substitutes_every_member():
    """The real use: reconfigure a row of machines in one click."""
    # Three configured Sources, copied as one stamp.
    for y in (10, 13, 16):
        s = engine.place_structure(Source, 10, y)
        s.emit_value = 7.5
    engine.sync_segments()
    ctrl_key(glfw.KEY_C)
    drag_cells((9, 9), (12, 17))
    assert len(orch.clipboard.stamp) == 3, len(orch.clipboard.stamp)

    # Three unconfigured ones in the same arrangement.
    old_rows = []
    for y in (20, 23, 26):
        s = engine.place_structure(Source, 30, y)
        s.emit_value = 1.0
        old_rows.append(s.row)
    engine.sync_segments()
    live_before = board.live_count

    put_cursor_at_cell(30, 23); step()
    assert _ghost_states()[cell.GHOST_MATCHED] > 0, "should be lined up"
    assert _ghost_states()[cell.GHOST_FRUSTRATED] == 0, "nothing else blocks"

    click()
    step()

    for y in (20, 23, 26):
        got = engine.entity_at(30, y)
        assert got is not None and isinstance(got, Source), (y, got)
        assert got.emit_value == 7.5, (
            f"the Source at y={y} still emits {got.emit_value}")
    assert board.live_count == live_before, (
        f"three out, three in: {live_before} -> {board.live_count}")
    ok("one click reconfigured all three Sources")


def test_a_cut_is_one_continuous_mode_chain():
    """The tool held before a cut survives cut -> paste -> exit.

    Same property as ctrl+C -> ctrl+V, and it holds for the same reason:
    `Clipboard._stash()` refuses to overwrite an existing stash, so the
    intermediate mode switch cannot replace the player's real tool with
    erase mode's own empty hand.
    """
    select_tool("Shear")
    engine.place_belt_path((20, 20), (22, 20))
    engine.sync_segments()
    enter_cut_mode()
    drag_cells((19, 19), (23, 21))
    assert orch.clipboard.mode == MODE_PASTE
    press_key(glfw.KEY_ESCAPE)
    assert orch.clipboard.mode == MODE_OFF
    assert orch.toolbelt.active.name == "Shear", orch.toolbelt.active.name
    ok("the Shear came back after cut -> paste -> Esc")


def test_the_cut_flag_does_not_outlive_its_mode():
    """A later bare-X stroke must not silently cut.

    `cutting` lives on the mode, so every exit clears it. Without that a
    player who cut once would find their next plain erase quietly copying --
    invisible until the history filled with things they meant to delete.
    """
    select_tool("Belt")
    enter_cut_mode()
    assert orch.clipboard.cutting
    press_key(glfw.KEY_ESCAPE)
    assert not orch.clipboard.cutting, "Esc must disarm the cut"

    enter_erase_mode()
    assert not orch.clipboard.cutting, "bare X after a cut must not cut"
    engine.place_belt_path((10, 10), (12, 10))
    engine.sync_segments()
    drag_cells((9, 9), (13, 11))
    assert _row_survivors(10, range(9, 14)) == []
    assert not orch.clipboard.history, "the stale cut flag copied anyway"
    ok("the cut flag did not survive its mode")


def test_an_empty_cut_does_not_strand_you_in_paste_mode():
    """Cutting bare board catches nothing, so there is nothing to paste."""
    select_tool("Belt")
    enter_cut_mode()
    drag_cells((40, 40), (44, 44))
    assert not orch.clipboard.history, "nothing should have been pushed"
    assert orch.clipboard.mode == MODE_ERASE, (
        "an empty cut should leave you erasing, not in an empty paste mode")
    ok("an empty cut left the player in erase mode")


# --- selecting a tool overrides the stash ----------------------------------
def test_selecting_a_tool_leaves_any_mode_holding_that_tool():
    """A slot pick is a new choice, so it beats the stashed tool.

    Every other exit restores the stash, because the mode interrupted the
    player and handing the tool back is the courtesy. Choosing a slot is not
    an interruption ending -- restoring afterwards would override what was
    just asked for, which is why `_select_slot()` drops the stash instead.
    """
    for enter, name in ((lambda: ctrl_key(glfw.KEY_C), "copy"),
                        (enter_erase_mode, "erase"),
                        (enter_cut_mode, "cut")):
        harness.reset()
        select_tool("Shear")
        enter()
        assert orch.clipboard.active, f"setup: {name} mode should be on"
        select_tool("Belt")
        assert orch.clipboard.mode == MODE_OFF, (
            f"selecting a tool should have left {name} mode")
        assert orch.toolbelt.active.name == "Belt", (
            f"{name}: landed on {orch.toolbelt.active.name}, not the picked "
            f"tool -- the stash overrode the choice")
    ok("a slot pick left copy, erase and cut modes holding the picked tool")


def test_selecting_a_tool_leaves_paste_mode_too():
    """Including paste, whose tool override has to be dropped with it."""
    _copy_a_run()
    assert orch.clipboard.mode == MODE_PASTE
    select_tool("Mac")
    assert orch.clipboard.mode == MODE_OFF
    assert orch.toolbelt.active.name == "Mac", orch.toolbelt.active.name
    assert orch.paste_tool is None, "the paste tool must go with the mode"
    # The stamp itself survives -- leaving paste mode is not losing the copy.
    assert orch.clipboard.history, "the history should outlive the mode"
    ok("a slot pick left paste mode and dropped the paste tool")


def test_the_panels_slot_buttons_leave_a_mode_too():
    """Clicking a slot in the bar must behave exactly as its digit does.

    The two go through one path (`Orchestrator._select_slot`), so this pins
    the wiring rather than a second implementation.
    """
    select_tool("Shear")
    ctrl_key(glfw.KEY_C)
    assert orch.clipboard.mode == MODE_COPY
    belt_slot = next(i for i, s in enumerate(orch.toolbelt.slots)
                     if s is not None and s.name == "Belt")
    orch._select_slot(belt_slot)
    assert orch.clipboard.mode == MODE_OFF
    assert orch.toolbelt.active.name == "Belt"
    ok("the panel's slot path left the mode on the picked tool")
