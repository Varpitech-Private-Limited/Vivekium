"use client";

// The shared pieces of a support conversation, used by BOTH portals.
//
// A thread reads identically to the person who raised it and to the person
// answering it: same messages, same order, same three states. What differs is
// what surrounds it, and that lives in each portal's own screen. Rendering the
// conversation twice would be two answers to "what does this thread say", and
// the first divergence would be a customer and a support agent each believing
// they were looking at the same page.
//
// NAVY IS STRUCTURE, TEAL IS EVIDENCE. There is no evidence on this screen: a
// support thread carries no citation, no grade and nothing corroborated. So it
// is plain navy and neutral throughout, and teal is deliberately unused rather
// than spent on a status chip it would only decorate. Teal means something in
// this product; spending it here would cost it that meaning everywhere else.

import * as React from "react";

import type { SupportMessage, SupportThreadStatus } from "@/lib/types";
import { cn } from "@/lib/utils";
import { Badge } from "@/components/ui/badge";

// The status vocabulary, in one place, for both portals. A second copy would
// drift the first time a state was added, and the two portals would then
// disagree about what a thread is doing.
export const SUPPORT_STATUS_LABEL: Record<SupportThreadStatus, string> = {
  open: "Waiting on Vivekium",
  awaiting_customer: "Waiting on you",
  resolved: "Resolved",
};

// The staff queue reads the same three states from the other side of the desk,
// so the words invert. Same three states, never a fourth.
export const SUPPORT_STATUS_LABEL_STAFF: Record<SupportThreadStatus, string> = {
  open: "Waiting on us",
  awaiting_customer: "Waiting on the customer",
  resolved: "Resolved",
};

export function SupportStatusBadge({
  status,
  staff = false,
}: {
  status: SupportThreadStatus;
  staff?: boolean;
}) {
  const label = staff
    ? SUPPORT_STATUS_LABEL_STAFF[status]
    : SUPPORT_STATUS_LABEL[status];
  return (
    <Badge
      variant={status === "resolved" ? "outline" : "default"}
      className={cn(
        status === "resolved"
          ? "border-navy-200 text-ink"
          : "bg-navy-600 text-white hover:bg-navy-600",
      )}
    >
      {label}
    </Badge>
  );
}

export function formatSupportTime(value: string): string {
  const when = new Date(value);
  // An unparseable timestamp renders as nothing rather than as "Invalid Date",
  // which reads to a user as a bug in the message rather than in the clock.
  if (Number.isNaN(when.getTime())) return "";
  return when.toLocaleString(undefined, {
    day: "numeric",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
  });
}

// One message. The two sides are distinguished by ALIGNMENT and a name as well
// as by colour: a reader who cannot tell the colours apart still has to be able
// to tell who said what, and that is the entire content of this view.
export function SupportMessageBubble({
  message,
  viewerSide,
}: {
  message: SupportMessage;
  viewerSide: "customer" | "staff";
}) {
  const mine = message.author_side === viewerSide;
  const who =
    message.author_side === "staff"
      ? "Vivekium"
      : message.author_name ?? "Your team";
  return (
    <li
      className={cn(
        "flex w-full flex-col gap-1",
        mine ? "items-end" : "items-start",
      )}
    >
      <div className="flex items-baseline gap-2 text-xs">
        <span className="font-medium text-ink">{who}</span>
        <span>{formatSupportTime(message.created_at)}</span>
      </div>
      <div
        className={cn(
          "max-w-[46rem] rounded-xl border px-4 py-3 text-sm",
          // `whitespace-pre-wrap`, never a markdown renderer. A support message
          // legitimately contains a stack trace, and markdown would eat the
          // indentation that made it readable. It also means nothing a customer
          // types is ever interpreted as markup.
          "whitespace-pre-wrap break-words",
          mine
            ? "border-navy-600 bg-navy-600 text-white"
            : "border-border bg-card text-foreground",
        )}
      >
        {message.body}
      </div>
    </li>
  );
}

export function SupportConversation({
  messages,
  viewerSide,
}: {
  messages: SupportMessage[];
  viewerSide: "customer" | "staff";
}) {
  if (messages.length === 0) {
    // Should not happen: a thread is created with its first message in one
    // call. Stated honestly rather than rendered as a blank panel, because
    // "nothing here" and "failed to load" must not look the same.
    return <p className="text-sm text-ink">This conversation has no messages yet.</p>;
  }
  return (
    <ul className="flex flex-col gap-5">
      {messages.map((message) => (
        <SupportMessageBubble
          key={message.id}
          message={message}
          viewerSide={viewerSide}
        />
      ))}
    </ul>
  );
}
