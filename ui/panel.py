"""UI: imgui panels. Reads state and returns requests; never mutates the board."""

from imgui_bundle import imgui

from core import cell
from ui.input_manager import STATE_NAMES
from core.toolbelt import SLOT_COUNT, SPLIT_AFTER


class UIRequests:
    """Buttons the player pressed this frame."""

    __slots__ = ("reload_shader", "frame_board", "clear_board",
                 "verify_round_trip", "select_slot", "cycle_tool",
                 "cycle_backward")

    def __init__(self):
        self.reload_shader = False
        self.frame_board = False
        self.clear_board = False
        self.verify_round_trip = False
        self.select_slot = None
        self.cycle_tool = False
        self.cycle_backward = False


class UI:
    def __init__(self):
        self.last_verify = None     # None | True | False
        self.last_upload = (0, 0)

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

            # Active tool's state, under the bar.
            active = belt.active
            if active is not None:
                status = active.status()
                imgui.text_disabled(f"{active.name}" + (f"  —  {status}" if status else ""))
                imgui.same_line()
                if imgui.small_button("R"):
                    req.cycle_tool = True
                imgui.same_line()
                if imgui.small_button("shift+R"):
                    req.cycle_tool = True
                    req.cycle_backward = True
            else:
                imgui.text_disabled("empty slot")
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

        imgui.begin("Gates")

        imgui.text(f"FPS {io.framerate:5.0f}")
        imgui.text(f"Entities: {board.live_count}   capacity {board.capacity}")
        imgui.text(f"Placed {engine.stats['placed']}  Erased {engine.stats['erased']}")

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
        if imgui.button("Frame board (F)"):
            req.frame_board = True

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
        imgui.text("WASD pan    Q/E zoom    F frame")
        imgui.text("1-9,0 or scroll: select tool")
        imgui.text("Left-click: build with the active tool")
        imgui.text("R / shift+R: per-tool (belt shape, structure rotation)")
        imgui.text("Right-click: cancel plan / drag to erase")

        imgui.end()
        return req
