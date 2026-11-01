// @vitest-environment jsdom

/**
 * The job status component's honesty rules: no invented estimate, no bar
 * without a measured step, polling that backs off and stops, and a retry only
 * when the server offers one.
 */
import * as React from "react";
import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  JobStatus,
  POLL_MAX_MS,
  POLL_START_MS,
  estimateText,
  lastUpdatedText,
  nextPollDelayMs,
  type JobStatusData,
} from "./job-status";

const apiGet = vi.fn();
const apiPost = vi.fn();
vi.mock("@/lib/api", () => ({
  ApiError: class ApiError extends Error {
    status: number;
    constructor(status: number) {
      super("api");
      this.status = status;
    }
  },
  apiGet: (path: string) => apiGet(path),
  apiPost: (path: string) => apiPost(path),
}));

function job(over: Partial<JobStatusData> = {}): JobStatusData {
  return {
    job_id: "j1",
    kind: "pickready.process_candidate_project",
    resource_type: "candidate_project",
    resource_id: "p1",
    state: "running",
    terminal: false,
    stage: "Reading the repository",
    message: "Working on it.",
    queued_at: "2026-11-01T10:00:00Z",
    started_at: "2026-11-01T10:00:05Z",
    heartbeat_at: "2026-11-01T10:00:30Z",
    updated_at: new Date().toISOString(),
    completed_at: null,
    attempts: 1,
    failure_class: null,
    next_retry_at: null,
    percent: null,
    estimate: null,
    can_retry: false,
    ...over,
  };
}

async function flush() {
  await act(async () => {
    await Promise.resolve();
  });
}

beforeEach(() => {
  vi.useFakeTimers();
  apiGet.mockReset();
  apiPost.mockReset();
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe("pure helpers", () => {
  it("backs off from 3 s to a 30 s ceiling", () => {
    expect(nextPollDelayMs(0)).toBe(POLL_START_MS);
    expect(nextPollDelayMs(1)).toBeGreaterThan(POLL_START_MS);
    expect(nextPollDelayMs(50)).toBe(POLL_MAX_MS);
  });

  it("says the estimate is unavailable rather than inventing one", () => {
    expect(estimateText(job())).toBe("Time estimate unavailable");
    expect(estimateText(job({ terminal: true, state: "succeeded" }))).toBe(
      "Time estimate unavailable"
    );
    expect(
      estimateText(job({ estimate: { low_seconds: 60, high_seconds: 180, samples: 8 } }))
    ).toBe("Estimated 1 minute to 3 minutes left, from 8 recent runs");
  });

  it("reports the last update in plain words", () => {
    const now = Date.parse("2026-11-01T10:10:00Z");
    expect(lastUpdatedText("2026-11-01T10:09:58Z", now)).toBe("Last updated just now");
    expect(lastUpdatedText("2026-11-01T10:05:00Z", now)).toBe("Last updated 5 minutes ago");
  });
});

describe("JobStatus", () => {
  it("shows the stage, last updated and no progress bar without a measured step", async () => {
    apiGet.mockResolvedValue(job());
    render(<JobStatus resourceType="candidate_project" resourceId="p1" />);
    await flush();
    expect(screen.getByText("Reading the repository")).toBeTruthy();
    expect(screen.getByText(/Last updated/)).toBeTruthy();
    expect(screen.getByText(/Time estimate unavailable/)).toBeTruthy();
    expect(document.querySelector("progress")).toBeNull();
  });

  it("draws a bar only when the server reports a percentage", async () => {
    apiGet.mockResolvedValue(job({ percent: 40 }));
    render(<JobStatus resourceType="candidate_project" resourceId="p1" />);
    await flush();
    expect(document.querySelector("progress")?.getAttribute("value")).toBe("40");
  });

  it("renders nothing for a resource that never had a job", async () => {
    const { ApiError } = await import("@/lib/api");
    apiGet.mockRejectedValue(new ApiError(404, null));
    const { container } = render(<JobStatus resourceType="candidate_project" resourceId="p1" />);
    await flush();
    expect(container.textContent).toBe("");
    await act(async () => {
      vi.advanceTimersByTime(60000);
    });
    expect(apiGet).toHaveBeenCalledTimes(1);
  });

  it("stops polling at a terminal state and tells the owner once", async () => {
    const onTerminal = vi.fn();
    apiGet
      .mockResolvedValueOnce(job())
      .mockResolvedValue(job({ state: "succeeded", terminal: true, message: "Finished." }));
    render(<JobStatus resourceType="candidate_project" resourceId="p1" onTerminal={onTerminal} />);
    await flush();
    await act(async () => {
      vi.advanceTimersByTime(POLL_START_MS + 10);
    });
    await flush();
    expect(onTerminal).toHaveBeenCalledTimes(1);
    const calls = apiGet.mock.calls.length;
    await act(async () => {
      vi.advanceTimersByTime(120000);
    });
    expect(apiGet.mock.calls.length).toBe(calls);
  });

  it("offers Try again only for a failed job the server says can be retried", async () => {
    apiGet.mockResolvedValue(
      job({ state: "failed", terminal: true, message: "The worker stopped before it finished.", can_retry: true })
    );
    render(<JobStatus resourceType="candidate_project" resourceId="p1" />);
    await flush();
    expect(screen.getByRole("button", { name: "Try again" })).toBeTruthy();
    cleanup();

    apiGet.mockResolvedValue(
      job({ state: "failed", terminal: true, message: "This did not complete.", can_retry: false })
    );
    render(<JobStatus resourceType="candidate_project" resourceId="p1" />);
    await flush();
    expect(screen.queryByRole("button", { name: "Try again" })).toBeNull();
  });
});
