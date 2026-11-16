// Saving: a board written by Board.toNpz() must read back identical here,
// and must load in the desktop through core/persistence.py, array for array.

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync, writeFileSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { Board } from "../board.js";
import { readNpz } from "../npz.js";

const here = dirname(fileURLToPath(import.meta.url));
const root = join(here, "..", "..");
const T = JSON.parse(readFileSync(join(here, "..", "types.json"), "utf8"));
const buf = (b) => b.buffer.slice(b.byteOffset, b.byteOffset + b.byteLength);

for (const save of ["cpu.npz", "ram.npz"]) {
  test(`${save}: a written board reads back identical, here and on the desktop`, async () => {
    const src = readFileSync(join(root, save));
    const board = await Board.fromNpz(T, buf(src));
    // An edit, so the save is not just the file echoed back.
    const row = board.structs.find(s => s.emit !== null).row;
    board.setEmit(row, 3.5);
    const bytes = await board.toNpz();

    const before = await readNpz(buf(src));
    const after = await readNpz(buf(bytes));
    // A save from before notes existed has no annotations entry; writing one
    // holding "[]" is what the desktop's own save does.
    if (!before.annotations) assert.equal(after.annotations.data, "[]");
    else assert.equal(after.annotations.data, before.annotations.data);
    delete after.annotations;
    delete before.annotations;
    assert.deepEqual(Object.keys(after).sort(), Object.keys(before).sort());
    for (const k of Object.keys(before)) {
      if (k === "entities") continue;
      assert.deepEqual(after[k].shape, before[k].shape, k);
      assert.deepEqual(after[k].data, before[k].data, k);
    }
    const E = T.entity;
    const want = before.entities.data.slice();
    want[row * E.STRIDE + E.F_EMIT] = 3.5;
    assert.deepEqual(after.entities.data, want);

    // The desktop's loader, which checks the format version and the stride.
    const dir = mkdtempSync(join(tmpdir(), "gates-npz-"));
    try {
      const path = join(dir, "web.npz");
      writeFileSync(path, bytes);
      const out = execFileSync("python3", ["-c", `
import sys, numpy as np
sys.path.insert(0, ${JSON.stringify(root)})
from core.gameboard import Gameboard
from core import persistence
a = Gameboard(512, 512); persistence.load(a, ${JSON.stringify(path)})
b = Gameboard(512, 512); persistence.load(b, ${JSON.stringify(join(root, save))})
assert (a.grid == b.grid).all()
d = np.argwhere(a.entities != b.entities)
assert len(d) == 1 and tuple(d[0]) == (${row}, ${E.F_EMIT}), d
assert a.entities[${row}, ${E.F_EMIT}] == 3.5
print("ok")
`], { encoding: "utf8" });
      assert.match(out, /ok/);
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });
}
