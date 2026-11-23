// The Hack assembler: the nand2tetris machine language, plus a tiny
// preprocessor. Pure -- no DOM, no storage -- so Node tests it.
//
// assemble(source) -> { words, errors, lineOf, symbols }
//   words    one unsigned 16-bit number per instruction (0..65535), in ROM
//            order. These are what indexing writes into Sources: the machine
//            code read as an unsigned integer, so "15" is the low four bits.
//   errors   [{ line, msg }], 1-based source lines; `words` is still filled
//            as far as it could be, but a program with errors is not usable
//   lineOf   lineOf[i] is the source line of word i
//   symbols  the final symbol table (predefined, labels, variables)
//
// The language, per the standard spec -- except the comp bits, which are
// this project's own ALU's (see COMP_A):
//   @value     A-instruction: a decimal constant 0..32767, or a symbol
//   dest=comp;jump   C-instruction; dest= and ;jump are each optional
//   (LABEL)    names the address of the next instruction
//   // ...     comment to end of line; whitespace is ignored everywhere
// Symbols are letters, digits, _ . $ : and may not start with a digit.
// A symbol that is neither predefined nor a label is a variable, given the
// next free RAM address from VAR_BASE in order of first use.
//
// Two leniencies over the textbook assembler, both unambiguous: dest letters
// may come in any order (DM = MD), and the commutative comps may be written
// either way round (A+D = D+A, M&D = D&M, ...).
//
// The preprocessor (see preprocess()): `#define NAME replacement`.

// The symbols every program starts with. One table, so a platform that maps
// its memory differently (the screen somewhere else, say) changes it here.
export const PREDEFINED = {
  SP: 0, LCL: 1, ARG: 2, THIS: 3, THAT: 4,
  ...Object.fromEntries(Array.from({ length: 16 }, (_, i) => [`R${i}`, i])),
  SCREEN: 16384, KBD: 24576,
};
// Where variables start being allocated (just past R15).
export const VAR_BASE = 16;
// The largest constant an A-instruction can hold: its top bit is the opcode.
export const A_MAX = 32767;

// comp -> "a c1..c6", for the A forms; the M forms are the same bits with a=1.
// NOT the book's table: these are the control bits of this project's own ALU,
// which decodes c1..c6 differently. 0, -D, -A and the two-register comps
// happen to match the book; 1, -1, D, A, !D, !A and the four +-1 forms do
// not (D+1/D-1 and A+1/A-1 are each the book's codes the other way round).
// Bit order is unchanged: c1 lands in bit 11, right after a.
const COMP_A = {
  "0": "101010", "1": "111101", "-1": "111111",
  "D": "001010", "A": "100010", "!D": "001011", "!A": "100011",
  "-D": "001111", "-A": "110011", "D+1": "001110", "A+1": "110010",
  "D-1": "011111", "A-1": "110111", "D+A": "000010", "D-A": "010011",
  "A-D": "000111", "D&A": "000000", "D|A": "010101",
};
export const COMP = {};
for (const [k, c] of Object.entries(COMP_A)) {
  COMP[k] = "0" + c;
  if (k.includes("A")) COMP[k.replace("A", "M")] = "1" + c;
}
// The commutative ones, the other way round.
for (const k of Object.keys(COMP)) {
  const m = /^([ADM1])([+&|])([ADM1])$/.exec(k);
  if (m) COMP[m[3] + m[2] + m[1]] ??= COMP[k];
}
export const JUMP = { JGT: "001", JEQ: "010", JGE: "011", JLT: "100", JNE: "101", JLE: "110", JMP: "111" };

const SYMBOL = /^[A-Za-z_.$:][A-Za-z0-9_.$:]*$/;

// dest letters in any order, each at most once -> "d1 d2 d3" (A, D, M).
function destBits(dest) {
  if (!/^[ADM]{1,3}$/.test(dest) || new Set(dest).size !== dest.length) return null;
  return ["A", "D", "M"].map(r => (dest.includes(r) ? "1" : "0")).join("");
}

// The preprocessor. `#define NAME replacement` (NAME any run of non-space
// characters, the replacement the rest of the line, trimmed, comment
// removed -- it may be empty, or contain spaces). Every other line has every
// define replaced in it as plain text, in the order they were defined, so a
// later define sees the text an earlier one produced; a define's own
// replacement is expanded by the defines before it when it is defined.
// Defines apply to the whole file, wherever they sit in it.
//
// Returns { lines, defines, errors }: `lines` has one entry per source line
// (a directive line becomes ""), so line numbers survive.
export function preprocess(source) {
  const raw = source.split(/\r?\n/);
  const defines = [];
  const errors = [];
  const expand = (text) => defines.reduce((t, d) => t.split(d.name).join(d.value), text);
  const isDirective = raw.map(l => /^\s*#/.test(l));
  raw.forEach((l, i) => {
    if (!isDirective[i]) return;
    const body = stripComment(l).trim();
    const m = /^#define(?:\s+(\S+)(?:\s+(.*))?)?$/.exec(body);
    if (!m) {
      const word = (/^#\S*/.exec(body) || ["#"])[0];
      errors.push({ line: i + 1, msg: word === "#define" ? "#define needs a name" : `unknown directive "${word}"` });
      return;
    }
    if (!m[1]) { errors.push({ line: i + 1, msg: "#define needs a name" }); return; }
    defines.push({ name: m[1], value: expand((m[2] || "").trim()), line: i + 1 });
  });
  const lines = raw.map((l, i) => (isDirective[i] ? "" : expand(l)));
  return { lines, defines, errors };
}

export function stripComment(line) {
  const k = line.indexOf("//");
  return k < 0 ? line : line.slice(0, k);
}

// One line, already preprocessed, as { kind, ... } or null when it is blank.
//   { kind: "label", name }  { kind: "a", value | symbol }  { kind: "c", bits }
//   { kind: "error", msg }
export function parseLine(line) {
  const s = stripComment(line).replace(/\s+/g, "");
  if (!s) return null;
  if (s[0] === "(") {
    const m = /^\((.*)\)$/.exec(s);
    if (!m) return { kind: "error", msg: "a label is (NAME), with its closing bracket" };
    if (!SYMBOL.test(m[1])) return { kind: "error", msg: `"${m[1]}" is not a valid label name` };
    return { kind: "label", name: m[1] };
  }
  if (s[0] === "@") {
    const v = s.slice(1);
    if (!v) return { kind: "error", msg: "@ needs a number or a symbol" };
    if (/^\d+$/.test(v)) {
      const n = Number(v);
      if (n > A_MAX) return { kind: "error", msg: `@${v} is too large: an A-instruction holds 0..${A_MAX}` };
      return { kind: "a", value: n };
    }
    if (!SYMBOL.test(v)) return { kind: "error", msg: `"${v}" is not a number or a valid symbol` };
    return { kind: "a", symbol: v };
  }
  // dest=comp;jump
  let rest = s, dest = "000", jump = "000";
  const eq = rest.indexOf("=");
  if (eq >= 0) {
    const d = destBits(rest.slice(0, eq));
    if (!d) return { kind: "error", msg: `"${rest.slice(0, eq)}" is not a dest: use some of A, D, M` };
    dest = d;
    rest = rest.slice(eq + 1);
  }
  const semi = rest.indexOf(";");
  if (semi >= 0) {
    const j = JUMP[rest.slice(semi + 1)];
    if (!j) return { kind: "error", msg: `"${rest.slice(semi + 1)}" is not a jump: JGT JEQ JGE JLT JNE JLE JMP` };
    jump = j;
    rest = rest.slice(0, semi);
  }
  const comp = COMP[rest];
  if (!comp) {
    if (!rest) return { kind: "error", msg: "missing comp" };
    return { kind: "error", msg: `"${rest}" is not a comp` };
  }
  return { kind: "c", bits: "111" + comp + dest + jump };
}

export function assemble(source) {
  const pre = preprocess(source);
  const errors = [...pre.errors];
  const symbols = { ...PREDEFINED };
  const parsed = pre.lines.map(parseLine);

  // Pass 1: labels.
  let pc = 0;
  parsed.forEach((p, i) => {
    if (!p) return;
    if (p.kind === "error") { errors.push({ line: i + 1, msg: p.msg }); return; }
    if (p.kind === "label") {
      if (p.name in PREDEFINED) errors.push({ line: i + 1, msg: `"${p.name}" is predefined and cannot be a label` });
      else if (p.name in symbols) errors.push({ line: i + 1, msg: `label "${p.name}" is already defined` });
      else symbols[p.name] = pc;
      return;
    }
    pc++;
  });

  // Pass 2: the words, allocating variables in order of first use.
  const words = [], lineOf = [];
  let nextVar = VAR_BASE;
  parsed.forEach((p, i) => {
    if (!p || p.kind === "label" || p.kind === "error") return;
    let w;
    if (p.kind === "a") {
      if (p.symbol !== undefined) {
        if (!(p.symbol in symbols)) symbols[p.symbol] = nextVar++;
        w = symbols[p.symbol];
        if (w > A_MAX) errors.push({ line: i + 1, msg: `"${p.symbol}" is ${w}, too large for an A-instruction` });
        w &= 0x7fff;
      } else w = p.value;
    } else w = parseInt(p.bits, 2);
    words.push(w);
    lineOf.push(i + 1);
  });

  errors.sort((a, b) => a.line - b.line);
  return { words, errors, lineOf, symbols };
}

// A word as its 16 binary digits.
export const toBinary = (w) => w.toString(2).padStart(16, "0");
