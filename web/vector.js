// Vectors: one action repeated at k * v for k = 0 .. count-1.
//
// Pure: offsets, the vector's turns, a cell-claims overlay for laying copies
// in sequence, and the chain follow used by vectored selection. main.js owns
// the gesture and decides what a "copy" is for each tool.

// The most copies a vector makes. More (x2) stops here.
export const VEC_MAX = 256;
export const VEC_MIN = 2;

export const isZero = (v) => v[0] === 0 && v[1] === 0;

// Rotate the offset a quarter turn, world +Y up: clockwise takes east to
// south. Flip negates its x.
export const rotateVec = ([x, y], cw) => (cw ? [y, -x] : [-y, x]);
export const flipVec = ([x, y]) => [-x, y];

export const moreCount = (n) => Math.min(VEC_MAX, n * 2);
export const lessCount = (n) => Math.max(VEC_MIN, Math.floor(n / 2));

// The offsets of every copy, the original first. A zero vector makes no
// copies: they would all sit on the original.
export function offsets(v, count) {
  if (!v || isZero(v)) return [[0, 0]];
  const out = [];
  for (let k = 0; k < count; k++) out.push([k * v[0], k * v[1]]);
  return out;
}

// Cells claimed by copies already laid, over a board's own free(x, y): copy
// k sees copies 0..k-1 as occupied, so copies are laid in sequence and a
// later one never lands on an earlier one.
export class Claims {
  constructor(free) {
    this.base = free;
    this.taken = new Set();
  }
  free = (x, y) => !this.taken.has(`${x},${y}`) && this.base(x, y);
  claim(cells) { for (const [x, y] of cells) this.taken.add(`${x},${y}`); }
  fits(cells) { return cells.every(([x, y]) => this.free(x, y)); }
}

// Lay copies in sequence. `place(offset, claims)` returns the cells the copy
// at that offset would occupy and whether it fits, as { cells, ok }, and
// optionally `claim`: the cells it will really take (a partly blocked
// original stamp takes only its free ones). The original (k = 0) is always
// kept -- it shows red where it is blocked, as a single placement does --
// and a later copy is kept only when it fits whole.
// Returns [{ k, offset, ...whatever place returned }] for the kept ones.
export function layCopies(offs, free, place) {
  const claims = new Claims(free);
  const kept = [];
  offs.forEach((offset, k) => {
    const r = place(offset, claims);
    if (k > 0 && !r.ok) return;
    claims.claim(r.claim ?? (r.ok ? r.cells : []));
    kept.push({ k, offset, ...r });
  });
  return kept;
}

// Vectored selection: follow `v` from a pattern for as long as the whole
// pattern repeats. `matchAt(offset)` says whether every entity of the
// pattern is found at that offset; the first offset where it is not ends
// the chain. Returns the offsets found, in order (not the pattern itself).
export function followChain(v, matchAt, limit = 4096) {
  const out = [];
  if (!v || isZero(v)) return out;
  for (let k = 1; k <= limit; k++) {
    const off = [k * v[0], k * v[1]];
    if (!matchAt(off)) break;
    out.push(off);
  }
  return out;
}
