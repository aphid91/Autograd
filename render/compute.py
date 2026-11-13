"""Compute dispatch: the plumbing the tick is being ported onto.

The simulation was built CPU-first and shaped for this all along -- the tick
is two phases that each read state frozen for their duration, which is what
makes each one a dispatch rather than a rewrite (see core/tick.py). This
module is the machinery that lets a phase *be* one: compile a kernel, bind
its buffers, run it, and get the answer back when something actually needs
it on the CPU.

Why a module rather than calls in `Camera`
------------------------------------------

`Camera` owns the *render* pass and its bindings. A compute pass has a
different lifetime (it runs with the tick, not with the frame), a different
failure mode (a wrong barrier corrupts state silently rather than drawing
something visibly wrong), and -- the point of the whole exercise -- is meant
to run thousands of times between readbacks. Mixing the two would tie the
simulation's cadence to the frame's, which is exactly what this is for.

Residency is the whole design
-----------------------------

The target is thousands of ticks per second with **no readback between
them**. That is not an optimisation of the current loop, it is a different
shape: state lives in VRAM, the tick mutates it in place, and the CPU reads
only when the player has to see something or the board is edited.

So `ComputeTick.run(n)` dispatches `n` ticks back to back with only a memory
barrier between them. Nothing is mapped, nothing is read, and the host does
not learn what happened. `readback()` is the deliberate, separate act of
asking -- and it is the expensive one, so it is never implicit.

That is why `dirty` tracking here is inverted from the rest of the project.
Elsewhere (`Gameboard.entities_dirty`) the CPU mutates and marks what the GPU
must be told. Here the GPU mutates and the CPU marks what it must be *told
about* -- `_gpu_ahead` is set by every dispatch and cleared by a readback.
Reading host state while that flag is set is reading a stale copy, which is
the one failure this module exists to make impossible to do by accident.

Barriers
--------

Between the two phases of one tick, and between consecutive ticks, the
barrier is `SHADER_STORAGE_BARRIER_BIT`: every dispatch reads and writes
SSBOs, and the next must see the previous one's writes. This is the explicit
form of the sequencing the CPU got for free by having one callback return
before the next was called (see core/tick.py, "the barrier between phases is
implicit now and becomes a real glMemoryBarrier later"). That comment is now
this line of code.
"""

import numpy as np

from render import shader_loader

#: SSBO bindings for the simulation state. 0-3 belong to the render pass
#: (see render/camera.py), so the compute side starts at 4 -- the two sets
#: coexist because a compute pass and a draw call do not share a binding
#: namespace in practice here, but keeping them disjoint means a kernel and
#: the renderer can read the same buffer with no rebinding.
GATE_IN_VALUE_BINDING = 4
GATE_IN_HELD_BINDING = 5
GATE_OUT_VALUE_BINDING = 6
GATE_OUT_HELD_BINDING = 7
GATE_GEOM_BINDING = 8
GATE_KONST_BINDING = 9
GATE_EDGE_BINDING = 10

#: Threads per work group. 64 is one wavefront on AMD and two warps on
#: NVIDIA, which keeps a partly-filled group cheap -- the gate count is a few
#: hundred, so the tail matters more than peak occupancy does.
LOCAL_SIZE = 64


def groups_for(n, local=LOCAL_SIZE):
    """Work groups needed to cover `n` invocations.

    The kernel must still bounds-check: the last group is partly out of
    range whenever `n` is not a multiple of `local`, and an unchecked thread
    writes past the end of the buffer. That check is one comparison per
    thread and is not worth avoiding.
    """
    return max(1, (int(n) + local - 1) // local)


class ComputeBuffer:
    """One SSBO plus its host mirror, sized together.

    Thin on purpose: the value is that creating, resizing and uploading a
    buffer is one object's business, so a kernel's buffers cannot drift out
    of step with the arrays they mirror -- the hazard `BeltDevice._ensure_
    buffers()` already deals with one layer down.
    """

    def __init__(self, ctx, array, binding):
        self.ctx = ctx
        self.binding = int(binding)
        self.array = None
        self.buffer = None
        self.set_array(array)

    def set_array(self, array):
        """Point at a (possibly new) host array, resizing the SSBO to match."""
        self.array = np.ascontiguousarray(array)
        nbytes = max(self.array.nbytes, 16)
        if self.buffer is None or self.buffer.size != nbytes:
            if self.buffer is not None:
                self.buffer.release()
            self.buffer = self.ctx.buffer(reserve=nbytes)

    def upload(self, array=None):
        """Host -> device. `array` replaces the mirror if given."""
        if array is not None:
            self.set_array(array)
        self.buffer.write(self.array)

    def download(self, out=None):
        """Device -> host. Returns an array shaped like the mirror.

        **This stalls**: it waits for every dispatch that has touched the
        buffer. That is the cost residency exists to avoid, so callers go
        through `ComputeTick.readback()` rather than reaching in here.
        """
        raw = np.frombuffer(self.buffer.read(), dtype=self.array.dtype)
        raw = raw[:self.array.size].reshape(self.array.shape)
        if out is not None:
            out[...] = raw
            return out
        return raw

    def bind(self):
        self.buffer.bind_to_storage_buffer(self.binding)

    def release(self):
        if self.buffer is not None:
            self.buffer.release()
            self.buffer = None


class ComputeKernel:
    """A compiled compute shader, with the same include/generate pipeline
    the render shaders use.

    Going through `shader_loader` rather than compiling a raw string is what
    gives a kernel the generated per-type tables -- `gate_ops()`,
    `type_footprint()`, the row-layout constants -- from the same source the
    fragment shader reads. A kernel that hand-rolled those would reintroduce
    exactly the "GLSL disagrees with Python" class of bug that generating
    them abolished.
    """

    def __init__(self, ctx, name, defines=None):
        self.ctx = ctx
        self.name = name
        self.source = shader_loader.load(name, defines=defines)
        self.program = ctx.compute_shader(self.source)

    def run(self, groups):
        self.program.run(group_x=int(groups))

    def __setitem__(self, key, value):
        """Set a uniform, ignoring one the compiler removed.

        Same forgiving behaviour as `Camera._set()`: an unused uniform is
        optimised out, and a kernel should not have to know which of its
        inputs survived.
        """
        try:
            self.program[key] = value
        except KeyError:
            pass

    def release(self):
        self.program.release()


def barrier(ctx):
    """Make the previous dispatch's SSBO writes visible to the next.

    The explicit form of what the CPU tick got by sequencing calls. Wrong or
    missing, this corrupts state **silently and intermittently** -- the
    previous phase's writes are visible or not depending on timing -- which
    is the worst failure mode in the port and the reason it is one named
    function rather than a line repeated at each dispatch site.
    """
    ctx.memory_barrier()


def _self_check():
    """Dispatch arithmetic. No context, so no GL here.

    The GL-dependent half is exercised by tests/test_compute.py, which needs
    the harness's context; this is the part that can be checked at import
    like the rest of the project's invariants.
    """
    # A partly-filled last group is the normal case, and the kernel's bounds
    # check is what makes it safe -- so the group count must *cover* n, never
    # truncate it.
    assert groups_for(0) == 1, "an empty dispatch still needs a valid call"
    assert groups_for(1) == 1
    assert groups_for(LOCAL_SIZE) == 1
    assert groups_for(LOCAL_SIZE + 1) == 2
    assert groups_for(3 * LOCAL_SIZE) == 3
    for n in (0, 1, 7, 63, 64, 65, 127, 128, 900, 1223):
        assert groups_for(n) * LOCAL_SIZE >= n, (n, groups_for(n))


_self_check()
