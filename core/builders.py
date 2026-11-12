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
from core.stamp import Stamp, StampEntry


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

    __slots__ = ("placements", "all_or_nothing", "paste_origin", "stamp")

    def __init__(self, placements, all_or_nothing=False):
        self.placements = placements
        self.all_or_nothing = all_or_nothing
        #: Where a paste landed, as `(ox, oy)`, and the stamp it came from.
        #: Both None for every other builder -- only a paste has annotations
        #: to carry, and only a paste knows the origin its own box chose.
        self.paste_origin = None
        self.stamp = None

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


class RawBeltBuilder(EntityBuilder):
    """Place one belt per click, facing wherever R last pointed it.

    The ordinary `BeltBuilder` is a *route planner*: it reads where in a cell
    you pointed, derives a path, and turns corners for you. That is the right
    tool for laying a run and the wrong one for fixing a single cell, where
    the sub-cell rules mean an apparently identical click produces different
    facings. This tool is the blunt instrument -- click places, `R` rotates,
    nothing is inferred from the pointer.

    Deliberately shaped like `StructureBuilder` rather than subclassing it:
    a belt is 1x1 with no ports, so centred placement, chirality and the
    footprint arithmetic all collapse to nothing, and inheriting them would
    mean carrying machinery whose every branch is dead here.

    **It does not consult `Belt.ROTATABLE`.** That flag says a belt's facing
    comes from the path it is drawn along, which is true of the planner and is
    exactly what this tool exists not to do -- so the rotation lives here, on
    the tool, rather than being bought by flipping a fact about the type.
    """

    name = "Raw belt"
    drag_based = False
    entity_cls = Belt

    def __init__(self):
        self.direction = cell.DIR_PX

    def cycle(self, backward=False):
        self.direction = cell.rotate_dir(self.direction, cw=not backward)
        return True

    def adopt(self, entity):
        """Take the facing off a picked-up belt, so Q clones its direction."""
        self.direction = entity.direction
        return True

    def status(self):
        return f"facing {cell.DIR_NAMES[self.direction]}"

    def plan(self, engine, start, end, start_frac=(0.5, 0.5),
             end_frac=(0.5, 0.5)):
        # Both fracs ignored on purpose -- see the class docstring.
        x, y = end
        blocked = [] if engine.buildable(x, y) else [(x, y)]
        return BuildPlan([Placement(Belt, x, y, self.direction, blocked)],
                         all_or_nothing=True)


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

    def flip(self):
        """Swap which end the next click lays: entrance <-> exit.

        The alternation is a convenience for laying a fresh pair, and it is
        wrong whenever the player wants the *other* half -- arriving at the
        far side of a gap with the tool primed for an entrance, wanting to
        close an existing entrance's pair. Without this the only way across
        is to place the unwanted end and erase it.

        F rather than a binding of its own because this is the same question
        F answers everywhere else: which of a type's two mirror-image
        orientations am I placing? An underground pair is chiral in the one
        way that matters -- which end dives -- and `is_chiral()` cannot see
        it, since the roles live in a field rather than in the port layout.
        """
        self.placing_exit = not self.placing_exit
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

        out = BuildPlan(placements, all_or_nothing=not self.partial)
        # Where the stamp landed, so the commit path can place the stamp's
        # annotations at the same origin. Carried on the plan rather than
        # recomputed by the caller: `origin_for_cursor` depends on the
        # stamp's box, and deriving it twice is two places to get it wrong.
        out.paste_origin = (ox, oy)
        out.stamp = stamp
        return out


class StampBuilder(PasteBuilder):
    """A copy-stamp **baked into a toolbelt tool**.

    Paste mode places `Clipboard.stamp` -- whatever was copied most recently.
    That makes a blueprint you want to keep a fragile thing to hold: one more
    `ctrl+C` and the history has moved on. Baking takes a snapshot and nails
    it to a slot, so the stamp survives later copies, row swaps and restarts.

    Subclasses `PasteBuilder` because placing a stamp is placing a stamp:
    the plan, the per-click all-or-nothing policy and the intra-stamp
    underground pairing are all identical, and the only differences are where
    the stamp comes from and that this one lives in a slot. Two consequences
    of being a slot tool rather than a mode:

    - **`R` and `F` transform this tool's own stamp**, not the clipboard's.
      In paste mode those keys go to `Clipboard.transform_active()` because
      the stamp outlives the plan; here the stamp belongs to the tool, so the
      tool handles them -- which is also what makes the toolbelt's generic
      `cycle()`/`flip()` dispatch work with no mode branch.
    - **It takes the partial policy per click the same way**, so the
      orchestrator's existing "rebuild when shift changes" logic applies
      unchanged. `partial` is therefore still a plain attribute.
    """

    drag_based = False

    def __init__(self, stamp, partial=False, name=None):
        super().__init__(stamp, partial=partial)
        _x, _y, w, h = stamp.bounds() if stamp else (0, 0, 0, 0)
        self.name = name or f"Stamp {w}x{h}"

    def cycle(self, backward=False):
        """Rotate the baked stamp in place.

        Mutates this tool's stamp rather than pushing a copy, matching what
        `Clipboard.transform_active()` does for the clipboard's: rotating a
        stamp is adjusting the thing in your hand.
        """
        if self.stamp is None:
            return False
        self.stamp = self.stamp.rotated(backward=backward)
        return True

    def flip(self):
        if self.stamp is None:
            return False
        self.stamp = self.stamp.mirrored()
        return True


class AnnotationTool:
    """Base for the annotation tools. **Not an EntityBuilder.**

    Deliberately outside that hierarchy rather than a subclass with stubbed
    methods. An `EntityBuilder` exists to turn a gesture into a `BuildPlan`
    the engine previews and commits; these tools never produce one, never
    touch the grid, and never spawn a row -- the orchestrator intercepts them
    *before* the builder path, the same way a clipboard mode overrides the
    held tool. Inheriting from `EntityBuilder` would mean carrying `plan()`,
    `adopt()` and `entity_cls` as dead members and inviting a future caller
    to treat one as placeable.

    They are still **toolbelt tools**: they occupy a slot, they are selected
    with a digit, and they are persisted by kind. So they implement the small
    protocol the toolbelt and panel actually use -- `name`, `drag_based`,
    `cycle()`, `flip()`, `status()` -- and nothing else.

    `entity_cls` is `None`, which keeps the pipette honest: `Q` resolves a
    board type to the slot whose tool claims it, and an annotation tool
    claims no entity type, so `Q` can never land on one.
    """

    name = "annotation"
    #: Both annotation tools are drag gestures (a stroke, or a box), but they
    #: are driven by the orchestrator's own annotation branch rather than by
    #: the input manager's plan machine, so this is reported for the panel's
    #: benefit and not used to pick a gesture style.
    drag_based = True
    entity_cls = None

    def __init__(self, colour=0, size=1):
        #: Index into annotations.PALETTE.
        self.colour = int(colour)
        #: Index into STROKE_WIDTHS or TEXT_SIZES, depending on the subclass.
        self.size = int(size)

    def _size_count(self):
        raise NotImplementedError

    def cycle(self, backward=False):
        """`R` walks the size/width list. Wraps, since it is a short ring."""
        n = self._size_count()
        self.size = (self.size + (-1 if backward else 1)) % n
        return True

    def flip(self):
        """`F` walks the palette.

        `F` means "mirror" on every other tool, which an annotation has no
        notion of -- so rather than leave the key dead on the two tools whose
        whole job is choosing a pen, it picks the colour. Same licence every
        tool has to define `R` and `F` for itself (see CLAUDE.md: "R and F
        deliberately mean different things in different tools").
        """
        from core.annotations import PALETTE
        self.colour = (self.colour + 1) % len(PALETTE)
        return True

    def colour_name(self):
        from core.annotations import PALETTE
        i = max(0, min(len(PALETTE) - 1, self.colour))
        return PALETTE[i][0]


class DrawAnnotationTool(AnnotationTool):
    """Freehand strokes: left-drag draws, right-drag erases.

    The telestrator pen. Right-drag is a *brush* eraser rather than a
    rectangle -- it deletes the stroke under the cursor as you sweep over it,
    which is how erasing a scribble actually feels. The rectangle eraser is
    the existing `X` mode, which now catches annotations too.
    """

    name = "Draw note"

    def _size_count(self):
        from core.annotations import STROKE_WIDTHS
        return len(STROKE_WIDTHS)

    def status(self):
        from core.annotations import STROKE_WIDTHS
        return (f"{self.colour_name()}, width "
                f"{self.size + 1}/{len(STROKE_WIDTHS)}"
                f"  —  drag to draw, right-drag to erase")


class TextAnnotationTool(AnnotationTool):
    """Text labels: left-drag sizes a box, which then opens for editing.

    The drag sets the box and the Status window takes the text, because a
    label is typed and the board is not a text field. A fresh box is created
    empty and selected, so the window is already open on it the moment the
    drag ends.
    """

    name = "Text note"

    def _size_count(self):
        from core.annotations import TEXT_SIZES
        return len(TEXT_SIZES)

    def status(self):
        from core.annotations import TEXT_SIZES
        return (f"{self.colour_name()}, size "
                f"{self.size + 1}/{len(TEXT_SIZES)}"
                f"  —  drag out a text box")


#: The tool kinds a slot can hold, as `kind -> factory`. This is the registry
#: the right-click type menu lists and the one the persisted layout names, so
#: a kind string is a **compatibility surface**: it ends up in the player's
#: config file, and renaming one silently empties their slot.
#:
#: Structures are not listed individually -- `"structure"` takes the type name
#: as a parameter, so adding a machine to `STRUCTURES` adds it to the menu
#: with no change here.
BUILDER_KINDS = {
    "belt": BeltBuilder,
    "raw_belt": RawBeltBuilder,
    "underground": UndergroundBuilder,
    "draw_note": DrawAnnotationTool,
    "text_note": TextAnnotationTool,
}

#: The annotation tools, for the orchestrator's "is this an annotation tool?"
#: test. A tuple rather than a flag on the class so the check is one isinstance
#: at the call site and cannot be accidentally inherited by a real builder.
ANNOTATION_TOOLS = (DrawAnnotationTool, TextAnnotationTool)


def make_builder(kind, type_name=None, stamp=None, pen=None):
    """Build a tool from its serialised description, or None if unknown.

    Fails **softly** by design: this is fed by a hand-editable config file and
    by saved layouts that may name a machine a later version renamed. An
    unknown kind or type leaves the slot empty, which is a recoverable state
    the player can see, rather than refusing to start.

    `pen` is the annotation tools' `(colour, size)` pair and is ignored by
    every other kind -- an explicit parameter rather than smuggled through
    `type_name`, so the signature says what each kind takes.
    """
    if kind == "structure":
        for c in STRUCTURES:
            if c.__name__ == type_name:
                return StructureBuilder(c)
        return None
    if kind == "stamp":
        return StampBuilder(stamp) if stamp else None
    factory = BUILDER_KINDS.get(kind)
    if factory is None:
        return None
    tool = factory()
    if factory in ANNOTATION_TOOLS and pen is not None:
        _apply_pen(tool, pen)
    return tool


def _apply_pen(tool, pen):
    """Restore an annotation tool's `(colour, size)`, clamped to range.

    Validated rather than trusted: this comes from a hand-editable file, and
    an out-of-range index would otherwise reach the palette lookup. A bad
    value leaves that field at its default, matching how every other field in
    this module fails.
    """
    from core.annotations import PALETTE
    if not isinstance(pen, (tuple, list)) or len(pen) < 2:
        return
    try:
        tool.colour = int(pen[0]) % len(PALETTE)
    except (TypeError, ValueError):
        pass
    try:
        tool.size = max(0, int(pen[1])) % tool._size_count()
    except (TypeError, ValueError):
        pass


def describe_builder(tool):
    """A tool's serialised description, or None if it cannot be persisted.

    The inverse of `make_builder()`. Returns a plain dict so the config layer
    needs no knowledge of builder classes -- it round-trips what it is given.
    """
    if tool is None:
        return None
    if isinstance(tool, StampBuilder):
        if not tool.stamp:
            return None
        return {"kind": "stamp", "stamp": stamp_to_data(tool.stamp),
                "name": tool.name}
    if isinstance(tool, StructureBuilder):
        return {"kind": "structure", "type": tool.entity_cls.__name__}
    if isinstance(tool, ANNOTATION_TOOLS):
        # The pen settings travel with the slot. A player who put a red
        # heading pen in slot 11 means that slot to stay a red heading pen,
        # and re-deriving the defaults on restart would quietly reset it --
        # the same reasoning that has a baked stamp persist its stamp.
        kind = "draw_note" if isinstance(tool, DrawAnnotationTool) \
            else "text_note"
        return {"kind": kind, "colour": int(tool.colour),
                "size": int(tool.size)}
    for kind, factory in BUILDER_KINDS.items():
        # Exact type, not isinstance: RawBeltBuilder and BeltBuilder are
        # siblings, but a subclass relationship among these would otherwise
        # resolve to whichever kind the dict happened to yield first.
        if type(tool) is factory:
            return {"kind": kind}
    return None


def stamp_to_data(stamp):
    """A stamp as JSON-safe plain data, for the persisted toolbar.

    Types travel as **class names** rather than as the integer type ids saves
    use. The two formats have opposite priorities: a `.npz` holds thousands of
    grid words and wants ids, while this holds a handful of entries and wants
    to stay readable and hand-editable. Type ids are append-only precisely
    because they are baked into grids; a name here costs nothing and survives
    a renumbering.
    """
    out = []
    for e in stamp.entries:
        out.append({
            "type": e.entity_cls.__name__,
            "dx": int(e.dx), "dy": int(e.dy),
            "direction": int(e.direction),
            "flipped": bool(e.flipped),
            "is_exit": bool(e.is_exit),
            "partner": None if e.partner is None else int(e.partner),
            "emit": None if e.emit is None else float(e.emit),
            # (on, value) -> a two-element list, since JSON has no tuples.
            "seed": None if e.seed is None
                    else [bool(e.seed[0]), float(e.seed[1])],
        })
    return out


def stamp_from_data(data):
    """Rebuild a stamp from `stamp_to_data()`, or None if it cannot be.

    Validating rather than trusting: this comes from a file the player can
    edit and an older version may have written. An entry naming an unknown
    type makes the **whole stamp** invalid rather than being skipped -- a
    blueprint with a hole in it would paste silently wrong, which is worse
    than an empty slot.
    """
    if not isinstance(data, list) or not data:
        return None
    by_name = {c.__name__: c for c in (Belt, Underground) + tuple(STRUCTURES)}
    entries = []
    for item in data:
        if not isinstance(item, dict):
            return None
        entity_cls = by_name.get(item.get("type"))
        if entity_cls is None:
            return None
        try:
            direction = int(item.get("direction", 0)) % 4
            dx, dy = int(item.get("dx", 0)), int(item.get("dy", 0))
            partner = item.get("partner")
            partner = None if partner is None else int(partner)
            emit = item.get("emit")
            emit = None if emit is None else float(emit)
            seed = item.get("seed")
            if seed is not None:
                seed = (bool(seed[0]), float(seed[1]))
        except (TypeError, ValueError, IndexError):
            return None
        entries.append(StampEntry(
            entity_cls, dx, dy, direction,
            flipped=bool(item.get("flipped", False)),
            is_exit=bool(item.get("is_exit", False)),
            partner=partner, emit=emit, seed=seed,
        ))
    # A partner index pointing outside the stamp is the dangling state
    # `unpair_under()` exists to prevent, so drop the link rather than the
    # entry -- an unpaired underground end is legal.
    for e in entries:
        if e.partner is not None and not 0 <= e.partner < len(entries):
            e.partner = None
    return Stamp(entries)


def default_builders():
    """The toolbelt's starting tools, in slot order.

    The belt tools first, then one StructureBuilder per registered structure.
    Adding a machine is a new Entity subclass in core/entity.py plus its entry
    in STRUCTURES -- nothing here changes.

    This is the **default** layout only. The player's saved arrangement, if
    there is one, is applied over the top by `app.py` -- see
    `core/config.py`. Deliberately still exactly this list when there is no
    saved layout, so a first run looks as it always did.
    """
    return ([BeltBuilder(), RawBeltBuilder(), UndergroundBuilder()]
            + [StructureBuilder(c) for c in STRUCTURES])
