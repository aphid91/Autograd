# Getting sleep logic onto the GPU tick

> **Status: built.** See `core/sleep_device.py`, `shaders/sleep_*.comp` and
> "GPU sleep" in CLAUDE.md. It works, it is differentially checked
> (`tools/check_gpu_sleep_safe.py`), and on the real boards it dispatches
> 0.5–8% of the entities. It is also **2.2× slower** than not sleeping at
> these sizes, for a reason this brief predicted and a mechanism it got wrong
> — both recorded in "What was actually built" at the end.
>
> The brief is kept as written, because the two places it reasons incorrectly
> are instructive and the implementation notes refer back to them.

A design brief for whoever implements this. The tick already runs as three
compute dispatches (see "The tick on the GPU" in CLAUDE.md); what is missing
is any notion of skipping work, and the dormant-RAM case needs it badly — a
computer built from these parts is 99%+ idle, and the kernel currently visits
every gate and every segment every tick.

## The hard constraint, stated first

**A sleeping entity must not be a thread.** An early-return kernel —
`if (asleep) return;` — buys almost nothing: the warp still launches, the
scheduler still occupies it, and the divergence between sleeping and awake
lanes in the same warp costs more than the skipped arithmetic. On a 5060 the
whole tick is ~13 µs for ~500 gates and ~1200 segments, which is latency-
bound; adding a branch does not make it faster, it makes it wider.

So the only version worth building is one where **the dispatch size itself
shrinks**: one thread per *awake* entity, indices read from a compacted
buffer. `glDispatchCompute(groups_for(awake_count))` with
`awake_count << total`.

That is what "compacted index buffer" means and yes, it is what we want:

```glsl
layout(std430, binding = N) buffer Awake { int awake[]; };
uniform int awake_count;

void main() {
    uint i = gl_GlobalInvocationID.x;
    if (i >= uint(awake_count)) return;   // the only bounds check
    int slot = awake[i];                  // indirection, then normal work
    ...
}
```

The `awake[i]` read is a coalesced sequential load and the subsequent
accesses become a gather. `tools/probe_batch_prefilter.py` already measured
scattered gathers at **0.5% of the tick** on the CPU side, so the
indirection is not the thing to worry about.

## Why the existing CPU sleep logic is the wrong starting point

It is correct — `tools/check_wakelist_safe.py` agrees at every tick with both
tiers exercised — and it is **switched off**, because it measured as a net
loss on every real board. Read `GateSim._sleep_enabled`'s comment for the
numbers. The reason matters for this work:

> The sleep set is big and stable in *size* but never still: on cpu.npz 203
> gates are asleep yet 6 change state every tick, with only 1 tick in 100
> unchanged. So the awake slice cannot be memoised — measured at **0 cache
> hits in 150 ticks**.

A set that churns every tick cannot be maintained incrementally for profit.
That is a statement about the *workload*, not the implementation, and it
applies on the GPU too. **Do not port the dynamic sleep set.** It would mean
rebuilding the compacted buffer every tick, which is a scan — and a scan over
500 gates to avoid 500 cheap threads is a straight loss.

What the GPU changes is only that the rebuild could itself be parallel. If
you want to try it anyway, the honest experiment is: parallel predicate pass
→ prefix sum → compaction → dispatch, all on device, measured against the
current unconditional dispatch. Expect it to lose at these sizes and to start
winning somewhere past a few thousand gates. Measure before building more.

## The mechanism that should win: a static sleep graph

This is the one the dormant-RAM case actually wants, and it is **not** a port
of the existing logic. The insight is that a RAM bank's quietness is
*structural*, not momentary:

- a run with no producer and no consumer can never change;
- a gate whose every input lane is fed only by such runs can never fire;
- a region enclosed by those is inert until an **edit** or until material
  crosses its boundary.

That is computable **per edit**, costs nothing per tick, and is sound because
the conditions are properties of the graph rather than of this tick's state.
Concretely:

1. Per edit, after `sync_segments()`, walk the connectivity and mark every
   entity in a provably-inert region. The existing `seg.src`/`seg.dst`
   resolution and the direct edge list (`GateSim._direct_edges()`) already
   give you the graph — no new derivation needed.
2. Compact the *awake* slots into an index buffer once, at the same time.
   Upload it with the rest of the per-edit geometry in
   `GateDevice.rebuild()`.
3. Dispatch over that buffer. The set changes only on an edit, so there is no
   per-tick bookkeeping at all — which is exactly what the dynamic version
   could not achieve.
4. The boundary is the subtle part: a region is inert only while nothing
   enters it. The entry points are the gates and runs on its edge, so those
   stay awake and a value arriving at one must wake the region. Waking can be
   coarse (mark the whole region awake until the next edit) — a RAM bank that
   is being read is a bank that should be ticking.

**Soundness burden.** A missed wake fails silently and cumulatively — it does
not corrupt anything on the tick it happens, it just fails to move one entity,
and the error compounds over the next hundred ticks. On a CPU-shaped board
that reads as a logic bug in the circuit rather than a scheduler bug in the
engine. So:

- Generalise `tools/check_wakelist_safe.py` to this layer **before** building
  it. It already has a `SLEEPERS` registry for exactly this, with per-layer
  transition counts so a vacuous run is visible rather than silent. Its
  history is the cautionary tale: the first version was vacuous in two
  independent ways at once and four separate mutations passed it.
- `set_sleep_enabled(False)` must both latch sleeping off *and* clear the
  set. Waking alone leaves the coming tick free to re-sleep, so an oracle
  built on it stops being an oracle after one tick.
- Report a **grant count**. Removing a sleep is always safe, so a
  differential diff passes with the predicate gutted — only a count
  distinguishes a working clause from an absent one.

## What to reuse, and what to leave alone

| reuse | why |
|---|---|
| `GateState` slot assignment | the compacted buffer is a list of slots |
| `GateDevice.rebuild()` | per-edit upload point already exists |
| `_direct_edges()`, `seg.src`/`seg.dst` | the graph, already derived per edit |
| `tools/check_wakelist_safe.py` | generalised for multiple layers already |

| leave alone | why |
|---|---|
| `GateSim._asleep` and both tiers | dynamic, churns per tick, measured loss |
| `_candidate_rows()` prefilter | exists to avoid per-gate *Python* |
| `BeltSim.is_at_rest()` | a per-tick predicate; the static graph replaces it |

## One trap specific to this codebase

Anything keyed on **slot** goes stale when `GateState.build()` reassigns
slots, which it does on any gate-set change. The geometry block, fold list
and direct edge list all learned this the hard way — a dispatch sized by the
live mirror while the packed copy described a different set ran gates with
another machine's type and channel counts. `_device_stale` and
`GateDevice.gate_count()` exist because of it. **A compacted awake buffer is
slot-keyed and will need the same treatment**: pack it in `rebuild()`, size
the dispatch from what was packed, and never from the live mirror.

## Measuring it

The in-app profiler **cannot see GPU time** — it reports ~0.0008 ms for a
tick that truly costs ~13 µs, because it times the Python that queues the
dispatch, not the dispatch. Any claim about a sleep mechanism's value needs a
`ctx.finish()`-bracketed measurement:

```python
harness.ctx.finish(); t0 = time.perf_counter()
for i in range(N):
    gates.device.run(1); belt.run(tick + i, 1)
harness.ctx.finish()
us_per_tick = (time.perf_counter() - t0) / N * 1e6
```

Current baseline on this hardware, per tick, 20000 ticks, no readback:

| board | full | fire | direct | belt |
|---|---|---|---|---|
| cpu.npz | 13.50 µs | 6.27 | 2.86 | 3.32 |
| Ram2.npz | 13.32 µs | 5.91 | 2.86 | 3.21 |

Note the three kernels sum to roughly the whole tick, and that **`direct` and
`belt` barely differ between a 493-gate board and a 732-gate one** — this is
dispatch latency, not work. A sleep mechanism that halves the thread count
will therefore show *almost no gain at these sizes*. It is worth building for
the boards you intend to build, not the ones that exist; validate on a
synthetic board an order of magnitude larger before concluding anything.

---

## What was actually built, and where this brief was wrong

### The mechanism: dynamic, not static

This brief recommends a **static per-edit proof** and says plainly "do not
port the dynamic sleep set". What was built is dynamic — a push-based mailbox
plus a per-tick compaction — and the brief's own argument is what rules the
static version out once you state the target case precisely:

> a RAM bank's quietness is *structural*, not momentary

It is not. A RAM bank is wired to the CPU that reads it, so no graph-local
argument will ever call it inert; it is inert because of its *state*, for
thousands of ticks between accesses. The static proof covers only regions
nothing can **ever** reach, which on a working computer is close to nothing.

The brief's evidence against dynamic was a real measurement (203 gates
asleep, 6 changing state per tick, 0 cache hits in 150 ticks) and the
inference from it does not hold. That churn is the *active trace* — a roughly
constant handful of entities whatever the board size — and the CPU's cost was
`np.isin` being O(table), an artefact of keeping a Python set over a numpy
table rather than a property of the workload.

What makes dynamic viable here is a property of the **tick**, not of the
scheduler: material moves exactly one hop per tick, so the awake set at `t+1`
is contained in the awake set at `t` plus its one-hop neighbourhood. The
compaction is therefore dispatched over the *frontier*, which is O(awake) and
independent of board size. The brief assumed a rebuild had to be a scan; it
does not.

### The hard constraint was right

"A sleeping entity must not be a thread" is correct and shaped the whole
implementation: the dispatch size shrinks, via `glDispatchComputeIndirect`
reading a count the compaction wrote, so the host never learns it. The
sketched kernel preamble is almost exactly what `belt_tick.comp` and
`gate_fire.comp` now do.

### The soundness burden was right, and earned its keep

Every warning in that section proved justified. The implementation found
**four** genuine missed wakes, each silent and cumulative:

| missed wake | symptom |
|---|---|
| the fold pass writes an input lane and posted nothing | a folded gate slept and was never re-admitted |
| a blocked gate holding an undelivered result slept | the gate and its consumer both correctly concluded "nothing to do" — mutual sleep by a new route |
| `offered_nothing` read as "producer is idle" | a one-tick blockage read as permanent; fixed by requiring the zone to be *observably empty*, i.e. reading data rather than predicting |
| a **host** write (the Status window's emit box) posts no mailbox entry | a fed gate stayed asleep for ever |

And the warning about vacuity was the most valuable part. The first
differential check flipped the sleep switch per tick, and `set_gpu_sleep(True)`
calls `wake_all()` — so the sleeping timeline restarted from "everything
awake" every tick and **three genuinely unsafe mutations passed on all four
real boards**. That is this layer's own version of the vacuity the CPU tool
was written twice to escape.

`tools/check_gpu_sleep_safe.py` is the result, and it needed *two* checks
rather than one: conservation (which catches a stranded item) and lying
sleepers (an entity asleep whose own premise is already false, which the
conservation diff cannot see because some other wake usually rescues it a
tick later). Mutation-tested, with the known-untested cases named rather than
assumed covered.

### Measuring it: the brief was right, and understated it

"A sleep mechanism that halves the thread count will show almost no gain at
these sizes" — correct, and the reality is worse. Per tick, `ctx.finish()`-
bracketed over 4000 ticks, no readback:

| board | segs | gates | sleep off | sleep on | awake |
|---|---|---|---|---|---|
| cpu.npz | 539 | 494 | **13.9 µs** | 30.6 µs | 223 segs, 371 gates |
| Ram2.npz | 1269 | 732 | **13.5 µs** | 31.0 µs | 313 segs, 264 gates |
| noram.npz | 751 | 617 | **14.5 µs** | 30.9 µs | 394 segs, 509 gates |
| ram.npz | 337 | 197 | **13.4 µs** | 29.2 µs | 43 segs, 95 gates |

**The cause is dispatch latency, and it is measurable in isolation:**

| | cost |
|---|---|
| one 1-thread dispatch, no barrier | 1.06 µs |
| one 1-thread dispatch + barrier | 1.99 µs |
| four dispatches, four barriers | 7.79 µs |
| four dispatches, one barrier | 4.26 µs |

So a dispatch costs ~1 µs and a barrier another ~1 µs **regardless of the
work done**, and the sleep layer adds three dispatches and two barriers per
tick. Dropping the two barriers that are provably unnecessary (the two
frontier passes share only atomics, which are coherent without one) took it
from 35 µs to 30.6 µs.

**The decisive measurement is that the no-sleep column is flat.** On a
synthetic dormant-memory board the unslept tick cost 8.2–8.9 µs at *every*
size from 10 to 255 segments. The GPU is nowhere near work-bound at these
scales, so there is no work for sleeping to save — it is pure added latency.
The crossover needs a board where the tick is genuinely work-bound, which the
fixed 512×512 grid cannot currently hold (hence "a growable board" under
Deferred).

### What to do with it

The switch is in the panel, off-state being a **full bypass** rather than a
wake-everything, so the comparison stays honest. The mechanism is kept
because every hard part works — the frontier argument, the mailbox, the
indirect dispatch, the four wake edges, the overlay, and the differential
check — and because the thing it is waiting for is a bigger board, which is
a known, separate piece of work.

If it is picked up again, the first lever is **fewer dispatches**, not better
predicates: fuse `reset` and `groups` into the tick's existing kernels where
the ordering allows, or move the whole tick to one persistent kernel with
in-kernel sync. At ~2 µs per dispatch-and-barrier against a 13 µs tick, that
is where the budget is.
