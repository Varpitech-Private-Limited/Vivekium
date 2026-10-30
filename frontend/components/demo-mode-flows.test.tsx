// @vitest-environment jsdom
//
// Public demo (A-23). NEXT_PUBLIC_DEMO_MODE is read by demoModeEnabled().
// Flag on: sign-in posts /auth/demo/session first, company registration posts
// /company-onboarding/demo, and a credit pack posts /billing/demo/purchase
// without opening Razorpay. Flag off leaves the existing posts in place and
// never calls /auth/demo/session.

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const http = vi.hoisted(() => ({ apiGet: vi.fn(), apiPost: vi.fn() }));
vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, ...http };
});

const firebaseSdk = vi.hoisted(() => ({
  signInWithEmailAndPassword: vi.fn(),
  signInWithPopup: vi.fn(),
}));
vi.mock("firebase/auth", () => firebaseSdk);
vi.mock("@/lib/firebase", () => ({
  firebaseAuth: { name: "test-auth" },
  createCandidateGoogleProvider: () => ({}),
}));

const navigation = vi.hoisted(() => ({
  replace: vi.fn(),
  push: vi.fn(),
  params: new URLSearchParams(),
}));
vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: navigation.replace, push: navigation.push }),
  usePathname: () => "/org/billing",
  useSearchParams: () => navigation.params,
}));

const auth = vi.hoisted(() => ({ setSession: vi.fn() }));
vi.mock("@/lib/auth-context", () => ({
  useAuth: () => ({
    setSession: auth.setSession,
    user: {
      id: "admin-1",
      role: "client",
      tenant_id: "t1",
      full_name: "Saravan Kumar",
      email: "founder@circa.com",
      email_verified: true,
      phone_verified: true,
      workspace_name: "Circa",
    },
    hasCapability: () => true,
    capabilities: ["manage_billing", "view_billing"],
    loading: false,
  }),
  // Same destinations as homePathForRole for the roles these tests sign in.
  homePathForRole: (role: string) => {
    if (role === "candidate") return "/portal";
    if (role === "super_admin") return "/admin";
    if (role === "bd") return "/bd";
    return "/org";
  },
}));

const checkout = vi.hoisted(() => ({
  openOrderCheckout: vi.fn(),
  openSubscriptionCheckout: vi.fn(),
}));
vi.mock("@/lib/razorpay", () => checkout);

const toast = vi.hoisted(() => vi.fn());
vi.mock("@/components/ui/toast", () => ({ useToast: () => ({ toast }) }));

import { ApiError } from "@/lib/api";
import BillingPage from "@/app/(org)/org/billing/page";
import { CompanyRegisterFlow } from "./company-register-flow";
import { LoginFlow, LoginPageFlow } from "./login-flow";

type Surface = "candidate" | "company" | "provider" | "bd";
type SessionRole = "candidate" | "client" | "super_admin" | "bd";

const PURPOSE: Record<Surface, string> = {
  candidate: "candidate_login",
  company: "company_login",
  provider: "provider_login",
  bd: "bd_login",
};

const ROLE: Record<Surface, SessionRole> = {
  candidate: "candidate",
  company: "client",
  provider: "super_admin",
  bd: "bd",
};

function homeFor(role: SessionRole): string {
  if (role === "candidate") return "/portal";
  if (role === "super_admin") return "/admin";
  if (role === "bd") return "/bd";
  return "/org";
}

const CHALLENGE = {
  challenge_id: "c1",
  image: "data:image/svg+xml;base64,PHN2Zy8+",
  expires_in: 300,
};

const trace: string[] = [];

function paths(): string[] {
  return http.apiPost.mock.calls.map((call) => String(call[0]));
}

function makeSession(role: SessionRole) {
  return {
    user: {
      id: `user-${role}`,
      role,
      tenant_id: role === "client" ? "t1" : null,
      full_name: "Asha Rao",
      email: "a@b.test",
      email_verified: true,
      phone_verified: false,
      workspace_name: "ReadyPick",
    },
    capabilities: role === "client" ? ["manage_billing"] : [],
  };
}

function installSignIn(demo: "ok" | number, role: SessionRole) {
  const session = makeSession(role);
  http.apiPost.mockImplementation(async (path: string) => {
    trace.push(path);
    if (path === "/auth/captcha/challenge") return CHALLENGE;
    if (path === "/auth/captcha/verify") return { captcha_proof: "proof-1" };
    if (path === "/auth/demo/session") {
      if (demo !== "ok") throw new ApiError(demo, { detail: "Demo sign-in was refused." });
      return session;
    }
    if (path === "/auth/firebase/session") return session;
    throw new Error(`unexpected POST ${path}`);
  });
  firebaseSdk.signInWithEmailAndPassword.mockImplementation(async () => {
    trace.push("firebase");
    return { user: { getIdToken: async () => "id-token" } };
  });
  return session;
}

async function submitSignIn(): Promise<number> {
  await screen.findByLabelText("Email address");
  if (screen.queryByLabelText("Security check")) {
    await screen.findByAltText(/security check characters/i);
    fireEvent.change(screen.getByLabelText("Security check"), { target: { value: "k7m2px" } });
  }
  fireEvent.change(screen.getByLabelText("Email address"), { target: { value: "a@b.test" } });
  fireEvent.change(screen.getByLabelText("Password"), { target: { value: "Secret-123" } });
  const start = trace.length;
  fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
  return start;
}

function expectDemoCredentials(portal: Surface) {
  expect(http.apiPost).toHaveBeenCalledWith("/auth/demo/session", {
    email: "a@b.test",
    password: "Secret-123",
    portal,
  });
}

beforeEach(() => {
  vi.stubEnv("NEXT_PUBLIC_DEMO_MODE", "false");
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      throw new Error(`unexpected fetch ${String(input)}`);
    }),
  );
});

afterEach(() => {
  cleanup();
  trace.length = 0;
  navigation.params = new URLSearchParams();
  vi.clearAllMocks();
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
});

describe("demo sign-in", () => {
  it("posts the candidate portal first and opens that session", async () => {
    vi.stubEnv("NEXT_PUBLIC_DEMO_MODE", "true");
    navigation.params = new URLSearchParams();
    const session = installSignIn("ok", "candidate");
    render(<LoginPageFlow />);
    const start = await submitSignIn();

    await waitFor(() => expect(auth.setSession).toHaveBeenCalled());
    expect(trace[start]).toBe("/auth/demo/session");
    expectDemoCredentials("candidate");
    expect(trace).not.toContain("/auth/captcha/verify");
    expect(trace).not.toContain("/auth/firebase/session");
    expect(firebaseSdk.signInWithEmailAndPassword).not.toHaveBeenCalled();
    expect(auth.setSession).toHaveBeenCalledWith(session.user, session.capabilities);
    expect(navigation.replace).toHaveBeenCalledWith("/portal");
  });

  it("posts the company portal first and opens that session", async () => {
    vi.stubEnv("NEXT_PUBLIC_DEMO_MODE", "true");
    const session = installSignIn("ok", "client");
    render(<LoginFlow surface="company" title="Sign in to your company workspace" />);
    const start = await submitSignIn();

    await waitFor(() => expect(auth.setSession).toHaveBeenCalled());
    expect(trace[start]).toBe("/auth/demo/session");
    expectDemoCredentials("company");
    expect(firebaseSdk.signInWithEmailAndPassword).not.toHaveBeenCalled();
    expect(trace).not.toContain("/auth/firebase/session");
    expect(auth.setSession).toHaveBeenCalledWith(session.user, session.capabilities);
    expect(navigation.replace).toHaveBeenCalledWith("/org");
  });

  it("posts portal provider when the address is ?portal=owner", async () => {
    vi.stubEnv("NEXT_PUBLIC_DEMO_MODE", "true");
    navigation.params = new URLSearchParams("portal=owner");
    const session = installSignIn("ok", "super_admin");
    render(<LoginPageFlow />);
    const start = await submitSignIn();

    await waitFor(() => expect(auth.setSession).toHaveBeenCalled());
    expect(trace[start]).toBe("/auth/demo/session");
    expectDemoCredentials("provider");
    expect(paths().some((path) => path.includes("portal=owner"))).toBe(false);
    expect(firebaseSdk.signInWithEmailAndPassword).not.toHaveBeenCalled();
    expect(auth.setSession).toHaveBeenCalledWith(session.user, session.capabilities);
    expect(navigation.replace).toHaveBeenCalledWith("/admin");
  });

  it("posts the bd portal first and opens that session", async () => {
    vi.stubEnv("NEXT_PUBLIC_DEMO_MODE", "true");
    navigation.params = new URLSearchParams("portal=bd");
    const session = installSignIn("ok", "bd");
    render(<LoginPageFlow />);
    const start = await submitSignIn();

    await waitFor(() => expect(auth.setSession).toHaveBeenCalled());
    expect(trace[start]).toBe("/auth/demo/session");
    expectDemoCredentials("bd");
    expect(firebaseSdk.signInWithEmailAndPassword).not.toHaveBeenCalled();
    expect(auth.setSession).toHaveBeenCalledWith(session.user, session.capabilities);
    expect(navigation.replace).toHaveBeenCalledWith("/bd");
  });

  it.each([409, 404] as const)(
    "falls through to Firebase when the company demo session returns %s",
    async (status) => {
      vi.stubEnv("NEXT_PUBLIC_DEMO_MODE", "true");
      const session = installSignIn(status, "client");
      render(<LoginFlow surface="company" title="Sign in to your company workspace" />);
      const start = await submitSignIn();

      await waitFor(() => expect(auth.setSession).toHaveBeenCalled());
      expect(trace[start]).toBe("/auth/demo/session");
      expectDemoCredentials("company");
      const demoAt = trace.indexOf("/auth/demo/session");
      expect(trace.indexOf("/auth/captcha/verify")).toBeGreaterThan(demoAt);
      expect(trace.indexOf("firebase")).toBeGreaterThan(trace.indexOf("/auth/captcha/verify"));
      expect(trace.indexOf("/auth/firebase/session")).toBeGreaterThan(trace.indexOf("firebase"));
      expect(firebaseSdk.signInWithEmailAndPassword).toHaveBeenCalledWith(
        { name: "test-auth" },
        "a@b.test",
        "Secret-123",
      );
      expect(http.apiPost).toHaveBeenCalledWith(
        "/auth/firebase/session",
        expect.objectContaining({
          id_token: "id-token",
          captcha_proof: "proof-1",
          captcha_purpose: "company_login",
        }),
      );
      expect(auth.setSession).toHaveBeenCalledWith(session.user, session.capabilities);
      expect(navigation.replace).toHaveBeenCalledWith("/org");
    },
  );

  it("does not call Firebase when the company demo session returns 403", async () => {
    vi.stubEnv("NEXT_PUBLIC_DEMO_MODE", "true");
    installSignIn(403, "client");
    render(<LoginFlow surface="company" title="Sign in to your company workspace" />);
    const start = await submitSignIn();

    await waitFor(() => {
      expect(trace.length).toBeGreaterThan(start);
      const button = screen.getByRole("button", { name: "Sign in" }) as HTMLButtonElement;
      expect(button.disabled).toBe(false);
    });
    expect(trace[start]).toBe("/auth/demo/session");
    expectDemoCredentials("company");
    expect((await screen.findByRole("alert")).textContent).toContain("Demo sign-in was refused.");
    expect(firebaseSdk.signInWithEmailAndPassword).not.toHaveBeenCalled();
    expect(trace).not.toContain("/auth/firebase/session");
    expect(auth.setSession).not.toHaveBeenCalled();
  });

  it.each(["provider", "bd"] as const)(
    "falls through to Firebase when the %s demo session returns 403",
    async (surface) => {
      vi.stubEnv("NEXT_PUBLIC_DEMO_MODE", "true");
      const role = ROLE[surface];
      const session = installSignIn(403, role);
      navigation.params = new URLSearchParams(surface === "provider" ? "portal=owner" : "portal=bd");
      render(<LoginPageFlow />);
      const start = await submitSignIn();

      await waitFor(() => expect(auth.setSession).toHaveBeenCalled());
      expect(trace[start]).toBe("/auth/demo/session");
      expectDemoCredentials(surface);
      expect(trace.indexOf("firebase")).toBeGreaterThan(trace.indexOf("/auth/demo/session"));
      expect(trace.indexOf("/auth/firebase/session")).toBeGreaterThan(trace.indexOf("firebase"));
      expect(firebaseSdk.signInWithEmailAndPassword).toHaveBeenCalledWith(
        { name: "test-auth" },
        "a@b.test",
        "Secret-123",
      );
      expect(http.apiPost).toHaveBeenCalledWith(
        "/auth/firebase/session",
        expect.objectContaining({
          id_token: "id-token",
          captcha_proof: "proof-1",
          captcha_purpose: PURPOSE[surface],
        }),
      );
      expect(auth.setSession).toHaveBeenCalledWith(session.user, session.capabilities);
      expect(navigation.replace).toHaveBeenCalledWith(homeFor(role));
    },
  );

  it.each(["candidate", "company", "provider", "bd"] as const)(
    "does not call /auth/demo/session when the flag is off (%s)",
    async (surface) => {
      vi.stubEnv("NEXT_PUBLIC_DEMO_MODE", "false");
      const role = ROLE[surface];
      const session = installSignIn("ok", role);
      render(<LoginFlow surface={surface} title="Sign in" />);
      const start = await submitSignIn();

      await waitFor(() => expect(auth.setSession).toHaveBeenCalled());
      expect(trace).not.toContain("/auth/demo/session");
      expect(trace[start]).toBe("/auth/captcha/verify");
      expect(firebaseSdk.signInWithEmailAndPassword).toHaveBeenCalledWith(
        { name: "test-auth" },
        "a@b.test",
        "Secret-123",
      );
      expect(http.apiPost).toHaveBeenCalledWith(
        "/auth/firebase/session",
        expect.objectContaining({
          id_token: "id-token",
          captcha_proof: "proof-1",
          captcha_purpose: PURPOSE[surface],
        }),
      );
      expect(auth.setSession).toHaveBeenCalledWith(session.user, session.capabilities);
      expect(navigation.replace).toHaveBeenCalledWith(homeFor(role));
    },
  );
});

const COMPANY_SESSION = {
  user: {
    id: "u-company",
    role: "client" as const,
    tenant_id: "t1",
    full_name: "Saravan Kumar",
    email: "founder@circa.com",
    email_verified: true,
    phone_verified: false,
    workspace_name: "Circa Corp",
  },
  capabilities: ["manage_billing"],
};

function installCompany() {
  http.apiGet.mockImplementation(async (path: string) => {
    if (path === "/company-onboarding/state") return { stage: "details" };
    if (path === "/company-onboarding/monthly/plans") return { plans: [] };
    throw new Error(`unexpected GET ${path}`);
  });
  http.apiPost.mockImplementation(async (path: string) => {
    trace.push(path);
    if (path === "/auth/captcha/challenge") return CHALLENGE;
    if (path === "/auth/captcha/verify") return { captcha_proof: "proof-1" };
    if (path === "/company-onboarding/demo") return COMPANY_SESSION;
    if (path === "/company-onboarding/register") return { message: "sent" };
    throw new Error(`unexpected POST ${path}`);
  });
}

async function fillCompanyDetails() {
  await screen.findByLabelText("First name");
  fireEvent.change(screen.getByLabelText("First name"), { target: { value: "Saravan" } });
  fireEvent.change(screen.getByLabelText("Last name"), { target: { value: "Kumar" } });
  fireEvent.change(screen.getByLabelText("Company email"), { target: { value: "founder@circa.com" } });
  fireEvent.change(screen.getByLabelText("Mobile number"), { target: { value: "98765 43210" } });
  fireEvent.change(screen.getByLabelText("Company name"), { target: { value: "Circa Corp" } });
  fireEvent.change(screen.getByLabelText("Industry"), { target: { value: "Technology" } });
  if (screen.queryByLabelText("Password")) {
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "Circa-Corp-2026" } });
  }
  if (screen.queryByLabelText("Security check")) {
    await screen.findByAltText(/security check characters/i);
    fireEvent.change(screen.getByLabelText("Security check"), { target: { value: "k7m2px" } });
  }
}

describe("demo company registration", () => {
  it("posts /company-onboarding/demo and does not post the live register route", async () => {
    vi.stubEnv("NEXT_PUBLIC_DEMO_MODE", "true");
    installCompany();
    render(<CompanyRegisterFlow />);
    await fillCompanyDetails();
    fireEvent.click(screen.getByRole("button", { name: /create demo workspace|send security code/i }));

    await waitFor(() => expect(paths()).toContain("/company-onboarding/demo"));
    expect(http.apiPost).toHaveBeenCalledWith("/company-onboarding/demo", {
      first_name: "Saravan",
      last_name: "Kumar",
      email: "founder@circa.com",
      phone: "98765 43210",
      company_name: "Circa Corp",
      industry: "Technology",
      industry_other: null,
      password: "Circa-Corp-2026",
    });
    expect(paths()).not.toContain("/company-onboarding/register");
    await waitFor(() => expect(navigation.replace).toHaveBeenCalledWith("/org"));
    expect(auth.setSession).toHaveBeenCalledWith(COMPANY_SESSION.user, COMPANY_SESSION.capabilities);
  });

  it("keeps Send security code and posts /company-onboarding/register when the flag is off", async () => {
    vi.stubEnv("NEXT_PUBLIC_DEMO_MODE", "false");
    installCompany();
    render(<CompanyRegisterFlow />);
    await fillCompanyDetails();
    expect(screen.getByRole("button", { name: "Send security code" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Create demo workspace" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Send security code" }));

    await screen.findByLabelText("Security code");
    expect(http.apiPost).toHaveBeenCalledWith("/company-onboarding/register", {
      first_name: "Saravan",
      last_name: "Kumar",
      email: "founder@circa.com",
      phone: "98765 43210",
      company_name: "Circa Corp",
      industry: "Technology",
      industry_other: null,
      captcha_proof: "proof-1",
    });
    expect(paths()).not.toContain("/company-onboarding/demo");
  });
});

const PURCHASE = {
  purchase_id: "p1",
  razorpay_order_id: "order_1",
  razorpay_key_id: "rzp_test",
  total_inr: 1180,
  credits: 75,
  bonus_credits: 0,
  subtotal_inr: 1000,
  setup_fee_inr: 0,
  gst_inr: 180,
};

const OVERVIEW = {
  credits: {
    balance_subunits: 600,
    balance_credits: "10.00",
    subunits_per_credit: 60,
    granted_subunits: 600,
    consumed_subunits: 0,
    rollover_subunits: 0,
    rollover_credits: "0.00",
    usage_this_month_subunits: {
      completed_assessment: 0,
      incomplete_assessment: 0,
      no_show: 0,
      old_profile_review: 0,
      adjustment: 0,
    },
    in_deficit: false,
    deficit_message: null,
    exhausted: false,
    low_balance: false,
    balance_fraction: 1,
    low_balance_threshold: 10,
    warning_level: 0,
    alert_message: null,
    unlimited: false,
    credit_validity_months: 12,
  },
  razorpay_key_id: null,
  recent_ledger: [],
  transactions: [],
};

function installBilling() {
  http.apiGet.mockImplementation(async (path: string) => {
    if (path === "/billing/overview") return OVERVIEW;
    if (path === "/billing/credit-packs") {
      return {
        gst_rate_percent: 18,
        packs: [{
          slug: "starter-75",
          label: "Starter 75",
          credits: 75,
          bonus_credits: 0,
          credits_total: 75,
          subtotal_inr: 1000,
          setup_fee_inr: 0,
          setup_fee_waived: true,
          gst_inr: 180,
          total_inr: 1180,
          available: true,
          trial: false,
        }],
      };
    }
    if (path === "/billing/purchases") return [];
    if (path === "/billing/public/plans") return { plans: [] };
    if (path === "/billing/monthly/current") {
      return { plan_slug: null, pending_plan_slug: null, status: null, current_end: null };
    }
    if (path === "/billing/monthly/charges") return [];
    if (path.startsWith("/billing/ledger")) return [];
    throw new Error(`unexpected GET ${path}`);
  });
  http.apiPost.mockImplementation(async (path: string) => {
    trace.push(path);
    if (path === "/billing/demo/purchase") return OVERVIEW;
    if (path === "/billing/purchase") return PURCHASE;
    throw new Error(`unexpected POST ${path}`);
  });
  checkout.openOrderCheckout.mockImplementation(async () => {
    trace.push("razorpay-order");
    return true;
  });
  checkout.openSubscriptionCheckout.mockImplementation(async () => {
    trace.push("razorpay-subscription");
    return true;
  });
}

async function buyStarterPack() {
  fireEvent.click(await screen.findByRole("button", { name: /Starter 75/i }));
  fireEvent.click(await screen.findByRole("button", { name: "Proceed to Payment" }));
}

describe("demo billing purchase", () => {
  it("posts /billing/demo/purchase and does not open Razorpay", async () => {
    vi.stubEnv("NEXT_PUBLIC_DEMO_MODE", "true");
    installBilling();
    render(<BillingPage />);
    await buyStarterPack();

    await waitFor(() => expect(paths()).toContain("/billing/demo/purchase"));
    expect(http.apiPost).toHaveBeenCalledWith("/billing/demo/purchase", { pack_slug: "starter-75" });
    expect(paths()).not.toContain("/billing/purchase");
    await waitFor(() =>
      expect(toast).toHaveBeenCalledWith(expect.objectContaining({ title: "Credits added" })),
    );
    expect(checkout.openOrderCheckout).not.toHaveBeenCalled();
    expect(checkout.openSubscriptionCheckout).not.toHaveBeenCalled();
    expect(trace).not.toContain("razorpay-order");
    expect(trace).not.toContain("razorpay-subscription");
  });

  it("posts /billing/purchase when the flag is off", async () => {
    vi.stubEnv("NEXT_PUBLIC_DEMO_MODE", "false");
    installBilling();
    render(<BillingPage />);
    await buyStarterPack();

    await waitFor(() => expect(checkout.openOrderCheckout).toHaveBeenCalled());
    expect(http.apiPost).toHaveBeenCalledWith("/billing/purchase", { pack_slug: "starter-75" });
    expect(paths()).not.toContain("/billing/demo/purchase");
    expect(checkout.openOrderCheckout).toHaveBeenCalledWith(
      expect.objectContaining({
        keyId: "rzp_test",
        orderId: "order_1",
        amountInr: 1180,
      }),
    );
    expect(trace.indexOf("/billing/purchase")).toBeLessThan(trace.indexOf("razorpay-order"));
    expect(checkout.openSubscriptionCheckout).not.toHaveBeenCalled();
  });
});
