// Scales: which pitch a belt run of length n sounds.
//
// A scale is a WAVELENGTH function f(n), normalised so the shortest audible
// run keeps the dial's pitch:
//
//     Hz(n) = (min-length Hz) * f(m) / f(n)        m = the min length dial
//
// f(n) = n is the original law (twice the length, an octave down; every run
// a subharmonic of the dial). Anything else is a formula or a table:
//
//   - a FORMULA in a small expression language (`compileScale()`), e.g.
//     `2 ** ((n - m) / 12)` for a semitone per tile. The presets are just
//     formulas, so picking one fills the box with something to edit;
//   - a TABLE (a "LUT" file of numbers), whose i-th number is f(i) from
//     n = 1; past its end the last value holds.
//
// The language is parsed here, never `eval`ed: a formula travels inside save
// files, so it must not be able to run code. Pure and dependency-free: the
// synth (audio-dsp.js) imports it on the audio thread and compiles the
// formula itself, so live playback, a bake and a video's soundtrack all
// read the same dials the same way.

// Scale steps in semitones from the root, one octave's worth; degree k
// (any integer) is steps[k mod len] + 12 * floor(k / len).
export const SCALE_STEPS = {
  major: [0, 2, 4, 5, 7, 9, 11],
  minor: [0, 2, 3, 5, 7, 8, 10],          // natural minor
  harmonic: [0, 2, 3, 5, 7, 8, 11],       // harmonic minor
  pent: [0, 2, 4, 7, 9],                  // major pentatonic
  minpent: [0, 3, 5, 7, 10],              // minor pentatonic
  blues: [0, 3, 5, 6, 7, 10],
  whole: [0, 2, 4, 6, 8, 10],
  dorian: [0, 2, 3, 5, 7, 9, 10],
};

export function degree(steps, k) {
  k = Math.round(k);
  const len = steps.length, o = Math.floor(k / len);
  return steps[k - o * len] + 12 * o;
}

// Presets: a name and a formula. Longer runs go DOWN the scale (f grows
// with n), counted from the min length, so the shortest run is the root.
export const SCALE_PRESETS = [
  ["Standard", "n"],
  ["Semitones", "2 ** ((n - m) / 12)"],
  ["Major", "2 ** (major(n - m) / 12)"],
  ["Minor", "2 ** (minor(n - m) / 12)"],
  ["Harmonic minor", "2 ** (harmonic(n - m) / 12)"],
  ["Pentatonic", "2 ** (pent(n - m) / 12)"],
  ["Minor pentatonic", "2 ** (minpent(n - m) / 12)"],
  ["Blues", "2 ** (blues(n - m) / 12)"],
  ["Whole tone", "2 ** (whole(n - m) / 12)"],
  ["Dorian", "2 ** (dorian(n - m) / 12)"],
];

const FUNCS = {
  floor: [1, Math.floor], ceil: [1, Math.ceil], round: [1, Math.round], abs: [1, Math.abs],
  sqrt: [1, Math.sqrt], exp: [1, Math.exp], log: [1, Math.log], log2: [1, Math.log2], log10: [1, Math.log10],
  sin: [1, Math.sin], cos: [1, Math.cos], tan: [1, Math.tan], sign: [1, Math.sign],
  fract: [1, (x) => x - Math.floor(x)],
  min: [2, Math.min], max: [2, Math.max], pow: [2, Math.pow],
  mod: [2, (a, b) => a - b * Math.floor(a / b)],
  clamp: [3, (x, a, b) => Math.min(b, Math.max(a, x))],
};
for (const [name, steps] of Object.entries(SCALE_STEPS)) FUNCS[name] = [1, (k) => degree(steps, k)];
const CONSTS = { pi: Math.PI, e: Math.E };
const VARS = ["n", "m"];

// Every name a formula may use, for the dialog's help line.
export const SCALE_NAMES = { vars: VARS, consts: Object.keys(CONSTS), funcs: Object.keys(FUNCS) };

function tokenize(src) {
  const out = [];
  const re = /\s*(?:(\d+\.?\d*(?:[eE][+-]?\d+)?|\.\d+(?:[eE][+-]?\d+)?)|([A-Za-z_]\w*)|(\*\*|[-+*/%^(),]))/y;
  let i = 0;
  src = String(src);
  while (i < src.length) {
    if (/^\s*$/.test(src.slice(i))) break;
    re.lastIndex = i;
    const t = re.exec(src);
    if (!t) throw new Error(`unexpected "${src.slice(i).trim()[0]}" at ${i + 1}`);
    if (t[1] !== undefined) out.push({ k: "num", v: Number(t[1]) });
    else if (t[2] !== undefined) out.push({ k: "id", v: t[2] });
    else out.push({ k: "op", v: t[3] === "^" ? "**" : t[3] });
    i = re.lastIndex;
  }
  return out;
}

// A formula to a function (n, m) => number. Throws with a readable message.
//   expr  := add
//   add   := mul (('+' | '-') mul)*
//   mul   := unary (('*' | '/' | '%') unary)*        % is a modulo that is
//   unary := ('-' | '+') unary | pow                   never negative
//   pow   := atom (('**' | '^') unary)?              right-associative; -2**2 is -4
//   atom  := number | name | name '(' args ')' | '(' expr ')'
export function compileScale(src) {
  const toks = tokenize(src);
  if (!toks.length) throw new Error("empty formula");
  let p = 0;
  const peek = () => toks[p];
  const isOp = (v) => peek()?.k === "op" && peek().v === v;
  const expect = (v) => { if (!isOp(v)) throw new Error(`expected "${v}"`); p++; };
  const add = () => {
    let a = mul();
    while (isOp("+") || isOp("-")) {
      const op = toks[p++].v, l = a, r = mul();
      a = op === "+" ? (s) => l(s) + r(s) : (s) => l(s) - r(s);
    }
    return a;
  };
  const mul = () => {
    let a = unary();
    while (isOp("*") || isOp("/") || isOp("%")) {
      const op = toks[p++].v, l = a, r = unary();
      a = op === "*" ? (s) => l(s) * r(s)
        : op === "/" ? (s) => l(s) / r(s)
        : (s) => { const x = l(s), y = r(s); return x - y * Math.floor(x / y); };
    }
    return a;
  };
  const unary = () => {
    if (isOp("-")) { p++; const a = unary(); return (s) => -a(s); }
    if (isOp("+")) { p++; return unary(); }
    return pow();
  };
  const pow = () => {
    const a = atom();
    if (isOp("**")) { p++; const b = unary(); return (s) => Math.pow(a(s), b(s)); }
    return a;
  };
  const atom = () => {
    const t = toks[p++];
    if (!t) throw new Error("the formula ends too soon");
    if (t.k === "num") { const v = t.v; return () => v; }
    if (t.k === "op" && t.v === "(") { const a = add(); expect(")"); return a; }
    if (t.k === "id") {
      if (isOp("(")) {
        const f = FUNCS[t.v];
        if (!f) throw new Error(`no function "${t.v}"`);
        p++;
        const args = [];
        if (!isOp(")")) { args.push(add()); while (isOp(",")) { p++; args.push(add()); } }
        expect(")");
        if (args.length !== f[0]) throw new Error(`${t.v}() takes ${f[0]} argument${f[0] === 1 ? "" : "s"}`);
        const fn = f[1];
        if (args.length === 1) { const [a] = args; return (s) => fn(a(s)); }
        if (args.length === 2) { const [a, b] = args; return (s) => fn(a(s), b(s)); }
        return (s) => fn(...args.map(g => g(s)));
      }
      if (t.v in CONSTS) { const v = CONSTS[t.v]; return () => v; }
      if (t.v === "n") return (s) => s.n;
      if (t.v === "m") return (s) => s.m;
      throw new Error(`no name "${t.v}" (use n, m, pi, e, or a function)`);
    }
    throw new Error(`unexpected "${t.v}"`);
  };
  const f = add();
  if (p < toks.length) throw new Error(`unexpected "${toks[p].v}" after the formula`);
  return (n, m) => f({ n, m });
}

// A LUT file: numbers separated by commas, semicolons or whitespace. Each
// must be a positive wavelength.
export const LUT_MAX = 4096;
export function parseLut(text) {
  const out = [];
  String(text).split(/\r?\n/).forEach((line, li) => {
    for (const tok of line.split(/[\s,;]+/)) {
      if (!tok) continue;
      const v = Number(tok);
      if (!(Number.isFinite(v) && v > 0)) throw new Error(`line ${li + 1}: "${tok}" is not a positive number`);
      out.push(v);
    }
  });
  if (!out.length) throw new Error("the file holds no numbers");
  if (out.length > LUT_MAX) throw new Error(`more than ${LUT_MAX} numbers`);
  return out;
}

// The wavelength table the synth reads: f(n) for n = 0 .. size, from the
// dials `scaleMode` ("formula" or "lut"), `scaleExpr`, `scaleLut` and the min
// length. Any entry that is not a positive finite number falls back to n
// (the standard law), and is counted; so does every entry when the formula
// will not compile or the table is empty. Returns { f, bad, error }.
export const SCALE_SIZE = 1024;
export function scaleTable(p, size = SCALE_SIZE) {
  const m = Math.max(1, p.beltMinLen || 1);
  const f = new Float64Array(size + 1);
  let fn = null, error = null;
  try {
    if (p.scaleMode === "lut") {
      const lut = parseLut(p.scaleLut || "");
      fn = (n) => lut[Math.min(lut.length, Math.max(1, n)) - 1];
    } else fn = compileScale(p.scaleExpr || "n");
  } catch (e) { error = e.message; fn = null; }
  let bad = 0;
  for (let n = 0; n <= size; n++) {
    let v = NaN;
    if (fn) { try { v = fn(n, m); } catch { v = NaN; } }
    if (!(Number.isFinite(v) && v > 0)) { v = Math.max(n, 1); if (fn && n >= 1) bad++; }
    f[n] = v;
  }
  return { f, bad, error };
}

// The pitch of a run of length n under table f: the dial's pitch times
// f(m) / f(n), with m the min length (at least 1).
export function scaleHz(f, n, maxHz, minLen) {
  const m = Math.max(1, minLen), last = f.length - 1;
  const fn = n <= last ? f[n] : f[last] * n / last;   // past the table: the standard law's slope
  return maxHz * f[Math.min(m, last)] / fn;
}

// The nearest note's name and the cents off it, e.g. "A4 +3".
const NOTE = ["C", "C♯", "D", "D♯", "E", "F", "F♯", "G", "G♯", "A", "A♯", "B"];
export function noteName(hz) {
  if (!(hz > 0)) return "–";
  const semis = 12 * Math.log2(hz / 440) + 57;   // C0 = 0
  const k = Math.round(semis), cents = Math.round((semis - k) * 100);
  const name = `${NOTE[((k % 12) + 12) % 12]}${Math.floor(k / 12)}`;
  return cents ? `${name} ${cents > 0 ? "+" : ""}${cents}` : name;
}
