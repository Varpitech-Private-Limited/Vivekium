"use client";

// "Forgot password?", run by ReadyPick's server (auth spec 9.2).
//
// THREE STEPS, EACH PROVEN BY THE ONE BEFORE. The address and a security check
// ask the server to email a six-digit security code; the code is exchanged for
// a single-use reset ticket; the ticket and a new password set the password,
// and every session on the account is signed out. Firebase still stores the
// credential, but the browser no longer asks Firebase for a reset email: that
// path went around the security check and the security code entirely.
//
// THE ANSWER IS THE SAME WHETHER OR NOT THE ACCOUNT EXISTS. The server says one
// sentence for every address, so this screen cannot be used to test which
// addresses are registered. Only an answer the person can act on (a malformed
// address, a wrong code, too many attempts) differs, and nothing fails
// silently.

import * as React from "react";
import { Loader2, MailCheck } from "lucide-react";

import { apiPost } from "@/lib/api";
import { friendlyAuthError } from "@/lib/firebase-session";
import { InlineError } from "@/components/page-primitives";
import {
  PasswordRules,
  isPasswordValid,
  passwordRules,
} from "@/components/password-rules";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

/** The server's one sentence after a request, whatever the account state. */
export const RESET_SENT_MESSAGE =
  "If an account uses this address, we have sent a security code to it. Check your inbox and your spam folder.";

type Step = "request" | "code" | "password" | "done";

function onEnter(action: () => void) {
  return (event: React.KeyboardEvent) => {
    if (event.key === "Enter") {
      event.preventDefault();
      action();
    }
  };
}

/**
 * An inline "Forgot password?" control. Collapsed it is one link-styled
 * button; opened it walks the three steps in place. `onOpenChange` lets the
 * sign-in screen hide its own form (and its own security check) while this
 * one is open, so a person is never asked two security checks at once.
 */
export function ForgotPassword({
  initialEmail = "",
  idPrefix,
  onOpenChange,
}: {
  initialEmail?: string;
  /** Distinguishes the fields when two sign-in forms share a page. */
  idPrefix: string;
  onOpenChange?: (open: boolean) => void;
}) {
  const [open, setOpen] = React.useState(false);
  const [step, setStep] = React.useState<Step>("request");
  const [email, setEmail] = React.useState(initialEmail);
  const [code, setCode] = React.useState("");
  const [ticket, setTicket] = React.useState("");
  const [password, setPassword] = React.useState("");
  const [notice, setNotice] = React.useState<string | null>(null);
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const rules = passwordRules(password);

  const toggle = (next: boolean) => {
    setOpen(next);
    onOpenChange?.(next);
    if (next) {
      setEmail(initialEmail);
      setStep("request");
      setCode("");
      setTicket("");
      setPassword("");
      setNotice(null);
      setError(null);
    }
  };

  const run = (action: () => Promise<void>) => {
    if (busy) return;
    setBusy(true);
    setError(null);
    void action()
      .catch((failure) => setError(friendlyAuthError(failure) ?? "Please try again."))
      .finally(() => setBusy(false));
  };

  if (!open) {
    return (
      <button
        type="button"
        className="text-sm font-medium underline underline-offset-4 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        onClick={() => toggle(true)}
      >
        Forgot password?
      </button>
    );
  }

  const requestCode = () => {
    const address = email.trim();
    if (!EMAIL_RE.test(address)) {
      setError("Enter the email address you sign in with.");
      return;
    }
    run(async () => {
      const sent = await apiPost<{ message: string }>("/auth/password-reset/request", {
        email: address,
      });
      setNotice(sent.message || RESET_SENT_MESSAGE);
      setStep("code");
    });
  };

  const verifyCode = () => {
    if (!/^\d{6}$/.test(code.trim())) {
      setError("Enter the six-digit security code from the email.");
      return;
    }
    run(async () => {
      const verified = await apiPost<{ reset_token: string }>(
        "/auth/password-reset/verify",
        { email: email.trim(), code: code.trim() }
      );
      setTicket(verified.reset_token);
      setNotice(null);
      setStep("password");
    });
  };

  const setNewPassword = () => {
    if (!isPasswordValid(rules)) {
      setError("Use at least 8 characters with uppercase, lowercase, and a number.");
      return;
    }
    run(async () => {
      const done = await apiPost<{ message: string }>("/auth/password-reset/complete", {
        reset_token: ticket,
        password,
      });
      setPassword("");
      setNotice(done.message);
      setStep("done");
    });
  };

  return (
    <div className="space-y-3 rounded-xl border border-border p-4" role="group" aria-label="Reset your password">
      <p className="text-sm font-semibold">Reset your password</p>
      {notice ? (
        <p role="status" className="flex items-start gap-2 text-sm">
          <MailCheck className="mt-0.5 h-4 w-4 shrink-0 text-teal-700" aria-hidden="true" />
          {notice}
        </p>
      ) : null}

      {step === "request" ? (
        <div className="space-y-3">
          <div className="space-y-1.5">
            <Label htmlFor={`${idPrefix}-reset-email`}>Email address</Label>
            <Input
              id={`${idPrefix}-reset-email`}
              type="email"
              autoComplete="email"
              value={email}
              disabled={busy}
              onChange={(event) => setEmail(event.target.value)}
              onKeyDown={onEnter(requestCode)}
            />
          </div>
          <Button type="button" size="sm" disabled={busy} onClick={requestCode}>
            {busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" /> : null}
            Email me a security code
          </Button>
        </div>
      ) : null}

      {step === "code" ? (
        <div className="space-y-3">
          <div className="space-y-1.5">
            <Label htmlFor={`${idPrefix}-reset-code`}>Security code</Label>
            <Input
              id={`${idPrefix}-reset-code`}
              inputMode="numeric"
              autoComplete="one-time-code"
              maxLength={6}
              value={code}
              disabled={busy}
              onChange={(event) => setCode(event.target.value.replace(/\D/g, ""))}
              onKeyDown={onEnter(verifyCode)}
            />
          </div>
          <div className="flex flex-wrap items-center gap-3">
            <Button type="button" size="sm" disabled={busy} onClick={verifyCode}>
              {busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" /> : null}
              Verify code
            </Button>
            <button
              type="button"
              className="text-sm font-medium underline underline-offset-4"
              onClick={() => {
                setCode("");
                setNotice(null);
                setError(null);
                setStep("request");
              }}
            >
              Send a new code
            </button>
          </div>
        </div>
      ) : null}

      {step === "password" ? (
        <div className="space-y-3">
          <div className="space-y-1.5">
            <Label htmlFor={`${idPrefix}-reset-password`}>New password</Label>
            <Input
              id={`${idPrefix}-reset-password`}
              type="password"
              autoComplete="new-password"
              value={password}
              disabled={busy}
              aria-describedby={`${idPrefix}-reset-rules`}
              onChange={(event) => setPassword(event.target.value)}
              onKeyDown={onEnter(setNewPassword)}
            />
            <PasswordRules id={`${idPrefix}-reset-rules`} rules={rules} />
          </div>
          <Button type="button" size="sm" disabled={busy || !isPasswordValid(rules)} onClick={setNewPassword}>
            {busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" /> : null}
            Set new password
          </Button>
        </div>
      ) : null}

      {error ? <InlineError>{error}</InlineError> : null}
      <button
        type="button"
        className="text-sm font-medium underline underline-offset-4 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        onClick={() => toggle(false)}
      >
        Back to sign in
      </button>
    </div>
  );
}
