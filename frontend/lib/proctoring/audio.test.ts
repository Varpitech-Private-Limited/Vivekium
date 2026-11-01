// The microphone monitor (proctoring spec 3.4; Phase 3, 2026-09-24).
//
// Two things are pinned. A chunk in which speech was heard is uploaded, and
// the audio is ALL that is uploaded: whether that speech overlapped a spoken
// answer is the server's judgement against its own stamps, so no time this
// device measured travels with it. And a lost microphone is reported, retried
// and handed back rather than ending the monitor, which is what lets the
// session pause instead of stop.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { AudioChunkOut } from "./api";
import { setVoiceRecordingActive } from "@/lib/assessment/voice-activity";

import { AudioMonitor } from "./audio";
import { DEVICE_RETRY_MS, type LossKind } from "./device-watch";
import type { EventDraft } from "./events";

class FakeTrack {
  readyState: "live" | "ended" = "live";
  onended: (() => void) | null = null;
  stop(): void {
    this.readyState = "ended";
  }
  die(): void {
    this.readyState = "ended";
    this.onended?.();
  }
}

function fakeStream() {
  const track = new FakeTrack();
  const stream = {
    getAudioTracks: () => [track],
    getVideoTracks: () => [],
    getTracks: () => [track],
  } as unknown as MediaStream;
  return { stream, track };
}

/** Loud and quiet in turn, so every chunk "heard speech". */
class FakeAnalyser {
  fftSize = 4;
  private loud = false;
  getFloatTimeDomainData(buffer: Float32Array): void {
    this.loud = !this.loud;
    buffer.fill(this.loud ? 0.5 : 0.01);
  }
}

class FakeAudioContext {
  createAnalyser(): FakeAnalyser {
    return new FakeAnalyser();
  }
  createMediaStreamSource(): { connect: () => void } {
    return { connect: () => undefined };
  }
  close(): Promise<void> {
    return Promise.resolve();
  }
}

class FakeMediaRecorder {
  static isTypeSupported(): boolean {
    return true;
  }
  state = "inactive";
  ondataavailable: ((event: { data: Blob }) => void) | null = null;
  onstop: (() => void) | null = null;
  start(): void {
    this.state = "recording";
  }
  stop(): void {
    this.state = "inactive";
    this.ondataavailable?.({ data: new Blob(["speech"]) });
    this.onstop?.();
  }
}

const CHUNK_SECONDS = 15;
const START = Date.UTC(2026, 8, 24, 10, 0, 0);

function build(uploadEnabled: boolean) {
  const getUserMedia = vi.fn<() => Promise<MediaStream>>();
  const uploads: unknown[][] = [];
  const lost: LossKind[] = [];
  const recovered: MediaStream[] = [];
  const emitted: EventDraft[] = [];
  const advisories: string[] = [];
  const monitor = new AudioMonitor({
    chunkSeconds: CHUNK_SECONDS,
    maxChunkBytes: 1_000_000,
    uploadEnabled,
    upload: (...args: unknown[]) => {
      uploads.push(args);
      return Promise.resolve({
        analysed: true,
        status: "active",
        warnings_used: 0,
        warning: null,
        termination: null,
      } satisfies AudioChunkOut);
    },
    sound: {
      loud_threshold_dbfs: -20,
      loud_margin_db: 25,
      loud_hysteresis_db: 6,
      loud_sustained_seconds: 6,
      loud_cooldown_seconds: 120,
      loud_calibration_seconds: 10,
    },
    emit: (draft) => emitted.push(draft),
    onAdvisory: (message) => advisories.push(message),
    onLost: (kind) => lost.push(kind),
    onRecovered: (stream) => recovered.push(stream),
    onWarning: () => undefined,
    onTermination: () => undefined,
    onSessionEnded: () => undefined,
    navigator: { mediaDevices: { getUserMedia } } as unknown as Navigator,
  });
  return { monitor, getUserMedia, uploads, lost, recovered, emitted, advisories };
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(START);
  vi.stubGlobal("AudioContext", FakeAudioContext);
  vi.stubGlobal("MediaRecorder", FakeMediaRecorder);
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("the audio chunk", () => {
  it("is uploaded when speech was heard, carrying the audio and nothing else", async () => {
    const { monitor, uploads } = build(true);
    const { stream } = fakeStream();
    await monitor.start(stream);
    await vi.advanceTimersByTimeAsync(CHUNK_SECONDS * 1000);
    expect(uploads).toHaveLength(1);
    expect(uploads[0]).toHaveLength(1);
    expect(uploads[0][0]).toBeInstanceOf(Blob);
    await vi.advanceTimersByTimeAsync(CHUNK_SECONDS * 1000);
    expect(uploads).toHaveLength(2);
    monitor.stop();
  });

  it("is never uploaded when the deployment cannot analyse it", async () => {
    const { monitor, uploads } = build(false);
    const { stream } = fakeStream();
    await monitor.start(stream);
    await vi.advanceTimersByTimeAsync(CHUNK_SECONDS * 1000 * 2);
    expect(uploads).toEqual([]);
    monitor.stop();
  });
});

describe("a lost microphone", () => {
  it("is reported, retried on its own, and handed back", async () => {
    const { monitor, getUserMedia, lost, recovered } = build(false);
    const first = fakeStream();
    await monitor.start(first.stream);
    getUserMedia.mockRejectedValue(new DOMException("gone", "NotFoundError"));
    first.track.die();
    expect(lost).toEqual(["stream"]);
    expect(monitor.live()).toBe(false);
    expect(monitor.currentStream()).toBeNull();
    await vi.advanceTimersByTimeAsync(DEVICE_RETRY_MS * 2);
    expect(getUserMedia).toHaveBeenCalledTimes(2);
    const back = fakeStream();
    getUserMedia.mockResolvedValue(back.stream);
    await vi.advanceTimersByTimeAsync(DEVICE_RETRY_MS);
    expect(recovered).toEqual([back.stream]);
    expect(monitor.live()).toBe(true);
    monitor.stop();
  });

  it("is not retried on a timer once the permission is refused", async () => {
    const { monitor, getUserMedia, lost } = build(false);
    const first = fakeStream();
    await monitor.start(first.stream);
    getUserMedia.mockRejectedValue(new DOMException("no", "NotAllowedError"));
    first.track.die();
    await vi.advanceTimersByTimeAsync(DEVICE_RETRY_MS);
    expect(lost).toEqual(["stream", "permission"]);
    const calls = getUserMedia.mock.calls.length;
    await vi.advanceTimersByTimeAsync(DEVICE_RETRY_MS * 10);
    expect(getUserMedia).toHaveBeenCalledTimes(calls);
    monitor.stop();
  });

  it("drops the chunk it was capturing rather than send audio from a dead track", async () => {
    const { monitor, uploads, getUserMedia } = build(true);
    const first = fakeStream();
    await monitor.start(first.stream);
    getUserMedia.mockRejectedValue(new DOMException("gone", "NotFoundError"));
    await vi.advanceTimersByTimeAsync(5000);
    first.track.die();
    await vi.advanceTimersByTimeAsync(CHUNK_SECONDS * 1000);
    expect(uploads).toEqual([]);
    monitor.stop();
  });
});

// ── Sustained high sound level (mission item B, 2026-11-01) ───────────────────

/** An analyser whose level the test sets, so a stretch of shouting is exact. */
let level = 0.001;
class SteadyAnalyser {
  fftSize = 4;
  frequencyBinCount = 8;
  getFloatTimeDomainData(buffer: Float32Array): void {
    buffer.fill(level);
  }
  getFloatFrequencyData(bins: Float32Array): void {
    bins.fill(-40);
  }
}
class SteadyAudioContext extends FakeAudioContext {
  sampleRate = 48_000;
  createAnalyser(): FakeAnalyser {
    return new SteadyAnalyser() as unknown as FakeAnalyser;
  }
}

async function runLevels(
  monitor: AudioMonitor,
  stream: MediaStream,
  steps: Array<{ seconds: number; rms: number }>
) {
  await monitor.start(stream);
  for (const step of steps) {
    level = step.rms;
    await vi.advanceTimersByTimeAsync(step.seconds * 1000);
  }
}

describe("the sound-level advisory", () => {
  beforeEach(() => {
    vi.stubGlobal("AudioContext", SteadyAudioContext);
    setVoiceRecordingActive(false);
    level = 0.001;
  });
  afterEach(() => setVoiceRecordingActive(false));

  it("is raised as an event and a reminder after calibration and a sustained loud stretch", async () => {
    // Without an analysis service: the advisory is local and needs none.
    const { monitor, emitted, advisories } = build(false);
    await runLevels(monitor, fakeStream().stream, [
      { seconds: 11, rms: 0.001 },
      { seconds: 9, rms: 0.3 },
    ]);
    monitor.stop();
    expect(emitted.map((e) => e.event_type)).toEqual(["SUSTAINED_HIGH_SOUND_LEVEL"]);
    expect(emitted[0].metadata).toMatchObject({ character: "broadband" });
    expect(advisories).toEqual(["Sustained high sound level detected; please check your environment."]);
  });

  it("is not raised while a spoken answer is being captured", async () => {
    const { monitor, emitted, advisories } = build(false);
    await runLevels(monitor, fakeStream().stream, [
      { seconds: 11, rms: 0.001 },
      { seconds: 1, rms: 0.001 },
    ]);
    setVoiceRecordingActive(true);
    level = 0.3;
    await vi.advanceTimersByTimeAsync(20_000);
    monitor.stop();
    expect(emitted).toEqual([]);
    expect(advisories).toEqual([]);
  });

  it("resumes judging once the spoken answer is over", async () => {
    const { monitor, emitted } = build(false);
    await runLevels(monitor, fakeStream().stream, [{ seconds: 11, rms: 0.001 }]);
    setVoiceRecordingActive(true);
    level = 0.3;
    await vi.advanceTimersByTimeAsync(10_000);
    setVoiceRecordingActive(false);
    await vi.advanceTimersByTimeAsync(9_000);
    monitor.stop();
    expect(emitted.map((e) => e.event_type)).toEqual(["SUSTAINED_HIGH_SOUND_LEVEL"]);
  });

  it("does not judge a muted microphone", async () => {
    const { monitor, emitted } = build(false);
    const { stream, track } = fakeStream();
    await runLevels(monitor, stream, [{ seconds: 11, rms: 0.001 }]);
    (track as unknown as { muted: boolean }).muted = true;
    level = 0.3;
    await vi.advanceTimersByTimeAsync(20_000);
    monitor.stop();
    expect(emitted).toEqual([]);
  });
});
