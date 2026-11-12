"""Differential check: the prefilter must never skip a gate that would fire.

`GateSim._candidate_rows()` is an optimisation only if it is *conservative* --
it may admit a gate that turns out to do nothing, but it must never exclude
one the full per-gate path would have fired. Excluding one is silent: the gate
simply never runs, which on a real board looks like a machine that mysteriously
stalls rather than like a crash.

The suite covers the behaviours, but it cannot see the prefilter's decision
directly. This does: it runs a board tick by tick and, at every tick, computes
the admitted set *and* evaluates the full readiness predicate for every gate
in Python, then asserts no gate was excluded that the full path would have let
through to a fire.

It also asserts the reverse direction is merely wasteful rather than wrong
(an admitted gate that does nothing is fine) and reports how tight the filter
is, since a filter that admits everything is safe and useless.

    python tools/check_prefilter_safe.py [--seed N] [--ticks N]
"""

import argparse
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402

from tests import harness  # noqa: E402
from core import cell  # noqa: E402
from core.entity import (  # noqa: E402
    Belt, Floor, Max, Merge, Reciprocal, Route, Shear, Sink, Source, Swap)
from core.gameboard import GRID_CHANNELS  # noqa: E402

BOARD = 256
board = harness.board
engine = harness.engine
gates = engine.gates

MACHINES = (Shear, Route, Merge, Reciprocal, Max, Floor, Swap, Sink)


def _enlarge():
    if board.width >= BOARD:
        return
    board.width = board.height = BOARD
    board.grid = np.zeros((BOARD, BOARD, GRID_CHANNELS), dtype=np.int32)
    board.mark_all_dirty()


_enlarge()


def would_fire(gate):
    """The full per-gate predicate: would the unfiltered phase fire this?

    Mirrors structure_phase()'s own sequence of tests, which is what makes it
    a valid oracle: readiness, then (for a consumer) every connected output
    zone clear, or (for a generator) at least one connected zone free.
    """
    if not gate.inputs_ready():
        return False
    drains = gates._compute_connected_channels(gate)
    if gate.input_count() == 0:
        if not drains:
            return False
        return any(not gate.out_held(c) for c in drains)
    return gate.outputs_clear(drains)


def build(rng, runs=14, run_len=40):
    engine.clear_board()
    y = 1
    for _ in range(runs):
        if y + 3 >= board.height:
            break
        if board.is_empty(0, y):
            board.spawn(Source, 0, y, cell.DIR_PX)
        x = 2
        while x < run_len:
            if rng.random() < 0.12:
                m = rng.choice(MACHINES)
                cells = m.cells_for(x, y, cell.DIR_PX, False)
                if all(board.is_empty(cx, cy) for (cx, cy) in cells):
                    board.spawn(m, x, y, cell.DIR_PX)
                    x += m.FOOTPRINT[0] + 1
                    continue
            if board.is_empty(x, y):
                board.spawn(Belt, x, y, cell.DIR_PX)
            x += 1
        y += 4
    engine.invalidate_segments()
    engine.sync_segments()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--ticks", type=int, default=400)
    args = ap.parse_args()
    rng = random.Random(args.seed)

    build(rng)
    problems = []
    admitted = excluded = would_have_fired = 0

    for t in range(args.ticks):
        admit, _s, _b = gates._candidate_rows()
        for gate in gates.gates():
            fires = would_fire(gate)
            if gate.row in admit:
                admitted += 1
            else:
                excluded += 1
                if fires:
                    problems.append(
                        f"tick {t}: row {gate.row} "
                        f"({type(gate).__name__}) was EXCLUDED but the full "
                        f"path would have fired it")
            if fires:
                would_have_fired += 1
        if problems:
            break
        engine.clock.request_step(1)
        engine.clock.advance(0.0)
        # Occasional edits, so row/segment recycling is in the mix too.
        if rng.random() < 0.04:
            engine.erase(rng.randrange(1, 40), rng.choice([1, 5, 9, 13]))
            engine.sync_segments()

    total = admitted + excluded
    print(f"seed {args.seed}, {args.ticks} ticks, "
          f"{sum(1 for _ in gates.gates())} gates")
    if total:
        print(f"  gate-ticks examined : {total}")
        print(f"  admitted            : {admitted} "
              f"({100 * admitted / total:.1f}%)")
        print(f"  skipped             : {excluded} "
              f"({100 * excluded / total:.1f}%)")
        print(f"  would-fire          : {would_have_fired}")
    if problems:
        print(f"\n{len(problems)} UNSAFE EXCLUSION(S):")
        for p in problems[:10]:
            print("  " + p)
        return 1
    print("  no gate that would fire was ever skipped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
