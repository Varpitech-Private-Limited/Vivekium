"use client";

// Joining a company team from an invitation (auth spec 2.5 and 11.4).
//
// THE SERVER SETS THE PASSWORD, FOR EXACTLY THE INVITED EMAIL. Creating the
// account posts the password and a security check proof to
// `/companies/invites/{token}/setup-password`; the address is the invitation's
// and is never sent from here, so an invitation cannot be spent on another
// mailbox. The server creates the sign-in, accepts the invitation and opens the
// company session in one step.
//
// AN ADDRESS THAT ALREADY HAS A SIGN-IN signs in with its existing password
// instead: Firebase proves the password, the exchange (purpose `invite_join`)
// opens the company workspace, and the invitation is accepted.
//
// THERE IS NO GOOGLE HERE. Every company role signs in with an email and
// password, and the server refuses Google for one whatever this page shows.

import * as React from "react";
import { Check, Eye, EyeOff, Loader2, MailCheck } from "lucide-react";
import { signInWithEmailAndPassword } from "firebase/auth";
import { useRouter } from "next/navigation";

import { cn } from "@/lib/utils";
import { ApiError, apiGet, apiPost } from "@/lib/api";
import { demoModeEnabled } from "@/lib/demo-mode";
import { firebaseAuth } from "@/lib/firebase";
import {
  exchangeFirebaseSession,
  friendlyAuthError,
  isContextsResponse,
  selectContext,
} from "@/lib/firebase-session";
import { useAuth } from "@/lib/auth-context";
import { apiErrorMessage } from "@/lib/validation-errors";
import type { AuthSession, StaffRole } from "@/lib/types";
import { AuthShell } from "@/components/auth-shell";
import { InlineError, LoadingRows } from "@/components/page-primitives";
import {
  PasswordRules,
  isPasswordValid,
  passwordRules,
} from "@/components/password-rules";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

type InviteInfo = {
  email: string;
  full_name?: string | null;
  role: StaffRole;
  company_name: string;
  invited_by_name?: string | null;
  expires_at: string;
  status: "pending";
};

const ROLE_LABELS: Partial<Record<string, string>> = {
  recruitment_manager: "Recruitment Manager",
  hr_manager: "HR Manager",
  recruiter: "Recruiter",
  hiring_manager: "Hiring Manager",
  interview_manager: "Interview Manager",
  ceo: "CEO",
  md: "MD",
  functional_head: "Functional Head",
};

function roleLabel(role: string): string {
  return ROLE_LABELS[role] ?? "team member";
}

export function JoinFlow({ token }: { token: string }) {
  const router = useRouter();
  const { setSession } = useAuth();
  const [invite, setInvite] = React.useState<InviteInfo | null>(null);
  const [loading, setLoading] = React.useState(true);
  const [loadError, setLoadError] = React.useState<string | null>(null);
  const [mode, setMode] = React.useState<"create" | "signin">("create");
  const [name, setName] = React.useState("");
  const [password, setPassword] = React.useState("");
  const [showPassword, setShowPassword] = React.useState(false);
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const [accepted, setAccepted] = React.useState(false);
  const rules = passwordRules(password);
  const passwordValid = isPasswordValid(rules);
  const tokenPath = `/companies/invites/${encodeURIComponent(token)}`;

  React.useEffect(() => {
    if (!token) {
      setLoadError("This invitation link is incomplete.");
      setLoading(false);
      return;
    }
    apiGet<InviteInfo>(tokenPath)
      .then((result) => {
        setInvite(result);
        setName(result.full_name ?? "");
      })
      .catch((requestError) =>
        setLoadError(
          requestError instanceof ApiError
            ? apiErrorMessage(requestError)
            : "This invitation is invalid or no longer available."
        )
      )
      .finally(() => setLoading(false));
  }, [token, tokenPath]);

  const joined = React.useCallback(
    (session: AuthSession) => {
      setSession(session.user, session.capabilities ?? []);
      setAccepted(true);
    },
    [setSession]
  );

  const createAccount = async () => {
    try {
      joined(
        await apiPost<AuthSession>(`/companies/invites/${encodeURIComponent(token)}/setup-password`, {
          password,
          full_name: name.trim() || null,
        })
      );
    } catch (failure) {
      if (failure instanceof ApiError && failure.status === 409) {
        // The address already has a sign-in: offer it, keep the sentence.
        setMode("signin");
        setPassword("");
      }
      throw failure;
    }
  };

  const signInExisting = async () => {
    if (!invite) return;
    const credential = await signInWithEmailAndPassword(
      firebaseAuth,
      invite.email,
      password
    );
    const result = await exchangeFirebaseSession(credential.user, "invite_join");
    let session: AuthSession;
    if (isContextsResponse(result)) {
      const target = result.contexts.find(
        (context) =>
          context.tenant_name === invite.company_name &&
          context.role === invite.role
      );
      if (!target) {
        throw new Error(
          "Your account was verified, but this company workspace was not available. Ask the company admin to resend the invitation."
        );
      }
      session = await selectContext(result.context_token, target.user_id);
    } else {
      session = result;
    }
    await apiPost(`/companies/invites/${encodeURIComponent(token)}/accept`);
    joined(session);
  };

  const submit = (event: React.FormEvent) => {
    event.preventDefault();
    if (!invite || busy) return;
    if (mode === "create" && (!name.trim() || !passwordValid)) {
      setError(
        "Enter your name and use at least 8 characters with uppercase, lowercase, and a number."
      );
      return;
    }
    if (mode === "signin" && !password) {
      setError("Enter your password.");
      return;
    }
    setBusy(true);
    setError(null);
    void (async () => {
      if (demoModeEnabled() && mode === "create") {
        try {
          joined(
            await apiPost<AuthSession>(
              `/companies/invites/${encodeURIComponent(token)}/setup-password`,
              {
                password,
                full_name: name.trim() || null,
              }
            )
          );
          return;
        } catch (failure) {
          if (
            failure instanceof ApiError &&
            (failure.status === 404 || failure.status === 410)
          ) {
            setLoadError("This invitation is invalid or no longer available.");
            return;
          }
          if (
            failure instanceof ApiError &&
            (failure.status === 400 || failure.status === 403)
          ) {
            await signInExisting();
            return;
          }
          if (failure instanceof ApiError && failure.status === 409) {
            setMode("signin");
            setPassword("");
          }
          throw failure;
        }
      }
      await (mode === "create" ? createAccount() : signInExisting());
    })()
      .catch((failure) => {
        const direct =
          failure instanceof Error &&
          failure.message.startsWith("Your account")
            ? failure.message
            : friendlyAuthError(failure);
        if (direct) setError(direct);
      })
      .finally(() => setBusy(false));
  };

  if (loading) {
    return (
      <AuthShell title="Checking your invitation">
        <LoadingRows rows={3} label="Checking invitation" />
      </AuthShell>
    );
  }

  if (loadError || !invite) {
    return (
      <AuthShell
        title="Invitation unavailable"
        description={
          loadError ?? "Ask your company admin for a fresh invitation."
        }
      >
        <Button asChild size="lg" className="w-full">
          <a href="/company/login">Go to company sign in</a>
        </Button>
      </AuthShell>
    );
  }

  if (accepted) {
    return (
      <AuthShell
        title={`You joined ${invite.company_name}`}
        description={`Your ${roleLabel(invite.role)} workspace is ready.`}
      >
        <div className="flex justify-center">
          <span className="grid h-14 w-14 place-items-center rounded-2xl bg-rating-1-bg text-rating-1">
            <Check className="h-7 w-7" strokeWidth={2.5} aria-hidden="true" />
          </span>
        </div>
        <Button size="lg" className="w-full" onClick={() => router.replace("/org")}>
          Open company workspace
        </Button>
      </AuthShell>
    );
  }

  return (
    <AuthShell
      title={`Join ${invite.company_name}`}
      description={
        <>
          {invite.invited_by_name
            ? `${invite.invited_by_name} invited you`
            : "You were invited"}{" "}
          as {roleLabel(invite.role)}.
        </>
      }
    >
      <div className="flex items-start gap-3 rounded-xl border border-border bg-brand-100/50 px-4 py-3">
        <MailCheck
          className="mt-0.5 h-4 w-4 shrink-0 text-brand-600"
          aria-hidden="true"
        />
        <div className="min-w-0">
          <p className="truncate text-sm font-semibold">{invite.email}</p>
          <p className="mt-0.5 text-xs">
            Only this address can join with this invitation. Expires{" "}
            {new Date(invite.expires_at).toLocaleDateString()}.
          </p>
        </div>
      </div>

      <div
        role="tablist"
        aria-label="How to join"
        className="grid grid-cols-2 gap-1 rounded-xl border border-border bg-secondary p-1"
      >
        {(["create", "signin"] as const).map((value) => (
          <button
            key={value}
            role="tab"
            type="button"
            aria-selected={mode === value}
            onClick={() => {
              setMode(value);
              setError(null);
            }}
            className={cn(
              "rounded-lg px-3 py-2 text-sm transition-colors duration-150 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
              mode === value
                ? "bg-surface font-semibold shadow-card"
                : "font-medium hover:bg-brand-100/60"
            )}
          >
            {value === "create" ? "Create password" : "I already have one"}
          </button>
        ))}
      </div>

      <form className="space-y-4" onSubmit={submit}>
        {mode === "create" ? (
          <div className="space-y-1.5">
            <Label htmlFor="join-name">Full name</Label>
            <Input
              id="join-name"
              autoComplete="name"
              value={name}
              disabled={busy}
              onChange={(event) => setName(event.target.value)}
              required
            />
          </div>
        ) : null}
        <div className="space-y-1.5">
          <Label htmlFor="join-email">Email address</Label>
          <Input
            id="join-email"
            type="email"
            autoComplete="username"
            value={invite.email}
            readOnly
            aria-readonly="true"
          />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="join-password">
            {mode === "create" ? "Create a password" : "Your password"}
          </Label>
          <div className="relative">
            <Input
              id="join-password"
              type={showPassword ? "text" : "password"}
              autoComplete={
                mode === "create" ? "new-password" : "current-password"
              }
              value={password}
              disabled={busy}
              className="pr-11"
              onChange={(event) => setPassword(event.target.value)}
              required
            />
            <button
              type="button"
              className="absolute inset-y-0 right-0 flex w-11 items-center justify-center rounded-r-lg focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              onClick={() => setShowPassword((visible) => !visible)}
              aria-label={showPassword ? "Hide password" : "Show password"}
            >
              {showPassword ? (
                <EyeOff className="h-4 w-4" aria-hidden="true" />
              ) : (
                <Eye className="h-4 w-4" aria-hidden="true" />
              )}
            </button>
          </div>
          {mode === "create" ? <PasswordRules rules={rules} /> : null}
        </div>
        <Button
          size="lg"
          className="w-full"
          disabled={busy || (mode === "create" && !passwordValid)}
        >
          {busy ? (
            <>
              <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
              Joining
            </>
          ) : mode === "create" ? (
            "Create password and join"
          ) : (
            "Sign in and join"
          )}
        </Button>
      </form>

      {error ? <InlineError>{error}</InlineError> : null}
    </AuthShell>
  );
}
