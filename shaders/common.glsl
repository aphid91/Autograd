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
const int SEED_SHIFT     = 5;   // .z bit 5: belt cell seeds its run
const int SEED_POS_SHIFT = 6;   // .z bit 6: that seed's value is positive
const int LOCAL_SHIFT = 16, LOCAL_MASK = 0xFFFF;
// .w: belt segment membership. Derived state, stamped by core/segments.py.
const int SEG_OFF_SHIFT = 0,  SEG_OFF_MASK = 0xFFFF;
const int SEG_ID_SHIFT  = 16, SEG_ID_MASK  = 0x7FFF;  // 15 bits: sign stays clear

const int GHOST_NONE       = 0;
const int GHOST_PENDING    = 1;
const int GHOST_FRUSTRATED = 2;
// A frustration whose occupant is exactly what the pending build wanted: the
// stamp is lined up. Tinted differently so "already correct" does not read as
// "blocked" -- see core/cell.py's GHOST_MATCHED.
const int GHOST_MATCHED    = 3;

// Type ids are generated from cell.TYPE_NAMES, and the per-type tables
// further down from the Entity subclasses -- see core/glsl_types.py, which
// render/shader_loader.py expands at load time. Nothing here names a
// machine by hand.
// @generated type-ids

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
// A seeded belt cell, and the sign of what it seeds. The value itself is not
// in the cell -- only the sign, which is all a tint can convey and matches
// what the item discs themselves report.
bool is_seeded     (int w) { return ((w >> SEED_SHIFT)     & 1) != 0; }
bool is_seed_positive(int w) { return ((w >> SEED_POS_SHIFT) & 1) != 0; }

ivec2 cell_local(int w) {
    return ivec2((w >> LOCAL_SHIFT) & LOCAL_MASK, w & LOCAL_MASK);
}

// Segment id from .w, or 0 for a cell on no segment.
int segment_id(int w) { return (w >> SEG_ID_SHIFT) & SEG_ID_MASK; }
// Distance from the segment's tail, in item positions. Not the same as the
// cell's index along the run: an underground span advances this without
// contributing a drawn cell, which is why it is stored rather than counted.
int segment_offset(int w) { return (w >> SEG_OFF_SHIFT) & SEG_OFF_MASK; }

// --- belt item transport -------------------------------------------------
//
// The closed form from docs/belt_report.md, evaluated per pixel. MUST match
// BeltSim.position_of() in core/belts.py -- tests/test_glsl.py pins the two.
//
// Positions are never stored, so the shader recomputes them rather than
// reading them: materialising O(items) of state per frame is exactly the
// cost the closed form exists to avoid. What it reads instead is the three
// mutable per-segment values the CPU tick maintains, plus the ring.
//
// Layout mirrors core/belt_device.py. Two parallel arrays of fixed-size
// vectors rather than one struct: std430's alignment rules for mixed-type
// structs are a reliable source of silent corruption.

//: Fixed-point scale. MUST match belts.UNIT in core/belts.py.
const float BELT_UNIT = 256.0;

layout(std430, binding = 1) buffer SegmentsI {
    // .x head, .y tail, .z base, .w cap
    ivec4 seg_i[];
};
layout(std430, binding = 2) buffer SegmentsF {
    // .x anchor, .y length, .z speed, .w spacing
    vec4 seg_f[];
};
layout(std430, binding = 3) buffer BeltItems {
    // .x enter_tick, .y payload
    vec2 items[];
};

// The entity table, as flat floats. Row `r` starts at `r * ENTITY_STRIDE`.
// Generated constants give the field offsets -- see core/glsl_types.py, which
// emits ENTITY_STRIDE and the gate-state offsets from core/entity.py so the
// two cannot drift.
layout(std430, binding = 0) buffer Entities {
    float ent[];
};

float ent_field(int row, int offset) {
    return ent[row * ENTITY_STRIDE + offset];
}

// Is output channel `c` of this gate still holding a value the belt has not
// taken? That is the back-pressure signal, and at the end of a tick it means
// the gate produced something its belt refused -- drawn squished between the
// two. While material flows it is always empty by end of tick.
bool gate_out_held(int row, int c) {
    return ent_field(row, F_OUT_HELD + c) != 0.0;
}

float gate_out_value(int row, int c) {
    return ent_field(row, F_OUT_VALUE + c);
}

// Is input channel `c` fed? The mirror of gate_out_held(), and the other
// half of "is this machine holding anything" -- see the sleep overlay in
// world.frag, which is currently its only caller.
bool gate_in_held(int row, int c) {
    return ent_field(row, F_IN_HELD + c) != 0.0;
}

// Where item `k` of a segment sits at `tick + alpha`, in fixed point.
//
// `tick` and `alpha` stay separate rather than arriving as one float: the
// tick counter grows without bound and a float32 loses sub-tick resolution
// past ~16.7M ticks, and `anchor` is the value as of the *whole* tick, so
// alpha has to be applied to it explicitly rather than recovered with
// fract().
//
// The anchor advances with alpha *including* the clamp at the end, matching
// the whole-tick `min(anchor + v, L)`. Without that clamp a blocked clump
// slides forward between ticks and the head drifts off the end of the belt.
float item_position(int sid, int k, int tick, float alpha) {
    ivec4 si = seg_i[sid];
    vec4  sf = seg_f[sid];
    int   slot = si.z + (k % si.w);

    float age = float(tick - int(items[slot].x)) + alpha;
    // Cap the age so a long-settled item cannot lose precision in the
    // product; anything past the end is clamped by the min anyway.
    age = min(age, sf.y / max(sf.z, 1.0) + 1.0);
    float unblocked = age * sf.z;

    float anchor = min(sf.x + alpha * sf.z, sf.y);
    // Not named `packed` -- that is a GLSL reserved word.
    float packed_pos = anchor - float(k - si.x) * sf.w;
    return min(unblocked, packed_pos);
}

float item_payload(int sid, int k) {
    ivec4 si = seg_i[sid];
    return items[si.z + (k % si.w)].y;
}

// Is this segment at rest -- i.e. would the belt phase skip it?
//
// MUST match BeltSim.is_at_rest() in core/belts.py. Derived here rather than
// uploaded as a flag because every term is already in the segment record:
// head, tail and anchor are the three mutable fields, and length/spacing are
// the constants beside them. A sleeping segment is a fixed point of the tick
// (the anchor saturates, an empty ring cannot pop, an idle producer offers
// nothing), so "asleep" is a function of state the renderer already has.
//
// `tick` is taken as a parameter rather than read from a uniform so this
// file stays self-contained -- common.glsl declares no uniforms, and every
// other function here is a pure function of its arguments and the SSBOs.
// Evaluated at whole-tick state, with no alpha: sleeping is a property of
// the simulation's decision, not of where the interpolation currently sits.
// A run that woke this tick should read as awake for the whole frame.
bool segment_at_rest(int sid, int tick) {
    ivec4 si = seg_i[sid];
    vec4  sf = seg_f[sid];
    if (si.x == si.y) {
        // Empty: inert once the anchor has saturated at the far end. The
        // anchor clause is load-bearing -- an empty run whose anchor is
        // still advancing is not yet a fixed point.
        return sf.x >= sf.y;
    }
    // Holding items: inert exactly while nothing can enter, which is the
    // room test from BeltSim.has_room() -- the last item has not yet moved a
    // full spacing clear of the entrance. This is the RAM case: a run
    // storing a value nobody is taking.
    return item_position(sid, si.y - 1, tick, 0.0) < sf.w;
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
//   Source 2x1  one output on the +X tip
//   Sink   2x2  two inputs per cell, on that cell's two outward faces
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

// Glyph ids, referenced by the generated type table below. A type names a
// glyph rather than drawing one inline, so several machines can share a mark.
const int GLYPH_NONE  = 0;
const int GLYPH_PLUS  = 1;
const int GLYPH_CROSS = 2;   // X
const int GLYPH_ZED   = 3;   // Z
const int GLYPH_DOT   = 4;
const int GLYPH_DIVIDE = 5;  // obelus, for Reciprocal (1/x)
const int GLYPH_CHEVRONS = 6; // stacked >>, for Max
const int GLYPH_ARROW_DOWN = 7; // for Floor
const int GLYPH_WYE = 8;     // Y, for Merge (two in, one out)
const int GLYPH_WYE_DOWN = 9; // upside-down Y, for Route (one in, two out)

// Footprints, per-cell port masks, control axes, colours and glyphs --
// all emitted from the Entity subclasses. See core/glsl_types.py.
// @generated type-tables

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

// A placed face direction -> the authored one. Inverse of the rotate-then-
// flip that input_face_mask()/output_face_mask() apply, and in the opposite
// order: they flip then rotate, so undoing means un-rotate then un-flip.
//
// Needed when the *identity* of a port matters rather than merely whether a
// face is one -- looking up which channel a port drives, say. The masks only
// answer "is this a port?", which is orientation-independent; a channel
// index is not.
int unrotate_face(int placed, int facing, bool flipped) {
    int d = (placed - facing + 8) % 4;
    if (flipped) {
        if (d == DIR_PY) return DIR_NY;
        if (d == DIR_NY) return DIR_PY;
    }
    return d;
}

// pass_through_mask() -- the authored axes a type passes through unchanged
// -- is generated above from Entity.PASS_THROUGH. It is declared data, not
// derived: geometrically a mutated channel is indistinguishable from a
// pass-through (Shear's y enters -X and leaves +X exactly as its x enters
// +Y and leaves -Y), so only the machine's semantics tell them apart.

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

// Which of several incoming directions a cell actually takes material from.
// `mask` is a 4-bit set of directions (as seen from the receiving cell) that
// hold something emitting at us. Returns -1 for none.
//
// MUST match take_dir() in core/segments.py. Belts never merge in this game,
// but a player can still point two at one cell; one source wins
// deterministically and the rest are left unconnected. The simulation and the
// renderer have to agree on which, or the picture would show an arm that
// carries nothing -- that disagreement is the old "phantom merge" goof.
//
// Prefer straight-through (the cell directly behind us), then counter-
// clockwise from our own facing.
int take_dir(int facing, int mask) {
    if (mask == 0) return -1;
    int behind = (facing + 2) % 4;
    if ((mask & (1 << behind)) != 0) return behind;
    for (int i = 0; i < 4; ++i) {
        int d = (facing + i) % 4;
        if ((mask & (1 << d)) != 0) return d;
    }
    return -1;
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

// --- the bitfield font -------------------------------------------------
//
// Text is drawn from a 6x8 on/off bitfield per character, two ints wide,
// generated from core/font.py into the FONT_LO / FONT_HI tables above. A
// pixel finds which glyph cell it lands in, tests that one bit, and takes a
// box distance against the cell -- so a character costs an integer op and one
// sd_box whichever character it is. No texture, no sampler, no 36-way branch
// over glyph shapes.
//
// This replaced a seven-segment display, which could draw ten digits and a
// handful of fakeable letters. See core/font.py for why a bitfield rather
// than an atlas or per-glyph stroke SDFs.
//
// Glyph space is one unit per glyph *cell*: a character occupies
// x in [0, FONT_W], y in [0, FONT_H], origin at its bottom-left. Callers
// scale and place it. That is a different convention from sd_glyph's
// centred-on-origin space, deliberately -- text is laid out by advancing an
// origin, not by centring each mark.

// Is cell (x, y) of glyph `code` lit? Out-of-range cells and codes read
// clear, so a caller need not clamp before asking.
bool font_bit(int code, int x, int y) {
    if (code < 0 || code >= FONT_COUNT) return false;
    if (x < 0 || x >= FONT_W || y < 0 || y >= FONT_H) return false;
    int bit = y * FONT_W + x;
    // Two 24-bit halves, so neither can touch the sign bit -- see
    // core/font.py glyph_bits().
    int word = (bit < 24) ? FONT_LO[code] : FONT_HI[code];
    int shift = (bit < 24) ? bit : bit - 24;
    return ((word >> shift) & 1) != 0;
}

// Distance to glyph `code`, with `p` in glyph space (see above).
//
// Only the cell the point falls in is tested, plus nothing else: the lit
// cells of a glyph form a union of unit boxes, and for a point inside the
// glyph's bounding box the nearest lit cell is overwhelmingly the one it is
// in or an immediate neighbour. Testing the 3x3 neighbourhood gives a
// distance that is correct wherever it matters for antialiasing -- within a
// pixel of an edge -- and is a conservative over-estimate further away,
// where only the sign is read.
//
// Returning a real distance rather than a boolean is what lets text
// antialias through the usual fill(d, px) and stay crisp at any zoom, which
// is the whole reason a bitfield here is not simply a bitmap.
float sd_font_glyph(int code, vec2 p) {
    if (code < 0 || code >= FONT_COUNT) return 1e9;
    ivec2 c = ivec2(floor(p));
    float d = 1e9;
    for (int dy = -1; dy <= 1; ++dy) {
        for (int dx = -1; dx <= 1; ++dx) {
            ivec2 q = c + ivec2(dx, dy);
            if (!font_bit(code, q.x, q.y)) continue;
            // Unit box centred on the cell.
            vec2 centre = vec2(q) + 0.5;
            d = min(d, sd_box(p - centre, vec2(0.5)));
        }
    }
    return d;
}

// --- number formatting -------------------------------------------------
//
// A value becomes a short string of glyph codes, which the caller then draws.
// Formatting happens in the shader rather than CPU-side because the payload
// is already here: pre-formatting would mean per-item state in the SSBO,
// which is exactly the O(items) upload the closed form exists to avoid.
//
// The policy is a handful of knobs, so "how many digits" is a tunable rather
// than a rewrite:
//
//   sig      significant figures to keep after the decimal point
//   trim     drop trailing zeros, so -1.00 reads as -1 and 2.50 as 2.5
//
// Output is written into `codes` with the count returned. NUM_MAX_CODES
// bounds every buffer: GLSL has no dynamic arrays, so the caller declares a
// fixed one and this never writes past it.
//
// 16 is the worst case with headroom. The longest output is a fully
// untrimmed negative fixed-point value at the top of the fixed range:
// sign + NUM_FIXED_HI_E integer digits + point + sig decimals, which at
// sig=4 is 1 + 7 + 1 + 4 = 13. Overflowing this does not crash -- num_push()
// drops the excess -- but it silently truncates a number, which is exactly
// the failure a display must not have, so test_glsl.py sweeps the formatter
// and asserts nothing reaches the bound.
#define NUM_MAX_CODES 16

// The exponent range that stays in fixed-point notation. Outside it a value
// goes scientific, which is the only way to fit a huge or tiny magnitude in
// a few columns.
#define NUM_FIXED_HI_E   7     // < 1e7 stays fixed
#define NUM_FIXED_LO_E  -4     // >= 1e-4 stays fixed

// Append one code, if there is room. Every write goes through this so the
// bound is checked in one place rather than at each of the dozen call sites
// below -- a missed check there would be an out-of-bounds write, which on
// some drivers is silent corruption rather than an error.
void num_push(inout int codes[NUM_MAX_CODES], inout int n, int code) {
    if (n < NUM_MAX_CODES) { codes[n] = code; n = n + 1; }
}

// Format `value` into `codes`, returning how many were written.
//
// `sig` is digits after the point; `trim` drops trailing zeros (and then a
// bare trailing point), which is what turns -1.00 into -1.
int format_number(float value, int sig, bool trim,
                  out int codes[NUM_MAX_CODES]) {
    int n = 0;
    for (int i = 0; i < NUM_MAX_CODES; ++i) codes[i] = CODE_SPACE;

    // NaN first: it is the one value meaning "this computation had no
    // answer" (see the gate numeric policy) and every comparison below is
    // false for it, so without this it would format as "0".
    if (isnan(value)) {
        num_push(codes, n, CODE_NAN_0);
        num_push(codes, n, CODE_NAN_1);
        num_push(codes, n, CODE_NAN_2);
        return n;
    }

    bool neg = value < 0.0;
    float mag = abs(value);

    if (isinf(mag)) {
        if (neg) num_push(codes, n, CODE_MINUS);
        num_push(codes, n, CODE_INF_0);
        num_push(codes, n, CODE_INF_1);
        num_push(codes, n, CODE_INF_2);
        return n;
    }

    // A minus is a column of its own now. The seven-segment display could
    // not afford one -- three columns were three digits and the sign lived
    // only in the ring colour -- but a 6-wide glyph cell and a tolerance for
    // spilling past the disc rim buys it, and "-1" is worth far more than
    // the third significant figure it costs.
    if (neg) num_push(codes, n, CODE_MINUS);

    if (mag == 0.0) {
        num_push(codes, n, CODE_DIGIT_0);
        return n;
    }

    float e = floor(log(mag) / log(10.0));

    if (e >= float(NUM_FIXED_HI_E) || e < float(NUM_FIXED_LO_E)) {
        // Scientific: one digit, the point and up to `sig` decimals if they
        // are not all zero, then E and the signed exponent.
        float mant = mag / pow(10.0, e);
        // Rounding the mantissa can carry it to 10, which needs
        // renormalising rather than clamping -- 9.96e3 is 1E4, not 9.9E3.
        float scale = pow(10.0, float(sig));
        float r = floor(mant * scale + 0.5);
        if (r >= 10.0 * scale) { r = r / 10.0; e += 1.0; }

        int whole = int(floor(r / scale));
        num_push(codes, n, CODE_DIGIT_0 + whole);

        int frac = int(r - float(whole) * scale);
        int fdigits = sig;
        if (trim) {
            while (fdigits > 0 && (frac % 10) == 0) { frac /= 10; fdigits -= 1; }
        }
        if (fdigits > 0) {
            num_push(codes, n, CODE_POINT);
            for (int i = fdigits - 1; i >= 0; --i) {
                int dv = int(mod(floor(float(frac) / pow(10.0, float(i))), 10.0));
                num_push(codes, n, CODE_DIGIT_0 + dv);
            }
        }

        num_push(codes, n, CODE_E);
        if (e < 0.0) num_push(codes, n, CODE_MINUS);
        int ae = int(abs(e));
        if (ae >= 10) num_push(codes, n, CODE_DIGIT_0 + (ae / 10) % 10);
        num_push(codes, n, CODE_DIGIT_0 + ae % 10);
        return n;
    }

    // Fixed point. Round once at the requested precision, then split -- not
    // the other way round, or a value like 9.999 would print its integer
    // part as 9 and its rounded fraction as 00.
    float scale = pow(10.0, float(sig));
    float r = floor(mag * scale + 0.5);
    float whole = floor(r / scale);
    int frac = int(r - whole * scale);

    int fdigits = sig;
    if (trim) {
        while (fdigits > 0 && (frac % 10) == 0) { frac /= 10; fdigits -= 1; }
    }

    // Integer part, most significant digit first. A loop down from a fixed
    // ceiling rather than a digit count: NUM_FIXED_HI_E bounds it at 7
    // digits, and leading zeros are skipped.
    bool started = false;
    for (int place = 7; place >= 0; --place) {
        float pw = pow(10.0, float(place));
        int dv = int(mod(floor(whole / pw), 10.0));
        if (dv != 0) started = true;
        if (!started && place > 0) continue;
        num_push(codes, n, CODE_DIGIT_0 + dv);
    }

    if (fdigits > 0) {
        num_push(codes, n, CODE_POINT);
        for (int i = fdigits - 1; i >= 0; --i) {
            int dv = int(mod(floor(float(frac) / pow(10.0, float(i))), 10.0));
            num_push(codes, n, CODE_DIGIT_0 + dv);
        }
    }
    return n;
}

// --- drawing a run of codes --------------------------------------------
//
// Lay `count` glyphs out left to right and return coverage at `p`.
//
// `p` is in *glyph cell* units with the origin at the text's centre, so the
// caller scales by whatever it wants a cell to measure and does not have to
// know the string's width. Centring here rather than at the call site is
// what makes a variable-width number -- which is the point of trimming --
// stay put instead of drifting as digits come and go.
//
// The point advance is narrower than a full cell: a decimal point carries
// only a 2-wide pip, so giving it the same 6 cells as a digit leaves a
// visible hole in the middle of a number.
#define NUM_POINT_ADV  3.0

// Total width of a code run, in glyph cells.
float text_width(int codes[NUM_MAX_CODES], int count) {
    float w = 0.0;
    for (int i = 0; i < count; ++i) {
        w += (codes[i] == CODE_POINT) ? NUM_POINT_ADV : float(FONT_W);
    }
    return w;
}

// Distance to the nearest lit cell of the run, with `p` centred as above.
float sd_text(int codes[NUM_MAX_CODES], int count, vec2 p) {
    float w = text_width(codes, count);
    // Walk the pen from the left edge. Glyphs sit on rows 1..7 of an 8-row
    // cell, so the ink's vertical centre is at FONT_H*0.5 -- using the cell
    // centre keeps the baseline consistent across a mixed run.
    vec2 q = p + vec2(w * 0.5, float(FONT_H) * 0.5);
    float d = 1e9;
    float pen = 0.0;
    for (int i = 0; i < count; ++i) {
        float adv = (codes[i] == CODE_POINT) ? NUM_POINT_ADV : float(FONT_W);
        // Centre the narrow point glyph in its narrow advance.
        float off = (codes[i] == CODE_POINT)
                  ? pen - (float(FONT_W) - NUM_POINT_ADV) * 0.5
                  : pen;
        d = min(d, sd_font_glyph(codes[i], q - vec2(off, 0.0)));
        pen += adv;
    }
    return d;
}

// Format and draw in one step, which is what every caller actually wants.
// Returns the distance field; the caller applies fill() with its own px.
float sd_number(float value, int sig, bool trim, vec2 p) {
    int codes[NUM_MAX_CODES];
    int n = format_number(value, sig, trim, codes);
    return sd_text(codes, n, p);
}
