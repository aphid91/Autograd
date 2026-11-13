"""The structure phase on the GPU: packing, dispatch, and the oracle seam.

`GateState` holds the gate-dense SoA arrays (see core/gate_state.py); this
puts them in SSBOs, packs the per-edit geometry the kernel needs beside them,
and runs `shaders/gate_fire.comp`.

The geometry word
-----------------

Each gate gets one `ivec4`: `(type, n_in, n_out, flags)`. The flags word
carries three things, and packing them together is deliberate -- a thread
reads its geometry once and has everything it needs to decide, rather than
touching three arrays to answer one question:

    bit 0        host decides (FIRE_SELECT, or inputs_ready() overridden)
    bit 1        FIRE_ANY (one fed channel is enough)
    bits 8..8+c  the drains mask: can channel c's result go anywhere

`FLAG_HOST_DECIDES` is the honest part of the port. `inputs_ready()` and
`consumed()` are overridable in Python and Merge overrides both -- readiness
on the *values* held, consumption of only the selected lane. The kernel does
not guess: those gates are skipped and left exactly as the CPU found them,
which is the same bargain the CPU prefilter always made by admitting
FIRE_SELECT types rather than deciding them.

Verification
------------

The port's whole risk is that a kernel computes something plausible and
wrong -- there is no visual artefact for an arithmetic slip, and the pixel
tests cannot see gate channels at all. So the CPU implementation is kept as
the oracle, exactly as docs/belt_report.md prescribes for the belt phase:

    `compare_against_cpu()` runs one structure phase both ways from the same
    state and diffs every channel of every gate.

That is the same relationship `naive_tick()` has to the closed form, and it
is why the CPU path is not being deleted as it is ported.
"""

import numpy as np

from core.entity import MAX_CHANNELS, Entity, Source
from render import compute

#: Flag bits in the geometry word. Mirrored in shaders/gate_fire.comp and
#: pinned against it by tests/test_glsl.py -- a layout contract between two
#: files, like the segment record's fields.
FLAG_HOST_DECIDES = 1
FLAG_FIRE_ANY = 2
FLAG_SELECT = 4
FLAG_DRAIN_SHIFT = 8

#: ivec4s per gate in the geometry block: the counts and flags, then the
#: select rule. Mirrored by `GEOM_STRIDE` in shaders/gate_fire.comp.
GEOM_STRIDE = 2


def geometry_for(state, types):
    """The per-gate `ivec4` geometry block, from the SoA mirror.

    Pure, and takes the type classes rather than looking them up, so it can
    be checked without a board or a context.
    """
    n = len(state.rows)
    geom = np.zeros((max(n, 1) * GEOM_STRIDE, 4), dtype=np.int32)
    for i in range(n):
        cls = types[i]
        flags = 0
        if cls.FIRE_RULE == cls.FIRE_ANY:
            flags |= FLAG_FIRE_ANY
        rule = cls.SELECT_RULE
        if rule is not None:
            flags |= FLAG_SELECT
            geom[i * GEOM_STRIDE + 1] = (rule[0], rule[1], rule[2], 0)
        # A type that decides readiness or consumption from values rather
        # than occupancy stays on the host. Detected from the *class* rather
        # than from FIRE_RULE alone, because an override is the real
        # condition -- FIRE_SELECT is how a type announces it, but a type
        # that overrode `consumed()` without declaring it would otherwise
        # have its unused lane silently eaten.
        #
        # **`OPS` is tested against the output count, not for truthiness.**
        # An empty `OPS` is the correct and complete table for a type with no
        # outputs -- Sink declares none and destroys what arrives -- so
        # `not cls.OPS` reads that as "unsupported" and hands the commonest
        # consumer on any board back to the CPU, vetoing the dispatch for the
        # whole board. The real condition is a table that does not describe
        # every channel.
        #
        # **A declared `SELECT_RULE` excuses the overrides it explains.**
        # `inputs_ready()` and `consumed()` are overridden precisely *because*
        # the type selects a lane on a control's sign, and the rule states
        # that in a form the kernel evaluates -- so those two overrides are no
        # longer evidence the host is needed. Any *other* override still is:
        # the rule covers one shape, not every possible one.
        #
        # Without this, declaring the rule would change nothing, since the
        # override test alone would keep flagging Merge. That is the whole
        # reason the condition is phrased per-method rather than as "does
        # this class override anything".
        explained = Entity.inputs_ready, Entity.consumed
        if rule is not None:
            overridden = False
        else:
            overridden = (cls.inputs_ready is not explained[0]
                          or cls.consumed is not explained[1])
        if (overridden
                or (cls.FIRE_RULE == cls.FIRE_SELECT and rule is None)
                or len(cls.OPS) != cls.out_channels()):
            flags |= FLAG_HOST_DECIDES
        for c in range(MAX_CHANNELS):
            if state.drains[i, c]:
                flags |= 1 << (FLAG_DRAIN_SHIFT + c)
        geom[i * GEOM_STRIDE] = (cls.TYPE_ID, int(state.n_in[i]),
                                 int(state.n_out[i]), flags)
    return geom


def gate_flags(geom, slot):
    """The flags word for one gate slot.

    An accessor rather than `geom[slot][3]` spelled at each call site,
    because the block grew a second vector and the stride is now easy to
    forget -- the same reason `seg_core()` exists in common.glsl. Reading
    slot 1's flags out of the select rule of slot 0 would be a plausible
    number, not a crash.
    """
    return int(geom[slot * GEOM_STRIDE][3])


def gate_type_ids(geom, n):
    """The type id of each of the first `n` gate slots."""
    return [int(geom[i * GEOM_STRIDE][0]) for i in range(n)]


class GateDevice:
    """Runs the structure phase as a dispatch, and can check itself.

    Holds the buffers; `GateState` holds the arrays. The split matches
    `SegmentIndex`/`BeltDevice` one layer down -- the simulation object owns
    the state and the device object owns the GPU's view of it.
    """

    def __init__(self, ctx, state):
        self.ctx = ctx
        self.state = state
        self.kernel = compute.ComputeKernel(ctx, "gate_fire.comp")
        self.direct_kernel = compute.ComputeKernel(ctx, "gate_direct.comp")
        self.fold_kernel = compute.ComputeKernel(ctx, "gate_fold.comp")
        self.geom = np.zeros((1, 4), dtype=np.int32)
        self.konst = np.zeros(1, dtype=np.float32)
        #: Gate-to-gate edges in *slot* space, one per receiving lane.
        self.edges = np.zeros((1, 4), dtype=np.int32)
        self.edge_count = 0
        #: Folded constants as (slot, channel, value, 0). Re-asserted after
        #: every firing dispatch -- see shaders/gate_fold.comp.
        self.folds = np.zeros((1, 4), dtype=np.float32)
        self.fold_count = 0
        #: Slot -> entity row, for publishing the sleep flag. Packed by
        #: `rebuild()` alongside `geom`, never from the live mirror.
        self.gate_rows = np.zeros(1, dtype=np.int32)
        #: The sleep layer, or None when it is bypassed. Set by `Engine`.
        self.sleep = None

        b = compute
        self._bufs = {
            "in_value": compute.ComputeBuffer(
                ctx, state.in_value, b.GATE_IN_VALUE_BINDING),
            "in_held": compute.ComputeBuffer(
                ctx, state.in_held, b.GATE_IN_HELD_BINDING),
            "out_value": compute.ComputeBuffer(
                ctx, state.out_value, b.GATE_OUT_VALUE_BINDING),
            "out_held": compute.ComputeBuffer(
                ctx, state.out_held, b.GATE_OUT_HELD_BINDING),
            "geom": compute.ComputeBuffer(
                ctx, self.geom, b.GATE_GEOM_BINDING),
            "konst": compute.ComputeBuffer(
                ctx, self.konst, b.GATE_KONST_BINDING),
            "edges": compute.ComputeBuffer(
                ctx, self.edges, b.GATE_EDGE_BINDING),
            "folds": compute.ComputeBuffer(
                ctx, self.folds, b.GATE_FOLD_BINDING),
            #: Entity row per slot, so `gate_fire.comp` can publish the sleep
            #: flag into the row the renderer reads. int32 rather than the
            #: mirror's int64, because GLSL has no 64-bit int in std430 here
            #: and a row index never approaches 2^31.
            "gate_rows": compute.ComputeBuffer(
                ctx, np.zeros(1, dtype=np.int32), b.GATE_ROWS_BINDING),
        }
        #: Set by every dispatch, cleared by a readback. While true the host
        #: arrays are stale -- see the residency note in render/compute.py.
        self._gpu_ahead = False

    # --- layout -----------------------------------------------------------
    def gate_count(self):
        """How many gates the packed geometry actually describes.

        The authority for every dispatch size and every `geom` read, because
        it is a property of what was uploaded rather than of the live mirror
        -- see `run()` on why the difference bites.
        """
        packed = self.geom.shape[0] // GEOM_STRIDE
        return min(len(self.state.rows), packed)

    def set_folds(self, folds):
        """Record the folded constants, in slot space."""
        self.fold_count = len(folds)
        arr = np.zeros((max(len(folds), 1), 4), dtype=np.float32)
        for i, (slot, channel, value) in enumerate(folds):
            arr[i] = (slot, channel, value, 0.0)
        self.folds = arr
        self._bufs["folds"].set_array(self.folds)

    def rebuild(self, types, board, edges=None):
        """Re-pack the per-edit geometry and resize every buffer. Edit-time."""
        state = self.state
        self.geom = geometry_for(state, types)
        n = len(state.rows)
        self.konst = np.zeros(max(n, 1), dtype=np.float32)
        for i, row in enumerate(state.rows.tolist()):
            if types[i] is Source or issubclass(types[i], Source):
                self.konst[i] = board.entities[row, Source.F_EMIT]
        self.edges, self.edge_count = self._pack_edges(edges)
        #: Slot -> row, for the sleep flag's trip back to the entity table.
        #: Packed here with the rest of the per-edit geometry, so it can never
        #: describe a different slot assignment than `geom` does -- the
        #: staleness hazard `gate_count()` exists for.
        self.gate_rows = np.zeros(max(n, 1), dtype=np.int32)
        if n:
            self.gate_rows[:n] = state.rows.astype(np.int32)
        self._bufs["geom"].set_array(self.geom)
        self._bufs["konst"].set_array(self.konst)
        self._bufs["edges"].set_array(self.edges)
        self._bufs["gate_rows"].set_array(self.gate_rows)
        for name in ("in_value", "in_held", "out_value", "out_held"):
            self._bufs[name].set_array(getattr(state, name))

    def _pack_edges(self, edges):
        """Translate the host edge list from entity rows into gate slots.

        `GateSim._direct_edges()` speaks rows, because that is what the CPU
        path and every cache around it use; the kernel indexes the SoA arrays
        and so wants slots. Translating once here rather than per thread is
        the same bargain the geometry block makes.

        **An edge touching a row that is not in the gate table is dropped.**
        That is not defensive: a folded Source is deliberately absent from
        the table (it never fires, so it has nothing to hand over), and
        keeping its edges would index slot -1.
        """
        if not edges or not len(edges[0]):
            return np.zeros((1, 4), dtype=np.int32), 0
        src_row, src_ch, dst_row, dst_ch = edges
        slot_of = self.state.slot_of
        cap = slot_of.shape[0]
        ok = (src_row < cap) & (dst_row < cap)
        s = np.where(ok, slot_of[np.clip(src_row, 0, cap - 1)], -1)
        d = np.where(ok, slot_of[np.clip(dst_row, 0, cap - 1)], -1)
        live = (s >= 0) & (d >= 0)
        n = int(live.sum())
        if not n:
            return np.zeros((1, 4), dtype=np.int32), 0
        out = np.zeros((n, 4), dtype=np.int32)
        out[:, 0] = s[live]
        out[:, 1] = src_ch[live]
        out[:, 2] = d[live]
        out[:, 3] = dst_ch[live]
        return out, n

    # --- the dispatch -----------------------------------------------------
    def upload(self):
        """Host -> device for every buffer. The start of a residency run."""
        state = self.state
        for name in ("in_value", "in_held", "out_value", "out_held"):
            self._bufs[name].upload(getattr(state, name))
        self._bufs["geom"].upload(self.geom)
        self._bufs["konst"].upload(self.konst)
        self._bufs["edges"].upload(self.edges)
        self._bufs["folds"].upload(self.folds)
        self._bufs["gate_rows"].upload(self.gate_rows)
        self._gpu_ahead = False

    def run(self, ticks=1, direct=True):
        """Dispatch the tick's gate work `ticks` times, with no readback.

        **This is the point of the exercise.** Nothing is mapped and the host
        learns nothing; consecutive dispatches are separated only by a memory
        barrier, which is the explicit form of the sequencing the CPU got by
        having one phase return before the next was called.

        Each tick is two dispatches with a barrier between: fire, then the
        direct hand-off. That order is the CPU's -- `deliver_direct()` runs
        after the structure phase, so a value makes one hop per tick -- and
        the barrier is what makes the second see the first's writes.

        `direct=False` runs the firing half alone, for the oracle comparison
        which checks one phase at a time.
        """
        # **Dispatch what was *packed*, not what the mirror now holds.**
        # `state.rows` is rebuilt by `_gate_table()`, which anything can
        # trigger between an edit and the frame's `sync_device()`; `geom` and
        # the SoA arrays only change when this object is rebuilt. Sizing the
        # dispatch by the row count let threads read geometry past the end of
        # the block -- a gate running with another machine's type, channel
        # counts and drains mask. That is the "connections made improperly,
        # items appearing where they shouldn't" symptom, and it cleared on a
        # force rebuild because that re-packed the two into agreement.
        n = self.gate_count()
        if not n:
            return
        for buf in self._bufs.values():
            buf.bind()
        sleep = self.sleep
        if sleep is not None:
            sleep.bind()
        self.kernel["gate_count"] = int(n)
        self.kernel["sleep_on"] = 1 if sleep is not None else 0
        if sleep is not None:
            self.kernel["wake_gate_base"] = int(sleep.gate_base)
        groups = compute.groups_for(n)
        run_direct = direct and self.edge_count > 0
        if run_direct:
            self.direct_kernel["edge_count"] = int(self.edge_count)
            self.direct_kernel["sleep_on"] = 1 if sleep is not None else 0
            if sleep is not None:
                self.direct_kernel["wake_gate_base"] = int(sleep.gate_base)
            edge_groups = compute.groups_for(self.edge_count)
        run_folds = self.fold_count > 0
        if run_folds:
            self.fold_kernel["fold_count"] = int(self.fold_count)
            self.fold_kernel["sleep_on"] = 1 if sleep is not None else 0
            if sleep is not None:
                self.fold_kernel["wake_gate_base"] = int(sleep.gate_base)
            fold_groups = compute.groups_for(self.fold_count)
        for _ in range(int(ticks)):
            if sleep is None:
                self.kernel.run(groups)
            else:
                # Indirect: the group count was written by `sleep_groups.comp`
                # from the compaction's own atomic, so the awake count never
                # round-trips through the host.
                #
                # `wake_epoch` is the stamp this tick's kernels post with and
                # the *next* compaction accepts. **Read, never bumped here**:
                # the epoch advances in exactly one place, `Engine._gpu_tick()`
                # where the compaction is called, because both phases of one
                # tick must post the same stamp. Bumping it per phase put the
                # belt phase a tick ahead of the gate phase, so a run woken by
                # a gate was compacted on the following tick instead.
                self.kernel["wake_epoch"] = int(sleep.epoch)
                self.kernel.run_indirect(
                    sleep.args_buffer(), sleep.gate_arg_offset())
            compute.barrier(self.ctx)
            # Put the folded constants back, exactly as `_assert_folded()`
            # does after the CPU's structure phase: the channel must read as
            # held while the firing kernel decides, and be restored once
            # consumption has cleared it. Without this the kernel eats each
            # folded input once and the gate is starved for ever.
            if run_folds:
                if sleep is not None:
                    self.fold_kernel["wake_epoch"] = int(sleep.epoch)
                self.fold_kernel.run(fold_groups)
                compute.barrier(self.ctx)
            if run_direct:
                # Same epoch the firing kernel just posted with: both halves
                # of one tick arm for the *same* coming tick, so a value
                # delivered here and a gate that fired above are compacted
                # together rather than a tick apart.
                if sleep is not None:
                    self.direct_kernel["wake_epoch"] = int(sleep.epoch)
                self.direct_kernel.run(edge_groups)
                compute.barrier(self.ctx)
        self._gpu_ahead = True

    def run_direct_only(self):
        """Dispatch the hand-off alone, for the oracle comparison."""
        if not self.edge_count:
            return
        for buf in self._bufs.values():
            buf.bind()
        self.direct_kernel["edge_count"] = int(self.edge_count)
        self.direct_kernel.run(compute.groups_for(self.edge_count))
        compute.barrier(self.ctx)
        self._gpu_ahead = True

    def bind_rows(self):
        """Bind the slot->row map alone, for the sleep layer's publish pass."""
        self._bufs["gate_rows"].bind()

    def readback(self):
        """Device -> host. The deliberate, expensive act of asking.

        Never implicit: a readback stalls on every dispatch that has touched
        the buffers, which is the cost residency exists to avoid.
        """
        state = self.state
        for name in ("in_value", "in_held", "out_value", "out_held"):
            self._bufs[name].download(getattr(state, name))
        self._gpu_ahead = False

    @property
    def gpu_ahead(self):
        """True when the device holds state the host has not read back."""
        return self._gpu_ahead

    def release(self):
        for buf in self._bufs.values():
            buf.release()
        self.kernel.release()
        self.direct_kernel.release()
        self.fold_kernel.release()


def compare_against_cpu(gates, board, phase="fire"):
    """One phase both ways from identical state. Returns problems.

    `phase` is `"fire"` (the structure phase) or `"direct"` (the gate-to-gate
    hand-off), compared one at a time so a disagreement names which kernel
    produced it.

    The oracle seam. The CPU path is the reference -- it is what every test
    in the suite exercises and what the differential checkers already trust
    -- so the question is only ever "does the kernel agree with it".

    Snapshots the channel columns, runs the CPU phase, snapshots the result,
    restores, runs the GPU phase, and diffs. Restoring in between is what
    makes it a comparison of two implementations rather than of two
    consecutive ticks.
    """
    state = gates.state
    device = gates.device
    if device is None or not len(state.rows):
        return []

    rows = state.rows
    cols = (Entity.F_IN_VALUE, Entity.F_IN_HELD,
            Entity.F_OUT_VALUE, Entity.F_OUT_HELD)
    before = {c: board.entities[rows, c:c + MAX_CHANNELS].copy() for c in cols}

    if phase == "direct":
        gates.deliver_direct()
    else:
        gates.structure_phase()
    after_cpu = {c: board.entities[rows, c:c + MAX_CHANNELS].copy()
                 for c in cols}

    for c in cols:
        board.entities[rows, c:c + MAX_CHANNELS] = before[c]
    state.sync_from_rows()
    device.upload()
    if phase == "direct":
        device.run_direct_only()
    else:
        device.run(1, direct=False)
    device.readback()
    state.flush()
    after_gpu = {c: board.entities[rows, c:c + MAX_CHANNELS].copy()
                 for c in cols}

    names = {Entity.F_IN_VALUE: "in_value", Entity.F_IN_HELD: "in_held",
             Entity.F_OUT_VALUE: "out_value", Entity.F_OUT_HELD: "out_held"}
    problems = []
    for c in cols:
        bad = ~np.isclose(after_cpu[c], after_gpu[c], equal_nan=True)
        for i, ch in zip(*np.nonzero(bad)):
            problems.append(
                f"gate slot {i} (row {int(rows[i])}) {names[c]}[{ch}]: "
                f"cpu {after_cpu[c][i, ch]}, gpu {after_gpu[c][i, ch]}")
    return problems


def _self_check():
    """The geometry packing. No context, so no dispatch here."""
    from core.entity import Merge, Shear, Sink

    class _State:
        def __init__(self, n):
            self.rows = np.arange(1, n + 1, dtype=np.int64)
            self.n_in = np.zeros(n, dtype=np.int64)
            self.n_out = np.zeros(n, dtype=np.int64)
            self.drains = np.zeros((n, MAX_CHANNELS), dtype=bool)

    st = _State(3)
    st.n_in[:] = (2, 4, 3)
    st.n_out[:] = (2, 0, 2)
    st.drains[0, 0] = True
    st.drains[0, 1] = True
    st.drains[2, 1] = True
    geom = geometry_for(st, [Shear, Sink, Merge])

    # The counts travel verbatim.
    assert geom[0].tolist()[:3] == [Shear.TYPE_ID, 2, 2], geom[0]

    # Sink is FIRE_ANY and must say so, or it would wait for all four faces
    # and deadlock when fed from one side.
    assert gate_flags(geom, 1) & FLAG_FIRE_ANY, geom[GEOM_STRIDE]
    assert not gate_flags(geom, 0) & FLAG_FIRE_ANY, "Shear is FIRE_ALL"

    # **Merge is expressible now**, via its declared SELECT_RULE -- the rule
    # is what excuses the two overrides it explains, so the kernel handles it
    # rather than handing the whole board back to the host.
    assert gate_flags(geom, 2) & FLAG_SELECT, geom[2 * GEOM_STRIDE]
    assert not gate_flags(geom, 2) & FLAG_HOST_DECIDES, (
        "a declared SELECT_RULE should make Merge a kernel type")
    assert not gate_flags(geom, 0) & FLAG_SELECT, "Shear selects nothing"
    # ...and the rule's channels reach the second vector in order.
    assert geom[2 * GEOM_STRIDE + 1].tolist()[:3] == list(Merge.SELECT_RULE)

    # The drains mask lands in the right bits, and only for drained channels.
    assert gate_flags(geom, 0) & (1 << (FLAG_DRAIN_SHIFT + 0))
    assert gate_flags(geom, 0) & (1 << (FLAG_DRAIN_SHIFT + 1))
    assert not gate_flags(geom, 0) & (1 << (FLAG_DRAIN_SHIFT + 2))
    assert gate_flags(geom, 2) & (1 << (FLAG_DRAIN_SHIFT + 1))
    assert not gate_flags(geom, 2) & (1 << (FLAG_DRAIN_SHIFT + 0))

    # The mask must not collide with the low flag bits -- an off-by-one in
    # FLAG_DRAIN_SHIFT would have channel 0 read as "host decides".
    assert FLAG_DRAIN_SHIFT > max(
        FLAG_HOST_DECIDES, FLAG_FIRE_ANY, FLAG_SELECT).bit_length()

    # An empty gate set still packs a valid (unused) record rather than a
    # zero-length buffer, which moderngl cannot reserve.
    assert geometry_for(_State(0), []).shape == (GEOM_STRIDE, 4)


_self_check()
