#version 430

// World renderer. A fullscreen quad maps each pixel to a grid cell, reads the
// cell (and its 4 orthogonal neighbours) from the grid texture, and draws the
// grid, belts, and structures.

#include "common.glsl"

out vec4 fragColor;

uniform vec2  resolution;     // framebuffer size, px
uniform vec2  cam_center;     // view centre, world (cell) units
uniform float cam_zoom;       // pixels per cell
uniform ivec2 board_size;     // grid dimensions, cells
uniform isampler2D grid;      // RGBA32I, one texel per cell

// Simulation time, split so the whole-tick part keeps full integer precision:
// item positions are a closed-form function of (tick - enter_tick), and a
// float32 carrying a large absolute tick count would lose the sub-tick
// resolution the interpolation depends on. Render time is sim_tick + sim_alpha.
uniform int   sim_tick;       // whole ticks elapsed
uniform float sim_alpha;      // fraction into the next tick, 0..1
// Copy mode's live selection rectangle, in world units: `lo` is the lower
// left corner, `hi` is one past the upper right. Screen furniture rather
// than board state -- nothing is stored per cell for it.
uniform vec2  select_lo;
uniform vec2  select_hi;
uniform int   select_on;      // 0 when no selection is in progress

// ======================================================================
// TUNABLES -- everything here is safe to fiddle with.
// ======================================================================

// --- palette ---
#define COL_VOID        vec3(0.07, 0.07, 0.09)
#define COL_BG          vec3(0.12, 0.12, 0.14)
#define COL_GRID        vec3(0.22, 0.22, 0.26)
#define COL_GRID_MAJOR  vec3(0.30, 0.30, 0.35)
#define COL_BORDER      vec3(0.45, 0.45, 0.52)

#define COL_BELT        vec3(0.46, 0.47, 0.52)   // the spine
#define COL_CHEVRON     vec3(0.74, 0.76, 0.82)   // direction fringes
#define COL_STRUCT_MARK vec3(0.88, 0.90, 0.94)   // the glyph
#define COL_IN          vec3(0.35, 0.60, 0.95)   // input ports  (blue)
#define COL_OUT         vec3(0.95, 0.35, 0.30)   // output ports (red)
#define COL_FRUSTRATED  vec3(0.90, 0.25, 0.22)
// Copy mode's selection rectangle: a bright outline with a faint wash, so
// what is caught reads clearly over belts and machines alike.
#define COL_SELECT      vec3(0.35, 0.72, 0.98)
#define SELECT_EDGE     0.06   // outline half-width, in cells
#define SELECT_FILL     0.13   // interior wash alpha

// --- belts ---
#define BELT_WIDTH        0.34   // spine thickness, in cells
#define BELT_EDGE_SOFT    0.0    // extra softening on the spine edge
#define CHEVRON_PERIOD    0.5    // cells between chevrons along a run
#define CHEVRON_LEN       0.11   // reach along the travel axis
#define CHEVRON_SPAN      0.11   // half-width across the belt
#define CHEVRON_THICK     0.030  // stroke half-thickness
#define CHEVRON_INSET     0.25   // nudge chevrons along the run
// At a merge, tributary chevrons stop this far short of the centre so the
// converging streams stay legible instead of piling up on each other.
#define MERGE_GAP         0.18

// --- structures ---
#define STRUCT_INSET      0.42   // body half-extent within a cell
#define MARK_THICK        0.045  // Plus / X stroke half-thickness
#define MARK_RADIUS       0.26   // Plus / X arm reach (footprint units)
#define PORT_ARROW_LEN    0.11   // port arrow reach
#define PORT_ARROW_SPAN   0.10
#define PORT_ARROW_THICK  0.034
#define PORT_OFFSET       0.34   // distance from cell centre to the port
// A short belt-coloured stub bridging a port to a connected neighbour, so a
// structure and the belt it talks to read as physically joined. Only drawn
// when there is actually something on the other side.
#define STUB_WIDTH        0.34   // matches BELT_WIDTH by default
#define STUB_REACH        0.50   // out to the cell edge
// How far a cell probes for siblings when recovering its footprint extent.
// Must be >= (longest footprint axis - 1); the longest is 2, so 1 would do.
// Kept at 3 deliberately: it is headroom for the next machine added, and
// test_glsl.py asserts only the lower bound, so a too-small value would
// surface as a multi-cell body drawing in pieces rather than as a failure.
#define PROBE_REACH       3
// A faint belt drawn straight through a machine on any cell whose ports pass
// a signal through unchanged -- the "control" channels, which enter one face
// and leave the opposite one. Shows at a glance which way material survives
// the operation. Drawn over the body, under the glyph.
#define PASS_ALPHA        0.82   // overlay opacity -- tweak to taste
#define PASS_WIDTH        0.22   // spine thickness, in cells
#define PASS_CHEVRON      1.0    // chevron opacity, relative to PASS_ALPHA

// A Source draws its emitted value across its body, so what a generator
// produces is readable from the board instead of needing a click. Sized to
// be read at the zoom you build at -- considerably bigger than an item's
// label, since it sits on a 2x1 footprint rather than inside a 0.22-cell
// disc, and since it is the one number on the board that does not move.
//
// The value is read straight out of the entity row (Source.F_EMIT, which the
// generated constants expose as F_IN_VALUE -- a generator has no input
// channels, so that slot is free; see core/entity.py). Nothing new is
// uploaded for this: the entity SSBO is already bound and the cell already
// carries the row id.
#define EMIT_NUM_SIZE     0.052  // one glyph cell, in cells
#define EMIT_NUM_SIG      2
#define EMIT_NUM_TRIM     true
#define COL_EMIT_NUM      vec3(0.99, 0.99, 1.00)
// Legibility cutoff, in glyph pixel height, exactly as the item labels do it.
#define EMIT_MIN_PX       6.0
#define EMIT_GOOD_PX     11.0

// --- underground belts ---
// Each end draws a funnel: a tapered mouth on the surface side plus a solid
// throat on the buried side, so entrance and exit read as mirror images of
// one another. A paired run shows a dotted line along the span it jumps.
#define COL_UNDER         vec3(0.42, 0.45, 0.55)   // the funnel body
#define COL_UNDER_DOT     vec3(0.62, 0.66, 0.76)   // the span dots
#define UNDER_MOUTH       0.46   // half-width of the funnel's open end
#define UNDER_THROAT      0.15   // half-width where it goes under
#define UNDER_LEN         0.42   // reach from cell centre, each way
#define UNDER_DOT_R       0.075  // span dot radius, in cells
#define UNDER_DOT_PERIOD  0.5    // cells between span dots
#define UNDER_DOT_ALPHA   0.55
// Greatest distance between a pair's two ends. MUST match
// Entity.Underground.MAX_SPAN -- test_drive.py pins the two together.
#define UNDER_MAX_SPAN    5

// --- belt items ---
// Each item is a disc with a coloured ring -- green when its value is above
// zero, red otherwise -- carrying its value as a three-figure seven-segment
// number. The ring still reads at a glance when the digits are too small to
// resolve, so the sign survives all the way down the zoom range.
#define ITEM_RADIUS       0.22   // disc radius, in cells
#define ITEM_RING         0.055  // ring thickness, in cells

// The number on the face of an item, in the 6x8 bitfield font (see
// core/font.py and the font section of common.glsl). ITEM_NUM_SIZE is the
// size of one *glyph cell* in cells, so a character is FONT_H * ITEM_NUM_SIZE
// cells tall and FONT_W * ITEM_NUM_SIZE wide.
//
// The text is deliberately allowed to **spill past the disc rim**. A trimmed
// number is 1-4 columns wide depending on its value, and sizing for the
// worst case inside a 0.165-cell inner radius would make the common short
// cases far smaller than they need to be. The disc reads as a token with a
// label on it rather than a boxed display, and the ring still carries the
// sign underneath.
// Sized so a short label -- the common case, 1-3 glyphs after trimming --
// is readable at the zoom you actually build at without swamping the disc.
// Two failures bracket this value, and both are worth knowing:
//
//  * 0.030 put a glyph at 11px tall at a build zoom of ~46px/cell, inside
//    the fade band, so labels rendered as fragments and "137" read as "37".
//    That looks exactly like a formatter bug and is not one.
//  * 0.055 was legible but made a saturated belt wall-to-wall text: at one
//    item per cell the labels abut, and the discs and their sign-carrying
//    rings disappear underneath. The number is not the only thing an item
//    has to say.
#define ITEM_NUM_SIZE     0.042  // one glyph cell, in cells
#define COL_ITEM_NUM      vec3(0.97, 0.98, 1.00)
// A dark halo behind the text, so a digit staying legible does not depend on
// the disc fill it happens to overlap -- the spill means part of a number
// sits over the belt, the frustration tint or another item.
#define ITEM_NUM_HALO     0.9    // halo reach, in glyph cells
#define COL_ITEM_HALO     vec3(0.06, 0.07, 0.09)
#define ITEM_HALO_ALPHA   0.72
// Digits after the decimal point, and whether to drop trailing zeros. With
// trimming on, -1.00 draws as "-1" and 2.50 as "2.5", which is the whole
// reason the formatter takes the flag.
#define ITEM_NUM_SIG      2
#define ITEM_NUM_TRIM     true
// Digits fade out once a glyph is too small to read, leaving the plain disc.
// Expressed in *glyph pixel height* rather than in zoom, because that is the
// quantity legibility actually depends on: a glyph is FONT_H*ITEM_NUM_SIZE
// cells tall, and `px` is cells-per-pixel (1/cam_zoom), so the height in
// pixels is that over px. A 6x8 bitfield glyph needs more pixels than a
// seven-segment one did to resolve -- it has real interior detail rather
// than seven fat bars -- so these are higher than the 5/8 they replaced.
// A 6x8 bitfield glyph has real interior detail rather than seven fat bars,
// so it needs more pixels to resolve than the seven-segment display did --
// but the band must still clear a normal build zoom, or labels sit
// permanently half-faded and read as fragments.
#define NUM_MIN_PX        6.0   // gone at or below this glyph height
#define NUM_GOOD_PX      10.0   // fully drawn at or above it
#define COL_ITEM_POS      vec3(0.38, 0.82, 0.42)   // value > 0
#define COL_ITEM_NEG      vec3(0.90, 0.36, 0.34)   // value <= 0
#define COL_ITEM_FILL     vec3(0.14, 0.15, 0.18)   // disc interior
// How many items to examine per pixel once bisection has found the first
// candidate. An item can colour a pixel from up to (0.5 + reach) cells away,
// where `reach` is the disc radius or the number's half-width, whichever is
// wider -- and consecutive items are at least `spacing` apart, so at the
// default spacing of one cell this bounds how many can overlap one cell. The
// scan breaks out as soon as it passes behind the cell, so the ceiling only
// costs anything where items really are that dense.
//
// Raised from 4 when numbers were allowed to spill past the rim: the text
// half-width is several times the disc radius, so the window a pixel must
// consider grew with it. Too small shows as a number vanishing when its
// owner is a couple of cells away while nearer ones still draw.
#define ITEM_SCAN         8
// Bisection steps to locate that first candidate. Needs ceil(log2(ring
// capacity)); 16 covers a 65536-slot ring, far past the 16-bit cap on a
// segment's length.
#define ITEM_BISECT       16

// A gate's output waiting zone, when the belt refused to take it. Drawn
// squished into the gap between the body edge and the cell boundary, which
// is the only place it can go -- it is not on the belt and not inside the
// machine. Narrower than a normal disc on the travel axis so it reads as
// compressed rather than simply mispositioned.
#define WAIT_SQUISH       0.46   // scale along the travel axis
#define WAIT_OFFSET       0.44   // distance from the gate cell's centre

// --- seeded belts ---
// A belt the player marked as a seed injects an item onto its run every time
// segments are re-derived. The cell is tinted by the *sign* of that value --
// green above zero, red at or below, the same split the item discs use -- so
// a glance across the board says where the seeds are and roughly what they
// carry. Drawn over the belt body and under the items, which is the layer
// "this cell is configured" belongs in: it is a property of the cell, unlike
// an item (cargo on top) or frustration (a transient overlay).
#define COL_SEED_POS      vec3(0.30, 0.74, 0.36)
#define COL_SEED_NEG      vec3(0.82, 0.30, 0.28)
#define SEED_ALPHA        0.50
#define SEED_INSET        0.30   // half-extent of the tinted patch, in cells

// --- segment debug overlay ---
// Tints each belt cell by a hash of its segment id, so a run's extent and
// where it is cut are visible at a glance. Compiled out entirely unless
// SEGMENT_DEBUG is defined -- the loader injects that define when the
// inspection panel asks for it, so this costs nothing when off.
#define SEG_DEBUG_ALPHA   0.55
#define SEG_DEBUG_INSET   0.34   // half-extent of the tinted patch, in cells
// Marks the tail (upstream) end of each segment with a brighter dot, so the
// direction a run is indexed from is readable.
#define SEG_DEBUG_TAIL_R  0.12

// --- ghosts / overlays ---
#define GHOST_ALPHA       0.45
#define FRUSTRATED_ALPHA  0.55
#define FRUSTRATED_INSET  0.46

// --- grid ---
#define MAJOR_EVERY       8
#define GRID_FADE_LO      3.0    // zoom (px/cell) where grid lines vanish
#define GRID_FADE_HI      9.0    // ...and where they reach full strength
#define GRID_ALPHA        0.09

// ======================================================================

// Per-type colour and glyph come from type_colour() / type_glyph(), which
// core/glsl_types.py generates from the Entity subclasses -- a machine's
// appearance is declared beside its ports, in one place.

// --- grid access --------------------------------------------------------
bool in_board(ivec2 c) {
    return all(greaterThanEqual(c, ivec2(0))) && all(lessThan(c, board_size));
}

ivec4 fetch(ivec2 c) {
    if (!in_board(c)) return ivec4(0);
    return texelFetch(grid, c, 0);
}

// Two cells belong to the same "network" for rendering purposes when they
// share a ghost state. Ghosts chain with ghosts, real belts with real belts,
// so a preview reads as its own run rather than fusing with what is already
// built. Change this predicate to let ghosts connect to real belts.
bool same_layer(int a_z, int b_z) {
    return ghost_state(a_z) == ghost_state(b_z);
}

// Does the cell at `c` emit into the neighbour it faces along `toward`?
//
// Two kinds of source count. A belt emits along its own facing, so it feeds us
// iff its direction points back at us -- which is why nothing diverges and
// merges are the only junction to handle. A *structure* emits from each of its
// output faces, so it feeds us iff it has an output face pointing at us. That
// makes a belt downstream of a machine draw an incoming arm, exactly as if a
// belt were there.
bool feeds_into(ivec2 c, int toward, int my_z) {
    ivec4 n = fetch(c);
    int t = cell_type(n.x);
    if (t == TYPE_EMPTY) return false;
    if (!same_layer(my_z, n.z)) return false;
    if (t == TYPE_BELT) return cell_dir(n.x) == toward;
    // An underground end moves material along its own facing like a belt,
    // but only an *exit* emits onto the surface -- an entrance is diving,
    // so it feeds the span underneath rather than the cell ahead.
    if (t == TYPE_UNDER) {
        return is_under_exit(n.z) && cell_dir(n.x) == toward;
    }
    // Per-cell ports: ask the neighbour cell about its *own* local offset,
    // not ours -- only the cell of the footprint that actually carries the
    // output face emits.
    return has_face(output_face_mask(t, cell_local(n.y), cell_dir(n.x),
                                     is_flipped(n.z)), toward);
}

// Does the cell at `c` accept input from the neighbour facing it along
// `toward`? Only structures accept on a declared face; a belt's "input" is
// implicit (anything facing it), and is handled by feeds_into from the other
// side.
bool accepts_from(ivec2 c, int toward, int my_z) {
    ivec4 n = fetch(c);
    int t = cell_type(n.x);
    if (!is_structure(t)) return false;
    if (!same_layer(my_z, n.z)) return false;
    return has_face(input_face_mask(t, cell_local(n.y), cell_dir(n.x),
                                    is_flipped(n.z)), toward);
}

// ======================================================================
// Belts
// ======================================================================
//
// A belt is a thick spine through the cell plus chevrons showing travel.
// The 5-cell neighbourhood fixes the topology: our own facing gives the
// outgoing arm, and any neighbour whose facing points at us gives an
// incoming arm. Straights, corners, T and PLUS merges all fall out of that
// -- no lookup table needed.
//
// Chevron phase comes from the world coordinate along the travel axis rather
// than from the cell, so the pattern is continuous across cell boundaries on
// a straight run. It necessarily resets at a corner, where the axis changes;
// that reads as natural because the belt visibly turns there.

struct BeltDraw {
    float body;     // spine coverage
    float chevron;  // chevron coverage
};

// Chevrons marching along a straight arm from `start_t` to `end_t`, measured
// in cells along `axis` (a unit direction vector in cell-local space).
// `phase` is the world-space offset that keeps neighbouring cells in step.
float chevrons_along(vec2 p, vec2 axis, float t0, float t1,
                     float phase, float px) {
    // Component along / across the arm.
    float t = dot(p, axis);
    vec2 perp = vec2(-axis.y, axis.x);
    float s = dot(p, perp);

    if (t < t0 - CHEVRON_LEN || t > t1 + CHEVRON_LEN) return 0.0;

    // Snap to the nearest chevron centre in the repeating lattice.
    float k = floor((t + phase) / CHEVRON_PERIOD + 0.5);
    float best = 1e9;
    // Check the nearest two so a chevron straddling the boundary is not lost.
    for (int i = -1; i <= 1; ++i) {
        float centre = (k + float(i)) * CHEVRON_PERIOD - phase + CHEVRON_INSET;
        if (centre < t0 || centre > t1) continue;
        vec2 local = vec2(t - centre, s);
        best = min(best, sd_chevron(local, CHEVRON_LEN, CHEVRON_SPAN,
                                    CHEVRON_THICK));
    }
    return fill(best, px);
}

BeltDraw draw_belt(vec2 p, ivec2 ci, ivec4 c, float px, vec2 world) {
    BeltDraw o;
    o.body = 0.0;
    o.chevron = 0.0;

    int out_dir = cell_dir(c.x);
    vec2 out_vec = dir_vec(out_dir);

    // --- spine ---
    // The outgoing arm always runs from the centre to the output edge.
    float half_w = BELT_WIDTH * 0.5;
    float sd = sd_segment(p, vec2(0.0), out_vec * 0.5) - half_w;

    // Gather every neighbour emitting at us, then let take_dir() pick the one
    // the *simulation* will actually take from. Belts never merge here, so
    // drawing an arm per candidate would show material arriving down a path
    // nothing travels -- the phantom merge. Exactly one incoming arm is drawn,
    // chosen by the same rule core/segments.py uses.
    int in_mask_belt = 0;
    for (int d = 0; d < 4; ++d) {
        // Never treat the cell we output into as a source. A structure can
        // have an output face pointing back at a belt that feeds it (a
        // machine emits +Y while the belt above runs -Y into it), and taking that as
        // an incoming arm would lay two opposed chevron streams on one arm.
        // The belt's own facing wins: it is leaving, not arriving.
        if (d == out_dir) continue;

        ivec2 nc = ci + DIR_VECS[d];
        // A neighbour in direction d feeds us if it faces the opposite way.
        int toward = (d + 2) % 4;
        if (!feeds_into(nc, toward, c.z)) continue;
        in_mask_belt |= 1 << d;
    }
    int in_dir = take_dir(out_dir, in_mask_belt);
    if (in_dir >= 0) {
        sd = min(sd, sd_segment(p, vec2(0.0), dir_vec(in_dir) * 0.5) - half_w);
    }
    bool any_in = in_dir >= 0;
    // With one source at most there is no merge left to draw: a corner has
    // one stream in and one out, and stays continuous.
    if (!any_in) {
        // Extend backwards so a lone belt or a run start fills its cell.
        sd = min(sd, sd_segment(p, -out_vec * 0.5, out_vec * 0.5) - half_w);
    }
    o.body = fill(sd - BELT_EDGE_SOFT, px);

    // --- chevrons ---
    // One stream per incoming arm, each running from its edge to the centre,
    // plus the outgoing arm from centre to edge. On a straight run the
    // incoming and outgoing arms share an axis and the phase makes them one
    // continuous march.
    float chev = 0.0;

    // Outgoing: along the travel axis, phase locked to world position so
    // adjacent cells continue the same lattice.
    {
        float wphase = dot(world, out_vec);
        // Normally centre -> output edge. When something feeds us from
        // directly behind, that incoming stream is collinear with this one,
        // so this single pass covers the whole cell instead -- one lattice,
        // no seam and no doubling at the midpoint.
        bool fed_from_behind =
            feeds_into(ci + DIR_VECS[(out_dir + 2) % 4], out_dir, c.z);
        float t0 = (fed_from_behind || !any_in) ? -0.5 : 0.0;
        chev = max(chev, chevrons_along(p, out_vec, t0, 0.5,
                                        wphase - dot(p, out_vec), px));
    }

    // The single incoming arm, if it is not collinear with the outgoing one.
    // A stream arriving from directly behind travels our own axis, and the
    // outgoing pass above already marched the whole cell on that axis with
    // the correct world phase -- drawing it again would lay a second lattice
    // at a different offset, visible as doubled chevrons.
    if (in_dir >= 0 && in_dir != (out_dir + 2) % 4) {
        // Material travels from that edge toward the centre, i.e. along
        // -dir_vec(in_dir).
        vec2 axis = -dir_vec(in_dir);
        float wphase = dot(world, axis);
        chev = max(chev, chevrons_along(p, axis, -0.5, 0.0,
                                        wphase - dot(p, axis), px));
    }

    o.chevron = chev * o.body;   // never draw chevrons off the spine
    return o;
}

// ======================================================================
// Structures
// ======================================================================
//
// Every machine draws the same way: a body merged across its footprint, one
// glyph centred on that footprint, and short arrows at its ports -- blue
// inward for inputs, red outward for outputs. Which colour and glyph comes
// from the table above; which ports from input_face_mask()/output_face_mask()
// in common.glsl, matching Entity.face_masks_at() in core/entity.py.

float sd_plus(vec2 p, float r, float t) {
    return min(sd_box(p, vec2(r, t)), sd_box(p, vec2(t, r)));
}

float sd_cross(vec2 p, float r, float t) {
    // The X is a Plus rotated 45 degrees.
    vec2 q = vec2(p.x + p.y, p.x - p.y) * 0.70710678;
    return sd_plus(q, r, t);
}

// A "Z": top bar, diagonal, bottom bar. Drawn from segments so the stroke
// weight matches the other glyphs.
float sd_zed(vec2 p, float r, float t) {
    vec2 tl = vec2(-r,  r), tr = vec2( r,  r);
    vec2 bl = vec2(-r, -r), br = vec2( r, -r);
    float d = sd_segment(p, tl, tr);
    d = min(d, sd_segment(p, tr, bl));   // the diagonal
    d = min(d, sd_segment(p, bl, br));
    return d - t;
}

float sd_dot(vec2 p, float r, float t) {
    return length(p) - max(t * 2.0, r * 0.35);
}

// An obelus: a bar with a dot above and below. Stands in for "1/x" --
// lettering is a separate project, and this reads at a 1x2's size.
float sd_divide(vec2 p, float r, float t) {
    float d = sd_box(p, vec2(r, t));                    // the bar
    float gap = r * 0.55;
    d = min(d, length(p - vec2(0.0,  gap)) - t * 1.5);  // upper dot
    d = min(d, length(p - vec2(0.0, -gap)) - t * 1.5);  // lower dot
    return d;
}

// One ">" chevron, apex at +X, centred on the origin.
float sd_chevron(vec2 p, float r, float t) {
    vec2 a = vec2(-r,  r);
    vec2 b = vec2( r,  0.0);
    vec2 c = vec2(-r, -r);
    return min(sd_segment(p, a, b), sd_segment(p, b, c)) - t;
}

// Stacked ">>", for Max: "the larger one comes through". Two chevrons side
// by side along the long axis of a 2x1.
float sd_chevrons(vec2 p, float r, float t) {
    float s = r * 0.55;
    float d =      sd_chevron(p - vec2(-s * 0.9, 0.0), s, t);
    return min(d, sd_chevron(p - vec2( s * 0.9, 0.0), s, t));
}

// A downward arrow, for Floor: a stem with a two-stroke head at the bottom.
float sd_arrow_down(vec2 p, float r, float t) {
    float d = sd_segment(p, vec2(0.0, r), vec2(0.0, -r));
    d = min(d, sd_segment(p, vec2(0.0, -r), vec2( r * 0.6, -r * 0.4)));
    d = min(d, sd_segment(p, vec2(0.0, -r), vec2(-r * 0.6, -r * 0.4)));
    return d - t;
}

// A "Y": two arms converging from the top into a single stem going down.
// Merge's mark -- two inputs, one output. `flip` mirrors it vertically for
// Route, which is the same topology the other way up.
float sd_wye(vec2 p, float r, float t, bool flip) {
    if (flip) p.y = -p.y;
    vec2 mid = vec2(0.0, 0.0);
    float d = sd_segment(p, vec2(-r, r), mid);      // upper-left arm
    d = min(d, sd_segment(p, vec2( r, r), mid));    // upper-right arm
    d = min(d, sd_segment(p, mid, vec2(0.0, -r)));  // the stem
    return d - t;
}

// Dispatch on the glyph id from type_glyph(). `r` is the arm reach and `t`
// the stroke half-thickness, both already scaled to the footprint.
float sd_glyph(int glyph, vec2 p, float r, float t) {
    if (glyph == GLYPH_PLUS)  return sd_plus(p, r, t);
    if (glyph == GLYPH_CROSS) return sd_cross(p, r, t);
    if (glyph == GLYPH_ZED)   return sd_zed(p, r, t);
    if (glyph == GLYPH_DOT)   return sd_dot(p, r, t);
    if (glyph == GLYPH_DIVIDE) return sd_divide(p, r, t);
    if (glyph == GLYPH_CHEVRONS) return sd_chevrons(p, r, t);
    if (glyph == GLYPH_ARROW_DOWN) return sd_arrow_down(p, r, t);
    if (glyph == GLYPH_WYE) return sd_wye(p, r, t, false);
    if (glyph == GLYPH_WYE_DOWN) return sd_wye(p, r, t, true);
    return 1e9;
}

// ======================================================================
// Underground belts
// ======================================================================
//
// An end is a funnel whose **narrow end faces the buried span**: wide on the
// surface side where it meets ordinary belt, tapering to a point where it
// goes under. A pair therefore reads as two arrowheads pointing at each
// other across the gap, which is what makes the two ends of one run
// recognisable as a unit.
//
//     entrance, facing +X:   wide at -X (surface), narrow at +X (buried)
//     exit,     facing +X:   narrow at -X (buried), wide at +X (surface)
//
// The span is downstream of an entrance but upstream of an exit, so the two
// taper opposite ways: the exit is the mirrored one. A trapezoid in
// cell-local space, rotated into the facing, tapering from the wide end
// (-UNDER_LEN) to the point (+UNDER_LEN).
float sd_funnel(vec2 p, int dir, bool mirror) {
    vec2 q = rotate_to_dir(p, dir);
    if (mirror) q.x = -q.x;
    // Half-width shrinks linearly from mouth to throat.
    float t = clamp((q.x + UNDER_LEN) / (2.0 * UNDER_LEN), 0.0, 1.0);
    float half_w = mix(UNDER_MOUTH, UNDER_THROAT, t);
    float along = abs(q.x) - UNDER_LEN;
    float across = abs(q.y) - half_w;
    return max(along, across);
}

// Dots marching along the buried span, phase-locked to world position so a
// run reads as one dotted line rather than restarting in each cell.
float span_dots(vec2 p, vec2 axis, vec2 world, float px) {
    float t = dot(world, axis);
    float s = dot(p, vec2(-axis.y, axis.x));
    float k = floor(t / UNDER_DOT_PERIOD + 0.5);
    float best = 1e9;
    for (int i = -1; i <= 1; ++i) {
        float centre = (k + float(i)) * UNDER_DOT_PERIOD;
        vec2 local = vec2(t - centre, s);
        best = min(best, length(local) - UNDER_DOT_R);
    }
    return fill(best, px);
}

#ifdef SEGMENT_DEBUG
// ======================================================================
// Segment debug overlay
// ======================================================================
//
// Belt segments are *derived* state -- core/segments.py rebuilds them from
// the grid -- and the failure mode of derived state is being quietly wrong
// while looking internally consistent. This makes the derivation visible:
// every cell of a run gets the same hashed colour, so a run that is cut
// where it should not be, or fused with its neighbour, shows up immediately.
//
// Pairs with the inspection panel's audit(), which checks the same thing
// numerically. The overlay localises a problem; the audit names it.

// A segment id -> a distinct, reasonably bright colour. Any cheap integer
// hash does; this is the usual xorshift-and-multiply with the result spread
// across the three channels.
vec3 segment_colour(int sid) {
    uint h = uint(sid) * 2654435761u;
    h ^= h >> 15;
    h *= 2246822519u;
    h ^= h >> 13;
    // Keep saturation and value high so adjacent segments stay separable
    // against the dark background, varying only the hue.
    float hue = float(h & 1023u) / 1024.0;
    vec3 k = fract(vec3(hue) + vec3(0.0, 0.6667, 0.3333));
    vec3 rgb = clamp(abs(k * 6.0 - 3.0) - 1.0, 0.0, 1.0);
    return mix(vec3(1.0), rgb, 0.85);
}
#endif

// ======================================================================
// Belt items
// ======================================================================
//
// A pixel knows, from its own cell's .w, which segment it is on and how far
// along (`offset`). Items are indexed by the monotonic counter `k`, so the
// job is to find the handful of k whose position lands within a disc radius
// of this cell -- without looping over every item on the run.
//
// The one property that always holds is **monotonicity**: position strictly
// decreases in k (item k is at least `spacing` ahead of k+1), whichever term
// of the min happens to be binding. So the first item at or behind this cell
// can be found by **bisection** over k, then a short forward scan covers the
// two or three whose discs could overlap.
//
// Bisection rather than inverting a formula, because neither term inverts
// usefully:
//
//   * The **packed** term inverts algebraically, but it is an upper bound
//     derived from the anchor -- and on a free-flowing belt the anchor sits
//     far ahead of every actual item, so the result lands past `tail` and
//     finds nothing. Clamping it into range then skips the *oldest* items
//     instead. Both were tried; both drew discs in some situations and not
//     others.
//   * The **unblocked** term depends on each item's own enter_tick and does
//     not invert at all.
//
// Bisection needs only the ordering, so it is correct in every case rather
// than in some, and costs log2(cap) steps -- a handful, bounded by
// ITEM_BISECT.
//
// `travel` is the direction material moves through this cell, so the disc
// can be placed along the right axis: an item's position is a distance along
// the run, and the cell has to convert that into a local offset.

// Coverage of one item's disc at cell-local position `p`, given the item's
// distance along the run relative to this cell's centre.
//
// Returns (body, ring) coverage. `along` is in cells, signed, measured
// along `axis` from the cell centre.
vec2 item_disc(vec2 p, vec2 axis, float along, float px) {
    vec2 at = axis * along;
    float d = length(p - at);
    float body = fill(d - ITEM_RADIUS, px);
    // The ring is the annulus just inside the rim.
    float ring = fill(d - ITEM_RADIUS, px)
               * (1.0 - fill(d - (ITEM_RADIUS - ITEM_RING), px));
    return vec2(body, ring);
}

// How opaque the digits should be at this zoom. 1 when a glyph is comfortably
// readable, falling to 0 once it is a few pixels tall -- below that the digits
// are noise over the disc, and worse, they dilute the ring colour that
// carries the sign at every zoom.
float text_opacity(float px) {
    float glyph_px = (float(FONT_H) * ITEM_NUM_SIZE) / max(px, 1e-6);
    return smoothstep(NUM_MIN_PX, NUM_GOOD_PX, glyph_px);
}

// --- the number on an item ---------------------------------------------
//
// The item's value, in the bitfield font, centred on the disc. Formatting is
// shared with every other number on the board -- see format_number() in
// common.glsl for the policy and why it happens shader-side.
//
// The sign *is* drawn now. The seven-segment display could not afford the
// column (three columns were three significant figures, and a minus would
// not fit inside the rim at any legible size), but trimming trailing zeros
// pays for it: "-1" is two columns where "1.00" was three. The ring still
// carries the sign as well, and still carries it below the zoom where a
// glyph can be resolved at all -- so nothing is lost at distance and the
// exact value is readable up close.
//
// `p` is in cells, measured from the disc centre. Returns (text, halo)
// coverage: the caller lays the halo down first so a digit spilling past the
// rim stays legible over whatever is behind it.
vec2 item_number(float value, vec2 p, float px) {
    // Into glyph-cell units, where the font and the formatter work.
    vec2 g = p / ITEM_NUM_SIZE;
    float gpx = px / ITEM_NUM_SIZE;
    float d = sd_number(value, ITEM_NUM_SIG, ITEM_NUM_TRIM, g);
    return vec2(fill(d, gpx), fill(d - ITEM_NUM_HALO, gpx));
}

// Draw whatever items overlap this cell. Returns colour-with-coverage:
// .rgb premultiplied tint, .a total coverage.
vec4 draw_items(ivec4 c, vec2 p, int travel, float px) {
    vec4 out_col = vec4(0.0);
    // Which item owns this pixel so far. Ranked rather than compared on
    // coverage directly, so a glyph can outrank a neighbour's disc body --
    // see the comment at the bottom of the scan loop.
    float out_rank = 0.0;

    int sid = segment_id(c.w);
    if (sid == 0) return out_col;

    ivec4 si = seg_i[sid];
    int head = si.x, tail = si.y;
    if (head == tail) return out_col;          // nothing on this run

    vec4 sf = seg_f[sid];
    float spacing = max(sf.w, 1.0);
    // This cell's centre as a distance along the run, in fixed point.
    float here = float(segment_offset(c.w)) * BELT_UNIT;
    vec2 axis = dir_vec(travel);
    // How far along the run an item can be and still touch this cell. The
    // number is allowed to spill past the disc rim, so the reach is set by
    // whichever is wider -- the disc, or half the widest number the
    // formatter can produce (NUM_MAX_CODES columns).
    float text_half = float(NUM_MAX_CODES) * float(FONT_W) * ITEM_NUM_SIZE * 0.5;
    float reach = (0.5 + max(ITEM_RADIUS, text_half)) * BELT_UNIT;

    // Bisect for the smallest k whose position is within reach behind us.
    // Everything at smaller k is further forward and cannot touch this cell.
    int lo = head, hi = tail - 1;
    for (int i = 0; i < ITEM_BISECT; ++i) {
        if (lo >= hi) break;
        int mid = lo + (hi - lo) / 2;
        if (item_position(sid, mid, sim_tick, sim_alpha) > here + reach) {
            lo = mid + 1;
        } else {
            hi = mid;
        }
    }

    // Scan forward from there. Positions are ordered, so once an item is
    // behind us by more than `reach` every later one is too and the loop can
    // stop.
    for (int i = 0; i < ITEM_SCAN; ++i) {
        int k = lo + i;
        if (k >= tail) break;

        float pos = item_position(sid, k, sim_tick, sim_alpha);
        float delta = pos - here;
        if (delta < -reach) break;      // behind us, and so is everything after
        if (delta > reach) continue;    // still ahead of this cell

        vec2 cov = item_disc(p, axis, delta / BELT_UNIT, px);
        // No early-out on an uncovered pixel: the number is allowed to spill
        // past the rim, so a pixel the disc misses may still be inside a
        // digit. The union is taken below.

        float value = item_payload(sid, k);
        vec3 edge = (value > 0.0) ? COL_ITEM_POS : COL_ITEM_NEG;
        // Fill first, then the ring over it.
        vec3 col = mix(COL_ITEM_FILL, edge, cov.y);
        // Then the number over both. Skipped entirely when zoomed out past
        // legibility -- the ring still carries the sign there.
        float text = text_opacity(px);
        // The number can spill past the rim, so its coverage is tracked
        // separately and unioned into the disc's: a pixel outside the disc
        // that a digit reaches must still be drawn, or the overhang would be
        // clipped to the circle it is deliberately allowed to leave.
        float ink = 0.0;
        if (text > 0.0) {
            // Centred on the disc. item_number() takes cell units and does
            // the conversion to glyph space itself, including the antialias
            // width -- scaling a glyph without scaling its edge softness is
            // what double-antialiased the structure glyph at its interior
            // boundary.
            vec2 np = p - axis * (delta / BELT_UNIT);
            vec2 num = item_number(value, np, px);
            float halo = num.y * text * ITEM_HALO_ALPHA;
            col = mix(col, COL_ITEM_HALO, halo);
            col = mix(col, COL_ITEM_NUM, num.x * text);
            ink = max(num.x, num.y * ITEM_HALO_ALPHA) * text;
        }
        float cover = max(cov.x, ink);
        if (cover <= 0.0) continue;
        // Whichever disc covers this pixel most wins, so overlapping discs
        // (possible when spacing < 2 * radius) do not flicker by index.
        //
        // Except that a *glyph* outranks a neighbour's disc body. At tight
        // spacings the discs very nearly touch, so the next item's rim can
        // reach over the tail of this item's number -- and coverage alone
        // calls that a tie the opaque disc wins. Text is what the player is
        // trying to read, and a disc has nothing to say at the few pixels
        // where they disagree.
        float rank = max(cov.x, ink > 0.0 ? 1.0 + ink : 0.0);
        if (rank > out_rank) { out_rank = rank; out_col = vec4(col, cover); }
    }
    return out_col;
}

// ======================================================================
// Waiting zones
// ======================================================================
//
// A gate's output waiting zone is normally empty by the end of a tick: the
// belt phase drains it. It stays occupied only when the belt refused -- the
// run is backed up all the way to the machine -- and that is precisely the
// state the gate reads to know it is blocked. Drawing it makes the
// back-pressure visible instead of leaving the player to wonder why a
// machine stopped.
//
// Squished, because it is physically nowhere: not on the belt, not inside
// the body. Compressing it along the travel axis is what distinguishes it
// from an item that has simply stopped.
vec4 draw_waiting(ivec4 c, ivec2 ci, vec2 p, float px) {
    vec4 out_col = vec4(0.0);
    int type = cell_type(c.x);
    if (!is_structure(type)) return out_col;

    int row = cell_id(c.x);
    if (row == 0) return out_col;
    int facing = cell_dir(c.x);
    bool flipped = is_flipped(c.z);
    ivec2 lcl = cell_local(c.y);
    int out_mask = output_face_mask(type, lcl, facing, flipped);

    // Walk this cell's output faces. The channel index is the port's
    // position in the authored OUTPUTS list, so it has to be recovered in
    // authored space -- the same inversion the port masks do.
    ivec2 authored = unplace_local(type, lcl, facing, flipped);
    for (int d = 0; d < 4; ++d) {
        if (!has_face(out_mask, d)) continue;
        int channel = output_channel_at(type, authored,
                                        unrotate_face(d, facing, flipped));
        if (channel < 0) continue;
        if (!gate_out_held(row, channel)) continue;

        // Sit in the gap between the body edge and the cell boundary.
        vec2 axis = dir_vec(d);
        vec2 at = axis * WAIT_OFFSET;
        // Compress along travel, full width across it.
        vec2 local = rotate_to_dir(p - at, d);
        local.x /= WAIT_SQUISH;
        float dist = length(local);
        float body = fill(dist - ITEM_RADIUS, px);
        float ring = body * (1.0 - fill(dist - (ITEM_RADIUS - ITEM_RING), px));

        float value = gate_out_value(row, channel);
        vec3 edge = (value > 0.0) ? COL_ITEM_POS : COL_ITEM_NEG;
        vec3 col = mix(COL_ITEM_FILL, edge, ring);
        // The number, in the *unsquished* frame: compressing the glyphs along
        // with the disc would make them unreadable, and the squish is there
        // to say "this is nowhere", not to deform the cargo.
        float text = text_opacity(px);
        float ink = 0.0;
        if (text > 0.0) {
            vec2 num = item_number(value, p - at, px);
            col = mix(col, COL_ITEM_HALO, num.y * text * ITEM_HALO_ALPHA);
            col = mix(col, COL_ITEM_NUM, num.x * text);
            ink = max(num.x, num.y * ITEM_HALO_ALPHA) * text;
        }
        // As on a belt item, the number may reach past the rim, so coverage
        // is the union rather than the disc alone.
        float cover = max(body, ink);
        if (cover <= 0.0) continue;
        if (cover > out_col.a) out_col = vec4(col, cover);
    }
    return out_col;
}

// A port arrow: a chevron at `offset` from the cell centre, pointing along
// `face` (outward) or against it (inward).
float port_arrow(vec2 p, int face, bool inward, float px) {
    vec2 f = dir_vec(face);
    vec2 at = f * PORT_OFFSET;
    // Inward arrows point back toward the cell centre.
    int point_dir = inward ? (face + 2) % 4 : face;
    vec2 local = rotate_to_dir(p - at, point_dir);
    return fill(sd_chevron(local, PORT_ARROW_LEN, PORT_ARROW_SPAN,
                           PORT_ARROW_THICK), px);
}

void main() {
    // Pixel -> world. y is flipped so world +Y points up the screen.
    vec2 screen = gl_FragCoord.xy - resolution * 0.5;
    vec2 world  = cam_center + vec2(screen.x, screen.y) / cam_zoom;

    // One world unit is one cell; px is a pixel's width in world units, used
    // to keep line weights and antialiasing constant under zoom.
    float px = 1.0 / cam_zoom;

    ivec2 ci = ivec2(floor(world));
    vec2  cf = world - vec2(ci);          // 0..1 within the cell
    vec2  p  = cf - 0.5;                  // -0.5..0.5, centred

    bool inside = in_board(ci);
    vec3 col = inside ? COL_BG : COL_VOID;

    // --- grid lines, drawn FIRST ---------------------------------------
    // Under the entities, not over them, so belts visibly join across cell
    // boundaries and a multi-cell structure is not sliced up by the grid.
    {
        vec2 to_edge = min(cf, 1.0 - cf) / px;
        float line = 1.0 - smoothstep(0.0, 1.0, min(to_edge.x, to_edge.y));
        float grid_fade = smoothstep(GRID_FADE_LO, GRID_FADE_HI, cam_zoom);
        bool major = (ci.x % MAJOR_EVERY == 0) || (ci.y % MAJOR_EVERY == 0);
        col = mix(col, major ? COL_GRID_MAJOR : COL_GRID,
                  line * grid_fade * GRID_ALPHA);
    }

    if (inside) {
        ivec4 c = texelFetch(grid, ci, 0);
        int type = cell_type(c.x);
        int gstate = ghost_state(c.z);
        float alpha_scale = (gstate == GHOST_PENDING) ? GHOST_ALPHA : 1.0;

        // --- buried span -----------------------------------------------
        // Drawn by the cells *between* a paired set of ends, before whatever
        // occupies them, so the dotted line reads as passing underneath.
        // Each cell scans backwards along both axes for an entrance whose
        // partner exit lies ahead of it; nothing is stored per-cell, so the
        // span costs nothing until it is looked for.
        {
            float dots = 0.0;
            for (int d = 0; d < 4; ++d) {
                vec2 axis = dir_vec(d);
                // Walk back to find an entrance pointing this way.
                for (int k = 1; k <= UNDER_MAX_SPAN; ++k) {
                    ivec2 ec = ci - DIR_VECS[d] * k;
                    ivec4 ev = fetch(ec);
                    if (cell_type(ev.x) != TYPE_UNDER) continue;
                    if (cell_dir(ev.x) != d) continue;
                    if (is_under_exit(ev.z)) continue;
                    // Its exit must lie ahead of us, within reach.
                    for (int m = 1; k + m <= UNDER_MAX_SPAN; ++m) {
                        ivec2 xc = ec + DIR_VECS[d] * (k + m);
                        ivec4 xv = fetch(xc);
                        if (cell_type(xv.x) != TYPE_UNDER) continue;
                        if (cell_dir(xv.x) != d) continue;
                        if (!is_under_exit(xv.z)) continue;
                        // Either end claiming the pairing is enough. A
                        // *ghost* exit points at a real entrance that does
                        // not point back -- the link is one-way until the
                        // placement commits -- and that preview is exactly
                        // what should show the span while you hover.
                        if (!is_under_paired(ev.z) && !is_under_paired(xv.z))
                            continue;
                        dots = max(dots, span_dots(p, axis, world, px));
                        break;
                    }
                    break;
                }
            }
            col = mix(col, COL_UNDER_DOT, dots * UNDER_DOT_ALPHA);
        }

        if (type == TYPE_BELT) {
            BeltDraw b = draw_belt(p, ci, c, px, world);
            col = mix(col, COL_BELT, b.body * alpha_scale);
            col = mix(col, COL_CHEVRON, b.chevron * alpha_scale);
            // A seeded cell, tinted by the sign of what it seeds. Over the
            // spine so it reads as marking this belt, and clipped to a patch
            // rather than the whole cell so the run's shape stays legible
            // through it.
            if (is_seeded(c.z)) {
                vec3 sc = is_seed_positive(c.z) ? COL_SEED_POS : COL_SEED_NEG;
                float a = fill(sd_box(p, vec2(SEED_INSET)), px);
                col = mix(col, sc, a * SEED_ALPHA * alpha_scale);
            }
        } else if (type == TYPE_UNDER) {
            // A funnel, mouth on the surface side. An entrance's mouth faces
            // back the way material came; an exit's faces the way it goes.
            int facing = cell_dir(c.x);
            bool exit_end = is_under_exit(c.z);
            // The narrow end always faces the buried span: downstream of an
            // entrance, upstream of an exit. So the exit is the mirrored
            // one, and a pair points its two tips at each other.
            float a = fill(sd_funnel(p, facing, exit_end), px);
            col = mix(col, COL_UNDER, a * alpha_scale);
            // A chevron on the mouth half, so travel direction is readable.
            vec2 fv = dir_vec(facing);
            vec2 at = fv * (exit_end ? UNDER_LEN * 0.45 : -UNDER_LEN * 0.45);
            float chev = fill(sd_chevron(rotate_to_dir(p - at, facing),
                                         CHEVRON_LEN, CHEVRON_SPAN,
                                         CHEVRON_THICK), px);
            col = mix(col, COL_CHEVRON, chev * a * alpha_scale);
            // An underground *end* is an ordinary position on its run, so it
            // carries items like any belt cell. The buried span between a
            // pair holds items too, but has no cell to draw them in -- they
            // deliberately pop out of existence and back, per the design.

        } else if (type != TYPE_EMPTY) {
            int facing = cell_dir(c.x);
            int id = cell_id(c.x);
            ivec2 lcl = cell_local(c.y);

            // Recover the footprint extent by probing neighbours for the same
            // entity id, so a multi-cell entity draws as one merged shape.
            // PROBE_REACH must be at least (longest footprint axis - 1).
            // Raise it if a machine longer than PROBE_REACH+1 is added.
            ivec2 lo = ivec2(0), hi = ivec2(0);
            for (int k = 1; k <= PROBE_REACH; ++k) {
                if (cell_id(fetch(ci + ivec2(k, 0)).x) != id) break; hi.x = k;
            }
            for (int k = 1; k <= PROBE_REACH; ++k) {
                if (cell_id(fetch(ci - ivec2(k, 0)).x) != id) break; lo.x = k;
            }
            for (int k = 1; k <= PROBE_REACH; ++k) {
                if (cell_id(fetch(ci + ivec2(0, k)).x) != id) break; hi.y = k;
            }
            for (int k = 1; k <= PROBE_REACH; ++k) {
                if (cell_id(fetch(ci - ivec2(0, k)).x) != id) break; lo.y = k;
            }

            // Body: merge across interior edges. Going slightly *past* the
            // boundary matters -- stopping exactly at it leaves both cells
            // antialiasing the shared edge, which shows as a seam.
            float over = 0.5 + px;
            vec4 edge = vec4(
                (lo.x > 0) ? over : STRUCT_INSET,
                (hi.x > 0) ? over : STRUCT_INSET,
                (lo.y > 0) ? over : STRUCT_INSET,
                (hi.y > 0) ? over : STRUCT_INSET);
            float sd_body = max(max(-p.x - edge.x, p.x - edge.y),
                                max(-p.y - edge.z, p.y - edge.w));
            float a_body = fill(sd_body, px);

            // --- connection stubs ---
            // A short belt-coloured bar from the body edge out to the cell
            // boundary, on any port face that actually has something on the
            // other side. Drawn *under* the body so the body edge caps the
            // inner end cleanly.
            //
            // An input face stubs when the neighbour emits at it. An output
            // face stubs when the neighbour would take what we emit -- a belt
            // (which has already drawn an incoming arm back at us, since
            // feeds_into() counts our output faces as a source) or another
            // structure whose own input face points at us. Both halves of a
            // structure-to-structure join must agree, which is what
            // accepts_from() is for; checking only for belts here left the
            // output side silent while the input side stubbed.
            bool flipped = is_flipped(c.z);
            int in_mask  = input_face_mask(type, lcl, facing, flipped);
            int out_mask = output_face_mask(type, lcl, facing, flipped);
            float stub = 0.0;
            for (int d = 0; d < 4; ++d) {
                ivec2 nc = ci + DIR_VECS[d];
                int toward = (d + 2) % 4;
                bool want = false;
                if (has_face(in_mask, d)) {
                    // Something feeding us from that side.
                    want = feeds_into(nc, toward, c.z);
                } else if (has_face(out_mask, d)) {
                    ivec4 n = fetch(nc);
                    // Any adjacent belt connects: either it is draining this
                    // output, or it is pointing into us on a face we emit from
                    // -- a topology conflict the belt resolves in favour of
                    // its own facing. Either way the cells are joined.
                    bool belt_there =
                        cell_type(n.x) == TYPE_BELT && same_layer(c.z, n.z);
                    // A structure takes our output only on a declared input
                    // face pointing back at us.
                    want = belt_there || accepts_from(nc, toward, c.z);
                }
                if (!want) continue;
                float sd_stub = sd_segment(p, vec2(0.0),
                                           dir_vec(d) * STUB_REACH)
                                - STUB_WIDTH * 0.5;
                stub = max(stub, fill(sd_stub, px));
            }
            col = mix(col, COL_BELT, stub * alpha_scale);

            col = mix(col, type_colour(type), a_body * alpha_scale);

            // --- control-signal overlay ---
            // A faint belt straight through the cell on each axis the machine
            // passes unchanged, so you can see at a glance which signals
            // survive the operation. Over the body, under the glyph.
            //
            // The axes are declared (Entity.PASS_THROUGH), not inferred: a
            // mutated channel enters and leaves on opposite faces exactly as
            // a pass-through does, so geometry alone cannot tell them apart.
            //
            // Only cells that actually carry the channel draw it -- Shear
            // passes x through its first cell only, so its second cell shows
            // nothing. A cell carries it if it has a port on that axis, or if
            // it sits mid-channel between two cells that do (Swap's x enters
            // the top cell and leaves the bottom, so neither end sees both
            // halves, but every cell of that column is on the channel).
            {
                // Flip then rotate, exactly as the port masks do -- a
                // mirrored machine's control axis mirrors with it, so
                // rotating alone leaves the overlay pointing the wrong way
                // and disagreeing with the ports it is meant to describe.
                int pm = pass_through_mask(type);
                if (flipped) pm = flip_face_mask(pm);
                int pass_mask = rotate_face_mask(pm, facing);
                float pass = 0.0, pass_chev = 0.0;
                for (int d = 0; d < 4; ++d) {
                    if (!has_face(pass_mask, d)) continue;
                    int back = (d + 2) % 4;
                    // This cell is on the channel if it terminates either end
                    // of it here, or if the footprint continues both ways
                    // (making it an interior link in the run).
                    ivec2 step = DIR_VECS[d];
                    bool more_fwd  = (step.x != 0)
                                   ? ((step.x > 0) ? hi.x > 0 : lo.x > 0)
                                   : ((step.y > 0) ? hi.y > 0 : lo.y > 0);
                    bool more_back = (step.x != 0)
                                   ? ((step.x > 0) ? lo.x > 0 : hi.x > 0)
                                   : ((step.y > 0) ? lo.y > 0 : hi.y > 0);
                    bool on_channel = has_face(out_mask, d)
                                   || has_face(in_mask, back)
                                   || (more_fwd && more_back);
                    if (!on_channel) continue;

                    vec2 axis = dir_vec(d);
                    float sd_pass = sd_segment(p, -axis * 0.5, axis * 0.5)
                                    - PASS_WIDTH * 0.5;
                    pass = max(pass, fill(sd_pass, px));
                    // Chevrons share the belt lattice, so a control channel
                    // lines up with the belts feeding it.
                    float wphase = dot(world, axis);
                    pass_chev = max(pass_chev,
                        chevrons_along(p, axis, -0.5, 0.5,
                                       wphase - dot(p, axis), px));
                }
                col = mix(col, COL_BELT, pass * PASS_ALPHA * alpha_scale);
                col = mix(col, COL_CHEVRON,
                          pass_chev * pass * PASS_ALPHA * PASS_CHEVRON
                          * alpha_scale);
            }

            // Footprint-space coords, so the glyph is centred on the whole
            // entity rather than on one of its cells. `pf` is in *cell* units
            // measured from the footprint centre, and is continuous across
            // the interior edges -- which is what keeps the glyph from being
            // antialiased twice at a cell boundary and showing a seam.
            vec2 extent = vec2(lo + hi) + 1.0;
            vec2 centre = (extent - 1.0) * 0.5;
            vec2 pf = p + (vec2(lcl) - centre);
            // Scale the glyph to the footprint's shorter axis so it stays
            // inside a 2x1 rather than spilling past the short edges.
            float s = min(extent.x, extent.y);

            // A Source shows its emitted value instead of a glyph. The dot
            // it used to draw said only "this is a generator", which the
            // colour and the port arrows already say; the number is the one
            // thing about a Source that differs between two of them, and it
            // was reachable only by clicking.
            //
            // Deliberately *not* rotated with the facing. Text has a reading
            // orientation that a glyph does not, so a south-facing Source
            // must still read left-to-right -- `pf` is already axis-aligned
            // to the world, so simply not rotating it is correct.
            bool show_emit = (type == TYPE_SOURCE) && (id != 0);
            if (show_emit) {
                float emit_px = (float(FONT_H) * EMIT_NUM_SIZE)
                              / max(px, 1e-6);
                float vis = smoothstep(EMIT_MIN_PX, EMIT_GOOD_PX, emit_px);
                if (vis > 0.0) {
                    float value = ent_field(id, F_EMIT);
                    float d = sd_number(value, EMIT_NUM_SIG, EMIT_NUM_TRIM,
                                        pf / EMIT_NUM_SIZE);
                    float num = fill(d, px / EMIT_NUM_SIZE);
                    col = mix(col, COL_EMIT_NUM,
                              num * vis * a_body * alpha_scale);
                } else {
                    // Too small to read: fall back to the type glyph, so a
                    // zoomed-out Source still reads as a generator rather
                    // than as a blank box.
                    float mark = fill(sd_glyph(type_glyph(type), pf,
                                               MARK_RADIUS * s,
                                               MARK_THICK * s), px);
                    col = mix(col, COL_STRUCT_MARK,
                              mark * a_body * alpha_scale);
                }
            } else {
                float mark = fill(sd_glyph(type_glyph(type), pf,
                                           MARK_RADIUS * s, MARK_THICK * s),
                                  px);
                col = mix(col, COL_STRUCT_MARK, mark * a_body * alpha_scale);
            }

            // --- ports ---
            // Already rotated into `facing` by input_face_mask() /
            // output_face_mask(), and per-cell: this cell draws exactly the
            // ports it owns, so a port on one cell of a footprint does not
            // appear on its neighbours.
            float a_in = 0.0, a_out = 0.0;
            for (int d = 0; d < 4; ++d) {
                if (has_face(in_mask, d))
                    a_in = max(a_in, port_arrow(p, d, true, px));
                if (has_face(out_mask, d))
                    a_out = max(a_out, port_arrow(p, d, false, px));
            }
            col = mix(col, COL_IN,  a_in  * alpha_scale);//can extend outside a_body
            col = mix(col, COL_OUT, a_out * alpha_scale);//can extend outside a_body
        }

#ifdef SEGMENT_DEBUG
        // Tint belt cells by segment, over the entity so the run's shape
        // reads through whatever is drawn beneath.
        {
            int sid = segment_id(c.w);
            if (sid != 0) {
                // Not named `patch` -- that is a GLSL reserved word.
                float tint = fill(sd_box(p, vec2(SEG_DEBUG_INSET)), px);
                col = mix(col, segment_colour(sid), tint * SEG_DEBUG_ALPHA);
                // Offset 0 is the tail: where items enter and where the
                // closed form measures from. Marking it makes the run's
                // direction of indexing visible, which is what tells you a
                // segment was traced the wrong way round.
                if (segment_offset(c.w) == 0) {
                    float dot_a = fill(length(p) - SEG_DEBUG_TAIL_R, px);
                    col = mix(col, vec3(1.0), dot_a * 0.9);
                }
            }
        }
#endif

        // Frustration is an overlay, never a replacement: whatever occupies
        // the cell is drawn above, and this tints on top so you can see
        // exactly what is in the way.
        if (gstate == GHOST_FRUSTRATED) {
            float sd = sd_box(p, vec2(FRUSTRATED_INSET));
            col = mix(col, COL_FRUSTRATED, fill(sd, px) * FRUSTRATED_ALPHA);
        }

        // --- belt items, last ------------------------------------------
        // Items are cargo *on* a cell, not part of it, so they draw over
        // everything the cell itself put down -- including the frustration
        // tint. That ordering matters: the tint is a 55%-alpha red wash over
        // the whole cell, and drawing items under it left a green item
        // reading as red-brown, i.e. reporting the wrong sign, for as long
        // as the player held a blocked structure over the belt.
        //
        // A ghost draws none: it has no simulation state, and its .w holds
        // whatever the cell carried before the preview covered it. A
        // *frustrated* cell is a real belt with real items -- the overlay
        // leaves .x and .w untouched -- so it keeps drawing them. Same
        // distinction segment derivation makes: only GHOST_PENDING is
        // fictional.
        if (gstate != GHOST_PENDING && is_beltlike(type)) {
            vec4 it = draw_items(c, p, cell_dir(c.x), px);
            col = mix(col, it.rgb, it.a);
        }

        // A gate's undrained waiting zone, for the same reason and in the
        // same layer: it is cargo sitting on the boundary, not part of the
        // machine. Only real cells -- a ghost has no gate state.
        if (gstate != GHOST_PENDING) {
            vec4 w = draw_waiting(c, ci, p, px);
            col = mix(col, w.rgb, w.a);
        }
    }

    // Board border, over everything so the playfield edge stays readable.
    vec2 b0 = abs(world) / px;
    vec2 b1 = abs(world - vec2(board_size)) / px;
    float border = 1.0 - smoothstep(0.0, 1.5,
        min(min(b0.x, b0.y), min(b1.x, b1.y)));
    bool near_board = all(greaterThanEqual(world, vec2(-2.0)))
                   && all(lessThanEqual(world, vec2(board_size) + 2.0));
    if (near_board) col = mix(col, COL_BORDER, border * 0.8);

    // Copy mode's selection rectangle, over everything including the border:
    // it is a transient overlay and must never be occluded by the thing it
    // is being dragged across. A faint wash inside shows the catchment, and
    // the outline is drawn at a constant *world* width so it thickens with
    // zoom like the rest of the scene rather than staying a hairline.
    if (select_on != 0) {
        vec2 d0 = world - select_lo;
        vec2 d1 = select_hi - world;
        bool inside = all(greaterThan(d0, vec2(0.0)))
                   && all(greaterThan(d1, vec2(0.0)));
        if (inside) col = mix(col, COL_SELECT, SELECT_FILL);
        // Distance to the nearest edge of the box, signed inward.
        float edge = min(min(d0.x, d0.y), min(d1.x, d1.y));
        float aa = max(px, 1e-6);
        float line = 1.0 - smoothstep(SELECT_EDGE - aa, SELECT_EDGE + aa,
                                      abs(edge));
        // Only draw the outline band near the box, so `edge` being large
        // and negative far away cannot light up the whole screen.
        bool near_box = all(greaterThan(d0, vec2(-SELECT_EDGE - aa)))
                     && all(greaterThan(d1, vec2(-SELECT_EDGE - aa)));
        if (near_box) col = mix(col, COL_SELECT, line);
    }

    fragColor = vec4(col, 1.0);
}
