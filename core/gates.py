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

from core import cell
from core.entity import Entity


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
        self.stats = {"fired": 0, "blocked": 0, "starved": 0,
                      "drained": 0, "loaded": 0, "consumed": 0, "direct": 0,
                      "unconnected": 0, "destroyed": 0}
        #: gate row -> connected output channels, for the current tick only.
        #: See _connected_channels() for why a per-tick cache is sound.
        self._drains_cache = {}
        #: gate row -> ((world_cell, face, channel), ...). See _output_faces().
        self._faces_cache = {}

    def invalidate_connectivity(self):
        """Drop the cached output connectivity and port geometry.

        Called when the thing they are derived from changes: the grid (a
        placement, an erase, an in-place turn) or the segmentation. Cheap and
        idempotent.

        Both caches are keyed by row, and a row is recycled by the free list,
        so dropping them together on any grid change is what keeps a new
        entity from inheriting the previous occupant's ports.
        """
        self._drains_cache.clear()
        self._faces_cache.clear()

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

    # --- structure phase --------------------------------------------------
    def structure_phase(self):
        """Fire every gate that can. Returns the number that fired.

        Reads input channels and output waiting zones; writes output waiting
        zones and clears inputs. Deliberately touches no belt state -- see
        the module docstring.
        """
        fired = 0
        for gate in self.gates():
            if not gate.inputs_ready():
                self.stats["starved"] += 1
                continue

            n_in = gate.input_count()
            if n_in == 0:
                if self._fire_generator(gate):
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
            fired += 1

        self.stats["fired"] += fired
        return fired

    # --- the port callbacks, called from inside BeltSim.tick() ------------
    def offer_to(self, seg):
        """What the gate upstream of `seg` wants to put on it, or None.

        BeltSim's producer callback. Returning a value does *not* commit to
        it -- BeltSim decides whether there is room and only then pushes, so
        this must not clear the waiting zone. `took_from()` does that once
        the push has actually happened.
        """
        gate_port = self._src_port(seg)
        if gate_port is None:
            return None
        gate, channel = gate_port
        if not gate.out_held(channel):
            return None
        return gate.out_value(channel)

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
        gate_port = self._src_port(seg)
        if gate_port is None:
            return
        gate, channel = gate_port
        gate.clear_output(channel)
        self.stats["drained"] += 1

    def accept_from(self, seg, payload):
        """Can the gate downstream of `seg` take its head item?

        BeltSim's consumer callback. Writes the gate's input channel and
        returns True, or returns False to leave the item on the belt -- which
        is how a run backs up behind a gate that is not firing.
        """
        gate_port = self._dst_port(seg)
        if gate_port is None:
            return False
        gate, channel = gate_port
        if gate.in_held(channel):
            return False
        gate.set_input(channel, payload)
        self.stats["loaded"] += 1
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
        for gate in self.gates():
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
                moved += 1
        self.stats["direct"] += moved
        return moved

    # --- resolving a segment's ends to (gate, channel) --------------------
    def _src_port(self, seg):
        """The (gate_view, out_channel) feeding this segment, or None."""
        if seg.src is None:
            return None
        row, face = seg.src
        if not self.board.is_row_alive(row):
            return None
        gate = self.board.view(row)
        if gate.is_ghost:
            return None
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
                return gate, channel
        return None

    def _dst_port(self, seg):
        """The (gate_view, in_channel) this segment drains into, or None."""
        if seg.dst is None:
            return None
        row, face = seg.dst
        if not self.board.is_row_alive(row):
            return None
        gate = self.board.view(row)
        if gate.is_ghost:
            return None
        # As in _src_port(): `face` points from the gate toward the belt, so
        # the gate cell is one step *backwards* from the segment's head.
        head = seg.cells[-1]
        step = cell.DIR_STEPS[face]
        want = (head[0] - step[0], head[1] - step[1])
        channel = self._input_channel_for(gate, face, at=want)
        if channel is None:
            return None
        return gate, channel

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
