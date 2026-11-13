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
from core.annotations import AnnotationStore, TextBox
from core.builders import (ANNOTATION_TOOLS, DrawAnnotationTool, PasteBuilder,
                           TextAnnotationTool, default_builders, make_builder)
from core.clipboard import Clipboard, MODE_COPY, MODE_PASTE, capture
from core.entity import Belt, Source
from render.camera import Camera
from engine import Engine
from ui.annotation_layer import AnnotationLayer
from ui.input_manager import (InputManager, NOTE_BOX, NOTE_MOVE, NOTE_NONE,
                              NOTE_RESIZE, NOTE_STROKE, STATE_PLANNING)
from core.toolbelt import Toolbelt
from ui.panel import UI, UIRequests

#: Smallest text box a drag may produce, in world cells. A click that barely
#: moves would otherwise make a box too small to see or click again -- which,
#: since a fresh box is empty, would be invisible litter on the board.
MIN_BOX_W = 1.2
MIN_BOX_H = 0.6


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
        #: Set when the toolbar layout changes, so `app.py` knows to persist
        #: it. The orchestrator does not write files itself -- same discipline
        #: as `UI.save_name_dirty`, and for the same reason (see
        #: core/config.py: a test harness builds a real Orchestrator).
        self.toolbar_dirty = False

        self.cursor_cell = None
        #: Entity row the Status window is inspecting, or None when closed.
        #:
        #: A **row**, not a view: views must never be cached across frames,
        #: since a row can be freed and reused underneath one. The panel
        #: rebuilds a view each frame from this and checks it is still alive,
        #: so erasing the inspected entity closes the window rather than
        #: reading a recycled row.
        self.inspect_row = None
        #: Annotation the Status window is editing, as an **id**, or None.
        #:
        #: Parallel to `inspect_row` and for the same reason, but it cannot
        #: share it: an annotation is not an entity and has no row, so
        #: `is_row_alive()` has nothing to say about one. Ids are never reused
        #: (see core/annotations.py), so a stale id reads as `None` rather
        #: than as a different annotation -- the same safety `inspect_row`
        #: gets from the row liveness check.
        #:
        #: At most one of the two is set; `_inspect_at()` clears the other.
        self.inspect_note = None
        #: Every stroke and label on the board. Not part of the Gameboard:
        #: annotations are notes *about* a factory, carry no simulation state,
        #: and live in continuous coordinates rather than cells.
        self.annotations = AnnotationStore()
        self.annotation_layer = AnnotationLayer()
        #: The stroke currently being drawn, or None. Held here rather than in
        #: the store so an abandoned gesture (Esc, or a tool change) leaves
        #: nothing behind -- the same reason a ghost preview is not a real
        #: entity until it commits.
        self._live_stroke = None
        #: Live text-box rectangle in world coords while dragging, or None.
        self._note_box = None
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

    def _enter_erase(self, cut=False):
        """ctrl+D / ctrl+X: stash the tool and start a rectangle erase.

        Entered the same way as copy mode and sharing its gesture, since the
        only difference is what the release does -- see core/clipboard.py.
        """
        self.clipboard.enter_erase(self._stashable_slot(), cut=cut)
        self._on_tool_changed()
        self.select_rect = None

    def _erase_selection(self, rect):
        """Delete everything a released erase rectangle touched.

        Touching, not contained -- the same rule `capture()` uses, so a
        sloppy drag over a machine takes the whole machine. `Engine.erase()`
        already removes a multi-cell entity from any one of its cells and
        releases an underground's partner, so erasing per cell is both
        correct and idempotent here.

        **The mode survives**, which is the point of erase mode: the
        rectangle is a stroke you repeat, not a commitment that ends it.

        Under `cutting` the rectangle is captured *before* anything is
        deleted and we go to paste mode holding it -- a cut. An empty cut
        catches nothing, so it leaves the player erasing rather than
        stranding them in a paste mode with no stamp.
        """
        self.select_rect = None
        if rect is None:
            return 0

        stamp = None
        if self.clipboard.cutting:
            stamp = capture(self.engine.board, *rect,
                            annotations=self.annotations)

        x0, y0, x1, y1 = rect
        lo_x, hi_x = (x0, x1) if x0 <= x1 else (x1, x0)
        lo_y, hi_y = (y0, y1) if y0 <= y1 else (y1, y0)
        erased = 0
        for y in range(lo_y, hi_y + 1):
            for x in range(lo_x, hi_x + 1):
                if self.engine.board.in_bounds(x, y):
                    erased += int(self.engine.erase(x, y))

        # The rectangle takes annotations too, so `X` is one eraser rather
        # than two the player has to choose between. Only when the layer is
        # on: an invisible note must not be deleted by a rectangle drawn over
        # where it happens to be. The cell rectangle is inclusive of both end
        # cells, so in world terms it spans [lo, hi+1).
        if self.annotation_layer.enabled:
            erased += self.annotations.erase_rect(
                lo_x, lo_y, hi_x + 1, hi_y + 1)
            if self.inspected_note() is None:
                self.inspect_note = None

        if stamp:
            # A cut becomes the active stamp and takes us to paste mode, so
            # cut-then-place is one chain like copy-then-place. The stash
            # survives it: Clipboard._stash() refuses to overwrite, so the
            # tool the player held before the cut still comes back.
            self.clipboard.adopt(stamp)
            self._on_tool_changed()
            self._refresh_paste_tool()
        return erased

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
        """Leave copy, paste or erase mode.

        `to_empty` is Q's behaviour -- drop the stash and hold nothing. Every
        other exit (right-press, a plain click, Esc) restores the tool the
        player was using, so a mode entered mid-gesture gives the belt back.
        Selecting a toolbelt slot is the third case and does not come through
        here at all -- see `_select_slot()`.
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

    def _select_column(self, column):
        """Pick a slot by its *column* in the active row -- what a digit means.

        The number keys and the panel's buttons speak different languages on
        purpose: a digit names a column within whichever row is up, while a
        clicked button already knows its flat index. Resolving the column
        here, against the toolbelt's own `row_start`, keeps the input manager
        from having to know which row is active.
        """
        return self._select_slot(self.toolbelt.row_start + column)

    def _select_slot(self, slot):
        """Pick a toolbelt slot by flat index, leaving any clipboard mode.

        **Picking a tool wins over the stash.** Every other way out of a mode
        restores what the player was holding when they entered it, because
        the mode interrupted them and handing the tool back is the courtesy.
        Choosing a slot is not an interruption ending -- it is a new choice,
        and restoring the stash afterwards would override it with whatever
        was held several actions ago.

        So the stash is *discarded* rather than applied. Without that the
        order of these two operations would decide the outcome, which is
        exactly the kind of coupling the single accessor exists to avoid.
        """
        if self.clipboard.active:
            # exit_to_empty() drops the stash without touching the toolbelt,
            # which is what lets the select() below be the final word.
            self.clipboard.exit_to_empty()
            self.paste_tool = None
            self.select_rect = None
        if self.toolbelt.select(slot):
            self._on_tool_changed()
            return True
        return False

    def _set_slot(self, slot, kind, type_name):
        """Apply the right-click type menu's choice to a slot.

        The panel hands over a *description* and this resolves it, which is
        what keeps builder construction out of the UI -- and is load-bearing
        for the `"stamp"` kind, whose source is the live clipboard the panel
        should not be reaching into.

        Three things worth knowing:

        - **A `None` kind clears the slot.** Distinct from an unknown kind,
          which is a failure; this is the menu's "Clear slot".
        - **The selection is left alone.** Re-assigning the slot you are
          holding should leave you holding that slot with its new tool, so
          `set_slot()` does not touch `selected`. But any in-flight gesture
          belongs to the *old* tool, so it is abandoned when the slot being
          changed is the one in hand.
        - **It marks the layout dirty**, not saved. Writing the file is
          `app.py`'s job -- see core/config.py on why nothing here touches
          the player's config.
        """
        if kind is None:
            tool = None
        else:
            stamp = self.clipboard.stamp if kind == "stamp" else None
            if kind == "stamp" and not stamp:
                # Nothing to bake. The menu only offers this with a stamp in
                # hand, so reaching here means the clipboard changed between
                # the menu being drawn and this running.
                return False
            tool = make_builder(kind, type_name=type_name, stamp=stamp)
            if tool is None:
                return False
        if not self.toolbelt.set_slot(slot, tool):
            return False
        self.toolbar_dirty = True
        if slot == self.toolbelt.selected:
            self._on_tool_changed()
        return True

    def toolbar_data(self):
        """The current layout as plain data, for `app.py` to persist."""
        return config.toolbar_to_data(self.toolbelt.slots)

    def apply_toolbar(self, slots):
        """Install a saved layout. Called by `app.py`, never implicitly.

        `slots` is a flat list across both rows, as `config.toolbar_from_data`
        produces. A `None` argument means "no saved layout" and leaves the
        defaults in place, which is what makes a first run look unchanged.
        """
        if slots is None:
            return False
        for i, tool in enumerate(slots):
            self.toolbelt.set_slot(i, tool)
        # The hand may now hold a slot whose tool changed underneath it.
        self._on_tool_changed()
        return True

    def _dump_profile(self, engine):
        """Print the tick breakdown to the console.

        The orchestrator does this rather than the clock or the panel, for the
        reason the whole chain runs this way: the clock owns *time* and should
        have no opinion about where a report goes, and the panel only
        *requests*. This is the layer that already applies UI requests to the
        engine.

        The board's shape is reported with it, because a timing without it is
        not comparable to anything -- "0.4ms a tick" means one thing at 50
        segments and another at 2000, and the sleeping fraction is the number
        that explains the difference on a board built as a computer.
        """
        prof = engine.clock.profiler
        total = engine.segments.count
        asleep = engine.belts.stats.get("asleep", 0)
        context = (f"{total} segments ({asleep} asleep), "
                   f"{engine.board.live_count} entities")
        prof.dump(context, rate_hz=engine.clock.ticks_per_sec)

    def _substitute(self, tool, plan):
        """Replace lined-up entities with the plan that matched them.

        The point of the whole matched-ghost mechanism. Per-instance settings
        -- a Source's emitted value, a belt's seed -- are reachable only one
        entity at a time through the Status window, so changing twenty Sources
        means twenty trips. With this, the player configures *one*, copies it,
        rotates the stamp until it lines up, and clicks: the old entities go
        and the configured copies take their exact places.

        Returns the plan to commit, or None if substitution does not apply --
        in which case the caller leaves the plan blocked and the red tint
        stands, exactly as before.

        **Erase everything first, then re-plan.** Three reasons, and the order
        is load-bearing:

        - The plan in hand was computed against the *occupied* board, so
          every placement carries `blocked_cells` and `buildable` is empty.
          It cannot be committed as-is; the board has to be cleared and the
          gesture re-planned against the result.
        - Erasing one underground end releases its partner
          (`Engine.unpair_under`), so deleting and re-planning per placement
          would let a half-deleted pair re-pair against a survivor. Clearing
          the whole matched set up front means the re-plan sees no
          half-states.
        - `Engine.erase()` invalidates segments, which is correct and is the
          same cost any edit pays.

        Guarded on the re-plan actually succeeding. If anything still clashes
        the substitution is abandoned -- but the erase has already happened,
        so this reports the new plan rather than the old one and lets the
        caller refuse on current facts.
        """
        if plan is None or tool is None:
            return None
        rows = self.engine.matched_rows(plan)
        if not rows:
            # None  -> something clashes that is not a match.
            # []    -> nothing was blocked, so there is nothing to substitute
            #          and the caller's own `blocked` test already said so.
            return None

        # Drop the preview before erasing. Its ghosts occupy real cells, and
        # the re-plan below must not see them -- the same hazard
        # Engine.preview() exists to avoid.
        #
        # **Defensive rather than load-bearing**, and mutation-testing says
        # so: `_preview()` goes through `Engine.preview()`, which clears the
        # old preview itself before planning, so removing this line changes
        # nothing today. It stays because the erase loop below runs *between*
        # here and that clear, and it should not be reasoning about somebody
        # else's cells while the tool's own ghosts are still on the board --
        # `erase()` takes an entity from any one of its cells, and a ghost
        # sharing a cell with a matched row is a state worth not creating.
        self.engine.clear_ghosts()
        for row in rows:
            if not self.engine.board.is_row_alive(row):
                continue
            ox, oy = self.engine.board.view(row).origin
            self.engine.erase(ox, oy)

        self._preview(tool)
        fresh = self.last_plan
        if fresh is None or (fresh.all_or_nothing and not fresh.ok):
            return None
        return fresh

    def _capture_selection(self, rect):
        """Turn a released selection rectangle into a stamp.

        An empty selection returns the player to their tool -- there is
        nothing to paste, so dropping them into paste mode would be a dead
        end. Anything caught becomes the active stamp and we go straight to
        paste mode, which is what makes copy-then-place one gesture.
        """
        self.select_rect = None
        # Annotations ride along when the layer is on, so copying a labelled
        # block of a CPU reproduces its labels (see `capture`). Passing the
        # store only when the layer is enabled keeps a hidden note from
        # silently joining a stamp.
        notes = self.annotations if self.annotation_layer.enabled else None
        stamp = (capture(self.engine.board, *rect, annotations=notes)
                 if rect else None)
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

    # --- annotations -----------------------------------------------------
    def _note_tool(self):
        """The held annotation tool, or None if anything else is in charge.

        **A clipboard mode wins outright.** `_active_tool()` only substitutes
        the paste tool, because copy and erase mode express themselves through
        the input manager's own selection branch rather than through a tool --
        so asking it alone would still report the stashed annotation tool
        while `X` was running, and the annotation branch (which returns early)
        would eat the selection drag. The symptom was an erase rectangle
        drawing a new stroke instead of deleting one.
        """
        if self.clipboard.active:
            return None
        tool = self._active_tool()
        return tool if isinstance(tool, ANNOTATION_TOOLS) else None

    def _note_gesture(self, cursor_world=None):
        """Which annotation gesture the input manager should run.

        Move and resize come **first**, and are available both with an empty
        hand and with the text tool held. With the tool, a drag over the open
        box used to draw a new box on top of it (or, once the stacking guard
        landed, do nothing at all) -- but the box is right there under the
        cursor with its grip drawn on it, so dragging it is obviously what
        the player means. Editing a label should not require putting the tool
        down, which is the same reasoning that has a click with the text tool
        open an existing box.

        They are gated on the Status window being open on *that* box rather
        than on merely hovering any note, deliberately: with an empty hand a
        left-drag is the camera pan, and with the text tool it is "draw a new
        box" -- silently stealing either wherever a label happens to lie
        would make both unreliable near annotations. Having the window open
        is the player saying which box they are working on.
        """
        tool = self._note_tool()
        if isinstance(tool, DrawAnnotationTool):
            # The pen takes both buttons for drawing and erasing, so there is
            # no spare gesture to give a box. Nothing is lost: the player can
            # still grab the box with an empty hand or the text tool.
            return NOTE_STROKE

        empty = self.toolbelt.empty_handed
        if (cursor_world is not None
                and self.annotation_layer.enabled
                and not self.clipboard.active
                and (empty or isinstance(tool, TextAnnotationTool))):
            note = self.inspected_note()
            # Text boxes only. A stroke has no handle worth grabbing -- its
            # bbox is mostly empty space, so a drag starting "on" a circled
            # region would move it from a point nowhere near its ink.
            if isinstance(note, TextBox):
                # The grip is checked first and is *outside* the box's own
                # hit area at the corner, so grabbing it can never be
                # mistaken for grabbing the body.
                if self._on_resize_handle(note, cursor_world):
                    return NOTE_RESIZE
                if note.hit_point(*cursor_world):
                    return NOTE_MOVE

        if isinstance(tool, TextAnnotationTool):
            return NOTE_BOX
        return NOTE_NONE

    def _on_resize_handle(self, box, world):
        """Whether a world point is on `box`'s resize grip.

        Tested in **screen space**, because the grip is drawn at a fixed
        pixel size (see `AnnotationLayer._draw_handle`) -- a world-space
        radius would be unclickable when zoomed out and cover the whole
        label when zoomed in. The drawn target and the clickable target have
        to be the same shape, so both work in pixels.
        """
        from ui.annotation_layer import HANDLE_PX
        viewport = glfw.get_framebuffer_size(self.window)
        x0, y0, x1, y1 = box.bounds()
        # The grip sits at the box's bottom-right, which in world terms is
        # (x1, y0).
        hx, hy = self.camera.world_to_screen(x1, y0, viewport)
        sx, sy = self.camera.world_to_screen(world[0], world[1], viewport)
        # A square pad around the corner, slightly larger than the drawn
        # mark so the grip is forgiving to hit.
        pad = HANDLE_PX
        return abs(sx - hx) <= pad and abs(sy - hy) <= pad

    def _note_draw(self, point, tool):
        """Extend (or start) the stroke being drawn."""
        if self._live_stroke is None:
            # Built detached from the store and added on release, so an
            # abandoned stroke leaves nothing to clean up.
            from core.annotations import Stroke
            self._live_stroke = Stroke(None, [point], tool.colour, tool.size)
            return
        self._live_stroke.add_point(*point)

    def _note_draw_end(self):
        """Commit the in-flight stroke, if it has anything in it."""
        stroke = self._live_stroke
        self._live_stroke = None
        if stroke is None or not stroke.points:
            return None
        return self.annotations.add(stroke)

    def _note_box_end(self, rect, tool):
        """Turn a released drag rectangle into a text box, and open it.

        Creating one **selects it and opens the Status window**, because a
        fresh box is empty: a label the player cannot find is litter, and the
        window is where the text is typed. That is also why a too-small drag
        is widened to a minimum rather than rejected -- a click that barely
        moved still means "put a label here".

        **A drag that started on an existing text box makes nothing**, and
        opens that box instead. Stacking a fresh empty label on top of one
        already there is almost never the intent -- the two would overlap
        illegibly and the new one would swallow clicks meant for the old --
        so the gesture is reinterpreted as "edit this one". Which makes the
        text tool behave like the empty hand over a label, and means the
        player does not have to put the tool down to fix a typo.

        The test is on the **start** of the drag, not the released rectangle:
        where the gesture began is what the player was pointing at when they
        committed to it, and testing the whole rectangle would refuse any
        drag that merely swept past a label.
        """
        x0, y0, x1, y1 = rect
        start = (x0, y0)
        existing = self._text_box_at(*start)
        if existing is not None:
            self.inspect_row = None
            self.inspect_note = existing.aid
            return None

        lo_x, hi_x = (x0, x1) if x0 <= x1 else (x1, x0)
        lo_y, hi_y = (y0, y1) if y0 <= y1 else (y1, y0)
        w = max(MIN_BOX_W, hi_x - lo_x)
        h = max(MIN_BOX_H, hi_y - lo_y)
        # The anchor is the box's top-left, and world +Y is up, so the stored
        # y is the *high* edge (see TextBox.bounds()).
        box = self.annotations.add_text(lo_x, lo_y + h, w, h, "",
                                        tool.colour, tool.size)
        # Opening the window on it is the other half of the gesture.
        self.inspect_row = None
        self.inspect_note = box.aid
        return box

    def _text_box_at(self, wx, wy):
        """The topmost text box at a world point, or None.

        Narrower than `AnnotationStore.hit()`, which answers for any
        annotation: the text tool only defers to *boxes*, since a stroke is
        not something a new label would collide with illegibly and refusing
        to draw a label over one would be a surprise.
        """
        if not self.annotation_layer.enabled:
            return None
        for a in reversed(self.annotations.items):
            if isinstance(a, TextBox) and a.hit_point(wx, wy):
                return a
        return None

    def _paste_note_preview(self):
        """The current paste plan's annotations, where they would land.

        Drawn as ghosts beside the entity ghosts, so the player can see the
        labels coming with the stamp rather than discovering them on release.
        Built from the live plan's `paste_origin`, which is the same number
        the commit uses -- so what is previewed and what is placed cannot
        disagree about position.

        Empty for every builder but a paste, and cheap: a stamp carries a
        handful of notes and this runs once per frame.
        """
        plan = self.last_plan
        if plan is None or plan.stamp is None or plan.paste_origin is None:
            return ()
        if not plan.stamp.annotations:
            return ()
        return plan.stamp.place_annotations(*plan.paste_origin)

    def _resize_handle_box(self):
        """The text box to draw a resize grip on, or None.

        Only the box the Status window has open -- the same gate the move
        gesture uses, and for the same reason: a grip on every label would
        litter the board and make left-drag unreliable near any of them.
        """
        if not self.annotation_layer.enabled:
            return None
        note = self.inspected_note()
        return note if isinstance(note, TextBox) else None

    def _paste_annotations(self, plan):
        """Add a pasted stamp's annotations to the live store.

        Returns how many were added. Nothing happens for a plan with no
        stamp -- every builder except a paste -- or when the annotation layer
        is off, since placing notes the player cannot see would be litter
        they have no way to find.
        """
        if plan is None or plan.stamp is None or plan.paste_origin is None:
            return 0
        if not self.annotation_layer.enabled:
            return 0
        notes = plan.stamp.place_annotations(*plan.paste_origin)
        for a in notes:
            self.annotations.add(a)
        return len(notes)

    def _move_note(self, aid, dx, dy):
        """Shift an annotation by a world-space delta.

        Guarded on the annotation still existing, like every other setter
        here: the drag is driven from the previous frame's state and the box
        could have been deleted in between (the Status window's own Delete
        button, say, clicked mid-drag).
        """
        if dx == 0.0 and dy == 0.0:
            return False
        a = self.annotations.get(aid)
        if a is None:
            return False
        a.translate(dx, dy)
        self.annotations.revision += 1
        return True

    def _resize_note(self, aid, dx, dy):
        """Grow or shrink the open text box by a world-space delta.

        The grip is the box's **bottom-right** and the anchor is its
        top-left, so this changes the extent and leaves `x`/`y` alone --
        dragging the corner must not drag the whole label. World +Y is up and
        the box hangs downward from its anchor, so dragging the grip *down*
        (negative dy) makes it taller.

        Clamped to the same minimum a fresh box gets, since a box dragged to
        nothing would be invisible and then unclickable -- unrecoverable
        except through the panel's clear-all.
        """
        a = self.annotations.get(aid)
        if not isinstance(a, TextBox):
            return False
        w = max(MIN_BOX_W, a.w + dx)
        h = max(MIN_BOX_H, a.h - dy)
        if w == a.w and h == a.h:
            return False
        a.w = w
        a.h = h
        self.annotations.revision += 1
        return True

    def _set_note_text(self, aid, text):
        """Apply the Status window's text edit.

        Guarded on the annotation still existing, for the same reason
        `_set_emit` guards the row: a request is built from the previous
        frame's state and the annotation could have been erased in between.
        """
        a = self.annotations.get(aid)
        if a is None or not isinstance(a, TextBox):
            return False
        if a.text == text:
            return False
        a.text = text
        self.annotations.revision += 1
        return True

    def _set_note_style(self, aid, colour=None, size=None):
        """Apply a colour or size change from the Status window."""
        a = self.annotations.get(aid)
        if a is None:
            return False
        changed = False
        if colour is not None and a.colour != colour:
            a.colour = int(colour)
            changed = True
        if size is not None:
            attr = "width" if hasattr(a, "width") else "size"
            if getattr(a, attr) != size:
                setattr(a, attr, int(size))
                changed = True
        if changed:
            self.annotations.revision += 1
        return changed

    def _delete_note(self, aid):
        """The Status window's Delete button."""
        if self.annotations.remove(aid):
            if self.inspect_note == aid:
                self.inspect_note = None
            return True
        return False

    def inspected_note(self):
        """The annotation the Status window has open, or None.

        Looked up per call rather than held, exactly as `inspected()` is: the
        annotation may have been deleted by a brush stroke or a rectangle
        erase since the window was opened, and a stale id must close the
        window rather than resurrect anything.
        """
        return self.annotations.get(self.inspect_note)

    def _inspect_at(self, cursor_cell, world=None):
        """Open the Status window on whatever is at the cursor, or close it.

        The empty hand's one gesture. Clicking a structure inspects it;
        clicking bare board closes the window, which makes "click away to
        dismiss" work without the player hunting for the close button.

        Stores the **row** rather than the view, because a view must not
        outlive the frame it was made in -- see `inspect_row`.

        **Annotations are checked first**, when `world` is given, because they
        are drawn over the board: clicking a label has to reach the label
        rather than the belt beneath it, or a note sitting on a factory would
        be uneditable. Only when no annotation is hit does the click fall
        through to the entity underneath -- so clicking bare board still
        closes the window, and clicking a machine still inspects it.

        The two selections are mutually exclusive: whichever is found clears
        the other, so the Status window never has to decide which to show.

        Ghosts are skipped for the same reason the pipette skips them: a
        preview ghost can sit under the cursor, and inspecting it would open
        a window on an entity that is about to be despawned. An empty hand
        has no preview of its own, so this only matters on the frame a tool
        was just put down.
        """
        if world is not None and self.annotation_layer.enabled:
            # Only when the layer is on: an annotation that is not being
            # drawn must not be clickable, or an invisible label would eat
            # clicks meant for the factory.
            hit = self.annotations.hit(world[0], world[1])
            if hit is not None:
                self.inspect_row = None
                self.inspect_note = hit.aid
                return

        self.inspect_note = None
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
        # **Invalidate as well as flush**, which it did not before and which
        # is now load-bearing. A Source's constant is *derived state* on the
        # device: a fully-direct Source folds into its neighbours' input
        # channels, and the fold -- plus the `konst` array the firing kernel
        # reads for OP_CONST -- is packed once per edit. Flushing items in
        # flight does not touch either, so the old value kept arriving from
        # a fold nothing had re-derived.
        #
        # `invalidate_segments()` is the one method every real edit routes
        # through: it settles the device, drops the fold and the caches, and
        # schedules the rebuild whose `sync_device()` re-packs the constant.
        self.engine.invalidate_segments()
        self.engine.flush_material()

    def _set_seed(self, row, on, value):
        """Change a belt cell's seed, and re-derive so it takes effect.

        A seed fires when segments are derived, so changing one has to
        invalidate the index -- otherwise the player ticks the box and
        nothing happens until their next unrelated edit. `invalidate_segments()`
        is the whole mechanism: the frame's `sync_segments()` then rebuilds,
        flushes, and re-seeds, exactly as a belt placement would.

        That also means **changing a seed clears the board's items**, since a
        rebuild does. Consistent with every other edit to the belt network,
        and with a Source's emit value -- there is one meaning of "start
        fresh" (see `Engine.flush_material()`).

        Guarded on the row still being a live belt for the same reason
        `_set_emit` is: a request is built from the previous frame's state and
        the entity could have been erased in between.
        """
        board = self.engine.board
        if not board.is_row_alive(row):
            return
        view = board.view(row)
        if not isinstance(view, Belt):
            return
        if view.seed_on == bool(on) and view.seed_value == value:
            # Nothing changed. Checked for the same reason `_set_emit` checks:
            # imgui reports a change per keystroke, and re-deriving on every
            # one would wipe the board mid-edit.
            return
        view.seed_on = bool(on)
        view.seed_value = float(value)
        # The sign lives in the cell for the tint, so the row alone is not
        # enough -- see Gameboard._stamp_footprint().
        board.restamp(view)
        self.engine.invalidate_segments()

    def _needs_host_state(self):
        """Does anything this frame read simulation state off the host?

        The readback predicate -- see the comment at the device sync. Kept
        narrow and listed explicitly rather than defaulting to True, because
        the default costs 1.16ms a frame on a real board and buys nothing for
        the common case of watching a factory run.

        Correctness does not rest on this: every *edit* and every save calls
        `Engine.settle()` itself, so a wrong answer here can only make the
        waiting-zone overlay a frame stale, never corrupt the simulation.
        """
        # The Status window reads an entity's channels, and the segment
        # inspector reads ring contents and runs audits.
        if self.inspect_row is not None or self.ui.show_segments:
            return True
        # An armed profiler is measuring the tick; a dump that reported GPU
        # ticks without their readback would describe a cost nobody pays.
        if self.engine.clock.profiler.armed:
            return True
        return False

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
            path = self.engine.save_board(self._save_path(),
                                          annotations=self.annotations)
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
            n = self.engine.load_board(path, annotations=self.annotations)
        except (persistence.SaveError, OSError) as exc:
            return (False, f"Load failed: {exc}")
        # The loaded board has nothing to do with the in-flight gesture.
        self.input.cancel()
        self.last_plan = None
        self._reset_tool_state()
        # The window may be open on an annotation the old board owned. The
        # load re-assigned every id, so the held one is meaningless now.
        self.inspect_note = None
        self._live_stroke = None
        self._note_box = None
        notes = len(self.annotations)
        extra = f", {notes} note{'s' if notes != 1 else ''}" if notes else ""
        return (True, f"Loaded {n} entities{extra} from {path}")

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
            note=self._note_gesture(self.input.cursor_world_now()),
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
            # Delete takes the notes with the factory. They describe a board,
            # so leaving them over an empty one would be labels pointing at
            # nothing -- and the player asked for a clean slate.
            self.annotations.clear()
            self.inspect_note = None
        if intents.toggle_pause:
            engine.clock.toggle_pause()
        if intents.step_tick:
            engine.clock.request_step()
        if intents.reset_sim:
            engine.reset_sim()

        # 2b. annotations. Their own gesture, resolved before the toolbelt and
        # the build path because an annotation tool takes both mouse buttons
        # and never produces a BuildPlan -- see core/builders.AnnotationTool.
        note_tool = self._note_tool()
        if note_tool is not None:
            if intents.note_draw is not None:
                self._note_draw(intents.note_draw, note_tool)
            if intents.note_draw_end:
                self._note_draw_end()
            if intents.note_erase is not None:
                self.annotations.erase_at(*intents.note_erase)
                # A brush stroke can delete the annotation the window has
                # open. `inspected_note()` already returns None for a dead id,
                # so the window closes on its own -- but clearing the id here
                # keeps the two from disagreeing for a frame.
                if self.inspected_note() is None:
                    self.inspect_note = None
            self._note_box = intents.note_box
            if intents.note_box_end and intents.note_box is not None:
                self._note_box_end(intents.note_box, note_tool)
                self._note_box = None
            if intents.cancel_plan:
                # Esc abandons an in-flight stroke without committing it.
                self._live_stroke = None
                self._note_box = None
        else:
            # Holding anything else drops an in-flight annotation gesture, the
            # same way switching tools abandons a half-drawn belt run.
            self._live_stroke = None
            self._note_box = None

        # Dragging the open text box. Outside the branch above because this
        # is an **empty-hand** gesture -- there is no tool to be holding.
        if intents.note_move is not None:
            self._move_note(self.inspect_note, *intents.note_move)
        if intents.note_resize is not None:
            self._resize_note(self.inspect_note, *intents.note_resize)

        # 3. toolbelt selection. Changing tools drops any in-flight preview --
        # a half-drawn belt run means nothing to the structure tool.
        #
        # The row swap is handled first, so a digit pressed in the same frame
        # the row the player just swapped to. It deliberately does **not**
        # touch the hand or any in-flight gesture: it re-aims the number keys
        # and nothing more, so tabbing mid-belt-run keeps the run.
        if intents.swap_rows:
            self.toolbelt.swap_rows()
        if intents.select_slot is not None:
            self._select_column(intents.select_slot)

        # Q: pick up or put down. In copy or paste mode it means neither --
        # it leaves the mode with an *empty* hand, dropping the stashed tool
        # rather than returning to it.
        if intents.pipette:
            if self.clipboard.active:
                self._end_mode(to_empty=True)
            else:
                self._pipette(intents.cursor_cell, intents.cursor_in_world)

        # ctrl+C / ctrl+V / ctrl+D / ctrl+X, and the ways back out. Mutually
        # exclusive: one key press cannot mean two modes.
        if intents.copy_mode:
            self._enter_copy()
        elif intents.paste_mode:
            self._enter_paste()
        elif intents.cut_mode:
            self._enter_erase(cut=True)
        elif intents.erase_mode:
            self._enter_erase(cut=False)
        if intents.exit_mode:
            self._end_mode(to_empty=False)

        # shift+scroll in paste mode walks the stamp history.
        if intents.stamp_steps:
            step = 1 if intents.stamp_steps > 0 else -1
            if self.clipboard.cycle_stamp(step):
                self._refresh_paste_tool()

        # A completed selection drag becomes a stamp, or a deletion. The two
        # share the gesture and differ only here: a copy ends its mode, an
        # erase stroke keeps going.
        self.select_rect = intents.select_rect
        if intents.commit_select:
            self._capture_selection(intents.select_rect)
        elif intents.commit_erase:
            self._erase_selection(intents.select_rect)

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
        elif tool is None:
            # Empty hand: R and F transform whatever is **on the board** under
            # the cursor rather than a held tool. Same split as the left
            # click, which builds with a tool and inspects without one: the
            # key says "rotate", and what it rotates is whatever the hand is
            # not holding.
            #
            # In place, so per-instance settings survive -- a Source keeps its
            # emit value and a belt its seed, because the row is kept and only
            # the facing changes. The engine refuses any turn that would move
            # the entity, so this cannot fail destructively; a type with no
            # footprint-preserving rotation simply does nothing.
            if intents.cursor_in_world and intents.cursor_cell is not None:
                if intents.cycle_tool:
                    engine.turn_in_place(*intents.cursor_cell,
                                         backward=intents.cycle_backward)
                if intents.flip_tool:
                    engine.flip_in_place(*intents.cursor_cell)
        else:
            if intents.cycle_tool:
                tool.cycle(backward=intents.cycle_backward)
            if intents.flip_tool:
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
            # World coords as well as the cell, so a click can reach an
            # annotation drawn over the board before the entity beneath it.
            self._inspect_at(
                intents.cursor_cell if intents.cursor_in_world else None,
                intents.cursor_world if intents.cursor_in_world else None)
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
            if blocked and not intents.shift_held:
                # ...unless every clash is a **match**: the cells it wants are
                # already occupied by exactly what it was going to put there.
                # Then the player is lining a stamp up over existing machines
                # to re-stamp them with new settings, so substitute.
                #
                # The `shift_held` half is **defensive rather than
                # load-bearing**, and mutation-testing says so: shift makes a
                # paste `partial`, which makes the plan not `all_or_nothing`,
                # so `blocked` is already False and this branch is
                # unreachable under shift. It stays because that reasoning
                # runs through two other objects -- the modifier reaching
                # PasteBuilder and the policy reaching BuildPlan -- and a
                # tool that was all-or-nothing *and* honoured shift would
                # otherwise silently acquire substitution. Saying "bare
                # clicks only" here costs one condition and does not depend
                # on the paste policy staying as it is.
                plan = self._substitute(tool, plan)
                blocked = plan is None
            if not blocked:
                # A pasted underground pair must join to *itself* rather than
                # to whatever happens to sit near the paste site, so the
                # plan's own pairing goes in first and the engine's
                # nearest-candidate search only handles what is left.
                engine.commit_ghosts(engine.planned_under_pairs(plan))
                # A pasted stamp brings its annotations. After the entities,
                # so a partial paste that placed nothing still places no
                # notes -- `buildable` being empty is what `blocked` already
                # tested, so reaching here means something was built.
                self._paste_annotations(plan)
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
        if requests.swap_rows:
            self.toolbelt.swap_rows()
        if requests.select_slot is not None:
            # Same path as the number keys, so clicking a slot in the bar
            # leaves a clipboard mode exactly as pressing its digit does.
            # A *flat* index, unlike the keys' column -- a clicked button
            # knows which row it is in and so can select across rows.
            self._select_slot(requests.select_slot)
        if requests.set_slot is not None:
            self._set_slot(*requests.set_slot)
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
            self.inspect_note = None
        if requests.annotations_on is not None:
            self.annotation_layer.enabled = bool(requests.annotations_on)
        if requests.set_note_text is not None:
            self._set_note_text(*requests.set_note_text)
        if requests.set_note_style is not None:
            aid, colour, size = requests.set_note_style
            self._set_note_style(aid, colour=colour, size=size)
        if requests.delete_note is not None:
            self._delete_note(requests.delete_note)
        if requests.clear_notes:
            self.annotations.clear()
            self.inspect_note = None
        if requests.set_emit is not None:
            self._set_emit(*requests.set_emit)
        if requests.set_seed is not None:
            self._set_seed(*requests.set_seed)
        if requests.toggle_pause:
            engine.clock.toggle_pause()
        if requests.step_tick:
            engine.clock.request_step()
        if requests.tick_rate is not None:
            engine.clock.set_rate(requests.tick_rate)
        if requests.segment_debug is not None:
            self.camera.set_define("SEGMENT_DEBUG", requests.segment_debug)
        if requests.sleep_debug is not None:
            self.camera.set_define("SLEEP_DEBUG", requests.sleep_debug)
        # Plain attributes rather than set_define(): these are uniforms, so
        # flicking between them while watching a running factory costs
        # nothing and recompiles nothing. See render/camera.py.
        if requests.sleep_show_belts is not None:
            self.camera.sleep_show_belts = requests.sleep_show_belts
        if requests.sleep_show_gates is not None:
            self.camera.sleep_show_gates = requests.sleep_show_gates
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
        if requests.profile_ticks is not None:
            # Armed *here*, before the tick below, so the very next tick is
            # measured rather than the one after -- a 20-tick sample would
            # otherwise be 5% short and, worse, would miss the tick the player
            # pressed the button on.
            if requests.profile_ticks:
                engine.clock.profiler.arm(requests.profile_ticks)
            else:
                # Stopping is also a request to see the result: the player
                # pressed "stop and dump", and leaving them to find another
                # button would be the only way to lose the measurement.
                engine.clock.profiler.stop()
                self._dump_profile(engine)

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
        # The clock disarms itself when the requested count runs out and sets
        # the flag; dumping here rather than inside the clock keeps the clock
        # free of any opinion about where diagnostics go, the same division
        # that has the panel only *request* a board change.
        if engine.clock.profile_done:
            engine.clock.profile_done = False
            self._dump_profile(engine)

        # 8. device sync, then render
        #
        # **The readback is conditional, because it is the expensive thing.**
        # Measured on ram.npz: the dispatches are 0.031ms a tick and the round
        # trip is 1.16ms, so settling every frame makes the GPU path *slower*
        # than the CPU one at 1 tick/frame -- the opposite of the point.
        #
        # Belt state never needs it: the kernel writes the very buffers the
        # renderer reads (see core/belt_compute.py), so the picture is already
        # current. Gate *channels* are the exception -- they live in the gate
        # layer's SoA arrays while the shader draws waiting zones out of the
        # entity SSBO, which is packed from host rows -- and the only thing
        # that shows is an occupied zone, which is visible solely while a gate
        # is back-pressured.
        #
        # So: settle when something on the host actually reads simulation
        # state this frame (a window is open, a request came in), and
        # otherwise leave the device ahead. `Engine.settle()` is still called
        # by every *edit*, every save and every flush, so correctness never
        # depends on this decision -- only how fresh the gate overlay looks.
        #
        # **The sharp edge this leaves is reads that go straight to the
        # simulation objects.** `engine.belts.total_items()` and friends read
        # the host's segment objects, which are now whatever the last
        # readback left behind. `Engine.live_items()` is the asking form that
        # settles first; anything reporting a count should use it. The
        # distinction is the same one `_gpu_dirty` names -- the device is
        # authoritative until someone asks -- and it is why the flag is
        # checked rather than hidden behind a property that would settle on
        # every incidental access, including from inside the tick.
        if self._needs_host_state():
            engine.settle()
        self.ui.last_upload = engine.sync_to_device()
        self.camera.render(viewport, engine.clock, engine.belt_device,
                           select_rect=self.select_rect)

        # Annotations last: they are an imgui overlay, so they draw over the
        # world pass by construction. `draw()` returns immediately when the
        # layer is disabled -- before touching imgui or the store at all.
        #
        # `active` tracks `draw_ui` because both answer "is there an imgui
        # frame?". A headless harness sets `draw_ui = False` and never calls
        # `new_frame()`, and reaching for a draw list then takes the process
        # down natively rather than raising.
        self.annotation_layer.active = self.draw_ui
        self.annotation_layer.draw(
            self.annotations, self.camera, viewport,
            in_progress=self._live_stroke,
            drag_rect=self._note_box,
            selected_id=self.inspect_note,
            preview=self._paste_note_preview(),
            handle_of=self._resize_handle_box(),
        )

    def seed_demo(self):
        """A few belts so the board is not empty on first run."""
        from engine import MODE_HORIZONTAL_FIRST, MODE_VERTICAL_FIRST
        e = self.engine
        cx, cy = e.board.width // 2, e.board.height // 2
        e.place_belt_path((cx - 12, cy), (cx + 4, cy + 6), MODE_HORIZONTAL_FIRST)
        e.place_belt_path((cx - 12, cy - 8), (cx + 4, cy - 2), MODE_VERTICAL_FIRST)
