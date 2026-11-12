"""Differential check: sleeping must not change the simulation at all.

`BeltSim` now skips segments it believes are at rest. That is only sound if a
skipped segment really was a fixed point *and* something wakes it the moment
it stops being one. Both halves fail silently: a wrongly-slept run simply
stops moving, which on a board the size of a CPU looks like a logic bug in the
circuit rather than a scheduler bug in the engine.

So this runs the same board twice, tick for tick, with the sleep machinery on
and off, and compares the full item state of every segment after every tick.
Any divergence is reported with the tick and segment it first appeared on.

Two timelines, one board
------------------------

Running two boards side by side would need two engines and two GL contexts.
Instead two *timelines* are interleaved on one board: each tick is run twice
from the same starting snapshot -- once with the sleep set the sleeping
timeline has accumulated, once with sleeping disabled entirely -- and the two
results are compared. Each timeline then continues from its own result.

**Both halves of that are load-bearing, and getting either wrong makes the
check vacuous.** An earlier version called `wake_all()` before the oracle tick
*and* let both timelines continue from the sleeping result. That made the
oracle tick an awake-everything tick over a state the sleeping run had already
produced, so the two agreed by construction: three separate mutations --
removing the wake after a gate fires, removing it after a generator fires, and
sleeping every run holding items -- all passed. The fix is that the oracle
keeps its own state and never sees the sleep set, so a divergence accumulates
instead of being reset every tick.

Sleeping bugs are cumulative, which is why that matters: a missed wake does
not corrupt the tick it happens on, it just fails to move one run, and the
error compounds over the following hundred ticks.

    python tools/check_wakelist_safe.py [--seed N] [--ticks N]
"""

import argparse
import random
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests import harness  # noqa: E402
from core import cell  # noqa: E402
from core.entity import (  # noqa: E402
    Belt, Floor, Max, Merge, Reciprocal, Route, Shear, Sink, Source, Swap)
from core.gameboard import GRID_CHANNELS  # noqa: E402

BOARD = 256
board = harness.board
engine = harness.engine

MACHINES = (Shear, Route, Merge, Reciprocal, Max, Floor, Swap, Sink)


def _enlarge():
    if board.width >= BOARD:
        return
    board.width = board.height = BOARD
    board.grid = np.zeros((BOARD, BOARD, GRID_CHANNELS), dtype=np.int32)
    board.mark_all_dirty()


_enlarge()


# --- state capture --------------------------------------------------------
def snapshot():
    """Everything a tick can change: ring state, items, and gate channels."""
    segs = {}
    for sid, s in engine.segments.segments.items():
        segs[sid] = (s.head, s.tail, s.anchor,
                     tuple(s.enter_tick) if s.enter_tick is not None else (),
                     tuple(s.payload) if s.payload is not None else ())
    return segs, board.entities.copy(), set(engine.belts._asleep)


def restore(snap):
    segs, ents, asleep = snap
    for sid, (h, t, a, et, pl) in segs.items():
        s = engine.segments.segments.get(sid)
        if s is None:
            continue
        s.head, s.tail, s.anchor = h, t, a
        if s.enter_tick is not None:
            s.enter_tick[:] = et
        if s.payload is not None:
            s.payload[:] = pl
    board.entities[:] = ents
    engine.belts._asleep = set(asleep)


def compare_segs(a, b):
    """Differences between two segment snapshots, as readable strings."""
    out = []
    for sid in sorted(set(a) | set(b)):
        if sid not in a or sid not in b:
            out.append(f"segment {sid} exists in only one run")
            continue
        if a[sid][:3] != b[sid][:3]:
            out.append(
                f"segment {sid}: sleeping gave "
                f"head/tail/anchor {a[sid][:3]}, full gave {b[sid][:3]}")
        elif a[sid] != b[sid]:
            out.append(f"segment {sid}: item contents differ")
    return out


def build(rng, runs=12, run_len=34):
    """A board that actually sleeps **and wakes**.

    The sleep/wake transition is the only thing the wake edges affect, and a
    board that merely goes quiet does not exercise them: a first attempt here
    used Source-fed runs throughout, which either stay busy forever or sleep
    once and never wake. It reported 45 of 46 segments asleep and caught none
    of four mutations, because over 60 ticks it made **zero** wake
    transitions.

    So most rows here are deliberately **starved gates**: a Shear fed nothing
    sits idle with its output run asleep, and `main()` hand-feeds inputs at
    random to force the gate to fire. That is exactly the edge a missed
    `_wake_around()` drops -- the gate fires into a sleeping run that never
    notices.

    A couple of Source-fed rows stay for contrast, so the continuously-busy
    case is covered too.
    """
    engine.clear_board()
    y = 1
    # Two live rows, so "never sleeps" is represented.
    for _ in range(2):
        if y + 3 >= board.height:
            break
        if board.is_empty(0, y):
            board.spawn(Source, 0, y, cell.DIR_PX)
        for x in range(2, run_len):
            if board.is_empty(x, y):
                board.spawn(Belt, x, y, cell.DIR_PX)
        y += 4

    # The rest: a starved machine feeding a run that will sleep, with a Sink
    # at the far end so the run has a real consumer to deliver to.
    for _ in range(runs):
        if y + 3 >= board.height:
            break
        m = rng.choice(MACHINES)
        cells = m.cells_for(2, y, cell.DIR_PX, False)
        if all(board.is_empty(cx, cy) for (cx, cy) in cells):
            board.spawn(m, 2, y, cell.DIR_PX)
        x = 2 + m.FOOTPRINT[0]
        while x < run_len:
            if board.is_empty(x, y):
                board.spawn(Belt, x, y, cell.DIR_PX)
            x += 1
        cells = Sink.cells_for(run_len, y, cell.DIR_PX, False)
        if all(board.is_empty(cx, cy) for (cx, cy) in cells):
            board.spawn(Sink, run_len, y, cell.DIR_PX)
        y += 4
    engine.invalidate_segments()
    engine.sync_segments()


def starved_gates():
    """Live gates with at least one input, for the random feeder below."""
    return [g for g in engine.gates.gates() if g.input_count() > 0]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--ticks", type=int, default=250)
    args = ap.parse_args()
    rng = random.Random(args.seed)

    build(rng)
    problems = []
    slept_total = 0
    seg_total = 0

    # The two timelines, each carrying its own full board state. They start
    # identical; `sleep_state` keeps whatever sleep set accumulates, and
    # `full_state` is never allowed to have one.
    sleep_state = snapshot()
    full_state = snapshot()
    wake_edges = 0

    def run_one_tick(at_tick):
        """One tick at a fixed tick number, from whatever is loaded."""
        engine.clock.tick = at_tick
        engine.clock.request_step(1)
        engine.clock.advance(0.0)

    feedable = [g.row for g in starved_gates()]

    def feed(plan):
        """Apply the tick's feeding decisions. Identical in both timelines.

        Hand-feeding a starved gate is what forces the sleep->wake edge: the
        gate fires into a run that has gone to sleep, and only a correct
        `_wake_around()` lets that run notice. Decided once per tick and
        replayed, so the two timelines see the same stimulus.
        """
        for row in plan:
            if not board.is_row_alive(row):
                continue
            g = board.view(row)
            for c in range(g.input_count()):
                g.set_input(c, 2.0)

    for t in range(args.ticks):
        slept_total += len(sleep_state[2])
        seg_total += len(sleep_state[0])

        # The stimulus for this tick, chosen once and replayed into both.
        plan = [r for r in feedable if rng.random() < 0.15]

        # (1) the timeline under test: its own state, its own sleep set.
        restore(sleep_state)
        feed(plan)
        was_asleep = set(engine.belts._asleep)
        run_one_tick(t)
        wake_edges += len(was_asleep - set(engine.belts._asleep))
        sleep_state = snapshot()

        # (2) the oracle: its own state, and sleeping disabled outright. It
        #     must never inherit the sleeping timeline's state or its skips.
        restore(full_state)
        feed(plan)
        engine.belts.wake_all()
        run_one_tick(t)
        full_state = snapshot()
        # Keep the oracle free of any sleep set it just accumulated, so the
        # next oracle tick is a genuine full tick.
        full_state = (full_state[0], full_state[1], set())

        diffs = compare_segs(sleep_state[0], full_state[0])
        # Gate channels must match too -- a slept run that failed to deliver
        # shows up here before it shows up in ring state.
        if not np.array_equal(sleep_state[1], full_state[1]):
            bad = np.argwhere(
                ~np.isclose(sleep_state[1], full_state[1], equal_nan=True))
            rows = sorted({int(r) for r, _c in bad})[:5]
            diffs.append(f"entity rows differ: {rows}")
        if diffs:
            problems.append((t, diffs))
            break

    print(f"seed {args.seed}, {args.ticks} ticks, "
          f"{engine.segments.count} segments")
    if seg_total:
        print(f"  mean asleep at tick start: "
              f"{slept_total / max(args.ticks, 1):.1f} of "
              f"{seg_total / max(args.ticks, 1):.1f}")
    # Reported because a run with no wake transitions proves nothing about
    # the wake edges, however many ticks it ran -- that is exactly how the
    # first version of this check passed against four mutations.
    print(f"  sleep -> wake transitions: {wake_edges}")
    if not wake_edges:
        print("  WARNING: no segment ever woke, so this run did not "
              "exercise the wake edges at all")
    if problems:
        t, diffs = problems[0]
        print(f"\nDIVERGENCE at tick {t}:")
        for d in diffs[:8]:
            print("  " + d)
        return 1
    print("  sleeping and full ticking agree at every tick")
    return 0


if __name__ == "__main__":
    sys.exit(main())
