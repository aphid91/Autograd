"""Tick: the simulation clock, and the phase order the whole sim obeys.

This module owns *time*. Nothing else decides when a tick happens, how far
through one we are, or what order the phases run in. `Engine` owns board
state; `Clock` owns when that state is allowed to change.

It lives apart from engine.py deliberately: everything tick-dependent --
the counter, the fixed-step accumulator, the render-time fraction, and the
phase sequence -- is one concern, and engine.py is already carrying the
placement and erase API.


The fixed timestep
------------------

Simulation runs at a fixed rate (`TICKS_PER_SEC`) regardless of framerate.
`advance(dt)` accumulates real time and fires whole ticks out of it, so the
sim is frame-rate independent and reproducible: the same tick sequence
yields the same board on a 30Hz machine and a 300Hz one.

`alpha` is the leftover -- how far into the *next* tick we are, 0..1. The
renderer uses it to interpolate item positions so a belt reads as a smooth
march rather than items snapping between slots. This is the one piece of
simulation state the renderer needs that is not in the board arrays, and it
reaches the shader as a uniform.

**Accumulated time is capped** (`MAX_CATCHUP_TICKS`). Without that, a long
stall -- dragging the window, a shader recompile -- banks hundreds of ticks
that then all fire in one frame, which both hitches visibly and makes the
sim appear to teleport. Dropping the backlog is the right trade: the sim
falls behind wall-clock rather than the frame budget blowing up. This is the
same reasoning as app.py clamping dt, one layer down.


The two-phase tick
------------------

A tick is two phases, in this order, and the order is load-bearing:

  1. **structure phase** -- every gate that can fire, fires. Reads input
     ports and waiting zones; writes waiting zones and clears inputs.
     Touches no belt state at all.
  2. **belt phase** -- waiting zones drain onto belts (or straight into a
     directly-connected gate's input port), belts advance one step, and
     belt heads load into gate input ports.

Why two phases rather than one: a gate's decision to fire depends on whether
its output is backed up. If gates and belts updated together, that answer
would depend on whether the downstream belt had already advanced this tick --
making fire/no-fire depend on execution order, which is nondeterministic the
moment this moves to a compute dispatch. Splitting the tick means each phase
reads state that is *frozen* for the whole phase and writes state the other
phase reads. That is the standard graph-cellular-automaton discipline, and it
is what makes the GPU port mechanical.

Why structure *before* belt, specifically: it is what lets a well-fed gate
fire on every single tick with no bubble. Trace a saturated gate G with one
output port feeding belt OUT:

    tick N    structure:  waiting empty -> FIRE -> waiting := item
              belt:       waiting drains onto OUT -> waiting empty
    tick N+1  structure:  waiting empty -> FIRE -> waiting := item
              belt:       waiting drains onto OUT -> waiting empty

G fires every tick, because the structure phase gets first look at a zone the
belt phase emptied one phase earlier. Reverse the two and the gate would see
a full zone every other tick and fire at half rate. The waiting zone is what
buys this: it decouples "the gate produced an item" from "the belt accepted
it", so the two phases never need to see each other's intermediate state.

Two consequences of this design worth knowing:

- **Bubble-free output needs spacing == speed.** A belt accepts a new item
  only once the previous one has moved a full `d` from the entrance, i.e.
  every `d/v` ticks. So a gate can only *sustain* one item per tick if
  `d == v` -- items nose-to-tail, one cell per tick. With `d > v` the belt
  throttles the gate, which is correct but means "fires every tick" is
  specifically the maximum-density configuration.
- **A stall costs exactly one tick to recover.** While blocked, a gate's
  input port stays fed, so the belt feeding it cannot load and backs up.
  When the blockage clears the gate fires, the input empties, and the belt
  phase refills it the same tick -- full rate from the next tick on. The one
  lost tick at the boundary is unavoidable without letting the structure
  phase read belt state, which would reintroduce the race.

There is also a one-tick latency *per hop*: an item loaded into a gate's
input during tick N's belt phase is first seen by the structure phase of
tick N+1. A chain of k gates therefore has k ticks of pipeline latency. That
is deliberate -- it is what makes each phase's reads order-independent -- and
it applies equally to a belt hop and a direct gate-to-gate connection, so the
two are consistent.


Phases are registered, not hardcoded
------------------------------------

`Clock` does not know what a gate or a belt is. Subsystems register a
callable per phase and the clock calls them in phase order. That keeps the
clock testable with no board at all, and means the eventual GPU port
replaces the *bodies* of those callbacks with compute dispatches while the
sequencing here stays exactly as it is. The barrier between phases is
implicit now (one returns before the next is called) and becomes a real
`glMemoryBarrier` later.
"""

#: Simulation rate. One tick is one belt step at d == v.
TICKS_PER_SEC = 10.0

#: Most ticks one `advance()` may fire. Caps the catch-up after a stall so a
#: hitch cannot bank a huge backlog and then spend it all in one frame.
MAX_CATCHUP_TICKS = 4

# Phase identifiers, in execution order. The values are the order.
PHASE_STRUCTURE = 0
PHASE_BELT = 1
PHASE_COUNT = 2

PHASE_NAMES = {
    PHASE_STRUCTURE: "structure",
    PHASE_BELT: "belt",
}


class Clock:
    """The simulation clock: tick counter, accumulator, and phase runner.

    Owns no board state. Subsystems register phase callbacks; the clock
    decides when they run and in what order.
    """

    def __init__(self, ticks_per_sec=TICKS_PER_SEC):
        self.ticks_per_sec = float(ticks_per_sec)
        #: Ticks elapsed since the world began. Monotonic; the closed-form
        #: belt position is a function of this, so it must never go backwards.
        self.tick = 0
        #: Real seconds banked toward the next tick.
        self._accum = 0.0
        #: True while the sim is halted. Rendering continues; `alpha` freezes.
        self.paused = False
        #: Ticks requested by the single-step control, run even while paused.
        self._pending_steps = 0
        #: Phase callbacks, in phase order. Each is a list, so several
        #: subsystems can take part in one phase.
        self._phases = [[] for _ in range(PHASE_COUNT)]
        #: Ticks run by the most recent advance(), for the UI.
        self.last_ticks = 0
        #: Ticks dropped to the catch-up cap, cumulative. A non-zero value
        #: means the sim is not keeping up with wall-clock.
        self.dropped_ticks = 0

    # --- configuration ---------------------------------------------------
    @property
    def seconds_per_tick(self):
        return 1.0 / self.ticks_per_sec

    def set_rate(self, ticks_per_sec):
        """Change the tick rate, keeping the phase of the current tick.

        The accumulator is rescaled rather than reset so changing speed does
        not jump the sim forward or stutter it.
        """
        ticks_per_sec = max(0.1, float(ticks_per_sec))
        frac = self.alpha
        self.ticks_per_sec = ticks_per_sec
        self._accum = frac * self.seconds_per_tick

    def on_phase(self, phase, fn):
        """Register `fn()` to run during `phase` of every tick."""
        if not 0 <= phase < PHASE_COUNT:
            raise ValueError(f"no such phase: {phase}")
        self._phases[phase].append(fn)

    # --- render-time interpolation ---------------------------------------
    @property
    def alpha(self):
        """How far into the next tick we are, 0..1.

        The renderer interpolates item positions by this, so motion is smooth
        at any framerate. Frozen while paused, so a paused board is stationary
        rather than creeping toward the next tick.
        """
        if self.paused:
            return 0.0
        return min(max(self._accum / self.seconds_per_tick, 0.0), 1.0)

    @property
    def now(self):
        """Fractional tick time, `tick + alpha`. What the shader wants."""
        return self.tick + self.alpha

    # --- controls --------------------------------------------------------
    def pause(self):
        self.paused = True

    def resume(self):
        self.paused = False

    def toggle_pause(self):
        self.paused = not self.paused
        return self.paused

    def request_step(self, n=1):
        """Queue `n` ticks to run on the next advance(), even while paused.

        The debugging primitive: pause, then step one tick at a time and watch
        the board change. Queued rather than run immediately so a tick always
        happens at the same point in the frame, with the same ordering
        guarantees as a normal one.
        """
        self._pending_steps += max(0, int(n))

    # --- the clock itself -------------------------------------------------
    def advance(self, dt):
        """Bank `dt` seconds and run whatever whole ticks fall out.

        Returns the number of ticks run. Explicit single-steps are honoured
        first and are never dropped by the catch-up cap -- a step the player
        asked for must always happen.
        """
        ran = 0

        while self._pending_steps > 0:
            self._pending_steps -= 1
            self._run_tick()
            ran += 1

        if not self.paused:
            self._accum += max(0.0, dt)
            spt = self.seconds_per_tick
            due = int(self._accum // spt)
            if due > MAX_CATCHUP_TICKS:
                # Give up on the backlog entirely: zero the accumulator rather
                # than charging it per tick. Charging only for the ticks that
                # *ran* would leave the dropped time banked for the next call
                # to fire -- the very backlog the cap exists to prevent -- and
                # subtracting all of it can leave a near-full period behind
                # from float residue, which would park alpha at ~1.0 and have
                # the renderer interpolating a whole tick past the last one
                # that actually ran.
                self.dropped_ticks += due - MAX_CATCHUP_TICKS
                due = MAX_CATCHUP_TICKS
                self._accum = 0.0
            else:
                self._accum -= due * spt
            for _ in range(due):
                self._run_tick()
                ran += 1

        self.last_ticks = ran
        return ran

    def _run_tick(self):
        """One tick: every phase in order, then the counter.

        The counter advances *after* the phases, so a phase callback sees
        `clock.tick` as the tick it is currently computing -- which is what
        the closed-form belt position expects when it stamps an item's
        entry tick.
        """
        for phase in range(PHASE_COUNT):
            for fn in self._phases[phase]:
                fn()
        self.tick += 1

    def reset(self):
        """Back to tick zero, keeping the rate and the registered phases.

        Used when the board is replaced wholesale -- a load or a clear. The
        tick counter is part of the simulation state the closed form reads,
        so a new board starts a new timeline.
        """
        self.tick = 0
        self._accum = 0.0
        self._pending_steps = 0
        self.last_ticks = 0
        self.dropped_ticks = 0

    def __repr__(self):
        state = "paused" if self.paused else f"{self.ticks_per_sec:g}/s"
        return f"<Clock tick={self.tick} alpha={self.alpha:.2f} {state}>"


def _self_check():
    """The clock's timing contract. Cheap; runs at import."""
    c = Clock(ticks_per_sec=10.0)
    assert c.tick == 0 and c.alpha == 0.0

    # A tick fires once a whole period has accumulated, not before.
    assert c.advance(0.05) == 0, "half a period is not a tick"
    assert c.tick == 0
    assert abs(c.alpha - 0.5) < 1e-9, c.alpha
    assert c.advance(0.05) == 1, "the period completing fires exactly one tick"
    assert c.tick == 1
    assert c.alpha < 1e-9

    # Several periods in one frame fire several ticks.
    c = Clock(ticks_per_sec=10.0)
    assert c.advance(0.251) == 2, "two whole periods"
    assert c.tick == 2
    assert abs(c.alpha - 0.51) < 1e-6, c.alpha

    # The catch-up cap drops the backlog instead of spending it.
    c = Clock(ticks_per_sec=10.0)
    assert c.advance(100.0) == MAX_CATCHUP_TICKS
    assert c.tick == MAX_CATCHUP_TICKS
    # ~1000 periods fit in 100s; don't pin the exact count, since 100.0 // 0.1
    # is at the mercy of float division. What matters is that the overwhelming
    # majority were dropped rather than run.
    assert c.dropped_ticks > 900, c.dropped_ticks
    # The whole backlog must leave the accumulator, including the part that
    # *ran*. Charging only for dropped ticks leaves the capped ones banked and
    # the next call fires them -- defeating the cap one frame later.
    assert c.advance(0.0) == 0, "the backlog must not survive the cap"
    assert c.alpha < 1e-9, c.alpha
    # Several idle frames in a row stay quiet, rather than slowly paying out.
    for _ in range(5):
        assert c.advance(0.0) == 0
    assert c.tick == MAX_CATCHUP_TICKS

    # A frame just over the cap runs the cap and drops the excess. Nudged up
    # by a fraction of a period so float division cannot land just short of a
    # whole tick and make the count ambiguous.
    c = Clock(ticks_per_sec=10.0)
    c.advance(0.1 * (MAX_CATCHUP_TICKS + 2) + 0.01)
    assert c.tick == MAX_CATCHUP_TICKS
    assert c.dropped_ticks == 2, c.dropped_ticks

    # A frame within the cap drops nothing and runs everything due.
    c = Clock(ticks_per_sec=10.0)
    assert c.advance(0.1 * (MAX_CATCHUP_TICKS - 1) + 0.01) \
        == MAX_CATCHUP_TICKS - 1
    assert c.dropped_ticks == 0

    # Phases run in order, every tick, exactly once each -- and in *phase*
    # order, not the order they were registered in.
    log = []
    c = Clock(ticks_per_sec=10.0)
    c.on_phase(PHASE_BELT, lambda: log.append("belt"))
    c.on_phase(PHASE_STRUCTURE, lambda: log.append("structure"))
    c.request_step(2)
    c.advance(0.0)
    assert log == ["structure", "belt", "structure", "belt"], log

    # Several callbacks in one phase all run, in registration order.
    log = []
    c = Clock(ticks_per_sec=10.0)
    c.on_phase(PHASE_STRUCTURE, lambda: log.append("a"))
    c.on_phase(PHASE_STRUCTURE, lambda: log.append("b"))
    c.advance(0.1)
    assert log == ["a", "b"], log

    # A phase callback sees the tick it is computing, not the one after.
    # Stepped explicitly rather than via one large advance(): float residue in
    # `accum // period` makes the exact tick count of a multi-period frame
    # unreliable, and it is the numbering being tested here, not the division.
    seen = []
    c = Clock(ticks_per_sec=10.0)
    c.on_phase(PHASE_STRUCTURE, lambda: seen.append(c.tick))
    c.request_step(3)
    c.advance(0.0)
    assert seen == [0, 1, 2], seen

    # Pausing stops ticks and freezes alpha -- a paused board must be
    # completely stationary, not creeping toward the next tick.
    c = Clock(ticks_per_sec=10.0)
    c.pause()
    assert c.advance(1.0) == 0
    assert c.tick == 0
    assert c.alpha == 0.0, "a paused clock must not interpolate"
    assert c.toggle_pause() is False
    assert c.advance(0.1) == 1

    # A single step runs exactly one tick even while paused...
    c = Clock(ticks_per_sec=10.0)
    c.pause()
    c.request_step()
    assert c.advance(0.0) == 1
    assert c.tick == 1
    assert c.advance(0.0) == 0, "a step is consumed once"
    # ...and several queued steps all run, none dropped by the cap.
    c.request_step(MAX_CATCHUP_TICKS + 3)
    assert c.advance(0.0) == MAX_CATCHUP_TICKS + 3

    # Changing rate keeps the sub-tick phase rather than jumping the sim.
    c = Clock(ticks_per_sec=10.0)
    c.advance(0.05)
    assert abs(c.alpha - 0.5) < 1e-9
    c.set_rate(20.0)
    assert abs(c.alpha - 0.5) < 1e-9, c.alpha
    assert c.tick == 0, "a rate change must not fire a tick"

    # `now` is the fractional tick time the renderer interpolates against,
    # and must be monotonic across a tick boundary.
    c = Clock(ticks_per_sec=10.0)
    c.advance(0.05)
    before = c.now
    c.advance(0.06)
    assert c.now > before, (before, c.now)

    # reset() restarts the timeline but keeps the wiring.
    log = []
    c = Clock(ticks_per_sec=10.0)
    c.on_phase(PHASE_BELT, lambda: log.append(1))
    c.advance(0.2)
    c.reset()
    assert c.tick == 0 and c.alpha == 0.0
    c.advance(0.1)
    assert len(log) == 3, "phases survive a reset"


_self_check()
