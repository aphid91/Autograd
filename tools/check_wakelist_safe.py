"""Differential check: sleeping must not change the simulation at all.

The tick skips work it believes cannot change anything. That is only sound if
a skipped unit really was a fixed point *and* something wakes it the moment it
stops being one. Both halves fail silently: a wrongly-slept run simply stops
moving, which on a board the size of a CPU looks like a logic bug in the
circuit rather than a scheduler bug in the engine.

So this runs the same board twice, tick for tick, with the sleep machinery on
and off, and compares the full state of every segment *and* every entity row
after every tick. Any divergence is reported with the tick it first appeared
on.

**Written to cover every sleeping layer, not just the belts.** `BeltSim` is
the only one that sleeps today; gate sleep is the next step, and the whole
point of a differential check is that it must already be watching when the
new mechanism lands rather than being extended alongside it (an optimisation
and its own safety net landing together is how the first version of this file
ended up vacuous). So:

  - the oracle disables sleeping through `SLEEPERS` below, one entry per
    layer, rather than naming `engine.belts` directly;
  - the snapshot already captured every entity row, which is where gate
    channels live, so gate sleep needs no new comparison -- a gate that
    wrongly slept shows up as a row that differs;
  - the transition count is per layer, so "this run did not exercise the wake
    edges" is answerable for each one separately. A combined count would let
    busy belts hide a gate layer that never woke once, which is exactly the
    failure this file exists to make visible.

Adding a layer means adding one `_Sleeper` entry. If that entry is missing the
oracle still sleeps in that layer, both timelines agree by construction, and
the check silently proves nothing -- so `main()` reports a transition count
per layer and warns when one is zero.

What this deliberately does not cover
-------------------------------------

Two wake edges are outside its reach, and both are named here rather than
left to be rediscovered as "the checker passed so it must be fine":

- **The wake on a segment rebuild** (`Engine.sync_segments()`). The board
  here never changes mid-run, so removing that `wake_all()` passes. Covering
  it needs an *edit* in the middle of both timelines, and a rebuild
  reassigns every segment id -- so the two snapshots would no longer line up
  by id and the whole comparison would need to be keyed on cells instead.
  That is a bigger change than it sounds and is not attempted;
  `tools/check_cache_coherence.py` is what exercises edits today.
- **The anchor clause of `is_at_rest()`**, which is invisible end to end for
  the reason the module docstring in core/belts.py gives: an empty run's
  unsaturated anchor re-saturates before any item position depends on it.
  `tools/probe_wakelist.py` checks the predicate against its *definition*
  rather than against behaviour, which is the only thing that catches it.

And one more, recorded in `build()`: the wake raised by `_fire_generator()`
is **unreachable by construction**, so removing it passes. That is an
argument, not an untested gap -- see the docstring there.

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
gates = engine.gates

MACHINES = (Shear, Route, Merge, Reciprocal, Max, Floor, Swap, Sink)


# --- the sleeping layers --------------------------------------------------
#
# One entry per subsystem that skips work it believes is inert. Each supplies
# three things, and no more, which is what keeps this file from having to know
# how a layer stores its own sleep set:
#
#   `asleep`   the set of currently-sleeping ids -- snapshotted and restored
#              with the rest of the timeline's state, and diffed tick over
#              tick to count wake edges.
#   `enable`   latch sleeping on or off. The oracle turns it *off*, which is
#              deliberately not the same as waking everything: waking lets the
#              coming tick re-sleep, so the oracle would accumulate a sleep
#              set and stop being one after a single tick. That is precisely
#              the bug that made the first version of this check vacuous.
#   `restore`  put a snapshotted sleep set back.
#
# A layer missing from this list is a layer the oracle also sleeps in, so both
# timelines agree by construction and the check proves nothing. `main()`
# therefore reports a transition count per layer and warns on zero rather than
# summing them -- a busy belt layer must not be able to vouch for a gate layer
# that never woke.
class _Sleeper:
    __slots__ = ("name", "_get", "_enable", "_restore")

    def __init__(self, name, get, enable, restore):
        self.name = name
        self._get = get
        self._enable = enable
        self._restore = restore

    def asleep(self):
        return set(self._get())

    def enable(self, on):
        self._enable(on)

    def restore(self, ids):
        self._restore(ids)


SLEEPERS = (
    _Sleeper(
        "belts",
        lambda: engine.belts._asleep,
        lambda on: engine.belts.set_sleep_enabled(on),
        lambda ids: setattr(engine.belts, "_asleep", set(ids)),
    ),
    _Sleeper(
        "gates",
        lambda: engine.gates._asleep,
        lambda on: engine.gates.set_sleep_enabled(on),
        lambda ids: setattr(engine.gates, "_asleep", set(ids)),
    ),
)


def _enlarge():
    if board.width >= BOARD:
        return
    board.width = board.height = BOARD
    board.grid = np.zeros((BOARD, BOARD, GRID_CHANNELS), dtype=np.int32)
    board.mark_all_dirty()


_enlarge()


# --- state capture --------------------------------------------------------
def snapshot():
    """Everything a tick can change, plus every layer's sleep set.

    The entity array is copied wholesale rather than reaching for named gate
    fields, which is what makes this cover gate sleep for free: a gate that
    wrongly slept leaves an input channel or a waiting zone holding what the
    oracle consumed, and that is a row that differs.
    """
    segs = {}
    for sid, s in engine.segments.segments.items():
        segs[sid] = (s.head, s.tail, s.anchor,
                     tuple(s.enter_tick) if s.enter_tick is not None else (),
                     tuple(s.payload) if s.payload is not None else ())
    return segs, board.entities.copy(), tuple(sl.asleep() for sl in SLEEPERS)


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
    for sleeper, ids in zip(SLEEPERS, asleep):
        sleeper.restore(ids)


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

    **And a couple where a generator's own run sleeps full and then drains**
    -- Source into a 3-cell run into a starved Shear. The two contrast rows
    above never reach that state (32 cells with a dangling far end never
    fill), so these are what take the belt layer from 8 wake transitions to
    54 and exercise the holding-items fixed point under a real consumer.

    They do **not**, however, make the `_wake_around()` in `_fire_generator()`
    load-bearing, and that is worth stating because removing it still passes
    this check. Measured: over 400 ticks of random feeding, 200 generator
    fires, and in **zero** of them was an adjacent segment asleep.

    It is unreachable by construction, not by luck of the fixture. A
    generator fires only when its output zone is clear; the zone is cleared by
    `took_from()`, which runs *inside* the belt tick of a segment that just
    accepted a push -- so that segment was awake on that tick, and is still
    awake on the next structure phase when the Source refills. For a run to
    be asleep as its Source fires, the zone would have had to drain while the
    run slept, and draining is something only an awake run does.

    So that call is **defensive rather than load-bearing**, in the same sense
    `took_from()` itself is (see core/gates.py): correct by construction given
    where the room check sits, and kept because the protocol is then right by
    argument rather than by coincidence. A splitter, or any producer that can
    be offered speculatively, would change that. This docstring is the record
    that it is known-untested rather than believed-covered -- the distinction
    the first version of this file got wrong by assuming the latter.
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

    # Two rows where a generator's own run sleeps: Source -> short run ->
    # starved gate. Short so it fills and reaches the holding-items fixed
    # point quickly; a Shear so it needs a second input it never gets on its
    # own, which is what keeps the run blocked until main() feeds it.
    for _ in range(2):
        if y + 3 >= board.height:
            break
        if board.is_empty(0, y):
            board.spawn(Source, 0, y, cell.DIR_PX)
        for x in range(2, 5):
            if board.is_empty(x, y):
                board.spawn(Belt, x, y, cell.DIR_PX)
        cells = Shear.cells_for(5, y, cell.DIR_PX, False)
        if all(board.is_empty(cx, cy) for (cx, cy) in cells):
            board.spawn(Shear, 5, y, cell.DIR_PX)
        y += 4

    # Two rows for **tier-two gate sleep**: a gate waiting on a lane whose
    # belt is empty and at rest. That is the whole population the second tier
    # exists for, and the fixture had *none* of it -- instrumenting
    # `_starved_on_sleeping_belts()` showed **zero** grants over 80 ticks, so
    # the check was green on a board that never reached the new code. The
    # partially-fed gates it did have were fed by `main()` by hand, so their
    # missing lane had no feeding segment at all and the predicate correctly
    # refused.
    #
    # The shape needs a belt that **stays empty**: a run whose own producer is
    # a starved gate rather than a Source. So this is
    #
    #     Shear A (starved, fed only by main()) -> run -> Shear B's x lane
    #     Source -> run -> Shear B's y lane
    #
    # B therefore sits with y fed and x missing, its x-feeding run empty and
    # at rest until main() happens to feed A -- which is the wake edge, and
    # the thing worth diffing.
    for _ in range(2):
        if y + 6 >= board.height:
            break
        # A, the starved producer. Nothing feeds it but main().
        if all(board.is_empty(cx, cy)
               for (cx, cy) in Shear.cells_for(2, y, cell.DIR_PX, False)):
            board.spawn(Shear, 2, y, cell.DIR_PX)
            # A's +X output at (3,y) -> a run into B's -X input.
            for x in range(4, 10):
                if board.is_empty(x, y):
                    board.spawn(Belt, x, y, cell.DIR_PX)
            if all(board.is_empty(cx, cy)
                   for (cx, cy) in Shear.cells_for(10, y, cell.DIR_PX,
                                                   False)):
                board.spawn(Shear, 10, y, cell.DIR_PX)
                # B's +Y input at (10,y) takes from (10,y+1): a Source there
                # facing -Y keeps that lane fed, so B is *partially* fed.
                if all(board.is_empty(cx, cy) for (cx, cy)
                       in Source.cells_for(10, y + 1, cell.DIR_NY, False)):
                    board.spawn(Source, 10, y + 1, cell.DIR_NY)
                # Somewhere for B's results to go, so it is not output-blocked
                # (a blocked gate is excluded from tier two and would make
                # this row vacuous for a second reason).
                for x in range(12, 18):
                    if board.is_empty(x, y):
                        board.spawn(Belt, x, y, cell.DIR_PX)
        y += 4

    # Two rows for the **RAM fixed point feeding a partially-fed gate**, which
    # is the one shape that can make tier-two sleep a *deadlock* rather than
    # merely a waste.
    #
    # A run that is full and at rest has its head sitting at the end waiting
    # to be taken -- by the very gate deciding whether to sleep. If that gate
    # sleeps, it never takes it, so the run stays at rest, so the gate stays
    # asleep: a mutual fixed point that no edge will ever break, and the
    # factory simply stops. `_starved_on_sleeping_belts()` refuses on
    # `seg.tail != seg.head` precisely to prevent it.
    #
    # Measuring showed that guard was **untested**: on the rows above, every
    # run that held items was also *awake and not at rest*, so the
    # `is_at_rest()` check refused first and removing the holds-items check
    # changed nothing. 22 refusals, all of them `(asleep=False, at_rest=False)`.
    # So the shape has to be built deliberately:
    #
    #     Source -> short run (fills, goes to rest) -> Shear's channel 1
    #     a folded Source abutted onto                 Shear's channel 0
    #
    # **Channel 0 has to be *fed*, not merely absent.** The predicate returns
    # on the *first* disqualifying channel, so a Shear with both lanes missing
    # refuses on channel 0 having no feeder and never looks at the full run at
    # all -- which is what the first attempt at this row did, leaving the
    # holds-items guard still untested. A folded Source abutting channel 0
    # holds it permanently fed, so channel 1 is the only missing lane and the
    # full, resting run is the thing the predicate actually has to judge.
    for _ in range(2):
        if y + 4 >= board.height:
            break
        if board.is_empty(0, y):
            board.spawn(Source, 0, y, cell.DIR_PX)
        for x in range(2, 5):
            if board.is_empty(x, y):
                board.spawn(Belt, x, y, cell.DIR_PX)
        if all(board.is_empty(cx, cy)
               for (cx, cy) in Shear.cells_for(5, y, cell.DIR_PX, False)):
            board.spawn(Shear, 5, y, cell.DIR_PX)
            # Channel 0 takes from (5, y+1): a Source there facing -Y abuts
            # it, folds, and holds the lane fed for ever.
            if all(board.is_empty(cx, cy) for (cx, cy)
                   in Source.cells_for(5, y + 1, cell.DIR_NY, False)):
                board.spawn(Source, 5, y + 1, cell.DIR_NY)
            # Somewhere for results to go, so the gate is not output-blocked
            # (which would exclude it from tier two for an unrelated reason
            # and make the row vacuous a second time).
            for x in range(7, 12):
                if board.is_empty(x, y):
                    board.spawn(Belt, x, y, cell.DIR_PX)
        y += 4

    # Two rows of gates that **abut with no belt between them**, which is the
    # `deliver_direct()` path and its own wake edge. The fixture had none of
    # this -- zero gates with a direct gate neighbour, zero direct deliveries
    # over 250 ticks -- so both gate-to-gate mutations (dropping the wake,
    # and making the adjacency index one-directional instead of symmetric)
    # passed against a board that never exercised them.
    #
    # A Source abutting a machine is the shape that matters, and it is also
    # the motif real designs use most: a constant wired straight into a gate
    # with no belt. The machine then abuts a Sink, so a delivered value has
    # somewhere to go and the chain is three gates long -- enough for a
    # missing wake to strand the middle one.
    for _ in range(2):
        if y + 3 >= board.height:
            break
        m = rng.choice((Shear, Max, Floor, Reciprocal))
        x = 1
        if board.is_empty(x, y) and board.is_empty(x + 1, y):
            board.spawn(Source, x, y, cell.DIR_PX)
        x += 2
        cells = m.cells_for(x, y, cell.DIR_PX, False)
        if all(board.is_empty(cx, cy) for (cx, cy) in cells):
            board.spawn(m, x, y, cell.DIR_PX)
        x += m.FOOTPRINT[0]
        cells = Sink.cells_for(x, y, cell.DIR_PX, False)
        if all(board.is_empty(cx, cy) for (cx, cy) in cells):
            board.spawn(Sink, x, y, cell.DIR_PX)
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


def lying_sleepers():
    """Tier-two sleepers whose own predicate no longer holds.

    [(row, channel, segment id)] for each sleeping, partially-fed gate with a
    missing lane whose feeding run is *not* an empty resting run -- i.e. a
    gate asleep on a claim that has stopped being true.

    **This is strictly stronger than the differential diff**, and that is why
    it exists. A gate sleeping on a stale claim is still woken by
    `accept_from()` when the item eventually reaches it, so the simulation is
    unaffected and both timelines agree -- the diff cannot see it. What is
    broken is that the scheduler is holding a conclusion its own premise no
    longer supports, which is a missed wake that happens not to matter yet.
    The next term added to the predicate is what would make it matter, and by
    then the cause would be a hundred ticks upstream of the symptom.

    It is also the only thing that makes the downstream-gate wake in
    `GateSim._wake_around()` testable: measured at 0 violations with that
    edge and 4 without, over 200 ticks, while the diff passes either way.

    Tier *one* is deliberately not checked here. Its claim is "I hold
    nothing", which the wake edges re-establish by construction, and a
    one-phase-stale sleep decision is normal there (see
    `mutual_sleep_instances()` on exactly that transient).
    """
    out = []
    gs = engine.gates
    belts = engine.belts
    feeders = gs._feeders()
    for row in sorted(gs._asleep):
        if not board.is_row_alive(row):
            continue
        view = board.view(row)
        n_in = view.input_count()
        if not n_in:
            continue
        held = [view.in_held(c) for c in range(n_in)]
        if not any(held):
            continue        # tier one: a different claim, checked elsewhere
        for ch in range(n_in):
            if held[ch]:
                continue
            seg = feeders.get((row, ch))
            if seg is None:
                # No belt feeds it, so tier two never granted this sleep --
                # it must have come from somewhere else. Not this check's
                # business.
                continue
            if seg.tail != seg.head or not belts.is_at_rest(seg, gs._tick):
                out.append((row, ch, seg.sid))
                break
    return out


def mutual_sleep_instances():
    """Gates asleep while a run that feeds them is holding items.

    The **mutual sleep** shape, checked as an invariant rather than left to
    the end-to-end diff, because it is the one failure mode where both
    layers' predicates are locally defensible and compose into a deadlock:

        the gate sleeps because its feeding run is "at rest"
        the run is at rest because the gate is not taking from it
        -> neither moves again, and the items are stranded for ever

    Nothing in the predicate forbids this shape directly -- `_gate_is_inert()`
    sleeps any gate holding nothing, deliberately, because the wake edges are
    what guarantee it comes back. So this is the invariant that checks those
    edges from the *outcome* side: if the conjunction below ever holds, some
    wake was missed, whatever the predicate thought.

    **Both layers must be asleep for this to be a deadlock**, and that
    qualifier is the whole subtlety. A sleeping gate whose loaded upstream run
    is *awake* is a transient, not a stall: the run is still ticked, it
    delivers, and `accept_from()` wakes the gate. That state really does
    occur, and routinely -- the gate sleeps during the structure phase, when
    the run was still empty, and the belt phase then pushes onto it, so the
    sleep decision is one phase stale by construction.

    Reporting the transients would make this fire on a correct build at tick
    0, which is exactly how a check gets disabled and then deleted. So the
    test is the conjunction, which is the condition under which nothing can
    ever move again.

    Deliberately a report rather than a bare assert: the end-to-end diff is
    what ultimately fails the run, and a deadlock does reach it eventually.
    This names it at the moment it forms, which is many ticks earlier and far
    easier to read than a ring-state difference.
    """
    out = []
    belts_asleep = engine.belts._asleep
    for row in sorted(engine.gates._asleep):
        _down, upstream = engine.gates._segments_of_gate(row)
        for seg in upstream:
            if seg.count and seg.sid in belts_asleep:
                out.append((row, seg.sid, seg.count))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--ticks", type=int, default=250)
    args = ap.parse_args()
    rng = random.Random(args.seed)

    build(rng)
    problems = []

    # The two timelines, each carrying its own full board state. They start
    # identical; `sleep_state` keeps whatever sleep set accumulates, and
    # `full_state` is never allowed to have one.
    sleep_state = snapshot()
    full_state = snapshot()
    #: Sleep -> wake transitions per layer. Kept apart rather than summed so a
    #: busy layer cannot vouch for one that never woke.
    wake_edges = {sl.name: 0 for sl in SLEEPERS}
    slept_by_layer = {sl.name: 0 for sl in SLEEPERS}

    #: Tier-two gate sleeps granted (a partially-fed gate whose missing lanes
    #: are fed by empty, resting runs). Counted and reported because the sleep
    #: set alone cannot distinguish "tier two never fired" from "tier two is
    #: safe" -- and it really did never fire: the fixture had no gate of that
    #: shape at all until the rows above were added, so this check was green
    #: on a board that never reached the new predicate. Reported for the same
    #: reason the per-layer wake counts are.
    tier2 = {"n": 0}
    _real_tier2 = gates._starved_on_sleeping_belts

    def _counting_tier2(row, n_in, held_in):
        granted = _real_tier2(row, n_in, held_in)
        if granted:
            tier2["n"] += 1
        return granted

    gates._starved_on_sleeping_belts = _counting_tier2

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
        for sl, ids in zip(SLEEPERS, sleep_state[2]):
            slept_by_layer[sl.name] += len(ids)

        # The stimulus for this tick, chosen once and replayed into both.
        plan = [r for r in feedable if rng.random() < 0.15]

        # (1) the timeline under test: its own state, its own sleep sets, and
        #     sleeping on. `enable(True)` is re-asserted every tick because
        #     the oracle below latches it off -- the two timelines share one
        #     engine, so each has to state its own scheduling.
        for sl in SLEEPERS:
            sl.enable(True)
        # After enable(), not before: turning sleeping on must not be allowed
        # to clear the sleep set this timeline has accumulated, and
        # set_sleep_enabled(False) clears it by design.
        restore(sleep_state)
        feed(plan)
        before = [sl.asleep() for sl in SLEEPERS]
        run_one_tick(t)
        for sl, was in zip(SLEEPERS, before):
            wake_edges[sl.name] += len(was - sl.asleep())
        # Checked here, while the engine still holds the timeline under
        # test's state -- after the oracle tick below it holds the oracle's,
        # and the oracle has no sleep set to inspect.
        # Does every tier-two sleeper still *satisfy* its own predicate? The
        # differential comparison cannot ask this: a gate sleeping on a stale
        # claim is still woken by `accept_from()` when the item finally
        # arrives, so behaviour is unchanged and the timelines agree. What is
        # wrong is that the sleep set and the predicate disagree, which is a
        # missed wake that happens not to matter *yet* -- and the next term
        # added to the predicate is what would make it matter.
        #
        # This is what makes the downstream-gate wake in `_wake_around()`
        # testable. Measured: 0 violations with that edge, 4 without it over
        # 200 ticks. The differential check alone passes either way.
        lying = lying_sleepers()
        if lying:
            problems.append((t, [
                f"STALE SLEEP: gate {row} is asleep but its missing lane "
                f"ch{ch} is fed by segment {sid}, which is not an empty "
                f"resting run" for (row, ch, sid) in lying[:4]]))
            sleep_state = snapshot()
            break

        stuck = mutual_sleep_instances()
        if stuck:
            problems.append((t, [
                f"MUTUAL SLEEP: gate {g} is asleep while segment {s} "
                f"feeding it holds {n} item(s) -- neither will move again"
                for (g, s, n) in stuck[:4]]))
            sleep_state = snapshot()
            break
        sleep_state = snapshot()

        # (2) the oracle: its own state, and sleeping *latched* off in every
        #     layer. Latched rather than woken: waking everything still lets
        #     this tick re-sleep, so the oracle would accumulate a sleep set
        #     and stop being an oracle after one tick. That exact mistake is
        #     what let four separate mutations through the first version.
        restore(full_state)
        for sl in SLEEPERS:
            sl.enable(False)
        feed(plan)
        run_one_tick(t)
        full_state = snapshot()
        # Belt and braces: the oracle's snapshot must carry no sleep set, so
        # even a layer that ignored `enable(False)` cannot skip next tick.
        full_state = (full_state[0], full_state[1],
                      tuple(set() for _ in SLEEPERS))

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
    # Per layer, because a run with no wake transitions proves nothing about
    # that layer's wake edges however many ticks it ran -- which is exactly
    # how the first version of this check passed against four mutations. Not
    # summed: a busy belt layer must not be able to vouch for a gate layer
    # that never woke once.
    vacuous = []
    for sl in SLEEPERS:
        n = wake_edges[sl.name]
        mean = slept_by_layer[sl.name] / max(args.ticks, 1)
        print(f"  {sl.name}: {n} sleep -> wake transitions, "
              f"mean {mean:.1f} asleep at tick start")
        if not n:
            vacuous.append(sl.name)
    # Tier-two gate sleep separately, because the gate layer's wake count
    # above cannot distinguish the two tiers: tier one alone produces
    # thousands of transitions, so a tier two that never fired once is
    # invisible in that number. It really was zero before the fixture gained
    # a gate of the right shape.
    print(f"  gates: {tier2['n']} tier-two sleeps granted "
          f"(partially fed, missing lanes on resting runs)")
    if not tier2["n"]:
        vacuous.append("gates tier two")
    for name in vacuous:
        print(f"  WARNING: nothing in '{name}' ever woke, so this run did "
              f"not exercise its wake edges at all")
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
