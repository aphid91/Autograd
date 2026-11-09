"""Gate firing: the arithmetic, the firing rules, and back pressure.

core/entity.py self-checks each machine's `fire()` against its signature at
import, which needs no board. These are the tests that need the whole stack:
that a gate wired to real belts fires at the right time, that items reach it
and leave it, and that the two-phase tick delivers the throughput the design
was built for.

The headline test is `test_well_fed_gate_fires_every_tick`. That is the claim
the whole structure-then-belt ordering exists to support, and it is worth
asserting rather than trusting -- an ordering mistake inside the belt phase
cost about one tick in seven while looking exactly like the documented
one-tick stall recovery.
"""
from core import cell
from core.belts import UNIT
from core.entity import (VALUE_LIMIT, Floor, Mac, Max, Merge, Reciprocal,
                         Route, Shear, Sink, Source, Swap)

from tests import harness
from tests.harness import board, engine, ok, step


def _run(ticks):
    """Advance the simulation by whole ticks, with no wall-clock involved."""
    for _ in range(ticks):
        engine.clock.request_step()
        engine.clock.advance(0.0)


def _feeding_channel(gate, seg):
    """Which of `gate`'s output channels actually feeds `seg`.

    A Source has six faces and so six channels; which one owns a given run
    depends on where the belts were laid, so a test must ask rather than
    assume channel 0.
    """
    port = engine.gates._src_port(seg)
    assert port is not None, f"nothing feeds segment {seg.sid}"
    owner, channel = port
    assert owner.row == gate.row, (owner.row, gate.row)
    return channel


def _drain_every_output(gate):
    """Put one belt cell on each of `gate`'s output faces, facing away.

    A **dangling output no longer blocks** -- its result is destroyed at fire
    time -- so a test that wants to observe a full waiting zone has to give
    every output somewhere to go, and then let that somewhere back up. Before
    that change a bare gate on an empty board filled its zones and jammed,
    which is what several of these tests used to rely on.

    One cell with nothing beyond it is exactly the shape wanted: connected,
    so the channel counts as drainable and can block, but saturating after a
    single item so back pressure arrives immediately.

    Call before `sync_segments()`.
    """
    for (xy, face, _ch) in engine.gates._output_faces(gate):
        step_v = cell.DIR_STEPS[face]
        engine.place_belt(xy[0] + step_v[0], xy[1] + step_v[1], face)


def _chain(gate_cls, gate_at, direction=cell.DIR_PX):
    """Source -> belt -> gate -> belt -> Sink, returning the pieces.

    Only wires the gate's *first* input and output channel; a multi-input
    machine needs its other channels fed by hand, which the tests that care
    do explicitly.
    """
    src = engine.place_structure(Source, 3, 10)
    engine.place_belt_path((5, 10), (gate_at[0] - 1, 10))
    gate = engine.place_structure(gate_cls, *gate_at, direction)
    engine.sync_segments()
    return src, gate


def test_source_emits_and_sink_consumes():
    """The simplest complete chain moves items end to end."""
    src = engine.place_structure(Source, 7, 10)
    engine.place_belt_path((9, 10), (14, 10))
    snk = engine.place_structure(Sink, 15, 10)
    engine.sync_segments()
    seg = next(iter(engine.segments.segments.values()))
    assert seg.src is not None and seg.dst is not None, (seg.src, seg.dst)

    _run(20)
    stats = engine.gates.stats
    assert stats["consumed"] > 10, stats
    assert seg.count > 0, "the belt should be carrying items"
    assert not engine.belts.audit(engine.clock.tick)
    ok(f"Source -> 6 belts -> Sink: {stats['consumed']} items consumed "
       f"in 20 ticks, {seg.count} in transit")


def test_well_fed_gate_fires_every_tick():
    """The design's central claim: no bubble on a saturated chain.

    Structure-then-belt with a waiting zone is supposed to let a gate fire on
    *every* tick -- the gate gets first look at a zone the belt phase emptied
    one phase earlier. This is the assertion that the ordering actually
    delivers it.

    Measured in steady state, after the belt has filled, so start-up
    transients are excluded.
    """
    engine.place_structure(Source, 7, 10)
    engine.place_belt_path((9, 10), (14, 10))
    engine.place_structure(Sink, 15, 10)
    engine.sync_segments()

    _run(20)                      # reach steady state
    before = dict(engine.gates.stats)
    tick_before = engine.clock.tick
    _run(30)
    elapsed = engine.clock.tick - tick_before
    consumed = engine.gates.stats["consumed"] - before["consumed"]
    blocked = engine.gates.stats["blocked"] - before["blocked"]

    # One item consumed per tick, with no bubble at all.
    assert consumed == elapsed, (
        f"{consumed} items consumed over {elapsed} ticks -- a saturated "
        f"chain must move one per tick. A shortfall means the gate work is "
        f"not happening at the two points inside the belt tick that "
        f"docs/belt_report.md puts it; doing it in passes either side costs "
        f"about one tick in seven.")
    assert blocked == 0, (
        f"the gate was blocked {blocked} times in steady state")
    ok(f"steady state: {consumed} items over {elapsed} ticks, "
       f"one per tick, never blocked")


def test_shear_computes_on_the_board():
    """x,y -> x, x+y, with the inputs delivered by belts.

    Reads the results off the **belts** rather than out of the waiting zones.
    Each output gets a one-cell run, and the belt phase drains a connected
    zone in the same tick the structure phase filled it -- so by end of tick
    the zones are empty and the values are on the belt, which is the normal
    flowing case. Asserting `out_held` here would really be asserting that
    nothing downstream took the result, i.e. back pressure, which
    test_back_pressure_stalls_a_blocked_gate covers deliberately.
    """
    gate = engine.place_structure(Shear, 10, 10)
    # Both outputs need a consumer, or their results are destroyed rather
    # than being delivered anywhere this test can read.
    _drain_every_output(gate)
    engine.sync_segments()
    # Feed both channels directly: belt-feeding two separate channels needs
    # two Sources and is covered by the wiring tests below.
    gate.set_input(0, 3.0)
    gate.set_input(1, 4.0)

    _run(1)
    assert not gate.in_held(0) and not gate.in_held(1), "inputs consumed"

    # Channel 0 is the pass-through (x, south), channel 1 the mutated one
    # (x+y, east). Find each by the face its port sits on.
    got = {}
    for (xy, face, ch) in engine.gates._output_faces(gate):
        step_v = cell.DIR_STEPS[face]
        seg = engine.segments.at(xy[0] + step_v[0], xy[1] + step_v[1])
        assert seg is not None, f"channel {ch} lost its run"
        assert seg.count == 1, f"channel {ch} delivered {seg.count} items"
        got[ch] = engine.belts.payload_of(seg, seg.head)

    assert got[0] == 3.0, f"the pass-through should carry x=3, got {got[0]}"
    assert got[1] == 7.0, f"the mutated channel should carry 7, got {got[1]}"
    ok("Shear fired on the board: (3,4) -> (3,7), both onto belts")


def _fire_once_and_read(gate, inputs, expect_empty=False):
    """Feed `inputs`, run one tick, and read each output channel's value.

    Wires a one-cell run to every output and reads the value off that run.
    Reading the waiting zones directly would be simpler but is not available:
    a connected zone is drained within the same tick, and a *dangling* one is
    destroyed at fire time, so neither leaves anything to inspect.

    Returns {channel: payload}. With `expect_empty`, a channel whose run
    received nothing maps to None instead of failing -- which is what Route
    needs, since it deliberately emits on only one of its two data channels.
    """
    _drain_every_output(gate)
    engine.sync_segments()
    for i, v in enumerate(inputs):
        gate.set_input(i, v)
    _run(1)
    got = {}
    for (xy, face, ch) in engine.gates._output_faces(gate):
        step_v = cell.DIR_STEPS[face]
        seg = engine.segments.at(xy[0] + step_v[0], xy[1] + step_v[1])
        assert seg is not None, f"channel {ch} lost its run"
        if expect_empty and seg.count == 0:
            got[ch] = None
            continue
        assert seg.count == 1, f"channel {ch} delivered {seg.count} items"
        got[ch] = engine.belts.payload_of(seg, seg.head)
    return got


def test_mac_and_swap_compute_on_the_board():
    """Mac accumulates a product; Swap exchanges a pair on a condition.

    Each output is given a one-cell run, since a dangling output is destroyed
    rather than held -- see _drain_every_output().
    """
    mac = engine.place_structure(Mac, 10, 10)
    got = _fire_once_and_read(mac, [2.0, 5.0, 1.0])   # x, y, z
    assert [got[i] for i in range(3)] == [2.0, 5.0, 11.0], got
    ok("Mac fired: (2,5,1) -> (2,5,11)")

    # x > 0: swap. The control reads as "swap when true", so a positive x is
    # the exchanging case and zero-or-negative is the pass-through.
    harness.reset()
    swap = engine.place_structure(Swap, 20, 20)
    got = _fire_once_and_read(swap, [1.0, 7.0, 9.0])
    assert [got[i] for i in range(3)] == [1.0, 9.0, 7.0], got
    ok("Swap with x>0 exchanged y and z")

    # x <= 0: no swap. A fresh board rather than draining by hand, since the
    # one-cell runs from the previous fire are now full.
    harness.reset()
    swap = engine.place_structure(Swap, 20, 20)
    got = _fire_once_and_read(swap, [-1.0, 7.0, 9.0])
    assert [got[i] for i in range(3)] == [-1.0, 7.0, 9.0], got
    ok("Swap with x<=0 passed y,z through unchanged")


def test_a_dangling_generator_stays_inert():
    """A Source with nothing at its tip never fills its waiting zone.

    Firing a generator is free and repeatable, so a dangling port is pure
    waste rather than something to hold material in. Contrast
    test_an_undrained_output_blocks_the_whole_gate: a machine that *consumes*
    inputs must write its result and let the block stop it, because dropping
    it would destroy material.
    """
    src = engine.place_structure(Source, 7, 10)
    engine.sync_segments()
    _run(10)
    assert src.outputs_clear(), (
        f"a dangling Source should hold nothing, got {src.out_value(0)}")
    assert engine.gates.stats["unconnected"] > 0, engine.gates.stats
    assert engine.gates.stats["fired"] == 0, (
        f"it should not have fired at all: {engine.gates.stats}")
    ok("a Source with nothing at its tip stayed inert for 10 ticks")

    # Give it somewhere to go and it starts immediately -- the check is on
    # live topology, not something latched at placement.
    engine.place_belt(9, 10, cell.DIR_PX)
    engine.sync_segments()
    _run(1)
    assert engine.gates.stats["fired"] > 0, engine.gates.stats
    ok("attaching a belt to the dangling tip started it on the next tick")


def test_one_source_fills_two_belts():
    """Independent output channels, each feeding its own run.

    This is why generators skip unconnected channels at all: with three ports
    and one belt, two waiting zones would fill once and block the machine for
    good. It is also why the channels must not fold onto one slot the way a
    Sink's inputs do -- a waiting zone is cleared by whoever drains it, so
    two belts sharing a channel would split one item per tick between them
    instead of each getting a full run.

    Uses the north and east faces. This test used to run north and south,
    which was the natural pair when Source carried all six perimeter faces;
    it is the only test whose *geometry* depended on the dropped ones.
    """
    src = engine.place_structure(Source, 10, 10)   # 2x1 over (10,10)-(11,10)
    # North face of cell 0 feeds an eastward run along the top...
    for x in range(10, 16):
        engine.place_belt(x, 11, cell.DIR_PX)
    engine.place_structure(Sink, 16, 11)
    # ...and the east face of cell 1 feeds another, straight out of the side.
    for x in range(12, 18):
        engine.place_belt(x, 10, cell.DIR_PX)
    engine.place_structure(Sink, 18, 9)
    engine.sync_segments()
    assert engine.segments.count == 2, engine.segments.count

    _run(30)
    segs = list(engine.segments.segments.values())
    assert all(s.count > 0 for s in segs), [s.count for s in segs]
    assert engine.gates.stats["blocked"] == 0, (
        f"independent channels must not block each other: "
        f"{engine.gates.stats}")
    stuck = [c for c in range(src.output_count()) if src.out_held(c)]
    assert not stuck, f"channels {stuck} filled a zone nothing drains"
    ok(f"one Source filled two runs: {engine.gates.stats['consumed']} items "
       f"consumed, no channel blocked another")


def test_a_generator_skips_its_dangling_faces():
    """The four faces with nothing attached stay empty, and nothing stalls."""
    src = engine.place_structure(Source, 10, 10)
    for x in range(10, 16):
        engine.place_belt(x, 11, cell.DIR_PX)
    engine.place_structure(Sink, 16, 11)
    engine.sync_segments()

    connected = engine.gates._connected_channels(src)
    assert len(connected) == 1, (
        f"exactly one face should own that run, got {sorted(connected)}")
    _run(30)
    # Every other channel stayed empty rather than filling once and jamming.
    for c in range(src.output_count()):
        if c in connected:
            continue
        assert not src.out_held(c), f"dangling channel {c} filled its zone"
    assert engine.gates.stats["blocked"] == 0, engine.gates.stats
    assert engine.gates.stats["unconnected"] > 0, engine.gates.stats
    assert engine.gates.stats["consumed"] > 0, "the connected face must flow"
    ok(f"5 dangling faces stayed empty while one fed "
       f"{engine.gates.stats['consumed']} items")


def test_two_faces_on_one_run_do_not_both_fill():
    """A run takes from one port, so the losing face must not fill a zone.

    A 2x1 Source has two +Y faces, and a long enough run touches both. Only
    one can be the segment's source (take_dir decides), and the other must
    read as unconnected -- otherwise it fills a waiting zone nothing will
    ever drain and sits there as a permanently stuck item.
    """
    src = engine.place_structure(Source, 10, 10)
    # The run spans both cells of the footprint, so both +Y faces touch it.
    for x in range(10, 16):
        engine.place_belt(x, 11, cell.DIR_PX)
    engine.place_structure(Sink, 16, 11)
    engine.sync_segments()

    faces = engine.gates._output_faces(src)
    plus_y = [ch for (_xy, f, ch) in faces if f == cell.DIR_PY]
    assert len(plus_y) == 2, plus_y
    connected = engine.gates._connected_channels(src)
    assert len([c for c in plus_y if c in connected]) == 1, (
        f"exactly one of the two +Y faces should own the run, got "
        f"{sorted(connected)}")

    _run(30)
    assert not [c for c in plus_y if src.out_held(c)], (
        "the losing face filled a zone that nothing drains")
    ok("two faces touching one run: only the owning face emits")


def test_a_gate_missing_an_input_does_not_fire():
    """FIRE_ALL means all: a Shear cannot compute x+y from x alone."""
    gate = engine.place_structure(Shear, 10, 10)
    engine.sync_segments()
    gate.set_input(0, 3.0)
    _run(5)
    assert gate.outputs_clear(), "a starved gate must not produce anything"
    assert gate.in_held(0), "the fed input must be held, not consumed"
    assert engine.gates.stats["starved"] > 0, engine.gates.stats
    ok("a half-fed Shear stayed starved for 5 ticks and kept its input")


def test_sink_fires_on_any_single_input():
    """FIRE_ANY: waiting for all eight faces would deadlock a normal sink."""
    snk = engine.place_structure(Sink, 10, 10)
    engine.sync_segments()
    assert snk.input_count() == 4, (
        f"Sink's eight faces should fold onto four channels, one per "
        f"footprint cell, got {snk.input_count()}")
    snk.set_input(0, 42.0)
    _run(1)
    assert not snk.in_held(0), "the sink should have consumed it"
    assert snk.outputs_clear(), "a sink emits nothing"
    assert engine.gates.stats["consumed"] > 0
    ok("a Sink fed on one face consumed immediately")


def test_a_sink_clears_every_channel_in_one_tick():
    """Four fed channels must all be consumed by a single tick, not queued.

    `structure_phase()` clears every held input under FIRE_ANY, so this is
    really pinning that the channels are independent slots rather than
    aliases of one. With all eight faces folded onto channel 0 there was only
    ever one value to clear, and this test could not have been written.
    """
    snk = engine.place_structure(Sink, 10, 10)
    engine.sync_segments()
    for ch in range(snk.input_count()):
        snk.set_input(ch, float(ch) + 1.0)
    before = engine.gates.stats["consumed"]
    _run(1)
    held = [c for c in range(snk.input_count()) if snk.in_held(c)]
    assert not held, f"channels {held} survived the tick"
    took = engine.gates.stats["consumed"] - before
    assert took == 4, f"a sink should eat 4 channels in one tick, ate {took}"
    ok("a Sink consumed all four channels in a single tick")


def test_two_saturated_belts_feed_one_sink_at_full_rate():
    """A sink must eat several saturated belts at once, not one belt's worth.

    This is the test the per-cell channel fold exists for. A channel is one
    slot with one occupancy flag, so it carries **one item per tick** -- when
    all eight faces folded onto channel 0, the first belt to load it made
    `accept_from()` refuse the other until the next structure phase, and two
    saturated belts delivered the throughput of one. The symptom is a
    bottleneck that looks like the belts are at fault.

    Asserts the rate rather than merely "items arrived": one belt alone would
    still pass a non-zero check, which is exactly how this went unnoticed.
    """
    # Two independent Source -> belt -> Sink runs, arriving at two *different*
    # cells of the same 2x2 sink. Different cells is the point: two faces of
    # one cell still share a channel by design.
    engine.place_structure(Source, 4, 11)          # 2x1 over (4,11)-(5,11)
    for x in range(6, 12):
        engine.place_belt(x, 11, cell.DIR_PX)
    engine.place_structure(Source, 4, 8)           # 2x1 over (4,8)-(5,8)
    for x in range(6, 12):
        engine.place_belt(x, 8, cell.DIR_PX)
    # Sink origin (12,10) covers (12,10)-(13,11), so the y=11 run lands on
    # its top-left cell. A second sink would defeat the point of the test, so
    # route the lower run into the same one via a vertical approach.
    snk = engine.place_structure(Sink, 12, 10)
    for y in range(8, 10):
        engine.place_belt(12, y, cell.DIR_PY)
    engine.sync_segments()

    # Let the pipeline fill, then measure over a window where both runs are
    # saturated -- the first few ticks are latency, not throughput.
    _run(40)
    before = engine.gates.stats["consumed"]
    _run(20)
    took = engine.gates.stats["consumed"] - before

    assert took >= 38, (
        f"two saturated belts into one sink consumed {took} items over 20 "
        f"ticks; expected ~40 (2/tick). A rate near 20 means the two runs "
        f"are sharing one input channel and serialising.")
    channels = {c for c in range(snk.input_count())}
    assert len(channels) == 4, snk.input_count()
    ok(f"two saturated belts fed one Sink {took} items in 20 ticks")


def test_back_pressure_stalls_a_blocked_gate():
    """A full waiting zone stops the gate, and it recovers when drained.

    This is the only mechanism: nothing asks "is the belt full". A gate reads
    its own output zone, which the belt phase empties when it can.
    """
    gate = engine.place_structure(Shear, 10, 10)
    # Connected but tiny: a one-cell run per output, so each takes exactly one
    # item and then backs up. A *dangling* output would not block at all --
    # its result is destroyed -- so back pressure needs a real consumer that
    # has stopped consuming.
    _drain_every_output(gate)
    engine.sync_segments()

    # Fill the runs first. A one-cell run holds **two** items at the default
    # spacing (capacity is a power of two), so it takes more than one fire to
    # saturate -- feeding until the zones stay occupied rather than assuming a
    # count keeps this independent of UNIT, d and v.
    for _ in range(8):
        gate.set_input(0, 1.0)
        gate.set_input(1, 1.0)
        _run(1)
        if not gate.outputs_clear():
            break
    assert not gate.outputs_clear(), (
        "the one-cell runs never backed up, so nothing is blocked")

    # Now it is genuinely blocked: a fresh pair of inputs cannot be taken.
    gate.set_input(0, 5.0)
    gate.set_input(1, 5.0)
    _run(1)
    held_value = gate.out_value(1)

    before = engine.gates.stats["blocked"]
    _run(5)
    assert engine.gates.stats["blocked"] >= before + 5, engine.gates.stats
    assert gate.out_value(1) == held_value, (
        f"the blocked gate overwrote its waiting zone: {gate.out_value(1)} "
        f"was {held_value}")
    assert gate.in_held(0) and gate.in_held(1), "inputs must be held"
    ok("a blocked Shear held its result and its inputs for 5 ticks")

    # Drain by hand; it fires on the next tick, taking the inputs it held.
    for i in range(2):
        gate.clear_output(i)
    _run(1)
    assert gate.out_value(1) == 10.0, (
        f"draining should have let it compute 5+5, got {gate.out_value(1)}")
    ok("draining the waiting zone let it fire again immediately")


def test_a_backed_up_belt_eventually_stalls_its_source():
    """Back pressure propagates: belt full -> zone full -> gate blocked."""
    src = engine.place_structure(Source, 7, 10)
    engine.place_belt_path((9, 10), (12, 10))   # nowhere to go
    engine.sync_segments()
    seg = next(iter(engine.segments.segments.values()))

    _run(25)
    assert seg.count == engine.belts.capacity_of(seg), (
        f"{seg.count} items on a belt with capacity "
        f"{engine.belts.capacity_of(seg)}")
    _ch = _feeding_channel(src, seg)
    assert src.out_held(_ch), (
        "with the belt full the Source's waiting zone should stay occupied")
    assert engine.gates.stats["blocked"] > 0, engine.gates.stats
    ok(f"belt packed to {seg.count}, Source blocked with its zone full")


def test_a_full_belt_is_never_even_offered_an_item():
    """A packed belt holds the gate's item without consulting it.

    Worth pinning because it explains the shape of the producer protocol.
    `BeltSim` tests for room *before* calling `offer()`, so the only way an
    offer can be refused is the gate having nothing to give -- there is no
    reachable offer-then-decline path.

    That makes the separate `took()` callback **defensive rather than
    load-bearing**: clearing the waiting zone inside `offer_to()` instead
    would behave identically today, and a mutation that does so is not
    caught by anything. It stays separate because the protocol is then
    correct by construction rather than by a coincidence of where the room
    check sits, and because a future producer that can be asked
    speculatively (a splitter choosing between two belts, say) would make
    the distinction matter immediately.
    """
    src = engine.place_structure(Source, 7, 10)
    engine.place_belt_path((9, 10), (12, 10))     # short, and drains nowhere
    engine.sync_segments()
    seg = next(iter(engine.segments.segments.values()))

    _run(25)
    assert seg.count == engine.belts.capacity_of(seg), seg.count
    _ch = _feeding_channel(src, seg)
    held = src.out_value(_ch)
    assert src.out_held(_ch), "the Source should be holding a refused item"

    offers = []
    real_offer = engine.gates.offer_to

    def counting_offer(s):
        offers.append(s.sid)
        return real_offer(s)

    for _ in range(10):
        engine.gates.set_tick(engine.clock.tick)
        engine.belts.tick(engine.clock.tick, offer=counting_offer,
                          accept=engine.gates.accept_from,
                          took=engine.gates.took_from)
        engine.clock.tick += 1

    assert not offers, (
        f"a packed belt should not be asked for items at all, got "
        f"{len(offers)} offers")
    assert src.out_held(_ch) and src.out_value(_ch) == held, (
        f"the held value changed from {held} to {src.out_value(_ch)}")
    assert seg.count == engine.belts.capacity_of(seg), seg.count
    ok(f"10 ticks against a packed belt: no offers made, the Source kept "
       f"its item ({held:+g})")


def test_two_gates_connect_directly_with_no_belt():
    """Adjacent gates hand items over, one tick per hop like a belt."""
    # A Source's +X tip at (8,10) touching a Sink at (9,10).
    src = engine.place_structure(Source, 7, 10)
    snk = engine.place_structure(Sink, 9, 10)
    engine.sync_segments()
    assert engine.segments.count == 0, "no belts in this layout"

    _run(6)
    assert engine.gates.stats["direct"] > 0, (
        f"nothing moved directly between adjacent gates: "
        f"{engine.gates.stats}")
    assert engine.gates.stats["consumed"] > 0, engine.gates.stats
    ok(f"Source -> Sink with no belt: {engine.gates.stats['direct']} direct "
       f"hand-offs, {engine.gates.stats['consumed']} consumed")


def test_ghost_gates_never_fire():
    """A preview has no business running the simulation."""
    harness.select_tool("Shear")
    harness.put_cursor_at_cell(10, 10)
    step()
    assert harness.ghost_cell_count() > 0, "expected a ghost preview"

    before = dict(engine.gates.stats)
    _run(5)
    assert engine.gates.stats["fired"] == before["fired"], (
        "a ghost gate fired")
    # And it is not even in the gate list.
    assert not any(g.is_ghost for g in engine.gates.gates())
    ok("a ghosted Shear never fired and is absent from the gate list")


def test_erasing_a_gate_leaves_no_stale_state():
    """A freed row must not keep feeding or blocking anything."""
    engine.place_structure(Source, 7, 10)
    engine.place_belt_path((9, 10), (14, 10))
    snk = engine.place_structure(Sink, 15, 10)
    engine.sync_segments()
    _run(10)
    assert engine.gates.stats["consumed"] > 0

    row = snk.row
    engine.erase(15, 10)
    engine.sync_segments()
    assert not board.is_row_alive(row), "the sink's row should be free"
    seg = next(iter(engine.segments.segments.values()))
    assert seg.dst is None, f"the segment still points at a dead gate: {seg.dst}"

    # The run now dead-ends, so it fills and stops. Nothing should crash.
    _run(20)
    assert seg.count == engine.belts.capacity_of(seg), seg.count
    assert not engine.belts.audit(engine.clock.tick)
    ok("erasing the Sink left the segment dangling and the belt simply filled")


def test_a_segment_rebuild_flushes_gate_state_too():
    """Editing belts clears what gates are holding, not just the runs.

    **This test used to assert the opposite**, on the reasoning that gate
    state lives in entity rows and so should be untouched by a rebuild of
    derived segment state. That is tidy and it was wrong: a rebuild drops
    every item on every belt, but material that has already *left* a belt
    sits in a gate's input channel or output waiting zone, and the next tick
    pushes it back onto the fresh runs. Items the player watched vanish
    reappeared a tick later.

    The giveaway was that it is bounded rather than continuous -- about one
    stray value per gate port that happened to be holding something -- which
    reads exactly like a Source simply refilling, which is why it survived
    both this test and ordinary play.

    So `sync_segments()` now calls `flush_material()`, making it the single
    meaning of "start fresh" alongside a Source's value change. Board-wide on
    purpose: an item is a payload and an entry tick with no provenance, so
    "only the gates near the edit" is not answerable.
    """
    gate = engine.place_structure(Shear, 10, 10)
    engine.sync_segments()
    gate.set_input(0, 7.0)
    gate.set_output(1, 9.0)

    # Force a rebuild by editing belts elsewhere.
    engine.place_belt_path((20, 20), (26, 20))
    engine.sync_segments()
    assert engine.segments.stats["rebuilds"] > 0

    assert not gate.in_held(0), (
        f"the gate's input survived a rebuild holding {gate.in_value(0)}; it "
        f"would re-enter the network on the next tick")
    assert gate.outputs_clear(), (
        f"the gate's waiting zone survived a rebuild holding "
        f"{gate.out_value(1)}")
    ok("a segment rebuild flushed the gate's input and waiting zone")


def test_an_edit_does_not_resurrect_items_onto_the_belts():
    """The reported bug, end to end: no stale value comes back after an edit.

    Distinguishes old material from new by changing what the Source emits at
    the same time as the edit, so anything carrying the old value can only
    have come from state the edit failed to clear. Asserting "the belts
    emptied" is not enough -- they do empty, and then refill a tick later
    from the gates.
    """
    src = engine.place_structure(Source, 10, 20)
    src.emit_value = 7.0
    engine.place_belt_path((12, 20), (19, 20))
    engine.place_structure(Shear, 20, 20)
    engine.place_belt_path((22, 20), (33, 20))
    engine.sync_segments()
    _run(50)

    # Saturated, with material inside the gates as well as on the belts.
    held = [c for g in engine.gates.gates()
            for c in range(g.input_count()) if g.in_held(c)]
    assert held, "expected a gate to be holding something before the edit"

    # Edit the network and change the emitted value in one go. 7.0 (and the
    # Shear's 14.0) can now only come from un-flushed state.
    src.emit_value = 2.0
    engine.erase(26, 20)
    engine.sync_segments()
    _run(20)

    seen = set()
    for seg in engine.segments.segments.values():
        for k in range(seg.head, seg.tail):
            seen.add(engine.belts.payload_of(seg, k))
    stale = {v for v in seen if v in (7.0, 14.0)}
    assert not stale, (
        f"stale payloads {sorted(stale)} reappeared after the edit; the "
        f"belts carried {sorted(seen)}")
    assert seen, "nothing flowed after the edit at all"
    ok(f"after an edit the belts carried only fresh values: {sorted(seen)}")


def test_long_chain_reaches_steady_throughput():
    """Two gates in series still settle at one item per tick.

    Each hop costs a tick of latency, but latency is not throughput: once the
    pipeline is full every stage fires every tick. If the belt phase has pop
    and push inverted, this is where it shows as a shortfall.
    """
    engine.place_structure(Source, 3, 10)
    engine.place_belt_path((5, 10), (9, 10))
    sh = engine.place_structure(Shear, 10, 10)        # 2x1 at (10,10)
    engine.place_belt_path((12, 10), (16, 10))
    engine.place_structure(Sink, 17, 10)
    engine.sync_segments()
    # The Shear needs its x channel too, which no belt feeds here.
    assert engine.segments.count == 2, engine.segments.count

    _run(30)
    # x is never fed, so the Shear is starved and nothing reaches the Sink --
    # which is itself the correct behaviour and worth pinning.
    assert engine.gates.stats["starved"] > 0, engine.gates.stats
    assert sh.outputs_clear(), "a starved Shear must produce nothing"
    upstream = engine.segments.at(9, 10)
    assert upstream.count == engine.belts.capacity_of(upstream), (
        f"the belt into a starved gate should back up: {upstream.count}")
    ok(f"a chain with one unfed channel stalls correctly: upstream belt "
       f"packed to {upstream.count}, nothing emitted")


def test_a_fully_wired_shear_computes_through_belts():
    """The whole stack: Sources feed a Shear by belt, results leave by belt.

    Every other arithmetic test feeds the gate's channels directly. This one
    builds the real thing -- two Sources, two input runs, two output runs, a
    Sink on each -- and checks the *payloads* that come out. It is the test
    that would catch a channel wired to the wrong face, which the direct-feed
    tests cannot see.

    Both outputs must be drained. A Shear emits x unchanged as well as x+y,
    and a gate with any waiting zone still occupied is blocked -- so leaving
    the pass-through output dangling stalls the machine permanently. That is
    correct behaviour and worth knowing when building one.
    """
    gate = engine.place_structure(Shear, 20, 20)

    # x channel: a Source whose tip emits -Y at (20,27), then belts down.
    engine.place_structure(Source, 20, 27, cell.DIR_NY)
    for y in range(21, 27):
        engine.place_belt(20, y, cell.DIR_NY)
    # y channel: a Source whose tip emits +X at (13,20), then belts east.
    engine.place_structure(Source, 12, 20, cell.DIR_PX)
    for x in range(14, 20):
        engine.place_belt(x, 20, cell.DIR_PX)
    # Drain the mutated channel east, and the pass-through south.
    for x in range(22, 27):
        engine.place_belt(x, 20, cell.DIR_PX)
    engine.place_structure(Sink, 27, 20)
    for y in range(19, 15, -1):
        engine.place_belt(20, y, cell.DIR_NY)
    engine.place_structure(Sink, 20, 14)
    engine.sync_segments()

    _run(60)
    stats = engine.gates.stats
    assert stats["blocked"] == 0, (
        f"a fully-drained chain should never block: {stats}")
    assert stats["consumed"] > 100, stats

    # Source emits -1 on both channels, so x+y = -2 and x = -1.
    east = engine.segments.at(26, 20)
    south = engine.segments.at(20, 17)
    assert east.count and south.count, (east.count, south.count)
    east_vals = [engine.belts.payload_of(east, k)
                 for k in range(east.head, east.tail)]
    south_vals = [engine.belts.payload_of(south, k)
                  for k in range(south.head, south.tail)]
    assert all(v == -2.0 for v in east_vals), (
        f"the mutated channel should carry x+y = -2, got {east_vals}")
    assert all(v == -1.0 for v in south_vals), (
        f"the pass-through should carry x = -1, got {south_vals}")
    assert not (engine.segments.audit()
                + engine.belts.audit(engine.clock.tick))
    ok(f"a belt-fed Shear computed x+y={east_vals[0]} east and passed "
       f"x={south_vals[0]} south, {stats['consumed']} items consumed")


def test_a_dangling_output_destroys_and_does_not_block():
    """A dangling output is a sink: the gate runs on and the result vanishes.

    The inverse of what this file used to assert. Leaving a Shear's
    pass-through unconnected once stalled the machine completely after a
    single fire, on the principle that a gate must never silently destroy
    material. Destroying it is now the specified behaviour -- it is what
    makes an explicit Sink unnecessary for outputs you do not want -- so the
    machine must instead run at full rate with the pass-through evaporating.

    Asserting the *rate* rather than merely "it kept firing" is the point: a
    gate that fired occasionally would also pass a non-zero check, and a
    half-rate gate is exactly the symptom a mistake here would produce.
    """
    gate = engine.place_structure(Shear, 20, 20)
    engine.place_structure(Source, 20, 27, cell.DIR_NY)
    for y in range(21, 27):
        engine.place_belt(20, y, cell.DIR_NY)
    engine.place_structure(Source, 12, 20, cell.DIR_PX)
    for x in range(14, 20):
        engine.place_belt(x, 20, cell.DIR_PX)
    # Drain the mutated channel only; leave the pass-through dangling.
    for x in range(22, 27):
        engine.place_belt(x, 20, cell.DIR_PX)
    engine.place_structure(Sink, 27, 20)
    engine.sync_segments()

    # Let the two feed runs fill, then measure steady state.
    _run(20)
    before_destroyed = engine.gates.stats["destroyed"]
    before_blocked = engine.gates.stats["blocked"]
    east = engine.segments.at(26, 20)
    before_items = east.tail
    _run(20)

    assert not gate.out_held(0), (
        "the dangling pass-through must hold nothing -- its result is "
        "destroyed at fire time, not parked in a waiting zone")
    assert engine.gates.stats["blocked"] == before_blocked, (
        f"a dangling output must never block: {engine.gates.stats}")

    # One fire per tick, so one discard per tick and one item east per tick.
    destroyed = engine.gates.stats["destroyed"] - before_destroyed
    emitted = east.tail - before_items
    assert destroyed == 20, (
        f"expected one discard per tick over 20 ticks, got {destroyed}")
    assert emitted == 20, (
        f"the gate should emit one item east per tick, got {emitted} -- a "
        f"shortfall means the dangling channel is still throttling it")
    ok(f"a dangling pass-through destroyed {destroyed} values over 20 ticks "
       f"while the gate ran at full rate ({emitted} items east)")


def test_audits_clean_through_a_running_chain():
    """Every audit stays clean while gates and belts run together."""
    engine.place_structure(Source, 7, 10)
    engine.place_belt_path((9, 10), (14, 10))
    engine.place_structure(Sink, 15, 10)
    engine.place_structure(Source, 7, 20)
    engine.place_belt_path((9, 20), (12, 20))
    engine.sync_segments()

    for _ in range(40):
        _run(1)
        # sync_to_device() before the device audit: the mirror is only
        # expected to match after an upload, and _run() deliberately does
        # not touch the GPU.
        engine.sync_to_device()
        problems = (engine.segments.audit()
                    + engine.belts.audit(engine.clock.tick)
                    + engine.belt_device.audit())
        assert not problems, (engine.clock.tick, problems[:4])
    ok("40 ticks of two chains: segmentation, transport and device audits "
       "all clean")


# ======================================================================
# The machines added with the second gate vocabulary
# ======================================================================
#
# core/entity.py already self-checks each `fire()` against its signature with
# no board. These are the ones that need the whole stack: that the gate is
# reachable through its declared ports, that items arrive and leave on the
# faces the class says, and -- for Route and Merge -- that the two new firing
# mechanisms behave on a real board rather than only in a view.


def test_reciprocal_and_floor_compute_on_the_board():
    """Two single-input machines, wired through their real ports."""
    rc = engine.place_structure(Reciprocal, 10, 10)
    got = _fire_once_and_read(rc, [4.0])
    assert got[0] == 0.25, got
    ok("Reciprocal fired on the board: 4 -> 0.25")

    # 1/0 clamps rather than reaching the belt as an infinity, which nothing
    # downstream could represent or render.
    harness.reset()
    rc = engine.place_structure(Reciprocal, 10, 10)
    got = _fire_once_and_read(rc, [0.0])
    assert got[0] == VALUE_LIMIT, got
    ok(f"Reciprocal clamped 1/0 to {VALUE_LIMIT:g} instead of emitting inf")

    harness.reset()
    fl = engine.place_structure(Floor, 10, 10)
    got = _fire_once_and_read(fl, [2.7])
    assert got[0] == 2.0, got
    ok("Floor fired on the board: 2.7 -> 2")

    # Toward -inf, which is the half of floor() a truncation would get wrong
    # while still passing on positive inputs.
    harness.reset()
    fl = engine.place_structure(Floor, 10, 10)
    got = _fire_once_and_read(fl, [-2.7])
    assert got[0] == -3.0, f"floor(-2.7) must be -3, got {got[0]}"
    ok("Floor rounded -2.7 toward -inf, to -3")


def test_max_puts_the_same_value_on_both_outputs():
    """Both lanes carry the larger value: Max fans out as well as compares."""
    mx = engine.place_structure(Max, 10, 10)
    got = _fire_once_and_read(mx, [3.0, 9.0])
    assert got[0] == 9.0 and got[1] == 9.0, got
    ok("Max fired on the board: (3,9) -> 9 on both outputs")


def test_max_with_one_output_dangling_runs_at_full_rate():
    """The second lane may simply be left unconnected.

    The spec's "do not create an item there at all" case. It needs no special
    handling -- a dangling output is destroyed for every gate now -- but it is
    the configuration a player will actually build, so it is worth pinning
    that it does not throttle the machine.
    """
    mx = engine.place_structure(Max, 10, 10)
    # Drain the left-hand output only; leave the right one dangling.
    faces = sorted(engine.gates._output_faces(mx), key=lambda f: f[0][0])
    (xy, face, drained_ch) = faces[0]
    step_v = cell.DIR_STEPS[face]
    start = (xy[0] + step_v[0], xy[1] + step_v[1])
    end = (start[0], start[1] - 5)
    engine.place_belt_path(start, end)
    # The Sink must actually be *under* the run's last cell, or the belt has
    # no consumer, fills, and back-pressures the gate -- which would make this
    # test measure a blocked belt rather than a dangling output. A 2x2's
    # origin is its lower-left corner, so it goes one further down and one
    # left of the end cell for its top-right tile to sit in the run's path.
    engine.place_structure(Sink, end[0] - 1, end[1] - 2)
    engine.sync_segments()

    drain_seg = engine.segments.at(*start)
    assert drain_seg is not None and drain_seg.dst is not None, (
        "the drained lane needs a real consumer, or this test measures back "
        "pressure instead of the dangling output")

    connected = engine.gates._connected_channels(mx)
    assert drained_ch in connected, "the drained lane should be connected"
    assert len(connected) == 1, (
        f"only one lane is wired, got {sorted(connected)}")

    before = engine.gates.stats["destroyed"]
    before_blocked = engine.gates.stats["blocked"]
    for _ in range(20):
        mx.set_input(0, 3.0)
        mx.set_input(1, 9.0)
        _run(1)
    destroyed = engine.gates.stats["destroyed"] - before
    assert destroyed == 20, (
        f"the dangling lane should discard one value per tick, got {destroyed}")
    assert engine.gates.stats["blocked"] == before_blocked, (
        f"a dangling lane must not block: {engine.gates.stats}")
    ok(f"Max with one lane dangling fired 20/20 ticks, discarding "
       f"{destroyed} values")


def test_route_sends_an_item_one_way_only():
    """The either/or output: one path gets the item, the other gets nothing.

    This is what `fire()` returning None buys. Asserting the *silent* path is
    the point -- a copy going both ways with one destroyed would look
    identical on the taken path.
    """
    for control, want_right in ((1.0, True), (0.0, False), (-3.0, False)):
        harness.reset()
        rt = engine.place_structure(Route, 10, 10)
        got = _fire_once_and_read(rt, [5.0, control], expect_empty=True)

        # Channels 0 and 1 are the two data outputs, left and right.
        live = {ch: v for ch, v in got.items()
                if ch in (0, 1) and v is not None}
        assert len(live) == 1, (
            f"control {control} put items on {sorted(live)}; exactly one "
            f"data output must fire")
        ch = next(iter(live))
        assert ch == (1 if want_right else 0), (
            f"control {control} routed to channel {ch}")
        assert live[ch] == 5.0, live
        # The control itself passes through unchanged on channel 2.
        assert got[2] == control, f"the control should survive: {got[2]}"
    ok("Route sent the item down exactly one path for c>0, c==0 and c<0, "
       "passing the control through")


def test_route_emits_nothing_on_the_path_not_taken():
    """The untaken path's run must stay genuinely empty, tick after tick.

    Separate from the above because one fire cannot distinguish "nothing was
    emitted" from "an item was emitted and has not arrived yet".
    """
    rt = engine.place_structure(Route, 10, 10)
    _drain_every_output(rt)
    engine.sync_segments()

    # Channel 1 is the right-hand data output, taken only when c > 0.
    by_ch = {ch: (xy, face) for (xy, face, ch)
             in engine.gates._output_faces(rt)}
    (xy, face) = by_ch[1]
    step_v = cell.DIR_STEPS[face]
    right = engine.segments.at(xy[0] + step_v[0], xy[1] + step_v[1])
    assert right is not None, "the right-hand output lost its run"

    for _ in range(15):
        rt.set_input(0, 5.0)
        rt.set_input(1, -1.0)       # always route left
        _run(1)
    assert right.count == 0, (
        f"the untaken path carried {right.count} items -- Route is emitting "
        f"on both outputs and relying on the discard")
    ok("15 ticks routing left: the right-hand run never received an item")


def test_merge_fires_without_waiting_for_both_lanes():
    """The control plus the selected lane is enough -- the other may be empty.

    FIRE_ALL would deadlock here, which is the whole reason FIRE_SELECT and
    the overridable `inputs_ready()` exist.
    """
    mg = engine.place_structure(Merge, 10, 10)
    _drain_every_output(mg)
    engine.sync_segments()

    before = engine.gates.stats["fired"]
    mg.set_input(Merge.CH_A, 5.0)
    mg.set_input(Merge.CH_CONTROL, 1.0)
    assert not mg.in_held(Merge.CH_B), "b must start empty for this test"
    _run(1)
    assert not mg.in_held(Merge.CH_A), "a should have been consumed"
    assert engine.gates.stats["fired"] > before, (
        f"Merge did not fire with b empty: {engine.gates.stats}")
    ok("Merge fired on control+a alone, with b empty")


def test_merge_waits_when_the_control_selects_an_empty_lane():
    """Readiness follows the control's *value*, not merely its presence.

    The other half of FIRE_SELECT: holding the control and the *wrong* lane
    is not enough. A rule keyed on occupancy alone (FIRE_ANY) would fire here
    and emit whichever lane happened to be loaded.
    """
    mg = engine.place_structure(Merge, 10, 10)
    _drain_every_output(mg)
    engine.sync_segments()

    before = engine.gates.stats["fired"]
    mg.set_input(Merge.CH_B, 6.0)
    mg.set_input(Merge.CH_CONTROL, 1.0)      # selects a, which is empty
    _run(5)
    assert engine.gates.stats["fired"] == before, (
        f"Merge fired while its selected lane was empty: {engine.gates.stats}")
    assert mg.in_held(Merge.CH_B), "b must still be waiting"
    assert mg.in_held(Merge.CH_CONTROL), "the control must still be waiting"

    # Feed the selected lane and it goes immediately.
    mg.set_input(Merge.CH_A, 5.0)
    _run(1)
    assert engine.gates.stats["fired"] > before, "it should fire now"
    ok("Merge waited 5 ticks for the lane its control named, then fired")


def test_merge_leaves_the_unselected_lane_loaded():
    """The lane the control did not pick is neither emitted nor destroyed.

    `consumed()` exists for exactly this. The default -- clear every fed
    channel -- would silently eat an item the player can see in the machine.
    """
    mg = engine.place_structure(Merge, 10, 10)
    _drain_every_output(mg)
    engine.sync_segments()

    mg.set_input(Merge.CH_A, 5.0)
    mg.set_input(Merge.CH_B, 6.0)
    mg.set_input(Merge.CH_CONTROL, 1.0)      # selects a
    before_destroyed = engine.gates.stats["destroyed"]
    _run(1)

    assert not mg.in_held(Merge.CH_A), "the selected lane is consumed"
    assert mg.in_held(Merge.CH_B), (
        "the unselected lane must stay loaded, not be consumed")
    assert mg.in_value(Merge.CH_B) == 6.0, (
        f"b's value changed to {mg.in_value(Merge.CH_B)}")
    assert engine.gates.stats["destroyed"] == before_destroyed, (
        "nothing should have been destroyed -- b was held, not discarded")

    # And the held lane is usable later: flip the control and it comes out.
    by_ch = {ch: (xy, face) for (xy, face, ch)
             in engine.gates._output_faces(mg)}
    (xy, face) = by_ch[0]
    step_v = cell.DIR_STEPS[face]
    out = engine.segments.at(xy[0] + step_v[0], xy[1] + step_v[1])

    mg.set_input(Merge.CH_CONTROL, -1.0)     # now selects b
    _run(1)
    assert not mg.in_held(Merge.CH_B), "b should now be consumed"
    vals = [engine.belts.payload_of(out, k)
            for k in range(out.head, out.tail)]
    assert 6.0 in vals, f"b's value 6.0 never arrived: {vals}"
    ok("Merge held b through a fire selecting a, then emitted it when the "
       "control flipped")


def test_a_hidden_machine_still_works_on_the_board():
    """Swap and Sink are off the toolbelt but must still simulate.

    An older saved board can contain either, so hiding a machine must be a
    toolbelt decision and nothing more. This places them the way a load does
    -- straight through the engine, with no tool involved.
    """
    from core.entity import ALL_STRUCTURES, HIDDEN_STRUCTURES, STRUCTURES
    assert Swap in HIDDEN_STRUCTURES and Sink in HIDDEN_STRUCTURES
    assert Swap not in STRUCTURES and Sink not in STRUCTURES
    assert set(ALL_STRUCTURES) == set(STRUCTURES) | set(HIDDEN_STRUCTURES)

    # A hidden Swap still computes, through its real ports.
    sw = engine.place_structure(Swap, 10, 10)
    got = _fire_once_and_read(sw, [1.0, 7.0, 9.0])
    assert [got[i] for i in range(3)] == [1.0, 9.0, 7.0], got
    ok("a hidden Swap placed by the engine still fired: (1,7,9) -> (1,9,7)")

    # And a hidden Sink still consumes a saturated belt.
    harness.reset()
    engine.place_structure(Source, 7, 10)
    engine.place_belt_path((9, 10), (14, 10))
    engine.place_structure(Sink, 15, 10)
    engine.sync_segments()
    _run(20)
    assert engine.gates.stats["consumed"] > 10, engine.gates.stats
    ok(f"a hidden Sink still consumed "
       f"{engine.gates.stats['consumed']} items in 20 ticks")
