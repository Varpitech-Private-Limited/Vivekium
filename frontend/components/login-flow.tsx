"use client";

import * as React from "react";
import { Eye, EyeOff, Loader2 } from "lucide-react";
import { signInWithEmailAndPassword, signInWithPopup } from "firebase/auth";
import { useRouter, useSearchParams } from "next/navigation";

import { ApiError, apiPost, isNetworkError } from "@/lib/api";
import { demoModeEnabled } from "@/lib/demo-mode";
import { createCandidateGoogleProvider, firebaseAuth } from "@/lib/firebase";
import {
  exchangeFirebaseSession,
  friendlyAuthError,
  isContextsResponse,
  ROLE_LABEL,
  selectContext,
  type ExchangePurpose,
  type FirebaseExchangeResult,
} from "@/lib/firebase-session";
import { homePathForRole, useAuth } from "@/lib/auth-context";
import { currentNextPath, withNext } from "@/lib/next-destination";
import type { AuthContextsResponse, AuthSession } from "@/lib/types";
import { apiErrorMessage } from "@/lib/validation-errors";
import { AuthDivider, AuthLink, AuthShell } from "@/components/auth-shell";
import { InlineError } from "@/components/page-primitives";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { GoogleMark } from "@/components/google-mark";
import { ForgotPassword } from "@/components/forgot-password";

/**
 * Which sign-in page this is. The surface is the portal: a candidate page
 * opens the candidate workspace, the company page a company workspace, and the
 * Provider and business development pages their consoles.
 *
 * Google is offered to candidates, the Provider and business development.
 * NEVER on the company page: every company role signs in with an email and
 * password, and the server refuses a Google sign-in for one even when a caller
 * skips this screen (auth spec 2.6 and 8).
 */
export type SignInSurface = "candidate" | "company" | "provider" | "bd";

const PURPOSE: Record<SignInSurface, ExchangePurpose> = {
  candidate: "candidate_login",
  company: "company_login",
  provider: "provider_login",
  bd: "bd_login",
};

const GOOGLE_ALLOWED: Record<SignInSurface, boolean> = {
  candidate: true,
  company: false,
  provider: true,
  bd: true,
};

const TITLES: Record<SignInSurface, string> = {
  candidate: "Sign in to Vivekium",
  company: "Sign in to your company workspace",
  provider: "Provider sign in",
  bd: "Business development sign in",
};

/**
 * `/login`: the candidate sign-in page, which also carries the Provider and
 * business development sign-ins behind `?portal=owner` and `?portal=bd`
 * (each asks its own security check, so the surface is chosen before the
 * check is drawn). `?portal=org` is the company page's sign-in; the company
 * page itself lives at /company/login. `?portal=candidate`, which invitation
 * links carry, is the candidate page.
 */
export function LoginPageFlow() {
  const portal = useSearchParams().get("portal");
  const surface: SignInSurface =
    portal === "owner"
      ? "provider"
      : portal === "bd"
        ? "bd"
        : portal === "org"
          ? "company"
          : "candidate";
  return <LoginFlow key={surface} surface={surface} title={TITLES[surface]} />;
}

export function LoginFlow({
  title,
  description,
  surface,
}: {
  title: string;
  description?: string;
  surface: SignInSurface;
}) {
  const router = useRouter();
  const { setSession } = useAuth();
  const purpose = PURPOSE[surface];
  const [email, setEmail] = React.useState("");
  const [password, setPassword] = React.useState("");
  const [showPassword, setShowPassword] = React.useState(false);
  const [contexts, setContexts] = React.useState<AuthContextsResponse | null>(
    null
  );
  const [resetOpen, setResetOpen] = React.useState(false);
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);

  const finish = React.useCallback(
    (session: AuthSession) => {
      setSession(session.user, session.capabilities ?? []);
      // Read at finish time rather than from a hook: the flow can rewrite the
      // URL between mount and completion. `currentNextPath` is the shared
      // same-origin guard (lib/next-destination).
      router.replace(currentNextPath() ?? homePathForRole(session.user.role));
    },
    [router, setSession]
  );

  const resolve = React.useCallback(
    (result: FirebaseExchangeResult) => {
      if (isContextsResponse(result)) {
        setContexts(result);
        return;
      }
      finish(result);
    },
    [finish]
  );

  const run = (action: () => Promise<void>) => {
    if (busy) return;
    setBusy(true);
    setError(null);
    void action()
      .catch((authError) => {
        const message = friendlyAuthError(authError);
        if (message) setError(message);
      })
      .finally(() => setBusy(false));
  };

  const passwordSignIn = (event: React.FormEvent) => {
    event.preventDefault();
    if (!email.trim() || !password) {
      setError("Enter your email and password.");
      return;
    }
    run(async () => {
      if (demoModeEnabled()) {
        try {
          resolve(
            await apiPost<FirebaseExchangeResult>("/auth/demo/session", {
              email: email.trim(),
              password,
              portal: surface,
            })
          );
          return;
        } catch (failure) {
          if (
            surface === "company" &&
            failure instanceof ApiError &&
            (failure.status === 403 || isNetworkError(failure))
          ) {
            setError(apiErrorMessage(failure));
            return;
          }
          const fallThrough =
            failure instanceof ApiError &&
            (failure.status === 409 ||
              failure.status === 404 ||
              (failure.status === 403 &&
                (surface === "provider" || surface === "bd")));
          if (!fallThrough) throw failure;
        }
      }
      const credential = await signInWithEmailAndPassword(
        firebaseAuth,
        email.trim(),
        password
      );
      resolve(await exchangeFirebaseSession(credential.user, purpose));
    });
  };

  const googleSignIn = () => {
    // The popup must open on the click itself or the browser blocks it.
    run(async () => {
      const credential = await signInWithPopup(
        firebaseAuth,
        createCandidateGoogleProvider()
      );
      resolve(await exchangeFirebaseSession(credential.user, purpose));
    });
  };

  /**
   * Finalize the workspace choice. A `context_token` is single use and short
   * lived; once it is spent or expired the person signs in again.
   */
  const chooseContext = (userId: string) =>
    run(async () => {
      if (!contexts) return;
      try {
        finish(await selectContext(contexts.context_token, userId));
      } catch (failure) {
        setContexts(null);
        throw failure;
      }
    });

  const footer = contexts ? null : surface === "candidate" ? (
    <div className="space-y-2">
      <p>
        Need an account?{" "}
        <AuthLink href={withNext("/register", currentNextPath())}>
          Create one
        </AuthLink>
      </p>
      <p>
        Part of a company hiring team?{" "}
        <AuthLink href="/company/login">Company login</AuthLink>
      </p>
      <p>
        Vivekium staff:{" "}
        <AuthLink href="/login?portal=owner">Provider</AuthLink>
        {" · "}
        <AuthLink href="/login?portal=bd">Business development</AuthLink>
      </p>
    </div>
  ) : surface === "company" ? (
    <div className="space-y-2">
      <p>
        New to Vivekium?{" "}
        <AuthLink href="/company/register">Register your company</AuthLink>
      </p>
      <p>
        Joining a team? Use the invitation email your company admin sent you.
      </p>
      <p>
        Looking for jobs? <AuthLink href="/login">Candidate sign in</AuthLink>
      </p>
    </div>
  ) : (
    <p>
      <AuthLink href="/login">Back to candidate sign in</AuthLink>
    </p>
  );

  return (
    <AuthShell
      title={contexts ? "Choose your workspace" : title}
      description={
        contexts
          ? "This email belongs to more than one Vivekium workspace."
          : description
      }
      footer={footer}
    >
      {contexts ? (
        <div className="space-y-3">
          {contexts.contexts.map((context) => (
            <button
              key={context.user_id}
              type="button"
              disabled={busy}
              onClick={() => chooseContext(context.user_id)}
              className="w-full rounded-xl border border-border bg-surface px-4 py-3 text-left shadow-card transition-colors duration-150 hover:border-brand-600/50 hover:bg-brand-100/60 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:pointer-events-none disabled:opacity-50"
            >
              <span className="block text-sm font-semibold">
                {context.tenant_name ?? "Vivekium"}
              </span>
              <span className="mt-0.5 block text-xs">
                {ROLE_LABEL[context.role] ?? context.role}
              </span>
            </button>
          ))}
          <Button
            variant="ghost"
            className="w-full"
            disabled={busy}
            onClick={() => setContexts(null)}
          >
            Use a different account
          </Button>
        </div>
      ) : (
        <div className="space-y-5">
          {!resetOpen ? (
            <>
              {GOOGLE_ALLOWED[surface] ? (
                <>
                  <Button
                    type="button"
                    variant="outline"
                    size="lg"
                    className="w-full"
                    disabled={busy}
                    onClick={googleSignIn}
                  >
                    <GoogleMark />
                    Continue with Google
                  </Button>
                  <AuthDivider />
                </>
              ) : null}

              <form className="space-y-4" onSubmit={passwordSignIn}>
                <div className="space-y-1.5">
                  <Label htmlFor="login-email">Email address</Label>
                  <Input
                    id="login-email"
                    type="email"
                    autoComplete="email"
                    placeholder="you@company.com"
                    value={email}
                    disabled={busy}
                    onChange={(event) => setEmail(event.target.value)}
                    required
                  />
                </div>
                <div className="space-y-1.5">
                  <Label htmlFor="login-password">Password</Label>
                  <div className="relative">
                    <Input
                      id="login-password"
                      type={showPassword ? "text" : "password"}
                      autoComplete="current-password"
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
                </div>
                <Button size="lg" className="w-full" disabled={busy}>
                  {busy ? (
                    <>
                      <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
                      Signing in
                    </>
                  ) : (
                    "Sign in"
                  )}
                </Button>
              </form>
            </>
          ) : null}
          <ForgotPassword
            initialEmail={email}
            idPrefix="login"
            onOpenChange={(open) => {
              setResetOpen(open);
              setError(null);
            }}
          />
        </div>
      )}

      {error ? <InlineError>{error}</InlineError> : null}
    </AuthShell>
  );
}
