// @vitest-environment jsdom
//
// The spoken-answer control on its own (Appendix B section 3): the recorder
// stops at the SERVER's limit, the clock can stop a capture and have it sent,
// a container the browser lacks is never assumed, and a failure that never
// reached the server offers typing without pretending a pause was held.

import * as React from "react";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const { apiPost, apiGet, apiUpload } = vi.hoisted(() => ({
  apiPost: vi.fn(),
  apiGet: vi.fn(),
  apiUpload: vi.fn(),
}));
vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return { ...actual, apiPost, apiGet, apiUpload };
});

import {
  TIMER_WAITED,
  VOICE_AUDIO_BITS_PER_SECOND,
  VoiceAnswer,
  pickAudioType,
  type VoiceAnswerHandle,
  type VoiceMedia,
  type VoicePhase,
} from "./voice-answer";

const BASE = "/api/v2/assessments/conversations/conv-1/voice";

class FakeRecorder {
  state: "inactive" | "recording" = "inactive";
  mimeType = "audio/webm;codecs=opus";
  ondataavailable: ((event: { data: Blob }) => void) | null = null;
  onstop: (() => void) | null = null;
  onerror: (() => void) | null = null;
  start = vi.fn(() => {
    this.state = "recording";
  });
  stop = vi.fn(() => {
    this.state = "inactive";
    this.ondataavailable?.({ data: new Blob(["voice"], { type: this.mimeType }) });
    this.onstop?.();
  });
}

function media(overrides: Partial<VoiceMedia> = {}): VoiceMedia & { recorder: FakeRecorder; track: { stop: ReturnType<typeof vi.fn> } } {
  const recorder = new FakeRecorder();
  const track = { stop: vi.fn() };
  return {
    recorder,
    track,
    getAudioStream: vi.fn(async () => ({ getTracks: () => [track] }) as unknown as MediaStream),
    isTypeSupported: (type: string) => type === "audio/webm;codecs=opus",
    createRecorder: () => recorder as unknown as MediaRecorder,
    ...overrides,
  };
}

function mount(fake: VoiceMedia, handle?: React.Ref<VoiceAnswerHandle>) {
  const phases: VoicePhase[] = [];
  const onTranscribed = vi.fn();
  const onSwitchToTyping = vi.fn();
  render(
    <VoiceAnswer
      ref={handle}
      conversationId="conv-1"
      turnSeq={7}
      disabled={false}
      onPhaseChange={(phase) => phases.push(phase)}
      onTranscribed={onTranscribed}
      onSwitchToTyping={onSwitchToTyping}
      media={fake}
    />
  );
  return { phases, onTranscribed, onSwitchToTyping };
}

beforeEach(() => {
  apiPost.mockReset();
  apiGet.mockReset();
  apiUpload.mockReset();
  apiPost.mockImplementation(async (path: string) => {
    if (path === `${BASE}/begin`) {
      return { id: "v-1", status: "recording", transcript: null, max_seconds: 2 };
    }
    throw new Error(`unexpected POST ${path}`);
  });
  apiUpload.mockResolvedValue({ id: "v-1", status: "uploaded", transcript: null, max_seconds: 2 });
  apiGet.mockResolvedValue({
    id: "v-1",
    status: "transcribed",
    transcript: "Spoken words.",
    max_seconds: 2,
  });
});
afterEach(cleanup);

describe("pickAudioType", () => {
  it("asks the browser rather than assuming a container", () => {
    expect(pickAudioType(() => false)).toBeNull();
    expect(pickAudioType((type) => type === "audio/mp4")).toBe("audio/mp4");
    expect(pickAudioType(() => true)).toBe("audio/webm;codecs=opus");
  });
});

describe("VoiceAnswer", () => {
  it("stops itself at the server's limit and sends what it captured", async () => {
    const fake = media();
    const { onTranscribed } = mount(fake);
    fireEvent.click(screen.getByRole("button", { name: "Answer by speaking" }));
    await screen.findByTestId("voice-recording");
    // The limit shown is the server's, not a number this control holds.
    expect(screen.getByTestId("voice-recording").textContent).toContain("of 0:02");
    await waitFor(() => expect(fake.recorder.stop).toHaveBeenCalledTimes(1), { timeout: 4000 });
    await waitFor(() => expect(apiUpload).toHaveBeenCalledTimes(1));
    expect(fake.track.stop).toHaveBeenCalled();
    await waitFor(() => expect(onTranscribed).toHaveBeenCalledWith("v-1", "Spoken words.", false), {
      timeout: 4000,
    });
    // The transcript is shown read-only: there is no box to edit it in.
    expect(screen.getByTestId("voice-transcript").textContent).toContain("Spoken words.");
    expect(screen.queryByRole("textbox")).toBeNull();
  });

  it("is stopped and sent by the clock, and says so to the player", async () => {
    const fake = media();
    const handle = React.createRef<VoiceAnswerHandle>();
    apiPost.mockImplementation(async (path: string) => {
      if (path === `${BASE}/begin`) {
        return { id: "v-1", status: "recording", transcript: null, max_seconds: 180 };
      }
      throw new Error(`unexpected POST ${path}`);
    });
    const { onTranscribed } = mount(fake, handle);
    fireEvent.click(screen.getByRole("button", { name: "Answer by speaking" }));
    await screen.findByTestId("voice-recording");

    let sent = false;
    act(() => {
      sent = handle.current?.stopForExpiry() ?? false;
    });
    expect(sent).toBe(true);
    await waitFor(() => expect(onTranscribed).toHaveBeenCalledWith("v-1", "Spoken words.", true), {
      timeout: 4000,
    });
  });

  it("claims nothing for the clock when no capture was started", () => {
    const handle = React.createRef<VoiceAnswerHandle>();
    mount(media(), handle);
    expect(handle.current?.stopForExpiry()).toBe(false);
  });

  it("offers typing without a server capture when the microphone is refused", async () => {
    const fake = media({
      getAudioStream: vi.fn(async () => {
        throw Object.assign(new Error("denied"), { name: "NotAllowedError" });
      }),
    });
    const { phases } = mount(fake);
    fireEvent.click(screen.getByRole("button", { name: "Answer by speaking" }));
    await screen.findByTestId("voice-unavailable");
    expect(phases).toContain("unavailable");
    expect(apiPost).not.toHaveBeenCalled();
  });

  it("does not claim a held pause when the upload never arrived", async () => {
    apiUpload.mockRejectedValue(new Error("offline"));
    const fake = media();
    const { phases, onTranscribed } = mount(fake);
    fireEvent.click(screen.getByRole("button", { name: "Answer by speaking" }));
    await screen.findByTestId("voice-recording");
    fireEvent.click(screen.getByRole("button", { name: "Stop and send" }));
    await screen.findByTestId("voice-unavailable");
    expect(phases).not.toContain("failed");
    expect(onTranscribed).not.toHaveBeenCalled();
  });

  it("records speech at a bitrate that keeps the longest answer under the upload ceiling", async () => {
    const created: MediaRecorderOptions[] = [];
    const fake = media();
    const createRecorder = fake.createRecorder;
    fake.createRecorder = (stream, options) => {
      created.push(options);
      return createRecorder(stream, options);
    };
    mount(fake);
    fireEvent.click(screen.getByRole("button", { name: "Answer by speaking" }));
    await screen.findByTestId("voice-recording");
    expect(created).toEqual([
      { mimeType: "audio/webm;codecs=opus", audioBitsPerSecond: VOICE_AUDIO_BITS_PER_SECOND },
    ]);
  });

  it("says a failed transcription in the server's words and closes its pause before typing", async () => {
    apiGet.mockResolvedValue({
      id: "v-1",
      status: "failed",
      transcript: null,
      message: "Speech to text did not answer in time.",
      max_seconds: 2,
    });
    apiPost.mockImplementation(async (path: string) => {
      if (path === `${BASE}/begin`) {
        return { id: "v-1", status: "recording", transcript: null, max_seconds: 180 };
      }
      if (path === `${BASE}/v-1/acknowledge`) return { id: "v-1", status: "failed", max_seconds: 180 };
      throw new Error(`unexpected POST ${path}`);
    });
    const fake = media();
    const { onTranscribed, onSwitchToTyping } = mount(fake);
    fireEvent.click(screen.getByRole("button", { name: "Answer by speaking" }));
    await screen.findByTestId("voice-recording");
    fireEvent.click(screen.getByRole("button", { name: "Stop and send" }));
    const failed = await screen.findByTestId("voice-failed", undefined, { timeout: 4000 });
    expect(failed.textContent).toContain("Speech to text did not answer in time.");
    expect(failed.textContent).toContain(TIMER_WAITED);
    // Nothing invented is offered as what was said.
    expect(onTranscribed).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "Type my answer instead" }));
    await waitFor(() => expect(onSwitchToTyping).toHaveBeenCalledTimes(1));
    expect(apiPost).toHaveBeenCalledWith(`${BASE}/v-1/acknowledge`);
  });
});

describe("VoiceAnswer recording flag for the proctoring audio detector", () => {
  it("is on only while recording, and off once the recording is sent", async () => {
    const { isVoiceRecordingActive } = await import("@/lib/assessment/voice-activity");
    const fake = media();
    mount(fake);
    expect(isVoiceRecordingActive()).toBe(false);
    fireEvent.click(screen.getByRole("button", { name: "Answer by speaking" }));
    await screen.findByTestId("voice-recording");
    expect(isVoiceRecordingActive()).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "Stop and send" }));
    await waitFor(() => expect(isVoiceRecordingActive()).toBe(false));
  });

  it("is cleared when the control unmounts mid-recording", async () => {
    const { isVoiceRecordingActive } = await import("@/lib/assessment/voice-activity");
    mount(media());
    fireEvent.click(screen.getByRole("button", { name: "Answer by speaking" }));
    await screen.findByTestId("voice-recording");
    cleanup();
    expect(isVoiceRecordingActive()).toBe(false);
  });
});

describe("VoiceAnswer recovery", () => {
  it("picks up a spoken answer already being transcribed instead of recording again", async () => {
    const fake = media();
    apiPost.mockImplementation(async (path: string) => {
      if (path === `${BASE}/begin`) {
        return { id: "v-1", status: "transcribing", transcript: null, max_seconds: 180 };
      }
      throw new Error(`unexpected POST ${path}`);
    });
    const { onTranscribed } = mount(fake);
    fireEvent.click(screen.getByRole("button", { name: "Answer by speaking" }));
    await waitFor(() => expect(onTranscribed).toHaveBeenCalledWith("v-1", "Spoken words.", false), {
      timeout: 4000,
    });
    expect(fake.recorder.start).not.toHaveBeenCalled();
  });

  it("offers another try after the microphone could not be opened", async () => {
    const fake = media({
      getAudioStream: vi.fn(async () => {
        throw new Error("denied");
      }),
    });
    mount(fake);
    fireEvent.click(screen.getByRole("button", { name: "Answer by speaking" }));
    await screen.findByTestId("voice-unavailable");
    fireEvent.click(screen.getByRole("button", { name: "Try speaking again" }));
    expect(screen.getByRole("button", { name: "Answer by speaking" })).toBeTruthy();
  });
});
