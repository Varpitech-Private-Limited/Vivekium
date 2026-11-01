/**
 * The running monitoring session: every detector wired to the one event
 * queue, the one heartbeat and the one self-check.
 *
 * `ProctoringShell` owns the screens; this owns the machinery so the shell
 * stays a state machine over four phases. Everything here is started by
 * `start()` and released by `stop()`, and `stop()` is the only place media is
 * released: a session that ends for any reason, completion, termination or a
 * closed tab, stops every track and terminates both workers.
 *
 * The self-check's four answers come from the parts themselves: the camera's
 * track state, the microphone's track state, the inference client having
 * answered within one heartbeat interval, and the lockdown and focus handles
 * still being installed. Each is a check on the machinery, not on the
 * candidate; the header of lockdown.ts says what that is worth.
 *
 * THE DEVICE PAUSE (Phase 3, 2026-09-24). A lost camera or microphone is no
 * longer an ending. The monitors report the loss to `DeviceWatch`, which turns
 * it into the events the server pauses on; the server's answer (`pause`, on
 * every ingest and heartbeat response) is combined with what this client can
 * see locally into a `PauseView`, and the shell renders the pause screen from
 * that and from nothing else. Three things here exist only because of it:
 *
 *   - THE INTEGRITY CHECK DOES NOT COUNT A DEVICE THE PAUSE ALREADY OWNS. Its
 *     episode rule would otherwise report INTEGRITY_CHECK_FAILED, a Path A
 *     termination, sixty seconds into a two-minute grace, and the candidate
 *     who was promised two minutes would get one. The HEARTBEAT still reports
 *     the device down, because it is down.
 *   - THE DETECTORS FORGET THE OUTAGE (`DetectionRules.resetPresence`), for
 *     the same reason: time without a camera is not time without a face.
 *   - THE SERVER'S CLOCK. The grace deadline is a server timestamp, placed on
 *     this device's clock with the offset each heartbeat measures, so a
 *     candidate whose clock is wrong still sees the true time left.
 *
 * ONE BLOCKED ATTEMPT, ONE EVENT, ONE COUNT. Copy, cut, paste and drop can be
 * caught in two places: the lockdown's listeners on the document, and an
 * answer field's own handlers. The lockdown runs first, in the capture phase,
 * and stops the event, so in practice the field never sees it; the lockdown
 * path therefore both emits the event and counts it against the answer on
 * screen. The field path does the same for an attempt the document did not
 * catch. Should both ever see one attempt, the field defers: the lockdown
 * marks the action for the rest of the current dispatch (cleared in a
 * microtask, which runs only after every synchronous handler of that event),
 * and a field report of the same action inside that window is the same
 * attempt, already recorded.
 */
import type { AnswerBehaviour, ProctoringFieldHooks } from "@/lib/assessment/contracts";

import {
  acknowledgeWarning,
  completeRecordingSegment,
  finalizeRecording,
  isSessionEnded,
  openRecordingSegment,
  postAudioChunk,
  postEvents,
  postHeartbeat,
  uploadRecordingPart,
  type HeartbeatOut,
  type MonitoringStatus,
  type PauseState,
  type SessionMediaStartOut,
  type SessionOut,
  type TerminationOut,
  type WarningOut,
} from "./api";
import { AudioMonitor } from "./audio";
import { CameraMonitor } from "./camera";
import { BehaviourCapture } from "./capture";
import { seconds } from "./config";
import { DetectionRules } from "./detections";
import { DeviceWatch, type Device } from "./device-watch";
import { watchDisplays, type DisplaysHandle } from "./displays";
import { EventQueue, type EventDraft } from "./events";
import { watchFocus, type FocusHandle } from "./focus";
import { startHeartbeat, type HeartbeatHandle } from "./heartbeat";
import { startIntegrityCheck, type IntegrityHandle } from "./integrity";
import { installLockdown, type BlockedAction, type BlockedVia, type LockdownHandle } from "./lockdown";
import { SessionRecorder } from "./recorder";
import type { InferenceClient } from "./worker-client";

export interface SessionMedia {
  camera: MediaStream;
  microphone: MediaStream;
  inference: InferenceClient;
}

/**
 * Everything the pause screen reads. `serverPaused`, the message, the deadline
 * and the two counts are the server's; `lostDevices` is what this browser can
 * see, which is what the screen says in the moment between a loss being
 * reported and the server's answer arriving, and how it knows the devices are
 * back before the server has lifted the pause.
 */
export interface PauseView {
  serverPaused: boolean;
  /** The server's sentence for the open pause, verbatim; null when none is
   *  open or the server did not send one. */
  message: string | null;
  /** The grace deadline on THIS device's clock, or null when none is open. */
  graceDeadlineMs: number | null;
  pausesUsed: number;
  maxPauses: number;
  lostDevices: Device[];
  /** A loss has been reported to the server in the current episode. */
  lossReported: boolean;
}

export const NO_PAUSE: PauseView = {
  serverPaused: false,
  message: null,
  graceDeadlineMs: null,
  pausesUsed: 0,
  maxPauses: 0,
  lostDevices: [],
  lossReported: false,
};

/** Whether the pause screen is up: the server says the assessment is paused,
 *  or a loss has been reported and its answer is still on the way. A glitch
 *  still inside its allowance shows nothing. */
export function pauseScreenOpen(view: PauseView): boolean {
  return view.serverPaused || (view.lossReported && view.lostDevices.length > 0);
}

/**
 * The hooks a field receives. `onBlockedAction` takes the kind of attempt the
 * field caught, so the session's event names it (the report itemises paste
 * attempts). The kind is REQUIRED: the CodeMirror editor that could not name
 * it is gone, and the Monaco editor names every attempt it refuses.
 */
export type SessionFieldHooks = ProctoringFieldHooks;

/** The actions an answer field can be the target of, and so the ones that
 *  count against the answer on screen. */
const FIELD_ACTIONS: ReadonlySet<BlockedAction> = new Set<BlockedAction>(["copy", "cut", "paste", "drop"]);

export interface SessionCallbacks {
  onWarning: (warning: WarningOut, warningsUsed: number) => void;
  onTermination: (termination: TerminationOut) => void;
  /** The server said the session was already over (409). */
  onSessionEnded: (message: string) => void;
  /** The pause picture changed. */
  onPauseChange: (view: PauseView) => void;
  /** A non-blocking reminder for the candidate (the sound-level advisory). It
   *  is not a warning: it holds no clock and counts toward nothing. */
  onAdvisory: (message: string) => void;
}

export class SessionRuntime {
  private readonly queue: EventQueue;
  private readonly rules: DetectionRules;
  private readonly capture: BehaviourCapture;
  private readonly devices: DeviceWatch;
  private camera: CameraMonitor | null = null;
  private audio: AudioMonitor | null = null;
  private lockdown: LockdownHandle | null = null;
  private focus: FocusHandle | null = null;
  private displays: DisplaysHandle | null = null;
  private heartbeat: HeartbeatHandle | null = null;
  private integrity: IntegrityHandle | null = null;
  private recorder: SessionRecorder | null = null;
  private stopped = false;
  /** Server clock minus this device's clock, measured by the heartbeat. */
  private clockOffsetMs = 0;
  private serverPause: PauseState | null = null;
  /**
   * The first pause statement after `start` gets one extra look. A candidate
   * who reloads while a pause is open comes back through the system check, so
   * their devices are live again, but nothing in this new page ever saw them
   * lost and so nothing would tell the server they are back. Without this the
   * page would sit on a pause nobody could lift until the grace ran out and
   * the server ended the session.
   */
  private resumeCheckPending = true;
  /** Field actions the document caught during the event now being
   *  dispatched. See the header. */
  private readonly caughtThisDispatch = new Set<string>();

  constructor(
    private readonly session: SessionOut,
    private readonly media: SessionMedia,
    private readonly callbacks: SessionCallbacks,
    /** The recording the server opened for this session, when it opened one.
     *  Null when opening it failed, which never blocks the assessment. */
    private readonly mediaRecording: SessionMediaStartOut | null = null
  ) {
    const { config } = session;
    this.rules = new DetectionRules(config);
    this.capture = new BehaviourCapture({
      maxKeystrokeSamples: config.max_keystroke_samples,
      mouseSampleHz: config.mouse_sample_hz,
    });
    this.queue = new EventQueue({
      batchMax: config.event_batch_max,
      // The retry backoff is capped at one heartbeat interval. A queue that
      // backed off further would be silent for longer than the server's own
      // liveness window, so the server would be recording a monitoring gap
      // while this client was merely waiting to try again.
      maxBackoffMs: seconds(config.heartbeat_interval_seconds),
      post: (events) => postEvents(session.session_id, events),
      onWarning: callbacks.onWarning,
      onTermination: (termination) => this.terminated(termination),
      onSessionEnded: (message) => this.ended(message),
      onPause: (pause) => this.serverSaid(pause),
    });
    this.devices = new DeviceWatch({
      glitchMs: seconds(config.device_glitch_seconds),
      emit: (draft) => this.emit(draft),
      onChange: () => this.pauseChanged(),
    });
  }

  readonly emit = (draft: EventDraft): void => {
    this.queue.enqueue(draft);
  };

  start(): void {
    const { config } = this.session;
    this.lockdown = installLockdown({ onBlocked: (action, via) => this.documentBlocked(action, via) });
    this.focus = watchFocus({ ignoreUnderMs: seconds(config.focus_loss_ignore_under_seconds), onEvent: this.emit });
    this.displays = watchDisplays({ intervalMs: seconds(config.display_check_interval_seconds), onEvent: this.emit });

    this.camera = new CameraMonitor({
      config,
      inference: this.media.inference,
      rules: this.rules,
      onEvent: this.emit,
      identityChecks: true,
      onLost: (kind) => {
        this.rules.resetPresence();
        this.devices.lost("camera", kind);
      },
      onRecovered: () => {
        this.rules.resetPresence();
        this.devices.recovered("camera");
        this.afterRecovery();
      },
    });
    void this.camera.start(this.media.camera);

    this.audio = new AudioMonitor({
      chunkSeconds: config.audio_chunk_seconds,
      maxChunkBytes: config.audio_max_chunk_bytes,
      uploadEnabled: this.session.audio_analysis_available,
      upload: (chunk) => postAudioChunk(this.session.session_id, chunk),
      sound: config,
      emit: this.emit,
      onAdvisory: this.callbacks.onAdvisory,
      onLost: (kind) => this.devices.lost("microphone", kind),
      onRecovered: () => {
        this.devices.recovered("microphone");
        this.afterRecovery();
      },
      onWarning: this.callbacks.onWarning,
      onTermination: (termination) => this.terminated(termination),
      onSessionEnded: (message) => this.ended(message),
    });
    void this.audio.start(this.media.microphone);

    // The stored record of the session (owner ruling, 2026-09-22). It rides
    // on the streams the monitors already hold rather than asking for a
    // second camera: one permission prompt, one light, one recording.
    if (this.mediaRecording !== null) {
      const conversationId = this.mediaRecording.conversation_id;
      this.recorder = new SessionRecorder({
        recording: this.mediaRecording,
        transport: {
          openSegment: (contentType) => openRecordingSegment(conversationId, contentType),
          uploadPart: (segmentId, partNumber, body, final) =>
            uploadRecordingPart(conversationId, segmentId, partNumber, body, final),
          completeSegment: (segmentId) => completeRecordingSegment(conversationId, segmentId),
          finalize: () => finalizeRecording(conversationId),
        },
        // Logged and never surfaced as a failure of the assessment: a
        // recording that did not arrive is a status the hiring team reads,
        // not a reason to stop a candidate mid-answer.
        onProblem: (reason) => console.warn("assessment recording: " + reason),
      });
      this.recorder.start(this.media.camera, this.media.microphone);
    }

    this.integrity = startIntegrityCheck({
      intervalMs: seconds(config.heartbeat_interval_seconds),
      terminationMs: seconds(config.integrity_failure_termination_seconds),
      probe: () => this.integrityProbe(),
      onEvent: this.emit,
    });
    this.heartbeat = startHeartbeat({
      intervalMs: seconds(config.heartbeat_interval_seconds),
      post: (body) => postHeartbeat(this.session.session_id, body),
      monitoring: () => {
        this.integrity?.check();
        return this.probe();
      },
      detection: () => this.camera?.takeDetectionStats() ?? null,
      identityMatched: () => this.rules.identityMatched,
      onTermination: (termination) => this.terminated(termination),
      onSessionEnded: (message) => this.ended(message),
      onResult: (result, sentAt, receivedAt) => this.heartbeatAnswered(result, sentAt, receivedAt),
    });

    // The pause as the server held it when this session was opened. After a
    // reload in the middle of a pause this is the first statement the page
    // gets, and it arrives with the devices live again (the system check just
    // passed), so the resume check below lifts the pause at once rather than
    // leaving it to run out.
    if (this.session.pause) this.serverSaid(this.session.pause);
  }

  /** What each part of the machinery says about itself, device loss included:
   *  the heartbeat reports this as it is. */
  probe(): MonitoringStatus {
    const withinMs = seconds(this.session.config.heartbeat_interval_seconds);
    return {
      camera: this.camera?.status().live ?? false,
      microphone: this.audio?.live() ?? false,
      models: this.media.inference.responsive(withinMs),
      handlers: Boolean(this.lockdown?.installed() && this.focus?.installed()),
    };
  }

  /** The same probe with a device the pause already owns counted as fine,
   *  for the integrity episode rule only. See the header. */
  integrityProbe(): MonitoringStatus {
    const status = this.probe();
    return {
      ...status,
      camera: status.camera || this.devices.isLost("camera"),
      microphone: status.microphone || this.devices.isLost("microphone"),
    };
  }

  pauseView(): PauseView {
    const pause = this.serverPause;
    const deadline = pause?.paused && pause.grace_deadline_at ? Date.parse(pause.grace_deadline_at) : NaN;
    return {
      serverPaused: Boolean(pause?.paused),
      message: pause?.paused ? pause.message ?? null : null,
      graceDeadlineMs: Number.isFinite(deadline) ? deadline - this.clockOffsetMs : null,
      pausesUsed: pause?.pauses_used ?? 0,
      maxPauses: pause?.max_pauses ?? 0,
      lostDevices: this.devices.lostDevices(),
      lossReported: this.devices.reportedThisEpisode(),
    };
  }

  /**
   * The pause screen's "Try again": reopen every lost device now, inside the
   * candidate's click. Resolves true when nothing is lost any more.
   */
  async retryDevices(): Promise<boolean> {
    const attempts: Array<Promise<boolean>> = [];
    if (this.devices.isLost("camera") && this.camera) attempts.push(this.camera.reacquire());
    if (this.devices.isLost("microphone") && this.audio) attempts.push(this.audio.reacquire());
    await Promise.all(attempts);
    return this.devices.lostDevices().length === 0;
  }

  /** Ask the server now rather than at the next beat. Called when the grace
   *  the screen was counting down reaches zero, so the ending it decides is
   *  shown at once. */
  checkNow(): void {
    void this.heartbeat?.beat();
  }

  /**
   * The candidate acknowledged a warning. The server holds the question timer
   * from the warning to this call. A 409 means the session is already over; any
   * other failure is logged, because the server caps the hold on its own side
   * and there is nothing the candidate could do about it.
   */
  async acknowledgeWarning(): Promise<void> {
    try {
      await acknowledgeWarning(this.session.session_id);
    } catch (error) {
      if (isSessionEnded(error)) {
        this.ended((error as Error).message);
        return;
      }
      console.warn(
        "warning acknowledgement not recorded: " + (error instanceof Error ? error.message : "unknown")
      );
    }
  }

  fieldHooksFor(questionKey: string): SessionFieldHooks {
    const hooks = this.capture.hooksFor(questionKey);
    return {
      ...hooks,
      onBlockedAction: (kind) => {
        if (this.caughtThisDispatch.has(kind)) return;
        hooks.onBlockedAction(kind);
        this.emit({
          event_type: "BLOCKED_ACTION_ATTEMPTED",
          metadata: { action: kind, via: "field" },
        });
      },
    };
  }

  collectAnswerBehaviour(questionKey: string): AnswerBehaviour | null {
    return this.capture.collect(questionKey);
  }

  requestFullscreen(): Promise<boolean> {
    return this.focus?.requestFullscreen() ?? Promise.resolve(false);
  }

  /** Send whatever is queued now. Called before the session is torn down. */
  flush(): Promise<void> {
    return this.queue.flush();
  }

  stop(): void {
    if (this.stopped) return;
    this.stopped = true;
    this.heartbeat?.stop();
    this.integrity?.stop();
    this.queue.stop();
    this.devices.stop();
    // BEFORE the tracks are released. A recorder whose tracks have already
    // ended produces nothing, and the last thing this session does is hand
    // over the recording of it.
    this.recorder?.stop();
    this.camera?.stop();
    this.audio?.stop();
    this.displays?.release();
    this.focus?.release();
    this.lockdown?.release();
    this.capture.release();
    this.media.inference.terminate();
    // Truthy rather than `!== null`: a browser without the fullscreen API has
    // no element to report and no `exitFullscreen` to call.
    if (typeof document !== "undefined" && document.fullscreenElement) {
      void document.exitFullscreen().catch(() => undefined);
    }
  }

  private documentBlocked(action: BlockedAction, via?: BlockedVia): void {
    this.emit({
      event_type: "BLOCKED_ACTION_ATTEMPTED",
      metadata: via ? { action, via } : { action },
    });
    // A script reading the clipboard is aimed at no field; an event is.
    if (via !== undefined || !FIELD_ACTIONS.has(action)) return;
    this.capture.recordBlocked();
    this.caughtThisDispatch.add(action);
    queueMicrotask(() => this.caughtThisDispatch.delete(action));
  }

  private afterRecovery(): void {
    if (this.devices.lostDevices().length > 0) return;
    const camera = this.camera?.currentStream() ?? null;
    const microphone = this.audio?.currentStream() ?? null;
    // A new segment on the new tracks: the old recorder is bound to tracks
    // that ended. Both streams must be live, or the segment would record one
    // device on the tracks of a session that has not recovered.
    if (camera && microphone) this.recorder?.restart(camera, microphone);
  }

  private heartbeatAnswered(result: HeartbeatOut, sentAt: number, receivedAt: number): void {
    const serverNow = Date.parse(result.server_time);
    if (Number.isFinite(serverNow)) {
      // The server stamped its time somewhere between the request leaving and
      // the answer arriving; the midpoint is the best estimate of when.
      this.clockOffsetMs = serverNow - (sentAt + receivedAt) / 2;
    }
    if (result.pause) this.serverSaid(result.pause);
  }

  private serverSaid(pause: PauseState): void {
    if (this.stopped) return;
    this.serverPause = pause;
    if (this.resumeCheckPending) {
      this.resumeCheckPending = false;
      if (pause.paused && this.devices.lostDevices().length === 0) {
        this.emit({ event_type: "DEVICE_RECOVERED", metadata: { devices: [], resumed: true } });
      }
    }
    this.pauseChanged();
  }

  private pauseChanged(): void {
    if (this.stopped) return;
    this.callbacks.onPauseChange(this.pauseView());
  }

  private terminated(termination: TerminationOut): void {
    if (this.stopped) return;
    this.stop();
    this.callbacks.onTermination(termination);
  }

  private ended(message: string): void {
    if (this.stopped) return;
    this.stop();
    this.callbacks.onSessionEnded(message);
  }
}
