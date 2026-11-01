// @vitest-environment jsdom
//
// The single-mode player (Appendix B sections 1 to 3, PLAN-p3 section 3.4).
//
// Each block pins one promise the screen makes and the server relies on:
// every submission names the server's turn and carries no time the browser
// measured; past answers are read-only; a stale turn is refused and never
// re-filed; zero on the face is checked against the server before anything
// is sent, and an empty answer the clock sends is sent empty; a paused turn
// cannot be answered; the draft reaches the server; a spoken answer is
// submitted as its final transcript and a failed one falls back to typing.

import * as React from "react";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ProctoringProvider } from "@/components/proctoring/proctoring-context";
import { ApiError } from "@/lib/api";
import { draftKey } from "@/lib/assessment/autosave";
import type {
  AnswerBehaviour,
  CodingPayloadViewV2,
  ConversationTurn,
  ProctoringBridge,
  ProctoringFieldHooks,
  QuestionOut,
  TurnClock,
} from "@/lib/assessment/contracts";

const { apiPost, apiPut, apiGet, apiUpload, toast } = vi.hoisted(() => ({
  apiPost: vi.fn(),
  apiPut: vi.fn(),
  apiGet: vi.fn(),
  apiUpload: vi.fn(),
  toast: vi.fn(),
}));

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return { ...actual, apiPost, apiPut, apiGet, apiUpload };
});
vi.mock("@/lib/auth-context", () => ({
  useAuth: () => ({ user: { full_name: "Asha Rao" } }),
}));
// ONE toast function for the whole file. The player's callbacks depend on
// `toast`, so a mock that minted a new function per render would re-create
// them on every render and the test would be measuring the mock.
vi.mock("@/components/ui/toast", () => ({
  useToast: () => ({ toast }),
}));
vi.mock("@/components/app-shell", () => ({
  PageHeader: ({ title }: { title: string }) => <h1>{title}</h1>,
}));
// Monaco cannot run under jsdom; the double stands in for the editor.
vi.mock("@monaco-editor/react", async () =>
  (await import("@/lib/assessment/monaco-test-double")).monacoReactModule()
);
vi.mock("next/link", () => ({
  default: ({ href, children }: { href: string; children: React.ReactNode }) => (
    <a href={href}>{children}</a>
  ),
}));

import { resetMonacoDouble } from "@/lib/assessment/monaco-test-double";

import { AssessmentConversation, EXPIRY_UNREACHABLE } from "./assessment-conversation";

const LINK = "link-1";
const START = `/api/v2/assessments/conversations/links/${LINK}/start`;
const RESPOND = "/api/v2/assessments/conversations/conv-1/respond";
const DRAFT = "/api/v2/assessments/conversations/conv-1/draft";
const VOICE = "/api/v2/assessments/conversations/conv-1/voice";
const TURN_SEQ = 4;
const TURN_KEY = `conv-1:turn:${TURN_SEQ}`;

const BEHAVIOUR: AnswerBehaviour = {
  keydown_offsets_ms: [10, 40],
  backspace_offsets_ms: [],
  blocked_action_count: 0,
  focus_ms: 900,
  mouse_samples: 3,
  mouse_path_px: 40,
  mouse_idle_ms: 0,
  mouse_clicks: 1,
  option_click_offsets_ms: [],
  scroll_events: 0,
};

/** The bridge the shell would provide, with every method a mock. Typed
 *  generics on `vi.fn` so the fake is checked against the real contract. */
function fakeBridge(overrides: Partial<ProctoringBridge> = {}): ProctoringBridge & {
  hooks: ProctoringFieldHooks;
} {
  const hooks: ProctoringFieldHooks = {
    onFieldFocus: vi.fn<() => void>(),
    onFieldBlur: vi.fn<() => void>(),
    onKeyDown: vi.fn<(timeStampMs: number, isDeletion: boolean) => void>(),
    onBlockedAction: vi.fn(),
    onOptionClick: vi.fn<(timeStampMs: number) => void>(),
    onScroll: vi.fn<() => void>(),
  };
  return {
    status: "active",
    sessionId: "ps-1",
    warningsUsed: 0,
    maxWarnings: 3,
    endedMessage: null,
    paused: false,
    hooks,
    fieldHooksFor: vi.fn<(questionKey: string) => ProctoringFieldHooks>(() => hooks),
    collectAnswerBehaviour: vi.fn<(questionKey: string) => AnswerBehaviour | null>(
      () => BEHAVIOUR
    ),
    onConversationEnded: vi.fn<(status: "completed" | "terminated") => void>(),
    ...overrides,
  };
}

/** A clock `secondsLeft` from the server's own now. */
function clock(secondsLeft: number, overrides: Partial<TurnClock> = {}): TurnClock {
  const serverNow = Date.now();
  return {
    kind: "prose",
    allocation_seconds: 180,
    deadline_at: new Date(serverNow + secondsLeft * 1000).toISOString(),
    server_now: new Date(serverNow).toISOString(),
    paused: false,
    pause_reason: null,
    ...overrides,
  };
}

function turn(overrides: Partial<ConversationTurn> = {}): ConversationTurn {
  return {
    conversation_id: "conv-1",
    status: "active",
    prompt: "Tell us about the migration.",
    progress_label: "Question 1 of 12",
    answered_questions: 0,
    total_questions: 12,
    is_reask: false,
    turn_seq: TURN_SEQ,
    turn: clock(180),
    voice_input_available: false,
    history: [],
    question: {
      id: "q-1",
      question_type: "short_answer",
      payload: {},
      time_allocation_seconds: 180,
    },
    ...overrides,
  };
}

const MCQ = {
  id: "q-mcq",
  question_type: "mcq_single" as const,
  payload: {
    options: [
      { id: "a", text: "Index the join column" },
      { id: "b", text: "Drop the foreign key" },
    ],
    select_count: 1,
  },
  time_allocation_seconds: 60,
};

/** Route the mocked POSTs by path: `start` answers from a queue (the last
 *  entry repeats), `respond` from its own. */
function server(starts: ConversationTurn[], responds: Array<ConversationTurn | Error> = []) {
  const startQueue = [...starts];
  const respondQueue = [...responds];
  apiPost.mockImplementation(async (path: string) => {
    if (path === START) {
      return startQueue.length > 1 ? startQueue.shift() : startQueue[0];
    }
    if (path === RESPOND) {
      const next = respondQueue.shift();
      if (next instanceof Error) throw next;
      if (!next) throw new Error("respond was not expected in this test");
      return next;
    }
    throw new Error(`unexpected POST ${path}`);
  });
}

function mount(bridge: ProctoringBridge) {
  return render(
    <ProctoringProvider value={bridge}>
      <AssessmentConversation linkId={LINK} />
    </ProctoringProvider>
  );
}

const respondCalls = () => apiPost.mock.calls.filter(([path]) => path === RESPOND);
const startCalls = () => apiPost.mock.calls.filter(([path]) => path === START);

beforeEach(() => {
  resetMonacoDouble();
  window.localStorage.clear();
  apiPost.mockReset();
  apiPut.mockReset();
  apiGet.mockReset();
  apiUpload.mockReset();
  toast.mockReset();
  apiPut.mockResolvedValue(undefined);
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe("respond", () => {
  it("names the server's turn and sends no time the browser measured", async () => {
    server([turn()], [turn({ turn_seq: 5, prompt: "Next", history: [] })]);
    mount(fakeBridge());
    const box = await screen.findByLabelText("Your answer");
    fireEvent.change(box, { target: { value: "I owned the cutover plan." } });
    fireEvent.click(screen.getByRole("button", { name: /^Send$/ }));

    await waitFor(() => expect(respondCalls()).toHaveLength(1));
    const [, body] = respondCalls()[0];
    expect(body).toEqual({
      turn_seq: TURN_SEQ,
      answer: "I owned the cutover plan.",
      behaviour: BEHAVIOUR,
    });
    // Built from parts so the removal sweeps, which fail on the retired
    // field's name anywhere in the tree, do not read this assertion as a use.
    expect(Object.keys(body)).not.toContain(["paused", "ms"].join("_"));
  });

  it("sends a structured answer as answer_payload", async () => {
    server([turn({ question: MCQ })], [turn({ turn_seq: 5, prompt: "Next" })]);
    mount(fakeBridge());
    fireEvent.click(await screen.findByLabelText("Drop the foreign key"));
    fireEvent.click(screen.getByRole("button", { name: /^Send$/ }));
    await waitFor(() => expect(respondCalls()).toHaveLength(1));
    expect(respondCalls()[0][1]).toEqual({
      turn_seq: TURN_SEQ,
      answer: "",
      answer_payload: { selected_option_id: "b" },
      behaviour: BEHAVIOUR,
    });
  });

  it("refuses to re-file an answer the server says belongs to a turn that has moved on", async () => {
    const moved = turn({
      turn_seq: 5,
      prompt: "The next question",
      history: [
        { question: "Tell us about the migration.", answer: "Sent from another tab." },
      ],
    });
    server(
      [turn(), moved],
      [new ApiError(409, { detail: "This question was already answered" }, "This question was already answered")]
    );
    mount(fakeBridge());
    const box = await screen.findByLabelText("Your answer");
    fireEvent.change(box, { target: { value: "A retry after a lost response" } });
    fireEvent.click(screen.getByRole("button", { name: /^Send$/ }));

    await screen.findByText("The next question");
    // One respond, never retried into the next turn; the screen re-read the
    // server instead.
    expect(respondCalls()).toHaveLength(1);
    expect(startCalls()).toHaveLength(2);
    expect(toast).toHaveBeenCalledWith(
      expect.objectContaining({
        title: "Your answer was not sent",
        description: "This question was already answered",
      })
    );
    expect(screen.getByText("Sent from another tab.")).toBeTruthy();
  });

  it("returns a typed answer to the field when the send never arrived", async () => {
    server([turn()], [new Error("offline")]);
    mount(fakeBridge());
    const box = (await screen.findByLabelText("Your answer")) as HTMLTextAreaElement;
    fireEvent.change(box, { target: { value: "Kept" } });
    fireEvent.click(screen.getByRole("button", { name: /^Send$/ }));
    await waitFor(() => expect(respondCalls()).toHaveLength(1));
    await waitFor(() =>
      expect((screen.getByLabelText("Your answer") as HTMLTextAreaElement).value).toBe("Kept")
    );
  });
});

describe("a coding question", () => {
  const STARTER = "def solve(items):\n    pass\n";
  const PAYLOAD: CodingPayloadViewV2 = {
    payload_version: 2,
    title: "Remove duplicates",
    io: "stdin_stdout",
    input_format: "One line of integers.",
    output_format: "The integers without duplicates.",
    constraints: "Do not sort the input.",
    languages: ["python"],
    starter_code: { python: STARTER },
    visible_tests: [{ id: "v1", stdin: "3 1 3", expected_stdout: "3 1", explanation: "" }],
    limits: {},
  };
  const CODING: QuestionOut = {
    id: "q-coding",
    question_type: "coding",
    payload: PAYLOAD,
    time_allocation_seconds: 1200,
  };

  it("renders inside the player and sends only after the final-answer confirmation", async () => {
    // Without the player's CodingConversationContext the editor's Run hook
    // throws by design, so this render IS the wiring check. Untouched
    // starter code is not an answer; the final send names the language and
    // asks before the hidden tests run.
    server([turn({ question: CODING })], [turn({ turn_seq: 5, prompt: "Next" })]);
    mount(fakeBridge());
    const editor = (await screen.findByTestId("monaco-input")) as HTMLTextAreaElement;
    const submit = screen.getByRole("button", { name: /Submit final answer/ });
    expect(screen.queryByRole("button", { name: /^Send$/ })).toBeNull();
    expect((submit as HTMLButtonElement).disabled).toBe(true);

    fireEvent.change(editor, { target: { value: "print(1)" } });
    await waitFor(() => expect((submit as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(submit);
    expect(respondCalls()).toHaveLength(0);
    const dialog = await screen.findByRole("alertdialog");
    expect(dialog.textContent).toContain("Python");
    fireEvent.click(screen.getAllByRole("button", { name: /Submit final answer/ }).at(-1)!);

    await waitFor(() => expect(respondCalls()).toHaveLength(1));
    expect(respondCalls()[0][1]).toMatchObject({
      turn_seq: TURN_SEQ,
      answer_payload: { language: "python", code: "print(1)" },
    });
  });
});

describe("history", () => {
  it("shows every past exchange from the server, read-only, with no edit control", async () => {
    server([
      turn({
        history: [
          { question: "First question", answer: "My first answer" },
          { question: "A follow-up", answer: "(No answer was given before time ran out.)" },
        ],
      }),
    ]);
    mount(fakeBridge());
    await screen.findByText("My first answer");
    // The server's own sentence for a turn that ran out, verbatim.
    expect(screen.getByText("(No answer was given before time ran out.)")).toBeTruthy();
    expect(screen.getByText("A follow-up")).toBeTruthy();
    expect(screen.queryByRole("button", { name: /edit/i })).toBeNull();
    // The only text box on the page is the current turn's.
    await waitFor(() => expect(screen.getAllByRole("textbox")).toHaveLength(1));
  });
});

describe("the clock", () => {
  it("checks with the server at zero and then submits what was typed as timed out", async () => {
    window.localStorage.setItem(draftKey(LINK, TURN_KEY), JSON.stringify({ text: "Half an answer" }));
    server([turn({ turn: clock(0) }), turn({ turn: clock(0) })], [turn({ turn_seq: 5, prompt: "Next" })]);
    mount(fakeBridge());

    await waitFor(() => expect(respondCalls()).toHaveLength(1));
    // The server was asked before anything was sent.
    expect(startCalls().length).toBeGreaterThanOrEqual(2);
    expect(respondCalls()[0][1]).toEqual({
      turn_seq: TURN_SEQ,
      answer: "Half an answer",
      timed_out: true,
      behaviour: BEHAVIOUR,
    });
  });

  it("sends an empty structured answer empty, with no payload, when time runs out", async () => {
    server(
      [turn({ question: MCQ, turn: clock(0) }), turn({ question: MCQ, turn: clock(0) })],
      [turn({ turn_seq: 5, prompt: "Next" })]
    );
    mount(fakeBridge());
    await waitFor(() => expect(respondCalls()).toHaveLength(1));
    expect(respondCalls()[0][1]).toEqual({
      turn_seq: TURN_SEQ,
      answer: "",
      timed_out: true,
      behaviour: BEHAVIOUR,
    });
  });

  it("says so, rather than sitting at zero, when the server cannot be reached", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let starts = 0;
    apiPost.mockImplementation(async (path: string) => {
      if (path === START) {
        starts += 1;
        if (starts === 1) return turn({ turn: clock(0) });
        throw new Error("offline");
      }
      throw new Error(`unexpected POST ${path}`);
    });
    mount(fakeBridge());
    await waitFor(() => expect(starts).toBe(2));
    // Bounded retries, a few seconds apart, then the notice.
    await act(async () => {
      vi.advanceTimersByTime(3_100);
    });
    await waitFor(() => expect(starts).toBe(3));
    await act(async () => {
      vi.advanceTimersByTime(3_100);
    });
    await waitFor(() => expect(starts).toBe(4));
    expect((await screen.findByRole("alert")).textContent).toBe(EXPIRY_UNREACHABLE);
    await act(async () => {
      vi.advanceTimersByTime(10_000);
    });
    expect(starts).toBe(4);
    expect(respondCalls()).toHaveLength(0);
  });

  it("returns a refused answer to the field while the session is paused", async () => {
    server(
      [turn(), turn({ status: "paused", turn: clock(100, { paused: true, pause_reason: "device_loss" }) })],
      [new ApiError(409, { detail: "The assessment is paused." }, "The assessment is paused.")]
    );
    mount(fakeBridge());
    const box = await screen.findByLabelText("Your answer");
    fireEvent.change(box, { target: { value: "Still mine" } });
    fireEvent.click(screen.getByRole("button", { name: /^Send$/ }));
    await waitFor(() => expect(startCalls()).toHaveLength(2));
    await waitFor(() =>
      expect((screen.getByLabelText("Your answer") as HTMLTextAreaElement).value).toBe("Still mine")
    );
    expect(toast).toHaveBeenCalledWith(
      expect.objectContaining({ description: "The assessment is paused." })
    );
  });

  it("submits nothing when the server's deadline moved past zero", async () => {
    server([turn({ turn: clock(0) }), turn({ turn: clock(60) })]);
    mount(fakeBridge());
    await waitFor(() => expect(startCalls()).toHaveLength(2));
    await waitFor(() =>
      expect(screen.getByTestId("turn-timer-value").textContent).toMatch(/^(1:00|0:59)$/)
    );
    expect(respondCalls()).toHaveLength(0);
  });

  it("holds a paused turn still and refuses input until it resumes", async () => {
    server([
      turn({
        status: "paused",
        turn: clock(90, { paused: true, pause_reason: "device_loss" }),
      }),
    ]);
    mount(fakeBridge());
    const box = (await screen.findByLabelText("Your answer")) as HTMLTextAreaElement;
    expect(box.disabled).toBe(true);
    expect(screen.getByText("Paused while your camera or microphone is restored")).toBeTruthy();
    expect(screen.getByTestId("turn-timer").getAttribute("data-paused")).toBe("true");
    expect((screen.getByRole("button", { name: /^Send$/ }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("freezes while the proctoring layer holds the session and re-reads the server after", async () => {
    server([turn()]);
    const bridge = fakeBridge();
    const view = mount(bridge);
    await screen.findByLabelText("Your answer");
    expect(startCalls()).toHaveLength(1);

    view.rerender(
      <ProctoringProvider value={{ ...bridge, paused: true }}>
        <AssessmentConversation linkId={LINK} />
      </ProctoringProvider>
    );
    expect((screen.getByLabelText("Your answer") as HTMLTextAreaElement).disabled).toBe(true);
    expect(screen.getByTestId("turn-timer").getAttribute("data-paused")).toBe("true");

    view.rerender(
      <ProctoringProvider value={{ ...bridge, paused: false }}>
        <AssessmentConversation linkId={LINK} />
      </ProctoringProvider>
    );
    await waitFor(() => expect(startCalls()).toHaveLength(2));
  });
});

describe("the draft", () => {
  it("reaches the server named by its turn, no more than once an interval", async () => {
    server([turn()]);
    mount(fakeBridge());
    const box = await screen.findByLabelText("Your answer");
    fireEvent.change(box, { target: { value: "First words" } });
    await waitFor(() => expect(apiPut).toHaveBeenCalledTimes(1), { timeout: 2000 });
    expect(apiPut).toHaveBeenCalledWith(DRAFT, { turn_seq: TURN_SEQ, answer: "First words" });
    await screen.findByText("Draft saved");

    fireEvent.change(box, { target: { value: "First words and more" } });
    // Inside the interval nothing else is sent; the latest value waits.
    await new Promise((resolve) => setTimeout(resolve, 800));
    expect(apiPut).toHaveBeenCalledTimes(1);
    expect(screen.getByText("Saving your draft")).toBeTruthy();
  });

  it("keeps the local copy so a reload puts the words back", async () => {
    server([turn()]);
    const first = mount(fakeBridge());
    const box = await screen.findByLabelText("Your answer");
    fireEvent.change(box, { target: { value: "Survives a reload" } });
    await waitFor(() =>
      expect(window.localStorage.getItem(draftKey(LINK, TURN_KEY))).toBe(
        JSON.stringify({ text: "Survives a reload" })
      )
    );
    first.unmount();
    mount(fakeBridge());
    const restored = (await screen.findByLabelText("Your answer")) as HTMLTextAreaElement;
    expect(restored.value).toBe("Survives a reload");
  });

  it("opens exactly one capture per turn through the bridge", async () => {
    server([turn()]);
    const bridge = fakeBridge();
    mount(bridge);
    await screen.findByLabelText("Your answer");
    expect(bridge.fieldHooksFor).toHaveBeenCalledTimes(1);
    expect(bridge.fieldHooksFor).toHaveBeenCalledWith(TURN_KEY);
  });
});

// ── Spoken answers ─────────────────────────────────────────────────────────

class FakeRecorder {
  static isTypeSupported = (type: string) => type === "audio/webm;codecs=opus";
  state: "inactive" | "recording" = "inactive";
  mimeType = "audio/webm;codecs=opus";
  ondataavailable: ((event: { data: Blob }) => void) | null = null;
  onstop: (() => void) | null = null;
  onerror: (() => void) | null = null;
  start() {
    this.state = "recording";
  }
  stop() {
    this.state = "inactive";
    this.ondataavailable?.({ data: new Blob(["voice"], { type: this.mimeType }) });
    this.onstop?.();
  }
}

function installMicrophone(available = true) {
  const track = { stop: vi.fn() };
  Object.defineProperty(window.navigator, "mediaDevices", {
    configurable: true,
    value: {
      getUserMedia: available
        ? vi.fn().mockResolvedValue({ getTracks: () => [track] })
        : vi.fn().mockRejectedValue(Object.assign(new Error("denied"), { name: "NotAllowedError" })),
    },
  });
  vi.stubGlobal("MediaRecorder", FakeRecorder);
  return track;
}

describe("spoken answers", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("are not offered when the server cannot transcribe, nor on a structured question", async () => {
    server([turn({ voice_input_available: false })]);
    const first = mount(fakeBridge());
    await screen.findByLabelText("Your answer");
    expect(screen.queryByRole("button", { name: "Answer by speaking" })).toBeNull();
    // Disabled is explained, never silent.
    expect(screen.getByTestId("voice-not-offered").textContent).toContain("not switched on");
    first.unmount();

    server([turn({ voice_input_available: true, question: MCQ })]);
    mount(fakeBridge());
    await screen.findByLabelText("Drop the foreign key");
    expect(screen.queryByRole("button", { name: "Answer by speaking" })).toBeNull();
    expect(screen.queryByTestId("voice-not-offered")).toBeNull();
  });

  it("submit the final transcript by its id, shown read-only, never an edited copy", async () => {
    installMicrophone();
    const next = turn({ turn_seq: 5, prompt: "Next" });
    apiPost.mockImplementation(async (path: string) => {
      if (path === START) return turn({ voice_input_available: true });
      if (path === `${VOICE}/begin`) {
        return { id: "v-1", status: "recording", transcript: null, max_seconds: 180 };
      }
      if (path === RESPOND) return next;
      throw new Error(`unexpected POST ${path}`);
    });
    apiUpload.mockResolvedValue({ id: "v-1", status: "uploaded", transcript: null, max_seconds: 180 });
    apiGet.mockResolvedValue({
      id: "v-1",
      status: "transcribed",
      transcript: "I split the ledger into three cutovers.",
      max_seconds: 180,
    });
    mount(fakeBridge());

    fireEvent.click(await screen.findByRole("button", { name: "Answer by speaking" }));
    await screen.findByTestId("voice-recording");
    expect(apiPost).toHaveBeenCalledWith(`${VOICE}/begin`, { turn_seq: TURN_SEQ });
    // Typing is not offered while a spoken answer is being captured.
    expect(screen.queryByLabelText("Your answer")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Stop and send" }));
    await waitFor(() => expect(apiUpload).toHaveBeenCalledTimes(1));
    expect(apiUpload.mock.calls[0][0]).toBe(`${VOICE}/v-1/audio`);
    expect(screen.getByTestId("turn-timer").getAttribute("data-paused")).toBe("true");

    await waitFor(() => expect(respondCalls()).toHaveLength(1), { timeout: 4000 });
    expect(respondCalls()[0][1]).toEqual({
      turn_seq: TURN_SEQ,
      answer: "",
      voice_answer_id: "v-1",
      behaviour: BEHAVIOUR,
    });
  });

  it("fall back to typing after a failed transcription is acknowledged", async () => {
    installMicrophone();
    apiPost.mockImplementation(async (path: string) => {
      if (path === START) return turn({ voice_input_available: true });
      if (path === `${VOICE}/begin`) {
        return { id: "v-1", status: "recording", transcript: null, max_seconds: 180 };
      }
      if (path === `${VOICE}/v-1/acknowledge`) return undefined;
      throw new Error(`unexpected POST ${path}`);
    });
    apiUpload.mockResolvedValue({ id: "v-1", status: "uploaded", transcript: null, max_seconds: 180 });
    apiGet.mockResolvedValue({ id: "v-1", status: "failed", transcript: null, max_seconds: 180 });
    mount(fakeBridge());

    fireEvent.click(await screen.findByRole("button", { name: "Answer by speaking" }));
    fireEvent.click(await screen.findByRole("button", { name: "Stop and send" }));
    await screen.findByTestId("voice-failed", undefined, { timeout: 4000 });
    // The clock waited for the failure and still waits until it is read.
    expect(screen.getByTestId("turn-timer").getAttribute("data-paused")).toBe("true");

    fireEvent.click(screen.getByRole("button", { name: "Type my answer instead" }));
    await screen.findByLabelText("Your answer");
    expect(apiPost).toHaveBeenCalledWith(`${VOICE}/v-1/acknowledge`);
    expect(screen.queryByRole("button", { name: "Answer by speaking" })).toBeNull();
    expect(respondCalls()).toHaveLength(0);
  });

  it("offer typing at once when the microphone cannot be opened, creating no capture", async () => {
    installMicrophone(false);
    server([turn({ voice_input_available: true })]);
    mount(fakeBridge());
    fireEvent.click(await screen.findByRole("button", { name: "Answer by speaking" }));
    await screen.findByTestId("voice-unavailable");
    expect(screen.getByLabelText("Your answer")).toBeTruthy();
    expect(apiPost).not.toHaveBeenCalledWith(`${VOICE}/begin`, expect.anything());
  });
});

describe("waiting and ending", () => {
  it("asks again while the questions are being prepared", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    server([turn({ status: "preparing", prompt: null, question: null, turn: null }), turn()]);
    mount(fakeBridge());
    await screen.findByTestId("preparing");
    await act(async () => {
      vi.advanceTimersByTime(5_100);
    });
    await screen.findByLabelText("Your answer");
    expect(startCalls()).toHaveLength(2);
  });

  it("shows the termination message and tells the bridge once", async () => {
    server(
      [turn()],
      [
        turn({
          status: "terminated",
          prompt: null,
          question: null,
          turn: null,
          termination_message: "The assessment was ended because a second person was seen.",
        }),
      ]
    );
    const bridge = fakeBridge();
    mount(bridge);
    const box = await screen.findByLabelText("Your answer");
    fireEvent.change(box, { target: { value: "An answer" } });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /^Send$/ }));
    });
    expect((await screen.findByTestId("termination-notice")).textContent).toContain(
      "a second person was seen"
    );
    expect(bridge.onConversationEnded).toHaveBeenCalledTimes(1);
    expect(bridge.onConversationEnded).toHaveBeenCalledWith("terminated");
    expect(screen.queryByLabelText("Your answer")).toBeNull();
  });

  it("tells the bridge the conversation completed", async () => {
    server([turn({ status: "completed", prompt: null, question: null, turn: null, answered_questions: 12 })]);
    const bridge = fakeBridge();
    mount(bridge);
    await screen.findByText("Assessment complete");
    expect(bridge.onConversationEnded).toHaveBeenCalledWith("completed");
    expect(bridge.fieldHooksFor).not.toHaveBeenCalled();
  });
});
