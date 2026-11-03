"""InputManager: raw glfw/imgui input -> game intents.

This is the only place that reads the keyboard/mouse or consults imgui's
want_capture_* flags, so there is exactly one answer to "is the UI eating this
input?". It owns the belt-planning state machine but no board state: it tells
the orchestrator what the player wants and the engine decides what happens.

State machine:

  IDLE      left-press on world   -> PLANNING (anchor = cell under cursor)
            right-press on world  -> ERASING
  PLANNING  mouse move            -> re-preview the path
            R                     -> cycle path mode, re-preview
            left-press            -> commit, -> IDLE
            right-press / Esc     -> cancel, -> IDLE
  ERASING   drag                  -> erase under cursor
            right-release         -> IDLE

Right-press is consumed by the cancel when PLANNING, so a cancel never also
starts an erase -- the erase gesture needs a fresh press.
"""

import glfw
from imgui_bundle import imgui

from engine import MODE_COUNT, bresenham

STATE_IDLE = 0
STATE_PLANNING = 1
STATE_ERASING = 2

STATE_NAMES = {STATE_IDLE: "Idle", STATE_PLANNING: "Planning", STATE_ERASING: "Erasing"}

# Keys that are edge-triggered (act once per press) rather than held.
_EDGE_KEYS = (glfw.KEY_R, glfw.KEY_ESCAPE, glfw.KEY_F, glfw.KEY_F5, glfw.KEY_DELETE)


class Intents:
    """What the player asked for this frame. Plain data, consumed once."""

    __slots__ = (
        "pan_x", "pan_y", "zoom", "cycle_mode", "frame_board",
        "reload_shader", "clear_board",
        "begin_plan", "update_plan", "commit_plan", "cancel_plan",
        "erase_at", "cursor_cell", "cursor_in_world",
    )

    def __init__(self):
        self.pan_x = 0.0
        self.pan_y = 0.0
        self.zoom = 0.0
        self.cycle_mode = False
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


class InputManager:
    def __init__(self, window, camera):
        self.window = window
        self.camera = camera

        self.state = STATE_IDLE
        self.anchor = None          # planning start cell
        self.target = None          # planning end cell (cursor)
        self.path_mode = 0

        self._prev_keys = {k: False for k in _EDGE_KEYS}
        self._prev_left = False
        self._prev_right = False
        self._last_erase_cell = None

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

    def _key(self, k):
        return glfw.get_key(self.window, k) == glfw.PRESS

    def _edge(self, k, allowed):
        """True on the frame a key goes down. Tracks state even when blocked."""
        now = self._key(k)
        fired = now and not self._prev_keys[k] and allowed
        self._prev_keys[k] = now
        return fired

    # --- main poll ------------------------------------------------------
    def poll(self, dt, board):
        """Read input and return the frame's Intents."""
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
            if left_press and board.in_bounds(cx, cy):
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
                    self.path_mode = (self.path_mode + 1) % MODE_COUNT
                    it.cycle_mode = True
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

        # R outside planning still cycles the mode, so the player can pick a
        # shape before starting a drag.
        if cycle and self.state != STATE_PLANNING:
            self.path_mode = (self.path_mode + 1) % MODE_COUNT
            it.cycle_mode = True

        return it

    def cancel(self):
        """Force the state machine back to idle (used when the UI takes over)."""
        self.state = STATE_IDLE
        self.anchor = self.target = None
        self._last_erase_cell = None


def _trace(a, b):
    """Cells from a to b exclusive of a, 4-connected. For gap-free erase drags."""
    return bresenham(a[0], a[1], b[0], b[1])[1:]
