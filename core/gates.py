"""Gate firing, and the port pass that joins gates to belts.

This is the other half of the two-phase tick described in core/tick.py. Read
that first -- the ordering here only makes sense against it.


What runs when
--------------

**Structure phase** (`GateSim.structure_phase`). Every gate that can fire,
fires: it reads its input channels, clears them, and puts results in its
output waiting zones. It touches no belt state at all. The decision to fire
depends only on values frozen since the end of the previous tick, so it is
order-independent -- which is what makes it a compute dispatch later.

**Belt phase.** A single `BeltSim.tick()` with this module supplying the
producer and consumer callbacks: `offer_to()` reads the upstream gate's
waiting zone, `accept_from()` writes the downstream gate's input channel.

That the callbacks do the gate half is not incidental -- it is the only way
to get the ordering right. docs/belt_report.md is explicit that one belt tick
must **advance the anchor, then pop, then push**, in that sequence, and the
pop's correctness argument depends on it: `A == L` holds before the pop, so
setting `A = L - d` after `head++` leaves every remaining item's packed
position unchanged and nothing jumps.

Doing the gate work in separate passes *around* `BeltSim.tick()` -- drain,
then transport, then load -- breaks that sequence. The drain pushes before
the anchor has advanced and before the pop, so it sees a belt one slot
fuller than it is about to be and declines; the pop then happens with
nothing new behind it. A saturated chain loses roughly **one tick in seven**
(measured: 26 items over 30 ticks against the correct 30).

That shortfall is easy to accept, because it presents exactly like the
one-tick stall recovery the design does document. It was written that way
first, and `test_well_fed_gate_fires_every_tick` is what caught it -- worth
keeping for that reason alone.

So the port work happens *inside* the belt tick, at the two points the
report puts it. These are still the only code that touches both a gate and a
belt, and the transport pass still only reads node decisions rather than
making them, which is the report's determinism recipe ("Nodes are the only
shared state").


Back pressure
-------------

A gate is blocked when an output waiting zone **that has a consumer** is
still occupied. The belt phase empties them; if a belt is full the zone stays
occupied and the gate does not fire next tick. That is the whole mechanism --
no explicit "is the belt full" query anywhere, which matters because a gate
must not read belt state during the structure phase.

A consequence worth knowing: **an occupied waiting zone at the end of a tick
is a renderable state.** It means "this gate produced something the belt
would not take", and the renderer draws it squished between the gate and the
belt. While things flow it is always empty by end of tick, so nothing shows.

**A dangling output is a sink.** A result sent to an output face with nothing
on the far side is destroyed at fire time rather than filling a zone nobody
will drain. So leaving a Shear's pass-through unconnected no longer stalls
the machine -- it runs at full rate and the pass-through evaporates, which is
what makes an explicit Sink unnecessary for tidying up outputs you do not
want.

This is a deliberate end to reversible computing here: a gate may now destroy
information. When "backwards" mode arrives, these discard points are where a
machine will have to remember a little of what it dropped.

It also collapses what used to be a special case. `_connected_channels()` was
consulted only by generators, on the grounds that a machine *consuming*
inputs must write every result or silently lose material -- the losing being
the thing to avoid. Now that losing it is the specified behaviour, every gate
consults the same connectivity, and generators keep exactly one difference:
they fill whichever free zones they find rather than requiring all of them
clear (see `_fire_generator`). Reading the grid is still safe for the reason
`_connected_channels()` documents: connectivity is belt *presence*, frozen
for the whole phase, not belt *state*.


Direct gate-to-gate connections
-------------------------------

Two gates can be adjacent with no belt between them. `drain_waiting()`
handles that by writing the receiving gate's input channel directly. The
receiving gate's structure phase has already run this tick, so it sees the
value at the start of the *next* tick -- the same one-tick hop a belt would
cost, which is what keeps the two cases consistent.

Contention is not an issue even though several gates may write inputs in the
same pass: each (gate, channel) pair has exactly one upstream source, because
a port face has exactly one neighbour. The pass reads only end-of-structure
state and writes disjoint slots.
"""

import numpy as np

from core import cell
from core.entity import MAX_CHANNELS, Entity
from core.tick_profile import Profiler


class GateSim:
    """Fires gates, and moves items between gates and belt segments.

    Holds no state of its own: everything lives in entity rows and segment
    objects, so there is no second copy to drift. Mirrors BeltSim in that
    respect.
    """

    def __init__(self, board, index, belts):
        self.board = board
        self.index = index
        self.belts = belts
        #: "gates_asleep" is a gauge rather than a total -- how many gates the
        #: last structure phase skipped -- matching belts.stats["asleep"].
        self.stats = {"fired": 0, "blocked": 0, "starved": 0,
                      "gates_asleep": 0,
                      "drained": 0, "loaded": 0, "consumed": 0, "direct": 0,
                      "unconnected": 0, "destroyed": 0}
        #: gate row -> connected output channels, for the current tick only.
        #: See _connected_channels() for why a per-tick cache is sound.
        self._drains_cache = {}
        #: gate row -> ((world_cell, face, channel), ...). See _output_faces().
        self._faces_cache = {}
        #: segment id -> (row, channel) | None, for each end of a run.
        #: See the "resolving a segment's ends" block below.
        self._src_cache = {}
        self._dst_cache = {}
        #: (rows, n_in, n_out, strict) arrays for the prefilter, or None.
        #: See _gate_table().
        self._gate_table_cache = None
        #: (downstream, upstream) gate row -> segments, or None. See
        #: _segments_of_gate().
        self._gate_segments = None
        #: Gate rows currently at rest. See the "sleeping gates" block below.
        #: Empty means "every gate is visited", the pre-sleep behaviour and so
        #: the safe default after any wholesale change.
        self._asleep = set()
        #: False disables gate sleeping outright, for
        #: `tools/check_wakelist_safe.py`. See set_sleep_enabled().
        self._sleep_enabled = True
        #: Per-tick timing. A disarmed stand-in by default rather than None,
        #: so every probe site is one shape and needs no guard; `Engine`
        #: replaces it with the clock's, which is the one the player arms.
        #: Not taken as a constructor argument because the gate layer is
        #: constructed before the clock is wired to it in several tests, and
        #: a profiler is diagnostics rather than a dependency.
        self.profiler = Profiler()

    def invalidate_connectivity(self):
        """Drop every cached derivation of the grid and the segmentation.

        Called when the thing they are derived from changes: the grid (a
        placement, an erase, an in-place turn) or the segmentation. Cheap and
        idempotent.

        All four caches are keyed by an id that gets **recycled** -- a row by
        the free list, a segment id by `SegmentIndex._release_sid()` -- so
        dropping them together on any grid change is what keeps a new entity
        from inheriting the previous occupant's ports, or a new run from
        inheriting the previous one's gate.

        One call for all four deliberately: they share an invalidation
        condition exactly, and splitting them would be four things to forget
        instead of one.
        """
        self._drains_cache.clear()
        self._faces_cache.clear()
        self._src_cache.clear()
        self._dst_cache.clear()
        self._gate_table_cache = None
        self._gate_segments = None
        # The gate sleep set is an *answer derived from* that adjacency -- "my
        # neighbours are quiet" -- so dropping the adjacency without dropping
        # the conclusion would leave gates asleep on a claim about a board
        # that no longer exists. Same recycling hazard the four caches above
        # document, with a silent-freeze consequence instead of a wrong-port
        # one: rows are recycled by the free list, so a new machine could
        # inherit the previous occupant's sleep.
        self._asleep.clear()

    # --- gate discovery ---------------------------------------------------
    #: Types that are rows but not gates. Checked against the raw type column
    #: before a view is built, so a belt-heavy board costs one array read per
    #: row rather than a view object.
    _NOT_A_GATE = frozenset(
        (cell.TYPE_EMPTY, cell.TYPE_BELT, cell.TYPE_UNDER))

    def gates(self):
        """Every live structure on the board, as views.

        Scans the entity table rather than the grid: a structure occupies
        several cells but is one row, and firing is per entity.

        **Iterates the live rows, not the capacity.** The table is sparse --
        3500 entities can sit in 8192 rows -- and this runs twice per tick
        (structure_phase and deliver_direct). Walking the capacity and
        building a view per live row cost 2.2ms a tick on a mid-sized board,
        against 0.06ms for the belt transport it exists to feed.

        The type is read straight out of the entity array rather than through
        a view, because most rows on a real board are *belts*: building a
        view object to discover a row is not a gate is the bulk of the work
        when 90% of rows are not. The view is constructed only for the rows
        that survive the filter.
        """
        board = self.board
        types = board.entities[:, Entity.F_TYPE]
        for row in board.live_rows():
            if int(types[row]) in self._NOT_A_GATE:
                continue
            view = board.view(row)
            if view.is_ghost:
                continue
            yield view

    # --- the vectorised prefilter -----------------------------------------
    #
    # Most gates on a large board can do nothing on a given tick: measured on
    # a 672-gate board, 576 were starved and 96 blocked, with none firing. The
    # phase below still built a view for each, asked its firing rule, resolved
    # its connectivity and tested its zones -- about 48k Python-level calls a
    # tick to reach "no".
    #
    # `_candidate_rows()` answers "could this gate possibly act?" for every
    # gate at once, as array operations over the entity table. The per-gate
    # Python path then runs only on the survivors, unchanged.
    #
    # **It must never exclude a gate the full phase would have fired**, or it
    # changes behaviour rather than merely speeding it up. So it tests only
    # *necessary* conditions, and only ones that are pure row state:
    #
    #   - the type's firing rule is satisfiable by occupancy alone. FIRE_ALL
    #     needs every declared channel fed; FIRE_ANY needs one. FIRE_SELECT
    #     is value-dependent and `inputs_ready()` is overridable, so those
    #     types are **always admitted** and decided in Python.
    #   - at least one output zone is free. With every zone full a gate is
    #     blocked under both the all-or-nothing rule and the generator's
    #     "fill whichever are free".
    #
    # Everything else -- connectivity, the arithmetic, which inputs are
    # consumed -- stays exactly where it was.
    #
    # This is also the shape the GPU port wants: a cheap parallel predicate
    # over every entity, then a compacted index list to dispatch the
    # expensive kernel over. The prefilter is the predicate pass and the
    # survivor list is the compaction, so the port replaces each half with a
    # dispatch instead of restructuring the phase.
    def _gate_table(self):
        """(rows, n_in, n_out, strict) for every live gate, as arrays.

        Rebuilt only when the set of gates changes, since all four are
        functions of which rows hold which type -- not of anything the tick
        mutates. `strict` marks the rows whose readiness is decided by
        occupancy (FIRE_ALL); the rest are always admitted.
        """
        if self._gate_table_cache is not None:
            return self._gate_table_cache

        rows, n_in, n_out, strict = [], [], [], []
        for gate in self.gates():
            cls = type(gate)
            rows.append(gate.row)
            n_in.append(gate.input_count())
            n_out.append(gate.output_count())
            # FIRE_ANY needs one fed channel; FIRE_ALL needs all of them.
            # FIRE_SELECT (and any type overriding inputs_ready()) is not
            # expressible as an occupancy count, so it is never filtered out.
            strict.append(cls.FIRE_RULE == cls.FIRE_ALL)

        table = (np.asarray(rows, dtype=np.int64),
                 np.asarray(n_in, dtype=np.int64),
                 np.asarray(n_out, dtype=np.int64),
                 np.asarray(strict, dtype=bool))
        self._gate_table_cache = table
        return table

    def _candidate_rows(self):
        """(admitted rows, n_starved, n_blocked) for this tick.

        Conservative -- see the block comment above. The two rejection counts
        come back with the set because the masks that decide them are already
        computed here: classifying a skip costs nothing, and it is what keeps
        `starved` and `blocked` meaning what they always meant. Without that
        the prefilter would silently empty two stats the panel and three tests
        read, which is a behaviour change dressed as an optimisation.
        """
        rows, n_in, n_out, strict = self._gate_table()
        if not len(rows):
            return [], 0, 0

        blk = self.board.entities[rows]
        held = blk[:, Entity.F_IN_HELD:Entity.F_IN_HELD + MAX_CHANNELS] > 0.0
        free = blk[:, Entity.F_OUT_HELD:Entity.F_OUT_HELD + MAX_CHANNELS] \
            <= 0.0

        # Mask the channel columns a type does not declare, so an unused slot
        # cannot be mistaken for an unfed one (or a free zone).
        lanes = np.arange(MAX_CHANNELS)
        in_used = lanes[None, :] < n_in[:, None]
        out_used = lanes[None, :] < n_out[:, None]

        n_fed = (held & in_used).sum(1)
        # A type with no inputs is vacuously ready -- that is what lets a
        # Source through with no special case.
        ready = np.where(strict, n_fed == n_in, n_fed > 0) | (n_in == 0)
        # No outputs at all (a Sink) can never be output-blocked.
        has_room = ((free & out_used).sum(1) > 0) | (n_out == 0)

        # Readiness is tested first in the phase below, so a gate that is both
        # unready and full counts as starved -- matching the order the
        # per-gate path would have reported it in.
        starved = int((~ready).sum())
        blocked = int((ready & ~has_room).sum())
        # A **list in ascending row order**, not a set: the phase iterates
        # this directly, and a set's order varies with hash layout. The phase
        # is designed to be order-independent (that is the whole point of the
        # two-phase split), but a nondeterministic iteration order would make
        # any *future* order dependence a heisenbug rather than a test
        # failure. Ascending row order is also what the old whole-table scan
        # produced, so stats and any logging stay comparable.
        admit = rows[ready & has_room]

        if not self._sleep_enabled:
            return admit.tolist(), starved, blocked

        # --- the sleep pass ---
        #
        # Two jobs, and they are deliberately on *different* row sets.
        #
        # **Put rejected gates to sleep.** A gate the prefilter just rejected
        # is starved or blocked, which is the precondition for being inert --
        # so this is where the candidates are, not among the admitted. The
        # predicate then adds the part occupancy cannot see: whether anything
        # could change that before next tick.
        #
        # **Skip gates already asleep.** The masks above are computed for
        # every row regardless, because they are one vectorised slice and
        # excluding rows first would cost more than it saves (sorting rows
        # buys ~15% while slicing buys 60x -- see
        # tools/probe_batch_prefilter.py). What sleeping removes is the
        # *per-gate Python*, which is the expensive half.
        asleep = self._asleep
        awake = ready & has_room
        if asleep:
            # An admitted gate that is asleep stays skipped. It can only be
            # here if it is ready and unblocked, which a sleeping gate should
            # never be -- but a stale sleep set is a silent freeze, so this
            # is belt and braces rather than trust: being admitted means
            # something changed, and something changing means it should wake.
            for i, row in enumerate(rows):
                if awake[i] and int(row) in asleep:
                    asleep.discard(int(row))

        # Only consider gates that did *not* get admitted. `np.nonzero` over
        # the rejection mask keeps this a vectorised selection; the per-gate
        # predicate then runs on candidates only, which on a quiet board is
        # where almost every row already is.
        rejected = np.nonzero(~awake)[0]
        for i in rejected:
            row = int(rows[i])
            if row in asleep:
                continue
            # `held_in` is "a declared input channel is fed" and `held_out`
            # is "a declared output zone is occupied" -- both expressed as
            # *occupancy*, so an undeclared lane contributes False to each and
            # cannot be mistaken for either. `free` is the inverse of
            # occupancy, so the zone test is `~free & out_used` rather than
            # `free & out_used`; passing the latter would read every
            # undeclared lane as "not free" and never sleep anything.
            if self._gate_is_inert(row, held[i] & in_used[i],
                                   (~free[i]) & out_used[i]):
                asleep.add(row)
        self.stats["gates_asleep"] = len(asleep)
        return admit.tolist(), starved, blocked

    # --- waking the belts a gate touches ----------------------------------
    #
    # A sleeping segment is only skippable while nothing can change it, and
    # the two things that can are both gate events (see BeltSim's "sleeping"
    # block):
    #
    #   - the gate **upstream** of a run puts something in a waiting zone, so
    #     the run now has something to accept;
    #   - the gate **downstream** frees an input channel, so a run that was
    #     full and refused can now deliver.
    #
    # Both are edge-triggered from `structure_phase()`, the single place gate
    # channels change. The reverse index is the inverse of the `_src_cache` /
    # `_dst_cache` resolution -- gate row -> the segments on either side --
    # and is built lazily from the same `seg.src` / `seg.dst` the forward
    # direction uses, so the two cannot disagree about who is adjacent to
    # whom.
    def _segments_of_gate(self, row):
        """(fed_by_this_gate, draining_into_this_gate) segment lists.

        Built for every gate at once on first use, because a per-gate scan of
        every segment would be the O(gates x segments) cost the wake list
        exists to avoid.
        """
        if self._gate_segments is None:
            downstream = {}
            upstream = {}
            for seg in self.index.segments.values():
                src = self._src_cached(seg)
                if src is not None:
                    downstream.setdefault(src[0], []).append(seg)
                dst = self._dst_cached(seg)
                if dst is not None:
                    upstream.setdefault(dst[0], []).append(seg)
            self._gate_segments = (downstream, upstream)
        downstream, upstream = self._gate_segments
        return downstream.get(row, ()), upstream.get(row, ())

    def _wake_around(self, row):
        """Wake the belt segments adjacent to a gate that just changed state.

        **Belts only, and deliberately not `row` itself.** A gate that reaches
        this has just fired, so it is plainly awake; what it has changed is
        the material available to the runs on either side of it.

        A gate whose *channels* are written by something else does need
        waking, and that is done at each such site rather than here --
        `accept_from()`, `took_from()` and `deliver_direct()` each discard the
        rows they touched. Folding those into this method is the tempting
        tidy-up and would be wrong in a way that is hard to see: this wakes
        what is *adjacent to* a row, so `_wake_around(other.row)` reads like
        it wakes `other` and does not. That exact misreading left a
        directly-fed gate unwoken -- see the comment in `deliver_direct()`.
        """
        downstream, upstream = self._segments_of_gate(row)
        belts = self.belts
        for seg in downstream:
            belts.wake(seg)
        for seg in upstream:
            belts.wake(seg)

    # --- sleeping gates ---------------------------------------------------
    #
    # The prefilter made reaching "this gate can do nothing" cheap; it did not
    # make it free. On cpu.npz it still costs 1.4ms a tick to touch all 670
    # rows, and `structure/fire` costs 4.6ms on the 378 it admits -- so the
    # same argument that justified sleeping segments applies here, one layer
    # up: stop *looking* at a gate that provably cannot act.
    #
    # **What can change a gate's state.** Exactly four things write a gate's
    # channels, and that closed list is what makes a predicate possible:
    #
    #   1. its own `fire()`, during the structure phase;
    #   2. `accept_from()`, when a belt delivers its head item into an input;
    #   3. `took_from()`, when a belt accepts what a waiting zone held;
    #   4. `deliver_direct()`, either as the giver or the receiver.
    #
    # (2) and (3) are belt events, and a *sleeping* segment raises neither --
    # that is exactly what `BeltSim.is_at_rest()` proves. (4) is a neighbouring
    # gate's event, and a sleeping gate raises none. (1) is this gate's own,
    # and a gate that is starved or blocked does not fire.
    #
    # So a gate is inert when it cannot fire **and** every neighbour that
    # could change that is itself asleep. Composition is the whole mechanism:
    # no deadline, no countdown, no "asleep for N ticks" protocol passed
    # between layers. Each layer sleeps on a local predicate and wakes on an
    # edge the other already raises.
    #
    # **Why no timers.** The tempting version is "my input belt is 20 cells
    # long and empty, so I cannot be fed for 20 ticks". That is a *prediction*
    # rather than a proof, and it can be wrong in the unsafe direction: an
    # edit, a `flush_material()`, a seed, or a producer starting early all
    # invalidate it, and a deadline that is too long silently freezes a gate.
    # It also needs a per-gate timer plus either an O(gates) countdown sweep
    # every tick (the very cost being removed) or a heap the GPU port has no
    # analogue for. Edge-triggering gets the same answer for free: the belt
    # wakes the gate when it actually has something, so the gate sleeps for
    # exactly the right number of ticks without anyone computing N.
    #
    # **The failure mode is mutual sleep.** Gate A sleeps because belt B is
    # asleep; belt B sleeps because gate A is asleep. If that is a genuine
    # fixed point it is correct and desirable -- it is the RAM case. If one
    # wake edge is missing it is a permanent stall that nothing will ever
    # break, and it is silent. Hence the predicate below is deliberately
    # conservative at every step, `_wake_around()` is the single place both
    # layers' edges are raised, and `tools/check_wakelist_safe.py` registers
    # gates as a second `_Sleeper` so a sleeping timeline is diffed against a
    # fully-ticked one every tick.
    def _gate_is_inert(self, row, held_in, held_out):
        """Could this gate be skipped entirely this tick?

        `held_in` / `held_out` are this row's *occupancy* masks over declared
        channels, already computed by the prefilter -- passed in rather than
        re-read, because this runs per candidate gate and the arrays are
        right there. Occupancy for both, so an undeclared lane is False in
        each and cannot be read as either "fed" or "blocked".

        Conservative in the same sense `_candidate_rows()` is: a False answer
        costs a tick of work, a wrong True silently freezes a gate for ever.
        """
        # Anything already in a waiting zone has to go somewhere, and the
        # thing that drains it (a belt pop, or a neighbour taking it) is not
        # this gate's own event -- so a gate holding output is never inert.
        # This is the conservative reading: the zone may be dangling and
        # therefore truly stuck for ever, but deciding that needs
        # connectivity, which is exactly the Python work being skipped.
        if held_out.any():
            return False
        # A fed input is material this gate may yet act on, so it stays awake
        # until it has consumed it. Strictly weaker than asking whether the
        # type's rule is satisfied, which is the safe direction -- and it
        # avoids mirroring FIRE_SELECT and every override of inputs_ready()
        # into the scheduler.
        if held_in.any():
            return False
        # Nothing held either way, which is the whole proof: `fire()` needs
        # material, so ticking this gate cannot change anything. It does not
        # matter *why* it is empty, or whether a neighbour is about to fill
        # it -- it only matters that it wakes before it next holds something,
        # and every path that gives a gate material raises that edge:
        #
        #   accept_from()     a belt loads an input   -> discards the row
        #   took_from()       a belt drains a zone    -> discards the row
        #   deliver_direct()  a neighbour hands over  -> discards both rows
        #   _fire_admitted()  its own fire            -> it is awake already
        #   flush_material()  a wholesale reset       -> wake_all_gates()
        #
        # That list is closed -- those are the only writers of a gate's
        # channels in the codebase -- which is what turns "probably fine" into
        # an argument. A new writer must raise the edge, and the cost of
        # forgetting is a silent freeze.
        #
        # **This deliberately does not ask about neighbours.** An earlier
        # version required every adjacent run to be empty and every adjacent
        # gate asleep, reasoning that a gate should only sleep once nothing
        # could feed it. That is sound but redundant given the wake list
        # above, and it is expensive: it halves the gates that sleep (11.3
        # against 21.4 of 36 on the checker's fixture) and costs a walk over
        # every neighbour per candidate gate, every tick -- an O(gates x
        # neighbours) scan to re-derive what an edge already tells us. The
        # wake list exists precisely so that nobody has to poll.
        #
        # It also hid a real bug rather than preventing one: `deliver_direct()`
        # called `_wake_around(other.row)`, which wakes what is *adjacent* to
        # the receiver and never the receiver itself, so a directly-fed gate
        # was never woken. The neighbour test masked it by keeping that gate
        # awake for unrelated reasons. Dropping the test is what exposed it.
        return True

    def wake_gate(self, row):
        """Mark a gate as needing ticks again. Idempotent and cheap."""
        self._asleep.discard(row)

    def wake_all_gates(self):
        """Wake every gate. Used after a rebuild or a wholesale change."""
        self._asleep.clear()

    def set_sleep_enabled(self, on):
        """Turn gate sleeping off entirely, for a differential check.

        Mirrors `BeltSim.set_sleep_enabled()`, and latched for the same
        reason: waking everything still lets the coming tick re-sleep, so an
        oracle timeline built on `wake_all_gates()` would accumulate a sleep
        set and stop being an oracle after one tick.
        """
        self._sleep_enabled = bool(on)
        if not self._sleep_enabled:
            self._asleep.clear()
        return self._sleep_enabled

    def _rows_holding_output(self):
        """Gate rows with anything in a waiting zone, ascending.

        The prefilter for `deliver_direct()`, and the same bargain as
        `_candidate_rows()`: a gate holding nothing has nothing to hand to a
        neighbour, so the port walk is pure waste. Measured at 96 of 672
        gates on a mid-sized board, so it skips 86% of them.

        Necessary-condition only, as the other prefilter is: holding
        something is a precondition for delivering it, and every other test
        (is the neighbour a gate, does its input face point back, is that
        channel free) stays in the per-gate path.

        **The gate sleep set needs no mention here**, which is worth stating
        because its absence looks like an oversight. `_gate_is_inert()`
        refuses to sleep a gate holding anything in a waiting zone, so a
        sleeping gate cannot pass the `held.any()` test below -- the sleep
        set would be a strictly redundant filter over a mask that already
        excludes it. Adding one would also make two places responsible for
        the same exclusion, and they could then disagree.
        """
        rows, _n_in, _n_out, _strict = self._gate_table()
        if not len(rows):
            return []
        held = self.board.entities[rows][
            :, Entity.F_OUT_HELD:Entity.F_OUT_HELD + MAX_CHANNELS] > 0.0
        return rows[held.any(axis=1)].tolist()

    def invalidate_gate_table(self):
        """Drop the cached gate table. Called when the gate set changes."""
        self._gate_table_cache = None

    # --- structure phase --------------------------------------------------
    def structure_phase(self):
        """Fire every gate that can. Returns the number that fired.

        Reads input channels and output waiting zones; writes output waiting
        zones and clears inputs. Deliberately touches no belt state -- see
        the module docstring.

        A vectorised prefilter decides which gates are worth looking at (see
        `_candidate_rows()`); the per-gate logic below is unchanged and still
        makes every real decision. A gate the prefilter skips is one that
        would have been counted `starved` or `blocked`, so those stats are
        credited in bulk rather than per gate.

        **Views are built for the admitted rows only.** Iterating `gates()`
        and testing membership would still construct a view per gate on the
        board, which is the bulk of what the prefilter exists to avoid -- it
        left `gates()` discovery as the phase's floor at 0.72ms. The admitted
        rows come straight from the prefilter, which already knows them.
        """
        fired = 0
        prof = self.profiler
        # The two halves the prefilter work split this phase into, timed
        # separately because that is the question it raises: the vectorised
        # pass is meant to be a flat cost that buys away a per-gate one, so
        # the useful number is the ratio between them. A board where `prefilter`
        # dominates `fire` is one where the prefilter is admitting almost
        # nothing -- working as intended -- and the reverse means the survivors
        # are where the time is.
        with prof.probe("structure/prefilter"):
            admit, pre_starved, pre_blocked = self._candidate_rows()
        self.stats["starved"] += pre_starved
        self.stats["blocked"] += pre_blocked
        board = self.board
        with prof.probe("structure/fire"):
            fired = self._fire_admitted(admit, board)
        self.stats["fired"] += fired
        return fired

    def _fire_admitted(self, admit, board):
        """The per-gate half of the structure phase: fire what was admitted.

        Split out of `structure_phase()` only so the prefilter and the per-gate
        work can be timed apart (see the probes there). Every decision is
        unchanged and still made here.
        """
        fired = 0
        for row in admit:
            # The gate table is rebuilt whenever the gate set changes, so a
            # row in it is live and real -- but a ghost can be committed or
            # despawned without the *set* changing, so the cheap guards stay.
            if not board.is_row_alive(row):
                continue
            gate = board.view(row)
            if gate.is_ghost:
                continue
            if not gate.inputs_ready():
                self.stats["starved"] += 1
                continue

            n_in = gate.input_count()
            if n_in == 0:
                if self._fire_generator(gate):
                    self._wake_around(gate.row)
                    fired += 1
                continue

            # Only the channels with somewhere to send a result can block.
            # A dangling output is a sink in its own right (see
            # _discard_dangling), so waiting on one would stall the machine
            # on a zone nothing will ever drain.
            drains = self._connected_channels(gate)
            if not gate.outputs_clear(drains):
                # Something is still waiting to leave. This is back pressure
                # arriving: the belt downstream did not take the last result.
                self.stats["blocked"] += 1
                continue

            values = [gate.in_value(i) if gate.in_held(i) else None
                      for i in range(n_in)]
            results = gate.fire(values)

            # Consume the inputs the machine says it used. The default is
            # every fed channel -- under FIRE_ANY that is whichever arrived,
            # which for a Sink means the one face that was loaded. Merge
            # overrides it to leave its unused data lane loaded.
            for i in gate.consumed(values):
                if gate.in_held(i):
                    gate.clear_input(i)
                    self.stats["consumed"] += 1

            for i, value in enumerate(results):
                if value is None:
                    # The machine declined this channel -- Route sending an
                    # item down one path and not the other. Not a discard:
                    # nothing was produced to begin with.
                    continue
                if i not in drains:
                    # A dangling output destroys what it is sent. This is the
                    # deliberate end of reversible computing here: a gate may
                    # drop a result it has nowhere to put, rather than filling
                    # a zone forever and jamming. When "backwards" mode
                    # arrives, this is the point that has to remember a little
                    # of what it destroyed.
                    self.stats["destroyed"] += 1
                    continue
                gate.set_output(i, value)
            # This gate's channels changed, so the runs on either side of it
            # may have work again -- a filled zone gives the downstream run
            # something to accept, and a cleared input lets a backed-up
            # upstream run deliver. Edge-triggered here because this is the
            # one place gate channels change.
            self._wake_around(gate.row)
            fired += 1

        return fired

    # --- the port callbacks, called from inside BeltSim.tick() ------------
    #
    # These run once per segment per tick -- the hottest code in the belt
    # phase -- and between them they need exactly four things out of an
    # entity row: is an output zone held, its value, is an input channel
    # held, and a write to either. All four are single floats at offsets
    # fixed by the class.
    #
    # So the hot path reads the entity array directly instead of binding a
    # view. `_bind()` was 0.44ms of a 0.55ms `_src_port()` -- building an
    # `Entity` subclass instance, resolving its type and testing its ghost
    # bit, to read one float. The *field offsets* still come from the class
    # (`F_OUT_HELD` and friends are class attributes, and MAX_CHANNELS fixes
    # the stride), so this is not a second copy of the layout; it is the same
    # constants without the object.
    #
    # The liveness and ghost guards stay, because a row can be freed or a
    # ghost committed between rebuilds -- that is exactly why the cache
    # stores a row and not a view.
    def _live_port(self, resolved):
        """(row, channel) if that row is a live non-ghost gate, else None.

        The shared preamble of the three callbacks, doing what `_bind()` does
        but without constructing a view. The ghost test reads the same place
        `Entity.is_ghost` reads -- the `.z` ghost bits of the cell at the
        entity's origin -- because a row carries no ghost flag of its own.
        """
        if resolved is None:
            return None
        row, channel = resolved
        ents = self.board.entities
        if not 0 < row < ents.shape[0] or not ents[row, Entity.F_ALIVE]:
            return None
        ox = int(ents[row, Entity.F_ORIGIN_X])
        oy = int(ents[row, Entity.F_ORIGIN_Y])
        grid = self.board.grid
        if 0 <= ox < self.board.width and 0 <= oy < self.board.height:
            if cell.is_ghost(grid[oy, ox, cell.CH_Z]):
                return None
        return row, channel

    def offer_to(self, seg):
        """What the gate upstream of `seg` wants to put on it, or None.

        BeltSim's producer callback. Returning a value does *not* commit to
        it -- BeltSim decides whether there is room and only then pushes, so
        this must not clear the waiting zone. `took_from()` does that once
        the push has actually happened.
        """
        port = self._live_port(self._src_cached(seg))
        if port is None:
            return None
        row, channel = port
        ents = self.board.entities
        if not ents[row, Entity.F_OUT_HELD + channel]:
            return None
        return float(ents[row, Entity.F_OUT_VALUE + channel])

    def took_from(self, seg):
        """Called after BeltSim actually pushed what offer_to() returned.

        Separate from `offer_to()` so that offering is not committing. Note
        that this is **defensive rather than load-bearing today**: BeltSim
        tests for room before it calls `offer()`, so the only way an offer
        gets refused is the gate having nothing to give, and clearing the
        zone inside `offer_to()` would behave identically. Nothing catches a
        mutation that does so -- `test_a_full_belt_is_never_even_offered_an_item`
        documents exactly that.

        It stays split because the protocol is then correct by construction
        rather than by where the room check happens to sit, and because a
        producer that can be asked speculatively -- a splitter choosing
        between two belts -- would make the distinction load-bearing at once.
        """
        port = self._live_port(self._src_cached(seg))
        if port is None:
            return
        row, channel = port
        ents = self.board.entities
        ents[row, Entity.F_OUT_VALUE + channel] = 0.0
        ents[row, Entity.F_OUT_HELD + channel] = 0.0
        self.board.entities_dirty.add(row)
        self.stats["drained"] += 1
        # A freed zone may be what was blocking this gate, so it has work
        # again. One set `discard` rather than the full `_wake_around()`:
        # this is the hottest code in the belt phase, and the belts on either
        # side are awake by construction here -- *this* callback only runs
        # from inside an awake segment's own tick.
        self._asleep.discard(row)

    def accept_from(self, seg, payload):
        """Can the gate downstream of `seg` take its head item?

        BeltSim's consumer callback. Writes the gate's input channel and
        returns True, or returns False to leave the item on the belt -- which
        is how a run backs up behind a gate that is not firing.
        """
        port = self._live_port(self._dst_cached(seg))
        if port is None:
            return False
        row, channel = port
        ents = self.board.entities
        if ents[row, Entity.F_IN_HELD + channel]:
            return False
        ents[row, Entity.F_IN_VALUE + channel] = float(payload)
        ents[row, Entity.F_IN_HELD + channel] = 1.0
        self.board.entities_dirty.add(row)
        self.stats["loaded"] += 1
        # A belt just fed this gate, which is the main reason a sleeping gate
        # has to wake: the belt layer is the only thing that can hand it
        # material without a gate event. Same one-`discard` reasoning as
        # `took_from()`.
        self._asleep.discard(row)
        return True

    def tick_belts(self, tick):
        """Run belt transport with the gate callbacks wired in.

        One BeltSim.tick(), so the report's advance-pop-push order holds. See
        the module docstring for why splitting this up is wrong.
        """
        self.belts.tick(tick, offer=self.offer_to, accept=self.accept_from,
                        took=self.took_from)

    # --- direct gate-to-gate ----------------------------------------------
    def deliver_direct(self):
        """Move waiting values between gates that touch with no belt between.

        Separate from the belt path because there is no segment to own the
        transfer. Runs after belt transport, so a gate feeding both a belt
        and a neighbour behaves the same either way: one tick per hop.

        Contention is not a concern even though several gates write inputs
        here -- a port face has exactly one neighbour, so each (gate,
        channel) pair has one upstream source and the writes are disjoint.
        """
        moved = 0
        for row in self._rows_holding_output():
            if not self.board.is_row_alive(row):
                continue
            gate = self.board.view(row)
            if gate.is_ghost:
                continue
            for (xy, face, channel) in self._output_faces(gate):
                if not gate.out_held(channel):
                    continue
                step = cell.DIR_STEPS[face]
                nx, ny = xy[0] + step[0], xy[1] + step[1]
                if not self.board.in_bounds(nx, ny):
                    continue
                # A belt there is the segment path's business, not ours.
                if self.index.at(nx, ny) is not None:
                    continue
                row = self.board.entity_id_at(nx, ny)
                if not row or not self.board.is_row_alive(row):
                    continue
                other = self.board.view(row)
                if other.row == gate.row or other.is_ghost:
                    continue
                back = (face + 2) % 4
                in_ch = self._input_channel_for(other, back, at=(nx, ny))
                if in_ch is None or other.in_held(in_ch):
                    continue
                other.set_input(in_ch, gate.out_value(channel))
                gate.clear_output(channel)
                # Both gates' channels changed, so the runs on either side of
                # either of them may have work again -- this is the same edge
                # the structure phase raises, reached by the no-belt path.
                self._wake_around(gate.row)
                self._wake_around(other.row)
                # **And the two gates themselves**, which the line above does
                # not do. `_wake_around(row)` wakes what is *adjacent to*
                # `row`, never `row` itself -- right for the structure phase
                # (a gate that just fired is plainly awake) and wrong here,
                # because this is the one path that writes *another* gate's
                # input channel directly. So the receiver has new material and
                # nothing has told it.
                #
                # This was a real hole, found by mutation-testing the sleep
                # predicate rather than the wake: `_wake_around(other.row)`
                # *reads* as "wake other", so the gap survived review. It was
                # masked by an earlier predicate that also required every
                # direct neighbour to be asleep -- which kept the receiver
                # awake for an unrelated reason and made the missing wake
                # invisible. Removing that redundant test is what exposed it.
                self._asleep.discard(gate.row)
                self._asleep.discard(other.row)
                moved += 1
        self.stats["direct"] += moved
        return moved

    # --- resolving a segment's ends to (gate, channel) --------------------
    #
    # Both ends resolve to a **(row, channel)** pair that is fixed for the
    # segment's whole life: `seg.src` / `seg.dst` are written once by
    # `SegmentIndex._resolve_ends()` during a rebuild and never touched
    # again, and the port geometry they are matched against cannot change
    # without a grid write. Yet `offer_to()` and `took_from()` each resolved
    # the source end per segment per tick, and `accept_from()` the sink end:
    # 1.80ms of a 2.19ms belt phase on a 672-segment board, 82% of it, for an
    # answer settled at the last edit.
    #
    # The cache therefore stores `(row, channel)` and **not the view**. A view
    # must never outlive the frame it was made in -- a row can be freed and
    # handed to an unrelated entity -- so caching one would be the exact
    # hazard `Orchestrator.inspect_row` documents. The view is rebuilt per
    # call from the cached row, which is cheap; what was expensive was the
    # face-by-face search that found the channel.
    #
    # `None` is a real cached answer (a dangling end), so a sentinel
    # distinguishes "resolved to nothing" from "not yet asked".
    _UNRESOLVED = object()

    def _src_cached(self, seg):
        """The cached (row, out_channel) feeding this segment, or None.

        The raw cached pair, with no view bound and no liveness test -- what
        the hot callbacks want, since they go on to read the row directly.
        `_src_port()` is the view-returning form for everything else.
        """
        hit = self._src_cache.get(seg.sid, self._UNRESOLVED)
        if hit is self._UNRESOLVED:
            hit = self._resolve_src(seg)
            self._src_cache[seg.sid] = hit
        return hit

    def _dst_cached(self, seg):
        """The cached (row, in_channel) this segment drains into, or None."""
        hit = self._dst_cache.get(seg.sid, self._UNRESOLVED)
        if hit is self._UNRESOLVED:
            hit = self._resolve_dst(seg)
            self._dst_cache[seg.sid] = hit
        return hit

    def _src_port(self, seg):
        """The (gate_view, out_channel) feeding this segment, or None.

        The view-returning form, kept for `_face_has_consumer()` and the
        tests: those compare gate identity rather than reading one float, so
        a view is the right currency there.
        """
        return self._bind(self._src_cached(seg))

    def _dst_port(self, seg):
        """The (gate_view, in_channel) this segment drains into, or None."""
        return self._bind(self._dst_cached(seg))

    def _bind(self, resolved):
        """A cached (row, channel) -> a live (view, channel), or None.

        The liveness and ghost tests stay here rather than in the resolver:
        they are about the entity's *current* state, and a ghost can be
        committed or despawned without the segmentation changing. Only the
        geometry -- which channel sits on which face -- is cached.
        """
        if resolved is None:
            return None
        row, channel = resolved
        if not self.board.is_row_alive(row):
            return None
        gate = self.board.view(row)
        if gate.is_ghost:
            return None
        return gate, channel

    def _resolve_src(self, seg):
        """The (row, out_channel) feeding this segment, or None. Uncached."""
        if seg.src is None:
            return None
        row, face = seg.src
        if not self.board.is_row_alive(row):
            return None
        gate = self.board.view(row)
        # Which output channel sits on that face, at the gate cell adjacent
        # to the segment's tail. The cell matters: a machine can emit on the
        # same facing from more than one cell of its footprint.
        #
        # `face` is the direction the gate's port *points*, i.e. from the
        # gate toward the belt -- so the gate cell is one step *backwards*
        # from the tail, not forwards.
        tail = seg.cells[0]
        step = cell.DIR_STEPS[face]
        want = (tail[0] - step[0], tail[1] - step[1])
        for (xy, f, channel) in self._output_faces(gate):
            if f == face and xy == want:
                return row, channel
        return None

    def _resolve_dst(self, seg):
        """The (row, in_channel) this segment drains into, or None. Uncached."""
        if seg.dst is None:
            return None
        row, face = seg.dst
        if not self.board.is_row_alive(row):
            return None
        gate = self.board.view(row)
        # As in _resolve_src(): `face` points from the gate toward the belt,
        # so the gate cell is one step *backwards* from the segment's head.
        head = seg.cells[-1]
        step = cell.DIR_STEPS[face]
        want = (head[0] - step[0], head[1] - step[1])
        channel = self._input_channel_for(gate, face, at=want)
        if channel is None:
            return None
        return row, channel

    # --- generators -------------------------------------------------------
    def _fire_generator(self, gate):
        """Fire a machine with no inputs. True if it produced anything.

        A generator's output channels are **independent**, which is the one
        thing still separating this from the arithmetic path: it fills
        whichever free zones it finds instead of demanding that *all* of them
        be clear. `outputs_clear()` over the connected set is all-or-nothing,
        so using it here would let one slow belt stall every other belt the
        same Source feeds -- the exact opposite of "fill both of them".

        Skipping dangling faces is no longer a difference -- every gate does
        that now (see the module docstring). But it still matters *more*
        here, and for the reason it always did: a generator creates material
        from nothing, so emitting into a dangling face would be a pure
        item-printing-and-shredding loop rather than a result being dropped.
        With **no** channel connected it stays inert, which is why a Source
        alone on the board does nothing at all rather than running flat out
        and discarding everything.
        """
        drains = self._connected_channels(gate)
        if not drains:
            # Nothing connected at all: stay inert rather than filling a zone
            # once and then counting as blocked forever.
            self.stats["unconnected"] += 1
            return False

        # Only the connected channels that are also free. Independent, so a
        # busy one does not hold up the rest.
        free = [c for c in drains if not gate.out_held(c)]
        if not free:
            self.stats["blocked"] += 1
            return False

        results = gate.fire([])
        produced = 0
        for channel in free:
            if channel >= len(results):
                continue
            value = results[channel]
            if value is None:
                continue
            gate.set_output(channel, value)
            produced += 1
        if not produced:
            return False
        self.stats["unconnected"] += len(
            [c for c in range(gate.output_count()) if c not in drains])
        return True

    # --- output connectivity ----------------------------------------------
    def _connected_channels(self, gate):
        """Which of `gate`'s output channels have somewhere to send a result.

        **Memoised for the tick.** The answer is a function of the grid and
        the segmentation, and the docstring below already argues that both are
        frozen for the whole phase -- the player cannot edit mid-tick. So the
        same gate asked twice in one tick must get the same answer, and
        computing it twice is waste: this was the single most expensive thing
        in the tick (2.2ms across 336 gates), because it walks every output
        face and probes the neighbouring cell, segment and entity row for
        each.

        The cache is cleared by `invalidate_connectivity()`, wired to the two
        things that can change the answer: a grid write and a segment
        rebuild. A stale entry would make a gate fire into a belt that is no
        longer there, or refuse to fire into one that now is.
        """
        hit = self._drains_cache.get(gate.row)
        if hit is None:
            hit = self._compute_connected_channels(gate)
            self._drains_cache[gate.row] = hit
        return hit

    def _compute_connected_channels(self, gate):
        """The uncached computation. See _connected_channels().

        A channel counts as connected if *any* of its faces has a genuine
        counterpart on the other side: a belt, or another gate whose own input
        face points back at us. Per channel rather than per face because a
        Sink-like fold maps several faces onto one slot, and draining any of
        them drains the channel. For every machine but Sink the two are the
        same thing, since channel == port index.

        **Every gate consults this**, which it did not always. It used to be
        a generator-only check, because a machine consuming inputs had to
        write every result or lose material. Destroying a result with nowhere
        to go is now the specified behaviour, so the same connectivity decides
        two things for every gate: which zones can block it
        (`outputs_clear(drains)`) and which results are discarded.

        **This reads the grid, which the structure phase otherwise must not
        do -- and it is safe for a specific reason.** The prohibition exists
        so a fire decision cannot depend on belt *state*, which the belt phase
        mutates mid-tick and whose read order would then matter. Connectivity
        is belt *presence*: a function of the grid alone, frozen for the whole
        phase because the player cannot edit mid-tick. So this stays
        order-independent, exactly like the port geometry beside it.
        """
        reached = set()
        for (xy, face, channel) in self._output_faces(gate):
            if channel in reached:
                continue
            if self._face_has_consumer(gate, xy, face):
                reached.add(channel)
        return reached

    def _face_has_consumer(self, gate, xy, face):
        """Would anything accept material pushed out of this port face?"""
        step = cell.DIR_STEPS[face]
        nx, ny = xy[0] + step[0], xy[1] + step[1]
        if not self.board.in_bounds(nx, ny):
            return False
        # A belt (or an underground end, which segments own) takes anything --
        # but only through the one port the segment actually named as its
        # source. A multi-port machine can have two faces touching the same
        # run, and a segment takes from exactly one of them (take_dir()); the
        # loser would otherwise fill a waiting zone nothing will ever drain
        # and sit there as a permanently stuck item.
        #
        # Asking _src_port() rather than comparing seg.src by hand is what
        # makes this exact: the face direction alone cannot tell two ports of
        # the same gate apart (a 2x1 Source has two +Y faces), and _src_port()
        # resolves the gate *cell* adjacent to the tail, which can.
        seg = self.index.at(nx, ny)
        if seg is not None:
            owner = self._src_port(seg)
            if owner is None:
                return False
            owner_gate, owner_channel = owner
            if owner_gate.row != gate.row:
                return False
            return owner_channel == self._channel_of_face(gate, xy, face)
        row = self.board.entity_id_at(nx, ny)
        if not row or not self.board.is_row_alive(row):
            return False
        other = self.board.view(row)
        if other.row == gate.row or other.is_ghost:
            return False
        # Another gate counts only if it has an input face pointing back at
        # us -- the same predicate the renderer's stubs use, so a drawn join
        # and a working one mean the same thing.
        back = (face + 2) % 4
        return self._input_channel_for(other, back, at=(nx, ny)) is not None

    def _channel_of_face(self, gate, xy, face):
        """Which output channel sits on this (cell, face) pair, or None."""
        for (oxy, oface, channel) in self._output_faces(gate):
            if oxy == xy and oface == face:
                return channel
        return None

    # --- port geometry ----------------------------------------------------
    def _output_faces(self, gate):
        """[(world_cell, face_dir, channel)] for each of a gate's outputs.

        Cached per gate row alongside the connectivity, and for the same
        reason: a placed machine's output faces are a function of its type,
        origin, facing and flip, none of which can change without a grid
        write -- an in-place turn restamps, which is what makes that true.
        So rebuilding this list for every gate on every tick, twice (the
        structure phase and `deliver_direct`), recomputed an answer fixed
        since the machine was placed.

        Shares `invalidate_connectivity()` rather than carrying its own
        invalidation hook, so there is one thing to call and one thing to
        forget rather than two.
        """
        hit = self._faces_cache.get(gate.row)
        if hit is None:
            ox, oy = gate.origin
            _ins, outs = gate.faces_for(ox, oy, gate.direction, gate.flipped)
            hit = tuple((xy, face, gate.out_channel(i))
                        for i, (xy, face) in enumerate(outs))
            self._faces_cache[gate.row] = hit
        return hit

    def _input_channel_for(self, gate, face, at=None):
        """Which input channel of `gate` accepts from `face`, or None.

        `face` is the direction, in world terms, that the port points. `at`
        narrows it to one cell of the footprint, which matters for a machine
        whose cells carry different ports -- without it, a Shear would accept
        its y input on either cell's west face.
        """
        ox, oy = gate.origin
        ins, _outs = gate.faces_for(ox, oy, gate.direction, gate.flipped)
        for i, (xy, f) in enumerate(ins):
            if f != face:
                continue
            if at is not None and xy != at:
                continue
            return gate.in_channel(i)
        return None

    # --- phase entry points -----------------------------------------------
    def set_tick(self, tick):
        """The tick the phases are computing. Set by the engine each tick.

        Also expires the connectivity cache when the tick number moves on.
        `invalidate_connectivity()` is the precise mechanism and is wired to
        every edit path; this is the backstop, and it is what makes the cache
        safe to reason about: an entry can never outlive the tick it was
        computed in, so the worst a missed invalidation can cost is the
        remainder of one tick rather than an indefinitely wrong answer.

        Cheap enough to do unconditionally -- one dict clear against the
        hundreds of neighbour probes it saves.
        """
        if tick != self._tick:
            self.invalidate_connectivity()
        self._tick = tick

    _tick = 0
