// Motion blur's averaging (mblur.js has the timing): a small WebGL2
// accumulator. Each blur sample is a whole Canvas 2D render; it is uploaded
// and added into a floating-point buffer, and the buffer's mean is the
// frame.
//
// - **Floating point, not canvas alpha.** Averaging by drawing k canvases at
//   alpha 1/k rounds every step to 8 bits, which bands badly by 64 samples.
//   Half floats hold the sum of up to 64 samples to about 1/2000.
// - **In linear light.** A real exposure adds light, so each sample's sRGB
//   values are decoded before they are summed and the mean is encoded after:
//   a bright item smeared over a dark board stays as bright as the eye
//   expects instead of going muddy.
// - **Two layers when bloom is on.** Layer 0 is each sample before its item
//   numbers (render.js's `onCapture`), layer 1 the whole sample. The frame is
//   layer 1's mean, and bloom takes its glow from layer 0's: the numbers are
//   blurred like everything that moves, and still never glow.
//
// Needs WebGL2 and EXT_color_buffer_float (render to a half-float buffer);
// without them `ok` is false and motion blur is simply off, as bloom is
// without WebGL2.

const VS = `#version 300 es
in vec2 p;
out vec2 uv;
void main() { uv = p * 0.5 + 0.5; gl_Position = vec4(p, 0.0, 1.0); }`;

// sRGB <-> linear, the exact piecewise curves.
const CURVES = `
vec3 toLinear(vec3 c) { return mix(c / 12.92, pow((c + 0.055) / 1.055, vec3(2.4)), step(0.04045, c)); }
vec3 toSrgb(vec3 c) { c = max(c, 0.0); return mix(c * 12.92, 1.055 * pow(c, vec3(1.0 / 2.4)) - 0.055, step(0.0031308, c)); }`;

// One sample into the sum (blending ONE, ONE).
const ADD = `#version 300 es
precision highp float;
in vec2 uv;
uniform sampler2D src;
out vec4 o;
${CURVES}
void main() { o = vec4(toLinear(texture(src, uv).rgb), 1.0); }`;

// The mean, back to sRGB.
const MEAN = `#version 300 es
precision highp float;
in vec2 uv;
uniform sampler2D acc;
uniform float inv;
out vec4 o;
${CURVES}
void main() { o = vec4(toSrgb(texture(acc, uv).rgb * inv), 1.0); }`;

export class Accumulator {
  constructor() {
    this.canvas = document.createElement("canvas");
    const gl = this.gl = this.canvas.getContext("webgl2", { alpha: false, antialias: false, depth: false, stencil: false, premultipliedAlpha: true, preserveDrawingBuffer: true });
    this.ok = !!gl && !!gl.getExtension("EXT_color_buffer_float");
    if (!this.ok) return;
    const prog = (fs) => {
      const p = gl.createProgram();
      for (const [type, src] of [[gl.VERTEX_SHADER, VS], [gl.FRAGMENT_SHADER, fs]]) {
        const sh = gl.createShader(type);
        gl.shaderSource(sh, src); gl.compileShader(sh);
        if (!gl.getShaderParameter(sh, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(sh));
        gl.attachShader(p, sh);
      }
      gl.bindAttribLocation(p, 0, "p");
      gl.linkProgram(p);
      if (!gl.getProgramParameter(p, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(p));
      return p;
    };
    this.add = prog(ADD);
    this.mean = prog(MEAN);
    gl.bindBuffer(gl.ARRAY_BUFFER, gl.createBuffer());
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 1, -1, -1, 1, 1, 1]), gl.STATIC_DRAW);
    gl.enableVertexAttribArray(0);
    gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 0, 0);
    this.src = this.texture();
    this.layers = [0, 1].map(() => ({ tex: this.texture(), fb: gl.createFramebuffer(), n: 0 }));
    this.w = this.h = 0;
  }

  texture() {
    const gl = this.gl, t = gl.createTexture();
    gl.bindTexture(gl.TEXTURE_2D, t);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.NEAREST);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.NEAREST);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    return t;
  }

  // A fresh, empty sum at this size.
  begin(w, h) {
    const gl = this.gl;
    if (w !== this.w || h !== this.h) {
      this.w = this.canvas.width = w;
      this.h = this.canvas.height = h;
      for (const L of this.layers) {
        gl.bindTexture(gl.TEXTURE_2D, L.tex);
        gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA16F, w, h, 0, gl.RGBA, gl.HALF_FLOAT, null);
        gl.bindFramebuffer(gl.FRAMEBUFFER, L.fb);
        gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, L.tex, 0);
      }
    }
    gl.clearColor(0, 0, 0, 0);
    for (const L of this.layers) {
      gl.bindFramebuffer(gl.FRAMEBUFFER, L.fb);
      gl.clear(gl.COLOR_BUFFER_BIT);
      L.n = 0;
    }
  }

  // The canvas as it stands, added to layer `layer`'s sum. Uploaded flipped,
  // so the sums (and the mean drawn from them) are the right way up.
  addSample(canvas, layer) {
    const gl = this.gl, L = this.layers[layer];
    gl.bindTexture(gl.TEXTURE_2D, this.src);
    gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, true);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, canvas);
    gl.bindFramebuffer(gl.FRAMEBUFFER, L.fb);
    gl.viewport(0, 0, this.w, this.h);
    gl.useProgram(this.add);
    gl.uniform1i(gl.getUniformLocation(this.add, "src"), 0);
    gl.enable(gl.BLEND);
    gl.blendFunc(gl.ONE, gl.ONE);
    gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
    gl.disable(gl.BLEND);
    L.n++;
  }

  // Layer `layer`'s mean, drawn into this canvas, which is returned (to draw
  // from, or to hand bloom as its source).
  resolve(layer) {
    const gl = this.gl, L = this.layers[layer];
    gl.bindFramebuffer(gl.FRAMEBUFFER, null);
    gl.viewport(0, 0, this.w, this.h);
    gl.useProgram(this.mean);
    gl.activeTexture(gl.TEXTURE0);
    gl.bindTexture(gl.TEXTURE_2D, L.tex);
    gl.uniform1i(gl.getUniformLocation(this.mean, "acc"), 0);
    gl.uniform1f(gl.getUniformLocation(this.mean, "inv"), 1 / Math.max(1, L.n));
    gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
    return this.canvas;
  }
}
