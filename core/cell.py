"""Grid cell encoding.

Each grid cell is 4 int32 channels in a texture. The packing of channel .x is
duplicated in GLSL at the top of world.frag -- KEEP THE TWO IN SYNC.

    .x  bits 0-1    direction   0=+X 1=+Y 2=-X 3=-Y
        bits 2-8    type        0=empty, 1=belt, 2.. structures  (7 bits)
        bits 9-30   entity id   row index into the entity SSBO   (22 bits)
        bit  31     unused -- deliberately left clear, see below
    .y              packed (local_x, local_y) for multi-cell entities
    .z  bits 0-1    ghost state  0=real, 1=ghost, 2=frustrated
        bit  2       flipped: entity mirrored about its authored X axis
        bit  3       underground end is an exit (not an entrance)
        bit  4       underground end is paired with a partner
        bit  5       belt cell is seeded (injects an item on every rebuild)
        bit  6       that seed's value is positive (for the cell's tint)
        bits 7-31   reserved
    .w  bits 0-15    offset along the belt segment, from its tail
        bits 16-30   segment id (0 = not on a segment)
        bit  31      unused -- deliberately clear, as in .x

GHOST_FRUSTRATED is an overlay, not an entity. A frustrated cell is one a
pending build wants but cannot have, which means something is already there --
and .x holds exactly one entity id, so a ghost entity cannot be spawned on top
without destroying the occupant. Frustration therefore lives only in .z and
leaves .x untouched, which is precisely what lets the renderer tint the cell
red *over* the existing entity. See Gameboard.set_frustrated().

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

`.w` carries belt-segment membership: which segment a belt cell belongs to,
and how far along that segment it sits. Both live in the cell rather than
being looked up because the fragment shader needs them for a cell it has
*already* fetched -- deciding whether an item disc covers this pixel costs no
extra texture reads. The same 31-bit discipline applies: the segment id is 15
bits, so bit 31 stays clear.

Segment membership is **derived state**, not authored: core/segments.py
rebuilds it from the grid whenever belts change. It is still stored in the
grid (rather than recomputed per frame) because the renderer cannot afford to
walk a run per pixel.
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


def rotate_dir(direction, cw=True):
    """Rotate a direction 90 degrees. Directions run CCW: +X, +Y, -X, -Y."""
    return (direction - 1 if cw else direction + 1) % 4


def opposite_dir(direction):
    """The reverse of a direction. Two quarter-turns, so `cw` is irrelevant."""
    return (direction + 2) % 4


def rotate_dir_ccw_n(direction, turns):
    """Rotate a direction CCW by `turns` quarter-turns.

    Used to carry a type's authored face directions into its actual facing;
    `turns` is itself a direction constant, since DIR_PX==0 means "no rotation"
    and each successive direction is one more CCW quarter-turn.
    """
    return (direction + turns) % 4


def flip_dir_y(direction):
    """Mirror a direction about the X axis: +Y <-> -Y, +X and -X unchanged.

    This is the face half of a structure flip. Entities are authored at DIR_PX
    and flipped about the authored X axis *before* being rotated, so a chiral
    machine has a mirrored twin at every facing.
    """
    if direction == DIR_PY:
        return DIR_NY
    if direction == DIR_NY:
        return DIR_PY
    return direction


def rotate_offset(lx, ly, direction):
    """Rotate an entity-local offset into a facing.

    Local space is authored at DIR_PX (east) and rotated CCW by `direction`
    quarter-turns, matching rotate_dir()'s convention so a footprint and its
    faces stay consistent under rotation.
    """
    for _ in range(direction % 4):
        lx, ly = -ly, lx
    return lx, ly


# --- entity types -------------------------------------------------------
TYPE_EMPTY = 0
TYPE_BELT = 1
TYPE_SWAP = 2    # conditional swap: x,y,z -> x, (z,y if x>0 else y,z)
TYPE_SHEAR = 3   # x,y -> x, x+y
TYPE_MAC = 4     # x,y,z -> x, y, z + x*y
TYPE_SOURCE = 5  # emits from one tip
TYPE_SINK = 6    # consumes on all four sides
TYPE_UNDER = 7   # underground belt end (entrance or exit -- see .y bit)
TYPE_RECIP = 8   # a -> 1/a
TYPE_MAX = 9     # a,b -> max, max
TYPE_FLOOR = 10  # a -> floor(a)
TYPE_ROUTE = 11  # a,b -> a down one of two paths, chosen by b
TYPE_MERGE = 12  # a,b,c -> whichever of a,b the control c selects
# 13..127 reserved for future structures.
#
# **Ids are permanent.** They are baked into every saved board's grid words,
# so a type that is hidden from the toolbelt keeps its id and its class (see
# Swap, which is no longer placeable but still loads). Renumbering would
# silently reinterpret old saves.

TYPE_NAMES = {
    TYPE_EMPTY: "empty",
    TYPE_BELT: "belt",
    TYPE_SWAP: "swap",
    TYPE_SHEAR: "shear",
    TYPE_MAC: "mac",
    TYPE_SOURCE: "source",
    TYPE_SINK: "sink",
    TYPE_RECIP: "recip",
    TYPE_MAX: "max",
    TYPE_FLOOR: "floor",
    TYPE_ROUTE: "route",
    TYPE_MERGE: "merge",
    # Keep these short and identifier-safe: glsl_types.py upper-cases them
    # into the GLSL type constants (TYPE_UNDER, TYPE_SHEAR, ...), so a name
    # with a space or a hyphen would not compile.
    TYPE_UNDER: "under",
}

# --- bit field layout ---------------------------------------------------
DIR_SHIFT, DIR_BITS = 0, 2
TYPE_SHIFT, TYPE_BITS = 2, 7
ID_SHIFT, ID_BITS = 9, 22  # 22 not 23: keeps bit 31 (int32 sign) clear

DIR_MASK = (1 << DIR_BITS) - 1
TYPE_MASK = (1 << TYPE_BITS) - 1
ID_MASK = (1 << ID_BITS) - 1

MAX_TYPE = TYPE_MASK
MAX_ENTITY_ID = ID_MASK

# .z bits 0-1: ghost state. Frustrated is an overlay on an occupied cell and
# never clears the occupant's id in .x -- see the module docstring.
GHOST_SHIFT, GHOST_BITS = 0, 2
GHOST_MASK = (1 << GHOST_BITS) - 1

# .z bit 2: the entity is mirrored about its authored X axis. This lives in
# the cell rather than only in the entity row because the renderer resolves
# ports per-cell straight from the grid texture, and must transform the cell's
# local offset the same way the placement did.
FLIP_SHIFT = 2
FLIP_BIT = 1 << FLIP_SHIFT

# .z bit 3: this underground end is an exit (surfacing) rather than an
# entrance (diving). .z bit 4: it is paired with a partner end. Both live in
# the cell because the renderer draws the funnel and the dotted span straight
# from the grid texture, with no entity-row lookup.
UNDER_EXIT_SHIFT = 3
UNDER_EXIT_BIT = 1 << UNDER_EXIT_SHIFT
UNDER_PAIRED_SHIFT = 4
UNDER_PAIRED_BIT = 1 << UNDER_PAIRED_SHIFT

# .z bit 5: this belt cell is seeded -- it injects an item onto its run
# whenever segments are re-derived (see Belt.F_SEED_ON). Bit 6: the *sign* of
# that seed's value, so the renderer can tint the cell green or red and the
# player can see at a glance where the seeds are and roughly what they carry.
#
# The sign rather than the value, because a cell flag has no room for a float
# and sign is what the item discs themselves already report. Positive is
# green, zero-or-below red -- the same split as COL_ITEM_POS / COL_ITEM_NEG,
# so a seeded cell and the item it produces agree on colour.
SEED_SHIFT = 5
SEED_BIT = 1 << SEED_SHIFT
SEED_POS_SHIFT = 6
SEED_POS_BIT = 1 << SEED_POS_SHIFT

GHOST_NONE = 0
GHOST_PENDING = 1      # a ghost entity of our own, in a free cell
GHOST_FRUSTRATED = 2   # a cell we wanted but cannot have; .x is someone else's

GHOST_NAMES = {
    GHOST_NONE: "real",
    GHOST_PENDING: "ghost",
    GHOST_FRUSTRATED: "frustrated",
}

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


# --- .w: belt segment membership ---------------------------------------
# Which segment a belt cell belongs to, and its distance from that segment's
# tail (the upstream end, where items enter). Derived state, rebuilt by
# core/segments.py; stored in the cell so the fragment shader gets it from a
# texel it has already fetched.
SEG_OFF_SHIFT, SEG_OFF_BITS = 0, 16
SEG_ID_SHIFT, SEG_ID_BITS = 16, 15   # 15 not 16: keeps bit 31 (sign) clear

SEG_OFF_MASK = (1 << SEG_OFF_BITS) - 1
SEG_ID_MASK = (1 << SEG_ID_BITS) - 1

#: Highest segment id a cell can name. Segment 0 is "no segment", mirroring
#: entity row 0 being the null entity.
MAX_SEGMENT_ID = SEG_ID_MASK
#: Longest run a single segment may cover, since every cell must be able to
#: record its own offset.
MAX_SEGMENT_LEN = SEG_OFF_MASK + 1


def pack_segment(segment_id=0, offset=0):
    """Pack channel .w. Segment 0 packs to exactly 0 -- "not on a segment"."""
    if segment_id == 0:
        return 0
    if not 0 <= segment_id <= MAX_SEGMENT_ID:
        raise ValueError(f"segment id {segment_id} exceeds {MAX_SEGMENT_ID}")
    if not 0 <= offset <= SEG_OFF_MASK:
        raise ValueError(f"segment offset {offset} exceeds {SEG_OFF_MASK}")
    return (segment_id << SEG_ID_SHIFT) | (offset << SEG_OFF_SHIFT)


def unpack_segment(word):
    """(segment_id, offset) from a channel .w word."""
    word = int(word)
    return ((word >> SEG_ID_SHIFT) & SEG_ID_MASK,
            (word >> SEG_OFF_SHIFT) & SEG_OFF_MASK)


def segment_id(word):
    return (int(word) >> SEG_ID_SHIFT) & SEG_ID_MASK


def segment_offset(word):
    return (int(word) >> SEG_OFF_SHIFT) & SEG_OFF_MASK


def segment_id_arr(words):
    return np.right_shift(words, SEG_ID_SHIFT) & SEG_ID_MASK


# --- .z: ghost state ----------------------------------------------------
def pack_flags(ghost_state=GHOST_NONE, flipped=False, under_exit=False,
               under_paired=False, seeded=False, seed_positive=False):
    if not 0 <= ghost_state <= GHOST_MASK:
        raise ValueError(f"bad ghost state {ghost_state}")
    return ((ghost_state << GHOST_SHIFT)
            | (FLIP_BIT if flipped else 0)
            | (UNDER_EXIT_BIT if under_exit else 0)
            | (UNDER_PAIRED_BIT if under_paired else 0)
            | (SEED_BIT if seeded else 0)
            | (SEED_POS_BIT if seed_positive else 0))


def is_flipped(word):
    return bool(int(word) & FLIP_BIT)


def is_under_exit(word):
    return bool(int(word) & UNDER_EXIT_BIT)


def is_under_paired(word):
    return bool(int(word) & UNDER_PAIRED_BIT)


def is_seeded(word):
    return bool(int(word) & SEED_BIT)


def is_seed_positive(word):
    return bool(int(word) & SEED_POS_BIT)


def ghost_state(word):
    return (int(word) >> GHOST_SHIFT) & GHOST_MASK


def is_ghost(word):
    """True for a pending ghost entity. Frustration is NOT a ghost entity."""
    return ghost_state(word) == GHOST_PENDING


def is_frustrated(word):
    return ghost_state(word) == GHOST_FRUSTRATED


def with_ghost_state(word, state):
    """Replace the ghost field of a .z word, preserving the reserved bits."""
    return (int(word) & ~(GHOST_MASK << GHOST_SHIFT)) | pack_flags(state)


def ghost_state_arr(words):
    return np.right_shift(words, GHOST_SHIFT) & GHOST_MASK


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

    assert is_ghost(pack_flags(GHOST_PENDING))
    assert not is_ghost(pack_flags(GHOST_NONE))
    assert is_frustrated(pack_flags(GHOST_FRUSTRATED))
    assert not is_ghost(pack_flags(GHOST_FRUSTRATED)), "frustrated is not a ghost"
    for s in (GHOST_NONE, GHOST_PENDING, GHOST_FRUSTRATED):
        assert ghost_state(pack_flags(s)) == s
        # Replacing the ghost field must leave every other bit alone --
        # including the flip bit, which shares .z.
        assert ghost_state(with_ghost_state(0xFF00 | 0x3, s)) == s
        assert (with_ghost_state(0xFF00, s) >> 5) == (0xFF00 >> 5)
        assert is_flipped(with_ghost_state(pack_flags(s, True), s))
        assert not is_flipped(with_ghost_state(pack_flags(s, False), s))

    # Every .z flag is independent of the others and of the ghost field --
    # with_ghost_state() in particular must not disturb any of them.
    import itertools
    for s in (GHOST_NONE, GHOST_PENDING, GHOST_FRUSTRATED):
        for (fl, ex, pr, sd, sp) in itertools.product((False, True),
                                                      repeat=5):
            args = (s, fl, ex, pr, sd, sp)
            w = pack_flags(*args)
            assert ghost_state(w) == s, args
            assert is_flipped(w) == fl, args
            assert is_under_exit(w) == ex, args
            assert is_under_paired(w) == pr, args
            assert is_seeded(w) == sd, args
            assert is_seed_positive(w) == sp, args
            # Rewriting the ghost field preserves the rest.
            w2 = with_ghost_state(w, GHOST_FRUSTRATED)
            assert is_flipped(w2) == fl
            assert is_under_exit(w2) == ex
            assert is_under_paired(w2) == pr
            assert is_seeded(w2) == sd
            assert is_seed_positive(w2) == sp

    # The seed flags occupy their own bits, so neither can be mistaken for
    # an underground's -- these two pairs share .z and nothing else.
    assert SEED_BIT != UNDER_PAIRED_BIT and SEED_BIT != UNDER_EXIT_BIT
    assert not is_under_paired(pack_flags(seeded=True))
    assert not is_seeded(pack_flags(under_paired=True))

    assert step_to_dir(1, 0) == DIR_PX and step_to_dir(0, -1) == DIR_NY

    # Rotation: four quarter-turns is identity, and cw/ccw are inverses.
    for d in range(4):
        assert rotate_dir(rotate_dir(rotate_dir(rotate_dir(d)))) == d
        assert rotate_dir(rotate_dir(d, cw=True), cw=False) == d
    assert rotate_dir(DIR_PX, cw=False) == DIR_PY, "ccw from east is north"
    assert rotate_dir(DIR_PX, cw=True) == DIR_NY, "cw from east is south"

    # Offsets rotate with the same convention as directions: rotating a unit
    # step by d must give the same result as rotating the direction itself.
    for d in range(4):
        assert rotate_offset(1, 0, d) == DIR_STEPS[d], d
    assert rotate_offset(2, 1, DIR_PX) == (2, 1)
    assert rotate_offset(1, 0, DIR_PY) == (0, 1)

    words = np.array([EMPTY_WORD, pack_cell(TYPE_BELT, DIR_PX, 5)], dtype=np.int32)
    assert list(is_empty_arr(words)) == [True, False]

    # --- .w: segment membership ---
    assert pack_segment(0, 0) == 0
    assert pack_segment(0, 7) == 0, "no segment means no offset either"
    assert unpack_segment(0) == (0, 0)
    for sid in (1, 2, 999, MAX_SEGMENT_ID):
        for off in (0, 1, 5, SEG_OFF_MASK):
            w = pack_segment(sid, off)
            assert unpack_segment(w) == (sid, off), (sid, off, w)
            assert segment_id(w) == sid
            assert segment_offset(w) == off
            # Same int32-sign discipline as .x: never let bit 31 be set.
            assert w > 0 and w <= 0x7FFFFFFF, (sid, off, w)
            assert int(np.array([w], dtype=np.int32)[0]) == w, (sid, off)
    # The id and offset fields must not bleed into one another.
    assert segment_offset(pack_segment(MAX_SEGMENT_ID, 0)) == 0
    assert segment_id(pack_segment(1, SEG_OFF_MASK)) == 1
    segs = np.array([0, pack_segment(3, 2), pack_segment(1, 0)], dtype=np.int32)
    assert list(segment_id_arr(segs)) == [0, 3, 1]


_self_check()
