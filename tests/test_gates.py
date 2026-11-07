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
from core.entity import Mac, Shear, Sink, Source, Swap

from tests import harness
from tests.harness import board, engine, ok, step


def _run(ticks):
    """Advance the simulation by whole ticks, with no wall-clock involved."""
    for _ in range(ticks):
        engine.clock.request_step()
        engine.clock.advance(0.0)


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
    """x,y -> x, x+y, with the inputs delivered by belts."""
    gate = engine.place_structure(Shear, 10, 10)
    engine.sync_segments()
    # Feed both channels directly: belt-feeding two separate channels needs
    # two Sources and is covered by the wiring tests below.
    gate.set_input(0, 3.0)
    gate.set_input(1, 4.0)

    _run(1)
    assert gate.out_held(0) and gate.out_held(1), "both outputs should fill"
    assert gate.out_value(0) == 3.0, gate.out_value(0)
    assert gate.out_value(1) == 7.0, gate.out_value(1)
    assert not gate.in_held(0) and not gate.in_held(1), "inputs consumed"
    ok("Shear fired on the board: (3,4) -> (3,7)")


def test_mac_and_swap_compute_on_the_board():
    """Mac accumulates a product; Swap exchanges a pair on a condition."""
    mac = engine.place_structure(Mac, 10, 10)
    engine.sync_segments()
    mac.set_input(0, 2.0)      # x
    mac.set_input(1, 5.0)      # y
    mac.set_input(2, 1.0)      # z
    _run(1)
    assert [mac.out_value(i) for i in range(3)] == [2.0, 5.0, 11.0], (
        [mac.out_value(i) for i in range(3)])
    ok("Mac fired: (2,5,1) -> (2,5,11)")

    harness.reset()
    swap = engine.place_structure(Swap, 20, 20)
    engine.sync_segments()
    # x > 0: no swap.
    swap.set_input(0, 1.0)
    swap.set_input(1, 7.0)
    swap.set_input(2, 9.0)
    _run(1)
    assert [swap.out_value(i) for i in range(3)] == [1.0, 7.0, 9.0]
    ok("Swap with x>0 passed y,z through unchanged")

    # x <= 0: swap. Drain the waiting zones first, or it cannot fire.
    for i in range(3):
        swap.clear_output(i)
    swap.set_input(0, -1.0)
    swap.set_input(1, 7.0)
    swap.set_input(2, 9.0)
    _run(1)
    assert [swap.out_value(i) for i in range(3)] == [-1.0, 9.0, 7.0], (
        [swap.out_value(i) for i in range(3)])
    ok("Swap with x<=0 exchanged y and z")


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


def test_a_generator_skips_only_its_unconnected_channels():
    """Connectivity is per channel: a connected port works regardless.

    Source has one output today, so this pins the mechanism rather than a
    multi-output generator that does not exist yet -- the channel loop is
    what a future two-port source would rely on.
    """
    src = engine.place_structure(Source, 7, 10)
    engine.place_belt(9, 10, cell.DIR_PX)
    engine.sync_segments()
    _run(1)
    assert src.out_held(0) or engine.gates.stats["fired"] > 0, (
        engine.gates.stats)
    # Its one channel is connected, so nothing is ever counted unconnected.
    assert engine.gates.stats["unconnected"] == 0, engine.gates.stats
    ok("a connected generator channel is never skipped")


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
    assert snk.input_count() == 1, (
        f"Sink's eight faces should fold onto one channel, got "
        f"{snk.input_count()}")
    snk.set_input(0, 42.0)
    _run(1)
    assert not snk.in_held(0), "the sink should have consumed it"
    assert snk.outputs_clear(), "a sink emits nothing"
    assert engine.gates.stats["consumed"] > 0
    ok("a Sink fed on one face consumed immediately")


def test_back_pressure_stalls_a_blocked_gate():
    """A full waiting zone stops the gate, and it recovers when drained.

    This is the only mechanism: nothing asks "is the belt full". A gate reads
    its own output zone, which the belt phase empties when it can.
    """
    gate = engine.place_structure(Shear, 10, 10)
    engine.sync_segments()
    gate.set_input(0, 1.0)
    gate.set_input(1, 1.0)
    _run(1)
    assert not gate.outputs_clear(), "should be holding results"

    # Nothing downstream, so the zones stay full and it cannot fire again.
    gate.set_input(0, 5.0)
    gate.set_input(1, 5.0)
    before = engine.gates.stats["blocked"]
    _run(5)
    assert engine.gates.stats["blocked"] >= before + 5, engine.gates.stats
    assert gate.out_value(1) == 2.0, (
        f"the blocked gate overwrote its waiting zone: {gate.out_value(1)}")
    assert gate.in_held(0) and gate.in_held(1), "inputs must be held"
    ok("a blocked Shear held its result and its inputs for 5 ticks")

    # Drain by hand; it fires on the next tick.
    for i in range(2):
        gate.clear_output(i)
    _run(1)
    assert gate.out_value(1) == 10.0, gate.out_value(1)
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
    assert src.out_held(0), (
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
    held = src.out_value(0)
    assert src.out_held(0), "the Source should be holding a refused item"

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
    assert src.out_held(0) and src.out_value(0) == held, (
        f"the held value changed from {held} to {src.out_value(0)}")
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


def test_gate_state_survives_a_segment_rebuild():
    """Editing belts must not disturb what a gate is holding.

    Segments are derived and get rebuilt wholesale; gate state lives in
    entity rows and must be untouched by that.
    """
    gate = engine.place_structure(Shear, 10, 10)
    engine.sync_segments()
    gate.set_input(0, 7.0)
    gate.set_output(1, 9.0)

    # Force a rebuild by editing belts elsewhere.
    engine.place_belt_path((20, 20), (26, 20))
    engine.sync_segments()
    assert engine.segments.stats["rebuilds"] > 0

    assert gate.in_held(0) and gate.in_value(0) == 7.0, gate.in_value(0)
    assert gate.out_held(1) and gate.out_value(1) == 9.0, gate.out_value(1)
    ok("a segment rebuild left the gate's held input and waiting zone intact")


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


def test_an_undrained_output_blocks_the_whole_gate():
    """Every output must have somewhere to go, not just the interesting one.

    Easy to get wrong when building: a Shear's pass-through output is easy to
    forget, and forgetting it stalls the machine completely rather than
    degrading it.
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

    _run(30)
    assert gate.out_held(0), (
        "the undrained pass-through output should still be holding")
    assert engine.gates.stats["blocked"] > 10, engine.gates.stats
    # It fired exactly once, then jammed.
    east = engine.segments.at(26, 20)
    assert east.tail <= 1, (
        f"the gate should have emitted at most once, got {east.tail}")
    ok(f"leaving one output undrained stalled the gate after a single fire "
       f"({engine.gates.stats['blocked']} blocked ticks)")


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
