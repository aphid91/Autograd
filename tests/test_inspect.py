"""The Status window, the Source's emit value, and the save filename box.

Three features that share one property: they are all *configuration* rather
than board geometry, and configuration is where state leaks between layers.
So these tests are mostly about ownership -- that the panel only requests,
that the orchestrator only applies, and that nothing caches an entity view
across a frame.

The panel itself is not drawn here: the headless harness has no imgui
backend, so `orch.draw_ui` is False and `draw_status()` never runs. What is
tested is the state it reads and the requests it returns, which is where the
bugs would be. The one thing that cannot be covered this way is the widget
layout, which is also the thing least likely to break silently.
"""

import os
import tempfile

import glfw

from core import cell, config
from core.builders import quadrant_dir
from core.entity import Belt, Shear, Sink, Source
from ui.panel import UIRequests

from tests import harness
from tests.harness import (board, click, ctrl_key, drag_cells, engine, fake,
                           ok, orch, press_key, put_cursor_at_cell,
                           select_tool, step)


# --- clicking with an empty hand -------------------------------------------
def test_clicking_a_structure_with_an_empty_hand_inspects_it():
    """The empty hand's one gesture. Nothing is built, nothing is erased."""
    src = engine.place_structure(Source, 10, 10)
    engine.sync_segments()
    assert orch.toolbelt.empty_handed, "harness.reset() leaves the hand empty"
    assert orch.inspect_row is None

    before = board.live_count
    put_cursor_at_cell(10, 10)
    step()
    click()

    assert orch.inspect_row == src.row, (orch.inspect_row, src.row)
    assert board.live_count == before, "inspecting must not build or erase"
    ok("clicking a Source with an empty hand opened Status on it")


def test_clicking_empty_board_closes_the_status_window():
    """Click-away-to-dismiss, which is why bare board is not simply ignored."""
    engine.place_structure(Source, 10, 10)
    engine.sync_segments()
    put_cursor_at_cell(10, 10)
    step()
    click()
    assert orch.inspect_row is not None

    put_cursor_at_cell(30, 30)
    step()
    click()
    assert orch.inspect_row is None, "clicking bare board should dismiss"
    ok("clicking empty board closed the Status window")


def test_inspecting_ignores_ghosts():
    """A preview ghost under the cursor must not be inspectable.

    Same reasoning as the pipette: a ghost is about to be despawned, so a
    window opened on one would be reading a dead row a frame later. An empty
    hand has no preview of its own, so this is reachable only on the frame a
    tool was just put down -- which is exactly when it would happen.
    """
    select_tool("Shear")
    put_cursor_at_cell(10, 10)
    step()
    assert engine.ghost_rows, "the structure tool should be previewing"
    ghost_row = engine.ghost_rows[0]

    # Q empties the hand, leaving the ghost up for this frame.
    harness.press_key(glfw.KEY_Q)
    step()
    put_cursor_at_cell(10, 10)
    step()
    click()
    assert orch.inspect_row != ghost_row, "inspected a ghost row"
    ok("inspecting skipped the preview ghost")


def test_a_held_tool_still_builds_rather_than_inspecting():
    """Inspect is the *empty* hand's gesture, not a global one."""
    select_tool("Shear")
    put_cursor_at_cell(10, 10)
    step()
    click()
    step()
    assert orch.inspect_row is None, "a held tool must not open Status"
    assert board.live_count > 0, "the click should have built"
    ok("a click with a tool held built instead of inspecting")


def test_erasing_the_inspected_entity_closes_the_window():
    """`inspected()` must report None once the row dies.

    The window stores a row, and a freed row goes back on the free list to be
    handed to something unrelated. Reading it would show another entity's
    state under the erased one's name.
    """
    src = engine.place_structure(Source, 10, 10)
    engine.sync_segments()
    put_cursor_at_cell(10, 10)
    step()
    click()
    assert orch.inspected() is not None

    engine.erase(10, 10)
    assert orch.inspected() is None, "a dead row must not be inspectable"

    # And the panel turns that into a close request rather than rendering.
    req = UIRequests()
    orch.ui.draw_status(orch, req)
    assert req.close_status, "draw_status should request a close"
    ok("erasing the inspected entity closed Status")


def test_inspected_never_returns_a_stale_view():
    """The row is re-resolved per call, so a reused row cannot masquerade.

    This is the hazard the "never cache a view across frames" rule exists
    for. Erase the inspected entity and spawn a different type, which the
    free list hands the same row -- `inspected()` must not report the new
    entity as the one that was clicked.
    """
    src = engine.place_structure(Source, 10, 10)
    row = src.row
    put_cursor_at_cell(10, 10)
    step()
    click()
    assert orch.inspect_row == row

    engine.erase(10, 10)
    reused = engine.place_structure(Shear, 20, 20)
    if reused.row != row:
        # The allocator did not reuse it; the assertion below is then vacuous
        # but the important half (a dead row reads as None) still holds.
        assert orch.inspected() is None
        ok("inspected() reported None for the freed row")
        return
    # The row came back as a different entity. inspect_row is a dumb index,
    # so this is the one case the panel has to tolerate: it renders whatever
    # now lives there rather than crashing. What must not happen is reading a
    # *dead* row, which the previous test covers.
    assert isinstance(orch.inspected(), Shear)
    ok("inspected() re-resolved a reused row rather than caching a view")


# --- the Source's emit value -----------------------------------------------
def test_a_placed_source_starts_at_the_type_default():
    """`initialise()` seeds EMIT_VALUE over whatever the row last held.

    EMIT_VALUE is 0.0 and so is a fresh row, so placing onto a clean row
    asserts nothing about whether `initialise()` writes the field at all.
    The test therefore **recycles a row that holds a different value**: place
    a configured Source, erase it so the row returns to the free list, and
    place another expecting the free list to hand the same row back. That is
    the case a dropped `initialise()` would break -- a new Source inheriting
    the previous one's setting.
    """
    first = engine.place_structure(Source, 10, 10)
    row = first.row
    first.emit_value = 77.5
    engine.erase(10, 10)

    src = engine.place_structure(Source, 10, 10)
    assert src.row == row, (
        f"expected the free list to reuse row {row}, got {src.row}; the test "
        f"needs a recycled row to have anything to prove")
    assert src.emit_value == Source.EMIT_VALUE, (
        f"a placed Source emits {src.emit_value}, inherited from the erased "
        f"one rather than reset to {Source.EMIT_VALUE}")
    ok(f"a placed Source emits {Source.EMIT_VALUE}, even on a recycled row")


def test_the_emit_value_is_per_instance():
    """Two Sources on one board must be able to emit different numbers.

    The whole reason the value moved into the entity row. With it on the
    class, setting one would have set every Source in the game.
    """
    a = engine.place_structure(Source, 10, 10)
    b = engine.place_structure(Source, 10, 20)
    a.emit_value = 5.0
    assert a.emit_value == 5.0
    assert b.emit_value == Source.EMIT_VALUE, (
        f"setting one Source changed another: {b.emit_value}")
    assert Source.EMIT_VALUE == 0.0, "the class default should be untouched"
    ok("two Sources held different emit values independently")


def test_a_source_emits_its_own_value_onto_a_belt():
    """The configured value must reach the belt, not just the row.

    `fire()` reads `emit_value`, so this is what pins the row field to the
    actual simulation rather than to an accessor nothing consults.
    """
    src = engine.place_structure(Source, 10, 10)
    src.emit_value = 42.0
    for x in range(12, 18):
        engine.place_belt(x, 10, cell.DIR_PX)
    engine.sync_segments()

    for _ in range(10):
        engine.clock.request_step()
        engine.clock.advance(0.0)

    seg = engine.segments.at(13, 10)
    assert seg is not None and seg.count > 0, "nothing reached the belt"
    payloads = {engine.belts.payload_of(seg, k) for k in range(seg.head, seg.tail)}
    assert payloads == {42.0}, f"belt carried {payloads}, expected 42.0"
    ok("a Source emitted its configured value onto the belt")


def test_changing_the_emit_value_flushes_material_in_flight():
    """The 'start fresh' event, which is why the setter is not a bare assign.

    Without the flush the old value keeps arriving for as long as the longest
    run takes to drain, mixed with the new one -- which reads as the setting
    not having taken.
    """
    src = engine.place_structure(Source, 10, 10)
    # A distinctive value, not the type default. The default is 0.0 now, which
    # is also what an *absent* payload reads as -- so asserting the belt
    # carries the default would pass on a belt carrying nothing, and the
    # flush below would then have nothing to prove.
    src.emit_value = 3.25
    for x in range(12, 20):
        engine.place_belt(x, 10, cell.DIR_PX)
    engine.sync_segments()
    for _ in range(12):
        engine.clock.request_step()
        engine.clock.advance(0.0)

    seg = engine.segments.at(13, 10)
    assert seg.count > 0, "need items in flight to flush"
    old = {engine.belts.payload_of(seg, k) for k in range(seg.head, seg.tail)}
    assert old == {3.25}, old

    orch._set_emit(src.row, 9.0)
    assert seg.count == 0, (
        f"{seg.count} items survived the emit change; stale material would "
        f"keep arriving mixed with the new value")
    assert src.emit_value == 9.0

    # And what arrives afterwards is only ever the new value.
    for _ in range(12):
        engine.clock.request_step()
        engine.clock.advance(0.0)
    fresh = {engine.belts.payload_of(seg, k) for k in range(seg.head, seg.tail)}
    assert fresh == {9.0}, f"after the change the belt carried {fresh}"
    ok("changing a Source's value flushed every item and emitted the new one")


def test_the_flush_clears_gate_state_too():
    """`clear_items()` alone leaves material inside the machines.

    A value already pulled off a belt sits in a gate's input channel and a
    computed result sits in its output zone, so clearing only the rings
    leaves material that re-enters the network next tick.
    """
    src = engine.place_structure(Source, 10, 10)
    gate = engine.place_structure(Shear, 20, 20)
    gate.set_input(0, 1.0)
    gate.set_input(1, 2.0)
    gate.set_output(0, 3.0)
    engine.sync_segments()

    engine.flush_material()
    assert not gate.in_held(0) and not gate.in_held(1), "inputs survived"
    assert gate.outputs_clear(), "a waiting zone survived the flush"
    ok("flush_material() emptied gate channels as well as the belts")


def test_setting_the_same_value_is_not_a_flush():
    """Re-entering the current number must not nuke the board's items.

    imgui's input_float reports a change on every keystroke, so a player
    typing "-1" into a Source already at -1 would otherwise wipe every belt
    mid-edit.
    """
    src = engine.place_structure(Source, 10, 10)
    for x in range(12, 18):
        engine.place_belt(x, 10, cell.DIR_PX)
    engine.sync_segments()
    for _ in range(10):
        engine.clock.request_step()
        engine.clock.advance(0.0)
    seg = engine.segments.at(13, 10)
    assert seg.count > 0

    orch._set_emit(src.row, src.emit_value)
    assert seg.count > 0, "setting the same value should not flush"
    ok("re-setting the same emit value left items alone")


def test_set_emit_tolerates_a_dead_row():
    """Requests are built from the previous frame, so the row may be gone."""
    src = engine.place_structure(Source, 10, 10)
    row = src.row
    engine.erase(10, 10)
    orch._set_emit(row, 3.0)      # must not raise
    ok("_set_emit ignored a freed row")


def test_set_emit_ignores_a_non_source():
    """The row could have been reused by another type between frames."""
    gate = engine.place_structure(Shear, 10, 10)
    orch._set_emit(gate.row, 3.0)   # must not raise or write
    ok("_set_emit ignored a non-Source row")


def test_a_copied_source_keeps_its_emit_value():
    """A configured Source must survive copy/paste.

    The paste looks identical either way, so losing the value here would be
    invisible until the numbers coming off the belt were wrong.
    """
    src = engine.place_structure(Source, 10, 10)
    src.emit_value = 17.5
    engine.sync_segments()

    ctrl_key(glfw.KEY_C)
    drag_cells((9, 9), (12, 11))
    stamp = orch.clipboard.stamp
    assert stamp is not None and len(stamp) == 1, stamp
    assert stamp.entries[0].emit == 17.5, (
        f"capture dropped the emit value: {stamp.entries[0].emit}")

    put_cursor_at_cell(30, 30)
    step()
    click()
    step()
    pasted = engine.entity_at(30, 30)
    assert isinstance(pasted, Source), pasted
    assert pasted.row != src.row
    assert pasted.emit_value == 17.5, (
        f"the pasted Source emits {pasted.emit_value}, not 17.5")
    ok("a copied Source pasted with its emit value intact")


def test_a_rotated_stamp_keeps_the_emit_value():
    """The value is orientation-independent, so R must carry it through."""
    src = engine.place_structure(Source, 10, 10)
    src.emit_value = -4.25
    engine.sync_segments()
    ctrl_key(glfw.KEY_C)
    drag_cells((9, 9), (12, 11))

    harness.press_key(glfw.KEY_R)
    step()
    assert orch.clipboard.stamp.entries[0].emit == -4.25, (
        "rotating the stamp dropped the emit value")
    ok("rotating a stamp preserved the Source's emit value")


def test_a_freshly_placed_source_does_not_inherit_a_copied_value():
    """Only a paste carries a value; the toolbelt places the default.

    `Placement.emit` defaults to None for exactly this -- otherwise copying a
    configured Source would quietly change what the Source *tool* places.
    """
    src = engine.place_structure(Source, 10, 10)
    src.emit_value = 99.0
    engine.sync_segments()
    ctrl_key(glfw.KEY_C)
    drag_cells((9, 9), (12, 11))
    harness.press_key(glfw.KEY_ESCAPE)
    step()

    select_tool("Source")
    put_cursor_at_cell(30, 30)
    step()
    click()
    step()
    placed = engine.entity_at(30, 30)
    assert isinstance(placed, Source), placed
    assert placed.emit_value == Source.EMIT_VALUE, (
        f"the Source tool placed one emitting {placed.emit_value}")
    ok("the Source tool still places the type default after a copy")


def test_the_emit_value_survives_a_save_round_trip():
    """It lives in the entity row, so persistence gets it for free.

    Worth pinning anyway: the row is saved wholesale, so a future change that
    moved F_EMIT out of the row would break this silently.
    """
    path = os.path.join(tempfile.gettempdir(), "gates_emit_roundtrip.npz")
    src = engine.place_structure(Source, 10, 10)
    src.emit_value = 6.5
    try:
        engine.save_board(path)
        engine.clear_board()
        step()
        engine.load_board(path)
        step()
        back = engine.entity_at(10, 10)
        assert isinstance(back, Source), back
        assert back.emit_value == 6.5, back.emit_value
    finally:
        if os.path.exists(path):
            os.remove(path)
    ok("a Source's emit value survived a save/load round trip")


# --- a belt's seed ---------------------------------------------------------
# A seeded belt cell puts an item on its run every time segments are derived.
# Like the Source's emit value it is per-instance configuration living in the
# entity row, so these tests cover the same ownership questions -- plus one
# the Source does not have: the seed has to *fire*, and firing is a side
# effect of the segment sweep rather than of the setter.


def _seeded_run(cells, value=1.0, a=(10, 10), b=(15, 10)):
    """A belt run with `cells` seeded at `value`. Returns its segment."""
    select_tool("Belt")
    engine.place_belt_path(a, b)
    engine.sync_segments()
    for xy in cells:
        view = board.entity_at(*xy)
        orch._set_seed(view.row, True, value)
    engine.sync_segments()
    return engine.segments.at(*a)


def test_a_placed_belt_is_not_seeded():
    """The default has to be off: an untouched belt must inject nothing."""
    engine.place_belt(10, 10)
    engine.sync_segments()
    view = board.entity_at(10, 10)
    assert not view.seed_on, "a freshly drawn belt must not be seeded"
    assert engine.segments.at(10, 10).count == 0
    # The value sits at the type default, ready for the box to be ticked.
    # That default is 0.0 now, which a fresh row also reads as -- so this
    # line no longer distinguishes "seeded the default" from "wrote nothing".
    # `core/entity.py`'s self-check covers that on a dirtied row; here the
    # load-bearing half is the flag being off.
    assert view.seed_value == Belt.SEED_VALUE, view.seed_value
    ok("a placed belt starts unseeded, with the default value ready")


def test_a_seeded_belt_puts_its_value_on_the_run():
    seg = _seeded_run([(12, 10)], value=2.5)
    assert seg.count == 1, seg.count
    assert engine.belts.payload_of(seg, seg.head) == 2.5
    ok("a seeded cell put one item of its value on the run")


def test_zero_is_a_seedable_value():
    """Which is why the flag is separate from the value.

    Nothing distinguishes "seeds zero" from "not seeded" if the value alone
    carries the state -- the same trap Source.initialise() exists to avoid.
    """
    seg = _seeded_run([(12, 10)], value=0.0)
    assert seg.count == 1, "zero must still seed"
    assert engine.belts.payload_of(seg, seg.head) == 0.0
    ok("a seed of zero produced an item, not an absence")


def test_the_seed_fires_again_on_every_rebuild():
    """The point of the feature: it survives the sweep that empties the run.

    An edit anywhere drops every item on every belt (positions are relative
    to a segment's anchor, so they cannot survive re-derivation). A seed is
    the standing instruction that refills afterwards, which is what makes it
    different from simply pushing an item once.
    """
    seg = _seeded_run([(12, 10)])
    assert seg.count == 1

    # An edit on the far side of the board: enough to invalidate, unrelated
    # to this run.
    engine.place_belt(40, 40)
    engine.sync_segments()
    seg = engine.segments.at(10, 10)
    assert seg.count == 1, (
        f"the seed should have re-fired after the rebuild, got {seg.count}")
    assert engine.belts.payload_of(seg, seg.head) == 1.0
    ok("the seed re-fired after an unrelated edit rebuilt the segments")


def test_several_seeds_on_one_run_give_several_items():
    """One item per seeded cell, nose to tail.

    They all enter on the same tick, so this needs the staggered entry ticks
    in `Engine.seed_segments()`: pushed at the same tick, the first lands at
    position 0 and `has_room()` then refuses every one after it -- four
    seeded cells produced a single item.
    """
    seg = _seeded_run([(10, 10), (11, 10), (12, 10), (13, 10)])
    assert seg.count == 4, f"expected one item per seeded cell, got {seg.count}"

    # Spaced by exactly the transport's spacing, and in order -- the state a
    # producer running at full rate would have reached.
    tick = engine.clock.tick
    pos = engine.belts.positions(seg, tick)
    assert pos == sorted(pos, reverse=True), pos
    gaps = [pos[i - 1] - pos[i] for i in range(1, len(pos))]
    assert all(g == engine.belts.spacing for g in gaps), gaps

    # And the simulation's own invariants hold, which is the real check: the
    # audit tests ordering, spacing and the anchor independently of this.
    assert not engine.belts.audit(tick), engine.belts.audit(tick)
    assert not engine.segments.audit(), engine.segments.audit()
    ok("four seeded cells gave four correctly spaced items")


def test_a_run_shorter_than_its_seeds_takes_what_fits():
    """Seeds go through the same room check as any other push.

    So a run with more seeded cells than it can physically hold takes as many
    as fit and drops the rest, rather than overfilling the ring.
    """
    select_tool("Belt")
    engine.place_belt_path((30, 30), (31, 30))
    engine.sync_segments()
    for x in (30, 31):
        orch._set_seed(board.entity_at(x, 30).row, True, 1.0)
    engine.sync_segments()
    seg = engine.segments.at(30, 30)
    cap = engine.belts.capacity_of(seg)
    assert 0 < seg.count <= cap, (seg.count, cap)
    assert not engine.belts.audit(engine.clock.tick), (
        engine.belts.audit(engine.clock.tick))
    ok(f"a 2-cell run with 2 seeds holds {seg.count}, within capacity {cap}")


def test_the_seeded_cell_is_tinted_by_its_sign():
    """The flags the renderer reads, mirrored into .z.

    The cell carries the *sign* rather than the value, which is all a tint
    can convey. Changing either half through the row alone would leave the
    grid stale, so this is really a test that `_set_seed` restamps.
    """
    engine.place_belt(10, 10)
    engine.sync_segments()
    row = board.entity_at(10, 10).row

    z = int(board.grid[10, 10, cell.CH_Z])
    assert not cell.is_seeded(z), "an unseeded cell must not claim a seed"

    orch._set_seed(row, True, 3.0)
    z = int(board.grid[10, 10, cell.CH_Z])
    assert cell.is_seeded(z) and cell.is_seed_positive(z)

    orch._set_seed(row, True, -3.0)
    z = int(board.grid[10, 10, cell.CH_Z])
    assert cell.is_seeded(z) and not cell.is_seed_positive(z), (
        "a negative seed must clear the sign bit")

    # Zero reads as negative, matching the item discs' own split.
    orch._set_seed(row, True, 0.0)
    z = int(board.grid[10, 10, cell.CH_Z])
    assert cell.is_seeded(z) and not cell.is_seed_positive(z)

    orch._set_seed(row, False, 3.0)
    z = int(board.grid[10, 10, cell.CH_Z])
    assert not cell.is_seeded(z), "unticking the box must clear the flag"
    ok("the cell's seed flags tracked the setting and its sign")


def test_turning_a_seed_off_stops_it_firing():
    seg = _seeded_run([(12, 10)])
    assert seg.count == 1
    orch._set_seed(board.entity_at(12, 10).row, False, 1.0)
    engine.sync_segments()
    seg = engine.segments.at(10, 10)
    assert seg.count == 0, f"an unticked seed still fired: {seg.count} items"
    ok("unticking the seed stopped it injecting")


def test_setting_the_same_seed_is_not_a_rebuild():
    """imgui reports a change per keystroke, so no-ops must stay no-ops.

    Same guard as the emit value's, and it matters more here: a rebuild
    empties every belt on the board, so re-applying the current value mid-edit
    would wipe the factory.
    """
    _seeded_run([(12, 10)])
    row = board.entity_at(12, 10).row
    before = engine.segments.stats["rebuilds"]
    orch._set_seed(row, True, 1.0)          # exactly what is already set
    engine.sync_segments()
    assert engine.segments.stats["rebuilds"] == before, (
        "re-setting the same seed forced a rebuild")
    ok("re-setting the same seed changed nothing")


def test_set_seed_tolerates_a_dead_or_wrong_row():
    """A request is built from the previous frame's state."""
    belt = engine.place_belt(10, 10)
    row = belt.row
    engine.erase(10, 10)
    orch._set_seed(row, True, 1.0)           # must not raise
    src = engine.place_structure(Source, 20, 20)
    orch._set_seed(src.row, True, 1.0)       # not a belt; must be ignored
    assert not hasattr(src, "seed_on") or not getattr(src, "seed_on", False)
    ok("_set_seed ignored a freed row and a non-belt")


def test_a_copied_belt_keeps_its_seed():
    """Per-instance configuration has to travel with a copy.

    Same reasoning as the Source's emit value: the paste looks identical, so
    a dropped seed would be invisible until the run failed to fill.
    """
    seg = _seeded_run([(12, 10)], value=4.0)
    assert seg.count == 1
    ctrl_key(glfw.KEY_C)
    drag_cells((9, 9), (16, 11))
    stamp = orch.clipboard.stamp
    assert stamp is not None and len(stamp) == 6, stamp
    seeds = [e.seed for e in stamp.entries if e.seed is not None]
    assert seeds == [(True, 4.0)], seeds

    # Paste it elsewhere and the seeded cell seeds there too.
    put_cursor_at_cell(12, 20)
    step()
    click()
    engine.sync_segments()
    pasted = [board.entity_at(x, 20) for x in range(10, 16)]
    on = [v for v in pasted if v is not None and v.seed_on]
    assert len(on) == 1, [v.seed_on for v in pasted if v is not None]
    assert on[0].seed_value == 4.0, on[0].seed_value
    assert engine.segments.at(10, 20).count == 1, "the pasted seed did not fire"
    ok("a copied belt pasted with its seed intact and firing")


def test_a_freshly_drawn_belt_does_not_inherit_a_copied_seed():
    """`Placement.seed` defaults to None for exactly this.

    Otherwise copying a seeded belt would make every later run the Belt tool
    drew inject items -- quietly, since the cells look the same.
    """
    _seeded_run([(12, 10)], value=4.0)
    ctrl_key(glfw.KEY_C)
    drag_cells((9, 9), (16, 11))
    assert orch.clipboard.stamp is not None, "setup: nothing was copied"
    press_key(glfw.KEY_ESCAPE)

    select_tool("Belt")
    engine.place_belt_path((10, 30), (13, 30))
    engine.sync_segments()
    for x in range(10, 14):
        view = board.entity_at(x, 30)
        assert not view.seed_on, f"({x},30) inherited a copied seed"
    assert engine.segments.at(10, 30).count == 0
    ok("the Belt tool still draws unseeded belts after copying a seeded one")


def test_the_seed_survives_a_save_round_trip():
    """It lives in the entity row, so persistence carries it wholesale."""
    path = os.path.join(tempfile.gettempdir(), "gates_seed_roundtrip.npz")
    _seeded_run([(12, 10)], value=5.5)
    try:
        engine.save_board(path)
        engine.clear_board()
        step()
        engine.load_board(path)
        engine.sync_segments()
        back = engine.entity_at(12, 10)
        assert isinstance(back, Belt), back
        assert back.seed_on and back.seed_value == 5.5, (
            back.seed_on, back.seed_value)
        # And it fires on the loaded board, which is a rebuild like any other.
        assert engine.segments.at(10, 10).count == 1
    finally:
        if os.path.exists(path):
            os.remove(path)
    ok("a belt's seed survived a save/load round trip and fired on load")


def test_the_panel_only_requests_a_seed_change():
    """The window never mutates the board; it returns a (row, on, value)."""
    req = UIRequests()
    assert req.set_seed is None
    # The orchestrator is what applies it, and it carries a row rather than a
    # view for the same reason inspect_row does.
    belt = engine.place_belt(10, 10)
    engine.sync_segments()
    req.set_seed = (belt.row, True, 2.0)
    assert isinstance(req.set_seed[0], int), "a row, not a view"
    orch._set_seed(*req.set_seed)
    assert board.entity_at(10, 10).seed_on
    ok("the panel's seed request is a (row, on, value) the orchestrator applies")


# --- the save filename box -------------------------------------------------
def test_an_empty_filename_box_uses_the_default():
    """The box is optional: empty means exactly the old behaviour."""
    orch.ui.save_name = ""
    from core import persistence
    assert orch._save_path() == persistence.DEFAULT_SAVE
    orch.ui.save_name = "   "
    assert orch._save_path() == persistence.DEFAULT_SAVE, (
        "whitespace should count as empty")
    ok("an empty filename box falls back to the default quicksave")


def test_a_bare_filename_gets_an_npz_extension():
    """`.npz` is the only format save() writes, so a bare name would lie."""
    assert config.resolve_save_path("mybase", "d.npz") == "mybase.npz"
    assert config.resolve_save_path("  mybase  ", "d.npz") == "mybase.npz"
    # An explicit extension is left alone, and so is a path.
    assert config.resolve_save_path("mybase.npz", "d.npz") == "mybase.npz"
    assert config.resolve_save_path("a/b.npz", "d.npz") == "a/b.npz"
    ok("filenames resolve to .npz without second-guessing an explicit one")


def test_the_filename_box_drives_save_and_load():
    """Both buttons must honour the box, or they would disagree."""
    name = os.path.join(tempfile.gettempdir(), "gates_named_slot.npz")
    orch.ui.save_name = name
    try:
        engine.place_structure(Source, 10, 10)
        okay, msg = orch._quicksave()
        assert okay, msg
        assert os.path.exists(name), f"saved somewhere else: {msg}"

        engine.clear_board()
        step()
        assert board.live_count == 0
        okay, msg = orch._quickload()
        assert okay, msg
        assert isinstance(engine.entity_at(10, 10), Source), msg
    finally:
        orch.ui.save_name = ""
        if os.path.exists(name):
            os.remove(name)
    ok("the filename box drove both quicksave and quickload")


def test_config_load_is_silent_about_a_missing_or_broken_file():
    """A first run has no file, and a corrupt one costs one filename."""
    missing = os.path.join(tempfile.gettempdir(), "gates_no_such_cfg.json")
    if os.path.exists(missing):
        os.remove(missing)
    assert config.load(missing) == dict(config.DEFAULTS)

    broken = os.path.join(tempfile.gettempdir(), "gates_broken_cfg.json")
    try:
        with open(broken, "w", encoding="utf-8") as fh:
            fh.write("{not json at all")
        assert config.load(broken) == dict(config.DEFAULTS)
        # A well-formed file of the wrong shape, too.
        with open(broken, "w", encoding="utf-8") as fh:
            fh.write('["a list"]')
        assert config.load(broken) == dict(config.DEFAULTS)
        # And a known key holding the wrong type.
        with open(broken, "w", encoding="utf-8") as fh:
            fh.write('{"save_name": 17}')
        assert config.load(broken)["save_name"] == ""
    finally:
        if os.path.exists(broken):
            os.remove(broken)
    ok("config.load() fell back to defaults on missing and malformed files")


def test_config_round_trips_the_save_name():
    path = os.path.join(tempfile.gettempdir(), "gates_cfg_roundtrip.json")
    try:
        assert config.save({"save_name": "base2"}, path)
        assert config.load(path)["save_name"] == "base2"
        # Unknown keys are dropped rather than accumulating.
        config.save({"save_name": "b3", "bogus": 1}, path)
        with open(path, "r", encoding="utf-8") as fh:
            assert "bogus" not in fh.read()
    finally:
        if os.path.exists(path):
            os.remove(path)
    ok("config.save()/load() round-tripped the filename")


def test_the_harness_never_inherits_the_players_filename():
    """The reason config.load() is called from app.py and nowhere else.

    The harness builds a real Orchestrator and a real UI. If either read the
    config in its constructor, every test run would inherit whatever the
    player last typed -- and the quicksave tests would then write there.
    """
    assert orch.ui.save_name == "", (
        f"the harness picked up a save name: {orch.ui.save_name!r}")
    from core import persistence
    assert orch._save_path() == persistence.DEFAULT_SAVE

    # And neither constructor actually calls into the config module. Checked
    # against the compiled code rather than the source text, so the comments
    # explaining *why* they must not do this don't trip the test.
    from ui.panel import UI
    from orchestrator import Orchestrator
    for cls in (UI, Orchestrator):
        names = cls.__init__.__code__.co_names
        assert "config" not in names, (
            f"{cls.__name__}.__init__ reads the config; that would give the "
            f"test harness the player's own filename")
    ok("neither UI nor Orchestrator reads the player's config at construction")


# --- the customisable toolbar ----------------------------------------------
#
# Same ownership concerns as the filename box, and one more: the toolbar is
# the first *structured* thing in the config file, so the validation matters
# as much as the round trip. A layout naming a machine this build does not
# have must cost one slot, not the whole arrangement or a crash on startup.

def test_the_type_menu_only_requests_a_slot_change():
    """The panel hands over a description; the orchestrator resolves it.

    Why it matters: the `"stamp"` kind's source is the live clipboard, which
    the panel has no business reaching into. Keeping construction in the
    orchestrator is what makes that boundary hold -- and it is the same
    discipline `set_emit` and `set_seed` follow.
    """
    from core.builders import RawBeltBuilder, StructureBuilder

    req = UIRequests()
    assert req.set_slot is None, "no request by default"

    # Structures carry a type name; the fixed kinds do not.
    req.set_slot = (0, "structure", "Floor")
    orch._set_slot(*req.set_slot)
    assert isinstance(orch.toolbelt.slots[0], StructureBuilder)
    assert orch.toolbelt.slots[0].entity_cls.__name__ == "Floor"

    orch._set_slot(1, "raw_belt", None)
    assert isinstance(orch.toolbelt.slots[1], RawBeltBuilder)

    # A None kind is the menu's "Clear slot" -- distinct from an unknown kind.
    orch._set_slot(1, None, None)
    assert orch.toolbelt.slots[1] is None

    # An unknown kind or machine fails softly and changes nothing.
    orch.toolbelt.set_slot(2, None)
    assert not orch._set_slot(2, "no_such_kind", None)
    assert not orch._set_slot(2, "structure", "NoSuchMachine")
    assert orch.toolbelt.slots[2] is None
    ok("the type menu's request resolved, cleared, and failed softly")


def test_assigning_a_slot_marks_the_layout_dirty_but_writes_nothing():
    """`app.py` persists; the orchestrator only flags. Same rule as save_name.

    The harness builds a real Orchestrator, so an orchestrator that wrote the
    file itself would have every test run overwrite the player's toolbar.
    """
    orch.toolbar_dirty = False
    orch._set_slot(0, "structure", "Floor")
    assert orch.toolbar_dirty, "a slot change must flag the layout for saving"

    # And nothing in the apply path reaches for the config module.
    from orchestrator import Orchestrator
    names = Orchestrator._set_slot.__code__.co_names
    assert "save" not in names, (
        "_set_slot writes the config file; app.py owns that")
    ok("assigning a slot flagged the layout without writing a file")


def test_the_toolbar_round_trips_through_the_config():
    """Every default tool must survive describe -> JSON -> make.

    Swept over the whole default layout rather than one example, because the
    failure mode is per *kind*: a tool with no `describe_builder()` branch
    saves as an empty slot, silently, and only for players who happened to
    have that tool somewhere in their bar.
    """
    import json
    from core.builders import default_builders
    from core.toolbelt import SLOT_COUNT

    tools = default_builders()
    slots = list(tools) + [None] * (SLOT_COUNT - len(tools))
    data = config.toolbar_to_data(slots)
    assert len(data) == SLOT_COUNT, len(data)
    # Every real tool described itself. An un-describable one would be None
    # here and would come back as an empty slot.
    for i, tool in enumerate(tools):
        assert data[i] is not None, (
            f"{type(tool).__name__} has no describe_builder() branch, so it "
            f"would vanish from a saved toolbar")

    # It has to survive actual JSON, not just a dict round trip -- tuples
    # become lists and that is where a seed pair can break.
    back = config.toolbar_from_data(json.loads(json.dumps(data)), SLOT_COUNT)
    assert back is not None
    assert [getattr(t, "name", None) for t in back] == \
        [getattr(t, "name", None) for t in slots]
    ok(f"all {len(tools)} default tools round-tripped through JSON")


def test_a_baked_stamp_round_trips_with_its_contents():
    """A stamp tool is the one kind carrying data rather than just a name.

    And the data is exactly the per-instance settings the matched-ghost
    feature exists to apply, so losing them on a restart would quietly
    defeat the reason for baking a stamp in the first place.
    """
    import json
    from core.builders import (StampBuilder, describe_builder, make_builder,
                               stamp_from_data)
    from core.stamp import Stamp, StampEntry

    stamp = Stamp([
        StampEntry(Source, 0, 0, cell.DIR_PY, emit=5.5),
        StampEntry(Belt, 2, 0, cell.DIR_NX, seed=(True, -3.25)),
    ])
    desc = json.loads(json.dumps(describe_builder(StampBuilder(stamp))))
    tool = make_builder("stamp", stamp=stamp_from_data(desc["stamp"]))
    assert tool is not None

    got = [(e.entity_cls.__name__, e.dx, e.dy, e.direction, e.emit, e.seed)
           for e in tool.stamp.entries]
    assert got == [("Source", 0, 0, cell.DIR_PY, 5.5, None),
                   ("Belt", 2, 0, cell.DIR_NX, None, (True, -3.25))], got
    ok("a baked stamp kept its emit value, its seed and its orientations")


def test_a_saved_layout_survives_an_unknown_entry():
    """One bad slot must cost one slot.

    The file is hand-editable and an older version may have written it, so
    an entry naming a machine that no longer exists is a thing that will
    happen. Refusing the whole layout would discard an arrangement built up
    over sessions; shifting the remaining tools left would be worse, since
    position *is* the content -- so a bad entry becomes an empty slot and
    everything around it stays put.
    """
    data = [
        {"kind": "belt"},
        {"kind": "structure", "type": "NoSuchMachine"},
        {"kind": "raw_belt"},
        "not even a dict",
        {"kind": "structure", "type": "Floor"},
    ]
    back = config.toolbar_from_data(data, 6)
    names = [getattr(t, "name", None) for t in back]
    assert names == ["Belt", None, "Raw belt", None, "Floor", None], names
    ok("an unknown entry emptied its own slot and shifted nothing")


def test_no_saved_layout_leaves_the_defaults_alone():
    """The first-run path: an absent layout must not blank the bar."""
    from core.toolbelt import SLOT_COUNT

    assert config.toolbar_from_data([], SLOT_COUNT) is None
    assert config.toolbar_from_data(None, SLOT_COUNT) is None
    assert config.toolbar_from_data("junk", SLOT_COUNT) is None
    # And `apply_toolbar(None)` is the no-op that relies on it.
    before = [getattr(t, "name", None) for t in orch.toolbelt.slots]
    assert not orch.apply_toolbar(None)
    after = [getattr(t, "name", None) for t in orch.toolbelt.slots]
    assert after == before, "apply_toolbar(None) changed the bar"

    # The default layout is unchanged by this feature existing: a first run
    # still gets exactly `default_builders()` in order.
    from core.builders import default_builders
    assert before[:len(default_builders())] == \
        [t.name for t in default_builders()]
    ok("no saved layout left the default bar exactly as it was")


def test_the_config_default_for_the_toolbar_is_an_empty_layout():
    """A config file predating the toolbar must load without a toolbar key."""
    import json

    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "old_config.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"save_name": "mybase.npz"}, fh)
        loaded = config.load(path)
        assert loaded["save_name"] == "mybase.npz"
        assert loaded["toolbar"] == [], loaded["toolbar"]

        # And a round trip through save() keeps both keys.
        loaded["toolbar"] = [{"kind": "belt"}]
        assert config.save(loaded, path)
        again = config.load(path)
        assert again["toolbar"] == [{"kind": "belt"}], again["toolbar"]
        assert again["save_name"] == "mybase.npz"
    ok("an older config loaded with an empty layout and saved both keys")
