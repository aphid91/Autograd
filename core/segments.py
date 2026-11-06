"""Belt segments: the O(1) simulation unit, derived from the grid.

A **segment** is a maximal run of belt-like cells carrying material from one
node to the next -- where a node is a gate port, or nothing at all (a dangling
end). The player draws individual cells; this module coalesces them into the
objects the simulation actually ticks.

Why segments exist
------------------

docs/belt_report.md gives a closed form for every item's position on a "pure"
belt: one line, no branching or merging, no mid-line insertion, uniform speed.
Under those conditions nothing per-item is ever updated after the item
enters, and a whole run ticks in O(1) regardless of how many items are on it.

That is only valid for a *maximal* run between two nodes. Cut a run in half
and you have two belts whose junction is neither a producer nor a consumer,
and the closed form no longer describes it. So the unit of simulation is the
segment, not the cell -- and a cell's only job is to say which segment it
belongs to and how far along it sits.

Derived, not authored
---------------------

Segments are a **cache over the grid**, rebuilt whenever belts change. The
grid remains the single source of truth: delete segments entirely and they
can be recomputed from it. Membership is still *stored* (in cell `.w`)
because the fragment shader needs it for a cell it has already fetched --
walking a run per pixel is not an option.

Two consequences:

- **An edit drops the items on the segments it touches.** Splitting a run
  means dividing a ring buffer's live contents between two new rings, which
  is not expressible in the closed form (every item's position is relative to
  its segment's anchor). Factorio preserves items across edits; it also is
  not using a closed form. Dropping them is the price of O(1), and edits are
  human-paced so the cost is invisible.
- **Ghost cells are not segmented.** A preview spawns real entities in real
  cells, so a moving ghost would otherwise rebuild segments every frame and
  splice a half-drawn run into the live simulation. `GHOST_PENDING` cells are
  skipped.

  **Frustrated cells are, though.** Frustration is an overlay on somebody
  else's cell: it lives in `.z` and leaves `.x` untouched precisely so the
  occupant survives. Skipping them would cut a live run in two for as long as
  the player held a blocked structure over it -- and destroy the items on it.
  Only `GHOST_PENDING` is fictional.

Geometry
--------

Belt-like cells (`TYPE_BELT`, `TYPE_UNDER`) move material along their own
facing. A run is traced by following that facing forward and backward:

    cell A (+X) -> cell B (+X) -> cell C (+Y)

B is downstream of A iff A faces at B *and* B accepts from A -- which for a
belt means B simply exists and is not facing back at A. Corners are fine, the
facing just changes.

**Undergrounds add capacity without adding cells.** An entrance paired with
an exit contributes `span + 1` positions: the two end cells plus the buried
tiles between them, which hold items nobody renders. The run continues
through the pair as though the gap were ordinary belt, which is exactly the
behaviour wanted and costs the O(1) trick nothing -- it is all just length.

Where runs end
--------------

A segment's **tail** is its upstream end (where items enter, offset 0) and
its **head** is the downstream end (where the consumer takes them). A run
stops when the next cell is not belt-like, or when more than one cell feeds
the same cell: a merge is not a pure belt, so it has to be a boundary.

Merges are *resolved*, not supported
------------------------------------

Belts never merge in this game, but the player can still point two belts at
one cell. The take-direction rule picks exactly one of them deterministically
-- straight-through first, then counter-clockwise from the receiving cell's
own facing -- and the losers are simply not connected. `take_dir()` is the
single authority, mirrored in GLSL, so the renderer draws the join the
simulation actually uses and the "phantom merge" rendering goof disappears.
"""

from core import cell
from core.entity import Underground

#: Cell types that move material along their own facing.
_BELTLIKE = (cell.TYPE_BELT, cell.TYPE_UNDER)

#: Segment ids start at 1; 0 means "not on a segment", like entity row 0.
NULL_SEGMENT = 0

#: Ring capacity is rounded up to a power of two. The closed form indexes
#: items by `k % cap` on a monotonically growing counter, and that is only
#: correct under unsigned wraparound when cap is a power of two -- see the
#: "counter wraparound" pitfall in docs/belt_report.md.
MIN_RING_CAP = 4


def is_beltlike(type_id):
    """Does this type carry material along its own facing, belt-style?"""
    return type_id in _BELTLIKE


def take_dir(facing, candidates):
    """Which of several incoming directions a cell actually takes from.

    `candidates` are directions (as seen from the receiving cell) that hold
    something emitting at us. Returns the winning direction, or None.

    Belts in this game never merge, but nothing stops a player pointing two
    at one cell. Rather than refuse the build or simulate a merge, one source
    wins deterministically and the rest are left unconnected.

    The rule: **prefer straight-through, then counter-clockwise** from the
    receiving cell's own facing. Straight-through first because it is what
    the player almost always means -- a run that continues is the run. Then
    CCW so the choice is a pure function of the geometry with no positional
    bias.

    This is the single authority for the tie-break, mirrored by `take_dir()`
    in common.glsl. Both the simulation and the renderer must agree: if the
    renderer drew an arm the simulation does not use, the picture would lie
    about where material goes.
    """
    if not candidates:
        return None
    cands = set(candidates)
    # Straight-through: the upstream cell is directly behind us, i.e. the
    # direction opposite our facing.
    behind = (facing + 2) % 4
    if behind in cands:
        return behind
    # Otherwise walk CCW from our own facing and take the first hit. Starting
    # at the facing itself makes the order a function of orientation only.
    for i in range(4):
        d = (facing + i) % 4
        if d in cands:
            return d
    return None


class Segment:
    """One maximal belt run, and the ring buffer of items on it.

    Positions are never stored per item. `enter_tick` is written once when an
    item enters and never changed; everything else is computed from the
    closed form in docs/belt_report.md. See `Belts.position_of`.
    """

    __slots__ = ("sid", "cells", "length", "head", "tail", "anchor",
                 "base", "cap", "enter_tick", "payload",
                 "src", "dst")

    def __init__(self, sid, cells, length):
        self.sid = sid
        #: The *rendered* cells, tail first. Shorter than `length` whenever an
        #: underground pair is involved: the buried tiles hold items but have
        #: no cell of their own to draw in.
        self.cells = cells
        #: Positions on this segment, counting buried tiles. The consumer
        #: takes items at `length`.
        self.length = length

        # Ring state. head/tail are monotonic counters, not slot indices:
        # items on the belt are [head, tail), head nearest the consumer.
        self.head = 0
        self.tail = 0
        #: Where the head would be if the whole run were packed tight. The
        #: closed form's `A`.
        self.anchor = length

        #: Slice of the item arena this segment owns.
        self.base = 0
        self.cap = 0
        #: Per-slot arrays, sized `cap`. Allocated by the arena.
        self.enter_tick = None
        self.payload = None

        #: What feeds this segment and what drains it, as
        #: (entity_row, face_direction) or None for a dangling end.
        self.src = None
        self.dst = None

    @property
    def count(self):
        """Items currently on the segment."""
        return self.tail - self.head

    @property
    def rendered_length(self):
        return len(self.cells)

    @property
    def buried(self):
        """Positions that exist but are not drawn (underground spans)."""
        return self.length - len(self.cells)

    def __repr__(self):
        return (f"<Segment {self.sid} len={self.length} "
                f"cells={len(self.cells)} items={self.count} "
                f"cap={self.cap}>")


class SegmentIndex:
    """Every segment on the board, and the cell -> segment mapping.

    Rebuilt from the grid by `rebuild()`. Holds no authoritative state: the
    grid and the entity table are the truth, and this is the derived view the
    simulation ticks and the renderer reads.
    """

    def __init__(self, board):
        self.board = board
        #: sid -> Segment, for live segments only.
        self.segments = {}
        self._next_sid = 1
        self._free_sids = []
        #: Cumulative counters, for the inspection panel.
        self.stats = {"rebuilds": 0, "segments_built": 0, "items_dropped": 0}

    # --- id allocation ---------------------------------------------------
    def _claim_sid(self):
        if self._free_sids:
            return self._free_sids.pop()
        sid = self._next_sid
        if sid > cell.MAX_SEGMENT_ID:
            raise RuntimeError(
                f"segment id would exceed the {cell.SEG_ID_BITS}-bit cell field")
        self._next_sid += 1
        return sid

    def _release_sid(self, sid):
        self._free_sids.append(sid)

    # --- queries ---------------------------------------------------------
    def at(self, x, y):
        """The segment covering (x, y), or None."""
        if not self.board.in_bounds(x, y):
            return None
        sid = cell.segment_id(self.board.grid[y, x, cell.CH_W])
        return self.segments.get(sid)

    def offset_at(self, x, y):
        """How far along its segment (x, y) sits, or None if unsegmented."""
        if not self.board.in_bounds(x, y):
            return None
        word = int(self.board.grid[y, x, cell.CH_W])
        sid, off = cell.unpack_segment(word)
        return off if sid else None

    @property
    def count(self):
        return len(self.segments)

    def total_items(self):
        return sum(s.count for s in self.segments.values())

    # --- the grid walk ---------------------------------------------------
    def _cell_at(self, x, y):
        """(type, direction, row) for a *real* belt-like cell, or None.

        Ghost cells are deliberately invisible here: a preview spawns real
        entities in real cells, so counting them would splice a half-drawn
        run into the live simulation and rebuild every segment on every frame
        the cursor moves.

        **Frustrated cells are real and do count.** Frustration is an overlay
        on somebody else's cell -- the occupant is untouched, which is the
        whole point of it living in .z and leaving .x alone. Treating a
        frustrated cell as absent would cut a live run in two just because
        the player waved a blocked structure over it, destroying the items on
        it. Only GHOST_PENDING is fictional.
        """
        if not self.board.in_bounds(x, y):
            return None
        z = int(self.board.grid[y, x, cell.CH_Z])
        if cell.ghost_state(z) == cell.GHOST_PENDING:
            return None
        word = int(self.board.grid[y, x, cell.CH_X])
        t = cell.cell_type(word)
        if not is_beltlike(t):
            return None
        return t, cell.cell_dir(word), cell.cell_id(word)

    def _under_jump(self, x, y, direction):
        """Where an underground entrance at (x, y) resurfaces, or None.

        A paired entrance hands material to its exit, skipping the tiles
        between. Returns ((exit_x, exit_y), buried_count) -- the buried tiles
        are real positions on the segment that simply have no cell to draw.
        """
        row = self.board.entity_id_at(x, y)
        if not row or not self.board.is_row_alive(row):
            return None
        view = self.board.view(row)
        if view.type_id != cell.TYPE_UNDER or view.is_exit:
            return None
        if not view.paired:
            return None
        mate = self.board.view(view.partner)
        if mate.type_id != cell.TYPE_UNDER or not mate.is_exit:
            return None
        if mate.direction != direction:
            return None
        span = Underground.span_cells((x, y), mate.origin)
        if span is None:
            return None
        return mate.origin, len(span)

    def _next_cell(self, x, y):
        """The cell downstream of (x, y), as ((nx, ny), buried) or None.

        `buried` counts positions crossed that have no cell of their own --
        non-zero only when jumping an underground span.
        """
        here = self._cell_at(x, y)
        if here is None:
            return None
        t, d, _row = here

        # A paired underground entrance dives: its downstream neighbour is
        # its exit, with the span's tiles in between.
        if t == cell.TYPE_UNDER:
            jump = self._under_jump(x, y, d)
            if jump is not None:
                (ex, ey), buried = jump
                return (ex, ey), buried
            # An unpaired entrance leads nowhere; an exit carries on normally.
            row = self.board.entity_id_at(x, y)
            if row and self.board.is_row_alive(row):
                if not self.board.view(row).is_exit:
                    return None

        step = cell.DIR_STEPS[d]
        nx, ny = x + step[0], y + step[1]
        nxt = self._cell_at(nx, ny)
        if nxt is None:
            return None
        # The downstream cell must actually take from us. It does not if it
        # faces back at us (two belts nose to nose), and it does not if some
        # other incoming direction wins the tie-break.
        if self._taken_from(nx, ny) != (x, y):
            return None
        return (nx, ny), 0

    def _incoming(self, x, y):
        """Directions (from (x,y)'s view) holding a belt-like cell emitting at us."""
        here = self._cell_at(x, y)
        if here is None:
            return []
        _ht, facing, _hrow = here
        out = []
        for d in range(4):
            # The cell we output into is never a source, even when it faces
            # back at us. Two belts nose to nose would otherwise each name
            # the other as upstream, leaving neither one a tail and sending
            # the run into the closed-loop fallback as a single segment. Our
            # own facing wins: we are leaving, not arriving. The renderer
            # applies the same rule in draw_belt().
            if d == facing:
                continue
            step = cell.DIR_STEPS[d]
            nx, ny = x + step[0], y + step[1]
            n = self._cell_at(nx, ny)
            if n is None:
                continue
            t, nd, _row = n
            # It emits at us only if its facing points back our way.
            if nd != (d + 2) % 4:
                continue
            # A *paired entrance* dives instead of feeding the cell ahead, so
            # it is not a surface source. An unpaired one leads nowhere at
            # all. Only exits and plain belts emit onto the surface.
            if t == cell.TYPE_UNDER:
                row = self.board.entity_id_at(nx, ny)
                if not row or not self.board.is_row_alive(row):
                    continue
                if not self.board.view(row).is_exit:
                    continue
            out.append(d)
        return out

    def _taken_from(self, x, y):
        """Which neighbouring cell (x, y) actually takes material from.

        Returns (nx, ny) or None. Resolves multi-source contention through
        take_dir(), so exactly one upstream cell ever wins.
        """
        here = self._cell_at(x, y)
        if here is None:
            return None
        _t, facing, _row = here
        cands = self._incoming(x, y)
        d = take_dir(facing, cands)
        if d is None:
            return None
        step = cell.DIR_STEPS[d]
        return x + step[0], y + step[1]

    def _is_tail(self, x, y):
        """Is (x, y) the upstream end of a run?

        True when nothing feeds it, or when what feeds it is an underground
        exit whose entrance is elsewhere (the run already accounts for the
        jump from the entrance side).
        """
        return self._taken_from(x, y) is None

    def _trace(self, x, y):
        """Walk a whole run downstream from its tail. Returns (cells, length).

        `cells` is every drawn cell, tail first. `length` counts buried
        underground tiles too, so it is the number of item positions.
        """
        cells = [(x, y)]
        length = 1
        seen = {(x, y)}
        at = (x, y)
        while True:
            nxt = self._next_cell(*at)
            if nxt is None:
                break
            (nx, ny), buried = nxt
            # A loop would otherwise walk forever. A closed belt ring is a
            # legal thing to build, so stop rather than fail.
            if (nx, ny) in seen:
                break
            if length + buried + 1 > cell.MAX_SEGMENT_LEN:
                break
            length += buried + 1
            cells.append((nx, ny))
            seen.add((nx, ny))
            at = (nx, ny)
        return cells, length

    # --- rebuild ---------------------------------------------------------
    def clear(self):
        """Drop every segment and unstamp the cells that named them."""
        dropped = 0
        for seg in self.segments.values():
            dropped += seg.count
            for (x, y) in seg.cells:
                if self.board.in_bounds(x, y):
                    self.board.grid[y, x, cell.CH_W] = 0
                    self.board.mark_dirty(x, y)
            self._release_sid(seg.sid)
        self.stats["items_dropped"] += dropped
        self.segments.clear()

    def rebuild(self):
        """Re-derive every segment from the grid. Returns the segment count.

        Whole-board for now: simple, and correct by construction. An
        incremental version (rebuild only the runs touching an edited cell)
        is the obvious optimisation and is deliberately deferred -- the
        inspection panel reports rebuild cost so there is a number to point
        at before anyone optimises this.

        Items are dropped, by design: positions are relative to a segment's
        anchor, so they cannot survive a re-derivation. See the module
        docstring.
        """
        self.clear()

        grid = self.board.grid
        h, w = self.board.height, self.board.width
        types = cell.cell_type_arr(grid[:, :, cell.CH_X])
        # Candidate cells: belt-like and real. Vectorised because scanning
        # 512x512 in Python per edit is far too slow.
        candidates = (types == cell.TYPE_BELT) | (types == cell.TYPE_UNDER)
        # Only GHOST_PENDING is excluded; a frustrated cell's occupant is
        # real. See _cell_at().
        candidates &= (cell.ghost_state_arr(grid[:, :, cell.CH_Z])
                       != cell.GHOST_PENDING)

        built = 0
        claimed = set()
        ys, xs = candidates.nonzero()
        for (y, x) in zip(ys.tolist(), xs.tolist()):
            if (x, y) in claimed:
                continue
            if not self._is_tail(x, y):
                continue
            cells, length = self._trace(x, y)
            seg = self._make_segment(cells, length)
            claimed.update(cells)
            built += 1

        # A closed loop has no tail, so the scan above never starts one.
        # Pick any unclaimed cell and trace from there: the result is the same
        # ring, just cut at an arbitrary point.
        for (y, x) in zip(ys.tolist(), xs.tolist()):
            if (x, y) in claimed:
                continue
            cells, length = self._trace(x, y)
            self._make_segment(cells, length)
            claimed.update(cells)
            built += 1

        self.stats["rebuilds"] += 1
        self.stats["segments_built"] += built
        return built

    def _make_segment(self, cells, length):
        """Create a segment over `cells` and stamp its membership into .w."""
        sid = self._claim_sid()
        seg = Segment(sid, cells, length)
        self._allocate_ring(seg)
        self._resolve_ends(seg)
        self.segments[sid] = seg

        # Stamp each drawn cell with the segment and its offset from the tail.
        # Buried underground tiles advance the offset without a cell to write,
        # which is exactly why the offset is stored rather than inferred from
        # a cell's index in the list.
        off = 0
        prev = None
        for (x, y) in cells:
            if prev is not None:
                step = self._span_between(prev, (x, y))
                off += step
            self.board.grid[y, x, cell.CH_W] = cell.pack_segment(sid, off)
            self.board.mark_dirty(x, y)
            prev = (x, y)
        return seg

    def _span_between(self, a, b):
        """Positions advanced moving from cell `a` to the next drawn cell `b`.

        One for an ordinary step; more when an underground span was jumped.
        """
        dist = abs(b[0] - a[0]) + abs(b[1] - a[1])
        return max(1, dist)

    def _allocate_ring(self, seg):
        """Size and allocate the segment's item ring.

        Capacity is the number of items that fit nose-to-tail, rounded up to
        a power of two. The power of two is required, not merely tidy: the
        closed form indexes by `k % cap` on an ever-growing counter, which
        only survives wraparound when cap divides the counter's range.
        """
        import numpy as np

        want = max(MIN_RING_CAP, seg.length + 1)
        cap = 1
        while cap < want:
            cap *= 2
        seg.cap = cap
        seg.base = 0          # per-segment arrays for now; see note below.
        seg.enter_tick = np.zeros(cap, dtype=np.int64)
        seg.payload = np.zeros(cap, dtype=np.float32)

    def _resolve_ends(self, seg):
        """Record what feeds a segment and what drains it.

        Each end is (entity_row, face_direction) naming the *structure* port
        on the other side, or None for a dangling end. Stored on the segment
        so the tick does not re-derive it per item.
        """
        seg.src = self._node_behind(seg.cells[0])
        seg.dst = self._node_ahead(seg.cells[-1])

    def _node_behind(self, xy):
        """The structure output port feeding this cell, or None."""
        x, y = xy
        here = self._cell_at(x, y)
        if here is None:
            return None
        _t, facing, _row = here
        behind = (facing + 2) % 4
        step = cell.DIR_STEPS[behind]
        nx, ny = x + step[0], y + step[1]
        return self._port_at(nx, ny, (behind + 2) % 4, want_output=True)

    def _node_ahead(self, xy):
        """The structure input port this cell drains into, or None."""
        x, y = xy
        here = self._cell_at(x, y)
        if here is None:
            return None
        _t, facing, _row = here
        step = cell.DIR_STEPS[facing]
        nx, ny = x + step[0], y + step[1]
        return self._port_at(nx, ny, (facing + 2) % 4, want_output=False)

    def _port_at(self, x, y, toward, want_output):
        """(row, face) if a structure at (x, y) has a port facing `toward`.

        `toward` is the direction, in the structure's own frame, pointing back
        at the belt. An output port facing that way feeds the belt; an input
        port facing that way takes from it.
        """
        if not self.board.in_bounds(x, y):
            return None
        z = int(self.board.grid[y, x, cell.CH_Z])
        if cell.ghost_state(z) == cell.GHOST_PENDING:
            return None
        row = self.board.entity_id_at(x, y)
        if not row or not self.board.is_row_alive(row):
            return None
        view = self.board.view(row)
        t = view.type_id
        if t in _BELTLIKE or t == cell.TYPE_EMPTY:
            return None
        ins, outs = view.faces_for(*view.origin, view.direction, view.flipped)
        faces = outs if want_output else ins
        for (pxy, face) in faces:
            if pxy == (x, y) and face == toward:
                return row, face
        return None

    # --- audit ------------------------------------------------------------
    def audit(self):
        """Check the index against the grid. Returns a list of problems.

        This is the observability backstop: segments are derived state, and
        the failure mode of derived state is disagreeing with its source
        while still looking internally consistent. Surfaced in the inspection
        panel so it can be run on a live board.
        """
        problems = []
        grid = self.board.grid

        # Every stamped cell must name a live segment that claims it back.
        sids = cell.segment_id_arr(grid[:, :, cell.CH_W])
        ys, xs = sids.nonzero()
        stamped = set(zip(xs.tolist(), ys.tolist()))
        for (x, y) in stamped:
            sid = int(sids[y, x])
            seg = self.segments.get(sid)
            if seg is None:
                problems.append(f"({x},{y}) names dead segment {sid}")
                continue
            if (x, y) not in seg.cells:
                problems.append(f"({x},{y}) names segment {sid} which omits it")

        for seg in self.segments.values():
            # Cells must be stamped, in order, with the right offsets.
            off = 0
            prev = None
            for (x, y) in seg.cells:
                if not self.board.in_bounds(x, y):
                    problems.append(f"segment {seg.sid} cell ({x},{y}) off board")
                    continue
                if prev is not None:
                    off += self._span_between(prev, (x, y))
                prev = (x, y)
                got_sid, got_off = cell.unpack_segment(
                    int(grid[y, x, cell.CH_W]))
                if got_sid != seg.sid:
                    problems.append(
                        f"segment {seg.sid} cell ({x},{y}) stamped {got_sid}")
                elif got_off != off:
                    problems.append(
                        f"segment {seg.sid} cell ({x},{y}) offset "
                        f"{got_off}, expected {off}")
            # Ring invariants.
            if seg.cap & (seg.cap - 1):
                problems.append(
                    f"segment {seg.sid} cap {seg.cap} is not a power of two")
            if seg.count < 0:
                problems.append(f"segment {seg.sid} has negative item count")
            if seg.count > seg.cap:
                problems.append(
                    f"segment {seg.sid} holds {seg.count} items in cap {seg.cap}")
            if seg.length < len(seg.cells):
                problems.append(
                    f"segment {seg.sid} length {seg.length} < "
                    f"{len(seg.cells)} cells")

        # No cell may belong to two segments.
        owned = {}
        for seg in self.segments.values():
            for c in seg.cells:
                if c in owned:
                    problems.append(
                        f"cell {c} claimed by segments {owned[c]} and {seg.sid}")
                owned[c] = seg.sid

        # Every real belt-like cell should be on some segment.
        types = cell.cell_type_arr(grid[:, :, cell.CH_X])
        live = ((types == cell.TYPE_BELT) | (types == cell.TYPE_UNDER))
        live &= (cell.ghost_state_arr(grid[:, :, cell.CH_Z])
                 != cell.GHOST_PENDING)
        ys, xs = live.nonzero()
        for (y, x) in zip(ys.tolist(), xs.tolist()):
            if (x, y) not in owned:
                problems.append(f"belt cell ({x},{y}) is on no segment")

        return problems

    def __repr__(self):
        return (f"<SegmentIndex {len(self.segments)} segments, "
                f"{self.total_items()} items>")


def _self_check():
    """Geometry and tie-break rules that need no board."""
    # Straight-through always wins, from every facing.
    for facing in range(4):
        behind = (facing + 2) % 4
        assert take_dir(facing, [behind]) == behind, facing
        # ...even when a sideways candidate is also present.
        side = (facing + 1) % 4
        assert take_dir(facing, [side, behind]) == behind, facing
        assert take_dir(facing, [behind, side]) == behind, facing

    # With no straight-through, the CCW-most from our facing wins, and the
    # answer must not depend on the order candidates were discovered in.
    for facing in range(4):
        a, b = (facing + 1) % 4, (facing + 3) % 4
        assert take_dir(facing, [a, b]) == take_dir(facing, [b, a]), facing
        # Walking CCW from the facing, (facing+1) comes before (facing+3).
        assert take_dir(facing, [a, b]) == a, facing

    assert take_dir(0, []) is None
    # A single candidate always wins, whatever it is.
    for facing in range(4):
        for d in range(4):
            assert take_dir(facing, [d]) == d, (facing, d)

    # The rule is a pure function: same inputs, same answer, no state.
    assert take_dir(1, [0, 2, 3]) == take_dir(1, [3, 2, 0])

    assert is_beltlike(cell.TYPE_BELT) and is_beltlike(cell.TYPE_UNDER)
    assert not is_beltlike(cell.TYPE_SHEAR) and not is_beltlike(cell.TYPE_EMPTY)

    # A ring must be able to hold a full run plus the one arriving.
    assert MIN_RING_CAP & (MIN_RING_CAP - 1) == 0


_self_check()
