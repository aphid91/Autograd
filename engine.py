"""Engine: owns the gameboard and exposes the API for mutating it.

Game logic goes here. Nothing else writes to the board -- the input manager
produces intents, the UI reads state, and this is the only layer that calls
Gameboard.spawn/despawn.
"""

import cell
from entity import Belt
from gameboard import Gameboard

# Path modes, cycled by R while planning.
MODE_HORIZONTAL_FIRST = 0
MODE_VERTICAL_FIRST = 1
MODE_LINE = 2
MODE_COUNT = 3

MODE_NAMES = {
    MODE_HORIZONTAL_FIRST: "Elbow: H then V",
    MODE_VERTICAL_FIRST: "Elbow: V then H",
    MODE_LINE: "Line (staircase)",
}


def _span(a, b):
    """Inclusive integer range from a to b, in the right direction."""
    step = 1 if b >= a else -1
    return range(a, b + step, step)


def plan_path(start, end, mode):
    """Cells from start to end under a path mode, inclusive of both ends.

    Returns a list of (x, y) with no duplicates, in travel order. Purely
    geometric -- it knows nothing about what occupies the board.
    """
    sx, sy = start
    ex, ey = end

    if mode == MODE_LINE:
        return bresenham(sx, sy, ex, ey)

    cells = []
    if mode == MODE_HORIZONTAL_FIRST:
        for x in _span(sx, ex):
            cells.append((x, sy))
        for y in _span(sy, ey):
            cells.append((ex, y))
    else:  # MODE_VERTICAL_FIRST
        for y in _span(sy, ey):
            cells.append((sx, y))
        for x in _span(sx, ex):
            cells.append((x, ey))

    # The corner cell is produced by both legs; drop the repeat while keeping
    # order. Only adjacent duplicates are possible here.
    out = [cells[0]]
    for c in cells[1:]:
        if c != out[-1]:
            out.append(c)
    return out


def bresenham(x0, y0, x1, y1):
    """Integer line. Produces a 4-connected staircase (no diagonal steps).

    Also used by the input manager to fill gaps in a fast erase drag.
    """
    cells = []
    dx = abs(x1 - x0)
    dy = abs(y1 - y0)
    sx = 1 if x1 >= x0 else -1
    sy = 1 if y1 >= y0 else -1
    x, y = x0, y0
    cells.append((x, y))
    err = dx - dy
    while (x, y) != (x1, y1):
        # Step one axis at a time so the path stays 4-connected -- belts
        # cannot move diagonally, so a diagonal step would break the chain.
        e2 = 2 * err
        if e2 > -dy and (x != x1):
            err -= dy
            x += sx
        elif y != y1:
            err += dx
            y += sy
        else:
            x += sx
        cells.append((x, y))
    return cells


def _intent_dir(start, end):
    """The direction a drag was aimed in, for paths with no step to read.

    Uses the dominant axis of the whole drag. A zero-length drag (a plain
    click) has no aim at all and gets +X.
    """
    dx = end[0] - start[0]
    dy = end[1] - start[1]
    if dx == 0 and dy == 0:
        return cell.DIR_PX
    if abs(dx) >= abs(dy):
        return cell.DIR_PX if dx > 0 else cell.DIR_NX
    return cell.DIR_PY if dy > 0 else cell.DIR_NY


def path_directions(path, fallback=cell.DIR_PX):
    """Direction for each cell, derived from the step to the next cell.

    The final cell has no successor, so it inherits its predecessor's
    direction. A single-cell path has no step at all and gets `fallback`.

    Call this on the *unfiltered* path -- see Engine.plan_oriented().
    """
    n = len(path)
    if n == 0:
        return []
    if n == 1:
        return [fallback]

    dirs = []
    for i in range(n - 1):
        (x0, y0), (x1, y1) = path[i], path[i + 1]
        try:
            dirs.append(cell.step_to_dir(x1 - x0, y1 - y0))
        except ValueError:
            # Non-adjacent cells should be impossible from plan_path; fall
            # back rather than crash the frame.
            dirs.append(dirs[-1] if dirs else cell.DIR_PX)
    dirs.append(dirs[-1])
    return dirs


class Engine:
    def __init__(self, width=512, height=512):
        self.board = Gameboard(width, height)
        self.ghost_rows = []
        self.stats = {"placed": 0, "erased": 0}

    # --- queries --------------------------------------------------------
    def buildable(self, x, y):
        """Can a new entity go here? Occupied and out-of-bounds cells cannot."""
        return self.board.is_empty(x, y)

    def filter_buildable(self, path):
        """Drop cells that are occupied or out of bounds.

        Blocked cells are skipped rather than truncating the path, so dragging
        across an existing belt builds on both sides of it.
        """
        return [(x, y) for (x, y) in path if self.buildable(x, y)]

    def plan_oriented(self, start, end, mode):
        """(cells, directions) for a belt run, blocked cells removed.

        Directions are derived from the *unfiltered* geometric path and only
        then filtered alongside it. Deriving them after filtering would make a
        belt's facing depend on what happens to be in the way -- a cell whose
        successor was blocked would lose its direction and fall back to +X.
        What the player drew is what they get, regardless of interference.
        """
        full = plan_path(start, end, mode)
        # For a single-cell path there is no step to read a direction from, so
        # fall back to the drag's overall intent: a click-without-drag onto one
        # free cell next to a blocker should still face the way it was aimed.
        dirs = path_directions(full, fallback=_intent_dir(start, end))
        keep = [
            (c, d) for c, d in zip(full, dirs) if self.buildable(*c)
        ]
        if not keep:
            return [], []
        cells, directions = zip(*keep)
        return list(cells), list(directions)

    # --- ghosts ---------------------------------------------------------
    def clear_ghosts(self):
        """Remove every ghost entity, freeing its rows and cells."""
        for row in self.ghost_rows:
            self.board.despawn(row)
        self.ghost_rows.clear()

    def preview_belt_path(self, start, end, mode):
        """Replace the current ghost preview with a belt path.

        Returns the list of cells actually ghosted (blocked cells omitted).
        """
        self.clear_ghosts()
        path, dirs = self.plan_oriented(start, end, mode)
        for (x, y), d in zip(path, dirs):
            belt = self.board.spawn(Belt, x, y, d, is_ghost=True)
            self.ghost_rows.append(belt.row)
        return path

    def commit_ghosts(self):
        """Turn the ghost preview into real entities. Returns the count."""
        n = len(self.ghost_rows)
        for row in self.ghost_rows:
            self.board.set_ghost(row, False)
        self.ghost_rows.clear()
        self.stats["placed"] += n
        return n

    # --- direct mutation ------------------------------------------------
    def place_belt(self, x, y, direction=cell.DIR_PX):
        if not self.buildable(x, y):
            return None
        belt = self.board.spawn(Belt, x, y, direction)
        self.stats["placed"] += 1
        return belt

    def place_belt_path(self, start, end, mode):
        """Build a belt run directly, skipping blocked cells."""
        path, dirs = self.plan_oriented(start, end, mode)
        for (x, y), d in zip(path, dirs):
            self.board.spawn(Belt, x, y, d)
        self.stats["placed"] += len(path)
        return path

    def erase(self, x, y):
        """Remove whatever occupies a cell. Ghosts are left alone."""
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
