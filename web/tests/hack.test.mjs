// The Hack assembler (hack.js) and its preprocessor.

import { test } from "node:test";
import assert from "node:assert/strict";
import { assemble, preprocess, toBinary, COMP, PREDEFINED } from "../hack.js";


const bin = (src) => {
  const r = assemble(src);
  assert.deepEqual(r.errors, []);
  return r.words.map(toBinary);
};

test("Max.asm assembles to the book's Max.hack, with this ALU's comp bits", () => {
  const src = `// Computes R2 = max(R0, R1)
   @R0
   D=M              // D = first number
   @R1
   D=D-M            // D = first number - second number
   @OUTPUT_FIRST
   D;JGT            // if D>0 (first is greater) goto output_first
   @R1
   D=M              // D = second number
   @OUTPUT_D
   0;JMP            // goto output_d
(OUTPUT_FIRST)
   @R0
   D=M              // D = first number
(OUTPUT_D)
   @R2
   M=D              // M[2] = D (greatest number)
(INFINITE_LOOP)
   @INFINITE_LOOP
   0;JMP            // infinite loop`;
  assert.deepEqual(bin(src), [
    "0000000000000000", "1111100010010000", "0000000000000001", "1111010011010000",
    "0000000000001010", "1110001010000001", "0000000000000001", "1111100010010000",
    "0000000000001100", "1110101010000111", "0000000000000000", "1111100010010000",
    "0000000000000010", "1110001010001000", "0000000000001110", "1110101010000111",
  ]);
});

test("the comp table has all 28 forms, plus the commuted ones", () => {
  const canon = ["0", "1", "-1", "D", "A", "!D", "!A", "-D", "-A", "D+1", "A+1", "D-1", "A-1", "D+A", "D-A", "A-D", "D&A", "D|A",
    "M", "!M", "-M", "M+1", "M-1", "D+M", "D-M", "M-D", "D&M", "D|M"];
  for (const c of canon) assert.ok(COMP[c], c);
  assert.equal(COMP["A+D"], COMP["D+A"]);
  assert.equal(COMP["M|D"], COMP["D|M"]);
  assert.equal(COMP["D+M"], "1000010");
  assert.equal(COMP["!M"], "1100011");
});

test("the comp bits are this project's ALU, not the book's", () => {
  const want = {
    "0": "101010", "1": "101001", "-1": "111111", "D": "001010", "A": "100010",
    "!D": "001011", "!A": "100011", "-D": "001111", "-A": "110011",
    "D+1": "001110", "A+1": "110010", "D-1": "011111", "A-1": "110111",
    "D+A": "000010", "D-A": "010011", "A-D": "000111", "D&A": "000000", "D|A": "010101",
  };
  for (const [k, c] of Object.entries(want)) {
    assert.equal(COMP[k], "0" + c, k);
    if (k.includes("A")) assert.equal(COMP[k.replace("A", "M")], "1" + c, k.replace("A", "M"));
  }
  const codes = Object.values(want);
  assert.equal(new Set(codes).size, codes.length, "two comps share a code");
});

test("words are unsigned: a C-instruction is 57344 or more", () => {
  const r = assemble("D=M\n@15\n0;JMP");
  assert.deepEqual(r.words, [0b1111100010010000, 15, 0b1110101010000111]);
  assert.ok(r.words[0] >= 57344);
});

test("dest letters in any order, and every jump", () => {
  assert.deepEqual(bin("MD=1\nDM=1\nAMD=0"), ["1110101001011000", "1110101001011000", "1110101010111000"]);
  assert.equal(bin("0;JLE")[0].slice(13), "110");
});

test("variables from 16 in order of first use; labels resolve forward", () => {
  const r = assemble("@i\n@j\n@i\n@END\n(END)\n@SCREEN\n@KBD\n@THAT");
  assert.deepEqual(r.words, [16, 17, 16, 4, 16384, 24576, 4]);
  assert.equal(PREDEFINED.R15, 15);
});

test("#define replaces plain text, in order, across the whole file", () => {
  const r = assemble("#define gg @\n#define two gg2\ngg100\ntwo\nD=M // gg in a comment is harmless");
  assert.deepEqual(r.errors, []);
  assert.deepEqual(r.words.slice(0, 2), [100, 2]);
  // A define below its use still applies.
  assert.deepEqual(assemble("xx5\n#define xx @").words, [5]);
  // Directive lines keep their line numbers.
  assert.equal(preprocess("#define a b\na").lines[1], "b");
});

test("errors carry their line, and the rest still parses", () => {
  const r = assemble("@5\nD=Q\n@99999\n(LOOP)\n(LOOP)\n0;JMPP\n(SP)\n#include x\n@3x\nX=1");
  const lines = r.errors.map(e => e.line);
  assert.deepEqual(lines, [2, 3, 5, 6, 7, 8, 9, 10]);
  assert.match(r.errors[1].msg, /too large/);
  assert.equal(r.words[0], 5);
});
