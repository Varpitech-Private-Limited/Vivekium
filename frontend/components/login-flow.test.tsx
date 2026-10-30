// @vitest-environment jsdom
//
// The sign-in pages. Pinned: the company page offers no Google and names
// `company_login`; the candidate page offers Google and names
// `candidate_login`; the exchange carries the surface and no proof.

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

const http = vi.hoisted(() => ({ apiPost: vi.fn() }));
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
const navigation = vi.hoisted(() => ({ replace: vi.fn(), params: new URLSearchParams() }));
vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: navigation.replace }),
  useSearchParams: () => navigation.params,
}));
const auth = vi.hoisted(() => ({ setSession: vi.fn() }));
vi.mock("@/lib/auth-context", () => ({
  useAuth: () => ({ setSession: auth.setSession }),
  homePathForRole: (role: string) => (role === "candidate" ? "/portal" : "/org"),
}));

import { LoginFlow, LoginPageFlow } from "./login-flow";

const order: string[] = [];

function serve() {
  http.apiPost.mockImplementation(async (path: string) => {
    order.push(path);
    if (path === "/auth/firebase/session") {
      return { user: { id: "u1", role: "recruiter" }, capabilities: [] };
    }
    throw new Error(`unexpected POST ${path}`);
  });
  firebaseSdk.signInWithEmailAndPassword.mockImplementation(async () => {
    order.push("firebase");
    return { user: { getIdToken: async () => "id-token" } };
  });
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  order.length = 0;
  navigation.params = new URLSearchParams();
});

async function signIn() {
  fireEvent.change(screen.getByLabelText("Email address"), { target: { value: "a@b.test" } });
  fireEvent.change(screen.getByLabelText("Password"), { target: { value: "Secret-123" } });
  fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
  await waitFor(() => expect(auth.setSession).toHaveBeenCalled());
}

describe("the company sign-in page", () => {
  it("offers no Google and signs in with a company_login surface", async () => {
    serve();
    render(<LoginFlow surface="company" title="Sign in to your company workspace" />);
    expect(screen.queryByRole("button", { name: /google/i })).toBeNull();
    expect(screen.queryByLabelText("Security check")).toBeNull();

    await signIn();
    expect(order.indexOf("firebase")).toBeLessThan(order.indexOf("/auth/firebase/session"));
    expect(http.apiPost).toHaveBeenCalledWith("/auth/firebase/session", {
      id_token: "id-token",
      captcha_purpose: "company_login",
    });
    expect(navigation.replace).toHaveBeenCalledWith("/org");
  });
});

describe("the candidate sign-in page", () => {
  it("offers Google, links to the company page, and signs in with a candidate_login surface", async () => {
    serve();
    render(<LoginPageFlow />);
    expect(screen.queryByLabelText("Security check")).toBeNull();
    expect(screen.getByRole("button", { name: /continue with google/i })).toBeTruthy();
    expect(screen.getByRole("link", { name: "Company login" }).getAttribute("href")).toBe(
      "/company/login",
    );
    await signIn();
    expect(http.apiPost).toHaveBeenCalledWith(
      "/auth/firebase/session",
      expect.objectContaining({ captcha_purpose: "candidate_login" }),
    );
  });

  it("opens Google from the click, with no security check", async () => {
    serve();
    firebaseSdk.signInWithPopup.mockImplementation(async () => {
      order.push("popup");
      return { user: { getIdToken: async () => "id-token" } };
    });
    render(<LoginPageFlow />);
    fireEvent.click(screen.getByRole("button", { name: /continue with google/i }));
    await waitFor(() => expect(firebaseSdk.signInWithPopup).toHaveBeenCalled());
    expect(screen.queryByLabelText("Security check")).toBeNull();
  });

  it("uses the Provider purpose behind ?portal=owner", async () => {
    serve();
    navigation.params = new URLSearchParams("portal=owner");
    render(<LoginPageFlow />);
    await signIn();
    expect(http.apiPost).toHaveBeenCalledWith("/auth/firebase/session", {
      id_token: "id-token",
      captcha_purpose: "provider_login",
    });
  });
});
