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
import os
import tempfile

import glfw

from core import cell
from core.entity import Source
from core.tick import MAX_CATCHUP_TICKS, PHASE_BELT, PHASE_STRUCTURE

from tests import harness
from tests.harness import (
    board, engine, fake, ok, orch, press_key, put_cursor_at_cell, step,
)

#: Never the default path. `save_board()` with no argument writes
#: persistence.DEFAULT_SAVE relative to the cwd -- which, run from the repo
#: root, is the player's own quicksave. A test must not be able to destroy it.
_SCRATCH_SAVE = os.path.join(tempfile.gettempdir(), "gates_test_save.npz")


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
    belt_cb = clock.on_phase(PHASE_BELT, lambda: log.append("belt"))
    struct_cb = clock.on_phase(PHASE_STRUCTURE,
                               lambda: log.append("structure"))
    try:
        step(3)
        # The engine registers belt transport at construction, so the belt
        # phase already has a callback; only ours is asserted on here.
        assert [e for e in log] == ["structure", "belt"] * 3, log
        ok("three ticks ran structure,belt three times in order")
    finally:
        # Remove exactly our own, never clear the phase: the engine's belt
        # transport lives on the belt phase, and clearing it silently stops
        # the simulation for every later scenario.
        clock.off_phase(PHASE_BELT, belt_cb)
        clock.off_phase(PHASE_STRUCTURE, struct_cb)


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
    try:
        engine.save_board(_SCRATCH_SAVE)
        engine.load_board(_SCRATCH_SAVE)
        assert clock.tick == 0, "load_board should restart the clock"
    finally:
        if os.path.exists(_SCRATCH_SAVE):
            os.remove(_SCRATCH_SAVE)
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
        # Ours only -- the engine's belt transport is on this phase too.
        clock.off_phase(PHASE_BELT, dirty_a_cell)


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


# --- the tick profiler -----------------------------------------------------
#
# core/tick_profile.py self-checks the Profiler in isolation at import -- the
# aggregation, the countdown, the out-of-tick split. These are the tests that
# need the orchestrator, for the same reason the scenarios above do: that it
# is actually wired into the frame loop, that the panel's request reaches it,
# and that it costs nothing when off.

def test_profiling_a_tick_names_both_phases():
    """An armed profiler attributes a real tick to the phases that ran.

    The claim worth pinning is not the timings -- those are machine-dependent
    and would make this flaky -- but the *tree*: that both phases appear, and
    that the sub-phase probes inside them nest underneath rather than landing
    beside them. A probe whose path lost its prefix would still record time
    and would silently stop being attributable to a phase.
    """
    clock = engine.clock
    clock.set_rate(60.0)
    # A Source feeding a run, so both phases have something to do: an empty
    # board measures the structure phase firing nothing, which is a real
    # measurement but not one that exercises the belt side.
    engine.place_structure(Source, 2, 20, cell.DIR_PX)
    for x in range(4, 14):
        engine.place_belt(x, 20)
    step()

    prof = clock.profiler
    prof.arm(5)
    # **The CPU path explicitly.** This test is about *its* probe tree --
    # `structure/prefilter`, `belt/transport` -- which the dispatch path does
    # not have and should not fake: it has no prefilter, by design, and its
    # time goes somewhere else entirely. Leaving it to whichever path happened
    # to be available would make the assertions below depend on whether a GL
    # context existed.
    was_gpu = engine._gpu_enabled
    engine._gpu_enabled = False
    try:
        for _ in range(5):
            step()
        assert not prof.armed, "5 armed ticks should have disarmed it"
        assert prof.ticks == 5, prof.ticks
        paths = set(prof.stats)
        assert "structure" in paths and "belt" in paths, paths
        # The sub-phases must be *children*, which is what the dump's
        # indentation and the share column both depend on.
        assert "structure/prefilter" in paths, paths
        assert "belt/transport" in paths, paths
        # A child cannot have taken longer than its parent -- it runs inside
        # it. This is the cheapest check that the nesting is real rather than
        # two unrelated probes that happen to share a name prefix.
        assert (prof.stats["belt/transport"].total
                <= prof.stats["belt"].total + 1e-9)
        ok(f"5 ticks attributed across {len(paths)} paths: "
           f"{sorted(paths)}")
    finally:
        prof.stop()
        engine._gpu_enabled = was_gpu


def test_the_profiler_dumps_itself_when_its_count_runs_out():
    """The countdown reaches the console without anyone polling it.

    `Clock` sets `profile_done` and the orchestrator dumps, which is the
    division the panel already follows -- the clock owns time and has no
    opinion about where a report goes. The flag must be *consumed*, or the
    breakdown would print on every frame after the sample ended.
    """
    clock = engine.clock
    clock.set_rate(60.0)
    engine.place_belt(20, 20)
    step()

    lines = []
    real_dump = orch._dump_profile

    def capture(eng):
        lines.append(eng.clock.profiler.report("test"))

    orch._dump_profile = capture
    try:
        clock.profiler.arm(3)
        step(3)
        assert len(lines) == 1, f"expected exactly one dump, got {len(lines)}"
        text = "\n".join(lines[0])
        assert "3 ticks" in text, text
        assert not clock.profile_done, "the flag must be consumed, not latched"
        # ...and it must not fire again on later frames.
        step(5)
        assert len(lines) == 1, (
            f"the dump repeated on later frames: {len(lines)}")
        ok("the armed count dumped exactly once and did not repeat")
    finally:
        orch._dump_profile = real_dump
        clock.profiler.stop()


def test_a_disarmed_profiler_records_nothing():
    """Off by default, and off means off.

    The simulation must not get slower for carrying a tool that measures how
    fast it is, so a disarmed probe has to be a single attribute test that
    records nothing at all. Asserting on the *stats* rather than on timing is
    what makes this a real test rather than a benchmark: if a probe recorded
    while disarmed it would show up here as a path that should not exist.
    """
    clock = engine.clock
    clock.set_rate(60.0)
    prof = clock.profiler
    assert not prof.armed, "the profiler must start disarmed"
    engine.place_structure(Source, 2, 20, cell.DIR_PX)
    for x in range(4, 10):
        engine.place_belt(x, 20)
    step(10)
    assert not prof.stats, f"a disarmed profiler recorded {sorted(prof.stats)}"
    assert prof.ticks == 0, prof.ticks
    assert not clock.profile_done
    ok(f"10 ticks with the profiler off recorded nothing (tick {clock.tick})")


def test_a_segment_rebuild_is_timed_outside_the_tick():
    """An edit's rebuild is reported, but never averaged into a tick.

    A rebuild is ~88ms on a large board and happens once per edit, so folding
    it into the per-tick mean describes neither cost: divided by one tick it
    claims the tick is 88ms, and divided by 200 it vanishes. It has to be
    collected (it is the answer to "why did that placement stutter") and kept
    out of the tick total (it is the answer to a different question).
    """
    clock = engine.clock
    clock.set_rate(60.0)
    prof = clock.profiler
    prof.arm(None)
    try:
        # Place through the engine while armed, so sync_segments() rebuilds
        # inside the sample.
        for x in range(5, 15):
            engine.place_belt(x, 25)
        step(3)
        assert "rebuild" in prof.stats, sorted(prof.stats)
        assert prof.stats["rebuild"].count >= 1

        report = "\n".join(prof.report("test"))
        assert "not part of a tick" in report, report
        # The share column must still sum to the phases alone. `structure`
        # and `belt` are the only in-tick roots, so their shares are the
        # whole of it -- a rebuild leaking in would push both well below.
        shares = [float(ln.split("%")[0].split()[-1])
                  for ln in report.splitlines()
                  if ln.startswith(("structure ", "belt "))]
        assert abs(sum(shares) - 100.0) < 0.5, (shares, report)
        ok(f"a rebuild was timed ({prof.stats['rebuild'].count} of them) "
           f"and left out of the tick total")
    finally:
        prof.stop()
