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
//   .z  bit  0    is_ghost
const int DIR_SHIFT = 0,  DIR_MASK  = 0x3;
const int TYPE_SHIFT = 2, TYPE_MASK = 0x7F;
const int ID_SHIFT = 9,   ID_MASK   = 0x3FFFFF;
const int GHOST_BIT = 1;

const int TYPE_EMPTY = 0;
const int TYPE_BELT  = 1;

int cell_type(int w) { return (w >> TYPE_SHIFT) & TYPE_MASK; }
int cell_dir (int w) { return (w >> DIR_SHIFT)  & DIR_MASK;  }
int cell_id  (int w) { return (w >> ID_SHIFT)   & ID_MASK;   }

// --- palette ------------------------------------------------------------
const vec3 COL_VOID      = vec3(0.07, 0.07, 0.09);  // outside the board
const vec3 COL_BG        = vec3(0.12, 0.12, 0.14);  // empty cell
const vec3 COL_GRID      = vec3(0.22, 0.22, 0.26);
const vec3 COL_GRID_MAJOR= vec3(0.30, 0.30, 0.35);
const vec3 COL_BELT      = vec3(0.55, 0.55, 0.58);
const vec3 COL_ARROW     = vec3(0.16, 0.16, 0.18);

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

        if (type != TYPE_EMPTY) {
            int dir = cell_dir(c.x);
            bool ghost = (c.z & GHOST_BIT) != 0;

            // Cell-local coords centred on the cell, -0.5..0.5.
            vec2 p = cf - 0.5;

            vec3 body = COL_BELT;
            // Inset square so adjacent belts read as separate tiles.
            float inset = 0.42;
            vec2 q = abs(p);
            float sd_body = max(q.x, q.y) - inset;
            float a_body = fill(sd_body, px);

            // Arrow pointing along the belt direction.
            vec2 pr = rotate_to_dir(p, dir);
            float sd_arrow = sd_triangle(pr,
                vec2( 0.26,  0.00),
                vec2(-0.12,  0.20),
                vec2(-0.12, -0.20));
            float a_arrow = fill(sd_arrow, px);

            vec3 ent = mix(body, COL_ARROW, a_arrow);
            float alpha = a_body * (ghost ? 0.45 : 1.0);
            col = mix(col, ent, alpha);
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
