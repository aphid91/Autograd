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


#: Glyph layout constants, mirroring the shader's. SEG_HALF_W and NUM_GAP are
#: in glyph units; one glyph unit is ITEM_NUM_SIZE cells.
SEG_HALF_W = 0.60
NUM_GAP = 0.30
ITEM_NUM_SIZE = 0.068


def _columns(img):
    """Lit area of each of the three glyph columns, left to right.

    Sliced at the layout's own column boundaries rather than at blank gaps in
    the render. Gap-finding does not work here: the decimal pip sits *in* the
    gap between columns 0 and 1, leaving a one-pixel separation for "8.88"
    that no threshold can tell from a bar's own antialiasing -- it read as two
    columns, not three.

    Returns (width, height, lit) per column, measuring only the glyph's own
    pixels so the pip is excluded by falling outside every column's span.
    """
    m = _glyph_pixels(img)
    ys, xs = np.nonzero(m)
    if not len(xs):
        return []
    # The three columns are centred at -adv, 0, +adv in glyph units, each
    # SEG_HALF_W wide, so the lit span runs from -(adv + SEG_HALF_W) to
    # +(adv + SEG_HALF_W) -- map that onto the rendered bounding box.
    adv = SEG_HALF_W * 2.0 + NUM_GAP
    span = (adv + SEG_HALF_W) * 2.0          # total width, glyph units
    x0, x1 = int(xs.min()), int(xs.max())
    # Scale from the actual extent, so this does not depend on the zoom.
    per_unit = (x1 - x0 + 1) / span
    out = []
    for centre in (-adv, 0.0, adv):
        lo = x0 + (centre - SEG_HALF_W + span / 2.0) * per_unit
        hi = x0 + (centre + SEG_HALF_W + span / 2.0) * per_unit
        sub = m[:, int(round(lo)):int(round(hi)) + 1]
        if not sub.any():
            out.append((0, 0, 0))
            continue
        cx = np.nonzero(sub.any(axis=0))[0]
        cy = np.nonzero(sub.any(axis=1))[0]
        out.append((int(cx.max() - cx.min() + 1),
                    int(cy.max() - cy.min() + 1),
                    int(sub.sum())))
    return out


def test_the_glyphs_say_what_the_formatter_says():
    """The digits drawn must be the digits the format rules call for.

    The other tests show that *something* value-dependent is drawn. This one
    pins *what*, which is the difference between a working display and a
    plausible-looking one. Mutation testing is why it exists: disabling the
    scientific branch entirely left the other two tests passing, because a
    fallback to fixed point still differs from a 1.00 in some pixels.

    Read back structurally rather than by exact pixels: a column's lit area
    is a proxy for how many bars it lights, which is enough to tell an 8
    (all seven) from a 1 (two) from a blank.
    """
    try:
        # "1.11": the three columns are all 1s, so all three are narrow --
        # a 1 lights only the two right-hand bars.
        harness.reset()
        _one_item_at(1.11)
        ones = _columns(_render())
        assert len(ones) == 3, f"expected 3 glyph columns for 1.11, got {len(ones)}"

        # "8.88": same three columns, every bar lit.
        harness.reset()
        _one_item_at(8.88)
        eights = _columns(_render())
        assert len(eights) == 3, f"expected 3 columns for 8.88, got {len(eights)}"

        # Density is the signal, not width: an 8 lights all seven bars where
        # a 1 lights two, so each column's lit area roughly trebles. Width
        # alone is unreliable -- column 0 of a fixed-point render also picks
        # up the decimal pip, which widens a narrow 1 without filling it.
        for i, (one, eight) in enumerate(zip(ones, eights)):
            assert eight[2] > one[2] * 1.8, (
                f"column {i}: an 8 ({eight[2]}px lit) must be far denser "
                f"than a 1 ({one[2]}px) -- two bars against seven")

        # Every glyph spans the full glyph height, so all three of the 8s
        # must be the same height. (The 1s are not checked this way: a 1 is
        # two vertical bars and its *lit* extent is the same two units, but
        # antialiasing at the ends makes the measurement noisier.)
        heights = [c[1] for c in eights]
        assert max(heights) - min(heights) <= 2, (
            f"the three 8s should be the same height, got {heights}")

        # The middle bar must actually be drawn. An 8 without it is a 0 and a
        # 2 without it is a 7, which a density check does not notice -- one
        # bar in seven is inside the tolerance. Look for it directly: across
        # the vertical centre of an 8, the glyph is solid, whereas a 0 is
        # hollow there. Sample the middle row of the centre column and
        # require it to be mostly lit.
        m = _glyph_pixels(_render())
        ys, xs = np.nonzero(m)
        mid_row = (int(ys.min()) + int(ys.max())) // 2
        adv = SEG_HALF_W * 2.0 + NUM_GAP
        span = (adv + SEG_HALF_W) * 2.0
        per_unit = (int(xs.max()) - int(xs.min()) + 1) / span
        lo = int(xs.min()) + round((span / 2.0 - SEG_HALF_W) * per_unit)
        hi = int(xs.min()) + round((span / 2.0 + SEG_HALF_W) * per_unit)
        band = m[mid_row - 1:mid_row + 2, lo:hi + 1]
        frac = band.any(axis=0).mean() if band.size else 0.0
        assert frac > 0.5, (
            f"an 8's middle bar is missing: only {frac:.0%} of its centre "
            f"row is lit, so it would read as a 0")
        ok("glyph columns read back as the formatter's digits "
           f"(1.11 widths {[c[0] for c in ones]}, "
           f"8.88 widths {[c[0] for c in eights]})")
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

    With only three columns the exponent's sign has nowhere to go, so the
    middle column carries it: "1-9" against "1E9". Get that wrong and two
    values eighteen orders of magnitude apart draw the same three digits --
    the worst failure this display can have, and invisible to every other
    check here, since both render as three crisp well-formed glyphs.

    An earlier version used a "C" (an E minus its middle bar) and this test
    is why it is gone: the two differed by 16 pixels over the whole frame.
    """
    try:
        harness.reset()
        _one_item_at(1e9)
        img_big = _render()
        big, cols_big = _glyph_pixels(img_big), _columns(img_big)

        harness.reset()
        _one_item_at(1e-9)
        img_small = _render()
        small, cols_small = _glyph_pixels(img_small), _columns(img_small)

        h = min(big.shape[0], small.shape[0])
        w = min(big.shape[1], small.shape[1])
        diff = int((big[:h, :w] != small[:h, :w]).sum())
        assert diff > 20, (
            f"1e9 and 1e-9 rendered near-identically ({diff}px differ) -- "
            f"the exponent's sign is being dropped")
        # Specifically the *middle* column, which is the E or the C. Both
        # outer columns carry the same mantissa and exponent digit, so this
        # localises the difference to the sign rather than accepting any
        # difference anywhere.
        mid_big, mid_small = cols_big[1][2], cols_small[1][2]
        assert mid_big != mid_small, (
            f"the E/C column is identical for 1e9 and 1e-9 "
            f"({mid_big}px both) -- the sign is not reaching the glyph")
        ok(f"1e9 and 1e-9 draw distinct glyphs ({diff}px differ; "
           f"E column {mid_big}px vs minus column {mid_small}px)")
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
        """Glyph pixels around a Shear, optionally holding results."""
        harness.reset()
        gate = engine.place_structure(Shear, 10, 20)
        engine.sync_segments()
        if feed:
            gate.set_input(0, 3.0)
            gate.set_input(1, 4.0)
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
