"""Draw the annotation store through imgui's draw lists.

The one place annotations become pixels. It projects world coordinates to
screen with `Camera.world_to_screen` and emits imgui primitives -- so there is
no shader, no SSBO, no vertex buffer and no cell arithmetic anywhere in the
feature.

**Why imgui rather than the world shader.** Annotations are mostly *labels*,
and the world shader's text is a 6x8 bitfield font (see core/font.py): blocky
by design, uppercase-only, and a fine machine readout but poor prose. imgui
already has a real proportional font with kerning and lowercase, and
imgui-bundle 1.92 bakes glyphs dynamically per size -- measured at 1000 labels
across 1000 *distinct* sizes for the same cost as one size, so sizing text by
zoom is free. That is the property a world-space label needs and the thing
that would have made an older imgui unusable here.

The costs, accepted deliberately:

- **Annotations draw over everything.** imgui renders after the world pass, so
  an annotation cannot be occluded by the board or sit under an item disc.
  For telestrator marks that is the correct layering anyway.
- **They are outside the framebuffer the pixel tests read.** The world pass's
  readback tests (tests/test_item_render.py) cannot see them, so annotations
  are tested through their model and their projection rather than their
  pixels. That is a weaker *visual* guarantee and a stronger *semantic* one:
  a test asserts the exact string, not a glyph-shape match.

**Nothing here mutates.** Same discipline as ui/panel.py: this reads the store
and draws. The tool state it needs (an in-progress stroke, a drag rectangle)
is passed in and owned by the orchestrator.
"""

from imgui_bundle import imgui

from core.annotations import PALETTE, Stroke, TextBox

#: Opacity annotations are drawn at. Slightly under 1 so a mark reads as an
#: overlay on the factory rather than as part of it -- the same instinct
#: behind the control-signal overlay's PASS_ALPHA.
ALPHA = 0.92

#: Opacity for a *previewed* annotation -- a paste's notes, drawn where they
#: would land but not yet placed. Low enough to read as pending, since a
#: pasted label is otherwise identical to one already on the board.
GHOST_ALPHA = 0.45

#: The resize grip on the selected text box: a right-angle corner mark at the
#: box's bottom-right, in screen pixels so it stays grabbable at any zoom.
HANDLE_PX = 11.0

#: Screen-pixel size below which a text label is not drawn at all. Under this
#: imgui's font is illegible mush, and a smear of grey pixels over the board
#: is worse than nothing. The shader's item labels fade out over a band for
#: the same reason (NUM_MIN_PX); this is a hard cut because a label has no
#: ring to fall back on, so there is nothing to fade *to*.
MIN_TEXT_PX = 5.0

#: Screen-pixel thickness floor for a stroke, so a thin mark stays visible
#: when zoomed out instead of disappearing into a sub-pixel line.
MIN_STROKE_PX = 1.0

#: Text inset from its box's top-left corner, in screen pixels.
TEXT_PAD_PX = 3.0


def _rgba(colour_index, alpha=ALPHA):
    """A PALETTE index as the packed ABGR int imgui's draw lists take."""
    i = max(0, min(len(PALETTE) - 1, int(colour_index)))
    r, g, b = PALETTE[i][1]
    return imgui.IM_COL32(int(r * 255), int(g * 255), int(b * 255),
                          int(alpha * 255))


def _visible(x0, y0, x1, y1, vw, vh, margin=64.0):
    """Whether a screen-space box is near enough the viewport to bother with.

    Culling matters here in a way it does not for the world shader: every
    annotation costs CPU work and vertices whether or not it lands on screen,
    so a board with hundreds of notes should only pay for the ones in view.
    The margin keeps a stroke whose ends are off-screen but whose middle
    crosses the view from being dropped.
    """
    return not (x1 < -margin or x0 > vw + margin
                or y1 < -margin or y0 > vh + margin)


class AnnotationLayer:
    """Projects and draws annotations. Holds no annotation state of its own."""

    def __init__(self, active=True):
        #: Set by the orchestrator from the panel's checkbox. When False,
        #: `draw()` returns before touching imgui or the store at all, so a
        #: disabled layer costs exactly one bool test per frame -- the same
        #: guarantee the segment inspector's window guard gives.
        self.enabled = True
        #: Whether there is an imgui frame to draw into at all.
        #:
        #: Separate from `enabled`, and not a player setting: the headless
        #: test harness builds a real Orchestrator but has no imgui backend
        #: and never calls `new_frame()`, and
        #: `imgui.get_foreground_draw_list()` outside a frame **crashes the
        #: process natively** -- no Python exception, no traceback, just a
        #: silent exit. So the precondition lives here rather than being a
        #: condition the caller is trusted to remember.
        #:
        #: It deliberately does *not* gate the model: with this False the
        #: store still records, hit-tests, copies and saves, so every
        #: annotation behaviour except the pixels is testable headlessly.
        #: Gating `enabled` instead would have made "the layer is off" mean
        #: two different things.
        self.active = bool(active)
        #: Last frame's primitive count, for the panel to report.
        self.drawn = 0

    def draw(self, store, camera, viewport, in_progress=None,
             drag_rect=None, selected_id=None, preview=(), handle_of=None):
        """Draw everything. `store` is an AnnotationStore.

        `in_progress` is the stroke being drawn this frame (not yet in the
        store), `drag_rect` the live text-box rectangle as world coords, and
        `selected_id` the annotation the Status window has open -- drawn with
        a selection box so the player can see which one they are editing.

        `preview` is a list of annotations that are **not** in the store: a
        paste's notes, positioned where they would land. Drawn at a lower
        alpha so they read as pending rather than placed -- the same
        distinction the entity ghosts make, and necessary because a pasted
        label is otherwise indistinguishable from one already on the board.

        `handle_of` is a TextBox to draw a resize grip on.
        """
        self.drawn = 0
        # The zero-cost-when-off guard. Before any imgui call, any iteration
        # of the store, and any projection: an unchecked box must not cost a
        # draw list lookup, let alone a world->screen conversion per item.
        if not self.enabled or not self.active:
            return 0

        if store is None:
            return 0
        if (not store and in_progress is None and drag_rect is None
                and not preview):
            return 0

        vw, vh = viewport
        # The foreground list draws over every imgui window as well as the
        # world. No begin()/end() and no invisible window needed, which is
        # what keeps this from stealing mouse focus from the board.
        dl = imgui.get_foreground_draw_list()
        zoom = camera.zoom

        def to_screen(wx, wy):
            return camera.world_to_screen(wx, wy, viewport)

        for a in store:
            if isinstance(a, Stroke):
                self._draw_stroke(dl, a, to_screen, zoom, vw, vh)
            elif isinstance(a, TextBox):
                self._draw_text(dl, a, to_screen, zoom, vw, vh)

        # A paste's notes, under the same code path at ghost alpha. Drawn
        # after the store so a previewed note sits over what it would land
        # on, matching how the entity ghosts read.
        for a in preview:
            if isinstance(a, Stroke):
                self._draw_stroke(dl, a, to_screen, zoom, vw, vh,
                                  alpha=GHOST_ALPHA)
            elif isinstance(a, TextBox):
                self._draw_text(dl, a, to_screen, zoom, vw, vh,
                                alpha=GHOST_ALPHA)

        if in_progress is not None and len(in_progress.points) >= 1:
            self._draw_stroke(dl, in_progress, to_screen, zoom, vw, vh)

        if drag_rect is not None:
            self._draw_drag_rect(dl, drag_rect, to_screen)

        if selected_id is not None:
            sel = store.get(selected_id)
            if sel is not None:
                self._draw_selection(dl, sel, to_screen)

        if handle_of is not None:
            self._draw_handle(dl, handle_of, to_screen)

        return self.drawn

    # --- per-type drawing ------------------------------------------------
    def _draw_stroke(self, dl, stroke, to_screen, zoom, vw, vh,
                     alpha=ALPHA):
        pts = stroke.points
        if not pts:
            return
        bx0, by0, bx1, by1 = stroke.bounds()
        # Project the box corners, then test. Screen y is flipped relative to
        # world y, so the projected corners come back with y descending --
        # hence the explicit min/max rather than trusting the order.
        sx0, sy0 = to_screen(bx0, by1)
        sx1, sy1 = to_screen(bx1, by0)
        if not _visible(min(sx0, sx1), min(sy0, sy1),
                        max(sx0, sx1), max(sy0, sy1), vw, vh):
            return

        thickness = max(MIN_STROKE_PX, stroke.world_width() * zoom)
        col = _rgba(stroke.colour, alpha)

        if len(pts) == 1:
            # A dot: a click with no drag. Drawn as a filled circle, since a
            # one-point polyline draws nothing at all.
            cx, cy = to_screen(*pts[0])
            dl.add_circle_filled(imgui.ImVec2(cx, cy), thickness * 0.5, col)
            self.drawn += 1
            return

        screen = [imgui.ImVec2(*to_screen(x, y)) for (x, y) in pts]
        # One polyline per stroke rather than per segment: imgui joins the
        # segments and the vertex count is the same, but the call overhead
        # (and the Python loop) is paid once.
        dl.add_polyline(screen, col, 0, thickness)
        self.drawn += 1

    def _draw_text(self, dl, box, to_screen, zoom, vw, vh, alpha=ALPHA):
        x0, y0, x1, y1 = box.bounds()
        # The anchor is the box's top-left in world terms, which is (x0, y1).
        sx, sy = to_screen(x0, y1)
        ex, ey = to_screen(x1, y0)
        if not _visible(min(sx, ex), min(sy, ey),
                        max(sx, ex), max(sy, ey), vw, vh):
            return

        col = _rgba(box.colour, alpha)
        # A faint outline, so an empty box is still findable and clickable.
        # Without it a text box with no text yet would be invisible, and the
        # creation flow deliberately makes one of exactly that kind.
        dl.add_rect(imgui.ImVec2(sx, sy), imgui.ImVec2(ex, ey),
                    _rgba(box.colour, alpha * 0.35), 0.0, 0, 1.0)
        self.drawn += 1

        if not box.text:
            return

        px = box.world_size() * zoom
        if px < MIN_TEXT_PX:
            # Too small to read. The box outline above still shows something
            # is there, which is the graceful degradation a hard cut needs.
            return

        font = imgui.get_font()
        # Clipped to the box so long text does not bleed across the board.
        # `add_text`'s wrap_width wraps at the box width, and the clip rect
        # bounds it vertically -- together they make the dragged rectangle
        # mean something rather than being decoration.
        clip = imgui.ImVec4(sx, sy, ex, ey)
        dl.add_text(font, px,
                    imgui.ImVec2(sx + TEXT_PAD_PX, sy + TEXT_PAD_PX),
                    col, box.text,
                    None,
                    max(8.0, (ex - sx) - TEXT_PAD_PX * 2.0),
                    clip)
        self.drawn += 1

    def _draw_drag_rect(self, dl, rect, to_screen):
        """The live rectangle while a text box is being dragged out."""
        wx0, wy0, wx1, wy1 = rect
        sx0, sy0 = to_screen(wx0, wy0)
        sx1, sy1 = to_screen(wx1, wy1)
        lo = imgui.ImVec2(min(sx0, sx1), min(sy0, sy1))
        hi = imgui.ImVec2(max(sx0, sx1), max(sy0, sy1))
        dl.add_rect_filled(lo, hi, imgui.IM_COL32(200, 200, 255, 36))
        dl.add_rect(lo, hi, imgui.IM_COL32(220, 220, 255, 190), 0.0, 0, 1.5)
        self.drawn += 2

    def _draw_handle(self, dl, box, to_screen):
        """The resize grip, at the box's bottom-right corner.

        Sized in **screen pixels** rather than world units so it stays the
        same grabbable size at every zoom -- a world-sized grip would be
        sub-pixel when zoomed out and enormous when zoomed in. That is also
        why `Orchestrator` does the hit test in screen space: the drawn
        target and the clickable target have to be the same shape.

        Drawn as two strokes making a right angle rather than a filled
        triangle, which reads as a grip rather than as part of the label.
        """
        x0, y0, x1, y1 = box.bounds()
        # Bottom-right in world terms is (x1, y0); screen y is flipped, so
        # that corner is the largest screen y.
        cx, cy = to_screen(x1, y0)
        col = imgui.IM_COL32(255, 255, 255, 230)
        dl.add_line(imgui.ImVec2(cx - HANDLE_PX, cy), imgui.ImVec2(cx, cy),
                    col, 2.0)
        dl.add_line(imgui.ImVec2(cx, cy - HANDLE_PX), imgui.ImVec2(cx, cy),
                    col, 2.0)
        # A short diagonal across the corner, the conventional resize mark.
        dl.add_line(imgui.ImVec2(cx - HANDLE_PX * 0.55, cy),
                    imgui.ImVec2(cx, cy - HANDLE_PX * 0.55),
                    imgui.IM_COL32(255, 255, 255, 150), 1.5)
        self.drawn += 3

    def _draw_selection(self, dl, annotation, to_screen):
        """A dashed-ish box round whatever the Status window has open."""
        x0, y0, x1, y1 = annotation.bounds()
        sx0, sy0 = to_screen(x0, y1)
        sx1, sy1 = to_screen(x1, y0)
        lo = imgui.ImVec2(min(sx0, sx1) - 2.0, min(sy0, sy1) - 2.0)
        hi = imgui.ImVec2(max(sx0, sx1) + 2.0, max(sy0, sy1) + 2.0)
        dl.add_rect(lo, hi, imgui.IM_COL32(255, 255, 255, 210), 0.0, 0, 1.5)
        self.drawn += 1
