"""Can a vectorised prefilter cut the structure phase's Python work?

The scatter question, answered by measurement
---------------------------------------------

The obvious reading of "42% of the tick is view/row access" is that the entity
table needs sorting so gates can be sliced contiguously. Measured, that is
**wrong**, and the numbers are worth keeping:

    reading a 4-wide channel block for 672 gates
      scattered fancy  a[idx][:,5:9]   0.0071 ms
      contiguous idx   a[idx][:,5:9]   0.0061 ms   <- sorting buys ~15%
      pure slice       a[1:673,5:9]    0.0001 ms   <- slicing buys 60x

So sortedness alone is nearly worthless; only real *slicing* is fast. But the
absolute scale is what settles it: the whole scattered gather is 0.006 ms
against a 1.30 ms structure phase. Data access is already 0.5% of the cost.
The 1.3 ms is ~48k Python-level calls.

That makes row layout the wrong lever. The right one is **doing Python work
for fewer entities**, which is what this probe measures: one vectorised pass
over the whole table to find the gates that could possibly act this tick, then
the existing per-gate code on just those.

Why this is the GPU-compatible shape
------------------------------------

This is not a CPU-only trick. A compute dispatch wants exactly the same two
steps: a cheap parallel predicate over every entity, then a compacted index
buffer that the expensive kernel is dispatched over. The prefilter *is* the
predicate pass, and the surviving index list *is* the compaction. Building it
now means the port replaces each half with a dispatch rather than restructuring
the phase.

What it does and does not prove
-------------------------------

The prefilter here is deliberately **conservative**: it must never exclude a
gate the real phase would have fired, or it changes behaviour. It tests only
the two necessary conditions that are pure row state -- every input channel a
type needs is fed, and at least one output zone is free -- and lets everything
else through to the unchanged Python path. Per-type fire rules, connectivity
and the arithmetic are untouched.

Run:  python tools/probe_batch_prefilter.py [--scale 2]
"""

import argparse
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests import harness  # noqa: E402
from core import cell  # noqa: E402
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


# --- the two boards, which behave very differently -------------------------
def build_starved(runs, run_len=80, gate_every=12):
    """Gates spaced along runs. Most are starved or blocked -- the sparse case."""
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
    return _settle()


def build_saturated(runs, run_len=80):
    """Source -> long belt, no mid-run gates. Every gate fires every tick."""
    engine.clear_board()
    y = 1
    for _ in range(runs):
        if y + 2 >= board.height:
            break
        if board.is_empty(0, y):
            board.spawn(Source, 0, y, cell.DIR_PX)
        for x in range(2, run_len):
            if board.is_empty(x, y):
                board.spawn(Belt, x, y, cell.DIR_PX)
        y += 3
    return _settle()


def _settle():
    engine.invalidate_segments()
    engine.sync_segments()
    for _ in range(40):
        engine.clock.request_step(1)
        engine.clock.advance(0.0)
    return engine.segments.count, sum(1 for _ in engine.gates.gates())


# --- the prefilter ---------------------------------------------------------
def gate_rows():
    return np.asarray([g.row for g in engine.gates.gates()], dtype=np.int64)


def needed_inputs_by_type():
    """type id -> how many input channels that type has. Class data."""
    out = {}
    for t, klass in Entity._registry.items():
        try:
            out[t] = klass.in_channels()
        except Exception:
            out[t] = 0
    return out


def build_prefilter(rows):
    """Precompute what the vectorised pass needs. Done once per rebuild."""
    ents = board.entities
    types = ents[rows, Entity.F_TYPE].astype(np.int64)
    need = needed_inputs_by_type()
    n_in = np.asarray([need.get(int(t), 0) for t in types])
    n_out = np.asarray(
        [Entity._registry[int(t)].out_channels() for t in types])
    return types, n_in, n_out


def candidates(rows, n_in, n_out):
    """Rows that could possibly act this tick. Conservative: never excludes
    a gate the real phase would fire.

    Two necessary conditions, both pure row state:
      - every input channel the type declares is fed (FIRE_ALL's requirement;
        a type with 0 inputs is vacuously ready, which is Source)
      - at least one output zone is free (if all are full the gate is blocked
        under both the all-or-nothing and the generator rule)
    """
    ents = board.entities
    blk = ents[rows]
    ih, oh = Entity.F_IN_HELD, Entity.F_OUT_HELD
    held_in = blk[:, ih:ih + MAX_CHANNELS] > 0.0
    free_out = blk[:, oh:oh + MAX_CHANNELS] <= 0.0

    idx = np.arange(MAX_CHANNELS)
    in_mask = idx[None, :] < n_in[:, None]
    out_mask = idx[None, :] < n_out[:, None]

    fed = ((held_in & in_mask).sum(1) == n_in)
    any_free = ((free_out & out_mask).sum(1) > 0) | (n_out == 0)
    return rows[fed & any_free]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scale", type=int, default=2)
    args = ap.parse_args()
    runs = 48 * args.scale

    for label, build in (("starved (gates mid-run)", build_starved),
                         ("saturated (all firing)", build_saturated)):
        segs, ng = build(runs)
        rows = gate_rows()
        if not len(rows):
            print(f"\n=== {label}: no gates ===")
            continue
        _types, n_in, n_out = build_prefilter(rows)

        cand = candidates(rows, n_in, n_out)
        t_pre = ms(lambda: candidates(rows, n_in, n_out), 200)
        t_phase = ms(engine._structure_phase)

        # What the phase actually achieves, for context.
        before = dict(engine.gates.stats)
        for _ in range(20):
            engine.clock.request_step(1)
            engine.clock.advance(0.0)
        d = {k: engine.gates.stats[k] - before[k] for k in engine.gates.stats}

        print(f"\n=== {label} ===")
        print(f"  {segs} segments, {ng} gates")
        print(f"  fired/tick {d['fired'] / 20:.0f}  "
              f"starved/tick {d['starved'] / 20:.0f}  "
              f"blocked/tick {d['blocked'] / 20:.0f}")
        print(f"  prefilter admits {len(cand)} of {ng} gates "
              f"({100 * len(cand) / ng:.0f}%)")
        print(f"  prefilter cost   {t_pre:>7.4f} ms")
        print(f"  structure phase  {t_phase:>7.3f} ms")
        if len(cand) < ng:
            saved = t_phase * (1 - len(cand) / ng) - t_pre
            print(f"  projected phase  {t_phase - saved:>7.3f} ms "
                  f"({t_phase / max(t_phase - saved, 1e-9):.1f}x)")
        else:
            print("  projected phase  no gain: every gate is a candidate")


if __name__ == "__main__":
    main()
