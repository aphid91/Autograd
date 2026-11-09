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
    falls in names the direction -- equivalently, *which edge is nearest*.
    Used to aim a belt placed with a single click, where there is no drag to
    take a heading from -- clicking the right part of a square gives an
    east-facing belt, the top part north, and so on. Dead centre is +X,
    matching the old fixed default.

    **This is the single authority on the question**, and both places that ask
    it go through here. They did not always: the exit-turn rule below used to
    carry its own threshold-box version, which disagreed with this one over
    most of the cell. The two rules differ in kind -- a box has corner regions
    where *both* axes are past the threshold, and whichever axis the code
    tested first won. Since that test ran perpendicular to the diagonal split,
    it flipped the answer systematically: tracing a circle round the centre,
    two thirds of the way round came out facing the wrong way, with the
    north-east arc reading as east. See `quadrant_offsets()` for the piece
    `_exit_turn` needs to stay consistent with this.
    """
    d = quadrant_offsets(frac)
    if d is None:
        return cell.DIR_PX
    ox, oy = d
    if abs(ox) >= abs(oy):
        return cell.DIR_PX if ox > 0 else cell.DIR_NX
    return cell.DIR_PY if oy > 0 else cell.DIR_NY


def quadrant_offsets(frac):
    """Cursor offset from the cell centre as (ox, oy), each -0.5..0.5.

    Positive toward +X / +Y. `None` for dead centre, which has no direction
    at all and which every caller has to special-case anyway.

    Factored out so `quadrant_dir()` and `BeltBuilder._exit_turn()` measure
    the same thing from the same origin rather than each recomputing it.
    """
    ox, oy = frac[0] - 0.5, frac[1] - 0.5
    if ox == 0.0 and oy == 0.0:
        return None
    return (ox, oy)


class Placement:
    """One entity a plan would create, and whether it can actually happen."""

    __slots__ = ("entity_cls", "x", "y", "direction", "blocked_cells",
                 "flipped", "is_exit", "under_partner", "emit", "seed")

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
        # Source-only: the emit value to spawn with, or None for the type
        # default. Only a paste sets it -- placing a fresh Source from the
        # toolbelt should start at the default, not inherit whatever the last
        # copied one happened to emit.
        self.emit = None
        # Belt-only: `(on, value)` for a seeded cell, or None for the type
        # default. Only a paste sets it, for exactly the same reason -- a
        # freshly drawn belt run must not inherit a copied seed, or every
        # later run would quietly inject items.
        self.seed = None

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

    #: The entity type this tool places. The pipette (Q) reads the type under
    #: the cursor and asks the toolbelt for the slot whose builder claims it,
    #: so every tool must name its type for Q to find it -- see
    #: Toolbelt.select_tool_for().
    entity_cls = None

    def adopt(self, entity):
        """Match this tool's state to an existing entity, for the pipette.

        Called after Q has selected this slot. The default takes nothing,
        which is right for a tool with no orientation to copy; a structure
        takes the rotation and flip so the clone comes out identical.
        """
        return False

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
    entity_cls = Belt

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

        if len(dirs) > 1:
            # The last belt exits the run, and the pointer's position in that
            # final cell says which way. Straight on by default; past the
            # threshold toward a side edge, it turns and forms a corner.
            #
            # A single-cell run is excluded because the question does not
            # apply to it: there is no travel direction to deflect *from*,
            # its heading already came from quadrant_dir(start_frac), and
            # that is the final answer.
            #
            # Note this guard is **not** what fixes the wrong-facing ghost --
            # `_exit_turn` now returns None here anyway, since a single-cell
            # run's heading *is* the quadrant and so reads as "straight on".
            # Sharing the quadrant rule is the fix; mutation-testing the
            # guard alone leaves every test passing. It stays because
            # asking the question at all is meaningless on a one-cell run,
            # and relying on the answer coming back None is a subtler thing
            # to depend on than simply not asking.
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

        **Which way to turn is `quadrant_dir()`'s answer, not a separate
        rule.** This used to test one axis directly, which made it disagree
        with the quadrant split near the cell's corners -- see the note in
        `quadrant_dir()`. Going through the shared function means the turn a
        corner produces and the heading a single click produces can never
        again come out as different directions for the same pointer position.

        The threshold survives on top of it, and does the one job it is for:
        making *straight* sticky. The quadrant alone would turn the belt the
        moment the pointer crossed a diagonal, which is far too eager when
        the run already has a heading -- so a turn additionally needs the
        pointer genuinely out toward that edge.
        """
        offsets = quadrant_offsets(frac)
        if offsets is None:
            return None
        ox, oy = offsets
        margin = self.TURN_THRESHOLD - 0.5

        want = quadrant_dir(frac)
        if want == travel_dir:
            # Pointing the way we are already going: that is "straight on".
            return None
        if want == cell.opposite_dir(travel_dir):
            # Pulling backwards down the run we just came from. Not a turn --
            # reversing the final belt into its own predecessor would make the
            # two point at each other.
            return None

        # A genuine sideways quadrant. Require the pointer to be properly out
        # toward that edge before committing to a corner, measuring on the
        # axis the turn is actually onto.
        reach = ox if want in (cell.DIR_PX, cell.DIR_NX) else oy
        if abs(reach) <= margin:
            return None
        return want


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

    def adopt(self, entity):
        """Match the picked-up structure's orientation exactly.

        Both halves matter: taking the rotation without the flip would hand
        back a mirrored copy of a chiral machine, which looks identical in
        outline and wires up differently.
        """
        self.direction = entity.direction
        self.flipped = bool(entity.flipped)
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
    entity_cls = Underground

    def __init__(self):
        self.direction = cell.DIR_PX
        #: False = the next click lays an entrance, True = an exit.
        self.placing_exit = False

    def cycle(self, backward=False):
        self.direction = cell.rotate_dir(self.direction, cw=not backward)
        return True

    def adopt(self, entity):
        """Take the facing off a picked-up end, and lay the same kind next.

        Pipetting an exit should leave you holding an exit: the alternation
        is a convenience for laying a fresh pair, not something that should
        override an explicit pick.
        """
        self.direction = entity.direction
        self.placing_exit = entity.is_exit
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


class PasteBuilder(EntityBuilder):
    """Place every entity of a copy-stamp at once.

    Not a toolbelt tool -- paste mode hands this to the engine in place of
    the held tool, which is why it takes its stamp at construction and is
    rebuilt whenever the active stamp changes. Fitting the builder shape is
    the point: the engine's preview/commit path needs no paste-specific code.

    `all_or_nothing` is **per click**, not per tool: a plain click places the
    stamp only if every entity fits, while shift places whatever fits and
    rejects only the frustrated ones. The flag is therefore set from the
    modifier at plan time rather than fixed on the class.

    Rotation and flip are not handled here. They transform the *stamp*, which
    outlives any one plan -- see Clipboard.transform_active().
    """

    name = "Paste"
    drag_based = False

    def __init__(self, stamp, partial=False):
        self.stamp = stamp
        #: True when shift is held: place what fits, reject only clashes.
        self.partial = partial

    def status(self):
        if self.stamp is None:
            return "nothing copied"
        _x, _y, w, h = self.stamp.bounds()
        mode = "partial" if self.partial else "all-or-nothing"
        return f"{len(self.stamp)} entities {w}x{h} ({mode})"

    def plan(self, engine, start, end, start_frac=(0.5, 0.5),
             end_frac=(0.5, 0.5)):
        stamp = self.stamp
        if stamp is None or not stamp:
            return BuildPlan([], all_or_nothing=False)

        # The cursor sits in the middle of the stamp's full box, so a long
        # copied run straddles the pointer rather than hanging off it.
        ox, oy = stamp.origin_for_cursor(*end)

        placements = []
        for (entry, x, y) in stamp.place(ox, oy):
            blocked = [c for c in entry.entity_cls.cells_for(
                x, y, entry.direction, entry.flipped)
                if not engine.buildable(*c)]
            p = Placement(entry.entity_cls, x, y, entry.direction, blocked,
                          flipped=entry.flipped)
            p.is_exit = entry.is_exit
            # A captured Source keeps the value it was emitting, and a
            # captured belt keeps its seed.
            p.emit = entry.emit
            p.seed = entry.seed
            placements.append(p)

        # Intra-stamp underground pairing travels with the plan, so a copied
        # pair joins to itself rather than to whatever sits near the paste
        # site. Expressed as the partner's *cell*, which is what the engine's
        # pairing step already speaks.
        for (entry, _x, _y), p in zip(stamp.place(ox, oy), placements):
            if entry.partner is None:
                continue
            mate = placements[entry.partner]
            p.under_partner = (mate.x, mate.y)

        return BuildPlan(placements, all_or_nothing=not self.partial)


def default_builders():
    """The toolbelt's starting tools, in slot order.

    Belt and its underground form first, then one StructureBuilder per
    registered structure. Adding a machine is a new Entity subclass in
    core/entity.py plus its entry in STRUCTURES -- nothing here changes.
    """
    return ([BeltBuilder(), UndergroundBuilder()]
            + [StructureBuilder(c) for c in STRUCTURES])
