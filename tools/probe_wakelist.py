"""Would a wake list pay on a CPU-shaped board, and is sleeping provable?

The prefilter work assumed a factory: mixed activity, with the saturated case
as the pessimistic bound. A *computer* built out of these parts is the opposite
workload -- one trace active inside a CPU while thousands of RAM gates and the
belt segments wiring them sit idle indefinitely. At 99% idle the question is no
longer "skip the gates that cannot fire this tick" but "stop touching the
sleeping 99% at all".

Two things this establishes before any wake list is built.

**1. Sleeping is provable, not heuristic.**

`BeltSim.tick_segment()` on an idle segment changes nothing, and that is
arithmetic rather than luck:

  - the anchor advance is `min(anchor + v, L)`, which **saturates** at `L` and
    then returns `L` forever;
  - the pop needs `head != tail`, and an empty run has neither;
  - the push needs `offer()` to return a value, and an idle upstream gate
    returns None.

So an empty segment with `anchor == L` is a fixed point. A *full, blocked* run
is the other fixed point, and it is the one that matters for RAM: the anchor
has already stopped at `L - d`, the head is not at the end or the consumer
refuses, and `has_room()` is False so nothing enters. Its item positions are
frozen too -- the closed form keeps describing it with no special case, which
is what makes sleeping safe for the *renderer* as well as the simulation.

`verify_fixed_points()` asserts both by brute force rather than trusting the
argument.

**2. What fraction is actually asleep, and what that is worth.**

`measure()` builds a board shaped like memory -- many short runs between
gates, fed by one active trace -- and reports the sleeping fraction alongside
what the tick currently costs, so the projected win is measured rather than
asserted.

    python tools/probe_wakelist.py [--scale 2]
"""

import argparse
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests import harness  # noqa: E402
from core import cell  # noqa: E402
from core.belts import UNIT, BeltSim  # noqa: E402
from core.entity import (  # noqa: E402
    MAX_CHANNELS, Belt, Entity, Shear, Sink, Source)
from core.gameboard import GRID_CHANNELS  # noqa: E402

BOARD = 512
board = harness.board
engine = harness.engine


def _enlarge():
    if board.width >= BOARD:
        return
    board.width = board.height = BOARD
    board.grid = np.zeros((BOARD, BOARD, GRID_CHANNELS), dtype=np.int32)
    board.mark_all_dirty()


_enlarge()


def ms(fn, n=20):
    best = None
    for _ in range(3):
        t0 = time.perf_counter()
        for _ in range(n):
            fn()
        dt = (time.perf_counter() - t0) / n * 1e3
        best = dt if best is None else min(best, dt)
    return best


# --- 1. is sleeping provable? ---------------------------------------------
class _Seg:
    """Minimal stand-in, as core/belts.py's own self-check uses."""

    def __init__(self, length, cap=8):
        self.sid = 1
        self.length = length
        self.head = self.tail = 0
        self.anchor = length * UNIT
        self.cap = cap
        self.enter_tick = [0] * cap
        self.payload = [0.0] * cap
        self.cells = [(0, i) for i in range(length)]
        self.src = self.dst = None

    @property
    def count(self):
        return self.tail - self.head

    def state(self):
        return (self.head, self.tail, self.anchor,
                tuple(self.enter_tick), tuple(self.payload))


class _Index:
    def __init__(self, seg):
        self.segments = {seg.sid: seg}


def verify_fixed_points():
    """Both sleep states must be exact fixed points of tick_segment()."""
    print("\n=== fixed points: is an idle segment provably a no-op? ===")
    problems = []

    # (a) empty, anchor saturated. The state a fresh or drained run sits in.
    for L in (1, 2, 3, 5, 8, 13):
        for spacing in (UNIT, 2 * UNIT, 3 * UNIT):
            seg = _Seg(L)
            sim = BeltSim(_Index(seg), UNIT, spacing)
            before = seg.state()
            for t in range(50):
                sim.tick_segment(seg, t)          # no producer, no consumer
            if seg.state() != before:
                problems.append(
                    f"empty L={L} d={spacing}: {before} -> {seg.state()}")
    print(f"  empty + saturated anchor : "
          f"{'FIXED' if not problems else 'NOT FIXED'}")

    # (b) full and blocked. The RAM case: a run holding a value nobody takes.
    full_problems = []
    for L in (1, 2, 3, 5, 8):
        for spacing in (UNIT, 2 * UNIT):
            seg = _Seg(L, cap=32)
            sim = BeltSim(_Index(seg), UNIT, spacing)
            # Fill to capacity against a refusing consumer.
            for t in range(60):
                sim.tick_segment(seg, t, offer=lambda _s: 1.0, accept=None)
            before = seg.state()
            pos_before = sim.positions(seg, 60)
            # Now nothing upstream and nothing downstream: it must freeze.
            for t in range(60, 110):
                sim.tick_segment(seg, t)
            if seg.state() != before:
                full_problems.append(
                    f"full L={L} d={spacing}: {before} -> {seg.state()}")
            # Item positions must be frozen too, or the renderer would drift.
            if sim.positions(seg, 110) != pos_before:
                full_problems.append(
                    f"full L={L} d={spacing}: positions moved while asleep")
    print(f"  full + blocked           : "
          f"{'FIXED' if not full_problems else 'NOT FIXED'}")

    # (c) the negative control: a run that is *not* idle must NOT be fixed,
    # or the sleep test would be vacuous.
    seg = _Seg(5)
    sim = BeltSim(_Index(seg), UNIT, UNIT)
    before = seg.state()
    sim.tick_segment(seg, 0, offer=lambda _s: 1.0,
                     accept=lambda _s, _p: True)
    moved = seg.state() != before
    print(f"  active run changes state : {'yes' if moved else 'NO -- BUG'}")

    # (d) `is_at_rest()` must agree with "a tick changes nothing", on every
    # state reachable by draining a run -- including the one that motivates
    # the anchor clause.
    #
    # An **empty run whose anchor has not yet saturated** is the case worth
    # naming: it looks idle (head == tail, nothing can pop) but the anchor
    # advance still moves, so sleeping it freezes the anchor below `L`. With
    # d == v that state is unreachable, and with d > v the anchor happens to
    # re-saturate before any item's position depends on it -- which is why
    # dropping the clause is invisible to the end-to-end differential in
    # tools/check_wakelist_safe.py. It is checked *here*, against the
    # definition rather than against observable behaviour, because "correct
    # by luck under today's spacing" is not a property to rest a scheduler on.
    disagreements = []
    for L in (1, 2, 3, 5):
        for spacing in (UNIT, 2 * UNIT, 3 * UNIT):
            seg = _Seg(L, cap=32)
            sim = BeltSim(_Index(seg), UNIT, spacing)
            sim.tick_segment(seg, 0, offer=lambda _s: 1.0)
            for t in range(1, 40):
                sim.tick_segment(seg, t, accept=lambda _s, _p: True)
                claimed = sim.is_at_rest(seg, t)
                before_s = seg.state()
                sim.tick_segment(seg, t + 1)      # idle tick
                really = (seg.state() == before_s)
                if claimed and not really:
                    # Report head/tail/anchor only: the ring arrays are 32
                    # slots wide and drown the one field that differs.
                    disagreements.append(
                        f"L={L} d={spacing // UNIT}: is_at_rest() said yes "
                        f"at tick {t}, but an idle tick moved "
                        f"head/tail/anchor {before_s[:3]} -> "
                        f"{seg.state()[:3]}")
                    break
    print(f"  is_at_rest matches reality: "
          f"{'yes' if not disagreements else 'NO'}")
    for d in disagreements[:4]:
        print("    " + d)

    for p in problems + full_problems:
        print("    " + p)
    return not (problems or full_problems or disagreements) and moved


# --- 2. how much of a CPU-shaped board is asleep? -------------------------
def build_memory_bank(banks, cells_per_word=6):
    """A board shaped like RAM: many short runs, each between two gates.

    Deliberately *not* the factory shape the earlier probes used. Each "word"
    is a short belt run with a Shear at each end, so the board is dominated by
    segments and gates that have nothing to do -- which is the real workload.
    One Source drives a single active trace.
    """
    engine.clear_board()
    placed = 0
    y = 1
    # One live trace along the bottom, so the board is not wholly inert.
    if board.is_empty(0, y):
        board.spawn(Source, 0, y, cell.DIR_PX)
    for x in range(2, 60):
        if board.is_empty(x, y):
            board.spawn(Belt, x, y, cell.DIR_PX)
    y += 3

    while placed < banks and y + 3 < board.height:
        x = 2
        while x + cells_per_word + 4 < board.width and placed < banks:
            cells = Shear.cells_for(x, y, cell.DIR_PX, False)
            if all(board.is_empty(cx, cy) for (cx, cy) in cells):
                board.spawn(Shear, x, y, cell.DIR_PX)
            x += 2
            for _ in range(cells_per_word):
                if board.is_empty(x, y):
                    board.spawn(Belt, x, y, cell.DIR_PX)
                x += 1
            placed += 1
        y += 3
    engine.invalidate_segments()
    engine.sync_segments()
    for _ in range(60):
        engine.clock.request_step(1)
        engine.clock.advance(0.0)
    return engine.segments.count, sum(1 for _ in engine.gates.gates())


def sleeping_segments():
    """Segments currently in one of the two fixed points."""
    sim = engine.belts
    asleep = 0
    for seg in engine.segments.segments.values():
        length = sim.length_of(seg)
        empty_fixed = (seg.head == seg.tail and seg.anchor >= length)
        # Full and unable to accept: nothing can enter, and only the consumer
        # could change it -- which is the downstream gate's business, not the
        # segment's.
        full_fixed = (seg.head != seg.tail
                      and not sim.has_room(seg, engine.clock.tick))
        if empty_fixed or full_fixed:
            asleep += 1
    return asleep


def measure(scale):
    print("\n=== a CPU-shaped board: how much is asleep? ===")
    segs, ngates = build_memory_bank(220 * scale)
    asleep = sleeping_segments()

    rows, n_in, n_out, strict = engine.gates._gate_table()
    admit, starved, blocked = engine.gates._candidate_rows()

    st = ms(engine._structure_phase)
    bl = ms(engine._belt_phase)
    print(f"  {segs} segments, {ngates} gates")
    print(f"  segments asleep      : {asleep}/{segs} "
          f"({100 * asleep / max(segs, 1):.1f}%)")
    print(f"  gates admitted       : {len(admit)}/{ngates} "
          f"({100 * len(admit) / max(ngates, 1):.1f}%)  "
          f"[starved {starved}, blocked {blocked}]")
    print(f"  structure phase      : {st:>7.3f} ms")
    print(f"  belt phase           : {bl:>7.3f} ms")
    print(f"  total                : {st + bl:>7.3f} ms "
          f"({1e3 / (st + bl):.0f} ticks/s)")
    live = segs - asleep
    if live < segs:
        print(f"  if only the {live} awake segments ticked, the belt phase")
        print(f"  would be ~{bl * live / max(segs, 1):.3f} ms "
              f"({segs / max(live, 1):.0f}x less work)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scale", type=int, default=1)
    args = ap.parse_args()
    ok = verify_fixed_points()
    measure(args.scale)
    print()
    print("sleeping is provable" if ok else "SLEEP PRECONDITION FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
