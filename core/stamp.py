"""Copy-stamps: a captured group of entities, transformable as one unit.

A stamp is what a copy-mode selection produces and what paste mode places. It
holds one `StampEntry` per captured entity -- a type, an offset within the
stamp, and an orientation -- and knows how to rotate or mirror the whole group
while preserving the connections between its members.

**A stamp stores offsets, not board positions.** Entries are relative to the
stamp's own origin, so pasting is a translation and the transforms are closed
over the stamp itself. `bounds()` reports the covered box; `place()` turns the
whole thing into absolute cells for a given paste origin.


Rotation is the easy half
-------------------------

Rotate each entry's offset about the group's centre and rotate its own facing
by the same amount. Both use the existing `cell.rotate_*` helpers, so a stamp
turns exactly as a single structure does and relative positions survive.

The bounding box changes shape on a quarter turn (an NxM stamp becomes MxN),
so the box is re-derived afterwards and offsets are renormalised against it.


Mirroring is the subtle half
----------------------------

A stamp mirror is about a **world** axis. An entity's `flipped` flag is a
mirror about its **authored** X axis, applied *before* rotation
(`Entity.place_local`). Those coincide only when the entity faces +X or -X, so
"just set flipped" is wrong in general -- which is why mirroring some machines
needs a flip *and* a rotation.

The algebra, for a mirror about the world Y axis:

    mirror_Y . rotate(d)  ==  rotate(-d) . mirror_Y
    mirror_Y              ==  mirror_X . rotate(180)

so an entity at `(direction, flipped)` becomes:

    direction' = (2 - direction) % 4
    flipped'   = not flipped          (only if the type is chiral)

The direction map is exact for every type. The flip half is conditional
because `flipped` is a no-op on an achiral type -- toggling it there would
store a difference that does not exist.

For a belt this gives +X <-> -X with +Y and -Y unchanged, which is the
reversal you want: a horizontal belt turns around, a vertical one does not.

`_self_check()` verifies the rule the strong way rather than trusting the
derivation: for every type, rotation and flip it compares the transformed
entity's actual port layout against the mirrored original's, and asserts they
match as sets. A type for which no `(direction, flipped)` pair reproduces the
mirrored layout fails loudly instead of pasting something subtly wrong.

Note the mirror exchanges which *face* carries which *channel* on a machine
whose inputs sit symmetrically about the mirror axis -- Swap's y/z and Mac's
x/y. That is semantically safe only because those operations are invariant
under exchanging that pair (Swap exchanges the two lanes either way; Mac
multiplies them). `tests/test_stamp.py` pins that invariance, because it is an
assumption about arithmetic that geometry cannot check.
"""

from core import cell


class StampEntry:
    """One captured entity: a type, an offset in the stamp, an orientation."""

    __slots__ = ("entity_cls", "dx", "dy", "direction", "flipped",
                 "is_exit", "partner")

    def __init__(self, entity_cls, dx, dy, direction, flipped=False,
                 is_exit=False, partner=None):
        self.entity_cls = entity_cls
        self.dx = dx
        self.dy = dy
        self.direction = direction
        self.flipped = bool(flipped)
        # Underground-only. `partner` is the *index of another entry in this
        # stamp*, or None. Recording it in stamp-local terms is what lets a
        # copied pair re-pair with each other on paste rather than latching
        # onto whatever happens to be near the paste site.
        self.is_exit = bool(is_exit)
        self.partner = partner

    def copy(self):
        return StampEntry(self.entity_cls, self.dx, self.dy, self.direction,
                          self.flipped, self.is_exit, self.partner)

    def cells(self, ox=0, oy=0):
        """Cells this entry covers, offset by a paste origin."""
        return self.entity_cls.cells_for(ox + self.dx, oy + self.dy,
                                         self.direction, self.flipped)

    def __repr__(self):
        m = " flipped" if self.flipped else ""
        return (f"<StampEntry {self.entity_cls.__name__} +({self.dx},{self.dy}) "
                f"{cell.DIR_NAMES[self.direction]}{m}>")


def mirror_orientation(entity_cls, direction, flipped):
    """(direction, flipped) for `entity_cls` mirrored about the world Y axis.

    See the module docstring for the derivation. The flip only toggles for a
    chiral type, since on an achiral one it stores a difference that does not
    exist.
    """
    new_dir = (2 - direction) % 4
    new_flip = (not flipped) if entity_cls.is_chiral() else flipped
    return new_dir, new_flip


class Stamp:
    """A group of entities captured together, transformable as one unit."""

    __slots__ = ("entries",)

    def __init__(self, entries=()):
        self.entries = list(entries)

    def __len__(self):
        return len(self.entries)

    def __bool__(self):
        return bool(self.entries)

    # --- geometry --------------------------------------------------------
    def bounds(self):
        """(min_x, min_y, w, h) of every cell the stamp covers.

        Measured over *cells*, not entry origins: a 2x1 structure at the east
        edge extends the box past its own offset, and the paste cursor centres
        on the box rather than on the origins.
        """
        if not self.entries:
            return (0, 0, 0, 0)
        xs, ys = [], []
        for e in self.entries:
            for (cx, cy) in e.cells():
                xs.append(cx)
                ys.append(cy)
        lo_x, lo_y = min(xs), min(ys)
        return (lo_x, lo_y, max(xs) - lo_x + 1, max(ys) - lo_y + 1)

    def normalise(self):
        """Shift every entry so the covered box starts at (0, 0).

        Called after each transform so a stamp's offsets stay non-negative
        and its box is always anchored at the origin. That keeps `place()` a
        plain translation.
        """
        lo_x, lo_y, _w, _h = self.bounds()
        for e in self.entries:
            e.dx -= lo_x
            e.dy -= lo_y
        return self

    def centre_offset(self):
        """(dx, dy) from the stamp's origin to the cell under the cursor.

        The paste cursor sits in the middle of the full box, so stamping a
        long belt run puts the middle of the run under the pointer. Uses the
        same lower-of-the-two-middles convention as a single structure's
        Entity.centre_offset(), so the two tools feel alike.
        """
        _lo_x, _lo_y, w, h = self.bounds()
        return (w - 1) // 2, (h - 1) // 2

    # --- transforms ------------------------------------------------------
    def rotated(self, backward=False):
        """A copy turned a quarter turn about the group's centre.

        Each entry's occupied box rotates and its facing rotates with it, so
        relative positions and connections survive. The group's box swaps
        extent, so the result is renormalised.

        **A multi-cell entity's origin cannot simply be rotated.** A
        footprint always extends +X/+Y from its origin whatever its facing
        (see Entity.cells_for), so the origin is a *corner*, not a centre.
        Rotating that corner leaves the body hanging off the wrong side: a
        2x2 Sink at the east end of a run, rotated 180 degrees, ends up
        extending back west over the belt that feeds it. So rotate the
        entity's whole cell span and take the new span's lower-left as the
        new origin.
        """
        turns = 3 if backward else 1
        out = Stamp([e.copy() for e in self.entries])
        for e in out.entries:
            for _ in range(turns):
                # Rotate every cell the entity covers, CCW to match
                # cell.rotate_offset(), then re-derive the origin corner.
                pts = [(-cy, cx) for (cx, cy) in e.cells()]
                e.direction = cell.rotate_dir(e.direction, cw=False)
                e.dx = min(px for (px, _py) in pts)
                e.dy = min(py for (_px, py) in pts)
        return out.normalise()

    def mirrored(self):
        """A copy reflected about the stamp's vertical centre line.

        Offsets reflect in x and each entry's orientation is mirrored by
        `mirror_orientation()`. A multi-cell entity's *origin* has to be
        re-derived rather than just negated: the origin is the footprint's
        lower-left corner, so reflecting a 2-wide body about the axis moves
        which of its cells is leftmost.
        """
        out = Stamp([e.copy() for e in self.entries])
        for e in out.entries:
            # Reflect the footprint's cell span, not the bare origin: a
            # w-wide body occupying [dx, dx+w) reflects to (-dx-w, -dx], so
            # its new origin is -(dx + w - 1).
            w, _h = e.entity_cls.footprint_for(e.direction)
            e.dx = -(e.dx + w - 1)
            e.direction, e.flipped = mirror_orientation(
                e.entity_cls, e.direction, e.flipped)
        return out.normalise()

    # --- placement -------------------------------------------------------
    def place(self, ox, oy):
        """[(entry, origin_x, origin_y)] for a paste with the box at (ox, oy)."""
        return [(e, ox + e.dx, oy + e.dy) for e in self.entries]

    def origin_for_cursor(self, cx, cy):
        """Box origin for a paste centred on the cursor cell."""
        off_x, off_y = self.centre_offset()
        return cx - off_x, cy - off_y

    def cells(self, ox=0, oy=0):
        """Every cell the stamp would cover, pasted at (ox, oy)."""
        out = []
        for e in self.entries:
            out.extend(e.cells(ox, oy))
        return out

    def __repr__(self):
        _x, _y, w, h = self.bounds()
        return f"<Stamp {len(self.entries)} entities {w}x{h}>"


class StampHistory:
    """The last `LIMIT` stamps, newest first, with one marked active.

    Factorio keeps a short ring of recent copies and lets shift+scroll walk
    it. Newest-first ordering means index 0 is always the most recent, which
    is what a fresh copy becomes.
    """

    LIMIT = 16

    def __init__(self, limit=None):
        self.limit = limit or self.LIMIT
        self.stamps = []
        self.active_index = 0

    def __len__(self):
        return len(self.stamps)

    def __bool__(self):
        return bool(self.stamps)

    @property
    def active(self):
        if not self.stamps:
            return None
        return self.stamps[self.active_index]

    def push(self, stamp):
        """Add a stamp as the newest, make it active, and trim the tail."""
        self.stamps.insert(0, stamp)
        del self.stamps[self.limit:]
        self.active_index = 0
        return stamp

    def cycle(self, delta):
        """Walk the history. Negative goes older, positive newer.

        Clamps rather than wrapping: the ends of a short history are a
        meaningful place to be, and wrapping from oldest back to newest makes
        it easy to lose track of where you are.
        """
        if not self.stamps:
            return False
        before = self.active_index
        self.active_index = max(
            0, min(len(self.stamps) - 1, self.active_index - delta))
        return self.active_index != before

    def replace_active(self, stamp):
        """Swap the active entry in place, for a transform result."""
        if not self.stamps:
            return self.push(stamp)
        self.stamps[self.active_index] = stamp
        return stamp


def _self_check():
    """The mirror rule must reproduce the real mirrored port layout.

    Verified rather than derived: for every type, rotation and flip, compare
    the transformed entity's actual faces against the mirrored original's. A
    type for which the rule fails must fail here, not silently paste a
    machine wired differently from the one that was copied.
    """
    from core.entity import STRUCTURES, Belt, Underground

    def mirror_faces(faces):
        """Reflect world faces about x = 0."""
        out = []
        for ((x, y), f) in faces:
            nf = {cell.DIR_PX: cell.DIR_NX,
                  cell.DIR_NX: cell.DIR_PX}.get(f, f)
            out.append(((-x, y), nf))
        return out

    def norm(faces):
        """Translate a face set so its box starts at the origin."""
        if not faces:
            return frozenset()
        lo_x = min(x for ((x, _y), _f) in faces)
        lo_y = min(y for ((_x, y), _f) in faces)
        return frozenset(((x - lo_x, y - lo_y), f) for ((x, y), f) in faces)

    for c in STRUCTURES:
        for d in range(4):
            for fl in (False, True):
                ins, outs = c.faces_for(0, 0, d, fl)
                want_in = norm(mirror_faces(ins))
                want_out = norm(mirror_faces(outs))

                nd, nf = mirror_orientation(c, d, fl)
                got_in, got_out = c.faces_for(0, 0, nd, nf)
                assert norm(got_in) == want_in, (
                    f"{c.__name__} d={d} flip={fl}: mirrored inputs wrong")
                assert norm(got_out) == want_out, (
                    f"{c.__name__} d={d} flip={fl}: mirrored outputs wrong")

                # Mirroring twice is the identity.
                assert mirror_orientation(c, nd, nf) == (d, fl), (
                    c.__name__, d, fl)

    # A belt has no ports, so only its travel direction matters: horizontal
    # reverses, vertical does not. That is what keeps a copied run's topology
    # intact when the group is mirrored.
    for d, want in ((cell.DIR_PX, cell.DIR_NX), (cell.DIR_NX, cell.DIR_PX),
                    (cell.DIR_PY, cell.DIR_PY), (cell.DIR_NY, cell.DIR_NY)):
        got, flipped = mirror_orientation(Belt, d, False)
        assert got == want, (cell.DIR_NAMES[d], cell.DIR_NAMES[got])
        assert not flipped, "a belt has no mirrored form"

    # An underground end travels along its facing like a belt.
    for d in range(4):
        got, _f = mirror_orientation(Underground, d, False)
        assert got == (2 - d) % 4, d

    # --- stamp-level invariants ------------------------------------------
    # A mirror and a rotation each preserve the cell count and are
    # involutive / 4-periodic respectively.
    s = Stamp([
        StampEntry(Belt, 0, 0, cell.DIR_PX),
        StampEntry(Belt, 1, 0, cell.DIR_PX),
        StampEntry(Belt, 2, 0, cell.DIR_PY),
        StampEntry(Belt, 2, 1, cell.DIR_PY),
    ])
    assert s.bounds() == (0, 0, 3, 2), s.bounds()

    m = s.mirrored()
    assert len(m) == len(s)
    # The box is preserved; the *shape inside it* reflects, which is the
    # whole point -- this L-bend's vertical arm moves from the east end to
    # the west end, and its eastward belts turn westward.
    assert m.bounds() == (0, 0, 3, 2), m.bounds()
    assert len(set(m.cells())) == len(set(s.cells())), "cell count preserved"
    assert sorted(m.cells()) == [(0, 0), (0, 1), (1, 0), (2, 0)], \
        sorted(m.cells())
    # Mirroring twice returns the original offsets and facings.
    mm = m.mirrored()
    assert [(e.dx, e.dy, e.direction, e.flipped) for e in mm.entries] \
        == [(e.dx, e.dy, e.direction, e.flipped) for e in s.entries]

    r = s
    for _ in range(4):
        r = r.rotated()
    assert [(e.dx, e.dy, e.direction, e.flipped) for e in r.entries] \
        == [(e.dx, e.dy, e.direction, e.flipped) for e in s.entries], \
        "four quarter turns must be the identity"
    # One turn swaps the box extent.
    assert s.rotated().bounds() == (0, 0, 2, 3), s.rotated().bounds()

    # The run stays connected through a mirror: the belt that was the tail
    # still feeds its neighbour, just from the other side.
    line = Stamp([StampEntry(Belt, i, 0, cell.DIR_PX) for i in range(4)])
    lm = line.mirrored()
    assert all(e.direction == cell.DIR_NX for e in lm.entries), \
        "a mirrored eastward run must run west"
    assert sorted((e.dx, e.dy) for e in lm.entries) == [(i, 0) for i in range(4)]

    # A multi-cell entity's origin is re-derived, not negated: a 2-wide body
    # must still cover the same span after a mirror.
    from core.entity import Shear
    wide = Stamp([StampEntry(Shear, 0, 0, cell.DIR_PX)])
    assert wide.bounds() == (0, 0, 2, 1), wide.bounds()
    wm = wide.mirrored()
    assert wm.bounds() == (0, 0, 2, 1), wm.bounds()

    # --- history ---------------------------------------------------------
    h = StampHistory(limit=3)
    assert h.active is None and not h
    a, b, cc = Stamp(), Stamp(), Stamp()
    h.push(a)
    assert h.active is a
    h.push(b)
    assert h.active is b and h.stamps[1] is a, "newest first"
    h.push(cc)
    h.push(Stamp())
    assert len(h) == 3, "the history must be trimmed to its limit"
    assert a not in h.stamps, "the oldest should have been dropped"

    h2 = StampHistory(limit=4)
    for s_ in (a, b, cc):
        h2.push(s_)
    assert h2.active is cc
    assert h2.cycle(-1) and h2.active is b, "scroll down goes older"
    assert h2.cycle(-1) and h2.active is a
    assert not h2.cycle(-1), "clamped at the oldest"
    assert h2.cycle(+1) and h2.active is b, "scroll up goes newer"


_self_check()
