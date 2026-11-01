"use client";

import {
  Legend,
  PolarAngleAxis,
  PolarRadiusAxis,
  Radar,
  RadarChart,
  ResponsiveContainer,
} from "recharts";

import { Check, X } from "lucide-react";

import { RatingLabel } from "@/components/rating-label";
import { CitedRemark, type CitationsLoader } from "@/components/report-citations";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  RATING_GRADES,
  type ProctoringReport,
  type ProctoringState,
  type RatingGrade,
} from "@/lib/types";

export type { RatingGrade };

/** The word a rated row carries when its evaluation could not be completed. */
export const NOT_ASSESSED_WORD = "Not assessed";

export interface ReportDimension {
  name: string;
  description?: string | null;
  /** One of the four grades, or "Not assessed" when `status` is
   *  `not_assessed`. Never a number, a percentage, or a letter. */
  grade: RatingGrade | typeof NOT_ASSESSED_WORD;
  /** The row's stored status. Absent on a report written before it existed,
   *  which reads as graded. */
  status?: "graded" | "unanswered" | "not_assessed";
  /** The server's sentence beside a `not_assessed` row. */
  status_note?: string | null;
  /** What the job requires of this item. Null on legacy AI Match and
   *  technical rows. */
  required_level?: RatingGrade | null;
  remark: string;
  /** The server's marker for a remark a fixed template wrote because the
   *  writing model was unavailable. */
  remark_note?: string | null;
  /** The citation trail's marker for a remark its evidence did not clearly
   *  support. */
  support_note?: string | null;
  /**
   * EVIDENCE CONFIDENCE: "High", "Moderate", "Low" or "Insufficient evidence".
   *
   * How well corroborated the evidence behind the grade is, never a statement
   * about the candidate. The server derives it after scoring from the distinct
   * ORIGINATORS behind the line, so it cannot move the grade beside it.
   *
   * Absent on every report written before it existed, and the card renders
   * without the line rather than with an invented word: a report is immutable
   * and the evidence set an older one was written from is not reconstructable.
   */
  evidence_confidence?: string | null;
  /** The named sources it rests on. Empty on a report written before them. */
  evidence_sources?: string[];
}

export interface ValidationPoint {
  area: string;
  /** `confidence | borderline | contradiction`. Why the area is listed. */
  driver: string;
  confidence?: string | null;
  reason: string;
  /** One interview probe. Advisory, and never an advance or reject decision. */
  probe: string;
}

/**
 * Recommended Human Validation Points.
 *
 * DELIBERATELY NOT the Gap Analysis. That section is GRADE driven and
 * unbounded; this one is CONFIDENCE and CONTRADICTION driven and stops at
 * five. A Highly Matching item resting on the candidate's own unchecked
 * account is invisible to the first and is the first row of the second, which
 * is why both are rendered and why they are not adjacent on the page.
 */
export interface ValidationPoints {
  note?: string;
  points: ValidationPoint[];
  no_points_statement?: string | null;
}

export interface ClaimEvidenceEntry {
  area?: string;
  /** What the candidate asserted, in the ledger's normalised wording. */
  claim: string;
  /**
   * What was found when it was looked for. ABSENCE OF EVIDENCE IS NEVER
   * RENDERED AS THE CLAIM BEING FALSE: the server's sentence for an
   * unevidenced claim says in so many words that it is a gap in what was
   * examined, and this component prints that sentence rather than writing one.
   */
  evidence: string;
  confidence?: string | null;
}

export interface ClaimEvidence {
  note?: string;
  entries: ClaimEvidenceEntry[];
  no_claims_statement?: string | null;
}

/**
 * Leadership Alignment (2026-09-29, spec 22.9). Every line is a statement the
 * server rendered through the citation chokepoint; a withheld one never
 * arrives. `kind` separates a leader's expectation (`finding`, the requirement
 * source) from evidence the candidate gave (`grade`), a gap (`gap`) and a
 * follow-up question (`probe`).
 */
export interface LeadershipAlignment {
  note?: string;
  groups: { title: string; lines: { kind: string; text: string }[] }[];
}

/**
 * One spoke of one radar chart, built server-side (spec §10.4).
 *
 * Two shapes are plotted on the same axes: what the job requires and what the
 * candidate demonstrated. `*_index` (1 innermost .. 4 outermost) is a RENDERING
 * COORDINATE, not a disclosed score, because a radar cannot be drawn without a radius,
 * and the four grades are the radial axis. It is never displayed: no axis
 * ticks, no data labels, no tooltips.
 */
export interface RadarAxis {
  axis: string;
  /** Null when the report recorded no requirement for this spoke: the chart
   *  then draws the candidate's shape only, never a requirement nobody stated. */
  requirement_band: RatingGrade | null;
  requirement_index: number | null;
  candidate_band: RatingGrade;
  candidate_index: number;
}

export interface GapProbeItem {
  name: string;
  grade: RatingGrade;
  /** The SAME remark the item carries in its own section. Reused, never
   *  rewritten: one report states one assessment of an item. */
  remark?: string | null;
  probes: string[];
}

export interface GapGroup {
  category: string;
  label: string;
  items: GapProbeItem[];
  /** Said in words when the group is empty, rather than left as blank space. */
  no_gaps_statement?: string | null;
  /** Present on the Must-have group when a Not Matching item has capped the
   *  Overall Grade, so the reader is told rather than left to cross-reference. */
  cap_statement?: string | null;
}

export interface GapAnalysis {
  focus_summary: string;
  must_have_cap_applied: boolean;
  groups: GapGroup[];
}

export interface RadarChartSpec {
  key: string;
  title: string;
  axes: RadarAxis[];
}

/**
 * The PRISM Report's section order (spec doc 4, part 3), and the ONLY place it
 * is written down on this side.
 *
 * The view below is driven by this array rather than by a hand-ordered block of
 * JSX, so a section cannot be moved by editing one file and left where it was
 * in the other. `backend/tests/test_prism_report.py` reads this literal out of
 * this source file and asserts it equals `report_pdf.SECTION_ORDER`: a reorder
 * that touches only one renderer fails there instead of shipping a PDF that
 * disagrees with the screen a recruiter approved it from.
 *
 * Gap Analysis now precedes Validation. Validation is the candidate's own
 * unrated submission and is the last thing on the document; the action plan
 * belongs beside the grades it was derived from, not after a block of
 * uninterpreted form answers.
 */
export const REPORT_SECTION_ORDER = [
  "ai_score",
  "overall",
  "must_have",
  "nice_to_have",
  "behavioural",
  // Leadership Alignment reads the grades above it against what the company's
  // leaders asked for, so it follows them directly (spec 22.9).
  "leadership_alignment",
  // The Evidence vs Claim Summary sits with the rated sections and the
  // Recommended Human Validation Points sit with the plan, which is why they
  // are not adjacent. The first annotates the grades above it; the second is
  // the other thing an interviewer acts on, beside the Gap Analysis. Gap
  // Analysis still precedes Validation, unchanged.
  "claim_evidence",
  "gap_analysis",
  "validation_points",
  "validation",
  // The Proctoring Report is LAST (proctoring spec section 7). It is
  // informational, it moves no grade, and it sits after everything that does.
  "proctoring",
] as const;

/**
 * THREE radar charts, not four (spec doc 4, part 3).
 *
 * The spec lists a chart for Overall Assessment, Must-have and Nice-to-have and
 * lists only a grade and a remark for Behavioural. Filtering here rather than
 * at the generator is deliberate: a report is immutable, so every report
 * already written still carries a behavioural chart in its stored payload, and
 * a reader opening one today must see the same three charts as a reader opening
 * one written tomorrow.
 */
export const RENDERED_CHART_KEYS = ["overall", "must_have", "nice_to_have"] as const;

export interface AiMatchTag {
  text: string;
  polarity: "positive" | "negative";
}

/** The AI Match of a report written from the ReadyPick release on: the
 *  pre-assessment check, frozen when the report was written. A grade word, a
 *  header sentence and evidence tags; no remark, no chart, no number. */
export interface AiMatchSnapshot {
  status: "scored" | "not_assessed" | "pending";
  grade?: RatingGrade | null;
  header?: string;
  tags?: AiMatchTag[];
}

export interface FunctionalReport {
  id: string;
  job_candidate_link_id: string;
  /** COMPANY-JOB-CANDIDATE, so a printed report and a row in the candidate
   *  table can be matched by eye. A label, never a permission. */
  reference_code?: string;
  grade: string;
  /** LEGACY: the four resume-only rows of a report written before the
   *  ReadyPick release. Empty on a newer report, which carries
   *  `ai_score_snapshot`. The key keeps its stored name. */
  ai_score: ReportDimension[];
  ai_score_snapshot?: AiMatchSnapshot | null;
  /** "Not assessed" exactly when `overall_status` is `not_assessed`. */
  overall_grade: RatingGrade | typeof NOT_ASSESSED_WORD;
  overall_status?: "graded" | "not_assessed";
  overall_summary: string;
  must_have: ReportDimension[];
  nice_to_have: ReportDimension[];
  behavioural: ReportDimension[];
  /** LEGACY, and empty on every report written from Draft v4 onward: technical
   *  depth is assessed inside Must-have. Non-empty only on a report written
   *  against the standalone technical bank that no longer exists. */
  technical?: ReportDimension[];
  validation: ValidationBlock;
  /** Gap Analysis & Action Plan (spec 9.6). */
  gap_analysis?: GapAnalysis;
  /** Evidence vs Claim Summary. Absent on a report written before it. */
  claim_evidence?: ClaimEvidence;
  /** Leadership Alignment. Absent (null) when the job had no leadership input,
   *  and on every report written before the section existed. */
  leadership_alignment?: LeadershipAlignment | null;
  /** Recommended Human Validation Points. Same reading of absent. */
  validation_points?: ValidationPoints;
  /** RETIRED, replaced by `gap_analysis`. Non-empty only on a report written
   *  before Draft v4, which still renders what it was actually written with. */
  suggested_interview_questions?: string[];
  radar_charts?: RadarChartSpec[];
  radar_bands?: string[];
  radar_series?: string[];
  /** The Proctoring Report, when one has been generated for this assessment.
   *  Null while the session is still running or its report is still being
   *  written. Words only: it carries no number at any path. */
  proctoring?: ProctoringReport | null;
  /** Why `proctoring` is or is not there: no session, still running,
   *  generating, could not be produced, partially unavailable, or genuinely no
   *  events. The server's words; absent only on a payload from before it. */
  proctoring_state?: ProctoringState | null;
  synthesized_at: string;
  immutable?: boolean;
  /** Whether the candidate consented to their assessment being retained for
   *  future jobs: Download offered only when true, View-only otherwise. The
   *  server enforces the same rule on the PDF route, this flag just keeps the
   *  UI from offering a button the server would refuse. */
  report_download_allowed?: boolean;
  /** Whether gate G4 releases the PDF now. False, with the server's sentence
   *  in `pdf_blocked_reason`, while a report routed to a person has no
   *  recorded decision. Absent reads as available, as before the gate. */
  pdf_available?: boolean;
  pdf_blocked_reason?: string | null;
}

interface ValidationField {
  key: string;
  label: string;
  value: string | null;
  /** "Application" for the six mandatory fields (the default when absent, for
   *  reports written before the profile questionnaire was added here), or the
   *  candidate profile form's own section title for the 38 profile items. */
  group?: string;
}

interface ValidationBlock {
  captured?: boolean;
  fields?: ValidationField[];
  role_interest?: string | null;
  [key: string]: unknown;
}

/** Outermost (best) to innermost, matching `services/rating.GRADES`. */
const BAND_FILL: Record<RatingGrade, string> = {
  "Highly Matching": "#065f46",
  Matching: "#15803d",
  "Moderately Matching": "#d97706",
  "Not Matching": "#b91c1c",
};

const REQUIREMENT_COLOR = "#64748b";
const CANDIDATE_COLOR = "#0F6B5C";

function DimensionSection({
  sectionKey,
  title,
  dimensions,
  chart,
  series,
  loadCitations,
}: {
  /** The report section key, which is how the citation trail names it. */
  sectionKey: string;
  title: string;
  dimensions: ReportDimension[];
  chart?: RadarChartSpec;
  series: string[];
  loadCitations?: CitationsLoader;
}) {
  if (dimensions.length === 0) return null;
  return (
    <section aria-label={title}>
      <h3 className="mb-3 text-heading">{title}</h3>
      {chart ? <DualRadar chart={chart} series={series} /> : null}
      <div className="grid gap-3 md:grid-cols-2">
        {dimensions.map((dimension) => (
          <Card key={`${title}-${dimension.name}`}>
            <CardHeader className="pb-2">
              <div className="flex items-start justify-between gap-3">
                <CardTitle>{dimension.name}</CardTitle>
                <RatingLabel label={dimension.grade} />
              </div>
              {dimension.description ? (
                <p className="text-xs">{dimension.description}</p>
              ) : null}
              {dimension.status_note ? (
                <p className="text-xs">{dimension.status_note}</p>
              ) : null}
              {dimension.required_level ? (
                <p className="text-xs">
                  This role requires: <RatingLabel label={dimension.required_level} />
                </p>
              ) : null}
              <EvidenceConfidence dimension={dimension} />
            </CardHeader>
            <CardContent>
              <CitedRemark
                remark={dimension.remark}
                section={sectionKey}
                item={dimension.name}
                load={loadCitations}
              />
              <RemarkNotes dimension={dimension} />
            </CardContent>
          </Card>
        ))}
      </div>
    </section>
  );
}

/**
 * The server's two markers under a remark: written from a fixed template
 * while the writing model was unavailable, and not clearly supported by the
 * evidence it cites. Both are the server's words; neither is derived here.
 */
function RemarkNotes({ dimension }: { dimension: ReportDimension }) {
  const notes = [dimension.remark_note, dimension.support_note].filter(
    (note): note is string => Boolean(note)
  );
  if (notes.length === 0) return null;
  return (
    <div className="mt-2 space-y-1">
      {notes.map((note) => (
        <p key={note} className="text-xs italic">
          {note}
        </p>
      ))}
    </div>
  );
}

/**
 * The Evidence Confidence line under a grade.
 *
 * It renders NOTHING when the server sent no confidence, which is every report
 * written before the field existed. That is the same rule `chartFor` follows
 * for the fourth chart: a report is immutable, so the renderer is the only
 * thing that can hold a rule for documents written under a different one, and
 * the only honest rendering of a value that was never computed is no line.
 *
 * The word is the server's. This component never derives one, never maps a
 * code, and never colours it: a tinted chip beside "Low" would state a
 * judgement about the candidate, and confidence is a statement about the
 * record.
 */
function EvidenceConfidence({ dimension }: { dimension: ReportDimension }) {
  if (!dimension.evidence_confidence) return null;
  const sources = dimension.evidence_sources ?? [];
  return (
    <p className="text-xs">
      Evidence confidence: {dimension.evidence_confidence}
      {sources.length > 0 ? <> {"\u00b7"} Sources: {sources.join(", ")}</> : null}
    </p>
  );
}

/**
 * One radar plotting the job's required level against the candidate's assessed
 * level (spec §10.4). No numbers anywhere: no axis ticks, no data labels, no
 * tooltip. The legend names the two shapes by word.
 *
 * The radial domain is FIXED at 0..4 rather than auto: an auto domain rescales
 * per candidate, which would make a weak profile draw identically to a strong
 * one and defeat the whole point of comparing two people's charts.
 */
function DualRadar({ chart, series }: { chart: RadarChartSpec; series: string[] }) {
  if (chart.axes.length < 3) return null;
  // The requirement shape is drawn only when every spoke on THIS chart states
  // a requirement. A spoke with none would otherwise sit at the centre, which
  // states "requires nothing": a requirement nobody recorded. The server
  // sends one series label when no spoke anywhere carries a requirement.
  const drawsRequirement =
    series.length > 1 && chart.axes.every((entry) => entry.requirement_index !== null);
  const requirementLabel = series.length > 1 ? series[0] : "Job Requirement";
  const candidateLabel = series[series.length - 1] ?? "Candidate Assessment";

  const data = chart.axes.map((entry) => ({
    dimension: entry.axis,
    ...(drawsRequirement ? { [requirementLabel]: entry.requirement_index } : {}),
    [candidateLabel]: entry.candidate_index,
  }));

  return (
    <div className="mb-4">
      <div className="h-[360px] rounded-lg border bg-background p-3">
        <ResponsiveContainer width="100%" height="100%">
          <RadarChart data={data} outerRadius="70%">
            <PolarAngleAxis dataKey="dimension" tick={{ fontSize: 11, fill: "currentColor" }} />
            <PolarRadiusAxis domain={[0, RATING_GRADES.length]} tick={false} axisLine={false} />
            {drawsRequirement ? (
              <Radar
                name={requirementLabel}
                dataKey={requirementLabel}
                stroke={REQUIREMENT_COLOR}
                strokeWidth={2}
                strokeDasharray="5 4"
                fill={REQUIREMENT_COLOR}
                fillOpacity={0.1}
                dot={false}
                isAnimationActive={false}
              />
            ) : null}
            <Radar
              name={candidateLabel}
              dataKey={candidateLabel}
              stroke={CANDIDATE_COLOR}
              strokeWidth={2}
              fill={CANDIDATE_COLOR}
              fillOpacity={0.25}
              dot={false}
              isAnimationActive={false}
            />
            <Legend wrapperStyle={{ fontSize: 12 }} />
          </RadarChart>
        </ResponsiveContainer>
      </div>
    </div>
  );
}

/** The grade ramp, outermost to innermost. Words only. */
function GradeLegend() {
  return (
    <ul className="flex flex-wrap items-center gap-3 text-xs" aria-label="Grade legend">
      {RATING_GRADES.map((grade) => (
        <li key={grade} className="flex items-center gap-1.5">
          <span
            aria-hidden
            className="h-2.5 w-2.5 rounded-full"
            style={{ backgroundColor: BAND_FILL[grade] }}
          />
          {grade}
        </li>
      ))}
    </ul>
  );
}

function chartFor(report: FunctionalReport, key: string): RadarChartSpec | undefined {
  // The allowlist is checked here rather than at each call site: a section
  // added later gets no chart unless somebody names it above, which is the
  // direction that fails safely against the three-chart rule.
  if (!(RENDERED_CHART_KEYS as readonly string[]).includes(key)) return undefined;
  return report.radar_charts?.find((chart) => chart.key === key);
}

function ValidationSection({ validation }: { validation: ValidationBlock }) {
  // The server sends the field list in form order so the report and the form a
  // candidate filled in cannot drift apart.
  const fields = validation.fields ?? [];
  const facts = fields.filter((field) => field.key !== "role_interest");
  const interest = fields.find((field) => field.key === "role_interest")?.value;

  // Grouped by "Application" (the six mandatory fields) then the candidate
  // profile form's own sections (the 38 profile items), in first-appearance
  // order, so the section reads as two questionnaires rather than one long
  // grid once the profile answers are included.
  const groups: Array<{ title: string; items: ValidationField[] }> = [];
  for (const field of facts) {
    const title = field.group ?? "Application";
    let group = groups.find((g) => g.title === title);
    if (!group) {
      group = { title, items: [] };
      groups.push(group);
    }
    group.items.push(field);
  }

  return (
    <section aria-label="Validation">
      <h3 className="mb-1 text-heading">Validation</h3>
      <p className="mb-3 max-w-prose text-body-sm">
        Submitted by the candidate on their application and profile, shown exactly as written.
        Nothing here is rated or interpreted.
      </p>
      {validation.captured === false ? (
        <p className="rounded-md border p-3 text-sm">
          This application was submitted before these fields became mandatory, so none were
          collected.
        </p>
      ) : (
        <>
          {/* SEMANTIC left rule, and a documented Impeccable `side-tab`
              exception (.impeccable-exceptions.md). It marks the candidate's
              OWN unrated words, reproduced exactly as submitted, and that
              boundary is the point: a reader has to be able to tell what the
              candidate said from what the platform concluded. */}
          <blockquote className="mb-4 border-l-4 border-teal-600 pl-4 text-sm italic">
            “{interest || "Not stated"}”
          </blockquote>
          <div className="space-y-5">
            {groups.map((group) => (
              <div key={group.title}>
                <h4 className="mb-2 type-eyebrow text-muted-foreground">
                  {group.title}
                </h4>
                <div className="grid gap-3 sm:grid-cols-2">
                  {group.items.map((field) => (
                    <div key={field.key} className="rounded-md border p-3">
                      <p className="type-eyebrow">{field.label}</p>
                      <p className="mt-1 text-sm">{field.value || "Not stated"}</p>
                    </div>
                  ))}
                </div>
              </div>
            ))}
          </div>
        </>
      )}
    </section>
  );
}

export function FunctionalSkillsReportView({
  report,
  loadCitations,
}: {
  report: FunctionalReport;
  /** Fetches what each remark rests on. Called on the first remark a reader
   *  opens, never on render; omitted where no citations route exists. */
  loadCitations?: CitationsLoader;
}) {
  const series = report.radar_series ?? ["Job Requirement", "Candidate Assessment"];

  // Keyed by the section identifiers in REPORT_SECTION_ORDER and rendered by
  // walking that array. A section can therefore be reordered in exactly one
  // place, and a key with no entry here would render nothing loudly rather
  // than quietly moving to the end.
  const sections: Record<(typeof REPORT_SECTION_ORDER)[number], React.ReactNode> = {
    ai_score: <AiScoreSection key="ai_score" report={report} />,
    overall: <OverallSection key="overall" report={report} series={series} />,
    must_have: (
      <DimensionSection
        key="must_have"
        sectionKey="must_have"
        loadCitations={loadCitations}
        title="Must-have"
        dimensions={report.must_have}
        chart={chartFor(report, "must_have")}
        series={series}
      />
    ),
    nice_to_have: (
      <DimensionSection
        key="nice_to_have"
        sectionKey="nice_to_have"
        loadCitations={loadCitations}
        title="Nice-to-have"
        dimensions={report.nice_to_have}
        chart={chartFor(report, "nice_to_have")}
        series={series}
      />
    ),
    // No chart: the spec gives Behavioural a grade and a remark only.
    behavioural: (
      <DimensionSection
        key="behavioural"
        sectionKey="behavioural"
        loadCitations={loadCitations}
        title="Behavioural Competencies"
        dimensions={report.behavioural}
        series={series}
      />
    ),
    leadership_alignment: (
      <LeadershipAlignmentSection
        key="leadership_alignment"
        alignment={report.leadership_alignment ?? null}
      />
    ),
    claim_evidence: (
      <ClaimEvidenceSection key="claim_evidence" summary={report.claim_evidence} />
    ),
    gap_analysis: <GapAnalysisSection key="gap_analysis" report={report} />,
    validation_points: (
      <ValidationPointsSection key="validation_points" points={report.validation_points} />
    ),
    validation: <ValidationSection key="validation" validation={report.validation} />,
    proctoring: <ProctoringSection
        key="proctoring"
        report={report.proctoring ?? null}
        state={report.proctoring_state ?? null}
      />,
  };

  return (
    <div className="space-y-8">
      {REPORT_SECTION_ORDER.map((key) => sections[key])}
    </div>
  );
}

/** The section's heading: the product's name for the pre-assessment check,
 *  the same words the candidate table uses. The payload key stays `ai_score`
 *  because stored reports carry it; the PDF prints the same heading. */
export const AI_MATCH_TITLE = "AI Match";

/**
 * The AI Match: the resume check made before the assessment, frozen when the
 * report was written (spec doc 4, part 3).
 *
 * A report written from the ReadyPick release on carries the snapshot: a grade
 * word, the server's header sentence and evidence tags, each marked with a
 * check or a cross icon (never an emoji) and named for a screen reader. An
 * older report carries the four legacy rows it was written with, rendered as
 * they were written because a report is immutable.
 */
function AiScoreSection({ report }: { report: FunctionalReport }) {
  const snapshot = report.ai_score_snapshot;
  const legacy = report.ai_score ?? [];
  if (!snapshot && legacy.length === 0) return null;
  return (
    <section aria-label={AI_MATCH_TITLE}>
      <h3 className="mb-1 text-heading">{AI_MATCH_TITLE}</h3>
      <p className="mb-3 max-w-prose text-body-sm">
        A resume-based check made before the assessment. A close match with the Tatva
        Assessment below confirms the resume was accurate; a gap between them is itself
        worth knowing.
      </p>
      {snapshot ? (
        <div className="rounded-lg border p-4">
          <RatingLabel label={snapshot.grade ?? NOT_ASSESSED_WORD} />
          {snapshot.header ? <p className="mt-2 max-w-prose text-body">{snapshot.header}</p> : null}
          {snapshot.tags && snapshot.tags.length > 0 ? (
            <ul className="mt-3 flex flex-wrap gap-2" aria-label="Evidence tags">
              {snapshot.tags.map((tag) => (
                <li
                  key={`${tag.polarity}-${tag.text}`}
                  className="flex items-center gap-1 rounded border px-2 py-0.5 text-xs"
                >
                  {tag.polarity === "positive" ? (
                    <Check className="h-3.5 w-3.5 text-teal-700 dark:text-teal-300" aria-hidden />
                  ) : (
                    <X className="h-3.5 w-3.5" aria-hidden />
                  )}
                  <span className="sr-only">
                    {tag.polarity === "positive" ? "Evidenced:" : "Not evidenced:"}
                  </span>
                  {tag.text}
                </li>
              ))}
            </ul>
          ) : null}
        </div>
      ) : (
        <div className="grid gap-3 md:grid-cols-2">
          {legacy.map((dimension) => (
            <Card key={`ai-${dimension.name}`}>
              <CardHeader className="pb-2">
                <div className="flex items-start justify-between gap-3">
                  <CardTitle>{dimension.name}</CardTitle>
                  <RatingLabel label={dimension.grade} />
                </div>
                {dimension.description ? (
                  <p className="text-xs">{dimension.description}</p>
                ) : null}
                <EvidenceConfidence dimension={dimension} />
              </CardHeader>
              <CardContent>
                <p className="max-w-prose text-body">{dimension.remark}</p>
              </CardContent>
            </Card>
          ))}
        </div>
      )}
    </section>
  );
}

/**
 * The Overall Assessment: the Overall Grade, its 45 to 50 word remark, and the
 * first of the three radar charts.
 *
 * The heading is the spec's own section name. It is deliberately not named
 * after the framework: the framework is the Tatva Assessment and this is the
 * PRISM Report that states its result, and using either word for the other is
 * how a reader ends up thinking they are one thing.
 */
function OverallSection({
  report,
  series,
}: {
  report: FunctionalReport;
  series: string[];
}) {
  const chart = chartFor(report, "overall");
  return (
      <section aria-label="Overall Assessment">
        <h3 className="mb-1 text-heading">Overall Assessment</h3>
        <div className="rounded-lg border bg-muted/30 p-5">
          <div className="mb-2 flex items-center gap-3">
            <p className="type-eyebrow">Overall</p>
            <RatingLabel label={report.overall_grade} />
          </div>
          <p className="max-w-prose leading-7">{report.overall_summary}</p>
        </div>
        {chart ? (
          <div className="mt-4">
            <DualRadar chart={chart} series={series} />
          </div>
        ) : null}
        <GradeLegend />
      </section>
  );
}


/**
 * Gap Analysis & Action Plan (spec 9.6).
 *
 * Replaces the suggested-questions list entirely. Three groups in the same
 * Must-have / Nice-to-have / Behavioural order the rest of the report uses,
 * Not Matching before Moderately Matching inside each, and every empty group
 * says so in words rather than rendering blank space.
 *
 * The retired suggested-questions payload is deliberately never rendered.
 * Draft v4 replaced that client-facing section completely.
 */
function GapAnalysisSection({ report }: { report: FunctionalReport }) {
  const gaps = report.gap_analysis;
  if (!gaps || gaps.groups.length === 0) {
    return null;
  }

  return (
    <section aria-label="Gap Analysis and Action Plan">
      <h3 className="mb-3 text-heading">Gap Analysis &amp; Action Plan</h3>
      {gaps.focus_summary ? (
        <p className="mb-4 rounded-md border bg-muted/30 p-4 font-medium leading-7">
          {gaps.focus_summary}
        </p>
      ) : null}

      <div className="space-y-6">
        {gaps.groups.map((group) => (
          <div key={group.category}>
            <h4 className="mb-2 type-eyebrow">
              {group.label}
            </h4>
            {group.cap_statement ? (
              <p className="mb-3 rounded-md border border-dashed p-3 text-sm font-medium">
                {group.cap_statement}
              </p>
            ) : null}
            {group.items.length === 0 ? (
              <p className="rounded-md border p-3 text-sm">{group.no_gaps_statement}</p>
            ) : (
              <ul className="space-y-3">
                {group.items.map((item) => (
                  <li key={item.name} className="rounded-md border p-4">
                    <div className="mb-2 flex flex-wrap items-center gap-3">
                      <span className="font-medium">{item.name}</span>
                      <RatingLabel label={item.grade} />
                    </div>
                    {item.remark ? (
                      <p className="mb-3 max-w-prose text-sm leading-7">{item.remark}</p>
                    ) : null}
                    <ul className="space-y-2">
                      {item.probes.map((probe, index) => (
                        <li
                          key={`${item.name}-${index}`}
                          className="rounded-md border border-dashed p-3 text-sm"
                        >
                          {probe}
                        </li>
                      ))}
                    </ul>
                  </li>
                ))}
              </ul>
            )}
          </div>
        ))}
      </div>

      <p className="mt-4 text-xs">
        Advisory input for the interviewer, grounded in what the candidate actually said. It
        identifies what to probe, never whether to advance or reject.
      </p>
    </section>
  );
}

export const LEADERSHIP_ALIGNMENT_TITLE = "Leadership Alignment";

/** Leadership Alignment: absent entirely when the report has none. */
function LeadershipAlignmentSection({ alignment }: { alignment: LeadershipAlignment | null }) {
  if (!alignment || alignment.groups.length === 0) return null;
  return (
    <section aria-label={LEADERSHIP_ALIGNMENT_TITLE}>
      <h3 className="mb-1 text-heading">{LEADERSHIP_ALIGNMENT_TITLE}</h3>
      {alignment.note ? <p className="mb-3 max-w-prose text-body-sm">{alignment.note}</p> : null}
      <div className="space-y-4">
        {alignment.groups.map((group) => (
          <div key={group.title}>
            <p className="mb-1 font-semibold">{group.title}</p>
            <ul className="list-disc space-y-1 pl-5">
              {group.lines.map((line, index) => (
                <li key={`${group.title}-${index}`} className="max-w-prose text-sm leading-7">
                  {line.text}
                </li>
              ))}
            </ul>
          </div>
        ))}
      </div>
    </section>
  );
}

export const CLAIM_EVIDENCE_TITLE = "Evidence vs Claim Summary";
export const VALIDATION_POINTS_TITLE = "Recommended Human Validation Points";

/**
 * Evidence vs Claim Summary.
 *
 * Every sentence in it is the server's. In particular the sentence for a claim
 * nothing addressed is printed verbatim and is never shortened to "no evidence
 * found": the server's wording says in so many words that it is a gap in what
 * was examined rather than a finding about the claim, and that second half is
 * the whole reason the section is safe to render beside a hiring decision.
 */
function ClaimEvidenceSection({ summary }: { summary?: ClaimEvidence }) {
  const entries = summary?.entries ?? [];
  const statement = summary?.no_claims_statement;
  if (!summary || (entries.length === 0 && !statement)) return null;

  return (
    <section aria-label={CLAIM_EVIDENCE_TITLE}>
      <h3 className="mb-1 text-heading">{CLAIM_EVIDENCE_TITLE}</h3>
      {summary.note ? <p className="mb-3 max-w-prose text-body-sm">{summary.note}</p> : null}
      {entries.length === 0 ? (
        <p className="rounded-md border p-3 text-sm">{statement}</p>
      ) : (
        <ul className="space-y-3">
          {entries.map((entry, index) => (
            <li key={`${entry.claim}-${index}`} className="rounded-md border p-4">
              {entry.area ? (
                <p className="mb-1 type-eyebrow">{entry.area}</p>
              ) : null}
              <p className="max-w-prose font-medium leading-7">{entry.claim}</p>
              <p className="mt-2 max-w-prose text-sm leading-7">{entry.evidence}</p>
              {entry.confidence ? (
                <p className="mt-2 text-xs">Evidence confidence: {entry.confidence}</p>
              ) : null}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

/**
 * Recommended Human Validation Points.
 *
 * Rendered as its own section rather than folded into the Gap Analysis, and
 * the separation is the product decision: the Gap Analysis answers "where is
 * this person weaker than the role needs", this answers "where is this report
 * least safe to act on", and an item can be in either without being in both.
 *
 * No icons, no colour codes and no severity column, for the same reason the
 * Proctoring Report has none: order carries what weight the section is
 * entitled to state, and a tinted chip beside an area would be a judgement the
 * product has not earned.
 */
function ValidationPointsSection({ points }: { points?: ValidationPoints }) {
  const rows = points?.points ?? [];
  const statement = points?.no_points_statement;
  if (!points || (rows.length === 0 && !statement)) return null;

  return (
    <section aria-label={VALIDATION_POINTS_TITLE}>
      <h3 className="mb-1 text-heading">{VALIDATION_POINTS_TITLE}</h3>
      {points.note ? <p className="mb-3 max-w-prose text-body-sm">{points.note}</p> : null}
      {rows.length === 0 ? (
        <p className="rounded-md border p-3 text-sm">{statement}</p>
      ) : (
        <ul className="space-y-3">
          {rows.map((point, index) => (
            <li key={`${point.area}-${index}`} className="rounded-md border p-4">
              <div className="mb-1 flex flex-wrap items-center gap-3">
                <span className="font-medium">{point.area}</span>
                {point.confidence ? (
                  <span className="text-xs">
                    Evidence confidence: {point.confidence}
                  </span>
                ) : null}
              </div>
              <p className="max-w-prose text-sm leading-7">{point.reason}</p>
              <p className="mt-2 rounded-md border border-dashed p-3 text-sm">
                {point.probe}
              </p>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

/**
 * The Proctoring Report (proctoring spec section 7), the final section.
 *
 * NO ICONS, NO COLOUR CODES, NO SEVERITY COLUMN (spec 7.1). The reader is
 * meant to understand importance from what is said and where it sits: the
 * most significant finding is first because the server ordered it that way,
 * and nothing on this page ranks it again. A tinted chip or a warning icon
 * beside a finding would state a judgement the system is not entitled to
 * make, which is the same reason the sentences never say the candidate
 * cheated.
 *
 * Every string here comes from the server's phrasing library. This component
 * writes only the two constants below, which are the same words the PDF
 * prints, because a recruiter reads one and forwards the other.
 */
export const PROCTORING_TITLE = "Proctoring Report";
export const PROCTORING_NOTE =
  "Informational only. This section does not affect this candidate's score or ranking.";
export const PROCTORING_ABSENT = "No proctoring report exists for this assessment.";

const FINDING_GROUPS: Array<{ key: keyof ProctoringReport["findings"]; label: string }> = [
  { key: "screen_browser", label: "Screen & Browser Activity" },
  { key: "camera", label: "Camera Monitoring" },
  { key: "audio", label: "Audio Monitoring" },
  { key: "answer_patterns", label: "Answer Pattern Analysis" },
];

function ProctoringSection({
  report,
  state,
}: {
  report: ProctoringReport | null;
  state: ProctoringState | null;
}) {
  return (
    <section aria-label="Proctoring Report">
      <h3 className="mb-1 text-heading">{PROCTORING_TITLE}</h3>
      <p className="mb-3 max-w-prose text-body-sm">{PROCTORING_NOTE}</p>
      {report === null ? (
        <p className="rounded-md border p-3 text-sm" data-proctoring-state={state?.state}>
          {state?.message ?? PROCTORING_ABSENT}
        </p>
      ) : (
        <div className="space-y-5">
          {state?.message ? (
            <p className="rounded-md border p-3 text-sm" data-proctoring-state={state.state}>
              {state.message}
            </p>
          ) : null}
          <div className="rounded-md border p-4">
            <p className="text-xs">{report.date_line}</p>
            <p className="mt-2 max-w-prose font-medium leading-7">{report.outcome}</p>
            <p className="mt-2 max-w-prose text-sm leading-7">{report.summary}</p>
          </div>

          <div className="space-y-4">
            {FINDING_GROUPS.map((group) => (
              <div key={group.key}>
                <h4 className="mb-2 type-eyebrow">{group.label}</h4>
                <ul className="space-y-2">
                  {report.findings[group.key].map((sentence, index) => (
                    <li key={`${group.key}-${index}`} className="text-sm leading-7">
                      {sentence}
                    </li>
                  ))}
                </ul>
              </div>
            ))}
          </div>

          {report.activity_log.length > 0 ? (
            <div>
              <h4 className="mb-2 type-eyebrow">Activity Log</h4>
              <div className="overflow-x-auto">
                <table className="w-full min-w-[36rem] border-collapse text-sm">
                  <thead>
                    <tr className="border-b border-border text-left">
                      <th className="py-2 pr-4 font-medium">Time</th>
                      <th className="py-2 pr-4 font-medium">What happened</th>
                      <th className="py-2 pr-4 font-medium">How long</th>
                      <th className="py-2 font-medium">What the system did</th>
                    </tr>
                  </thead>
                  <tbody>
                    {report.activity_log.map((row, index) => (
                      <tr key={`log-${index}`} className="border-b border-border align-top">
                        <td className="py-2 pr-4 text-xs tabular-nums">{row.time}</td>
                        <td className="py-2 pr-4">{row.what_happened}</td>
                        <td className="py-2 pr-4">{row.how_long}</td>
                        <td className="py-2">{row.what_the_system_did}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          ) : null}

          <p className="text-xs leading-6">{report.closing}</p>
        </div>
      )}
    </section>
  );
}
