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

from core import cell, font
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


# --- the number on an item -------------------------------------------------
# Seven-segment digits drawn over the disc. There is nothing to read back and
# no glyph geometry stored anywhere, so pixels are the only evidence that a
# value reached the screen -- and the only way to catch digits that render but
# say the wrong thing.

def _disc_centre(img):
    """Screen position of the strongest item-coloured pixel in the frame.

    The glyphs are inside the disc, and the disc is wherever the simulation
    put it, so the probes locate it by colour rather than by cell. Returns
    None if no item colour is present at all.
    """
    f = img.astype(float)
    cast = np.maximum(f[:, :, 1] - np.maximum(f[:, :, 0], f[:, :, 2]),
                      f[:, :, 0] - np.maximum(f[:, :, 1], f[:, :, 2]))
    iy, ix = np.unravel_index(int(np.argmax(cast)), cast.shape)
    return (int(ix), int(iy)) if cast[iy, ix] > 25 else None


def _glyph_pixels(img):
    """Mask of glyph-coloured pixels over the whole frame.

    Glyphs are near-white (COL_ITEM_NUM is (0.93, 0.95, 1.00)) and so are
    both *bright* and *desaturated*. Thresholding the darkest channel is what
    separates them from the ring, which is bright in one channel only, and
    from the belt, which is mid-grey -- a luminance test passes both.

    Frame-wide rather than windowed on the disc: the glyphs sit inside the
    disc and nothing else on the board is this colour, so there is nothing to
    exclude, and a window centred by colour cast lands on the *ring* and
    clips the digits it is meant to measure.
    """
    f = img.astype(float)
    lo = f.min(axis=2)
    spread = f.max(axis=2) - lo
    return (lo > 190) & (spread < 45)


def _digit_mask(img, at=None, half=26):
    """Glyph pixels, cropped to a window around them.

    Returned as a fixed-size window centred on the glyphs' own bounding box
    so two renders can be compared pixel by pixel. An empty result is a
    1x1 False, so `.sum()` is 0 and comparisons still work.
    """
    m = _glyph_pixels(img)
    if at is None:
        ys, xs = np.nonzero(m)
        if not len(ys):
            return np.zeros((1, 1), dtype=bool)
        at = ((int(xs.min()) + int(xs.max())) // 2,
              (int(ys.min()) + int(ys.max())) // 2)
    x, y = at
    return m[max(0, y - half):y + half + 1,
             max(0, x - half):x + half + 1]


#: Zoom for the digit probes. PROBE_ZOOM puts a glyph at 5.4px, right at the
#: fade threshold, so these need their own: at 160 a glyph is ~22px tall and
#: individual bars are several pixels wide.
TEXT_ZOOM = 160.0


def _one_item_at(value):
    """One item on a belt, with the view centred on wherever it ended up.

    The item's position is a function of the tick, so where it lands is the
    simulation's business, not the test's -- `_look_at` on the belt's start
    cell would frame the wrong cell. The disc is found by its colour cast and
    the view is left wide enough for the caller to probe around it.
    """
    engine.place_belt_path((10, 20), (16, 20))
    engine.sync_segments()
    engine.debug_feed = value
    _at_whole_tick()
    engine.clock.request_step()
    engine.clock.advance(0.0)
    # Frame the tail cell, where a single freshly-pushed item sits.
    seg = next(iter(engine.segments.segments.values()))
    pos = engine.belts.position_of(seg, seg.tail - 1, engine.clock.tick, 0.0)
    cam.center[:] = (10.0 + pos / UNIT + 0.5, 20.5)
    cam.zoom = TEXT_ZOOM


def test_an_item_draws_digits_on_its_face():
    """A disc must carry near-white glyph pixels, not just a ring."""
    try:
        _one_item_at(3.14159)
        img = _render()
        lit = _digit_mask(img).sum()
        # "3.14" measures ~210 glyph pixels at TEXT_ZOOM; 100 is comfortably
        # above anything stray and far below a real render.
        assert lit > 100, f"expected glyph pixels on the disc, got {lit}"
        ok(f"an item's value draws as digits ({lit} glyph pixels)")
    finally:
        engine.debug_feed = None


def test_different_values_draw_different_digits():
    """The glyphs must depend on the payload.

    This is the assertion with teeth. A renderer that drew a fixed glyph, or
    that ignored the value, still passes a "digits appear" check -- and that
    is not hypothetical: the first version of this test passed while nothing
    was drawn at all, because its mask was loose enough to count belt pixels
    and it sampled a cell the item was not in.

    Three values chosen to differ in *both* format branches: 1.00 and 8.88
    are fixed point (an 8 lights all seven bars, a 1 lights two), and 5E7 is
    scientific, so a formatter stuck on one branch fails here.
    """
    shapes = {}
    try:
        for value in (1.0, 8.88, 5e7):
            harness.reset()
            _one_item_at(value)
            img = _render()
            m = _digit_mask(img)
            shapes[value] = m
            assert m.sum() > 100, f"{value}: only {m.sum()} glyph pixels"

        # An 8 lights all seven bars where a 1 lights two, so "8.88" draws
        # measurably more than "1.00" -- 409 against 295 as rendered. The
        # margin is deliberately loose: the point is that the value reaches
        # the glyphs at all, not the exact bar areas.
        assert shapes[8.88].sum() > shapes[1.0].sum() * 1.2, (
            f"8.88 ({shapes[8.88].sum()}) should light more segments "
            f"than 1.00 ({shapes[1.0].sum()})")
        # And every pair must differ pixel by pixel, not merely in total --
        # two different numbers can light the same bar area by coincidence.
        # Windows are the same size by construction, but crop to the common
        # shape rather than trusting that, since one clipped at a frame edge
        # would otherwise raise instead of failing with a message.
        vals = list(shapes)
        for a, b in ((vals[0], vals[1]), (vals[1], vals[2]),
                     (vals[0], vals[2])):
            ma, mb = shapes[a], shapes[b]
            h = min(ma.shape[0], mb.shape[0])
            w = min(ma.shape[1], mb.shape[1])
            diff = (ma[:h, :w] != mb[:h, :w]).sum()
            assert diff > 20, (
                f"{a} and {b} drew near-identical glyphs ({diff} px differ)")
        ok("1.00, 8.88 and 5E7 each draw a distinct glyph pattern")
    finally:
        engine.debug_feed = None


#: Mirrors of the shader's text-layout tunables, needed to predict where a
#: glyph lands on screen. NUM_POINT_ADV is common.glsl's; the rest are
#: world.frag's.
#:
#: Parsed out of the shader rather than hardcoded. These are *tunables* --
#: the block they live in exists to be fiddled with, and ITEM_NUM_SIZE was
#: retuned twice while getting the labels legible. A hardcoded mirror turns
#: every such tweak into a test failure that names the wrong thing: the
#: render is fine and the test's arithmetic is stale.
def _shader_float(name, default):
    import re
    from render import shader_loader
    m = re.search(rf"^[ \t]*#define[ \t]+{name}[ \t]+([\d.]+)",
                  shader_loader.load("world.frag"), re.M)
    return float(m.group(1)) if m else default


NUM_POINT_ADV = _shader_float("NUM_POINT_ADV", 3.0)
ITEM_NUM_SIZE = _shader_float("ITEM_NUM_SIZE", 0.042)
ITEM_RADIUS = _shader_float("ITEM_RADIUS", 0.22)


def _read_glyphs(img, text):
    """Slice the rendered text into per-glyph masks, left to right.

    Sliced at the *layout's* advances rather than at blank columns in the
    render. Gap-finding does not work and the failure is instructive: a "1"
    has a blank column inside it, between the serif flag and the stem, so
    "1.11" segmented into nine fragments instead of four. Any font with an
    interior gap in any glyph defeats that approach, which is most fonts.

    So the expected string is passed in and used to reconstruct the pen
    positions, exactly as sd_text() does -- including the narrow advance for
    a decimal point. That makes this a check of *what each cell contains*
    given the layout, with the glyph count asserted separately by the
    caller; it cannot silently pass by finding a different number of glyphs
    than it looked for.

    Returns a list of (x0, x1, mask), the mask cropped to its own ink.
    """
    m = _glyph_pixels(img)
    ys, xs = np.nonzero(m)
    if not len(xs):
        return []
    # Advances in glyph cells, mirroring NUM_POINT_ADV in the shader.
    advs = [NUM_POINT_ADV if ch == "." else float(font.FONT_W) for ch in text]
    total = sum(advs)
    # The render's ink spans the text's *ink*, not its advance box: the first
    # glyph's left bearing and the last one's spacing column are blank. Map
    # from the ink extent by trimming those, which the font makes knowable --
    # the spacing column is always the rightmost of a cell.
    x0, x1 = int(xs.min()), int(xs.max())
    # Leading blank columns of the first glyph, trailing of the last.
    lead = _ink_bounds(text[0])[0]
    trail = float(font.FONT_W) - 1.0 - _ink_bounds(text[-1])[1]
    span = total - lead - trail
    per_cell = (x1 - x0 + 1) / span
    out = []
    pen = 0.0
    for ch, adv in zip(text, advs):
        # Slice at the *advance* boundaries, not at a fixed FONT_W window.
        # A point advances only NUM_POINT_ADV cells, so a FONT_W-wide slice
        # centred on it runs past into the next glyph and swallows it --
        # which read as a "[" and a "/" where "1" and "." belonged.
        #
        # The pen runs in cell units from the text box's left edge, while x0
        # is where the *ink* starts -- which is `lead` cells further in. So
        # the conversion subtracts lead rather than starting the pen
        # negative: doing both double-counts it and shifts every slice left
        # by one cell, which is enough to clip a digit and pick up its
        # neighbour.
        #
        # A point is also *drawn* shifted left within its narrow advance, so
        # the 2-wide pip is centred in it -- sd_text() offsets it by
        # -(FONT_W - NUM_POINT_ADV)/2. The slice has to follow, or the pip
        # falls outside its own window and the next digit's slice is read
        # one cell late.
        draw = pen - ((float(font.FONT_W) - NUM_POINT_ADV) * 0.5
                      if ch == "." else 0.0)
        # Window the glyph's *own* ink columns rather than its whole cell, so
        # a narrow glyph in a narrow advance cannot reach into its
        # neighbour's window. The slack is a third of a cell each side:
        # enough for antialiasing, and tight enough to exclude a neighbour.
        # At half a cell the window following a "0" picked up the decimal
        # pip, and the extra ink on the right made the zero match a "C".
        g0, g1 = _ink_bounds(ch)
        lo = x0 + (draw + g0 - 0.34 - lead) * per_cell
        hi = x0 + (draw + g1 + 1.34 - lead) * per_cell
        sub = m[:, max(0, int(round(lo))):int(round(hi)) + 1]
        if not sub.any():
            out.append((int(round(lo)), int(round(hi)),
                        np.zeros((1, 1), dtype=bool)))
        else:
            rows = np.nonzero(sub.any(axis=1))[0]
            cols = np.nonzero(sub.any(axis=0))[0]
            out.append((int(round(lo)) + int(cols.min()),
                        int(round(lo)) + int(cols.max()),
                        sub[rows.min():rows.max() + 1,
                            cols.min():cols.max() + 1]))
        pen += adv
    return out


def _ink_bounds(ch):
    """(first, last) lit column of `ch`, in glyph cells."""
    lo, hi = font.glyph_bits(ch)
    cols = [x for y in range(font.FONT_H) for x in range(font.FONT_W)
            if _bit(lo, hi, y * font.FONT_W + x)]
    return (min(cols), max(cols)) if cols else (0, font.FONT_W - 1)


def _bit(lo, hi, i):
    """Bit `i` of a glyph's two 24-bit halves."""
    return bool((lo >> i) & 1) if i < 24 else bool((hi >> (i - 24)) & 1)


def _identify(mask):
    """Which character's bitfield best matches a rendered glyph.

    This is the piece that makes the pixel tests able to say *what* was
    drawn rather than merely that something was. The font is Python-known, so
    a rendered glyph can be downsampled onto the 6x8 grid and compared
    against every entry in the table -- no reference images, no committed
    asset, and a new character is covered the moment it is added.

    Returns (char, score) for the best match, score being the fraction of
    the 48 cells that agree.
    """
    h, w = mask.shape
    best, best_score = None, -1.0
    for ch in font.CHARS:
        if ch == " ":
            continue
        lo, hi = font.glyph_bits(ch)
        # The glyph's own ink columns, so a narrow character like "1" is
        # compared on its own extent rather than against a 6-wide box it
        # does not fill. Mirrors what the render's gap-finding produced.
        lit = [(x, y) for y in range(font.FONT_H) for x in range(font.FONT_W)
               if (lo >> (y * font.FONT_W + x) & 1
                   if y * font.FONT_W + x < 24
                   else hi >> (y * font.FONT_W + x - 24) & 1)]
        if not lit:
            continue
        xs = [p[0] for p in lit]
        ys = [p[1] for p in lit]
        gx0, gx1 = min(xs), max(xs)
        gy0, gy1 = min(ys), max(ys)
        gw, gh = gx1 - gx0 + 1, gy1 - gy0 + 1
        agree = 0
        for cy in range(gh):
            for cx in range(gw):
                # Sample the render at this cell's centre. Rows are top-down
                # in the image and bottom-up in the font, hence the flip.
                px = int((cx + 0.5) / gw * w)
                py = int((gh - 1 - cy + 0.5) / gh * h)
                got = bool(mask[min(py, h - 1), min(px, w - 1)])
                want = (gx0 + cx, gy0 + cy) in lit
                if got == want:
                    agree += 1
        score = agree / (gw * gh)
        # Shape agreement alone cannot separate two solid rectangles: a "-"
        # is 4x1 cells and a "." is 2x2, and cropping each to its own ink
        # makes both a filled box that matches the other perfectly. The
        # rendered pip is square and the minus is wide, so the *aspect* is
        # what tells them apart -- and without this a "2.5" read as "2-5".
        #
        # Folded into the score rather than applied as a filter so it breaks
        # ties without overriding a clear structural match: a glyph with real
        # interior detail wins on `agree` by a margin this cannot close.
        want_aspect = gw / gh
        got_aspect = w / h
        penalty = abs(want_aspect - got_aspect) / max(want_aspect, got_aspect)
        score -= 0.25 * penalty
        if score > best_score:
            best, best_score = ch, score
    return best, best_score


def _read_number(img, text):
    """The render identified glyph by glyph, given the layout `text` implies.

    Returns a string of the same length as `text`, so comparing the two says
    whether each cell drew the character the formatter asked for. It cannot
    detect an *extra* glyph beyond the expected count -- `_ink_span` is what
    bounds that, by checking the total ink extent against the layout's width.
    """
    return "".join(_identify(g[2])[0] for g in _read_glyphs(img, text))


def _ink_span(img):
    """(x0, x1) of all glyph ink in the frame, or None if there is none."""
    m = _glyph_pixels(img)
    ys, xs = np.nonzero(m)
    if not len(xs):
        return None
    return int(xs.min()), int(xs.max())


def _expected_ink_px(text, zoom, size):
    """How wide `text`'s ink should be on screen, in pixels.

    Derived from the font and the layout rather than measured, so it can be
    compared against the render to catch a glyph the expected string does not
    account for -- the one thing per-cell identification cannot see.
    """
    advs = [NUM_POINT_ADV if ch == "." else float(font.FONT_W) for ch in text]
    lead = _ink_bounds(text[0])[0]
    trail = float(font.FONT_W) - 1.0 - _ink_bounds(text[-1])[1]
    return (sum(advs) - lead - trail) * size * zoom


def test_the_glyphs_say_what_the_formatter_says():
    """The characters drawn must be the characters the formatter calls for.

    The other tests show that *something* value-dependent is drawn. This one
    pins *what*, by identifying each rendered glyph against the font table
    and comparing against core/font.py's reference formatter.

    That comparison is only possible because the font and the format policy
    both have Python counterparts now. The seven-segment display could only
    be probed statistically -- lit area per column as a proxy for how many
    bars a digit lights -- which could tell an 8 from a 1 but never catch a
    2 drawn where a 5 belonged.

    Two assertions per value, because neither alone is enough: the per-cell
    identification says each position holds the right character, and the ink
    span says there is no *extra* glyph the expected string does not account
    for (a stray minus, or a padded "1.00" where "1" was asked for).
    """
    try:
        for value in (1.11, 8.88, -1.0, 2.5, 0.0, -0.5, -2.75):
            harness.reset()
            _one_item_at(value)
            want = font.format_number(value, sig=2, trim=True)
            img = _render()

            got = _read_number(img, want)
            assert got == want, (
                f"item carrying {value} rendered as {got!r}, but the "
                f"formatter says {want!r}")

            x0, x1 = _ink_span(img)
            measured = x1 - x0 + 1
            expect = _expected_ink_px(want, TEXT_ZOOM, ITEM_NUM_SIZE)
            # Antialiasing adds a pixel or so each side; a wrong glyph count
            # is off by a whole 6-cell advance, which at this zoom is ~29px.
            assert abs(measured - expect) <= 6, (
                f"{value} drew {measured}px of ink but {want!r} should span "
                f"{expect:.0f}px -- the render has a glyph the formatter "
                f"did not ask for, or is missing one")
        ok("rendered glyphs read back as the formatter's own output, at the "
           "expected width (1.11, 8.88, -1, 2.5, 0, -0.5, -2.75)")
    finally:
        engine.debug_feed = None


def test_a_negative_value_draws_its_minus_sign():
    """-1 must render with a leading minus, not as a bare 1.

    This is the behaviour the font change was for. The seven-segment display
    spent all three columns on digits and left the sign to the ring colour
    alone; trimming trailing zeros paid for a sign column, so "-1" is two
    glyphs where "1.00" was three.

    Worth its own test rather than leaning on the string comparison above:
    a dropped minus is a *sign error*, the most consequential thing a number
    display can get wrong, and it leaves a perfectly plausible render.
    """
    try:
        harness.reset()
        _one_item_at(-1.0)
        img = _render()
        neg = _read_glyphs(img, "-1")

        # The leading glyph is a minus.
        first, score = _identify(neg[0][2])
        assert first == "-", (
            f"the leading glyph of -1 identifies as {first!r} "
            f"(score {score:.2f}), not a minus")
        # A minus is wide and short. The one shape it could be confused with
        # is a 1 -- tall and narrow -- so check the aspect directly as well:
        # best-match identification could in principle win on a degenerate
        # render, and this cannot.
        h, w = neg[0][2].shape
        assert w > h, (
            f"the minus rendered {w}x{h}; it must be wider than it is tall")

        # And "-1" really is wider than "1": the minus occupies a column of
        # its own rather than being drawn over the digit.
        neg_span = _ink_span(img)
        harness.reset()
        _one_item_at(1.0)
        pos_span = _ink_span(_render())
        assert (neg_span[1] - neg_span[0]) > (pos_span[1] - pos_span[0]), (
            "-1 must span more pixels than 1 -- the minus needs its own "
            "column, not an overlay on the digit")

        # The ring still carries the sign, as it did before. The point was to
        # add the glyph, not to replace the colour cue, which works at every
        # zoom including below where a glyph resolves at all.
        #
        # Sampled at the *camera centre*, which is where _one_item_at frames
        # the item -- and offset by half a cell because _peak_cast takes a
        # cell index and looks at its centre. The ring is a thin annulus at
        # the rim, so the patch has to be wide enough to reach it: at this
        # zoom the disc is ~35px across, well past the default half of 8.
        harness.reset()
        _one_item_at(-1.0)
        cast = _peak_cast(_render(), cam.center[0] - 0.5, cam.center[1] - 0.5,
                          half=int(ITEM_RADIUS * TEXT_ZOOM) + 2)
        assert _is_red(cast), (
            "a negative item must still draw a red ring as well as a minus")
        ok(f"-1 draws a leading minus ({w}x{h}px), wider than 1, and keeps "
           f"its red ring")
    finally:
        engine.debug_feed = None


def test_a_trimmed_value_drops_its_trailing_zeros_on_screen():
    """-1.00 must draw as "-1" on screen: narrower, not padded.

    The formatter's trimming is unit-tested in core/font.py, but that cannot
    tell whether the *renderer* honours the shortened string -- a layout
    always drawing NUM_MAX_CODES columns would pass every formatter test
    while showing "1.00" where "1" was asked for. The ink extent is what
    distinguishes them, and it is why `sd_text` centres on the *used* width.
    """
    try:
        harness.reset()
        _one_item_at(1.0)
        one = _ink_span(_render())
        harness.reset()
        _one_item_at(1.25)
        full = _ink_span(_render())

        one_px = one[1] - one[0] + 1
        full_px = full[1] - full[0] + 1
        # "1" is one glyph; "1.25" is four with a narrow point. The trimmed
        # render must be substantially narrower, not merely different.
        assert full_px > one_px * 2.0, (
            f"1 drew {one_px}px of ink and 1.25 drew {full_px}px -- trimming "
            f"should make 1 far narrower, so is the renderer padding to a "
            f"fixed column count?")
        assert one_px <= _expected_ink_px("1", TEXT_ZOOM, ITEM_NUM_SIZE) + 6, (
            f"1 drew {one_px}px, wider than a single glyph should be")
        ok(f"a trimmed value draws narrower ({one_px}px for 1 against "
           f"{full_px}px for 1.25)")
    finally:
        engine.debug_feed = None


def test_the_label_may_spill_past_the_disc_rim():
    """A wide number must not be clipped to the disc it sits on.

    The spill is deliberate -- sizing every number to fit the worst case
    inside a 0.165-cell inner radius would make the common short cases
    unreadably small -- but it needs two things in the draw path: the
    coverage written out must be the union of the text's and the disc's, and
    the per-pixel scan's reach must cover the text rather than just the disc.

    Both fail the same way, and it does not look like a bug: the overhang
    simply is not there, which reads as "the number is small". So this
    measures the text against the disc's own diameter.
    """
    try:
        harness.reset()
        # Five glyphs: comfortably wider than the disc.
        _one_item_at(-2.75)
        img = _render()
        x0, x1 = _ink_span(img)
        text_px = x1 - x0 + 1

        disc_px = 2.0 * ITEM_RADIUS * TEXT_ZOOM
        assert text_px > disc_px, (
            f"the number spans {text_px}px but the disc is {disc_px:.0f}px "
            f"across -- the overhang is being clipped to the rim")

        # Specifically: there is ink *outside* the disc on both sides, which
        # is the thing that gets clipped. Measured from the frame centre,
        # where _one_item_at puts the item.
        cx = W / 2.0
        assert (cx - x0) > disc_px / 2.0 and (x1 - cx) > disc_px / 2.0, (
            f"ink runs from {x0} to {x1} around a centre at {cx:.0f} with a "
            f"disc radius of {disc_px / 2.0:.0f}px -- the spill is clipped "
            f"on one side")
        ok(f"a wide label spills past the rim on both sides ({text_px}px of "
           f"text over a {disc_px:.0f}px disc)")
    finally:
        engine.debug_feed = None


def test_a_big_value_goes_scientific():
    """A value past the fixed-point range must switch format, not clamp.

    5e7 cannot be shown as d.dd at all, so the mantissa/E/exponent branch has
    to engage. Checked by the *middle* column: an E is three bars plus a
    spine where a fixed-point middle column is a digit, and crucially the
    decimal pip is absent -- so the column count and the gap structure
    differ from any fixed-point render.

    Without this, disabling the scientific branch outright still passed the
    whole file: a silent fallback to fixed point draws *something* for every
    value, and every other assertion here is satisfied by "something".
    """
    try:
        harness.reset()
        _one_item_at(5e7)          # -> 5E7
        sci = _glyph_pixels(_render())
        sci_px = int(sci.sum())

        harness.reset()
        # Same leading digit in the fixed branch, so any difference between
        # the two renders is the format and not the mantissa.
        _one_item_at(5.0)          # -> 5.00
        fixed = _glyph_pixels(_render())
        fixed_px = int(fixed.sum())

        assert sci_px != fixed_px, (
            f"5e7 and 5.0 drew the same glyph area ({sci_px}px) -- is the "
            f"scientific branch engaging at all?")
        # The decimal pip makes the fixed render's lowest row extend further
        # left-of-centre; more robustly, the two differ substantially.
        h = min(sci.shape[0], fixed.shape[0])
        w = min(sci.shape[1], fixed.shape[1])
        diff = int((sci[:h, :w] != fixed[:h, :w]).sum())
        assert diff > 50, (
            f"5e7 and 5.0 rendered near-identically ({diff}px differ)")
        ok(f"5e7 goes scientific: {sci_px}px against 5.0's {fixed_px}px, "
           f"{diff}px differ")
    finally:
        engine.debug_feed = None


def test_a_negative_exponent_reads_differently():
    """1e-9 and 1e9 must not render identically.

    Two values eighteen orders of magnitude apart drawing the same glyphs is
    the worst failure this display can have, and it is invisible to a check
    that only asks whether *something* was drawn -- both render as crisp
    well-formed glyphs either way.

    The seven-segment display had no column to spare for the exponent's
    sign, so the middle column carried it: "1-9" against "1E9". An earlier
    version used a "C" there (an E minus its middle bar) and the two differed
    by 16 pixels over the whole frame, which is not a legible distinction.
    The bitfield font has room for a real minus, so this now asserts the
    strings themselves rather than probing column densities.
    """
    try:
        harness.reset()
        _one_item_at(1e9)
        big_want = font.format_number(1e9, sig=2, trim=True)
        img_big = _render()
        assert _read_number(img_big, big_want) == big_want, big_want

        harness.reset()
        _one_item_at(1e-9)
        small_want = font.format_number(1e-9, sig=2, trim=True)
        img_small = _render()
        assert _read_number(img_small, small_want) == small_want, small_want

        # The two strings differ by the sign, which is the whole point.
        assert big_want == "1E9" and small_want == "1E-9", (
            f"expected 1E9 and 1E-9, got {big_want!r} and {small_want!r}")
        # And they are visibly different renders, not merely different
        # expectations: the negative is one glyph wider.
        bw = _ink_span(img_big)
        sw = _ink_span(img_small)
        assert (sw[1] - sw[0]) > (bw[1] - bw[0]), (
            f"1E-9 spans {sw[1] - sw[0]}px and 1E9 spans {bw[1] - bw[0]}px "
            f"-- the exponent's minus is not being drawn")
        ok(f"1e9 draws {big_want!r} and 1e-9 draws {small_want!r}, the "
           f"latter one glyph wider")
    finally:
        engine.debug_feed = None


def test_a_blocked_gates_waiting_zone_shows_its_value():
    """An occupied waiting zone must carry its number too.

    A waiting zone is back pressure made visible: the gate produced something
    the belt refused. Showing *what* it is holding is the point of drawing it
    at all, so the number belongs there as much as on a belt -- and the zone
    is drawn squished, which the digits must not inherit or they would be
    illegible.

    A separate test because the waiting zone is a second call site for the
    glyph code, and deleting the number from it left every other assertion
    here passing.
    """
    from core.entity import Shear

    def _glyph_px_for(feed):
        """Glyph pixels around a Shear, optionally holding results.

        Every output gets a one-cell run. A **dangling** output no longer
        fills a waiting zone at all -- its result is destroyed at fire time --
        so a connected run that saturates after one item is now what produces
        a visibly blocked zone.
        """
        harness.reset()
        gate = engine.place_structure(Shear, 10, 20)
        for (xy, face, _ch) in engine.gates._output_faces(gate):
            step_v = cell.DIR_STEPS[face]
            engine.place_belt(xy[0] + step_v[0], xy[1] + step_v[1], face)
        engine.sync_segments()
        if feed:
            # Fire until the runs back up and the zones stay occupied. A
            # one-cell run holds two items at the default spacing, so the
            # count is not one -- loop until blocked rather than assume it.
            for _ in range(8):
                gate.set_input(0, 3.0)
                gate.set_input(1, 4.0)
                _at_whole_tick()
                engine.clock.request_step()
                engine.clock.advance(0.0)
                if not gate.outputs_clear():
                    break
        _at_whole_tick()
        engine.clock.request_step()
        engine.clock.advance(0.0)
        held = not gate.outputs_clear()
        cam.center[:] = (11.0, 20.5)
        cam.zoom = TEXT_ZOOM
        return int(_glyph_pixels(_render()).sum()), held

    # Measured against a starved gate, not against zero. A Shear's own glyph
    # is COL_STRUCT_MARK, which is near-white and so lands in the same mask:
    # the structure alone accounts for ~2100 pixels, enough that a plain
    # "> 100" check passes with the waiting-zone number deleted entirely.
    # Only the *difference* is the numbers.
    starved, held_none = _glyph_px_for(False)
    assert not held_none, "a starved Shear must hold nothing"

    held, held_some = _glyph_px_for(True)
    assert held_some, (
        "the gate should be holding results with nothing downstream")

    delta = held - starved
    assert delta > 150, (
        f"a held waiting zone added only {delta} glyph pixels over a starved "
        f"gate's {starved} -- back pressure is visible but its value is not")
    ok(f"a blocked gate's waiting zone shows its value "
       f"(+{delta} glyph pixels over a starved gate)")


#: Mirrors of the shader's Source-label tunables, so these tests format the
#: expected string with the same policy the renderer uses. Parsed from the
#: shader for the same reason the item ones are.
EMIT_NUM_SIZE = _shader_float("EMIT_NUM_SIZE", 0.052)
EMIT_NUM_SIG = int(_shader_float("EMIT_NUM_SIG", 2))


def _source_at(value, zoom=TEXT_ZOOM, facing=cell.DIR_PX):
    """A Source carrying `value`, framed on its footprint."""
    from core.entity import Source

    harness.reset()
    src = engine.place_structure(Source, 10, 20, direction=facing)
    src.emit_value = value
    engine.sync_segments()
    # A Source is 2x1 authored, so its footprint centre is half a cell off
    # the origin along whichever axis the facing put the long side on.
    w, h = Source.footprint_for(facing)
    cam.center[:] = (10.0 + w * 0.5, 20.0 + h * 0.5)
    cam.zoom = zoom
    return src


def test_a_source_shows_its_emitted_value():
    """A Source must draw the number it emits, read back as that number.

    The whole point of the change: what a generator produces was reachable
    only by clicking it. The value comes straight out of the entity row
    (Source.F_EMIT), so this also exercises the renderer reading gate state
    out of the SSBO rather than from anything uploaded for the purpose.
    """
    for value in (7.0, -3.0, 2.5, 0.0, -1.0):
        _source_at(value)
        want = font.format_number(value, sig=EMIT_NUM_SIG, trim=True)
        img = _render()
        got = _read_number(img, want)
        assert got == want, (
            f"a Source emitting {value} rendered as {got!r}, expected "
            f"{want!r}")
    ok("a Source draws its emitted value (7, -3, 2.5, 0, -1)")


def test_two_sources_show_their_own_values():
    """The value is per instance, so two Sources must differ on screen.

    This is the assertion with teeth. A renderer that drew `Source.EMIT_VALUE`
    -- the class default -- rather than the row's own field would pass the
    test above for whichever value happened to match, and would show every
    Source on the board the same number. That is exactly the bug F_EMIT
    living in the row exists to prevent.
    """
    from core.entity import Source

    harness.reset()
    a = engine.place_structure(Source, 10, 20)
    b = engine.place_structure(Source, 10, 24)
    a.emit_value = 4.0
    b.emit_value = -9.0
    engine.sync_segments()

    reads = []
    for src, y, want_v in ((a, 20, 4.0), (b, 24, -9.0)):
        cam.center[:] = (11.0, y + 0.5)
        cam.zoom = TEXT_ZOOM
        want = font.format_number(want_v, sig=EMIT_NUM_SIG, trim=True)
        reads.append(_read_number(_render(), want))
        assert reads[-1] == want, (
            f"the Source at y={y} shows {reads[-1]!r}, expected {want!r}")
    assert reads[0] != reads[1], (
        f"both Sources rendered {reads[0]!r} -- the renderer is drawing a "
        f"class default rather than each row's own F_EMIT")
    ok(f"two Sources show their own values ({reads[0]} and {reads[1]})")


def test_the_source_label_reads_upright_at_every_facing():
    """Text has a reading orientation a glyph does not.

    A rotated machine's *glyph* turns with it, which is right for a mark and
    wrong for a number: a south-facing Source showing an upside-down "-3"
    is worse than useless. The footprint-space coords the label is drawn in
    are already world-aligned, so the fix is to not rotate them -- and this
    checks the consequence rather than the code.
    """
    for facing in (cell.DIR_PX, cell.DIR_PY, cell.DIR_NX, cell.DIR_NY):
        _source_at(-3.0, facing=facing)
        want = font.format_number(-3.0, sig=EMIT_NUM_SIG, trim=True)
        got = _read_number(_render(), want)
        assert got == want, (
            f"a Source facing {facing} rendered {got!r}, expected {want!r} "
            f"-- the label is rotating with the machine")
    ok("a Source's label reads upright at all four facings")


def test_the_source_label_gives_way_to_its_glyph_when_small():
    """Zoomed out, the number must go and the generator glyph come back.

    A number too small to read is noise, but a blank body is worse than the
    dot it replaced -- a Source would stop being identifiable at a glance.
    So below the cutoff the type glyph returns, which is a different
    behaviour from the item labels (they simply fade, because the disc and
    its ring still carry the sign).
    """
    # Comfortably legible.
    _source_at(5.0, zoom=TEXT_ZOOM)
    big = int(_glyph_pixels(_render()).sum())
    # Far too small for a glyph cell: EMIT_MIN_PX is in glyph pixel height.
    _source_at(5.0, zoom=12.0)
    small = int(_glyph_pixels(_render()).sum())

    assert big > small * 2, (
        f"the label measured {big}px zoomed in and {small}px zoomed out; it "
        f"should shrink away rather than stay drawn")
    # But something is still drawn: the fallback glyph, not an empty body.
    assert small > 0, (
        "a zoomed-out Source drew no mark at all -- the glyph fallback is "
        "missing, so a generator is unidentifiable at a distance")
    ok(f"a Source's label gives way to its glyph when small "
       f"({big}px -> {small}px)")


def test_digits_vanish_when_zoomed_out():
    """Below legibility the digits must go, leaving the disc readable.

    A glyph a few pixels tall is noise over the fill, and worse, its
    near-white pixels dilute the ring colour that carries the sign at every
    zoom. The fade is the reason a puck still reads when its number cannot.
    """
    try:
        _one_item_at(8.88)
        img = _render()
        assert _digit_mask(img).sum() > 40, "no digits at the text zoom"

        # Well below NUM_MIN_PX: at zoom 20 a glyph is ~2.7px tall.
        #
        # Counting near-white pixels is useless here and that is the whole
        # difficulty: a 3px glyph is so heavily antialiased that it never
        # reaches the glyph colour even when fully drawn, so "no bright
        # pixels" is true whether the fade works or not. Removing the fade
        # entirely still passed a threshold-based check.
        #
        # So compare against a disc that is *known* to have no number on it:
        # the same item rendered at the same zoom carrying a value whose
        # glyphs would land in the same pixels. A faded disc must be flat --
        # its interior the fill colour alone -- while an unfaded one has the
        # grey wash of sub-pixel digits over it. Measure the interior's
        # brightness spread rather than its peak.
        cam.zoom = 20.0
        img = _render()
        at = _disc_centre(img)
        assert at is not None, "the disc vanished along with the digits"
        x, y = at
        interior = img[y - 2:y + 3, x - 2:x + 3].astype(float)
        assert interior.size, "sampled off the frame"
        # COL_ITEM_FILL is (0.14, 0.15, 0.18) -> ~36..46. Digits, however
        # dim, lift the darkest channel well above that.
        darkest = float(interior.min(axis=2).max())
        assert darkest < 110.0, (
            f"the disc interior is too bright ({darkest:.0f}) for a disc "
            f"with no number on it -- are the digits still being drawn?")

        # The sign must still be legible from the ring alone.
        x, y = at
        assert _is_green(img[y, x].astype(float)), \
            f"the disc lost its colour: {img[y, x].tolist()}"
        ok("digits fade out when zoomed out; the ring colour survives")
    finally:
        engine.debug_feed = None


# --- the copy-mode selection rectangle -------------------------------------
# Screen furniture rather than board state: nothing is stored per cell for it,
# so the only way to check it exists is to look at the pixels.

def _render_with_rect(rect):
    """Draw the world offscreen with a selection rectangle, as uint8 RGB."""
    engine.sync_to_device()
    fbo = _target()
    fbo.use()
    ctx.clear(0.07, 0.07, 0.09)
    cam.render((W, H), engine.clock, engine.belt_device, select_rect=rect)
    raw = fbo.read(components=3, dtype="f1")
    img = np.frombuffer(raw, dtype=np.uint8).reshape(H, W, 3)[::-1]
    ctx.screen.use()
    return img


def _blue_cast(img, wx, wy, half=3):
    """How blue the pixels near a world point are, at their most extreme."""
    sx, sy = cam.world_to_screen(wx, wy, (W, H))
    x, y = int(round(sx)), int(round(sy))
    patch = img[max(0, y - half):y + half + 1,
                max(0, x - half):x + half + 1].astype(float)
    if patch.size == 0:
        return 0.0
    flat = patch.reshape(-1, 3)
    # Blue minus the warmer channels: the selection colour is the only strongly
    # blue thing on an empty board.
    return float(np.max(flat[:, 2] - (flat[:, 0] + flat[:, 1]) * 0.5))


def test_the_selection_rectangle_draws_where_it_is_asked():
    """Copy mode's rubber band must appear, and only where it is dragged."""
    harness.reset()
    cam.center[:] = (20.0, 20.0)
    cam.zoom = PROBE_ZOOM

    off = _render_with_rect(None)
    # The box covers cells 18..22 inclusive, so world 18..23.
    on = _render_with_rect((18, 18, 22, 22))

    # An edge of the box lights up; the same point is plain without it.
    edge_on = _blue_cast(on, 18.0, 20.0)
    edge_off = _blue_cast(off, 18.0, 20.0)
    assert edge_on > edge_off + 20, (
        f"no selection edge drawn: {edge_off:.1f} -> {edge_on:.1f}")
    ok(f"the selection edge drew (blue cast {edge_off:.1f} -> {edge_on:.1f})")

    # The interior gets a faint wash, weaker than the outline.
    mid_on = _blue_cast(on, 20.5, 20.5)
    mid_off = _blue_cast(off, 20.5, 20.5)
    assert mid_on > mid_off + 4, (
        f"no interior wash: {mid_off:.1f} -> {mid_on:.1f}")
    assert mid_on < edge_on, "the wash should be fainter than the outline"
    ok(f"the interior washed faintly ({mid_off:.1f} -> {mid_on:.1f})")

    # Well outside the box, nothing changed. This is the assertion that would
    # catch the shader's `edge` going large and negative far away and lighting
    # up the whole screen.
    far_on = _blue_cast(on, 28.0, 28.0)
    far_off = _blue_cast(off, 28.0, 28.0)
    assert abs(far_on - far_off) < 3, (
        f"the selection leaked outside its box: {far_off:.1f} -> {far_on:.1f}")
    ok("nothing drew outside the selection box")


# --- the window is resizable ----------------------------------------------
# The GL viewport does not follow the window on its own, and the default
# framebuffer never resizes its own viewport, so the world pass has to
# re-state it every frame. Skip that and the pass keeps drawing into the
# rectangle the context was created with: the window grows and the new
# region stays the clear colour, as unused grey.
#
# Only a pixel test can see this. Every uniform is already correct when the
# bug is present -- `resolution` is the live framebuffer size and
# screen_to_world agrees with it -- so nothing at the state level disagrees
# with anything. What is wrong is the rectangle the GPU is allowed to write.

def test_the_world_pass_fills_a_resized_window():
    """The render must cover whatever size it is handed, not the startup one.

    Rendered into a target deliberately *larger* than the harness window, so
    a pass still clipped to (W, H) leaves the extra region untouched. The
    clear colour is a sentinel the world never draws rather than the app's
    own dark grey -- against the real background an undrawn pixel and a
    legitimately-empty one are the same colour, which is precisely what made
    the first attempt at this test pass against the broken build.
    """
    harness.reset()

    big_w, big_h = W + 256, H + 192
    sentinel = (1.0, 0.0, 1.0)      # magenta
    target = ctx.framebuffer(
        color_attachments=[ctx.texture((big_w, big_h), 4)])
    try:
        engine.sync_to_device()
        target.use()
        ctx.clear(*sentinel)
        # Zoomed in far enough that the board covers the whole target, so any
        # surviving sentinel pixel is a region the pass never wrote rather
        # than honest off-board space.
        cam.center[:] = (20.0, 20.0)
        cam.zoom = PROBE_ZOOM
        # Leave a stale, too-small viewport behind, which is the state a
        # resized window is actually in: the old rectangle is still current
        # and only the render can widen it. Without this the test has no
        # teeth at all -- binding a framebuffer resets the viewport to that
        # framebuffer's full size, so the pass would cover the target whether
        # it set the viewport or not.
        ctx.viewport = (0, 0, W, H)
        cam.render((big_w, big_h), engine.clock, engine.belt_device)
        raw = target.read(components=3, dtype="f1")
        img = np.frombuffer(raw, dtype=np.uint8).reshape(big_h, big_w, 3)
    finally:
        # Hand the default framebuffer back, or the next real frame draws
        # into our texture and the window goes black.
        ctx.screen.use()
        target.release()

    want = np.array([int(c * 255 + 0.5) for c in sentinel])
    undrawn = np.all(np.abs(img.astype(int) - want) <= 2, axis=2)

    # The far corner is the first place a stale viewport shows, being
    # outside (W, H) on both axes.
    corner = undrawn[-40:-4, -40:-4]
    assert not corner.any(), (
        f"the corner past ({W}, {H}) was never drawn: "
        f"{corner.mean() * 100:.0f}% still the clear colour -- the pass is "
        f"clipped to the startup viewport")

    # And nowhere else either, so a partial fix does not pass.
    assert not undrawn.any(), (
        f"{undrawn.mean() * 100:.1f}% of a {big_w}x{big_h} target was left "
        f"undrawn")
    ok(f"the world pass filled a {big_w}x{big_h} target "
       f"(harness window is {W}x{H})")


def test_the_selection_rectangle_normalises_its_corners():
    """A drag up-and-left must draw the same box as down-and-right."""
    harness.reset()
    cam.center[:] = (20.0, 20.0)
    cam.zoom = PROBE_ZOOM

    forward = _render_with_rect((18, 18, 22, 22))
    backward = _render_with_rect((22, 22, 18, 18))
    assert np.array_equal(forward, backward), (
        "corner order changed the rendered box")
    ok("the selection box is corner-order independent")
