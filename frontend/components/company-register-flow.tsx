"use client";

// Register your company (owner spec 2026-09-29, sections 2.2 and 5).
//
// Details and a security check -> a security code from the company mailbox ->
// the paid Starter pilot -> a password -> the company workspace. The SERVER
// decides every step: `GET /company-onboarding/state` says where this browser's
// registration stands (from a short-lived, httpOnly onboarding cookie the
// code step sets), so a reload lands on the right step and nothing here is a
// source of truth. The browser's payment callback proves nothing by itself:
// the server checks Razorpay's signature, and the password step is refused
// until a paid purchase exists.
//
// There is no Google here. The Company Super Admin signs in with an email and
// a password, like every company role.

import * as React from "react";
import Link from "next/link";
import { Eye, EyeOff, Loader2, MailCheck } from "lucide-react";
import { signInWithEmailAndPassword } from "firebase/auth";
import { useRouter } from "next/navigation";

import { ApiError, apiGet, apiPost } from "@/lib/api";
import { demoModeEnabled } from "@/lib/demo-mode";
import { apiErrorMessage } from "@/lib/validation-errors";
import { firebaseAuth } from "@/lib/firebase";
import { friendlyAuthError } from "@/lib/firebase-session";
import { useAuth } from "@/lib/auth-context";
import { openSubscriptionCheckout } from "@/lib/razorpay";
import type {
  AuthSession,
  OnboardingState,
} from "@/lib/types";
import { AuthLink, AuthShell } from "@/components/auth-shell";
import { formatInr } from "@/components/billing/credit-pack-picker";
import { INDUSTRIES } from "@/components/customer-edit-modal";
import { InlineError, LoadingRows } from "@/components/page-primitives";
import {
  PasswordRules,
  isPasswordValid,
  passwordRules,
} from "@/components/password-rules";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

const BASE = "/company-onboarding";

interface RegistrationPlan {
  slug: string;
  name: string;
  assessments: number;
  price_inr: number;
  gst_inr: number;
  total_inr: number;
  rollover_months: number;
}

type Details = {
  first_name: string;
  last_name: string;
  email: string;
  phone: string;
  company_name: string;
  industry: string;
  industry_other: string;
};

const EMPTY_DETAILS: Details = {
  first_name: "",
  last_name: "",
  email: "",
  phone: "",
  company_name: "",
  industry: "",
  industry_other: "",
};

/** The server's sentence when it gave one, else `fallback`. */
function sentence(error: unknown, fallback: string): string {
  if (error instanceof ApiError && error.message) return error.message;
  return fallback;
}

const SELECT_CLASS =
  "flex h-10 w-full rounded-lg border border-input bg-surface px-3 py-2 text-sm transition-[border-color,box-shadow] duration-150 hover:border-field-hover focus:border-brand-600 focus:outline-none focus:ring-2 focus:ring-ring/30 disabled:cursor-not-allowed disabled:opacity-50";

export function CompanyRegisterFlow() {
  const router = useRouter();
  const { setSession } = useAuth();
  const [state, setState] = React.useState<OnboardingState | null>(null);
  const [loadError, setLoadError] = React.useState<string | null>(null);
  const [error, setError] = React.useState<string | null>(null);
  const [notice, setNotice] = React.useState<string | null>(null);
  const [busy, setBusy] = React.useState(false);
  const [paymentPending, setPaymentPending] = React.useState(false);
  const [resuming, setResuming] = React.useState(false);
  const [details, setDetails] = React.useState<Details>(EMPTY_DETAILS);
  const [pendingEmail, setPendingEmail] = React.useState("");
  const [code, setCode] = React.useState("");
  const [plans, setPlans] = React.useState<RegistrationPlan[] | null>(null);
  const [password, setPassword] = React.useState("");
  const [showPassword, setShowPassword] = React.useState(false);
  const [existingSignIn, setExistingSignIn] = React.useState(false);
  const rules = passwordRules(password);

  const loadState = React.useCallback(async () => {
    setLoadError(null);
    try {
      setState(await apiGet<OnboardingState>(`${BASE}/state`));
    } catch (failure) {
      setLoadError(sentence(failure, "We could not load your registration. Refresh to try again."));
    }
  }, []);

  React.useEffect(() => {
    void loadState();
  }, [loadState]);

  React.useEffect(() => {
    if (!paymentPending || state?.stage !== "choose_pack") return;
    const timer = window.setInterval(() => {
      void apiGet<OnboardingState>(`${BASE}/state`).then((next) => {
        setState(next);
        if (next.stage === "set_password") setPaymentPending(false);
      }).catch(() => {
        setNotice("Your first charge is still being confirmed. Keep this page open or return later to continue.");
      });
    }, 5000);
    return () => window.clearInterval(timer);
  }, [paymentPending, state?.stage]);

  // The plan step reads the same server-owned prices the public page displays.
  React.useEffect(() => {
    if (state?.stage !== "choose_pack" || plans) return;
    apiGet<{ plans: RegistrationPlan[] }>(`${BASE}/monthly/plans`)
      .then((result) => setPlans(result.plans))
      .catch((failure) => {
        if (failure instanceof ApiError && failure.status === 401) {
          setState({ stage: "details" });
          setResuming(true);
        }
        setError(sentence(failure, "We could not load the monthly plans."));
      });
  }, [state?.stage, plans]);

  const run = (action: () => Promise<void>) => {
    if (busy) return;
    setBusy(true);
    setError(null);
    setNotice(null);
    void action()
      .catch((failure) => setError(sentence(failure, "That did not go through. Please try again.")))
      .finally(() => setBusy(false));
  };

  const field = (key: keyof Details) => ({
    value: details[key],
    disabled: busy,
    onChange: (event: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) =>
      setDetails((current) => ({ ...current, [key]: event.target.value })),
  });

  const finish = (session: AuthSession) => {
    setSession(session.user, session.capabilities ?? []);
    router.replace("/org");
  };

  const submitDetails = (event: React.FormEvent) => {
    event.preventDefault();
    const required: Array<keyof Details> = [
      "first_name", "last_name", "email", "phone", "company_name", "industry",
    ];
    if (required.some((key) => !details[key].trim())) {
      setError("Fill in every field to register your company.");
      return;
    }
    if (details.industry === "Other" && !details.industry_other.trim()) {
      setError("Tell us your industry when you choose Other.");
      return;
    }
    if (demoModeEnabled() && !password.trim()) {
      setError("Enter a password.");
      return;
    }
    run(async () => {
      if (demoModeEnabled()) {
        try {
          finish(
            await apiPost<AuthSession>(`${BASE}/demo`, {
              ...details,
              industry_other:
                details.industry === "Other" ? details.industry_other : null,
              password,
            })
          );
        } catch (failure) {
          if (failure instanceof ApiError && failure.status === 409) {
            setError(apiErrorMessage(failure));
            return;
          }
          throw failure;
        }
        return;
      }
      await apiPost(`${BASE}/register`, {
        ...details,
        industry_other: details.industry === "Other" ? details.industry_other : null,
      });
      setPendingEmail(details.email.trim());
      setState({ stage: "verify_email", email: details.email.trim() });
    });
  };

  const submitResume = (event: React.FormEvent) => {
    event.preventDefault();
    if (!pendingEmail.trim()) {
      setError("Enter the email address you registered with.");
      return;
    }
    run(async () => {
      await apiPost(`${BASE}/code/resend`, { email: pendingEmail.trim() });
      setResuming(false);
      setState({ stage: "verify_email", email: pendingEmail.trim() });
    });
  };

  const email = state?.email ?? pendingEmail;

  const submitCode = (event: React.FormEvent) => {
    event.preventDefault();
    if (!code.trim()) {
      setError("Enter the six-digit security code from the email.");
      return;
    }
    run(async () => {
      const next = await apiPost<OnboardingState>(`${BASE}/code/verify`, {
        email,
        code: code.trim(),
      });
      setCode("");
      setState(next);
    });
  };

  const resend = () =>
    run(async () => {
      await apiPost(`${BASE}/code/resend`, { email });
      setNotice("A new security code is on its way. Use the newest email.");
    });

  const buy = (plan: RegistrationPlan) =>
    run(async () => {
      const checkout = await apiPost<{
        subscription_id: string; razorpay_key_id: string;
      }>(`${BASE}/monthly/subscribe`, {
        plan_slug: plan.slug,
      });
      await new Promise<void>((resolve, reject) => {
        void openSubscriptionCheckout({
          keyId: checkout.razorpay_key_id,
          subscriptionId: checkout.subscription_id,
          name: "Vivekium",
          description: `${plan.name} monthly plan`,
          prefill: { email, name: state?.first_name ?? undefined },
          onSuccess: (payload) => {
            apiPost<OnboardingState>(`${BASE}/monthly/verify`, payload)
              .then((next) => {
                setState(next);
                if (next.stage === "choose_pack") {
                  setPaymentPending(true);
                  setNotice("Your payment mandate is authorized. The pilot starts when the first monthly charge is confirmed. This page will update automatically.");
                }
                resolve();
              })
              .catch(reject);
          },
          onDismiss: () => resolve(),
        }).then((opened) => {
          if (!opened) {
            reject(
              new Error(
                "Checkout could not open. Check that your browser is not blocking payment scripts, then retry."
              )
            );
          }
        });
      });
    });

  const submitPassword = (event: React.FormEvent) => {
    event.preventDefault();
    if (existingSignIn) {
      if (!password) {
        setError("Enter your existing password.");
        return;
      }
      run(async () => {
        try {
          const credential = await signInWithEmailAndPassword(firebaseAuth, email, password);
          finish(
            await apiPost<AuthSession>(`${BASE}/activate`, {
              id_token: await credential.user.getIdToken(),
            })
          );
        } catch (failure) {
          if (failure instanceof ApiError) throw failure;
          throw new Error(friendlyAuthError(failure) ?? "That password did not work.");
        }
      });
      return;
    }
    if (!isPasswordValid(rules)) {
      setError("Use at least 8 characters with an uppercase letter, a lowercase letter and a number.");
      return;
    }
    run(async () => {
      try {
        finish(await apiPost<AuthSession>(`${BASE}/activate`, { password }));
      } catch (failure) {
        if (failure instanceof ApiError && failure.status === 409) {
          // The address already has a sign-in: finish with it instead.
          setExistingSignIn(true);
          setPassword("");
        }
        throw failure;
      }
    });
  };

  if (!state && !loadError) {
    return (
      <AuthShell title="Register your company">
        <LoadingRows rows={3} label="Loading your registration" />
      </AuthShell>
    );
  }

  if (loadError || !state) {
    return (
      <AuthShell title="Register your company" description={loadError}>
        <Button size="lg" className="w-full" onClick={() => void loadState()}>
          Try again
        </Button>
      </AuthShell>
    );
  }

  const footer = (
    <p>
      Already registered? <AuthLink href="/company/login">Company login</AuthLink>
    </p>
  );

  if (state.stage === "done") {
    return (
      <AuthShell
        title="Your company is ready"
        description="Your registration is finished. Sign in with your email and password."
      >
        <Button asChild size="lg" className="w-full">
          <Link href="/company/login">Go to company login</Link>
        </Button>
      </AuthShell>
    );
  }

  if (state.stage === "details" && resuming) {
    return (
      <AuthShell
        title="Continue your registration"
        description="Enter the email address you registered with and we will send a new security code."
        footer={footer}
      >
        <form className="space-y-4" onSubmit={submitResume}>
          <div className="space-y-1.5">
            <Label htmlFor="resume-email">Company email</Label>
            <Input
              id="resume-email"
              type="email"
              autoComplete="email"
              value={pendingEmail}
              disabled={busy}
              onChange={(event) => setPendingEmail(event.target.value)}
            />
          </div>
          <Button size="lg" className="w-full" disabled={busy}>
            {busy ? <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" /> : null}
            Send a security code
          </Button>
        </form>
        <p className="text-center text-sm">
          <button type="button" className="font-semibold underline" onClick={() => setResuming(false)}>
            Start a new registration instead
          </button>
        </p>
        {error ? <InlineError>{error}</InlineError> : null}
      </AuthShell>
    );
  }

  if (state.stage === "details") {
    return (
      <AuthShell
        title="Register your company"
        description="Create your company workspace. You will be its Company Super Admin and can invite your team after the first monthly payment."
        footer={footer}
      >
        <form className="space-y-4" onSubmit={submitDetails}>
          <div className="grid gap-4 sm:grid-cols-2">
            <div className="space-y-1.5">
              <Label htmlFor="reg-first">First name</Label>
              <Input id="reg-first" autoComplete="given-name" {...field("first_name")} />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="reg-last">Last name</Label>
              <Input id="reg-last" autoComplete="family-name" {...field("last_name")} />
            </div>
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="reg-email">Company email</Label>
            <Input id="reg-email" type="email" autoComplete="email" {...field("email")} />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="reg-phone">Mobile number</Label>
            <Input
              id="reg-phone"
              type="tel"
              autoComplete="tel"
              placeholder="+91 98765 43210"
              {...field("phone")}
            />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="reg-company">Company name</Label>
            <Input id="reg-company" autoComplete="organization" {...field("company_name")} />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="reg-industry">Industry</Label>
            <select id="reg-industry" className={SELECT_CLASS} {...field("industry")}>
              <option value="" disabled>
                Choose an industry
              </option>
              {INDUSTRIES.map((industry) => (
                <option key={industry} value={industry}>
                  {industry}
                </option>
              ))}
            </select>
          </div>
          {details.industry === "Other" ? (
            <div className="space-y-1.5">
              <Label htmlFor="reg-industry-other">Your industry</Label>
              <Input id="reg-industry-other" {...field("industry_other")} />
            </div>
          ) : null}
          {demoModeEnabled() ? (
            <div className="space-y-1.5">
              <Label htmlFor="reg-demo-password">Password</Label>
              <div className="relative">
                <Input
                  id="reg-demo-password"
                  type={showPassword ? "text" : "password"}
                  autoComplete="new-password"
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
          ) : null}
          <Button size="lg" className="w-full" disabled={busy}>
            {busy ? <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" /> : null}
            {demoModeEnabled() ? "Create demo workspace" : "Send security code"}
          </Button>
        </form>
        <p className="text-center text-sm">
          Already started?{" "}
          <button
            type="button"
            className="font-semibold underline"
            onClick={() => {
              setError(null);
              setResuming(true);
            }}
          >
            Continue your registration
          </button>
        </p>
        {error ? <InlineError>{error}</InlineError> : null}
      </AuthShell>
    );
  }

  if (state.stage === "verify_email") {
    return (
      <AuthShell
        title="Check your email"
        description="Enter the six-digit security code we sent to your company email. It expires in ten minutes."
        footer={footer}
      >
        <div className="flex items-start gap-3 rounded-xl border border-border bg-brand-100/50 px-4 py-3">
          <MailCheck className="mt-0.5 h-4 w-4 shrink-0 text-brand-600" aria-hidden="true" />
          <p className="min-w-0 truncate text-sm font-semibold">{email}</p>
        </div>
        <form className="space-y-4" onSubmit={submitCode}>
          <div className="space-y-1.5">
            <Label htmlFor="reg-code">Security code</Label>
            <Input
              id="reg-code"
              inputMode="numeric"
              autoComplete="one-time-code"
              maxLength={6}
              value={code}
              disabled={busy}
              onChange={(event) => setCode(event.target.value.replace(/\D/g, ""))}
            />
          </div>
          <Button size="lg" className="w-full" disabled={busy}>
            {busy ? <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" /> : null}
            Verify email
          </Button>
        </form>
        <p className="text-center text-sm">
          No email?{" "}
          <button type="button" className="font-semibold underline" disabled={busy} onClick={resend}>
            Send a new security code
          </button>
        </p>
        {notice ? <p role="status" className="text-center text-sm">{notice}</p> : null}
        {error ? <InlineError>{error}</InlineError> : null}
      </AuthShell>
    );
  }

  if (state.stage === "choose_pack") {
    return (
      <AuthShell
        className="max-w-4xl"
        title="Start your 30-day Starter pilot"
        description={`Your paid 30-day pilot activates ${state.company_name ?? "your company"}. There is no annual commitment or lock-in, and you can cancel anytime.`}
        footer={footer}
      >
        {plans ? (
          <div className="grid gap-4 sm:grid-cols-2">
            {plans.filter((plan) => plan.slug === "starter").map((plan) => (
              <div key={plan.slug} className="rounded-xl border border-border bg-surface p-5">
                <h2 className="text-lg font-semibold">{plan.name}</h2>
                <p className="mt-2 text-2xl font-semibold tabular-nums">{formatInr(plan.price_inr)} / month</p>
                <p className="mt-2 text-sm">{plan.assessments.toLocaleString("en-IN")} completed assessments per month</p>
                <p className="mt-3 rounded-lg bg-teal-50 p-3 text-sm dark:bg-teal-950/30">Unused monthly credits roll over for {plan.rollover_months} months, then expire.</p>
                <p className="mt-3 text-sm">GST: {formatInr(plan.gst_inr)}. Due today: {formatInr(plan.total_inr)}.</p>
                <Button className="mt-5 w-full" disabled={busy || paymentPending} onClick={() => buy(plan)}>
                  {busy ? "Opening checkout..." : `Start on ${plan.name}`}
                </Button>
              </div>
            ))}
          </div>
        ) : error ? null : (
          <LoadingRows rows={2} label="Loading monthly plans" />
        )}
        {notice ? <p role="status" className="mt-4 text-sm">{notice}</p> : null}
        {error ? <InlineError>{error}</InlineError> : null}
      </AuthShell>
    );
  }

  // set_password
  return (
    <AuthShell
      title="Set your password"
      description="Payment confirmed. Set the password you will use to sign in to your company workspace."
      footer={footer}
    >
      <form className="space-y-4" onSubmit={submitPassword}>
        <div className="space-y-1.5">
          <Label htmlFor="reg-login-email">Email address</Label>
          <Input id="reg-login-email" type="email" autoComplete="username" value={email} readOnly aria-readonly="true" />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="reg-password">
            {existingSignIn ? "Your existing password" : "Create a password"}
          </Label>
          <div className="relative">
            <Input
              id="reg-password"
              type={showPassword ? "text" : "password"}
              autoComplete={existingSignIn ? "current-password" : "new-password"}
              value={password}
              disabled={busy}
              className="pr-11"
              onChange={(event) => setPassword(event.target.value)}
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
          {existingSignIn ? null : <PasswordRules rules={rules} />}
        </div>
        <Button size="lg" className="w-full" disabled={busy}>
          {busy ? <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" /> : null}
          {existingSignIn ? "Sign in and open workspace" : "Set password and open workspace"}
        </Button>
      </form>
      {error ? <InlineError>{error}</InlineError> : null}
    </AuthShell>
  );
}
