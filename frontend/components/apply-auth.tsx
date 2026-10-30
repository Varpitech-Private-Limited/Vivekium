"use client";

import * as React from "react";
import { Check, Eye, EyeOff } from "lucide-react";
import {
  createUserWithEmailAndPassword,
  signInWithEmailAndPassword,
  signInWithPopup,
  updateProfile,
} from "firebase/auth";

import { createCandidateGoogleProvider, firebaseAuth } from "@/lib/firebase";
import {
  exchangeFirebaseSession,
  friendlyAuthError,
  isContextsResponse,
  selectContext,
  type FirebaseExchangeResult,
} from "@/lib/firebase-session";
import { useAuth } from "@/lib/auth-context";
import { ForgotPassword } from "@/components/forgot-password";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

type Mode = "signin" | "register";
const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
const passwordRules = (password: string) => ({
  length: password.length >= 8,
  lower: /[a-z]/.test(password),
  upper: /[A-Z]/.test(password),
  number: /\d/.test(password),
});

export function ApplyAuth({ onAuthed }: { onAuthed: () => void }) {
  const { setSession } = useAuth();
  const [mode, setMode] = React.useState<Mode>("signin");
  const [name, setName] = React.useState("");
  const [email, setEmail] = React.useState("");
  const [password, setPassword] = React.useState("");
  const [showPassword, setShowPassword] = React.useState(false);
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const rules = passwordRules(password);
  const passwordValid = Object.values(rules).every(Boolean);
  const [resetOpen, setResetOpen] = React.useState(false);
  // The surface is the portal: signing in is a candidate sign-in, creating
  // an account a candidate registration.
  const purpose = mode === "register" ? "candidate_register" : "candidate_login";

  const handleExchange = React.useCallback(
    async (result: FirebaseExchangeResult) => {
      let session;
      if (isContextsResponse(result)) {
        const candidate = result.contexts.find(
          (context) => context.role === "candidate"
        );
        if (!candidate) {
          await firebaseAuth.signOut();
          throw new Error(
            "That account does not have a candidate profile. Use a different email."
          );
        }
        session = await selectContext(result.context_token, candidate.user_id);
      } else {
        session = result;
      }
      if (session.user.role !== "candidate") {
        await firebaseAuth.signOut();
        throw new Error(
          "That account is a company account. Use a candidate account to apply."
        );
      }
      setSession(session.user, session.capabilities ?? []);
      onAuthed();
    },
    [onAuthed, setSession]
  );

  const run = (action: () => Promise<void>) => {
    if (busy) return;
    setBusy(true);
    setError(null);
    void action()
      .catch((authError) => {
        const direct =
          authError instanceof Error &&
          authError.message.startsWith("That account")
            ? authError.message
            : friendlyAuthError(authError);
        if (direct) setError(direct);
      })
      .finally(() => setBusy(false));
  };

  const google = () => {
    // The popup must open on the click itself or the browser blocks it.
    run(async () => {
      const credential = await signInWithPopup(
        firebaseAuth,
        createCandidateGoogleProvider()
      );
      await handleExchange(
        await exchangeFirebaseSession(credential.user, purpose)
      );
    });
  };

  const submit = (event: React.FormEvent) => {
    event.preventDefault();
    if (!EMAIL_RE.test(email.trim())) {
      setError("Enter a valid email address.");
      return;
    }
    if (mode === "register" && (!name.trim() || !passwordValid)) {
      setError(
        "Enter your name and use 8+ characters with uppercase, lowercase, and a number."
      );
      return;
    }
    run(async () => {
      const credential =
        mode === "register"
          ? await createUserWithEmailAndPassword(
              firebaseAuth,
              email.trim(),
              password
            )
          : await signInWithEmailAndPassword(
              firebaseAuth,
              email.trim(),
              password
            );
      if (mode === "register") {
        await updateProfile(credential.user, { displayName: name.trim() });
      }
      await handleExchange(
        await exchangeFirebaseSession(credential.user, purpose)
      );
    });
  };

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-2" role="group" aria-label="Account action">
        <Button
          type="button"
          variant={mode === "signin" ? "default" : "outline"}
          aria-pressed={mode === "signin"}
          onClick={() => setMode("signin")}
        >
          Sign in
        </Button>
        <Button
          type="button"
          variant={mode === "register" ? "default" : "outline"}
          aria-pressed={mode === "register"}
          onClick={() => setMode("register")}
        >
          Create account
        </Button>
      </div>
      {!resetOpen ? (
      <>
      <Button
        type="button"
        variant="outline"
        className="w-full"
        disabled={busy}
        onClick={google}
      >
        Continue with Google
      </Button>
      <div className="flex items-center gap-3">
        <span className="h-px flex-1 bg-border" />
        <span className="text-xs">or</span>
        <span className="h-px flex-1 bg-border" />
      </div>
      <form className="space-y-3" onSubmit={submit}>
        {mode === "register" ? (
          <div className="space-y-1">
            <Label htmlFor="apply-name">Full name</Label>
            <Input
              id="apply-name"
              autoComplete="name"
              value={name}
              disabled={busy}
              onChange={(event) => setName(event.target.value)}
              required
            />
          </div>
        ) : null}
        <div className="space-y-1">
          <Label htmlFor="apply-email">Email address</Label>
          <Input
            id="apply-email"
            type="email"
            autoComplete="email"
            value={email}
            disabled={busy}
            onChange={(event) => setEmail(event.target.value)}
            required
          />
        </div>
        <div className="space-y-1">
          <Label htmlFor="apply-password">Password</Label>
          <div className="relative">
            <Input
              id="apply-password"
              type={showPassword ? "text" : "password"}
              autoComplete={
                mode === "register" ? "new-password" : "current-password"
              }
              value={password}
              disabled={busy}
              className="pr-10"
              onChange={(event) => setPassword(event.target.value)}
              required
            />
            <button
              type="button"
              className="absolute inset-y-0 right-0 flex w-10 items-center justify-center"
              onClick={() => setShowPassword((visible) => !visible)}
              aria-label={showPassword ? "Hide password" : "Show password"}
            >
              {showPassword ? (
                <EyeOff className="h-4 w-4" />
              ) : (
                <Eye className="h-4 w-4" />
              )}
            </button>
          </div>
          {mode === "register" ? (
            <ul className="grid grid-cols-2 gap-1 pt-1 text-xs">
              <Rule ok={rules.length}>8+ characters</Rule>
              <Rule ok={rules.upper}>Uppercase</Rule>
              <Rule ok={rules.lower}>Lowercase</Rule>
              <Rule ok={rules.number}>Number</Rule>
            </ul>
          ) : null}
        </div>
        <Button
          className="w-full"
          disabled={busy || (mode === "register" && !passwordValid)}
        >
          {busy
            ? "Please wait"
            : mode === "register"
              ? "Create account and continue"
              : "Sign in and continue"}
        </Button>
      </form>
      </>
      ) : null}
      {mode === "signin" ? (
        <ForgotPassword
          initialEmail={email}
          idPrefix="apply"
          onOpenChange={(open) => {
            setResetOpen(open);
            setError(null);
          }}
        />
      ) : null}
      {error ? (
        <p role="alert" className="text-sm text-destructive">
          {error}
        </p>
      ) : null}
    </div>
  );
}

function Rule({ ok, children }: { ok: boolean; children: React.ReactNode }) {
  return (
    <li className="flex items-center gap-1">
      <Check className={`h-3 w-3 ${ok ? "opacity-100" : "opacity-30"}`} />
      {children}
    </li>
  );
}
