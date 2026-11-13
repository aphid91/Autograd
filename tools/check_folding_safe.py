"""Differential check: constant folding must not change what the board computes.

A fully-direct Source is replaced by a constant held permanently on its
neighbour's input channel, and the Source then stops firing (see "Constant
folding" in core/gates.py). That is an optimisation only if the factory
computes the same thing afterwards, and the failure mode is quiet: a folded
channel holding a stale or wrong value produces *plausible* arithmetic, so it
reads as a logic bug in the circuit rather than as a scheduler bug in the
engine.

So this runs the same board twice, tick for tick, with folding on and off, and
compares what the two timelines actually produce.


What is compared, and what deliberately is not
----------------------------------------------

**Not the entity rows wholesale**, which is what `check_wakelist_safe.py`
does. That would be wrong here rather than merely strict: a folded channel is
*supposed* to read as held when the unfolded timeline has it empty, and a
folded Source's waiting zones are *supposed* to stay clear where the unfolded
one fills them every tick. Those are the mechanism working, so diffing every
row would report a divergence on tick one and prove nothing thereafter.

The folded Sources' own rows and the folded channels themselves are excluded
by construction, and `main()` reports how many of each were excluded so an
excluded-everything run is visible rather than silent. What *is* compared is
set out below -- the choice took three attempts and each rejected version is
recorded, because each looked obviously right.

**The comparison is shift-invariant, and it has to be.** A folded input is
present before the first tick; the unfolded equivalent arrives one hop later
via `deliver_direct()`. That one tick of head start is the whole observable
difference folding makes, it is permanent, and it means the two timelines can
never be compared state-for-state at the same tick number:

  - `enter_tick` is an **absolute** tick, so every item on a time-shifted
    timeline carries a different number while sitting in exactly the same
    place;
  - `head` and `tail` are **absolute** monotonic counters, so a run that has
    pushed one more item reads `(22, 27)` against `(21, 26)` -- the same five
    items at the same positions.

Measured on the fixture: the folded run destroys items at **exactly** the same
rate (10/tick) with a constant cumulative lead of 9, never growing.

**The head start can also be *stored*, which rules out comparing ring
contents.** A run whose downstream end dangles fills to capacity and sleeps;
whether it settles holding 8 items or 9 depends on which tick it started
filling. So the folded and unfolded timelines sit permanently one item apart
on such a run, for ever, with no divergence in behaviour at all -- and a
comparison of item counts calls that a failure. A lag does not rescue it
either: that was measured too, and no single shift aligns the snapshots,
because `enter_tick`, `head` and `tail` are all absolute and differ under
every shift.

So the claim this file checks is the behavioural one -- **the same values
flow at the same rate, for ever** -- and the things compared are the ones
that can express it:

  - the per-tick **rate** at which Sinks destroy items, compared tick by tick
    once both timelines are past start-up. A folded constant that went stale,
    arrived twice, or stopped arriving changes this immediately.
  - the **cumulative lead**, which must be *constant*. This is the stronger
    half: a rate check alone passes on a board whose rate is briefly zero in
    both runs, while a lead that grows means one timeline is permanently
    gaining or losing items.
  - the **set of values** each non-folded gate has been seen holding over the
    whole run, which is where a wrong *value* shows up. The rates could match
    perfectly while the arithmetic was wrong, so something has to check the
    numbers -- but it cannot be a tick-by-tick comparison, for the reason
    below.

**Why the gate channels are compared as a set over time, not per tick.** The
stored head start turns into *back pressure*, and then into a permanent,
stable difference in whose zone is full. Measured on row 118 of the fixture: a
Shear whose downstream run dangles sits for ever with its output zone holding
27.0 in the folded timeline and empty in the unfolded one, because the folded
run filled to capacity (9 items) and the unfolded one stopped one short (8).
Both are correct steady states of the same factory one item apart. So "what
values does this gate ever hold" is comparable and "what is it holding right
now" is not.

`--warmup` ticks are run and not failed. It has to be generous: a belt-fed
input on this fixture takes ~26 ticks to arrive, so the two timelines are
legitimately different for far longer than the one tick folding itself costs.


Two timelines, one board
------------------------

The same interleaving `check_wakelist_safe.py` uses, and for the same reason:
two boards would need two engines and two GL contexts. Each tick is run twice
from the same starting snapshot -- once folded, once not -- and each timeline
then continues from its own result, so a divergence **accumulates** instead of
being reset every tick. Letting both continue from the folded result is the
mistake that made the first version of the wakelist checker vacuous, and it
would be the same mistake here.

Turning folding off goes through `GateSim.set_folding_enabled()`, which is
latched rather than a one-shot clear -- the fold is derived lazily, so merely
dropping it would have the next tick fold again and the oracle would stop
being an oracle immediately.

    python tools/check_folding_safe.py [--seed N] [--ticks N] [--warmup N]
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
    Belt, Floor, Mac, Max, Reciprocal, Shear, Sink, Source)
from core.gameboard import GRID_CHANNELS  # noqa: E402
from core.entity import MAX_CHANNELS, Entity  # noqa: E402

BOARD = 256
board = harness.board
engine = harness.engine
gates = engine.gates


def _enlarge():
    if board.width >= BOARD:
        return
    board.width = board.height = BOARD
    board.grid = np.zeros((BOARD, BOARD, GRID_CHANNELS), dtype=np.int32)
    board.mark_all_dirty()


_enlarge()


# --- state capture --------------------------------------------------------
def snapshot():
    """Everything a tick can change. Restored to re-run a tick."""
    segs = {}
    for sid, s in engine.segments.segments.items():
        segs[sid] = (s.head, s.tail, s.anchor,
                     tuple(s.enter_tick) if s.enter_tick is not None else (),
                     tuple(s.payload) if s.payload is not None else ())
    return (segs, board.entities.copy(), dict(gates.stats),
            set(engine.belts._asleep), set(gates._asleep))


def restore(snap):
    segs, ents, stats, belt_sleep, gate_sleep = snap
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
    gates.stats.clear()
    gates.stats.update(stats)
    engine.belts._asleep = set(belt_sleep)
    gates._asleep = set(gate_sleep)


def observable(snap, folded_rows, folded_channels):
    """The shift-invariant part of a snapshot. See the module docstring.

    The exclusions are passed in rather than recomputed so both timelines are
    measured against the *same* exclusion set -- deriving it per timeline
    would let the unfolded run exclude nothing and silently compare different
    things.
    """
    _segs, ents, stats, _bs, _gs = snap
    rows = {}
    for row in board.live_rows():
        if row in folded_rows:
            continue
        t = int(ents[row, Entity.F_TYPE])
        if t in (cell.TYPE_EMPTY, cell.TYPE_BELT, cell.TYPE_UNDER):
            continue
        vals = []
        for c in range(MAX_CHANNELS):
            if (row, c) in folded_channels:
                # The folded slot itself: held in one timeline by design.
                continue
            if ents[row, Entity.F_IN_HELD + c]:
                vals.append(("in", c, float(ents[row, Entity.F_IN_VALUE + c])))
            if ents[row, Entity.F_OUT_HELD + c]:
                vals.append(
                    ("out", c, float(ents[row, Entity.F_OUT_VALUE + c])))
        rows[row] = tuple(vals)
    return rows, stats.get("destroyed", 0), stats.get("consumed", 0)


def accumulate(seen, obs):
    """Fold one tick's gate channels into the per-row set of values seen.

    See the module docstring: a per-tick comparison is not available, so the
    values are collected over the whole run and compared as sets at the end.
    """
    rows, _destroyed, _consumed = obs
    for row, vals in rows.items():
        bucket = seen.setdefault(row, set())
        bucket.update(vals)


def compare(a, b, rate_a=None, rate_b=None):
    """Readable differences between two observable states.

    `rate_*` are the per-tick destroy counts. They are compared instead of
    the cumulative totals because the folded timeline keeps a constant lead
    from its one-tick head start -- see the module docstring. A *rate*
    mismatch is the real failure: it means values stopped flowing, or started
    flowing twice.
    """
    out = []
    if rate_a is not None and rate_a != rate_b:
        out.append(
            f"sinks destroyed {rate_a}/tick folded vs {rate_b}/tick unfolded")
    return out


def compare_seen(seen_a, seen_b):
    """Differences between the value sets each timeline's gates ever held.

    **Compared one direction at a time, because the two are not symmetric.**
    The folded timeline runs one item ahead, and that lead turns into back
    pressure: a gate whose downstream run filled to capacity in the folded
    run visits a *blocked* state -- its output zone holding a result -- that
    the unfolded run, one item short of full, never reaches. That is a
    strictly larger set of states over the same values, not a wrong value.

    What would be a real failure is a **value** appearing in one run that the
    other never computes at all, in either a held or a blocked state. So the
    comparison is on the (channel, value) pairs with the held/blocked
    distinction dropped, and it is reported in both directions separately so
    "the folded run also blocks sometimes" cannot hide "the folded run
    computed a number the unfolded one never did".
    """
    out = []
    for row in sorted(set(seen_a) | set(seen_b)):
        va = {(c, v) for (_kind, c, v) in seen_a.get(row, set())}
        vb = {(c, v) for (_kind, c, v) in seen_b.get(row, set())}
        only_a, only_b = va - vb, vb - va
        if only_a:
            out.append(
                f"row {row}: folded computed (channel, value) "
                f"{sorted(only_a)[:4]} which unfolded never did")
        if only_b:
            out.append(
                f"row {row}: unfolded computed (channel, value) "
                f"{sorted(only_b)[:4]} which folded never did")
    return out


# --- the board ------------------------------------------------------------
def build(rng, rows=10):
    """A board built around the motif folding exists for.

    The shape that matters is **a gate with some inputs wired straight to a
    Source and some fed by belt** -- the user's "multiply by negative one": a
    Mac with two constant Sources and one real belt input. That is the only
    shape where folding and not folding could disagree, because it is the only
    one where a folded constant has to compose with material arriving on a
    belt at a belt's rate.

    So each row here is:

        Source -> belt run -> [gate with a Source abutted to another face]
                                      -> belt run -> Sink

    plus, deliberately mixed in:

      - **gates with every input folded**, which must still fire at full rate
        with no belt involved anywhere;
      - **a Source feeding both a belt and a gate**, which must *not* fold and
        is the narrowing most likely to regress;
      - Sinks on the far end, so values leave the board somewhere countable.
    """
    # The geometry is spelled out rather than guessed, because a fixture that
    # *almost* abuts folds nothing and the check then silently proves nothing
    # -- which is exactly what the first version of this file did. A Shear or
    # Mac placed at (gx, y) facing +X takes its x input on the +Y face of
    # (gx, y), i.e. from the cell (gx, y+1); a Source at (gx, y+1) facing -Y
    # emits into that cell. `main()` asserts something folded, and the
    # `_assert_abuts` check below fails loudly rather than quietly skipping.
    engine.clear_board()
    y = 2
    made = {"belt_fed": 0, "all_folded": 0, "mixed_source": 0}
    for _ in range(rows):
        if y + 4 >= board.height:
            break
        kind = rng.choice(("belt_fed", "belt_fed", "all_folded",
                           "mixed_source"))
        gx = 11
        if kind == "belt_fed":
            # Source -> run -> machine, with a Source abutted onto the
            # machine's +Y input. The mixed case: one input a constant, one
            # arriving on a belt at a belt's rate. This is the shape folding
            # exists for and the only one where it could disagree.
            m = rng.choice((Shear, Mac, Max))
            cells = m.cells_for(gx, y, cell.DIR_PX, False)
            if all(board.is_empty(cx, cy) for (cx, cy) in cells):
                board.spawn(m, gx, y, cell.DIR_PX)
                # The belt-fed input: the -X face of (gx, y), from (gx-1, y).
                if board.is_empty(0, y):
                    board.spawn(Source, 0, y, cell.DIR_PX)
                for x in range(2, gx):
                    if board.is_empty(x, y):
                        board.spawn(Belt, x, y, cell.DIR_PX)
                # The folded input: a Source above, emitting -Y.
                if all(board.is_empty(cx, cy) for (cx, cy)
                       in Source.cells_for(gx, y + 1, cell.DIR_NY, False)):
                    board.spawn(Source, gx, y + 1, cell.DIR_NY)
                for x in range(gx + 2, gx + 9):
                    if board.is_empty(x, y):
                        board.spawn(Belt, x, y, cell.DIR_PX)
                # The Sink origin is one past the run's last cell: a Sink at
                # (ox, oy) accepts on the -X face of (ox, oy), i.e. from
                # (ox-1, oy). Off by one and the run dangles, which this file
                # is specifically unable to notice -- see the mixed_source
                # branch below.
                if all(board.is_empty(cx, cy) for (cx, cy)
                       in Sink.cells_for(gx + 9, y, cell.DIR_PX, False)):
                    board.spawn(Sink, gx + 9, y, cell.DIR_PX)
                made["belt_fed"] += 1
        elif kind == "all_folded":
            # Every input a constant: the machine must still fire at full
            # rate with no belt on its input side at all.
            m = rng.choice((Floor, Shear))
            cells = m.cells_for(gx, y, cell.DIR_PX, False)
            if all(board.is_empty(cx, cy) for (cx, cy) in cells):
                board.spawn(m, gx, y, cell.DIR_PX)
                if all(board.is_empty(cx, cy) for (cx, cy)
                       in Source.cells_for(gx, y + 1, cell.DIR_NY, False)):
                    board.spawn(Source, gx, y + 1, cell.DIR_NY)
                # The -X input of (gx, y), fed from (gx-1, y): a Source at
                # (gx-2, y) facing +X emits from its far cell into it.
                if all(board.is_empty(cx, cy) for (cx, cy)
                       in Source.cells_for(gx - 2, y, cell.DIR_PX, False)):
                    board.spawn(Source, gx - 2, y, cell.DIR_PX)
                for x in range(gx + 2, gx + 11):
                    if board.is_empty(x, y):
                        board.spawn(Belt, x, y, cell.DIR_PX)
                if all(board.is_empty(cx, cy) for (cx, cy)
                       in Sink.cells_for(gx + 11, y, cell.DIR_PX, False)):
                    board.spawn(Sink, gx + 11, y, cell.DIR_PX)
                made["all_folded"] += 1
        else:
            # A Source feeding a belt *and* abutting a gate: must not fold.
            # The narrowing most likely to regress, so it is a whole row type
            # rather than an afterthought.
            m = Shear
            if all(board.is_empty(cx, cy)
                   for (cx, cy) in m.cells_for(gx, y, cell.DIR_PX, False)):
                board.spawn(m, gx, y, cell.DIR_PX)
                # One Source whose -Y output abuts the gate and whose +X
                # output lands on a belt.
                #
                # **That belt must end in a Sink.** A run that merely dangles
                # is empty in both timelines whatever happens upstream, so a
                # wrongly-folded Source -- which stops feeding its belt
                # entirely -- is invisible to a rate comparison. Measured:
                # with the belt dangling, the mutation "a belt no longer
                # blocks the fold" left five runs permanently empty and this
                # check still passed on every seed. Terminating in a Sink is
                # what puts those items in the destroy rate.
                if all(board.is_empty(cx, cy) for (cx, cy)
                       in Source.cells_for(gx, y + 1, cell.DIR_NY, False)):
                    board.spawn(Source, gx, y + 1, cell.DIR_NY)
                    for x in range(gx + 1, gx + 8):
                        if board.is_empty(x, y + 1):
                            board.spawn(Belt, x, y + 1, cell.DIR_PX)
                    if all(board.is_empty(cx, cy) for (cx, cy)
                           in Sink.cells_for(gx + 8, y + 1, cell.DIR_PX,
                                             False)):
                        board.spawn(Sink, gx + 8, y + 1, cell.DIR_PX)
                if board.is_empty(0, y):
                    board.spawn(Source, 0, y, cell.DIR_PX)
                for x in range(2, gx):
                    if board.is_empty(x, y):
                        board.spawn(Belt, x, y, cell.DIR_PX)
                made["mixed_source"] += 1
        y += 5

    # Give the Sources distinct non-zero values. Matching values would make a
    # swapped channel pair invisible, which is the same trap the suite's
    # integration tests document.
    v = 1
    for g in gates.gates():
        if isinstance(g, Source):
            g.emit_value = float(v if v % 3 else -v)
            v += 1

    engine.invalidate_segments()
    engine.sync_segments()
    return made


def run_one_tick():
    engine.clock.request_step(1)
    engine.clock.advance(0.0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--ticks", type=int, default=200)
    ap.add_argument("--warmup", type=int, default=40,
                    help="ticks run but not failed, covering start-up. 40 "
                         "because a belt-fed input on this fixture takes "
                         "~26 ticks to arrive and the rates differ until it "
                         "does; measured, not guessed -- at 5 the ramp "
                         "reports a divergence on four of seven seeds")
    args = ap.parse_args()
    rng = random.Random(args.seed)

    made = build(rng)

    # The exclusion set, derived **once** with folding on, and used for both
    # timelines. Deriving it per timeline would have the unfolded run exclude
    # nothing, so the two would be compared on different things.
    gates.set_folding_enabled(True)
    folded_rows = set(gates.folded_sources())
    folded_channels = {(row, ch) for (row, ch, _v) in gates._folds()[1]}
    n_gates = sum(1 for _ in gates.gates())

    print(f"seed {args.seed}, {args.ticks} ticks "
          f"({args.warmup} warmup), {n_gates} gates, "
          f"{len(engine.segments.segments)} segments")
    print(f"  rows built          : {made}")
    print(f"  folded Sources      : {len(folded_rows)}")
    print(f"  folded channels     : {len(folded_channels)}")

    if not folded_rows:
        print("\nNOTHING FOLDED -- this run proves nothing. Fix the fixture.")
        return 1

    folded_state = snapshot()
    gates.set_folding_enabled(False)
    unfolded_state = snapshot()

    problems = []
    warm_diffs = 0
    prev_a = prev_b = 0
    lead = None
    seen_a, seen_b = {}, {}
    lead_since = args.warmup
    leads = []
    for t in range(args.ticks):
        # --- the folding timeline ---
        # **The latch is set before the state is restored, not after.**
        # `set_folding_enabled()` writes entity rows -- it releases the folded
        # channels on the way down and re-asserts them on the way up -- so
        # calling it second lets it overwrite the timeline being restored.
        # Doing that to the *oracle* silently cleared the constants it was
        # meant to be running without, which made its gates starve and this
        # check report a divergence that was entirely its own doing.
        gates.set_folding_enabled(True)
        restore(folded_state)
        run_one_tick()
        folded_state = snapshot()
        a = observable(folded_state, folded_rows, folded_channels)

        # --- the oracle: the same tick with folding latched off ---
        gates.set_folding_enabled(False)
        restore(unfolded_state)
        run_one_tick()
        unfolded_state = snapshot()
        b = observable(unfolded_state, folded_rows, folded_channels)

        rate_a, rate_b = a[1] - prev_a, b[1] - prev_b
        prev_a, prev_b = a[1], b[1]
        diffs = compare(a, b, rate_a, rate_b)
        if t >= args.warmup:
            accumulate(seen_a, a)
            accumulate(seen_b, b)
            # The cumulative lead must *settle* and then stay put. A lead that
            # keeps moving means one timeline is permanently gaining or losing
            # items, which is the failure a rate comparison alone could miss
            # on a board whose rate is briefly zero in both runs.
            #
            # It is checked as "unchanged for the last `settle` ticks" rather
            # than "equal to its first value", because the lead legitimately
            # *shrinks* during start-up: a fully-folded gate begins producing
            # immediately while its unfolded twin waits for a belt, so the
            # unfolded run catches up over the first dozen ticks. Pinning the
            # first value reported a divergence on three of five seeds, all of
            # them this.
            delta = a[1] - b[1]
            if delta != lead:
                lead, lead_since = delta, t
            leads.append(delta)
        if diffs:
            if t < args.warmup:
                warm_diffs += 1
                continue
            problems.append((t, diffs))
            break

    # Did the lead settle, and stay settled for long enough to mean anything?
    # A lead still moving at the last tick is the real "one run is gaining
    # items" failure; a lead that settled only on the final tick proves
    # nothing, so a minimum stable stretch is required rather than just
    # "unchanged at the end".
    stable_for = args.ticks - lead_since
    min_stable = max(20, (args.ticks - args.warmup) // 4)
    if not problems and stable_for < min_stable:
        problems.append(
            (args.ticks,
             [f"the folded timeline's lead was still moving {stable_for} "
              f"ticks before the end (last changed at tick {lead_since}, "
              f"now {lead}); it must hold for at least {min_stable}"]))

    # The value-set comparison, which is what checks the arithmetic rather
    # than the rate. Only if the tick loop got all the way through -- on an
    # early break the sets cover different numbers of ticks and would differ
    # for that reason alone.
    if not problems:
        value_diffs = compare_seen(seen_a, seen_b)
        if value_diffs:
            problems.append((args.ticks, value_diffs))

    # Leave the engine folding again, so a later import is not surprised.
    gates.set_folding_enabled(True)

    print(f"  warmup ticks differing: {warm_diffs}/{args.warmup} "
          f"(expected, see the module docstring)")
    print(f"  gate rows value-checked: {len(seen_a)}")
    if problems:
        t, diffs = problems[0]
        print(f"\nDIVERGED at tick {t}:")
        for d in diffs[:12]:
            print(f"  {d}")
        if len(diffs) > 12:
            print(f"  ... and {len(diffs) - 12} more")
        return 1
    print(f"\nfolded and unfolded agree at every tick after warmup "
          f"({args.ticks - args.warmup} compared)")
    print(f"  settled lead        : {lead} items, stable for the last "
          f"{stable_for} ticks (the head start, not growing)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
