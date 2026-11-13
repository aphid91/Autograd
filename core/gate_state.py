"""Gate channel state, laid out for the tick rather than for storage.

`Gameboard.entities` is the single source of truth and stays that way: one
`(capacity, ENTITY_STRIDE)` float32 row per entity, AoS, uploaded wholesale
to the renderer and baked into every save file. That layout is right for what
it is -- a row is the unit a save, a free list and a view all deal in -- and
it is the wrong layout for the tick.

The mismatch is concrete. The structure phase wants, for every gate at once,
"which input channels are held" -- eight float columns out of twenty-eight,
for a few hundred rows scattered through a table that also holds every belt
on the board. Expressed over `entities` that is a fancy-index gather
(`entities[rows]`), which materialises a full 28-float row per gate to read 8
of them. On a CPU that is a wasted copy. On the GPU it is the thing the
hardware is actually bottlenecked by: a 112-byte stride between consecutive
threads' reads, most of it discarded.

So this module keeps a **gate-dense, structure-of-arrays mirror**:

    rows     (n_gates,)                which entity row each slot is
    in_held  (n_gates, MAX_CHANNELS)   float32, 1.0 or 0.0
    in_value (n_gates, MAX_CHANNELS)
    out_held (n_gates, MAX_CHANNELS)
    out_value(n_gates, MAX_CHANNELS)

Three properties follow, and all three are what the port needs:

- **Dense.** Slot `i` is the i'th *gate*, not the i'th row, so a board that is
  mostly belt costs nothing. The gate set changes only on an edit, so the
  slot assignment is an edit-time derivation like every other one here.
- **Separated.** A pass reading `in_held` touches one contiguous array and
  nothing else. That is a coalesced read on the GPU and a cache-friendly one
  now.
- **Flat.** Every array is a plain numpy block with no Python objects, so the
  whole structure uploads as four SSBOs with no packing step.

Why a mirror rather than a move
-------------------------------

The alternative is to restructure `entities` itself. It was rejected for two
reasons that are not going away: the row layout is **baked into save files**
(`ENTITY_STRIDE` is checked on load, and changing it invalidates every save
written before), and the renderer reads gate channels out of the entity SSBO
per pixel to draw waiting zones. Both want AoS rows; the tick wants SoA
columns. A mirror is the honest answer to two readers with opposite needs.

The cost is the thing to watch, and it is the usual one: **a second copy can
drift.** The discipline is the same one `BeltDevice` already follows -- there
is exactly one direction of authority at any moment, and an `audit()` that
checks the mirror against the source. See `sync_from_rows()` / `flush()`.
"""

import numpy as np

from core.entity import MAX_CHANNELS, Entity


class GateState:
    """A gate-dense SoA mirror of the entity table's channel columns.

    Holds no opinion about the simulation: it is storage, plus the two
    transfers that keep it in step with `Gameboard.entities`.
    """

    def __init__(self, board):
        self.board = board
        #: Entity row for each gate slot, ascending. Empty until built.
        self.rows = np.zeros(0, dtype=np.int64)
        #: row -> slot, for the callbacks that arrive holding a row. A numpy
        #: array rather than a dict: it is indexed in bulk by the port passes,
        #: and -1 means "not a gate".
        self.slot_of = np.full(1, -1, dtype=np.int64)
        self.in_held = np.zeros((0, MAX_CHANNELS), dtype=np.float32)
        self.in_value = np.zeros((0, MAX_CHANNELS), dtype=np.float32)
        self.out_held = np.zeros((0, MAX_CHANNELS), dtype=np.float32)
        self.out_value = np.zeros((0, MAX_CHANNELS), dtype=np.float32)
        #: Per-slot type geometry, which the prefilter needs beside the
        #: channel state. Edit-time like `rows`.
        self.n_in = np.zeros(0, dtype=np.int64)
        self.n_out = np.zeros(0, dtype=np.int64)
        self.type_id = np.zeros(0, dtype=np.int64)
        #: Channel-column masks derived from n_in / n_out, precomputed because
        #: every prefilter pass rebuilt them from `arange` comparisons.
        self.in_used = np.zeros((0, MAX_CHANNELS), dtype=bool)
        self.out_used = np.zeros((0, MAX_CHANNELS), dtype=bool)
        #: Which output channels have somewhere to send a result -- the
        #: vectorised form of `GateSim._connected_channels()`. Filled by
        #: `set_drains()` rather than in `build()`, because it is derived from
        #: the grid *and* the segmentation while everything above comes from
        #: the entity rows alone, and the two have different owners.
        #:
        #: It belongs here, with the rest of the per-edit geometry, because
        #: it is the one term the firing kernel needs that is not row state:
        #: it decides both which zones can block a gate and which results are
        #: discarded. Resolved once per edit, it stops being a grid walk and
        #: becomes a mask the kernel ANDs against.
        self.drains = np.zeros((0, MAX_CHANNELS), dtype=bool)

    # --- layout -----------------------------------------------------------
    def build(self, rows, n_in, n_out, type_id):
        """Assign slots to `rows` and size the arrays. Edit-time.

        Takes the geometry it is given rather than deriving it, so there is
        one authority on which rows are gates -- `GateSim._gate_table()` --
        and this cannot disagree with the prefilter about the population.
        """
        self.rows = np.asarray(rows, dtype=np.int64)
        self.n_in = np.asarray(n_in, dtype=np.int64)
        self.n_out = np.asarray(n_out, dtype=np.int64)
        self.type_id = np.asarray(type_id, dtype=np.int64)
        n = len(self.rows)

        for name in ("in_held", "in_value", "out_held", "out_value"):
            setattr(self, name,
                    np.zeros((n, MAX_CHANNELS), dtype=np.float32))

        lanes = np.arange(MAX_CHANNELS)
        self.in_used = lanes[None, :] < self.n_in[:, None]
        self.out_used = lanes[None, :] < self.n_out[:, None]
        # Unknown until `set_drains()` runs. **All-False rather than
        # all-True**, so a caller that forgets produces a visibly inert gate
        # rather than one that silently fires into nothing and destroys its
        # results -- the conservative direction for a mask whose meaning is
        # "this result has somewhere to go".
        self.drains = np.zeros((n, MAX_CHANNELS), dtype=bool)

        # The reverse map is sized to cover the highest gate row *and* the
        # table, rebuilt here because the free list may have recycled a row
        # into or out of the gate set.
        #
        # Sized by `max(row) + 1` as well as by the table because the two can
        # part company: `Gameboard._grow()` doubles the table, and while every
        # path that grows it also changes the gate set (so this is rebuilt),
        # that is a coincidence of call order rather than a guarantee. An
        # undersized map would raise on the lookup if the row were past the
        # end and -- far worse -- silently return a *stale slot* if it were
        # not, which reads as one gate's channels appearing on another.
        cap = max(self.board.entities.shape[0], 1)
        if n:
            cap = max(cap, int(self.rows.max()) + 1)
        self.slot_of = np.full(cap, -1, dtype=np.int64)
        if n:
            self.slot_of[self.rows] = np.arange(n, dtype=np.int64)

    def set_drains(self, slot, channels):
        """Record which output channels of one gate have a consumer.

        Per slot rather than wholesale because the source
        (`GateSim._connected_channels()`) answers one gate at a time and is
        memoised per gate; filling the mask as those answers arrive keeps a
        single authority rather than a second walk that could disagree.
        """
        if not 0 <= slot < self.drains.shape[0]:
            return
        row = self.drains[slot]
        row[:] = False
        for c in channels:
            if 0 <= c < MAX_CHANNELS:
                row[c] = True

    # --- transfers --------------------------------------------------------
    #
    # The mirror is authoritative **only between** `sync_from_rows()` and
    # `flush()`, and the tick is what sits between them. Outside that window
    # `Gameboard.entities` is the single source of truth, which is what keeps
    # every existing reader -- views, the Status window, the renderer, saves,
    # the differential checkers -- correct with no change at all.
    #
    # That is the same contract `BeltDevice` has with the segment objects,
    # and it is deliberately narrow: a second copy is only safe while exactly
    # one party may write it.
    def sync_from_rows(self):
        """Load the mirror from the entity table. Start of the tick."""
        if not len(self.rows):
            return
        ents = self.board.entities
        rows = self.rows
        c = MAX_CHANNELS
        # One gather per column block rather than one per gate. Each is a
        # contiguous slice of the row, so this is the widest read numpy can
        # make of an AoS table -- and it is the step that disappears entirely
        # once the state lives in VRAM.
        self.in_value[:] = ents[rows, Entity.F_IN_VALUE:Entity.F_IN_VALUE + c]
        self.in_held[:] = ents[rows, Entity.F_IN_HELD:Entity.F_IN_HELD + c]
        self.out_value[:] = ents[
            rows, Entity.F_OUT_VALUE:Entity.F_OUT_VALUE + c]
        self.out_held[:] = ents[rows, Entity.F_OUT_HELD:Entity.F_OUT_HELD + c]

    def flush(self):
        """Write the mirror back to the entity table. End of the tick."""
        if not len(self.rows):
            return
        ents = self.board.entities
        rows = self.rows
        c = MAX_CHANNELS
        ents[rows, Entity.F_IN_VALUE:Entity.F_IN_VALUE + c] = self.in_value
        ents[rows, Entity.F_IN_HELD:Entity.F_IN_HELD + c] = self.in_held
        ents[rows, Entity.F_OUT_VALUE:Entity.F_OUT_VALUE + c] = self.out_value
        ents[rows, Entity.F_OUT_HELD:Entity.F_OUT_HELD + c] = self.out_held
        dirty = self.board.entities_dirty
        for row in rows.tolist():
            dirty.add(row)

    # --- audit ------------------------------------------------------------
    def audit(self):
        """Check the mirror against the entity table. Returns problems.

        A second copy of live state gets the same treatment as the other two
        in this project (`BeltDevice.audit()`, `SegmentIndex.audit()`):
        something that checks it rather than assuming.
        """
        problems = []
        ents = self.board.entities
        c = MAX_CHANNELS
        for i, row in enumerate(self.rows.tolist()):
            if self.slot_of[row] != i:
                problems.append(
                    f"row {row} maps to slot {self.slot_of[row]}, not {i}")
            pairs = (
                ("in_value", Entity.F_IN_VALUE, self.in_value),
                ("in_held", Entity.F_IN_HELD, self.in_held),
                ("out_value", Entity.F_OUT_VALUE, self.out_value),
                ("out_held", Entity.F_OUT_HELD, self.out_held),
            )
            for name, off, arr in pairs:
                want = ents[row, off:off + c]
                if not np.allclose(arr[i], want, equal_nan=True):
                    problems.append(
                        f"slot {i} (row {row}) {name}: mirror {arr[i]} != "
                        f"table {want}")
        return problems

    def __repr__(self):
        return f"<GateState {len(self.rows)} gates x {MAX_CHANNELS} channels>"


def _self_check():
    """The mirror round-trips, and the slot map inverts the row list."""
    class _Dirty:
        def __init__(self):
            self.rows = []

        def add(self, row):
            self.rows.append(row)

    class _Board:
        def __init__(self, cap=16):
            self.entities = np.zeros((cap, 64), dtype=np.float32)
            self.entities_dirty = _Dirty()

    board = _Board()
    gs = GateState(board)
    gs.build(rows=[3, 7, 9], n_in=[2, 0, 1], n_out=[1, 3, 0],
             type_id=[5, 6, 7])

    # The slot map must invert the row list, and say -1 for everything else.
    assert gs.slot_of[3] == 0 and gs.slot_of[7] == 1 and gs.slot_of[9] == 2
    assert gs.slot_of[4] == -1, gs.slot_of[4]

    # Channel masks follow the declared counts, so an undeclared lane can
    # never read as unfed -- the trap the prefilter's `in_used` exists for.
    assert gs.in_used[0].tolist() == [True, True, False, False]
    assert gs.in_used[1].tolist() == [False] * MAX_CHANNELS
    assert gs.out_used[1].tolist() == [True, True, True, False]

    # A round trip must be the identity on the channel columns.
    board.entities[3, Entity.F_IN_VALUE + 1] = 2.5
    board.entities[3, Entity.F_IN_HELD + 1] = 1.0
    board.entities[7, Entity.F_OUT_VALUE + 2] = -4.0
    board.entities[7, Entity.F_OUT_HELD + 2] = 1.0
    gs.sync_from_rows()
    assert gs.in_value[0, 1] == 2.5, gs.in_value[0]
    assert gs.out_value[1, 2] == -4.0, gs.out_value[1]
    assert not gs.audit(), gs.audit()

    # A write to the mirror must reach the table, and only through flush() --
    # the one-writer-at-a-time contract the module docstring states.
    gs.in_value[0, 1] = 9.0
    assert board.entities[3, Entity.F_IN_VALUE + 1] == 2.5, (
        "the mirror must not write through to the table on its own")
    gs.flush()
    assert board.entities[3, Entity.F_IN_VALUE + 1] == 9.0
    assert 3 in board.entities_dirty.rows, board.entities_dirty.rows

    # ...and the audit has to be able to *fail*, or it proves nothing.
    gs.out_value[1, 2] = 123.0
    assert gs.audit(), "a diverged mirror must be reported"

    # An empty gate set is a real state (a board of nothing but belt), and
    # both transfers must be no-ops rather than errors on it.
    empty = GateState(_Board())
    empty.build(rows=[], n_in=[], n_out=[], type_id=[])
    empty.sync_from_rows()
    empty.flush()
    assert not empty.audit(), empty.audit()


_self_check()
