"""Where a tick's time actually goes, on the board the player is looking at.

[tools/profile_tick.py](../tools/profile_tick.py) already answers this for
*synthetic* boards, and that is the right tool for justifying an optimisation:
it builds a board of a known shape, sweeps the scale, and isolates one cost at
a time. What it cannot do is answer "why is **my quicksave** slow", which is a
different question with a different answer -- a real board has a mix of
saturated trace and sleeping memory that no synthetic shape reproduces.

So this is the in-app counterpart: arm it, let the running simulation tick,
and dump the breakdown to the console. The numbers come from the same ticks
the player is watching, which is the whole point.

Design
------

**Nothing is measured unless it is armed.** `Profiler.armed` is False by
default and every probe is a single attribute test behind it, so the disabled
cost is what the panel's window guard costs -- see the segment inspector,
which makes the same bargain. The simulation must not get slower for carrying
a tool that measures how fast it is.

**Timings are a tree, not a flat table.** "The belt phase took 0.5ms" is the
number `tools/profile_tick.py` already reports; the useful follow-up is which
*part* -- the transport loop, the port callbacks, the direct deliveries. Each
probe names a path (`belt`, `belt/transport`) and the dump indents by depth,
so a child's share of its parent is readable without arithmetic.

**Wall clock, with `perf_counter`.** Not `process_time`: the tick is pure
Python and numpy with no I/O, so the two agree, and wall clock is what the
frame budget is actually spent against. `perf_counter` rather than `time`
because the quantities here are tens of microseconds and `time()`'s
resolution on Windows is ~16ms -- it would report zero for everything.

**A sample is a tick, not a frame.** A frame may run zero ticks (the clock
banks sub-period time) or several (catch-up), and averaging per frame would
mix those. The counter is incremented by the clock's own tick, so "mean per
tick" means exactly that.

Why the aggregates are what they are
------------------------------------

Mean alone hides the thing most worth finding: a tick that is cheap 99 times
and expensive once is a *stall*, and at 10 ticks/sec one 90ms tick is a
visible hitch that a 1ms mean conceals completely. So each path carries
count/mean/max, and the dump sorts by **total** -- the share of the budget --
rather than by mean, because a cheap probe called thousands of times is a
real cost and a sort by mean buries it.

`max` is kept rather than a percentile: the samples are not stored (a long
session would be unbounded), and for finding a hitch the single worst tick is
the number that matters. A full histogram is the thing to add if the max
turns out to need context.
"""

import time

#: Order the top-level phases are reported in, whatever order they were first
#: timed in. A dump whose sections move between runs is hard to compare
#: against the previous one, and the phase order is the sim's own.
PHASE_ORDER = ("structure", "belt")

#: Roots that are **not** part of a tick and so are excluded from the tick
#: total and the share column, reported separately instead.
#:
#: A segment rebuild is the only one so far. It happens at most once a frame
#: and only after an edit, so averaging it into a per-tick cost describes
#: neither: ~88ms once when a belt is placed reads as "the tick is 88ms" if
#: divided by one tick, and as nothing at all if divided by 200. The player's
#: two questions -- "why is my tick slow" and "why did that placement
#: stutter" -- want the same timings presented differently, so both are
#: collected and the dump keeps them apart.
OUT_OF_TICK = ("rebuild",)


class _Stat:
    """Running aggregate for one timed path. No samples stored."""

    __slots__ = ("count", "total", "worst")

    def __init__(self):
        self.count = 0
        self.total = 0.0
        self.worst = 0.0

    def add(self, dt):
        self.count += 1
        self.total += dt
        if dt > self.worst:
            self.worst = dt

    @property
    def mean(self):
        return self.total / self.count if self.count else 0.0


class _Span:
    """The context manager a probe returns. Reusable, allocation-free.

    One instance per path rather than a fresh object per `with`: a probe runs
    once per tick per segment in the hot callbacks, and allocating a context
    manager there would be measuring the profiler. The instance holds the
    start time of the *current* span, which is safe because a path is never
    re-entered -- a probe nests inside its parent, not inside itself.
    """

    __slots__ = ("stat", "_t0")

    def __init__(self, stat):
        self.stat = stat
        self._t0 = 0.0

    def __enter__(self):
        self._t0 = time.perf_counter()
        return self

    def __exit__(self, *exc):
        self.stat.add(time.perf_counter() - self._t0)
        return False


class _Null:
    """What a disarmed probe returns: a `with` that does nothing.

    A single shared instance, so a disarmed probe allocates nothing at all.
    """

    __slots__ = ()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


_NULL = _Null()


class Profiler:
    """Per-tick timing for the simulation, off by default.

    Armed either for a fixed number of ticks (`arm(200)`) or until stopped
    (`arm()`), which are the two things a player actually wants: "measure the
    next N ticks and tell me", and "measure while I do this".

    The fixed-count form is the default because it is the one that cannot be
    forgotten about -- an armed profiler left running costs a little and, more
    to the point, dilutes its own numbers with whatever the board does next.
    """

    def __init__(self):
        self.armed = False
        #: Ticks still to measure, or None for "until stopped".
        self.remaining = None
        #: path -> _Stat, insertion-ordered.
        self.stats = {}
        #: Reused span objects, one per path.
        self._spans = {}
        #: Ticks measured since the last arm().
        self.ticks = 0
        #: Set by the dump, so the panel can show the last result without
        #: re-running. None until something has been measured.
        self.last_report = None

    # --- control ---------------------------------------------------------
    def arm(self, ticks=200):
        """Start measuring. `ticks=None` measures until `stop()`.

        Clears previous stats rather than accumulating into them: two
        measurements of different board states averaged together describe
        neither, and the common case is arming again after a change to see
        whether it helped.
        """
        self.reset()
        self.remaining = None if ticks is None else max(1, int(ticks))
        self.armed = True

    def stop(self):
        """Stop measuring, keeping the stats so a dump can follow."""
        self.armed = False
        self.remaining = None

    def reset(self):
        """Stop measuring and discard what was collected.

        Distinct from `stop()`, and the difference matters: stopping keeps the
        stats because the player pressing "stop and dump" wants to see them,
        while a *fresh start* must leave no trace -- a disarmed profiler
        holding a previous measurement's paths looks exactly like one that
        recorded while disarmed.
        """
        self.stop()
        self.stats = {}
        self._spans = {}
        self.ticks = 0

    # --- measuring -------------------------------------------------------
    def probe(self, path):
        """A context manager timing `path`, or a no-op when disarmed.

        `path` is slash-separated (`"belt/transport"`), which is what the dump
        indents by. Call sites pass a literal, so the string is interned and
        the dict lookup is a pointer compare.
        """
        if not self.armed:
            return _NULL
        span = self._spans.get(path)
        if span is None:
            stat = _Stat()
            self.stats[path] = stat
            span = _Span(stat)
            self._spans[path] = span
        return span

    def count_tick(self):
        """Called once per tick by the clock, after the phases have run.

        Returns True if this tick was the last one armed, which is the signal
        to dump -- decided here rather than by the caller so the countdown and
        the disarm cannot get out of step.
        """
        if not self.armed:
            return False
        self.ticks += 1
        if self.remaining is not None:
            self.remaining -= 1
            if self.remaining <= 0:
                self.armed = False
                self.remaining = None
                return True
        return False

    # --- reporting -------------------------------------------------------
    def report(self, context="", rate_hz=10.0):
        """The breakdown as a list of text lines. Does not print.

        Returned rather than printed so the caller decides where it goes --
        the console today, and a panel window or a file without changing this.

        `rate_hz` is the configured tick rate, which the caller knows and this
        does not: a per-tick cost is only meaningful against how often a tick
        happens, and the clock owns that number.
        """
        if not self.ticks or not self.stats:
            return ["tick profile: nothing measured "
                    "(arm it and let the simulation run)"]

        n = self.ticks
        lines = []
        head = f"tick profile: {n} tick{'s' if n != 1 else ''}"
        if context:
            head += f"  [{context}]"
        lines.append("")
        lines.append("=" * 72)
        lines.append(head)
        lines.append("=" * 72)

        # The total is the top-level phases only. Summing every path would
        # double-count, since a child's time is inside its parent's -- that is
        # the whole reason the paths are a tree. Out-of-tick roots are left
        # out as well, and reported under their own heading below.
        roots = [p for p in self.stats
                 if "/" not in p and p not in OUT_OF_TICK]
        total = sum(self.stats[p].total for p in roots)
        per_tick = total / n

        lines.append(f"{'':34}{'share':>7} {'mean/tick':>11} "
                     f"{'worst':>9} {'calls':>8}")
        lines.append("-" * 72)

        for path in self._ordered():
            if path.split("/", 1)[0] in OUT_OF_TICK:
                continue
            stat = self.stats[path]
            depth = path.count("/")
            name = path.rsplit("/", 1)[-1]
            label = f"{'  ' * depth}{name}"
            # A share is of the *whole tick*, not of the parent: it is the
            # frame-budget question, and a parent-relative percentage would
            # need the reader to multiply down the tree to get it.
            share = 100.0 * stat.total / total if total else 0.0
            # Per *tick*, not per call -- a probe inside a per-segment loop
            # runs hundreds of times a tick, and its per-call mean is
            # microseconds of noise while its per-tick cost is the real
            # number. `calls` is reported beside it so the two are never
            # confused.
            lines.append(
                f"{label:<34}{share:6.1f}% {1e3 * stat.total / n:9.3f}ms "
                f"{1e3 * stat.worst:7.3f}ms {stat.count:8d}")

        lines.append("-" * 72)
        lines.append(f"{'tick total':<34}{100.0:6.1f}% "
                     f"{1e3 * per_tick:9.3f}ms")
        if per_tick > 0:
            lines.append(f"  = {1.0 / per_tick:,.0f} ticks/sec "
                         f"if the tick were all that ran")
            # A cost only means something against the rate the sim is set to:
            # 5ms a tick is nothing at 10/s and the whole budget at 200/s.
            lines.append(f"  at {rate_hz:g} ticks/sec that is "
                         f"{100.0 * per_tick * rate_hz:.1f}% of wall clock")

        # Edit-time costs, per *occurrence* rather than per tick: a rebuild
        # happens when the player places something, and "88ms each time you
        # place a belt" is the sentence that means something. Dividing it by
        # the tick count would say 0.4ms and hide a visible stutter.
        extra = [p for p in self.stats
                 if p.split("/", 1)[0] in OUT_OF_TICK]
        if extra:
            lines.append("")
            lines.append("not part of a tick (cost per occurrence):")
            for path in sorted(extra):
                stat = self.stats[path]
                lines.append(f"  {path:<32}{1e3 * stat.mean:9.3f}ms "
                             f"{1e3 * stat.worst:7.3f}ms {stat.count:8d}")
        lines.append("=" * 72)
        lines.append("")

        self.last_report = (n, per_tick)
        return lines

    def _ordered(self):
        """Every path, each immediately after its own parent.

        A **pre-order walk**, not a sort. Within one parent's children the
        order is by total descending, because the dump's job is to put the
        expensive thing where the eye lands; roots follow PHASE_ORDER so
        successive dumps are comparable line for line.

        Sorting the whole group flat by `(depth, -total)` is the obvious
        shortcut and is wrong: it puts every depth-1 path before every
        depth-2 one, so a grandchild lands under whichever *sibling* happened
        to sort last. `belt/transport/schedule` rendered indented beneath
        `belt/direct`, claiming the scheduler was part of the direct
        deliveries -- a plausible-looking lie, which is the worst kind in a
        tool whose whole output is an attribution.
        """
        def rank_root(root):
            if root in PHASE_ORDER:
                return (0, PHASE_ORDER.index(root), root)
            # Out-of-tick roots last, after any unrecognised in-tick one --
            # they are an appendix to the dump, not part of its main table.
            return (2 if root in OUT_OF_TICK else 1, 0, root)

        def children(prefix, depth):
            """Immediate children of `prefix` -- one level down, not all."""
            return [p for p in self.stats
                    if p.startswith(prefix) and p.count("/") == depth]

        out = []

        def walk(path):
            out.append(path)
            kids = children(path + "/", path.count("/") + 1)
            for kid in sorted(kids, key=lambda p: -self.stats[p].total):
                walk(kid)

        for root in sorted({p.split("/", 1)[0] for p in self.stats},
                           key=rank_root):
            # A root may exist only as an ancestor (nothing timed it
            # directly), in which case it has no row of its own to emit.
            if root in self.stats:
                walk(root)
            else:
                for kid in sorted(children(root + "/", 1),
                                  key=lambda p: -self.stats[p].total):
                    walk(kid)
        return out

    def dump(self, context="", rate_hz=10.0, out=print):
        """Print the breakdown. Returns the lines, for a test to assert on."""
        lines = self.report(context, rate_hz)
        for line in lines:
            out(line)
        return lines

    def __repr__(self):
        state = "armed" if self.armed else "off"
        if self.armed and self.remaining is not None:
            state = f"armed({self.remaining} left)"
        return f"<Profiler {state} ticks={self.ticks} paths={len(self.stats)}>"


def _self_check():
    """The profiler's contract. Cheap; runs at import."""
    p = Profiler()

    # Disarmed, a probe is a no-op that records nothing and allocates the
    # one shared null object.
    assert p.probe("belt") is _NULL
    with p.probe("belt"):
        pass
    assert not p.stats
    assert p.count_tick() is False, "a disarmed profiler counts no ticks"

    # Armed, a probe records. Two spans on one path aggregate rather than
    # replacing each other.
    p.arm(ticks=2)
    with p.probe("belt"):
        pass
    with p.probe("belt"):
        pass
    assert p.stats["belt"].count == 2
    assert p.probe("belt") is p.probe("belt"), "spans are reused, not rebuilt"

    # The countdown disarms itself and says so exactly once.
    assert p.count_tick() is False, "one of two ticks is not the last"
    assert p.ticks == 1
    assert p.count_tick() is True, "the last armed tick reports itself"
    assert not p.armed
    assert p.count_tick() is False, "and does not report twice"
    assert p.ticks == 2, "a disarmed tick is not counted"

    # A report names the paths and does not crash on a child path.
    p.arm(ticks=None)
    with p.probe("belt"):
        with p.probe("belt/transport"):
            pass
    p.count_tick()
    lines = p.report("test")
    text = "\n".join(lines)
    assert "belt" in text and "transport" in text, text
    # The share is of the whole tick, so a root is 100% when it is the only
    # root -- the child's time is *inside* it and must not push the total
    # over 100.
    assert "100.0%" in text, text

    # An out-of-tick root is reported but kept out of the tick total, which
    # is the whole reason it is distinguished: a rebuild dwarfs a tick, and
    # letting it into the share column would push every real phase to ~0%.
    p.arm(ticks=None)
    with p.probe("belt"):
        pass
    with p.probe("rebuild"):
        # Spin rather than sleep: a measurable span with no scheduler
        # involvement, so the assertion below cannot be flaky.
        t0 = time.perf_counter()
        while time.perf_counter() - t0 < 0.002:
            pass
    p.count_tick()
    text = "\n".join(p.report())
    assert "not part of a tick" in text, text
    # `belt` is the only in-tick root, so it must still read as all of it.
    belt_line = [ln for ln in p.report() if ln.strip().startswith("belt")][0]
    assert "100.0%" in belt_line, belt_line
    # And the tick total must be the phase, not the phase plus the rebuild,
    # even though the rebuild is far larger.
    assert p.stats["rebuild"].total > p.stats["belt"].total
    total_line = [ln for ln in p.report() if "tick total" in ln][0]
    assert f"{1e3 * p.stats['belt'].total:9.3f}ms" in total_line, total_line

    # A grandchild follows its own parent, not whichever sibling sorted last.
    # Built so a flat (depth, -total) sort gets it wrong: `direct` outweighs
    # `transport`, so that sort would list direct second and strand
    # `transport/schedule` underneath it.
    p.arm(ticks=None)
    for path, spin in (("belt", 0.004), ("belt/direct", 0.003),
                       ("belt/transport", 0.001),
                       ("belt/transport/schedule", 0.0005)):
        with p.probe(path):
            t0 = time.perf_counter()
            while time.perf_counter() - t0 < spin:
                pass
    order = p._ordered()
    assert order == ["belt", "belt/direct", "belt/transport",
                     "belt/transport/schedule"], order

    # arm() clears rather than accumulating.
    p.arm(ticks=5)
    assert not p.stats and p.ticks == 0

    # stop() and reset() differ, and both halves of that matter: stopping
    # keeps the stats so a dump can follow, resetting leaves no trace so a
    # disarmed profiler cannot look like one that recorded while off.
    with p.probe("belt"):
        pass
    p.count_tick()
    p.stop()
    assert p.stats and p.ticks == 1, "stop() must keep what it collected"
    p.reset()
    assert not p.stats and p.ticks == 0 and not p.armed

    # Nothing measured is reported as such rather than dividing by zero.
    assert "nothing measured" in Profiler().report()[0]


_self_check()
