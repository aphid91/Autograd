"""Differential check: GPU sleeping must not strand material.

The GPU sleep layer (core/sleep_device.py) shrinks each tick's dispatch to the
entities that can act. That is only sound if a sleeping entity really could do
nothing *and* something wakes it the moment that stops being true. Both halves
fail silently and cumulatively: a missed wake corrupts nothing on the tick it
happens, it just fails to move one entity, and on a board the size of a CPU
that reads as a logic bug in the player's circuit rather than a scheduler bug
in the engine.

So this runs the real boards twice -- sleeping and fully dispatched -- and
checks two different things, because neither alone is sufficient.

Why two checks and not one
--------------------------

**Conservation** (`check_conservation`) runs both paths for many ticks and
compares how much material is on the belts. A stranded item never arrives, so
the sleeping total falls behind and stays behind.

It cannot be an exact state diff, and that is worth stating because the CPU
tool (`check_wakelist_safe.py`) *is* one. Two differences make equality the
wrong bar here:

- the GPU belt phase has no intra-phase visibility, so a gate drained by one
  run still looks loaded to another until the next tick (see the long note in
  shaders/belt_tick.comp). The two paths therefore reach slightly different
  *schedules* from identical state even with sleeping off;
- a wake costs a tick to propagate. A run whose producer fills a zone is
  woken by that producer and accepts on the following tick, so the sleeping
  path legitimately lags by one tick at each such hop.

Both are delays, not losses. Measured across 400 ticks the difference settles
under 1% and oscillates around zero rather than growing, which is the
signature being tested for.

**Lying sleepers** (`check_lying_sleepers`) is the strictly stronger half, and
is what actually catches most mutations. It asks, every tick: is any sleeping
entity's own premise already false?

  - a sleeping run whose producer is holding something it has room for;
  - a sleeping gate still holding an undelivered result.

A stale sleep is usually rescued by some other wake a tick or two later, so
material still flows and conservation agrees -- the diff cannot see it. What
is broken is that the scheduler holds a conclusion its premise no longer
supports. That is the same reasoning, and the same name, as
`lying_sleepers()` in the CPU tool, which likewise caught a mutation its diff
could not.

What is known-untested
----------------------

Recorded rather than left to be assumed covered, which is the habit the CPU
tool's history argues for:

- **The fold pass's wake** (`shaders/gate_fold.comp`). Removing it passes
  both checks on all three boards. It is unreachable by construction: the
  wake fires every tick, so a folded gate is never asleep -- instrumented on
  cpu.npz, 234 folded slots over 120 ticks, **0** asleep instances. Kept so
  every writer of a lane announces it, not because of a coincidence about
  which gates stay awake.
- **The wake on an edit.** The board never changes mid-run here, so removing
  `wake_all()` from `SleepSets.rebuild()` passes. Covering it needs an edit
  inside both timelines, and a rebuild reassigns every segment id and gate
  slot, so the comparison would have to be keyed on cells. Not attempted --
  the same gap, for the same reason, that the CPU tool documents.
- **The belt rest predicate's anchor clause**, invisible end to end for the
  reason core/belts.py gives: an empty run's unsaturated anchor re-saturates
  before any item position depends on it.

Mutation-tested, which is the only evidence a checker is not vacuous:

    M1  belt never re-arms itself              conservation   FAIL (45.8%)
    M2  firing gate skips its neighbours       lying          FAIL (301)
    M4  blocked gate owing output sleeps       lying          FAIL (348)

An earlier version of this check passed M1, M2 *and* M4, because it flipped
the sleep switch per tick -- and `set_gpu_sleep(True)` calls `wake_all()`, so
the sleeping timeline restarted from "everything awake" every tick and nothing
could ever be stranded. That is this file's own version of the vacuity the CPU
tool was written twice to escape, and it is why the switch is thrown once here
and the device-side sets are left alone.

    python tools/check_gpu_sleep_safe.py [--ticks N] [--boards a.npz,b.npz]
"""

import argparse
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests import harness  # noqa: E402
from core.entity import MAX_CHANNELS, Entity  # noqa: E402
from core.gameboard import GRID_CHANNELS  # noqa: E402

BOARD = 512
#: Within this much of the fully-dispatched item count. Not zero, for the two
#: reasons the module docstring gives -- both are one-tick delays.
TOLERANCE_PCT = 5.0

board = harness.board
engine = harness.engine


def _enlarge():
    if board.width >= BOARD:
        return
    board.width = board.height = BOARD
    board.grid = np.zeros((BOARD, BOARD, GRID_CHANNELS), dtype=np.int32)
    board.mark_all_dirty()


def _load(name):
    engine.load_board(str(ROOT / name))
    engine.sync_segments()


def check_conservation(name, ticks):
    """Item counts after a long run, both ways. Returns (sleep, full, pct)."""
    totals = {}
    for on in (True, False):
        _load(name)
        engine.gates.set_gpu_sleep(on)
        engine._gpu_tick(ticks, start=0)
        engine.settle()
        totals[on] = engine.belts.total_items()
    full = max(totals[False], 1)
    return totals[True], totals[False], 100.0 * (totals[True] - full) / full


def check_lying_sleepers(name, ticks):
    """Sleeping entities whose own premise is already false.

    Returns `(run_violations, gate_violations, awake_mean)`. Counted per tick
    and capped at one per family per tick, since a single missed wake tends to
    show up on many entities at once and the interesting number is "did this
    ever happen", not how wide it was.
    """
    _load(name)
    gates = engine.gates
    sets = gates.sleep_sets
    gates.set_gpu_sleep(True)
    bad_run = bad_gate = 0
    awake = []
    ents = board.entities
    for t in range(ticks):
        engine._gpu_tick(1, start=t)
        n_seg, n_gate = sets.counts()
        awake_seg = set(
            sets._bufs["awake_seg"].download()[:n_seg].tolist())
        awake_gate = set(
            sets._bufs["awake_gate"].download()[:n_gate].tolist())
        awake.append((n_seg, n_gate))
        engine.settle()

        # A sleeping run whose producer is holding something it has room for.
        # `has_room` at t+1 because that is the tick the run would act on.
        for sid, seg in engine.segments.segments.items():
            if sid in awake_seg:
                continue
            src = gates._src_cached(seg)
            if src is None:
                continue
            row, channel = src
            if not board.is_row_alive(row):
                continue
            if (board.view(row).out_held(channel)
                    and engine.belts.has_room(seg, t + 1)):
                bad_run += 1
                break

        # A sleeping gate still holding an undelivered result.
        for slot, row in enumerate(gates.state.rows.tolist()):
            if slot in awake_gate:
                continue
            if any(ents[row, Entity.F_OUT_HELD + c] > 0
                   for c in range(MAX_CHANNELS)):
                bad_gate += 1
                break
    mean = (sum(a[0] for a in awake) / max(len(awake), 1),
            sum(a[1] for a in awake) / max(len(awake), 1))
    return bad_run, bad_gate, mean


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticks", type=int, default=400)
    ap.add_argument("--lying-ticks", type=int, default=120)
    ap.add_argument("--boards", default="ram.npz,cpu.npz,Ram2.npz,noram.npz")
    args = ap.parse_args()
    _enlarge()

    if engine.gates.sleep_sets is None:
        print("no GPU sleep layer on this engine -- nothing to check")
        return 0

    names = [n for n in args.boards.split(",")
             if n and (ROOT / n).exists()]
    if not names:
        print("none of the named boards exist")
        return 1

    failures = []
    print(f"conservation over {args.ticks} ticks "
          f"(tolerance {TOLERANCE_PCT:.0f}%)")
    for name in names:
        sleep, full, pct = check_conservation(name, args.ticks)
        flag = "" if abs(pct) <= TOLERANCE_PCT else "   <-- FAIL"
        print(f"  {name:12s} sleeping {sleep:6d} vs full {full:6d}  "
              f"{pct:+6.1f}%{flag}")
        if abs(pct) > TOLERANCE_PCT:
            failures.append(f"{name}: item count off by {pct:+.1f}%")

    print(f"\nlying sleepers over {args.lying_ticks} ticks "
          f"(any is a failure)")
    for name in names:
        bad_run, bad_gate, mean = check_lying_sleepers(
            name, args.lying_ticks)
        sets = engine.gates.sleep_sets
        flag = "" if not (bad_run or bad_gate) else "   <-- FAIL"
        print(f"  {name:12s} {bad_run:4d} run-ticks, {bad_gate:4d} gate-ticks"
              f"  | mean awake {mean[0]:6.1f}/{sets.seg_count} segs, "
              f"{mean[1]:6.1f}/{sets.gate_count} gates{flag}")
        if bad_run or bad_gate:
            failures.append(
                f"{name}: {bad_run} run and {bad_gate} gate stale sleeps")
        # A run where nothing ever slept proves nothing, exactly as the CPU
        # tool's per-layer transition counts do.
        if mean[0] >= sets.seg_count or mean[1] >= sets.gate_count:
            print(f"  WARNING: nothing slept on {name}, so this board did "
                  f"not exercise the wake edges at all")

    if failures:
        print("\nFAILED:")
        for f in failures:
            print("  " + f)
        return 1
    print("\nGPU sleeping strands no material and tells no lies")
    return 0


if __name__ == "__main__":
    sys.exit(main())
