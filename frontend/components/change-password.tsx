"use client";

// Changing the password of the signed-in account (auth spec 9.1).
//
// THE SERVER CHANGES IT. Settings, then a security check, then a six-digit
// security code emailed to the account's own address, then the new password.
// The server sets it in Firebase, signs out every OTHER session of the account
// (in Firebase and here) and gives this browser a fresh session, so the person
// who changed it stays signed in on this device and nowhere else. The browser
// no longer updates the credential itself and no longer has to make a second
// call to revoke sessions it could simply fail to make.
//
// Visibility rule (client decision, 2026-07-27): the card renders ONLY for an
// account that signs in with a password. A Google-only account has no
// password to change, so offering the form would be a dead end.

import * as React from "react";
import { Eye, EyeOff, MailCheck } from "lucide-react";

import { apiPost } from "@/lib/api";
import { firebaseAuth } from "@/lib/firebase";
import { friendlyAuthError } from "@/lib/firebase-session";
import { useAuth } from "@/lib/auth-context";
import {
  PasswordRules,
  isPasswordValid,
  passwordRules,
} from "@/components/password-rules";
import { useToast } from "@/components/ui/toast";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { FormField } from "@/components/ui/form";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";

type Step = "start" | "code" | "password";

/** The confirmation must repeat the new password exactly. */
export function validateNewPassword(next: string, confirm: string): string | null {
  if (!isPasswordValid(passwordRules(next))) {
    return "Use at least 8 characters with uppercase, lowercase, and a number.";
  }
  if (next !== confirm) return "The two passwords don't match.";
  return null;
}

function PasswordInput({
  id,
  value,
  disabled,
  autoComplete,
  describedBy,
  onChange,
}: {
  id: string;
  value: string;
  disabled?: boolean;
  autoComplete: string;
  describedBy?: string;
  onChange: (value: string) => void;
}) {
  const [visible, setVisible] = React.useState(false);
  return (
    <div className="relative">
      <Input
        id={id}
        type={visible ? "text" : "password"}
        autoComplete={autoComplete}
        value={value}
        disabled={disabled}
        className="pr-10"
        aria-describedby={describedBy}
        onChange={(event) => onChange(event.target.value)}
      />
      <button
        type="button"
        className="absolute inset-y-0 right-0 flex w-10 items-center justify-center"
        onClick={() => setVisible((shown) => !shown)}
        aria-label={visible ? "Hide password" : "Show password"}
      >
        {visible ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
      </button>
    </div>
  );
}

export function ChangePasswordCard() {
  const { toast } = useToast();
  const { user } = useAuth();
  const [step, setStep] = React.useState<Step>("start");
  const [code, setCode] = React.useState("");
  const [ticket, setTicket] = React.useState("");
  const [next, setNext] = React.useState("");
  const [confirm, setConfirm] = React.useState("");
  const [notice, setNotice] = React.useState<string | null>(null);
  const [saving, setSaving] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);

  if (!user?.password_enabled) return null;

  const run = (action: () => Promise<void>) => {
    if (saving) return;
    setSaving(true);
    setError(null);
    void action()
      .catch((failure) => {
        const message = friendlyAuthError(failure) ?? "We couldn't change your password. Please try again.";
        setError(message);
      })
      .finally(() => setSaving(false));
  };

  const sendCode = (event: React.FormEvent) => {
    event.preventDefault();
    run(async () => {
      const sent = await apiPost<{ message: string }>("/auth/password-change/request", {});
      setNotice(sent.message);
      setCode("");
      setStep("code");
    });
  };

  const verifyCode = (event: React.FormEvent) => {
    event.preventDefault();
    if (!/^\d{6}$/.test(code.trim())) {
      setError("Enter the six-digit security code from the email.");
      return;
    }
    run(async () => {
      const verified = await apiPost<{ change_token: string }>(
        "/auth/password-change/verify",
        { code: code.trim() }
      );
      setTicket(verified.change_token);
      setNotice(null);
      setStep("password");
    });
  };

  const savePassword = (event: React.FormEvent) => {
    event.preventDefault();
    const invalid = validateNewPassword(next, confirm);
    if (invalid) {
      setError(invalid);
      return;
    }
    run(async () => {
      const done = await apiPost<{ message: string }>("/auth/password-change/complete", {
        change_token: ticket,
        password: next,
      });
      // Firebase's own tokens for this identity were revoked by the server, so
      // the browser's Firebase sign-in is stale; the ReadyPick session is fresh.
      try {
        await firebaseAuth.signOut();
      } catch {
        /* already signed out of Firebase */
      }
      setNext("");
      setConfirm("");
      setTicket("");
      setStep("start");
      toast({ title: done.message });
    });
  };

  const rules = passwordRules(next);

  return (
    <Card>
      <CardHeader>
        <CardTitle>Password</CardTitle>
        <CardDescription>
          Change the password you use to sign in. We email a security code to{" "}
          {user.email ?? "your address"} first.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        {notice ? (
          <p role="status" className="flex items-start gap-2 text-sm">
            <MailCheck className="mt-0.5 h-4 w-4 shrink-0 text-teal-700" aria-hidden="true" />
            {notice}
          </p>
        ) : null}

        {step === "start" ? (
          <form className="space-y-4" onSubmit={sendCode} noValidate>
            <Button type="submit" disabled={saving}>
              {saving ? "Sending" : "Email me a security code"}
            </Button>
          </form>
        ) : null}

        {step === "code" ? (
          <form className="space-y-4" onSubmit={verifyCode} noValidate>
            <FormField label="Security code" htmlFor="password-code" required>
              <Input
                id="password-code"
                inputMode="numeric"
                autoComplete="one-time-code"
                maxLength={6}
                value={code}
                disabled={saving}
                onChange={(event) => setCode(event.target.value.replace(/\D/g, ""))}
              />
            </FormField>
            <div className="flex flex-wrap items-center gap-3">
              <Button type="submit" disabled={saving || code.length !== 6}>
                {saving ? "Checking" : "Verify code"}
              </Button>
              <Button
                type="button"
                variant="ghost"
                disabled={saving}
                onClick={() => {
                  setStep("start");
                  setNotice(null);
                  setError(null);
                }}
              >
                Send a new code
              </Button>
            </div>
          </form>
        ) : null}

        {step === "password" ? (
          <form className="space-y-4" onSubmit={savePassword} noValidate>
            <FormField label="New password" htmlFor="password-new" required>
              <PasswordInput
                id="password-new"
                value={next}
                disabled={saving}
                autoComplete="new-password"
                describedBy="password-new-rules"
                onChange={setNext}
              />
            </FormField>
            <PasswordRules id="password-new-rules" rules={rules} />
            <FormField label="Confirm new password" htmlFor="password-confirm" required>
              <PasswordInput
                id="password-confirm"
                value={confirm}
                disabled={saving}
                autoComplete="new-password"
                onChange={setConfirm}
              />
            </FormField>
            <Button type="submit" disabled={saving || !next || !confirm}>
              {saving ? "Saving" : "Change password"}
            </Button>
          </form>
        ) : null}

        {error ? (
          <p role="alert" className="text-sm font-medium text-destructive">
            {error}
          </p>
        ) : null}
      </CardContent>
    </Card>
  );
}
