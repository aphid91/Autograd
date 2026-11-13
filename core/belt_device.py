"""Packing belt segments and their items into SSBOs for the renderer.

The fragment shader evaluates the closed form itself, per pixel, so it needs
the same three mutable values per segment the CPU tick maintains (`head`,
`tail`, `anchor`) plus the ring contents. This module is the one place that
knows the GPU layout.

Why the shader recomputes rather than reading positions
-------------------------------------------------------

Positions are never stored anywhere -- that is the whole point of the closed
form. Writing them out for the renderer would mean materialising O(items) of
state every frame, which is exactly the cost the closed form exists to avoid.
Instead the shader gets `head`, `tail`, `anchor` and `enter_tick`, and
evaluates `min(unblocked, packed)` for the handful of items that could
possibly cover the pixel being shaded. The GLSL is a transcription of
`BeltSim.position_of()`, and tests/test_glsl.py pins the two together.

Layout
------

Two buffers, both `std430`:

  segments   one `ivec4` + one `vec4` per segment, indexed by segment id
  items      a flat arena of `(enter_tick, payload)` pairs

`ivec4`/`vec4` rather than a packed struct because std430 alignment rules for
mixed-type structs are a reliable source of silent corruption -- a `vec3`
padding to 16 bytes, an `int` after a `vec2` landing where you did not expect
it. Two parallel arrays of fixed-size vectors have no such ambiguity, and
each is read coherently by a different part of the shader.

**Segment id indexes the array directly**, so slot 0 is unused (id 0 means
"no segment", mirroring entity row 0). Ids come from a free list and stay
small, so the array is dense enough that indexing beats a lookup table.

The item arena
--------------

Rings are carved out of one array by prefix sum over `cap`, with `base`
recording each segment's slice. On the CPU the rings could be separate
numpy arrays -- and were -- but the GPU needs one buffer, and the compute
port needs `base` to mean something. Doing it properly now keeps the port
mechanical.

Ring capacity is a power of two (enforced in segments.py) because the closed
form indexes `k % cap` on a monotonically growing counter.

Reallocation
------------

A rebuild changes every segment's `cap` and `base`, so the arena is rebuilt
wholesale. That is fine: a rebuild already drops every item, and edits are
human-paced. The buffers grow by doubling and never shrink within a session,
matching how the entity table behaves.
"""

import numpy as np

#: Slots per segment record, in each of the two arrays. The int side is two
#: `ivec4`s: the four original fields, then the sleep flag (see
#: `SEG_I_ASLEEP` below) and three reserved slots.
SEG_IVEC = 8
SEG_VEC = 4

#: int32 fields of a segment record.
#:   [0].x  head   (monotonic counter)
#:   [0].y  tail   (monotonic counter)
#:   [0].z  base   (first slot of this segment's ring in the item arena)
#:   [0].w  cap    (ring capacity, a power of two)
#:   [1].x  asleep (1 if the belt phase is currently skipping this run)
SEG_I_HEAD, SEG_I_TAIL, SEG_I_BASE, SEG_I_CAP = 0, 1, 2, 3

#: float32 fields of a segment record.
#:   .x  anchor  (packed head position, fixed point)
#:   .y  length  (fixed point; items are taken at exactly this)
#:   .z  speed   (fixed point per tick)
#:   .w  spacing (fixed point)
SEG_F_ANCHOR, SEG_F_LENGTH, SEG_F_SPEED, SEG_F_SPACING = 0, 1, 2, 3

#: **The sleep flag, and why it is uploaded rather than derived.**
#:
#: `segment_at_rest()` in common.glsl re-derived the rest predicate from
#: `head`/`tail`/`anchor`, which was honest while every term was in the
#: record. The producer-idle clause broke that: "the upstream gate offered
#: nothing" is a fact about an *entity row*, not about the segment, and the
#: renderer would have to resolve the run's source port and read that gate's
#: waiting zone -- a mirror of `GateSim._src_cached()` in GLSL, which is
#: exactly the kind of second copy that has already caused two
#: "GLSL disagrees with Python" bugs.
#:
#: So the overlay reads the simulation's own answer, the same choice
#: `Entity.F_ASLEEP` made for gates one layer up.
#:
#: It takes the int record's spare slot. `SEG_IVEC` was 4 with only three
#: fields used (`head`, `tail`, `base`, `cap` -- all four, in fact), so the
#: vector grows to 8: std430 pads an `ivec4` array stride anyway, and two
#: `ivec4`s per record is the next honest size.
#:
#: Against the alternatives: `speed` is **not** spare (`item_position()` reads
#: `sf.z` three times, which I checked rather than assumed), and squatting on
#: a sign bit of `base` or `cap` would be a silent corruption waiting for the
#: first run that grew past the bit, since those are used in an index and a
#: modulo respectively.
SEG_I_ASLEEP = 4

#: Slots per item: (enter_tick as float, payload). enter_tick is a float
#: rather than an int so both arrays are vec2 and the shader needs one type;
#: tick counts stay exact in float32 well past any session length (2^24 ticks
#: is ~19 days at 10/s), and the shader only ever uses it as a difference
#: against the current tick.
ITEM_VEC = 2
ITEM_ENTER, ITEM_PAYLOAD = 0, 1

#: Minimum arena size, so a board with no belts still has a valid buffer.
MIN_ARENA = 64


class BeltDevice:
    """Mirrors segment and item state into two SSBOs.

    Rebuilt from the `SegmentIndex` whenever the segmentation changes, and
    re-uploaded every frame the simulation ticks (`head`/`tail`/`anchor`
    change on almost every tick, and a per-segment dirty set would cost more
    bookkeeping than the upload saves -- the whole table is a few KB).
    """

    def __init__(self, index, sim):
        self.index = index
        self.sim = sim
        self._ctx = None

        #: Host-side mirrors. Sized for the highest live segment id + 1.
        self.seg_i = np.zeros((1, SEG_IVEC), dtype=np.int32)
        self.seg_f = np.zeros((1, SEG_VEC), dtype=np.float32)
        self.items = np.zeros((MIN_ARENA, ITEM_VEC), dtype=np.float32)

        self.seg_i_buffer = None
        self.seg_f_buffer = None
        self.item_buffer = None

        #: True when the arena layout must be rebuilt (segmentation changed).
        self._layout_stale = True
        #: Who writes `SEG_I_ASLEEP`. False (the default) means this class
        #: mirrors `BeltSim`'s set, which is right for the CPU tick. The GPU
        #: sleep layer sets it True: `belt_tick.comp` publishes the flag
        #: itself, and the CPU set is empty on that path, so mirroring would
        #: overwrite every kernel decision with a zero.
        self.gpu_owns_sleep_flag = False
        self.stats = {"layouts": 0, "uploads": 0, "arena_slots": 0}

    # --- device resources -------------------------------------------------
    def create_device_resources(self, ctx):
        self._ctx = ctx
        self._ensure_buffers()

    def _ensure_buffers(self):
        """(Re)create buffers to match the host arrays' current size."""
        if self._ctx is None:
            return
        for name, arr in (("seg_i_buffer", self.seg_i),
                          ("seg_f_buffer", self.seg_f),
                          ("item_buffer", self.items)):
            buf = getattr(self, name)
            if buf is not None and buf.size == arr.nbytes:
                continue
            if buf is not None:
                buf.release()
            setattr(self, name, self._ctx.buffer(reserve=max(arr.nbytes, 16)))

    def release(self):
        for name in ("seg_i_buffer", "seg_f_buffer", "item_buffer"):
            buf = getattr(self, name)
            if buf is not None:
                buf.release()
                setattr(self, name, None)

    # --- layout -----------------------------------------------------------
    def invalidate_layout(self):
        """Mark the arena layout stale. Call when the segmentation changes."""
        self._layout_stale = True

    def _rebuild_layout(self):
        """Assign every segment a slice of the item arena, by prefix sum.

        Also resizes the host arrays. Segment ids index the segment arrays
        directly, so they are sized by the highest live id rather than by the
        segment count -- a sparse id space costs a few unused records, which
        is cheaper than an indirection.
        """
        segs = self.index.segments
        max_sid = max(segs, default=0)
        n = max_sid + 1

        if self.seg_i.shape[0] < n:
            # Grow by doubling, like the entity table.
            size = max(n, self.seg_i.shape[0] * 2, 8)
            self.seg_i = np.zeros((size, SEG_IVEC), dtype=np.int32)
            self.seg_f = np.zeros((size, SEG_VEC), dtype=np.float32)
        self.seg_i[:] = 0
        self.seg_f[:] = 0

        # Prefix sum over capacities gives each ring its base.
        base = 0
        for sid in sorted(segs):
            seg = segs[sid]
            seg.base = base
            base += seg.cap

        need = max(base, MIN_ARENA)
        if self.items.shape[0] < need:
            size = max(need, self.items.shape[0] * 2)
            self.items = np.zeros((size, ITEM_VEC), dtype=np.float32)
        else:
            # Stale item data beyond the live rings would be read by a shader
            # that trusted a bad head/tail; zero it so a bug shows as empty
            # rather than as phantom items.
            self.items[:] = 0.0

        self._layout_stale = False
        self.stats["layouts"] += 1
        self.stats["arena_slots"] = base
        self._ensure_buffers()

    # --- per-frame sync ---------------------------------------------------
    def sync(self):
        """Mirror current segment and item state, and upload. Returns slots used.

        Called once per frame after the tick. The whole table goes up: the
        three mutable fields change on nearly every tick of every segment, so
        tracking which changed would cost more than it saves at these sizes.
        """
        if self._layout_stale:
            self._rebuild_layout()

        sim = self.sim
        # The simulation's own sleep set, so the overlay reports what the tick
        # actually skipped rather than re-deriving a predicate whose newest
        # term lives in an entity row. See SEG_I_ASLEEP.
        #
        # Through `is_asleep()` rather than reading `_asleep`, so the device
        # layer depends on a stated interface rather than on another object's
        # private attribute.
        #
        # **Unless the GPU owns the flag**, in which case this must not touch
        # it. `belt_tick.comp` publishes its own sleep decision into the same
        # slot, and the CPU's `_asleep` is empty on the GPU path -- so writing
        # it here stamped `0` over every run the kernel had just slept, and the
        # overlay painted a board that was 97% asleep entirely amber. The flag
        # is a diagnostic, but a diagnostic that lies about the mechanism it
        # exists to show is worse than none.
        asleep = sim.is_asleep
        gpu_owns = self.gpu_owns_sleep_flag
        for sid, seg in self.index.segments.items():
            self.seg_i[sid, :4] = (seg.head, seg.tail, seg.base, seg.cap)
            if not gpu_owns:
                self.seg_i[sid, SEG_I_ASLEEP] = 1 if asleep(sid) else 0
            self.seg_f[sid] = (seg.anchor, sim.length_of(seg),
                               sim.speed, sim.spacing)
            # The ring's live window only. Slots outside [head, tail) are
            # never read by a correct shader, but copying the whole ring is
            # one slice and avoids a per-item loop.
            if seg.cap and seg.base + seg.cap <= self.items.shape[0]:
                lo = seg.base
                self.items[lo:lo + seg.cap, ITEM_ENTER] = seg.enter_tick
                self.items[lo:lo + seg.cap, ITEM_PAYLOAD] = seg.payload

        self._upload()
        self.stats["uploads"] += 1
        return self.stats["arena_slots"]

    def _upload(self):
        if self._ctx is None:
            return
        self._ensure_buffers()
        self.seg_i_buffer.write(np.ascontiguousarray(self.seg_i))
        self.seg_f_buffer.write(np.ascontiguousarray(self.seg_f))
        self.item_buffer.write(np.ascontiguousarray(self.items))

    def download(self):
        """Device -> host, for the three mutable arrays.

        The counterpart of `_upload()`, and the opposite direction from
        everything else here: this class was written to *feed* the renderer,
        but the compute tick writes the same buffers, so the host copy has
        to be refreshable from them. See core/belt_compute.py -- it borrows
        these buffers rather than keeping a second set, which is what makes
        one copy authoritative instead of two racing on one binding.

        **This stalls**, like every readback. Callers go through
        `BeltCompute.readback()`, which is the deliberate act of asking.
        """
        if self._ctx is None:
            return
        for buf, arr in ((self.seg_i_buffer, self.seg_i),
                         (self.seg_f_buffer, self.seg_f),
                         (self.item_buffer, self.items)):
            if buf is None:
                continue
            raw = np.frombuffer(buf.read(), dtype=arr.dtype)
            arr[...] = raw[:arr.size].reshape(arr.shape)

    def bind(self, seg_i_binding, seg_f_binding, item_binding):
        """Bind the three buffers to their storage-buffer slots."""
        if self.seg_i_buffer is None:
            return
        self.seg_i_buffer.bind_to_storage_buffer(seg_i_binding)
        self.seg_f_buffer.bind_to_storage_buffer(seg_f_binding)
        self.item_buffer.bind_to_storage_buffer(item_binding)

    # --- audit ------------------------------------------------------------
    def audit(self):
        """Check the device mirror against the index. Returns problems.

        The mirror is a third copy of the simulation state -- after the
        segment objects and the grid -- so it gets the same treatment as the
        others: something that checks it rather than assuming.
        """
        problems = []
        if self._layout_stale:
            problems.append("layout is stale; sync() has not run")
            return problems

        # Rings must not overlap, and must fit the arena.
        spans = []
        for sid, seg in sorted(self.index.segments.items()):
            if seg.base + seg.cap > self.items.shape[0]:
                problems.append(
                    f"segment {sid}: ring [{seg.base},{seg.base + seg.cap}) "
                    f"overruns the {self.items.shape[0]}-slot arena")
            spans.append((seg.base, seg.base + seg.cap, sid))
        spans.sort()
        for (a_lo, a_hi, a_sid), (b_lo, _b_hi, b_sid) in zip(spans, spans[1:]):
            if a_hi > b_lo:
                problems.append(
                    f"segments {a_sid} and {b_sid} share arena slots "
                    f"[{b_lo},{a_hi})")

        # The mirror must match the live objects.
        for sid, seg in self.index.segments.items():
            if sid >= self.seg_i.shape[0]:
                problems.append(f"segment {sid} is past the mirror's size")
                continue
            # The **core four only**: the record's second vector carries the
            # sleep flag, which is the simulation's scheduling decision rather
            # than anything the index holds, so there is nothing here to
            # compare it against. Slicing rather than comparing the whole row
            # keeps this audit about "does the mirror match the index".
            got = tuple(int(v) for v in self.seg_i[sid][:4])
            want = (seg.head, seg.tail, seg.base, seg.cap)
            if got != want:
                problems.append(
                    f"segment {sid}: mirror has {got}, index has {want}")
            if abs(float(self.seg_f[sid][SEG_F_ANCHOR]) - seg.anchor) > 1e-3:
                problems.append(
                    f"segment {sid}: mirror anchor "
                    f"{self.seg_f[sid][SEG_F_ANCHOR]} != {seg.anchor}")
        return problems

    def __repr__(self):
        return (f"<BeltDevice {len(self.index.segments)} segments, "
                f"{self.stats['arena_slots']} arena slots>")


def _self_check():
    """Layout invariants, with a stub index."""
    class _Seg:
        def __init__(self, sid, length, cap):
            self.sid = sid
            self.length = length
            self.head = self.tail = 0
            self.anchor = float(length)
            self.base = 0
            self.cap = cap
            self.enter_tick = np.zeros(cap, dtype=np.int64)
            self.payload = np.zeros(cap, dtype=np.float32)
            self.cells = [(0, i) for i in range(length)]
            self.src = self.dst = None

        @property
        def count(self):
            return self.tail - self.head

    class _Index:
        def __init__(self, segs):
            self.segments = {s.sid: s for s in segs}

    class _Sim:
        speed = 256
        spacing = 256

        def length_of(self, seg):
            return seg.length * 256

        def is_asleep(self, sid):
            # The stub implements the whole protocol `sync()` uses, rather
            # than `sync()` guarding with getattr: a stub that silently lacks
            # a field is how an upload path stops being exercised.
            return sid == 3

    # Non-contiguous ids: the segment arrays are indexed by id, so a gap
    # must simply leave an unused record rather than shifting anything.
    index = _Index([_Seg(1, 4, 8), _Seg(3, 10, 16), _Seg(7, 2, 4)])
    dev = BeltDevice(index, _Sim())
    dev.sync()

    assert dev.seg_i.shape[0] >= 8, dev.seg_i.shape
    # Bases must be a prefix sum with no overlap.
    bases = {sid: s.base for sid, s in index.segments.items()}
    caps = {sid: s.cap for sid, s in index.segments.items()}
    spans = sorted((bases[s], bases[s] + caps[s]) for s in bases)
    for (a_lo, a_hi), (b_lo, b_hi) in zip(spans, spans[1:]):
        assert a_hi <= b_lo, (spans, "rings overlap")
    assert dev.stats["arena_slots"] == sum(caps.values()), dev.stats
    assert not dev.audit(), dev.audit()

    # The mirror must track mutation.
    seg = index.segments[3]
    seg.tail = 5
    seg.anchor = 123.0
    dev.sync()
    assert int(dev.seg_i[3][SEG_I_TAIL]) == 5
    assert abs(float(dev.seg_f[3][SEG_F_ANCHOR]) - 123.0) < 1e-6
    assert not dev.audit(), dev.audit()

    # The sleep flag reaches its slot, and only for the sleeping run. Pinned
    # here because the overlay now *reads* this rather than re-deriving the
    # predicate, so a record that silently stopped carrying it would paint
    # every run awake with no other symptom -- and the pixel tests can only
    # see the colour, not which field produced it. `_Sim.is_asleep` sleeps
    # segment 3 alone, so this also catches a flag written to every record.
    assert int(dev.seg_i[3][SEG_I_ASLEEP]) == 1, dev.seg_i[3]
    assert int(dev.seg_i[1][SEG_I_ASLEEP]) == 0, dev.seg_i[1]
    assert int(dev.seg_i[7][SEG_I_ASLEEP]) == 0, dev.seg_i[7]
    # And it must not have landed on a field something else reads.
    assert int(dev.seg_i[3][SEG_I_HEAD]) == index.segments[3].head
    assert int(dev.seg_i[3][SEG_I_CAP]) == index.segments[3].cap

    # An unused id's record stays zeroed, so a shader reading it finds an
    # empty segment rather than stale values. The whole record, both vectors,
    # so the sleep flag cannot be left set on a gap either -- that would paint
    # a nonexistent run blue, which is harmless now and would not be if
    # anything ever iterated the flags.
    assert not dev.seg_i[2].any(), dev.seg_i[2]

    # Item data must land in the right slice.
    seg.enter_tick[0] = 42
    seg.payload[0] = -1.5
    dev.sync()
    assert float(dev.items[seg.base + 0, ITEM_ENTER]) == 42.0
    assert abs(float(dev.items[seg.base + 0, ITEM_PAYLOAD]) + 1.5) < 1e-6

    # A re-layout after the segmentation changes must not leave the arena
    # holding the old contents.
    index.segments.pop(3)
    dev.invalidate_layout()
    dev.sync()
    assert not dev.audit(), dev.audit()
    assert dev.stats["arena_slots"] == 8 + 4, dev.stats

    # An empty board still produces a valid, bindable arena.
    empty = BeltDevice(_Index([]), _Sim())
    empty.sync()
    assert empty.items.shape[0] >= MIN_ARENA
    assert not empty.audit(), empty.audit()


_self_check()
