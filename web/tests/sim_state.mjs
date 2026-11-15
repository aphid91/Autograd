// The canonical one-line form of a simulation's state, shared with
// tools/check_web_sim.py, which builds the same line from the desktop's.
// Integers (ticks, counters, anchors) print as themselves; payloads and
// channel values are float32 and print as their bit pattern, so the
// comparison is exact rather than at the mercy of two languages' decimal
// formatting -- and a -0 against a 0 is a difference.

export function num(v) { return String(v); }

const f32 = new Float32Array(1), u32 = new Uint32Array(f32.buffer);
export function bits(v) { f32[0] = v; return u32[0].toString(16); }

export function stateLine(sim) {
  const S = sim.T.sim;
  const parts = [`t${sim.tick}`];
  for (const seg of sim.order) {
    const items = [];
    for (let k = seg.head; k < seg.tail; k++) {
      items.push(`${num(seg.enter[k % seg.cap])}:${bits(seg.payload[k % seg.cap])}`);
    }
    const [x, y] = seg.cells[0];
    parts.push(`s${seg.sid}@${x},${y}/${seg.length} ${seg.head}-${seg.tail} a${num(seg.anchor)} [${items.join(" ")}]`);
  }
  for (const g of sim.gates) {
    const ch = [];
    for (let c = 0; c < S.MAX_CHANNELS; c++) {
      ch.push(sim.inHeld(g.row, c) ? bits(sim.inValue(g.row, c)) : "_");
    }
    ch.push("|");
    for (let c = 0; c < S.MAX_CHANNELS; c++) {
      ch.push(sim.outHeld(g.row, c) ? bits(sim.outValue(g.row, c)) : "_");
    }
    parts.push(`g${g.row} ${ch.join(" ")}`);
  }
  return parts.join(" ; ");
}
