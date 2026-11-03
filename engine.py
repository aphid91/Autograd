"""Engine: owns the gameboard and exposes the API for mutating it.

Game logic goes here. Nothing else writes to the board -- the input manager
produces intents, the UI reads state, and this is the only layer that calls
Gameboard.spawn/despawn.

All placement goes through a BuildPlan (see builders.py), whatever the tool.
The engine does not know what a belt run is; it knows how to preview a plan,
commit a plan, and erase.
"""

import cell
from builders import BeltBuilder
from engine_paths import bresenham, intent_dir, path_directions, plan_path
from entity import Belt
from gameboard import Gameboard

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
        self.ghost_rows = []
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
        self.board.clear_frustrated()

    def preview_plan(self, plan):
        """Replace the current preview with `plan`.

        Ghosts the placements that can happen and tints the cells that cannot.
        Under an all-or-nothing plan nothing is ghosted when any cell is
        blocked, but the blocked cells are still tinted -- you see exactly what
        is in the way.
        """
        self.clear_ghosts()

        for (x, y) in plan.frustrated_cells:
            self.board.set_frustrated(x, y)

        for p in plan.buildable:
            view = self.board.spawn(
                p.entity_cls, p.x, p.y, p.direction, is_ghost=True
            )
            self.ghost_rows.append(view.row)

        return plan

    def commit_ghosts(self):
        """Turn the ghost preview into real entities. Returns the count."""
        n = len(self.ghost_rows)
        for row in self.ghost_rows:
            self.board.set_ghost(row, False)
        self.ghost_rows.clear()
        self.board.clear_frustrated()
        self.stats["placed"] += n
        return n

    def build_plan(self, plan):
        """Commit a plan directly, with no preview step."""
        rows = []
        for p in plan.buildable:
            view = self.board.spawn(p.entity_cls, p.x, p.y, p.direction)
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

    def place_structure(self, entity_cls, x, y, direction=cell.DIR_PX):
        """Place one structure if its whole footprint is free."""
        blocked = [c for c in entity_cls.cells_for(x, y, direction)
                   if not self.buildable(*c)]
        if blocked:
            return None
        view = self.board.spawn(entity_cls, x, y, direction)
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
        if row in self.ghost_rows:
            return False
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
