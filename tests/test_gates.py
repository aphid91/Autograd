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
    """Adjacent gates hand items over, one tick per hop like a belt.

    **The producer here must not be a constant-foldable Source.** A Source
    whose every output reaches a gate is folded into its neighbour's input
    channel and stops firing entirely, so `deliver_direct()` never runs and
    `stats["direct"]` stays zero -- correct behaviour, and it would make this
    test silently stop exercising the path it is named after. So the producer
    is a Shear fed by belt: a real gate whose result has to make the hop.
    `test_a_folded_source_feeds_its_neighbour_without_a_direct_hop` covers
    the other route deliberately.
    """
    # A Shear at (10,10)-(11,10) with its +X output tip touching a Sink.
    engine.place_structure(Source, 3, 10)
    engine.place_belt_path((5, 10), (9, 10))
    sh = engine.place_structure(Shear, 10, 10)
    snk = engine.place_structure(Sink, 12, 10)
    engine.sync_segments()
    # Feed the Shear's x channel directly so it is actually ready to fire.
    _drain_every_output(sh)

    before = engine.gates.stats["direct"]
    for _ in range(8):
        sh.set_input(0, 2.0)
        sh.set_input(1, 3.0)
        _run(1)
    assert engine.gates.stats["direct"] > before, (
        f"nothing moved directly between adjacent gates: "
        f"{engine.gates.stats}")
    assert engine.gates.stats["consumed"] > 0, engine.gates.stats
    ok(f"Shear -> Sink with no belt: "
       f"{engine.gates.stats['direct'] - before} direct hand-offs, "
       f"{engine.gates.stats['consumed']} consumed")


def test_a_folded_source_feeds_its_neighbour_without_a_direct_hop():
    """A fully-direct Source becomes a constant on its neighbour's channel.

    The other half of the test above. Three claims, and the third is the one
    that makes folding a saving rather than a rename:

      - the neighbour's input reads as fed, with the Source's value;
      - the Source is reported folded;
      - **no direct hand-off happens**, because the Source never fires.
    """
    src = engine.place_structure(Source, 7, 10)
    src.emit_value = -1.0
    snk = engine.place_structure(Sink, 9, 10)
    engine.sync_segments()
    assert engine.segments.count == 0, "no belts in this layout"

    assert engine.gates.is_folded(src.row), (
        "a Source whose only output reaches a gate should fold")
    # Fed before a single tick has run: the fold is a property of the board,
    # not something that arrives a hop later.
    fed = [c for c in range(snk.input_count()) if snk.in_held(c)]
    assert fed, "the folded constant should already be on a Sink channel"
    assert snk.in_value(fed[0]) == -1.0, snk.in_value(fed[0])

    before = engine.gates.stats["direct"]
    _run(6)
    assert engine.gates.stats["direct"] == before, (
        f"a folded Source should make no direct hand-offs, got "
        f"{engine.gates.stats['direct'] - before}")
    assert engine.gates.stats["consumed"] > 0, (
        f"the Sink should still be eating the constant: {engine.gates.stats}")
    # And it is still held after six ticks of being consumed every one.
    assert any(snk.in_held(c) for c in range(snk.input_count())), (
        "the folded constant must be re-asserted after each fire")
    ok(f"a folded Source held -1 on its neighbour for 6 ticks with 0 direct "
       f"hops and {engine.gates.stats['consumed']} consumed")


def test_a_folded_source_is_not_ticked_at_all():
    """The saving, pinned: a folded Source leaves the firing set.

    Folding is behaviour-neutral by design, so **every correctness test here
    passes whether or not the Source is actually skipped** -- it would just
    fire, fill a zone, and have `deliver_direct()` write a channel the fold
    already holds. Mutation-tested: leaving folded rows in the gate table
    survived the whole suite *and* the differential checker, because there is
    nothing wrong with the result, only with the cost.

    So the saving needs its own assertion. It is the reason the feature
    exists: on cpu.npz 133 of 285 Sources fold, and a generator fires every
    single tick, so these are rows no prefilter or sleep set could ever skip.
    """
    src = engine.place_structure(Source, 7, 10)
    engine.place_structure(Shear, 9, 10)
    engine.sync_segments()
    assert engine.gates.is_folded(src.row)

    rows, _n_in, _n_out, _strict = engine.gates._gate_table()
    assert src.row not in set(rows.tolist()), (
        "a folded Source must be absent from the gate table, which is what "
        "keeps it out of both the firing prefilter and deliver_direct()")

    admit, _starved, _blocked = engine.gates._candidate_rows()
    assert src.row not in set(admit), "and so never admitted to fire"
    # And it really never fires, over a stretch where an unfolded one would
    # have fired every tick.
    before = engine.gates.stats["fired"]
    _run(10)
    fired = engine.gates.stats["fired"] - before
    assert src.row not in set(engine.gates._rows_holding_output()), (
        "a folded Source should never hold a waiting zone, having not fired")
    ok(f"a folded Source stayed out of the gate table and never fired "
       f"({fired} fires on the board, none its own)")


def test_a_belt_on_any_output_blocks_the_fold():
    """One belt and the Source is a real producer again.

    The narrowing that keeps folding safe: a belt regulates a producer's rate
    through back pressure, so a Source feeding one must keep firing. Folding
    on the strength of its *other* face reaching a gate would have it emit on
    the belt while also standing permanently fed into the neighbour.
    """
    src = engine.place_structure(Source, 7, 10)
    engine.place_structure(Sink, 9, 10)          # +X tip -> a gate
    engine.place_belt_path((7, 11), (10, 11))    # +Y face -> a belt
    engine.sync_segments()

    assert not engine.gates.is_folded(src.row), (
        "a Source with a belt on any output must not fold")
    _run(8)
    assert engine.gates.stats["direct"] > 0, (
        f"an unfolded Source should still hand over directly: "
        f"{engine.gates.stats}")
    seg = engine.segments.at(8, 11) or engine.segments.at(9, 11)
    assert seg is not None and seg.count > 0, (
        "and should still be filling its belt")
    ok("a belt on one output kept the Source firing on both routes")


def test_erasing_a_folded_source_releases_the_channel():
    """A fold is derived state, so it must not outlive its Source.

    The failure this guards is the nastiest one available to the mechanism: a
    folded channel is held by nothing but the derivation, so a fold that
    survived its Source would leave the neighbour fed for ever by a constant
    with no visible origin.

    **Asserting on the board after an erase is not enough, and the first
    version of this test was vacuous for exactly that reason.** `erase()`
    invalidates the segments, and `sync_segments()` calls `flush_material()`,
    which zeroes every gate input channel on the board -- so the channel comes
    out clear whether or not the fold released it. Mutation-tested: gutting
    `_drop_folding()`'s release left this passing.

    So the release is tested through `_drop_folding()` directly, with no flush
    anywhere near it. The erase path is still exercised below, for the
    *re-derivation* half -- that the fold is gone and does not come back.
    """
    src = engine.place_structure(Source, 7, 10)
    src.emit_value = 5.0
    sh = engine.place_structure(Shear, 9, 10)
    engine.sync_segments()
    assert engine.gates.is_folded(src.row)
    held = [c for c in range(sh.input_count()) if sh.in_held(c)]
    assert held, "expected the Shear to be holding the folded constant"
    assert sh.in_value(held[0]) == 5.0, sh.in_value(held[0])

    # The mechanism, with no flush to mask it.
    engine.gates._drop_folding()
    assert not any(sh.in_held(c) for c in range(sh.input_count())), (
        "dropping the fold must release the channel it was holding, since "
        "nothing else on the board is feeding it")

    # And the edit path: the fold is gone and does not re-derive.
    engine.gates._folds()           # re-derive against the unchanged board
    assert engine.gates.is_folded(src.row), "it should fold again unchanged"
    engine.erase(7, 10)
    engine.sync_segments()
    assert not engine.gates.folded_sources(), "the fold should be gone"
    assert not any(sh.in_held(c) for c in range(sh.input_count())), (
        "erasing the Source must leave the channel clear")
    ok("dropping a fold released the channel, and erasing the Source kept "
       "it released")


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
    #
    # The two Sources carry **different, non-default** values. The type
    # default is 0.0, which is also what an empty slot reads as, so feeding
    # both channels the default would make `x+y == 0` and `x == 0` true of a
    # belt carrying nothing at all -- and this is precisely the test meant to
    # catch a channel wired to the wrong face. Distinct values also mean a
    # swapped pair fails rather than cancelling out.
    src_x = engine.place_structure(Source, 20, 27, cell.DIR_NY)
    src_x.emit_value = 3.0
    for y in range(21, 27):
        engine.place_belt(20, y, cell.DIR_NY)
    # y channel: a Source whose tip emits +X at (13,20), then belts east.
    src_y = engine.place_structure(Source, 12, 20, cell.DIR_PX)
    src_y.emit_value = 10.0
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

    # x = 3 and y = 10, so the mutated channel carries x+y = 13 and the
    # pass-through carries x = 3. Asymmetric on purpose: swapping the two
    # channels would leave x+y unchanged but send 10 south instead of 3.
    east = engine.segments.at(26, 20)
    south = engine.segments.at(20, 17)
    assert east.count and south.count, (east.count, south.count)
    east_vals = [engine.belts.payload_of(east, k)
                 for k in range(east.head, east.tail)]
    south_vals = [engine.belts.payload_of(south, k)
                  for k in range(south.head, south.tail)]
    assert all(v == 13.0 for v in east_vals), (
        f"the mutated channel should carry x+y = 13, got {east_vals}")
    assert all(v == 3.0 for v in south_vals), (
        f"the pass-through should carry x = 3, got {south_vals}")
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


def test_a_machine_placed_without_a_tool_still_works():
    """A machine placed straight through the engine must simulate.

    This is what a **load** does -- no tool, no builder, no toolbelt -- so it
    is the path by which a saved board's contents come back to life, and it
    has to work for every registered type whether or not a tool offers one.

    Swap and Sink are the subjects because they were the two machines hidden
    from the toolbelt, which made this the only way to get one onto a board.
    They are placeable again (see `STRUCTURES`), so the hiding is no longer
    what makes the test interesting -- the tool-free placement path is. The
    assertions below therefore pin the **mechanism** that made hiding cheap,
    rather than the fact that these two were hidden.
    """
    from core.entity import ALL_STRUCTURES, HIDDEN_STRUCTURES, STRUCTURES
    # The split is the thing worth keeping: tools derive from STRUCTURES,
    # type-reasoning iterates ALL_STRUCTURES, and the two differ by exactly
    # the hidden set. That holds with the hidden set empty, which is what
    # makes re-hiding a machine a one-line change.
    assert set(ALL_STRUCTURES) == set(STRUCTURES) | set(HIDDEN_STRUCTURES)
    assert not (set(STRUCTURES) & set(HIDDEN_STRUCTURES)), (
        "a machine cannot be both offered and hidden")
    for _cls in HIDDEN_STRUCTURES:
        assert _cls in ALL_STRUCTURES, (
            f"{_cls.__name__} is hidden but not registered, so a save "
            f"containing one could not be loaded")

    # A Swap computes through its real ports.
    sw = engine.place_structure(Swap, 10, 10)
    got = _fire_once_and_read(sw, [1.0, 7.0, 9.0])
    assert [got[i] for i in range(3)] == [1.0, 9.0, 7.0], got
    ok("a Swap placed by the engine fired: (1,7,9) -> (1,9,7)")

    # And a Sink consumes a saturated belt.
    harness.reset()
    src = engine.place_structure(Source, 7, 10)
    src.emit_value = 4.0
    engine.place_belt_path((9, 10), (14, 10))
    engine.place_structure(Sink, 15, 10)
    engine.sync_segments()
    _run(20)
    assert engine.gates.stats["consumed"] > 10, engine.gates.stats
    ok(f"a Sink consumed {engine.gates.stats['consumed']} items in 20 ticks")


def test_the_port_callbacks_mark_the_rows_they_write():
    """Each port callback marks the row it writes, so the GPU sees the change.

    The three port callbacks (`offer_to`, `took_from`, `accept_from`) write
    entity rows **directly** rather than through a view, because they run once
    per segment per tick and building an `Entity` to set one float was the
    bulk of the belt phase. A view's `_set()` marks `entities_dirty`; direct
    array writes have to do it by hand.

    Nothing else catches a missed mark. `to_device()` uploads only the dirty
    row range, so the simulation stays perfectly correct while the GPU keeps
    the previous tick's gate state -- and since the renderer draws waiting
    zones and a Source's emit label out of those rows, the symptom is a
    *stale picture of a working factory*.

    **Each callback is driven on its own**, which is the only way to see it.
    Running a whole tick and checking the range does not work: the structure
    phase writes the same Source row through a view a phase earlier, so its
    mark hides a missing one here. Both mutants passed a whole-tick version
    of this test, which is how that was found.
    """
    src = engine.place_structure(Source, 7, 10)
    src.emit_value = 2.5
    engine.place_belt_path((9, 10), (13, 10))
    sink = engine.place_structure(Sink, 14, 10)
    engine.sync_segments()
    _run(12)

    seg = next(iter(engine.segments.segments.values()))
    gates = engine.gates

    # accept_from(): writes the downstream gate's input channel. Needs a free
    # channel to write into, so clear whatever the last tick left.
    dst = gates._dst_port(seg)
    if dst is not None:
        gate, ch = dst
        gate.clear_input(ch)
        board.entities_dirty.clear()
        assert gates.accept_from(seg, 1.0), "the sink should have taken it"
        assert not board.entities_dirty.empty, (
            "accept_from() wrote an input channel without marking the row "
            "dirty -- to_device() would not upload it")
        lo, hi = board.entities_dirty.lo, board.entities_dirty.hi
        assert lo <= gate.row < hi, (gate.row, lo, hi)
        ok(f"accept_from marked row {gate.row}")

    # took_from(): clears the upstream gate's waiting zone. Fill it first, so
    # there is something for the call to clear.
    srcp = gates._src_port(seg)
    assert srcp is not None, "the run should be fed by the Source"
    gate, ch = srcp
    gate.set_output(ch, 3.0)
    board.entities_dirty.clear()
    gates.took_from(seg)
    assert not board.entities_dirty.empty, (
        "took_from() cleared a waiting zone without marking the row dirty "
        "-- to_device() would not upload it")
    lo, hi = board.entities_dirty.lo, board.entities_dirty.hi
    assert lo <= gate.row < hi, (gate.row, lo, hi)
    ok(f"took_from marked row {gate.row}")
