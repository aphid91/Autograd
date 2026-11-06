"""InputManager: raw glfw/imgui input -> game intents.

This is the only place that reads the keyboard/mouse or consults imgui's
want_capture_* flags, so there is exactly one answer to "is the UI eating this
input?". It owns the belt-planning state machine but no board state: it tells
the orchestrator what the player wants and the engine decides what happens.

A left-press does not act immediately. It *arms* a gesture, and what happens
depends on what the mouse does next: moving past DRAG_THRESHOLD_PX turns it
into a camera pan, while releasing without moving that far is a click. This is
why building happens on release rather than press -- the press alone is
ambiguous.

State machine, for a drag-based tool (a belt run):

  IDLE      left-press on world    -> ARMED (remember where, act later)
            right-press on world   -> ERASING
  ARMED     moved past threshold   -> PANNING
            left-release           -> PLANNING (anchor = the pressed cell)
  PANNING   mouse move             -> drag the camera
            left-release           -> IDLE (no build)
  PLANNING  mouse move             -> re-preview the path
            R / shift+R            -> the active tool's cycle, re-preview
            F                      -> the active tool's flip, re-preview
            shift+F                -> frame the whole board
            left-press             -> ARMED again; the release commits
            right-press / Esc      -> cancel, -> IDLE
  ERASING   drag                   -> erase under cursor
            right-release          -> IDLE

A click-based tool (a structure) never enters PLANNING: it previews under the
cursor while IDLE and commits on a left *release* that was not a drag. Both
styles hand the builder the same (anchor, target) pair, so builders need not
care which gesture produced them.

Right-press is consumed by the cancel when PLANNING, so a cancel never also
starts an erase -- the erase gesture needs a fresh press.

`R`, `shift+R` and `F` are deliberately *per-tool*: this class reports that
they were pressed and the active builder decides what they mean (cycle a
belt's path shape, rotate a structure, mirror a chiral one). `F` takes the
bare key and the camera's frame-the-board moves to `shift+F`, since framing
is much the rarer action.

`Space` and `.` drive the simulation clock -- pause and single-step. They are
global rather than per-tool: the clock is not something a builder has an
opinion about.
"""

import glfw
from imgui_bundle import imgui

from core.engine_paths import bresenham
from core.toolbelt import SLOT_COUNT

STATE_IDLE = 0
STATE_PLANNING = 1
STATE_ERASING = 2
STATE_ARMED = 3      # left button down, not yet known to be click or drag
STATE_PANNING = 4

STATE_NAMES = {
    STATE_IDLE: "Idle",
    STATE_PLANNING: "Planning",
    STATE_ERASING: "Erasing",
    STATE_ARMED: "Armed",
    STATE_PANNING: "Panning",
}

# How far the cursor must move, in framebuffer pixels, before a held left
# button counts as a camera drag rather than a click. Small enough that a
# deliberate drag registers immediately, large enough that hand tremor during
# a click does not steal the build.
DRAG_THRESHOLD_PX = 6.0

# Keys that are edge-triggered (act once per press) rather than held.
_SLOT_KEYS = {
    glfw.KEY_1: 1, glfw.KEY_2: 2, glfw.KEY_3: 3, glfw.KEY_4: 4, glfw.KEY_5: 5,
    glfw.KEY_6: 6, glfw.KEY_7: 7, glfw.KEY_8: 8, glfw.KEY_9: 9, glfw.KEY_0: 0,
}
_EDGE_KEYS = (
    glfw.KEY_R, glfw.KEY_ESCAPE, glfw.KEY_F, glfw.KEY_F5, glfw.KEY_DELETE,
    glfw.KEY_SPACE, glfw.KEY_PERIOD,
) + tuple(_SLOT_KEYS)


class Intents:
    """What the player asked for this frame. Plain data, consumed once."""

    __slots__ = (
        "pan_x", "pan_y", "zoom", "cycle_tool", "cycle_backward", "flip_tool",
        "frame_board", "reload_shader", "clear_board",
        "begin_plan", "update_plan", "commit_plan", "cancel_plan",
        "erase_at", "cursor_cell", "cursor_in_world", "cursor_frac",
        "select_slot", "scroll_steps", "scroll_at",
        "drag_pan_px", "toggle_pause", "step_tick",
    )

    def __init__(self):
        self.pan_x = 0.0
        self.pan_y = 0.0
        self.zoom = 0.0
        # R (or shift+R) -- meaning is the active tool's to decide.
        self.cycle_tool = False
        self.cycle_backward = False
        # F -- also the active tool's to interpret.
        self.flip_tool = False
        self.frame_board = False
        self.reload_shader = False
        self.clear_board = False
        self.begin_plan = None     # (x, y) anchor
        self.update_plan = False
        self.commit_plan = False
        self.cancel_plan = False
        self.erase_at = []         # [(x, y), ...] cells to erase this frame
        self.cursor_cell = None    # (x, y) or None if off-world
        # Where the cursor sits *within* its cell, each component 0..1. Tools
        # use this to aim a belt from which part of the square was clicked.
        self.cursor_frac = (0.5, 0.5)
        self.cursor_in_world = False
        self.select_slot = None    # slot index requested this frame
        self.scroll_steps = 0.0    # scroll notches this frame
        self.scroll_at = None      # cursor position to anchor the zoom on
        self.drag_pan_px = (0.0, 0.0)   # camera drag delta, framebuffer px
        # Simulation clock controls. Space pauses, '.' single-steps -- which
        # is the primary way to watch one tick's worth of state change.
        self.toggle_pause = False
        self.step_tick = False


class InputManager:
    def __init__(self, window, camera):
        self.window = window
        self.camera = camera

        self.state = STATE_IDLE
        self.anchor = None          # planning start cell
        self.target = None          # planning end cell (cursor)
        # Sub-cell cursor position at each end, 0..1 each axis. Tools use these
        # to aim the first and last belt of a run.
        self.anchor_frac = (0.5, 0.5)
        self.target_frac = (0.5, 0.5)
        self._last_hover = None     # for click-based tools' idle preview
        self._last_frac = None
        # Armed-press bookkeeping.
        self._press_px = (0.0, 0.0)
        self._press_cell = None
        self._press_frac = (0.5, 0.5)
        self._pending_commit = False
        self._pan_last_px = None

        self._prev_keys = {k: False for k in _EDGE_KEYS}
        self._prev_left = False
        self._prev_right = False
        self._last_erase_cell = None

        # Scroll arrives as events, not state, so it is accumulated by a
        # callback and drained once per frame. imgui's glfw backend installs
        # its own scroll callback; chain to it so the UI keeps scrolling.
        self._scroll_accum = 0.0
        self._prev_scroll_cb = glfw.set_scroll_callback(
            window, self._on_scroll
        )

    # --- helpers --------------------------------------------------------
    def _viewport(self):
        return glfw.get_framebuffer_size(self.window)

    def _cursor_px(self):
        """Cursor position in framebuffer pixels.

        glfw reports the cursor in window coordinates; on a HiDPI display the
        framebuffer is larger, so scale before converting or picking drifts
        from drawing.
        """
        mx, my = glfw.get_cursor_pos(self.window)
        win_w, win_h = glfw.get_window_size(self.window)
        fb_w, fb_h = self._viewport()
        if win_w > 0 and win_h > 0:
            mx *= fb_w / win_w
            my *= fb_h / win_h
        return mx, my

    def _cursor_cell_frac(self):
        """((cell_x, cell_y), (fx, fy)) for the cursor.

        `fx`/`fy` are the position within the cell, 0..1, with y up to match
        world orientation -- so (0.5, 0.9) is near the cell's north edge.
        """
        import math
        mx, my = self._cursor_px()
        wx, wy = self.camera.screen_to_world(mx, my, self._viewport())
        cx, cy = math.floor(wx), math.floor(wy)
        return (cx, cy), (wx - cx, wy - cy)

    def _on_scroll(self, window, xoff, yoff):
        self._scroll_accum += yoff
        if self._prev_scroll_cb is not None:
            self._prev_scroll_cb(window, xoff, yoff)

    def _take_scroll(self):
        v, self._scroll_accum = self._scroll_accum, 0.0
        return v

    def _key(self, k):
        return glfw.get_key(self.window, k) == glfw.PRESS

    def _edge(self, k, allowed):
        """True on the frame a key goes down. Tracks state even when blocked."""
        now = self._key(k)
        fired = now and not self._prev_keys[k] and allowed
        self._prev_keys[k] = now
        return fired

    # --- main poll ------------------------------------------------------
    def poll(self, dt, board, drag_based=True):
        """Read input and return the frame's Intents.

        `drag_based` comes from the active tool: a belt needs press-move-click
        to define a run, while a structure previews continuously under the
        cursor and places on a single click.
        """
        io = imgui.get_io()
        want_kb = io.want_capture_keyboard
        want_mouse = io.want_capture_mouse

        it = Intents()

        # --- camera: held keys, scaled by dt ---
        if not want_kb:
            if self._key(glfw.KEY_A):
                it.pan_x -= 1.0
            if self._key(glfw.KEY_D):
                it.pan_x += 1.0
            if self._key(glfw.KEY_W):
                it.pan_y += 1.0
            if self._key(glfw.KEY_S):
                it.pan_y -= 1.0
            if self._key(glfw.KEY_E):
                it.zoom += 1.0
            if self._key(glfw.KEY_Q):
                it.zoom -= 1.0

        it.reload_shader = self._edge(glfw.KEY_F5, not want_kb)
        it.clear_board = self._edge(glfw.KEY_DELETE, not want_kb)
        cycle = self._edge(glfw.KEY_R, not want_kb)
        escape = self._edge(glfw.KEY_ESCAPE, not want_kb)

        # --- simulation clock ---
        # Space pauses; '.' runs exactly one tick, which works whether or not
        # the sim is paused. Both are edge-triggered: holding them must not
        # run a tick per frame.
        it.toggle_pause = self._edge(glfw.KEY_SPACE, not want_kb)
        it.step_tick = self._edge(glfw.KEY_PERIOD, not want_kb)

        # R is per-tool; shift reverses whatever it means.
        shift = (self._key(glfw.KEY_LEFT_SHIFT)
                 or self._key(glfw.KEY_RIGHT_SHIFT))
        it.cycle_tool = cycle
        it.cycle_backward = shift

        # F is per-tool like R (the active builder decides what a flip means);
        # shift+F is the camera's frame-the-board, which is the rarer action
        # and so gives up the bare key.
        f_key = self._edge(glfw.KEY_F, not want_kb)
        it.flip_tool = f_key and not shift
        it.frame_board = f_key and shift

        # --- toolbelt selection ---
        # Number keys only: scroll is zoom now, Factorio-style.
        for key, digit in _SLOT_KEYS.items():
            if self._edge(key, not want_kb):
                it.select_slot = (SLOT_COUNT - 1) if digit == 0 else digit - 1
                break

        # --- scroll zoom, anchored at the cursor ---
        scroll = 0.0 if want_mouse else self._take_scroll()
        if scroll:
            it.scroll_steps = scroll
            it.scroll_at = self._cursor_px()

        # --- cursor ---
        (cx, cy), frac = self._cursor_cell_frac()
        mx, my = self._cursor_px()
        it.cursor_cell = (cx, cy)
        it.cursor_frac = frac
        it.cursor_in_world = not want_mouse and board.in_bounds(cx, cy)

        left = glfw.get_mouse_button(self.window, glfw.MOUSE_BUTTON_LEFT) == glfw.PRESS
        right = glfw.get_mouse_button(self.window, glfw.MOUSE_BUTTON_RIGHT) == glfw.PRESS
        left_press = left and not self._prev_left
        left_release = (not left) and self._prev_left
        right_press = right and not self._prev_right
        right_release = (not right) and self._prev_right
        self._prev_left = left
        self._prev_right = right

        # Mouse presses that start a gesture are ignored over imgui windows.
        # Releases are always honoured, so a gesture cannot get stuck on if the
        # cursor happens to end up over a panel.
        if want_mouse:
            left_press = right_press = False

        # --- state machine ---
        if self.state == STATE_IDLE:
            # Escape while idle still cancels: a click-based tool never
            # enters PLANNING, but it can hold multi-step state -- an
            # underground part-way through a pair -- that needs a way out.
            if escape:
                it.cancel_plan = True
            # A click-based tool (a structure) has no drag phase, but it still
            # must not build on press -- the press might become a pan. It
            # previews under the cursor and waits for the release.
            if not drag_based and not want_mouse and board.in_bounds(cx, cy):
                self.anchor = self.target = (cx, cy)
                # Carry the sub-cell position too: a click-based tool plans
                # from target_frac, and a structure whose footprint is even
                # on an axis uses it to choose which cell boundary its
                # midline lands on. Leaving it stale pinned every hover
                # preview to whatever fraction a previous gesture ended on.
                self.anchor_frac = self.target_frac = frac
                if (cx, cy) != self._last_hover or frac != self._last_frac \
                        or cycle or it.flip_tool:
                    it.update_plan = True
                self._last_hover = (cx, cy)
                self._last_frac = frac

            if left_press and board.in_bounds(cx, cy):
                self.state = STATE_ARMED
                self._press_px = (mx, my)
                self._press_cell = (cx, cy)
                self._press_frac = frac
            elif right_press and board.in_bounds(cx, cy):
                self.state = STATE_ERASING
                self._last_erase_cell = None
                it.erase_at.append((cx, cy))
                self._last_erase_cell = (cx, cy)

        elif self.state == STATE_ARMED:
            moved = ((mx - self._press_px[0]) ** 2
                     + (my - self._press_px[1]) ** 2) ** 0.5
            if moved > DRAG_THRESHOLD_PX:
                # This is a drag, not a click: take over as a camera pan and
                # never build from this press.
                self.state = STATE_PANNING
                self._pan_last_px = (mx, my)
                it.drag_pan_px = (mx - self._press_px[0], my - self._press_px[1])
            elif left_release:
                # A click. What it means depends on what we were doing.
                if self._pending_commit:
                    # Second click of a drag-based gesture: commit.
                    self.state = STATE_IDLE
                    self._pending_commit = False
                    self.anchor = self.target = None
                    it.commit_plan = True
                elif drag_based:
                    # First click: start planning from the pressed cell.
                    self.state = STATE_PLANNING
                    self.anchor = self._press_cell
                    self.target = (cx, cy)
                    self.anchor_frac = self._press_frac
                    self.target_frac = frac
                    it.begin_plan = self.anchor
                    it.update_plan = True
                else:
                    # Click-based tool: build where the press landed.
                    self.state = STATE_IDLE
                    self.anchor = self.target = self._press_cell
                    self.anchor_frac = self.target_frac = self._press_frac
                    it.commit_plan = True

        elif self.state == STATE_PANNING:
            if left_release:
                # If the press that became this pan was going to commit a
                # plan, that plan is still live -- return to it rather than
                # dropping the player's half-drawn run because they panned.
                if self._pending_commit:
                    self.state = STATE_PLANNING
                    self._pending_commit = False
                    self.target = (cx, cy)
                    self.target_frac = frac
                    it.update_plan = True
                else:
                    self.state = STATE_IDLE
                self._pan_last_px = None
            else:
                last = self._pan_last_px or (mx, my)
                it.drag_pan_px = (mx - last[0], my - last[1])
                self._pan_last_px = (mx, my)

        elif self.state == STATE_PLANNING:
            if right_press or escape:
                # Cancel. The right-press is consumed here and does not fall
                # through to starting an erase.
                self.state = STATE_IDLE
                self.anchor = self.target = None
                self._pending_commit = False
                it.cancel_plan = True
            elif left_press:
                # Arm the commit: it fires on release, unless this turns into
                # a drag, in which case the plan survives and we keep planning.
                self.state = STATE_ARMED
                self._press_px = (mx, my)
                self._press_cell = (cx, cy)
                self._press_frac = frac
                self._pending_commit = True
            else:
                if cycle or it.flip_tool:
                    # The orchestrator applies these to the active tool; we
                    # only need to know the preview must be rebuilt.
                    it.update_plan = True
                if (cx, cy) != self.target or frac != self.target_frac:
                    self.target = (cx, cy)
                    self.target_frac = frac
                    it.update_plan = True

        elif self.state == STATE_ERASING:
            if right_release:
                self.state = STATE_IDLE
                self._last_erase_cell = None
            else:
                # Erase along the drag. Interpolating between the previous and
                # current cell keeps a fast drag from leaving gaps.
                if (cx, cy) != self._last_erase_cell:
                    if self._last_erase_cell is not None:
                        it.erase_at.extend(
                            _trace(self._last_erase_cell, (cx, cy))
                        )
                    else:
                        it.erase_at.append((cx, cy))
                    self._last_erase_cell = (cx, cy)

        return it

    def cancel(self):
        """Force the state machine back to idle (used when the UI takes over)."""
        self.state = STATE_IDLE
        self.anchor = self.target = None
        self._last_erase_cell = None
        self._last_hover = None
        self._last_frac = None
        self._pending_commit = False
        self._pan_last_px = None


def _trace(a, b):
    """Cells from a to b exclusive of a, 4-connected. For gap-free erase drags."""
    return bresenham(a[0], a[1], b[0], b[1])[1:]
