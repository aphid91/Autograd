"""Gates: a grid-based factory game.

Window/context setup and the main loop. Everything else lives behind the
Orchestrator, which owns the engine, camera, input manager, and UI.

Controls:
  WASD          pan
  Q / E         zoom out / in
  Scroll        zoom, anchored at the cursor
  Left-drag     pan the camera
  F             per-tool: flip (mirror) the held structure
  shift+F       frame the whole board
  1-9, 0        select a toolbelt slot
  X             swap which toolbelt row the number keys address
  ctrl+D        erase mode (drag a box); ctrl+shift+D cuts
  Left-click    build with the active tool (on release, not press)
  R / shift+R   per-tool: belt path shape, or structure rotation
  Right-click   cancel a plan, or drag to erase
  Esc           cancel a plan, or a tool's multi-step state
  F5            reload the world shaders
  Delete        clear the board
  Space         pause / resume the simulation
  .             run exactly one tick (works while paused)
"""

import glfw
import moderngl
from imgui_bundle import imgui
from imgui_bundle.python_backends.glfw_backend import GlfwRenderer

from core import config
from orchestrator import Orchestrator

WINDOW_W, WINDOW_H = 1280, 720
WINDOW_TITLE = "Gates"

BOARD_W, BOARD_H = 512, 512


def main():
    if not glfw.init():
        raise RuntimeError("Failed to initialise GLFW")

    # 4.3 for SSBOs and (later) compute shaders.
    glfw.window_hint(glfw.CONTEXT_VERSION_MAJOR, 4)
    glfw.window_hint(glfw.CONTEXT_VERSION_MINOR, 3)
    glfw.window_hint(glfw.OPENGL_PROFILE, glfw.OPENGL_CORE_PROFILE)
    glfw.window_hint(glfw.OPENGL_FORWARD_COMPAT, glfw.TRUE)

    window = glfw.create_window(WINDOW_W, WINDOW_H, WINDOW_TITLE, None, None)
    if not window:
        glfw.terminate()
        raise RuntimeError("Failed to create GLFW window")

    glfw.make_context_current(window)
    glfw.swap_interval(1)

    ctx = moderngl.create_context()
    ctx.enable(moderngl.BLEND)

    imgui.create_context()
    io = imgui.get_io()
    io.config_flags |= imgui.ConfigFlags_.docking_enable
    renderer = GlfwRenderer(window)

    orch = Orchestrator(window, ctx, BOARD_W, BOARD_H)
    # Remembered UI preferences. Loaded here rather than in the Orchestrator
    # or UI constructor on purpose: the test harness builds both, and an
    # implicit load would give every test run the player's own save filename.
    # See core/config.py.
    prefs = config.load()
    orch.ui.save_name = prefs["save_name"]
    # The customised toolbar, for the same reasons and by the same route: the
    # player's layout must not leak into a test run, so the Orchestrator takes
    # it as plain data rather than reading the file. `None` (no saved layout)
    # leaves `default_builders()` in place.
    orch.apply_toolbar(
        config.toolbar_from_data(prefs["toolbar"], len(orch.toolbelt.slots)))

    # Pick up the quicksave if there is one; otherwise start from the demo
    # layout, so a first run still has something on the board. This reads the
    # filename box, so it has to follow the line above -- autoloading first
    # would always open the default rather than the file the player last used.
    loaded = orch.autoload()
    if loaded is not None:
        print(loaded[1])
    else:
        orch.seed_demo()

    last_wall = glfw.get_time()

    while not glfw.window_should_close(window):
        glfw.poll_events()
        renderer.process_inputs()
        imgui.new_frame()

        # passthru_central_node keeps the centre transparent so the world
        # stays visible wherever no panel is docked.
        imgui.dock_space_over_viewport(
            0, imgui.get_main_viewport(),
            imgui.DockNodeFlags_.passthru_central_node,
        )

        wall_now = glfw.get_time()
        dt = wall_now - last_wall
        last_wall = wall_now
        # Clamp dt so a hitch or a drag of the window title bar does not fling
        # the camera across the board.
        dt = min(dt, 0.1)

        ctx.clear(0.07, 0.07, 0.09)
        orch.frame(dt)

        # Persist the filename box when it changes. Writing on the edit rather
        # than at shutdown means a crash cannot lose it, and the file is tiny
        # enough that the cost does not matter at typing speed.
        if orch.ui.save_name_dirty:
            orch.ui.save_name_dirty = False
            prefs["save_name"] = orch.ui.save_name
            config.save(prefs)

        # Same treatment for the toolbar. Written on the edit rather than at
        # shutdown for the reason above -- a crash or a kill must not lose an
        # arrangement built up over a session -- and a slot change is a few
        # keystrokes per hour, so there is nothing to batch.
        if orch.toolbar_dirty:
            orch.toolbar_dirty = False
            prefs["toolbar"] = orch.toolbar_data()
            config.save(prefs)

        imgui.render()
        renderer.render(imgui.get_draw_data())
        glfw.swap_buffers(window)

    renderer.shutdown()
    imgui.destroy_context()
    glfw.terminate()


if __name__ == "__main__":
    main()
