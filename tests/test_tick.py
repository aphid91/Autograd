"""Tick scenarios: the simulation clock as driven through the orchestrator.

core/tick.py already self-checks the Clock in isolation at import -- the
timing contract, the phase ordering, the catch-up cap. These are the tests
that need the *orchestrator*: that the clock is actually wired into the frame
loop, that the keys reach it, and that it advances at the point in the frame
the design says it does.

`harness.step(n)` runs n frames at 1/60s each, so the number of ticks a
scenario sees depends on the tick rate. Scenarios that count ticks set the
rate explicitly rather than relying on the default.
"""
import glfw

from core.tick import MAX_CATCHUP_TICKS, PHASE_BELT, PHASE_STRUCTURE

from tests import harness
from tests.harness import (
    board, engine, fake, ok, orch, press_key, put_cursor_at_cell, step,
)


def test_clock_advances_with_frames():
    """Frames drive the tick counter at the configured rate."""
    clock = engine.clock
    clock.set_rate(60.0)          # one tick per frame at 1/60s
    start = clock.tick
    step(10)
    assert clock.tick == start + 10, (start, clock.tick)
    ok(f"10 frames at 60/s ran 10 ticks (tick {clock.tick})")

    # Half the rate, half the ticks.
    clock.set_rate(30.0)
    mid = clock.tick
    step(10)
    assert clock.tick == mid + 5, (mid, clock.tick)
    ok("30/s ran 5 ticks over 10 frames at 1/60s")


def test_alpha_interpolates_between_ticks():
    """alpha rises through a tick and resets, staying in 0..1 throughout."""
    clock = engine.clock
    # Four frames per tick, so alpha should visibly climb in quarters.
    clock.set_rate(15.0)
    clock.reset()
    seen = []
    for _ in range(4):
        step()
        seen.append(clock.alpha)
    assert all(0.0 <= a <= 1.0 for a in seen), seen
    # It must advance, not sit still.
    assert seen[1] > seen[0], seen
    ok(f"alpha climbed through a tick: {[round(a, 2) for a in seen]}")

    # Over many frames alpha never escapes 0..1, whatever the rate.
    for rate in (0.5, 7.0, 60.0, 240.0):
        clock.set_rate(rate)
        for _ in range(20):
            step()
            assert 0.0 <= clock.alpha <= 1.0, (rate, clock.alpha)
    ok("alpha stayed in 0..1 across rates 0.5..240/s")


def test_space_pauses_and_period_steps():
    """The keyboard reaches the clock: Space pauses, '.' steps one tick."""
    clock = engine.clock
    clock.set_rate(60.0)

    press_key(glfw.KEY_SPACE)
    assert clock.paused, "Space should pause the sim"
    frozen = clock.tick
    step(10)
    assert clock.tick == frozen, "a paused clock must not tick"
    assert clock.alpha == 0.0, "a paused clock must not interpolate"
    ok(f"Space paused the sim at tick {frozen}")

    # '.' runs exactly one tick while paused -- the core debugging move.
    press_key(glfw.KEY_PERIOD)
    assert clock.tick == frozen + 1, (frozen, clock.tick)
    assert clock.paused, "stepping must not resume the sim"
    ok("'.' ran exactly one tick while paused")

    press_key(glfw.KEY_SPACE)
    assert not clock.paused
    step(5)
    assert clock.tick > frozen + 1
    ok("Space resumed the sim")


def test_held_keys_do_not_repeat():
    """Pause and step are edge-triggered, not per-frame."""
    clock = engine.clock
    clock.set_rate(60.0)

    # Holding '.' for many frames must run one tick, not one per frame.
    clock.pause()
    before = clock.tick
    fake["keys"].add(glfw.KEY_PERIOD)
    step(10)
    fake["keys"].discard(glfw.KEY_PERIOD)
    step()
    assert clock.tick == before + 1, (
        f"held '.' ran {clock.tick - before} ticks, expected 1")
    ok("held '.' stepped exactly one tick")

    # Likewise holding Space must not flicker the pause state every frame.
    fake["keys"].add(glfw.KEY_SPACE)
    step(9)        # odd count: a per-frame toggle would land on the far state
    fake["keys"].discard(glfw.KEY_SPACE)
    step()
    assert not clock.paused, "held Space should toggle pause exactly once"
    ok("held Space toggled pause exactly once")


def test_phases_run_in_order_every_tick():
    """Registered phases run structure-then-belt, once each, per tick.

    This is the ordering the whole two-phase design rests on -- a gate must
    read its waiting zone before the belt phase drains it -- so it is worth
    pinning through the real frame loop and not only in the unit self-check.
    """
    clock = engine.clock
    clock.set_rate(60.0)
    log = []
    # Registered in reverse to prove the *phase* decides the order, not the
    # registration sequence.
    clock.on_phase(PHASE_BELT, lambda: log.append("belt"))
    clock.on_phase(PHASE_STRUCTURE, lambda: log.append("structure"))
    try:
        step(3)
        assert log == ["structure", "belt"] * 3, log
        ok("three ticks ran structure,belt three times in order")
    finally:
        # Phase callbacks live on the shared clock, so a scenario that adds
        # them must take them away again or every later one inherits them.
        clock._phases[PHASE_BELT].clear()
        clock._phases[PHASE_STRUCTURE].clear()


def test_catchup_cap_bounds_work_per_frame():
    """A long stall cannot bank an unbounded backlog of ticks."""
    clock = engine.clock
    clock.set_rate(60.0)
    clock.reset()
    before = clock.tick
    # app.py clamps dt to 0.1s, but the clock must be robust on its own:
    # hand it a 10-second frame directly.
    orch.frame(10.0)
    ran = clock.tick - before
    assert ran == MAX_CATCHUP_TICKS, (
        f"a 10s frame ran {ran} ticks, expected the cap {MAX_CATCHUP_TICKS}")
    assert clock.dropped_ticks > 0, "the backlog should be recorded as dropped"
    ok(f"a 10s frame ran {ran} ticks and dropped {clock.dropped_ticks}")

    # And the dropped time is genuinely discarded: the next frame runs a
    # normal number of ticks rather than still working through a backlog.
    mark = clock.tick
    step(2)
    assert clock.tick - mark <= 2, "the backlog should not persist"
    ok("the dropped backlog did not carry into later frames")


def test_board_edits_restart_the_timeline():
    """Clearing or loading the board resets the tick counter.

    Item positions are a closed-form function of the tick counter, so a new
    board must start a new timeline -- otherwise every item on it is dated to
    a tick that never happened for it.
    """
    clock = engine.clock
    clock.set_rate(60.0)
    step(5)
    assert clock.tick > 0
    engine.clear_board()
    assert clock.tick == 0, "clear_board should restart the clock"
    ok("clear_board reset the tick counter")

    # A quicksave round trip does the same.
    put_cursor_at_cell(10, 10)
    engine.place_belt(10, 10)
    step(5)
    assert clock.tick > 0
    engine.save_board()
    engine.load_board()
    assert clock.tick == 0, "load_board should restart the clock"
    ok("load_board reset the tick counter")


def test_tick_happens_before_device_sync():
    """A tick's effects reach the GPU in the frame they happen.

    The clock advances after player mutation and before the device sync, so a
    phase callback that dirties the board has its changes uploaded the same
    frame rather than being displayed one frame stale. Dirty a cell from a
    phase callback and assert the upload carried it.
    """
    clock = engine.clock
    clock.set_rate(60.0)
    engine.place_belt(20, 20)
    step()                        # let the placement upload and settle

    def dirty_a_cell():
        board.mark_dirty(20, 20)

    clock.on_phase(PHASE_BELT, dirty_a_cell)
    try:
        step()
        cells, _rows = orch.ui.last_upload
        assert cells > 0, (
            "a cell dirtied during the tick should upload in the same frame")
        ok(f"a tick-dirtied cell uploaded the same frame ({cells} cells)")
    finally:
        clock._phases[PHASE_BELT].clear()


def test_clock_does_not_disturb_building():
    """A running sim leaves the build gestures alone.

    The clock is new global state in the frame loop; the cheapest way to catch
    it interfering with input or placement is to build something while it
    runs.
    """
    clock = engine.clock
    clock.set_rate(60.0)
    harness.select_tool("Belt")
    put_cursor_at_cell(5, 5)
    harness.click()
    put_cursor_at_cell(10, 5)
    step()
    harness.click()
    placed = harness.cells_of_type(1)
    assert placed == 6, f"expected a 6-cell run, got {placed}"
    assert clock.tick > 0, "the sim should have been running throughout"
    ok(f"built a 6-belt run with the sim running (tick {clock.tick})")
