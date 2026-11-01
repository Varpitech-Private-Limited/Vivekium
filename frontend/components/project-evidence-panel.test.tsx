// @vitest-environment jsdom
//
// The recruiter's Projects tab. Pinned: "No project evidence submitted" is for a
// candidate with ZERO submissions only; a queued or failed submission is shown
// as that, with its note; a failed load is an error, never "none".

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

const http = vi.hoisted(() => ({ apiGet: vi.fn() }));
vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, ...http };
});

import { ProjectEvidencePanel, submissionSummary } from "./project-evidence-panel";

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

const COUNTS = { submitted: 0, queued: 0, analysing: 0, ready: 0, summary_only: 0, failed: 0 };

function submission(name: string, state: string, detail: string | null = null) {
  return {
    id: name,
    name,
    state,
    status_detail: detail,
    submitted_at: "2026-10-30T10:00:00Z",
    updated_at: "2026-10-30T10:05:00Z",
    processed_at: null,
  };
}

describe("submissionSummary", () => {
  it("says three queued projects are submitted, not absent", () => {
    expect(submissionSummary({ ...COUNTS, submitted: 3, queued: 3 })).toBe(
      "3 projects submitted; analysis queued"
    );
  });
  it("counts evidence and failures", () => {
    expect(
      submissionSummary({ ...COUNTS, submitted: 3, ready: 1, summary_only: 1, failed: 1 })
    ).toBe("3 projects submitted; 2 with evidence ready; 1 could not be analysed");
  });
});

describe("ProjectEvidencePanel", () => {
  it("reserves the empty message for zero submissions", async () => {
    http.apiGet.mockResolvedValue({ projects: [], submissions: [], counts: COUNTS });
    render(<ProjectEvidencePanel candidateId="c1" />);
    expect(await screen.findByText("No project evidence submitted")).toBeTruthy();
  });

  it("shows queued submissions instead of the empty message", async () => {
    http.apiGet.mockResolvedValue({
      projects: [],
      submissions: [submission("OpenFrame Studio", "queued"), submission("Unfold", "queued")],
      counts: { ...COUNTS, submitted: 2, queued: 2 },
    });
    render(<ProjectEvidencePanel candidateId="c1" />);
    expect(await screen.findByText("2 projects submitted; analysis queued")).toBeTruthy();
    expect(screen.queryByText("No project evidence submitted")).toBeNull();
    expect(screen.getByText("OpenFrame Studio")).toBeTruthy();
  });

  it("shows a failure note", async () => {
    http.apiGet.mockResolvedValue({
      projects: [],
      submissions: [submission("Aegis", "failed", "We could not read the repository.")],
      counts: { ...COUNTS, submitted: 1, failed: 1 },
    });
    render(<ProjectEvidencePanel candidateId="c1" />);
    expect(await screen.findByText(/We could not read the repository\./)).toBeTruthy();
    expect(screen.getByText("1 project submitted; 1 could not be analysed")).toBeTruthy();
  });

  it("does not claim 'none' when the load fails", async () => {
    http.apiGet.mockRejectedValue(new Error("boom"));
    render(<ProjectEvidencePanel candidateId="c1" />);
    expect(await screen.findByText("Project status could not be loaded")).toBeTruthy();
    expect(screen.queryByText("No project evidence submitted")).toBeNull();
  });
});
