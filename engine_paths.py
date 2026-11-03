"""Pure path geometry for belt runs.

Separated from engine.py so builders.py can use it without a circular import
(engine owns builders, builders need paths). Nothing here touches the board --
these functions know only about integer cell coordinates.
"""

import cell


def _span(a, b):
    """Inclusive integer range from a to b, in the right direction."""
    step = 1 if b >= a else -1
    return range(a, b + step, step)


def plan_path(start, end, mode):
    """Cells from start to end under a path mode, inclusive of both ends.

    Returns a list of (x, y) with no duplicates, in travel order. Purely
    geometric -- it knows nothing about what occupies the board.
    """
    # Imported lazily to keep this module free of builder dependencies.
    from builders import BeltBuilder

    sx, sy = start
    ex, ey = end

    if mode == BeltBuilder.MODE_LINE:
        return bresenham(sx, sy, ex, ey)

    cells = []
    if mode == BeltBuilder.MODE_HORIZONTAL_FIRST:
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


def intent_dir(start, end):
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

    Call this on the *unfiltered* path: deriving directions after dropping
    blocked cells would make a belt's facing depend on what is in the way.
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
