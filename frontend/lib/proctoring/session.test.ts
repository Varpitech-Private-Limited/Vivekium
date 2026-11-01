// @vitest-environment jsdom
//
// The running session's wiring (Phase 3, 2026-09-24).
//
// Every detector here is a double; what is exercised is what the runtime does
// with their answers, because that is where the device pause lives:
//
//   - a device the pause owns must not ALSO be an integrity failure, or the
//     self-check ends the session a minute into a two-minute grace;
//   - the grace deadline must be the server's, moved onto this device's clock;
//   - a candidate who reloads during a pause must be able to lift it;
//   - a recovery must start a new recording segment on the NEW tracks;
//   - one blocked paste must be one event and one count, whichever layer saw it.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "@/lib/api";

import type { EventIn, HeartbeatOut, IngestOut, SessionOut } from "./api";
import type { ProctoringClientConfig } from "./config";
import type { LossKind } from "./device-watch";

type Handler<T> = ((value: T) => void) | null;

const hoisted = vi.hoisted(() => ({
  postEvents: vi.fn(),
  acknowledgeWarning: vi.fn(),
  postAudioChunk: vi.fn(),
  camera: {
    options: null as null | { onLost: (kind: LossKind) => void; onRecovered: (stream: MediaStream) => void },
    live: true,
    stream: null as MediaStream | null,
    reacquire: vi.fn(),
  },
  audio: {
    options: null as null | {
      onLost: (kind: LossKind) => void;
      onRecovered: (stream: MediaStream) => void;
      upload: (...args: unknown[]) => unknown;
    },
    live: true,
    stream: null as MediaStream | null,
    reacquire: vi.fn(),
  },
  recorder: { start: vi.fn(), restart: vi.fn(), stop: vi.fn() },
  lockdown: { onBlocked: null as Handler<string> | ((action: string, via?: string) => void) },
  heartbeat: {
    onResult: null as null | ((result: HeartbeatOut, sentAt: number, receivedAt: number) => void),
    monitoring: null as null | (() => unknown),
    beat: vi.fn(),
  },
  integrity: { probe: null as null | (() => { camera: boolean; microphone: boolean }), check: vi.fn() },
}));

vi.mock("./api", async () => {
  const actual = await vi.importActual<typeof import("./api")>("./api");
  return {
    ...actual,
    postEvents: hoisted.postEvents,
    acknowledgeWarning: hoisted.acknowledgeWarning,
    postHeartbeat: vi.fn(),
    postAudioChunk: hoisted.postAudioChunk,
  };
});

vi.mock("./camera", () => ({
  CameraMonitor: class {
    constructor(options: typeof hoisted.camera.options) {
      hoisted.camera.options = options;
    }
    start = () => Promise.resolve();
    stop = () => undefined;
    status = () => ({ live: hoisted.camera.live });
    currentStream = () => hoisted.camera.stream;
    reacquire = hoisted.camera.reacquire;
  },
}));

vi.mock("./audio", () => ({
  AudioMonitor: class {
    constructor(options: typeof hoisted.audio.options) {
      hoisted.audio.options = options;
    }
    start = () => Promise.resolve();
    stop = () => undefined;
    live = () => hoisted.audio.live;
    currentStream = () => hoisted.audio.stream;
    reacquire = hoisted.audio.reacquire;
  },
}));

vi.mock("./recorder", () => ({
  SessionRecorder: class {
    start = hoisted.recorder.start;
    restart = hoisted.recorder.restart;
    stop = hoisted.recorder.stop;
  },
}));

vi.mock("./lockdown", () => ({
  installLockdown: (options: { onBlocked: (action: string, via?: string) => void }) => {
    hoisted.lockdown.onBlocked = options.onBlocked;
    return { release: () => undefined, installed: () => true };
  },
}));

vi.mock("./focus", () => ({
  watchFocus: () => ({
    release: () => undefined,
    installed: () => true,
    requestFullscreen: () => Promise.resolve(true),
  }),
}));

vi.mock("./displays", () => ({ watchDisplays: () => ({ release: () => undefined }) }));

vi.mock("./heartbeat", () => ({
  startHeartbeat: (options: {
    onResult: typeof hoisted.heartbeat.onResult;
    monitoring: () => unknown;
  }) => {
    hoisted.heartbeat.onResult = options.onResult;
    hoisted.heartbeat.monitoring = options.monitoring;
    return { stop: () => undefined, beat: hoisted.heartbeat.beat };
  },
}));

vi.mock("./integrity", () => ({
  startIntegrityCheck: (options: { probe: () => { camera: boolean; microphone: boolean } }) => {
    hoisted.integrity.probe = options.probe;
    return { stop: () => undefined, check: hoisted.integrity.check, current: options.probe };
  },
}));

import { pauseScreenOpen, SessionRuntime, type PauseView } from "./session";

const CONFIG = {
  max_warnings: 3,
  object_confidence_threshold: 0.65,
  object_consecutive_frames: 3,
  face_distance_threshold: 0.6,
  identity_check_interval_seconds: 30,
  obstruction_seconds: 60,
  obstruction_variance_threshold: 12,
  face_absent_moderate_seconds: 20,
  face_absent_extended_seconds: 90,
  focus_loss_ignore_under_seconds: 2,
  display_check_interval_seconds: 60,
  audio_chunk_seconds: 15,
  audio_max_chunk_bytes: 2_097_152,
  heartbeat_interval_seconds: 10,
  integrity_failure_termination_seconds: 60,
  device_glitch_seconds: 5,
  sampling_fps_normal: 2,
  sampling_fps_confirming: 6,
  confirming_window_seconds: 5,
  sampling_fps_degraded: 1,
  low_light_luminance_threshold: 40,
  low_light_cooldown_seconds: 300,
  loud_threshold_dbfs: -20,
  loud_margin_db: 25,
  loud_hysteresis_db: 6,
  loud_sustained_seconds: 6,
  loud_cooldown_seconds: 120,
  loud_calibration_seconds: 10,
  mouse_sample_hz: 10,
  max_keystroke_samples: 20000,
  event_batch_max: 200,
} satisfies ProctoringClientConfig;

const SESSION: SessionOut = {
  session_id: "session-1",
  conversation_id: "conversation-1",
  status: "active",
  warnings_used: 0,
  max_warnings: 3,
  warning_policy: "continue_and_note",
  consented_at: "2026-09-24T10:00:00Z",
  config: CONFIG,
  audio_analysis_available: true,
};

const RECORDING = {
  conversation_id: "conversation-1",
  recording_id: "recording-1",
  status: "recording",
  part_min_bytes: 5_242_880,
  part_max_bytes: 16_777_216,
  video_bits_per_second: 400_000,
  audio_bits_per_second: 48_000,
  max_width: 640,
  max_height: 360,
};

function ingest(overrides: Partial<IngestOut> = {}): IngestOut {
  return {
    accepted: 1,
    warnings_used: 0,
    max_warnings: 3,
    status: "active",
    warning: null,
    termination: null,
    ...overrides,
  };
}

function heartbeat(overrides: Partial<HeartbeatOut> = {}): HeartbeatOut {
  return {
    status: "active",
    warnings_used: 0,
    server_time: new Date(Date.now()).toISOString(),
    interval_seconds: 10,
    termination: null,
    ...overrides,
  };
}

/** Every event posted so far, in order. */
function posted(): EventIn[] {
  return hoisted.postEvents.mock.calls.flatMap((call) => call[1] as EventIn[]);
}

function build(session: SessionOut = SESSION) {
  const views: PauseView[] = [];
  const ended: string[] = [];
  const runtime = new SessionRuntime(
    session,
    {
      camera: { id: "camera-0" } as unknown as MediaStream,
      microphone: { id: "microphone-0" } as unknown as MediaStream,
      inference: { responsive: () => true, terminate: () => undefined } as never,
    },
    {
      onWarning: () => undefined,
      onTermination: () => undefined,
      onSessionEnded: (message) => ended.push(message),
      onPauseChange: (view) => views.push(view),
      onAdvisory: () => undefined,
    },
    RECORDING
  );
  runtime.start();
  return { runtime, views, ended };
}

const NOW = Date.UTC(2026, 8, 24, 10, 0, 0);

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(NOW);
  hoisted.postEvents.mockResolvedValue(ingest());
  hoisted.camera.live = true;
  hoisted.audio.live = true;
  hoisted.camera.stream = { id: "camera-0" } as unknown as MediaStream;
  hoisted.audio.stream = { id: "microphone-0" } as unknown as MediaStream;
});

afterEach(() => {
  vi.useRealTimers();
  vi.clearAllMocks();
});

describe("the integrity check and the device pause", () => {
  it("does not count a device the pause owns, while the heartbeat still reports it down", () => {
    const { runtime } = build();
    hoisted.camera.live = false;
    // Lost but not yet known to the watch: an integrity failure like any other.
    expect(hoisted.integrity.probe?.().camera).toBe(false);
    hoisted.camera.options?.onLost("stream");
    expect(hoisted.integrity.probe?.().camera).toBe(true);
    expect(runtime.probe().camera).toBe(false);
    // The heartbeat body is the honest probe, and asking for it runs the check.
    expect((hoisted.heartbeat.monitoring?.() as { camera: boolean }).camera).toBe(false);
    expect(hoisted.integrity.check).toHaveBeenCalled();
    runtime.stop();
  });
});

describe("the pause picture", () => {
  it("places the server's grace deadline on this device's clock", () => {
    const { runtime, views } = build();
    // The server's clock runs five seconds ahead of this device's.
    const skew = 5000;
    hoisted.heartbeat.onResult?.(
      heartbeat({
        server_time: new Date(NOW + skew).toISOString(),
        pause: {
          paused: true,
          grace_deadline_at: new Date(NOW + skew + 120_000).toISOString(),
          pauses_used: 1,
          max_pauses: 2,
        },
      }),
      NOW,
      NOW
    );
    const view = views.at(-1)!;
    expect(view.serverPaused).toBe(true);
    expect(view.graceDeadlineMs).toBe(NOW + 120_000);
    expect([view.pausesUsed, view.maxPauses]).toEqual([1, 2]);
    expect(pauseScreenOpen(view)).toBe(true);
    runtime.stop();
  });

  it("opens the screen once a loss is reported, before the server has answered", async () => {
    const { runtime, views } = build();
    hoisted.camera.live = false;
    hoisted.camera.options?.onLost("stream");
    // Inside the glitch allowance: nothing on screen.
    expect(pauseScreenOpen(views.at(-1)!)).toBe(false);
    await vi.advanceTimersByTimeAsync(5000);
    expect(posted().map((event) => event.event_type)).toContain("CAMERA_STREAM_FAILED");
    const view = views.at(-1)!;
    expect(view.lostDevices).toEqual(["camera"]);
    expect(pauseScreenOpen(view)).toBe(true);
    runtime.stop();
  });

  it("tells the server the devices are back after a reload into an open pause, once", async () => {
    const { runtime } = build();
    const pause = { paused: true, grace_deadline_at: new Date(NOW + 90_000).toISOString(), pauses_used: 1, max_pauses: 2 };
    hoisted.heartbeat.onResult?.(heartbeat({ pause }), NOW, NOW);
    hoisted.heartbeat.onResult?.(heartbeat({ pause }), NOW, NOW);
    await vi.advanceTimersByTimeAsync(0);
    const recoveries = posted().filter((event) => event.event_type === "DEVICE_RECOVERED");
    expect(recoveries).toHaveLength(1);
    expect(recoveries[0].metadata).toEqual({ devices: [], resumed: true });
    runtime.stop();
  });

  it("reads the pause the session was opened with, and lifts it at once after a reload", async () => {
    const pause = {
      paused: true,
      message: "Your camera has stopped, so the assessment is paused.",
      grace_deadline_at: new Date(NOW + 90_000).toISOString(),
      pauses_used: 1,
      max_pauses: 2,
    };
    const { runtime, views } = build({ ...SESSION, pause });
    // Shown before any heartbeat has answered: the page does not sit on a
    // question the server will refuse.
    expect(views.at(-1)!.serverPaused).toBe(true);
    expect(views.at(-1)!.message).toBe(pause.message);
    await vi.advanceTimersByTimeAsync(0);
    expect(posted().filter((event) => event.event_type === "DEVICE_RECOVERED")).toHaveLength(1);
    // The first heartbeat is no longer the first statement, so it sends nothing more.
    hoisted.heartbeat.onResult?.(heartbeat({ pause }), NOW, NOW);
    await vi.advanceTimersByTimeAsync(0);
    expect(posted().filter((event) => event.event_type === "DEVICE_RECOVERED")).toHaveLength(1);
    runtime.stop();
  });

  it("carries the server's sentence only while the pause is open", () => {
    const { runtime, views } = build();
    hoisted.heartbeat.onResult?.(
      heartbeat({
        pause: {
          paused: true,
          message: "The server's words.",
          grace_deadline_at: new Date(NOW + 60_000).toISOString(),
          pauses_used: 1,
          max_pauses: 2,
        },
      }),
      NOW,
      NOW
    );
    expect(views.at(-1)!.message).toBe("The server's words.");
    hoisted.heartbeat.onResult?.(
      heartbeat({
        pause: { paused: false, message: "stale", grace_deadline_at: null, pauses_used: 1, max_pauses: 2 },
      }),
      NOW,
      NOW
    );
    expect(views.at(-1)!.message).toBeNull();
    runtime.stop();
  });

  it("sends no such recovery when the first word from the server is not a pause", async () => {
    const { runtime } = build();
    hoisted.heartbeat.onResult?.(
      heartbeat({ pause: { paused: false, grace_deadline_at: null, pauses_used: 0, max_pauses: 2 } }),
      NOW,
      NOW
    );
    await vi.advanceTimersByTimeAsync(0);
    expect(posted()).toEqual([]);
    runtime.stop();
  });
});

describe("the audio chunk", () => {
  it("goes to the session's audio route with nothing but the audio", () => {
    const { runtime } = build();
    const chunk = new Blob(["speech"]);
    hoisted.audio.options?.upload(chunk);
    expect(hoisted.postAudioChunk).toHaveBeenCalledWith("session-1", chunk);
    runtime.stop();
  });
});

describe("a recovery", () => {
  it("starts a new recording segment on the new tracks once every device is back", () => {
    const { runtime } = build();
    expect(hoisted.recorder.start).toHaveBeenCalledWith(
      { id: "camera-0" },
      { id: "microphone-0" }
    );
    hoisted.camera.options?.onLost("stream");
    hoisted.audio.options?.onLost("permission");
    hoisted.camera.stream = { id: "camera-1" } as unknown as MediaStream;
    hoisted.camera.options?.onRecovered(hoisted.camera.stream);
    // The microphone is still down: no segment on half a session.
    expect(hoisted.recorder.restart).not.toHaveBeenCalled();
    hoisted.audio.stream = { id: "microphone-1" } as unknown as MediaStream;
    hoisted.audio.options?.onRecovered(hoisted.audio.stream);
    expect(hoisted.recorder.restart).toHaveBeenCalledWith({ id: "camera-1" }, { id: "microphone-1" });
    runtime.stop();
  });

  it("is attempted for the lost devices only when the candidate presses Try again", async () => {
    const { runtime } = build();
    hoisted.audio.options?.onLost("permission");
    hoisted.audio.reacquire.mockImplementation(async () => {
      hoisted.audio.options?.onRecovered({ id: "microphone-1" } as unknown as MediaStream);
      return true;
    });
    await expect(runtime.retryDevices()).resolves.toBe(true);
    expect(hoisted.audio.reacquire).toHaveBeenCalledTimes(1);
    expect(hoisted.camera.reacquire).not.toHaveBeenCalled();
    runtime.stop();
  });
});

describe("blocked actions", () => {
  it("records a paste the document caught once, against the answer on screen", async () => {
    const { runtime } = build();
    const hooks = runtime.fieldHooksFor("q1");
    hoisted.lockdown.onBlocked?.("paste");
    // The same attempt reaching the field in the same dispatch is not a second one.
    hooks.onBlockedAction("paste" as never);
    await vi.advanceTimersByTimeAsync(3000);
    expect(posted().map((event) => event.metadata)).toEqual([{ action: "paste" }]);
    expect(runtime.collectAnswerBehaviour("q1")?.blocked_action_count).toBe(1);
    runtime.stop();
  });

  it("records an attempt only the field saw, with its kind", async () => {
    const { runtime } = build();
    const hooks = runtime.fieldHooksFor("q1");
    hooks.onBlockedAction("drop" as never);
    await vi.advanceTimersByTimeAsync(3000);
    expect(posted().map((event) => event.metadata)).toEqual([{ action: "drop", via: "field" }]);
    expect(runtime.collectAnswerBehaviour("q1")?.blocked_action_count).toBe(1);
    runtime.stop();
  });

  it("does not count a clipboard read or a shortcut against the answer", async () => {
    const { runtime } = build();
    runtime.fieldHooksFor("q1");
    hoisted.lockdown.onBlocked?.("paste", "clipboard_api");
    hoisted.lockdown.onBlocked?.("developer_tools");
    await vi.advanceTimersByTimeAsync(3000);
    expect(posted().map((event) => event.metadata)).toEqual([
      { action: "paste", via: "clipboard_api" },
      { action: "developer_tools" },
    ]);
    expect(runtime.collectAnswerBehaviour("q1")?.blocked_action_count).toBe(0);
    runtime.stop();
  });
});

describe("a warning acknowledgement", () => {
  it("is sent to the server", async () => {
    const { runtime } = build();
    hoisted.acknowledgeWarning.mockResolvedValue(undefined);
    await runtime.acknowledgeWarning();
    expect(hoisted.acknowledgeWarning).toHaveBeenCalledWith("session-1");
    runtime.stop();
  });

  it("ends the session when the server says it is already over", async () => {
    const { runtime, ended } = build();
    hoisted.acknowledgeWarning.mockRejectedValue(new ApiError(409, null, "This assessment has ended."));
    await runtime.acknowledgeWarning();
    expect(ended).toEqual(["This assessment has ended."]);
  });
});
