/**
 * The microphone: voice activity and the audio chunk (proctoring spec 3.4).
 *
 * THE ONE PIECE OF MEDIA THAT LEAVES THE BROWSER, and the rules around it:
 * chunks of `audio_chunk_seconds`, uploaded ONLY when speech was heard in
 * that chunk and ONLY when the deployment has an analysis service
 * (`audio_analysis_available`); silence is never uploaded; a chunk over
 * `audio_max_chunk_bytes` is not uploaded either. The chunk goes to the one
 * route that analyses it in memory and destroys it, and this thread drops its
 * reference the moment the request is made. Nothing is played back, stored,
 * or logged.
 *
 * WHY THE RECORDER IS RESTARTED RATHER THAN SLICED. `MediaRecorder` with a
 * timeslice hands back fragments of one continuous file, and only the first
 * carries the container header, so every later fragment is undecodable on
 * its own. Stopping and restarting every chunk produces complete files the
 * analysis service can decode independently.
 *
 * VOICE ACTIVITY. An AnalyserNode is polled for the RMS energy of the signal;
 * a chunk is "speech present" when its loudest reading exceeds its quietest
 * by `SPEECH_ENERGY_RATIO`. That is the VAD's own calibration, the one
 * number in this client that is not on the session config, because the
 * config has no field for it; it is named here so it is visible and it errs
 * toward uploading, because a false "silence" would hide a second voice and
 * a false "speech" costs one analysis call. The SERVER decides speaker
 * counts; this module only decides whether to ask.
 *
 * SPEECH DURING A QUESTION THAT TAKES NO SPOKEN ANSWER (Phase 3, 2026-09-24).
 * The same chunk is what the server reads to log a candidate talking while an
 * MCQ, a blank or a code editor is on screen. The server decides it, against
 * its own record of the spoken answers open around the time the chunk
 * arrived; this module sends the audio and nothing about when it was heard.
 *
 * SUSTAINED HIGH SOUND LEVEL (mission item B, 2026-11-01). The same analyser
 * also feeds `SoundLevelDetector` (`sound-level.ts`), which raises an
 * ADVISORY, never a warning, when the level stays well above the room's own
 * noise floor. It runs whether or not an analysis service exists, because it
 * is local arithmetic over the browser's own stream. It does not judge a
 * reading while the candidate is dictating a spoken answer: the voice-answer
 * component sets `setVoiceRecordingActive` (`lib/assessment/voice-activity.ts`)
 * for the whole recording phase, and this monitor reads
 * `isVoiceRecordingActive`. One shared flag, owned by the voice side, because
 * the recorder and this monitor are built by different parts of the page.
 *
 * A LOST MICROPHONE PAUSES the assessment (see `device-watch.ts`). This
 * monitor reports the loss through `onLost`, retries a failed stream on its
 * own every `DEVICE_RETRY_MS`, waits for the candidate on a revoked
 * permission, and hands a recovered stream to `onRecovered` so the session
 * recording can start a new segment on it.
 */
import type { AudioChunkOut, TerminationOut, WarningOut } from "./api";
import { isVoiceRecordingActive } from "@/lib/assessment/voice-activity";

import { isSessionEnded } from "./api";
import { isPermissionDenied } from "./camera";
import { DEVICE_RETRY_MS, type LossKind } from "./device-watch";
import type { EventDraft } from "./events";
import {
  rmsToDbfs,
  SOUND_LEVEL_ADVISORY,
  SoundLevelDetector,
  spectrumCharacter,
  type SoundLevelConfig,
} from "./sound-level";

/** See the header. Quietest-to-loudest RMS ratio within one chunk. */
export const SPEECH_ENERGY_RATIO = 4;

/**
 * How often the analyser is read. A measurement resolution, not a threshold:
 * speech is hundreds of milliseconds long, and reading ten times a second
 * cannot miss it while costing nothing the assessment would notice.
 */
export const VAD_POLL_MS = 100;

export interface AudioOptions {
  chunkSeconds: number;
  maxChunkBytes: number;
  /** `SessionOut.audio_analysis_available`. When false nothing is recorded. */
  uploadEnabled: boolean;
  upload: (chunk: Blob) => Promise<AudioChunkOut>;
  /** The sound-level rules (a subset of the session config). */
  sound: SoundLevelConfig;
  /** Where a sound-level advisory event goes (the session's event queue). */
  emit: (draft: EventDraft) => void;
  /** The candidate-facing reminder, shown without interrupting the answer. */
  onAdvisory: (message: string) => void;
  onLost: (kind: LossKind) => void;
  onRecovered: (stream: MediaStream) => void;
  onWarning: (warning: WarningOut, warningsUsed: number) => void;
  onTermination: (termination: TerminationOut) => void;
  onSessionEnded: (message: string) => void;
  navigator?: Navigator;
  now?: () => number;
}

export async function openMicrophone(navigator: Navigator = window.navigator): Promise<MediaStream> {
  return navigator.mediaDevices.getUserMedia({ audio: true, video: false });
}

function preferredMimeType(): string | undefined {
  if (typeof MediaRecorder === "undefined") return undefined;
  for (const candidate of ["audio/webm;codecs=opus", "audio/webm"]) {
    if (MediaRecorder.isTypeSupported(candidate)) return candidate;
  }
  return undefined;
}

export class AudioMonitor {
  private stream: MediaStream | null = null;
  private context: AudioContext | null = null;
  private analyser: AnalyserNode | null = null;
  private recorder: MediaRecorder | null = null;
  private vadTimer: ReturnType<typeof setInterval> | null = null;
  private chunkTimer: ReturnType<typeof setTimeout> | null = null;
  private stopped = false;
  private quietest = Number.POSITIVE_INFINITY;
  private loudest = 0;
  private readonly nav: Navigator;
  private permissionStatus: PermissionStatus | null = null;
  /** Why the microphone is down, or null while it is live. */
  private lostKind: LossKind | null = null;
  private retryTimer: ReturnType<typeof setTimeout> | null = null;
  private reacquiring: Promise<boolean> | null = null;
  private readonly sound: SoundLevelDetector;
  private readonly now: () => number;

  constructor(private readonly options: AudioOptions) {
    this.nav = options.navigator ?? window.navigator;
    this.now = options.now ?? Date.now;
    this.sound = new SoundLevelDetector(options.sound, () => this.character());
  }

  async start(stream?: MediaStream): Promise<void> {
    this.stream = stream ?? (await openMicrophone(this.nav));
    this.attach(this.stream);
    this.watchPermission();
  }

  live(): boolean {
    const track = this.stream?.getAudioTracks()[0];
    return Boolean(track && track.readyState === "live");
  }

  /** The stream being monitored, or null while the microphone is down. */
  currentStream(): MediaStream | null {
    return this.lostKind === null ? this.stream : null;
  }

  /** Open the microphone again now; see `CameraMonitor.reacquire`. */
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

  stop(): void {
    this.stopped = true;
    this.clearRetry();
    if (this.permissionStatus) this.permissionStatus.onchange = null;
    this.release();
  }

  private attach(stream: MediaStream): void {
    const track = stream.getAudioTracks()[0];
    if (track) track.onended = () => void this.onTrackEnded();
    // The analyser always runs: the sound-level advisory needs it even where
    // there is no analysis service to upload to.
    this.context = new AudioContext();
    this.analyser = this.context.createAnalyser();
    this.context.createMediaStreamSource(stream).connect(this.analyser);
    this.vadTimer = setInterval(() => this.sampleEnergy(), VAD_POLL_MS);
    if (this.options.uploadEnabled) this.startChunk(stream);
  }

  private character(): "voice_like" | "broadband" | null {
    if (!this.analyser || !this.context) return null;
    const bins = new Float32Array(this.analyser.frequencyBinCount);
    this.analyser.getFloatFrequencyData(bins);
    return spectrumCharacter(bins, this.context.sampleRate, this.analyser.fftSize);
  }

  private sampleEnergy(): void {
    if (!this.analyser) return;
    const buffer = new Float32Array(this.analyser.fftSize);
    this.analyser.getFloatTimeDomainData(buffer);
    let total = 0;
    for (const sample of buffer) total += sample * sample;
    const rms = Math.sqrt(total / buffer.length);
    this.quietest = Math.min(this.quietest, rms);
    this.loudest = Math.max(this.loudest, rms);
    // A muted track stays "live" and reads silence; a lost one has no analyser.
    // Neither is a loud room, and neither is judged.
    const track = this.stream?.getAudioTracks()[0];
    const draft = this.sound.observe({
      at: this.now(),
      dbfs: rmsToDbfs(rms),
      excluded: isVoiceRecordingActive() || !track || track.muted === true || track.readyState !== "live",
    });
    if (draft) {
      this.options.emit(draft);
      this.options.onAdvisory(SOUND_LEVEL_ADVISORY);
    }
  }

  private speechHeard(): boolean {
    if (!Number.isFinite(this.quietest)) return false;
    return this.loudest > this.quietest * SPEECH_ENERGY_RATIO;
  }

  private startChunk(stream: MediaStream): void {
    if (this.stopped) return;
    const mimeType = preferredMimeType();
    const recorder = new MediaRecorder(stream, mimeType ? { mimeType } : undefined);
    this.recorder = recorder;
    this.quietest = Number.POSITIVE_INFINITY;
    this.loudest = 0;
    recorder.ondataavailable = (event) => {
      const speech = this.speechHeard();
      const chunk = event.data;
      if (speech && chunk.size > 0 && chunk.size <= this.options.maxChunkBytes) {
        void this.send(chunk);
      }
      // `chunk` is not referenced after this handler returns. Whether or
      // not it was sent, nothing holds it.
    };
    recorder.onstop = () => {
      if (!this.stopped && this.stream === stream) this.startChunk(stream);
    };
    recorder.start();
    this.chunkTimer = setTimeout(() => {
      if (recorder.state === "recording") recorder.stop();
    }, this.options.chunkSeconds * 1000);
  }

  private async send(chunk: Blob): Promise<void> {
    try {
      const result = await this.options.upload(chunk);
      if (result.warning) this.options.onWarning(result.warning, result.warnings_used);
      if (result.termination) {
        this.stop();
        this.options.onTermination(result.termination);
      }
    } catch (error) {
      if (isSessionEnded(error)) {
        this.stop();
        this.options.onSessionEnded((error as Error).message);
      }
      // Any other failure loses this chunk's analysis only. The next chunk
      // is a fresh request, and a missed second voice in one chunk is not a
      // reason to surface an error to the candidate.
    }
  }

  private watchPermission(): void {
    if (!this.nav.permissions) return;
    this.nav.permissions
      .query({ name: "microphone" as PermissionName })
      .then((status) => {
        this.permissionStatus = status;
        status.onchange = () => {
          // As for the camera: anything but "granted" is lost, and "granted"
          // again reopens the microphone without waiting for a click.
          if (status.state === "granted") {
            if (this.lostKind === "permission") void this.reacquire();
          } else {
            this.lose("permission");
          }
        };
      })
      .catch(() => {
        // Not exposed by name in this browser; the track's `ended` handler
        // and a refused re-acquire cover it.
      });
  }

  private onTrackEnded(): void {
    if (this.stopped) return;
    this.lose(this.permissionStatus?.state === "denied" ? "permission" : "stream");
  }

  /** Release the dead stream and its analysis, and say so. The chunk that was
   *  being captured is dropped with it: its recorder is bound to a track that
   *  no longer produces audio. */
  private lose(kind: LossKind): void {
    if (this.stopped) return;
    if (this.lostKind === kind || this.lostKind === "permission") return;
    const first = this.lostKind === null;
    this.lostKind = kind;
    if (first) this.release();
    this.options.onLost(kind);
    if (kind === "stream") {
      this.scheduleRetry();
    } else {
      this.clearRetry();
    }
  }

  private release(): void {
    this.clearTimers();
    if (this.recorder && this.recorder.state !== "inactive") {
      this.recorder.ondataavailable = null;
      this.recorder.onstop = null;
      this.recorder.stop();
    }
    this.recorder = null;
    const dead = this.stream;
    this.stream = null;
    const track = dead?.getAudioTracks()[0];
    if (track) track.onended = null;
    dead?.getTracks().forEach((each) => each.stop());
    void this.context?.close();
    this.context = null;
    this.analyser = null;
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
      stream = await openMicrophone(this.nav);
    } catch (error) {
      if (isPermissionDenied(error)) this.lose("permission");
      // Any other refusal leaves the outage as it is; see the camera.
      return false;
    }
    if (this.stopped) {
      stream.getTracks().forEach((track) => track.stop());
      return false;
    }
    try {
      this.attach(stream);
    } catch (error) {
      // A stream that opened and could not be analysed is not a recovered
      // microphone; the outage stands and the next attempt asks again.
      this.clearTimers();
      stream.getTracks().forEach((track) => track.stop());
      void this.context?.close();
      this.context = null;
      this.analyser = null;
      console.warn(
        "proctoring microphone could not be resumed: " +
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

  private clearTimers(): void {
    if (this.vadTimer !== null) clearInterval(this.vadTimer);
    if (this.chunkTimer !== null) clearTimeout(this.chunkTimer);
    this.vadTimer = null;
    this.chunkTimer = null;
  }
}
