import { CalendarClock, Loader2 } from "lucide-react";

import { Button } from "@/components/ui/button";

import { cn } from "@/lib/utils";
import {
  POSTING_STATUS_LABELS,
  type Job,
  type PostingStatus,
} from "@/lib/types";

/**
 * The fixed 30-day posting window, shown prominently on the job page (spec
 * §2.1). The recruiter has no control over these dates, they are stamped at
 * publish and the end dates are database-generated, so this is presented as
 * information, never as an editable field.
 */

const STATUS_STYLES: Record<PostingStatus, string> = {
  // Colour is used sparingly (claude.md: monochrome except rating labels), so
  // these lean on border and weight rather than fills.
  active: "border-emerald-700 text-emerald-900 dark:text-emerald-200",
  grace_period: "border-amber-600 text-amber-950 dark:text-amber-100",
  expired: "border-border",
  scheduled: "border-border",
  // A deliberate ending reads as the navy structural colour rather than as a
  // warning: the requirement was met, which is the good outcome.
  closed: "border-navy-600 text-navy-900 dark:text-white",
};

function formatDate(value?: string | null): string {
  if (!value) return "-";
  return new Date(value).toLocaleDateString(undefined, {
    day: "numeric",
    month: "short",
    year: "numeric",
  });
}

export function PostingWindowBanner({
  job,
  className,
  onRenew,
  renewing = false,
  onClose,
  closing = false,
}: {
  job: Job;
  className?: string;
  /** Omit to hide the Renew action (caller lacks publish_job). */
  onRenew?: () => void;
  renewing?: boolean;
  /** Omit to hide the Close action (caller lacks publish_job). */
  onClose?: () => void;
  closing?: boolean;
}) {
  const status = (job.posting_status ?? "expired") as PostingStatus;
  if (!job.posting_start_date) return null;

  return (
    <section
      aria-label="Job posting window"
      className={cn(
        // SEMANTIC left rule, and a documented Impeccable `side-tab`
        // exception (.impeccable-exceptions.md). Its colour carries the
        // posting state -- live, in grace, closed -- so the rule IS the
        // information rather than a decoration beside it.
        "flex flex-wrap items-start gap-x-6 gap-y-2 rounded-lg border-l-4 bg-muted/40 px-4 py-3 text-sm",
        STATUS_STYLES[status],
        className
      )}
    >
      <div className="flex items-center gap-2 font-semibold">
        <CalendarClock className="h-4 w-4" aria-hidden />
        {POSTING_STATUS_LABELS[status]}
      </div>
      <dl className="flex flex-wrap gap-x-6 gap-y-1">
        <div className="flex gap-1.5">
          <dt>Live from</dt>
          <dd className="font-medium">{formatDate(job.posting_start_date)}</dd>
        </div>
        <div className="flex gap-1.5">
          <dt>Applications close</dt>
          <dd className="font-medium">{formatDate(job.posting_end_date)}</dd>
        </div>
        <div className="flex gap-1.5">
          <dt>Grace period ends</dt>
          <dd className="font-medium">
            {formatDate(job.grace_period_end_date)}
          </dd>
        </div>
      </dl>
      {job.posting_summary ? (
        <p className="w-full text-xs">{job.posting_summary}</p>
      ) : null}
      {job.closed_reason ? (
        <p className="w-full text-xs">
          Your note: {job.closed_reason}
        </p>
      ) : null}
      {status === "closed" ? null : (
        <p className="w-full text-xs">
          Every posting runs for exactly 30 days, then 5 days in which existing
          applicants can still update their application. These dates are set
          automatically and cannot be changed. You can close the job sooner once
          the requirement is met.
        </p>
      )}
      {/* Close is offered only while the posting is still taking applications.
          There is no reopen, so the confirmation lives at the call site rather
          than behind a bare button here. */}
      {onClose && (status === "active" || status === "scheduled") ? (
        <div className="flex w-full flex-wrap items-center gap-3 pt-1">
          <Button size="sm" variant="outline" onClick={onClose} disabled={closing}>
            {closing ? (
              <>
                <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
                Closing
              </>
            ) : (
              "Close, requirement met"
            )}
          </Button>
          <span className="text-xs">
            Stops new applications straight away. Everyone already in your
            pipeline stays exactly where they are.
          </span>
        </div>
      ) : null}
      {/* Renew is offered only once the window has actually closed. The rule
          that publish cannot re-stamp a live posting exists so nobody silently
          extends one, and a Renew button on a live job would be the same thing
          wearing a different label. */}
      {onRenew && (status === "grace_period" || status === "expired") ? (
        <div className="flex w-full flex-wrap items-center gap-3 pt-1">
          <Button size="sm" variant="outline" onClick={onRenew} disabled={renewing}>
            {renewing ? (
              <>
                <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
                Renewing
              </>
            ) : (
              "Renew for another 30 days"
            )}
          </Button>
          <span className="text-xs">
            Everyone who already applied stays in your candidate list, marked as
            an earlier applicant.
          </span>
        </div>
      ) : null}
    </section>
  );
}

/** Compact variant for a job list row. */
export function PostingWindowChip({ job }: { job: Job }) {
  const status = (job.posting_status ?? "expired") as PostingStatus;
  if (!job.posting_start_date) return null;
  const detail =
    status === "active"
      ? `${job.days_until_posting_ends ?? 0}d left`
      : status === "grace_period"
        ? `grace: ${job.days_until_grace_ends ?? 0}d`
        : null;
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 rounded border px-1.5 py-0.5 text-chip font-medium",
        STATUS_STYLES[status]
      )}
    >
      {POSTING_STATUS_LABELS[status]}
      {detail ? <span className="font-normal">· {detail}</span> : null}
    </span>
  );
}
