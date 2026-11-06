"""Gates: a grid-based factory game.

Window/context setup and the main loop. Everything else lives behind the
Orchestrator, which owns the engine, camera, input manager, and UI.

Controls:
  WASD          pan
  Q / E         zoom out / in
  Scroll        zoom, anchored at the cursor
  Left-drag     pan the camera
  F             frame the whole board
  1-9, 0        select a toolbelt slot
  Left-click    build with the active tool (on release, not press)
  R / shift+R   per-tool: belt path shape, or structure rotation
  Right-click   cancel a plan, or drag to erase
  Esc           cancel a plan
  F5            reload the world shaders
  Delete        clear the board
"""

import glfw
import moderngl
from imgui_bundle import imgui
from imgui_bundle.python_backends.glfw_backend import GlfwRenderer

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
    # Pick up the quicksave if there is one; otherwise start from the demo
    # layout, so a first run still has something on the board.
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

        imgui.render()
        renderer.render(imgui.get_draw_data())
        glfw.swap_buffers(window)

    renderer.shutdown()
    imgui.destroy_context()
    glfw.terminate()


if __name__ == "__main__":
    main()
