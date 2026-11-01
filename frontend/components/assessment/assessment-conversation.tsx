"use client";

// The assessment, one question at a time (Appendix B sections 1 to 3).
//
// ONE MODE. Every candidate takes the same proctored, question-by-question
// assessment: prose answered by typing or speaking, multiple choice and
// fill-in-the-blank by clicking and typing, coding in the editor. The format
// on screen comes from the server's `question`, and the answer field is
// whatever `QuestionRenderer` dispatches for it.
//
// FIXED ORDER, AND THE SERVER NAMES THE TURN. Only the current turn is
// answerable. Every submission carries the server's `turn_seq`, and a
// submission naming a turn that is no longer current is refused with a 409 and
// writes nothing, so a retry after a lost response is never filed as the
// answer to the next question. The answers already given are shown read-only
// from the server's own `history`; there is no edit control because there is
// no edit route.
//
// THE CLOCK IS THE SERVER'S. The countdown renders the server's deadline; the
// draft is sent to the server as the candidate works, because on expiry the
// server submits what it holds; and reaching zero here first re-reads the
// server's clock, because a pause this browser did not see (a warning, a
// camera recovery, a transcription) may have moved the deadline. Nothing this
// component measures is sent as time. An empty answer submitted by the clock
// is recorded by the server as an evidence gap, and it says so.
//
// Rendered INSIDE `components/proctoring/proctoring-shell`, which owns consent
// to monitoring, the system check and the monitoring session, and hands this
// component the `ProctoringBridge` through `useProctoring()`. There is no
// unmonitored mode and the page never mounts this outside the shell.

import * as React from "react";
import Link from "next/link";
import { ArrowLeft, CheckCircle2, Loader2, Send, Sparkles, Trash2 } from "lucide-react";

import { PageHeader } from "@/components/app-shell";
import { AssessmentProgress, AssessmentSteps } from "@/components/assessment-progress";
import { AutosaveIndicator } from "@/components/assessment/autosave-indicator";
import { CodingSubmitButton } from "@/components/assessment/coding-submit-button";
import { HistoryList } from "@/components/assessment/history-list";
import { QuestionRenderer } from "@/components/assessment/question-renderer";
import { TurnTimer, remainingMs } from "@/components/assessment/turn-timer";
import {
  SERVER_PAUSED_PHASES,
  VOICE_OCCUPIED_PHASES,
  VoiceAnswer,
  type VoiceAnswerHandle,
  type VoicePhase,
} from "@/components/assessment/voice-answer";
import { useProctoring } from "@/components/proctoring/proctoring-context";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { useToast } from "@/components/ui/toast";
import { ApiError, apiPost } from "@/lib/api";
import {
  answerLine,
  emptyAnswerFor,
  isAnswerComplete,
  isAnswerEmpty,
  isCodingAnswer,
  starterCodeFor,
  textOf,
  turnIsProse,
  turnKeyFor,
} from "@/lib/assessment/answers";
import { clearDraft, readDraft, useAutosaveDraft } from "@/lib/assessment/autosave";
import { CodingConversationContext } from "@/lib/assessment/coding";
import type {
  AnswerPayload,
  ConversationTurn,
  PauseReason,
  ProctoringFieldHooks,
  QuestionOut,
  RespondBody,
} from "@/lib/assessment/contracts";
import { useAuth } from "@/lib/auth-context";

/** How often a conversation that is being prepared, or is paused by the
 *  proctoring layer, is asked for again. */
export const WAITING_POLL_MS = 5000;

/** Attempts at an answer the clock submitted, when the network fails it. Each
 *  one is a full request; after the last the candidate is told what happens
 *  next rather than left looking at zero. */
export const EXPIRY_SUBMIT_ATTEMPTS = 3;
const EXPIRY_RETRY_MS = 3000;

/** Said when a turn's time ran out and this page could not reach the server
 *  to hand it in. True whatever the network does next: the server submits the
 *  last draft it received for the turn the next time the assessment is opened
 *  (PLAN-p3 section 3.4), so nothing more is promised than that. */
export const EXPIRY_UNREACHABLE =
  "Time ran out on this question and this page could not reach us to send your answer. We will use the last draft we received from you for this question. Reload the page to carry on.";

const PAUSE_LABELS: Record<PauseReason, string> = {
  device_loss: "Paused while your camera or microphone is restored",
  transcription: "Paused while your spoken answer is transcribed",
  warning: "Paused while you read the warning",
};

/** What was consumed from the bridge for a turn whose send failed. The bridge
 *  clears its capture on read, so a retry after a network blip would
 *  otherwise report no behaviour for a turn that had some. Kept until the
 *  turn is accepted. */
interface CarriedCapture {
  turnKey: string;
  behaviour: RespondBody["behaviour"] | null;
}

/** The exchange on its way to the server, shown the moment it is sent. */
interface PendingExchange {
  prompt: string;
  answer: string;
}

interface SubmitOptions {
  timedOut: boolean;
  voice?: { id: string; transcript: string };
}

/** What became of a submission. `stale` means the server had already moved
 *  past this turn and nothing was written; `failed` means it never arrived
 *  and may be tried again. */
type SubmitOutcome = "sent" | "stale" | "failed" | "not_sent";

const START_PATH = (linkId: string) =>
  `/api/v2/assessments/conversations/links/${linkId}/start`;

export function AssessmentConversation({ linkId }: { linkId: string }) {
  const { toast } = useToast();
  const { user } = useAuth();
  const bridge = useProctoring();
  const [conversation, setConversation] = React.useState<ConversationTurn | null>(null);
  const [receivedAt, setReceivedAt] = React.useState(0);
  const [value, setValueState] = React.useState<AnswerPayload | null>(null);
  // The answer as of the last change, readable synchronously. The clock can
  // expire in the same effect flush that restores a draft, before React has
  // re-rendered with it, and the submission must carry the restored words.
  const valueRef = React.useRef<AnswerPayload | null>(null);
  const setValue = React.useCallback((next: AnswerPayload | null) => {
    valueRef.current = next;
    setValueState(next);
  }, []);
  const [fieldHooks, setFieldHooks] = React.useState<ProctoringFieldHooks | null>(null);
  const [loading, setLoading] = React.useState(true);
  const [loadError, setLoadError] = React.useState<string | null>(null);
  const [sending, setSending] = React.useState(false);
  const [pending, setPending] = React.useState<PendingExchange | null>(null);
  const [voicePhase, setVoicePhase] = React.useState<VoicePhase>("idle");
  const [typingOnly, setTypingOnly] = React.useState(false);
  const [expiryNotice, setExpiryNotice] = React.useState<string | null>(null);
  // A transcribed spoken answer whose submission did not arrive. The
  // transcript is final and already on the server, so the only thing to do
  // is send it again; it is never put back into a box to be edited.
  const [voiceRetry, setVoiceRetry] = React.useState<{
    id: string;
    transcript: string;
    timedOut: boolean;
  } | null>(null);

  const endRef = React.useRef<HTMLDivElement | null>(null);
  const voiceRef = React.useRef<VoiceAnswerHandle | null>(null);
  const carried = React.useRef<CarriedCapture | null>(null);
  const endedFor = React.useRef<string | null>(null);
  const sendingRef = React.useRef(false);
  const expiring = React.useRef(false);
  // The bridge is read through a ref inside effects so a re-render of the
  // shell that hands down a new bridge object does not restart a capture:
  // `fieldHooksFor` starts a fresh capture for its key every time it is
  // called, and it must be called exactly once per turn.
  const bridgeRef = React.useRef(bridge);
  bridgeRef.current = bridge;

  const question: QuestionOut | null = conversation?.question ?? null;
  // A turn paused by the proctoring layer is still the current turn: its
  // draft, its capture and its clock stay where they are, and only answering
  // is held until the session resumes.
  const hasTurn =
    (conversation?.status === "active" || conversation?.status === "paused") &&
    Boolean(conversation.prompt) &&
    conversation.turn !== null;
  const answerable = hasTurn && conversation?.status === "active";
  const turnKey =
    hasTurn && conversation ? turnKeyFor(conversation.conversation_id, conversation.turn_seq) : null;
  const prose = turnIsProse(question);
  // Untouched starter code is not an answer. A version 2 coding question
  // carries one starter per language, so the comparison follows the language
  // the answer is in (`starterCodeFor`), never the first one offered.
  const isEmpty = React.useCallback(
    (candidate: AnswerPayload | null) =>
      isAnswerEmpty(candidate, starterCodeFor(question, candidate)),
    [question]
  );
  const bridgePaused = Boolean(bridge.paused);
  const serverPaused = Boolean(conversation?.turn?.paused) || conversation?.status === "paused";
  const voiceHoldsClock = SERVER_PAUSED_PHASES.includes(voicePhase);
  const clockPaused = serverPaused || bridgePaused || voiceHoldsClock;
  const voiceOccupied = VOICE_OCCUPIED_PHASES.includes(voicePhase);

  /** Take a server response as the current state. The browser's clock is
   *  read HERE, once, as the instant the server's `server_now` arrived. */
  const adopt = React.useCallback((next: ConversationTurn) => {
    setReceivedAt(Date.now());
    setConversation(next);
  }, []);

  const resync = React.useCallback(async (): Promise<ConversationTurn | null> => {
    try {
      const fresh = await apiPost<ConversationTurn>(START_PATH(linkId));
      adopt(fresh);
      return fresh;
    } catch (error) {
      toast({
        title: "Could not reach the assessment",
        description: error instanceof Error ? error.message : undefined,
        variant: "destructive",
      });
      return null;
    }
  }, [adopt, linkId, toast]);

  const autosave = useAutosaveDraft({
    linkId,
    conversationId: conversation?.conversation_id ?? null,
    turnKey,
    turnSeq: answerable ? (conversation?.turn_seq ?? null) : null,
    value,
    isEmpty,
    prose,
    enabled: answerable && !clockPaused && !sending && !voiceOccupied,
    onStale: () => void resync(),
  });

  React.useEffect(() => {
    let cancelled = false;
    apiPost<ConversationTurn>(START_PATH(linkId))
      .then((started) => {
        if (!cancelled) adopt(started);
      })
      .catch((error: unknown) => {
        if (cancelled) return;
        setLoadError(error instanceof Error ? error.message : "The assessment could not be opened.");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [adopt, linkId]);

  // A conversation being prepared, or a turn the server is holding still, is
  // asked for again until it can be answered. The server's answer is the
  // only thing that ends the wait; a spoken answer being transcribed is
  // followed by its own poll and needs no second one.
  React.useEffect(() => {
    if (!conversation) return;
    const waiting =
      conversation.status === "preparing" ||
      conversation.status === "paused" ||
      (conversation.status === "active" && Boolean(conversation.turn?.paused) && !voiceHoldsClock);
    if (!waiting) return;
    const timer = window.setTimeout(() => void resync(), WAITING_POLL_MS);
    return () => window.clearTimeout(timer);
  }, [conversation, resync, voiceHoldsClock]);

  // The proctoring layer released a pause: the server extended the deadline
  // by however long it lasted, so the face re-reads it.
  const wasBridgePaused = React.useRef(bridgePaused);
  React.useEffect(() => {
    if (wasBridgePaused.current && !bridgePaused) void resync();
    wasBridgePaused.current = bridgePaused;
  }, [bridgePaused, resync]);

  // A new turn is on screen: restore its local draft before the candidate can
  // type, and open its behaviour capture. Both are keyed by the turn, so a
  // re-ask of the same question is a fresh draft and a fresh capture.
  React.useEffect(() => {
    setVoicePhase("idle");
    setTypingOnly(false);
    setExpiryNotice(null);
    setVoiceRetry(null);
    if (turnKey === null) {
      setValue(null);
      setFieldHooks(null);
      return;
    }
    const restored = readDraft(linkId, turnKey);
    setValue(restored ?? (question ? emptyAnswerFor(question) : { text: "" }));
    setFieldHooks(bridgeRef.current.fieldHooksFor(turnKey));
    // `question` is the object the key was derived from; a new key always
    // means a new question object.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [linkId, turnKey]);

  // The conversation has ended, one way or the other: tell the shell once so
  // monitoring stops and the camera is released.
  React.useEffect(() => {
    if (!conversation) return;
    if (conversation.status !== "completed" && conversation.status !== "terminated") return;
    const key = `${conversation.conversation_id}:${conversation.status}`;
    if (endedFor.current === key) return;
    endedFor.current = key;
    bridgeRef.current.onConversationEnded(conversation.status);
  }, [conversation]);

  React.useEffect(() => {
    endRef.current?.scrollIntoView?.({ behavior: "smooth", block: "end" });
  }, [conversation?.history.length, conversation?.prompt, sending]);

  const complete = isAnswerComplete(value, starterCodeFor(question, value));
  const view = React.useRef({ conversation, question, turnKey, prose, answerable });
  view.current = { conversation, question, turnKey, prose, answerable };

  /**
   * Send the current turn's answer.
   *
   * The exchange is shown the moment it is sent and replaced by the server's
   * own history when the response arrives. On a failure the answer goes back
   * into the field, so nothing is ever silently lost. A 409 means the turn is
   * no longer current (already answered, or the server's clock submitted it):
   * nothing was written, and the screen re-reads the server's state rather
   * than retrying into the next question.
   */
  const submit = async (options: SubmitOptions): Promise<SubmitOutcome> => {
    // Read the LATEST render, never this closure's: the clock's expiry and a
    // spoken answer's transcript both call this after an await, and a closure
    // from the render that started them would submit what the screen held
    // then (an empty box before the draft was restored, say).
    const currentValue = valueRef.current;
    const {
      conversation: current,
      question: currentQuestion,
      turnKey: currentKey,
      prose: currentProse,
      answerable: open,
    } = view.current;
    if (!current?.prompt || currentKey === null || !open || sendingRef.current) {
      return "not_sent";
    }
    const currentStarter = starterCodeFor(currentQuestion, currentValue);
    const empty = options.voice ? false : isAnswerEmpty(currentValue, currentStarter);
    if (
      !options.timedOut &&
      !options.voice &&
      (currentValue === null || !isAnswerComplete(currentValue, currentStarter))
    ) {
      return "not_sent";
    }

    const previous = carried.current?.turnKey === currentKey ? carried.current : null;
    const behaviour =
      bridgeRef.current.collectAnswerBehaviour(currentKey) ?? previous?.behaviour ?? null;
    carried.current = { turnKey: currentKey, behaviour };

    const body: RespondBody = { turn_seq: current.turn_seq, answer: "" };
    if (options.voice) {
      body.voice_answer_id = options.voice.id;
    } else if (!empty && currentValue !== null) {
      if (currentProse) body.answer = textOf(currentValue).trim();
      else body.answer_payload = currentValue;
    }
    if (options.timedOut) body.timed_out = true;
    if (behaviour) body.behaviour = behaviour;

    sendingRef.current = true;
    setSending(true);
    setPending({
      prompt: current.prompt,
      answer: options.voice
        ? options.voice.transcript
        : empty
          ? ""
          : answerLine(currentQuestion, currentValue),
    });
    if (!options.voice) {
      setValue(currentQuestion ? emptyAnswerFor(currentQuestion) : { text: "" });
    }

    try {
      const next = await apiPost<ConversationTurn>(
        `/api/v2/assessments/conversations/${current.conversation_id}/respond`,
        body
      );
      carried.current = null;
      clearDraft(linkId, currentKey);
      adopt(next);
      return "sent";
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) {
        // Nothing was written: the turn was already answered, its time had
        // run out, or the session is paused. The server's own sentence says
        // which, and whatever it now says is current is what comes next.
        carried.current = null;
        if (!options.voice) setValue(currentValue);
        toast({
          title: "Your answer was not sent",
          description: error.message,
        });
        await resync();
        return "stale";
      }
      if (!options.voice) setValue(currentValue);
      toast({
        title: "Could not save your response",
        description:
          error instanceof Error
            ? error.message
            : "Your answer is still in the box. Please try sending it again.",
        variant: "destructive",
      });
      return "failed";
    } finally {
      sendingRef.current = false;
      setSending(false);
      setPending(null);
    }
  };

  /**
   * The face reached zero. Ask the server first: if a pause this browser did
   * not see moved the deadline, the new deadline simply takes over. Only a
   * turn the server agrees is out of time is submitted, with whatever is in
   * the field, and a spoken answer in progress is stopped and sent instead.
   */
  const expire = async (attempt = 1): Promise<void> => {
    if (expiring.current || sendingRef.current) return;
    expiring.current = true;
    // Neither the clock check nor the submission reached the server. Tried
    // again a bounded number of times, then said out loud rather than left
    // showing zero.
    const unreachable = () => {
      if (attempt < EXPIRY_SUBMIT_ATTEMPTS) {
        window.setTimeout(() => void expire(attempt + 1), EXPIRY_RETRY_MS);
      } else {
        setExpiryNotice(EXPIRY_UNREACHABLE);
      }
    };
    try {
      const expiringSeq = view.current.conversation?.turn_seq;
      if (expiringSeq === undefined) return;
      const fresh = await resync();
      if (fresh === null) {
        unreachable();
        return;
      }
      if (fresh.status !== "active" || fresh.turn_seq !== expiringSeq || fresh.turn === null) {
        return;
      }
      if (fresh.turn.paused) return;
      // Read at the server's own instant: no browser time enters it.
      const now = Date.now();
      if (remainingMs(fresh.turn.deadline_at, fresh.turn.server_now, now, now) > 0) return;
      if (voiceRef.current?.stopForExpiry()) return;
      const outcome = await submit({ timedOut: true });
      if (outcome === "failed") unreachable();
    } finally {
      expiring.current = false;
    }
  };

  const sendSpokenAnswer = async (id: string, transcript: string, timedOut: boolean) => {
    setVoiceRetry(null);
    const outcome = await submit({ timedOut, voice: { id, transcript } });
    if (outcome === "failed") setVoiceRetry({ id, transcript, timedOut });
  };

  const candidateInitials = initials(user?.full_name || "You");
  const pauseLabel =
    voiceHoldsClock
      ? PAUSE_LABELS.transcription
      : conversation?.turn?.pause_reason
        ? PAUSE_LABELS[conversation.turn.pause_reason]
        : bridgePaused
          ? "Paused"
          : null;
  const showVoice =
    Boolean(conversation?.voice_input_available) && prose && !typingOnly;
  const showTyping = !voiceOccupied;
  const inputDisabled = sending || clockPaused;

  return (
    <div>
      <PageHeader
        title="Your assessment"
        description="Answer each question in order. Your draft is saved as you go, so you can close this page and come back."
        actions={
          <Button variant="outline" asChild>
            <Link href="/portal/applications">
              <ArrowLeft className="h-4 w-4" aria-hidden="true" />
              Applied Jobs
            </Link>
          </Button>
        }
      />

      <div className="mx-auto max-w-5xl">
        {conversation ? (
          <AssessmentSteps
            answered={conversation.answered_questions}
            total={conversation.total_questions}
          />
        ) : null}
        <div className="mt-6 grid gap-6 lg:grid-cols-[15rem_minmax(0,1fr)]">
          {conversation ? (
            <AssessmentProgress
              answered={conversation.answered_questions}
              total={conversation.total_questions}
            />
          ) : (
            <div />
          )}
          <div className="min-w-0 space-y-4">
            <HistoryList entries={conversation?.history ?? []} initials={candidateInitials} />

            {pending ? (
              <div className="space-y-3 opacity-80" data-testid="pending-exchange">
                <div className="sm:mr-10">
                  <div className="rounded-2xl rounded-tl-md border border-border bg-surface p-5 text-sm leading-7 shadow-card">
                    {pending.prompt}
                  </div>
                </div>
                <div className="sm:ml-10">
                  <div className="rounded-2xl rounded-tr-md border border-brand-600/30 bg-brand-100/70 p-5 text-sm leading-7">
                    <p className="whitespace-pre-wrap">
                      {pending.answer || "No answer was given before time ran out."}
                    </p>
                  </div>
                </div>
              </div>
            ) : null}

            {loading ? (
              <p role="status" className="flex items-center justify-center gap-2 py-12 text-sm">
                <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
                Opening your assessment
              </p>
            ) : null}

            {loadError ? (
              <Card className="shadow-card" data-testid="load-error">
                <CardContent className="p-8 text-center">
                  <h2 className="text-subheading font-semibold">This assessment could not be opened</h2>
                  <p className="mt-2 text-sm">{loadError}</p>
                  <Button className="mt-5" variant="outline" asChild>
                    <Link href="/portal/applications">Back to Applied Jobs</Link>
                  </Button>
                </CardContent>
              </Card>
            ) : null}

            {sending ? (
              <div className="sm:mr-10" aria-live="polite">
                <p className="sr-only">The interviewer is typing</p>
                <div className="inline-flex items-center gap-1.5 rounded-2xl rounded-tl-md border border-border bg-surface px-5 py-4 shadow-card">
                  <Dot delay="0ms" />
                  <Dot delay="150ms" />
                  <Dot delay="300ms" />
                </div>
              </div>
            ) : null}

            {conversation?.status === "preparing" ? (
              <p
                role="status"
                className="flex items-center justify-center gap-2 py-12 text-sm"
                data-testid="preparing"
              >
                <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
                Your questions are being prepared. This page will carry on by itself in a moment.
              </p>
            ) : null}

            {conversation?.status === "terminated" ? (
              <Card className="shadow-card" data-testid="termination-notice">
                <CardContent className="p-8 text-center">
                  <h2 className="text-subheading font-semibold">This assessment has ended</h2>
                  <p className="mt-2 max-w-md text-pretty text-sm sm:mx-auto">
                    {conversation.termination_message ??
                      "The assessment was ended before its final question. The answers you had already sent were kept."}
                  </p>
                  <Button className="mt-5" variant="outline" asChild>
                    <Link href="/portal/applications">Back to Applied Jobs</Link>
                  </Button>
                </CardContent>
              </Card>
            ) : conversation?.status === "completed" ? (
              <Card className="shadow-card">
                <CardContent className="flex flex-col items-center p-8 text-center">
                  <span className="grid h-14 w-14 place-items-center rounded-2xl bg-rating-1-bg text-rating-1">
                    <CheckCircle2 className="h-7 w-7" aria-hidden="true" />
                  </span>
                  <h2 className="mt-4 text-subheading font-semibold">Assessment complete</h2>
                  <p className="mt-1 max-w-sm text-pretty text-sm">
                    Your responses were saved and your report is being compiled.
                  </p>
                  <Button className="mt-5" variant="outline" asChild>
                    <Link href="/portal/applications">Back to Applied Jobs</Link>
                  </Button>
                </CardContent>
              </Card>
            ) : conversation?.prompt && conversation.turn && turnKey !== null ? (
              // Hidden rather than unmounted while an answer is on its way, so
              // a spoken answer's state survives a submission that fails.
              <div className="space-y-4" hidden={pending !== null}>
                <div className="sm:mr-10">
                  <div className="rounded-2xl rounded-tl-md border border-border bg-surface p-5 shadow-card">
                    <div className="flex flex-wrap items-center gap-2 text-xs">
                      <span className="grid h-7 w-7 place-items-center rounded-full bg-brand-100 text-brand-700">
                        <Sparkles className="h-3.5 w-3.5" aria-hidden="true" />
                      </span>
                      <span className="font-semibold">AI Assessor</span>
                      <span className="ml-auto">
                        <TurnTimer
                          key={turnKey}
                          deadlineAt={conversation.turn.deadline_at}
                          serverNow={conversation.turn.server_now}
                          receivedAtMs={receivedAt}
                          paused={clockPaused}
                          pauseLabel={pauseLabel}
                          onExpire={() => void expire()}
                        />
                      </span>
                    </div>
                    <p className="mt-3 type-eyebrow text-brand-600">
                      {conversation.is_reask
                        ? `Re-asking ${conversation.progress_label}`
                        : conversation.progress_label}
                    </p>
                    <p className="mt-2 text-pretty leading-7">{conversation.prompt}</p>
                  </div>
                </div>

                <div className="space-y-3 sm:ml-10">
                  {expiryNotice ? (
                    <p role="alert" className="border border-warning bg-warning/10 p-3 text-sm">
                      {expiryNotice}
                    </p>
                  ) : null}
                  {voiceRetry ? (
                    <div role="alert" className="space-y-2 border border-warning bg-warning/10 p-3 text-sm">
                      <p>
                        Your spoken answer was transcribed but did not reach us. It has not been lost.
                      </p>
                      <blockquote className="whitespace-pre-wrap border-l-2 border-teal-600 bg-surface p-3 leading-7">
                        {voiceRetry.transcript}
                      </blockquote>
                      <Button
                        type="button"
                        disabled={sending}
                        onClick={() =>
                          void sendSpokenAnswer(voiceRetry.id, voiceRetry.transcript, voiceRetry.timedOut)
                        }
                      >
                        Send my spoken answer again
                      </Button>
                    </div>
                  ) : null}
                  {prose && !typingOnly && !conversation.voice_input_available ? (
                    <p className="text-xs" data-testid="voice-not-offered">
                      Answering by voice is not switched on for this assessment. Please type your
                      answer below.
                    </p>
                  ) : null}
                  {showVoice ? (
                    <div hidden={voiceRetry !== null}>
                      <VoiceAnswer
                        key={turnKey}
                        ref={voiceRef}
                        conversationId={conversation.conversation_id}
                        turnSeq={conversation.turn_seq}
                        disabled={inputDisabled || !isEmpty(value)}
                        onPhaseChange={setVoicePhase}
                        onTranscribed={(id, transcript, byClock) =>
                          void sendSpokenAnswer(id, transcript, byClock)
                        }
                        onSwitchToTyping={() => {
                          setTypingOnly(true);
                          setVoicePhase("idle");
                          void resync();
                        }}
                      />
                    </div>
                  ) : null}
                  {showTyping && fieldHooks && value !== null ? (
                    // A coding question's Run button needs the conversation it
                    // belongs to; `useCodingConversationId` throws without it.
                    <CodingConversationContext.Provider value={conversation.conversation_id}>
                      <QuestionRenderer
                        // A follow-up or a re-ask has no question row of its
                        // own; it is prose, and prose renders as a short answer.
                        question={question ?? proseTurn(turnKey)}
                        prompt={conversation.prompt}
                        value={value}
                        onChange={setValue}
                        disabled={inputDisabled}
                        autosave={autosave}
                        fieldHooks={fieldHooks}
                        onSubmitShortcut={() => void submit({ timedOut: false })}
                      />
                    </CodingConversationContext.Provider>
                  ) : null}
                  {showTyping ? (
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <AutosaveIndicator state={autosave} />
                      <div className="flex items-center gap-2">
                        <Button
                          type="button"
                          variant="outline"
                          disabled={inputDisabled || isEmpty(value)}
                          onClick={() => setValue(question ? emptyAnswerFor(question) : { text: "" })}
                        >
                          <Trash2 className="h-4 w-4" aria-hidden="true" />
                          Clear
                        </Button>
                        {question?.question_type === "coding" && isCodingAnswer(value) ? (
                          // A coding answer is final: the press confirms, naming
                          // the language, before the hidden tests are run.
                          <CodingSubmitButton
                            language={value.language}
                            disabled={inputDisabled || !complete}
                            sending={sending}
                            onConfirm={() => void submit({ timedOut: false })}
                          />
                        ) : (
                          <Button
                            size="lg"
                            disabled={inputDisabled || !complete}
                            onClick={() => void submit({ timedOut: false })}
                          >
                            {sending ? (
                              <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
                            ) : (
                              <Send className="h-4 w-4" aria-hidden="true" />
                            )}
                            {sending ? "Sending" : "Send"}
                          </Button>
                        )}
                      </div>
                    </div>
                  ) : null}
                </div>
              </div>
            ) : null}

            {/* Scroll target. An empty node rather than scrolling the last bubble,
                so the view lands below the newest message instead of pinning its
                top edge to the bottom of the viewport. */}
            <div ref={endRef} aria-hidden="true" />
          </div>
        </div>
      </div>
    </div>
  );
}

/** The question shape a prose follow-up or re-ask renders through. It has no
 *  row on the server, so its id is the turn key. */
function proseTurn(turnKey: string): QuestionOut {
  return {
    id: turnKey,
    question_type: "short_answer",
    payload: {},
    time_allocation_seconds: 0,
  };
}

/** One animated dot of the typing indicator.
 *
 * A FADE, NOT A BOUNCE. An overshooting indicator on a screen where somebody
 * is waiting to be asked the next question in an assessment of their career
 * is the product being cheerful at somebody who is nervous. DESIGN.md section
 * 7 forbids spring overshoot on anything a person is waiting on.
 */
function Dot({ delay }: { delay: string }) {
  return (
    <span
      className="h-2 w-2 rounded-full bg-navy-600/70 motion-safe:animate-[typing-dot_1.2s_ease-in-out_infinite]"
      style={{ animationDelay: delay }}
    />
  );
}

function initials(name: string): string {
  const parts = name.trim().split(/\s+/).filter(Boolean);
  return (parts[0]?.[0] || "Y") + (parts[1]?.[0] || "");
}
