"""InputManager: raw glfw/imgui input -> game intents.

This is the only place that reads the keyboard/mouse or consults imgui's
want_capture_* flags, so there is exactly one answer to "is the UI eating this
input?". It owns the belt-planning state machine but no board state: it tells
the orchestrator what the player wants and the engine decides what happens.

State machine, for a drag-based tool (a belt run):

  IDLE      left-press on world   -> PLANNING (anchor = cell under cursor)
            right-press on world  -> ERASING
  PLANNING  mouse move            -> re-preview the path
            R / shift+R           -> the active tool's cycle, re-preview
            left-press            -> commit, -> IDLE
            right-press / Esc     -> cancel, -> IDLE
  ERASING   drag                  -> erase under cursor
            right-release         -> IDLE

A click-based tool (a structure) never enters PLANNING: it previews under the
cursor while IDLE and commits on a single left-press. Both styles hand the
builder the same (anchor, target) pair, so builders need not care which
gesture produced them.

Right-press is consumed by the cancel when PLANNING, so a cancel never also
starts an erase -- the erase gesture needs a fresh press.

`R` and `shift+R` are deliberately *per-tool*: this class reports that they
were pressed and the active builder decides what they mean (cycle a belt's
path shape, rotate a structure).
"""

import glfw
from imgui_bundle import imgui

from engine_paths import bresenham
from toolbelt import SLOT_COUNT

STATE_IDLE = 0
STATE_PLANNING = 1
STATE_ERASING = 2

STATE_NAMES = {STATE_IDLE: "Idle", STATE_PLANNING: "Planning", STATE_ERASING: "Erasing"}

# Keys that are edge-triggered (act once per press) rather than held.
_SLOT_KEYS = {
    glfw.KEY_1: 1, glfw.KEY_2: 2, glfw.KEY_3: 3, glfw.KEY_4: 4, glfw.KEY_5: 5,
    glfw.KEY_6: 6, glfw.KEY_7: 7, glfw.KEY_8: 8, glfw.KEY_9: 9, glfw.KEY_0: 0,
}
_EDGE_KEYS = (
    glfw.KEY_R, glfw.KEY_ESCAPE, glfw.KEY_F, glfw.KEY_F5, glfw.KEY_DELETE,
) + tuple(_SLOT_KEYS)


class Intents:
    """What the player asked for this frame. Plain data, consumed once."""

    __slots__ = (
        "pan_x", "pan_y", "zoom", "cycle_tool", "cycle_backward", "frame_board",
        "reload_shader", "clear_board",
        "begin_plan", "update_plan", "commit_plan", "cancel_plan",
        "erase_at", "cursor_cell", "cursor_in_world",
        "select_slot", "scroll",
    )

    def __init__(self):
        self.pan_x = 0.0
        self.pan_y = 0.0
        self.zoom = 0.0
        # R (or shift+R) -- meaning is the active tool's to decide.
        self.cycle_tool = False
        self.cycle_backward = False
        self.frame_board = False
        self.reload_shader = False
        self.clear_board = False
        self.begin_plan = None     # (x, y) anchor
        self.update_plan = False
        self.commit_plan = False
        self.cancel_plan = False
        self.erase_at = []         # [(x, y), ...] cells to erase this frame
        self.cursor_cell = None    # (x, y) or None if off-world
        self.cursor_in_world = False
        self.select_slot = None    # slot index requested this frame
        self.scroll = 0.0


class InputManager:
    def __init__(self, window, camera):
        self.window = window
        self.camera = camera

        self.state = STATE_IDLE
        self.anchor = None          # planning start cell
        self.target = None          # planning end cell (cursor)
        self._last_hover = None     # for click-based tools' idle preview

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

    def _cursor_cell(self):
        """Cell under the cursor, in framebuffer pixels.

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
        return self.camera.screen_to_cell(mx, my, (fb_w, fb_h))

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

        it.frame_board = self._edge(glfw.KEY_F, not want_kb)
        it.reload_shader = self._edge(glfw.KEY_F5, not want_kb)
        it.clear_board = self._edge(glfw.KEY_DELETE, not want_kb)
        cycle = self._edge(glfw.KEY_R, not want_kb)
        escape = self._edge(glfw.KEY_ESCAPE, not want_kb)

        # R is per-tool; shift reverses whatever it means.
        shift = (self._key(glfw.KEY_LEFT_SHIFT)
                 or self._key(glfw.KEY_RIGHT_SHIFT))
        it.cycle_tool = cycle
        it.cycle_backward = shift

        # --- toolbelt selection ---
        for key, digit in _SLOT_KEYS.items():
            if self._edge(key, not want_kb):
                it.select_slot = (SLOT_COUNT - 1) if digit == 0 else digit - 1
                break

        it.scroll = 0.0 if want_mouse else self._take_scroll()

        # --- cursor ---
        cx, cy = self._cursor_cell()
        it.cursor_cell = (cx, cy)
        it.cursor_in_world = not want_mouse and board.in_bounds(cx, cy)

        left = glfw.get_mouse_button(self.window, glfw.MOUSE_BUTTON_LEFT) == glfw.PRESS
        right = glfw.get_mouse_button(self.window, glfw.MOUSE_BUTTON_RIGHT) == glfw.PRESS
        left_press = left and not self._prev_left
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
            # A click-based tool (a structure) has no drag phase: it shows a
            # preview under the cursor whenever idle, and a click commits it.
            # Anchor and target are the same cell so builders see one gesture
            # shape regardless of which style produced it.
            if not drag_based and not want_mouse and board.in_bounds(cx, cy):
                self.anchor = self.target = (cx, cy)
                if left_press:
                    it.commit_plan = True
                    it.update_plan = True
                elif (cx, cy) != self._last_hover or cycle:
                    it.update_plan = True
                self._last_hover = (cx, cy)

            if drag_based and left_press and board.in_bounds(cx, cy):
                self.state = STATE_PLANNING
                self.anchor = (cx, cy)
                self.target = (cx, cy)
                it.begin_plan = (cx, cy)
                it.update_plan = True
            elif right_press and board.in_bounds(cx, cy):
                self.state = STATE_ERASING
                self._last_erase_cell = None
                it.erase_at.append((cx, cy))
                self._last_erase_cell = (cx, cy)

        elif self.state == STATE_PLANNING:
            if right_press or escape:
                # Cancel. The right-press is consumed here and does not fall
                # through to starting an erase.
                self.state = STATE_IDLE
                self.anchor = self.target = None
                it.cancel_plan = True
            elif left_press:
                self.state = STATE_IDLE
                self.anchor = self.target = None
                it.commit_plan = True
            else:
                if cycle:
                    # The orchestrator applies this to the active tool; we
                    # only need to know the preview must be rebuilt.
                    it.update_plan = True
                if (cx, cy) != self.target:
                    self.target = (cx, cy)
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


def _trace(a, b):
    """Cells from a to b exclusive of a, 4-connected. For gap-free erase drags."""
    return bresenham(a[0], a[1], b[0], b[1])[1:]
