// @vitest-environment jsdom
//
// The shell's four screens and the two gates between them.
//
// The gates are the point: consent is an explicit action and nothing opens a
// device before it, and the assessment cannot begin until every system-check
// row has passed. The system check and the session runtime are mocked,
// because both need a real camera; what is exercised here is what the shell
// does with their answers, including the three the SERVER decides, a
// warning, a pause and a termination, which are driven through the callbacks
// the shell hands the runtime.
//
// Phase 3 (2026-09-24): the consent screen renders the server's rules and
// refuses to offer the agreement without them; a warning's hold is the
// server's to measure, so acknowledging it is a call and not a stopwatch; and
// a lost device puts up the pause screen, which the player also sees as
// `paused` on the bridge.

import * as React from "react";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { PauseView, SessionCallbacks } from "@/lib/proctoring/session";

const {
  fetchClientConfig,
  createSession,
  startSessionMedia,
  runSystemCheck,
  releaseOutcome,
  sessionStart,
  sessionStop,
  acknowledgeWarning,
  retryDevices,
  checkNow,
  captured,
} = vi.hoisted(() => ({
  fetchClientConfig: vi.fn(),
  createSession: vi.fn(),
  startSessionMedia: vi.fn(),
  runSystemCheck: vi.fn(),
  releaseOutcome: vi.fn(),
  sessionStart: vi.fn(),
  sessionStop: vi.fn(),
  acknowledgeWarning: vi.fn(),
  retryDevices: vi.fn(),
  checkNow: vi.fn(),
  captured: { callbacks: null as SessionCallbacks | null },
}));

vi.mock("@/lib/proctoring/api", () => ({ createSession, fetchClientConfig, startSessionMedia }));
vi.mock("@/lib/proctoring/session", async () => {
  const actual = await vi.importActual<typeof import("@/lib/proctoring/session")>(
    "@/lib/proctoring/session"
  );
  return {
    ...actual,
    SessionRuntime: class {
      constructor(_session: unknown, _media: unknown, callbacks: SessionCallbacks) {
        captured.callbacks = callbacks;
      }
      start = sessionStart;
      stop = sessionStop;
      flush = () => Promise.resolve();
      requestFullscreen = () => Promise.resolve(true);
      fieldHooksFor = () => null;
      collectAnswerBehaviour = () => null;
      acknowledgeWarning = acknowledgeWarning;
      retryDevices = retryDevices;
      checkNow = checkNow;
    },
  };
});
vi.mock("@/lib/proctoring/system-check", async () => {
  const actual = await vi.importActual<typeof import("@/lib/proctoring/system-check")>(
    "@/lib/proctoring/system-check"
  );
  return { ...actual, runSystemCheck, releaseOutcome };
});

import { CONSENT_ACTION, CONSENT_LOADING, CONSENT_RETRY } from "./consent-screen";
import { MONITORING_PAUSED } from "./monitoring-indicator";
import { PAUSE_RETRY, PAUSE_TITLE } from "./pause-overlay";
import { useProctoring } from "./proctoring-context";
import { ANSWERS_SAVED, ProctoringShell } from "./proctoring-shell";
import { WARNING_ACKNOWLEDGE } from "./warning-modal";
import { CHECK_LABELS, buildRows, type SystemCheckOutcome } from "@/lib/proctoring/system-check";

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
};

function outcome(passed: boolean): SystemCheckOutcome {
  const payload = {
    camera: passed,
    microphone: true,
    browser_supported: true,
    fullscreen_supported: true,
    face_detected: true,
    inference_adequate: true,
    measured_fps: 2,
  };
  return {
    rows: buildRows(payload, passed ? {} : { camera: "Allow camera access for this site." }),
    payload,
    allPassed: passed,
    camera: {} as MediaStream,
    microphone: {} as MediaStream,
    inference: { setBaseline: vi.fn(), terminate: vi.fn() } as never,
    faceDescriptor: Array.from({ length: 128 }, () => 0.1),
    deviceContext: {
      user_agent: "test",
      platform: "test",
      screen_count: 1,
      screen_width: 1280,
      screen_height: 800,
      camera_width: 640,
      camera_height: 480,
      hardware_concurrency: 8,
      webgl: true,
    },
  };
}

const RULES = [
  "Each question has its own timer, shown on screen.",
  "If your camera or microphone stops, the assessment pauses for two minutes while you fix it.",
];

beforeEach(() => {
  fetchClientConfig.mockResolvedValue({
    config: CONFIG,
    max_warnings: 3,
    audio_analysis_available: true,
    candidate_rules: RULES,
  });
  startSessionMedia.mockRejectedValue(new Error("no recording in this test"));
  acknowledgeWarning.mockResolvedValue(undefined);
  retryDevices.mockResolvedValue(true);
  createSession.mockResolvedValue({
    session_id: "session-1",
    conversation_id: "conversation-1",
    status: "active",
    warnings_used: 0,
    max_warnings: 3,
    warning_policy: "continue_and_note",
    consented_at: "2026-09-02T10:00:00Z",
    config: CONFIG,
    audio_analysis_available: true,
  });
});

afterEach(() => {
  cleanup();
  captured.callbacks = null;
  vi.clearAllMocks();
});

/**
 * A stand-in for the assessment player: it reads the bridge exactly as the
 * conversation does, so `paused` is observed through the context rather than
 * through an internal the player cannot reach.
 */
function PausedProbe() {
  const bridge = useProctoring() as ReturnType<typeof useProctoring> & { paused?: boolean };
  return (
    <>
      <p>The first question</p>
      <p data-testid="bridge-paused">{bridge.paused ? "held" : "running"}</p>
    </>
  );
}

function renderShell() {
  return render(
    <ProctoringShell linkId="link-1">
      <PausedProbe />
    </ProctoringShell>
  );
}

async function agree() {
  fireEvent.click(await screen.findByRole("button", { name: CONSENT_ACTION }));
}

async function startSession() {
  runSystemCheck.mockResolvedValue(outcome(true));
  renderShell();
  await agree();
  fireEvent.click(await screen.findByRole("button", { name: /Start the assessment/i }));
  await screen.findByText("The first question");
}

function pauseView(overrides: Partial<PauseView> = {}): PauseView {
  return {
    serverPaused: true,
    message: "Your camera has stopped, so the assessment is paused.",
    graceDeadlineMs: Date.now() + 120_000,
    pausesUsed: 1,
    maxPauses: 2,
    lostDevices: ["camera"],
    lossReported: true,
    ...overrides,
  };
}

describe("consent", () => {
  it("states the server's rules, verbatim, before anything is opened", async () => {
    renderShell();
    for (const rule of RULES) {
      expect(await screen.findByText(rule), rule).toBeTruthy();
    }
    expect(runSystemCheck).not.toHaveBeenCalled();
    expect(createSession).not.toHaveBeenCalled();
  });

  it("does not offer the agreement while the rules are loading", async () => {
    let resolve: (value: unknown) => void = () => undefined;
    fetchClientConfig.mockReturnValue(new Promise((done) => (resolve = done)));
    renderShell();
    expect(screen.getByText(CONSENT_LOADING)).toBeTruthy();
    expect(screen.queryByRole("button", { name: CONSENT_ACTION })).toBeNull();
    resolve({ config: CONFIG, max_warnings: 3, audio_analysis_available: true, candidate_rules: RULES });
    expect(await screen.findByRole("button", { name: CONSENT_ACTION })).toBeTruthy();
  });

  it("says so and offers a retry when the rules cannot be loaded, never a fallback text", async () => {
    fetchClientConfig
      .mockRejectedValueOnce(new Error("The assessment rules could not be loaded. Please try again."))
      .mockResolvedValueOnce({
        config: CONFIG,
        max_warnings: 3,
        audio_analysis_available: true,
        candidate_rules: RULES,
      });
    renderShell();
    expect(
      await screen.findByText("The assessment rules could not be loaded. Please try again.")
    ).toBeTruthy();
    expect(screen.queryByRole("button", { name: CONSENT_ACTION })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: CONSENT_RETRY }));
    expect(await screen.findByText(RULES[0])).toBeTruthy();
    expect(fetchClientConfig).toHaveBeenCalledTimes(2);
  });

  it("does not reach the assessment or the devices until the explicit action", async () => {
    runSystemCheck.mockResolvedValue(outcome(true));
    renderShell();
    await screen.findByText(RULES[0]);
    expect(screen.queryByText("The first question")).toBeNull();
    expect(runSystemCheck).not.toHaveBeenCalled();
    await agree();
    await waitFor(() => expect(runSystemCheck).toHaveBeenCalled());
    expect(screen.queryByText("The first question")).toBeNull();
  });
});

describe("system check", () => {
  it("refuses to start while a row has failed, and says how to fix it", async () => {
    runSystemCheck.mockResolvedValue(outcome(false));
    renderShell();
    await agree();
    expect(await screen.findByText(CHECK_LABELS.camera)).toBeTruthy();
    expect(screen.getByText("Allow camera access for this site.")).toBeTruthy();
    expect(screen.queryByRole("button", { name: /Start the assessment/i })).toBeNull();
    expect(screen.getByRole("button", { name: /Check again/i })).toBeTruthy();
    expect(createSession).not.toHaveBeenCalled();
  });

  it("re-runs every check on retry and offers the start once they pass", async () => {
    runSystemCheck.mockResolvedValueOnce(outcome(false)).mockResolvedValueOnce(outcome(true));
    renderShell();
    await agree();
    fireEvent.click(await screen.findByRole("button", { name: /Check again/i }));
    expect(await screen.findByRole("button", { name: /Start the assessment/i })).toBeTruthy();
    expect(runSystemCheck).toHaveBeenCalledTimes(2);
  });

  it("mounts the assessment only once a session exists", async () => {
    await startSession();
    expect(createSession).toHaveBeenCalledWith("link-1", expect.objectContaining({ consent: true }));
    expect(sessionStart).toHaveBeenCalled();
  });

  it("sends the descriptor and nothing resembling an image", async () => {
    await startSession();
    const body = createSession.mock.calls[0][1] as Record<string, unknown>;
    expect(body.face_descriptor).toHaveLength(128);
    expect(Object.keys(body).sort()).toEqual(
      ["consent", "device_context", "face_descriptor", "system_check"].sort()
    );
  });

  it("stays on the check screen and explains when the session cannot be created", async () => {
    createSession.mockRejectedValue(new Error("The invitation could not be found."));
    await runSystemCheck.mockResolvedValue(outcome(true));
    renderShell();
    await agree();
    fireEvent.click(await screen.findByRole("button", { name: /Start the assessment/i }));
    expect(await screen.findByText("The invitation could not be found.")).toBeTruthy();
    expect(screen.queryByText("The first question")).toBeNull();
  });
});

describe("the running session", () => {
  it("shows the indicator with the count the server sent", async () => {
    await startSession();
    expect(screen.getByText("Monitoring active")).toBeTruthy();
    expect(screen.getByText("No warnings so far")).toBeTruthy();
  });

  it("blocks on the server's warning message and updates the indicator to its count", async () => {
    await startSession();
    act(() => {
      captured.callbacks?.onWarning(
        {
          number: 1,
          max_warnings: 3,
          event_type: "DEVICE_DETECTED_PHONE",
          message: "A phone was detected on camera. Please move it out of view.",
          final: false,
        },
        1
      );
    });
    expect(
      await screen.findByText("A phone was detected on camera. Please move it out of view.")
    ).toBeTruthy();
    expect(screen.getByText("One warning used")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: WARNING_ACKNOWLEDGE }));
    await waitFor(() =>
      expect(screen.queryByText("A phone was detected on camera. Please move it out of view.")).toBeNull()
    );
  });

  it("holds the player while a warning is open and tells the server when it is acknowledged", async () => {
    await startSession();
    expect(screen.getByTestId("bridge-paused").textContent).toBe("running");
    act(() => {
      captured.callbacks?.onWarning(
        {
          number: 1,
          max_warnings: 3,
          event_type: "FULLSCREEN_EXITED",
          message: "Please return to fullscreen.",
          final: false,
        },
        1
      );
    });
    expect(screen.getByTestId("bridge-paused").textContent).toBe("held");
    expect(acknowledgeWarning).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: WARNING_ACKNOWLEDGE }));
    // The server measured the hold; the acknowledgement is what ends it.
    expect(acknowledgeWarning).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(screen.getByTestId("bridge-paused").textContent).toBe("running"));
  });

  it("unmounts the assessment and explains the ending when the server terminates", async () => {
    await startSession();
    act(() => {
      captured.callbacks?.onTermination({
        reason_code: "IDENTITY_MISMATCH",
        message: "Your assessment has ended. " + ANSWERS_SAVED,
      });
    });
    expect(await screen.findByText(/Your assessment has ended\./)).toBeTruthy();
    expect(screen.queryByText("The first question")).toBeNull();
    expect(screen.queryByText("Monitoring active")).toBeNull();
    expect(sessionStop).toHaveBeenCalled();
  });
});

describe("the device pause", () => {
  it("puts up the pause screen, holds the player and says so in the indicator", async () => {
    await startSession();
    act(() => {
      captured.callbacks?.onPauseChange(pauseView());
    });
    expect(await screen.findByText(PAUSE_TITLE)).toBeTruthy();
    // The server's sentence, verbatim.
    expect(screen.getByText("Your camera has stopped, so the assessment is paused.")).toBeTruthy();
    expect(screen.getByTestId("bridge-paused").textContent).toBe("held");
    expect(screen.getByText(MONITORING_PAUSED)).toBeTruthy();
  });

  it("stays down for a glitch still inside its allowance", async () => {
    await startSession();
    act(() => {
      captured.callbacks?.onPauseChange(
        pauseView({ serverPaused: false, graceDeadlineMs: null, lossReported: false })
      );
    });
    expect(screen.queryByText(PAUSE_TITLE)).toBeNull();
    expect(screen.getByTestId("bridge-paused").textContent).toBe("running");
  });

  it("hands Try again to the runtime and asks the server when the grace runs out", async () => {
    await startSession();
    act(() => {
      captured.callbacks?.onPauseChange(pauseView({ graceDeadlineMs: Date.now() + 60_000 }));
    });
    fireEvent.click(await screen.findByRole("button", { name: PAUSE_RETRY }));
    await waitFor(() => expect(retryDevices).toHaveBeenCalledTimes(1));
    act(() => {
      captured.callbacks?.onPauseChange(pauseView({ graceDeadlineMs: Date.now() - 1 }));
    });
    await waitFor(() => expect(checkNow).toHaveBeenCalledTimes(1));
  });

  it("lifts when the server says the pause is over", async () => {
    await startSession();
    act(() => {
      captured.callbacks?.onPauseChange(pauseView());
    });
    await screen.findByText(PAUSE_TITLE);
    act(() => {
      captured.callbacks?.onPauseChange(
        pauseView({ serverPaused: false, graceDeadlineMs: null, lostDevices: [], lossReported: false })
      );
    });
    await waitFor(() => expect(screen.queryByText(PAUSE_TITLE)).toBeNull());
    expect(screen.getByTestId("bridge-paused").textContent).toBe("running");
  });
});
