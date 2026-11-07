"""Headless orchestrator harness: a fake mouse and keyboard, and a runner.

Creates an invisible GL 4.3 context, patches the glfw input getters so the
input state machine can be stepped deterministically, and exposes the small
vocabulary the scenarios are written in -- `click()`, `put_cursor_at_cell()`,
`select_tool()`, `step()`.

**Scenarios are isolated.** Each one runs with a cleared board, a reset
camera, no held keys or buttons, and the Belt tool selected. The suite used
to be one long script whose sections shared state through side effects, so a
change in one place could break an assertion hundreds of lines later for
reasons that had nothing to do with it -- which happened twice while the
suite was being written. `run_all()` resets before every scenario, so a
failure points at the scenario that caused it.

The GL context is created once and shared: it is slow to make, and nothing a
scenario does can corrupt it.
"""

import traceback

import glfw
import moderngl
from imgui_bundle import imgui

from core import cell
from core.tick import PHASE_BELT, TICKS_PER_SEC

W, H = 1024, 768
BOARD = 64

#: Populated by init(), which runs at import so that scenario modules can
#: `from tests.harness import board, engine, ...` and get the real objects.
#: These are never rebound afterwards -- load() mutates the board in place
#: rather than replacing it, precisely so references like these stay valid.
orch = None
engine = None
board = None
cam = None
ctx = None
win = None

#: The fake input state the patched glfw getters read.
fake = {"cursor": (0.0, 0.0), "left": False, "right": False, "keys": set()}

_SLOT_DIGITS = None


def init():
    """Create the window, context and orchestrator. Idempotent."""
    global orch, engine, board, cam, ctx, win, _SLOT_DIGITS
    if orch is not None:
        return orch

    glfw.init()
    for k, v in [(glfw.CONTEXT_VERSION_MAJOR, 4),
                 (glfw.CONTEXT_VERSION_MINOR, 3),
                 (glfw.OPENGL_PROFILE, glfw.OPENGL_CORE_PROFILE),
                 (glfw.VISIBLE, glfw.FALSE)]:
        glfw.window_hint(k, v)
    win = glfw.create_window(W, H, "headless", None, None)
    glfw.make_context_current(win)
    ctx = moderngl.create_context()
    ctx.enable(moderngl.BLEND)

    imgui.create_context()
    imgui.get_io().display_size = (W, H)

    glfw.get_cursor_pos = lambda w: fake["cursor"]
    glfw.get_mouse_button = lambda w, b: (
        glfw.PRESS
        if (fake["left"] if b == glfw.MOUSE_BUTTON_LEFT else fake["right"])
        else glfw.RELEASE)
    glfw.get_key = lambda w, k: (
        glfw.PRESS if k in fake["keys"] else glfw.RELEASE)
    glfw.get_framebuffer_size = lambda w: (W, H)
    glfw.get_window_size = lambda w: (W, H)

    from orchestrator import Orchestrator
    orch = Orchestrator(win, ctx, BOARD, BOARD)
    # No imgui backend here, so skip the panel: these tests drive the state
    # machine and the board, not the widgets.
    orch.draw_ui = False
    engine = orch.engine
    board = engine.board
    cam = orch.camera

    _SLOT_DIGITS = {
        glfw.KEY_1: 1, glfw.KEY_2: 2, glfw.KEY_3: 3, glfw.KEY_4: 4,
        glfw.KEY_5: 5, glfw.KEY_6: 6, glfw.KEY_7: 7, glfw.KEY_8: 8,
        glfw.KEY_9: 9, glfw.KEY_0: 0,
    }
    return orch


def reset():
    """Return everything to a known state, between scenarios."""
    fake["cursor"] = (0.0, 0.0)
    fake["left"] = False
    fake["right"] = False
    fake["keys"].clear()
    # Two frames so any edge-triggered key or button release is consumed and
    # the input state machine lands back in IDLE.
    step(2)
    orch.input.cancel()
    engine.clear_ghosts()
    engine.clear_board()
    orch.last_plan = None
    for slot in orch.toolbelt.slots:
        if slot is None:
            continue
        if hasattr(slot, "reset"):
            slot.reset()
        if hasattr(slot, "direction"):
            slot.direction = cell.DIR_PX
        if hasattr(slot, "flipped"):
            slot.flipped = False
    # An empty hand is the app's default state, so it is the baseline every
    # scenario starts from. It also keeps `reset()` from leaving a stray
    # ghost: the trailing step() below would otherwise have a drag-based tool
    # preview its one-cell idle run, and a scenario opening with
    # `live_count == 0` would fail on a board it never touched.
    orch.toolbelt.clear()
    cam.center[:] = (BOARD / 2, BOARD / 2)
    cam.zoom = 16.0
    # The clock is global state like everything else here: a scenario that
    # paused the sim or ran it far forward must not leave the next one
    # starting mid-timeline. clear_board() above already resets the tick
    # counter, but not the pause flag.
    engine.clock.resume()
    # The tick *rate* is global state too, and clock.reset() deliberately
    # keeps it (a rate change is configuration, not timeline). A scenario
    # that set 30/s would otherwise leave every later one banking half a
    # tick per frame, so `alpha` lands mid-tick and every item renders
    # half a cell from where a whole-tick calculation expects it.
    engine.clock.set_rate(TICKS_PER_SEC)
    engine.clock.reset()
    # The debug item feed is global state too: a scenario that leaves it on
    # would quietly fill every later scenario's belts.
    engine.debug_feed = None
    for k in engine.belts.stats:
        engine.belts.stats[k] = 0
    for k in engine.gates.stats:
        engine.gates.stats[k] = 0
    # The engine registers belt transport on the belt phase at construction.
    # A scenario that clears the phase instead of removing its own callback
    # silently stops the simulation for everything after it -- which happened,
    # and surfaced three files later as "the belt phase ran but nothing
    # moved". Catch it here, where the cause is still obvious.
    assert engine.clock.phase_count(PHASE_BELT) >= 1, (
        "the engine's belt phase callback has been lost -- a scenario "
        "cleared the phase rather than calling clock.off_phase()")
    step()


# --- the vocabulary scenarios are written in -------------------------------
def step(n=1):
    for _ in range(n):
        orch.frame(1.0 / 60.0)


def put_cursor_at_cell(cx, cy):
    """Place the fake cursor at the centre of a cell."""
    put_cursor_at(cx + 0.5, cy + 0.5)


def put_cursor_at(wx, wy):
    """Place the fake cursor at an arbitrary world point."""
    sx, sy = cam.world_to_screen(wx, wy, (W, H))
    fake["cursor"] = (float(sx), float(sy))


def click():
    """A full press+release without moving: the build gesture.

    Building happens on release, because a press alone is ambiguous -- it
    might become a camera drag.
    """
    fake["left"] = True
    step()
    fake["left"] = False
    step()


def drag_px(dx, dy, steps=4):
    """Hold left and move the cursor by a pixel delta, to pan the camera."""
    sx, sy = fake["cursor"]
    fake["left"] = True
    step()
    for i in range(1, steps + 1):
        fake["cursor"] = (sx + dx * i / steps, sy + dy * i / steps)
        step()
    fake["left"] = False
    step()


def press_key(key, frames=1):
    """Tap a key: held for `frames`, then released and settled."""
    fake["keys"].add(key)
    step(frames)
    fake["keys"].discard(key)
    step()


def select_tool(name):
    """Press the number key whose slot holds the named tool.

    By name rather than by digit: the slot order shifts whenever a tool is
    added, and every scenario cares about *which tool*, never which key
    reaches it.
    """
    idx = next(i for i, s in enumerate(orch.toolbelt.slots)
               if s is not None and s.name == name)
    assert idx < 10, f"{name} is past the number keys"
    digit = 0 if idx == 9 else idx + 1
    key = next(k for k, d in _SLOT_DIGITS.items() if d == digit)
    press_key(key)
    assert orch.toolbelt.active.name == name, orch.toolbelt.active.name
    return orch.toolbelt.active


def cells_of_type(t):
    return int((cell.cell_type_arr(board.grid[:, :, 0]) == t).sum())


def ghost_cell_count():
    return int((cell.ghost_state_arr(board.grid[:, :, cell.CH_Z])
                == cell.GHOST_PENDING).sum())


def frustrated_cell_count():
    return int((cell.ghost_state_arr(board.grid[:, :, cell.CH_Z])
                == cell.GHOST_FRUSTRATED).sum())


def ok(msg):
    print("  OK:", msg)


# --- the runner ------------------------------------------------------------
#: Build everything at import, so scenario modules can bind the objects by
#: name at their own import time.
init()


#: Names of everything that has failed this run, across every run_all() call.
failures = []


def run_all(scenarios, only=None, reset_each=True):
    """Run each scenario, returning the number that failed.

    `only` filters by substring, so a single scenario can be re-run while
    working on it. `reset_each` is False for tests that touch no board state
    and so need nothing reset (the static GLSL checks).

    A scenario that raises is reported and the run continues: one broken
    gesture should not hide the state of everything after it.
    """
    ran = 0
    for fn in scenarios:
        if only and only not in fn.__name__:
            continue
        name = fn.__name__.removeprefix("test_").replace("_", " ")
        if reset_each:
            reset()
        print(f"=== {name} ===")
        ran += 1
        try:
            fn()
        except Exception:
            failures.append(name)
            print(f"  FAIL: {name}")
            traceback.print_exc()
    return ran


def report():
    """Print the final tally. Returns a process exit code."""
    print()
    if failures:
        print(f"{len(failures)} FAILED:")
        for name in failures:
            print(f"  - {name}")
        return 1
    print("ALL TESTS PASSED")
    return 0
