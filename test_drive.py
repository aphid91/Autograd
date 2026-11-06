"""Run the test suite.

    python test_drive.py              # everything
    python test_drive.py underground  # only scenarios matching a substring

Two kinds of test live under tests/:

  tests/test_scenarios.py  drive the orchestrator headlessly and assert board
                           state. Each is independent -- tests/harness.py
                           resets the board, camera, input and toolbelt before
                           every one, so a failure names the scenario that
                           caused it.
  tests/test_glsl.py       static checks on the generated GLSL and the
                           hand-written code that consumes it. No GL context,
                           no board.
  tests/test_tick.py       the simulation clock as wired into the frame loop.
                           core/tick.py self-checks the Clock in isolation;
                           these cover the orchestrator wiring and the keys.

core/cell.py and core/entity.py also run self-checks at import, so simply
importing anything here exercises the cell encoding and the entity geometry.
"""

import sys

from tests import (
    harness, test_glsl, test_scenarios, test_segments, test_tick,
)


def _scenarios(module):
    """Every test_* function in a module, in definition order."""
    order = {name: i for i, name in enumerate(vars(module))}
    return sorted(
        (v for k, v in vars(module).items()
         if k.startswith("test_") and callable(v)),
        key=lambda f: order[f.__name__])


def main(argv):
    only = argv[1] if len(argv) > 1 else None
    # Static checks first: if the generator or the shader is broken, the
    # scenarios fail too, and the cause is much clearer here.
    ran = harness.run_all(_scenarios(test_glsl), only=only, reset_each=False)
    ran += harness.run_all(_scenarios(test_scenarios), only=only)
    ran += harness.run_all(_scenarios(test_tick), only=only)
    ran += harness.run_all(_scenarios(test_segments), only=only)
    if not ran:
        print(f"no tests matched {only!r}")
        return 1
    return harness.report()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
