"""GPU sleep: the mailbox, the compacted awake sets, and the indirect args.

The tick already runs as compute dispatches over *every* gate and *every*
segment (see "The tick on the GPU" in CLAUDE.md). This is the machinery that
makes the dispatch size follow the number of entities that can actually do
something, which on a board built as a computer is a percent or two of the
board.

Why this is dynamic, against docs/sleep2gpu.md's advice
-------------------------------------------------------

That brief recommends a **static** per-edit proof and explicitly says "do not
port the dynamic sleep set". Its reasoning is a real measurement -- on
cpu.npz, 203 gates asleep with 6 changing state every tick and only 1 tick in
100 unchanged, so the awake slice could not be memoised (0 cache hits in 150
ticks). The conclusion drawn from it does not follow, for two reasons:

- **That measurement was taken where ~40% of the table is awake.** The churn
  is the *active trace*, which is a roughly constant few entities per tick
  whatever the board size. A static proof cannot express the thing actually
  wanted -- a RAM bank is structurally connected to the CPU that reads it, so
  no graph-local argument will ever call it inert, yet it is inert for
  thousands of ticks at a stretch between accesses.
- **The CPU's cost was `np.isin` being O(table)** to find the awake slice.
  That is an artefact of maintaining a Python set over a numpy table, not a
  property of the workload.

So the rule this module is built to is sharper than "dynamic is too
expensive": **the per-tick cost must be O(awake), never O(total).** Every
design decision below follows from it, and the one that makes it achievable
is a property of the tick rather than of this code -- see the frontier
argument in `SleepSets`.

The mailbox
-----------

Waking is **push-based**. A sleeping entity cannot read anything -- that is
what sleeping means -- so it can never notice that its producer filled a
zone. The entity that *does* the filling is awake by definition, and it knows
who it just fed, so it is the one that posts the wake.

    wake[sid]                  == epoch  ->  run sid is awake next tick
    wake[gate_base + slot]     == epoch  ->  gate slot is awake next tick

**One array for both families**, segments first and gates after
`gate_base`. Forced rather than chosen: `GL_MAX_COMPUTE_SHADER_STORAGE_BLOCKS`
is 16 on this hardware and `gate_fire.comp` needs 4 of them for the blocks
`common.glsl` declares, so two separate mailboxes put it at 17 and it refused
to link with a bare "no buffers available for bindable storage buffer" --
naming neither the limit nor the cause. Two int32 arrays indexed by entity
concatenate for free, and "the mailbox" was already singular.

It is indexed by entity, so it is O(total) in *storage*. It is never scanned,
which is the whole point: storage is free and traffic is not. Nothing
iterates the mailbox looking for set entries.

**An epoch rather than a bool**, so clearing is free. A tick stamps
`epoch + 1` into the mailbox of everything that must run next tick, and the
compaction pass accepts an entry only if it carries the epoch it is building.
A stale entry from fifty ticks ago is simply a number that no longer matches,
so the mailbox needs no clearing pass -- which would be O(total) per tick and
would defeat the module's one rule.

Why not an early return in the existing kernels
-----------------------------------------------

`if (asleep) return;` was the first thing tried on paper and it is a trap:
the warp still launches, the scheduler still occupies it, and the sleeping
lanes diverge against the awake ones in the same warp. The dispatch has to
*shrink*, which means one thread per awake entity and the ids coming out of a
compacted buffer. That is what `awake_seg` / `awake_gate` are.

The counts live on the device
-----------------------------

`awake_gate_count` is produced by an atomic in a compute pass, so reading it
on the host to size the next dispatch would stall every tick -- exactly the
residency the port exists to protect. The counts therefore go straight into
an **indirect dispatch argument buffer** (`glDispatchComputeIndirect`, which
moderngl exposes as `ComputeShader.run_indirect`), and the host never learns
them during a residency run.

That is why `_args` is laid out as it is: an indirect dispatch reads
`(group_x, group_y, group_z)` as three uints at a given offset, so the
compaction kernel divides its own atomic counter by the local size and writes
the group count where the next dispatch will read it.

Reading a count for the *overlay* is a different act, and a deliberately
cheap one: one 4-byte read per frame, not per tick. See `counts()`.
"""

import numpy as np

from render import compute

#: Indirect dispatch argument slots. Each dispatch needs three uints
#: `(group_x, group_y, group_z)` at a 16-byte-aligned offset, and the counts
#: ride alongside so the compaction kernel writes both in one place.
#:
#: One `uvec4` per consumer: `.x` is the group count the indirect dispatch
#: reads, `.y`/`.z` are the other two group dimensions (always 1), and `.w`
#: carries the raw entity count the kernel bounds-checks against.
#:
#: **`.w` rather than a separate uniform** because the count is produced on
#: the device: a uniform would have to come from the host, which is the
#: readback this design exists to avoid.
ARG_GROUPS, ARG_COUNT = 0, 3
ARG_SEG, ARG_GATE = 0, 1
#: The previous tick's counts, which the compaction's own dispatch is sized
#: from. Carried in the buffer rather than on the host for the same reason the
#: current counts are: the host must never need to know them.
ARG_PREV_SEG, ARG_PREV_GATE = 2, 3
ARG_SLOTS = 4

#: Byte offset of each consumer's dispatch args, for `run_indirect`.
ARG_STRIDE_BYTES = 16


def arg_offset(which):
    """Byte offset `run_indirect` reads this consumer's group count from."""
    return int(which) * ARG_STRIDE_BYTES


class SleepSets:
    """The mailbox, the two awake sets, and the indirect dispatch args.

    Why the per-tick cost is bounded, which is the load-bearing argument
    ---------------------------------------------------------------------

    The compaction pass is **not** a scan over the board. The only entities
    that can be awake at tick `t+1` are those awake at `t`, or a one-hop
    neighbour of one -- because material moves exactly one hop per tick, which
    is what the two-phase tick guarantees (see core/tick.py: a gate fills a
    zone in the structure phase and the belt phase moves it, one hop, every
    time, for a belt hop and a direct gate-to-gate hop alike).

    So the wake frontier cannot advance faster than one edge per tick, and the
    compaction's thread count is O(awake x ports) -- a small multiple of the
    awake set, completely independent of how big the board is. That is the
    property docs/sleep2gpu.md assumed was unavailable and is in fact a
    consequence of the tick's own design.

    It also means the compaction must be dispatched over the *previous* awake
    set together with its neighbourhood, never over the table. `rebuild()`
    uploads the adjacency that defines "neighbour".

    Everything here is slot-keyed and that is a hazard
    --------------------------------------------------

    `GateState.build()` reassigns slots on any gate-set change, so an awake
    set packed against the old assignment describes a different population --
    the trap `GateDevice.gate_count()` exists for, with a worse consequence
    here: not a gate reading another machine's geometry, but a gate being
    skipped because some unrelated row was asleep. So the awake set is packed
    in `rebuild()` and every dispatch is sized from what was packed.

    On an edit the honest answer is to wake everything (`wake_all()`), which
    is also the correct one: a rebuild drops every item and reassigns every
    segment id, so nothing that was asleep is still the thing it was.
    """

    def __init__(self, ctx):
        self.ctx = ctx
        self.kernel = compute.ComputeKernel(ctx, "sleep_compact.comp")
        self.reset_kernel = compute.ComputeKernel(ctx, "sleep_reset.comp")
        self.groups_kernel = compute.ComputeKernel(ctx, "sleep_groups.comp")
        #: The overlay publish, and the one O(total) pass here -- so it runs
        #: only while the player is actually looking at the overlay. See
        #: shaders/sleep_publish.comp.
        self.publish_kernel = compute.ComputeKernel(ctx, "sleep_publish.comp")
        self.publish_awake_kernel = compute.ComputeKernel(
            ctx, "sleep_publish_awake.comp")
        #: Set by the orchestrator from the overlay's state. A diagnostic must
        #: not become the cost of the thing it measures.
        self.publish_flags = False

        #: The epoch the *next* tick will run under. Bumped once per tick, so
        #: a mailbox entry is "for the coming tick" iff it equals this.
        self.epoch = 1

        #: **One mailbox for both families**: segment ids occupy
        #: `[0, seg_count)` and gate slots `[gate_base, gate_base + n_gates)`.
        #: Never scanned.
        #:
        #: Merged because `GL_MAX_COMPUTE_SHADER_STORAGE_BLOCKS` is 16 here
        #: and `gate_fire.comp` was at 17 with two -- it failed to link with a
        #: bare "no buffers available for bindable storage buffer", which
        #: names neither the limit nor the shader. Two int32 arrays indexed by
        #: entity concatenate for free.
        self.wake = np.zeros(2, dtype=np.int32)
        #: Where the gate region starts. A host-side number pushed to the
        #: kernels as a uniform, so no shader hardcodes the split.
        self.gate_base = 1
        #: The compacted awake sets, **double-buffered**. The compaction reads
        #: the frontier out of one and writes survivors into the other, then
        #: the pair is swapped: compacting in place aliases, since one thread
        #: would overwrite an entry another has yet to read, and no barrier
        #: available to a dispatch can order that. Costs one id array.
        self.awake_seg = np.zeros(1, dtype=np.int32)
        self.awake_gate = np.zeros(1, dtype=np.int32)
        self.next_seg = np.zeros(1, dtype=np.int32)
        self.next_gate = np.zeros(1, dtype=np.int32)
        #: Indirect dispatch args: one uvec4 per consumer.
        self._args = np.zeros((ARG_SLOTS, 4), dtype=np.uint32)

        #: Per-gate adjacency for the compaction's frontier: the segments and
        #: gates one hop from each gate. Packed per edit by `rebuild()`.
        self.gate_nbr = np.zeros((1, 4), dtype=np.int32)
        self.gate_nbr_at = np.zeros(2, dtype=np.int32)

        self.seg_count = 0
        self.gate_count = 0

        b = compute
        self._bufs = {
            "wake": compute.ComputeBuffer(
                ctx, self.wake, b.SLEEP_WAKE_BINDING),
            "awake_seg": compute.ComputeBuffer(
                ctx, self.awake_seg, b.SLEEP_AWAKE_SEG_BINDING),
            "awake_gate": compute.ComputeBuffer(
                ctx, self.awake_gate, b.SLEEP_AWAKE_GATE_BINDING),
            "next_seg": compute.ComputeBuffer(
                ctx, self.next_seg, b.SLEEP_NEXT_SEG_BINDING),
            "next_gate": compute.ComputeBuffer(
                ctx, self.next_gate, b.SLEEP_NEXT_GATE_BINDING),
            "gate_nbr": compute.ComputeBuffer(
                ctx, self.gate_nbr, b.SLEEP_GATE_NBR_BINDING),
            "gate_nbr_at": compute.ComputeBuffer(
                ctx, self.gate_nbr_at, b.SLEEP_GATE_NBR_AT_BINDING),
        }
        #: The argument buffer is not an SSBO to the compaction kernel alone
        #: -- it is also the dispatch-indirect source, so it needs that usage.
        #: moderngl buffers carry no usage flags, so one buffer serves both.
        self._args_buf = compute.ComputeBuffer(
            ctx, self._args, b.SLEEP_ARGS_BINDING)

    # --- layout -----------------------------------------------------------
    def rebuild(self, seg_count, gate_count, gate_nbr, gate_nbr_at):
        """Resize for a new population and wake all of it. Edit-time.

        `gate_nbr` / `gate_nbr_at` are the per-gate adjacency CSR built by
        `sleep_graph.gate_neighbours()`. Taken as arguments rather than
        derived here for the same reason `GateState.build()` takes its
        geometry: one authority on the graph, so this cannot disagree with the
        wake pushes about who is adjacent to whom.
        """
        self.seg_count = int(seg_count)
        self.gate_count = int(gate_count)

        self.gate_base = max(self.seg_count, 1)
        self.wake = np.zeros(
            self.gate_base + max(self.gate_count, 1), dtype=np.int32)
        self.awake_seg = np.zeros(max(self.seg_count, 1), dtype=np.int32)
        self.awake_gate = np.zeros(max(self.gate_count, 1), dtype=np.int32)
        self.next_seg = np.zeros(max(self.seg_count, 1), dtype=np.int32)
        self.next_gate = np.zeros(max(self.gate_count, 1), dtype=np.int32)
        self.gate_nbr = np.ascontiguousarray(gate_nbr, dtype=np.int32)
        self.gate_nbr_at = np.ascontiguousarray(gate_nbr_at, dtype=np.int32)

        # `set_array` only resizes the SSBO -- it does **not** transfer. The
        # adjacency is the one thing here the device cannot derive and nothing
        # else uploads, so it has to be pushed explicitly.
        #
        # Missing this was a silent, total failure of the gate->run wake: the
        # device read an all-zero `gate_nbr_at`, so every gate had an *empty*
        # neighbour list and `wake_neighbours()` walked nothing. The sleep
        # sets, the mailbox and the compaction were all correct, which is what
        # made it hard to see -- gates armed themselves, runs never got armed,
        # and the differential check reported an empty at-rest run that never
        # accepted the item its producer was holding.
        for name in ("wake", "awake_seg", "awake_gate", "next_seg",
                     "next_gate", "gate_nbr", "gate_nbr_at"):
            buf = self._bufs[name]
            buf.set_array(getattr(self, name))
            buf.upload(getattr(self, name))
        self.wake_all()

    def wake_all(self):
        """Every entity awake for the coming tick, with no compaction pass.

        The edit-time hammer, and the correct one: a rebuild reassigns every
        segment id and drops every item, so no sleep decision taken before it
        still describes the same entity.

        Written directly into the *awake* sets rather than into the mailbox,
        because the mailbox is only ever read by the compaction -- and this
        has to take effect for the coming tick without one running.
        """
        n_seg, n_gate = max(self.seg_count, 0), max(self.gate_count, 0)
        self.awake_seg = np.arange(max(n_seg, 1), dtype=np.int32)
        self.awake_gate = np.arange(max(n_gate, 1), dtype=np.int32)
        self._bufs["awake_seg"].set_array(self.awake_seg)
        self._bufs["awake_gate"].set_array(self.awake_gate)
        self._set_args(n_seg, n_gate)
        self._bufs["awake_seg"].upload(self.awake_seg)
        self._bufs["awake_gate"].upload(self.awake_gate)
        self._args_buf.upload(self._args)
        # Nothing may carry a stale "I am for this epoch" stamp into the first
        # compaction, or an entity that should have slept would be admitted on
        # the strength of a mailbox entry older than the rebuild.
        self.epoch += 1
        self.wake = np.zeros_like(self.wake)
        self._bufs["wake"].upload(self.wake)

    def _set_args(self, n_seg, n_gate):
        """Fill the indirect args for a known (host-side) awake count.

        Both the *current* slots (which the tick kernels dispatch over) and
        the *previous* slots (which the compaction's own frontier walk
        dispatches over) are filled, because after `wake_all()` those are the
        same set -- and leaving the previous slots at zero would give the next
        compaction an empty frontier, so nothing would ever be claimed and the
        whole board would sleep on the tick after an edit.
        """
        for cur, prev, n in ((ARG_SEG, ARG_PREV_SEG, n_seg),
                             (ARG_GATE, ARG_PREV_GATE, n_gate)):
            arg = (compute.groups_for(max(n, 0)), 1, 1, max(n, 0))
            self._args[cur] = arg
            self._args[prev] = arg

    # --- the dispatch -----------------------------------------------------
    def bind(self):
        for buf in self._bufs.values():
            buf.bind()
        self._args_buf.bind()

    def args_buffer(self):
        """The raw buffer an indirect dispatch reads its group count from."""
        return self._args_buf.buffer

    def gate_arg_offset(self):
        """Byte offset of the awake-gate group count."""
        return arg_offset(ARG_GATE)

    def seg_arg_offset(self):
        """Byte offset of the awake-segment group count."""
        return arg_offset(ARG_SEG)

    def upload(self):
        """Host -> device for every buffer. The start of a residency run."""
        for name, buf in self._bufs.items():
            buf.upload(getattr(self, name))
        self._args_buf.upload(self._args)

    def compact(self, epoch, extra_binds=()):
        """Rebuild both awake sets for `epoch`, on the device.

        `extra_binds` are callables binding buffers this object does not own
        but the publish pass reads -- the entity table, the segment records
        and the gate row map. Passed in rather than reached for, so this class
        keeps knowing only about its own buffers (the same discipline
        `BeltCompute` follows by borrowing `BeltDevice`'s).

        Four dispatches, and each boundary is a real ordering requirement
        rather than caution:

        1. **reset** -- this tick's counts become next tick's frontier extent,
           and the counts the compaction will bump start at zero. Separate
           because work groups have no ordering between them, so a thread
           cannot zero a counter another thread is already bumping;
           `barrier()` synchronises one group, not a dispatch.
        2. **compact over the gate frontier** -- each previously-awake gate
           and everything one hop from it.
        3. **compact over the segment frontier** -- each previously-awake run
           and the gates at its ends. A second dispatch of the same kernel
           because a run's neighbours live in its own port table, not in the
           gate adjacency.
        4. **groups** -- `ceil(count / LOCAL)` for the tick kernels. After its
           own barrier, because the count is only knowably final once every
           claim has landed.

        O(awake) throughout, which is the module's one rule. The host learns
        no count at any point.
        """
        if not self.seg_count and not self.gate_count:
            return
        # **The one place the epoch advances.** Both phases of the coming tick
        # read it and post with it, so it must not move between them -- a
        # per-phase bump put the belt phase a tick ahead and a run woken by a
        # gate was compacted on the tick after the one it should have been.
        self.epoch = int(epoch)
        self.bind()
        args = self._args_buf.buffer

        self.reset_kernel.run(1)
        compute.barrier(self.ctx)

        for key, val in (("epoch", int(epoch)),
                         ("seg_total", int(self.seg_count)),
                         ("gate_total", int(self.gate_count)),
                         ("wake_gate_base", int(self.gate_base))):
            self.kernel[key] = val
        # Both frontier walks are themselves indirect: their extent is the
        # previous tick's awake count, which lives on the device.
        #
        # **No barrier between the two**, and that is a real argument rather
        # than a saving taken on faith. A barrier costs ~1us of the ~2us a
        # dispatch costs on this hardware, measured, so it is worth knowing
        # exactly which ones are needed.
        #
        # These two passes are independent: each reads a *different* frontier
        # buffer (`prev_gate` vs `prev_seg`, neither written here) and the only
        # state they share is written with atomics -- `atomicExchange` on the
        # mailbox to claim, `atomicAdd` on the count to append. Atomics are
        # coherent without a barrier, which is the whole reason the claim is
        # one: it has to admit exactly one of however many threads reach a
        # candidate, *including* threads from these two dispatches. So they
        # may overlap freely, and a candidate reachable from both a gate and a
        # segment is still claimed once.
        #
        # A barrier *is* needed after the pair, before `groups_kernel` reads
        # the final counts -- see there.
        self.kernel["pass_segments"] = 0
        self.kernel.run_indirect(args, arg_offset(ARG_PREV_GATE))
        self.kernel["pass_segments"] = 1
        self.kernel.run_indirect(args, arg_offset(ARG_PREV_SEG))
        compute.barrier(self.ctx)

        # The group counts, from the now-final atomic totals. **This barrier
        # is required**: the next dispatch reads the group count as an
        # *indirect argument*, which is a different access than an SSBO read
        # and needs the write to have landed. Dropping it is the one barrier
        # here that would fail intermittently rather than visibly.
        self.groups_kernel.run(1)
        compute.barrier(self.ctx)
        # The survivors were written to the `next_*` buffers; swap so the tick
        # kernels read them at the bindings they expect. A host-side name swap
        # only -- no data moves, and the GPU never sees a copy.
        self._swap()
        if self.publish_flags:
            self._publish(extra_binds)

    def _publish(self, extra_binds=()):
        """Write the awake sets out as per-entity flags, for the overlay.

        Mark-everything then clear-the-members, because the set is a list of
        ids rather than a mask and a single pass would need a search per
        entity. See shaders/sleep_publish.comp for why this is published from
        the compacted set rather than by each tick kernel -- the short version
        is that "I am at rest" and "the tick will skip me" are different
        claims, and only the second is what the overlay promises.

        Must run **after** `_swap()`: the flags describe what the *next* tick
        will dispatch over, which is the buffer the swap has just installed.
        """
        self.bind()
        for fn in extra_binds:
            fn()
        for k in (self.publish_kernel, self.publish_awake_kernel):
            k["seg_total"] = int(self.seg_count)
            k["gate_total"] = int(self.gate_count)
        self.publish_kernel.run(
            compute.groups_for(max(self.seg_count, self.gate_count)))
        compute.barrier(self.ctx)
        args = self._args_buf.buffer
        for family, which in ((0, ARG_SEG), (1, ARG_GATE)):
            self.publish_awake_kernel["family"] = family
            self.publish_awake_kernel.run_indirect(args, arg_offset(which))
            compute.barrier(self.ctx)

    def _swap(self):
        """Exchange the frontier and survivor buffers, host-side only.

        The two `ComputeBuffer`s trade bindings, so the next compaction reads
        what this one wrote and writes over what it read. Nothing is copied:
        this is a relabelling, which is the whole saving of double-buffering
        over a copy-back.
        """
        a, b = self._bufs["awake_seg"], self._bufs["next_seg"]
        a.binding, b.binding = b.binding, a.binding
        self._bufs["awake_seg"], self._bufs["next_seg"] = b, a
        a, b = self._bufs["awake_gate"], self._bufs["next_gate"]
        a.binding, b.binding = b.binding, a.binding
        self._bufs["awake_gate"], self._bufs["next_gate"] = b, a

    def counts(self):
        """`(awake_segments, awake_gates)`, read back from the device.

        **One 32-byte read, and never per tick.** The counts are produced by
        an atomic, so asking for them stalls; the panel's readout and the
        overlay want them once a *frame*, which is a thousand times cheaper
        than once a tick and is the trade this method exists to make explicit.
        """
        args = self._args_buf.download()
        return int(args[ARG_SEG][ARG_COUNT]), int(args[ARG_GATE][ARG_COUNT])

    def release(self):
        for buf in self._bufs.values():
            buf.release()
        self._args_buf.release()
        self.kernel.release()
        self.reset_kernel.release()
        self.groups_kernel.release()
        self.publish_kernel.release()
        self.publish_awake_kernel.release()

    def __repr__(self):
        return (f"<SleepSets {self.seg_count} segments, "
                f"{self.gate_count} gates, epoch {self.epoch}>")


def _self_check():
    """The argument layout. No context, so no dispatch here."""
    # An indirect dispatch reads three uints at a 16-byte-aligned offset, so
    # the two consumers must not overlap and the group count must be first.
    assert arg_offset(ARG_SEG) == 0
    assert arg_offset(ARG_GATE) == 16
    assert ARG_GROUPS == 0, "run_indirect reads group_x first"
    # The count shares the vector with the groups, so it must not be one of
    # the three the dispatch reads -- `.w` is the only slot that is not.
    assert ARG_COUNT == 3, "the count must sit past (group_x, group_y, group_z)"
    assert arg_offset(ARG_SLOTS) == ARG_SLOTS * ARG_STRIDE_BYTES


_self_check()
