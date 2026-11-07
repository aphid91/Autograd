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

A gate is blocked when any output waiting zone is still occupied. The belt
phase empties them; if a belt is full the zone stays occupied and the gate
does not fire next tick. That is the whole mechanism -- no explicit "is the
belt full" query anywhere, which matters because a gate must not read belt
state during the structure phase.

A consequence worth knowing: **an occupied waiting zone at the end of a tick
is a renderable state.** It means "this gate produced something the belt
would not take", and the renderer draws it squished between the gate and the
belt. While things flow it is always empty by end of tick, so nothing shows.


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
                      "drained": 0, "loaded": 0, "consumed": 0, "direct": 0}

    # --- gate discovery ---------------------------------------------------
    def gates(self):
        """Every live structure on the board, as views.

        Scans the entity table rather than the grid: a structure occupies
        several cells but is one row, and firing is per entity.
        """
        board = self.board
        for row in range(1, board.capacity):
            if not board.is_row_alive(row):
                continue
            view = board.view(row)
            t = view.type_id
            if t == cell.TYPE_EMPTY or t == cell.TYPE_BELT \
                    or t == cell.TYPE_UNDER:
                continue
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
            if not gate.outputs_clear():
                # Something is still waiting to leave. This is back pressure
                # arriving: the belt downstream did not take the last result.
                self.stats["blocked"] += 1
                continue

            n_in = gate.input_count()
            values = [gate.in_value(i) if gate.in_held(i) else None
                      for i in range(n_in)]
            results = gate.fire(values)

            # Consume the inputs. Under FIRE_ANY only the fed channels are
            # cleared, which for a Sink means the one that arrived.
            for i in range(n_in):
                if gate.in_held(i):
                    gate.clear_input(i)
                    self.stats["consumed"] += 1

            for i, value in enumerate(results):
                if value is not None:
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

    # --- port geometry ----------------------------------------------------
    def _output_faces(self, gate):
        """[(world_cell, face_dir, channel)] for each of a gate's outputs."""
        ox, oy = gate.origin
        _ins, outs = gate.faces_for(ox, oy, gate.direction, gate.flipped)
        return [(xy, face, gate.out_channel(i))
                for i, (xy, face) in enumerate(outs)]

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
        """The tick the phases are computing. Set by the engine each tick."""
        self._tick = tick

    _tick = 0
