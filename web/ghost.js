// A stamp at a board position, in the renderer's own shapes, so ghosts draw
// with the same code as the real thing. Used for the selection (entities
// lifted off the board) and for a paste.

import { typeByName } from "./stamp.js";

// `entries` and `at` as Board.placeEntries() takes them. Returns
//   { belts, unders, structs, blocked, boxes }
// where `boxes` is each entry's footprint and whether it is blocked.
export function stampGhost(board, entries, at) {
  const T = board.T, C = T.cell, W = board.width;
  const belts = [], unders = [], structs = [], blocked = [], boxes = [];
  // A ghost belt's incoming arm must see the other ghosts, not the empty
  // cells (or the obstacles) under them: a lifted run still reads as a run.
  const mine = new Map();   // cell key -> mask of directions it emits toward
  const claim = (x, y, mask) => { const k = y * W + x; mine.set(k, (mine.get(k) || 0) | mask); };

  entries.forEach((e, i) => {
    const { id, td } = typeByName(T, e.type);
    const o = td.orient[`${e.dir}${e.flip ? 1 : 0}`];
    const [x, y] = at[i];
    const b = board.entryBlocked(e, x, y);
    blocked.push(...b);
    boxes.push({ x, y, w: o.w, h: o.h, blocked: b.length > 0 });
    if (id === C.TYPE_BELT) {
      claim(x, y, 1 << e.dir);
      belts.push({ x, y, dir: e.dir, inDir: -1, seed: e.seed === null || e.seed === undefined ? 0 : e.seed > 0 ? 1 : -1 });
    } else if (id === C.TYPE_UNDER) {
      claim(x, y, e.exit ? 1 << e.dir : 0);
      const p = e.partner;
      unders.push({ x, y, dir: e.dir, exit: e.exit,
        span: !e.exit && p !== null && p !== undefined && at[p] ? at[p] : null });
    } else {
      for (let lx = 0; lx < o.w; lx++) for (let ly = 0; ly < o.h; ly++) claim(x + lx, y + ly, 0);
      for (const [lx, ly, f] of o.outputs) claim(x + lx, y + ly, 1 << f);
      structs.push({ type: id, x, y, dir: e.dir, flip: e.flip ? 1 : 0, w: o.w, h: o.h, o,
        emit: td.name === "source" ? (e.emit ?? 0) : null });
    }
  });

  const emits = (nx, ny, d) => {
    const k = ny * W + nx;
    return mine.has(k) ? !!(mine.get(k) & (1 << d)) : board.emitsToward(nx, ny, d);
  };
  for (const b of belts) b.inDir = board.incoming(b.x, b.y, b.dir, emits);
  return { belts, unders, structs, blocked, boxes };
}
