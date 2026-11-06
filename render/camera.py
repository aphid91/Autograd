"""Camera: pan/zoom, world<->screen conversion, and the world render pass.

Pan is WASD, zoom is Q/E -- both continuous, driven by held keys scaled by
frame dt (see InputManager). Pan speed is specified in cells/second and
divided by zoom so the apparent on-screen speed stays constant as you zoom.
"""

import numpy as np
import moderngl

from render import shader_loader

# Shader files live in shaders/ and are resolved relative to the package, not
# the cwd, so the app and hot-reload work from any directory.
VERT_NAME = "world.vert"
FRAG_NAME = "world.frag"

MIN_ZOOM = 2.0      # pixels per cell
MAX_ZOOM = 160.0
DEFAULT_ZOOM = 32.0

PAN_CELLS_PER_SEC = 18.0
ZOOM_PER_SEC = 2.2     # held Q/E: zoom *= ZOOM_PER_SEC ** dt
ZOOM_PER_SCROLL = 1.15  # one scroll notch

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
        #: Optional shader features, as preprocessor symbols. Debug overlays
        #: live behind #ifdef rather than a uniform so they are not compiled
        #: at all when off -- see shader_loader.load().
        self.defines = set()
        self.reload_shader()

    # --- shader ---------------------------------------------------------
    def set_define(self, name, enabled):
        """Turn an optional shader feature on or off, recompiling if it moved.

        Returns True if a recompile happened. Toggling a define means a new
        program: that is the point -- the disabled build does not contain the
        feature's code at all.
        """
        had = name in self.defines
        if had == bool(enabled):
            return False
        if enabled:
            self.defines.add(name)
        else:
            self.defines.discard(name)
        self.reload_shader()
        return True

    def reload_shader(self):
        """Recompile the world shaders. Keeps the old program on failure."""
        try:
            vert_src = shader_loader.load(VERT_NAME, self.defines)
            frag_src = shader_loader.load(FRAG_NAME, self.defines)
        except shader_loader.ShaderError as e:
            self.compile_error = str(e)
            return False

        try:
            prog = self.ctx.program(
                vertex_shader=vert_src, fragment_shader=frag_src
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
        self._apply_zoom(factor)

    def zoom_at(self, steps, screen_pos, viewport):
        """Zoom by discrete scroll steps, anchored at a screen position.

        The world point under the cursor stays put, so scrolling feels like
        pulling the map toward the pointer rather than toward the screen
        centre. Compute what the cursor is over, change the zoom, then shift
        the centre by however much that point moved.
        """
        if not steps:
            return
        before = self.screen_to_world(screen_pos[0], screen_pos[1], viewport)
        self._apply_zoom(ZOOM_PER_SCROLL ** steps)
        after = self.screen_to_world(screen_pos[0], screen_pos[1], viewport)
        self.center[0] += before[0] - after[0]
        self.center[1] += before[1] - after[1]
        self._clamp_center()

    def _apply_zoom(self, factor):
        self.zoom = float(np.clip(self.zoom * factor, MIN_ZOOM, MAX_ZOOM))

    def pan_pixels(self, dx_px, dy_px):
        """Drag the world by a screen-space delta, in framebuffer pixels.

        Used by click-drag panning: the world point under the cursor tracks
        the cursor exactly, so the map feels grabbed rather than nudged.
        Screen y is top-down and world y is up, hence the sign flip.
        """
        if dx_px == 0.0 and dy_px == 0.0:
            return
        self.center[0] -= dx_px / self.zoom
        self.center[1] += dy_px / self.zoom
        self._clamp_center()

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
    def render(self, viewport, clock=None):
        """Draw the world.

        `clock` supplies the simulation time the shader interpolates item
        motion against -- `tick + alpha`, so items move smoothly between
        ticks instead of snapping between slots. Optional so the camera can
        still be driven with no simulation at all.
        """
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
        # Fractional tick time. Split into whole and fraction rather than sent
        # as one float: the tick counter grows without bound and a float32
        # loses sub-tick precision past ~16.7M ticks, which at 10/s is under
        # three weeks of runtime. The shader adds them where it needs to.
        if clock is not None:
            self._set("sim_tick", int(clock.tick))
            self._set("sim_alpha", float(clock.alpha))
        else:
            self._set("sim_tick", 0)
            self._set("sim_alpha", 0.0)

        self.vao.render(moderngl.TRIANGLE_STRIP)

    def _set(self, name, value):
        """Set a uniform only if the shader declares (and uses) it."""
        if self.program is not None and name in self.program:
            self.program[name].value = value
