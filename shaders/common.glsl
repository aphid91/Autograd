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
const int LOCAL_SHIFT = 16, LOCAL_MASK = 0xFFFF;

const int GHOST_NONE       = 0;
const int GHOST_PENDING    = 1;
const int GHOST_FRUSTRATED = 2;

const int TYPE_EMPTY = 0;
const int TYPE_BELT  = 1;
const int TYPE_ADD   = 2;
const int TYPE_MUL   = 3;

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

ivec2 cell_local(int w) {
    return ivec2((w >> LOCAL_SHIFT) & LOCAL_MASK, w & LOCAL_MASK);
}

bool is_empty(int w) { return cell_type(w) == TYPE_EMPTY; }

// --- structure ports ----------------------------------------------------
//
// MUST match Entity.INPUTS / OUTPUTS and faces_for() in core/entity.py.
// Ports are per-cell: every cell of a multi-cell footprint carries its own
// faces, so a shader can answer "is this face a port?" from the cell alone,
// with no footprint-wide reasoning.
//
// Authored at DIR_PX and rotated into the entity's facing:
//   Add  inputs +Y, outputs -Y   (on each of its two cells)
//   Mul  inputs +Y and -X, outputs +X and -Y
//
// Faces are returned as a 4-bit mask indexed by direction.

int rotate_face_mask(int mask, int facing) {
    int out_mask = 0;
    for (int d = 0; d < 4; ++d) {
        if ((mask & (1 << d)) != 0) out_mask |= 1 << ((d + facing) % 4);
    }
    return out_mask;
}

int input_face_mask(int type, int facing) {
    int m = 0;
    if (type == TYPE_ADD) m = (1 << DIR_PY);
    else if (type == TYPE_MUL) m = (1 << DIR_PY) | (1 << DIR_NX);
    else return 0;
    return rotate_face_mask(m, facing);
}

int output_face_mask(int type, int facing) {
    int m = 0;
    if (type == TYPE_ADD) m = (1 << DIR_NY);
    else if (type == TYPE_MUL) m = (1 << DIR_PX) | (1 << DIR_NY);
    else return 0;
    return rotate_face_mask(m, facing);
}

bool has_face(int mask, int dir) { return (mask & (1 << dir)) != 0; }

bool is_structure(int type) {
    return type != TYPE_EMPTY && type != TYPE_BELT;
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
