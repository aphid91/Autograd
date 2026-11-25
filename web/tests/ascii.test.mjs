// ASCII sources and the character rules (ascii.js): what a value means as a
// character, what the value box reads, and the Source's ASCII flag riding
// the row through capture, placement, a save and the simulation.

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { Board, F_ASCII } from "../board.js";
import { Sim } from "../sim.js";
import { isAscii, isPrintable, glyphOf, signedGlyph, NULL_GLYPH, parseValue, valueText, pushRecent, keyCode, nameOf, textDiff } from "../ascii.js";

const here = dirname(fileURLToPath(import.meta.url));
const root = join(here, "..", "..");
const T = JSON.parse(readFileSync(join(here, "..", "types.json"), "utf8"));
const buf = (b) => b.buffer.slice(b.byteOffset, b.byteOffset + b.byteLength);
const load = async (save) => Board.fromNpz(T, buf(readFileSync(join(root, save))));

test("a value is a character by its whole part, 0..127", () => {
  assert.ok(isAscii(65) && isAscii(65.9) && isAscii(0) && isAscii(127));
  assert.ok(!isAscii(-1) && !isAscii(-0.5) && !isAscii(128) && !isAscii(NaN) && !isAscii(Infinity));
  assert.ok(isPrintable(32) && isPrintable(126) && !isPrintable(31) && !isPrintable(127));
  assert.equal(glyphOf(61), "=");
  assert.equal(glyphOf(61.75), "=");
  assert.equal(glyphOf(32), "␣", "space shows as an open box");
  assert.equal(glyphOf(10), "␊", "control codes as their pictures");
  assert.equal(glyphOf(127), "␡");
  assert.equal(glyphOf(-78.3584), null, "not ASCII: the null glyph");
  assert.equal(NULL_GLYPH, "□");
  assert.equal(nameOf(10), "\\n");
  assert.equal(nameOf(65), "'A'");
});

test("the value box: a character on an ASCII source, a number elsewhere", () => {
  // ASCII source: one character is that character, longer is a number.
  assert.equal(parseValue("=", true), 61);
  assert.equal(parseValue("5", true), 53);
  assert.equal(parseValue("53", true), 53);
  assert.equal(parseValue("-1", true), -49, "minus the character '1' (a negated character beats a number)");
  assert.equal(parseValue(" ", true), 32, "a lone space is a space");
  // Plain Source: numbers are numbers, a lone non-number its code.
  assert.equal(parseValue("5", false), 5);
  assert.equal(parseValue("=", false), 61);
  assert.equal(parseValue("2,5", false), 2.5);
  assert.equal(parseValue("abc", false), null);
  assert.equal(parseValue("", false), null);
  // "-" and a character: that character negated.
  assert.equal(parseValue("-F", true), -70);
  assert.equal(parseValue("-5", true), -53, "on an ASCII source, as 5 is 53");
  assert.equal(parseValue("- ", true), -32, "minus a space");
  assert.equal(parseValue("-", true), 45, "a lone minus is the minus sign");
  assert.equal(parseValue("-12", true), -12, "longer is still a number");
  assert.equal(parseValue("-=", false), -61, "on a plain Source too");
  assert.equal(parseValue("-5", false), -5, "where a number stays a number");
  assert.equal(signedGlyph(-70), "-F");
  assert.equal(signedGlyph(-70.5), "-F", "by the whole part of the magnitude");
  assert.equal(signedGlyph(-200), null, "not a negated character: the null glyph");
  assert.equal(valueText(-70, true), "-F");
  assert.equal(valueText(-10, true), "-\\n");
  assert.equal(valueText(-70, false), "-70");
  // Shown back: the character on an ASCII source, the number otherwise.
  assert.equal(valueText(61, true), "=");
  assert.equal(valueText(61, false), "61");
  assert.equal(valueText(10, true), "\\n");
  assert.equal(valueText(-3.5, true), "-3.5");
});

test("recent values: distinct, most recent first, six of them", () => {
  let r = [32, 65, 48, 10, 46, 45];
  r = pushRecent(r, 61);
  assert.deepEqual(r, [61, 32, 65, 48, 10, 46]);
  r = pushRecent(r, 48);
  assert.deepEqual(r, [48, 61, 32, 65, 10, 46], "a repeat moves to the front, not in twice");
});

test("keys and phone input as codes", () => {
  assert.equal(keyCode({ key: "a" }), 97);
  assert.equal(keyCode({ key: "Enter" }), 10);
  assert.equal(keyCode({ key: "Backspace" }), 8);
  assert.equal(keyCode({ key: " " }), 32);
  assert.equal(keyCode({ key: "Tab" }), 9);
  assert.equal(keyCode({ key: "ArrowLeft" }), null);
  assert.equal(keyCode({ key: "Shift" }), null);
  assert.equal(keyCode({ key: "c", ctrlKey: true }), 3);
  // A phone keyboard reports no keys: the field's text before and after.
  assert.deepEqual(textDiff("    ", "    h"), [104]);
  assert.deepEqual(textDiff("    ", "   "), [8], "a backspace");
  assert.deepEqual(textDiff("    teh", "    the"), [8, 8, 104, 101], "an autocorrect: back to where it differs, then the new text");
  assert.deepEqual(textDiff("    hi", "    hi\n"), [10]);
  assert.deepEqual(textDiff("    ", "    "), []);
});

test("the ASCII flag rides capture, placement and a save, and never the sim", async () => {
  const board = await load("cpu.npz");
  const s = board.structs.find(x => x.emit !== null);
  const row = s.row;
  const rev = board.revision;
  assert.equal(board.setAscii(row, true), true);
  assert.equal(board.revision, rev, "converting is not a board edit: the sim runs on");
  assert.equal(board.styleRev, 1, "but it is an undo step");
  assert.equal(board.structs.find(x => x.row === row).ascii, true, "the render list follows at once");
  assert.equal(board.describe(row).ascii, true);
  assert.equal(board.setAscii(row, true), false, "setting what it is is no change");

  // Capture and put back: the flag travels.
  const [e] = board.capture([row]);
  assert.equal(e.ascii, true);
  board.eraseRows([row]);
  board.placeEntries([e], [[e.dx, e.dy]]);
  const back = board.structs.find(x => x.x === e.dx && x.y === e.dy);
  assert.equal(back.ascii, true);
  assert.equal(board.field(back.row, F_ASCII), 1);

  // A save and a load.
  const again = await Board.fromNpz(T, buf(new Uint8Array(await board.toNpz())));
  assert.equal(again.structs.find(x => x.x === e.dx && x.y === e.dy).ascii, true);

  // The sim cannot tell: the same ticks with the flag on every Source or off.
  const plain = await load("cpu.npz"), flagged = await load("cpu.npz");
  for (const x of flagged.structs) if (x.emit !== null) flagged.setAscii(x.row, true);
  const a = new Sim(T, plain), b = new Sim(T, flagged);
  for (let t = 0; t < 60; t++) { a.step(); b.step(); }
  const gate = (bd) => bd.structs.map(x => Array.from(bd.entities.subarray(x.row * T.entity.STRIDE + 8, x.row * T.entity.STRIDE + 24)).join());
  assert.deepEqual(gate(flagged), gate(plain));
  assert.equal(b.stats.items ?? 0, a.stats.items ?? 0);
});

test("the flag survives the worker's deltas, which copy the gate block whole", async () => {
  // The slot is outside F_IN_VALUE .. F_IN_VALUE + 4 * MAX_CHANNELS.
  assert.ok(F_ASCII >= T.sim.F_IN_VALUE + 4 * T.sim.MAX_CHANNELS);
  assert.ok(F_ASCII < T.entity.STRIDE);
});
