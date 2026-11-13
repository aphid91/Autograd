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
# A gate needs, per channel: the value held at each input, the value waiting
# at each output, and an occupancy flag for both. The widest is Sink's four
# input channels, so 8 common + 4*2 values + 4*2 flags = 24 slots of gate
# state. Rows are cheap and branching is not, so every type carries the full
# set rather than packing per-type -- a belt row simply has unused slots.
#
# The last four carry a **belt's seed** (see `Belt.F_SEED_ON`). Unlike
# Source's emit value, which squats on an input slot a generator can never
# read, a belt has no dead slot to reuse: every one of the 24 belongs to the
# gate block, and a belt is not a gate so none of them *is* read -- but
# aliasing onto them would make a belt's seed and a machine's channel the
# same storage, which is exactly the kind of coincidence that breaks the
# first time a belt gains any channel at all. So the row grew by one vec4,
# the smallest honest increment. That widens every row in the game, which is
# the price; at 512x512 with a few thousand entities it is a few tens of KB.
#
# Was 32, set by a six-port Source. Narrowing that machine to three ports
# left Sink's four as the widest, which dropped it to 24; the belt seed takes
# it to 28.
#
# **The stride is baked into save files**, so changing it invalidates older
# ones -- persistence checks the shape and refuses them.
ENTITY_STRIDE = 28

#: Most *channels* of one kind any machine has -- not port faces. Fixes the
#: per-row gate layout, so a machine with more channels means bumping this
#: and ENTITY_STRIDE together.
#:
#: Faces and channels are not the same thing, and the two ends of the scale
#: show why it has to be channels.
#:
#: **A channel is one item per tick**, in either direction, because it is one
#: slot with one occupancy flag. That makes every fold a throughput decision
#: rather than a storage one, and it is the fact behind both cases below.
#:
#: **Sink folds eight faces onto four channels, one per footprint cell.** The
#: values really are interchangeable -- a sink destroys whatever arrives --
#: but the rate is not, so folding all eight onto a single channel capped the
#: machine at one item per tick and made two saturated belts into one sink
#: deliver one belt's worth. Per cell, each of the four tiles drains
#: independently. See Sink.in_channel().
#:
#: **Source does not fold at all -- three faces, three channels.** Same
#: arithmetic from the other side: a waiting zone is cleared by whoever
#: drains it, so two belts sharing one channel would split one item per tick
#: between them instead of each getting their own. Independent channels are
#: what let one Source fill several belts at once, with the unconnected faces
#: simply staying empty.
#:
#: Sizing the row by *faces* would mean 8 channels for every entity. Sizing
#: by channels keeps it at 4, now set by Sink -- Source used to set it at 6
#: with all six perimeter faces, so narrowing that machine is what let both
#: this and ENTITY_STRIDE come down.
MAX_CHANNELS = 4


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

    # Whether an *already placed* entity may be turned or mirrored where it
    # stands, by R/F with an empty hand. Distinct from ROTATABLE, which is a
    # statement about the tool that places the type: a belt's facing comes
    # from the path it is drawn along (so ROTATABLE is False) but turning one
    # that already exists is a perfectly ordinary edit.
    #
    # True by default, so a new machine gets the gesture for free; the turns
    # it actually permits are still decided by geometry in inplace_turns().
    # Underground overrides the gesture entirely rather than clearing this --
    # its R and F both mean "reverse the pair" (see Underground).
    INPLACE_TURNABLE = True

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
    #: FIRE_SELECT -- readiness depends on the *values* held, not merely on
    #:              which channels are fed. Declared by a type that overrides
    #:              `inputs_ready()`; the base class never resolves it, so a
    #:              type naming it without overriding is a bug the self-check
    #:              catches rather than a silently-never-firing machine.
    #:
    #: The enum survives the move to an overridable `inputs_ready()` because
    #: it is also the declared place for a future per-type rate limit, and
    #: because it keeps the common two cases readable at the class level.
    FIRE_ALL = 0
    FIRE_ANY = 1
    FIRE_SELECT = 2
    FIRE_RULE = FIRE_ALL

    # --- the arithmetic, as data -----------------------------------------
    #
    # `fire()` is the authority and stays Python: it is readable, it is what
    # the tests exercise, and ten short methods are the clearest statement of
    # ten machines' semantics. But it cannot cross to the GPU -- there is no
    # subclassing in GLSL and no `None` -- so the arithmetic also has to exist
    # in a form a generator can emit.
    #
    # `OPS` is that form. One entry per declared output channel, each a tuple
    #
    #     (opcode, arg, arg, ...)
    #
    # where an arg is an input channel index. The opcodes are deliberately few
    # and total -- every one is defined for every float input, which is what
    # lets a kernel evaluate them branchlessly or with a cheap switch:
    #
    #   OP_IN        a              pass input `a` through unchanged
    #   OP_ADD       a, b           a + b
    #   OP_MAC       a, b, c        c + a*b
    #   OP_RECIP     a              1/a, clamped (see VALUE_LIMIT)
    #   OP_MAX       a, b           max(a, b), NaN-propagating
    #   OP_FLOOR     a              floor(a), clamped before flooring
    #   OP_SEL       c, a, b        a if c > 0 else b
    #   OP_GATED     c, a, pol      `a` if (c > 0) == pol, else emit nothing
    #   OP_CONST     -              the per-instance constant (Source)
    #   OP_NONE      -              emit nothing, ever
    #
    # **`OPS` is a declaration, not an implementation.** The base `fire()`
    # does not read it, because a machine whose semantics do not fit the
    # opcode set must still be expressible -- it simply overrides `fire()` and
    # declares `OPS = None`, which the generator reads as "this type has no
    # GPU form yet" rather than silently emitting something wrong.
    #
    # The two are cross-validated: `_self_check()` evaluates `OPS` against
    # `fire()` over a sweep of inputs for every type that declares both, so
    # the pair cannot drift. That is the same oracle relationship
    # `naive_tick()` has to the closed form, and it exists for the same
    # reason -- a wrong opcode table computes a perfectly plausible wrong
    # number, with no visual artefact at all.
    OP_NONE = 0
    OP_IN = 1
    OP_ADD = 2
    OP_MAC = 3
    OP_RECIP = 4
    OP_MAX = 5
    OP_FLOOR = 6
    OP_SEL = 7
    OP_GATED = 8
    OP_CONST = 9

    #: Per-output-channel opcodes, or None for a type with no GPU form.
    #: Default is "emit nothing on every channel", matching the base
    #: `fire()` -- which is correct for a type with no outputs and a safe
    #: default for one whose arithmetic has not been written.
    OPS = ()

    #: Value a *freshly placed* Source emits. The live value is per instance,
    #: in the row at `Source.F_EMIT`; this is only the starting point, which
    #: is why it stays a class attribute rather than a literal in the tick.
    #:
    #: Zero rather than a non-zero marker: a Source's job is to be configured,
    #: and starting at an arithmetically neutral value means placing one does
    #: not inject a number the player has to notice and correct. Note this
    #: makes the class default indistinguishable from a zeroed row -- see
    #: `Source.initialise()`, which is what keeps "placed" and "never
    #: initialised" from collapsing into each other.
    EMIT_VALUE = 0.0

    # --- appearance ------------------------------------------------------
    # Declared here, beside the ports, so a machine is defined in exactly one
    # place. core/glsl_types.py emits these into GLSL at shader-load time;
    # nothing in the shaders names a type by hand.
    #: Body colour as (r, g, b), 0..1. None means "not drawn as a body".
    COLOUR = None
    #: Glyph drawn centred on the footprint. One of the GLYPH_* names.
    GLYPH = "none"

    # Exposed for callers that read a row without building a view --
    # Gameboard, which pokes these directly, and the gate tick's hot port
    # callbacks, which read one float per segment per tick and cannot afford
    # an object to do it (see GateSim._live_port).
    F_ALIVE = _F_ALIVE
    F_TYPE = _F_TYPE
    F_ORIGIN_X = _F_ORIGIN_X
    F_ORIGIN_Y = _F_ORIGIN_Y
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

    #: **Diagnostic only: is this gate currently in `GateSim._asleep`?**
    #: Written by the gate layer each structure phase purely so the
    #: SLEEP_DEBUG overlay can draw the *real* sleep set instead of guessing
    #: at it. Nothing in the simulation reads it, and it is deliberately not
    #: the authority -- `GateSim._asleep` is.
    #:
    #: It lives here rather than in a `.z` bit for two reasons. A `.z` bit
    #: would need a `restamp()` on every sleep transition (6-11 gates a tick
    #: on a real board, against a field the SSBO upload already carries), and
    #: `.z`'s ghost field is full. And it squats on a **free** slot rather
    #: than widening the row: gate state ends at `_GATE_FIELDS_END` (24) and
    #: the belt seed takes 24-25, so 26 is spare at the current stride. That
    #: matters because widening the stride invalidates every save file.
    #:
    #: Aliasing onto the belt-seed block is safe in the one direction that
    #: counts -- a belt is never a gate, so nothing writes both -- but it is
    #: still an alias, so the self-check below pins that this sits past the
    #: gate block and clear of `Belt.F_SEED_VALUE`.
    F_ASLEEP = _GATE_FIELDS_END + 2

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
        channels are None (only reachable under FIRE_ANY or FIRE_SELECT).
        Returns a list of length output_count(), where None means "emit
        nothing on this channel".

        **A None result is a real decision, not a gap.** Route uses it to
        send one item down one of two paths, which is what makes "either/or"
        routing expressible without a special case in the tick: the channel
        it declines simply never fills.

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

    def consumed(self, values):
        """Which input channels firing with `values` uses up.

        A sequence of channel indices. The default is **every fed channel**,
        which is what every machine but Merge wants: an arithmetic gate reads
        all its inputs, and a Sink under FIRE_ANY eats whatever arrived.

        Merge is why this is a method at all. It waits for its control and
        then takes *one* of its two data lanes, and the lane it did not take
        has to **stay loaded** -- destroying it would lose material the
        player can see sitting in the machine, and emitting it would defeat
        the point of the gate. Consumption used to be implicit in
        `GateSim.structure_phase()`, which cleared every held channel and
        gave the machine no say.

        Returned rather than mutated here so the structure phase stays the
        only writer of gate state, exactly as `fire()` is a pure function of
        its inputs.
        """
        return [i for i in range(self.input_count()) if self.in_held(i)]

    def inputs_ready(self):
        """Does this gate's firing rule consider it fed?

        Reads FIRE_RULE rather than branching on type, and is **overridable**
        for the cases the enum cannot express. A machine with no inputs is
        vacuously ready under either rule, which is what makes Source fire
        without a special case.

        FIRE_SELECT is deliberately not resolved here: a type declaring it
        must override this method, because readiness then depends on the
        values held rather than on occupancy alone. Returning False would
        make such a type silently never fire, so the self-check pins that
        every FIRE_SELECT type really does override.
        """
        n = self.input_count()
        if n == 0:
            return True
        if self.FIRE_RULE == self.FIRE_ANY:
            return any(self.in_held(i) for i in range(n))
        return all(self.in_held(i) for i in range(n))

    def outputs_clear(self, channels=None):
        """Is every output waiting zone free?

        A gate cannot fire with anything still waiting -- that is the
        back-pressure signal, and it is why a backed-up belt eventually
        stalls the machine feeding it rather than losing items.

        `channels` narrows the question to a subset, which is how a
        **dangling output no longer blocks**: the caller passes the channels
        that actually have a consumer, and a channel with nothing on the far
        side is neither waited on nor filled. See
        `GateSim._connected_channels()`, which is now consulted for every
        gate rather than only for generators.

        A dangling channel's zone must therefore never be filled in the
        first place -- the structure phase discards those results. Were one
        to fill anyway, excluding it here would mean a gate blocked forever
        on a zone nothing drains and nothing checks.
        """
        if channels is None:
            channels = range(self.output_count())
        return not any(self.out_held(i) for i in channels)

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
    def inplace_turns(cls):
        """Quarter-turns that leave this type's cell set unchanged, as a tuple.

        The set of rotations an **already placed** entity may take without
        moving: rotating in place must not ask for a cell the entity does not
        already own, or the turn could be blocked by a neighbour and "rotate
        this machine" would become a placement question.

        Footprint-preserving is the whole test, because every current type is
        a solid rectangle and `cells_for()` fills the bounding box: if the
        extent is unchanged then so is the cell set, whatever the origin.
        A square (1x1 belt, 2x2 Sink) turns freely; an oblong turns only by
        180 degrees, which is why `R` and `shift+R` are the same thing on a
        2x1 machine. Deliberately excludes 0 -- a no-op is not a turn.

        A type whose cells are *not* the full bounding box (an L tetromino,
        say) would need the cell set compared rather than the extent, and
        would then almost certainly answer "no turns at all". Returning an
        empty tuple is how rotation silently fails on such a type, which is
        the stated policy -- so this stays a predicate about geometry and no
        caller has to special-case a shape.

        **This deliberately does not consult `ROTATABLE`.** That flag is
        about *tools* -- it says a belt's facing comes from the path it is
        drawn along, so the planner does not offer R -- and a belt already
        sitting on the board is the opposite case: turning it is exactly the
        edit the player means. See `INPLACE_TURNABLE` for the per-type veto.
        """
        if not cls.INPLACE_TURNABLE:
            return ()
        w, h = cls.FOOTPRINT
        # Relative to whatever the entity's current facing is, so the answer
        # is the same at every orientation: a quarter turn swaps the extent.
        return (1, 2, 3) if w == h else (2,)

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

    #: (class, direction, flipped) -> (inputs, outputs) at origin (0, 0).
    #: Shared across every instance; see faces_for().
    _faces_cache = {}

    @classmethod
    def faces_at_origin(cls, direction, flipped=False):
        """`faces_for()` at origin (0, 0), memoised per (type, dir, flip).

        The port layout is **class** data: it depends on the type, its facing
        and its flip, and on nothing per-instance. Placement only translates
        it, so the expensive half -- `place_local()`, which rotates four
        bounding-box corners and takes two minima *per port* -- can be
        computed once per orientation and reused by every instance forever.

        That matters because the gate tick recomputes port geometry for every
        gate on every tick, through `_output_faces()` and
        `_connected_channels()`. On a board of 336 gates that was about 2ms a
        tick of arithmetic whose answer had not changed since the machine was
        placed.

        Returns tuples rather than lists, so a caller cannot mutate the cached
        entry and corrupt every later lookup.
        """
        key = (cls, direction, flipped)
        hit = Entity._faces_cache.get(key)
        if hit is not None:
            return hit

        def place(spec):
            out = []
            for (lx, ly, face) in spec:
                rx, ry = cls.place_local(lx, ly, direction, flipped)
                f = cell.flip_dir_y(face) if flipped else face
                out.append(((rx, ry), cell.rotate_dir_ccw_n(f, direction)))
            return tuple(out)

        hit = (place(cls.INPUTS), place(cls.OUTPUTS))
        Entity._faces_cache[key] = hit
        return hit

    @classmethod
    def faces_for(cls, x, y, direction, flipped=False):
        """(inputs, outputs) in world space for a placement.

        Each entry is ((cell_x, cell_y), face_direction). Faces are derived
        from the type and its orientation rather than stored per-instance --
        every instance of a type has the same port layout, so storage would be
        pure duplication. If ports ever become per-instance configurable this
        is where that changes.

        A cell and its faces must transform *together*: the flip is applied
        then the rotation, to both, which is what keeps a port on the outside
        of the footprint at every orientation. That work is done once per
        orientation by `faces_at_origin()`; this only translates the result,
        which is exact because a placement is a pure translation of the
        layout at the origin.
        """
        ins, outs = cls.faces_at_origin(direction, flipped)
        return ([((x + cx, y + cy), f) for ((cx, cy), f) in ins],
                [((x + cx, y + cy), f) for ((cx, cy), f) in outs])

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
    """A single-cell conveyor. Direction is which way items travel.

    Items in flight belong to the *segment* a belt is part of, not to the
    cell -- see core/segments.py -- so a belt row holds no item state. The
    **seed** is the one exception, and it is not item state: it is a standing
    instruction to put one item on this cell's run whenever that run is
    (re)derived, which is a property of the cell the player clicked and
    therefore has to live with the cell.

    That distinction is what decides where it goes. A segment is rebuilt from
    the grid and gets a fresh id every time, so storing the seed on the
    segment would lose it on the next edit -- which is precisely the moment
    it is supposed to fire.
    """

    TYPE_ID = cell.TYPE_BELT
    FOOTPRINT = (1, 1)

    #: Seed state, in the row's trailing vec4 (see ENTITY_STRIDE).
    #:
    #: `F_SEED_ON` is a flag rather than "a value of zero means off", because
    #: zero is a perfectly good thing to seed -- the same reason a Source's
    #: emit value needed `initialise()` to seed its default.
    F_SEED_ON = Entity._GATE_FIELDS_END + 0
    F_SEED_VALUE = Entity._GATE_FIELDS_END + 1

    #: What a freshly ticked seed box starts at.
    #:
    #: Zero, matching a Source's default (see `Entity.EMIT_VALUE`): the two
    #: are the only per-instance values the player configures, and they should
    #: start from the same neutral place. A seeded zero is still **visible** --
    #: the disc's ring tints by sign, and zero reads as red -- so ticking the
    #: box produces something to look at even though the number is nothing.
    SEED_VALUE = 0.0

    def initialise(self, x, y, direction, is_ghost=False, flipped=False,
                   seed_on=False, seed_value=None):
        # A belt is 1x1 with no ports, so `flipped` is meaningless for it --
        # accepted and passed through only so every entity type takes the
        # same orientation arguments and callers need no special case.
        super().initialise(x, y, direction, is_ghost=is_ghost, flipped=flipped)
        self._set(self.F_SEED_ON, 1.0 if seed_on else 0.0)
        self._set(self.F_SEED_VALUE,
                  float(self.SEED_VALUE if seed_value is None else seed_value))

    @property
    def seed_on(self):
        """Does this cell put an item on its run at every rebuild?"""
        return bool(self._get(self.F_SEED_ON))

    @seed_on.setter
    def seed_on(self, value):
        self._set(self.F_SEED_ON, 1.0 if value else 0.0)

    @property
    def seed_value(self):
        return float(self._get(self.F_SEED_VALUE))

    @seed_value.setter
    def seed_value(self, v):
        self._set(self.F_SEED_VALUE, float(v))


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

    #: R and F on a placed underground both mean "reverse this pair" rather
    #: than the generic turn -- a quarter turn would be footprint-legal on a
    #: 1x1 but would take this end off the axis it shares with its partner.
    #: Engine.turn_in_place() branches on the type for that; the flag stays
    #: True because the gesture *is* available, just differently defined.
    INPLACE_TURNABLE = True

    @property
    def is_exit(self):
        return bool(self._get(self.F_IS_EXIT))

    @is_exit.setter
    def is_exit(self, value):
        """Swap diving for surfacing. Callers must keep the pair coherent.

        The role is positional -- an exit sits downstream of its entrance --
        so changing it on one end alone describes a pair that cannot exist.
        Engine.reverse_under() is the supported way in: it reverses both ends
        and both facings together.
        """
        self._set(self.F_IS_EXIT, 1.0 if value else 0.0)

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


#: Largest magnitude a value may take. Beyond this a float32 is still exact
#: but the *integers* are not contiguous, so arithmetic stops behaving like
#: the numbers the player sees: 2^24 + 1 is not representable.
#:
#: Items are float32 (the entity array is destined for an SSBO), so this is
#: the honest ceiling rather than an arbitrary one. Division can produce
#: infinity and `floor(inf)` has no answer at all, so both clamp here instead
#: of propagating a value nothing downstream can use. A real decision about
#: saturating vs. wrapping vs. an error item is deferred -- see the Deferred
#: notes -- and this is the placeholder that keeps the board finite meanwhile.
VALUE_LIMIT = float(2 ** 24)


def clamp_value(v):
    """Bring a result into the representable-integer range.

    Infinities clamp to +/- VALUE_LIMIT. **NaN passes through unchanged**,
    deliberately: it is the one value that means "this computation had no
    answer", and collapsing it to a number would hide that. Max of a NaN
    reports NaN for exactly this reason.
    """
    if v != v:                       # NaN: no ordering, so no clamping
        return v
    if v > VALUE_LIMIT:
        return VALUE_LIMIT
    if v < -VALUE_LIMIT:
        return -VALUE_LIMIT
    return v


class Swap(Structure):
    """Conditional swap: x,y,z -> x, (z,y if x>0 else y,z).

    **Hidden from the toolbelt** -- it is absent from `STRUCTURES`, so no
    tool places it. The class stays registered and keeps TYPE_SWAP, so an
    older saved board still loads and draws one correctly. That split is the
    point: hiding a machine is a toolbelt decision, not a reason to
    invalidate saves.

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
        """x,y,z -> x, (z,y if x>0 else y,z).

        x is the control: it passes through unchanged (it is the declared
        PASS_THROUGH axis) and its sign decides whether y and z swap.
        A **positive** x swaps; zero or negative passes the pair straight
        through, so x reads as "swap when true" rather than the reverse.
        """
        x, y, z = values
        if x > 0.0:
            return [x, z, y]
        return [x, y, z]

    # x passes through; y and z are each selected from the pair on x's sign.
    OPS = (
        (Structure.OP_IN, 0),
        (Structure.OP_SEL, 0, 2, 1),
        (Structure.OP_SEL, 0, 1, 2),
    )


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

    OPS = (
        (Structure.OP_IN, 0),
        (Structure.OP_ADD, 0, 1),
    )


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

    OPS = (
        (Structure.OP_IN, 0),
        (Structure.OP_IN, 1),
        (Structure.OP_MAC, 0, 1, 2),
    )


class Reciprocal(Structure):
    """a -> 1/a. Authored as a 1x2 (tall) body, in at the top, out at the
    bottom.

    **No pass-through.** The single channel is mutated, so there is no
    control signal to draw -- `PASS_THROUGH` stays empty and no faint belt
    crosses the body. That is the visual distinction from Shear, where the
    overlay means "this signal survives unchanged".

    **Flipping reverses the flow.** The mirror is about the authored X axis,
    which exchanges north and south, so a flipped Reciprocal takes its input
    at the bottom and emits at the top. That makes `F` a genuinely useful
    second orientation here rather than a no-op -- and it is derived from the
    ports by `is_chiral()`, not declared.
    """

    TYPE_ID = cell.TYPE_RECIP
    FOOTPRINT = (1, 2)
    COLOUR = (0.66, 0.30, 0.30)   # red
    GLYPH = "divide"

    INPUTS = ((0, 1, cell.DIR_PY),)
    OUTPUTS = ((0, 0, cell.DIR_NY),)

    def fire(self, values):
        """a -> 1/a, with 1/0 clamped rather than infinite.

        Division by zero is the one input with no answer. It clamps to
        VALUE_LIMIT carrying the sign of the zero's reciprocal -- so +0 gives
        +limit and -0 gives -limit, which at least keeps the sign meaningful.
        """
        a, = values
        if a == 0.0:
            # math.copysign reads the sign *bit*, so -0.0 is distinguished
            # from +0.0 and the result keeps the direction the limit is
            # approached from.
            return [math.copysign(VALUE_LIMIT, a)]
        return [clamp_value(1.0 / a)]

    OPS = ((Structure.OP_RECIP, 0),)


class Max(Structure):
    """a,b -> max(a,b) on both outputs. Authored as a 2x1 (wide) body.

    Two inputs on the north faces of the two cells, two outputs on the south
    faces of the same two cells -- so it reads as two lanes in, two lanes
    out, both carrying the larger value.

    Both outputs carry the same number, which is what makes it a *fan-out*
    as well as a comparison: one lane can feed the next stage while the other
    is left dangling and destroyed. That needs no special case -- a dangling
    output is discarded for every gate now.

    **No pass-through**: neither lane survives unchanged (both become the
    max), so no control overlay is drawn.
    """

    TYPE_ID = cell.TYPE_MAX
    FOOTPRINT = (2, 1)
    COLOUR = (0.78, 0.50, 0.22)   # orange
    GLYPH = "chevrons"

    INPUTS = (
        (0, 0, cell.DIR_PY),   # a in
        (1, 0, cell.DIR_PY),   # b in
    )
    OUTPUTS = (
        (0, 0, cell.DIR_NY),   # max out, left
        (1, 0, cell.DIR_NY),   # max out, right
    )

    def fire(self, values):
        """a,b -> max,max. **NaN propagates** rather than being ordered.

        `max()` on a NaN is unspecified in the "which operand wins" sense --
        Python's builtin returns whichever it compared first -- so this tests
        explicitly. A NaN input means the comparison has no answer, and
        reporting NaN is more honest than silently preferring the other lane.
        """
        a, b = values
        if a != a or b != b:
            return [float("nan")] * 2
        m = clamp_value(a if a > b else b)
        return [m, m]

    # Both outputs carry the same value, so both name the same op.
    OPS = (
        (Structure.OP_MAX, 0, 1),
        (Structure.OP_MAX, 0, 1),
    )


class Floor(Structure):
    """a -> floor(a). Authored as a 2x1 (wide) body.

    In at the north face of the left cell, out at the east face of the right
    cell -- so the value takes the long way across the footprint, which is
    the idiom for a channel that gets mutated. **No pass-through**, since the
    one channel is the one being changed.

    The right cell carries no input, only the output: the width is there to
    give the value somewhere to travel and the glyph somewhere to sit.
    """

    TYPE_ID = cell.TYPE_FLOOR
    FOOTPRINT = (2, 1)
    COLOUR = (0.78, 0.72, 0.26)   # yellow
    GLYPH = "arrow_down"

    INPUTS = ((0, 0, cell.DIR_PY),)
    OUTPUTS = ((1, 0, cell.DIR_PX),)

    def fire(self, values):
        """a -> floor(a), clamped into the exact-integer range.

        `floor(inf)` has no value at all, and `math.floor` raises on it
        rather than returning one -- so the clamp has to happen *before* the
        floor, not after. NaN is passed through untouched for the same reason
        Max propagates it: it means the computation had no answer.
        """
        a, = values
        if a != a:
            return [a]
        return [float(math.floor(clamp_value(a)))]

    OPS = ((Structure.OP_FLOOR, 0),)


class Route(Structure):
    """a,b -> a down one of two outputs, chosen by the control b.

    Authored as a 2x1 (wide) body. The control enters the west face of the
    left cell and leaves the east face of the right cell **unchanged**, so it
    crosses the whole footprint and is the declared `PASS_THROUGH` axis --
    the faint belt overlay marks it, exactly as Swap's control does. The
    routed value enters the north face of the left cell and leaves by one of
    the two south faces.

    **b > 0 sends a to the right-hand output, otherwise to the left.** Zero
    counts as "left", so the control reads as a plain condition -- the same
    polarity convention Swap uses, where a positive control is the active
    case.

    This is the machine the `fire()`-returns-None contract exists for: it
    emits on exactly one of its two data channels per fire, and the other
    stays empty. Nothing in the tick special-cases it.
    """

    TYPE_ID = cell.TYPE_ROUTE
    FOOTPRINT = (2, 1)
    COLOUR = (0.38, 0.68, 0.86)   # sky blue
    GLYPH = "wye_down"

    INPUTS = (
        (0, 0, cell.DIR_PY),   # a in, the value being routed
        (0, 0, cell.DIR_NX),   # b in, the control
    )
    OUTPUTS = (
        (0, 0, cell.DIR_NY),   # a out, left  -- taken when b <= 0
        (1, 0, cell.DIR_NY),   # a out, right -- taken when b > 0
        (1, 0, cell.DIR_PX),   # b out, the control passing through
    )

    #: The control survives; `a` is merely redirected. Declared because
    #: geometry cannot tell a pass-through from a mutated channel.
    PASS_THROUGH = (cell.DIR_PX,)

    def fire(self, values):
        """a,b -> (a, None, b) or (None, a, b), on the sign of b.

        The declined channel returns None, which the structure phase skips --
        so one item goes one way and *nothing* goes the other, rather than a
        copy going both ways and one being destroyed.

        Note the control is a normal output and so takes part in back
        pressure: if its own path backs up, the whole gate stalls. That is
        correct -- the control is real material, not a discardable copy.
        """
        a, b = values
        if b > 0.0:
            return [None, a, b]
        return [a, None, b]

    # The two data paths are gated on opposite polarities of the control, so
    # exactly one emits; the control itself passes through. `OP_GATED`'s last
    # arg is the polarity: 1 means "emit when c > 0", 0 means "emit when not".
    OPS = (
        (Structure.OP_GATED, 1, 0, 0),
        (Structure.OP_GATED, 1, 0, 1),
        (Structure.OP_IN, 1),
    )


class Merge(Structure):
    """a,b,c -> whichever of a,b the control c selects, on one output.

    Authored as a 2x1 (wide) body, with Route's topology: the control enters
    the west face of the left cell and leaves the east face of the right cell
    unchanged (the declared `PASS_THROUGH`), `a` enters the north face of the
    left cell and `b` the north face of the right, and the merged value
    leaves the south face of the right cell.

    **c > 0 selects a, otherwise b** -- the same polarity as Route and Swap,
    where a positive control is the active case.

    Two things make this the most unusual machine here, and both are why the
    firing protocol grew:

    * **It does not wait for both data lanes.** Readiness depends on the
      control's *value*: with c held it needs only the lane c names. Neither
      FIRE_ALL (which would deadlock whenever the unused lane is empty) nor
      FIRE_ANY (which would fire with no control at all) expresses that, so
      it declares FIRE_SELECT and overrides `inputs_ready()`.
    * **The unused lane stays loaded.** `consumed()` reports only the control
      and the selected lane, so an item waiting on the other lane is neither
      destroyed nor emitted -- it sits in the machine until a later tick's
      control selects it. The default "consume every fed channel" would have
      silently eaten it.
    """

    TYPE_ID = cell.TYPE_MERGE
    FOOTPRINT = (2, 1)
    COLOUR = (0.24, 0.34, 0.66)   # dark blue
    GLYPH = "wye"
    FIRE_RULE = Structure.FIRE_SELECT

    INPUTS = (
        (0, 0, cell.DIR_PY),   # a in
        (1, 0, cell.DIR_PY),   # b in
        (0, 0, cell.DIR_NX),   # c in, the control
    )
    OUTPUTS = (
        (1, 0, cell.DIR_NY),   # the selected value
        (1, 0, cell.DIR_PX),   # c out, the control passing through
    )

    #: Channel indices, named because three of this machine's methods index
    #: them and an off-by-one would be a silent mis-wiring.
    CH_A, CH_B, CH_CONTROL = 0, 1, 2

    PASS_THROUGH = (cell.DIR_PX,)

    def _selected(self, control):
        """Which data channel the control names. c > 0 selects a."""
        return self.CH_A if control > 0.0 else self.CH_B

    def inputs_ready(self):
        """The control, plus the one lane it selects. Nothing else.

        Value-dependent, which is what FIRE_SELECT means and why the base
        class cannot answer this. Waiting for both lanes would deadlock the
        normal use -- two producers feeding one consumer, only one of which
        has anything at a given moment.
        """
        if not self.in_held(self.CH_CONTROL):
            return False
        return self.in_held(self._selected(self.in_value(self.CH_CONTROL)))

    def consumed(self, values):
        """The control and the selected lane. The other lane stays loaded.

        Returning the default (every fed channel) would destroy an item
        waiting on the lane this fire did not select -- material the player
        can see sitting in the machine.
        """
        control = values[self.CH_CONTROL]
        return [self.CH_CONTROL, self._selected(control)]

    def fire(self, values):
        """a,b,c -> (selected, c).

        Reads only the selected lane, which `inputs_ready()` guarantees is
        fed. The other may be None and is simply not looked at.
        """
        control = values[self.CH_CONTROL]
        return [values[self._selected(control)], control]

    # The selected lane, then the control passing through. Note `OPS`
    # describes the *arithmetic* only: Merge's readiness and its partial
    # consumption are value-dependent and live in `inputs_ready()` /
    # `consumed()`, which the opcode set deliberately does not try to
    # express -- see FIRE_SELECT.
    OPS = (
        (Structure.OP_SEL, CH_CONTROL, CH_A, CH_B),
        (Structure.OP_IN, CH_CONTROL),
    )


class Source(Structure):
    """2x1 emitter with outputs on its north and east faces only.

    Three ports: `+Y` on each of the two cells, and `+X` on the eastern cell.
    Authored at DIR_PX and rotated, so "north and east" means *relative to
    the machine* -- a Source rotated 180 degrees draws off south and west.

    **The three faces are three independent channels, not one folded
    channel** -- the opposite of Sink. A waiting zone is cleared by whoever
    drains it, so two belts sharing a channel would split one item per tick
    between them; separate channels let one Source fill several belts at
    once. The faces nobody connected simply stay empty rather than filling a
    zone and blocking the machine, which is what makes a multi-port generator
    usable at all (see GateSim._connected_channels()).

    It used to carry all six perimeter faces, which is where MAX_CHANNELS's
    value of 6 came from. Three is the deliberate smaller set: material comes
    off the two sides you are most likely to build along, and the machine
    reads less like a junction.

    **Dropping the -X and -Y faces makes this type chiral**, where the
    six-port version was symmetric about the authored X axis. Chirality is
    derived from the port layout rather than declared (`is_chiral()`), so `F`
    became a real mirror here with no change to the toolbelt -- the north
    faces stay put and the east port moves to the west cell's `-X`.

    **The emitted value is per instance**, stored in the row (see `F_EMIT`),
    so two Sources on one board can emit different numbers. `EMIT_VALUE` is
    now only the value a freshly placed one starts at.

    Even on x and odd on y, so it is the type that exercises both halves of
    the centred-placement rule at once -- see origin_for_cursor().
    """

    TYPE_ID = cell.TYPE_SOURCE
    FOOTPRINT = (2, 1)
    COLOUR = (0.42, 0.52, 0.60)
    GLYPH = "dot"

    INPUTS = ()
    OUTPUTS = (
        (0, 0, cell.DIR_PY),
        (1, 0, cell.DIR_PY), (1, 0, cell.DIR_PX),
    )

    #: Where this Source's own emit value lives in its row.
    #:
    #: Deliberately an **input** slot. A generator has no input channels at
    #: all -- `INPUTS` is empty, so `in_channels()` is 0 and nothing in the
    #: tick, the port pass or the renderer can ever read or write these
    #: twelve slots on a Source. Reusing one costs no stride, where appending
    #: a field would have pushed ENTITY_STRIDE from 32 to 36 and widened
    #: every row in the game (including every belt's) to carry a number only
    #: one machine has.
    #:
    #: `F_IN_HELD + 0` stays zero regardless, so even code that wrongly
    #: treated a Source as having an input would read the channel as unfed
    #: rather than as holding this value.
    F_EMIT = Structure.F_IN_VALUE

    def initialise(self, x, y, direction, is_ghost=False, flipped=False,
                   emit=None):
        """Seed the emit value, since `initialise()` zeroes the whole row.

        Done here rather than in `Gameboard.spawn()` so the board needs no
        per-type knowledge -- the same seam Underground uses for its role and
        partner fields. Without it a Source would spawn emitting 0.0, which is
        a perfectly legal value and therefore indistinguishable from a
        deliberate one.
        """
        super().initialise(x, y, direction, is_ghost=is_ghost, flipped=flipped)
        self._set(self.F_EMIT,
                  float(self.EMIT_VALUE if emit is None else emit))

    @property
    def emit_value(self):
        """What this Source emits, per instance.

        Zero in a freshly-spawned row, which is a legal emit value and not a
        sentinel -- `Gameboard.spawn()` is what seeds the class default, so a
        Source placed by any path starts at EMIT_VALUE and the player's
        edits persist from there.
        """
        return self._get(self.F_EMIT)

    @emit_value.setter
    def emit_value(self, v):
        self._set(self.F_EMIT, float(v))

    def fire(self, values):
        """Emit this instance's constant on every output channel.

        With no inputs this type is vacuously ready every tick, so it emits
        whenever its waiting zones are clear -- i.e. at whatever rate the
        belts downstream will take. That self-regulation falls out of the
        back-pressure rule rather than needing a rate here.

        Note this says *what* to emit, not *where* it can go: a channel whose
        face has nothing on the other side is skipped by the caller and its
        waiting zone stays empty. That check is topology and so lives in
        core/gates.py, the only place allowed to look at the board -- see
        GateSim._connected_channels(). Without it, five dangling zones would
        block this machine permanently the first time it fired.

        Reads `emit_value` rather than the class attribute, so two Sources on
        one board can emit different numbers. `EMIT_VALUE` survives as the
        value a newly placed one starts at.
        """
        return [self.emit_value] * self.out_channels()

    # Every channel carries the per-instance constant from the row, which is
    # why `OP_CONST` takes no argument: a kernel reads `F_EMIT` rather than
    # an input channel. This is also the one type whose `OPS` length is a
    # function of the *instance* rather than the class, since out_channels()
    # follows the declared ports -- the self-check derives it per type.
    OPS = ((Structure.OP_CONST,),) * 3


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

    **Its eight faces fold onto four channels, one per footprint cell** --
    which is a throughput decision, not a tidiness one. See `in_channel()`.
    """

    TYPE_ID = cell.TYPE_SINK
    FOOTPRINT = (2, 2)
    COLOUR = (0.34, 0.34, 0.40)
    GLYPH = "dot"
    FIRE_RULE = Structure.FIRE_ANY

    #: Grouped in pairs, one pair per footprint cell, because `in_channel()`
    #: folds by `port_index // 2`. Reordering these without reordering the
    #: fold would put two faces of different cells on one channel.
    INPUTS = (
        (0, 1, cell.DIR_NX), (0, 1, cell.DIR_PY),   # top-left corner
        (1, 1, cell.DIR_PY), (1, 1, cell.DIR_PX),   # top-right
        (1, 0, cell.DIR_PX), (1, 0, cell.DIR_NY),   # bottom-right
        (0, 0, cell.DIR_NY), (0, 0, cell.DIR_NX),   # bottom-left
    )
    OUTPUTS = ()

    @classmethod
    def in_channel(cls, port_index):
        """Two faces per channel, grouped by the footprint cell they sit on.

        **A channel is one item per tick, because it is one slot.** `in_held`
        is a single flag per channel, so once a belt has loaded a channel,
        `GateSim.accept_from()` refuses every other belt on that channel
        until the structure phase clears it. That is what a fold costs, and
        it is the same arithmetic that stops Source from folding -- there, a
        shared channel splits one *emit* per tick between belts; here, it
        splits one *drain*.

        All eight faces used to fold onto channel 0, on the reasoning that
        eight ways into one consumer are not eight independent values. True
        of the *semantics* -- a sink destroys everything, so the values really
        are interchangeable -- and wrong about the rate: it capped the whole
        machine at one item per tick, so pointing two saturated belts at a
        sink got one belt's throughput. A sink is meant to eat several
        saturated belts at once.

        Folding per cell rather than per face is the compromise that keeps
        the entity row narrow. Four channels is a 2x2's cell count, so each
        of the four tiles drains independently -- four items per tick, from
        up to four separate belts -- while `MAX_CHANNELS` stays at 6 (set by
        Source) and `ENTITY_STRIDE` stays at 32. One channel per *face* would
        allow eight, and would widen every row in the game to do it.

        The cost is that the two faces of a single corner tile still share a
        slot, so feeding one tile from both its sides gets one item per tick
        between them. Spread the belts across tiles and each gets its own.
        """
        return port_index // 2


#: Every placeable structure, in the order the toolbelt's **default** layout
#: offers them. The belt tools take the first slots, so this list starts after
#: them -- but the toolbelt is customisable and persisted now, so this is the
#: starting arrangement rather than the arrangement.
#:
#: `Swap` and `Sink` are back. They were hidden when the toolbelt had exactly
#: ten slots and every one was spoken for, which made "is this machine worth a
#: slot?" a real question -- Sink lost it to the rule that a dangling output
#: destroys what it is sent, and so does a sink's job without taking a slot.
#: With two rows and a right-click menu that question is gone: a machine the
#: player never picks costs nothing, and *not* offering one meant `Q` on a
#: Sink already on the board did nothing at all, since the pipette resolves a
#: type through its tool. Sink is still redundant for discarding an output --
#: leave the face unconnected instead -- but it is no longer unreachable.
STRUCTURES = (Shear, Source, Route, Merge, Reciprocal, Max, Mac, Floor,
              Swap, Sink)

#: Machines defined but not offered by the toolbelt's type menu.
#:
#: Empty today. Kept as a mechanism rather than deleted, because the split it
#: expresses is the load-bearing one: `STRUCTURES` is what the *tools* derive
#: from and `ALL_STRUCTURES` is what anything reasoning about *types*
#: iterates. A machine put here stays registered with its type id, so a saved
#: board containing one still loads, draws and simulates -- only the tool goes
#: away. That asymmetry is what made hiding a machine cost one line, and it is
#: worth keeping available.
HIDDEN_STRUCTURES = ()

#: Every machine, placeable or not. What anything reasoning about *types*
#: rather than about *tools* should iterate.
ALL_STRUCTURES = STRUCTURES + HIDDEN_STRUCTURES


def eval_ops(ops, values, const=0.0):
    """Evaluate a declared `OPS` table. The reference the GLSL transcribes.

    `values` are the input channel values (None for an unfed channel),
    `const` the per-instance constant `OP_CONST` reads. Returns a list the
    same shape `fire()` returns, with None meaning "emit nothing".

    This is the opcode set's *meaning*, written once. `fire()` stays the
    authority for the CPU tick -- this exists so the declaration can be
    checked against it (see `_self_check`) and so the GLSL has a definition
    to be a transcription of rather than an invention. Same relationship
    `BeltSim.position_of()` has to `item_position()` in common.glsl.

    Deliberately total: every opcode is defined for every float input, with
    no raising and no unreachable branch. A kernel evaluating this needs no
    guards, which is the property the opcode set was chosen for.
    """
    E = Entity
    out = []
    for entry in ops:
        op, args = entry[0], entry[1:]

        def val(i):
            v = values[i]
            return 0.0 if v is None else float(v)

        if op == E.OP_NONE:
            out.append(None)
        elif op == E.OP_IN:
            out.append(val(args[0]))
        elif op == E.OP_ADD:
            out.append(val(args[0]) + val(args[1]))
        elif op == E.OP_MAC:
            out.append(val(args[2]) + val(args[0]) * val(args[1]))
        elif op == E.OP_RECIP:
            a = val(args[0])
            # 1/0 has no answer; clamp carrying the sign *bit* of the zero so
            # +0 and -0 stay distinguishable. Mirrors Reciprocal.fire().
            out.append(math.copysign(VALUE_LIMIT, a) if a == 0.0
                       else clamp_value(1.0 / a))
        elif op == E.OP_MAX:
            a, b = val(args[0]), val(args[1])
            # NaN propagates rather than being ordered -- see Max.fire().
            out.append(float("nan") if (a != a or b != b)
                       else clamp_value(a if a > b else b))
        elif op == E.OP_FLOOR:
            a = val(args[0])
            # Clamp *before* flooring: floor(inf) has no value at all.
            out.append(a if a != a else float(math.floor(clamp_value(a))))
        elif op == E.OP_SEL:
            out.append(val(args[1]) if val(args[0]) > 0.0 else val(args[2]))
        elif op == E.OP_GATED:
            # Emit only on the declared polarity of the control; the other
            # channel stays silent, which is how Route sends one item one way
            # rather than a copy both ways.
            emit = (val(args[0]) > 0.0) == bool(args[2])
            out.append(val(args[1]) if emit else None)
        elif op == E.OP_CONST:
            out.append(float(const))
        else:
            raise ValueError(f"unknown opcode {op}")
    return out


def _check_ops_match_fire():
    """Every declared `OPS` must agree with that type's `fire()`.

    The cross-validation that makes `OPS` a second *expression* of the
    arithmetic rather than a second *truth*. Without it the table is a
    hand-written mirror of exactly the kind generating the GLSL tables was
    meant to abolish -- and a wrong opcode draws a perfectly crisp wrong
    number, with no visual artefact to notice.

    Swept over values chosen to separate the machines that are easy to
    confuse: both signs (every control is "positive means act"), zero (the
    inactive branch *and* Reciprocal's special case), and a NaN (which Max
    and Floor propagate and nothing else should invent).
    """
    probes = (-2.5, -1.0, -0.0, 0.0, 0.5, 1.0, 3.0, float("nan"))
    for cls in ALL_STRUCTURES:
        ops = cls.OPS
        if not ops:
            continue
        n_in = cls.in_channels()
        n_out = cls.out_channels()
        assert len(ops) == n_out, (
            f"{cls.__name__}: {len(ops)} opcodes for {n_out} output channels")

        # A fake instance over a one-row stub table. Every type declaring OPS
        # reads only `values`, except Source, whose `emit_value` is a row
        # property -- so the instance needs a board rather than a patched
        # __dict__.
        # A dict keyed by `(row, offset)` rather than numpy: `_get`/`_set`
        # index with that tuple and nothing here needs an array, and
        # entity.py is otherwise numpy-free -- a self-check is not a reason
        # to give the module a dependency.
        class _Cells(dict):
            def __missing__(self, _key):
                return 0.0

        class _Dirty:
            def add(self, _row):
                pass

        class _Stub:
            def __init__(self):
                self.entities = _Cells()
                self.entities_dirty = _Dirty()

        inst = cls.__new__(cls)
        inst.board = _Stub()
        inst.row = 0
        inst._ghost = None
        for i, a in enumerate(probes):
            # Rotate the probe list so each channel sees a different value --
            # which is what makes a swapped pair of channels visible. Equal
            # inputs would let Shear's (x, x+y) pass as (y, x+y).
            values = [probes[(i + k) % len(probes)] for k in range(n_in)]
            const = a
            if n_in == 0:
                inst.board.entities[0, cls.F_EMIT] = const
            try:
                got = cls.fire(inst, list(values))
            except Exception as exc:   # pragma: no cover - a real failure
                raise AssertionError(
                    f"{cls.__name__}.fire({values}) raised {exc!r}") from exc
            want = eval_ops(ops, values, const)
            assert len(got) == len(want), (
                f"{cls.__name__}: fire gave {len(got)} results, OPS "
                f"{len(want)}")
            for ch, (g, w) in enumerate(zip(got, want)):
                if g is None or w is None:
                    assert (g is None) == (w is None), (
                        f"{cls.__name__} ch{ch} on {values}: fire gave {g!r}, "
                        f"OPS gave {w!r} -- one emitted and the other did not")
                    continue
                same = (g == w) or (g != g and w != w)   # NaN == NaN
                assert same, (
                    f"{cls.__name__} ch{ch} on {values}: fire gave {g}, "
                    f"OPS gave {w}")


def _self_check():
    """The field layout must fit the stride, and ids must be unique."""
    assert _COMMON_FIELDS <= ENTITY_STRIDE
    # The gate block must fit, and every machine's ports must fit MAX_PORTS.
    assert Entity._GATE_FIELDS_END <= ENTITY_STRIDE, (
        f"gate fields need {Entity._GATE_FIELDS_END} slots, stride is "
        f"{ENTITY_STRIDE}")
    assert ENTITY_STRIDE % 4 == 0, "rows must land on vec4 boundaries"
    for c in ALL_STRUCTURES:
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
    # A positive control swaps -- "swap when the condition holds". The two
    # branches were the other way round at first, which reads backwards:
    # the machine sat swapping whenever its control said not to.
    sw = _view(Swap)
    assert sw.fire([1.0, 7.0, 9.0]) == [1.0, 9.0, 7.0], "x>0 must swap"
    assert sw.fire([-1.0, 7.0, 9.0]) == [-1.0, 7.0, 9.0], "x<0 must not swap"
    assert sw.fire([0.0, 7.0, 9.0]) == [0.0, 7.0, 9.0], "x==0 means no swap"

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
    # A bare view sits on a zeroed row, so its emit value is 0.0. Note this
    # now *coincides* with EMIT_VALUE rather than differing from it, so this
    # pair of assertions no longer distinguishes "spawn() seeded the default"
    # from "nobody wrote anything" -- the explicit non-default checks below
    # are what carry that weight. Assert what fire() actually promises:
    # whatever this instance holds, on every channel.
    assert src.emit_value == 0.0, "a zeroed row should read as 0.0"
    assert src.fire([]) == [0.0] * Source.out_channels()
    assert len(src.fire([])) == Source.out_channels()

    # The value is per instance, and fire() must follow it rather than the
    # class attribute. This is the whole point of F_EMIT living in the row.
    src.emit_value = 7.5
    assert src.emit_value == 7.5
    assert src.fire([]) == [7.5] * Source.out_channels(), (
        "fire() must read the instance's value, not Source.EMIT_VALUE")

    # It lives in an *input* slot, which is safe only because a generator has
    # no input channels. If Source ever gains one, F_EMIT must move.
    assert Source.in_channels() == 0, (
        "Source.F_EMIT squats on an input slot; giving Source an input "
        "channel would alias its emit value onto that channel")
    assert Source.F_EMIT == Structure.F_IN_VALUE
    # The occupancy flag stays clear, so nothing reads the emit value as a
    # held input even by mistake.
    assert not src.in_held(0)

    # --- a belt's seed ----------------------------------------------------
    # The seed lives *past* the gate block rather than aliasing onto it. A
    # belt reads none of those 24 slots today, so reuse would work -- and
    # would make a belt's seed and a machine's channel one piece of storage,
    # which stops being harmless the moment a belt gains a channel.
    assert Belt.F_SEED_ON >= Entity._GATE_FIELDS_END, (
        "a belt's seed must not alias the gate block")
    assert Belt.F_SEED_VALUE < ENTITY_STRIDE, (
        f"seed value at {Belt.F_SEED_VALUE} is past the {ENTITY_STRIDE}-slot "
        f"row")

    # --- the gate sleep diagnostic flag -----------------------------------
    # Same reasoning as the seed above, one slot further on: past the gate
    # block (so it cannot alias a channel), inside the stride (so it needs no
    # save-invalidating widening), and clear of the belt seed. The last is the
    # one that would bite -- a belt is never a gate, so nothing writes both,
    # but an overlap would still be a silent two-writers bug if that ever
    # changed.
    assert Entity.F_ASLEEP >= Entity._GATE_FIELDS_END, (
        "the sleep flag must not alias the gate channel block")
    assert Entity.F_ASLEEP < ENTITY_STRIDE, (
        f"sleep flag at {Entity.F_ASLEEP} is past the {ENTITY_STRIDE}-slot "
        f"row")
    assert Entity.F_ASLEEP not in (Belt.F_SEED_ON, Belt.F_SEED_VALUE), (
        "the sleep flag must not alias a belt's seed")

    blt = _view(Belt)
    # A zeroed row reads as "not seeded", which is the right default: a belt
    # the player has not touched must not inject anything.
    assert not blt.seed_on and blt.seed_value == 0.0

    # initialise() seeds the *value* default but leaves the flag off, so a
    # belt drawn by the ordinary path never injects until it is asked to.
    #
    # Dirty both fields first. SEED_VALUE is 0.0, which a zeroed row already
    # reads as, so initialising a fresh view would assert nothing about
    # whether initialise() writes the field at all -- it has to be seen
    # *overwriting* something. Same reasoning applies to the flag: a recycled
    # row can arrive with a seed left on it.
    blt.seed_on = True
    blt.seed_value = 99.0
    blt.initialise(1, 2, cell.DIR_PX)
    assert not blt.seed_on, "a freshly placed belt must not be seeded"
    assert blt.seed_value == Belt.SEED_VALUE, blt.seed_value

    # Zero is a legal seed value, not a sentinel -- which is the whole reason
    # the flag exists separately from the value.
    blt.seed_on = True
    blt.seed_value = 0.0
    assert blt.seed_on and blt.seed_value == 0.0, (
        "zero must be a seedable value, distinct from 'not seeded'")
    blt.seed_value = -2.5
    assert blt.seed_value == -2.5

    # And the explicit spawn path carries both halves, which is what a paste
    # and a load need.
    seeded_belt = _view(Belt)
    seeded_belt.initialise(0, 0, cell.DIR_PY, seed_on=True, seed_value=3.5)
    assert seeded_belt.seed_on and seeded_belt.seed_value == 3.5

    # initialise() seeds the class default. Dirtied first for the same reason
    # the belt's seed is: EMIT_VALUE is 0.0 and so is a zeroed row, and an
    # assertion that cannot tell "wrote the default" from "wrote nothing"
    # would survive initialise() dropping the field entirely.
    seeded = _view(Source)
    seeded.emit_value = 99.0
    seeded.initialise(3, 4, cell.DIR_PX)
    assert seeded.emit_value == Source.EMIT_VALUE, seeded.emit_value
    override = _view(Source)
    override.initialise(3, 4, cell.DIR_PX, emit=2.25)
    assert override.emit_value == 2.25
    snk = _view(Sink)
    assert snk.fire([None] * Sink.in_channels()) == [], "a sink emits nothing"

    # Sink's eight faces fold onto four channels, one per footprint cell.
    # Pinned because the fold is what sets the machine's throughput: one
    # channel is one slot is one item per tick, so collapsing these to a
    # single channel silently caps a sink at one belt's worth of drain.
    assert Sink.in_channels() == 4, Sink.in_channels()
    assert Sink.out_channels() == 0
    fw, fh = Sink.FOOTPRINT
    assert Sink.in_channels() == fw * fh, "one channel per footprint cell"

    # Each channel must cover exactly the faces of one cell, and each cell
    # must get its own channel. This is the pairing in_channel() assumes of
    # INPUTS' ordering, and the thing that breaks silently if either is
    # reordered without the other.
    by_channel = {}
    for i, (lx, ly, _face) in enumerate(Sink.INPUTS):
        by_channel.setdefault(Sink.in_channel(i), set()).add((lx, ly))
    assert len(by_channel) == 4, by_channel
    for ch, cells in by_channel.items():
        assert len(cells) == 1, (
            f"Sink channel {ch} spans {sorted(cells)} -- a channel must "
            f"belong to one cell, or two tiles share one item per tick")
    assert len({next(iter(c)) for c in by_channel.values()}) == 4, (
        "two channels on the same cell")

    # Every machine returns exactly one value per output channel. Covers the
    # hidden machines too: an old save can still put one on the board.
    for c, args in ((Swap, [1.0, 2.0, 3.0]), (Shear, [1.0, 2.0]),
                    (Mac, [1.0, 2.0, 3.0]), (Source, []),
                    (Sink, [1.0] * Sink.in_channels()),
                    (Reciprocal, [2.0]), (Max, [1.0, 2.0]),
                    (Floor, [1.5]), (Route, [5.0, 1.0]),
                    (Merge, [5.0, 6.0, 1.0])):
        got = _view(c).fire(args)
        assert len(got) == c.out_channels(), (
            f"{c.__name__}.fire() returned {len(got)} values for "
            f"{c.out_channels()} output channels")

    # --- the new arithmetic -----------------------------------------------
    # Reciprocal: 1/a, with the zero case clamped rather than infinite.
    rc = _view(Reciprocal)
    assert rc.fire([2.0]) == [0.5]
    assert rc.fire([-4.0]) == [-0.25]
    assert rc.fire([0.0]) == [VALUE_LIMIT], "1/0 must clamp, not be inf"
    assert rc.fire([-0.0]) == [-VALUE_LIMIT], "1/-0 keeps the sign"
    assert math.isfinite(rc.fire([1e-30])[0]), "a tiny divisor must not be inf"
    assert not Reciprocal.PASS_THROUGH, "a mutated channel is not a control"

    # Max: both outputs carry the larger value, and NaN propagates.
    mx = _view(Max)
    assert mx.fire([3.0, 7.0]) == [7.0, 7.0]
    assert mx.fire([7.0, 3.0]) == [7.0, 7.0]
    assert mx.fire([-5.0, -2.0]) == [-2.0, -2.0]
    assert mx.fire([4.0, 4.0]) == [4.0, 4.0]
    _nan = float("nan")
    for args in ([_nan, 1.0], [1.0, _nan]):
        got = mx.fire(args)
        assert all(v != v for v in got), (
            f"Max of a NaN must report NaN, got {got}")
    assert not Max.PASS_THROUGH

    # Floor: rounds toward -inf, and infinities clamp *before* the floor --
    # math.floor(inf) raises rather than returning a value.
    fl = _view(Floor)
    assert fl.fire([1.7]) == [1.0]
    assert fl.fire([-1.2]) == [-2.0], "floor goes toward -inf, not toward 0"
    assert fl.fire([3.0]) == [3.0]
    assert fl.fire([float("inf")]) == [VALUE_LIMIT]
    assert fl.fire([float("-inf")]) == [-VALUE_LIMIT]
    assert fl.fire([_nan])[0] != fl.fire([_nan])[0], "NaN must pass through"

    # Route: one item down one path, *nothing* down the other, plus the
    # control passing through. The None is the whole mechanism.
    rt = _view(Route)
    assert rt.fire([5.0, 1.0]) == [None, 5.0, 1.0], "c>0 routes right"
    assert rt.fire([5.0, 0.0]) == [5.0, None, 0.0], "c==0 routes left"
    assert rt.fire([5.0, -1.0]) == [5.0, None, -1.0], "c<0 routes left"
    for ctrl in (1.0, 0.0, -1.0):
        got = rt.fire([5.0, ctrl])
        assert sum(1 for v in got[:2] if v is not None) == 1, (
            f"exactly one data output must fire, got {got}")
    assert Route.PASS_THROUGH == (cell.DIR_PX,), "the control survives"

    # Merge: the control selects a lane, and the *other lane is not touched*.
    mg = _view(Merge)
    assert mg.fire([5.0, 6.0, 1.0]) == [5.0, 1.0], "c>0 selects a"
    assert mg.fire([5.0, 6.0, 0.0]) == [6.0, 0.0], "c<=0 selects b"
    assert mg.fire([5.0, 6.0, -2.0]) == [6.0, -2.0]
    # It must not read the unselected lane at all, which is what lets it fire
    # with that lane empty.
    assert mg.fire([5.0, None, 1.0]) == [5.0, 1.0], (
        "selecting a must not require b")
    assert mg.fire([None, 6.0, 0.0]) == [6.0, 0.0], (
        "selecting b must not require a")
    assert Merge.PASS_THROUGH == (cell.DIR_PX,)

    # --- readiness --------------------------------------------------------
    # FIRE_ALL waits for every input; FIRE_ANY needs one. A type with no
    # inputs is vacuously ready under either, which is what makes Source
    # fire with no special case anywhere.
    assert Sink.FIRE_RULE == Entity.FIRE_ANY, "a sink fires on any input"
    for c in (Swap, Shear, Mac, Source, Reciprocal, Max, Floor, Route):
        assert c.FIRE_RULE == Entity.FIRE_ALL, c.__name__
    assert Merge.FIRE_RULE == Entity.FIRE_SELECT

    # A type declaring FIRE_SELECT **must** override inputs_ready(): the base
    # class cannot resolve a value-dependent rule, and would fall through to
    # the FIRE_ALL branch -- making the machine wait for a lane it is
    # supposed to be able to ignore. Silent, and exactly the bug the enum
    # value exists to make visible.
    for c in ALL_STRUCTURES:
        if c.FIRE_RULE == Entity.FIRE_SELECT:
            assert c.inputs_ready is not Entity.inputs_ready, c.__name__
            assert "inputs_ready" in vars(c), (
                f"{c.__name__} declares FIRE_SELECT without overriding "
                f"inputs_ready()")

    # Merge's readiness: the control plus the selected lane, nothing more.
    mg = _view(Merge)
    assert not mg.inputs_ready(), "nothing fed"
    mg.set_input(Merge.CH_A, 5.0)
    assert not mg.inputs_ready(), "a alone is not enough -- no control"
    mg.set_input(Merge.CH_CONTROL, 1.0)
    assert mg.inputs_ready(), "control selecting a, and a is held"
    # Flip the control to select the *empty* lane and it must stall again.
    mg.set_input(Merge.CH_CONTROL, -1.0)
    assert not mg.inputs_ready(), (
        "the control selects b, which is empty -- it must wait")
    mg.set_input(Merge.CH_B, 6.0)
    assert mg.inputs_ready()

    # And consumption leaves the unselected lane alone. This is the claim
    # that `consumed()` exists for: the default would eat both.
    mg = _view(Merge)
    mg.set_input(Merge.CH_A, 5.0)
    mg.set_input(Merge.CH_B, 6.0)
    mg.set_input(Merge.CH_CONTROL, 1.0)          # selects a
    vals = [mg.in_value(i) if mg.in_held(i) else None for i in range(3)]
    taken = sorted(mg.consumed(vals))
    assert taken == sorted([Merge.CH_A, Merge.CH_CONTROL]), taken
    assert Merge.CH_B not in taken, (
        "the unselected lane must stay loaded, not be destroyed")

    # Every other machine consumes every fed channel, which is the default.
    sh_c = _view(Shear)
    sh_c.set_input(0, 1.0)
    sh_c.set_input(1, 2.0)
    assert sorted(sh_c.consumed([1.0, 2.0])) == [0, 1]
    # ...and an unfed channel is not reported, so FIRE_ANY does not clear
    # slots that never arrived.
    sk_c = _view(Sink)
    sk_c.set_input(2, 9.0)
    assert sk_c.consumed([None, None, 9.0, None]) == [2]

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

    # A sink's channels are independent slots: filling one must leave the
    # others free to take their own item the same tick. That is the whole
    # point of the per-cell fold -- if these shared a slot, a second belt
    # would find the sink full and the machine would drain one item per tick
    # however many belts fed it.
    v = _view(Sink)
    for ch in range(Sink.in_channels()):
        assert not v.in_held(ch), f"channel {ch} should start empty"
    for ch in range(Sink.in_channels()):
        v.set_input(ch, float(ch))
    for ch in range(Sink.in_channels()):
        assert v.in_held(ch) and v.in_value(ch) == float(ch), (
            f"channel {ch} did not hold its own value -- channels are "
            f"aliasing, so the fold is not independent")

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
    for c in ALL_STRUCTURES:
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

    # Source: a 2x1 emitting north and east only -- +Y on both cells, +X on
    # the eastern one. Three ports, each its own channel.
    assert Source.FOOTPRINT == (2, 1)
    assert not Source.INPUTS and len(Source.OUTPUTS) == 3
    assert Source.out_channels() == 3, Source.out_channels()
    assert Source.face_masks_at(0, 0) == (0, 1 << cell.DIR_PY)
    assert Source.face_masks_at(1, 0) == (
        0, (1 << cell.DIR_PY) | (1 << cell.DIR_PX))
    _, souts = Source.faces_for(0, 0, cell.DIR_PX)
    assert len(set(souts)) == 3, souts
    # The dropped faces really are gone, not merely unlisted somewhere.
    for gone in (((0, 0), cell.DIR_NX), ((0, 0), cell.DIR_NY),
                 ((1, 0), cell.DIR_NY)):
        assert gone not in souts, f"{gone} should no longer be a port"
    assert ((0, 0), cell.DIR_PX) not in souts, "interior face is not a port"
    assert ((1, 0), cell.DIR_NX) not in souts, "interior face is not a port"
    # Three independent channels, not one folded channel: a waiting zone is
    # cleared by whoever drains it, so folding would make two belts split
    # one item per tick instead of each getting their own.
    assert len({Source.out_channel(i) for i in range(3)}) == 3
    # Firing produces one value per channel, all of them this instance's.
    _s = _view(Source)
    _s.initialise(0, 0, cell.DIR_PX)
    assert _s.fire([]) == [Source.EMIT_VALUE] * 3

    # Asymmetric about the authored X axis now that -X and -Y are gone, so
    # the mirror is a genuinely different machine and F does something. The
    # six-port version was symmetric and F was a no-op on it; nothing in the
    # toolbelt had to change, because chirality is derived from the ports.
    assert Source.is_chiral(), (
        "a north/east Source should be chiral -- if this fails, is_chiral() "
        "is not seeing the dropped faces")

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
    for c in ALL_STRUCTURES:
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
    for c in ALL_STRUCTURES:
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
    for c in ALL_STRUCTURES:
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
    for c in ALL_STRUCTURES:
        out_faces = {f for _c, f in c.faces_for(0, 0, cell.DIR_PX)[1]}
        for f in c.PASS_THROUGH:
            assert f in out_faces, (c.__name__, cell.DIR_NAMES[f])
    # The machines carrying a control signal declare it; the ones that mutate
    # every channel they touch must not, or the overlay would promise a
    # pass-through the arithmetic does not honour.
    for c in (Swap, Shear, Mac, Route, Merge):
        assert c.PASS_THROUGH, c.__name__
    for c in (Source, Sink, Reciprocal, Max, Floor):
        assert not c.PASS_THROUGH, c.__name__

    # Chirality is derived from the ports, so it cannot drift from them.
    # Asserted against `is_chiral()` per type rather than against a hardcoded
    # list, so adding or hiding a machine cannot leave a stale expectation --
    # the two loops below are what actually pin the derivation, and this is
    # only the inventory.
    # Only Sink is achiral: its ports cover the whole perimeter, so mirroring
    # permutes them onto each other. Every other machine is chiral, including
    # the ones whose ports sit on the body's centre line -- the flip mirrors
    # about the authored *X* axis, which exchanges north and south, so a
    # Reciprocal's north input lands where its south output was. Flipping one
    # therefore reverses which way material flows through it, which is a
    # genuinely useful second orientation rather than a no-op.
    assert not Sink.is_chiral(), "a full perimeter is symmetric"
    for c in (Swap, Shear, Mac, Source, Reciprocal, Max, Floor, Route, Merge):
        assert c.is_chiral(), c.__name__

    # An achiral type's flipped placement is identical to its plain one, and a
    # chiral type's really differs -- at every facing. Derived from
    # is_chiral() so a new machine is covered automatically, whichever it is.
    # Compares *both* port sets: checking only inputs passes vacuously for a
    # type with none, which is exactly Source's case.
    for c in ALL_STRUCTURES:
        for d in range(4):
            plain, flipped = c.faces_for(0, 0, d, False), \
                c.faces_for(0, 0, d, True)
            same = (sorted(plain[0]) == sorted(flipped[0])
                    and sorted(plain[1]) == sorted(flipped[1]))
            if c.is_chiral():
                assert not same, (
                    f"{c.__name__} claims chirality but mirrors to itself at "
                    f"{cell.DIR_NAMES[d]}")
            else:
                assert same, (
                    f"{c.__name__} claims to be achiral but its mirror "
                    f"differs at {cell.DIR_NAMES[d]}")

    assert Belt.ROTATABLE is False

    # The opcode tables against the methods they describe. Last, because it
    # is the only check here that depends on ALL_STRUCTURES being complete.
    _check_ops_match_fire()


_self_check()
