// Types mirroring docs/API_CONTRACT.md responses.

export type Role =
  | "super_admin"
  | "client"
  | "recruitment_manager"
  | "hr_manager"
  | "recruiter"
  | "hiring_manager"
  | "interview_manager"
  // The leadership roles (2026-09-29): CEO and MD read the whole company and
  // write only their own Leadership Intelligence; a Functional Head reads one
  // department. All three are org-portal roles.
  | "ceo"
  | "md"
  | "functional_head"
  | "candidate"
  // Business Development: ReadyPick's own sales staff. Platform staff, so
  // tenant_id is always null on this user.
  | "bd";

export interface User {
  id: string;
  role: Role;
  tenant_id: string | null;
  full_name: string;
  email: string;
  email_verified: boolean;
  phone_verified: boolean;
  password_enabled?: boolean;
  workspace_name: string;
}

/** Capability strings resolved by the RBAC engine ("*" = owner/all). */
export type Capability = string;

/** Single-user auth success: cookies set, user + capabilities returned. */
export interface AuthSession {
  user: User;
  capabilities: Capability[];
}

/** One selectable workspace when an identifier matches multiple users. */
export interface AuthContextOption {
  user_id: string;
  role: Role;
  tenant_id: string | null;
  tenant_name: string | null;
  portal: "admin" | "org" | "portal" | string;
}

/** Multi-user auth: NO cookies yet, pick a context, then select-context. */
export interface AuthContextsResponse {
  contexts: AuthContextOption[];
  context_token: string;
}

export function isContextsResponse(
  res: AuthSession | AuthContextsResponse
): res is AuthContextsResponse {
  // The single-user response carries `contexts: null` (not an absent key), so
  // test the value, not key presence, otherwise every single-user login is
  // wrongly routed into the empty "choose workspace" step and never navigates.
  return Array.isArray((res as AuthContextsResponse).contexts)
    && (res as AuthContextsResponse).contexts.length > 0;
}

// ---- Provider Portal (the ReadyPick owner's view of its customers) ----
//
// A "customer" is one onboarded client company. It is the same underlying row
// the Owner console has always called a tenant, `Tenant` below stays for the
// onboarding and delete flows, while these types carry the customer-management
// view: analytics, lifecycle and compliance records.

export type CustomerStatus = "active" | "archived";

export type ComplianceDocumentType =
  | "gstin_certificate"
  | "pan_card"
  | "tan_number"
  | "bank_account_details"
  | "signed_agreement"
  | "purchase_order"
  | "msme_certificate";

export type ComplianceDocumentGroup = "tax" | "commercial";

/** The customer's HR Head. Read-only for the Provider, they maintain it. */
export interface PrimaryContact {
  user_id?: string | null;
  name?: string | null;
  email?: string | null;
  phone?: string | null;
  /** Landline WITH extension, as one string. */
  landline?: string | null;
  status?: string | null;
}

/**
 * `jobs_closed` and `jobs_ongoing` OVERLAP during a job's 5-day grace period
 * and are not a partition of `jobs_posted`, they answer two independent
 * questions. Do not render them as parts of a whole.
 */
export interface CustomerAnalytics {
  jobs_posted: number;
  jobs_closed: number;
  jobs_ongoing: number;
  total_candidates_interacted: number;
  jobs_last_30_days: number;
}

export interface ComplianceDocument {
  id: string;
  document_type: ComplianceDocumentType;
  label: string;
  group: ComplianceDocumentGroup;
  file_name: string;
  mime_type?: string | null;
  size_bytes?: number | null;
  uploaded_at: string;
  uploaded_by_name?: string | null;
}

/** One of the seven slots. `document: null` renders "Not Available Yet". */
export interface ComplianceSlot {
  document_type: ComplianceDocumentType;
  label: string;
  group: ComplianceDocumentGroup;
  document: ComplianceDocument | null;
}

export interface Customer {
  id: string;
  name: string;
  industry?: string | null;
  website_domain?: string | null;
  /** The internal tenant key. Displayed as a subtitle, never editable. */
  domain: string;
  status: CustomerStatus;
  archived_at?: string | null;
  created_at: string;
  notes?: string | null;
  primary_contact: PrimaryContact;
  team_size: number;
  analytics: CustomerAnalytics;
}

export interface CustomerTeamMember {
  id: string;
  name?: string | null;
  email?: string | null;
  role: string;
  status: string;
}

export interface CustomerDetail extends Customer {
  culture?: string | null;
  details?: string | null;
  team: CustomerTeamMember[];
  compliance_documents: ComplianceSlot[];
}

export interface CustomerListResponse {
  customers: Customer[];
  total: number;
  page: number;
  page_size: number;
}

// ---- Admin ----

export interface TenantProfile {
  id: string;
  name: string;
  industry?: string | null;
  culture?: string | null;
  details?: string | null;
  created_at: string;
  client_email?: string | null;
  client_name?: string | null;
  client_phone?: string | null;
  editable: boolean;
}

export interface AuditLogEntry {
  id: string;
  tenant_id: string | null;
  actor_id?: string | null;
  actor_email?: string | null;
  action: string;
  detail?: string | Record<string, unknown> | null;
  created_at: string;
}

// ---- Company ----

/** Roles creatable through the staff page (contract rev 2). */
export type StaffRole =
  | "recruitment_manager"
  | "hr_manager"
  | "recruiter"
  | "hiring_manager"
  | "interview_manager"
  | "ceo"
  | "md"
  | "functional_head";

/** Row from GET /companies/departments (the leadership release, spec 13). */
export interface Department {
  id: string;
  name: string;
  is_active: boolean;
}

/** Row from GET /companies/me/staff (contract rev 2). */
export interface StaffMember {
  id: string;
  email: string;
  full_name: string;
  phone?: string | null;
  role: StaffRole;
  status: string;
  approval_level?: string | null;
  /** A Functional Head's one department. */
  department_id?: string | null;
  department_name?: string | null;
  /**
   * Whether THE CALLER may edit, re-invite, deactivate or re-permission this
   * person. The server answers it per row; a view-only reader (a CEO or MD)
   * sees every row with it false.
   */
  can_manage?: boolean;
  created_at?: string | null;
  invite_status?: "pending" | "accepted" | "revoked" | "expired" | null;
  invite_sent_at?: string | null;
  invite_expires_at?: string | null;
  invite_link?: string | null;
  email_dispatch?: "queued" | "not_configured" | null;
}

/**
 * Row from GET /admin/bd-users, ReadyPick's own Business Development team.
 *
 * There is no tenant on this record and there never will be: a BD user is
 * platform staff. `signed_in` is false until Firebase binds an identity on the
 * person's first login, which is the usual reason a new account looks broken.
 */
export interface BDUser {
  id: string;
  email: string;
  full_name?: string | null;
  phone?: string | null;
  status: "invited" | "active" | "disabled" | string;
  created_at?: string | null;
  signed_in: boolean;
}

export type ApprovalLevelName =
  | "requested"
  | "recommended"
  | "approved"
  | "ratified";

export interface ApprovalLevelConfigEntry {
  active: boolean;
  approver_user_id: string | null;
}

export type ApprovalLevelsConfig = Record<
  ApprovalLevelName,
  ApprovalLevelConfigEntry
>;

// ---- Jobs ----

/**
 * GET /jobs/{id}/assessment-retention, and the body of both dispute routes.
 *
 * Dates only: no candidate detail and no count of assessed people, because
 * the route exists precisely where assessment facts are withheld. `message`
 * is the server's own sentence and is rendered verbatim.
 */
export interface AssessmentRetention {
  state: "live" | "pending_deletion" | "purged";
  closed_at: string | null;
  purge_due_at: string | null;
  purged_at: string | null;
  dispute_open: boolean;
  days_remaining: number | null;
  message: string | null;
  dispute_reason: string | null;
}

export interface JobJD {
  description: string;
  reporting_to: string;
  reportees: string | number;
  role: string;
  responsibilities: string | string[];
  accountabilities: string | string[];
  education: string;
  skills: string[];
  experience_years: number | string;
}

/**
 * Job grade (spec §5/§6). Drives the technical question count and the number
 * of PPI questions the candidate is asked (25/20/15/10). Required on
 * create; every job response carries it (never null).
 */
export type JobGrade = "non_managerial" | "managerial" | "leadership" | "cxo";

export const JOB_GRADES: { value: JobGrade; label: string }[] = [
  { value: "non_managerial", label: "Non-Managerial" },
  { value: "managerial", label: "Managerial" },
  { value: "leadership", label: "Leadership" },
  { value: "cxo", label: "CXO" },
];

export const jobGradeLabel = (grade?: JobGrade | string | null): string =>
  JOB_GRADES.find((g) => g.value === grade)?.label ?? "-";

export type JobStatus =
  | "draft"
  | "requested"
  | "recommended"
  | "approved"
  | "ratified"
  | "rejected";

export interface Job {
  id: string;
  title: string;
  department: string;
  /** The job's department row (the leadership release, spec 13). */
  department_id?: string | null;
  // `level` is gone (ReadyPick release, Phase 1): nothing writes or reads the
  // free-text seniority any more. The grade and the experience band replaced
  // it; the column survives in the database as history only.
  /** The experience band this role expects, in years. */
  experience_min_years?: number | null;
  experience_max_years?: number | null;
  requirement_period: string;
  /**
   * The whole job description as one markdown document. Canonical since
   * 2026-07-28; the per-section fields on `jd` are derived from it.
   */
  jd_markdown?: string | null;
  /**
   * Absolute, shareable application link for this job. Present once the job is
   * published. This is the link a recruiter posts to LinkedIn or Naukri.
   */
  public_application_url?: string | null;
  /** Assessment grade (spec §5/§6), always present on a job response. */
  grade: JobGrade;
  /**
   * STEM / Non-STEM classification (directive Part 3). System-determined,
   * read-only for the client; drives the per-report credit cost shown beside
   * it. Absent on responses from a pre-directive backend.
   */
  role_classification?: "STEM" | "NON_STEM" | string;
  credit_cost_per_report?: number;
  status: JobStatus;
  jd: JobJD;
  compensation?: Record<string, unknown> | null;
  /**
   * Legacy/raw column names still emitted by the backend alongside the
   * canonical `jd` / `compensation` aliases. Read defensively via
   * `jobJd(job)` / `jobCompensation(job)` so the UI never silently degrades
   * to "-" when only one of the two names is present.
   */
  jd_json?: Partial<JobJD> | null;
  compensation_json?: Record<string, unknown> | null;
  created_at?: string;
  archived_at?: string | null;
  /**
   * Readiness of the auto-generated assessment. There is no recruiter-facing
   * question-bank UI, generation and finalization are fully automatic.
   */
  assessment_status?: "questions_pending_review" | "ready_for_candidates";
  /**
   * Company-narrative JD sections (spec §3.1). Already RESOLVED by the backend
   * through the per-job override -> company profile chain, so the UI renders
   * them directly and never has to know which layer supplied the text.
   */
  about_company?: string | null;
  work_life?: string | null;
  benefits?: string | null;
  /** Which of the three this job overrides, vs inherits from the company. */
  overridden_sections?: string[];
  public_url?: string | null;

  // ── Fixed 30-day posting window (spec §2.1) ────────────────────────────────
  // The recruiter never sets these. `posting_start_date` is stamped at publish;
  // the other two are database-generated and immutable.
  posting_start_date?: string | null;
  posting_end_date?: string | null;
  grace_period_end_date?: string | null;
  posting_status?: PostingStatus | null;
  days_until_posting_ends?: number | null;
  days_until_grace_ends?: number | null;
  posting_summary?: string | null;
  /** When the client closed this posting early, and their own words on why.
   *  Team-facing only: no candidate surface renders either. */
  closed_at?: string | null;
  closed_reason?: string | null;
  /** What happens at the third monitoring warning (proctoring spec 6).
   *  Absent on responses from a backend without proctoring. */
  proctoring_warning_policy?: ProctoringWarningPolicy;
}

/**
 * Where a job sits in its fixed 30-day lifecycle.
 *
 * `closed` is the client's own early stop, once the hiring requirement is met
 * (workflow Gate 8). It is not one of the four date-derived states and it
 * outranks all of them, which is why it reads differently below: the other
 * four describe a calendar, this one describes a decision.
 */
export type PostingStatus =
  | "scheduled"
  | "active"
  | "grace_period"
  | "expired"
  | "closed";

export const POSTING_STATUS_LABELS: Record<PostingStatus, string> = {
  scheduled: "Not yet live",
  active: "Live",
  grace_period: "Closed, grace period",
  expired: "Expired",
  closed: "Closed, requirement met",
};

// ── The 10-stage hiring pipeline (spec §3.3) ─────────────────────────────────

export const PIPELINE_STATUSES = [
  // Gate 5: a resume the recruiter uploaded from their databank, belonging to
  // somebody who has not applied. Its only forward edge is `applied`, which
  // the candidate takes themselves.
  "sourced",
  "applied",
  "assessment_invited",
  "assessment_in_progress",
  "assessment_completed",
  "shortlisted",
  "interview_scheduled",
  "interview_completed",
  "offer_extended",
  "joined",
  "hold",
  "rejected",
] as const;
export type PipelineStage = (typeof PIPELINE_STATUSES)[number];

export const PIPELINE_LABELS: Record<PipelineStage, string> = {
  sourced: "Sourced, not yet applied",
  applied: "Application received",
  assessment_invited: "Assessment invitation sent",
  assessment_in_progress: "Assessment in progress",
  assessment_completed: "Assessment complete, under review",
  shortlisted: "Shortlisted",
  interview_scheduled: "Interview scheduled",
  interview_completed: "Interview complete",
  offer_extended: "Offer extended",
  joined: "Joined",
  hold: "On hold",
  rejected: "Not proceeding",
};

/** Short label for a table cell, where the full sentence is too wide. */
export const PIPELINE_SHORT_LABELS: Record<PipelineStage, string> = {
  sourced: "Sourced",
  applied: "Applied",
  assessment_invited: "Invited",
  assessment_in_progress: "Assessing",
  assessment_completed: "Assessed",
  shortlisted: "Shortlisted",
  interview_scheduled: "Interview set",
  interview_completed: "Interviewed",
  offer_extended: "Offer out",
  joined: "Joined",
  hold: "On hold",
  rejected: "Rejected",
};

export interface StatusEvent {
  status: string;
  label: string;
  at: string;
}

export interface PipelineStageCount {
  status: PipelineStage;
  label: string;
  count: number;
}

export interface CandidatePipeline {
  job_id: string;
  stages: PipelineStageCount[];
  total: number;
}

export interface ApplicationStatusResponse {
  link_id: string;
  status: PipelineStage;
  stage_label: string;
  status_updated_at?: string | null;
  allowed_transitions: PipelineStage[];
  timeline: StatusEvent[];
  email_queued?: boolean;
}

// ── The one rating scale (spec §10.2) ────────────────────────────────────────
// FOUR grades, and only four. They replaced the product's two parallel
// five-label scales on 2026-07-30: the matching scale that labelled the ranking
// comments, and the assessment scale that labelled report dimensions. Those had
// to be kept in step by hand, and a reader had no way to know that a "High" and
// a "Matching" meant the same thing.
//
// Numbers never appear on it. The backend (services/rating.py) converts its
// internal score to a word and only the word crosses the API boundary.

export const RATING_GRADES = [
  "Highly Matching",
  "Matching",
  "Moderately Matching",
  "Not Matching",
] as const;
export type RatingGrade = (typeof RATING_GRADES)[number];

export type RatingWordLabel = RatingGrade;

/** The three ways a candidate reaches a job. See `RankedCandidate.source_type`. */
export type CandidateProcurement = "applied" | "sourced" | "databank";

/** One legal pipeline move, with the label the server wants shown for it. */
export interface TransitionOption {
  status: PipelineStage;
  label: string;
}

/**
 * Old Profiles vs New Profiles, derived server-side from the application date
 * against the job's CURRENT posting window. A job that was never renewed has
 * only New Profiles.
 */
export type ProfileAge = "old" | "new";

export interface ReviewProfileResponse {
  profile_age: ProfileAge;
  /** False for a New Profile and for a re-open, both of which are free. */
  charged: boolean;
  subunits_charged: number;
}

/** One mandatory application field, or one candidate profile-form item, and
 *  the candidate's exact answer. */
export interface ValidationAnswer {
  key: string;
  question: string;
  /** Exactly as submitted. Null when this application predates the field or the
   *  candidate left it blank; the row still renders, saying "Not answered",
   *  because "never asked" and "did not answer" look identical when a row is
   *  simply missing and only one of them is the candidate's doing. */
  answer: string | null;
  /** "Application" for the six mandatory fields, or the profile form's own
   *  section title (e.g. "Work Experience") for the 38 profile items. Used
   *  only to group the modal into readable sections. */
  group?: string;
}

/** One piece of evidence the resume check found, or a Must-have it did not.
 *  Mirrors `schemas/ranking.EvidenceTagOut`. `text` is the skill's CURRENT
 *  name or a short server-vetted phrase; `shown_in_row` is the SERVER's choice
 *  of which tags fit on the table row (the Details dialog shows them all). */
export interface EvidenceTag {
  text: string;
  polarity: "positive" | "negative";
  shown_in_row: boolean;
}

/** What the resume check holds for a row. `legacy` is a grade carried over
 *  from the retired matcher until AI Matching runs again. */
export type AiMatchStatus = "pending" | "scored" | "not_assessed" | "legacy";

/** One row of the job page's inline candidate table. Carries no numbers:
 *  mirrors `schemas/ranking.RankedCandidateOut`, which forbids extra keys. */
export interface RankedCandidate {
  link_id: string;
  candidate_id: string;
  full_name: string;
  /**
   * COMPANY-JOB-CANDIDATE, e.g. "K7QP-2M4X-9TB1". Rendered under the name.
   * One readable handle for this application, stable everywhere it appears.
   * Derived server-side and one-way; it identifies a row without disclosing
   * anything about it, and it is never an authorisation input.
   */
  reference_code?: string;
  email?: string | null;
  source?: CandidateSource | null;
  archived_at?: string | null;
  /** The application's Profile. Resumes live in private storage, so this is
   *  the handle the viewer and the download endpoint are keyed on. */
  profile_id?: string | null;
  /**
   * Whether a resume exists, and NOT where it is.
   *
   * This replaced `resume_url`, which carried the raw `s3://bucket/key`
   * object reference. A browser cannot fetch that, so the only thing this
   * screen ever did with it was ask whether it was truthy, while it handed
   * every recruiter's browser the bucket name and the object key for nothing.
   * The resume itself is read through the authorized proxy route, built by
   * `resumeTabUrl` from `profile_id` plus the two descriptive fields below.
   */
  has_resume?: boolean;
  resume_filename?: string | null;
  resume_mime_type?: string | null;
  has_report: boolean;
  report_ready_at?: string | null;
  /** Where this applicant came from (spec §1.1). */
  application_source?: "direct" | "sourced" | "external_link" | null;
  /** How this candidate was procured. Applied means they came through
   *  ReadyPick themselves, sourced means a third-party link, databank means
   *  the recruitment team uploaded them in bulk. All three are parsed,
   *  matched and assessed identically; this is display and filtering only. */
  source_type: CandidateProcurement;
  /** Server-rendered display text for `source_type`. */
  source_type_label: string;
  /** "Databank, not an applicant" / "Sourced, not an applicant" while the
   *  candidate has not applied, null once they have. Server-worded. */
  applicant_label?: string | null;
  /** `old` when this application arrived BEFORE the job's current 30-day
   *  posting window, i.e. the job has since been renewed. Presentation and
   *  billing only: an Old Profile is ranked, listed and openable exactly like
   *  a new one. */
  profile_age: ProfileAge;
  /** "Old Profile" / "New Profile", so the UI never renders a raw enum. */
  profile_age_label: string;
  /** Applied after the last assessment round on this job, so nobody has
   *  considered them yet (workflow section 32). Presentation only: it changes
   *  no score, no ranking and no access. */
  is_new_candidate: boolean;
  /** True once someone on the team has already paid the bulk review rate for
   *  this profile, so reopening it costs nothing. */
  review_charged: boolean;
  status: PipelineStage;
  stage_label: string;
  status_updated_at?: string | null;
  /** Legal moves from here, the UI renders exactly these, so no button can
   *  appear that would 409. */
  allowed_transitions: PipelineStage[];
  /** The same legal moves, each with the label to show. Prefer this over
   *  `allowed_transitions`: the labels come from the server, so the UI never
   *  has to hardcode a stage name it might get wrong. */
  allowed_transition_options: TransitionOption[];
  /** AI Match (Yukti), words only. The grade word is blended with the Tatva
   *  Assessment once there is one; null when there is no grade at all, in
   *  which case `ai_match_status_word` says why ("Not checked yet" /
   *  "Not assessed"). No score, percentage or rank ever arrives here. */
  ai_match_status: AiMatchStatus;
  ai_match_label: RatingGrade | null;
  ai_match_status_word: string | null;
  /** Positives first, in the server's order. */
  evidence_tags: EvidenceTag[];
  /** Where the grade came from, as server-written sentences. */
  provenance: string[];
  /** The skills or the resume changed after the check: a rerun refreshes it. */
  ai_match_stale: boolean;
  validation_answers: ValidationAnswer[];
  /** PRISM Report availability word: Available / Processing / Not available. */
  prism_report_status?: string;
  /** Proctoring Report availability word, same vocabulary. */
  proctoring_report_status?: string;
  /** "Within range" / "Above range" / "Below range", or null for "Not
   *  stated". Derived server-side; nothing here computes a comparison. */
  ctc_match_label?: string | null;
  /** The brief's notice bucket ("Immediate", "Within 30 days", ...), or
   *  null when the candidate stated none. */
  notice_period_label?: string | null;
  /** "Match" / "Partial match" / "No match", or null when either the JD or
   *  the candidate is silent about education. */
  education_match_label?: string | null;
  /** Raw derived BGV status ('verified' | 'pending' | ...), for logic. */
  bgv_status?: string;
  /** "Done" / "Pending" / "Not Started" / "Not Required" / "Not Confirmed",
   *  server-worded; detail lives inside the candidate's profile only. */
  bgv_status_label?: string;
}

export interface RankedCandidatesResponse {
  job_id: string;
  grade: JobGrade;
  /** The one line above the table, written by the server. */
  ranking_header: string;
  results: RankedCandidate[];
  total: number;
  page: number;
  page_size: number;
  total_pages: number;
  has_next: boolean;
  has_previous: boolean;
  range_start: number;
  range_end: number;
  /** How many candidates on this JOB arrived after the last assessment round.
   *  Counted over the whole job and never narrowed by the page filters, so it
   *  still reads correctly while the supplement is filtered out of the view. */
  new_candidate_count: number;
}

/**
 * A Team Review verdict, per the Candidate Dashboard Specification Column 7.
 *
 * A DECISION vocabulary, deliberately not the four machine grades a candidate
 * is assessed on. Keep them distinct: a colleague's note rendered in the
 * machine's words reads as a machine grade, which is the opposite of what the
 * Team Review panel is for. `backend/app/services/team_review.py` carries the
 * argument and the override-rate mapping between the two.
 */
export type TeamRating = "pass" | "hold" | "reject";

export interface CandidateTeamReview {
  id: string;
  reviewer_user_id: string;
  reviewer_name: string;
  rating: TeamRating;
  remarks: string;
  ai_rewritten_remarks: string | null;
  is_current_user: boolean;
  created_at: string;
  updated_at: string;
}

export interface CandidateTeamReviews {
  reviews: CandidateTeamReview[];
  overall_rating: TeamRating | null;
  overall_remarks: string | null;
  review_count: number;
}

export interface TeamReviewRewrite {
  rewritten_remarks: string;
  used_ai: boolean;
}

/** Company Portal -> Profile (spec §3.2). */
export interface CompanyProfile {
  tenant_id: string;
  company_name: string;
  industry?: string | null;
  about_company?: string | null;
  work_life?: string | null;
  benefits?: string | null;
  recommended_min_chars: number;
  recommended_max_chars: number;
}

// ── Lifecycle emails (spec §6) ───────────────────────────────────────────────

export const EMAIL_TYPES = [
  "application_confirmation",
  "assessment_reminder",
  "shortlist",
  "rejected",
  "hold",
  "question_bank_reminder",
] as const;
export type EmailType = (typeof EMAIL_TYPES)[number];

export const EMAIL_TYPE_LABELS: Record<EmailType, string> = {
  application_confirmation: "Application received",
  assessment_reminder: "Assessment reminder",
  shortlist: "Shortlisted, moving forward",
  rejected: "Not proceeding",
  hold: "On hold",
  question_bank_reminder: "Internal: job needs review",
};

export interface EmailDraft {
  link_id: string;
  candidate_id?: string | null;
  recipient_email?: string | null;
  candidate_name?: string | null;
  email_type: string;
  subject: string;
  body: string;
  /** False when a provider outage forced the deterministic template. */
  generated_by_ai: boolean;
}

export interface EmailDraftsResponse {
  email_type: string;
  drafts: EmailDraft[];
  skipped: { link_id: string; name?: string; reason: string }[];
}

export interface EmailLogEntry {
  id: string;
  email_type: string;
  recipient_email: string;
  subject: string;
  body: string;
  status: "queued" | "sent" | "failed";
  error?: string | null;
  edited_by_human: boolean;
  generated_by_ai: boolean;
  created_at: string;
  sent_at?: string | null;
}

/** Per-user permission matrix (spec §7.1). */
export interface StaffPermissions {
  user_id: string;
  role: Role;
  full_name?: string | null;
  email?: string | null;
  all_capabilities: Capability[];
  /** Granted by the ROLE, before any per-user pin. */
  role_defaults: Capability[];
  /** Explicit per-user pins. Sparse, absent means "follow the role". */
  overrides: Record<string, boolean>;
  /** What actually applies: role defaults with the overlay on top. */
  effective: Capability[];
  role_label?: string | null;
  /** Capabilities the current manager is allowed to grant. */
  grantable: Capability[];
}

export interface CompanyProfileResearch {
  about_company: string;
  work_life: string;
  benefits: string;
  sources: string[];
  degraded: boolean;
  message?: string | null;
}

/** The JD, whichever field name the backend used. Never undefined. */
export const jobJd = (job: Job | null | undefined): Partial<JobJD> =>
  (job?.jd ?? job?.jd_json ?? {}) as Partial<JobJD>;

/** The compensation object, whichever field name the backend used. */
export const jobCompensation = (
  job: Job | null | undefined
): Record<string, unknown> =>
  (job?.compensation ?? job?.compensation_json ?? {}) as Record<string, unknown>;

// ---- Candidates & matching ----

export type CandidateSource = "fresh" | "databank";

export type PipelineStatus =
  | "rejected"
  | "shortlisted"
  | "hold"
  | "offered"
  | "joined"
  | "pending"
  | string;

// ---- Portal ----


export interface PortalJob {
  id: string;
  title: string;
  department?: string;
  // No `level`: the portal job payload stopped carrying it (the experience
  // band and the grade answer that question; the column is history only).
  tenant_name?: string;
  company_name?: string;
  /**
   * Slug of the employer's PUBLIC page (/employers/{slug}); null whenever the
   * page is hidden, so the portal never links to a URL that 404s.
   */
  company_slug?: string | null;
  /** True when this candidate already holds an APPLICATION on the job. A
   *  recruiter's sourced databank entry is not one. */
  already_applied?: boolean;
  /** The application's id when `already_applied`, for the Applied Jobs link. */
  application_id?: string | null;
  /** Present on the single-job read; the list endpoint may omit it. */
  jd?: Record<string, unknown> | null;
  jd_json?: Record<string, unknown> | null;
  /** Assessment grade, drives how many technical questions are asked. */
  grade?: string | null;
  // The employer, as the candidate needs to see it before applying.
  company_about?: string | null;
  company_culture?: string | null;
  company_industry?: string | null;
  company_benefits?: string | null;
  /** The job's skills by bucket, names only (CONTRACT v10). An empty list on
   *  a job with no SAVED skills; read through `postingSkillsFrom`. */
  skill_buckets?: PostingSkillBucket[] | null;
}

/**
 * One bucket of a job's skills as a candidate reads them (CONTRACT v10,
 * point 2; `s4-api-shapes.md` section 4): the server's label and the skill
 * NAMES, alphabetical. The hidden evidence line, the priority and the role
 * summary are assessment context and never ride this shape.
 */
export interface PostingSkillBucket {
  bucket: "must_have" | "nice_to_have" | "behavioural";
  label: string;
  names: string[];
}

/**
 * `GET /api/v2/assessments/jobs/{id}/posting-preview` (`PostingPreviewOut`):
 * the candidate-facing posting, for the recruiter, before and after
 * publishing. Every word here is the server's.
 */
export interface PostingPreview {
  job_id: string;
  title: string;
  department: string | null;
  grade: string;
  grade_label: string;
  experience_band: string | null;
  jd_markdown: string | null;
  company_name: string | null;
  about_company: string | null;
  work_life: string | null;
  benefits: string | null;
  skill_buckets: PostingSkillBucket[];
  /** False: the names are the team's unsaved working set. */
  skills_saved: boolean;
  published: boolean;
  public_application_url: string | null;
  publish_blocked_reason: string | null;
  frozen: boolean;
  frozen_at: string | null;
  frozen_reason: string | null;
}

export interface PortalApplication {
  id: string;
  job_id?: string;
  job_title: string;
  company_name?: string;
  status: string;
  updated_at?: string;
}

// ---- Verification form (public) ----



// ---- Dashboard ----

export interface DashboardJobMetrics {
  job_id: string;
  title: string;
  databank_matched: number;
  fresh_sourced: number;
  shortlisted: number;
  offered: number;
  joined: number;
}

export interface DashboardSummary {
  jobs: DashboardJobMetrics[];
  total_jobs_worked: number;
}

// ---- AI Dashboard: REMOVED (spec 30) ----
// `AIGradeCount`, `AIAssessmentFunnel`, `AIFrameworkHealth` and `AIDashboard`
// lived here. The feature was removed from the customer portal entirely: the
// page, the component, the route and its response schemas all went in the same
// change, so these types described a payload nothing sends.

// ---- Billing and credits (per-credit only since 2026-09-29) ----

/**
 * Credits are exchanged in SUB-UNITS everywhere except display. One credit is
 * 60 sub-units, so 1/3, 1/15 and 1/20 of a credit are all whole numbers and no
 * arithmetic on this side of the wire ever touches a float. The server also
 * sends `*_credits` already rounded to two decimals: render those, compute with
 * the sub-units.
 */
export const SUBUNITS_PER_CREDIT = 60;

export type CreditEventType =
  | "grant"
  | "completed_assessment"
  | "incomplete_assessment"
  | "no_show"
  | "old_profile_review"
  | "adjustment"
  // A credit lot reaching its expiry with sub-units left (2026-09-22, new
  // grants only). The ledger has written it since then; the type had not.
  | "expiry";

export interface UsageBreakdown {
  completed_assessment: number;
  incomplete_assessment: number;
  no_show: number;
  old_profile_review: number;
  adjustment: number;
}

export interface CreditSummary {
  balance_subunits: number;
  balance_credits: string;
  subunits_per_credit: number;
  granted_subunits: number;
  consumed_subunits: number;
  rollover_subunits: number;
  rollover_credits: string;
  usage_this_month_subunits: UsageBreakdown;
  in_deficit: boolean;
  deficit_message: string | null;
  exhausted: boolean;
  low_balance: boolean;
  balance_fraction: number;
  low_balance_threshold: number;
  /** Directive Part 5 §4: 0 none, 1 LOW (≤20 credits), 2 CRITICAL (≤10). */
  warning_level?: number;
  warning_1_threshold_credits?: number;
  warning_2_threshold_credits?: number;
  /** §4.2 estimate: balance ÷ 30-day average credits per report. */
  estimated_assessments_remaining?: number;
  average_credits_per_assessment?: number;
  alert_message: string | null;
  unlimited: boolean;
  /** How long a NEW grant stays spendable. Credits granted before expiry
   *  was introduced carry no expiry at all (change request 25). */
  credit_validity_months: number;
}

export interface CreditLedgerEntry {
  id: string;
  event_type: CreditEventType;
  subunits_delta: number;
  credits_delta: string;
  created_at: string;
  job_candidate_link_id: string | null;
}

export interface BillingTransaction {
  id: string;
  razorpay_payment_id: string | null;
  amount_inr: number;
  status: "success" | "failed" | "refunded";
  transaction_type: "credit_pack" | "subscription_charge" | "refund";
  created_at: string;
}

export interface BillingOverview {
  credits: CreditSummary;
  razorpay_key_id: string | null;
  recent_ledger: CreditLedgerEntry[];
  transactions: BillingTransaction[];
}

// ── Credit packs, one-time purchases (directive Part 5 sections 3 and 7) ─────

/**
 * One purchasable credit pack, PRICED SERVER-SIDE. The server sends every line
 * of the breakdown (subtotal, GST, total) already computed, so the page
 * renders the server's quote. Historical setup fee fields remain in the API.
 */
export interface CreditPack {
    slug: string;
    label: string;
    credits: number;
    bonus_credits: number;
    credits_total: number;
  subtotal_inr: number;
  setup_fee_inr: number;
  setup_fee_waived: boolean;
  gst_inr: number;
  total_inr: number;
  available: boolean;
  trial: boolean;
}

export interface CreditPacksResponse {
  packs: CreditPack[];
  gst_rate_percent: number;
}

/**
 * GET /company-onboarding/state: where this browser's company registration
 * stands. DERIVED by the server on every read from the registration row and
 * the paid purchase, so the register page never keeps its own copy.
 */
export interface OnboardingState {
  stage: "details" | "verify_email" | "choose_pack" | "set_password" | "done";
  email?: string | null;
  first_name?: string | null;
  company_name?: string | null;
}

/** POST /billing/purchase: a Razorpay Order created and waiting for payment. */
export interface PurchaseCreateResponse {
  purchase_id: string;
  razorpay_order_id: string;
  razorpay_key_id: string;
  total_inr: number;
  credits: number;
  bonus_credits: number;
  subtotal_inr: number;
  setup_fee_inr: number;
  gst_inr: number;
}

/** One row of GET /billing/purchases: the purchase history with invoices. */
export interface CreditPurchaseRow {
  id: string;
  pack_slug: string;
  credits_purchased: number;
  bonus_credits: number;
  subtotal_inr: number;
  setup_fee_inr: number;
  gst_inr: number;
  total_inr: number;
  status: string;
  invoice_number: string | null;
  created_at: string;
  paid_at: string | null;
}

export interface ProviderBillingRow {
  tenant_id: string;
  customer_name: string;
  balance_subunits: number;
  balance_credits: string;
  in_deficit: boolean;
}

/**
 * GET /matching/jobs/{job_id}/tasks/{task_id}.
 *
 * `stages` is the inline reasoning the job page renders while a run is under
 * way. It is a fixed vocabulary the backend pipeline emits as it reaches each
 * stage, never a model narrating itself: the prompts behind this run carry a
 * real candidate's resume and a real client's JD, and a generated narration
 * could describe work that never happened.
 */
export interface MatchingTaskStatus {
  task_id: string;
  state: string;
  done: boolean;
  stages: Array<{
    key: string;
    label: string;
    detail: string;
    status: "pending" | "active" | "done" | "skipped" | "failed";
  }>;
  /** Counts of candidate ROWS being processed. Never a score or a rank. */
  candidate_count: number;
  scored_count: number;
  /** True when the run could not do everything it set out to, with the
   *  server's own sentences saying what. */
  degraded: boolean;
  degraded_reasons: string[];
}

// ---- Proctoring (proctoring spec sections 6 and 7) ----

/**
 * The recruiter's one monitoring setting, per job. `terminate` stops the
 * assessment at the third warning; `continue_and_note` lets it finish and
 * says so in the report. The default is never to terminate without an
 * explicit choice.
 */
export type ProctoringWarningPolicy = "terminate" | "continue_and_note";

export const PROCTORING_WARNING_POLICIES: ProctoringWarningPolicy[] = [
  "terminate",
  "continue_and_note",
];

export interface ProctoringReportFindings {
  screen_browser: string[];
  camera: string[];
  audio: string[];
  answer_patterns: string[];
}

export interface ProctoringActivityRow {
  time: string;
  what_happened: string;
  how_long: string;
  what_the_system_did: string;
}

/**
 * Mirrors `schemas/proctoring.ProctoringReportOut`. Words only: counts are
 * spelled out and durations are approximate, because this travels inside
 * the PRISM payload under the serialiser's number ban and because the
 * specification forbids counts, timings and internal terms in the
 * recruiter's view. The only digits are clock times.
 */
/**
 * Mirrors `schemas/proctoring.ProctoringStateOut`: where one application's
 * Proctoring Report stands, in the server's words. `message` is null for a
 * finished report with complete monitoring.
 */
export type ProctoringStateName =
  | "no_session"
  | "in_progress"
  | "generating"
  | "failed"
  | "ready"
  | "partially_unavailable"
  | "no_events";

export interface ProctoringState {
  state: ProctoringStateName;
  message: string | null;
}

export interface ProctoringReport {
  candidate: string;
  assessment: string;
  date_line: string;
  outcome: string;
  summary: string;
  findings: ProctoringReportFindings;
  activity_log: ProctoringActivityRow[];
  closing: string;
  /** True when a monitoring gap, a degraded device or an unavailable audio
   *  analysis means the report describes less than the whole session. */
  monitoring_was_incomplete: boolean;
  generated_at: string;
}

/* ── Talent Intelligence dashboards (2026-09-05 spec, sections 2-5) ──────────
 * Operational metrics only: latencies, ratios, compliance percentages. No
 * per-candidate assessment score, numeric grade or match percentage ever
 * travels through these types; candidate quality reaches a client only as
 * the four grade words, elsewhere. */

/** Server-decided health WORD; the client renders it and never re-bands. */
export type IntelligenceHealth = "green" | "amber" | "red" | "no_data";

export interface IntelligenceMetric {
  metric_id: string;
  title: string;
  formula: string;
  unit: string;
  thresholds: string;
  proxy_note: string | null;
  value: number | null;
  status: IntelligenceHealth;
  /** Set exactly when status is no_data: the plain-language reason there is
   *  nothing to show, rendered instead of a fabricated zero. */
  status_reason: string | null;
  inputs: Record<string, unknown>;
  segments?: Record<string, number | null> | null;
}

export type IntelligenceTier = "A" | "B" | "C" | "D";

export interface IntelligenceDashboardSummary {
  key: string;
  title: string;
  tier: IntelligenceTier;
  tier_title: string;
  audience: string;
  description: string;
  metric_ids: string[];
  /** Widgets the product can measure today; a measurable widget can still be
   *  no_data for a tenant with no rows yet. */
  metrics_measurable: number;
  metrics_total: number;
}

export interface IntelligenceDashboardTierGroup {
  tier: IntelligenceTier;
  title: string;
  dashboards: IntelligenceDashboardSummary[];
}

export interface IntelligenceDashboardIndex {
  tiers: IntelligenceDashboardTierGroup[];
}

export interface IntelligenceDashboardDetail {
  key: string;
  title: string;
  tier: IntelligenceTier;
  tier_title: string;
  audience: string;
  description: string;
  widgets: IntelligenceMetric[];
}

export interface IntelligenceAlert {
  id: string;
  severity: "red" | "amber";
  title: string;
  detail: string;
  link_path: string | null;
}

export interface IntelligenceAlerts {
  alerts: IntelligenceAlert[];
  computed_at: string;
}

// ── Background verification (add-features spec 2026-09-05) ────────────────

export type BgvDomainMatch = "matched" | "mismatched" | "indeterminate";

export type BgvInquiryStatus =
  | "collected"
  | "dispatched"
  | "dispatch_failed"
  | "response_received"
  | "parsed"
  | "parse_failed";

export interface BgvShareConsent {
  tenant_id: string;
  tenant_name: string | null;
  consented_at: string;
}

export interface BgvInquiry {
  id: string;
  employer_name: string;
  departmental_email: string;
  domain_match_result: BgvDomainMatch;
  status: BgvInquiryStatus;
  inquiry_sent_at: string | null;
  response_received_at: string | null;
  /** The seven parsed reply fields once extraction succeeds, else null. */
  parsed_fields: Record<string, string | boolean | null> | null;
  consents: BgvShareConsent[];
}

export interface BgvShareableTenant {
  tenant_id: string;
  tenant_name: string | null;
}

export interface BgvList {
  inquiries: BgvInquiry[];
  shareable_tenants: BgvShareableTenant[];
  can_add: boolean;
}

/* ── Corporate email senders (Corporate Email System spec, 2026-09-05) ───── */

export type EmailSenderStatus =
  | "pending_verification"
  | "active"
  | "disabled"
  | "revoked"
  | "rejected"
  // Retired with the mailbox OTP on 2026-09-08 and KEPT IN THE UNION, because
  // rows written before it still carry them. A union missing a value the API
  // can return makes every consumer of that row a type error or, worse, an
  // empty render.
  | "email_verified"
  | "verification_expired";

export interface EmailSender {
  id: string;
  name: string;
  email: string;
  status: EmailSenderStatus;
  // Retained for rows verified under the withdrawn mailbox check. Nothing
  // sets it any more; the provider's own identity verification is the
  // ownership check now.
  email_verified: boolean;
  authorized_at: string | null;
  created_at: string;
  /** Whether mail would actually leave for this address, asked of the
   *  provider at read time. Never a stored copy: the answer changes without
   *  this product being told. */
  can_send: boolean;
  /** One plain sentence for the Super Admin. Deliberately carries no AWS
   *  vocabulary: not SES, not an identity, not DKIM. */
  sending_detail: string;
  /** The tenant's ONE default sender: what every email that does not name a
   *  sender goes out under, the automatic ones included. Only an active
   *  sender can hold it, and leaving `active` clears it on the server. */
  is_default: boolean;
}

export interface EmailSenderList {
  senders: EmailSender[];
  can_manage: boolean;
  can_authorize: boolean;
}

/** The code itself is never in a response; it travels only to the mailbox. */
// EmailSenderOtpIssue was REMOVED on 2026-09-08 with the sender mailbox OTP.

// EmailSenderVerifyResult went with it: nothing verifies a code any more.

// ── The single-mode assessment (2026-09-24, Appendix B section 1) ────────────
//
// There is one assessment mode. The dual-mode types that lived here (the mode
// choice, the per-mode consent state and the video interview's question list)
// are deleted with the screens that used them. A mode is still STORED on old
// rows, so the recruiter-side payloads that describe a recording carry it as
// the server's plain string; nothing on the candidate side reads or sends one.

/** The mode a stored session was taken in: "conversational" for every new
 *  session, "video_interview" only on rows written before 2026-09-24. */
export type StoredAssessmentMode = string;

/** One consent item, server-authored (vivekium feature 6). The version is the
 *  wording's, bumped whenever the text changes, and it is stored with the
 *  tick so a later rewording cannot re-describe an agreement already given. */
export interface ConsentCatalogueItem {
  key: string;
  stage: string;
  text: string;
  version: number;
  /** Whether declining it stops the candidate. The stage says WHEN an item is
   *  asked; this says what refusing costs. An optional item never blocks. */
  required: boolean;
}

/** A consent item as the candidate's own record shows it: the current wording
 *  beside what they actually agreed to and when. `wording_current` false means
 *  their standing consent is to words that have since been replaced. */
export interface ConsentItemStatus extends ConsentCatalogueItem {
  consented_at: string | null;
  consented_version: number | null;
  wording_current: boolean;
}

/** The assessment's consent terms, exactly as the server will stamp them.
 *  One text for the one mode; the versions travel with it so the screen shows
 *  what the server will record. */
export interface AssessmentConsentTerms {
  text: string;
  consent_version: string;
  privacy_policy_version: string;
  terms_version: string;
  /** The Stage B per-item catalogue, rendered verbatim on the screen;
   *  acceptance stamps each item individually server-side. */
  items?: ConsentCatalogueItem[];
}

/** GET and POST /assessments/conversations/links/{id}/consent: whether this
 *  session has been consented to, and the terms that apply to it. */
export interface AssessmentConsentState {
  consented: boolean;
  consent: AssessmentConsentTerms;
}

export interface VideoRecordingStatus {
  recording_id: string;
  status: string;
  message: string;
  can_retry_upload: boolean;
}

// ── Client-portal video access (2026-09-05 dashboard/video spec 15-18) ──────

/** GET /videos/links/{link_id}: one application's video facts. Metadata only;
 *  no bucket, no object key, no score ever appears here. */
export interface VideoAccess {
  job_candidate_link_id: string;
  /** Null when no recording exists (every conversational session today). */
  recording_id: string | null;
  assessment_mode: StoredAssessmentMode | null;
  /** "Video interview" / "Conversational" / "Not started". */
  assessment_mode_label: string;
  /** "Ready" / "Processing" / "Failed" / "No recording". */
  video_status: string;
  /** One client-facing sentence explaining the word above. */
  video_status_detail: string;
  /** recording, uploaded, compressing, storing, ready, missing, failed or stalled. */
  video_stage: string;
  /** What the stage means and what the reviewer can do about it. */
  video_stage_detail: string;
  /** When the pipeline last reported on this recording; null when unknown. */
  last_reported_at: string | null;
  /** Set only once the recording is ready; null is "not known yet". */
  duration_seconds: number | null;
  compressed_size_bytes: number | null;
  /** Whether POST .../preview will mint a URL right now. */
  preview_available: boolean;
  /** Preview availability AND the candidate's download consent. */
  download_available: boolean;
  /** Why download is withheld while preview works; null otherwise. */
  download_blocked_reason: string | null;
  /** Whether the retry endpoint would accept this recording. */
  can_retry: boolean;
}

/** POST /videos/links/{link_id}/preview or /download: one minted short-lived
 *  URL. Returned once, expires on the server's clock, never stored. */
export interface VideoDelivery {
  url: string;
  expires_in_seconds: number;
  disposition: "inline" | "attachment";
  filename: string | null;
}

// ── In-product support (2026-09-10) ─────────────────────────────────────────
//
// Two audiences, one conversation. `SupportThread` is what a customer sees of
// their own thread; `ProviderSupportThread` adds the fact ReadyPick staff need
// and the customer already knows, which is WHOSE thread it is.
//
// The status names WHO OWES THE NEXT MOVE, which is the only thing a support
// queue is ever sorted by. "awaiting_customer" rather than "pending" so a
// reader cannot get the direction backwards.

export type SupportThreadStatus = "open" | "awaiting_customer" | "resolved";

export type SupportMessageSide = "customer" | "staff";

export interface SupportMessage {
  id: string;
  /** Stored at write time, never re-derived from the author's current role. */
  author_side: SupportMessageSide;
  /** Null once the author's account is gone. Rendered as an absence, never as
   *  "Deleted user", which is a claim about what happened to them. */
  author_name: string | null;
  body: string;
  created_at: string;
}

export interface SupportThread {
  id: string;
  subject: string;
  status: SupportThreadStatus;
  created_at: string;
  last_message_at: string;
  /** Operational: about a queue, never about a person. */
  message_count: number;
}

export interface SupportThreadDetail extends SupportThread {
  messages: SupportMessage[];
}

export interface ProviderSupportThread extends SupportThread {
  tenant_id: string;
  tenant_name: string;
  /** The first staff member who replied. Claimed by replying; there is no
   *  separate claim action. */
  assigned_to: string | null;
  assigned_to_name: string | null;
}

export interface ProviderSupportThreadDetail extends ProviderSupportThread {
  messages: SupportMessage[];
}

export interface SupportThreadPage {
  items: SupportThread[];
  total: number;
  page: number;
  page_size: number;
  total_pages: number;
  has_next: boolean;
  has_previous: boolean;
}

export interface ProviderSupportThreadPage {
  items: ProviderSupportThread[];
  total: number;
  page: number;
  page_size: number;
  total_pages: number;
  has_next: boolean;
  has_previous: boolean;
  /** Threads waiting on ReadyPick across every customer, UNNARROWED by the
   *  page filters: it answers how much is owed, not how much is on screen. */
  open_total: number;
}

// ---- The AI-assisted Job SWOT Analysis (2026-09-13 spec, sections 23 to 33) ----

/**
 * not_generated | generating | generated | failed | edited.
 *
 * `generating` (ReadyPick release, Phase 1): generation is DISPATCHED work now,
 * so the document says it is being drafted and the panel polls. A draft that
 * outlives the server's stale window is served as `failed`, never as
 * generating for ever.
 */
export type SwotAnalysisStatus =
  | "not_generated"
  | "generating"
  | "generated"
  | "failed"
  | "edited";

export interface SwotAnalysis {
  job_id: string;
  status: SwotAnalysisStatus;
  strengths: string | null;
  weaknesses: string | null;
  opportunities: string | null;
  threats: string | null;
  /** "ai" once a generation has succeeded, null for a hand-written document. */
  generated_by: string | null;
  last_generated_at: string | null;
  /** Why the last generation failed. Rendered only in the failed state. */
  generation_error: string | null;
  human_edited: boolean;
  last_modified_at: string | null;
  last_modified_by_name: string | null;
  version: number;
  can_restore_previous: boolean;
  /**
   * True when the saved SWOT is newer than the version the skills were drafted
   * from and the skills are not locked. The panel offers "Re-draft skills from
   * the updated SWOT"; nothing is re-drafted without that click. Absent on a
   * backend that predates the Skills step.
   */
  skills_redraft_available?: boolean;
  /**
   * The effective answer for this user on THIS job: the capability AND the
   * assignment scope AND the lifecycle state, resolved server-side by the same
   * call the write routes enforce with. The interface renders from this rather
   * than from the capability list alone, because the capability list cannot
   * express the per-job half of the question.
   */
  can_edit: boolean;
}

export interface SwotAnalysisDraft {
  strengths: string;
  weaknesses: string;
  opportunities: string;
  threats: string;
}
