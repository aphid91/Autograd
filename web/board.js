// The board: the save's own arrays, plus render lists derived from them.
//
// The grid (H*W*4 int32) and the entity table (rows*STRIDE float32) are held
// exactly as the save stores them, so writing a save back later is a dump
// rather than a translation, and the Python app and this one read each
// other's files. Everything else here is *derived* and rebuilt by derive().

import { readNpz, writeNpz } from "./npz.js";

export const FORMAT_VERSION = 1;

export class Board {
  constructor(T, arrays) {
    this.T = T;            // types.json
    this.arrays = arrays;  // every array from the save, kept for writing back
    const [w, h] = arrays.size.data;
    this.width = w;
    this.height = h;
    this.grid = arrays.grid.data;
    this.entities = arrays.entities.data;
    this.rows = arrays.entities.shape[0];
    // The row allocator, exactly as core/gameboard.py keeps it: rows come
    // off the free list first (popped from the end), then from next_row.
    // Row 0 is never handed out -- it means "none" in cells and partners.
    this.nextRow = arrays.next_row ? arrays.next_row.data[0] : 1;
    this.freeRows = arrays.free_rows ? Array.from(arrays.free_rows.data) : [];
    this.liveCount = arrays.live_count ? arrays.live_count.data[0] : 0;
    this.derive();
  }

  static async fromNpz(T, arrayBuffer) {
    const a = await readNpz(arrayBuffer);
    for (const k of ["version", "size", "grid", "entities"]) {
      if (!a[k]) throw new Error(`save is missing '${k}'`);
    }
    const version = a.version.data[0];
    if (version !== FORMAT_VERSION) {
      throw new Error(`save is format ${version}, this viewer reads ${FORMAT_VERSION}`);
    }
    const stride = a.entities.shape[1];
    if (stride !== T.entity.STRIDE) {
      throw new Error(`entity rows are ${stride} wide, expected ${T.entity.STRIDE}`);
    }
    const [w, h] = a.size.data;
    const gs = a.grid.shape;
    if (gs[0] !== h || gs[1] !== w || gs[2] !== 4) {
      throw new Error(`grid shape ${gs} does not match size ${w}x${h}`);
    }
    return new Board(T, a);
  }

  // The board as a save file, laid out as core/persistence.py save() writes
  // it, so the desktop loads it and it loads here. Segment membership (.w)
  // is derived and saved as zero, as the desktop does. The annotations blob
  // is carried through untouched: this app does not draw notes, but a save
  // that went through it must not lose them.
  async toNpz() {
    const E = this.T.entity;
    const grid = this.grid.slice();
    for (let i = 3; i < grid.length; i += 4) grid[i] = 0;
    const notes = this.arrays.annotations?.data;
    return writeNpz({
      version: { descr: "<i4", shape: [1], data: new Int32Array([FORMAT_VERSION]) },
      size: { descr: "<i4", shape: [2], data: new Int32Array([this.width, this.height]) },
      grid: { descr: "<i4", shape: [this.height, this.width, 4], data: grid },
      entities: { descr: "<f4", shape: [this.rows, E.STRIDE], data: this.entities },
      annotations: { descr: "<U", shape: [], data: typeof notes === "string" ? notes : "[]" },
      next_row: { descr: "<i4", shape: [1], data: new Int32Array([this.nextRow]) },
      free_rows: { descr: "<i4", shape: [this.freeRows.length], data: new Int32Array(this.freeRows) },
      live_count: { descr: "<i4", shape: [1], data: new Int32Array([this.liveCount]) },
    });
  }

  // --- raw access -------------------------------------------------------

  cellX(x, y) {
    if (x < 0 || y < 0 || x >= this.width || y >= this.height) return 0;
    return this.grid[(y * this.width + x) * 4];
  }
  cellZ(x, y) {
    if (x < 0 || y < 0 || x >= this.width || y >= this.height) return 0;
    return this.grid[(y * this.width + x) * 4 + 2];
  }
  cellType(word) { return (word >> this.T.cell.TYPE_SHIFT) & this.T.cell.TYPE_MASK; }
  cellDir(word) { return (word >> this.T.cell.DIR_SHIFT) & this.T.cell.DIR_MASK; }
  cellId(word) { return (word >> this.T.cell.ID_SHIFT) & this.T.cell.ID_MASK; }
  field(row, f) { return this.entities[row * this.T.entity.STRIDE + f]; }
  setField(row, f, v) { this.entities[row * this.T.entity.STRIDE + f] = v; }

  // The entity row covering cell (x, y), or -1. Every cell of a multi-cell
  // entity carries its row id, so this is one grid read.
  entityAt(x, y) {
    const w = this.cellX(x, y);
    if (this.cellType(w) === this.T.cell.TYPE_EMPTY) return -1;
    const row = this.cellId(w);
    return this.field(row, this.T.entity.F_ALIVE) ? row : -1;
  }

  // What the Status panel shows about a row. A fresh object each call:
  // like the desktop's views, never hold one across an edit.
  describe(row) {
    const E = this.T.entity;
    if (row < 0 || row >= this.rows || !this.field(row, E.F_ALIVE)) return null;
    const td = this.T.types[this.field(row, E.F_TYPE) | 0];
    if (!td) return null;
    return {
      row, td,
      x: this.field(row, E.F_ORIGIN_X) | 0,
      y: this.field(row, E.F_ORIGIN_Y) | 0,
      w: this.field(row, E.F_W) | 0,
      h: this.field(row, E.F_H) | 0,
      dir: this.field(row, E.F_DIR) | 0,
      flip: !!this.field(row, E.F_FLIPPED),
      emit: td.name === "source" ? this.field(row, E.F_EMIT) : null,
      seedOn: td.kind === "belt" ? !!this.field(row, E.F_SEED_ON) : null,
      seedValue: td.kind === "belt" ? this.field(row, E.F_SEED_VALUE) : null,
    };
  }

  // --- placing things ----------------------------------------------------
  // Ports of Gameboard._claim_row / spawn / _stamp_footprint and
  // Engine.pair_under, so a board built here is the board the desktop would
  // have built. Callers check isFree() first, as the desktop's do.

  isFree(x, y) {
    if (x < 0 || y < 0 || x >= this.width || y >= this.height) return false;
    return this.cellType(this.cellX(x, y)) === this.T.cell.TYPE_EMPTY;
  }

  claimRow() {
    if (this.freeRows.length) return this.freeRows.pop();
    if (this.nextRow >= this.rows) this.grow();
    return this.nextRow++;
  }

  // Doubling, as the desktop's _grow(). Ids are row indices, so nothing moves.
  grow() {
    const S = this.T.entity.STRIDE;
    const next = new Float32Array(this.rows * 2 * S);
    next.set(this.entities);
    this.entities = next;
    this.rows *= 2;
    this.arrays.entities = { ...this.arrays.entities, shape: [this.rows, S], data: next };
  }

  // A 1x1 entity (belt or underground end). Returns its row.
  spawn1(type, x, y, dir, { isExit = false } = {}) {
    const E = this.T.entity, C = this.T.cell, S = E.STRIDE;
    const row = this.claimRow();
    this.entities.fill(0, row * S, (row + 1) * S);
    this.setField(row, E.F_ALIVE, 1);
    this.setField(row, E.F_TYPE, type);
    this.setField(row, E.F_ORIGIN_X, x);
    this.setField(row, E.F_ORIGIN_Y, y);
    this.setField(row, E.F_DIR, dir);
    this.setField(row, E.F_W, 1);
    this.setField(row, E.F_H, 1);
    if (type === C.TYPE_UNDER) this.setField(row, E.F_UNDER_IS_EXIT, isExit ? 1 : 0);
    this.liveCount++;
    const i = (y * this.width + x) * 4;
    this.grid[i] = (dir << C.DIR_SHIFT) | (type << C.TYPE_SHIFT) | (row << C.ID_SHIFT);
    this.grid[i + 1] = 0;   // local offset (0, 0)
    this.grid[i + 2] = type === C.TYPE_UNDER && isExit ? C.UNDER_EXIT_BIT : 0;
    this.grid[i + 3] = 0;   // segment membership: derived by the desktop
    return row;
  }

  // A raw belt with an optional seed. `seed` null means no seed; a number
  // turns it on with that value, mirrored into .z as setSeed() does.
  spawnBelt(x, y, dir, seed = null) {
    const E = this.T.entity, C = this.T.cell;
    const row = this.spawn1(C.TYPE_BELT, x, y, dir);
    if (seed !== null) {
      this.setField(row, E.F_SEED_ON, 1);
      this.setField(row, E.F_SEED_VALUE, seed);
      this.grid[(y * this.width + x) * 4 + 2] |= C.SEED_BIT | (seed > 0 ? C.SEED_POS_BIT : 0);
    }
    return row;
  }

  // The cells a machine of `type` would cover at `origin`, facing `dir`,
  // and which of them are not free. The footprint fills its bounding box,
  // so the box is the cell set (core/entity.py cells_for()).
  footprint(type, [ox, oy], dir, flip) {
    const o = this.T.types[type].orient[`${dir}${flip ? 1 : 0}`];
    const cells = [], blocked = [];
    for (let lx = 0; lx < o.w; lx++) {
      for (let ly = 0; ly < o.h; ly++) {
        const c = { x: ox + lx, y: oy + ly };
        cells.push(c);
        if (!this.isFree(c.x, c.y)) blocked.push(c);
      }
    }
    return { o, cells, blocked };
  }

  // Place a machine, all or nothing: one blocked cell and nothing is built,
  // as the desktop's StructureBuilder. Returns the row, or -1.
  //
  // Each cell's .y holds its offset within the PLACED footprint, which is
  // what Gameboard._stamp_footprint() writes and what the desktop renderer
  // un-places to find a port.
  spawnStructure(type, origin, dir, flip, { emit = null, derive = true } = {}) {
    const E = this.T.entity, C = this.T.cell, S = E.STRIDE;
    const { o, blocked } = this.footprint(type, origin, dir, flip);
    if (blocked.length) return -1;
    const [ox, oy] = origin;
    const row = this.claimRow();
    this.entities.fill(0, row * S, (row + 1) * S);
    this.setField(row, E.F_ALIVE, 1);
    this.setField(row, E.F_TYPE, type);
    this.setField(row, E.F_ORIGIN_X, ox);
    this.setField(row, E.F_ORIGIN_Y, oy);
    this.setField(row, E.F_DIR, dir);
    this.setField(row, E.F_W, o.w);
    this.setField(row, E.F_H, o.h);
    this.setField(row, E.F_FLIPPED, flip ? 1 : 0);
    if (this.T.types[type].name === "source") this.setField(row, E.F_EMIT, emit ?? 0);
    this.liveCount++;
    const wordX = (dir << C.DIR_SHIFT) | (type << C.TYPE_SHIFT) | (row << C.ID_SHIFT);
    const wordZ = flip ? C.FLIP_BIT : 0;
    for (let lx = 0; lx < o.w; lx++) {
      for (let ly = 0; ly < o.h; ly++) {
        const i = ((oy + ly) * this.width + (ox + lx)) * 4;
        this.grid[i] = wordX;
        this.grid[i + 1] = (lx << C.LOCAL_SHIFT) | ly;
        this.grid[i + 2] = wordZ;
        this.grid[i + 3] = 0;
      }
    }
    if (derive) this.derive();
    return row;
  }

  // Link two underground ends, and mirror the pairing into both cells' .z.
  pairUnder(a, b) {
    const E = this.T.entity, C = this.T.cell;
    this.setField(a, E.F_UNDER_PARTNER, b);
    this.setField(b, E.F_UNDER_PARTNER, a);
    for (const r of [a, b]) {
      const x = this.field(r, E.F_ORIGIN_X) | 0, y = this.field(r, E.F_ORIGIN_Y) | 0;
      this.grid[(y * this.width + x) * 4 + 2] |= C.UNDER_PAIRED_BIT;
    }
  }

  // Drop the link from whatever was paired with `row` (Engine.unpair_under):
  // a surviving end left pointing at a freed row would draw a span to
  // nothing, and could re-pair wrongly once the row is reused.
  unpairUnder(row) {
    const E = this.T.entity, C = this.T.cell;
    if (!this.alive(row) || (this.field(row, E.F_TYPE) | 0) !== C.TYPE_UNDER) return;
    const other = this.field(row, E.F_UNDER_PARTNER) | 0;
    if (!other || !this.alive(other)) return;
    if ((this.field(other, E.F_UNDER_PARTNER) | 0) !== row) return;
    this.setField(other, E.F_UNDER_PARTNER, 0);
    const x = this.field(other, E.F_ORIGIN_X) | 0, y = this.field(other, E.F_ORIGIN_Y) | 0;
    this.grid[(y * this.width + x) * 4 + 2] &= ~C.UNDER_PAIRED_BIT;
  }

  alive(row) { return row > 0 && row < this.rows && !!this.field(row, this.T.entity.F_ALIVE); }

  // Gameboard.despawn(): clear every cell, zero the row, free it.
  despawn(row) {
    const E = this.T.entity, S = E.STRIDE;
    if (!this.alive(row)) return;
    const x = this.field(row, E.F_ORIGIN_X) | 0, y = this.field(row, E.F_ORIGIN_Y) | 0;
    const w = this.field(row, E.F_W) | 0, h = this.field(row, E.F_H) | 0;
    for (let lx = 0; lx < w; lx++) {
      for (let ly = 0; ly < h; ly++) this.grid.fill(0, ((y + ly) * this.width + x + lx) * 4, ((y + ly) * this.width + x + lx + 1) * 4);
    }
    this.entities.fill(0, row * S, (row + 1) * S);
    this.freeRows.push(row);
    this.liveCount--;
  }

  // Engine.erase() for a set of rows: unpair, despawn, derive once.
  eraseRows(rows) {
    for (const r of rows) { this.unpairUnder(r); this.despawn(r); }
    this.derive();
  }

  // Rows as stamp entries in ABSOLUTE coordinates (dx, dy = the origin).
  // An underground's partner becomes an index into the result when the
  // partner is among `rows`, else null -- core/clipboard.py capture().
  capture(rows) {
    const E = this.T.entity, C = this.T.cell;
    const index = new Map(rows.map((r, i) => [r, i]));
    return rows.map((r) => {
      const type = this.field(r, E.F_TYPE) | 0;
      const td = this.T.types[type];
      const partner = type === C.TYPE_UNDER ? this.field(r, E.F_UNDER_PARTNER) | 0 : 0;
      return {
        type: td.name,
        dx: this.field(r, E.F_ORIGIN_X) | 0, dy: this.field(r, E.F_ORIGIN_Y) | 0,
        dir: this.field(r, E.F_DIR) | 0, flip: !!this.field(r, E.F_FLIPPED),
        exit: type === C.TYPE_UNDER && !!this.field(r, E.F_UNDER_IS_EXIT),
        partner: partner && index.has(partner) ? index.get(partner) : null,
        emit: td.name === "source" ? this.field(r, E.F_EMIT) : null,
        seed: td.kind === "belt" && this.field(r, E.F_SEED_ON) ? this.field(r, E.F_SEED_VALUE) : null,
      };
    });
  }

  // Can entry `e` (a stamp entry) go at origin (x, y)? Returns its blocked
  // cells; empty means yes.
  entryBlocked(e, x, y) {
    const t = this.typeId(e.type);
    const o = this.T.types[t].orient[`${e.dir}${e.flip ? 1 : 0}`];
    const out = [];
    for (let lx = 0; lx < o.w; lx++) {
      for (let ly = 0; ly < o.h; ly++) if (!this.isFree(x + lx, y + ly)) out.push({ x: x + lx, y: y + ly });
    }
    return out;
  }

  typeId(name) {
    if (!this._typeIds) {
      this._typeIds = new Map(Object.entries(this.T.types).map(([id, td]) => [td.name, Number(id)]));
    }
    return this._typeIds.get(name);
  }

  // Place stamp entries, each at its own absolute origin `at[i]`, PARTIALLY:
  // every entry that fits is placed and any that clashes is skipped -- the
  // desktop's shift-click paste. Then pairs: first the stamp's own (both
  // ends placed), then each remaining unpaired end with its nearest
  // candidate, the order Engine.commit_ghosts() uses so a pasted exit cannot
  // be grabbed by an unrelated entrance at the paste site. Returns the set
  // of indices placed.
  placeEntries(entries, at) {
    const C = this.T.cell;
    const rows = new Map();
    entries.forEach((e, i) => {
      const [x, y] = at[i];
      if (this.entryBlocked(e, x, y).length) return;
      const t = this.typeId(e.type);
      let row;
      if (t === C.TYPE_BELT) row = this.spawnBelt(x, y, e.dir, e.seed ?? null);
      else if (t === C.TYPE_UNDER) row = this.spawn1(t, x, y, e.dir, { isExit: e.exit });
      else row = this.spawnStructure(t, [x, y], e.dir, e.flip, { emit: e.emit, derive: false });
      rows.set(i, row);
    });
    const E = this.T.entity;
    for (const [i, row] of rows) {
      const p = entries[i].partner;
      if (p !== null && p !== undefined && rows.has(p) && !this.field(row, E.F_UNDER_PARTNER)) {
        this.pairUnder(row, rows.get(p));
      }
    }
    for (const [i, row] of rows) {
      if (this.typeId(entries[i].type) !== C.TYPE_UNDER || this.field(row, E.F_UNDER_PARTNER)) continue;
      const mate = this.findUnderPartner(at[i][0], at[i][1], entries[i].dir, entries[i].exit);
      if (mate > 0) this.pairUnder(row, mate);
    }
    this.derive();
    return new Set(rows.keys());
  }

  // A belt-like cell as segtrace.js's node(): belt or underground end, or
  // null. An underground's partner is its row's partner, if alive.
  beltNode(x, y) {
    const E = this.T.entity, C = this.T.cell;
    const w = this.cellX(x, y), t = this.cellType(w);
    if (t !== C.TYPE_BELT && t !== C.TYPE_UNDER) return null;
    const row = this.cellId(w);
    if (!this.alive(row)) return null;
    const dir = this.cellDir(w);
    if (t === C.TYPE_BELT) return { kind: "belt", dir, exit: false, partner: null, key: row, row };
    const p = this.field(row, E.F_UNDER_PARTNER) | 0;
    const partner = p && this.alive(p) && (this.field(p, E.F_UNDER_PARTNER) | 0) === row
      ? [this.field(p, E.F_ORIGIN_X) | 0, this.field(p, E.F_ORIGIN_Y) | 0] : null;
    return { kind: "under", dir, exit: !!this.field(row, E.F_UNDER_IS_EXIT), partner, key: row, row };
  }

  // Engine.find_under_partner(): the nearest unpaired end of the opposite
  // role with the same facing, looking upstream from a new exit and
  // downstream from a new entrance. Nothing in between blocks.
  findUnderPartner(x, y, dir, isExit) {
    const E = this.T.entity, C = this.T.cell;
    const [sx, sy] = C.DIR_STEPS[dir];
    const sign = isExit ? -1 : 1;
    for (let d = 1; d <= this.T.under.MAX_SPAN; d++) {
      const cx = x + sx * sign * d, cy = y + sy * sign * d;
      if (cx < 0 || cy < 0 || cx >= this.width || cy >= this.height) break;
      const row = this.entityAt(cx, cy);
      if (row < 0 || (this.field(row, E.F_TYPE) | 0) !== C.TYPE_UNDER) continue;
      if (!!this.field(row, E.F_UNDER_IS_EXIT) === isExit) continue;
      if ((this.field(row, E.F_DIR) | 0) !== dir || this.field(row, E.F_UNDER_PARTNER)) continue;
      return row;
    }
    return -1;
  }

  // Build a planner.js plan: belts, then each underground pair. Blocked and
  // buried cells place nothing. Returns how many entities were placed.
  buildPlan(plan) {
    const C = this.T.cell;
    const rows = new Map();   // path index -> row
    plan.cells.forEach((c, i) => {
      if (c.kind === "blocked" || c.kind === "buried") return;
      if (!this.isFree(c.x, c.y)) return;   // the plan is stale; never overwrite
      rows.set(i, c.kind === "belt"
        ? this.spawn1(C.TYPE_BELT, c.x, c.y, c.dir)
        : this.spawn1(C.TYPE_UNDER, c.x, c.y, c.dir, { isExit: c.kind === "exit" }));
    });
    // Only pair ends that both got built: a survivor pointing at a row that
    // was never created is the dangling state the desktop guards against.
    for (const [a, e] of plan.pairs) {
      if (rows.has(a) && rows.has(e)) this.pairUnder(rows.get(a), rows.get(e));
    }
    this.derive();
    return rows.size;
  }

  // Turn a placed belt to face `dir`, in place: the row is kept, so its seed
  // travels with the turn, as the desktop's empty-hand R does
  // (Engine._restamp_turned). The facing lives in the row and in the cell's
  // .x, and both are written. Does not derive(): the caller's edit does.
  turnBelt(row, dir) {
    const E = this.T.entity, C = this.T.cell;
    this.setField(row, E.F_DIR, dir);
    const x = this.field(row, E.F_ORIGIN_X) | 0, y = this.field(row, E.F_ORIGIN_Y) | 0;
    const i = (y * this.width + x) * 4;
    this.grid[i] = (this.grid[i] & ~(C.DIR_MASK << C.DIR_SHIFT)) | (dir << C.DIR_SHIFT);
  }

  // --- per-instance settings ---------------------------------------------
  // Same fields the desktop's Status window writes, so a save made here
  // loads there with the setting intact.

  setEmit(row, value) {
    this.setField(row, this.T.entity.F_EMIT, value);
    this.derive();
  }

  // The seed's on-flag and sign are mirrored into the cell's .z, because
  // the desktop renderer reads them from the grid with no row lookup. A
  // row-only write would leave the desktop drawing a stale tint.
  setSeed(row, on, value) {
    const E = this.T.entity, C = this.T.cell;
    this.setField(row, E.F_SEED_ON, on ? 1 : 0);
    this.setField(row, E.F_SEED_VALUE, value);
    const x = this.field(row, E.F_ORIGIN_X) | 0, y = this.field(row, E.F_ORIGIN_Y) | 0;
    const i = (y * this.width + x) * 4 + 2;
    let z = this.grid[i] & ~(C.SEED_BIT | C.SEED_POS_BIT);
    if (on) z |= C.SEED_BIT | (value > 0 ? C.SEED_POS_BIT : 0);
    this.grid[i] = z;
    this.derive();
  }

  // --- derived render lists --------------------------------------------

  // Every edit ends here, so this is also where the simulation learns the
  // board changed: sim.js rebuilds (and starts fresh) when `revision` moves.
  derive() {
    this.revision = (this.revision || 0) + 1;
    const { T } = this;
    const E = T.entity, C = T.cell;
    const belts = [], unders = [], structs = [];
    const W = this.width;
    // Per cell: bitmask of world directions a structure output port emits
    // toward from that cell. Lets a belt see a machine feeding it.
    this.portOut = new Map();
    let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;

    for (let r = 0; r < this.rows; r++) {
      if (!this.field(r, E.F_ALIVE)) continue;
      const type = this.field(r, E.F_TYPE) | 0;
      const td = T.types[type];
      if (!td) continue;
      const x = this.field(r, E.F_ORIGIN_X) | 0;
      const y = this.field(r, E.F_ORIGIN_Y) | 0;
      const dir = this.field(r, E.F_DIR) | 0;
      const flip = this.field(r, E.F_FLIPPED) ? 1 : 0;
      const o = td.orient[`${dir}${flip}`];
      minX = Math.min(minX, x); minY = Math.min(minY, y);
      maxX = Math.max(maxX, x + o.w); maxY = Math.max(maxY, y + o.h);

      if (td.kind === "belt") {
        const z = this.cellZ(x, y);
        belts.push({ row: r, x, y, dir, inDir: -1,
          seed: (z & C.SEED_BIT) ? ((z & C.SEED_POS_BIT) ? 1 : -1) : 0 });
      } else if (td.kind === "under") {
        unders.push({ row: r, x, y, dir,
          exit: !!this.field(r, E.F_UNDER_IS_EXIT),
          partner: this.field(r, E.F_UNDER_PARTNER) | 0, span: null });
      } else {
        structs.push({ row: r, type, x, y, dir, flip, w: o.w, h: o.h, o,
          emit: td.name === "source" ? this.field(r, E.F_EMIT) : null });
        for (const [lx, ly, f] of o.outputs) {
          const k = (y + ly) * W + (x + lx);
          this.portOut.set(k, (this.portOut.get(k) || 0) | (1 << f));
        }
      }
    }

    for (const b of belts) b.inDir = this.incoming(b.x, b.y, b.dir);
    for (const u of unders) {
      // A paired entrance owns the span; the exit is found through the row.
      if (u.exit || !u.partner) continue;
      if (!this.field(u.partner, E.F_ALIVE)) continue;
      u.span = [this.field(u.partner, E.F_ORIGIN_X) | 0,
                this.field(u.partner, E.F_ORIGIN_Y) | 0];
    }

    this.belts = belts;
    this.unders = unders;
    this.structs = structs;
    this.bounds = isFinite(minX) ? { minX, minY, maxX, maxY } : null;
    this.count = belts.length + unders.length + structs.length;
  }

  // Does the cell at (nx, ny) emit toward direction `d` (i.e. at us)?
  emitsToward(nx, ny, d) {
    const C = this.T.cell;
    const w = this.cellX(nx, ny);
    const t = this.cellType(w);
    if (t === C.TYPE_EMPTY) return false;
    if (t === C.TYPE_BELT) return this.cellDir(w) === d;
    if (t === C.TYPE_UNDER) {
      // Only an exit surfaces; an entrance is diving.
      return !!(this.cellZ(nx, ny) & C.UNDER_EXIT_BIT) && this.cellDir(w) === d;
    }
    const m = this.portOut.get(ny * this.width + nx) || 0;
    return !!(m & (1 << d));
  }

  // Which side a belt takes from: core/segments.py take_dir(). Straight
  // through first, then counter-clockwise from the facing. The cell we
  // output into is never a source.
  incoming(x, y, facing, emits = (nx, ny, d) => this.emitsToward(nx, ny, d)) {
    const S = this.T.cell.DIR_STEPS;
    let mask = 0;
    for (let d = 0; d < 4; d++) {
      if (d === facing) continue;
      const nx = x + S[d][0], ny = y + S[d][1];
      if (emits(nx, ny, (d + 2) % 4)) mask |= 1 << d;
    }
    if (!mask) return -1;
    const behind = (facing + 2) % 4;
    if (mask & (1 << behind)) return behind;
    for (let i = 0; i < 4; i++) {
      const d = (facing + i) % 4;
      if (mask & (1 << d)) return d;
    }
    return -1;
  }
}
