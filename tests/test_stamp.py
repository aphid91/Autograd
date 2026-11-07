"""Copy-stamp transforms: the flip/rotate algebra, and what it preserves.

core/stamp.py self-checks the mirror rule against every type's real port
layout at import, so these are the tests that need more than geometry:

  * that a mirrored machine still *computes* the same thing, which is an
    assumption about arithmetic that no amount of face-comparison can check;
  * that a copied group's internal connections survive a transform, measured
    the way the simulation measures them -- by placing the stamp on a real
    board and asking the segment index what it sees.

The second kind matters because "the ports line up" and "the belts actually
form one run" are different claims, and the second is the one a player cares
about.
"""

from core import cell
from core.entity import Belt, Mac, Shear, Sink, Source, Swap, Underground
from core.stamp import Stamp, StampEntry, StampHistory, mirror_orientation

from tests import harness
from tests.harness import board, engine, ok


def _entry(cls, dx, dy, d=cell.DIR_PX, flipped=False):
    return StampEntry(cls, dx, dy, d, flipped)


def _channel_faces(cls, direction, flipped):
    """{channel: {faces}} for inputs and outputs, in world terms.

    Groups port faces by the *channel* they feed rather than by declaration
    index, which is what makes the comparison meaningful across a mirror: the
    mirror can exchange which face carries which channel, and we want to see
    exactly that.
    """
    ins, outs = cls.faces_for(0, 0, direction, flipped)
    in_map, out_map = {}, {}
    for i, (_xy, face) in enumerate(ins):
        in_map.setdefault(cls.in_channel(i), set()).add(face)
    for i, (_xy, face) in enumerate(outs):
        out_map.setdefault(cls.out_channel(i), set()).add(face)
    return in_map, out_map


def test_mirroring_preserves_what_each_machine_computes():
    """A mirrored machine must compute the same function.

    The geometric rule is pinned in core/stamp.py. What it cannot check is
    semantics: the mirror can swap which face carries which channel, so a
    machine whose operation is *not* symmetric in that pair would quietly
    compute something else after a paste-and-flip.

    Swap and Mac are the cases at risk -- their y/z and x/y inputs sit
    symmetrically about the mirror axis -- and both are safe because their
    operations are invariant under exchanging that pair. Shear is safe for a
    different reason: its two inputs are on different axes, so the mirror
    never exchanges them.
    """
    class _FakeBoard:
        def __init__(self):
            import numpy as np
            from core.entity import ENTITY_STRIDE
            self.entities = np.zeros((4, ENTITY_STRIDE), dtype=np.float32)
            self.entities_dirty = type("_D", (), {"add": lambda s, r: None})()
            self.grid = np.zeros((1, 1, 4), dtype=np.int32)

        def in_bounds(self, x, y):
            return False

    def view(cls):
        v = cls(_FakeBoard(), 1)
        v._ghost = False
        return v

    # Swap exchanges its two lanes; exchanging them first changes nothing.
    sw = view(Swap)
    assert sw.fire([1.0, 7.0, 9.0]) == [1.0, 7.0, 9.0]
    assert sw.fire([1.0, 9.0, 7.0]) == [1.0, 9.0, 7.0]
    # With x <= 0 it swaps, and swapping a swapped pair is the same operation
    # viewed from the other side -- which is exactly what a mirror does.
    a = sw.fire([-1.0, 7.0, 9.0])
    b = sw.fire([-1.0, 9.0, 7.0])
    assert a[1:] == list(reversed(b[1:])), (a, b)
    ok("Swap's operation is invariant under exchanging its y/z lanes")

    # Mac multiplies its two control channels, so their order is irrelevant.
    mc = view(Mac)
    assert mc.fire([3.0, 5.0, 1.0])[2] == mc.fire([5.0, 3.0, 1.0])[2]
    ok("Mac's product is invariant under exchanging its x/y controls")

    # Shear's inputs are on different axes, so no mirror can exchange them:
    # the pass-through enters +Y and the mutated channel enters -X.
    in_map, _out = _channel_faces(Shear, cell.DIR_PX, False)
    assert in_map[0] == {cell.DIR_PY} and in_map[1] == {cell.DIR_NX}
    nd, nf = mirror_orientation(Shear, cell.DIR_PX, False)
    m_in, _m_out = _channel_faces(Shear, nd, nf)
    # The channel-to-axis assignment survives: channel 0 is still vertical.
    assert m_in[0] == {cell.DIR_PY}, m_in
    assert m_in[1] == {cell.DIR_PX}, m_in
    ok("Shear's channels stay on their own axes through a mirror")


def test_mirroring_is_an_involution_for_every_type():
    """Mirroring twice returns the original orientation, for all types."""
    for c in (Belt, Underground, Swap, Shear, Mac, Source, Sink):
        for d in range(4):
            for fl in (False, True):
                nd, nf = mirror_orientation(c, d, fl)
                assert mirror_orientation(c, nd, nf) == (d, fl), (
                    c.__name__, d, fl)
    ok("mirror . mirror == identity for every type, facing and flip")


def test_a_mirrored_belt_run_is_still_one_run():
    """Topology, not just geometry: the pasted run must segment as one.

    The ports lining up and the belts forming a single run are different
    claims. This asks the segment index, which is what the simulation
    actually uses.
    """
    # An L: four belts east, then three north.
    entries = [_entry(Belt, i, 0, cell.DIR_PX) for i in range(4)]
    entries += [_entry(Belt, 4, j, cell.DIR_PY) for j in range(3)]
    # The corner belt must turn, or the run breaks there.
    entries[3] = _entry(Belt, 3, 0, cell.DIR_PX)
    s = Stamp(entries)

    def segments_for(stamp, ox, oy):
        engine.clear_board()
        for (e, x, y) in stamp.place(ox, oy):
            engine.place_belt(x, y, e.direction)
        engine.sync_segments()
        return engine.segments

    base = segments_for(s, 10, 10)
    n_base, cells_base = base.count, sum(
        len(sg.cells) for sg in base.segments.values())

    mirrored = segments_for(s.mirrored(), 10, 10)
    assert mirrored.count == n_base, (
        f"a mirrored run should segment the same way: {mirrored.count} "
        f"vs {n_base}")
    assert sum(len(sg.cells) for sg in mirrored.segments.values()) \
        == cells_base
    assert not mirrored.audit(), mirrored.audit()[:3]
    ok(f"a mirrored L-run still forms {mirrored.count} segment(s), audit clean")

    rotated = segments_for(s.rotated(), 10, 10)
    assert rotated.count == n_base, (rotated.count, n_base)
    assert not rotated.audit(), rotated.audit()[:3]
    ok(f"a rotated L-run still forms {rotated.count} segment(s), audit clean")


def test_a_mirrored_machine_still_connects_to_its_belts():
    """A Source -> belt -> Sink group keeps working after a mirror.

    This is the end-to-end version of the port algebra: place the mirrored
    stamp for real and check the segment index resolves both ends, which is
    what makes items flow.
    """
    s = Stamp([
        _entry(Source, 0, 0, cell.DIR_PX),
        _entry(Belt, 2, 0, cell.DIR_PX),
        _entry(Belt, 3, 0, cell.DIR_PX),
        _entry(Belt, 4, 0, cell.DIR_PX),
        _entry(Sink, 5, 0, cell.DIR_PX),
    ])

    def build(stamp):
        engine.clear_board()
        for (e, x, y) in stamp.place(20, 20):
            if e.entity_cls is Belt:
                engine.place_belt(x, y, e.direction)
            else:
                engine.place_structure(e.entity_cls, x, y, e.direction,
                                       e.flipped)
        engine.sync_segments()
        seg = next(iter(engine.segments.segments.values()), None)
        return seg

    seg = build(s)
    assert seg is not None and seg.src is not None and seg.dst is not None, (
        "setup: the unmirrored chain should resolve both ends")
    ok("the original chain resolves source and sink")

    seg = build(s.mirrored())
    assert seg is not None, "a mirrored chain should still have a segment"
    assert seg.src is not None, (
        "the mirrored Source should still feed the run")
    assert seg.dst is not None, (
        "the mirrored run should still drain into the Sink")
    assert not engine.segments.audit()
    ok("the mirrored chain still resolves source and sink")

    # And it actually moves material, which is the claim that matters.
    for _ in range(20):
        engine.clock.request_step()
        engine.clock.advance(0.0)
    assert engine.gates.stats["consumed"] > 0, engine.gates.stats
    ok(f"the mirrored chain moved {engine.gates.stats['consumed']} items")


def test_a_rotated_machine_group_keeps_its_wiring():
    """Rotation preserves a Source->Sink group at every quarter turn."""
    s = Stamp([
        _entry(Source, 0, 0, cell.DIR_PX),
        _entry(Belt, 2, 0, cell.DIR_PX),
        _entry(Belt, 3, 0, cell.DIR_PX),
        _entry(Sink, 4, 0, cell.DIR_PX),
    ])
    cur = s
    for turn in range(4):
        engine.clear_board()
        for (e, x, y) in cur.place(20, 20):
            if e.entity_cls is Belt:
                engine.place_belt(x, y, e.direction)
            else:
                engine.place_structure(e.entity_cls, x, y, e.direction,
                                       e.flipped)
        engine.sync_segments()
        seg = next(iter(engine.segments.segments.values()), None)
        assert seg is not None, f"turn {turn}: no segment"
        assert seg.src is not None and seg.dst is not None, (
            f"turn {turn}: wiring lost (src={seg.src}, dst={seg.dst})")
        for _ in range(15):
            engine.clock.request_step()
            engine.clock.advance(0.0)
        assert engine.gates.stats["consumed"] > 0, (
            f"turn {turn}: nothing flowed")
        for k in engine.gates.stats:
            engine.gates.stats[k] = 0
        cur = cur.rotated()
    ok("a Source->belt->Sink group flows at all four rotations")


def test_the_paste_cursor_sits_in_the_middle_of_the_stamp():
    """The cursor centres on the full box, so a long run straddles it."""
    run = Stamp([_entry(Belt, i, 0) for i in range(9)])
    _x, _y, w, h = run.bounds()
    assert (w, h) == (9, 1), (w, h)
    ox, oy = run.origin_for_cursor(50, 50)
    cells = run.cells(ox, oy)
    assert (50, 50) in cells, "the cursor must be inside what it is placing"
    # The middle of a 9-long run is its 5th cell, so the box starts 4 west.
    assert ox == 46, ox
    ok("a 9-cell run centres its middle cell on the cursor")

    # An even extent has no middle cell; the lower of the two middles wins,
    # matching Entity.centre_offset()'s convention for a single structure.
    even = Stamp([_entry(Belt, i, 0) for i in range(4)])
    ox2, _oy2 = even.origin_for_cursor(50, 50)
    assert (50, 50) in even.cells(ox2, _oy2)
    assert ox2 == 49, ox2
    ok("an even-length run puts the lower middle cell on the cursor")


def test_stamp_history_keeps_sixteen():
    """A short history, newest first, walked by shift+scroll."""
    h = StampHistory()
    assert h.LIMIT == 16, h.LIMIT
    made = []
    for i in range(20):
        s = Stamp([_entry(Belt, 0, 0)])
        made.append(s)
        h.push(s)
    assert len(h) == 16, len(h)
    assert h.active is made[-1], "the newest push should be active"
    assert made[0] not in h.stamps, "the oldest should have fallen off"

    # Scroll down walks older, up walks newer, and both clamp.
    assert h.cycle(-1) and h.active is made[-2]
    for _ in range(50):
        h.cycle(-1)
    assert h.active is h.stamps[-1], "clamped at the oldest"
    for _ in range(50):
        h.cycle(+1)
    assert h.active is h.stamps[0], "clamped at the newest"
    ok("history holds 16, newest first, clamping at both ends")
