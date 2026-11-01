"use client";

// The sound-level reminder (mission item B, 2026-11-01).
//
// NOT A WARNING. It does not block, holds no clock, counts toward no limit and
// has no acknowledgement the server waits for. It is a polite status line the
// candidate can ignore or dismiss, and it clears itself. The sentence is the
// hedged one the requirement gave: a microphone level cannot say what made the
// noise, so this says only that the level stayed high.

import * as React from "react";

import { Button } from "@/components/ui/button";

/** How long the reminder stays up if the candidate does nothing. */
export const ADVISORY_VISIBLE_MS = 15_000;

export function AdvisoryNotice({
  message,
  onDismiss,
}: {
  /** Null when there is no reminder to show. */
  message: string | null;
  onDismiss: () => void;
}) {
  React.useEffect(() => {
    if (message === null) return;
    const timer = setTimeout(onDismiss, ADVISORY_VISIBLE_MS);
    return () => clearTimeout(timer);
  }, [message, onDismiss]);

  if (message === null) return null;
  return (
    <div
      className="fixed bottom-16 right-4 z-40 flex max-w-sm items-start gap-3 border border-border bg-surface px-3 py-2 text-xs shadow-card"
      role="status"
      aria-live="polite"
    >
      <span>{message}</span>
      <Button variant="outline" size="sm" onClick={onDismiss}>
        Dismiss
      </Button>
    </div>
  );
}
