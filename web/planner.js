// The belt planner: what a run between two cells looks like, and where it
// tunnels under what is in the way.
//
// Pure functions over a `free(x, y)` predicate -- no board, no DOM -- so the
// rules can be tested in Node. The path geometry is a port of
// core/engine_paths.py and BeltBuilder.plan(); keep the two in step.

export const ROUTE_H_FIRST = 0;
export const ROUTE_V_FIRST = 1;
export const ROUTE_LINE = 2;
export const ROUTE_NAMES = ["H → V", "V → H", "Line"];

const STEPS = [[1, 0], [0, 1], [-1, 0], [0, -1]];
const stepDir = (dx, dy) => STEPS.findIndex(s => s[0] === dx && s[1] === dy);

function span(a, b) {
  const out = [], s = b >= a ? 1 : -1;
  for (let v = a; v !== b + s; v += s) out.push(v);
  return out;
}

// Integer line as a 4-connected staircase: belts cannot step diagonally.
export function bresenham(x0, y0, x1, y1) {
  const cells = [[x0, y0]];
  const dx = Math.abs(x1 - x0), dy = Math.abs(y1 - y0);
  const sx = x1 >= x0 ? 1 : -1, sy = y1 >= y0 ? 1 : -1;
  let x = x0, y = y0, err = dx - dy;
  while (x !== x1 || y !== y1) {
    const e2 = 2 * err;
    if (e2 > -dy && x !== x1) { err -= dy; x += sx; }
    else if (y !== y1) { err += dx; y += sy; }
    else x += sx;
    cells.push([x, y]);
  }
  return cells;
}

// Cells from start to end, inclusive, in travel order (engine_paths.plan_path).
export function planPath([sx, sy], [ex, ey], route) {
  if (route === ROUTE_LINE) return bresenham(sx, sy, ex, ey);
  const cells = [];
  if (route === ROUTE_H_FIRST) {
    for (const x of span(sx, ex)) cells.push([x, sy]);
    for (const y of span(sy, ey)) cells.push([ex, y]);
  } else {
    for (const y of span(sy, ey)) cells.push([sx, y]);
    for (const x of span(sx, ex)) cells.push([x, ey]);
  }
  const out = [cells[0]];
  for (const c of cells.slice(1)) {
    const l = out[out.length - 1];
    if (c[0] !== l[0] || c[1] !== l[1]) out.push(c);
  }
  return out;
}

// Each cell faces its successor; the last inherits its predecessor's
// facing. Derived from the UNFILTERED path, so a belt's facing never depends
// on what happens to be in the way (see the root CLAUDE.md).
export function pathDirs(path, fallback = 0) {
  if (path.length === 1) return [fallback];
  const dirs = [];
  for (let i = 0; i + 1 < path.length; i++) {
    dirs.push(stepDir(path[i + 1][0] - path[i][0], path[i + 1][1] - path[i][1]));
  }
  dirs.push(dirs[dirs.length - 1]);
  return dirs;
}

// --- the tip --------------------------------------------------------------
// The last belt of a multi-cell run may go straight or turn left or right,
// never back into the run. Held as an offset from the travel direction (0
// straight, 1 left, 3 right), so "turn right at the end" survives the last
// leg changing heading. A one-cell run has no travel direction; its tip is
// an absolute facing and takes all four.

export function turnTip(run, cw) {
  const path = planPath(run.start, run.end, run.route);
  if (path.length === 1) {
    run.singleDir = cw ? (run.singleDir + 3) % 4 : (run.singleDir + 1) % 4;
    return;
  }
  // Rotate the absolute facing, stepping over "backwards" -- so the three
  // legal facings form a ring the buttons walk round in either direction.
  let off = run.tipTurn;
  do { off = cw ? (off + 3) % 4 : (off + 1) % 4; } while (off === 2);
  run.tipTurn = off;
}

// --- the plan ---------------------------------------------------------------
//
// Returns { cells, pairs } where every path cell is one of:
//   belt     placed as a belt
//   entry    an underground entrance
//   exit     an underground exit
//   buried   inside a bridge: nothing placed (occupied or not)
//   blocked  occupied and not bridged: skipped, tinted red
// and `pairs` lists [entryIndex, exitIndex].
//
// UNDERGROUNDS ARE INSERTED where the run crosses something it cannot build
// on, if the geometry allows. A bridge from cell a (entrance) to cell e
// (exit) needs:
//   - a and e on the run and free -- true of the cells either side of a
//     maximal blocked stretch, unless the stretch touches an end of the run;
//   - every step from a to e in ONE direction D: the pair is a straight span;
//   - the exit's own facing to be D too. An exit only surfaces forward, so a
//     run that turns on the cell straight after the crossing cannot be
//     bridged -- the exit would emit into a cell the run does not continue to;
//   - at most MAX_GAP cells between the ends.
// The entrance's input side is unconstrained: an underground accepts from
// any side, as a belt does.
//
// TWO OBSTACLES ONE FREE CELL APART ARE ONE OBSTACLE. Bridging them
// separately would need the exit of the first and the entrance of the second
// on that single free cell. So clusters separated by exactly one free cell
// are merged and bridged whole or not at all; two or more free cells between
// them leave room for exit-then-entrance, and each is decided on its own.

export function planRun(run, free, maxGap) {
  const path = planPath(run.start, run.end, run.route);
  const dirs = pathDirs(path, run.singleDir);
  const n = path.length;
  if (n > 1) dirs[n - 1] = (dirs[n - 1] + run.tipTurn) % 4;

  const blocked = path.map(([x, y]) => !free(x, y));
  const kind = blocked.map(b => (b ? "blocked" : "belt"));
  const pairs = [];

  // Maximal blocked stretches, as [first, last] indices.
  const groups = [];
  for (let i = 0; i < n; i++) {
    if (!blocked[i]) continue;
    let j = i;
    while (j + 1 < n && blocked[j + 1]) j++;
    groups.push([i, j]);
    i = j;
  }
  // Merge stretches separated by a single free cell.
  const clusters = [];
  for (const g of groups) {
    const last = clusters[clusters.length - 1];
    if (last && g[0] - last[1] === 2) last[1] = g[1];
    else clusters.push([...g]);
  }

  for (const [i, j] of clusters) {
    const a = i - 1, e = j + 1;
    if (a < 0 || e >= n) continue;                 // touches an end of the run
    if (e - a - 1 > maxGap) continue;              // too long to tunnel
    const D = dirs[a];
    let straight = true;
    for (let k = a; k < e; k++) {
      if (stepDir(path[k + 1][0] - path[k][0], path[k + 1][1] - path[k][1]) !== D) {
        straight = false;
        break;
      }
    }
    if (!straight || dirs[e] !== D) continue;
    // A cell can only be one end of one pair. Unreachable given the merge
    // rule (clusters are >= 2 free cells apart), kept as the stated invariant.
    if (kind[a] !== "belt" || kind[e] !== "belt") continue;
    kind[a] = "entry";
    kind[e] = "exit";
    for (let k = a + 1; k < e; k++) kind[k] = "buried";
    pairs.push([a, e]);
  }

  const cells = path.map(([x, y], i) => ({ x, y, dir: dirs[i], kind: kind[i] }));
  return { cells, pairs };
}
