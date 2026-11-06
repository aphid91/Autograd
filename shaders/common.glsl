// Cell encoding and shared helpers.
//
// MUST match core/cell.py -- see the module docstring there. This is the only
// GLSL copy of the bit layout; every shader that reads the grid includes this
// file rather than repeating it.
//
//   .x  bits 0-1  direction, 2-8 type, 9-30 entity id
//   .y  packed (local_x, local_y) within a multi-cell footprint
//   .z  bits 0-1  ghost state

const int DIR_SHIFT  = 0,  DIR_MASK   = 0x3;
const int TYPE_SHIFT = 2,  TYPE_MASK  = 0x7F;
const int ID_SHIFT   = 9,  ID_MASK    = 0x3FFFFF;
const int GHOST_SHIFT = 0, GHOST_MASK = 0x3;
const int FLIP_SHIFT = 2;   // .z bit 2: mirrored about the authored X axis
const int UNDER_EXIT_SHIFT   = 3;   // .z bit 3: this end surfaces
const int UNDER_PAIRED_SHIFT = 4;   // .z bit 4: this end has a partner
const int LOCAL_SHIFT = 16, LOCAL_MASK = 0xFFFF;

const int GHOST_NONE       = 0;
const int GHOST_PENDING    = 1;
const int GHOST_FRUSTRATED = 2;

const int TYPE_EMPTY  = 0;
const int TYPE_BELT   = 1;
const int TYPE_SWAP   = 2;
const int TYPE_SHEAR  = 3;
const int TYPE_MAC    = 4;
const int TYPE_SOURCE = 5;
const int TYPE_SINK   = 6;
const int TYPE_UNDER  = 7;   // underground belt end

// Directions. These index DIR_VECS below.
const int DIR_PX = 0;   // +X east
const int DIR_PY = 1;   // +Y north
const int DIR_NX = 2;   // -X west
const int DIR_NY = 3;   // -Y south

const ivec2 DIR_VECS[4] = ivec2[4](
    ivec2( 1,  0),
    ivec2( 0,  1),
    ivec2(-1,  0),
    ivec2( 0, -1)
);

int cell_type  (int w) { return (w >> TYPE_SHIFT)  & TYPE_MASK;  }
int cell_dir   (int w) { return (w >> DIR_SHIFT)   & DIR_MASK;   }
int cell_id    (int w) { return (w >> ID_SHIFT)    & ID_MASK;    }
int ghost_state(int w) { return (w >> GHOST_SHIFT) & GHOST_MASK; }
bool is_flipped (int w) { return ((w >> FLIP_SHIFT) & 1) != 0; }
bool is_under_exit  (int w) { return ((w >> UNDER_EXIT_SHIFT)   & 1) != 0; }
bool is_under_paired(int w) { return ((w >> UNDER_PAIRED_SHIFT) & 1) != 0; }

ivec2 cell_local(int w) {
    return ivec2((w >> LOCAL_SHIFT) & LOCAL_MASK, w & LOCAL_MASK);
}

bool is_empty(int w) { return cell_type(w) == TYPE_EMPTY; }

// --- structure ports ----------------------------------------------------
//
// MUST match Entity.INPUTS / OUTPUTS and Entity.face_masks_at() in
// core/entity.py. Ports are per-cell: a port lives on *one* cell of a
// footprint, not on all of them, so these take the cell's authored local
// offset as well as its type. That is what lets a shader answer "is this
// face a port?" from a single cell with no footprint-wide reasoning.
//
// Masks are 4-bit, indexed by direction, authored at DIR_PX.
//
//   Swap   1x2  x down the whole body (in top, out bottom);
//               y and z cross their own cell, west to east
//   Shear  2x1  x down through cell 0 only; y in at cell 0's west face,
//               out at cell 1's east face as x+y
//   Mac    2x1  Shear plus a second vertical channel down cell 1
//   Source 4x1  one output on the +X tip
//   Sink   3x3  one input at the middle of each side
//
// .y holds the *placed* offset (flipped, then rotated, then shifted into the
// +X/+Y quadrant -- see Entity.place_local()), NOT the authored one. The
// tables below are authored at DIR_PX, so a placed offset must be inverted
// back into authored space before any lookup: unplace_local() here mirrors
// Entity.unplace_local(). Looking up the placed offset directly un-rotates a
// port's *face* while leaving its *cell* put, which lands ports on interior
// edges of a rotated footprint -- a 3x3 Sink is the clearest case, since its
// extent is rotation-invariant and so hides the error everywhere else.

#define FACE(d) (1 << (d))

// The authored (DIR_PX) footprint of a type. MUST match Entity.FOOTPRINT.
ivec2 type_footprint(int type) {
    if (type == TYPE_SWAP)   return ivec2(1, 2);
    if (type == TYPE_SHEAR)  return ivec2(2, 1);
    if (type == TYPE_MAC)    return ivec2(2, 1);
    if (type == TYPE_SOURCE) return ivec2(4, 1);
    if (type == TYPE_SINK)   return ivec2(3, 3);
    return ivec2(1, 1);
}

ivec2 rotate_offset(ivec2 p, int dir) {
    for (int i = 0; i < (dir % 4); ++i) p = ivec2(-p.y, p.x);
    return p;
}

// Placed offset (what .y holds) -> authored offset. Inverse of
// Entity.place_local(); see Entity.unplace_local().
ivec2 unplace_local(int type, ivec2 p, int facing, bool flipped) {
    ivec2 fp = type_footprint(type);
    // Undo the shift that kept the rotated block in the +X/+Y quadrant.
    ivec2 lo = ivec2(0);
    for (int cx = 0; cx < 2; ++cx) {
        for (int cy = 0; cy < 2; ++cy) {
            ivec2 corner = rotate_offset(
                ivec2(cx == 0 ? 0 : fp.x - 1, cy == 0 ? 0 : fp.y - 1), facing);
            lo = min(lo, corner);
        }
    }
    ivec2 a = rotate_offset(p + lo, (4 - facing) % 4);
    if (flipped) a.y = (fp.y - 1) - a.y;
    return a;
}

int rotate_face_mask(int mask, int facing) {
    int out_mask = 0;
    for (int d = 0; d < 4; ++d) {
        if ((mask & (1 << d)) != 0) out_mask |= 1 << ((d + facing) % 4);
    }
    return out_mask;
}

// Mirror a face mask about the X axis: +Y <-> -Y. Matches cell.flip_dir_y().
int flip_face_mask(int mask) {
    int out_mask = mask & (FACE(DIR_PX) | FACE(DIR_NX));
    if ((mask & FACE(DIR_PY)) != 0) out_mask |= FACE(DIR_NY);
    if ((mask & FACE(DIR_NY)) != 0) out_mask |= FACE(DIR_PY);
    return out_mask;
}

// Authored input mask for one local cell of a type.
int input_face_mask_local(int type, ivec2 lcl) {
    if (type == TYPE_SWAP) {
        if (lcl == ivec2(0, 1)) return FACE(DIR_PY) | FACE(DIR_NX);
        if (lcl == ivec2(0, 0)) return FACE(DIR_NX);
    } else if (type == TYPE_SHEAR) {
        if (lcl == ivec2(0, 0)) return FACE(DIR_PY) | FACE(DIR_NX);
    } else if (type == TYPE_MAC) {
        if (lcl == ivec2(0, 0)) return FACE(DIR_PY) | FACE(DIR_NX);
        if (lcl == ivec2(1, 0)) return FACE(DIR_PY);
    } else if (type == TYPE_SINK) {
        if (lcl == ivec2(1, 2)) return FACE(DIR_PY);
        if (lcl == ivec2(1, 0)) return FACE(DIR_NY);
        if (lcl == ivec2(0, 1)) return FACE(DIR_NX);
        if (lcl == ivec2(2, 1)) return FACE(DIR_PX);
    }
    return 0;
}

// Authored output mask for one local cell of a type.
int output_face_mask_local(int type, ivec2 lcl) {
    if (type == TYPE_SWAP) {
        if (lcl == ivec2(0, 1)) return FACE(DIR_PX);
        if (lcl == ivec2(0, 0)) return FACE(DIR_NY) | FACE(DIR_PX);
    } else if (type == TYPE_SHEAR) {
        if (lcl == ivec2(0, 0)) return FACE(DIR_NY);
        if (lcl == ivec2(1, 0)) return FACE(DIR_PX);
    } else if (type == TYPE_MAC) {
        if (lcl == ivec2(0, 0)) return FACE(DIR_NY);
        if (lcl == ivec2(1, 0)) return FACE(DIR_NY) | FACE(DIR_PX);
    } else if (type == TYPE_SOURCE) {
        if (lcl == ivec2(3, 0)) return FACE(DIR_PX);
    }
    return 0;
}

// `lcl` is the PLACED offset straight out of .y. The pipeline mirrors
// Entity.faces_for(): invert the cell back to authored space, read its
// authored faces, then apply the same flip and rotation to those faces so
// cell and face move together.
int input_face_mask(int type, ivec2 lcl, int facing, bool flipped) {
    int m = input_face_mask_local(
        type, unplace_local(type, lcl, facing, flipped));
    if (flipped) m = flip_face_mask(m);
    return rotate_face_mask(m, facing);
}

int output_face_mask(int type, ivec2 lcl, int facing, bool flipped) {
    int m = output_face_mask_local(
        type, unplace_local(type, lcl, facing, flipped));
    if (flipped) m = flip_face_mask(m);
    return rotate_face_mask(m, facing);
}

bool has_face(int mask, int dir) { return (mask & (1 << dir)) != 0; }

// --- control channels ---------------------------------------------------
//
// The authored axes a type passes through *unchanged*. MUST match
// Entity.PASS_THROUGH. This is declared, not derived: geometrically a
// mutated channel is indistinguishable from a pass-through (Shear's y
// enters -X and leaves +X exactly as its x enters +Y and leaves -Y), so
// only the machine's semantics can tell them apart.
//
// Returned as a face mask in authored space; the caller rotates it.
int pass_through_mask(int type) {
    if (type == TYPE_SWAP)  return FACE(DIR_NY);
    if (type == TYPE_SHEAR) return FACE(DIR_NY);
    if (type == TYPE_MAC)   return FACE(DIR_NY);
    return 0;
}

// Glyph ids, used by world.frag's per-type visual table. A type names a
// glyph rather than drawing one inline, so several machines can share a mark.
const int GLYPH_NONE  = 0;
const int GLYPH_PLUS  = 1;
const int GLYPH_CROSS = 2;   // X
const int GLYPH_ZED   = 3;   // Z
const int GLYPH_DOT   = 4;

// A machine with declared port faces. Undergrounds are excluded: they have
// no port tables and behave like belts for connection purposes, taking input
// from behind and emitting ahead along their own facing.
bool is_structure(int type) {
    return type != TYPE_EMPTY && type != TYPE_BELT && type != TYPE_UNDER;
}

// Types that move material along a single facing, belt-style.
bool is_beltlike(int type) {
    return type == TYPE_BELT || type == TYPE_UNDER;
}

// Rotate a cell-local coordinate so that +X becomes `dir`.
// Local space is centred on the cell, -0.5..0.5.
vec2 rotate_to_dir(vec2 p, int dir) {
    if (dir == DIR_PX) return p;
    if (dir == DIR_PY) return vec2(p.y, -p.x);
    if (dir == DIR_NX) return vec2(-p.x, -p.y);
    return vec2(-p.y, p.x);
}

// Unit vector pointing along a direction, in cell-local space.
vec2 dir_vec(int dir) { return vec2(DIR_VECS[dir]); }

// --- signed distance helpers -------------------------------------------

// Antialiased fill: 1 inside, 0 outside, smooth over ~1px.
float fill(float sd, float px) {
    return 1.0 - smoothstep(-px, px, sd);
}

float sd_segment(vec2 p, vec2 a, vec2 b) {
    vec2 pa = p - a, ba = b - a;
    float h = clamp(dot(pa, ba) / dot(ba, ba), 0.0, 1.0);
    return length(pa - ba * h);
}

float sd_box(vec2 p, vec2 half_extent) {
    vec2 d = abs(p) - half_extent;
    return length(max(d, 0.0)) + min(max(d.x, d.y), 0.0);
}

float sd_triangle(vec2 p, vec2 a, vec2 b, vec2 c) {
    vec2 e0 = b - a, e1 = c - b, e2 = a - c;
    vec2 v0 = p - a, v1 = p - b, v2 = p - c;
    vec2 q0 = v0 - e0 * clamp(dot(v0, e0) / dot(e0, e0), 0.0, 1.0);
    vec2 q1 = v1 - e1 * clamp(dot(v1, e1) / dot(e1, e1), 0.0, 1.0);
    vec2 q2 = v2 - e2 * clamp(dot(v2, e2) / dot(e2, e2), 0.0, 1.0);
    float s = sign(e0.x * e2.y - e0.y * e2.x);
    vec2 d = min(min(vec2(dot(q0, q0), s * (v0.x * e0.y - v0.y * e0.x)),
                     vec2(dot(q1, q1), s * (v1.x * e1.y - v1.y * e1.x))),
                     vec2(dot(q2, q2), s * (v2.x * e2.y - v2.y * e2.x)));
    return -sqrt(d.x) * sign(d.y);
}

// A chevron (">" shape) pointing along +X, centred at the origin.
// `half_len` is its reach along X, `half_span` across it.
float sd_chevron(vec2 p, float half_len, float half_span, float thick) {
    // Two mirrored segments meeting at the tip.
    vec2 tip  = vec2(half_len, 0.0);
    vec2 up   = vec2(-half_len, half_span);
    vec2 dn   = vec2(-half_len, -half_span);
    float d = min(sd_segment(p, tip, up), sd_segment(p, tip, dn));
    return d - thick;
}
