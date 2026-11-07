"""Belt transport: the closed-form item simulation, and its oracle.

Implements docs/belt_report.md. Every item's position on a segment is an
expression, not a stored value:

    pos(k, t) = min( (t - enter_tick[k]) * v,  anchor - (k - head) * d )

The first term is where the item would be if nothing had ever blocked it; the
second is where it would be if packed tight behind the segment's head. Items
in a clump take the packed value, free items take the unblocked one, and the
`min` decides which without anything tracking where the clump boundary is.
Both grow at rate `v`, so once an item is packed it stays packed -- Factorio's
rule that compression is permanent, for free.

Nothing per-item is written after the item enters. `enter_tick` is stamped
once; `head`, `tail` and `anchor` are the only mutable state, and all three
are per-segment. That is what makes a whole run tick in O(1) however many
items are on it.


Fixed point
-----------

`L`, `v` and `d` are integers in UNIT-ths of a cell. The report is emphatic
about this and it is worth restating: the pop test is `pos == L` exactly, and
float drift would eventually miss it, stalling a belt permanently with an
item sitting a hair short of the end.

At the default `v == d == UNIT` (one cell per tick, items nose to tail)
positions are whole cells and the arithmetic is trivially exact. UNIT exists
so slower belts and tighter packing become a constant change rather than a
rewrite -- see "Bubble-free output needs d == v" in core/tick.py.


What a producer and consumer are
--------------------------------

`BeltSim.tick()` takes two callbacks per segment, exactly as the report's
`producerOffers` / `consumerAccepts`:

    offer(seg)  -> payload to push, or None
    accept(seg, payload) -> True if it was taken

Belt transport knows nothing about gates. Wiring these to waiting zones and
input ports is the gate layer's job (step 5); until then the sim is driven
by test harnesses and by the debug injector in the UI, which is enough to
prove the transport math on its own.


The oracle
----------

`naive_tick()` is a direct transcription of the report's reference
implementation: it stores every item's position and moves them one at a time,
which is obviously correct and obviously too slow. `tests/test_belts.py`
runs both against the same random producer/consumer decisions and compares
positions every tick.

This is the single most valuable test in the project right now. The closed
form is the part most likely to be subtly wrong and least likely to *look*
wrong on screen -- an item a fraction of a cell out of place, or a clump that
decompresses when it should not, reads as plausible motion. The oracle does
not care what looks plausible.
"""

#: Fixed-point scale: positions, speeds and spacings are in UNIT-ths of a
#: cell. See "Fixed point" above -- the pop test needs exact arithmetic.
UNIT = 256

#: Default belt speed, in UNIT-ths of a cell per tick. One cell per tick.
DEFAULT_SPEED = UNIT

#: Default minimum spacing between item centres, in UNIT-ths of a cell. Equal
#: to the speed, which is what allows a gate to sustain one item per tick --
#: see core/tick.py. Raising it throttles the belt and spaces items out.
DEFAULT_SPACING = UNIT


def naive_tick(pos, length, speed, spacing, offer, accept):
    """The reference implementation from docs/belt_report.md. The oracle.

    `pos` is every item's position, consumer end first, in fixed point.
    Returns `(new_positions, popped, pushed)`.

    Deliberately the dumbest correct thing: advance each item as far as it can
    go without crowding the one ahead, then pop and push. O(items) per tick
    and not used in the game -- it exists to disagree with the closed form.
    """
    new = []
    for i, p in enumerate(pos):
        # The head may reach the very end; everyone else stops `spacing`
        # short of whoever is ahead of them.
        limit = length if i == 0 else new[-1] - spacing
        new.append(min(p + speed, limit))

    popped = bool(new) and new[0] == length and accept
    if popped:
        new.pop(0)

    pushed = offer and (not new or new[-1] >= spacing)
    if pushed:
        new.append(0)

    return new, popped, pushed


class BeltSim:
    """Ticks every segment's items, and answers where they are for rendering.

    Holds no state of its own: everything lives on the `Segment` objects in
    the index, so a rebuild wipes the simulation with the segmentation and
    there is no second copy to go stale.
    """

    def __init__(self, index, speed=DEFAULT_SPEED, spacing=DEFAULT_SPACING):
        self.index = index
        #: Distance per tick and minimum item separation, fixed point. Uniform
        #: across every belt for now; the report requires uniformity *within*
        #: a segment, so per-tier speeds would cut segments at speed changes.
        self.speed = int(speed)
        self.spacing = int(spacing)
        if self.spacing < 1 or self.speed < 1:
            raise ValueError("speed and spacing must be positive")
        #: Cumulative counters for the inspection panel.
        self.stats = {"pushed": 0, "popped": 0, "ticks": 0}

    # --- geometry --------------------------------------------------------
    def length_of(self, seg):
        """A segment's length in fixed point. Items are taken at exactly this."""
        return seg.length * UNIT

    def capacity_of(self, seg):
        """How many items fit nose to tail. `L/d + 1`, per the report."""
        return self.length_of(seg) // self.spacing + 1

    # --- the closed form -------------------------------------------------
    def position_of(self, seg, k, tick, alpha=0.0):
        """Where item `k` is at `tick + alpha`, in fixed point.

        The whole simulation, in two lines. `alpha` is the fraction into the
        next tick, which makes this exact at render time rather than merely
        interpolated: both terms are linear in time, so evaluating them at a
        fractional tick is the same expression. Clumps therefore form smoothly
        with no snapping, and an item waiting at the end simply sits there.
        """
        length = self.length_of(seg)
        slot = k % seg.cap
        age = (tick - int(seg.enter_tick[slot])) + alpha
        # Cap the age so a long-settled item cannot overflow the product. Any
        # position past the end is clamped by the min below anyway.
        max_age = length / self.speed + 1.0
        age = min(age, max_age)
        unblocked = age * self.speed

        # The anchor advances with alpha exactly as it does on a whole tick --
        # *including* the clamp at the end. tick_segment() does
        # `min(anchor + v, L)`; dropping that clamp here lets a blocked belt's
        # whole clump slide forward as alpha rises and then snap back when the
        # tick lands, with the head drifting clean off the end of the belt.
        # This is the report's `A_render = min(A + alpha*v, L)`.
        anchor = min(seg.anchor + alpha * self.speed, length)
        packed = anchor - (k - seg.head) * self.spacing
        return min(unblocked, packed)

    def positions(self, seg, tick, alpha=0.0):
        """Every live item's position on a segment, head (consumer end) first."""
        return [self.position_of(seg, k, tick, alpha)
                for k in range(seg.head, seg.tail)]

    def payload_of(self, seg, k):
        return float(seg.payload[k % seg.cap])

    # --- the tick --------------------------------------------------------
    def tick_segment(self, seg, tick, offer=None, accept=None):
        """Advance one segment by one tick. O(1), no loops over items.

        Order is load-bearing and comes straight from the report: advance the
        anchor, then pop, then push. Returns `(popped, pushed)`.
        """
        length = self.length_of(seg)

        # 1. The packed front advances until it reaches the end. Every item
        #    taking the packed value moves with it, which is how a whole clump
        #    advances without being touched individually.
        seg.anchor = min(seg.anchor + self.speed, length)

        # 2. Consume. The head must be *exactly* at the end -- this is the
        #    test that needs fixed point.
        popped = False
        if seg.head != seg.tail:
            if self.position_of(seg, seg.head, tick) == length:
                payload = self.payload_of(seg, seg.head)
                if accept is not None and accept(seg, payload):
                    seg.head += 1
                    # The new head's packed slot is one spacing back, which
                    # leaves every remaining item's packed position unchanged
                    # -- so nothing jumps. See "Why the pop is correct".
                    #
                    # Clamped at 0 because the report's bare `L - d` assumes
                    # `L >= d`, a requirement it states but which a short
                    # segment can violate: a 1-cell run with spacing 3 would
                    # put the anchor at -2 cells, and the next item to enter
                    # would take that as its packed position and render
                    # *behind* the belt's start. At or below zero the packed
                    # term is never the binding one anyway -- an item at the
                    # entrance is already at 0 -- so clamping changes nothing
                    # where the report's precondition does hold.
                    seg.anchor = max(0, length - self.spacing)
                    popped = True

        # 3. Produce. There is room once the last item has moved a full
        #    spacing clear of the entrance.
        pushed = False
        if offer is not None:
            room = (seg.head == seg.tail
                    or self.position_of(seg, seg.tail - 1, tick) >= self.spacing)
            if room:
                payload = offer(seg)
                if payload is not None:
                    slot = seg.tail % seg.cap
                    seg.enter_tick[slot] = tick
                    seg.payload[slot] = payload
                    seg.tail += 1
                    pushed = True

        if popped:
            self.stats["popped"] += 1
        if pushed:
            self.stats["pushed"] += 1
        return popped, pushed

    def tick(self, tick, offer=None, accept=None):
        """Advance every segment by one tick.

        `offer` / `accept` are called per segment, so one pair of callbacks
        can serve the whole board. Returns `(popped, pushed)` totals.

        Segments are independent: the report's only shared state is at the
        *nodes*, and nothing here touches a node. That independence is what
        makes this one compute dispatch later -- the loop below becomes
        `gl_GlobalInvocationID.x` and nothing else changes.
        """
        popped = pushed = 0
        for seg in self.index.segments.values():
            p, q = self.tick_segment(seg, tick, offer, accept)
            popped += int(p)
            pushed += int(q)
        self.stats["ticks"] += 1
        return popped, pushed

    # --- inspection ------------------------------------------------------
    def audit(self, tick):
        """Check the simulation's invariants. Returns a list of problems.

        Separate from SegmentIndex.audit(), which checks the *segmentation*
        against the grid. This checks the items on it.
        """
        problems = []
        for seg in self.index.segments.values():
            length = self.length_of(seg)
            pos = self.positions(seg, tick)

            if seg.count > seg.cap:
                problems.append(
                    f"segment {seg.sid}: {seg.count} items in a ring of "
                    f"{seg.cap}")
            if seg.count > self.capacity_of(seg):
                problems.append(
                    f"segment {seg.sid}: {seg.count} items exceeds physical "
                    f"capacity {self.capacity_of(seg)}")

            for i, p in enumerate(pos):
                if p < 0 or p > length:
                    problems.append(
                        f"segment {seg.sid} item {i}: position {p} outside "
                        f"0..{length}")
            # Items must stay in order and never closer than the spacing.
            for i in range(1, len(pos)):
                gap = pos[i - 1] - pos[i]
                if gap < self.spacing - 1e-9:
                    problems.append(
                        f"segment {seg.sid} items {i-1},{i}: gap {gap} < "
                        f"spacing {self.spacing}")
            if seg.anchor > length:
                problems.append(
                    f"segment {seg.sid}: anchor {seg.anchor} past the end "
                    f"{length}")
            if seg.anchor < 0:
                problems.append(
                    f"segment {seg.sid}: anchor {seg.anchor} is negative")
            # The anchor must be on the fixed-point scale, not the cell
            # scale. A units mix-up here is invisible to the simulation --
            # the unblocked term of the min is the smaller one until a belt
            # actually backs up, so a wrong anchor sits there for thousands
            # of ticks doing nothing -- and then places every item wrongly
            # the moment a clump forms or the renderer reads it. That
            # happened: Segment.__init__ initialised it in cells.
            #
            # Only checkable on a segment nothing has touched yet: after a
            # pop the anchor legitimately sits at max(0, L - spacing), which
            # on a short belt can be 0 even though the run is empty.
            if (seg.head == 0 and seg.tail == 0
                    and seg.anchor != length):
                problems.append(
                    f"segment {seg.sid}: untouched but anchor is "
                    f"{seg.anchor}, expected the far end {length} "
                    f"(a units mix-up? length is in cells, anchor in "
                    f"UNIT-ths of a cell)")
        return problems

    def total_items(self):
        return sum(s.count for s in self.index.segments.values())


def _self_check():
    """The closed form against the oracle, on a handful of fixed scenarios.

    The randomised comparison lives in tests/test_belts.py; this is the cheap
    import-time version that catches a gross breakage immediately.
    """
    class _Seg:
        """Minimal stand-in: the sim only touches these fields."""
        # No __slots__: `count` is a property on the real Segment, and
        # declaring it in __slots__ here would clash with the descriptor.

        def __init__(self, length, cap=64):
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

    class _Index:
        def __init__(self, segs):
            self.segments = {s.sid: s for s in segs}

    def run(length, offers, accepts, speed=DEFAULT_SPEED,
            spacing=DEFAULT_SPACING):
        """Run both models on the same decisions; return their position lists.

        Both are sampled at the *same* instant: tick `t` has just finished, so
        the closed form is evaluated at `t` -- not `t+1`. An item pushed
        during tick `t` is stamped `enter_tick = t`, so at time `t` its age is
        zero and it sits at position 0, which is exactly where the oracle puts
        it. Reading at `t+1` ages every item a tick further than the oracle
        has moved it and makes the two disagree by one step at the entrance.
        """
        seg = _Seg(length)
        sim = BeltSim(_Index([seg]), speed, spacing)
        naive = []
        closed_log, naive_log = [], []
        for t, (o, a) in enumerate(zip(offers, accepts)):
            sim.tick_segment(
                seg, t,
                offer=(lambda _s: 1.0) if o else None,
                accept=(lambda _s, _p: True) if a else None,
            )
            naive, _p, _q = naive_tick(
                naive, length * UNIT, speed, spacing, o, a)
            closed_log.append(sim.positions(seg, t))
            naive_log.append(list(naive))
        return closed_log, naive_log

    # A steady stream into an accepting consumer: the saturated case.
    c, n = run(5, [True] * 20, [True] * 20)
    assert c == n, f"saturated stream diverged:\n  closed {c}\n  naive  {n}"

    # A stream into a consumer that never takes: the belt must back up and
    # then refuse further pushes.
    c, n = run(5, [True] * 20, [False] * 20)
    assert c == n, f"blocked belt diverged:\n  closed {c}\n  naive  {n}"
    # ...and it must have filled to physical capacity, not beyond.
    seg = _Seg(5)
    sim = BeltSim(_Index([seg]), DEFAULT_SPEED, DEFAULT_SPACING)
    for t in range(40):
        sim.tick_segment(seg, t, offer=lambda _s: 1.0, accept=None)
    assert seg.count == sim.capacity_of(seg), (
        seg.count, sim.capacity_of(seg))
    assert not sim.audit(40), sim.audit(40)

    # A stall that then clears: the clump must stay compressed, which is the
    # property the `min` gives for free.
    c, n = run(6, [True] * 24, [False] * 8 + [True] * 16)
    assert c == n, f"stall-then-drain diverged:\n  closed {c}\n  naive  {n}"

    # An intermittent producer, so items are *not* all packed -- this is the
    # case that exercises the unblocked term rather than the packed one.
    offers = [True, False, False, True, False, True] * 4
    c, n = run(8, offers, [True] * 24)
    assert c == n, f"intermittent producer diverged:\n  closed {c}\n  naive  {n}"

    # Spacing wider than the speed: the belt throttles itself.
    c, n = run(6, [True] * 20, [True] * 20, spacing=2 * UNIT)
    assert c == n, f"d > v diverged:\n  closed {c}\n  naive  {n}"

    # Speed wider than the spacing, which the report explicitly covers.
    c, n = run(6, [True] * 20, [True] * 20, speed=2 * UNIT, spacing=UNIT)
    assert c == n, f"v > d diverged:\n  closed {c}\n  naive  {n}"

    # A one-cell segment: the degenerate case where the entrance *is* the exit.
    c, n = run(1, [True] * 10, [True] * 10)
    assert c == n, f"1-cell segment diverged:\n  closed {c}\n  naive  {n}"


_self_check()
