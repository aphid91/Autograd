"""UI: imgui panels. Reads state and returns requests; never mutates the board."""

from imgui_bundle import imgui

from core import cell, persistence
from core.annotations import TextBox
from core.belts import UNIT
from core.entity import Belt, Source
from ui.input_manager import STATE_NAMES
from core.toolbelt import ROW_COUNT, ROW_SIZE, SLOT_COUNT, SPLIT_AFTER


class UIRequests:
    """Buttons the player pressed this frame."""

    __slots__ = ("reload_shader", "frame_board", "clear_board",
                 "verify_round_trip", "select_slot", "cycle_tool",
                 "cycle_backward", "flip_tool", "quicksave", "quickload",
                 "toggle_pause", "step_tick", "tick_rate",
                 "segment_debug", "sleep_debug",
                 "sleep_show_belts", "sleep_show_gates",
                 "rebuild_segments", "audit_segments", "profile_ticks",
                 "debug_feed", "clear_items", "close_status",
                 "set_emit", "set_seed", "set_slot", "swap_rows",
                 "annotations_on", "set_note_text", "set_note_style",
                 "delete_note", "clear_notes")

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
        self.sleep_debug = None
        #: New state for the sleep overlay's per-family masks, or None for no
        #: change. Unticked forces that family to read asleep, which is how
        #: one is isolated to compare against the other.
        self.sleep_show_belts = None
        self.sleep_show_gates = None
        self.rebuild_segments = False
        self.audit_segments = False
        #: Ticks to profile and dump to the console, 0 to stop a running
        #: measurement, or None for no change. A *count* rather than a bool
        #: because "measure the next 200 ticks" is the request -- an armed
        #: profiler with no end is the one that gets forgotten about and
        #: dilutes its own numbers with whatever the board does next.
        self.profile_ticks = None
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
        #: `(slot, kind, type_name)` from the right-click type menu, or None.
        #:
        #: A *description* rather than a built tool, for the same reason
        #: `set_emit` carries a row: the panel never mutates game state, and
        #: constructing a builder here would have the UI own a piece of it.
        #: The orchestrator resolves the description -- which is also what
        #: lets the "stamp" kind reach for the live clipboard, something the
        #: panel has no business doing.
        self.set_slot = None
        #: X's equivalent as a button: swap which row the digits address.
        self.swap_rows = False
        #: New state for the annotation overlay, or None for no change.
        #: `False` and `None` are different requests, hence not a plain bool.
        self.annotations_on = None
        #: `(aid, text)` from the Status window's text field, or None. An id
        #: rather than the annotation itself, for the same reason `set_emit`
        #: carries a row: the panel never mutates, and an object held across
        #: the request boundary could have been deleted in between.
        self.set_note_text = None
        #: `(aid, colour_or_None, size_or_None)` for a style change, or None.
        self.set_note_style = None
        #: Annotation id to delete, or None. The Status window's Delete.
        self.delete_note = None
        #: True when the panel's "Clear all notes" button was pressed.
        self.clear_notes = False


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
        #: How many ticks the Profile button asks for. Panel state rather than
        #: a config entry: it is a knob for one measurement, not a preference
        #: worth surviving a restart.
        self.profile_ticks = 200

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

        sleep_on = "SLEEP_DEBUG" in orch.camera.defines
        changed, on = imgui.checkbox("Sleep overlay", sleep_on)
        if changed:
            req.sleep_debug = on
        imgui.text_disabled("blue = asleep (skipped), amber = awake")
        # Which families report their real state. Shown only while the overlay
        # is compiled in, since they do nothing otherwise -- and indented,
        # because they modify it rather than standing beside it.
        #
        # Requests rather than direct writes, like every other panel control:
        # the panel only asks. These happen to land on the camera rather than
        # the board, but routing them differently for that reason would make
        # "the panel only requests" a rule with exceptions.
        if sleep_on:
            imgui.indent()
            changed, on = imgui.checkbox(
                "report belts", orch.camera.sleep_show_belts)
            if changed:
                req.sleep_show_belts = on
            imgui.same_line()
            changed, on = imgui.checkbox(
                "report gates", orch.camera.sleep_show_gates)
            if changed:
                req.sleep_show_gates = on
            # The point of the pair: belts and gates have *different*
            # schedulers -- belts are edge-triggered and genuinely skipped,
            # gates are prefiltered but visited every tick -- so comparing
            # their amber needs one family silenced at a time.
            imgui.text_disabled("unticked = force blue, to isolate the other")
            imgui.unindent()
        # The count beside the colour, because the overlay answers "where"
        # and this answers "how much" -- and on a board built as a computer
        # the ratio is the number that matters. Taken from the sim rather
        # than recomputed: this is what the belt phase actually skipped.
        asleep = engine.belts.stats.get("asleep", 0)
        total = index.count
        if total:
            imgui.text(f"belts asleep: {asleep}/{total} "
                       f"({100.0 * asleep / total:.0f}%)")
        # **The gate ratio, separately, because the two schedulers are
        # different and a combined number would hide either one.** Belts and
        # gates sleep on unrelated predicates, so summing them would let a
        # quiet memory field vouch for a gate layer that never sleeps at all
        # -- the same reason `check_wakelist_safe.py` reports its transition
        # counts per layer rather than as a total.
        #
        # Worth having at all because gate sleep was previously invisible in
        # the running app: `gates_asleep` was tracked and never displayed, and
        # the overlay guessed at the set rather than reading it. With no
        # readout and a wrong overlay there was no way to tell the feature
        # from a no-op.
        gate_total = len(engine.gates._gate_table()[0])
        if gate_total:
            gates_asleep = engine.gates.stats.get("gates_asleep", 0)
            imgui.text(f"gates asleep: {gates_asleep}/{gate_total} "
                       f"({100.0 * gates_asleep / gate_total:.0f}%)")
            if not engine.gates._sleep_enabled:
                # Otherwise a flat 0/428 reads as "nothing can sleep on this
                # board" rather than "the scheduler is switched off", which
                # are very different things to learn from a debug panel.
                imgui.text_disabled("  (gate sleeping is switched off)")
        # Folded Sources, beside the sleep ratio because it is the same
        # question -- how much of the board is the tick not visiting -- for
        # the one population sleeping can never reach. A generator fires every
        # tick it can, so folding is the *only* thing that takes one out of
        # the phase, and the count is how much that is worth on this board.
        folded = len(engine.gates.folded_sources())
        if folded:
            imgui.text(f"folded Sources: {folded}")

        # The compute path. A readout *and* a switch, because the whole point
        # of keeping the CPU implementation is that the two can be compared
        # on the same running board -- flip it and watch whether anything
        # changes. Says why it is unavailable rather than just going quiet:
        # "the GPU tick is off" and "this board cannot use it" are different
        # things to learn, and the second is the interesting one.
        if engine.gpu_ready():
            _changed, engine._gpu_enabled = imgui.checkbox(
                "GPU tick", engine._gpu_enabled)
            if engine._gpu_enabled and not engine.use_gpu_tick():
                if engine.debug_feed is not None:
                    imgui.text_disabled("  (CPU: the debug feed has no kernel)")
                elif engine.gates.has_host_decided_gates():
                    imgui.text_disabled(
                        "  (CPU: a machine on this board decides on the host)")
                else:
                    imgui.text_disabled("  (CPU: a phase callback is hooked)")
        elif engine.gpu_error:
            imgui.text_disabled("GPU tick unavailable:")
            imgui.text_disabled(f"  {engine.gpu_error[:60]}")
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

        # --- tick profile ---
        #
        # The counterpart to tools/profile_tick.py, which measures a synthetic
        # board. This measures *this* board, which is the only way to answer
        # "why is my quicksave slow" -- a real factory's mix of saturated
        # trace and sleeping memory is not a shape the synthetic boards
        # reproduce. See core/tick_profile.py.
        imgui.separator()
        prof = engine.clock.profiler
        if prof.armed:
            left = prof.remaining
            imgui.text(f"profiling... {prof.ticks} ticks"
                       + (f", {left} to go" if left is not None else ""))
            if imgui.button("Stop and dump"):
                req.profile_ticks = 0
        else:
            if imgui.button(f"Profile {self.profile_ticks} ticks"):
                req.profile_ticks = self.profile_ticks
            imgui.same_line()
            # A slider rather than a text box: the useful range is narrow and
            # the choice is a trade the player should see. Too few ticks and
            # one unlucky GC lands in the sample; too many and an edit halfway
            # through mixes two different boards into one average.
            _moved, self.profile_ticks = imgui.slider_int(
                "##proficks", self.profile_ticks, 20, 2000)
            imgui.text_disabled("prints a per-phase breakdown to the console")
            if prof.last_report is not None:
                n, per_tick = prof.last_report
                imgui.text(f"last: {1e3 * per_tick:.3f}ms/tick over {n}")

        imgui.end()

    # --- toolbelt -------------------------------------------------------
    def _draw_slot_menu(self, orch, req, slot):
        """The right-click type menu for one slot: every tool it could hold.

        Factorio-style customisation. The list is **generated**, not written
        out: the belt-ish tools come from `BUILDER_KINDS` and the machines
        from `STRUCTURES`, so adding a machine puts it in this menu with no
        change here -- the same property the GLSL tables have.

        Every item emits a *description* into `req.set_slot` rather than a
        tool. See `UIRequests.set_slot` for why the panel does not build one.
        """
        from core.builders import BUILDER_KINDS
        from core.entity import STRUCTURES

        imgui.text_disabled(f"Slot {slot % ROW_SIZE + 1}"
                            f"  (row {slot // ROW_SIZE + 1})")
        imgui.separator()

        for kind in BUILDER_KINDS:
            label = BUILDER_KINDS[kind].name
            if imgui.menu_item_simple(label):
                req.set_slot = (slot, kind, None)

        imgui.separator()
        for c in STRUCTURES:
            if imgui.menu_item_simple(c.__name__):
                req.set_slot = (slot, "structure", c.__name__)

        # "Stamp" bakes the clipboard's *current* stamp into this slot, so it
        # only appears when there is one to bake. Offered last because it is
        # the one entry whose meaning depends on session state rather than on
        # what the build supports.
        stamp = orch.clipboard.stamp
        if stamp:
            imgui.separator()
            _x, _y, w, h = stamp.bounds()
            if imgui.menu_item_simple(f"Stamp ({len(stamp)} entities {w}x{h})"):
                req.set_slot = (slot, "stamp", None)

        imgui.separator()
        if imgui.menu_item_simple("Clear slot"):
            req.set_slot = (slot, None, None)

    def draw_toolbelt(self, orch, req):
        """Factorio-style slot bar, docked to the bottom centre of the view."""
        belt = orch.toolbelt
        vp = imgui.get_main_viewport()

        slot = 48.0
        pad = 4.0
        gap = 18.0          # the 5/6 split, within a row
        width = ROW_SIZE * slot + (ROW_SIZE - 1) * pad + gap + 20.0

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
            # The active row is drawn **on top**, so "the row the number keys
            # address" is always the one nearest the status line and the
            # board. X therefore visibly exchanges the two rows rather than
            # just moving a highlight, which is what the key is named for.
            for r in (belt.row, (belt.row + 1) % ROW_COUNT):
                active_row = (r == belt.row)
                for col in range(ROW_SIZE):
                    i = r * ROW_SIZE + col
                    if col:
                        imgui.same_line(
                            0.0, gap if col == SPLIT_AFTER else pad)

                    tool = belt.slots[i]
                    selected = (i == belt.selected)
                    label = tool.name if tool is not None else ""
                    # 1..9 then 0, matching the key -- but only on the active
                    # row, since the digits do not reach the other one.
                    digit = f"{(col + 1) % 10}" if active_row else "·"

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
                        # The inactive row is dimmed, so which digits are live
                        # is readable without counting rows.
                        base = (0.24, 0.24, 0.28) if active_row \
                            else (0.19, 0.19, 0.22)
                        hov = (0.30, 0.30, 0.35) if active_row \
                            else (0.25, 0.25, 0.29)
                        imgui.push_style_color(
                            imgui.Col_.button, imgui.ImVec4(*base, 1.0))
                        imgui.push_style_color(
                            imgui.Col_.button_hovered, imgui.ImVec4(*hov, 1.0))

                    if imgui.button(f"{digit}\n{label}##slot{i}",
                                    imgui.ImVec2(slot, slot)):
                        # A clicked button knows its own flat index, so it
                        # selects across rows without needing the swap first.
                        req.select_slot = i
                    imgui.pop_style_color(2)

                    # Right-click opens the type menu for this slot, which is
                    # how the bar is customised. Opened here and drawn below,
                    # since imgui popups must be begun after their anchor.
                    if imgui.begin_popup_context_item(f"##slotmenu{i}"):
                        self._draw_slot_menu(orch, req, i)
                        imgui.end_popup()

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

            # Tab's equivalent, plus the hint that right-click customises a
            # slot -- neither is discoverable from the bar alone.
            imgui.same_line()
            if imgui.small_button(f"X: row {belt.row + 1}"):
                req.swap_rows = True
            imgui.same_line()
            imgui.text_disabled("  (right-click a slot to change it)")
        imgui.end()
        if tinted:
            imgui.pop_style_color()

    # --- annotation status ----------------------------------------------
    def draw_note_status(self, orch, req):
        """Edit the selected annotation: its text, its style, or delete it.

        Reached two ways, and they are the same window on purpose: creating a
        text box opens it (so the empty box can be typed into immediately),
        and clicking an existing annotation with an empty hand reopens it.

        Like every other panel here it only **requests** -- the orchestrator
        owns the store. Note the text field is the one place in this file
        where imgui reports a change per keystroke and that is exactly what
        is wanted: a label should update as it is typed, and unlike a seed or
        an emit value there is nothing expensive behind it.
        """
        from core.annotations import PALETTE, STROKE_WIDTHS, TEXT_SIZES

        note = orch.inspected_note()
        if note is None:
            # Deleted while the window was open -- by a brush stroke, a
            # rectangle erase, or a board load. Same resolution as a dead
            # entity row: close rather than guess.
            req.close_status = True
            return

        expanded, keep_open = imgui.begin("Status", True)
        if not keep_open:
            req.close_status = True
        if not expanded:
            imgui.end()
            return

        is_text = isinstance(note, TextBox)
        x0, y0, x1, y1 = note.bounds()
        imgui.text(f"{'Text note' if is_text else 'Drawn note'}  #{note.aid}")
        imgui.text(f"at ({x0:.1f}, {y0:.1f})  "
                   f"{x1 - x0:.1f} x {y1 - y0:.1f} cells")
        if not is_text:
            imgui.text(f"{len(note.points)} points")

        if not orch.annotation_layer.enabled:
            imgui.push_style_color(imgui.Col_.text, imgui.ImVec4(1, .8, .3, 1))
            imgui.text_wrapped("Notes are hidden -- tick Show notes to see "
                               "this one.")
            imgui.pop_style_color()

        imgui.separator()
        if is_text:
            imgui.text_unformatted("Text")
            imgui.set_next_item_width(260)
            # multiline would be nicer for a paragraph, but a label is a
            # label: one line keeps the box and the text agreeing about how
            # much room they need.
            changed, value = imgui.input_text("##note_text", note.text)
            if changed:
                req.set_note_text = (note.aid, value)
            imgui.text_disabled("shown on the board at this box")

        # --- style ---
        imgui.separator()
        imgui.text_unformatted("Colour")
        # Swatches rather than a picker: the palette is fixed (see
        # core/annotations.PALETTE) so a readable diagram stays readable.
        for i, (cname, rgb) in enumerate(PALETTE):
            if i:
                imgui.same_line()
            imgui.push_style_color(imgui.Col_.button, imgui.ImVec4(*rgb, 1.0))
            imgui.push_style_color(imgui.Col_.button_hovered,
                                   imgui.ImVec4(*rgb, 0.8))
            imgui.push_style_color(imgui.Col_.button_active,
                                   imgui.ImVec4(*rgb, 1.0))
            mark = "*" if i == note.colour else " "
            if imgui.button(f"{mark}##notecol{i}", imgui.ImVec2(26, 22)):
                req.set_note_style = (note.aid, i, None)
            imgui.pop_style_color(3)
            if imgui.is_item_hovered():
                imgui.set_tooltip(cname)

        imgui.separator()
        sizes = TEXT_SIZES if is_text else STROKE_WIDTHS
        current = note.size if is_text else note.width
        imgui.text_unformatted("Size" if is_text else "Width")
        for i in range(len(sizes)):
            if i:
                imgui.same_line()
            label = f"{i + 1}{'*' if i == current else ''}##notesize{i}"
            if imgui.button(label, imgui.ImVec2(32, 22)):
                req.set_note_style = (note.aid, None, i)

        imgui.separator()
        imgui.push_style_color(imgui.Col_.button, imgui.ImVec4(.5, .15, .15, 1))
        imgui.push_style_color(imgui.Col_.button_hovered,
                               imgui.ImVec4(.65, .2, .2, 1))
        if imgui.button("Delete note"):
            req.delete_note = note.aid
        imgui.pop_style_color(2)
        imgui.end()

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
        # An annotation takes the window when one is selected. The two are
        # mutually exclusive (see Orchestrator._inspect_at), so this is a
        # branch rather than a merge, and the annotation case is checked first
        # because it is the one the player just created.
        if orch.inspect_note is not None:
            self.draw_note_status(orch, req)
            return

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
            # Whether this Source has been folded into a constant. Reported
            # rather than configurable: it is derived from how the machine is
            # wired, so the way to change it is to wire it differently.
            #
            # Worth surfacing because a folded Source is **invisible** in
            # every other view -- it looks identical, and the sleep overlay
            # paints it the same blue as any gate holding nothing (which it
            # is, since the constant lives on its *neighbour's* channel). A
            # player wondering why a Source shows no activity has nowhere
            # else to find out that this is the answer.
            folded = orch.engine.gates.is_folded(entity.row)
            if folded:
                imgui.text("folded: yes")
                imgui.text_disabled(
                    "every output goes straight to a gate, so this Source is\n"
                    "a constant on its neighbours' inputs and is not ticked")
            else:
                imgui.text("folded: no")
                imgui.text_disabled(
                    "a belt on any output makes this a real producer, whose\n"
                    "rate the belt regulates -- so it ticks like any gate")

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
        if orch.inspect_row is not None or orch.inspect_note is not None:
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
        imgui.text_unformatted("Notes")
        layer = orch.annotation_layer
        changed, on = imgui.checkbox("Show notes", layer.enabled)
        if changed:
            req.annotations_on = on
        if layer.enabled:
            imgui.text(f"{len(orch.annotations)} on the board"
                       f"   ({layer.drawn} drawn)")
            if imgui.button("Clear all notes"):
                req.clear_notes = True
            imgui.text_disabled("right-click a slot to add a note tool")
        else:
            # Said explicitly, because a disabled layer also stops notes
            # being clickable, copied and rectangle-erased -- so a player who
            # has forgotten the checkbox would otherwise think they had lost
            # their annotations.
            imgui.text_disabled(f"{len(orch.annotations)} hidden"
                                "  (not drawn, clickable or copied)")

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
        imgui.text("1-9,0: select tool    X: swap toolbar row")
        imgui.text("Q: pick up what's under the cursor / empty the hand")
        imgui.text("ctrl+C: copy (drag a box)   ctrl+V: paste")
        imgui.text("  paste: click places, shift+click places what fits")
        imgui.text("  shift+scroll: older/newer copy")
        imgui.text("ctrl+D: erase mode (drag a box)   ctrl+X: cut")
        imgui.text("Left-click: build with the active tool")
        imgui.text("R / shift+R: per-tool (belt shape, structure rotation)")
        imgui.text("Right-click: cancel plan / drag to erase")
        imgui.text("Space: pause sim    .: step one tick")

        imgui.end()
        return req
