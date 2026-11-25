// Video export: a stretch of the simulation rendered offline into an MP4,
// after Fluoddity's recorder (src/recorder/recorder.ts), with mediabunny.
// Loaded on demand -- nothing here, and not mediabunny, costs anything
// until a recording starts.
//
// Offline: it runs on a clone of the sim (as a bake does) and draws each
// frame into its own canvas, so neither the frame budget nor the live
// board's playback touches it. Frame f is the state at tick
// t0 + f * tps / 60, fractional ticks drawn at that alpha, and is stamped
// f / 60 -- a computed time, never a measured one, so a frame that took a
// second to draw still lasts 1/60 s.
//
// The encoder's add() resolves when it is ready for more: awaiting it is
// the backpressure that keeps a fast stretch from queueing frames until
// the tab is killed. Do not drop the await.

import { VIDEO_FPS, frameCount, frameBox } from "./video-params.js";
import { Camera } from "./camera.js";
import { frameSampleTimes } from "./mblur.js";

const CODECS = ["avc", "vp9", "av1"];        // best-supported first (Fluoddity's order)
const AUDIO_CODECS = ["aac", "opus"];
const YIELD_MS = 30;                          // drawing between yields to the page

// Record `sim` (a clone the caller made, at the tick the video starts on;
// it is stepped and not restored). `draw(ctx, sim, cam, alpha)` renders one
// frame as main.js would; `blur`, if given (View > look > motion blur above
// 1), is { k, begin(ctx), sample(ctx, sim, cam, alpha), finish(ctx, sim,
// cam, alpha) }: each frame is then k samples ending on the frame's own tick
// (mblur.js frameSampleTimes), each with the camera at its own fraction of
// the move, averaged and finished in place of `draw`; `audio`, if given, is { params, bake } for the
// soundtrack (`bake` is audio-bake.js's bakeAudio). `writable` is a file to
// stream into, or null to buffer in memory. Resolves to
// { kind: "buffered", blob } | { kind: "streamed" } | { kind: "empty" }.
export async function recordVideo({ sim, settings, startCam, endCam, draw, blur = null, audio, writable,
                                    onProgress, shouldStop }) {
  const W = settings.width, H = settings.height;
  const frames = frameCount(settings), tps = settings.tps, t0 = sim.tick;
  const yieldNow = () => new Promise(r => setTimeout(r, 0));

  // The soundtrack first: the same ticks, baked with no deadline.
  let sound = null;
  if (audio) {
    sound = await audio.bake({
      start: sim, params: audio.params, rate: tps, seconds: settings.duration, sampleRate: 48000, t0, selection: audio.selection,
      space: audio.space ?? null, spaceAt: audio.spaceAt ?? null,
      onProgress: (f) => onProgress?.({ phase: "audio", fraction: f }), shouldStop,
    });
    if (!sound) { await writable?.abort?.().catch(() => {}); return { kind: "empty" }; }
  }

  const MB = await import("./vendor/mediabunny-1.55.1.min.mjs");
  let codec = null;
  for (const c of CODECS) if (await MB.canEncodeVideo(c, { width: W, height: H })) { codec = c; break; }
  if (!codec) throw new Error(`this browser cannot encode ${W}×${H} video; try a smaller size or a Chromium browser`);
  let acodec = null;
  if (sound) for (const c of AUDIO_CODECS) if (await MB.canEncodeAudio(c, { numberOfChannels: 1, sampleRate: sound.sampleRate })) { acodec = c; break; }

  // Streaming to a chosen file keeps memory flat (metadata at the end);
  // buffering puts it at the front so the download seeks at once.
  const output = writable
    ? new MB.Output({ format: new MB.Mp4OutputFormat({ fastStart: false }), target: new MB.StreamTarget(writable) })
    : new MB.Output({ format: new MB.Mp4OutputFormat({ fastStart: "in-memory" }), target: new MB.BufferTarget() });
  const quality = MB[`QUALITY_${settings.quality.toUpperCase().replace("-", "_")}`] ?? MB.QUALITY_HIGH;

  const canvas = document.createElement("canvas");
  canvas.width = W; canvas.height = H;
  const ctx = canvas.getContext("2d");
  const video = new MB.CanvasSource(canvas, { codec, quality, latencyMode: "quality" });
  output.addVideoTrack(video, { frameRate: VIDEO_FPS });
  let track = null;
  if (sound && acodec) {
    track = new MB.AudioBufferSource({ codec: acodec, quality });
    output.addAudioTrack(track);
  }
  await output.start();

  // The soundtrack goes in a second at a time beside the frames, so the
  // muxer can interleave as it goes rather than hold it all.
  let audioDone = 0;
  const feedAudio = async (upToSec) => {
    if (!track) return;
    const end = Math.min(sound.samples.length, Math.round(upToSec * sound.sampleRate));
    while (audioDone < end) {
      const n = Math.min(sound.sampleRate, end - audioDone);
      const buf = new AudioBuffer({ numberOfChannels: 1, length: n, sampleRate: sound.sampleRate });
      buf.copyToChannel(sound.samples.subarray(audioDone, audioDone + n), 0);
      await track.add(buf);
      audioDone += n;
    }
  };

  const cam = new Camera();
  cam.resize(W, H);
  let done = 0, last = performance.now();
  try {
    for (let f = 0; f < frames; f++) {
      if (shouldStop?.()) break;             // keep what is encoded: a partial video is still a video
      const at = t0 + (f * tps) / VIDEO_FPS;
      // The camera at frame ff (fractional for blur samples), from the move.
      const aim = (ff) => {
        const box = frameBox(startCam, endCam, settings, ff, frames);
        cam.cx = box.cx; cam.cy = box.cy; cam.zoom = W / (2 * box.hw);
      };
      if (blur && blur.k > 1) {
        // k samples over the frame's span of ticks, the last on the frame
        // itself; none before the first frame (the video starts at t0).
        blur.begin(ctx);
        for (const t of frameSampleTimes(at, tps / VIDEO_FPS, blur.k, t0)) {
          while (sim.tick < Math.floor(t)) sim.step();
          aim(Math.max(0, (t - t0) * VIDEO_FPS / tps));
          blur.sample(ctx, sim, cam, t - sim.tick);
        }
        aim(f);
        blur.finish(ctx, sim, cam, at - sim.tick);
      } else {
        while (sim.tick < Math.floor(at)) sim.step();
        aim(f);
        draw(ctx, sim, cam, at - sim.tick);
      }
      await video.add(f / VIDEO_FPS, 1 / VIDEO_FPS);
      done = f + 1;
      await feedAudio((f + 1) / VIDEO_FPS);
      if (performance.now() - last > YIELD_MS) {
        onProgress?.({ phase: "video", fraction: done / frames, frame: done, frames });
        await yieldNow();
        last = performance.now();
      }
    }
    if (done === 0) {
      await writable?.abort?.().catch(() => {});
      return { kind: "empty" };
    }
    video.close();
    track?.close();
    await output.finalize();
  } catch (e) {
    await output.cancel?.().catch(() => {});
    throw e;
  }
  // finalize() has closed a streamed file itself (closing again throws).
  if (writable) return { kind: "streamed", frames: done };
  const buffer = output.target.buffer;
  return buffer ? { kind: "buffered", blob: new Blob([buffer], { type: "video/mp4" }), frames: done }
                : { kind: "empty" };
}
