"""Segment scenarios: belt runs coalesced into the simulation's unit of work.

core/segments.py self-checks the take-direction rule at import, which needs
no board. These are the tests that need a real grid: that runs are traced
correctly through corners and undergrounds, cut where they must be, and that
the derived index agrees with the grid after arbitrary editing.

`audit()` is the backstop and is asserted clean almost everywhere -- it
compares the index against the grid cell by cell, which is exactly the
disagreement derived state is prone to.
"""
import glfw

from core import cell
from core.entity import Mac, Shear, Sink, Source, Swap

from tests import harness
from tests.harness import (
    board, cells_of_type, click, engine, fake, ok, orch, press_key,
    put_cursor_at_cell, select_tool, step,
)


def _segs():
    """The segment index, rebuilt."""
    engine.sync_segments()
    return engine.segments


def _shapes():
    """{(tail, head, length)} for every segment -- id-independent."""
    return {(s.cells[0], s.cells[-1], s.length)
            for s in _segs().segments.values()}


def _clean():
    problems = _segs().audit()
    assert not problems, problems


def test_straight_run_is_one_segment():
    """A plain run coalesces, with offsets marching from the tail."""
    engine.place_belt_path((5, 5), (12, 5))
    index = _segs()
    assert index.count == 1, index.count
    seg = next(iter(index.segments.values()))
    assert seg.cells[0] == (5, 5), seg.cells[0]
    assert seg.cells[-1] == (12, 5), seg.cells[-1]
    assert seg.length == 8, seg.length
    # Offset 0 is the upstream end, counting up toward the consumer.
    offs = [index.offset_at(x, 5) for x in range(5, 13)]
    assert offs == list(range(8)), offs
    _clean()
    ok(f"8-cell run -> 1 segment, offsets {offs[0]}..{offs[-1]}")


def test_corner_stays_one_segment():
    """A run that turns is still one pure belt -- the facing just changes."""
    engine.place_belt_path((5, 5), (10, 9))
    index = _segs()
    assert index.count == 1, [str(s) for s in index.segments.values()]
    seg = next(iter(index.segments.values()))
    assert seg.length == len(seg.cells), "no buried positions in a plain elbow"
    _clean()
    ok(f"elbow -> 1 segment of {seg.length}")


def test_gate_cuts_a_run_in_two():
    """A structure between two runs makes them separate segments.

    The closed form only describes a maximal run between two nodes, so a gate
    in the middle must be a boundary, not a cell the run flows through.
    """
    engine.place_belt_path((5, 5), (9, 5))
    sh = engine.place_structure(Shear, 10, 5)
    engine.place_belt_path((12, 5), (16, 5))
    index = _segs()
    assert index.count == 2, [str(s) for s in index.segments.values()]

    upstream = index.at(9, 5)
    downstream = index.at(12, 5)
    assert upstream is not downstream
    # The ends must name the gate, so the tick knows where material goes.
    assert upstream.dst is not None and upstream.dst[0] == sh.row, upstream.dst
    assert downstream.src is not None and downstream.src[0] == sh.row, \
        downstream.src
    # ...and the far ends dangle.
    assert upstream.src is None and downstream.dst is None
    _clean()
    ok("belt->Shear->belt gives 2 segments, both naming the gate")


def test_source_and_sink_are_ends():
    """A Source feeds a segment and a Sink drains one."""
    src = engine.place_structure(Source, 7, 5)      # 2x1, output on the +X tip
    engine.place_belt_path((9, 5), (14, 5))
    snk = engine.place_structure(Sink, 15, 5)
    index = _segs()
    assert index.count == 1, index.count
    seg = next(iter(index.segments.values()))
    assert seg.src is not None and seg.src[0] == src.row, seg.src
    assert seg.dst is not None and seg.dst[0] == snk.row, seg.dst
    _clean()
    ok("Source -> belt -> Sink resolves both ends")


def test_underground_adds_capacity_without_cells():
    """A paired underground extends one run across the gap it jumps.

    The buried tiles are real item positions with no cell to draw, which is
    the whole reason a segment stores length separately from its cell list --
    and why each cell carries its own offset rather than its list index.
    """
    engine.place_belt_path((5, 5), (8, 5))
    en = engine.place_under(9, 5, cell.DIR_PX, is_exit=False)
    ex = engine.place_under(13, 5, cell.DIR_PX, is_exit=True)
    assert en.paired and ex.paired, "the pair should have formed"
    engine.place_belt_path((14, 5), (18, 5))

    index = _segs()
    assert index.count == 1, [str(s) for s in index.segments.values()]
    seg = next(iter(index.segments.values()))
    assert len(seg.cells) == 11, len(seg.cells)
    # 3 tiles between the ends at x=9 and x=13.
    assert seg.buried == 3, seg.buried
    assert seg.length == 14, seg.length
    # The offset jumps the span: the exit sits 4 positions past the entrance.
    assert index.offset_at(9, 5) == 4, index.offset_at(9, 5)
    assert index.offset_at(13, 5) == 8, index.offset_at(13, 5)
    _clean()
    ok(f"underground run: {seg.length} positions over "
       f"{len(seg.cells)} cells (+{seg.buried} buried)")


def test_two_underground_pairs_chain():
    """Any number of spans on a run is still one O(1) segment."""
    engine.place_belt_path((5, 5), (6, 5))
    engine.place_under(7, 5, cell.DIR_PX, is_exit=False)
    engine.place_under(11, 5, cell.DIR_PX, is_exit=True)
    engine.place_belt_path((12, 5), (13, 5))
    engine.place_under(14, 5, cell.DIR_PX, is_exit=False)
    engine.place_under(18, 5, cell.DIR_PX, is_exit=True)
    engine.place_belt_path((19, 5), (20, 5))

    index = _segs()
    assert index.count == 1, [str(s) for s in index.segments.values()]
    seg = next(iter(index.segments.values()))
    assert seg.buried == 6, seg.buried
    _clean()
    ok(f"two spans in one run: 1 segment, {seg.buried} buried positions")


def test_underground_run_survives_scan_order():
    """A paired exit is not a tail, whichever cell the scan reaches first.

    An exit is fed across the buried span, which `_incoming()` cannot see --
    it only probes the four adjacent cells. Treating the exit as a tail
    starts a second trace over cells the entrance's run already covers, and
    the two segments double-claim them.

    The bug only *shows* when the exit is scanned before its entrance, so
    every direction has to be checked: a run laid westward or southward puts
    the exit earlier in row-major order, and that is the case the other
    underground tests (all +X) never reach.
    """
    for direction, (sx, sy), step in (
            (cell.DIR_PX, (5, 5), (1, 0)),
            (cell.DIR_NX, (20, 8), (-1, 0)),
            (cell.DIR_PY, (30, 5), (0, 1)),
            (cell.DIR_NY, (35, 20), (0, -1))):
        def at(i):
            return (sx + step[0] * i, sy + step[1] * i)

        engine.place_belt_path(at(0), at(2))
        engine.place_under(*at(3), direction, is_exit=False)
        engine.place_under(*at(7), direction, is_exit=True)
        engine.place_belt_path(at(8), at(10))

        index = _segs()
        name = cell.DIR_NAMES[direction]
        assert index.count == 1, (name, [str(s) for s in index.segments.values()])
        seg = next(iter(index.segments.values()))
        assert seg.cells[0] == at(0), (name, seg.cells[0])
        assert seg.cells[-1] == at(10), (name, seg.cells[-1])
        assert seg.buried == 3, (name, seg.buried)
        _clean()
        harness.reset()
    ok("a paired exit is never a tail, in all four directions")


def test_unpaired_entrance_ends_a_run():
    """An unpaired entrance leads nowhere, so the run stops there."""
    engine.place_belt_path((5, 5), (8, 5))
    engine.place_under(9, 5, cell.DIR_PX, is_exit=False)
    index = _segs()
    seg = index.at(9, 5)
    assert seg is not None, "the entrance should still be on a segment"
    assert seg.cells[-1] == (9, 5), seg.cells[-1]
    assert seg.buried == 0, "an unpaired entrance buries nothing"
    _clean()
    ok("an unpaired entrance terminates its run")


def test_merge_is_resolved_not_simulated():
    """Two belts into one cell: straight-through wins, the loser is separate.

    Belts never merge here. The take rule picks one source deterministically,
    and the cells that lost are simply not connected -- which is what the
    renderer must agree with, or it would draw an arm carrying nothing.
    """
    engine.place_belt_path((5, 5), (15, 5))      # main run, west -> east
    engine.place_belt(10, 4, cell.DIR_PY)        # tributary from the south
    index = _segs()
    assert index.count == 2, [str(s) for s in index.segments.values()]

    main = index.at(10, 5)
    trib = index.at(10, 4)
    assert main is not trib
    # The main run is unbroken: straight-through beat the tributary.
    assert len(main.cells) == 11, len(main.cells)
    assert main.cells[0] == (5, 5) and main.cells[-1] == (15, 5)
    # The tributary is its own one-cell segment, draining nowhere.
    assert len(trib.cells) == 1, trib.cells
    _clean()
    ok("straight-through won the merge; the tributary is its own segment")


def test_sideways_feed_is_taken_when_alone():
    """With no straight-through source, a sideways one does connect."""
    # A corner: belt at (10,4) facing +Y into (10,5), which faces +X.
    engine.place_belt(10, 4, cell.DIR_PY)
    engine.place_belt_path((10, 5), (14, 5))
    index = _segs()
    assert index.count == 1, [str(s) for s in index.segments.values()]
    seg = next(iter(index.segments.values()))
    assert seg.cells[0] == (10, 4), seg.cells[0]
    _clean()
    ok("a lone sideways feed joins the run")


def test_nose_to_nose_belts_do_not_connect():
    """Two belts facing each other are two segments, not one.

    The cell we output into is never a source. Without that rule each belt
    names the other as upstream, neither is a tail, and the pair falls through
    to the closed-loop path as a single bogus segment.
    """
    engine.place_belt(5, 5, cell.DIR_PX)
    engine.place_belt(6, 5, cell.DIR_NX)
    index = _segs()
    assert index.count == 2, [str(s) for s in index.segments.values()]
    for s in index.segments.values():
        assert len(s.cells) == 1, s.cells
    _clean()
    ok("nose-to-nose belts stay separate")


def test_closed_loop_becomes_one_segment():
    """A ring has no tail, and must still be segmented rather than skipped."""
    for x in range(5, 9):
        engine.place_belt(x, 5, cell.DIR_PX)
    for y in range(5, 9):
        engine.place_belt(9, y, cell.DIR_PY)
    for x in range(9, 5, -1):
        engine.place_belt(x, 9, cell.DIR_NX)
    for y in range(9, 5, -1):
        engine.place_belt(5, y, cell.DIR_NY)

    index = _segs()
    assert index.count == 1, [str(s) for s in index.segments.values()]
    seg = next(iter(index.segments.values()))
    assert len(seg.cells) == 16, len(seg.cells)
    _clean()
    ok(f"a closed ring of 16 cells -> 1 segment")


def test_ghost_cells_are_not_segmented():
    """A preview must not reach the simulation, or it would rebuild per frame.

    Ghosts are real entities in real cells, so segment derivation has to skip
    them explicitly.
    """
    engine.place_belt_path((5, 5), (9, 5))
    before = _shapes()

    # Hover a belt run that would extend the existing one.
    select_tool("Belt")
    put_cursor_at_cell(11, 5)
    click()                               # press+release starts PLANNING
    put_cursor_at_cell(15, 5)
    step()
    assert harness.ghost_cell_count() > 0, "expected a live ghost preview"

    index = _segs()
    assert _shapes() == before, (
        f"ghosts changed the segmentation:\n  {before}\n  {_shapes()}")
    for s in index.segments.values():
        for c in s.cells:
            z = int(board.grid[c[1], c[0], cell.CH_Z])
            assert cell.ghost_state(z) == cell.GHOST_NONE, c
    _clean()
    ok(f"{harness.ghost_cell_count()} ghost cells, segmentation unchanged")


def test_frustration_does_not_cut_a_live_run():
    """A blocked structure hovered over a belt must not disturb its segment.

    Frustration is an overlay: it lives in .z and leaves .x alone precisely
    so the occupant survives. If the derivation treated a frustrated cell as
    absent, merely waving a blocked structure across a run would split it --
    and once items exist, destroy them.
    """
    engine.place_belt_path((5, 5), (15, 5))
    before = _shapes()
    assert len(before) == 1

    select_tool("Shear")
    put_cursor_at_cell(10, 5)
    step()
    assert harness.frustrated_cell_count() > 0, "expected frustrated cells"

    index = _segs()
    assert index.count == 1, (
        f"frustration split the run: {[str(s) for s in index.segments.values()]}")
    assert _shapes() == before, (before, _shapes())
    _clean()
    ok(f"{harness.frustrated_cell_count()} frustrated cells left the run whole")


def test_preview_churn_does_not_rebuild():
    """A moving preview must not re-derive the index every frame.

    Ghost cells are excluded from derivation, so writing them cannot change
    its answer -- and rebuilding anyway would scan the whole board once per
    frame for an identical result. The invalidation hook therefore compares
    *real* cell content, not raw grid writes.
    """
    engine.place_belt_path((5, 5), (15, 5))
    step()

    # A drag-based belt preview, redrawn every frame as the cursor moves.
    select_tool("Belt")
    put_cursor_at_cell(20, 20)
    click()
    base = _segs().stats["rebuilds"]
    for x in range(21, 36):
        put_cursor_at_cell(x, 20)
        step()
    ghost_rebuilds = engine.segments.stats["rebuilds"] - base
    assert ghost_rebuilds == 0, (
        f"a moving ghost preview caused {ghost_rebuilds} rebuilds")
    ok("15 frames of moving ghost preview -> 0 rebuilds")

    # A click-based structure preview, which also frustrates cells each frame.
    harness.reset()
    engine.place_belt_path((5, 5), (25, 5))
    step()
    select_tool("Shear")
    base = engine.segments.stats["rebuilds"]
    for x in range(6, 24):
        put_cursor_at_cell(x, 5)
        step()
    frust_rebuilds = engine.segments.stats["rebuilds"] - base
    assert frust_rebuilds == 0, (
        f"hovering a blocked structure caused {frust_rebuilds} rebuilds")
    _clean()
    ok("18 frames hovering a blocked structure -> 0 rebuilds")


def test_idle_frames_do_not_rebuild():
    """With nothing changing, the index is left alone."""
    engine.place_belt_path((5, 5), (15, 5))
    step()
    base = _segs().stats["rebuilds"]
    step(60)
    assert engine.segments.stats["rebuilds"] == base, (
        f"{engine.segments.stats['rebuilds'] - base} rebuilds while idle")
    ok("60 idle frames -> 0 rebuilds")


def test_committing_a_preview_extends_the_segment():
    """Once a ghost commits it joins the run, as one segment."""
    engine.place_belt_path((5, 5), (9, 5))
    assert _segs().count == 1

    select_tool("Belt")
    put_cursor_at_cell(10, 5)
    click()
    put_cursor_at_cell(15, 5)
    step()
    click()                               # commit

    index = _segs()
    assert index.count == 1, [str(s) for s in index.segments.values()]
    seg = next(iter(index.segments.values()))
    assert seg.cells[0] == (5, 5) and seg.cells[-1] == (15, 5), \
        (seg.cells[0], seg.cells[-1])
    _clean()
    ok(f"committed preview fused into one {seg.length}-cell run")


def test_erasing_mid_run_splits_the_segment():
    """Removing a cell cuts one segment into two."""
    engine.place_belt_path((5, 5), (15, 5))
    assert _segs().count == 1

    engine.erase(10, 5)
    index = _segs()
    assert index.count == 2, [str(s) for s in index.segments.values()]
    left, right = sorted(index.segments.values(), key=lambda s: s.cells[0])
    assert left.cells[-1] == (9, 5), left.cells[-1]
    assert right.cells[0] == (11, 5), right.cells[0]
    # Offsets must restart from the new tail, not continue the old numbering.
    assert index.offset_at(11, 5) == 0, index.offset_at(11, 5)
    _clean()
    ok("erasing mid-run split it, and the downstream half re-indexed from 0")


def test_items_are_dropped_on_edit():
    """An edit drops the items on the segments it touches, by design.

    Positions are relative to a segment's anchor, so they cannot survive a
    re-derivation. This pins the documented behaviour so nobody later assumes
    items persist across a rebuild.
    """
    engine.place_belt_path((5, 5), (15, 5))
    index = _segs()
    seg = next(iter(index.segments.values()))

    # Fake a couple of items onto the ring, as the tick will once it exists.
    seg.tail = 2
    seg.enter_tick[0] = 1
    seg.enter_tick[1] = 2
    assert seg.count == 2
    dropped_before = index.stats["items_dropped"]

    engine.erase(10, 5)
    index = _segs()
    assert index.stats["items_dropped"] >= dropped_before + 2, (
        index.stats["items_dropped"], dropped_before)
    assert index.total_items() == 0, "items must not survive the rebuild"
    _clean()
    ok("an edit dropped the in-flight items and re-derived cleanly")


def test_ring_capacity_is_a_power_of_two():
    """Required by the closed form, not merely tidy.

    Item slots are indexed `k % cap` on a monotonically growing counter, which
    only survives unsigned wraparound when cap is a power of two.
    """
    for length in (1, 2, 3, 5, 8, 13, 30):
        engine.clear_board()
        engine.place_belt_path((5, 5), (5 + length - 1, 5))
        index = _segs()
        for s in index.segments.values():
            assert s.cap & (s.cap - 1) == 0, (length, s.cap)
            # ...and big enough for a full belt plus one arriving.
            assert s.cap >= s.length + 1, (length, s.cap, s.length)
    ok("ring capacities are powers of two across run lengths 1..30")


def test_audit_survives_heavy_editing():
    """The index must agree with the grid after arbitrary churn.

    This is the real test of the derivation: build, erase, overlap and
    re-build a tangle, then assert the audit is clean. An audit failure here
    means the index and the grid have diverged -- the exact failure mode
    derived state is prone to.
    """
    import random
    rng = random.Random(1234)

    for step_i in range(40):
        op = rng.random()
        x, y = rng.randrange(2, 30), rng.randrange(2, 30)
        if op < 0.55:
            x2 = min(31, x + rng.randrange(0, 8))
            y2 = min(31, y + rng.randrange(0, 8))
            engine.place_belt_path((x, y), (x2, y2))
        elif op < 0.70:
            engine.place_under(x, y, rng.randrange(4),
                               is_exit=bool(rng.getrandbits(1)))
        elif op < 0.80:
            engine.place_structure(
                rng.choice([Shear, Mac, Swap, Source, Sink]), x, y,
                rng.randrange(4))
        else:
            engine.erase(x, y)

        problems = _segs().audit()
        assert not problems, (
            f"audit failed after op {step_i} at ({x},{y}):\n"
            + "\n".join(problems[:10]))

    index = _segs()
    ok(f"40 random edits: {index.count} segments, audit clean "
       f"({cells_of_type(cell.TYPE_BELT)} belt cells)")


def test_clearing_the_board_releases_segments():
    """Nothing is left stamped, and no segment survives."""
    engine.place_belt_path((5, 5), (15, 5))
    engine.place_belt_path((5, 8), (15, 8))
    assert _segs().count == 2

    engine.clear_board()
    index = _segs()
    assert index.count == 0, index.count
    stamped = cell.segment_id_arr(board.grid[:, :, cell.CH_W])
    assert not stamped.any(), "cells still name a segment after a clear"
    _clean()
    ok("clearing released every segment and unstamped every cell")


def test_rebuild_is_deferred_to_one_per_frame():
    """Many edits in a frame cost one rebuild, not one each."""
    engine.place_belt_path((5, 5), (9, 5))
    step()                                  # settle, so the index is current
    before = _segs().stats["rebuilds"]

    # A burst of edits with no frame in between.
    for x in range(10, 20):
        engine.place_belt(x, 5, cell.DIR_PX)
    # Still only marked stale; nothing rebuilt yet.
    assert engine.segments.stats["rebuilds"] == before, "rebuilt too eagerly"

    engine.sync_segments()
    assert engine.segments.stats["rebuilds"] == before + 1, (
        engine.segments.stats["rebuilds"], before)
    _clean()
    ok("10 edits in one frame -> exactly 1 rebuild")


def test_segment_overlay_toggles_without_breaking_the_shader():
    """The debug overlay compiles both ways and leaves rendering working.

    It lives behind #ifdef so it costs nothing when off, which means toggling
    it is a recompile -- worth checking that the recompile actually succeeds
    in both directions.
    """
    cam = orch.camera
    was = "SEGMENT_DEBUG" in cam.defines
    try:
        engine.place_belt_path((5, 5), (12, 5))

        assert cam.set_define("SEGMENT_DEBUG", True), "expected a recompile"
        assert cam.compile_error is None, cam.compile_error
        step()
        ok("segment overlay compiled and rendered")

        assert not cam.set_define("SEGMENT_DEBUG", True), \
            "re-enabling an active define should not recompile"

        assert cam.set_define("SEGMENT_DEBUG", False)
        assert cam.compile_error is None, cam.compile_error
        step()
        ok("overlay disabled, shader still good")
    finally:
        # The camera is shared across scenarios, and a stray define would
        # leave every later one rendering the debug build.
        cam.set_define("SEGMENT_DEBUG", was)
