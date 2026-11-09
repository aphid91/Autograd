"""UI: imgui panels. Reads state and returns requests; never mutates the board."""

from imgui_bundle import imgui

from core import cell, persistence
from core.belts import UNIT
from core.entity import Belt, Source
from ui.input_manager import STATE_NAMES
from core.toolbelt import SLOT_COUNT, SPLIT_AFTER


class UIRequests:
    """Buttons the player pressed this frame."""

    __slots__ = ("reload_shader", "frame_board", "clear_board",
                 "verify_round_trip", "select_slot", "cycle_tool",
                 "cycle_backward", "flip_tool", "quicksave", "quickload",
                 "toggle_pause", "step_tick", "tick_rate",
                 "segment_debug", "rebuild_segments", "audit_segments",
                 "debug_feed", "clear_items", "close_status",
                 "set_emit", "set_seed")

    def __init__(self):
        self.reload_shader = False
        self.frame_board = False
        self.clear_board = False
        self.verify_round_trip = False
        self.select_slot = None
        self.cycle_tool = False
        self.cycle_backward = False
        self.flip_tool = False
        self.quicksave = False
        self.quickload = False
        self.toggle_pause = False
        self.step_tick = False
        #: New ticks/sec, or None to leave the rate alone.
        self.tick_rate = None
        #: New state for the shader's segment overlay, or None for no change.
        self.segment_debug = None
        self.rebuild_segments = False
        self.audit_segments = False
        #: New debug-feed value, `False` to disable it, or None for no change.
        #: Not just a float: None and "off" are different requests.
        self.debug_feed = None
        self.clear_items = False
        #: True when the Status window's own close button was used. The
        #: orchestrator owns what is being inspected, so the panel asks
        #: rather than clearing it.
        self.close_status = False
        #: (row, value) to set a Source's emitted constant, or None. Carries
        #: the row rather than a view, matching Orchestrator.inspect_row --
        #: a view must not cross a frame boundary.
        self.set_emit = None
        #: (row, on, value) to set a belt cell's seed, or None. A row for the
        #: same reason as set_emit; both halves together because the checkbox
        #: and the value box are one setting and either one changing has to
        #: carry the other's current state.
        self.set_seed = None


class UI:
    def __init__(self):
        self.last_verify = None     # None | True | False
        self.last_upload = (0, 0)
        #: None, or (ok, message) from the last save/load attempt.
        self.last_save = None
        #: Filename typed into the Save box. Empty means "use the default
        #: quicksave path", so an untouched box behaves exactly as the
        #: buttons did before the box existed.
        #:
        #: Seeded from core/config.py by app.py, never by the constructor --
        #: the test harness builds a real UI, and an implicit load would hand
        #: every test run the player's own filename. See core/config.py.
        self.save_name = ""
        #: Set when save_name changes, so app.py knows to persist it. The UI
        #: does not write files itself.
        self.save_name_dirty = False
        #: Whether the segment inspector window is open. Everything expensive
        #: it does is inside this guard, so a closed inspector costs one bool
        #: test per frame.
        self.show_segments = False
        #: Result of the last audit: None, or (problem_count, first_few).
        self.last_audit = None

    # --- segment inspector ----------------------------------------------
    def draw_segment_inspector(self, orch, req):
        """Observability for the segment index. Only drawn when open.

        Segments are derived state rebuilt from the grid, and the thing that
        goes wrong with derived state is disagreeing with its source while
        staying internally consistent. This window exists to make that
        visible: counts and pool state for the whole index, the full ring
        state of whatever the cursor is over, and an audit that checks the
        index against the grid cell by cell.

        Pairs with the shader's SEGMENT_DEBUG overlay -- the overlay shows
        *where* a run is wrong, this says *what* is wrong with it.
        """
        expanded, self.show_segments = imgui.begin(
            "Segments", self.show_segments)
        if not expanded:
            imgui.end()
            return

        engine = orch.engine
        index = engine.segments
        board = engine.board

        sim = engine.belts
        clock = engine.clock

        # --- overview ---
        imgui.text(f"Segments: {index.count}")
        imgui.text(f"Items:    {index.total_items()}")
        st = index.stats
        bst = sim.stats
        imgui.text(f"Rebuilds: {st['rebuilds']}   "
                   f"built {st['segments_built']}")
        imgui.text(f"Items dropped to edits: {st['items_dropped']}")
        imgui.text(f"Pushed {bst['pushed']}  popped {bst['popped']}  "
                   f"over {bst['ticks']} ticks")
        if orch.last_rebuild is not None:
            imgui.text_disabled(
                f"rebuilt this frame: {orch.last_rebuild} segments")
        else:
            imgui.text_disabled("index current (no rebuild this frame)")

        # --- id pool ---
        imgui.separator()
        imgui.text_unformatted("Id pool")
        imgui.text(f"Next id: {index._next_sid}   "
                   f"free: {len(index._free_sids)}")
        # A free list longer than the live set means ids are churning, which
        # is expected while editing and suspicious while idle.
        total = index.count + len(index._free_sids)
        imgui.text(f"Allocated ever: {total}")

        # --- cells ---
        imgui.separator()
        imgui.text_unformatted("Capacity")
        cells = sum(len(s.cells) for s in index.segments.values())
        positions = sum(s.length for s in index.segments.values())
        ring = sum(s.cap for s in index.segments.values())
        imgui.text(f"Belt cells:     {cells}")
        imgui.text(f"Item positions: {positions}"
                   f"   (+{positions - cells} buried)")
        imgui.text(f"Ring slots:     {ring}")

        # --- the hovered segment ---
        imgui.separator()
        imgui.text_unformatted("Under cursor")
        seg = None
        if orch.cursor_cell is not None:
            cx, cy = orch.cursor_cell
            if board.in_bounds(cx, cy):
                seg = index.at(cx, cy)
        if seg is None:
            imgui.text_disabled("no segment under the cursor")
        else:
            cx, cy = orch.cursor_cell
            imgui.text(f"Segment {seg.sid}")
            imgui.text(f"  tail {seg.cells[0]} -> head {seg.cells[-1]}")
            imgui.text(f"  length {seg.length} positions, "
                       f"{len(seg.cells)} cells")
            if seg.buried:
                imgui.text(f"  buried (underground): {seg.buried}")
            imgui.text(f"  this cell's offset: {index.offset_at(cx, cy)}")
            imgui.separator()
            imgui.text_unformatted("  Ring")
            imgui.text(f"    head {seg.head}  tail {seg.tail}  "
                       f"count {seg.count}")
            imgui.text(f"    base {seg.base}  cap {seg.cap}")
            imgui.text(f"    anchor {seg.anchor} / {sim.length_of(seg)}"
                       f"   (packed head position)")
            imgui.text(f"    physical capacity {sim.capacity_of(seg)} items")
            imgui.text_unformatted("  Ends")
            imgui.text(f"    src {seg.src if seg.src else '(dangling)'}")
            imgui.text(f"    dst {seg.dst if seg.dst else '(dangling)'}")

            # --- the items themselves ---
            # Positions are computed, never stored, so this is the closed form
            # being evaluated live rather than a readout of saved state.
            imgui.separator()
            imgui.text_unformatted("  Items (cell offset from tail)")
            if seg.count == 0:
                imgui.text_disabled("    empty")
            else:
                for k in range(seg.head, seg.tail):
                    pos = sim.position_of(seg, k, clock.tick, clock.alpha)
                    payload = sim.payload_of(seg, k)
                    enter = int(seg.enter_tick[k % seg.cap])
                    # Mark whichever term of the min is binding: "packed"
                    # means this item is in a clump, "free" that it is still
                    # running at full speed. That distinction is the heart of
                    # the closed form and is otherwise invisible.
                    unblocked = (clock.tick - enter + clock.alpha) * sim.speed
                    packed = pos < unblocked - 1e-6
                    imgui.text(
                        f"    k={k:<4} {pos / UNIT:6.2f}  "
                        f"val {payload:+.3g}  entered t={enter}  "
                        f"{'packed' if packed else 'free'}")
                    if k - seg.head > 12:
                        imgui.text_disabled(
                            f"    ... {seg.tail - k - 1} more")
                        break

        # --- the hovered gate ---
        # A gate's channels are the one piece of simulation state with no
        # visual representation except the squished waiting zone, so this is
        # the only way to see why a machine is or is not firing.
        imgui.separator()
        imgui.text_unformatted("Gate under cursor")
        gate = None
        if orch.cursor_cell is not None:
            cx, cy = orch.cursor_cell
            if board.in_bounds(cx, cy):
                row = board.entity_id_at(cx, cy)
                if row and board.is_row_alive(row):
                    v = board.view(row)
                    if v.input_count() or v.output_count():
                        gate = v
        if gate is None:
            imgui.text_disabled("no gate under the cursor")
        else:
            name = cell.TYPE_NAMES.get(gate.type_id, "?")
            imgui.text(f"{name} row {gate.row} at {gate.origin}")
            rule = "ANY" if gate.FIRE_RULE == gate.FIRE_ANY else "ALL"
            ready = gate.inputs_ready()
            clear = gate.outputs_clear()
            imgui.text(f"  fires on {rule}   "
                       f"ready {'yes' if ready else 'no'}   "
                       f"outputs {'clear' if clear else 'BLOCKED'}")
            if ready and clear:
                imgui.push_style_color(
                    imgui.Col_.text, imgui.ImVec4(0.4, 1, 0.4, 1))
                imgui.text("  -> fires next tick")
                imgui.pop_style_color()
            elif not ready:
                imgui.text_disabled("  -> starved, waiting for input")
            else:
                imgui.push_style_color(
                    imgui.Col_.text, imgui.ImVec4(1, 0.8, 0.3, 1))
                imgui.text("  -> blocked, waiting zone not drained")
                imgui.pop_style_color()
            for i in range(gate.input_count()):
                state = (f"{gate.in_value(i):+.4g}" if gate.in_held(i)
                         else "empty")
                imgui.text(f"    in  {i}: {state}")
            for i in range(gate.output_count()):
                state = (f"{gate.out_value(i):+.4g}" if gate.out_held(i)
                         else "empty")
                imgui.text(f"    out {i}: {state}")

        # --- debug feed ---
        # Gates do not drive the belts yet, so this is how transport gets
        # exercised on a real board: every run with a dangling upstream end
        # is offered an item each tick, and every dangling downstream end
        # takes one. Goes away once the gate layer lands.
        imgui.separator()
        imgui.text_unformatted("Debug feed")
        feeding = engine.debug_feed is not None
        changed, on = imgui.checkbox("Feed dangling runs", feeding)
        if changed:
            req.debug_feed = (-1.0 if on else False)
        if feeding:
            moved, value = imgui.slider_float(
                "value", float(engine.debug_feed), -4.0, 4.0, "%.2f")
            if moved:
                req.debug_feed = value
        imgui.text_disabled("stands in for Source/Sink until gates exist")
        if imgui.button("Clear items"):
            req.clear_items = True

        # --- controls ---
        imgui.separator()
        changed, on = imgui.checkbox(
            "Segment colour overlay", "SEGMENT_DEBUG" in orch.camera.defines)
        if changed:
            req.segment_debug = on
        imgui.text_disabled("recompiles the shader; zero cost when off")
        if imgui.button("Force rebuild"):
            req.rebuild_segments = True
        imgui.same_line()
        if imgui.button("Audit"):
            req.audit_segments = True
        if self.last_audit is not None:
            count, sample = self.last_audit
            if count == 0:
                imgui.push_style_color(
                    imgui.Col_.text, imgui.ImVec4(0.4, 1, 0.4, 1))
                imgui.text("audit clean")
                imgui.pop_style_color()
            else:
                imgui.push_style_color(
                    imgui.Col_.text, imgui.ImVec4(1, 0.3, 0.3, 1))
                imgui.text(f"{count} PROBLEM(S)")
                for line in sample:
                    imgui.text_wrapped(f"  {line}")
                imgui.pop_style_color()

        imgui.end()

    # --- toolbelt -------------------------------------------------------
    def draw_toolbelt(self, orch, req):
        """Factorio-style slot bar, docked to the bottom centre of the view."""
        belt = orch.toolbelt
        vp = imgui.get_main_viewport()

        slot = 48.0
        pad = 4.0
        gap = 18.0          # the 5/6 split
        width = SLOT_COUNT * slot + (SLOT_COUNT - 1) * pad + gap + 20.0

        imgui.set_next_window_pos(
            imgui.ImVec2(vp.work_pos.x + vp.work_size.x * 0.5,
                         vp.work_pos.y + vp.work_size.y - 16.0),
            imgui.Cond_.always, imgui.ImVec2(0.5, 1.0),
        )
        imgui.set_next_window_size(imgui.ImVec2(width, 0))
        flags = (imgui.WindowFlags_.no_decoration
                 | imgui.WindowFlags_.no_move
                 | imgui.WindowFlags_.no_docking
                 | imgui.WindowFlags_.always_auto_resize
                 | imgui.WindowFlags_.no_saved_settings
                 | imgui.WindowFlags_.no_focus_on_appearing
                 | imgui.WindowFlags_.no_nav)
        imgui.set_next_window_bg_alpha(0.85)

        # A mode tints the whole bar, so one that overrides the toolbelt is
        # never mistaken for a slot selection. Blue for copy, a warmer shade
        # for paste, red for erase -- the bar is the one piece of UI always
        # on screen, which makes it the right place to say what mode you are
        # in, and erase is the one worth noticing you are still in.
        cb = orch.clipboard
        tinted = cb.active
        if tinted:
            if cb.copying:
                tint = imgui.ImVec4(0.14, 0.26, 0.40, 0.92)
            elif cb.erasing:
                tint = imgui.ImVec4(0.38, 0.14, 0.14, 0.92)
            else:
                tint = imgui.ImVec4(0.30, 0.24, 0.12, 0.92)
            imgui.push_style_color(imgui.Col_.window_bg, tint)

        if imgui.begin("##toolbelt", None, flags):
            for i in range(SLOT_COUNT):
                if i:
                    imgui.same_line(0.0, gap if i == SPLIT_AFTER else pad)

                tool = belt.slots[i]
                selected = (i == belt.selected)
                label = tool.name if tool is not None else ""
                # 1..9 then 0, matching the key that selects the slot.
                digit = (i + 1) % 10

                if selected:
                    imgui.push_style_color(
                        imgui.Col_.button, imgui.ImVec4(0.30, 0.48, 0.70, 1.0))
                    imgui.push_style_color(
                        imgui.Col_.button_hovered, imgui.ImVec4(0.36, 0.55, 0.78, 1.0))
                elif tool is None:
                    imgui.push_style_color(
                        imgui.Col_.button, imgui.ImVec4(0.16, 0.16, 0.19, 1.0))
                    imgui.push_style_color(
                        imgui.Col_.button_hovered, imgui.ImVec4(0.20, 0.20, 0.24, 1.0))
                else:
                    imgui.push_style_color(
                        imgui.Col_.button, imgui.ImVec4(0.24, 0.24, 0.28, 1.0))
                    imgui.push_style_color(
                        imgui.Col_.button_hovered, imgui.ImVec4(0.30, 0.30, 0.35, 1.0))

                if imgui.button(f"{digit}\n{label}##slot{i}",
                                imgui.ImVec2(slot, slot)):
                    req.select_slot = i
                imgui.pop_style_color(2)

            # Active tool's state, under the bar. An empty hand says so: it is
            # the default state and clicking does nothing, which is worth
            # stating rather than leaving the line blank.
            active = belt.active
            if cb.active:
                # A mode overrides the hand, so report the mode rather than
                # whatever slot is stashed behind it.
                imgui.text_disabled(cb.status())
                if cb.pasting:
                    imgui.same_line()
                    if imgui.small_button("R"):
                        req.cycle_tool = True
                    imgui.same_line()
                    if imgui.small_button("F"):
                        req.flip_tool = True
            elif belt.empty_handed:
                imgui.text_disabled("(empty hand)  —  Q picks up what is "
                                    "under the cursor")
            elif active is not None:
                status = active.status()
                imgui.text_disabled(f"{active.name}" + (f"  —  {status}" if status else ""))
                imgui.same_line()
                if imgui.small_button("R"):
                    req.cycle_tool = True
                imgui.same_line()
                if imgui.small_button("shift+R"):
                    req.cycle_tool = True
                    req.cycle_backward = True
                imgui.same_line()
                if imgui.small_button("F"):
                    req.flip_tool = True
            else:
                imgui.text_disabled("empty slot")
        imgui.end()
        if tinted:
            imgui.pop_style_color()

    # --- status window --------------------------------------------------
    def draw_status(self, orch, req):
        """Inspect the clicked entity. Only drawn when something is selected.

        Opened by clicking the board with an empty hand, and dismissed three
        ways: the window's close button, clicking bare board, or erasing the
        entity. The orchestrator owns *what* is inspected (as a row, not a
        view), so this reports a close rather than clearing it -- the panel
        returns requests and never mutates game state.

        Most machines have nothing configurable, and that is fine: the window
        earns its place by reporting what a machine is and what it holds,
        which is otherwise only visible in the segment inspector. A Source is
        the one type with a setting so far.
        """
        entity = orch.inspected()
        if entity is None:
            # The row died -- erased, or overwritten -- while the window was
            # open. Close rather than render a recycled row's contents.
            req.close_status = True
            return

        expanded, keep_open = imgui.begin("Status", True)
        if not keep_open:
            req.close_status = True
        if not expanded:
            imgui.end()
            return

        name = cell.TYPE_NAMES.get(entity.type_id, f"type{entity.type_id}")
        ox, oy = entity.origin
        imgui.text(f"{name}  at ({ox}, {oy})")
        imgui.text(f"facing {cell.DIR_NAMES[entity.direction]}"
                   f"{'  flipped' if entity.flipped else ''}")
        imgui.text(f"{entity.footprint_w}x{entity.footprint_h}"
                   f"   row {entity.row}")

        # Per-type affordances. Source and Belt have one each; everything
        # else shows its channels and nothing more.
        if isinstance(entity, Belt):
            imgui.separator()
            imgui.text_unformatted("Seed")
            changed_on, on = imgui.checkbox("##seed_on", entity.seed_on)
            imgui.same_line()
            imgui.set_next_item_width(140)
            # A float entry like Source's, for the same reason: the seeded
            # value is the point of the control and should not be clamped to
            # a slider's range.
            changed_val, value = imgui.input_float(
                "##seed_value", float(entity.seed_value))
            if changed_on or changed_val:
                # Both halves travel together -- whichever widget moved, the
                # other's current value has to come along or the setter would
                # see a half-update and treat it as a change.
                req.set_seed = (entity.row, on, value)
            imgui.text_disabled(
                "puts an item of this value on this belt's run\n"
                "every time the run is rebuilt (an edit, a clear, a load)")
            imgui.text_disabled("changing it clears every item in flight")

        if isinstance(entity, Source):
            imgui.separator()
            imgui.text_unformatted("Emits")
            imgui.set_next_item_width(140)
            # A float entry, so any value is reachable -- the emitted number
            # is the machine's whole purpose and should not be clamped to a
            # slider's range.
            changed, value = imgui.input_float(
                "##emit", float(entity.emit_value))
            if changed:
                req.set_emit = (entity.row, value)
            imgui.text_disabled(
                "changing this clears every item in flight")

        n_in = entity.input_count()
        n_out = entity.output_count()
        if n_in or n_out:
            imgui.separator()
            imgui.text_unformatted("Channels")
            for c in range(n_in):
                held = entity.in_held(c)
                imgui.text(f"  in  {c}: "
                           + (f"{entity.in_value(c):g}" if held else "-"))
            for c in range(n_out):
                held = entity.out_held(c)
                # An occupied output zone at rest means back pressure: the
                # gate produced something the belt would not take.
                imgui.text(f"  out {c}: "
                           + (f"{entity.out_value(c):g}  (waiting)"
                              if held else "-"))
        imgui.end()

    # --- main panel -----------------------------------------------------
    def draw(self, orch):
        req = UIRequests()
        engine = orch.engine
        board = engine.board
        cam = orch.camera
        im = orch.input
        io = imgui.get_io()

        self.draw_toolbelt(orch, req)
        if self.show_segments:
            self.draw_segment_inspector(orch, req)
        if orch.inspect_row is not None:
            self.draw_status(orch, req)

        imgui.begin("Gates")

        imgui.text(f"FPS {io.framerate:5.0f}")
        imgui.text(f"Entities: {board.live_count}   capacity {board.capacity}")
        imgui.text(f"Placed {engine.stats['placed']}  Erased {engine.stats['erased']}")

        imgui.separator()
        imgui.text_unformatted("Simulation")
        clock = engine.clock
        if clock.paused:
            imgui.push_style_color(imgui.Col_.text, imgui.ImVec4(1, 0.8, 0.3, 1))
            imgui.text(f"Tick {clock.tick}   PAUSED")
            imgui.pop_style_color()
        else:
            imgui.text(f"Tick {clock.tick}   alpha {clock.alpha:.2f}")
        if imgui.button("Pause (Space)" if not clock.paused
                        else "Resume (Space)"):
            req.toggle_pause = True
        imgui.same_line()
        if imgui.button("Step (.)"):
            req.step_tick = True
        changed, rate = imgui.slider_float(
            "ticks/sec", clock.ticks_per_sec, 0.5, 60.0, "%.1f")
        if changed:
            req.tick_rate = rate
        # A non-zero drop count means the sim is not keeping up with
        # wall-clock and is deliberately running late -- see MAX_CATCHUP_TICKS.
        if clock.dropped_ticks:
            imgui.text_disabled(f"dropped {clock.dropped_ticks} ticks")
        imgui.text(f"Segments: {engine.segments.count}")
        gst = engine.gates.stats
        imgui.text(f"Gates fired {gst['fired']}  blocked {gst['blocked']}  "
                   f"starved {gst['starved']}")
        _changed, self.show_segments = imgui.checkbox(
            "Inspect segments", self.show_segments)

        imgui.separator()
        imgui.text_unformatted("Build")
        active = orch.toolbelt.active
        imgui.text(f"Tool:  {active.name if active else '(none)'}")
        if active is not None and active.status():
            imgui.text(f"       {active.status()}")
        imgui.text(f"State: {STATE_NAMES[im.state]}")

        plan = orch.last_plan
        if plan is not None:
            n_build = len(plan.buildable)
            n_frust = len(plan.frustrated_cells)
            imgui.text(f"Plan:  {n_build} to build, {n_frust} blocked")
            if plan.all_or_nothing and n_frust:
                imgui.push_style_color(imgui.Col_.text, imgui.ImVec4(1, 0.45, 0.4, 1))
                imgui.text("       placement blocked")
                imgui.pop_style_color()
        imgui.text(f"Ghost rows: {len(engine.ghost_rows)}")

        imgui.separator()
        imgui.text_unformatted("Camera")
        imgui.text(f"Center {cam.center[0]:7.1f}, {cam.center[1]:7.1f}")
        imgui.text(f"Zoom   {cam.zoom:7.1f} px/cell")
        if orch.cursor_cell is not None:
            cx, cy = orch.cursor_cell
            inside = board.in_bounds(cx, cy)
            imgui.text(f"Cursor {cx}, {cy}" + ("" if inside else "  (off board)"))
            if inside:
                word = board.cell_word(cx, cy)
                t, d, i = cell.unpack_cell(word)
                name = cell.TYPE_NAMES.get(t, f"type{t}")
                gstate = cell.ghost_state(int(board.grid[cy, cx, cell.CH_Z]))
                imgui.text(f"  {name} dir={cell.DIR_NAMES[d]} id={i}"
                           f"  [{cell.GHOST_NAMES[gstate]}]")
        if imgui.button("Frame board (shift+F)"):
            req.frame_board = True

        imgui.separator()
        imgui.text_unformatted("Save")
        # The filename box drives both buttons. Left empty it falls back to
        # the default quicksave path, so the pair behaves exactly as it did
        # before the box existed -- the box adds named slots without taking
        # the one-click case away.
        imgui.set_next_item_width(180)
        changed, self.save_name = imgui.input_text(
            "##save_name", self.save_name)
        if changed:
            # app.py persists it; the UI does not touch the filesystem.
            self.save_name_dirty = True
        if not self.save_name.strip():
            imgui.same_line()
            imgui.text_disabled(f"({persistence.DEFAULT_SAVE})")
        if imgui.button("Quicksave"):
            req.quicksave = True
        imgui.same_line()
        if imgui.button("Quickload"):
            req.quickload = True
        if self.last_save is not None:
            okay, msg = self.last_save
            imgui.push_style_color(
                imgui.Col_.text,
                imgui.ImVec4(0.4, 1, 0.4, 1) if okay
                else imgui.ImVec4(1, 0.3, 0.3, 1))
            imgui.text_wrapped(msg)
            imgui.pop_style_color()

        imgui.separator()
        imgui.text_unformatted("Device")
        cells, rows = self.last_upload
        imgui.text(f"Last upload: {cells} cells, {rows} rows")
        imgui.text(f"Grid dirty:  {board.grid_dirty}")
        imgui.text(f"Rows dirty:  {board.entities_dirty}")
        if imgui.button("Verify round trip"):
            req.verify_round_trip = True
        if self.last_verify is not None:
            if self.last_verify:
                imgui.push_style_color(imgui.Col_.text, imgui.ImVec4(0.4, 1, 0.4, 1))
                imgui.text("to_device/from_device MATCH")
            else:
                imgui.push_style_color(imgui.Col_.text, imgui.ImVec4(1, 0.3, 0.3, 1))
                imgui.text("MISMATCH")
            imgui.pop_style_color()

        imgui.separator()
        if imgui.button("Reload shader (F5)"):
            req.reload_shader = True
        imgui.same_line()
        if imgui.button("Clear board (Del)"):
            req.clear_board = True

        if cam.compile_error:
            imgui.push_style_color(imgui.Col_.text, imgui.ImVec4(1, 0.3, 0.3, 1))
            imgui.text_wrapped(cam.compile_error)
            imgui.pop_style_color()

        imgui.separator()
        imgui.text_unformatted("Controls")
        imgui.text("WASD pan    scroll: zoom    shift+F frame")
        imgui.text("Left-drag: pan the camera")
        imgui.text("1-9,0: select tool")
        imgui.text("Q: pick up what's under the cursor / empty the hand")
        imgui.text("ctrl+C: copy (drag a box)   ctrl+V: paste")
        imgui.text("  paste: click places, shift+click places what fits")
        imgui.text("  shift+scroll: older/newer copy")
        imgui.text("X: erase mode (drag a box)   ctrl+X: cut")
        imgui.text("Left-click: build with the active tool")
        imgui.text("R / shift+R: per-tool (belt shape, structure rotation)")
        imgui.text("Right-click: cancel plan / drag to erase")
        imgui.text("Space: pause sim    .: step one tick")

        imgui.end()
        return req
