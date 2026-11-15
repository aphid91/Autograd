// The belt run through a cell: the cells core/segments.py would put in one
// segment. Used by long press, which selects (or deselects) a whole run.
//
// Written against two callbacks rather than the Board, because a run can be
// partly on the board and partly lifted into the selection, and the trace
// has to see both:
//
//   node(x, y)  -> { kind: "belt" | "under", dir, exit, partner: [x, y] | null,
//                    key } for a belt-like cell, or null for anything else
//                    (empty, a machine, off the board)
//   taken(x, y, dir) -> the side a cell facing `dir` takes from, as
//                    Board.incoming() answers it (take_dir: straight
//                    through first, then counter-clockwise), or -1
//
// A run stops where the desktop's does: at a machine, at a cell whose
// take_dir picked someone else (the loser of a merge is not connected), at
// an unpaired underground entrance, and where it would close on itself.

const STEPS = [[1, 0], [0, 1], [-1, 0], [0, -1]];

// Every cell of the run through (x, y), as the node objects, upstream first.
// Empty if (x, y) is not a belt or underground.
export function traceRun(node, taken, x, y) {
  const start = node(x, y);
  if (!start) return [];
  const seen = new Set([start.key]);

  // Upstream: who feeds this cell, if that is a belt-like cell.
  const back = [];
  let c = { ...start, x, y };
  for (;;) {
    let px, py;
    if (c.kind === "under" && c.exit) {
      if (!c.partner) break;
      [px, py] = c.partner;
    } else {
      const d = taken(c.x, c.y, c.dir);
      if (d < 0) break;
      px = c.x + STEPS[d][0]; py = c.y + STEPS[d][1];
    }
    const n = node(px, py);
    // An entrance only feeds its own exit (the partner branch above); a
    // machine ends the run (a machine-fed cell is a tail).
    if (!n || (n.kind === "under" && !n.exit && !(c.kind === "under" && c.exit))) break;
    if (seen.has(n.key)) break;
    seen.add(n.key);
    c = { ...n, x: px, y: py };
    back.push(c);
  }

  // Downstream: the cell this one feeds, if it takes from us.
  const fwd = [];
  c = { ...start, x, y };
  for (;;) {
    let nx, ny;
    if (c.kind === "under" && !c.exit) {
      if (!c.partner) break;
      [nx, ny] = c.partner;
      const n = node(nx, ny);
      if (!n || seen.has(n.key)) break;
      seen.add(n.key);
      c = { ...n, x: nx, y: ny };
      fwd.push(c);
      continue;
    }
    nx = c.x + STEPS[c.dir][0]; ny = c.y + STEPS[c.dir][1];
    const n = node(nx, ny);
    // An exit surfaces from its partner, never from a neighbour.
    if (!n || (n.kind === "under" && n.exit)) break;
    if (taken(nx, ny, n.dir) !== (c.dir + 2) % 4) break;
    if (seen.has(n.key)) break;
    seen.add(n.key);
    c = { ...n, x: nx, y: ny };
    fwd.push(c);
  }
  return [...back.reverse(), { ...start, x, y }, ...fwd];
}
