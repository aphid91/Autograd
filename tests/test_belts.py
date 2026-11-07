"""Belt transport: the closed form against the naive oracle.

docs/belt_report.md says to use the naive per-item simulation as the test
oracle, run both on the same producer/consumer decisions, and compare
positions every tick. That is what this file does.

This is deliberately the most paranoid test in the project. The closed form
is the part most likely to be subtly wrong and least likely to *look* wrong:
an item a fraction of a cell out of place, or a clump that decompresses when
compression should be permanent, reads as perfectly plausible motion on
screen. The oracle has no opinion about plausibility -- it stores every
position and moves them one at a time, which is obviously correct and far too
slow to ship.

Both models are sampled at the same instant. An item pushed during tick `t`
is stamped `enter_tick = t`, so at time `t` its age is zero and it sits at
position 0 -- exactly where the oracle puts it. Reading the closed form at
`t+1` instead ages every item one tick further than the oracle has moved it,
which shows up as a one-step disagreement at the entrance.
"""
import random

from core import cell
from core.belts import (
    DEFAULT_SPACING, DEFAULT_SPEED, UNIT, BeltSim, naive_tick,
)
from core.entity import Sink, Source

from tests import harness
from tests.harness import board, engine, ok, orch, step


# --- a segment stand-in, so these tests need no board ----------------------
class FakeSegment:
    """The fields BeltSim touches, and nothing else.

    Transport does not care about cells or geometry -- only length, the ring,
    and the three mutable counters. Testing against a stub keeps a transport
    failure from looking like a segmentation failure.
    """

    def __init__(self, length, cap=256):
        self.sid = 1
        self.length = length
        self.head = self.tail = 0
        self.anchor = length * UNIT
        self.cap = cap
        self.enter_tick = [0] * cap
        self.payload = [0.0] * cap
        self.cells = [(0, i) for i in range(length)]
        self.src = self.dst = None

    @property
    def count(self):
        return self.tail - self.head


class FakeIndex:
    def __init__(self, segs):
        self.segments = {s.sid: s for s in segs}


def _compare(length, offers, accepts, speed=DEFAULT_SPEED,
             spacing=DEFAULT_SPACING, label=""):
    """Run both models on identical decisions, asserting they agree every tick.

    Returns (pops, pushes) as counted by the closed form.
    """
    seg = FakeSegment(length)
    sim = BeltSim(FakeIndex([seg]), speed, spacing)
    naive = []
    pops = pushes = 0

    for t, (o, a) in enumerate(zip(offers, accepts)):
        popped, pushed = sim.tick_segment(
            seg, t,
            offer=(lambda _s: 1.0) if o else None,
            accept=(lambda _s, _p: True) if a else None,
        )
        naive, n_popped, n_pushed = naive_tick(
            naive, length * UNIT, speed, spacing, o, a)

        got = sim.positions(seg, t)
        assert got == naive, (
            f"{label}: positions diverged at tick {t}\n"
            f"  L={length} v={speed} d={spacing}\n"
            f"  offers so far:  {offers[:t+1]}\n"
            f"  accepts so far: {accepts[:t+1]}\n"
            f"  closed form: {got}\n"
            f"  oracle:      {naive}")
        # The *events* must match too, not merely the resulting positions:
        # a model could land on the right arrangement having popped and
        # pushed when it should have done neither.
        assert popped == n_popped, (
            f"{label}: pop disagreed at tick {t} "
            f"(closed {popped}, oracle {n_popped})")
        assert pushed == n_pushed, (
            f"{label}: push disagreed at tick {t} "
            f"(closed {pushed}, oracle {n_pushed})")

        pops += int(popped)
        pushes += int(pushed)
        assert not sim.audit(t), sim.audit(t)

        # Render-time sanity at the same tick. The oracle has nothing to say
        # about fractional time, so these are invariants rather than a
        # comparison -- but they are what the player actually sees, and a
        # whole-tick-only check misses them entirely: an unclamped anchor let
        # a blocked clump slide off the end of the belt between ticks while
        # every whole-tick sample stayed perfect.
        length_fp = length * UNIT
        for frac in (0.25, 0.5, 0.75):
            mid = sim.positions(seg, t, frac)
            for p in mid:
                assert -1e-9 <= p <= length_fp + 1e-9, (
                    f"{label}: tick {t} alpha {frac}: position {p} outside "
                    f"0..{length_fp}")
            for i in range(1, len(mid)):
                assert mid[i - 1] - mid[i] >= spacing - 1e-9, (
                    f"{label}: tick {t} alpha {frac}: items {i-1},{i} "
                    f"overlap: {mid}")

    return pops, pushes


def test_saturated_run_matches_oracle():
    """A full stream into an accepting consumer: the steady state."""
    n = 40
    pops, pushes = _compare(8, [True] * n, [True] * n, label="saturated")
    # At d == v the belt sustains one item per tick, which is the whole point
    # of that configuration -- see core/tick.py.
    assert pushes >= n - 8, (pushes, n)
    assert pops >= n - 10, (pops, n)
    ok(f"saturated 8-cell run: {pushes} pushed, {pops} popped, oracle agreed")


def test_blocked_run_backs_up_and_matches_oracle():
    """A consumer that never takes: the belt must clump and then refuse."""
    n = 40
    _compare(8, [True] * n, [False] * n, label="blocked")

    seg = FakeSegment(8)
    sim = BeltSim(FakeIndex([seg]), DEFAULT_SPEED, DEFAULT_SPACING)
    for t in range(n):
        sim.tick_segment(seg, t, offer=lambda _s: 1.0, accept=None)
    # Filled to physical capacity and not one item beyond it.
    assert seg.count == sim.capacity_of(seg), (
        seg.count, sim.capacity_of(seg))
    pos = sim.positions(seg, n)
    # Nose to tail, from the end backwards.
    assert pos[0] == 8 * UNIT, pos[0]
    for i in range(1, len(pos)):
        assert pos[i - 1] - pos[i] == DEFAULT_SPACING, (i, pos)
    ok(f"blocked run packed {seg.count} items nose-to-tail at capacity")


def test_stall_then_drain_keeps_compression():
    """Compression is permanent: a clump does not decompress when it moves.

    Both terms of the min grow at rate v, so once an item is packed it stays
    packed. That is a property of the formula rather than something tracked,
    and it is exactly the kind of thing that would look fine on screen while
    being wrong.
    """
    offers = [True] * 40
    accepts = [False] * 12 + [True] * 28
    _compare(10, offers, accepts, label="stall-then-drain")

    seg = FakeSegment(10)
    sim = BeltSim(FakeIndex([seg]), DEFAULT_SPEED, DEFAULT_SPACING)
    for t in range(12):
        sim.tick_segment(seg, t, offer=lambda _s: 1.0, accept=None)
    packed = sim.positions(seg, 12)
    gaps_before = [packed[i - 1] - packed[i] for i in range(1, len(packed))]

    # Now let it drain a few ticks and re-measure the gaps between the items
    # that are still in the clump.
    for t in range(12, 16):
        sim.tick_segment(seg, t, offer=None,
                         accept=lambda _s, _p: True)
    moving = sim.positions(seg, 16)
    gaps_after = [moving[i - 1] - moving[i] for i in range(1, len(moving))]
    assert all(g == DEFAULT_SPACING for g in gaps_after), gaps_after
    assert gaps_before[:len(gaps_after)] == gaps_after, (
        gaps_before, gaps_after)
    ok("a clump stayed compressed as it drained")


def test_intermittent_producer_matches_oracle():
    """Gappy input exercises the *unblocked* term rather than the packed one."""
    for period in (2, 3, 5, 7):
        offers = [(i % period == 0) for i in range(60)]
        _compare(9, offers, [True] * 60, label=f"every {period} ticks")
    ok("intermittent producers at periods 2,3,5,7 matched the oracle")


def test_speed_and_spacing_variations_match_oracle():
    """Both v < d and v > d, which the report explicitly covers.

    The game runs at v == d, but the formula is supposed to hold either side
    of that and the fixed point exists so those become reachable. Pinning
    them now means the constant can change later without re-deriving
    anything.
    """
    n = 50
    cases = [
        (UNIT, UNIT, "v == d"),
        (UNIT, 2 * UNIT, "d = 2v (throttled)"),
        (UNIT, 3 * UNIT, "d = 3v"),
        (2 * UNIT, UNIT, "v = 2d (fast)"),
        (3 * UNIT, UNIT, "v = 3d"),
        (UNIT // 2, UNIT, "half-speed belt"),
        (UNIT // 4, UNIT // 2, "quarter speed, half spacing"),
    ]
    for speed, spacing, label in cases:
        _compare(10, [True] * n, [True] * n, speed, spacing, label=label)
        # ...and with a stall in the middle, which is where the two terms of
        # the min actually trade places.
        _compare(10, [True] * n, [False] * 15 + [True] * (n - 15),
                 speed, spacing, label=label + " + stall")
    ok(f"{len(cases)} speed/spacing combinations matched, with and without a stall")


def test_short_segments_match_oracle():
    """A one-cell run, where the entrance is also the exit."""
    for length in (1, 2, 3):
        n = 30
        _compare(length, [True] * n, [True] * n, label=f"{length}-cell")
        _compare(length, [True] * n, [False] * n, label=f"{length}-cell blocked")
        offers = [(i % 3 == 0) for i in range(n)]
        _compare(length, offers, [True] * n, label=f"{length}-cell gappy")
    ok("1-, 2- and 3-cell runs matched the oracle")


def test_spacing_wider_than_the_belt():
    """`d > L` must not drive the anchor negative.

    The report's pop sets `A = L - d` and states `L >= d` as a requirement.
    A short run can violate it: a 1-cell belt with 3-cell spacing would put
    the anchor at -2 cells, and the next item to enter takes that as its
    packed position -- rendering *behind* the start of the belt. Found by the
    randomised sweep, which is the only reason this configuration was tried.
    """
    for length in (1, 2):
        for mult in (2, 3, 5):
            n = 30
            offers = [(i % 2 == 0) for i in range(n)]
            _compare(length, offers, [True] * n, UNIT, mult * UNIT,
                     label=f"L={length} d={mult}v")

            seg = FakeSegment(length)
            sim = BeltSim(FakeIndex([seg]), UNIT, mult * UNIT)
            for t in range(n):
                sim.tick_segment(seg, t, offer=lambda _s: 1.0,
                                 accept=lambda _s, _p: True)
                assert seg.anchor >= 0, (length, mult, t, seg.anchor)
                for frac in (0.0, 0.5):
                    for p in sim.positions(seg, t, frac):
                        assert p >= -1e-9, (length, mult, t, frac, p)
    ok("spacing up to 5x the belt length keeps every position non-negative")


def test_blocked_clump_does_not_drift_at_fractional_time():
    """A backed-up belt must be stationary between ticks, not creeping.

    The anchor is clamped at the end on a whole tick (`min(A + v, L)`), and
    the fractional path has to apply the same clamp. Without it the whole
    clump slides forward as alpha rises and snaps back when the tick lands --
    on screen, items visibly creeping into the gate and resetting. The head
    would also drift clean off the end of the belt.
    """
    seg = FakeSegment(5)
    sim = BeltSim(FakeIndex([seg]), DEFAULT_SPEED, DEFAULT_SPACING)
    length = sim.length_of(seg)
    for t in range(12):
        sim.tick_segment(seg, t, offer=lambda _s: 1.0, accept=None)
    assert seg.count == sim.capacity_of(seg), seg.count

    at_zero = sim.positions(seg, 12, 0.0)
    for frac in (0.0, 0.1, 0.25, 0.5, 0.75, 0.9, 1.0):
        pos = sim.positions(seg, 12, frac)
        assert pos == at_zero, (
            f"a fully blocked clump moved at alpha={frac}:\n"
            f"  alpha=0   {at_zero}\n  alpha={frac} {pos}")
        for p in pos:
            assert p <= length + 1e-9, (frac, p, length)
    ok("a fully blocked clump is identical at every alpha in 0..1")


def test_partially_blocked_belt_moves_only_its_free_items():
    """The clump holds still while items behind it are still closing up.

    This is the case that would hide the clamp bug: the tail is genuinely
    moving, so motion looks right unless you check the head specifically.
    """
    seg = FakeSegment(10)
    sim = BeltSim(FakeIndex([seg]), DEFAULT_SPEED, DEFAULT_SPACING)
    # A few items, consumer refusing, stopped before it fills.
    for t in range(4):
        sim.tick_segment(seg, t, offer=lambda _s: 1.0, accept=None)
    for t in range(4, 12):
        sim.tick_segment(seg, t, offer=None, accept=None)

    length = sim.length_of(seg)
    head_at = [sim.position_of(seg, seg.head, 12, f)
               for f in (0.0, 0.5, 1.0)]
    assert head_at[0] == length, head_at
    assert head_at == [length] * 3, (
        f"the head drifted past the end: {head_at}")
    for frac in (0.0, 0.25, 0.5, 0.75):
        pos = sim.positions(seg, 12, frac)
        for p in pos:
            assert p <= length + 1e-9, (frac, p)
        for i in range(1, len(pos)):
            assert pos[i - 1] - pos[i] >= DEFAULT_SPACING - 1e-9, (frac, pos)
    ok("a parked head stays parked while the rest of the belt closes up")


def test_randomised_comparison_against_oracle():
    """The report's recipe: random configs, random decisions, compare always.

    5,000 configurations is what the report says it verified against. This
    runs a smaller but still substantial sweep on every run, with a fixed
    seed so a failure is reproducible.
    """
    rng = random.Random(20260311)
    configs = 600
    total_ticks = 0
    short_and_wide = 0

    for i in range(configs):
        # Short runs are over-represented on purpose: `d > L` is where the
        # report's `L >= d` precondition bites, and it drove the anchor
        # negative until that was clamped. A uniform length distribution
        # reaches it only by luck.
        length = rng.randrange(1, 4) if rng.random() < 0.3 \
            else rng.randrange(1, 14)
        # Mostly v == d, but regularly stray either side of it.
        if rng.random() < 0.4:
            speed = spacing = UNIT
        else:
            speed = rng.choice([UNIT // 4, UNIT // 2, UNIT, 2 * UNIT, 3 * UNIT])
            spacing = rng.choice(
                [UNIT // 2, UNIT, 2 * UNIT, 3 * UNIT, 5 * UNIT])
        if spacing > length * UNIT:
            short_and_wide += 1
        n = rng.randrange(20, 60)
        # Bias the rates so some runs saturate, some starve, some stall hard.
        p_offer = rng.random()
        p_accept = rng.random()
        offers = [rng.random() < p_offer for _ in range(n)]
        accepts = [rng.random() < p_accept for _ in range(n)]

        _compare(length, offers, accepts, speed, spacing,
                 label=f"random config {i} (seed 20260311)")
        total_ticks += n

    assert short_and_wide > 20, (
        f"only {short_and_wide} configs had spacing wider than the belt -- "
        f"the d > L case is barely being exercised")
    ok(f"{configs} random configurations ({short_and_wide} with d > L), "
       f"{total_ticks} ticks, closed form == oracle at every step")


def test_ring_wraparound_matches_oracle():
    """Counters must survive wrapping the ring many times over.

    head and tail are monotonic and index the ring by `k % cap`, so a long
    saturated run exercises the modulo repeatedly. A ring sized exactly to
    the run makes it wrap as fast as possible.
    """
    length = 4
    cap = 8                      # power of two, as the closed form requires
    seg = FakeSegment(length, cap=cap)
    sim = BeltSim(FakeIndex([seg]), DEFAULT_SPEED, DEFAULT_SPACING)
    naive = []
    n = 500
    for t in range(n):
        sim.tick_segment(seg, t, offer=lambda _s: 1.0,
                         accept=lambda _s, _p: True)
        naive, _p, _q = naive_tick(
            naive, length * UNIT, DEFAULT_SPEED, DEFAULT_SPACING, True, True)
        assert sim.positions(seg, t) == naive, (
            f"diverged at tick {t} after {seg.tail} pushes")
    assert seg.tail > cap * 10, (
        f"only {seg.tail} pushes -- the ring barely wrapped")
    ok(f"{seg.tail} items through a {cap}-slot ring ({seg.tail // cap}x "
       f"wraparound), oracle agreed throughout")


def test_fractional_alpha_is_exact_and_monotonic():
    """Interpolation is exact, not approximate -- both terms are linear in t.

    Evaluating at tick+alpha must land exactly between the two whole-tick
    positions, and must never move an item backwards.
    """
    seg = FakeSegment(10)
    sim = BeltSim(FakeIndex([seg]), DEFAULT_SPEED, DEFAULT_SPACING)
    for t in range(6):
        sim.tick_segment(seg, t, offer=lambda _s: 1.0,
                         accept=lambda _s, _p: True)

    k = seg.head
    at_t = sim.position_of(seg, k, 6, 0.0)
    at_next = sim.position_of(seg, k, 7, 0.0)
    for frac in (0.0, 0.25, 0.5, 0.75):
        got = sim.position_of(seg, k, 6, frac)
        want = at_t + (at_next - at_t) * frac
        assert abs(got - want) < 1e-6, (frac, got, want)
    # alpha -> 1 approaches the next whole tick from below.
    assert abs(sim.position_of(seg, k, 6, 1.0) - at_next) < 1e-6

    # Monotonic across a tick boundary, for every live item.
    prev = {k: -1.0 for k in range(seg.head, seg.tail)}
    for t in (6, 7, 8):
        for frac in (0.0, 0.2, 0.4, 0.6, 0.8):
            for k in range(seg.head, seg.tail):
                p = sim.position_of(seg, k, t, frac)
                assert p >= prev[k] - 1e-9, (k, t, frac, p, prev[k])
                prev[k] = p
    ok("fractional positions are exact midpoints and never go backwards")


def test_items_never_overlap_or_leave_the_belt():
    """The invariants a player would notice, under adversarial input."""
    rng = random.Random(77)
    for trial in range(60):
        length = rng.randrange(1, 12)
        seg = FakeSegment(length)
        sim = BeltSim(FakeIndex([seg]), DEFAULT_SPEED, DEFAULT_SPACING)
        for t in range(80):
            sim.tick_segment(
                seg, t,
                offer=(lambda _s: 1.0) if rng.random() < 0.7 else None,
                accept=(lambda _s, _p: True) if rng.random() < 0.4 else None,
            )
            # Check at a few fractional times too: a rendering-time overlap
            # is just as visible as a tick-time one.
            for frac in (0.0, 0.5):
                pos = sim.positions(seg, t, frac)
                for p in pos:
                    assert -1e-9 <= p <= length * UNIT + 1e-9, (
                        trial, t, frac, p, length * UNIT)
                for i in range(1, len(pos)):
                    assert pos[i - 1] - pos[i] >= DEFAULT_SPACING - 1e-9, (
                        trial, t, frac, pos)
            assert seg.count <= sim.capacity_of(seg), (trial, t, seg.count)
    ok("60 adversarial trials: no overlaps, nothing off the belt, never over capacity")


def test_payloads_ride_with_their_items():
    """An item's value must arrive with the item, not with its slot."""
    seg = FakeSegment(6)
    sim = BeltSim(FakeIndex([seg]), DEFAULT_SPEED, DEFAULT_SPACING)
    sent = []
    got = []
    nxt = [100.0]

    def offer(_s):
        v = nxt[0]
        nxt[0] += 1.0
        sent.append(v)
        return v

    def accept(_s, payload):
        got.append(payload)
        return True

    for t in range(40):
        sim.tick_segment(seg, t, offer=offer, accept=accept)

    assert got, "nothing arrived"
    # FIFO: a belt never reorders, so what comes out is a prefix of what
    # went in, in order.
    assert got == sent[:len(got)], (sent, got)
    ok(f"{len(got)} payloads arrived in order ({got[0]:.0f}..{got[-1]:.0f})")


# --- integration with the real segment index -------------------------------
def test_real_segments_tick():
    """The sim drives genuine board-derived segments, not just stubs."""
    engine.place_belt_path((5, 5), (14, 5))
    engine.sync_segments()
    index = engine.segments
    assert index.count == 1

    sim = BeltSim(index)
    seg = next(iter(index.segments.values()))
    for t in range(30):
        sim.tick_segment(seg, t, offer=lambda _s: 1.0,
                         accept=lambda _s, _p: True)
    assert seg.count > 0, "nothing on the belt"
    assert not sim.audit(30), sim.audit(30)
    assert not index.audit(), index.audit()
    ok(f"a real 10-cell segment carried {seg.tail} items, both audits clean")


def test_underground_capacity_is_usable():
    """Buried positions genuinely hold items, not just inflate a number.

    An underground pair adds `span + 1` positions to a run. Those have no
    cell to draw in, but transport must treat them as ordinary capacity --
    which is the entire reason length is tracked apart from the cell list.
    """
    engine.place_belt_path((5, 5), (8, 5))
    engine.place_under(9, 5, cell.DIR_PX, is_exit=False)
    engine.place_under(13, 5, cell.DIR_PX, is_exit=True)
    engine.place_belt_path((14, 5), (17, 5))
    engine.sync_segments()
    index = engine.segments
    assert index.count == 1, [str(s) for s in index.segments.values()]

    seg = next(iter(index.segments.values()))
    assert seg.buried == 3, seg.buried
    sim = BeltSim(index)

    # Fill it with the consumer refusing, so it packs to capacity.
    for t in range(60):
        sim.tick_segment(seg, t, offer=lambda _s: 1.0, accept=None)
    assert seg.count == sim.capacity_of(seg), (
        seg.count, sim.capacity_of(seg))
    # Capacity must account for the buried stretch: a 13-position run holds
    # more than the 10 cells you can see.
    assert seg.count > len(seg.cells), (
        f"{seg.count} items on {len(seg.cells)} visible cells -- the buried "
        f"positions are not being used")
    assert not sim.audit(60), sim.audit(60)
    ok(f"{seg.count} items on a {seg.length}-position run "
       f"({len(seg.cells)} cells + {seg.buried} buried)")


def test_sim_runs_on_the_belt_phase():
    """The engine's belt phase actually moves items, driven by the clock."""
    engine.place_belt_path((5, 5), (14, 5))
    engine.sync_segments()
    engine.debug_feed = -1.0
    try:
        engine.clock.set_rate(60.0)      # one tick per frame
        step(12)
        seg = next(iter(engine.segments.segments.values()))
        assert seg.count > 0, "the belt phase ran but nothing moved"
        assert engine.belts.stats["pushed"] > 0, engine.belts.stats
        assert not engine.belts.audit(engine.clock.tick), \
            engine.belts.audit(engine.clock.tick)
        ok(f"12 frames of the real belt phase carried {seg.count} items")
    finally:
        # The engine is shared across scenarios; a live feed would leak.
        engine.debug_feed = None


def test_debug_feed_defers_to_real_gates():
    """On a run with real gates at both ends, the feed changes nothing.

    The feed exists to stand in for a Source/Sink pair on a *dangling* run.
    Where real gates are wired up they must win, or the feed would conjure
    items into the middle of a chain and make the gate layer unreadable.

    Asserted by running the same chain with and without the feed and
    comparing: it is the *difference* that matters, not the item count --
    a Source legitimately fills the belt either way.
    """
    def run(feed):
        harness.reset()
        engine.place_structure(Source, 7, 5)
        engine.place_belt_path((9, 5), (14, 5))
        engine.place_structure(Sink, 15, 5)
        engine.sync_segments()
        seg = next(iter(engine.segments.segments.values()))
        assert seg.src is not None and seg.dst is not None
        engine.debug_feed = feed
        try:
            engine.clock.set_rate(60.0)
            step(20)
            return (seg.count, seg.head, seg.tail,
                    engine.gates.stats["consumed"])
        finally:
            engine.debug_feed = None

    without = run(None)
    with_feed = run(-1.0)
    assert without == with_feed, (
        f"the debug feed altered a fully-wired chain:\n"
        f"  without: count/head/tail/consumed = {without}\n"
        f"  with:    count/head/tail/consumed = {with_feed}")
    assert without[3] > 0, "the chain should have moved items at all"
    ok(f"a Source->belt->Sink chain behaves identically with and without "
       f"the feed ({without[3]} consumed)")


def test_items_dropped_when_the_run_is_edited():
    """Editing a run clears its items -- the documented O(1) price."""
    engine.place_belt_path((5, 5), (15, 5))
    engine.sync_segments()
    engine.debug_feed = -1.0
    try:
        engine.clock.set_rate(60.0)
        step(8)
        assert engine.segments.total_items() > 0, "nothing to drop"

        engine.erase(10, 5)
        engine.sync_segments()
        assert engine.segments.total_items() == 0, (
            "items survived a topology edit")
        assert engine.segments.count == 2, engine.segments.count
        assert not engine.belts.audit(engine.clock.tick)
        ok("erasing mid-run split the segment and dropped its items")
    finally:
        engine.debug_feed = None


def test_clear_items_keeps_the_segmentation():
    """clear_items() empties the belts without re-deriving the runs."""
    engine.place_belt_path((5, 5), (15, 5))
    engine.place_belt_path((5, 8), (15, 8))
    engine.sync_segments()
    engine.debug_feed = -1.0
    try:
        engine.clock.set_rate(60.0)
        step(8)
        assert engine.segments.total_items() > 0
        shapes = {(s.cells[0], s.cells[-1]) for s
                  in engine.segments.segments.values()}
        rebuilds = engine.segments.stats["rebuilds"]

        engine.clear_items()
        assert engine.segments.total_items() == 0
        assert engine.segments.stats["rebuilds"] == rebuilds, \
            "clear_items should not rebuild"
        assert {(s.cells[0], s.cells[-1]) for s
                in engine.segments.segments.values()} == shapes
        # Anchors must be back to the initial state, or the next item to
        # enter would take a stale packed position.
        for s in engine.segments.segments.values():
            assert s.anchor == engine.belts.length_of(s), (s.sid, s.anchor)
        assert not engine.belts.audit(engine.clock.tick)
        ok("clear_items emptied both runs and left the segmentation intact")
    finally:
        engine.debug_feed = None


def test_many_segments_tick_independently():
    """Segments do not interfere: the only shared state is at the nodes.

    That independence is what makes this one compute dispatch later, so it is
    worth pinning rather than assuming.
    """
    for y in range(5, 15):
        engine.place_belt_path((5, y), (12, y))
    engine.sync_segments()
    index = engine.segments
    assert index.count == 10, index.count

    sim = BeltSim(index)
    # Feed only the even-numbered rows, and check the odd ones stay empty.
    fed = {s.sid for s in index.segments.values() if s.cells[0][1] % 2 == 0}

    for t in range(25):
        for seg in index.segments.values():
            if seg.sid in fed:
                sim.tick_segment(seg, t, offer=lambda _s: 1.0, accept=None)
            else:
                sim.tick_segment(seg, t)

    for seg in index.segments.values():
        if seg.sid in fed:
            assert seg.count > 0, f"segment {seg.sid} should have items"
        else:
            assert seg.count == 0, (
                f"segment {seg.sid} got items from a neighbour")
    assert not sim.audit(25), sim.audit(25)
    ok(f"{len(fed)} fed segments filled, {10 - len(fed)} unfed stayed empty")
