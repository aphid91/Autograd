"""Grid cell encoding.

Each grid cell is 4 int32 channels in a texture. The packing of channel .x is
duplicated in GLSL at the top of world.frag -- KEEP THE TWO IN SYNC.

    .x  bits 0-1    direction   0=+X 1=+Y 2=-X 3=-Y
        bits 2-8    type        0=empty, 1=belt, 2.. structures  (7 bits)
        bits 9-30   entity id   row index into the entity SSBO   (22 bits)
        bit  31     unused -- deliberately left clear, see below
    .y              packed (local_x, local_y) for multi-cell entities
    .z  bit  0      is_ghost
        bits 1-31   reserved
    .w              reserved

Emptiness is `type == 0`, not `word == 0` -- a cell could in principle carry
stale direction bits. pack_cell() refuses to produce such a word (type 0 packs
to exactly 0), so in practice both tests agree; EMPTY_WORD is the canonical
empty value.

The id field is 22 bits rather than the 23 the layout has room for, so that
bit 31 -- the int32 sign bit -- is never set. The grid texture is signed
(moderngl 'i4' / GL_RGBA32I), and a cell word that goes negative would both
break Python-side comparisons and need sign-aware unpacking in GLSL. Keeping
every word non-negative is worth one bit of a field that already addresses
4.2M entities.
"""

import numpy as np

# --- directions ---------------------------------------------------------
DIR_PX = 0  # +X, east
DIR_PY = 1  # +Y, north
DIR_NX = 2  # -X, west
DIR_NY = 3  # -Y, south

# Step vectors indexed by direction. y is up.
DIR_STEPS = ((1, 0), (0, 1), (-1, 0), (0, -1))

DIR_NAMES = ("+X", "+Y", "-X", "-Y")


def step_to_dir(dx, dy):
    """Direction constant for a one-cell step. Raises on a non-axis step."""
    for d, (sx, sy) in enumerate(DIR_STEPS):
        if (dx, dy) == (sx, sy):
            return d
    raise ValueError(f"not a unit axis step: {(dx, dy)}")


# --- entity types -------------------------------------------------------
TYPE_EMPTY = 0
TYPE_BELT = 1
# 2..127 reserved for structures.

TYPE_NAMES = {TYPE_EMPTY: "empty", TYPE_BELT: "belt"}

# --- bit field layout ---------------------------------------------------
DIR_SHIFT, DIR_BITS = 0, 2
TYPE_SHIFT, TYPE_BITS = 2, 7
ID_SHIFT, ID_BITS = 9, 22  # 22 not 23: keeps bit 31 (int32 sign) clear

DIR_MASK = (1 << DIR_BITS) - 1
TYPE_MASK = (1 << TYPE_BITS) - 1
ID_MASK = (1 << ID_BITS) - 1

MAX_TYPE = TYPE_MASK
MAX_ENTITY_ID = ID_MASK

GHOST_BIT = 1  # .z bit 0

EMPTY_WORD = 0

# Channel indices into the 4-wide cell vector.
CH_X, CH_Y, CH_Z, CH_W = 0, 1, 2, 3


def pack_cell(entity_type, direction=DIR_PX, entity_id=0):
    """Pack channel .x. Returns EMPTY_WORD for TYPE_EMPTY regardless of args."""
    if entity_type == TYPE_EMPTY:
        return EMPTY_WORD
    if not 0 <= entity_type <= MAX_TYPE:
        raise ValueError(f"type {entity_type} exceeds {MAX_TYPE}")
    if not 0 <= direction <= DIR_MASK:
        raise ValueError(f"bad direction {direction}")
    if not 0 <= entity_id <= MAX_ENTITY_ID:
        raise ValueError(f"entity id {entity_id} exceeds {MAX_ENTITY_ID}")
    return (
        (direction << DIR_SHIFT)
        | (entity_type << TYPE_SHIFT)
        | (entity_id << ID_SHIFT)
    )


def unpack_cell(word):
    """(entity_type, direction, entity_id) from a channel .x word."""
    word = int(word)
    return (
        (word >> TYPE_SHIFT) & TYPE_MASK,
        (word >> DIR_SHIFT) & DIR_MASK,
        (word >> ID_SHIFT) & ID_MASK,
    )


def cell_type(word):
    return (int(word) >> TYPE_SHIFT) & TYPE_MASK


def cell_dir(word):
    return (int(word) >> DIR_SHIFT) & DIR_MASK


def cell_id(word):
    return (int(word) >> ID_SHIFT) & ID_MASK


def is_empty(word):
    return cell_type(word) == TYPE_EMPTY


# --- vectorised forms, for whole-array queries --------------------------
def cell_type_arr(words):
    return np.right_shift(words, TYPE_SHIFT) & TYPE_MASK


def is_empty_arr(words):
    return cell_type_arr(words) == TYPE_EMPTY


# --- .y: multi-cell local offset ---------------------------------------
LOCAL_SHIFT = 16
LOCAL_MASK = 0xFFFF


def pack_local(local_x, local_y):
    """Pack an entity-local cell offset into channel .y."""
    if not (0 <= local_x <= LOCAL_MASK and 0 <= local_y <= LOCAL_MASK):
        raise ValueError(f"local offset out of range: {(local_x, local_y)}")
    return (local_x << LOCAL_SHIFT) | local_y


def unpack_local(word):
    word = int(word)
    return (word >> LOCAL_SHIFT) & LOCAL_MASK, word & LOCAL_MASK


# --- .z: flags ----------------------------------------------------------
def pack_flags(is_ghost=False):
    return GHOST_BIT if is_ghost else 0


def is_ghost(word):
    return bool(int(word) & GHOST_BIT)


def _self_check():
    """Round-trip the packing. Cheap; runs at import."""
    assert pack_cell(TYPE_EMPTY) == EMPTY_WORD
    assert pack_cell(TYPE_EMPTY, DIR_NY, 1234) == EMPTY_WORD, "empty must erase"
    assert is_empty(EMPTY_WORD)

    # Every field at its maximum simultaneously must still fit a positive
    # int32 -- the grid texture is signed, so bit 31 has to stay clear.
    assert pack_cell(MAX_TYPE, DIR_MASK, MAX_ENTITY_ID) <= 0x7FFFFFFF

    for t in (TYPE_BELT, 2, MAX_TYPE):
        for d in range(4):
            for i in (0, 1, 777, MAX_ENTITY_ID):
                w = pack_cell(t, d, i)
                assert unpack_cell(w) == (t, d, i), (t, d, i, w)
                assert not is_empty(w)
                assert w > 0, (t, d, i, w)
                # Must survive a round trip through int32 storage.
                assert int(np.array([w], dtype=np.int32)[0]) == w, (t, d, i)

    for lx, ly in ((0, 0), (1, 2), (LOCAL_MASK, LOCAL_MASK)):
        assert unpack_local(pack_local(lx, ly)) == (lx, ly)

    assert is_ghost(pack_flags(is_ghost=True))
    assert not is_ghost(pack_flags(is_ghost=False))

    assert step_to_dir(1, 0) == DIR_PX and step_to_dir(0, -1) == DIR_NY

    words = np.array([EMPTY_WORD, pack_cell(TYPE_BELT, DIR_PX, 5)], dtype=np.int32)
    assert list(is_empty_arr(words)) == [True, False]


_self_check()
