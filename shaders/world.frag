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
#define COL_ADD         vec3(0.40, 0.56, 0.44)
#define COL_MUL         vec3(0.54, 0.44, 0.62)
#define COL_STRUCT_MARK vec3(0.88, 0.90, 0.94)   // the Plus / X glyph
#define COL_IN          vec3(0.35, 0.60, 0.95)   // input ports  (blue)
#define COL_OUT         vec3(0.95, 0.35, 0.30)   // output ports (red)
#define COL_FRUSTRATED  vec3(0.90, 0.25, 0.22)

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

// --- ghosts / overlays ---
#define GHOST_ALPHA       0.45
#define FRUSTRATED_ALPHA  0.55
#define FRUSTRATED_INSET  0.46

// --- grid ---
#define MAJOR_EVERY       8
#define GRID_FADE_LO      3.0    // zoom (px/cell) where grid lines vanish
#define GRID_FADE_HI      9.0    // ...and where they reach full strength
#define GRID_ALPHA        0.9

// ======================================================================

vec3 body_colour(int type) {
    if (type == TYPE_ADD) return COL_ADD;
    if (type == TYPE_MUL) return COL_MUL;
    return COL_BELT;
}

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
// makes a belt downstream of an Add or Mul draw an incoming arm, exactly as if
// a belt were there.
bool feeds_into(ivec2 c, int toward, int my_z) {
    ivec4 n = fetch(c);
    int t = cell_type(n.x);
    if (t == TYPE_EMPTY) return false;
    if (!same_layer(my_z, n.z)) return false;
    if (t == TYPE_BELT) return cell_dir(n.x) == toward;
    return has_face(output_face_mask(t, cell_dir(n.x)), toward);
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
    return has_face(input_face_mask(t, cell_dir(n.x)), toward);
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

    // Every incoming neighbour adds an arm from its edge to the centre. With
    // no incoming belts at all the spine still covers the whole cell, so an
    // isolated belt reads as a full tile rather than a half one.
    int in_count = 0;
    for (int d = 0; d < 4; ++d) {
        // Never treat the cell we output into as a source. A structure can
        // have an output face pointing back at a belt that feeds it (a Mul
        // emits +Y while the belt above runs -Y into it), and taking that as
        // an incoming arm would lay two opposed chevron streams on one arm.
        // The belt's own facing wins: it is leaving, not arriving.
        if (d == out_dir) continue;

        ivec2 nc = ci + DIR_VECS[d];
        // A neighbour in direction d feeds us if it faces the opposite way.
        int toward = (d + 2) % 4;
        if (!feeds_into(nc, toward, c.z)) continue;
        in_count++;
        sd = min(sd, sd_segment(p, vec2(0.0), dir_vec(d) * 0.5) - half_w);
    }
    bool any_in = in_count > 0;
    // A junction is anywhere more than one stream arrives, or where the one
    // arriving stream is not the one leaving (a corner is not a junction --
    // its single stream simply bends, and should stay continuous).
    bool is_merge = in_count > 1;
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

    for (int d = 0; d < 4; ++d) {
        // Same rule as the spine: the cell we output into is never a source.
        if (d == out_dir) continue;

        ivec2 nc = ci + DIR_VECS[d];
        int toward = (d + 2) % 4;
        if (!feeds_into(nc, toward, c.z)) continue;
        // Incoming from direction d: material travels from that edge toward
        // the centre, i.e. along -dir_vec(d). The arm spans t in 0..0.5
        // measured along the travel direction.
        // A stream arriving from directly behind us travels the same axis as
        // our outgoing stream. The outgoing pass below already marches the
        // whole cell on that axis with the correct world phase, so drawing
        // this one too would lay a second lattice over the first at a
        // different offset -- visible as doubled chevrons. Skip it.
        bool straight_through = (d == (out_dir + 2) % 4);
        if (straight_through) continue;

        vec2 axis = -dir_vec(d);
        float wphase = dot(world, axis);
        // Tributaries stop short of the centre at a true merge so converging
        // streams stay legible.
        float t_end = is_merge ? -MERGE_GAP : 0.0;
        chev = max(chev, chevrons_along(p, axis, -0.5, t_end,
                                        wphase - dot(p, axis), px));
    }

    o.chevron = chev * o.body;   // never draw chevrons off the spine
    return o;
}

// ======================================================================
// Structures
// ======================================================================
//
// Add: a Plus centred on the whole 2x1 footprint.
// Mul: an X in its single cell.
// Both carry short arrows at their ports -- blue inward for inputs, red
// outward for outputs. Ports are derived from type + facing, matching
// Entity.faces_for() in core/entity.py.

float sd_plus(vec2 p, float r, float t) {
    return min(sd_box(p, vec2(r, t)), sd_box(p, vec2(t, r)));
}

float sd_cross(vec2 p, float r, float t) {
    // The X is a Plus rotated 45 degrees.
    vec2 q = vec2(p.x + p.y, p.x - p.y) * 0.70710678;
    return sd_plus(q, r, t);
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

// Rotate a face authored at DIR_PX into the entity's facing.
int rot_face(int face, int facing) { return (face + facing) % 4; }

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

        if (type == TYPE_BELT) {
            BeltDraw b = draw_belt(p, ci, c, px, world);
            col = mix(col, COL_BELT, b.body * alpha_scale);
            col = mix(col, COL_CHEVRON, b.chevron * alpha_scale);

        } else if (type != TYPE_EMPTY) {
            int facing = cell_dir(c.x);
            int id = cell_id(c.x);
            ivec2 lcl = cell_local(c.y);

            // Recover the footprint extent by probing neighbours for the same
            // entity id, so a multi-cell entity draws as one merged shape.
            ivec2 lo = ivec2(0), hi = ivec2(0);
            for (int k = 1; k <= 3; ++k) {
                if (cell_id(fetch(ci + ivec2(k, 0)).x) != id) break; hi.x = k;
            }
            for (int k = 1; k <= 3; ++k) {
                if (cell_id(fetch(ci - ivec2(k, 0)).x) != id) break; lo.x = k;
            }
            for (int k = 1; k <= 3; ++k) {
                if (cell_id(fetch(ci + ivec2(0, k)).x) != id) break; hi.y = k;
            }
            for (int k = 1; k <= 3; ++k) {
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
            int in_mask  = input_face_mask(type, facing);
            int out_mask = output_face_mask(type, facing);
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

            col = mix(col, body_colour(type), a_body * alpha_scale);

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

            float mark = 0.0;
            if (type == TYPE_ADD) {
                mark = fill(sd_plus(pf, MARK_RADIUS * s, MARK_THICK * s), px);
            } else if (type == TYPE_MUL) {
                mark = fill(sd_cross(pf, MARK_RADIUS * s, MARK_THICK * s), px);
            }
            col = mix(col, COL_STRUCT_MARK, mark * a_body * alpha_scale);

            // --- ports ---
            // Authored at DIR_PX, rotated into `facing`, and matched to the
            // cell that owns them. Add's ports sit on both cells of its 2x1;
            // Mul's four all sit on its single cell.
            float a_in = 0.0, a_out = 0.0;
            if (type == TYPE_ADD) {
                // Inputs face +Y, outputs -Y, one pair per footprint cell.
                a_in  = max(a_in,  port_arrow(p, rot_face(DIR_PY, facing), true,  px));
                a_out = max(a_out, port_arrow(p, rot_face(DIR_NY, facing), false, px));
            } else if (type == TYPE_MUL) {
                a_in  = max(a_in,  port_arrow(p, rot_face(DIR_PY, facing), true,  px));
                a_in  = max(a_in,  port_arrow(p, rot_face(DIR_NX, facing), true,  px));
                a_out = max(a_out, port_arrow(p, rot_face(DIR_PX, facing), false, px));
                a_out = max(a_out, port_arrow(p, rot_face(DIR_NY, facing), false, px));
            }
            col = mix(col, COL_IN,  a_in  * alpha_scale);//can extend outside a_body
            col = mix(col, COL_OUT, a_out * alpha_scale);//can extend outside a_body
        }

        // Frustration is an overlay, never a replacement: whatever occupies
        // the cell is drawn above, and this tints on top so you can see
        // exactly what is in the way.
        if (gstate == GHOST_FRUSTRATED) {
            float sd = sd_box(p, vec2(FRUSTRATED_INSET));
            col = mix(col, COL_FRUSTRATED, fill(sd, px) * FRUSTRATED_ALPHA);
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

    fragColor = vec4(col, 1.0);
}
