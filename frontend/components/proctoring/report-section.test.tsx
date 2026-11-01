// @vitest-environment jsdom
//
// The Proctoring Report inside the PRISM Report (proctoring spec section 7).
//
// Three properties, and the last is the one this file exists for:
//
//  1. It is the LAST section. It is informational, it moves no grade, and it
//     sits after everything that does.
//  2. A report that does not exist says so in one line rather than rendering
//     an empty heading, because a blank section reads as a clean session.
//  3. NO ICON, NO COLOUR CODE, NO SEVERITY COLUMN, and none of the internal
//     vocabulary (spec 7.1). Importance comes from what is said and where it
//     sits; a tinted chip beside a finding would state a judgement the system
//     is explicitly not entitled to make.

import * as React from "react";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("recharts", () => ({
  Legend: () => null,
  PolarAngleAxis: () => null,
  PolarRadiusAxis: () => null,
  Radar: () => null,
  RadarChart: ({ children }: { children?: React.ReactNode }) => <div>{children}</div>,
  ResponsiveContainer: ({ children }: { children?: React.ReactNode }) => <div>{children}</div>,
}));

import {
  FunctionalSkillsReportView,
  PROCTORING_ABSENT,
  PROCTORING_NOTE,
  PROCTORING_TITLE,
  REPORT_SECTION_ORDER,
  type FunctionalReport,
} from "@/components/functional-skills-report";
import type { ProctoringReport } from "@/lib/types";

/** Every word the specification forbids in a recruiter's view (7.1). */
const FORBIDDEN = [
  "strike",
  "tier",
  "violation",
  "anomaly",
  "signal",
  "confidence",
  "threshold",
  "severity",
];

afterEach(cleanup);

function proctoring(): ProctoringReport {
  return {
    candidate: "Fixture Candidate",
    assessment: "Platform Engineer",
    date_line: "2 September 2026, 10:32 to 11:41",
    outcome: "Assessment completed. The candidate was warned once during the session.",
    summary:
      "The candidate left the assessment screen twice, briefly. Nothing else of note was seen, "
      + "and they finished the assessment in full.",
    findings: {
      screen_browser: ["Left the assessment screen twice, for about ten seconds each time."],
      camera: ["No issues detected."],
      audio: ["No issues detected."],
      answer_patterns: ["No issues detected."],
    },
    activity_log: [
      {
        time: "10:41",
        what_happened: "Left the assessment screen",
        how_long: "About ten seconds",
        what_the_system_did: "Showed the candidate a warning",
      },
    ],
    closing:
      "This report reflects only what the system detected during the assessment. It does not "
      + "affect this candidate's score or ranking.",
    monitoring_was_incomplete: false,
    generated_at: "2026-09-02T11:45:00Z",
  };
}

function report(overrides: Partial<FunctionalReport> = {}): FunctionalReport {
  return {
    id: "report-1",
    job_candidate_link_id: "link-1",
    grade: "non_managerial",
    ai_score: [],
    overall_grade: "Matching",
    overall_summary: "Consistent evidence of ownership across the stack.",
    must_have: [],
    nice_to_have: [],
    behavioural: [],
    validation: { fields: [] },
    synthesized_at: "2026-09-02T11:45:00Z",
    ...overrides,
  };
}

describe("the proctoring section", () => {
  it("is the last section of the report", () => {
    expect(REPORT_SECTION_ORDER[REPORT_SECTION_ORDER.length - 1]).toBe("proctoring");
  });

  it("states in one line that no report exists, rather than rendering a blank", () => {
    render(<FunctionalSkillsReportView report={report()} />);
    expect(screen.getByRole("heading", { name: PROCTORING_TITLE })).toBeTruthy();
    expect(screen.getByText(PROCTORING_ABSENT)).toBeTruthy();
  });

  it("renders the outcome, the summary, every findings group and the activity log", () => {
    render(<FunctionalSkillsReportView report={report({ proctoring: proctoring() })} />);
    const section = screen.getByLabelText("Proctoring Report");
    expect(section.textContent).toContain("Assessment completed.");
    expect(section.textContent).toContain("2 September 2026, 10:32 to 11:41");
    for (const heading of [
      "Screen & Browser Activity",
      "Camera Monitoring",
      "Audio Monitoring",
      "Answer Pattern Analysis",
    ]) {
      expect(screen.getByText(heading), heading).toBeTruthy();
    }
    expect(screen.getByText("10:41")).toBeTruthy();
    expect(screen.getByText("Showed the candidate a warning")).toBeTruthy();
  });

  it("says it is informational and affects no score", () => {
    render(<FunctionalSkillsReportView report={report({ proctoring: proctoring() })} />);
    expect(screen.getByText(PROCTORING_NOTE)).toBeTruthy();
    // The same words the PDF prints, so a recruiter reading one and
    // forwarding the other reads the same disclaimer twice.
    expect(PROCTORING_NOTE).toContain("does not affect this candidate's score or ranking");
  });

  it("carries no icon, no severity column and none of the internal vocabulary", () => {
    render(<FunctionalSkillsReportView report={report({ proctoring: proctoring() })} />);
    const section = screen.getByLabelText("Proctoring Report");
    expect(section.querySelector("svg")).toBeNull();
    const headers = [...section.querySelectorAll("th")].map((cell) => cell.textContent);
    expect(headers).toEqual(["Time", "What happened", "How long", "What the system did"]);
    const text = (section.textContent ?? "").toLowerCase();
    for (const word of FORBIDDEN) {
      expect(text.includes(word), word).toBe(false);
    }
  });

  it("prints no event identifier and no measurement", () => {
    // The identifiers are internal and the report speaks in sentences; the
    // only digits allowed are clock times.
    render(<FunctionalSkillsReportView report={report({ proctoring: proctoring() })} />);
    const text = screen.getByLabelText("Proctoring Report").textContent ?? "";
    expect(text).not.toMatch(/[A-Z]{2,}_[A-Z_]+/);
    expect(text).not.toMatch(/\d+\s*ms\b/);
    expect(text).not.toMatch(/\d+\s*%/);
  });
});

describe("the proctoring section says WHY there is no report", () => {
  const states = [
    ["no_session", "No proctoring session was recorded for this assessment."],
    ["in_progress", "The monitored session has not ended yet."],
    ["generating", "The Proctoring Report is being prepared."],
    ["failed", "The Proctoring Report could not be produced yet."],
  ] as const;

  for (const [state, message] of states) {
    it(`shows the server's sentence for ${state}, never a clean-looking absence`, () => {
      render(
        <FunctionalSkillsReportView
          report={report({ proctoring: null, proctoring_state: { state, message } })}
        />
      );
      const section = screen.getByLabelText("Proctoring Report");
      expect(section.textContent).toContain(message);
      expect(section.textContent).not.toContain(PROCTORING_ABSENT);
      expect(section.textContent).not.toMatch(/no issues/i);
    });
  }

  it("puts the partial-monitoring sentence above the report it qualifies", () => {
    render(
      <FunctionalSkillsReportView
        report={report({
          proctoring: proctoring(),
          proctoring_state: {
            state: "partially_unavailable",
            message: "Monitoring was incomplete for part of this assessment.",
          },
        })}
      />
    );
    const section = screen.getByLabelText("Proctoring Report");
    expect(section.textContent).toContain("Monitoring was incomplete for part of this assessment.");
    expect(section.textContent).toContain("Assessment completed.");
  });
});
