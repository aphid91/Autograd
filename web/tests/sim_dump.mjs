// Run the web simulation on a save and print its state after every tick, one
// line per tick, in the canonical form tools/check_web_sim.py compares
// against the desktop's. Not a test on its own -- the Python tool drives it.
//
//   node web/tests/sim_dump.mjs SAVE.npz TICKS [RESET_AT]
//
// RESET_AT, if given, does a Reset (ctrl+R) just before that tick, which
// re-derives the index and so exercises the segment id allocator's
// alternation.

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { Board } from "../board.js";
import { Sim } from "../sim.js";
import { stateLine } from "./sim_state.mjs";

const here = dirname(fileURLToPath(import.meta.url));
const T = JSON.parse(readFileSync(join(here, "..", "types.json"), "utf8"));
const [path, ticksArg, resetArg] = process.argv.slice(2);
const bytes = readFileSync(path);
const board = await Board.fromNpz(T, bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength));
const sim = new Sim(T, board);
const ticks = Number(ticksArg);
const resetAt = resetArg === undefined ? -1 : Number(resetArg);

const out = [stateLine(sim)];
for (let t = 0; t < ticks; t++) {
  if (t === resetAt) sim.reset();
  sim.step();
  out.push(stateLine(sim));
}
process.stdout.write(out.join("\n") + "\n");
