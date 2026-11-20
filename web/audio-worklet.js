// The audio thread: GatesSynth (audio-dsp.js) behind an AudioWorkletProcessor.
// Loaded by audio.js only when audio is switched on.

import { GatesSynth } from "./audio-dsp.js";

const METER_HZ = 10;

class GatesAudio extends AudioWorkletProcessor {
  constructor() {
    super();
    this.synth = new GatesSynth(sampleRate);
    this.since = 0;
    this.port.onmessage = (e) => this.synth.message(e.data);
  }
  process(_inputs, outputs) {
    const out = outputs[0], L = out[0];
    // A processor that throws is stopped for good, which reads as sound
    // that will not come back until it is switched off and on. So an error
    // costs this block (silence) and the queue, never the processor.
    try {
      this.synth.render(L, L.length);
    } catch (e) {
      L.fill(0);
      this.synth.stop();
      this.port.postMessage({ type: "error", message: String(e?.message || e) });
    }
    for (let c = 1; c < out.length; c++) out[c].set(L);
    if ((this.since += L.length) >= sampleRate / METER_HZ) {
      this.since = 0;
      this.port.postMessage(this.synth.meter());
    }
    return true;
  }
}

registerProcessor("gates-audio", GatesAudio);
