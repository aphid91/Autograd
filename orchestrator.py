"""Orchestrator: owns the engine, camera, input manager, and UI, and wires them.

Per-frame order matters:

  1. poll input  -> intents
  2. apply intents to the camera and engine (the only mutation point)
  3. build the UI frame, apply its requests
  4. advance the simulation clock
  5. sync dirty state to the device
  6. render the world, then the UI on top

Input is polled before the UI is built, so the UI shows the state the player
just produced rather than lagging a frame behind. The device sync happens after
all mutation and before rendering, so what is drawn is always what the CPU
believes -- including the ghost preview, which changes every frame the cursor
moves and therefore exercises the partial-upload path continuously.

The clock advances *after* player mutation and *before* the device sync, which
puts it in the one window where both hold: a tick sees the board the player
just edited, and its effects reach the GPU in the same frame rather than being
displayed a frame late.
"""

import os

import glfw

from core import persistence
from core.builders import default_builders
from render.camera import Camera
from engine import Engine
from ui.input_manager import InputManager, STATE_PLANNING
from core.toolbelt import Toolbelt
from ui.panel import UI, UIRequests


class Orchestrator:
    def __init__(self, window, ctx, board_w=512, board_h=512):
        self.window = window
        self.ctx = ctx

        self.engine = Engine(board_w, board_h)
        self.engine.create_device_resources(ctx)

        self.camera = Camera(ctx, self.engine.board)
        self.input = InputManager(window, self.camera)
        self.toolbelt = Toolbelt(default_builders())
        self.ui = UI()
        self.last_plan = None

        self.cursor_cell = None
        #: Segments rebuilt this frame, or None if the index was already
        #: current. Shown in the inspection panel.
        self.last_rebuild = None
        # Headless test harnesses set this to skip the imgui panel, which needs
        # a real backend (font atlas, texture upload) to build a frame.
        self.draw_ui = True
        self.camera.frame_board(glfw.get_framebuffer_size(window))

    # --- tool plumbing --------------------------------------------------
    def _on_tool_changed(self):
        """Switching tools abandons any in-flight gesture and its preview."""
        self.engine.clear_ghosts()
        self.input.cancel()
        self.last_plan = None
        self._reset_tool_state()

    def _reset_tool_state(self):
        """Drop a tool's multi-step state after an abandoned gesture.

        An underground part-way through a pair would otherwise still be
        expecting to lay an exit when you come back to it, which is a
        confusing thing to return to.
        """
        for slot in self.toolbelt.slots:
            if slot is not None and hasattr(slot, "reset"):
                slot.reset()

    # --- quicksave -------------------------------------------------------
    def _quicksave(self):
        """Save, returning (ok, message) for the panel to show."""
        try:
            path = self.engine.save_board()
        except (persistence.SaveError, OSError) as exc:
            return (False, f"Save failed: {exc}")
        self.last_plan = None
        return (True, f"Saved {self.engine.board.live_count} entities "
                      f"to {path}")

    def _quickload(self, quiet=False):
        """Load, returning (ok, message). `quiet` suppresses "no save yet",
        which is the normal case on a first run rather than an error."""
        path = persistence.DEFAULT_SAVE
        if not os.path.exists(path):
            return None if quiet else (False, f"No save at {path}")
        try:
            n = self.engine.load_board(path)
        except (persistence.SaveError, OSError) as exc:
            return (False, f"Load failed: {exc}")
        # The loaded board has nothing to do with the in-flight gesture.
        self.input.cancel()
        self.last_plan = None
        self._reset_tool_state()
        return (True, f"Loaded {n} entities from {path}")

    def autoload(self):
        """Quickload at startup if a save exists. Returns (ok, msg) or None."""
        result = self._quickload(quiet=True)
        if result is not None:
            self.ui.last_save = result
        return result

    def _preview(self, tool):
        """Rebuild the preview for the active tool's current gesture."""
        anchor = self.input.anchor
        target = self.input.target
        if anchor is None or target is None:
            return
        # Engine.preview() clears the old preview before planning, so the tool
        # never sees its own ghosts as obstacles. The sub-cell positions let a
        # belt aim its first and last segment from where the player pointed.
        self.last_plan = self.engine.preview(
            tool, anchor, target,
            self.input.anchor_frac, self.input.target_frac,
        )

    # --- frame ----------------------------------------------------------
    def frame(self, dt):
        viewport = glfw.get_framebuffer_size(self.window)
        engine = self.engine
        board = engine.board

        # 1. input. The active tool decides the gesture style, so it has to be
        # known before polling.
        tool_now = self.toolbelt.active
        intents = self.input.poll(
            dt, board, drag_based=(tool_now.drag_based if tool_now else True)
        )
        self.cursor_cell = intents.cursor_cell

        # 2. camera
        self.camera.pan(intents.pan_x, intents.pan_y, dt)
        self.camera.zoom_by(intents.zoom, dt)
        if intents.scroll_steps and intents.scroll_at is not None:
            self.camera.zoom_at(
                intents.scroll_steps, intents.scroll_at, viewport
            )
        dpx, dpy = intents.drag_pan_px
        if dpx or dpy:
            self.camera.pan_pixels(dpx, dpy)
        if intents.frame_board:
            self.camera.frame_board(viewport)
        if intents.reload_shader:
            self.camera.reload_shader()
        if intents.clear_board:
            engine.clear_board()
        if intents.toggle_pause:
            engine.clock.toggle_pause()
        if intents.step_tick:
            engine.clock.request_step()

        # 3. toolbelt selection. Changing tools drops any in-flight preview --
        # a half-drawn belt run means nothing to the structure tool.
        if intents.select_slot is not None:
            if self.toolbelt.select(intents.select_slot):
                self._on_tool_changed()

        tool = self.toolbelt.active

        # 4. per-tool R / shift+R and F
        if intents.cycle_tool and tool is not None:
            tool.cycle(backward=intents.cycle_backward)
        if intents.flip_tool and tool is not None:
            tool.flip()

        # 5. build / erase
        if intents.cancel_plan:
            engine.clear_ghosts()
            self.last_plan = None
            if tool is not None and hasattr(tool, "reset"):
                tool.reset()

        if intents.commit_plan:
            # A click-based tool has no PLANNING phase, so on the very first
            # frame the cursor is over the board there may be no preview yet.
            # Only build one if it is genuinely missing -- rebuilding an
            # existing preview here would re-plan against a board that still
            # contains our own ghosts.
            if tool is not None and self.last_plan is None:
                self._preview(tool)

            # An all-or-nothing plan that is frustrated must not commit, and
            # must keep showing why: leave the tint in place.
            plan = self.last_plan
            blocked = plan is not None and plan.all_or_nothing and not plan.ok
            if not blocked:
                engine.commit_ghosts()
                self.last_plan = None
                # Tools with multi-step state advance it here -- an
                # underground alternates entrance/exit -- and only on a
                # placement that actually happened.
                if tool is not None and hasattr(tool, "committed"):
                    tool.committed()
                # Re-preview so a click-based tool still shows a ghost under
                # the cursor after placing, rather than blinking empty until
                # the mouse moves. Now that the entity is real, this correctly
                # reports the cells as frustrated.
                if tool is not None and not tool.drag_based:
                    self._preview(tool)
        elif intents.update_plan and tool is not None:
            self._preview(tool)

        for (x, y) in intents.erase_at:
            engine.erase(x, y)

        # 6. UI (built mid-frame; drawn last)
        requests = self.ui.draw(self) if self.draw_ui else UIRequests()
        if requests.reload_shader:
            self.camera.reload_shader()
        if requests.frame_board:
            self.camera.frame_board(viewport)
        if requests.clear_board:
            engine.clear_board()
        if requests.select_slot is not None:
            if self.toolbelt.select(requests.select_slot):
                self._on_tool_changed()
        if requests.cycle_tool and self.toolbelt.active is not None:
            self.toolbelt.active.cycle(backward=requests.cycle_backward)
            if self.input.anchor is not None:
                self._preview(self.toolbelt.active)
        if requests.flip_tool and self.toolbelt.active is not None:
            self.toolbelt.active.flip()
            if self.input.anchor is not None:
                self._preview(self.toolbelt.active)
        if requests.verify_round_trip:
            self.ui.last_verify = board.verify_round_trip()
        if requests.quicksave:
            self.ui.last_save = self._quicksave()
        if requests.quickload:
            self.ui.last_save = self._quickload()
        if requests.toggle_pause:
            engine.clock.toggle_pause()
        if requests.step_tick:
            engine.clock.request_step()
        if requests.tick_rate is not None:
            engine.clock.set_rate(requests.tick_rate)
        if requests.segment_debug is not None:
            self.camera.set_define("SEGMENT_DEBUG", requests.segment_debug)
        if requests.rebuild_segments:
            engine.invalidate_segments()
        if requests.audit_segments:
            problems = engine.segments.audit()
            self.ui.last_audit = (len(problems), problems[:6])

        # 7. simulation. After every player mutation, so a tick acts on the
        # board as just edited; before the device sync, so its effects are
        # drawn this frame rather than next.
        #
        # Segments are re-derived first: the tick runs on segments, so they
        # must match the board the player just edited. Once per frame rather
        # than once per edit -- an erase drag touches many cells and only the
        # final shape matters.
        self.last_rebuild = engine.sync_segments()
        engine.tick(dt)

        # 8. device sync, then render
        self.ui.last_upload = engine.sync_to_device()
        self.camera.render(viewport, engine.clock)

    def seed_demo(self):
        """A few belts so the board is not empty on first run."""
        from engine import MODE_HORIZONTAL_FIRST, MODE_VERTICAL_FIRST
        e = self.engine
        cx, cy = e.board.width // 2, e.board.height // 2
        e.place_belt_path((cx - 12, cy), (cx + 4, cy + 6), MODE_HORIZONTAL_FIRST)
        e.place_belt_path((cx - 12, cy - 8), (cx + 4, cy - 2), MODE_VERTICAL_FIRST)
