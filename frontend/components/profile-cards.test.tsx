// @vitest-environment jsdom
//
// The My Profile cards for the candidate's own records: keep my profile and my
// consents. (The verification-documents card was retired in the 2026-11
// production-fix pass; there is no document upload on the profile.) What is
// pinned:
//
// * keep my profile: the server's message and dates are what renders, reading
//   renews nothing, and the button posts the renewal then re-reads the state;
// * my consents: the whole catalogue with given and not given, and the
//   history's verbatim wording, with no version or digest on screen.

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

const http = vi.hoisted(() => ({
  apiGet: vi.fn(),
  apiPost: vi.fn(),
  apiDelete: vi.fn(),
}));
vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, ...http };
});
const toast = vi.hoisted(() => vi.fn());
vi.mock("@/components/ui/toast", () => ({ useToast: () => ({ toast }) }));

import { ApiError } from "@/lib/api";
import { ConsentRenewalCard } from "./consent-renewal-card";
import { ConsentHistoryCard } from "./consent-history-card";

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("keep my profile", () => {
  const renewal = {
    consented_at: "2026-03-01T00:00:00+00:00",
    renewal_due_at: "2026-09-01T00:00:00+00:00",
    stage: "reminder_due",
    renewal_needed: true,
    message: "Your profile is due for renewal. Confirm below to keep it.",
  };

  it("renders the server's message and renews only when asked", async () => {
    http.apiGet.mockResolvedValue(renewal);
    http.apiPost.mockResolvedValue({
      renewed: true,
      renewed_at: "2026-09-24T10:00:00+00:00",
      message: "Thank you. Your profile will be kept.",
    });
    render(<ConsentRenewalCard />);
    expect(await screen.findByText(renewal.message)).toBeTruthy();
    expect(http.apiPost).not.toHaveBeenCalled();

    http.apiGet.mockResolvedValue({ ...renewal, renewal_needed: false, message: "Your profile is active." });
    fireEvent.click(screen.getByRole("button", { name: "Keep my profile" }));
    await waitFor(() => expect(http.apiPost).toHaveBeenCalledWith("/portal/me/consent/renew"));
    expect(await screen.findByText("Thank you. Your profile will be kept.")).toBeTruthy();
    expect(await screen.findByText("Your profile is active.")).toBeTruthy();
    expect(http.apiGet).toHaveBeenCalledTimes(2);
  });
});

describe("my consents", () => {
  it("shows the whole catalogue and the verbatim history, with no version or digest", async () => {
    http.apiGet.mockResolvedValue({
      items: [
        {
          key: "privacy",
          stage: "A",
          text: "I agree to the privacy policy.",
          version: "2026-09-01",
          required: true,
          consented_at: "2026-09-02T00:00:00+00:00",
          consented_version: "2026-08-01",
          wording_current: false,
        },
        {
          key: "cross_employer_reuse",
          stage: "B",
          text: "Evidence may be reused across employers.",
          version: "2026-09-01",
          required: false,
          consented_at: null,
          consented_version: null,
          wording_current: false,
        },
      ],
      history: [
        {
          key: "privacy",
          stage: "A",
          source: "registration",
          version: "2026-08-01",
          text: "I agreed to the old privacy wording.",
          text_sha256: "abc123digest",
          recorded_at: "2026-09-02T00:00:00+00:00",
        },
        {
          key: "privacy",
          stage: "A",
          source: "registration",
          version: null,
          text: null,
          text_sha256: null,
          recorded_at: "2026-01-02T00:00:00+00:00",
        },
      ],
    });
    const { container } = render(<ConsentHistoryCard />);
    expect(await screen.findByText("I agree to the privacy policy.")).toBeTruthy();
    expect(screen.getByText("Evidence may be reused across employers.")).toBeTruthy();
    expect(screen.getByText(/not agreed/i)).toBeTruthy();
    expect(screen.getByText(/wording has changed since you agreed/i)).toBeTruthy();
    expect(screen.getByText("I agreed to the old privacy wording.")).toBeTruthy();
    expect(screen.getByText(/exact wording was not recorded/i)).toBeTruthy();
    expect(container.textContent).not.toContain("abc123digest");
    expect(container.textContent).not.toContain("2026-08-01");
  });

  it("says a failed load rather than showing an empty record", async () => {
    http.apiGet.mockRejectedValue(new ApiError(500, { detail: "Internal error" }));
    render(<ConsentHistoryCard />);
    expect(await screen.findByText(/could not be loaded/i)).toBeTruthy();
  });
});
