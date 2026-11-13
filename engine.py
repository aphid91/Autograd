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
from render import compute

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
        # Belt transport: the closed form from docs/belt_report.md. Registered
        # on the belt phase, so it runs after the structure phase every tick --
        # see core/tick.py for why that order is what lets a gate fire every
        # tick without a bubble.
        self.belts = BeltSim(self.segments)
        # Gates: the structure phase, plus the port pass that joins them to
        # belts. Registered on both phases -- see core/gates.py.
        self.gates = GateSim(self.board, self.segments, self.belts)
        # Any grid write marks segments stale and drops the gate layer's
        # cached connectivity. Hooked at the board rather than at each call
        # site here, so a new edit path cannot forget to invalidate -- see
        # Gameboard._grid_changed().
        #
        # Hooked *after* GateSim is built, not beside the SegmentIndex above:
        # invalidate_segments() reaches through self.gates, so an earlier
        # assignment would leave a window in which a grid write raises
        # AttributeError.
        self.board.on_grid_change = self.invalidate_segments
        self.clock.on_phase(PHASE_STRUCTURE, self._structure_phase)
        self.clock.on_phase(PHASE_BELT, self._belt_phase)
        # Point the subsystems' sub-phase probes at the clock's profiler --
        # the one the player arms. Both ship a disarmed stand-in so they are
        # constructible without a clock; this is the wiring that makes them
        # report into the same tree. Assigned rather than passed in because
        # the clock is wired to the phases here, not in their constructors.
        self.belts.profiler = self.clock.profiler
        self.gates.profiler = self.clock.profiler
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
        #: True while the device holds simulation state the host has not read
        #: back. See the "GPU tick" block below -- this is the inverted dirty
        #: flag, marking what the *CPU* must be told about.
        self._gpu_dirty = False
        #: True when `settle()` ran this frame, so the belt buffers are the
        #: device's own current copy and must not be re-packed from the host.
        #: Distinct from `_gpu_dirty`, which is already clear by then --
        #: see `sync_to_device()`.
        self._gpu_settled_this_frame = False
        #: True when the host has *written* simulation state since the device
        #: last ran, as against merely read it. A write is a producer event
        #: the device's mailbox never saw, so the GPU sleep layer has to be
        #: woken before the next dispatch -- see `_gpu_tick()`. Kept apart
        #: from `_gpu_dirty` because an ordinary readback must stay free.
        self._gpu_host_wrote = False
        #: Player-facing switch for the compute path, so the CPU and GPU
        #: timelines can be compared in the running app. On when a device
        #: exists; the panel exposes it.
        self._gpu_enabled = True
        #: Why the compute path is unavailable, if it failed to build.
        self.gpu_error = None
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

    def placement_matches(self, placement):
        """Is `placement` already built, exactly, where it wants to go?

        "Exactly" means one entity covering the placement's whole footprint
        with the same type, origin, orientation and flip. That is what lets a
        lined-up stamp be substituted in: the cells it wants are occupied by
        the thing it was going to put there, so deleting and re-placing is a
        no-op geometrically and carries the *new* per-instance settings.

        Checked per **placement**, never per cell, and the origin is part of
        it. Two 2x1 Shears offset by one cell overlap in a single cell that
        would compare equal type-and-facing on its own, so a per-cell test
        would call a half-overlap a match and substitute a structure one cell
        from where the player saw it. Comparing the origin makes the two
        footprints the same footprint rather than merely compatible.

        Returns the matching row, or None. A row rather than a bool because
        the substitution has to erase exactly these entities, and finding
        them again later would mean trusting the board not to have changed.
        """
        cells = placement.cells()
        if not cells:
            return None
        first = self.board.entity_at(*cells[0])
        if first is None or first.is_ghost:
            return None
        if type(first) is not placement.entity_cls:
            return None
        if first.direction != placement.direction:
            return None
        if bool(first.flipped) != bool(placement.flipped):
            return None
        if tuple(first.origin) != (placement.x, placement.y):
            return None
        # An underground's role is part of its identity: an entrance and an
        # exit of the same facing are different machines, and substituting
        # one for the other would quietly reverse where material surfaces.
        if placement.entity_cls is Underground:
            if bool(first.is_exit) != bool(placement.is_exit):
                return None
        # Every cell must be that same entity -- which is what actually
        # rejects a half-overlap, and the reason this is a per-placement test
        # rather than a per-cell one.
        #
        # Mutation-testing shows the origin comparison above is **redundant**
        # given this loop: two instances of one type can only disagree about
        # their origin by covering a cell the other does not, so some cell
        # here resolves to a different row (or to none) and this rejects it.
        # It stays because it states the invariant the loop merely implies --
        # "same footprint", not "footprints that happen to agree cell by
        # cell" -- and because it is the check that would still hold if a
        # future type's footprint could contain another's.
        for c in cells[1:]:
            other = self.board.entity_at(*c)
            if other is None or other.row != first.row:
                return None
        return first.row

    def matched_rows(self, plan):
        """Rows a plan would substitute, or None if it cannot be substituted.

        `None` -- not an empty list -- when the plan has any frustration that
        is *not* a match, because substitution is all-or-nothing: deleting the
        matched entities would not unblock the genuinely-clashing ones, so the
        plan would still refuse and the player would have lost the matched
        ones for nothing.

        An empty list is a real answer meaning "nothing to delete" (a plan
        that was never blocked). Callers distinguish the two.
        """
        if plan is None:
            return None
        rows = []
        for p in plan.placements:
            if not p.frustrated:
                continue
            row = self.placement_matches(p)
            if row is None:
                return None
            rows.append(row)
        return rows

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
        # Cells whose occupant is exactly what the placement wanted. Tinted
        # differently so the player can see a stamp is lined up, and the set
        # the commit path substitutes. Collected per placement, because a
        # match is a property of a whole footprint -- see placement_matches().
        matched = set()
        for p in plan.previewable:
            if p.frustrated and self.placement_matches(p) is not None:
                matched.update(p.cells())

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
            self.board.set_frustrated(x, y, matched=(x, y) in matched)

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
        if placement.seed is not None:
            on, value = placement.seed
            extra["seed_on"] = on
            extra["seed_value"] = value
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
    def save_board(self, path=None, annotations=None):
        """Quicksave. Clears the preview first, since ghosts are not board
        state -- they belong to a gesture in flight.

        `annotations` is the orchestrator's `AnnotationStore`, saved inside
        the same file. The engine does not own it -- annotations carry no
        simulation state and never touch the grid -- so it is passed through
        rather than held.
        """
        path = path or persistence.DEFAULT_SAVE
        # A save serialises entity rows and ring state, so the host's copy
        # has to be the current one -- otherwise the file records the board
        # as it was before however many ticks are still outstanding on the
        # device.
        self.settle()
        self.clear_ghosts()
        return persistence.save(self.board, path, annotations=annotations)

    def load_board(self, path=None, annotations=None):
        """Quickload, replacing everything. Returns the entity count.

        Any in-flight preview is dropped: its ghost rows index the *old*
        entity table and would dangle into the new one.

        `annotations`, if given, is replaced from the file -- including being
        emptied when the file has none, so the outgoing board's notes never
        linger over the incoming one.
        """
        path = path or persistence.DEFAULT_SAVE
        self.clear_ghosts()
        n = persistence.load(self.board, path, annotations=annotations)
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

    # --- rotating and flipping what is already built --------------------
    def turn_in_place(self, x, y, backward=False):
        """Rotate the entity at (x, y) without moving it. True if it turned.

        The empty hand's R. In-place rather than erase-and-replace, which is
        what keeps per-instance settings: the row is kept and only its facing
        changes, so a Source's emit value and a belt's seed travel with the
        turn for free rather than needing to be copied across.

        **The cell set never changes**, so this can never be blocked and
        needs no placement check -- `Entity.inplace_turns()` only offers
        rotations that preserve the footprint. Many machines are 2x1, whose
        only such rotation is a half turn, so R and shift+R do the same thing
        on them; a square footprint takes quarter turns in either direction.
        A type that cannot turn at all returns False and nothing happens,
        which is the stated policy for a shape that would have to move.

        An underground is the one type with its own answer: both R and F mean
        "reverse this pair", because reversing a run is the common edit and
        tearing up the undergrounds to do it is the irritation. See
        `reverse_under()`.
        """
        view = self.entity_at(x, y)
        if view is None:
            return False
        if view.type_id == cell.TYPE_UNDER:
            return self.reverse_under(view)
        turns = view.inplace_turns()
        if not turns:
            return False
        # The turn to apply, as quarter-turns CCW. A single permitted turn
        # (the 2x1 case) ignores the direction entirely -- there is only one
        # rotation that preserves the footprint, so shift cannot mean a
        # different one.
        step = turns[0] if len(turns) == 1 else (turns[-1] if backward
                                                 else turns[0])
        view.direction = cell.rotate_dir_ccw_n(view.direction, step)
        self._restamp_turned(view)
        return True

    def flip_in_place(self, x, y):
        """Mirror the entity at (x, y) where it stands. True if it flipped.

        The empty hand's F. A flip never changes the extent -- it permutes
        cells within the footprint's bounding box -- so unlike a rotation
        this is footprint-preserving for *every* type and needs no geometry
        test. What it does need is for the mirror to mean something: an
        achiral type (only Sink) looks identical mirrored, and a belt has no
        ports to mirror at all, so both report False rather than dirtying the
        board for no visible change.
        """
        view = self.entity_at(x, y)
        if view is None:
            return False
        if view.type_id == cell.TYPE_UNDER:
            # F and R are the same gesture on an underground: reverse.
            return self.reverse_under(view)
        if not view.INPLACE_TURNABLE or not view.is_chiral():
            return False
        view.flipped = not view.flipped
        self._restamp_turned(view)
        return True

    def _restamp_turned(self, view):
        """Publish an orientation change to the grid and the simulation.

        The row is the source of truth for the facing but the *cells* carry a
        copy -- `.x`'s direction field and `.z`'s flip bit, both of which the
        renderer reads per-pixel with no row lookup -- so a turn that skipped
        this would leave the entity drawn at its old orientation.

        Restamping writes the cells, and `Gameboard.write_cell()` notifies
        the segment hook, so the belt network re-derives on its own: a turned
        belt joins a different run, and a turned machine feeds a different
        one. The extent is deliberately *not* rewritten -- a footprint
        preserving turn leaves it alone, which is the invariant that makes
        this safe.
        """
        self.board.restamp(view)
        self.stats["turned"] = self.stats.get("turned", 0) + 1

    def reverse_under(self, view):
        """Swap an underground end's role, and its partner's. True if changed.

        Both R and F mean this, because an underground has no other sensible
        in-place transform: it is 1x1 so a quarter turn is footprint-legal,
        but turning one end alone takes it off the axis it shares with its
        partner and breaks a pair the player did not ask to break.
        Reversing is the edit actually wanted -- "run this underground the
        other way" -- and doing it to both ends at once is what makes
        reversing a whole belt run cheap instead of a teardown.

        **Swapping the roles must also reverse the facing.** The exit is
        downstream of the entrance *by position*, so toggling `is_exit` alone
        would leave an exit sitting upstream of its own entrance -- the span
        traversed backwards, which is geometrically incoherent rather than
        merely surprising. Reversing `direction` on both ends puts the pair
        back in agreement: the end that was diving now surfaces, the material
        flows the other way, and neither cell moves.

        An unpaired end reverses on its own, which is the same operation with
        nothing to keep in step.
        """
        mate = None
        if view.paired:
            candidate = self.board.view(view.partner)
            # Only follow a link the other end agrees with. A preview's
            # pairing is one-way (see commit_ghosts), so a ghost exit can
            # point at an entrance that does not point back, and dragging
            # that entrance's role around would edit a real entity on the
            # strength of a preview.
            if (candidate.type_id == cell.TYPE_UNDER
                    and candidate.partner == view.row
                    and not candidate.is_ghost):
                mate = candidate
        for end in (view, mate):
            if end is None:
                continue
            end.is_exit = not end.is_exit
            end.direction = cell.rotate_dir_ccw_n(end.direction, 2)
            self._restamp_turned(end)
        return True

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

        Also drops the gate layer's cached output connectivity, which is
        derived from the same two things a segment rebuild is -- the grid and
        the segmentation -- and so goes stale at exactly the same moments.
        Hanging it here rather than at each edit site is the same reasoning
        that put the segment invalidation on `Gameboard.on_grid_change`: a new
        edit path cannot forget it.

        **`invalidate_board()` rather than `invalidate_connectivity()`**,
        because this is the *edit* path and so is also where the constant fold
        has to die. The fold deliberately survives the gate layer's per-tick
        cache expiry (see the comment in `GateSim.invalidate_connectivity()`),
        which means something has to drop it when the board really changes --
        and this method, reached by every path that changes what a run looks
        like, is the one place that is true of.
        """
        # **Bring the host level before anything re-derives from it.** An
        # edit rebuilds segments and re-resolves ports from entity rows and
        # ring state, so a device that is ahead has to be read back first --
        # otherwise the rebuild reads a stale board and the dispatches'
        # results are then overwritten by the upload that follows, silently
        # rewinding the simulation by however many ticks were outstanding.
        #
        # Here rather than at each edit site for the same reason the
        # invalidation itself is here: this is the one method every real edit
        # goes through, so a new edit path cannot forget it.
        self.settle()
        self._segments_stale = True
        self.gates.invalidate_board()

    def sync_segments(self):
        """Rebuild the segment index if anything invalidated it.

        Returns the number of segments if a rebuild happened, else None.
        Called once per frame before the tick, so the simulation always runs
        against segments matching the board the player just edited.

        **A rebuild flushes gate state as well as the belts.** `rebuild()`
        drops every item on every run -- positions are relative to a
        segment's anchor and so cannot survive re-derivation -- but it only
        touches segments, and material that has already *left* a belt is
        sitting in an entity row: a value in a gate's input channel, or a
        result in its output waiting zone. Those are untouched by a rebuild,
        so the next tick pushes them back onto the fresh runs and they appear
        as new items on belts the player just watched empty.

        That was a real bug, and the giveaway is that it is bounded rather
        than continuous: roughly one stray item per gate port that happened
        to be holding something, so a one-gate chain resurrected two values
        and a two-gate chain two more. Easy to dismiss as the Source simply
        refilling.

        Flushing the gates too makes `flush_material()` the single meaning of
        "start fresh" -- the same thing a Source's value change does. It is
        deliberately board-wide: an item is a payload and an entry tick with
        no record of which machine produced it, so "only the gates near the
        edit" is not a question the closed form can answer. Edits are
        human-paced, and this is the bargain the belts already make.
        """
        if not self._segments_stale:
            return None
        self._segments_stale = False
        # Timed as `rebuild`, a **root** beside the phases rather than a child
        # of either. It is not part of a tick: it happens at most once a frame
        # and only after an edit, so folding it into the tick tree would
        # average an 88ms edit cost across every quiet tick and describe
        # neither. It still belongs in the dump, because "why did placing that
        # belt stutter" and "why is my tick slow" are the two questions a
        # player has and only one of them is about the tick.
        with self.clock.profiler.probe("rebuild"):
            n = self._rebuild_segments()
        # **Re-pack the device's per-edit state, here and nowhere else.**
        # Everything the kernels need that is not row or ring state -- the
        # gate slot assignment, the geometry word, the drains mask, the
        # direct edge list, the segment port table -- is derived from the
        # grid and the segmentation, so it goes stale at exactly the moment
        # a rebuild happens and is correct again the moment one finishes.
        #
        # Missing this is silent and total: `_gate_table()` is cached, so a
        # device built before the first placement keeps an *empty* gate table
        # for ever. Every dispatch then runs over zero gates and the factory
        # simply never starts, with no error anywhere -- which is exactly how
        # it presented ("the Source should have filled the run").
        if self.gates.device is not None:
            self.gates.sync_device()
        return n

    def _rebuild_segments(self):
        """The rebuild itself. Split out only so `sync_segments()` can time it."""
        n = self.segments.rebuild()
        # Every segment id has just been reassigned, so the sleep set refers
        # to runs that no longer exist -- and an id handed to a *different*
        # run would have it born asleep. Waking everything is the safe reset:
        # a spuriously awake segment costs one tick and then sleeps again.
        self.belts.wake_all()
        # And every gate, for a sharper version of the same reason: a gate
        # sleeps on the claim that its neighbours are quiet, and a rebuild
        # has just replaced every one of those neighbours. The gate rows
        # themselves survive, so unlike the segments nothing here would look
        # obviously wrong -- the sleep set would simply be reasoning about an
        # adjacency that no longer exists.
        self.gates.wake_all_gates()
        # Order matters: rebuild() first, so flush_material() resets the
        # anchors of the runs that now exist rather than the ones that just
        # stopped existing.
        self.flush_material()
        # ...and the seeds go on *after* the flush, for the same reason in
        # reverse: a flush empties every ring, so seeding before it would
        # wipe exactly what was just placed.
        self.seed_segments()
        # A rebuild reassigns every ring's base and cap, so the device arena
        # has to be laid out again before the next upload.
        self.belt_device.invalidate_layout()
        return n

    def reset_sim(self):
        """Restart the simulation from tick zero, keeping the factory.

        ctrl+R. Everything a belt-network edit does -- every item in flight
        discarded, belts and gate channels alike -- plus the one thing an
        edit deliberately does *not* do: the tick counter goes back to 0.

        The counter has to be part of this. An item's position is a closed
        form function of `clock.tick` and its own entry tick, so a flush that
        left the counter where it was would restart the factory at tick
        four million -- arithmetically fine, but it means "start over" leaves
        the clock somewhere the player cannot see and the readouts disagree
        with the state. A board replacement already resets the clock for the
        same reason (see load_board); this is that half without the board
        half.

        Seeds go back on afterwards, so a seeded run starts loaded exactly as
        it does after a load or a clear -- and after the reset rather than
        before, since the flush would otherwise wipe what was just placed.

        **It forces a full re-derivation**, not just a flush. "Start over"
        has to mean the connectivity too: segment ids, the port resolutions,
        the constant fold, the direct edge list and the device's packed copy
        of all of them are derived state, and a flush leaves every one of
        them as it was. Re-deriving makes ctrl+R the player's recovery from
        anything stale, which is what they reach for it expecting.
        """
        self.invalidate_segments()
        self.flush_material()
        self.clock.reset()
        # Re-seed against the new timeline: seed_segments() dates its pushes
        # from clock.tick, so it must run after the reset or every seeded
        # item would carry an entry tick from the old one.
        return self.seed_segments()

    def seed_segments(self):
        """Put one item on each run holding a seeded belt cell. Returns the count.

        A seeded belt is a standing instruction: whenever this cell's run is
        (re)derived -- a board clear, a load, or any edit that triggers the
        segment sweep -- it starts with an item of the player's chosen value
        on it. That makes a run testable without wiring a Source to it.

        **Items enter at the tail, as ordinary pushes.** The closed form can
        only accept an item at a run's entrance (`pos` is measured from the
        tail and `enter_tick` is stamped once), so there is no way to
        materialise one at the seeded cell's own offset -- it rides from the
        start like anything else.

        **Several seeds on one run arrive as a clump, which needs staggered
        entry ticks.** Every seed enters on the same tick, so pushing them
        all at `clock.tick` puts the first at position 0 and then has
        `has_room()` refuse every one after it -- four seeded cells produced
        a single item. Dating each successive seed one `d/v` further into the
        past puts it exactly one spacing ahead instead, which is the state a
        producer feeding the run at full rate would have reached. The `min`
        in the closed form then treats them as the compressed clump they are,
        and `push()`'s room check still refuses anything that genuinely will
        not fit -- so a run shorter than its seed count takes as many as fit
        and drops the rest, as asked.

        Seeds are deliberately pushed directly rather than offered through
        the producer callback: a seed is not a producer, and routing it
        through `offer()` would have it compete with the gate actually
        feeding that run.
        """
        seeded = 0
        for seg in self.segments.segments.values():
            values = [v.seed_value for v in self._seeded_cells(seg)]
            if not values:
                continue
            # Ticks per item of separation, so a clump of seeds is spaced by
            # the same `d` the transport enforces.
            per = max(1, self.belts.spacing // self.belts.speed)
            # Oldest first: an item's position grows with its age, and the
            # ring requires positions to *decrease* in k, so the first push
            # has to be the most backdated one. Enumerating the seeds up
            # front is what makes that expressible -- the stagger is counted
            # down from the total rather than up from zero.
            n = len(values)
            for i, value in enumerate(values):
                when = self.clock.tick - (n - 1 - i) * per
                if self.belts.push(seg, value, when):
                    seeded += 1
        return seeded

    def _seeded_cells(self, seg):
        """Live, real, seeded belt views along a segment, tail first."""
        for (x, y) in seg.cells:
            view = self.board.entity_at(x, y)
            if view is None or view.type_id != cell.TYPE_BELT:
                continue
            if view.is_ghost or not view.seed_on:
                continue
            yield view

    def clear_items(self):
        """Take every item off every belt, leaving the segmentation alone.

        Resets each ring to empty and the anchor to the far end, which is the
        initial state from docs/belt_report.md. Distinct from a rebuild: the
        runs survive, only what is on them is discarded.
        """
        # Writing ring state from the host, so the device must not be left
        # holding a newer copy to upload over the top of it.
        self.settle()
        for seg in self.segments.segments.values():
            seg.head = seg.tail = 0
            seg.anchor = self.belts.length_of(seg)
        # Ring state changed from outside the tick. These runs are now empty
        # with a saturated anchor, which *is* the sleep state -- but they may
        # have been asleep holding items, and a producer upstream is probably
        # still loaded, so let the next tick decide rather than assuming.
        self.belts.wake_all()
        # Gates too: a gate asleep behind a full run was sleeping on the claim
        # that the run could not change, and the run has just emptied from
        # outside the tick.
        self.gates.wake_all_gates()

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

        **Settles first**, like every other host-side write to simulation
        state. `clear_items()` does too, but this also zeroes gate channels
        through entity rows, and a device left holding newer ones would
        upload them back over the top on the next tick -- so a ctrl+R would
        appear to clear the board and then refill it from nowhere.
        """
        self.settle()
        self.clear_items()
        for gate in self.gates.gates():
            for c in range(gate.input_count()):
                gate.clear_input(c)
            for c in range(gate.output_count()):
                gate.clear_output(c)
        # A folded constant is not material in flight -- it is a property of
        # the board, like the Source's emit value it comes from -- so a flush
        # must put it back. Nothing else would: a folded channel is held by
        # the fold alone, with no belt or neighbour to re-deliver it, so
        # clearing it here and leaving it clear would starve every gate fed by
        # a constant until the next structure phase refilled it. One tick on a
        # running board, and for ever on a paused one, which is exactly when a
        # player changes a Source's value and watches what happens.
        self.gates.reassert_folded()
        # Every gate channel on the board just changed from the host, which
        # the device's mailbox never saw -- so the GPU sleep layer has to be
        # told. Without it a flush can leave a gate holding a freshly-asserted
        # folded constant while asleep, and nothing would ever re-admit it.
        self.note_host_write()

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
        prof = self.clock.profiler

        if self.debug_feed is None:
            # `transport` is the belt tick with the gate callbacks inside it,
            # so it includes the port work by construction -- see tick_belts()
            # on why that cannot be split out into passes around it. The
            # callbacks carry their own probes for that reason: subdividing
            # this number is the only way to see inside it.
            with prof.probe("belt/transport"):
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

            with prof.probe("belt/transport"):
                self.belts.tick(tick, offer=offer, accept=accept,
                                took=gates.took_from)

        with prof.probe("belt/direct"):
            self.gates.deliver_direct()

    def tick(self, dt):
        """Advance the simulation clock, running whatever ticks are due.

        Called once per frame by the orchestrator, after all player mutation
        and before the device sync -- so a tick's effects reach the GPU in the
        same frame they happen.

        **The clock still owns time.** When the compute path is live the
        phases are dispatches rather than Python calls, but `Clock.advance()`
        decides how many ticks are due, caps the backlog and keeps `alpha`;
        none of that moves to the device. The phase callbacks are swapped out
        for the duration rather than the clock being bypassed, so the tick
        count, the profiler's tick counting and `request_step()` all behave
        identically either way -- which is what lets the two timelines be
        compared by flipping one switch.
        """
        if not self.use_gpu_tick():
            # The host may be ahead-of or behind the device; either way the
            # CPU phases read entity rows and rings, so level them first.
            self.settle()
            return self.clock.advance(dt)
        return self._advance_on_device(dt)

    def _advance_on_device(self, dt):
        """`Clock.advance()` with the phase bodies replaced by dispatches.

        The clock's own sequencing is untouched -- see `tick()`. What changes
        is what a phase *does*, which is exactly the substitution core/tick.py
        predicted: "the GPU port replaces the bodies of those callbacks with
        compute dispatches while the sequencing stays as it is".
        """
        clock = self.clock
        pending = []

        prof = clock.profiler

        def structure():
            # Collect rather than dispatch, so one `advance()` spanning
            # several ticks issues them as one batch below. Dispatching here
            # would still be correct and would cost a bind per phase.
            #
            # The probes are named to match the CPU path's sub-phases, so a
            # profile dump describes the same tree either way and the two can
            # be compared line for line. `dispatch` is where the time goes
            # instead of `prefilter` + `fire`, which is the honest shape:
            # there is no prefilter, by design.
            with prof.probe("structure/dispatch"):
                pending.append(clock.tick)

        def belt():
            with prof.probe("belt/dispatch"):
                pass

        saved = clock.swap_phases(structure, belt)
        try:
            ran = clock.advance(dt)
        finally:
            clock.restore_phases(saved)
        if pending:
            # `pending` holds the tick number each dispatch computes, taken
            # *inside* the phase -- the clock has already moved on by the
            # time we get here, so reading `clock.tick` now would date every
            # item an `advance()`-worth too late. Item position is a closed
            # form over the tick counter, so that is a real error and not an
            # off-by-one in bookkeeping.
            self._gpu_tick(len(pending), start=pending[0])
        return ran

    # --- the GPU tick ----------------------------------------------------
    #
    # The whole tick -- fire, direct hand-off, belt transport -- runs as three
    # compute dispatches when a device is available. See "The tick on the GPU"
    # in CLAUDE.md for the measurements and the traps; this is the policy
    # layer, which is the part the kernels cannot decide for themselves.
    #
    # **Residency is the point, so the readback is the thing to get right.**
    # A readback stalls on every dispatch that touched the buffers, so doing
    # it per tick would throw away the entire gain -- the dispatches are ~10us
    # and a round trip is far more. But host state cannot simply be allowed to
    # rot either: the Status window reads entity rows, the segment inspector
    # reads rings, a save serialises both, and an edit re-derives from them.
    #
    # The rule is therefore: **the GPU owns the state between a sync and the
    # next thing that needs the host's copy.** `_gpu_dirty` says the device is
    # ahead, and `settle()` is the single place that brings the host level.
    # Everything that reads host simulation state calls it; nothing else has
    # to know the GPU exists.
    #
    # What does *not* need it is the renderer, which is the whole reason the
    # belt kernel borrows `BeltDevice`'s buffers: the pass that advances a run
    # and the shader that draws it share one copy, so a frame is correct with
    # no round trip at all.
    def live_items(self):
        """Every item currently on a belt, with the host's copy made current.

        `BeltSim.total_items()` reads the segment objects directly, which is
        right for the simulation and wrong for a caller that just wants to
        know what is on the board: with the compute path running, those
        objects are whatever the last readback left behind.

        So this is the *asking* form, and it settles. Anything reporting,
        asserting or displaying an item count wants this; the tick itself
        does not and must not, since settling per tick is the cost the whole
        design avoids.
        """
        self.settle()
        return self.belts.total_items()

    def gpu_ready(self):
        """Is the compute path built and usable?"""
        return (self.gates.device is not None
                and self.gates.belt_compute is not None)

    def use_gpu_tick(self):
        """Should the tick run as dispatches this frame?

        Separate from `gpu_ready()` because being *able* to is not the same
        as being *allowed* to. Two things veto it, and both are cases where
        the CPU path is the only correct one rather than merely the slower
        one:

        - **The debug feed.** It stands in for gates on dangling runs and
          lives in a Python closure; there is no kernel for it. It is a
          diagnostic, so falling back is right.
        - **Host-decided machines.** Merge's readiness depends on the values
          held and it consumes only the selected lane, which the opcode set
          deliberately does not express (see `FLAG_HOST_DECIDES`). A board
          with one needs the CPU phase, or that machine silently never fires.
        """
        if not self.gpu_ready() or not self._gpu_enabled:
            return False
        if self.debug_feed is not None:
            return False
        # **Someone else has registered a phase callback.** The dispatch path
        # swaps the phase lists wholesale, so anything else on them would
        # simply not run -- and a phase callback is how a test or a tool
        # observes the tick (see "A scenario that registers a phase callback"
        # in CLAUDE.md). Silently dropping it would make the observer wrong
        # about a simulation that was otherwise fine, which is worse than
        # running on the CPU.
        if not self.clock.phases_are_only(self._structure_phase,
                                          self._belt_phase):
            return False
        # **The device's packed geometry must describe the current slots.**
        # `GateState.build()` reassigns every slot, and anything that touches
        # `_gate_table()` can trigger it between an edit and the frame's
        # `sync_device()`. Dispatching in that window ran gates against
        # another machine's type and channel counts -- the "connections made
        # improperly" symptom that a force rebuild cleared. One tick on the
        # CPU is the right price for closing it.
        if self.gates._device_stale:
            return False
        return not self.gates.has_host_decided_gates()

    def _gpu_tick(self, ticks=1, start=None):
        """Run `ticks` whole ticks on the device. The host is left stale.

        Uploads only on the first tick after the host last touched the state,
        so a run of a thousand ticks is one upload, a thousand dispatch pairs
        and no readback -- which is the shape the whole port exists for.

        `start` is the tick number the first dispatch computes.
        """
        gates = self.gates
        if not self._gpu_dirty:
            # The host is authoritative, so push it up before the GPU takes
            # over. After this the direction of authority is reversed until
            # `settle()`.
            # **The sleep sets are deliberately NOT touched here.**
            #
            # This is the handover from host authority to device authority,
            # and the tempting thing is to `wake_all()` on the grounds that
            # the host may have changed something. It is wrong twice over:
            #
            # - It is not needed. The only host changes that can invalidate a
            #   sleep decision are *edits*, and every edit goes through
            #   `invalidate_segments()` -> `sync_segments()` -> `sync_device()`,
            #   which re-packs the sleep sets and wakes everything itself. A
            #   `settle()` for a readback changes nothing about the board.
            # - It is actively destructive. `settle()` clears `_gpu_dirty`, so
            #   anything that reads host state -- an open panel, a profiler,
            #   the Status window -- sends the next tick through this branch.
            #   Waking here meant the sleep set was wiped on most frames, so
            #   on a board with a window open **nothing ever slept**, and a
            #   differential check could not fail: two genuinely unsafe
            #   mutations passed on all four real boards because the sleeping
            #   timeline was restarted from "all awake" every tick.
            #
            # The awake sets and the mailbox live in VRAM and persist across a
            # readback, which is exactly the residency this layer sits inside.
            #
            # **But a host *write* is a different matter, and needs a wake.**
            # This branch is reached both after a plain readback (nothing
            # changed, so the sets are still right) and after something on the
            # host has altered gate channels -- the Status window setting a
            # Source's emit value, a test feeding a starved gate, a tool
            # poking a row. Such a write is a producer event that posted no
            # mailbox entry, because the mailbox only exists on the device, so
            # the entity it fed stays asleep for ever: measured as a Shear
            # with both inputs held that never fired again.
            #
            # `_gpu_host_wrote` is set by the paths that mutate simulation
            # state rather than merely read it, so an ordinary settle-for-a-
            # readback still costs nothing.
            gates.state.sync_from_rows()
            gates.device.upload()
            gates.belt_compute.upload()
            sleep = gates.device.sleep
            if sleep is not None and self._gpu_host_wrote:
                sleep.wake_all()
            self._gpu_host_wrote = False
            self._gpu_dirty = True
        if start is None:
            start = self.clock.tick
        sleep = gates.device.sleep
        for i in range(ticks):
            # **The phases run first, then the compaction builds the set for
            # the next tick.** Order is the whole correctness argument, and
            # the other way round does not work: a tick's wakes are posted
            # *by* its phases, so compacting before them consumes a mailbox
            # nothing has written yet. That is not a subtle degradation --
            # the first compaction found an empty mailbox and slept the entire
            # board on tick 0, and nothing ever woke it again because waking
            # requires an awake neighbour.
            #
            # So the invariant is: entering a tick, the awake sets are already
            # correct for it (from `wake_all()` on the first tick, or from the
            # previous tick's compaction), and the mailbox is being filled for
            # the next one. Both phases post with the same epoch because they
            # contribute to one compaction, not to two.
            # The epoch the phases stamp their wakes with, and the one the
            # compaction below accepts -- the *same* number, since a wake
            # posted this tick is for the set being built right after it.
            # `compact()` does not advance it; `wake_all()` and this line are
            # the only places it moves.
            if sleep is not None:
                sleep.epoch += 1
            gates.device.run(1)
            gates.belt_compute.run(start + i, 1)
            if sleep is not None:
                # The publish pass reads the entity table, the segment records
                # and the gate row map, none of which `SleepSets` owns -- so
                # the binds are handed in rather than reached for.
                sleep.compact(sleep.epoch, extra_binds=(
                    lambda: gates.belt_compute.device.bind(
                        compute.BELT_SEG_I_BINDING,
                        compute.BELT_SEG_F_BINDING,
                        compute.BELT_ITEM_BINDING),
                    lambda: gates.device.bind_rows(),
                    self.board.bind_entities,
                ))

    def note_host_write(self):
        """Declare that the host has changed simulation state.

        Call after writing a gate channel, a Source's emit value, a seed --
        anything the *device* would have posted a mailbox wake for had it
        done the write itself. The GPU sleep layer wakes everything on the
        next dispatch, which is the coarse but correct answer: a host write is
        rare, human-paced, and can touch anything.

        Not folded into `settle()`, because settling is overwhelmingly a
        *read* -- an open panel settles every frame -- and waking on a read
        would mean nothing ever slept while a window was open. That is the
        same mistake, in a different place, as the `wake_all()` this branch
        used to do unconditionally.

        An edit needs no call: `invalidate_segments()` leads to
        `sync_device()`, which re-packs the sleep sets and wakes them itself.
        """
        self._gpu_host_wrote = True

    def settle(self):
        """Bring the host's copy level with the device. Idempotent.

        The single readback point. Called by anything that reads simulation
        state off the host -- an edit, a save, the inspector, the Status
        window -- so no caller has to know whether a dispatch ran.

        Cheap when nothing is outstanding, which is what makes it safe to
        call liberally: the flag is the whole guard.
        """
        if not self._gpu_dirty:
            return False
        gates = self.gates
        gates.belt_compute.readback()
        gates.device.readback()
        gates.state.flush()
        self._gpu_dirty = False
        self._gpu_settled_this_frame = True
        return True

    # --- device ---------------------------------------------------------
    def create_device_resources(self, ctx):
        self.board.create_device_resources(ctx)
        self.belt_device.create_device_resources(ctx)
        # The compute path is additive: if the kernels fail to build, the
        # simulation keeps running on the CPU exactly as before. A broken
        # shader should cost the speed-up, not the game.
        try:
            self.gates.create_device_resources(ctx, self.belt_device)
        except Exception as exc:            # pragma: no cover - driver-specific
            self.gpu_error = str(exc)
            self.gates.device = None
            self.gates.belt_compute = None

    def sync_to_device(self):
        """Upload whatever is dirty. Called once per frame by the orchestrator.

        **The belt upload is skipped while the device is ahead**, and that is
        the single most important line in the integration. `BeltDevice.sync()`
        re-packs the SSBOs from the *host's* segment objects, so running it
        after a dispatch would overwrite the GPU's results with the state the
        host last saw -- rewinding the simulation by however many ticks were
        outstanding, every frame, silently. The symptom is a factory that
        looks alive but never progresses.

        Skipping it is not a compromise: the belt kernel writes the very
        buffers the renderer reads (see core/belt_compute.py), so they are
        already current. There is nothing to upload because the data never
        left the device.
        """
        # Belt state goes up unconditionally *unless the device already has
        # it*. Two ways it can: dispatches are outstanding (`_gpu_dirty`), or
        # they were read back earlier this very frame
        # (`_gpu_settled_this_frame`) and the buffers are still the device's
        # own current copy. Either way re-packing from the host is at best a
        # no-op and at worst a rewind of however many ticks were in flight.
        if not (self._gpu_dirty or self._gpu_settled_this_frame):
            self.belt_device.sync()
        self._gpu_settled_this_frame = False
        return self.board.to_device()
