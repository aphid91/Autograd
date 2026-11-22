import { test } from "node:test";
import assert from "node:assert/strict";
import { presentationName, safeName, presentationRecord, speedRates, schedulePose } from "../present.js";

test("the hash names a presentation", () => {
  assert.equal(presentationName("#b=demo"), "demo");
  assert.equal(presentationName("#b=my%20show"), "my show");
  assert.equal(presentationName("#b="), null);
  assert.equal(presentationName(""), null);
  assert.equal(presentationName("#x=demo"), null);
  assert.equal(presentationName("#b=%E0%A4%A"), null);   // malformed escape
});

test("names are file-safe", () => {
  assert.equal(safeName(" CPU / demo! "), "CPU _ demo");
  assert.equal(safeName("../x"), "x");
  assert.equal(safeName(""), "");
});

const draft = { rate: "30", rates: ["", "0", "240", "5"], ticks: 600.4, lerpStart: 0, lerpEnd: 1, fadeIn: 0.25, fadeOut: 0.25,
  startCamera: { cx: 0, cy: 0, hw: 10, hh: 10 }, endCamera: { cx: 10, cy: 0, hw: 1, hh: 1 } };

test("the record drops unused rates and rounds the ticks", () => {
  const r = presentationRecord("demo", draft, [{ name: "default" }]);
  assert.equal(r.board, "demo.npz");
  assert.equal(r.rate, 30);
  assert.deepEqual(r.rates, [240, 5]);
  assert.equal(r.schedule.ticks, 600);
  assert.equal(presentationRecord("d", { ...draft, rate: "" }, []).rate, 30);
});

test("the speed panel lists the start rate and the others, once, in order", () => {
  assert.deepEqual(speedRates({ rate: 30, rates: [240, 5, 30] }), [5, 30, 240]);
  assert.deepEqual(speedRates({ rate: 30, rates: [] }), []);
  assert.deepEqual(speedRates({ rate: 30, rates: [30] }), []);   // nothing else: no panel
});

test("the schedule runs start to end over its ticks, and 0 ticks is none", () => {
  const r = presentationRecord("demo", draft, []);
  const a = schedulePose(r, 0), b = schedulePose(r, 600), c = schedulePose(r, 5000);
  assert.ok(Math.abs(a.cx) < 1e-9 && Math.abs(a.hw - 10) < 1e-9);
  assert.ok(Math.abs(b.cx - 10) < 1e-9 && Math.abs(b.hw - 1) < 1e-9);
  assert.deepEqual(c, b);   // holds the end
  const mid = schedulePose(r, 300);
  assert.ok(mid.cx > 0 && mid.cx < 10);
  assert.equal(schedulePose({ ...r, schedule: { ...r.schedule, ticks: 0 } }, 10), null);
  assert.equal(schedulePose({ ...r, endCamera: null }, 10), null);
});
