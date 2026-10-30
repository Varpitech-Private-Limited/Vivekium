"use client";

// ReadyPick's support queue, across every customer.
//
// The same conversation view the customer sees, from the other side of the
// desk. What this screen adds is the two facts a queue needs and a customer
// does not: WHOSE thread it is, and how much is waiting on us in total.
//
// `open_total` is deliberately NOT narrowed by the filters. Filtered, it would
// answer "how many are on the screen you happen to be looking at", which is the
// number somebody stops checking. Unnarrowed it answers "how much is owed",
// the same shape and the same reasoning as the New Candidates count on the job
// page.
//
// Filtering and paging both happen in SQL, before the page is cut. Filtering a
// fetched page in the browser makes the match count depend on which page was
// loaded, which is the defect the customer list already records.

import * as React from "react";
import { Inbox } from "lucide-react";

import { apiGet, apiPatch, apiPost } from "@/lib/api";
import { apiErrorMessage } from "@/lib/validation-errors";
import type {
  ProviderSupportThread,
  ProviderSupportThreadDetail,
  ProviderSupportThreadPage,
  SupportThreadStatus,
} from "@/lib/types";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Textarea } from "@/components/ui/textarea";
import { useToast } from "@/components/ui/toast";
import {
  SupportConversation,
  SupportStatusBadge,
  formatSupportTime,
} from "@/components/support-thread-view";

const MAX_BODY = 20_000;

// The three real states plus "all". Written as data so the control cannot
// drift from the vocabulary the server validates against, which refuses an
// unrecognised value with a 422 rather than ignoring it and returning
// everything.
const FILTERS: Array<{ value: SupportThreadStatus | "all"; label: string }> = [
  { value: "open", label: "Waiting on us" },
  { value: "awaiting_customer", label: "Waiting on the customer" },
  { value: "resolved", label: "Resolved" },
  { value: "all", label: "All" },
];

export function SupportProviderQueue() {
  const { toast } = useToast();
  const [filter, setFilter] = React.useState<SupportThreadStatus | "all">("open");
  const [page, setPage] = React.useState<ProviderSupportThreadPage | null>(null);
  const [loadError, setLoadError] = React.useState<string | null>(null);
  const [selectedId, setSelectedId] = React.useState<string | null>(null);
  const [detail, setDetail] =
    React.useState<ProviderSupportThreadDetail | null>(null);
  const [reply, setReply] = React.useState("");
  const [busy, setBusy] = React.useState(false);

  const loadQueue = React.useCallback(async () => {
    const query = filter === "all" ? "" : `?status=${filter}`;
    try {
      const result = await apiGet<ProviderSupportThreadPage>(
        `/provider/support/threads${query}`,
      );
      setPage(result);
      setLoadError(null);
    } catch (error) {
      // Never an empty queue on failure. "Nobody is waiting" is the one wrong
      // answer this screen must never give, because it is the answer that makes
      // somebody stop looking.
      setPage(null);
      setLoadError(apiErrorMessage(error));
    }
  }, [filter]);

  React.useEffect(() => {
    void loadQueue();
  }, [loadQueue]);

  const openDetail = React.useCallback(
    async (threadId: string) => {
      setSelectedId(threadId);
      try {
        setDetail(
          await apiGet<ProviderSupportThreadDetail>(
            `/provider/support/threads/${threadId}`,
          ),
        );
      } catch (error) {
        toast({ title: apiErrorMessage(error), variant: "destructive" });
      }
    },
    [toast],
  );

  async function submitReply(event: React.FormEvent) {
    event.preventDefault();
    if (!selectedId) return;
    setBusy(true);
    try {
      await apiPost(`/provider/support/threads/${selectedId}/messages`, {
        body: reply,
      });
      setReply("");
      // Re-read rather than appending locally: the server decides the new
      // status and the assignment, and a locally appended message would leave
      // the badge saying the opposite of what the queue believes.
      await openDetail(selectedId);
      await loadQueue();
    } catch (error) {
      toast({ title: apiErrorMessage(error), variant: "destructive" });
    } finally {
      setBusy(false);
    }
  }

  async function move(status: SupportThreadStatus) {
    if (!selectedId) return;
    setBusy(true);
    try {
      await apiPatch(`/provider/support/threads/${selectedId}`, { status });
      await openDetail(selectedId);
      await loadQueue();
    } catch (error) {
      // A refused move names both ends. Surfaced verbatim rather than replaced
      // with "could not update", which tells the reader nothing about why.
      toast({ title: apiErrorMessage(error), variant: "destructive" });
    } finally {
      setBusy(false);
    }
  }

  const threads: ProviderSupportThread[] = page?.items ?? [];

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="type-page-title text-ink">Support</h1>
          <p className="text-sm text-ink">
            Conversations from every customer. Replying claims the thread.
          </p>
        </div>
        {page ? (
          <p className="text-sm font-medium text-ink">
            {page.open_total} waiting on us
          </p>
        ) : null}
      </div>

      <div className="flex flex-wrap gap-2">
        {FILTERS.map((option) => (
          <Button
            key={option.value}
            size="sm"
            variant={filter === option.value ? "default" : "outline"}
            onClick={() => {
              setFilter(option.value);
              setSelectedId(null);
              setDetail(null);
            }}
          >
            {option.label}
          </Button>
        ))}
      </div>

      <div className="grid gap-6 lg:grid-cols-[24rem_1fr]">
        <Card>
          <CardHeader>
            <CardTitle>Queue</CardTitle>
          </CardHeader>
          <CardContent>
            {loadError ? (
              <div className="space-y-3">
                <p className="text-sm text-ink">{loadError}</p>
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => void loadQueue()}
                >
                  Try again
                </Button>
              </div>
            ) : page === null ? (
              <p className="text-sm text-ink">Loading.</p>
            ) : threads.length === 0 ? (
              <p className="text-sm text-ink">Nothing in this view.</p>
            ) : (
              <ul className="space-y-2">
                {threads.map((thread) => (
                  <li key={thread.id}>
                    <button
                      type="button"
                      onClick={() => void openDetail(thread.id)}
                      className={cn(
                        "w-full rounded-lg border p-3 text-left transition",
                        thread.id === selectedId
                          ? "border-navy-600 bg-navy-50"
                          : "border-navy-200 hover:border-navy-600",
                      )}
                    >
                      <span className="block truncate text-sm font-medium text-ink">
                        {thread.subject}
                      </span>
                      <span className="block truncate text-xs text-ink">
                        {thread.tenant_name}
                      </span>
                      <span className="mt-2 flex flex-wrap items-center gap-2">
                        <SupportStatusBadge status={thread.status} staff />
                        <span className="text-xs">
                          {formatSupportTime(thread.last_message_at)}
                        </span>
                      </span>
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </CardContent>
        </Card>

        <Card>
          {detail ? (
            <>
              <CardHeader>
                <div className="flex flex-wrap items-center justify-between gap-3">
                  <CardTitle>{detail.subject}</CardTitle>
                  <SupportStatusBadge status={detail.status} staff />
                </div>
                <CardDescription>
                  {detail.tenant_name}
                  {detail.assigned_to_name
                    ? `, answered by ${detail.assigned_to_name}`
                    : ", unclaimed"}
                </CardDescription>
              </CardHeader>
              <CardContent className="space-y-6">
                <SupportConversation
                  messages={detail.messages}
                  viewerSide="staff"
                />
                <form onSubmit={submitReply} className="space-y-3">
                  <Textarea
                    value={reply}
                    onChange={(event) => setReply(event.target.value)}
                    maxLength={MAX_BODY}
                    rows={6}
                    placeholder="Reply to the customer"
                  />
                  <div className="flex flex-wrap gap-2">
                    <Button type="submit" disabled={busy || !reply.trim()}>
                      {busy ? "Sending" : "Reply"}
                    </Button>
                    {detail.status === "resolved" ? (
                      <Button
                        type="button"
                        variant="outline"
                        disabled={busy}
                        onClick={() => void move("open")}
                      >
                        Reopen
                      </Button>
                    ) : (
                      <Button
                        type="button"
                        variant="outline"
                        disabled={busy}
                        onClick={() => void move("resolved")}
                      >
                        Mark resolved
                      </Button>
                    )}
                  </div>
                </form>
              </CardContent>
            </>
          ) : (
            <CardContent className="flex min-h-[18rem] flex-col items-center justify-center gap-3 text-center">
              <Inbox className="h-8 w-8 text-navy-600" aria-hidden="true" />
              <p className="text-sm text-ink">
                Choose a conversation to read and answer it.
              </p>
            </CardContent>
          )}
        </Card>
      </div>
    </div>
  );
}
