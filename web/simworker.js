// The simulation's own thread: a SimHost (simhost.js) and the loop that
// runs it. The main thread starts this as a module worker and talks to it
// only through simhost.js's messages.
//
// The loop: while there is work, the next slice runs at once (a
// MessageChannel post, since setTimeout clamps to 4 ms and at 100k ticks a
// second that is a fifth of the time thrown away); at a slow rate, it sleeps
// until the next tick is due. Messages from the main thread -- a delta
// request, a pause, an edit -- are handled between slices, which last at
// most SLICE_MS.

import { installWebTypes } from "./webtypes.js";
import { SimHost } from "./simhost.js";

let host = null;
const early = [];             // messages that arrived before the types did

const chan = new MessageChannel();
let kicked = false, timer = null;
chan.port1.onmessage = () => { kicked = false; loop(); };

function schedule(wait) {
  if (timer !== null) { clearTimeout(timer); timer = null; }
  if (!host || !host.playing) return;
  if (wait > 0) timer = setTimeout(() => { timer = null; loop(); }, wait);
  else if (!kicked) { kicked = true; chan.port2.postMessage(0); }
}

function loop() {
  try {
    const wait = host.slice();
    if (wait !== null) schedule(wait);
  } catch (e) {
    self.postMessage({ type: "error", message: String(e?.stack || e) });
  }
}

function handle(m) {
  try {
    host.message(m);
  } catch (e) {
    self.postMessage({ type: "error", message: String(e?.stack || e) });
  }
  schedule(0);
}

self.onmessage = (e) => (host ? handle(e.data) : early.push(e.data));

fetch("types.json")
  .then(r => r.json())
  .then(t => {
    host = new SimHost(installWebTypes(t), (m, transfer) => self.postMessage(m, transfer || []));
    for (const m of early.splice(0)) handle(m);
    self.postMessage({ type: "ready" });
  })
  .catch(e => self.postMessage({ type: "error", message: `no types.json: ${e.message}` }));
