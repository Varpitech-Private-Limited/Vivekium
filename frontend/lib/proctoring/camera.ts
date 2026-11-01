/**
 * The camera: stream, frame sampling, the rate policy and stream recovery
 * (proctoring spec 3.6, 4.1, 4.6).
 *
 * THE FRAME'S LIFE. On each tick a bitmap is created from the video element,
 * transferred to the inference worker, and the worker closes it after
 * drawing it once. This thread never holds a frame past the `postMessage`
 * call, never draws one to a visible canvas, never encodes one, and the video
 * element it samples from is never attached to the document. What comes back
 * is `FrameDetections`, which `DetectionRules` turns into event requests.
 *
 * THE RATE. `sampling_fps_normal` by default; `sampling_fps_confirming` for
 * `confirming_window_seconds` after the rules say something may be present;
 * `sampling_fps_degraded` for the rest of the session once the device has
 * shown it cannot keep up. "Cannot keep up" is measured, not guessed: over one
 * heartbeat interval's worth of frames the worker's mean inference time
 * exceeded the normal frame interval. The downgrade is reported ONCE as
 * SESSION_QUALITY_DEGRADED, because a slow laptop is context for the report
 * and never a reason to stop a candidate (spec 3.6).
 *
 * A FRAME THAT FAILS IS COUNTED, NOT HIDDEN (mission item A, 2026-11-01). A
 * frame the worker could not process used to vanish in a bare catch, so a
 * worker that errored on every frame looked, from outside, like a camera that
 * saw nothing. Now each failure is counted; a run of them as long as one
 * heartbeat interval's worth of frames is reported ONCE per run as
 * SESSION_QUALITY_DEGRADED with the note `camera_analysis_failing`, which the
 * Proctoring Report states as a period a phone could have gone unnoticed. The
 * counts, the mean inference time and the best phone confidence seen (even
 * below the event threshold) go to the server with each heartbeat
 * (`takeDetectionStats`). Numbers only: no frame, no label list.
 *
 * A DEAD STREAM (Phase 3, 2026-09-24: a loss PAUSES, it no longer ends). The
 * track ending is either the candidate revoking the permission or the device
 * failing. The permission API says which, where the browser offers it; where
 * it does not, a re-acquire refused with NotAllowedError is the same answer.
 * Either way the monitor tells `onLost`, keeps itself alive and can be brought
 * back: a failed stream is retried on its own every `DEVICE_RETRY_MS`, and a
 * revoked permission waits for the candidate, either for the browser to say
 * the permission is granted again or for them to press "Try again" on the
 * pause screen (`reacquire`). Retrying a revoked permission on a timer would
 * put a browser prompt in front of somebody every second. Which event each of
 * those becomes, and when, is `device-watch.ts`'s business, not this file's.
 */
import type { DetectionStats } from "./api";
import type { ProctoringClientConfig } from "./config";
import { seconds } from "./config";
import { DetectionRules, type FrameDetections } from "./detections";
import { DEVICE_RETRY_MS, type LossKind } from "./device-watch";
import { OBJECT_EVENT_FOR_LABEL } from "./catalog";
import type { EventDraft } from "./events";
import type { InferenceClient } from "./worker-client";

type CameraConfig = Pick<
  ProctoringClientConfig,
  | "sampling_fps_normal"
  | "sampling_fps_confirming"
  | "confirming_window_seconds"
  | "sampling_fps_degraded"
  | "identity_check_interval_seconds"
  | "heartbeat_interval_seconds"
>;

export interface CameraOptions {
  config: CameraConfig;
  inference: InferenceClient;
  rules: DetectionRules;
  onEvent: (draft: EventDraft) => void;
  /** Every frame's detections, when the caller wants them. The system check
   *  uses it to read the latest frame; the session does not, because the
   *  rules are the only consumer there. */
  onFrame?: (frame: FrameDetections) => void;
  /** Whether the identity comparison runs on this session. Off during the
   *  system check, where the baseline is taken instead. */
  identityChecks: boolean;
  /** The camera stopped being usable. Called once per outage, and again only
   *  if a stream failure turns out to be a revoked permission. */
  onLost: (kind: LossKind) => void;
  /** A fresh stream is attached after an outage. */
  onRecovered: (stream: MediaStream) => void;
  navigator?: Navigator;
  document?: Document;
  now?: () => number;
}

export interface CameraStatus {
  live: boolean;
  measuredFps: number | null;
  degraded: boolean;
  width: number | null;
  height: number | null;
}

export async function openCamera(navigator: Navigator = window.navigator): Promise<MediaStream> {
  return navigator.mediaDevices.getUserMedia({ video: { facingMode: "user" }, audio: false });
}

export function isPermissionDenied(error: unknown): boolean {
  return error instanceof DOMException && (error.name === "NotAllowedError" || error.name === "SecurityError");
}

export class CameraMonitor {
  private stream: MediaStream | null = null;
  private readonly video: HTMLVideoElement;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private stopped = false;
  private inFlight = false;
  private confirmingUntil = 0;
  private degraded = false;
  private lastIdentityCheck: number;
  private readonly inferenceTimes: number[] = [];
  private readonly windowFrames: number;
  private frames = 0;
  private startedAt: number;
  private statProcessed = 0;
  private statFailed = 0;
  private statSince: number;
  private statInferenceMs = 0;
  private statPhoneBest: number | null = null;
  private failureRun = 0;
  /** Why the camera is down, or null while it is live. */
  private lostKind: LossKind | null = null;
  private retryTimer: ReturnType<typeof setTimeout> | null = null;
  private reacquiring: Promise<boolean> | null = null;
  private readonly nav: Navigator;
  private readonly now: () => number;
  private permissionStatus: PermissionStatus | null = null;

  constructor(private readonly options: CameraOptions) {
    this.nav = options.navigator ?? window.navigator;
    this.now = options.now ?? Date.now;
    const doc = options.document ?? document;
    this.video = doc.createElement("video");
    this.video.muted = true;
    this.video.playsInline = true;
    this.windowFrames = options.config.sampling_fps_normal * options.config.heartbeat_interval_seconds;
    this.startedAt = this.now();
    this.lastIdentityCheck = this.startedAt;
    this.statSince = this.startedAt;
  }

  async start(stream?: MediaStream): Promise<void> {
    this.stream = stream ?? (await openCamera(this.nav));
    await this.attach(this.stream);
    this.watchPermission();
    this.startedAt = this.now();
    this.frames = 0;
    this.schedule();
  }

  status(): CameraStatus {
    const track = this.stream?.getVideoTracks()[0];
    const settings = track?.getSettings();
    const elapsed = (this.now() - this.startedAt) / 1000;
    return {
      live: Boolean(track && track.readyState === "live"),
      measuredFps: elapsed > 0 && this.frames > 0 ? this.frames / elapsed : null,
      degraded: this.degraded,
      width: settings?.width ?? null,
      height: settings?.height ?? null,
    };
  }

  /** What the last interval looked like, then start a new one. */
  takeDetectionStats(): DetectionStats {
    const elapsed = (this.now() - this.statSince) / 1000;
    const stats: DetectionStats = {
      frames_processed: this.statProcessed,
      frames_failed: this.statFailed,
      measured_fps: elapsed > 0 && this.statProcessed > 0 ? this.statProcessed / elapsed : null,
      inference_ms_mean: this.statProcessed > 0 ? this.statInferenceMs / this.statProcessed : null,
      phone_best_score: this.statPhoneBest,
    };
    this.statProcessed = 0;
    this.statFailed = 0;
    this.statInferenceMs = 0;
    this.statPhoneBest = null;
    this.statSince = this.now();
    return stats;
  }

  /** The stream being sampled, or null while the camera is down. */
  currentStream(): MediaStream | null {
    return this.lostKind === null ? this.stream : null;
  }

  /**
   * Try to open the camera again now. The pause screen's "Try again" calls
   * this, so it runs inside the candidate's click and a browser that needs a
   * gesture to prompt for the permission gets one. Resolves true when a live
   * stream is attached. Concurrent calls share one attempt, because two
   * `getUserMedia` calls racing would attach one stream and leak the other's
   * camera light.
   */
  reacquire(): Promise<boolean> {
    if (this.stopped) return Promise.resolve(false);
    if (this.lostKind === null) return Promise.resolve(true);
    if (this.reacquiring === null) {
      this.reacquiring = this.attemptRecovery().finally(() => {
        this.reacquiring = null;
      });
    }
    return this.reacquiring;
  }

  /** One frame for the identity worker, outside the sampling loop. Used by
   *  the system check to take the baseline. */
  async snapshot(): Promise<ImageBitmap> {
    return createImageBitmap(this.video);
  }

  /**
   * Stop sampling and let go of the stream WITHOUT stopping its tracks.
   *
   * The system check hands its camera to the session, so the tracks have to
   * outlive the monitor that opened them. Stopping them here and reopening
   * would show the candidate a second permission prompt and a second camera
   * light for the same session.
   */
  detach(): MediaStream | null {
    this.stopped = true;
    if (this.timer !== null) clearTimeout(this.timer);
    this.timer = null;
    this.clearRetry();
    if (this.permissionStatus) this.permissionStatus.onchange = null;
    const stream = this.stream;
    const track = stream?.getVideoTracks()[0];
    if (track) track.onended = null;
    this.stream = null;
    this.video.srcObject = null;
    return stream;
  }

  stop(): void {
    this.detach()?.getTracks().forEach((track) => track.stop());
  }

  private async attach(stream: MediaStream): Promise<void> {
    this.video.srcObject = stream;
    await this.video.play();
    const track = stream.getVideoTracks()[0];
    if (track) track.onended = () => void this.onTrackEnded();
  }

  private watchPermission(): void {
    if (!this.nav.permissions) return;
    this.nav.permissions
      .query({ name: "camera" as PermissionName })
      .then((status) => {
        this.permissionStatus = status;
        status.onchange = () => {
          // "prompt" counts as lost too: the candidate reset the permission,
          // and the stream it guarded is gone either way. "granted" again is
          // the browser handing the choice back, so the camera is reopened
          // without waiting for a click.
          if (status.state === "granted") {
            if (this.lostKind === "permission") void this.reacquire();
          } else {
            this.lose("permission");
          }
        };
      })
      .catch(() => {
        // The browser does not expose the camera permission by name. The
        // track's `ended` handler and a refused re-acquire cover the case.
      });
  }

  private onTrackEnded(): void {
    if (this.stopped) return;
    this.lose(this.permissionStatus?.state === "denied" ? "permission" : "stream");
  }

  /**
   * Stop sampling a dead stream and say so. The monitor stays alive: the
   * sampling loop keeps ticking and skips while there is no live track, so a
   * recovered stream is picked up with no restart.
   */
  private lose(kind: LossKind): void {
    if (this.stopped) return;
    if (this.lostKind === kind || this.lostKind === "permission") return;
    const first = this.lostKind === null;
    this.lostKind = kind;
    if (first) {
      const dead = this.stream;
      this.stream = null;
      this.video.srcObject = null;
      const track = dead?.getVideoTracks()[0];
      if (track) track.onended = null;
      dead?.getTracks().forEach((each) => each.stop());
    }
    this.options.onLost(kind);
    if (kind === "stream") {
      this.scheduleRetry();
    } else {
      this.clearRetry();
    }
  }

  private scheduleRetry(): void {
    this.clearRetry();
    if (this.stopped || this.lostKind !== "stream") return;
    this.retryTimer = setTimeout(() => {
      this.retryTimer = null;
      void this.reacquire().then((live) => {
        if (!live) this.scheduleRetry();
      });
    }, DEVICE_RETRY_MS);
  }

  private clearRetry(): void {
    if (this.retryTimer !== null) clearTimeout(this.retryTimer);
    this.retryTimer = null;
  }

  private async attemptRecovery(): Promise<boolean> {
    let stream: MediaStream;
    try {
      stream = await openCamera(this.nav);
    } catch (error) {
      if (isPermissionDenied(error)) this.lose("permission");
      // Any other refusal (no device, the device busy) leaves the outage as it
      // is: a stream failure keeps retrying and a revoked permission keeps
      // waiting for the candidate.
      return false;
    }
    if (this.stopped) {
      stream.getTracks().forEach((track) => track.stop());
      return false;
    }
    try {
      await this.attach(stream);
    } catch (error) {
      // A stream that opened and would not play is not a recovered camera.
      // The outage stands and the next attempt asks again.
      stream.getTracks().forEach((track) => track.stop());
      this.video.srcObject = null;
      console.warn(
        "proctoring camera could not be resumed: " +
          (error instanceof Error ? error.message : "unknown")
      );
      return false;
    }
    this.stream = stream;
    this.lostKind = null;
    this.clearRetry();
    this.options.onRecovered(stream);
    return true;
  }

  private currentIntervalMs(): number {
    const { config } = this.options;
    if (this.degraded) return 1000 / config.sampling_fps_degraded;
    if (this.now() < this.confirmingUntil) return 1000 / config.sampling_fps_confirming;
    return 1000 / config.sampling_fps_normal;
  }

  private schedule(): void {
    if (this.stopped) return;
    this.timer = setTimeout(() => void this.tick(), this.currentIntervalMs());
  }

  private async tick(): Promise<void> {
    if (this.stopped) return;
    const track = this.stream?.getVideoTracks()[0];
    if (this.inFlight || !track || track.readyState !== "live" || this.video.readyState < this.video.HAVE_CURRENT_DATA) {
      this.schedule();
      return;
    }
    this.inFlight = true;
    const at = this.now();
    try {
      const wantsIdentity =
        this.options.identityChecks &&
        at - this.lastIdentityCheck >= seconds(this.options.config.identity_check_interval_seconds);
      const [frame, identityFrame] = await Promise.all([
        createImageBitmap(this.video),
        wantsIdentity ? createImageBitmap(this.video) : Promise.resolve(null),
      ]);
      const detections = await this.options.inference.detect(frame, at);
      this.frames += 1;
      this.failureRun = 0;
      this.statProcessed += 1;
      this.statInferenceMs += detections.inferenceMs;
      for (const detection of detections.objects) {
        if (OBJECT_EVENT_FOR_LABEL[detection.label] !== "DEVICE_DETECTED_PHONE") continue;
        this.statPhoneBest = Math.max(this.statPhoneBest ?? 0, detection.score);
      }
      this.recordInference(detections.inferenceMs);
      const observed: FrameDetections = {
        at,
        objects: detections.objects,
        faces: detections.faces,
        persons: detections.persons,
        luminance: detections.luminance,
        variance: detections.variance,
      };
      this.options.onFrame?.(observed);
      const outcome = this.options.rules.observe(observed);
      for (const event of outcome.events) this.options.onEvent(event);
      if (outcome.confirming) {
        this.confirmingUntil = at + seconds(this.options.config.confirming_window_seconds);
      }
      if (identityFrame) {
        this.lastIdentityCheck = at;
        const distance = await this.options.inference.check(identityFrame);
        for (const event of this.options.rules.observeIdentity(distance)) this.options.onEvent(event);
      }
    } catch {
      // One bad frame is not an outage, but it is not nothing either: it is
      // counted, and a run of them is reported (see the header).
      this.statFailed += 1;
      this.failureRun += 1;
      if (this.failureRun === this.windowFrames) {
        this.options.onEvent({
          event_type: "SESSION_QUALITY_DEGRADED",
          metadata: { note: "camera_analysis_failing", failed_frames: this.failureRun },
        });
      }
    } finally {
      this.inFlight = false;
      this.schedule();
    }
  }

  private recordInference(inferenceMs: number): void {
    if (this.degraded) return;
    this.inferenceTimes.push(inferenceMs);
    if (this.inferenceTimes.length < this.windowFrames) return;
    const mean = this.inferenceTimes.reduce((sum, value) => sum + value, 0) / this.inferenceTimes.length;
    this.inferenceTimes.length = 0;
    if (mean > 1000 / this.options.config.sampling_fps_normal) {
      this.degraded = true;
      this.options.onEvent({
        event_type: "SESSION_QUALITY_DEGRADED",
        metadata: { note: "inference_below_normal_rate", mean_inference_ms: Math.round(mean) },
      });
    }
  }
}
