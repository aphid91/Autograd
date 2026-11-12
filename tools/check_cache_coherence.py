"""Differential check: the cached gate layer against an uncached one.

The tick now memoises two things per gate row -- output connectivity and
world-space port geometry (see `GateSim._connected_channels` and
`_output_faces`). Both are keyed by **row**, and rows are recycled by the free
list, so the failure mode is a new entity inheriting the previous occupant's
ports: silent, and only reachable under churn.

This runs a factory while editing it, and after every edit compares every
gate's cached answers against a freshly computed one. A disagreement is
reported with the row and the edit that caused it.

    python tools/check_cache_coherence.py [--seed N] [--edits N]

It is a tool rather than a test because it is a randomised sweep over edits
rather than a statement about one behaviour -- the same relationship
`tests/test_belts.py`'s oracle comparison has to the closed form, except that
this one is cheap to re-run with a new seed when the caching changes.
"""

import argparse
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests import harness  # noqa: E402
from core import cell  # noqa: E402
from core.entity import Belt, Shear, Source  # noqa: E402

board = harness.board
engine = harness.engine
gates = engine.gates


def fresh_drains(gate):
    """Connectivity computed from scratch, bypassing the cache."""
    return gates._compute_connected_channels(gate)


def fresh_faces(gate):
    """Port geometry computed from scratch, bypassing the cache."""
    ox, oy = gate.origin
    _ins, outs = gate.faces_for(ox, oy, gate.direction, gate.flipped)
    return tuple((xy, face, gate.out_channel(i))
                 for i, (xy, face) in enumerate(outs))


def compare(label):
    """Every gate's cached answers against freshly computed ones."""
    problems = []
    for gate in gates.gates():
        want_f = fresh_faces(gate)
        got_f = gates._output_faces(gate)
        if tuple(got_f) != want_f:
            problems.append(
                f"{label}: row {gate.row} ({type(gate).__name__}) ports\n"
                f"    cached {got_f}\n    fresh  {want_f}")
        want_d = fresh_drains(gate)
        got_d = gates._connected_channels(gate)
        if set(got_d) != set(want_d):
            problems.append(
                f"{label}: row {gate.row} ({type(gate).__name__}) drains "
                f"cached {sorted(got_d)} fresh {sorted(want_d)}")
    return problems


def seed_board():
    engine.clear_board()
    for y in (2, 6, 10, 14):
        if not board.is_empty(0, y):
            continue
        board.spawn(Source, 0, y, cell.DIR_PX)
        for x in range(2, 30):
            if x == 16:
                if all(board.is_empty(cx, cy) for (cx, cy)
                       in Shear.cells_for(x, y, cell.DIR_PX, False)):
                    board.spawn(Shear, x, y, cell.DIR_PX)
                continue
            if x == 17:
                continue
            if board.is_empty(x, y):
                board.spawn(Belt, x, y, cell.DIR_PX)
    engine.invalidate_segments()
    engine.sync_segments()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--edits", type=int, default=120)
    args = ap.parse_args()
    rng = random.Random(args.seed)

    seed_board()
    failures = list(compare("initial"))

    for i in range(args.edits):
        # Tick a little, so caches are populated and material is in flight.
        for _ in range(rng.randint(1, 4)):
            engine.clock.request_step(1)
            engine.clock.advance(0.0)
        failures += compare(f"edit {i} (before)")

        x, y = rng.randrange(1, 30), rng.choice((2, 6, 10, 14))
        if rng.random() < 0.5:
            engine.erase(x, y)
        else:
            if board.is_empty(x, y):
                board.spawn(rng.choice((Belt, Belt, Shear)), x, y,
                            rng.randrange(4))
        engine.sync_segments()
        failures += compare(f"edit {i} (after)")

        if failures:
            break

    print(f"seed {args.seed}, {args.edits} edits, "
          f"{sum(1 for _ in gates.gates())} gates")
    if failures:
        print(f"\n{len(failures)} DISAGREEMENT(S):")
        for f in failures[:10]:
            print("  " + f)
        return 1
    print("cached and fresh agree at every checkpoint")
    return 0


if __name__ == "__main__":
    sys.exit(main())
