"""Orchestrator: owns the engine, camera, input manager, and UI, and wires them.

Per-frame order matters:

  1. poll input  -> intents
  2. apply intents to the camera and engine (the only mutation point)
  3. build the UI frame, apply its requests
  4. sync dirty state to the device
  5. render the world, then the UI on top

Input is polled before the UI is built, so the UI shows the state the player
just produced rather than lagging a frame behind. The device sync happens after
all mutation and before rendering, so what is drawn is always what the CPU
believes -- including the ghost preview, which changes every frame the cursor
moves and therefore exercises the partial-upload path continuously.
"""

import glfw

from camera import Camera
from engine import Engine
from input_manager import InputManager, STATE_PLANNING
from ui import UI, UIRequests


class Orchestrator:
    def __init__(self, window, ctx, board_w=512, board_h=512):
        self.window = window
        self.ctx = ctx

        self.engine = Engine(board_w, board_h)
        self.engine.create_device_resources(ctx)

        self.camera = Camera(ctx, self.engine.board)
        self.input = InputManager(window, self.camera)
        self.ui = UI()

        self.cursor_cell = None
        # Headless test harnesses set this to skip the imgui panel, which needs
        # a real backend (font atlas, texture upload) to build a frame.
        self.draw_ui = True
        self.camera.frame_board(glfw.get_framebuffer_size(window))

    # --- frame ----------------------------------------------------------
    def frame(self, dt):
        viewport = glfw.get_framebuffer_size(self.window)
        engine = self.engine
        board = engine.board

        # 1. input
        intents = self.input.poll(dt, board)
        self.cursor_cell = intents.cursor_cell

        # 2. camera
        self.camera.pan(intents.pan_x, intents.pan_y, dt)
        self.camera.zoom_by(intents.zoom, dt)
        if intents.frame_board:
            self.camera.frame_board(viewport)
        if intents.reload_shader:
            self.camera.reload_shader()
        if intents.clear_board:
            engine.clear_board()

        # 3. build / erase
        if intents.cancel_plan:
            engine.clear_ghosts()

        if intents.commit_plan:
            engine.commit_ghosts()

        if intents.update_plan and self.input.state == STATE_PLANNING:
            anchor, target = self.input.anchor, self.input.target
            if anchor is not None and target is not None:
                engine.preview_belt_path(anchor, target, self.input.path_mode)

        for (x, y) in intents.erase_at:
            engine.erase(x, y)

        # 4. UI (built mid-frame; drawn last)
        requests = self.ui.draw(self) if self.draw_ui else UIRequests()
        if requests.reload_shader:
            self.camera.reload_shader()
        if requests.frame_board:
            self.camera.frame_board(viewport)
        if requests.clear_board:
            engine.clear_board()
        if requests.set_mode is not None:
            self.input.path_mode = requests.set_mode
            if self.input.state == STATE_PLANNING and self.input.anchor:
                engine.preview_belt_path(
                    self.input.anchor, self.input.target, self.input.path_mode
                )
        if requests.verify_round_trip:
            self.ui.last_verify = board.verify_round_trip()

        # 5. device sync, then render
        self.ui.last_upload = engine.sync_to_device()
        self.camera.render(viewport)

    def seed_demo(self):
        """A few belts so the board is not empty on first run."""
        from engine import MODE_HORIZONTAL_FIRST, MODE_VERTICAL_FIRST
        e = self.engine
        cx, cy = e.board.width // 2, e.board.height // 2
        e.place_belt_path((cx - 12, cy), (cx + 4, cy + 6), MODE_HORIZONTAL_FIRST)
        e.place_belt_path((cx - 12, cy - 8), (cx + 4, cy - 2), MODE_VERTICAL_FIRST)
