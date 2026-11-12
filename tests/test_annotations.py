"""The annotation layer: drawing, erasing, editing, copying, persistence.

`core/annotations.py` self-checks the geometry and the id discipline at
import, so these cover what only the wired-up app can show: the gestures
through the real mouse, the Status window flow, the interaction with copy and
erase, and the enable flag's reach.

Three things these tests are careful about, each having been a real trap:

- **A disabled layer must not merely stop drawing.** It also stops notes
  being clicked, copied and rectangle-erased, because an invisible
  annotation that still eats clicks or joins stamps is worse than one that
  is simply off. Several tests assert the *absence* of those effects.
- **The brush eraser and the rectangle eraser use different hit rules** --
  real distance to the ink versus the bounding box -- so a test that only
  exercised one would miss the other's rule entirely.
- **Text must survive a transform upright.** A stamp rotation moves a label
  with its machines but must never rotate the glyphs, so the assertions are
  on the box's *position* changing while its width and height do not swap.
"""

import os

from core import cell
from core.annotations import PALETTE, Stroke, TextBox
from core.clipboard import capture
from tests.harness import (board, click, ctrl_key, drag_cells, drag_world,
                           engine, enter_cut_mode, enter_erase_mode, fake,
                           install_tool, orch, press_key, put_cursor_at,
                           put_cursor_at_cell, select_tool, step)

import glfw


def _notes():
    return orch.annotations


def _belt_run(a, b):
    """Lay a belt run with the planner's real click-move-click gesture.

    Not `drag_cells()`: the belt planner is drag-based in the sense that it
    builds a plan between two clicks, so a held-button drag pans the camera
    instead of drawing. Several of these tests need something on the board to
    annotate, and getting that wrong silently gives an empty board.
    """
    select_tool("Belt")
    put_cursor_at_cell(*a)
    step()
    click()
    put_cursor_at_cell(*b)
    step()
    click()
    step()


def _draw_tool(slot=15):
    return install_tool("draw_note", slot)


def _text_tool(slot=16):
    return install_tool("text_note", slot)


# --- drawing ---------------------------------------------------------------
def test_a_left_drag_draws_a_stroke():
    _draw_tool()
    assert len(_notes()) == 0
    drag_world((10.0, 10.0), (14.0, 12.0), steps=6)
    assert len(_notes()) == 1, "the drag should have left exactly one stroke"
    s = _notes().items[0]
    assert isinstance(s, Stroke)
    # Several points, not just the two ends: the stroke samples per frame.
    assert len(s.points) >= 3, s.points
    x0, y0, x1, y1 = s.bounds()
    assert x0 <= 10.0 and x1 >= 14.0, (x0, x1)
    print(f"  OK: a left-drag recorded a {len(s.points)}-point stroke")


def test_a_stroke_takes_the_tools_colour_and_width():
    tool = _draw_tool()
    # R walks the width, F the palette -- the annotation tools' meanings for
    # the two per-tool keys.
    press_key(glfw.KEY_R)
    press_key(glfw.KEY_F)
    want_c, want_w = tool.colour, tool.size
    assert (want_c, want_w) != (0, 1), "R and F should have moved something"
    drag_world((20.0, 20.0), (23.0, 20.0))
    s = _notes().items[0]
    assert (s.colour, s.width) == (want_c, want_w), (
        f"stroke took {(s.colour, s.width)}, tool held {(want_c, want_w)}")
    print(f"  OK: the stroke took the tool's pen ({PALETTE[want_c][0]}, "
          f"width {want_w})")


def test_a_right_drag_erases_strokes_under_the_brush():
    _draw_tool()
    drag_world((30.0, 30.0), (36.0, 30.0), steps=6)
    drag_world((30.0, 40.0), (36.0, 40.0), steps=6)
    assert len(_notes()) == 2
    # Sweep the brush along the first stroke only.
    drag_world((30.0, 30.0), (36.0, 30.0), steps=6, button="right")
    assert len(_notes()) == 1, "the brush should have taken one stroke"
    left = _notes().items[0]
    assert left.bounds()[1] > 35.0, "the surviving stroke is the upper one"
    print("  OK: a right-drag erased the stroke it swept and left the other")


def test_the_brush_ignores_an_empty_bounding_box():
    """The brush tests distance to the ink, not the box.

    An L-shaped stroke has a large empty bbox corner. Dragging the brush
    through that corner must delete nothing -- which is exactly where the
    rectangle eraser's rule differs, so this is the test that separates them.
    """
    _draw_tool()
    # An L: along the bottom, then up the right side.
    drag_world((40.0, 10.0), (46.0, 10.0), steps=6)
    s = _notes().items[0]
    for y in (11.0, 12.0, 13.0, 14.0, 15.0, 16.0):
        s.points.append((46.0, y))
    x0, y0, x1, y1 = s.bounds()
    assert y1 > 15.0 and x0 < 41.0, "the L should have a tall empty corner"
    # The corner at (41, 15) is inside the bbox and far from any ink.
    assert s.intersects(40.5, 14.5, 41.5, 15.5), "bbox covers the corner"
    assert not s.hit_point(41.0, 15.0), "but there is no ink there"
    drag_world((40.6, 14.6), (41.4, 15.4), steps=4, button="right")
    assert len(_notes()) == 1, "the brush must not take a stroke by its bbox"
    print("  OK: the brush spared a stroke whose empty bbox corner it crossed")


def test_escape_abandons_an_in_flight_stroke():
    _draw_tool()
    put_cursor_at(50.0, 50.0)
    step()
    fake["left"] = True
    step()
    put_cursor_at(53.0, 50.0)
    step()
    assert orch._live_stroke is not None, "a stroke should be in flight"
    press_key(glfw.KEY_ESCAPE)
    assert orch._live_stroke is None, "Esc should drop the in-flight stroke"
    fake["left"] = False
    step()
    assert len(_notes()) == 0, "nothing should have been committed"
    print("  OK: Esc abandoned the stroke without committing it")


def test_switching_tools_drops_an_in_flight_stroke():
    _draw_tool()
    put_cursor_at(50.0, 60.0)
    step()
    fake["left"] = True
    step()
    put_cursor_at(53.0, 60.0)
    step()
    assert orch._live_stroke is not None
    try:
        select_tool("Belt")
        assert orch._live_stroke is None, (
            "holding another tool must drop the annotation gesture")
        assert len(_notes()) == 0
    finally:
        fake["left"] = False
        step()
    print("  OK: picking up another tool dropped the half-drawn stroke")


# --- text boxes ------------------------------------------------------------
def test_a_text_drag_makes_a_box_and_opens_the_status_window():
    _text_tool()
    drag_world((10.0, 30.0), (16.0, 28.0), steps=5)
    assert len(_notes()) == 1, "the drag should have made one text box"
    box = _notes().items[0]
    assert isinstance(box, TextBox)
    assert box.text == "", "a fresh box starts empty"
    # The other half of the gesture: it is selected, so the window is open on
    # it and the player can type immediately.
    assert orch.inspect_note == box.aid, (
        "creating a text box must open the Status window on it")
    assert orch.inspect_row is None, "and must not also inspect an entity"
    assert orch.inspected_note() is box
    print("  OK: the drag made an empty box and opened Status on it")


def test_a_tiny_text_drag_still_gives_a_usable_box():
    _text_tool()
    # Barely a drag: a box this small would be invisible and unclickable, so
    # it is widened rather than rejected -- a click still means "label here".
    drag_world((20.0, 30.0), (20.05, 30.02), steps=2)
    assert len(_notes()) == 1
    box = _notes().items[0]
    x0, y0, x1, y1 = box.bounds()
    assert (x1 - x0) >= 1.0 and (y1 - y0) >= 0.5, (x1 - x0, y1 - y0)
    print(f"  OK: a near-zero drag still gave a {x1 - x0:.1f}x{y1 - y0:.1f} box")


def test_typing_into_the_status_window_sets_the_text():
    _text_tool()
    drag_world((10.0, 40.0), (16.0, 38.0))
    box = _notes().items[0]
    # The panel only requests; the orchestrator applies. Driven through the
    # setter rather than imgui, since the headless harness draws no panel.
    assert orch._set_note_text(box.aid, "CARRY IN")
    assert box.text == "CARRY IN"
    # A no-op edit is not a change, the same guard _set_emit uses: imgui
    # reports a change per keystroke.
    assert not orch._set_note_text(box.aid, "CARRY IN")
    print("  OK: the Status window's text reached the label")


def test_clicking_a_text_box_with_an_empty_hand_reopens_it():
    _text_tool()
    drag_world((10.0, 50.0), (16.0, 48.0))
    box = _notes().items[0]
    orch._set_note_text(box.aid, "ALU")
    # Empty the hand, close the window, then click the label again.
    press_key(glfw.KEY_Q)
    assert orch.toolbelt.empty_handed
    orch.inspect_note = None
    put_cursor_at(13.0, 49.0)
    click()
    assert orch.inspect_note == box.aid, (
        "clicking a text box with an empty hand should reopen it")
    print("  OK: an empty-handed click on the label reopened its window")


def test_a_note_click_beats_the_entity_underneath():
    """A label drawn over a belt must be clickable, not the belt.

    Annotations draw over the board, so the click order has to match the
    draw order or a note sitting on a factory would be uneditable.
    """
    _belt_run((10, 60), (16, 60))
    row_cells = [x for x in range(10, 17) if not board.is_empty(x, 60)]
    assert len(row_cells) >= 5, "expected a belt run to annotate"

    _text_tool()
    drag_world((11.0, 61.0), (15.0, 60.2), steps=5)
    box = _notes().items[0]
    press_key(glfw.KEY_Q)
    orch.inspect_note = None
    orch.inspect_row = None
    # Click inside the box, which also sits over a belt cell.
    put_cursor_at(12.0, 60.5)
    click()
    assert orch.inspect_note == box.aid, "the note should win the click"
    assert orch.inspect_row is None, "and the belt should not be inspected"
    print("  OK: a click landed on the note rather than the belt beneath it")


def test_deleting_from_the_status_window_closes_it():
    _text_tool()
    drag_world((20.0, 50.0), (26.0, 48.0))
    box = _notes().items[0]
    assert orch.inspect_note == box.aid
    assert orch._delete_note(box.aid)
    assert len(_notes()) == 0
    assert orch.inspect_note is None, "deleting must close the window"
    assert orch.inspected_note() is None
    print("  OK: Delete removed the note and closed its window")


def test_erasing_a_note_closes_a_window_open_on_it():
    """A stale id must read as gone rather than as another annotation."""
    _text_tool()
    drag_world((30.0, 50.0), (36.0, 48.0))
    box = _notes().items[0]
    aid = box.aid
    assert orch.inspect_note == aid
    _draw_tool()
    # Brush over it. A TextBox's hit test is its box, so this takes it.
    drag_world((31.0, 49.0), (35.0, 49.0), steps=4, button="right")
    assert _notes().get(aid) is None, "the brush should have taken the box"
    assert orch.inspected_note() is None, (
        "a window on a deleted note must report nothing")
    print("  OK: erasing the inspected note closed its window")


def test_the_style_buttons_change_colour_and_size():
    _text_tool()
    drag_world((40.0, 50.0), (46.0, 48.0))
    box = _notes().items[0]
    assert orch._set_note_style(box.aid, colour=3)
    assert box.colour == 3
    assert orch._set_note_style(box.aid, size=2)
    assert box.size == 2
    assert not orch._set_note_style(box.aid, colour=3), "a no-op is no change"
    # The same request shape reaches a stroke's *width* rather than its size.
    _draw_tool()
    drag_world((40.0, 44.0), (44.0, 44.0))
    s = [a for a in _notes() if isinstance(a, Stroke)][0]
    assert orch._set_note_style(s.aid, size=0)
    assert s.width == 0, "a stroke's size request sets its width"
    print("  OK: the style requests reached both a box's size and a "
          "stroke's width")


# --- dragging the open box -------------------------------------------------
def _open_box(a=(10.0, 30.0), b=(16.0, 28.0)):
    """A text box with the Status window open on it, and an empty hand.

    Creating one already selects it; `Q` then empties the hand without
    closing the window, which is the state the move gesture needs.
    """
    _text_tool()
    drag_world(a, b, steps=5)
    box = _notes().items[0]
    press_key(glfw.KEY_Q)
    assert orch.toolbelt.empty_handed
    assert orch.inspect_note == box.aid
    return box


def test_dragging_the_open_box_moves_it_instead_of_panning():
    box = _open_box()
    before = box.bounds()
    cam_before = tuple(orch.camera.center)
    drag_world((13.0, 29.0), (23.0, 24.0), steps=8)

    after = _notes().get(box.aid).bounds()
    # Exactly the cursor's delta: the move anchors at the *press*, so the
    # frames spent crossing DRAG_THRESHOLD_PX are part of the drag rather
    # than lost. Anchoring at the threshold crossing instead left the box
    # lagging the pointer by the threshold on every single grab.
    assert abs((after[0] - before[0]) - 10.0) < 1e-6, (
        f"want +10 east, got {after[0] - before[0]:.4f}")
    assert abs((after[1] - before[1]) + 5.0) < 1e-6, (
        f"want -5 south, got {after[1] - before[1]:.4f}")
    assert tuple(orch.camera.center) == cam_before, (
        "the drag must move the box, not pan the camera")
    # Its extent and its selection both survive.
    assert (round(after[2] - after[0], 6), round(after[3] - after[1], 6)) \
        == (round(before[2] - before[0], 6), round(before[3] - before[1], 6)), \
        "a move must not resize the box"
    assert orch.inspect_note == box.aid, "and must keep it selected"
    print("  OK: the drag moved the open box and left the camera alone")


def test_the_box_keeps_its_grab_offset():
    """A delta, not a teleport.

    Dragging from near one corner must not snap the box's origin under the
    cursor -- which is the difference between applying the move as a delta
    and as an absolute position.
    """
    box = _open_box()
    x0, y0, x1, y1 = box.bounds()
    # Grab near the box's left edge and drag right.
    grab = (x0 + 0.3, (y0 + y1) * 0.5)
    drag_world(grab, (grab[0] + 8.0, grab[1]), steps=8)
    after = _notes().get(box.aid).bounds()
    # The cursor ended 8 cells east of a point 0.3 from the left edge. If the
    # origin had snapped to the cursor the box's left edge would be *at* the
    # final cursor position; with the offset kept it stays 0.3 behind it.
    final_cursor_x = grab[0] + 8.0
    assert after[0] < final_cursor_x - 0.1, (
        f"the box origin ({after[0]:.2f}) snapped to the cursor "
        f"({final_cursor_x:.2f}) instead of keeping its grab offset")
    print("  OK: the box kept the offset it was grabbed at")


def test_a_closed_window_leaves_the_drag_as_a_camera_pan():
    """The window being open is what claims the gesture.

    Without this the camera's left-drag would be unreliable anywhere a label
    happened to lie, which is most of an annotated board.
    """
    box = _open_box()
    orch.inspect_note = None
    step()
    before = _notes().get(box.aid).bounds()
    cam_before = tuple(orch.camera.center)
    mid = ((before[0] + before[2]) * 0.5, (before[1] + before[3]) * 0.5)
    drag_world(mid, (mid[0] + 6.0, mid[1] + 3.0), steps=6)
    after = _notes().get(box.aid).bounds()
    assert after == before, (
        f"with the window closed the box must not move: {before} -> {after}")
    assert tuple(orch.camera.center) != cam_before, (
        "and the drag should pan the camera as usual")
    print("  OK: a closed window left the drag as a camera pan")


def test_a_drag_off_the_box_still_pans():
    box = _open_box()
    before = _notes().get(box.aid).bounds()
    cam_before = tuple(orch.camera.center)
    # Well away from the box.
    drag_world((40.0, 40.0), (44.0, 44.0), steps=6)
    assert _notes().get(box.aid).bounds() == before, (
        "a drag that did not start on the box must not move it")
    assert tuple(orch.camera.center) != cam_before, "it should pan"
    print("  OK: a drag starting off the box panned instead of moving it")


def test_a_click_inside_the_box_is_still_a_click():
    """Only a real drag moves it; a click keeps selecting.

    The press is ambiguous exactly as it is for the camera pan, so the
    gesture goes through ARMED and only becomes a move past the threshold.
    """
    box = _open_box()
    before = _notes().get(box.aid).bounds()
    put_cursor_at(13.0, 29.0)
    step()
    click()
    assert _notes().get(box.aid).bounds() == before, "a click must not move it"
    assert orch.inspect_note == box.aid, "and must leave it selected"
    print("  OK: a click inside the box selected rather than moved")


def test_a_stroke_is_not_draggable():
    """Only text boxes get the handle.

    A stroke's bbox is mostly empty space, so a drag starting "on" a circled
    region would move it from a point nowhere near its ink.
    """
    _draw_tool()
    drag_world((10.0, 40.0), (16.0, 40.0), steps=6)
    s = _notes().items[0]
    press_key(glfw.KEY_Q)
    orch.inspect_note = s.aid          # as if the window were open on it
    step()
    before = s.bounds()
    cam_before = tuple(orch.camera.center)
    drag_world((13.0, 40.0), (19.0, 43.0), steps=6)
    assert _notes().get(s.aid).bounds() == before, (
        "a stroke must not be draggable")
    assert tuple(orch.camera.center) != cam_before, "the drag should pan"
    print("  OK: a stroke kept the camera pan rather than becoming draggable")


def test_moving_survives_the_cursor_leaving_the_box():
    """An in-flight move must not be stranded by the hit test going false.

    The gesture kind is recomputed each frame from "is the cursor over the
    open box?", and a fast drag outruns the box. If that answer gated the
    branch the move would freeze mid-drag with the button still down.
    """
    box = _open_box()
    before = _notes().get(box.aid).bounds()
    # Start inside, then leave in one big hop so the cursor is far outside
    # the box's new position every frame after the first.
    put_cursor_at(13.0, 29.0)
    step()
    fake["left"] = True
    step()
    for wx in (20.0, 30.0, 40.0, 50.0):
        put_cursor_at(wx, 29.0)
        step()
    fake["left"] = False
    step()
    after = _notes().get(box.aid).bounds()
    assert after[0] > before[0] + 25.0, (
        f"the move should have followed the cursor the whole way: "
        f"{before[0]:.1f} -> {after[0]:.1f}")
    assert orch.input.state == 0, (
        f"the gesture should have ended in IDLE, not {orch.input.state}")
    print(f"  OK: the move tracked the cursor {after[0] - before[0]:.0f} "
          f"cells and ended cleanly")


# --- resizing --------------------------------------------------------------
def test_dragging_the_grip_resizes_without_moving():
    """The grip is the bottom-right; the anchor is the top-left.

    So a resize changes the extent and leaves the position alone -- dragging
    the corner must not drag the whole label.
    """
    box = _open_box()
    anchor = (box.x, box.y)
    w0, h0 = box.w, box.h
    x0, y0, x1, y1 = box.bounds()
    # Grab the grip (world bottom-right) and drag out and down.
    drag_world((x1, y0), (x1 + 3.0, y0 - 1.0), steps=6)

    assert abs(box.w - (w0 + 3.0)) < 1e-6, f"width {w0} -> {box.w}, want +3"
    assert abs(box.h - (h0 + 1.0)) < 1e-6, (
        f"height {h0} -> {box.h}; dragging the grip down should grow it")
    assert (box.x, box.y) == anchor, (
        f"a resize must not move the anchor: {anchor} -> {(box.x, box.y)}")
    print(f"  OK: the grip resized {w0}x{h0} to {box.w}x{box.h} in place")


def test_the_grip_wins_over_the_body():
    """Grabbing the corner must never be read as grabbing the box.

    The grip is checked before the body hit test, so the two cannot be
    confused -- otherwise the corner would move the box instead of resizing.
    """
    box = _open_box()
    before = (box.x, box.y, box.w, box.h)
    x0, y0, x1, y1 = box.bounds()
    drag_world((x1, y0), (x1 + 2.0, y0), steps=5)
    assert (box.x, box.y) == before[:2], "the anchor should not have moved"
    assert box.w > before[2], "and the width should have grown"
    print("  OK: a drag from the corner resized rather than moved")


def test_a_resize_cannot_shrink_the_box_to_nothing():
    """A box dragged to zero would be invisible and then unclickable.

    Unrecoverable except through the panel's clear-all, so the resize clamps
    to the same minimum a fresh box gets.
    """
    box = _open_box()
    x0, y0, x1, y1 = box.bounds()
    # Drag the grip far past the anchor, up and to the left.
    drag_world((x1, y0), (x0 - 10.0, y1 + 10.0), steps=8)
    assert box.w > 0.0 and box.h > 0.0, (box.w, box.h)
    assert box.w >= 1.0 and box.h >= 0.5, (
        f"the box collapsed to {box.w}x{box.h}")
    print(f"  OK: a collapsing drag clamped at {box.w}x{box.h}")


def test_a_stroke_gets_no_grip():
    """Only text boxes are resizable, so only they draw a handle."""
    _draw_tool()
    drag_world((10.0, 40.0), (16.0, 40.0), steps=6)
    s = _notes().items[0]
    press_key(glfw.KEY_Q)
    orch.inspect_note = s.aid
    step()
    assert orch._resize_handle_box() is None, (
        "a stroke must not offer a resize handle")
    print("  OK: a stroke offered no resize grip")


def test_no_grip_without_a_selection():
    _draw_tool()
    drag_world((10.0, 44.0), (14.0, 44.0))
    press_key(glfw.KEY_Q)
    orch.inspect_note = None
    step()
    assert orch._resize_handle_box() is None
    print("  OK: nothing selected means no grip")


# --- the paste preview -----------------------------------------------------
def test_a_paste_previews_its_notes():
    """The stamp's labels show where they would land, before the click.

    Built from the live plan's own `paste_origin`, so what is previewed and
    what is placed cannot disagree about position.
    """
    _belt_run((10, 20), (14, 20))
    _text_tool()
    drag_world((10.0, 22.0), (15.0, 21.0), steps=5)
    orch._set_note_text(_notes().items[0].aid, "ADDER")
    assert len(_notes()) == 1

    ctrl_key(glfw.KEY_C)
    drag_cells((9, 19), (16, 23))
    assert orch.clipboard.pasting
    assert len(orch.clipboard.stamp.annotations) == 1

    # Hover a long way from the original, so a previewed note can only be
    # the paste's.
    put_cursor_at_cell(40, 50)
    step()
    preview = orch._paste_note_preview()
    assert len(preview) == 1, (
        f"the paste should preview its one note, got {len(preview)}")
    px0 = preview[0].bounds()[0]
    assert px0 > 30.0, (
        f"the previewed note should sit at the cursor, not at the source: "
        f"{preview[0].bounds()}")
    # And it is a *copy* -- previewing must not touch the store or the stamp.
    assert len(_notes()) == 1, "previewing must not place anything"
    assert preview[0] is not orch.clipboard.stamp.annotations[0]
    press_key(glfw.KEY_ESCAPE)
    print(f"  OK: the paste previewed its note at x={px0:.0f}, placing nothing")


def test_the_preview_follows_the_cursor():
    _belt_run((10, 30), (13, 30))
    _draw_tool()
    drag_world((10.0, 31.0), (13.0, 31.0), steps=4)
    ctrl_key(glfw.KEY_C)
    drag_cells((9, 29), (15, 32))
    assert orch.clipboard.pasting

    put_cursor_at_cell(30, 30)
    step()
    first = orch._paste_note_preview()[0].bounds()[0]
    put_cursor_at_cell(45, 30)
    step()
    second = orch._paste_note_preview()[0].bounds()[0]
    assert second > first + 10.0, (
        f"the preview should track the cursor: {first} -> {second}")
    press_key(glfw.KEY_ESCAPE)
    print("  OK: the previewed note followed the paste cursor")


def test_nothing_previews_without_a_paste():
    """Every other builder has no stamp, so this must be empty and cheap."""
    select_tool("Belt")
    put_cursor_at_cell(20, 20)
    step()
    assert orch._paste_note_preview() == ()
    _draw_tool()
    step()
    assert orch._paste_note_preview() == ()
    print("  OK: no stamp in hand means no previewed notes")


# --- the text tool over an existing box ------------------------------------
def test_the_text_tool_does_not_stack_a_box_on_a_box():
    """A drag starting on a label edits it instead of stacking a new one.

    Two overlapping boxes would be illegible and the new one would swallow
    clicks meant for the old, so the gesture is reinterpreted.
    """
    tool = _text_tool()
    drag_world((10.0, 30.0), (16.0, 28.0), steps=5)
    first = _notes().items[0]
    orch._set_note_text(first.aid, "FIRST")
    orch.inspect_note = None
    step()

    # A drag that *starts* inside the existing box.
    drag_world((13.0, 29.0), (22.0, 24.0), steps=6)
    assert len(_notes()) == 1, (
        f"no second box should have been made, got {len(_notes())}")
    assert orch.inspect_note == first.aid, (
        "and the existing box should have been opened instead")
    assert first.text == "FIRST", "its text must be untouched"
    print("  OK: a drag from inside the box opened it rather than stacking")


def test_clicking_a_box_with_the_text_tool_opens_it():
    """The other half of the same rule, and the one you reach for by reflex.

    Fixing a typo should not require putting the tool down.
    """
    _text_tool()
    drag_world((10.0, 40.0), (16.0, 38.0), steps=5)
    box = _notes().items[0]
    orch._set_note_text(box.aid, "TYPO")
    orch.inspect_note = None
    step()

    put_cursor_at(13.0, 39.0)
    step()
    click()
    assert len(_notes()) == 1, "a click on a box must not make a new one"
    assert orch.inspect_note == box.aid, (
        "clicking a box with the text tool should open its window")
    print("  OK: a click with the text tool opened the box under it")


def test_the_text_tool_still_works_on_empty_board():
    """The suppression must be narrow: empty space still makes a box."""
    _text_tool()
    drag_world((10.0, 50.0), (16.0, 48.0), steps=5)
    assert len(_notes()) == 1
    # A second, well clear of the first.
    drag_world((30.0, 50.0), (36.0, 48.0), steps=5)
    assert len(_notes()) == 2, "a drag on empty space should still make one"
    assert orch.inspect_note == _notes().items[-1].aid, (
        "and the new box should be the one opened")
    print("  OK: drags on empty space still made boxes")


def test_the_text_tool_draws_over_a_stroke():
    """Only *boxes* block a new box.

    A stroke is not something a label would collide with illegibly, and
    refusing to label a circled region would be a surprise.
    """
    _draw_tool()
    drag_world((10.0, 60.0), (18.0, 60.0), steps=6)
    assert len(_notes()) == 1
    _text_tool()
    # Start the drag right on the stroke's ink.
    drag_world((13.0, 60.0), (19.0, 58.0), steps=5)
    assert len(_notes()) == 2, (
        "a stroke must not block a new text box")
    print("  OK: a label was created over a stroke")


# --- the enable flag -------------------------------------------------------
def test_disabling_the_layer_keeps_the_data():
    """Disabled is hidden, never deleted.

    Note the harness cannot observe `drawn` meaningfully: it has no imgui
    backend, so `AnnotationLayer.active` is False and nothing is ever drawn
    here (see that attribute's comment). What *is* testable headlessly is
    everything the flag gates besides pixels -- which is the subject of the
    two tests after this one.
    """
    _draw_tool()
    drag_world((10.0, 20.0), (14.0, 20.0))
    assert len(_notes()) == 1
    try:
        orch.annotation_layer.enabled = False
        step()
        assert len(_notes()) == 1, "hiding must not delete anything"
        assert orch.annotation_layer.drawn == 0
    finally:
        orch.annotation_layer.enabled = True
    step()
    assert len(_notes()) == 1, "and re-enabling brings it back"
    print("  OK: hiding the layer kept the note and re-showing restored it")


def test_the_layer_short_circuits_before_touching_imgui():
    """The zero-cost-when-off guarantee, asserted rather than assumed.

    `draw()` must return before it reaches a draw list, which is also what
    keeps it safe to call with no imgui frame. Checked by handing it a
    deliberately broken camera: if the guard works nothing touches it, and if
    it is ever removed this raises instead of silently costing work.
    """
    layer = orch.annotation_layer
    store = _notes()
    _draw_tool()
    drag_world((20.0, 20.0), (24.0, 20.0))
    assert len(store) >= 1, "need a note, or the empty-store guard hides this"

    class Exploding:
        zoom = 32.0

        def world_to_screen(self, *a):
            raise AssertionError("draw() projected with the layer off")

    was = layer.enabled
    try:
        layer.enabled = False
        assert layer.draw(store, Exploding(), (800, 600)) == 0
        # `active` is the other half of the same guard.
        layer.enabled = True
        layer.active = False
        assert layer.draw(store, Exploding(), (800, 600)) == 0
    finally:
        layer.enabled = was
        layer.active = orch.draw_ui
    print("  OK: draw() returned without projecting or calling imgui")


def test_a_hidden_note_is_not_clickable():
    """Disabling is not just visual: an invisible note must not eat clicks."""
    _text_tool()
    drag_world((10.0, 10.0), (16.0, 8.0))
    box = _notes().items[0]
    press_key(glfw.KEY_Q)
    orch.inspect_note = None
    try:
        orch.annotation_layer.enabled = False
        put_cursor_at(13.0, 9.0)
        click()
        assert orch.inspect_note is None, (
            "a hidden note must not be selectable by clicking where it is")
    finally:
        orch.annotation_layer.enabled = True
    assert _notes().get(box.aid) is not None, "and must still exist"
    print("  OK: a hidden note ignored a click on its position")


def test_a_hidden_note_is_not_rectangle_erased():
    _draw_tool()
    drag_world((20.0, 10.0), (24.0, 10.0))
    assert len(_notes()) == 1
    try:
        orch.annotation_layer.enabled = False
        enter_erase_mode()
        drag_cells((19, 9), (25, 11))
        assert len(_notes()) == 1, (
            "a hidden note must survive a rectangle erase")
        press_key(glfw.KEY_ESCAPE)
    finally:
        orch.annotation_layer.enabled = True
    print("  OK: the rectangle erase spared a hidden note")


# --- interaction with erase and copy --------------------------------------
def test_the_rectangle_erase_takes_notes_too():
    _draw_tool()
    drag_world((30.0, 10.0), (34.0, 10.0))
    drag_world((50.0, 10.0), (54.0, 10.0))
    assert len(_notes()) == 2
    enter_erase_mode()
    drag_cells((29, 9), (35, 11))
    assert len(_notes()) == 1, "the box should have taken the note inside it"
    assert _notes().items[0].bounds()[0] > 45.0, "the far note survives"
    press_key(glfw.KEY_ESCAPE)
    print("  OK: X's rectangle erased the note it covered")


def test_the_rectangle_erase_uses_the_bounding_box():
    """The stated rule, and the one that differs from the brush.

    An L-shaped stroke is taken by a rectangle clipping its empty bbox
    corner. That is deliberately the simple rule -- one statement, applied to
    every annotation type -- and this pins it so it is a decision rather than
    an accident.
    """
    _draw_tool()
    drag_world((40.0, 20.0), (46.0, 20.0), steps=6)
    s = _notes().items[0]
    for y in (21.0, 22.0, 23.0, 24.0, 25.0):
        s.points.append((46.0, y))
    assert not s.hit_point(41.0, 24.0), "no ink in the corner"
    enter_erase_mode()
    # A rectangle entirely inside the empty corner.
    drag_cells((40, 23), (42, 25))
    assert len(_notes()) == 0, (
        "the rectangle erase takes an annotation by its bounding box")
    press_key(glfw.KEY_ESCAPE)
    print("  OK: the rectangle took the stroke by its bbox, as specified")


def test_copying_a_selection_brings_its_notes():
    _belt_run((10, 30), (14, 30))
    _draw_tool()
    drag_world((11.0, 31.0), (13.0, 31.0), steps=4)
    assert len(_notes()) == 1

    stamp = capture(board, 9, 29, 15, 32, annotations=_notes())
    assert len(stamp) >= 4, "the belts should be in the stamp"
    assert len(stamp.annotations) == 1, (
        "the note's bbox is inside the rectangle, so it should be captured")
    print(f"  OK: the stamp carried {len(stamp)} entities and "
          f"{len(stamp.annotations)} note")


def test_a_note_alone_makes_a_real_stamp():
    """An annotation-only selection is copyable.

    A block of commentary is worth reusing independently of any machines, so
    notes count towards whether a stamp holds anything. `len()` stays the
    *entity* count, since that is what the status line and the type menu
    mean by it.
    """
    _draw_tool()
    drag_world((40.0, 30.0), (44.0, 30.0))
    stamp = capture(board, 39, 29, 45, 31, annotations=_notes())
    assert stamp, "a selection holding only notes is still a stamp"
    assert len(stamp) == 0, "but its entity count is zero"
    assert len(stamp.annotations) == 1
    # The box comes from the annotations, since there is nothing else to
    # measure -- a zero box would centre the paste on nothing.
    _x, _y, w, h = stamp.bounds()
    assert w >= 1 and h >= 1, f"an annotation-only stamp needs a box: {w}x{h}"
    print(f"  OK: notes alone gave a real {w}x{h} stamp with 0 entities")


def test_an_empty_selection_is_still_empty():
    """The other half: nothing at all is not a stamp.

    Worth stating separately now that notes count -- the guard that returns
    the player to their tool rather than into a paste mode with nothing to
    place keys off this.
    """
    stamp = capture(board, 40, 40, 44, 44, annotations=_notes())
    assert not stamp, "a selection over bare board catches nothing"
    print("  OK: a selection over bare board is still an empty stamp")


def test_an_annotation_only_stamp_pastes():
    _draw_tool()
    drag_world((10.0, 30.0), (14.0, 30.0), steps=5)
    assert len(_notes()) == 1

    ctrl_key(glfw.KEY_C)
    drag_cells((9, 29), (15, 31))
    assert orch.clipboard.pasting, (
        "copying notes alone should still reach paste mode")
    stamp = orch.clipboard.stamp
    assert len(stamp) == 0 and len(stamp.annotations) == 1

    put_cursor_at_cell(40, 30)
    step()
    click()
    assert len(_notes()) == 2, (
        f"the paste should have added a note, got {len(_notes())}")
    pasted = _notes().items[-1]
    assert pasted.bounds()[0] > 35.0, (
        f"the pasted note should be at the paste site: {pasted.bounds()}")
    press_key(glfw.KEY_ESCAPE)
    print("  OK: an annotation-only stamp pasted at the cursor")


def test_cutting_notes_alone_takes_them_and_holds_them():
    _draw_tool()
    drag_world((10.0, 50.0), (14.0, 50.0), steps=5)
    aid = _notes().items[0].aid
    enter_cut_mode()
    drag_cells((9, 49), (15, 51))
    assert _notes().get(aid) is None, "the cut should have taken the note"
    assert orch.clipboard.pasting, "and left us holding it in paste mode"
    assert len(orch.clipboard.stamp.annotations) == 1
    put_cursor_at_cell(40, 50)
    step()
    click()
    assert len(_notes()) == 1, "pasting it back should restore one note"
    assert _notes().items[0].bounds()[0] > 35.0
    press_key(glfw.KEY_ESCAPE)
    print("  OK: ctrl+X cut a note on its own and pasted it back")


def test_pasting_a_stamp_places_its_notes():
    _belt_run((10, 40), (14, 40))
    _draw_tool()
    drag_world((11.0, 41.0), (13.0, 41.0), steps=4)
    before = len(_notes())
    assert before == 1

    ctrl_key(glfw.KEY_C)
    drag_cells((9, 39), (15, 42))
    assert orch.clipboard.pasting, "the copy should have gone to paste mode"
    assert len(orch.clipboard.stamp.annotations) == 1

    put_cursor_at_cell(30, 40)
    step()
    click()
    assert len(_notes()) == before + 1, (
        f"the paste should have added one note, got {len(_notes())}")
    pasted = _notes().items[-1]
    assert pasted.bounds()[0] > 25.0, (
        f"the pasted note should be near the paste site, not at "
        f"{pasted.bounds()}")
    press_key(glfw.KEY_ESCAPE)
    print("  OK: pasting the stamp placed a copy of its note at the new site")


def test_a_rotated_stamp_keeps_its_text_upright():
    """Position rotates with the stamp; the glyphs never do.

    Text has a reading orientation a mark does not, so a rotated label must
    move with its machines and stay readable. The box's w/h not swapping is
    the observable form of "the text did not turn".
    """
    from core.stamp import Stamp, StampEntry

    entries = [StampEntry(cell_cls, i, 0, cell.DIR_PX)
               for i, cell_cls in enumerate([_belt_cls()] * 4)]
    label = TextBox(None, 1.0, 1.0, 2.0, 0.5, "ADD")
    stamp = Stamp(entries, [label])
    before = (label.w, label.h)
    centre_before = (label.x + label.w / 2, label.y - label.h / 2)

    turned = stamp.rotated()
    got = turned.annotations[0]
    assert (got.w, got.h) == before, (
        f"the text box must not swap extent: {before} -> {(got.w, got.h)}")
    centre_after = (got.x + got.w / 2, got.y - got.h / 2)
    assert centre_after != centre_before, (
        "but its position must move with the stamp")

    # Four quarter turns is the identity, which is the real proof the
    # rotation is a rotation and not a drift.
    r = stamp
    for _ in range(4):
        r = r.rotated()
    back = r.annotations[0]
    assert abs(back.x - label.x) < 1e-9 and abs(back.y - label.y) < 1e-9, (
        f"four turns should return the label to {(label.x, label.y)}, "
        f"got {(back.x, back.y)}")
    print("  OK: a rotated stamp moved its label and left the text upright")


def test_a_mirrored_stamp_keeps_its_text_upright():
    from core.stamp import Stamp, StampEntry
    entries = [StampEntry(_belt_cls(), i, 0, cell.DIR_PX) for i in range(4)]
    label = TextBox(None, 0.0, 1.0, 2.0, 0.5, "SUM")
    stamp = Stamp(entries, [label])
    m = stamp.mirrored()
    got = m.annotations[0]
    assert (got.w, got.h) == (label.w, label.h), "extent preserved"
    assert got.x != label.x, "but reflected across the box"
    # Mirroring twice is the identity.
    back = m.mirrored().annotations[0]
    assert abs(back.x - label.x) < 1e-9, (back.x, label.x)
    print("  OK: a mirrored stamp reflected its label and kept it readable")


def _belt_cls():
    from core.entity import Belt
    return Belt


# --- clearing and persistence ---------------------------------------------
def test_delete_clears_the_notes_with_the_board():
    _draw_tool()
    drag_world((10.0, 12.0), (14.0, 12.0))
    assert len(_notes()) == 1
    press_key(glfw.KEY_DELETE)
    assert len(_notes()) == 0, "Delete should take the notes with the factory"
    assert orch.inspect_note is None
    print("  OK: Delete cleared the notes along with the board")


def test_notes_survive_a_save_and_load(tmp_name="_annotations_test.npz"):
    _belt_run((10, 20), (13, 20))
    _text_tool()
    drag_world((10.0, 22.0), (16.0, 21.0))
    box = _notes().items[0]
    orch._set_note_text(box.aid, "REGISTER FILE")
    _draw_tool()
    drag_world((10.0, 19.0), (14.0, 19.0), steps=5)
    assert len(_notes()) == 2

    path = os.path.abspath(tmp_name)
    try:
        engine.save_board(path, annotations=_notes())
        # Wipe, then load it back.
        _notes().clear()
        orch.inspect_note = None
        engine.clear_board()
        assert len(_notes()) == 0
        engine.load_board(path, annotations=_notes())
        assert len(_notes()) == 2, (
            f"both notes should have come back, got {len(_notes())}")
        texts = [a.text for a in _notes() if isinstance(a, TextBox)]
        assert texts == ["REGISTER FILE"], texts
        strokes = [a for a in _notes() if isinstance(a, Stroke)]
        assert len(strokes) == 1 and len(strokes[0].points) >= 3
        print(f"  OK: {len(_notes())} notes survived a save and load")
    finally:
        if os.path.exists(path):
            os.remove(path)


def test_an_older_save_loads_as_having_no_notes(tmp_name="_annot_old.npz"):
    """Every existing save predates this field, so its absence is normal.

    And the outgoing board's notes must not linger over the incoming one,
    which is why the load clears unconditionally before reading.
    """
    import numpy as np
    from core import persistence

    _belt_run((10, 25), (13, 25))
    path = os.path.abspath(tmp_name)
    try:
        engine.save_board(path, annotations=None)
        # Rewrite the file without the annotations key at all, which is what
        # a pre-annotation build would have produced.
        with np.load(path) as fh:
            data = {k: fh[k] for k in fh.files if k != "annotations"}
        assert "annotations" not in data
        np.savez_compressed(path, **data)

        _draw_tool()
        drag_world((40.0, 40.0), (44.0, 40.0))
        assert len(_notes()) == 1, "a note to be left behind"

        engine.load_board(path, annotations=_notes())
        assert len(_notes()) == 0, (
            "an older save must load as no notes, and must not leave the "
            "previous board's behind")
        print("  OK: a save without the field loaded as having no notes")
    finally:
        if os.path.exists(path):
            os.remove(path)


def test_a_corrupt_note_blob_costs_the_notes_not_the_board(
        tmp_name="_annot_bad.npz"):
    import numpy as np

    _belt_run((10, 27), (14, 27))
    want = board.live_count
    path = os.path.abspath(tmp_name)
    try:
        engine.save_board(path, annotations=_notes())
        with np.load(path) as fh:
            data = {k: fh[k] for k in fh.files}
        data["annotations"] = np.array("{not json at all")
        np.savez_compressed(path, **data)
        n = engine.load_board(path, annotations=_notes())
        assert n == want, f"the factory should still load: {n} != {want}"
        assert len(_notes()) == 0
        print("  OK: a corrupt note blob lost the notes and kept the factory")
    finally:
        if os.path.exists(path):
            os.remove(path)


# --- tool plumbing ---------------------------------------------------------
def test_an_annotation_tool_is_not_a_placement_tool():
    """It must never reach the build path or claim an entity type.

    `entity_cls` being None is what keeps the pipette from resolving a board
    type onto one of these -- Q looks up the slot whose tool claims the type
    under the cursor.
    """
    tool = _draw_tool()
    assert tool.entity_cls is None
    assert not hasattr(tool, "plan") or not callable(
        getattr(tool, "plan", None)), (
        "an annotation tool should not offer a plan() the engine could call")
    # Holding one and clicking the board must build nothing.
    before = board.live_count
    put_cursor_at_cell(20, 20)
    click()
    assert board.live_count == before, (
        "clicking with an annotation tool must not place an entity")
    assert orch.last_plan is None
    print("  OK: the annotation tool placed nothing and claims no type")


def test_the_note_tools_persist_their_pen():
    from core import config
    tool = _draw_tool()
    press_key(glfw.KEY_R)
    press_key(glfw.KEY_F)
    want = (tool.colour, tool.size)
    data = config.toolbar_to_data(orch.toolbelt.slots)
    rebuilt = config.toolbar_from_data(data, len(orch.toolbelt.slots))
    same = [t for t in rebuilt if t is not None and t.name == tool.name]
    assert same, "the draw tool should have survived the round trip"
    assert (same[0].colour, same[0].size) == want, (
        f"pen lost: {want} -> {(same[0].colour, same[0].size)}")
    print(f"  OK: the pen {want} survived a toolbar round trip")


def test_a_note_tool_suppresses_the_camera_drag_and_entity_erase():
    """Left and right are spoken for, as copy and erase mode also do.

    Left-drag must draw rather than pan, and right-drag must erase notes
    rather than entities -- otherwise the specified gestures would fight the
    camera and the eraser.
    """
    _belt_run((20, 50), (24, 50))
    cells_before = [x for x in range(20, 25) if not board.is_empty(x, 50)]
    assert len(cells_before) >= 4

    centre_before = tuple(orch.camera.center)
    _draw_tool()
    drag_world((40.0, 60.0), (46.0, 60.0), steps=5)
    assert tuple(orch.camera.center) == centre_before, (
        "a left-drag with a note tool must draw, not pan the camera")
    # Right-drag across the belts must take notes, not belts.
    drag_world((20.5, 50.5), (24.5, 50.5), steps=5, button="right")
    cells_after = [x for x in range(20, 25) if not board.is_empty(x, 50)]
    assert cells_after == cells_before, (
        "a right-drag with a note tool must not erase entities")
    print("  OK: the note tool took both buttons without panning or erasing")
