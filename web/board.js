// The board: the save's own arrays, plus render lists derived from them.
//
// The grid (H*W*4 int32) and the entity table (rows*STRIDE float32) are held
// exactly as the save stores them, so writing a save back later is a dump
// rather than a translation, and the Python app and this one read each
// other's files. Everything else here is *derived* and rebuilt by derive().

import { readNpz } from "./npz.js";

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

  derive() {
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
  incoming(x, y, facing) {
    const S = this.T.cell.DIR_STEPS;
    let mask = 0;
    for (let d = 0; d < 4; d++) {
      if (d === facing) continue;
      const nx = x + S[d][0], ny = y + S[d][1];
      if (this.emitsToward(nx, ny, (d + 2) % 4)) mask |= 1 << d;
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
