// @vitest-environment jsdom
//
// Register your company (owner spec 2026-09-29, sections 2.2 and 5). Pinned:
// the page resumes at the step the SERVER reports; the details step offers no
// Google and does not ask for a security check; the first
// purchase goes through the onboarding routes (never a billing or
// subscription route) and success is only claimed after the server verifies
// the payment; the password step opens the workspace, and an address that
// already has a sign-in finishes with its existing password.

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

const http = vi.hoisted(() => ({ apiGet: vi.fn(), apiPost: vi.fn() }));
vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, ...http };
});
const firebaseSdk = vi.hoisted(() => ({ signInWithEmailAndPassword: vi.fn() }));
vi.mock("firebase/auth", () => firebaseSdk);
vi.mock("@/lib/firebase", () => ({ firebaseAuth: { name: "test-auth" } }));
const navigation = vi.hoisted(() => ({ replace: vi.fn() }));
vi.mock("next/navigation", () => ({ useRouter: () => ({ replace: navigation.replace }) }));
const auth = vi.hoisted(() => ({ setSession: vi.fn() }));
vi.mock("@/lib/auth-context", () => ({ useAuth: () => ({ setSession: auth.setSession }) }));
const checkout = vi.hoisted(() => ({ openSubscriptionCheckout: vi.fn() }));
vi.mock("@/lib/razorpay", () => checkout);

import { ApiError } from "@/lib/api";
import { CompanyRegisterFlow } from "./company-register-flow";

const PLANS = { plans: [{
  slug: "starter", name: "Starter", price_inr: 24000, assessments: 75,
  gst_inr: 4320, total_inr: 28320, rollover_months: 3,
}] };

let stage: Record<string, unknown> = { stage: "details" };

function serve(overrides: Record<string, (body: unknown) => unknown> = {}) {
  http.apiGet.mockImplementation(async (path: string) => {
    if (path === "/company-onboarding/state") return stage;
    if (path === "/company-onboarding/monthly/plans") return PLANS;
    throw new Error(`unexpected GET ${path}`);
  });
  http.apiPost.mockImplementation(async (path: string, body: unknown) => {
    if (overrides[path]) return overrides[path](body);
    if (path === "/company-onboarding/register") return { message: "sent" };
    if (path === "/company-onboarding/code/verify") {
      return { stage: "choose_pack", email: "founder@circa.com", company_name: "Circa Corp" };
    }
    if (path === "/company-onboarding/monthly/subscribe") {
      return { subscription_id: "sub_1", razorpay_key_id: "rzp_test" };
    }
    if (path === "/company-onboarding/monthly/verify") {
      return { stage: "set_password", email: "founder@circa.com", company_name: "Circa Corp" };
    }
    if (path === "/company-onboarding/activate") {
      return { user: { id: "u1", role: "client" }, capabilities: ["manage_billing"] };
    }
    throw new Error(`unexpected POST ${path}`);
  });
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  stage = { stage: "details" };
});

function posted(): string[] {
  return http.apiPost.mock.calls.map((call) => call[0] as string);
}

describe("register your company", () => {
  it("sends the details with no security check and offers no Google", async () => {
    serve();
    render(<CompanyRegisterFlow />);
    expect(screen.queryByRole("button", { name: /google/i })).toBeNull();
    expect(screen.queryByLabelText("Security check")).toBeNull();

    fireEvent.change(await screen.findByLabelText("First name"), { target: { value: "Saravan" } });
    fireEvent.change(screen.getByLabelText("Last name"), { target: { value: "Kumar" } });
    fireEvent.change(screen.getByLabelText("Company email"), { target: { value: "founder@circa.com" } });
    fireEvent.change(screen.getByLabelText("Mobile number"), { target: { value: "98765 43210" } });
    fireEvent.change(screen.getByLabelText("Company name"), { target: { value: "Circa Corp" } });
    fireEvent.change(screen.getByLabelText("Industry"), { target: { value: "Technology" } });
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
    });
  });

  it("resumes at the step the server reports and verifies the code", async () => {
    stage = { stage: "verify_email", email: "founder@circa.com" };
    serve();
    render(<CompanyRegisterFlow />);
    fireEvent.change(await screen.findByLabelText("Security code"), { target: { value: "123456" } });
    fireEvent.click(screen.getByRole("button", { name: "Verify email" }));
    await screen.findByText("Start your 30-day Starter pilot");
    expect(http.apiPost).toHaveBeenCalledWith("/company-onboarding/code/verify", {
      email: "founder@circa.com",
      code: "123456",
    });
  });

  it("pays for the Starter pilot through the onboarding routes", async () => {
    stage = { stage: "choose_pack", email: "founder@circa.com", company_name: "Circa Corp" };
    serve();
    checkout.openSubscriptionCheckout.mockImplementation(async (options) => {
      options.onSuccess({
        razorpay_subscription_id: "sub_1",
        razorpay_payment_id: "pay_1",
        razorpay_signature: "sig",
      });
      return true;
    });
    render(<CompanyRegisterFlow />);
    fireEvent.click(await screen.findByRole("button", { name: "Start on Starter" }));
    await screen.findByText("Set your password");
    expect(http.apiPost).toHaveBeenCalledWith("/company-onboarding/monthly/subscribe", { plan_slug: "starter" });
    expect(http.apiPost).toHaveBeenCalledWith("/company-onboarding/monthly/verify", {
      razorpay_subscription_id: "sub_1",
      razorpay_payment_id: "pay_1",
      razorpay_signature: "sig",
    });
    expect(posted().filter((path) => path.startsWith("/billing"))).toEqual([]);
  });

  it("sets the password and opens the workspace", async () => {
    stage = { stage: "set_password", email: "founder@circa.com" };
    serve();
    render(<CompanyRegisterFlow />);
    fireEvent.change(await screen.findByLabelText("Create a password"), { target: { value: "Circa-Corp-2026" } });
    fireEvent.click(screen.getByRole("button", { name: "Set password and open workspace" }));
    await waitFor(() => expect(navigation.replace).toHaveBeenCalledWith("/org"));
    expect(http.apiPost).toHaveBeenCalledWith("/company-onboarding/activate", { password: "Circa-Corp-2026" });
    expect(auth.setSession).toHaveBeenCalled();
  });

  it("finishes with an existing sign-in when the address already has one", async () => {
    stage = { stage: "set_password", email: "founder@circa.com" };
    let activations = 0;
    serve({
      "/company-onboarding/activate": (body) => {
        activations += 1;
        if (activations === 1) {
          throw new ApiError(409, { detail: "This email address already has a ReadyPick sign-in." });
        }
        expect(body).toEqual({ id_token: "id-token" });
        return { user: { id: "u1", role: "client" }, capabilities: [] };
      },
    });
    firebaseSdk.signInWithEmailAndPassword.mockResolvedValue({
      user: { getIdToken: async () => "id-token" },
    });
    render(<CompanyRegisterFlow />);
    fireEvent.change(await screen.findByLabelText("Create a password"), { target: { value: "Circa-Corp-2026" } });
    fireEvent.click(screen.getByRole("button", { name: "Set password and open workspace" }));
    const existing = await screen.findByLabelText("Your existing password");
    fireEvent.change(existing, { target: { value: "Old-Password-1" } });
    fireEvent.click(screen.getByRole("button", { name: "Sign in and open workspace" }));
    await waitFor(() => expect(navigation.replace).toHaveBeenCalledWith("/org"));
    expect(firebaseSdk.signInWithEmailAndPassword).toHaveBeenCalledWith(
      { name: "test-auth" }, "founder@circa.com", "Old-Password-1",
    );
  });
});
