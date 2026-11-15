// node --test web/tests/*.test.mjs
//
// The ghost's footprint against Entity.origin_for_cursor() itself, so the
// port cannot drift from the desktop's rule.

import { test } from "node:test";
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { originFor } from "../placer.js";

const ROOT = fileURLToPath(new URL("../..", import.meta.url));

test("originFor agrees with the desktop at every extent and sub-cell position", () => {
  // Points on a 0.1 lattice across a few cells, for every footprint shape
  // the machines have, at both orientations of an oblong.
  const shapes = [[1, 1], [2, 1], [1, 2], [2, 2], [3, 1], [3, 2]];
  const pts = [];
  for (let x = 3; x < 5; x += 0.1) for (let y = 7; y < 8.05; y += 0.25) pts.push([+x.toFixed(2), +y.toFixed(2)]);
  const py = `
import json, sys, math
sys.path.insert(0, ${JSON.stringify(ROOT)})
from core.entity import Entity
shapes, pts = json.loads(sys.argv[1]), json.loads(sys.argv[2])
out = []
for w, h in shapes:
    cls = type("T", (Entity,), {"FOOTPRINT": (w, h)})
    for px, py in pts:
        cx, cy = math.floor(px), math.floor(py)
        out.append(cls.origin_for_cursor(cx, cy, 0, (px - cx, py - cy)))
print(json.dumps(out))`;
  const want = JSON.parse(execFileSync("python3", ["-c", py, JSON.stringify(shapes), JSON.stringify(pts)]));
  let k = 0;
  for (const [w, h] of shapes) {
    for (const [px, py] of pts) {
      assert.deepEqual(originFor(w, h, px, py), want[k++], `${w}x${h} at (${px}, ${py})`);
    }
  }
});

test("the point is always inside what it places", () => {
  for (const [w, h] of [[1, 1], [2, 1], [1, 2], [2, 2]]) {
    for (let px = 0; px < 3; px += 0.05) {
      const [ox] = originFor(w, h, px, 0);
      assert.ok(px >= ox && px <= ox + w, `${w}x${h} at ${px}`);
    }
  }
});
