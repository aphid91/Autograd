"""Item rendering: read the framebuffer back and check what was drawn.

Every other test in the suite asserts on state. These assert on *pixels*,
which is the only way to catch the class of bug that lives between a correct
simulation and a correct-looking picture: the shader evaluates the closed
form a second time, independently, and can disagree with Python while both
look plausible.

That is not hypothetical. Writing this found three real bugs:

  * `Segment.anchor` was initialised in cells while everything else used
    fixed point. The simulation hid it completely -- the unblocked term of
    the `min` is the smaller one until a belt backs up -- so it only
    surfaced when the renderer read the anchor directly and drew nothing.
  * The shader bracketed candidate items by inverting the *packed* term,
    which on a free-flowing belt names a `k` past `tail` and found nothing.
  * Clamping that bracket into range then skipped the *oldest* items, so a
    run drew its first four discs and dropped the rest.

The fix was to bisect on k instead, which needs only monotonicity and so is
correct in every case rather than in some.

Discs are sampled by peak colour cast over a patch, not by mean: a disc is
antialiased and alpha-blended over the belt beneath it, so averaging in the
surrounding grey washes the hue below any sensible threshold.
"""
import numpy as np

from core import cell
from core.belts import UNIT

from tests import harness
from tests.harness import W, H, board, cam, ctx, engine, ok, orch

#: Zoom for the probes. Big enough that a disc is tens of pixels across, so
#: a one-pixel sampling error cannot flip a result.
PROBE_ZOOM = 40.0

#: Offscreen target, created lazily and reused. The harness window is 1024x768
#: but reading the default framebuffer picks up whatever imgui drew, so these
#: render to their own target.
_fbo = None


#: Whether the world shader has been recompiled for this run.
_reloaded = False


def _target():
    global _fbo, _reloaded
    if not _reloaded:
        # The camera compiles its program once, when the harness builds the
        # orchestrator at import time. Anything that edits the shader source
        # afterwards -- a developer mid-change, a mutation check probing
        # whether these tests have teeth -- would otherwise be invisible
        # here, and these are the only tests that look at shader output.
        assert cam.reload_shader(), cam.compile_error
        assert cam.compile_error is None, cam.compile_error
        _reloaded = True
    if _fbo is None:
        _fbo = ctx.framebuffer(color_attachments=[ctx.texture((W, H), 4)])
    return _fbo


def _render():
    """Draw the world offscreen and return it as an (H, W, 3) uint8 array."""
    engine.sync_to_device()
    fbo = _target()
    fbo.use()
    ctx.clear(0.07, 0.07, 0.09)
    cam.render((W, H), engine.clock, engine.belt_device)
    raw = fbo.read(components=3, dtype="f1")
    img = np.frombuffer(raw, dtype=np.uint8).reshape(H, W, 3)[::-1]
    # Hand the default framebuffer back, or the next real frame draws into
    # our texture and the window goes black.
    ctx.screen.use()
    return img


def _peak_cast(img, cx, cy, half=8):
    """The most strongly red-or-green pixel near a cell's centre."""
    sx, sy = cam.world_to_screen(cx + 0.5, cy + 0.5, (W, H))
    x, y = int(round(sx)), int(round(sy))
    patch = img[max(0, y - half):y + half + 1,
                max(0, x - half):x + half + 1].astype(float)
    if patch.size == 0:
        return np.zeros(3)
    flat = patch.reshape(-1, 3)
    cast = np.maximum(flat[:, 0] - flat[:, 1], flat[:, 1] - flat[:, 0])
    return flat[int(np.argmax(cast))]


def _is_red(c):
    return c[0] > c[1] + 25 and c[0] > c[2] + 25


def _is_green(c):
    return c[1] > c[0] + 25 and c[1] > c[2] + 25


def _discs_on_row(img, x0, x1, y, predicate):
    return {x for x in range(x0, x1 + 1)
            if predicate(_peak_cast(img, x, y))}


def _look_at(cx, cy):
    cam.center[:] = (cx, cy)
    cam.zoom = PROBE_ZOOM


def _at_whole_tick():
    """Pin the clock to an exact tick boundary, so alpha is 0.

    Tests that compare pixel positions against whole-cell offsets need
    alpha == 0, and alpha depends on the accumulator *and* the tick rate.
    Pausing is not enough on its own -- a paused clock reports alpha 0 but
    `request_step()` still works, which is exactly the combination these
    tests want.
    """
    engine.clock.pause()
    engine.clock._accum = 0.0


def test_empty_belt_draws_no_items():
    """A belt with nothing on it must show no item colour at all."""
    engine.place_belt_path((10, 20), (20, 20))
    engine.sync_segments()
    _look_at(15.5, 20.5)
    img = _render()
    red = _discs_on_row(img, 10, 20, 20, _is_red)
    green = _discs_on_row(img, 10, 20, 20, _is_green)
    assert not red and not green, (red, green)
    ok("an empty belt draws no discs")


def test_discs_appear_where_the_simulation_says():
    """Pixel positions must match the simulation, tick by tick.

    This is the check that ties the shader's closed form to Python's. A
    disagreement in either -- the formula, the fixed-point scale, the
    candidate search -- shows up here as a missing or displaced disc.
    """
    engine.place_belt_path((10, 20), (20, 20))
    engine.sync_segments()
    engine.debug_feed = -1.0
    _look_at(15.5, 20.5)
    # Items must be sampled at whole-cell offsets, so alpha has to be 0.
    _at_whole_tick()
    try:
        seg = next(iter(engine.segments.segments.values()))
        for _ in range(9):
            engine.clock.request_step()
            engine.clock.advance(0.0)
            img = _render()
            pos = [p / UNIT for p
                   in engine.belts.positions(seg, engine.clock.tick)]
            # Offset 0 is the tail cell at x=10.
            want = {10 + int(round(p)) for p in pos}
            got = _discs_on_row(img, 10, 20, 20, _is_red)
            assert want == got, (
                f"tick {engine.clock.tick}: simulation says cells "
                f"{sorted(want)}, pixels say {sorted(got)}")
        ok(f"9 ticks: every disc rendered at its simulated cell "
           f"({seg.count} items)")
    finally:
        engine.clock.resume()
        engine.debug_feed = None


def test_disc_colour_follows_the_sign():
    """Green above zero, red at or below it."""
    engine.place_belt_path((10, 20), (20, 20))
    engine.sync_segments()
    _look_at(15.5, 20.5)
    _at_whole_tick()
    try:
        for value, want_green in ((2.5, True), (-1.0, False), (0.0, False)):
            engine.clear_items()
            engine.debug_feed = value
            for _ in range(5):
                engine.clock.request_step()
                engine.clock.advance(0.0)
            img = _render()
            green = _discs_on_row(img, 10, 20, 20, _is_green)
            red = _discs_on_row(img, 10, 20, 20, _is_red)
            if want_green:
                assert green and not red, (value, green, red)
            else:
                assert red and not green, (value, green, red)
        ok("payload sign picks the disc colour (>0 green, <=0 red, 0 red)")
    finally:
        engine.clock.resume()
        engine.debug_feed = None


def test_discs_interpolate_with_alpha():
    """Motion is smooth between ticks, not a snap at the tick boundary.

    Sweeping alpha across a whole tick must move a disc monotonically by
    exactly one cell -- that is what `sim_alpha` is for.
    """
    engine.place_belt_path((10, 20), (20, 20))
    engine.sync_segments()
    engine.debug_feed = -1.0
    _look_at(15.5, 20.5)
    try:
        for _ in range(4):
            engine.clock.request_step()
            engine.clock.advance(0.0)

        row_y = int(round(cam.world_to_screen(0, 20.5, (W, H))[1]))
        centres = []
        for num in range(5):
            alpha = num / 4.0
            engine.clock._accum = alpha * engine.clock.seconds_per_tick
            img = _render()
            row = img[row_y].astype(float)
            xs = [x for x in range(W) if _is_red(row[x])]
            assert xs, f"lost every disc at alpha={alpha}"
            centres.append(max(xs))
        assert centres == sorted(centres), (
            f"the leading disc did not advance monotonically: {centres}")
        moved = (centres[-1] - centres[0]) / PROBE_ZOOM
        assert abs(moved - 1.0) < 0.15, (
            f"a whole tick of alpha moved the disc {moved:.2f} cells, "
            f"expected 1.0")
        ok(f"alpha 0->1 advanced the disc {moved:.2f} cells, monotonically")
    finally:
        engine.clock._accum = 0.0
        engine.debug_feed = None


def test_blocked_clump_is_stationary_between_ticks():
    """A backed-up belt must not creep as alpha rises.

    The whole-tick anchor is clamped at the belt's end; the render path has
    to apply the same clamp. Without it the clump slides forward and snaps
    back every tick, and the head drifts off the end of the belt -- which is
    exactly what the pixels show.
    """
    engine.place_belt_path((10, 20), (20, 20))
    engine.sync_segments()
    _look_at(15.5, 20.5)
    seg = next(iter(engine.segments.segments.values()))
    # Fill it with no consumer, so it packs solid against the end.
    for t in range(40):
        engine.belts.tick_segment(seg, t, offer=lambda _s: -1.0, accept=None)
    engine.clock.tick = 40
    try:
        fixed = None
        for num in range(5):
            engine.clock._accum = (num / 4.0) * engine.clock.seconds_per_tick
            img = _render()
            got = _discs_on_row(img, 10, 20, 20, _is_red)
            if fixed is None:
                fixed = got
            assert got == fixed, (
                f"a fully blocked clump moved at alpha={num / 4.0}: "
                f"{sorted(got)} != {sorted(fixed)}")
        assert len(fixed) >= 10, (
            f"expected the run packed solid, saw {len(fixed)} discs")
        ok(f"{len(fixed)} packed discs, pixel-identical at every alpha")
    finally:
        engine.clock._accum = 0.0


def test_items_render_on_underground_ends_but_not_the_span():
    """An underground end carries items; the buried span cannot draw them.

    Buried positions are real capacity -- items occupy them -- but there is
    no cell to draw in, so items deliberately pop out of existence and back.
    """
    engine.place_belt_path((10, 20), (12, 20))
    engine.place_under(13, 20, cell.DIR_PX, is_exit=False)
    engine.place_under(17, 20, cell.DIR_PX, is_exit=True)
    engine.place_belt_path((18, 20), (20, 20))
    engine.sync_segments()
    assert engine.segments.count == 1, engine.segments.count

    seg = next(iter(engine.segments.segments.values()))
    _look_at(15.5, 20.5)
    # Pack it solid so every drawable position is occupied.
    for t in range(60):
        engine.belts.tick_segment(seg, t, offer=lambda _s: -1.0, accept=None)
    engine.clock.tick = 60
    img = _render()

    drawn = _discs_on_row(img, 10, 20, 20, _is_red)
    # The two end cells are ordinary positions and must show discs.
    assert 13 in drawn, "the underground entrance drew no item"
    assert 17 in drawn, "the underground exit drew no item"
    # The buried cells between them have no segment stamp, so nothing there.
    for x in (14, 15, 16):
        assert x not in drawn, (
            f"cell {x} is a buried span tile and must not draw an item")
    ok(f"items drawn on both underground ends, none on the buried span "
       f"({sorted(drawn)})")


def test_ghost_preview_draws_no_items():
    """A ghost belt shows no items, and does not disturb the real run.

    Note on how much this proves: the `GHOST_PENDING` guard in the shader is
    **defensive, not load-bearing**. A ghost can only spawn on an empty cell,
    and `Gameboard.clear_cell()` zeroes all four channels -- so a ghost cell's
    `.w` is always 0 and `draw_items()` returns immediately whether the guard
    is there or not. Removing the guard does not fail this test, and no
    reachable board state makes it fail.

    It is kept because the invariant it relies on (ghosts never land on a
    stamped cell) lives in two other files, and a future tool that previews
    *over* existing belt -- a blueprint paste, say -- would make the guard
    load-bearing without touching this code. This test at least pins the
    visible behaviour.
    """
    engine.place_belt_path((10, 20), (20, 20))
    engine.sync_segments()
    engine.debug_feed = -1.0
    _look_at(15.5, 20.5)
    try:
        for _ in range(8):
            engine.clock.request_step()
            engine.clock.advance(0.0)
        # Pause so both screenshots are at the same sub-tick time: the
        # harness.step() below banks real dt and would otherwise move every
        # item a fraction of a cell between samples.
        _at_whole_tick()
        before = _discs_on_row(_render(), 10, 20, 20, _is_red)
        assert before, "expected items on the real run"

        # A drag-based belt preview on a clear row.
        harness.select_tool("Belt")
        harness.put_cursor_at_cell(10, 24)
        harness.click()
        harness.put_cursor_at_cell(20, 24)
        harness.step()
        ghosts = harness.ghost_cell_count()
        assert ghosts > 0, "expected a ghost preview"
        # The precondition that makes the guard unreachable, asserted so a
        # change to it is noticed here rather than silently making the guard
        # matter.
        for x in range(10, 21):
            if cell.ghost_state(int(board.grid[24, x, cell.CH_Z])) \
                    == cell.GHOST_PENDING:
                assert cell.segment_id(int(board.grid[24, x, cell.CH_W])) == 0, (
                    f"ghost cell ({x},24) carries a segment stamp -- the "
                    f"shader's GHOST_PENDING guard is now load-bearing and "
                    f"this test needs to actually exercise it")

        img = _render()
        ghost_row = _discs_on_row(img, 10, 20, 24, _is_red)
        assert not ghost_row, (
            f"a ghost preview drew items at {sorted(ghost_row)}")
        # The real run must be untouched by the preview, too.
        real_row = _discs_on_row(img, 10, 20, 20, _is_red)
        assert real_row == before, (before, real_row)
        ok(f"{ghosts} ghost cells drew no items (all unstamped); the real "
           f"run still shows {len(before)}")
    finally:
        engine.clock.resume()
        engine.debug_feed = None


def test_frustrated_cells_keep_drawing_their_items():
    """Hovering a blocked structure over a belt must not hide its items.

    Frustration is an overlay: it leaves .x and .w untouched so the occupant
    survives, and segment derivation counts those cells as real. The renderer
    has to make the same distinction -- a guard of `gstate == GHOST_NONE`
    suppresses a frustrated belt's items, so the discs blink out for as long
    as the player holds a blocked structure over the run. Only GHOST_PENDING
    is fictional.
    """
    engine.place_belt_path((10, 20), (20, 20))
    engine.sync_segments()
    # GREEN items, deliberately. The frustration overlay is itself red
    # (COL_FRUSTRATED), covering the whole cell at 55% alpha, so a red disc
    # under a red tint is indistinguishable from the tint alone -- this test
    # passed for that reason while the guard was wrong. A green disc cannot
    # be faked by the overlay.
    engine.debug_feed = 3.0
    _look_at(15.5, 20.5)
    try:
        for _ in range(10):
            engine.clock.request_step()
            engine.clock.advance(0.0)

        # Pin alpha for both screenshots. harness.step() runs a real frame
        # and banks real dt, so without this the two samples are taken at
        # different sub-tick times -- items genuinely move a fraction of a
        # cell between them and the trailing disc falls off its sample point,
        # which looks exactly like the bug being tested for.
        _at_whole_tick()
        before = _discs_on_row(_render(), 10, 20, 20, _is_green)
        assert len(before) >= 5, before

        # A Shear cannot be placed on an occupied run, so hovering it
        # frustrates the cells it wants without disturbing them.
        harness.select_tool("Shear")
        harness.put_cursor_at_cell(14, 20)
        harness.step()
        frustrated = harness.frustrated_cell_count()
        assert frustrated > 0, "expected frustrated cells"

        img = _render()
        after = _discs_on_row(img, 10, 20, 20, _is_green)
        assert after == before, (
            f"frustration changed which items are drawn:\n"
            f"  before {sorted(before)}\n  after  {sorted(after)}")
        # And specifically: the cells under the tint still show their discs.
        tinted = {x for x in range(10, 21)
                  if cell.is_frustrated(int(board.grid[20, x, cell.CH_Z]))}
        assert tinted, "no frustrated cells on the sampled row"
        assert tinted <= after, (
            f"frustrated cells {sorted(tinted - after)} lost their items "
            f"under the overlay")
        ok(f"{frustrated} frustrated cells (incl. {sorted(tinted)}) kept all "
           f"{len(before)} discs")
    finally:
        engine.clock.resume()
        engine.debug_feed = None


def test_audits_stay_clean_while_rendering():
    """Every audit passes on a board that is actively being drawn.

    Cheap, and it is the check that catches a mis-scaled anchor: the
    simulation hides one (the unblocked term of the min is the smaller until
    a belt backs up) and the pixels can look plausible, but
    `BeltSim.audit()` compares the anchor against the belt length on a fresh
    segment and sees it immediately.
    """
    engine.place_belt_path((10, 20), (20, 20))
    engine.place_belt_path((10, 24), (16, 24))
    engine.sync_segments()
    _look_at(15.5, 22.0)

    # Fresh segments, before anything has ticked: this is where a units
    # mix-up in the initial anchor shows up.
    assert not engine.belts.audit(0), engine.belts.audit(0)
    for seg in engine.segments.segments.values():
        assert seg.anchor == engine.belts.length_of(seg), (
            f"segment {seg.sid}: anchor {seg.anchor} != "
            f"length {engine.belts.length_of(seg)} -- a units mix-up?")

    engine.debug_feed = -1.0
    try:
        for _ in range(14):
            engine.clock.request_step()
            engine.clock.advance(0.0)
            _render()
            problems = (engine.segments.audit()
                        + engine.belts.audit(engine.clock.tick)
                        + engine.belt_device.audit())
            assert not problems, (engine.clock.tick, problems[:4])
        ok("14 rendered ticks: segmentation, transport and device audits "
           "all clean")
    finally:
        engine.debug_feed = None
