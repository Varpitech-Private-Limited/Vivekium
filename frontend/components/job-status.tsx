"use client";

/**
 * Durable status for any background job, read from the server's job ledger.
 *
 * ONE COMPONENT FOR EVERY LONG OPERATION. Mount it with the resource a job
 * is about:
 *
 *   <JobStatus resourceType="candidate_project" resourceId={project.id} />
 *
 * The backend (`GET /job-status/resource/{type}/{id}`, `workers/jobs.py`)
 * decides what is true. This component only tells it plainly:
 *
 * - the CURRENT stage and a server-written sentence, never a guessed one;
 * - "Last updated", from the ledger's own timestamp;
 * - a progress bar ONLY when the task reported a measurable done/total;
 * - a time estimate ONLY when the server found enough recent runs of the same
 *   kind. Otherwise it says "Time estimate unavailable". There is no fixed
 *   fallback minute count and no animated bar that implies progress;
 * - backoff polling (3 s growing to 30 s while nothing changes), paused while
 *   the tab is hidden, stopped for good at a terminal state;
 * - a Retry button only when the server says the job can be retried.
 *
 * It renders nothing when the resource has never had a job (a 404), so it is
 * safe to mount unconditionally.
 */
import * as React from "react";

import { Button } from "@/components/ui/button";
import { ApiError, apiGet, apiPost } from "@/lib/api";

export type JobState =
  | "queued"
  | "accepted"
  | "capacity_blocked"
  | "running"
  | "waiting_external"
  | "retrying"
  | "succeeded"
  | "failed";

export interface JobStatusData {
  job_id: string;
  kind: string;
  resource_type: string | null;
  resource_id: string | null;
  state: JobState;
  terminal: boolean;
  stage: string | null;
  message: string;
  queued_at: string;
  started_at: string | null;
  heartbeat_at: string | null;
  updated_at: string;
  completed_at: string | null;
  attempts: number;
  failure_class: string | null;
  next_retry_at: string | null;
  percent: number | null;
  estimate: { low_seconds: number; high_seconds: number; samples: number } | null;
  can_retry: boolean;
}

export const POLL_START_MS = 3000;
export const POLL_MAX_MS = 30000;

/** 3 s, then 1.5x per unchanged poll, capped at 30 s. A change resets it. */
export function nextPollDelayMs(unchangedPolls: number): number {
  return Math.min(POLL_MAX_MS, Math.round(POLL_START_MS * 1.5 ** unchangedPolls));
}

/** Whole minutes for anything over a minute, otherwise seconds. */
function span(seconds: number): string {
  if (seconds < 60) return `${Math.max(1, Math.round(seconds))} seconds`;
  const minutes = Math.round(seconds / 60);
  return minutes === 1 ? "1 minute" : `${minutes} minutes`;
}

/** The server's estimate in words, or the honest absence of one. */
export function estimateText(job: JobStatusData): string {
  const estimate = job.estimate;
  if (job.terminal || !estimate) return "Time estimate unavailable";
  const range =
    estimate.high_seconds <= estimate.low_seconds + 5
      ? `about ${span(estimate.high_seconds)}`
      : `${span(estimate.low_seconds)} to ${span(estimate.high_seconds)}`;
  return `Estimated ${range} left, from ${estimate.samples} recent runs`;
}

export function lastUpdatedText(iso: string, now: number = Date.now()): string {
  const seconds = Math.max(0, Math.round((now - Date.parse(iso)) / 1000));
  if (seconds < 10) return "Last updated just now";
  if (seconds < 60) return `Last updated ${seconds} seconds ago`;
  return `Last updated ${span(seconds)} ago`;
}

export interface UseJobStatus {
  job: JobStatusData | null;
  error: string | null;
  retrying: boolean;
  retry: () => Promise<void>;
}

export function useJobStatus(
  resourceType: string,
  resourceId: string,
  onTerminal?: (job: JobStatusData) => void
): UseJobStatus {
  const [job, setJob] = React.useState<JobStatusData | null>(null);
  const [error, setError] = React.useState<string | null>(null);
  const [retrying, setRetrying] = React.useState(false);
  const [epoch, setEpoch] = React.useState(0);
  const terminalRef = React.useRef(onTerminal);
  terminalRef.current = onTerminal;

  React.useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let unchanged = 0;
    let lastSeen = "";

    const schedule = () => {
      if (cancelled) return;
      timer = setTimeout(poll, nextPollDelayMs(unchanged));
    };

    const poll = async () => {
      if (cancelled) return;
      // Hidden tab: no request, no timer. `visibilitychange` restarts it.
      if (document.hidden) return;
      try {
        const next = await apiGet<JobStatusData>(
          `/job-status/resource/${encodeURIComponent(resourceType)}/${encodeURIComponent(resourceId)}`
        );
        if (cancelled) return;
        setError(null);
        setJob(next);
        const signature = `${next.state}|${next.stage}|${next.updated_at}`;
        unchanged = signature === lastSeen ? unchanged + 1 : 0;
        lastSeen = signature;
        if (next.terminal) {
          terminalRef.current?.(next);
          return;
        }
      } catch (e) {
        if (cancelled) return;
        if (e instanceof ApiError && e.status === 404) {
          setJob(null);
          return; // never had a job: nothing to show, nothing to poll
        }
        setError("Status is unavailable right now. Trying again.");
        unchanged += 1;
      }
      schedule();
    };

    const onVisibility = () => {
      if (!document.hidden && !cancelled) {
        if (timer) clearTimeout(timer);
        unchanged = 0;
        void poll();
      }
    };

    document.addEventListener("visibilitychange", onVisibility);
    void poll();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [resourceType, resourceId, epoch]);

  const retry = React.useCallback(async () => {
    if (!job) return;
    setRetrying(true);
    try {
      setJob(await apiPost<JobStatusData>(`/job-status/${job.job_id}/retry`));
      setError(null);
      setEpoch((value) => value + 1); // restart polling
    } catch {
      setError("This could not be retried. Please try again in a moment.");
    } finally {
      setRetrying(false);
    }
  }, [job]);

  return { job, error, retrying, retry };
}

export interface JobStatusProps {
  resourceType: string;
  resourceId: string;
  /** A short noun phrase for the accessible name, such as "Project analysis". */
  label?: string;
  onTerminal?: (job: JobStatusData) => void;
  className?: string;
}

export function JobStatus({
  resourceType,
  resourceId,
  label = "Background work",
  onTerminal,
  className,
}: JobStatusProps) {
  const { job, error, retrying, retry } = useJobStatus(
    resourceType,
    resourceId,
    onTerminal
  );
  if (!job) return null;

  const failed = job.state === "failed";
  return (
    <div
      role="status"
      aria-live="polite"
      aria-label={label}
      data-job-state={job.state}
      className={className}
    >
      <p className="text-sm font-medium">{job.stage ?? job.message}</p>
      {job.stage ? <p className="text-sm text-muted-foreground">{job.message}</p> : null}
      {job.percent !== null ? (
        <progress
          className="mt-2 h-2 w-full"
          value={job.percent}
          max={100}
          aria-label={`${label} progress`}
        />
      ) : null}
      <p className="mt-1 text-xs text-muted-foreground">
        {lastUpdatedText(job.updated_at)}
        {job.terminal ? "" : `. ${estimateText(job)}`}
      </p>
      {error ? <p className="mt-1 text-xs text-muted-foreground">{error}</p> : null}
      {failed && job.can_retry ? (
        <Button
          type="button"
          variant="outline"
          size="sm"
          className="mt-2"
          disabled={retrying}
          onClick={() => void retry()}
        >
          {retrying ? "Retrying" : "Try again"}
        </Button>
      ) : null}
    </div>
  );
}
