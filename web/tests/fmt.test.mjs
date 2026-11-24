import { test } from "node:test";
import assert from "node:assert/strict";
import { fmtNumber } from "../render.js";

test("whole numbers print in full up to 2^24, then go scientific", () => {
  assert.equal(fmtNumber(0), "0");
  assert.equal(fmtNumber(-42), "-42");
  assert.equal(fmtNumber(16777216), "16777216");
  assert.equal(fmtNumber(-16777216), "-16777216");
  assert.equal(fmtNumber(16777218), "1.7e+7");
  assert.equal(fmtNumber(2 ** 30), "1.1e+9");
  assert.equal(fmtNumber(-1e30), "-1.0e+30");
});

test("fractions keep their rules", () => {
  assert.equal(fmtNumber(2.5), "2.5");
  assert.equal(fmtNumber(1 / 3), "0.333");
  assert.equal(fmtNumber(12345.5), "1.2e+4");
  assert.equal(fmtNumber(0.0001), "1.0e-4");
});
