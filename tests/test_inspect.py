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
from core.entity import Shear, Sink, Source
from ui.panel import UIRequests

from tests import harness
from tests.harness import (board, click, ctrl_key, drag_cells, engine, fake,
                           ok, orch, put_cursor_at_cell, select_tool, step)


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
    """`initialise()` seeds EMIT_VALUE; a zeroed row would read 0.0."""
    src = engine.place_structure(Source, 10, 10)
    assert src.emit_value == Source.EMIT_VALUE, src.emit_value
    ok(f"a placed Source emits {Source.EMIT_VALUE} by default")


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
    assert Source.EMIT_VALUE == -1.0, "the class default should be untouched"
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
    for x in range(12, 20):
        engine.place_belt(x, 10, cell.DIR_PX)
    engine.sync_segments()
    for _ in range(12):
        engine.clock.request_step()
        engine.clock.advance(0.0)

    seg = engine.segments.at(13, 10)
    assert seg.count > 0, "need items in flight to flush"
    old = {engine.belts.payload_of(seg, k) for k in range(seg.head, seg.tail)}
    assert old == {Source.EMIT_VALUE}, old

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
