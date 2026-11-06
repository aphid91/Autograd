"""Entity builders: the single mechanism behind every placement tool.

A builder turns a gesture (a drag, a click, eventually a pasted blueprint) into
a `BuildPlan`: the set of entities it *wants* to create, split into placements
that can happen and cells that are blocked. The engine then ghosts the plan,
and on confirm commits it. Belt-drawing and structure-placement are not two
systems -- they are two builders differing in how they generate placements and
in one policy flag.

    BeltBuilder       many 1x1 placements along a path, per-placement failure
    StructureBuilder  one multi-cell placement, all-or-nothing

Future tools fit the same shape: a blueprint paste is a builder emitting many
multi-cell placements; undo can record the rows a committed plan produced.

Frustration is a property of a *placement*, not of the board: a cell is
frustrated because some placement wanted it and could not have it. Placements
that fail still appear in the plan so the renderer can show you what blocked
them -- that is what makes a frustrated cell different from an absent one.
"""

from core import cell
from core.engine_paths import plan_path, path_directions, intent_dir
from core.entity import STRUCTURES, Belt, Underground


def quadrant_dir(frac):
    """Which way a sub-cell cursor position points, as a direction constant.

    The cell is split by its diagonals into four triangles; the one the point
    falls in names the direction. Used to aim a belt placed with a single
    click, where there is no drag to take a heading from -- clicking the right
    part of a square gives an east-facing belt, the top part north, and so on.
    Dead centre is +X, matching the old fixed default.
    """
    ox, oy = frac[0] - 0.5, frac[1] - 0.5
    if ox == 0.0 and oy == 0.0:
        return cell.DIR_PX
    if abs(ox) >= abs(oy):
        return cell.DIR_PX if ox > 0 else cell.DIR_NX
    return cell.DIR_PY if oy > 0 else cell.DIR_NY


class Placement:
    """One entity a plan would create, and whether it can actually happen."""

    __slots__ = ("entity_cls", "x", "y", "direction", "blocked_cells",
                 "flipped", "is_exit", "under_partner")

    def __init__(self, entity_cls, x, y, direction, blocked_cells=(),
                 flipped=False):
        self.entity_cls = entity_cls
        self.x = x
        self.y = y
        self.direction = direction
        self.blocked_cells = list(blocked_cells)
        self.flipped = flipped
        # Underground-only. `is_exit` picks which end this placement lays;
        # `under_partner` is the cell of the end it would pair with, or None,
        # and exists so the preview can draw the dotted span before anything
        # is committed.
        self.is_exit = False
        self.under_partner = None

    @property
    def frustrated(self):
        return bool(self.blocked_cells)

    def cells(self):
        return self.entity_cls.cells_for(self.x, self.y, self.direction,
                                         self.flipped)

    def __repr__(self):
        state = "frustrated" if self.frustrated else "ok"
        mirror = " flipped" if self.flipped else ""
        return (f"<Placement {self.entity_cls.__name__} ({self.x},{self.y}) "
                f"{cell.DIR_NAMES[self.direction]}{mirror} {state}>")


class BuildPlan:
    """What a builder wants to do, before anything is committed."""

    __slots__ = ("placements", "all_or_nothing")

    def __init__(self, placements, all_or_nothing=False):
        self.placements = placements
        self.all_or_nothing = all_or_nothing

    @property
    def frustrated_cells(self):
        """Every cell to tint red, across all placements."""
        out = []
        for p in self.placements:
            out.extend(p.blocked_cells)
        return out

    @property
    def buildable(self):
        """Placements that will actually be created on commit.

        Under all_or_nothing a single frustrated placement voids the whole
        plan, which is what makes a partially-overlapping structure refuse to
        place while a belt run happily builds around an obstacle.
        """
        if self.all_or_nothing and any(p.frustrated for p in self.placements):
            return []
        return [p for p in self.placements if not p.frustrated]

    @property
    def previewable(self):
        """Placements to *show* as ghosts, which is not the same set.

        A frustrated multi-cell placement builds nothing, but the player still
        wants to see the part that would fit -- the shape tells you how far to
        move to clear the obstruction. So a blocked placement is still
        previewed; only its blocked cells are tinted instead of ghosted, and
        `preview_cells()` reports which is which.
        """
        return list(self.placements)

    @property
    def ok(self):
        """True if the plan would create everything it wanted."""
        return bool(self.placements) and not any(
            p.frustrated for p in self.placements
        )

    def __repr__(self):
        return (f"<BuildPlan {len(self.placements)} placements, "
                f"{len(self.buildable)} buildable, "
                f"{len(self.frustrated_cells)} frustrated>")


class EntityBuilder:
    """Base class for placement tools.

    A builder holds the tool's own transient state -- a belt's path mode, a
    structure's rotation -- and interprets the per-tool keys. `R` and `shift+R`
    mean whatever the active tool decides they mean.
    """

    name = "builder"
    #: True if this tool needs a drag (press, move, release-click) rather than
    #: acting on a single click.
    drag_based = True

    def plan(self, engine, start, end, start_frac=(0.5, 0.5),
             end_frac=(0.5, 0.5)):
        """Build a BuildPlan for a gesture from `start` to `end` cells.

        `start_frac` / `end_frac` give the cursor's position *within* those
        cells, 0..1 on each axis with y up. Tools that care where in a square
        the player pointed -- a belt aiming its first and last segment -- use
        them; others ignore them.
        """
        raise NotImplementedError

    def cycle(self, backward=False):
        """Handle the tool's R / shift+R key. Return True if state changed."""
        return False

    def flip(self):
        """Handle the tool's F key. Return True if state changed.

        Like R, F is per-tool: the input manager reports only that it was
        pressed. A tool with no mirrored form (a belt, an achiral structure)
        returns False and nothing happens.
        """
        return False

    def status(self):
        """One short line for the UI describing current tool state."""
        return ""


class BeltBuilder(EntityBuilder):
    """Draw a run of 1x1 belts along a planned path.

    Blocked cells are frustrated individually: the run still builds on both
    sides of an obstacle, and only the overlapping cells are tinted. R cycles
    the path shape.
    """

    name = "Belt"
    drag_based = True

    MODE_HORIZONTAL_FIRST = 0
    MODE_VERTICAL_FIRST = 1
    MODE_LINE = 2
    MODE_COUNT = 3

    MODE_NAMES = {
        MODE_HORIZONTAL_FIRST: "Elbow: H then V",
        MODE_VERTICAL_FIRST: "Elbow: V then H",
        MODE_LINE: "Line (staircase)",
    }

    def __init__(self):
        self.mode = self.MODE_HORIZONTAL_FIRST

    def cycle(self, backward=False):
        step = -1 if backward else 1
        self.mode = (self.mode + step) % self.MODE_COUNT
        return True

    def status(self):
        return self.MODE_NAMES[self.mode]

    #: How far across a cell the pointer must be, measured from the centre
    #: toward an edge, before the final belt turns to exit that way. 0.6 means
    #: the outer 40% of the cell on that side.
    TURN_THRESHOLD = 0.6

    def plan(self, engine, start, end, start_frac=(0.5, 0.5),
             end_frac=(0.5, 0.5)):
        # Directions come from the unfiltered path so a belt's facing never
        # depends on what is in the way -- see the note in CLAUDE.md.
        path = plan_path(start, end, self.mode)
        dirs = path_directions(path, fallback=quadrant_dir(start_frac))

        if dirs:
            # The last belt exits the run, and the pointer's position in that
            # final cell says which way. Straight on by default; past the
            # threshold toward a side edge, it turns and forms a corner.
            turn = self._exit_turn(dirs[-1], end_frac)
            if turn is not None:
                dirs[-1] = turn

        placements = []
        for (x, y), d in zip(path, dirs):
            blocked = [] if engine.buildable(x, y) else [(x, y)]
            placements.append(Placement(Belt, x, y, d, blocked))
        return BuildPlan(placements, all_or_nothing=False)

    def _exit_turn(self, travel_dir, frac):
        """A turned exit direction for the final belt, or None to go straight.

        Only sideways deflection counts: pushing the pointer *along* the
        direction of travel means "keep going", which is already the default,
        and pulling it backwards would reverse the belt into the run it just
        came from.
        """
        fx, fy = frac
        # Offsets from the cell centre, -0.5..0.5, positive toward +X / +Y.
        ox, oy = fx - 0.5, fy - 0.5
        margin = self.TURN_THRESHOLD - 0.5

        if travel_dir in (cell.DIR_PX, cell.DIR_NX):
            # Travelling horizontally: a vertical push turns the belt.
            if oy > margin:
                return cell.DIR_PY
            if oy < -margin:
                return cell.DIR_NY
        else:
            # Travelling vertically: a horizontal push turns the belt.
            if ox > margin:
                return cell.DIR_PX
            if ox < -margin:
                return cell.DIR_NX
        return None


class StructureBuilder(EntityBuilder):
    """Place a single structure, rotatable with R / shift+R.

    All-or-nothing: if any cell of the footprint is occupied the whole
    placement is frustrated and commits nothing, while still tinting every
    blocked cell so the obstruction is visible.
    """

    drag_based = False

    def __init__(self, entity_cls, name=None):
        self.entity_cls = entity_cls
        self.name = name or entity_cls.__name__
        self.direction = cell.DIR_PX
        self.flipped = False

    def cycle(self, backward=False):
        if not self.entity_cls.ROTATABLE:
            return False
        self.direction = cell.rotate_dir(self.direction, cw=not backward)
        return True

    def flip(self):
        """Mirror the held structure, if mirroring means anything for it.

        Chirality is derived from the port layout rather than declared, so an
        achiral type (Source, Sink) reports no change and F is a silent no-op
        -- there is no second version of it to reach.
        """
        if not self.entity_cls.is_chiral():
            return False
        self.flipped = not self.flipped
        return True

    def status(self):
        w, h = self.entity_cls.footprint_for(self.direction)
        s = f"{w}x{h} facing {cell.DIR_NAMES[self.direction]}"
        if self.entity_cls.is_chiral():
            s += " (flipped)" if self.flipped else ""
        return s

    def plan(self, engine, start, end, start_frac=(0.5, 0.5),
             end_frac=(0.5, 0.5)):
        # A structure ignores the drag -- it places under the cursor, rotated
        # by R and mirrored by F -- but it does use the sub-cell position.
        #
        # The cursor marks the *centre* of the footprint, not its origin
        # corner, so a multi-cell machine straddles the pointer and rotating
        # it turns about its middle. On an axis of even extent the midline is
        # a cell boundary rather than a cell, so `end_frac` picks which
        # boundary: the structure then tracks the pointer every half cell.
        # cells_for() still takes an origin -- that is the storage model
        # everywhere else -- so convert here, at the one place a gesture
        # becomes a placement.
        x, y = self.entity_cls.origin_for_cursor(
            *end, self.direction, end_frac)
        blocked = [
            c for c in self.entity_cls.cells_for(
                x, y, self.direction, self.flipped)
            if not engine.buildable(*c)
        ]
        placement = Placement(self.entity_cls, x, y, self.direction, blocked,
                              flipped=self.flipped)
        return BuildPlan([placement], all_or_nothing=True)


class UndergroundBuilder(EntityBuilder):
    """Place underground belt ends, alternating entrance then exit.

    Factorio-style: each click places one end. The first lays an entrance
    (diving), the next an exit (surfacing) which auto-pairs with a reachable
    unpaired entrance facing the same way. The tool simply alternates --
    pairing itself is the engine's job, and an exit placed out of range of
    any entrance is legal and just sits there unpaired.

    Click-based like a structure, so it previews under the cursor while IDLE
    and commits on release. R rotates the facing, which for an underground
    is the travel direction both ends share.
    """

    name = "Underground"
    drag_based = False

    def __init__(self):
        self.direction = cell.DIR_PX
        #: False = the next click lays an entrance, True = an exit.
        self.placing_exit = False

    def cycle(self, backward=False):
        self.direction = cell.rotate_dir(self.direction, cw=not backward)
        return True

    def status(self):
        end = "exit" if self.placing_exit else "entrance"
        return f"{end} facing {cell.DIR_NAMES[self.direction]}"

    def reset(self):
        """Back to laying an entrance. Called when the gesture is abandoned."""
        self.placing_exit = False

    def plan(self, engine, start, end, start_frac=(0.5, 0.5),
             end_frac=(0.5, 0.5)):
        x, y = end
        blocked = [] if engine.buildable(x, y) else [(x, y)]
        placement = Placement(Underground, x, y, self.direction, blocked)
        placement.is_exit = self.placing_exit
        # The span to draw dotted while previewing: only shown for an exit
        # that would actually pair, since an entrance has nothing to join yet.
        placement.under_partner = None
        if self.placing_exit and not blocked:
            partner = engine.find_under_partner(x, y, self.direction, True)
            if partner is not None:
                placement.under_partner = partner.origin
        return BuildPlan([placement], all_or_nothing=True)

    def committed(self):
        """Called after a successful placement: alternate the end type."""
        self.placing_exit = not self.placing_exit


def default_builders():
    """The toolbelt's starting tools, in slot order.

    Belt and its underground form first, then one StructureBuilder per
    registered structure. Adding a machine is a new Entity subclass in
    core/entity.py plus its entry in STRUCTURES -- nothing here changes.
    """
    return ([BeltBuilder(), UndergroundBuilder()]
            + [StructureBuilder(c) for c in STRUCTURES])
