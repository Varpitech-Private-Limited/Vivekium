// @vitest-environment jsdom
//
// Joining from an invitation (auth spec 11.4). Pinned: no Google; creating the
// password posts to the server's setup route WITHOUT an email (the address is
// the invitation's); an address that already has a sign-in is moved to "I
// already have one" with the server's sentence.

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

const http = vi.hoisted(() => ({ apiPost: vi.fn(), apiGet: vi.fn() }));
vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, ...http };
});
vi.mock("firebase/auth", () => ({ signInWithEmailAndPassword: vi.fn() }));
vi.mock("@/lib/firebase", () => ({ firebaseAuth: {} }));
vi.mock("next/navigation", () => ({ useRouter: () => ({ replace: vi.fn() }) }));
const auth = vi.hoisted(() => ({ setSession: vi.fn() }));
vi.mock("@/lib/auth-context", () => ({ useAuth: () => ({ setSession: auth.setSession }) }));

import { ApiError } from "@/lib/api";
import { JoinFlow } from "./join-flow";

const INVITE = {
  email: "asha@acme.test",
  full_name: "Asha Rao",
  role: "recruiter",
  company_name: "Acme",
  invited_by_name: "Ravi",
  expires_at: "2026-10-10T00:00:00Z",
  status: "pending",
};

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function serve(setup: () => unknown) {
  http.apiGet.mockResolvedValue(INVITE);
  http.apiPost.mockImplementation(async (path: string) => {
    if (path.endsWith("/setup-password")) return setup();
    throw new Error(`unexpected POST ${path}`);
  });
}

async function fillAndSubmit() {
  await screen.findByLabelText("Create a password");
  expect(screen.queryByLabelText("Security check")).toBeNull();
  fireEvent.change(screen.getByLabelText("Create a password"), { target: { value: "Join-Team-7" } });
  fireEvent.click(screen.getByRole("button", { name: "Create password and join" }));
}

describe("JoinFlow", () => {
  it("offers no Google and sets the password on the server for the invited address", async () => {
    serve(() => ({ user: { id: "u1", role: "recruiter" }, capabilities: [] }));
    render(<JoinFlow token="tok-1" />);
    await screen.findByText("asha@acme.test");
    expect(screen.queryByRole("button", { name: /google/i })).toBeNull();
    await fillAndSubmit();
    await waitFor(() => expect(auth.setSession).toHaveBeenCalled());
    const [, body] = http.apiPost.mock.calls.find(([path]) =>
      String(path).endsWith("/setup-password"),
    )!;
    expect(http.apiPost).toHaveBeenCalledWith(
      "/companies/invites/tok-1/setup-password",
      expect.anything(),
    );
    expect(body).toEqual({ password: "Join-Team-7", full_name: "Asha Rao" });
    expect(await screen.findByText("You joined Acme")).toBeTruthy();
  });

  it("moves an existing sign-in to the sign-in tab with the server's sentence", async () => {
    const sentence =
      "This email address already has a ReadyPick sign-in. Sign in with your existing password to join.";
    serve(() => {
      throw new ApiError(409, { detail: sentence });
    });
    render(<JoinFlow token="tok-2" />);
    await screen.findByText("asha@acme.test");
    await fillAndSubmit();
    expect(await screen.findByText(sentence)).toBeTruthy();
    expect(screen.getByRole("tab", { name: "I already have one" }).getAttribute("aria-selected")).toBe(
      "true",
    );
  });
});
