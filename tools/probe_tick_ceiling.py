"""What is the tick's cost actually made of, and what is the ceiling?

The first round of optimisation removed the recomputation (see
tools/profile_tick.py). What is left is flat: no single hotspot, ~48k Python
function calls per tick, and the top entries are all *view machinery* --
`Gameboard.view()`, `Entity.__init__`, `_get`, `origin`, `is_ghost`. That is a
different kind of problem from a hot loop, and it needs a different
measurement: not "which function is slow" but "how much of this is the
representation rather than the work".

The experiments:

  attribution  -- group the profile by what the time is *for*, not by function
  per_entity   -- cost per gate and per segment, to separate fixed from linear
  view_tax     -- the same field read through a view vs. straight from numpy
  vector_floor -- what the structure phase's readiness test costs vectorised,
                  which is the floor a batched/GPU-shaped implementation hits

`vector_floor` is the important one for the GPU question: it measures the same
decision the structure phase makes, taken for every gate at once with numpy
instead of per gate with Python. That is the shape a compute dispatch has, so
it is a credible estimate of the headroom -- and it is measured rather than
asserted.
"""

import argparse
import cProfile
import pstats
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests import harness  # noqa: E402
from core import cell  # noqa: E402
from core.entity import (  # noqa: E402
    MAX_CHANNELS, Belt, Entity, Shear, Source)
from core.gameboard import GRID_CHANNELS  # noqa: E402

BOARD = 512
board = harness.board
engine = harness.engine


def _enlarge():
    import numpy as np
    if board.width >= BOARD:
        return
    board.width = board.height = BOARD
    board.grid = np.zeros((BOARD, BOARD, GRID_CHANNELS), dtype=np.int32)
    board.mark_all_dirty()


_enlarge()


def build(runs, run_len=80, gate_every=12):
    engine.clear_board()
    y = 1
    for _ in range(runs):
        if y + 2 >= board.height:
            break
        if board.is_empty(0, y):
            board.spawn(Source, 0, y, cell.DIR_PX)
        x = 2
        while x < run_len:
            if gate_every and x % gate_every == 0 and x + 2 < run_len:
                if all(board.is_empty(cx, cy) for (cx, cy)
                       in Shear.cells_for(x, y, cell.DIR_PX, False)):
                    board.spawn(Shear, x, y, cell.DIR_PX)
                x += 2
                continue
            if board.is_empty(x, y):
                board.spawn(Belt, x, y, cell.DIR_PX)
            x += 1
        y += 3
    engine.invalidate_segments()
    engine.sync_segments()
    for _ in range(30):
        engine.clock.request_step(1)
        engine.clock.advance(0.0)
    return engine.segments.count, sum(1 for _ in engine.gates.gates())


def ms(fn, n=20):
    best = None
    for _ in range(3):
        t0 = time.perf_counter()
        for _ in range(n):
            fn()
        dt = (time.perf_counter() - t0) / n * 1e3
        best = dt if best is None else min(best, dt)
    return best


def tick():
    engine._structure_phase()
    engine._belt_phase()


# --- 1. attribution --------------------------------------------------------
#: Which concern each function belongs to. The point is to separate "the
#: simulation's actual decisions" from "the cost of reaching the data".
_BUCKETS = (
    ("view/row access", (
        "view", "view_for", "__init__", "_get", "_set", "origin", "is_ghost",
        "ghost_state", "type_id", "direction", "flipped", "is_row_alive",
        "live_rows", "in_bounds", "alive", "footprint_w", "footprint_h")),
    ("gate discovery", ("gates",)),
    ("port resolution", (
        "_src_port", "_dst_port", "_output_faces", "_input_channel_for",
        "_connected_channels", "_compute_connected_channels",
        "_face_has_consumer", "_channel_of_face", "faces_for",
        "faces_at_origin", "place_local", "rotate_offset",
        "rotate_dir_ccw_n")),
    ("gate firing", (
        "structure_phase", "inputs_ready", "fire", "consumed", "in_channels",
        "out_channels", "input_count", "output_count", "outputs_clear",
        "in_held", "out_held", "in_value", "out_value", "set_input",
        "set_output", "clear_input", "clear_output", "_fire_generator",
        "in_channel", "out_channel")),
    ("belt transport", (
        "tick", "tick_segment", "position_of", "has_room", "_push", "push",
        "payload_of", "length_of", "capacity_of", "positions")),
    ("port callbacks", (
        "offer_to", "accept_from", "took_from", "deliver_direct",
        "tick_belts", "_belt_phase", "_structure_phase", "set_tick",
        "invalidate_connectivity")),
)


def experiment_attribution(scale):
    print("\n=== attribution: where 50 ticks' time goes, by concern ===")
    segs, n_gates = build(48 * scale)
    prof = cProfile.Profile()
    prof.enable()
    for _ in range(50):
        tick()
    prof.disable()

    want = {}
    for label, names in _BUCKETS:
        for n in names:
            want.setdefault(n, label)

    totals = defaultdict(float)
    calls = defaultdict(int)
    grand = 0.0
    for (fpath, _lineno, fname), st in pstats.Stats(prof).stats.items():
        tt = st[2]
        grand += tt
        if "Gates" not in str(fpath) and "{" not in str(fpath):
            label = "python builtins"
        else:
            label = want.get(fname)
            if label is None:
                label = ("python builtins" if not str(fpath).endswith(".py")
                         else "unclassified")
        totals[label] += tt
        calls[label] += st[0]

    print(f"  {segs} segments, {n_gates} gates, "
          f"{sum(calls.values()) // 50} calls/tick")
    for label in sorted(totals, key=lambda k: -totals[k]):
        print(f"  {label:<18} {totals[label] * 1e3 / 50:>7.3f} ms/tick "
              f"{100 * totals[label] / grand:>5.1f}%  "
              f"{calls[label] // 50:>7} calls/tick")


# --- 2. per-entity marginal cost -------------------------------------------
def experiment_per_entity(scale):
    """Separate the fixed cost from the per-gate and per-segment slopes."""
    print("\n=== per-entity: marginal cost of one more gate / segment ===")
    rows = []
    for runs in (12 * scale, 24 * scale, 48 * scale, 96 * scale):
        segs, n_gates = build(runs)
        rows.append((n_gates, segs, ms(engine._structure_phase),
                     ms(engine._belt_phase)))
    print(f"  {'gates':>6} {'segs':>6} {'struct':>8} {'belt':>8} "
          f"{'us/gate':>9} {'us/seg':>8}")
    for (g, s, st, bl) in rows:
        print(f"  {g:>6} {s:>6} {st:>8.2f} {bl:>8.2f} "
              f"{st * 1e3 / g:>9.2f} {bl * 1e3 / s:>8.2f}")


# --- 3. the view tax -------------------------------------------------------
def experiment_view_tax(scale):
    """One field, read three ways. The representation's overhead, isolated."""
    print("\n=== view tax: reading one field per gate, three ways ===")
    build(48 * scale)
    rows = [g.row for g in engine.gates.gates()]
    ents = board.entities
    off = Entity.F_OUT_HELD

    def through_view():
        total = 0.0
        for r in rows:
            total += board.view(r).out_value(0)
        return total

    def direct_scalar():
        total = 0.0
        for r in rows:
            total += float(ents[r, off])
        return total

    def vectorised():
        import numpy as np
        return float(ents[np.asarray(rows), off].sum())

    n = len(rows)
    for name, fn in (("board.view(r).out_value", through_view),
                     ("ents[r, off] scalar", direct_scalar),
                     ("ents[rows, off] vector", vectorised)):
        t = ms(fn, 50)
        print(f"  {name:<24} {t:>7.3f} ms  ({t * 1e6 / n:>6.1f} ns/gate)")


# --- 4. the vectorised floor ----------------------------------------------
def experiment_vector_floor(scale):
    """The readiness test for every gate at once, in numpy.

    This is the structure phase's central decision -- "are my inputs fed and
    my outputs clear" -- taken over the whole entity table as array ops
    instead of per gate in Python. It is deliberately *not* a drop-in
    replacement (it ignores per-type fire rules and connectivity), so it is a
    lower bound rather than a proposal. What it establishes is the order of
    magnitude a batched implementation could reach, which is the same shape
    the GPU port would take.
    """
    import numpy as np
    print("\n=== vector floor: readiness for every gate at once ===")
    segs, n_gates = build(48 * scale)
    ents = board.entities
    rows = np.asarray([g.row for g in engine.gates.gates()])
    n_ch = MAX_CHANNELS
    in_held = Entity.F_IN_HELD
    out_held = Entity.F_OUT_HELD

    def scalar_ready():
        out = []
        for g in engine.gates.gates():
            out.append(g.inputs_ready() and g.outputs_clear())
        return out

    def vector_ready():
        blk = ents[rows]
        fed = blk[:, in_held:in_held + n_ch] > 0.0
        clear = blk[:, out_held:out_held + n_ch] <= 0.0
        return fed.all(axis=1) & clear.all(axis=1)

    a = ms(scalar_ready, 20)
    b = ms(vector_ready, 20)
    print(f"  {n_gates} gates")
    print(f"  per-gate Python : {a:>7.3f} ms")
    print(f"  vectorised      : {b:>7.3f} ms   ({a / b:>5.1f}x faster)")
    print(f"  full structure  : {ms(engine._structure_phase):>7.3f} ms")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scale", type=int, default=2)
    args = ap.parse_args()
    print(f"board {board.width}x{board.height}, scale {args.scale}")
    experiment_attribution(args.scale)
    experiment_per_entity(args.scale)
    experiment_view_tax(args.scale)
    experiment_vector_floor(args.scale)


if __name__ == "__main__":
    main()
