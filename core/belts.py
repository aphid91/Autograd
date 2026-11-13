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

from core.tick_profile import Profiler

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
        #: Cumulative counters for the inspection panel. `asleep` is a
        #: gauge rather than a total -- how many segments the last tick
        #: skipped.
        self.stats = {"pushed": 0, "popped": 0, "ticks": 0, "asleep": 0}
        #: Segment ids currently at rest. See the "sleeping" block below.
        #: Empty means "everything ticks", which is the pre-sleep behaviour
        #: and so the safe default after any wholesale change.
        self._asleep = set()
        #: False disables sleeping outright. Only
        #: `tools/check_wakelist_safe.py` sets it -- see set_sleep_enabled().
        self._sleep_enabled = True
        #: Per-tick timing, disarmed. `Engine` replaces it with the clock's,
        #: for the same reason `GateSim.profiler` is assigned rather than
        #: injected: diagnostics, not a dependency.
        self.profiler = Profiler()

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

    # --- entry ------------------------------------------------------------
    def has_room(self, seg, tick):
        """Can an item enter at the tail right now?

        Room exists once the last item has moved a full `spacing` clear of
        the entrance -- an empty run always has room. Shared by the tick's
        produce step and by `push()` so the two cannot disagree about when a
        belt is full.
        """
        if seg.head == seg.tail:
            return True
        return self.position_of(seg, seg.tail - 1, tick) >= self.spacing

    def _push(self, seg, payload, tick):
        """Place an item at the tail. Assumes `has_room()` already said yes."""
        slot = seg.tail % seg.cap
        seg.enter_tick[slot] = tick
        seg.payload[slot] = payload
        seg.tail += 1
        self.stats["pushed"] += 1

    def push(self, seg, payload, tick):
        """Put an item on a run's tail if it fits. Returns whether it did.

        For producers that are not part of the tick's offer/accept protocol --
        currently a seeded belt cell (see `Engine.seed_segments()`), which is
        a standing instruction rather than a machine with a waiting zone to
        drain. It goes through the same room check as the tick's own push, so
        a seed cannot overfill a run or land on top of another item.
        """
        if not self.has_room(seg, tick):
            return False
        self._push(seg, payload, tick)
        # A seed is a push from outside the tick's offer/accept protocol, so
        # it has to wake the run itself: seeding happens right after a
        # rebuild, and the seeded run would otherwise be asleep from the
        # first tick with an item on it that never moves.
        self.wake(seg)
        return True

    # --- the tick --------------------------------------------------------
    def tick_segment(self, seg, tick, offer=None, accept=None, took=None):
        """Advance one segment by one tick. O(1), no loops over items.

        Order is load-bearing and comes straight from the report: advance the
        anchor, then pop, then push. Returns `(popped, pushed)`.

        `took(seg)` is called after a push actually happens. It is separate
        from `offer()` because offering must not commit: a full belt declines,
        and the item has to stay with its producer as the back-pressure
        signal the next structure phase reads.
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
        # **"The producer was asked and gave nothing"**, which is not the same
        # as "nothing was pushed": a run with no room is never asked, so its
        # producer may be holding an item and about to be taken from. Only the
        # asked-and-declined case licenses the sleep clause in
        # `is_at_rest()`, so the two are tracked apart. Starts False, meaning
        # "no information", which is the conservative value.
        declined = False
        if offer is not None:
            if self.has_room(seg, tick):
                payload = offer(seg)
                if payload is None:
                    declined = True
                else:
                    self._push(seg, payload, tick)
                    pushed = True
                    # Only now has the producer actually parted with it.
                    if took is not None:
                        took(seg)

        if popped:
            self.stats["popped"] += 1
        # Hand the producer's answer to the sleep test via the segment rather
        # than widening the return tuple. **Deliberate, and the lesser evil:**
        # `(popped, pushed)` is unpacked at ~25 call sites across the tests,
        # the oracle comparison and two probe tools, and a third element would
        # churn all of them to deliver a value only `tick()` consumes -- one
        # line below the call.
        #
        # It is per-tick scratch, not state: written on every visit, read
        # immediately, and never persisted or uploaded. `_declined` rather
        # than a public name for exactly that reason.
        seg._declined = declined
        # `pushed` is counted by _push(), the single place an item enters a
        # ring -- counting again here would double every tick's push and also
        # miss a seed, which does not come through this path at all.
        return popped, pushed

    # --- sleeping ---------------------------------------------------------
    #
    # A segment at rest is a **fixed point of tick_segment()**, not merely
    # "probably quiet", and that is what makes skipping it safe rather than a
    # heuristic. Three facts combine:
    #
    #   - the anchor advance is `min(anchor + v, L)`, which saturates at `L`
    #     and returns `L` for ever after;
    #   - the pop needs `head != tail`, so an empty run never pops;
    #   - the push needs `offer()` to yield a value, and an idle upstream gate
    #     yields None.
    #
    # So two states are provably inert:
    #
    #   **empty, anchor saturated** -- a fresh or fully drained run.
    #   **holding items with no room** -- the interesting one for a machine
    #     built out of these parts: a run storing a value nobody is taking.
    #     Its anchor has already stopped, `has_room()` is False so nothing
    #     enters, and the only thing that could change it is the consumer
    #     deciding to take -- which is the downstream gate's event, not the
    #     segment's. Item positions freeze too, so the closed form keeps
    #     describing it and the renderer needs no special case.
    #
    # ...and a third, which needs one fact the segment does not own:
    #
    #   **holding items, head parked at the end, and the producer offering
    #     nothing.** The two above are *local* -- "no room" is a property of
    #     the run. But the room test is the wrong question when the run is
    #     nearly empty: capacity is `L/d + 1`, so a 3-cell run needs *four*
    #     items before `has_room()` goes False, and one item short of that the
    #     predicate notices nothing even though the upstream gate is idle and
    #     will offer nothing for a thousand ticks.
    #
    #     Measured, and it is the larger of the two remaining gaps: of the
    #     awake non-empty runs after 60 ticks, **42 of 67 on cpu.npz and 92 of
    #     107 on Ram2.npz** were blocked-with-room behind a producer holding
    #     nothing. The whole awake set was being ticked to re-derive "the
    #     producer still has nothing".
    #
    #     The missing term is not the run's to answer: "nothing can enter" is
    #     a property of the segment, while "nothing will be offered" is a
    #     property of the upstream *gate*. `offers_nothing` carries it in --
    #     see `is_at_rest()` on why it is a parameter rather than a lookup.
    #
    # This matters far more than the factory workload suggested. A *computer*
    # built from these parts runs one trace through a sea of memory: measured
    # on a CPU-shaped board, 1101 of 1101 segments were inert and the belt
    # phase spent 0.83ms of 0.91ms proving it. See tools/probe_wakelist.py,
    # which asserts both fixed points by brute force and reports the fraction.
    #
    # **Waking is edge-triggered, never polled.** Polling every segment for
    # "should I wake?" is the cost being removed. Instead `wake()` is called
    # by whatever *causes* a change: a gate filling a waiting zone, a gate
    # draining an input, a seed, a rebuild. Missing a wake call stalls a run
    # silently, so the set of callers is deliberately small and
    # `tools/check_wakelist_safe.py` diffs an awake-only tick against ticking
    # everything.
    #
    # It is also the GPU shape: the awake set is a compacted index buffer, and
    # the sleep test is a per-segment predicate a compute pass can evaluate
    # and compact. That is the same structure GateSim._candidate_rows() has.
    def is_at_rest(self, seg, tick, offers_nothing=False):
        """Is this segment in a fixed point of `tick_segment()`?

        Conservative: a False answer only costs a tick of work, while a wrong
        True silently freezes a run. Every branch is one of the exact
        conditions argued above, with no slack.

        `offers_nothing` is the caller's assertion that this run's **producer
        yielded nothing on this tick** -- the one term the segment cannot
        derive, since it is a fact about the upstream gate. Default False, so
        a caller who does not know keeps the old, purely-local answer; that
        matters because this is called from the shader-mirroring tests, from
        `tools/probe_wakelist.py`, and from `GateSim`, none of which should
        have to care.

        **A parameter rather than a lookup.** `BeltSim` holds no reference to
        the gate layer and must not acquire one: the belt phase is a compute
        dispatch later and `offer()` is already the sanctioned way for node
        state to reach it (see `tick()`'s "Segments are independent" note).
        The tick loop has just *called* `offer()`, so it knows the answer for
        free -- asking again here would both duplicate the call and invent a
        dependency the report's determinism argument rests on not existing.
        """
        length = self.length_of(seg)
        if seg.head == seg.tail:
            return seg.anchor >= length
        # Holding items: **both** halves of the tick have to be unable to act.
        #
        # Nothing can enter -- that is the room test, and it was the only test
        # here originally. But the *pop* is also work, and it needs the head
        # to be exactly at the end. A run whose head is still travelling has
        # work to do on some future tick with no external event to announce
        # it: an item's position is a closed form over `tick`, so it keeps
        # advancing whether or not the run is visited, and the arrival is the
        # run's own business rather than a wake anyone raises.
        #
        # Missing this stranded every **seeded** run that feeds a starved
        # gate. `seed_segments()` pushes at position 0, which is the entrance,
        # so `has_room()` is False immediately and the run was slept on its
        # very first tick -- while the item visibly slid down the belt (closed
        # form) and then sat at the end for ever, because the pop never ran.
        # Nothing could wake it either: a seeded run has no producer, so no
        # `src` edge exists, and the gate ahead was starved precisely because
        # delivery never happened. The player sees a seeded value travel the
        # belt and then refuse to enter the gate.
        #
        # The head-at-end case is still a real fixed point, and it is the one
        # that matters for memory: a full run parked against a consumer that
        # will not take. There the pop is attempted and refused, and the
        # consumer taking *is* an event that raises a wake.
        if self.position_of(seg, seg.head, tick) < length:
            return False
        # Either nothing can enter (local, and the RAM case), or nothing will
        # be offered (the producer term). The head-at-end test above guards
        # **both**, and keeping it is the whole safety argument for the new
        # clause rather than an inherited detail:
        #
        # A run holding one item mid-flight has *identical stored state* for
        # as many ticks as the item takes to travel -- traced on a 10-cell
        # run, `(head, tail, anchor) == (0, 1, 2560)` from tick 1 to tick 10
        # while the item visibly slid the whole length. Position is a closed
        # form over `tick`, so it advances whether or not the run is visited,
        # and the pop at tick 11 has no external cause. So an idle producer on
        # its own is *not* a fixed point: sleeping on it would strand the item
        # mid-belt, which is exactly the seeded-belt bug the head-at-end
        # clause was added to fix, re-entered by a different door.
        if not self.has_room(seg, tick):
            return True
        return offers_nothing

    def is_asleep(self, sid):
        """Is the belt phase currently skipping this segment id?

        The public read of the sleep set, for `BeltDevice` (which uploads it
        for the SLEEP_DEBUG overlay) and the tests. A method rather than
        exposing `_asleep`, so a caller cannot mutate it by accident -- every
        change goes through `wake()` or the tick's own decision.
        """
        return sid in self._asleep

    def wake(self, seg):
        """Mark a segment as needing ticks again. Idempotent and cheap.

        Called by the *cause* of a change rather than discovered by polling --
        see the block comment above.
        """
        self._asleep.discard(seg.sid)

    def wake_all(self):
        """Wake every segment. Used after a rebuild or a wholesale change."""
        self._asleep.clear()

    def set_sleep_enabled(self, on):
        """Turn sleeping off entirely, for a differential check.

        `wake_all()` is not enough on its own for that job, and the difference
        is what made the first `check_wakelist_safe.py` vacuous: waking
        everything before a tick still lets that tick *re-sleep* runs, so the
        "oracle" accumulates a sleep set of its own and stops being an oracle
        after one tick. This latches the behaviour off instead, so a reference
        timeline ticks every segment every tick however quiet the board gets.

        Not a performance switch and not reachable from the UI -- the only
        caller is `tools/check_wakelist_safe.py`. It exists so a checker can
        ask for "no scheduling at all" in one call per layer, rather than
        knowing how each layer stores its own sleep set.
        """
        self._sleep_enabled = bool(on)
        if not self._sleep_enabled:
            self._asleep.clear()
        return self._sleep_enabled

    def awake_segments(self):
        """The segments a tick must visit, in segment-id order.

        Id order rather than dict order so the traversal does not depend on
        which runs happen to be awake -- the phase is designed to be
        order-independent, and a varying order would turn any future order
        dependence into a heisenbug.
        """
        segs = self.index.segments
        if not self._asleep:
            return [segs[sid] for sid in sorted(segs)]
        return [segs[sid] for sid in sorted(segs) if sid not in self._asleep]

    def tick(self, tick, offer=None, accept=None, took=None):
        """Advance every awake segment by one tick.

        `offer` / `accept` / `took` are called per segment, so one set of
        callbacks can serve the whole board. Returns `(popped, pushed)`
        totals.

        Segments are independent: the report's only shared state is at the
        *nodes*, and nothing here touches a node. That independence is what
        makes this one compute dispatch later -- the loop below becomes
        `gl_GlobalInvocationID.x` and nothing else changes.

        A segment that ticks without changing anything, and is in a fixed
        point afterwards, goes to sleep and is skipped until something wakes
        it. A segment is re-tested *after* its tick rather than before, so a
        run always gets the tick that settles it -- testing first would need
        the same arithmetic the tick does and save nothing.
        """
        popped = pushed = 0
        asleep = self._asleep
        sleeping = self._sleep_enabled
        prof = self.profiler
        # Building the awake list is timed apart from ticking it, because the
        # two scale with different things and the wake list changed which one
        # dominates. `schedule` is O(segments) -- it sorts every segment id on
        # the board, asleep or not -- while `segments` is O(awake). On a
        # CPU-shaped board where 99% sleeps, the scheduler deciding what to
        # skip can cost more than the work it skipped, and only separating
        # them shows that.
        with prof.probe("belt/transport/schedule"):
            due = self.awake_segments()
        for seg in due:
            p, q = self.tick_segment(seg, tick, offer, accept, took)
            popped += int(p)
            pushed += int(q)
            # Nothing moved and the run is inert: stop visiting it. The
            # `not (p or q)` guard is belt and braces -- a segment that just
            # popped or pushed is not at rest by either test -- but it keeps
            # "did work" and "is inert" from having to agree.
            if sleeping and not (p or q) and self.is_at_rest(
                    seg, tick, seg._declined):
                asleep.add(seg.sid)
        self.stats["ticks"] += 1
        self.stats["asleep"] = len(asleep)
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
