"""UI: imgui panels. Reads state and returns requests; never mutates the board."""

from imgui_bundle import imgui

import cell
from engine import MODE_NAMES
from input_manager import STATE_NAMES


class UIRequests:
    """Buttons the player pressed this frame."""

    __slots__ = ("reload_shader", "frame_board", "clear_board", "verify_round_trip",
                 "set_mode")

    def __init__(self):
        self.reload_shader = False
        self.frame_board = False
        self.clear_board = False
        self.verify_round_trip = False
        self.set_mode = None


class UI:
    def __init__(self):
        self.show_debug = False
        self.last_verify = None     # None | True | False
        self.last_upload = (0, 0)

    def draw(self, orch):
        req = UIRequests()
        engine = orch.engine
        board = engine.board
        cam = orch.camera
        im = orch.input
        io = imgui.get_io()

        imgui.begin("Gates")

        imgui.text(f"FPS {io.framerate:5.0f}")
        imgui.text(f"Entities: {board.live_count}   capacity {board.capacity}")
        imgui.text(f"Placed {engine.stats['placed']}  Erased {engine.stats['erased']}")

        imgui.separator()
        imgui.text_unformatted("Build")
        imgui.text(f"State: {STATE_NAMES[im.state]}")
        imgui.text(f"Mode:  {MODE_NAMES[im.path_mode]}")
        if imgui.button("Cycle mode (R)"):
            req.set_mode = (im.path_mode + 1) % len(MODE_NAMES)

        if im.state == 1 and im.anchor is not None:
            imgui.text(f"From {im.anchor} to {im.target}")
            imgui.text(f"Ghost cells: {len(engine.ghost_rows)}")

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
                ghost = cell.is_ghost(int(board.grid[cy, cx, cell.CH_Z]))
                imgui.text(f"  {name} dir={cell.DIR_NAMES[d]} id={i}"
                           + ("  GHOST" if ghost else ""))
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
        imgui.text("Left-click: start belt, click again to confirm")
        imgui.text("R: cycle elbow/line while planning")
        imgui.text("Right-click: cancel plan / drag to erase")

        imgui.end()
        return req
