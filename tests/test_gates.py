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
    # x > 0: swap. The control reads as "swap when true", so a positive x is
    # the exchanging case and zero-or-negative is the pass-through.
    swap.set_input(0, 1.0)
    swap.set_input(1, 7.0)
    swap.set_input(2, 9.0)
    _run(1)
    assert [swap.out_value(i) for i in range(3)] == [1.0, 9.0, 7.0], (
        [swap.out_value(i) for i in range(3)])
    ok("Swap with x>0 exchanged y and z")

    # x <= 0: no swap. Drain the waiting zones first, or it cannot fire.
    for i in range(3):
        swap.clear_output(i)
    swap.set_input(0, -1.0)
    swap.set_input(1, 7.0)
    swap.set_input(2, 9.0)
    _run(1)
    assert [swap.out_value(i) for i in range(3)] == [-1.0, 7.0, 9.0], (
        [swap.out_value(i) for i in range(3)])
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
