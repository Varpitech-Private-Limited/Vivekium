"use client";

// Recruiter-facing Project Evidence (Project Evidence Intelligence,
// 2026-09-01). Renders DERIVED intelligence only: the candidate's claim and
// the system's observed evidence are labelled separately, strength is a word
// (never a number), and there is no original-file download because the
// product does not retain originals. A candidate with no projects is a
// normal, unpunished state and reads as one. "No project evidence submitted"
// is reserved for a candidate with ZERO submissions: a project that is queued
// or being analysed is shown as exactly that, never as absent.

import * as React from "react";
import { FolderGit2 } from "lucide-react";

import { apiGet } from "@/lib/api";
import { Badge } from "@/components/ui/badge";
import { Separator } from "@/components/ui/separator";

type ClaimAssessment = {
  claim: string;
  supporting_evidence: string[];
  limiting_evidence: string[];
  assessment: string;
};

type ProjectEvidence = {
  id: string;
  name: string;
  status: string;
  submission_kind: string;
  repository_url: string | null;
  domains: string[];
  candidate_description: string;
  technologies: string[];
  observed_evidence: string[];
  documentation_evidence: string[];
  claim_assessments: ClaimAssessment[];
  synthesis: string | null;
  evidence_strength: string | null;
  potential_gaps: string[];
  validation_areas: string[];
  uncertainties: string[];
  processed_at: string | null;
};

type Submission = {
  id: string;
  name: string;
  state: "queued" | "analysing" | "ready" | "summary_only" | "failed";
  status_detail: string | null;
  submitted_at: string | null;
  updated_at: string | null;
  processed_at: string | null;
};

export type SubmissionCounts = {
  submitted: number;
  queued: number;
  analysing: number;
  ready: number;
  summary_only: number;
  failed: number;
};

type Overview = {
  projects: ProjectEvidence[];
  submissions: Submission[];
  counts: SubmissionCounts;
};

const POLL_MS = 20000;

const STATE_LABEL: Record<Submission["state"], string> = {
  queued: "Analysis queued",
  analysing: "Being analysed",
  ready: "Evidence ready",
  summary_only: "Summary only",
  failed: "Could not be analysed",
};

/** "3 projects submitted; analysis queued": the headline a reviewer reads first. */
export function submissionSummary(counts: SubmissionCounts): string {
  const noun = counts.submitted === 1 ? "project" : "projects";
  const parts = [`${counts.submitted} ${noun} submitted`];
  if (counts.queued > 0) parts.push("analysis queued");
  if (counts.analysing > 0) parts.push("analysis in progress");
  const withEvidence = counts.ready + counts.summary_only;
  if (withEvidence > 0) parts.push(`${withEvidence} with evidence ready`);
  if (counts.failed > 0) parts.push(`${counts.failed} could not be analysed`);
  return parts.join("; ");
}

function formatWhen(iso: string | null): string | null {
  return iso ? new Date(iso).toLocaleString() : null;
}

export function ProjectEvidencePanel({ candidateId }: { candidateId: string }) {
  const [data, setData] = React.useState<Overview | null>(null);
  const [loading, setLoading] = React.useState(true);
  const [failed, setFailed] = React.useState(false);

  const waiting = data
    ? data.counts.queued > 0 || data.counts.analysing > 0
    : false;

  const load = React.useCallback(
    (isCancelled: () => boolean) =>
      apiGet<Overview>(`/candidates/${candidateId}/project-evidence`)
        .then((res) => {
          if (isCancelled()) return;
          setData(res);
          setFailed(false);
        })
        .catch(() => {
          if (!isCancelled()) setFailed(true);
        })
        .finally(() => {
          if (!isCancelled()) setLoading(false);
        }),
    [candidateId]
  );

  React.useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setData(null);
    void load(() => cancelled);
    return () => {
      cancelled = true;
    };
  }, [load]);

  // While analysis is still pending, re-read the status; stop on any settled
  // state, and skip the poll while the tab is hidden.
  React.useEffect(() => {
    if (!waiting) return;
    let cancelled = false;
    const timer = window.setInterval(() => {
      if (document.visibilityState === "visible") void load(() => cancelled);
    }, POLL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [waiting, load]);

  if (loading) {
    return (
      <p role="status" className="text-sm">
        Loading project evidence
      </p>
    );
  }
  if (!data) {
    return (
      <div className="rounded-md border border-dashed p-5" role="alert">
        <p className="font-medium">Project status could not be loaded</p>
        <p className="mt-1 text-sm">
          Refresh to try again. This does not mean the candidate has no projects.
        </p>
      </div>
    );
  }
  const { projects, submissions, counts } = data;
  if (counts.submitted === 0) {
    return (
      <div className="rounded-md border border-dashed p-5">
        <p className="font-medium">No project evidence submitted</p>
        <p className="mt-1 text-sm">
          This candidate has not added projects to their profile. Their
          resume, validation answers and assessment remain the evidence base.
        </p>
      </div>
    );
  }

  const evidenceIds = new Set(projects.map((project) => project.id));
  const notes = submissions.filter(
    (submission) => !evidenceIds.has(submission.id) || submission.state === "summary_only"
  );

  return (
    <div className="space-y-4">
      <div className="rounded-md border border-border p-4" role="status">
        <p className="text-sm font-medium">{submissionSummary(counts)}</p>
        {failed ? (
          <p className="mt-1 text-xs">
            The latest refresh failed; this status may be out of date.
          </p>
        ) : null}
        {notes.length > 0 ? (
          <ul className="mt-2 space-y-2">
            {notes.map((submission) => (
              <li key={submission.id} className="text-sm">
                <span className="font-medium">{submission.name}</span>
                {": "}
                {STATE_LABEL[submission.state]}
                {submission.status_detail ? `. ${submission.status_detail}` : ""}
                {formatWhen(submission.updated_at ?? submission.submitted_at) ? (
                  <span className="block text-xs">
                    Last updated {formatWhen(submission.updated_at ?? submission.submitted_at)}
                  </span>
                ) : null}
              </li>
            ))}
          </ul>
        ) : null}
      </div>
      {projects.map((project) => (
        <div key={project.id} className="rounded-md border border-border p-4">
          <div className="flex flex-wrap items-start justify-between gap-2">
            <div className="flex min-w-0 items-center gap-2">
              <FolderGit2 className="h-4 w-4 shrink-0" aria-hidden="true" />
              <h4 className="truncate text-sm font-semibold">{project.name}</h4>
            </div>
            {project.evidence_strength ? (
              <Badge
                variant="outline"
                className="border-teal-600/40 text-teal-700 dark:text-teal-300"
              >
                {project.evidence_strength} evidence
              </Badge>
            ) : (
              <Badge variant="secondary">Evidence summary only</Badge>
            )}
          </div>

          {project.domains.length > 0 ? (
            <p className="mt-1 text-xs">{project.domains.join(" / ")}</p>
          ) : null}

          <div className="mt-3 space-y-3 text-sm">
            <div>
              <p className="type-eyebrow">
                Candidate&apos;s description (their claim)
              </p>
              <p className="mt-1">{project.candidate_description}</p>
            </div>

            {project.technologies.length > 0 ? (
              <div>
                <p className="type-eyebrow">
                  Observed stack
                </p>
                <div className="mt-1 flex flex-wrap gap-1.5">
                  {project.technologies.map((tech) => (
                    <Badge key={tech} variant="secondary">
                      {tech}
                    </Badge>
                  ))}
                </div>
              </div>
            ) : null}

            {project.observed_evidence.length > 0 ? (
              <div>
                <p className="type-eyebrow">
                  System-observed evidence
                </p>
                <ul className="mt-1 list-disc space-y-0.5 pl-5">
                  {project.observed_evidence.map((item) => (
                    <li key={item}>{item}</li>
                  ))}
                </ul>
              </div>
            ) : null}

            {project.synthesis ? (
              <div>
                <p className="type-eyebrow">
                  Assessment
                </p>
                <p className="mt-1">{project.synthesis}</p>
              </div>
            ) : null}

            {project.claim_assessments.length > 0 ? (
              <div>
                <p className="type-eyebrow">
                  Claims against evidence
                </p>
                <div className="mt-1 space-y-2">
                  {project.claim_assessments.map((entry) => (
                    <div
                      key={entry.claim}
                      className="rounded-md border border-border p-2"
                    >
                      <p className="font-medium">{entry.claim}</p>
                      <p className="mt-0.5 text-xs">
                        Assessment: {entry.assessment}
                      </p>
                      {entry.supporting_evidence.length > 0 ? (
                        <p className="mt-0.5 text-xs">
                          Supported by: {entry.supporting_evidence.join("; ")}
                        </p>
                      ) : null}
                      {entry.limiting_evidence.length > 0 ? (
                        <p className="mt-0.5 text-xs">
                          Limited by: {entry.limiting_evidence.join("; ")}
                        </p>
                      ) : null}
                    </div>
                  ))}
                </div>
              </div>
            ) : null}

            {project.potential_gaps.length > 0 ? (
              <div>
                <p className="type-eyebrow">
                  Evidence gaps
                </p>
                <ul className="mt-1 list-disc space-y-0.5 pl-5">
                  {project.potential_gaps.map((gap) => (
                    <li key={gap}>{gap}</li>
                  ))}
                </ul>
              </div>
            ) : null}

            {project.validation_areas.length > 0 ? (
              <div>
                <p className="type-eyebrow">
                  Worth validating in interview
                </p>
                <ul className="mt-1 list-disc space-y-0.5 pl-5">
                  {project.validation_areas.map((area) => (
                    <li key={area}>{area}</li>
                  ))}
                </ul>
              </div>
            ) : null}

            {project.uncertainties.length > 0 ? (
              <>
                <Separator />
                <p className="text-xs">
                  Processing notes: {project.uncertainties.join(" ")}
                </p>
              </>
            ) : null}
          </div>
        </div>
      ))}
    </div>
  );
}
