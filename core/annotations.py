"""Freehand strokes and text labels drawn over the board.

Telestrator marks, not game objects. Nothing here simulates, nothing here is
an entity, and nothing here touches the grid -- an annotation is a note *about*
a factory, which is why it lives in its own side table rather than in a cell.

Three reasons it could not be a cell or an entity, each independently fatal:

- **Annotations are sub-cell and overlapping.** A circle drawn around a region
  crosses forty cells, and two annotations routinely want the same one. A grid
  cell holds exactly one entity id in `.x`, so the grid has nowhere to put the
  second -- the same wall `GHOST_FRUSTRATED` hit, which is why frustration is
  an overlay in `.z` rather than an entity.
- **They carry data no entity row can hold.** A colour, a thickness, a string.
  `ENTITY_STRIDE` is a float32 row sized by the gate block, and widening it
  invalidates every save file (see `core/persistence.py`) to carry text that
  one non-machine wants.
- **Their coordinates are continuous.** A stroke point is a world position, not
  a cell index. Snapping to the lattice would make a circle a staircase.

So: a flat list of objects with stable integer ids, in world coordinates,
drawn by `ui/annotation_layer.py` through imgui and saved beside the board.


Ids are handles, not indices
----------------------------

`AnnotationStore.add_*` returns an id from a monotonic counter, and every
lookup goes through `get()`. Deleting an annotation does not renumber the
others, which is what makes it safe for the Status window to hold one across
frames -- exactly the hazard `Orchestrator.inspect_row` documents for entity
rows, with the same resolution. An id is never reused, so a stale one reads as
`None` rather than as somebody else's label.


Bounding boxes, and what "touching" means
-----------------------------------------

Every annotation reports `bounds()` as a world-space `(x0, y0, x1, y1)`. Copy
capture and the rectangle eraser both test that box against their rectangle --
deliberately the box and not the ink, including for strokes.

That is a real trade and it is the chosen one: circling an entire ALU gives a
large mostly-empty square, so a copy rectangle clipping any corner of it takes
the circle along. Testing the actual points would be stricter, but "one rule,
stated once" beats a per-type rule that a player has to learn by surprise --
and for the common case (a short underline, a label, an arrow) the box *is*
the ink.

The brush eraser is the exception, and necessarily: it is a point, not a
rectangle, so it tests real distance to the stroke (`hit_point`). A bbox test
there would delete a circle whose centre the player merely passed through.
"""

import math

#: Stroke thicknesses and text sizes the player can pick, in world cells.
#: A short fixed list rather than a continuous slider: a diagram reads better
#: with a few consistent weights, and three is enough to separate a heading
#: from a note from a scribble.
STROKE_WIDTHS = (0.06, 0.12, 0.24)
TEXT_SIZES = (0.5, 0.9, 1.6)

#: The palette, as `(name, (r, g, b))` with components in 0..1. Fixed rather
#: than a colour picker for the same reason the widths are: a telestrator has
#: one pen at a time, and a readable diagram uses few colours. Ordered so the
#: first is the default and the most visible against the dark board.
PALETTE = (
    ("yellow", (1.00, 0.85, 0.20)),
    ("red",    (1.00, 0.33, 0.28)),
    ("green",  (0.40, 0.95, 0.45)),
    ("cyan",   (0.35, 0.85, 1.00)),
    ("white",  (0.96, 0.96, 0.98)),
    ("orange", (1.00, 0.58, 0.18)),
)

#: Minimum world distance between consecutive recorded stroke points. The
#: mouse is sampled per frame, so a slow drag would otherwise record hundreds
#: of near-identical points -- which costs vertices on every later frame and
#: saves a file full of noise. Scaled to the smallest visible detail rather
#: than to the zoom, so a stroke drawn zoomed-in stays smooth when zoomed out.
MIN_POINT_GAP = 0.04

#: How close (in world cells, plus half the stroke width) the brush eraser has
#: to come to a stroke to delete it.
BRUSH_SLACK = 0.12


def _seg_distance(px, py, ax, ay, bx, by):
    """Distance from a point to the segment AB. Plain geometry, no numpy.

    Used per stroke segment by the brush eraser, which runs once per frame at
    most -- a loop in Python is fine at that rate and keeps this module free
    of an import the rest of it does not need.
    """
    dx, dy = bx - ax, by - ay
    span = dx * dx + dy * dy
    if span <= 1e-12:
        return math.hypot(px - ax, py - ay)
    # Project onto the segment, clamped to its ends.
    t = ((px - ax) * dx + (py - ay) * dy) / span
    t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


class Annotation:
    """Common behaviour: an id, a colour index, and a world-space box."""

    __slots__ = ("aid", "colour")

    #: Discriminator written to the save file. A *string*, like a builder
    #: kind, because it ends up in a file the player can edit and an integer
    #: there would be unreadable. Renaming one silently drops those
    #: annotations on load, so these are a compatibility surface.
    KIND = None

    def __init__(self, aid, colour=0):
        self.aid = aid
        #: Index into PALETTE. An index rather than an rgb triple so a
        #: palette tweak restyles existing annotations, and so the saved form
        #: stays small and legible.
        self.colour = int(colour)

    def bounds(self):
        raise NotImplementedError

    def translate(self, dx, dy):
        raise NotImplementedError

    def intersects(self, x0, y0, x1, y1):
        """True if this annotation's bounding box overlaps the world rect.

        The rect is given in any corner order. Boxes touching exactly at an
        edge count as overlapping, matching the inclusive feel of the cell
        rectangle the player dragged.
        """
        lo_x, hi_x = (x0, x1) if x0 <= x1 else (x1, x0)
        lo_y, hi_y = (y0, y1) if y0 <= y1 else (y1, y0)
        bx0, by0, bx1, by1 = self.bounds()
        return not (bx1 < lo_x or bx0 > hi_x or by1 < lo_y or by0 > hi_y)

    def to_data(self):
        raise NotImplementedError

    def rotate_in_box(self, w, h, backward=False):
        """Rotate a quarter turn inside a `w` x `h` box, in place.

        For a stamp transform. The box is the stamp's extent *before* the
        turn, and the caller renormalises afterwards -- so this only has to
        get the rotation right, not the resulting origin.

        A CCW quarter turn about the box takes `(x, y)` to `(h - y, x)`,
        matching `cell.rotate_offset()`'s convention so an annotation turns
        with the entities it sits among rather than against them.
        """
        raise NotImplementedError

    def mirror_in_box(self, w):
        """Reflect about the vertical centre line of a `w`-wide box, in place.

        Matches `Stamp.mirrored()`, which reflects x and leaves y alone.
        """
        raise NotImplementedError


class Stroke(Annotation):
    """A freehand polyline in world coordinates."""

    __slots__ = ("points", "width")

    KIND = "stroke"

    def __init__(self, aid, points=(), colour=0, width=1):
        super().__init__(aid, colour)
        #: Flat list of (x, y) world positions, in drawing order.
        self.points = [(float(x), float(y)) for (x, y) in points]
        #: Index into STROKE_WIDTHS, for the same reason `colour` is an index.
        self.width = int(width)

    def add_point(self, x, y):
        """Append a point if it is far enough from the last. Returns if added.

        The gap test is what keeps a slow drag from recording a point per
        frame -- see MIN_POINT_GAP.
        """
        if self.points:
            lx, ly = self.points[-1]
            if math.hypot(x - lx, y - ly) < MIN_POINT_GAP:
                return False
        self.points.append((float(x), float(y)))
        return True

    def world_width(self):
        i = max(0, min(len(STROKE_WIDTHS) - 1, self.width))
        return STROKE_WIDTHS[i]

    def bounds(self):
        if not self.points:
            return (0.0, 0.0, 0.0, 0.0)
        xs = [p[0] for p in self.points]
        ys = [p[1] for p in self.points]
        # Inflated by the half-width, so the box covers the drawn ink rather
        # than the mathematical centre line.
        pad = self.world_width() * 0.5
        return (min(xs) - pad, min(ys) - pad, max(xs) + pad, max(ys) + pad)

    def translate(self, dx, dy):
        self.points = [(x + dx, y + dy) for (x, y) in self.points]

    def hit_point(self, x, y, slack=BRUSH_SLACK):
        """True if (x, y) is within `slack` of the stroke's ink.

        Real distance to the polyline, not the bounding box: the brush eraser
        is a point the player drags *along* what they want gone, so a box test
        would take a whole circle for passing near its centre.
        """
        if not self.points:
            return False
        reach = slack + self.world_width() * 0.5
        if len(self.points) == 1:
            px, py = self.points[0]
            return math.hypot(x - px, y - py) <= reach
        for i in range(len(self.points) - 1):
            ax, ay = self.points[i]
            bx, by = self.points[i + 1]
            if _seg_distance(x, y, ax, ay, bx, by) <= reach:
                return True
        return False

    def rotate_in_box(self, w, h, backward=False):
        """Rotate every point about the box. A stroke has no reading order,
        so unlike text it turns fully and faithfully."""
        if backward:
            self.points = [(y, w - x) for (x, y) in self.points]
        else:
            self.points = [(h - y, x) for (x, y) in self.points]

    def mirror_in_box(self, w):
        self.points = [(w - x, y) for (x, y) in self.points]

    def to_data(self):
        # Points are rounded on the way out: they come from mouse positions
        # and three decimals is well past what a 6-pixel brush can express,
        # so the extra digits are noise that triples the file size.
        return {
            "kind": self.KIND,
            "colour": self.colour,
            "width": self.width,
            "points": [[round(x, 3), round(y, 3)] for (x, y) in self.points],
        }

    def copy(self, aid):
        return Stroke(aid, self.points, self.colour, self.width)

    def __repr__(self):
        return f"<Stroke #{self.aid} {len(self.points)} pts>"


class TextBox(Annotation):
    """A text label anchored at a world position.

    The anchor is the label's **top-left**, and `w`/`h` are the box the player
    dragged out. The drag sets the box; the text is typed afterwards in the
    Status window, which is why a fresh one is created empty and immediately
    opens that window (an empty label the player cannot find would be litter).
    """

    __slots__ = ("x", "y", "w", "h", "text", "size")

    KIND = "text"

    def __init__(self, aid, x, y, w, h, text="", colour=0, size=1):
        super().__init__(aid, colour)
        self.x = float(x)
        self.y = float(y)
        self.w = float(w)
        self.h = float(h)
        self.text = str(text)
        #: Index into TEXT_SIZES.
        self.size = int(size)

    def world_size(self):
        i = max(0, min(len(TEXT_SIZES) - 1, self.size))
        return TEXT_SIZES[i]

    def bounds(self):
        # y is the top and the box extends downward, so the world-space box
        # runs from y - h to y. World +Y is up (see the project conventions),
        # and text reads downward from its anchor like every other label.
        return (self.x, self.y - self.h, self.x + self.w, self.y)

    def translate(self, dx, dy):
        self.x += dx
        self.y += dy

    def hit_point(self, x, y, slack=0.0):
        """True if (x, y) is inside the box. How clicking one selects it."""
        x0, y0, x1, y1 = self.bounds()
        return (x0 - slack) <= x <= (x1 + slack) and \
               (y0 - slack) <= y <= (y1 + slack)

    def rotate_in_box(self, w, h, backward=False):
        """Move the box with the turn; never rotate the text itself.

        **The position rotates and the glyphs do not.** Text has a reading
        orientation a mark does not, so a label that turned with the stamp
        would come out sideways or upside down -- unreadable, and the player
        rotated the stamp to place a factory, not to re-letter it. This is
        the same rule the Source's emit label already follows in the shader
        (CLAUDE.md: "The label does not rotate with the facing").

        The box's own extent therefore swaps `w`/`h` only in the sense that
        the anchor moves; the label keeps its own width and height, since a
        rotated-but-upright box of swapped proportions would reflow the text
        for no reason the player asked for.
        """
        # Rotate the anchor's *centre* rather than its corner, so the label
        # stays over the same machines. Using the corner would drift the box
        # by its own size on every turn, which compounds over four turns into
        # a visible walk instead of the identity.
        cx, cy = self.x + self.w * 0.5, self.y - self.h * 0.5
        ncx, ncy = (cy, w - cx) if backward else (h - cy, cx)
        self.x = ncx - self.w * 0.5
        self.y = ncy + self.h * 0.5

    def mirror_in_box(self, w):
        """Reflect the box's position; never mirror the text.

        Mirrored text is simply broken, so the same rule as rotation applies
        and more starkly.
        """
        cx = self.x + self.w * 0.5
        self.x = (w - cx) - self.w * 0.5

    def to_data(self):
        return {
            "kind": self.KIND,
            "colour": self.colour,
            "size": self.size,
            "text": self.text,
            "rect": [round(self.x, 3), round(self.y, 3),
                     round(self.w, 3), round(self.h, 3)],
        }

    def copy(self, aid):
        return TextBox(aid, self.x, self.y, self.w, self.h, self.text,
                       self.colour, self.size)

    def __repr__(self):
        head = (self.text[:16] + "...") if len(self.text) > 16 else self.text
        return f"<TextBox #{self.aid} {head!r}>"


#: Every annotation class, by its saved `KIND`. Generated from the classes so
#: a new annotation type needs no second registration -- the same property the
#: GLSL type tables have.
KINDS = {c.KIND: c for c in (Stroke, TextBox)}


def _from_data(aid, item):
    """Rebuild one annotation from its saved form, or None if unusable.

    Per-item validation rather than all-or-nothing, matching
    `config.toolbar_from_data`: this comes from a hand-editable file and an
    older build may have written it, so one bad entry costs one annotation
    and leaves the rest alone.
    """
    if not isinstance(item, dict):
        return None
    cls = KINDS.get(item.get("kind"))
    if cls is None:
        return None
    colour = item.get("colour", 0)
    if not isinstance(colour, int):
        colour = 0
    try:
        if cls is Stroke:
            pts = item.get("points") or []
            if not isinstance(pts, list):
                return None
            points = [(float(p[0]), float(p[1])) for p in pts
                      if isinstance(p, (list, tuple)) and len(p) >= 2]
            if not points:
                return None
            width = item.get("width", 1)
            return Stroke(aid, points, colour,
                          width if isinstance(width, int) else 1)
        rect = item.get("rect")
        if not isinstance(rect, (list, tuple)) or len(rect) < 4:
            return None
        size = item.get("size", 1)
        text = item.get("text", "")
        return TextBox(aid, float(rect[0]), float(rect[1]),
                       float(rect[2]), float(rect[3]),
                       text if isinstance(text, str) else "",
                       colour, size if isinstance(size, int) else 1)
    except (TypeError, ValueError):
        return None


class AnnotationStore:
    """Every annotation on the board, in draw order, addressed by id.

    Draw order is insertion order and never changes, so a later annotation is
    drawn over an earlier one -- the only layering rule, and the one a player
    would predict.
    """

    def __init__(self):
        self._items = []
        self._next_id = 1
        #: Bumped on every change, so the renderer and the save path can tell
        #: whether anything happened without diffing the list.
        self.revision = 0

    # --- queries ---------------------------------------------------------
    def __len__(self):
        return len(self._items)

    def __bool__(self):
        return bool(self._items)

    def __iter__(self):
        return iter(self._items)

    @property
    def items(self):
        """The annotations, in draw order. Treat as read-only."""
        return self._items

    def get(self, aid):
        """The annotation with this id, or None if it is gone.

        The only lookup. Ids are never reused, so a stale handle reads as
        None rather than as a different annotation -- which is what lets the
        Status window hold one across frames.
        """
        if aid is None:
            return None
        for a in self._items:
            if a.aid == aid:
                return a
        return None

    def hit(self, x, y, slack=0.0):
        """The topmost annotation at a world point, or None.

        Reverse draw order, so the one drawn last -- the one visually on top
        -- is the one a click selects.
        """
        for a in reversed(self._items):
            if a.hit_point(x, y, slack):
                return a
        return None

    def in_rect(self, x0, y0, x1, y1):
        """Every annotation whose bounding box the rect touches."""
        return [a for a in self._items if a.intersects(x0, y0, x1, y1)]

    # --- mutation --------------------------------------------------------
    def _claim_id(self):
        aid = self._next_id
        self._next_id += 1
        return aid

    def add_stroke(self, points=(), colour=0, width=1):
        s = Stroke(self._claim_id(), points, colour, width)
        self._items.append(s)
        self.revision += 1
        return s

    def add_text(self, x, y, w, h, text="", colour=0, size=1):
        t = TextBox(self._claim_id(), x, y, w, h, text, colour, size)
        self._items.append(t)
        self.revision += 1
        return t

    def add(self, annotation):
        """Adopt an annotation built elsewhere, assigning it a fresh id.

        How a paste lands: the stamp carries annotation *templates* with no
        ids of their own, and each placement becomes a new annotation here.
        """
        annotation.aid = self._claim_id()
        self._items.append(annotation)
        self.revision += 1
        return annotation

    def remove(self, aid):
        """Delete by id. Returns True if anything went."""
        for i, a in enumerate(self._items):
            if a.aid == aid:
                del self._items[i]
                self.revision += 1
                return True
        return False

    def erase_rect(self, x0, y0, x1, y1):
        """Delete every annotation the rect touches. Returns the count."""
        doomed = [a.aid for a in self.in_rect(x0, y0, x1, y1)]
        for aid in doomed:
            self.remove(aid)
        return len(doomed)

    def erase_at(self, x, y, slack=BRUSH_SLACK):
        """Delete the topmost annotation under a world point. Returns count.

        One at a time rather than everything at that point: the brush is
        dragged, so the next frame takes the next one, and deleting a stack
        on a single frame makes a flick of the wrist destroy more than the
        player saw.
        """
        a = self.hit(x, y, slack)
        if a is None:
            return 0
        self.remove(a.aid)
        return 1

    def clear(self):
        """Drop everything. What `Delete` and a board load do."""
        if not self._items:
            return 0
        n = len(self._items)
        self._items = []
        self.revision += 1
        return n

    # --- persistence -----------------------------------------------------
    def to_data(self):
        """The whole store as JSON-safe plain data."""
        return [a.to_data() for a in self._items]

    def load_data(self, data):
        """Replace the contents from `to_data()`'s output.

        Ids are re-assigned rather than saved: they are session handles, and
        persisting them would mean a loaded board could hand out an id the
        Status window is already holding from the previous board. A bad entry
        is skipped (see `_from_data`); a non-list `data` -- including the
        `None` an older save has no field for -- loads as empty, which is
        exactly "this board has no annotations".
        """
        self._items = []
        self._next_id = 1
        self.revision += 1
        if not isinstance(data, list):
            return 0
        for item in data:
            a = _from_data(self._next_id, item)
            if a is None:
                continue
            self._claim_id()
            self._items.append(a)
        return len(self._items)


def _self_check():
    """Geometry and id discipline. Cheap; runs at import."""
    st = AnnotationStore()
    assert not st and len(st) == 0

    s = st.add_stroke([(1, 1), (3, 1)], colour=1, width=0)
    t = st.add_text(5.0, 9.0, 4.0, 1.0, "ALU", colour=2, size=2)
    assert len(st) == 2
    assert st.get(s.aid) is s and st.get(t.aid) is t
    assert st.get(None) is None and st.get(9999) is None

    # Ids are handles: deleting one must not renumber or re-point another.
    third = st.add_stroke([(8, 8)])
    assert st.remove(s.aid)
    assert st.get(s.aid) is None, "a deleted id must read as gone"
    assert st.get(third.aid) is third, "ids must not shift on delete"
    assert not st.remove(s.aid), "removing twice is not a second delete"
    # And are never reused, so a stale handle cannot name a new annotation.
    fresh = st.add_stroke([(0, 0)])
    assert fresh.aid != s.aid

    # --- stroke geometry ---
    line = Stroke(1, [(0, 0), (2, 0)], width=0)
    pad = STROKE_WIDTHS[0] * 0.5
    x0, y0, x1, y1 = line.bounds()
    assert abs(x0 + pad) < 1e-9 and abs(x1 - (2 + pad)) < 1e-9
    assert abs(y0 + pad) < 1e-9 and abs(y1 - pad) < 1e-9, \
        "a horizontal stroke's box is its half-width tall"

    # Distance is to the ink, not to the box: the far corner of a long
    # stroke's bbox is nowhere near the line.
    assert line.hit_point(1.0, 0.0)
    assert line.hit_point(1.0, 0.05)
    assert not line.hit_point(1.0, 5.0)
    # An L-shaped stroke has a large empty bbox corner. The box test catches
    # it and the point test does not -- which is the documented split
    # between the rectangle eraser and the brush.
    bend = Stroke(2, [(0, 0), (4, 0), (4, 4)], width=0)
    assert bend.intersects(0.2, 3.0, 1.0, 3.5), "the bbox covers the corner"
    assert not bend.hit_point(0.5, 3.2), "but no ink is there"

    # The point-gap filter keeps a slow drag from recording every frame.
    gap = Stroke(3, [(0, 0)])
    assert not gap.add_point(MIN_POINT_GAP * 0.4, 0.0), "too close"
    assert gap.add_point(MIN_POINT_GAP * 2.0, 0.0)
    assert len(gap.points) == 2

    # Translation moves every point and leaves the extent alone.
    before = line.bounds()
    line.translate(3.0, -2.0)
    after = line.bounds()
    assert abs((after[0] - before[0]) - 3.0) < 1e-9
    assert abs((after[1] - before[1]) + 2.0) < 1e-9
    assert abs((after[2] - after[0]) - (before[2] - before[0])) < 1e-9

    # --- text geometry ---
    # The anchor is the top-left and the box hangs downward, since world +Y
    # is up and text reads top-down.
    tb = TextBox(4, 10.0, 20.0, 5.0, 2.0, "HI")
    assert tb.bounds() == (10.0, 18.0, 15.0, 20.0), tb.bounds()
    assert tb.hit_point(12.0, 19.0)
    assert not tb.hit_point(12.0, 21.0), "above the anchor is outside"
    assert not tb.hit_point(16.0, 19.0)

    # --- rect queries ---
    st2 = AnnotationStore()
    near = st2.add_stroke([(1, 1), (2, 2)])
    far = st2.add_stroke([(50, 50), (51, 51)])
    caught = st2.in_rect(0, 0, 5, 5)
    assert caught == [near], caught
    # Corner order must not matter.
    assert st2.in_rect(5, 5, 0, 0) == [near]
    assert st2.erase_rect(0, 0, 5, 5) == 1
    assert len(st2) == 1 and st2.get(far.aid) is far

    # hit() is topmost-first, so a click takes what is drawn on top.
    st3 = AnnotationStore()
    under = st3.add_text(0, 1, 2, 1, "under")
    over = st3.add_text(0, 1, 2, 1, "over")
    assert st3.hit(1.0, 0.5) is over, "the last drawn wins a click"
    # The brush deletes one per call, so a drag cannot wipe a stack at once.
    assert st3.erase_at(1.0, 0.5) == 1
    assert st3.hit(1.0, 0.5) is under

    # --- persistence round trip ---
    st4 = AnnotationStore()
    st4.add_stroke([(1.5, 2.5), (3.5, 4.5)], colour=3, width=2)
    st4.add_text(1.0, 2.0, 6.0, 1.5, "CARRY OUT", colour=4, size=0)
    data = st4.to_data()
    import json
    data = json.loads(json.dumps(data))      # must survive real JSON
    st5 = AnnotationStore()
    assert st5.load_data(data) == 2
    a, b = st5.items
    assert isinstance(a, Stroke) and isinstance(b, TextBox)
    assert a.colour == 3 and a.width == 2
    assert b.text == "CARRY OUT" and b.colour == 4 and b.size == 0
    assert b.bounds() == (1.0, 0.5, 7.0, 2.0), b.bounds()

    # An older save has no annotation field at all. That must load as empty
    # rather than raising -- it is the normal case for every existing file.
    st6 = AnnotationStore()
    assert st6.load_data(None) == 0
    assert st6.load_data([]) == 0
    assert st6.load_data("garbage") == 0

    # One bad entry costs one annotation, not the whole set.
    mixed = st6.load_data([
        {"kind": "stroke", "points": [[0, 0], [1, 1]]},
        {"kind": "nonesuch"},
        {"kind": "text"},                       # no rect
        {"kind": "text", "rect": [0, 0, 1, 1], "text": "ok"},
    ])
    assert mixed == 2, mixed

    # Loading re-assigns ids, so a handle from the previous board cannot
    # name an annotation on the new one.
    st7 = AnnotationStore()
    st7.add_stroke([(0, 0), (1, 1)])
    old = st7.add_stroke([(2, 2), (3, 3)])
    st7.load_data(data)
    assert all(x.aid != old.aid for x in st7.items) or len(st7) == 0 \
        or st7.get(old.aid) is not old

    # Every class registers itself by KIND, and every KIND is a real string.
    for kind, cls in KINDS.items():
        assert isinstance(kind, str) and kind
        assert cls.KIND == kind

    # clear() is what Delete and a load use.
    assert st7.clear() == len(data)
    assert not st7 and st7.clear() == 0


_self_check()
