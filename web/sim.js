// The simulation: belt segments, the closed-form belt transport, gate firing,
// and the clock. A port of the desktop's CPU tick -- core/segments.py,
// core/belts.py, core/gates.py, core/tick.py and the tick half of engine.py
// -- with none of its schedulers: no wake lists, no gate sleep, no constant
// folding, no GPU. The two optimisations kept are the ones that change the
// cost class rather than shave it: a whole belt run ticks in O(1) (the closed
// form), and a gate that provably cannot act is rejected by a cheap
// occupancy test before the full firing rule runs (the prefilter).
//
// **It matches the desktop tick for tick** (with folding off -- folding
// moves a constant's arrival one tick earlier, permanently). That is why
// some details look arbitrary and are kept anyway:
//
//   - Segment ids come from the desktop's allocator: a free list popped from
//     the end, refilled in creation order on every rebuild. The belt phase
//     visits segments in id order and a run that drains a gate first changes
//     what a later run sees, so the ids decide the result. They alternate
//     between ascending and descending scan order on each rebuild, exactly as
//     the desktop's do.
//   - Rebuild scans the grid row-major (y outer), as numpy's nonzero() does,
//     so the same cells become tails in the same order.
//   - Payloads and gate channels are float32, rounded at the same points:
//     the entity table is the save's own Float32Array, and the rings are
//     Float32Arrays too.
//
// tools/check_web_sim.py runs the desktop's tick and this one on the same
// save and compares every ring and every gate channel, every tick.
//
// **Any edit is a fresh start.** Board.derive() bumps `board.revision` after
// every mutation, and sync() sees it: the index is rebuilt, every item on
// every belt and inside every gate is discarded, and the seeds go back on.
// The desktop does the same for a belt-network edit (an item's position is
// relative to its run, so a run cannot survive being re-derived); here every
// edit of any kind counts. The tick counter is not reset -- that is Reset's
// job, as ctrl+R's on the desktop.

const BELT = 1, UNDER = 7, EMPTY = 0;

export class Sim {
  constructor(T, board) {
    this.T = T;
    const S = T.sim;
    this.UNIT = S.UNIT;
    this.speed = S.SPEED;
    this.spacing = S.SPACING;
    this.board = board;
    this.tick = 0;
    this.seen = -1;                 // board.revision the index was built from
    // The desktop's sid allocator, which outlives a rebuild.
    this.nextSid = 1;
    this.freeSids = [];
    this.segments = new Map();      // sid -> segment, in creation order
    this.order = [];                // the segments by ascending sid
    const n = board.width * board.height;
    this.segAt = new Int32Array(n); // sid per cell, 0 = none
    this.offAt = new Int32Array(n); // offset along that run
    this.gates = [];                // gate records, ascending row
    this.gateOf = new Map();        // row -> gate record
    this.edges = [];                // direct gate-to-gate hand-offs
    this.stats = { rebuilds: 0, fired: 0, pushed: 0, popped: 0, direct: 0, destroyed: 0 };
    // Bumped by every change to the simulation's state (a tick, a rebuild),
    // so a forecast knows when it is stale.
    this.version = 0;
    this.rec = null;                // the forecast's event log, while one runs
    // audio.js's Recorder while audio is on, else null: then the tick's whole
    // audio cost is this null check (step() and structurePhase()).
    this.audio = null;
    // pixel.js's Screen while View > pixel overlay is on, else null: a
    // Pixel's fire records its colour there. Web-only; no desktop analogue.
    this.screen = null;
    this.fc = null;                 // the last forecast
    this.sync();
  }

  // --- the grid -------------------------------------------------------------

  inBounds(x, y) { return x >= 0 && y >= 0 && x < this.board.width && y < this.board.height; }
  word(x, y, c) { return this.board.grid[(y * this.board.width + x) * 4 + c]; }
  rowAt(x, y) { return this.inBounds(x, y) ? this.board.cellId(this.word(x, y, 0)) : 0; }
  alive(row) { return this.board.alive(row); }
  f(row, field) { return this.board.entities[row * this.board.T.entity.STRIDE + field]; }
  setF(row, field, v) { this.board.entities[row * this.board.T.entity.STRIDE + field] = v; }
  pending(x, y) { return (this.word(x, y, 2) & this.T.cell.GHOST_MASK) === this.T.sim.GHOST_PENDING; }

  // SegmentIndex._cell_at: a real belt-like cell as [type, dir, row], or null.
  cellAt(x, y) {
    if (!this.inBounds(x, y) || this.pending(x, y)) return null;
    const w = this.word(x, y, 0), b = this.board;
    const t = b.cellType(w);
    if (t !== BELT && t !== UNDER) return null;
    return [t, b.cellDir(w), b.cellId(w)];
  }

  typeOf(row) { return this.f(row, this.T.entity.F_TYPE) | 0; }
  isExit(row) { return !!this.f(row, this.T.entity.F_UNDER_IS_EXIT); }
  partner(row) { return this.f(row, this.T.entity.F_UNDER_PARTNER) | 0; }
  paired(row) { const p = this.partner(row); return p !== 0 && this.alive(p); }
  originOf(row) { const E = this.T.entity; return [this.f(row, E.F_ORIGIN_X) | 0, this.f(row, E.F_ORIGIN_Y) | 0]; }
  dirOf(row) { return this.f(row, this.T.entity.F_DIR) | 0; }

  // The placed port table for a row: { inputs, outputs } as [lx, ly, face].
  orientOf(row) {
    const td = this.T.types[this.typeOf(row)];
    return td.orient[`${this.dirOf(row)}${this.f(row, this.T.entity.F_FLIPPED) ? 1 : 0}`];
  }

  // Is the row a ghost? Read where Entity.is_ghost reads it: the .z ghost
  // bits of the cell at its origin. The web never stamps ghosts into the
  // grid, but a save could carry one.
  ghostRow(row) {
    const [ox, oy] = this.originOf(row);
    return this.inBounds(ox, oy) && this.pending(ox, oy);
  }

  // --- tracing (core/segments.py) -------------------------------------------

  // _under_jump: where a paired entrance resurfaces, as [[ex, ey], buried].
  underJump(x, y, dir) {
    const row = this.rowAt(x, y);
    if (!row || !this.alive(row)) return null;
    if (this.typeOf(row) !== UNDER || this.isExit(row) || !this.paired(row)) return null;
    const mate = this.partner(row);
    if (this.typeOf(mate) !== UNDER || !this.isExit(mate) || this.dirOf(mate) !== dir) return null;
    const [mx, my] = this.originOf(mate);
    const dx = mx - x, dy = my - y;
    if ((dx !== 0) === (dy !== 0)) return null;
    const dist = Math.abs(dx) + Math.abs(dy);
    if (dist < 1 || dist > this.T.under.MAX_SPAN) return null;
    return [[mx, my], dist - 1];
  }

  // _next_cell: the cell downstream of (x, y), as [[nx, ny], buried] or null.
  nextCell(x, y) {
    const here = this.cellAt(x, y);
    if (!here) return null;
    const [t, d] = here;
    if (t === UNDER) {
      const jump = this.underJump(x, y, d);
      if (jump) return jump;
      const row = this.rowAt(x, y);
      if (row && this.alive(row) && !this.isExit(row)) return null;
    }
    const s = this.T.cell.DIR_STEPS[d];
    const nx = x + s[0], ny = y + s[1];
    if (!this.cellAt(nx, ny)) return null;
    const from = this.takenFrom(nx, ny);
    if (!from || from[0] !== x || from[1] !== y) return null;
    return [[nx, ny], 0];
  }

  // _incoming: directions holding anything emitting at (x, y) -- belts,
  // surfacing exits and machine output ports, in one set.
  incoming(x, y) {
    const here = this.cellAt(x, y);
    if (!here) return [];
    const facing = here[1], S = this.T.cell.DIR_STEPS;
    const out = [];
    for (let d = 0; d < 4; d++) {
      if (d === facing) continue;
      const nx = x + S[d][0], ny = y + S[d][1];
      const n = this.cellAt(nx, ny);
      if (!n) {
        if (this.portAt(nx, ny, (d + 2) % 4, true)) out.push(d);
        continue;
      }
      if (n[1] !== (d + 2) % 4) continue;
      if (n[0] === UNDER) {
        const row = this.rowAt(nx, ny);
        if (!row || !this.alive(row) || !this.isExit(row)) continue;
      }
      out.push(d);
    }
    return out;
  }

  // _taken_from: the neighbour (x, y) actually takes from, via take_dir().
  takenFrom(x, y) {
    const here = this.cellAt(x, y);
    if (!here) return null;
    const d = takeDir(here[1], this.incoming(x, y));
    if (d === null) return null;
    const s = this.T.cell.DIR_STEPS[d];
    return [x + s[0], y + s[1]];
  }

  beltTakenFrom(x, y) {
    const got = this.takenFrom(x, y);
    return got && this.cellAt(got[0], got[1]) ? got : null;
  }

  // _fed_by_under: is this a paired exit some entrance dives into?
  fedByUnder(x, y) {
    const row = this.rowAt(x, y);
    if (!row || !this.alive(row)) return false;
    if (this.typeOf(row) !== UNDER || !this.isExit(row) || !this.paired(row)) return false;
    const mate = this.partner(row);
    if (this.typeOf(mate) !== UNDER || this.isExit(mate)) return false;
    const [ex, ey] = this.originOf(mate);
    const jump = this.underJump(ex, ey, this.dirOf(mate));
    return !!jump && jump[0][0] === x && jump[0][1] === y;
  }

  isTail(x, y) {
    if (this.fedByUnder(x, y)) return false;
    return this.beltTakenFrom(x, y) === null;
  }

  trace(x, y) {
    const cells = [[x, y]];
    let length = 1;
    const seen = new Set([y * this.board.width + x]);
    let at = [x, y];
    for (;;) {
      const nxt = this.nextCell(at[0], at[1]);
      if (!nxt) break;
      const [[nx, ny], buried] = nxt;
      const k = ny * this.board.width + nx;
      if (seen.has(k)) break;
      if (length + buried + 1 > this.T.sim.MAX_SEGMENT_LEN) break;
      length += buried + 1;
      cells.push([nx, ny]);
      seen.add(k);
      at = [nx, ny];
    }
    return { cells, length };
  }

  // _port_at: [row, face] if the structure at (x, y) has a port facing
  // `toward` (pointing back at the belt), else null.
  portAt(x, y, toward, wantOutput) {
    if (!this.inBounds(x, y) || this.pending(x, y)) return null;
    const row = this.rowAt(x, y);
    if (!row || !this.alive(row)) return null;
    const t = this.typeOf(row);
    if (t === BELT || t === UNDER || t === EMPTY) return null;
    const o = this.orientOf(row), [ox, oy] = this.originOf(row);
    for (const [lx, ly, face] of (wantOutput ? o.outputs : o.inputs)) {
      if (ox + lx === x && oy + ly === y && face === toward) return [row, face];
    }
    return null;
  }

  nodeBehind([x, y]) {
    const got = this.takenFrom(x, y);
    if (!got) return null;
    const [nx, ny] = got;
    if (this.cellAt(nx, ny)) return null;
    return this.portAt(nx, ny, stepToDir(x - nx, y - ny), true);
  }

  nodeAhead([x, y]) {
    const here = this.cellAt(x, y);
    if (!here) return null;
    const s = this.T.cell.DIR_STEPS[here[1]];
    return this.portAt(x + s[0], y + s[1], (here[1] + 2) % 4, false);
  }

  // --- rebuild ----------------------------------------------------------------

  claimSid() {
    if (this.freeSids.length) return this.freeSids.pop();
    if (this.nextSid > this.T.sim.MAX_SEGMENT_ID) throw new Error("segment id overflow");
    return this.nextSid++;
  }

  // Rebuild everything derived from the board if it changed since the last
  // look, then start fresh: no items anywhere, seeds back on. Returns
  // whether it did.
  sync() {
    if (this.board.revision === this.seen) return false;
    this.seen = this.board.revision;
    this.version++;
    this.rebuild();
    this.flush();
    this.seed();
    this.screen?.clear();      // an edit, a Reset: the screen starts blank
    return true;
  }

  rebuild() {
    // clear(): release in creation order, unstamp.
    for (const seg of this.segments.values()) {
      this.freeSids.push(seg.sid);
      for (const [x, y] of seg.cells) {
        const k = y * this.board.width + x;
        this.segAt[k] = 0; this.offAt[k] = 0;
      }
    }
    this.segments.clear();

    const { width: W, height: H } = this.board;
    const g = this.board.grid, C = this.T.cell;
    const cands = [];
    for (let y = 0; y < H; y++) {
      for (let x = 0; x < W; x++) {
        const i = (y * W + x) * 4;
        const t = (g[i] >> C.TYPE_SHIFT) & C.TYPE_MASK;
        if (t !== BELT && t !== UNDER) continue;
        if ((g[i + 2] & C.GHOST_MASK) === this.T.sim.GHOST_PENDING) continue;
        cands.push(y * W + x);
      }
    }
    const claimed = new Uint8Array(W * H);
    const make = (x, y) => {
      const { cells, length } = this.trace(x, y);
      this.makeSegment(cells, length);
      for (const [cx, cy] of cells) claimed[cy * W + cx] = 1;
    };
    for (const k of cands) {
      const x = k % W, y = (k / W) | 0;
      if (!claimed[k] && this.isTail(x, y)) make(x, y);
    }
    // A closed loop has no tail: cut it anywhere.
    for (const k of cands) if (!claimed[k]) make(k % W, (k / W) | 0);

    this.order = [...this.segments.values()].sort((a, b) => a.sid - b.sid);
    this.stats.rebuilds++;
    this.buildGates();
  }

  makeSegment(cells, length) {
    const sid = this.claimSid();
    let cap = 1;
    while (cap < Math.max(this.T.sim.MIN_RING_CAP, length + 1)) cap *= 2;
    const seg = {
      sid, cells, length,
      head: 0, tail: 0, anchor: length * this.UNIT,
      cap, enter: new Float64Array(cap), payload: new Float32Array(cap),
      offs: new Int32Array(cells.length),
      src: this.nodeBehind(cells[0]), dst: this.nodeAhead(cells[cells.length - 1]),
      srcPort: null, dstPort: null,
    };
    this.segments.set(sid, seg);
    // For the renderer: each drawn cell's facing, and the box round the run.
    seg.dirs = new Int8Array(cells.length);
    seg.box = { x0: Infinity, y0: Infinity, x1: -Infinity, y1: -Infinity };
    let off = 0, prev = null;
    cells.forEach(([x, y], i) => {
      if (prev) off += Math.max(1, Math.abs(x - prev[0]) + Math.abs(y - prev[1]));
      const k = y * this.board.width + x;
      this.segAt[k] = sid; this.offAt[k] = off;
      seg.offs[i] = off;
      seg.dirs[i] = this.board.cellDir(this.word(x, y, 0));
      seg.box.x0 = Math.min(seg.box.x0, x); seg.box.y0 = Math.min(seg.box.y0, y);
      seg.box.x1 = Math.max(seg.box.x1, x + 1); seg.box.y1 = Math.max(seg.box.y1, y + 1);
      prev = [x, y];
    });
    return seg;
  }

  segmentAt(x, y) {
    if (!this.inBounds(x, y)) return null;
    const sid = this.segAt[y * this.board.width + x];
    return sid ? this.segments.get(sid) : null;
  }

  // --- the gate table -------------------------------------------------------
  //
  // Everything here is a function of the grid and the segmentation, so it is
  // derived once per rebuild. The desktop caches the same answers lazily and
  // expires them each tick as a backstop; with a rebuild on every edit there
  // is nothing for that backstop to catch.

  buildGates() {
    const E = this.T.entity, b = this.board;
    this.gates = [];
    this.gateOf = new Map();
    for (let row = 1; row < b.rows; row++) {
      if (!b.field(row, E.F_ALIVE)) continue;
      const t = this.typeOf(row);
      if (t === EMPTY || t === BELT || t === UNDER) continue;
      if (this.ghostRow(row)) continue;
      const td = this.T.types[t];
      if (!td?.sim) continue;
      const o = this.orientOf(row), [ox, oy] = this.originOf(row);
      const gate = {
        row, td, sim: td.sim,
        pixel: td.class === "Pixel",
        strict: td.sim.fire_rule === this.T.sim.FIRE_ALL,
        outFaces: o.outputs.map(([lx, ly, face], i) => ({ x: ox + lx, y: oy + ly, face, ch: td.sim.out_ch[i] })),
        inFaces: o.inputs.map(([lx, ly, face], i) => ({ x: ox + lx, y: oy + ly, face, ch: td.sim.in_ch[i] })),
        drains: null,
      };
      this.gates.push(gate);
      this.gateOf.set(row, gate);
    }
    // Segment ends -> (row, channel), resolved once (GateSim._resolve_src /
    // _resolve_dst). `face` points from the gate toward the belt, so the
    // gate's cell is one step back from the run's end.
    const S = this.T.cell.DIR_STEPS;
    for (const seg of this.segments.values()) {
      seg.srcPort = null; seg.dstPort = null;
      if (seg.src && this.alive(seg.src[0])) {
        const [row, face] = seg.src, [tx, ty] = seg.cells[0];
        const wx = tx - S[face][0], wy = ty - S[face][1];
        const gate = this.gateOf.get(row);
        const hit = gate?.outFaces.find(p => p.face === face && p.x === wx && p.y === wy);
        if (hit) seg.srcPort = [row, hit.ch];
      }
      if (seg.dst && this.alive(seg.dst[0])) {
        const [row, face] = seg.dst, [hx, hy] = seg.cells[seg.cells.length - 1];
        const ch = this.inputChannelFor(row, face, hx - S[face][0], hy - S[face][1]);
        if (ch !== null) seg.dstPort = [row, ch];
      }
    }
    for (const gate of this.gates) gate.drains = this.connectedChannels(gate);
    this.buildDirect();
  }

  // _input_channel_for: which input channel of `row` accepts from `face` at
  // world cell (x, y), or null.
  inputChannelFor(row, face, x, y) {
    const gate = this.gateOf.get(row);
    if (!gate) {
      // A ghost or a type with no sim record: answer from the port table.
      const td = this.T.types[this.typeOf(row)];
      if (!td?.sim) return null;
      const o = this.orientOf(row), [ox, oy] = this.originOf(row);
      const i = o.inputs.findIndex(([lx, ly, f]) => f === face && ox + lx === x && oy + ly === y);
      return i < 0 ? null : td.sim.in_ch[i];
    }
    const p = gate.inFaces.find(q => q.face === face && q.x === x && q.y === y);
    return p ? p.ch : null;
  }

  // _connected_channels: the output channels with somewhere to send a
  // result, as a sorted array. The rest are discard points.
  connectedChannels(gate) {
    const reached = new Set();
    for (const p of gate.outFaces) {
      if (!reached.has(p.ch) && this.faceHasConsumer(gate, p)) reached.add(p.ch);
    }
    return [...reached].sort((a, b) => a - b);
  }

  faceHasConsumer(gate, p) {
    const s = this.T.cell.DIR_STEPS[p.face];
    const nx = p.x + s[0], ny = p.y + s[1];
    if (!this.inBounds(nx, ny)) return false;
    const seg = this.segmentAt(nx, ny);
    if (seg) {
      // Only through the one port the run named as its source (take_dir()).
      const owner = this.livePort(seg.srcPort);
      if (!owner || owner[0] !== gate.row) return false;
      const mine = gate.outFaces.find(q => q.x === p.x && q.y === p.y && q.face === p.face);
      return owner[1] === mine.ch;
    }
    const row = this.rowAt(nx, ny);
    if (!row || !this.alive(row) || row === gate.row || this.ghostRow(row)) return false;
    return this.inputChannelFor(row, (p.face + 2) % 4, nx, ny) !== null;
  }

  // _build_direct: one hand-off per output face that touches another gate's
  // input with no belt between.
  buildDirect() {
    const edges = [];
    const S = this.T.cell.DIR_STEPS;
    for (const gate of this.gates) {
      for (const p of gate.outFaces) {
        const nx = p.x + S[p.face][0], ny = p.y + S[p.face][1];
        if (!this.inBounds(nx, ny) || this.segmentAt(nx, ny)) continue;
        const row = this.rowAt(nx, ny);
        if (!row || !this.alive(row) || row === gate.row || this.ghostRow(row)) continue;
        const ch = this.inputChannelFor(row, (p.face + 2) % 4, nx, ny);
        if (ch === null) continue;
        edges.push([gate.row, p.ch, row, ch]);
      }
    }
    this.edges = edges;
  }

  // GateSim._live_port: the cached pair, if that row is still a live, real gate.
  livePort(port) {
    if (!port) return null;
    const [row] = port;
    if (!(row > 0 && row < this.board.rows) || !this.f(row, this.T.entity.F_ALIVE)) return null;
    if (this.ghostRow(row)) return null;
    return port;
  }

  // --- gate state -------------------------------------------------------------

  inHeld(row, c) { return this.f(row, this.T.sim.F_IN_HELD + c) > 0; }
  inValue(row, c) { return this.f(row, this.T.sim.F_IN_VALUE + c); }
  outHeld(row, c) { return this.f(row, this.T.sim.F_OUT_HELD + c) > 0; }
  outValue(row, c) { return this.f(row, this.T.sim.F_OUT_VALUE + c); }
  setIn(row, c, v) { this.setF(row, this.T.sim.F_IN_VALUE + c, v); this.setF(row, this.T.sim.F_IN_HELD + c, 1); }
  clearIn(row, c) { this.setF(row, this.T.sim.F_IN_VALUE + c, 0); this.setF(row, this.T.sim.F_IN_HELD + c, 0); }
  setOut(row, c, v) { this.setF(row, this.T.sim.F_OUT_VALUE + c, v); this.setF(row, this.T.sim.F_OUT_HELD + c, 1); }
  clearOut(row, c) { this.setF(row, this.T.sim.F_OUT_VALUE + c, 0); this.setF(row, this.T.sim.F_OUT_HELD + c, 0); }

  // Engine.flush_material(): every item off every belt and out of every gate.
  flush() {
    for (const seg of this.segments.values()) {
      seg.head = seg.tail = 0;
      seg.anchor = seg.length * this.UNIT;
    }
    for (const g of this.gates) {
      for (let c = 0; c < g.sim.n_in; c++) this.clearIn(g.row, c);
      for (let c = 0; c < g.sim.n_out; c++) this.clearOut(g.row, c);
    }
  }

  // Engine.seed_segments(): one item per seeded belt cell, onto the tail of
  // its run, the oldest first and each one spacing older than the next, so a
  // run with several seeds starts as a packed clump.
  seed() {
    const E = this.T.entity;
    let seeded = 0;
    for (const seg of this.segments.values()) {
      const values = [];
      for (const [x, y] of seg.cells) {
        const row = this.rowAt(x, y);
        if (!row || !this.alive(row) || this.typeOf(row) !== BELT || this.ghostRow(row)) continue;
        if (this.f(row, E.F_SEED_ON)) values.push(this.f(row, E.F_SEED_VALUE));
      }
      const per = Math.max(1, Math.floor(this.spacing / this.speed));
      values.forEach((v, i) => {
        const when = this.tick - (values.length - 1 - i) * per;
        if (this.hasRoom(seg, when)) { this.push(seg, v, when); seeded++; }
      });
    }
    return seeded;
  }

  // ctrl+R: start over from tick 0, keeping the factory. A full
  // re-derivation, like the desktop's, so it is also the recovery from
  // anything stale.
  reset() {
    this.seen = -1;
    this.tick = 0;
    this.sync();
  }

  // --- the closed form (core/belts.py) ----------------------------------------

  // Where item k is at `tick + alpha`, in fixed point. Exact at any alpha:
  // both terms are linear in time.
  positionOf(seg, k, tick, alpha = 0) {
    const length = seg.length * this.UNIT;
    let age = (tick - seg.enter[k % seg.cap]) + alpha;
    age = Math.min(age, length / this.speed + 1);
    const unblocked = age * this.speed;
    const anchor = Math.min(seg.anchor + alpha * this.speed, length);
    const packed = anchor - (k - seg.head) * this.spacing;
    return Math.min(unblocked, packed);
  }

  hasRoom(seg, tick) {
    if (seg.head === seg.tail) return true;
    return this.positionOf(seg, seg.tail - 1, tick) >= this.spacing;
  }

  push(seg, payload, tick) {
    const slot = seg.tail % seg.cap;
    if (this.rec) this.rec.slots.push([seg, slot, seg.enter[slot], seg.payload[slot]]);
    seg.enter[slot] = tick;
    seg.payload[slot] = payload;
    seg.tail++;
    this.stats.pushed++;
  }

  // One run, one tick: advance the anchor, pop, push -- in that order, which
  // the report's pop argument depends on. The gate work happens inside it,
  // through offer/accept, not in passes around it (see core/gates.py).
  tickSegment(seg, tick) {
    const length = seg.length * this.UNIT;
    seg.anchor = Math.min(seg.anchor + this.speed, length);
    if (seg.head !== seg.tail && this.positionOf(seg, seg.head, tick) === length) {
      if (this.accept(seg, seg.payload[seg.head % seg.cap])) {
        if (this.rec) this.rec.pops.set(seg, seg.payload[seg.head % seg.cap]);
        seg.head++;
        seg.anchor = Math.max(0, length - this.spacing);
        this.stats.popped++;
      }
    }
    if (this.hasRoom(seg, tick)) {
      const v = this.offer(seg);
      if (v !== null) {
        if (this.rec) this.rec.pushes.set(seg, v);
        this.push(seg, v, tick);
        this.took(seg);
      }
    }
  }

  // --- the port callbacks (GateSim.offer_to / took_from / accept_from) --------

  offer(seg) {
    const p = this.livePort(seg.srcPort);
    if (!p || !this.outHeld(p[0], p[1])) return null;
    return this.outValue(p[0], p[1]);
  }

  took(seg) {
    const p = this.livePort(seg.srcPort);
    if (p) this.clearOut(p[0], p[1]);
  }

  accept(seg, payload) {
    const p = this.livePort(seg.dstPort);
    if (!p || this.inHeld(p[0], p[1])) return false;
    this.setIn(p[0], p[1], payload);
    return true;
  }

  // --- the tick -----------------------------------------------------------------

  // One whole tick: structure phase, belt phase, then the counter.
  step() {
    this.sync();
    this.version++;
    // Not while forecasting: that step is rolled back, and is not heard.
    const A = this.rec ? null : this.audio;
    if (A) A.pre(this);
    this.structurePhase(A, this.rec ? null : this.screen);
    for (const seg of this.order) this.tickSegment(seg, this.tick);
    this.deliverDirect();
    this.tick++;
    if (A) A.post(this);
  }

  // The prefilter (GateSim._candidate_rows) and the per-gate firing rule.
  // The prefilter tests only necessary conditions that are pure row state --
  // the rule satisfiable by occupancy, a free output zone -- so it can admit
  // a gate that then does nothing but never rejects one that would fire.
  structurePhase(A = null, P = null) {
    for (const g of this.gates) {
      const row = g.row, s = g.sim;
      let fed = 0;
      for (let c = 0; c < s.n_in; c++) if (this.inHeld(row, c)) fed++;
      const ready = s.n_in === 0 || (g.strict ? fed === s.n_in : fed > 0);
      if (!ready) continue;
      if (s.n_out > 0) {
        let free = false;
        for (let c = 0; c < s.n_out && !free; c++) if (!this.outHeld(row, c)) free = true;
        if (!free) continue;
      }
      if (this.fire(g, P)) {
        this.stats.fired++;
        if (A) A.fires[g.agroup]++;
      }
    }
  }

  // _fire_admitted, for one gate the prefilter let through.
  fire(g, P = null) {
    const row = g.row, s = g.sim;
    if (!this.inputsReady(g)) return false;
    const drains = g.drains;
    if (s.n_in === 0) {
      // A generator fills whichever connected zones are free, independently.
      if (!drains.length) return false;
      const emit = this.f(row, this.T.entity.F_EMIT);
      const results = evalOps(this.T, s.ops, [], emit);
      const produced = [];
      for (const c of drains) {
        if (this.outHeld(row, c) || c >= results.length || results[c] === null) continue;
        this.setOut(row, c, results[c]);
        produced.push(c);
      }
      if (this.rec && produced.length) this.rec.fires.set(row, { values: [], results, consumed: [], produced });
      return produced.length > 0;
    }
    for (const c of drains) if (this.outHeld(row, c)) return false;   // back pressure
    const values = [];
    for (let c = 0; c < s.n_in; c++) values.push(this.inHeld(row, c) ? this.inValue(row, c) : null);
    const results = evalOps(this.T, s.ops, values, 0);
    if (P && g.pixel) P.fire(row, values);
    const consumed = this.consumed(g, values).filter(c => this.inHeld(row, c));
    for (const c of consumed) this.clearIn(row, c);
    const produced = [];
    results.forEach((v, c) => {
      if (v === null) return;                           // declined: Route's other path
      if (!drains.includes(c)) { this.stats.destroyed++; return; }   // a dangling output is a sink
      this.setOut(row, c, v);
      produced.push(c);
    });
    if (this.rec) this.rec.fires.set(row, { values, results, consumed, produced });
    return true;
  }

  inputsReady(g) {
    const row = g.row, s = g.sim, R = this.T.sim;
    if (s.n_in === 0) return true;
    if (s.fire_rule === R.FIRE_SELECT) {
      const [ctl, pos, neg] = s.select;
      if (!this.inHeld(row, ctl)) return false;
      return this.inHeld(row, this.inValue(row, ctl) > 0 ? pos : neg);
    }
    let fed = 0;
    for (let c = 0; c < s.n_in; c++) if (this.inHeld(row, c)) fed++;
    return s.fire_rule === R.FIRE_ANY ? fed > 0 : fed === s.n_in;
  }

  // Entity.consumed(): every fed channel, except under a select rule, which
  // takes the control and the lane it named and leaves the other loaded.
  consumed(g, values) {
    if (g.sim.select) {
      const [ctl, pos, neg] = g.sim.select;
      return [ctl, values[ctl] > 0 ? pos : neg];
    }
    const out = [];
    for (let c = 0; c < g.sim.n_in; c++) if (this.inHeld(g.row, c)) out.push(c);
    return out;
  }

  // GateSim.deliver_direct(): decide every hand-off from the state as the
  // belt phase left it, then apply them all -- the desktop does this as one
  // vectorised gather and scatter, so no transfer sees another's result.
  deliverDirect() {
    const live = [];
    for (const e of this.edges) {
      if (this.outHeld(e[0], e[1]) && !this.inHeld(e[2], e[3])) live.push(e);
    }
    const values = live.map(e => this.outValue(e[0], e[1]));
    live.forEach((e, i) => this.setIn(e[2], e[3], values[i]));
    if (this.rec) live.forEach((e, i) => this.rec.directs.push([e[0], e[1], e[2], e[3], values[i]]));
    for (const e of live) this.clearOut(e[0], e[1]);
    this.stats.direct += live.length;
    return live.length;
  }

  // --- the forecast ------------------------------------------------------------------
  //
  // What the NEXT step will do, from the state now: which runs pop their head
  // into which gate, which gates fire (consuming what, producing what), which
  // runs take a push and from where, and which direct hand-offs happen. The
  // renderer animates those over the coming tick, so an item can be drawn
  // moving into, through and out of a machine at the current alpha.
  //
  // It is the real step() run on the live state with an event log, then
  // rolled back -- not a second copy of the rules, so it cannot disagree
  // with the tick it predicts. Everything the step writes is restored: the
  // entity table (every gate channel), each run's head/tail/anchor, the one
  // ring slot each push writes (it may be the slot the popped head lived
  // in), the tick, the stats and the version. Cached on `version`, so it
  // costs one extra step per tick drawn, not per frame.
  forecast() {
    this.sync();
    if (this.fc && this.fc.version === this.version) return this.fc;
    const ent = this.board.entities.slice();
    const segs = this.order.map(seg => [seg, seg.head, seg.tail, seg.anchor]);
    const tick = this.tick, version = this.version, stats = { ...this.stats };
    const rec = { pops: new Map(), pushes: new Map(), fires: new Map(), directs: [], slots: [] };
    this.rec = rec;
    try {
      this.step();
      // Where each pushed item is at the start of the tick after: usually a
      // cell in, but a packed run can hold it short of that.
      for (const [seg, v] of rec.pushes) {
        rec.pushes.set(seg, { value: v, end: this.positionOf(seg, seg.tail - 1, this.tick, 0) / this.UNIT });
      }
    } finally {
      this.rec = null;
      this.board.entities.set(ent);
      for (const [seg, h, t, a] of segs) { seg.head = h; seg.tail = t; seg.anchor = a; }
      for (let i = rec.slots.length - 1; i >= 0; i--) {
        const [seg, slot, e, p] = rec.slots[i];
        seg.enter[slot] = e; seg.payload[slot] = p;
      }
      this.tick = tick;
      this.version = version;
      Object.assign(this.stats, stats);
    }
    // Indexed the ways the renderer asks: a push by the gate channel it came
    // from, a hand-off by both ends.
    const pushFrom = new Map(), directFrom = new Map(), directTo = new Map();
    for (const [seg, push] of rec.pushes) {
      const p = this.livePort(seg.srcPort);
      if (p) pushFrom.set(`${p[0]}:${p[1]}`, { seg, ...push });
    }
    for (const d of rec.directs) {
      directFrom.set(`${d[0]}:${d[1]}`, d);
      directTo.set(`${d[2]}:${d[3]}`, d);
    }
    this.fc = { version, pops: rec.pops, pushes: rec.pushes, fires: rec.fires, pushFrom, directFrom, directTo };
    return this.fc;
  }

  // --- copies, for baking audio --------------------------------------------------
  //
  // A copy that ticks on without touching this one: its own entity table
  // (gate channels) and its own runs (rings, counters); everything derived
  // from the board -- the gate table, the edges, the cell maps -- is shared,
  // since a copy never rebuilds (its board's revision is frozen with it).
  // The grid is shared too: an edit to the live board invalidates a bake,
  // and the bake checks for one between chunks.
  clone() {
    const c = Object.create(Sim.prototype);
    Object.assign(c, this);
    const b = Object.create(Object.getPrototypeOf(this.board));
    Object.assign(b, this.board);
    b.entities = this.board.entities.slice();
    c.board = b;
    c.segments = new Map();
    for (const [sid, seg] of this.segments) {
      c.segments.set(sid, { ...seg, enter: seg.enter.slice(), payload: seg.payload.slice() });
    }
    c.order = this.order.map(seg => c.segments.get(seg.sid));
    c.stats = { ...this.stats };
    c.audio = null; c.rec = null; c.fc = null; c.screen = null;
    return c;
  }

  // Put this sim back into a state copied from it by clone(), with no edit
  // since. What a bake's Reset uses: a rebuild renumbers the runs, and the
  // numbering decides the belt phase's order, so only the same state is
  // guaranteed to replay the same ticks.
  restoreFrom(snap) {
    this.board.entities.set(snap.board.entities);
    for (const seg of this.order) {
      const s = snap.segments.get(seg.sid);
      seg.head = s.head; seg.tail = s.tail; seg.anchor = s.anchor;
      seg.enter.set(s.enter); seg.payload.set(s.payload);
    }
    this.tick = snap.tick;
    Object.assign(this.stats, snap.stats);
    this.version++;
    this.fc = null;
    this.screen?.clear();
  }

  // --- reading it back ------------------------------------------------------------

  liveItems() {
    let n = 0;
    for (const seg of this.segments.values()) n += seg.tail - seg.head;
    return n;
  }

  // Every item on a run, head first: { k, pos (in cells), value }.
  items(seg, alpha = 0) {
    const out = [];
    for (let k = seg.head; k < seg.tail; k++) {
      out.push({ k, pos: this.positionOf(seg, k, this.tick, alpha) / this.UNIT, value: seg.payload[k % seg.cap] });
    }
    return out;
  }
}

// --- the clock (core/tick.py) ---------------------------------------------------
//
// A fixed timestep: real time is banked and spent in whole ticks, so the same
// ticks happen at any frame rate. `alpha` is the leftover fraction, which
// the renderer feeds to the closed form so items glide between ticks.

export const RATE_PRESETS = [1, 5, 30, 240];
export const RATE_MIN = 0.1;
export const RATE_MAX = 10000;
// A frame that arrives after a long stall (a backgrounded tab) runs at most
// this many ticks -- or a quarter second's worth at the current rate, if that
// is more -- and drops the rest, rather than freezing to catch up. A flat 240
// would cap a fast rate at 240 ticks a frame whatever the rate asked for.
const MAX_CATCHUP = 240;

export class Clock {
  constructor(rate = 1) {
    this.rate = rate;
    this.paused = true;
    // Banked time in TICKS, not seconds: whole ticks are spent out of it and
    // the fraction left is alpha. Banking seconds and dividing left float
    // residue that turned a whole tick into 0.9999 of one.
    this.frac = 0;
    this.dropped = 0;
  }
  get alpha() { return Math.min(1, Math.max(0, this.frac)); }
  // A rate change keeps alpha where it is, so nothing on the board jumps.
  setRate(r) { this.rate = Math.min(RATE_MAX, Math.max(RATE_MIN, r)); }
  reset() { this.frac = 0; }
  // Bank dt seconds; return how many whole ticks are due.
  advance(dt) {
    if (this.paused) return 0;
    this.frac += Math.max(0, dt) * this.rate;
    let due = Math.floor(this.frac);
    const cap = Math.max(MAX_CATCHUP, Math.ceil(this.rate / 4));
    if (due > cap) {
      this.dropped += due - cap;
      due = cap;
      this.frac = 0;
    } else {
      this.frac -= due;
    }
    return due;
  }
}

// --- pure helpers ----------------------------------------------------------------

// core/segments.py take_dir(): straight through first, then counter-clockwise
// from the receiving cell's own facing.
export function takeDir(facing, cands) {
  if (!cands.length) return null;
  const behind = (facing + 2) % 4;
  if (cands.includes(behind)) return behind;
  for (let i = 0; i < 4; i++) {
    const d = (facing + i) % 4;
    if (cands.includes(d)) return d;
  }
  return null;
}

function stepToDir(dx, dy) {
  if (dx === 1 && dy === 0) return 0;
  if (dx === 0 && dy === 1) return 1;
  if (dx === -1 && dy === 0) return 2;
  if (dx === 0 && dy === -1) return 3;
  throw new Error(`not a unit step: ${dx}, ${dy}`);
}

const clampValue = (v, L) => (v !== v ? v : v > L ? L : v < -L ? -L : v);

// Entity.eval_ops(): a machine's arithmetic, from its exported opcode table.
// One entry per output channel; null means "emit nothing". `values` are the
// input channels (null when unfed), `konst` the Source's emit value.
export function evalOps(T, ops, values, konst) {
  const O = T.sim.OPS, L = T.sim.VALUE_LIMIT;
  const val = (i) => (values[i] === null || values[i] === undefined ? 0 : values[i]);
  return ops.map(([op, ...a]) => {
    switch (op) {
      case O.OP_NONE: return null;
      case O.OP_IN: return val(a[0]);
      case O.OP_ADD: return val(a[0]) + val(a[1]);
      case O.OP_MAC: return val(a[2]) + val(a[0]) * val(a[1]);
      case O.OP_RECIP: {
        const x = val(a[0]);
        // 1/0 clamps, keeping the sign of the zero.
        if (x === 0) return Object.is(x, -0) ? -L : L;
        return clampValue(1 / x, L);
      }
      case O.OP_MAX: {
        const x = val(a[0]), y = val(a[1]);
        if (x !== x || y !== y) return NaN;
        return clampValue(x > y ? x : y, L);
      }
      case O.OP_FLOOR: {
        const x = val(a[0]);
        return x !== x ? x : Math.floor(clampValue(x, L));
      }
      case O.OP_SEL: return val(a[0]) > 0 ? val(a[1]) : val(a[2]);
      case O.OP_GATED: return (val(a[0]) > 0) === !!a[2] ? val(a[1]) : null;
      case O.OP_CONST: return konst;
      default: throw new Error(`unknown opcode ${op}`);
    }
  });
}
