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


Constant folding
----------------

A **fully-direct Source** -- one whose output faces reach gates and nothing
else, with no belt anywhere on them -- emits the same number into the same
channels for ever. Nothing can change that but an edit. So it is not a
machine that needs ticking; it is a constant.

Folding it removes two per-tick costs. The Source stops being fired (on
cpu.npz, 133 of 285 Sources fold, and a generator fires *every* tick -- firing
is its steady state, so it is never idle enough for a prefilter or a sleep set
to skip), and the `deliver_direct()` walk that carried its value one hop also
goes.

**The fold is expressed as a permanently-held input channel**, written into
the receiving gate's row and re-asserted after each fire. That choice is what
keeps the mechanism small: `inputs_ready()`, `consumed()`, `fire()`, the
vectorised prefilter and the renderer all read `F_IN_HELD` / `F_IN_VALUE` and
keep working unchanged, with no folded-input branch anywhere. The alternative
-- overriding `inputs_ready()` and `consumed()` to treat some channels as
virtual -- would have put a fold test in every one of those readers, including
the numpy prefilter where a per-gate branch is exactly the cost being avoided.

Three things make it safe:

- **The channel is exclusively ours.** A port face has exactly one neighbour,
  so if a folded Source is on the far side of that face then no belt can
  `accept_from()` into it and no other gate can `deliver_direct()` into it.
  Nothing else writes the slot, so re-asserting it cannot clobber material.
- **Only a *fully*-direct Source folds.** One output on a belt and the machine
  is a real producer whose rate the belt regulates through back pressure; its
  value arrives one hop later and its zone can be held. Folding that would
  change behaviour. Other faces may dangle freely -- a dangling output is
  already discarded at fire time, so it was never carrying anything.

  **"On a belt" means a belt the face can actually deposit onto**, which is
  narrower than "a belt is adjacent" and was originally conflated with it.
  A segment takes from exactly one port (`take_dir()`), so a multi-port
  machine can have a face pressed against a run that named a *different*
  port as its source -- and that face can never push an item. It is a
  discard point exactly like an empty cell, so it must not block the fold.
  `_face_has_consumer()` is the authority, the same one `_connected_channels()`
  uses to decide which zones can block a fire; asking it here is what keeps
  the two rules from drifting.

  Treating adjacency as connection refused the fold on a large, entirely
  real population -- a Source tucked under a passing run. Measured:

                 folded before   after      tick before   after
    cpu.npz        133 of 285     242         8.58ms      6.38ms
    noram.npz      143 of 325     264        11.57ms      8.94ms
    Ram2.npz       108 of 188     157        13.24ms     12.23ms
    ram.npz         32 of  84      63         3.85ms      3.01ms

  Nearly a doubling of the folded population, because the losing-face shape
  is the common one on a board dense enough for runs to pass over machines.
- **It is derived state with the same invalidation condition as the four
  caches above**, so it is dropped by `invalidate_connectivity()` and nothing
  new has to be remembered on an edit.

The visible consequence is a **one-tick latency difference**: a folded input
is present from the first tick rather than arriving one hop later. For a
constant that is the same value either way, so no arithmetic changes -- but a
chain's start-up is one tick earlier, which is why the tests assert on values
rather than on exact tick numbers at the boundary.
"""

import numpy as np

from core import cell
from core.entity import MAX_CHANNELS, Entity, Source
from core.gate_state import GateState
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
        #: The gate-dense SoA mirror of the channel columns. Rebuilt by
        #: `_gate_table()` from the same walk, so the two cannot disagree
        #: about the population. See core/gate_state.py.
        self.state = GateState(board)
        #: The compute-shader form of the structure phase, or None when
        #: there is no GL context (every headless path, and the app before
        #: `create_device_resources`). **None is the normal state**, not a
        #: degraded one: the CPU phase is the reference implementation and
        #: stays the one the tick calls -- the device exists to be compared
        #: against it (see `gate_device.compare_against_cpu`) until it is
        #: carrying the work.
        self.device = None
        #: The belt phase's compute form, beside the gate one. Also None
        #: without a context, and also additive: `tick_belts()` is still the
        #: reference and what `belt_compute.compare_against_cpu()` checks
        #: against.
        self.belt_compute = None
        #: The gate classes, parallel to `state.rows`. Kept because the
        #: device's geometry packing needs the *class* and views are not
        #: safe to hold across frames.
        self._gate_types = []
        #: (downstream, upstream) gate row -> segments, or None. See
        #: _segments_of_gate().
        self._gate_segments = None
        #: (row, channel) -> feeding segment, or None. See _feeders().
        self._feeder_map = None
        #: (row, channel) -> (upstream row, out channel), or None. The
        #: belt-less counterpart of `_feeder_map`. See _direct_feeders().
        self._direct_map = None
        #: (src_row, src_chan, dst_row, dst_chan) arrays, or None. The
        #: forward form of `_direct_map`, and what `deliver_direct()`
        #: iterates. See _direct_edges().
        self._direct_edges_cache = None
        #: upstream row -> [downstream rows], the forward direction of
        #: `_direct_map`, or None. See _direct_downstream().
        self._direct_down = None
        #: Constant folding, or None when not yet derived. See _folds().
        #: `folded_sources` is the set of Source rows that no longer tick;
        #: `folded_inputs` is [(row, channel, value)] to hold permanently.
        self._folding = None
        #: False disables constant folding outright, for
        #: `tools/check_folding_safe.py`. See set_folding_enabled().
        self._folding_enabled = True
        #: Gate rows currently at rest. See the "sleeping gates" block below.
        #: Empty means "every gate is visited", the pre-sleep behaviour and so
        #: the safe default after any wholesale change.
        self._asleep = set()
        #: **Gate sleeping is a measured net loss, and this flag is what
        #: switches it off.** It is correct -- `tools/check_wakelist_safe.py`
        #: agrees at every tick, with both tiers exercised -- and it still
        #: costs more than it saves on every real board:
        #:
        #:                 baseline   folding   folding+gate sleep
        #:   cpu.npz        9.13ms     7.17ms      7.66ms
        #:   noram.npz     12.95ms    10.98ms     11.29ms
        #:   Ram2.npz      13.12ms    12.29ms     12.40ms
        #:   ram.npz        3.43ms     2.93ms      3.10ms
        #:
        #: Re-measured after the folding fix above widened the fold (so there
        #: are fewer gates left for a scheduler to skip), and the verdict is
        #: unchanged -- sleep is still the slower column:
        #:
        #:                 folding   folding+gate sleep
        #:   cpu.npz        6.15ms      6.38ms
        #:   noram.npz      8.49ms      8.94ms
        #:   Ram2.npz      11.30ms     12.23ms
        #:   ram.npz        2.88ms      3.01ms
        #:
        #: It genuinely removes work -- `structure/fire` falls 4.06 -> 3.35ms
        #: on cpu.npz with 207 of 537 gates asleep -- and the bookkeeping to
        #: decide *which* 207 costs more than that: `structure/prefilter`
        #: rises 1.35 -> 2.32ms.
        #:
        #: The reason is the workload, not the implementation, and three
        #: attempts at the accounting establish it. The sleep set is big and
        #: stable in *size* but never still: on cpu.npz 203 gates are asleep
        #: yet 6 change state every tick, with only 1 tick in 100 unchanged
        #: (Ram2: 495 asleep, 11.3 churn, 0 ticks unchanged). So the awake
        #: slice cannot be memoised -- measured at **0 cache hits in 150
        #: ticks** -- and the per-gate tier-two test runs ~186 times a tick
        #: however it is narrowed. A running factory simply does not hold
        #: still long enough for a gate scheduler to amortise.
        #:
        #: Kept rather than deleted, because the mechanism is the part that
        #: was hard: the predicate, the wake edges, the feeder index and the
        #: differential check all work and are all tested. What is missing is
        #: a representation in which "which gates are awake" is cheap to
        #: maintain under churn -- most likely the same compacted index buffer
        #: the GPU port wants, where the set is rebuilt by a parallel
        #: predicate pass rather than edited incrementally. Flip this to True
        #: to measure it again.
        self._sleep_enabled = True
        #: Bumped by every mutation of `_asleep`, so the awake slice below can
        #: be memoised across ticks. See _awake_slice() / _sleep_changed().
        self._sleep_gen = 0
        #: ((table id, sleep gen), sliced table) or None. See _awake_slice().
        self._awake_cache = None
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
        self._gate_segments = None
        # The feeder map is the inverse of `_dst_cache`, so it dies with it --
        # and it must, because it holds segment *objects* and a rebuild
        # recycles segment ids. A stale entry would have a gate sleeping on a
        # run that no longer exists.
        self._feeder_map = None
        # **The direct-feeder index is deliberately NOT dropped here**, for
        # exactly the reason the constant fold below is not, and it paid the
        # same price before being moved: `set_tick()` calls this once per tick
        # as a backstop, and the index is a whole-board walk over every gate's
        # output faces. Measured when it was wired here: **58 rebuilds over 50
        # ticks** on cpu.npz -- more than once a tick -- and the tick went
        # 6.83 -> 12.27ms. An edit-time derivation dropped per tick is a
        # per-tick cost, which is the opposite of the point.
        #
        # It is dropped by `invalidate_board()` instead, the edit-time entry
        # point every real edit routes through. The recycling hazard the four
        # caches above document still applies -- it holds *rows*, which the
        # free list reuses, and it is derived from port geometry an in-place
        # turn changes -- so it must die on an edit; it just must not die on a
        # tick.
        # **The gate table is NOT dropped here either, for the same reason and
        # at the same price.** This is the third derivation to make the
        # mistake, after the direct-feeder index above and the constant fold
        # below, and it survived longest because it is *correct* -- merely
        # recomputed. `_gate_table()` is `(rows, n_in, n_out, strict)`, all
        # four functions of which rows hold which type, which nothing but an
        # edit changes; but it is built by walking `gates()` and constructing
        # a view per row, which is the per-gate Python the prefilter exists to
        # avoid, paid once a tick to rebuild the list of who to avoid it for.
        #
        # Measured at exactly one rebuild per tick on every real board, and it
        # is the single largest line in the profile after `fire`:
        #
        #                 rows   per rebuild   share of tick
        #   cpu.npz        450      1.20ms         20%
        #   noram.npz      617      1.54ms         19%
        #   Ram2.npz       732      2.06ms         19%
        #   ram.npz        197      0.64ms         23%
        #
        # The docstring above calls this method "cheap enough to do
        # unconditionally -- one dict clear", which is true of the four caches
        # it was written for and false of everything added since. A whole-board
        # walk hidden behind a line that reads as a clear is how all three of
        # these got here, so the rule is now stated rather than implied: this
        # method may only drop things whose rebuild is O(1) or lazy per entry.
        # **Constant folding is deliberately NOT dropped here**, which is the
        # one exception among the derivations above and needs stating because
        # it looks like an omission.
        #
        # This method has two callers with different meanings. A real edit
        # (`Engine`'s grid hooks, a segment rebuild) genuinely changes what is
        # on the board. But `set_tick()` also calls it once per tick as a
        # *backstop*, so that a cache entry can never outlive the tick it was
        # computed in -- and the fold must survive that one, for two reasons:
        #
        #   - **Cost.** Re-deriving the fold is a whole-board walk over every
        #     gate's output faces. Dropping it per tick made it a per-tick
        #     cost, which is precisely the per-tick cost folding exists to
        #     remove: measured at 10 derivations over 5 ticks, i.e. twice a
        #     tick, turning an edit-time optimisation into a tick-time
        #     pessimisation.
        #   - **Correctness.** `_drop_folding()` *releases* the channels it
        #     was holding, so a per-tick drop briefly unfeeds every folded
        #     gate. It happened to be invisible only because the re-derivation
        #     re-asserted them within the same call chain -- a coincidence of
        #     ordering, not a guarantee.
        #
        # `invalidate_folding()` is the explicit form, called from the edit
        # paths. See `set_tick()` for the other half of this split.
        # The gate sleep set is an *answer derived from* that adjacency -- "my
        # neighbours are quiet" -- so dropping the adjacency without dropping
        # the conclusion would leave gates asleep on a claim about a board
        # that no longer exists. Same recycling hazard the four caches above
        # document, with a silent-freeze consequence instead of a wrong-port
        # one: rows are recycled by the free list, so a new machine could
        # inherit the previous occupant's sleep.
        self._wake_rows(tuple(self._asleep))

    def invalidate_board(self):
        """A real edit: drop the per-tick caches **and** the constant fold.

        The edit-time entry point, as against `invalidate_connectivity()`'s
        per-tick backstop role. See the comment there for why the fold is
        excluded from that one; everything that genuinely changes the board
        must call this instead, and `Engine` routes its grid hook and its
        segment rebuild here.
        """
        self.invalidate_connectivity()
        self._drop_folding()
        # The direct-feeder index and its inverse, for the same reason the
        # fold is here rather than in `invalidate_connectivity()`: both are
        # whole-board walks that must survive the per-tick backstop and die on
        # a real edit. The two directions go together, or the wake edge would
        # point at the old adjacency while the sleep predicate used the new
        # one -- and the two disagreeing is exactly the silent-stall shape.
        self._direct_map = None
        self._direct_down = None
        # The edge list comes out of the same walk as `_direct_map`, so the
        # two must die together or `deliver_direct()` would move material
        # along the old adjacency while the sleep predicate consulted the new
        # one -- the silent-stall shape named just above, from the other side.
        self._direct_edges_cache = None
        # The gate table excludes folded rows, so dropping the fold without
        # dropping the table would leave a folded Source missing from the
        # firing set with nothing holding its constant.
        self._gate_table_cache = None

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

        **A folded Source is left out of the table entirely**, which is what
        makes constant folding a saving rather than just a different spelling.
        Excluding it here rather than in the two consumers means the firing
        prefilter and `_rows_holding_output()` both drop it from one change,
        and the exclusion is paid once per edit instead of once per tick. A
        folded Source can hold nothing in a waiting zone anyway -- it never
        fires -- so `deliver_direct()` loses nothing by not seeing it.
        """
        if self._gate_table_cache is not None:
            return self._gate_table_cache

        folded, _inputs = self._folds()
        rows, n_in, n_out, strict = [], [], [], []
        type_id, classes = [], []
        for gate in self.gates():
            if gate.row in folded:
                continue
            cls = type(gate)
            classes.append(cls)
            rows.append(gate.row)
            n_in.append(gate.input_count())
            n_out.append(gate.output_count())
            type_id.append(cls.TYPE_ID)
            # FIRE_ANY needs one fed channel; FIRE_ALL needs all of them.
            # FIRE_SELECT (and any type overriding inputs_ready()) is not
            # expressible as an occupancy count, so it is never filtered out.
            strict.append(cls.FIRE_RULE == cls.FIRE_ALL)

        # The SoA mirror is built from the same walk, so it cannot disagree
        # with the prefilter about which rows are gates -- the same
        # one-authority argument `_build_direct()` settles for adjacency.
        self.state.build(rows, n_in, n_out, type_id)
        self._gate_types = classes

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
        all_rows, all_n_in, all_n_out, all_strict = self._gate_table()
        if not len(all_rows):
            return [], 0, 0

        # **Load the SoA mirror here, not in `structure_phase()`.** The
        # mirror is authoritative only for the span between this load and the
        # next write to `Gameboard.entities` (see core/gate_state.py), and
        # putting the load in the caller let the two come apart: every
        # in-tree caller went through `structure_phase()`, but
        # `tools/check_prefilter_safe.py` calls this directly and so read a
        # mirror one tick stale. It reported an excluded Sink that the full
        # path would have fired -- which is exactly the "conservative"
        # property the prefilter must never lose, surfacing as a safety
        # failure rather than as a wrong number.
        #
        # Keeping the load adjacent to the only read is what makes that
        # unrepresentable: there is no way to reach the gather below without
        # having refreshed first.
        self.state.sync_from_rows()

        # **The sleep set slices the table, and it has to.** For a long time
        # it did not: every vectorised quantity below was computed over the
        # whole table and the sleep set was updated afterwards, so sleeping
        # *added* bookkeeping and removed no work from this pass. Measured on
        # cpu.npz with 207 of 537 gates asleep: `fire` fell 4.06 -> 3.35ms
        # (the sleep set working) while `prefilter` rose 1.35 -> 2.32ms (the
        # bookkeeping), for a net 3% *loss*. The mechanism was sound and the
        # accounting was not.
        #
        # Slicing first is the same lesson `tools/probe_batch_prefilter.py`
        # already recorded one layer down -- sorting rows buys ~15% while
        # slicing buys 60x -- and it is what makes a sleeping gate cost
        # nothing rather than cost less.
        #
        # `starved` and `blocked` are the one thing that cannot be sliced
        # away: they are observable stats that three tests and the panel read,
        # and a sleeping gate is still starved or blocked. So the skipped
        # rows' contribution is credited from the *reason they slept*, which
        # is known without re-deriving it -- see `_sleeping_stats`.
        # **The slice is memoised against the sleep set's identity**, not
        # recomputed per tick, and that is the difference between a win and a
        # loss. The obvious version -- `np.isin(all_rows, sleeping)` every
        # tick -- is O(table) work to avoid O(awake) work, and since the awake
        # set is ~40% of the table on cpu.npz it loses: prefilter went 2.32 ->
        # 2.69ms and the tick 7.08 -> 8.17ms, *worse* than not slicing at all.
        #
        # A sleep set changes only when a gate sleeps or an edge wakes one, so
        # a generation counter bumped at those points is enough to reuse the
        # slice across every tick in between -- which on a quiet board is all
        # of them. That is the same reasoning as every other cache here: the
        # answer is a function of something that changes on an event, so it is
        # computed on the event and not on the tick.
        rows, n_in, n_out, strict = self._awake_slice(
            all_rows, all_n_in, all_n_out, all_strict)
        if not len(rows):
            # Everything is asleep, which is the whole point on a quiet
            # board. The stats still have to be credited.
            s, b = self._sleeping_stats()
            return [], s, b

        # **Read the SoA mirror, not the entity table.** The old form was
        # `entities[rows]`, a fancy-index gather materialising a full
        # 28-float row per gate to read the 8 channel columns this pass
        # actually uses. Here each quantity is one contiguous
        # `(gates, MAX_CHANNELS)` block, so the read is dense in exactly the
        # values being tested -- which is the whole argument for the mirror
        # (see core/gate_state.py) and the shape a coalesced GPU read needs.
        state = self.state
        slots = state.slot_of[rows]
        held = state.in_held[slots] > 0.0
        free = state.out_held[slots] <= 0.0

        # Mask the channel columns a type does not declare, so an unused slot
        # cannot be mistaken for an unfed one (or a free zone). Precomputed
        # per edit on the mirror rather than rebuilt from `arange` each tick.
        in_used = state.in_used[slots]
        out_used = state.out_used[slots]

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
        # The sleeping rows were not examined above, but they are still
        # starved or blocked and the stats must say so -- see the slicing
        # comment. Credited from the recorded reason rather than re-derived,
        # which would mean reading the rows this pass exists to skip.
        sleep_starved, sleep_blocked = self._sleeping_stats()
        starved += sleep_starved
        blocked += sleep_blocked
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
        # **Entirely vectorised, and it has to be.** The first version walked
        # the rejected rows in Python and asked a per-gate predicate, which on
        # a real board (cpu.npz, 670 gates) took the prefilter from 1.40ms to
        # 6.81ms a tick -- a 4.9x *regression* on the very pass whose whole
        # purpose is to avoid per-gate Python. The sleep set is supposed to
        # remove work, and a scheduler that costs more than the work it skips
        # is worse than no scheduler. Measured, not assumed: this is exactly
        # what `tools/probe_batch_prefilter.py` already recorded about slicing
        # versus looping, re-learned one layer up.
        #
        # The predicate is "holds nothing on any declared channel", which is
        # two reductions over masks already computed above. Nothing per-gate
        # survives.
        holds_in = (held & in_used).any(axis=1)
        holds_out = ((~free) & out_used).any(axis=1)
        inert = ~(holds_in | holds_out)

        awake = ready & has_room
        asleep = self._asleep
        # Gates to put to sleep: inert, and not admitted. An admitted gate is
        # one the phase is about to run, so sleeping it would skip real work.
        # (With this predicate the two are nearly disjoint anyway -- a gate
        # holding nothing is starved unless it has no inputs at all -- but
        # `ready` is True for a generator, which holds nothing and must still
        # fire, so the exclusion is load-bearing for exactly that case.)
        to_sleep = inert & ~awake

        # --- tier two: a partially-fed gate whose missing lanes cannot fill --
        #
        # The predicate above only sleeps a gate holding *nothing*, which
        # leaves the common case awake (211 of 537 rows on cpu.npz). This tier
        # adds the gate waiting on a lane whose belt is empty and at rest; see
        # the "sleeping a partially-fed gate" block for the argument.
        #
        # **The candidate set is narrowed in numpy first, and that is what
        # makes a per-gate test affordable here.** The lesson from the 4.9x
        # regression above is not "never loop" but "never loop over the whole
        # table": the rows reaching the Python test below hold an input, hold
        # no output, and are not admitted. Sleeping rows are already gone --
        # the table was sliced by the sleep set at the top -- so a gate is
        # tested once on the tick it goes quiet and then costs nothing until
        # something wakes it. The steady-state cost is the gates that keep
        # waking, not the population.
        #
        # Output-holding rows are excluded for the same reason tier one
        # excludes them: a waiting zone drains by somebody else's event. It is
        # also what makes `_sleeping_stats()` exact.
        # Both tiers collect into one list and sleep in a single call, so the
        # generation is bumped once per tick rather than once per gate.
        newly_asleep = []
        maybe = holds_in & ~holds_out & ~awake & ~to_sleep
        if maybe.any():
            n_in_list = n_in.tolist()
            for i in np.flatnonzero(maybe).tolist():
                if self._starved_on_sleeping_belts(
                        int(rows[i]), n_in_list[i], held[i]):
                    newly_asleep.append(int(rows[i]))
        # **There is no longer a vectorised wake backstop, and its removal is
        # forced rather than chosen.** It used to read `to_wake = ~inert`:
        # anything holding something must be awake, since a tier-one sleeper
        # holds nothing. Two separate things killed it.
        #
        # Tier two made it *wrong as a backstop*: a partially-fed gate sleeps
        # while holding an input, so `~inert` would have woken every tier-two
        # sleeper the tick after it slept, making the whole tier a no-op --
        # not incorrect, merely useless, which is worse, because the
        # measurements would have shown no gain with no visible reason.
        #
        # Slicing the table by the sleep set made it *impossible*: the rows
        # reaching this point are the awake ones, so nothing here can observe
        # a sleeping gate at all, let alone wake it. That is the price of the
        # slice, and it is the right trade -- a backstop that costs a
        # whole-table pass to guard against a missed edge is the same bargain
        # polling would be.
        #
        # So the explicit edges carry the whole weight now:
        #
        #   accept_from()     a belt loads an input   -> discards the row
        #   took_from()       a belt drains a zone    -> discards the row
        #   deliver_direct()  a neighbour hands over  -> discards both rows
        #   _wake_around()    a feeder stops resting  -> discards downstream
        #   flush_material()  a wholesale reset       -> wake_all_gates()
        #   invalidate_board() an edit                -> clears the set
        #
        # That list is closed, and `tools/check_wakelist_safe.py` is what
        # holds it to account: `lying_sleepers()` fails the run if any
        # sleeping gate's own predicate has stopped holding, which is exactly
        # the state a missed edge produces and the thing the backstop used to
        # paper over. Measured: 0 violations with the `_wake_around()` edge,
        # 4 without it.
        newly_asleep.extend(rows[to_sleep].tolist())
        self._sleep_rows(newly_asleep)
        self.stats["gates_asleep"] = len(asleep)

        # **`admit` needs no filtering against the sleep set.** The table was
        # already sliced to the awake rows above, and both tiers exclude
        # `awake`, so a row cannot be both admitted and asleep.
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

        **It does, however, wake the gate at the far end of each downstream
        run**, which is the wake edge tier-two gate sleep rests on. A gate
        sleeping because its input belt was empty and at rest has to wake when
        that belt can deliver again, and the moment that becomes true is
        exactly this one: this gate has just filled a waiting zone, so the run
        it feeds is no longer at rest. Waking the run alone is not enough --
        the run will carry the item along and call `accept_from()`, which
        wakes the receiver, but only `d/v` ticks later; until then the
        receiver is asleep on a claim ("my feeder is at rest") that stopped
        being true on this tick.

        Nothing *breaks* without it, since `accept_from()` is the edge that
        matters for delivery -- but the receiver would spend the transit
        asleep while its own predicate was false, and a sleep set that
        disagrees with its own predicate is the state every checker here is
        designed to catch.
        """
        downstream, upstream = self._segments_of_gate(row)
        belts = self.belts
        asleep = self._asleep
        for seg in downstream:
            belts.wake(seg)
            # The gate this run drains into, if any. `_dst_cached()` is the
            # same resolution the forward path uses, so the two cannot
            # disagree about who is downstream of what.
            dst = self._dst_cached(seg)
            if dst is not None:
                self._wake_one(dst[0])
        for seg in upstream:
            belts.wake(seg)
        # **And the gates directly downstream, with no belt between** -- the
        # companion to the `dst` wake above, for tier two's gate-to-gate
        # clause: a neighbour sleeping because *our* output zone was empty
        # should wake the moment we fill it, which is now.
        #
        # **This one is defensive rather than load-bearing, and measurement
        # says so** -- recorded because the belt analogue above *is*
        # load-bearing (0 violations with it, 4 without) and the symmetry
        # invites assuming the same here. It is not the same, because the
        # backstop sits one phase away rather than `d/v` ticks away:
        #
        #   this fire is in the **structure** phase;
        #   `deliver_direct()` runs in the **belt** phase of the same tick,
        #   moves the value, and `_wake_one()`s both rows itself.
        #
        # So the receiver is awake again before the tick ends either way, and
        # it is never asleep on a false premise *across a tick boundary* --
        # which is the only thing `lying_sleepers()` can sample, since it runs
        # after the full tick. Mutation-tested: removing these two lines
        # passes the whole differential tool, and instrumenting shows
        # `deliver_direct()` picking up exactly the slack -- **35 wakes, equal
        # to the 35 grants the direct clause made**.
        #
        # Kept for the same reason `took_from()`'s split is kept: the
        # scheduler should be correct because each cause raises its own edge,
        # not because another subsystem happens to run later in the same tick.
        # A future reordering of the phases, or a direct delivery that became
        # conditional, would make it load-bearing with no other change.
        for down_row in self._direct_downstream(row):
            self._wake_one(down_row)

    def _direct_downstream(self, row):
        """Gate rows this gate feeds directly, with no belt between.

        The forward direction of `_direct_feeders()`, inverted once per edit
        and cached beside it. A per-call walk of the output faces would be
        the O(faces) scan on every fire that the indexes exist to avoid --
        and `_wake_around()` runs for every gate that fires, which is the
        hottest caller there is for this.
        """
        if self._direct_down is None:
            down = {}
            for (dst_row, _ch), (src_row, _c) in \
                    self._direct_feeders().items():
                down.setdefault(src_row, []).append(dst_row)
            self._direct_down = down
        return self._direct_down.get(row, ())

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

    # --- sleeping a partially-fed gate ------------------------------------
    #
    # The predicate above sleeps a gate only when it holds *nothing*. That is
    # sound but leaves the common case awake: a gate with some inputs and not
    # all of them, waiting on a lane whose belt is empty and asleep. Measured
    # at the end of 60 ticks, after constant folding:
    #
    #            partially fed   of those, provably stuck
    #   cpu.npz        211                123
    #   noram.npz      319                186
    #   Ram2.npz       239                163
    #
    # So this roughly doubles the sleeping population, and it is the shape a
    # computer actually has: a gate part-way through assembling its operands,
    # waiting on a memory read that is not coming this tick.
    #
    # **It is a proof, not a prediction.** The claim is narrow: if every
    # *unfed* declared input channel is fed by a belt segment that is both
    # **empty** and **at rest**, then no material can reach this gate's
    # missing lanes, so its readiness cannot change -- whatever its fire rule
    # is. Three things have to hold, and each is checked rather than assumed:
    #
    #   - **empty**, not merely asleep. A sleeping run that *holds* items is
    #     the RAM fixed point, and its head is at the end waiting to be taken
    #     -- by this very gate. Sleeping on that would be the deadlock.
    #   - **at rest**, not merely empty. An empty run with an unsaturated
    #     anchor, or one whose producer is loaded, will deliver shortly.
    #     `BeltSim.is_at_rest()` is the single authority and is already
    #     brute-force checked against its own definition by
    #     `tools/probe_wakelist.py`.
    #   - **every** unfed channel, and a channel with no segment at all
    #     disqualifies the gate. A directly-fed lane can be written by
    #     `deliver_direct()` with no belt involved, and a dangling lane is
    #     never fed by anything -- but both are decided by connectivity, which
    #     is the Python work being skipped, so the conservative answer is to
    #     stay awake.
    #
    # **The wake edges already cover it**, which is why this needs no new
    # mechanism. A sleeping belt cannot deliver, and the moment it can it was
    # woken by `_wake_around()` from the gate upstream of it -- and the same
    # call wakes the segments either side of this gate. The one edge that is
    # genuinely new is the belt waking *this* gate, and `accept_from()`
    # already discards the row it loads.
    #
    # The feeder map is built per **edit**, not per tick: it is the inverse of
    # `_dst_cache` and changes only when the segmentation does. Deriving it
    # per tick would be the O(gates x segments) scan the wake list exists to
    # avoid -- the probe that produced the numbers above did exactly that and
    # took longer than the phase it was measuring.
    def _feeders(self):
        """(row, channel) -> the segment id feeding it, built per edit.

        The inverse of `_dst_cached()`, over every segment at once. Shares
        `_gate_segments`' invalidation because it is derived from the same
        `seg.dst` resolution.
        """
        if self._feeder_map is None:
            feeders = {}
            for seg in self.index.segments.values():
                dst = self._dst_cached(seg)
                if dst is not None:
                    # One segment per (row, channel): a port face has exactly
                    # one neighbour, so a second would be a resolution bug.
                    feeders[dst] = seg
            self._feeder_map = feeders
        return self._feeder_map

    # --- the direct feeder index ------------------------------------------
    #
    # The belt feeder map above answers "which run fills this lane". This is
    # the same question for the lanes no run fills: a gate abutting another
    # with no belt between them, which `deliver_direct()` serves.
    #
    # **It is the dominant gap in tier two, by a wide margin.** Measured on
    # cpu.npz after 60 ticks, of the 95 awake partially-fed gates, tier two
    # refused 64 for `seg is None` -- and classifying those 64 shows **61 are
    # direct gate-to-gate lanes** and only 3 genuinely dangle. On a computer
    # most short hops are direct rather than belted, so the belt-only
    # predicate was blind to the common shape.
    #
    # Same invalidation as `_feeders()` and for the same reason: it is derived
    # from the grid and the port geometry, both of which change only on an
    # edit, and it holds *rows*, which the free list recycles.
    def _direct_feeders(self):
        """(row, channel) -> (upstream row, upstream out channel), per edit.

        The inverse of `deliver_direct()`'s neighbour walk: for each gate
        input lane with no belt on it, which gate's output face points back.

        Built for every gate at once rather than per candidate, because a
        per-gate version is the O(gates x faces) scan per tick that the wake
        list exists to avoid -- the same argument `_feeders()` records.

        **Keyed on the receiving lane, not the producing face**, so the
        lookup below is one dict hit per missing lane with no search. A lane
        has exactly one upstream source (a port face has exactly one
        neighbour), which is what makes that key unique.

        Derived from `_direct_edges()` rather than from its own grid walk, so
        the map and the edge list `deliver_direct()` iterates cannot disagree
        about who feeds whom -- the same one-authority argument
        `_face_has_consumer()` settles for connectivity.
        """
        if self._direct_map is None:
            self._build_direct()
        return self._direct_map

    # --- the direct edge list ---------------------------------------------
    #
    # `deliver_direct()` used to rediscover the gate-to-gate adjacency every
    # tick: for every gate holding an output, walk its faces, probe the
    # neighbouring cell, build a view, ask which input channel faces back.
    # That is a grid-shaped search for an answer fixed at the last edit, and
    # it measured 5.7-12.6% of the tick -- the largest line in the belt phase.
    #
    # It is also the one place in the tick that **writes another entity's
    # row**, which makes its shape a correctness question and not only a
    # speed one. Expressed as a search, the disjointness argument is a claim
    # about geometry ("a port face has exactly one neighbour") that has to be
    # re-established by reading the loop. Expressed as an edge list, it is a
    # property of the data: one entry per receiving lane, so a kernel with one
    # thread per edge writes disjoint slots by construction.
    #
    # That is the GPU shape. The transfer becomes one dispatch over a flat
    # array with no neighbour probing, no view construction and no grid reads
    # at all -- every term resolved once per edit and uploaded.
    #
    # Four parallel arrays rather than a list of tuples, for the same reason
    # `BeltDevice` keeps two parallel vectors rather than a packed struct:
    # this is what gets uploaded, and SoA is both the coherent layout and the
    # one with no alignment ambiguity.
    def _direct_edges(self):
        """The gate-to-gate transfers, as four parallel arrays.

        `(src_row, src_chan, dst_row, dst_chan)`, one entry per receiving
        lane. Everything `deliver_direct()` needs to move a value, with no
        geometry left to resolve.

        Derived once per edit. Both this and `_direct_feeders()`' inverse map
        come out of a single walk, so there is one authority on the adjacency
        and one thing to invalidate.
        """
        if self._direct_edges_cache is None:
            self._build_direct()
        return self._direct_edges_cache

    def _build_direct(self):
        """Walk the board once and build both direct-adjacency derivations.

        The walk is the expensive half (a view and a neighbour probe per
        output face), so it is paid once and feeds both the forward edge list
        and the reverse lane map.
        """
        src_row, src_ch, dst_row, dst_ch = [], [], [], []
        direct = {}
        for gate in self.gates():
            for (xy, face, channel) in self._output_faces(gate):
                step = cell.DIR_STEPS[face]
                nx, ny = xy[0] + step[0], xy[1] + step[1]
                if not self.board.in_bounds(nx, ny):
                    continue
                # A belt there is the segment path's business -- that lane
                # is in `_feeders()`, not this map. Mirrors the identical
                # skip the old `deliver_direct()` walk made.
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
                if in_ch is None:
                    continue
                direct[(other.row, in_ch)] = (gate.row, channel)
                src_row.append(gate.row)
                src_ch.append(channel)
                dst_row.append(other.row)
                dst_ch.append(in_ch)
        self._direct_map = direct
        self._direct_edges_cache = (
            np.asarray(src_row, dtype=np.int64),
            np.asarray(src_ch, dtype=np.int64),
            np.asarray(dst_row, dtype=np.int64),
            np.asarray(dst_ch, dtype=np.int64),
        )

    def _starved_on_sleeping_belts(self, row, n_in, held_in):
        """Is every unfed input of this gate fed by an empty, resting run?

        The partially-fed sleep test. See the block comment above for the
        argument; this is only the mechanics.

        Returns False the moment any unfed channel could still be fed, which
        is the conservative direction: a wrong False costs a tick of work, a
        wrong True freezes the gate for ever.
        """
        feeders = self._feeders()
        belts = self.belts
        tick = self._tick
        for ch in range(n_in):
            if held_in[ch]:
                continue
            seg = feeders.get((row, ch))
            if seg is None:
                # No belt feeds this lane, so it is fed by a direct neighbour
                # or it dangles. Only the first can grant a sleep, and only
                # while that neighbour's output zone is empty -- a dangling
                # lane is refused, which is the opposite of what it looks
                # like it should be. See `_direct_lane_is_quiet()`.
                if not self._direct_lane_is_quiet(row, ch):
                    return False
                continue
            if seg.tail != seg.head:
                # Holding items -- the RAM fixed point, whose head sits at the
                # end waiting to be taken by this very gate. Sleeping on that
                # would be the deadlock the block comment names.
                #
                # **Redundant given the `is_at_rest()` test below, and kept
                # anyway.** Mutation-testing shows removing it passes, and the
                # reason is structural rather than lucky: a run at rest
                # holding items has its head at the end and no room, and the
                # belt tick that put it there had already called
                # `accept_from()` -- so the lane it feeds is *held*, and a
                # held lane never reaches this loop. Brute-forced over run
                # lengths 1-5 and 120 ticks each: zero instances of
                # (non-empty, at rest, downstream lane unfed).
                #
                # It stays because that argument runs through two other
                # objects' invariants, and the cost of being wrong is a
                # silent permanent stall. One integer comparison is the right
                # price for not having to re-derive it.
                return False
            if not belts.is_at_rest(seg, tick):
                # **Also redundant today, and for a reason worth recording
                # rather than rediscovering.** By this line the run is known
                # *empty*, and for an empty run `is_at_rest()` adds exactly
                # one thing: that the anchor has saturated. That cannot
                # matter to this gate -- an empty run delivers nothing
                # whatever its anchor, because delivery needs a push and a
                # push needs the producer to offer. Verified both ways: the
                # mutation removing this line passes the differential check,
                # and a sweep over run lengths 1-6 for 150 ticks found zero
                # runs that were empty-but-not-at-rest with the lane they
                # feed unfed.
                #
                # Kept because it is the *stated* predicate. "The feeding run
                # is at rest" is the claim this sleep rests on, and spelling
                # it as "is empty" would be a second, weaker rule that
                # happens to coincide -- and would silently stop coinciding
                # the moment `is_at_rest()` grows a term. `BeltSim` owns what
                # "at rest" means; this asks rather than re-deriving.
                return False
        return True

    # --- a lane fed by a neighbouring gate, not a belt ---------------------
    #
    # The third case of a missing input lane, and the one tier two originally
    # refused outright. The claim is as narrow as the belt one:
    #
    #   if a lane has no feeding segment, and the gate whose output face
    #   points into it is **holding nothing in that output channel**, then
    #   `deliver_direct()` cannot write this lane on the coming belt phase,
    #   so this gate's readiness cannot change this tick.
    #
    # **Why `out_held` is the whole test.** `deliver_direct()` writes a lane
    # only under `if not gate.out_held(channel): continue`. So an empty
    # upstream zone is not a prediction about what the neighbour will do --
    # it is the precondition of the only code path that can feed us, read
    # from the row. The neighbour may well fire this tick and fill that zone;
    # that fire raises `_wake_around()`, and `deliver_direct()` already
    # `_wake_one()`s both rows when it moves a value. Both edges exist.
    #
    # **It does not require the neighbour to be asleep**, which was the
    # tempting formulation and is strictly worse. "My feeder is asleep" is a
    # claim about another scheduler's conclusion, so two gates could each
    # sleep on the other's sleep -- the mutual-sleep fixed point the block
    # comment above names, and sound only if a wake edge is guaranteed to
    # break it. "My feeder's zone is empty" is a claim about *data*, which
    # cannot be mutually self-supporting: a zone is empty or it is not.
    # Composition stays accidental rather than load-bearing.
    #
    #
    # Translating to the GPU
    # ----------------------
    #
    # This is the question worth settling before the predicate is written,
    # because a rule that needs a barrier *inside* a phase cannot be a
    # parallel pass. It does not need one, and the reason is the two-phase
    # split that already exists:
    #
    #   - the predicate is evaluated in the **structure** phase;
    #   - the only writer of a direct lane, `deliver_direct()`, runs in the
    #     **belt** phase.
    #
    # So it reads `F_OUT_HELD` as it stood at the end of the previous tick,
    # across the phase barrier -- exactly as the belt clause reads
    # `head`/`tail`/`anchor` before the belt phase touches them. Neither is a
    # same-phase read of a value another invocation is writing, which is the
    # only thing that would need ordering.
    #
    # Two properties follow, and they are what make it dispatchable:
    #
    #   - **Read-only on shared state.** Every invocation reads other rows'
    #     `F_OUT_HELD` and writes only its own sleep flag. Many-readers,
    #     one-writer-per-slot, so there is no contention and the result is
    #     independent of invocation order -- the report's determinism recipe.
    #   - **One hop, not a transitive closure.** The predicate asks about the
    #     *immediate* upstream zone and never about that gate's own sleep
    #     state, so there is no chain to iterate to a fixed point. A
    #     transitive version ("my feeder is asleep, whose feeder is
    #     asleep...") would need either multiple passes per tick or a
    #     race-prone single pass, and is precisely what reading data instead
    #     of conclusions avoids.
    #
    # The index itself is the part that is *not* GPU-shaped yet: a Python
    # dict keyed on `(row, channel)`. It is derived per edit, so it is not on
    # the hot path, and the natural device form is a flat
    # `int32[rows * MAX_CHANNELS]` of packed `(upstream row, channel)` with a
    # sentinel for "no direct feeder" -- a gather, which is the one access
    # pattern `tools/probe_batch_prefilter.py` already measured as
    # negligible (scattered gathers are 0.5% of the tick). So the shape ports
    # mechanically; only the container changes.
    def _direct_lane_is_quiet(self, row, channel):
        """Can no direct neighbour feed this lane this tick?

        See the block comment above for the safety and GPU arguments.

        **A lane with no direct feeder at all is refused, not granted**, and
        getting that backwards is the one mistake this method has already
        made. The reasoning that fails is seductive: such a lane dangles --
        no belt, no neighbouring output face -- so nothing can *ever* feed
        it, so it can never change this gate's readiness, so sleep. Every
        step is true and the conclusion is still wrong, because it proves too
        much: a `FIRE_ALL` gate with a permanently unfed lane will never fire
        *at all*, so sleeping it strands whatever its other lanes are
        holding, for ever.

        `tools/check_wakelist_safe.py` caught exactly that as
        `MUTUAL SLEEP: gate 71 is asleep while segment 3 feeding it holds 4
        item(s)`. The stall is genuine and **predates this clause** -- the
        gate never fires either way, so nothing observable changed and the
        differential diff passes with the clause neutered -- but sleeping the
        gate converts a live stall into one the scheduler asserts is
        permanent, and the deadlock invariant is right to refuse that
        distinction. A board can be edited; "this lane dangles" is a fact
        about the board, not about the simulation.

        Two further reasons the dangling case cannot be granted here, both
        fatal on their own and neither visible from this method:

        - **`FIRE_ANY`.** A Sink destroys whatever arrives, so an unfed lane
          says nothing about its readiness. It is excluded today only
          because holding any lane makes it `ready`, so the prefilter admits
          it and `~awake` keeps it out of the tier-two candidate set
          entirely. That is load-bearing and accidental -- it rests on
          another mask's behaviour rather than on anything stated here.
        - **`inputs_ready()` is overridable**, which is what `FIRE_SELECT`
          names. The scheduler deliberately does not mirror those rules (see
          `_gate_is_inert`), so it cannot know which lanes a given type
          actually requires.

        So the quiet answer is only ever given about a lane with a *known
        upstream producer whose zone is observably empty*. That is a claim
        about data for one tick, which is the only kind this predicate is
        entitled to make.
        """
        feeder = self._direct_feeders().get((row, channel))
        if feeder is None:
            return False
        up_row, up_channel = feeder
        ents = self.board.entities
        if not 0 < up_row < ents.shape[0] or not ents[up_row, Entity.F_ALIVE]:
            # The index is rebuilt per edit, so this should not happen -- but
            # a dead row cannot hold anything either way, and guessing "quiet"
            # on a row we cannot read is the unsafe direction.
            return False
        # **Known gap in the coverage, stated rather than discovered later.**
        # `check_wakelist_safe.py` reports this clause's refusals, and the
        # "refused on a loaded zone" count is **0** on its fixture -- so this
        # line is never the one that refuses, and mutating it to `return True`
        # passes the whole tool. The reason is benign and is the same thing
        # the `_wake_around()` comment records: a producer that fills its zone
        # fires in the structure phase and wakes this gate in the same breath,
        # so the predicate is never *asked* about a loaded zone.
        #
        # It is kept because it is the stated premise -- exactly the argument
        # for keeping the two redundant guards in
        # `_starved_on_sleeping_belts()`. Forcing a fixture into the state
        # would need a producer that fills a zone *without* raising a wake,
        # which nothing on a real board does. The dangling refusal above, by
        # contrast, **is** pinned: mutating it to `return True` is caught
        # within 7 ticks as a MUTUAL SLEEP deadlock.
        return not ents[up_row, Entity.F_OUT_HELD + up_channel]

    # The sleep set is mutated from eleven places -- two phases, three port
    # callbacks, the wake edges, the latches and the invalidation -- and the
    # memoised slice above is only correct if *every* one of them bumps the
    # generation. Rather than trust eleven call sites, the mutations go
    # through these three methods, and `_asleep` is never written directly.
    #
    # The failure a missed bump produces is quiet in the dangerous direction:
    # a stale slice that still contains a row which has since gone to sleep
    # merely wastes a tick, but one that *omits* a row an edge has just woken
    # stops ticking it indefinitely. That asymmetry is why this is a wrapper
    # rather than a convention.
    def _sleep_rows(self, rows):
        """Put these rows to sleep."""
        before = len(self._asleep)
        self._asleep.update(rows)
        if len(self._asleep) != before:
            self._sleep_gen += 1

    def _wake_rows(self, rows):
        """Wake these rows. Cheaper than it looks: usually a one-element hit."""
        asleep = self._asleep
        if not asleep:
            return
        before = len(asleep)
        asleep.difference_update(rows)
        if len(asleep) != before:
            self._sleep_gen += 1

    def _wake_one(self, row):
        """Wake a single row. The hot form, used by the port callbacks."""
        asleep = self._asleep
        if asleep and row in asleep:
            asleep.discard(row)
            self._sleep_gen += 1

    def _publish_sleep(self):
        """Mirror the sleep set into `F_ASLEEP` for the SLEEP_DEBUG overlay.

        **Once per structure phase, not per mutation.** The three mutators
        above are hot -- `_wake_one()` runs from every port callback, inside
        the belt tick -- so writing the array there would charge the
        simulation for a diagnostic on its hottest path. A frame can only
        show one answer per tick anyway, so the end of the phase is both the
        cheapest place and the only one that matters.

        Written over the **gate table** rather than the whole entity array:
        belts are most rows on a real board and none of them is a gate, so
        zeroing them would be a large write to say something already true.
        That does mean a row leaving the table (an erase) keeps its last
        value, which is harmless -- the cell is gone, so nothing samples it --
        and a *recycled* row is handled because `invalidate_board()` clears
        the table and the set together.

        **A folded Source publishes as asleep too**, and it is not in
        `_asleep` -- it leaves the gate table entirely (see `_gate_table()`),
        which is a stronger form of not being ticked than sleeping is. Blue
        means "costing no time in `tick()`", so a constant that has been
        folded out of the phase is exactly what the colour is for, and it read
        amber before this. It is also the only way folding is visible in the
        world view: a folded Source is identical in outline and the Status
        window's `folded: yes` is one entity at a time.

        That is why the folded rows are published **separately from the gate
        table** rather than by including them in it -- the table's whole point
        is that they are absent from it.

        Runs whether or not gate *sleeping* is on, for the same reason: the
        fold is a different mechanism with its own switch
        (`set_folding_enabled`), so tying its colour to the sleep latch would
        make the overlay lie about whichever one happened to be off.
        """
        ents = self.board.entities
        rows, _n_in, _n_out, _strict = self._gate_table()
        if len(rows):
            asleep = self._asleep
            if asleep:
                flags = np.isin(rows, np.fromiter(
                    asleep, dtype=np.int64, count=len(asleep)))
                ents[rows, Entity.F_ASLEEP] = flags.astype(np.float32)
            else:
                ents[rows, Entity.F_ASLEEP] = 0.0
        folded = self._folds()[0]
        if folded:
            ents[list(folded), Entity.F_ASLEEP] = 1.0
        # `entities_dirty` is a **union range**, not a set, so one
        # `add_range()` spanning both populations is both the correct call and
        # cheaper than adding each row -- the upload is one contiguous write
        # either way, and these rows are scattered through the table.
        touched = list(folded)
        if len(rows):
            touched.append(int(rows[0]))
            touched.append(int(rows[-1]))
        if touched:
            self.board.entities_dirty.add_range(
                int(min(touched)), int(max(touched)) + 1)

    def _awake_slice(self, rows, n_in, n_out, strict):
        """The gate table restricted to the rows not asleep, memoised.

        Keyed on `(gate table identity, sleep generation)`: the table is
        rebuilt per edit and the generation is bumped by every mutation of the
        sleep set, so a quiet board reuses one slice indefinitely. See the
        call site for the measurement that forced this -- recomputing the mask
        per tick is slower than not slicing at all.
        """
        if not self._sleep_enabled or not self._asleep:
            return rows, n_in, n_out, strict
        key = (id(rows), self._sleep_gen)
        if self._awake_cache is not None and self._awake_cache[0] == key:
            return self._awake_cache[1]
        asleep = self._asleep
        mask = ~np.isin(rows, np.fromiter(
            asleep, dtype=np.int64, count=len(asleep)))
        sliced = (rows[mask], n_in[mask], n_out[mask], strict[mask])
        self._awake_cache = (key, sliced)
        return sliced

    def _sleep_changed(self):
        """Invalidate the awake slice. Called by every sleep-set mutation.

        A plain counter rather than a dirty flag, because the cache key has to
        distinguish "changed and changed back" from "unchanged" -- a set that
        loses one row and gains another has the same length and would
        otherwise look identical.
        """
        self._sleep_gen += 1

    def _sleeping_stats(self):
        """(starved, blocked) to credit for the gates this tick skipped.

        **Every sleeping gate is starved, never blocked**, and that falls out
        of both tiers' predicates rather than being an approximation:

          - tier one sleeps a gate holding *nothing*, which cannot be blocked
            (blocked means a waiting zone is occupied) and is unready unless
            it has no inputs at all -- and a generator is `ready`, so it is
            admitted and never slept.
          - tier two excludes `holds_out` explicitly, for exactly this reason
            among others, and requires a *missing* input lane. So it is
            unready too.

        `_gate_is_inert()` and the tier-two mask are the two places that can
        add to the sleep set, and both exclude `awake`, so a sleeping row
        would have been counted `starved` by the full pass. One number, no
        per-row bookkeeping, and it is exact rather than close.

        A self-check in the differential tool would be the place to pin this
        if either predicate ever admits an output-holding gate.
        """
        if not self._sleep_enabled:
            return 0, 0
        return len(self._asleep), 0

    def wake_gate(self, row):
        """Mark a gate as needing ticks again. Idempotent and cheap."""
        self._wake_one(row)

    def wake_all_gates(self):
        """Wake every gate. Used after a rebuild or a wholesale change."""
        self._wake_rows(tuple(self._asleep))

    def set_sleep_enabled(self, on):
        """Turn gate sleeping off entirely, for a differential check.

        Mirrors `BeltSim.set_sleep_enabled()`, and latched for the same
        reason: waking everything still lets the coming tick re-sleep, so an
        oracle timeline built on `wake_all_gates()` would accumulate a sleep
        set and stop being an oracle after one tick.
        """
        self._sleep_enabled = bool(on)
        if not self._sleep_enabled:
            self._wake_rows(tuple(self._asleep))
        # The slice is keyed on the generation, not on the latch, so flipping
        # the latch has to invalidate it explicitly.
        self._awake_cache = None
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

        **This reads the entity table rather than the SoA mirror**, and that
        is deliberate rather than an omission. It is called from the belt
        phase, after `_fire_admitted()` has written results through views
        straight to `Gameboard.entities` -- so the mirror loaded at the top of
        the structure phase is stale by the time this runs. The mirror's
        contract is that exactly one copy is authoritative at a time (see
        core/gate_state.py), and outside the prefilter's window that copy is
        the table.

        Reading the mirror here would be the drift the contract exists to
        prevent, and it would be silent: a gate whose zone filled this tick
        would simply not be offered to its neighbour, which looks like back
        pressure. The fix when the whole phase moves to the mirror is to move
        the write side too, not to widen the read.
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

    # --- the compute form -------------------------------------------------
    def create_device_resources(self, ctx, belt_device=None):
        """Compile the compute kernels and size their buffers.

        Optional: with no device the CPU phases run exactly as before, which
        is how every headless test and the whole existing suite behave. The
        device is additive until it is proven, and `compare_against_cpu()`
        is what proves it.

        `belt_device` is the renderer's `BeltDevice`, passed in rather than
        made here because the belt kernel **borrows its buffers** -- see the
        binding note in render/compute.py. Without it only the gate kernels
        are built, which is the whole feature for a caller that has no
        renderer.
        """
        from core.belt_compute import BeltCompute
        from core.gate_device import GateDevice
        self._gate_table()
        self.device = GateDevice(ctx, self.state)
        if belt_device is not None:
            self.belt_compute = BeltCompute(
                ctx, self.index, self.belts, self, belt_device)
        self.sync_device()
        return self.device

    def has_host_decided_gates(self):
        """Is any live gate one the firing kernel declines to decide?

        `FIRE_SELECT` types -- and anything overriding `inputs_ready()` or
        `consumed()` -- are skipped by the kernel rather than guessed at (see
        `gate_device.geometry_for`). That is right for the kernel and means
        the *board* needs the CPU phase: one Merge and the GPU tick would
        leave it permanently unfired, which is a stall the player would read
        as a wiring mistake.

        Derived from the packed geometry rather than re-walking the types, so
        there is one authority on which gates the kernel handles and the
        answer cannot drift from what was uploaded.
        """
        if self.device is None:
            return False
        from core.gate_device import FLAG_HOST_DECIDES, gate_flags
        n = len(self.state.rows)
        if not n:
            return False
        geom = self.device.geom
        return any(gate_flags(geom, i) & FLAG_HOST_DECIDES
                   for i in range(n))

    def sync_device(self):
        """Re-pack the device's per-edit geometry. Call after an edit.

        Needs `_connected_channels()` to have run for every gate, since the
        drains mask is filled as those answers arrive -- so it asks for them
        here rather than hoping the tick already did. That is the one term
        the kernel needs that is not row state, and a missing entry reads as
        "no consumer", which would make a gate inert rather than wrong.
        """
        if self.device is None:
            return
        self._gate_table()
        for row in self.state.rows.tolist():
            if self.board.is_row_alive(row):
                self._connected_channels(self.board.view(row))
        self.device.rebuild(self._gate_types, self.board,
                            edges=self._direct_edges())
        if self.belt_compute is not None:
            self.belt_compute.rebuild(self.state)

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
        # Re-assert the folded constants the fires above consumed. **After
        # firing, not before**: a folded channel must read as held when the
        # prefilter and `inputs_ready()` look at it, which is what the
        # assertion at derivation time already guarantees, and then be put
        # back once `consumed()` has cleared it. Refilling first would be
        # wrong in a way that is invisible on a constant -- the value is the
        # same either way -- but would mean a gate's folded input was
        # momentarily unfed between the phase's start and the refill, which
        # any future reader of mid-phase state would see.
        self._assert_folded(self._folds()[1])
        # Publish the sleep set for the overlay, last, so it describes the
        # state the frame will actually render rather than a mid-phase one.
        # **Unconditional**, not gated on `_sleep_enabled`: it also publishes
        # the folded Sources, which are a separate mechanism with a separate
        # switch, so skipping it with sleeping off would paint every folded
        # constant awake.
        with prof.probe("structure/publish_sleep"):
            self._publish_sleep()
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
        self._wake_one(row)

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
        self._wake_one(row)
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
        **That is now a property of the data rather than of this loop**: the
        edge list has one entry per receiving lane, so the disjointness holds
        however the entries are visited. See `_direct_edges()`.

        The geometry is resolved once per edit, so what remains here is the
        transfer itself: read a held zone, write the far lane, clear the
        zone. Every term is a row-and-column index, which is what makes this
        a dispatch over a flat array later rather than a grid search.
        """
        src_row, src_ch, dst_row, dst_ch = self._direct_edges()
        if not len(src_row):
            return 0

        ents = self.board.entities
        # Gather the whole edge list at once: which sources are holding, and
        # which destinations have a free lane. Both are the vectorised form
        # of the two `continue`s the per-face walk made, and together they
        # pick out exactly the edges that will move something.
        held = ents[src_row, Entity.F_OUT_HELD + src_ch] > 0.0
        free = ents[dst_row, Entity.F_IN_HELD + dst_ch] <= 0.0
        live = held & free
        if not live.any():
            return 0

        s_row = src_row[live]
        s_ch = src_ch[live]
        d_row = dst_row[live]
        d_ch = dst_ch[live]

        # The transfer. Disjoint by construction on the destination side (one
        # edge per lane), and on the source side a zone feeds at most one
        # direct neighbour, so neither gather aliases.
        values = ents[s_row, Entity.F_OUT_VALUE + s_ch]
        ents[d_row, Entity.F_IN_VALUE + d_ch] = values
        ents[d_row, Entity.F_IN_HELD + d_ch] = 1.0
        ents[s_row, Entity.F_OUT_VALUE + s_ch] = 0.0
        ents[s_row, Entity.F_OUT_HELD + s_ch] = 0.0

        dirty = self.board.entities_dirty
        touched = set(s_row.tolist()) | set(d_row.tolist())
        for row in touched:
            dirty.add(row)
            # Both gates' channels changed, so the runs on either side of
            # either of them may have work again -- this is the same edge
            # the structure phase raises, reached by the no-belt path.
            self._wake_around(row)
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
            self._wake_one(row)

        moved = int(live.sum())
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
            # Mirror it into the SoA mask as the answer arrives, so the
            # kernel form has the same term without a second walk that could
            # disagree with this one. Harmless for a row that is not in the
            # table (a folded Source): `set_drains()` ignores a bad slot.
            slot = self.state.slot_of
            if gate.row < slot.shape[0]:
                self.state.set_drains(int(slot[gate.row]), hit)
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

    # --- constant folding -------------------------------------------------
    #
    # See the module docstring for what this is and why it is shaped this way.
    # The two halves it produces are used in three places and nowhere else:
    #
    #   - `folded_sources` is subtracted from the firing set (the prefilter)
    #     and from `deliver_direct()`'s walk;
    #   - `folded_inputs` is asserted into the receiving rows once at
    #     derivation and re-asserted after each structure phase.
    def _folds(self):
        """(folded_source_rows, folded_inputs), derived once per edit.

        `folded_inputs` is a tuple of `(row, channel, value)`. Derived lazily
        and cached beside the connectivity caches, because it is a function of
        exactly the same inputs and must die at exactly the same moment.
        """
        if self._folding is not None:
            return self._folding
        if not self._folding_enabled:
            # Cache the empty answer, so the latch costs one test rather than
            # a whole-board walk per tick.
            self._folding = (frozenset(), ())
            return self._folding

        sources = set()
        inputs = []
        for gate in self.gates():
            fold = self._fold_of(gate)
            if fold is None:
                continue
            sources.add(gate.row)
            inputs.extend(fold)
        folding = (sources, tuple(inputs))
        self._folding = folding
        # Assert the constants immediately rather than waiting for the next
        # structure phase. Two reasons: a gate's readiness is read by the
        # renderer and the Status window between ticks, so a folded input must
        # be visible as held the moment the fold is derived; and the phase's
        # refill runs *after* firing, so waiting would cost the first tick.
        self._assert_folded(folding[1])
        return folding

    def _fold_of(self, gate):
        """[(row, channel, value)] if `gate` is a foldable Source, else None.

        Foldable means: a generator whose output faces reach at least one gate
        input and **no belt it can actually deposit onto**. One such belt makes
        it a real producer whose rate that belt regulates, so folding it would
        change behaviour; see the module docstring.

        Dangling faces are ignored rather than disqualifying, per the design:
        a dangling output is already destroyed at fire time, so it carries
        nothing a fold could lose.

        **A belt a face merely *touches* is a dangling face, not a belt.** A
        multi-port machine can have two faces against one run and a segment
        takes from exactly one of them (`take_dir()`); the loser can never
        push an item, so it is a discard point exactly like an empty cell.
        Treating any adjacent belt as disqualifying refused the fold on a
        whole class of real Source -- the one whose spare face happens to sit
        beside a run it does not own -- and refused it for a connection that
        does not exist. `_face_has_consumer()` is the authority on which it
        is, because the face *direction* alone cannot tell two ports of the
        same gate apart; it resolves the gate **cell** adjacent to the tail,
        which can. Asking it here is also what keeps this rule and the one
        `_connected_channels()` enforces from drifting apart: the set of faces
        that can block a fold is now exactly the set that can block a fire.
        """
        if gate.input_count() != 0 or not isinstance(gate, Source):
            return None
        reached = []
        for (xy, face, _channel) in self._output_faces(gate):
            step = cell.DIR_STEPS[face]
            nx, ny = xy[0] + step[0], xy[1] + step[1]
            if not self.board.in_bounds(nx, ny):
                continue
            # A belt this face can really deposit onto disqualifies the whole
            # machine, not just that face: the Source is then a producer that
            # has to be ticked, and a half-folded one would emit on the belt
            # while its folded channel also stood permanently fed.
            #
            # A run the face only *abuts* is not that. `_face_has_consumer()`
            # decides, per the docstring -- False here means the segment named
            # a different port, so this face can never push and is a discard
            # point like any dangling one.
            if self.index.at(nx, ny) is not None:
                if self._face_has_consumer(gate, xy, face):
                    return None
                continue
            row = self.board.entity_id_at(nx, ny)
            if not row or not self.board.is_row_alive(row):
                continue
            other = self.board.view(row)
            if other.row == gate.row or other.is_ghost:
                continue
            in_ch = self._input_channel_for(other, (face + 2) % 4, at=(nx, ny))
            if in_ch is None:
                continue
            reached.append((other.row, in_ch, float(gate.emit_value)))
        if not reached:
            # Nothing but dangling faces. Deliberately *not* folded: it emits
            # nowhere, so there is nothing to hold, and leaving it unfolded
            # keeps it reported as `unconnected` exactly as before.
            return None
        return reached

    def _assert_folded(self, folded_inputs):
        """Write every folded constant into its receiving row.

        Called once when the fold is derived and again after each structure
        phase, which is what makes `consumed()` clearing the channel harmless.
        Writing unconditionally rather than testing `in_held` first: the test
        costs more than the two float stores it would save, and a value that
        is already right is written with the same number it had.
        """
        if not folded_inputs:
            return
        ents = self.board.entities
        dirty = self.board.entities_dirty
        for (row, channel, value) in folded_inputs:
            ents[row, Entity.F_IN_VALUE + channel] = value
            ents[row, Entity.F_IN_HELD + channel] = 1.0
            dirty.add(row)

    def _drop_folding(self):
        """Forget the fold, and release the channels it was holding.

        **Releasing is the half that is easy to miss and fatal to omit.** A
        folded channel is held by nothing but this derivation -- no belt, no
        neighbour -- so dropping the fold without clearing it would leave the
        input permanently fed by a Source that may have just been erased, and
        no later edit or flush would ever clear it. The gate would fire for
        ever on a phantom constant.

        Rows are cleared before the re-derivation that follows, so a fold that
        survives an edit is simply re-asserted; one that does not is gone.
        Guarded on the row still being alive, since an erase is exactly the
        edit that gets here.
        """
        if self._folding is None:
            return
        _sources, inputs = self._folding
        self._folding = None
        ents = self.board.entities
        for (row, channel, _value) in inputs:
            if not self.board.is_row_alive(row):
                continue
            ents[row, Entity.F_IN_VALUE + channel] = 0.0
            ents[row, Entity.F_IN_HELD + channel] = 0.0
            self.board.entities_dirty.add(row)

    def set_folding_enabled(self, on):
        """Turn constant folding off entirely, for a differential check.

        Mirrors `set_sleep_enabled()` and is latched for the same reason: the
        fold is re-derived lazily, so clearing it without a latch would simply
        have the next tick fold again and an oracle timeline built on that
        would stop being an oracle immediately.

        Turning it **off** releases the channels the fold was holding (via
        `_drop_folding()`), and turning it back on re-derives on next use. So
        an oracle built with this off sees a board where every Source really
        does fire, which is the pre-folding behaviour it is meant to compare
        against.
        """
        self._folding_enabled = bool(on)
        self._drop_folding()
        # The gate table *excludes* folded rows, so it is derived from the
        # fold and has to be dropped with it in either direction.
        self._gate_table_cache = None
        if self._folding_enabled:
            # **Re-derive now, do not wait.** `_drop_folding()` has just
            # released every folded channel, and the re-derivation is lazy, so
            # returning here would leave the board with folding nominally on
            # and every constant missing until something next called
            # `_folds()`. The gates fed by a constant would be starved for
            # that whole window -- one tick on a running board, and
            # indefinitely on a paused one.
            self._folds()
        return self._folding_enabled

    def reassert_folded(self):
        """Put every folded constant back. For `Engine.flush_material()`.

        The public form of `_assert_folded()`, so the engine does not reach
        into the derivation. See the call site for why a flush has to do this.
        """
        self._assert_folded(self._folds()[1])

    def folded_sources(self):
        """The set of Source rows currently folded. For the UI and the tools."""
        return self._folds()[0]

    def is_folded(self, row):
        """Is this row a Source whose output is folded into its neighbours?

        The query the Status window and the sleep overlay ask. Derives the
        fold if it has not been derived yet, so a caller between ticks gets a
        real answer rather than `False`.
        """
        sources, _inputs = self._folds()
        return row in sources

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
