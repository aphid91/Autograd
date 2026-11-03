"""Camera: pan/zoom, world<->screen conversion, and the world render pass.

Pan is WASD, zoom is Q/E -- both continuous, driven by held keys scaled by
frame dt (see InputManager). Pan speed is specified in cells/second and
divided by zoom so the apparent on-screen speed stays constant as you zoom.
"""

import os

import numpy as np
import moderngl

WORLD_VERT = """
#version 430
in vec2 in_pos;
void main() { gl_Position = vec4(in_pos, 0.0, 1.0); }
"""

# Resolved against this file rather than the cwd, so the app runs from
# anywhere -- and so hot-reload keeps working after a chdir.
FRAG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "world.frag")

MIN_ZOOM = 2.0      # pixels per cell
MAX_ZOOM = 160.0
DEFAULT_ZOOM = 32.0

PAN_CELLS_PER_SEC = 18.0
ZOOM_PER_SEC = 2.2  # multiplicative: zoom *= ZOOM_PER_SEC ** dt

GRID_TEXTURE_UNIT = 0
ENTITY_SSBO_BINDING = 0


class Camera:
    def __init__(self, ctx, board):
        self.ctx = ctx
        self.board = board

        # Start centred on the board.
        self.center = np.array(
            [board.width * 0.5, board.height * 0.5], dtype=np.float64
        )
        self.zoom = DEFAULT_ZOOM

        self.vbo = ctx.buffer(np.array([-1, -1, 1, -1, -1, 1, 1, 1], dtype="f4"))
        self.program = None
        self.vao = None
        self.compile_error = None
        self.reload_shader()

    # --- shader ---------------------------------------------------------
    def reload_shader(self):
        """Recompile world.frag. Keeps the old program on failure."""
        try:
            with open(FRAG_PATH) as f:
                frag_src = f.read()
        except FileNotFoundError:
            self.compile_error = f"{FRAG_PATH} not found"
            return False

        try:
            prog = self.ctx.program(
                vertex_shader=WORLD_VERT, fragment_shader=frag_src
            )
        except Exception as e:
            self.compile_error = f"Shader compile error:\n{e}"
            return False

        if self.vao is not None:
            self.vao.release()
        if self.program is not None:
            self.program.release()
        self.program = prog
        self.vao = self.ctx.vertex_array(prog, [(self.vbo, "2f", "in_pos")])
        self.compile_error = None
        return True

    # --- movement -------------------------------------------------------
    def pan(self, dx, dy, dt):
        """Pan by a normalised direction, scaled by dt and inverse zoom."""
        if dx == 0.0 and dy == 0.0:
            return
        # Normalise so diagonal panning is not faster than axis-aligned.
        mag = (dx * dx + dy * dy) ** 0.5
        speed = PAN_CELLS_PER_SEC * dt * (DEFAULT_ZOOM / self.zoom)
        self.center += np.array([dx / mag, dy / mag]) * speed
        self._clamp_center()

    def zoom_by(self, direction, dt):
        """direction > 0 zooms in, < 0 zooms out. Centred on the view centre."""
        if direction == 0:
            return
        factor = ZOOM_PER_SEC ** (dt * direction)
        self.zoom = float(np.clip(self.zoom * factor, MIN_ZOOM, MAX_ZOOM))

    def _clamp_center(self):
        """Keep the view centre within a margin of the board."""
        margin = 8.0
        self.center[0] = np.clip(self.center[0], -margin, self.board.width + margin)
        self.center[1] = np.clip(self.center[1], -margin, self.board.height + margin)

    def frame_board(self, viewport):
        """Set zoom/centre so the whole board is visible."""
        w, h = viewport
        self.center[:] = (self.board.width * 0.5, self.board.height * 0.5)
        if self.board.width > 0 and self.board.height > 0:
            self.zoom = float(np.clip(
                min(w / self.board.width, h / self.board.height),
                MIN_ZOOM, MAX_ZOOM,
            ))

    # --- coordinate conversion ------------------------------------------
    def screen_to_world(self, sx, sy, viewport):
        """Mouse position (px, y-down from top-left) -> world cell coords.

        Returns floats; floor() them for a cell index. Mirrors the mapping in
        world.frag exactly, so what the cursor points at is what gets drawn.
        """
        w, h = viewport
        # glfw cursor y is top-down; the shader works in bottom-up framebuffer
        # space, so flip before converting.
        fx = sx - w * 0.5
        fy = (h - sy) - h * 0.5
        return (
            self.center[0] + fx / self.zoom,
            self.center[1] + fy / self.zoom,
        )

    def screen_to_cell(self, sx, sy, viewport):
        wx, wy = self.screen_to_world(sx, sy, viewport)
        import math
        return math.floor(wx), math.floor(wy)

    def world_to_screen(self, wx, wy, viewport):
        w, h = viewport
        sx = (wx - self.center[0]) * self.zoom + w * 0.5
        sy = h - ((wy - self.center[1]) * self.zoom + h * 0.5)
        return sx, sy

    def visible_cell_rect(self, viewport):
        """(x0, y0, x1, y1) half-open cell range currently on screen."""
        import math
        w, h = viewport
        x0, y0 = self.screen_to_world(0, h, viewport)
        x1, y1 = self.screen_to_world(w, 0, viewport)
        return (
            math.floor(x0), math.floor(y0),
            math.ceil(x1) + 1, math.ceil(y1) + 1,
        )

    # --- render ---------------------------------------------------------
    def render(self, viewport):
        if self.program is None or self.vao is None:
            return

        w, h = viewport
        board = self.board

        if board.grid_texture is not None:
            board.grid_texture.use(GRID_TEXTURE_UNIT)
        if board.entity_buffer is not None:
            board.entity_buffer.bind_to_storage_buffer(ENTITY_SSBO_BINDING)

        self._set("resolution", (float(w), float(h)))
        self._set("cam_center", (float(self.center[0]), float(self.center[1])))
        self._set("cam_zoom", float(self.zoom))
        self._set("board_size", (int(board.width), int(board.height)))
        self._set("grid", GRID_TEXTURE_UNIT)

        self.vao.render(moderngl.TRIANGLE_STRIP)

    def _set(self, name, value):
        """Set a uniform only if the shader declares (and uses) it."""
        if self.program is not None and name in self.program:
            self.program[name].value = value
