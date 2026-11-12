"""Profile the simulation tick on a large, synthetic factory.

Run as:

    python tools/profile_tick.py            # the default sweep
    python tools/profile_tick.py --profile   # add a cProfile breakdown
    python tools/profile_tick.py --scale 4   # a bigger board

Why this exists
---------------

Tick cost is reported in the panel as a number of segments and gates, which
says nothing about *where* the time goes. The two phases have quite different
shapes -- the belt phase is O(segments) by construction, the structure phase
is written as O(entity table capacity) -- so a board that feels slow needs
the two separated before anything is optimised.

The boards here are synthetic but shaped like a real one: long belt runs fed
by Sources, with gates spaced along them, which is what produces many
segments *and* many gate rows at once. `--scale` multiplies both.

Three experiments, each isolating one suspected cost:

  phases    -- structure vs belt, per tick, as the board grows
  parts     -- inside each phase: gate discovery, port geometry, transport
  churn     -- the cost of a segment rebuild, which an edit pays

The numbers are per *tick* in milliseconds. At 10 ticks/sec a tick has a
100ms budget to share with rendering, so anything approaching single-digit
milliseconds is already eating the frame.
"""

import argparse
import cProfile
import io
import pstats
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests import harness  # noqa: E402  (creates the GL context at import)
from core import cell  # noqa: E402
from core.entity import Belt, Shear, Source  # noqa: E402

# The harness board is 64x64, which is too small to hold the thousands of
# entities this is about. Resize it in place -- the engine, camera and segment
# index all hold a reference to this object, so replacing it would strand them.
BOARD = 512
board = harness.board
engine = harness.engine


def _enlarge_board(size=BOARD):
    import numpy as np
    from core.gameboard import GRID_CHANNELS
    if board.width >= size:
        return
    board.width = board.height = size
    board.grid = np.zeros((size, size, GRID_CHANNELS), dtype=np.int32)
    board.mark_all_dirty()


_enlarge_board()


# --- board construction ----------------------------------------------------
def build_factory(runs, run_len, gate_every=0):
    """Lay `runs` horizontal belt runs of `run_len` cells, fed by Sources.

    Returns (segments, gate_rows). Built straight through the engine rather
    than through the input manager: this is about simulation cost, and driving
    a few thousand placements through the gesture machine would dominate the
    setup time without changing what is being measured.

    `gate_every` > 0 breaks each run with a Shear that many cells along, which
    is what turns one long segment into several short ones *and* adds gate
    rows -- the combination a real factory has and a bare belt field does not.
    """
    engine.clear_board()
    y = 1
    for _ in range(runs):
        if y + 2 >= board.height:
            break
        # A Source at the start of the run, so the belts actually carry items:
        # an empty belt and a full one are different measurements.
        _place(Source, 0, y)
        x = 2
        while x < run_len:
            if gate_every and x % gate_every == 0 and x + 2 < run_len:
                _place(Shear, x, y)
                x += 2
                continue
            _place_belt(x, y)
            x += 1
        y += 3
    engine.invalidate_segments()
    engine.sync_segments()
    return engine.segments.count, board.live_count


def _place(entity_cls, x, y):
    """Spawn one structure, skipping a clash."""
    cells = entity_cls.cells_for(x, y, cell.DIR_PX, False)
    if any(not board.is_empty(cx, cy) for (cx, cy) in cells):
        return None
    return board.spawn(entity_cls, x, y, cell.DIR_PX)


def _place_belt(x, y):
    if not board.is_empty(x, y):
        return None
    return board.spawn(Belt, x, y, cell.DIR_PX)


def warm(ticks=30):
    """Run the sim until items are actually on the belts.

    An empty board measures the wrong thing: the belt phase's pop/push
    branches and the gate phase's fire path only run once material exists.
    """
    for _ in range(ticks):
        engine.clock.request_step(1)
        engine.clock.advance(0.0)


# --- timing ----------------------------------------------------------------
def time_calls(fn, n):
    """Mean milliseconds per call over `n` calls, best of 3 batches."""
    best = None
    for _ in range(3):
        t0 = time.perf_counter()
        for _ in range(n):
            fn()
        dt = (time.perf_counter() - t0) / n * 1e3
        best = dt if best is None else min(best, dt)
    return best


def experiment_phases(scale):
    """structure vs belt, as the board grows. The headline number."""
    print("\n=== phases: ms per tick, by board size ===")
    print(f"{'runs':>5} {'segs':>6} {'rows':>6} {'cap':>6} "
          f"{'structure':>10} {'belt':>8} {'total':>8} {'ticks/s':>8}")
    for runs in (4 * scale, 16 * scale, 48 * scale, 96 * scale):
        segs, rows = build_factory(runs, 80, gate_every=12)
        warm()
        s = time_calls(engine._structure_phase, 20)
        b = time_calls(engine._belt_phase, 20)
        total = s + b
        rate = 1e3 / total if total else float("inf")
        print(f"{runs:>5} {segs:>6} {rows:>6} {board.capacity:>6} "
              f"{s:>10.2f} {b:>8.2f} {total:>8.2f} {rate:>8.0f}")


def experiment_parts(scale):
    """Inside the phases: what the structure phase actually spends time on.

    `gates()` walks the whole entity table; `_connected_channels()` recomputes
    port geometry per gate per tick. Timing them separately says which.
    """
    print("\n=== parts: ms per tick, 48-run board ===")
    build_factory(48 * scale, 80, gate_every=12)
    warm()
    gates = engine.gates

    def discovery():
        for _ in gates.gates():
            pass

    def geometry():
        for g in gates.gates():
            gates._output_faces(g)

    def connectivity():
        for g in gates.gates():
            gates._connected_channels(g)

    def transport():
        engine.belts.tick(engine.clock.tick)

    n_gates = sum(1 for _ in gates.gates())
    print(f"  gates on the board: {n_gates}, "
          f"entity table capacity: {board.capacity}")
    for name, fn in (("gates() discovery", discovery),
                     ("+ _output_faces", geometry),
                     ("+ _connected_channels", connectivity),
                     ("structure_phase (all)", gates.structure_phase),
                     ("belts.tick (bare)", transport),
                     ("deliver_direct", gates.deliver_direct),
                     ("belt phase (all)", engine._belt_phase)):
        print(f"  {name:<24} {time_calls(fn, 20):>8.2f}")


def experiment_churn(scale):
    """What an edit costs: a full segment rebuild, which runs once per frame."""
    print("\n=== churn: ms per segment rebuild ===")
    for runs in (16 * scale, 48 * scale, 96 * scale):
        segs, rows = build_factory(runs, 80, gate_every=12)
        warm()

        def rebuild():
            engine.invalidate_segments()
            engine.sync_segments()

        print(f"  {runs:>4} runs, {segs:>5} segments: "
              f"{time_calls(rebuild, 5):>8.2f} ms")


def experiment_profile(scale):
    """A cProfile breakdown of 50 whole ticks, by cumulative time."""
    print("\n=== cProfile: 50 ticks on a 48-run board ===")
    build_factory(48 * scale, 80, gate_every=12)
    warm()

    def run():
        for _ in range(50):
            engine._structure_phase()
            engine._belt_phase()

    prof = cProfile.Profile()
    prof.enable()
    run()
    prof.disable()
    buf = io.StringIO()
    pstats.Stats(prof, stream=buf).sort_stats("tottime").print_stats(25)
    print(buf.getvalue())


def experiment_profile_rebuild(scale):
    """A cProfile breakdown of the segment rebuild, the edit-time cost."""
    print("\n=== cProfile: 10 segment rebuilds on a 48-run board ===")
    build_factory(48 * scale, 80, gate_every=12)
    warm()

    def run():
        for _ in range(10):
            engine.invalidate_segments()
            engine.sync_segments()

    prof = cProfile.Profile()
    prof.enable()
    run()
    prof.disable()
    buf = io.StringIO()
    pstats.Stats(prof, stream=buf).sort_stats("tottime").print_stats(16)
    print(buf.getvalue())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scale", type=int, default=1,
                    help="multiply every board size")
    ap.add_argument("--profile", action="store_true",
                    help="add a cProfile breakdown")
    args = ap.parse_args()

    print(f"board {board.width}x{board.height}, scale {args.scale}")
    experiment_phases(args.scale)
    experiment_parts(args.scale)
    experiment_churn(args.scale)
    if args.profile:
        experiment_profile(args.scale)
        experiment_profile_rebuild(args.scale)


if __name__ == "__main__":
    main()
