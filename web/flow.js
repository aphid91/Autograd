// Where every item is drawn: on a belt, and on its way into, through and out
// of a machine. Pure functions over a Sim and its forecast -- no canvas, no
// DOM -- so Node can test that the picture is continuous across a tick.
//
// THE MODEL. An item is drawn on one path, from belt to belt:
//
//   belt run ... head's front edge = the gate's IN-EDGE
//     -> the input's WAIT point            (an input held in the gate sits here)
//     -> the output's OUT-EDGE             (an output zone holding a result sits here,
//                                           half on the gate and half on the belt)
//     = the next run's back edge ... belt run
//
// and the value it shows changes at the wait point, as if computed there.
//
// BELTS ARE DRAWN HALF A CELL BACK. The closed form's position 0 is a run's
// tail-cell centre and its length L is one cell past the head's centre,
// i.e. inside the next machine. Drawn at `pos - 0.5` instead, position 0 is
// the tail's back edge -- the out-edge an item is pushed from -- and L is the
// head's front edge, the in-edge where an item waits for a full input. So
// both ends of a run meet a machine exactly where its ports are.
//
// THE TICK IS ANIMATED FROM ITS FORECAST. The state on screen is "after tick
// t"; Sim.forecast() says what step t+1 will do, and alpha is how far into it
// we are. So over one tick:
//   - a run's head that will POP moves on to the in-edge (it is usually
//     there already) and then to the wait point;
//   - an input its gate will CONSUME moves from the wait point along the
//     output it takes (chosen from the machine's opcodes, see linkFor) to
//     the out-edge, and on: onto the run that will take the PUSH, as far as
//     the closed form will have it next tick (a cell, unless packed), or into the next gate's wait point for a DIRECT hand-off; a
//     result for a dangling output stops at the out-edge and is gone;
//   - a held output zone that will be pushed or handed off does the same
//     from the out-edge; a generator's result appears on the out-edge;
//   - everything else is where the state puts it.
// Each moving item covers its whole path at a constant speed over the tick.
// The paths' ends are where the next tick's state draws the same item, so
// the motion is continuous (tests/flow.test.mjs checks exactly that).

// --- the machines' internal links -------------------------------------------
//
// Which output port each input port's item can leave by, as indices into the
// type's INPUTS and OUTPUTS (core/entity.py). Geometry only: WHICH link an
// item takes on a given fire is read from the opcodes (linkFor), so nothing
// here restates a machine's arithmetic. `diag` cuts straight from the wait
// point to the output's edge rather than through the cell centres (Swap's
// exchange). Sink (legacy) has none: what enters it simply vanishes at the
// edge. Source has no inputs: its results appear on the out-edge.
export const LINKS = {
  Shear: [[0], [1]],                       // x down cell 0; y across both cells -> x+y
  Mac: [[0], [1], [2]],                    // x, y down their cells; z across -> z + x*y
  Max: [[0], [1]],
  Reciprocal: [[0]],
  Floor: [[0]],                            // down into cell 0, across to cell 1's east face
  Route: [[0, 1], [2]],                    // a: left or right on b's sign; b passes
  Merge: [[0], [0], [1]],                  // a or b, as c selects; c passes
  Swap: [[0], [1, { out: 2, diag: true }], [2, { out: 1, diag: true }]],
};

const link = (l) => (typeof l === "number" ? { out: l, diag: false } : l);

// --- geometry ---------------------------------------------------------------
// Points are in the PLACED footprint's continuous coordinates: cell (lx, ly)
// covers [lx, lx+1) x [ly, ly+1). Built from the placed port tables in
// types.json, so a turned or mirrored machine needs no transform here.

const STEP = [[1, 0], [0, 1], [-1, 0], [0, -1]];
const centre = (lx, ly) => [lx + 0.5, ly + 0.5];
const edge = (lx, ly, f) => [lx + 0.5 + 0.5 * STEP[f][0], ly + 0.5 + 0.5 * STEP[f][1]];
const same = (a, b) => Math.abs(a[0] - b[0]) < 1e-9 && Math.abs(a[1] - b[1]) < 1e-9;
const dist = (a, b) => Math.hypot(b[0] - a[0], b[1] - a[1]);

function dedupe(pts) {
  const out = [];
  for (const p of pts) if (!out.length || !same(out[out.length - 1], p)) out.push(p);
  return out;
}

function polyLength(pts) {
  let n = 0;
  for (let i = 1; i < pts.length; i++) n += dist(pts[i - 1], pts[i]);
  return n;
}

// The point `t` of the way along a polyline, by length.
export function along(pts, t) {
  if (pts.length === 1) return pts[0];
  let want = Math.min(1, Math.max(0, t)) * polyLength(pts);
  for (let i = 1; i < pts.length; i++) {
    const d = dist(pts[i - 1], pts[i]);
    if (want <= d || i === pts.length - 1) {
      const u = d > 0 ? Math.min(1, want / d) : 0;
      return [pts[i - 1][0] + (pts[i][0] - pts[i - 1][0]) * u, pts[i - 1][1] + (pts[i][1] - pts[i - 1][1]) * u];
    }
    want -= d;
  }
  return pts[pts.length - 1];
}

// Split a polyline at length `d` into [before, after], both containing the cut.
function splitAt(pts, d) {
  for (let i = 1; i < pts.length; i++) {
    const s = dist(pts[i - 1], pts[i]);
    if (d <= s + 1e-9) {
      const u = s > 0 ? d / s : 0;
      const cut = [pts[i - 1][0] + (pts[i][0] - pts[i - 1][0]) * u, pts[i - 1][1] + (pts[i][1] - pts[i - 1][1]) * u];
      return [dedupe([...pts.slice(0, i), cut]), dedupe([cut, ...pts.slice(i)])];
    }
    d -= s;
  }
  return [pts, [pts[pts.length - 1]]];
}

function collinear(pts) {
  for (let i = 2; i < pts.length; i++) {
    const [a, b, c] = [pts[i - 2], pts[i - 1], pts[i]];
    if (Math.abs((b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])) > 1e-9) return false;
  }
  return true;
}

// One placed orientation of a machine:
//   ins[i]  = { edge, wait, inHalf: in-edge -> wait, outs: Map(outPort -> wait -> out-edge) }
//   outs[j] = { edge }
// The wait point is the midpoint of the path when every link from that
// input runs straight through (Shear's y waits on the boundary between its
// cells); otherwise it is the centre of the input's own cell, so a lane that
// turns or branches (Floor, Route's a, Merge's a and b, Swap's y and z)
// waits before it commits to a direction.
export function geometry(td, orient) {
  const links = LINKS[td.class] || null;
  const outs = orient.outputs.map(([lx, ly, f]) => ({ edge: edge(lx, ly, f), lx, ly, f }));
  const ins = orient.inputs.map(([lx, ly, f], i) => {
    const inEdge = edge(lx, ly, f), ci = centre(lx, ly);
    const mine = links?.[i]?.map(link) || [];
    if (!mine.length) return { edge: inEdge, wait: null, inHalf: [inEdge], outs: new Map() };
    const full = mine.map(l => {
      const o = outs[l.out];
      return l.diag ? null : dedupe([inEdge, ci, centre(o.lx, o.ly), o.edge]);
    });
    const straight = full.every(p => p && collinear(p));
    const res = { edge: inEdge, outs: new Map() };
    if (straight) {
      const [a] = splitAt(full[0], polyLength(full[0]) / 2);
      res.inHalf = a;
      res.wait = a[a.length - 1];
      mine.forEach((l, k) => res.outs.set(l.out, splitAt(full[k], polyLength(full[k]) / 2)[1]));
    } else {
      res.inHalf = dedupe([inEdge, ci]);
      res.wait = ci;
      mine.forEach((l, k) => {
        const o = outs[l.out];
        res.outs.set(l.out, l.diag ? [ci, o.edge] : full[k].slice(full[k].findIndex(p => same(p, ci))));
      });
    }
    return res;
  });
  return { ins, outs };
}

const geoCache = new WeakMap();
function geomFor(td, key, orient) {
  let byKey = geoCache.get(td);
  if (!byKey) geoCache.set(td, (byKey = new Map()));
  let g = byKey.get(key);
  if (!g) byKey.set(key, (g = geometry(td, orient)));
  return g;
}

// --- which link a fire takes ------------------------------------------------
//
// The input channels an output's value came from, given the inputs it was
// computed from -- read off the opcode, so Route's gate and Swap's select are
// the machine's own decision rather than a second copy of it here.
export function sourcesOf(T, op, values) {
  const O = T.sim.OPS, [code, ...a] = op;
  const v = (i) => (values[i] === null || values[i] === undefined ? 0 : values[i]);
  switch (code) {
    case O.OP_IN: case O.OP_RECIP: case O.OP_FLOOR: return [a[0]];
    case O.OP_ADD: case O.OP_MAX: return [a[0], a[1]];
    case O.OP_MAC: return [a[0], a[1], a[2]];
    case O.OP_SEL: return [v(a[0]) > 0 ? a[1] : a[2]];
    case O.OP_GATED: return (v(a[0]) > 0) === !!a[2] ? [a[1]] : [];
    default: return [];
  }
}

// The output port input port `i` leaves by on a fire with these input values
// and results, or -1 (a lane not consumed, or a type with no links).
export function linkFor(T, td, i, values, results) {
  const s = td.sim, mine = LINKS[td.class]?.[i];
  if (!mine) return -1;
  const ch = s.in_ch[i];
  for (const l of mine.map(link)) {
    const oc = s.out_ch[l.out];
    if (results[oc] === null || results[oc] === undefined) continue;
    if (sourcesOf(T, s.ops[oc], values).includes(ch)) return l.out;
  }
  return -1;
}

// --- where a run draws a position ---------------------------------------------
//
// `q` is a drawn distance along the run, in cells from the tail cell's
// centre (so the closed form's position minus half a cell). The cell whose
// centre is nearest carries it: ahead of its centre along its facing, behind
// it along the way the run came in -- from the previous cell, or, at the
// tail, from the machine port feeding it -- so an item rounds a corner on
// the belt rather than cutting across it. Null where no drawn cell is within
// `reach` (an underground's buried tiles).
export function runPoint(sim, seg, q, reach) {
  const offs = seg.offs, n = offs.length;
  let lo = 0, hi = n - 1;
  while (lo < hi) {
    const mid = (lo + hi + 1) >> 1;
    if (offs[mid] <= q) lo = mid; else hi = mid - 1;
  }
  let i = lo;
  if (i + 1 < n && offs[i + 1] - q < q - offs[i]) i++;
  const d = q - offs[i];
  if (d > reach || d < -reach) return null;
  const [cx, cy] = seg.cells[i];
  let v = STEP[seg.dirs[i]];
  if (d < 0) {
    if (i > 0) {
      const [px, py] = seg.cells[i - 1];
      if (Math.abs(cx - px) + Math.abs(cy - py) === 1) v = [cx - px, cy - py];
    } else if (seg.src && sim.alive(seg.src[0])) {
      v = STEP[seg.src[1]];   // the feeding port's face points into the run
    }
  }
  return [cx + 0.5 + v[0] * d, cy + 0.5 + v[1] * d];
}

// The points along a run from drawn distance qa to qb (qa <= qb): the ends,
// and every cell centre between them, where a corner turns.
export function runPath(sim, seg, qa, qb, reach) {
  const out = [runPoint(sim, seg, qa, reach)];
  for (let i = 0; i < seg.offs.length; i++) {
    if (seg.offs[i] > qa && seg.offs[i] < qb) out.push(runPoint(sim, seg, seg.offs[i], reach));
  }
  out.push(runPoint(sim, seg, qb, reach));
  return out.filter(Boolean);
}

// --- every item, this frame -----------------------------------------------------
//
// Returns { pts, ends }: `pts` is flat (x, y, value) triples in world cells;
// `ends` flags (per triple) an item drawn for the last time this tick -- a
// result destroyed at a dangling output, or one entering a machine with no
// links (Sink). `visible(x0, y0, x1, y1)` culls whole runs and gates.
export function itemPoints(sim, alpha, opts = {}) {
  const T = sim.T, E = T.entity, UNIT = sim.UNIT;
  const visible = opts.visible || (() => true);
  const reach = opts.reach ?? 0.72;
  const F = sim.forecast();
  const pts = [], ends = [];
  const put = (p, value, end = false) => {
    if (!p) return;
    pts.push(p[0], p[1], value);
    ends.push(end);
  };

  const gateGeom = (g) => {
    const key = `${sim.dirOf(g.row)}${sim.f(g.row, E.F_FLIPPED) ? 1 : 0}`;
    const [ox, oy] = sim.originOf(g.row);
    return { geo: geomFor(g.td, key, sim.orientOf(g.row)), ox, oy };
  };
  const world = (gg, pts) => pts.map(([x, y]) => [gg.ox + x, gg.oy + y]);
  const inPortOf = (g, ch, x, y, face) =>
    g.inFaces.findIndex(p => p.ch === ch && (x === undefined || (p.x === x && p.y === y && p.face === face)));

  // What happens after an item reaches out-port j of gate g's channel ch:
  // the rest of its path this tick (excluding the out-edge itself), and
  // whether it ends there.
  const onward = (g, ch) => {
    const push = F.pushFrom.get(`${g.row}:${ch}`);
    if (push) {
      // Onto the run, as far as the closed form will have it next tick.
      return { more: runPath(sim, push.seg, -0.5, push.end - 0.5, reach).slice(1), end: false };
    }
    const d = F.directFrom.get(`${g.row}:${ch}`);
    if (d) {
      const tg = sim.gateOf.get(d[2]);
      const i = tg ? inPortOf(tg, d[3]) : -1;
      if (i >= 0) {
        const gg = gateGeom(tg), ip = gg.geo.ins[i];
        if (ip.wait) return { more: world(gg, ip.inHalf.slice(1)), end: false };
        return { more: [], end: true };
      }
    }
    return { more: [], end: !g.drains.includes(ch) };
  };
  const outPortOf = (g, ch) => {
    // A channel has one port on every machine but Sink (which has no
    // outputs); prefer the one a run or a hand-off actually uses.
    const push = F.pushFrom.get(`${g.row}:${ch}`);
    if (push) {
      const [tx, ty] = push.seg.cells[0], s = STEP[push.seg.src[1]];
      const j = g.outFaces.findIndex(p => p.ch === ch && p.x === tx - s[0] && p.y === ty - s[1]);
      if (j >= 0) return j;
    }
    return g.outFaces.findIndex(p => p.ch === ch);
  };

  // Belts.
  for (const seg of sim.segments.values()) {
    if (seg.head === seg.tail) continue;
    const b = seg.box;
    if (!visible(b.x0 - 1, b.y0 - 1, b.x1 + 1, b.y1 + 1)) continue;
    const pop = F.pops.has(seg);
    for (let k = seg.head; k < seg.tail; k++) {
      const value = seg.payload[k % seg.cap];
      if (k === seg.head && pop) {
        // Into the gate: in-edge -> wait point, over this tick.
        const [row, ch] = sim.livePort(seg.dstPort) || [];
        const g = row !== undefined ? sim.gateOf.get(row) : null;
        const [hx, hy] = seg.cells[seg.cells.length - 1], s = STEP[seg.dirs[seg.dirs.length - 1]];
        const i = g ? inPortOf(g, ch, hx + s[0], hy + s[1], (seg.dirs[seg.dirs.length - 1] + 2) % 4) : -1;
        const gg = i >= 0 ? gateGeom(g) : null;
        // From wherever the head is now: a run just unblocked pops a head
        // still a cell short, since the step advances the anchor first.
        const q0 = sim.positionOf(seg, k, sim.tick, 0) / UNIT - 0.5;
        const path = runPath(sim, seg, q0, seg.length - 0.5, reach);
        if (gg && gg.geo.ins[i].wait) put(along([...path, ...world(gg, gg.geo.ins[i].inHalf.slice(1))], alpha), value);
        else put(along(path, alpha), value, true);
        continue;
      }
      const q = sim.positionOf(seg, k, sim.tick, alpha) / UNIT - 0.5;
      put(runPoint(sim, seg, q, reach), value);
    }
  }

  // Gates: held inputs, held outputs, and this tick's fires.
  for (const g of sim.gates) {
    const row = g.row, s = g.sim;
    const fire = F.fires.get(row);
    const h = sim.f(row, E.F_H), w = sim.f(row, E.F_W);
    const [ox, oy] = sim.originOf(row);
    if (!visible(ox - 1, oy - 1, ox + w + 1, oy + h + 1)) continue;
    let gg = null;
    const geo = () => (gg || (gg = gateGeom(g)));

    for (let c = 0; c < s.n_in; c++) {
      if (!sim.inHeld(row, c)) continue;
      const i = inPortOf(g, c);
      if (i < 0) continue;
      const ip = geo().geo.ins[i];
      if (!ip.wait) continue;                       // Sink: gone once inside
      if (fire && fire.consumed.includes(c)) {
        const j = linkFor(T, g.td, i, fire.values, fire.results);
        if (j < 0) continue;
        const oc = s.out_ch[j];
        const { more, end } = fire.produced.includes(oc) ? onward(g, oc) : { more: [], end: true };
        put(along([...world(geo(), ip.outs.get(j)), ...more], alpha), fire.results[oc], end);
      } else {
        put([geo().ox + ip.wait[0], geo().oy + ip.wait[1]], sim.inValue(row, c));
      }
    }

    for (let c = 0; c < s.n_out; c++) {
      const held = sim.outHeld(row, c);
      const made = !held && fire && s.n_in === 0 && fire.produced.includes(c);
      if (!held && !made) continue;
      const j = outPortOf(g, c);
      if (j < 0) continue;
      const e = geo().geo.outs[j].edge, at = [geo().ox + e[0], geo().oy + e[1]];
      const { more } = onward(g, c);
      // A generator's new item that will not move this tick (pushed onto a
      // packed run, or held) would sit on the edge on top of the item just
      // leaving it. It is drawn from the next tick, where the state has it.
      if (made && polyLength([at, ...more]) < 1e-9) continue;
      put(along([at, ...more], alpha), held ? sim.outValue(row, c) : fire.results[c]);
    }
  }
  return { pts, ends };
}
