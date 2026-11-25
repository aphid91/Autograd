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

import { RATE_MIN, RATE_MAX } from "./rate.js";

const BELT = 1, UNDER = 7, EMPTY = 0;
const NEVER = -(2 ** 30);   // an activity stamp for "never" (see flush())

export class Sim {
  constructor(T, board) {
    this.T = T;
    const S = T.sim;
    this.UNIT = S.UNIT;
    this.speed = S.SPEED;
    this.spacing = S.SPACING;
    this.board = board;
    // Field offsets, looked up once: the gate-state accessors below run
    // thousands of times a tick, and each `this.T.sim.F_*` chain was a cost.
    this.ST = T.entity.STRIDE;
    this.F_IN_VALUE = S.F_IN_VALUE; this.F_IN_HELD = S.F_IN_HELD;
    this.F_OUT_VALUE = S.F_OUT_VALUE; this.F_OUT_HELD = S.F_OUT_HELD;
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
    this.undoLog = null;            // ...and its entity writes, to roll back (setF())
    // audio.js's Recorder while audio is on, else null: then the tick's whole
    // audio cost is this null check (step() and structurePhase()).
    this.audio = null;
    // pixel.js's Screen while View > pixel overlay is on, else null: a
    // Pixel's fire records its colour there. Web-only; no desktop analogue.
    this.screen = null;
    this.fc = null;                 // the last forecast
    // The wake list (see "the wake list" below): on unless a checker or a
    // debug switch turns it off (setSleeping()); `sched` is its state, null
    // while off and while a forecast runs.
    this.sleeping = true;
    this.sched = null;
    // simhost.js: on the worker, a Tracker told of every change a tick makes
    // (the rows written, the runs ticked, the ring slots pushed), for the
    // main thread's mirror; on the main thread, a callback after every
    // rebuild, given the run-id allocator as it stood before it. Both null
    // here.
    this.track = null;
    // Truthiness (View > truthiness colours, Audio > truthiness): when on, a
    // gate's fire records whether what it produced was > 0 (`truth[row]`,
    // see truthSource()) and a push records its item's sign (`seg.truth`).
    // Off, nothing is written -- one boolean test per fire and per push --
    // so turning it on starts from stale bits (1, truthy, until the next
    // fire or push).
    this.truthOn = false;
    this.truth = new Uint8Array(0);
    // Keyboards (keyboard.js): each one's queue of typed codes, row ->
    // array, oldest first. Live input from the player, not derived from the
    // board, so it is dynamic state like a ring: emptied on every fresh
    // start (flush()), carried by clone(), the worker's deltas and full
    // states (simhost.js), and never consumed by a forecast.
    this.keys = new Map();
    this.onRebuild = null;
    this.allocOverride = null;      // simhost.js: the allocator a rebuild must start from
    this.sync();
  }

  // --- the grid -------------------------------------------------------------

  inBounds(x, y) { return x >= 0 && y >= 0 && x < this.board.width && y < this.board.height; }
  word(x, y, c) { return this.board.grid[(y * this.board.width + x) * 4 + c]; }
  rowAt(x, y) { return this.inBounds(x, y) ? this.board.cellId(this.word(x, y, 0)) : 0; }
  alive(row) { return this.board.alive(row); }
  f(row, field) { return this.board.entities[row * this.ST + field]; }
  // While forecasting, every write logs the value it replaces (`undoLog`,
  // flat index/value pairs) so the rollback restores exactly those slots.
  setF(row, field, v) {
    const i = row * this.ST + field, E = this.board.entities;
    if (this.undoLog) this.undoLog.push(i, E[i]);
    E[i] = v;
  }
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
    this.sched = null;          // built for the new runs below, everything awake
    this.rebuild();               // records `lastAlloc`, for a worker's rebuild (simhost.js)
    this.flush();
    this.seed();
    this.screen?.clear();      // an edit, a Reset: the screen starts blank
    if (this.sleeping) this.sched = this.makeSched();
    if (this.onRebuild) this.onRebuild(this.lastAlloc);
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
    // The allocator as it now stands -- old runs released, none yet taken --
    // is what decides every new run's id. A worker (simhost.js) rebuilding
    // the same board takes this one's (`allocOverride`) instead of its own,
    // whose released runs were its own: then both number every run alike.
    if (this.allocOverride) {
      this.freeSids = this.allocOverride.free.slice();
      this.nextSid = this.allocOverride.next;
      this.allocOverride = null;
    }
    this.lastAlloc = { free: this.freeSids.slice(), next: this.nextSid, tick: this.tick };

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
    this.order.forEach((seg, i) => { seg.oi = i; });   // a run's index, which deltas and the wake list key on
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
      lastPush: NEVER, lastPop: NEVER,   // activity stamps (see flush())
      truth: 1,                          // the newest push's sign, while truthOn
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
        ...truthSource(td.sim, this.T.sim.OPS),
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
    // The ports again as plain numbers for the tick (`srcRow`/`srcCh`,
    // `dstRow`/`dstCh`; row 0: none), validated here once. offer() and
    // accept() used to re-run livePort() -- the row's alive flag and a grid
    // read for its ghost bit -- on every run every tick, ~2000 calls a tick
    // on a big board to learn what only an edit can change, and an edit is
    // a rebuild.
    for (const seg of this.segments.values()) {
      const sp = this.livePort(seg.srcPort), dp = this.livePort(seg.dstPort);
      seg.srcRow = sp ? sp[0] : 0; seg.srcCh = sp ? sp[1] : 0;
      seg.dstRow = dp ? dp[0] : 0; seg.dstCh = dp ? dp[1] : 0;
    }
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

  inHeld(row, c) { return this.board.entities[row * this.ST + this.F_IN_HELD + c] > 0; }
  inValue(row, c) { return this.board.entities[row * this.ST + this.F_IN_VALUE + c]; }
  outHeld(row, c) { return this.board.entities[row * this.ST + this.F_OUT_HELD + c] > 0; }
  outValue(row, c) { return this.board.entities[row * this.ST + this.F_OUT_VALUE + c]; }
  // Every change to a gate's channels comes through these four, so they are
  // where the wake list hears of it (touch(), wakeRun()): the gate is asked
  // again next structure phase, its direct edges next hand-off, and a run
  // that can now act is woken -- the one an output feeds when the output
  // fills, the one feeding an input when the input empties.
  setIn(row, c, v) { this.setF(row, this.F_IN_VALUE + c, v); this.setF(row, this.F_IN_HELD + c, 1); if (this.sched) this.touch(row); if (this.track) this.track.row(row); }
  clearIn(row, c) {
    this.setF(row, this.F_IN_VALUE + c, 0); this.setF(row, this.F_IN_HELD + c, 0);
    const S = this.sched;
    if (S) { this.touch(row); this.wakeRun(S.runIn[row * S.C + c]); }
    if (this.track) this.track.row(row);
  }
  setOut(row, c, v) {
    this.setF(row, this.F_OUT_VALUE + c, v); this.setF(row, this.F_OUT_HELD + c, 1);
    const S = this.sched;
    if (S) { this.touch(row); this.wakeRun(S.runOut[row * S.C + c]); }
    if (this.track) this.track.row(row);
  }
  clearOut(row, c) { this.setF(row, this.F_OUT_VALUE + c, 0); this.setF(row, this.F_OUT_HELD + c, 0); if (this.sched) this.touch(row); if (this.track) this.track.row(row); }

  // Engine.flush_material(): every item off every belt and out of every gate.
  //
  // Also clears the activity stamps the status colours' afterglow reads
  // (main.js): `seg.lastPush` / `seg.lastPop`, and `fired[row]`, each the
  // tick a run last pushed or popped, or a gate last fired, as the tick the
  // state was in after that step (so "just now" is `this.tick`). Written by
  // step() and never read by it, so the tick is unchanged by them; not
  // written by a forecast, which is rolled back.
  flush() {
    for (const seg of this.segments.values()) {
      seg.head = seg.tail = 0;
      seg.anchor = seg.length * this.UNIT;
      seg.lastPush = seg.lastPop = NEVER;
      seg.truth = 1;
    }
    this.fired = new Int32Array(this.board.rows).fill(NEVER);
    this.truth = new Uint8Array(this.board.rows).fill(1);
    for (const g of this.gates) {
      for (let c = 0; c < g.sim.n_in; c++) this.clearIn(g.row, c);
      for (let c = 0; c < g.sim.n_out; c++) this.clearOut(g.row, c);
    }
    this.keys = new Map();   // a fresh start forgets what was typed
    this.keysVersion = (this.keysVersion || 0) + 1;
  }

  // A key press on keyboard `row`: queue its code. Returns whether it was
  // taken -- not a keyboard (or not live), or a full queue, and it is not.
  // The keyboard is asked again on the coming tick (touch()), since a
  // sleeping gate reads nothing and would never notice.
  inject(row, code, max = 256) {
    const g = this.gateOf.get(row);
    if (!g || !g.sim.keyboard || !Number.isFinite(code)) return false;
    let q = this.keys.get(row);
    if (!q) this.keys.set(row, q = []);
    if (q.length >= max) return false;
    q.push(Math.fround(code));
    this.keysVersion = (this.keysVersion || 0) + 1;
    if (this.sched) this.touch(row);
    if (this.track) this.track.keys = true;
    this.version++;
    this.fc = null;
    return true;
  }

  // How many codes keyboard `row` has waiting.
  keysWaiting(row) { return this.keys.get(row)?.length || 0; }

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

  // The tick from which item k is at rest if nothing pops or pushes on its
  // run from now on: the first tick t whose step, and every step after it,
  // leaves k where it is -- in the units of the activity stamps, so an item
  // last moved by the step at tick t - 1 rests from t. Position is the min
  // of two terms that each grow by `speed` a step until they stop: the
  // unblocked one reaches k's packed place P at its entry tick + P / speed;
  // the anchor is at the end except for the steps after a pop (which sets it
  // to length - spacing), and is back by the pop's step + spacing / speed.
  // Nothing in it depends on the current tick, so between pops it is a
  // constant (a push does not move it for the items already on the run),
  // which is what lets the afterglow read it at any frame.
  // `tests/sim.test.mjs` pins it against running the run on with no
  // producer or consumer.
  restTick(seg, k) {
    const L = seg.length * this.UNIT, v = this.speed;
    const P = Math.max(0, L - (k - seg.head) * this.spacing);
    const unblocked = seg.enter[k % seg.cap] + Math.ceil(P / v) + 1;
    const anchor = seg.lastPop + Math.ceil(Math.min(this.spacing, L) / v);
    return Math.max(unblocked, anchor);
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
    if (this.truthOn && !this.rec) {
      const t = payload > 0 ? 1 : 0;
      if (t !== seg.truth) {
        seg.truth = t;
        const A = this.audio;
        if (A && A.truth && seg.aIdx >= 0) A.flip(seg.aIdx, t);
      }
    }
    if (this.track) this.track.push(seg.oi, slot);
    this.stats.pushed++;
  }

  // One run, one tick: advance the anchor, pop, push -- in that order, which
  // the report's pop argument depends on. The gate work happens inside it,
  // through offer/accept, not in passes around it (see core/gates.py).
  tickSegment(seg, tick) {
    if (this.track) this.track.run(seg.oi);
    const length = seg.length * this.UNIT;
    seg.anchor = Math.min(seg.anchor + this.speed, length);
    if (seg.head !== seg.tail && this.positionOf(seg, seg.head, tick) === length) {
      if (this.accept(seg, seg.payload[seg.head % seg.cap])) {
        if (this.rec) this.rec.pops.set(seg, seg.payload[seg.head % seg.cap]);
        seg.head++;
        seg.anchor = Math.max(0, length - this.spacing);
        this.stats.popped++;
        if (!this.rec) seg.lastPop = tick + 1;
      }
    }
    if (this.hasRoom(seg, tick)) {
      const v = this.offer(seg);
      if (v !== null) {
        if (this.rec) this.rec.pushes.set(seg, v);
        this.push(seg, v, tick);
        this.took(seg);
        if (!this.rec) seg.lastPush = tick + 1;
      }
    }
  }

  // --- the port callbacks (GateSim.offer_to / took_from / accept_from) --------

  offer(seg) {
    const r = seg.srcRow;
    if (!r || !this.outHeld(r, seg.srcCh)) return null;
    return this.outValue(r, seg.srcCh);
  }

  took(seg) {
    if (seg.srcRow) this.clearOut(seg.srcRow, seg.srcCh);
  }

  accept(seg, payload) {
    const r = seg.dstRow;
    if (!r || this.inHeld(r, seg.dstCh)) return false;
    this.setIn(r, seg.dstCh, payload);
    return true;
  }

  // --- the tick -----------------------------------------------------------------

  // One whole tick: structure phase, belt phase, then the counter. With the
  // wake list on, only what can act is visited (sleepStep()); otherwise --
  // and always for a forecast -- everything is (fullStep()). The two are the
  // same tick: tests/sleep.test.mjs runs them side by side.
  step() {
    this.sync();
    // A mirror whose state came from the worker (simhost.js) has no wake
    // list; stepped itself, it starts one with everything awake.
    if (!this.sched && this.sleeping && !this.rec) this.sched = this.makeSched();
    if (this.sched && !this.rec) return this.sleepStep();
    this.fullStep();
  }

  fullStep() {
    this.version++;
    // Not while forecasting: that step is rolled back, and is not heard.
    const A = this.rec ? null : this.audio;
    if (A) A.pre(this);
    this.structurePhase(A, this.rec ? null : this.screen);
    const t = this.tick;
    if (A) for (const seg of this.order) this.tickHeard(seg, t, A);
    else for (const seg of this.order) this.tickSegment(seg, t);
    this.deliverDirect();
    this.tick++;
    if (A) A.post(this);
  }

  // A run's tick, telling the audio recorder whether it was active: it
  // pushed or popped, or its newest item moved (the drone's rule). Returns
  // whether it pushed or popped.
  tickHeard(seg, t, A) {
    // Only a run with a bit (a candidate: in the length window, the sound
    // selection, belts not muted) is asked about; hit() ignores the rest,
    // so skipping their two position reads changes nothing recorded.
    const H = A && seg.aIdx >= 0 ? A : null;
    const h0 = seg.head, t0 = seg.tail;
    const p0 = H && t0 > h0 ? this.positionOf(seg, t0 - 1, t) : -1;
    this.tickSegment(seg, t);
    const moved = seg.head !== h0 || seg.tail !== t0;
    if (H && (moved || (t0 > h0 && this.positionOf(seg, t0 - 1, t + 1) > p0))) H.hit(seg);
    return moved;
  }

  // The prefilter (GateSim._candidate_rows) and the per-gate firing rule.
  // The prefilter tests only necessary conditions that are pure row state --
  // the rule satisfiable by occupancy, a free output zone -- so it can admit
  // a gate that then does nothing but never rejects one that would fire.
  //
  // The output test is fire()'s own, on the CONNECTED outputs (`drains`): a
  // generator needs one of them free, anything else all of them (a
  // dangling output discards and never blocks). It used to ask whether any
  // output at all was free, which a dangling one always is -- so every
  // Source with an unconnected face passed, and fire() was called ~1770
  // times a tick on `test` to fire about once.
  structurePhase(A = null, P = null, gates = this.gates) {
    const E = this.board.entities, ST = this.ST, IH = this.F_IN_HELD, OH = this.F_OUT_HELD;
    for (const g of gates) {
      const row = g.row, s = g.sim, drains = g.drains, base = row * ST;
      if (s.n_in === 0) {
        let free = false;
        for (let i = 0; i < drains.length; i++) if (!(E[base + OH + drains[i]] > 0)) { free = true; break; }
        if (!free) continue;
      } else {
        let fed = 0;
        for (let c = 0; c < s.n_in; c++) if (E[base + IH + c] > 0) fed++;
        if (g.strict ? fed !== s.n_in : fed === 0) continue;
        let blocked = false;
        for (let i = 0; i < drains.length; i++) if (E[base + OH + drains[i]] > 0) { blocked = true; break; }
        if (blocked) continue;
      }
      if (this.fire(g, P)) {
        this.stats.fired++;
        if (!this.rec) { this.fired[row] = this.tick + 1; if (this.track) this.track.row(row); }
        // A far gate (space.js) counts for less; with Audio > truthiness a
        // falsy fire counts in its group's falsy half (fires[group + 3]).
        if (A) A.fires[A.truth && this.truthOn && !this.truth[row] ? g.agroup + 3 : g.agroup] += g.again ?? 1;
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
      // A keyboard's constant is the head of its queue, and with nothing
      // typed it has nothing to emit. The head is taken only when it went
      // somewhere, and never by a forecast (which rolls everything back but
      // does not know the queue).
      let q = null;
      if (s.keyboard) {
        q = this.keys.get(row);
        if (!q || !q.length) return false;
      }
      const emit = q ? q[0] : this.f(row, this.T.entity.F_EMIT);
      const results = evalOps(this.T, s.ops, [], emit);
      const produced = [];
      for (const c of drains) {
        if (this.outHeld(row, c) || c >= results.length || results[c] === null) continue;
        this.setOut(row, c, results[c]);
        produced.push(c);
      }
      if (q && produced.length && !this.rec) {
        q.shift();
        this.keysVersion = (this.keysVersion || 0) + 1;
        if (this.track) this.track.keys = true;
      }
      if (this.rec && produced.length) this.rec.fires.set(row, { values: [], results, consumed: [], produced });
      if (this.truthOn && !this.rec && produced.length) this.truth[row] = truthOf(g, null, results);
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
    if (this.truthOn && !this.rec) this.truth[row] = truthOf(g, values, results);
    return true;
  }

  inputsReady(g) {
    const row = g.row, s = g.sim, R = this.T.sim;
    // A keyboard is ready only with something typed (gate status colours
    // then call an idle one starved, which it is).
    if (s.keyboard) return this.keysWaiting(row) > 0;
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
  deliverDirect(edges = this.edges) {
    const live = [];
    for (const e of edges) {
      if (this.outHeld(e[0], e[1]) && !this.inHeld(e[2], e[3])) live.push(e);
    }
    const values = live.map(e => this.outValue(e[0], e[1]));
    live.forEach((e, i) => this.setIn(e[2], e[3], values[i]));
    if (this.rec) live.forEach((e, i) => this.rec.directs.push([e[0], e[1], e[2], e[3], values[i]]));
    for (const e of live) this.clearOut(e[0], e[1]);
    this.stats.direct += live.length;
    return live.length;
  }

  // --- the wake list ---------------------------------------------------------------
  //
  // A tick used to visit every gate, every run and every direct edge to find
  // the few with something to do: on `test` 0.27% of runs and 0.08% of gates
  // change in a tick. sleepStep() visits only those that can. It is the same
  // tick, not an approximation -- tests/sleep.test.mjs and check_web_sim.py
  // hold it to fullStep() and to the desktop -- because every rule below
  // only ever skips work that would have changed nothing:
  //
  // - A GATE's firing reads its own row and nothing else (its channels; its
  //   drains and emit value are fixed between edits). So it is asked again
  //   only when one of its channels was written since it was last asked
  //   (`gList`, from touch() in the four channel setters) -- its own fire,
  //   a run's pop or push, a hand-off. Asked in row order, as the full scan
  //   does: Pixels that fire in one tick paint over each other in that order.
  // - A DIRECT EDGE delivers when its source holds and its target lane is
  //   empty: two rows. So it is checked only when either row changed since
  //   the last hand-off (`dList`). All decided, then all applied, as before.
  // - A RUN touches only its own two lanes -- the output zone it takes from
  //   and the input it pops into, each with exactly one run or edge on it --
  //   so runs never affect one another within the belt phase, and a run's
  //   tick changes nothing when (a) its anchor is at the end, (b) its head
  //   is short of the end or its consumer's lane is full, and (c) it has no
  //   room or its producer holds nothing. A run that ticks with no pop and no
  //   push and its anchor at the end sleeps until one of those can change:
  //     * its producer fills the zone it takes from   -> setOut() wakes it
  //     * its consumer empties the lane it pops into  -> clearIn() wakes it
  //     * its head reaches the end, or room opens behind its newest item, by
  //       the closed form alone -- a timed wake (`wakeAt`, tick -> runs) at
  //       entry + length / speed and entry + spacing / speed, the tick each
  //       becomes true (positions are a closed form over the tick, so an item
  //       moving down a sleeping run moves with nobody ticking it).
  //   A pop leaves the anchor short of the end, so a run that popped stays
  //   awake until it is back; asleep, the anchor is always at the end, which
  //   is why skipping its ticks loses nothing.
  // - An edit, a Reset, a bake's restore or a new recorder wakes everything
  //   (makeSched()); waking more than needed is always safe -- a tick of a
  //   run or a look at a gate that does nothing is exactly what the full
  //   step does -- so every wake rule errs that way.
  //
  // Audio: the recorder's "this run was active" bit (a push, a pop, or the
  // newest item moving) is worked out for each run ticked, as before, and
  // for a sleeping run whose newest item is still sliding into place, by
  // the closed form: with the anchor at the end it moves on every tick
  // before entry + (its packed place) / speed (`moveUntil`, `movers`).
  makeSched() {
    const R = this.board.rows, C = this.T.sim.MAX_CHANNELS, n = this.order.length;
    const S = {
      C,
      gDirty: new Uint8Array(R), gList: [],        // gates to ask next structure phase
      dDirty: new Uint8Array(R), dList: [],        // rows whose edges to check next hand-off
      awake: new Uint8Array(n), aList: [],         // runs (by index in `order`) to tick
      ran: new Int32Array(n).fill(-1),             // the tick each run was last ticked
      runOut: new Int32Array(R * C).fill(-1),      // (row, output channel) -> the run it feeds
      runIn: new Int32Array(R * C).fill(-1),       // (row, input channel) -> the run feeding it
      rowEdges: new Map(),                         // row -> its direct edges, by index
      eMark: new Uint8Array(this.edges.length),    // edges already gathered this hand-off
      gateAt: new Array(R).fill(null),             // row -> gate record (gateOf, as an array)
      wakeAt: new Map(),                           // tick -> runs to wake then
      movers: [], moversNext: [],                  // sleeping runs still sliding (audio only)
      stats: { ticks: 0, gates: 0, runs: 0, edges: 0, timed: 0 },
    };
    this.order.forEach((seg, i) => {
      if (seg.srcRow) S.runOut[seg.srcRow * C + seg.srcCh] = i;
      if (seg.dstRow) S.runIn[seg.dstRow * C + seg.dstCh] = i;
    });
    this.edges.forEach((e, i) => {
      for (const r of [e[0], e[2]]) {
        let l = S.rowEdges.get(r);
        if (!l) S.rowEdges.set(r, l = []);
        l.push(i);
      }
    });
    for (const g of this.gates) {
      S.gateAt[g.row] = g;
      S.gDirty[g.row] = 1; S.gList.push(g.row);
      S.dDirty[g.row] = 1; S.dList.push(g.row);
    }
    for (let i = 0; i < n; i++) { S.awake[i] = 1; S.aList.push(i); }
    return S;
  }

  // Everything awake: the next tick visits all of it, then lets it sleep.
  wakeAll() { if (this.sleeping) this.sched = this.makeSched(); }

  // The wake list on or off (a checker's reference timeline, or to measure).
  setSleeping(on) {
    this.sleeping = !!on;
    this.sched = on ? this.makeSched() : null;
  }

  touch(row) {
    const S = this.sched;
    if (!S.gDirty[row]) { S.gDirty[row] = 1; S.gList.push(row); }
    if (!S.dDirty[row]) { S.dDirty[row] = 1; S.dList.push(row); }
  }

  wakeRun(oi) {
    const S = this.sched;
    if (oi >= 0 && !S.awake[oi]) { S.awake[oi] = 1; S.aList.push(oi); }
  }

  sleepStep() {
    this.version++;
    const A = this.audio, t = this.tick;
    if (A) A.pre(this);              // may wake everything (a new recorder): read sched after
    const S = this.sched;
    S.stats.ticks++;
    // Structure: the gates whose channels changed since they were last asked,
    // in row order -- the only list whose order shows: Pixels fired in one
    // tick paint in it, and a spatial recorder sums float gains in it. (A
    // typed array's sort is numeric and several times a comparator's speed.)
    const gl = Int32Array.from(S.gList).sort();
    S.gList = [];
    const gates = [];
    for (let i = 0; i < gl.length; i++) {
      const row = gl[i];
      S.gDirty[row] = 0;
      const g = S.gateAt[row];
      if (g) gates.push(g);
    }
    S.stats.gates += gates.length;
    this.structurePhase(A, this.screen, gates);
    // Belt: the runs woken by an event (the structure phase's included) or
    // by the clock.
    const timed = S.wakeAt.get(t);
    if (timed) {
      S.wakeAt.delete(t);
      S.stats.timed += timed.length;
      for (const oi of timed) this.wakeRun(oi);
    }
    // In any order: each run touches only its own two lanes, so no run's tick
    // can see another's.
    const al = S.aList;
    S.aList = [];
    for (const oi of al) S.awake[oi] = 0;
    S.stats.runs += al.length;
    for (const oi of al) this.tickAwake(this.order[oi], oi, t, A);
    if (A) this.hearMovers(t, A);
    // Direct hand-offs: the edges of every row that changed.
    // All decided from one state before any is applied, so in any order.
    const rows = S.dList;
    S.dList = [];
    const edges = [], mark = S.eMark;
    for (const r of rows) {
      S.dDirty[r] = 0;
      const l = S.rowEdges.get(r);
      if (l) for (const i of l) if (!mark[i]) { mark[i] = 1; edges.push(i); }
    }
    if (edges.length) {
      for (let j = 0; j < edges.length; j++) { mark[edges[j]] = 0; edges[j] = this.edges[edges[j]]; }
      S.stats.edges += edges.length;
      this.deliverDirect(edges);
    }
    this.tick++;
    if (A) A.post(this);
  }

  // One awake run's tick, and whether it may sleep after it.
  tickAwake(seg, oi, t, A) {
    const S = this.sched;
    S.ran[oi] = t;
    const moved = this.tickHeard(seg, t, A);
    const L = seg.length * this.UNIT;
    if (moved || seg.anchor < L) { this.wakeRun(oi); return; }
    if (seg.head === seg.tail) return;       // empty: only a push can change it, and that is an event
    // Asleep from here; when could the closed form alone make it act?
    const v = this.speed, d = this.spacing, h = seg.head, n = seg.tail - 1;
    let w = Infinity;
    if (this.positionOf(seg, h, t) < L) w = seg.enter[h % seg.cap] + Math.ceil(L / v);   // the head arrives
    if (!this.hasRoom(seg, t) && L - (n - h) * d >= d) {
      w = Math.min(w, seg.enter[n % seg.cap] + Math.ceil(d / v));                        // room opens
    }
    if (w !== Infinity) {
      if (w <= t) w = t + 1;                 // never in the past: a wake too early is only a wasted look
      let l = S.wakeAt.get(w);
      if (!l) S.wakeAt.set(w, l = []);
      l.push(oi);
    }
    // Sliding sleepers are tracked only for the audio, so only candidates
    // (hit() ignores the rest). A reconfigure wakes everything, which
    // rebuilds this list for the new candidates.
    if (A && seg.aIdx >= 0) {
      const P = Math.max(0, L - (n - h) * d);
      seg.moveUntil = seg.enter[n % seg.cap] + Math.ceil(P / v);
      if (seg.moveUntil > t + 1) S.moversNext.push(oi);
    }
  }

  // The audio bit for sleeping runs whose newest item is still sliding: on
  // every tick before `moveUntil`. A run ticked this tick was heard there.
  hearMovers(t, A) {
    const S = this.sched, m = S.movers;
    let j = 0;
    for (let i = 0; i < m.length; i++) {
      const oi = m[i];
      if (S.ran[oi] === t) continue;
      const seg = this.order[oi];
      if (t < seg.moveUntil) {
        A.hit(seg);
        if (t + 1 < seg.moveUntil) m[j++] = oi;
      }
    }
    m.length = j;
    for (const oi of S.moversNext) m.push(oi);
    S.moversNext = [];
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
  // entity slots it wrote (logged by setF(), so the rollback touches what
  // changed rather than copying the whole table -- 3.6 MB on a 32k-row
  // board, most of the forecast's cost), each run's head/tail/anchor, the one
  // ring slot each push writes (it may be the slot the popped head lived
  // in), the tick, the stats and the version. Cached on `version`, so it
  // costs one extra step per tick drawn, not per frame.
  forecast() {
    this.sync();
    if (this.fc && this.fc.version === this.version) return this.fc;
    // The forecast's step is the full one, with the wake list set aside: the
    // same tick (that is the wake list's whole claim), and nothing of the
    // scheduler's to roll back.
    const sched = this.sched;
    this.sched = null;
    const undo = this.undoLog = [];
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
      this.undoLog = null;
      this.sched = sched;
      const E = this.board.entities;
      for (let i = undo.length - 2; i >= 0; i -= 2) E[undo[i]] = undo[i + 1];
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
    c.audio = null; c.rec = null; c.fc = null; c.screen = null; c.undoLog = null;
    c.fired = this.fired.slice();
    c.truth = this.truth.slice();
    c.keys = copyKeys(this.keys);
    c.sched = this.sched ? c.makeSched() : null;   // its own, everything awake
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
      seg.lastPush = s.lastPush; seg.lastPop = s.lastPop; seg.truth = s.truth;
      seg.enter.set(s.enter); seg.payload.set(s.payload);
    }
    this.tick = snap.tick;
    this.fired.set(snap.fired);
    this.truth.set(snap.truth);
    this.keys = copyKeys(snap.keys);
    this.keysVersion = (this.keysVersion || 0) + 1;
    Object.assign(this.stats, snap.stats);
    this.version++;
    this.fc = null;
    this.screen?.clear();
    if (this.sched) this.sched = this.makeSched();
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
// The tick-rate bounds live in rate.js (see there); re-exported for callers.
export { RATE_MIN, RATE_MAX };
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

// entity.clamp_value(): only a result that is infinite *as float32* clamps,
// to +/- VALUE_LIMIT. Every finite float32 passes through, integer-exact or
// not, and NaN passes through too. JS computes in float64, so 1/1e-40 is
// finite here and infinite once it reaches the Float32Array -- hence fround.
const clampValue = (v, L) => {
  const f = Math.fround(v);
  return f === Infinity ? L : f === -Infinity ? -L : v;
};

// Entity.eval_ops(): a machine's arithmetic, from its exported opcode table.
// One entry per output channel; null means "emit nothing". `values` are the
// input channels (null when unfed), `konst` the Source's emit value.
// Which value a gate's truthiness reads, from its own declared data:
//   - a select rule (Merge), or a conditional op (Swap's OP_SEL, Route's
//     OP_GATED): the control input;
//   - otherwise the first output that is not a plain copy of an input (the
//     pass-throughs): Shear's sum, Mac's accumulator, Max, Floor, 1/a, and
//     a Source's emitted constant;
//   - no outputs at all (Pixel, Sink): always truthy.
// `tKind` 0 always, 1 an input channel, 2 an output channel; `tCh` which.
export function truthSource(s, O) {
  if (s.select) return { tKind: 1, tCh: s.select[0] };
  const ops = s.ops || [];
  for (const [op, ...a] of ops) if (op === O.OP_SEL || op === O.OP_GATED) return { tKind: 1, tCh: a[0] };
  for (let c = 0; c < ops.length; c++) {
    const op = ops[c][0];
    if (op !== O.OP_IN && op !== O.OP_NONE) return { tKind: 2, tCh: c };
  }
  return { tKind: 0, tCh: 0 };
}

// A fire's truthiness: 1 iff the value truthSource() names is > 0 (NaN is not).
export function truthOf(g, values, results) {
  if (g.tKind === 1) return values && values[g.tCh] > 0 ? 1 : 0;
  if (g.tKind === 2) return results[g.tCh] > 0 ? 1 : 0;
  return 1;
}

// The keyboards' queues, copied (clone(), restoreFrom(), simhost.js).
export function copyKeys(keys) {
  return new Map([...(keys || [])].filter(([, q]) => q.length).map(([r, q]) => [r, q.slice()]));
}

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
