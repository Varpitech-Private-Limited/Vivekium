import uuid
from datetime import datetime

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.jobs import PostingSkillBucketOut
from app.schemas.proctoring import ProctoringReportOut, ProctoringStateOut
from app.schemas.reports import NumberFreeDelivery
from app.services.assessment_formats import types as question_types
from app.services.ppi import CATEGORIES


# REMOVED 2026-08-06: `TechnicalQuestionIn`, `TechnicalQuestionOut` and
# `QuestionBankOut`, the request and response shapes of the Company Portal's
# preset technical question bank.
#
# A company can no longer create, edit, store or assign technical questions.
# They are written per candidate, during the conversation, from the JD, that
# candidate's resume and the live transcript (`services/technical_interview`),
# so there is nothing on a job for a form to submit and nothing stored for a
# screen to list. The routes went with them; the schemas are removed rather than
# deprecated because a response model nothing returns is a contract that quietly
# reads as still supported.


# ── Job setup: the Skills step and the setup checklist (Vivekium release) ──
#
# The Tatva matrix editor's shapes (`CompetencyIn`, `BulkCompetencyIn`,
# `CompetencyOut`, `CompetencyMoveIn`, `MatrixReorderIn`, `FrameworkOut` and the
# old `JobSetupOut`) are DELETED with the routes that spoke them. What replaced
# them carries NAMES AND STATES ONLY: no grade word per skill, no priority, no
# evidence line, no number. Those are the hidden half of the assessment
# contract (`services/assessment_contract`) and never cross this boundary.


class SkillOut(BaseModel):
    """One skill as the recruitment team reads it."""

    id: uuid.UUID
    name: str
    #: `swot` | `jd` | `company` | `team`. `team` means the hiring team wrote it.
    source: str
    #: The SWOT sentence this skill answers, VERBATIM, or None. Cleared by a
    #: rename, because a quotation carried onto a different skill is a
    #: fabricated citation (2026-09-23).
    from_swot: str | None = None


class SkillBucketsOut(BaseModel):
    must_have: list[SkillOut] = []
    nice_to_have: list[SkillOut] = []
    behavioural: list[SkillOut] = []


class SkillBucketPermissionsOut(BaseModel):
    """Per-bucket edit answers for THIS person on THIS job, resolved by the
    same `rbac.authorize` calls the write routes enforce with."""

    must_have: bool = False
    nice_to_have: bool = False
    behavioural: bool = False


class SkillsOut(BaseModel):
    """The Skills step (PLAN-p1 section 3.12)."""

    job_id: uuid.UUID
    #: not_started | drafting | drafted | failed. A lost draft reads failed.
    draft_status: str
    #: The server's own sentence for a failed draft, rendered verbatim.
    draft_error: str | None = None
    saved: bool
    locked: bool
    #: The most a bucket may hold. A limit the reviewer is told, not a score.
    max_per_bucket: int
    #: The saved SWOT is newer than the one the skills were drafted from. An
    #: OFFER only: nothing is re-drafted without a person asking.
    redraft_available: bool = False
    #: Active skills the team wrote, named so a redraft confirmation can say
    #: exactly what it would replace.
    human_authored_names: list[str] = []
    #: Why these skills cannot be saved as they stand, every problem named.
    blocking_reason: str | None = None
    buckets: SkillBucketsOut
    can_edit: SkillBucketPermissionsOut
    can_save: bool = False
    #: The server's frozen sentence (`assessment_contract.FROZEN_DETAIL`, with
    #: the date), rendered verbatim, or None when the job is not frozen.
    frozen_reason: str | None = None
    #: Why a draft cannot be asked for now (the JD is too thin to draft from),
    #: the fixed sentence rendered verbatim, or None when it can.
    draft_blocked_reason: str | None = None


class SkillAddIn(BaseModel):
    bucket: str = Field(pattern="^(" + "|".join(CATEGORIES) + ")$")
    name: str = Field(min_length=1, max_length=255)


class SkillBulkAddIn(BaseModel):
    """"Paste a list". All or nothing against the per-bucket limit."""

    bucket: str = Field(pattern="^(" + "|".join(CATEGORIES) + ")$")
    names: list[str] = Field(min_length=1, max_length=50)


class SkillPatchIn(BaseModel):
    """Rename, move, or both. A rename is applied before a move."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=255)
    bucket: str | None = Field(default=None, pattern="^(" + "|".join(CATEGORIES) + ")$")


class SkillsDraftIn(BaseModel):
    #: Required to replace skills the team wrote themselves. Without it a
    #: redraft over them is a 409 naming them.
    confirm_overwrite: bool = False


class JobSetupOut(BaseModel):
    """The job-setup checklist (PLAN-p1 section 3.12, CONTRACT v10). States only.

    Every flag is DERIVED from the tables on read: `swot_saved` from the SWOT
    row's own status, `skills_saved` from the saved stamp AND the hidden
    context, `frozen` from the existence of a snapshot row. A timestamp is not
    evidence that work happened (rule 8).
    """

    job_id: uuid.UUID
    jd_ready: bool
    #: The SWOT document's state as a reader sees it (a lost generation reads
    #: `failed`). Informational: the SWOT is internal hiring intelligence and
    #: never a publication step (CONTRACT v10).
    swot_status: str
    swot_saved: bool
    skills_draft_status: str
    skills_saved: bool
    #: Equal to `frozen`; kept for the readers that already ask it.
    skills_locked: bool
    #: Equal to `frozen`: the grade is frozen with the skills.
    grade_locked: bool
    published: bool
    ready_for_candidates: bool
    #: The server's own sentence naming every step still missing before
    #: publication (the JD and saved skills), or None when nothing blocks it.
    publish_blocked_reason: str | None = None
    #: The JD, the title, the experience band, the grade and the skills are
    #: read-only: the first genuine application froze them (CONTRACT v10).
    frozen: bool = False
    #: When the job was first frozen, or None.
    frozen_at: datetime | None = None
    #: The banner sentence, rendered verbatim, or None when not frozen.
    frozen_reason: str | None = None


class PostingPreviewOut(BaseModel):
    """The Final Job Posting, as the recruiter previews it (CONTRACT v10).

    What the candidate-facing posting says: the JD, the skills by NAME in
    three buckets, the company narrative, the grade as a WORD and the
    experience band as words. Never an evidence line, a priority or the role
    summary. Before the freeze the skills are the job's current active ones
    (`skills_saved` says whether they are the saved set); once frozen they are
    the snapshot's.
    """

    job_id: uuid.UUID
    title: str
    department: str | None = None
    #: The grade code, for a reader that keys on it.
    grade: str
    #: The grade as the posting prints it.
    grade_label: str
    #: "3 to 5 years", "at least 3 years", or None when no band is set.
    experience_band: str | None = None
    jd_markdown: str | None = None
    company_name: str | None = None
    about_company: str | None = None
    work_life: str | None = None
    benefits: str | None = None
    skill_buckets: list[PostingSkillBucketOut]
    skills_saved: bool
    published: bool
    public_application_url: str | None = None
    publish_blocked_reason: str | None = None
    frozen: bool = False
    frozen_at: datetime | None = None
    frozen_reason: str | None = None


# ── The PRISM Report (spec §10) ──────────────────────────────────────────────


class DimensionOut(BaseModel):
    name: str
    description: str | None
    #: One of the four grades. Never a number, a percentage, or a letter grade.
    #: "Not assessed" exactly when `status` is `not_assessed`: the evaluation
    #: could not be completed and no grade is stated for it (migration 0130).
    grade: str
    #: `graded`, `unanswered` or `not_assessed`, the row's stored status.
    status: Literal["graded", "unanswered", "not_assessed"] = "graded"
    #: The sentence beside a `not_assessed` row. Words only.
    status_note: str | None = None
    #: What the job requires of this item, as a word. Null on AI Score
    #: parameters and technical items, which have no job-requirement shape.
    required_level: str | None = None
    remark: str
    #: The server's marker for a remark a fixed template wrote because the
    #: writing model was unavailable. None for a model remark and for a row
    #: written before remark provenance existed.
    remark_note: str | None = None
    #: The citation trail's marker for a remark its cited evidence did not
    #: clearly support. None when it is supported or no trail exists.
    support_note: str | None = None
    #: EVIDENCE CONFIDENCE (0107): High, Moderate, Low, or "Insufficient
    #: evidence". How well corroborated the evidence behind the grade is, and
    #: never a statement about the candidate: it is derived after scoring, from
    #: distinct evidence ORIGINATORS, and it moves no grade.
    #:
    #: NULL on every row written before 0107, and nothing is substituted. A
    #: report is immutable and the evidence set an older one was written from
    #: cannot be reconstructed; a plausible word here would be the only
    #: uncheckable claim on the line.
    evidence_confidence: str | None = None
    #: The named sources it rests on, as words a reader recognises. Empty on a
    #: pre-0107 row, which renders as no source line rather than as none found.
    evidence_sources: list[str] = []


class RadarAxisOut(BaseModel):
    """One spoke of one radar chart (spec §10.4).

    Two shapes are plotted on the same axes: what the job requires and what the
    candidate demonstrated. `*_index` is a RENDERING COORDINATE, not a score:
    1 (Not Matching, innermost) to 4 (Highly Matching, outermost). A radar has
    no geometry without a radius, and the four grades ARE the axis, so this is
    the coarsest value that can draw the required chart. It is never displayed
    as a number anywhere, and it is not the underlying 0-100 score, which stays
    internal.
    """

    axis: str
    #: None when the report recorded no requirement for this spoke. The chart
    #: then draws the candidate's shape only, rather than a requirement nobody
    #: stated.
    requirement_band: str | None = None
    requirement_index: int | None = None
    candidate_band: str
    candidate_index: int


class RadarChartOut(BaseModel):
    key: str
    title: str
    axes: list[RadarAxisOut]


class GapProbeItemOut(BaseModel):
    """One gap, with its grade, its REUSED item remark, and its probes."""

    name: str
    grade: str
    #: The same remark the item carries in its own section (spec 9.6). Reused,
    #: never rewritten: one report states one assessment of an item.
    remark: str | None = None
    #: 25-30 words each, grounded in what the candidate actually said, advisory
    #: only, never phrased as an advance or reject decision (spec 9.5, 9.6).
    probes: list[str] = []


class GapGroupOut(BaseModel):
    category: str
    label: str
    items: list[GapProbeItemOut] = []
    #: Said in words when the group is empty, rather than left as blank space.
    no_gaps_statement: str | None = None
    #: Present on the Must-have group when a Not Matching Must-have item has
    #: capped the Overall Grade, so the reader is told rather than left to
    #: cross-reference the Overall Assessment (spec 9.6).
    cap_statement: str | None = None


class GapAnalysisOut(BaseModel):
    """Gap Analysis & Action Plan (spec 9.6).

    Replaces the suggested-questions section entirely. Nothing about gaps or
    probes appears anywhere else in the report.
    """

    #: One sentence naming the one or two items most worth interview time.
    focus_summary: str = ""
    must_have_cap_applied: bool = False
    #: Must-have, Nice-to-have, Behavioural, in that order.
    groups: list[GapGroupOut] = []


class ValidationPointOut(BaseModel):
    """One Recommended Human Validation Point (0107).

    NOTE THE FIELD LIST. No severity, no score, no priority and no decision.
    Order carries what ranking the section is entitled to state, exactly as the
    Proctoring Report does, and a field an outcome could be written into is a
    field an outcome eventually appears in.
    """

    area: str
    #: `confidence | borderline | contradiction`. Why this area is listed, as a
    #: code the UI can group by; the sentence a reader sees is `reason`.
    driver: str
    #: The evidence confidence word, where the driver is a confidence verdict.
    #: Null rather than blank, so a renderer can tell "not about confidence"
    #: from "confidence was empty".
    confidence: str | None = None
    reason: str
    #: One interview probe. Advisory, grounded in the record, and never phrased
    #: as an advance or reject decision.
    probe: str


class ValidationPointsOut(BaseModel):
    """Recommended Human Validation Points (0107).

    DELIBERATELY NOT THE GAP ANALYSIS. That section is GRADE driven and
    unbounded; this one is CONFIDENCE and CONTRADICTION driven and stops at
    five. A Highly Matching item resting on the candidate's own unchecked
    account is invisible to the first and is the first row of the second.
    """

    note: str = ""
    points: list[ValidationPointOut] = []
    #: Said in words when nothing needs checking, rather than blank space.
    no_points_statement: str | None = None


class ClaimEvidenceEntryOut(BaseModel):
    """One row of the Evidence vs Claim Summary (0107)."""

    #: The competency or employer the claim bears on. May be empty: a claim the
    #: matrix does not grade is still part of the candidate's account.
    area: str = ""
    #: What the candidate asserted, in the ledger's normalised wording.
    claim: str
    #: What was found when it was looked for. ABSENCE OF EVIDENCE IS NEVER
    #: RENDERED AS THE CLAIM BEING FALSE: the sentence for an unevidenced claim
    #: says in so many words that it is a gap in what was examined.
    evidence: str
    #: How well corroborated that evidence is, as a word.
    confidence: str | None = None


class ClaimEvidenceOut(BaseModel):
    """Evidence vs Claim Summary (0107).

    A CLAIM IS NOT A FACT, and this is the section that says so. It never
    states whether a claim is true; it states what the record holds.
    """

    note: str = ""
    entries: list[ClaimEvidenceEntryOut] = []
    no_claims_statement: str | None = None


class LeadershipAlignmentLineOut(BaseModel):
    """One rendered statement of the Leadership Alignment section. `kind` says
    what it is: `finding` (a leader's expectation, the requirement source),
    `grade` (evidence demonstrated), `gap` (evidence not demonstrated) or
    `probe` (a follow-up question). Words only."""

    kind: str
    text: str


class LeadershipAlignmentGroupOut(BaseModel):
    title: str
    lines: list[LeadershipAlignmentLineOut] = []


class LeadershipAlignmentOut(BaseModel):
    """Leadership Alignment (0135, spec 22.9). Absent (None) on a report whose
    contract carried no leadership context, and on every report before it."""

    note: str = ""
    groups: list[LeadershipAlignmentGroupOut] = []


class AiScoreTagOut(BaseModel):
    """One evidence tag on the AI Score snapshot: a phrase, for or against."""

    text: str
    polarity: Literal["positive", "negative"]


class AiScoreSnapshotOut(BaseModel):
    """Yukti's frozen pre-assessment snapshot (the AI Score of a report written
    from the Vivekium release on). A grade word, a header sentence and evidence
    tags; no radar, no remark, no number."""

    status: Literal["scored", "not_assessed", "pending"]
    grade: str | None = None
    header: str = ""
    tags: list[AiScoreTagOut] = []


class CitationEvidenceOut(BaseModel):
    """One piece of evidence a report statement rests on, resolved at read
    time. Words and the candidate's own text only: no id, no locator, no
    position."""

    kind: str
    question: str | None = None
    excerpt: str | None = None


class CitationStatementOut(BaseModel):
    section: str
    item: str
    kind: str
    text: str
    #: The trail's words-only marker when the statement's support is weak.
    support: str | None = None
    evidence: list[CitationEvidenceOut] = []


class ReportCitationsOut(BaseModel):
    """What each statement of a PRISM Report rests on. `trail_available` is
    False for a report written before the citation trail existed, which is a
    different answer from an empty trail."""

    trail_available: bool
    statements: list[CitationStatementOut] = []


class FunctionalReportOut(NumberFreeDelivery):
    # THE SERIALISER-LEVEL NUMBER BAN (spec-doc6 D8). Inherited rather than
    # asserted in the route: this model is the last shape a delivered PRISM
    # Report holds before it becomes JSON, and a check placed anywhere earlier
    # would be a check a second route could skip. See `schemas/reports.py`.
    id: uuid.UUID
    job_candidate_link_id: uuid.UUID
    #: COMPANY-JOB-CANDIDATE, the same code the candidate table renders under
    #: the name. Present so a printed report and a row on screen can be matched
    #: by eye. Derived, one-way, and never an authorisation input.
    reference_code: str = ""
    grade: str
    # ── AI Score: the pre-assessment resume snapshot (9.1) ──────────────
    #: LEGACY rows: the four matching parameters of a report written before
    #: the Vivekium release. Empty on a newer report, which carries
    #: `ai_score_snapshot` instead.
    ai_score: list[DimensionOut]
    ai_score_snapshot: AiScoreSnapshotOut | None = None
    # ── Tatva Assessment (9.3) ──────────────────────────────────
    #: "Not assessed" exactly when `overall_status` is `not_assessed`.
    overall_grade: str
    overall_status: Literal["graded", "not_assessed"] = "graded"
    overall_summary: str
    must_have: list[DimensionOut]
    nice_to_have: list[DimensionOut]
    behavioural: list[DimensionOut]
    #: LEGACY, and empty on every report written from Draft v4 onward: technical
    #: depth is assessed inside Must-have. Populated only for a report written
    #: against the standalone technical bank that no longer exists.
    technical: list[DimensionOut] = []
    #: The application's mandatory fields, as submitted. Never rated (6).
    validation: dict
    #: The Proctoring Report (proctoring spec 7), appended as the final section
    #: of the delivered document. INFORMATIONAL ONLY: it moves no grade, no
    #: score and no ranking, and it is words only so this model's number ban
    #: holds over it unchanged. None when no proctoring report exists yet.
    proctoring: ProctoringReportOut | None = None
    #: Why `proctoring` is or is not there, in words (no session, in progress,
    #: generating, failed, partially unavailable, no events). None only for a
    #: caller that did not ask; `prism_view.report_out` always sets it.
    proctoring_state: ProctoringStateOut | None = None
    #: Gap Analysis & Action Plan (9.6).
    gap_analysis: GapAnalysisOut = GapAnalysisOut()
    #: Evidence vs Claim Summary (0107). Empty on a report written before it,
    #: which renders without the section rather than with an empty one.
    claim_evidence: ClaimEvidenceOut = ClaimEvidenceOut()
    #: Recommended Human Validation Points (0107). Same reading of empty.
    validation_points: ValidationPointsOut = ValidationPointsOut()
    #: Leadership Alignment (0135), immediately after Behavioural in both
    #: renderers. None when the report has no such section.
    leadership_alignment: LeadershipAlignmentOut | None = None
    #: RETIRED, replaced by `gap_analysis`. Non-empty only on a report written
    #: before Draft v4, so an old report opened today still renders what it was
    #: actually written with rather than an empty section.
    suggested_interview_questions: list[str] = []
    #: Four charts are built (Overall, Must-have, Nice-to-have, Behavioural);
    #: the renderers draw three.
    radar_charts: list[RadarChartOut] = []
    #: Ordered best-to-worst grade labels, for the chart legend and colour ramp.
    radar_bands: list[str] = []
    #: The two shapes on every chart, by word (§10.4).
    radar_series: list[str] = []
    synthesized_at: datetime
    #: Reports are immutable. Advertised in the payload so the UI never has to
    #: infer it in order to hide edit/delete affordances.
    immutable: bool = True
    #: Whether the candidate consented to their assessment being retained for
    #: future jobs (Consent & Privacy spec, 2026-09-05): Download enabled if
    #: Yes, View-only if No or never asked. Advertised here so the UI hides
    #: the Download control instead of offering a button the PDF route will
    #: refuse with 403. Viewing is not gated by this flag.
    report_download_allowed: bool = False
    #: Whether gate G4 would release the PDF now (`siddhi.delivery`, the same
    #: gate the PDF route runs). False with `pdf_blocked_reason` set while a
    #: report routed to a person has no recorded decision, so the screen says
    #: why instead of offering a button the route would refuse with 409.
    pdf_available: bool = True
    pdf_blocked_reason: str | None = None


class AnswerBehaviourIn(BaseModel):
    """Keystroke and mouse TIMINGS for the answer being submitted (proctoring
    spec 4.5). Offsets in milliseconds from when the field was first focused.
    Never characters: what was typed is the answer, stored separately.
    """

    model_config = ConfigDict(extra="forbid")

    keydown_offsets_ms: list[int] = Field(default_factory=list, max_length=20_000)
    backspace_offsets_ms: list[int] = Field(default_factory=list, max_length=20_000)
    #: Blocked paste, drop and clipboard attempts on this field.
    blocked_action_count: int = Field(default=0, ge=0, le=10_000)
    #: Total milliseconds the field held focus.
    focus_ms: int = Field(default=0, ge=0, le=24 * 3600 * 1000)
    #: Pointer path, aggregated on the client at the sample rate the server
    #: configured. Never raw coordinates.
    mouse_samples: int = Field(default=0, ge=0, le=1_000_000)
    mouse_path_px: int = Field(default=0, ge=0, le=100_000_000)
    mouse_idle_ms: int = Field(default=0, ge=0, le=24 * 3600 * 1000)
    mouse_clicks: int = Field(default=0, ge=0, le=100_000)
    #: Offsets of clicks on MCQ options, for rapid-fire versus considered
    #: selection.
    option_click_offsets_ms: list[int] = Field(default_factory=list, max_length=1_000)
    #: Scroll events while the question was being read.
    scroll_events: int = Field(default=0, ge=0, le=100_000)


# ── The recruiter's view of what was actually asked and answered ─────────────
# A report states a grade; this is the evidence behind it. A recruiter deciding
# whether to interview someone, and a candidate disputing a grade, both need the
# transcript, and until 2026-08-06 the only way to read one was a psql session.


# The turn's request and response shapes (`ConversationMessageIn`,
# `ConversationOut`) live in `schemas/assessment_conversation.py` with the
# routes that speak them (2026-09-24). `ConversationAnswerEditIn` is DELETED:
# past answers are viewable and never editable (Appendix B section 3).


class QuestionOut(BaseModel):
    """The question on screen, as the candidate may see it (formats spec 5).

    `payload` is the CANDIDATE VIEW (`assessment_formats.types.candidate_view`):
    an MCQ's options in this candidate's order and no correct id, a
    fill-blank's template and blank sizes and no accepted answers, a coding
    question's language and starter code and no expected approach. The answer
    key never crosses this boundary.
    """

    id: uuid.UUID
    question_type: str
    payload: dict[str, Any] = {}
    #: Suggested time, shown as guidance. Navigation, not a score.
    time_allocation_seconds: int

    @field_validator("question_type")
    @classmethod
    def _known_type(cls, value: str) -> str:
        if value not in question_types.QUESTION_TYPES:
            raise ValueError(f"unknown question type {value!r}")
        return value


class TranscriptAnswerDetailOut(BaseModel):
    """The recruiter's view of one structured answer (formats spec 7).

    Correctness is a WORD (`correct`, `partially_correct`, `incorrect`,
    `not_answered`, or None for a format that has none), never a score. The
    AI evaluation is its reasoning, never its number. `not_executed_note` is
    present on every LEGACY coding answer so a reader cannot mistake a
    read-only judgement for a verified run.

    An EXECUTED coding answer (Phase 4) carries the four `coding_*` / review
    fields instead, all prose: the outcome sentence with its counts spelled
    out, the compiler's message when the code did not compile, and the
    code-quality review's reasoning and verbatim citations. No hidden test,
    no reference and no number crosses here.
    """

    #: The candidate view of the payload, so the recruiter sees the options
    #: in the order the candidate saw them.
    payload: dict[str, Any] = {}
    #: The answer as submitted, in its type's shape.
    answer: dict[str, Any] = {}
    #: For an MCQ: the correct option ids. For a fill-blank: accepted answers
    #: per blank. Shown BESIDE the candidate's choice, marked clearly.
    answer_key: dict[str, Any] = {}
    correctness: str | None = None
    #: Per blank, for a fill-blank: `exact`, `equivalent`, `incorrect`,
    #: `not_answered`.
    blank_results: list[str] = []
    evaluation_reasoning: str | None = None
    evaluation_citations: list[str] = []
    not_executed_note: str | None = None
    coding_outcome: str | None = None
    compile_error: str | None = None
    review_reasoning: str | None = None
    review_citations: list[str] = []
    time_spent: str | None = None


class TranscriptExchangeOut(BaseModel):
    """One question and the answer it received.

    Paired rather than returned as a flat message list. `assessment_messages`
    stores speakers in sequence, which is the right shape to write and the wrong
    shape to read: a client rendering a Q&A view would have to re-pair them, and
    every client would re-pair them slightly differently. The pairing is done
    once, server-side, by the module that knows how follow-ups are keyed.
    """

    #: 1-based position in the conversation, counting exchanges rather than
    #: messages, so it matches the "Question 7 of 45" the candidate saw.
    ordinal: int
    #: The aspect this exchange probed. Provenance for the reader, never a
    #: filter that hides anything: the candidate experienced one conversation
    #: and so does this.
    domain: str
    #: What the candidate actually READ, which is not always what was stored --
    #: the interviewer writes the question for this candidate at this point.
    question: str
    #: Verbatim, as submitted, after the inbound guard defanged attack framing.
    #: Never re-worded and never summarised: a summary of an answer is not
    #: evidence of what someone said.
    answer: str
    #: The skill or competency this exchange was filed under, as a WORD. This is
    #: what the answer was scored against, so it is what makes the transcript
    #: readable as evidence rather than as a wall of text.
    criterion: str | None = None
    #: True when this exchange was a follow-up or a re-ask rather than one of
    #: the planned questions. It shares its predecessor's criterion by design.
    follow_up: bool = False
    asked_at: datetime | None = None
    # ── The question's format (assessment-spec-doc section 7) ────────────────
    #: One of `assessment_formats.types.QUESTION_TYPES`, or None for an
    #: exchange whose question predates the formats or whose key belongs to a
    #: retired question table. The recruiter's view dispatches on it.
    question_type: str | None = None
    #: The specific, quotable resume item an evidence-based question was
    #: anchored to. "the most valuable thing in the view" (section 7): it is
    #: what tells a recruiter what was being probed.
    resume_anchor: str | None = None
    #: The per-format view of the answer: the option chosen beside the correct
    #: one, the entry beside the accepted answers, the evaluation's reasoning.
    #: Present on a BASE exchange only; a follow-up is more evidence for the
    #: same question and would otherwise render the detail twice.
    detail: TranscriptAnswerDetailOut | None = None


class TranscriptOut(BaseModel):
    job_candidate_link_id: uuid.UUID
    candidate_name: str | None = None
    job_title: str | None = None
    status: str
    #: Present only once the conversation is complete.
    completed_at: datetime | None = None
    exchanges: list[TranscriptExchangeOut]
    #: Total exchanges available, for a client paging through a long interview.
    total: int
    limit: int
    offset: int


# The invitation and consent shapes moved to
# `schemas/assessment_conversation.py` on 2026-09-24. The mode-choice shapes
# are DELETED with the mode choice: there is one assessment mode (Appendix B
# section 1).


# ── The AI-assisted Job SWOT Analysis (2026-09-13 spec, sections 23 to 33) ───

class SwotAnalysisSectionsIn(BaseModel):
    """A human's four sections. Every one is optional and may be empty: a team
    that wants to clear a section they disagree with must be able to, and a
    required field would force them to keep the model's paragraph."""

    strengths: str = Field(default="", max_length=4000)
    weaknesses: str = Field(default="", max_length=4000)
    opportunities: str = Field(default="", max_length=4000)
    threats: str = Field(default="", max_length=4000)
    #: The version the editor loaded. Sent back so a save that would overwrite
    #: somebody else's newer save is refused instead of silently winning.
    expected_version: int = Field(ge=0)


class SwotAnalysisGenerateIn(BaseModel):
    """Section 32: regeneration over human-edited content is an explicit act.

    Defaults to False so the destructive path is never the one a caller
    reaches by omission."""

    confirm_overwrite: bool = False


class SwotAnalysisOut(BaseModel):
    """The SWOT document plus everything the UI needs to render the right state.

    `can_edit` is included deliberately. The frontend could resolve it from
    the capability list it already holds, and for `edit_swot` alone that would
    be right; it could not resolve the per-JOB half of the same question,
    because assignment scope and lifecycle state are properties of this job
    and not of the person. So the server answers the whole question once, on
    the resource, and the UI renders from the answer. It is not a security
    boundary: the write routes re-authorize independently.
    """

    job_id: uuid.UUID
    #: not_generated | generating | generated | failed | edited. A generation
    #: past its stale window is served as `failed` (derived, never written).
    status: str
    strengths: str | None = None
    weaknesses: str | None = None
    opportunities: str | None = None
    threats: str | None = None
    #: "ai" once a generation has succeeded, null for a hand-written document.
    generated_by: str | None = None
    last_generated_at: datetime | None = None
    #: Why the last generation failed. Rendered only in the `failed` state.
    generation_error: str | None = None
    human_edited: bool = False
    last_modified_at: datetime | None = None
    last_modified_by_name: str | None = None
    version: int = 0
    #: True when a confirmed regeneration replaced human content that can
    #: still be put back.
    can_restore_previous: bool = False
    #: The effective answer for THIS user on THIS job.
    can_edit: bool = False
    #: The saved SWOT is newer than the one the skills were drafted from and
    #: the skills are not locked, so the team may be OFFERED "Re-draft skills
    #: from the updated SWOT". An offer only: nothing is re-drafted without the
    #: team asking (`services/skills.redraft_available`).
    skills_redraft_available: bool = False
