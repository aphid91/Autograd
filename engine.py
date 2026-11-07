"""Engine: owns the gameboard and exposes the API for mutating it.

Game logic goes here. Nothing else writes to the board -- the input manager
produces intents, the UI reads state, and this is the only layer that calls
Gameboard.spawn/despawn.

All placement goes through a BuildPlan (see builders.py), whatever the tool.
The engine does not know what a belt run is; it knows how to preview a plan,
commit a plan, and erase.
"""

from core import cell
from core.builders import BeltBuilder
from core.engine_paths import bresenham, intent_dir, path_directions, plan_path
from core.entity import Belt, Underground
from core.belt_device import BeltDevice
from core.belts import BeltSim
from core.gameboard import Gameboard
from core.segments import SegmentIndex
from core.gates import GateSim
from core.tick import Clock, PHASE_BELT, PHASE_STRUCTURE
from core import persistence

# Re-exported so existing callers and tests keep working after the path
# functions moved into engine_paths.
MODE_HORIZONTAL_FIRST = BeltBuilder.MODE_HORIZONTAL_FIRST
MODE_VERTICAL_FIRST = BeltBuilder.MODE_VERTICAL_FIRST
MODE_LINE = BeltBuilder.MODE_LINE
MODE_COUNT = BeltBuilder.MODE_COUNT
MODE_NAMES = BeltBuilder.MODE_NAMES

__all__ = [
    "Engine", "bresenham", "plan_path", "path_directions", "intent_dir",
    "MODE_HORIZONTAL_FIRST", "MODE_VERTICAL_FIRST", "MODE_LINE",
    "MODE_COUNT", "MODE_NAMES",
]


class Engine:
    def __init__(self, width=512, height=512):
        self.board = Gameboard(width, height)
        # The simulation clock. Owns the tick counter, the fixed-step
        # accumulator and the phase order; see core/tick.py. Subsystems
        # register their phase callbacks on it as they are built.
        self.clock = Clock()
        # Belt segments: derived from the grid, rebuilt whenever belts change.
        # The simulation ticks segments, not cells -- see core/segments.py.
        self.segments = SegmentIndex(self.board)
        #: Set when something has changed that segments depend on. Rebuilding
        #: is deferred to one point per frame rather than done per edit: an
        #: erase drag touches many cells, and a rebuild per cell would be
        #: wasted work on state nothing has read yet.
        self._segments_stale = True
        # Any grid write marks segments stale. Hooked at the board rather
        # than at each call site here, so a new edit path cannot forget to
        # invalidate -- see Gameboard._grid_changed().
        self.board.on_grid_change = self.invalidate_segments
        # Belt transport: the closed form from docs/belt_report.md. Registered
        # on the belt phase, so it runs after the structure phase every tick --
        # see core/tick.py for why that order is what lets a gate fire every
        # tick without a bubble.
        self.belts = BeltSim(self.segments)
        # Gates: the structure phase, plus the port pass that joins them to
        # belts. Registered on both phases -- see core/gates.py.
        self.gates = GateSim(self.board, self.segments, self.belts)
        self.clock.on_phase(PHASE_STRUCTURE, self._structure_phase)
        self.clock.on_phase(PHASE_BELT, self._belt_phase)
        # The GPU mirror of belt state. The fragment shader evaluates the
        # closed form itself, so it needs head/tail/anchor and the rings --
        # see core/belt_device.py.
        self.belt_device = BeltDevice(self.segments, self.belts)
        #: Debug feed, until gates drive the belts for real. When set, every
        #: segment with a dangling upstream end is offered this value each
        #: tick, and every dangling downstream end accepts. Exists so the
        #: transport can be watched on a real board before the gate layer
        #: lands; None disables it entirely.
        self.debug_feed = None
        # Ghost rows that will become real on commit.
        self.ghost_rows = []
        # Ghost rows that exist only to show the player what *would* fit, from
        # a placement that is blocked and will build nothing. They are
        # despawned on commit rather than promoted -- committing them would
        # place a fragment of a structure.
        self.preview_only_rows = []
        self.stats = {"placed": 0, "erased": 0}

    # --- queries --------------------------------------------------------
    def buildable(self, x, y):
        """Can a new entity go here? Occupied and out-of-bounds cells cannot."""
        return self.board.is_empty(x, y)

    def entity_at(self, x, y, skip_ghosts=True):
        """The real entity occupying (x, y), or None.

        Ghosts are skipped by default, which is what the pipette needs: a
        preview ghost sits under the cursor at the moment Q is pressed, so
        picking up the topmost entity would hand back whatever the player is
        already holding instead of what is built there.
        """
        view = self.board.entity_at(x, y)
        if view is None:
            return None
        if skip_ghosts and view.is_ghost:
            return None
        return view

    # --- plan preview / commit ------------------------------------------
    def clear_ghosts(self):
        """Drop the whole preview: ghost entities and frustration overlays.

        Both halves matter. Ghost entities own rows and cells and must be
        despawned; frustration overlays own neither -- they are a flag on
        somebody else's cell -- and must be unset without disturbing the
        occupant.
        """
        for row in self.ghost_rows:
            self.board.despawn(row)
        self.ghost_rows.clear()
        for row in self.preview_only_rows:
            self.board.despawn(row)
        self.preview_only_rows.clear()
        self.board.clear_frustrated()

    def preview(self, builder, start, end, start_frac=(0.5, 0.5),
                end_frac=(0.5, 0.5)):
        """Replace the current preview with what `builder` plans for a gesture.

        The old preview is cleared *before* the new plan is computed. This
        ordering is not incidental: ghosts are real entities occupying real
        cells, so planning against a board that still holds the previous
        frame's preview makes a tool collide with itself -- drag a structure
        one cell sideways and the overlap with its own last position reads as
        blocked. Clearing first means a plan only ever sees committed entities.
        """
        self.clear_ghosts()
        plan = builder.plan(self, start, end, start_frac, end_frac)
        self._apply_preview(plan)
        return plan

    def preview_plan(self, plan):
        """Show a pre-computed plan.

        Prefer preview(builder, ...) -- a plan built while an old preview is
        still on the board has already seen those ghosts as obstacles. This
        exists for tests and for callers that genuinely have a plan in hand.
        """
        self.clear_ghosts()
        self._apply_preview(plan)
        return plan

    def _apply_preview(self, plan):
        """Ghost what the player would get, and tint what is in the way.

        A placement that will commit is ghosted as a whole entity. A frustrated
        one still shows the cells that *would* fit, so the player can see how
        far to move to clear the obstruction -- but it cannot be spawned as an
        entity, since its origin may be the blocked cell and a partial
        footprint is not a valid entity. Those cells are stamped directly as
        single-cell ghosts of the same type instead.
        """
        blocked = set(plan.frustrated_cells)

        for p in plan.previewable:
            if not p.frustrated:
                extra = self._spawn_fields(p)
                view = self.board.spawn(
                    p.entity_cls, p.x, p.y, p.direction, is_ghost=True,
                    flipped=p.flipped, **extra
                )
                # A ghost end that *would* pair shows the dotted span while
                # you hover, which is the whole point of the preview. Point
                # it at the real partner's row so the cell's paired bit is
                # set; the partner is not touched, so this stays one-way and
                # disappears with the ghost. Real pairing happens on commit.
                if (p.entity_cls is Underground
                        and p.under_partner is not None):
                    row = self.board.entity_id_at(*p.under_partner)
                    if row:
                        view.partner = row
                        self.board.restamp(view)
                self.ghost_rows.append(view.row)
                continue

            # Partial preview: ghost only the cells that are actually free,
            # one cell at a time. spawn() would stamp the whole footprint and
            # so overwrite the very cells blocking this placement. These rows
            # are preview-only -- this placement commits nothing.
            for (cx, cy) in p.cells():
                if (cx, cy) in blocked or not self.buildable(cx, cy):
                    continue
                view = self.board.spawn_cell_ghost(
                    p.entity_cls, cx, cy, p.direction, flipped=p.flipped
                )
                if view is not None:
                    self.preview_only_rows.append(view.row)

        for (x, y) in blocked:
            self.board.set_frustrated(x, y)

    def commit_ghosts(self, paired_cells=()):
        """Turn the ghost preview into real entities. Returns the count.

        Preview-only rows are discarded rather than promoted: they are
        fragments of a blocked placement, and half a structure is not a thing
        that can exist.

        `paired_cells` names underground ends whose partner the *plan* already
        decided -- a pasted pair, which must join to itself. Those are joined
        here, before the search below runs, because the search's
        nearest-candidate rule would otherwise grab an unrelated unpaired end
        sitting at the paste site and leave the pair's real other half with
        nothing. Pairing explicitly first also marks both ends `paired`, so
        the search skips them naturally.
        """
        n = len(self.ghost_rows)
        promoted = list(self.ghost_rows)
        for row in promoted:
            self.board.set_ghost(row, False)
        self.ghost_rows.clear()

        # Explicit pairs first -- see `paired_cells` above.
        for (a_xy, b_xy) in paired_cells:
            a = self.board.entity_at(*a_xy)
            b = self.board.entity_at(*b_xy)
            if a is None or b is None:
                continue
            if a.type_id != cell.TYPE_UNDER or b.type_id != cell.TYPE_UNDER:
                continue
            if a.is_ghost or b.is_ghost or a.row == b.row:
                continue
            # A preview's pairing is **one-way**: the ghost exit points at
            # the entrance, which never points back, purely so the span
            # draws. `paired` therefore reads true on that end even though no
            # real pair exists, so testing occupancy directly would skip this
            # pair -- and the nearest-candidate search below would then hand
            # the exit to an unrelated end at the paste site, which is the
            # exact bug this path exists to prevent.
            #
            # So ask whether either end is committed to *somebody else*, and
            # treat a link pointing at our intended partner as the half-built
            # state it is. pair_under() makes it two-way.
            if self._under_spoken_for(a, b.row) \
                    or self._under_spoken_for(b, a.row):
                continue
            self.pair_under(a, b)

        # Pair the rest only once they are real. find_under_partner()
        # skips ghosts, so a pair cannot form while either end is still a
        # preview -- which is what keeps a hovering exit from latching onto
        # an entrance it has not actually been placed near yet.
        for row in promoted:
            if not self.board.is_row_alive(row):
                continue
            view = self.board.view(row)
            if view.type_id != cell.TYPE_UNDER:
                continue
            # Skip ends the explicit pass above already joined. The test has
            # to be *mutual*: a one-way link is a leftover preview and must
            # still be re-derived, which is what the clear below handles.
            if self._under_mutually_paired(view):
                continue
            # A preview's pairing is one-way: the ghost pointed at a partner
            # that never pointed back, purely so the span would draw. Drop
            # it and pair properly now, or `paired` would read true here and
            # the real two-way link would never be made.
            view.partner = 0
            ox, oy = view.origin
            partner = self.find_under_partner(
                ox, oy, view.direction, view.is_exit)
            if partner is not None:
                self.pair_under(view, partner)
        for row in self.preview_only_rows:
            self.board.despawn(row)
        self.preview_only_rows.clear()
        self.board.clear_frustrated()
        self.stats["placed"] += n
        return n

    def _spawn_fields(self, placement):
        """Per-type spawn kwargs a placement carries, as `initialise` names.

        One place rather than a branch at each spawn site, so a new per-type
        field is added once. Only non-default values are passed, which keeps
        the normal path identical to what it was before any of these existed.
        """
        extra = {}
        if placement.entity_cls is Underground:
            extra["is_exit"] = placement.is_exit
        if placement.emit is not None:
            extra["emit"] = placement.emit
        return extra

    def build_plan(self, plan):
        """Commit a plan directly, with no preview step."""
        rows = []
        for p in plan.buildable:
            view = self.board.spawn(p.entity_cls, p.x, p.y, p.direction,
                                    flipped=p.flipped,
                                    **self._spawn_fields(p))
            rows.append(view.row)
        self.stats["placed"] += len(rows)
        return rows

    def _under_mutually_paired(self, end):
        """True only for a real two-way pairing.

        `Underground.paired` is satisfied by a one-way link, which a preview
        legitimately creates (a ghost exit points at an entrance that never
        points back, so the span draws). Anything deciding whether a pair is
        already *made* has to check both directions.
        """
        row = end.partner
        if not row or not self.board.is_row_alive(row):
            return False
        return self.board.view(row).partner == end.row

    def _under_spoken_for(self, end, intended_row):
        """Is this underground end already committed to someone *else*?

        A link to `intended_row` does not count -- that is either the pair we
        are about to complete or a preview's one-way link to it. A link to any
        other live row does, and must not be stolen.
        """
        row = end.partner
        if not row or row == intended_row:
            return False
        return self.board.is_row_alive(row)

    @staticmethod
    def planned_under_pairs(plan):
        """[(cell_a, cell_b)] for underground ends a plan pairs explicitly.

        A paste records which ends were copied *together* (as
        `Placement.under_partner`), and that beats re-deriving: a pasted pair
        must join to itself rather than to some unpaired end that happens to
        sit near the paste site. Feed the result to commit_ghosts().

        Only pairs where **both** halves will actually be built: under a
        partial paste one may have been refused, and a survivor pointing at a
        row that was never created is precisely the dangling state
        unpair_under() exists to avoid.
        """
        if plan is None:
            return []
        built = {(p.x, p.y) for p in plan.buildable}
        pairs = []
        seen = set()
        for p in plan.buildable:
            if p.entity_cls is not Underground or p.under_partner is None:
                continue
            a, b = (p.x, p.y), tuple(p.under_partner)
            if b not in built:
                continue
            key = tuple(sorted((a, b)))
            if key in seen:
                continue
            seen.add(key)
            pairs.append((a, b))
        return pairs

    # --- direct mutation (convenience; tests and seeding) ---------------
    def place_belt(self, x, y, direction=cell.DIR_PX):
        if not self.buildable(x, y):
            return None
        belt = self.board.spawn(Belt, x, y, direction)
        self.stats["placed"] += 1
        return belt

    def place_belt_path(self, start, end, mode=MODE_HORIZONTAL_FIRST):
        """Build a belt run directly, skipping blocked cells."""
        builder = BeltBuilder()
        builder.mode = mode
        plan = builder.plan(self, start, end)
        self.build_plan(plan)
        return [(p.x, p.y) for p in plan.buildable]

    def place_structure(self, entity_cls, x, y, direction=cell.DIR_PX,
                        flipped=False):
        """Place one structure if its whole footprint is free."""
        blocked = [c for c in entity_cls.cells_for(x, y, direction, flipped)
                   if not self.buildable(*c)]
        if blocked:
            return None
        view = self.board.spawn(entity_cls, x, y, direction, flipped=flipped)
        self.stats["placed"] += 1
        return view

    # --- persistence -----------------------------------------------------
    def save_board(self, path=None):
        """Quicksave. Clears the preview first, since ghosts are not board
        state -- they belong to a gesture in flight."""
        path = path or persistence.DEFAULT_SAVE
        self.clear_ghosts()
        return persistence.save(self.board, path)

    def load_board(self, path=None):
        """Quickload, replacing everything. Returns the entity count.

        Any in-flight preview is dropped: its ghost rows index the *old*
        entity table and would dangle into the new one.
        """
        path = path or persistence.DEFAULT_SAVE
        self.clear_ghosts()
        n = persistence.load(self.board, path)
        self.ghost_rows.clear()
        self.preview_only_rows.clear()
        # A new board is a new timeline. Item positions are a closed-form
        # function of the tick counter, so carrying the old count over would
        # date every item on the loaded board to a tick that never happened.
        self.clock.reset()
        return n

    # --- underground belts ----------------------------------------------
    def find_under_partner(self, x, y, direction, want_exit):
        """An unpaired end this one would pair with, or None.

        Looks back along `direction` from the cell being placed. A new exit
        searches *upstream* for an entrance; a new entrance searches
        *downstream* for an exit. Both ends of a pair share a facing, so the
        search axis is the facing itself.

        The nearest candidate wins, which is what makes laying a long run
        feel right: a fresh exit joins the entrance you just placed rather
        than some older one further back. Nothing in between blocks -- the
        span passes underneath whatever is there.
        """
        step = cell.DIR_STEPS[direction]
        # An exit looks back the way material came; an entrance looks ahead.
        sign = -1 if want_exit else 1
        for dist in range(1, Underground.MAX_SPAN + 1):
            cx = x + step[0] * sign * dist
            cy = y + step[1] * sign * dist
            if not self.board.in_bounds(cx, cy):
                break
            row = self.board.entity_id_at(cx, cy)
            if row == 0 or not self.board.is_row_alive(row):
                continue
            if row in self.ghost_rows or row in self.preview_only_rows:
                continue
            view = self.board.view(row)
            if view.type_id != cell.TYPE_UNDER:
                continue
            # Must be the opposite role, same facing, and still free.
            if view.is_exit == want_exit:
                continue
            if view.direction != direction or view.paired:
                continue
            return view
        return None

    def pair_under(self, a, b):
        """Link two underground ends and restamp both cells."""
        a.partner = b.row
        b.partner = a.row
        self.board.restamp(a)
        self.board.restamp(b)

    def unpair_under(self, row):
        """Drop the link from whatever was paired with `row`."""
        if row == 0 or not self.board.is_row_alive(row):
            return
        view = self.board.view(row)
        if view.type_id != cell.TYPE_UNDER:
            return
        other = view.partner
        if other == 0 or not self.board.is_row_alive(other):
            return
        mate = self.board.view(other)
        if mate.type_id == cell.TYPE_UNDER and mate.partner == row:
            mate.partner = 0
            self.board.restamp(mate)

    def place_under(self, x, y, direction=cell.DIR_PX, is_exit=False):
        """Place one underground end, auto-pairing it if a partner is free."""
        if not self.buildable(x, y):
            return None
        partner = self.find_under_partner(x, y, direction, is_exit)
        view = self.board.spawn(Underground, x, y, direction,
                                is_exit=is_exit)
        if partner is not None:
            self.pair_under(view, partner)
        self.stats["placed"] += 1
        return view

    def erase(self, x, y):
        """Remove whatever occupies a cell. Ghosts are left alone.

        Erasing any cell of a multi-cell entity removes the whole entity --
        there is no such thing as half a structure.
        """
        row = self.board.entity_id_at(x, y)
        if row == 0 or not self.board.is_row_alive(row):
            return False
        if row in self.ghost_rows or row in self.preview_only_rows:
            return False
        # An underground's partner outlives it, so it must be told: a
        # surviving end left pointing at a freed row would draw a dotted
        # span to nothing, and could re-pair wrongly once the row is reused.
        self.unpair_under(row)
        self.board.despawn(row)
        self.stats["erased"] += 1
        return True

    def clear_board(self):
        self.clear_ghosts()
        rows = [r for r in range(1, self.board.capacity)
                if self.board.is_row_alive(r)]
        for r in rows:
            self.board.despawn(r)
        # Nothing is left to carry a history, so restart the clock too --
        # same reasoning as load_board().
        self.clock.reset()

    # --- simulation ------------------------------------------------------
    def invalidate_segments(self):
        """Mark the segment index as needing a rebuild.

        Called by every path that changes what a run looks like. Cheap and
        idempotent: the rebuild itself happens once per frame in
        sync_segments(), so an erase drag over fifty cells still rebuilds
        once rather than fifty times.
        """
        self._segments_stale = True

    def sync_segments(self):
        """Rebuild the segment index if anything invalidated it.

        Returns the number of segments if a rebuild happened, else None.
        Called once per frame before the tick, so the simulation always runs
        against segments matching the board the player just edited.
        """
        if not self._segments_stale:
            return None
        self._segments_stale = False
        n = self.segments.rebuild()
        # A rebuild reassigns every ring's base and cap, so the device arena
        # has to be laid out again before the next upload.
        self.belt_device.invalidate_layout()
        return n

    def clear_items(self):
        """Take every item off every belt, leaving the segmentation alone.

        Resets each ring to empty and the anchor to the far end, which is the
        initial state from docs/belt_report.md. Distinct from a rebuild: the
        runs survive, only what is on them is discarded.
        """
        for seg in self.segments.segments.values():
            seg.head = seg.tail = 0
            seg.anchor = self.belts.length_of(seg)

    def flush_material(self):
        """Discard every item in flight, on belts *and* inside gates.

        `clear_items()` alone is not enough for this: a value already pulled
        off a belt sits in a gate's input channel, and a result already
        computed sits in its output waiting zone, so clearing only the rings
        leaves material that re-enters the network on the next tick. After a
        Source's emitted value changes, that stale material is exactly what
        the player does not want to see trickling out mixed with the new
        value.

        Deliberately a board-wide reset rather than a targeted one. Tracing
        which items descend from one Source means knowing an item's
        provenance, which the closed form does not store -- an item is a
        payload and an entry tick, nothing more. Clearing everything is the
        honest version of "start fresh", and it is the same bargain an edit
        to the belt network already makes (see "An edit drops the items on
        the segments it touches").
        """
        self.clear_items()
        for gate in self.gates.gates():
            for c in range(gate.input_count()):
                gate.clear_input(c)
            for c in range(gate.output_count()):
                gate.clear_output(c)

    def _structure_phase(self):
        """Fire every gate that can. Registered on PHASE_STRUCTURE.

        Runs before the belt phase, which is what lets a well-fed gate fire
        every tick with no bubble -- see core/tick.py for the trace.
        """
        self.gates.set_tick(self.clock.tick)
        self.gates.structure_phase()

    def _belt_phase(self):
        """The belt half of a tick. Registered on PHASE_BELT.

        One BeltSim.tick() with the gate callbacks wired in, so the report's
        advance-pop-push order holds within each segment -- splitting the
        gate work into passes around it inverts pop and push and silently
        costs about one tick in seven. See core/gates.py.

        Direct gate-to-gate transfers run after, since they involve no
        segment for the belt tick to own.
        """
        tick = self.clock.tick
        self.gates.set_tick(tick)

        if self.debug_feed is None:
            self.gates.tick_belts(tick)
        else:
            # The debug feed stands in for a Source/Sink pair on runs that
            # have neither, so transport can be exercised without building a
            # whole chain. A run wired to a real gate keeps the gate's
            # callbacks; only dangling ends get the feed.
            value = float(self.debug_feed)
            gates = self.gates

            def offer(seg):
                if seg.src is not None:
                    return gates.offer_to(seg)
                return value

            def accept(seg, payload):
                if seg.dst is not None:
                    return gates.accept_from(seg, payload)
                return True

            self.belts.tick(tick, offer=offer, accept=accept,
                            took=gates.took_from)

        self.gates.deliver_direct()

    def tick(self, dt):
        """Advance the simulation clock, running whatever ticks are due.

        Called once per frame by the orchestrator, after all player mutation
        and before the device sync -- so a tick's effects reach the GPU in the
        same frame they happen.
        """
        return self.clock.advance(dt)

    # --- device ---------------------------------------------------------
    def create_device_resources(self, ctx):
        self.board.create_device_resources(ctx)
        self.belt_device.create_device_resources(ctx)

    def sync_to_device(self):
        """Upload whatever is dirty. Called once per frame by the orchestrator."""
        # Belt state goes up unconditionally: head/tail/anchor change on
        # nearly every tick of every segment, so tracking which moved would
        # cost more than the few KB the upload takes.
        self.belt_device.sync()
        return self.board.to_device()
