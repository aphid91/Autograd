#version 430

// World renderer: a fullscreen quad that maps each pixel to a grid cell,
// reads the cell from the grid texture, and draws grid lines + entities.

out vec4 fragColor;

uniform vec2  resolution;     // framebuffer size, px
uniform vec2  cam_center;     // view centre, world (cell) units
uniform float cam_zoom;       // pixels per cell
uniform ivec2 board_size;     // grid dimensions, cells
uniform isampler2D grid;      // RGBA32I, one texel per cell

// --- cell encoding ------------------------------------------------------
// MUST match cell.py. See the module docstring there.
//   .x  bits 0-1  direction, 2-8 type, 9-30 entity id
//   .y  packed (local_x, local_y) within a multi-cell footprint
//   .z  bits 0-1  ghost state
const int DIR_SHIFT = 0,  DIR_MASK  = 0x3;
const int TYPE_SHIFT = 2, TYPE_MASK = 0x7F;
const int ID_SHIFT = 9,   ID_MASK   = 0x3FFFFF;
const int GHOST_SHIFT = 0, GHOST_MASK = 0x3;
const int LOCAL_SHIFT = 16, LOCAL_MASK = 0xFFFF;

const int GHOST_NONE = 0;
const int GHOST_PENDING = 1;
const int GHOST_FRUSTRATED = 2;

const int TYPE_EMPTY = 0;
const int TYPE_BELT  = 1;
const int TYPE_ADD   = 2;
const int TYPE_MUL   = 3;

int cell_type (int w) { return (w >> TYPE_SHIFT)  & TYPE_MASK;  }
int cell_dir  (int w) { return (w >> DIR_SHIFT)   & DIR_MASK;   }
int cell_id   (int w) { return (w >> ID_SHIFT)    & ID_MASK;    }
int ghost_state(int w){ return (w >> GHOST_SHIFT) & GHOST_MASK; }
ivec2 cell_local(int w) {
    return ivec2((w >> LOCAL_SHIFT) & LOCAL_MASK, w & LOCAL_MASK);
}

// --- palette ------------------------------------------------------------
const vec3 COL_VOID      = vec3(0.07, 0.07, 0.09);  // outside the board
const vec3 COL_BG        = vec3(0.12, 0.12, 0.14);  // empty cell
const vec3 COL_GRID      = vec3(0.22, 0.22, 0.26);
const vec3 COL_GRID_MAJOR= vec3(0.30, 0.30, 0.35);
const vec3 COL_BELT      = vec3(0.55, 0.55, 0.58);
const vec3 COL_ARROW     = vec3(0.16, 0.16, 0.18);
const vec3 COL_ADD       = vec3(0.42, 0.58, 0.44);
const vec3 COL_MUL       = vec3(0.56, 0.46, 0.64);
const vec3 COL_FRUSTRATED= vec3(0.90, 0.25, 0.22);

vec3 body_colour(int type) {
    if (type == TYPE_ADD) return COL_ADD;
    if (type == TYPE_MUL) return COL_MUL;
    return COL_BELT;
}

const int MAJOR_EVERY = 8;   // heavier line every N cells

// Rotate a cell-local coordinate so that +X becomes the entity's direction.
// Local space is centred on the cell, -0.5..0.5.
vec2 rotate_to_dir(vec2 p, int dir) {
    if (dir == 0) return p;                  // +X
    if (dir == 1) return vec2(p.y, -p.x);    // +Y
    if (dir == 2) return vec2(-p.x, -p.y);   // -X
    return vec2(-p.y, p.x);                  // -Y
}

// Signed distance to a triangle, used for the belt arrow.
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

// Antialiased fill: 1 inside, 0 outside, smooth over ~1px.
float fill(float sd, float px) {
    return 1.0 - smoothstep(-px, px, sd);
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

    bool inside = all(greaterThanEqual(ci, ivec2(0)))
               && all(lessThan(ci, board_size));

    vec3 col = inside ? COL_BG : COL_VOID;

    if (inside) {
        ivec4 c = texelFetch(grid, ci, 0);
        int type = cell_type(c.x);

        int gstate = ghost_state(c.z);

        if (type != TYPE_EMPTY) {
            int dir = cell_dir(c.x);

            // Cell-local coords centred on the cell, -0.5..0.5.
            vec2 p = cf - 0.5;

            // A multi-cell entity should read as one block, so the inset is
            // dropped on edges that face another cell of the same entity.
            // .y carries this cell's offset within the footprint, so we can
            // tell an interior edge from an outer one without any neighbour
            // lookups.
            ivec2 lcl = cell_local(c.y);
            // Recover the footprint extent by probing neighbours for the same
            // entity id. Cheap (4 fetches) and keeps the extent out of the
            // cell encoding.
            int id = cell_id(c.x);
            ivec2 lo = ivec2(0), hi = ivec2(0);
            for (int k = 1; k <= 3; ++k) {
                ivec2 n = ci + ivec2(k, 0);
                if (n.x >= board_size.x || cell_id(texelFetch(grid, n, 0).x) != id) break;
                hi.x = k;
            }
            for (int k = 1; k <= 3; ++k) {
                ivec2 n = ci - ivec2(k, 0);
                if (n.x < 0 || cell_id(texelFetch(grid, n, 0).x) != id) break;
                lo.x = k;
            }
            for (int k = 1; k <= 3; ++k) {
                ivec2 n = ci + ivec2(0, k);
                if (n.y >= board_size.y || cell_id(texelFetch(grid, n, 0).x) != id) break;
                hi.y = k;
            }
            for (int k = 1; k <= 3; ++k) {
                ivec2 n = ci - ivec2(0, k);
                if (n.y < 0 || cell_id(texelFetch(grid, n, 0).x) != id) break;
                lo.y = k;
            }

            float inset = 0.42;
            // Push the boundary past the cell edge where the entity continues,
            // so the halves of a multi-cell footprint merge into one shape.
            // Going *past* 0.5 rather than exactly to it matters: at exactly
            // 0.5 both cells antialias their shared edge and the blend leaves
            // a visible seam down the middle.
            float over = 0.5 + px;
            vec4 edge = vec4(
                (lo.x > 0) ? over : inset,   // -X
                (hi.x > 0) ? over : inset,   // +X
                (lo.y > 0) ? over : inset,   // -Y
                (hi.y > 0) ? over : inset);  // +Y
            float sd_body = max(
                max(-p.x - edge.x, p.x - edge.y),
                max(-p.y - edge.z, p.y - edge.w));
            float a_body = fill(sd_body, px);

            // Orientation arrow, drawn once per entity rather than once per
            // cell. Work in footprint space: `p` is this cell's local offset
            // from its centre, and (lcl - centre) shifts it to the whole
            // entity's centre, so the arrow is centred on a 2x1 instead of on
            // one of its halves. Every cell evaluates this -- the arrow simply
            // falls outside most of them.
            vec2 extent = vec2(lo + hi) + 1.0;          // footprint w,h
            vec2 centre = (extent - 1.0) * 0.5;         // centre in local cells
            vec2 pf = p + (vec2(lcl) - centre);
            // Scale so the arrow keeps its proportions on a long footprint.
            float s = max(extent.x, extent.y);
            vec2 pr = rotate_to_dir(pf / s, dir);
            float sd_arrow = sd_triangle(pr,
                vec2( 0.26,  0.00),
                vec2(-0.12,  0.20),
                vec2(-0.12, -0.20));
            float a_arrow = fill(sd_arrow, px / s);

            vec3 ent = mix(body_colour(type), COL_ARROW, a_arrow);
            float alpha = a_body * (gstate == GHOST_PENDING ? 0.45 : 1.0);
            col = mix(col, ent, alpha);
        }

        // Frustration is an overlay, never a replacement: the occupying
        // entity is drawn above, and this tints the cell on top of it so you
        // can see exactly what is in the way.
        if (gstate == GHOST_FRUSTRATED) {
            vec2 p = cf - 0.5;
            float sd = max(abs(p.x), abs(p.y)) - 0.46;
            col = mix(col, COL_FRUSTRATED, fill(sd, px) * 0.55);
        }
    }

    // --- grid lines -----------------------------------------------------
    // Distance to the nearest cell boundary, in pixels, on each axis.
    vec2 to_edge = min(cf, 1.0 - cf) / px;
    float line = 1.0 - smoothstep(0.0, 1.0, min(to_edge.x, to_edge.y));

    // Fade the grid out when cells get small, so it does not turn into mush.
    float grid_fade = smoothstep(3.0, 9.0, cam_zoom);

    bool major = (ci.x % MAJOR_EVERY == 0) || (ci.y % MAJOR_EVERY == 0);
    vec3 line_col = major ? COL_GRID_MAJOR : COL_GRID;
    col = mix(col, line_col, line * grid_fade * 0.9);

    // Board border, drawn slightly brighter so the playfield edge is clear.
    vec2 b0 = abs(world) / px;
    vec2 b1 = abs(world - vec2(board_size)) / px;
    float border = 1.0 - smoothstep(0.0, 1.5,
        min(min(b0.x, b0.y), min(b1.x, b1.y)));
    bool near_board = all(greaterThanEqual(world, vec2(-2.0)))
                   && all(lessThanEqual(world, vec2(board_size) + 2.0));
    if (near_board) col = mix(col, vec3(0.45, 0.45, 0.52), border * 0.8);

    fragColor = vec4(col, 1.0);
}
