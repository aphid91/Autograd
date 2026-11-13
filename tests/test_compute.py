"""The structure phase on the GPU, against the CPU implementation.

These are the port's real tests. The risk in moving a tick to a compute
shader is not that it crashes -- it is that it computes something plausible
and wrong, with no visual artefact and no assertion anywhere else in the
suite that can see a gate's channels. So the CPU path is kept as the oracle
and the kernel is diffed against it, which is the same relationship
`naive_tick()` has to the closed form and the reason docs/belt_report.md
says to keep the CPU implementation after the port.

Every test here runs **both** implementations from identical state and
compares, rather than asserting an expected value. Hardcoding what a Shear
should produce would test the fixture; comparing against the reference tests
the kernel.
"""

import numpy as np

from core import cell
from core.entity import (
    MAX_CHANNELS, Entity, Floor, Mac, Max, Merge, Reciprocal, Route, Shear,
    Sink, Source, Swap)
from core.gate_device import (
    FLAG_DRAIN_SHIFT, FLAG_FIRE_ANY, FLAG_HOST_DECIDES, FLAG_SELECT,
    GEOM_STRIDE, compare_against_cpu, gate_flags, gate_type_ids, geometry_for)
from render import compute, shader_loader

from tests import harness
from tests.harness import ok

engine = harness.engine
board = harness.board


def _with_device():
    """Build the gate device on the shared context. Returns the GateSim."""
    gates = engine.gates
    if gates.device is None:
        gates.create_device_resources(harness.ctx, engine.belt_device)
    else:
        gates.sync_device()
    return gates


def _run(n=1):
    for _ in range(n):
        engine.clock.request_step(1)
        engine.clock.advance(0.0)


def _drain_every_output(gate):
    """Put a belt on every output face of `gate`, so its results go somewhere.

    **Without this the comparison is vacuous**, and silently so. A bare gate
    on an empty board has no connected outputs, so every result is a
    dangling-output discard: the CPU writes nothing, the kernel writes
    nothing, and they agree without either having computed anything. Four
    separate arithmetic mutations to the generated evaluator survived the
    whole file before the fixtures grew consumers.

    That is the same trap `test_gates.py` documents from the other side --
    "a bare gate no longer fills its zones, so anything wanting to observe
    back pressure must attach a real consumer".
    """
    ox, oy = gate.origin
    _ins, outs = gate.faces_for(ox, oy, gate.direction, gate.flipped)
    for (xy, face) in outs:
        step = cell.DIR_STEPS[face]
        nx, ny = xy[0] + step[0], xy[1] + step[1]
        if board.in_bounds(nx, ny) and board.is_empty(nx, ny):
            engine.place_belt_path((nx, ny), (nx, ny))
    engine.sync_segments()


def test_the_compute_kernel_compiles():
    """The kernel builds against the generated tables.

    Worth its own test because `gate_fire.comp` goes through the same
    include/generate pipeline as the render shaders -- so a change to
    `Entity.OPS` or the row layout can break it, and the failure would
    otherwise surface as whichever later test happened to build it first.
    """
    harness.reset()
    src = shader_loader.load("gate_fire.comp")
    assert "gate_ops(" in src, "the generated opcode table did not splice in"
    assert "gate_eval(" in src, "the generated evaluator did not splice in"
    kernel = compute.ComputeKernel(harness.ctx, "gate_fire.comp")
    try:
        assert kernel.program is not None
    finally:
        kernel.release()
    ok("gate_fire.comp compiles with the generated tables")


def test_the_glsl_flag_bits_match_python():
    """The geometry word's layout is a contract between two files.

    `core/gate_device.py` packs it and `shaders/gate_fire.comp` unpacks it,
    with no generator between them -- so unlike the opcode table these are
    two hand-written copies and need pinning, exactly as the segment record's
    field offsets do.
    """
    src = shader_loader.load("gate_fire.comp")
    for name, want in (("FLAG_HOST_DECIDES", FLAG_HOST_DECIDES),
                       ("FLAG_FIRE_ANY", FLAG_FIRE_ANY),
                       ("FLAG_SELECT", FLAG_SELECT),
                       ("GEOM_STRIDE", GEOM_STRIDE),
                       ("FLAG_DRAIN_SHIFT", FLAG_DRAIN_SHIFT)):
        needle = f"const int {name} = {want};"
        assert needle in src, (
            f"{name} is {want} in Python; GLSL disagrees or is missing it "
            f"(looked for `{needle}`)")
    # The drains mask must clear the flag bits. An off-by-one here would
    # make channel 0's drain read as "host decides", which silently stops a
    # whole population of gates firing on the GPU.
    assert FLAG_DRAIN_SHIFT > max(
        FLAG_HOST_DECIDES, FLAG_FIRE_ANY, FLAG_SELECT).bit_length()
    ok("the geometry flag bits agree between Python and GLSL")


def test_a_simple_gate_fires_identically_on_both():
    """One Shear, fed, compared channel by channel."""
    harness.reset()
    sh = engine.place_structure(Shear, 10, 10)
    engine.sync_segments()
    gates = _with_device()
    sh.set_input(0, 2.0)
    sh.set_input(1, 3.0)
    problems = compare_against_cpu(gates, board)
    assert not problems, "\n".join(problems)
    ok("a fed Shear produces the same channels on CPU and GPU")


def test_every_machine_type_agrees():
    """Each machine on its own board, fed, both ways.

    One type at a time rather than all at once, so a failure names the
    machine rather than a slot index.

    **Every machine in the game is now a kernel type**, Merge included --
    its `SELECT_RULE` states the value-dependent readiness and consumption
    that used to need the host. The `FLAG_HOST_DECIDES` skip below stays as
    the mechanism for a *future* type that does not fit, and the assertion
    at the end pins that nothing currently uses it: a machine quietly
    falling back would otherwise look exactly like a pass.
    """
    checked = []
    for cls in (Shear, Mac, Reciprocal, Max, Floor, Route, Swap, Merge, Sink,
                Source):
        harness.reset()
        gate = engine.place_structure(cls, 10, 10)
        if gate is None:
            continue
        _drain_every_output(gate)
        gates = _with_device()
        slot = int(gates.state.slot_of[gate.row])
        if gate_flags(gates.device.geom, slot) & FLAG_HOST_DECIDES:
            continue
        # Vacuity guard: with no connected output every result is discarded
        # and the two implementations agree without computing anything.
        if gate.output_count():
            assert gates.state.drains[slot].any(), (
                f"{cls.__name__} has no drained output -- the comparison "
                f"below would pass whatever the kernel did")
        # **Sweep both polarities of channel 0.** Every control in the game
        # reads "positive means act", so a machine with a control has two
        # branches and a single-signed fixture exercises one of them. That
        # is not hypothetical: with only a negative probe, inverting OP_SEL's
        # two arms in the generated evaluator passed this test -- Swap is
        # the only non-host-decided type using OP_SEL, so its untaken branch
        # was the whole coverage gap.
        #
        # Distinct non-zero values per channel, so a swapped channel pair is
        # visible too; equal values would let a transposition through.
        #
        # **And no 1.0, and no two channels equal.** The obvious arithmetic
        # sequence put 1.0 on channel 1, where `c + a*b` and `a + b*c`
        # coincide -- so transposing OP_MAC's arguments was invisible while
        # every other mutation was caught. The probes below are pairwise
        # distinct, none is 0 or +-1, and no two sum or multiply to a third.
        probes = (2.5, 3.0, 7.0, 11.0)
        for sign in (1.0, -1.0):
            for c in range(gate.input_count()):
                v = probes[c % len(probes)]
                gate.set_input(c, v * sign if c == 0 else v)
            if isinstance(gate, Source):
                gate.emit_value = -7.25
                gates.sync_device()
            problems = compare_against_cpu(gates, board)
            assert not problems, (
                f"{cls.__name__} (control sign {sign:+.0f}):\n"
                + "\n".join(problems))
        checked.append(cls.__name__)
    # **Every machine is now a kernel type**, Merge included -- its
    # `SELECT_RULE` made it expressible. The guard is against the skip above
    # quietly widening and leaving this test asserting nothing, so it names
    # the full set rather than a count: a type silently dropping out is
    # exactly the regression that would otherwise look like a pass.
    assert "Merge" in checked, (
        "Merge should be compared now that it declares a SELECT_RULE; if it "
        "is being skipped again, FLAG_HOST_DECIDES has caught it")
    assert len(checked) >= 9, checked
    ok(f"CPU and GPU agree on every machine type: {', '.join(checked)}")


def test_merge_selects_a_lane_and_leaves_the_other_loaded():
    """Merge on the GPU: the whole reason `SELECT_RULE` exists.

    Two claims, and the second is the one that matters. Firing is easy to
    get right; **not eating the unselected lane** is the bug with no visual
    artefact -- items quietly vanishing from a machine the player can see
    holding them. The default "consume every fed channel" would do exactly
    that, which is why `consumed()` became a method in the first place.

    Both control polarities, because a rule that selected the same lane
    either way would pass a single-signed test.
    """
    for control, taken, left in ((1.0, Merge.CH_A, Merge.CH_B),
                                 (-1.0, Merge.CH_B, Merge.CH_A)):
        harness.reset()
        mg = engine.place_structure(Merge, 10, 10)
        _drain_every_output(mg)
        gates = _with_device()
        slot = int(gates.state.slot_of[mg.row])
        assert gate_flags(gates.device.geom, slot) & FLAG_SELECT, (
            "Merge should be a selecting type on the device")
        assert not gate_flags(gates.device.geom, slot) & FLAG_HOST_DECIDES, (
            "a declared SELECT_RULE should let the kernel handle Merge")

        mg.set_input(Merge.CH_CONTROL, control)
        mg.set_input(Merge.CH_A, 11.0)
        mg.set_input(Merge.CH_B, 22.0)
        problems = compare_against_cpu(gates, board)
        assert not problems, (
            f"control {control:+.0f}:\n" + "\n".join(problems))
        # ...and specifically: the unselected lane survived, with its value.
        assert mg.in_held(left), (
            f"control {control:+.0f}: the kernel ate the unselected lane "
            f"{left} -- material the player can see in the machine")
        assert mg.in_value(left) == (11.0 if left == Merge.CH_A else 22.0)
        assert not mg.in_held(taken), (
            f"control {control:+.0f}: lane {taken} should have been consumed")
    ok("Merge selects on the control's sign and leaves the other lane loaded")


def test_merge_waits_for_the_lane_its_control_names():
    """Readiness is value-dependent, not a count of fed channels.

    The other half of `SELECT_RULE`. With the control held and only the
    *unselected* lane fed, Merge must not fire -- FIRE_ANY would (one
    channel is fed) and FIRE_ALL would deadlock waiting for both. Neither
    occupancy rule expresses it, which is the whole reason the rule is data.
    """
    harness.reset()
    mg = engine.place_structure(Merge, 10, 10)
    _drain_every_output(mg)
    gates = _with_device()

    # Control selects A; only B is fed. Nothing should happen on either path.
    mg.set_input(Merge.CH_CONTROL, 1.0)
    mg.set_input(Merge.CH_B, 22.0)
    problems = compare_against_cpu(gates, board)
    assert not problems, "\n".join(problems)
    assert mg.in_held(Merge.CH_B) and mg.in_held(Merge.CH_CONTROL), (
        "nothing was ready, so nothing should have been consumed")
    assert not any(mg.out_held(c) for c in range(mg.output_count())), (
        "Merge fired with the lane its control did not name")
    ok("Merge waits for the lane its control names, on both paths")


def test_back_pressure_blocks_the_kernel_too():
    """A gate with an occupied drained zone must not fire on the GPU either.

    Back pressure is the only mechanism in the design, so a kernel that
    ignored it would run a blocked machine at full rate and silently
    overwrite the waiting value -- which looks like the belt losing items.
    """
    harness.reset()
    sh = engine.place_structure(Shear, 10, 10)
    engine.place_belt_path((12, 10), (14, 10))
    engine.sync_segments()
    gates = _with_device()

    sh.set_input(0, 2.0)
    sh.set_input(1, 3.0)
    # Occupy every output zone by hand, so the gate is unambiguously blocked.
    for c in range(sh.output_count()):
        sh.set_output(c, 99.0)
    problems = compare_against_cpu(gates, board)
    assert not problems, "\n".join(problems)
    # ...and the waiting values must still be the ones we put there.
    for c in range(sh.output_count()):
        assert sh.out_value(c) == 99.0, (
            f"channel {c} was overwritten while blocked: {sh.out_value(c)}")
    ok("a blocked gate stays blocked on the GPU, with its zone intact")


def test_many_ticks_run_without_a_readback():
    """The residency property: N dispatches, one readback at the end.

    This is what the port is *for* -- thousands of ticks in VRAM with the
    host learning nothing in between. Asserts the state actually advanced,
    because a kernel that silently did nothing would also "run" 64 times.
    """
    harness.reset()
    src = engine.place_structure(Source, 6, 10)
    src.emit_value = 3.5
    engine.place_belt_path((8, 10), (12, 10))
    engine.sync_segments()
    gates = _with_device()

    gates.state.sync_from_rows()
    gates.device.upload()
    assert not gates.device.gpu_ahead
    gates.device.run(64)
    assert gates.device.gpu_ahead, (
        "a dispatch must mark the host's copy stale")
    gates.device.readback()
    assert not gates.device.gpu_ahead
    gates.state.flush()

    slot = int(gates.state.slot_of[src.row])
    held = gates.state.out_held[slot]
    assert held.any(), (
        "64 dispatches left every waiting zone empty -- the generator "
        "never fired")
    vals = gates.state.out_value[slot][held > 0]
    assert np.allclose(vals, 3.5), vals
    ok("64 ticks ran in VRAM with one readback, and the Source emitted")


def test_the_kernel_bounds_checks_its_last_group():
    """A gate count that is not a multiple of the work-group size.

    The last group is partly out of range whenever `n % 64`, and an
    unchecked thread writes past the end of every bound buffer -- which
    corrupts whatever numpy put after it, intermittently. 64 is the group
    size, so a board with a few gates is already the awkward case; this
    pins the arithmetic as well as running it.
    """
    for n in (0, 1, 63, 64, 65):
        assert compute.groups_for(n) * compute.LOCAL_SIZE >= n, n
    harness.reset()
    made = 0
    for i in range(5):
        if engine.place_structure(Shear, 4 + i * 3, 10) is not None:
            made += 1
    engine.sync_segments()
    gates = _with_device()
    assert len(gates.state.rows) == made, (len(gates.state.rows), made)
    problems = compare_against_cpu(gates, board)
    assert not problems, "\n".join(problems)
    ok(f"a {made}-gate dispatch stays inside its buffers")


def test_the_direct_handoff_agrees_with_the_cpu():
    """Gate-to-gate delivery, both ways, on a board that actually has one.

    The producer is a belt-fed Shear rather than a Source, for the reason
    `test_gates.py` records: a Source abutting a gate *folds* into its
    neighbour's channel and never fires, so the edge list would be empty and
    this would compare two no-ops. The vacuity guard below is what makes
    that visible rather than a silent pass.
    """
    harness.reset()
    engine.place_structure(Source, 3, 10)
    engine.place_belt_path((5, 10), (9, 10))
    sh = engine.place_structure(Shear, 10, 10)
    engine.place_structure(Sink, 12, 10)
    engine.sync_segments()
    gates = _with_device()

    assert gates.device.edge_count > 0, (
        "no direct edge on a board built to have one -- the comparison "
        "below would pass whatever the kernel did")

    # Load the Shear's zones by hand so there is material to hand over.
    for c in range(sh.output_count()):
        sh.set_output(c, 4.0 + c)
    problems = compare_against_cpu(gates, board, phase="direct")
    assert not problems, "\n".join(problems)
    ok(f"the direct hand-off agrees with the CPU over "
       f"{gates.device.edge_count} edge(s)")


def test_a_direct_edge_moves_material_on_the_gpu():
    """...and actually moves something, rather than agreeing about nothing.

    Separate from the comparison above because "both did nothing" is a
    passing comparison. This asserts the destination lane filled and the
    source zone emptied, which is the whole content of the hand-off.
    """
    harness.reset()
    engine.place_structure(Source, 3, 10)
    engine.place_belt_path((5, 10), (9, 10))
    sh = engine.place_structure(Shear, 10, 10)
    engine.place_structure(Sink, 12, 10)
    engine.sync_segments()
    gates = _with_device()
    assert gates.device.edge_count > 0

    edge = gates.device.edges[0]
    s_slot, s_ch, d_slot, d_ch = (int(v) for v in edge)
    st = gates.state
    st.sync_from_rows()
    st.out_value[s_slot, s_ch] = 6.25
    st.out_held[s_slot, s_ch] = 1.0
    st.in_held[d_slot, d_ch] = 0.0
    gates.device.upload()
    gates.device.run_direct_only()
    gates.device.readback()

    assert st.in_held[d_slot, d_ch] > 0.0, "the far lane was never loaded"
    assert st.in_value[d_slot, d_ch] == 6.25, st.in_value[d_slot, d_ch]
    assert st.out_held[s_slot, s_ch] <= 0.0, "the source zone was not cleared"
    ok("a direct edge moved its value and cleared the zone")


def test_a_loaded_destination_refuses_the_handoff():
    """Back pressure on the direct path: a full lane is not overwritten.

    The failure this guards is silent and destructive -- a kernel that
    clobbered a loaded lane would destroy material with no trace, which is
    exactly what the `in_held` test exists to prevent.
    """
    harness.reset()
    engine.place_structure(Source, 3, 10)
    engine.place_belt_path((5, 10), (9, 10))
    engine.place_structure(Shear, 10, 10)
    engine.place_structure(Sink, 12, 10)
    engine.sync_segments()
    gates = _with_device()
    assert gates.device.edge_count > 0

    s_slot, s_ch, d_slot, d_ch = (int(v) for v in gates.device.edges[0])
    st = gates.state
    st.sync_from_rows()
    st.out_value[s_slot, s_ch] = 6.25
    st.out_held[s_slot, s_ch] = 1.0
    st.in_value[d_slot, d_ch] = 99.0
    st.in_held[d_slot, d_ch] = 1.0
    gates.device.upload()
    gates.device.run_direct_only()
    gates.device.readback()

    assert st.in_value[d_slot, d_ch] == 99.0, (
        f"the kernel overwrote a loaded lane: {st.in_value[d_slot, d_ch]}")
    assert st.out_held[s_slot, s_ch] > 0.0, (
        "the source zone was cleared even though nothing was delivered")

    # **An empty source zone must also deliver nothing**, which is the other
    # half of the guard and needs its own case: with every fixture holding
    # material, deleting the `out_held` test entirely was invisible --
    # mutation-tested, and it survived until this block existed. Without it
    # a gate holding nothing would hand its neighbour a stale zero.
    st.out_held[s_slot, s_ch] = 0.0
    st.out_value[s_slot, s_ch] = 0.0
    st.in_held[d_slot, d_ch] = 0.0
    st.in_value[d_slot, d_ch] = 0.0
    gates.device.upload()
    gates.device.run_direct_only()
    gates.device.readback()
    assert st.in_held[d_slot, d_ch] <= 0.0, (
        "an empty source zone still loaded the destination lane")
    ok("a loaded lane refuses the hand-off, and an empty zone offers none")


def test_belt_transport_agrees_with_the_cpu():
    """The belt phase both ways, on a run with material actually moving.

    The CPU closed form is the reference here rather than `naive_tick()`
    directly, because the closed form is itself oracle-checked over 600
    randomised configurations in tests/test_belts.py. Chaining the two keeps
    each comparison about one thing.
    """
    from core import belt_compute

    harness.reset()
    src = engine.place_structure(Source, 3, 10)
    src.emit_value = 2.5
    engine.place_belt_path((5, 10), (14, 10))
    engine.place_structure(Sink, 16, 10)
    engine.sync_segments()
    gates = _with_device()

    # Let the run fill, so the comparison covers a belt carrying items
    # rather than an empty one -- an empty run agrees trivially.
    _run(12)
    total = engine.belts.total_items()
    assert total > 0, "nothing reached the belt; the fixture is vacuous"

    problems = belt_compute.compare_against_cpu(engine, ticks=1)
    assert not problems, "\n".join(problems)
    ok(f"belt transport agrees with the CPU with {total} items in flight")


def test_belt_transport_agrees_over_many_ticks():
    """...and keeps agreeing, which single-tick parity does not imply.

    A one-tick comparison cannot see an error that only appears once a run
    saturates and the packed term of the `min` starts binding -- which is
    the half of the closed form most likely to be subtly wrong. Ten ticks
    with a blocked consumer gets there.
    """
    from core import belt_compute

    harness.reset()
    src = engine.place_structure(Source, 3, 10)
    src.emit_value = -1.5
    engine.place_belt_path((5, 10), (9, 10))
    engine.sync_segments()
    _run(8)
    gates = _with_device()

    problems = belt_compute.compare_against_cpu(engine, ticks=10)
    assert not problems, "\n".join(problems)
    seg = next(iter(engine.segments.segments.values()))
    assert seg.count > 0, "the run emptied; nothing was compared"
    ok(f"belt transport agrees over 10 ticks, {seg.count} items packed")


def test_the_belt_kernel_pops_into_a_gate():
    """A run delivering into a gate's input lane, on the GPU.

    The pop is the half of the kernel that writes *gate* state, so it is
    where the belt and gate dispatches meet -- and it is the one that found
    a real bug. The belt kernel reads `in_held` to decide whether the gate
    will take the head item, but `BeltCompute.run()` originally bound only
    the segment buffers, leaving bindings 4-7 as whatever bound last. The
    kernel read a stale lane, popped into a channel that was actually full,
    and the symptom was the belt layer disagreeing about a *pop* -- which
    reads as a transport bug and was a binding bug.

    Worth a dedicated fixture for that reason: the two broader comparisons
    either side of this one both passed throughout, because neither has a
    run delivering into an already-loaded gate lane.
    """
    from core import belt_compute

    harness.reset()
    src = engine.place_structure(Source, 3, 10)
    src.emit_value = 7.5
    engine.place_belt_path((5, 10), (10, 10))
    engine.place_structure(Sink, 11, 10)
    engine.sync_segments()
    _run(10)
    gates = _with_device()

    seg = next(iter(engine.segments.segments.values()))
    assert gates._dst_cached(seg) is not None, (
        "the run does not resolve to a gate; the fixture is vacuous")
    problems = belt_compute.compare_against_cpu(engine, ticks=6)
    assert not problems, "\n".join(problems)
    ok("the belt kernel pops into a gate lane in step with the CPU")


def test_the_gpu_tick_matches_the_cpu_tick_through_the_clock():
    """The integrated path: the same board, ticked both ways through `tick()`.

    Everything above tests a kernel. This tests the *wiring* -- the phase
    swap, the upload-once policy, the settle points -- by running the real
    `Engine.tick()` on two identical boards and comparing the result.

    Built twice rather than snapshot-and-restore, because the integration's
    whole risk is state living in the wrong place: a snapshot would have to
    know where that is, which is the thing under test.
    """
    def build():
        harness.reset()
        src = engine.place_structure(Source, 3, 10)
        src.emit_value = 2.0
        engine.place_belt_path((5, 10), (10, 10))
        engine.place_structure(Sink, 11, 10)
        engine.sync_segments()

    def run_and_read(use_gpu):
        build()
        gates = _with_device()
        engine._gpu_enabled = use_gpu
        assert engine.use_gpu_tick() == use_gpu, (
            f"the engine refused to run with gpu={use_gpu}")
        for _ in range(24):
            engine.clock.request_step(1)
            engine.tick(0.0)
        engine.settle()
        segs = {sid: (s.head, s.tail, s.anchor)
                for sid, s in engine.segments.segments.items()}
        return segs, engine.belts.total_items()

    try:
        cpu, cpu_items = run_and_read(False)
        gpu, gpu_items = run_and_read(True)
    finally:
        engine._gpu_enabled = True

    assert cpu_items > 0, "nothing moved on the CPU; the fixture is vacuous"
    assert cpu.keys() == gpu.keys(), (sorted(cpu), sorted(gpu))
    for sid in cpu:
        assert cpu[sid] == gpu[sid], (
            f"segment {sid} after 24 ticks: cpu {cpu[sid]}, gpu {gpu[sid]}")
    assert cpu_items == gpu_items, (cpu_items, gpu_items)
    ok(f"24 ticks through Engine.tick() agree, {cpu_items} items on the belts")


def test_the_tick_counter_advances_the_same_either_way():
    """The clock is untouched by the phase swap.

    `swap_phases()` replaces what a phase *does*, not the clock's sequencing,
    so the tick count, the backlog cap and `alpha` must behave identically.
    A port that quietly ran a different number of ticks would make every
    comparison above meaningless.
    """
    harness.reset()
    engine.place_structure(Source, 3, 10)
    engine.place_belt_path((5, 10), (9, 10))
    engine.sync_segments()
    _with_device()

    for use_gpu in (False, True):
        engine._gpu_enabled = use_gpu
        engine.clock.reset()
        before = engine.clock.tick
        for _ in range(7):
            engine.clock.request_step(1)
            engine.tick(0.0)
        assert engine.clock.tick - before == 7, (
            f"gpu={use_gpu}: ran {engine.clock.tick - before} ticks, not 7")
    engine._gpu_enabled = True
    ok("the tick counter advances identically on both paths")


def test_a_merge_no_longer_vetoes_the_dispatch():
    """A board with a Merge runs on the device.

    This test used to assert the opposite, and the inversion is the point:
    Merge was on three of the four real saves, so the veto meant the port
    did not reach the boards that mattered. `SELECT_RULE` is what removed
    it.

    The veto *mechanism* is still live and still needed for a future type
    that does not fit the rule -- `test_an_unexpressible_machine_vetoes_the
    _dispatch` covers that path directly rather than relying on a real
    machine happening to need it.
    """
    harness.reset()
    engine.place_structure(Shear, 6, 10)
    engine.sync_segments()
    _with_device()
    assert engine.use_gpu_tick(), (
        "a plain Shear board should run on the device")

    engine.place_structure(Merge, 12, 10)
    engine.sync_segments()
    engine.gates.sync_device()
    assert engine.use_gpu_tick(), (
        "a Merge should no longer veto the dispatch -- its SELECT_RULE "
        "makes it expressible")
    assert not engine.gates.has_host_decided_gates()
    ok("a Merge on the board no longer vetoes the GPU tick")


def test_an_unexpressible_machine_vetoes_the_dispatch():
    """The veto mechanism, exercised without a real machine needing it.

    Nothing in the game is host-decided any more, so the fallback has no
    natural fixture -- and an untested fallback is one that quietly stops
    working before the type that needs it arrives. This forces the flag the
    way `geometry_for()` would set it and checks the engine honours it.

    The flag is forced in the packed block rather than by inventing a type,
    because what is under test is the *engine's* response to it, not the
    packing rule (which `geometry_for`'s own self-check covers).
    """
    harness.reset()
    engine.place_structure(Shear, 6, 10)
    engine.sync_segments()
    gates = _with_device()
    assert engine.use_gpu_tick()

    saved = gates.device.geom.copy()
    try:
        gates.device.geom[0][3] |= FLAG_HOST_DECIDES
        assert gates.has_host_decided_gates(), (
            "has_host_decided_gates() did not see the flag")
        assert not engine.use_gpu_tick(), (
            "a host-decided gate must send the whole board to the CPU, or "
            "that machine would silently never fire")
    finally:
        gates.device.geom[...] = saved
    assert engine.use_gpu_tick(), "the veto should have lifted"
    ok("a host-decided gate still vetoes the dispatch")


def test_an_edit_settles_before_re_deriving():
    """An edit must read back first, or it rewinds the simulation.

    `invalidate_segments()` re-derives from entity rows and ring state, so a
    device holding newer results has to be read back before the rebuild --
    otherwise the rebuild reads a stale board, and the upload that follows
    overwrites the dispatches' results. The symptom would be a factory that
    loses however many ticks were outstanding, every time it is edited.
    """
    harness.reset()
    src = engine.place_structure(Source, 3, 10)
    src.emit_value = 5.0
    engine.place_belt_path((5, 10), (10, 10))
    engine.place_structure(Sink, 11, 10)
    engine.sync_segments()
    _with_device()
    engine._gpu_enabled = True

    for _ in range(10):
        engine.clock.request_step(1)
        engine.tick(0.0)
    assert engine._gpu_dirty, (
        "ten GPU ticks should have left the device ahead")

    # An edit anywhere -- this one does not even touch the run.
    engine.place_belt_path((5, 20), (6, 20))
    assert not engine._gpu_dirty, (
        "the edit did not settle; the rebuild just read a stale board")
    ok("an edit settles the device before re-deriving")


def test_the_gpu_tick_conserves_material():
    """Over many ticks the GPU neither loses nor duplicates items.

    The honest statement of what the port guarantees. A *single* phase from
    identical state is exact (every comparison above), but across many ticks
    the two timelines can drift by a tick on individual runs: the CPU walks
    segments in id order, so a run that drains a gate does it before a later
    run in the same phase reads that gate, while every GPU thread reads the
    start-of-dispatch state. See the note in `shaders/belt_tick.comp`.

    That is a scheduling difference, not a correctness one -- material is
    delayed, never created or destroyed -- so this asserts conservation
    rather than equality. The distinction matters: a test demanding equality
    would be demanding a serialised belt phase, which is the thing being
    removed.
    """
    def run(use_gpu, ticks=40):
        harness.reset()
        src = engine.place_structure(Source, 3, 10)
        src.emit_value = 1.0
        engine.place_belt_path((5, 10), (12, 10))
        sh = engine.place_structure(Shear, 13, 10)
        engine.place_belt_path((15, 10), (18, 10))
        engine.sync_segments()
        _drain_every_output(sh)
        _with_device()
        engine._gpu_enabled = use_gpu
        for _ in range(ticks):
            engine.clock.request_step(1)
            engine.tick(0.0)
        engine.settle()
        return (engine.belts.total_items(),
                engine.gates.stats["destroyed"],
                engine.belts.stats["pushed"],
                engine.belts.stats["popped"])

    try:
        cpu = run(False)
        gpu = run(True)
    finally:
        engine._gpu_enabled = True

    assert cpu[0] > 0 and gpu[0] > 0, (
        f"nothing reached the belts at all: cpu {cpu}, gpu {gpu}")
    # **The ring counters are CPU-side only.** `pushed` / `popped` are
    # incremented by `BeltSim._push()` and the pop branch, which the kernel
    # does not run -- it writes ring state directly. So they read zero on the
    # GPU path and are not a conservation check here; the item counts are.
    # Worth stating because a stat that silently reads zero is exactly the
    # kind of thing a later test would assert on and get a false pass from.
    assert gpu[2] == 0 and gpu[3] == 0, (
        "the kernel does not maintain the host ring counters; if it now "
        "does, this test can use them")
    # The two must be within a tick's worth of each other, rather than
    # drifting without bound. A systematic leak would grow with the tick
    # count; a scheduling skew does not.
    assert abs(cpu[0] - gpu[0]) <= 4, (
        f"cpu has {cpu[0]} items, gpu {gpu[0]} -- more than a scheduling "
        f"skew apart")
    ok(f"40 ticks conserve material on both paths "
       f"(cpu {cpu[0]} items, gpu {gpu[0]})")


def test_the_geometry_block_tracks_an_edit():
    """Placing a gate re-packs the geometry rather than leaving it stale.

    The geometry is per-edit, which means something has to notice the edit.
    A stale block is the silent kind of wrong: the kernel would run the
    previous board's types and channel counts against the current rows.
    """
    harness.reset()
    engine.place_structure(Shear, 10, 10)
    engine.sync_segments()
    gates = _with_device()
    first = len(gates.state.rows)

    engine.place_structure(Mac, 14, 10)
    engine.sync_segments()
    gates.sync_device()
    assert len(gates.state.rows) == first + 1, (
        "the new gate never reached the mirror")
    assert gates.device.geom.shape[0] >= first + 1, gates.device.geom.shape
    types = set(gate_type_ids(gates.device.geom, first + 1))
    assert Mac.TYPE_ID in types, (
        "the geometry block does not know about the Mac that was just placed")
    problems = compare_against_cpu(gates, board)
    assert not problems, "\n".join(problems)
    ok("an edit re-packs the geometry block")
