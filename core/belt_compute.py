"""Belt transport on the GPU, and the oracle it is checked against.

`BeltDevice` already mirrors segment and item state into SSBOs for the
*renderer*, which evaluates the closed form per pixel. This is the other
consumer of the same layout: a compute dispatch that *advances* it.

Sharing the record layout is deliberate -- `belt_tick.comp` reads the same
`seg_i` / `seg_f` / `items` shape `common.glsl` does, so the simulation and
the picture cannot disagree about where an item is. What this adds is the
one thing the renderer never needs: each end's resolved port, so a thread
can offer to and accept from gate channels without touching the grid.

Why the ports are a table
-------------------------

`seg.src` / `seg.dst` are written once per rebuild and never touched again
(see the "resolving a segment's ends" block in core/gates.py). On the CPU
that justified a cache; here it justifies an array. Resolving a port is grid
and geometry work, and neither exists on the device -- so the host does it
per edit and the kernel reads two ints.

Dangling ends are `-1`, which is the whole encoding: a run with no producer
simply never pushes, and one with no consumer never pops. The CPU expresses
that with `None` and an early return; the kernel with a comparison.

Verification
------------

The belt phase has the strongest oracle in the project -- `naive_tick()`,
the report's reference implementation, which `tests/test_belts.py` already
runs against the closed form over 600 randomised configurations. The GPU
form is checked against the *CPU closed form* rather than against the naive
oracle directly, because the CPU form is itself oracle-checked: chaining
them is what keeps each comparison about one thing.
"""

import numpy as np

from core.belt_device import ITEM_VEC, SEG_IVEC, SEG_VEC
from render import compute

#: The int record's layout is `belt_device.SEG_IVEC` -- **the renderer's**,
#: not one of this module's own. The kernel binds common.glsl's blocks at
#: bindings 1/2/3 rather than declaring its own, so the pass that advances a
#: run and the shader that draws it share one record shape and cannot
#: disagree about it. A narrower compute-only record was the first attempt
#: and bought nothing: GLSL refuses to let one buffer variable belong to two
#: interface blocks, which is the compiler stating the same rule.
SEG_I_COMPUTE = SEG_IVEC


def port_table(index, gates, state):
    """Each segment's two ends as `(src slot, src ch, dst slot, dst ch)`.

    Indexed by segment id, like the other segment arrays, so a gap in the id
    space is a dangling record rather than a shifted one.

    Slots rather than entity rows, for the same reason the direct edge list
    is packed that way: the kernel indexes the gate SoA arrays, so one host
    translation beats one per thread. A row outside the gate table resolves
    to -1 and reads as dangling -- which is right, since a folded Source is
    absent from the table precisely because it never fires.
    """
    max_sid = max(index.segments, default=0)
    table = np.full((max_sid + 1, 4), -1, dtype=np.int32)
    slot_of = state.slot_of
    cap = slot_of.shape[0]
    for sid, seg in index.segments.items():
        for resolved, lo in ((gates._src_cached(seg), 0),
                             (gates._dst_cached(seg), 2)):
            if resolved is None:
                continue
            row, channel = resolved
            if not 0 <= row < cap:
                continue
            slot = int(slot_of[row])
            if slot < 0:
                continue
            table[sid, lo] = slot
            table[sid, lo + 1] = channel
    return table


class BeltCompute:
    """Runs belt transport as a dispatch over segments.

    Holds the device-side copies; `SegmentIndex` still holds the authority
    between runs. Same split as `GateDevice`/`GateState`.
    """

    def __init__(self, ctx, index, sim, gates, device):
        self.ctx = ctx
        self.index = index
        self.sim = sim
        self.gates = gates
        #: **The renderer's `BeltDevice`, whose buffers this borrows.** See
        #: the binding note in render/compute.py: one copy on one binding,
        #: or the kernel advances a buffer nothing draws.
        self.device = device
        self.kernel = compute.ComputeKernel(ctx, "belt_tick.comp")

        self.ports = np.full((1, 4), -1, dtype=np.int32)
        self.count = 0
        self._ports_buf = compute.ComputeBuffer(
            ctx, self.ports, compute.BELT_PORT_BINDING)

    # The segment arrays live on the device object; these keep the old
    # spelling working and make the borrowing explicit at each use.
    @property
    def seg_i(self):
        return self.device.seg_i

    @property
    def seg_f(self):
        return self.device.seg_f

    @property
    def items(self):
        return self.device.items

    # --- layout -----------------------------------------------------------
    def rebuild(self, state):
        """Re-pack every per-edit array. Call after a segment rebuild.

        A rebuild reassigns every segment id and recarves the arena, so
        there is nothing incremental to do here -- the same wholesale
        treatment `BeltDevice._rebuild_layout()` gives it, and for the same
        reason: a rebuild already drops every item.
        """
        segs = self.index.segments
        self.count = max(segs, default=0) + 1
        # The arena layout belongs to `BeltDevice`: it owns the arrays, and
        # two prefix sums over the same capacities would be two chances to
        # disagree about a ring's base.
        self.device.invalidate_layout()
        self.device.sync()

        self.ports = port_table(self.index, self.gates, state)
        if self.ports.shape[0] < self.count:
            grown = np.full((self.count, 4), -1, dtype=np.int32)
            grown[:self.ports.shape[0]] = self.ports
            self.ports = grown
        self._ports_buf.set_array(self.ports)

    # --- transfers --------------------------------------------------------
    def upload(self):
        """Mirror the live segment objects into the device arrays.

        `BeltDevice.sync()` already packs and uploads the segment and item
        state -- it does it every frame for the renderer -- so this adds only
        the port table the kernel needs and nothing else does.
        """
        self.device.sync()
        self._ports_buf.upload(self.ports)

    def readback(self):
        """Device -> the live segment objects. The expensive act of asking."""
        self.device.download()
        for sid, seg in self.index.segments.items():
            if sid >= self.count:
                continue
            seg.head = int(self.seg_i[sid, 0])
            seg.tail = int(self.seg_i[sid, 1])
            seg.anchor = float(self.seg_f[sid, 0])
            if seg.cap and seg.base + seg.cap <= self.items.shape[0]:
                lo = seg.base
                seg.enter_tick[:] = self.items[lo:lo + seg.cap, 0]
                seg.payload[:] = self.items[lo:lo + seg.cap, 1]

    # --- the dispatch -----------------------------------------------------
    def run(self, tick, ticks=1):
        """Advance every segment `ticks` times.

        `tick` is the simulation tick the first dispatch computes; it
        advances with each one, because an item's position is a closed form
        over the tick counter and the kernel evaluates it.
        """
        if not self.count:
            return
        self.device.bind(compute.BELT_SEG_I_BINDING,
                         compute.BELT_SEG_F_BINDING,
                         compute.BELT_ITEM_BINDING)
        self._ports_buf.bind()
        # **Bind the gate channel buffers too.** The pop writes a gate's
        # input lane and the push reads its waiting zone, so bindings 4-7
        # are as much this kernel's inputs as the segment arrays are --
        # leaving them to whatever bound last meant the kernel read a
        # *stale* `in_held` and popped into a lane that was actually full.
        # The symptom was the belt layer disagreeing with the CPU about a
        # pop, which reads as a transport bug and is a binding bug.
        gate_device = self.gates.device
        if gate_device is not None:
            for buf in gate_device._bufs.values():
                buf.bind()
        self.kernel["segment_count"] = int(self.count)
        groups = compute.groups_for(self.count)
        for i in range(int(ticks)):
            self.kernel["sim_tick"] = int(tick) + i
            self.kernel.run(groups)
            compute.barrier(self.ctx)

    def release(self):
        self._ports_buf.release()
        self.kernel.release()


def compare_against_cpu(engine, ticks=1):
    """One belt phase both ways from identical state. Returns problems.

    Snapshots every ring, runs the CPU transport, snapshots the result,
    restores, runs the kernel, and diffs -- the same shape
    `gate_device.compare_against_cpu()` has, and restoring in between is
    what makes it a comparison of two implementations rather than of two
    consecutive ticks.
    """
    index = engine.segments
    gates = engine.gates
    belt = gates.belt_compute
    if belt is None or not index.segments:
        return []

    def snapshot():
        return {sid: (s.head, s.tail, s.anchor,
                      np.array(s.enter_tick, copy=True),
                      np.array(s.payload, copy=True))
                for sid, s in index.segments.items()}

    def restore(snap):
        for sid, (h, t, a, e, p) in snap.items():
            s = index.segments[sid]
            s.head, s.tail, s.anchor = h, t, a
            s.enter_tick[:] = e
            s.payload[:] = p

    before = snapshot()
    rows = gates.state.rows
    chan = gates.board.entities[rows].copy() if len(rows) else None

    # **The CPU's belt phase is run with gate sleeping latched off.** The
    # kernel has no sleep set -- every segment is visited, which is the right
    # trade when a visit is a few ALU ops -- so a sleeping CPU run would
    # skip work the GPU does and the diff would report the *scheduler*
    # rather than the transport. That is not hypothetical: it showed up as
    # the kernel popping six items the CPU "refused", where the CPU had
    # simply not visited the run.
    #
    # `set_sleep_enabled(False)` both latches it off *and* clears the set,
    # which matters: runs that went to sleep during the fixture's warm-up
    # are already in it, so latching alone would still skip them on the
    # first compared tick. That is the same distinction
    # `BeltSim.set_sleep_enabled()` documents for the differential checker.
    sleeping = engine.belts._sleep_enabled
    engine.belts.set_sleep_enabled(False)
    tick = engine.clock.tick
    try:
        for i in range(ticks):
            gates.tick_belts(tick + i)
    finally:
        engine.belts.set_sleep_enabled(sleeping)
    after_cpu = snapshot()

    restore(before)
    if chan is not None:
        gates.board.entities[rows] = chan
    gates.state.sync_from_rows()
    belt.upload()
    gates.device.upload()
    belt.run(tick, ticks)
    belt.readback()
    gates.device.readback()
    gates.state.flush()
    after_gpu = snapshot()

    problems = []
    for sid in sorted(after_cpu):
        c, g = after_cpu[sid], after_gpu[sid]
        for name, i in (("head", 0), ("tail", 1)):
            if c[i] != g[i]:
                problems.append(
                    f"segment {sid} {name}: cpu {c[i]}, gpu {g[i]}")
        if abs(c[2] - g[2]) > 1e-3:
            problems.append(
                f"segment {sid} anchor: cpu {c[2]}, gpu {g[2]}")
        # Only the live window matters: slots outside [head, tail) are never
        # read by a correct implementation, so a difference there is noise.
        lo, hi = c[0], c[1]
        cap = index.segments[sid].cap
        for k in range(lo, hi):
            slot = k % cap
            if not np.isclose(c[4][slot], g[4][slot], equal_nan=True):
                problems.append(
                    f"segment {sid} item {k} payload: cpu {c[4][slot]}, "
                    f"gpu {g[4][slot]}")
            if c[3][slot] != g[3][slot]:
                problems.append(
                    f"segment {sid} item {k} enter_tick: cpu {c[3][slot]}, "
                    f"gpu {g[3][slot]}")
    return problems
