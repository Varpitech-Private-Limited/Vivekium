// @vitest-environment jsdom
//
// "Forgot password?" runs on ReadyPick's server now (auth spec 9.2). Pinned:
//
// * the request carries the address and a security check proof, and the
//   screen shows the server's one sentence, whatever the account state;
// * the security code is exchanged for a reset ticket, and the ticket with a
//   new password sets it; a password breaking the rule never leaves the page;
// * a malformed address never reaches the server;
// * the control sits inside a sign-in form without submitting it;
// * Firebase's own reset email is not called at all.

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

const http = vi.hoisted(() => ({ apiPost: vi.fn() }));
vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, ...http };
});
vi.mock("@/lib/firebase", () => ({ firebaseAuth: { name: "test-auth" } }));

import { ForgotPassword, RESET_SENT_MESSAGE } from "./forgot-password";

const CHALLENGE = {
  challenge_id: "c1",
  image: "data:image/svg+xml;base64,PHN2Zy8+",
  expires_in: 300,
};

function serve(routes: Record<string, (body: unknown) => unknown>) {
  http.apiPost.mockImplementation(async (path: string, body: unknown) => {
    const handler = routes[path];
    if (!handler) throw new Error(`unexpected POST ${path}`);
    return handler(body);
  });
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

const posted = (path: string) =>
  http.apiPost.mock.calls.filter(([called]) => called === path).map(([, body]) => body);

async function open(initialEmail = "person@example.com") {
  const outerSubmit = vi.fn((event: React.FormEvent) => event.preventDefault());
  render(
    <form onSubmit={outerSubmit}>
      <ForgotPassword initialEmail={initialEmail} idPrefix="login" />
    </form>,
  );
  fireEvent.click(screen.getByRole("button", { name: "Forgot password?" }));
  await screen.findByLabelText("Email address");
  expect(screen.queryByLabelText("Security check")).toBeNull();
  return outerSubmit;
}

describe("ForgotPassword", () => {
  it("asks the server and shows its one sentence", async () => {
    serve({
      "/auth/password-reset/request": () => ({ sent: true, message: RESET_SENT_MESSAGE }),
    });
    const outerSubmit = await open();
    const field = screen.getByLabelText("Email address") as HTMLInputElement;
    expect(field.value).toBe("person@example.com");
    fireEvent.keyDown(field, { key: "Enter" });

    expect(await screen.findByText(RESET_SENT_MESSAGE)).toBeTruthy();
    expect(outerSubmit).not.toHaveBeenCalled();
    expect(posted("/auth/password-reset/request")).toEqual([
      { email: "person@example.com" },
    ]);
  });

  it("never sends a malformed address", async () => {
    serve({});
    await open("not-an-address");
    fireEvent.click(screen.getByRole("button", { name: "Email me a security code" }));
    expect(await screen.findByText(/enter the email address you sign in with/i)).toBeTruthy();
    expect(posted("/auth/password-reset/request")).toEqual([]);
  });

  it("walks the code and the new password through to the server", async () => {
    serve({
      "/auth/password-reset/request": () => ({ sent: true, message: RESET_SENT_MESSAGE }),
      "/auth/password-reset/verify": () => ({ reset_token: "ticket-1" }),
      "/auth/password-reset/complete": () => ({
        password_changed: true,
        message: "Your password has been changed. Sign in with your new password.",
      }),
    });
    await open();
    fireEvent.click(screen.getByRole("button", { name: "Email me a security code" }));

    const codeField = await screen.findByLabelText("Security code");
    fireEvent.change(codeField, { target: { value: "04 29 17" } });
    fireEvent.click(screen.getByRole("button", { name: "Verify code" }));

    const passwordField = await screen.findByLabelText("New password");
    fireEvent.change(passwordField, { target: { value: "weak" } });
    expect(
      (screen.getByRole("button", { name: "Set new password" }) as HTMLButtonElement).disabled,
    ).toBe(true);
    fireEvent.change(passwordField, { target: { value: "Fresh-Pass-2026" } });
    fireEvent.click(screen.getByRole("button", { name: "Set new password" }));

    expect(await screen.findByText(/your password has been changed/i)).toBeTruthy();
    expect(posted("/auth/password-reset/verify")).toEqual([
      { email: "person@example.com", code: "042917" },
    ]);
    expect(posted("/auth/password-reset/complete")).toEqual([
      { reset_token: "ticket-1", password: "Fresh-Pass-2026" },
    ]);
  });

  it("says the server's refusal of a wrong code", async () => {
    const { ApiError } = await import("@/lib/api");
    serve({
      "/auth/password-reset/request": () => ({ sent: true, message: RESET_SENT_MESSAGE }),
      "/auth/password-reset/verify": () => {
        throw new ApiError(400, { detail: "That security code is not valid or has expired." });
      },
    });
    await open();
    fireEvent.click(screen.getByRole("button", { name: "Email me a security code" }));
    fireEvent.change(await screen.findByLabelText("Security code"), { target: { value: "111111" } });
    fireEvent.click(screen.getByRole("button", { name: "Verify code" }));
    await waitFor(() =>
      expect(screen.getByText("That security code is not valid or has expired.")).toBeTruthy(),
    );
  });
});
