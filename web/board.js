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
