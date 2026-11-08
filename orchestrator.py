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

from core import config, persistence
from core.builders import PasteBuilder, default_builders
from core.clipboard import Clipboard, MODE_COPY, MODE_PASTE, capture
from core.entity import Source
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
        self.clipboard = Clipboard()
        self.ui = UI()
        self.last_plan = None
        #: The paste tool, rebuilt whenever the active stamp or the shift
        #: modifier changes. Not a toolbelt slot -- paste mode overrides
        #: whatever the hand holds.
        self.paste_tool = None
        #: Live selection rectangle in cells, or None. Drawn by the camera.
        self.select_rect = None

        self.cursor_cell = None
        #: Entity row the Status window is inspecting, or None when closed.
        #:
        #: A **row**, not a view: views must never be cached across frames,
        #: since a row can be freed and reused underneath one. The panel
        #: rebuilds a view each frame from this and checks it is still alive,
        #: so erasing the inspected entity closes the window rather than
        #: reading a recycled row.
        self.inspect_row = None
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

    # --- copy / paste modes ----------------------------------------------
    def _active_tool(self):
        """The builder in charge this frame.

        Paste mode **overrides** the toolbelt rather than occupying a slot,
        so everything downstream -- the gesture style, preview, commit --
        goes through one accessor and needs no mode branch of its own.
        """
        if self.clipboard.pasting:
            return self.paste_tool
        return self.toolbelt.active

    def _enter_copy(self):
        """ctrl+C: stash the held tool and start selecting."""
        self.clipboard.enter_copy(self._stashable_slot())
        self._on_tool_changed()
        self.select_rect = None

    def _enter_paste(self):
        """ctrl+V: stash the held tool and hold the active stamp.

        Does nothing if nothing has been copied yet -- there is no empty
        paste mode to be in.
        """
        if not self.clipboard.enter_paste(self._stashable_slot()):
            return False
        self._on_tool_changed()
        self._refresh_paste_tool()
        return True

    def _stashable_slot(self):
        """The slot to return to, or None for an empty hand.

        None is a real answer here, not a failure: leaving a mode must give
        back an empty hand if that is what the player had, rather than
        collapsing to slot 0.
        """
        if self.toolbelt.empty_handed:
            return None
        return self.toolbelt.selected

    def _end_mode(self, to_empty=False):
        """Leave copy or paste mode.

        `to_empty` is Q's behaviour -- drop the stash and hold nothing. Every
        other exit (right-press, a plain click, Esc) restores the tool the
        player was using, so a mode entered mid-gesture gives the belt back.
        """
        if not self.clipboard.active:
            return
        if to_empty:
            self.clipboard.exit_to_empty()
            self.toolbelt.clear()
        else:
            slot, had = self.clipboard.exit_to_stash()
            if had:
                if slot is None:
                    self.toolbelt.clear()
                else:
                    self.toolbelt.select(slot)
        self.paste_tool = None
        self.select_rect = None
        self._on_tool_changed()

    def _capture_selection(self, rect):
        """Turn a released selection rectangle into a stamp.

        An empty selection returns the player to their tool -- there is
        nothing to paste, so dropping them into paste mode would be a dead
        end. Anything caught becomes the active stamp and we go straight to
        paste mode, which is what makes copy-then-place one gesture.
        """
        self.select_rect = None
        stamp = capture(self.engine.board, *rect) if rect else None
        if stamp is None or not stamp:
            self._end_mode(to_empty=False)
            return None
        self.clipboard.adopt(stamp)
        self._on_tool_changed()
        self._refresh_paste_tool()
        return stamp

    def _refresh_paste_tool(self, partial=None):
        """Rebuild the paste tool for the active stamp.

        Cheap and called whenever the stamp or the shift modifier changes: a
        PasteBuilder is a thin wrapper around a stamp, and rebuilding keeps
        the "all-or-nothing is per click" rule honest without the tool
        holding mutable policy.
        """
        if partial is None:
            partial = self.paste_tool.partial if self.paste_tool else False
        self.paste_tool = PasteBuilder(self.clipboard.stamp, partial=partial)
        self.last_plan = None
        return self.paste_tool

    def _pipette(self, cursor_cell, in_world):
        """Q: fill an empty hand from the board, or empty a full one.

        Factorio's context key. The asymmetry is deliberate: with something
        held, Q always clears -- it is the universal "put this down", so it
        must not depend on what happens to be under the cursor. Only an empty
        hand looks at the board.

        Picking up copies the *orientation* as well as the type, via the
        builder's adopt(), so the clone comes out identical rather than at
        whatever rotation the tool was last left in.
        """
        if not self.toolbelt.empty_handed:
            # Holding something: put it down. Cancels any in-flight plan.
            self.toolbelt.clear()
            self._on_tool_changed()
            return

        if not in_world or cursor_cell is None:
            return
        entity = self.engine.entity_at(*cursor_cell)
        if entity is None:
            return
        tool = self.toolbelt.select_tool_for(type(entity))
        if tool is None:
            # A type with no builder -- nothing to pick it up with. Leave the
            # hand empty rather than selecting an unrelated slot.
            return
        self._on_tool_changed()
        tool.adopt(entity)

    def _inspect_at(self, cursor_cell):
        """Open the Status window on the entity at `cursor_cell`, or close it.

        The empty hand's one gesture. Clicking a structure inspects it;
        clicking bare board closes the window, which makes "click away to
        dismiss" work without the player hunting for the close button.

        Stores the **row** rather than the view, because a view must not
        outlive the frame it was made in -- see `inspect_row`.

        Ghosts are skipped for the same reason the pipette skips them: a
        preview ghost can sit under the cursor, and inspecting it would open
        a window on an entity that is about to be despawned. An empty hand
        has no preview of its own, so this only matters on the frame a tool
        was just put down.
        """
        if cursor_cell is None:
            self.inspect_row = None
            return
        entity = self.engine.entity_at(*cursor_cell)
        self.inspect_row = None if entity is None else entity.row

    def _set_emit(self, row, value):
        """Change a Source's emitted constant and flush material in flight.

        The flush is the point, and it is the same bargain editing the belt
        network already makes: an item on a belt is a payload and an entry
        tick with no record of which machine produced it, so there is no way
        to retire just the stale ones. Without it the old value keeps
        arriving for as long as the longest run takes to drain, mixed in with
        the new one and looking like the setting did not take.

        Guarded on the row still being a live Source, because a request is
        built from the previous frame's state -- the entity could have been
        erased between the panel drawing and this running.
        """
        board = self.engine.board
        if not board.is_row_alive(row):
            return
        view = board.view(row)
        if not isinstance(view, Source):
            return
        if view.emit_value == value:
            return
        view.emit_value = value
        self.engine.flush_material()

    def inspected(self):
        """A fresh view of the inspected entity, or None.

        Built per call rather than held, and `None` the moment the row dies,
        so erasing or overwriting the inspected entity closes the window
        instead of reading a row the free list has handed to something else.
        """
        row = self.inspect_row
        if row is None or not self.engine.board.is_row_alive(row):
            return None
        return self.engine.board.view(row)

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
    def _save_path(self):
        """Where the save buttons point, honouring the panel's filename box.

        An empty box means the default quicksave, which is what keeps the
        buttons working with nothing typed. Read from `self.ui` rather than
        held here so there is one copy of the value.
        """
        return config.resolve_save_path(
            getattr(self.ui, "save_name", ""), persistence.DEFAULT_SAVE)

    def _quicksave(self):
        """Save, returning (ok, message) for the panel to show."""
        try:
            path = self.engine.save_board(self._save_path())
        except (persistence.SaveError, OSError) as exc:
            return (False, f"Save failed: {exc}")
        self.last_plan = None
        return (True, f"Saved {self.engine.board.live_count} entities "
                      f"to {path}")

    def _quickload(self, quiet=False):
        """Load, returning (ok, message). `quiet` suppresses "no save yet",
        which is the normal case on a first run rather than an error."""
        path = self._save_path()
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
        # known before polling -- and in paste mode that is the paste tool,
        # not whatever the toolbelt holds.
        #
        # An empty hand is **click-based**. It has no plan to build up, so a
        # drag-based gesture would park it in STATE_PLANNING on the first
        # click and only commit on the second -- which for the inspect
        # gesture means the Status window needing two clicks to open. The
        # old `True` default was harmless only because an empty hand had no
        # gesture at all.
        tool_now = self._active_tool()
        intents = self.input.poll(
            dt, board,
            drag_based=(tool_now.drag_based if tool_now else False),
            mode=self.clipboard.mode,
        )
        self.cursor_cell = intents.cursor_cell

        # 2. camera
        self.camera.pan(intents.pan_x, intents.pan_y, dt)
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

        # Q: pick up or put down. In copy or paste mode it means neither --
        # it leaves the mode with an *empty* hand, dropping the stashed tool
        # rather than returning to it.
        if intents.pipette:
            if self.clipboard.active:
                self._end_mode(to_empty=True)
            else:
                self._pipette(intents.cursor_cell, intents.cursor_in_world)

        # ctrl+C / ctrl+V, and the ways back out.
        if intents.copy_mode:
            self._enter_copy()
        elif intents.paste_mode:
            self._enter_paste()
        if intents.exit_mode:
            self._end_mode(to_empty=False)

        # shift+scroll in paste mode walks the stamp history.
        if intents.stamp_steps:
            step = 1 if intents.stamp_steps > 0 else -1
            if self.clipboard.cycle_stamp(step):
                self._refresh_paste_tool()

        # A completed selection drag becomes a stamp.
        self.select_rect = intents.select_rect
        if intents.commit_select:
            self._capture_selection(intents.select_rect)

        tool = self._active_tool()

        # In paste mode, shift is read per *click* rather than held on the
        # tool: a plain click is all-or-nothing, shift places what fits. The
        # preview must show the policy that is actually armed, so a change to
        # the modifier rebuilds the tool and re-previews.
        if self.clipboard.pasting and self.paste_tool is not None:
            if self.paste_tool.partial != intents.shift_held:
                self._refresh_paste_tool(partial=intents.shift_held)
                tool = self.paste_tool
                intents.update_plan = True

        # 4. per-tool R / shift+R and F. In paste mode these transform the
        # *stamp*, which outlives any one plan, so they go to the clipboard
        # and the tool is rebuilt around the result.
        if self.clipboard.pasting:
            if intents.cycle_tool or intents.flip_tool:
                self.clipboard.transform_active(
                    rotate=(-1 if intents.cycle_backward else 1)
                    if intents.cycle_tool else 0,
                    mirror=intents.flip_tool,
                )
                self._refresh_paste_tool()
                tool = self.paste_tool
                intents.update_plan = True
        else:
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

        if intents.commit_plan and tool is None:
            # Empty hand: a click inspects instead of building. This is the
            # one gesture the empty hand has, and it is why the state is
            # distinct from "an empty slot is selected" -- see the note on
            # NO_SELECTION in CLAUDE.md.
            self._inspect_at(intents.cursor_cell if intents.cursor_in_world
                             else None)
        elif intents.commit_plan:
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
                # A pasted underground pair must join to *itself* rather than
                # to whatever happens to sit near the paste site, so the
                # plan's own pairing goes in first and the engine's
                # nearest-candidate search only handles what is left.
                engine.commit_ghosts(engine.planned_under_pairs(plan))
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
        # The panel's R / F buttons go through the same path as the keys, so
        # they transform the stamp in paste mode rather than reaching past it
        # into the toolbelt.
        if requests.cycle_tool or requests.flip_tool:
            if self.clipboard.pasting:
                self.clipboard.transform_active(
                    rotate=(-1 if requests.cycle_backward else 1)
                    if requests.cycle_tool else 0,
                    mirror=requests.flip_tool,
                )
                self._refresh_paste_tool()
                self._preview(self.paste_tool)
            else:
                active = self.toolbelt.active
                if active is not None:
                    if requests.cycle_tool:
                        active.cycle(backward=requests.cycle_backward)
                    if requests.flip_tool:
                        active.flip()
                    if self.input.anchor is not None:
                        self._preview(active)
        if requests.verify_round_trip:
            self.ui.last_verify = board.verify_round_trip()
        if requests.quicksave:
            self.ui.last_save = self._quicksave()
        if requests.quickload:
            self.ui.last_save = self._quickload()
        if requests.close_status:
            self.inspect_row = None
        if requests.set_emit is not None:
            self._set_emit(*requests.set_emit)
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
            # Both halves: the segmentation against the grid, and the items
            # on it against the transport's invariants. They fail for quite
            # different reasons, so running one without the other hides half
            # the possible trouble.
            problems = (engine.segments.audit()
                        + engine.belts.audit(engine.clock.tick))
            self.ui.last_audit = (len(problems), problems[:6])
        if requests.debug_feed is not None:
            engine.debug_feed = (None if requests.debug_feed is False
                                 else float(requests.debug_feed))
        if requests.clear_items:
            # flush_material(), not clear_items(): the button says "clear
            # items" and the player means all of them, including the ones
            # sitting in a gate's channels where clear_items() cannot see
            # them. Leaving those behind had them reappear on the belts a
            # tick later -- the same bug a network edit had.
            engine.flush_material()

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
        self.camera.render(viewport, engine.clock, engine.belt_device,
                           select_rect=self.select_rect)

    def seed_demo(self):
        """A few belts so the board is not empty on first run."""
        from engine import MODE_HORIZONTAL_FIRST, MODE_VERTICAL_FIRST
        e = self.engine
        cx, cy = e.board.width // 2, e.board.height // 2
        e.place_belt_path((cx - 12, cy), (cx + 4, cy + 6), MODE_HORIZONTAL_FIRST)
        e.place_belt_path((cx - 12, cy - 8), (cx + 4, cy - 2), MODE_VERTICAL_FIRST)
