// Bloom: a glow round the brightest parts of the board. The renderer is
// Canvas 2D, which can blur but cannot threshold, so this is a small WebGL2
// post-pass on a canvas of its own, never shown: render() hands it the 2D
// canvas at the point where the board is drawn and the editing marks are
// not (`onWorld`), and the glow is added back into the 2D canvas there
// ("lighter"), so selections, frame boxes and screen-space text stay crisp
// on top and never glow themselves.
//
// The pass, all at half resolution or less:
//   1. bright pass: each pixel keeps the part of its colour above the cutoff
//      (by luminance, with a short soft knee so the edge does not alias),
//      downsampled to half size;
//   2. a chain of halvings, each a 13-tap filter (Jimenez, "Next generation
//      post processing in Call of Duty", 2014), so a bright pixel does not
//      flicker as it moves;
//   3. back up the chain, a 3x3 tent at each step added to the level above.
//      Radius is how many levels: 1 is a tight halo, 8 a wide soft one,
//      fractional weighting the last;
//   4. the result times intensity, drawn into the 2D canvas, scaled up.
//
// Loaded only when View > bloom intensity is above 0; off costs nothing.
// Video frames have their own instance (their canvas is a different size).

const VS = `#version 300 es
in vec2 p;
out vec2 uv;
void main() { uv = p * 0.5 + 0.5; gl_Position = vec4(p, 0.0, 1.0); }`;

const BRIGHT = `#version 300 es
precision highp float;
in vec2 uv;
uniform sampler2D src;
uniform vec2 texel;     // one source pixel
uniform float cutoff, knee;
out vec4 o;
vec3 pick(vec2 t) {
  vec3 c = texture(src, t).rgb;
  float l = dot(c, vec3(0.2126, 0.7152, 0.0722));
  // A soft threshold: 0 below cutoff - knee, rising quadratically to the
  // straight (l - cutoff) above cutoff + knee.
  float s = clamp(l - cutoff + knee, 0.0, 2.0 * knee);
  s = s * s / (4.0 * knee + 1e-5);
  float w = max(s, l - cutoff) / max(l, 1e-4);
  return c * w;
}
void main() {
  // A 2x2 box at the half-size texel's corners: the downsample.
  vec2 h = texel * 0.5;
  o = vec4((pick(uv + vec2(-h.x, -h.y)) + pick(uv + vec2(h.x, -h.y)) +
            pick(uv + vec2(-h.x, h.y)) + pick(uv + vec2(h.x, h.y))) * 0.25, 1.0);
}`;

const DOWN = `#version 300 es
precision highp float;
in vec2 uv;
uniform sampler2D src;
uniform vec2 texel;     // one source pixel
out vec4 o;
vec3 s(float x, float y) { return texture(src, uv + texel * vec2(x, y)).rgb; }
void main() {
  vec3 a = s(-2., 2.), b = s(0., 2.), c = s(2., 2.);
  vec3 d = s(-2., 0.), e = s(0., 0.), f = s(2., 0.);
  vec3 g = s(-2., -2.), h = s(0., -2.), i = s(2., -2.);
  vec3 j = s(-1., 1.), k = s(1., 1.), l = s(-1., -1.), m = s(1., -1.);
  vec3 r = e * 0.125 + (a + c + g + i) * 0.03125 + (b + d + f + h) * 0.0625 + (j + k + l + m) * 0.125;
  o = vec4(r, 1.0);
}`;

const UP = `#version 300 es
precision highp float;
in vec2 uv;
uniform sampler2D src;  // the smaller level, built so far
uniform sampler2D add;  // this level's own downsample
uniform vec2 texel;     // one pixel of the smaller level
uniform float w;        // how much of the smaller level to take
out vec4 o;
vec3 s(float x, float y) { return texture(src, uv + texel * vec2(x, y)).rgb; }
void main() {
  vec3 t = s(0., 0.) * 4.0 + (s(-1., 0.) + s(1., 0.) + s(0., -1.) + s(0., 1.)) * 2.0
         + s(-1., -1.) + s(1., -1.) + s(-1., 1.) + s(1., 1.);
  o = vec4(texture(add, uv).rgb + w * t / 16.0, 1.0);
}`;

const OUT = `#version 300 es
precision highp float;
in vec2 uv;
uniform sampler2D src;
uniform float gain;
out vec4 o;
void main() { o = vec4(min(texture(src, uv).rgb * gain, vec3(1.0)), 1.0); }`;

export const BLOOM_LEVELS = 8;

export class Bloom {
  constructor() {
    this.canvas = document.createElement("canvas");
    const gl = this.gl = this.canvas.getContext("webgl2", { alpha: false, antialias: false, depth: false, stencil: false, premultipliedAlpha: true, preserveDrawingBuffer: true });
    if (!gl) throw new Error("bloom needs WebGL2");
    // Half floats where they can be rendered to, so levels summing past 1
    // keep their brightness; bytes otherwise (the glow clips sooner).
    this.float = !!gl.getExtension("EXT_color_buffer_float") || !!gl.getExtension("EXT_color_buffer_half_float");
    gl.getExtension("OES_texture_float_linear");
    const prog = (fs) => {
      const p = gl.createProgram();
      for (const [type, src] of [[gl.VERTEX_SHADER, VS], [gl.FRAGMENT_SHADER, fs]]) {
        const sh = gl.createShader(type);
        gl.shaderSource(sh, src);
        gl.compileShader(sh);
        if (!gl.getShaderParameter(sh, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(sh));
        gl.attachShader(p, sh);
      }
      gl.bindAttribLocation(p, 0, "p");
      gl.linkProgram(p);
      if (!gl.getProgramParameter(p, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(p));
      const u = {};
      const n = gl.getProgramParameter(p, gl.ACTIVE_UNIFORMS);
      for (let i = 0; i < n; i++) { const name = gl.getActiveUniform(p, i).name; u[name] = gl.getUniformLocation(p, name); }
      return { p, u };
    };
    this.bright = prog(BRIGHT); this.down = prog(DOWN); this.up = prog(UP); this.out = prog(OUT);
    const buf = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, buf);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 3, -1, -1, 3]), gl.STATIC_DRAW);
    gl.enableVertexAttribArray(0);
    gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 0, 0);
    this.src = this.texture();
    this.levels = [];     // [{ w, h, down: {tex, fb}, up: {tex, fb} }]
    this.w = this.h = 0;
  }

  texture() {
    const gl = this.gl, t = gl.createTexture();
    gl.bindTexture(gl.TEXTURE_2D, t);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    return t;
  }

  target(w, h) {
    const gl = this.gl, tex = this.texture();
    if (this.float) gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA16F, w, h, 0, gl.RGBA, gl.HALF_FLOAT, null);
    else gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA8, w, h, 0, gl.RGBA, gl.UNSIGNED_BYTE, null);
    const fb = gl.createFramebuffer();
    gl.bindFramebuffer(gl.FRAMEBUFFER, fb);
    gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, tex, 0);
    return { tex, fb };
  }

  // The chain for a w x h source: level 0 is half size, each next half again.
  resize(w, h) {
    if (w === this.w && h === this.h) return;
    const gl = this.gl;
    for (const l of this.levels) for (const t of [l.down, l.up]) { gl.deleteTexture(t.tex); gl.deleteFramebuffer(t.fb); }
    this.levels = [];
    this.w = w; this.h = h;
    let lw = w, lh = h;
    for (let i = 0; i < BLOOM_LEVELS; i++) {
      lw = Math.max(1, lw >> 1); lh = Math.max(1, lh >> 1);
      this.levels.push({ w: lw, h: lh, down: this.target(lw, lh), up: this.target(lw, lh) });
      if (lw === 1 && lh === 1) break;
    }
    this.canvas.width = this.levels[0].w;
    this.canvas.height = this.levels[0].h;
  }

  pass(prog, fb, w, h, tex, uniforms) {
    const gl = this.gl;
    gl.bindFramebuffer(gl.FRAMEBUFFER, fb);
    gl.viewport(0, 0, w, h);
    gl.useProgram(prog.p);
    let unit = 0;
    for (const [name, v] of Object.entries(uniforms)) {
      const loc = prog.u[name];
      if (loc === undefined) continue;
      if (v && v.tex) { gl.activeTexture(gl.TEXTURE0 + unit); gl.bindTexture(gl.TEXTURE_2D, v.tex); gl.uniform1i(loc, unit++); }
      else if (Array.isArray(v)) gl.uniform2f(loc, v[0], v[1]);
      else gl.uniform1f(loc, v);
    }
    if (tex) { gl.activeTexture(gl.TEXTURE0 + unit); gl.bindTexture(gl.TEXTURE_2D, tex); gl.uniform1i(prog.u.src, unit); }
    gl.drawArrays(gl.TRIANGLES, 0, 3);
  }

  // Bloom `ctx`'s canvas as it stands and add the glow back into it.
  // `intensity` 0..: the glow's gain; `cutoff` 0..1: the luminance below
  // which nothing glows; `radius` 1..BLOOM_LEVELS: how wide.
  apply(ctx, { intensity, cutoff, radius }) {
    const src = ctx.canvas, gl = this.gl;
    if (!(intensity > 0) || !src.width || !src.height) return;
    this.resize(src.width, src.height);
    gl.bindTexture(gl.TEXTURE_2D, this.src);
    gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, true);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, src);
    // n levels, the deepest weighted by the fraction past n - 1, so the
    // radius is continuous: 1 is the bright pass alone, 2 adds one level.
    const L = this.levels, r = Math.min(L.length, Math.max(1, radius));
    const n = Math.max(1, Math.ceil(r)), frac = n === 1 ? 0 : r - (n - 1);
    const knee = Math.max(0.02, 0.1 * (1 - cutoff));
    this.pass(this.bright, L[0].down.fb, L[0].w, L[0].h, this.src, { texel: [1 / src.width, 1 / src.height], cutoff, knee });
    for (let i = 1; i < n; i++) {
      this.pass(this.down, L[i].down.fb, L[i].w, L[i].h, L[i - 1].down.tex, { texel: [1 / L[i - 1].w, 1 / L[i - 1].h] });
    }
    // Up: the smallest level is its own downsample; each larger one adds the
    // tent of the one below. The deepest takes `frac`, so radius is smooth.
    let top = L[n - 1].down;
    for (let i = n - 2; i >= 0; i--) {
      const w = i === n - 2 ? frac : 1;
      this.pass(this.up, L[i].up.fb, L[i].w, L[i].h, top.tex, { add: L[i].down, texel: [1 / L[i + 1].w, 1 / L[i + 1].h], w });
      top = L[i].up;
    }
    this.pass(this.out, null, L[0].w, L[0].h, top.tex, { gain: intensity });

    ctx.save();
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.globalCompositeOperation = "lighter";
    ctx.globalAlpha = 1;
    ctx.imageSmoothingEnabled = true;
    ctx.drawImage(this.canvas, 0, 0, src.width, src.height);
    ctx.restore();
  }
}
