// @vitest-environment jsdom
//
// The phone path, end to end on the client, with scripted detections
// (mission item A, 2026-11-01).
//
//   CameraMonitor (frame sampling) -> InferenceClient.detect (scripted here)
//   -> DetectionRules (debounce) -> EventQueue -> the ingest POST -> the
//   warning handed to the candidate's screen.
//
// WHY SCRIPTED. A browser model cannot be run deterministically in CI, and a
// real phone cannot be filmed in a test. What CAN be pinned is everything this
// repository owns between the model's output and the candidate's screen: given
// a sequence of per-frame detections, exactly which events leave the browser.
// The sequences below are the staged cases from the requirement (a held phone,
// a brief exposure, a half-seen phone, a model that keeps failing).
//
// WHAT THIS DOES NOT SHOW. It says nothing about the model's precision or
// recall on real video. Measuring that needs consented staged recordings, and
// the telemetry added in this change (`phone_best_score`, frame counts) exists
// so a staged session can produce those figures. Until then the thresholds in
// the server's config stand unchanged, and docs/spec/PROCTORING.md says so.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { IngestOut, WarningOut } from "./api";
import { CameraMonitor } from "./camera";
import type { ProctoringClientConfig } from "./config";
import { DetectionRules } from "./detections";
import type { EventDraft } from "./events";
import { EventQueue } from "./events";
import type { InferenceClient } from "./worker-client";
import type { CvDetectionsMessage } from "./workers/protocol";

const CONFIG = {
  object_confidence_threshold: 0.65,
  object_consecutive_frames: 3,
  obstruction_seconds: 60,
  obstruction_variance_threshold: 12,
  face_absent_moderate_seconds: 20,
  face_absent_extended_seconds: 90,
  low_light_luminance_threshold: 40,
  face_distance_threshold: 0.6,
  sampling_fps_normal: 2,
  sampling_fps_confirming: 6,
  confirming_window_seconds: 5,
  sampling_fps_degraded: 1,
  identity_check_interval_seconds: 30,
  heartbeat_interval_seconds: 10,
} as unknown as ProctoringClientConfig;

const NO_FRAME = Symbol("the model failed on this frame");
type Step = { label: string; score: number }[] | typeof NO_FRAME;

function detections(objects: { label: string; score: number }[]): CvDetectionsMessage {
  return {
    type: "detections",
    id: 1,
    objects,
    faces: 1,
    persons: 1,
    faceObservations: [],
    luminance: 120,
    variance: 45,
    inferenceMs: 40,
  };
}

function phone(score: number) {
  return [{ label: "cell phone", score }];
}

/** `count` frames of the same detections, as a script. */
function repeat(count: number, step: Step): Step[] {
  return Array.from({ length: count }, () => step);
}

function harness(script: Step[], options: { warning?: WarningOut } = {}) {
  const events: EventDraft[] = [];
  const posted: Array<Array<{ event_type: string; confidence: number | null }>> = [];
  const shown: WarningOut[] = [];
  let index = 0;
  const inference = {
    detect: vi.fn(async () => {
      const step = index < script.length ? script[index] : [];
      index += 1;
      if (step === NO_FRAME) throw new Error("model failed");
      return detections(step);
    }),
  } as unknown as InferenceClient;
  const queue = new EventQueue({
    batchMax: 200,
    maxBackoffMs: 10_000,
    post: async (batch) => {
      posted.push(batch.map((e) => ({ event_type: e.event_type, confidence: e.confidence ?? null })));
      return {
        accepted: batch.length,
        warnings_used: options.warning ? 1 : 0,
        max_warnings: 3,
        status: "active",
        warning: options.warning ?? null,
        termination: null,
      } satisfies IngestOut;
    },
    onWarning: (warning) => shown.push(warning),
    onTermination: () => undefined,
    onSessionEnded: () => undefined,
  });
  const monitor = new CameraMonitor({
    config: CONFIG,
    inference,
    rules: new DetectionRules(CONFIG),
    onEvent: (draft) => {
      events.push(draft);
      queue.enqueue(draft);
    },
    identityChecks: false,
    onLost: () => undefined,
    onRecovered: () => undefined,
  });
  return { monitor, events, posted, shown, queue, calls: () => index };
}

async function run(h: ReturnType<typeof harness>, script: Step[]) {
  const track = {
    readyState: "live",
    onended: null,
    stop: () => undefined,
    getSettings: () => ({ width: 640, height: 480 }),
  };
  const stream = {
    getVideoTracks: () => [track],
    getAudioTracks: () => [],
    getTracks: () => [track],
  } as unknown as MediaStream;
  await h.monitor.start(stream);
  // Long enough for the whole script at the slowest rate, plus the queue's tick.
  await vi.advanceTimersByTimeAsync(script.length * 1000 + 4000);
  h.monitor.stop();
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.spyOn(HTMLMediaElement.prototype, "play").mockResolvedValue(undefined);
  vi.spyOn(HTMLMediaElement.prototype, "readyState", "get").mockReturnValue(4);
  vi.stubGlobal("createImageBitmap", async () => ({ close: () => undefined }));
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("a phone the model sees clearly", () => {
  it("reaches the server once, and the candidate is shown the server's warning", async () => {
    const warning: WarningOut = {
      number: 1,
      max_warnings: 3,
      event_type: "DEVICE_DETECTED_PHONE",
      message: "A phone was detected on camera. Please move it out of view.",
      final: false,
    };
    const script = repeat(8, phone(0.9));
    const h = harness(script, { warning });
    await run(h, script);
    // Eight frames with the phone in view: ONE request for it, not eight.
    expect(h.events.map((e) => e.event_type)).toEqual(["DEVICE_DETECTED_PHONE"]);
    expect(h.events[0].confidence).toBe(0.9);
    expect(h.posted.flat().filter((e) => e.event_type === "DEVICE_DETECTED_PHONE")).toHaveLength(1);
    expect(h.shown).toEqual([warning]);
  });

  it("is reported again only after the phone has gone and come back", async () => {
    const script = [...repeat(4, phone(0.9)), ...repeat(2, []), ...repeat(4, phone(0.9))];
    const h = harness(script);
    await run(h, script);
    expect(h.events.map((e) => e.event_type)).toEqual([
      "DEVICE_DETECTED_PHONE",
      "DEVICE_DETECTED_PHONE",
    ]);
  });
});

describe("what must NOT become a phone event", () => {
  it("ignores a phone in view for fewer frames than the rule needs", async () => {
    const script = [...repeat(2, phone(0.95)), ...repeat(6, [])];
    const h = harness(script);
    await run(h, script);
    expect(h.events).toEqual([]);
  });

  it("ignores a half-seen phone below the confidence floor, but keeps its best score", async () => {
    const script = repeat(30, phone(0.6));
    const h = harness(script);
    await run(h, script);
    expect(h.events).toEqual([]);
    const stats = h.monitor.takeDetectionStats();
    expect(stats.phone_best_score).toBe(0.6);
    expect(stats.frames_processed).toBeGreaterThanOrEqual(30);
    expect(stats.frames_failed).toBe(0);
  });

  it("does not turn other objects into a phone", async () => {
    const script = repeat(10, [{ label: "cup", score: 0.99 }]);
    const h = harness(script);
    await run(h, script);
    expect(h.events).toEqual([]);
    expect(h.monitor.takeDetectionStats().phone_best_score).toBeNull();
  });
});

describe("a documented limit of strict debounce", () => {
  it("misses a phone whose confidence flickers across the floor every third frame", async () => {
    // Three consecutive frames above the floor are required, and one frame
    // below it restarts the count. A phone the model sees at 0.9, 0.9, 0.5,
    // 0.9, 0.9, 0.5 ... is in plain view and is never reported. This is pinned
    // as the CURRENT behaviour, not as the desired one: loosening the debounce
    // is a tuning change that needs measured recall and false-positive figures
    // from staged sessions (see docs/spec/PROCTORING.md), so it is not done
    // here on a guess. The telemetry reports the best score, which is how the
    // flicker would show up in the staged data.
    const flicker: Step[] = [];
    for (let i = 0; i < 6; i += 1) flicker.push(phone(0.9), phone(0.9), phone(0.5));
    const h = harness(flicker);
    await run(h, flicker);
    expect(h.events).toEqual([]);
    expect(h.monitor.takeDetectionStats().phone_best_score).toBe(0.9);
  });
});

describe("a model that fails", () => {
  it("is counted and then reported once, never read as a quiet camera", async () => {
    // One heartbeat interval's worth of frames is twenty at the normal rate.
    const script = [...repeat(2, []), ...repeat(25, NO_FRAME), ...repeat(2, [])];
    const h = harness(script);
    await run(h, script);
    const degraded = h.events.filter((e) => e.event_type === "SESSION_QUALITY_DEGRADED");
    expect(degraded).toHaveLength(1);
    expect(degraded[0].metadata).toMatchObject({ note: "camera_analysis_failing" });
    const stats = h.monitor.takeDetectionStats();
    expect(stats.frames_failed).toBe(25);
    expect(stats.frames_processed).toBeGreaterThanOrEqual(4);
  });

  it("does not report a handful of bad frames as an outage", async () => {
    const script = [...repeat(3, NO_FRAME), ...repeat(10, [])];
    const h = harness(script);
    await run(h, script);
    expect(h.events).toEqual([]);
    expect(h.monitor.takeDetectionStats().frames_failed).toBe(3);
  });
});
