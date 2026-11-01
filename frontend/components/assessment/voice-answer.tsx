"use client";

// A spoken answer to a prose question (Appendix B section 3).
//
//   idle -> starting -> recording -> uploading -> transcribing -> transcribed
//                  \           \           \                  \-> failed
//                   `-----------`-----------`-> unavailable
//
// TWO KINDS OF FAILURE, BECAUSE ONLY ONE OF THEM HOLDS THE CLOCK. `failed` is
// the server's: the audio arrived, transcription did not work, and the server
// holds a pause open until the candidate has read that and acknowledged it.
// `unavailable` never reached the server (no microphone, a refused upload, a
// recorder that died): no pause was opened, so there is nothing to
// acknowledge and the typing box is offered at once.
//
// THE TRANSCRIPT IS THE ANSWER, AND IT IS FINAL. Amazon Transcribe turns the
// recording into text on the server; that text is what is evaluated, it is
// shown here read-only, and the player submits it without offering an edit.
// There is no edit route on the server either, so a hand-built request cannot
// do what this screen does not offer. Only the text is kept: the audio object
// is deleted once transcribed, and the session recording is the one place the
// candidate's voice lives.
//
// ENGLISH ONLY, AT MOST THE SERVER'S LIMIT. The begin call returns the longest
// capture the server accepts and the recorder stops itself there, so the
// limit a candidate reads and the limit the server enforces are one number.
//
// A FAILURE SWITCHES THIS ANSWER TO TYPING, AND THE CLOCK WAITED. From upload
// until the transcript (or the failure) the server holds the turn's clock
// still. On a failure the candidate is told plainly, acknowledging closes the
// server's pause, and the player offers the typing box for this turn only.
// Nothing here ever presents an empty or invented transcript as what was said.
//
// THE MICROPHONE CAPTURE IS OPENED BEFORE THE SERVER ROW. A browser that will
// not give up the microphone never creates a capture record the server would
// then have to treat as an open spoken answer.

import * as React from "react";
import { AlertTriangle, Loader2, Mic, Square } from "lucide-react";

import { formatClock } from "@/components/assessment/turn-timer";
import { Button } from "@/components/ui/button";
import { apiGet, apiPost, apiUpload } from "@/lib/api";
import type { VoiceAnswerOut } from "@/lib/assessment/contracts";
import { setVoiceRecordingActive } from "@/lib/assessment/voice-activity";

export type VoicePhase =
  | "idle"
  | "starting"
  | "recording"
  | "uploading"
  | "transcribing"
  | "transcribed"
  | "failed"
  | "unavailable";

/** The phases during which the server holds the turn's clock still. */
export const SERVER_PAUSED_PHASES: readonly VoicePhase[] = [
  "uploading",
  "transcribing",
  "transcribed",
  "failed",
];

/** The phases during which the typing box is not offered, because a spoken
 *  answer is being captured or is already on its way. */
export const VOICE_OCCUPIED_PHASES: readonly VoicePhase[] = [
  "starting",
  "recording",
  "uploading",
  "transcribing",
  "transcribed",
  "failed",
];

/** How often the transcription status is asked for while the server works. */
export const TRANSCRIPT_POLL_MS = 1500;

/** Consecutive status reads that may fail on the network before the screen
 *  says so instead of polling quietly. */
export const POLL_FAILURES_BEFORE_NOTICE = 5;

/** The containers the server accepts, in preference order. Asked of the
 *  browser rather than assumed: a hardcoded type the browser lacks records
 *  nothing and reports no error. */
/** The recorder's audio bitrate. Speech needs far less than a browser's
 *  music-grade default, and at this rate the longest answer the server
 *  accepts stays well inside its upload ceiling in every container, including
 *  the AAC one a Safari recorder produces. */
export const VOICE_AUDIO_BITS_PER_SECOND = 64_000;

const PREFERRED_TYPES = [
  "audio/webm;codecs=opus",
  "audio/webm",
  "audio/ogg;codecs=opus",
  "audio/mp4",
] as const;

/** The file extension the upload is named with, keyed by the container the
 *  recorder actually produced. The server derives its own object key; this is
 *  only the multipart filename. */
function extensionFor(mimeType: string): string {
  if (mimeType.startsWith("audio/ogg")) return "ogg";
  if (mimeType.startsWith("audio/mp4")) return "mp4";
  return "webm";
}

/** Said after any transcription failure, whoever words the failure itself. */
export const TIMER_WAITED = "The timer was paused while we tried, so you have not lost time.";

export const TRANSCRIPTION_FAILED = `We could not turn your spoken answer into text. Please type your answer instead. ${TIMER_WAITED}`;

export const MICROPHONE_UNAVAILABLE =
  "Your microphone could not be opened for a spoken answer. Please type your answer instead.";

export interface VoiceAnswerHandle {
  /** The turn's clock ran out. A capture in progress is stopped and sent,
   *  and the answer it produces is submitted as the timed-out answer.
   *  Returns true when a spoken answer is now on its way, so the caller
   *  submits nothing else for this turn. */
  stopForExpiry(): boolean;
}

export interface VoiceMedia {
  getAudioStream(): Promise<MediaStream>;
  isTypeSupported(type: string): boolean;
  createRecorder(stream: MediaStream, options: MediaRecorderOptions): MediaRecorder;
}

const BROWSER_MEDIA: VoiceMedia = {
  getAudioStream: () => navigator.mediaDevices.getUserMedia({ audio: true, video: false }),
  isTypeSupported: (type) =>
    typeof MediaRecorder !== "undefined" && MediaRecorder.isTypeSupported(type),
  createRecorder: (stream, options) => new MediaRecorder(stream, options),
};

export function pickAudioType(isSupported: (type: string) => boolean): string | null {
  return PREFERRED_TYPES.find((type) => isSupported(type)) ?? null;
}

interface Props {
  conversationId: string;
  turnSeq: number;
  disabled: boolean;
  /** Every phase change, so the player can hold its own face of the clock
   *  still while the server holds the real one, and hide the typing box
   *  while a spoken answer is in progress. */
  onPhaseChange(phase: VoicePhase): void;
  /** The final transcript arrived. `stoppedByClock` is true when the turn's
   *  clock ended the capture rather than the candidate. */
  onTranscribed(voiceAnswerId: string, transcript: string, stoppedByClock: boolean): void;
  /** The candidate read the failure and chose to type. The server's pause is
   *  already closed when this is called. */
  onSwitchToTyping(): void;
  /** Injected in tests; the browser's own devices otherwise. */
  media?: VoiceMedia;
}

export const VoiceAnswer = React.forwardRef<VoiceAnswerHandle, Props>(function VoiceAnswer(
  {
    conversationId,
    turnSeq,
    disabled,
    onPhaseChange,
    onTranscribed,
    onSwitchToTyping,
    media = BROWSER_MEDIA,
  },
  ref
) {
  const [phase, setPhaseState] = React.useState<VoicePhase>("idle");
  const [voice, setVoice] = React.useState<VoiceAnswerOut | null>(null);
  const [elapsedMs, setElapsedMs] = React.useState(0);
  const [problem, setProblem] = React.useState<string | null>(null);
  const [pollTrouble, setPollTrouble] = React.useState(false);
  const [acknowledging, setAcknowledging] = React.useState(false);

  const phaseRef = React.useRef<VoicePhase>("idle");
  const recorder = React.useRef<MediaRecorder | null>(null);
  const stream = React.useRef<MediaStream | null>(null);
  const chunks = React.useRef<Blob[]>([]);
  const startedAt = React.useRef(0);
  const stoppedByClock = React.useRef(false);
  const mounted = React.useRef(true);
  const base = `/api/v2/assessments/conversations/${conversationId}/voice`;
  // The player's callbacks are read through a ref so a re-render that hands
  // down new function objects cannot restart the transcript poll or reset
  // its failure count.
  const callbacks = React.useRef({ onPhaseChange, onTranscribed, onSwitchToTyping });
  callbacks.current = { onPhaseChange, onTranscribed, onSwitchToTyping };

  const setPhase = React.useCallback((next: VoicePhase) => {
    phaseRef.current = next;
    setVoiceRecordingActive(next === "recording");
    setPhaseState(next);
    callbacks.current.onPhaseChange(next);
  }, []);

  const releaseStream = React.useCallback(() => {
    stream.current?.getTracks().forEach((track) => track.stop());
    stream.current = null;
  }, []);

  React.useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      setVoiceRecordingActive(false);
      if (recorder.current && recorder.current.state !== "inactive") {
        recorder.current.ondataavailable = null;
        recorder.current.onstop = null;
        recorder.current.stop();
      }
      releaseStream();
    };
  }, [releaseStream]);

  const upload = React.useCallback(
    async (row: VoiceAnswerOut, blob: Blob) => {
      setPhase("uploading");
      const form = new FormData();
      form.append("file", blob, `answer.${extensionFor(blob.type)}`);
      try {
        const uploaded = await apiUpload<VoiceAnswerOut>(
          `${base}/${row.id}/audio`,
          form
        );
        if (!mounted.current) return;
        setVoice(uploaded);
        setPhase("transcribing");
      } catch (error) {
        if (!mounted.current) return;
        // Nothing reached the server, so no pause was opened there and there
        // is nothing to acknowledge: the answer simply goes to typing.
        setProblem(
          error instanceof Error
            ? `Your spoken answer could not be sent (${error.message}). Please type your answer instead.`
            : "Your spoken answer could not be sent. Please type your answer instead."
        );
        setPhase("unavailable");
      }
    },
    [base, setPhase]
  );

  const stop = React.useCallback(
    (byClock: boolean) => {
      const active = recorder.current;
      if (!active || active.state === "inactive") return false;
      stoppedByClock.current = byClock;
      active.stop();
      return true;
    },
    []
  );

  React.useImperativeHandle(
    ref,
    () => ({
      stopForExpiry: () => {
        const current = phaseRef.current;
        if (current === "recording") return stop(true);
        // Already on its way: the transcript will be submitted when it lands.
        return current === "uploading" || current === "transcribing" || current === "transcribed";
      },
    }),
    [stop]
  );

  const begin = async () => {
    setProblem(null);
    setPollTrouble(false);
    const type = pickAudioType((candidate) => media.isTypeSupported(candidate));
    if (type === null) {
      setProblem(MICROPHONE_UNAVAILABLE);
      setPhase("unavailable");
      return;
    }
    setPhase("starting");
    let opened: MediaStream;
    try {
      opened = await media.getAudioStream();
    } catch (error) {
      // The browser's refusal (permission, no device) is the whole reason,
      // and it is said to the candidate below rather than swallowed.
      console.warn(
        "microphone for a spoken answer could not be opened: " +
          (error instanceof Error ? error.name : "unknown")
      );
      if (!mounted.current) return;
      setProblem(MICROPHONE_UNAVAILABLE);
      setPhase("unavailable");
      return;
    }
    if (!mounted.current) {
      opened.getTracks().forEach((track) => track.stop());
      return;
    }
    stream.current = opened;
    let row: VoiceAnswerOut;
    try {
      row = await apiPost<VoiceAnswerOut>(`${base}/begin`, { turn_seq: turnSeq });
    } catch (error) {
      releaseStream();
      if (!mounted.current) return;
      setProblem(
        error instanceof Error
          ? `${error.message} Please type your answer instead.`
          : "A spoken answer could not be started. Please type your answer instead."
      );
      setPhase("unavailable");
      return;
    }
    if (!mounted.current) {
      releaseStream();
      return;
    }
    setVoice(row);
    if (row.status !== "recording") {
      // A spoken answer for this turn already exists (the page was reloaded
      // while it was being transcribed): pick it up rather than record a
      // second one the server would discard. The poll reads its outcome.
      releaseStream();
      setPhase("transcribing");
      return;
    }
    chunks.current = [];
    stoppedByClock.current = false;
    const created = media.createRecorder(opened, {
      mimeType: type,
      audioBitsPerSecond: VOICE_AUDIO_BITS_PER_SECOND,
    });
    created.ondataavailable = (event: BlobEvent) => {
      if (event.data && event.data.size > 0) chunks.current.push(event.data);
    };
    created.onerror = () => {
      // The recorder died on its own (the device went away mid-answer). The
      // server has a capture row but no audio, so no pause was opened there
      // and there is nothing to acknowledge.
      created.ondataavailable = null;
      created.onstop = null;
      recorder.current = null;
      releaseStream();
      if (!mounted.current) return;
      setProblem(
        "The recording stopped unexpectedly, so this answer could not be sent. Please type your answer instead."
      );
      setPhase("unavailable");
    };
    created.onstop = () => {
      releaseStream();
      recorder.current = null;
      if (!mounted.current) return;
      const blob = new Blob(chunks.current, { type: created.mimeType || type });
      chunks.current = [];
      void upload(row, blob);
    };
    recorder.current = created;
    startedAt.current = Date.now();
    setElapsedMs(0);
    created.start(1000);
    setPhase("recording");
  };

  // The recording's own clock: shown to the candidate, and the capture stops
  // itself at the server's limit so a longer recording is never produced.
  React.useEffect(() => {
    if (phase !== "recording" || voice === null) return;
    const limitMs = voice.max_seconds * 1000;
    const timer = window.setInterval(() => {
      const elapsed = Date.now() - startedAt.current;
      setElapsedMs(elapsed);
      if (elapsed >= limitMs) stop(false);
    }, 250);
    return () => window.clearInterval(timer);
  }, [phase, stop, voice]);

  // Ask for the transcript until the server has an answer. A network blip is
  // retried quietly a few times and then said out loud; the pause on the
  // server keeps the candidate's time safe while this waits.
  React.useEffect(() => {
    if (phase !== "transcribing" || voice === null) return;
    let cancelled = false;
    let failures = 0;
    const poll = async () => {
      try {
        const read = await apiGet<VoiceAnswerOut>(`${base}/${voice.id}`);
        if (cancelled) return;
        failures = 0;
        setPollTrouble(false);
        // `consumed` is a transcript another tab of this assessment already
        // submitted; sending it again is refused by the server, which then
        // hands the player the turn that is current.
        if (
          (read.status === "transcribed" || read.status === "consumed") &&
          read.transcript !== null
        ) {
          setVoice(read);
          setPhase("transcribed");
          callbacks.current.onTranscribed(
            read.id,
            read.transcript,
            stoppedByClock.current
          );
          return;
        }
        if (read.status === "failed") {
          setVoice(read);
          // The server's own account when it gives one; the pause it holds
          // open is the same either way.
          setProblem(read.message ? `${read.message} ${TIMER_WAITED}` : TRANSCRIPTION_FAILED);
          setPhase("failed");
          return;
        }
      } catch (error) {
        if (cancelled) return;
        failures += 1;
        if (failures >= POLL_FAILURES_BEFORE_NOTICE) setPollTrouble(true);
        console.warn(
          "voice transcript status could not be read: " +
            (error instanceof Error ? error.message : "unknown")
        );
      }
      if (!cancelled) timer = window.setTimeout(() => void poll(), TRANSCRIPT_POLL_MS);
    };
    let timer = window.setTimeout(() => void poll(), TRANSCRIPT_POLL_MS);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [base, phase, setPhase, voice]);

  const switchToTyping = async () => {
    // A failure the server recorded holds a pause open until the candidate
    // has read it; acknowledging is what lets the clock run again. A failure
    // that never reached the server (no microphone, a refused upload) holds
    // no pause and needs no call.
    if (voice !== null && voice.status === "failed") {
      setAcknowledging(true);
      try {
        await apiPost(`${base}/${voice.id}/acknowledge`);
      } catch (error) {
        setAcknowledging(false);
        setProblem(
          error instanceof Error
            ? `${TRANSCRIPTION_FAILED} (${error.message}) Please try the button again.`
            : TRANSCRIPTION_FAILED
        );
        return;
      }
      setAcknowledging(false);
    }
    callbacks.current.onSwitchToTyping();
  };

  if (phase === "failed") {
    return (
      <div
        role="alert"
        className="space-y-3 border border-warning bg-warning/10 p-4 text-sm"
        data-testid="voice-failed"
      >
        <p className="flex items-start gap-2">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
          <span>{problem ?? TRANSCRIPTION_FAILED}</span>
        </p>
        <Button type="button" disabled={acknowledging} onClick={() => void switchToTyping()}>
          {acknowledging ? <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" /> : null}
          Type my answer instead
        </Button>
      </div>
    );
  }

  if (phase === "unavailable") {
    return (
      <div
        role="alert"
        className="space-y-2 border border-warning bg-warning/10 p-3 text-sm"
        data-testid="voice-unavailable"
      >
        <p className="flex items-start gap-2">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
          <span>{problem ?? MICROPHONE_UNAVAILABLE}</span>
        </p>
        <Button type="button" variant="outline" onClick={() => setPhase("idle")}>
          Try speaking again
        </Button>
      </div>
    );
  }

  if (phase === "transcribed" && voice?.transcript) {
    return (
      <div className="space-y-2" data-testid="voice-transcript">
        <p className="type-eyebrow">
          Your spoken answer, as transcribed
        </p>
        <blockquote className="whitespace-pre-wrap border-l-2 border-teal-600 bg-surface p-4 text-sm leading-7">
          {voice.transcript}
        </blockquote>
        <p className="flex items-center gap-2 text-xs" role="status">
          <Loader2 className="h-3 w-3 animate-spin" aria-hidden="true" />
          Sending your answer
        </p>
      </div>
    );
  }

  if (phase === "uploading" || phase === "transcribing") {
    return (
      <div className="space-y-1 text-sm" role="status" aria-live="polite" data-testid="voice-processing">
        <p className="flex items-center gap-2 font-medium">
          <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
          {phase === "uploading" ? "Sending your recording" : "Turning your answer into text"}
        </p>
        <p className="text-xs">The timer is paused while this happens.</p>
        {pollTrouble ? (
          <p className="text-xs font-medium">
            We are having trouble reaching the server. Your recording is safe and we are still trying.
          </p>
        ) : null}
      </div>
    );
  }

  if (phase === "recording" && voice !== null) {
    return (
      <div className="flex flex-wrap items-center gap-3" data-testid="voice-recording">
        <span className="inline-flex items-center gap-2 text-sm font-medium" role="status">
          <span aria-hidden className="h-2.5 w-2.5 rounded-full bg-destructive motion-safe:animate-pulse" />
          Recording
          <span className="tabular-nums">
            {formatClock(elapsedMs)} of {formatClock(voice.max_seconds * 1000)}
          </span>
        </span>
        <Button type="button" onClick={() => stop(false)}>
          <Square className="h-4 w-4" aria-hidden="true" />
          Stop and send
        </Button>
      </div>
    );
  }

  return (
    <div className="space-y-1">
      <Button
        type="button"
        variant="outline"
        disabled={disabled || phase === "starting"}
        onClick={() => void begin()}
      >
        {phase === "starting" ? (
          <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
        ) : (
          <Mic className="h-4 w-4" aria-hidden="true" />
        )}
        Answer by speaking
      </Button>
      <p className="text-xs" data-testid="voice-hint">
        {phase === "starting"
          ? "Opening your microphone. Allow access if your browser asks."
          : "Or type below. To speak, allow microphone access when asked, and speak in English. Your answer is turned into text, and that text is your final answer: it cannot be edited afterwards."}
      </p>
    </div>
  );
});
