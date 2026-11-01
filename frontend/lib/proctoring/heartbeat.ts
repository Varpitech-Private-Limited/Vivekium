/**
 * The heartbeat (proctoring spec section 9).
 *
 * Every `heartbeat_interval_seconds` the client tells the server it is still
 * there, whether the last identity check matched, and what its own
 * monitoring self-check found. A gap longer than the server's allowance is
 * recorded there as a monitoring interruption and appears in the report,
 * which is the honest alternative to a clean report for a period nobody was
 * watching. The next interval comes back on the response, so the server can
 * slow a client down without a deploy.
 *
 * A failed post is not retried early: the following beat is the retry, and a
 * gap that opens because the network was down is exactly what the server is
 * supposed to record.
 *
 * Each answer also carries the server's clock and the device pause (Phase 3,
 * 2026-09-24). Both are handed to `onResult` with the moments the request left
 * and the answer arrived, so the session can place the server's grace deadline
 * on this device's clock: the countdown a candidate watches is the server's
 * deadline, not one this client started.
 */
import type { DetectionStats, HeartbeatIn, HeartbeatOut, MonitoringStatus, TerminationOut } from "./api";
import { isSessionEnded } from "./api";

export interface HeartbeatOptions {
  intervalMs: number;
  post: (body: HeartbeatIn) => Promise<HeartbeatOut>;
  monitoring: () => MonitoringStatus;
  /** Camera-analysis telemetry for the interval just ended, when there is a
   *  camera monitor to ask. */
  detection?: () => DetectionStats | null;
  /** The most recent identity comparison, or null when none has run. */
  identityMatched: () => boolean | null;
  onTermination: (termination: TerminationOut) => void;
  onSessionEnded: (message: string) => void;
  /** Every successful answer, with this device's clock when the request was
   *  sent and when the answer arrived. */
  onResult?: (result: HeartbeatOut, sentAtMs: number, receivedAtMs: number) => void;
  now?: () => number;
}

export interface HeartbeatHandle {
  stop(): void;
  /** Send one now, outside the schedule. Used by the integrity check. */
  beat(): Promise<void>;
}

export function startHeartbeat(options: HeartbeatOptions): HeartbeatHandle {
  let stopped = false;
  let timer: ReturnType<typeof setTimeout> | null = null;
  let nextIntervalMs = options.intervalMs;
  const now = options.now ?? Date.now;

  const beat = async () => {
    if (stopped) return;
    try {
      const sentAt = now();
      const result = await options.post({
        identity_matched: options.identityMatched(),
        monitoring: options.monitoring(),
        detection: options.detection?.() ?? null,
      });
      if (stopped) return;
      options.onResult?.(result, sentAt, now());
      if (result.interval_seconds > 0) nextIntervalMs = result.interval_seconds * 1000;
      if (result.termination) {
        stop();
        options.onTermination(result.termination);
      }
    } catch (error) {
      if (isSessionEnded(error)) {
        stop();
        options.onSessionEnded((error as Error).message);
      }
    }
  };

  const schedule = () => {
    if (stopped) return;
    timer = setTimeout(async () => {
      await beat();
      schedule();
    }, nextIntervalMs);
  };

  const stop = () => {
    stopped = true;
    if (timer !== null) clearTimeout(timer);
    timer = null;
  };

  void beat().then(schedule);
  return { stop, beat };
}
