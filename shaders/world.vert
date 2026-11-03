#version 430

// Fullscreen quad. The fragment shader does all the work, mapping each pixel
// to a world position and then to a grid cell.

in vec2 in_pos;

void main() {
    gl_Position = vec4(in_pos, 0.0, 1.0);
}
