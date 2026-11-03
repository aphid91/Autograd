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

import cell

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
_COMMON_FIELDS = 7

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

    # Whether R rotates this type at all. Belts take their facing from the
    # path they are drawn along, so rotating one in isolation is meaningless.
    ROTATABLE = False

    # Exposed for Gameboard, which pokes these without building a view.
    F_ALIVE = _F_ALIVE
    F_TYPE = _F_TYPE
    F_W = _F_W
    F_H = _F_H

    @classmethod
    def footprint_for(cls, direction):
        """(w, h) of this type rotated into `direction`."""
        w, h = cls.FOOTPRINT
        return (h, w) if direction % 2 else (w, h)

    @classmethod
    def cells_for(cls, x, y, direction):
        """Every (cx, cy) a placement at (x, y) facing `direction` would cover.

        The origin stays at (x, y) and the footprint extends in +X/+Y from it,
        so a rotated 2x1 occupies (x,y) and (x,y+1) rather than pivoting about
        some interior point. That keeps "the cell under the cursor is the
        origin" true at every rotation, which is what makes placement
        predictable.
        """
        w, h = cls.footprint_for(direction)
        return [(x + lx, y + ly) for lx in range(w) for ly in range(h)]

    @classmethod
    def faces_for(cls, x, y, direction):
        """(inputs, outputs) in world space for a placement.

        Each entry is ((cell_x, cell_y), face_direction). Faces are derived
        from the type and its facing rather than stored per-instance -- every
        instance of a type has the same port layout, so storage would be pure
        duplication. If ports ever become per-instance configurable this is
        where that changes.
        """
        def place(spec):
            out = []
            for (lx, ly, face) in spec:
                rx, ry = cls._rotate_local(lx, ly, direction)
                out.append(((x + rx, y + ry), cell.rotate_dir_ccw_n(face, direction)))
            return out

        return place(cls.INPUTS), place(cls.OUTPUTS)

    @classmethod
    def _rotate_local(cls, lx, ly, direction):
        """Rotate a local offset, then shift it back into the +X/+Y quadrant.

        Rotating (1,0) by a quarter turn gives (0,1) but rotating (0,0)..(1,0)
        as a *block* gives offsets that can go negative; normalising against
        the rotated footprint keeps every offset non-negative so it still packs
        into .y's unsigned local field.
        """
        rx, ry = cell.rotate_offset(lx, ly, direction)
        w, h = cls.FOOTPRINT
        # The bounding box corners, rotated, give the shift needed.
        corners = [cell.rotate_offset(cx, cy, direction)
                   for cx in (0, w - 1) for cy in (0, h - 1)]
        min_x = min(c[0] for c in corners)
        min_y = min(c[1] for c in corners)
        return rx - min_x, ry - min_y

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

    def initialise(self, x, y, direction, is_ghost=False):
        """Populate a freshly claimed row. Subclasses extend, calling super()."""
        # Store the *rotated* extent, so footprint_w/h describe the entity as
        # it actually sits on the board.
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

    def initialise(self, x, y, direction, is_ghost=False, throughput=1.0):
        super().initialise(x, y, direction, is_ghost=is_ghost)
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


class Add(Structure):
    """2x1 splitter-like structure: two inputs on one long edge, two outputs
    on the other.

    Authored at DIR_PX as two cells side by side in X, inputs facing +Y (top)
    and outputs facing -Y (bottom). Rotating makes it 1x2 with the faces
    carried around accordingly.
    """

    TYPE_ID = cell.TYPE_ADD
    FOOTPRINT = (2, 1)

    INPUTS = ((0, 0, cell.DIR_PY), (1, 0, cell.DIR_PY))
    OUTPUTS = ((0, 0, cell.DIR_NY), (1, 0, cell.DIR_NY))


class Mul(Structure):
    """1x1 structure with two adjacent inputs and two adjacent outputs.

    Authored at DIR_PX with inputs on the north and west faces and outputs on
    the east and south -- so material turns a corner through it.
    """

    TYPE_ID = cell.TYPE_MUL
    FOOTPRINT = (1, 1)

    INPUTS = ((0, 0, cell.DIR_PY), (0, 0, cell.DIR_NX))
    OUTPUTS = ((0, 0, cell.DIR_PX), (0, 0, cell.DIR_NY))


def _self_check():
    """The field layout must fit the stride, and ids must be unique."""
    assert _COMMON_FIELDS <= ENTITY_STRIDE
    assert Belt.F_ITEM_COUNT < ENTITY_STRIDE, "belt fields overflow the stride"
    assert Entity.view_for(cell.TYPE_BELT) is Belt
    assert Entity.view_for(cell.TYPE_ADD) is Add
    assert Entity.view_for(cell.TYPE_MUL) is Mul
    assert Entity.view_for(999) is Entity
    for c in (Belt, Add, Mul):
        assert c.TYPE_ID <= cell.MAX_TYPE

    # Rotation swaps the extent of a non-square footprint and leaves a square
    # one alone.
    assert Add.footprint_for(cell.DIR_PX) == (2, 1)
    assert Add.footprint_for(cell.DIR_PY) == (1, 2)
    assert Add.footprint_for(cell.DIR_NX) == (2, 1)
    assert Mul.footprint_for(cell.DIR_PY) == (1, 1)

    # The origin is always the cell under the cursor, at every rotation.
    for d in range(4):
        assert (5, 5) in Add.cells_for(5, 5, d), d
        assert len(Add.cells_for(5, 5, d)) == 2, d
        assert len(set(Add.cells_for(5, 5, d))) == 2, "footprint cells must differ"
        assert Mul.cells_for(5, 5, d) == [(5, 5)], d

    # Faces land on cells the entity actually occupies, and rotate with it.
    for d in range(4):
        occupied = set(Add.cells_for(7, 7, d))
        ins, outs = Add.faces_for(7, 7, d)
        assert len(ins) == 2 and len(outs) == 2, d
        for (c, _f) in ins + outs:
            assert c in occupied, (d, c, occupied)
        # Inputs and outputs always point in opposite directions for an Add.
        assert {f for _c, f in ins} == {cell.rotate_dir_ccw_n(cell.DIR_PY, d)}
        assert {f for _c, f in outs} == {cell.rotate_dir_ccw_n(cell.DIR_NY, d)}

    # Mul's ports are adjacent (perpendicular), not opposed.
    ins, outs = Mul.faces_for(0, 0, cell.DIR_PX)
    in_dirs = {f for _c, f in ins}
    out_dirs = {f for _c, f in outs}
    assert in_dirs == {cell.DIR_PY, cell.DIR_NX}, in_dirs
    assert out_dirs == {cell.DIR_PX, cell.DIR_NY}, out_dirs
    assert not (in_dirs & out_dirs), "a face cannot be both input and output"

    assert Belt.ROTATABLE is False and Add.ROTATABLE and Mul.ROTATABLE


_self_check()
