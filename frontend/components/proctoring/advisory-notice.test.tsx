// @vitest-environment jsdom
// The sound-level reminder is a status line, not a warning: it can be ignored,
// dismissed, and clears itself.

import * as React from "react";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ADVISORY_VISIBLE_MS, AdvisoryNotice } from "./advisory-notice";

const MESSAGE = "Sustained high sound level detected; please check your environment.";

beforeEach(() => vi.useFakeTimers());
afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe("the advisory notice", () => {
  it("renders nothing without a message", () => {
    render(<AdvisoryNotice message={null} onDismiss={() => undefined} />);
    expect(screen.queryByRole("status")).toBeNull();
  });

  it("shows the hedged sentence as a polite status, not a dialog", () => {
    render(<AdvisoryNotice message={MESSAGE} onDismiss={() => undefined} />);
    expect(screen.getByRole("status").textContent).toContain(MESSAGE);
    expect(screen.queryByRole("alertdialog")).toBeNull();
  });

  it("is dismissed by the candidate", () => {
    const onDismiss = vi.fn();
    render(<AdvisoryNotice message={MESSAGE} onDismiss={onDismiss} />);
    fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));
    expect(onDismiss).toHaveBeenCalledTimes(1);
  });

  it("clears itself when ignored", () => {
    const onDismiss = vi.fn();
    render(<AdvisoryNotice message={MESSAGE} onDismiss={onDismiss} />);
    act(() => {
      vi.advanceTimersByTime(ADVISORY_VISIBLE_MS);
    });
    expect(onDismiss).toHaveBeenCalledTimes(1);
  });
});
