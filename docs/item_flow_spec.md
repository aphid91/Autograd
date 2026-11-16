# Spec: items seen through machines, and around corners (desktop)

**Status: to be replicated in the desktop app.** Built and tested in the web
port first (`web/flow.js`, `web/sim.js` `forecast()`,
`web/tests/flow.test.mjs`; see "How items draw" in `web/CLAUDE.md`). This is
the brief for bringing the same picture to `shaders/world.frag` /
`shaders/common.glsl`. The simulation does not change: this is rendering
only, and the web tick is checked identical to the desktop's by
`tools/check_web_sim.py`.

## What the player should see

1. **Items never vanish at a belt's end.** An item arriving at a machine
   travels on into it, waits inside it, and leaves through an output onto
   the next belt (or straight into the next machine), drawn the whole way.
2. **One path, belt to belt:** belt → the machine's **in-edge** (the port's
   cell boundary) → the input's **wait point** → the output's **out-edge** →
   the next belt. The disc shows the **input's value until the wait point
   and the output's value after it**, as if computed there.
3. **Waiting states sit at fixed places:**
   - an input held in a machine: at its **wait point**;
   - a result held in an output waiting zone: **on the out-edge**, a normal
     disc, half on the machine and half on the belt. This replaces the
     squashed disc `draw_waiting()` draws in the gap today;
   - a belt run blocked at a full input: its head **on the in-edge**.
4. **Items draw over the control overlay** (the faint belt through a
   machine) and over the glyph, like the rest of the cargo.
5. **Items round corners on the belt.** Today an item crossing into a
   corner cell is drawn along that cell's *facing* only, so it disappears on
   the incoming side and reappears on the far side, cutting straight across.
   The back half of every cell must be drawn along the direction the run
   *came in* from (the previous cell, or for a run's tail, the feeding
   port's face), the front half along the facing.

## The paths inside each machine

Per input port, which output ports its item can leave by (indices into
`INPUTS` / `OUTPUTS` in `core/entity.py`). Geometry only: which one a given
fire takes is decided by the opcodes (below).

| machine | input → output ports | shape |
|---|---|---|
| Shear | x 0→0; y 1→1 | x straight down cell 0; y straight across both cells |
| Mac | 0→0, 1→1, 2→2 | x, y down their cells; z across |
| Max | 0→0, 1→1 | straight down each cell |
| Reciprocal | 0→0 | straight through both cells |
| Floor | 0→0 | down into cell 0's centre, across to cell 1's centre, out its east face (an elbow) |
| Route | a 0→{0, 1}; b 1→2 | a: shared first half to cell 0's centre, then down cell 0, or across to cell 1's centre and down; b straight across |
| Merge | a 0→0; b 1→0; c 2→1 | a: elbow via cell 0's centre and cell 1's centre; b straight down cell 1; c straight across |
| Swap | x 0→0; y 1→{1, 2}; z 2→{2, 1} | x straight down; y and z straight to their own cell's centre, then either straight out, or **diagonally** to the other cell's port when they swap |
| Source | — | no inputs: each result **appears on the out-edge** |
| Sink | — | legacy, excluded: what enters vanishes at the edge |

Path construction, from the **placed** port tables (so no transform is
needed for rotation or mirroring): in-edge → centre of the input's cell →
centre of the output's cell → out-edge, de-duplicated; a diagonal link is
input cell centre → out-edge directly.

**Wait point:** the midpoint (by length) of the path when every link from
that input is a straight line; otherwise the centre of the input's own
cell. That keeps a turning or branching lane from committing to a direction
before it fires, and gives no two lanes of any machine the same wait point
(asserted).

**Which link a fire takes** comes from the output opcodes (`Entity.OPS`):
the output whose value *came from* this input on this fire. Per opcode, the
source inputs are: `IN`/`RECIP`/`FLOOR` → its operand; `ADD`/`MAX` → both;
`MAC` → all three; `SEL(c, a, b)` → `a` if `c > 0` else `b`; `GATED(c, a,
sense)` → `a` if it emits, else none. An input takes the first of its links
whose output produced a value and lists it as a source. The web test
asserts, for every machine and 200 value sets, that each consumed input
finds exactly one link and each emitted result is reached exactly once.

## Belts draw half a cell back

The closed form's position 0 is the tail cell's centre and its length `L`
is one cell past the head's centre (inside the next machine). Draw at
`pos - 0.5` instead: then 0 is the tail's **back edge**, where a push
enters, and `L` is the head's **front edge**, where a blocked head waits,
so both ends of a run meet the machines exactly at their ports. Buried
underground positions still draw nothing.

## Timing: animate the next tick, not the last

The state on screen is "after tick t", and `alpha` runs toward the next
step. To draw items *between* states (into a machine, through it, out of
it) the renderer needs to know what the next step will do. The web port
gets it by **running the real step with an event log and rolling it back**
(`Sim.forecast()`), cached per tick — so it cannot disagree with the tick.
Events: runs that pop their head (into which gate channel), gates that fire
(inputs consumed, outputs produced, the results), runs that take a push
(from which gate channel, and the pushed item's position at the start of
the following tick), and direct hand-offs. Over the tick, each moving item
covers its whole path at constant speed:

- **pop:** from the head's current drawn position, along the run to the
  in-edge, then to the wait point. On a saturated run the head is still a
  cell short at `alpha = 0` (the step advances the anchor before testing the
  pop), so this path is normally one belt cell plus the first half of the
  machine.
- **fire:** each consumed input from its wait point along its link to the
  out-edge, showing the result's value; then onto the run taking the push
  (as far as the closed form will have it next tick — usually one cell,
  less on a packed run), or on to the next machine's wait point for a direct
  hand-off. A result for a dangling output stops on the out-edge and is
  gone next tick.
- **held output** that will be pushed or handed off: the same, from the
  out-edge.
- **generator:** its result starts on the out-edge; if it will not move
  this tick (a packed run, or held) it is not drawn until the next tick,
  or it would sit on the item just leaving the edge.

The end of every path is where the next tick's state draws the same item;
the web test checks that across six saves and 80 ticks.

Consequences to expect, all faithful to the tick:

- **Items speed up through machines.** A hop costs one tick of latency, so
  belt-cell + first half, then second half + belt-cell, each take one tick.
- A packed run's newest item at position 0 and a result waiting in the zone
  behind it coincide on the out-edge for an instant.
- Route's a (right branch) and Merge's a run along the control lane's centre
  line and cross its discs mid-tick.

## Notes for the shader port

- The shader draws per pixel from the cell it is in, so items inside a
  machine need a per-machine pass: for each of the cell's footprint, the
  held inputs (at their wait points) and held outputs (on their out-edges)
  are row data the SSBO already carries; moving items need the forecast's
  events uploaded (a few ints per gate and per segment per tick), or the
  equivalent "previous state + events" if the desktop prefers to render one
  tick late.
- A disc spills across cell boundaries (out-edge discs always do), so the
  existing neighbourhood scan needs to include machine cells, not only belt
  cells of the same segment.
- The path tables are small and per (type, orientation): generate them from
  Python into `common.glsl` beside the other `@generated` tables, as the
  port tables are, rather than writing them by hand. The link table would
  then live on the entity classes (it is currently `LINKS` in
  `web/flow.js`), and the web's `types.json` export would carry it too.
- The corner rule needs only the cell's own facing and its incoming
  direction (`take_dir()` already resolves it in `common.glsl`), plus the
  feeding port's face at a run's tail.
