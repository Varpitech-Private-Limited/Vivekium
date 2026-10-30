"use client";

import type { User as FirebaseUser } from "firebase/auth";

import { ApiError, apiPost } from "@/lib/api";
import type {
  AuthContextsResponse,
  AuthSession,
  Role,
} from "@/lib/types";
import { isContextsResponse } from "@/lib/types";
/**
 * Result of trading a Firebase ID token for a ReadyPick session.
 * The backend returns EITHER a finalized single-user session (cookies set)
 * OR a multi-workspace identity that still needs `select-context`.
 */
export type FirebaseExchangeResult = AuthSession | AuthContextsResponse;

/**
 * The surfaces that may open a session. The purpose IS the portal: a
 * candidate sign-in opens the candidate workspace, a company sign-in (or an
 * invitation) the company one, and the Provider and business development
 * sign-ins their consoles. The wire name is still `captcha_purpose`.
 */
export type ExchangePurpose =
  | "candidate_login"
  | "candidate_register"
  | "company_login"
  | "invite_join"
  | "provider_login"
  | "bd_login";

export { isContextsResponse };

/**
 * POST /auth/firebase/session, trade a freshly minted Firebase ID token for a
 * ReadyPick session. Single-user → {user, capabilities} (cookies set).
 * Multi-workspace → {contexts, context_token} (no cookies yet).
 */
export async function exchangeFirebaseSession(
  user: FirebaseUser,
  purpose: ExchangePurpose
): Promise<FirebaseExchangeResult> {
  return apiPost<FirebaseExchangeResult>("/auth/firebase/session", {
    id_token: await user.getIdToken(),
    captcha_purpose: purpose,
  });
}

/**
 * POST /auth/select-context, finalize a multi-workspace identity by choosing
 * one context. Returns the single-user session (cookies set).
 */
export async function selectContext(
  contextToken: string,
  userId: string
): Promise<AuthSession> {
  return apiPost<AuthSession>("/auth/select-context", {
    context_token: contextToken,
    user_id: userId,
  });
}

/** Human-readable label for a role, used in the workspace chooser. */
export const ROLE_LABEL: Record<Role, string> = {
  super_admin: "Owner",
  client: "Client admin",
  recruitment_manager: "Recruitment manager",
  hr_manager: "HR manager",
  recruiter: "Recruiter",
  hiring_manager: "Hiring manager",
  interview_manager: "Interview manager",
  ceo: "CEO",
  md: "MD",
  functional_head: "Functional head",
  candidate: "Candidate",
  bd: "Business development",
};

/**
 * Map a Firebase / backend auth failure to clean, user-facing copy, never a
 * stack trace or raw error code. Returns `null` for user-initiated
 * cancellations (e.g. closing the Google popup), where showing an error would
 * be noise rather than signal.
 */
export function friendlyAuthError(err: unknown): string | null {
  // Backend rejection (e.g. staff attempting Google sign-in → 403).
  if (err instanceof ApiError) {
    const raw =
      err.detail && typeof err.detail === "object" && "detail" in err.detail
        ? (err.detail as { detail: unknown }).detail
        : undefined;
    // A validation list is not a sentence; only a string detail is shown.
    const detail = typeof raw === "string" ? raw : "";
    if (err.status === 403) {
      if (detail === "Google sign-in is available to candidates only") {
        return "Company accounts sign in with an email and password. Use Company login.";
      }
      if (detail === "Account unavailable") {
        return "This account is unavailable. Contact support if you think this is a mistake.";
      }
      if (detail === "No candidate workspace is linked to this account") {
        return "This email does not have a candidate account. Company team members sign in on the Company login page.";
      }
      if (detail === "No company workspace is linked to this account") {
        return "This email is not a company team account. Candidates sign in on the candidate sign-in page.";
      }
      if (detail.startsWith("No ") && detail.includes(" workspace is linked")) {
        return "That account does not have access to this sign-in page. Use the sign-in page for your account.";
      }
      return "This sign-in method isn't available for your account.";
    }
    // An expired step or a password rule: the server's sentence is already
    // written for a person.
    if ((err.status === 400 || err.status === 409 || err.status === 422) && detail) {
      return detail;
    }
    if (err.status === 429) {
      return "Too many attempts. Please wait a few minutes and try again.";
    }
    if (err.status >= 500) {
      return "Something went wrong on our end. Please try again in a moment.";
    }
    return "We couldn't complete sign-in. Please try again.";
  }

  const code =
    typeof err === "object" && err !== null && "code" in err
      ? String((err as { code: unknown }).code)
      : "";

  switch (code) {
    // User cancelled, stay silent.
    case "auth/popup-closed-by-user":
    case "auth/cancelled-popup-request":
    case "auth/user-cancelled":
      return null;

    case "auth/popup-blocked":
      return "Your browser blocked the sign-in popup. Allow popups and try again.";

    // The serving origin is not in Firebase Auth's authorized-domains list.
    // This is an OPERATOR fault, never the user's: telling them to "check
    // their details" (the default below) sends them round the login form for
    // a failure no retyping can fix. Production hit exactly this on
    // 2026-09-05, when the apex readypick.ai was serving traffic while only
    // www.readypick.ai had been authorized. The console line names the origin
    // so the next operator can see the mismatch in one glance; no token or
    // account information is ever logged.
    case "auth/unauthorized-domain":
      console.error(
        `google-sign-in failed: ${code} for origin ${window.location.origin}. ` +
          "Add this origin to Firebase Authentication > Settings > Authorized domains."
      );
      return "Google sign-in is currently unavailable. Please try again.";

    // Provider misconfiguration or a transient Firebase-side fault. Same
    // shape: not the user's fault, so the copy says unavailable, not invalid.
    case "auth/internal-error":
    case "auth/invalid-api-key":
    case "auth/configuration-not-found":
      console.error(`google-sign-in failed: ${code}`);
      return "Google sign-in is currently unavailable. Please try again.";

    // Newer Firebase collapses wrong-password / user-not-found into this.
    case "auth/invalid-credential":
    case "auth/wrong-password":
      return "Incorrect email or password.";
    case "auth/user-not-found":
      return "No account found with that email.";
    case "auth/invalid-email":
      return "Enter a valid email address.";
    case "auth/user-disabled":
      return "This account has been disabled. Contact your administrator.";

    case "auth/email-already-in-use":
      return "An account with this email already exists. Sign in instead.";
    case "auth/weak-password":
      return "Choose a password with at least 8 characters.";

    case "auth/too-many-requests":
      return "Too many attempts. Please wait a few minutes and try again.";
    case "auth/network-request-failed":
      return "Network error. Check your connection and try again.";
    case "auth/account-exists-with-different-credential":
      return "An account already exists with a different sign-in method for this email.";
    case "auth/operation-not-allowed":
      return "This sign-in method isn't enabled. Try another option.";

    default:
      return "Sign-in could not be completed. Check your details and try again.";
  }
}
