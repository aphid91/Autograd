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
ENTITY_STRIDE = 12


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

    # Exposed for Gameboard, which pokes these without building a view.
    F_ALIVE = _F_ALIVE
    F_TYPE = _F_TYPE
    F_W = _F_W
    F_H = _F_H
    F_FLIPPED = _F_FLIPPED

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

    # Belt-specific fields, after the common prefix. Unused this pass --
    # item simulation is deferred -- but the slots are reserved so adding
    # it later does not reshuffle the row.
    F_THROUGHPUT = _COMMON_FIELDS + 0
    F_ITEM_COUNT = _COMMON_FIELDS + 1

    def initialise(self, x, y, direction, is_ghost=False, flipped=False,
                   throughput=1.0):
        # A belt is 1x1 with no ports, so `flipped` is meaningless for it --
        # accepted and passed through only so every entity type takes the
        # same orientation arguments and callers need no special case.
        super().initialise(x, y, direction, is_ghost=is_ghost, flipped=flipped)
        self._set(self.F_THROUGHPUT, float(throughput))

    @property
    def throughput(self):
        return float(self._get(self.F_THROUGHPUT))


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


class Shear(Structure):
    """Shear: x,y -> x, x+y.

    Authored at DIR_PX as a 2x1 (wide) body. x passes straight down through
    the first cell only -- one cell in, one cell out, unchanged. y enters the
    west face of that same first cell, travels the long way across both cells,
    and leaves the east face of the second as x+y.
    """

    TYPE_ID = cell.TYPE_SHEAR
    FOOTPRINT = (2, 1)

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


class Mac(Structure):
    """Multiply-accumulate: x,y,z -> x, y, z + x*y.

    Authored at DIR_PX as a 2x1 (wide) body, like Shear but with a second
    vertical control channel: x passes down through the first cell and y down
    through the second, both unchanged. z crosses the long way and picks up
    the product.
    """

    TYPE_ID = cell.TYPE_MAC
    FOOTPRINT = (2, 1)

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


class Source(Structure):
    """4x1 emitter with a single output at one short end.

    Authored at DIR_PX lying along X with the port on the +X tip, so the
    default placement emits east and R walks it around.
    """

    TYPE_ID = cell.TYPE_SOURCE
    FOOTPRINT = (4, 1)

    INPUTS = ()
    OUTPUTS = ((3, 0, cell.DIR_PX),)


class Sink(Structure):
    """3x3 consumer with an input at the middle of each side.

    Square, so rotation does not change the footprint and all four faces are
    symmetric -- R is a no-op in practice, but it stays ROTATABLE rather than
    special-casing the toolbelt.
    """

    TYPE_ID = cell.TYPE_SINK
    FOOTPRINT = (3, 3)

    INPUTS = (
        (1, 2, cell.DIR_PY),   # north
        (1, 0, cell.DIR_NY),   # south
        (0, 1, cell.DIR_NX),   # west
        (2, 1, cell.DIR_PX),   # east
    )
    OUTPUTS = ()


#: Every placeable structure, in the order the toolbelt offers them.
STRUCTURES = (Swap, Shear, Mac, Source, Sink)


def _self_check():
    """The field layout must fit the stride, and ids must be unique."""
    assert _COMMON_FIELDS <= ENTITY_STRIDE
    assert Belt.F_ITEM_COUNT < ENTITY_STRIDE, "belt fields overflow the stride"
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

    # Sink: an input at the middle of each side of a 3x3, and no outputs. The
    # four corners and the centre carry no ports at all.
    assert Sink.FOOTPRINT == (3, 3)
    assert not Sink.OUTPUTS and len(Sink.INPUTS) == 4
    for (lx, ly) in ((0, 0), (2, 0), (0, 2), (2, 2), (1, 1)):
        assert Sink.face_masks_at(lx, ly) == (0, 0), (lx, ly)
    # Square, so its extent survives rotation.
    for d in range(4):
        assert Sink.footprint_for(d) == (3, 3), d

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
