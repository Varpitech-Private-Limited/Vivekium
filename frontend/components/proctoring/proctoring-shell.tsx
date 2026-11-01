"use client";

// The proctoring shell: the four screens a monitored assessment passes
// through, and the owner of the monitoring session behind them.
//
//   consenting -> checking -> active -> ended
//
// CONSENT FIRST, ALWAYS. Nothing opens a device or creates a session until
// the candidate has pressed "I understand and agree" (spec 8.1). The system
// check runs next and the assessment cannot begin until every row passes
// (spec 8.2). Only then is a session created, the detectors started and the
// children mounted, so there is no moment at which a question is on screen
// without monitoring. Proctoring is mandatory and this component has no other
// branch: a failure to start the session is a screen that says so and offers
// a retry, never an unmonitored assessment.
//
// WHAT THE CHILDREN SEE. A `ProctoringBridge`, and nothing else. The player
// reads the warning count for its own display, asks for field hooks per
// question, collects the timings when it submits, reads whether the
// assessment is held still (`paused`), and tells the shell when the
// conversation ended. It never touches a detector and no detector knows the
// player exists.
//
// THE SERVER DECIDES. Every warning, every pause and every termination on
// these screens arrived on a response. This component counts nothing and
// concludes nothing: the pause screen renders the server's deadline and the
// server's count, and a warning's hold on the question timer is the server's
// to measure (the acknowledgement tells it when the hold ended).
//
// THE RULES COME FROM THE SERVER (Phase 3, 2026-09-24). The consent screen
// used to carry seven hard-coded statements; it now renders the server's
// `candidate_rules`, and the shell will not offer the agreement until they
// have loaded.

import * as React from "react";
import Link from "next/link";

import { MonitoringIndicator } from "@/components/proctoring/monitoring-indicator";
import { PauseOverlay } from "@/components/proctoring/pause-overlay";
import { ProctoringProvider } from "@/components/proctoring/proctoring-context";
import { AdvisoryNotice } from "@/components/proctoring/advisory-notice";
import { ConsentScreen } from "@/components/proctoring/consent-screen";
import { SystemCheckScreen } from "@/components/proctoring/system-check-screen";
import { WarningModal } from "@/components/proctoring/warning-modal";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import type { AnswerBehaviour, ProctoringBridge, ProctoringFieldHooks } from "@/lib/assessment/contracts";
import {
  createSession,
  fetchClientConfig,
  startSessionMedia,
  type ClientConfigOut,
  type SessionMediaStartOut,
  type SessionOut,
  type TerminationOut,
  type WarningOut,
} from "@/lib/proctoring/api";
import { NO_PAUSE, pauseScreenOpen, SessionRuntime, type PauseView } from "@/lib/proctoring/session";
import {
  releaseOutcome,
  runSystemCheck,
  type CheckRow,
  type SystemCheckOutcome,
} from "@/lib/proctoring/system-check";

export const START_FAILED_TITLE = "We could not start the monitoring";
export const ENDED_TITLE = "Your assessment has ended";
/** Said whether the session ended well or early: the answers already given
 *  are saved either way, and that is the first thing a candidate asks. */
export const ANSWERS_SAVED = "The answers you gave up to that point were saved.";

type Phase = "consenting" | "checking" | "active" | "ended";

/** The hooks handed to a field before the session exists. They record
 *  nothing, because there is no session for them to record against, and they
 *  are never reachable: the children mount only in the active phase. */
const NO_HOOKS: ProctoringFieldHooks = {
  onFieldFocus: () => undefined,
  onFieldBlur: () => undefined,
  onKeyDown: () => undefined,
  onBlockedAction: () => undefined,
  onOptionClick: () => undefined,
  onScroll: () => undefined,
};

export function ProctoringShell({
  linkId,
  children,
}: {
  linkId: string;
  children: React.ReactNode;
}) {
  const [phase, setPhase] = React.useState<Phase>("consenting");
  const [loaded, setLoaded] = React.useState<ClientConfigOut | null>(null);
  const [configError, setConfigError] = React.useState<string | null>(null);
  const [configAttempt, setConfigAttempt] = React.useState(0);
  const [rows, setRows] = React.useState<CheckRow[] | null>(null);
  const [checking, setChecking] = React.useState(false);
  const [checkError, setCheckError] = React.useState<string | null>(null);
  const [startError, setStartError] = React.useState<string | null>(null);
  const [session, setSession] = React.useState<SessionOut | null>(null);
  const [warningsUsed, setWarningsUsed] = React.useState(0);
  const [warning, setWarning] = React.useState<WarningOut | null>(null);
  const [endedMessage, setEndedMessage] = React.useState<string | null>(null);
  const [pause, setPause] = React.useState<PauseView>(NO_PAUSE);
  const [retrying, setRetrying] = React.useState(false);
  const [advisory, setAdvisory] = React.useState<string | null>(null);

  const config = loaded?.config ?? null;
  const runtime = React.useRef<SessionRuntime | null>(null);
  /** One check at a time. A ref rather than the `checking` state because the
   *  guard has to hold within a render, before the state has been applied. */
  const running = React.useRef(false);
  const outcome = React.useRef<SystemCheckOutcome | null>(null);

  const finish = React.useCallback((message: string) => {
    runtime.current?.stop();
    runtime.current = null;
    setEndedMessage(message);
    setPhase("ended");
  }, []);

  // The thresholds and the rules, before anything else. They are the
  // server's numbers and the server's sentences and the client holds none of
  // its own, so neither the agreement nor the check can happen without them.
  React.useEffect(() => {
    let cancelled = false;
    setConfigError(null);
    fetchClientConfig()
      .then((response) => {
        if (!cancelled) setLoaded(response);
      })
      .catch((error: unknown) => {
        if (!cancelled) {
          setConfigError(
            error instanceof Error ? error.message : "The monitoring settings could not be loaded."
          );
        }
      });
    return () => {
      cancelled = true;
    };
  }, [configAttempt]);

  // Whatever happens, the camera light goes out. A tab closed mid-assessment
  // must not leave a stream running behind it.
  React.useEffect(
    () => () => {
      runtime.current?.stop();
      runtime.current = null;
      if (outcome.current) releaseOutcome(outcome.current);
      outcome.current = null;
    },
    []
  );

  const check = React.useCallback(async () => {
    if (!config || running.current) return;
    running.current = true;
    setChecking(true);
    setCheckError(null);
    if (outcome.current) {
      releaseOutcome(outcome.current);
      outcome.current = null;
    }
    try {
      const result = await runSystemCheck(config);
      outcome.current = result;
      setRows(result.rows);
    } catch (error) {
      setCheckError(
        error instanceof Error ? error.message : "The setup check could not be completed. Please try again."
      );
      setRows(null);
    } finally {
      running.current = false;
      setChecking(false);
    }
  }, [config]);

  /**
   * The check runs when the phase reaches it AND the thresholds have arrived,
   * whichever happens second. Agreeing before the config request has returned
   * is the ordinary case on a slow connection, and a check fired from the
   * button alone would find no config and quietly do nothing.
   */
  React.useEffect(() => {
    if (phase !== "checking" || config === null || rows !== null || running.current) return;
    void check();
  }, [check, config, phase, rows]);

  const agree = React.useCallback(() => {
    setPhase("checking");
  }, []);

  /**
   * Create the session and start monitoring. Called from the Start button, so
   * it also has the user gesture fullscreen needs.
   */
  const start = React.useCallback(async () => {
    const result = outcome.current;
    if (!result || !result.allPassed || !result.camera || !result.microphone || !result.inference) return;
    if (result.faceDescriptor === null) return;
    setStartError(null);
    try {
      const created = await createSession(linkId, {
        consent: true,
        device_context: result.deviceContext,
        system_check: result.payload,
        face_descriptor: result.faceDescriptor,
      });
      // The stored record of this session (owner ruling, 2026-09-22). Opened
      // AFTER the monitoring session, because the proctoring gate is what
      // authorizes it. A failure to open one NEVER blocks the assessment: the
      // candidate answers, and the hiring team's dashboard says honestly that
      // no recording arrived.
      let mediaRecording: SessionMediaStartOut | null = null;
      try {
        mediaRecording = await startSessionMedia(linkId);
      } catch (error) {
        console.warn(
          "assessment recording could not be opened: " +
            (error instanceof Error ? error.message : "unknown")
        );
      }
      const started = new SessionRuntime(
        created,
        { camera: result.camera, microphone: result.microphone, inference: result.inference },
        {
          onWarning: (issued, used) => {
            setWarningsUsed(used);
            setWarning(issued);
          },
          onTermination: (termination: TerminationOut) => finish(termination.message),
          onSessionEnded: (message) => finish(message),
          onPauseChange: setPause,
          onAdvisory: setAdvisory,
        },
        mediaRecording
      );
      // The media and the workers now belong to the runtime, which stops them.
      outcome.current = null;
      runtime.current = started;
      started.start();
      // The baseline the check took is what later checks compare against, and
      // it lives in the worker rather than on this thread.
      result.inference.setBaseline(result.faceDescriptor);
      void started.requestFullscreen();
      setSession(created);
      setWarningsUsed(created.warnings_used);
      setPhase("active");
    } catch (error) {
      setStartError(
        error instanceof Error
          ? error.message
          : "The monitoring session could not be started. Please try again."
      );
    }
  }, [finish, linkId]);

  const acknowledge = React.useCallback(() => {
    setWarning(null);
    // The server holds the question timer until it hears this.
    void runtime.current?.acknowledgeWarning();
    void runtime.current?.requestFullscreen();
  }, []);

  const retryDevices = React.useCallback(async () => {
    const active = runtime.current;
    if (!active) return;
    setRetrying(true);
    try {
      await active.retryDevices();
    } finally {
      setRetrying(false);
    }
    // Reopening a device may have needed a browser prompt, which can take the
    // page out of fullscreen; this click is the gesture that restores it.
    void active.requestFullscreen();
  }, []);

  const graceExpired = React.useCallback(() => {
    runtime.current?.checkNow();
  }, []);

  const dismissAdvisory = React.useCallback(() => setAdvisory(null), []);

  const pauseOpen = pauseScreenOpen(pause);

  const bridge = React.useMemo<ProctoringBridge>(() => {
    const value = {
      status: phase,
      sessionId: session?.session_id ?? null,
      warningsUsed,
      maxWarnings: session?.max_warnings ?? config?.max_warnings ?? 0,
      endedMessage,
      // Held still: a warning on screen or the device pause. The player
      // freezes its countdown and refuses input while this is true, and
      // re-reads the server's clock when it turns false.
      paused: warning !== null || pauseOpen,
      fieldHooksFor: (questionKey: string) => runtime.current?.fieldHooksFor(questionKey) ?? NO_HOOKS,
      collectAnswerBehaviour: (questionKey: string): AnswerBehaviour | null =>
        runtime.current?.collectAnswerBehaviour(questionKey) ?? null,
      onConversationEnded: (status: "completed" | "terminated") => {
        // Flush what is queued before the detectors are torn down, so the
        // last few events reach the report rather than dying with the page.
        const active = runtime.current;
        if (!active) return;
        void active.flush().finally(() => {
          active.stop();
          runtime.current = null;
        });
        if (status === "terminated" && phase !== "ended") {
          finish("This assessment was ended early. " + ANSWERS_SAVED);
        }
      },
    };
    // Built as a value rather than returned as a literal, so the bridge can
    // carry `paused` for the player that reads it while the contract it is
    // checked against here still declares the member it replaces.
    return value;
  }, [config, endedMessage, finish, pauseOpen, phase, session, warning, warningsUsed]);

  if (phase === "consenting") {
    return (
      <ConsentScreen
        rules={loaded?.candidate_rules ?? null}
        error={configError}
        onAgree={agree}
        onRetry={() => setConfigAttempt((attempt) => attempt + 1)}
      />
    );
  }

  if (phase === "checking") {
    return (
      <div className="space-y-4">
        <SystemCheckScreen
          rows={rows}
          running={checking || config === null}
          error={startError ?? checkError}
          onRetry={() => {
            setRows(null);
          }}
          onStart={() => void start()}
        />
      </div>
    );
  }

  if (phase === "ended" || session === null) {
    return (
      <div className="mx-auto max-w-2xl">
        <Card>
          <CardHeader>
            <CardTitle>{session === null ? START_FAILED_TITLE : ENDED_TITLE}</CardTitle>
          </CardHeader>
          <CardContent className="space-y-4">
            <p className="text-sm">{endedMessage ?? startError ?? ANSWERS_SAVED}</p>
            <p className="text-sm">
              If you believe this was a mistake, reply to the email that invited you and the hiring
              team will look at it.
            </p>
            <Button variant="outline" asChild>
              <Link href="/portal/applications">Back to Applied Jobs</Link>
            </Button>
          </CardContent>
        </Card>
      </div>
    );
  }

  return (
    <ProctoringProvider value={bridge}>
      {children}
      <MonitoringIndicator
        warningsUsed={warningsUsed}
        maxWarnings={session.max_warnings}
        paused={pauseOpen}
      />
      <WarningModal message={warning?.message ?? null} onAcknowledge={acknowledge} />
      <AdvisoryNotice message={advisory} onDismiss={dismissAdvisory} />
      <PauseOverlay
        view={pause}
        open={pauseOpen}
        retrying={retrying}
        onRetry={() => void retryDevices()}
        onExpired={graceExpired}
      />
    </ProctoringProvider>
  );
}
