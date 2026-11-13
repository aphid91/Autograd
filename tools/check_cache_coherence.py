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

#: Checkpoints where a given cache actually had a stored answer to compare.
#: Reported by `main()`, because "agreed everywhere" is the same output
#: whether the check ran or found nothing to look at -- and for the
#: direct-feeder index the second is the common case, since a correct edit
#: drops it and the comparison then has nothing to do.
stats = {"direct_checked": 0}


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
    """Every gate's and segment's cached answers against fresh ones."""
    problems = []
    # Segment end resolution: cached (row, channel) against a fresh resolve.
    # Compared as rows rather than views, since that is what is stored --
    # a view is rebuilt per call and two views of one row are not equal.
    for seg in engine.segments.segments.values():
        for which, fresh in (("src", gates._resolve_src),
                             ("dst", gates._resolve_dst)):
            cache = (gates._src_cache if which == "src"
                     else gates._dst_cache)
            if seg.sid not in cache:
                continue
            want = fresh(seg)
            got = cache[seg.sid]
            if got != want:
                problems.append(
                    f"{label}: segment {seg.sid} {which} "
                    f"cached {got} fresh {want}")
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
    # The **direct-feeder index**, and its inverse. Same recycling hazard as
    # everything above -- it is keyed on rows, which the free list reuses, and
    # derived from port geometry an in-place turn changes -- but with a worse
    # consequence: a stale entry does not produce a wrong port, it lets a gate
    # sleep on a claim about whichever machine now owns that row, which is a
    # silent permanent stall.
    #
    # It is also the one derivation here that is **not** dropped by
    # `invalidate_connectivity()` (the per-tick backstop would make a
    # whole-board walk a per-tick cost -- measured at 58 rebuilds over 50
    # ticks, taking the tick from 6.8 to 12.3ms), so it relies entirely on
    # `invalidate_board()` being reached by every real edit. That is exactly
    # the kind of claim this tool exists to check rather than assume.
    # **Read what is stored, do not call the accessor**, which would derive
    # the index on a miss and then compare it against itself -- a check that
    # passes by construction. That mistake made this vacuous once already:
    # calling `_direct_feeders()` here repopulated the cache at every
    # checkpoint, so a missing invalidation was healed by the checker before
    # the next comparison could see it.
    # **And put back what was stored**, which is the other half of the same
    # mistake. Deriving the fresh copy requires clearing the slot, and leaving
    # it cleared makes this check *self-healing*: it would hand the engine a
    # correctly-empty cache at every checkpoint, so a missing invalidation
    # could never survive to be observed. Mutation-tested -- gutting the drop
    # in `invalidate_board()` passed until the restore was added.
    cached_direct = gates._direct_map
    if cached_direct is None:
        # Never derived, or correctly dropped: nothing stale to find, and
        # deriving one here would only measure this function.
        #
        # **This being the usual case after an edit is the point, not a
        # weakness** -- but it does mean the comparison alone cannot tell
        # "correctly dropped" from "never populated", so `direct_checked`
        # counts the checkpoints that actually had something to compare.
        # `main()` reports it, because a run where that is 0 proves nothing
        # about this index at all. That is the same vacuity discipline
        # `check_wakelist_safe.py` applies to its wake counts.
        return problems
    stats["direct_checked"] += 1
    gates._direct_map = None
    gates._direct_down = None
    fresh_direct = gates._direct_feeders()
    gates._direct_map = cached_direct
    gates._direct_down = None
    if cached_direct != fresh_direct:
        only_cached = set(cached_direct.items()) - set(fresh_direct.items())
        only_fresh = set(fresh_direct.items()) - set(cached_direct.items())
        problems.append(
            f"{label}: direct-feeder index stale\n"
            f"    cached-only {sorted(only_cached)[:4]}\n"
            f"    fresh-only  {sorted(only_fresh)[:4]}")
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
    # A row of **abutting gates**, so the direct-feeder index is non-empty.
    # Without it that check is vacuous: every row above is
    # Source -> belt -> Shear -> belt, with no two gates touching, so the
    # index stays `{}` and compares equal to a fresh `{}` however stale the
    # invalidation is. Mutation-tested -- removing the drop from
    # `invalidate_board()` passed until this row existed.
    #
    # Three gates long, and placed where the random edits below can reach
    # them, since the hazard is specifically an *edit* leaving the index
    # describing a board that no longer exists.
    y = 18
    if all(board.is_empty(cx, cy)
           for (cx, cy) in Shear.cells_for(2, y, cell.DIR_PX, False)):
        board.spawn(Shear, 2, y, cell.DIR_PX)
    if all(board.is_empty(cx, cy)
           for (cx, cy) in Shear.cells_for(4, y, cell.DIR_PX, False)):
        board.spawn(Shear, 4, y, cell.DIR_PX)
    if all(board.is_empty(cx, cy)
           for (cx, cy) in Source.cells_for(4, y + 1, cell.DIR_NY, False)):
        board.spawn(Source, 4, y + 1, cell.DIR_NY)
    for x in range(6, 12):
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

        # Row 18 is the abutting-gates row, and it is weighted heavily
        # **and narrowed to the gates' own columns**, because that is the only
        # edit that can invalidate the direct-feeder index. Spread uniformly
        # over x in 1..30 and five rows, only 2 edits in 120 touched it --
        # measured -- so the check ran 178 times and never once had a reason
        # to disagree. A checkpoint count alone cannot show that; the edits
        # have to be aimed at the structure under test.
        if rng.random() < 0.4:
            x, y = rng.randrange(1, 8), 18
        else:
            x, y = rng.randrange(1, 30), rng.choice((2, 6, 10, 14))
        if rng.random() < 0.5:
            engine.erase(x, y)
        else:
            if board.is_empty(x, y):
                board.spawn(rng.choice((Belt, Belt, Shear)), x, y,
                            rng.randrange(4))
        # **Rebuild the abutting pair when the churn has destroyed it.** Left
        # alone, row 18 is erased within a few dozen edits and never comes
        # back as two touching gates, so the index is permanently `{}` for the
        # rest of the run and every later checkpoint is vacuous. Re-seeding
        # keeps the structure under test actually present.
        if y == 18 and rng.random() < 0.5:
            for gx in (2, 4):
                cells = Shear.cells_for(gx, 18, cell.DIR_PX, False)
                if all(board.is_empty(cx, cy) for (cx, cy) in cells):
                    board.spawn(Shear, gx, 18, cell.DIR_PX)
        engine.sync_segments()
        failures += compare(f"edit {i} (after)")

        if failures:
            break

    print(f"seed {args.seed}, {args.edits} edits, "
          f"{sum(1 for _ in gates.gates())} gates")
    print(f"  direct-feeder index compared at {stats['direct_checked']} "
          f"checkpoint(s) that had a cached answer")
    if not stats["direct_checked"]:
        print("  WARNING: the direct-feeder index was never populated at a "
              "checkpoint, so this run says nothing about its invalidation")
    if failures:
        print(f"\n{len(failures)} DISAGREEMENT(S):")
        for f in failures[:10]:
            print("  " + f)
        return 1
    print("cached and fresh agree at every checkpoint")
    return 0


if __name__ == "__main__":
    sys.exit(main())
