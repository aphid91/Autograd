"""The web simulation against the desktop's CPU tick, tick for tick.

web/sim.js is a port of the desktop's CPU tick with the schedulers taken out.
"Taken out" is only safe if they were behaviour-neutral, and "a port" is only
worth the name if it computes the same thing -- so this runs both on the same
save and compares their whole state after every tick: every segment's id,
position, ring counters, anchor, and every item's entry tick and payload, and
every gate's eight channel slots.

The desktop runs with **constant folding off**, since folding moves a folded
constant's arrival one tick earlier for ever and the web does not fold, and
with both sleep layers latched off (they are behaviour-neutral, but an oracle
should not lean on that). No GPU: the Engine is built without a context.

    python tools/check_web_sim.py                 # every root .npz, 200 ticks
    python tools/check_web_sim.py cpu.npz 500     # one save, 500 ticks

Each run also does a Reset (ctrl+R) partway, which re-derives the segment
index: segment ids come from a free list refilled on every rebuild, so their
order alternates, and the belt phase visits runs in id order. A port that
numbered runs 1..n every time would agree up to the first reset and then
diverge.

Reports the first differing tick and the first differing entry on it. A
vacuous pass is visible: the item and fire counts are printed per save.
"""

import glob
import os
import subprocess
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core.entity import MAX_CHANNELS, Entity  # noqa: E402
from engine import Engine  # noqa: E402


def num(v):
    """An integer, as num() in web/tests/sim_state.mjs prints it."""
    return str(int(v))


def bits(v):
    """A float32's bit pattern, as bits() in web/tests/sim_state.mjs."""
    return format(int(np.float32(v).view(np.uint32)), "x")


def state_line(engine):
    """The desktop's state in the same form as web/tests/sim_state.mjs."""
    parts = [f"t{engine.clock.tick}"]
    segs = engine.segments.segments
    for sid in sorted(segs):
        seg = segs[sid]
        items = []
        for k in range(seg.head, seg.tail):
            slot = k % seg.cap
            items.append(f"{num(seg.enter_tick[slot])}:{bits(seg.payload[slot])}")
        x, y = seg.cells[0]
        parts.append(f"s{sid}@{x},{y}/{seg.length} {seg.head}-{seg.tail} "
                     f"a{num(seg.anchor)} [{' '.join(items)}]")
    ents = engine.board.entities
    for gate in engine.gates.gates():
        row = gate.row
        ch = []
        for c in range(MAX_CHANNELS):
            held = ents[row, Entity.F_IN_HELD + c] > 0
            ch.append(bits(ents[row, Entity.F_IN_VALUE + c]) if held else "_")
        ch.append("|")
        for c in range(MAX_CHANNELS):
            held = ents[row, Entity.F_OUT_HELD + c] > 0
            ch.append(bits(ents[row, Entity.F_OUT_VALUE + c]) if held else "_")
        parts.append(f"g{row} {' '.join(ch)}")
    return " ; ".join(parts)


def desktop_lines(path, ticks, reset_at):
    e = Engine()
    e.gates.set_folding_enabled(False)
    e.gates.set_sleep_enabled(False)
    e.belts.set_sleep_enabled(False)
    e.load_board(path)
    e.sync_segments()
    lines = [state_line(e)]
    items = fired = 0
    for t in range(ticks):
        if t == reset_at:
            e.reset_sim()
        e.clock.request_step(1)
        e.tick(0.0)
        lines.append(state_line(e))
        items = max(items, e.belts.total_items())
    fired = e.gates.stats.get("fired", 0)
    return lines, items, fired


def web_lines(path, ticks, reset_at):
    out = subprocess.run(
        ["node", os.path.join(ROOT, "web", "tests", "sim_dump.mjs"),
         path, str(ticks), str(reset_at)],
        check=True, capture_output=True, text=True)
    return out.stdout.rstrip("\n").split("\n")


def first_difference(a, b):
    pa, pb = a.split(" ; "), b.split(" ; ")
    for x, y in zip(pa, pb):
        if x != y:
            return x, y
    return (f"<{len(pa)} entries>", f"<{len(pb)} entries>")


def check(path, ticks):
    reset_at = ticks // 2
    want, items, fired = desktop_lines(path, ticks, reset_at)
    got = web_lines(path, ticks, reset_at)
    name = os.path.basename(path)
    if len(want) != len(got):
        print(f"FAIL {name}: {len(want)} desktop lines, {len(got)} web lines")
        return False
    for t, (a, b) in enumerate(zip(want, got)):
        if a != b:
            x, y = first_difference(a, b)
            print(f"FAIL {name}: first difference after {t} ticks"
                  f"{' (reset at %d)' % reset_at if t > reset_at else ''}")
            print(f"  desktop: {x[:300]}")
            print(f"  web:     {y[:300]}")
            return False
    print(f"ok   {name}: {ticks} ticks identical, reset at {reset_at}; "
          f"peak {items} items on belts, {fired} fires")
    return True


def main():
    args = sys.argv[1:]
    ticks = 200
    saves = sorted(glob.glob(os.path.join(ROOT, "*.npz")))
    if args:
        saves = [os.path.join(ROOT, args[0]) if not os.path.exists(args[0])
                 else args[0]]
        if len(args) > 1:
            ticks = int(args[1])
    ok = all([check(p, ticks) for p in saves])
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
