"""Entities as views over the gameboard's entity array.

The array is the single source of truth. An Entity instance holds only
(gameboard, row) and reads/writes `gameboard.entities[row, offset]` through
properties, so there is no second copy of the state to drift out of sync.
Views are cheap and disposable -- make them, use them, drop them. Never cache
one across frames; a row can be freed and reused under you.

Subclasses declare fields and behaviour, never storage. To add an entity kind:
subclass Entity, set TYPE_ID, append field offsets after the common prefix,
bump ENTITY_STRIDE if needed, and the registry picks it up automatically.
"""

import math

from core import cell

# Row layout, shared prefix for every entity type. Offsets are float32 slots.
# Floats throughout, because this array is destined for an SSBO that compute
# shaders will read as floats; integral values round-trip exactly well below
# 2^24, which bounds everything stored here.
_F_ALIVE = 0
_F_TYPE = 1
_F_ORIGIN_X = 2
_F_ORIGIN_Y = 3
_F_DIR = 4
_F_W = 5
_F_H = 6
_F_FLIPPED = 7   # 0 or 1: mirrored about the authored X axis, before rotation
_COMMON_FIELDS = 8

# Room for subclass fields after the common prefix. Keep this a multiple of 4
# so each row lands on a vec4 boundary in the SSBO -- GLSL std430 arrays of
# vec4 are the easiest thing to index from a compute shader later.
#
# A gate needs, per port: the value held at each input, the value waiting at
# each output, and an occupancy flag for both. The widest machine (Mac) has
# three of each, so 8 common + 3*2 values + 3*2 flags = 20, rounded up to 24.
# Rows are cheap and branching is not, so every type carries the full set
# rather than packing per-type -- Source's 4x1 row simply has unused slots.
ENTITY_STRIDE = 24

#: Most *channels* of one kind any machine has -- not port faces. Fixes the
#: per-row gate layout, so a machine with more channels means bumping this
#: and ENTITY_STRIDE together.
#:
#: Faces and channels are not the same thing, and Sink is why. It accepts on
#: eight perimeter faces, but those are eight ways in to one consumer, not
#: eight independent values: whatever arrives gets destroyed, and a stack per
#: *face* would be meaningless. The arithmetic machines are the opposite --
#: Mac's three inputs are genuinely distinct channels (x, y, z) and must not
#: be conflated.
#:
#: Sizing the row by faces would mean 8 channels for every entity, so a belt
#: row would carry 32 unused floats. Sizing by channels keeps it at 3.
MAX_CHANNELS = 3


class Entity:
    """Base view. Not spawnable directly -- TYPE_ID must be set by a subclass."""

    # Footprint and faces are authored at DIR_PX (east) and rotated into the
    # entity's actual facing. See footprint_for() / faces_for().
    TYPE_ID = cell.TYPE_EMPTY
    FOOTPRINT = (1, 1)

    # Entity-local cell offsets that carry an input or output face, each as
    # (local_x, local_y, face_direction), authored at DIR_PX. Empty means the
    # type has no directional ports (a belt just moves along its direction).
    INPUTS = ()
    OUTPUTS = ()

    # Authored axes along which this type passes a signal through *unchanged*
    # -- its control channels. Each entry is a face direction; the channel is
    # understood to run the whole way across the footprint on that axis.
    #
    # This has to be declared rather than derived. Geometrically a mutated
    # channel looks exactly like a pass-through -- Shear's y enters -X and
    # leaves +X just as its x enters +Y and leaves -Y -- so only the machine's
    # semantics can tell them apart. The renderer draws a faint belt along
    # these axes to show which signals survive the operation.
    PASS_THROUGH = ()

    # Whether R rotates this type at all. Belts take their facing from the
    # path they are drawn along, so rotating one in isolation is meaningless.
    ROTATABLE = False

    # --- firing ----------------------------------------------------------
    #: When this type fires. Declared rather than special-cased: the tick
    #: reads FIRE_RULE and never names a machine, which is what keeps
    #: core/gates.py generic (and what lets glsl_types.py emit it later).
    #:
    #: FIRE_ALL  -- every input port must be fed. The normal arithmetic rule:
    #:              a Shear cannot compute x+y with only x.
    #: FIRE_ANY  -- any one fed input is enough. Sink's rule: it destroys
    #:              whatever arrives, and waiting for all four sides would
    #:              deadlock a sink fed from one direction.
    #:
    #: A type with no inputs at all (Source) is vacuously ready under either,
    #: so it fires whenever its output is clear -- which is what we want, and
    #: falls out rather than being special-cased. _self_check() pins that.
    FIRE_ALL = 0
    FIRE_ANY = 1
    FIRE_RULE = FIRE_ALL

    #: Value a Source emits. Only meaningful for types with no inputs; one
    #: day this becomes per-instance configuration, hence the class attribute
    #: rather than a literal in the tick.
    EMIT_VALUE = -1.0

    # --- appearance ------------------------------------------------------
    # Declared here, beside the ports, so a machine is defined in exactly one
    # place. core/glsl_types.py emits these into GLSL at shader-load time;
    # nothing in the shaders names a type by hand.
    #: Body colour as (r, g, b), 0..1. None means "not drawn as a body".
    COLOUR = None
    #: Glyph drawn centred on the footprint. One of the GLYPH_* names.
    GLYPH = "none"

    # Exposed for Gameboard, which pokes these without building a view.
    F_ALIVE = _F_ALIVE
    F_TYPE = _F_TYPE
    F_W = _F_W
    F_H = _F_H
    F_FLIPPED = _F_FLIPPED

    # --- gate state, shared by every type ---------------------------------
    # Laid out on the base class rather than per-subclass so the tick can read
    # any machine's ports without knowing which it is. Slots are indexed by
    # **channel**, which for every machine but Sink is the port's position in
    # INPUTS / OUTPUTS -- see MAX_CHANNELS and in_channel().
    #
    #   F_IN_VALUE  + c   value held on input channel c
    #   F_IN_HELD   + c   1 if that channel is fed, else 0
    #   F_OUT_VALUE + c   value waiting on output channel c
    #   F_OUT_HELD  + c   1 if that waiting zone is occupied, else 0
    #
    # The waiting zone is the whole trick behind firing every tick -- see
    # core/tick.py. A gate reads F_OUT_HELD to decide whether it is blocked,
    # and the belt phase drains it, so the two phases never need to see each
    # other's intermediate state.
    F_IN_VALUE = _COMMON_FIELDS
    F_IN_HELD = _COMMON_FIELDS + MAX_CHANNELS
    F_OUT_VALUE = _COMMON_FIELDS + 2 * MAX_CHANNELS
    F_OUT_HELD = _COMMON_FIELDS + 3 * MAX_CHANNELS
    _GATE_FIELDS_END = _COMMON_FIELDS + 4 * MAX_CHANNELS

    #: Which channel each declared port face belongs to. Defaults to one
    #: channel per face, in declaration order, which is what every
    #: arithmetic machine wants. Sink overrides it to fold its eight
    #: perimeter faces onto a single channel -- see MAX_CHANNELS.
    @classmethod
    def in_channel(cls, port_index):
        return port_index

    @classmethod
    def out_channel(cls, port_index):
        return port_index

    @classmethod
    def in_channels(cls):
        """How many distinct input channels this type has."""
        if not cls.INPUTS:
            return 0
        return max(cls.in_channel(i) for i in range(len(cls.INPUTS))) + 1

    @classmethod
    def out_channels(cls):
        if not cls.OUTPUTS:
            return 0
        return max(cls.out_channel(i) for i in range(len(cls.OUTPUTS))) + 1

    # --- gate state accessors --------------------------------------------
    # These are indexed by **channel**, not by port face. A Sink's eight
    # faces all map to channel 0, so feeding any of them fills the same slot.
    def input_count(self):
        return self.in_channels()

    def output_count(self):
        return self.out_channels()

    def in_held(self, i):
        return bool(self._get(self.F_IN_HELD + i))

    def in_value(self, i):
        return float(self._get(self.F_IN_VALUE + i))

    def set_input(self, i, value):
        """Feed input port i. Overwrites whatever was there."""
        self._set(self.F_IN_VALUE + i, float(value))
        self._set(self.F_IN_HELD + i, 1.0)

    def clear_input(self, i):
        self._set(self.F_IN_VALUE + i, 0.0)
        self._set(self.F_IN_HELD + i, 0.0)

    def out_held(self, i):
        return bool(self._get(self.F_OUT_HELD + i))

    def out_value(self, i):
        return float(self._get(self.F_OUT_VALUE + i))

    def set_output(self, i, value):
        """Put a value in output port i's waiting zone."""
        self._set(self.F_OUT_VALUE + i, float(value))
        self._set(self.F_OUT_HELD + i, 1.0)

    def clear_output(self, i):
        self._set(self.F_OUT_VALUE + i, 0.0)
        self._set(self.F_OUT_HELD + i, 0.0)

    def fire(self, values):
        """Transform fed input channel values into output channel values.

        `values` is a list of length input_count(); entries for unfed
        channels are None (only reachable under FIRE_ANY). Returns a list of
        length output_count(), where None means "emit nothing on this
        channel".

        Overridden per machine. The base implementation emits nothing, which
        is correct for a type with no outputs and a safe default for a type
        whose arithmetic has not been written.

        Channels are numbered by `in_channel()` / `out_channel()`, which for
        every machine but Sink is just the port's position in INPUTS /
        OUTPUTS. So a subclass reads `values[i]` for its i'th declared input
        and returns its i'th declared output in the same order. That
        indexing is the only contract between a machine's geometry and its
        arithmetic.
        """
        return [None] * self.output_count()

    def inputs_ready(self):
        """Does this gate's firing rule consider it fed?

        Reads FIRE_RULE rather than branching on type. A machine with no
        inputs is vacuously ready under either rule, which is what makes
        Source fire without a special case.
        """
        n = self.input_count()
        if n == 0:
            return True
        if self.FIRE_RULE == self.FIRE_ANY:
            return any(self.in_held(i) for i in range(n))
        return all(self.in_held(i) for i in range(n))

    def outputs_clear(self):
        """Is every output's waiting zone free?

        A gate cannot fire with anything still waiting -- that is the
        back-pressure signal, and it is why a backed-up belt eventually
        stalls the machine feeding it rather than losing items.
        """
        return not any(self.out_held(i)
                       for i in range(self.output_count()))

    #: True if mirroring this type produces a genuinely different machine.
    #: A type whose port layout is symmetric about the authored X axis is
    #: achiral, and F is a no-op for it -- see is_chiral().
    @classmethod
    def is_chiral(cls):
        """Does flipping this type change its port layout at all?

        Derived rather than declared, so it cannot drift from the ports: flip
        every face and cell, and see whether the resulting layout differs.
        """
        if not cls.INPUTS and not cls.OUTPUTS:
            return False
        plain = (frozenset(cls.faces_for(0, 0, cell.DIR_PX, False)[0]),
                 frozenset(cls.faces_for(0, 0, cell.DIR_PX, False)[1]))
        mirrored = (frozenset(cls.faces_for(0, 0, cell.DIR_PX, True)[0]),
                    frozenset(cls.faces_for(0, 0, cell.DIR_PX, True)[1]))
        return plain != mirrored

    @classmethod
    def footprint_for(cls, direction):
        """(w, h) of this type rotated into `direction`."""
        w, h = cls.FOOTPRINT
        return (h, w) if direction % 2 else (w, h)

    @classmethod
    def centre_offset(cls, direction):
        """(dx, dy) from the footprint's origin to the cell under the cursor.

        Structures are placed centred on the pointer rather than by their
        origin corner, so a big machine straddles the cursor and a rotation
        turns about its middle instead of swinging around a corner.

        For an even extent this is the lower/left of the two middle cells.
        Which of the two the cursor actually picks is decided by
        origin_for_cursor(), from the sub-cell position.
        """
        w, h = cls.footprint_for(direction)
        return (w - 1) // 2, (h - 1) // 2

    @classmethod
    def origin_for_cursor(cls, cx, cy, direction, frac=(0.5, 0.5)):
        """Footprint origin for a placement centred on the pointer.

        `frac` is the cursor's position within its cell, 0..1 on each axis
        with y up. It matters only on an axis of **even** extent, and there
        it doubles the placement resolution.

        An odd extent has a middle cell, so the footprint centres on the cell
        under the cursor and the fraction is irrelevant. An even extent has
        no middle cell -- its midline falls on a cell *boundary* -- so the
        pointer picks which boundary: the nearest one. Pointing at the left
        half of a cell puts the seam on that cell's left edge, the right half
        on its right edge. A 2x1 therefore follows the pointer every half
        cell rather than jumping a whole one, and a footprint even on both
        axes snaps its centre to the nearest *corner*.

        Working in continuous coordinates makes this fall out: the midline
        wants to sit at the cursor, so round it to the lattice the extent
        allows -- integers for even extents, half-integers for odd ones.
        """
        w, h = cls.footprint_for(direction)

        def axis(c, f, extent):
            # The caller's fraction is 0..1 within the cell. Clamp just below
            # 1 so an exact 1.0 -- the far edge, which InputManager never
            # produces but a direct caller might -- cannot round the midline
            # into the next cell and step the footprint off the cursor.
            f = min(max(f, 0.0), 1.0 - 1e-9)
            # Where the footprint's midline should sit, in world units.
            want = c + f
            if extent % 2:
                # Odd: the midline runs through a cell centre (half-integer).
                mid = math.floor(want) + 0.5
            else:
                # Even: the midline is a cell boundary (integer). Round to
                # the nearest, so the near edge of the pointer's cell wins.
                mid = math.floor(want + 0.5)
            # origin = midline - half the extent.
            return int(round(mid - extent * 0.5))

        return axis(cx, frac[0], w), axis(cy, frac[1], h)

    @classmethod
    def cells_for(cls, x, y, direction, flipped=False):
        """Every (cx, cy) a placement at (x, y) facing `direction` would cover.

        `flipped` is accepted for signature parity with faces_for() but cannot
        change the answer: mirroring permutes cells within the footprint's
        bounding box, it does not move the box.

        The origin stays at (x, y) and the footprint extends in +X/+Y from it,
        so a rotated 2x1 occupies (x,y) and (x,y+1) rather than pivoting about
        some interior point. That keeps "the cell under the cursor is the
        origin" true at every rotation, which is what makes placement
        predictable.
        """
        w, h = cls.footprint_for(direction)
        return [(x + lx, y + ly) for lx in range(w) for ly in range(h)]

    @classmethod
    def faces_for(cls, x, y, direction, flipped=False):
        """(inputs, outputs) in world space for a placement.

        Each entry is ((cell_x, cell_y), face_direction). Faces are derived
        from the type and its orientation rather than stored per-instance --
        every instance of a type has the same port layout, so storage would be
        pure duplication. If ports ever become per-instance configurable this
        is where that changes.

        A cell and its faces must transform *together*: this applies the flip
        then the rotation to both, which is what keeps a port on the outside
        of the footprint at every orientation.
        """
        def place(spec):
            out = []
            for (lx, ly, face) in spec:
                rx, ry = cls.place_local(lx, ly, direction, flipped)
                if flipped:
                    face = cell.flip_dir_y(face)
                out.append(((x + rx, y + ry), cell.rotate_dir_ccw_n(face, direction)))
            return out

        return place(cls.INPUTS), place(cls.OUTPUTS)

    @classmethod
    def face_masks_at(cls, lx, ly):
        """(input_mask, output_mask) for one authored local cell, as bitmasks.

        Indexed by direction, authored at DIR_PX. This is the per-cell view of
        INPUTS/OUTPUTS and is the Python side of the GLSL
        input_face_mask()/output_face_mask() pair -- the shader answers "is
        this face a port?" from a single cell, so the two must agree cell by
        cell, not merely type by type. test_drive.py pins that.
        """
        in_mask = out_mask = 0
        for (ax, ay, face) in cls.INPUTS:
            if (ax, ay) == (lx, ly):
                in_mask |= 1 << face
        for (ax, ay, face) in cls.OUTPUTS:
            if (ax, ay) == (lx, ly):
                out_mask |= 1 << face
        return in_mask, out_mask

    @classmethod
    def place_local(cls, lx, ly, direction, flipped=False):
        """An authored local offset -> its offset within the placed footprint.

        Flip first (mirror about the authored X axis), then rotate, then shift
        back into the +X/+Y quadrant. Rotating (1,0) by a quarter turn gives
        (0,1), but rotating (0,0)..(1,0) as a *block* gives offsets that can go
        negative; normalising against the rotated footprint keeps every offset
        non-negative so it still packs into .y's unsigned local field.

        This is the mapping Gameboard._stamp_footprint() writes into .y, so
        `.y` holds *placed* offsets, not authored ones. Anything reading .y
        back to ask about ports must invert it -- see unplace_local().
        """
        w, h = cls.FOOTPRINT
        if flipped:
            ly = (h - 1) - ly
        rx, ry = cell.rotate_offset(lx, ly, direction)
        # The bounding box corners, rotated, give the shift needed.
        corners = [cell.rotate_offset(cx, cy, direction)
                   for cx in (0, w - 1) for cy in (0, h - 1)]
        min_x = min(c[0] for c in corners)
        min_y = min(c[1] for c in corners)
        return rx - min_x, ry - min_y

    @classmethod
    def unplace_local(cls, px, py, direction, flipped=False):
        """The inverse of place_local(): placed offset -> authored offset.

        The renderer reads a *placed* offset out of .y but the port tables are
        authored at DIR_PX, so it must come back here before any lookup. Doing
        the lookup on the placed offset directly is what put Sink's ports on
        interior edges: the face was being un-rotated while the cell was not.
        """
        w, h = cls.FOOTPRINT
        corners = [cell.rotate_offset(cx, cy, direction)
                   for cx in (0, w - 1) for cy in (0, h - 1)]
        min_x = min(c[0] for c in corners)
        min_y = min(c[1] for c in corners)
        # Undo the quadrant shift, then the rotation.
        rx, ry = cell.rotate_offset(px + min_x, py + min_y, (4 - direction) % 4)
        if flipped:
            ry = (h - 1) - ry
        return rx, ry

    @classmethod
    def _rotate_local(cls, lx, ly, direction):
        """Deprecated alias kept for callers predating the flip support."""
        return cls.place_local(lx, ly, direction, False)

    _registry = {}

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        if cls.TYPE_ID != cell.TYPE_EMPTY:
            existing = Entity._registry.get(cls.TYPE_ID)
            if existing is not None and existing is not cls:
                raise ValueError(
                    f"type id {cls.TYPE_ID} claimed by both {existing.__name__} "
                    f"and {cls.__name__}"
                )
            Entity._registry[cls.TYPE_ID] = cls

    @staticmethod
    def view_for(type_id):
        """The view class registered for a type id, falling back to Entity."""
        return Entity._registry.get(int(type_id), Entity)

    def __init__(self, gameboard, row):
        self.board = gameboard
        self.row = row
        # None means "no pending override, read the grid". See is_ghost.
        self._ghost = None

    # --- raw slot access ------------------------------------------------
    def _get(self, offset):
        return self.board.entities[self.row, offset]

    def _set(self, offset, value):
        self.board.entities[self.row, offset] = value
        self.board.entities_dirty.add(self.row)

    def initialise(self, x, y, direction, is_ghost=False, flipped=False):
        """Populate a freshly claimed row. Subclasses extend, calling super()."""
        # Store the *rotated* extent, so footprint_w/h describe the entity as
        # it actually sits on the board. A flip mirrors within that extent and
        # so never changes it.
        w, h = self.footprint_for(direction)
        row = self.board.entities[self.row]
        row[:] = 0
        row[_F_ALIVE] = 1.0
        row[_F_TYPE] = float(self.TYPE_ID)
        row[_F_ORIGIN_X] = float(x)
        row[_F_ORIGIN_Y] = float(y)
        row[_F_DIR] = float(direction)
        row[_F_W] = float(w)
        row[_F_H] = float(h)
        row[_F_FLIPPED] = 1.0 if flipped else 0.0
        self._ghost = is_ghost
        self.board.entities_dirty.add(self.row)

    # --- common properties ----------------------------------------------
    @property
    def alive(self):
        return bool(self._get(_F_ALIVE))

    @property
    def type_id(self):
        return int(self._get(_F_TYPE))

    @property
    def origin(self):
        return int(self._get(_F_ORIGIN_X)), int(self._get(_F_ORIGIN_Y))

    @property
    def direction(self):
        return int(self._get(_F_DIR))

    @direction.setter
    def direction(self, value):
        self._set(_F_DIR, float(value))

    @property
    def flipped(self):
        return bool(self._get(_F_FLIPPED))

    @flipped.setter
    def flipped(self, value):
        self._set(_F_FLIPPED, 1.0 if value else 0.0)

    @property
    def footprint_w(self):
        return max(1, int(self._get(_F_W)))

    @property
    def footprint_h(self):
        return max(1, int(self._get(_F_H)))

    # is_ghost lives in the grid cell (.z bit 0) rather than the entity row,
    # because the renderer reads it per-cell. A view caches it in _ghost so
    # that Gameboard._stamp_footprint() has a value to write *before* the cells
    # exist (on spawn) or when changing it (set_ghost); reads fall back to the
    # grid, which is the persistent store.
    @property
    def is_ghost(self):
        if self._ghost is not None:
            return self._ghost
        ox, oy = self.origin
        if not self.board.in_bounds(ox, oy):
            return False
        return cell.is_ghost(self.board.grid[oy, ox, cell.CH_Z])

    @is_ghost.setter
    def is_ghost(self, value):
        self._ghost = bool(value)

    def cells(self):
        """Every (x, y) this entity occupies."""
        ox, oy = self.origin
        for lx in range(self.footprint_w):
            for ly in range(self.footprint_h):
                yield ox + lx, oy + ly

    def __repr__(self):
        name = cell.TYPE_NAMES.get(self.type_id, f"type{self.type_id}")
        return f"<{type(self).__name__} row={self.row} {name} at {self.origin}>"


class Belt(Entity):
    """A single-cell conveyor. Direction is which way items travel."""

    TYPE_ID = cell.TYPE_BELT
    FOOTPRINT = (1, 1)

    # No belt-specific row fields. Items belong to the *segment* a belt is
    # part of, not to the cell -- see core/segments.py -- so per-cell item
    # state would be a second home for something that already has one.

    def initialise(self, x, y, direction, is_ghost=False, flipped=False):
        # A belt is 1x1 with no ports, so `flipped` is meaningless for it --
        # accepted and passed through only so every entity type takes the
        # same orientation arguments and callers need no special case.
        super().initialise(x, y, direction, is_ghost=is_ghost, flipped=flipped)


class Underground(Entity):
    """One end of an underground belt -- an entrance or an exit.

    The two ends are **separate entities**, each 1x1, each storing the other's
    row. That matches how they are placed (one click each) and means an
    unpaired entrance is a perfectly legal thing to have sitting on the board,
    exactly as in Factorio. The tiles *between* the ends are not owned by
    anything: belts and machines can be built straight over the span, which is
    the whole point of going underground.

    A single entity spanning the gap would be the obvious alternative and is
    wrong here -- Gameboard stamps every cell of a footprint, so the span
    would claim tiles it must leave free.

    `direction` is the travel direction for both ends: an entrance at (x,y)
    facing +X dives eastward and its exit surfaces further east, still facing
    +X. So a pair always shares a facing, and the exit is downstream.
    """

    TYPE_ID = cell.TYPE_UNDER
    FOOTPRINT = (1, 1)
    ROTATABLE = True

    #: Tiles that may separate the two ends, i.e. the gap they jump. Factorio's
    #: base underground belt spans 4 tiles, putting the ends 5 apart. Faster
    #: tiers reach further; when those arrive this becomes per-tier.
    MAX_GAP = 4

    #: Greatest distance between entrance and exit cells.
    MAX_SPAN = MAX_GAP + 1

    F_IS_EXIT = _COMMON_FIELDS + 0
    #: Partner's entity row, or 0 for an unpaired end. Row 0 is never a live
    #: entity, so 0 is an unambiguous "none".
    F_PARTNER = _COMMON_FIELDS + 1

    def initialise(self, x, y, direction, is_ghost=False, flipped=False,
                   is_exit=False, partner=0):
        super().initialise(x, y, direction, is_ghost=is_ghost, flipped=flipped)
        self._set(self.F_IS_EXIT, 1.0 if is_exit else 0.0)
        self._set(self.F_PARTNER, float(partner))

    @property
    def is_exit(self):
        return bool(self._get(self.F_IS_EXIT))

    @property
    def partner(self):
        """The other end's row, or 0 if unpaired."""
        return int(self._get(self.F_PARTNER))

    @partner.setter
    def partner(self, row):
        self._set(self.F_PARTNER, float(row))

    @property
    def paired(self):
        row = self.partner
        return row != 0 and self.board.is_row_alive(row)

    @classmethod
    def span_cells(cls, a, b):
        """The cells strictly between two ends, or None if they cannot pair.

        Ends pair when they are on a common axis, no further than MAX_SPAN
        apart, and not on the same cell. Nothing in between blocks -- the
        span passes under whatever is there, which is the point.
        """
        (ax, ay), (bx, by) = a, b
        dx, dy = bx - ax, by - ay
        if (dx != 0) == (dy != 0):
            return None                      # diagonal, or the same cell
        dist = abs(dx) + abs(dy)
        if not 1 <= dist <= cls.MAX_SPAN:
            return None
        sx = (dx > 0) - (dx < 0)
        sy = (dy > 0) - (dy < 0)
        return [(ax + sx * i, ay + sy * i) for i in range(1, dist)]


class Structure(Entity):
    """Base for placed buildings: rotatable, with input and output faces.

    Every cell of a footprint carries the same entity id in .x and its own
    (local_x, local_y) in .y, so the renderer can draw a multi-cell sprite by
    reading the local offset.
    """

    ROTATABLE = True


# The machine set. Every one is a continuous rectangle, authored at DIR_PX.
#
# The shared idiom: a *pass-through* channel crosses the body on one axis and
# leaves its value unchanged, while the channel taking the long way across the
# footprint is the one that gets mutated. Which cell carries which port is the
# whole specification -- see face_masks_at(), and note that a port lives on one
# cell of the footprint, not on all of them.


class Swap(Structure):
    """Conditional swap: x,y,z -> x, (y,z if x>0 else z,y).

    Authored at DIR_PX as a 1x2 (tall) body. The x channel runs vertically
    through the *whole* body -- in at the north face of the top cell, out at
    the south face of the bottom cell -- so the two cells carry one half each
    of that pass-through. y and z each cross their own cell horizontally,
    west to east, and are the pair that may be exchanged.
    """

    TYPE_ID = cell.TYPE_SWAP
    FOOTPRINT = (1, 2)
    COLOUR = (0.60, 0.40, 0.42)   # reddish
    GLYPH = "zed"

    INPUTS = (
        (0, 1, cell.DIR_PY),   # x in, top of the body
        (0, 1, cell.DIR_NX),   # y in
        (0, 0, cell.DIR_NX),   # z in
    )
    OUTPUTS = (
        (0, 0, cell.DIR_NY),   # x out, bottom of the body
        (0, 1, cell.DIR_PX),   # y' out
        (0, 0, cell.DIR_PX),   # z' out
    )

    # x survives untouched; y and z are the pair that may be exchanged.
    PASS_THROUGH = (cell.DIR_NY,)

    def fire(self, values):
        """x,y,z -> x, (y,z if x>0 else z,y).

        x is the control: it passes through unchanged (it is the declared
        PASS_THROUGH axis) and its sign decides whether y and z swap.
        x == 0 means no swap.
        """
        x, y, z = values
        if x > 0.0:
            return [x, y, z]
        return [x, z, y]


class Shear(Structure):
    """Shear: x,y -> x, x+y.

    Authored at DIR_PX as a 2x1 (wide) body. x passes straight down through
    the first cell only -- one cell in, one cell out, unchanged. y enters the
    west face of that same first cell, travels the long way across both cells,
    and leaves the east face of the second as x+y.
    """

    TYPE_ID = cell.TYPE_SHEAR
    FOOTPRINT = (2, 1)
    COLOUR = (0.40, 0.56, 0.44)   # green
    GLYPH = "plus"

    INPUTS = (
        (0, 0, cell.DIR_PY),   # x in
        (0, 0, cell.DIR_NX),   # y in
    )
    OUTPUTS = (
        (0, 0, cell.DIR_NY),   # x out, same cell: a pass-through
        (1, 0, cell.DIR_PX),   # x+y out, at the far end
    )

    # x survives untouched; y is what the machine rewrites.
    PASS_THROUGH = (cell.DIR_NY,)

    def fire(self, values):
        """x,y -> x, x+y. Outputs are declared (x out, x+y out)."""
        x, y = values
        return [x, x + y]


class Mac(Structure):
    """Multiply-accumulate: x,y,z -> x, y, z + x*y.

    Authored at DIR_PX as a 2x1 (wide) body, like Shear but with a second
    vertical control channel: x passes down through the first cell and y down
    through the second, both unchanged. z crosses the long way and picks up
    the product.
    """

    TYPE_ID = cell.TYPE_MAC
    FOOTPRINT = (2, 1)
    COLOUR = (0.54, 0.44, 0.62)   # purple
    GLYPH = "cross"

    INPUTS = (
        (0, 0, cell.DIR_PY),   # x in
        (1, 0, cell.DIR_PY),   # y in
        (0, 0, cell.DIR_NX),   # z in
    )
    OUTPUTS = (
        (0, 0, cell.DIR_NY),   # x out
        (1, 0, cell.DIR_NY),   # y out
        (1, 0, cell.DIR_PX),   # z + x*y out
    )

    # Both control channels survive untouched; z picks up the product.
    PASS_THROUGH = (cell.DIR_NY,)

    def fire(self, values):
        """x,y,z -> x, y, z + x*y. Declared order is (x out, y out, z' out)."""
        x, y, z = values
        return [x, y, z + x * y]


class Source(Structure):
    """4x1 emitter with a single output at one short end.

    Authored at DIR_PX lying along X with the port on the +X tip, so the
    default placement emits east and R walks it around.
    """

    TYPE_ID = cell.TYPE_SOURCE
    FOOTPRINT = (4, 1)
    COLOUR = (0.42, 0.52, 0.60)
    GLYPH = "dot"

    INPUTS = ()
    OUTPUTS = ((3, 0, cell.DIR_PX),)

    def fire(self, values):
        """Emit a constant. No inputs, so `values` is empty.

        With no inputs this type is vacuously ready every tick, so it emits
        whenever its waiting zone is clear -- i.e. at whatever rate the belt
        downstream will take. That self-regulation falls out of the
        back-pressure rule rather than needing a rate here.
        """
        return [self.EMIT_VALUE]


class Sink(Structure):
    """2x2 consumer that accepts on every perimeter face.

    Eight inputs: each of the four cells carries the two outward faces of its
    corner, so material can be fed in from anywhere without aiming at a
    particular tile.

    Square and fully symmetric, so rotation changes nothing -- R is a no-op
    in practice, but it stays ROTATABLE rather than special-casing the
    toolbelt. Being achiral, F does nothing either (is_chiral() derives that
    from this port layout).

    Fires on *any* fed input rather than all of them, and destroys what it
    takes. Waiting for all eight would deadlock a sink fed from one side,
    which is the normal way to use one. Nothing is recorded yet; a stack per
    input is the obvious next step.
    """

    TYPE_ID = cell.TYPE_SINK
    FOOTPRINT = (2, 2)
    COLOUR = (0.34, 0.34, 0.40)
    GLYPH = "dot"
    FIRE_RULE = Structure.FIRE_ANY

    INPUTS = (
        (0, 1, cell.DIR_NX), (0, 1, cell.DIR_PY),   # top-left corner
        (1, 1, cell.DIR_PY), (1, 1, cell.DIR_PX),   # top-right
        (1, 0, cell.DIR_PX), (1, 0, cell.DIR_NY),   # bottom-right
        (0, 0, cell.DIR_NY), (0, 0, cell.DIR_NX),   # bottom-left
    )
    OUTPUTS = ()

    @classmethod
    def in_channel(cls, port_index):
        """Every face feeds the same channel.

        The eight perimeter faces are eight ways *in* to one consumer, not
        eight independent values: a sink destroys whatever arrives, so a
        separate slot per face would be meaningless -- and would size every
        entity row in the game for eight channels. See MAX_CHANNELS.
        """
        return 0


#: Every placeable structure, in the order the toolbelt offers them.
STRUCTURES = (Swap, Shear, Mac, Source, Sink)


def _self_check():
    """The field layout must fit the stride, and ids must be unique."""
    assert _COMMON_FIELDS <= ENTITY_STRIDE
    # The gate block must fit, and every machine's ports must fit MAX_PORTS.
    assert Entity._GATE_FIELDS_END <= ENTITY_STRIDE, (
        f"gate fields need {Entity._GATE_FIELDS_END} slots, stride is "
        f"{ENTITY_STRIDE}")
    assert ENTITY_STRIDE % 4 == 0, "rows must land on vec4 boundaries"
    for c in STRUCTURES:
        # Channels, not faces: Sink has 8 faces on 1 channel.
        assert c.in_channels() <= MAX_CHANNELS, (c.__name__, c.in_channels())
        assert c.out_channels() <= MAX_CHANNELS, (c.__name__, c.out_channels())
        # Every face must map to a channel inside the row's range.
        for i in range(len(c.INPUTS)):
            assert 0 <= c.in_channel(i) < MAX_CHANNELS, (c.__name__, i)
        for i in range(len(c.OUTPUTS)):
            assert 0 <= c.out_channel(i) < MAX_CHANNELS, (c.__name__, i)

    # --- firing rules and arithmetic --------------------------------------
    # Every machine's fire() must return one value per declared output, and
    # the tick indexes ports positionally, so a signature that disagrees with
    # the declaration order would wire the wrong channel to the wrong face.
    class _FakeBoard:
        def __init__(self):
            import numpy as _np
            self.entities = _np.zeros((4, ENTITY_STRIDE), dtype=_np.float32)
            self.entities_dirty = type("_D", (), {"add": lambda s, r: None})()
            self.grid = _np.zeros((1, 1, 4), dtype=_np.int32)

        def in_bounds(self, x, y):
            return False

    def _view(cls):
        v = cls(_FakeBoard(), 1)
        v._ghost = False
        return v

    # Swap: x is the control and passes through; y and z exchange iff x > 0.
    sw = _view(Swap)
    assert sw.fire([1.0, 7.0, 9.0]) == [1.0, 7.0, 9.0], "x>0 must not swap"
    assert sw.fire([-1.0, 7.0, 9.0]) == [-1.0, 9.0, 7.0], "x<=0 must swap"
    assert sw.fire([0.0, 7.0, 9.0]) == [0.0, 9.0, 7.0], "x==0 means swap"

    # Shear: x passes through, y becomes x+y.
    sh = _view(Shear)
    assert sh.fire([3.0, 4.0]) == [3.0, 7.0]
    assert sh.fire([-2.0, 5.0]) == [-2.0, 3.0]

    # Mac: both controls pass through, z accumulates x*y.
    mc = _view(Mac)
    assert mc.fire([2.0, 3.0, 10.0]) == [2.0, 3.0, 16.0]
    assert mc.fire([0.0, 9.0, 4.0]) == [0.0, 9.0, 4.0], "x==0 adds nothing"

    # Source emits its constant; Sink emits nothing at all.
    src = _view(Source)
    assert src.fire([]) == [Source.EMIT_VALUE]
    assert len(src.fire([])) == Source.out_channels()
    snk = _view(Sink)
    assert snk.fire([None]) == [], "a sink emits nothing"
    # Sink's eight faces fold onto one channel, so the row holds one value.
    assert Sink.in_channels() == 1, Sink.in_channels()
    assert all(Sink.in_channel(i) == 0 for i in range(len(Sink.INPUTS)))
    assert Sink.out_channels() == 0

    # Every machine returns exactly one value per output channel.
    for c, args in ((Swap, [1.0, 2.0, 3.0]), (Shear, [1.0, 2.0]),
                    (Mac, [1.0, 2.0, 3.0]), (Source, []),
                    (Sink, [1.0])):
        got = _view(c).fire(args)
        assert len(got) == c.out_channels(), (
            f"{c.__name__}.fire() returned {len(got)} values for "
            f"{c.out_channels()} output channels")

    # --- readiness --------------------------------------------------------
    # FIRE_ALL waits for every input; FIRE_ANY needs one. A type with no
    # inputs is vacuously ready under either, which is what makes Source
    # fire with no special case anywhere.
    assert Sink.FIRE_RULE == Entity.FIRE_ANY, "a sink fires on any input"
    for c in (Swap, Shear, Mac, Source):
        assert c.FIRE_RULE == Entity.FIRE_ALL, c.__name__

    v = _view(Shear)
    assert not v.inputs_ready(), "nothing fed yet"
    v.set_input(0, 1.0)
    assert not v.inputs_ready(), "FIRE_ALL needs every input"
    v.set_input(1, 2.0)
    assert v.inputs_ready()
    assert v.in_value(0) == 1.0 and v.in_value(1) == 2.0
    v.clear_input(0)
    assert not v.inputs_ready()

    v = _view(Sink)
    assert not v.inputs_ready()
    v.set_input(0, 5.0)
    assert v.inputs_ready(), "FIRE_ANY needs only one"

    assert _view(Source).inputs_ready(), "no inputs is vacuously ready"

    # --- back pressure ----------------------------------------------------
    # A gate with anything still in a waiting zone is blocked. That is the
    # whole back-pressure mechanism: see core/tick.py.
    v = _view(Shear)
    assert v.outputs_clear()
    v.set_output(1, 9.0)
    assert not v.outputs_clear()
    assert v.out_held(1) and v.out_value(1) == 9.0
    assert not v.out_held(0)
    v.clear_output(1)
    assert v.outputs_clear()
    # A type with no outputs is always clear, so a sink never self-blocks.
    assert _view(Sink).outputs_clear()
    assert Entity.view_for(cell.TYPE_BELT) is Belt
    assert Entity.view_for(999) is Entity
    for c in (Belt,) + STRUCTURES:
        assert c.TYPE_ID <= cell.MAX_TYPE
        assert Entity.view_for(c.TYPE_ID) is c, c.__name__
        assert c.TYPE_ID in cell.TYPE_NAMES, c.__name__

    # --- properties every structure must have, whatever its shape ---------
    for c in STRUCTURES:
        w, h = c.FOOTPRINT
        authored = {(lx, ly) for lx in range(w) for ly in range(h)}
        assert c.ROTATABLE, c.__name__

        # Rotation swaps the extent of a non-square footprint, leaves a
        # square one alone, and never changes the cell count.
        assert c.footprint_for(cell.DIR_PX) == (w, h), c.__name__
        assert c.footprint_for(cell.DIR_PY) == (h, w), c.__name__
        assert c.footprint_for(cell.DIR_NX) == (w, h), c.__name__

        for d in range(4):
            cells = c.cells_for(5, 5, d)
            # The origin is always the cell under the cursor, at every
            # rotation, and the footprint never doubles up on a cell.
            assert (5, 5) in cells, (c.__name__, d)
            assert len(cells) == w * h, (c.__name__, d)
            assert len(set(cells)) == w * h, (c.__name__, d)

            # Faces land on cells the entity actually occupies, and rotate
            # with it. A face is never both an input and an output of the
            # same cell.
            occupied = set(cells)
            ins, outs = c.faces_for(5, 5, d)
            assert len(ins) == len(c.INPUTS), (c.__name__, d)
            assert len(outs) == len(c.OUTPUTS), (c.__name__, d)
            for (xy, _f) in ins + outs:
                assert xy in occupied, (c.__name__, d, xy)
            assert not (set(ins) & set(outs)), (c.__name__, d)

        # Ports are authored on real cells of the footprint, and the per-cell
        # masks must account for every one of them -- this is the invariant
        # the GLSL side mirrors.
        total_in = total_out = 0
        for (lx, ly) in authored:
            im, om = c.face_masks_at(lx, ly)
            assert not (im & om), (c.__name__, (lx, ly), "face is in and out")
            total_in += bin(im).count("1")
            total_out += bin(om).count("1")
        assert total_in == len(c.INPUTS), c.__name__
        assert total_out == len(c.OUTPUTS), c.__name__
        for (lx, ly, _f) in c.INPUTS + c.OUTPUTS:
            assert (lx, ly) in authored, (c.__name__, (lx, ly))

    # --- the specifics each machine is defined by -------------------------
    # Swap: x passes through the whole 1x2 body, in at the top, out at the
    # bottom; y and z cross their own cell west to east.
    assert Swap.FOOTPRINT == (1, 2)
    assert Swap.face_masks_at(0, 1) == ((1 << cell.DIR_PY) | (1 << cell.DIR_NX),
                                        1 << cell.DIR_PX)
    assert Swap.face_masks_at(0, 0) == (1 << cell.DIR_NX,
                                        (1 << cell.DIR_NY) | (1 << cell.DIR_PX))

    # Shear: x in and out of the first cell (a pass-through on one cell), the
    # mutated channel entering the first cell and leaving the second.
    assert Shear.FOOTPRINT == (2, 1)
    assert Shear.face_masks_at(0, 0) == ((1 << cell.DIR_PY) | (1 << cell.DIR_NX),
                                         1 << cell.DIR_NY)
    assert Shear.face_masks_at(1, 0) == (0, 1 << cell.DIR_PX)

    # Mac: Shear plus a second vertical control channel on the far cell.
    assert Mac.FOOTPRINT == (2, 1)
    assert Mac.face_masks_at(0, 0) == ((1 << cell.DIR_PY) | (1 << cell.DIR_NX),
                                       1 << cell.DIR_NY)
    assert Mac.face_masks_at(1, 0) == (1 << cell.DIR_PY,
                                       (1 << cell.DIR_NY) | (1 << cell.DIR_PX))

    # Source: one output, on the +X tip of a 4x1 and nowhere else.
    assert Source.FOOTPRINT == (4, 1)
    assert not Source.INPUTS and len(Source.OUTPUTS) == 1
    assert Source.face_masks_at(3, 0) == (0, 1 << cell.DIR_PX)
    for lx in range(3):
        assert Source.face_masks_at(lx, 0) == (0, 0), lx

    # Sink: a 2x2 accepting on every perimeter face, and no outputs. Each
    # cell carries exactly the two faces that point out of the footprint.
    assert Sink.FOOTPRINT == (2, 2)
    assert not Sink.OUTPUTS and len(Sink.INPUTS) == 8
    for (lx, ly) in ((0, 0), (1, 0), (0, 1), (1, 1)):
        in_mask, out_mask = Sink.face_masks_at(lx, ly)
        assert out_mask == 0, (lx, ly)
        # Two ports per cell, and both must face away from the other cells.
        outward = {cell.DIR_NX if lx == 0 else cell.DIR_PX,
                   cell.DIR_NY if ly == 0 else cell.DIR_PY}
        assert in_mask == sum(1 << d for d in outward), (lx, ly)
    # Square, so its extent survives rotation.
    for d in range(4):
        assert Sink.footprint_for(d) == (2, 2), d
    # Every perimeter face of the 2x2 is covered exactly once.
    ins, _ = Sink.faces_for(0, 0, cell.DIR_PX)
    assert len(set(ins)) == 8, ins

    # --- orientation: cells and faces must transform together -------------
    # place_local() is a bijection onto the placed bounding box and
    # unplace_local() inverts it, at every orientation. This is what the
    # renderer relies on to turn a .y offset back into an authored one; when
    # it was missing, a rotated Sink put its ports on interior edges.
    for c in STRUCTURES:
        w, h = c.FOOTPRINT
        for d in range(4):
            for fl in (False, True):
                placed = {}
                for lx in range(w):
                    for ly in range(h):
                        p = c.place_local(lx, ly, d, fl)
                        placed[p] = (lx, ly)
                        assert c.unplace_local(*p, d, fl) == (lx, ly), (
                            c.__name__, d, fl, (lx, ly), p)
                ew, eh = c.footprint_for(d)
                assert set(placed) == {(a, b) for a in range(ew)
                                       for b in range(eh)}, (c.__name__, d, fl)

                # Every port lands on a cell the entity actually occupies --
                # never off the footprint, never on an interior edge.
                occupied = set(c.cells_for(5, 5, d, fl))
                ins, outs = c.faces_for(5, 5, d, fl)
                for (xy, _f) in ins + outs:
                    assert xy in occupied, (c.__name__, d, fl, xy)
                assert len(ins) == len(c.INPUTS)
                assert len(outs) == len(c.OUTPUTS)

    # A flip is an involution, and never changes which cells are covered.
    for c in STRUCTURES:
        for d in range(4):
            for lx in range(c.FOOTPRINT[0]):
                for ly in range(c.FOOTPRINT[1]):
                    once = c.place_local(lx, ly, d, True)
                    assert c.unplace_local(*once, d, True) == (lx, ly)
            assert (sorted(c.cells_for(3, 3, d, False))
                    == sorted(c.cells_for(3, 3, d, True))), c.__name__

    # Structures are placed centred on the cursor, so the cursor cell must
    # always land inside the footprint and near its middle -- otherwise a
    # rotation would swing the body around the pointer instead of turning
    # about it.
    _fracs = [i / 8 for i in range(9)]
    for c in STRUCTURES:
        for d in range(4):
            w, h = c.footprint_for(d)
            for fx in _fracs:
                for fy in _fracs:
                    ox, oy = c.origin_for_cursor(20, 20, d, (fx, fy))
                    # The pointer must never fall outside what it is placing.
                    assert (20, 20) in c.cells_for(ox, oy, d), (
                        c.__name__, d, (fx, fy))
                    # ...and the footprint's midline must track the pointer to
                    # within half a cell, which is the lattice's resolution.
                    assert abs(ox + w / 2.0 - (20 + fx)) <= 0.5 + 1e-6, (
                        c.__name__, d, (fx, fy))
                    assert abs(oy + h / 2.0 - (20 + fy)) <= 0.5 + 1e-6, (
                        c.__name__, d, (fx, fy))

            # An EVEN axis has no middle cell, so the sub-cell position picks
            # which boundary the midline lands on: the two halves of the cell
            # must give different origins. An ODD axis has a middle cell and
            # must ignore the fraction entirely.
            lo_x = c.origin_for_cursor(20, 20, d, (0.25, 0.5))[0]
            hi_x = c.origin_for_cursor(20, 20, d, (0.75, 0.5))[0]
            assert (lo_x != hi_x) == (w % 2 == 0), (c.__name__, d, "x")
            lo_y = c.origin_for_cursor(20, 20, d, (0.5, 0.25))[1]
            hi_y = c.origin_for_cursor(20, 20, d, (0.5, 0.75))[1]
            assert (lo_y != hi_y) == (h % 2 == 0), (c.__name__, d, "y")

        # An odd square sits exactly on the cursor at every rotation.
        if c.FOOTPRINT[0] == c.FOOTPRINT[1] and c.FOOTPRINT[0] % 2 == 1:
            for d in range(4):
                off = c.centre_offset(d)
                assert off == (c.FOOTPRINT[0] // 2,) * 2, (c.__name__, d)

    # A declared pass-through axis must have an output face pointing that
    # way, or the overlay would draw a channel the machine cannot carry.
    for c in STRUCTURES:
        out_faces = {f for _c, f in c.faces_for(0, 0, cell.DIR_PX)[1]}
        for f in c.PASS_THROUGH:
            assert f in out_faces, (c.__name__, cell.DIR_NAMES[f])
    assert Swap.PASS_THROUGH and Shear.PASS_THROUGH and Mac.PASS_THROUGH
    assert not Source.PASS_THROUGH and not Sink.PASS_THROUGH

    # Chirality is derived from the ports, so it cannot drift from them.
    # The arithmetic machines are chiral; Source and Sink are symmetric
    # about the authored X axis and so have no second version.
    assert Swap.is_chiral() and Shear.is_chiral() and Mac.is_chiral()
    assert not Source.is_chiral() and not Sink.is_chiral()
    # An achiral type's flipped placement is identical to its plain one.
    for c in (Source, Sink):
        for d in range(4):
            assert (sorted(c.faces_for(0, 0, d, False)[0])
                    == sorted(c.faces_for(0, 0, d, True)[0])), c.__name__

    assert Belt.ROTATABLE is False


_self_check()
