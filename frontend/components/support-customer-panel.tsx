"use client";

// The customer's Support screen: their own conversations, and one open thread.
//
// A list and a detail in ONE component with one selection, rather than two
// routes. A support screen is somewhere you glance at three threads and answer
// the one that moved; a route change per thread would make that three page
// loads. The job detail page makes the same call for candidates, and this
// follows it rather than inventing a second convention.
//
// EVERYTHING THIS SCREEN CAN DO IS ALSO REFUSED SERVER-SIDE. The nav entry and
// the composer are gated on `open_support_threads`, and so is every route
// behind them. Hiding a control the server would refuse is courtesy; the
// refusal is the security.

import * as React from "react";
import { LifeBuoy, Plus } from "lucide-react";

import { apiGet, apiPost } from "@/lib/api";
import { apiErrorMessage } from "@/lib/validation-errors";
import type {
  SupportThread,
  SupportThreadDetail,
  SupportThreadPage,
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
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { useToast } from "@/components/ui/toast";
import {
  SupportConversation,
  SupportStatusBadge,
  formatSupportTime,
} from "@/components/support-thread-view";

// Mirrors `services/support.MAX_SUBJECT_CHARS` / `MAX_BODY_CHARS`. Stated here
// so the field stops accepting, rather than letting the server answer 422 after
// somebody has typed a long message they must now shorten by guesswork.
const MAX_SUBJECT = 200;
const MAX_BODY = 20_000;

export function SupportCustomerPanel() {
  const { toast } = useToast();
  const [threads, setThreads] = React.useState<SupportThread[] | null>(null);
  const [loadError, setLoadError] = React.useState<string | null>(null);
  const [selectedId, setSelectedId] = React.useState<string | null>(null);
  const [detail, setDetail] = React.useState<SupportThreadDetail | null>(null);
  const [composing, setComposing] = React.useState(false);
  const [subject, setSubject] = React.useState("");
  const [body, setBody] = React.useState("");
  const [reply, setReply] = React.useState("");
  const [busy, setBusy] = React.useState(false);

  const loadThreads = React.useCallback(async () => {
    try {
      const page = await apiGet<SupportThreadPage>("/support/threads");
      setThreads(page.items);
      setLoadError(null);
    } catch (error) {
      // Never an empty list on failure. "You have no conversations" and "we
      // could not load them" are different facts, and showing the first when
      // the second is true tells a customer their ticket has vanished.
      setThreads(null);
      setLoadError(apiErrorMessage(error));
    }
  }, []);

  React.useEffect(() => {
    void loadThreads();
  }, [loadThreads]);

  React.useEffect(() => {
    if (!selectedId) {
      setDetail(null);
      return;
    }
    let active = true;
    apiGet<SupportThreadDetail>(`/support/threads/${selectedId}`)
      .then((result) => {
        if (active) setDetail(result);
      })
      .catch((error) => {
        if (active) toast({ title: apiErrorMessage(error), variant: "destructive" });
      });
    return () => {
      active = false;
    };
  }, [selectedId, toast]);

  async function submitNew(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    try {
      const created = await apiPost<SupportThreadDetail>("/support/threads", {
        subject,
        body,
      });
      setSubject("");
      setBody("");
      setComposing(false);
      setDetail(created);
      setSelectedId(created.id);
      await loadThreads();
      toast({ title: "Sent. Vivekium has been notified." });
    } catch (error) {
      toast({ title: apiErrorMessage(error), variant: "destructive" });
    } finally {
      setBusy(false);
    }
  }

  async function submitReply(event: React.FormEvent) {
    event.preventDefault();
    if (!selectedId) return;
    setBusy(true);
    try {
      await apiPost(`/support/threads/${selectedId}/messages`, { body: reply });
      setReply("");
      // Re-read rather than appending locally: the server decides the thread's
      // new status, and a locally appended message would leave the badge
      // saying the opposite of what the queue believes.
      const refreshed = await apiGet<SupportThreadDetail>(
        `/support/threads/${selectedId}`,
      );
      setDetail(refreshed);
      await loadThreads();
    } catch (error) {
      toast({ title: apiErrorMessage(error), variant: "destructive" });
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="type-page-title text-ink">Support</h1>
          <p className="text-sm text-ink">
            Questions about your account, your billing, or anything that is not
            working. Vivekium replies here and by email.
          </p>
        </div>
        <Button
          onClick={() => {
            setComposing(true);
            setSelectedId(null);
          }}
        >
          <Plus className="mr-2 h-4 w-4" aria-hidden="true" />
          New conversation
        </Button>
      </div>

      <div className="grid gap-6 lg:grid-cols-[22rem_1fr]">
        <Card>
          <CardHeader>
            <CardTitle>Your conversations</CardTitle>
          </CardHeader>
          <CardContent className="space-y-2">
            {loadError ? (
              <div className="space-y-3">
                <p className="text-sm text-ink">{loadError}</p>
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => void loadThreads()}
                >
                  Try again
                </Button>
              </div>
            ) : threads === null ? (
              <p className="text-sm text-ink">Loading.</p>
            ) : threads.length === 0 ? (
              <p className="text-sm text-ink">
                You have not started a conversation yet.
              </p>
            ) : (
              <ul className="space-y-2">
                {threads.map((thread) => (
                  <li key={thread.id}>
                    <button
                      type="button"
                      onClick={() => {
                        setComposing(false);
                        setSelectedId(thread.id);
                      }}
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
                      <span className="mt-2 flex flex-wrap items-center gap-2">
                        <SupportStatusBadge status={thread.status} />
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
          {composing ? (
            <>
              <CardHeader>
                <CardTitle>New conversation</CardTitle>
                <CardDescription>
                  Tell us what is happening and what you expected instead. Please
                  do not include a candidate&rsquo;s personal details here.
                </CardDescription>
              </CardHeader>
              <CardContent>
                <form onSubmit={submitNew} className="space-y-4">
                  <Input
                    value={subject}
                    onChange={(event) => setSubject(event.target.value)}
                    maxLength={MAX_SUBJECT}
                    placeholder="Subject"
                    required
                  />
                  <Textarea
                    value={body}
                    onChange={(event) => setBody(event.target.value)}
                    maxLength={MAX_BODY}
                    rows={10}
                    placeholder="What is happening?"
                    required
                  />
                  <div className="flex gap-2">
                    <Button
                      type="submit"
                      disabled={busy || !subject.trim() || !body.trim()}
                    >
                      {busy ? "Sending" : "Send"}
                    </Button>
                    <Button
                      type="button"
                      variant="outline"
                      onClick={() => setComposing(false)}
                    >
                      Cancel
                    </Button>
                  </div>
                </form>
              </CardContent>
            </>
          ) : detail ? (
            <>
              <CardHeader>
                <div className="flex flex-wrap items-center justify-between gap-3">
                  <CardTitle>{detail.subject}</CardTitle>
                  <SupportStatusBadge status={detail.status} />
                </div>
                <CardDescription>
                  Started {formatSupportTime(detail.created_at)}
                </CardDescription>
              </CardHeader>
              <CardContent className="space-y-6">
                <SupportConversation
                  messages={detail.messages}
                  viewerSide="customer"
                />
                <form onSubmit={submitReply} className="space-y-3">
                  <Textarea
                    value={reply}
                    onChange={(event) => setReply(event.target.value)}
                    maxLength={MAX_BODY}
                    rows={5}
                    placeholder="Reply"
                  />
                  <Button type="submit" disabled={busy || !reply.trim()}>
                    {busy ? "Sending" : "Reply"}
                  </Button>
                  {detail.status === "resolved" ? (
                    <p className="text-xs text-ink">
                      This conversation is resolved. Replying reopens it.
                    </p>
                  ) : null}
                </form>
              </CardContent>
            </>
          ) : (
            <CardContent className="flex min-h-[18rem] flex-col items-center justify-center gap-3 text-center">
              <LifeBuoy className="h-8 w-8 text-navy-600" aria-hidden="true" />
              <p className="text-sm text-ink">
                Choose a conversation, or start a new one.
              </p>
            </CardContent>
          )}
        </Card>
      </div>
    </div>
  );
}
