// Baking: the sound of a stretch of ticks rendered ahead of time, with no
// frame budget and no deadline -- every tick lands exactly once, evenly
// spaced, through the same synth (offline) and the same recorder realtime
// uses. Main-thread, in chunks, so the page stays responsive and the bake
// can be cancelled.
//
// It runs on a clone of the sim, so the live board is never touched. The
// caller hands it a sim at the bake's first tick (main.js resets first and
// keeps that state, so Reset can return to it exactly).

import { GatesSynth } from "./audio-dsp.js";
import { Recorder } from "./audio.js";

const CHUNK_MS = 25;   // work between yields

// Render the ticks [t0, t0 + seconds * rate) of `start` (a Sim, not
// modified) at `rate`. Resolves to { samples, sampleRate, t0, end, rate } --
// samples[0] is the start of tick t0 -- or null if `shouldStop()` turned
// true. t0 defaults to the start tick (the Bake dialog: audio from there);
// a video passes its own first tick, so the soundtrack starts and ends with
// the picture, silent before the start tick if that comes later.
export async function bakeAudio({ start, params, rate, seconds, sampleRate, onProgress, shouldStop, t0: from0, selection = null,
                                  space = null, spaceAt = null }) {
  const sim = start.clone();
  const t0 = from0 ?? Math.max(params.startTick, sim.tick);
  const end = t0 + Math.max(1, Math.round(seconds * rate));
  // The synth runs from a little before t0, silent, so a drone already
  // sounding at t0 fades in rather than starting cold.
  const warm = Math.ceil(Math.max(0.5, (params.attackMs + params.releaseMs) / 1000 + 0.05) * rate);
  const from = Math.max(sim.tick, t0 - warm);

  const synth = new GatesSynth(sampleRate, { offline: true });
  synth.message({ type: "params", p: { ...params } });
  const rec = new Recorder({ postMessage: (m) => synth.message(m) }, params, selection, space, spaceAt);
  const spt = sampleRate / rate;                     // samples per tick
  const total = Math.round((end - from) * spt);
  const out = new Float32Array(total);
  let rendered = 0;
  const renderTo = (upto) => {
    upto = Math.min(total, upto);
    while (rendered < upto) {
      const n = Math.min(128, upto - rendered);
      synth.render(out.subarray(rendered, rendered + n), n);
      rendered += n;
    }
  };
  const yieldNow = () => new Promise(r => setTimeout(r, 0));
  const progress = () => onProgress?.((sim.tick - start.tick) / Math.max(1, end - start.tick), sim.tick, end);

  // Silent run-up to the warm-up: ticks only.
  let last = performance.now();
  while (sim.tick < from) {
    sim.step();
    if (performance.now() - last > CHUNK_MS) {
      progress(); await yieldNow(); last = performance.now();
      if (shouldStop?.()) return null;
    }
  }
  rec.attach(sim);
  while (sim.tick < end) {
    sim.step();
    if (performance.now() - last > CHUNK_MS || sim.tick === end) {
      rec.flush(rate);
      // Never render into a tick that has not been fed: stop one short of
      // the boundary of the first unfed one.
      renderTo(sim.tick === end ? total : Math.floor((sim.tick - from - 1) * spt));
      progress(); await yieldNow(); last = performance.now();
      if (shouldStop?.()) return null;
    }
  }
  rec.detach();
  const skip = Math.round((t0 - from) * spt);
  return { samples: out.slice(skip), sampleRate, t0, end, rate };
}

// 16-bit PCM mono WAV.
export function wavBytes(samples, sampleRate) {
  const n = samples.length, buf = new ArrayBuffer(44 + n * 2), v = new DataView(buf);
  const str = (o, s) => { for (let i = 0; i < s.length; i++) v.setUint8(o + i, s.charCodeAt(i)); };
  str(0, "RIFF"); v.setUint32(4, 36 + n * 2, true); str(8, "WAVE");
  str(12, "fmt "); v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true);
  v.setUint32(24, sampleRate, true); v.setUint32(28, sampleRate * 2, true);
  v.setUint16(32, 2, true); v.setUint16(34, 16, true);
  str(36, "data"); v.setUint32(40, n * 2, true);
  for (let i = 0; i < n; i++) {
    const x = Math.max(-1, Math.min(1, samples[i]));
    v.setInt16(44 + i * 2, x < 0 ? x * 0x8000 : x * 0x7fff, true);
  }
  return buf;
}
