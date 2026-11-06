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
from core.gameboard import Gameboard
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
                extra = {}
                if p.entity_cls is Underground:
                    extra["is_exit"] = p.is_exit
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

    def commit_ghosts(self):
        """Turn the ghost preview into real entities. Returns the count.

        Preview-only rows are discarded rather than promoted: they are
        fragments of a blocked placement, and half a structure is not a thing
        that can exist.
        """
        n = len(self.ghost_rows)
        promoted = list(self.ghost_rows)
        for row in promoted:
            self.board.set_ghost(row, False)
        self.ghost_rows.clear()
        # Pair undergrounds only once they are real. find_under_partner()
        # skips ghosts, so a pair cannot form while either end is still a
        # preview -- which is what keeps a hovering exit from latching onto
        # an entrance it has not actually been placed near yet.
        for row in promoted:
            if not self.board.is_row_alive(row):
                continue
            view = self.board.view(row)
            if view.type_id != cell.TYPE_UNDER:
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

    def build_plan(self, plan):
        """Commit a plan directly, with no preview step."""
        rows = []
        for p in plan.buildable:
            view = self.board.spawn(p.entity_cls, p.x, p.y, p.direction,
                                    flipped=p.flipped)
            rows.append(view.row)
        self.stats["placed"] += len(rows)
        return rows

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

    # --- device ---------------------------------------------------------
    def create_device_resources(self, ctx):
        self.board.create_device_resources(ctx)

    def sync_to_device(self):
        """Upload whatever is dirty. Called once per frame by the orchestrator."""
        return self.board.to_device()
